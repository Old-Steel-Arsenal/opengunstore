"""Sales Summary on real submitted documents: the worked examples of its definitions, to the cent.

Register tickets are made on the Store A-like stand-in of test_cash_drawer (8.25%, its own POS
profile, a cashier with an open shift; trade-ins booked the way the counter's checkout books them,
via NegFixtures). The excise store is test_sales_report_excise's stand-in. Each test rolls back to
its own savepoint, so a report run sees that test's documents only — the stand-in companies have
no other sales.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.ffl_core.report.sales_summary import sales_summary as ss
from ffl_core.setup.gunsmith import DEPOSIT_ITEM
from ffl_core.tests.test_cash_drawer import _store_tests, _user
from ffl_core.tests.test_negative_trade_in import NegFixtures
from ffl_core.tests.test_sales_report_excise import GUN, SERVICE, ExciseStoreFixtures

GOODS = "_TEST-SS-ACCESSORY"
MEMBER = "_TEST-SS-MEMBERSHIP"
CARD = "Credit Card"


def _run(company, view="Summary", day=None):
	day = day or nowdate()
	return ss.execute({"company": company, "from_date": day, "to_date": day, "view": view})


def _summary(company, day=None):
	"""{line label: (amount, count)} of the Summary view."""
	_c, rows, *_ = _run(company, "Summary", day)
	return {r["line"]: (r["amount"], r.get("count")) for r in rows if not r.get("is_section") and not r.get("is_note")}


def _amount(summary, label):
	return flt(summary[label][0], 2)


class SummaryFixtures(NegFixtures):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		group = "Accessory" if frappe.db.exists("Item Group", "Accessory") else "All Item Groups"
		if not frappe.db.exists("Item", GOODS):
			frappe.get_doc({"doctype": "Item", "item_code": GOODS, "item_name": GOODS, "item_group": group,
				"stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
		cls.goods_group = group
		# The stand-in's template is its default, as a store's is: the reports read sales-tax accounts from it.
		frappe.db.set_value("Sales Taxes and Charges Template", cls.template, "is_default", 1)
		mop = frappe.get_doc("Mode of Payment", CARD)
		if not any(r.company == cls.company for r in mop.accounts):
			mop.append("accounts", {"company": cls.company, "default_account": cls.bank})
			mop.save(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", cls.neg_profile)
		if not any(p.mode_of_payment == CARD for p in profile.payments):
			profile.append("payments", {"mode_of_payment": CARD})
			profile.save(ignore_permissions=True)
		# A gunsmith deposit is untaxed here, as it is at a store: the stand-in's 0% template on the item.
		from ffl_core.setup import customer_deposits, gunsmith as gunsmith_setup
		gunsmith_setup.ensure()
		customer_deposits.ensure()
		dep = frappe.get_doc("Item", DEPOSIT_ITEM)
		exempt_itt = frappe.db.get_value("Item Tax", {"parent": cls.exempt_item}, "item_tax_template")
		if not any(t.item_tax_template == exempt_itt for t in dep.taxes):
			dep.append("taxes", {"item_tax_template": exempt_itt})
			dep.save(ignore_permissions=True)

	def _sale(self, lines, payments, *, discount=None, submit=True, user=None):
		"""A register ticket by the cashier: lines (code, rate[, list rate]), payments (method, amount)."""
		frappe.set_user(user or self.neg_cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.neg_profile
			inv.customer = "_Test CD Customer"
			for line in lines:
				inv.append("items", {"item_code": line[0], "qty": 1})
			inv.set_missing_values()
			for row, line in zip(inv.items, lines):
				row.price_list_rate = line[2] if len(line) > 2 else line[1]
				row.rate = line[1]
			if discount:
				inv.apply_discount_on = "Grand Total"
				inv.discount_amount = discount
			inv.set("payments", [])
			for method, amount in payments:
				inv.append("payments", {"mode_of_payment": method, "amount": amount})
			if any(m == CARD for m, _a in payments):
				inv.card_unique_reference = "TEST-APPROVAL"  # a standalone terminal's approval code
			inv.flags.ignore_permissions = True
			inv.save()
			if submit:
				inv.submit()
			return inv
		finally:
			frappe.set_user("Administrator")

	def _total(self, inv):
		return flt(inv.rounded_total) or flt(inv.grand_total)

	def _paid_sale(self, lines, **kw):
		"""A ticket paid in cash to the cent."""
		draft = self._sale(lines, [("Cash", 0)], submit=False, **kw)
		draft.payments[0].amount = self._total(draft)
		draft.flags.ignore_permissions = True
		frappe.set_user(self.neg_cashier)
		try:
			draft.save()
			draft.submit()
		finally:
			frappe.set_user("Administrator")
		return draft

	def _si(self, lines, *, submit=True, **fields):
		si = frappe.get_doc({"doctype": "Sales Invoice", "company": self.company, "customer": "_Test CD Customer",
			"taxes_and_charges": self.template, "items": [dict(item_code=c, qty=1, rate=r, price_list_rate=r, **extra)
				for c, r, *rest in lines for extra in [rest[0] if rest else {}]], **fields})
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", self.template))
		si.insert(ignore_permissions=True)
		if submit:
			si.submit()
		return si

	def _checks_clear(self, s):
		self.assertEqual(_amount(s, "E1: tickets less sales and charges"), 0)
		self.assertEqual(_amount(s, "E2: tickets less payments"), 0)

	def _views_agree(self, s):
		"""The three grouped views' Total rows equal the Summary."""
		_c, cats, *_ = _run(self.company, "Category")
		cat_total = next(r for r in cats if r.get("is_total"))
		self.assertEqual((flt(cat_total["gross"], 2), flt(cat_total["discounts"], 2), flt(cat_total["returns"], 2),
			flt(cat_total["net_sales"], 2)), (_amount(s, "Gross sales"), _amount(s, "Recorded discounts"),
			_amount(s, "Returns"), _amount(s, "Net sales")))
		for view in ("Register", "Employee"):
			cols, rows, *_ = _run(self.company, view)
			total = next(r for r in rows if r.get("is_total"))
			self.assertEqual((flt(total["net_sales"], 2), flt(total["gross"], 2), flt(total["total"], 2)),
				(_amount(s, "Net sales"), _amount(s, "Gross sales"), _amount(s, "Total of tickets")), view)
			pay = sum(flt(total[c["fieldname"]]) for c in cols if c["fieldname"].startswith("pay_"))
			self.assertEqual(flt(pay, 2), _amount(s, "Total"), view)
			self.assertEqual(sum(r["orders"] for r in rows if not r.get("is_total")), total["orders"])


