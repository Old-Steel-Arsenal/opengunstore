"""Drop-ship status events: POS → WooCommerce (storefront plugin).

Until now the shop only heard from a Distributor Order once it shipped — one
private order note per parcel — and nothing at all when the distributor placed,
held, cancelled or errored it. This module posts a small signed JSON event to
the shop's storefront plugin on every status that matters, so the order screen,
the tracking page and the customer emails can follow the leg without an
operator opening the POS.

Contract (storefront plugin ``DropshipStatusWebhook``):

    POST {store_url}/wp-json/ffl-core/v1/dropship-status
    x-osa-timestamp: <unix seconds>
    x-osa-signature: hex(HMAC-SHA256(secret, "<timestamp>.<raw body>"))

    {
      "event_id":   "DO-2609-0004:Placed:2026-09-22 10:00:00",
      "do_name":    "DO-2609-0004",
      "woo_order_id": "1609",
      "site":       "retail",
      "distributor": "RSR",
      "status":     "Placed",
      "hold_reason": "",
      "lines": [{"item_code": "GLK19", "sku": "GLK19G5", "qty": 1,
                 "fulfillment": "direct", "tracking": ["9400…"]}],
      "tracking":   [{"number": "9400…", "carrier": "", "shipped_at": "2026-09-16"}]
    }

``event_id`` is the DO name, status and ``modified`` stamp, so a retried job
carries the same id and the shop can dedupe; a resend after a poll bumped
``modified`` gets a new id, and the shop then records it without a second note
(it notes only a changed status or a new tracking number). The secret lives on the store's
WooCommerce Settings (``outbound_webhook_secret``); blank means the feature is
off, which is the state every existing site is in until an operator sets it.

The existing per-parcel order notes are untouched — the storefront plugin still parses them
as a fallback, and a shop on an older plugin sees exactly what it saw before.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import frappe
import requests

from ffl_woo_sync.woocommerce.client import SITES, STORES, resolve_site

# Statuses the shop is told about. Draft/Queued are POS-internal; Acknowledged
# adds nothing over Placed that a shopper or a clerk acts on.
NOTIFY_STATES = frozenset({
	"Placed", "Hold", "Partially Shipped", "Complete", "Cancelled", "Error",
})

WEBHOOK_PATH = "/wp-json/ffl-core/v1/dropship-status"
# Comfortably above the shop's per-order lock wait (5 s) plus an order save,
# so an event the shop applied is not reported here as a failure.
TIMEOUT_SECONDS = 30


def should_notify(before_status: str | None, after_status: str | None,
		before_tracking=(), after_tracking=()) -> bool:
	"""A transition INTO a notifiable status, or a new tracking number on a leg
	already in one (parcel 2 of a Partially Shipped leg changes no status).
	Same-state re-saves (idempotent polling) and moves into POS-internal states
	stay silent."""
	after = (after_status or "").strip()
	if after not in NOTIFY_STATES:
		return False
	return (before_status or "").strip() != after or bool(set(after_tracking) - set(before_tracking))


def tracking_numbers(lines) -> set:
	"""Every tracking number on a DO's lines (their CSV echoes)."""
	return {n.strip() for line in lines or []
		for n in str((line or {}).get("tracking_numbers") or "").split(",") if n.strip()}


