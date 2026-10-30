"""Force-delete Item endpoint tests."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.item_admin import (
	preview_delete,
	force_delete,
	add_stock,
	set_stock,
	get_item_stock,
	on_stock_ledger_entry_change,
	recompute_item_actual_qty,
)


class ItemAdminForceDelete(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		# Run as a System Manager so the perm gate passes.
		frappe.set_user("Administrator")

		# test_bootstrap's setup_complete creates the FFL Test company + chart
		# of accounts but leaves Company.stock_received_but_not_billed (and
		# default_inventory_account) NULL, which makes Purchase Receipt submit
		# blow up with "Please set default Stock Received But Not Billed in
		# Company FFL Test". Wire those once for the whole test class.
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		if company:
			updates = {}
			if not frappe.db.get_value("Company", company, "stock_received_but_not_billed"):
				srbnb = frappe.db.get_value(
					"Account",
					{"company": company, "account_type": "Stock Received But Not Billed"},
					"name",
				)
				if srbnb:
					updates["stock_received_but_not_billed"] = srbnb
			if not frappe.db.get_value("Company", company, "default_inventory_account"):
				stock_in_hand = frappe.db.get_value(
					"Account",
					{"company": company, "account_name": "Stock In Hand"},
					"name",
				)
				if stock_in_hand:
					updates["default_inventory_account"] = stock_in_hand
			if updates:
				for k, v in updates.items():
					frappe.db.set_value("Company", company, k, v, update_modified=False)
				frappe.db.commit()

	def setUp(self):
		# Wipe any leftovers from prior runs aggressively (FrappeTestCase
		# wraps each test in a transaction, but submitted-doc state from a
		# previous failed run can persist if commit slipped through).
		test_codes = ("FORCE-DEL-CLEAN", "FORCE-DEL-WITH-PR", "FORCE-DEL-DRAFT-PR")
		for code in test_codes:
			# Drop linked PR rows directly (cascades child rows)
			pr_names = frappe.db.sql_list(
				"SELECT DISTINCT parent FROM `tabPurchase Receipt Item` WHERE item_code=%s",
				(code,),
			)
			for pr_name in pr_names:
				if frappe.db.exists("Purchase Receipt", pr_name):
					try:
						doc = frappe.get_doc("Purchase Receipt", pr_name)
						if doc.docstatus == 1:
							doc.flags.ignore_permissions = True
							doc.cancel()
						frappe.delete_doc("Purchase Receipt", pr_name, force=1, ignore_permissions=True)
					except Exception:
						pass
			# Drop Stock Ledger / Bin / Serial No / Item Barcode / Item Image Gallery
			for tbl, where in [
				("`tabStock Ledger Entry`", "item_code=%s"),
				("`tabBin`", "item_code=%s"),
				("`tabSerial No`", "item_code=%s"),
				("`tabItem Barcode`", "parent=%s"),
				("`tabItem Image Gallery`", "parent=%s"),
			]:
				try:
					frappe.db.sql(f"DELETE FROM {tbl} WHERE {where}", (code,))
				except Exception:
					pass
			# Now drop the Item
			if frappe.db.exists("Item", code):
				try:
					frappe.delete_doc("Item", code, force=1, ignore_permissions=True, ignore_on_trash=True)
				except Exception:
					frappe.db.sql("DELETE FROM `tabItem` WHERE name=%s", (code,))
		frappe.db.commit()

	def _make_item(self, code: str, *, has_serial: bool = False) -> str:
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "Products",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1 if has_serial else 0,
		})
		if has_serial:
			doc.serial_no_series = f"{code}-.####"
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	def test_preview_clean_item_returns_no_links(self):
		self._make_item("FORCE-DEL-CLEAN")
		out = preview_delete(["FORCE-DEL-CLEAN"])
		self.assertEqual(out["item_codes"], ["FORCE-DEL-CLEAN"])
		self.assertEqual(out["submitted"], [])
		self.assertEqual(out["drafts"], [])
		# May have residue from the migrate (unlikely on a fresh test bootstrap)
		# but for a brand-new item nothing in simple should reference it.
		self.assertEqual(out["simple"], [])
		self.assertEqual(out["missing"], [])

	def test_force_delete_clean_item_succeeds(self):
		self._make_item("FORCE-DEL-CLEAN")
		out = force_delete(["FORCE-DEL-CLEAN"])
		self.assertTrue(out["ok"])
		self.assertEqual(out["deleted"], ["FORCE-DEL-CLEAN"])
		self.assertEqual(out["cancelled"], [])
		self.assertFalse(frappe.db.exists("Item", "FORCE-DEL-CLEAN"))

	def test_force_delete_missing_item_reported(self):
		out = force_delete(["NEVER-EXISTED-XYZ"])
		self.assertTrue(out["ok"])
		self.assertEqual(out["deleted"], [])
		# Preview reports it; force_delete just no-ops on missing
		preview = preview_delete(["NEVER-EXISTED-XYZ"])
		self.assertEqual(preview["missing"], ["NEVER-EXISTED-XYZ"])

	def test_force_delete_with_draft_pr_succeeds(self):
		"""Items linked from a DRAFT (docstatus=0) PR are deletable: the
		draft PR is removed, then the item."""
		self._make_item("FORCE-DEL-DRAFT-PR")
		# Need a supplier to make the PR insertable
		supplier_name = "TEST-FFL-SUPPLIER-FORCE-DEL"
		if not frappe.db.exists("Supplier", supplier_name):
			frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": supplier_name,
				"supplier_group": "All Supplier Groups",
			}).insert(ignore_permissions=True)
		# Use the FFL Test company's warehouse (mirrors test_purchase_compliance pattern)
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		warehouse = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0},
			pluck="name",
			limit=1,
		)
		warehouse = warehouse[0] if warehouse else None
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier_name,
			"company": company,
			"set_warehouse": warehouse,
			"items": [{
				"item_code": "FORCE-DEL-DRAFT-PR",
				"qty": 1,
				"rate": 10,
				"warehouse": warehouse,
			}],
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		# Don't submit — leave as draft
		self.assertEqual(pr.docstatus, 0)

		# Preview should show 1 draft PR but no submitted
		preview = preview_delete(["FORCE-DEL-DRAFT-PR"])
		self.assertEqual(preview["submitted"], [])
		self.assertEqual(len(preview["drafts"]), 1)
		self.assertEqual(preview["drafts"][0]["doctype"], "Purchase Receipt")

		# Force delete WITHOUT cancel_submitted=True still works (drafts
		# are removed, not "cancelled")
		out = force_delete(["FORCE-DEL-DRAFT-PR"])
		self.assertTrue(out["ok"])
		self.assertIn("FORCE-DEL-DRAFT-PR", out["deleted"])
		self.assertEqual(len(out["deleted_drafts"]), 1)
		self.assertFalse(frappe.db.exists("Purchase Receipt", pr.name))
		self.assertFalse(frappe.db.exists("Item", "FORCE-DEL-DRAFT-PR"))

	def test_force_delete_without_cancel_flag_throws_when_submitted_exists(self):
		"""When a submitted PR exists, force_delete must throw unless
		cancel_submitted=True is explicitly passed."""
		self._make_item("FORCE-DEL-WITH-PR")
		supplier_name = "TEST-FFL-SUPPLIER-FORCE-DEL"
		if not frappe.db.exists("Supplier", supplier_name):
			frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": supplier_name,
				"supplier_group": "All Supplier Groups",
			}).insert(ignore_permissions=True)
		# Use the FFL Test company's warehouse (mirrors test_purchase_compliance pattern)
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		warehouse = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0},
			pluck="name",
			limit=1,
		)
		warehouse = warehouse[0] if warehouse else None
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier_name,
			"company": company,
			"set_warehouse": warehouse,
			"items": [{
				"item_code": "FORCE-DEL-WITH-PR",
				"qty": 1,
				"rate": 10,
				"warehouse": warehouse,
			}],
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()

		# Without cancel flag: ValidationError
		with self.assertRaises(frappe.ValidationError):
			force_delete(["FORCE-DEL-WITH-PR"], cancel_submitted=False)

		# With cancel flag: OK, PR cancelled, item gone
		out = force_delete(["FORCE-DEL-WITH-PR"], cancel_submitted=True)
		self.assertTrue(out["ok"])
		self.assertIn("FORCE-DEL-WITH-PR", out["deleted"])
		self.assertEqual(len(out["cancelled"]), 1)
		self.assertEqual(out["cancelled"][0]["doctype"], "Purchase Receipt")
		# PR row still exists but is cancelled (docstatus=2) — audit trail
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "docstatus"), 2)
		self.assertFalse(frappe.db.exists("Item", "FORCE-DEL-WITH-PR"))


class ItemAdminAddStock(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")

		# test_bootstrap doesn't fill all company-default accounts. Material
		# Receipt Stock Entries need stock_adjustment_account; PR submits
		# need stock_received_but_not_billed; both need default_inventory.
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		if company:
			updates = {}
			if not frappe.db.get_value("Company", company, "stock_adjustment_account"):
				adj = frappe.db.get_value("Account",
					{"company": company, "account_type": "Stock Adjustment"}, "name")
				if adj:
					updates["stock_adjustment_account"] = adj
			if not frappe.db.get_value("Company", company, "stock_received_but_not_billed"):
				srbnb = frappe.db.get_value("Account",
					{"company": company, "account_type": "Stock Received But Not Billed"}, "name")
				if srbnb:
					updates["stock_received_but_not_billed"] = srbnb
			if not frappe.db.get_value("Company", company, "default_inventory_account"):
				stock_in_hand = frappe.db.get_value("Account",
					{"company": company, "account_name": "Stock In Hand"}, "name")
				if stock_in_hand:
					updates["default_inventory_account"] = stock_in_hand
			if updates:
				for k, v in updates.items():
					frappe.db.set_value("Company", company, k, v, update_modified=False)
				frappe.db.commit()

	def setUp(self):
		# Wipe leftover SE rows + test items
		for code in ("ADDSTOCK-CLEAN", "ADDSTOCK-SERIAL"):
			# Cancel + delete any Stock Entries referencing this item
			ses = frappe.db.sql_list(
				"SELECT DISTINCT parent FROM `tabStock Entry Detail` WHERE item_code=%s",
				(code,))
			for se in ses:
				if frappe.db.exists("Stock Entry", se):
					try:
						doc = frappe.get_doc("Stock Entry", se)
						if doc.docstatus == 1:
							doc.flags.ignore_permissions = True
							doc.cancel()
						frappe.delete_doc("Stock Entry", se, force=1, ignore_permissions=True)
					except Exception:
						pass
			# Drop SLE / Bin rows
			for tbl, where in [
				("`tabStock Ledger Entry`", "item_code=%s"),
				("`tabBin`", "item_code=%s"),
			]:
				try:
					frappe.db.sql(f"DELETE FROM {tbl} WHERE {where}", (code,))
				except Exception:
					pass
			if frappe.db.exists("Item", code):
				try:
					frappe.delete_doc("Item", code, force=1, ignore_permissions=True)
				except Exception:
					frappe.db.sql("DELETE FROM `tabItem` WHERE name=%s", (code,))
		frappe.db.commit()

	def _ensure_warehouse(self) -> str:
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		row = frappe.get_all("Warehouse",
			filters={"company": company, "is_group": 0}, pluck="name", limit=1)
		return row[0] if row else None

	def test_add_stock_creates_submitted_stock_entry(self):
		"""Happy path: add 5 units to a non-serial item, verify Bin reflects it."""
		# Make a non-serial stock item
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Add Stock Clean",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)

		warehouse = self._ensure_warehouse()
		out = add_stock("ADDSTOCK-CLEAN", 5, warehouse=warehouse, rate=12.50, remarks="initial seed")
		self.assertTrue(out["ok"])
		self.assertEqual(out["qty"], 5.0)
		self.assertTrue(out["name"])  # Stock Entry name returned

		# SE submitted
		self.assertEqual(frappe.db.get_value("Stock Entry", out["name"], "docstatus"), 1)
		# Bin reflects the qty
		bin_qty = frappe.db.get_value("Bin",
			{"item_code": "ADDSTOCK-CLEAN", "warehouse": warehouse}, "actual_qty")
		self.assertEqual(float(bin_qty or 0), 5.0)
		# get_item_stock helper agrees
		stock = get_item_stock(["ADDSTOCK-CLEAN"])
		self.assertEqual(stock["ADDSTOCK-CLEAN"], 5.0)

	def test_add_stock_rejects_serial_item(self):
		"""Items with has_serial_no=1 must go through Receive Goods."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-SERIAL",
			"item_name": "Serial Firearm",
			"item_group": "Pistol",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
			"serial_no_series": "ADDSTOCK-SERIAL-.####",
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as ctx:
			add_stock("ADDSTOCK-SERIAL", 1, warehouse=self._ensure_warehouse())
		self.assertIn("serial", str(ctx.exception).lower())

	def test_add_stock_qty_must_be_positive(self):
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Add Stock Clean",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			add_stock("ADDSTOCK-CLEAN", 0, warehouse=self._ensure_warehouse())
		with self.assertRaises(frappe.ValidationError):
			add_stock("ADDSTOCK-CLEAN", -3, warehouse=self._ensure_warehouse())

	def test_add_stock_missing_item_throws(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			add_stock("DOES-NOT-EXIST-ZZZ", 1, warehouse=self._ensure_warehouse())
		self.assertIn("not found", str(ctx.exception).lower())

	def test_set_stock_creates_stock_reconciliation(self):
		"""set_stock should create a submitted Stock Reconciliation that
		drives Bin.actual_qty to the absolute target."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Set Stock Test",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
			"valuation_rate": 5,
		}).insert(ignore_permissions=True)
		warehouse = self._ensure_warehouse()
		# Seed some existing stock first
		add_stock("ADDSTOCK-CLEAN", 3, warehouse=warehouse, rate=5)
		# Now adjust to 10
		out = set_stock("ADDSTOCK-CLEAN", 10, warehouse=warehouse, reason="Annual count")
		self.assertTrue(out["ok"])
		self.assertEqual(out["prev_qty"], 3.0)
		self.assertEqual(out["new_qty"], 10.0)
		self.assertEqual(out["delta"], 7.0)
		self.assertEqual(frappe.db.get_value("Stock Reconciliation", out["name"], "docstatus"), 1)
		# Bin reflects new absolute qty
		bin_qty = frappe.db.get_value("Bin",
			{"item_code": "ADDSTOCK-CLEAN", "warehouse": warehouse}, "actual_qty")
		self.assertEqual(float(bin_qty or 0), 10.0)

	def test_set_stock_no_change_throws(self):
		"""Setting qty to current qty should error rather than create an
		empty reconciliation."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "No Change Test",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		warehouse = self._ensure_warehouse()
		# Bin is empty (qty=0). Setting to 0 should throw.
		with self.assertRaises(frappe.ValidationError) as ctx:
			set_stock("ADDSTOCK-CLEAN", 0, warehouse=warehouse, reason="No-op")
		self.assertIn("nothing to adjust", str(ctx.exception).lower())

	def test_set_stock_rejects_serial_item(self):
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-SERIAL",
			"item_name": "Serial Firearm",
			"item_group": "Pistol",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
			"serial_no_series": "ADDSTOCK-SERIAL-.####",
		}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			set_stock("ADDSTOCK-SERIAL", 5, warehouse=self._ensure_warehouse())
		self.assertIn("serial-tracked", str(ctx.exception).lower())

	def test_set_stock_negative_qty_throws(self):
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Neg Qty Test",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			set_stock("ADDSTOCK-CLEAN", -1, warehouse=self._ensure_warehouse())

	def _clean_item(self, **extra) -> str:
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Set Stock Rate",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
			**extra,
		}).insert(ignore_permissions=True)
		return self._ensure_warehouse()

	def test_set_stock_keeps_the_bins_own_rate(self):
		"""Writing one unit off books exactly that unit's cost and leaves the rest
		of the bin at what it cost — not re-valued at the Item's default rate."""
		warehouse = self._clean_item(valuation_rate=5)
		add_stock("ADDSTOCK-CLEAN", 10, warehouse=warehouse, rate=12)
		out = set_stock("ADDSTOCK-CLEAN", 9, warehouse=warehouse, reason="Damaged box")
		bin_rate = frappe.db.get_value("Bin",
			{"item_code": "ADDSTOCK-CLEAN", "warehouse": warehouse}, "valuation_rate")
		self.assertAlmostEqual(float(bin_rate), 12.0)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Stock Reconciliation", out["name"], "difference_amount")), -12.0)

	def test_set_stock_decrease_works_without_a_master_rate(self):
		"""Stock received through + Add Stock has no Item.valuation_rate or
		last_purchase_rate; lowering it must still post (at the bin's rate)."""
		warehouse = self._clean_item()
		add_stock("ADDSTOCK-CLEAN", 4, warehouse=warehouse, rate=7)
		out = set_stock("ADDSTOCK-CLEAN", 2, warehouse=warehouse)
		self.assertEqual(frappe.db.get_value("Stock Reconciliation", out["name"], "docstatus"), 1)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Stock Reconciliation", out["name"], "difference_amount")), -14.0)

	def test_set_stock_increase_with_no_cost_on_file_is_refused(self):
		warehouse = self._clean_item()
		with self.assertRaises(frappe.ValidationError) as ctx:
			set_stock("ADDSTOCK-CLEAN", 5, warehouse=warehouse)
		self.assertIn("no cost on file", str(ctx.exception).lower())

	def test_set_stock_allows_for_an_open_register_sale(self):
		"""A submitted POS Invoice stays out of the stock ledger until the register
		closes, so the Bin still holds the sold unit. The entered figure is the shelf:
		the Bin is set to it plus the open sale, and reads it once the close posts."""
		from frappe.utils import now_datetime
		warehouse = self._clean_item()
		add_stock("ADDSTOCK-CLEAN", 10, warehouse=warehouse, rate=5)
		company = frappe.db.get_value("Warehouse", warehouse, "company")
		# Written straight to the tables, as the inventory count tests do: a real
		# sale needs a shift, a profile and payments, none of which this is about.
		when = now_datetime()
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"ADDSTOCK-POS-{frappe.generate_hash(length=8)}",
			"company": company, "docstatus": 1,
			"posting_date": when.date().isoformat(), "posting_time": when.time().isoformat()})
		inv.db_insert()
		line = frappe.new_doc("POS Invoice Item")
		line.update({"name": frappe.generate_hash(length=10), "parent": inv.name,
			"parenttype": "POS Invoice", "parentfield": "items", "idx": 1, "docstatus": 1,
			"item_code": "ADDSTOCK-CLEAN", "qty": 1, "stock_qty": 1, "warehouse": warehouse})
		line.db_insert()
		self.addCleanup(lambda: (frappe.db.delete("POS Invoice Item", {"parent": inv.name}),
			frappe.db.delete("POS Invoice", {"name": inv.name}), frappe.db.commit()))

		out = set_stock("ADDSTOCK-CLEAN", 8, warehouse=warehouse, reason="Shelf count")
		self.assertEqual(out["new_qty"], 8.0)
		self.assertEqual(out["open_register_qty"], 1.0)
		self.assertEqual(out["delta"], -1.0)

		def bin_qty():
			return float(frappe.db.get_value("Bin",
				{"item_code": "ADDSTOCK-CLEAN", "warehouse": warehouse}, "actual_qty") or 0)
		self.assertEqual(bin_qty(), 9.0)
		# Entering the same shelf count again is a no-op, not a second unit off.
		with self.assertRaises(frappe.ValidationError) as ctx:
			set_stock("ADDSTOCK-CLEAN", 8, warehouse=warehouse)
		self.assertIn("nothing to adjust", str(ctx.exception).lower())

		# The register close posts the sale's stock exit (stood in for by an issue)
		# and marks the ticket merged: the Bin now reads the shelf count.
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Issue",
			"company": company, "items": [{"item_code": "ADDSTOCK-CLEAN", "qty": 1,
				"s_warehouse": warehouse}]})
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.set_value("POS Invoice", inv.name, "consolidated_invoice", "SINV-CLOSE",
			update_modified=False)
		self.assertEqual(bin_qty(), 8.0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			set_stock("ADDSTOCK-CLEAN", 8, warehouse=warehouse)
		self.assertIn("nothing to adjust", str(ctx.exception).lower())

	def test_two_consecutive_add_stock_keeps_item_qty_in_sync(self):
		"""Regression for race condition: when two stock-adding events hit
		the same Item back-to-back, Item.actual_qty must reflect the cumulative
		Bin total — not just the first SLE's contribution. Previously the
		SLE doc_events hook read Bin with a stale snapshot."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Two-Add Race",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		warehouse = self._ensure_warehouse()
		add_stock("ADDSTOCK-CLEAN", 10, warehouse=warehouse, rate=5)
		add_stock("ADDSTOCK-CLEAN", 10, warehouse=warehouse, rate=5)
		# Bin total = 20
		bin_qty = frappe.db.get_value("Bin",
			{"item_code": "ADDSTOCK-CLEAN", "warehouse": warehouse}, "actual_qty")
		self.assertEqual(float(bin_qty or 0), 20.0)
		# Item.actual_qty must agree
		item_qty = frappe.db.get_value("Item", "ADDSTOCK-CLEAN", "actual_qty")
		self.assertEqual(float(item_qty or 0), 20.0)

	def test_recompute_item_actual_qty_corrects_stale_value(self):
		"""recompute_item_actual_qty re-derives Item.actual_qty from the Bin
		sum, healing any drift (manual DB edit, missed hook, reconciliation)."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Recompute Heals Drift",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		warehouse = self._ensure_warehouse()
		add_stock("ADDSTOCK-CLEAN", 7, warehouse=warehouse, rate=5)
		# Corrupt Item.actual_qty to simulate drift (e.g. a reconciliation whose
		# actual_qty=0 SLE never moved the delta).
		frappe.db.set_value("Item", "ADDSTOCK-CLEAN", "actual_qty", 999, update_modified=False)
		out = recompute_item_actual_qty("ADDSTOCK-CLEAN")
		self.assertEqual(out, 7.0)
		self.assertEqual(float(frappe.db.get_value("Item", "ADDSTOCK-CLEAN", "actual_qty")), 7.0)

	def test_reconciliation_sle_enqueues_recompute_not_delta(self):
		"""A Stock Reconciliation SLE carries actual_qty=0 (the absolute lives
		in qty_after_transaction), so the delta path is blind to it. The hook
		must instead enqueue a Bin-sum recompute deferred to after_commit, and
		must NOT mutate Item.actual_qty via the (zero) delta."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Reco Enqueues Recompute",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", "ADDSTOCK-CLEAN", "actual_qty", 5, update_modified=False)

		doc = frappe._dict(item_code="ADDSTOCK-CLEAN", actual_qty=0, voucher_type="Stock Reconciliation")
		with patch("ffl_core.api.item_admin.frappe.enqueue") as mock_enqueue:
			on_stock_ledger_entry_change(doc, method="on_submit")
			mock_enqueue.assert_called_once()
			pos_args, kwargs = mock_enqueue.call_args
			self.assertEqual(pos_args[0], "ffl_core.api.item_admin.recompute_item_actual_qty")
			self.assertEqual(kwargs["item_code"], "ADDSTOCK-CLEAN")
			self.assertTrue(kwargs["enqueue_after_commit"])
		# Delta path skipped → the (stale) value is left untouched for the
		# deferred recompute, not bumped by the zero delta.
		self.assertEqual(float(frappe.db.get_value("Item", "ADDSTOCK-CLEAN", "actual_qty")), 5.0)

	def test_normal_sle_applies_delta_and_does_not_enqueue(self):
		"""A non-reconciliation SLE applies its actual_qty as a delta in-band
		and never enqueues a recompute (the Bin isn't settled yet at hook time)."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": "ADDSTOCK-CLEAN",
			"item_name": "Normal SLE Delta",
			"item_group": "Misc Accessory",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", "ADDSTOCK-CLEAN", "actual_qty", 5, update_modified=False)

		doc = frappe._dict(item_code="ADDSTOCK-CLEAN", actual_qty=3, voucher_type="Stock Entry")
		with patch("ffl_core.api.item_admin.frappe.enqueue") as mock_enqueue:
			on_stock_ledger_entry_change(doc, method="on_submit")
			mock_enqueue.assert_not_called()
		self.assertEqual(float(frappe.db.get_value("Item", "ADDSTOCK-CLEAN", "actual_qty")), 8.0)
