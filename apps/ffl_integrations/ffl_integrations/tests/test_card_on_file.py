"""Card on file: save a card at the terminal, charge it later, remove it (payments/saved_cards.py).

Two layers, like test_payroc:
* Payroc client against a mocked requests Session — the exact bodies and URLs card on file sends
  (tokenizing device sale, merchant-initiated payment, credential delete, terminal load), and
  the adapter reading the token off a recorded Payment (``transactionResult.
  storedPaymentCredentials.credentialsNumber``) with ``raw`` masked.
* The neutral orchestrator against the REAL schema (POS Invoice charge rows, counter Sales
  Invoices, Payment Entries, Saved Card with its encrypted token), with only PayrocClient and the
  register's permission/ready gates mocked.
"""
from __future__ import annotations

import json as _json
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, flt, now_datetime

from ffl_core.api import manual_order
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.tests.test_build_order import BuildOrderMixin
from ffl_integrations.payments import api, hub, saved_cards
from ffl_integrations.payroc import client as wn_client
from ffl_integrations.payroc.adapter import PayrocProcessor
from ffl_integrations.payroc.client import SANDBOX_BASE, PayrocClient, PayrocConfig, PayrocError
from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account, payroc_configured
from ffl_integrations.tests.test_card_processor_seam import FAKE, PAYROC, FakeGateway, card_hooks
from ffl_integrations.tests.test_payroc import _ensure_pos_fixtures, _mock_response, _pos_draft_invoice

TOKEN = "2967530000001234"            # Payroc Secure Credentials numbers carry BIN 296753
SETTINGS = saved_cards.SETTINGS
PAYMENT_HREF = "https://x/merchant/api/v1/transaction/payments/REF-COF-1"

_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
	_PAYROC_CONFIGURED.start()


def tearDownModule():
	_PAYROC_CONFIGURED.stop()


def _config(**kw) -> PayrocConfig:
	return PayrocConfig(api_key="testkey", base_url=SANDBOX_BASE, default_terminal="4480001",
		default_serial_number="SIM-0001", default_currency="USD", integration_id=None, timeout=30, **kw)


def _payment(ref="REF-COF-1", amount=50, *, code="A", token=TOKEN, reference="ref-1"):
	"""A Payroc Payment as the API returns it, Secure Credentials included when ``token``."""
	tr = {"resultCode": code, "authorizedAmount": amount if code == "A" else 0,
		"approvalCode": "OK1" if code == "A" else "", "resultMessage": "Approved" if code == "A" else "Declined"}
	if token:
		tr["storedPaymentCredentials"] = {"terminal": "4480001", "usageAgreement": "UNSCHEDULED",
			"merchantReference": reference, "credentialsNumber": token}
	return {"uniqueReference": ref, "terminal": "4480001",
		"customerAccount": {"cardType": "Visa", "maskedPan": "411111******1111", "expiryDate": "1229"},
		"transactionResult": tr}


def _uniq(prefix: str) -> str:
	return f"{prefix} {frappe.generate_hash(length=6)}"


# ------------------------------------------------------------------ Payroc wire shapes


