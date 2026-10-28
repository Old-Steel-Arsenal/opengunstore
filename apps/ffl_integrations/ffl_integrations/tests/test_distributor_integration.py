"""Distributor frappe-integration tests (make test only; NOT imported by the
pure-logic harness). Covers the compliance receive leg and the two background
workers end-to-end on a real site (including a real receive):

* DO document lifecycle — non-submittable, illegal transition rejected, born Draft.
* poll_order — provider status folds onto lines (serials land) + status advances +
  the per-order backoff advances (wiring on a real doc).
* place_order_job — idempotent placement write-back, terminal-failure → Error,
  and the non-Queued double-guard (never re-places).
* receive_from_do — staff gate (DIVERGENCE-3), a two-batch `received_qty`
  increment, the FFL Acquisition carrying `distributor_order` provenance
  stamped BEFORE the (disabled) FastBound push (M2a), and the unit-cost
  pre-check that keeps the zero-cost guard from firing at the door on this
  UI-less path (2026-08-08).

FastBound is disabled here (push no-ops); the acquire_PurchaseOrderNumber mapping
itself is unit-tested in test_receive_bridge.TestFastBoundPoNumber.
"""
from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api.receive_goods import create_receive  # noqa: F401 — ensures app wired
from ffl_integrations.distributor import api as dist_api
from ffl_integrations.distributor import receive_bridge as rb
from ffl_integrations.distributor import tasks
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError
from ffl_integrations.tests import catalog_stub


def _ensure_supplier_group(name):
	if not frappe.db.exists("Supplier Group", name):
		frappe.get_doc({
			"doctype": "Supplier Group", "supplier_group_name": name,
			"parent_supplier_group": "All Supplier Groups", "is_group": 0,
		}).insert(ignore_permissions=True)


def _ensure_supplier(name, **fields):
	# A firearm receive acquires FROM an FFL dealer, so the supplier must be a
	# verified FFL (create_receive's FFL gate) — mirror test_receive_goods. Update
	# the fields even if the row already exists (warm DB).
	if frappe.db.exists("Supplier", name):
		for k, v in fields.items():
			frappe.db.set_value("Supplier", name, k, v, update_modified=False)
		return
	frappe.get_doc({
		"doctype": "Supplier", "supplier_name": name, "supplier_group": "Distributor",
		**fields,
	}).insert(ignore_permissions=True)


def _ensure_brand(name):
	# Item.manufacturer is a Link → Brand; the master must exist before the Item.
	if not frappe.db.exists("Brand", name):
		frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)


def _ensure_caliber(name):
	# Item.caliber is a Link → Caliber.
	if not frappe.db.exists("Caliber", name):
		frappe.get_doc({"doctype": "Caliber", "caliber_name": name}).insert(
			ignore_permissions=True, ignore_if_duplicate=True)


def _ensure_item(code, item_group, has_serial_no, is_firearm=0, **fields):
	if frappe.db.exists("Item", code):
		return
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": item_group, "stock_uom": "Nos", "is_stock_item": 1,
		"has_serial_no": has_serial_no, "is_firearm": is_firearm,
		"serial_no_series": f"{code}-.####" if has_serial_no else "",
		**fields,
	}).insert(ignore_permissions=True)


