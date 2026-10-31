"""Terminal card payment against a counter Sales Invoice (Create Order / Pending
Order → Record Payment): charge → poll → Payment Entry, with the gateway mocked
at the PayrocClient boundary. The invoice is a real unpaid counter order so the
ledger half (record_payment) runs for real."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import BuildOrderMixin
from ffl_integrations.payments import api
from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account, payroc_configured


# These tests stand in for a store whose Payroc is configured (the test site holds no key).
_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()

PAYMENT_HREF = "https://x/merchant/api/v1/transaction/payments/REF-TP-1"


def _approved(client, *, amount, ref="REF-TP-1", code="OK1"):
	client.poll_payment_instruction.return_value = {"status": "COMPLETED",
		"links": [{"rel": "payment", "href": PAYMENT_HREF}]}
	client.payment_link.return_value = PAYMENT_HREF
	client.retrieve_payment.return_value = {"uniqueReference": ref,
		"customerAccount": {"cardType": "Visa", "maskedPan": "****1111"},
		"transactionResult": {"resultCode": "A", "approvalCode": code,
			"authorizedAmount": amount}}


def _declined(client):
	client.poll_payment_instruction.return_value = {"status": "COMPLETED",
		"links": [{"rel": "payment", "href": PAYMENT_HREF}]}
	client.payment_link.return_value = PAYMENT_HREF
	client.retrieve_payment.return_value = {"uniqueReference": "REF-DECL",
		"transactionResult": {"resultCode": "D", "resultMessage": "Declined"}}


@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class TerminalPaymentTest(BuildOrderMixin, FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# A fresh CI site's "Credit Card" Mode of Payment has no account for the
		# test company; the Payment Entry the approval books needs one.
		ensure_credit_card_mop_account()

	def _unpaid_order(self, rate=100):
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("TP Card Buyer"),
			"dispose_now": 0, "payment_received": 0,
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": rate, "serials": []}],
		})
		return out["sales_invoice"], flt(out["outstanding"])

	def _records(self, si):
		return frappe.get_all(api.TERMINAL_PAYMENT, filters={"sales_invoice": si},
			fields=["name", "status", "amount", "instruction_id", "unique_reference",
				"payment_entry", "approval_code"], order_by="creation asc")

	def _start(self, client, si, amount=None, iid="PI-TP-1"):
		client.create_payment_instruction.return_value = {
			"paymentInstructionId": iid, "status": "IN_PROGRESS"}
		return api.charge_sales_invoice(si, amount=amount)

	# --- card taken off the terminal (Virtual Terminal) --------------------

	def test_create_order_card_off_terminal_needs_its_code_while_terminal_live(self, cc, _p, _r):
		"""Create Order with "Charge via Terminal" unticked marks the card paid
		without the A80 — with the terminal live it was run on the Virtual
		Terminal, so it needs the approval code record_payment would."""
		payload = {"buyer_type": "individual", "customer": self._indiv("TP VT Buyer"),
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": api.MODE_OF_PAYMENT_NAME,
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": 100, "serials": []}]}
		with patch("ffl_integrations.integration_gate.payroc_ready", return_value=True):
			with self.assertRaises(frappe.ValidationError) as ctx:
				manual_order.create_order(dict(payload))
			self.assertIn("virtual terminal", str(ctx.exception).lower())
			out = manual_order.create_order(dict(payload, transaction_number="APPR 7788"))
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual([p.reference_no for p in si.payments if flt(p.amount)], ["APPR 7788"])
		cc.from_settings.return_value.create_payment_instruction.assert_not_called()

	# --- charge -----------------------------------------------------------

	def test_charge_records_the_instruction_for_the_whole_balance(self, cc, _p, _r):
		si, outstanding = self._unpaid_order()
		out = self._start(cc.from_settings.return_value, si)
		self.assertEqual(out["instruction_id"], "PI-TP-1")
		self.assertEqual(out["amount"], outstanding)
		kw = cc.from_settings.return_value.create_payment_instruction.call_args.kwargs
		self.assertEqual(kw["amount"], outstanding)
		self.assertEqual(kw["order_id"], si)
		rec = self._records(si)
		self.assertEqual([(r.status, r.amount, r.instruction_id) for r in rec],
			[("IN_PROGRESS", outstanding, "PI-TP-1")])

	def test_charge_refuses_more_than_the_balance_and_zero(self, cc, _p, _r):
		si, outstanding = self._unpaid_order()
		with self.assertRaises(frappe.ValidationError):
			api.charge_sales_invoice(si, amount=outstanding + 5)
		with self.assertRaises(frappe.ValidationError):
			api.charge_sales_invoice(si, amount=0)
		self.assertEqual(self._records(si), [])

	def test_charge_reuses_an_in_flight_instruction(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, _ = self._unpaid_order()
		self._start(client, si)
		client.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
		again = api.charge_sales_invoice(si)
		self.assertEqual(again["instruction_id"], "PI-TP-1")
		self.assertEqual(client.create_payment_instruction.call_count, 1)

	def test_charge_settles_an_unrecorded_approval_instead_of_a_second_charge(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_approved(client, amount=outstanding)
		res = api.charge_sales_invoice(si)
		self.assertTrue(res["ok"] and res.get("already"))
		self.assertEqual(client.create_payment_instruction.call_count, 1)
		self.assertTrue(res["payment_entry"])
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), 0)

	def test_charge_after_a_decline_starts_a_fresh_instruction(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, _ = self._unpaid_order()
		self._start(client, si)
		_declined(client)
		client.create_payment_instruction.return_value = {
			"paymentInstructionId": "PI-TP-2", "status": "IN_PROGRESS"}
		out = api.charge_sales_invoice(si)
		self.assertEqual(out["instruction_id"], "PI-TP-2")
		self.assertEqual([r.status for r in self._records(si)], ["DECLINED", "IN_PROGRESS"])

	def test_second_card_carries_its_own_order_id(self, cc, _p, _r):
		"""Payroc refuses a second payment instruction on an orderId whose sale
		already went through ("Order has already been processed"), so the card for
		a balance must go out under its own id — card 1 keeps the bare invoice
		name."""
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si, amount=40)
		self.assertEqual(
			client.create_payment_instruction.call_args.kwargs["order_id"], si)
		_approved(client, amount=40)
		api.poll_sales_invoice_charge(si, "PI-TP-1")

		self._start(client, si, amount=outstanding - 40, iid="PI-TP-2")
		self.assertEqual(
			client.create_payment_instruction.call_args.kwargs["order_id"], f"{si}-A2")

	def test_charge_is_inert_when_the_integration_is_off(self, cc, _p, ready):
		ready.return_value = False
		self.assertEqual(api.charge_sales_invoice("SINV-nope"), {"skip": True})

	# --- poll -------------------------------------------------------------

	def test_deposit_approval_books_a_payment_entry_and_leaves_the_balance(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si, amount=40)
		_approved(client, amount=40)
		res = api.poll_sales_invoice_charge(si, "PI-TP-1")
		self.assertTrue(res["ok"])
		self.assertEqual(res["amount"], 40)
		self.assertEqual(res["unique_reference"], "REF-TP-1")
		self.assertFalse(res["paid"])
		self.assertAlmostEqual(res["outstanding"], outstanding - 40, places=2)
		rec = self._records(si)[0]
		self.assertEqual((rec.status, rec.amount, rec.approval_code), ("CHARGED", 40, "OK1"))
		pe = frappe.get_doc("Payment Entry", rec.payment_entry)
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.mode_of_payment, api.MODE_OF_PAYMENT_NAME)
		self.assertEqual(pe.reference_no, "REF-TP-1")
		self.assertEqual(flt(pe.paid_amount), 40)
		self.assertAlmostEqual(flt(frappe.db.get_value(
			"Sales Invoice", si, "outstanding_amount")), outstanding - 40, places=2)

	def test_approval_without_a_gateway_reference_still_books(self, cc, _p, _r):
		"""record_payment demands a reference for a card while the terminal is
		live; a captured charge whose payment came back with no uniqueReference
		must still book — against its approval code (what the batch shows), or
		the terminal record's name when even that is missing — never strand."""
		client = cc.from_settings.return_value
		for iid, code, expect_code in (("PI-TP-1", "OK7", True), ("PI-TP-2", "", False)):
			with self.subTest(code=code):
				si, outstanding = self._unpaid_order()
				self._start(client, si, iid=iid)
				_approved(client, amount=outstanding, ref="", code=code)
				with patch("ffl_integrations.integration_gate.payroc_ready", return_value=True):
					res = api.poll_sales_invoice_charge(si, iid)
				self.assertTrue(res["ok"] and res["paid"])
				rec = self._records(si)[0]
				self.assertEqual(frappe.db.get_value("Payment Entry", rec.payment_entry,
					"reference_no"), "OK7" if expect_code else rec.name)

	def test_full_approval_settles_the_invoice(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_approved(client, amount=outstanding)
		res = api.poll_sales_invoice_charge(si, "PI-TP-1")
		self.assertTrue(res["paid"])
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), 0)

	def test_decline_books_nothing(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_declined(client)
		res = api.poll_sales_invoice_charge(si, "PI-TP-1")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"], "Declined")
		rec = self._records(si)[0]
		self.assertEqual((rec.status, rec.payment_entry), ("DECLINED", None))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), outstanding)

	def test_in_progress_passes_through(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, _ = self._unpaid_order()
		self._start(client, si)
		client.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
		self.assertEqual(api.poll_sales_invoice_charge(si, "PI-TP-1"),
			{"ok": False, "status": "IN_PROGRESS"})

	def test_a_second_poll_of_a_settled_charge_does_not_book_twice(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si, amount=30)
		_approved(client, amount=30)
		first = api.poll_sales_invoice_charge(si, "PI-TP-1")
		second = api.poll_sales_invoice_charge(si, "PI-TP-1")
		self.assertEqual(second["payment_entry"], first["payment_entry"])
		# Counted against THIS invoice: create_order commits, so sibling tests'
		# entries with the same mocked gateway reference outlive their rollback.
		self.assertEqual(frappe.db.count("Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": si, "docstatus": 1}), 1)

	def test_a_racing_settle_from_a_stale_row_does_not_book_twice(self, cc, _p, _r):
		"""A poll and a cancel (or two screens) each read the row IN_PROGRESS before
		either settled: the second settle must re-read the record, not book again."""
		client = cc.from_settings.return_value
		si, _ = self._unpaid_order()
		self._start(client, si, amount=30)
		stale = api._terminal_payment_row(si, "PI-TP-1")
		_approved(client, amount=30)
		first = api.poll_sales_invoice_charge(si, "PI-TP-1")
		p = api._gateways()(stale.processor or api.LEGACY_PROCESSOR)
		second = api._settle_terminal_payment(p, p.poll("PI-TP-1"), stale)
		self.assertEqual(second["payment_entry"], first["payment_entry"])
		self.assertEqual(frappe.db.count("Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": si, "docstatus": 1}), 1)

	@patch("ffl_integrations.payments.api.notify_admins")
	@patch("ffl_core.api.manual_order.record_payment", side_effect=RuntimeError("ledger down"))
	def test_booking_failure_keeps_the_money_fact_and_stops_a_reswipe(self, _rp, alert, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_approved(client, amount=outstanding)
		with self.assertRaises(frappe.ValidationError) as cm:
			api.poll_sales_invoice_charge(si, "PI-TP-1")
		self.assertIn("Do not retry", str(cm.exception))
		rec = self._records(si)[0]
		# The capture is on record (with its gateway reference) even though the
		# ledger side failed — the alert carries the same facts.
		self.assertEqual((rec.status, rec.unique_reference, rec.payment_entry),
			("CHARGED", "REF-TP-1", None))
		self.assertTrue(alert.called)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), outstanding)

	# --- cancel -----------------------------------------------------------

	def test_cancel_marks_the_record_canceled(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, _ = self._unpaid_order()
		self._start(client, si)
		client.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
		client.cancel_payment_instruction.return_value = {"status": "CANCELED"}
		self.assertEqual(api.cancel_sales_invoice_charge(si, "PI-TP-1"),
			{"ok": True, "status": "CANCELED"})
		self.assertEqual(self._records(si)[0].status, "CANCELED")

	def test_cancel_racing_an_approval_settles_it(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_approved(client, amount=outstanding)
		res = api.cancel_sales_invoice_charge(si, "PI-TP-1")
		self.assertTrue(res["ok"] and res["reconciled"])
		self.assertFalse(client.cancel_payment_instruction.called)
		self.assertEqual(self._records(si)[0].status, "CHARGED")

	# --- record -----------------------------------------------------------

	def test_a_charged_record_cannot_be_deleted(self, cc, _p, _r):
		client = cc.from_settings.return_value
		si, outstanding = self._unpaid_order()
		self._start(client, si)
		_approved(client, amount=outstanding)
		api.poll_sales_invoice_charge(si, "PI-TP-1")
		name = self._records(si)[0].name
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(api.TERMINAL_PAYMENT, name, ignore_permissions=True)


class ChargeAmountTest(FrappeTestCase):
	"""The one amount rule both card paths (terminal and saved card) share."""

	def test_blank_amount_takes_the_whole_balance(self):
		self.assertEqual(api._charge_amount("SI-X", 125.004, None), 125.0)
		self.assertEqual(api._charge_amount("SI-X", 125, ""), 125.0)

	def test_partial_and_within_tolerance(self):
		self.assertEqual(api._charge_amount("SI-X", 125, "50"), 50.0)
		self.assertEqual(api._charge_amount("SI-X", 125, 125.01), 125.01)

	def test_rejects_paid_zero_and_over_balance(self):
		for outstanding, amount in ((0.01, None), (125, 0), (125, -5), (125, 125.02)):
			with self.assertRaises(frappe.ValidationError):
				api._charge_amount("SI-X", outstanding, amount)
