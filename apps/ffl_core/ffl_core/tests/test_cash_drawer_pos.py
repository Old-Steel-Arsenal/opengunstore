"""Cash drawer, the parts that meet ERPNext's POS: shifts and their closings, the 23:00 job, and the
money paid to sellers for guns.

Same two stand-in stores as test_cash_drawer (Store A-like / Store B-like). The trade-in dialog's endpoint and the cost
correction run on the shared dev company, whose intake machinery they use.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, flt, getdate, now_datetime, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.api import cash_drawer as api
from ffl_core.tests.test_cash_drawer import SPECS, CashFixtures, _account, _store_tests, _user


class ShiftFixtures(CashFixtures):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cd.ensure_accounts(cls.company)
		cls.cashier = _user(f"cd-cashier-{cls.KIND}@example.com", "Sales User")
		cls.profile = cls._profile()
		if not frappe.db.exists("Item", "CD-SERVICE"):
			frappe.get_doc({"doctype": "Item", "item_code": "CD-SERVICE", "item_name": "CD-SERVICE",
				"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)

	@classmethod
	def _profile(cls, suffix: str = "") -> str:
		name = f"{cls.company} POS{suffix}"
		if frappe.db.exists("POS Profile", name):
			return name
		mop = frappe.get_doc("Mode of Payment", "Cash")
		if not any(r.company == cls.company for r in mop.accounts):
			mop.append("accounts", {"company": cls.company, "default_account": f"Cash - {cls.abbr}"})
			mop.save(ignore_permissions=True)
		cost_center = frappe.db.get_value("Company", cls.company, "cost_center")
		frappe.get_doc({
			"doctype": "POS Profile", "name": name, "company": cls.company,
			"warehouse": frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": "Stores"}),
			"currency": "USD", "customer_group": "All Customer Groups", "territory": "All Territories",
			"selling_price_list": "Standard Selling",
			"write_off_account": frappe.db.get_value("Account", {"company": cls.company, "account_name": ("like", "Write Off%")}),
			"write_off_cost_center": cost_center,
			"income_account": frappe.db.get_value("Account", {"company": cls.company, "account_type": "Income Account", "is_group": 0}),
			"expense_account": frappe.db.get_value("Account", {"company": cls.company, "account_type": "Expense Account", "is_group": 0}),
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
		}).insert(ignore_permissions=True)
		return name

	def setUp(self):
		# ERPNext's merge of a closing's invoices commits the open transaction when it finishes (this ERPNext
		# does not treat the test runner as a test there): that would make every row of the class permanent and
		# release the test's savepoint. docs/testing.md rule 1: neutralise it.
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		super().setUp()

	# --- a shift and its sales ---------------------------------------------------------------

	def _open_shift(self, profile: str | None = None, user: str | None = None) -> str:
		opening = frappe.get_doc({
			"doctype": "POS Opening Entry", "company": self.company, "pos_profile": profile or self.profile,
			"user": user or self.cashier,
			"period_start_date": now_datetime(), "posting_date": nowdate(),
			"balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
		})
		opening.flags.ignore_permissions = True
		opening.insert()
		opening.submit()
		return opening.name

	def _pos_sale(self, rate=100, tendered=120) -> str:
		"""A real submitted POS Invoice by the cashier: cash tendered, change given."""
		frappe.set_user(self.cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.profile
			inv.customer = "_Test CD Customer"
			inv.append("items", {"item_code": "CD-SERVICE", "qty": 1, "rate": rate, "price_list_rate": rate})
			inv.set_missing_values()
			inv.items[0].rate = inv.items[0].price_list_rate = rate
			inv.set("payments", [])
			inv.append("payments", {"mode_of_payment": "Cash", "amount": tendered})
			inv.flags.ignore_permissions = True
			inv.save()
			inv.submit()
			return inv.name
		finally:
			frappe.set_user("Administrator")

	def _receive_cash(self, amount):
		pe = frappe.get_doc({
			"doctype": "Payment Entry", "payment_type": "Receive", "company": self.company, "posting_date": nowdate(),
			"party_type": "Customer", "party": "_Test CD Customer", "paid_from": f"Debtors - {self.abbr}",
			"paid_to": self.cash, "paid_amount": amount, "received_amount": amount,
			"source_exchange_rate": 1, "target_exchange_rate": 1,
		})
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		return pe.name

	def _cash_row(self, closing):
		return next(r for r in frappe.get_doc("POS Closing Entry", closing).payment_reconciliation if r.mode_of_payment == "Cash")


class ShiftCases:
	def test_close_day_puts_the_ledger_on_the_cash_row(self):
		"""A1/A2: the Cash row is the ledger (merged sale, pending sale, counter-order deposit), not ERPNext's figure."""
		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now

		self._first_count(books=1000, counted=120)
		self._next_day()
		opening = self._open_shift()
		merged = self._pos_sale()
		consolidate_pos_invoice_now(merged)  # a firearm sale is merged the moment it completes
		self._pos_sale()  # an ammo sale waits for the close
		self._receive_cash(250)  # a deposit on a counter order
		books = self._expected()
		self.assertEqual(books, 120 + 100 + 100 + 250)
		erpnext = next((r.expected_amount for r in make_closing_entry_from_opening(
			frappe.get_doc("POS Opening Entry", opening)).payment_reconciliation if r.mode_of_payment == "Cash"), None)
		self.assertNotEqual(erpnext, books)  # ERPNext sees the pending sale only
		r = self._close(books - 10)
		self.assertEqual(len(r["pos_closing_entries"]), 1)
		self.assertEqual(r["pos_errors"], [])
		closing = r["pos_closing_entries"][0]
		row = self._cash_row(closing)
		self.assertEqual((row.expected_amount, row.closing_amount, row.difference, row.opening_amount), (books, books - 10, -10, 120))
		doc = frappe.get_doc("POS Closing Entry", closing)
		self.assertEqual((doc.docstatus, doc.ffl_cash_counted, doc.ffl_cash_drawer_close), (1, 1, r["close"]))
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", r["close"], "status"), "Closed")
		self.assertEqual(frappe.db.get_value("POS Opening Entry", opening, "status"), "Closed")
		self.assertEqual(self._gl_cash(), books - 10)  # merged, then pulled back to the count
		self.assertEqual(len(self._jes("CASH-VARIANCE")), 1)

	def test_the_log_shows_merged_register_sales_one_by_one(self):
		self._first_count(books=1000, counted=120)
		self._next_day()
		self._open_shift()
		first = self._pos_sale()
		second = self._pos_sale(rate=40, tendered=40)
		self._close(self._expected())  # merges both into the night's invoice
		self.assertTrue(frappe.db.get_value("POS Invoice", first, "consolidated_invoice"))
		frappe.set_user(self.cashier)
		log = api.get_log(self.company, nowdate(), nowdate())
		frappe.set_user("Administrator")
		sales = [(l["voucher_type"], l["voucher_no"], l["amount"], l["by"]) for l in log["lines"] if l["kind"] == "sale_pos"]
		self.assertEqual(sales, [("POS Invoice", first, 100, self.cashier), ("POS Invoice", second, 40, self.cashier)])
		self.assertEqual(log["closing"], self._expected())

	def test_a_sale_rung_up_before_midnight_stays_on_its_own_day(self):
		self._first_count(books=1000, counted=120)
		self._next_day()
		self._open_shift()
		late = self._pos_sale()
		frappe.db.set_value("POS Invoice", late, "posting_date", add_days(nowdate(), -1))  # rung up before midnight
		self._pos_sale(rate=40, tendered=40)
		self._close(self._expected())
		log = api.get_log(self.company, add_days(nowdate(), -1), nowdate())
		sales = [(l["posting_date"], l["voucher_type"], l["amount"]) for l in log["lines"] if l["kind"] == "sale_pos"]
		self.assertEqual(sales, [(add_days(nowdate(), -1), "POS Invoice", 100), (nowdate(), "POS Invoice", 40)])
		self.assertEqual(log["closing"], self._expected())
		today = api.get_log(self.company, nowdate(), nowdate())  # its window starts after the late sale
		self.assertEqual(today["opening"] + 40, today["closing"])
		# ERPNext dates a merged invoice like its LAST sale: the late sale's ledger line moves to today, its log line does not
		si = frappe.db.get_value("POS Invoice", late, "consolidated_invoice")
		frappe.db.sql("UPDATE `tabGL Entry` SET posting_date = %s WHERE voucher_no = %s", (nowdate(), si))
		yesterday = api.get_log(self.company, add_days(nowdate(), -1), add_days(nowdate(), -1))
		self.assertIn((late, 100), [(l["voucher_no"], l["amount"]) for l in yesterday["lines"]])
		self.assertEqual(yesterday["closing"], api.get_log(self.company, nowdate(), nowdate())["opening"])

	def test_a_day_without_cash_sales_still_has_a_cash_row(self):
		"""A1: the closing carries a Cash row whatever the day sold, opening = the last counted cash."""
		self._first_count(books=1000, counted=120)
		self._next_day()
		opening = self._open_shift()
		r = self._close(120)
		row = self._cash_row(r["pos_closing_entries"][0])
		self.assertEqual((row.opening_amount, row.expected_amount, row.closing_amount, row.difference), (120, 120, 120, 0))
		self.assertEqual(frappe.db.get_value("POS Opening Entry", opening, "status"), "Closed")

	def test_the_merge_moves_cash_from_pending_to_the_ledger_without_changing_expected(self):
		self._open_shift()
		sale = self._pos_sale()
		before = cd.drawer_expected(self.company)
		self.assertEqual((before.gl, before.pending), (0, 100))
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		consolidate_pos_invoice_now(sale)
		after = cd.drawer_expected(self.company)
		self.assertEqual((after.gl, after.pending, after.expected), (100, 0, 100))

	def test_cash_taken_on_a_draft_counts_once_take_payment_stamped_it(self):
		"""A part payment (or a tender taken before the 4473 clears) stays a draft; its cash is in the drawer."""
		self._open_shift()
		before = self._expected()
		frappe.set_user(self.cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.profile
			inv.customer = "_Test CD Customer"
			inv.append("items", {"item_code": "CD-SERVICE", "qty": 1, "rate": 300, "price_list_rate": 300})
			inv.set_missing_values()
			inv.items[0].rate = inv.items[0].price_list_rate = 300
			inv.set("payments", [])
			inv.append("payments", {"mode_of_payment": "Cash", "amount": 100})
			inv.flags.ignore_permissions = True
			inv.save()
			self.assertEqual(self._expected(), before)  # typed on a draft, never taken: not counted
			inv.ffl_cash_taken_at = "2000-01-01 00:00:00"  # the browser asks; the server dates it
			inv.save()
			stamp = frappe.db.get_value("POS Invoice", inv.name, "ffl_cash_taken_at")
			self.assertGreater(stamp, add_to_date(now_datetime(), minutes=-5))
			self.assertEqual(self._expected(), before + 100)
			rows = [r for r in cd.pending_pos_rows(self.company) if r.voucher_no == inv.name]
			self.assertEqual([(r.kind, r.amount, r.creation, r.posting_date) for r in rows],
				[("order_payment", 100, stamp, getdate(stamp))])  # dated by the stamp, not the draft's moving posting_date
			_opening, logged = cd.log_rows(self.company, self.cash, getdate(stamp), getdate(stamp))
			self.assertEqual([(r.kind, r.amount) for r in logged if r.voucher_no == inv.name], [("order_payment", 100)])
			frappe.set_user(self.manager)
			page = api.get_today(self.company)
			frappe.set_user(self.cashier)
			self.assertIn(("order_payment", "Cash paid on an open order", 100), [
				(l["kind"], l["label"], l["amount"]) for l in page["lines"] if l["voucher_no"] == inv.name])
			stale = frappe.get_doc("POS Invoice", inv.name)
			stale.ffl_cash_taken_at = None  # a register screen loaded before the stamp
			stale.flags.ignore_permissions = True
			stale.save()
			self.assertEqual(frappe.db.get_value("POS Invoice", inv.name, "ffl_cash_taken_at"), stamp)
			inv = frappe.get_doc("POS Invoice", inv.name)
			total = flt(inv.rounded_total or inv.grand_total)
			next(p for p in inv.payments if p.mode_of_payment == "Cash").amount = total  # the rest, paid on completing
			inv.flags.ignore_permissions = True
			inv.save()  # ERPNext checks the paid amount before it totals the payments again
			inv.submit()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._expected(), flt(before + total, 2))  # the sale now, not the draft as well

	def test_a_closing_made_on_the_pos_screen_is_per_shift_and_not_a_count(self):
		"""One shift at a time: the Cash row is that shift's own cash (ERPNext's figure), opening 0, never the whole drawer."""
		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

		self._sale(500)  # the drawer holds 500 already
		opening = self._open_shift()
		closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", opening))
		closing.flags.ignore_permissions = True
		closing.insert()
		row = next(r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash")
		self.assertEqual((row.opening_amount, row.expected_amount, row.closing_amount, row.difference), (0, 0, 0, 0))
		self.assertEqual(closing.ffl_cash_counted, 0)

	def test_a_closing_from_the_pos_screen_keeps_what_the_cashier_typed(self):
		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

		self._sale(500)
		opening = self._open_shift()
		self._pos_sale()  # 100 in this shift
		closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", opening))
		row = next(r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash")
		self.assertEqual(row.expected_amount, 100)  # ERPNext's own figure for the shift
		row.closing_amount = 90  # the cashier typed what they counted
		closing.flags.ignore_permissions = True
		closing.insert()
		row = next(r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash")
		self.assertEqual((row.opening_amount, row.expected_amount, row.closing_amount, row.difference), (0, 100, 90, -10))
		self.assertEqual(closing.ffl_cash_counted, 0)  # the Cash Drawer Close is the record of a count
		self.assertFalse(frappe.db.exists("Cash Drawer Close", {"company": self.company}))

	def test_the_nightly_job_never_counts(self):
		"""A4: the shift is merged as always; the Cash row repeats the ledger, not counted; nothing is booked; the
		next real count covers the day."""
		from ffl_core.api.pos_close import auto_close_open_pos

		self._first_count(books=1000, counted=120)
		self._next_day()
		opening = self._open_shift()
		sale = self._pos_sale()
		books = self._expected()
		self.assertEqual(books, 220)
		out = auto_close_open_pos(min_age_hours=0)
		mine = next(c for c in out["closed"] if c["opening"] == opening)
		row = self._cash_row(mine["closing"])
		self.assertEqual((row.expected_amount, row.closing_amount, row.difference), (books, books, 0))
		self.assertEqual(frappe.db.get_value("POS Closing Entry", mine["closing"], "ffl_cash_counted"), 0)
		self.assertEqual(self._jes("CASH-VARIANCE"), [])  # nothing booked
		self.assertTrue(frappe.db.get_value("POS Invoice", sale, "consolidated_invoice"))  # merged as usual
		self.assertEqual(frappe.db.get_value("GL Entry", {"company": self.company, "account": self.sales, "is_cancelled": 0,
			"voucher_type": "Sales Invoice", "credit": 100}, "credit"), 100)
		night = frappe.get_doc("Cash Drawer Close", {"company": self.company, "business_date": nowdate(), "status": "Closed"})
		self.assertEqual((night.is_counted, night.counted, night.expected, night.carry_forward, night.opening_amount),
			(0, 0, books, books, 120))
		self._next_day()
		self._sale(80)  # books 300
		r = self._close(250, reason="counted the morning after")
		self.assertEqual(r["variance"], -50)
		self.assertEqual(r["days_covered"], 2)
		self.assertEqual(frappe.get_doc("Cash Drawer Close", r["close"]).opening_amount, books)  # the uncounted night's balance


	def test_with_several_open_shifts_only_the_latest_carries_the_drawer(self):
		self._first_count(books=1000, counted=120)
		self._next_day()
		cashier2 = _user(f"cd-cashier2-{self.KIND}@example.com", "Sales User")
		first = self._open_shift()
		second = self._open_shift(self._profile(" 2"), cashier2)
		self._sale(80)
		r = self._close(190)
		self.assertEqual(len(r["pos_closing_entries"]), 2)
		early, late = r["pos_closing_entries"]
		self.assertEqual(frappe.db.get_value("POS Closing Entry", early, "pos_opening_entry"), first)
		self.assertEqual(frappe.db.get_value("POS Closing Entry", late, "pos_opening_entry"), second)
		row = self._cash_row(early)
		self.assertEqual((row.opening_amount, row.expected_amount, row.closing_amount, row.difference), (0, 0, 0, 0))
		row = self._cash_row(late)
		self.assertEqual((row.expected_amount, row.closing_amount), (200, 190))
		self.assertEqual([frappe.db.get_value("POS Closing Entry", n, "ffl_cash_counted") for n in (early, late)], [1, 1])

	def test_the_nightly_job_zeroes_all_but_the_last_shift_of_a_company(self):
		from ffl_core.api.pos_close import auto_close_open_pos

		self._first_count(books=1000, counted=120)
		self._next_day()
		cashier2 = _user(f"cd-cashier2-{self.KIND}@example.com", "Sales User")
		first = self._open_shift()
		second = self._open_shift(self._profile(" 2"), cashier2)
		out = auto_close_open_pos(min_age_hours=0)
		made = {c["opening"]: c["closing"] for c in out["closed"]}
		early, late = self._cash_row(made[first]), self._cash_row(made[second])
		self.assertEqual((early.expected_amount, early.closing_amount, early.difference), (0, 0, 0))
		self.assertEqual((late.expected_amount, late.closing_amount, late.difference), (120, 120, 0))
		self.assertEqual(frappe.db.get_value("POS Closing Entry", made[first], "ffl_cash_counted"), 0)

	def _counter_sale(self, rate=80, tendered=100) -> str:
		"""A counter order paid at the desk (a Sales Invoice with its own cash payment): $100 for an $80 sale."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice", "company": self.company, "customer": "_Test CD Customer", "is_pos": 1,
			"pos_profile": self.profile, "update_stock": 0, "set_posting_time": 0,
			"items": [{"item_code": "CD-SERVICE", "qty": 1, "rate": rate}],
			"payments": [{"mode_of_payment": "Cash", "amount": tendered}],
			"paid_amount": tendered, "change_amount": tendered - rate, "account_for_change_amount": self.cash,
		})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si.name

	def test_change_given_back_is_part_of_the_sale_not_a_refund(self):
		sale = self._counter_sale(80, 100)
		self.assertEqual(self._gl_cash(), 80)  # the books hold the net
		rows = [r for r in cd.cash_ledger_rows(self.company) if r.voucher_no == sale]
		self.assertEqual([(r.kind, r.amount) for r in rows], [("sale_counter", 80.0)])
		frappe.set_user(self.manager)
		page = api.get_today(self.company)
		frappe.set_user("Administrator")
		self.assertEqual([(l["kind"], l["amount"]) for l in page["lines"]], [("sale_counter", 80.0)])
		_cols, report = self._report(days=0)
		today = next(r for r in report if r.get("day") == nowdate())
		self.assertEqual((today["cash_counter"], today["refunds"]), (80, 0))


	# ------------------------------------------------------------------ close_day ordering

	def _setup_for_a_close_attempt(self):
		self._first_count(books=1000, counted=120)
		self._next_day()
		return self._open_shift()

	def _shift_still_open(self, opening):
		return frappe.db.get_value("POS Opening Entry", opening, "status") == "Open" and not frappe.db.exists(
			"POS Closing Entry", {"pos_opening_entry": opening, "docstatus": ["!=", 2]})

	def test_every_refusal_comes_before_a_shift_is_merged(self):
		opening = self._setup_for_a_close_attempt()
		self._sale(100)  # expected 220
		with self.assertRaises(frappe.ValidationError):  # a big difference without a reason
			self._close(100)
		self.assertTrue(self._shift_still_open(opening))
		with self.assertRaises(frappe.ValidationError):  # the screen figure is stale
			self._close(220, expected_seen=1)
		self.assertTrue(self._shift_still_open(opening))
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", nowdate())
		with self.assertRaises(frappe.ValidationError):  # the period is frozen
			self._close(220)
		self.assertTrue(self._shift_still_open(opening))
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		self._close(220)
		with self.assertRaises(frappe.ValidationError):  # already counted today
			self._close(220)

	def test_a_second_close_stops_at_the_claim_before_any_shift_is_merged(self):
		opening = self._setup_for_a_close_attempt()
		self._sale(100)
		other = frappe.get_doc({"doctype": "Cash Drawer Close", "company": self.company, "business_date": nowdate(),
			"status": "Closing", "counted_by": self.manager, "counted_at": now_datetime()})
		other.flags.ignore_permissions = True
		other.insert()
		with self.assertRaises(frappe.ValidationError):
			self._close(220)
		self.assertTrue(self._shift_still_open(opening))
		# a claim nobody finished is taken over after a few minutes
		frappe.db.set_value("Cash Drawer Close", other.name, "modified", add_to_date(now_datetime(), minutes=-30), update_modified=False)
		r = self._close(220)
		self.assertEqual(r["close"], other.name)
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", other.name, ["status", "is_counted"]), ("Closed", 1))

	def test_a_failure_after_the_merge_leaves_no_closing_claiming_a_count(self):
		opening = self._setup_for_a_close_attempt()
		self._sale(100)  # expected 220, count 200: a variance entry has to be booked after the merge
		from unittest.mock import patch

		with patch("ffl_core.cash_drawer._post_je", side_effect=frappe.ValidationError("books are locked")):
			with self.assertRaises(frappe.ValidationError):
				self._close(200.5)
		closing = frappe.db.get_value("POS Closing Entry", {"pos_opening_entry": opening, "docstatus": 1}, "name")
		self.assertTrue(closing)  # the shift did merge
		doc = frappe.get_doc("POS Closing Entry", closing)
		row = next(r for r in doc.payment_reconciliation if r.mode_of_payment == "Cash")
		self.assertEqual((doc.ffl_cash_counted, doc.ffl_cash_drawer_close), (0, None))
		self.assertEqual((row.closing_amount, row.difference), (row.expected_amount, 0))  # the count was never written on it
		claim = frappe.get_doc("Cash Drawer Close", {"company": self.company, "business_date": nowdate(), "status": ["!=", "Undone"]})
		self.assertEqual(claim.status, "Closing")
		# the same person tries again: it goes through, and now says what was counted
		r = self._close(200.5)
		self.assertEqual(r["close"], claim.name)
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", claim.name, "status"), "Closed")

	def test_a_shift_that_will_not_close_does_not_stop_the_count(self):
		from unittest.mock import patch

		self._first_count(books=1000, counted=120)
		self._next_day()
		self._open_shift()
		self._sale(80)
		with patch("erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry.make_closing_entry_from_opening",
				side_effect=frappe.ValidationError("boom")):
			r = self._close(190)
		self.assertEqual(r["pos_closing_entries"], [])
		self.assertEqual(len(r["pos_errors"]), 1)
		self.assertEqual(r["variance"], -10)
		self.assertTrue(r["variance_je"])


StoreAShift = _store_tests("StoreAShift", "store_a", ShiftFixtures, ShiftCases)
StoreBShift = _store_tests("StoreBShift", "store_b", ShiftFixtures, ShiftCases)


# ---------------------------------------------------------------------------------------------
# Seller payments
# ---------------------------------------------------------------------------------------------


def trade_in_acquisition(company, serial, cost=300, item="CD-GUN"):
	"""A booked Trade-in acquisition from a private seller (shared with test_trade_in_settlement)."""
	from ffl_core.acquisition import record_firearm_acquisition

	if not frappe.db.exists("Item", item):
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "Pistol",
			"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			"serial_no_series": f"{item}-.####"}).insert(ignore_permissions=True)
	return record_firearm_acquisition(
		item_code=item, serial=serial, source_type="Trade-in", source_name="Testy Seller", cost=cost,
		company=company, source_id_type="Driver License", source_id_number="DL-1", source_first_name="Testy",
		source_last_name="Seller", source_address="123 Main St, Springfield TX 75024")


def legacy_payout(acquisition, method):
	"""A gun's own seller payment as record_payout made it before a pay-now buy became a receipt paid with it: the
	history the rest of the payout code still undoes, books again, changes and corrects."""
	acq = frappe.db.get_value("FFL Acquisition", acquisition, ["company", "cost", "serial"], as_dict=True)
	return cd._new_entry(entry_type="Payout", company=acq.company, amount=flt(acq.cost, 2), payment_method=method,
		acquisition=acquisition, serial=acq.serial)


class PayoutCases:
	def _acquisition(self, serial, cost=300, item="CD-GUN"):
		return trade_in_acquisition(self.company, serial, cost, item)

	def test_a_cash_payment_is_stock_adjustment_against_cash(self):
		"""A5: Dr Stock Adjustment / Cr Cash for exactly the cost; the remark never says FFL Disposition."""
		acq = self._acquisition("CD-S1")
		before = self._expected()
		made = legacy_payout(acq, "Cash")
		self.assertEqual(self._lines(made.journal_entry), {f"Stock Adjustment - {self.abbr}": (300, 0), self.cash: (0, 300)})
		remark = frappe.db.get_value("Journal Entry", made.journal_entry, "user_remark")
		self.assertEqual(remark, f"CASH-PAYOUT {acq} CD-S1")
		self.assertNotIn("FFL Disposition", remark)
		self.assertEqual(self._expected(), before - 300)

	def test_a_zelle_or_check_payment_comes_out_of_the_bank(self):
		for method, serial in (("Zelle", "CD-S2"), ("Check", "CD-S3"), ("ACH", "CD-S4")):
			made = legacy_payout(self._acquisition(serial), method)
			self.assertEqual(self._lines(made.journal_entry),
				{f"Stock Adjustment - {self.abbr}": (300, 0), f"Operating Bank - {self.abbr}": (0, 300)}, method)
		with self.assertRaises(frappe.ValidationError):
			legacy_payout(self._acquisition("CD-S5"), "Bitcoin")

	def test_an_acquisition_is_paid_once(self):
		acq = self._acquisition("CD-S6")
		legacy_payout(acq, "Cash")
		with self.assertRaises(frappe.ValidationError):
			cd.record_payout(acq, "Cash")
		# and the database says the same even when the application check is skipped
		with self.assertRaises(frappe.UniqueValidationError):
			cd._new_entry(entry_type="Payout", company=self.company, amount=300, payment_method="Cash", acquisition=acq, serial="CD-S6")
		self.assertEqual(len(self._jes("CASH-PAYOUT")), 1)

	def test_undoing_a_payment_lets_it_be_recorded_again(self):
		acq = self._acquisition("CD-S7")
		first = legacy_payout(acq, "Cash")
		cd.void_entry(first.entry)
		again = cd.record_payout(acq, "Zelle")
		self.assertIn(f"Operating Bank - {self.abbr}", self._lines(again.journal_entry))

	def test_a_cost_correction_books_the_difference_the_same_way(self):
		acq = self._acquisition("CD-S8")
		legacy_payout(acq, "Cash")
		up = cd.adjust_payout_for_cost_correction("CD-S8", 350)
		self.assertEqual(self._lines(up), {f"Stock Adjustment - {self.abbr}": (50, 0), self.cash: (0, 50)})
		self.assertIsNone(cd.adjust_payout_for_cost_correction("CD-S8", 350))
		down = cd.adjust_payout_for_cost_correction("CD-S8", 300)
		self.assertEqual(self._lines(down), {f"Stock Adjustment - {self.abbr}": (0, 50), self.cash: (50, 0)})
		self.assertIsNone(cd.adjust_payout_for_cost_correction("CD-NO-PAYOUT", 100))


	def test_a_cost_correction_looks_only_at_the_current_acquisition(self):
		"""A serial that was bought, sold and bought again has two acquisitions, each with its own payment."""
		old = self._acquisition("CD-RE", cost=200)
		legacy_payout(old, "Cash")
		frappe.db.set_value("FFL Acquisition", old, "acquisition_date", add_days(nowdate(), -30), update_modified=False)
		new = self._acquisition("CD-RE", cost=300, item="CD-GUN-B")
		legacy_payout(new, "Zelle")
		je = cd.adjust_payout_for_cost_correction("CD-RE", 350)
		self.assertEqual(self._lines(je), {f"Stock Adjustment - {self.abbr}": (50, 0), f"Operating Bank - {self.abbr}": (0, 50)})
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", {"journal_entry": je}, "acquisition"), new)

	def test_only_a_system_manager_undoes_a_payment_and_its_corrections_go_with_it(self):
		acq = self._acquisition("CD-UN")
		pay = legacy_payout(acq, "Cash")
		cd.adjust_payout_for_cost_correction("CD-UN", 330)
		frappe.set_user(self.manager)
		with self.assertRaises(frappe.PermissionError):
			api.undo_entry(pay.entry)
		frappe.set_user("Administrator")
		before = self._expected()
		cd.void_entry(pay.entry)
		self.assertEqual(self._expected(), before + 330)
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"acquisition": acq, "status": "Posted"}), 0)

	def test_nobody_records_a_first_payment_and_a_system_manager_can_change_one(self):
		"""A gun's own payment is history -- a first one is refused for everyone, one made the old way is
		changed by a System Manager only."""
		acq = self._acquisition("CD-FX")
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.PermissionError):
			api.record_payout(acq, "Cash")
		frappe.set_user(self.manager)
		with self.assertRaisesRegex(frappe.ValidationError, "paid with the gun's receipt now"):
			api.record_payout(acq, "Cash")
		frappe.set_user("Administrator")
		with self.assertRaisesRegex(frappe.ValidationError, "paid with the gun's receipt now"):
			api.record_payout(acq, "Cash")
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"acquisition": acq}), 0)
		first = legacy_payout(acq, "Cash")
		frappe.set_user(self.manager)
		with self.assertRaises(frappe.PermissionError):  # already recorded: changing it is for a System Manager
			api.record_payout(acq, "Zelle")
		frappe.set_user("Administrator")
		cd.adjust_payout_for_cost_correction("CD-FX", 320)
		before = self._expected()
		changed = api.record_payout(acq, "Zelle")
		self.assertNotEqual(changed["entry"], first["entry"])
		self.assertEqual(changed["amount"], 320)  # the net paid, corrections included
		self.assertEqual(self._lines(changed["journal_entry"]),
			{f"Stock Adjustment - {self.abbr}": (320, 0), f"Operating Bank - {self.abbr}": (0, 320)})
		self.assertEqual(self._expected(), before + 320)  # the cash is back in the drawer
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"acquisition": acq, "status": "Posted"}), 1)


	def test_a_purchase_is_either_paid_out_or_spent_as_a_credit(self):
		from types import SimpleNamespace

		from ffl_core.compliance.trade_in_credit import validate_trade_in_credit_rows
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

		paid = self._acquisition("CD-EX1")
		legacy_payout(paid, "Cash")
		row = SimpleNamespace(item_code=TRADE_IN_CREDIT_ITEM, ffl_trade_in_acquisition=paid, qty=1, rate=-300, idx=1)
		doc = SimpleNamespace(docstatus=1, is_return=0, doctype="Sales Invoice", name="X", items=[row], grand_total=100)
		with self.assertRaises(frappe.ValidationError):  # the gate: a paid-out purchase cannot be a credit
			validate_trade_in_credit_rows(doc)
		credited = self._acquisition("CD-EX2")
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"CD-CR-{frappe.generate_hash(length=6)}", "company": self.company, "docstatus": 1, "is_return": 0})
		inv.db_insert()
		item = frappe.new_doc("POS Invoice Item")
		item.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice", "parentfield": "items",
			"idx": 1, "docstatus": 1, "item_code": TRADE_IN_CREDIT_ITEM, "ffl_trade_in_acquisition": credited})
		item.db_insert()
		with self.assertRaises(frappe.ValidationError):  # and a credited one cannot be paid out
			cd.record_payout(credited, "Cash")

	def test_a_purchase_from_before_cash_tracking_is_not_paid_out_afterwards(self):
		acq = self._acquisition("CD-OLD")
		frappe.db.set_single_value("Cash Drawer Settings", "tracking_started", add_to_date(now_datetime(), hours=1))
		with self.assertRaisesRegex(frappe.ValidationError, "before cash tracking started"):
			cd.record_payout(acq, "Cash")
		frappe.db.set_single_value("Cash Drawer Settings", "tracking_started", add_to_date(now_datetime(), hours=-1))
		made = legacy_payout(acq, "Cash")
		# an undone payment was recorded after the start: it can come back whatever the setting says now
		cd.void_entry(made.entry)
		frappe.db.set_single_value("Cash Drawer Settings", "tracking_started", add_to_date(now_datetime(), hours=1))
		self.assertTrue(cd.record_payout(acq, "Zelle").entry)

	def test_an_undone_payment_comes_back_at_its_corrected_net(self):
		acq = self._acquisition("CD-NET")
		made = legacy_payout(acq, "Cash")
		cd.adjust_payout_for_cost_correction("CD-NET", 330)
		cd.void_entry(made.entry)
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"acquisition": acq, "status": "Posted"}), 0)
		again = cd.record_payout(acq, "Zelle")
		self.assertEqual(again.amount, 330)
		self.assertEqual(self._lines(again.journal_entry),
			{f"Stock Adjustment - {self.abbr}": (330, 0), f"Operating Bank - {self.abbr}": (0, 330)})

	def test_bank_payments_are_listed_for_a_system_manager_but_not_counted_in_the_drawer(self):
		acq = self._acquisition("CD-BNK")
		before = self._expected()
		legacy_payout(acq, "Zelle")
		self.assertEqual(self._expected(), before)
		page = api.get_today(self.company)
		self.assertEqual([(b["acquisition"], b["method"], b["amount"], b["can_fix"]) for b in page["bank_payouts"]], [(acq, "Zelle", 300.0, True)])
		self.assertEqual(page["lines"], [])
		frappe.set_user(self.manager)
		self.assertFalse(api.get_today(self.company)["bank_payouts"][0]["can_fix"])
		frappe.set_user("Administrator")

	def test_a_payment_by_bank_never_goes_through_a_clearing_account(self):
		clearing = _account(self.company, self.abbr, "GunBroker Clearing", "Bank Accounts", "Bank")
		frappe.db.set_value("Company", self.company, "default_bank_account", clearing)
		made = legacy_payout(self._acquisition("CD-CL"), "Zelle")
		lines = self._lines(made.journal_entry)
		self.assertIn(f"Operating Bank - {self.abbr}", lines)
		self.assertNotIn(clearing, lines)

	def test_a_payment_never_touches_sales_tax_debtors_cogs_or_stock(self):
		acq = self._acquisition("CD-S9")
		snapshot = self._protected()
		legacy_payout(acq, "Cash")
		cd.adjust_payout_for_cost_correction("CD-S9", 310)
		self.assertEqual(self._protected(), snapshot)


StoreAPayout = _store_tests("StoreAPayout", "store_a", CashFixtures, PayoutCases)
StoreBPayout = _store_tests("StoreBPayout", "store_b", CashFixtures, PayoutCases)


class TradeInPayout(CashFixtures):
	"""The POS trade-in dialog's endpoint and the cost correction, on the shared dev company (Store A-like: its
	Operating Bank exists, the company's default bank account is empty)."""

	KIND = "store_a"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.abbr = frappe.db.get_value("Company", cls.company, "abbr")
		cls.cash = f"Cash - {cls.abbr}"
		frappe.defaults.set_user_default("Company", cls.company)
		cd.ensure_accounts(cls.company)
		if not frappe.db.exists("Customer", "_Test Trade-in Seller"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test Trade-in Seller",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are about money, not FastBound.
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in trade-in payout tests"))
		p.start()
		self.addCleanup(p.stop)

	def _payload(self, suffix, **over):
		base = {
			"serial_no": f"CDTI-{suffix}", "manufacturer": "Glock", "model_name": "19", "caliber": "9mm", "firearm_type": "Pistol",
			"condition_grade": "Good", "id_type": "Driver License", "id_number": f"DL-{suffix}",
			"source_name": "_Test Trade-in Seller", "source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024", "acquisition_cost": 300,
		}
		base.update(over)
		return base

	def _net(self, account, vouchers):
		return flt(frappe.db.sql(
			"SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE company = %s AND account = %s AND is_cancelled = 0 "
			"AND voucher_no IN %s", (self.company, account, tuple(vouchers)))[0][0], 2)

	def _stock_adjustment_net(self, vouchers):
		return self._net(f"Stock Adjustment - {self.abbr}", vouchers)

	def _srbnb_net(self, vouchers):
		return self._net(cd._srbnb(self.company), vouchers)

	def _unsettled(self, receipt):
		return [r.left for r in cd.unsettled_seller_receipts(add_days(nowdate(), -1), nowdate()) if r.name == receipt]

	def test_a_buy_has_to_say_how_the_seller_was_paid(self):
		"""A5: no payment method (or a bank payment without its reference), no intake (and nothing half-written)."""
		from ffl_core.api import trade_in

		for bad in ({}, {"payout_method": ""}, {"payout_method": "Bitcoin"}, {"payout_method": "Zelle"}):
			with self.assertRaises(frappe.ValidationError):
				trade_in.create_trade_in_intake(self._payload("NOPAY", **bad))
		self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": "CDTI-NOPAY"}))
		self.assertFalse(frappe.db.exists("Serial No", "CDTI-NOPAY"))

	def test_a_cash_buy_is_a_receipt_from_the_seller_paid_with_it(self):
		"""Received on a Purchase Receipt dated today with Source Trade-in, the acquisition still a
		Trade-in, paid Dr Stock Received But Not Billed / Cr Cash; the receipt nets to zero there and Stock
		Adjustment is never touched."""
		from ffl_core.api import trade_in

		before = cd.drawer_expected(self.company).expected
		res = trade_in.create_trade_in_intake(self._payload("CASH", payout_method="Cash", retail_price=499))
		pr = frappe.db.get_value("Purchase Receipt", res["purchase_receipt"], ["posting_date", "ffl_source",
			"ffl_acquisition_source", "ffl_seller_customer", "docstatus"], as_dict=True)
		self.assertEqual((str(pr.posting_date), pr.ffl_source, pr.ffl_acquisition_source, pr.ffl_seller_customer, pr.docstatus),
			(nowdate(), "Trade-in", "Individual", "_Test Trade-in Seller", 1))
		acq = frappe.db.get_value("FFL Acquisition", res["acquisition"], ["source_type", "serial", "purchase_receipt",
			"source_first_name", "source_address", "source_id_number"], as_dict=True)
		self.assertEqual((acq.source_type, acq.serial, acq.purchase_receipt, acq.source_first_name, acq.source_id_number),
			("Trade-in", "CDTI-CASH", res["purchase_receipt"], "Testy", "DL-CASH"))
		self.assertEqual(acq.source_address, "123 Main St, Springfield TX 75024")
		je = res["payout"]["journal_entry"]
		self.assertEqual(self._lines(je), {cd._srbnb(self.company): (300, 0), self.cash: (0, 300)})
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "user_remark"), f"CASH-GOODS {res['purchase_receipt']} (Cash)")
		self.assertEqual(cd.drawer_expected(self.company).expected, before - 300)
		self.assertEqual(self._srbnb_net([res["purchase_receipt"], je]), 0)
		self.assertEqual(self._stock_adjustment_net([res["purchase_receipt"], je]), 0)
		self.assertEqual(self._unsettled(res["purchase_receipt"]), [])
		serial = frappe.db.get_value("Serial No", "CDTI-CASH", ["purchase_rate", "condition_grade", "sell_price", "item_code"],
			as_dict=True)
		self.assertEqual((flt(serial.purchase_rate), serial.condition_grade, flt(serial.sell_price), serial.item_code),
			(300, "Good", 499, res["item_code"]))
		with self.assertRaises(frappe.ValidationError):
			cd.record_payout(res["acquisition"], "Cash")
		# where a credit trade-in lands too
		from ffl_core.api.firearm_intake import _default_warehouse

		self.assertEqual(frappe.db.get_value("Serial No", "CDTI-CASH", "warehouse"), _default_warehouse(self.company))

	def test_a_pay_now_gun_cannot_also_be_spent_as_a_credit(self):
		"""Its seller was paid with the receipt, so a credit line naming it is refused at submit."""
		from types import SimpleNamespace

		from ffl_core.api import trade_in
		from ffl_core.compliance.trade_in_credit import validate_trade_in_credit_rows
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

		res = trade_in.create_trade_in_intake(self._payload("TWICE", payout_method="Cash"))
		row = SimpleNamespace(item_code=TRADE_IN_CREDIT_ITEM, ffl_trade_in_acquisition=res["acquisition"], qty=1, rate=-300,
			idx=1)
		doc = SimpleNamespace(docstatus=1, is_return=0, doctype="POS Invoice", name="X", items=[row], grand_total=100)
		with self.assertRaisesRegex(frappe.ValidationError, "already paid out"):
			validate_trade_in_credit_rows(doc)

	def test_a_pay_now_gun_on_a_shared_sku_leaves_the_sku_alone(self):
		"""Like a credit trade-in, the gun's own model/caliber go on its serial, not the shared SKU."""
		from ffl_core.api import trade_in

		sku = "CDTI-SHARED-SKU"
		if not frappe.db.exists("Item", sku):
			from ffl_core.api.firearm_intake import _create_model_item

			_create_model_item(sku, {"manufacturer": "Glock", "model_name": "19", "caliber": "9mm", "firearm_type": "Pistol"})
		before = frappe.db.get_value("Item", sku, ["model_name", "caliber"])
		trade_in.create_trade_in_intake(self._payload("SKU", payout_method="Cash", item_code=sku, model_name="19 Gen4",
			caliber="9x19"))
		self.assertEqual(frappe.db.get_value("Item", sku, ["model_name", "caliber"]), before)
		self.assertEqual(frappe.db.get_value("Serial No", "CDTI-SKU", ["item_code", "model_name", "caliber"]),
			(sku, "19 Gen4", "9x19"))

	def test_a_pay_now_buy_that_pays_nothing_is_refused_before_anything_is_written(self):
		"""A cost that rounds to $0.00 would make a receipt that pays nobody."""
		from ffl_core.api import trade_in

		with self.assertRaisesRegex(frappe.ValidationError, "what the store pays"):
			trade_in.create_trade_in_intake(self._payload("ZERO", payout_method="Cash", acquisition_cost=0.004))
		self.assertFalse(frappe.db.exists("Serial No", "CDTI-ZERO"))

	def test_a_cash_payment_drops_a_reference_left_from_another_method(self):
		"""A Cash payout with a payout reference leaves the drawer entry's reference empty."""
		from ffl_core.api import trade_in

		res = trade_in.create_trade_in_intake(self._payload("REF", payout_method="Cash", payout_reference="ZL-123"))
		self.assertFalse(frappe.db.get_value("Cash Drawer Entry", res["payout"]["entry"], "reference"))

	def test_the_trade_in_source_is_matched_however_it_is_spelled(self):
		from ffl_core.acquisition import is_trade_in_source

		self.assertTrue(all(is_trade_in_source(x) for x in ("Trade-in", " trade-IN ", "TRADE-IN")))
		self.assertFalse(any(is_trade_in_source(x) for x in ("", None, "Individual (trade-in)", "Trade In")))

	def test_inventory_receipts_files_a_pay_now_buy_as_a_trade_in(self):
		"""The receipt is a Trade-in on Inventory Receipts, sourced to the seller."""
		from ffl_core.api import trade_in
		from ffl_core.ffl_core.report.inventory_receipts import inventory_receipts as report

		res = trade_in.create_trade_in_intake(self._payload("IR", payout_method="Cash"))
		_cols, rows, *_ = report.execute({"from_date": nowdate(), "to_date": nowdate(), "view": "Units"})
		row = next(r for r in rows if r.get("serial_no") == "CDTI-IR")
		self.assertEqual((row["receipt_class"], row["voucher_no"]), ("Trade-in", res["purchase_receipt"]))
		self.assertEqual(row["source"], frappe.db.get_value("Customer", "_Test Trade-in Seller", "customer_name"))

	def test_a_zelle_buy_leaves_the_drawer_alone(self):
		from ffl_core.api import trade_in

		before = cd.drawer_expected(self.company).expected
		res = trade_in.create_trade_in_intake(self._payload("ZEL", payout_method="Zelle", payout_reference="ZL-1"))
		self.assertEqual(self._lines(res["payout"]["journal_entry"]),
			{cd._srbnb(self.company): (300, 0), f"Operating Bank - {self.abbr}": (0, 300)})
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", res["payout"]["entry"], "reference"), "ZL-1")
		self.assertEqual(cd.drawer_expected(self.company).expected, before)

	def test_a_buy_applied_as_credit_pays_nobody(self):
		"""The register's trade-in credit is untouched: a draft acquisition, a credit line, no receipt, no payment."""
		from ffl_core.api import trade_in

		res = trade_in.create_trade_in_intake(self._payload("CRED", apply_credit=1))
		self.assertIn("credit", res)
		self.assertNotIn("payout", res)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", res["acquisition"], ["docstatus", "source_type",
			"purchase_receipt"]), (0, "Trade-in", None))
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"acquisition": res["acquisition"]}))

	def test_a_serial_already_on_file_is_refused_before_anything_is_written(self):
		from ffl_core.api import trade_in

		trade_in.create_trade_in_intake(self._payload("DUP", payout_method="Cash"))
		receipts = frappe.db.count("Purchase Receipt")
		with self.assertRaisesRegex(frappe.ValidationError, "already on file"):
			trade_in.create_trade_in_intake(self._payload("DUP", payout_method="Cash"))
		self.assertEqual(frappe.db.count("Purchase Receipt"), receipts)

	def test_a_refund_from_the_seller_on_a_cost_correction_settles_like_the_cpa_example(self):
		"""Bought for 300 cash, corrected to 280 and the seller gave 20 back: Dr Stock Adjustment 20 / Cr Stock
		In Hand 20, then Dr Cash 20 / Cr Stock Adjustment 20 -- stock 280, cash -280, SRBNB 0, Stock Adjustment 0."""
		from ffl_core.api import cost_correction, trade_in

		res = trade_in.create_trade_in_intake(self._payload("CPA", payout_method="Cash"))
		before = cd.drawer_expected(self.company).expected
		fixed = cost_correction.correct_serial_cost("CDTI-CPA", 280, "seller gave 20 back", payout_was_different=1)
		self.assertEqual(self._lines(fixed["payout_adjustment"]), {f"Stock Adjustment - {self.abbr}": (0, 20), self.cash: (20, 0)})
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", {"journal_entry": fixed["payout_adjustment"]},
			"purchase_receipt"), res["purchase_receipt"])
		vouchers = [res["purchase_receipt"], res["payout"]["journal_entry"], fixed["payout_adjustment"], fixed["stock_reconciliation"]]
		self.assertEqual(self._stock_adjustment_net(vouchers), 0)
		self.assertEqual(self._srbnb_net(vouchers), 0)
		self.assertEqual(self._unsettled(res["purchase_receipt"]), [])
		self.assertEqual(cd.drawer_expected(self.company).expected, before + 20)
		self.assertEqual(flt(frappe.db.get_value("Serial No", "CDTI-CPA", "purchase_rate")), 280)
		back = cost_correction.correct_serial_cost("CDTI-CPA", 300, "it was 300 after all", payout_was_different=1)
		self.assertEqual(self._lines(back["payout_adjustment"]), {f"Stock Adjustment - {self.abbr}": (20, 0), self.cash: (0, 20)})

	def test_an_undone_payment_shows_the_receipt_as_not_settled_until_it_is_recorded_again(self):
		"""Undone -> the receipt owes its seller (-300) on the month-end check; recorded again -> settled."""
		from ffl_core.api import trade_in

		res = trade_in.create_trade_in_intake(self._payload("UNS", payout_method="Cash"))
		cd.void_entry(res["payout"]["entry"])
		self.assertEqual(self._unsettled(res["purchase_receipt"]), [-300])
		cd.record_goods_payout(res["purchase_receipt"], "Cash")
		self.assertEqual(self._unsettled(res["purchase_receipt"]), [])

	def test_a_payment_left_on_a_cancelled_receipt_shows_as_not_settled(self):
		"""The money left but the receipt was cancelled (+300) until the payment is undone."""
		from ffl_core.api import trade_in

		res = trade_in.create_trade_in_intake(self._payload("CXL", payout_method="Cash"))
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		pr.flags.ignore_permissions = True
		pr.cancel()
		self.assertEqual(self._unsettled(pr.name), [300])
		cd.void_entry(res["payout"]["entry"])
		self.assertEqual(self._unsettled(pr.name), [])

	def test_a_ticked_box_with_nothing_to_book_says_so(self):
		from ffl_core.api import cost_correction, trade_in

		trade_in.create_trade_in_intake(self._payload("NOTE", payout_method="Cash"))
		first = cost_correction.correct_serial_cost("CDTI-NOTE", 350, "typo", payout_was_different=1)
		self.assertTrue(first["payout_adjustment"])
		self.assertIsNone(first["payout_note"])
		again = cost_correction.correct_serial_cost("CDTI-NOTE", 350, "same again", payout_was_different=1)
		self.assertIsNone(again["payout_adjustment"])
		self.assertIn("Nothing was booked", again["payout_note"])

	def test_a_cost_correction_moves_no_cash_unless_the_payment_was_different(self):
		from ffl_core.api import cost_correction, trade_in

		trade_in.create_trade_in_intake(self._payload("QUIET", payout_method="Cash"))
		before = cd.drawer_expected(self.company).expected
		fixed = cost_correction.correct_serial_cost("CDTI-QUIET", 350, "refurbished, cost went up")
		self.assertIsNone(fixed["payout_adjustment"])
		self.assertEqual(cd.drawer_expected(self.company).expected, before)
		listed = cost_correction.list_item_serials_for_cost(frappe.db.get_value("Serial No", "CDTI-QUIET", "item_code"))
		self.assertTrue(next(r for r in listed["serials"] if r["serial"] == "CDTI-QUIET")["cash_payout"])