def _ensure_distributor(supplier):
	if frappe.db.exists("Distributor", "TEST-DC"):
		frappe.db.set_value("Distributor", "TEST-DC", "supplier", supplier, update_modified=False)
		return
	frappe.get_doc({
		"doctype": "Distributor", "code": "TEST-DC", "distributor_name": "Test Distributor",
		"provider": "rsr", "enabled": 1, "stocking_enabled": 1, "poll_enabled": 1,
		"supplier": supplier,
	}).insert(ignore_permissions=True)


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		_ensure_supplier_group("Distributor")
		# Verified FFL dealer (unique ffl_number per test module — the column is
		# UNIQUE; ...77777/...77778/...77779 are taken by other modules).
		_ensure_supplier("DC-TEST-SUPPLIER", is_ffl_dealer=1,
			ffl_number="9-99-999-99-9X-99991", ez_check_status="Verified")
		# DC-GUN's firearm-detail Link fields need their masters first (manufacturer
		# → Brand, caliber → Caliber); model_name/importer are Data and firearm_type
		# is a Select, so they need none.
		_ensure_brand("TestArms")
		_ensure_caliber("5.56")
		_ensure_item("DC-ACC", item_group="Ammunition", has_serial_no=0)
		_ensure_item("DC-GUN", item_group="Rifle", has_serial_no=1, is_firearm=1,
			manufacturer="TestArms", model_name="M1", firearm_type="Rifle", caliber="5.56")
		# RSR Catalog rows (now the remote-catalog stub, Task 8) so create_orders'
		# enrichment sees a matching ffl_required (else the classification-divergence guard
		# correctly fires: empty catalog → ffl_required False vs Item.is_firearm
		# True). Gun dept 05 (Sporting Long Guns), acc dept 13.
		cls.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "DC-GUN", "ffl_required": 1, "department_number": "05",
				"dealer_price": 400, "map_price": 400, "msrp": 400, "quantity": 10,
				"manufacturer": "TestArms", "model": "M1"},
			{"rsr_stock_number": "DC-ACC", "ffl_required": 0, "department_number": "13",
				"dealer_price": 1, "map_price": 1, "msrp": 1, "quantity": 10},
			# The shop's Item for this one is named by its UPC, not the house SKU.
			{"rsr_stock_number": "DC-UPC-ACC", "upc": "000000777001", "ffl_required": 0,
				"department_number": "13", "dealer_price": 1, "map_price": 1, "msrp": 1,
				"quantity": 10},
			{"rsr_stock_number": "DC-NO-ITEM", "ffl_required": 0, "department_number": "13",
				"dealer_price": 1, "map_price": 1, "msrp": 1, "quantity": 10},
		])
		_ensure_item("000000777001", item_group="Ammunition", has_serial_no=0)
		_ensure_distributor("DC-TEST-SUPPLIER")

	@classmethod
	def tearDownClass(cls):
		catalog_stub.uninstall()
		super().tearDownClass()

	def _do(self, lines, status="Draft"):
		do = frappe.get_doc({
			"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer", "lines": lines,
		}).insert(ignore_permissions=True)
		if status != "Draft":
			frappe.db.set_value("Distributor Order", do.name, "status", status,
				update_modified=False)
			do.reload()
		return do


