"""Consignment intake capture — Purchase Receipt on_submit tests, plus the
per-sale commission-freeze tests.

A consignment firearm arrives via a Purchase Receipt with
ffl_acquisition_type="Consignment" (individual seller). On submit,
create_acquisitions_for_firearm_lines must stamp Serial No.is_consignment /
consignment_commission_pct / consignor for every serial on the row — inside
the same per-serial sn_updates block that already stamps purchase_rate /
sell_price / needs_service. A non-consignment receipt must leave those three
fields unstamped.

The second part covers the next step — freezing the EFFECTIVE commission % onto the
invoice line at sale time, so later reporting/GL logic reads one frozen
value instead of re-deriving it:
  - EffectiveCommissionPct: pure priority-rule tests for
	ffl_core.consignment.effective_commission_pct.
  - FreezeConsignmentCommissionRows: direct calls to
	compliance/consignment.freeze_consignment_commission over lightweight
	rows — cheap coverage of each branch.
  - FreezeConsignmentCommissionInvoice: the hook wired onto POS Invoice /
	Sales Invoice `before_validate` (hooks.py — ahead of `validate` so ERPNext's
	own totals calculation doesn't zero the field first; see that class's
	docstring) — a real draft invoice proves the wiring itself, not just the
	bare function.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase


class ConsignmentCapture(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()  # Serial No consignment fields may not exist yet on this transaction
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)

		cls._ensure_supplier_group("Distributor")
		# Unique ffl_number per test module (the column is UNIQUE; _ensure_supplier
		# de-dupes only by name) so this doesn't collide with test_receive_goods /
		# test_4473_nics_gating on a fresh DB.
		cls._ensure_supplier("CONS-ACME-FFL", is_ffl_dealer=1, ffl_number="1-77-777-77-7X-77779",
			ez_check_status="Verified")
		cls._ensure_item("CONS-NEW-FIREARM", item_group="Pistol", has_serial_no=1)
		cls._ensure_customer("_Test Consignor Jane")

	# ---------- fixtures (mirrors PurchaseValidate in test_purchase_compliance.py) ----------

	@staticmethod
	def _ensure_supplier_group(name):
		if not frappe.db.exists("Supplier Group", name):
			frappe.get_doc({
				"doctype": "Supplier Group",
				"supplier_group_name": name,
				"parent_supplier_group": "All Supplier Groups",
				"is_group": 0,
			}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_supplier(name, **fields):
		if frappe.db.exists("Supplier", name):
			for k, v in fields.items():
				frappe.db.set_value("Supplier", name, k, v, update_modified=False)
			return
		frappe.get_doc({
			"doctype": "Supplier",
			"supplier_name": name,
			"supplier_group": "Distributor",
			**fields,
		}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item(code, item_group, has_serial_no):
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": has_serial_no,
			"serial_no_series": f"{code}-.####" if has_serial_no else "",
		}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_customer(name):
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
					or "All Customer Groups",
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")
					or "All Territories",
			}).insert(ignore_permissions=True)
		return name

	def _ensure_warehouse(self):
		row = frappe.get_all("Warehouse",
			filters={"company": self.company, "is_group": 0}, pluck="name", limit=1)
		return row[0] if row else None

	def _make_real_pr(self, supplier, lines, **extra):
		"""Build, insert, submit a real Purchase Receipt with the given
		(item, qty, serials) lines. `extra` carries top-level ffl_* fields
		(acquisition_type/source/seller_customer/...). Returns the submitted PR."""
		warehouse = self._ensure_warehouse()
		company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
		items = []
		for line in lines:
			code, qty, serials = line[0], line[1], line[2]
			row_extra = line[3] if len(line) > 3 else {}
			items.append({
				"item_code": code,
				"qty": qty,
				"warehouse": warehouse,
				"serial_no": "\n".join(serials) if serials else None,
				**row_extra,
			})
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": company,
			"items": items,
			**extra,
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		return pr

	# ---------- tests ----------

	def test_consignment_receipt_stamps_serial(self):
		self._make_real_pr(
			"CONS-ACME-FFL", [("CONS-NEW-FIREARM", 1, ["CONS-SN-1"])],
			ffl_acquisition_type="Consignment",
			ffl_acquisition_source="Individual",
			ffl_seller_customer="_Test Consignor Jane",
			ffl_seller_id_type="Driver License",
			ffl_seller_id_number="DL-CONS-1",
		)
		self.assertEqual(frappe.db.get_value("Serial No", "CONS-SN-1", "is_consignment"), 1)
		self.assertEqual(
			float(frappe.db.get_value("Serial No", "CONS-SN-1", "consignment_commission_pct")), 20.0)
		consignor = frappe.db.get_value("Serial No", "CONS-SN-1", "consignor")
		self.assertTrue(consignor and frappe.db.exists("Supplier", consignor))

	def test_non_consignment_receipt_leaves_serial_unstamped(self):
		"""A plain FFL-dealer purchase (no ffl_acquisition_type) must not stamp
		any consignment field onto its serial."""
		self._make_real_pr("CONS-ACME-FFL", [("CONS-NEW-FIREARM", 1, ["CONS-SN-2"])])
		self.assertFalse(frappe.db.get_value("Serial No", "CONS-SN-2", "is_consignment"))

	def test_consignment_receipt_uses_per_line_commission(self):
		"""A per-line ffl_consignment_commission_pct entered at Receive Goods
		(Part A) wins over the store default (20) when stamping the serial."""
		self._make_real_pr(
			"CONS-ACME-FFL",
			[("CONS-NEW-FIREARM", 1, ["CONS-SN-PCT"], {"ffl_consignment_commission_pct": 12})],
			ffl_acquisition_type="Consignment",
			ffl_acquisition_source="Individual",
			ffl_seller_customer="_Test Consignor Jane",
			ffl_seller_id_type="Driver License",
			ffl_seller_id_number="DL-CONS-PCT",
		)
		self.assertEqual(
			float(frappe.db.get_value("Serial No", "CONS-SN-PCT", "consignment_commission_pct")),
			12.0)


class ValidateConsignmentSerial(FrappeTestCase):
	"""compliance/consignment.validate_consignment_serial — the Serial No
	`validate` guard (hooks.py) that keeps a hand-flagged consignment gun
	payable: is_consignment/consignor are now editable on the form (Part B), so
	when a gun is flagged it must name a consignor, and a blank commission %
	defaults from FFL Settings. Bare-function calls cover the branches; two real
	Serial No saves prove the hooks.py wiring fires."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()  # seeds FFL Settings.consignment_default_commission_pct = 20
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
				"parent_supplier_group": "All Supplier Groups", "is_group": 0}
				).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier", "VCS-CONSIGNOR"):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "VCS-CONSIGNOR",
				"supplier_group": "Distributor"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "VCS-GUN"):
			frappe.get_doc({"doctype": "Item", "item_code": "VCS-GUN", "item_name": "VCS-GUN",
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1}).insert(ignore_permissions=True)

	# ---- bare-function branch coverage (SimpleNamespace doc) ----

	def test_non_consignment_doc_is_noop(self):
		from ffl_core.compliance.consignment import validate_consignment_serial
		doc = SimpleNamespace(is_consignment=0, consignor=None, consignment_commission_pct=None)
		validate_consignment_serial(doc)  # must not raise
		self.assertIsNone(doc.consignment_commission_pct)

	def test_consignment_without_consignor_raises(self):
		from ffl_core.compliance.consignment import validate_consignment_serial
		doc = SimpleNamespace(is_consignment=1, consignor="", consignment_commission_pct=7)
		with self.assertRaises(frappe.ValidationError):
			validate_consignment_serial(doc)

	def test_blank_pct_defaults_to_store_default(self):
		from ffl_core.compliance.consignment import validate_consignment_serial
		doc = SimpleNamespace(is_consignment=1, consignor="VCS-CONSIGNOR",
			consignment_commission_pct=None)
		validate_consignment_serial(doc)
		self.assertEqual(float(doc.consignment_commission_pct), 20.0)

	def test_default_commission_pct_reads_setting_and_falls_back(self):
		"""setup/consignment.default_commission_pct — the one store-default rule
		intake stamping and this form backfill both read."""
		from ffl_core.setup.consignment import DEFAULT_COMMISSION_PCT, default_commission_pct
		field = "consignment_default_commission_pct"
		saved = frappe.db.get_single_value("FFL Settings", field)
		self.addCleanup(frappe.db.set_single_value, "FFL Settings", field, saved)
		frappe.db.set_single_value("FFL Settings", field, 15)
		self.assertEqual(default_commission_pct(), 15.0)
		frappe.db.set_single_value("FFL Settings", field, 0)
		self.assertEqual(default_commission_pct(), DEFAULT_COMMISSION_PCT)

	def test_typed_pct_is_left_alone(self):
		from ffl_core.compliance.consignment import validate_consignment_serial
		doc = SimpleNamespace(is_consignment=1, consignor="VCS-CONSIGNOR",
			consignment_commission_pct=7)
		validate_consignment_serial(doc)
		self.assertEqual(float(doc.consignment_commission_pct), 7.0)

	# ---- real Serial No saves prove the hooks.py wiring ----

	def test_serial_save_requires_consignor(self):
		doc = frappe.get_doc({"doctype": "Serial No", "serial_no": "VCS-SN-NOCONS",
			"item_code": "VCS-GUN", "is_consignment": 1})
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_serial_save_defaults_pct_when_flagged(self):
		doc = frappe.get_doc({"doctype": "Serial No", "serial_no": "VCS-SN-OK",
			"item_code": "VCS-GUN", "is_consignment": 1, "consignor": "VCS-CONSIGNOR"})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertEqual(
			float(frappe.db.get_value("Serial No", "VCS-SN-OK", "consignment_commission_pct")),
			20.0)


