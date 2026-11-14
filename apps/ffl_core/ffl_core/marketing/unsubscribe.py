"""Unsubscribing from marketing mail: the link, and what ``/unsubscribe`` does with it (``www/unsubscribe``).

* ``GET /unsubscribe?t=<token>`` shows the store's name and an Unsubscribe button and changes nothing
  (mail scanners fetch every link in a message);
* ``POST`` to the same address unsubscribes: the page's button (with an optional reason), and the
  one-click unsubscribe mailbox providers send for ``List-Unsubscribe-Post`` (no page, no cookie, no
  CSRF token: a Guest request);
* ``?legacy=<hash>`` finds the contact by the previous mail plugin's hashes (``legacy_hash`` /
  ``legacy_hash_2``) instead of a token, for links in mail sent before the switch.

Every unsubscribe goes through :func:`ffl_core.marketing.contacts.set_status`. A validly signed token
for an address with no contact gets the same page and the same "done" as one that has a contact: the
page never says whether an address is on file.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url

from ffl_core.marketing import CONTACT
from ffl_core.marketing.contacts import UNSUBSCRIBED, event_detail, set_status
from ffl_core.marketing.signing import contact_token, email_of

PATH = "/unsubscribe"
ONE_CLICK = "One-Click"   # the body a provider posts: List-Unsubscribe=One-Click (RFC 8058)
# Optional reasons on the page; the key is what the form posts.
REASONS = {
	"too_many": "Too many emails",
	"not_relevant": "Not relevant to me",
	"never_signed_up": "I never signed up",
	"other": "Other",
}
_LEGACY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# Per IP address and hour, for requests that carry no valid token (a link that is not valid, an old
# ``legacy`` hash): those are the ones that guess. A validly signed token is never limited, GET or POST:
# one-click posts come from a mailbox provider's servers (few addresses, many readers) and a limit there
# would silently keep real people on the list.
LIMIT = 300


def url(email: str) -> str:
	"""The unsubscribe link (and ``List-Unsubscribe`` target) for *email*."""
	return get_url(PATH) + "?" + urlencode({"t": contact_token(email)})


def page(form: dict, method: str) -> dict:
	"""What ``/unsubscribe`` shows for a request, after doing what a POST asks. A request without a validly
	signed token is rate-limited per IP address (:data:`LIMIT` an hour); one with is not.

	Returns ``{"state": "invalid"}`` for a link that is not one; ``{"state": "ask", "t", "legacy",
	"reasons"}`` for a GET; ``{"state": "done"}`` after a POST."""
	form, method = dict(form or {}), (method or "GET").upper()
	if email_of(form.get("t")):
		return _page(form, method)
	return _unsigned(form, method)


@rate_limit(limit=LIMIT, seconds=3600)
def _unsigned(form: dict, method: str) -> dict:
	return _page(form, method)


def _page(form: dict, method: str) -> dict:
	token = form.get("t") or ""
	legacy = "" if token else (form.get("legacy") or "")
	email = _resolve(token, legacy)
	if not email:
		return {"state": "invalid"}
	if method != "POST":
		return {"state": "ask", "t": token, "legacy": legacy, "reasons": REASONS}
	one_click = form.get("List-Unsubscribe") == ONE_CLICK
	unsubscribe(email, reason=None if one_click else form.get("reason"),
		via="one_click" if one_click else ("legacy_link" if legacy else "page"))
	return {"state": "done"}


def _resolve(token, legacy) -> str | None:
	"""The address a link stands for, or None for a link that is not one. A signed token stands for
	its address whether or not it has a contact; a legacy hash only for a contact that has it."""
	if token:
		return email_of(token)
	if isinstance(legacy, str) and _LEGACY.match(legacy):
		return (frappe.db.get_value(CONTACT, {"legacy_hash": legacy}, "name")
			or frappe.db.get_value(CONTACT, {"legacy_hash_2": legacy}, "name"))
	return None


def unsubscribe(email: str | None, *, reason: str | None = None, via: str = "page") -> bool:
	"""Unsubscribe *email*'s contact (no-op without one); True when its status changed. *reason* is a
	key of :data:`REASONS` (anything else is dropped); *via* is how: ``page``, ``one_click``,
	``legacy_link``."""
	if not email or not frappe.db.exists(CONTACT, email):
		return False
	reason = reason if reason in REASONS else None
	label = REASONS[reason] if reason else ("One-click unsubscribe" if via == "one_click" else "Unsubscribe link")
	return set_status(email, UNSUBSCRIBED, reason=label,
		detail=event_detail(via=via, reason=reason, ip=getattr(frappe.local, "request_ip", None)))


def store_name() -> str:
	from ffl_core.store_identity import company_brand
	from ffl_core.utils import default_company

	try:
		return company_brand(default_company()).get("name") or ""
	except frappe.ValidationError:   # a site without a Company
		return ""


def invalid_message() -> str:
	return _("This unsubscribe link is not valid. If you copied it, check that you copied all of it.")
