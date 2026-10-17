"""Setup for LOCKED invoice lines — the TRADE-IN-CREDIT line (setup/trade_in_credit)
and the GUNSMITH-DEPOSIT line (setup/gunsmith).

A locked line's amount is not a price: it is minus the trade-in's recorded
cost. Two things keep ERPNext's pricing machinery away from it (incident
A past invoice, +$180 credit):

1. ``ffl_locked_rate`` on POS Invoice Item / Sales Invoice Item — the amount
   the server pinned the row to (compliance/locked_lines.pin_locked_lines).
   The POS client re-pins the row from this field alone, so a counter-only
   cashier resuming a draft needs no read on FFL Acquisition / Sales Order.
   Not no_copy: POS→Sales Invoice consolidation and POS returns carry it.

2. An expired "tombstone" Item Price per locked item and enabled selling
   price list. Stock Settings `auto_insert_price_list_rate_if_missing` makes
   ERPNext insert an Item Price from the first sale's rate for any user who
   may write Item Price (get_item_details.insert_item_price) — prod got
   TRADE-IN-CREDIT = -869 that way, and every later re-price (customer /
   coupon / price-list change, qty, …) sent -869 to the cart. The price
   lookup filters on validity dates (get_item_details._get_item_price_query),
   so a price valid only on 1900-01-01 is never returned — the cart gets no
   price_list_rate for the item and leaves its rate alone — while the
   auto-insert's existence check ignores dates and returns early when any
   price exists and `update_existing_price_list_rate` is off (insert_item_price).
   ensure() also removes any live price for the locked items (self-heals a
   deleted tombstone on the next migrate / trade-in).
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import getdate

LOCKED_RATE_FIELD = "ffl_locked_rate"
TOMBSTONE_DATE = "1900-01-01"
TOMBSTONE_NOTE = (
	"ffl_core tombstone: keeps ERPNext from auto-inserting a price for this locked "
	"line item (its amount is never a price). Expired on purpose; do not edit."
)

_ROW_FIELDS = {
	dt: [
		{
			"fieldname": LOCKED_RATE_FIELD,
			"fieldtype": "Currency",
			"options": "currency",
			"label": "Locked Rate",
			"read_only": 1,
			"no_copy": 0,
			"description": "Amount this line is locked to (trade-in credit, gunsmith deposit); "
				"the invoice pins rate to it on every save.",
			"insert_after": "rate",
		}
	]
	for dt in ("POS Invoice Item", "Sales Invoice Item")
}


def locked_items() -> tuple[str, ...]:
	"""The locked line items. Lazy: setup/trade_in_credit imports this module."""
	from ffl_core.setup.gunsmith import DEPOSIT_ITEM
	from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
	return (TRADE_IN_CREDIT_ITEM, DEPOSIT_ITEM)


def ensure_fields() -> None:
	create_custom_fields(_ROW_FIELDS, ignore_validate=True)


def is_tombstone(price) -> bool:
	"""An Item Price row/dict whose validity is the single tombstone day."""
	vf, vu = price.get("valid_from"), price.get("valid_upto")
	return bool(vf and vu and getdate(vf) == getdate(TOMBSTONE_DATE) == getdate(vu))


def ensure_price_tombstones() -> dict:
	"""Delete live Item Prices for the locked items; make sure each enabled
	selling price list has a tombstone for each of them. Idempotent."""
	out = {"deleted": 0, "created": 0}
	items = [i for i in locked_items() if frappe.db.exists("Item", i)]
	if not items:
		return out
	for price in frappe.get_all(
		"Item Price",
		filters={"item_code": ["in", items]},
		fields=["name", "valid_from", "valid_upto"],
	):
		if not is_tombstone(price):
			frappe.delete_doc("Item Price", price.name, ignore_permissions=True, force=True)
			out["deleted"] += 1
	price_lists = frappe.get_all("Price List", filters={"enabled": 1, "selling": 1}, pluck="name")
	for item_code in items:
		stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
		for price_list in price_lists:
			if frappe.db.exists("Item Price", {
				"item_code": item_code, "price_list": price_list,
				"valid_from": TOMBSTONE_DATE, "valid_upto": TOMBSTONE_DATE,
			}):
				continue
			frappe.get_doc({
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"uom": stock_uom,
				"price_list_rate": 0,
				"valid_from": TOMBSTONE_DATE,
				"valid_upto": TOMBSTONE_DATE,
				"note": TOMBSTONE_NOTE,
			}).insert(ignore_permissions=True)
			out["created"] += 1
	return out


def ensure() -> dict:
	"""Row field + tombstones. Called from trade_in_credit.ensure (lazy at first
	use + after_migrate); like it, it must never raise."""
	try:
		ensure_fields()
		return ensure_price_tombstones()
	except Exception:
		frappe.log_error(title="locked_lines.ensure failed", message=frappe.get_traceback())
		return {"error": 1}


def on_price_list_update(doc, method: str | None = None) -> None:
	"""Price List on_update: an enabled selling list gets its tombstones at once,
	so ERPNext never auto-inserts a live price for a locked item on a list made
	after the migrate. Never blocks the save."""
	if not (doc.get("selling") and doc.get("enabled")):
		return
	try:
		ensure_price_tombstones()
	except Exception:
		frappe.log_error(title="locked_lines: tombstones for a new price list failed",
			message=frappe.get_traceback())
