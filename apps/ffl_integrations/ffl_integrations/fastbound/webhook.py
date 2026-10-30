"""FastBound → us webhook receiver.

Configure in FastBound: Settings → Webhooks → URL =
``https://<site>/api/method/ffl_integrations.fastbound.webhook.fastbound_event``.

Envelope (per FastBound docs): ``{id, eventName, accountNumber, timestampUtc,
data: {...}}`` — the event details live under ``data``. The signed input is
``<unix_ts>.<raw_body>`` (HMAC-SHA256, hex) in an
``X-FastBound-Signature: t=<ts>,v1=<hex>`` header; the secret is the 256-bit key
FastBound returns when the webhook is created (stored in FastBound Settings).
FastBound retries non-2xx up to ~23× over 72h and disables after 45 failures, so
we ALWAYS return 200 — even on handler errors (after logging) — to avoid retry
storms / auto-disable.
Ref: https://fastbound.help/en/articles/4782279-subscribing-to-events
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe
from frappe.utils import now_datetime

from ffl_core.alerts import safe_log_error
from ffl_core.compliance.disposition_lock import book_disposition_once
from ffl_core.utils import split_serials
from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfigError,
	FastBoundError,
)
from ffl_integrations.integration_gate import fastbound_enabled


SETTINGS_DOCTYPE = "FastBound Settings"


# Webhook-safe error logging: never blocks the webhook ack.
_log_error = safe_log_error


class WebhookAuthError(Exception):
	"""HMAC verification failed — return 401."""


# The body every failed signature check returns. Named rather than inlined because
# the switched-off branch of `fastbound_event` must answer with EXACTLY this string
# to stay indistinguishable from an enabled site refusing a bad signature; two
# spellings of the same sentence would quietly turn a guest-open endpoint back into
# an "is this store on FastBound?" oracle.
SIGNATURE_MISMATCH_ERROR = "HMAC signature mismatch"

# Cache key that throttles the "webhook_secret is blank" Error Log to one row an
# hour. The endpoint is guest-open, so without it every anonymous request would
# write a row — disk amplification anyone on the internet can drive.
_BLANK_SECRET_LOG_KEY = "fastbound_webhook:blank_secret_logged"


def _has_request() -> bool:
	"""Is there an HTTP request to read from?

	``frappe.request`` is a LocalProxy whose ``__bool__`` is False while unbound
	(frappe/utils/local.py), so ``getattr(frappe, "request", None)`` in boolean
	position was already a working guard — frappe itself uses that idiom. This is
	the same test made explicit: ask the Local the proxy stands in for, so the
	next reader does not have to know the proxy's truthiness rules to trust it.
	Reached from bench execute, the scheduler and the test runner, where the
	answer has to be "no headers, no body" rather than a 500.
	"""
	return getattr(frappe.local, "request", None) is not None


def _signature_header() -> str:
	"""The raw ``X-FastBound-Signature`` header value, or "" when absent.

	Reads through ``frappe.get_request_header`` unconditionally instead of gating
	on ``_has_request()``: that call is the seam every test patches to supply a
	header, and gating first would make a patched reader unreachable. Outside a
	request the unbound proxy raises instead of answering, so catch that and give
	the same answer a header-less request gets.
	"""
	try:
		return (
			frappe.get_request_header("X-FastBound-Signature")
			or frappe.get_request_header("x-fastbound-signature")
			or ""
		)
	except RuntimeError:
		return ""


def _auth_refusal(message: str) -> dict:
	"""Set 401 and return the refusal body — the single exit for every auth failure."""
	frappe.local.response["http_status_code"] = 401
	return {"ok": False, "error": message}


def _read_raw_body() -> bytes:
	"""Return the raw POST body bytes (HMAC must run over EXACTLY these bytes —
	re-serializing JSON would change whitespace and break the signature)."""
	return frappe.request.get_data() if _has_request() else b""


def _parse_signature_header(header_value: str) -> tuple[str, str]:
	"""Parse ``t=<ts>,v1=<hex>`` → (ts, sig). Raises on malformed input."""
	if not header_value:
		raise WebhookAuthError("Missing X-FastBound-Signature header")
	parts = dict(seg.split("=", 1) for seg in header_value.split(",") if "=" in seg)
	ts = parts.get("t", "").strip()
	sig = parts.get("v1", "").strip()
	if not ts or not sig:
		raise WebhookAuthError(f"Malformed X-FastBound-Signature: {header_value!r}")
	return ts, sig


def _verify_signature(raw_body: bytes, ts: str, sig_hex: str, secret: str) -> None:
	# Canonical payload per FastBound docs: "<ts>.<body>" (body is the raw bytes).
	canonical = f"{ts}.".encode("utf-8") + raw_body
	expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, sig_hex):
		raise WebhookAuthError(SIGNATURE_MISMATCH_ERROR)


def _get_webhook_secret() -> str:
	"""The configured HMAC secret, or a refusal indistinguishable from a bad one.

	"No secret configured" is answered with the ordinary mismatch message, not its
	own: it is a fact about how this store is set up, and the caller asking is
	anonymous by construction. Naming it would leave the same oracle the switched-
	off 404 used to be — "enabled but not finished configuring" is exactly the
	state a prober would most like to find. It is also honest: with no secret, no
	signature can verify, so refusing every request IS a signature mismatch.

	The operator still needs to know, so the real reason goes to the Error Log,
	where the daily digest and a human can see it and an anonymous caller cannot —
	once an hour, not once per request: this is a guest-open endpoint, and an
	unthrottled row per anonymous call is disk amplification (this box has filled
	its disk twice already).
	"""
	s = frappe.get_single(SETTINGS_DOCTYPE)
	secret = s.get_password("webhook_secret", raise_exception=False) or ""
	if not secret:
		if not frappe.cache.get_value(_BLANK_SECRET_LOG_KEY):
			frappe.cache.set_value(_BLANK_SECRET_LOG_KEY, 1, expires_in_sec=3600)
			_log_error(
				title="FastBound webhook: refused, webhook_secret is empty",
				message=("FastBound is switched ON but FastBound Settings.webhook_secret "
					"is blank, so no delivery can ever verify. The caller was answered "
					"with the ordinary signature-mismatch 401. (Logged at most once an "
					"hour; the refusals themselves are not logged.)"),
			)
		raise WebhookAuthError(SIGNATURE_MISMATCH_ERROR)
	return secret


def _disposition_id_from_event(data: dict | None) -> str | None:
	"""Extract the FB disposition id from the event payload. The exact field
	name varies slightly across event types — try the common ones."""
	if not data:
		return None
	for key in ("dispositionId", "id", "disposition_id"):
		val = data.get(key)
		if val:
			return val
	# Some events nest the disposition under a `disposition` object.
	disposition = data.get("disposition")
	if isinstance(disposition, dict):
		for key in ("id", "dispositionId"):
			if disposition.get(key):
				return disposition[key]
	return None


def _pdf_url_from_event(data: dict | None) -> str | None:
	if not data:
		return None
	# FB's Form4473CompletedEvent carries the PDF link as `apiDownloadUrl`; keep
	# the other names + the nested form4473 object as fallbacks.
	for key in ("apiDownloadUrl", "pdfUrl", "url", "downloadUrl"):
		if data.get(key):
			return data[key]
	form = data.get("form4473")
	if isinstance(form, dict):
		for key in ("apiDownloadUrl", "pdfUrl", "url"):
			if form.get(key):
				return form[key]
	return None


def _item_node(it: dict) -> dict:
	"""The flat fields dict for one item entry. FastBound sends firearm items either
	flat (ItemModel {id, serial}) or nested (DispositionItemModel {item:{id, serial}})."""
	return it["item"] if isinstance(it.get("item"), dict) else it


def _our_serials_from_fb_items(items: Any) -> list[str]:
	"""Resolve FastBound item entries to OUR Serial No names.

	Matching key priority, most-stable first:
	  1. the FastBound item id → Serial No.fastbound_item_id — stable across a
	     serial edit, and how inventory_sync / the disposal stock-sync already
	     match (see _fb_id_serial_map);
	  2. externalId → a Serial No name — we stamp externalId = our serial on the
	     acquisition push, so it survives a FastBound-side serial edit;
	  3. the bare `serial` string — LAST resort. FastBound's serial can diverge
	     from ours (a serial edited directly in FastBound, or a disambiguating
	     suffix we append on intake), and serials aren't unique across makers, so
	     a bare-serial match can MISS the gun (leaving its POS sale stuck at
	     form_4473_status=Pending) or hit the wrong one.

	The Draft POS Invoice line carries OUR serial, so the completed-4473 match
	MUST translate FastBound's item identity to ours before looking up invoices.
	"""
	out: list[str] = []
	if not isinstance(items, list):
		return out
	for it in items:
		if not isinstance(it, dict):
			continue
		node = _item_node(it)
		our = None
		# str() on every payload field: these come off the wire, so their types are
		# not ours to assume. A non-string id (FastBound sending a numeric item id,
		# or a shape change) would raise AttributeError here — and because
		# fastbound_event returns 200 even on handler errors (to avoid FastBound's
		# retry storm / auto-disable), FastBound would NOT redeliver. The event is
		# then lost silently and the sale falls back to a manual manager override.
		# Same hardening as _fetch_4473_disposition_items applies to its own id.
		fb_id = str(node.get("id") or "").strip()
		if fb_id:
			our = frappe.db.get_value("Serial No", {"fastbound_item_id": fb_id}, "name")
		if not our:
			ext = str(node.get("externalId") or "").strip()
			if ext and frappe.db.exists("Serial No", ext):
				our = ext
		if not our:
			our = str(node.get("serial") or "").strip() or None
		if our:
			out.append(our)
	return out


def _fb_id_serial_map(items: Any) -> dict[str, str]:
	"""Map FastBound item id -> serial from a payload's items. Items with no FB id
	are skipped: disposals are matched by the stable, unique FB item id stamped on
	the Serial No (fastbound_item_id), NOT the serial string — serials aren't unique
	across manufacturers and collate case/space-insensitively, so a bare-serial match
	could take the wrong gun out of stock."""
	out: dict[str, str] = {}
	if not isinstance(items, list):
		return out
	for it in items:
		if not isinstance(it, dict):
			continue
		node = _item_node(it)
		fb_id = str(node.get("id") or "").strip()  # payload types are not ours to assume
		if fb_id:
			out[fb_id] = str(node.get("serial") or "").strip()
	return out


def _fetch_disposition_items(disposition_id: str, *, is_4473: bool = False) -> list:
	"""A disposition's item list, fetched from FastBound when the webhook payload
	didn't carry items. Returns [] (logged) if FastBound is unreachable or not
	configured, so callers degrade to a no-op instead of raising inside the webhook.

	`is_4473=True` skips the fetch entirely: a 4473 (to-individual) disposition is
	NOT on the normal GET /Dispositions/{id} endpoint — that 404s for it (incident
	a past invoice), so this returns [] rather than
	recording a spurious "sync failed".

	Note this is NOT the same as "a 4473's items are unobtainable": they are, just
	from the other side of the relation — see `_fetch_4473_disposition_items`, which
	`_fetch_disposition_serials` routes 4473 callers to. This function keeps the
	no-op for its OTHER downstream, the disposal stock-sync (see
	`_fetch_disposition_fb_items`). Remove the guard if a future FastBound API
	exposes 4473 dispositions on this endpoint."""
	if is_4473:
		return []
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError as exc:
		_log_error(title="FastBound webhook: not configured", message=str(exc))
		return []
	try:
		disp = client.get_disposition(disposition_id) or {}
	except FastBoundError as exc:
		_log_error(title=f"FastBound webhook: GET Dispositions/{disposition_id} failed",
			message=str(exc))
		return []
	return disp.get("items") or []


# A 4473 carries a handful of guns, so the bound-book scan below is capped: if
# FastBound ever stops honouring the dispositionId filter we must not walk the
# whole book inside a webhook.
_4473_ITEM_PAGE_SIZE = 100
_4473_ITEM_MAX_PAGES = 3
# Per-request ceiling for the lookup. Both callers now run it BEFORE they take any
# row lock on the POS Invoice, so this budget is no longer what stands between a
# cashier and their own invoice — but it still bounds how long the whole webhook
# handler takes, and FastBound retries non-2xx deliveries ~23× / 72h and disables
# the webhook after 45 failures, so a handler that hangs feeds its own retry storm.
# Hence the fail-fast client (one attempt, short timeout) instead of the shared
# one's 5-retry ~18s budget.
_4473_LOOKUP_TIMEOUT_SECONDS = 5


def _fetch_4473_disposition_items(disposition_id: str) -> list:
	"""The items on a 4473 (to-individual) disposition, via ``GET /Items?dispositionId=``.

	Why this exists: a 4473 disposition is NOT retrievable via GET /Dispositions/{id}
	— it 404s, which is why _fetch_disposition_items
	short-circuits on is_4473. That left every "fetch the authoritative serial list"
	fallback in the coverage paths a permanent no-op, so coverage silently degraded
	to whatever the event payload happened to carry. When guns are added to a 4473 in
	more than one pass, form4473.completed can carry only some of them and the
	late-added gun ends up with no Retail Sale disposition — the sale then dies at the
	submit gate with the card already charged (FIT0039
	was added at 14:36, the payload held only U360725). So ask the relation from the
	other side: which bound-book items carry this dispositionId.

	FastBound SILENTLY IGNORES query filters it doesn't honour (documented on
	find_item_by_serial, the ``?fflNumber=`` precedent), and an ignored filter here
	would return the entire bound book. Every row is therefore re-checked client-side
	and the scan stops at the first page with no matching row. Degrading to "no items"
	is safe — callers fall back to the payload serials; degrading to "the whole book
	is on this 4473" would mint Retail Sale dispositions for strangers' guns and
	satisfy the 4473 gate for guns that never had one.

	Read-only, and NOT a source of NICS truth: whether the transfer may proceed only
	ever arrives on the form4473.completed payload. This answers WHICH guns are on the
	4473, never WHETHER the sale may go through.
	"""
	# str() not just .strip(): the id comes straight off a webhook payload, so its
	# type is not ours to assume. An AttributeError here would escape the handler and
	# roll the whole event back — the one failure mode this function exists to avoid.
	disposition_id = str(disposition_id or "").strip()
	if not disposition_id:
		return []
	try:
		client = FastBoundClient.from_settings_fast(timeout=_4473_LOOKUP_TIMEOUT_SECONDS)
	except FastBoundConfigError as exc:
		_log_error(title="FastBound webhook: not configured", message=str(exc))
		return []
	except Exception:  # noqa: BLE001 — see the contract note on the lookup below
		_log_error(title="FastBound webhook: 4473 item lookup could not build a client",
			message=frappe.get_traceback())
		return []
	wanted = disposition_id.casefold()
	out: list = []
	try:
		for page_no, page in enumerate(client.iter_items(
				page_size=_4473_ITEM_PAGE_SIZE,
				extra_params={"dispositionId": disposition_id})):
			matched = [
				it for it in page
				if str(_item_node(it).get("dispositionId") or "").strip().casefold() == wanted
			]
			if not matched:
				# iter_items only yields NON-empty pages (it returns on an empty one),
				# so this branch has exactly one cause: FastBound handed back rows and
				# not one of them carries our dispositionId — the filter is being
				# ignored, or the field moved. That silently empties the authoritative
				# list and drops us straight back to the partial-payload behaviour this
				# whole change exists to fix. Log it: a bounded-but-wrong result that
				# looks identical to "this 4473 has no other guns" is exactly how the
				# original dead fallback survived unnoticed for months.
				_log_error(
					title=f"FastBound webhook: dispositionId filter returned no matching "
						f"items for {disposition_id}",
					message=(f"Page {page_no} held {len(page)} row(s), none with "
						f"dispositionId={disposition_id!r}. FastBound may have stopped "
						"honouring the filter or changed the field — 4473 coverage is "
						"falling back to the event payload, which can be partial."),
				)
				break
			out.extend(matched)
			if page_no + 1 >= _4473_ITEM_MAX_PAGES:
				_log_error(
					title=f"FastBound webhook: 4473 item scan hit page cap for {disposition_id}",
					message=(f"Stopped after {_4473_ITEM_MAX_PAGES} pages with {len(out)} "
						"items — a 4473 with more guns than this needs the cap raised."),
				)
				break
	except FastBoundError as exc:
		_log_error(
			title=f"FastBound webhook: GET Items?dispositionId={disposition_id} failed",
			message=str(exc))
		# Keep whatever we already resolved: coverage is idempotent, so a partial
		# list still helps and a later event can finish the job.
	except Exception:  # noqa: BLE001 — the webhook contract is "always 200"
		# This lookup is a best-effort enrichment sitting in the middle of handlers
		# that must never fail. A raw network/library exception (ConnectionError,
		# Timeout, a mocked-out session in a test site) is NOT a FastBoundError and
		# would otherwise propagate out of the handler and turn the whole event into
		# error=internal — which is exactly what CI caught on the first cut of this
		# change: disposition.committed died on a runner that could actually attempt
		# the HTTP call. Degrade to the payload serials like every other failure.
		_log_error(
			title=f"FastBound webhook: 4473 item lookup crashed for {disposition_id}",
			message=frappe.get_traceback())
	return out


def _merge_serials(*lists: list[str]) -> list[str]:
	"""Ordered, de-duplicated union of serial lists.

	4473 coverage takes the UNION of the bound-book lookup and the event payload
	rather than preferring either one, because neither is reliably complete: a
	form4473.completed payload can hold only the guns added in an earlier pass
