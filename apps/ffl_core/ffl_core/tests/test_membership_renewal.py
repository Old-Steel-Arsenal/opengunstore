"""Membership renewals, the parts that need no card gateway (ffl_core/membership.py): anchoring each
period on the start day, the job's switch, the no-card path, grace running out, the yearly
reminder, Needs attention for unpaid sign-ups, and who counts as a member.

The charging itself (approved / declined / unknown, voids, retries) runs against the real
card-on-file service in ffl_integrations/tests/test_membership_renewal.py. Here only the clock
(``membership.nowdate``) and, where named, the card service's hooks are stubbed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, getdate, now_datetime

from ffl_core import membership as ms
from ffl_core.tests.test_customer_profile import _customer
from ffl_core.tests.test_membership import _Base, _pay, _plan, _sell


@contextmanager
def _on(day):
	with patch("ffl_core.membership.nowdate", return_value=day):
		yield


class Anchoring(_Base):
	def _chain(self, start, period, n):
		doc = frappe._dict(start_date=getdate(start), current_period_start=getdate(start),
			current_period_end=ms.period_end(start, period))
		out = [(doc.current_period_start, doc.current_period_end)]
		for _i in range(n):
			doc.current_period_start, doc.current_period_end = ms.renewal_period(doc, period)
			out.append((doc.current_period_start, doc.current_period_end))
		return out

	def test_a_31st_start_returns_to_the_31st_after_short_months(self):
		self.assertEqual(self._chain("2027-01-31", ms.MONTHLY, 5), [
			(date(2027, 1, 31), date(2027, 2, 27)),
			(date(2027, 2, 28), date(2027, 3, 30)),      # billed on Feb's last day
			(date(2027, 3, 31), date(2027, 4, 29)),      # back on the 31st
			(date(2027, 4, 30), date(2027, 5, 30)),
			(date(2027, 5, 31), date(2027, 6, 29)),
			(date(2027, 6, 30), date(2027, 7, 30)),
		])

	def test_mid_month_and_yearly_periods(self):
		self.assertEqual(self._chain("2026-11-15", ms.MONTHLY, 2), [
			(date(2026, 11, 15), date(2026, 12, 14)), (date(2026, 12, 15), date(2027, 1, 14)),
			(date(2027, 1, 15), date(2027, 2, 14))])
		self.assertEqual(self._chain("2028-02-29", ms.YEARLY, 2), [
			(date(2028, 2, 29), date(2029, 2, 27)), (date(2029, 2, 28), date(2030, 2, 27)),
			(date(2030, 2, 28), date(2031, 2, 27))])


class RenewalJob(_Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.grace_plan = _plan(grace_enabled=1, grace_days=3)
		cls.yearly = _plan(period=ms.YEARLY, price=650, fee=0)

	def setUp(self):
		super().setUp()
		frappe.db.savepoint("renewal_job_test")
		self.addCleanup(frappe.db.rollback, save_point="renewal_job_test")

	def _active(self, plan=None, auto_renew=1, start="2026-11-15"):
		m, si = _sell(self.customer, plan or self.plan.name, start=start)
		_pay(si)
		# A card is a ffl_integrations record: here the membership renews with none at all.
		frappe.db.set_value(ms.DOCTYPE, m, "auto_renew", auto_renew)
		return m

	def _invoices(self, m):
		return frappe.db.count(ms.INVOICE_ROW, {"parent": m, "parenttype": ms.DOCTYPE})

	def test_no_card_no_invoice_suspends_without_grace(self):
		m = self._active()
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.NO_CARD), 1)
		self.assertEqual(self._invoices(m), 1)                 # the sign-up's only
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, doc.grace_until), (ms.SUSPENDED, None))
		self.assertIn("No card", doc.last_charge_status)

	def test_no_card_with_grace_goes_past_due_then_suspended(self):
		m = self._active(self.grace_plan.name)
		with _on("2026-12-15"):
			ms.renew_memberships()
		doc = frappe.get_doc(ms.DOCTYPE, m)
		self.assertEqual((doc.status, getdate(doc.grace_until)), (ms.PAST_DUE, date(2026, 12, 17)))
		with _on("2026-12-17"):
			self.assertTrue(ms.is_member(self.customer))
			ms.renew_memberships()
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.PAST_DUE)
		with _on("2026-12-18"):
			self.assertFalse(ms.is_member(self.customer))     # grace over, even before the job runs
			ms.renew_memberships()
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.SUSPENDED)
		self.assertEqual(self._invoices(m), 1)

	def test_grace_runs_out_even_after_auto_renew_is_switched_off(self):
		m = self._active(self.grace_plan.name)
		with _on("2026-12-15"):
			ms.renew_memberships()
			ms.set_auto_renew(m, 0)
		with _on("2026-12-18"):
			self.assertEqual(ms.renew_memberships().get(ms.SUSPENDED_NOW), 1)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.SUSPENDED)

	def test_switched_off_nothing_runs(self):
		m = self._active()
		self.set_single(ms.SETTINGS, "auto_renew_enabled", 0)
		with _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships(), {})
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.ACTIVE)
		self.assertFalse(ms.needs_attention()["renewals_on"])

	def test_without_the_card_service_nothing_is_charged(self):
		"""Core never imports the payments app: with no hook answering, no card is chargeable."""
		m = self._active()
		with patch("ffl_core.membership.frappe.get_hooks", return_value=[]), _on("2026-12-15"):
			self.assertEqual(ms.renew_memberships().get(ms.NO_CARD), 1)
		self.assertEqual(self._invoices(m), 1)

	def test_one_failing_membership_does_not_stop_the_others(self):
		first, second = self._active(), self._active(self.grace_plan.name)
		real = ms._renew_one

		def flaky(name, today):
			if name == first:
				raise frappe.ValidationError("boom")
			return real(name, today)
		with patch("ffl_core.membership._renew_one", side_effect=flaky), _on("2026-12-15"):
			out = ms.renew_memberships()
		self.assertEqual((out.get("error"), out.get(ms.NO_CARD)), (1, 1))
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, first, "status"), ms.ACTIVE)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, second, "status"), ms.PAST_DUE)
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"Membership {first}: renewal failed"}))

	def test_counter_payment_on_a_suspended_membership_restores_it_from_the_due_date(self):
		m = self._active()
		with _on("2026-12-15"):
			ms.renew_memberships()
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.SUSPENDED)
		with _on("2026-12-20"), patch("ffl_core.receipts.frappe.enqueue") as enqueue:
			frappe.db.set_value("Customer", self.customer, "email_id", "renewal-counter@example.com")
			inv = ms.invoice_renewal(m)
			self.assertFalse([c for c in enqueue.call_args_list if "send_sale_receipt" in str(c)])
			_pay(inv["sales_invoice"])
			self.assertEqual([c.kwargs["name"] for c in enqueue.call_args_list if "send_sale_receipt" in str(c)],
				[inv["sales_invoice"]])                 # sent once paid, not at submit
			row = next(r for r in ms.get_memberships(self.customer)["memberships"] if r["name"] == m)
		self.assertEqual((row["status"], row["current_period_start"], row["current_period_end"]),
			(ms.ACTIVE, date(2026, 12, 15), date(2027, 1, 14)))
		si = frappe.get_doc("Sales Invoice", inv["sales_invoice"])
		self.assertEqual((getdate(si.posting_date), len(si.items)), (date(2026, 12, 20), 1))   # no registration line

	def test_cancelling_a_past_due_membership_voids_its_open_renewal_invoice(self):
		m = self._active(self.grace_plan.name)
		with _on("2026-12-15"):
			ms.renew_memberships()
			si = ms.invoice_renewal(m)["sales_invoice"]
			ms.cancel_membership(m, "moved away")
		self.assertEqual(frappe.db.get_value("Sales Invoice", si, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.CANCELLED)

	def test_retry_and_take_payment_only_for_an_overdue_renewal(self):
		m = self._active()
		with _on("2026-12-10"):
			with self.assertRaises(frappe.ValidationError):
				ms.retry_renewal(m)
			with self.assertRaises(frappe.ValidationError):
				ms.invoice_renewal(m)
		with _on("2026-12-15"), self.assertRaises(frappe.ValidationError) as ctx:
			ms.retry_renewal(m)                          # overdue, but no card to charge
		self.assertIn("no card", str(ctx.exception))

	def test_yearly_reminder_once_and_never_for_monthly(self):
		frappe.db.set_value("Customer", self.customer, "email_id", "member-reminder@example.com")
		yearly = self._active(self.yearly.name)
		monthly = self._active()
		with patch("ffl_core.membership.frappe.sendmail") as sendmail:
			with _on("2027-11-07"):
				ms.renew_memberships()                   # 8 days before the 11-15 charge
			sendmail.assert_not_called()
			with _on("2027-11-08"):
				self.assertEqual(ms.renew_memberships()["reminded"], 1)
				self.assertEqual(ms.renew_memberships()["reminded"], 0)     # once per renewal
			(call,) = sendmail.call_args_list
		self.assertEqual((call.kwargs["recipients"], call.kwargs["reference_name"]), (["member-reminder@example.com"], yearly))
		self.assertIn(frappe.format_value("2027-11-15", {"fieldtype": "Date"}), call.kwargs["subject"])
		self.assertIn("$650", call.kwargs["message"].replace(" ", ""))
		self.assertNotEqual(call.kwargs["reference_name"], monthly)

	def test_yearly_reminder_zero_days_is_off(self):
		frappe.db.set_value("Customer", self.customer, "email_id", "member-reminder2@example.com")
		self._active(self.yearly.name)
		self.set_single(ms.SETTINGS, "yearly_reminder_days", 0)
		with patch("ffl_core.membership.frappe.sendmail") as sendmail, _on("2027-11-14"):
			self.assertEqual(ms.renew_memberships()["reminded"], 0)
		sendmail.assert_not_called()

	def test_unpaid_sign_up_shows_in_needs_attention_after_three_days(self):
		m, si = _sell(self.customer, self.plan.name)
		self.assertNotIn(m, [r["membership"] for r in ms.needs_attention()["rows"]])
		frappe.db.set_value(ms.DOCTYPE, m, "creation", add_to_date(now_datetime(), days=-4), update_modified=False)
		row = next(r for r in ms.needs_attention()["rows"] if r["membership"] == m)
		self.assertEqual((row["sales_invoice"], row["can_drop"], row["can_retry"]), (si, True, False))
		self.assertIn("not paid", row["reason"])

	def test_is_member(self):
		other = _customer().name
		m = self._active(auto_renew=0)
		with _on("2026-12-01"):
			self.assertTrue(ms.is_member(self.customer))
			self.assertTrue(ms.is_member(self.customer, self.plan.name))
			self.assertFalse(ms.is_member(self.customer, self.grace_plan.name))
			self.assertFalse(ms.is_member(other))
		frappe.db.set_value(ms.DOCTYPE, m, "status", ms.SUSPENDED)
		self.assertFalse(ms.is_member(self.customer))
