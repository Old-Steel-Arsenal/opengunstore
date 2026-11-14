"""One-shot, safe to run again: website inquiry notes take the shape new ones have (``crm.inquiries``).

* A sell-your-gun note (titled "Website sell-your-gun request (<event id>)") becomes a sell-request deal of
  its own (``crm.sell_requests``): the form's fields read back from the note's list, the note moved onto the
  new deal and titled with the request's reference, its photos queued for copying. A note whose text does
  not read back cleanly (no list, or no manufacturer and model in it) is left as it was and listed in one
  Error Log.
* A price request or question note keeps its record and loses the event id from its title; the id goes in
  the note's own field, which the repeat check reads.

The lead or deal a sell note was on stays as it is.
"""
from __future__ import annotations

import html
import re

import frappe

from ffl_core.crm import TITLE, installed, setup
from ffl_core.crm import sell_requests
from ffl_core.crm.inquiries import EVENT_FIELD, KINDS, SELL_GUN
from ffl_core.crm.web_orders import NOTE

_TITLE = re.compile(r"^Website (price request|sell-your-gun request|question) \((.+)\)$")
_LIST = re.compile(r"<ul>((?:<li><b>[^<]*</b>: [^<]*</li>)+)</ul>")
_ITEM = re.compile(r"<li><b>([^<]*)</b>: ([^<]*)</li>")
_PHOTOS = re.compile(r'<a href="(https://[^"]+)"[^>]*>\d+</a>')   # the note's sanitizer adds rel
_PHONE = re.compile(r"<p>Phone: ([^<]*)</p>")


def parse(content: str) -> tuple[dict, list[str], str] | None:
	"""(details, photo URLs, phone) of a sell-your-gun note as ``inquiries._note_html`` wrote it, or None."""
	found = _LIST.search(content or "")
	if not found:
		return None
	details = {html.unescape(k): html.unescape(v) for k, v in _ITEM.findall(found.group(1))}
	if not (details.get("Manufacturer") and details.get("Model")):
		return None
	phone = _PHONE.search(content)
	return details, [html.unescape(u) for u in _PHOTOS.findall(content)], html.unescape(phone.group(1)) if phone else ""


def _email_of(doctype: str, name: str) -> str:
	return (frappe.db.get_value(doctype, name, "email") or "").strip().lower() if frappe.db.exists(doctype, name) else ""


def _person(doctype: str, name: str) -> str:
	if doctype == "CRM Lead" and frappe.db.exists(doctype, name):
		return " ".join(n for n in frappe.db.get_value(doctype, name, ["first_name", "last_name"]) if n)
	return ""


def execute() -> None:
	if not installed():
		return
	setup.ensure_custom_fields()   # the patch runs before after_migrate adds them
	frappe.clear_cache(doctype="CRM Deal")
	frappe.clear_cache(doctype=NOTE)
	kinds = {label: kind for kind, label in KINDS.items()}
	skipped = []
	for note in frappe.get_all(NOTE, filters={"title": ["like", "Website % (%)"]},
			fields=["name", "title", "content", "reference_doctype", "reference_docname"], order_by="creation asc"):
		match = _TITLE.match(note.title or "")
		if not match:
			continue
		kind, event_id = kinds[match.group(1)], match.group(2)
		if kind != SELL_GUN:
			frappe.db.set_value(NOTE, note.name, {"title": f"Website {KINDS[kind]}", EVENT_FIELD: event_id},
				update_modified=False)
			continue
		parsed = parse(note.content)
		email = _email_of(note.reference_doctype, note.reference_docname) if note.reference_doctype else ""
		if not parsed or not email:
			skipped.append(f"{note.name} on {note.reference_doctype} {note.reference_docname}")
			continue
		details, photos, phone = parsed
		frappe.db.savepoint("ffl_sell_patch")
		try:
			values, _rest = sell_requests.known_fields(details)
			deal = sell_requests.new_deal(email, _person(note.reference_doctype, note.reference_docname), phone,
				values, photos[:12])
			reference = values.get(sell_requests.FIELDS["Reference"], "")
			frappe.db.set_value(NOTE, note.name, {"reference_doctype": "CRM Deal", "reference_docname": deal,
				"title": " ".join(("Sell-your-gun request", reference)).strip(), EVENT_FIELD: event_id},
				update_modified=False)
			if photos:
				sell_requests.queue_photos(deal)
		except Exception:
			frappe.db.rollback(save_point="ffl_sell_patch")
			skipped.append(f"{note.name}: {frappe.get_traceback()}")
	if skipped:
		frappe.log_error(title=f"[{TITLE}] Sell-your-gun notes left as they were",
			message="These notes did not read back as a sell request and stay where they are:\n" + "\n".join(skipped))
