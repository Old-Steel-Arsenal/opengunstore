"""GunBroker member questions in the team mailbox (see ``crm.mail``).

A buyer's question about a listing or an order reaches us from one GunBroker address, with a
``MsgId`` in the subject that is the same on every message of that conversation. The question sits
in the body between ``BEGIN MESSAGE FROM USER`` and ``END MESSAGE FROM USER`` markers (dash counts
vary), and GunBroker's own lines follow: ``Item Number`` (the listing, also on an order question)
and, for a listing made from the POS, ``SKU``. In the HTML-to-text variant a label and its value are
separated by `` | ``. Only the lines after the last end marker are read: the member's own words are
not trusted to name a gun. A conversation's later mail may carry a MsgId of its own (a member asking
again from the listing): it goes where that member's open conversation is (``crm.mail``).

GunBroker's own notices (a listing sold, an auction won, an FFL transfer) come from GunBroker with
the other party in Reply-To, and Frappe files a received mail under its Reply-To address: the mail
reads as that person writing. :func:`notice` tells them apart by subject, or by GunBroker's name over
an address that is not GunBroker's. "won" names a seller the store bought from: :data:`SELLER_CATEGORY`.
"""
from __future__ import annotations

import re

import frappe
from frappe.core.utils import html_to_plain_text

from ffl_core.crm import TITLE

SENDER = "members@members.gunbroker.com"
MSGID_FIELD = "ffl_gb_msgid"
# The other MsgIds of a conversation a member asked again in: ",id1,id2," (crm.mail joins and the repair merge).
MORE_MSGIDS_FIELD = "ffl_gb_more_msgids"
_MSGID = re.compile(r"MsgId:\s*([\w-]{4,64})")
_END = re.compile(r"-+\s*END MESSAGE FROM USER\s*-+", re.I)
_LISTING = re.compile(r"Item Number[ \t:|]*(\d+)", re.I)
_SKU = re.compile(r"\bSKU[ \t:|]*([^\n|]+)", re.I)
DOMAIN = "gunbroker.com"
SELLER_CATEGORY = "Vendor"   # the mail category of a seller the store won an item from
# Matched at the start of the subject: "Re: ..." is a person answering one.
# lazy: GunBroker's subjects as they read in 2026; upgrade path: a subject list in Lead Mail Settings.
_NOTICES = (
	("sold", re.compile(r"Congratulations! Your item has sold\b", re.I)),   # Reply-To: the buyer
	("won", re.compile(r"Congratulations! You won this item\b", re.I)),     # Reply-To: the seller
	("transfer", re.compile(r"(?:New Inbound FFL Transfer|FFL Transfer Documents Ready|FFL Transfer Complete)\b", re.I)),
)
WON_LIKE = "Congratulations!%won this item%"
# Mail about an order on GunBroker: its order or item number, or the FFL and payment details the store
# sends a seller it won from (and their answer to them).
_ORDER_MAIL = re.compile(r"GunBroker(?:\.com)? Order|\b(?:Order|Item)\s*#?\s*\d{6,}|FFL\s*(?:&|and)\s*payment", re.I)
# A GunBroker user name: one word. A display name with a space is a person's name or a GunBroker
# service writing for many members ("GunBroker Valet"), and GunBroker's own names are no member's.
# lazy: GunBroker's user name rules are not published; 3-30 letters, digits, "_", "-", "."; upgrade
# path: the member's user id from the mail body, once GunBroker puts one there.
_USER_NAME = re.compile(r"[A-Za-z0-9][\w.-]{2,29}")


def msgid(subject: str | None) -> str | None:
	found = _MSGID.search(subject or "")
	return found.group(1) if found else None


def order_mail(subject: str | None) -> bool:
	return bool(_ORDER_MAIL.search(subject or ""))


def user_name(name: str | None) -> bool:
	"""Whether a member mail's display name is a GunBroker user name, the one person it can be."""
	name = (name or "").strip()
	return bool(_USER_NAME.fullmatch(name)) and not name.lower().startswith("gunbroker")


def joined(value: str | None, msgid: str) -> str:
	"""*value* of :data:`MORE_MSGIDS_FIELD` with *msgid* in it."""
	ids = [i for i in (value or "").split(",") if i]
	return "," + ",".join([*ids, msgid] if msgid not in ids else ids) + ","


def notice(email: str, sender_name: str | None, subject: str | None) -> str | None:
	"""What GunBroker notice this received mail is ("sold", "won", "transfer", or "notice" when only
	GunBroker's name tells), or None for a person's mail. GunBroker's own address is never one here:
	its mail goes by the category rules, and its member questions by :func:`conversation`."""
	domain = email.rsplit("@", 1)[-1].lower()
	subject = " ".join((subject or "").split())
	if domain == DOMAIN or domain.endswith("." + DOMAIN) or _MSGID.search(subject):
		return None
	for kind, pattern in _NOTICES:
		if pattern.match(subject):
			return kind
	return "notice" if (sender_name or "").strip().lower().startswith("gunbroker") else None


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
	and it may fail (logged): the mail is then other mail, as one about a listing that is not ours."""
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
