"""The Payroc / FastBound on-off switches, and what a site running without them does.

Three groups:

* ``IntegrationSwitchDefaults`` — the read semantics, including the migrate trap
  these switches exist inside of (a Check field defaulting to 1 reads back as 0 on
  every site that has never saved the Single).
* ``PayrocSwitchedOff`` — the gateway goes inert and the card row is plain tender.
* ``FastBoundSwitchedOff`` — nothing leaves for the bound book, from any path.

Every test changes its switch through ``SettingsSandbox.set_single``, which
restores the RAW prior state on teardown. Relying on the test transaction alone is
not enough here: the rollback only rewinds to the last commit, so one intervening
commit anywhere in the run would leave a switch flipped for every module after
this one — and "FastBound is off" is not a failure that points back at its cause.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.integration_gate import (
	card_reference_required,
	FASTBOUND_SETTINGS,
	PAYROC_SETTINGS,
	fastbound_enabled,
	payroc_enabled,
	payroc_ready,
)


class IntegrationSwitchDefaults(SettingsSandbox, FrappeTestCase):
	def test_unset_switch_reads_as_the_docfield_default_not_zero(self):
		"""The bug these switches would otherwise ship WITH.

		A Single stores one tabSingles row per field that has ever been saved, and
		``frappe.db.get_single_value`` casts a missing row to 0 for a Check. So the
		migrate that adds "Enable Payroc" (docfield default 1) would read back OFF on
		every existing site — silently disabling a live integration in production
		until somebody opened the form and pressed Save.

		The first assertion pins that framework behaviour (if a Frappe upgrade ever
		makes get_single_value honour docfield defaults, this test says so out loud
		rather than leaving dead defensive code behind). The second is the contract:
		our readers return the DEFAULT for a row that was never written.
		"""
		self.clear_single(PAYROC_SETTINGS, "enabled")
		self.clear_single(FASTBOUND_SETTINGS, "enabled")

		self.assertEqual(
			frappe.db.get_single_value(PAYROC_SETTINGS, "enabled", cache=False), 0,
			"framework behaviour changed — re-read utils.single_flag's rationale")

		self.assertTrue(payroc_enabled(), "an unsaved switch must read as its default (ON)")
		self.assertTrue(fastbound_enabled(), "an unsaved switch must read as its default (ON)")

	def test_explicit_zero_disables(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(FASTBOUND_SETTINGS, "enabled", 0)
		self.assertFalse(payroc_enabled())
		self.assertFalse(fastbound_enabled())

	def test_explicit_one_enables(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self.set_single(FASTBOUND_SETTINGS, "enabled", 1)
		self.assertTrue(payroc_enabled())
		self.assertTrue(fastbound_enabled())

	def test_switches_are_independent(self):
		"""'Separately' is the whole requirement: a store may keep FastBound and
		swipe on a standalone terminal, or keep Payroc and a paper bound book."""
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(FASTBOUND_SETTINGS, "enabled", 1)
		self.assertFalse(payroc_enabled())
		self.assertTrue(fastbound_enabled())

	def test_card_reference_requirement_is_inert_while_payroc_is_live(self):
		"""With the gateway on, the approval code comes back from the terminal —
		there is nothing for staff to type, so a stale checked sub-flag (e.g. left
		over from a period running on the standalone terminal) must not start
		demanding a hand-typed reference the moment Payroc is switched back on."""
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 1)
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self.assertFalse(card_reference_required())

		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.assertTrue(card_reference_required())

	def test_card_reference_defaults_on_in_manual_mode(self):
		"""Safe to default on precisely
		because it is inert while the gateway is live: it costs a Payroc store
		nothing and only reaches the stores that have no gateway record at all."""
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.clear_single(PAYROC_SETTINGS, "require_card_reference")
		self.assertTrue(card_reference_required())

	def test_card_reference_can_still_be_turned_off(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 0)
		self.assertFalse(card_reference_required())

	def test_payroc_ready_is_false_when_switched_off_regardless_of_credentials(self):
		"""Credentials outlive the decision to stop using a vendor. Unchecking the
		box must be enough — an operator should not have to delete their API key to
		stop the terminal flow."""
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		with patch("ffl_integrations.payroc.client.PayrocConfig.from_settings") as from_settings:
			from_settings.return_value = MagicMock(default_serial_number="A80-LIVE")
			self.assertFalse(payroc_ready())
			from_settings.assert_not_called()  # short-circuits before reading credentials


class PayrocSwitchedOff(SettingsSandbox, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(PAYROC_SETTINGS, "enabled", 0)

	def test_charge_is_inert(self):
		"""The pre-existing go-live gate now also answers to the switch: POS gets
		{"skip": True} and submits the invoice with the card row as plain tender."""
		from ffl_integrations.payments.api import charge_pos_invoice

		with patch("ffl_integrations.payroc.adapter.PayrocClient") as client_cls:
			self.assertEqual(charge_pos_invoice("PSINV-DOES-NOT-EXIST"), {"skip": True})
			client_cls.from_settings.assert_not_called()

	def test_refund_is_inert(self):
		""""Unconfigured ⇒ skip, touching nothing" — including for a docname that
		does not exist. An earlier cut of the gateway-charge check reordered this
		into a get_doc() and turned the skip into DoesNotExistError; CI caught it on
		this test and its twin in test_payroc."""
		from ffl_integrations.payments.api import refund_pos_invoice

		with patch("ffl_integrations.payroc.adapter.PayrocClient") as client_cls:
			self.assertEqual(refund_pos_invoice("PSINV-DOES-NOT-EXIST"), {"skip": True})
			client_cls.from_settings.assert_not_called()

	def test_refund_of_a_gateway_charged_sale_refuses_until_confirmed(self):
		"""The one case where "inert" is unsafe, and what this PR does about it.

		Sale charged on the terminal → operator switches Payroc off → customer
		returns it. Skipping would book a refund the card never received, so the
		return is REFUSED — and ops are emailed, because the person at the
		counter is not the person with the portal login. The exit is a manager
		action (confirm_out_of_band_refund), deliberately not a field: an earlier
		design accepted the tender row's reference as the evidence, and the
		standalone-terminal rule force-collects that very field, so the guard
		cleared itself in the default configuration.
		"""
		from ffl_integrations.payments import api

		def _get_value(doctype, name, field=None, *a, **k):
			return {"return_against": "PSINV-ORIG",
				"card_unique_reference": "AUTH-123"}.get(field)

		with patch.object(api.frappe.db, "exists", return_value=True), \
				patch.object(api.frappe.db, "get_value", side_effect=_get_value), \
				patch.object(api, "_charge_rows", return_value=[frappe._dict({
					"status": "CHARGED", "amount": 100.0, "unique_reference": "AUTH-123"})]), \
				patch.object(api, "notify_admins") as notify:
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.refund_pos_invoice("PSINV-RET")
			notify.assert_called_once()
		self.assertIn("Confirm Portal Refund", str(ctx.exception))
		self.assertIn("AUTH-123", notify.call_args.kwargs["message"])

	def test_a_confirmed_portal_refund_lets_the_return_skip(self):
		"""After the manager records the portal refund, the same call answers
		{"skip": True} — the return may submit, and the money story is on the
		document (its own field + timeline), not implied by silence."""
		from ffl_integrations.payments import api

		def _get_value(doctype, name, field=None, *a, **k):
			return {"return_against": "PSINV-ORIG",
				"card_unique_reference": "AUTH-123",
				"card_oob_refund_reference": "PORTAL-RF-77"}.get(field)

		with patch.object(api.frappe.db, "exists", return_value=True), \
				patch.object(api.frappe.db, "get_value", side_effect=_get_value), \
				patch.object(api, "_charge_rows", return_value=[frappe._dict({
					"status": "CHARGED", "amount": 100.0, "unique_reference": "AUTH-123"})]), \
				patch.object(api, "notify_admins") as notify:
			self.assertEqual(api.refund_pos_invoice("PSINV-RET"), {"skip": True})
			notify.assert_not_called()

	def test_refund_of_a_never_charged_sale_still_skips(self):
		"""A cash / standalone-terminal original has no gateway money to send back,
		so the return proceeds exactly as before."""
		from ffl_integrations.payments import api

		def _get_value(doctype, name, field=None, *a, **k):
			return {"return_against": "PSINV-ORIG2"}.get(field)

		with patch.object(api.frappe.db, "exists", return_value=True), \
				patch.object(api.frappe.db, "get_value", side_effect=_get_value), \
				patch.object(api, "_charge_rows", return_value=[]):
			self.assertEqual(api.refund_pos_invoice("PSINV-RET2"), {"skip": True})

	def test_terminal_discovery_is_inert(self):
		from ffl_integrations.payroc.api import discover_terminals

		with patch("ffl_integrations.payroc.adapter.PayrocClient") as client_cls:
			self.assertEqual(discover_terminals(), {"skip": True})
			client_cls.from_settings.assert_not_called()

	def test_boot_tells_the_pos_bundle(self):
		"""The bundle short-circuits on boot rather than saving a draft and making a
		round-trip to a server that would only answer {"skip": true}."""
		from ffl_integrations.boot import extend_bootinfo

		boot = frappe._dict()
		extend_bootinfo(boot)
		self.assertEqual(boot.ffl_card_processor, {"code": "", "label": "", "enabled": 0})
		self.assertEqual(boot.card_mop, "Credit Card")


class FastBoundSwitchedOff(SettingsSandbox, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(FASTBOUND_SETTINGS, "enabled", 0)
		frappe.local.response = frappe._dict()

	def test_client_refuses_to_build(self):
		"""The net under every outbound path that is not a hook or a cron — the
		manual admin tools, ffl_core's serial correction, anything added later."""
		from ffl_integrations.fastbound.client import FastBoundConfig, FastBoundConfigError

		with self.assertRaises(FastBoundConfigError) as ctx:
			FastBoundConfig.from_settings()
		self.assertIn("switched off", str(ctx.exception).lower())

	def test_acquisition_submit_enqueues_nothing(self):
		from ffl_integrations.fastbound import tasks

		doc = frappe._dict({"name": "FFL-ACQ-X", "fastbound_id": None})
		with patch.object(tasks.frappe, "enqueue") as enq:
			tasks.enqueue_push_on_submit(doc)
			enq.assert_not_called()

	def test_disposition_submit_enqueues_nothing(self):
		from ffl_integrations.fastbound import tasks

		doc = frappe._dict({"name": "FFL-DISP-X", "fastbound_id": None,
			"disposition_type": "Dealer Transfer"})
		with patch.object(tasks.frappe, "enqueue") as enq:
			tasks.enqueue_disposition_push_on_submit(doc)
			enq.assert_not_called()

	def test_queued_push_that_predates_the_switch_does_not_fire(self):
		"""Guarded on both sides: a job already sitting in the queue when the
		operator unchecked the box must not push either."""
		from ffl_integrations.fastbound import tasks

		with patch.object(tasks.frappe, "get_doc") as get_doc:
			tasks.push_acquisition_to_fastbound("FFL-ACQ-QUEUED")
			get_doc.assert_not_called()

	def test_receiving_never_reaches_the_synchronous_intake_push(self):
		"""Receiving has to work with no FastBound account. This is the one
		FastBound call that can ROLL BACK a receive — it runs inside the Receive
		Goods transaction and raises on rejection — so a store without an account
		must never get near it, or intake stops working entirely."""
		from ffl_integrations.fastbound import tasks

		with patch.object(tasks.frappe, "get_all") as get_all:
			tasks.push_receipt_acquisitions_sync("PREC-DOES-NOT-EXIST")
			get_all.assert_not_called()

	def test_smartlist_cron_is_quiet(self):
		from ffl_integrations.fastbound import client_api

		with patch.object(client_api, "sync_smart_lists") as sync:
			client_api.sync_smart_lists_scheduled()
			sync.assert_not_called()

	def test_reconcile_and_stuck_digest_are_quiet(self):
		"""Without this the store is nagged daily about a backlog it cannot clear:
		every locally-booked record is 'unsynced' forever."""
		from ffl_integrations.fastbound import tasks

		with patch.object(tasks.frappe, "get_all") as get_all, \
				patch.object(tasks, "notify_admins") as notify:
			tasks.reconcile_failed_pushes()
			tasks.reconcile_failed_deletes()
			tasks.alert_stuck_fastbound()
			get_all.assert_not_called()
			notify.assert_not_called()

	def test_trade_in_deadline_alert_still_runs(self):
		"""Deliberately NOT gated: §478.125(e)'s next-business-day bound-book clock
		is about OUR records and matters more to a paper-book store, not less."""
		from ffl_integrations.fastbound import tasks

		with patch.object(tasks.frappe, "get_all", return_value=[]) as get_all:
			tasks.alert_stale_trade_in_drafts()
			get_all.assert_called()

	def test_webhook_is_refused_without_reading_the_body(self):
		"""A site with no FastBound relationship must not accept bound-book state
		from one — and must not leak whether a signature would have verified."""
		from ffl_integrations.fastbound import webhook as fb_webhook

		with patch.object(fb_webhook, "_read_raw_body") as read_body, \
				patch.object(fb_webhook, "_get_webhook_secret") as secret:
			res = fb_webhook.fastbound_event()
			read_body.assert_not_called()
			# Not reading the secret is half the point: the refusal has to be
			# computable on a site whose FastBound credentials were cleared out
			# when the operator switched the integration off.
			secret.assert_not_called()
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)

	def test_webhook_refusal_is_indistinguishable_from_a_bad_signature(self):
		"""The switch must not be readable from outside.

		`fastbound_event` is guest-open by construction, so whatever it answers an
		unauthenticated POST is public. It used to answer a distinctive 404 naming
		the integration, which told any prober — including one shopping for shops
		that just walked away from a FastBound account — exactly which stores keep
		their bound book there. Both states now return the same 401 an enabled site
		gives a bad signature, so status AND body must match character for
		character on every unauthenticated shape.
		"""
		from ffl_integrations.fastbound import webhook as fb_webhook

		def _answer(enabled: int, header: str | None) -> tuple:
			self.set_single(FASTBOUND_SETTINGS, "enabled", enabled)
			frappe.local.response = frappe._dict()

			def _get(key, default=None):
				return header if key.lower() == "x-fastbound-signature" else default

			with patch.object(frappe, "get_request_header", side_effect=_get), \
					patch.object(fb_webhook, "_read_raw_body", return_value=b"{}"):
				body = fb_webhook.fastbound_event()
			return frappe.local.response.get("http_status_code"), body

		for label, header in (
			("unsigned", None),
			("malformed", "garbage"),
			("wrong signature", "t=1,v1=" + "0" * 64),
		):
			with self.subTest(label):
				off = _answer(0, header)
				on = _answer(1, header)
				self.assertEqual(off, on)
				self.assertEqual(off[0], 401)
				# and however the two agree, the answer must not name the switch
				self.assertNotIn("disabled", str(off[1]).lower())

	def test_an_enabled_but_unconfigured_site_answers_the_same_401(self):
		"""The third state, and the one a prober would most like to find.

		"Switched on, secret not filled in yet" used to name itself in the response
		body, which re-opened the oracle from the other side: the switched-off
		answer and the not-yet-configured answer were two different sentences, so
		either one identified the store. Both are now the ordinary mismatch, and
		the operator learns about it from the Error Log instead.

		Found by a FRESH site: every warm one has a committed webhook_secret left
		by test_fastbound_pos_4473, which hid this for as long as it existed.
		"""
		from ffl_integrations.fastbound import webhook as fb_webhook

		self.set_single(FASTBOUND_SETTINGS, "enabled", 1)
		frappe.local.response = frappe._dict()
		real_get_single = frappe.get_single

		def _single(doctype, *args, **kwargs):
			# Answer ONLY the doctype under test; everything else goes through, so
			# this cannot reach code that merely happens to read some other Single.
			if doctype == FASTBOUND_SETTINGS:
				return frappe._dict(get_password=lambda *a, **k: "")
			return real_get_single(doctype, *args, **kwargs)

		with patch.object(frappe, "get_request_header",
					side_effect=lambda k, d=None: ("t=1,v1=" + "0" * 64)
						if k.lower() == "x-fastbound-signature" else d), \
				patch.object(fb_webhook.frappe, "get_single", side_effect=_single), \
				patch.object(fb_webhook, "_log_error") as logged, \
				patch.object(fb_webhook, "_read_raw_body", return_value=b"{}"):
			frappe.cache.delete_value(fb_webhook._BLANK_SECRET_LOG_KEY)
			res = fb_webhook.fastbound_event()
			frappe.local.response = frappe._dict()
			again = fb_webhook.fastbound_event()

		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		self.assertEqual(res, {"ok": False, "error": fb_webhook.SIGNATURE_MISMATCH_ERROR})
		self.assertEqual(again, res)
		self.assertEqual(logged.call_count, 1,
			"the operator has to find out somewhere; the caller must not — and an "
			"anonymous caller must not be able to write an Error Log row per request")

	def test_webhook_refuses_a_correctly_signed_event_without_handling_it(self):
		"""Off means off: the SAME request an enabled site accepts is refused with
		the switch off, and no handler runs. "Correctly signed" is proven, not
		asserted — the signature is a real HMAC over the real body with the secret
		the settings hand out, and the enabled leg shows it verifies. A 401 keeps
		FastBound's retry / auto-disable behaviour identical to the 404 this
		replaced (it treats every non-2xx alike), so a still-configured account
		backs off on its own instead of being told its event was consumed."""
		import hashlib
		import hmac
		import json

		from ffl_integrations.fastbound import webhook as fb_webhook

		secret = "switch-off-test-secret"
		body = json.dumps({"eventName": "form4473.completed", "data": {"id": "x"}}).encode()
		ts = "1700000000"
		sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
		headers = {"x-fastbound-signature": f"t={ts},v1={sig}"}
		handler = MagicMock()

		def deliver(enabled: int) -> dict:
			self.set_single(FASTBOUND_SETTINGS, "enabled", enabled)
			frappe.local.response = frappe._dict()
			with patch.dict(fb_webhook._HANDLERS, {"form4473.completed": handler}), \
					patch.object(frappe, "get_request_header",
						side_effect=lambda k, d=None: headers.get(k.lower(), d)), \
					patch.object(fb_webhook, "_read_raw_body", return_value=body), \
					patch.object(fb_webhook, "_get_webhook_secret", return_value=secret), \
					patch.object(fb_webhook.frappe.db, "commit"):
				return fb_webhook.fastbound_event()

		accepted = deliver(1)
		handler.assert_called_once_with({"id": "x"})  # the signature really verifies
		self.assertTrue(accepted["ok"])
		self.assertNotEqual(frappe.local.response.get("http_status_code"), 401)

		handler.reset_mock()
		refused = deliver(0)
		handler.assert_not_called()
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		self.assertEqual(refused, {"ok": False, "error": fb_webhook.SIGNATURE_MISMATCH_ERROR})

	def test_cancel_paths_enqueue_nothing(self):
		"""The cancel/delete family, historically where FastBound goes wrong most
		often, and previously the one part of the guard set with no coverage."""
		from ffl_integrations.fastbound import tasks

		acq = frappe._dict({"name": "FFL-ACQ-C", "purchase_receipt": None,
			"fastbound_id": "FB-1", "serial": None})
		disp = frappe._dict({"name": "FFL-DISP-C", "fastbound_id": "FB-2",
			"disposition_type": "Dealer Transfer"})
		with patch.object(tasks.frappe, "enqueue") as enq:
			tasks.enqueue_delete_on_cancel(acq)
			tasks.cancel_disposition_on_cancel(disp)
			enq.assert_not_called()

	def test_queued_delete_and_disposition_push_do_not_fire(self):
		"""Jobs already in the queue when the operator unchecked the box."""
		from ffl_integrations.fastbound import tasks

		with patch.object(tasks, "FastBoundClient") as client_cls, \
				patch.object(tasks.frappe, "get_doc") as get_doc:
			tasks.delete_acquisition_from_fastbound("FFL-ACQ-Q", "FB-1")
			tasks.delete_disposition_from_fastbound("FFL-DISP-Q", "FB-2")
			tasks.push_disposition_to_fastbound("FFL-DISP-Q")
			client_cls.from_settings.assert_not_called()
			get_doc.assert_not_called()

	def test_boot_tells_the_pos_bundle(self):
		from ffl_integrations.boot import extend_bootinfo

		boot = frappe._dict()
		extend_bootinfo(boot)
		self.assertEqual(boot.ffl_fastbound_enabled, 0)


