"""The Sales Report costs a sold gun as it stood when THAT sale was made.

A gun the store sold can come back: bought back, traded in, or a customer's
transfer gun the store later buys outright. The receipt rewrites the Serial
No (purchase_rate, is_transfer_gun, zero_cost_confirmed) for the new stay, so a
report that read the Serial No re-costed every earlier sale of that serial
each time the period was run again. The stock ledger keeps each stay apart:
the serial's last standing inward Serial and Batch Entry at or before the sale.

Real documents end to end — Purchase Receipt → Sales Invoice / POS Invoice →
Purchase Receipt of the same serial — read through the report's own execute()
in both the Order view and the Product view. The gun Item is an antique, so
the sales skip the 4473 / NICS gates (as test_reacquired_gun_stamps does);
its costing is the same as any firearm's.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from ffl_core.ffl_core.report.sales_report import sales_report as sr

COMPANY_NAME = "FFL Test"
GUN = "SRAS-ANTIQUE-GUN"
TRADED = "SRAS-TRADED-GUN"     # the gun a customer trades in; never sold here
ACC = "SRAS-ACCESSORY"
SUPPLIER = "SRAS-PLAIN-FFL"
PAY_ON_SALE_SUPPLIER = "SRAS-PAYONSALE-FFL"
BUYER = "_Test SRAS Buyer"
TRANSFEREE = "_Test SRAS Transferee"
CONSIGNOR = "_Test SRAS Consignor"
ACC_COST = 20.0


class CostAsOfSale(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, custody, dev_site, pay_on_sale
		from ffl_core.warehouses import default_stock_warehouse, ensure_custody_warehouse
		dev_site.run()
		pay_on_sale.ensure()
		consignment.ensure()
		custody.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.warehouse = default_stock_warehouse(cls.company)
		cls.custody = ensure_custody_warehouse(cls.company)
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
				"parent_supplier_group": "All Supplier Groups", "is_group": 0}).insert(ignore_permissions=True)
		for name, ffl, pos in ((SUPPLIER, "1-77-777-77-7X-77801", 0), (PAY_ON_SALE_SUPPLIER, "1-77-777-77-7X-77802", 1)):
			fields = dict(is_ffl_dealer=1, ffl_number=ffl, ez_check_status="Verified", pay_on_sale=pos)
			if frappe.db.exists("Supplier", name):
				frappe.db.set_value("Supplier", name, fields, update_modified=False)
			else:
				frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": "Distributor",
					**fields}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", GUN):
			frappe.get_doc({"doctype": "Item", "item_code": GUN, "item_name": GUN, "item_group": "Pistol",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", GUN, "is_antique", 1, update_modified=False)
		if not frappe.db.exists("Item", TRADED):
			frappe.get_doc({"doctype": "Item", "item_code": TRADED, "item_name": TRADED, "item_group": "Pistol",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", ACC):
			frappe.get_doc({"doctype": "Item", "item_code": ACC, "item_name": ACC, "item_group": "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)
		for name in (BUYER, TRANSFEREE, CONSIGNOR):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": "Individual",
					"territory": "All Territories"}).insert(ignore_permissions=True)
		# Accessories on hand at a known cost, so an "other line" on a ticket costs something.
		cls._receipt(SUPPLIER, [{"item_code": ACC, "qty": 10, "warehouse": cls.warehouse, "rate": ACC_COST,
			"price_list_rate": ACC_COST}])

	# --- documents --------------------------------------------------------------------------------

	@classmethod
	def _receipt(cls, supplier, items, **extra):
		pr = frappe.get_doc({"doctype": "Purchase Receipt", "supplier": supplier, "company": cls.company,
			"items": items, **extra})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		return pr

	def _receive(self, serial, rate, supplier=SUPPLIER, **extra):
		# price_list_rate pinned: ERPNext fills an empty one from the item's last purchase.
		return self._receipt(supplier, [{"item_code": GUN, "qty": 1, "warehouse": extra.pop("warehouse", self.warehouse),
			"serial_no": serial, "rate": rate, "price_list_rate": rate, "allow_zero_valuation_rate": int(not rate),
			**extra.pop("row", {})}], **extra)

	def _receive_transfer(self, serial):
		"""A customer's gun in for an FFL transfer: $0, into the custody warehouse."""
		return self._receive(serial, 0, warehouse=self.custody, is_ffl_transfer=1,
			ffl_transfer_for_customer=TRANSFEREE, ffl_acquisition_type="Transfer")

	def _receive_on_consignment(self, serial):
		return self._receive(serial, 0, supplier=PAY_ON_SALE_SUPPLIER, ffl_acquisition_type="Consignment",
			ffl_acquisition_source="Individual", ffl_seller_customer=CONSIGNOR,
			ffl_seller_id_type="Driver License", ffl_seller_id_number=f"DL-{serial}")

	def _gun_row(self, serial, net, warehouse=None, qty=1):
		return {"item_code": GUN, "qty": qty, "rate": net, "warehouse": warehouse or self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1}

	def _acc_row(self, net=30.0):
		return {"item_code": ACC, "qty": 1, "rate": net, "warehouse": self.warehouse}

	def _invoice(self, rows, **header):
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = header.pop("customer", BUYER)
		si.update_stock = header.pop("update_stock", 1)
		si.set_warehouse = self.warehouse
		si.update(header)
		for row in rows:
			si.append("items", row)
		si.set_missing_values()
		for row, wanted in zip(si.items, rows):
			row.rate = row.price_list_rate = wanted["rate"]
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def _sell(self, serial, net, *extra_rows, warehouse=None):
		return self._invoice([self._gun_row(serial, net, warehouse), *extra_rows])

	def _return(self, si, serial, update_stock=1):
		return self._invoice([self._gun_row(serial, si.items[0].rate, qty=-1)], is_return=1,
			return_against=si.name, update_stock=update_stock)

	def _pos_sale(self, serial, net):
		"""A counter sale left unconsolidated: no stock posted yet for it."""
		from ffl_core.tests.pos_opening import ensure_pos_profile, todays_pos_opening
		profile = ensure_pos_profile(self.company)
		todays_pos_opening(self.company, profile)
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = profile
		inv.customer = BUYER
		inv.append("items", self._gun_row(serial, net))
		inv.set_missing_values()
		inv.items[0].rate = inv.items[0].price_list_rate = net
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": "Cash", "amount": net})
		inv.flags.ignore_permissions = True
		inv.insert()
		inv.submit()
		return inv

	# --- the report -------------------------------------------------------------------------------

	@staticmethod
	def _run(view):
		return sr.execute({"from_date": nowdate(), "to_date": nowdate(), "view": view})[1]

	def _order_row(self, invoice):
		return next(r for r in self._run("Order") if r.get("invoice") == invoice)

	def _gun_line(self, invoice):
		return next(r for r in self._run("Product")
			if r.get("invoice") == invoice and r.get("item_code") == GUN)

	def assertCost(self, invoice, cost, *, missing=False, placeholder=False, order_cost=None):
		"""The gun line's cost in the Product view and its ticket's in the Order view."""
		line, row = self._gun_line(invoice), self._order_row(invoice)
		self.assertAlmostEqual(line["cost"], cost)
		self.assertAlmostEqual(row["cost"], cost if order_cost is None else order_cost)
		self.assertEqual((bool(line["missing_cost"]), bool(line.get("placeholder_cost"))), (missing, placeholder))
		self.assertEqual((bool(row["missing_cost"]), bool(row.get("placeholder_cost"))), (missing, placeholder))

	# --- the cost of each sale stays the cost of that sale ----------------------------------------

	def test_a_transfer_gun_the_store_later_buys_still_cost_nothing_when_it_was_transferred(self):
		serial = self._testMethodName[:40]
		self._receive_transfer(serial)
		transfer = self._sell(serial, 0, self._acc_row(25), warehouse=self.custody)
		self.assertCost(transfer.name, 0.0, order_cost=ACC_COST)
		self._receive(serial, 1152)                       # the store buys the same gun outright
		self.assertEqual(frappe.db.get_value("Serial No", serial, ["purchase_rate", "is_transfer_gun"]), (1152.0, 0))
		self.assertCost(transfer.name, 0.0, order_cost=ACC_COST)
		self.assertAlmostEqual(self._order_row(transfer.name)["profit"], 25.0 - ACC_COST)

	def test_a_gun_sold_bought_back_and_sold_again_keeps_each_sales_cost(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		first = self._sell(serial, 900)
		self.assertCost(first.name, 500.0)
		self._receive(serial, 700)                        # bought back
		second = self._sell(serial, 1000)
		self.assertCost(first.name, 500.0)
		self.assertCost(second.name, 700.0)

	def test_two_guns_of_one_model_each_cost_their_own(self):
		a, b = f"{self._testMethodName[:36]}-A", f"{self._testMethodName[:36]}-B"
		self._receipt(SUPPLIER, [
			{"item_code": GUN, "qty": 1, "warehouse": self.warehouse, "serial_no": a, "rate": 300, "price_list_rate": 300},
			{"item_code": GUN, "qty": 1, "warehouse": self.warehouse, "serial_no": b, "rate": 800, "price_list_rate": 800}])
		sold_a, sold_b = self._sell(a, 600), self._sell(b, 1100)
		self.assertCost(sold_a.name, 300.0)
		self.assertCost(sold_b.name, 800.0)

	def test_a_serial_cost_edited_after_the_sale_does_not_re_cost_it(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 450)
		sale = self._sell(serial, 900)
		frappe.db.set_value("Serial No", serial, "purchase_rate", 999, update_modified=False)
		self.assertCost(sale.name, 450.0)

	def test_a_return_reverses_what_its_sale_cost_even_after_a_buy_back(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._sell(serial, 900)
		self._receive(serial, 700)
		refund = self._return(sale, serial, update_stock=0)   # money back, the gun stays sold
		self.assertCost(refund.name, -500.0)

	def test_a_return_with_the_gun_reverses_its_sale(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._sell(serial, 900)
		back = self._return(sale, serial)
		self.assertCost(back.name, -500.0)
		resold = self._sell(serial, 950)                  # the same stay: the return is not a new receipt
		self.assertCost(resold.name, 500.0)

	def test_an_unconsolidated_counter_sale_has_its_cost(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 640)
		sale = self._pos_sale(serial, 990)
		self.assertEqual(frappe.db.get_value("POS Invoice", sale.name, "consolidated_invoice"), None)
		self.assertCost(sale.name, 640.0)

	# --- placeholders and confirmed-free guns ------------------------------------------------------

	def test_a_placeholder_ledger_cost_reads_the_serials_cost_and_says_so(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		frappe.db.set_value("Serial No", serial, "purchase_rate", 600, update_modified=False)
		sale = self._sell(serial, 900)
		self.assertCost(sale.name, 600.0, placeholder=True)

	def test_a_cost_corrected_before_the_sale_is_the_sales_cost(self):
		"""The Item-page cost correction revalues the gun in the ledger (a
		Stock Reconciliation): that entry, not the placeholder receipt, is the
		gun's cost at the sale."""
		from ffl_core.api.cost_correction import correct_serial_cost
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		correct_serial_cost(serial, 400, "supplier invoice found")
		sale = self._sell(serial, 900)
		self._receive(serial, 650)
		self.assertCost(sale.name, 400.0)

	def test_a_placeholder_behind_a_later_receipt_is_a_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		frappe.db.set_value("Serial No", serial, "purchase_rate", 600, update_modified=False)
		sale = self._sell(serial, 900)
		self._receive(serial, 800)
		self.assertCost(sale.name, 0.01, missing=True)

	def test_a_gun_confirmed_free_costs_nothing_and_is_no_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0, row={"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": "Supplier gift"})
		self.assertEqual(frappe.db.get_value("Serial No", serial, "zero_cost_confirmed"), 1)
		sale = self._sell(serial, 300)
		self.assertCost(sale.name, 0.0)

	# --- the branches the ledger does not decide ---------------------------------------------------

	def test_consignment_trade_in_and_accessory_rows_are_unchanged(self):
		"""Each of these is costed before the firearm branch (or outside it):
		the report without the ledger lookup gives the same rows, and Net Sales,
		Tax and Total are the ledger lookup's to leave alone on every row."""
		consigned = f"{self._testMethodName[:34]}-CON"
		self._receive_on_consignment(consigned)
		consignment_sale = self._sell(consigned, 1000)
		owned = f"{self._testMethodName[:34]}-OWN"
		self._receive(owned, 400)
		trade_in_sale = self._invoice([self._gun_row(owned, 1300), self._trade_in_row(300)])
		accessory_sale = self._invoice([self._acc_row(45)])
		self._receive(consigned, 650)                     # both guns come back afterwards
		self._receive(owned, 900)
		ours = {consignment_sale.name, trade_in_sale.name, accessory_sale.name}

		def rows(view):
			return [{k: r.get(k) for k in ("invoice", "item_code", "cost", "missing_cost", "net_sales", "net_amount",
				"total", "line_tax", "sales_tax", "profit")} for r in self._run(view) if r.get("invoice") in ours]

		with patch.object(sr, "_fetch_cost_as_of_sale", return_value={}):
			before = {view: rows(view) for view in ("Order", "Order Detail", "Product")}
		after = {view: rows(view) for view in ("Order", "Order Detail", "Product")}
		consigned_rows = [r for r in after["Product"] if r["invoice"] == consignment_sale.name]
		self.assertTrue(consigned_rows and consigned_rows[0]["cost"] < 1000)   # the consignor's share
		credit = next(r for r in after["Product"] if r["invoice"] == trade_in_sale.name and r["item_code"] != GUN)
		self.assertEqual(credit["cost"], 0.0)
		# The owned gun on the trade-in ticket was bought back: only it may differ.
		for view in before:
			for old, new in zip(before[view], after[view]):
				if old["invoice"] == trade_in_sale.name and old.get("item_code") in (GUN, None):
					self.assertAlmostEqual(new["cost"], 400.0)
					self.assertEqual({k: v for k, v in old.items() if k not in ("cost", "profit")},
						{k: v for k, v in new.items() if k not in ("cost", "profit")})
				else:
					self.assertEqual(old, new)

	def _trade_in_row(self, cost):
		from ffl_core.acquisition import record_firearm_acquisition
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

		acquisition = record_firearm_acquisition(item_code=TRADED, serial=f"SRAS-TI-{frappe.generate_hash(length=8).upper()}",
			source_type="Trade-in", source_name="Testy Seller", cost=cost, company=self.company,
			source_id_type="Driver License", source_id_number="DL-1", source_first_name="Testy",
			source_last_name="Seller", source_address="123 Main St, Springfield TX 75024", submit_now=True,
			pending_intake_payload=frappe.as_json({"manufacturer": "Colt", "model_name": "1860", "caliber": ".44"}))
		return {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -flt(cost), "ffl_trade_in_acquisition": acquisition}
