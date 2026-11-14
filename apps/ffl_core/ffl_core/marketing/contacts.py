"""Marketing Contact: one per lowercase email address, its consent status, and the event log.

Status moves one way. ``Not Subscribed`` → ``Subscribed`` (an explicit consent) → ``Unsubscribed`` /
``Bounced`` / ``Complained`` (suppressed; ``Complained`` outranks ``Bounced`` outranks
``Unsubscribed``). No automatic path (a website form, a website event, an import, the store's own
customers) ever moves a contact down that order: :func:`set_status` answers False and changes nothing.
The only way back is a manager, by hand, to ``Subscribed``, with a reason, which leaves a ``Subscribed``
event on the log.

Every status change goes through :func:`set_status`; the DocType's ``validate`` refuses any other.
Entering a suppressed status also files a reference-level ``Email Unsubscribe`` (this contact only,
never global: a global one would stop the same address's receipts and agreement links), which Frappe's
mail queue honours for mail sent with ``reference_doctype="Marketing Contact"``.

Every change, and anything else worth keeping (a request, a send, an import), is a ``Marketing Event``.
An event may carry a ``dedupe_key``, unique on the table: a second insert of the same key is the same
thing happening again, and is not recorded twice (:func:`log_event`).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime, now_datetime, validate_email_address
from frappe.utils.data import get_system_timezone

from ffl_core.marketing import CONTACT, EVENT
from ffl_core.roles import MARKETING_MANAGER_ROLES

NOT_SUBSCRIBED = "Not Subscribed"
SUBSCRIBED = "Subscribed"
UNSUBSCRIBED = "Unsubscribed"
BOUNCED = "Bounced"
COMPLAINED = "Complained"
# In the order a contact may move through them without a manager.
STATUSES = (NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, BOUNCED, COMPLAINED)
RANK = {status: rank for rank, status in enumerate(STATUSES)}
SUPPRESSED = (UNSUBSCRIBED, BOUNCED, COMPLAINED)

# Marketing Event kinds besides the five statuses' own names.
REQUESTED, SENT, SKIPPED, OPENED, CLICKED, IMPORTED = "Requested", "Sent", "Skipped", "Opened", "Clicked", "Imported"

UNSUBSCRIBE = "Email Unsubscribe"
EMAIL_CAP = 140   # the name column
TEXT_CAP = 140    # a Data field
DETAIL_CAP = 500   # one value in an event's detail


def clip(value, cap: int = TEXT_CAP) -> str:
	"""Text trimmed and capped."""
	return str(value if value is not None else "").strip()[:cap]


def safe_text(value, cap: int = TEXT_CAP) -> str:
	"""Public text as stored: trimmed, HTML-escaped, and no longer than *cap* once escaped."""
	raw = clip(value, cap)
	out = escape_html(raw)
	while len(out) > cap:
		raw = raw[:-1]
		out = escape_html(raw)
	return out


def normalize_email(value) -> str:
	"""The address as stored (trimmed, lowercase), or "" when *value* is not exactly one valid address."""
	address = clip(value, 1000).lower()
	if not address or len(address) > EMAIL_CAP or any(c in address for c in " ,;<>\"()[]\\"):
		return ""
	return address if validate_email_address(address) == address else ""


def to_system_datetime(value) -> datetime | None:
	"""An ISO 8601 time (with or without an offset) as a naive datetime in the site's timezone; None
	when *value* is not one. A time without an offset is taken as already local."""
	if not value:
		return None
	try:
		when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
	except ValueError:
		return None
	if when.tzinfo:
		when = when.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	return when


def event_detail(**fields) -> str | None:
	"""An event's detail: the non-empty fields as JSON, every string escaped and capped."""
	out = {k: safe_text(v, DETAIL_CAP) if isinstance(v, str) else v for k, v in fields.items() if v not in (None, "", [], {})}
	return frappe.as_json(out, indent=None) if out else None


def is_suppressed(status: str | None) -> bool:
	return status in SUPPRESSED


def _doc(contact):
	if isinstance(contact, str):
		return frappe.get_doc(CONTACT, normalize_email(contact) or contact)
	return contact


def get_or_create(email: str, *, first_name: str | None = None, last_name: str | None = None,
		source: str | None = None, customer: str | None = None):
	"""The Marketing Contact for *email*, made ``Not Subscribed`` when there is none. Fills a blank
	first / last name, customer or source; never overwrites one, never changes the status. Raises
	``frappe.InvalidEmailAddressError`` for an address that is not one."""
	address = normalize_email(email)
	if not address:
		frappe.throw(_("Not a valid email address: {0}").format(escape_html(clip(email))),
			frappe.InvalidEmailAddressError)
	fills = {"first_name": clip(first_name), "last_name": clip(last_name), "source": clip(source),
		"customer": customer or ""}
	if frappe.db.exists(CONTACT, address):
		doc = frappe.get_doc(CONTACT, address)
		blanks = {k: v for k, v in fills.items() if v and not doc.get(k)}
		if blanks:
			doc.update(blanks)
			doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc({"doctype": CONTACT, "email": address, "status": NOT_SUBSCRIBED,
		**{k: v for k, v in fills.items() if v}})
	frappe.db.savepoint("mkt_contact_new")
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:   # made by a concurrent request since the check
		frappe.db.rollback(save_point="mkt_contact_new")
		frappe.clear_last_message()
		return get_or_create(address, first_name=first_name, last_name=last_name, source=source, customer=customer)
	return doc