class TestOrderLifecycle(_Base):
	def test_do_is_never_submittable(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}])
		self.assertEqual(do.docstatus, 0)
		do.status = "Queued"
		do.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "docstatus"), 0)

	def test_order_number_carries_the_year_and_month(self):
		# `.YYMM.` is not a Frappe naming token (only .YY. / .MM. / .DD. / ... are),
		# so it printed literally and every order was named DO-YYMM-#### on prod
		# until 2026-09-18. The series is config a typo cannot announce itself in.
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}])
		self.assertRegex(do.name, r"^DO-\d{4}-\d+$")

	def _plant_series_setters(self, options, default):
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		def drop():
			frappe.db.delete("Property Setter", {"doc_type": "Distributor Order",
				"field_name": "naming_series"})
			frappe.clear_cache(doctype="Distributor Order")
			frappe.local.new_doc_templates.pop("Distributor Order", None)
		# Rollback is per class, so a failure before the patch must not leave the
		# setters naming every later order in this class.
		self.addCleanup(drop)
		for prop, value in (("options", options), ("default", default)):
			make_property_setter("Distributor Order", "naming_series", prop, value, "Text",
				validate_fields_for_doctype=False)

	def _series_a_new_order_gets(self):
		# What set_name_by_naming_series picks. The new-doc template is cached per
		# process and clear_cache leaves it, so without the pop every order in this
		# class keeps the series of the first one and the setters never show.
		from frappe.model.naming import get_default_naming_series

		frappe.local.new_doc_templates.pop("Distributor Order", None)
		return (frappe.new_doc("Distributor Order").naming_series
			or get_default_naming_series("Distributor Order"))

	def test_stale_series_property_setter_is_fixed(self):
		# The JSON fix above never showed on a live site: an options + default
		# Property Setter pair still held the broken series, and a setter outranks
		# the JSON (DO-YYMM-0009, 2026-09-22). Document Naming Settings writes the
		# pair in more than one shape; each must be fixed.
		from ffl_integrations.patches import fix_distributor_order_series_token

		shapes = {
			"prod (exact)": ("DO-.YYMM.-.####", "DO-.YYMM.-.####"),
			"several series": ("DO-.YYMM.-.####\nDO-.YY..MM.-.####", "DO-.YYMM.-.####"),
			"user must always select": ("\nDO-.YYMM.-.####", ""),
		}
		for label, (options, default) in shapes.items():
			with self.subTest(label):
				self._plant_series_setters(options, default)
				self.assertIn(".YYMM.", self._series_a_new_order_gets())
				fix_distributor_order_series_token.execute()
				self.assertNotIn(".YYMM.",
					frappe.get_meta("Distributor Order").get_field("naming_series").options)
				self.assertEqual(self._series_a_new_order_gets(), "DO-.YY..MM.-.####")
				do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}])
				self.assertRegex(do.name, r"^DO-\d{4}-\d+$")

	def test_only_the_token_is_rewritten(self):
		# A deliberate extra series sharing the setter survives.
		from ffl_integrations.patches import fix_distributor_order_series_token

		self._plant_series_setters("DOX-.####\nDO-.YYMM.-.####", "DOX-.####")
		fix_distributor_order_series_token.execute()
		values = dict(frappe.get_all("Property Setter", fields=["property", "value"],
			filters={"doc_type": "Distributor Order", "field_name": "naming_series"},
			as_list=True))
		self.assertEqual(values, {"options": "DOX-.####\nDO-.YY..MM.-.####", "default": "DOX-.####"})

	def test_existing_order_no_longer_passes_the_broken_series_on(self):
		# Duplicate copies naming_series, and a non-empty one is used as it is.
		from ffl_integrations.patches import fix_distributor_order_series_token

		old = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}])
		frappe.db.set_value("Distributor Order", old.name, "naming_series", "DO-.YYMM.-.####",
			update_modified=False)
		fix_distributor_order_series_token.execute()
		self.assertEqual(frappe.db.get_value("Distributor Order", old.name, "naming_series"),
			"DO-.YY..MM.-.####")
		dup = frappe.copy_doc(frappe.get_doc("Distributor Order", old.name))
		dup.insert(ignore_permissions=True)
		self.assertRegex(dup.name, r"^DO-\d{4}-\d+$")

	def test_illegal_status_jump_rejected(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}])
		do.status = "Complete"  # Draft → Complete is illegal
		with self.assertRaises(Exception):
			do.save(ignore_permissions=True)

	def test_new_order_forced_to_draft(self):
		do = frappe.get_doc({
			"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer", "status": "Placed",
			"lines": [{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}],
		})
		with self.assertRaises(Exception):
			do.insert(ignore_permissions=True)


