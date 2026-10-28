"""Membership renewals end to end (ffl_core/membership.py → payments/saved_cards.py): the daily job
and Retry now charge the member's saved RECURRING card through the real card-on-file service and
the real schema (Sales Invoices, Card Terminal Payments, Payment Entries, credit notes, deferred
revenue). Only the Payroc client (the gateway) and the clock (``membership.nowdate``) are mocked.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import frappe
import requests
from frappe.utils import add_to_date, flt, getdate, now_datetime

from ffl_core import membership as ms
from ffl_core.setup import membership as membership_setup
from ffl_core.tests.test_membership import _pay, _sell
from ffl_integrations.payments import api, saved_cards
from ffl_integrations.tests.site_fixtures import payroc_configured
from ffl_integrations.tests.test_card_on_file import TOKEN, _CardOnFileBase, _payment, _uniq

_PAYROC_CONFIGURED = payroc_configured()
AS = membership_setup.ACCOUNTS_SETTINGS


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


@contextmanager
def _on(day):
	with patch("ffl_core.membership.nowdate", return_value=day):
		yield


def _plan(grace_days=0, period=ms.MONTHLY, price=30) -> str:
	codes = []
	for prefix in ("RNW", "RNWREG"):
		code = f"{prefix}-{frappe.generate_hash(length=8)}".upper()
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Service",
			"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1}).insert(ignore_permissions=True)
		codes.append(code)
	return frappe.get_doc({"doctype": ms.PLAN, "plan_name": _uniq("Renewal plan"), "active": 1,
		"billing_period": period, "price": price, "item": codes[0], "registration_item": codes[1],
		"registration_fee": 100, "grace_enabled": 1 if grace_days else 0, "grace_days": grace_days or None,
	}).insert(ignore_permissions=True).name


@patch("ffl_integrations.payroc.adapter.PayrocClient")
class MembershipRenewal(_CardOnFileBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		membership_setup.ensure()
		cls.plan = _plan()
		cls.grace_plan = _plan(grace_days=3)

	def setUp(self):
		super().setUp()
		# Each test on its own: the job walks every due membership on the site.
		frappe.db.savepoint("renewal_test")
		self.addCleanup(frappe.db.rollback, save_point="renewal_test")

	# ---------------------------------------------------------------- helpers

	def _member(self, plan=None):
		"""An Active auto-renewing member from 11-15 (period 11-15..12-14) with a RECURRING card."""
		cust = self._customer()
		frappe.db.set_value("Customer", cust, "email_id", f"{frappe.generate_hash(length=8)}@example.com")
		m, si = _sell(cust, plan or self.plan)
		_pay(si)
		card = self._card(cust)
		ms.change_card(m, card)
		self.assertEqual(ms.set_auto_renew(m, 1)["auto_renew"], 1)
		return m, card

	@staticmethod
	def _card(cust, token=TOKEN) -> str:
		return frappe.get_doc({"doctype": saved_cards.SAVED_CARD, "customer": cust, "processor": "payroc",
			"token": token, "merchant_reference": f"t-{frappe.generate_hash(length=10)}", "card_summary": "Visa •1111",
			"card_expiry": "1229", "usage": "RECURRING", "status": "Active", "is_default": 1,
			"initial_reference": "REF0000001", "authorization": saved_cards.card_on_file_offer(cust)["agreement"],
		}).insert(ignore_permissions=True).name

	@staticmethod
	def _approve_all(client):
		def charge(**kw):
			return _payment(f"REF-{frappe.generate_hash(length=6)}", kw["amount"])
		client.charge_stored_credential.side_effect = charge

	@staticmethod
	def _decline_all(client):
		client.charge_stored_credential.side_effect = None
		client.charge_stored_credential.return_value = _payment("REF-D", 0, code="D", token=None)

	@staticmethod
	def _renewals(m):
		"""The membership's renewal invoice rows (every row after the sign-up's)."""
		return frappe.get_all(ms.INVOICE_ROW, filters={"parent": m, "parenttype": ms.DOCTYPE},
			fields=["sales_invoice", "period_start", "period_end", "status", "credit_note"], order_by="idx asc")[1:]

	@staticmethod
	def _doc(m):
		return frappe.get_doc(ms.DOCTYPE, m)

	def _open(self, m):
		return [r for r in self._renewals(m) if r.status == ms.UNPAID]

	# ---------------------------------------------------------------- approved

	def test_approved_renewal_pays_and_advances_exactly_one_month(self, cc):
		client = cc.from_settings.return_value
		m, card = self._member()
		seen = {}

		def charge(**kw):
			# The charge record is written (and committed) before the gateway is called.
			seen["rows"] = frappe.get_all(api.TERMINAL_PAYMENT, filters={"sales_invoice": kw["order_id"]},
				fields=["status", "saved_card"])
			return _payment("REF-R1", kw["amount"])
		client.charge_stored_credential.side_effect = charge
		with _on("2026-12-14"):
			ms.renew_memberships()                       # the period has not ended yet
		client.charge_stored_credential.assert_not_called()
		with _on("2026-12-15"), patch("ffl_core.receipts.frappe.enqueue") as enqueue:
			ms.renew_memberships()
		(row,) = self._renewals(m)
		self.assertEqual(seen["rows"], [{"status": "IN_PROGRESS", "saved_card": card}])
		self.assertEqual(client.charge_stored_credential.call_args.kwargs["usage"], "RECURRING")
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		self.assertEqual((si.docstatus, getdate(si.posting_date), flt(si.outstanding_amount)), (1, date(2026, 12, 15), 0))
		# The plan line only (no registration fee on a renewal), from the old end + 1, one month long.
		self.assertEqual([(r.item_code, getdate(r.service_start_date), getdate(r.service_end_date)) for r in si.items],
			[(frappe.db.get_value(ms.PLAN, self.plan, "item"), date(2026, 12, 15), date(2027, 1, 14))])
		doc = self._doc(m)
		self.assertEqual((doc.status, getdate(doc.current_period_start), getdate(doc.current_period_end), row.status),
			(ms.ACTIVE, date(2026, 12, 15), date(2027, 1, 14), ms.PAID))
		# One receipt, for the paid renewal (none at submit: the card could still have declined).
		receipts = [c for c in enqueue.call_args_list if "send_sale_receipt" in str(c)]
		self.assertEqual([c.kwargs["name"] for c in receipts], [si.name])
		self.assertTrue(ms.is_member(doc.customer, self.plan))
		# Twice the same day, and the next day: nothing more is charged.
		with _on("2026-12-15"):
			ms.renew_memberships()
		with _on("2026-12-16"):
			ms.renew_memberships()
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		self.assertEqual(len(self._renewals(m)), 1)
		self._assert_no_token_anywhere(ms.get_memberships(doc.customer), ms.needs_attention())

	def test_a_31st_start_bills_on_the_last_day_of_february_then_the_31st(self, cc):
		client = cc.from_settings.return_value
		self._approve_all(client)
		cust = self._customer()
		m, si = _sell(cust, self.plan, start="2027-01-31")
		_pay(si)
		self.assertEqual(getdate(self._doc(m).current_period_end), date(2027, 2, 27))
		ms.change_card(m, self._card(cust))
		ms.set_auto_renew(m, 1)
		for day in ("2027-02-28", "2027-03-31", "2027-04-30"):
			with _on(day):
				ms.renew_memberships()
		self.assertEqual([(getdate(r.period_start), getdate(r.period_end)) for r in self._renewals(m)],
			[(date(2027, 2, 28), date(2027, 3, 30)), (date(2027, 3, 31), date(2027, 4, 29)),
				(date(2027, 4, 30), date(2027, 5, 30))])

	# ---------------------------------------------------------------- declined

	def test_declined_without_grace_suspends_and_cancels_the_invoice(self, cc):
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member()
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.DECLINED), 1)
		(row,) = self._renewals(m)
		self.assertEqual((row.status, row.credit_note), (ms.CANCELLED, None))
		self.assertEqual(frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus"), 2)
		# Cancelled the same day it was posted: every ledger row is marked cancelled, so the receivable,
		# income and sales tax read through is_cancelled = 0 (and docstatus = 1) never see it.
		gl = frappe.get_all("GL Entry", filters={"voucher_no": row.sales_invoice}, fields=["is_cancelled", "posting_date"])
		self.assertTrue(gl)
		self.assertEqual({(g.is_cancelled, getdate(g.posting_date)) for g in gl}, {(1, date(2026, 12, 15))})
		self.assertFalse(frappe.db.exists("Sales Invoice", {"name": row.sales_invoice, "docstatus": 1}))
		doc = self._doc(m)
		self.assertEqual((doc.status, doc.grace_until), (ms.SUSPENDED, None))
		self.assertIn("declined", doc.last_charge_status)
		self.assertFalse(ms.is_member(doc.customer))
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": row.sales_invoice}, "status"),
			"DECLINED")
		with _on("2026-12-15"):
			ms.renew_memberships()
		with _on("2026-12-16"):
			ms.renew_memberships()                       # Suspended: never charged by the job
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		self.assertIn(m, [r["membership"] for r in ms.needs_attention()["rows"]])

	def test_three_grace_days_retry_daily_then_suspend_on_day_four(self, cc):
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member(plan=self.grace_plan)
		with _on("2026-12-15"):
			ms.renew_memberships()
			doc = self._doc(m)
			self.assertEqual((doc.status, getdate(doc.grace_until)), (ms.PAST_DUE, date(2026, 12, 17)))
			self.assertTrue(ms.is_member(doc.customer, self.grace_plan))     # within grace
			ms.renew_memberships()                       # same day again: not retried
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		for day, calls in (("2026-12-16", 2), ("2026-12-17", 3)):
			with _on(day):
				ms.renew_memberships()
			self.assertEqual(client.charge_stored_credential.call_count, calls)
			self.assertEqual(self._doc(m).status, ms.PAST_DUE)
			self.assertEqual(self._open(m), [])          # never more than one open renewal invoice
		with _on("2026-12-18"):
			self.assertEqual(ms.renew_memberships().get(ms.SUSPENDED_NOW), 1)
			self.assertFalse(ms.is_member(self._doc(m).customer, self.grace_plan))
		self.assertEqual(client.charge_stored_credential.call_count, 3)
		rows = self._renewals(m)
		# A fresh invoice per retry, each for the same period, each voided; none deleted.
		self.assertEqual(len(rows), 3)
		self.assertEqual({(getdate(r.period_start), r.status) for r in rows}, {(date(2026, 12, 15), ms.CANCELLED)})
		self.assertEqual({frappe.db.get_value("Sales Invoice", r.sales_invoice, "docstatus") for r in rows}, {2})
		self.assertEqual(self._doc(m).status, ms.SUSPENDED)

	def test_paying_during_grace_starts_the_period_at_the_original_due_date(self, cc):
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member(plan=self.grace_plan)
		with _on("2026-12-15"):
			ms.renew_memberships()
		self._approve_all(client)
		with _on("2026-12-16"):
			out = ms.retry_renewal(m)
		self.assertEqual(out["outcome"], ms.RENEWED)
		doc = self._doc(m)
		self.assertEqual((doc.status, doc.grace_until, getdate(doc.current_period_start), getdate(doc.current_period_end)),
			(ms.ACTIVE, None, date(2026, 12, 15), date(2027, 1, 14)))

	def test_counter_payment_by_any_tender_restores_the_membership(self, cc):
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member(plan=self.grace_plan)
		with _on("2026-12-15"):
			ms.renew_memberships()
		with _on("2026-12-17"):
			inv = ms.invoice_renewal(m)
			self.assertEqual(ms.invoice_renewal(m)["sales_invoice"], inv["sales_invoice"])   # the open one again
		si = frappe.get_doc("Sales Invoice", inv["sales_invoice"])
		self.assertEqual({(getdate(r.service_start_date), getdate(r.service_end_date)) for r in si.items},
			{(date(2026, 12, 15), date(2027, 1, 14))})
		_pay(si.name)
		doc = self._doc(m)
		self.assertEqual((doc.status, getdate(doc.current_period_start)), (ms.ACTIVE, date(2026, 12, 15)))
		self.assertEqual(client.charge_stored_credential.call_count, 1)

	# ---------------------------------------------------------------- unknown outcome

	def test_unknown_outcome_is_never_charged_again_and_needs_attention(self, cc):
		client = cc.from_settings.return_value
		client.charge_stored_credential.side_effect = requests.exceptions.ReadTimeout("read timed out")
		m, _card = self._member()
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.UNKNOWN), 1)
		(row,) = self._renewals(m)
		self.assertEqual(row.status, ms.UNPAID)
		self.assertEqual(frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": row.sales_invoice}, "status"),
			"IN_PROGRESS")
		self.assertEqual(self._doc(m).status, ms.ACTIVE)
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.SKIPPED), 1)       # tried today already
		for day in ("2026-12-16", "2026-12-17"):
			with _on(day):
				self.assertEqual(ms.renew_memberships().get(ms.UNKNOWN), 1)
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		self.assertEqual(len(self._renewals(m)), 1)
		ms.set_auto_renew(m, 0)                          # still listed: the charge is unresolved
		with _on("2026-12-17"):
			att = next(r for r in ms.needs_attention()["rows"] if r["membership"] == m)
			self.assertEqual((att["reason"], att["sales_invoice"], att["can_retry"], att["can_cancel"]),
				("Card charge not confirmed", row.sales_invoice, True, False))
			with self.assertRaises(frappe.ValidationError):
				ms.invoice_renewal(m)                    # no second payment while it is unresolved
			with self.assertRaises(frappe.ValidationError):
				ms.cancel_membership(m, "moving")
			# The gateway has it now: Retry now looks it up and records it, still one charge.
			client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": row.sales_invoice,
				"resultCode": "A", "uniqueReference": "REF-LATE"}]}
			client.retrieve_payment.return_value = _payment("REF-LATE",
				flt(frappe.db.get_value("Sales Invoice", row.sales_invoice, "outstanding_amount")))
			self.assertEqual(ms.retry_renewal(m)["outcome"], ms.RENEWED)
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		self.assertEqual((self._doc(m).status, getdate(self._doc(m).current_period_end)), (ms.ACTIVE, date(2027, 1, 14)))

	def test_a_released_charge_is_retried_on_the_same_invoice(self, cc):
		client = cc.from_settings.return_value
		client.charge_stored_credential.side_effect = requests.exceptions.ReadTimeout("read timed out")
		m, _card = self._member()
		with _on("2026-12-15"):
			ms.renew_memberships()
		(row,) = self._renewals(m)
		tp = frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": row.sales_invoice}, "name")
		frappe.db.set_value(api.TERMINAL_PAYMENT, tp, "creation", add_to_date(now_datetime(), minutes=-30),
			update_modified=False)
		client.find_transaction.return_value = {"data": []}
		saved_cards.release_saved_card_charge(tp, "not in the portal")
		self._approve_all(client)
		with _on("2026-12-16"):
			self.assertEqual(ms.renew_memberships().get(ms.RENEWED), 1)
		# At most one open renewal invoice per period: the retry charged it, it made no new one.
		self.assertEqual([r.sales_invoice for r in self._renewals(m)], [row.sales_invoice])
		self.assertEqual(client.charge_stored_credential.call_args.kwargs["order_id"], f"{row.sales_invoice}-A2")
		self.assertEqual(self._doc(m).status, ms.ACTIVE)

	def test_a_decline_after_a_booked_month_voids_by_credit_note(self, cc):
		"""The answer was lost on 12-15, December's deferred revenue was booked from the open invoice,
		and the lookup on 01-03 finds a decline: cancelling would rewrite December, so service stops
		that day and a full credit note takes the invoice back."""
		client = cc.from_settings.return_value
		self.set_single(AS, "book_deferred_entries_based_on", "Days")
		self.set_single(AS, "book_deferred_entries_via_journal_entry", 0)
		client.charge_stored_credential.side_effect = requests.exceptions.ReadTimeout("read timed out")
		m, _card = self._member()
		with _on("2026-12-15"):
			ms.renew_memberships()
		(row,) = self._renewals(m)
		deferred, income = membership_setup.accounts(self.company)
		frappe.get_doc({"doctype": "Process Deferred Accounting", "company": self.company, "type": "Income",
			"account": deferred, "posting_date": "2026-12-31", "start_date": "2026-12-01",
			"end_date": "2026-12-31"}).insert(ignore_permissions=True).submit()
		december = self._income(row.sales_invoice, income, "2026-12-01", "2026-12-31")
		self.assertAlmostEqual(december, flt(30 * 17 / 31, 2), places=2)
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": row.sales_invoice,
			"resultCode": "D", "resultMessage": "Do not honor", "uniqueReference": "REF-DEC"}]}
		with _on("2027-01-03"), patch("ffl_core.receipts.frappe.enqueue") as enqueue:
			self.assertEqual(ms.retry_renewal(m)["outcome"], ms.DECLINED)
		self.assertFalse([c for c in enqueue.call_args_list if "send_sale_receipt" in str(c)])
		(row,) = self._renewals(m)
		si = frappe.get_doc("Sales Invoice", row.sales_invoice)
		self.assertEqual((si.docstatus, flt(si.outstanding_amount)), (1, 0))
		self.assertEqual({getdate(r.service_stop_date) for r in si.items}, {date(2027, 1, 3)})
		ret = frappe.get_doc("Sales Invoice", row.credit_note)
		self.assertEqual((ret.docstatus, ret.return_against, getdate(ret.posting_date)), (1, si.name, date(2027, 1, 3)))
		self.assertEqual(ret.remarks, f"Void of declined renewal {si.name}")
		self.assertAlmostEqual(flt(ret.grand_total), -flt(si.grand_total), places=2)
		self.assertEqual({r.enable_deferred_revenue for r in ret.items}, {0})
		self.assertAlmostEqual(self._income(si.name, income, "2026-12-01", "2026-12-31"), december, places=2)
		self.assertEqual((row.status, self._doc(m).status), (ms.CANCELLED, ms.SUSPENDED))
		self.assertEqual(client.charge_stored_credential.call_count, 1)

	@staticmethod
	def _income(si, account, start, end) -> float:
		return flt(frappe.db.sql("""select sum(credit) - sum(debit) from `tabGL Entry` where account = %s
			and is_cancelled = 0 and posting_date between %s and %s and voucher_no = %s""", (account, start, end, si))[0][0])

	# ---------------------------------------------------------------- no card / not started

	def test_no_chargeable_card_makes_no_invoice(self, cc):
		client = cc.from_settings.return_value
		self._approve_all(client)
		removed, _c = self._member()
		frappe.db.set_value(saved_cards.SAVED_CARD, self._doc(removed).saved_card, "status", "Removed")
		tokenless, _c = self._member(plan=self.grace_plan)
		frappe.db.delete("__Auth", {"doctype": saved_cards.SAVED_CARD, "name": self._doc(tokenless).saved_card,
			"fieldname": "token"})
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.NO_CARD), 2)
		self.assertEqual((self._renewals(removed), self._renewals(tokenless)), ([], []))
		self.assertEqual(self._doc(removed).status, ms.SUSPENDED)
		self.assertEqual((self._doc(tokenless).status, getdate(self._doc(tokenless).grace_until)),
			(ms.PAST_DUE, date(2026, 12, 17)))
		client.charge_stored_credential.assert_not_called()

	def test_a_processor_that_cannot_start_is_not_a_decline(self, cc):
		client = cc.from_settings.return_value
		self._approve_all(client)
		m, _card = self._member()
		with _on("2026-12-15"), patch("ffl_integrations.payroc.adapter.PayrocProcessor.prepare",
				side_effect=frappe.ValidationError("Payroc is not set up")):
			self.assertEqual(ms.renew_memberships().get(ms.NOT_STARTED), 1)
		doc = self._doc(m)
		self.assertEqual((doc.status, self._renewals(m)), (ms.ACTIVE, []))     # no invoice left behind
		self.assertIn("Payroc is not set up", doc.last_charge_status)
		with _on("2026-12-15"):
			att = next(r for r in ms.needs_attention()["rows"] if r["membership"] == m)
			self.assertEqual(att["reason"], "Renewal not charged")
		with _on("2026-12-16"):
			self.assertEqual(ms.renew_memberships().get(ms.RENEWED), 1)
		self.assertEqual(self._doc(m).status, ms.ACTIVE)
		self.assertEqual(client.charge_stored_credential.call_count, 1)

	def test_a_processor_refusal_after_the_record_is_not_a_decline(self, cc):
		"""The processor refuses the request itself (expired key, feature off) after the charge record
		exists: recorded CANCELED, nothing charged — the store's problem, so the member keeps its status
		and the invoice is voided; tomorrow it is tried again."""
		from ffl_integrations.payments.base import CardProcessorError

		client = cc.from_settings.return_value
		self._approve_all(client)
		m, _card = self._member()
		with _on("2026-12-15"), patch("ffl_integrations.payroc.adapter.PayrocProcessor.charge_stored",
				side_effect=CardProcessorError("401 Unauthorized", outcome_known=True)):
			self.assertEqual(ms.renew_memberships().get(ms.NOT_STARTED), 1)
		(row,) = self._renewals(m)
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": row.sales_invoice}, "status"),
			"CANCELED")
		self.assertEqual((row.status, frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus")),
			(ms.CANCELLED, 2))
		doc = self._doc(m)
		self.assertEqual(doc.status, ms.ACTIVE)
		self.assertIn("refused the request", doc.last_charge_status)
		with _on("2026-12-16"):
			self.assertEqual(ms.renew_memberships().get(ms.RENEWED), 1)

	def test_a_suspended_member_who_pays_starts_again_today(self, cc):
		"""Grace over: paying later bills a fresh period from today, not the months it was suspended,
		and the next renewal counts from that day."""
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member()
		with _on("2026-12-15"):
			ms.renew_memberships()
		self.assertEqual(self._doc(m).status, ms.SUSPENDED)
		with _on("2027-03-20"):
			inv = ms.invoice_renewal(m)
			self.assertEqual(ms.invoice_renewal(m)["sales_invoice"], inv["sales_invoice"])   # one open at a time
			si = frappe.get_doc("Sales Invoice", inv["sales_invoice"])
			self.assertEqual({(getdate(r.service_start_date), getdate(r.service_end_date)) for r in si.items},
				{(date(2027, 3, 20), date(2027, 4, 19))})
			_pay(si.name)
		doc = self._doc(m)
		self.assertEqual((doc.status, getdate(doc.start_date), getdate(doc.current_period_end)),
			(ms.ACTIVE, date(2027, 3, 20), date(2027, 4, 19)))
		calls = client.charge_stored_credential.call_count
		with _on("2027-03-21"):
			ms.renew_memberships()                       # nothing back-billed
		self.assertEqual(client.charge_stored_credential.call_count, calls)

	def test_a_charge_waiting_for_its_answer_holds_off_suspension(self, cc):
		client = cc.from_settings.return_value
		self._decline_all(client)
		m, _card = self._member(plan=self.grace_plan)
		with _on("2026-12-15"):
			ms.renew_memberships()                       # declined: Past Due until 12-17
		client.charge_stored_credential.side_effect = requests.exceptions.ReadTimeout("read timed out")
		with _on("2026-12-16"):
			self.assertEqual(ms.renew_memberships().get(ms.UNKNOWN), 1)
		with _on("2026-12-19"):
			self.assertEqual(ms.renew_memberships().get(ms.UNKNOWN), 1)
		self.assertEqual(self._doc(m).status, ms.PAST_DUE)

	def test_overdue_members_are_listed_with_renewals_switched_off(self, cc):
		m, _card = self._member()
		frappe.db.set_single_value(ms.SETTINGS, "auto_renew_enabled", 0)
		with _on("2026-12-20"):
			self.assertEqual(ms.renew_memberships(), {})
			att = next(r for r in ms.needs_attention()["rows"] if r["membership"] == m)
		self.assertEqual(att["reason"], "Renewal not tried yet")
