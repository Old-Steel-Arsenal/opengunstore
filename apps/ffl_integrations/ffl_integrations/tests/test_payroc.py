"""Payroc client + API tests with mocked HTTP.

We mock at the requests-Session level so tests never reach the network and
can assert on the exact URLs / headers / bodies we send. Auth is
`Authorization: Basic <key>` → GET …/account/authenticate → JWT (expiresIn
in HOURS); device card-present sales go through paymentInstructions
(create → poll → follow link → read transactionResult.resultCode).
"""
from __future__ import annotations

import json as _json
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from ffl_core.tests.live_session import live_session as _live_session
from frappe.utils import flt

from ffl_integrations.payroc import client as wn_client
from ffl_integrations.payroc.client import (
    SANDBOX_BASE,
    PayrocClient,
    PayrocConfig,
    PayrocError,
)
from ffl_integrations.payments.api import (
    block_partial_submit,
    charge_pos_invoice,
    poll_pos_charge,
    cancel_pos_charge,
    refund_pos_invoice,
    restore_charge_rows,
    unlock_refund_lock,
    void_pos_invoice,
    _charge_rows,
    _persist_money_fact,
    _refund_across_rows,
)
from ffl_integrations.payroc.adapter import _order_id
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME
from ffl_integrations.tests.site_fixtures import payroc_configured


# These tests stand in for a store whose Payroc is configured (the test site holds no key).
_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
    _PAYROC_CONFIGURED.start()


def tearDownModule():
    _PAYROC_CONFIGURED.stop()


def _mock_response(status_code: int = 200, json_body=None, content_set=True):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = _json.dumps(json_body) if json_body is not None else ""
    resp.content = b"x" if content_set else b""
    resp.json.return_value = json_body or {}
    return resp


def _gv_legacy(orig_ref, guard=None):
    """for_update-aware frappe.db.get_value stand-in for the legacy refund
    mocked tests: the re-refund guard reads its field WITH for_update (a
    locking read, see the api._outcome discussion), the
    amended_from probe must see None, everything else gets the original's
    capture reference. A blanket return_value fed the guard the capture ref
    and every legacy test died on "has already been refunded"."""
    def _f(*a, **k):
        if k.get("for_update"):
            return guard
        if len(a) > 2 and a[2] == "amended_from":
            return None
        return orig_ref
    return _f


def _config(*, sandbox=True) -> PayrocConfig:
    return PayrocConfig(
        api_key="testkey",
        base_url=SANDBOX_BASE,
        default_terminal="4480001",
        default_serial_number="SIM-0001",
        default_currency="USD",
        integration_id=None,
        timeout=30,
    )


class PayrocClientUnit(FrappeTestCase):
    def setUp(self):
        wn_client._bearer.token = ""
        wn_client._bearer.expires_at = 0.0

    def test_authenticate_basic_then_bearer_and_hours(self):
        """Basic key → GET /account/authenticate → JWT cached for ~expiresIn HOURS;
        subsequent calls use Bearer."""
        session = MagicMock()
        session.get.return_value = _mock_response(200, {
            "token": "JWT1", "tokenType": "Bearer", "expiresIn": 1})  # 1 HOUR
        session.request.return_value = _mock_response(202, {
            "paymentInstructionId": "PI1", "status": "IN_PROGRESS", "links": []})
        c = PayrocClient(_config(), session=session)

        c.create_payment_instruction(amount=42.50, order_id="POS-1",
            serial_number="SIM-0001", terminal="4480001")

        auth = session.get.call_args
        self.assertTrue(auth.args[0].endswith("/api/v1/account/authenticate"))
        self.assertEqual(auth.kwargs["headers"]["Authorization"], "Basic testkey")
        # 1 hour ≈ 3600s, comfortably more than a few seconds
        self.assertGreater(wn_client._bearer.expires_at - time.time(), 3000)

        req = session.request.call_args
        self.assertEqual(req.args[0], "POST")
        self.assertTrue(req.args[1].endswith(
            "/api/v1/transaction/devices/SIM-0001/paymentInstructions"))
        self.assertEqual(req.kwargs["headers"]["Authorization"], "Bearer JWT1")
        body = req.kwargs["json"]
        self.assertEqual(body["terminal"], "4480001")
        self.assertEqual(body["order"]["totalAmount"], 42.50)   # decimal, not cents
        self.assertEqual(body["order"]["orderId"], "POS-1")
        self.assertEqual(body["customizationOptions"]["entryMethod"], "DEVICE_READ")
        self.assertTrue(body["autoCapture"])
        self.assertFalse(body["processAsSale"])   # Auth+autocapture; terminal rejects one-step Sale

    def test_integration_id_header_when_set(self):
        cfg = _config()
        cfg.integration_id = "ISV-9"
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T", "expiresIn": 1})
        session.request.return_value = _mock_response(202, {"paymentInstructionId": "PI"})
        PayrocClient(cfg, session=session).create_payment_instruction(
            amount=1.0, order_id="X", serial_number="S", terminal="T")
        self.assertEqual(session.request.call_args.kwargs["headers"]["X-Integration-ID"], "ISV-9")

    def test_poll_and_cancel(self):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T", "expiresIn": 1})
        session.request.return_value = _mock_response(200, {"status": "IN_PROGRESS"})
        c = PayrocClient(_config(), session=session)
        c.poll_payment_instruction("PI1")
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertTrue(session.request.call_args.args[1].endswith(
            "/api/v1/transaction/paymentInstructions/PI1"))
        c.cancel_payment_instruction("PI1")
        self.assertEqual(session.request.call_args.args[0], "DELETE")

    def test_retrieve_payment_follows_absolute_link(self):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T", "expiresIn": 1})
        session.request.return_value = _mock_response(200, {
            "uniqueReference": "ABCD123456",
            "transactionResult": {"resultCode": "A", "approvalCode": "OK",
                "authorizedAmount": 42.50}})
        c = PayrocClient(_config(), session=session)
        href = "https://testpayments.worldnettps.com/merchant/api/v1/transaction/payments/ABCD123456"
        out = c.retrieve_payment(href)
        self.assertEqual(session.request.call_args.args[1], href)   # absolute URL as-is
        self.assertEqual(out["transactionResult"]["resultCode"], "A")

    def test_payment_link_extracts_href(self):
        inst = {"links": [{"rel": "self", "href": "https://x/instructions/PI1"},
                          {"rel": "payment", "method": "GET",
                           "href": "https://x/merchant/api/v1/transaction/payments/REF"}]}
        self.assertEqual(PayrocClient.payment_link(inst),
            "https://x/merchant/api/v1/transaction/payments/REF")
        self.assertIsNone(PayrocClient.payment_link({"links": []}))

    def test_refund_and_reverse_field_names(self):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T", "expiresIn": 1})
        session.request.return_value = _mock_response(200, {"refundReference": "R1"})
        c = PayrocClient(_config(), session=session)
        c.refund("ABCD123456", refund_amount=25.00, refund_reason="returned ammo")
        rc = session.request.call_args
        self.assertEqual(rc.args[0], "POST")
        self.assertTrue(rc.args[1].endswith("/api/v1/transaction/payments/ABCD123456/refunds"))
        self.assertEqual(rc.kwargs["json"], {"refundAmount": 25.00, "refundReason": "returned ammo"})
        c.reverse("ABCD123456")
        vc = session.request.call_args
        self.assertEqual(vc.args[0], "PATCH")
        self.assertTrue(vc.args[1].endswith("/api/v1/transaction/payments/ABCD123456/reverse"))

    def test_refund_rejects_zero(self):
        c = PayrocClient(_config(), session=MagicMock())
        with self.assertRaises(PayrocError):
            c.refund("X", refund_amount=0, refund_reason="r")

    def test_create_requires_serial_and_terminal(self):
        cfg = _config(); cfg.default_serial_number = None
        with self.assertRaises(PayrocError):
            PayrocClient(cfg, session=MagicMock()).create_payment_instruction(
                amount=1, order_id="X", terminal="T")
        cfg2 = _config(); cfg2.default_terminal = None
        with self.assertRaises(PayrocError):
            PayrocClient(cfg2, session=MagicMock()).create_payment_instruction(
                amount=1, order_id="X", serial_number="S")

    def test_bearer_cached_across_calls(self):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T1", "expiresIn": 1})
        session.request.return_value = _mock_response(200, {"status": "IN_PROGRESS"})
        c = PayrocClient(_config(), session=session)
        c.poll_payment_instruction("PI1")
        c.poll_payment_instruction("PI2")
        self.assertEqual(session.get.call_count, 1)        # authenticate once
        self.assertEqual(session.request.call_count, 2)

    def test_retry_adapter_only_retries_get(self):
        # No idempotency header → POST/PATCH must NOT auto-retry (would double-charge).
        s = PayrocClient._build_session()
        allowed = s.get_adapter("https://x").max_retries.allowed_methods
        self.assertEqual(set(allowed), {"GET"})

    def test_payment_link_skips_refund_sub_links(self):
        inst = {"links": [
            {"rel": "refund", "href": "https://x/merchant/api/v1/transaction/payments/REF/refunds"},
            {"rel": "payment", "href": "https://x/merchant/api/v1/transaction/payments/REF"}]}
        self.assertTrue(PayrocClient.payment_link(inst).endswith("/payments/REF"))


