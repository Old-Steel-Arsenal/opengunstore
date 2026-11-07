"""Purchase Receipt firearm-compliance hook tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.purchase import require_ffl_supplier_for_firearm_lines


class PurchaseValidate(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

		# Two suppliers: one FFL, one not.
		cls._ensure_supplier_group("Distributor")
		cls._ensure_supplier("ACME-FFL", is_ffl_dealer=1, ffl_number="1-99-999-99-9X-99999",
			ez_check_status="Verified")
		cls._ensure_supplier("PaperGoods Co", is_ffl_dealer=0)
		cls._ensure_supplier("FAILED-FFL", is_ffl_dealer=1, ffl_number="1-88-888-88-8X-88888",
			ez_check_status="Failed")

		# Two items: firearm with serial-no=1 (new), firearm with serial-no=0 (used).
		cls._ensure_item("PR-NEW-FIREARM", item_group="Pistol", has_serial_no=1)
		cls._ensure_item("PR-USED-FIREARM", item_group="Rifle", has_serial_no=0)
		cls._ensure_item("PR-AMMO", item_group="Ammunition", has_serial_no=0)
		# Antique firearm — GCA-exempt: received like a gun but no FFL Acquisition.
		cls._ensure_item("PR-ANTIQUE", item_group="Pistol", has_serial_no=1)
		frappe.db.set_value("Item", "PR-ANTIQUE", "is_antique", 1, update_modified=False)

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
			# Refresh state in case prior test mutated.
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
	def _doc(supplier, *items):
		return SimpleNamespace(
			supplier=supplier,
			items=[
				SimpleNamespace(idx=i + 1, item_code=code, qty=qty)
				for i, (code, qty) in enumerate(items)
			],
		)

	def test_firearm_line_with_non_ffl_supplier_throws(self):
		doc = self._doc("PaperGoods Co", ("PR-NEW-FIREARM", 5))
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_ffl_supplier_for_firearm_lines(doc)
		self.assertIn("FFL", str(ctx.exception))

	def test_firearm_line_with_failed_ez_check_supplier_throws(self):
		doc = self._doc("FAILED-FFL", ("PR-NEW-FIREARM", 5))
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_ffl_supplier_for_firearm_lines(doc)
		self.assertIn("EZ Check", str(ctx.exception))

	def test_firearm_line_with_verified_ffl_supplier_passes(self):
		doc = self._doc("ACME-FFL", ("PR-NEW-FIREARM", 5))
		require_ffl_supplier_for_firearm_lines(doc)  # must not raise

	def test_non_firearm_lines_with_non_ffl_supplier_pass(self):
		doc = self._doc("PaperGoods Co", ("PR-AMMO", 50))
		require_ffl_supplier_for_firearm_lines(doc)  # must not raise

	# (The has_serial_no=0 / Used Firearm one-of-one tests were removed
	# alongside the Used Firearm path unification — every firearm is now
	# has_serial_no=1 and tracked via Serial No records on the line.)

	# ---------- on_submit tests ----------

	def _ensure_warehouse(self):
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		row = frappe.get_all("Warehouse",
			filters={"company": company, "is_group": 0}, pluck="name", limit=1)
		return row[0] if row else None

	def _make_real_pr(self, supplier, lines):
		"""Build, insert, submit a real Purchase Receipt with the given
		(item, qty, serials[, extra_fields]) lines.
		Returns the submitted PR doc."""
		warehouse = self._ensure_warehouse()
		# Derive company from the warehouse so there's no company/warehouse mismatch.
		company = frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None
		items = []
		for line in lines:
			code, qty, serials = line[0], line[1], line[2]
			extra = line[3] if len(line) > 3 else {}
			items.append({
				"item_code": code,
				"qty": qty,
				"warehouse": warehouse,
				"serial_no": "\n".join(serials) if serials else None,
				**extra,
			})
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": company,
			"items": items,
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		return pr

	def test_pr_submit_creates_one_acquisition_per_firearm_serial(self):
		serials = ["TEST-A-001", "TEST-A-002"]
		pr = self._make_real_pr("ACME-FFL", [("PR-NEW-FIREARM", 2, serials)])
		linked = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": pr.name, "docstatus": 1},
			pluck="serial")
		self.assertEqual(sorted(linked), sorted(serials))

	def test_pr_submit_enqueues_fastbound_push_once_per_serial(self):
		"""Regression: the FFL Acquisition on_submit hook is the single FastBound
		push trigger. create_acquisitions_for_firearm_lines must NOT also enqueue,
		or every PR-received firearm double-pushes (duplicate FB acquisition /
		spurious 'externalId already used' on the losing job)."""
		with patch("frappe.enqueue") as enq:
			self._make_real_pr("ACME-FFL", [("PR-NEW-FIREARM", 1, ["TEST-PUSH-ONCE-1"])])
		pushes = [
			c for c in enq.call_args_list
			if "push_acquisition_to_fastbound" in str(c.args[0] if c.args else c.kwargs.get("method", ""))
		]
		self.assertEqual(len(pushes), 1, f"expected 1 push enqueue per serial, got {len(pushes)}")

	def test_pr_submit_with_mixed_lines_only_creates_for_firearm(self):
		serials = ["TEST-MIX-001"]
		pr = self._make_real_pr("ACME-FFL", [
			("PR-NEW-FIREARM", 1, serials),
			("PR-AMMO", 50, []),
		])
		linked = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": pr.name, "docstatus": 1},
			pluck="serial")
		self.assertEqual(linked, ["TEST-MIX-001"])
		# No Acquisition for non-firearm.
		self.assertEqual(
			frappe.db.count("FFL Acquisition", {"purchase_receipt": pr.name, "item": "PR-AMMO"}),
			0,
		)

	def test_antique_pr_creates_no_acquisition(self):
		"""A GCA-exempt antique is received like a gun — its Serial No is created
		and tracked — but NO FFL Acquisition is booked (it stays off the bound book)."""
		pr = self._make_real_pr("ACME-FFL", [("PR-ANTIQUE", 1, ["ANTIQUE-PR-001"])])
		self.assertEqual(
			frappe.db.count("FFL Acquisition", {"purchase_receipt": pr.name}), 0)
		# Still serialized inventory: the Serial No exists and is in stock.
		self.assertEqual(
			frappe.db.get_value("Serial No", "ANTIQUE-PR-001", "status"), "Active")

	def test_antique_pr_enqueues_no_fastbound_push(self):
		"""No FFL Acquisition => the FastBound acquisition push is never enqueued
		(gating creation cuts the push at its only upstream source)."""
		with patch("frappe.enqueue") as enq:
			self._make_real_pr("ACME-FFL", [("PR-ANTIQUE", 1, ["ANTIQUE-PUSH-1"])])
		pushes = [
			c for c in enq.call_args_list
			if "push_acquisition_to_fastbound" in str(c.args[0] if c.args else c.kwargs.get("method", ""))
		]
		self.assertEqual(len(pushes), 0, f"antique must not push to FastBound, got {len(pushes)}")

	def test_mixed_antique_and_regulated_pr_creates_one_acquisition(self):
		"""A PR with an antique + a regulated gun books exactly one Acquisition —
		for the regulated serial only."""
		pr = self._make_real_pr("ACME-FFL", [
			("PR-ANTIQUE", 1, ["ANTIQUE-MIX-1"]),
			("PR-NEW-FIREARM", 1, ["REGULATED-MIX-1"]),
		])
		linked = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": pr.name, "docstatus": 1}, pluck="serial")
		self.assertEqual(linked, ["REGULATED-MIX-1"])

	def test_cannot_flag_antique_on_booked_item(self):
		"""An Item with a bound-book firearm (live FFL Acquisition) can't be flagged
		antique — a booked gun leaves only via a disposition, not a checkbox."""
		self._make_real_pr("ACME-FFL", [("PR-NEW-FIREARM", 1, ["ANTIQUE-GUARD-1"])])
		item = frappe.get_doc("Item", "PR-NEW-FIREARM")
		item.is_antique = 1
		with self.assertRaises(frappe.ValidationError) as ctx:
			item.save(ignore_permissions=True)
		self.assertIn("bound book", str(ctx.exception).lower())

	def test_can_flag_antique_on_clean_item(self):
		"""A firearm Item with no bound-book footprint CAN be flagged antique."""
		self._ensure_item("PR-CLEAN-ANTIQUE", item_group="Pistol", has_serial_no=1)
		item = frappe.get_doc("Item", "PR-CLEAN-ANTIQUE")
		item.is_antique = 1
		item.save(ignore_permissions=True)  # must not raise
		self.assertEqual(frappe.db.get_value("Item", "PR-CLEAN-ANTIQUE", "is_antique"), 1)

	def test_boundbook_presence_predicate(self):
		from ffl_core.firearm import item_has_boundbook_presence
		self._ensure_item("PR-PRESENCE", item_group="Pistol", has_serial_no=1)
		self.assertFalse(item_has_boundbook_presence("PR-PRESENCE"))
		self._make_real_pr("ACME-FFL", [("PR-PRESENCE", 1, ["PRESENCE-1"])])
		self.assertTrue(item_has_boundbook_presence("PR-PRESENCE"))

	def test_boundbook_presence_excludes_sold_serial(self):
		"""A sold/Delivered serial (even with a stale fastbound_item_id) must NOT keep
		its Item antique-locked — only in-stock (Active) booked serials count."""
		from ffl_core.firearm import item_has_boundbook_presence
		self._make_real_pr("ACME-FFL", [("PR-NEW-FIREARM", 1, ["BB-SOLD-1"])])
		self.assertTrue(item_has_boundbook_presence("PR-NEW-FIREARM"))  # in stock + acquisition
		frappe.db.set_value("Serial No", "BB-SOLD-1", "fastbound_item_id", "fb-sold")
		frappe.db.set_value("Serial No", "BB-SOLD-1", "status", "Delivered")
		self.assertFalse(item_has_boundbook_presence("PR-NEW-FIREARM"))  # cycled out

	def test_off_book_acquisition_does_not_lock_antique_flag(self):
		"""A gun booked by mistake and marked [NOT A&D] (an antique taken in as regulated)
		is NOT in the bound book: its Item can be flagged antique. A normal submitted
		acquisition still locks it, and so does a FastBound id on the serial."""
		from ffl_core.firearm import NOT_IN_BOOK_PREFIX, item_has_boundbook_presence
		self._ensure_item("PR-OFFBOOK", item_group="Pistol", has_serial_no=1)
		self._make_real_pr("ACME-FFL", [("PR-OFFBOOK", 1, ["OFFBOOK-1"])])
		self.assertTrue(item_has_boundbook_presence("PR-OFFBOOK"))  # normal acquisition locks
		acq = frappe.db.get_value("FFL Acquisition", {"serial": "OFFBOOK-1", "docstatus": 1}, "name")
		frappe.db.set_value("FFL Acquisition", acq, "fastbound_sync_error",
			NOT_IN_BOOK_PREFIX + "antique booked by mistake")
		self.assertFalse(item_has_boundbook_presence("PR-OFFBOOK"))
		# A FastBound id on the serial is still a hard "on the books" signal.
		frappe.db.set_value("Serial No", "OFFBOOK-1", "fastbound_item_id", "fb-offbook")
		self.assertTrue(item_has_boundbook_presence("PR-OFFBOOK"))
		frappe.db.set_value("Serial No", "OFFBOOK-1", "fastbound_item_id", "")
		item = frappe.get_doc("Item", "PR-OFFBOOK")
		item.is_antique = 1
		item.save(ignore_permissions=True)  # must not raise
		self.assertEqual(frappe.db.get_value("Item", "PR-OFFBOOK", "is_antique"), 1)

	def test_antique_flag_cleared_on_non_firearm(self):
		"""is_antique is meaningless on a non-firearm — the Item validate hook clears it."""
		self._ensure_item("PR-NONFA-ANT", item_group="Ammunition", has_serial_no=0)
		item = frappe.get_doc("Item", "PR-NONFA-ANT")
		item.is_antique = 1
		item.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Item", "PR-NONFA-ANT", "is_antique"), 0)

	# test_pr_submit_used_firearm_creates_acquisition was removed alongside
	# the Used Firearm path unification — Acquisitions are now created from
	# per-line Serial Nos for every firearm, exercised by the New Firearm
	# tests above.

	def test_ffl_dealer_price_stamped_onto_serials(self):
		"""ffl_dealer_price on a PR Item line is stamped onto Serial No.dealer_sell_price
		for every serial created when the receipt is submitted."""
		serials = ["TEST-DEALER-PRICE-001", "TEST-DEALER-PRICE-002"]
		self._make_real_pr("ACME-FFL", [
			("PR-NEW-FIREARM", 2, serials, {"ffl_sell_price": 999, "ffl_dealer_price": 700}),
		])
		for sn in serials:
			self.assertEqual(
				frappe.db.get_value("Serial No", sn, "dealer_sell_price"), 700
			)

	def test_pr_cancel_cancels_linked_acquisitions(self):
		serials = ["TEST-CANCEL-001"]
		pr = self._make_real_pr("ACME-FFL", [("PR-NEW-FIREARM", 1, serials)])
		acq_name = frappe.db.get_value(
			"FFL Acquisition", {"purchase_receipt": pr.name}, "name")
		self.assertTrue(acq_name)

		pr.cancel()
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", acq_name, "docstatus"), 2
		)


if __name__ == "__main__":
	unittest.main()
