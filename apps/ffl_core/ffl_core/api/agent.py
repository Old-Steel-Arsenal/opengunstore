"""Read-only entry points for Frappe's built-in AI agent (Flow).

Flow's call_api runs a whitelisted function marked ``@frappe.read_only()`` without an
approval card, inside a read-only transaction. The marker also sends the function's reads
to a replica when ``read_from_replica`` is set, so it goes only on these entry points and
never on functions the POS calls inside its own write transactions.
"""
from __future__ import annotations

import frappe

from ffl_core.api.item_admin import _get_item_stock
from ffl_core.firearm import _pos_picker_excluded_serials, available_serials_with_prices
from ffl_core.roles import COUNTER_ROLES

STOCK_READ_ROLES = (*COUNTER_ROLES, "Stock User")


@frappe.whitelist()
@frappe.read_only()
def firearm_stock_summary() -> dict:
	"""How many firearms the store has on hand, and how many of them can be sold.

	A gun is on hand while its Serial No is Active. It is not for sale when the POS
	picker would not offer it: held on a ticket or order, reserved by a web or
	GunBroker order, consigned out, or a customer's transfer gun.

	Returns:
	    {on_hand, for_sale, not_for_sale, antiques, by_warehouse: {warehouse: count},
	    by_type: {firearm_type: count}}. Antiques are counted in on_hand and are not
	    in the A&D book.
	"""
	frappe.only_for(STOCK_READ_ROLES)
	rows = frappe.db.sql(
		"""
		SELECT sn.name, sn.item_code, sn.warehouse, COALESCE(i.is_antique, 0) AS is_antique,
		       COALESCE(NULLIF(sn.firearm_type, ''), i.firearm_type) AS firearm_type
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
		WHERE sn.status = 'Active'
		""",
		as_dict=True,
	)
	not_for_sale = _pos_picker_excluded_serials({r.item_code for r in rows})
	by_warehouse: dict[str, int] = {}
	by_type: dict[str, int] = {}
	for r in rows:
		by_warehouse[r.warehouse or ""] = by_warehouse.get(r.warehouse or "", 0) + 1
		by_type[r.firearm_type or ""] = by_type.get(r.firearm_type or "", 0) + 1
	held = sum(1 for r in rows if r.name in not_for_sale)
	return {
		"on_hand": len(rows),
		"for_sale": len(rows) - held,
		"not_for_sale": held,
		"antiques": sum(1 for r in rows if r.is_antique),
		"by_warehouse": by_warehouse,
		"by_type": by_type,
	}


@frappe.whitelist()
@frappe.read_only()
def serials_for_sale(item_codes: list[str] | str) -> dict[str, list[dict]]:
	"""The units of each firearm item the counter would sell now, with their price.

	Args:
	    item_codes: Item codes — list, JSON list or comma-separated string.

	Returns:
	    {item_code: [{"serial", "sell_price", "purchase_rate"}, ...]}, cheapest first.
	"""
	frappe.only_for(COUNTER_ROLES)
	return available_serials_with_prices(item_codes)


@frappe.whitelist()
@frappe.read_only()
def item_stock(item_codes: list[str] | str) -> dict:
	"""Quantity on hand per item across all warehouses, for non-gun items.

	Args:
	    item_codes: Item codes — list, JSON list or comma-separated string.

	Returns:
	    {item_code: total_actual_qty}.
	"""
	frappe.has_permission("Item", "read", throw=True)
	return _get_item_stock(item_codes)