class PayrocApiUnit(FrappeTestCase):
    def _inv(self, **o):
        d = {"name": "POS-7", "grand_total": 42.50, "rounded_total": 42.50,
             "card_unique_reference": None, "card_instruction_id": None,
             "card_attempt": 0}
        d.update(o)
        return frappe._dict(d)

    # _append_charge_row is patched by NAME (docs/testing.md stub discipline):
    # the blanket get_doc patch would otherwise hijack frappe.new_doc's
    # internal get_doc call and hand db_insert a fake.
    @patch("ffl_integrations.payments.api._append_charge_row", return_value="ROW1")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api.frappe.db.exists", return_value=True)
    @patch("ffl_integrations.payments.api._check_perm")
    def test_charge_creates_instruction_decimal_amount(self, _p, _e, gd, cc, sv, _ir, _ar):
        gd.return_value = self._inv()
        cc.from_settings.return_value.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI9", "status": "IN_PROGRESS"}
        out = charge_pos_invoice("POS-7")
        self.assertEqual(out["instruction_id"], "PI9")
        kw = cc.from_settings.return_value.create_payment_instruction.call_args.kwargs
        self.assertEqual(kw["amount"], 42.50)        # decimal, NOT 4250

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_poll_completed_approved(self, _p, cc, sv):
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"rel": "payment",
                       "href": "https://x/merchant/api/v1/transaction/payments/REF1234567"}]}
        c.payment_link.return_value = "https://x/merchant/api/v1/transaction/payments/REF1234567"
        c.retrieve_payment.return_value = {"uniqueReference": "REF1234567",
            "transactionResult": {"resultCode": "A", "approvalCode": "OK",
                "authorizedAmount": 42.50}}
        out = poll_pos_charge("POS-7", "PI9", expected_amount=42.50)
        self.assertTrue(out["ok"])
        self.assertEqual(out["unique_reference"], "REF1234567")

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_poll_declined(self, _p, cc, sv):
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/.../payments/R"}]}
        c.payment_link.return_value = "https://x/.../payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "D"}}
        self.assertFalse(poll_pos_charge("POS-7", "PI9", expected_amount=42.50)["ok"])

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_poll_inprogress_passthrough(self, _p, cc, sv):
        cc.from_settings.return_value.poll_payment_instruction.return_value = {
            "status": "IN_PROGRESS"}
        out = poll_pos_charge("POS-7", "PI9", expected_amount=42.50)
        self.assertEqual(out["status"], "IN_PROGRESS")
        self.assertFalse(out["ok"])

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api.frappe.db.exists", return_value=True)
    @patch("ffl_integrations.payments.api._check_perm")
    def test_charge_resumes_inflight_no_double_create(self, _p, _e, gd, cc, sv, _ir):
        gd.return_value = self._inv(card_instruction_id="PI-OLD")
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
        out = charge_pos_invoice("POS-7")
        self.assertEqual(out["instruction_id"], "PI-OLD")
        self.assertEqual(out["status"], "IN_PROGRESS")
        c.create_payment_instruction.assert_not_called()     # no second device charge

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api.frappe.db.exists", return_value=True)
    @patch("ffl_integrations.payments.api._check_perm")
    def test_charge_resume_keeps_an_unknown_state_in_flight(self, _p, _e, gd, cc, sv, _ir):
        """An instruction in a state Payroc does not document may still capture: the
        legacy resume hands it back for polling instead of starting a second charge."""
        gd.return_value = self._inv(card_instruction_id="PI-ODD")
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "SETTLING"}
        out = charge_pos_invoice("POS-7")
        self.assertEqual(out["instruction_id"], "PI-ODD")
        self.assertEqual(out["status"], "IN_PROGRESS")
        c.create_payment_instruction.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api.frappe.db.exists", return_value=True)
    @patch("ffl_integrations.payments.api._check_perm")
    def test_charge_reconciles_completed_prior_no_double_charge(self, _p, _e, gd, cc, sv, _ir):
        gd.return_value = self._inv(card_instruction_id="PI-DONE", grand_total=42.50)
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"rel": "payment", "href": "https://x/payments/REF"}]}
        c.payment_link.return_value = "https://x/payments/REF"
        c.retrieve_payment.return_value = {"uniqueReference": "REF",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 42.50}}
        with self.assertRaises(frappe.ValidationError):
            charge_pos_invoice("POS-7")
        c.create_payment_instruction.assert_not_called()

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_poll_zero_authorized_is_not_full(self, _p, cc, sv):
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 0}}
        out = poll_pos_charge("POS-7", "PI9", expected_amount=42.50)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("partial"))

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_poll_without_expected_is_not_auto_full(self, _p, cc, sv):
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 30.00}}
        out = poll_pos_charge("POS-7", "PI9")    # expected_amount omitted
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("partial"))

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_cancel_reconciles_completed_not_blind_cancel(self, _p, cc, gd, sv):
        gd.return_value = self._inv(payments=[
            frappe._dict(mode_of_payment="Credit Card", amount=42.50)])
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 42.50}}
        out = cancel_pos_charge("POS-7", "PI9")
        self.assertTrue(out.get("reconciled"))
        self.assertEqual(out.get("unique_reference"), "R")
        c.cancel_payment_instruction.assert_not_called()     # didn't strand a capture

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_cancel_split_tender_reconciles_card_portion(self, _p, cc, gd, sv):
        # $100 sale = $40 cash + $60 card. The device charged $60; a cancel
        # racing the approval must reconcile against 60 (full), NOT grand_total.
        gd.return_value = self._inv(grand_total=100.00, payments=[
            frappe._dict(mode_of_payment="Cash", amount=40.00),
            frappe._dict(mode_of_payment="Credit Card", amount=60.00)])
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 60.00}}
        out = cancel_pos_charge("POS-7", "PI9")
        self.assertTrue(out["ok"])                           # full, not partial
        self.assertFalse(out.get("partial"))
        self.assertTrue(out.get("reconciled"))

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_partial_capture_sets_partial_flag(self, _p, cc, sv):
        # A stamped partial capture must be marked payroc_partial=1 so the POS
        # short-circuit never treats it as fully paid after a reload.
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 30.00}}
        out = poll_pos_charge("POS-7", "PI9", expected_amount=42.50)
        self.assertTrue(out.get("partial"))
        stamps = [c_.args[2] for c_ in sv.call_args_list if isinstance(c_.args[2], dict)]
        self.assertTrue(any(s.get("card_status") == "PARTIAL"
            and s.get("card_unique_reference") == "R"
            and s.get("card_charged_amount") == 30.00 for s in stamps))

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_full_capture_clears_partial_flag(self, _p, cc, sv):
        c = cc.from_settings.return_value
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/R"}]}
        c.payment_link.return_value = "https://x/payments/R"
        c.retrieve_payment.return_value = {"uniqueReference": "R",
            "transactionResult": {"resultCode": "A", "authorizedAmount": 42.50}}
        out = poll_pos_charge("POS-7", "PI9", expected_amount=42.50)
        self.assertTrue(out["ok"])
        stamps = [c_.args[2] for c_ in sv.call_args_list if isinstance(c_.args[2], dict)]
        self.assertTrue(any(s.get("card_status") == "CHARGED"
            and s.get("card_unique_reference") == "R"
            and s.get("card_charged_amount") == 42.50 for s in stamps))

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_clears_stamps_for_recharge(self, _p, cc, gv, sv):
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        out = void_pos_invoice("POS-7")
        self.assertTrue(out["ok"])
        # The durable stamp write is the dict-valued set_value; the 4473 hold
        # release that follows it is a separate scalar write (see
        # _clear_payment_hold) and must not be mistaken for the stamps.
        stamped = next(c.args[2] for c in sv.call_args_list if isinstance(c.args[2], dict))
        self.assertIsNone(stamped.get("card_unique_reference"))
        self.assertIsNone(stamped.get("card_instruction_id"))
        self.assertEqual(stamped.get("card_status"), "VOIDED")
        # 0, never None: the Currency column is NOT NULL, and a None here crashed
        # the stamp clear AFTER the gateway reversal succeeded (money reversed,
        # DB still CHARGED — a past incident). The subscript (not
        # .get) also distinguishes a missing key from an explicit 0. The real
        # schema-level guard is VoidPosInvoiceRealWrite below.
        self.assertEqual(stamped["card_charged_amount"], 0)

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_declined_reverse_keeps_stamps(self, _p, cc, gv, sv):
        # HTTP 200 but the reversal is DECLINED in the body — the card is still
        # captured, so the re-charge guard must stay armed (stamps NOT cleared).
        cc.from_settings.return_value.reverse.return_value = {
            "uniqueReference": "REF1",
            "transactionResult": {"resultCode": "D", "resultMessage": "cannot reverse"}}
        with self.assertRaises(frappe.ValidationError):
            void_pos_invoice("POS-7")
        for call in sv.call_args_list:
            if isinstance(call.args[2], dict):
                self.fail("stamps were cleared on a declined reversal")

    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_gateway_error_friendly_throw(self, _p, cc, gv, sv):
        # A gateway HTTP error (already settled / not reversible) surfaces as a
        # clean frappe throw, not a raw traceback — and stamps stay armed.
        cc.from_settings.return_value.reverse.side_effect = PayrocError(
            "Payroc PATCH .../reverse 422: transaction already settled")
        with self.assertRaises(frappe.ValidationError):
            void_pos_invoice("POS-7")
        for call in sv.call_args_list:
            if isinstance(call.args[2], dict):
                self.fail("stamps were cleared after a gateway error")

    def _ret(self, **o):
        d = {"name": "RET-1", "is_return": 1, "return_against": "POS-7",
             "payments": [frappe._dict(mode_of_payment="Credit Card", amount=-25.00)],
             "items": [frappe._dict(item_code="AMMO-9MM")]}
        d.update(o)
        return frappe._dict(d)

    def _orig(self, cc_amount=40.00):
        return frappe._dict(payments=[
            frappe._dict(mode_of_payment="Credit Card", amount=cc_amount)])

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_all", return_value=[])
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=[])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1234567"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_non_firearm(self, _p, gd, _gv, _f, cc, ga, sv, _ir):
        gd.side_effect = [self._ret(), self._orig()]   # get_doc(return), get_doc(original)
        cc.from_settings.return_value.refund.return_value = {"refundReference": "RR1"}
        out = refund_pos_invoice("RET-1")
        self.assertTrue(out["ok"])
        call = cc.from_settings.return_value.refund.call_args
        self.assertEqual(call.args[0], "REF1234567")          # original's card_unique_reference
        self.assertEqual(call.kwargs["refund_amount"], 25.00)  # |−25.00|

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=["GLOCK19"])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_firearm_blocked(self, _p, gd, _gv, _f, cc, _ir):
        gd.return_value = self._ret(items=[frappe._dict(item_code="GLOCK19")])
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice("RET-1")
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_all", return_value=[])
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=[])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_over_original_blocked(self, _p, gd, _gv, _f, cc, ga, sv, _ir):
        # return card row -100 but original only captured 40 → over-refund blocked.
        ret = self._ret(payments=[frappe._dict(mode_of_payment="Credit Card", amount=-100.00)])
        gd.side_effect = [ret, self._orig(40.00)]    # get_doc(return) then get_doc(original)
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice("RET-1")
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1", guard="RR-PRIOR"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_double_refund_blocked(self, _p, gd, _gv, cc, _ir):
        # The return already carries a refund reference → a second server call
        # must throw, not push a second refund (Worldnet has no idempotency header).
        gd.return_value = self._ret(card_refund_reference="RR-PRIOR")
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice("RET-1")
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_all")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    # _charge_rows patched by name: the wrapper's row-era fork would otherwise
    # eat this blanket get_all side_effect queue.
    @patch("ffl_integrations.payments.api._charge_rows", return_value=[])
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=[])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_cumulative_cap_blocked(self, _p, gd, _gv, _f, _cr, cc, ga, sv, _ir):
        # Original captured 40; a prior return already refunded 25 → only 15
        # remains, so a second 25 must be blocked (cumulative cap, not per-call).
        gd.side_effect = [self._ret(), self._orig(40.00)]
        ga.side_effect = [   # orphan-gate sibling scan (clean), cap sibling rows, CC amounts
            [],
            [frappe._dict(name="RET-0", card_refund_reference="RR-DONE")],
            [-25.00]]
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice("RET-1")
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=[])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_blocked_when_orig_card_unreadable(self, _p, gd, _gv, _f, cc, sv, _ir):
        # Original carries a capture ref but its Credit Card rows sum to 0 →
        # the cap can't be computed; fail safe (block) instead of skipping it.
        orig = frappe._dict(payments=[frappe._dict(mode_of_payment="Cash", amount=40.00)])
        gd.side_effect = [self._ret(), orig]
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice("RET-1")
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=False)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_charge_skips_when_unconfigured(self, _p, cc, _ir):
        # Go-live gate: no key / no serial → POS records plain tender, no gateway.
        out = charge_pos_invoice("POS-7")
        self.assertTrue(out["skip"])
        cc.from_settings.assert_not_called()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=False)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_refund_skips_when_unconfigured(self, cc, _ir):
        # Inert integration must not even hit the manager gate — a cashier's
        # card-row return submits normally (manual refund on the terminal).
        out = refund_pos_invoice("RET-1")
        self.assertTrue(out["skip"])
        cc.from_settings.assert_not_called()

    # The manager gate is stubbed like every sibling refund test: with
    # frappe.db.get_value patched to None, the real gate's role lookup only
    # worked while the Role meta happened to be cached by an earlier test.
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", return_value=None)
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    def test_refund_skips_original_not_payroc_charged(self, gd, _gv, cc, sv, _ir, _perm):
        # Original was cash / manual-terminal — nothing to refund via gateway;
        # the return proceeds normally instead of being blocked.
        gd.return_value = self._ret()
        out = refund_pos_invoice("RET-1")
        self.assertTrue(out["skip"])
        cc.from_settings.return_value.refund.assert_not_called()

    def test_block_partial_submit_throws_on_partial(self):
        # Server-side enforcement (before_submit doc_event): a PARTIAL capture
        # must never submit as fully paid — POS, desk form, or direct API.
        from ffl_integrations.payments.api import block_partial_submit
        with self.assertRaises(frappe.ValidationError):
            block_partial_submit(frappe._dict(card_status="PARTIAL"))

    def test_block_partial_submit_passes_other_states(self):
        from ffl_integrations.payments.api import block_partial_submit
        for status in (None, "", "CHARGED", "VOIDED", "REFUNDED"):
            block_partial_submit(frappe._dict(card_status=status))   # no throw


class PayrocSetup(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ensure_mode_of_payment() saves every enabled POS Profile, and
        # ERPNext refuses that save while the "Credit Card" MoP has no
        # default account for the company. On a full-suite run an earlier
        # module used to leave the account behind; standalone runs must
        # seed it themselves.
        from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account
        ensure_credit_card_mop_account()

    def test_ensure_mode_of_payment_idempotent(self):
        from ffl_integrations.payments.setup import ensure_mode_of_payment, MODE_OF_PAYMENT_NAME
        ensure_mode_of_payment()
        self.assertTrue(frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT_NAME))
        ensure_mode_of_payment()
        self.assertTrue(frappe.db.exists("Mode of Payment", MODE_OF_PAYMENT_NAME))

    def test_ensure_mode_of_payment_adds_to_pos_profile(self):
        from ffl_integrations.payments.setup import ensure_mode_of_payment, MODE_OF_PAYMENT_NAME
        # Pick (or create) a disposable POS Profile without "Credit Card".
        profile_name = "_PayrocSetupTest"
        if frappe.db.exists("POS Profile", profile_name):
            frappe.delete_doc("POS Profile", profile_name, force=1, ignore_permissions=True)
        # Prefer a company that already has a POS Profile to borrow the
        # mandatory write-off fields from — "any company" is order-dependent
        # (multi-company dev sites flip on Company.modified) and a chartless
        # pick has no Expense-Account-typed account to fall back to.
        company = frappe.get_value("POS Profile", {"disabled": 0}, "company") \
            or frappe.get_value("Company", {}, "name")
        template = frappe.get_value("POS Profile", {"company": company},
            ["write_off_account", "write_off_cost_center"], as_dict=True) or {}
        profile = frappe.get_doc({
            "doctype": "POS Profile",
            "name": profile_name,
            "company": company,
            "warehouse": frappe.get_value("Warehouse",
                {"is_group": 0, "company": company}, "name"),
            "write_off_account": template.get("write_off_account")
                or frappe.get_value("Account",
                    {"account_type": "Expense Account", "company": company}, "name")
                or frappe.get_value("Account",
                    {"root_type": "Expense", "is_group": 0, "company": company}, "name"),
            "write_off_cost_center": template.get("write_off_cost_center")
                or frappe.get_value("Cost Center",
                    {"company": company, "is_group": 0}, "name"),
            "disabled": 0,
            "payments": [{"mode_of_payment": "Cash", "default": 1}],
        })
        profile.flags.ignore_permissions = True
        profile.insert()
        try:
            ensure_mode_of_payment()
            profile.reload()
            modes = {row.mode_of_payment for row in profile.payments}
            self.assertIn(MODE_OF_PAYMENT_NAME, modes)
        finally:
            frappe.delete_doc("POS Profile", profile_name, force=1, ignore_permissions=True)


_RW_ITEM = "_Test Payroc Void Item"
_RW_CUSTOMER = "_Test Payroc Void Buyer"


