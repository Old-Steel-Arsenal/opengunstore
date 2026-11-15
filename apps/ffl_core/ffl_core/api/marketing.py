"""Marketing on the Desk and the connector: the Marketing Contact form's Unsubscribe and Resubscribe
buttons, the Marketing line on the Customer form, and the marketing reads and writes the POS connector's
tools call (find contacts, one contact, tags, a recorded consent, reminder requests).

Every status change goes through ``marketing.contacts.set_status``, the one function that changes a
contact's status; the form's status field is read-only. Tags go through ``marketing.tags``, requests
through ``marketing.requests``. A contact that is not on file is refused, never made. Nothing here sends
email.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import escape_html

from ffl_core.marketing import CONTACT, EVENT, REQUEST, enabled
from ffl_core.marketing import contacts, customers, requests, tags
from ffl_core.roles import MARKETING_MANAGER_ROLES, MARKETING_ROLES

CONTACT_FIELDS = ("email", "first_name", "last_name", "status", "source", "consent_at", "consent_channel",
	"status_changed_at", "status_reason", "customer", "creation")
EVENT_FIELDS = ("kind", "occurred_at", "mail", "campaign", "detail")
MAX_LIMIT = 200


def _existing(contact) -> str:
	name = contacts.normalize_email(contact)
	if not name or not frappe.db.exists(CONTACT, name):
		frappe.throw(_("No marketing contact {0}.").format(escape_html(contacts.clip(contact))), frappe.DoesNotExistError)
	return name


@frappe.whitelist(methods=["POST"])
def unsubscribe(contact: str, reason: str | None = None) -> dict:
	"""Stop marketing email to a contact.

	Its status becomes Unsubscribed, with who did it and the reason on the log. A contact already
	Unsubscribed, Bounced or Complained is left as it is.

	Args:
	    contact: The Marketing Contact (its email address).
	    reason: Why, in a few words. Optional.

	Returns:
	    {"contact": the address, "status": the status now, "changed": False when it was already
	    suppressed}.
	"""
	frappe.only_for(MARKETING_MANAGER_ROLES)
	name = _existing(contact)
	changed = contacts.set_status(name, contacts.UNSUBSCRIBED, reason=reason,
		detail=contacts.event_detail(reason=reason, by=frappe.session.user, via="desk"))
	return {"contact": name, "status": frappe.db.get_value(CONTACT, name, "status"), "changed": changed}


@frappe.whitelist(methods=["POST"])
def resubscribe(contact: str, reason: str) -> dict:
	"""Put a suppressed contact back to Subscribed.

	For a contact that unsubscribed, bounced or complained. The reason is required and goes on the log
	with who did it.

	Args:
	    contact: The Marketing Contact (its email address).
	    reason: Why this contact may be emailed again.

	Returns:
	    {"contact": the address, "status": "Subscribed"}. Refused for a contact that is not suppressed.
	"""
	frappe.only_for(MARKETING_MANAGER_ROLES)
	name = _existing(contact)
	if not contacts.is_suppressed(frappe.db.get_value(CONTACT, name, "status")):
		frappe.throw(_("Only a contact that unsubscribed, bounced or complained can be resubscribed."))
	contacts.set_status(name, contacts.SUBSCRIBED, reason=reason, manual=True, channel="manual")
	return {"contact": name, "status": frappe.db.get_value(CONTACT, name, "status")}


@frappe.whitelist()
def customer_card(customer: str) -> dict | None:
	"""The line the Customer form shows about a customer's marketing email.

	Nothing (None) when marketing is off, the user may not read Marketing Contacts or the customer,
	or no marketing contact is linked to the customer.

	Args:
	    customer: The Customer's name.

	Returns:
	    {"status": the contact's status, "tags": its tags, "text": the line to show, "contact": the
	    Marketing Contact (its email address) to open}, or None.
	"""
	if not frappe.db.exists("Customer", customer) or not frappe.has_permission(CONTACT, "read") \
			or not frappe.has_permission("Customer", "read", doc=customer):
		return None
	card = customers.card(customer, with_contact=True)
	return {**card, "text": customers.card_text(card)} if card else None


def _require_enabled() -> None:
	if not enabled():
		frappe.throw(_("Marketing is switched off on this store (Marketing Settings › Enabled)."),
			title=_("Marketing is off"))


def _limit(value, default: int) -> int:
	return min(max(frappe.utils.cint(value) or default, 1), MAX_LIMIT)


def _tag_list(value) -> list[str]:
	"""A list of tags from a list or a JSON / comma-separated string; refused when one is not a slug."""
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip().startswith("[") else value.split(",")
	values = [str(v).strip() for v in (value or []) if str(v).strip()]
	bad = [v for v in values if not tags.is_slug(v)]
	if bad:
		frappe.throw(_("Not a tag: {0}. A tag is lowercase letters, digits and dashes, up to 60.").format(
			escape_html(", ".join(contacts.clip(v, 60) for v in bad))))
	return list(dict.fromkeys(values))


@frappe.whitelist(methods=["GET"])
def find_contacts(email: str | None = None, name: str | None = None, tag: str | None = None,
		status: str | None = None, source: str | None = None, limit: int = 20, start: int = 0) -> dict:
	"""Find marketing contacts, newest first.

	Any of the filters, together. Refused while marketing is switched off.

	Args:
	    email: Part of the email address.
	    name: Part of the first or last name.
	    tag: A tag the contact has (exact).
	    status: Not Subscribed, Subscribed, Unsubscribed, Bounced or Complained.
	    source: Where the contact first came from; a prefix ("fluentform" matches "fluentform:3").
	    limit: How many to return (at most 200).
	    start: How many to skip, for the next page.

	Returns:
	    {"total": how many contacts match, "contacts": [{email, first_name, last_name, status, source,
	    consent_at, consent_channel, status_changed_at, status_reason, customer, creation, tags}]}.
	"""
	frappe.has_permission("Marketing Contact", "read", throw=True)   # a literal: the API docs read the gate
	_require_enabled()
	if status and status not in contacts.STATUSES:
		frappe.throw(_("Unknown marketing status: {0}").format(escape_html(contacts.clip(status))))
	c = frappe.qb.DocType(CONTACT)
	where = []
	if email and email.strip():
		where.append(c.name.like(f"%{contacts.clip(email).lower()}%"))
	if name and name.strip():
		like = f"%{contacts.clip(name)}%"
		where.append(c.first_name.like(like) | c.last_name.like(like))
	if status:
		where.append(c.status == status)
	if source and source.strip():
		where.append(c.source.like(f"{contacts.clip(source)}%"))
	if tag and tag.strip():
		link = frappe.qb.DocType("Tag Link")
		where.append(c.name.isin(frappe.qb.from_(link).select(link.document_name)
			.where((link.document_type == CONTACT) & (link.tag == tag.strip()))))
	count = frappe.qb.from_(c).select(Count("*"))
	rows = frappe.qb.from_(c).select(*(c[f] for f in CONTACT_FIELDS))
	for condition in where:
		count, rows = count.where(condition), rows.where(condition)
	rows = rows.orderby(c.creation, order=frappe.qb.desc).orderby(c.name) \
		.limit(_limit(limit, 20)).offset(max(frappe.utils.cint(start), 0)).run(as_dict=True)
	for row in rows:
		row["tags"] = tags.of(row["email"])
	return {"total": count.run()[0][0], "contacts": rows}


@frappe.whitelist(methods=["GET"])
def contact_card(contact: str, events: int = 20) -> dict:
	"""One marketing contact: its record, tags, latest events and the reminders still waiting.

	Refused while marketing is switched off.

	Args:
	    contact: The Marketing Contact (its email address).
	    events: How many of the latest events to return (at most 200).

	Returns:
	    {"contact": {email, first_name, last_name, status, source, consent_at, consent_channel,
	    status_changed_at, status_reason, customer, creation, tags, suppressed}, "events": [{kind,
	    occurred_at, mail, campaign, detail}] newest first, "open_requests": the Open reminder requests}.
	"""
	frappe.has_permission("Marketing Contact", "read", throw=True)   # a literal: the API docs read the gate
	_require_enabled()
	name = _existing(contact)
	record = frappe.db.get_value(CONTACT, name, list(CONTACT_FIELDS), as_dict=True)
	record.update(tags=tags.of(name), suppressed=contacts.is_suppressed(record.status))
	log = frappe.get_list(EVENT, filters={"contact": name}, fields=list(EVENT_FIELDS),
		order_by="occurred_at desc, creation desc", limit_page_length=_limit(events, 20))
	return {"contact": record, "events": log, "open_requests": requests.find(contact=name, limit=MAX_LIMIT)}


@frappe.whitelist(methods=["POST"])
def tag_contact(contact: str, add: list | str | None = None, remove: list | str | None = None) -> dict:
	"""Add tags to a marketing contact and take tags off it.

	A tag is a slug (lowercase letters, digits and dashes); anything else is refused, nothing changed.
	Tags never send anything. Refused while marketing is switched off.

	Args:
	    contact: The Marketing Contact (its email address).
	    add: Tags to add (a list, or a comma-separated string).
	    remove: Tags to take off.

	Returns:
	    {"contact": the address, "added": tags that were new to it, "removed": tags it had,
	    "tags": its tags now}.
	"""
	frappe.only_for(MARKETING_ROLES)
	_require_enabled()
	name = _existing(contact)
	add, remove = _tag_list(add), _tag_list(remove)
	if not add and not remove:
		frappe.throw(_("Name a tag to add or to remove."))
	if set(add) & set(remove):
		frappe.throw(_("A tag cannot be added and removed at once."))
	removed = tags.remove(name, *remove) if remove else []
	added = tags.add(name, *add) if add else []
	return {"contact": name, "added": added, "removed": removed, "tags": tags.of(name)}


@frappe.whitelist(methods=["POST"])
def record_consent(contact: str, channel: str, note: str | None = None) -> dict:
	"""Record that a contact agreed to marketing email: Not Subscribed becomes Subscribed.

	For a consent given outside the website (at the counter, on the phone, on a paper form). A contact
	that unsubscribed, bounced or complained is refused: this never puts one back (a manager can, on
	the contact's form, with a reason). Who recorded it, the channel and the note go on the log.
	Refused while marketing is switched off.

	Args:
	    contact: The Marketing Contact (its email address).
	    channel: Where the consent was given, in a word or two ("in store", "phone").
	    note: Anything worth keeping with it. Optional.

	Returns:
	    {"contact": the address, "status": the status now, "changed": False when it was already
	    Subscribed}.
	"""
	frappe.only_for(MARKETING_ROLES)
	_require_enabled()
	name = _existing(contact)
	channel = contacts.plain_text(channel, 60)
	if not channel:
		frappe.throw(_("Say where the consent was given."), frappe.MandatoryError)
	status = frappe.db.get_value(CONTACT, name, "status", for_update=True)
	if contacts.is_suppressed(status):
		frappe.throw(_("{0} is {1}: a recorded consent never puts a contact back on the list.").format(
			escape_html(name), _(status)), title=_("Not subscribed"))
	changed = contacts.set_status(name, contacts.SUBSCRIBED, channel=channel,
		detail=contacts.event_detail(channel=channel, note=note, by=frappe.session.user))
	return {"contact": name, "status": frappe.db.get_value(CONTACT, name, "status"), "changed": changed}


@frappe.whitelist(methods=["GET"])
def list_requests(status: str | None = "Open", kind: str | None = None, contact: str | None = None,
		item: str | None = None, limit: int = 50, start: int = 0) -> list[dict]:
	"""Back-in-stock and model-alert reminder requests, newest first.

	Refused while marketing is switched off.

	Args:
	    status: Open (the default), Notified or Closed; "all" for every status.
	    kind: Back in stock or Model alert.
	    contact: One contact's (its email address).
	    item: The POS Item the request is for.
	    limit: How many to return (at most 200).
	    start: How many to skip, for the next page.

	Returns:
	    [{name, contact, kind, status, label, item, sku, web_product_id, model_key, url, requested_at,
	    notified_at}].
	"""
	frappe.has_permission("Marketing Request", "read", throw=True)
	_require_enabled()
	status = None if (status or "").lower() == "all" else status
	if status and status not in (requests.OPEN, requests.NOTIFIED, requests.CLOSED):
		frappe.throw(_("status must be Open, Notified, Closed or all."))
	if kind and kind not in (requests.BACK_IN_STOCK, requests.MODEL_ALERT):
		frappe.throw(_("kind must be Back in stock or Model alert."))
	return requests.find(status=status, kind=kind, contact=contacts.normalize_email(contact) or contact,
		item=item, limit=_limit(limit, 50), start=max(frappe.utils.cint(start), 0))


@frappe.whitelist(methods=["POST"])
def close_request(request: str) -> dict:
	"""Stop waiting on a reminder request: an Open request becomes Closed and is never notified.

	Refused while marketing is switched off.

	Args:
	    request: The Marketing Request.

	Returns:
	    {"request": its name, "status": the status now, "changed": False when it was already Notified
	    or Closed}.
	"""
	frappe.only_for(MARKETING_ROLES)
	_require_enabled()
	if not isinstance(request, str) or not frappe.db.exists(REQUEST, request):
		frappe.throw(_("No reminder request {0}.").format(escape_html(contacts.clip(request))),
			frappe.DoesNotExistError)
	changed = requests.close(request)
	return {"request": request, "status": frappe.db.get_value(REQUEST, request, "status"), "changed": changed}