class EffectiveCommissionPct(FrappeTestCase):
	"""Pure priority-rule tests for ffl_core.consignment.effective_commission_pct:
	a REAL (non-zero) line % wins over the Serial No's %; a line % of 0 — like
	None/"" — is UNSET and falls back to the serial's %. Explicit 0% is not
	expressible (a 0%-commission consignment sale does not exist,
	and the POS front end / ERPNext's own round_floats_in both surface an
	untouched field as 0.0, indistinguishable from a typed 0)."""

	def test_line_pct_wins_over_serial_pct(self):
		from ffl_core.consignment import effective_commission_pct
		self.assertEqual(
			effective_commission_pct(SimpleNamespace(consignment_commission_pct=15), 20.0), 15.0)

	def test_zero_line_pct_falls_back_to_serial(self):
		# 0 is UNSET, NOT a real 0% override — falls back to the serial's rate.
		from ffl_core.consignment import effective_commission_pct
		self.assertEqual(
			effective_commission_pct(SimpleNamespace(consignment_commission_pct=0), 20.0), 20.0)

	def test_unset_spellings_all_fall_back_to_serial(self):
		# Every "unset" spelling — 0, 0.0, "", None — resolves to the serial rate.
		from ffl_core.consignment import effective_commission_pct
		for unset in (0, 0.0, "", None):
			self.assertEqual(
				effective_commission_pct(SimpleNamespace(consignment_commission_pct=unset), 20.0),
				20.0, msg=f"unset spelling {unset!r} should fall back")

	def test_blank_line_pct_falls_back_to_serial_pct(self):
		from ffl_core.consignment import effective_commission_pct
		self.assertEqual(
			effective_commission_pct(SimpleNamespace(consignment_commission_pct=None), 20.0), 20.0)