class PayrocCardOnFileWire(FrappeTestCase):
	def setUp(self):
		wn_client._bearer.token = ""
		wn_client._bearer.expires_at = 0.0

	def _client(self, status=200, body=None, **cfg):
		session = MagicMock()
		session.get.return_value = _mock_response(200, {"token": "JWT1", "expiresIn": 1})
		session.request.return_value = _mock_response(status, body if body is not None else {})
		return PayrocClient(_config(**cfg), session=session), session

	def test_tokenizing_device_sale_sends_credential_on_file(self):
		c, session = self._client(202, {"paymentInstructionId": "PI1", "status": "IN_PROGRESS"})
		c.create_payment_instruction(amount=10, order_id="POS-1", credential_on_file={
			"tokenize": True, "merchantReference": "site-cust-abc12345", "usageAgreement": "UNSCHEDULED"})
		body = session.request.call_args.kwargs["json"]
		self.assertEqual(body["credentialOnFile"], {"tokenize": True,
			"merchantReference": "site-cust-abc12345", "usageAgreement": "UNSCHEDULED"})
		self.assertTrue(session.request.call_args.args[1].endswith(
			"/api/v1/transaction/devices/SIM-0001/paymentInstructions"))

	def test_device_sale_without_card_on_file_is_unchanged(self):
		c, session = self._client(202, {"paymentInstructionId": "PI1"})
		c.create_payment_instruction(amount=10, order_id="POS-1")
		self.assertNotIn("credentialOnFile", session.request.call_args.kwargs["json"])

	def test_merchant_initiated_payment_body(self):
		c, session = self._client(201, _payment())
		c.charge_stored_credential(credentials_number=TOKEN, amount=12.5, order_id="ACC-SINV-1-A2",
			usage="UNSCHEDULED", first_reference="REF0000001", description="ACC-SINV-1 — Jane",
			operator="cashier@example.com")
		args, kw = session.request.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], f"{SANDBOX_BASE}/api/v1/transaction/payments")
		body = kw["json"]
		self.assertEqual(body["channel"], "MOTO")
		self.assertEqual(body["terminal"], "4480001")
		self.assertEqual(body["customerAccount"], {"payloadType": "SECURE_CREDENTIALS",
			"credentialsNumber": TOKEN})
		self.assertEqual(body["order"]["orderId"], "ACC-SINV-1-A2")
		self.assertEqual(body["order"]["totalAmount"], 12.5)
		self.assertEqual(body["order"]["currency"], "USD")
		self.assertEqual(body["order"]["standingInstructions"], {"sequence": "SUBSEQUENT",
			"processingModel": "UNSCHEDULED", "referenceDataOfFirstTxn": {"uniqueReference": "REF0000001"}})
		self.assertTrue(body["autoCapture"])
		self.assertFalse(body["processAsSale"])
		self.assertNotIn("credentialOnFile", body)

	def test_merchant_initiated_payment_uses_the_saved_card_terminal_when_set(self):
		c, session = self._client(201, _payment(), saved_card_terminal="4480009")
		c.charge_stored_credential(credentials_number=TOKEN, amount=1, order_id="X", usage="RECURRING")
		body = session.request.call_args.kwargs["json"]
		self.assertEqual(body["terminal"], "4480009")
		self.assertEqual(body["order"]["standingInstructions"],
			{"sequence": "SUBSEQUENT", "processingModel": "RECURRING"})

	def test_delete_credentials_url(self):
		c, session = self._client(204, None)
		session.request.return_value = _mock_response(204, None, content_set=False)
		c.delete_credentials("site-Jane Roe/x-abc")
		args = session.request.call_args.args
		self.assertEqual(args[0], "DELETE")
		self.assertEqual(args[1], f"{SANDBOX_BASE}/api/v1/customer/credentials/"
			"site-Jane%20Roe%2Fx-abc?terminal=4480001")

	def test_load_terminal_url(self):
		c, session = self._client(200, {"terminalNumber": "4480001"})
		c.load_terminal()
		self.assertEqual(session.request.call_args.args[:2],
			("GET", f"{SANDBOX_BASE}/api/v1/account/terminals/4480001"))

	def test_poll_reads_the_token_off_the_payment_and_masks_raw(self):
		"""The pinned response path: transactionResult.storedPaymentCredentials.credentialsNumber."""
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.poll_payment_instruction.return_value = {"status": "COMPLETED",
			"links": [{"rel": "payment", "href": PAYMENT_HREF}]}
		p._client.payment_link.return_value = PAYMENT_HREF
		p._client.retrieve_payment.return_value = _payment()
		res = p.poll("PI1")
		self.assertEqual((res.status, res.stored_credential, res.card_expiry), ("APPROVED", TOKEN, "1229"))
		self.assertNotIn(TOKEN, _json.dumps(res.raw))
		self.assertEqual(res.raw["transactionResult"]["storedPaymentCredentials"]["merchantReference"], "ref-1")

	def test_adapter_passes_save_card_as_credential_on_file(self):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.create_payment_instruction.return_value = {"paymentInstructionId": "PI9"}
		p.start_charge(document="POS-1", attempt=1, amount=5, terminal=None, description="d",
			operator="o", save_card={"reference": "r-1", "usage": "UNSCHEDULED"})
		self.assertEqual(p._client.create_payment_instruction.call_args.kwargs["credential_on_file"],
			{"tokenize": True, "merchantReference": "r-1", "usageAgreement": "UNSCHEDULED"})
		p.start_charge(document="POS-1", attempt=2, amount=5, terminal=None, description="d", operator="o")
		self.assertIsNone(p._client.create_payment_instruction.call_args.kwargs["credential_on_file"])

	def test_charge_stored_sorts_failures_and_scrubs_the_token(self):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.charge_stored_credential.side_effect = PayrocError(
			f"Payroc POST /payments 400: bad credentialsNumber {TOKEN}", outcome_known=True)
		with self.assertRaises(PayrocError) as ctx:
			p.charge_stored(token=TOKEN, amount=1, document="SI", attempt=2, description="", operator="")
		self.assertTrue(ctx.exception.outcome_known)
		self.assertNotIn(TOKEN, str(ctx.exception))
		for boom in (requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.ConnectionError("x")):
			p._client.charge_stored_credential.side_effect = boom
			with self.assertRaises(Exception) as ctx:
				p.charge_stored(token=TOKEN, amount=1, document="SI", attempt=2, description="", operator="")
			self.assertFalse(ctx.exception.outcome_known)
		# E = accepted, result unknown: never read as declined.
		p._client.charge_stored_credential.side_effect = None
		p._client.charge_stored_credential.return_value = _payment(code="E", token=None)
		self.assertEqual(p.charge_stored(token=TOKEN, amount=1, document="SI", attempt=2,
			description="", operator="").status, "UNKNOWN")

	def test_find_stored_charge(self):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.find_transaction.return_value = {"data": []}
		self.assertEqual(p.find_stored_charge("SI-A2").status, "UNKNOWN")
		p._client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": "SI-A2",
			"resultCode": "D", "uniqueReference": "R1", "resultMessage": "Do not honor"}]}
		self.assertEqual(p.find_stored_charge("SI-A2").status, "DECLINED")
		p._client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": "SI-A2",
			"resultCode": "A", "uniqueReference": "R2"}]}
		p._client.retrieve_payment.return_value = _payment("R2", 30, token=None)
		res = p.find_stored_charge("SI-A2")
		self.assertEqual((res.status, res.amount, res.unique_reference), ("APPROVED", 30, "R2"))

	def test_delete_stored_treats_404_as_removed(self):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.delete_credentials.side_effect = PayrocError("Payroc DELETE /x 404: not found")
		p.delete_stored("r-1")   # no raise
		p._client.delete_credentials.side_effect = PayrocError("Payroc DELETE /x 500: boom", outcome_known=False)
		with self.assertRaises(PayrocError):
			p.delete_stored("r-1")

	def test_probe_shows_saved_cards_and_never_fails_on_it(self):
		p = PayrocProcessor()
		p._client = MagicMock()
		p._client.account_summary.return_value = {"allowedTerminals": ["4480001"]}
		with patch.object(p, "enabled", return_value=True), patch.object(p, "ready", return_value=True), \
				patch("ffl_integrations.payroc.adapter.frappe.db.get_single_value", return_value="4480001"):
			p._client.load_terminal.side_effect = PayrocError("Payroc GET 503: down", outcome_known=False)
			checks = {c["label"]: c for c in p.probe()}
			self.assertIsNone(checks["Saved cards"]["ok"])
			self.assertIn("unknown", checks["Saved cards"]["detail"])
			self.assertTrue(checks["Gateway"]["ok"])
			p._client.load_terminal.side_effect = None
			p._client.load_terminal.return_value = {"terminalFeatures": {"secureCredentials": {"enable": True}}}
			self.assertTrue({c["label"]: c for c in p.probe()}["Saved cards"]["ok"])
			p._client.load_terminal.return_value = {"terminalFeatures": {"secureCredentials": {"enable": False}}}
			self.assertIn("not enabled", {c["label"]: c for c in p.probe()}["Saved cards"]["detail"])