def _ensure_pos_fixtures() -> str:
    """Company / POS profile / customer / item / opening entry for REAL-write
    POS Invoice tests — shared by the void regression and the multi-card
    lifecycle suite. The item is non-stock and non-firearm: these paths never
    touch stock, and a stock row would leave ledger residue that survives the
    rollback (docs/testing.md)."""
    from ffl_core.setup import dev_site
    from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account
    dev_site.run()
    # dev_site only seeds Cash — ERPNext refuses to save any POS document
    # whose MoP has no default account (this helper exists for exactly
    # this and is idempotent under any suite ordering).
    ensure_credit_card_mop_account()
    company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
    frappe.defaults.set_user_default("Company", company)
    if not frappe.db.exists("Customer", _RW_CUSTOMER):
        frappe.get_doc({
            "doctype": "Customer", "customer_name": _RW_CUSTOMER,
            "customer_group": "Individual", "territory": "All Territories",
        }).insert(ignore_permissions=True)
    if not frappe.db.exists("Item", _RW_ITEM):
        frappe.get_doc({
            "doctype": "Item", "item_code": _RW_ITEM, "item_name": _RW_ITEM,
            "item_group": "Ammunition", "stock_uom": "Nos",
            "is_stock_item": 0,
        }).insert(ignore_permissions=True)
    from ffl_core.tests.pos_opening import todays_pos_opening
    todays_pos_opening(company)
    return company


def _pos_draft_invoice(rate: float = 50, card_amount: float | None = None,
        cash_amount: float = 0, qty: float = 1) -> str:
    """Draft POS Invoice with pinned tender rows — the multi-card math keys
    off the Credit Card tender, so tests control it exactly (plus an optional
    Cash row for mixed-tender cases)."""
    inv = frappe.new_doc("POS Invoice")
    inv.pos_profile = "FFL Test"
    inv.customer = _RW_CUSTOMER
    inv.append("items", {
        "item_code": _RW_ITEM, "qty": qty, "rate": rate,
        "price_list_rate": rate,
    })
    inv.set_missing_values()
    inv.set("payments", [])
    inv.append("payments", {
        "mode_of_payment": MODE_OF_PAYMENT_NAME,
        "amount": rate * qty if card_amount is None else card_amount,
    })
    if cash_amount:
        inv.append("payments", {"mode_of_payment": "Cash", "amount": cash_amount})
    inv.flags.ignore_permissions = True
    inv.save()
    return inv.name


def _cc_return_against(original: str, cc_amount: float, qty: float | None = None) -> str:
    """Draft Credit-Card return via make_return_doc — the ONE place that owns
    the trap knowledge: make_return_doc's paid_amount is NOT recomputed from
    edited payments (full-qty returns mask it because stale paid == grand);
    every builder must restate paid or partial-qty returns die in
    validate_pos."""
    from erpnext.controllers.sales_and_purchase_return import make_return_doc
    ret = make_return_doc("POS Invoice", original)
    if qty is not None:
        ret.items[0].qty = qty
    ret.set("payments", [])
    ret.append("payments", {
        "mode_of_payment": MODE_OF_PAYMENT_NAME, "amount": cc_amount})
    ret.paid_amount = cc_amount
    ret.base_paid_amount = cc_amount
    ret.flags.ignore_permissions = True
    ret.save()
    return ret.name


def _marked_return_draft(reference=None, booked: float = 0) -> str:
    """Bare draft flagged as a return with money-fact fields written the way
    the server writes them (db.set_value) — the shared fixture for guard/
    unlock/trash tests."""
    name = _pos_draft_invoice(rate=10)
    stamps = {"is_return": 1}
    if reference is not None:
        stamps["card_refund_reference"] = reference
    if booked:
        stamps["card_refunded_amount"] = booked
    frappe.db.set_value("POS Invoice", name, stamps, update_modified=False)
    return name


class VoidPosInvoiceRealWrite(FrappeTestCase):
    """Regression for a past incident — REAL DB writes only.

    Every other void test in this module mocks frappe.db.set_value, which is
    exactly how the original bug shipped: the cleanup dict carried a None for
    card_charged_amount (a Currency column — NOT NULL DEFAULT 0 by Frappe
    convention) and no mock ever objected, while the gateway reversal had
    already gone through. Here the stamp UPDATE runs against the real schema,
    so a None in any numeric stamp field is an IntegrityError and this test
    goes red. Same recipe as
    ffl_core.tests.test_consignment_portal.MarkSoldWithoutASalePrice.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def _stamped_draft_invoice(self) -> str:
        name = _pos_draft_invoice(rate=50)
        frappe.db.set_value("POS Invoice", name, {
            "card_unique_reference": "REF-REAL",
            "card_instruction_id": "PI-REAL",
            "card_status": "CHARGED",
            "card_charged_amount": 50.0,
            "ffl_payment_held": 1,          # parked for the 4473 (charge-before-4473)
        }, update_modified=False)
        return name

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_stamp_clear_survives_the_real_schema(self, _p, cc):
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        name = self._stamped_draft_invoice()

        out = void_pos_invoice(name)  # the stamp UPDATE hits the real table

        self.assertTrue(out["ok"])
        row = frappe.db.get_value("POS Invoice", name,
            ["card_status", "card_charged_amount",
             "card_unique_reference", "card_instruction_id",
             "ffl_payment_held"], as_dict=True)
        self.assertEqual(row.card_status, "VOIDED")
        self.assertEqual(float(row.card_charged_amount), 0.0)
        self.assertFalse(row.card_unique_reference)
        self.assertFalse(row.card_instruction_id)
        # Legacy void path: the 4473 hold is released with the money.
        self.assertEqual(int(row.ffl_payment_held), 0)

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_survives_an_unmigrated_hold_column(self, _p, cc):
        # A site whose ffl_core fixture has not landed yet: the gateway reversal
        # must still stamp durably; the hold flag is UI state and is skipped.
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        name = self._stamped_draft_invoice()
        with patch("ffl_integrations.payments.api.frappe.db.has_column", return_value=False):
            out = void_pos_invoice(name)
        self.assertTrue(out["ok"])
        row = frappe.db.get_value("POS Invoice", name,
            ["card_status", "ffl_payment_held"], as_dict=True)
        self.assertEqual(row.card_status, "VOIDED")
        self.assertEqual(int(row.ffl_payment_held), 1)   # untouched, not crashed


def _approved_payment(ref: str, amount: float, card: str = "Visa",
        pan: str = "****1111") -> dict:
    return {"uniqueReference": ref,
            "customerAccount": {"cardType": card, "maskedPan": pan},
            "transactionResult": {"resultCode": "A", "authorizedAmount": amount,
                "approvalCode": f"AP-{ref}"}}


class MultiCardLifecycle(FrappeTestCase):
    """Multi-card charge lifecycle against the REAL schema (child rows,
    aggregate stamps, submit guard, per-card void, reverse-order refund).
    Only PayrocClient and the permission checks are mocked — every row insert
    and stamp UPDATE hits the real tables, per the VoidPosInvoiceRealWrite
    lesson."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    # ------------------------------------------------------------- helpers

    def _charge_and_settle(self, cc, inv: str, *, ask, ref, authorized=None,
            pi=None, card="Visa", pan="****1111"):
        """One full charge cycle: create instruction → poll COMPLETED →
        row settled. Returns the poll result."""
        c = cc.from_settings.return_value
        pi = pi or f"PI-{ref}"
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": pi, "status": "IN_PROGRESS"}
        out = charge_pos_invoice(inv, amount=ask)
        self.assertEqual(out["instruction_id"], pi)
        c.poll_payment_instruction.return_value = {
            "status": "COMPLETED", "links": [{"href": f"https://x/payments/{ref}"}]}
        c.payment_link.return_value = f"https://x/payments/{ref}"
        c.retrieve_payment.return_value = _approved_payment(
            ref, authorized if authorized is not None else ask, card=card, pan=pan)
        return poll_pos_charge(inv, pi)

    def _stamps(self, inv: str):
        return frappe.db.get_value("POS Invoice", inv,
            ["card_status", "card_charged_amount", "card_unique_reference",
             "card_instruction_id", "card_attempt"], as_dict=True)

    # --------------------------------------------------------------- tests

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_two_card_split_charges_to_full(self, _p, cc, _r):
        inv = _pos_draft_invoice(rate=100)

        first = self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        self.assertTrue(first["ok"])
        self.assertEqual(flt(first["remaining"], 2), 40)
        s = self._stamps(inv)
        self.assertEqual(s.card_status, "PARTIAL")       # Σ 60 < tender 100
        self.assertEqual(flt(s.card_charged_amount), 60)
        self.assertEqual(s.card_unique_reference, "REF-A")

        second = self._charge_and_settle(cc, inv, ask=None, ref="REF-B",
            authorized=40, card="Amex", pan="****2222")
        self.assertTrue(second["ok"])
        self.assertEqual(flt(second["remaining"], 2), 0)
        s = self._stamps(inv)
        self.assertEqual(s.card_status, "CHARGED")
        self.assertEqual(flt(s.card_charged_amount), 100)
        self.assertEqual(s.card_unique_reference, "MULTI")
        self.assertEqual(s.card_attempt, 2)

        rows = _charge_rows(inv)
        self.assertEqual([(r.status, flt(r.amount)) for r in rows],
            [("CHARGED", 60), ("CHARGED", 40)])
        self.assertIn("2222", rows[1].card_summary)

        # Fully collected → a third slice must refuse.
        with self.assertRaises(frappe.ValidationError):
            charge_pos_invoice(inv, amount=1)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_each_card_gets_its_own_order_id(self, _p, cc, _r):
        """Payroc ties an order to its first approved sale: a second instruction
        on the same orderId comes back "Order has already been processed" and the
        card is never read (a past incident, where card 2 of a split
        could not be sent at all). Card 1 keeps the bare invoice name — the
        single-card receipt still matches the invoice in the portal."""
        inv = _pos_draft_invoice(rate=100)
        c = cc.from_settings.return_value

        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        self.assertEqual(c.create_payment_instruction.call_args.kwargs["order_id"], inv)

        self._charge_and_settle(cc, inv, ask=None, ref="REF-B", authorized=40)
        self.assertEqual(
            c.create_payment_instruction.call_args.kwargs["order_id"], f"{inv}-A2")

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_slice_over_remaining_refuses(self, _p, cc, _r):
        inv = _pos_draft_invoice(rate=100)
        with self.assertRaises(frappe.ValidationError):
            charge_pos_invoice(inv, amount=120)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_partial_approval_is_real_money_not_a_dead_end(self, _p, cc, _r):
        """Ask 100, card only approves 30 → the row is CHARGED at 30 and the
        sale simply has 70 left to collect on another card (the single-charge
        era would have dead-ended this as PARTIAL)."""
        inv = _pos_draft_invoice(rate=100)
        out = self._charge_and_settle(cc, inv, ask=100, ref="REF-GIFT", authorized=30)
        self.assertTrue(out["ok"])
        self.assertEqual(flt(out["remaining"], 2), 70)
        s = self._stamps(inv)
        self.assertEqual(s.card_status, "PARTIAL")
        self.assertEqual(flt(s.card_charged_amount), 30)
        rows = _charge_rows(inv)
        self.assertEqual((rows[0].status, flt(rows[0].amount)), ("CHARGED", 30))

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_submit_guard_mismatch_inflight_and_override(self, _p, cc, _r):
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")

        # Σ 60 ≠ tender 100 → the server-side gate the incident exposed.
        doc = frappe.get_doc("POS Invoice", inv)
        with self.assertRaises(frappe.ValidationError):
            block_partial_submit(doc)

        # Manager override: checkbox without a reason refuses; with one, passes.
        doc.card_manual_settlement = 1
        doc.card_manual_settlement_reason = ""
        with self.assertRaises(frappe.ValidationError):
            block_partial_submit(doc)
        doc.card_manual_settlement_reason = "rest collected on Virtual Terminal, batch #12"
        block_partial_submit(doc)   # no throw

        # Fully collected → passes without any override.
        doc.card_manual_settlement = 0
        self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        block_partial_submit(frappe.get_doc("POS Invoice", inv))   # no throw

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_submit_guard_blocks_inflight_instruction(self, _p, cc, _r):
        inv = _pos_draft_invoice(rate=100)
        cc.from_settings.return_value.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-LIVE", "status": "IN_PROGRESS"}
        charge_pos_invoice(inv, amount=100)
        with self.assertRaises(frappe.ValidationError):
            block_partial_submit(frappe.get_doc("POS Invoice", inv))

    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_void_per_card(self, _p, cc, _r, _rp):
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        # Fully tendered and parked for the 4473 (charge-before-4473 flow).
        frappe.db.set_value("POS Invoice", inv, "ffl_payment_held", 1, update_modified=False)

        # Two live charges: an unqualified void must refuse and name them.
        with self.assertRaises(frappe.ValidationError):
            void_pos_invoice(inv, reason="nics denied")

        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        out = void_pos_invoice(inv, reason="nics denied", unique_reference="REF-B")
        self.assertEqual(out["unique_reference"], "REF-B")
        cc.from_settings.return_value.reverse.assert_called_once()
        self.assertEqual(
            cc.from_settings.return_value.reverse.call_args.args[0], "REF-B")

        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, r.status) for r in rows],
            [("REF-A", "CHARGED"), ("REF-B", "VOIDED")])
        s = self._stamps(inv)
        self.assertEqual(s.card_status, "PARTIAL")       # 60 live of 100
        self.assertEqual(flt(s.card_charged_amount), 60)
        self.assertEqual(s.card_unique_reference, "REF-A")
        # Row void path: 40 is owed again, so the hold is released and the
        # payment screen offers Take Payment for the balance.
        self.assertEqual(int(frappe.db.get_value("POS Invoice", inv, "ffl_payment_held")), 0)

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_refund_allocates_newest_charge_first(self, cc):
        """Reverse-order refund: a 70 refund against [60 REF-A, 40 REF-B] takes all of
        REF-B (newest) then 30 of REF-A, stamping per-row refunded_amount."""
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
            self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        ret_shell = _pos_draft_invoice(rate=1)

        c = cc.from_settings.return_value
        c.refund.side_effect = [{"refundReference": "RF-1"}, {"refundReference": "RF-2"}]
        out = _refund_across_rows(frappe._dict(name=ret_shell), inv,
            [r for r in _charge_rows(inv) if r.status == "CHARGED"], 70.0, None)

        self.assertEqual(out["refund_references"], ["RF-1", "RF-2"])
        calls = c.refund.call_args_list
        self.assertEqual([(k.args[0], k.kwargs["refund_amount"]) for k in calls],
            [("REF-B", 40.0), ("REF-A", 30.0)])
        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, flt(r.refunded_amount)) for r in rows],
            [("REF-A", 30), ("REF-B", 40)])
        st = frappe.db.get_value("POS Invoice", ret_shell,
            ["card_refund_reference", "card_status"], as_dict=True)
        self.assertEqual(st.card_refund_reference, "MULTI")
        self.assertEqual(st.card_status, "REFUNDED")

        # The per-row counters ARE the cumulative cap: only 30 is left.
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 40.0, None)

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_refund_decline_mid_allocation_locks_the_return(self, cc):
        """A decline after a successful slice must stamp what went through and
        arm the re-refund guard — a wholesale retry would double-refund."""
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
            self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        ret_shell = _pos_draft_invoice(rate=1)

        c = cc.from_settings.return_value
        c.refund.side_effect = [
            {"refundReference": "RF-1"},
            {"uniqueReference": "REF-A", "transactionResult": {
                "resultCode": "D", "resultMessage": "cannot refund"}},
        ]
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 70.0, None)

        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, flt(r.refunded_amount)) for r in rows],
            [("REF-A", 0), ("REF-B", 40)])     # the successful slice IS booked
        locked = frappe.db.get_value("POS Invoice", ret_shell, "card_refund_reference")
        self.assertTrue((locked or "").startswith("PARTIAL:"))