class FreezeConsignmentCommissionRows(FrappeTestCase):
	"""compliance/consignment.freeze_consignment_commission called directly
	over a lightweight SimpleNamespace "doc" (NOT frappe._dict — dict.items()
	is a builtin method, so a dict-like object can never carry a usable
	.items child-table attribute) wrapping rows that ARE frappe._dict (the
	shared serial parser ffl_core.serials.serials_for_line calls row.get()).
	Each Serial No is inserted directly (no stock movement needed — the hook
	only reads is_consignment / consignment_commission_pct), which is cheap
	enough to cover every branch here; the real-Document / hooks.py-wiring
	path is covered separately by FreezeConsignmentCommissionInvoice below."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()  # Serial No / line consignment fields may not exist yet
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls._ensure_item("CONS-FRZ-GUN", "Pistol", has_serial_no=1)
		cls._ensure_item("CONS-FRZ-ACC", "Accessory", has_serial_no=0)

	@staticmethod
	def _ensure_item(code: str, item_group: str, has_serial_no: int) -> None:
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": item_group, "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": has_serial_no,
		}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_serial(serial: str, *, is_consignment: int = 0, pct: float | None = None,
			item_code: str = "CONS-FRZ-GUN") -> str:
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": serial, "item_code": item_code,
			}).insert(ignore_permissions=True)
		updates = {"is_consignment": is_consignment}
		if pct is not None:
			updates["consignment_commission_pct"] = pct
		frappe.db.set_value("Serial No", serial, updates, update_modified=False)
		return serial

	@staticmethod
	def _row(**over) -> "frappe._dict":
		base = {"idx": 1, "item_code": "CONS-FRZ-GUN", "serial_no": "",
			"consignment_commission_pct": None}
		base.update(over)
		return frappe._dict(base)

	def test_non_consignment_serial_leaves_row_untouched(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=0)
		row = self._row(serial_no=serial)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertIsNone(row.consignment_commission_pct)

	def test_blank_line_freezes_from_consignment_serial(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 18.0)

	def test_zero_line_pct_falls_back_to_serial(self):
		# 0 on the line is UNSET — the freeze resolves it to the
		# serial's rate (18), NOT a real 0% override. Explicit 0% is not expressible.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial, consignment_commission_pct=0)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 18.0)

	def test_resolved_zero_pct_raises(self):
		# Both line AND serial resolve to 0 (a mis-stamped serial that bypassed
		# validate_consignment_serial's default backfill via db.set_value) → the
		# freeze must THROW, not silently lock a 0 that would book the whole sale
		# price as COGS (profit 0) and split GL/report. Surfaces the
		# misconfiguration at save time instead of at the next audit.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=0)
		row = self._row(serial_no=serial, consignment_commission_pct=0)
		with self.assertRaises(frappe.ValidationError):
			freeze_consignment_commission(SimpleNamespace(items=[row]))

	def test_explicit_nonzero_line_pct_is_preserved(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial, consignment_commission_pct=5)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 5.0)

	def test_boundary_100_pct_is_preserved(self):
		# Edge case: 100% is a legitimate value (store keeps all, COGS 0)
		# — must NOT be rejected as "out of range".
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial, consignment_commission_pct=100)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 100.0)

	def test_line_pct_over_100_raises(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial, consignment_commission_pct=200)
		with self.assertRaises(frappe.ValidationError):
			freeze_consignment_commission(SimpleNamespace(items=[row]))

	def test_line_pct_below_zero_raises(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		serial = self._ensure_serial(self._testMethodName, is_consignment=1, pct=18)
		row = self._row(serial_no=serial, consignment_commission_pct=-5)
		with self.assertRaises(frappe.ValidationError):
			freeze_consignment_commission(SimpleNamespace(items=[row]))

	def test_row_without_serial_is_skipped(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		row = self._row(item_code="CONS-FRZ-ACC", serial_no="")
		freeze_consignment_commission(SimpleNamespace(items=[row]))  # must not raise
		self.assertIsNone(row.consignment_commission_pct)

	def test_freezes_from_a_consignment_gun_in_second_position(self):
		# A qty>1 line carries every gun in ONE row. With only the first serial
		# read, a line whose consigned gun sat behind a store-owned one froze
		# nothing — and an unfrozen line books no consignor payable downstream.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		plain = self._ensure_serial(self._testMethodName + "-P", is_consignment=0)
		cons = self._ensure_serial(self._testMethodName + "-C", is_consignment=1, pct=18)
		row = self._row(serial_no=f"{plain}\n{cons}", qty=2)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 18.0)

	def test_two_consigned_guns_at_the_same_rate_freeze_that_rate(self):
		# Same rate on both guns — one line CAN represent them, so it freezes
		# exactly as a single-gun line does.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=18)
		row = self._row(serial_no=f"{a}\n{b}", qty=2)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 18.0)

	def test_two_consigned_guns_at_different_rates_raise(self):
		# The commission is a LINE-level field, and effective_commission_pct
		# gives the line's value priority over each serial's — so freezing gun
		# A's 18% onto the line MASKS gun B's own 25% and pays B's consignor at
		# A's rate. One line cannot represent two rates: fail closed and make the
		# cashier split the line, rather than silently paying someone wrong.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=25)
		row = self._row(serial_no=f"{a}\n{b}", qty=2)
		with self.assertRaises(frappe.ValidationError) as ctx:
			freeze_consignment_commission(SimpleNamespace(items=[row]))
		message = str(ctx.exception)
		self.assertIn(a, message)   # the message must name the guns in conflict
		self.assertIn(b, message)

	def test_a_gun_added_to_an_already_frozen_line_is_still_rate_checked(self):
		# The re-save hole: before_validate runs on EVERY save, so by the second
		# save the line already carries a frozen rate — and comparing RESOLVED
		# rates would then see 18 for both guns and wave the mismatch through.
		# The check therefore compares the guns' OWN serial rates, which the
		# frozen line value cannot mask.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=25)
		row = self._row(serial_no=f"{a}\n{b}", qty=2, consignment_commission_pct=18)
		with self.assertRaises(frappe.ValidationError):
			freeze_consignment_commission(SimpleNamespace(items=[row]))

	def test_a_gun_with_no_rate_of_its_own_is_diagnosed_as_mis_stamped(self):
		# A gun whose own rate was never stamped (blank/0) is NOT a rate conflict:
		# splitting the line cannot fix it. It must get the accurate "stamp the
		# Serial No" message — the same one it would get standing alone — rather
		# than being sent off to split a line and hit this throw one step later.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		good = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		unstamped = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=0)
		row = self._row(serial_no=f"{good}\n{unstamped}", qty=2)
		with self.assertRaises(frappe.ValidationError) as ctx:
			freeze_consignment_commission(SimpleNamespace(items=[row]))
		message = str(ctx.exception)
		self.assertIn("Set a commission rate", message)
		self.assertIn(unstamped, message)          # names the gun that needs stamping
		self.assertNotIn("its own line", message)  # NOT the split-the-line advice

	def test_an_override_supplies_the_rate_for_an_unstamped_gun(self):
		# The boundary of the rule above: a missing rate is "no opinion", not a
		# conflicting rate, so an operator override that supplies it is accepted —
		# only guns that BOTH carry real rates can conflict with each other.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		good = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		unstamped = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=0)
		row = self._row(serial_no=f"{good}\n{unstamped}", qty=2,
			consignment_commission_pct=5)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 5.0)

	def test_an_operator_override_does_not_suppress_a_rate_conflict(self):
		# An override is a deliberate "this rate for this whole line" statement,
		# but it cannot make two DIFFERENT consignors' contracts agree: one typed
		# number can't honour 18% to one and 25% to the other. The conflict wins
		# over the override, so the operator is still made to split the line.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=25)
		row = self._row(serial_no=f"{a}\n{b}", qty=2, consignment_commission_pct=5)
		with self.assertRaises(frappe.ValidationError) as ctx:
			freeze_consignment_commission(SimpleNamespace(items=[row]))
		message = str(ctx.exception)
		self.assertIn(a, message)
		self.assertIn(b, message)

	def test_an_operator_override_still_applies_to_the_whole_line(self):
		# An explicitly typed line rate is a deliberate "this rate for this whole
		# line" statement, so it keeps winning over the serials — the conflict
		# check is about the GUNS disagreeing with each other, not with the
		# override.
		from ffl_core.compliance.consignment import freeze_consignment_commission
		a = self._ensure_serial(self._testMethodName + "-A", is_consignment=1, pct=18)
		b = self._ensure_serial(self._testMethodName + "-B", is_consignment=1, pct=18)
		row = self._row(serial_no=f"{a}\n{b}", qty=2, consignment_commission_pct=5)
		freeze_consignment_commission(SimpleNamespace(items=[row]))
		self.assertEqual(float(row.consignment_commission_pct), 5.0)

	def test_mixed_rows_only_consignment_row_updated(self):
		from ffl_core.compliance.consignment import freeze_consignment_commission
		cons_serial = self._ensure_serial(self._testMethodName + "-C", is_consignment=1, pct=18)
		plain_serial = self._ensure_serial(self._testMethodName + "-P", is_consignment=0)
		cons_row = self._row(idx=1, serial_no=cons_serial)
		plain_row = self._row(idx=2, serial_no=plain_serial)
		acc_row = self._row(idx=3, item_code="CONS-FRZ-ACC", serial_no="")
		freeze_consignment_commission(SimpleNamespace(items=[cons_row, plain_row, acc_row]))
		self.assertEqual(float(cons_row.consignment_commission_pct), 18.0)
		self.assertIsNone(plain_row.consignment_commission_pct)
		self.assertIsNone(acc_row.consignment_commission_pct)


class FreezeConsignmentCommissionInvoice(FrappeTestCase):
	"""End-to-end: the freeze hook must fire through the real POS Invoice /
	Sales Invoice `before_validate` doc_event (hooks.py), not just as a bare
	function call. Wired onto before_validate rather than validate: ERPNext's
	own core validate() (calculate_taxes_and_totals -> round_floats_in) casts
	every Currency/Float/Percent field on each item row — including this
	custom one — through flt(), turning a truly-blank None into 0.0 before
	any `validate` doc_event would get a chance to see it (confirmed with a
	throwaway probe against a real Sales Invoice: the field reads 0.0, not
	None, by the time `validate` doc_events run). before_validate fires
	earlier in Document.run_before_save_methods(), ahead of that coercion —
	the same reason Item.before_validate runs sync_upc_barcode (hooks.py).
	Seeded the way test_trade_in_credit.py seeds a POS/Sales Invoice carrying
	a special line — here a firearm row referencing a stocked consignment
	Serial No."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site
		dev_site.run()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.pos_profile = "FFL Test"
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 0}, "name")
		if not frappe.db.exists("Item", "CONS-FRZ-INV-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "CONS-FRZ-INV-GUN",
				"item_name": "CONS-FRZ-INV-GUN", "item_group": "Pistol",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", "_Test Consignment Buyer"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Consignment Buyer",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		cls._ensure_open_pos_entry()

	@classmethod
	def _ensure_open_pos_entry(cls) -> None:
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company, cls.pos_profile)

	def _stock_serial(self, serial: str) -> str:
		"""Put one CONS-FRZ-INV-GUN with this serial into the test warehouse."""
		from ffl_core.firearm import serialized_receipt_row
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			return serial
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code="CONS-FRZ-INV-GUN", qty=1, warehouse=self.warehouse,
				basic_rate=500, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return serial

	def _consignment_serial(self, serial: str, pct: float = 18.0) -> str:
		self._stock_serial(serial)
		frappe.db.set_value("Serial No", serial,
			{"is_consignment": 1, "consignment_commission_pct": pct}, update_modified=False)
		return serial

	def test_sales_invoice_validate_freezes_blank_line_from_serial(self):
		serial = self._consignment_serial("CONS-FRZ-SI-1")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test Consignment Buyer"
		si.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()  # draft — before_validate fires as part of the same insert() call
		self.assertEqual(si.docstatus, 0)
		self.assertEqual(float(si.items[0].consignment_commission_pct), 18.0)

	def test_pos_invoice_validate_freezes_blank_line_from_serial(self):
		serial = self._consignment_serial("CONS-FRZ-PI-1")
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Consignment Buyer"
		inv.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.insert()  # draft — before_validate fires as part of the same insert() call
		self.assertEqual(inv.docstatus, 0)
		self.assertEqual(float(inv.items[0].consignment_commission_pct), 18.0)

	def test_sales_invoice_zero_line_falls_back_to_serial(self):
		"""An on-the-wire 0% line (whether operator-typed or an untouched field
		ERPNext coerced to 0.0 in round_floats_in) is UNSET, not a real 0%
		override: the freeze resolves it to the consignment Serial
		No's own rate (18), never 0. Proves the unified 0-semantics through the
		real before_validate wiring, not just the bare function."""
		serial = self._consignment_serial("CONS-FRZ-SI-2")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test Consignment Buyer"
		si.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": 0,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		self.assertEqual(float(si.items[0].consignment_commission_pct), 18.0)

	def test_pos_invoice_zero_line_falls_back_to_serial(self):
		"""POS Invoice mirror of the Sales Invoice case above. POS Invoice is
		this app's PRIMARY doctype (the register), so the 0→serial fall-back
		guarantee needs its own direct coverage here rather than resting on
		inference from the Sales Invoice case."""
		serial = self._consignment_serial("CONS-FRZ-PI-2")
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Consignment Buyer"
		inv.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": 0,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.insert()
		self.assertEqual(float(inv.items[0].consignment_commission_pct), 18.0)

	def test_sales_invoice_explicit_nonzero_line_not_overwritten_by_serial(self):
		serial = self._consignment_serial("CONS-FRZ-SI-3")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test Consignment Buyer"
		si.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": 7,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		self.assertEqual(float(si.items[0].consignment_commission_pct), 7.0)

	def test_pos_invoice_explicit_nonzero_line_not_overwritten_by_serial(self):
		"""POS Invoice mirror of the Sales Invoice case above: an explicit
		non-zero line % (12.5) must survive core validate() unchanged, not get
		overwritten by the consignment Serial No's own % (18.0, per
		_consignment_serial's default)."""
		serial = self._consignment_serial("CONS-FRZ-PI-3")
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Consignment Buyer"
		inv.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": 12.5,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.insert()
		self.assertEqual(float(inv.items[0].consignment_commission_pct), 12.5)

	def test_sales_invoice_out_of_range_line_pct_raises_on_insert(self):
		"""Edge case ("Commission % validated to 0-100") enforced through
		the real before_validate wiring, not just the bare function: an
		operator-typed 200% must reject the whole insert()."""
		serial = self._consignment_serial("CONS-FRZ-SI-4")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test Consignment Buyer"
		si.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": 200,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			si.insert()

	def test_pos_invoice_out_of_range_line_pct_raises_on_insert(self):
		"""POS Invoice mirror — this app's PRIMARY doctype (the register)."""
		serial = self._consignment_serial("CONS-FRZ-PI-4")
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.pos_profile
		inv.customer = "_Test Consignment Buyer"
		inv.append("items", {
			"item_code": "CONS-FRZ-INV-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse, "serial_no": serial,
			"use_serial_batch_fields": 1, "consignment_commission_pct": -10,
		})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			inv.insert()


if __name__ == "__main__":
	unittest.main()
