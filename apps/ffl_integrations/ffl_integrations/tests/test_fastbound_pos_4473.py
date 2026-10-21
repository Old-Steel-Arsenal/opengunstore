"""Tests for the POS 4473 / Disposition flow + the FastBound webhook receiver."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.fastbound.client import FastBoundClient
from ffl_integrations.fastbound import webhook as fb_webhook


WEBHOOK_SECRET = "test-webhook-secret"


def _fb_config(**over):
	"""The FastBound config every test fixture in this file builds on.

	`base_url` is deliberately NOT cloud.fastbound.com: client.py fails closed on a
	blank base_url precisely because defaulting to the live bound book once made a
	dev site post real acquisitions. Keep that address out of test fixtures
	entirely — this is the single place the rule has to hold.
	"""
	from ffl_integrations.fastbound.client import FastBoundConfig

	fields = {
		"account_num": "TEST-ACC", "api_key": "key-x",
		"base_url": "https://example.invalid", "api_version": "1.0",
		"audit_user": "qa@example.com", "timeout": 10,
	}
	fields.update(over)
	return FastBoundConfig(**fields)


def _items_client(pages: list[dict]):
	"""A FastBoundClient whose HTTP session replays `pages`, one per GET.

	The seam is deliberately ``session.request`` — the LOWEST layer — so the
	client's own paging contract and the webhook's client-side dispositionId
	re-check both execute for real. Mocking any higher (e.g. patching
	``_fetch_disposition_serials``) is what let the multi-gun coverage bug ship:
	the tests handed back a serial list the production code could never have
	obtained. Returns (client, session) so callers can assert on the query params.
	"""
	session = MagicMock()
	if pages:
		responses = []
		for body in pages:
			resp = MagicMock()
			resp.status_code = 200
			resp.content = b"{}"
			resp.json.return_value = body
			responses.append(resp)
		session.request.side_effect = responses
	# No pages → leave `session.request` as a bare MagicMock so callers can program
	# it themselves (a canned empty side_effect would raise StopIteration on the
	# first call instead).
	return FastBoundClient(_fb_config(), session=session), session


def _signed_request(body: bytes, *, secret: str = WEBHOOK_SECRET, ts: str | None = None):
	"""Mutate frappe.local request to look like a signed FastBound POST.

	Returns the (ts, sig_hex) so tests can assert against it.
	"""
	ts = ts or str(int(time.time()))
	canonical = f"{ts}.".encode("utf-8") + body
	sig = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
	# frappe.request is a werkzeug Request; for tests we monkey-patch the
	# helpers the webhook reads from.
	frappe.local.request = type("MockReq", (), {"get_data": lambda self: body})()
	frappe.local.request_ip = "127.0.0.1"
	# A real request's flags start with currently_saving=[] (frappe.init); the
	# handler saves documents (Form 4473 Log), which need it.
	frappe.local.flags = frappe._dict(currently_saving=[])
	# Header lookup uses frappe.get_request_header → reads from request.headers
	# We can't easily set that, so monkey-patch the function for the test.
	return ts, sig


class FBPosWebhook(SettingsSandbox, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		# Ensure webhook_secret + minimal creds on FastBound Settings.
		s = frappe.get_single("FastBound Settings")
		if not s.account_num:
			s.account_num = "TEST-ACC"
		if not s.get_password("api_key", raise_exception=False):
			s.api_key = "test-api-key"
		s.webhook_secret = WEBHOOK_SECRET
		s.flags.ignore_permissions = True
		s.save()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		# log_error inside our webhook handler needs frappe.flags.currently_saving
		# to exist. In the bench test runner it's None by default — initialise.
		if getattr(frappe.flags, "currently_saving", None) is None:
			frappe.flags.currently_saving = []
		# Reset the response object between tests.
		frappe.local.response = frappe._dict()
		# The receiver refuses with 401 when the integration is switched off, so
		# these tests must state that it is ON rather than inherit the site's
		# setting — otherwise the suite goes red on exactly the configuration this
		# feature exists to support, and CI's always-enabled fresh site never shows
		# it.
		self.set_single("FastBound Settings", "enabled", 1)

	def _patch_header(self, sig_header: str | None):
		"""Patch frappe.get_request_header so the webhook reads our sig."""
		def _get(key, default=None):
			if key.lower() == "x-fastbound-signature":
				return sig_header
			return default
		return patch.object(frappe, "get_request_header", side_effect=_get)

	def test_signature_mismatch_returns_401(self):
		body = json.dumps({"eventName": "form4473.completed", "data": {"dispositionId": "X"}}).encode()
		ts, _good = _signed_request(body)
		bad_sig = "0" * 64
		with self._patch_header(f"t={ts},v1={bad_sig}"):
			res = fb_webhook.fastbound_event()
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)

	def test_missing_header_returns_401(self):
		body = b'{"eventName":"x"}'
		_signed_request(body)
		with self._patch_header(None):
			res = fb_webhook.fastbound_event()
		self.assertFalse(res["ok"])
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)

	def _seed_disposition(self, *, fb_id: str, form_status: str, nics_status: str):
		"""Seed an FFL Disposition row pointing at a fake FastBound id.

		Cleans up any prior dispositions with the same fb_id so re-runs are
		safe. We avoid faking POS Invoice rows entirely — invoice-flip
		behavior is covered by the end-to-end Chrome MCP test (where we
		drive the real POS path) instead of unit-faking POS Invoice's many
		mandatory fields.
		"""
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		# Tear down any leftovers from a previous failed run.
		frappe.db.delete("FFL Disposition", {"fastbound_id": fb_id})
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": frappe.utils.now_datetime(),
			"disposition_type": "Retail Sale",
			"company": company,
			"customer": customer,
			"item": _ensure_test_firearm_item(),
			"serial": "MFG-TESTSER",
			"fastbound_id": fb_id,
			"form_4473_status": form_status,
			"nics_status": nics_status,
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		frappe.db.commit()
		return disp

	def test_form4473_completed_handler_dispatches(self):
		# Real FB Form4473CompletedEvent shape: disposition nested, items carry
		# serials, PDF link is apiDownloadUrl. Without a POS Invoice fixture the
		# handler logs + acks (ok=true) when no invoice matches.
		body = json.dumps({
			"id": "evt-1",
			"eventName": "form4473.completed",
			"accountNumber": "TEST-ACC",
			"data": {
				"id": "FORM4473-X",
				"disposition": {"id": "FB-DISP-FORM4473"},
				"items": [{"serial": "MFG-TESTSER"}],
				"apiDownloadUrl": "https://example.com/4473.pdf",
			},
		}).encode()
		ts, sig = _signed_request(body)
		with self._patch_header(f"t={ts},v1={sig}"):
			res = fb_webhook.fastbound_event()
		self.assertTrue(res.get("ok"), res)
		self.assertEqual(res.get("event"), "form4473.completed")

	def test_form4473_uses_nested_disposition_id_not_top_level(self):
		# Regression: top-level `id` is the 4473 id; the disposition id is nested.
		# The handler must query the nested disposition.id.
		body = json.dumps({
			"eventName": "form4473.completed",
			"data": {
				"id": "FORM4473-ID",
				"disposition": {"id": "DISP-REAL-ID"},
				"apiDownloadUrl": "https://example.com/dl",
			},
		}).encode()
		ts, sig = _signed_request(body)
		with patch.object(fb_webhook, "_fetch_disposition_serials", return_value=[]) as fetch:
			with self._patch_header(f"t={ts},v1={sig}"):
				res = fb_webhook.fastbound_event()
		self.assertTrue(res.get("ok"), res)
		# form4473.completed is a 4473 event → the fetch is marked is_4473 (skips the
		# 404-ing GET); the nested disposition id is still what's queried.
		fetch.assert_called_once_with("DISP-REAL-ID", is_4473=True)

	def test_form4473_fetches_authoritative_serials_for_coverage(self):
		# After the multi-gun coverage fix, the handler now fetches the authoritative
		# serial list from FastBound to cover ALL firearms — not just the ones that
		# happen to be in the payload. Confirm the fetch IS called for coverage when
		# is_proceed is True (response=2) even though the payload carries items.
		body = json.dumps({
			"eventName": "form4473.completed",
			"data": {
				"disposition": {"id": "DISP-2"},
				"items": [{"serial": "SER-1"}, {"item": {"serial": "SER-2"}}],
				"response": 2,  # Proceed
			},
		}).encode()
		ts, sig = _signed_request(body)
		# Patch _find_draft_invoices_for_serials to return a non-empty match so the
		# handler doesn't short-circuit before the coverage block.
		# Patch fetch to return the authoritative list (simulates the server call).
		# Patch _ensure_disposition_coverage to avoid real DB writes in this unit test.
		# The stub answers the fake invoice's reads only (docs/testing.md rule 4): a blanket
		# return_value also answered frappe's own Workflow lookup for Error Log, and the "N/A"
		# it cached in redis broke every later log_error in the process.
		real_get_value = frappe.db.get_value

		def _get_value(doctype, *args, **kwargs):
			if doctype == "POS Invoice":
				return "N/A"
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(fb_webhook, "_find_draft_invoices_for_serials",
				return_value={"FAKE-INV-1": ["SER-1"]}), \
		     patch.object(fb_webhook, "_fetch_disposition_serials",
				return_value=["SER-1", "SER-2"]) as fetch, \
		     patch.object(fb_webhook, "_ensure_disposition_coverage"), \
		     patch.object(fb_webhook.frappe.db, "get_value", side_effect=_get_value), \
		     patch.object(fb_webhook.frappe.db, "set_value"), \
		     patch.object(fb_webhook.frappe, "publish_realtime"):
			with self._patch_header(f"t={ts},v1={sig}"):
				res = fb_webhook.fastbound_event()
		self.assertTrue(res.get("ok"), res)
		fetch.assert_called_once_with("DISP-2", is_4473=True)

	def test_pdf_url_prefers_apiDownloadUrl(self):
		self.assertEqual(
			fb_webhook._pdf_url_from_event({"apiDownloadUrl": "u1", "pdfUrl": "u2"}), "u1")
		self.assertEqual(fb_webhook._pdf_url_from_event({"pdfUrl": "u2"}), "u2")
		self.assertIsNone(fb_webhook._pdf_url_from_event({}))

	def test_disposition_committed_marks_nics_proceed(self):
		fb_id = "FB-DISP-COMMITTED"
		disp = self._seed_disposition(
			fb_id=fb_id, form_status="Completed", nics_status="N/A",
		)
		body = json.dumps({
			"id": "evt-2",
			"eventName": "disposition.committed",
			"data": {"dispositionId": fb_id},
		}).encode()
		ts, sig = _signed_request(body)
		with self._patch_header(f"t={ts},v1={sig}"):
			res = fb_webhook.fastbound_event()

		self.assertTrue(res.get("ok"), res)
		self.assertEqual(frappe.db.get_value("FFL Disposition", disp.name, "nics_status"),
			"Proceed")

	def test_completed_proceed_retail_sale_commits_to_bound_book(self):
		"""A completed 4473 + NICS Proceed is a finished, legal sale, so its Retail
		Sale disposition must commit (docstatus=1) — not linger in Draft. Submitting
		books nothing extra: Retail Sale skips the stock-out (the POS Invoice already
		moved the gun) and FastBound isn't re-pushed (fastbound_id is set)."""
		# A real sold gun has a Serial No on file (submit verifies it exists).
		if not frappe.db.exists("Serial No", "MFG-TESTSER"):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": "MFG-TESTSER",
				"item_code": _ensure_test_firearm_item(),
			}).insert(ignore_permissions=True)
		disp = self._seed_disposition(
			fb_id="FB-SUBMIT-OK", form_status="Completed", nics_status="Proceed",
		)
		self.assertEqual(disp.docstatus, 0)  # seeded as Draft
		disp.submit()
		disp.reload()
		self.assertEqual(disp.docstatus, 1)  # committed to the bound book
		self.assertFalse(disp.get("stock_entry"))  # Retail Sale issues no stock here
		frappe.db.delete("FFL Disposition", {"fastbound_id": "FB-SUBMIT-OK"})

	def test_unknown_event_acked_but_unhandled(self):
		body = json.dumps({"eventName": "item.imported", "data": {}}).encode()
		ts, sig = _signed_request(body)
		with self._patch_header(f"t={ts},v1={sig}"):
			res = fb_webhook.fastbound_event()
		self.assertTrue(res.get("ok"))
		self.assertFalse(res.get("handled"))


