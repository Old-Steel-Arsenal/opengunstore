"""The card processor seam (docs/extensions/hooks.md, ``gunshop_card_processors``).

A fake processor — an in-memory gateway, no Payroc anywhere — is registered through the hook
the way an extension would register one, and drives the neutral orchestrator end to end: a
two-card POS charge, submit, a partial return refunded newest card first. Then the rules the
seam adds: one processor switched on at a time; void / refund go to the processor that took the
charge (a sale charged by Payroc is reversed through Payroc after the store moved to another
processor, and refused like a switched-off Payroc when its processor is off or gone); a
provider that fails is logged and left out.

Real schema throughout (charge rows, stamps, submit, return); only the gateway is fake and
Payroc's client is a MagicMock where Payroc is in play.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.payments import api, hub, registry
from ffl_integrations.payments.base import APPROVED, PENDING, CardProcessor, CardResult
from ffl_integrations.tests.site_fixtures import payroc_configured
from ffl_integrations.tests.test_payroc import (
    _approved_payment,
    _cc_return_against,
    _ensure_pos_fixtures,
    _pos_draft_invoice,
)

HERE = "ffl_integrations.tests.test_card_processor_seam"
PAYROC = "ffl_integrations.payroc.adapter.processor"
FAKE = f"{HERE}.fake_processor"
PAYROC_SETTINGS = "Payroc Settings"


# These tests stand in for a store whose Payroc is configured (the test site holds no key).
_PAYROC_CONFIGURED = payroc_configured()


def setUpModule():
    _PAYROC_CONFIGURED.start()


def tearDownModule():
    _PAYROC_CONFIGURED.stop()


class FakeGateway:
    """What the fake processor's gateway holds — shared by every instance, reset per test."""
    on = True
    charges: dict = {}
    calls: list = []

    @classmethod
    def reset(cls):
        cls.on, cls.charges, cls.calls = True, {}, []


class FakeProcessor(CardProcessor):
    code = "fakepay"
    label = "FakePay"
    settings_doctype = "Payroc Settings"     # never saved by these tests: only the hub reads it

    def enabled(self):
        return FakeGateway.on

    def ready(self, require_device=False):
        return FakeGateway.on

    def start_charge(self, *, document, attempt, amount, terminal, description, operator):
        handle = f"FP-{document}-{attempt}"
        FakeGateway.charges[handle] = amount
        FakeGateway.calls.append(("start", document, attempt, amount))
        return handle, ""

    def poll(self, handle):
        FakeGateway.calls.append(("poll", handle))
        if handle not in FakeGateway.charges:
            return CardResult(status=PENDING)
        amount = FakeGateway.charges[handle]
        return CardResult(status=APPROVED, amount=amount, unique_reference=f"REF-{handle}",
            approval_code="OK", card_summary="Fake 4242", raw={"handle": handle, "amount": amount})

    def cancel(self, handle):
        FakeGateway.calls.append(("cancel", handle))
        return CardResult(status="CANCELED", raw={})

    def void(self, unique_reference, *, reason):
        FakeGateway.calls.append(("void", unique_reference))
        return CardResult(status=APPROVED, raw={"voided": unique_reference})

    def refund(self, unique_reference, *, amount, reason, operator):
        FakeGateway.calls.append(("refund", unique_reference, amount))
        return CardResult(status=APPROVED, unique_reference=f"RF-{unique_reference}",
            raw={"refunded": amount})

    def list_terminals(self):
        return {"terminals": ["FAKE-1"]}

    def probe(self):
        return [{"label": "Fake", "ok": True, "detail": "ok"}]


def fake_processor():
    return FakeProcessor()


def broken_factory():
    raise RuntimeError("provider down")


def not_a_processor():
    return {"code": "nope"}


def duplicate_payroc():
    p = FakeProcessor()
    p.code = "payroc"
    p.label = "Impostor"
    return p


