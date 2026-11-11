"""GunBroker member questions in the team mailbox (see ``crm.mail``).

A buyer's question about a listing or an order reaches us from one GunBroker address, with a
``MsgId`` in the subject that is the same on every message of that conversation. The question sits
in the body between ``BEGIN MESSAGE FROM USER`` and ``END MESSAGE FROM USER`` markers (dash counts
vary), and GunBroker's own lines follow: ``Item Number`` (the listing, also on an order question)
and, for a listing made from the POS, ``SKU``. In the HTML-to-text variant a label and its value are
separated by `` | ``. Only the lines after the last end marker are read: the member's own words are
not trusted to name a gun.
"""
from __future__ import annotations

import re

import frappe
from frappe.core.utils import html_to_plain_text

from ffl_core.crm import TITLE

SENDER = "members@members.gunbroker.com"
MSGID_FIELD = "ffl_gb_msgid"
_MSGID = re.compile(r"MsgId:\s*([\w-]{4,64})")
_END = re.compile(r"-+\s*END MESSAGE FROM USER\s*-+", re.I)
_LISTING = re.compile(r"Item Number[ \t:|]*(\d+)", re.I)
_SKU = re.compile(r"\bSKU[ \t:|]*([^\n|]+)", re.I)


def conversation(email: str, subject: str | None, content: str | None) -> dict | None:
	"""The facts of a GunBroker member's mail: {"msgid", "listing", "sku"} (the last two may be empty),
	or None when the mail is not one (another sender, or no MsgId in the subject)."""
	if email != SENDER:
		return None
	found = _MSGID.search(subject or "")
	if not found:
		return None
	text = html_to_plain_text(content or "")
	ends = list(_END.finditer(text))
	trailer = text[ends[-1].end():] if ends else ""
	listing, sku = _LISTING.search(trailer), _SKU.search(trailer)
	return {"msgid": found.group(1), "listing": listing.group(1) if listing else "", "sku": sku.group(1).strip() if sku else ""}


def gun(facts: dict) -> tuple[str, str | None] | None:
	"""(item code, serial number or None) of the gun the listing is, or None when it cannot be told.
	The listing lookup belongs to ffl_integrations, which this app does not import; it may be absent
	and it may fail, and the lead is made anyway."""
	if "ffl_integrations" not in frappe.get_installed_apps():
		return None
	try:
		kind, name = frappe.get_attr("ffl_integrations.gunbroker.webhooks.resolve_listing")(facts["sku"], facts["listing"])
		if kind == "serial":
			return frappe.db.get_value("Serial No", name, "item_code"), name
		return (name, None) if kind == "item" else None
	except Exception:
		frappe.log_error(title=f"[{TITLE}] GunBroker listing not resolved: {facts['listing'] or facts['sku']}",
			message=frappe.get_traceback())
		return None
