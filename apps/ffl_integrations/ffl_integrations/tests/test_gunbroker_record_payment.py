"""Record Payment on a GunBroker order: the counter's payment methods, minus cash.

A GunBroker order is paid before it has an invoice (Dispose raises it), so what is
recorded lives on the order and is replayed onto a Payment Entry when the invoice is
raised, against the account of the Mode of Payment recorded. These run that for real:
a real order ingested through the poll, a real Dispose and a real invoice; only
GunBroker's and Payroc's networks are faked.

* a check keeps its GunBroker mode (and the deposit account in GunBroker Settings);
* Zelle / ACH / a card taken on the Virtual Terminal book to the store's own accounts;
* a card keyed in on the card processor's payment page is recorded as the order's
  payment, and once invoiced the charge is tied to the invoice and its receipt;
* cash is refused.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt, nowdate

from ffl_core import cash_drawer
from ffl_integrations.gunbroker import fulfillment
from ffl_integrations.gunbroker import orders as gb_orders
from ffl_integrations.payments import api
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME
from ffl_integrations.payroc import adapter
from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account, payroc_configured
from ffl_integrations.tests.test_gunbroker_model_accounting import KEY0, _BookedCase
from ffl_integrations.tests.test_payroc import _approved_payment

_PAYROC_CONFIGURED = payroc_configured()
PAGE = "https://pay.example/paymentpage?ORDERID=x"


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _ensure_mode_account(mode: str, company: str) -> str:
	"""A store tender with an account for the test company (a fresh site may have none)."""
	if not frappe.db.exists("Mode of Payment", mode):
		frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mode,
			"type": "Bank", "enabled": 1}).insert(ignore_permissions=True)
	mop = frappe.get_doc("Mode of Payment", mode)
	row = next((r for r in mop.accounts if r.company == company and r.default_account), None)
	if row:
		return row.default_account
	account = cash_drawer.bank_account(company)
	mop.append("accounts", {"company": company, "default_account": account})
	mop.save(ignore_permissions=True)
	return account


class _GbRecordPaymentCase(_BookedCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_credit_card_mop_account()
		cls.zelle_account = _ensure_mode_account("Zelle", cls.company)

	def setUp(self):
		super().setUp()
		self._start(patch.object(fulfillment, "_flag_payment_received"))
		self.gun = self.stock("GBR-A", rate=210)
		self.order = self.ingest(self.detail(qty=1, price=499.0))
		self.total = flt(self.order.order_total)

	def dispose_and_invoice(self):
		self.assign(self.order, KEY0, self.gun)
		self.dispose(self.order)
		self.order.reload()
		si = self.invoice(self.order)
		self.assertTrue(si and si.docstatus == 1, "the invoice was not posted")
		return si


class TypedPayments(_GbRecordPaymentCase):
	def test_zelle_books_to_the_zelle_account_when_invoiced(self):
		out = fulfillment.mark_payment_received(self.order.name, "Zelle", self.total,
			reference_no="ZL-1001", received_on=nowdate())
		self.assertTrue(out["ok"])
		self.order.reload()
		self.assertEqual(self.order.payment_mode_used, "Zelle")
		self.assertEqual(self.order.payment_source, gb_orders.SOURCE_STORE)
		self.assertEqual(fulfillment.order_stage(self.order), fulfillment.STAGE_READY_TO_DISPOSE)

		si = self.dispose_and_invoice()
		pe = frappe.get_doc("Payment Entry", self.order.payment_entry)
		self.assertEqual((pe.mode_of_payment, pe.paid_to, pe.reference_no),
			("Zelle", self.zelle_account, "ZL-1001"))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)

	def test_check_money_order_keeps_the_gunbroker_check_mode(self):
		fulfillment.mark_payment_received(self.order.name, "Check/Money Order", self.total,
			reference_no="4471")
		self.order.reload()
		self.assertEqual(self.order.payment_mode_used, fulfillment.MODE_CHECK)
		self.assertEqual(self.order.payment_source, gb_orders.SOURCE_CHECK)

	def test_cash_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			fulfillment.mark_payment_received(self.order.name, "Cash", self.total)
		self.assertIn("cash", str(caught.exception).lower())
		self.order.reload()
		self.assertFalse(self.order.payment_mode_used)

	def test_a_store_tender_needs_its_reference(self):
		for mode in ("Zelle", "ACH", MODE_OF_PAYMENT_NAME):
			with self.assertRaises(frappe.ValidationError):
				fulfillment.mark_payment_received(self.order.name, mode, self.total, reference_no="")

	def test_a_card_taken_on_the_virtual_terminal_is_not_a_gateway_payment(self):
		# Before this, any mode with "Card" in it read as GunBroker's own checkout and
		# raised the "card money on an account with no gateway" alarm.
		fulfillment.mark_payment_received(self.order.name, MODE_OF_PAYMENT_NAME, self.total,
			reference_no="APP-77")
		self.order.reload()
		self.assertEqual(self.order.payment_source, gb_orders.SOURCE_STORE)
		self.assertEqual(gb_orders.payment_source_for_mode("GunBroker Card"),
			gb_orders.SOURCE_GATEWAY_CHECKOUT)


@patch.object(adapter, "KEYED_CANCEL_GRACE_SECONDS", 0)
@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class KeyedCard(_GbRecordPaymentCase):
	def _approve(self, client, ref="RKG1"):
		client.find_transaction.return_value = {"data": [{"type": "SALE",
			"orderId": self.order.name, "resultCode": "A", "uniqueReference": ref}]}
		client.retrieve_payment.return_value = _approved_payment(ref, self.total)

	def test_a_keyed_card_is_the_orders_payment_and_books_when_invoiced(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.payment_page_url.return_value = PAGE
		out = api.charge_gb_order(self.order.name)
		self.assertEqual(out["keyed_url"], PAGE)
		self.assertEqual(out["instruction_id"], f"keyed:{self.order.name}:{self.total:.2f}")
		client.create_payment_instruction.assert_not_called()

		client.find_transaction.return_value = {"data": []}
		self.assertEqual(api.poll_gb_order_charge(self.order.name, out["instruction_id"])["status"],
			"IN_PROGRESS")
		# Charging again while the page is open hands the same page back.
		again = api.charge_gb_order(self.order.name)
		self.assertEqual(again["instruction_id"], out["instruction_id"])

		self._approve(client)
		res = api.poll_gb_order_charge(self.order.name, out["instruction_id"])
		self.assertTrue(res["ok"] and res["paid"])
		self.order.reload()
		self.assertEqual((self.order.payment_mode_used, self.order.payment_reference_no,
			flt(self.order.payment_amount)), (MODE_OF_PAYMENT_NAME, "RKG1", self.total))
		row = frappe.db.get_value(api.TERMINAL_PAYMENT, {"gunbroker_order": self.order.name},
			["status", "sales_invoice", "payment_entry"], as_dict=True)
		self.assertEqual((row.status, row.sales_invoice, row.payment_entry), ("CHARGED", None, None))

		# Typing a different payment over the charged card is refused.
		with self.assertRaises(frappe.ValidationError):
			fulfillment.mark_payment_received(self.order.name, "Zelle", self.total,
				reference_no="ZL-9")
		# So is charging it again.
		with self.assertRaises(frappe.ValidationError):
			api.charge_gb_order(self.order.name)

		si = self.dispose_and_invoice()
		pe = frappe.get_doc("Payment Entry", self.order.payment_entry)
		card_account = frappe.db.get_value("Mode of Payment Account",
			{"parent": MODE_OF_PAYMENT_NAME, "company": self.company}, "default_account")
		self.assertEqual((pe.mode_of_payment, pe.paid_to, pe.reference_no),
			(MODE_OF_PAYMENT_NAME, card_account, "RKG1"))
		row = frappe.db.get_value(api.TERMINAL_PAYMENT, {"gunbroker_order": self.order.name},
			["sales_invoice", "payment_entry"], as_dict=True)
		self.assertEqual((row.sales_invoice, row.payment_entry), (si.name, pe.name))

	def test_cancel_with_nothing_filed_records_nothing(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		out = api.charge_gb_order(self.order.name)
		self.assertEqual(api.cancel_gb_order_charge(self.order.name, out["instruction_id"])["status"],
			"CANCELED")
		self.order.reload()
		self.assertFalse(self.order.payment_mode_used)

	def test_more_than_the_order_total_is_refused(self, cc, _p, _r):
		with self.assertRaises(frappe.ValidationError):
			api.charge_gb_order(self.order.name, amount=self.total + 5)

	def test_staff_cannot_key_a_card(self, cc, _p, _r):
		with patch("ffl_integrations.payments.api.frappe.get_roles", return_value=["Sales User"]), \
				self.assertRaises(frappe.PermissionError):
			api.charge_gb_order(self.order.name)
