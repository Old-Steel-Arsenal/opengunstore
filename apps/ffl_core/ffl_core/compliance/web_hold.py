"""Units a web order is waiting on are not the counter's to sell.

A web sale takes its unit off the shelf at checkout, but its stock exit posts only
when the order ships; a ship-to-store line's unit is the customer's the moment it
arrives. Guns are held by their serial (``woo_reserved``); an accessory has no
serial, so until now the register could ring up the very unit a web customer paid
for. The storefront already counts these units as spoken for
(``store_stock_for_item`` — the Bin less custody and the web lines the store fills);
this refuses a counter sale that would dip into them.

A manager (Sales Manager / System Manager) is allowed through — they are standing at
the register and decide — and the invoice notes it.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core.reservations import channel_claim_block_reason, is_uniform_firearm_item
from ffl_core.serials import serials_for_line

MANAGER_ROLES = ("Sales Manager", "System Manager")
STORE_STOCK_HOOK = "store_stock_for_item"


def _free_on_shelf(item_code) -> int | None:
	"""The shelf as the storefront counts it (ffl_woo_sync's local rules), or None
	on a site without a webshop — nothing is held there.

	Less today's register sales not merged yet: an accessory ticket leaves the Bin
	only at the nightly close (ERPNext's own ``get_pos_reserved_qty`` counts them the
	same way; a return's negative qty puts its unit back). A web pickup ticket is
	left out — its order's lines already count as held. The ticket being submitted
	is still a draft in the database, so it is not counted against itself."""
	for path in frappe.get_hooks(STORE_STOCK_HOOK) or []:
		stock = frappe.get_attr(path)(item_code)
		if stock is None:
			return None
		unmerged = frappe.db.sql(
			"""SELECT COALESCE(SUM(pii.stock_qty), 0) FROM `tabPOS Invoice Item` pii
			INNER JOIN `tabPOS Invoice` pi ON pi.name = pii.parent
			WHERE pii.item_code = %s AND pii.docstatus = 1 AND pii.parenttype = 'POS Invoice'
			  AND COALESCE(pi.consolidated_invoice, '') = ''
			  AND COALESCE(pi.woo_online_order, '') = ''""", item_code)[0][0]
		return int(stock.get("qty") or 0) - int(flt(unmerged))
	return None


def _held_by(item_code) -> list:
	"""The open web orders holding units of this item (for the message)."""
	return frappe.db.sql_list(
		"""SELECT DISTINCT o.woo_order_id FROM `tabWoo Online Order Line` l
		INNER JOIN `tabWoo Online Order` o ON o.name = l.parent
		WHERE l.item_code = %s AND o.order_status = 'Pending Fulfillment'
		  AND COALESCE(l.lane, '') NOT IN ('direct', 'fds')""", item_code)


def refuse_held_units(doc, method=None) -> None:
	"""``before_submit`` on a POS Invoice, and a Sales Invoice that moves stock:
	each accessory's quantity may not exceed the free shelf. Returns, consolidated
	copies and web orders' own invoices are not counter sales."""
	if doc.get("is_return") or doc.get("is_consolidated") or doc.get("woo_online_order"):
		return
	if doc.doctype == "Sales Invoice" and not doc.get("update_stock"):
		return
	wanted: dict = {}
	for row in doc.get("items") or []:
		if row.get("item_code") and not row.get("serial_no") and not row.get("serial_and_batch_bundle"):
			wanted[row.item_code] = wanted.get(row.item_code, 0) + flt(row.get("stock_qty") or row.get("qty"))
	short = []
	for item_code, qty in wanted.items():
		if frappe.db.get_value("Item", item_code, "has_serial_no") or not frappe.db.get_value(
				"Item", item_code, "is_stock_item"):
			continue
		free = _free_on_shelf(item_code)
		if free is None or qty <= free:
			continue
		short.append((item_code, qty, free, _held_by(item_code)))
	if not short:
		return
	lines = [frappe._("{0}: selling {1}, only {2} free — the rest is held for web order(s) {3}.")
		.format(code, frappe.format(qty), max(free, 0), ", ".join(f"#{o}" for o in orders) or "—")
		for code, qty, free, orders in short]
	if set(MANAGER_ROLES) & set(frappe.get_roles()):
		doc.add_comment("Info", frappe._("Manager sold units held for web orders: ") + " ".join(lines))
		return
	frappe.throw("<br>".join(lines) + "<br>" + frappe._(
		"Ship those units to the web customer instead, or ask a manager to ring this sale."),
		title=frappe._("Held for a web order"))


def refuse_claimed_firearm_units(doc, method=None) -> None:
	"""``before_submit`` on a POS Invoice: a uniform-price gun may not be sold
	when it is the last one a sold online order is already owed.

	The firearm half of :func:`refuse_held_units`, which skips every line that
	carries a serial. A sale on any channel (web, GunBroker) of a uniform-price gun
	owes a UNIT and reserves no `Serial No`, so none of the per-gun holds sees it;
	the counter's scan-to-add asks (`reservations.channel_claim_block_reason`) but
	a ticket built another way — the register's own serial picker, a resumed draft
	— reached submit with nothing asking. This is the same question, asked where
	every ticket passes.

	No manager override, unlike the accessory gate: the scan gate has none either,
	and what is at stake is a paid order that can no longer be filled.

	The ticket is asked by name (``ignore_hold_ref``): its draft already holds the
	gun it is selling, and that hold is not a rival claim on it.
	"""
	if doc.get("is_return") or doc.get("is_consolidated") or doc.get("woo_online_order"):
		return
	selling: dict = {}
	for row in doc.get("items") or []:
		item_code = row.get("item_code")
		if not item_code or not (row.get("serial_no") or row.get("serial_and_batch_bundle")):
			continue
		if not is_uniform_firearm_item(item_code):
			continue
		selling[item_code] = selling.get(item_code, 0) + len(serials_for_line(row))
	reasons = []
	for item_code, count in selling.items():
		reason = channel_claim_block_reason(
			item_code, selling=max(1, count), ignore_hold_ref=doc.name)
		if reason:
			reasons.append(reason)
	if reasons:
		frappe.throw("<br>".join(reasons), title=frappe._("Owed to an online order"))