class FBClientDisposition(FrappeTestCase):
	"""Pure-mock tests of the new disposition client methods. No DB needed."""

	def _make_client(self):
		"""Same fixture as _items_client, with no canned pages — these tests set
		`session.request` themselves."""
		return _items_client([])

	def test_create_pending_disposition_omits_type_for_4473(self):
		# Per FastBound integration guide: "a null disposition type indicates
		# that the disposition is related to a 4473." Default call must NOT
		# send a `type` field, otherwise FastBound treats it as B2B.
		client, session = self._make_client()
		response = type("R", (), {})()
		response.status_code = 201
		response.content = b'{"id":"FB-DISP-123"}'
		response.headers = {}
		response.json = lambda: {"id": "FB-DISP-123"}
		session.request.return_value = response

		out = client.create_pending_disposition(external_id="POS-001")
		self.assertEqual(out, "FB-DISP-123")
		call = session.request.call_args
		self.assertEqual(call.args[0], "POST")
		self.assertIn("/TEST-ACC/api/Dispositions", call.args[1])
		body = call.kwargs["json"]
		self.assertNotIn("type", body)
		self.assertEqual(body["externalId"], "POS-001")

	def test_attach_contact_to_disposition_PUTs(self):
		client, session = self._make_client()
		response = type("R", (), {})()
		response.status_code = 204
		response.content = b""
		response.headers = {}
		response.json = lambda: None
		session.request.return_value = response

		client.attach_contact_to_disposition(
			disposition_id="DISP-1", contact_id="C-1",
		)
		call = session.request.call_args
		self.assertEqual(call.args[0], "PUT")
		self.assertIn("/Dispositions/DISP-1/AttachContact/C-1", call.args[1])

	def test_create_pending_disposition_uses_location_header_when_body_empty(self):
		"""Real FastBound returns 201 + Content-Length:0 + Location header
		on POST /Dispositions. _request() must extract the id from Location."""
		client, session = self._make_client()
		response = type("R", (), {})()
		response.status_code = 201
		response.content = b""
		response.headers = {"Location": "https://cloud.fastbound.com/TEST-ACC/api/Dispositions/abc-uuid-1"}
		response.json = lambda: None
		session.request.return_value = response
		out = client.create_pending_disposition(external_id="POS-X")
		self.assertEqual(out, "abc-uuid-1")

	def test_find_or_create_contact_creates_on_miss(self):
		client, session = self._make_client()
		# First call: GetByExternalId → 404
		not_found = type("R", (), {})()
		not_found.status_code = 404
		not_found.content = b"Not Found"
		not_found.text = "Not Found"
		not_found.headers = {}
		not_found.json = lambda: {}
		# Second call: POST /Contacts → 201 with empty body + Location header
		# (FastBound's actual response shape for creates).
		created = type("R", (), {})()
		created.status_code = 201
		created.content = b""
		created.headers = {"Location": "https://cloud.fastbound.com/X/api/Contacts/NEW-CID"}
		created.json = lambda: None
		session.request.side_effect = [not_found, created]

		cid = client.find_or_create_contact(
			external_id="EXT-1",
			create_payload={"firstName": "Jane", "lastName": "Roe"},
		)
		self.assertEqual(cid, "NEW-CID")
		# Second call should be POST /Contacts with externalId stamped.
		post_call = session.request.call_args_list[1]
		self.assertEqual(post_call.args[0], "POST")
		self.assertIn("/Contacts", post_call.args[1])
		self.assertEqual(post_call.kwargs["json"]["externalId"], "EXT-1")


class FB4473Response(FrappeTestCase):
	"""Pure mapping: FastBound 4473 NICS response → (nics_status, is_proceed)."""

	def test_response_mapping(self):
		from ffl_integrations.fastbound import webhook as wh
		self.assertEqual(wh._nics_from_4473_event({"response": 2}), ("Proceed", True))
		self.assertEqual(wh._nics_from_4473_event({"response": 7}), ("Proceed", True))    # DelayedProceed
		self.assertEqual(wh._nics_from_4473_event({"response": 3}), ("Denied", False))
		self.assertEqual(wh._nics_from_4473_event({"response": 4}), ("Cancelled", False))
		self.assertEqual(wh._nics_from_4473_event({"response": 10}), ("Delayed", False))  # DelayedNoResponse
		self.assertEqual(wh._nics_from_4473_event({"status": 3}), ("Cancelled", False))   # Terminated / Void
		self.assertEqual(wh._nics_from_4473_event({"responseText": "Denied"}), ("Denied", False))
		self.assertEqual(wh._nics_from_4473_event({"responseText": "Proceed"}), ("Proceed", True))
		self.assertEqual(wh._nics_from_4473_event({}), ("Delayed", False))  # missing → fail-safe block


class WebhookLikeEscape(FrappeTestCase):
	"""LIKE-wildcard escaping for the serial prefilter."""

	def test_like_escape_escapes_backslash_percent_underscore(self):
		from ffl_integrations.fastbound.webhook import _like_escape
		# Backslash FIRST, then % and _ — each becomes a literal in a LIKE pattern.
		self.assertEqual(_like_escape("A\\B%C_D"), "A\\\\B\\%C\\_D")

	def test_like_escape_leaves_plain_serial_untouched(self):
		from ffl_integrations.fastbound.webhook import _like_escape
		self.assertEqual(_like_escape("J4940"), "J4940")