class MultiCardR1Closures(FrappeTestCase):
    """Closures for audit findings: the branches the first round proved were unwatched —
    cancel's row path (incl. the raced approval), the submit guard reading DB
    truth instead of the client payload, the manager gate on the override, the
    client-save row-wipe protection, and the legacy default-amount change."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle
    _stamps = MultiCardLifecycle._stamps

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_cancel_row_path_and_raced_approval(self, _p, cc, _r):
        inv = _pos_draft_invoice(rate=100)
        c = cc.from_settings.return_value

        # Plain cancel: instruction still in flight → row CANCELED, stamps reset.
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-C1", "status": "IN_PROGRESS"}
        charge_pos_invoice(inv, amount=100)
        c.poll_payment_instruction.return_value = {"status": "IN_PROGRESS"}
        c.cancel_payment_instruction.return_value = {"status": "CANCELED"}
        out = cancel_pos_charge(inv, "PI-C1")
        self.assertEqual(out["status"], "CANCELED")
        rows = _charge_rows(inv)
        self.assertEqual(rows[0].status, "CANCELED")
        self.assertEqual(self._stamps(inv).card_status, "CANCELED")
        self.assertFalse(self._stamps(inv).card_instruction_id)

        # Raced approval: cancel arrives after the card captured → reconcile
        # into the row, never strand the money.
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-C2", "status": "IN_PROGRESS"}
        charge_pos_invoice(inv, amount=100)
        c.poll_payment_instruction.return_value = {
            "status": "COMPLETED", "links": [{"href": "https://x/payments/REF-R"}]}
        c.payment_link.return_value = "https://x/payments/REF-R"
        c.retrieve_payment.return_value = _approved_payment("REF-R", 100)
        out = cancel_pos_charge(inv, "PI-C2")
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("reconciled"))
        s = self._stamps(inv)
        self.assertEqual(s.card_status, "CHARGED")
        self.assertEqual(flt(s.card_charged_amount), 100)

        # Row-era invoice + unknown instruction id must never fall into the
        # legacy resolver.
        with self.assertRaises(frappe.ValidationError):
            cancel_pos_charge(inv, "PI-UNKNOWN")

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_submit_guard_reads_db_not_payload(self, _p, cc, _r):
        """A stale/hand-built payload with no (or doctored) child rows
        must not disarm the mismatch gate — the guard reads _charge_rows."""
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        doc = frappe.get_doc("POS Invoice", inv)
        doc.set("card_charges", [])          # what a stale client would send
        with self.assertRaises(frappe.ValidationError):
            block_partial_submit(doc)

    def test_manual_settlement_requires_manager(self):
        """The override is a MANAGER path — the guard must invoke the
        role check, not trust the checkbox."""
        inv = _pos_draft_invoice(rate=100)
        doc = frappe.get_doc("POS Invoice", inv)
        doc.card_manual_settlement = 1
        doc.card_manual_settlement_reason = "VT batch #9"
        with patch("ffl_integrations.payments.api._check_manager",
                side_effect=frappe.PermissionError) as cm:
            with self.assertRaises(frappe.PermissionError):
                block_partial_submit(doc)
            cm.assert_called_once()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_client_save_cannot_wipe_or_mutate_rows(self, _p, cc, _r):
        """A client save with missing/mutated child rows — Frappe's
        child sync would delete/overwrite; the validate hook restores DB
        truth."""
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        doc = frappe.get_doc("POS Invoice", inv)
        doc.set("card_charges", [])          # stale page: rows not loaded
        doc.flags.ignore_permissions = True
        doc.save()
        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, r.status, flt(r.amount)) for r in rows],
            [("REF-A", "CHARGED", 60)])

        # Mutation attempt: client resurrects a VOIDED row as CHARGED.
        with patch("ffl_integrations.payments.api._check_refund_perm"):
            cc.from_settings.return_value.reverse.return_value = {"ok": True}
            void_pos_invoice(inv, reason="test", unique_reference="REF-A")
        doc = frappe.get_doc("POS Invoice", inv)
        doc.card_charges[0].status = "CHARGED"
        doc.flags.ignore_permissions = True
        doc.save()
        self.assertEqual(_charge_rows(inv)[0].status, "VOIDED")

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_mixed_tender_default_amount_is_card_portion(self, _p, cc, _r):
        """Declared behavior change: with no explicit amount the
        charge asks the terminal for the CARD tender portion, not grand_total
        — a cash+card split must not over-charge the card."""
        inv = _pos_draft_invoice(rate=100, card_amount=60, cash_amount=40)
        c = cc.from_settings.return_value
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-MIX", "status": "IN_PROGRESS"}
        out = charge_pos_invoice(inv)
        self.assertEqual(flt(out["amount"], 2), 60)
        self.assertEqual(
            flt(c.create_payment_instruction.call_args.kwargs["amount"], 2), 60)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_refund_wrapper_end_to_end_on_rows(self, _p, _rp, cc, _r):
        """The whole row path through refund_pos_invoice's wrapper —
        real submit (exercising the before_submit hook chain), a real return
        doc, reverse-order allocation, and the armed re-refund guard."""
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        orig = frappe.get_doc("POS Invoice", inv)
        orig.flags.ignore_permissions = True
        orig.submit()   # Σ rows == card tender → the mismatch gate passes

        from erpnext.controllers.sales_and_purchase_return import make_return_doc
        ret = make_return_doc("POS Invoice", inv)
        ret.set("payments", [])
        ret.append("payments", {
            "mode_of_payment": MODE_OF_PAYMENT_NAME, "amount": -70})
        ret.flags.ignore_permissions = True
        ret.save()

        c = cc.from_settings.return_value
        c.refund.side_effect = [
            {"refundReference": "RF-1"}, {"refundReference": "RF-2"}]
        out = refund_pos_invoice(ret.name)
        self.assertTrue(out["ok"])
        self.assertEqual(
            [(k.args[0], k.kwargs["refund_amount"]) for k in c.refund.call_args_list],
            [("REF-B", 40.0), ("REF-A", 30.0)])
        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, flt(r.refunded_amount)) for r in rows],
            [("REF-A", 30), ("REF-B", 40)])
        # The re-refund guard is armed on the return.
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice(ret.name)

    @patch("ffl_integrations.payments.api._commit_money_point")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_refund_commit_points_and_unknown_outcome_lock(self, _p, cc, _r, cp):
        """Every successful slice hits the durable commit point
        AFTER its row ledger write, and a non-PayrocError network failure
        (outcome unknown at the gateway) arms the lock even mid-sequence."""
        from requests.exceptions import Timeout
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=60, ref="REF-A")
        self._charge_and_settle(cc, inv, ask=40, ref="REF-B")
        ret_shell = _pos_draft_invoice(rate=1)

        c = cc.from_settings.return_value
        c.refund.side_effect = [{"refundReference": "RF-1"}, Timeout("gateway")]
        cp.reset_mock()
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 70.0, None)

        # One commit for the successful REF-B slice, one for the lock —
        # both BEFORE the throw could roll anything back.
        self.assertEqual(cp.call_count, 2)
        rows = _charge_rows(inv)
        self.assertEqual([(r.unique_reference, flt(r.refunded_amount)) for r in rows],
            [("REF-A", 0), ("REF-B", 40)])
        locked = frappe.db.get_value("POS Invoice", ret_shell, "card_refund_reference")
        self.assertTrue((locked or "").startswith("PARTIAL:"))

    @patch("ffl_integrations.payments.api._commit_money_point")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_first_slice_network_error_still_arms_the_lock(self, cc, cp):
        """An outcome-unknown failure on the FIRST slice (nothing booked
        yet) must still lock the return — the timeout may have landed."""
        from requests.exceptions import ConnectionError as ReqConnectionError
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        ret_shell = _pos_draft_invoice(rate=1)
        cc.from_settings.return_value.refund.side_effect = ReqConnectionError("net")
        cp.reset_mock()
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertEqual(cp.call_count, 1)   # the lock's durable point
        self.assertEqual(
            frappe.db.get_value("POS Invoice", ret_shell, "card_refund_reference"),
            "UNKNOWN-OUTCOME")
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 0)

    @patch("ffl_integrations.payments.api._commit_money_point")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_first_slice_gateway_reject_stays_retryable(self, cc, cp):
        """A PayrocError is a DEFINITE non-refund — with zero
        slices booked the return must stay retryable (no UNKNOWN-OUTCOME
        lock), or a credential expiry would mass-lock every return."""
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        ret_shell = _pos_draft_invoice(rate=1)
        cc.from_settings.return_value.refund.side_effect = PayrocError("401 auth")
        cp.reset_mock()
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertFalse(frappe.db.get_value(
            "POS Invoice", ret_shell, "card_refund_reference"))
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 0)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_client_save_cannot_tamper_gateway_handles(self, _p, cc, _r):
        """A payload that matches on the coarse shape but swaps a
        server-owned field (the void/refund gateway handle) must be restored
        — the comparison only decides logging, never whether DB truth wins."""
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        doc = frappe.get_doc("POS Invoice", inv)
        doc.card_charges[0].unique_reference = "EVIL-REF"
        doc.flags.ignore_permissions = True
        doc.save()
        self.assertEqual(_charge_rows(inv)[0].unique_reference, "REF-A")


class PayrocErrorClassification(FrappeTestCase):
    """PayrocError.outcome_known must sort failures into
    definitely-not-processed (retry-safe) vs outcome-unknown (must lock)."""

    def setUp(self):
        wn_client._bearer.token = ""
        wn_client._bearer.expires_at = 0.0

    def _client_with(self, resp):
        session = MagicMock()
        session.get.return_value = _mock_response(200, {"token": "T", "expiresIn": 1})
        session.request.return_value = resp
        return PayrocClient(_config(), session=session)

    def test_4xx_is_a_definite_failure(self):
        c = self._client_with(_mock_response(403, {"error": "forbidden"}))
        with self.assertRaises(PayrocError) as ctx:
            c.refund("REF", refund_amount=5, refund_reason="r")
        self.assertTrue(ctx.exception.outcome_known)

    def test_5xx_is_outcome_unknown(self):
        # The request may have reached the processor before the 5xx.
        c = self._client_with(_mock_response(502, {"error": "bad gateway"}))
        with self.assertRaises(PayrocError) as ctx:
            c.refund("REF", refund_amount=5, refund_reason="r")
        self.assertFalse(ctx.exception.outcome_known)

    def test_non_json_2xx_is_outcome_unknown(self):
        # The exact cell: a 2xx whose body we can't parse — the
        # processor most likely DID act.
        resp = _mock_response(200)
        resp.json.side_effect = ValueError("no json")
        resp.text = "<html>proxy interstitial</html>"
        c = self._client_with(resp)
        with self.assertRaises(PayrocError) as ctx:
            c.refund("REF", refund_amount=5, refund_reason="r")
        self.assertFalse(ctx.exception.outcome_known)

    def test_auth_failure_is_definite_even_on_5xx(self):
        # A failed authenticate means the refund POST was never sent — a
        # definite non-refund regardless of the auth endpoint's status code.
        session = MagicMock()
        session.get.return_value = _mock_response(500, {"error": "down"})
        c = PayrocClient(_config(), session=session)
        with self.assertRaises(PayrocError) as ctx:
            c.refund("REF", refund_amount=5, refund_reason="r")
        self.assertTrue(ctx.exception.outcome_known)

    def test_default_and_config_error_are_definite(self):
        from ffl_integrations.payroc.client import PayrocConfigError
        self.assertTrue(PayrocError("x").outcome_known)
        self.assertTrue(PayrocConfigError("no key").outcome_known)


class RefundUnknownPayrocErrorLock(FrappeTestCase):
    """End-to-end: an outcome-unknown PayrocError in the refund
    loop must arm the lock exactly like a network timeout — before this fix it
    fell into the definite-failure branch and left the return retryable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    @patch("ffl_integrations.payments.api._commit_money_point")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_first_slice_unknown_payroc_error_arms_the_lock(self, cc, cp):
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        ret_shell = _pos_draft_invoice(rate=1)
        cc.from_settings.return_value.refund.side_effect = PayrocError(
            "Payroc POST payments/REF-A/refunds 502: upstream", outcome_known=False)
        cp.reset_mock()
        with self.assertRaises(frappe.ValidationError):
            _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertEqual(cp.call_count, 1)   # the lock's durable point
        self.assertEqual(
            frappe.db.get_value("POS Invoice", ret_shell, "card_refund_reference"),
            "UNKNOWN-OUTCOME")
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 0)