def sign(secret: str, timestamp: str, body: bytes) -> str:
	msg = f"{timestamp}.".encode() + body
	return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def build_event(do, order) -> dict:
	"""The event body for a Distributor Order and its source Woo Online Order.

	Pure: takes the two documents (or dicts) and returns JSON-ready data. Tracking
	is flattened from the lines' CSV echoes and deduped, the same reading
	``tasks.shipments_from_lines`` applies for the order notes.
	"""
	lines, tracking, seen = [], [], set()
	for line in do.get("lines") or []:
		numbers = []
		for raw in str(line.get("tracking_numbers") or "").split(","):
			number = raw.strip()
			if not number:
				continue
			numbers.append(number)
			if number not in seen:
				seen.add(number)
				# The DO lines carry no carrier (the shop infers one from the
				# number) and no ship date: the poll that first saw the number is
				# the closest stamp, and the shop keeps the first capture of a
				# number, so a later resend does not move it.
				tracking.append({
					"number": number,
					"carrier": "",
					"shipped_at": str(do.get("last_poll_at") or "")[:10],
				})
		lines.append({
			"item_code": str(line.get("item_code") or ""),
			"sku": str(line.get("sku") or ""),
			"qty": int(line.get("wish_qty") or 0),
			"fulfillment": str(line.get("fulfillment") or ""),
			"line_status": str(line.get("line_status") or ""),
			"tracking": numbers,
		})
	status = str(do.get("status") or "")
	return {
		"event_id": f"{do.get('name')}:{status}:{do.get('modified')}",
		"do_name": str(do.get("name") or ""),
		"woo_order_id": str(order.get("woo_order_id") or ""),
		"site": str(order.get("site") or "retail"),
		"distributor": str(do.get("distributor") or ""),
		"status": status,
		"hold_reason": str(do.get("hold_reason") or ""),
		"web_ref": str(do.get("web_ref") or ""),
		# The DO's own clock, so the shop can refuse an older event that lands
		# after a newer one (a retried Placed after Complete), plus when the
		# distributor took the order — the shop's grid shows it.
		"modified": str(do.get("modified") or ""),
		"placed_at": str(do.get("placed_at") or ""),
		"lines": lines,
		"tracking": tracking,
	}


def outbound_config(site: str) -> tuple[str, str] | None:
	"""``(store_url, secret)`` for the store, or None when events are off —
	no store for the site (blank = retail), no store URL, or a blank secret."""
	site = resolve_site(site)
	if site is None:
		return None
	store = STORES[site]
	try:
		settings = frappe.get_single(store["settings"])
	except Exception:  # noqa: BLE001 — settings not migrated on this site
		return None
	store_url = str(settings.get("store_url") or "").strip().rstrip("/")
	if not store_url:
		return None
	try:
		secret = settings.get_password("outbound_webhook_secret", raise_exception=False) or ""
	except Exception:  # noqa: BLE001 — field absent on an older schema
		return None
	return (store_url, secret) if secret else None


# The shop answered and will never take this event: a malformed event
# (400/422), a gone resource (410), or the plugin's own "No such order" 404.
# Retrying those hourly only fills the Error Log. A 404 WITHOUT that code is
# WordPress's rest_no_route — the plugin is not deployed yet, or rolled back —
# and stays retryable, as do 401/403 (a mismatched secret, fixed on one side)
# and 503 (the shop's per-order lock was busy).
PERMANENT_HTTP = frozenset({400, 410, 422})
NO_ORDER_CODE = "ffl_dropship_no_order"
BUSY_HTTP = 503


def is_permanent(response) -> bool:
	"""Did the shop refuse this event for good? (See :data:`PERMANENT_HTTP`.)"""
	code = getattr(response, "status_code", None)
	if code in PERMANENT_HTTP:
		return True
	if code != 404:
		return False
	try:
		return (response.json() or {}).get("code") == NO_ORDER_CODE
	except Exception:  # noqa: BLE001 — an HTML 404 page is not ours
		return False


def deliver(store_url: str, secret: str, event: dict, path: str = WEBHOOK_PATH) -> int:
	"""POST one signed event to ``path`` on the shop. Returns the HTTP status;
	raises on a transport error or a non-2xx."""
	body = frappe.as_json(event, indent=None).encode()
	ts = str(int(time.time()))
	headers = {
		"content-type": "application/json",
		"x-osa-timestamp": ts,
		"x-osa-signature": sign(secret, ts, body),
	}
	response = requests.post(store_url + path, data=body, headers=headers,
		timeout=TIMEOUT_SECONDS)
	response.raise_for_status()
	return response.status_code