class TestPollOrderWorker(_Base):
	def test_poll_folds_status_and_advances_backoff(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1,
			"ship_method": "2Day"}], status="Placed")
		frappe.db.set_value("Distributor Order", do.name, {
			"placed_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30),
			"next_poll_at": frappe.utils.now_datetime()}, update_modified=False)

		class FakeProvider:
			def get_order_status(self, name):
				return {"po_num": name, "hold_status": "", "lines": [
					{"sku": "DC-GUN", "ack_qty": 1, "invoiced_qty": 1, "serials": ["SN-P1"],
						"tracking_numbers": ["1Z9"], "invoices": ["INV1"], "item_status": "OK"}]}

		with mock.patch.object(tasks, "_provider_for", return_value=FakeProvider()):
			tasks.poll_order(do.name)
		do.reload()
		self.assertEqual(do.status, "Complete")            # 1 of 1 invoiced → settled
		self.assertEqual(do.lines[0].serials, "SN-P1")     # serial landed on the line
		self.assertEqual(do.lines[0].line_status, "Shipped")
		self.assertTrue(do.next_poll_at)                   # backoff advanced

	def test_poll_backfills_a_missing_web_ref(self):
		"""A placement that answered without a WebRef (StatusCode 60 queued)
		leaves web_ref empty — and update-order-ffl is keyed by WebRef, so an
		FDS hold on such an order would be unreleasable. check-order echoes the
		WebRef; the poll is the self-heal point."""
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1}],
			status="Placed")
		frappe.db.set_value("Distributor Order", do.name, {
			"web_ref": "",
			"placed_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30),
			"next_poll_at": frappe.utils.now_datetime()}, update_modified=False)

		class FakeProvider:
			def get_order_status(self, name):
				return {"po_num": name, "web_ref": "1000001", "hold_status": "FDS Hold",
					"lines": [{"sku": "DC-GUN", "invoiced_qty": 0, "serials": [],
						"tracking_numbers": [], "invoices": [], "item_status": ""}]}

		with mock.patch.object(tasks, "_provider_for", return_value=FakeProvider()):
			tasks.poll_order(do.name)
		do.reload()
		self.assertEqual(do.web_ref, "1000001")

	def test_poll_never_overwrites_an_existing_web_ref(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1}],
			status="Placed")
		frappe.db.set_value("Distributor Order", do.name, {
			"web_ref": "W-ORIGINAL",
			"placed_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30),
			"next_poll_at": frappe.utils.now_datetime()}, update_modified=False)

		class FakeProvider:
			def get_order_status(self, name):
				return {"po_num": name, "web_ref": "W-OTHER", "hold_status": "",
					"lines": []}

		with mock.patch.object(tasks, "_provider_for", return_value=FakeProvider()):
			tasks.poll_order(do.name)
		do.reload()
		self.assertEqual(do.web_ref, "W-ORIGINAL")

	def test_poll_failure_backs_off_and_does_not_raise(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}], status="Placed")

		class FailingProvider:
			def get_order_status(self, name):
				raise RuntimeError("RSR down")

		with mock.patch.object(tasks, "_provider_for", return_value=FailingProvider()):
			tasks.poll_order(do.name)  # must not raise
		do.reload()
		self.assertTrue(do.next_poll_at)  # still backed off despite the failure


