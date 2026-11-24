"""Price Hub — what the buyer reads before ordering a product: each enabled
distributor's cost over time, and what this store paid and holds.

The page's search and offers are ``distributor.api.search``; this module adds the
two reads that one does not carry."""
from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from ffl_integrations.distributor import remote_catalog
from ffl_integrations.distributor.api import ORDER_ROLES
from ffl_integrations.distributor.items import local_item

RECEIPTS = 5


@frappe.whitelist()
def detail(upc: str | None = None, sku: str | None = None, distributor: str | None = None,
		days: int = 90) -> dict:
	"""One product's cost history and this store's own record of it.

	Args:
	    upc: The product's UPC.
	    sku: A distributor SKU — names the product when it has no UPC.
	    distributor: The Distributor that SKU belongs to.
	    days: History window in days; 0 is everything the service keeps.

	Returns:
	    {trend: {window_start, server_time, series: [{distributor, sku, segments:
	    [[{at, cost}]], last_cost, low_cost, high_cost}]} or null (no history, or
	    the service is down), store: {item_code, on_hand, price, receipts:
	    [{posting_date, supplier, qty, rate}]} or null (no Item here)}. Only
	    enabled Distributors have a series.
	"""
	frappe.only_for(ORDER_ROLES)
	upc, sku = (upc or "").strip(), (sku or "").strip()
	if not (upc or sku):
		return {"trend": None, "store": None}
	return {"trend": _trend(upc, sku, distributor, cint(days)), "store": _store(upc, sku)}


def _trend(upc, sku, distributor, days):
	houses = {}   # house code -> the first enabled Distributor buying through it
	for d in frappe.get_all("Distributor", filters={"enabled": 1},
			fields=["name", "provider"], order_by="priority asc, modified asc"):
		houses.setdefault(d.provider, d.name)
	code = next((c for c, name in houses.items() if name == distributor), None)
	try:
		raw = (remote_catalog.cost_trend(upc=upc, days=days) if upc
			else remote_catalog.cost_trend(distributor=code, sku=sku, days=days) if code else None)
	except Exception:  # noqa: BLE001 — the page shows the offers without a chart
		from ffl_integrations.notifications import safe_log_error

		safe_log_error(f"Cost history read failed ({upc or sku})", frappe.get_traceback())
		raw = None
	if not raw:
		return None
	return {"window_start": raw.get("window_start"), "server_time": raw.get("server_time"),
		"series": [{"distributor": houses[s["distributor"]], "sku": s.get("sku"),
				"segments": [seg.get("points") or [] for seg in s.get("segments") or []],
				"last_cost": s.get("last_cost"), "low_cost": s.get("low_cost"),
				"high_cost": s.get("high_cost")}
			for s in raw.get("series") or [] if s.get("distributor") in houses]}


def _store(upc, sku):
	item_code = local_item(sku, upc)
	if not item_code:
		return None
	from ffl_core.api.item_admin import _get_item_stock as get_item_stock
	from ffl_core.pricing import counter_list_price

	pr, line = frappe.qb.DocType("Purchase Receipt"), frappe.qb.DocType("Purchase Receipt Item")
	receipts = (frappe.qb.from_(line).join(pr).on(pr.name == line.parent)
		.select(pr.posting_date, pr.supplier, line.qty, line.rate)
		.where((line.item_code == item_code) & (pr.docstatus == 1) & (pr.is_return == 0))
		.orderby(pr.posting_date, pr.creation, order=frappe.qb.desc).limit(RECEIPTS)).run(as_dict=True)
	return {"item_code": item_code,
		"on_hand": flt((get_item_stock([item_code]) or {}).get(item_code)),
		"price": flt(counter_list_price(item_code)), "receipts": receipts}