@contextmanager
def card_hooks(*paths, extra: dict | None = None):
    """frappe.get_hooks("gunshop_card_processors") → paths (and any ``extra`` hook → its value);
    every other hook untouched."""
    real = frappe.get_hooks
    answers = {registry.HOOK: list(paths), **(extra or {})}

    def fake(hook=None, default="_KEEP_", app_name=None):
        if hook in answers:
            return answers[hook]
        return real(hook, app_name=app_name) if default == "_KEEP_" else real(hook, default, app_name)

    with patch.object(frappe, "get_hooks", side_effect=fake):
        yield


class _Seam(SettingsSandbox, FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _ensure_pos_fixtures()

    def setUp(self):
        super().setUp()
        FakeGateway.reset()
        self.addCleanup(FakeGateway.reset)


class RegistryContract(_Seam):
    def test_payroc_comes_through_the_same_hook(self):
        self.assertIn(PAYROC, frappe.get_hooks(registry.HOOK))
        with card_hooks(PAYROC, FAKE):
            self.assertEqual([p.code for p in registry.processors()], ["payroc", "fakepay"])
            self.assertEqual(registry.get_processor("fakepay").label, "FakePay")
            self.assertIsNone(registry.get_processor("gone"))

    def test_a_failing_or_wrong_provider_is_logged_and_left_out(self):
        with card_hooks(f"{HERE}.broken_factory", f"{HERE}.not_a_processor", PAYROC,
                f"{HERE}.duplicate_payroc", FAKE):
            with patch.object(registry.frappe, "log_error") as logged:
                codes = [(p.code, p.label) for p in registry.processors()]
        self.assertEqual(codes, [("payroc", "Payroc"), ("fakepay", "FakePay")])   # first payroc wins
        titles = [c.kwargs.get("title", "") for c in logged.call_args_list]
        self.assertTrue(any("broken_factory" in t for t in titles), titles)
        self.assertTrue(any("not_a_processor" in t for t in titles), titles)

    def test_a_switched_off_extension_contributes_no_processor(self):
        with card_hooks(FAKE, extra={"gunshop_extension": {"title": "Fake"}}), \
                patch("ffl_core.extensions.is_enabled", return_value=False):
            self.assertEqual(registry.processors(), [])
        with card_hooks(FAKE, extra={"gunshop_extension": {"title": "Fake"}}), \
                patch("ffl_core.extensions.is_enabled", return_value=True):
            self.assertEqual([p.code for p in registry.processors()], ["fakepay"])


class OneProcessorAtATime(_Seam):
    def test_two_switched_on_refuse_to_pick(self):
        self.set_single(PAYROC_SETTINGS, "enabled", 1)
        with card_hooks(PAYROC, FAKE):
            with self.assertRaises(frappe.ValidationError) as ctx:
                registry.active_processor()
            self.assertIn("More than one card processor", str(ctx.exception))
            self.assertTrue(registry.any_on())     # the validate-time question never throws

    def test_the_hub_refuses_a_second_switch(self):
        self.set_single(PAYROC_SETTINGS, "enabled", 0)
        with card_hooks(PAYROC, FAKE), patch.object(hub.frappe, "get_doc") as get_doc:
            with self.assertRaises(frappe.ValidationError) as ctx:
                hub.toggle("payroc", 1)
            get_doc.assert_not_called()
        self.assertIn("FakePay is switched on", str(ctx.exception))
        # Switching one OFF is always allowed.
        FakeGateway.on = False
        with card_hooks(PAYROC, FAKE):
            self.assertEqual(registry.active_processor(), None)

    def test_payroc_settings_refuses_to_switch_on_beside_another(self):
        self.set_single(PAYROC_SETTINGS, "enabled", 0)
        doc = frappe.get_doc(PAYROC_SETTINGS)
        doc.enabled = 1
        doc.api_key = "test-key"
        with card_hooks(PAYROC, FAKE), self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_payroc_switched_on_without_a_key_does_not_count(self):
        """A site that never saved Payroc Settings reads Payroc as switched on. Without
        credentials it takes no cards, so it must not stand in the way of the store's
        real processor."""
        self.set_single(PAYROC_SETTINGS, "enabled", 1)
        with card_hooks(PAYROC, FAKE), \
                patch("ffl_integrations.integration_gate.payroc_ready", return_value=False):
            self.assertEqual(registry.active_processor().code, "fakepay")
            registry.refuse_second_active("fakepay")             # must not raise
            doc = frappe.get_doc(PAYROC_SETTINGS)
            doc.enabled = 1
            doc.validate()                                       # no key: not arming Payroc

    def test_boot_names_the_active_processor(self):
        from ffl_integrations.boot import extend_bootinfo

        self.set_single(PAYROC_SETTINGS, "enabled", 0)
        boot = frappe._dict()
        with card_hooks(PAYROC, FAKE):
            extend_bootinfo(boot)
        self.assertEqual(boot.ffl_card_processor, {"code": "fakepay", "label": "FakePay", "enabled": 1})
        self.assertEqual(boot.card_mop, "Credit Card")


class FakeProcessorDrivesTheOrchestrator(_Seam):
    """The seam proven: nothing Payroc runs, every money fact is the orchestrator's."""

    def _charge(self, inv, amount):
        out = api.charge_pos_invoice(inv, amount=amount)
        self.assertEqual(out["status"], "IN_PROGRESS")
        return api.poll_pos_charge(inv, out["instruction_id"])

    def test_two_cards_submit_and_a_partial_return_refund_newest_first(self):
        self.set_single(PAYROC_SETTINGS, "enabled", 0)
        with card_hooks(PAYROC, FAKE), patch("ffl_integrations.payroc.adapter.PayrocClient") as payroc:
            inv = _pos_draft_invoice(rate=50, qty=2)              # 100 on the card
            first = self._charge(inv, 60)
            self.assertEqual((first["ok"], first["remaining"]), (True, 40))
            second = self._charge(inv, 40)
            self.assertEqual((second["ok"], second["remaining"]), (True, 0))

            rows = api._charge_rows(inv)
            self.assertEqual([(r.processor, r.status, r.amount) for r in rows],
                [("fakepay", "CHARGED", 60), ("fakepay", "CHARGED", 40)])
            st = frappe.db.get_value("POS Invoice", inv, ["card_status", "card_charged_amount",
                "card_unique_reference", "card_attempt"], as_dict=True)
            self.assertEqual((st.card_status, st.card_charged_amount, st.card_unique_reference,
                st.card_attempt), ("CHARGED", 100, "MULTI", 2))

            doc = frappe.get_doc("POS Invoice", inv)
            doc.flags.ignore_permissions = True
            doc.submit()                                         # block_partial_submit: 100 == 100

            ret = _cc_return_against(inv, -50, qty=-1)
            out = api.refund_pos_invoice(ret)
            payroc.from_settings.assert_not_called()

        self.assertTrue(out["ok"])
        self.assertEqual(out["refund_references"], [f"RF-{rows[1].unique_reference}",
            f"RF-{rows[0].unique_reference}"])
        refunds = [c for c in FakeGateway.calls if c[0] == "refund"]
        self.assertEqual(refunds, [("refund", rows[1].unique_reference, 40),
            ("refund", rows[0].unique_reference, 10)])
        self.assertEqual([r.refunded_amount for r in api._charge_rows(inv)], [10, 40])
        st = frappe.db.get_value("POS Invoice", ret, ["card_refunded_amount", "card_refund_reference",
            "card_status"], as_dict=True)
        self.assertEqual((st.card_refunded_amount, st.card_refund_reference, st.card_status),
            (50, "MULTI", "REFUNDED"))

    def test_nothing_on_is_inert(self):
        self.set_single(PAYROC_SETTINGS, "enabled", 0)
        FakeGateway.on = False
        with card_hooks(PAYROC, FAKE):
            inv = _pos_draft_invoice(rate=20)
            self.assertEqual(api.charge_pos_invoice(inv), {"skip": True})
        self.assertEqual(FakeGateway.calls, [])


class ChargesGoBackToTheirOwnProcessor(_Seam):
    def _payroc_charged_sale(self, payroc_cls, amount=100):
        """A sale charged through Payroc while Payroc was the active processor, submitted."""
        self.set_single(PAYROC_SETTINGS, "enabled", 1)
        FakeGateway.on = False
        c = payroc_cls.from_settings.return_value
        inv = _pos_draft_invoice(rate=amount)
        c.create_payment_instruction.return_value = {"paymentInstructionId": "PI-P1", "status": "IN_PROGRESS"}
        with patch.object(api, "_integration_ready", return_value=True):
            api.charge_pos_invoice(inv)
        c.poll_payment_instruction.return_value = {"status": "COMPLETED",
            "links": [{"href": "https://x/payments/REF-P1"}]}
        c.payment_link.return_value = "https://x/payments/REF-P1"
        c.retrieve_payment.return_value = _approved_payment("REF-P1", amount)
        api.poll_pos_charge(inv, "PI-P1")
        self.assertEqual([r.processor for r in api._charge_rows(inv)], ["payroc"])
        return inv

    def test_void_reaches_payroc_after_the_store_moved_on(self):
        with card_hooks(PAYROC, FAKE), patch("ffl_integrations.payroc.adapter.PayrocClient") as payroc:
            inv = self._payroc_charged_sale(payroc)
            self.set_single(PAYROC_SETTINGS, "enabled", 0)   # the store switches to FakePay
            FakeGateway.on = True
            payroc.from_settings.return_value.reverse.return_value = {"ok": True}
            out = api.void_pos_invoice(inv)
        self.assertTrue(out["ok"])
        payroc.from_settings.return_value.reverse.assert_called_once()
        self.assertEqual(payroc.from_settings.return_value.reverse.call_args.args[0], "REF-P1")
        self.assertFalse([c for c in FakeGateway.calls if c[0] == "void"])
        self.assertEqual(api._charge_rows(inv)[0].status, "VOIDED")

    def test_refund_of_a_payroc_sale_with_payroc_off_waits_for_the_portal_refund(self):
        """The active processor is ready, but the money went through Payroc, now off:
        exactly today's switched-off path — refused, ops told, nothing sent anywhere."""
        with card_hooks(PAYROC, FAKE), patch("ffl_integrations.payroc.adapter.PayrocClient") as payroc:
            inv = self._payroc_charged_sale(payroc)
            doc = frappe.get_doc("POS Invoice", inv)
            doc.flags.ignore_permissions = True
            doc.submit()
            ret = _cc_return_against(inv, -100)
            self.set_single(PAYROC_SETTINGS, "enabled", 0)
            FakeGateway.on = True
            with patch.object(api, "notify_admins") as notify, \
                    self.assertRaises(frappe.ValidationError) as ctx:
                api.refund_pos_invoice(ret)
        self.assertIn("Confirm Portal Refund", str(ctx.exception))
        self.assertIn("Payroc integration is switched off", str(ctx.exception))
        notify.assert_called_once()
        payroc.from_settings.return_value.refund.assert_not_called()
        self.assertFalse([c for c in FakeGateway.calls if c[0] == "refund"])

    def test_a_charge_from_a_processor_no_longer_installed(self):
        with card_hooks(PAYROC, FAKE), patch("ffl_integrations.payroc.adapter.PayrocClient") as payroc:
            inv = self._payroc_charged_sale(payroc)
            row = api._charge_rows(inv)[0]
            frappe.db.set_value("Card Charge", row.name, "processor", "gone", update_modified=False)
            with self.assertRaises(frappe.ValidationError) as ctx:
                api.void_pos_invoice(inv)
            self.assertIn('"gone"', str(ctx.exception))
            payroc.from_settings.return_value.reverse.assert_not_called()

            doc = frappe.get_doc("POS Invoice", inv)
            doc.flags.ignore_permissions = True
            doc.submit()
            ret = _cc_return_against(inv, -100)
            self.set_single(PAYROC_SETTINGS, "enabled", 0)     # FakePay active and ready:
            FakeGateway.on = True                             # only the row's processor can refuse
            with patch.object(api, "notify_admins"), self.assertRaises(frappe.ValidationError) as ctx:
                api.refund_pos_invoice(ret)
        self.assertIn("Confirm Portal Refund", str(ctx.exception))
        payroc.from_settings.return_value.refund.assert_not_called()
        self.assertFalse([c for c in FakeGateway.calls if c[0] == "refund"])
