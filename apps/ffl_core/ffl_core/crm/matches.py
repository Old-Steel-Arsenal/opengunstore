"""What the store has for this customer, on the Lead and Deal pages' side panel.

The list is the one a draft reply is written from (``reply_draft.gather_facts``): the record's product
lines (a website question puts the gun asked about there), then what its open wants fit in stock, then
what the wants in the customer's memory fit, nearest the remembered budget first. Each with the register's
price now and how many can be sold now. Nothing is stored: the page asks each time it opens.

On a deal the guns of those items that can be held now are offered in Hold gun, one tick each.

Shown while Lead Mail Settings › crm_pos_link is on (the section is placed by ``crm.setup.apply_pos_link``),
and only on a record with something to show.

lazy: as ``gather_facts``, every in-stock Item is read for a record with a want; upgrade path: its keyword index.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import escape_html, flt, fmt_money

from ffl_core.crm import memory, pos_link_on, reply_draft
from ffl_core.crm.deals import DEAL
from ffl_core.crm.mail import LEAD, OTHER_MAIL

CARD_FIELD = "ffl_matches_card"
SECTION = "ffl_matches_section"
DOCTYPES = (LEAD, DEAL)
ROW_LIMIT = 8
GUN_LIMIT = 6
_MUTED = "color:var(--ink-gray-5)"


def rows(doc) -> list[dict]:
	"""``[{"item", "name", "price", "available", "guns": [{"serial", "price"}]}]``: *price* the lowest a
	gun priced one by one goes for now, *guns* the ones of a serialized item that can be held now."""
	facts = reply_draft.gather_facts(doc, memory.kept_rows(doc))
	out = []
	for code, fact in zip(facts.kept, facts.facts):
		serialized = frappe.get_cached_value("Item", code, "has_serial_no")
		guns = [{"serial": g["serial"], "price": flt(g["sell_price"])}
			for g in (reply_draft.units(code, GUN_LIMIT) if serialized and fact["available"] else [])]
		price = min([u["price"] for u in fact["units"]] or [fact["price"]])
		out.append({"item": code, "name": fact["name"], "price": price, "available": fact["available"], "guns": guns})
	return out[:ROW_LIMIT]


def _money(amount: float) -> str:
	return fmt_money(amount, currency=frappe.db.get_default("currency"))


def card_html(found: list[dict]) -> str:
	"""One line per item: its name, the price, and how many can be sold. Every value escaped."""
	lines = []
	for row in found:
		stock = _("{0} in stock").format(row["available"]) if row["available"] else _("Out of stock")
		lines.append(f'<div><div>{escape_html(row["name"])}</div><div style="{_MUTED}">{escape_html(stock)}</div></div>'
			f'<div style="text-align:right">{escape_html(_money(row["price"]))}</div>')
	return ('<div style="display:grid;grid-template-columns:1fr auto;column-gap:12px;row-gap:8px">'
		+ "".join(lines) + "</div>")


def card(doc) -> dict:
	"""What the page shows: ``{"shown", "html", "guns"}``. *guns* (a deal only): the matched guns not on
	the deal yet that can be held now, ``[{"serial", "label"}]``."""
	if not pos_link_on() or (doc.doctype == LEAD and doc.status == OTHER_MAIL):
		return {"shown": False, "html": "", "guns": []}
	found = rows(doc)
	guns = []
	if doc.doctype == DEAL:
		on_deal = {serial for _item, serial in reply_draft.record_lines(doc) if serial}
		guns = [{"serial": g["serial"], "label": f'{row["name"]} · {g["serial"]} · {_money(g["price"])}'}
			for row in found for g in row["guns"] if g["serial"] not in on_deal][:GUN_LIMIT]
	return {"shown": bool(found), "html": card_html(found) if found else "", "guns": guns}


def place(on: bool) -> bool:
	"""The Matches section in the Lead and Deal side panels, below the contacts, the POS's cards and
	Memory. Returns whether a layout changed."""
	from ffl_core.crm.setup import CARD_SECTION, SELL_SECTION

	changed = False
	for doctype in DOCTYPES:
		name = f"{doctype}-Side Panel"
		if not frappe.db.exists("CRM Fields Layout", name):
			continue
		layout = frappe.get_doc("CRM Fields Layout", name)
		sections = json.loads(layout.layout or "[]")
		if on == any(s.get("name") == SECTION for s in sections):
			continue
		if on:
			above = ("contacts_section", CARD_SECTION, SELL_SECTION, memory.SECTION)
			at = 0
			while at < len(sections) and sections[at].get("name") in above:
				at += 1
			sections.insert(at, {"name": SECTION, "label": "Matches", "opened": True,
				"columns": [{"name": "ffl_matches_column", "fields": [CARD_FIELD]}]})
		else:
			sections = [s for s in sections if s.get("name") != SECTION]
		layout.layout = json.dumps(sections)
		layout.save(ignore_permissions=True)   # the CRM page layout, written by migrate or the settings' save
		changed = True
	return changed
