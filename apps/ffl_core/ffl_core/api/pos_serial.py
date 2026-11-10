"""POS serial-reservation check that forgets consolidated tickets.

ERPNext's serial_no.get_pos_reserved_serial_nos treats every serial on any
submitted POS Invoice as reserved, consolidated or not. Once the nightly
close consolidates a ticket, its Sales Invoice has already moved the stock,
so the serial is no longer "pending". The upstream query never drops it,
and a gun sold at the counter and later bought back (a new receipt, no
return ticket) can never be sold at the register again: "Serial No: X has
already been transacted into another POS Invoice."

This override (wired via override_whitelisted_methods) is the upstream
query plus one filter: only POS Invoices not yet consolidated reserve.
"""
from __future__ import annotations

import json

import frappe
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos


@frappe.whitelist()
def get_pos_reserved_serial_nos(filters):
	"""Serials held by submitted, not-yet-consolidated POS Invoices (override).

	Replaces ERPNext's serial_no.get_pos_reserved_serial_nos for the POS
	page's add-to-cart check. Same result as upstream, except a POS Invoice
	that has been consolidated into a Sales Invoice no longer reserves its
	serials: the stock ledger already reflects that sale.

	Args:
	    filters: Dict (or JSON string) with item_code and warehouse.

	Returns:
	    List of serial numbers reserved by open POS tickets, net of open
	    return tickets.

	Permissions:
	    Open by design: upstream's method is gateless and the POS page calls
	    it for every serialized item a clerk adds.
	"""
	if isinstance(filters, str):
		filters = json.loads(filters)

	pi = frappe.qb.DocType("POS Invoice")
	pii = frappe.qb.DocType("POS Invoice Item")
	rows = (
		frappe.qb.from_(pi)
		.from_(pii)
		.select(pi.is_return, pii.serial_no)
		.where(
			(pi.name == pii.parent)
			& (pi.docstatus == 1)
			& (pii.docstatus == 1)
			& (pi.consolidated_invoice.isnull() | (pi.consolidated_invoice == ""))
			& (pii.item_code == filters.get("item_code"))
			& (pii.warehouse == filters.get("warehouse"))
			& (pii.serial_no.isnotnull())
			& (pii.serial_no != "")
		)
	).run(as_dict=True)

	reserved, returned = [], []
	for r in rows:
		(returned if r.is_return else reserved).extend(get_serial_nos(r.serial_no))
	for sn in returned:
		if sn in reserved:
			reserved.remove(sn)
	return reserved
