"""Inventory Import (Settings → Inventory Import): CSV → Items → opening stock."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import inventory_import as imp

HEADER = "UPC,Name,Brand,Part Number,Category,Quantity,Unit Cost,Price\n"


def _csv(*lines: str) -> str:
	return HEADER + "\n".join(lines) + "\n"


def _upc(seed: int) -> str:
	"""A valid 12-digit UPC-A, unique per test."""
	body = f"{seed:011d}"
	odd = sum(int(d) for d in body[0::2])
	even = sum(int(d) for d in body[1::2])
	return body + str((10 - (odd * 3 + even) % 10) % 10)


class InventoryImportTest(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")
		from ffl_core.api.item_admin import _default_warehouse
		cls.warehouse = _default_warehouse()
		cls.seed = int(frappe.utils.now_datetime().strftime("%H%M%S%f")[:9])

	def setUp(self):
		self.patcher = patch.object(imp, "_catalog_offer", return_value=None)
		self.patcher.start()
		self.addCleanup(self.patcher.stop)
		type(self).seed += 1

	def upc(self, k: int = 0) -> str:
		return _upc(self.seed * 10 + k)

	def plan(self, text: str) -> list[dict]:
		return imp.plan(imp.parse(text), self.warehouse)

	# --- parsing ---

	def test_parse_tolerates_bom_case_and_blank_lines(self):
		rows = imp.parse("﻿upc , NAME,quantity,UNIT COST\n123,Box,2,5\n,,,\n")
		self.assertEqual(rows, [{"line": 2, "upc": "123", "name": "Box", "qty": "2", "cost": "5"}])

	def test_parse_refuses_file_without_quantity_or_cost(self):
		with self.assertRaises(frappe.ValidationError):
			imp.parse("UPC,Name\n123,Box\n")

	def test_upc_normalization(self):
		self.assertEqual(imp.normalize_upc("12345678901"), ("012345678901", None))
		self.assertEqual(imp.normalize_upc("0123-4567-8901")[0], "012345678901")
		self.assertTrue(imp.normalize_upc("8.5E+11")[1])
		self.assertTrue(imp.normalize_upc("12345")[1])

	# --- planning ---

	def test_row_checks(self):
		u = self.upc
		planned = self.plan(_csv(
			f"{u(1)},Sling,Acme,SL-1,Slings & Mounts,3,12.50,24.99",
			f"{u(2)},Pistol,Acme,P-1,Pistol,1,300,450",
			f"{u(3)},Free thing,,,,2,0,5",
			f"{u(4)},Nothing,,,,0,5,9",
			f"{u(5)},Half,,,,1.5,5,9",
			f"{u(6)},Odd,,,Not A Category,1,5,9",
			f"{u(1)},Sling again,,,,1,12.50,24.99",
			",,,,,1,5,9",
		))
		got = [(r["line"], r["outcome"]) for r in planned]
		# Both lines of a repeated item are refused, not the first kept.
		self.assertEqual(got, [(2, "error"), (3, "error"), (4, "error"), (5, "skipped"),
			(6, "error"), (7, "error"), (8, "error"), (9, "error")])
		self.assertIn("FastBound", planned[1]["message"])
		self.assertIn("2, 8", planned[0]["message"])
		self.assertIn("2, 8", planned[6]["message"])

	def test_blank_category_defaults_and_no_upc_uses_part_number(self):
		planned = self.plan(_csv(f",Cleaning kit,Acme,CK-{self.seed},,2,8,15"))
		self.assertEqual(planned[0]["outcome"], "new")
		self.assertEqual(planned[0]["item_code"], f"CK-{self.seed}")
		self.assertEqual(planned[0]["item_group"], imp.DEFAULT_GROUP)

	def test_catalog_offer_is_used_and_catalog_firearm_refused(self):
		offers = {
			self.upc(1): {"distributor": "Sports South", "sku": "A1", "is_firearm": False},
			self.upc(2): {"distributor": "Lipsey's", "sku": "B2", "is_firearm": True},
		}
		with patch.object(imp, "_catalog_offer", side_effect=lambda upc: offers.get(upc)):
			planned = self.plan(_csv(f"{self.upc(1)},,,,,4,10,", f"{self.upc(2)},,,,,1,300,"))
		self.assertEqual([r["outcome"] for r in planned], ["catalog", "error"])
		self.assertIn("Sports South", planned[0]["message"])

	# --- import ---

	def test_import_creates_items_and_opening_stock_then_rerun_skips(self):
		u = self.upc
		text = _csv(
			f"{u(1)},Test Sling {self.seed},Acme Test,SL-{self.seed},Slings & Mounts,3,12.50,24.99",
			f"{u(2)},Test Mag {self.seed},,,Magazine,5,20,34.99",
		)
		result = imp.run(self.plan(text), warehouse=self.warehouse, posting_date=frappe.utils.nowdate())
		self.assertEqual(result["summary"]["counts"]["new"], 2)
		self.assertEqual(result["summary"]["value"], 137.5)
		self.assertEqual(len(result["reconciliations"]), 1)

		sr = frappe.get_doc("Stock Reconciliation", result["reconciliations"][0])
		self.assertEqual((sr.docstatus, sr.purpose), (1, "Opening Stock"))
		gl = frappe.get_all("GL Entry", filters={"voucher_no": sr.name, "is_cancelled": 0},
			fields=["account", "debit", "credit", "is_opening"])
		temp = frappe.db.get_value("Account", sr.expense_account, "account_type")
		self.assertEqual(temp, "Temporary")
		self.assertEqual({g.is_opening for g in gl}, {"Yes"})
		self.assertEqual(sum(g.debit for g in gl), 137.5)
		self.assertEqual(sum(g.credit for g in gl if g.account == sr.expense_account), 137.5)
		code = frappe.db.get_value("Item Barcode", {"barcode": u(1)}, "parent")
		item = frappe.db.get_value("Item", code,
			["item_group", "brand", "mpn", "standard_rate", "dealer_price"], as_dict=True)
		self.assertEqual((item.item_group, item.brand, item.mpn), ("Slings & Mounts", "Acme Test", f"SL-{self.seed}"))
		self.assertEqual((item.standard_rate, item.dealer_price), (24.99, 12.5))
		bin_ = frappe.db.get_value("Bin", {"item_code": code, "warehouse": self.warehouse},
			["actual_qty", "valuation_rate"], as_dict=True)
		self.assertEqual((bin_.actual_qty, bin_.valuation_rate), (3, 12.5))

		again = self.plan(text)
		self.assertEqual([r["outcome"] for r in again], ["skipped", "skipped"])

	def test_existing_firearm_item_is_refused(self):
		code = f"II-GUN-{self.seed}"
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Rifle", "stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
			"is_firearm": 1}).insert(ignore_permissions=True)
		planned = self.plan(_csv(f",,,{code},,1,100,"))
		self.assertEqual(planned[0]["outcome"], "error")
		self.assertIn("FastBound", planned[0]["message"])

	def test_item_with_stock_history_is_skipped_even_at_zero(self):
		from ffl_core.api.item_admin import add_stock, set_stock
		code = f"II-HIST-{self.seed}"
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Magazine", "stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)
		add_stock(code, 2, warehouse=self.warehouse, rate=5)
		set_stock(code, 0, warehouse=self.warehouse)
		planned = self.plan(_csv(f",,,{code},,4,5,"))
		self.assertEqual(planned[0]["outcome"], "skipped")

	def test_cut_over_date_checks(self):
		with self.assertRaises(frappe.ValidationError):
			imp.check_posting(self.warehouse, None)
		with self.assertRaises(frappe.ValidationError):
			imp.check_posting(self.warehouse, frappe.utils.add_days(frappe.utils.nowdate(), 1))
		company, account = imp.check_posting(self.warehouse, frappe.utils.nowdate())
		self.assertEqual(frappe.db.get_value("Account", account, "account_type"), "Temporary")

	def test_cost_not_below_price_warns_but_imports(self):
		planned = self.plan(_csv(f"{self.upc(1)},Swapped,,,,1,30,20"))
		self.assertEqual(planned[0]["outcome"], "new")
		self.assertIn("Unit Cost is not below Price", planned[0]["message"])