class Cases:
	def test_a_discount_and_a_return(self):
		"""List 500 sold at 450, and a -100 return of an earlier sale: Gross 500 / Disc 50 / Returns 100 / Net 350."""
		self._paid_sale([(GOODS, 450, 500)])
		earlier = self._paid_sale([(GOODS, 100)])
		frappe.db.set_value("POS Invoice", earlier.name, "posting_date", add_days(nowdate(), -1))
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		ret = make_sales_return(earlier.name)
		ret.posting_date = nowdate()
		ret.payments[0].amount = -self._total(earlier)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Gross sales", "Recorded discounts", "Returns", "Net sales")],
			[500.0, 50.0, 100.0, 350.0])
		self.assertEqual(s["Returns"][1], 1)
		self._checks_clear(s)
		self._views_agree(s)

	def test_a_grand_total_discount_on_a_taxed_sale(self):
		"""200 of goods, 21.65 off the grand total at 8.25%: Discounts 20.00, tax 14.85, total 194.85."""
		inv = self._sale([(GOODS, 200)], [("Cash", 194.85)], discount=21.65)
		self.assertEqual(flt(self._total(inv), 2), 194.85)
		# No tax-inclusive line: the shared discount is exactly amount − net amount.
		self.assertAlmostEqual(sum(flt(r.distributed_discount_amount) for r in inv.items),
			sum(flt(r.amount) - flt(r.net_amount) for r in inv.items), delta=0.01)
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Recorded discounts", "Net sales", "Sales tax", "Total of tickets")],
			[20.0, 180.0, 14.85, 194.85])
		self.assertEqual(_amount(s, "Gross sales"), 200.0)
		self._checks_clear(s)

	def test_a_trade_in_ticket_that_pays_the_customer(self):
		"""565 of goods against 5,450 of trade-ins: Net 565, Allowance 5,450, tax 0, total -4,885, payout 4,885."""
		self._ticket()
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Net sales", "Trade-in allowance", "Sales tax", "Total of tickets",
			"Trade-in payouts")], [565.0, 5450.0, 0, -4885.0, -4885.0])
		self.assertNotIn("Unpaid / on account", s)
		self._checks_clear(s)
		self._views_agree(s)

	def test_the_worked_check_example(self):
		"""Ticket A: 1,300 against an 800 trade-in, 41.25 tax, 541.25 cash. Ticket B: a 100 gunsmith deposit by
		card. 1,300 - 800 + 100 + 41.25 = 641.25 = 541.25 + 100."""
		a = self._ticket(goods=1300, credits=(800,), pay=541.25)
		self.assertEqual(flt(self._total(a), 2), 541.25)
		from ffl_core import gunsmith
		from ffl_core.tests.test_gunsmith_work_order import _customer, _job

		job = _job(_customer(), company=self.company)
		line = gunsmith.deposit_line(job.name, 100)["deposit_line"]
		frappe.set_user(self.neg_cashier)
		try:
			b = frappe.get_doc({"doctype": "POS Invoice", "company": self.company, "pos_profile": self.neg_profile,
				"customer": job.customer, "items": [line]})
			b.set_missing_values()
			b.set("payments", [])
			b.append("payments", {"mode_of_payment": CARD, "amount": 100})
			b.card_unique_reference = "TEST-APPROVAL"
			b.flags.ignore_permissions = True
			b.insert()
			b.submit()
		finally:
			frappe.set_user("Administrator")
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Net sales", "Trade-in allowance", "Deposits taken", "Sales tax",
			"Total of tickets", "Cash", CARD, "Total")], [1300.0, 800.0, 100.0, 41.25, 641.25, 541.25, 100.0, 641.25])
		self._checks_clear(s)
		self._views_agree(s)

	def test_split_tender_change_and_an_unpaid_balance(self):
		"""Cash and card on one ticket, change given on another, a Sales Invoice half paid: E1 and E2 stay 0.00."""
		split = self._sale([(GOODS, 300)], [("Cash", 100), (CARD, 224.75)])
		self.assertEqual(flt(self._total(split), 2), 324.75)
		change = self._sale([(GOODS, 100)], [("Cash", 120)])
		self.assertEqual(flt(change.change_amount, 2), 11.75)
		si = self._si([(GOODS, 200)])
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry("Sales Invoice", si.name, party_amount=100)
		pe.paid_to = self.cash
		pe.mode_of_payment = "Check"
		pe.reference_no, pe.reference_date = "1001", nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		s = _summary(self.company)
		self.assertEqual(_amount(s, "Cash"), 100 + 108.25)
		self.assertEqual(_amount(s, CARD), 224.75)
		self.assertEqual(_amount(s, "Check"), 100.0)
		self.assertEqual(_amount(s, "Unpaid / on account"), flt(si.grand_total - 100, 2))
		self._checks_clear(s)
		self._views_agree(s)

	def test_a_membership_is_in_net_sales_and_shown_billed(self):
		"""A 650 membership billed today: Memberships (billed) 650, inside Net sales; recognized reads the GL."""
		from ffl_core.setup.membership import accounts

		deferred, income = accounts(self.company)
		if not frappe.db.exists("Item", MEMBER):
			frappe.get_doc({"doctype": "Item", "item_code": MEMBER, "item_name": MEMBER, "item_group": "Service",
				"stock_uom": "Nos", "is_stock_item": 0, "enable_deferred_revenue": 1, "no_of_months": 12,
				"taxes": [{"item_tax_template": frappe.db.get_value("Item Tax", {"parent": self.exempt_item},
					"item_tax_template")}]}).insert(ignore_permissions=True)
		self._si([(MEMBER, 650, {"enable_deferred_revenue": 1, "deferred_revenue_account": deferred,
			"income_account": income, "service_start_date": nowdate(),
			"service_end_date": add_days(nowdate(), 364)})])
		je = frappe.get_doc({"doctype": "Journal Entry", "company": self.company, "posting_date": nowdate(),
			"accounts": [{"account": deferred, "debit_in_account_currency": 28.49},
				{"account": income, "credit_in_account_currency": 28.49}]})
		je.insert(ignore_permissions=True)
		je.submit()
		s = _summary(self.company)
		self.assertEqual(_amount(s, "of which Memberships (billed)"), 650.0)
		self.assertEqual(_amount(s, "Net sales"), 650.0)
		self.assertEqual(_amount(s, "Memberships recognized (GL)"), 28.49)
		_c, cats, *_ = _run(self.company, "Category")
		self.assertEqual([(r["category"], r["net_sales"]) for r in cats if not r.get("is_total")],
			[(ss.MEMBERSHIPS, 650.0)])
		self._checks_clear(s)

	def test_what_is_left_out_and_who_is_unattributed(self):
		"""Locked lines stay out of categories, a pickup ticket and a consolidated invoice are not sales, and a
		Sales Invoice opened by Administrator with no register is Unattributed in both views."""
		self._ticket(goods=1300, credits=(800,), pay=541.25)
		pickup = self._paid_sale([(GOODS, 77)])
		frappe.db.set_value("POS Invoice", pickup.name, "ffl_counter_order", "_TEST-PICKUP", update_modified=False)
		merged = self._si([(GOODS, 55)])
		frappe.db.set_value("Sales Invoice", merged.name, "is_consolidated", 1, update_modified=False)
		self._si([(GOODS, 40)])
		s = _summary(self.company)
		self.assertEqual(_amount(s, "Net sales"), 1340.0)
		self.assertEqual(s["Net sales"][1], 2)
		_c, cats, *_ = _run(self.company, "Category")
		self.assertEqual({r["category"] for r in cats if not r.get("is_total")}, {self.goods_group_top(), ss.UNCATEGORIZED})
		_c, regs, *_ = _run(self.company, "Register")
		self.assertEqual({r["group"]: r["net_sales"] for r in regs if not r.get("is_total")},
			{self.neg_profile: 1300.0, ss.UNATTRIBUTED: 40.0})
		_c, emps, *_ = _run(self.company, "Employee")
		self.assertEqual({r["group"]: r["net_sales"] for r in emps if not r.get("is_total")},
			{frappe.utils.get_fullname(self.neg_cashier): 1300.0, ss.UNATTRIBUTED: 40.0})
		self._checks_clear(s)
		self._views_agree(s)

	def goods_group_top(self):
		return ss.UNCATEGORIZED if self.goods_group == "All Item Groups" else self.goods_group

	def test_net_sales_is_the_sales_reports_own(self):
		"""Same period, same invoices: the Summary's Net sales is the Sales Report's Net Sales."""
		self._ticket()
		self._paid_sale([(GOODS, 450, 500)])
		self._si([(GOODS, 40)])
		_c, data, *_ = sr.execute({"from_date": nowdate(), "to_date": nowdate(), "view": "Order"})
		mine = set(frappe.get_all("POS Invoice", filters={"company": self.company, "docstatus": 1}, pluck="name")) | set(
			frappe.get_all("Sales Invoice", filters={"company": self.company, "docstatus": 1}, pluck="name"))
		report = sum(flt(r["net_sales"]) for r in data if not r.get("is_total") and r.get("invoice") in mine)
		self.assertEqual(_amount(_summary(self.company), "Net sales"), flt(report, 2))

	def test_a_web_refund_is_a_return_not_a_discount(self):
		"""A web order's whole-ticket discount is what Woo refunded: it is in Returns."""
		si = self._si([(GOODS, 200)], apply_discount_on="Net Total", discount_amount=50)
		frappe.db.set_value("Sales Invoice", si.name, "woo_online_order", "_TEST-WOO", update_modified=False)
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Gross sales", "Recorded discounts", "Returns", "Net sales")],
			[200.0, 0, 50.0, 150.0])
		self._checks_clear(s)

	def test_credit_notes_against_their_invoice_and_refunded(self):
		"""A credit note set off against its unpaid invoice, and one refunded by a Pay entry: both are Returns,
		and the payments side still ties."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		owed = self._si([(GOODS, 200)])
		cn = make_sales_return(owed.name)
		cn.update_outstanding_for_self = 0
		cn.insert(ignore_permissions=True)
		cn.submit()
		paid = self._si([(GOODS, 100)])
		pe = get_payment_entry("Sales Invoice", paid.name)
		pe.paid_to, pe.mode_of_payment = self.cash, "Cash"
		pe.insert(ignore_permissions=True)
		pe.submit()
		refund = make_sales_return(paid.name)
		refund.update_outstanding_for_self = 1
		refund.insert(ignore_permissions=True)
		refund.submit()
		back = get_payment_entry("Sales Invoice", refund.name)
		back.paid_from, back.mode_of_payment = self.cash, "Cash"
		back.insert(ignore_permissions=True)
		back.submit()
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Gross sales", "Returns", "Net sales", "Total of tickets")],
			[300.0, 300.0, 0, 0])
		# Taken in and paid back in cash, set off against each other: nothing left on any payment line.
		for row in ("Cash", "Unpaid / on account", "Credit notes applied"):
			self.assertNotIn(row, s)
		self._checks_clear(s)
		self._views_agree(s)

	def test_filters_are_checked(self):
		with self.assertRaises(frappe.ValidationError):
			ss.execute({"company": self.company, "from_date": nowdate(), "to_date": nowdate(), "view": "Bogus"})
		with self.assertRaises(frappe.ValidationError):
			ss.execute({"company": self.company, "from_date": nowdate(), "to_date": add_days(nowdate(), -1)})
		nobody = _user(f"ss-nobody-{self.KIND}@example.com")
		frappe.set_user(nobody)
		try:
			with self.assertRaises(frappe.PermissionError):
				ss.execute({"company": self.company, "from_date": nowdate(), "to_date": nowdate()})
		finally:
			frappe.set_user("Administrator")

	def test_an_empty_period_reads_zero_and_clean(self):
		s = _summary(self.company, add_days(nowdate(), -400))
		self.assertEqual(_amount(s, "Net sales"), 0)
		self._checks_clear(s)


StoreASalesSummary = _store_tests("StoreASalesSummary", "store_a", SummaryFixtures, Cases)


class ExciseStoreSalesSummary(ExciseStoreFixtures):
	"""A pistol at a store that collects the excise: sales tax 43.75, excise 55.00, the service line untaxed."""

	def test_sales_tax_and_excise_on_their_own_lines(self):
		si = self._si((GUN, 500), (SERVICE, 100))
		s = _summary(self.company)
		self.assertEqual([_amount(s, k) for k in ("Net sales", "Sales tax", "Excise (CFET)", "Tax owed",
			"Non-taxable items", "Total of tickets", "Unpaid / on account")],
			[600.0, 43.75, 55.0, 98.75, 100.0, 698.75, 698.75])
		self.assertEqual(flt(si.grand_total, 2), 698.75)
		self.assertEqual(_amount(s, "E1: tickets less sales and charges"), 0)
		self.assertEqual(_amount(s, "E2: tickets less payments"), 0)

	def test_the_gl_memo_matches_on_sales_invoices(self):
		self._si((GUN, 500))
		memo = ss.gl_sales_tax_memo({"company": self.company, "from_date": nowdate(), "to_date": nowdate()})
		self.assertEqual((memo["gl"], memo["report"], memo["difference"]), (43.75, 43.75, 0))