class SwitchDefaultsPatch(SettingsSandbox, FrappeTestCase):
	"""The migrate patch that makes the FORM tell the truth on an existing site.

	Reads already survive without it (``single_flag`` honours the docfield default
	for a missing row), but the form does not: Frappe only falls back to docfield
	defaults when a Single has NO saved fields at all, so an operator would open
	FastBound Settings, see "Enable FastBound" unticked while the integration was
	demonstrably running, and one absent-minded Save would switch it off for real.
	This patch is the only thing standing between that, and its "never flip a
	deliberate choice back" invariant had no assertion at all.
	"""

	def _run_patch(self):
		from ffl_integrations.patches import (
			finish_integration_switch_defaults,
			set_integration_switch_defaults,
		)

		set_integration_switch_defaults.execute()
		finish_integration_switch_defaults.execute()

	def test_writes_the_on_default_when_the_row_is_missing(self):
		from ffl_core.tests.settings_sandbox import read_raw_single

		self.clear_single(PAYROC_SETTINGS, "enabled")
		self.clear_single(FASTBOUND_SETTINGS, "enabled")
		self._run_patch()
		self.assertEqual(read_raw_single(PAYROC_SETTINGS, "enabled"), "1")
		self.assertEqual(read_raw_single(FASTBOUND_SETTINGS, "enabled"), "1")

	def test_carries_the_renamed_paper_mode_value_across(self):
		"""A dev site that ran an earlier build of this branch has the OLD field.
		Renaming it must not quietly reset the operator's setting."""
		from ffl_core.tests.settings_sandbox import read_raw_single

		self.set_single("FFL Settings", "manual_4473_mode", 1)
		self.clear_single("FFL Settings", "paper_4473_mode")
		self._run_patch()
		self.assertEqual(read_raw_single("FFL Settings", "paper_4473_mode"), "1")
		self.assertIsNone(read_raw_single("FFL Settings", "manual_4473_mode"),
			"the old row must not linger to confuse the next reader")

	def test_never_flips_a_deliberately_disabled_integration_back_on(self):
		"""The invariant that matters: re-running the patch (a second migrate, a
		restore, a re-install) must not undo an operator's decision."""
		from ffl_core.tests.settings_sandbox import read_raw_single

		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(FASTBOUND_SETTINGS, "enabled", 0)
		self._run_patch()
		self.assertEqual(read_raw_single(PAYROC_SETTINGS, "enabled"), "0")
		self.assertEqual(read_raw_single(FASTBOUND_SETTINGS, "enabled"), "0")
		self.assertFalse(payroc_enabled())
		self.assertFalse(fastbound_enabled())

	def test_is_idempotent(self):
		from ffl_core.tests.settings_sandbox import read_raw_single

		self.clear_single(PAYROC_SETTINGS, "enabled")
		self._run_patch()
		self._run_patch()
		self.assertEqual(read_raw_single(PAYROC_SETTINGS, "enabled"), "1")


