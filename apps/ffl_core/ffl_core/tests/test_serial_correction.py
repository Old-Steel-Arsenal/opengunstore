"""Serial-number correction — ffl_core.api.serial_correction.correct_serial.

Productizes the manual bench-console fix (rename an in-stock firearm's Serial No,
repoint its submitted A&D, and edit the FastBound bound-book item) into a
manager-gated, atomic whitelisted action.

Scope: IN-STOCK, unsold firearms only — Serial No.status == "Active"
and no committed (docstatus=1) FFL Disposition on the serial.

Test discipline (docs/testing.md): NO real commit inside the test transaction.
Stock setup runs under ``patch("frappe.db.commit")`` so the class rollback cleans
everything up and ``make test`` stays same-site repeatable. correct_serial itself
is exercised WITHOUT that patch — an errant commit inside it would break the
atomic rollback, and the FastBound-failure test is what would catch it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_core.api.serial_correction import correct_serial
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.fastbound.client import FastBoundError

_FB_FROM_SETTINGS = "ffl_integrations.fastbound.client.FastBoundClient.from_settings"


class SerialCorrection(SettingsSandbox, FrappeTestCase):
	ITEM = "SC-TEST-ITEM"
	CUSTOMER = "SC-TEST-CUSTOMER"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls._ensure("UOM", "Nos", {"uom_name": "Nos", "must_be_whole_number": 1})
		cls._ensure("Item Group", "Used Firearm", {
			"item_group_name": "Used Firearm", "parent_item_group": "All Item Groups",
			"is_group": 0,
		})
		cls._ensure("Customer", cls.CUSTOMER, {
			"customer_name": cls.CUSTOMER, "customer_type": "Individual",
			"customer_group": "Individual", "territory": "All Territories",
		})
		cls._ensure("Item", cls.ITEM, {
			"item_code": cls.ITEM, "item_name": cls.ITEM, "item_group": "Used Firearm",
			"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
			"is_firearm": 1, "firearm_type": "Pistol",
		})
		cls.warehouse = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0, "disabled": 0},
			pluck="name", limit=1,
		)[0]

	def setUp(self):
		super().setUp()
		# Pin the ambient switch: correct_serial's behaviour forks on it, and these
		# tests are about the FastBound-synced path. A site that has genuinely
		# switched FastBound off must still be able to run its own suite green.
		self.set_single("FastBound Settings", "enabled", 1)

	@staticmethod
	def _ensure(doctype: str, name: str, doc: dict) -> None:
		if not frappe.db.exists(doctype, name):
			frappe.get_doc({"doctype": doctype, **doc}).insert(ignore_permissions=True)

	# ------------------------------------------------------------------ helpers

	def _make_gun(self, tag: str, *, fb_item_id: str = "auto", woo_product_id: str = ""):
		"""Create an in-stock firearm: real Material Receipt → Serial No (Active) +
		Serial and Batch Entry, plus a submitted FFL Acquisition on the serial. Runs
		under patched commit so nothing survives the class rollback. Returns
		(serial, acquisition_name, fastbound_item_id)."""
		serial = f"SC-{tag}"
		fb_item_id = f"fb-{tag}" if fb_item_id == "auto" else fb_item_id
		with patch("frappe.db.commit"):
			se = frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{
					"item_code": self.ITEM, "qty": 1, "t_warehouse": self.warehouse,
					"basic_rate": 100, "use_serial_batch_fields": 1, "serial_no": serial,
				}],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
			acq = frappe.get_doc({
				"doctype": "FFL Acquisition", "acquisition_date": now_datetime(),
				"source_type": "Trade-in", "source_name": "Jane Seller",
				"source_id_type": "Driver License", "source_id_number": f"DL-{tag}",
				"company": self.company, "item": self.ITEM, "serial": serial,
				"cost": 100, "notes": "",
			})
			acq.insert(ignore_permissions=True)
			acq.submit()
			frappe.db.set_value("Serial No", serial, {
				"fastbound_item_id": fb_item_id, "woo_product_id": woo_product_id,
			}, update_modified=False)
		return serial, acq.name, fb_item_id

	def _submit_retail_disposition(self, serial: str, tag: str) -> None:
		with patch("frappe.db.commit"):
			disp = frappe.get_doc({
				"doctype": "FFL Disposition", "disposition_date": now_datetime(),
				"disposition_type": "Retail Sale", "customer": self.CUSTOMER,
				"company": self.company, "item": self.ITEM, "serial": serial,
				"form_4473_status": "Completed", "nics_status": "Proceed",
				"fastbound_id": f"FB-DISP-{tag}",
			})
			disp.insert(ignore_permissions=True)
			disp.submit()

	# ------------------------------------------------------------------ happy path

	def test_happy_path_renames_repoints_and_syncs_fastbound(self):
		tag = self._testMethodName
		serial, acq, fb = self._make_gun(tag, woo_product_id="9999")
		new = f"SC-NEW-{tag}"
		client = MagicMock()
		client.update_item_serial.return_value = {"id": fb}
		with patch(_FB_FROM_SETTINGS, return_value=client), patch("frappe.enqueue") as enq:
			result = correct_serial(serial, new, "typo in intake")

		self.assertTrue(result["ok"])
		# Serial No renamed in place, still Active, FastBound link preserved.
		self.assertFalse(frappe.db.exists("Serial No", serial))
		self.assertTrue(frappe.db.exists("Serial No", new))
		self.assertEqual(frappe.db.get_value("Serial No", new, "status"), "Active")
		self.assertEqual(frappe.db.get_value("Serial No", new, "fastbound_item_id"), fb)
		# Submitted A&D carries the new serial (Data field — set_value, no cascade).
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "serial"), new)
		# Stock cascaded via rename: no old serial left in the bundle, new present.
		self.assertEqual(frappe.db.count("Serial and Batch Entry", {"serial_no": serial}), 0)
		self.assertGreaterEqual(frappe.db.count("Serial and Batch Entry", {"serial_no": new}), 1)
		# FastBound bound-book item edited in place with an audit note.
		client.update_item_serial.assert_called_once()
		args, kwargs = client.update_item_serial.call_args
		self.assertEqual(args[0], fb)
		self.assertEqual(args[1], new)
		note = kwargs.get("note", "")
		self.assertIn(serial, note)
		self.assertIn(new, note)
		self.assertIn("typo in intake", note)
		# Audit trail: A&D note line + Serial No timeline comment.
		self.assertIn("typo in intake", frappe.db.get_value("FFL Acquisition", acq, "notes") or "")
		self.assertTrue(frappe.db.exists("Comment", {
			"reference_doctype": "Serial No", "reference_name": new,
			"content": ["like", "%typo in intake%"],
		}))
		# Woo re-push enqueued after commit (title/SKU embed the old serial).
		woo = [c for c in enq.call_args_list if c.args and "push_serial_task" in str(c.args[0])]
		self.assertTrue(woo, "expected a Woo push_serial_task enqueue")
		self.assertEqual(woo[0].kwargs.get("serial_no"), new)
		# MUST be a FULL push: the task's machine default (pos_fields_only=True)
		# filters out exactly the title/SKU/_osa_serial this enqueue exists to
		# refresh — lean here strands the store on the OLD serial (r1 blocker).
		self.assertIs(woo[0].kwargs.get("pos_fields_only"), False)

	def test_no_woo_enqueue_when_not_listed(self):
		tag = self._testMethodName
		serial, acq, fb = self._make_gun(tag)  # woo_product_id blank
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client), patch("frappe.enqueue") as enq:
			correct_serial(serial, f"SC-NEW-{tag}", "fix")
		woo = [c for c in enq.call_args_list if c.args and "push_serial_task" in str(c.args[0])]
		self.assertEqual(woo, [], "unlisted gun must not enqueue a Woo push")

	def test_runs_with_the_dealer_store_fields_gone(self):
		"""1.11.0: the retired dealer store's fields are off Serial No. The correction
		reads none of them and re-pushes on the retail store only (no site=…)."""
		from ffl_core.tests.test_remove_dealer_store import refuse_removed_field_reads

		tag = self._testMethodName
		serial, _acq, fb = self._make_gun(tag, woo_product_id="7777")
		new = f"SC-NEW-{tag}"
		client = MagicMock()
		client.update_item_serial.return_value = {"id": fb}
		with refuse_removed_field_reads(), patch(_FB_FROM_SETTINGS, return_value=client), \
				patch("frappe.enqueue") as enq:
			self.assertTrue(correct_serial(serial, new, "typo")["ok"])
		woo = [c for c in enq.call_args_list if c.args and "push_serial_task" in str(c.args[0])]
		self.assertEqual(len(woo), 1)
		self.assertEqual(woo[0].kwargs.get("serial_no"), new)
		self.assertIn(woo[0].kwargs.get("site", "retail"), ("retail",))

	# ------------------------------------------------------------------ rejections

	def test_target_serial_conflict_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		other, _a2, _f2 = self._make_gun(tag + "B")
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaisesRegex(frappe.ValidationError, "already exists"):
				correct_serial(serial, other, "typo")
		self.assertTrue(frappe.db.exists("Serial No", serial))
		self.assertTrue(frappe.db.exists("Serial No", other))
		client.update_item_serial.assert_not_called()

	def test_same_serial_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaises(frappe.ValidationError):
				correct_serial(serial, serial, "typo")
		client.update_item_serial.assert_not_called()

	def test_committed_disposition_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		self._submit_retail_disposition(serial, tag)
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaisesRegex(frappe.ValidationError, "[Dd]isposition"):
				correct_serial(serial, f"SC-NEW-{tag}", "typo")
		# No partial write; the gun keeps its serial.
		self.assertTrue(frappe.db.exists("Serial No", serial))
		self.assertFalse(frappe.db.exists("Serial No", f"SC-NEW-{tag}"))
		client.update_item_serial.assert_not_called()

	def test_gun_held_on_a_parked_pos_sale_rejected(self):
		"""The order line holds the gun by its serial TEXT, which a rename does not
		follow: the hold would vanish and the order would point at nothing."""
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		# A parked (Draft) POS sale carrying the gun, written straight to the tables:
		# a real one needs a shift, a profile and payments, none of which this is about.
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"SC-POS-{frappe.generate_hash(length=8)}", "company": self.company,
			"docstatus": 0, "posting_date": now_datetime().date().isoformat()})
		inv.db_insert()
		line = frappe.new_doc("POS Invoice Item")
		line.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice",
			"parentfield": "items", "idx": 1, "docstatus": 0, "item_code": self.ITEM, "qty": 1,
			"serial_no": serial, "warehouse": self.warehouse})
		line.db_insert()
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaisesRegex(frappe.ValidationError, "is held on"):
				correct_serial(serial, f"SC-NEW-{tag}", "typo")
		self.assertTrue(frappe.db.exists("Serial No", serial))
		self.assertFalse(frappe.db.exists("Serial No", f"SC-NEW-{tag}"))
		client.update_item_serial.assert_not_called()

	def test_empty_reason_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		client = MagicMock()
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaises(frappe.ValidationError):
				correct_serial(serial, f"SC-NEW-{tag}", "   ")
		self.assertTrue(frappe.db.exists("Serial No", serial))
		client.update_item_serial.assert_not_called()

	def test_non_manager_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag)
		email = "sc_nonmgr@example.com"
		if not frappe.db.exists("User", email):
			u = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "SC NonMgr",
				"send_welcome_email": 0, "roles": [],
			})
			u.flags.ignore_permissions = True
			u.insert(ignore_permissions=True)
		client = MagicMock()
		frappe.set_user(email)
		try:
			with patch(_FB_FROM_SETTINGS, return_value=client):
				with self.assertRaises(frappe.PermissionError):
					correct_serial(serial, f"SC-NEW-{tag}", "typo")
		finally:
			frappe.set_user("Administrator")
		# Permission gate fires first — nothing touched.
		self.assertTrue(frappe.db.exists("Serial No", serial))
		client.update_item_serial.assert_not_called()

	def test_switched_off_fastbound_corrects_locally_instead_of_refusing(self):
		"""A store with the integration switched off has no bound book to desync
		from, so the local rename IS the whole correction. Refusing here would leave
		a mistyped serial permanently uncorrectable — and the LOCAL A&D would be the
		record left carrying the wrong number."""
		tag = self._testMethodName
		serial, acq, _fb = self._make_gun(tag, fb_item_id="")
		new = f"SC-NEW-{tag}"
		self.set_single("FastBound Settings", "enabled", 0)
		with patch(_FB_FROM_SETTINGS) as from_settings, patch("frappe.enqueue"):
			result = correct_serial(serial, new, "typo, no FastBound here")
			from_settings.assert_not_called()

		self.assertTrue(result["ok"])
		self.assertEqual(result["fastbound_item_id"], "")
		self.assertFalse(frappe.db.exists("Serial No", serial))
		self.assertTrue(frappe.db.exists("Serial No", new))
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "serial"), new)
		self.assertIn("typo, no FastBound here",
			frappe.db.get_value("FFL Acquisition", acq, "notes") or "")

	def test_switched_off_rename_of_a_pushed_gun_records_the_divergence(self):
		"""The "nothing to desync" reasoning only holds for a gun that was never
		pushed. This one HAS a bound-book item and the integration is merely off, so
		the two records diverge from here — and the only thing keeping that from
		being invisible is saying so on the record itself."""
		tag = self._testMethodName
		serial, _acq, fb = self._make_gun(tag)          # fb_item_id populated
		new = f"SC-NEW-{tag}"
		self.set_single("FastBound Settings", "enabled", 0)
		with patch(_FB_FROM_SETTINGS) as from_settings, patch("frappe.enqueue"):
			correct_serial(serial, new, "typo while FastBound was off")
			from_settings.assert_not_called()

		comments = frappe.get_all("Comment", filters={
			"reference_doctype": "Serial No", "reference_name": new,
		}, pluck="content")
		joined = " ".join(comments)
		self.assertIn(fb, joined, "the bound-book item id must be named")
		self.assertIn(serial, joined, "the OLD serial must be named")
		self.assertIn("switched off", joined.lower())

	def test_fastbound_item_not_locatable_rejected(self):
		tag = self._testMethodName
		serial, _acq, _fb = self._make_gun(tag, fb_item_id="")  # never synced
		client = MagicMock()
		client.get_item_id_by_serial.return_value = None
		with patch(_FB_FROM_SETTINGS, return_value=client):
			with self.assertRaises(frappe.ValidationError):
				correct_serial(serial, f"SC-NEW-{tag}", "typo")
		self.assertTrue(frappe.db.exists("Serial No", serial))
		client.get_item_id_by_serial.assert_called_once()
		client.update_item_serial.assert_not_called()

	# ------------------------------------------------------------------ atomicity

	def test_fastbound_failure_rolls_back_all_erp_changes(self):
		tag = self._testMethodName
		serial, acq, _fb = self._make_gun(tag, woo_product_id="8888")
		new = f"SC-NEW-{tag}"
		client = MagicMock()
		client.update_item_serial.side_effect = FastBoundError("FastBound 500")
		with patch(_FB_FROM_SETTINGS, return_value=client), patch("frappe.enqueue") as enq:
			with self.assertRaises(FastBoundError):
				correct_serial(serial, new, "typo fix")

		# FastBound was attempted, then EVERY ERP write rolled back.
		client.update_item_serial.assert_called_once()
		self.assertTrue(frappe.db.exists("Serial No", serial))
		self.assertFalse(frappe.db.exists("Serial No", new))
		self.assertEqual(frappe.db.get_value("FFL Acquisition", acq, "serial"), serial)
		self.assertEqual(frappe.db.count("Serial and Batch Entry", {"serial_no": new}), 0)
		self.assertGreaterEqual(frappe.db.count("Serial and Batch Entry", {"serial_no": serial}), 1)
		# No audit residue and no Woo push for a rolled-back correction.
		self.assertNotIn("typo fix", frappe.db.get_value("FFL Acquisition", acq, "notes") or "")
		self.assertFalse(frappe.db.exists("Comment", {
			"reference_doctype": "Serial No", "content": ["like", "%typo fix%"],
		}))
		woo = [c for c in enq.call_args_list if c.args and "push_serial_task" in str(c.args[0])]
		self.assertEqual(woo, [], "a rolled-back correction must not enqueue a Woo push")
