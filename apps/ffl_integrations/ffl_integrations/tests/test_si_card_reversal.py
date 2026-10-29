"""Void / refund a card charge on a counter Sales Invoice (payments/si_reversal.py).

Real schema end to end — counter orders, saved-card charges, Payment Entries, credit notes, Journal
Entries, memberships — with only the Payroc client (the gateway) mocked.
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch

import frappe
import requests
from frappe.utils import add_days, flt, nowdate

from ffl_core import membership as ms
from ffl_core.setup import membership as membership_setup
from ffl_core.tests.test_membership import _sell
from ffl_integrations.payments import api, saved_cards, si_reversal as rv
from ffl_integrations.payroc.client import PayrocError
from ffl_integrations.tests.site_fixtures import payroc_configured
from ffl_integrations.tests.test_card_on_file import _CardOnFileBase, _payment
from ffl_integrations.tests.test_membership_renewal import _plan

_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _gl(voucher: str) -> dict:
	rows = frappe.db.sql("""select account, sum(debit) d, sum(credit) c from `tabGL Entry`
		where voucher_no=%s and is_cancelled=0 group by account""", voucher, as_dict=True)
	return {r.account: flt(r.d) - flt(r.c) for r in rows}


@patch("ffl_integrations.payroc.adapter.PayrocClient")
class SalesInvoiceCardReversal(_CardOnFileBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		membership_setup.ensure()
		cls.plan = _plan()

	def setUp(self):
		super().setUp()
		frappe.db.savepoint("si_reversal_test")
		self.addCleanup(frappe.db.rollback, save_point="si_reversal_test")

	# ---------------------------------------------------------------- helpers

	def _card(self, cust) -> str:
		return frappe.get_doc({"doctype": saved_cards.SAVED_CARD, "customer": cust, "processor": "payroc",
			"token": "2967530000001234", "merchant_reference": f"t-{frappe.generate_hash(length=10)}",
			"card_summary": "Visa •1111", "card_expiry": "1229", "usage": "UNSCHEDULED", "status": "Active",
			"is_default": 1, "initial_reference": "REF0000001",
			"authorization": saved_cards.card_on_file_offer(cust)["agreement"]}).insert(ignore_permissions=True).name

	def _charged(self, client, si=None, rate=100, ref=None):
		"""A counter order (or *si*) paid in full by a saved-card charge → (si, charge row, amount)."""
		cust = frappe.db.get_value("Sales Invoice", si, "customer") if si else self._customer()
		if not si:
			si, _owed = self._unpaid_order(cust, rate=rate)
		owed = flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount"))
		ref = ref or f"REF-{frappe.generate_hash(length=6)}"
		client.charge_stored_credential.return_value = _payment(ref, owed)
		self.assertTrue(saved_cards.charge_saved_card(si, self._card(cust))["ok"])
		row = frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si, "status": "CHARGED"}, "name")
		return si, row, owed

	def _taxed_invoice(self, rate=100) -> str:
		"""A submitted invoice for a non-stock item with an 8.25% tax row."""
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		tax = frappe.db.get_value("Account", {"company": company, "account_name": "SIR Sales Tax"}, "name")
		if not tax:
			parent = frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			tax = frappe.get_doc({"doctype": "Account", "account_name": "SIR Sales Tax", "company": company,
				"parent_account": parent, "account_type": "Tax"}).insert(ignore_permissions=True).name
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": self._customer(), "company": company,
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": rate}],
			"taxes": [{"charge_type": "On Net Total", "account_head": tax, "rate": 8.25, "description": "Test tax"}]})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si.name

	@staticmethod
	def _row(name):
		return frappe.db.get_value(api.TERMINAL_PAYMENT, name, ["status", "refunded_amount", "reversal_lock",
			"reversal_entry_type", "reversal_entry", "payment_entry"], as_dict=True)

	@staticmethod
	def _outstanding(si):
		return flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount"))

	# ---------------------------------------------------------------- void

	def test_void_same_day_cancels_the_payment(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		client.reverse.return_value = {}
		out = rv.void_sales_invoice_charge(row, "rang the wrong card")
		self.assertTrue(out["ok"])
		client.reverse.assert_called_once()
		r = self._row(row)
		self.assertEqual((r.status, flt(r.refunded_amount), r.reversal_entry_type, r.reversal_lock),
			("VOIDED", amt, "Payment Entry", None))
		self.assertEqual(frappe.db.get_value("Payment Entry", r.payment_entry, "docstatus"), 2)
		self.assertAlmostEqual(self._outstanding(si), amt, 2)

	def test_void_of_an_earlier_days_payment_books_a_journal_entry(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		pe = self._row(row).payment_entry
		frappe.db.set_value("Payment Entry", pe, "posting_date", add_days(nowdate(), -1))
		client.reverse.return_value = {}
		rv.void_sales_invoice_charge(row, "customer changed their mind")
		r = self._row(row)
		self.assertEqual((r.status, r.reversal_entry_type), ("VOIDED", "Journal Entry"))
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 1)
		clearing = frappe.db.get_value("Payment Entry", pe, "paid_to")
		debtors = frappe.db.get_value("Sales Invoice", si, "debit_to")
		self.assertEqual(_gl(r.reversal_entry), {debtors: amt, clearing: -amt})
		self.assertAlmostEqual(self._outstanding(si), amt, 2)

	def test_void_refused_by_the_gateway_books_nothing(self, cc):
		client = cc.from_settings.return_value
		si, row, _amt = self._charged(client)
		client.reverse.side_effect = PayrocError("HTTP 409 already settled", outcome_known=True)
		with self.assertRaisesRegex(frappe.ValidationError, "refund it instead"):
			rv.void_sales_invoice_charge(row, "x")
		r = self._row(row)
		self.assertEqual((r.status, flt(r.refunded_amount), r.reversal_lock), ("CHARGED", 0, None))
		self.assertEqual(self._outstanding(si), 0)

	def test_unknown_outcome_locks_until_resolved(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		client.reverse.side_effect = requests.Timeout()
		with self.assertRaisesRegex(frappe.ValidationError, "Resolve card reversal"):
			rv.void_sales_invoice_charge(row, "x")
		self.assertEqual(_json.loads(self._row(row).reversal_lock)["action"], "void")
		client.reverse.reset_mock()
		client.refund.reset_mock()
		for again in (lambda: rv.void_sales_invoice_charge(row, "x"),
				lambda: rv.refund_sales_invoice_charge(row, 1, rv.COLLECT_AGAIN, "x")):
			with self.assertRaisesRegex(frappe.ValidationError, "never confirmed"):
				again()
		client.reverse.assert_not_called()
		client.refund.assert_not_called()
		# Not done after all → unlocked, nothing booked.
		self.assertFalse(rv.resolve_card_reversal(row, 0, "portal shows it still captured")["booked"])
		self.assertEqual((self._row(row).status, self._row(row).reversal_lock), ("CHARGED", None))
		# Locked again; this time the portal shows it voided → booked like an approval.
		client.reverse.side_effect = requests.Timeout()
		with self.assertRaises(frappe.ValidationError):
			rv.void_sales_invoice_charge(row, "x")
		with self.assertRaisesRegex(frappe.ValidationError, "reference"):
			rv.resolve_card_reversal(row, 1, "portal shows VOID")
		self.assertTrue(rv.resolve_card_reversal(row, 1, "portal shows VOID", "PORTAL-1")["booked"])
		self.assertEqual(self._row(row).status, "VOIDED")
		self.assertAlmostEqual(self._outstanding(si), amt, 2)

	# ---------------------------------------------------------------- refund

	def test_partial_return_refund_credits_pro_rata(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client, si=self._taxed_invoice(100))
		self.assertAlmostEqual(amt, 108.25, 2)
		client.refund.return_value = {"refundReference": "RF-1"}
		out = rv.refund_sales_invoice_charge(row, round(amt / 4, 2), rv.RETURN, "one of four returned")
		cn = frappe.get_doc("Sales Invoice", out["credit_note"])
		self.assertEqual((cn.docstatus, cn.is_return, cn.return_against), (1, 1, si))
		self.assertAlmostEqual(-flt(cn.grand_total), out["amount"], 2)
		self.assertAlmostEqual(out["amount"], amt / 4, delta=0.011)
		self.assertAlmostEqual(client.refund.call_args.kwargs["refund_amount"], out["amount"], 2)
		orig = frappe.get_doc("Sales Invoice", si)
		self.assertGreater(flt(orig.total_taxes_and_charges), 0, "the order must be taxed to test the tax pro rata")
		self.assertAlmostEqual(-flt(cn.total_taxes_and_charges), flt(orig.total_taxes_and_charges) / 4, delta=0.011)
		pe = frappe.get_doc("Payment Entry", out["entry"])
		self.assertEqual((pe.payment_type, pe.docstatus, pe.reference_no, pe.references[0].reference_name),
			("Pay", 1, "RF-1", cn.name))
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", cn.name, "outstanding_amount")), 0, 2)
		r = self._row(row)
		self.assertEqual(r.status, "CHARGED")
		self.assertAlmostEqual(flt(r.refunded_amount), out["amount"], 2)
		# The rest, then nothing left.
		rest = round(amt - out["amount"], 2)
		out2 = rv.refund_sales_invoice_charge(row, rest, rv.COLLECT_AGAIN, "rest")
		self.assertEqual(self._row(row).status, "REFUNDED")
		self.assertAlmostEqual(self._outstanding(si), out2["amount"], 2)
		with self.assertRaisesRegex(frappe.ValidationError, "nothing left"):
			rv.refund_sales_invoice_charge(row, 1, rv.COLLECT_AGAIN, "more")

	def test_collect_again_puts_it_back_on_the_invoice(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		client.refund.return_value = {"refundReference": "RF-2"}
		out = rv.refund_sales_invoice_charge(row, amt, rv.COLLECT_AGAIN, "paying cash instead")
		self.assertEqual(out["entry_type"], "Journal Entry")
		self.assertIsNone(out["credit_note"])
		self.assertAlmostEqual(self._outstanding(si), amt, 2)
		self.assertNotIn(frappe.db.get_value("Sales Invoice", si, "against_income_account"), _gl(out["entry"]))

	def test_refusals_before_the_gateway(self, cc):
		client = cc.from_settings.return_value
		_si, row, amt = self._charged(client)
		with self.assertRaisesRegex(frappe.ValidationError, "left to refund"):
			rv.refund_sales_invoice_charge(row, amt + 1, rv.COLLECT_AGAIN, "x")
		with self.assertRaisesRegex(frappe.ValidationError, "Say why"):
			rv.void_sales_invoice_charge(row, " ")
		with patch.object(frappe, "get_roles", return_value=["Sales User"]), \
				self.assertRaises(frappe.PermissionError):
			rv.void_sales_invoice_charge(row, "x")
		with patch("ffl_integrations.payments.si_reversal._stock_or_book", return_value="it sells stock items"), \
				self.assertRaisesRegex(frappe.ValidationError, "Cancel Order"):
			rv.refund_sales_invoice_charge(row, 1, rv.RETURN, "x")
		client.reverse.assert_not_called()
		client.refund.assert_not_called()

	def test_declined_refund_drops_the_draft_credit_note(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		before = frappe.db.count("Sales Invoice", {"return_against": si})
		client.refund.side_effect = PayrocError("HTTP 400 bad request", outcome_known=True)
		with self.assertRaisesRegex(frappe.ValidationError, "nothing was refunded"):
			rv.refund_sales_invoice_charge(row, amt, rv.RETURN, "x")
		self.assertEqual(frappe.db.count("Sales Invoice", {"return_against": si}), before)
		self.assertEqual((self._row(row).status, self._row(row).reversal_lock), ("CHARGED", None))

	def test_unknown_refund_resolved_as_done_submits_the_credit_note(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		client.refund.side_effect = requests.ConnectionError()
		with self.assertRaises(frappe.ValidationError):
			rv.refund_sales_invoice_charge(row, amt, rv.RETURN, "x")
		cn = _json.loads(self._row(row).reversal_lock)["credit_note"]
		self.assertEqual(frappe.db.get_value("Sales Invoice", cn, "docstatus"), 0)
		out = rv.resolve_card_reversal(row, 1, "portal shows the refund", "PORTAL-RF")
		self.assertEqual((out["credit_note"], frappe.db.get_value("Sales Invoice", cn, "docstatus")), (cn, 1))
		self.assertEqual(self._row(row).status, "REFUNDED")

	# ---------------------------------------------------------------- memberships

	def test_membership_invoice_needs_the_membership_ended(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		m, si = _sell(cust, self.plan, start=nowdate())
		si, row, amt = self._charged(client, si=si)
		self.assertEqual(frappe.db.get_value(ms.DOCTYPE, m, "status"), ms.ACTIVE)
		for call in (lambda: rv.void_sales_invoice_charge(row, "x"),
				lambda: rv.refund_sales_invoice_charge(row, amt, rv.RETURN, "x"),
				lambda: rv.refund_sales_invoice_charge(row, amt, rv.COLLECT_AGAIN, "x")):
			with self.assertRaisesRegex(frappe.ValidationError, "membership"):
				call()
		client.refund.assert_not_called()
		ms.cancel_membership(m, "test", end_now=1)
		client.refund.return_value = {"refundReference": "RF-M"}
		out = rv.refund_sales_invoice_charge(row, amt, rv.RETURN, "membership refunded")
		cn = frappe.get_doc("Sales Invoice", out["credit_note"])
		self.assertTrue(all(not i.enable_deferred_revenue for i in cn.items))
		self.assertEqual(self._row(row).status, "REFUNDED")

	# ---------------------------------------------------------------- form + reconcile

	def test_card_charges_lists_what_can_be_given_back(self, cc):
		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		out = rv.card_charges(si)
		self.assertEqual([(c["name"], c["left"], c["can_void"]) for c in out["charges"]], [(row, amt, True)])
		client.reverse.return_value = {}
		rv.void_sales_invoice_charge(row, "x")
		self.assertEqual(rv.card_charges(si)["charges"], [])

	def test_reconcile_reads_the_refunded_amount(self, cc):
		from ffl_integrations.payroc import reconcile

		client = cc.from_settings.return_value
		si, row, amt = self._charged(client)
		client.refund.return_value = {"refundReference": "RF-R"}
		rv.refund_sales_invoice_charge(row, 10, rv.COLLECT_AGAIN, "x")
		ref = frappe.db.get_value(api.TERMINAL_PAYMENT, row, "unique_reference")
		since = frappe.utils.get_datetime(add_days(nowdate(), -1))
		got = {c.ref: c for c in reconcile._captured_charges(since)}
		self.assertAlmostEqual(flt(got[ref].refunded_amount), 10, 2)
