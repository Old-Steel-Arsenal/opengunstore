"""Signing terminals: the store's tablets that customers sign agreements on.

A tablet keeps the page ``/agreement/terminal`` open (``www/agreement/terminal.*``).
It holds no staff session — only a device key that can do one thing: pick up the
signing link the counter sent to it.

* **Pairing** — a manager opens the Signing Terminal record and clicks Pair a
  tablet (:func:`start_pairing`): a 6-digit code, good once for
  :data:`PAIR_MINUTES`. Typed on the tablet (:func:`pair_terminal`), it gives the
  tablet a 256-bit device key, kept in the tablet's browser; only its sha256 is
  stored. Pairing again cuts the previous tablet off, and so does unticking Active.
* **Sending** — ``agreements.request_agreement(..., deliver="tablet", terminal=…)``
  makes the signing link as usual and drops it here (:func:`deliver`): one waiting
  link per tablet, the newest wins, gone after :data:`DELIVERY_MINUTES`.
* **Polling** — the tablet asks every two seconds (:func:`terminal_poll`) and gets
  the link once; the page then opens the ordinary signing screen with it and
  sends the device key along with the signature, so the copy records which tablet
  it was signed on (``agreements.submit_signature``).

# lazy: 2-second polling, fine for a few tablets per store; socket.io push is the
# upgrade path if a store runs many.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, escape_html, get_datetime, now_datetime

from ffl_core.roles import CUSTOMER_MANAGER_ROLES
from ffl_core.store_identity import company_brand

DOCTYPE = "Signing Terminal"
PAIR_MINUTES = 10
# Wrong codes the whole site accepts per PAIR_MINUTES window before pairing pauses:
# the per-IP limit alone would let guesses spread over many addresses add up.
PAIR_FAILURES_MAX = 50
_PAIR_FAILURES = "ffl-agr-pair-failures"
DELIVERY_MINUTES = 10
SEEN_EVERY = timedelta(minutes=1)
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CODE_RE = re.compile(r"^\d{6}$")


def _hash(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


def _pair_key(code: str) -> str:
	return f"ffl-agr-pair:{code}"


def _delivery_key(terminal: str) -> str:
	return f"ffl-agr-terminal:{terminal}"


def terminal_for_secret(secret) -> str | None:
	"""The active terminal this device key belongs to, or None."""
	if not isinstance(secret, str) or not _SECRET_RE.match(secret):
		return None
	return frappe.db.get_value(DOCTYPE, {"device_secret_hash": _hash(secret), "active": 1}, "name")


def deliver(terminal: str, token: str) -> None:
	"""Leave a signing link for *terminal* to pick up (replaces one already waiting)."""
	frappe.cache.set_value(_delivery_key(terminal), token, expires_in_sec=DELIVERY_MINUTES * 60)


def active_terminals() -> list[dict]:
	"""Tablets the counter can send to: [{"name", "pos_profile", "paired"}]."""
	return [{"name": r.name, "pos_profile": r.pos_profile, "paired": bool(r.device_secret_hash)}
		for r in frappe.get_all(DOCTYPE, filters={"active": 1},
			fields=["name", "pos_profile", "device_secret_hash"], order_by="name asc")]


@frappe.whitelist()
def list_terminals() -> list[dict]:
	"""Active signing tablets, for the Request agreement dialog.

	Returns:
	    [{"name", "pos_profile", "paired"}], by name.
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	return active_terminals()


@frappe.whitelist(methods=["POST"])
def start_pairing(terminal: str) -> dict:
	"""Manager starts pairing a tablet: a 6-digit code to type on it.

	Args:
	    terminal: Signing Terminal name; must be active.

	Returns:
	    {"code", "expires_in_minutes", "page": the URL to open on the tablet}.
	"""
	frappe.only_for(CUSTOMER_MANAGER_ROLES)
	if not frappe.db.get_value(DOCTYPE, terminal, "active"):
		frappe.throw(_("{0} is not an active signing tablet.").format(escape_html(terminal)))
	code = f"{secrets.randbelow(1_000_000):06d}"
	frappe.cache.set_value(_pair_key(code), {"terminal": terminal, "by": frappe.session.user},
		expires_in_sec=PAIR_MINUTES * 60)
	return {"code": code, "expires_in_minutes": PAIR_MINUTES,
		"page": frappe.utils.get_url("/kiosk")}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=600)