def post_status_event(do_name: str, quiet: bool = False) -> dict:
	"""Job: read the DO fresh (after commit) and post its current status.

	Best effort — a shop outage is logged, never raised: the DO's own state
	machine must not depend on the shop answering. On success the DO records
	the status the shop acknowledged (``woo_notified_status``); the hourly
	:func:`resend_status_events` sweep re-posts any leg whose status moved on
	without that acknowledgement, so a shop outage is caught up rather than lost.

	``quiet`` (the sweep's retries): a transient failure was already logged by
	the first attempt, so a retry does not log it again every hour; a busy 503
	is never logged. A permanent refusal (:func:`is_permanent`) is logged once
	and recorded as settled, so the sweep stops asking. Any other failure
	CLEARS the acknowledgement, so the sweep resends — without that, a failed
	tracking-only event (same status as the one already acked) would look
	delivered and its parcel would never reach the shop.
	"""
	do = frappe.get_doc("Distributor Order", do_name)
	if do.get("source_doctype") != "Woo Online Order" or not do.get("source_name"):
		return {"sent": False, "reason": "not a web order"}
	order = frappe.db.get_value("Woo Online Order", do.source_name,
		["woo_order_id", "site"], as_dict=True) or {}
	config = outbound_config(str(order.get("site") or "retail"))
	if not config:
		return {"sent": False, "reason": "outbound webhook not configured"}
	event = build_event(do, order)
	try:
		code = deliver(config[0], config[1], event)
	except Exception as exc:  # noqa: BLE001
		response = getattr(exc, "response", None)
		http = getattr(response, "status_code", None)
		permanent = is_permanent(response)
		if permanent or (not quiet and http != BUSY_HTTP):
			frappe.log_error(
				title=(f"Drop-ship status event refused ({http}), not retried: {do_name} → {event['status']}"
					if permanent else f"Drop-ship status event failed: {do_name} → {event['status']}"),
				message=frappe.get_traceback())
		_record_notified(do_name, event["status"] if permanent else "")
		return {"sent": False, "reason": "refused" if permanent else "delivery failed", "http": http}
	_record_notified(do_name, event["status"])
	return {"sent": True, "http": code, "event_id": event["event_id"]}


def _record_notified(do_name: str, status: str) -> None:
	frappe.db.set_value("Distributor Order", do_name, "woo_notified_status", status,
		update_modified=False)


# The sweep looks back this far: a leg older than that has either been
# acknowledged or is an operator's problem by now, not a transport hiccup.
RESEND_LOOKBACK_DAYS = 7
RESEND_BATCH = 200


def _unacknowledged_names() -> list:
	return [r["name"] for r in _unacknowledged_query().run(as_dict=True)]


def _unacknowledged_query():
	"""Recent web-order legs whose status the shop has not acknowledged — the
	status/ack comparison is in SQL, so the batch holds only real work (acked
	legs stay in the window: every poll save bumps ``modified``). A failed
	delivery blanks the ack (see :func:`post_status_event`), which is how a
	tracking-only event lost to an outage comes back here."""
	from frappe.utils import add_days, now_datetime
	do = frappe.qb.DocType("Distributor Order")
	return (
		frappe.qb.from_(do)
		.select(do.name, do.status, do.woo_notified_status)
		.where(do.source_doctype == "Woo Online Order")
		.where(do.status.isin(sorted(NOTIFY_STATES)))
		.where(do.modified >= add_days(now_datetime(), -RESEND_LOOKBACK_DAYS))
		.where(do.woo_notified_status.isnull() | (do.woo_notified_status != do.status))
		.orderby(do.modified)
		.limit(RESEND_BATCH)
	)


def resend_status_events() -> dict:
	"""Scheduled (hourly): re-post the status of every recent web-order leg the
	shop has not acknowledged. No-op when no store has the webhook configured,
	so an unconfigured site never enqueues a job that would only log."""
	if not any(outbound_config(site) for site in SITES):
		return {"enqueued": 0, "reason": "outbound webhook not configured"}
	names = _unacknowledged_names()
	for name in names:
		frappe.enqueue(
			"ffl_woo_sync.woocommerce.status_events.post_status_event",
			queue="short", job_name=f"dist_status_resend_{name}", do_name=name, quiet=True)
	return {"enqueued": len(names)}