def set_status(contact, status: str, *, reason: str | None = None, channel: str | None = None, manual: bool = False,
		occurred_at=None, detail: str | None = None, dedupe_key: str | None = None) -> bool:
	"""Move *contact* (a Marketing Contact or its email) to *status*; True when it moved.

	Up the order (``STATUSES``) always moves. Down the order moves only with *manual*, only from a
	suppressed status to ``Subscribed``, only for a manager (``MARKETING_MANAGER_ROLES``) and only
	with a *reason*; refused otherwise. Without *manual* a move down is a no-op (False): an automatic
	path never undoes a suppression. Staying ``Subscribed`` with a *channel* refreshes the consent
	time when *occurred_at* is later than the one on file.

	A move writes one Marketing Event of the new status's kind (*detail*, *dedupe_key* and
	*occurred_at* go on it) and files (suppressed) or removes (put back) the contact's
	reference-level ``Email Unsubscribe``. *reason* is stored on the contact; *channel* is where a
	consent came from.
	"""
	if status not in RANK:
		frappe.throw(_("Unknown marketing status: {0}").format(escape_html(clip(status))))
	doc = _doc(contact)
	old = doc.status or NOT_SUBSCRIBED
	when = get_datetime(occurred_at) if occurred_at else now_datetime()
	reason = clip(reason)
	if status == old:
		if status == SUBSCRIBED and channel and (not doc.consent_at or get_datetime(doc.consent_at) < when):
			doc.consent_at, doc.consent_channel = when, clip(channel)
			_save(doc)
		return False
	if RANK[status] < RANK[old]:
		if not manual:
			return False
		if status != SUBSCRIBED or old not in SUPPRESSED:
			frappe.throw(_("A contact can only be put back from Unsubscribed, Bounced or Complained, and only to Subscribed."))
		frappe.only_for(MARKETING_MANAGER_ROLES)
		if not reason:
			frappe.throw(_("Say why this contact may be emailed again."), frappe.MandatoryError)
	doc.status, doc.status_changed_at, doc.status_reason = status, when, reason or None
	if status == SUBSCRIBED:
		doc.consent_at, doc.consent_channel = when, clip(channel) or ("manual" if manual else None)
	_save(doc)
	if status in SUPPRESSED:
		_suppress_mail(doc.name)
	else:
		frappe.db.delete(UNSUBSCRIBE, {"email": doc.name, "reference_doctype": CONTACT, "reference_name": doc.name})
	if manual and not detail:
		detail = event_detail(reason=reason, by=frappe.session.user, previous=old)
	log_event(doc.name, status, occurred_at=when, detail=detail, dedupe_key=dedupe_key)
	return True


def _save(doc) -> None:
	doc.flags.via_set_status = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.via_set_status = False


def _suppress_mail(email: str) -> None:
	"""The second gate for a suppressed contact: Frappe drops a recipient with an ``Email Unsubscribe``
	for the same reference from mail sent on this contact. Reference-level, never global."""
	if not frappe.db.exists(UNSUBSCRIBE, {"email": email, "reference_doctype": CONTACT, "reference_name": email}):
		frappe.get_doc({"doctype": UNSUBSCRIBE, "email": email, "reference_doctype": CONTACT,
			"reference_name": email, "global_unsubscribe": 0}).insert(ignore_permissions=True)


def carry_suppression(old_email: str, new_email: str) -> bool:
	"""A customer's new address takes their old address's suppression (an unsubscribe follows the
	person; a subscription does not, the new address never consented). True when the new address's
	contact was suppressed by this call."""
	old = normalize_email(old_email)
	status = frappe.db.get_value(CONTACT, old, "status") if old else None
	if not is_suppressed(status) or normalize_email(new_email) in ("", old):
		return False
	contact = get_or_create(new_email)
	return set_status(contact, status, reason=_("Carried over from {0}").format(old))


def log_event(contact: str, kind: str, *, mail: str | None = None, campaign: str | None = None,
		occurred_at=None, detail: str | None = None, dedupe_key: str | None = None) -> str | None:
	"""Append a Marketing Event; its name, or None when *dedupe_key* is already on file (the same
	thing happened before and was recorded then)."""
	if dedupe_key and frappe.db.exists(EVENT, {"dedupe_key": dedupe_key}):
		return None
	doc = frappe.get_doc({"doctype": EVENT, "contact": contact, "kind": kind, "mail": mail or None,
		"campaign": campaign or None, "occurred_at": occurred_at or now_datetime(), "detail": detail,
		"dedupe_key": dedupe_key or None})
	if not dedupe_key:
		return doc.insert(ignore_permissions=True).name
	frappe.db.savepoint("mkt_event")
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):   # a concurrent insert of the same key
		frappe.db.rollback(save_point="mkt_event")
		frappe.clear_last_message()
		return None
	return doc.name