def pair_terminal(code: str) -> dict:
	"""The tablet trades a pairing code for its device key (Guest).

	The code works once. Any tablet paired to the same terminal before stops
	working. 10 tries per 10 minutes per IP address; after 50 wrong codes from
	anywhere in 10 minutes, pairing pauses until the window passes.

	Args:
	    code: The 6 digits shown on the Signing Terminal record.

	Returns:
	    {"secret": the device key (shown this once), "terminal_name"}.
	"""
	if _failures() >= PAIR_FAILURES_MAX:
		frappe.throw(_("Pairing is paused for a few minutes. Try again shortly."), title=_("Pairing code"))
	code = (code or "").strip() if isinstance(code, str) else ""
	entry = frappe.cache.get_value(_pair_key(code)) if _CODE_RE.match(code) else None
	if not entry or not frappe.db.get_value(DOCTYPE, entry.get("terminal"), "active"):
		_count_failure()
		frappe.throw(_("That code is not valid. Ask a manager for a new one."), title=_("Pairing code"))
	frappe.cache.delete_value(_pair_key(code))
	secret = secrets.token_urlsafe(32)
	frappe.db.set_value(DOCTYPE, entry["terminal"], {"device_secret_hash": _hash(secret),
		"paired_on": now_datetime(), "paired_by": entry.get("by"), "last_seen": now_datetime()},
		update_modified=False)
	frappe.cache.delete_value(_delivery_key(entry["terminal"]))
	return {"secret": secret, "terminal_name": entry["terminal"]}


# A plain Redis counter, the way frappe's rate limiter keeps its own (not get_value's pickles).
def _failures() -> int:
	return cint(frappe.cache.get(frappe.cache.make_key(_PAIR_FAILURES)))


def _count_failure() -> None:
	key = frappe.cache.make_key(_PAIR_FAILURES)
	# The window starts with the first failure; a counter that somehow has no expiry
	# gets one, so pairing can never stay paused for good.
	frappe.cache.set(key, 0, ex=PAIR_MINUTES * 60, nx=True)
	frappe.cache.incrby(key, 1)
	if frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, PAIR_MINUTES * 60)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30000, seconds=3600)
@rate_limit(key="secret", limit=2400, seconds=3600)
def terminal_poll(secret: str) -> dict:
	"""What the tablet should show now (Guest, every 2 seconds).

	Limited per tablet (2400 an hour) and per IP address (30000 an hour: a shop's
	tablets together, about 16 of them polling all hour).

	Args:
	    secret: The tablet's device key.

	Returns:
	    {"paired": false} for an unknown or switched-off tablet (the page goes back
	    to pairing); {"paired": true, "token"} when the counter sent a link (handed
	    out once); otherwise {"paired": true, "idle": true, "terminal_name",
	    "store_name"}.
	"""
	terminal = terminal_for_secret(secret)
	if not terminal:
		return {"paired": False}
	seen = frappe.db.get_value(DOCTYPE, terminal, "last_seen")
	if not seen or now_datetime() - get_datetime(seen) > SEEN_EVERY:
		frappe.db.set_value(DOCTYPE, terminal, "last_seen", now_datetime(), update_modified=False)
	key = _delivery_key(terminal)
	token = frappe.cache.get_value(key)
	if token:
		frappe.cache.delete_value(key)
		return {"paired": True, "token": token}
	return {"paired": True, "idle": True, "terminal_name": terminal,
		"store_name": company_brand(frappe.defaults.get_global_default("company")).get("name") or ""}