class TestPlaceOrderWorker(_Base):
	def test_place_writes_ack_and_sets_placed(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2,
			"ship_method": "Grnd"}], status="Queued")

		class FakeProvider:
			def place_order(self, name):
				return {"web_ref": "W-1", "hold_status": "", "raw": {}, "ack_lines": [
					{"sku": "DC-ACC", "ack_qty": 2, "unit_cost": 9.5, "item_status": "OK"}]}

		with mock.patch.object(tasks, "_provider_for", return_value=FakeProvider()):
			tasks.place_order_job(do.name)
		do.reload()
		self.assertEqual(do.status, "Placed")
		self.assertEqual(do.web_ref, "W-1")
		self.assertEqual(do.lines[0].ack_qty, 2)
		self.assertTrue(do.placed_at)

	def test_queued_placement_waits_out_the_maintenance_window(self):
		"""StatusCode 60 (order queued): check-order won't know the PONum until
		RSR's queue drains after the 3–5am ET window, so the first poll must
		wait it out instead of harvesting a 98 + admin alert at +30m."""
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}],
			status="Queued")

		class QueuedProvider:
			def place_order(self, name):
				return {"web_ref": "", "hold_status": "", "queued": True,
					"raw": {"StatusCode": "60"}, "ack_lines": []}

		with mock.patch.object(tasks, "_provider_for", return_value=QueuedProvider()):
			tasks.place_order_job(do.name)
		do.reload()
		self.assertEqual(do.status, "Placed")
		gap_s = frappe.utils.time_diff_in_seconds(do.next_poll_at, do.placed_at)
		self.assertGreaterEqual(gap_s, 150 * 60 - 5)   # 2h window + slack, not +30m

	def test_terminal_failure_sets_error(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}], status="Queued")

		class FailingProvider:
			def place_order(self, name):
				raise RuntimeError("StatusCode 12 invalid item")

		with mock.patch.object(tasks, "_provider_for", return_value=FailingProvider()):
			tasks.place_order_job(do.name)
		do.reload()
		self.assertEqual(do.status, "Error")

	def test_non_queued_order_is_never_placed(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}], status="Placed")
		called = []

		class FakeProvider:
			def place_order(self, name):
				called.append(name)
				return {}

		with mock.patch.object(tasks, "_provider_for", return_value=FakeProvider()):
			tasks.place_order_job(do.name)
		self.assertEqual(called, [])  # double-guard: not Queued → no placement


