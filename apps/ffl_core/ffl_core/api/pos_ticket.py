"""A register ticket opened without the POS screen: a draft POS Invoice on the
open shift, for an agent (the MCP's pos_ticket) to tender and submit.

Built through the same helpers every server-side counter draft uses
(:mod:`ffl_core.pos_draft`), so it is rung on the caller's open shift with the
profile's stock and pricing flags, and ``set_missing_values`` seeds the
profile's payment rows. Nothing here approves a price or books anything: the
submit runs every register check (4473, NICS, paid in full, price approval).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.pos_draft import open_shift, profile_defaults
from ffl_core.roles import COUNTER_ROLES

# What a caller may put on a ticket line; anything else (accounts, locked rates,
# work-order links) is the builders' to set, never the caller's.
_LINE_KEYS = frozenset({"item_code", "qty", "rate", "serial_no", "storage_location"})


def _company(company: str | None) -> str:
	company = (company or "").strip() or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("No company is set up yet."))
	return company


def _lines(items) -> list[dict]:
	"""The caller's lines as POS Invoice Item rows, checked at the trust boundary."""
	items = frappe.parse_json(items) if isinstance(items, str) else items
	if not isinstance(items, list) or not items:
		frappe.throw(_("A ticket needs at least one line."))
	rows = []
	for i, raw in enumerate(items, 1):
		if not isinstance(raw, dict):
			frappe.throw(_("Line {0} is not an item.").format(i))
		stray = set(raw) - _LINE_KEYS
		if stray:
			frappe.throw(_("Line {0}: unknown field(s) {1}.").format(i, ", ".join(sorted(stray))))
		item_code = str(raw.get("item_code") or "").strip()
		if not item_code or not frappe.db.exists("Item", item_code):
			frappe.throw(_("Line {0}: item {1} was not found.").format(i, item_code or "?"))
		qty = flt(raw.get("qty") if raw.get("qty") not in (None, "") else 1)
		if qty <= 0:
			frappe.throw(_("Line {0}: qty must be more than 0.").format(i))
		row = {"item_code": item_code, "qty": qty}
		if raw.get("rate") not in (None, ""):
			rate = flt(raw["rate"], 2)
			if rate < 0:
				frappe.throw(_("Line {0}: rate cannot be negative.").format(i))
			row["rate"] = rate
		serial = str(raw.get("serial_no") or "").strip()
		if serial:
			if qty != 1:
				frappe.throw(_("Line {0}: a serial is one unit (qty 1).").format(i))
			sn = frappe.db.get_value("Serial No", serial, ["item_code", "warehouse"], as_dict=True)
			if not sn or sn.item_code != item_code:
				frappe.throw(_("Line {0}: serial {1} is not a {2}.").format(i, serial, item_code))
			row.update({"serial_no": serial, "use_serial_batch_fields": 1, "warehouse": sn.warehouse})
		location = str(raw.get("storage_location") or "").strip()
		if location:
			row["ffl_storage_location"] = location
		rows.append(row)
	return rows


def _set_rate(row, rate: float) -> None:
	"""Put the caller's *rate* on a priced *row* so that ERPNext's save keeps it to the cent.

	A rate under the line's list price is a discount off it: the row carries it as a percentage
	and an amount (what the register's past-order summary shows as "x% off") when the percentage,
	at its field precision, gives back exactly this rate under ERPNext's own arithmetic
	(``calculate_item_rate``: rate = list - round(list * % / 100)). Otherwise — or when it would
	pass the item's Max Discount — the percentage stays 0 and the amount is the difference, which is
	what ERPNext itself would save. A rate at or above the list price, or a line with no list
	price, carries no discount (ERPNext keeps the rate and sets a margin where it is above).
	Price approval reads the rate against the list price, never these fields.
	# lazy: one rounded percentage is tried; on a list price over ~$100 a cent rate it cannot
	# reproduce shows 0% with the amount still right (the register's own typed rate does the same).
	"""
	row.rate = rate
	row.discount_amount = row.discount_percentage = 0
	listed = flt(row.get("price_list_rate"))
	if listed <= 0 or rate >= listed:
		return
	row.margin_type, row.margin_rate_or_amount = None, 0
	pct = flt((1 - rate / listed) * 100, row.precision("discount_percentage"))
	amount = flt(listed * pct / 100, row.precision("discount_amount"))
	cap = flt(frappe.get_cached_value("Item", row.item_code, "max_discount"))
	if pct > 0 and flt(listed - amount, row.precision("rate")) == rate and not (cap and pct > cap):
		row.discount_percentage, row.discount_amount = pct, amount
	else:
		row.discount_amount = flt(listed - rate, row.precision("discount_amount"))


@frappe.whitelist(methods=["POST"])
def open_ticket(customer: str, items, company: str | None = None) -> dict:
	"""Open a draft register ticket (POS Invoice) on the open POS shift.

	Args:
	    customer: Customer name.
	    items: JSON list of lines ``[{item_code, qty=1, rate?, serial_no?,
	        storage_location?}]``; rate blank = the item's price for the
	        customer, a serial is one unit.
	    company: Company the shift is on; blank = the user's default company.

	Returns:
	    {"pos_invoice", "grand_total", "payment_modes": the profile's modes of
	    payment on the ticket}. Refused when no shift is open.
	"""
	frappe.only_for(COUNTER_ROLES)
	company = _company(company)
	customer = (customer or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} was not found.").format(customer or "?"))
	lines = _lines(items)
	shift = open_shift(company)
	inv = frappe.get_doc({
		"doctype": "POS Invoice", "company": company, "pos_profile": shift.pos_profile,
		"customer": customer, "items": lines, **profile_defaults(shift.pos_profile),
	})
	inv.flags.ignore_permissions = True
	inv.set_missing_values()
	# A rate under the list price goes in as its discount off it (ERPNext keeps a bare typed rate
	# but shows it as 0% off, and prices a bare $0 at the list price); others go in as sent.
	for row, line in zip(inv.items, lines):
		if "rate" in line and line["rate"] < flt(row.price_list_rate):
			_set_rate(row, line["rate"])
	inv.insert()
	# Pricing may move a typed rate on the way in; the caller's rate is the ticket's. (Price
	# approval judges a register save against the rate saved here, so it does not ask again.)
	moved = False
	for row, line in zip(inv.items, lines):
		if "rate" in line and flt(row.rate) != line["rate"]:
			_set_rate(row, line["rate"])
			moved = True
	if moved:
		inv.save()
	return {"pos_invoice": inv.name, "grand_total": flt(inv.rounded_total) or flt(inv.grand_total),
		"payment_modes": [p.mode_of_payment for p in inv.get("payments") or []]}