class FB4473Gate(FrappeTestCase):
	"""Per-firearm 4473 coverage gate (require_4473_and_nics_for_firearm_sale).
	The FFL Disposition lookup is mocked so we don't need a real POS Invoice +
	disposition fixture (linked_pos_invoice is a Link field)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

	def _doc(self, serial):
		# SimpleNamespace, not frappe._dict: a dict's `.items` is the dict method,
		# which would shadow the items child-table the validator iterates.
		return SimpleNamespace(
			doctype="POS Invoice", name="POSINV-GATE", docstatus=1,
			form_4473_status="Completed", nics_status="Proceed", is_consolidated=0,
			items=[frappe._dict(item_code=_ensure_test_firearm_item(), serial_no=serial, idx=1)],
		)

	def _run(self, doc, covered_serials):
		from ffl_core.compliance import firearm as compliance
		orig = frappe.get_all
		def fake(doctype, *a, **k):
			return list(covered_serials) if doctype == "FFL Disposition" else orig(doctype, *a, **k)
		with patch.object(frappe, "get_all", side_effect=fake):
			compliance.require_4473_and_nics_for_firearm_sale(doc)

	def test_blocks_uncovered_firearm(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(self._doc("UNCOVERED-SER"), covered_serials=[])

	def test_passes_when_serial_is_covered(self):
		self._run(self._doc("COVERED-SER"), covered_serials=["COVERED-SER"])  # must not raise


# Fixture helper.
def _ensure_test_firearm_item() -> str:
	code = "FB-POS-TESTFIREARM"
	if frappe.db.exists("Item", code):
		return code
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": "Test Firearm",
		"item_group": "Rifle", "stock_uom": "Nos",
		"is_stock_item": 1, "has_serial_no": 0,
		"firearm_serial": "MFG-TESTSER",
	}).insert(ignore_permissions=True)
	return code


# ---------------------------------------------------------------------------
# Multi-gun 4473 coverage tests
# ---------------------------------------------------------------------------

MULTIGUN_ITEM = "MG4473-GUN"
MULTIGUN_CUSTOMER = "_Test MG4473 Buyer"


def _ensure_multigun_item() -> str:
	"""Serialized firearm item for multi-gun 4473 tests."""
	if frappe.db.exists("Item", MULTIGUN_ITEM):
		return MULTIGUN_ITEM
	frappe.get_doc({
		"doctype": "Item", "item_code": MULTIGUN_ITEM, "item_name": MULTIGUN_ITEM,
		"item_group": "Firearm", "stock_uom": "Nos",
		"is_stock_item": 1, "has_serial_no": 1,
	}).insert(ignore_permissions=True)
	return MULTIGUN_ITEM


def _stock_serial(serial: str, item_code: str, company: str, warehouse: str) -> None:
	"""Material Receipt one serialized firearm into stock (idempotent).

	⚠️ KNOWN GAP (DEBT-1, pre-existing) — this guard does not survive a test that
	MOVES the gun. The manager-override flow parks it in Customer Custody, so on the
	next run status is no longer Active, the guard falls through to a Material
	Receipt, and that dies with

	    SerialNoDuplicateError: Serial No MG-OV-A is already present in the
	    warehouse Customer Custody - FT

	leaving test_manager_override_books_audited_disposition_and_unblocks_gate red on
	any warm site (CI's fresh site is unaffected). A compliance module that is
	permanently red teaches people to stop reading its red, so this is worth fixing —
	but NOT here: the obvious repair (transfer it back from
	`Serial No.warehouse`) does not work, because that field does not reliably name
	where the stock actually sits. Attempting it turned 1 error into 6
	(`NegativeStockError ... Goods In Transit - FT`), i.e. the gun can be parked
	in-transit or already issued. A correct fix has to read the real ledger position
	and handle the issued case, which is its own change with its own tests.
	Tracked as a test-hygiene follow-up.
	"""
	from ffl_core.firearm import serialized_receipt_row
	if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
			"Serial No", serial, "status") == "Active":
		return
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": company,
		"items": [serialized_receipt_row(
			item_code=item_code, qty=1, warehouse=warehouse,
			basic_rate=500, serial=serial)],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()


class FBMultiGunCoverage(SettingsSandbox, FrappeTestCase):
	"""Multi-gun 4473 webhook coverage — regression tests for the partial-payload
	and lockout bugs.

	Fixture: a 2-firearm Draft POS Invoice (serials MG-SER-A and MG-SER-B) on
	the FFL Test POS profile. Tests call the webhook handlers directly to avoid
	HMAC plumbing complexity.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		# The Dealer Transfer seed (test_disposition_committed_non_retail…)
		# links destination_ffl to this record; a standalone run of this app
		# can't rely on ffl_core's tests having created it.
		from ffl_integrations.tests.site_fixtures import ensure_atf_ffl_record
		ensure_atf_ffl_record("1-99-888-01-2D-09999")
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 0}, "name")

		# Customer
		if not frappe.db.exists("Customer", MULTIGUN_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": MULTIGUN_CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

		# POS Opening Entry (needed for POS Invoice save/validate)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)

		# Firearm item
		_ensure_multigun_item()

		# Ensure FastBound Settings has a webhook_secret (for other tests that
		# might share setUpClass results).
		s = frappe.get_single("FastBound Settings")
		if not s.get_password("webhook_secret", raise_exception=False):
			s.webhook_secret = WEBHOOK_SECRET
			s.flags.ignore_permissions = True
			s.save()

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		if getattr(frappe.flags, "currently_saving", None) is None:
			frappe.flags.currently_saving = []
		frappe.local.response = frappe._dict()
		# These tests exercise the ELECTRONIC 4473 flow, so pin the switch on rather
		# than inherit whatever the site happens to be set to. Without this, a store
		# that has actually switched FastBound off — the very configuration this
		# feature exists to support — cannot run `make test` green on its own site,
		# and CI's fresh site (always enabled) would never show it. Tests that want
		# the switch off set it in the test body, which runs after setUp.
		self.set_single("FastBound Settings", "enabled", 1)

	def _stock_both(self, ser_a: str, ser_b: str) -> None:
		"""Stock both test serials (idempotent)."""
		_stock_serial(ser_a, MULTIGUN_ITEM, self.company, self.warehouse)
		_stock_serial(ser_b, MULTIGUN_ITEM, self.company, self.warehouse)

	def _draft_invoice_two_guns(self, ser_a: str, ser_b: str,
			form_4473: str = "", nics: str = "") -> str:
		"""Build a Draft POS Invoice with two firearm rows."""
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = MULTIGUN_CUSTOMER
		inv.append("items", {
			"item_code": MULTIGUN_ITEM, "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": ser_a,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1,
		})
		inv.append("items", {
			"item_code": MULTIGUN_ITEM, "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": ser_b,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1,
		})
		inv.set_missing_values()
		if form_4473:
			inv.form_4473_status = form_4473
		if nics:
			inv.nics_status = nics
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _get_retail_sale_dispositions(self, fastbound_id: str, serial: str) -> list:
		return frappe.get_all(
			"FFL Disposition",
			filters={
				"fastbound_id": fastbound_id,
				"serial": serial,
				"disposition_type": "Retail Sale",
			},
			fields=["name", "docstatus", "form_4473_status", "nics_status",
				"linked_pos_invoice"],
		)

	def _cleanup_invoice(self, inv_name: str) -> None:
		"""Delete the draft POS Invoice and any associated dispositions."""
		frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
		frappe.db.delete("POS Invoice Item", {"parent": inv_name})
		frappe.db.delete("POS Invoice", {"name": inv_name})

	# -----------------------------------------------------------------------
	# Test 1 — core regression: partial payload still covers all guns
	# -----------------------------------------------------------------------

	def test_multigun_partial_payload_covers_all_serials(self):
		"""form4473.completed with only serial A in the payload must still create
		Retail Sale dispositions for BOTH A and B when the authoritative list
		(_fetch_disposition_serials) returns [A, B]."""
		ser_a = "MG-T1-A"
		ser_b = "MG-T1-B"
		disp_id = "FB-MG-DISP-T1"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			# Payload carries ONLY serial A; the authoritative fetch returns both.
			data = {
				"id": "FORM4473-T1",
				"disposition": {"id": disp_id},
				"items": [{"serial": ser_a}],   # partial — B is missing
				"response": 2,                   # Proceed
			}
			with patch.object(fb_webhook, "_fetch_disposition_serials",
					return_value=[ser_a, ser_b]):
				fb_webhook._handle_form4473_completed(data)

			# Both guns must have a submitted Retail Sale disposition.
			for ser in (ser_a, ser_b):
				disps = self._get_retail_sale_dispositions(disp_id, ser)
				self.assertEqual(len(disps), 1,
					f"Expected exactly 1 Retail Sale disposition for {ser}, got {len(disps)}")
				d = disps[0]
				self.assertEqual(d.docstatus, 1,
					f"Disposition for {ser} should be submitted (docstatus=1)")
				self.assertEqual(d.form_4473_status, "Completed")
				self.assertEqual(d.nics_status, "Proceed")
				self.assertEqual(d.linked_pos_invoice, inv_name)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	def test_partial_payload_covers_late_added_gun_via_real_lookup(self):
		"""Regression — a past incident.

		Guns were added to the 4473 in two passes (14:27 and 14:36); the
		form4473.completed payload carried only the first one, so the second gun
		(FIT0039) never got a Retail Sale disposition and Complete Order was
		refused by the submit gate — with the card already charged.

		The test above this one covers the same shape but patches
		``_fetch_disposition_serials`` to return the full list, which is precisely
		why it could not catch this: in production that call returned [] for every
		4473, because ``_fetch_disposition_items`` short-circuits on is_4473. Here
		nothing above the HTTP session is mocked, so the authoritative lookup has
		to genuinely resolve the missing gun.
		"""
		ser_a, ser_b = "MG-T71-A", "MG-T71-B"
		disp_id = "FB-MG-DISP-T71"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		client, session = _items_client([
			{"items": [
				{"id": "FBIT-T71-A", "serial": ser_a, "dispositionId": disp_id},
				{"id": "FBIT-T71-B", "serial": ser_b, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		data = {
			"id": "FORM4473-T71",
			"disposition": {"id": disp_id},
			"items": [{"serial": ser_a}],  # partial — B was added in a later pass
			"response": 2,                 # Proceed
		}
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed(data)

			for ser in (ser_a, ser_b):
				disps = self._get_retail_sale_dispositions(disp_id, ser)
				self.assertEqual(len(disps), 1,
					f"{ser} must have exactly 1 Retail Sale disposition; got {len(disps)}")
				self.assertEqual(disps[0].docstatus, 1,
					f"disposition for {ser} must be submitted")
				self.assertEqual(disps[0].nics_status, "Proceed")
				self.assertEqual(disps[0].linked_pos_invoice, inv_name)
			# The lookup must be scoped to this disposition, not a bound-book sweep.
			params = session.request.call_args_list[0].kwargs.get("params") or {}
			self.assertEqual(params.get("dispositionId"), disp_id)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	def test_split_ticket_second_invoice_gets_doc_level_4473_fields(self):
		"""One 4473, two Draft invoices — BOTH must get their doc-level fields.

		The submit gate checks `form_4473_status` on the invoice BEFORE it checks
		per-serial coverage, so an invoice that receives its FFL Disposition but not
		its doc-level fields is still refused — the same counter symptom as
		a past invoice. That happens whenever the invoice match is computed
		from the payload alone: the gun known only to the bound book lives on the
		other ticket, and that ticket is never matched.
		"""
		ser_a, ser_b = "MG-SPLIT-A", "MG-SPLIT-B"
		disp_id = "FB-MG-DISP-SPLIT"
		self._stock_both(ser_a, ser_b)
		inv_a = self._draft_invoice_one_firearm_line(ser_a)
		inv_b = self._draft_invoice_one_firearm_line(ser_b)
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-SPLIT-A", "serial": ser_a, "dispositionId": disp_id},
				{"id": "FBIT-SPLIT-B", "serial": ser_b, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": ser_a}],  # payload knows only invoice A's gun
					"response": 2,
				})

			for inv, ser in ((inv_a, ser_a), (inv_b, ser_b)):
				self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser)), 1,
					f"{ser} must be covered")
				self.assertEqual(
					frappe.db.get_value("POS Invoice", inv, "form_4473_status"), "Completed",
					f"{inv} must get doc-level 4473 fields, else the gate refuses it")
				self.assertEqual(
					frappe.db.get_value("POS Invoice", inv, "nics_status"), "Proceed")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_a)
			self._cleanup_invoice(inv_b)

	def test_short_lookup_is_completed_by_the_payload(self):
		"""The union's reason for existing — and the ONLY shape that falsifies it.

		Every other case has the lookup returning a superset of the payload (or
		nothing at all), where `authoritative or payload` is indistinguishable from
		the union. Here the lookup comes back NON-EMPTY BUT SHORT — FastBound lagging
		on a just-added gun, a half-honoured filter, or a 4473 wider than
		_4473_ITEM_MAX_PAGES — while the payload happens to carry the gun it missed.
		With `or`, the payload's gun is silently dropped: its invoice never matches,
		never gets doc-level fields, never gets a disposition. That is the original
		incident, reproduced by a supposed fix.

		Verified to fail on purpose: swapping `_merge_serials(...)` back to
		`authoritative or payload_serials` turns this test red (the same
		experiment against the whole module found NOTHING that caught it).
		"""
		ser_payload, ser_lookup = "MG-SHORT-P", "MG-SHORT-L"
		disp_id = "FB-MG-DISP-SHORT"
		self._stock_both(ser_payload, ser_lookup)
		inv_payload = self._draft_invoice_one_firearm_line(ser_payload)
		inv_lookup = self._draft_invoice_one_firearm_line(ser_lookup)
		# The bound book knows only about the OTHER gun — it does not echo the
		# payload's one back to us.
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-SHORT-L", "serial": ser_lookup, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": ser_payload}],  # the half the lookup missed
					"response": 2,
				})

			for inv, ser in ((inv_payload, ser_payload), (inv_lookup, ser_lookup)):
				self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser)), 1,
					f"{ser} must be covered — it is known to exactly one of the two "
					"sources, which is the whole point of merging them")
				self.assertEqual(
					frappe.db.get_value("POS Invoice", inv, "form_4473_status"), "Completed",
					f"{inv} must get doc-level fields, else the gate refuses it")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_payload)
			self._cleanup_invoice(inv_lookup)

	def test_committed_backstop_short_lookup_is_completed_by_the_payload(self):
		"""Same falsifying shape for the disposition.committed backstop.

		Its existing coverage all runs with an empty payload, where `fetch or []`
		and the union agree — so that call site had no test with teeth either.
		"""
		ser_seed, ser_payload = "MG-CSHORT-S", "MG-CSHORT-P"
		disp_id = "FB-MG-DISP-CSHORT"
		self._stock_both(ser_seed, ser_payload)
		inv_name = self._draft_invoice_two_guns(ser_seed, ser_payload)
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-CSHORT-S", "serial": ser_seed, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		try:
			# Seed the retail row so the backstop's retail_committed gate opens.
			fb_webhook._ensure_disposition_coverage(disp_id, [ser_seed], "Proceed")
			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser_payload)), 0)

			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client), \
			     patch.object(fb_webhook, "_sync_fb_direct_disposal_stock"):
				fb_webhook._handle_disposition_committed({
					"dispositionId": disp_id,
					"items": [{"serial": ser_payload}],  # only the payload has this one
				})

			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser_payload)), 1,
				"a gun the payload knows and the lookup missed must still be covered")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	def test_other_dispositions_guns_are_neither_minted_nor_stamped(self):
		"""Blast radius of the reordering: `matches` is now driven by the lookup, so
		a stranger's gun leaking through would both mint it a Retail Sale record AND
		stamp its invoice as 4473-Completed — satisfying the gate for a gun that
		never had a 4473. The client-side dispositionId re-check is what prevents
		that; this pins it at the handler level, where the damage would happen."""
		ser_mine, ser_other = "MG-XTALK-MINE", "MG-XTALK-OTHER"
		disp_id = "FB-MG-DISP-XTALK"
		self._stock_both(ser_mine, ser_other)
		inv_mine = self._draft_invoice_one_firearm_line(ser_mine)
		inv_other = self._draft_invoice_one_firearm_line(ser_other)
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-XTALK-MINE", "serial": ser_mine, "dispositionId": disp_id},
				# Same page, different sale — FastBound ignoring the filter looks like this
				{"id": "FBIT-XTALK-OTHER", "serial": ser_other,
					"dispositionId": "FB-SOMEONE-ELSES-4473"},
			]},
			{"items": []},
		])
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": ser_mine}],
					"response": 2,
				})

			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser_mine)), 1)
			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser_other)), 0,
				"a gun on someone else's disposition must never be minted here")
			# All four webhook-owned fields, not just the one the gate reads first:
			# they are written as one group, so checking one would still pass if the
			# grouping ever came apart.
			other_flags = frappe.db.get_value("POS Invoice", inv_other, [
				"form_4473_status", "nics_status",
				"fastbound_disposition_id", "fastbound_4473_pdf_url",
			], as_dict=True)
			self.assertNotEqual(other_flags.form_4473_status, "Completed",
				"…and its invoice must not be stamped 4473-Completed either")
			self.assertNotEqual(other_flags.nics_status, "Proceed",
				"…nor carry someone else's NICS verdict")
			self.assertFalse(other_flags.fastbound_disposition_id,
				"…nor be linked to a disposition it has nothing to do with")
			self.assertFalse(other_flags.fastbound_4473_pdf_url,
				"…nor point at a stranger's 4473 PDF")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_mine)
			self._cleanup_invoice(inv_other)

	def test_unresolvable_payload_serial_does_not_skip_coverage(self):
		"""A payload serial that matches no Draft invoice must not abort the handler.

		The "no Draft POS Invoice matches" early return is computed from the merged
		serial list; if it were computed from the payload alone, a payload naming a
		gun we can't resolve (serial edited in FastBound, its invoice already
		submitted) would return before the bound-book lookup was ever consulted, and
		the guns that ARE on a Draft invoice would get nothing at all.
		"""
		ser = "MG-UNRES-B"
		disp_id = "FB-MG-DISP-UNRES"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_one_firearm_line(ser)
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-UNRES-X", "serial": "MG-UNRES-NOT-IN-POS",
					"dispositionId": disp_id},
				{"id": "FBIT-UNRES-B", "serial": ser, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": "MG-UNRES-NOT-IN-POS"}],  # resolves to nothing
					"response": 2,
				})
			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser)), 1,
				f"{ser} is on the 4473 AND on a Draft invoice — it must be covered "
				"regardless of whether the payload's own serial matched anything")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	def test_lookup_failure_still_covers_the_payload_gun(self):
		"""Degradation, at the HANDLER level, not just the helper.

		When the bound-book lookup dies, the payload is all we have: its gun must
		still be covered and the doc-level fields must still be written. Nothing may
		be double-minted. Without this test the union is unfalsifiable — swapping it
		back to `authoritative or payload` keeps every other test green.
		"""
		ser = "MG-DEGRADE-A"
		disp_id = "FB-MG-DISP-DEGRADE"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_one_firearm_line(ser)
		client, _ = _items_client([])
		client.session.request.side_effect = RuntimeError("FastBound unreachable")
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client), \
			     patch.object(fb_webhook, "_log_error"):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": ser}],
					"response": 2,
				})
			self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser)), 1,
				"the payload gun must still be covered when the lookup fails")
			self.assertEqual(
				frappe.db.get_value("POS Invoice", inv_name, "form_4473_status"), "Completed")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	def test_non_proceed_records_verdict_but_mints_nothing(self):
		"""A Denied 4473 must never produce the disposition that satisfies the gate.

		The merged serial list widens what the handler can reach, so pin the
		proceed-only guard explicitly: the verdict lands on the invoice, the bound
		book gets nothing.
		"""
		ser_a, ser_b = "MG-DENY-A", "MG-DENY-B"
		disp_id = "FB-MG-DISP-DENY"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-DENY-A", "serial": ser_a, "dispositionId": disp_id},
				{"id": "FBIT-DENY-B", "serial": ser_b, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		try:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					return_value=client):
				fb_webhook._handle_form4473_completed({
					"disposition": {"id": disp_id},
					"items": [{"serial": ser_a}],
					"response": 3,  # Denied
				})
			for ser in (ser_a, ser_b):
				self.assertEqual(len(self._get_retail_sale_dispositions(disp_id, ser)), 0,
					f"a Denied 4473 must not mint coverage for {ser}")
			self.assertEqual(
				frappe.db.get_value("POS Invoice", inv_name, "nics_status"), "Denied",
				"the verdict itself must still be recorded on the invoice")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	# -----------------------------------------------------------------------
	# Test 2 — lockout removed: Completed invoice still gets coverage
	# -----------------------------------------------------------------------

	def test_lockout_removed_completed_invoice_gets_coverage(self):
		"""A Draft POS Invoice whose form_4473_status is already 'Completed'
		must not be excluded from disposition coverage. The old lockout filter
		on form_4473_status blocked this case — verifying it's gone."""
		ser = "MG-T2-X"
		disp_id = "FB-MG-DISP-T2"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		# Build a single-gun invoice already marked Completed (simulating the
		# first gun's coverage having flipped the invoice status).
		inv_name = self._draft_invoice_two_guns(ser, "MG-T2-UNUSED",
			form_4473="Completed")
		try:
			# _ensure_disposition_coverage must still match this invoice
			# despite form_4473_status="Completed".
			fb_webhook._ensure_disposition_coverage(disp_id, [ser], "Proceed")

			disps = self._get_retail_sale_dispositions(disp_id, ser)
			self.assertEqual(len(disps), 1,
				f"Expected 1 Retail Sale disposition for {ser} even though "
				f"invoice has form_4473_status=Completed; got {len(disps)}")
			self.assertEqual(disps[0].docstatus, 1)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-T2-UNUSED"})

	# -----------------------------------------------------------------------
	# Test 3 — disposition.committed backstop covers uncovered gun
	# -----------------------------------------------------------------------

	def test_disposition_committed_backstop_covers_uncovered_gun(self):
		"""disposition.committed handler must create a Retail Sale disposition for
		gun B even when gun A already has one (and the invoice is already Completed).
		Mirrors the _handle_disposition_committed backstop call."""
		ser_a = "MG-T3-A"
		ser_b = "MG-T3-B"
		disp_id = "FB-MG-DISP-T3"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Completed")
		try:
			# Pre-seed gun A's disposition (simulates form4473.completed partial coverage).
			fb_webhook._ensure_disposition_coverage(disp_id, [ser_a], "Proceed")
			a_before = self._get_retail_sale_dispositions(disp_id, ser_a)
			self.assertEqual(len(a_before), 1, "Pre-seed A should exist")
			b_before = self._get_retail_sale_dispositions(disp_id, ser_b)
			self.assertEqual(len(b_before), 0, "B should NOT exist yet")

			# Fire disposition.committed with the authoritative [A, B] list.
			# Patch _sync_fb_direct_disposal_stock to avoid stock side-effects
			# (like test_webhook_nics_trigger.py does).
			with patch.object(fb_webhook, "_fetch_disposition_serials",
					return_value=[ser_a, ser_b]), \
			     patch.object(fb_webhook, "_sync_fb_direct_disposal_stock"):
				fb_webhook._handle_disposition_committed({"dispositionId": disp_id})

			# Gun B must now have a Retail Sale disposition.
			b_after = self._get_retail_sale_dispositions(disp_id, ser_b)
			self.assertEqual(len(b_after), 1,
				f"Expected disposition.committed backstop to create gun B's "
				f"disposition; got {len(b_after)}")
			self.assertEqual(b_after[0].docstatus, 1)

			# Gun A must still have exactly one (idempotent).
			a_after = self._get_retail_sale_dispositions(disp_id, ser_a)
			self.assertEqual(len(a_after), 1,
				f"Gun A should still have exactly 1 disposition; got {len(a_after)}")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)

	# -----------------------------------------------------------------------
	# Test 3b — disposition.committed backstop must NOT fire for non-retail types
	# -----------------------------------------------------------------------

	def test_disposition_committed_non_retail_does_not_create_coverage(self):
		"""A non-retail disposition (e.g. Dealer Transfer) committing while its gun
		sits on a NEW Draft POS Invoice must NOT create a spurious Retail Sale FFL
		Disposition — that would bypass the new buyer's 4473 gate. The backstop is
		gated on an existing Retail Sale/Sale row for this fastbound_id."""
		ser = "MG-T3B-X"
		disp_id = "FB-MG-DISP-T3B"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_two_guns(ser, "MG-T3B-UNUSED")
		try:
			# Seed a NON-retail disposition for this fastbound_id (Dealer Transfer).
			non_retail = frappe.get_doc({
				"doctype": "FFL Disposition",
				"disposition_date": frappe.utils.now_datetime(),
				"disposition_type": "Dealer Transfer",
				"company": self.company,
				"item": MULTIGUN_ITEM,
				"serial": ser,
				"destination_ffl": "1-99-888-01-2D-09999",
				"fastbound_id": disp_id,
			})
			non_retail.flags.ignore_permissions = True
			non_retail.insert()

			# Before: only the Dealer Transfer exists, no Retail Sale.
			before = self._get_retail_sale_dispositions(disp_id, ser)
			self.assertEqual(len(before), 0, "No Retail Sale should exist before commit")

			# Fire disposition.committed with the gun's serial as the authoritative
			# list. The backstop must be skipped because no Retail Sale row exists.
			with patch.object(fb_webhook, "_fetch_disposition_serials",
					return_value=[ser]), \
			     patch.object(fb_webhook, "_sync_fb_direct_disposal_stock"):
				fb_webhook._handle_disposition_committed({"dispositionId": disp_id})

			# After: still NO Retail Sale disposition for that serial+invoice — the
			# guard skipped it, so the new buyer's 4473 gate is intact.
			after = self._get_retail_sale_dispositions(disp_id, ser)
			self.assertEqual(len(after), 0,
				f"Non-retail commit must NOT create a Retail Sale disposition; "
				f"got {len(after)}")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-T3B-UNUSED"})

	# -----------------------------------------------------------------------
	# Test 4 — idempotent: running coverage twice yields exactly one per serial
	# -----------------------------------------------------------------------

	def test_ensure_disposition_coverage_is_idempotent(self):
		"""Running _ensure_disposition_coverage twice must create exactly one
		Retail Sale disposition per serial (idempotent on fastbound_id + serial)."""
		ser = "MG-T4-X"
		disp_id = "FB-MG-DISP-T4"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_two_guns(ser, "MG-T4-UNUSED")
		try:
			fb_webhook._ensure_disposition_coverage(disp_id, [ser], "Proceed")
			fb_webhook._ensure_disposition_coverage(disp_id, [ser], "Proceed")

			disps = self._get_retail_sale_dispositions(disp_id, ser)
			self.assertEqual(len(disps), 1,
				f"Expected exactly 1 disposition after two coverage runs; got {len(disps)}")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-T4-UNUSED"})

	# -----------------------------------------------------------------------
	# Test 5 — match by fastbound_item_id when FastBound's serial diverges
	# -----------------------------------------------------------------------

	def test_4473_matches_by_fastbound_item_id_when_serial_diverges(self):
		"""form4473.completed must match the Draft POS Invoice by the stable
		fastbound_item_id even when FastBound's `serial` string has diverged from
		OUR Serial No (a serial edited directly in FastBound, or a disambiguating
		suffix we append on intake).

		Regression: the old bare-serial match looked up POS Invoice Items by
		FastBound's serial, which here is a DIFFERENT string than ours, so it
		found no invoice and left the sale stuck at form_4473_status=Pending."""
		ser = "MG-DIV-OURS"            # our Serial No / POS line serial
		fb_item_id = "FB-ITEM-DIVERGE"  # stable FastBound item id (= Serial No.fastbound_item_id)
		fb_serial = "MG-DIV-FBWRONG"    # FastBound's diverged serial string
		disp_id = "FB-MG-DISP-DIV"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		frappe.db.set_value("Serial No", ser, "fastbound_item_id", fb_item_id,
			update_modified=False)
		inv_name = self._draft_invoice_two_guns(ser, "MG-DIV-UNUSED")
		try:
			data = {
				"id": "FORM4473-DIV",
				"disposition": {"id": disp_id},
				# id + externalId resolve to OUR serial; `serial` is the wrong string.
				"items": [{"id": fb_item_id, "externalId": ser, "serial": fb_serial}],
				"response": 2,  # Proceed
			}
			with patch.object(fb_webhook, "_fetch_disposition_serials", return_value=[ser]):
				fb_webhook._handle_form4473_completed(data)

			# Invoice flipped to Completed/Proceed despite the diverged FB serial.
			inv = frappe.db.get_value("POS Invoice", inv_name,
				["form_4473_status", "nics_status", "fastbound_disposition_id"], as_dict=True)
			self.assertEqual(inv.form_4473_status, "Completed")
			self.assertEqual(inv.nics_status, "Proceed")
			self.assertEqual(inv.fastbound_disposition_id, disp_id)

			# And a submitted Retail Sale disposition was created for OUR serial.
			disps = self._get_retail_sale_dispositions(disp_id, ser)
			self.assertEqual(len(disps), 1,
				f"Expected 1 Retail Sale disposition for {ser}; got {len(disps)}")
			self.assertEqual(disps[0].docstatus, 1)
			self.assertEqual(disps[0].linked_pos_invoice, inv_name)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-DIV-UNUSED"})
			frappe.db.delete("Serial No", {"name": ser})

	# -----------------------------------------------------------------------
	# Test 6 — ONE line carrying TWO newline-joined serials (the incident shape)
	# -----------------------------------------------------------------------

	def _draft_invoice_one_firearm_line(self, serial: str,
			form_4473: str = "", nics: str = "") -> str:
		"""One firearm line, qty 1, single serial — passes the one-per-line guard.
		Tests then PLANT a newline-joined serial via db.set_value to reproduce the
		historical incident row (2 guns on one line) without tripping validate."""
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = MULTIGUN_CUSTOMER
		inv.append("items", {
			"item_code": MULTIGUN_ITEM, "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": serial,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1,
		})
		inv.set_missing_values()
		if form_4473:
			inv.form_4473_status = form_4473
		if nics:
			inv.nics_status = nics
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _plant_two_serials_on_line(self, inv_name: str, ser_a: str, ser_b: str) -> None:
		"""Force the incident shape: the invoice's single firearm line holds BOTH
		serials newline-joined, as if rung qty=2 before the guard existed."""
		row = frappe.db.get_value("POS Invoice Item", {"parent": inv_name}, "name")
		frappe.db.set_value("POS Invoice Item", row, "serial_no",
			f"{ser_a}\n{ser_b}", update_modified=False)

	def test_form4473_completed_matches_one_line_two_serials(self):
		"""form4473.completed must match a Draft POS Invoice whose SINGLE line
		carries two newline-joined serials (a past incident), flip
		it Completed/Proceed, and mint TWO per-serial Retail Sale dispositions —
		neither with a "\\n" in its serial."""
		ser_a, ser_b = "MG-ONELINE-A", "MG-ONELINE-B"
		disp_id = "FB-MG-DISP-ONELINE"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_one_firearm_line(ser_a)
		self._plant_two_serials_on_line(inv_name, ser_a, ser_b)
		try:
			data = {
				"id": "FORM4473-ONELINE",
				"disposition": {"id": disp_id},
				"items": [{"serial": ser_a}, {"serial": ser_b}],
				"response": 2,  # Proceed
			}
			with patch.object(fb_webhook, "_fetch_disposition_serials",
					return_value=[ser_a, ser_b]):
				fb_webhook._handle_form4473_completed(data)

			inv = frappe.db.get_value("POS Invoice", inv_name,
				["form_4473_status", "nics_status", "fastbound_disposition_id"],
				as_dict=True)
			self.assertEqual(inv.form_4473_status, "Completed")
			self.assertEqual(inv.nics_status, "Proceed")
			self.assertEqual(inv.fastbound_disposition_id, disp_id)

			for ser in (ser_a, ser_b):
				disps = self._get_retail_sale_dispositions(disp_id, ser)
				self.assertEqual(len(disps), 1,
					f"Expected exactly 1 Retail Sale disposition for {ser}, got {len(disps)}")
				self.assertEqual(disps[0].docstatus, 1)
				self.assertEqual(disps[0].linked_pos_invoice, inv_name)
			# No disposition may carry the raw newline-joined string as its serial.
			joined = frappe.get_all("FFL Disposition",
				filters={"fastbound_id": disp_id}, pluck="serial")
			self.assertNotIn(f"{ser_a}\n{ser_b}", joined)
			self.assertEqual(sorted(joined), sorted([ser_a, ser_b]))
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_manager_override_one_line_two_serials(self):
		"""manager_override_4473 on a single line carrying two newline-joined
		serials must book TWO per-serial override dispositions — never one with a
		"\\n" in its serial (a past incident)."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-OVLINE-A", "MG-OVLINE-B"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_one_firearm_line(ser_a, form_4473="Pending")
		self._plant_two_serials_on_line(inv_name, ser_a, ser_b)
		try:
			res = pos_4473.manager_override_4473(
				inv_name, "4473 completed in FastBound; webhook never synced")
			self.assertTrue(res["ok"])
			self.assertEqual(res["n_firearms"], 2)
			self.assertEqual(len(res["dispositions"]), 2)

			for ser in (ser_a, ser_b):
				disps = self._override_dispositions(inv_name, ser)
				self.assertEqual(len(disps), 1, f"one override disposition for {ser}")
				self.assertEqual(disps[0].docstatus, 1)
				self.assertEqual(disps[0].manual_override, 1)
			booked = frappe.get_all("FFL Disposition",
				filters={"linked_pos_invoice": inv_name}, pluck="serial")
			self.assertNotIn(f"{ser_a}\n{ser_b}", booked)
			self.assertEqual(sorted(booked), sorted([ser_a, ser_b]))
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	# -----------------------------------------------------------------------
	# Case-insensitive match + substring rejection
	# -----------------------------------------------------------------------

	def test_find_matches_serial_case_insensitively(self):
		"""Restore the historical case-insensitive collation match. FastBound
		may return a serial in a different case than we store (j4940 vs J4940);
		the matcher must still find the line and return OUR stored serial."""
		ser = "MgCiCASE7"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_one_firearm_line(ser)
		try:
			hits = fb_webhook._find_draft_invoices_for_serials([ser.casefold()])
			self.assertEqual(hits, {inv_name: [ser]},
				"a case-different serial must match and return the stored serial")
		finally:
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": ser})

	def test_find_rejects_substring_false_positive(self):
		"""A serial that is a SUBSTRING of a line serial must NOT match — the
		LIKE prefilter is only a superset; the exact-token check rejects it. And
		firing the handler for the substring mints no disposition and no flip."""
		line_ser = "MG-N2-J49400"   # what's on the invoice line
		req_ser = "MG-N2-J4940"     # FB request — a prefix substring, must not hit
		disp_id = "FB-MG-DISP-N2"
		_stock_serial(line_ser, MULTIGUN_ITEM, self.company, self.warehouse)
		inv_name = self._draft_invoice_one_firearm_line(line_ser)
		before = frappe.db.get_value("POS Invoice", inv_name, "form_4473_status")
		try:
			self.assertEqual(fb_webhook._find_draft_invoices_for_serials([req_ser]), {},
				"a substring must not false-match a longer line serial")
			data = {
				"id": "FORM4473-N2",
				"disposition": {"id": disp_id},
				"items": [{"serial": req_ser}],
				"response": 2,  # Proceed
			}
			with patch.object(fb_webhook, "_fetch_disposition_serials",
					return_value=[req_ser]):
				fb_webhook._handle_form4473_completed(data)
			# The invoice must NOT flip to Completed (unchanged from its start state).
			self.assertEqual(
				frappe.db.get_value("POS Invoice", inv_name, "form_4473_status"), before,
				"invoice must not flip on a substring non-match")
			self.assertEqual(
				frappe.get_all("FFL Disposition", filters={"fastbound_id": disp_id}), [],
				"no disposition may be minted for a substring non-match")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": line_ser})

	# -----------------------------------------------------------------------
	# Manager Override (force-complete a stuck firearm sale)
	# -----------------------------------------------------------------------

	def _override_dispositions(self, inv_name: str, serial: str) -> list:
		return frappe.get_all(
			"FFL Disposition",
			filters={"linked_pos_invoice": inv_name, "serial": serial,
				"disposition_type": "Retail Sale"},
			fields=["name", "docstatus", "manual_override", "override_reason",
				"override_by", "form_4473_status", "nics_status", "fastbound_id"],
		)

	def test_manager_override_books_audited_disposition_and_unblocks_gate(self):
		"""manager_override_4473 books an audited manual-override Retail Sale
		disposition (no fastbound_id) per firearm and flips the invoice to
		Completed/Proceed, so the firearm-sale submit gate passes."""
		from ffl_integrations.fastbound import pos_4473
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		ser_a, ser_b = "MG-OV-A", "MG-OV-B"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			res = pos_4473.manager_override_4473(
				inv_name, "4473 completed in FastBound; webhook never synced")
			self.assertTrue(res["ok"])
			self.assertEqual(res["n_firearms"], 2)
			self.assertEqual(len(res["dispositions"]), 2)

			inv = frappe.db.get_value("POS Invoice", inv_name,
				["form_4473_status", "nics_status"], as_dict=True)
			self.assertEqual(inv.form_4473_status, "Completed")
			self.assertEqual(inv.nics_status, "Proceed")

			for ser in (ser_a, ser_b):
				disps = self._override_dispositions(inv_name, ser)
				self.assertEqual(len(disps), 1, f"one override disposition for {ser}")
				d = disps[0]
				self.assertEqual(d.docstatus, 1)
				self.assertEqual(d.manual_override, 1)
				self.assertTrue((d.override_reason or "").strip())
				self.assertTrue((d.override_by or "").strip())
				self.assertFalse((d.fastbound_id or "").strip(),
					"override carries NO fastbound_id (FastBound handled separately)")
				self.assertEqual(d.form_4473_status, "Completed")
				self.assertEqual(d.nics_status, "Proceed")

			# The firearm-sale gate now passes for this (now Completed) invoice.
			gate_doc = frappe.get_doc("POS Invoice", inv_name)
			gate_doc.docstatus = 1  # simulate submit-time evaluation
			require_4473_and_nics_for_firearm_sale(gate_doc)  # must not raise
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_manager_override_requires_reason(self):
		from ffl_integrations.fastbound import pos_4473
		ser = "MG-OV-NOREASON"
		self._stock_both(ser, "MG-OV-NR2")
		inv_name = self._draft_invoice_two_guns(ser, "MG-OV-NR2", form_4473="Pending")
		try:
			with self.assertRaises(frappe.ValidationError):
				pos_4473.manager_override_4473(inv_name, "   ")
			# Nothing booked on the failed override.
			self.assertEqual(len(self._override_dispositions(inv_name, ser)), 0)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-OV-NR2"})
			frappe.db.delete("Serial No", {"name": ser})

	def test_manager_override_requires_manager_role(self):
		from ffl_integrations.fastbound import pos_4473
		ser = "MG-OV-NOROLE"
		self._stock_both(ser, "MG-OV-NRL2")
		inv_name = self._draft_invoice_two_guns(ser, "MG-OV-NRL2", form_4473="Pending")
		try:
			with patch.object(pos_4473.frappe, "get_roles", return_value=["Sales User"]):
				with self.assertRaises(frappe.PermissionError):
					pos_4473.manager_override_4473(inv_name, "trying without manager role")
			self.assertEqual(len(self._override_dispositions(inv_name, ser)), 0)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			frappe.db.delete("Serial No", {"name": "MG-OV-NRL2"})
			frappe.db.delete("Serial No", {"name": ser})

	def test_manager_override_is_idempotent(self):
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-OV-IDEM-A", "MG-OV-IDEM-B"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			pos_4473.manager_override_4473(inv_name, "first")
			pos_4473.manager_override_4473(inv_name, "second")
			for ser in (ser_a, ser_b):
				self.assertEqual(len(self._override_dispositions(inv_name, ser)), 1,
					f"exactly one override disposition for {ser} after two runs")
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_override_disposition_guard_rejects_missing_reason(self):
		"""Defense in depth: a direct manual_override Retail Sale insert with no
		reason is rejected by the doctype validator, not just the POS method."""
		ser = "MG-OV-GUARD"
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		try:
			disp = frappe.get_doc({
				"doctype": "FFL Disposition",
				"disposition_date": frappe.utils.now_datetime(),
				"disposition_type": "Retail Sale",
				"company": self.company,
				"customer": MULTIGUN_CUSTOMER,
				"item": MULTIGUN_ITEM,
				"serial": ser,
				"manual_override": 1,  # no override_reason → must be rejected
			})
			disp.flags.ignore_permissions = True
			with self.assertRaises(frappe.ValidationError):
				disp.insert()
		finally:
			frappe.db.delete("FFL Disposition", {"serial": ser})
			frappe.db.delete("Serial No", {"name": ser})

	# -----------------------------------------------------------------------
	# Paper 4473 Mode (store keeps paper 4473s; no FastBound)
	# -----------------------------------------------------------------------

	def _set_paper_mode(self, on: bool) -> None:
		"""Auto-restored on teardown (SettingsSandbox): a leaked paper-mode flag
		would silently put every later firearm test in the run on the paper path,
		and the failures would land nowhere near the test that caused them."""
		self.set_single("FFL Settings", "paper_4473_mode", 1 if on else 0)

	def test_attest_paper_4473_refused_while_fastbound_is_the_source_of_truth(self):
		"""With Paper 4473 Mode off, a cashier must NOT be able to click past a
		pending FastBound 4473 — that is what the manager override is for."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPER-OFF-A", "MG-PAPER-OFF-B"
		self._set_paper_mode(False)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			with self.assertRaises(frappe.ValidationError):
				pos_4473.attest_paper_4473(inv_name)
			self.assertEqual(len(self._override_dispositions(inv_name, ser_a)), 0,
				"a refused attestation must book nothing")
			self.assertEqual(
				frappe.db.get_value("POS Invoice", inv_name, "form_4473_status"), "Pending")
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_attest_paper_4473_books_the_bound_book_and_unblocks_the_sale(self):
		"""The headline behaviour: ticking the checkbox books one audited Retail
		Sale disposition per gun (so the POS keeps its own A&D book with no
		FastBound at all) and the firearm-sale submit gate then passes."""
		from ffl_integrations.fastbound import pos_4473
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		ser_a, ser_b = "MG-PAPER-A", "MG-PAPER-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			res = pos_4473.attest_paper_4473(inv_name)
			self.assertTrue(res["ok"])
			self.assertEqual(res["n_firearms"], 2)

			inv = frappe.db.get_value("POS Invoice", inv_name,
				["form_4473_status", "nics_status"], as_dict=True)
			self.assertEqual(inv.form_4473_status, "Completed")
			self.assertEqual(inv.nics_status, "Proceed")

			for ser in (ser_a, ser_b):
				disps = self._override_dispositions(inv_name, ser)
				self.assertEqual(len(disps), 1, f"one paper-4473 disposition for {ser}")
				d = disps[0]
				self.assertEqual(d.docstatus, 1)
				self.assertEqual(d.manual_override, 1,
					"a hand-asserted 4473 must be marked as one, never read as FastBound-committed")
				self.assertEqual(d.override_by, frappe.session.user,
					"the record must name WHO said the paper form was done")
				self.assertIn("paper", (d.override_reason or "").lower())
				self.assertFalse((d.fastbound_id or "").strip(),
					"nothing was pushed anywhere — this store keeps no electronic book")

			gate_doc = frappe.get_doc("POS Invoice", inv_name)
			gate_doc.docstatus = 1  # simulate submit-time evaluation
			require_4473_and_nics_for_firearm_sale(gate_doc)  # must not raise
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_attest_paper_4473_needs_no_manager(self):
		"""The whole point: this is the store's ordinary way of working, so the
		cashier who filled the paper form in can record it. Requiring a manager
		would put one behind every single gun sale.

		Patches the GLOBAL ``frappe.get_roles``, not the endpoint module's — the
		FFL Disposition doctype runs its OWN role gate inside validate, and a
		module-scoped patch would leave that gate reading Administrator's roles and
		pass while a real cashier was rejected at the counter.
		"""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPER-CASHIER-A", "MG-PAPER-CASHIER-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			with patch("frappe.get_roles", return_value=["Sales User"]):
				res = pos_4473.attest_paper_4473(inv_name)
			self.assertTrue(res["ok"])
			self.assertEqual(len(self._override_dispositions(inv_name, ser_a)), 1)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_manual_override_doctype_gate_still_demands_a_manager_outside_paper_mode(self):
		"""The widened role rule is scoped to Paper 4473 Mode. With FastBound as the
		source of truth, a hand-asserted Retail Sale is an EXCEPTION and a manager
		still owns it — including against a direct REST / script insert."""
		ser = "MG-PAPER-DIRECT"
		self._set_paper_mode(False)
		_stock_serial(ser, MULTIGUN_ITEM, self.company, self.warehouse)
		try:
			def _mint():
				disp = frappe.get_doc({
					"doctype": "FFL Disposition",
					"disposition_date": frappe.utils.now_datetime(),
					"disposition_type": "Retail Sale",
					"company": self.company,
					"customer": MULTIGUN_CUSTOMER,
					"item": MULTIGUN_ITEM,
					"serial": ser,
					"form_4473_status": "Completed",
					"nics_status": "Proceed",
					"manual_override": 1,
					"override_reason": "direct insert by a cashier",
				})
				disp.flags.ignore_permissions = True
				disp.insert()

			with patch("frappe.get_roles", return_value=["Sales User"]):
				with self.assertRaises(frappe.PermissionError):
					_mint()
			# ...and the same insert is accepted once the store IS in paper mode.
			self._set_paper_mode(True)
			with patch("frappe.get_roles", return_value=["Sales User"]):
				_mint()
			self.assertEqual(len(frappe.get_all("FFL Disposition", filters={"serial": ser})), 1)
		finally:
			frappe.db.delete("FFL Disposition", {"serial": ser})
			frappe.db.delete("Serial No", {"name": ser})

	def test_attest_paper_4473_still_needs_a_counter_role(self):
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPER-NOROLE-A", "MG-PAPER-NOROLE-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			with patch("frappe.get_roles", return_value=["Guest"]):
				with self.assertRaises(frappe.PermissionError):
					pos_4473.attest_paper_4473(inv_name)
			self.assertEqual(len(self._override_dispositions(inv_name, ser_a)), 0)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_attest_paper_4473_is_idempotent(self):
		"""A double-tapped checkbox books one disposition per gun, not two."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPER-IDEM-A", "MG-PAPER-IDEM-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			pos_4473.attest_paper_4473(inv_name)
			pos_4473.attest_paper_4473(inv_name)
			for ser in (ser_a, ser_b):
				self.assertEqual(len(self._override_dispositions(inv_name, ser)), 1,
					f"exactly one paper-4473 disposition for {ser} after two ticks")
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_attest_paper_4473_one_line_two_serials_books_per_gun(self):
		"""Each gun needs its own bound-book entry — never one row whose `serial`
		is the raw newline-joined string (a past incident)."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPERLINE-A", "MG-PAPERLINE-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_one_firearm_line(ser_a, form_4473="Pending")
		self._plant_two_serials_on_line(inv_name, ser_a, ser_b)
		try:
			res = pos_4473.attest_paper_4473(inv_name)
			self.assertEqual(res["n_firearms"], 2)
			booked = frappe.get_all("FFL Disposition",
				filters={"linked_pos_invoice": inv_name}, pluck="serial")
			self.assertNotIn(f"{ser_a}\n{ser_b}", booked)
			self.assertEqual(sorted(booked), sorted([ser_a, ser_b]))
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})

	def test_start_4473_refuses_when_fastbound_is_switched_off(self):
		"""Credentials outlive the decision to stop using a vendor, so account_num is
		usually still filled in and cannot be what stops us. Without an explicit
		refusal the cashier is sent to complete a REAL 4473 in a FastBound account
		whose webhook this site now refuses — the record splits across two books
		and the order sits at Pending forever."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-FBOFF-A", "MG-FBOFF-B"
		self._set_paper_mode(False)
		self.set_single("FastBound Settings", "enabled", 0)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			doc = frappe.get_doc("POS Invoice", inv_name)
			serial_map = {row.name: row.serial_no for row in doc.items}
			with self.assertRaises(frappe.ValidationError) as ctx:
				pos_4473.start_4473_for_pos_invoice(inv_name, serial_map)
			self.assertIn("Paper 4473 Mode", str(ctx.exception))
			self.assertNotEqual(
				frappe.db.get_value("POS Invoice", inv_name, "form_4473_status"), "Pending",
				"a refused kickoff must not park the order awaiting a webhook")
		finally:
			self._cleanup_invoice(inv_name)
			for s2 in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s2})

	def test_start_4473_in_paper_mode_holds_the_ticket_with_no_fastbound_url(self):
		"""The customer transfer-in flow ends by calling this, so a hard requirement
		on FastBound here took the whole transfer service down with it — including
		the only place is_ffl_transfer is ever set."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPERSTART-A", "MG-PAPERSTART-B"
		self._set_paper_mode(True)
		self.set_single("FastBound Settings", "enabled", 0)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			doc = frappe.get_doc("POS Invoice", inv_name)
			serial_map = {row.name: row.serial_no for row in doc.items}
			res = pos_4473.start_4473_for_pos_invoice(inv_name, serial_map)
			self.assertTrue(res["ok"])
			self.assertEqual(res["fastbound_url"], "",
				"nowhere to send anyone — paper mode has no wizard")
			inv = frappe.db.get_value("POS Invoice", inv_name,
				["form_4473_status", "fastbound_url"], as_dict=True)
			self.assertEqual(inv.form_4473_status, "Pending")
			self.assertFalse((inv.fastbound_url or "").strip())
			# ...and the counter can then close it out on paper.
			self.assertTrue(pos_4473.attest_paper_4473(inv_name)["ok"])
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s2 in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s2})

	def test_attest_refuses_while_an_electronic_4473_is_in_flight(self):
		"""Both settings can legitimately be on during a migration. What must not
		happen is a cashier ticking past an electronic 4473 somebody is filling in
		right now on FastBound's side — redoing that is a manager action with a
		reason, not a checkbox."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-INFLIGHT-A", "MG-INFLIGHT-B"
		self._set_paper_mode(True)
		self.set_single("FastBound Settings", "enabled", 1)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		frappe.db.set_value("POS Invoice", inv_name, "fastbound_url",
			"https://cloud.fastbound.com/TEST-ACC/Form4473s", update_modified=False)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				pos_4473.attest_paper_4473(inv_name)
			self.assertIn("FastBound", str(ctx.exception))
			self.assertEqual(len(self._override_dispositions(inv_name, ser_a)), 0)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s2 in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s2})

	def test_manager_override_still_works_in_paper_mode(self):
		"""The escape hatch is not replaced by the checkbox — a manager may still
		record a reasoned override, and it stays reason-required."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-PAPER-OVR-A", "MG-PAPER-OVR-B"
		self._set_paper_mode(True)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			with self.assertRaises(frappe.ValidationError):
				pos_4473.manager_override_4473(inv_name, "  ")
			res = pos_4473.manager_override_4473(inv_name, "buyer's paper form re-run")
			self.assertTrue(res["ok"])
			self.assertEqual(len(self._override_dispositions(inv_name, ser_a)), 1)
		finally:
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)
			for s in (ser_a, ser_b):
				frappe.db.delete("Serial No", {"name": s})


	# -----------------------------------------------------------------------
	# Form 4473 Log — the durable record every completed 4473 leaves behind
	# -----------------------------------------------------------------------

	def _log_for(self, form_id: str):
		return frappe.db.get_value("Form 4473 Log", {"form4473_id": form_id},
			["name", "pos_invoice", "nics_status", "source", "serials", "pdf_status",
				"fastbound_disposition_id", "pdf_url"], as_dict=True)

	def _complete_4473(self, disp_id: str, form_id: str, ser_a: str, ser_b: str, response: int):
		client, _ = _items_client([
			{"items": [
				{"id": f"FBIT-{ser_a}", "serial": ser_a, "dispositionId": disp_id},
				{"id": f"FBIT-{ser_b}", "serial": ser_b, "dispositionId": disp_id},
			]},
			{"items": []},
		])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast", return_value=client), \
				patch("frappe.enqueue") as enqueue:
			fb_webhook._handle_form4473_completed({
				"id": form_id,
				"disposition": {"id": disp_id},
				"items": [{"serial": ser_a}],
				"response": response,
				"apiDownloadUrl": f"https://cloud.fastbound.com/1/api/Form4473s/Download/{form_id}",
			})
		return enqueue

	def _drop_log(self, form_id: str) -> None:
		frappe.db.delete("Form 4473 Log", {"form4473_id": form_id})

	def test_completed_4473_is_logged_with_its_verdict(self):
		"""Proceed or Denied, the 4473 gets one durable row, and its PDF copy is
		queued for after the commit."""
		for response, nics, tag in ((2, "Proceed", "P"), (3, "Denied", "D")):
			ser_a, ser_b = f"MG-LOG{tag}-A", f"MG-LOG{tag}-B"
			disp_id, form_id = f"FB-LOG-DISP-{tag}", f"FB-LOG-FORM-{tag}"
			self._stock_both(ser_a, ser_b)
			inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
			try:
				enqueue = self._complete_4473(disp_id, form_id, ser_a, ser_b, response)
				log = self._log_for(form_id)
				self.assertEqual((log.pos_invoice, log.nics_status, log.source, log.pdf_status),
					(inv_name, nics, "FastBound", "Pending"))
				self.assertEqual(log.fastbound_disposition_id, disp_id)
				self.assertEqual(sorted(log.serials.split(", ")), sorted([ser_a, ser_b]))
				queued = [c for c in enqueue.call_args_list
					if c.args and c.args[0].endswith("pdf_archive.store_4473_pdf")]
				self.assertEqual(len(queued), 1)
				self.assertEqual(queued[0].kwargs["log_name"], log.name)
				self.assertTrue(queued[0].kwargs["enqueue_after_commit"])
			finally:
				frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
				self._drop_log(form_id)
				self._cleanup_invoice(inv_name)

	def test_redelivered_4473_updates_the_same_row(self):
		ser_a, ser_b = "MG-LOGR-A", "MG-LOGR-B"
		disp_id, form_id = "FB-LOG-DISP-R", "FB-LOG-FORM-R"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			self._complete_4473(disp_id, form_id, ser_a, ser_b, 3)
			self._complete_4473(disp_id, form_id, ser_a, ser_b, 3)
			self.assertEqual(frappe.db.count("Form 4473 Log", {"form4473_id": form_id}), 1)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._drop_log(form_id)
			self._cleanup_invoice(inv_name)

	def test_4473_with_no_matching_order_is_still_logged(self):
		"""Done straight in FastBound (no draft here): still on file, no order."""
		disp_id, form_id = "FB-LOG-DISP-N", "FB-LOG-FORM-N"
		try:
			self._complete_4473(disp_id, form_id, "MG-LOGN-A", "MG-LOGN-B", 2)
			log = self._log_for(form_id)
			self.assertIsNotNone(log)
			self.assertFalse(log.pos_invoice)
			self.assertEqual(log.nics_status, "Proceed")
		finally:
			self._drop_log(form_id)

	def test_4473_whose_guns_resolve_to_no_serial_is_still_logged(self):
		"""Done in FastBound for a gun never received here: no serials resolve."""
		disp_id, form_id = "FB-LOG-DISP-Z", "FB-LOG-FORM-Z"
		try:
			with patch.object(fb_webhook, "_authoritative_4473_serials", return_value=[]), \
					patch("frappe.enqueue"):
				fb_webhook._handle_form4473_completed({
					"id": form_id, "disposition": {"id": disp_id}, "response": 2})
			log = self._log_for(form_id)
			self.assertIsNotNone(log)
			self.assertFalse(log.pos_invoice)
		finally:
			self._drop_log(form_id)

	def test_log_survives_the_draft_being_deleted(self):
		ser_a, ser_b = "MG-LOGX-A", "MG-LOGX-B"
		disp_id, form_id = "FB-LOG-DISP-X", "FB-LOG-FORM-X"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			self._complete_4473(disp_id, form_id, ser_a, ser_b, 3)
			frappe.delete_doc("POS Invoice", inv_name, ignore_permissions=True, force=True)
			self.assertEqual(self._log_for(form_id).pos_invoice, inv_name)
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._drop_log(form_id)
			self._cleanup_invoice(inv_name)

	def test_a_failed_log_write_never_costs_the_sale(self):
		ser_a, ser_b = "MG-LOGF-A", "MG-LOGF-B"
		disp_id, form_id = "FB-LOG-DISP-F", "FB-LOG-FORM-F"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		try:
			with patch("ffl_core.ffl_core.doctype.form_4473_log.form_4473_log.record_4473",
					side_effect=RuntimeError("log table broken")):
				self._complete_4473(disp_id, form_id, ser_a, ser_b, 2)
			self.assertEqual(
				frappe.db.get_value("POS Invoice", inv_name, ["form_4473_status", "nics_status"]),
				("Completed", "Proceed"))
			self.assertIsNone(self._log_for(form_id))
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			self._drop_log(form_id)
			self._cleanup_invoice(inv_name)

	def test_disposition_committed_clears_the_logged_nics(self):
		"""A Delayed buyer who cleared: the log follows the invoice to Proceed."""
		from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
		ser_a, ser_b = "MG-LOGC-A", "MG-LOGC-B"
		disp_id, form_id = "FB-LOG-DISP-C", "FB-LOG-FORM-C"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b)
		frappe.db.set_value("POS Invoice", inv_name, "fastbound_disposition_id", disp_id)
		record_4473(form4473_id=form_id, pos_invoices=[inv_name], nics_status="Delayed",
			fastbound_disposition_id=disp_id)
		# A 4473 voided earlier on the same disposition must stay what it was.
		record_4473(form4473_id=form_id + "-VOID", pos_invoices=[inv_name],
			nics_status="Cancelled", fastbound_disposition_id=disp_id)
		try:
			with patch.object(fb_webhook, "_sync_fb_direct_disposal_stock"), \
					patch.object(fb_webhook, "_alert_invoice_serial_coverage"), \
					patch("frappe.enqueue"):
				fb_webhook._handle_disposition_committed({"dispositionId": disp_id})
			self.assertEqual(self._log_for(form_id).nics_status, "Proceed")
			self.assertEqual(self._log_for(form_id + "-VOID").nics_status, "Cancelled")
		finally:
			self._drop_log(form_id)
			self._drop_log(form_id + "-VOID")
			self._cleanup_invoice(inv_name)

	def test_override_logs_one_paper_row(self):
		"""A hand-asserted 4473 is on file too — once, however often it is re-clicked."""
		from ffl_integrations.fastbound import pos_4473
		ser_a, ser_b = "MG-LOGO-A", "MG-LOGO-B"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			pos_4473.manager_override_4473(inv_name, "4473 done in FastBound; never synced")
			pos_4473.manager_override_4473(inv_name, "clicked again")
			rows = frappe.get_all("Form 4473 Log", filters={"pos_invoice": inv_name},
				fields=["source", "nics_status", "form4473_id", "pdf_status", "serials"])
			self.assertEqual(len(rows), 1)
			self.assertEqual((rows[0].source, rows[0].nics_status, rows[0].pdf_status),
				("Paper / override", "Proceed", "No PDF"))
			self.assertFalse(rows[0].form4473_id)
			self.assertEqual(sorted(rows[0].serials.split(", ")), sorted([ser_a, ser_b]))
		finally:
			frappe.db.delete("Form 4473 Log", {"pos_invoice": inv_name})
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			self._cleanup_invoice(inv_name)

	def test_override_after_redo_books_and_logs_again(self):
		"""Redo 4473 cancels the hand-asserted dispositions; the corrected
		4473 attested next must book new ones (the cancelled rows are not a
		booking) and leave its own Form 4473 Log row."""
		from ffl_integrations.fastbound import pos_4473
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		from ffl_core.compliance.firearm import covered_serials_for_invoice
		ser_a, ser_b = "MG-REDO-A", "MG-REDO-B"
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		try:
			pos_4473.manager_override_4473(inv_name, "paper 4473 on file")
			recovery.manager_redo_4473(inv_name, "wrong serial on the paper 4473")
			res = pos_4473.manager_override_4473(inv_name, "corrected paper 4473")
			self.assertEqual(len(res["dispositions"]), 2)
			for ser in (ser_a, ser_b):
				self.assertEqual(sorted(d.docstatus for d in
					self._override_dispositions(inv_name, ser)), [1, 2])
			self.assertEqual(covered_serials_for_invoice(inv_name, [ser_a, ser_b]), {ser_a, ser_b})
			logs = frappe.get_all("Form 4473 Log", filters={"pos_invoice": inv_name},
				pluck="nics_status", order_by="creation asc")
			self.assertEqual(logs, ["Cancelled", "Proceed"],
				"the redone 4473's row is voided; the corrected one is on file")
		finally:
			frappe.db.delete("Form 4473 Log", {"pos_invoice": inv_name})
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			frappe.db.delete("Comment", {"reference_doctype": "POS Invoice",
				"reference_name": inv_name})
			self._cleanup_invoice(inv_name)

	def test_paper_attest_after_redo_of_an_electronic_4473(self):
		"""An order that went to FastBound and was redone can be closed with
		a paper 4473 — the refusal's own message says to run Redo 4473, so the
		redo must clear the in-flight marker."""
		from ffl_integrations.fastbound import pos_4473
		from ffl_integrations.fastbound import pos_4473_recovery as recovery
		ser_a, ser_b = "MG-REDO-PAPER-A", "MG-REDO-PAPER-B"
		self._set_paper_mode(True)
		self.set_single("FastBound Settings", "enabled", 1)
		self._stock_both(ser_a, ser_b)
		inv_name = self._draft_invoice_two_guns(ser_a, ser_b, form_4473="Pending")
		frappe.db.set_value("POS Invoice", inv_name, "fastbound_url",
			"https://cloud.fastbound.com/TEST-ACC/Form4473s", update_modified=False)
		try:
			pos_4473.manager_override_4473(inv_name, "4473 done in FastBound; never synced")
			recovery.manager_redo_4473(inv_name, "wrong serial on the 4473")
			res = pos_4473.attest_paper_4473(inv_name)
			self.assertEqual(len(res["dispositions"]), 2)
			for ser in (ser_a, ser_b):
				self.assertEqual(sorted(d.docstatus for d in
					self._override_dispositions(inv_name, ser)), [1, 2])
		finally:
			frappe.db.delete("Form 4473 Log", {"pos_invoice": inv_name})
			frappe.db.delete("FFL Disposition", {"linked_pos_invoice": inv_name})
			frappe.db.delete("Comment", {"reference_doctype": "POS Invoice",
				"reference_name": inv_name})
			self._cleanup_invoice(inv_name)