, and the lookup can come back short or empty when
	FastBound is unreachable or has stopped honouring the filter. Nothing is minted
	for a serial that isn't on a matched Draft POS Invoice, so a superset costs
	nothing — while a subset costs a stuck sale with the card already charged.
	"""
	return list(dict.fromkeys(s for lst in lists for s in lst if s))


def _authoritative_4473_serials(disposition_id: str, data: dict) -> list[str]:
	"""Every gun on this 4473, from both sources that know anything about it.

	The source list, its order, and the is_4473 routing flag live here and only
	here — both webhook handlers need exactly this composition, and having it in
	two places means a third source (or a change to what is_4473 means) has to be
	found twice. See `_merge_serials` for why the union rather than a preference.

	Callers must invoke this BEFORE their first write: it makes a network call, and
	everything after the first `set_value` holds row locks until commit.
	"""
	return _merge_serials(
		_fetch_disposition_serials(disposition_id, is_4473=True),
		_our_serials_from_fb_items(data.get("items")),
	)


def _fetch_disposition_serials(disposition_id: str, *, is_4473: bool = False) -> list[str]:
	# A 4473 needs the bound-book lookup; every other disposition type can use the
	# disposition endpoint. Deliberately NOT pushed down into _fetch_disposition_items:
	# that one also feeds the disposal stock-sync via _fetch_disposition_fb_items, where
	# making 4473 items suddenly resolvable would change which serials reach the
	# never-linked-4473 alarm (P5). Coverage is the only caller that needs this.
	if is_4473:
		return _our_serials_from_fb_items(_fetch_4473_disposition_items(disposition_id))
	return _our_serials_from_fb_items(_fetch_disposition_items(disposition_id, is_4473=is_4473))


def _fetch_disposition_fb_items(disposition_id: str, *, is_4473: bool = False) -> dict[str, str]:
	"""FB item id → serial for a disposition. Feeds the disposal stock-sync.

	⚠ `is_4473=True` still returns {} here, unlike its sibling
	`_fetch_disposition_serials`, which now resolves 4473 items via
	`_fetch_4473_disposition_items`. The asymmetry is deliberate: this map decides
	which serials reach `_sync_fb_direct_disposal_stock`, where a 4473 serial with
	no open POS Invoice raises the never-linked alarm (P5). Making 4473 items
	resolvable here would widen that alarm's trigger surface — a separate decision
	from 4473 coverage, and not one this change makes.
	"""
	return _fb_id_serial_map(_fetch_disposition_items(disposition_id, is_4473=is_4473))


def _event_is_4473(data: dict | None) -> bool:
	"""Whether the disposition in this event is a 4473 (to-individual) sale.

	4473 dispositions are NOT retrievable via GET /Dispositions/{id} (it 404s for
	them, a past incident), so callers must trust the event payload
	instead of fetching. FastBound marks them with ``is4473=true``; the flag may sit
	at the top level of ``data`` or on a nested ``disposition`` object."""
	if not data:
		return False
	nested = data.get("disposition") if isinstance(data.get("disposition"), dict) else None
	for container in (data, nested):
		if not isinstance(container, dict):
			continue
		val = container.get("is4473")
		if isinstance(val, bool):
			return val
		if isinstance(val, str) and val.strip().lower() == "true":
			return True
	return False


def _like_escape(value: str) -> str:
	"""Escape LIKE wildcards so a serial matches LITERALLY in the prefilter.
	Backslash FIRST (MariaDB's default LIKE escape char), then % and _ — else a
	serial like ``A\\B`` would be read as an escape and the row silently dropped
	from the superset (the token check can't recover a row that never came back)."""
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_draft_invoices_for_serials(serials: list[str]) -> dict[str, list[str]]:
	"""For each serial, find Draft POS Invoices whose items[].serial_no
	contains that serial. Returns {invoice_name: [matched_serial, ...]}, grouped
	to deduplicate when one invoice has multiple matched firearms, and holding
	only OUR stored serials that truly matched (not the whole line).

	A POS Invoice Item line can carry MULTIPLE serials newline-joined (ERPNext
	qty>1 semantics), so an exact `serial_no IN (...)` match would miss a two-gun
	line entirely ("J4940\\n7805C"). Pre-filter
	with an (escaped) LIKE superset, then split each line and keep only the target
	serials that are exact tokens — so a serial that is merely a substring of
	another line's serial can never false-hit.

	Case contract: matching is CASE-INSENSITIVE (both sides ``.casefold()``),
	restoring the historical ``serial_no IN (...)`` behavior — the column's
	``*_ci`` collation folded case, so ``j4940`` (FastBound) matched a stored
	``J4940``. Our serials round-trip through FastBound from the same source, so
	a case-only difference is not a real distinct gun; treating it as a miss
	would strand the exact sale this fix exists to unstick (lead-ratified, M1).
	The returned serial is always OUR stored line serial, so downstream mints the
	disposition on the canonical value.

	A Draft invoice (docstatus=0) matches regardless of its doc-level
	form_4473_status — the downstream _record_disposition_audit is idempotent
	on (fastbound_id, serial), so re-matching an invoice whose 4473 status
	already flipped to Completed is safe and necessary for multi-gun coverage.
	"""
	wanted = {s for s in serials if s}
	if not wanted:
		return {}
	wanted_cf = {s.casefold() for s in wanted}
	# LIKE is a SUPERSET filter (a token is always a substring of the field);
	# the exact-token check below makes the match precise. The column's ci
	# collation makes LIKE case-insensitive like the historical IN.
	rows = frappe.get_all(
		"POS Invoice Item",
		or_filters=[["serial_no", "like", f"%{_like_escape(s.casefold())}%"] for s in wanted],
		fields=["parent", "serial_no"],
	)
	if not rows:
		return {}
	parent_names = list({r.parent for r in rows})
	open_parents = set(frappe.get_all(
		"POS Invoice",
		filters={
			"name": ("in", parent_names),
			"docstatus": 0,
		},
		pluck="name",
	))
	hits: dict[str, list[str]] = {}
	for r in rows:
		if r.parent not in open_parents:
			continue
		matched = [s for s in split_serials(r.serial_no) if s.casefold() in wanted_cf]
		if not matched:
			continue  # LIKE false-positive (substring, not a real token)
		bucket = hits.setdefault(r.parent, [])
		for s in matched:
			if s not in bucket:
				bucket.append(s)
	return hits


def _record_disposition_audit(*, invoice_name: str, serials: list[str],
	disposition_id: str, nics_status: str = "Proceed") -> None:
	"""One FFL Disposition row per firearm SERIAL in the matched invoice,
	idempotent on (fastbound_id, serial).

	A single POS Invoice Item line can carry multiple serials newline-joined
	(qty>1), so split each line and mint one disposition per gun — each paired
	with its own item_code, never one whose `serial` is the raw "J4940\\n7805C"
	string."""
	wanted_cf = {s.casefold() for s in serials if s}
	if not wanted_cf:
		return
	parent = frappe.db.get_value(
		"POS Invoice", invoice_name, ["company", "customer"], as_dict=True,
	) or {}
	# (item_code, serial) for each requested gun actually on the invoice — split
	# each line so a two-gun line contributes BOTH serials. Case-insensitive match
	#, consistent with _find_draft_invoices_for_serials; the disposition is
	# minted on OUR stored line serial.
	targets: list[tuple[str, str]] = []
	seen: set[str] = set()
	for row in frappe.get_all(
		"POS Invoice Item",
		filters={"parent": invoice_name},
		fields=["item_code", "serial_no"],
	):
		for serial in split_serials(row.serial_no):
			cf = serial.casefold()
			if cf in wanted_cf and cf not in seen:
				seen.add(cf)
				targets.append((row.item_code, serial))
	if not targets:
		return
	now = now_datetime()
	for item_code, serial in targets:
		def _insert(item_code=item_code, serial=serial):
			disp = frappe.get_doc({
				"doctype": "FFL Disposition",
				"disposition_date": now,
				"disposition_type": "Retail Sale",
				"company": parent.get("company"),
				"customer": parent.get("customer"),
				"item": item_code,
				"serial": serial,
				"linked_pos_invoice": invoice_name,
				"form_4473_status": "Completed",
				"nics_status": nics_status,
				"fastbound_id": disposition_id,
				"fastbound_synced_at": now,
				"notes": f"POS Invoice {invoice_name}",
			})
			disp.flags.ignore_permissions = True
			disp.insert()
			# A completed 4473 with a NICS Proceed is a finished, legal sale — commit
			# the disposition to the bound book instead of leaving it in Draft. Safe
			# to submit: a Retail Sale skips the stock-out on submit (the POS Invoice
			# already moved the gun) and the FastBound push is skipped because
			# fastbound_id is set, so on_submit just finalises the A&D record.
			if disp.form_4473_status == "Completed" and disp.nics_status == "Proceed":
				disp.submit()
			return disp.name
		# DB-level idempotency on (fastbound_id, serial): serialise the check-then-
		# insert so a concurrent duplicate delivery can't double-mint (ledger #1).
		book_disposition_once(
			dedup_key=f"{disposition_id}|{serial}",
			exists_filters={"fastbound_id": disposition_id, "serial": serial},
			insert_fn=_insert,
		)


def _ensure_disposition_coverage(disposition_id: str, serials: list[str],
		nics_status: str = "Proceed") -> None:
	"""Create a Retail Sale FFL Disposition for EVERY firearm in `serials` that
	sits on a linked Draft POS Invoice — idempotent on (fastbound_id, serial).

	Pass the disposition's FULL firearm list (authoritative, from FastBound) so a
	multi-gun sale gets ALL its guns covered in one pass — not just the serials a
	partial webhook payload happened to carry, and regardless of whether the
	invoice's doc-level form_4473_status already flipped to Completed."""
	for invoice_name, matched_serials in _find_draft_invoices_for_serials(serials).items():
		_record_disposition_audit(
			invoice_name=invoice_name, serials=matched_serials,
			disposition_id=disposition_id, nics_status=nics_status)


# FastBound Form4473Responses enum → our nics_status. Only the proceed family
# legally allows the transfer; everything else must keep the order blocked.
_4473_RESPONSE_TO_NICS = {
	2: "Proceed", 5: "Proceed", 6: "Proceed", 7: "Proceed", 11: "Proceed",
	3: "Denied", 8: "Denied",
	4: "Cancelled", 9: "Cancelled",
	1: "Delayed", 10: "Delayed", 12: "Delayed",
}
_4473_PROCEED_RESPONSES = frozenset({2, 5, 6, 7, 11})


def _nics_from_4473_event(data: dict) -> tuple[str, bool]:
	"""Map a form4473.completed event's NICS outcome to (nics_status, is_proceed).

	A 4473 fires this event whether the determination is Proceed, Denied,
	Cancelled or Delayed (status=Terminated = Void / No Transfer). Only the
	proceed family allows the sale; anything else — or anything unrecognised —
	must block. Prefers the int `response` enum, falls back to `responseText`."""
	if data.get("status") == 3 or (data.get("statusText") or "").strip().lower() == "terminated":
		return "Cancelled", False  # Void / No Transfer
	resp = data.get("response")
	if isinstance(resp, int):
		return _4473_RESPONSE_TO_NICS.get(resp, "Delayed"), resp in _4473_PROCEED_RESPONSES
	text = (data.get("responseText") or "").strip().lower()
	if "deni" in text:
		return "Denied", False
	if "cancel" in text:
		return "Cancelled", False
	if text in ("proceed", "nfaexempt", "permitexempt", "delayedproceed", "deniedproceedoverturned"):
		return "Proceed", True
	return "Delayed", False  # unknown / missing → fail safe


def _handle_form4473_completed(data: dict) -> None:
	# In FB's Form4473CompletedEvent the top-level `id` is the 4473 id; the
	# disposition is nested under `disposition`. Reading the top-level id here
	# would query the wrong record, so go through the nested object.
	disposition = data.get("disposition") if isinstance(data.get("disposition"), dict) else {}
	disposition_id = disposition.get("id") or disposition.get("dispositionId")
	if not disposition_id:
		_log_error(
			title="FastBound form4473.completed without disposition.id",
			message=json.dumps(data)[:5000],
		)
		return
	pdf_url = _pdf_url_from_event(data)

	# Resolve the guns on this 4473 to OUR Serial No names, from BOTH sources, BEFORE
	# touching the database.
	#
	# The payload's own `items` is not trustworthy on its own: when guns are added to a
	# 4473 in more than one pass, form4473.completed can carry only the earlier batch
	# (a past invoice — FIT0039 was added at 14:36, the payload held only
	# U360725). The bound-book lookup is the authoritative side, but it can come back
	# short too (FastBound unreachable, filter degraded, replication lag). So take the
	# union and let it drive EVERYTHING downstream — the invoice match, the doc-level
	# fields, and the coverage minting alike.
	#
	# Order matters twice over:
	#   * the lookup must feed `matches`, not just coverage. A gun known only to the
	#     bound book sits on its own Draft invoice in the split-ticket case; if the
	#     match is computed from the payload alone, that invoice never gets its
	#     doc-level 4473 fields and the submit gate rejects it — the same counter
	#     symptom this change exists to remove. It also means an unresolvable payload
	#     serial can no longer short-circuit the handler before the lookup ever runs.
	#   * the lookup must happen before the first set_value below. It is a network
	#     call, and everything after this point holds row locks on the very POS Invoice
	#     the cashier is waiting on.
	serials = _authoritative_4473_serials(disposition_id, data)
	matches = _find_draft_invoices_for_serials(serials) if serials else {}
	nics_status, is_proceed = _nics_from_4473_event(data)
	# The durable 4473 record, written before anything can bail out: a 4473 whose
	# guns resolve to no Serial No here, that matched no draft (done straight in
	# FastBound), or whose draft is later deleted / redone still has to be on file
	# (ffl_core Form 4473 Log).
	_record_4473_log(data, disposition_id, serials, list(matches), nics_status, pdf_url)
	if not serials:
		_log_error(
			title=f"FastBound form4473.completed: disposition {disposition_id} has no item serials",
			message=json.dumps(data)[:5000],
		)
		return
	if not matches:
		_log_error(
			title=f"FastBound form4473.completed: no Draft POS Invoice matches serials {serials}",
			message=json.dumps(data)[:5000],
		)
		return

	for invoice_name, matched_serials in matches.items():
		prev_nics = frappe.db.get_value("POS Invoice", invoice_name, "nics_status")
		frappe.db.set_value("POS Invoice", invoice_name, {
			"fastbound_disposition_id": disposition_id,
			"form_4473_status": "Completed",
			"nics_status": nics_status,
			"fastbound_4473_pdf_url": pdf_url or "",
		}, update_modified=False)
		_notify_if_nics_cleared(invoice_name, prev_nics, nics_status)
		_notify_staff_of_nics(invoice_name, prev_nics, nics_status)
		frappe.publish_realtime(
			"doc_update", {"doctype": "POS Invoice", "name": invoice_name},
			after_commit=True,
		)

	# Mint the Retail Sale records for every covered gun. `serials` is already the
	# union resolved above, so no second lookup here. Only a proceed family outcome
	# may mint — a Denied/Delayed/Cancelled 4473 still records its verdict on the
	# invoice above, but must never produce the disposition that satisfies the gate.
	if is_proceed:
		_ensure_disposition_coverage(disposition_id, serials, nics_status)


def _record_4473_log(data: dict, disposition_id: str, serials: list[str],
		invoices: list[str], nics_status: str, pdf_url: str | None) -> None:
	"""Upsert this 4473's Form 4473 Log row (keyed by the FastBound form id, so a
	redelivery updates it) and queue the private PDF copy for after the commit.

	Isolated in a savepoint: the record must never cost the sale — if it cannot
	be written, the invoice still gets its 4473 verdict and the failure is logged."""
	from ffl_core.compliance.webhook_fields import form_4473_id
	from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473

	frappe.db.savepoint("form_4473_log")
	try:
		name = record_4473(
			form4473_id=str(data.get("id") or "") or form_4473_id(pdf_url),
			pos_invoices=invoices,
			customer=frappe.db.get_value("POS Invoice", invoices[0], "customer") if invoices else None,
			serials=serials,
			nics_status=nics_status,
			fastbound_disposition_id=disposition_id,
			pdf_url=pdf_url or "",
		)
	except Exception:  # noqa: BLE001 — see docstring
		frappe.db.rollback(save_point="form_4473_log")
		_log_error(title=f"Form 4473 Log: could not record the 4473 on disposition {disposition_id}",
			message=frappe.get_traceback())
		return
	frappe.enqueue(
		"ffl_integrations.fastbound.pdf_archive.store_4473_pdf",
		queue="short", enqueue_after_commit=True,
		job_id=f"4473-pdf:{name}", deduplicate=True, log_name=name,
	)


def _notify_if_nics_cleared(pos_invoice_name: str, prev_nics: str, new_nics: str) -> None:
	"""Email the buyer once their *delayed* background check clears. No-op for an
	immediate Proceed (buyer is at the counter) or any non-Proceed outcome."""
	if prev_nics == "Delayed" and new_nics == "Proceed":
		frappe.enqueue(
			"ffl_integrations.fastbound.customer_notify.notify_customer_nics_cleared",
			queue="long", enqueue_after_commit=True, pos_invoice_name=pos_invoice_name,
		)


def _notify_staff_of_nics(pos_invoice_name: str, prev_nics: str, new_nics: str) -> None:
	"""Ring the counter's bell on a NICS verdict landing on a sale: Proceed, Delayed,
	Denied, and a Delayed check clearing. A redelivery (status unchanged) stays quiet.
	The manager override never comes through here — that person is at the counter."""
	if new_nics == prev_nics or new_nics not in ("Proceed", "Delayed", "Denied"):
		return
	from ffl_core.alerts import notify_staff
	from ffl_core.roles import COUNTER_4473_ROLES

	buyer = frappe.db.get_value("POS Invoice", pos_invoice_name, "customer_name") or ""
	cleared = prev_nics == "Delayed" and new_nics == "Proceed"
	verdict = "Delayed NICS cleared: Proceed" if cleared else f"NICS {new_nics}"
	notify_staff("POS Invoice", pos_invoice_name, f"{verdict} — {buyer} ({pos_invoice_name})",
		COUNTER_4473_ROLES)


def _handle_disposition_committed(data: dict) -> None:
	disposition_id = _disposition_id_from_event(data)
	if not disposition_id:
		_log_error(
			title="FastBound disposition.committed without dispositionId",
			message=json.dumps(data)[:5000],
		)
		return

	now = now_datetime()
	# Only stamp nics_status='Proceed' on 4473-style sales (Retail Sale / Sale)
	# — for FFL-to-FFL transfers, returns, and destructions, NICS is meaningless
	# (no buyer to background-check) and writing 'Proceed' there pollutes the
	# field semantics for downstream reports.
	rows = frappe.get_all(
		"FFL Disposition",
		filters={"fastbound_id": disposition_id},
		fields=["name", "disposition_type"],
	)

	# Resolve the backstop's gun list BEFORE the writes below, same as
	# _handle_form4473_completed: this is a network call, and everything after this
	# point holds row locks until commit. `rows` is a read, so the retail gate can be
	# evaluated here without touching anything.
	retail_committed = any(r.disposition_type in ("Retail Sale", "Sale") for r in rows)
	coverage_serials: list[str] = []
	if retail_committed:
		coverage_serials = _authoritative_4473_serials(disposition_id, data)

	for row in rows:
		updates: dict = {"fastbound_synced_at": now}
		if row.disposition_type in ("Retail Sale", "Sale"):
			updates["nics_status"] = "Proceed"
		frappe.db.set_value("FFL Disposition", row.name, updates, update_modified=False)

	names = frappe.get_all(
		"POS Invoice",
		filters={"fastbound_disposition_id": disposition_id},
		pluck="name",
	)
	for name in names:
		prev_nics = frappe.db.get_value("POS Invoice", name, "nics_status")
		frappe.db.set_value("POS Invoice", name, {
			"nics_status": "Proceed",
		}, update_modified=False)
		_notify_if_nics_cleared(name, prev_nics, "Proceed")
		# The write above forces Proceed whatever the invoice held; the bell must not
		# announce a Denied or voided 4473 as cleared.
		if (prev_nics or "N/A") in ("N/A", "Delayed"):
			_notify_staff_of_nics(name, prev_nics, "Proceed")
		frappe.publish_realtime(
			"doc_update", {"doctype": "POS Invoice", "name": name},
			after_commit=True,
		)
	# The 4473's durable record follows the invoice: a Delayed buyer who cleared.
	# Delayed rows only — a voided / Denied 4473 on the same disposition stays what it was.
	if names:
		frappe.db.set_value("Form 4473 Log",
			{"fastbound_disposition_id": disposition_id, "nics_status": "Delayed"},
			"nics_status", "Proceed", update_modified=False)

	# Backstop — RETAIL 4473 sales only: cover any firearm on this committed
	# disposition that a partial form4473.completed missed. Gated on an existing
	# Retail Sale row for this fastbound_id so that none of the OTHER disposition
	# types — FFL Transfer, Return, Scrap, Gunsmithing, Dealer Transfer, NFA (the
	# full set, see the FFL Disposition doctype's select options) — can spuriously
	# create a Retail Sale record, which would satisfy the 4473 gate for a re-listed
	# gun. The list was resolved above, before the writes. Idempotent.
	if coverage_serials:
		_ensure_disposition_coverage(disposition_id, coverage_serials, "Proceed")

	# P5: once coverage has run, warn LOUDLY if a
	# linked POS Invoice still has a firearm serial with no matching Retail Sale
	# disposition — the serial on the invoice line doesn't match the serial the 4473
	# was run on (a mistyped/swapped serial, H5123 vs V2689). The submit hard-gate
	# (_require_disposition_coverage) is the guarantee; this surfaces the mismatch
	# EARLY so staff fix it before Complete Order rather than discovering it at submit.
	if names:
		_alert_invoice_serial_coverage(disposition_id, names)

	# Sync inventory for a gun disposed DIRECTLY in FastBound (no POS sale on our
	# side): take it out of ERPNext stock so the two systems agree. Derive is_4473
	# from the payload flag OR an existing retail committed row (belt-and-suspenders:
	# a real retail 4473 still HOLDs a mismatched serial even if a future payload drops
	# the is4473 flag).
	_sync_fb_direct_disposal_stock(
		disposition_id, data, is_4473=_event_is_4473(data) or retail_committed)


def _serial_on_open_pos_invoice(serial: str) -> bool:
	"""True if the serial sits on a not-yet-consolidated POS Invoice — our own
	in-flight sale/transfer owns its stock-out (the POS consolidation issues it),
	so the FastBound disposal sync must NOT also issue it."""
	parents = list({
		r.parent for r in frappe.get_all(
			"POS Invoice Item", filters={"serial_no": serial}, fields=["parent"])
	})
	if not parents:
		return False
	return bool(frappe.get_all(
		"POS Invoice",
		filters={"name": ("in", parents), "docstatus": ("in", [0, 1]),
			"consolidated_invoice": ("in", ["", None])},
		limit=1,
	))


def _invoice_firearm_serials(invoice_name: str) -> set[str]:
	"""The regulated-firearm line serials on a POS Invoice — the gate's own read
	(regulated_firearm_serials), so the alarm previews exactly what the gate will
	demand. Rows, not get_doc: a deleted invoice just reads as no serials."""
	from ffl_core.compliance.firearm import regulated_firearm_serials

	return set(regulated_firearm_serials(frappe.get_all(
		"POS Invoice Item", filters={"parent": invoice_name},
		fields=["item_code", "serial_no", "serial_and_batch_bundle"],
	)))


def _alert_invoice_serial_coverage(disposition_id: str, invoice_names: list[str]) -> None:
	"""P5: for each POS Invoice linked to this committed disposition, raise a loud,
	early alarm on any firearm serial that has NO matching committed Retail Sale
	disposition — i.e. the serial on the invoice line doesn't match the serial the
	4473 was run on (a mistyped/swapped serial: a past incident,
	H5123 vs V2689). Best-effort early warning; the submit hard-gate
	(_require_disposition_coverage) is the guarantee."""
	# The SAME predicate the submit gate uses — the alarm is just an earlier preview
	# of the gate, so they share covered_serials_for_invoice and can't drift.
	from ffl_core.compliance.firearm import covered_serials_for_invoice

	for invoice_name in invoice_names:
		firearm_serials = _invoice_firearm_serials(invoice_name)
		if not firearm_serials:
			continue
		covered = covered_serials_for_invoice(invoice_name, firearm_serials)
		uncovered = sorted(firearm_serials - covered)
		if uncovered:
			_raise_serial_mismatch_alarm(invoice_name, disposition_id, uncovered)


def _raise_serial_mismatch_alarm(invoice_name: str, disposition_id: str,
		uncovered_serials: list[str]) -> None:
	"""The three P5 alarm channels: a prominent Error Log, a comment on the invoice,
	and a realtime nudge to the POS. Each channel is best-effort — one failing must
	not suppress the others or break the webhook ack."""
	message = (
		f"Disposition {disposition_id} committed, but firearm serial(s) "
		f"{uncovered_serials} on POS Invoice {invoice_name} have NO matching completed "
		f"4473 (Retail Sale) disposition. The serial on the invoice line likely does "
		f"not match the serial the 4473 was run on (a mistyped or swapped serial). "
		f"Verify against FastBound before completing this sale — the submit gate will "
		f"block it otherwise."
	)
	_log_error(
		title=f"⚠️ 4473 SERIAL MISMATCH — POS Invoice {invoice_name}",
		message=message,
	)
	try:
		frappe.get_doc("POS Invoice", invoice_name).add_comment("Comment", text=message)
	except Exception:  # noqa: BLE001 — best-effort; the Error Log already fired
		pass
	try:
		frappe.publish_realtime(
			"ffl_serial_mismatch",
			{"pos_invoice": invoice_name, "disposition_id": disposition_id,
				"serials": uncovered_serials},
			after_commit=True,
		)
	except Exception:  # noqa: BLE001
		pass


def _raise_direct_4473_no_invoice_alarm(disposition_id: str, serial: str) -> None:
	"""P5 (never-linked variant): a 4473 disposition disposed a serial that no POS
	Invoice on our side carries. A 4473 retail sale must go through POS, so this is a
	suspected mistyped/swapped serial. Alarm loudly; the caller HOLDS the gun in stock
	(does not issue it) for manual reconciliation. Best-effort channels."""
	message = (
		f"FastBound 4473 disposition {disposition_id} disposed serial {serial}, but NO "
		f"POS Invoice on our side carries it. A 4473 retail sale must go through POS — "
		f"this is likely a mistyped/swapped serial on the invoice line. "
		f"{serial} is HELD in ERPNext stock (not auto-disposed); "
		f"reconcile against FastBound and correct the invoice serial."
	)
	_log_error(
		title=f"⚠️ 4473 DISPOSAL WITH NO POS INVOICE — serial {serial}",
		message=message,
	)
	try:
		frappe.publish_realtime(
			"ffl_serial_mismatch",
			{"disposition_id": disposition_id, "serial": serial, "no_invoice": True},
			after_commit=True,
		)
	except Exception:  # noqa: BLE001 — the Error Log already fired
		pass


def _sync_fb_direct_disposal_stock(disposition_id: str, data: dict,
		*, is_4473: bool | None = None) -> None:
	"""Take a firearm out of ERPNext stock when FastBound reports it disposed but
	OUR POS isn't handling it (a gun disposed directly in FastBound's UI).

	`is_4473` lets the caller override the payload-derived flag with a stronger
	signal (e.g. an existing Retail Sale disposition row for this fastbound_id);
	None (the default, and direct callers) falls back to the payload's is4473.

	Matches by the FastBound item id stamped on the Serial No (fastbound_item_id),
	exactly like the bound-book reconcile (inventory_sync) — never the serial
	string, which isn't unique and collates case/space-insensitively, so a bare
	match could issue the WRONG gun out. A firearm with no fastbound_item_id (never
	pushed to FastBound) is left alone: under-sync is the safe direction.

	Idempotent + race-safe: in_stock_targets returns only Active firearm serials
	(already-gone serials are skipped) and never returns stock out on consignment
	at a dealer (disposed in FastBound while still our asset is that stock's
	correct steady state, not a discrepancy — incident CONO-2026-01544); a serial
	owned by an open POS Invoice is skipped too, so a retail sale we're processing
	is never double-decremented (the POS consolidation still delivers its
	serial)."""
	from ffl_core.firearm import issue_serial
	from ffl_integrations.fastbound.inventory_sync import in_stock_targets

	# P5: a disposition LINKED to a POS Invoice
	# (fastbound_disposition_id) is a retail sale — the POS owns its stock-out, so this
	# is NOT a "direct FastBound disposal" and must issue nothing. Covers the LINKED
	# mismatch variant (the invoice links via one matching serial while another line's
	# serial is mistyped): the mismatch is surfaced by _alert_invoice_serial_coverage
	# and the gun is HELD, not auto-disposed. frappe.get_all ignores permissions
	# (Guest-safe). The NEVER-linked variant (a single-gun 4473 whose only serial was
	# mistyped, so nothing ever linked) is caught by the is_4473 hold-and-alarm below.
	if frappe.get_all("POS Invoice",
			filters={"fastbound_disposition_id": disposition_id}, limit=1):
		return

	# NOTE on outbound consignment (incident CONO-2026-01544, 2026-08-08): the
	# echo of a Dealer Transfer WE pushed for a consignment ship used to land
	# here and Material-Issue all 39 guns straight off the books ($22,323.85).
	# Every other disposition type has already issued its serial out by echo
	# time, so in_stock_targets' `status = 'Active'` filter drops it; a
	# consignment gun is deliberately left Active (it merely moved into the
	# dealer's warehouse and stays OUR asset), which made it the one kind this
	# function could still see in stock.
	#
	# There is deliberately NO extra guard at this line. in_stock_targets is
	# this function's only source of rows and now excludes consignment stock by
	# the canonical two-key predicate (consigned_out OR a consignment
	# warehouse) — set before the ship's own Material Transfer, so no echo can
	# outrun it. A guard here keyed on the disposition would have to match on
	# fastbound_id, which is written back only AFTER the push succeeds: an echo
	# that arrives first (or a worker killed before the write-back) would sail
	# straight through it. Fixing the source beats a second, racier door.

	if is_4473 is None:
		is_4473 = _event_is_4473(data)

	# P4: the webhook is allow_guest, so this runs as
	# Guest. issue_serial submits a Stock Entry whose auto-created Serial and Batch
	# Bundle runs its OWN permission check — it does NOT inherit the Stock Entry's
	# ignore_permissions — so a Guest hits PermissionError and the entire direct-
	# disposal stock sync dies. The path is HMAC-verified, so elevate to a system user
	# for this trusted server-to-server stock move. Scoped here (not inside
	# issue_serial), so the sole stock-out exit keeps its permission posture for its
	# other callers (consignment G guard, reconcile). Same set_user pattern as
	# osa_consignment's settlement (set_user inside the try so a partial failure is
	# still unwound by the finally).
	prev_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		# Prefer the FB item ids in the event payload; fall back to a disposition fetch.
		# A 4473 disposition 404s on the GET (P3), so mark the fetch is_4473 — for those
		# we rely solely on the payload's items (the POS owns a 4473 sale's stock-out
		# anyway; the _serial_on_open_pos_invoice guard below still applies).
		fb_items = _fb_id_serial_map(data.get("items")) or _fetch_disposition_fb_items(
			disposition_id, is_4473=is_4473)
		if not fb_items:
			return  # no FB item ids to match on → nothing we can safely sync
		for t in in_stock_targets(fb_items):
			if _serial_on_open_pos_invoice(t["serial"]):
				continue  # our POS owns this sale's stock-out
			if is_4473:
				# NEVER-linked 4473 mismatch: a 4473 (retail) disposition with NO linked
				# invoice and a serial on NO open invoice. A 4473 sale MUST go through POS,
				# so this is a suspected mistyped/swapped serial (H5123 vs V2689) — alarm +
				# HOLD; a human reconciles against FastBound. We never auto-dispose a
				# possibly-mistyped retail serial (under-sync is the safe direction).
				_raise_direct_4473_no_invoice_alarm(disposition_id, t["serial"])
				continue
			try:
				issue_serial(
					t["item_code"], t["serial"], t["warehouse"],
					remarks=f"FastBound disposition {disposition_id} (synced from FastBound)")
			except Exception:
				_log_error(
					title=f"FastBound disposal stock-sync failed for {t['serial']}",
					message=frappe.get_traceback())
	finally:
		frappe.set_user(prev_user)


_HANDLERS: dict[str, Any] = {
	"form4473.completed": _handle_form4473_completed,
	"disposition.committed": _handle_disposition_committed,
}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def fastbound_event() -> dict:
	"""Single FastBound webhook entry point (guest, HMAC-verified).

	Payload:
	    FastBound's webhook JSON body; the event name selects the handler.
	    The X-FastBound-Signature header must carry a valid HMAC over the
	    raw body (secret configured in FastBound Settings).

	Returns:
	    200 with a minimal body ({ok, event/handled/error}) on success.
	    401 when the HMAC signature is missing, malformed or fails — and also,
	    word for word, when the integration is switched off on this site (see
	    below). Every other failure path logs + returns 200 to keep FastBound
	    from retrying.
	"""
	# Switched off: this site has no FastBound relationship, so it must not accept
	# bound-book state from one. Refused BEFORE any secret is read, without reading
	# the body and without reaching a handler — a store that turned FastBound off
	# may well have handed that account to somebody else.
	#
	# The refusal is deliberately the SAME 401 an enabled site gives a bad
	# signature, character for character. The header is still parsed, which needs
	# no secret, so a missing or malformed header keeps its own message; anything
	# that parses gets the mismatch message, because without the secret we could
	# not have told a good signature from a bad one anyway. An anonymous prober
	# therefore learns nothing about whether this store runs FastBound. The
	# previous answer here — a distinctive 404 naming the integration — handed
	# that fact to anyone who asked, on an endpoint that is guest-open by
	# construction.
	#
	# Retry semantics are unchanged by the swap: FastBound treats every non-2xx
	# alike (retries ~23x over 72h, disables the webhook after 45 failures), so a
	# still-configured account behaves exactly as it did against the 404, and
	# nothing here acks an event that was never processed.
	if not fastbound_enabled():
		try:
			_parse_signature_header(_signature_header())
		except WebhookAuthError as exc:
			return _auth_refusal(str(exc))
		return _auth_refusal(SIGNATURE_MISMATCH_ERROR)

	raw_body = _read_raw_body()

	try:
		ts, sig_hex = _parse_signature_header(_signature_header())
		secret = _get_webhook_secret()
		_verify_signature(raw_body, ts, sig_hex, secret)
	except WebhookAuthError as exc:
		return _auth_refusal(str(exc))

	try:
		body = json.loads(raw_body or b"{}")
	except ValueError:
		_log_error(title="FastBound webhook: non-JSON body",
			message=(raw_body[:5000] if raw_body else "").decode("utf-8", errors="replace"))
		return {"ok": False, "error": "non-JSON body"}

	event_name = (body or {}).get("eventName") or (body or {}).get("event") or ""
	data = (body or {}).get("data") or {}
	handler = _HANDLERS.get(event_name)
	if handler is None:
		# Subscribed events we don't act on — just log + ack.
		_log_error(
			title=f"FastBound webhook: unhandled event {event_name}",
			message=json.dumps(body)[:5000],
		)
		return {"ok": True, "handled": False}

	try:
		handler(data)
		frappe.db.commit()
	except Exception as exc:  # noqa: BLE001 — we MUST return 200 to FastBound
		import traceback as _tb
		frappe.db.rollback()
		_log_error(
			title=f"FastBound webhook handler failed for {event_name}",
			message=f"{exc}\n\n{_tb.format_exc()}\n\nBody: {json.dumps(body)[:5000]}",
		)
		return {"ok": False, "error": "internal", "event": event_name}

	return {"ok": True, "event": event_name}