# ------------------------------------------------------------------ orchestrator (real schema)


class _CardOnFileBase(SettingsSandbox, BuildOrderMixin, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _ensure_pos_fixtures()
		ensure_credit_card_mop_account()
		tpl = frappe.get_doc({"doctype": "Agreement Template", "title": _uniq("Card on file"),
			"body": "<p>I, {{ customer_name }}, let the store charge my saved card.</p>",
			"scope": "Per customer"})
		tpl.insert(ignore_permissions=True)
		cls.template = tpl.name

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.set_single(SETTINGS, "card_on_file_enabled", 1)
		self.set_single(SETTINGS, "card_on_file_template", self.template)

	def _customer(self, signed=True) -> str:
		name = self._indiv(_uniq("COF Buyer"))
		if signed:
			from ffl_core.agreements import request_agreement
			r = request_agreement(name, self.template, deliver="none")
			frappe.db.set_value("Customer Agreement", r["name"], "status", "Signed")
		return name

	def _pos_invoice(self, customer, rate=50):
		with patch("ffl_integrations.tests.test_payroc._RW_CUSTOMER", customer):
			return _pos_draft_invoice(rate=rate)

	def _unpaid_order(self, customer, rate=100):
		out = manual_order.create_order({"buyer_type": "individual", "customer": customer,
			"dispose_now": 0, "payment_received": 0,
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": rate, "serials": []}]})
		return out["sales_invoice"], flt(out["outstanding"])

	@staticmethod
	def _approve(client, payment):
		client.poll_payment_instruction.return_value = {"status": "COMPLETED",
			"links": [{"rel": "payment", "href": PAYMENT_HREF}]}
		client.payment_link.return_value = PAYMENT_HREF
		client.retrieve_payment.return_value = payment

	@staticmethod
	def _cards(customer):
		return frappe.get_all(saved_cards.SAVED_CARD, filters={"customer": customer},
			fields=["name", "status", "is_default", "card_summary", "card_expiry", "authorization",
				"merchant_reference", "initial_reference", "source_doctype", "source_name", "token"])

	def _assert_no_token_anywhere(self, *payloads):
		for p in payloads:
			self.assertNotIn(TOKEN, _json.dumps(p, default=str))
		for dt, field in (("Card Charge", "response"), ("Card Terminal Payment", "response"),
				("POS Invoice", "card_response"), ("Saved Card", "token"), ("Error Log", "error")):
			self.assertFalse(frappe.db.sql(f"select 1 from `tab{dt}` where `{field}` like %s limit 1",
				(f"%{TOKEN}%",)), f"token found in {dt}.{field}")


@patch("ffl_integrations.payments.api._integration_ready", return_value=True)
@patch("ffl_integrations.payments.api._check_perm")
@patch("ffl_integrations.payroc.adapter.PayrocClient")
class SavingAtTheTerminal(_CardOnFileBase):
	def test_register_sale_saves_the_card_once(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		inv = self._pos_invoice(cust)
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-S1", "status": "IN_PROGRESS"}
		started = api.charge_pos_invoice(inv, save_card=1)
		cof = client.create_payment_instruction.call_args.kwargs["credential_on_file"]
		self.assertTrue(cof["tokenize"])
		self.assertEqual(cof["usageAgreement"], "UNSCHEDULED")
		self.assertTrue(cof["merchantReference"].endswith(cof["merchantReference"][-8:]))
		self.assertLessEqual(len(cof["merchantReference"]), 200)
		row = api._charge_rows(inv)[0]
		self.assertEqual((row.save_card, row.save_reference), (1, cof["merchantReference"]))

		self._approve(client, _payment(amount=50, reference=cof["merchantReference"]))
		first = api.poll_pos_charge(inv, "PI-S1")
		again = api.poll_pos_charge(inv, "PI-S1")            # a re-poll adds none
		self.assertTrue(first["ok"] and first["card_saved"])
		self.assertTrue(again["card_saved"])
		cards = self._cards(cust)
		self.assertEqual(len(cards), 1)
		card = cards[0]
		self.assertEqual((card.status, card.is_default, card.card_summary, card.card_expiry,
			card.merchant_reference, card.initial_reference, card.source_doctype, card.source_name),
			("Active", 1, "Visa •1111", "1229", cof["merchantReference"], "REF-COF-1", "POS Invoice", inv))
		self.assertTrue(card.authorization)
		self.assertNotEqual(card.token, TOKEN)            # the column holds a mask, __Auth the cipher
		self.assertEqual(frappe.get_doc(saved_cards.SAVED_CARD, card.name).get_password("token"), TOKEN)
		self.assertEqual(api._charge_rows(inv)[0].saved_card, card.name)
		self._assert_no_token_anywhere(started, first, again,
			saved_cards.list_saved_cards(cust), saved_cards.card_on_file_offer(cust))

	def test_counter_order_saves_and_second_card_is_not_default(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		for i, iid in enumerate(("PI-C1", "PI-C2")):
			si, outstanding = self._unpaid_order(cust)
			client.create_payment_instruction.return_value = {"paymentInstructionId": iid, "status": "IN_PROGRESS"}
			api.charge_sales_invoice(si, save_card=1)
			ref = client.create_payment_instruction.call_args.kwargs["credential_on_file"]["merchantReference"]
			self._approve(client, _payment(f"REF-C{i}", outstanding, token=f"29675300000099{i}0", reference=ref))
			res = api.poll_sales_invoice_charge(si, iid)
			self.assertTrue(res["ok"] and res["paid"] and res["card_saved"])
			self.assertTrue(res["payment_entry"])
		cards = sorted(self._cards(cust), key=lambda c: c.initial_reference)
		self.assertEqual([(c.initial_reference, c.is_default) for c in cards], [("REF-C0", 1), ("REF-C1", 0)])

	def test_approved_without_a_token_keeps_the_sale_and_saves_nothing(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		si, outstanding = self._unpaid_order(cust)
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-NT", "status": "IN_PROGRESS"}
		api.charge_sales_invoice(si, save_card=1)
		self._approve(client, _payment(amount=outstanding, token=None))
		res = api.poll_sales_invoice_charge(si, "PI-NT")
		self.assertTrue(res["ok"] and res["paid"])
		self.assertFalse(res["card_saved"])
		self.assertIn("not saved", res["save_message"])
		self.assertEqual(self._cards(cust), [])

	def test_no_signed_authorization_refuses_before_the_gateway(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer(signed=False)
		inv = self._pos_invoice(cust)
		si, _ = self._unpaid_order(cust)
		for call in (lambda: api.charge_pos_invoice(inv, save_card=1),
				lambda: api.charge_sales_invoice(si, save_card=1)):
			with self.assertRaises(frappe.ValidationError) as ctx:
				call()
			self.assertIn("has not signed", str(ctx.exception))
		client.create_payment_instruction.assert_not_called()
		client.poll_payment_instruction.assert_not_called()
		self.assertFalse(saved_cards.card_on_file_offer(cust)["state"] in ("signed", "expiring"))

	def test_switched_off_or_walk_in_refuses(self, cc, _p, _r):
		client = cc.from_settings.return_value
		cust = self._customer()
		si, _ = self._unpaid_order(cust)
		self.set_single(SETTINGS, "card_on_file_enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			api.charge_sales_invoice(si, save_card=1)
		self.assertFalse(saved_cards.card_on_file_offer(cust)["offer"])
		self.assertFalse(saved_cards.offer_enabled())
		self.set_single(SETTINGS, "card_on_file_enabled", 1)
		self.assertTrue(saved_cards.offer_enabled())
		with patch("ffl_core.customer_profile._system_customer_names", return_value={cust}):
			with self.assertRaises(frappe.ValidationError):
				api.charge_sales_invoice(si, save_card=1)
		client.create_payment_instruction.assert_not_called()

	def test_feature_off_charge_is_the_old_request(self, cc, _p, _r):
		client = cc.from_settings.return_value
		self.set_single(SETTINGS, "card_on_file_enabled", 0)
		cust = self._customer()
		si, outstanding = self._unpaid_order(cust)
		client.create_payment_instruction.return_value = {"paymentInstructionId": "PI-OFF", "status": "IN_PROGRESS"}
		api.charge_sales_invoice(si)
		self.assertIsNone(client.create_payment_instruction.call_args.kwargs["credential_on_file"])
		self._approve(client, _payment(amount=outstanding, token=None))
		res = api.poll_sales_invoice_charge(si, "PI-OFF")
		self.assertNotIn("card_saved", res)
		self.assertNotIn("save_message", res)

	def test_settings_need_an_active_per_customer_template(self, cc, _p, _r):
		doc = frappe.get_doc(SETTINGS)
		doc.card_on_file_enabled = 1
		doc.card_on_file_template = None
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		per_gun = frappe.get_doc({"doctype": "Agreement Template", "title": _uniq("Per gun"),
			"body": "<p>x</p>", "scope": "Each firearm sold"}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			hub.save_card_on_file(1, per_gun.name)
		self.assertEqual(hub.save_card_on_file(1, self.template), {"enabled": 1, "template": self.template})
		self.assertEqual(hub.overview()["card_on_file"]["template"], self.template)


class ProcessorWithoutCardOnFile(_CardOnFileBase):
	def setUp(self):
		super().setUp()
		FakeGateway.reset()
		self.addCleanup(FakeGateway.reset)

	def test_no_offer_and_save_card_throws(self):
		self.set_single("Payroc Settings", "enabled", 0)
		cust = self._customer()
		inv = self._pos_invoice(cust)
		with card_hooks(PAYROC, FAKE), patch("ffl_integrations.payments.api._check_perm"):
			self.assertFalse(saved_cards.offer_enabled())
			self.assertFalse(saved_cards.card_on_file_offer(cust)["offer"])
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.charge_pos_invoice(inv, save_card=1)
			self.assertIn("cannot save cards", str(ctx.exception))
			# Without save_card the fake processor (whose start_charge takes no save_card) charges.
			out = api.charge_pos_invoice(inv)
		self.assertTrue(out["ok"])
		self.assertEqual(FakeGateway.calls[0][0], "start")


@patch("ffl_integrations.payroc.adapter.PayrocClient")
class ChargingASavedCard(_CardOnFileBase):
	def _saved_card(self, customer, token=TOKEN) -> str:
		doc = frappe.get_doc({"doctype": saved_cards.SAVED_CARD, "customer": customer, "processor": "payroc",
			"token": token, "merchant_reference": f"test-{frappe.generate_hash(length=10)}",
			"card_summary": "Visa •1111", "card_expiry": "1229", "usage": "UNSCHEDULED", "status": "Active",
			"is_default": 1, "initial_reference": "REF0000001",
			"authorization": saved_cards.card_on_file_offer(customer)["agreement"]})
		doc.insert(ignore_permissions=True)
		return doc.name

	def _rows(self, si):
		return frappe.get_all(api.TERMINAL_PAYMENT, filters={"sales_invoice": si},
			fields=["name", "status", "amount", "instruction_id", "payment_entry", "saved_card", "response"],
			order_by="creation asc")

	def test_approval_books_a_payment_entry(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		card = self._saved_card(cust)
		si, outstanding = self._unpaid_order(cust)
		client.charge_stored_credential.return_value = _payment("REF-M1", outstanding)  # MIT echoes the token
		res = saved_cards.charge_saved_card(si, card)
		self.assertTrue(res["ok"] and res["paid"])
		kw = client.charge_stored_credential.call_args.kwargs
		self.assertEqual((kw["credentials_number"], kw["amount"], kw["order_id"], kw["usage"], kw["first_reference"]),
			(TOKEN, outstanding, si, "UNSCHEDULED", "REF0000001"))
		row = self._rows(si)[0]
		self.assertEqual((row.status, row.saved_card, row.instruction_id), ("CHARGED", card, si))
		pe = frappe.get_doc("Payment Entry", row.payment_entry)
		self.assertEqual((pe.docstatus, flt(pe.paid_amount), pe.reference_no), (1, outstanding, "REF-M1"))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), 0)
		self._assert_no_token_anywhere(res)

	def test_partial_amount_and_decline(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		card = self._saved_card(cust)
		si, outstanding = self._unpaid_order(cust)
		client.charge_stored_credential.return_value = _payment("REF-D", 0, code="D", token=None)
		res = saved_cards.charge_saved_card(si, card, amount=40)
		self.assertFalse(res["ok"])
		row = self._rows(si)[0]
		self.assertEqual((row.status, row.payment_entry), ("DECLINED", None))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), outstanding)
		# The next attempt goes out under its own order id.
		client.charge_stored_credential.return_value = _payment("REF-P", 40)
		self.assertTrue(saved_cards.charge_saved_card(si, card, amount=40)["ok"])
		self.assertEqual(client.charge_stored_credential.call_args.kwargs["order_id"], f"{si}-A2")
		self.assertAlmostEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")),
			outstanding - 40, places=2)

	def test_unknown_outcome_locks_then_lookup_settles_without_a_second_charge(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		card = self._saved_card(cust)
		si, outstanding = self._unpaid_order(cust)
		client.charge_stored_credential.side_effect = requests.exceptions.ReadTimeout("read timed out")
		with self.assertRaises(frappe.ValidationError) as ctx:
			saved_cards.charge_saved_card(si, card)
		self.assertIn("Do not charge this card again", str(ctx.exception))
		row = self._rows(si)[0]
		self.assertEqual((row.status, row.payment_entry), ("IN_PROGRESS", None))
		# The device terminal refuses to run over it.
		with patch("ffl_integrations.payments.api._integration_ready", return_value=True), \
				patch("ffl_integrations.payments.api._check_perm"):
			with self.assertRaises(frappe.ValidationError):
				api.charge_sales_invoice(si)
		# The gateway has not filed it yet → still locked, still no second charge.
		client.find_transaction.return_value = {"data": []}
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, card)
		# Now it has: the retry settles it from the lookup.
		client.find_transaction.return_value = {"data": [{"type": "SALE", "orderId": si,
			"resultCode": "A", "uniqueReference": "REF-LATE"}]}
		client.retrieve_payment.return_value = _payment("REF-LATE", outstanding)
		res = saved_cards.charge_saved_card(si, card)
		self.assertTrue(res["ok"] and res["resolved"] and res["already"] and res["paid"])
		self.assertEqual(client.charge_stored_credential.call_count, 1)
		self.assertEqual(client.find_transaction.call_args.kwargs["order_id"], si)
		row = self._rows(si)[0]
		self.assertEqual(row.status, "CHARGED")
		self.assertEqual(frappe.db.count("Payment Entry Reference", {"reference_doctype": "Sales Invoice",
			"reference_name": si, "docstatus": 1}), 1)
		self._assert_no_token_anywhere(res)

	def test_lookup_that_never_finds_it_closes_the_record_after_the_grace_period(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		card = self._saved_card(cust)
		si, _ = self._unpaid_order(cust)
		client.charge_stored_credential.side_effect = PayrocError("Payroc POST 502: bad gateway",
			outcome_known=False)
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, card)
		row = self._rows(si)[0]
		frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, "creation", add_to_date(now_datetime(), minutes=-30),
			update_modified=False)
		client.find_transaction.return_value = {"data": []}
		res = saved_cards.charge_saved_card(si, card)
		self.assertEqual((res["ok"], res["status"], res["resolved"]), (False, "CANCELED", True))
		self.assertEqual(self._rows(si)[0].status, "CANCELED")
		self.assertEqual(client.charge_stored_credential.call_count, 1)

	def test_gateway_refusal_records_nothing_charged_and_scrubs_the_token(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		card = self._saved_card(cust)
		si, outstanding = self._unpaid_order(cust)
		client.charge_stored_credential.side_effect = PayrocError(
			f"Payroc POST /payments 422: credentialsNumber {TOKEN} invalid", outcome_known=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			saved_cards.charge_saved_card(si, card)
		self.assertNotIn(TOKEN, str(ctx.exception))
		self.assertEqual(self._rows(si)[0].status, "CANCELED")
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), outstanding)
		self._assert_no_token_anywhere()

	def test_refusals_before_the_gateway(self, cc):
		client = cc.from_settings.return_value
		cust, other = self._customer(), self._customer()
		card = self._saved_card(cust)
		other_card = self._saved_card(other)
		si, outstanding = self._unpaid_order(cust)
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, other_card)                 # another customer's card
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, card, amount=outstanding + 5)   # more than owed
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, card, amount=0)
		draft, _ = self._unpaid_order(cust)
		frappe.db.set_value("Sales Invoice", draft, "docstatus", 0)      # stands in for a draft
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(draft, card)                   # not submitted
		frappe.db.set_value(saved_cards.SAVED_CARD, card, "status", "Removed")
		with self.assertRaises(frappe.ValidationError):
			saved_cards.charge_saved_card(si, card)                      # removed
		client.charge_stored_credential.assert_not_called()
		self.assertEqual(self._rows(si), [])

	def test_remove_goes_to_the_gateway_first(self, cc):
		client = cc.from_settings.return_value
		cust = self._customer()
		first = self._saved_card(cust)
		second = self._saved_card(cust)
		frappe.db.set_value(saved_cards.SAVED_CARD, second, "is_default", 0)
		client.delete_credentials.side_effect = PayrocError("Payroc DELETE 500: down", outcome_known=False)
		with self.assertRaises(frappe.ValidationError):
			saved_cards.remove_saved_card(first, "customer asked")
		self.assertEqual(frappe.db.get_value(saved_cards.SAVED_CARD, first, "status"), "Active")
		client.delete_credentials.side_effect = None
		self.assertEqual(saved_cards.remove_saved_card(first, "customer asked"), {"ok": True, "status": "Removed"})
		ref = frappe.db.get_value(saved_cards.SAVED_CARD, first, "merchant_reference")
		client.delete_credentials.assert_called_with(ref)
		gone = frappe.db.get_value(saved_cards.SAVED_CARD, first,
			["status", "is_default", "removed_by", "removed_reason"], as_dict=True)
		self.assertEqual((gone.status, gone.is_default, gone.removed_reason), ("Removed", 0, "customer asked"))
		self.assertEqual(frappe.db.get_value(saved_cards.SAVED_CARD, second, "is_default"), 1)
		self.assertEqual([c["name"] for c in saved_cards.list_saved_cards(cust)["cards"]], [second])
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(saved_cards.SAVED_CARD, second, ignore_permissions=True)   # Active

	def test_token_stays_out_of_the_list_view_and_the_form(self, cc):
		meta = frappe.get_meta(saved_cards.SAVED_CARD)
		token = meta.get_field("token")
		self.assertEqual((token.fieldtype, token.hidden, token.in_list_view), ("Password", 1, 0))
		self.assertFalse(any(p.get("write") or p.get("create") or p.get("delete") for p in meta.permissions))
		cust = self._customer()
		card = self._saved_card(cust)
		self.assertNotIn(TOKEN, _json.dumps(frappe.get_doc(saved_cards.SAVED_CARD, card).as_dict(), default=str))