class TestReceiveFromDO(_Base):
	def setUp(self):
		super().setUp()
		# Receive pushes to FastBound synchronously; disable it (push no-ops) — the
		# DO→acquisition stamp happens BEFORE the push, so it's still asserted.
		p = mock.patch.object(FastBoundClient, "from_settings",
			side_effect=FastBoundConfigError("disabled in distributor tests"))
		p.start()
		self.addCleanup(p.stop)

	def _receive(self, do, warehouse):
		# Build the staff pre-fill payload (carries distributor_order), pin a
		# company-matched warehouse, then submit through the staff-gated receive.
		payload = rb.build_receive_payload(do.name)
		payload["warehouse"] = warehouse
		return rb.receive_from_do(do.name, json.dumps(payload))

	@property
	def _warehouse(self):
		company = frappe.new_doc("Purchase Receipt").company \
			or frappe.db.get_single_value("Global Defaults", "default_company")
		from ffl_core.warehouses import default_stock_warehouse
		return default_stock_warehouse(company)

	def test_line_meta_never_carries_item_importer(self):
		# The automated restock path has no operator to catch a wrong marking:
		# line meta starts importer blank even when the Item carries one
		# (2026-08-20 — Item.importer is not a per-gun default anywhere).
		# manufacturer still rides along: a model-level maker is a real SKU fact.
		frappe.db.set_value("Item", "DC-GUN", "importer", "Bucket Importer Inc")
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1}])
		meta = rb._line_with_item_meta(do.lines[0])
		self.assertEqual(meta["importer"], "")
		self.assertEqual(meta["manufacturer"], "TestArms")

	def test_drop_ship_order_is_never_received(self):
		# A drop-ship leg went distributor → buyer; receiving it would book stock and
		# an A&D acquisition for goods that never entered the store.
		for order_type in ("Dropship Accessory", "Dropship Firearm"):
			do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
				"unit_cost": 1.0}], status="Placed")
			frappe.db.set_value("Distributor Order", do.name, "order_type", order_type,
				update_modified=False)
			frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoiced_qty", 1,
				update_modified=False)
			with self.subTest(order_type=order_type):
				with self.assertRaises(frappe.ValidationError):
					rb.build_receive_payload(do.name)
				with self.assertRaises(frappe.ValidationError):
					rb.receive_from_do(do.name)
				self.assertEqual(frappe.db.get_value(
					"Distributor Order Line", do.lines[0].name, "received_qty"), 0)

	def test_non_staff_is_refused(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				rb.build_receive_payload("DO-NONE")
		finally:
			frappe.set_user("Administrator")

	def test_accessory_two_batch_received_qty_increment(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 5,
			"unit_cost": 1.0}], status="Placed")
		wh = self._warehouse
		# Batch 1: RSR invoiced 2 of 5.
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoiced_qty", 2,
			update_modified=False)
		do.reload()
		res1 = self._receive(do, wh)
		self.assertTrue(res1["ok"])
		do.reload()
		self.assertEqual(do.lines[0].received_qty, 2)
		self.assertEqual(do.receive_status, "Partial")
		# Batch 2: RSR invoiced the remaining 3 (cumulative 5).
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoiced_qty", 5,
			update_modified=False)
		do.reload()
		self._receive(do, wh)
		do.reload()
		self.assertEqual(do.lines[0].received_qty, 5)  # increment 3, not double
		self.assertEqual(do.receive_status, "Received")

	def test_a_gun_with_no_unit_cost_is_refused_on_the_order_not_at_the_door(self):
		"""The zero-cost guard's second caller. `unit_cost` reaches 0 whenever the
		catalog lookup missed or the RSR ack carried no Cost/Price
		(distributor/api.py:583,639 · rsr_provider.py:559), and the bridge used to
		send that 0 on as "rate": 0.0 — which the receive guard now reads as an
		unconfirmed free gun and refuses, from a server-to-server path with no
		checkbox anywhere on screen.

		So the refusal is moved forward to the pre-fill, where it names the order
		line an operator can actually edit. Asserted at build_receive_payload
		because that is what the DO's Receive button calls first: the operator is
		told before they are shown a preview of a receive that cannot succeed.
		"""
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1,
			"unit_cost": 0}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, {
			"invoiced_qty": 1, "serials": "SN-DO-GUN-NOCOST"}, update_modified=False)
		do.reload()
		with self.assertRaises(frappe.ValidationError) as ctx:
			rb.build_receive_payload(do.name)
		self.assertIn("DC-GUN", str(ctx.exception))
		# Nothing was booked: no receipt, and no bound-book acquisition.
		self.assertFalse(frappe.db.exists("Serial No", "SN-DO-GUN-NOCOST"))
		self.assertEqual(do.lines[0].received_qty, 0)

	def test_filling_the_unit_cost_in_lets_the_same_order_receive(self):
		"""The other half of the refusal: it must name a fix that works. Unit Cost
		is editable on the order line, so the operator types what the invoice says
		and receives again."""
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1,
			"unit_cost": 0}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, {
			"invoiced_qty": 1, "serials": "SN-DO-GUN-FIXED"}, update_modified=False)
		do.reload()
		with self.assertRaises(frappe.ValidationError):
			rb.build_receive_payload(do.name)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name,
			"unit_cost", 430.0, update_modified=False)
		do.reload()
		res = self._receive(do, self._warehouse)
		self.assertTrue(res["ok"])
		self.assertEqual(flt(frappe.db.get_value(
			"Serial No", "SN-DO-GUN-FIXED", "purchase_rate")), 430.0)

	def test_the_same_order_line_repeated_across_items_cannot_over_receive(self):
		"""receive_lines checked each item against the line's outstanding qty as it
		stood BEFORE the call, so three items of qty 1 against 2 outstanding each
		passed and 3 units were booked. The check is cumulative across the call."""
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2,
			"unit_cost": 1.0}], status="Placed")
		line = do.lines[0].name
		frappe.db.set_value("Distributor Order Line", line, {"ack_qty": 2, "invoiced_qty": 2},
			update_modified=False)
		before = frappe.db.count("Purchase Receipt")
		with self.assertRaises(frappe.ValidationError):
			rb.receive_lines(do.name, [{"line_name": line, "qty": 1}] * 3, self._warehouse)
		self.assertEqual(frappe.db.count("Purchase Receipt"), before)
		self.assertEqual(frappe.db.get_value("Distributor Order Line", line, "received_qty"), 0)
		# Two of them fit exactly.
		res = rb.receive_lines(do.name, [{"line_name": line, "qty": 1}] * 2, self._warehouse)
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Distributor Order Line", line, "received_qty"), 2)

	def test_firearm_receive_stamps_distributor_order_on_acquisition(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1,
			"unit_cost": 400.0, "ship_method": "2Day"}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, {
			"invoiced_qty": 1, "serials": "SN-DO-GUN-1"}, update_modified=False)
		do.reload()
		res = self._receive(do, self._warehouse)
		# The acquisition created by this receive carries the DO as provenance —
		# stamped by create_receive BEFORE the (disabled) FastBound push (M2a).
		acqs = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": res["name"], "docstatus": 1},
			fields=["name", "distributor_order", "serial"])
		self.assertEqual(len(acqs), 1)
		self.assertEqual(acqs[0].distributor_order, do.name)
		self.assertEqual(acqs[0].serial, "SN-DO-GUN-1")


