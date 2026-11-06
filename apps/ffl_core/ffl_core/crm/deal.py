"""CRM Deal: every line of a POS item is priced as the register charges it today.

A gun priced one by one is priced from the gun on the line (``ffl_serial_no``), or the cheapest
one in stock while none is picked. The rate is always re-set; a discount goes in the line's
discount. Amounts are recomputed the way CRM's own form script does (amount = qty x rate, the
discount % off it, the deal's total and net total), so the deal reads the same in every view.
A won or lost deal is left as it was: its guns may be sold by now, and its amount is history.
A deal amount is a forecast, not revenue: the sale is rung at the register.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core import pricing
from ffl_core.crm.deals import is_closed
from ffl_core.crm.item import PRODUCT

SERIAL_FIELD = "ffl_serial_no"
# Set by crm.orders on its own save of a deal: an invoice's write-back never re-prices the quote.
WRITE_BACK_FLAG = "ffl_crm_write_back"


def _check_gun(serial: str, qty) -> None:
	"""The gun itself (its model, in stock) is checked by ``pricing.counter_price_for``."""
	if flt(qty) != 1:
		frappe.throw(_("A line for one gun ({0}) has quantity 1.").format(frappe.bold(serial)), title=_("One gun per line"))


def price_lines(doc, method=None):
	"""CRM Deal validate."""
	rows = doc.get("products") or []
	if not rows or doc.flags.get(WRITE_BACK_FLAG) or is_closed(doc.get("status")):
		return
	products = {p.name: p for p in frappe.get_all(PRODUCT, filters={"name": ["in", list({r.product_code for r in rows
		if r.product_code})]}, fields=["name", "erpnext_item_code", "product_name"])}
	for row in rows:
		product = products.get(row.product_code)
		item = product and product.erpnext_item_code
		if not item:
			continue   # a product that is not a POS item: CRM's own pricing
		row.product_name = row.product_name or product.product_name   # a line added outside CRM's form
		serial = row.get(SERIAL_FIELD) or None
		if serial:
			_check_gun(serial, row.qty)
		money = row.precision("amount")
		row.rate = flt(pricing.counter_price_for(item, serial), row.precision("rate"))
		row.amount = flt(flt(row.qty) * row.rate, money)
		if flt(row.discount_percentage):
			row.discount_amount = flt(flt(row.discount_percentage) / 100 * row.amount, money)
			row.net_amount = flt(row.amount - row.discount_amount, money)
		else:
			row.discount_amount = 0
			row.net_amount = row.amount
	total = flt(sum(flt(r.amount) for r in rows), doc.precision("total"))
	net_total = flt(sum(flt(r.net_amount) for r in rows), doc.precision("net_total"))
	doc.total = total
	# CRM's form script: net total falls back to the total unless discounts took it to 0.
	doc.net_total = net_total or (0 if any(flt(r.discount_percentage) > 0 for r in rows) else total)
