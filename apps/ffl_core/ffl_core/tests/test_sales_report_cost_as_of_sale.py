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
from frappe.utils import add_days, flt, nowdate

from ffl_core.ffl_core.report.sales_report import sales_report as sr

COMPANY_NAME = "FFL Test"
GUN = "SRAS-ANTIQUE-GUN"
TRADED = "SRAS-TRADED-GUN"     # the gun a customer trades in; never sold here
ACC = "SRAS-ACCESSORY"
SUPPLIER = "SRAS-PLAIN-FFL"
PAY_ON_SALE_SUPPLIER = "SRAS-PAYONSALE-FFL"
BUYER = "_Test SRAS Buyer"
OTHER_BUYER = "_Test SRAS Other Buyer"
TRANSFEREE = "_Test SRAS Transferee"
CONSIGNOR = "_Test SRAS Consignor"
ACC_COST = 20.0
BACK_ROOM = "SRAS Back Room"
COUNT_ROOM = "SRAS Count Room"   # holds one test's gun at a time: a count sets the whole warehouse's quantity


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
		cls.back_room, cls.count_room = (frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": name})
			or frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": cls.company,
				"parent_warehouse": frappe.db.get_value("Warehouse", cls.warehouse, "parent_warehouse")}).insert(
					ignore_permissions=True).name for name in (BACK_ROOM, COUNT_ROOM))
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
		for name in (BUYER, OTHER_BUYER, TRANSFEREE, CONSIGNOR):
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

	def _bill(self, serial, net, customer=BUYER):
		"""Invoiced now, the gun handed over later (update_stock=0): no stock moves with the sale."""
		return self._invoice([self._gun_row(serial, net)], update_stock=0, customer=customer)

	def _issue(self, serial, warehouse=None):
		"""The gun handed over by hand: a Material Issue, as for a billed (update_stock=0) sale."""
		from ffl_core.firearm import issue_serial
		return issue_serial(GUN, serial, warehouse or self.warehouse)

	def _material_receipt(self, serial, rate, remarks=None, warehouse=None):
		"""The gun taken back in by hand: a Material Receipt at *rate*."""
		from ffl_core.firearm import serialized_receipt_row
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
			"remarks": remarks,
			"items": [serialized_receipt_row(item_code=GUN, qty=1, warehouse=warehouse or self.warehouse, basic_rate=rate,
				serial=serial)]})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return se

	@staticmethod
	def _ledger(serial):
		"""The serial's standing stock entries in order: (voucher type, purpose, direction)."""
		return [tuple(r) for r in frappe.db.sql("""
			select b.voucher_type, coalesce(se.purpose, ''), b.type_of_transaction
			from `tabSerial and Batch Bundle` b join `tabSerial and Batch Entry` e on e.parent = b.name
			left join `tabStock Entry` se on b.voucher_type = 'Stock Entry' and se.name = b.voucher_no
			where e.serial_no = %s and b.docstatus = 1 and b.is_cancelled = 0
			order by b.posting_datetime, b.creation""", serial)]

	def _returned_in_transit_and_reissued(self, serial, customer=BUYER, credit_note=False):
		"""Sold (billed, issued by hand), back in by a Material Receipt at its cost that names the
		Material Issue it reverses, billed again at $0."""
		self._receive(serial, 500)
		sale = self._bill(serial, 900)
		issue = self._issue(serial)
		if credit_note:
			self._return(sale, serial, update_stock=0)
		self._material_receipt(serial, 500, remarks=f"Returned in transit, reverses {issue}")
		reissue = self._bill(serial, 0, customer=customer)
		self._issue(serial)
		self.assertEqual(self._ledger(serial), [
			("Purchase Receipt", "", "Inward"), ("Stock Entry", "Material Issue", "Outward"),
			("Stock Entry", "Material Receipt", "Inward"), ("Stock Entry", "Material Issue", "Outward")])
		return sale, reissue

	def _new_serial(self, serial):
		"""A Serial No on file before its gun has arrived (the order is taken first)."""
		frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": GUN,
			"company": self.company}).insert(ignore_permissions=True)

	def _count(self, serial, rate=None):
		"""An inventory count of the count room: the gun found there at *rate*, or (no rate) not found."""
		row = {"item_code": GUN, "warehouse": self.count_room, "qty": 1, "serial_no": serial, "valuation_rate": rate,
			"use_serial_batch_fields": 1} if rate is not None else {
			"item_code": GUN, "warehouse": self.count_room, "qty": 0, "valuation_rate": 0,
			"allow_zero_valuation_rate": 1, "reconcile_all_serial_batch": 1}
		doc = frappe.get_doc({"doctype": "Stock Reconciliation", "company": self.company,
			"purpose": "Stock Reconciliation", "items": [row]})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.submit()
		return doc

	def _move(self, serial, src, dst):
		from ffl_core.firearm import transfer_serial
		return transfer_serial(GUN, serial, src, dst)

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

	def _transfer_ticket(self, serial):
		"""A counter transfer ticket as the report reads it: the gun at $0 on an
		is_ffl_transfer POS Invoice. Written directly, since the gun is no longer
		in stock to put through the register."""
		return self._free_ticket(serial, TRANSFEREE, is_ffl_transfer=1)

	def _free_ticket(self, serial, customer, **header):
		"""A submitted POS Invoice handing *serial* over at $0, written directly."""
		name = f"SRAS-FREE-{frappe.generate_hash(length=8)}"
		frappe.get_doc({"doctype": "POS Invoice", "name": name, "company": self.company, "customer": customer,
			"posting_date": nowdate(), "posting_time": frappe.utils.nowtime(), "docstatus": 1,
			"base_net_total": 0, "grand_total": 0, **header}).db_insert()
		frappe.get_doc({"doctype": "POS Invoice Item", "name": frappe.generate_hash(length=10), "parent": name,
			"parenttype": "POS Invoice", "parentfield": "items", "idx": 1, "docstatus": 1, "item_code": GUN,
			"qty": 1, "base_net_amount": 0, "serial_no": serial}).db_insert()
		return name

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
		self.assertAlmostEqual(self._order_row(sale.name)["cost"] + self._order_row(refund.name)["cost"], 0.0)

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

	# --- moves and revaluations are not receipts ----------------------------------------------------

	def test_a_transfer_gun_moved_into_custody_after_its_sale_is_no_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0, is_ffl_transfer=1, ffl_transfer_for_customer=TRANSFEREE,
			ffl_acquisition_type="Transfer")              # landed in sellable stock by hand
		self.assertEqual(frappe.db.get_value("Serial No", serial, "is_transfer_gun"), 1)
		ticket = self._bill(serial, 0)
		self._move(serial, self.warehouse, self.custody)
		self.assertCost(ticket.name, 0.0)

	def test_the_stores_own_gun_moved_into_custody_before_its_sale_keeps_its_cost(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 600)
		self._move(serial, self.warehouse, self.custody)
		sale = self._sell(serial, 950, warehouse=self.custody)
		self.assertCost(sale.name, 600.0)

	def test_a_placeholder_gun_moved_after_its_sale_still_reads_the_serials_cost(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		frappe.db.set_value("Serial No", serial, "purchase_rate", 900, update_modified=False)
		sale = self._bill(serial, 1200)
		self._move(serial, self.warehouse, self.back_room)
		self.assertCost(sale.name, 900.0, placeholder=True)

	def test_a_cost_correction_between_sale_and_hand_over_is_not_a_receipt(self):
		from ffl_core.api.cost_correction import correct_serial_cost
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		frappe.db.set_value("Serial No", serial, "purchase_rate", 600, update_modified=False)
		sale = self._bill(serial, 1000)
		correct_serial_cost(serial, 700, "supplier invoice found")
		self.assertCost(sale.name, 700.0, placeholder=True)

	# --- a sale made before the gun arrived ----------------------------------------------------------

	def test_a_gun_invoiced_before_it_arrived_costs_its_arrival(self):
		from ffl_core.firearm import issue_serial
		serial = self._testMethodName[:40]
		self._new_serial(serial)
		order = self._bill(serial, 1100)                  # T0: invoiced, the gun still on its way
		self._receive(serial, 620)                        # T1: it arrives
		issue_serial(GUN, serial, self.warehouse)         # T2: handed over
		self._receive(serial, 900)                        # T3: bought back
		self.assertCost(order.name, 620.0)

	def test_a_gun_corrected_before_hand_over_costs_what_it_was_handed_over_at(self):
		from ffl_core.api.cost_correction import correct_serial_cost
		from ffl_core.firearm import issue_serial
		serial = self._testMethodName[:40]
		self._new_serial(serial)
		order = self._bill(serial, 1100)                  # invoiced, the gun still on its way
		self._receive(serial, 600)                        # it arrives
		correct_serial_cost(serial, 700, "supplier invoice found")
		issue_serial(GUN, serial, self.warehouse)         # handed over
		self._receive(serial, 900)                        # bought back later
		self.assertCost(order.name, 700.0)

	def test_a_gun_invoiced_before_it_arrived_and_not_handed_over_costs_its_latest_value(self):
		from ffl_core.api.cost_correction import correct_serial_cost
		serial = self._testMethodName[:40]
		self._new_serial(serial)
		order = self._bill(serial, 1100)
		self._receive(serial, 600)
		correct_serial_cost(serial, 700, "supplier invoice found")
		self.assertCost(order.name, 700.0)

	def test_a_gun_out_of_stock_before_its_first_receipt_is_a_gap(self):
		"""The gun left stock before the ledger ever took it in: the receipt
		that put it on the shelf is missing (stood in for here by striking the
		first receipt's bundle, as for a gun received before bundles existed).
		The first receipt after the sale is a later stay, not this sale's."""
		serial = self._testMethodName[:40]
		first = self._receive(serial, 500)
		sale = self._sell(serial, 900)
		frappe.db.sql("""update `tabSerial and Batch Bundle` set is_cancelled = 1
			where voucher_type = 'Purchase Receipt' and voucher_no = %s""", first.name)
		self._receive(serial, 650)
		self.assertCost(sale.name, 650.0, missing=True)   # the Serial No's cost, flagged

	def test_a_sale_with_no_posting_time_still_reports(self):
		"""Read at its creation: here after the receipt, so it is that stay's sale."""
		serial = self._testMethodName[:40]
		self._receive(serial, 480)
		sale = self._sell(serial, 900)
		refund = self._return(sale, serial, update_stock=0)
		frappe.db.sql("update `tabSales Invoice` set posting_time = null where name in %s", ((sale.name, refund.name),))
		self.assertCost(sale.name, 480.0)
		self.assertCost(refund.name, -480.0)

	def test_a_transfer_tickets_net_zero_does_not_clear_a_ledger_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0.01)
		ticket = self._pos_sale(serial, 0)
		frappe.db.set_value("POS Invoice", ticket.name, "is_ffl_transfer", 1, update_modified=False)
		self._receive(serial, 800)                        # bought back: the marks are the new stay's
		self.assertCost(ticket.name, 0.01, missing=True)

	def test_a_sale_with_no_posting_time_after_a_buy_back_is_the_new_stays(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		self._sell(serial, 900)
		self._receive(serial, 700)                        # bought back the same day
		second = self._sell(serial, 1000)
		frappe.db.sql("update `tabSales Invoice` set posting_time = null where name = %s", second.name)
		self.assertCost(second.name, 700.0)

	def test_a_gun_found_in_a_count_opens_a_stay(self):
		serial = self._testMethodName[:40]
		self._new_serial(serial)
		self._count(serial, 500)                          # counted in: never received otherwise
		sale = self._sell(serial, 900, warehouse=self.count_room)
		self._receive(serial, 650)
		self.assertCost(sale.name, 500.0)

	def test_a_gun_counted_out_and_back_in_before_hand_over_costs_its_count(self):
		from ffl_core.firearm import issue_serial
		serial = self._testMethodName[:40]
		self._new_serial(serial)
		order = self._bill(serial, 1100)                  # invoiced, the gun still on its way
		self._receive(serial, 600, warehouse=self.count_room)
		self._count(serial)                               # not found
		self._count(serial, 300)                          # found again
		issue_serial(GUN, serial, self.count_room)        # handed over
		self.assertCost(order.name, 300.0)

	def test_the_customers_gun_on_a_net_zero_transfer_with_an_unrecorded_receipt_costs_nothing(self):
		from ffl_core.firearm import issue_serial
		serial = self._testMethodName[:40]
		first = self._receive(serial, 500)
		issue_serial(GUN, serial, self.warehouse)         # handed over
		frappe.db.sql("""update `tabSerial and Batch Bundle` set is_cancelled = 1
			where voucher_type = 'Purchase Receipt' and voucher_no = %s""", first.name)
		ticket = self._transfer_ticket(serial)
		self._receive(serial, 650)
		self.assertCost(ticket, 0.0)

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

	def test_a_confirmed_free_gun_bought_back_leaves_its_old_sale_a_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0, row={"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": "Supplier gift"})
		sale = self._sell(serial, 300)
		self._receive(serial, 450)
		self.assertCost(sale.name, 0.0, missing=True)

	def test_an_old_placeholder_transfer_bought_back_keeps_its_placeholder(self):
		"""A transfer gun that came in at a $10 placeholder before custody existed:
		after the store buys it, the old ticket reads the ledger's $10."""
		serial = self._testMethodName[:40]
		self._receive(serial, 10, is_ffl_transfer=1, ffl_transfer_for_customer=TRANSFEREE,
			ffl_acquisition_type="Transfer")
		ticket = self._sell(serial, 0)
		self._receive(serial, 800)
		self.assertCost(ticket.name, 10.0)

	def test_a_gun_confirmed_free_costs_nothing_and_is_no_gap(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 0, row={"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": "Supplier gift"})
		self.assertEqual(frappe.db.get_value("Serial No", serial, "zero_cost_confirmed"), 1)
		sale = self._sell(serial, 300)
		self.assertCost(sale.name, 0.0)

	# --- a gun sold once, sent back in transit and re-invoiced at $0 --------------------------------

	def test_a_gun_returned_in_transit_and_reissued_at_no_charge_costs_once(self):
		serial = self._testMethodName[:40]
		sale, reissue = self._returned_in_transit_and_reissued(serial)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 0.0)
		self.assertAlmostEqual(self._order_row(reissue.name)["profit"], 0.0)

	def test_a_reissue_matches_an_original_line_spelling_the_serial_in_lower_case(self):
		serial = self._testMethodName[:40].upper()
		sale, reissue = self._returned_in_transit_and_reissued(serial)
		frappe.db.set_value("Sales Invoice Item", sale.items[0].name, "serial_no", serial.lower(), update_modified=False)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "name"), serial)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 0.0)

	def test_a_gun_bought_back_from_its_buyer_and_sold_to_another_costs_the_buy_back(self):
		for label, buy_back in (("PR", lambda s: self._receive(s, 700)),
				("MR", lambda s: self._material_receipt(s, 700))):
			with self.subTest(buy_back=label):
				serial = f"{self._testMethodName[:36]}-{label}"
				self._receive(serial, 500)
				first = self._bill(serial, 900)
				self._issue(serial)
				buy_back(serial)
				resale = self._bill(serial, 1000, customer=OTHER_BUYER)
				self._issue(serial)
				self.assertCost(first.name, 500.0)
				self.assertCost(resale.name, 700.0)

	def test_a_no_charge_invoice_after_a_credit_note_is_costed_as_a_sale(self):
		sale, reissue = self._returned_in_transit_and_reissued(self._testMethodName[:40], credit_note=True)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 500.0)

	def test_a_no_charge_invoice_to_another_customer_is_costed_as_a_sale(self):
		sale, reissue = self._returned_in_transit_and_reissued(self._testMethodName[:40], customer=OTHER_BUYER)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 500.0)

	def test_a_reissue_after_a_counter_sale_costs_nothing(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._pos_sale(serial, 990)
		reissue = self._bill(serial, 0)                    # nothing came back in between
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 0.0)

	def test_a_reissue_of_a_sale_from_an_earlier_period_costs_nothing(self):
		# In the back room: a backdated receipt is refused where other tests' counts stand later.
		serial, room = self._testMethodName[:40], self.back_room
		self._receive(serial, 500, warehouse=room, posting_date=add_days(nowdate(), -2), set_posting_time=1)
		sale = self._invoice([self._gun_row(serial, 900, room)], update_stock=0,
			posting_date=add_days(nowdate(), -1), set_posting_time=1)
		issue = self._issue(serial, room)
		self._material_receipt(serial, 500, remarks=f"Returned in transit, reverses {issue}", warehouse=room)
		reissue = self._invoice([self._gun_row(serial, 0, room)], update_stock=0)
		self._issue(serial, room)
		self.assertFalse(any(r.get("invoice") == sale.name for r in self._run("Order")))
		self.assertCost(reissue.name, 0.0)

	def test_a_receipt_naming_the_sales_disposition_ties_the_reissue(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._bill(serial, 900)
		issue = self._issue(serial)
		disposition = f"SRAS-DISP-{frappe.generate_hash(length=8)}"
		frappe.get_doc({"doctype": "FFL Disposition", "name": disposition, "company": self.company,
			"disposition_type": "FFL Transfer", "item": GUN, "serial": serial, "customer": BUYER,
			"sales_invoice": sale.name, "stock_entry": issue, "docstatus": 1}).db_insert()
		self._material_receipt(serial, 500, remarks=f"Returned in transit ({disposition})")
		reissue = self._bill(serial, 0)
		self._issue(serial)
		self.assertCost(reissue.name, 0.0)

	def test_a_returned_gun_moved_before_its_reissue_still_costs_nothing(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._bill(serial, 900)
		issue = self._issue(serial)
		self._material_receipt(serial, 500, remarks=f"Returned in transit, reverses {issue}")
		self._move(serial, self.warehouse, self.back_room)   # a move is not a receipt
		reissue = self._bill(serial, 0)
		self._issue(serial, self.back_room)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 0.0)

	def test_a_gun_bought_back_then_given_to_its_buyer_at_no_charge_is_costed_from_the_ledger(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		sale = self._bill(serial, 900)
		self._issue(serial)
		self._receive(serial, 700)                        # bought back on a Purchase Receipt
		reissue = self._bill(serial, 0)
		self._issue(serial)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 700.0)

	def test_a_receipt_not_tied_to_the_sales_hand_over_leaves_the_reissue_to_the_ledger(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		self._bill(serial, 900)
		self._issue(serial)
		self._material_receipt(serial, 650, remarks="Trade-in")
		reissue = self._bill(serial, 0)
		self._issue(serial)
		self.assertCost(reissue.name, 650.0)

	def test_a_credit_note_of_a_reissue_costs_nothing(self):
		serial = self._testMethodName[:40]
		sale, reissue = self._returned_in_transit_and_reissued(serial)
		credit = self._return(reissue, serial, update_stock=0)
		self.assertCost(sale.name, 500.0)
		self.assertCost(reissue.name, 0.0)
		self.assertCost(credit.name, 0.0)

	def test_a_priced_sale_to_someone_else_in_between_leaves_the_reissue_to_the_ledger(self):
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		self._bill(serial, 900)
		issue = self._issue(serial)
		self._material_receipt(serial, 500, remarks=f"Returned in transit, reverses {issue}")
		other = self._bill(serial, 1000, customer=OTHER_BUYER)
		issue = self._issue(serial)
		self._material_receipt(serial, 500, remarks=f"Returned in transit, reverses {issue}")
		reissue = self._bill(serial, 0)
		self._issue(serial)
		self.assertCost(other.name, 500.0)
		self.assertCost(reissue.name, 500.0)

	def test_a_counter_pickup_reports_as_before(self):
		"""The pickup ticket is $0 to the order's own customer after a priced sale
		of the same gun: it stays out of the report, and the order keeps its cost."""
		serial = self._testMethodName[:40]
		self._receive(serial, 500)
		order = self._bill(serial, 900)
		ticket = self._free_ticket(serial, BUYER, ffl_counter_order=order.name)
		self._issue(serial)

		def rows():
			return {view: [r for r in self._run(view) if r.get("invoice") in (order.name, ticket)]
				for view in ("Order", "Order Detail", "Product")}

		with patch.object(sr, "_reissued_at_no_charge", return_value=set()):
			before = rows()
		self.assertEqual(rows(), before)
		self.assertFalse(any(r.get("invoice") == ticket for view in before.values() for r in view))
		self.assertCost(order.name, 500.0)

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