class CardReferenceGate(SettingsSandbox, FrappeTestCase):
	"""Standalone-terminal reconciliation aid: with no gateway record of the swipe,
	the only way to tie a sale to the terminal's own batch report is a reference the
	cashier types. Same shape as the Zelle Transaction Number gate — submit-time,
	consolidated rollups exempt — plus the two exemptions the audits demanded
	(in-flight gateway sales; returns of gateway-charged sales)."""

	def setUp(self):
		super().setUp()
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 1)

	def _doc(self, *, amount=100.0, reference="", docstatus=1, consolidated=0,
			mode="Credit Card", **extra):
		return frappe._dict({
			"doctype": "POS Invoice",
			"docstatus": docstatus,
			"is_consolidated": consolidated,
			"payments": [frappe._dict({
				"mode_of_payment": mode, "amount": amount, "reference_no": reference})],
			**extra,
		})

	def _run(self, doc):
		from ffl_integrations.payments.api import require_card_reference_on_manual_tender

		require_card_reference_on_manual_tender(doc)

	def test_blocks_a_card_tender_with_no_reference(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(self._doc(reference="  "))

	def test_allows_a_card_tender_carrying_a_reference(self):
		self._run(self._doc(reference="APPR 004512"))

	def test_draft_saves_are_never_blocked(self):
		"""Submit-time only, like every other tender gate: the cashier types the
		reference at Complete Order, and blocking the draft save would strand a
		half-built cart."""
		self._run(self._doc(reference="", docstatus=0))

	def test_consolidated_rollup_is_exempt(self):
		"""POS Invoice Merge Log sums payments by (account, mode) and drops the
		per-invoice reference_no; the source invoices were already gated."""
		self._run(self._doc(reference="", consolidated=1))

	def test_refunds_are_blocked_too(self):
		"""Refunds ARE gated here, unlike Zelle's.

		A negative card row reverses revenue AND sales tax. On a standalone terminal
		nothing else in this system shows the money went back — no gateway record,
		no lock, no reference — so an unreferenced card refund is a booked reversal
		with zero evidence behind it.
		"""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(self._doc(amount=-100.0, reference=""))
		self.assertIn("approval code", str(ctx.exception).lower())

	def test_a_referenced_refund_passes(self):
		self._run(self._doc(amount=-100.0, reference="APPR 004512"))

	def test_zelle_refunds_stay_unblocked(self):
		"""The shared helper's default must not have moved under the Zelle gate:
		a Zelle refund is the store pushing money back by its own action, with a
		bank record of its own, and was never gated."""
		from ffl_core.compliance.payment_mode import require_zelle_transaction_no

		require_zelle_transaction_no(self._doc(amount=-100.0, reference="", mode="Zelle"))

	def test_other_tenders_are_untouched(self):
		self._run(self._doc(mode="Cash", reference=""))

	def test_a_pan_shaped_reference_is_refused(self):
		"""Typing the card NUMBER where the approval code goes would put
		cardholder data into the GL — refused at the shared gate."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(self._doc(reference="4111 1111 1111 1111"))
		self.assertIn("card number", str(ctx.exception).lower())

	def test_an_in_flight_gateway_sale_is_exempt(self):
		"""Charged through the gateway BEFORE the switch flipped: the reference is
		already a money fact on the document and no terminal receipt exists —
		demanding one would strand every order caught mid-transition."""
		self._run(self._doc(reference="", card_unique_reference="UR-123"))
		self._run(self._doc(reference="", card_charges=[
			frappe._dict({"status": "CHARGED", "amount": 100.0})]))

	def test_a_return_of_a_gateway_charged_sale_is_exempt(self):
		"""Its money goes back through the Payroc portal, evidenced by the
		manager's confirmation — there is no terminal refund receipt to type. An
		ordinary standalone return (original never gateway-charged) stays gated."""
		from ffl_integrations.payments import api

		doc = self._doc(amount=-100.0, reference="", is_return=1, name="RET-GW-1")
		with patch.object(api, "_original_gateway_charged", return_value="AUTH-9"):
			self._run(doc)   # exempt — must not raise
		with patch.object(api, "_original_gateway_charged", return_value=""):
			with self.assertRaises(frappe.ValidationError):
				self._run(doc)

	def test_a_real_return_document_is_gated_and_clearable(self):
		"""Same rule, but on a REAL POS Invoice rather than a hand-rolled dict.

		The dict tests pin the predicate; this one pins that a genuine return
		document — real ``Sales Invoice Payment`` child rows, built the way ERPNext
		builds them — both trips the gate and can be cleared by writing the approval
		code onto the row. That second half matters: ERPNext's ``make_return_doc``
		copies mode/amount/account but NOT ``reference_no``, so every card return
		starts out unreferenced and something has to be able to fill it in.

		The POS *client* half (does the counter actually get asked?) is browser
		behaviour and belongs to QA.
		"""
		from ffl_integrations.payments.api import require_card_reference_on_manual_tender

		doc = frappe.new_doc("POS Invoice")
		doc.is_return = 1
		doc.append("payments", {"mode_of_payment": "Credit Card", "amount": -100.0})
		doc.docstatus = 1

		with self.assertRaises(frappe.ValidationError):
			require_card_reference_on_manual_tender(doc)

		doc.payments[0].reference_no = "APPR 771203"
		require_card_reference_on_manual_tender(doc)   # must not raise

	def test_inert_when_the_requirement_is_off(self):
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 0)
		self._run(self._doc(reference=""))

	def test_inert_when_payroc_is_live(self):
		"""The gateway supplies the approval code; asking staff to type one would be
		busywork with a worse source of truth."""
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self._run(self._doc(reference=""))


class BlockUnconfirmedGatewayRefundHook(SettingsSandbox, FrappeTestCase):
	"""The desk-submit side of the refund guard: refund_pos_invoice only runs for
	the POS client, so submit itself must hold the same line."""

	def setUp(self):
		super().setUp()
		self.set_single(PAYROC_SETTINGS, "enabled", 0)

	def _doc(self, **extra):
		return frappe._dict({"doctype": "POS Invoice", "docstatus": 1,
			"is_return": 1, "name": "RET-HOOK-1", **extra})

	def _run(self, doc):
		from ffl_integrations.payments.api import block_unconfirmed_gateway_refund

		block_unconfirmed_gateway_refund(doc)

	def test_blocks_an_unconfirmed_gateway_return_at_submit(self):
		from ffl_integrations.payments import api

		with patch.object(api, "_original_gateway_charged", return_value="AUTH-1"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._run(self._doc())
		self.assertIn("Confirm Portal Refund", str(ctx.exception))

	def test_a_confirmed_return_submits(self):
		from ffl_integrations.payments import api

		with patch.object(api, "_original_gateway_charged", return_value="AUTH-1"):
			self._run(self._doc(card_oob_refund_reference="PORTAL-RF-9"))

	def test_ordinary_returns_and_sales_are_untouched(self):
		from ffl_integrations.payments import api

		with patch.object(api, "_original_gateway_charged", return_value=""):
			self._run(self._doc())                       # never gateway-charged
		self._run(self._doc(is_return=0))                # forward sale
		self._run(self._doc(docstatus=0))                # draft

	def test_inert_while_payroc_is_live(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self._run(self._doc())


class OutOfBandRefundConfirmation(SettingsSandbox, FrappeTestCase):
	"""confirm_out_of_band_refund — the manager exit itself."""

	def setUp(self):
		super().setUp()
		self.set_single(PAYROC_SETTINGS, "enabled", 0)

	def _confirm(self, **kw):
		from ffl_integrations.payments.api import confirm_out_of_band_refund

		args = {"return_invoice_name": "RET-OOB-1",
			"portal_reference": "PORTAL-RF-42", "reason": "customer refunded 8/18"}
		args.update(kw)
		return confirm_out_of_band_refund(**args)

	def test_reference_and_reason_are_mandatory(self):
		with self.assertRaises(frappe.ValidationError):
			self._confirm(portal_reference="  ")
		with self.assertRaises(frappe.ValidationError):
			self._confirm(reason="")

	def test_a_pan_shaped_reference_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._confirm(portal_reference="4111111111111111")
		self.assertIn("card number", str(ctx.exception).lower())

	def test_refused_while_payroc_is_live(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._confirm()
		self.assertIn("switched on", str(ctx.exception))

	def test_happy_path_writes_field_and_timeline(self):
		from ffl_integrations.payments import api

		meta = frappe._dict({"is_return": 1, "docstatus": 0,
			"card_oob_refund_reference": ""})

		def _get_value(doctype, name, field=None, *a, **k):
			if isinstance(field, (list, tuple)):
				return meta
			return {"return_against": "PSINV-ORIG",
				"card_unique_reference": "AUTH-123"}.get(field)

		with patch.object(api.frappe.db, "exists", return_value=True), \
				patch.object(api.frappe.db, "get_value", side_effect=_get_value), \
				patch.object(api, "_charge_rows", return_value=[]), \
				patch.object(api.frappe, "get_doc") as get_doc, \
				patch.object(api.frappe.db, "set_value") as set_value:
			out = self._confirm()
		self.assertEqual(out["ok"], True)
		self.assertEqual(out["portal_reference"], "PORTAL-RF-42")
		get_doc.return_value.add_comment.assert_called_once()
		note = get_doc.return_value.add_comment.call_args.args[1]
		self.assertIn("PORTAL-RF-42", note)
		self.assertIn("AUTH-123", note)
		set_value.assert_called_once()
		self.assertEqual(set_value.call_args.args[:4], ("POS Invoice", "RET-OOB-1",
			"card_oob_refund_reference", "PORTAL-RF-42"))

	def test_double_confirmation_is_refused(self):
		from ffl_integrations.payments import api

		meta = frappe._dict({"is_return": 1, "docstatus": 0,
			"card_oob_refund_reference": "PORTAL-RF-1"})
		with patch.object(api.frappe.db, "get_value", return_value=meta):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._confirm()
		self.assertIn("already", str(ctx.exception))

	def test_never_charged_original_is_refused(self):
		from ffl_integrations.payments import api

		meta = frappe._dict({"is_return": 1, "docstatus": 0,
			"card_oob_refund_reference": ""})

		def _get_value(doctype, name, field=None, *a, **k):
			if isinstance(field, (list, tuple)):
				return meta
			return {"return_against": "PSINV-ORIG2"}.get(field)

		with patch.object(api.frappe.db, "exists", return_value=True), \
				patch.object(api.frappe.db, "get_value", side_effect=_get_value), \
				patch.object(api, "_charge_rows", return_value=[]):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._confirm()
		self.assertIn("never charged", str(ctx.exception))

	def test_not_a_return_is_refused(self):
		from ffl_integrations.payments import api

		meta = frappe._dict({"is_return": 0, "docstatus": 1,
			"card_oob_refund_reference": ""})
		with patch.object(api.frappe.db, "get_value", return_value=meta):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._confirm()
		self.assertIn("not a return", str(ctx.exception))


class HandAsserted4473Report(FrappeTestCase):
	def test_report_executes(self):
		"""Smoke: the review surface for hand-asserted dispositions must at least
		run — a report that crashes is a review that never happens."""
		from ffl_core.ffl_core.report.hand_asserted_4473_dispositions import (
			hand_asserted_4473_dispositions as report,
		)

		columns, rows = report.execute({})
		self.assertTrue(columns)
		for row in rows:
			self.assertEqual(int(row.get("manual_override", 1) or 1), 1)
