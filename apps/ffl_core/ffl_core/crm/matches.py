"""What the store has for this customer, on the Lead and Deal pages' side panel.

The list is the one a draft reply is written from (``reply_draft.gather_facts``): the record's product
lines (a website question puts the gun asked about there), then what its open wants fit in stock, then
what the wants in the customer's memory fit, nearest the remembered budget first. Each with the register's
price now and how many can be sold now; a line that names a gun shows that gun. Nothing is stored: the page asks each time it opens.

On a deal the guns of those items that can be held now are offered in Hold gun, one tick each.

Shown while Lead Mail Settings › crm_pos_link is on (the section is placed by ``crm.setup.apply_pos_link``),
and only on a record with something to show that can still be sold to: not Other mail, a closed deal or a
sell request.

lazy: as ``gather_facts``, every in-stock Item is read for a record with a want; upgrade path: its keyword index.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import escape_html, flt, fmt_money

from ffl_core.crm import memory, pos_link_on, reply_draft
from ffl_core.crm.deals import DEAL, is_closed
from ffl_core.crm.mail import LEAD, OTHER_MAIL
from ffl_core.crm.sell_requests import SELL

CARD_FIELD = "ffl_matches_card"
SECTION = "ffl_matches_section"
DOCTYPES = (LEAD, DEAL)
ROW_LIMIT = 8
GUN_LIMIT = 6
UNIT_SCAN = 200   # guns of one item read to count the ones for sale
PLACED = "ffl_crm_matches_placed"   # site default: the section was placed once
_MUTED = "color:var(--ink-gray-5)"


def _per_gun(code: str) -> bool:
	"""A serialized Item whose guns are priced one by one: the only kind a single gun of can be held."""
	item = frappe.get_cached_value("Item", code, ["has_serial_no", "uniform_sell_price"], as_dict=True)
	return bool(item and item.has_serial_no and not item.uniform_sell_price)


def rows(doc) -> list[dict]:
	"""``[{"item", "name", "price", "available", "guns": [{"serial", "price"}]}]``.

	A line that names a gun priced one by one is that gun: its own price, in stock or not, whatever
	else the item has. Then one row per item: *price* the lowest a gun priced one by one goes for now,
	*guns* the ones that can be held now (none for a model sold at one price: its guns cannot be held)."""
	from ffl_core import pricing

	lines = reply_draft.record_lines(doc)
	named = {}
	out = []
	for code, serial in lines:
		if not serial or not _per_gun(code):
			continue
		named.setdefault(code, []).append(serial)
		gun = frappe.db.get_value("Serial No", serial, ["status", "item_name"], as_dict=True) or frappe._dict()
		name = gun.item_name or frappe.get_cached_value("Item", code, "item_name") or code
		here = gun.status == "Active"
		out.append({"item": code, "name": f"{name} · {serial}", "available": int(here), "guns": [],
			"price": flt(pricing.counter_price_for(code, serial)) if here else 0})
	facts = reply_draft.gather_facts(doc, memory.kept_rows(doc))
	for code, fact in zip(facts.kept, facts.facts):
		if not _per_gun(code):
			out.append({"item": code, "name": fact["name"], "price": fact["price"], "available": fact["available"], "guns": []})
			continue
		guns = [{"serial": g["serial"], "price": flt(g["sell_price"])} for g in reply_draft.units(code, UNIT_SCAN)
			if g["serial"] not in named.get(code, ())]
		if guns or code not in named:   # the item's other guns, after the one the line names
			out.append({"item": code, "name": fact["name"], "price": guns[0]["price"] if guns else fact["price"],
				"available": len(guns), "guns": guns[:GUN_LIMIT]})
	return out[:ROW_LIMIT]


def _money(amount: float) -> str:
	return fmt_money(amount, currency=frappe.db.get_default("currency"))


def card_html(found: list[dict]) -> str:
	"""One line per item: its name, the price, and how many can be sold. Every value escaped."""
	lines = []
	for row in found:
		stock = _("{0} in stock").format(row["available"]) if row["available"] else _("Out of stock")
		price = _money(row["price"]) if row["price"] else ""
		lines.append(f'<div><div>{escape_html(row["name"])}</div><div style="{_MUTED}">{escape_html(stock)}</div></div>'
			f'<div style="text-align:right">{escape_html(price)}</div>')
	return ('<div style="display:grid;grid-template-columns:1fr auto;column-gap:12px;row-gap:8px">'
		+ "".join(lines) + "</div>")


def card(doc) -> dict:
	"""What the page shows: ``{"shown", "html", "guns"}``. *guns* (a deal only): the matched guns not on
	the deal yet that can be held now, ``[{"serial", "label"}]``."""
	if not pos_link_on() or (doc.doctype == LEAD and doc.status == OTHER_MAIL) or (doc.doctype == DEAL
			and (is_closed(doc.status) or doc.get(SELL))):   # nothing to sell: not a customer, closed, the store buying
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
	Memory. Placed once: a section a manager removed since stays removed until the POS link is switched
	off and on. Returns whether a layout changed."""
	from ffl_core.crm.setup import CARD_SECTION, SELL_SECTION

	changed = False
	placed = frappe.db.get_default(PLACED)
	if on and not placed:
		frappe.db.set_default(PLACED, "1")
	elif placed and not on:
		frappe.defaults.clear_default(PLACED)
	for doctype in DOCTYPES:
		name = f"{doctype}-Side Panel"
		if not frappe.db.exists("CRM Fields Layout", name):
			continue
		layout = frappe.get_doc("CRM Fields Layout", name)
		sections = json.loads(layout.layout or "[]")
		if on == any(s.get("name") == SECTION for s in sections) or (on and placed):
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