def _final_lock_grants(records: list[tuple[int, bool]]) -> dict[int, bool]:
	"""(thread_ident, granted) call records → final grant per thread.

	book_disposition_once retries acquisition once, so a worker may record
	``[False, True]`` — the FINAL value is whether it entered the locked path;
	an unrecovered ``[False, False]`` stays False."""
	out: dict[int, bool] = {}
	for ident, granted in records:
		out[ident] = granted
	return out


class FinalLockGrantsTriage(FrappeTestCase):
	"""Pin the triage helper ConcurrentDispositionBooking relies on to tell
	'lock unavailable in this environment' apart from 'serialization broken'."""

	def test_recovered_retry_counts_as_granted(self):
		self.assertEqual(
			_final_lock_grants([(11, False), (11, True), (22, True)]),
			{11: True, 22: True})

	def test_unrecovered_failure_stays_false(self):
		self.assertEqual(
			_final_lock_grants([(11, False), (11, False), (22, True)]),
			{11: False, 22: True})


class ConcurrentDispositionBooking(FrappeTestCase):
	"""Isolated in its OWN class because it does LOAD-BEARING commits: real threads
	on their own DB connections are the only way to exercise the cross-transaction
	double-mint race, and cross-connection visibility needs real commits. Kept out
	of FBMultiGunCoverage so those commits can't move that class's rollback floor
	(docs/testing.md rule 2). Everything committed is torn down in finally."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		_ensure_multigun_item()
		if not frappe.db.exists("Customer", MULTIGUN_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": MULTIGUN_CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.commit()  # masters must be visible to the worker connections

	def setUp(self):
		super().setUp()
		if getattr(frappe.flags, "currently_saving", None) is None:
			frappe.flags.currently_saving = []

	def test_book_disposition_once_serialises_concurrent_bookings(self):
		"""DB-level idempotency: book_disposition_once must
		serialise two OVERLAPPING bookings of the same (fastbound_id, serial), each
		on its OWN DB connection, so exactly ONE FFL Disposition is inserted — the
		loser blocks on the advisory lock, then sees the winner's committed row and
		skips.

		Deterministic (not timing-luck): the winner's insert_fn sleeps WHILE holding
		the lock, so the loser is guaranteed to contend on it. The load-bearing
		commits + every committed row are torn down in finally.

		If the runner cannot grant the advisory lock AT ALL (nightly 29560313078:
		a swallowed transient GET_LOCK failure degraded the guard to the unlocked
		path), that is reported as its own distinct failure BEFORE the serialization
		assertions — 'loser must skip the insert' was a misleading read of an
		environment problem."""
		import threading
		import time
		from ffl_core.compliance.disposition_lock import book_disposition_once
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		ser = "MG-CONC-A"
		disp_id = "FB-MG-CONC"
		company = self.company
		customer = MULTIGUN_CUSTOMER
		# Bare committed Serial No (no Stock Entry → minimal committed footprint);
		# the disposition submit only needs the Serial No to exist.
		if not frappe.db.exists("Serial No", ser):
			frappe.get_doc({"doctype": "Serial No", "serial_no": ser,
				"item_code": MULTIGUN_ITEM}).insert(ignore_permissions=True)
		frappe.db.commit()  # load-bearing: worker connections must see the fixture
		errors: list[str] = []
		calls: list[int] = []  # insert_fn call count — the loser must NOT call it
		barrier = threading.Barrier(2)

		def _insert():
			calls.append(1)
			time.sleep(0.4)  # hold the critical section so the other worker contends
			disp = frappe.get_doc({
				"doctype": "FFL Disposition", "disposition_date": frappe.utils.now_datetime(),
				"disposition_type": "Retail Sale", "company": company, "customer": customer,
				"item": MULTIGUN_ITEM, "serial": ser, "fastbound_id": disp_id,
				"form_4473_status": "Completed", "nics_status": "Proceed",
			})
			disp.flags.ignore_permissions = True
			disp.insert()
			disp.submit()
			return disp.name

		def _worker():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				barrier.wait(timeout=15)
				book_disposition_once(
					dedup_key=f"{disp_id}|{ser}",
					exists_filters={"fastbound_id": disp_id, "serial": ser},
					insert_fn=_insert)
				frappe.db.commit()
			except Exception as exc:  # noqa: BLE001
				errors.append(repr(exc))
				try:
					frappe.db.rollback()
				except Exception:  # noqa: BLE001
					pass
			finally:
				frappe.destroy()

		from ffl_core.compliance import disposition_lock
		lock_records: list[tuple[int, bool]] = []
		real_get_lock = disposition_lock._get_lock

		def _recording_get_lock(lock_name):
			got = real_get_lock(lock_name)
			lock_records.append((threading.get_ident(), got))
			return got

		t1 = threading.Thread(target=_worker)
		t2 = threading.Thread(target=_worker)
		try:
			with patch.object(disposition_lock, "_get_lock", _recording_get_lock):
				t1.start()
				t2.start()
				t1.join()
				t2.join()
			frappe.db.rollback()  # main conn: fresh snapshot to see the workers' commits
			n = frappe.db.count("FFL Disposition", {"fastbound_id": disp_id, "serial": ser})
			# Environment triage FIRST: a worker that never held the lock (even
			# after the retry) means serialization wasn't exercisable here — the
			# assertions below would misreport that as a product failure.
			grants = _final_lock_grants(lock_records)
			if len(grants) != 2 or not all(grants.values()):
				self.fail("advisory lock unavailable on this runner — serialization not "
					f"exercised (final grant per worker: {grants}, raw: {lock_records}); "
					"see 'ffl_core.disposition_lock' log warnings for the reason. "
					f"errors={errors}")
			# The lock made the LOSER skip the insert entirely (insert_fn called once),
			# so there's exactly one row and no naming-series deadlock. Without the
			# lock both call insert_fn (calls==2) and one dies on a tabSeries deadlock —
			# so `calls==1` is the assertion with teeth, not just the row count.
			self.assertEqual(len(calls), 1,
				f"loser must skip the insert; insert_fn calls={len(calls)} errors={errors}")
			self.assertEqual(n, 1, f"expected exactly ONE disposition, got {n}; errors={errors}")
			self.assertEqual(errors, [], f"concurrent bookings must not error; got {errors}")
		finally:
			frappe.db.delete("FFL Disposition", {"fastbound_id": disp_id})
			frappe.db.delete("Serial No", {"name": ser})
			frappe.db.commit()


class FB4473AuthoritativeItemLookup(FrappeTestCase):
	"""The authoritative gun list behind a 4473 disposition.

	A 4473 (to-individual) disposition is not retrievable via
	GET /Dispositions/{id} — it 404s (a past incident). So the guns
	on it are resolved from the other side of the relation: ask the bound book
	which items carry this dispositionId. These tests mock only the HTTP session.
	"""

	def _serials(self, disposition_id, pages, *, from_settings_error=None):
		"""Run the real lookup with a replaying session; returns (serials, session)."""
		if from_settings_error is not None:
			with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
					side_effect=from_settings_error), \
			     patch.object(fb_webhook, "_log_error"):
				return fb_webhook._fetch_disposition_serials(
					disposition_id, is_4473=True), None
		client, session = _items_client(pages)
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client), \
		     patch.object(fb_webhook, "_log_error"):
			serials = fb_webhook._fetch_disposition_serials(disposition_id, is_4473=True)
		return serials, session

	def test_returns_every_serial_on_the_disposition(self):
		serials, session = self._serials("D-AUTH-1", [
			{"items": [
				{"id": "FBIT-A1", "serial": "AUTH-A", "dispositionId": "D-AUTH-1"},
				{"id": "FBIT-B1", "serial": "AUTH-B", "dispositionId": "D-AUTH-1"},
			]},
			{"items": []},
		])
		self.assertEqual(sorted(serials), ["AUTH-A", "AUTH-B"])
		params = session.request.call_args_list[0].kwargs.get("params") or {}
		self.assertEqual(params.get("dispositionId"), "D-AUTH-1")

	def test_drops_rows_belonging_to_other_dispositions(self):
		"""FastBound silently ignores query filters it doesn't honour (the
		?fflNumber= precedent documented on find_item_by_serial). If that happens
		here the response is the whole bound book — every row must be re-checked,
		because treating a stranger's gun as part of this sale would mint a bogus
		Retail Sale disposition and satisfy the 4473 gate for a gun with no 4473."""
		serials, _ = self._serials("D-AUTH-2", [
			{"items": [
				{"id": "FBIT-MINE", "serial": "AUTH-MINE", "dispositionId": "D-AUTH-2"},
				{"id": "FBIT-OTHER", "serial": "AUTH-OTHER", "dispositionId": "D-SOMEONE-ELSE"},
				{"id": "FBIT-NONE", "serial": "AUTH-NONE", "dispositionId": None},
			]},
			{"items": []},
		])
		self.assertEqual(serials, ["AUTH-MINE"])

	def test_stops_paging_once_a_page_yields_no_match(self):
		"""An ignored filter would otherwise walk the entire bound book inside a
		webhook. The first page with zero matching rows ends the scan."""
		serials, session = self._serials("D-AUTH-3", [
			{"items": [{"id": "FBIT-X", "serial": "AUTH-X", "dispositionId": "D-AUTH-3"}]},
			{"items": [{"id": "FBIT-Y", "serial": "AUTH-Y", "dispositionId": "D-NOPE"}]},
			{"items": [{"id": "FBIT-Z", "serial": "AUTH-Z", "dispositionId": "D-AUTH-3"}]},
		])
		self.assertEqual(serials, ["AUTH-X"])
		self.assertEqual(session.request.call_count, 2,
			"must stop at the first page with no matching rows, not keep sweeping")

	def test_degrades_to_empty_when_fastbound_errors(self):
		"""A webhook must never raise: coverage falls back to the payload serials."""
		client, _ = _items_client([])
		failing = MagicMock()
		failing.status_code = 500
		failing.text = "boom"
		client.session.request.side_effect = [failing]
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client), \
		     patch.object(fb_webhook, "_log_error") as logged:
			serials = fb_webhook._fetch_disposition_serials("D-AUTH-4", is_4473=True)
		self.assertEqual(serials, [])
		self.assertTrue(logged.called, "a failed lookup must be logged, not swallowed")

	def test_rows_that_match_nothing_are_logged_not_silent(self):
		"""Rows came back, none carried our dispositionId → the filter is being
		ignored (or the field moved), and coverage silently degrades to the partial
		payload. That is this whole change failing closed with no trace — the exact
		shape of the dead fallback it replaced, which survived unnoticed for months.
		It must leave a log line, and it must be distinguishable from 'this 4473 has
		no other guns' (an empty page, which is NOT logged)."""
		client, _ = _items_client([
			{"items": [
				{"id": "FBIT-NOFILTER-1", "serial": "SOMEONE-ELSES-GUN"},
				{"id": "FBIT-NOFILTER-2", "serial": "ANOTHER-STRANGER"},
			]},
		])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client), \
		     patch.object(fb_webhook, "_log_error") as logged:
			serials = fb_webhook._fetch_disposition_serials("D-AUTH-NOFILTER", is_4473=True)
		self.assertEqual(serials, [])
		self.assertTrue(logged.called,
			"a lookup that returned rows but matched none of them is "
			"indistinguishable from 'no guns' unless it is logged")

	def test_empty_result_is_not_logged_as_an_anomaly(self):
		"""The counterpart: a 4473 whose only gun is already in the payload comes back
		as an empty page. That is normal — logging it would train people to ignore the
		signal that matters."""
		client, _ = _items_client([{"items": []}])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client), \
		     patch.object(fb_webhook, "_log_error") as logged:
			serials = fb_webhook._fetch_disposition_serials("D-AUTH-EMPTY", is_4473=True)
		self.assertEqual(serials, [])
		self.assertFalse(logged.called, "an empty bound-book page is not an anomaly")

	def test_lookup_uses_the_fail_fast_client(self):
		"""This runs inside a webhook holding row locks on the invoice a cashier is
		waiting on, so it must not inherit the shared client's 5-retry ~18s budget."""
		client, _ = _items_client([{"items": []}])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client) as fast, \
		     patch.object(fb_webhook.FastBoundClient, "from_settings") as shared:
			fb_webhook._fetch_disposition_serials("D-AUTH-FAST", is_4473=True)
		self.assertTrue(fast.called, "must build the fail-fast client")
		self.assertFalse(shared.called, "must not use the retry-storm client")
		self.assertEqual(fast.call_args.kwargs.get("timeout"),
			fb_webhook._4473_LOOKUP_TIMEOUT_SECONDS)

	def test_fail_fast_client_really_has_no_retry_budget(self):
		"""The test above pins the WIRING but mocks the constructor it names, so it
		would still pass if from_settings_fast quietly grew a retry adapter. Build
		the real thing and inspect it.

		Sibling: test_pos_4473_recovery.test_probe_client_fails_fast_instead_of_retrying
		covers the counter's ⟳ button through the same classmethod. That one goes
		through the real _probe_client and additionally pins PROBE_TIMEOUT_SECONDS;
		this one owns the control assertion below (the shared client really does
		carry a retry budget), which is what keeps the comparison from being vacuous.
		"""
		from ffl_integrations.fastbound.client import FastBoundConfig

		cfg = _fb_config(timeout=30)
		with patch.object(FastBoundConfig, "from_settings", return_value=cfg):
			fast = FastBoundClient.from_settings_fast(timeout=5)
			shared = FastBoundClient.from_settings()
		self.assertEqual(fast.config.timeout, 5, "the timeout override must land")
		for prefix, adapter in fast.session.adapters.items():
			total = getattr(adapter.max_retries, "total", None)
			self.assertIn(total, (0, None, False),
				f"fail-fast client must not retry ({prefix} → total={total})")
		# Control: the shared client is the thing we are refusing to inherit from.
		shared_totals = [getattr(a.max_retries, "total", None)
			for a in shared.session.adapters.values()]
		self.assertTrue(any(t for t in shared_totals),
			"control failed — the shared client should carry a retry budget, so this "
			"comparison is not vacuous")

	def test_non_string_disposition_id_does_not_raise(self):
		"""The id comes off a webhook payload; its type is not ours to assume. An
		AttributeError here would escape the handler and roll the event back."""
		client, session = _items_client([{"items": []}])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client):
			self.assertEqual(fb_webhook._fetch_disposition_serials(12345, is_4473=True), [])
		self.assertTrue(session.request.called, "a numeric id should still be queried")

	def test_non_string_payload_item_fields_do_not_raise(self):
		"""Payload item fields are wire data — their types are not ours to assume.

		A numeric `id` / `externalId` / `serial` used to blow up on `.strip()` inside
		_our_serials_from_fb_items. That AttributeError escapes the handler, and
		because fastbound_event answers 200 even on handler errors (deliberately —
		FastBound disables a webhook after 45 failed deliveries), FastBound never
		redelivers: the event is lost silently and the sale falls back to a manual
		manager override. Resolution may fail, but it must fail as "no match", not
		as an exception.
		"""
		resolved = fb_webhook._our_serials_from_fb_items([
			{"id": 12345, "externalId": 678, "serial": 90210},
			{"item": {"id": None, "serial": 4473}},
		])
		self.assertEqual(resolved, ["90210", "4473"],
			"numeric fields must be coerced, not raise")
		# The FB-id map on the disposal path takes the same wire data.
		self.assertEqual(
			fb_webhook._fb_id_serial_map([{"id": 12345, "serial": 90210}]),
			{"12345": "90210"})

	def test_degrades_when_the_http_layer_raises_anything(self):
		"""A raw network/library exception is NOT a FastBoundError, and the webhook
		contract is "always 200" — it must degrade like any other lookup failure.

		Regression: the first cut of this change caught only FastBound* exceptions.
		Local runs stayed green because the dev site has no FastBound credentials
		(FastBoundConfigError, caught), but CI's site could actually attempt the HTTP
		call, and the resulting error propagated out of _handle_disposition_committed
		as error=internal — taking down an event that has nothing to do with 4473
		coverage.
		"""
		client, _ = _items_client([])
		client.session.request.side_effect = RuntimeError("connection reset by peer")
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client), \
		     patch.object(fb_webhook, "_log_error") as logged:
			serials = fb_webhook._fetch_disposition_serials("D-AUTH-6", is_4473=True)
		self.assertEqual(serials, [])
		self.assertTrue(logged.called, "an unexpected crash must still be logged")

	def test_client_construction_failure_degrades(self):
		"""Same contract one layer up: from_settings() blowing up in an unforeseen
		way must not escape either."""
		serials, _ = self._serials("D-AUTH-7", [],
			from_settings_error=RuntimeError("settings doctype exploded"))
		self.assertEqual(serials, [])

	def test_degrades_to_empty_when_not_configured(self):
		from ffl_integrations.fastbound.client import FastBoundConfigError

		serials, _ = self._serials("D-AUTH-5", [],
			from_settings_error=FastBoundConfigError("no credentials"))
		self.assertEqual(serials, [])

	def test_blank_disposition_id_makes_no_http_call(self):
		client, session = _items_client([])
		with patch.object(fb_webhook.FastBoundClient, "from_settings_fast",
				return_value=client):
			self.assertEqual(fb_webhook._fetch_disposition_serials("", is_4473=True), [])
		self.assertEqual(session.request.call_count, 0)
