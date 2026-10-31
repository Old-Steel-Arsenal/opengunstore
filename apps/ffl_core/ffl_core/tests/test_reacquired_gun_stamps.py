"""A gun the store sold and later takes in again (bought back, traded in) is a
NEW stay: its receipt rewrites the ownership stamps — pay-on-sale and
consignment — the way it already rewrites needs_service, is_transfer_gun and
the zero-cost trio (compliance/purchase.py).

Before, only the truthy branch was written, so a stamp from the gun's previous
stay rode along: the return of the gun's NEXT sale reversed the pay-on-sale
supplier's payable of the EARLIER sale (pay_on_sale.py reverses on any
return of a settled, stamped gun), and every sale of an is_consignment serial
books a consignor payable (consignment.py has no "already paid" flag).

A sales return is not a receipt — it never reaches purchase.py — so a
returned gun keeps its stamps; nor does a purchase return rewrite them. A rewrite that meets a cost still
parked for a pay-on-sale supplier leaves a trace instead of booking anything.
Whether an earlier sale was a
consignment sale is read from that sale (the commission % frozen on its line),
so the report and the consignment-return block do not change their minds about
it when the gun comes back under another owner.

The events that follow a stay go by what was booked, not by the stamp: a
credit note reverses what the sale it returns settled,
once, and only while the gun is still in that sale's stay; a cancelled
receipt puts back the stay before it, rebuilt from the records.

Real documents end to end: Purchase Receipt → Sales Invoice (Update Stock)
→ Purchase Receipt of the same serial → Sales Invoice → return. The Item is an
antique so the sales skip the 4473 / NICS gates (as SettleInvoice and
PostConsignmentPayableInvoice do); the stamps live on the Serial No either way.
"""
from __future__ import annotations

from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

COMPANY_NAME = "FFL Test"
ITEM = "REACQ-ANTIQUE-GUN"
REGULATED = "REACQ-REGULATED-GUN"   # takes a bound-book acquisition on receipt; never sold here
PAY_ON_SALE_SUPPLIER = "REACQ-PAYONSALE-FFL"
PLAIN_SUPPLIER = "REACQ-PLAIN-FFL"
BUYER = "_Test Reacquired Buyer"
CONSIGNOR = "_Test Reacquired Consignor"


