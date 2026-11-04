"""Keyed card entry: the card number is typed on Payroc's hosted payment page (card not present),
never on this server, and the charge is then started / polled / cancelled / booked through the
same paths as a terminal charge. The gateway is mocked at the PayrocClient boundary; the counter
order and POS invoice are real, so the ledger half runs for real."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import BuildOrderMixin
from ffl_integrations.payments import api
from ffl_integrations.payroc import adapter
from ffl_integrations.payroc.adapter import PayrocProcessor
from ffl_integrations.payroc.client import PayrocClient, PayrocConfig, PayrocConfigError
from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account, payroc_configured
from ffl_integrations.tests.test_payroc import _approved_payment, _ensure_pos_fixtures, _pos_draft_invoice

_PAYROC_CONFIGURED = payroc_configured()
PAGE = "https://pay.example/paymentpage?ORDERID=x"


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _config(**kw):
	return PayrocConfig(**{"api_key": "k", "base_url": "https://testpayments.example/merchant",
		"default_terminal": "T100", "default_serial_number": None, "default_currency": "usd",
		"integration_id": None, "timeout": 5, **kw})


class PaymentPageUrl(FrappeTestCase):
	def test_url_is_signed_like_the_woo_plugin_and_carries_no_secret(self):
		secret = frappe.generate_hash(length=16)
		c = PayrocClient(_config(page_secret=secret), session=MagicMock())
		url = c.payment_page_url(order_id="SINV-1-A2", amount=42.5, return_url="https://pos/x",
			now=datetime(2026, 11, 4, 15, 7, 9, tzinfo=timezone.utc))
		u = urlparse(url)
		self.assertEqual(f"{u.scheme}://{u.netloc}{u.path}", "https://testpayments.example/merchant/paymentpage")
		q = {k: v[0] for k, v in parse_qs(u.query).items()}
		self.assertEqual((q["TERMINALID"], q["ORDERID"], q["AMOUNT"], q["CURRENCY"], q["DATETIME"]),
			("T100", "SINV-1-A2", "42.50", "USD", "04-11-2026:15:07:09:000"))
		self.assertEqual(q["HASH"], hashlib.md5(
			f"T100SINV-1-A242.5004-11-2026:15:07:09:000https://pos/x{secret}".encode()).hexdigest())
		self.assertNotIn(secret, url)

	def test_billing_prefills_the_page_outside_the_signature(self):
		secret = frappe.generate_hash(length=16)
		c = PayrocClient(_config(page_secret=secret), session=MagicMock())
		at = datetime(2026, 11, 4, 15, 7, 9, tzinfo=timezone.utc)
		plain = c.payment_page_url(order_id="S1", amount=1, return_url="https://pos/x", now=at)
		url = c.payment_page_url(order_id="S1", amount=1, return_url="https://pos/x", now=at,
			billing={"name": "Ann Lee", "address1": "1 Main St", "postcode": "75001", "country": "US"})
		q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
		self.assertEqual((q["CARDHOLDERNAME"], q["ADDRESS1"], q["POSTCODE"], q["COUNTRY"]),
			("Ann Lee", "1 Main St", "75001", "US"))
		self.assertNotIn("CITY", q)
		self.assertEqual(q["HASH"], parse_qs(urlparse(plain).query)["HASH"][0])

	def test_no_secret_no_page(self):
		c = PayrocClient(_config(), session=MagicMock())
		with self.assertRaises(PayrocConfigError):
			c.payment_page_url(order_id="S", amount=1, return_url="https://pos/x")


@patch.object(adapter, "KEYED_CANCEL_GRACE_SECONDS", 0)
class KeyedHandle(FrappeTestCase):
	def _p(self, rows=()):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.find_transaction.return_value = {"data": list(rows)}
		return p

	def test_handle_carries_order_and_amount_and_opens_the_page(self):
		p = self._p()
		h = p.start_keyed_charge(document="SINV-9", attempt=2, amount=30)
		self.assertEqual(h, "keyed:SINV-9-A2:30.00")
		p._client.payment_page_url.return_value = PAGE
		self.assertEqual(p.keyed_page(h, "https://pos/r"), PAGE)
		self.assertEqual(p._client.payment_page_url.call_args.kwargs,
			{"order_id": "SINV-9-A2", "amount": 30.0, "return_url": "https://pos/r", "billing": None})
		self.assertEqual(p.keyed_page("PI-device-1", "https://pos/r"), "")

	def test_poll_is_pending_until_payroc_files_the_sale(self):
		p = self._p()
		h = p.start_keyed_charge(document="SINV-9", attempt=1, amount=30)
		self.assertEqual(p.poll(h).status, "PENDING")
		p._client.poll_payment_instruction.assert_not_called()
		p._client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": "SINV-9",
			"resultCode": "A", "uniqueReference": "R9"}]}
		p._client.retrieve_payment.return_value = _approved_payment("R9", 30)
		res = p.poll(h)
		self.assertEqual((res.status, res.amount, res.unique_reference), ("APPROVED", 30, "R9"))

	def test_a_sale_that_cannot_be_settled_stays_in_flight(self):
		p = self._p([{"type": "SALE", "orderId": "SINV-9", "resultCode": "E"}])
		res = p.poll(p.start_keyed_charge(document="SINV-9", attempt=1, amount=30))
		self.assertEqual(res.status, "UNKNOWN")
		self.assertTrue(api._still_on_device(res))

	def test_cancel_refuses_once_payroc_holds_an_answer(self):
		p = self._p()
		h = p.start_keyed_charge(document="SINV-9", attempt=1, amount=30)
		self.assertEqual(p.cancel(h).status, "CANCELED")
		p._client.cancel_payment_instruction.assert_not_called()
		p._client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": "SINV-9",
			"resultCode": "D", "uniqueReference": "R0"}]}
		with self.assertRaises(frappe.ValidationError):
			p.cancel(h)


@patch.object(adapter, "KEYED_CANCEL_GRACE_SECONDS", 0)
@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class KeyedSalesInvoice(BuildOrderMixin, FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		ensure_credit_card_mop_account()

	def _order(self):
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Keyed Card Buyer"),
			"dispose_now": 0, "payment_received": 0,
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": 100, "serials": []}],
		})
		return out["sales_invoice"], flt(out["outstanding"])

	def test_keyed_charge_opens_the_page_then_books_the_approval(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.payment_page_url.return_value = PAGE
		si, outstanding = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		self.assertEqual(out["keyed_url"], PAGE)
		self.assertEqual(out["instruction_id"], f"keyed:{si}:{outstanding:.2f}")
		client.create_payment_instruction.assert_not_called()
		self.assertTrue(client.payment_page_url.call_args.kwargs["return_url"].endswith(api.KEYED_RETURN_PATH))

		client.find_transaction.return_value = {"data": []}
		self.assertEqual(api.poll_sales_invoice_charge(si, out["instruction_id"])["status"], "IN_PROGRESS")
		# Charging again while the page is open hands the same page back, never a second charge.
		again = api.charge_sales_invoice(si, keyed=1)
		self.assertEqual((again["instruction_id"], again["keyed_url"]), (out["instruction_id"], PAGE))

		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": si,
			"resultCode": "A", "uniqueReference": "RK1"}]}
		client.retrieve_payment.return_value = _approved_payment("RK1", outstanding)
		res = api.poll_sales_invoice_charge(si, out["instruction_id"])
		self.assertTrue(res["ok"] and res["paid"] and res["payment_entry"])
		self.assertEqual(frappe.db.get_value("Payment Entry", res["payment_entry"], "reference_no"), "RK1")

	def test_the_page_starts_with_the_customers_address(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.payment_page_url.return_value = PAGE
		si, _ = self._order()
		cust = frappe.db.get_value("Sales Invoice", si, "customer")
		addr = frappe.get_doc({"doctype": "Address", "address_title": "Keyed", "address_type": "Billing",
			"address_line1": "12 Oak St", "city": "Dallas", "state": "TX", "pincode": "75201",
			"country": "United States", "links": [{"link_doctype": "Customer", "link_name": cust}]}).insert()
		frappe.db.set_value("Sales Invoice", si, "customer_address", addr.name)
		api.charge_sales_invoice(si, keyed=1)
		billing = client.payment_page_url.call_args.kwargs["billing"]
		self.assertEqual((billing["address1"], billing["postcode"], billing["region"], billing["country"]),
			("12 Oak St", "75201", "TX", "US"))
		self.assertEqual(billing["name"], frappe.db.get_value("Sales Invoice", si, "customer_name"))

	def test_cancel_with_nothing_filed_cancels(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		si, _ = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		self.assertEqual(api.cancel_sales_invoice_charge(si, out["instruction_id"])["status"], "CANCELED")
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT,
			{"sales_invoice": si}, "status"), "CANCELED")

	def _approve_at_payroc(self, client, si, amount, ref="RK2"):
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": si,
			"resultCode": "A", "uniqueReference": ref}]}
		client.retrieve_payment.return_value = _approved_payment(ref, amount)

	def test_cancel_refused_keeps_it_in_flight_and_the_next_charge_records_it(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		si, outstanding = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		# The poll the cancel starts with still sees nothing; the approval lands during the wait.
		client.find_transaction.side_effect = [{"data": []}, {"data": [{"type": "SALE",
			"orderId": si, "resultCode": "A", "uniqueReference": "RK2"}]}]
		client.retrieve_payment.return_value = _approved_payment("RK2", outstanding)
		with self.assertRaises(frappe.ValidationError):
			api.cancel_sales_invoice_charge(si, out["instruction_id"])
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si}, "status"),
			"IN_PROGRESS")
		client.find_transaction.side_effect = None
		self._approve_at_payroc(client, si, outstanding)
		res = api.charge_sales_invoice(si, keyed=1)
		self.assertTrue(res["ok"] and res.get("already") and res["payment_entry"])

	def test_an_approval_after_the_cancel_is_recorded_not_charged_again(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		si, outstanding = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		self.assertEqual(api.cancel_sales_invoice_charge(si, out["instruction_id"])["status"], "CANCELED")
		self._approve_at_payroc(client, si, outstanding)   # Payroc lists it only now
		res = api.charge_sales_invoice(si, keyed=1)
		self.assertTrue(res["ok"] and res.get("already") and res["paid"])
		self.assertEqual(frappe.db.count(api.TERMINAL_PAYMENT, {"sales_invoice": si}), 1)

	def test_an_unsettled_sale_after_the_cancel_stops_the_next_charge(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		si, _ = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		api.cancel_sales_invoice_charge(si, out["instruction_id"])
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": si, "resultCode": "E"}]}
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-X", "status": "IN_PROGRESS"}
		with self.assertRaises(frappe.ValidationError):
			api.charge_sales_invoice(si)          # the device too: never a second charge
		client.create_payment_instruction.assert_not_called()
		self.assertEqual(frappe.db.get_value(api.TERMINAL_PAYMENT, {"sales_invoice": si}, "status"), "CANCELED")

	def test_a_device_charge_records_a_late_keyed_approval_first(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		si, outstanding = self._order()
		out = api.charge_sales_invoice(si, keyed=1)
		api.cancel_sales_invoice_charge(si, out["instruction_id"])
		self._approve_at_payroc(client, si, outstanding)
		with patch("ffl_integrations.payments.api.frappe.get_roles", return_value=["Sales User"]):
			res = api.charge_sales_invoice(si)
		self.assertTrue(res["ok"] and res.get("already") and res["paid"])
		client.create_payment_instruction.assert_not_called()

	def test_staff_cannot_resume_a_managers_keyed_charge(self, cc, _p, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		client.payment_page_url.return_value = PAGE
		si, _ = self._order()
		api.charge_sales_invoice(si, keyed=1)
		with patch("ffl_integrations.payments.api.frappe.get_roles", return_value=["Sales User"]):
			self.assertRaises(frappe.PermissionError, api.charge_sales_invoice, si)

	def test_refused_for_staff_without_setup_and_with_save_card(self, cc, _p, _r):
		si, _ = self._order()
		with patch("ffl_integrations.payments.api.frappe.get_roles", return_value=["Sales User"]):
			self.assertRaises(frappe.PermissionError, api.charge_sales_invoice, si, keyed=1)
		with patch.object(PayrocProcessor, "supports_keyed_entry", return_value=False):
			self.assertRaises(frappe.ValidationError, api.charge_sales_invoice, si, keyed=1)
		with patch("ffl_integrations.payments.saved_cards.save_request",
				return_value={"reference": "M1", "usage": "UNSCHEDULED"}):
			self.assertRaises(frappe.ValidationError, api.charge_sales_invoice, si, keyed=1, save_card=1)
		self.assertFalse(frappe.db.exists(api.TERMINAL_PAYMENT, {"sales_invoice": si}))


@patch.object(adapter, "KEYED_CANCEL_GRACE_SECONDS", 0)
@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payroc.adapter.PayrocClient")
@patch("ffl_integrations.payments.api._check_perm")
class KeyedPosInvoice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _ensure_pos_fixtures()

	def test_keyed_charge_settles_a_card_row(self, _p, cc, _r):
		client = cc.from_settings.return_value
		client.payment_page_url.return_value = PAGE
		inv = _pos_draft_invoice(rate=100)
		out = api.charge_pos_invoice(inv, amount=100, keyed=1)
		self.assertEqual(out["keyed_url"], PAGE)
		client.create_payment_instruction.assert_not_called()
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": inv,
			"resultCode": "A", "uniqueReference": "RKP"}]}
		client.retrieve_payment.return_value = _approved_payment("RKP", 100)
		res = api.poll_pos_charge(inv, out["instruction_id"])
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("POS Invoice", inv,
			["card_status", "card_unique_reference"]), ("CHARGED", "RKP"))

	def test_an_approval_after_the_cancel_is_recorded_not_charged_again(self, _p, cc, _r):
		client = cc.from_settings.return_value
		client.find_transaction.return_value = {"data": []}
		inv = _pos_draft_invoice(rate=100)
		out = api.charge_pos_invoice(inv, amount=100, keyed=1)
		self.assertEqual(api.cancel_pos_charge(inv, out["instruction_id"])["status"], "CANCELED")
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": inv,
			"resultCode": "A", "uniqueReference": "RKL"}]}
		client.retrieve_payment.return_value = _approved_payment("RKL", 100)
		# The next charge records the late approval; the card is fully paid, so no new charge.
		with self.assertRaises(frappe.ValidationError):
			api.charge_pos_invoice(inv, amount=100, keyed=1)
		self.assertEqual(frappe.db.get_value("POS Invoice", inv,
			["card_status", "card_unique_reference"]), ("CHARGED", "RKL"))
