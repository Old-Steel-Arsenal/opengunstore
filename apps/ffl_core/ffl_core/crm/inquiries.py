"""A website inquiry on CRM: a price request on a hidden-price gun, a sell-your-gun form, a question.

The website relays public form input through ``ffl_woo_sync``'s ``web_inquiry`` endpoint. It is filed
the way incoming mail is (``crm.mail``): an email that belongs to a Contact of a Customer goes on that
contact's newest open deal (one is opened when there is none); anyone else goes on their existing CRM
Lead (not converted, not other mail), or a new one. What a new lead or deal makes is shared with the
team (``share_with_team``); an existing record keeps its owner, status and sharing. The inquiry is an
FCRM Note, and the gun asked about (a SKU of an Item the POS knows) a line of the record's products.
Nothing is emailed to the customer.

Every field is public input: capped, and stored escaped. A note's title carries the website's event
id, which is how a repeat of the same event is told from a new one.

lazy: the repeat check reads Note titles (the notes table of one store is small); upgrade path: a
field on FCRM Note, indexed.
"""
from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import escape_html, validate_email_address
from frappe.utils.synchronization import filelock

from ffl_core.crm import installed
from ffl_core.crm.deal import add_product_row
from ffl_core.crm.deals import DEAL, open_deal_for
from ffl_core.crm.mail import LEAD, OTHER_MAIL, _customer_contact, _new_lead, share_with_team
from ffl_core.crm.web_orders import NOTE, _open_deal
from ffl_core.utils import commit

MAP_PRICE, SELL_GUN, ASK = "map_price", "sell_gun", "ask"
KINDS = {MAP_PRICE: "price request", SELL_GUN: "sell-your-gun request", ASK: "question"}
MAX_DETAILS, MAX_PHOTOS = 40, 12
# Field caps: the longest a real form value is, with room.
CAP = {"event_id": 80, "name": 140, "phone": 40, "message": 5000, "sku": 140, "url": 500, "label": 80, "value": 1000}


def _text(value, cap: str) -> str:
	return str(value if value is not None else "").strip()[:CAP[cap]]


def _html_lines(*lines: str) -> str:
	return "".join(f"<p>{line}</p>" for line in lines if line)


def _note_html(phone: str, message: str, sku: str, url: str, details: dict, photos: list[str]) -> str:
	lines = [escape_html(message).replace("\n", "<br>")]
	if phone:
		lines.append(escape_html(_("Phone: {0}").format(phone)))
	if sku or url:
		label = escape_html(sku or url)
		lines.append(escape_html(_("Product")) + ": " + (f'<a href="{escape_html(url)}">{label}</a>' if url else label))
	html = _html_lines(*lines)
	if details:
		html += "<ul>" + "".join(f"<li><b>{escape_html(k)}</b>: {escape_html(v)}</li>" for k, v in details.items()) + "</ul>"
	if photos:
		html += _html_lines(escape_html(_("Photos")) + ": " + ", ".join(
			f'<a href="{escape_html(u)}">{i}</a>' for i, u in enumerate(photos, 1)))
	return html


def _gun_of(sku: str) -> tuple[str, str | None]:
	"""A SKU as (item code, serial number or None): ``item_code`` or ``item_code::serial``."""
	item, _sep, serial = sku.partition("::")
	return item.strip(), serial.strip() or None


def _target(email: str, full_name: str) -> tuple[str, str, bool]:
	"""(doctype, name, created): where this address's inquiry goes."""
	contact = _customer_contact(email)
	if contact:
		deal = _open_deal(email)
		if deal:
			return DEAL, deal, False
		deal = open_deal_for(contact)
		share_with_team(DEAL, deal)
		return DEAL, deal, True
	lead = frappe.db.get_value(LEAD, {"email": email, "converted": 0, "status": ["!=", OTHER_MAIL]}, "name",
		order_by="modified desc")
	if lead:
		return LEAD, lead, False
	lead = _new_lead(email, full_name, source="Website")
	share_with_team(LEAD, lead)
	return LEAD, lead, True


def file_inquiry(kind: str, event_id: str, email: str, full_name: str = "", phone: str = "", message: str = "",
		sku: str = "", url: str = "", details: dict | None = None, photos: list | None = None) -> dict:
	"""File a website inquiry on CRM. Returns {"filed": True, "doctype", "name", "created"} (*created*:
	the record is new; False for a repeat of *event_id* too), or {"filed": False, "reason"} when CRM is
	not installed. Refuses (``frappe.ValidationError``) an unknown *kind*, a missing event id or an
	invalid email. *details* is a flat dict of label -> value (the sell-your-gun form), *photos* a list
	of https URLs (others are dropped), *sku* the Item the inquiry is about (``item_code`` or
	``item_code::serial``) and *url* its page."""
	if not installed():
		return {"filed": False, "reason": "CRM is not installed"}
	if kind not in KINDS:
		frappe.throw(_("Unknown inquiry kind: {0}").format(_text(kind, "label")), title=_("Inquiry refused"))
	event_id = _text(event_id, "event_id")
	address = _text(email, "name").lower()
	if not event_id or not address or any(c in address for c in " ,;<>") \
			or validate_email_address(address, throw=False) != address:
		frappe.throw(_("An inquiry needs an event id and a valid email address."), title=_("Inquiry refused"))
	title = f"Website {KINDS[kind]} ({event_id})"   # the repeat key: not translated
	sku, url = _text(sku, "sku"), _text(url, "url")
	details = {_text(k, "label"): _text(v, "value") for k, v in
		list(details.items() if isinstance(details, dict) else [])[:MAX_DETAILS]}
	photos = [u for u in (_text(p, "url") for p in (photos if isinstance(photos, list) else []))
		if u.startswith("https://")][:MAX_PHOTOS]
	with filelock("ffl_web_inquiry_" + hashlib.md5(event_id.encode()).hexdigest(), timeout=60):
		commit()   # a fresh snapshot: a repeat that held the lock before us is visible
		again = frappe.db.get_value(NOTE, {"title": title}, ["reference_doctype", "reference_docname"])
		if again:
			return {"filed": True, "doctype": again[0], "name": again[1], "created": False}
		doctype, name, created = _target(address, _text(full_name, "name"))
		frappe.get_doc({"doctype": NOTE, "title": title, "reference_doctype": doctype, "reference_docname": name,
			"content": _note_html(_text(phone, "phone"), _text(message, "message"), sku,
				url if url.startswith(("http://", "https://")) else "", details, photos)}).insert(ignore_permissions=True)
		item, serial = _gun_of(sku)
		if item:
			add_product_row(doctype, name, item, serial)
	return {"filed": True, "doctype": doctype, "name": name, "created": created}