class ReacquiredGun(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import consignment, dev_site, pay_on_sale
		dev_site.run()
		pay_on_sale.ensure()
		consignment.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		if not frappe.db.exists("Supplier Group", "Distributor"):
			frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
				"parent_supplier_group": "All Supplier Groups", "is_group": 0}).insert(ignore_permissions=True)
		# Unique ffl_numbers: the column is UNIQUE across the test DB.
		cls._supplier(PAY_ON_SALE_SUPPLIER, ffl_number="1-88-888-88-8X-88893", pay_on_sale=1)
		cls._supplier(PLAIN_SUPPLIER, ffl_number="1-88-888-88-8X-88894", pay_on_sale=0)
		for code in (ITEM, REGULATED):
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Pistol",
					"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", ITEM, "is_antique", 1, update_modified=False)
		for name in (BUYER, CONSIGNOR):
			if not frappe.db.exists("Customer", name):
				frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_group": "Individual",
					"territory": "All Territories"}).insert(ignore_permissions=True)

	@staticmethod
	def _supplier(name, *, ffl_number, pay_on_sale):
		fields = dict(is_ffl_dealer=1, ffl_number=ffl_number, ez_check_status="Verified", pay_on_sale=pay_on_sale)
		if frappe.db.exists("Supplier", name):
			frappe.db.set_value("Supplier", name, fields, update_modified=False)
			return
		frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": "Distributor",
			**fields}).insert(ignore_permissions=True)

	@staticmethod
	def _report_cost(si, serial):
		from ffl_core.ffl_core.report.sales_report import sales_report
		row = si.items[0]
		line = frappe._dict(parent=si.name, item_code=ITEM, qty=row.qty, serial_no=serial, posting_date=si.posting_date,
			base_net_amount=row.base_net_amount, consignment_commission_pct=row.get("consignment_commission_pct"))
		lines = {si.name: [line]}
		serial_cost, consignment, _ = sales_report._fetch_serial_facts(lines)
		cost, missing = sales_report._line_cost(line, sales_report._fetch_item_meta(lines),
			serial_cost, consignment)
		return round(cost, 2), missing, bool(sales_report._consigned(line, [serial], consignment))

	def _receive(self, supplier, serial, rate, item=ITEM, **extra):
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt", "supplier": supplier, "company": self.company,
			# price_list_rate pinned to the rate: ERPNext fills an empty one from
			# the item's last purchase and prices a $0 row at it, so a consignor's
			# $0 line would silently cost what the last trade-in of the item did.
			"items": [{"item_code": item, "qty": 1, "warehouse": self.warehouse, "serial_no": serial, "rate": rate,
				"price_list_rate": rate}],
			**extra,
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		self.assertAlmostEqual(flt(frappe.db.get_value("Purchase Receipt Item", pr.items[0].name, "rate")), flt(rate))
		s = self._stamps(serial)
		self.assertFalse(int(s.pay_on_sale or 0) and int(s.is_consignment or 0), f"{serial}: owned XOR consigned")
		return pr

	def _trade_in(self, serial, rate):
		"""The buyer brings the gun back: an Individual-source receipt (the
		trade-in shape; its seller is recorded on the acquisition, the PR's
		supplier is only a placeholder)."""
		return self._receive(PLAIN_SUPPLIER, serial, rate, ffl_acquisition_source="Individual",
			ffl_seller_customer=BUYER, ffl_seller_id_type="Driver License", ffl_seller_id_number=f"DL-TI-{serial}")

	def _receive_on_consignment(self, serial):
		return self._receive(PAY_ON_SALE_SUPPLIER, serial, 0, ffl_acquisition_type="Consignment",
			ffl_acquisition_source="Individual", ffl_seller_customer=CONSIGNOR,
			ffl_seller_id_type="Driver License", ffl_seller_id_number=f"DL-{serial}")

	def _sell(self, serial, net, **line):
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = BUYER
		si.update_stock = 1
		si.set_warehouse = self.warehouse
		si.append("items", {"item_code": ITEM, "qty": 1, "rate": net, "warehouse": self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1, **line})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def _return(self, si, serial, update_stock=1):
		ret = frappe.new_doc("Sales Invoice")
		ret.company = self.company
		ret.customer = si.customer
		ret.is_return = 1
		ret.return_against = si.name
		ret.update_stock = update_stock
		ret.set_warehouse = self.warehouse
		ret.append("items", {"item_code": ITEM, "qty": -1, "rate": si.items[0].rate, "warehouse": self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1})
		ret.set_missing_values()
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		return ret

	@staticmethod
	def _stamps(serial):
		return frappe.db.get_value("Serial No", serial, ["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost",
			"pay_on_sale_settled", "is_consignment", "consignor", "consignment_commission_pct"], as_dict=True)

	@staticmethod
	def _settlement(source):
		return frappe.db.get_value("Journal Entry", {"ffl_pay_on_sale_source": source, "docstatus": 1})

	@staticmethod
	def _owed(party):
		"""What the store owes a supplier-party across every account (credit − debit)."""
		return flt(frappe.db.sql("""select sum(credit - debit) from `tabGL Entry`
			where party_type = 'Supplier' and party = %s and is_cancelled = 0""", party)[0][0])

	@staticmethod
	def _cancel(doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.flags.ignore_permissions = True
		doc.cancel()
		return doc

	def _traced(self, serial, title, *parts):
		return frappe.db.exists("Comment", {"reference_doctype": "Serial No", "reference_name": serial,
			"comment_type": "Info", "content": ("like", "%".join((title, *parts)) + "%")})

	def assertUnstamped(self, serial):
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale or 0), s.pay_on_sale_supplier or None, flt(s.srbnb_cost),
			int(s.pay_on_sale_settled or 0)), (0, None, 0.0, 0), f"{serial}: pay-on-sale stamp left over")
		self.assertEqual((int(s.is_consignment or 0), s.consignor or None, flt(s.consignment_commission_pct)),
			(0, None, 0.0), f"{serial}: consignment stamp left over")

	def test_a_gun_traded_back_in_no_longer_answers_to_its_old_supplier(self):
		"""Received from the pay-on-sale supplier, sold, traded back
		in, sold again, returned with the gun — the supplier is owed the first
		sale's cost once, and nothing after."""
		serial = self._testMethodName[:40]
		owed = self._owed(PAY_ON_SALE_SUPPLIER)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		first = self._sell(serial, 900)
		settled = self._settlement(first.name)
		self.assertTrue(settled)                                # the supplier is owed 400 for that sale
		self._trade_in(serial, 300)                             # a new stay: the store's own gun
		self.assertUnstamped(serial)
		second = self._sell(serial, 800)
		self.assertFalse(self._settlement(second.name))         # nothing owed for this sale
		ret = self._return(second, serial)
		self.assertFalse(self._settlement(ret.name))            # its return reverses nothing …
		self.assertEqual(frappe.db.get_value("Journal Entry", settled, "docstatus"), 1)   # … the old payable stands
		self.assertAlmostEqual(self._owed(PAY_ON_SALE_SUPPLIER) - owed, 400.0)

	def test_a_gun_bought_back_from_another_dealer_is_the_stores_own(self):
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		self._sell(serial, 900)
		self._receive(PLAIN_SUPPLIER, serial, 300)
		self.assertUnstamped(serial)

	def test_a_gun_bought_back_from_its_pay_on_sale_supplier_is_stamped_afresh(self):
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		self._sell(serial, 900)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 350)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), s.pay_on_sale_supplier, flt(s.srbnb_cost), int(s.pay_on_sale_settled)),
			(1, PAY_ON_SALE_SUPPLIER, 350.0, 0))
		second = self._sell(serial, 700)
		je = frappe.get_doc("Journal Entry", self._settlement(second.name))
		self.assertAlmostEqual(je.total_debit, 350.0)           # this stay's parked cost, not the old 400

	def test_a_consigned_gun_traded_back_in_is_no_longer_the_consignors(self):
		"""The consignor is owed the first sale's share once; the
		second sale books no consignment payable and costs the trade-in rate."""
		from ffl_core.ffl_core.report.sales_report import sales_report
		serial = self._testMethodName[:40]
		self._receive_on_consignment(serial)
		consignor = self._stamps(serial).consignor
		self.assertTrue(consignor)
		owed = self._owed(consignor)
		first = self._sell(serial, 1000)
		self.assertTrue(frappe.db.exists("Journal Entry", {"ffl_consignment_source": first.name, "docstatus": 1}))
		share = self._owed(consignor) - owed
		self.assertGreater(share, 0)
		self._trade_in(serial, 500)
		self.assertUnstamped(serial)
		second = self._sell(serial, 900)
		self.assertFalse(frappe.db.exists("Journal Entry", {"ffl_consignment_source": second.name}))
		self.assertAlmostEqual(self._owed(consignor) - owed, share)
		self.assertEqual(self._report_cost(second, serial), (500.0, False, False))
		# The FIRST sale, run again after the trade-in, is still a consignment
		# sale costed at the consignor's share — not at the trade-in price.
		self.assertEqual(self._report_cost(first, serial), (round(share, 2), False, True))

	def test_a_consignment_sale_stays_unreturnable_after_the_gun_comes_back(self):
		"""The rule (cancel, never return, a consignment sale) is
		read from the sale; a trade-in clearing the stamp must not lift it."""
		serial = self._testMethodName[:40]
		self._receive_on_consignment(serial)
		first = self._sell(serial, 1000)
		self._trade_in(serial, 500)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._return(first, serial.lower(), update_stock=0)   # a hand-built note, another case
		self.assertIn("Consignment sales cannot be returned", str(ctx.exception))

	def test_an_owned_sale_is_still_the_stores_after_the_gun_comes_back_on_consignment(self):
		"""The other side of the same rule: a sale that was the
		store's own does not turn into a consignment sale — in the report, or for
		the return block — because the gun later came back on consignment."""
		serial = self._testMethodName[:40]
		self._receive(PLAIN_SUPPLIER, serial, 300)
		sale = self._sell(serial, 900)
		self._receive_on_consignment(serial)
		self.assertEqual(int(self._stamps(serial).is_consignment), 1)
		self.assertEqual(self._report_cost(sale, serial), (300.0, False, False))
		self._return(sale, serial, update_stock=0)             # a refund on the store's own sale goes through

	def test_a_commission_on_a_line_whose_gun_is_not_consigned_is_not_kept(self):
		"""The line's % is what says "consignment sale" later; a stray one on an
		owned gun's line would say it falsely."""
		serial = self._testMethodName[:40]
		self._receive(PLAIN_SUPPLIER, serial, 300)
		sale = self._sell(serial, 900, consignment_commission_pct=15)
		self.assertEqual(flt(sale.items[0].consignment_commission_pct), 0.0)

	def test_a_sale_older_than_the_commission_freeze_reads_the_stamp(self):
		"""No line % before COMMISSION_FROZEN_SINCE: the consignment backfill stamped
		the guns sold before 2026-07-01, and the first consigned lines after it froze
		a 0 (a live store's last, of 2026-07-15, booked a consignor payable). An old sale
		still reads the stamp; a newer one never does."""
		from ffl_core.ffl_core.report.sales_report import sales_report
		stamped = {"S-OLD": {"is_consignment": 1, "commission_pct": 20.0}}
		for posting_date in ("2026-06-15", "2026-07-15"):
			old = frappe._dict(consignment_commission_pct=0, posting_date=posting_date)
			self.assertTrue(sales_report._consigned(old, ["S-OLD"], stamped), posting_date)
		new = frappe._dict(consignment_commission_pct=0, posting_date="2026-07-16")
		self.assertIsNone(sales_report._consigned(new, ["S-OLD"], stamped))
		self.assertTrue(sales_report._consigned(frappe._dict(consignment_commission_pct=15, posting_date="2026-07-16"),
			["S-NEW"], {}))

	def test_a_consignment_receipt_takes_the_pay_on_sale_stamp_off_and_vice_versa(self):
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		self._sell(serial, 900)
		self._receive_on_consignment(serial)
		s = self._stamps(serial)
		self.assertEqual((int(s.is_consignment), int(s.pay_on_sale or 0), s.pay_on_sale_supplier or None), (1, 0, None))
		self._sell(serial, 900)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 380)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), int(s.is_consignment or 0), s.consignor or None), (1, 0, None))

	def test_a_purchase_return_leaves_the_stamp_alone(self):
		"""Sending a gun back to its supplier is a disposition, not an acquisition."""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		serial = self._testMethodName[:40]
		pr = self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		# A cost correction moved the parked cost and the cost together; a
		# return that re-stamped the gun would put the receipt's 400 back.
		frappe.db.set_value("Serial No", serial, {"srbnb_cost": 420, "purchase_rate": 420}, update_modified=False)
		back = make_return_doc("Purchase Receipt", pr.name)
		back.flags.ignore_permissions = True
		back.insert()
		back.submit()
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), s.pay_on_sale_supplier, flt(s.srbnb_cost), int(s.pay_on_sale_settled)),
			(1, PAY_ON_SALE_SUPPLIER, 420.0, 0))
		self.assertAlmostEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 420.0)

	def test_a_cost_still_parked_is_traced_not_booked(self):
		"""Sold, then credited WITHOUT the gun coming back — the
		payable is reversed and the cost re-parks (settled=0) — and the buyer
		later trades the gun in. The receipt goes through, books nothing, and
		leaves a Comment and an Error Log a bookkeeper finds by its title."""
		from ffl_core.compliance.purchase import PARKED_COST_LEFT
		serial = self._testMethodName[:40]
		# Error Log rows outlive a test's rollback: count only this run's.
		started = frappe.utils.now_datetime()
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		self._return(sale, serial, update_stock=0)              # money back, the gun stays with the buyer
		self.assertEqual(int(self._stamps(serial).pay_on_sale_settled), 0)
		journals = frappe.db.count("Journal Entry")
		pr = self._trade_in(serial, 300)
		self.assertUnstamped(serial)
		self.assertEqual(frappe.db.count("Journal Entry"), journals)
		logs = frappe.get_all("Error Log", filters={"method": PARKED_COST_LEFT, "reference_name": serial,
			"creation": (">=", started)}, pluck="error")
		self.assertEqual(len(logs), 1)
		for part in (serial, PAY_ON_SALE_SUPPLIER, pr.name, "400"):
			self.assertIn(part, logs[0])
		# The durable copy the month-end checklist works from (the Error Log is
		# cleared after 14 days): an Info comment carrying the title.
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Serial No", "reference_name": serial,
			"comment_type": "Info", "content": ("like", f"{PARKED_COST_LEFT}%{pr.name}%")}))

	@staticmethod
	def _left_as_before_cancels_restored(serial):
		"""A receipt cancelled before the put-back logic existed left its own stamp on the gun — the case
		the amendment carve-out is for (prod has such receipts)."""
		frappe.db.set_value("Serial No", serial, {"pay_on_sale": 1, "pay_on_sale_supplier": PAY_ON_SALE_SUPPLIER,
			"srbnb_cost": 400, "pay_on_sale_settled": 0}, update_modified=False)

	def test_an_amendment_restamping_its_own_predecessor_is_not_traced(self):
		from ffl_core.compliance.purchase import PARKED_COST_LEFT
		serial = self._testMethodName[:40]
		started = frappe.utils.now_datetime()
		pr = frappe.get_doc("Purchase Receipt", self._receive(PAY_ON_SALE_SUPPLIER, serial, 400).name)
		pr.flags.ignore_permissions = True
		pr.cancel()
		self._left_as_before_cancels_restored(serial)
		amended = frappe.copy_doc(pr)
		amended.amended_from = pr.name
		for d in [amended, *amended.get_all_children()]:
			d.docstatus = 0
		amended.items[0].rate = 410
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		self.assertFalse(frappe.db.exists("Error Log", {"method": PARKED_COST_LEFT, "reference_name": serial,
			"creation": (">=", started)}))
		self.assertAlmostEqual(flt(self._stamps(serial).srbnb_cost), 410.0)

	def test_an_amendment_finds_its_predecessors_guns_through_their_cancelled_bundles(self):
		"""A row built with the serial picker keeps no serial text, and a cancel
		unlinks its bundle: the cancelled bundle still names the
		guns — an antique's too, which has no bound-book acquisition to ask."""
		from ffl_core.compliance.purchase import PARKED_COST_LEFT
		serial = self._testMethodName[:40]
		started = frappe.utils.now_datetime()
		pr = frappe.get_doc("Purchase Receipt", self._receive(PAY_ON_SALE_SUPPLIER, serial, 400).name)
		pr.flags.ignore_permissions = True
		pr.cancel()
		self._left_as_before_cancels_restored(serial)
		frappe.db.set_value("Purchase Receipt Item", pr.items[0].name, "serial_no", None, update_modified=False)
		amended = frappe.copy_doc(pr)
		amended.amended_from = pr.name
		for d in [amended, *amended.get_all_children()]:
			d.docstatus = 0
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		self.assertFalse(frappe.db.exists("Error Log", {"method": PARKED_COST_LEFT, "reference_name": serial,
			"creation": (">=", started)}))

	def test_a_returned_gun_keeps_its_stamp(self):
		"""The other door back in: a return is not a receipt."""
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		self._return(sale, serial)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), s.pay_on_sale_supplier, flt(s.srbnb_cost), int(s.pay_on_sale_settled)),
			(1, PAY_ON_SALE_SUPPLIER, 400.0, 0))

	# --- A credit note reverses what its sale booked ------------------------

	def test_a_refund_on_a_sale_before_the_gun_came_back_reverses_nothing(self):
		"""Sold (stay 1), received again from the same supplier and sold
		(stay 2), then a refund on the FIRST sale. Money only: stay 1's payable
		stands, stay 2 stays settled, and a trace says why nothing was reversed."""
		from ffl_core.compliance.pay_on_sale import PAYABLE_KEPT
		serial = self._testMethodName[:40]
		owed = self._owed(PAY_ON_SALE_SUPPLIER)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		first = self._sell(serial, 900)
		again = self._receive(PAY_ON_SALE_SUPPLIER, serial, 350)
		self._sell(serial, 800)
		note = self._return(first, serial, update_stock=0)
		self.assertFalse(self._settlement(note.name))
		self.assertAlmostEqual(self._owed(PAY_ON_SALE_SUPPLIER) - owed, 750.0)
		self.assertEqual(int(self._stamps(serial).pay_on_sale_settled), 1)
		self.assertTrue(self._traced(serial, PAYABLE_KEPT, note.name, again.name))

	def test_a_refund_reverses_what_the_sale_booked(self):
		serial = self._testMethodName[:40]
		owed = self._owed(PAY_ON_SALE_SUPPLIER)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		# The stamp says something else now; the sale's settlement row is the record.
		frappe.db.set_value("Serial No", serial, "srbnb_cost", 420, update_modified=False)
		note = self._return(sale, serial, update_stock=0)
		self.assertAlmostEqual(frappe.get_doc("Journal Entry", self._settlement(note.name)).total_debit, 400.0)
		self.assertAlmostEqual(self._owed(PAY_ON_SALE_SUPPLIER) - owed, 0.0)
		self.assertEqual(int(self._stamps(serial).pay_on_sale_settled), 0)

	def test_a_sale_is_reversed_once_whatever_the_credit_notes(self):
		"""Another credit note on the same sale finds the gun reversed."""
		from ffl_core.compliance.pay_on_sale import settle_pay_on_sale_on_submit
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		self._return(sale, serial, update_stock=0)
		frappe.db.set_value("Serial No", serial, "pay_on_sale_settled", 1, update_modified=False)   # the stamp cannot gate it
		second = SimpleNamespace(doctype="Sales Invoice", name=f"{serial}-CN2", company=self.company,
			posting_date=frappe.utils.nowdate(), is_return=1, return_against=sale.name,
			items=[frappe._dict(idx=1, item_code=ITEM, serial_no=serial)])
		settle_pay_on_sale_on_submit(second)
		self.assertFalse(self._settlement(second.name))

	def test_cancelling_a_refund_after_the_gun_came_back_leaves_the_new_stay_alone(self):
		"""A refund that took no gun back can be cancelled after the gun came in
		again: the unsettled flag is the new stay's, and its sale must settle."""
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		note = self._return(sale, serial, update_stock=0)          # the 400 re-parks
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 350)            # a new stay, unsold
		self._cancel("Sales Invoice", note.name)
		self.assertEqual(int(self._stamps(serial).pay_on_sale_settled), 0)
		second = self._sell(serial, 800)
		self.assertAlmostEqual(frappe.get_doc("Journal Entry", self._settlement(second.name)).total_debit, 350.0)

	# --- A cancelled receipt puts back the stay before it -------------------

	def test_cancelling_a_mistaken_receipt_puts_the_stay_before_it_back(self):
		"""Sold and settled, then a trade-in keyed in by mistake clears
		the stamp; its cancel puts the pay-on-sale stay back — settled, since its
		settlement stands — and a refund on the sale finds its stay again."""
		from ffl_core.compliance.purchase import STAMPS_PUT_BACK
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		mistake = self._trade_in(serial, 300)
		self.assertUnstamped(serial)
		self._cancel("Purchase Receipt", mistake.name)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), s.pay_on_sale_supplier, flt(s.srbnb_cost), int(s.pay_on_sale_settled)),
			(1, PAY_ON_SALE_SUPPLIER, 400.0, 1))
		self.assertTrue(self._traced(serial, STAMPS_PUT_BACK, mistake.name))
		note = self._return(sale, serial, update_stock=0)
		self.assertAlmostEqual(frappe.get_doc("Journal Entry", self._settlement(note.name)).total_debit, 400.0)

	def test_a_stay_put_back_is_unsettled_when_its_sale_was_refunded(self):
		"""The latest pay-on-sale entry since that stay decides: here the refund."""
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		self._return(sale, serial, update_stock=0)
		mistake = self._trade_in(serial, 300)
		self._cancel("Purchase Receipt", mistake.name)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), flt(s.srbnb_cost), int(s.pay_on_sale_settled)), (1, 400.0, 0))

	def test_a_stay_put_back_ignores_what_an_earlier_stay_booked(self):
		"""Stay 1 sold and settled; stay 2 went back to its supplier unsold (a
		purchase return rewrites nothing); a receipt after that is cancelled.
		Stay 2 comes back unsettled — stay 1's settlement is not its."""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		self._sell(serial, 900)
		stay2 = self._receive(PAY_ON_SALE_SUPPLIER, serial, 350)
		back = make_return_doc("Purchase Receipt", stay2.name)
		back.flags.ignore_permissions = True
		back.insert()
		back.submit()
		mistake = self._receive(PLAIN_SUPPLIER, serial, 300)
		self._cancel("Purchase Receipt", mistake.name)
		s = self._stamps(serial)
		self.assertEqual((int(s.pay_on_sale), flt(s.srbnb_cost), int(s.pay_on_sale_settled)), (1, 350.0, 0))

	def test_cancelling_a_receipt_puts_a_consignment_back(self):
		serial = self._testMethodName[:40]
		self._receive_on_consignment(serial)
		before = self._stamps(serial)
		self._sell(serial, 1000)
		mistake = self._trade_in(serial, 500)
		self._cancel("Purchase Receipt", mistake.name)
		s = self._stamps(serial)
		self.assertEqual((int(s.is_consignment), s.consignor, flt(s.consignment_commission_pct), int(s.pay_on_sale or 0)),
			(1, before.consignor, flt(before.consignment_commission_pct), 0))

	def test_cancelling_the_only_receipt_leaves_the_gun_unstamped(self):
		"""No stay before it: the cancelled receipt's pay-on-sale stamp does not
		linger to be read later as a parked cost (it was month-end rule ④)."""
		serial = self._testMethodName[:40]
		pr = self._receive(PAY_ON_SALE_SUPPLIER, serial, 400, item=REGULATED)
		self._cancel("Purchase Receipt", pr.name)
		self.assertUnstamped(serial)
		self.assertAlmostEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 0.0)   # r2 #1
		# the other on_cancel hook still ran: the bound-book entry is withdrawn
		self.assertEqual(frappe.db.get_value("FFL Acquisition", {"purchase_receipt": pr.name}, "docstatus"), 2)

	# --- Reversal and put-back edge cases -----------------------------------------

	def test_a_stay_whose_stamp_says_reversed_is_not_reversed_again(self):
		"""A note the sale's rows cannot see (one against its consolidated
		invoice) already reversed the stay, and the stay's own stamp says so — a
		second note must not take the supplier's payable twice."""
		serial = self._testMethodName[:40]
		owed = self._owed(PAY_ON_SALE_SUPPLIER)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		frappe.db.set_value("Serial No", serial, "pay_on_sale_settled", 0, update_modified=False)
		note = self._return(sale, serial, update_stock=0)
		self.assertFalse(self._settlement(note.name))
		self.assertAlmostEqual(self._owed(PAY_ON_SALE_SUPPLIER) - owed, 400.0)

	def test_a_stay_put_back_keeps_its_corrected_cost(self):
		"""A cost correction moved the parked cost to 450 before the sale;
		the stay comes back at what its settlement booked, not the receipt's 400."""
		from ffl_core.api.cost_correction import correct_serial_cost
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		correct_serial_cost(serial, 450, "test: the supplier re-invoiced")
		self._sell(serial, 900)
		mistake = self._trade_in(serial, 300)
		self._cancel("Purchase Receipt", mistake.name)
		s = self._stamps(serial)
		self.assertEqual((flt(s.srbnb_cost), int(s.pay_on_sale_settled)), (450.0, 1))
		self.assertAlmostEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 450.0)

	def test_a_consignment_put_back_answers_to_the_consignor_its_sale_paid(self):
		"""The consignor is whom the stay's sale booked the payable to —
		a renamed seller does not make a cancel create another Supplier."""
		serial = self._testMethodName[:40]
		self._receive_on_consignment(serial)
		consignor = self._stamps(serial).consignor
		self._sell(serial, 1000)
		frappe.db.set_value("Customer", CONSIGNOR, "customer_name", "Renamed Consignor", update_modified=False)
		self.addCleanup(frappe.db.set_value, "Customer", CONSIGNOR, "customer_name", CONSIGNOR,
			update_modified=False)
		suppliers = frappe.db.count("Supplier")
		mistake = self._trade_in(serial, 500)
		self._cancel("Purchase Receipt", mistake.name)
		self.assertEqual(self._stamps(serial).consignor, consignor)
		self.assertEqual(frappe.db.count("Supplier"), suppliers)

	def test_a_stay_put_back_takes_its_cost_and_custody_mark_back(self):
		"""How the stay was acquired comes back with its owner — a gun keyed
		in by mistake as a customer's, held for transfer at a $10 placeholder, is
		the store's again at its cost once that receipt is cancelled."""
		serial = self._testMethodName[:40]
		self._receive(PLAIN_SUPPLIER, serial, 300)
		self._sell(serial, 900)
		mistake = self._receive(PLAIN_SUPPLIER, serial, 10, is_ffl_transfer=1)
		self.assertEqual(int(frappe.db.get_value("Serial No", serial, "is_transfer_gun")), 1)
		self._cancel("Purchase Receipt", mistake.name)
		transfer, cost = frappe.db.get_value("Serial No", serial, ["is_transfer_gun", "purchase_rate"])
		self.assertEqual((int(transfer), flt(cost)), (0, 300.0))

	def test_a_refund_on_an_older_settlement_still_leaves_the_trace(self):
		"""A settlement from before its rows named their guns — nothing is
		reversed after the gun came back, and the month-end search still finds it."""
		from ffl_core.compliance.pay_on_sale import PAYABLE_KEPT
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		first = self._sell(serial, 900)
		for row in frappe.get_all("Journal Entry Account", filters={"parent": self._settlement(first.name)},
				pluck="name"):
			frappe.db.set_value("Journal Entry Account", row, "user_remark", None, update_modified=False)
		again = self._trade_in(serial, 300)
		note = self._return(first, serial, update_stock=0)
		self.assertFalse(self._settlement(note.name))
		self.assertTrue(self._traced(serial, PAYABLE_KEPT, note.name, again.name))

	def test_a_stay_that_cannot_be_rebuilt_stops_the_cancel(self):
		"""An error in the rebuild is never swallowed. A database error has
		already rolled the whole cancel back by then, and a receipt that reads as
		cancelled while it stands is worse than one that stays."""
		from unittest.mock import patch
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		self._sell(serial, 900)
		mistake = self._trade_in(serial, 300)
		with patch("ffl_core.compliance.purchase._put_stay_back", side_effect=RuntimeError("boom")), \
				self.assertRaises(RuntimeError):
			self._cancel("Purchase Receipt", mistake.name)

	# --- More edge cases ----------------------------------------------------------

	def test_a_stay_put_back_behind_a_consignors_line_keeps_the_cost_before_it(self):
		"""A consignor's $0 line prices nothing and leaves the gun the cost
		of the stay before (the Sales Report reads that stay's sale off it). Put
		back after a mistaken trade-in, the gun carries that cost again, not the
		trade-in's."""
		serial = self._testMethodName[:40]
		self._receive(PLAIN_SUPPLIER, serial, 300)
		owned = self._sell(serial, 900)
		self._receive_on_consignment(serial)
		self._sell(serial, 1000)
		mistake = self._trade_in(serial, 500)
		self.assertAlmostEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 500.0)
		self._cancel("Purchase Receipt", mistake.name)
		self.assertAlmostEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)
		self.assertEqual(self._report_cost(owned, serial), (300.0, False, False))

	def test_a_credit_notes_own_reversal_counts_when_its_hook_runs_again(self):
		"""A note whose reversal did not flip the stamp (no longer the
		stay's) still finds its own rows when its hook fires again."""
		from ffl_core.compliance.pay_on_sale import settle_pay_on_sale_on_submit
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		sale = self._sell(serial, 900)
		frappe.db.set_value("Serial No", serial, "pay_on_sale_supplier", PLAIN_SUPPLIER, update_modified=False)
		note = self._return(sale, serial, update_stock=0)
		self.assertTrue(self._settlement(note.name))
		settle_pay_on_sale_on_submit(frappe.get_doc("Sales Invoice", note.name))
		self.assertEqual(frappe.db.count("Journal Entry", {"ffl_pay_on_sale_source": note.name, "docstatus": 1}), 1)

	def test_a_refund_with_nothing_booked_leaves_another_stay_alone_unread(self):
		"""Nothing booked to the sale and the gun received since — nothing
		to reverse, and the other stay's stamp (a broken one here) is not read,
		so it cannot block the note."""
		from ffl_core.compliance.pay_on_sale import _posted_at, _reversal_pair
		serial = self._testMethodName[:40]
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 400)
		first = self._sell(serial, 900)
		self._receive(PAY_ON_SALE_SUPPLIER, serial, 350)
		frappe.db.set_value("Serial No", serial, {"pay_on_sale_supplier": "no-such-supplier",
			"pay_on_sale_settled": 1}, update_modified=False)
		sale = frappe._dict(name=first.name, at=_posted_at("Sales Invoice", first.name), serials={}, unnamed=[],
			reversed=set())
		note = SimpleNamespace(doctype="Sales Invoice", name=f"{serial}-CN", return_against=first.name)
		self.assertIsNone(_reversal_pair(note, serial, sale))
