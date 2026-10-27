"""Memberships (ffl_core/membership.py): plans, sign-up invoices, deferred revenue, activation on
payment, cancel, the register guard and the account setup.

Site tests on the bootstrap company ("FFL Test") against the real doctypes and ERPNext's own
deferred revenue processing; nothing is stubbed but the clock (``membership.nowdate``).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, getdate

from ffl_core import membership as ms
from ffl_core.api import manual_order
from ffl_core.setup import membership as setup
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_customer_profile import _customer, _uniq, _user

COMPANY = "FFL Test"
AS = setup.ACCOUNTS_SETTINGS


def _item(prefix: str, group: str = "Service") -> str:
	code = f"MEM-{prefix}-{frappe.generate_hash(length=6)}".upper()
	frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": group,
		"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 0,
		"include_item_in_manufacturing": 0}).insert(ignore_permissions=True)
	return code


def _plan(period=ms.MONTHLY, price=30, fee=100, registration_item=None, **kw) -> frappe._dict:
	doc = frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("Plan"), "active": 1, "billing_period": period,
		"price": price, "item": _item("DUES"), "registration_fee": fee,
		"registration_item": (registration_item or _item("REG")) if fee else None,
		"auto_renew_default": 0, **kw})
	doc.insert(ignore_permissions=True)
	return doc


def _sell(customer, plan, start="2026-11-15", auto_renew=0) -> tuple[str, str]:
	"""Start + invoice a membership on *start*; returns (membership, sales invoice)."""
	with patch("ffl_core.membership.nowdate", return_value=start):
		m = ms.start_membership(customer, plan, auto_renew=auto_renew)["name"]
		si = ms.invoice_membership(m)["sales_invoice"]
	return m, si


def _pay(si, amount=None) -> None:
	owed = flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount"))
	manual_order.record_payment(si, "Cash", amount=owed if amount is None else amount)


def _gl(voucher, account) -> tuple[float, float]:
	row = frappe.db.sql("""select sum(debit), sum(credit) from `tabGL Entry` where voucher_no=%s and account=%s
		and is_cancelled=0""", (voucher, account))[0]
	return flt(row[0]), flt(row[1])


class _Base(SettingsSandbox, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup.ensure()
		cls.deferred, cls.income = setup.accounts(COMPANY)
		cls.plan = _plan()

	def setUp(self):
		frappe.set_user("Administrator")
		self.customer = _customer().name


class Periods(FrappeTestCase):
	def test_monthly_and_yearly_period_ends(self):
		cases = [
			("2026-11-15", ms.MONTHLY, date(2026, 12, 14)),
			("2026-12-01", ms.MONTHLY, date(2026, 12, 31)),
			# A day the next month lacks clamps to its last day, then − 1.
			("2027-01-31", ms.MONTHLY, date(2027, 2, 27)),
			("2027-01-30", ms.MONTHLY, date(2027, 2, 27)),
			("2028-01-31", ms.MONTHLY, date(2028, 2, 28)),     # leap year: Feb 29 − 1
			("2026-11-15", ms.YEARLY, date(2027, 11, 14)),
			("2028-02-29", ms.YEARLY, date(2029, 2, 27)),
		]
		for start, period, end in cases:
			self.assertEqual(ms.period_end(start, period), end, (start, period))


class AccountSetup(SettingsSandbox, FrappeTestCase):
	def test_accounts_created_once_and_settings_untouched(self):
		self.set_single(AS, "book_deferred_entries_based_on", "Months")
		self.set_single(AS, "automatically_process_deferred_accounting_entry", 0)
		self.set_single(AS, "book_deferred_entries_via_journal_entry", 0)
		setup.ensure()
		self.assertEqual(setup.ensure()["accounts"], 0)              # idempotent on re-migrate
		deferred, income = setup.accounts(COMPANY)
		info = frappe.db.get_value("Account", deferred, ["root_type", "parent_account"], as_dict=True)
		self.assertEqual(info.root_type, "Liability")
		self.assertTrue(info.parent_account.startswith("Current Liabilities"), info.parent_account)
		self.assertEqual(frappe.db.get_value("Account", income, "root_type"), "Income")
		# after_migrate never touches the store's Accounts Settings.
		self.assertEqual(frappe.db.get_single_value(AS, "book_deferred_entries_based_on"), "Months")
		self.assertEqual(frappe.db.get_single_value(AS, "automatically_process_deferred_accounting_entry"), 0)

	def test_apply_deferred_settings_never_toggles_the_journal_entry_switch(self):
		for via_je in (0, 1):
			self.set_single(AS, "book_deferred_entries_based_on", "Months")
			self.set_single(AS, "automatically_process_deferred_accounting_entry", 0)
			self.set_single(AS, "book_deferred_entries_via_journal_entry", via_je)
			self.assertEqual(len(setup.apply_deferred_settings()), 2)
			self.assertEqual(frappe.db.get_single_value(AS, "book_deferred_entries_based_on"), "Days")
			self.assertEqual(frappe.db.get_single_value(AS, "automatically_process_deferred_accounting_entry"), 1)
			self.assertEqual(frappe.db.get_single_value(AS, "book_deferred_entries_via_journal_entry"), via_je)
			self.assertEqual(setup.apply_deferred_settings(), [])

	def test_first_active_plan_applies_the_settings_later_plans_do_not(self):
		setup.ensure()
		frappe.db.savepoint("plans")
		try:
			frappe.db.sql("update `tabMembership Plan` set active = 0")
			self.set_single(AS, "book_deferred_entries_based_on", "Months")
			self.set_single(AS, "automatically_process_deferred_accounting_entry", 0)
			_plan()
			self.assertEqual(frappe.db.get_single_value(AS, "book_deferred_entries_based_on"), "Days")
			self.set_single(AS, "book_deferred_entries_based_on", "Months")
			_plan()                                                      # another active plan exists
			self.assertEqual(frappe.db.get_single_value(AS, "book_deferred_entries_based_on"), "Months")
		finally:
			frappe.db.rollback(save_point="plans")


class PlanRules(_Base):
	def test_plan_item_gets_the_accounts_deferral_and_price(self):
		p = self.plan
		item = frappe.get_doc("Item", p.item)
		self.assertEqual((item.enable_deferred_revenue, item.no_of_months), (1, 1))
		row = next(r for r in item.item_defaults if r.company == COMPANY)
		self.assertEqual((row.income_account, row.deferred_revenue_account), (self.income, self.deferred))
		self.assertEqual(flt(frappe.db.get_value("Item Price", {"item_code": p.item, "selling": 1},
			"price_list_rate")), 30)
		self.assertEqual(flt(frappe.db.get_value("Item Price", {"item_code": p.registration_item, "selling": 1},
			"price_list_rate")), 100)

	def test_plan_validation(self):
		with self.assertRaises(frappe.ValidationError):        # the item is already this plan's
			frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("Dup"),
				"billing_period": ms.MONTHLY, "price": 5, "item": self.plan.item}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):        # grace on needs days
			_plan(grace_enabled=1, grace_days=0)
		with self.assertRaises(frappe.ValidationError):        # a fee needs its item
			frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("NoReg"), "billing_period": ms.MONTHLY,
				"price": 5, "item": _item("X"), "registration_fee": 10}).insert(ignore_permissions=True)
		stock = frappe.get_doc({"doctype": "Item", "item_code": _uniq("STK"), "item_group": "Products",
			"stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):        # a stock item is not a service
			frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("Stock"), "billing_period": ms.MONTHLY,
				"price": 5, "item": stock.name}).insert(ignore_permissions=True)
		self.assertEqual(_plan(grace_enabled=1, grace_days=3).grace_days, 3)


class RegisterGuard(_Base):
	def test_pos_invoice_with_a_membership_item_is_refused(self):
		for code in (self.plan.item, self.plan.registration_item):
			doc = frappe._dict(items=[frappe._dict(item_code=code)])
			with self.assertRaises(frappe.ValidationError) as ctx:
				ms.refuse_on_pos_invoice(doc)
			self.assertIn("Memberships panel", str(ctx.exception))
		ms.refuse_on_pos_invoice(frappe._dict(items=[frappe._dict(item_code=_item("OTHER"))]))   # no raise

	def test_real_pos_invoice_is_refused_at_validate(self):
		from ffl_core.tests.pos_opening import todays_pos_opening
		profile = "FFL Test"
		todays_pos_opening(COMPANY, profile)
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": profile,
			"customer": self.customer, "items": [{"item_code": self.plan.item, "qty": 1, "rate": 30}]})
		inv.set_missing_values()
		with self.assertRaises(frappe.ValidationError) as ctx:
			inv.insert(ignore_permissions=True)
		self.assertIn("Memberships panel", str(ctx.exception))

	def test_no_plan_no_change(self):
		frappe.db.savepoint("noplan")
		try:
			frappe.db.sql("delete from `tabMembership Plan`")
			ms.refuse_on_pos_invoice(frappe._dict(items=[frappe._dict(item_code=self.plan.item)]))   # no raise
			self.assertEqual(ms.get_memberships(self.customer), {"plans": [], "memberships": [],
				"cards": ms.get_memberships(self.customer)["cards"], "can_cancel": True})
		finally:
			frappe.db.rollback(save_point="noplan")


class SignUp(_Base):
	def test_invoice_lines_dates_and_ledger(self):
		m, si_name = _sell(self.customer, self.plan.name)
		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual((si.docstatus, si.is_pos, getdate(si.posting_date)), (1, 0, date(2026, 11, 15)))
		self.assertEqual([(r.item_code, flt(r.rate)) for r in si.items],
			[(self.plan.item, 30), (self.plan.registration_item, 100)])
		for r in si.items:
			self.assertEqual((r.enable_deferred_revenue, getdate(r.service_start_date), getdate(r.service_end_date)),
				(1, date(2026, 11, 15), date(2026, 12, 14)))
			self.assertEqual((r.deferred_revenue_account, r.income_account), (self.deferred, self.income))
		# Dr receivable / Cr Deferred Membership Revenue; nothing is income on the day of sale.
		self.assertEqual(_gl(si.name, self.deferred), (0, 130))
		self.assertEqual(_gl(si.name, self.income), (0, 0))
		self.assertEqual(_gl(si.name, si.debit_to)[0], flt(si.grand_total))
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, len(doc.invoices), doc.invoices[0].status), (ms.PENDING, 1, ms.UNPAID))
		# Asking again hands back the same unpaid invoice.
		self.assertEqual(ms.invoice_membership(m)["sales_invoice"], si.name)

	def test_unpaid_stays_pending_paid_turns_active(self):
		m, si = _sell(self.customer, self.plan.name)
		_pay(si, amount=50)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.PENDING)
		_pay(si)
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, getdate(doc.start_date), getdate(doc.current_period_start),
			getdate(doc.current_period_end), doc.invoices[0].status),
			(ms.ACTIVE, date(2026, 11, 15), date(2026, 11, 15), date(2026, 12, 14), ms.PAID))

	def test_paid_with_auto_renew_but_no_saved_card_turns_auto_renew_off(self):
		m, si = _sell(self.customer, self.plan.name, auto_renew=1)
		_pay(si)
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, doc.auto_renew, doc.saved_card), (ms.ACTIVE, 0, None))
		self.assertIn("auto-renew is off", doc.last_charge_status)

	def test_registration_only_on_the_first_membership_of_the_plan(self):
		# A dropped sign-up never started: the next one still pays the fee.
		m0, si0 = _sell(self.customer, self.plan.name)
		ms.discard_membership(m0, "changed mind")
		self.assertEqual(frappe.db.get_value("Sales Invoice", si0, "docstatus"), 2)
		m1, si1 = _sell(self.customer, self.plan.name)
		self.assertEqual(len(frappe.get_doc("Sales Invoice", si1).items), 2)
		_pay(si1)
		with patch("ffl_core.membership.nowdate", return_value="2026-11-20"):
			ms.cancel_membership(m1, "moving away", end_now=1)
		m2, si2 = _sell(self.customer, self.plan.name, start="2026-12-01")
		self.assertEqual([r.item_code for r in frappe.get_doc("Sales Invoice", si2).items], [self.plan.item])

	def test_one_live_membership_per_customer_per_plan(self):
		a = ms.start_membership(self.customer, self.plan.name)["name"]
		self.assertEqual(ms.start_membership(self.customer, self.plan.name, auto_renew=1)["name"], a)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, a, "auto_renew"), 1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": ms.DOCTYPE, "customer": self.customer, "plan": self.plan.name,
				"status": ms.PENDING}).insert(ignore_permissions=True)
		_m, si = _sell(self.customer, self.plan.name)
		_pay(si)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ms.start_membership(self.customer, self.plan.name)
		self.assertIn("already has", str(ctx.exception))
		# Another plan is another membership.
		other = _plan(fee=0)
		self.assertTrue(ms.start_membership(self.customer, other.name)["name"])

	def test_walk_in_and_switched_off_plan_refused(self):
		with patch("ffl_core.customer_profile._system_customer_names", return_value={self.customer}):
			with self.assertRaises(frappe.ValidationError):
				ms.start_membership(self.customer, self.plan.name)
		off = _plan(fee=0, active=0)
		with self.assertRaises(frappe.ValidationError):
			ms.start_membership(self.customer, off.name)

	def test_terms_signed_for_the_membership_before_invoicing(self):
		from ffl_core.agreements import request_agreement
		tpl = frappe.get_doc({"doctype": "Agreement Template", "title": _uniq("Member terms"),
			"body": "<p>{{ customer_name }} joins {{ plan }} at {{ price }} {{ period }}; next {{ next_charge_date }}.</p>",
			"scope": "Per document", "requirement": "Optional"}).insert(ignore_permissions=True)
		plan = _plan(fee=0, terms_template=tpl.name)
		with patch("ffl_core.membership.nowdate", return_value="2026-11-15"):
			row = ms.start_membership(self.customer, plan.name, auto_renew=1)
			self.assertEqual(row["terms_state"], "missing")
			with self.assertRaises(frappe.ValidationError):
				ms.invoice_membership(row["name"])
			r = request_agreement(self.customer, tpl.name, deliver="none", reference_doctype=ms.DOCTYPE,
				reference_name=row["name"])
			body = frappe.db.get_value("Customer Agreement", r["name"], "body_snapshot")
			self.assertIn(plan.name, body)
			self.assertIn("$30", body.replace(" ", ""))
			self.assertIn(frappe.format_value("2026-12-15", {"fieldtype": "Date"}), body)   # next charge
			frappe.db.set_value("Customer Agreement", r["name"], "status", "Signed")
			self.assertTrue(ms.invoice_membership(row["name"])["sales_invoice"])


class DeferredRevenue(_Base):
	def _process_november(self):
		doc = frappe.get_doc({"doctype": "Process Deferred Accounting", "company": COMPANY, "type": "Income",
			"account": self.deferred, "posting_date": "2026-11-30", "start_date": "2026-11-01",
			"end_date": "2026-11-30"})
		doc.insert(ignore_permissions=True)
		doc.submit()

	def _recognized(self, si) -> float:
		return flt(frappe.db.sql("""select sum(credit) - sum(debit) from `tabGL Entry` where account=%s
			and is_cancelled=0 and posting_date between '2026-11-01' and '2026-11-30' and (voucher_no=%s or voucher_no in
			(select parent from `tabJournal Entry Account` where reference_name=%s))""",
			(self.income, si, si))[0][0])

	def test_november_recognizes_sixteen_of_thirty_days(self):
		"""$30 monthly + $100 registration from 11-15 (11-15..12-14, 30 days): November = 16 days."""
		for via_je in (0, 1):
			self.set_single(AS, "book_deferred_entries_based_on", "Days")
			self.set_single(AS, "book_deferred_entries_via_journal_entry", via_je)
			self.set_single(AS, "submit_journal_entries", 1)
			_m, si = _sell(_customer().name, self.plan.name)
			self._process_november()
			self.assertAlmostEqual(self._recognized(si), flt(30 * 16 / 30, 2) + flt(100 * 16 / 30, 2), places=2,
				msg=f"via journal entry = {via_je}")

	def test_end_now_sets_the_stop_date_and_keeps_the_invoice(self):
		m, si = _sell(self.customer, self.plan.name)
		_pay(si)
		with patch("ffl_core.membership.nowdate", return_value="2026-11-20"):
			row = ms.cancel_membership(m, "moved away", end_now=1)
		self.assertEqual(row["status"], ms.CANCELLED)
		doc = frappe.get_doc("Sales Invoice", si)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual({getdate(r.service_stop_date) for r in doc.items}, {date(2026, 11, 20)})
		# November then recognizes everything still deferred (the stop date is the last service day).
		self.set_single(AS, "book_deferred_entries_based_on", "Days")
		self.set_single(AS, "submit_journal_entries", 1)
		self._process_november()
		self.assertAlmostEqual(self._recognized(si), 130, places=2)


class Cancel(_Base):
	def _active(self):
		m, si = _sell(self.customer, self.plan.name)
		_pay(si)
		return m, si

	def test_cancel_at_period_end_then_closed_after_it(self):
		m, si = self._active()
		with patch("ffl_core.membership.nowdate", return_value="2026-11-20"):
			row = ms.cancel_membership(m, "not renewing")
		self.assertEqual((row["status"], row["cancel_at_period_end"], row["auto_renew"], row["next_charge_date"]),
			(ms.ACTIVE, 1, 0, None))
		self.assertFalse(any(r.service_stop_date for r in frappe.get_doc("Sales Invoice", si).items))
		with patch("ffl_core.membership.nowdate", return_value="2026-12-14"):
			ms.close_ended_memberships()
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.ACTIVE)
		with patch("ffl_core.membership.nowdate", return_value="2026-12-15"):
			ms.close_ended_memberships()
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.CANCELLED)
		with self.assertRaises(frappe.ValidationError):
			ms.set_auto_renew(m, 1)

	def test_cancel_needs_a_manager_and_a_reason(self):
		m, _si = self._active()
		with self.assertRaises(frappe.ValidationError):
			ms.cancel_membership(m, "  ")
		frappe.set_user(_user("member-clerk@example.com", "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			ms.cancel_membership(m, "no")
		# The counter may still switch auto-renew off.
		self.assertEqual(ms.set_auto_renew(m, 0)["auto_renew"], 0)

	def test_membership_invoice_is_not_cancelled_from_the_invoice(self):
		_m, si = self._active()
		doc = frappe.get_doc("Sales Invoice", si)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ms.refuse_invoice_cancel(doc)
		self.assertIn("credit note", str(ctx.exception))

	def test_drop_refused_once_paid_in_part(self):
		m, si = _sell(self.customer, self.plan.name)
		_pay(si, amount=10)
		with self.assertRaises(frappe.ValidationError):
			ms.discard_membership(m, "oops")
		self.assertEqual(frappe.db.get_value("Sales Invoice", si, "docstatus"), 1)

	def test_auto_renew_on_needs_a_recurring_card(self):
		m, _si = self._active()
		with self.assertRaises(frappe.ValidationError):
			ms.set_auto_renew(m, 1)
		self.assertEqual(ms.saved_card_usage(_si), None)

	def test_panel_lists_and_heals(self):
		m, si = _sell(self.customer, self.plan.name)
		# Paid by a path that does not run the Payment Entry hook: the panel catches up.
		frappe.db.set_value("Sales Invoice", si, "outstanding_amount", 0)
		out = ms.get_memberships(self.customer)
		row = next(r for r in out["memberships"] if r["name"] == m)
		self.assertEqual(row["status"], ms.ACTIVE)
		self.assertTrue(any(p.name == self.plan.name for p in out["plans"]))
		frappe.set_user(_user("member-clerk2@example.com", "Sales User"))
		self.assertFalse(ms.get_memberships(self.customer)["can_cancel"])
