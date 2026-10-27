"""Record Payment — "Payment received on": the collection's accounting date.

A counter order settled days after it was written must book on the day the
money actually arrived, not the day the operator got round to typing it in —
otherwise the deposit lands in the wrong month and the bank reconciliation
never ties out. record_payment therefore takes an optional `received_on` that
pins the Payment Entry's posting_date, and — for a tender that carries a bank
reference (Zelle, ACH) — its reference_date too: ERPNext's set_remarks prints
"Transaction reference no X dated <reference_date>" into the entry, so a
back-dated entry stamped with today's reference date would state two different
dates for one deposit.

Both ends of the window are refusals, never clamps:
  * a payment cannot arrive in the future;
  * it cannot arrive before the invoice that asks for it was dated (that would
    post the receipt outside the sale's period).
Blank → today, exactly the pre-feature behaviour, which the default-path tests
here pin so the new parameter can never move an ordinary same-day collection.
"""
from __future__ import annotations

import os
import re
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, flt, getdate, nowdate

from ffl_core.tests.test_build_order import DEST_FFL, INDIV, BuildOrderMixin


class RecordPaymentReceivedOn(BuildOrderMixin, FrappeTestCase):
	def _invoice_dated(self, serial: str, days_ago: int = 0) -> "frappe.Document":
		"""A submitted, unpaid counter invoice posted `days_ago` days back — the
		shape Record Payment collects against. Billing-only (update_stock=0), so
		back-dating moves no stock ledger; buyer_type Business keeps the unpaid
		invoice legal to sit on."""
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = INDIV
		si.ffl_destination_ffl = DEST_FFL
		si.ffl_buyer_type = "Business"
		si.append("items", {
			"item_code": "BO-GUN", "qty": 1, "rate": 900,
			"warehouse": self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1,
		})
		si.set_missing_values()
		if days_ago:
			si.set_posting_time = 1
			si.posting_date = add_days(nowdate(), -days_ago)
		si.flags.ignore_permissions = True
		si.insert()
		si.update_stock = 0
		si.save()
		si.submit()
		return si

	# ------------------------------------------------ default path (unchanged)

	def test_no_date_books_today(self):
		"""The historical call — no received_on — still books on today."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-CASH-1"), days_ago=6)
		out = record_payment(si.name, "Cash")
		self.assertTrue(out["paid"])
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(nowdate()))

	def test_blank_date_books_today(self):
		"""The dialog sends '' when the operator clears the control — blank is
		absent, not a parse error."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-CASH-2"), days_ago=6)
		out = record_payment(si.name, "Cash", received_on="")
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(nowdate()))

	def test_no_date_reference_still_dated_today(self):
		"""A tender that carries a bank reference keeps referencing today when
		no date is given — the reference_date branch must not drift for the
		collections that never touch this feature. (Zelle, not ACH: Zelle
		always writes a reference, while ACH's reference rides on whether its
		configured account is Bank-type on this site.)"""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-ZEL-0"), days_ago=4)
		out = record_payment(si.name, "Zelle", transaction_number="ZEL-RPD-000")
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(pe.reference_no, "ZEL-RPD-000")
		self.assertEqual(getdate(pe.posting_date), getdate(nowdate()))
		self.assertEqual(getdate(pe.reference_date), getdate(nowdate()))

	def test_no_date_ach_books_today(self):
		"""ACH now carries the same reference rule as Zelle (a manual bank
		transfer whose only machine record lives in the bank), so it books with
		one; the DATE behaviour under test is unchanged."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-ACH-1"), days_ago=4)
		out = record_payment(si.name, "ACH", transaction_number="ACH-CONF-8811")
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(nowdate()))

	# ------------------------------------------------------- a legal past date

	def test_past_date_pins_posting_date(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-PAST-1"), days_ago=10)
		received = add_days(nowdate(), -5)
		out = record_payment(si.name, "Cash", received_on=received)
		self.assertTrue(out["paid"])
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(received))
		# The receipt really lands in that period, not just on the document —
		# Payment Entry has no set_posting_time field (the one _book_payment_entry
		# sets is a no-op leftover), so the GL is the only proof that an explicit
		# posting_date survives submit.
		gl_dates = frappe.get_all(
			"GL Entry", filters={"voucher_type": "Payment Entry",
				"voucher_no": pe.name, "is_cancelled": 0},
			pluck="posting_date")
		self.assertTrue(gl_dates)
		for d in gl_dates:
			self.assertEqual(getdate(d), getdate(received))

	def test_invoice_date_itself_is_allowed(self):
		"""The lower bound is inclusive — paid the same day it was written."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-PAST-2"), days_ago=8)
		out = record_payment(si.name, "Cash", received_on=str(si.posting_date))
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(si.posting_date))

	def test_today_is_allowed(self):
		"""The upper bound is inclusive — today is not "the future"."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-TODAY-1"), days_ago=3)
		out = record_payment(si.name, "Cash", received_on=nowdate())
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(nowdate()))

	def test_partial_amount_with_date(self):
		"""A back-dated deposit is still a partial collection: the invoice keeps
		its balance and the entry keeps the date."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-PART-1"), days_ago=9)
		received = add_days(nowdate(), -2)
		out = record_payment(si.name, "Cash", amount=100, received_on=received)
		self.assertFalse(out["paid"])
		self.assertGreater(out["outstanding"], 0)
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(getdate(pe.posting_date), getdate(received))

	# ------------------------------------------------------------- both bounds

	def test_future_date_refused(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-FUT-1"), days_ago=5)
		with self.assertRaises(frappe.ValidationError) as cm:
			record_payment(si.name, "Cash", received_on=add_days(nowdate(), 1))
		msg = str(cm.exception).lower()
		self.assertIn("future", msg)
		# The refusal names BOTH bounds, so the operator knows the window.
		self.assertIn("invoice", msg)
		self.assertIn("today", msg)
		# Nothing booked.
		self.assertFalse(frappe.get_all("Payment Entry Reference",
			filters={"reference_name": si.name, "docstatus": 1}))

	def test_date_before_invoice_refused(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-EARLY-1"), days_ago=10)
		with self.assertRaises(frappe.ValidationError) as cm:
			record_payment(si.name, "Cash", received_on=add_days(nowdate(), -20))
		msg = str(cm.exception).lower()
		self.assertIn("before", msg)
		self.assertIn("invoice", msg)
		self.assertIn("today", msg)
		self.assertFalse(frappe.get_all("Payment Entry Reference",
			filters={"reference_name": si.name, "docstatus": 1}))

	def test_unreadable_date_refused(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-BAD-1"), days_ago=5)
		for bad in ("not-a-date", "2026-13-45", "yesterday-ish"):
			with self.assertRaises(frappe.ValidationError) as cm:
				record_payment(si.name, "Cash", received_on=bad)
			self.assertIn("yyyy-mm-dd", str(cm.exception).lower())
		self.assertFalse(frappe.get_all("Payment Entry Reference",
			filters={"reference_name": si.name, "docstatus": 1}))

	# ---------------------------------------------------------- Zelle + a date

	def test_zelle_with_date_dates_the_reference_too(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-ZEL-1"), days_ago=12)
		received = add_days(nowdate(), -7)
		out = record_payment(si.name, "Zelle", transaction_number="ZEL-RPD-777",
			received_on=received)
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(pe.reference_no, "ZEL-RPD-777")
		self.assertEqual(getdate(pe.posting_date), getdate(received))
		self.assertEqual(getdate(pe.reference_date), getdate(received))
		# ERPNext stamps the reference date into the remarks: it must not claim
		# a deposit date the entry itself does not carry.
		self.assertIn(str(getdate(received)), str(pe.remarks or ""))

	def test_zelle_still_requires_transaction_number_with_a_date(self):
		"""The date must not become a way past the Zelle reference gate."""
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-ZEL-2"), days_ago=6)
		with self.assertRaises(frappe.ValidationError) as cm:
			record_payment(si.name, "Zelle", received_on=add_days(nowdate(), -3))
		self.assertIn("transaction number", str(cm.exception).lower())

	def test_zelle_bad_date_still_refused(self):
		from ffl_core.api.manual_order import record_payment
		si = self._invoice_dated(self._stocked_serial("RPD-ZEL-3"), days_ago=6)
		with self.assertRaises(frappe.ValidationError) as cm:
			record_payment(si.name, "Zelle", transaction_number="ZEL-RPD-888",
				received_on="soon")
		self.assertIn("yyyy-mm-dd", str(cm.exception).lower())

	# ------------------------------------------- the marketplace remarks hook

	def test_booking_without_remarks_keeps_erpnexts_own_wording(self):
		"""The parameter has to be invisible to every caller that ignores it.

		`custom_remarks` staying 0 IS that guarantee: PaymentEntry.set_remarks()
		rewrites the field on every validate unless the flag is set, so a
		receipt booked the old way carries exactly the sentence it always did.
		Asserting the sentence's text here would only pin ERPNext's wording,
		which is not ours to pin."""
		from ffl_core.api.manual_order import _book_payment_entry
		si = self._invoice_dated(self._stocked_serial("RPD-REM-0"), days_ago=3)
		pe = frappe.get_doc("Payment Entry",
			_book_payment_entry(si, "Cash", flt(si.grand_total)))
		self.assertFalse(cint(pe.custom_remarks))
		self.assertTrue((pe.remarks or "").strip())

	def test_a_given_remark_survives_validate(self):
		"""What a marketplace receipt needs. Its posting date is the INVOICE's
		date by design, so the day the money actually reached the bank has
		nowhere else to live — and without custom_remarks the sentence carrying
		it is overwritten during validate and the deposit is untraceable on a
		bank statement."""
		from ffl_core.api.manual_order import _book_payment_entry
		si = self._invoice_dated(self._stocked_serial("RPD-REM-1"), days_ago=3)
		sentence = "GunBroker cheque 4471 deposited on 2026-08-20."
		pe = frappe.get_doc("Payment Entry", _book_payment_entry(
			si, "Cash", flt(si.grand_total), remarks=sentence))
		self.assertEqual(pe.remarks, sentence)
		self.assertEqual(cint(pe.custom_remarks), 1)


# ------------------------------------------------------- front-end invariants


def _page_js(page: str) -> str:
	return open(os.path.join(
		frappe.get_app_path("ffl_core"), "ffl_core", "page", page, f"{page}.js")).read()


class RecordPaymentDialogSource(FrappeTestCase):
	"""Source-level guard over the dialog wiring (this repo ships no JS runner,
	same rationale as test_consignment_out_front_end_invariants): the control
	exists, defaults to today, and its value actually reaches the endpoint. A
	Date field nobody passes is the silent version of this feature not shipping."""

	def test_pending_dispositions_dialog_has_received_on(self):
		js = _page_js("pending_dispositions")
		# The method definition, not the click handler that calls it (async since it asks the
		# server whether to offer "Save this card" before building the dialog).
		block = js[re.search(r"\n\t(?:async )?_recordPayment\(row\) \{", js).start():]
		block = block[:block.index("\n\t// ")]
		self.assertIn("fieldname: 'received_on'", block)
		self.assertIn("Payment received on", block)
		self.assertIn("frappe.datetime.get_today()", block)
		self.assertTrue(
			re.search(r"received_on:\s*v\.received_on", block),
			"the dialog's received_on must be passed to record_payment")


if __name__ == "__main__":
	unittest.main()