class TestCreateOrdersEndpoint(_Base):
	def test_create_orders_splits_guns_and_accessories(self):
		created = dist_api.create_orders(
			"TEST-DC",
			json.dumps([
				{"sku": "DC-GUN", "item_code": "DC-GUN", "qty": 1},
				{"sku": "DC-ACC", "item_code": "DC-ACC", "qty": 2},
			]),
			json.dumps({"mode": "stocking"}))
		# Firearms and accessories never share an order -> two separate Draft DOs.
		self.assertEqual(len(created), 2)
		for row in created:
			do = frappe.get_doc("Distributor Order", row["name"])
			self.assertEqual(do.status, "Draft")
			self.assertEqual(do.docstatus, 0)


	def test_search_cart_line_without_item_code_finds_the_upc_item(self):
		# The restock page's cross-house search sends no item_code; the house SKU
		# must never stand in for one (it is not an Item → link validation threw).
		created = dist_api.create_orders(
			"TEST-DC",
			json.dumps([
				{"sku": "DC-UPC-ACC", "item_code": "", "qty": 1},
				{"sku": "DC-NO-ITEM", "item_code": "", "qty": 1},
			]),
			json.dumps({"mode": "stocking"}))
		self.assertEqual(len(created), 1)
		do = frappe.get_doc("Distributor Order", created[0]["name"])
		self.assertEqual({ln.sku: ln.item_code for ln in do.lines},
			{"DC-UPC-ACC": "000000777001", "DC-NO-ITEM": ""})


class TestListOrdersTracking(_Base):
	def test_open_only_drops_terminal_and_lines_ride_along(self):
		live = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 3,
			"invoiced_qty": 2, "tracking_numbers": "1Z1,1Z2", "invoices": "INV-9"}],
			status="Placed")
		done = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}],
			status="Complete")
		rows = {r.name: r for r in dist_api.list_orders(
			distributor="TEST-DC", open_only=1, with_lines=1, limit=500)}
		self.assertIn(live.name, rows)
		self.assertNotIn(done.name, rows)
		line = rows[live.name]["lines"][0]
		self.assertEqual((line.sku, line.wish_qty, line.invoiced_qty), ("DC-ACC", 3, 2))
		self.assertEqual((line.tracking_numbers, line.invoices), ("1Z1,1Z2", "INV-9"))
		# An explicit status wins over open_only; lines stay off unless asked.
		rows = dist_api.list_orders(status="Complete", distributor="TEST-DC", open_only=1)
		self.assertIn(done.name, [r.name for r in rows])
		self.assertTrue(all("lines" not in r for r in rows))
		# order_type filters.
		self.assertEqual(dist_api.list_orders(distributor="TEST-DC",
			order_type="Dropship Firearm", limit=500), [])