class PersistMoneyFact(FrappeTestCase):
    """Unit: the durable money-fact writer — retry once under a
    savepoint, then durable trace + a DO-NOT-RETRY stop."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    @patch("ffl_integrations.payments.api._commit_money_point")
    def test_success_writes_once_and_commits_once(self, cp):
        calls = []
        _persist_money_fact(lambda: calls.append(1),
            action="t-ok", invoice="INV-T", facts={"a": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(cp.call_count, 1)

    @patch("ffl_integrations.payments.api._commit_money_point")
    def test_transient_failure_retries_under_savepoint(self, cp):
        inv = _pos_draft_invoice(rate=10)
        attempts = []

        def write():
            # A real write BEFORE the failure: the savepoint rollback must
            # undo it (clean slate for the retry) WITHOUT rolling back the
            # uncommitted test fixtures created before the helper ran.
            frappe.db.set_value("POS Invoice", inv, "card_status",
                "VOIDED", update_modified=False)
            attempts.append(1)
            if len(attempts) == 1:
                raise Exception("transient lock wait")

        _persist_money_fact(write, action="t-retry", invoice=inv, facts={})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(cp.call_count, 1)
        self.assertEqual(
            frappe.db.get_value("POS Invoice", inv, "card_status"), "VOIDED")
        self.assertTrue(frappe.db.exists("POS Invoice", inv))   # fixtures survived

    @patch("ffl_integrations.payments.api.notify_admins")
    @patch("ffl_integrations.payments.api._commit_money_point")
    def test_double_failure_leaves_trace_and_forbids_retry(self, cp, na):
        with self.assertRaises(frappe.ValidationError) as ctx:
            _persist_money_fact(lambda: (_ for _ in ()).throw(Exception("db down")),
                action="t-dead", invoice="INV-DEAD", facts={"ref": "REF-9"})
        self.assertIn("Do not retry", str(ctx.exception))
        self.assertEqual(cp.call_count, 0)   # the money point was never reached
        logs = frappe.get_all("Error Log",
            filters={"method": ["like", "%INV-DEAD%"]}, fields=["error"])
        self.assertTrue(logs)
        # The case file must carry the facts, not just exist.
        self.assertTrue(any("REF-9" in (l.error or "") for l in logs))
        # And somebody gets paged — spied per repo convention (real
        # notify_admins would leave a non-transactional Redis throttle key).
        self.assertEqual(na.call_count, 1)
        self.assertEqual(na.call_args.kwargs["dedup_key"],
            "card-money-fact:INV-DEAD")


class VoidPersistWindow(FrappeTestCase):
    """On the void path: a DB failure after a successful gateway reverse
    retries once (never re-hitting the gateway), then hard-stops with a
    durable trace instead of silently rolling back the VOIDED flip
    (a past incident's structural fix)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    def _charged_invoice(self, cc):
        inv = _pos_draft_invoice(rate=100)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        return inv

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_retries_transient_stamp_failure(self, _rp, cc):
        from ffl_integrations.payments import api as payroc_api
        inv = self._charged_invoice(cc)
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        real_refresh = payroc_api._refresh_charge_stamps
        boom = [True]

        def flaky(name):
            if boom:
                boom.pop()
                raise Exception("transient")
            return real_refresh(name)

        with patch("ffl_integrations.payments.api._refresh_charge_stamps",
                side_effect=flaky):
            out = void_pos_invoice(inv, reason="t")
        self.assertTrue(out["ok"])
        cc.from_settings.return_value.reverse.assert_called_once()   # gateway hit ONCE
        self.assertEqual(_charge_rows(inv)[0].status, "VOIDED")

    @patch("ffl_integrations.payments.api.notify_admins")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_double_failure_stops_and_traces(self, _rp, cc, na):
        inv = self._charged_invoice(cc)
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        with patch("ffl_integrations.payments.api._refresh_charge_stamps",
                side_effect=Exception("db down")):
            with self.assertRaises(frappe.ValidationError) as ctx:
                void_pos_invoice(inv, reason="t")
        self.assertEqual(na.call_count, 1)
        self.assertEqual(na.call_args.kwargs["dedup_key"], f"card-money-fact:{inv}")
        self.assertIn("Do not retry", str(ctx.exception))
        cc.from_settings.return_value.reverse.assert_called_once()
        # The row flip rolled back to the savepoint — DB truth still says
        # CHARGED; the Error Log is the durable record of the reversal and
        # must carry the gateway facts (the ref), not just exist.
        self.assertEqual(_charge_rows(inv)[0].status, "CHARGED")
        logs = frappe.get_all("Error Log",
            filters={"method": ["like", f"%{inv}%"]}, fields=["error"])
        self.assertTrue(logs)
        self.assertTrue(any("REF-A" in (l.error or "") for l in logs))


class RefundUnlock(FrappeTestCase):
    """The manager unlock — the product-side exit from a refund lock
    (before this, PARTIAL:*/UNKNOWN-OUTCOME returns died in bench console)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    def _locked_return(self, lock="UNKNOWN-OUTCOME"):
        # A bare draft flagged as a return: the endpoint gates on is_return
        # and a direct field write is exactly how a lock looks to
        # the endpoint — the real arming path is covered by the end-to-end
        # test below.
        ret = _pos_draft_invoice(rate=10)
        frappe.db.set_value("POS Invoice", ret,
            {"card_refund_reference": lock, "is_return": 1},
            update_modified=False)
        return ret

    def _comments(self, name):
        return frappe.get_all("Comment", filters={
            "reference_doctype": "POS Invoice", "reference_name": name,
            "comment_type": "Comment"}, pluck="content")

    @patch("ffl_integrations.payments.api._commit_money_point")
    def test_unlock_clears_and_audits(self, cp):
        ret = self._locked_return()
        out = unlock_refund_lock(ret, reason="Payroc portal shows no refund landed")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cleared"], "UNKNOWN-OUTCOME")
        self.assertEqual(cp.call_count, 1)   # the clear is a durable point
        self.assertFalse(
            frappe.db.get_value("POS Invoice", ret, "card_refund_reference"))
        comments = self._comments(ret)
        self.assertTrue(any("UNKNOWN-OUTCOME" in c for c in comments))
        self.assertTrue(any("no refund landed" in c for c in comments))

    @patch("ffl_integrations.payments.api._commit_money_point")
    def test_unlock_refuses_partial_lock(self, cp):
        """PARTIAL:* is the ONE record of what this return already pushed
        back — unlocking it re-arms a full-amount retry the per-original-card
        caps cannot bound. It must never unlock."""
        ret = self._locked_return(lock="PARTIAL:RF-1,RF-2")
        with self.assertRaises(frappe.ValidationError) as ctx:
            unlock_refund_lock(ret, reason="verified RF-1/RF-2 in portal")
        self.assertIn("cannot be released", str(ctx.exception))
        self.assertEqual(cp.call_count, 0)
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"), "PARTIAL:RF-1,RF-2")
        self.assertFalse(self._comments(ret))   # refused = no audit comment

    def test_unlock_refuses_real_refund_reference(self):
        ret = self._locked_return(lock="RF-REAL-123")
        with self.assertRaises(frappe.ValidationError):
            unlock_refund_lock(ret, reason="please")
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"), "RF-REAL-123")

    def test_unlock_refuses_non_return_unlocked_cancelled_and_blank_reason(self):
        plain = _pos_draft_invoice(rate=10)
        frappe.db.set_value("POS Invoice", plain, "card_refund_reference",
            "UNKNOWN-OUTCOME", update_modified=False)
        with self.assertRaises(frappe.ValidationError):
            unlock_refund_lock(plain, reason="r")      # not a return
        no_lock = _pos_draft_invoice(rate=10)
        frappe.db.set_value("POS Invoice", no_lock, "is_return", 1,
            update_modified=False)
        with self.assertRaises(frappe.ValidationError):
            unlock_refund_lock(no_lock, reason="r")    # no lock at all
        cancelled = self._locked_return()
        frappe.db.set_value("POS Invoice", cancelled, "docstatus", 2,
            update_modified=False)
        with self.assertRaises(frappe.ValidationError):
            unlock_refund_lock(cancelled, reason="r")  # cancelled doc
        locked = self._locked_return()
        with self.assertRaises(frappe.ValidationError):
            unlock_refund_lock(locked, reason="   ")   # blank reason
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", locked, "card_refund_reference"), "UNKNOWN-OUTCOME")

    def test_unlock_requires_manager(self):
        ret = self._locked_return()
        with patch("frappe.get_roles", return_value=["Sales User"]):
            with self.assertRaises(frappe.PermissionError):
                unlock_refund_lock(ret, reason="r")
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"), "UNKNOWN-OUTCOME")

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_locked_then_unlocked_return_refunds_through_real_entrance(
            self, _p, _rp, cc, _ir):
        """The first-slice-reject recovery story END-TO-END THROUGH refund_pos_invoice
        (an earlier audit killed the old version of this test for hand-
        feeding the residue to _refund_across_rows): a first attempt times
        out with ZERO slices booked → UNKNOWN-OUTCOME lock; the guard blocks
        a blind retry and names the manager exit; unlock; the retry re-runs
        the FULL amount — which is exactly the original entitlement, because
        nothing was booked."""
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        orig = frappe.get_doc("POS Invoice", inv)
        orig.flags.ignore_permissions = True
        orig.submit()
        from erpnext.controllers.sales_and_purchase_return import make_return_doc
        ret = make_return_doc("POS Invoice", inv)
        ret.set("payments", [])
        ret.append("payments", {
            "mode_of_payment": MODE_OF_PAYMENT_NAME, "amount": -50})
        ret.flags.ignore_permissions = True
        ret.save()

        from requests.exceptions import Timeout
        c = cc.from_settings.return_value
        c.refund.side_effect = Timeout("gateway")
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice(ret.name)
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret.name, "card_refund_reference"), "UNKNOWN-OUTCOME")
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 0)

        # A blind retry is blocked by the guard, and the message names the
        # manager exit instead of claiming "already refunded".
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice(ret.name)
        self.assertIn("Unlock Card Refund", str(ctx.exception))
        self.assertEqual(c.refund.call_count, 1)   # the retry never hit the gateway

        unlock_refund_lock(ret.name,
            reason="portal: no refund landed for this return")

        c.refund.side_effect = [{"refundReference": "RF-2"}]
        out = refund_pos_invoice(ret.name)
        self.assertTrue(out["ok"])
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 50)
        self.assertEqual(c.refund.call_count, 2)
        self.assertEqual(
            [(k.args[0], k.kwargs["refund_amount"]) for k in c.refund.call_args_list],
            [("REF-A", 50.0), ("REF-A", 50.0)])   # same full amount both times
        st = frappe.db.get_value("POS Invoice", ret.name,
            ["card_refund_reference", "card_status"], as_dict=True)
        self.assertEqual(st.card_refund_reference, "RF-2")
        self.assertEqual(st.card_status, "REFUNDED")


class ChargeRelockAfterSettle(FrappeTestCase):
    """The settle branch commits (InnoDB releases the
    parent-row lock with it) — charge_pos_invoice must re-take the lock before
    minting the next instruction."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_resume_path_retakes_the_row_lock(self, _p, cc, _ir):
        inv = _pos_draft_invoice(rate=100)
        c = cc.from_settings.return_value
        # First slice stays IN_PROGRESS on the terminal.
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-1", "status": "IN_PROGRESS"}
        charge_pos_invoice(inv, amount=60)
        # Second call finds it COMPLETED: settle commits → relock → next slice.
        c.poll_payment_instruction.return_value = {
            "status": "COMPLETED", "links": [{"href": "https://x/payments/REF-A"}]}
        c.payment_link.return_value = "https://x/payments/REF-A"
        c.retrieve_payment.return_value = _approved_payment("REF-A", 60)
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-2", "status": "IN_PROGRESS"}

        locked_reads = []
        real_get_value = frappe.db.get_value

        def spy(*a, **k):
            if k.get("for_update") and a and a[0] == "POS Invoice":
                locked_reads.append(a)
            return real_get_value(*a, **k)

        with patch.object(frappe.db, "get_value", side_effect=spy):
            out = charge_pos_invoice(inv, amount=40)
        self.assertEqual(out["instruction_id"], "PI-2")
        self.assertEqual(len(locked_reads), 2)   # entry lock + post-settle relock
        rows = _charge_rows(inv)
        self.assertEqual([(r.instruction_id, r.status) for r in rows],
            [("PI-1", "CHARGED"), ("PI-2", "IN_PROGRESS")])

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_foreign_inflight_after_settle_defers_instead_of_minting(self, _p, cc, _ir):
        """The relock must RE-JUDGE, not just re-read.
        Another till minting an instruction in the settle-commit window means
        this request must hand back that in-flight id for polling — never
        create a second live instruction."""
        from ffl_integrations.payments import api as payroc_api
        inv = _pos_draft_invoice(rate=100)
        c = cc.from_settings.return_value
        c.create_payment_instruction.return_value = {
            "paymentInstructionId": "PI-1", "status": "IN_PROGRESS"}
        charge_pos_invoice(inv, amount=60)
        c.poll_payment_instruction.return_value = {
            "status": "COMPLETED", "links": [{"href": "https://x/payments/REF-A"}]}
        c.payment_link.return_value = "https://x/payments/REF-A"
        c.retrieve_payment.return_value = _approved_payment("REF-A", 60)
        create_calls_before = c.create_payment_instruction.call_count

        real_settle = payroc_api._settle_instruction_row

        def settle_then_foreign(client, inst, invoice_name, row):
            res = real_settle(client, inst, invoice_name, row)
            # The other till slips in during the settle-commit unlock window.
            payroc_api._append_charge_row(invoice_name,
                instruction_id="PI-FOREIGN", attempt=9, processor="payroc")
            return res

        with patch("ffl_integrations.payments.api._settle_instruction_row",
                side_effect=settle_then_foreign):
            out = charge_pos_invoice(inv, amount=40)
        self.assertEqual(out["status"], "IN_PROGRESS")
        self.assertEqual(out["instruction_id"], "PI-FOREIGN")
        self.assertEqual(
            c.create_payment_instruction.call_count, create_calls_before)   # no mint
        statuses = [(r.instruction_id, r.status) for r in _charge_rows(inv)]
        self.assertEqual(statuses,
            [("PI-1", "CHARGED"), ("PI-FOREIGN", "IN_PROGRESS")])


class LegacyCapLockedSiblings(FrappeTestCase):
    """The legacy cumulative cap deliberately counts a LOCKED sibling
    at its full card amount (its refund may have landed — under-counting could
    over-refund), but the rejection must then name that sibling and point at
    the manager unlock instead of dead-ending the operator."""

    _ret = PayrocApiUnit._ret
    _orig = PayrocApiUnit._orig

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payments.api.frappe.db.set_value")
    @patch("ffl_integrations.payments.api.frappe.get_all")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._charge_rows", return_value=[])
    @patch("ffl_integrations.payments.api.firearm_items_in", return_value=[])
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1"))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_cap_rejection_names_locked_siblings(self, _p, gd, _gv, _f, _cr, cc, ga, sv, _ir):
        gd.side_effect = [self._ret(), self._orig(40.00)]
        ga.side_effect = [   # orphan gate (clean), then the locked sibling absorbs the cap
            [],
            [frappe._dict(name="RET-LOCKED",
                card_refund_reference="UNKNOWN-OUTCOME")],
            [-40.00]]
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice("RET-1")
        self.assertIn("RET-LOCKED", str(ctx.exception))
        self.assertIn("Unlock Card Refund", str(ctx.exception))
        cc.from_settings.return_value.refund.assert_not_called()


class RefundPersistWindow(FrappeTestCase):
    """The refund-side
    _persist_money_fact wiring under injected DB failures — the slice write's
    idempotency contract (new_refunded computed OUTSIDE the lambda), the
    gateway never re-hit on a write retry, and the lock-arm double failure
    replacing the operator message with the DO-NOT-RETRY stop."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle
    # Same charged-original fixture as the void suite — borrowed, not copied
    # (reuse over copy).
    _charged = VoidPersistWindow._charged_invoice

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_slice_transient_write_failure_retries_idempotently(self, cc):
        """First slice-ledger write fails once → retried under the savepoint;
        refunded_amount ends at EXACTLY old+take (the retry must not double-
        add — the contract that pins new_refunded outside the lambda), and
        the gateway refund is never re-hit."""
        inv = self._charged(cc)
        ret_shell = _pos_draft_invoice(rate=1)
        c = cc.from_settings.return_value
        c.refund.side_effect = [{"refundReference": "RF-1"}]
        real_sv = frappe.db.set_value
        boom = [True]

        def flaky_sv(dt, dn, *a, **k):
            if dt == "Card Charge" and boom and a and a[0] == "refunded_amount":
                boom.pop()
                raise Exception("transient lock wait")
            return real_sv(dt, dn, *a, **k)

        with patch.object(frappe.db, "set_value", side_effect=flaky_sv):
            out = _refund_across_rows(frappe._dict(name=ret_shell), inv,
                [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertTrue(out["ok"])
        self.assertEqual(c.refund.call_count, 1)   # gateway hit ONCE
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 50)   # not 100
        # The return-level booked fact rides the same idempotent write.
        self.assertEqual(flt(frappe.db.get_value(
            "POS Invoice", ret_shell, "card_refunded_amount")), 50)

    @patch("ffl_integrations.payments.api.notify_admins")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_slice_double_write_failure_stops_with_trace(self, cc, na):
        """Both slice-ledger writes fail → DO-NOT-RETRY throw, the row value
        rolls back to the savepoint (the Error Log is the durable record,
        naming the gateway ref), the ops alert fires, and the gateway was hit
        exactly once."""
        inv = self._charged(cc)
        ret_shell = _pos_draft_invoice(rate=1)
        c = cc.from_settings.return_value
        c.refund.side_effect = [{"refundReference": "RF-1"}]
        real_sv = frappe.db.set_value

        def dead_sv(dt, dn, *a, **k):
            if dt == "Card Charge" and a and a[0] == "refunded_amount":
                raise Exception("db down")
            return real_sv(dt, dn, *a, **k)

        with patch.object(frappe.db, "set_value", side_effect=dead_sv):
            with self.assertRaises(frappe.ValidationError) as ctx:
                _refund_across_rows(frappe._dict(name=ret_shell), inv,
                    [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertIn("Do not retry", str(ctx.exception))
        self.assertEqual(c.refund.call_count, 1)
        self.assertEqual(flt(_charge_rows(inv)[0].refunded_amount), 0)   # rolled back
        logs = frappe.get_all("Error Log",
            filters={"method": ["like", f"%{ret_shell}%"]}, fields=["error"])
        self.assertTrue(logs)
        self.assertTrue(any("REF-A" in (l.error or "") for l in logs))
        self.assertEqual(na.call_args.kwargs["dedup_key"],
            f"card-money-fact:{ret_shell}")

    @patch("ffl_integrations.payments.api.notify_admins")
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    def test_lock_arm_double_failure_replaces_message(self, cc, _na):
        """An outcome-unknown first slice arms the lock; if BOTH lock writes
        fail, the thrown message must be the scarier DO-NOT-RETRY stop (with
        an Error Log), not the ordinary 'is now locked' text that promises a guard
        which never actually armed."""
        from requests.exceptions import ConnectionError as ReqConnectionError
        inv = self._charged(cc)
        ret_shell = _pos_draft_invoice(rate=1)
        c = cc.from_settings.return_value
        c.refund.side_effect = ReqConnectionError("net")
        real_sv = frappe.db.set_value

        def dead_lock_sv(dt, dn, *a, **k):
            if (dt == "POS Invoice" and dn == ret_shell and a
                    and isinstance(a[0], dict) and "card_refund_reference" in a[0]):
                raise Exception("db down")
            return real_sv(dt, dn, *a, **k)

        with patch.object(frappe.db, "set_value", side_effect=dead_lock_sv):
            with self.assertRaises(frappe.ValidationError) as ctx:
                _refund_across_rows(frappe._dict(name=ret_shell), inv,
                    [r for r in _charge_rows(inv) if r.status == "CHARGED"], 50.0, None)
        self.assertIn("Do not retry", str(ctx.exception))
        self.assertNotIn("is now locked", str(ctx.exception))
        self.assertFalse(frappe.db.get_value(
            "POS Invoice", ret_shell, "card_refund_reference"))
        self.assertTrue(frappe.get_all("Error Log",
            filters={"method": ["like", f"%{ret_shell}%"]}, pluck="name"))

    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_void_legacy_transient_write_failure_retries(self, _rp, cc):
        """The legacy void stamp clear (the past invoice site) rides
        the same helper: one transient failure → retried, stamps cleared,
        gateway reversed exactly once."""
        inv = _pos_draft_invoice(rate=50)
        frappe.db.set_value("POS Invoice", inv, {
            "card_unique_reference": "REF-LEG", "card_status": "CHARGED",
            "card_charged_amount": 50.0}, update_modified=False)
        cc.from_settings.return_value.reverse.return_value = {"ok": True}
        real_sv = frappe.db.set_value
        boom = [True]

        def flaky_sv(dt, dn, *a, **k):
            if (dt == "POS Invoice" and dn == inv and a
                    and isinstance(a[0], dict) and "card_unique_reference" in a[0]
                    and boom):
                boom.pop()
                raise Exception("transient")
            return real_sv(dt, dn, *a, **k)

        with patch.object(frappe.db, "set_value", side_effect=flaky_sv):
            out = void_pos_invoice(inv, reason="t")
        self.assertTrue(out["ok"])
        cc.from_settings.return_value.reverse.assert_called_once()
        row = frappe.db.get_value("POS Invoice", inv,
            ["card_status", "card_unique_reference"], as_dict=True)
        self.assertEqual(row.card_status, "VOIDED")
        self.assertFalse(row.card_unique_reference)


class RefundGuardLockBranches(FrappeTestCase):
    """The re-refund guard's per-sentinel refusal messages —
    the counter's only signpost for where a locked return goes. The UNKNOWN
    branch is covered by the real-entrance recovery test; this pins the
    PARTIAL branch (back office, never unlock) and the real-ref branch."""

    def _ret(self, reference):
        return frappe._dict({
            "name": "RET-G", "is_return": 1, "return_against": "POS-G",
            "card_refund_reference": reference,
            "payments": [frappe._dict(mode_of_payment="Credit Card", amount=-25.00)],
            "items": [frappe._dict(item_code="AMMO-9MM")]})

    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1", guard='PARTIAL:RF-1'))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    def test_guard_partial_lock_points_to_back_office(self, gd, _gv, cc, _ir, _perm):
        gd.return_value = self._ret("PARTIAL:RF-1")
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice("RET-G")
        msg = str(ctx.exception)
        self.assertIn("by hand in the Payroc back office", msg)
        self.assertIn("cannot be released", msg)
        self.assertNotIn("Unlock Card Refund", msg)   # never send PARTIAL to unlock
        cc.from_settings.return_value.refund.assert_not_called()

    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api.frappe.db.get_value", side_effect=_gv_legacy("REF1", guard='RR-DONE'))
    @patch("ffl_integrations.payments.api.frappe.get_doc")
    def test_guard_real_reference_still_says_already_refunded(self, gd, _gv, cc, _ir, _perm):
        gd.return_value = self._ret("RR-DONE")
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice("RET-G")
        self.assertIn("has already been refunded", str(ctx.exception))
        cc.from_settings.return_value.refund.assert_not_called()


class LegacyPrearmLock(FrappeTestCase):
    """The legacy refund path pre-arms the UNKNOWN-OUTCOME lock
    BEFORE the gateway call — every failure mode defaults to guarded (the
    old path was naked on timeout, and a post-success DB double failure
    left a blind retry able to refund twice). Definite
    non-refunds disarm explicitly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def _legacy_pair(self, cc_amt=-50):
        """Submitted legacy-era original (stamps, NO charge rows) + a draft
        return carrying a Credit Card row of |cc_amt|."""
        inv = _pos_draft_invoice(rate=100)
        frappe.db.set_value("POS Invoice", inv, {
            "card_unique_reference": "REF-LEG", "card_status": "CHARGED",
            "card_charged_amount": 100.0}, update_modified=False)
        orig = frappe.get_doc("POS Invoice", inv)
        orig.flags.ignore_permissions = True
        orig.submit()
        return inv, _cc_return_against(inv, cc_amt)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_timeout_leaves_the_prearmed_lock(self, _p, _rp, cc, _ir):
        """The legacy path's OLD naked hole: a timeout stamped nothing and a
        blind retry could refund twice. Now the pre-armed lock stays."""
        from requests.exceptions import Timeout
        _inv, ret = self._legacy_pair()
        cc.from_settings.return_value.refund.side_effect = Timeout("gateway")
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice(ret)
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"), "UNKNOWN-OUTCOME")
        # The guard now blocks a blind retry cold — gateway stays at 1 call.
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice(ret)
        self.assertIn("Unlock Card Refund", str(ctx.exception))
        self.assertEqual(cc.from_settings.return_value.refund.call_count, 1)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_definite_failure_disarms_and_stays_retryable(self, _p, _rp, cc, _ir):
        _inv, ret = self._legacy_pair()
        c = cc.from_settings.return_value
        c.refund.side_effect = [
            PayrocError("Payroc POST refunds 400: bad request", outcome_known=True),
            {"refundReference": "RF-OK"},
        ]
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice(ret)
        self.assertIn("you can simply retry", str(ctx.exception))
        self.assertFalse(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"))   # disarmed
        out = refund_pos_invoice(ret)                          # retry sails through
        self.assertTrue(out["ok"])
        st = frappe.db.get_value("POS Invoice", ret,
            ["card_refund_reference", "card_status", "card_refunded_amount"],
            as_dict=True)
        self.assertEqual(st.card_refund_reference, "RF-OK")
        self.assertEqual(st.card_status, "REFUNDED")
        self.assertEqual(flt(st.card_refunded_amount), 50)   # booked fact

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_declined_in_200_disarms_and_stamps_response(self, _p, _rp, cc, _ir):
        _inv, ret = self._legacy_pair()
        cc.from_settings.return_value.refund.return_value = {
            "uniqueReference": "REF-LEG", "transactionResult": {
                "resultCode": "D", "resultMessage": "do not honor"}}
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice(ret)
        st = frappe.db.get_value("POS Invoice", ret,
            ["card_refund_reference", "card_response"], as_dict=True)
        self.assertFalse(st.card_refund_reference)           # disarmed
        self.assertIn("do not honor", st.card_response or "")

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_success_overwrite_double_failure_keeps_prearm_guarding(
            self, _p, _rp, cc, _ir):
        """The kill shot: gateway refunded, BOTH overwrite attempts
        fail → the committed pre-arm UNKNOWN-OUTCOME still guards, so the
        blind retry that used to double-refund is now blocked cold."""
        _inv, ret = self._legacy_pair()
        cc.from_settings.return_value.refund.return_value = {"refundReference": "RF-1"}
        real_sv = frappe.db.set_value

        def dead_overwrite(dt, dn, *a, **k):
            if (dt == "POS Invoice" and dn == ret and a
                    and isinstance(a[0], dict)
                    and a[0].get("card_status") == "REFUNDED"):
                raise Exception("db down")
            return real_sv(dt, dn, *a, **k)

        with patch("ffl_integrations.payments.api.notify_admins"), \
                patch.object(frappe.db, "set_value", side_effect=dead_overwrite):
            with self.assertRaises(frappe.ValidationError) as ctx:
                refund_pos_invoice(ret)
        self.assertIn("Do not retry", str(ctx.exception))
        self.assertEqual(frappe.db.get_value(
            "POS Invoice", ret, "card_refund_reference"), "UNKNOWN-OUTCOME")
        with self.assertRaises(frappe.ValidationError) as ctx2:
            refund_pos_invoice(ret)                            # blind retry
        self.assertIn("is locked", str(ctx2.exception))
        self.assertEqual(cc.from_settings.return_value.refund.call_count, 1)


class OrphanRefundGate(FrappeTestCase):
    """Money may only follow SUBMITTED return documents. Recreates
    the 250-on-150 math through the second-return side door and
    asserts the gate kills it; submitting the orphan restores flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    def _orig_two_cards(self, cc):
        # qty 2 × 150 so each unit's return entitlement is a clean 150.
        inv = _pos_draft_invoice(rate=150, qty=2, card_amount=300)
        with patch("ffl_integrations.payments.api._check_perm"), \
                patch("ffl_integrations.payments.api._integration_ready", return_value=True):
            self._charge_and_settle(cc, inv, ask=200, ref="REF-A")
            self._charge_and_settle(cc, inv, ask=100, ref="REF-B")
        doc = frappe.get_doc("POS Invoice", inv)
        doc.flags.ignore_permissions = True
        doc.submit()
        return inv

    def _one_unit_return(self, original):
        return _cc_return_against(original, -150, qty=-1)

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_abandoned_locked_draft_blocks_the_second_return(self, _p, _rp, cc, _ir):
        orig = self._orig_two_cards(cc)
        c = cc.from_settings.return_value

        # Return 1: slice on REF-B books 100, slice on REF-A declines → PARTIAL.
        ret1 = self._one_unit_return(orig)
        c.refund.side_effect = [
            {"refundReference": "RF-1"},
            {"uniqueReference": "REF-A", "transactionResult": {
                "resultCode": "D", "resultMessage": "cannot refund"}},
        ]
        with self.assertRaises(frappe.ValidationError):
            refund_pos_invoice(ret1)
        st1 = frappe.db.get_value("POS Invoice", ret1,
            ["card_refund_reference", "card_refunded_amount"], as_dict=True)
        self.assertTrue((st1.card_refund_reference or "").startswith("PARTIAL:"))
        self.assertEqual(flt(st1.card_refunded_amount), 100)   # booked through the real entrance

        # Return 2 while return 1 sits as an abandoned locked draft: the old
        # code refunded another 150 here (100 + 150 = 250 on 150 of goods).
        ret2 = self._one_unit_return(orig)
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice(ret2)
        self.assertIn(ret1, str(ctx.exception))
        self.assertEqual(c.refund.call_count, 2)   # gate fired BEFORE the gateway

        # Submitting return 1 restores its entitlement — flow resumes and the
        # per-card row caps take over.
        r1 = frappe.get_doc("POS Invoice", ret1)
        r1.flags.ignore_permissions = True
        r1.submit()
        c.refund.side_effect = [{"refundReference": "RF-2"}]
        out = refund_pos_invoice(ret2)
        self.assertTrue(out["ok"])
        self.assertEqual(c.refund.call_count, 3)
        self.assertEqual(
            (c.refund.call_args.args[0], c.refund.call_args.kwargs["refund_amount"]),
            ("REF-A", 150.0))
        self.assertEqual(flt(frappe.db.get_value(
            "POS Invoice", ret2, "card_refunded_amount")), 150)


class LockedReturnTrashGuard(FrappeTestCase):
    """A return carrying a refund LOCK is evidence of pushed-back
    money — deleting it re-arms a clean full-amount refund."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def test_any_money_fact_refuses_deletion(self):
        """Locks, completed references, AND booked amounts all guard: the
        audits probe-proved that deleting a refund-completed
        DRAFT blinds both the legacy cap and the orphan gate (their sibling
        scans lose the record) and re-opens a full re-refund."""
        unk = _marked_return_draft("UNKNOWN-OUTCOME")
        with self.assertRaises(frappe.ValidationError) as ctx:
            frappe.delete_doc("POS Invoice", unk)
        self.assertIn("Unlock Card Refund", str(ctx.exception))
        self.assertTrue(frappe.db.exists("POS Invoice", unk))

        par = _marked_return_draft("PARTIAL:RF-1")
        with self.assertRaises(frappe.ValidationError) as ctx2:
            frappe.delete_doc("POS Invoice", par)
        self.assertIn("back office", str(ctx2.exception))
        self.assertTrue(frappe.db.exists("POS Invoice", par))

        done = _marked_return_draft("RF-REAL-123", booked=50)   # completed refund
        with self.assertRaises(frappe.ValidationError) as ctx3:
            frappe.delete_doc("POS Invoice", done)
        self.assertIn("refunded again", str(ctx3.exception))
        self.assertTrue(frappe.db.exists("POS Invoice", done))

        booked_only = _marked_return_draft(None, booked=25)     # fact without ref
        with self.assertRaises(frappe.ValidationError):
            frappe.delete_doc("POS Invoice", booked_only)
        self.assertTrue(frappe.db.exists("POS Invoice", booked_only))

    def test_clean_returns_still_delete(self):
        clean = _marked_return_draft(None)
        frappe.delete_doc("POS Invoice", clean)
        self.assertFalse(frappe.db.exists("POS Invoice", clean))


class PayrocEndpointMethods(FrappeTestCase):
    """chore: every money-writing payroc endpoint is POST-only; the read-only
    admin smoke stays reachable by GET."""

    def _allowed(self, name, module="ffl_integrations.payments.api"):
        import ffl_integrations.payments.api  # noqa: F401 — registers the whitelist
        import ffl_integrations.payroc.api  # noqa: F401
        for fn, methods in frappe.allowed_http_methods_for_whitelisted_func.items():
            if (getattr(fn, "__module__", "") == module
                    and fn.__name__ == name):
                return methods
        return None

    def test_write_endpoints_are_post_only(self):
        for module in ("ffl_integrations.payments.api", "ffl_integrations.payroc.api"):
            for name in ("charge_pos_invoice", "poll_pos_charge", "cancel_pos_charge",
                    "void_pos_invoice", "refund_pos_invoice", "unlock_refund_lock"):
                self.assertEqual(self._allowed(name, module), ["POST"], f"{module}.{name}")

    def test_discover_terminals_stays_get(self):
        # frappe stores the DEFAULT verb set for methods-less whitelists —
        # the point is that GET stays allowed (read-only admin smoke).
        for module, name in (("ffl_integrations.payments.api", "list_terminals"),
                ("ffl_integrations.payroc.api", "discover_terminals")):
            methods = self._allowed(name, module)
            self.assertIn("GET", methods or [], name)
            self.assertNotEqual(methods, ["POST"])


class ReturnNeverInheritsChargeRows(FrappeTestCase):
    """Product bug flushed out by OrphanRefundGate: restore_charge_rows used
    to SKIP brand-new docs, so make_return_doc's copied charge table (the
    field predates no_copy) was inserted under the return's own name —
    phantom Σcaptured that jammed block_partial_submit on every return of a
    row-era sale. Two-layer fix: card_charges is no_copy now, and the
    restore hook strips foreign rows from any new doc's payload."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_return_of_row_era_sale_owns_no_rows_and_submits(self, _p, cc, _ir):
        inv = _pos_draft_invoice(rate=100)
        self._charge_and_settle(cc, inv, ask=100, ref="REF-A")
        orig = frappe.get_doc("POS Invoice", inv)
        orig.flags.ignore_permissions = True
        orig.submit()
        ret_name = _cc_return_against(inv, -100)
        self.assertEqual(_charge_rows(ret_name), [])   # no phantom rows in DB
        ret = frappe.get_doc("POS Invoice", ret_name)
        ret.flags.ignore_permissions = True
        ret.submit()                                    # guard sees a clean return
        self.assertEqual(ret.docstatus, 1)

    def test_hand_built_insert_payload_is_stripped(self):
        inv = frappe.new_doc("POS Invoice")
        inv.pos_profile = "FFL Test"
        inv.customer = _RW_CUSTOMER
        inv.append("items", {"item_code": _RW_ITEM, "qty": 1, "rate": 10,
            "price_list_rate": 10})
        inv.set_missing_values()
        inv.set("payments", [])
        inv.append("payments", {"mode_of_payment": MODE_OF_PAYMENT_NAME, "amount": 10})
        inv.append("card_charges", {"status": "CHARGED", "amount": 999,
            "instruction_id": "PI-FAKE", "attempt": 1})
        inv.flags.ignore_permissions = True
        inv.save()
        self.assertEqual(_charge_rows(inv.name), [])   # foreign row never landed


class MoneyFactSurvival(FrappeTestCase):
    """The money facts must survive the
    two document-lifecycle attacks the audits probe-proved — a stale client
    save (db.set_value never bumps modified, so Frappe's conflict check is
    blind) and an amend copy (frappe RETAINS no_copy values on amend; the
    consignment suite pinned that platform behavior first)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def test_stale_client_save_cannot_wipe_lock_or_booked(self):
        # A plain draft suffices: the protector covers every POS Invoice,
        # and a bare is_return flag would trip return-shape validation on
        # the re-save this test needs.
        name = _pos_draft_invoice(rate=10)
        stale = frappe.get_doc("POS Invoice", name)   # loaded BEFORE the server writes
        frappe.db.set_value("POS Invoice", name, {
            "card_refund_reference": "UNKNOWN-OUTCOME",
            "card_refunded_amount": 100}, update_modified=False)
        stale.flags.ignore_permissions = True
        stale.save()                                   # would have written the old None/0 back
        row = frappe.db.get_value("POS Invoice", name,
            ["card_refund_reference", "card_refunded_amount"], as_dict=True)
        self.assertEqual(row.card_refund_reference, "UNKNOWN-OUTCOME")
        self.assertEqual(flt(row.card_refunded_amount), 100)

    def test_amend_zeroes_booked_by_hook_and_keeps_the_lock(self):
        """Platform fact (pinned by ffl_core test_consignment_out_reinvoice):
        amend RETAINS no_copy fields. The orphan gate stays honest because
        restore_charge_rows zeroes booked on every NEW doc — while the
        inherited LOCK deliberately stays (fail-safe until a manager acts)."""
        name = _pos_draft_invoice(rate=10)
        frappe.db.set_value("POS Invoice", name, {
            "card_refund_reference": "PARTIAL:RF-1",
            "card_refunded_amount": 60}, update_modified=False)
        doc = frappe.get_doc("POS Invoice", name)
        doc.flags.ignore_permissions = True
        doc.submit()
        doc.cancel()
        amended = frappe.copy_doc(doc)
        amended.amended_from = doc.name
        amended.docstatus = 0
        amended.flags.ignore_permissions = True
        amended.insert()
        row = frappe.db.get_value("POS Invoice", amended.name,
            ["card_refund_reference", "card_refunded_amount"], as_dict=True)
        self.assertEqual(flt(row.card_refunded_amount), 0)   # zeroed BY HOOK
        # …and the inherited LOCK deliberately survives (fail-safe until a
        # manager acts) — whoever makes the islocal branch clear it too will
        # turn this red.
        self.assertEqual(row.card_refund_reference, "PARTIAL:RF-1")
        # The predecessor's facts are untouched — the orphan gate still
        # counts its booked 60 until this amendment submits.
        prev = frappe.db.get_value("POS Invoice", doc.name,
            ["card_refund_reference", "card_refunded_amount"], as_dict=True)
        self.assertEqual(flt(prev.card_refunded_amount), 60)
        self.assertEqual(prev.card_refund_reference, "PARTIAL:RF-1")


class MoneyFactSubmitCancelVectors(FrappeTestCase):
    """Frappe dispatches per _action — submit
    runs validate+before_submit, cancel only before_cancel; before_save runs
    on NEITHER. The scalar guard must ride the same events as
    restore_charge_rows or a stale Desk submit/cancel writes the whole stale
    payload back and re-opens the double refund the audit probes proved."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def test_stale_submit_cannot_wipe_lock_or_booked(self):
        name = _pos_draft_invoice(rate=10)
        stale = frappe.get_doc("POS Invoice", name)   # loaded BEFORE the server writes
        frappe.db.set_value("POS Invoice", name, {
            "card_refund_reference": "UNKNOWN-OUTCOME",
            "card_refunded_amount": 100}, update_modified=False)
        stale.flags.ignore_permissions = True
        stale.submit()                                 # the stale-submit vector
        row = frappe.db.get_value("POS Invoice", name,
            ["card_refund_reference", "card_refunded_amount", "docstatus"],
            as_dict=True)
        self.assertEqual(int(row.docstatus), 1)
        self.assertEqual(row.card_refund_reference, "UNKNOWN-OUTCOME")
        self.assertEqual(flt(row.card_refunded_amount), 100)

    def test_stale_cancel_cannot_wipe_lock_or_booked(self):
        name = _pos_draft_invoice(rate=10)
        doc = frappe.get_doc("POS Invoice", name)
        doc.flags.ignore_permissions = True
        doc.submit()
        stale = frappe.get_doc("POS Invoice", name)   # pre-server-write copy
        frappe.db.set_value("POS Invoice", name, {
            "card_refund_reference": "UNKNOWN-OUTCOME",
            "card_refunded_amount": 100}, update_modified=False)
        stale.flags.ignore_permissions = True
        stale.cancel()                                 # only before_cancel runs
        row = frappe.db.get_value("POS Invoice", name,
            ["card_refund_reference", "card_refunded_amount", "docstatus"],
            as_dict=True)
        self.assertEqual(int(row.docstatus), 2)
        self.assertEqual(row.card_refund_reference, "UNKNOWN-OUTCOME")
        self.assertEqual(flt(row.card_refunded_amount), 100)


class AmendedOriginalRefunds(FrappeTestCase):
    """An amended original is an inherited envelope — the
    copy retains the capture reference while charge rows strip and every
    sibling scan keys off the new name. The legacy path must route the whole
    family to the back office instead of refunding a capture that already
    funded refunds under the predecessor."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_refund_perm")
    def test_refund_against_amended_original_goes_to_back_office(self, _rp, cc, _ir):
        orig = _pos_draft_invoice(rate=100)
        frappe.db.set_value("POS Invoice", orig, {
            "card_unique_reference": "REF-SINGLE", "card_status": "CHARGED",
            "card_charged_amount": 100.0}, update_modified=False)
        doc = frappe.get_doc("POS Invoice", orig)
        doc.flags.ignore_permissions = True
        doc.submit()
        doc.cancel()
        amended = frappe.copy_doc(doc)                 # the Amend button's path
        amended.amended_from = doc.name
        amended.docstatus = 0
        amended.flags.ignore_permissions = True
        amended.insert()
        # The envelope inherits the REAL single-card reference.
        self.assertEqual(frappe.db.get_value("POS Invoice", amended.name,
            "card_unique_reference"), "REF-SINGLE")
        amended.submit()
        ret = _cc_return_against(amended.name, -100)
        with self.assertRaises(frappe.ValidationError) as ctx:
            refund_pos_invoice(ret)
        self.assertIn("amended copy", str(ctx.exception))
        cc.from_settings.return_value.refund.assert_not_called()


class RecycleBinRestoreKeepsChargeRows(FrappeTestCase):
    """Incident 2026-09-08 (a past invoice): a draft carrying a CHARGED
    row was deleted from Recent Orders; putting it back through the Deleted
    Document log re-inserts it as a NEW doc, and restore_charge_rows stripped
    the invoice's own money row as "foreign" — CHARGED stamps with no row,
    the next card would jam block_partial_submit. `flags.from_restore` (set
    by frappe's restore and by ffl_core's POS recycle bin) exempts exactly
    that path; a hand-built insert is still stripped."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    _charge_and_settle = MultiCardLifecycle._charge_and_settle

    @patch("ffl_integrations.payments.api._integration_ready", return_value=True)
    @patch("ffl_integrations.payroc.adapter.PayrocClient")
    @patch("ffl_integrations.payments.api._check_perm")
    def test_restored_draft_keeps_its_charge_row_and_number(self, _p, cc, _ir):
        from ffl_core.api.pos_recycle_bin import restore_deleted_invoice
        inv = _pos_draft_invoice(rate=100, card_amount=40, cash_amount=60)
        self._charge_and_settle(cc, inv, ask=40, ref="REF-BIN")
        self.assertEqual(len(_charge_rows(inv)), 1)

        frappe.delete_doc("POS Invoice", inv, ignore_permissions=True)
        log = frappe.db.get_value("Deleted Document",
            {"deleted_doctype": "POS Invoice", "deleted_name": inv, "restored": 0}, "name")
        self.assertTrue(log)

        out = restore_deleted_invoice(log)
        self.assertEqual(out["name"], inv)          # original number kept
        self.assertFalse(out["renamed"])
        rows = _charge_rows(inv)
        self.assertEqual([(r.status, flt(r.amount), r.unique_reference) for r in rows],
            [("CHARGED", 40.0, "REF-BIN")])
        stamps = frappe.db.get_value("POS Invoice", inv,
            ["card_status", "card_charged_amount"], as_dict=True)
        self.assertEqual((stamps.card_status, flt(stamps.card_charged_amount)), ("CHARGED", 40.0))
        self.assertEqual(frappe.db.get_value("Deleted Document", log, "restored"), 1)
        # The gate is satisfied by the surviving row: Σrows == card tender.
        doc = frappe.get_doc("POS Invoice", inv)
        doc.flags.ignore_permissions = True
        doc.submit()
        self.assertEqual(doc.docstatus, 1)


_AUTH_URL = "/account/authenticate"


class TracebackCredentialRedaction(FrappeTestCase):
    """Gateway credentials must not survive into an Error Log.

    Frappe writes Error Log through ``frappe.get_traceback(with_context=True)``,
    which prints EVERY frame's locals — ours and requests' alike — and redacts
    by variable NAME only (password/passwd/secret/token/key/pwd, see
    frappe.utils._get_traceback_sanitizer). One HTTP 500 out of
    charge_pos_invoice puts that traceback in the database
    (app.py handle_exception -> log_error_snapshot for any >= 500), so a
    credential sitting in a local called ``headers`` is a credential in the DB.

    Every test here drives a REAL requests.Session: the frame that matters most
    is requests' own ``Session.request``, whose ``headers`` parameter holds our
    mapping for the whole call.
    """

    def setUp(self):
        wn_client._bearer.token = ""
        wn_client._bearer.expires_at = 0.0

    def _traceback_from(self, fn) -> str:
        try:
            fn()
        except Exception:
            return frappe.get_traceback(with_context=True)
        self.fail("expected the call to raise")

    def test_merchant_api_key_never_reaches_the_traceback(self):
        """Authenticate carries the Basic key; the socket dies mid-call."""
        cfg = _config()
        cfg.api_key = "MERCHANTKEY-must-not-leak"
        c = PayrocClient(cfg, session=_live_session())
        tb = self._traceback_from(lambda: c.poll_payment_instruction("PI1"))
        self.assertIn("requests/sessions.py", tb)   # the frames we came for
        self.assertNotIn("MERCHANTKEY-must-not-leak", tb)

    def test_bearer_jwt_never_reaches_the_traceback(self):
        """Auth succeeds, the call after it dies: every later request carries
        the JWT in its headers."""
        session = _live_session({_AUTH_URL: (200, {
            "token": "JWT-must-not-leak", "expiresIn": 1})})
        c = PayrocClient(_config(), session=session)
        tb = self._traceback_from(lambda: c.poll_payment_instruction("PI1"))
        self.assertIn("requests/sessions.py", tb)
        self.assertNotIn("JWT-must-not-leak", tb)

    def test_authenticate_response_never_reaches_the_traceback(self):
        """A malformed expiresIn throws while the parsed auth body — and the
        Response object it came from — are still live locals in _bearer_token."""
        session = _live_session({_AUTH_URL: (200, {
            "token": "JWT-body-must-not-leak", "expiresIn": "one hour"})})
        c = PayrocClient(_config(), session=session)
        tb = self._traceback_from(lambda: c.poll_payment_instruction("PI1"))
        self.assertNotIn("JWT-body-must-not-leak", tb)

    def test_terminal_discovery_never_reaches_the_traceback(self):
        """Terminal Discovery reads the same authenticate endpoint; it used to
        hand-roll the Basic header and so bypassed the redaction entirely.
        One carrier, no exceptions."""
        cfg = _config()
        cfg.api_key = "MERCHANTKEY-discovery-must-not-leak"
        c = PayrocClient(cfg, session=_live_session())
        tb = self._traceback_from(c.account_summary)
        self.assertIn("requests/sessions.py", tb)
        self.assertNotIn("MERCHANTKEY-discovery-must-not-leak", tb)

    def test_no_token_error_masks_credential_shaped_keys(self):
        """The diagnostic kept for go-live/key rotation must not echo whatever
        else the gateway put in the body."""
        session = _live_session({_AUTH_URL: (200, {
            "accessToken": "OTHER-SECRET-must-not-leak", "status": "NO_TOKEN"})})
        c = PayrocClient(_config(), session=session)
        with self.assertRaises(PayrocError) as caught:
            c.poll_payment_instruction("PI1")
        self.assertIn("NO_TOKEN", str(caught.exception))      # diagnostics kept
        self.assertNotIn("OTHER-SECRET-must-not-leak", str(caught.exception))

    def test_headers_mapping_stays_transparent_to_requests(self):
        """The redaction must not change what actually goes on the wire."""
        cfg = _config()
        cfg.integration_id = "ISV-9"
        session = _live_session({
            _AUTH_URL: (200, {"token": "JWT9", "expiresIn": 1}),
            "/paymentInstructions/": (200, {"status": "IN_PROGRESS"}),
        })
        sent = []
        original = session.send
        session.send = lambda request, **kw: (sent.append(request), original(request, **kw))[1]
        PayrocClient(cfg, session=session).poll_payment_instruction("PI1")
        self.assertEqual(sent[-1].headers["Authorization"], "Bearer JWT9")
        self.assertEqual(sent[-1].headers["X-Integration-ID"], "ISV-9")


class PayrocOrderId(FrappeTestCase):
    """The per-attempt orderId rule (pure)."""

    def test_first_attempt_is_the_bare_name(self):
        self.assertEqual(_order_id("ACC-PSINV-2026-99001", 1), "ACC-PSINV-2026-99001")
        self.assertEqual(_order_id("ACC-PSINV-2026-99001", 0), "ACC-PSINV-2026-99001")

    def test_later_attempts_are_suffixed(self):
        self.assertEqual(_order_id("ACC-PSINV-2026-99001", 2), "ACC-PSINV-2026-99001-A2")
        self.assertEqual(_order_id("ACC-PSINV-2026-99001", 11), "ACC-PSINV-2026-99001-A11")

    def test_an_amended_document_name_can_never_be_a_prior_attempt_id(self):
        """Frappe amends as {name}-1, {name}-2 …: a bare "-2" attempt id would be
        the NAME of a later amendment, whose own first charge would then reuse an
        orderId Payroc had already processed."""
        self.assertEqual(_order_id("ACC-SINV-2026-99002-2", 1), "ACC-SINV-2026-99002-2")
        self.assertNotEqual(
            _order_id("ACC-SINV-2026-99002", 2), _order_id("ACC-SINV-2026-99002-2", 1))

    def test_cap_trims_the_name_not_the_suffix(self):
        """24 is Payroc's orderId limit; a trimmed suffix would collide with the
        attempt before it, which is the whole point of suffixing."""
        long_name = "ACC-PSINV-2026-99001-XYZ"
        for attempt in (2, 3, 12):
            out = _order_id(long_name, attempt)
            self.assertLessEqual(len(out), 24)
            self.assertTrue(out.endswith(f"-A{attempt}"), out)
        self.assertNotEqual(_order_id(long_name, 2), _order_id(long_name, 3))
