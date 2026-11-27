"""Price Hub — what the buyer reads before ordering a product: each enabled
distributor's cost over time, what this store paid, holds, has on order and sells,
and the cost drops and thinning stock the distributors' feeds show.

The page's search and offers are ``distributor.api.search``; this module adds the
reads that one does not carry."""
from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, flt, nowdate

from ffl_integrations.distributor import catalog, remote_catalog
from ffl_integrations.distributor.api import ORDER_ROLES
from ffl_integrations.distributor.items import local_item

RECEIPTS = 5
# The window drawn when everything the service keeps is too much to return whole.
LONGEST_WINDOW_DAYS = 365
SIGNAL_LISTS = ("cost_drop", "stockout", "fast_depletion")
SIGNAL_ROWS = 15
# A stock movement out through one of these is a sale (a return comes back in on the same).
_SALES_VOUCHERS = ("Sales Invoice", "POS Invoice", "Delivery Note")
# Store-bound orders that can still bring units in (receive_bridge._INCOMING_ORDER_TYPES),
# drafts and queued ones included: the buyer must see what is already asked for.
_STORE_BOUND = ("Stocking", "Procure To Store")
_ORDER_OVER = ("Cancelled", "Error", "Complete")
# The house may not have answered these yet: a line's acked quantity of 0 means "not told",
# not "none". Hold can come before any answer, and counting an order twice costs less than
# missing one.
_ORDER_UNANSWERED = ("Draft", "Queued", "Placed", "Hold")


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
	    {trend: {days, window_start, server_time, series: [{distributor, sku, state,
	    segments: [[{at, cost}]], last_cost, low_cost, high_cost}]} or null (no
	    history, or the service is down), store: {item_code, on_hand, on_order,
	    sold_30, sold_90, price, receipts: [{posting_date, supplier, qty, rate}]} or
	    null (no Item here)}. Only enabled Distributors the site-wide catalog serves
	    have a series (not one on its own catalog service). ``trend.days``
	    is the window read: a year when everything kept was asked for and is too
	    much to return.
	"""
	frappe.only_for(ORDER_ROLES)
	upc, sku = (upc or "").strip(), (sku or "").strip()
	if not (upc or sku):
		return {"trend": None, "store": None}
	return {"trend": _trend(upc, sku, distributor, cint(days)), "store": _store(upc, sku)}


@frappe.whitelist()
def signals(days: int = 7) -> dict:
	"""What the enabled distributors' feeds showed lately: costs that fell, stock
	about to run out, and stock that went fast after a restock.

	Args:
	    days: Window in days, 1 to 30.

	Returns:
	    {cost_drop, stockout, fast_depletion}: each a list of {distributor, sku, upc,
	    name, current_value, reference_value, amount, score}, the strongest first.
	    cost_drop: the cost now and before, the fall in dollars and percent.
	    stockout: units left, and score the days of cover. fast_depletion: units
	    left, units gone, and the percent gone. Observations of the feed, not sales.
	    Empty lists when the service keeps no history or is down.
	"""
	frappe.only_for(ORDER_ROLES)
	days = min(max(cint(days) or 7, 1), 30)
	out = {name: [] for name in SIGNAL_LISTS}
	from ffl_integrations.data_service import CapabilityOff

	for code, house in _houses().items():
		try:
			raw = remote_catalog.signals(code, days, SIGNAL_ROWS) or {}
		except CapabilityOff:   # no catalog set up for this house: nothing to list, nothing wrong
			continue
		except Exception:  # noqa: BLE001 — one house down leaves the others' lists
			_log(f"Inventory signals read failed ({code})")
			continue
		for name in SIGNAL_LISTS:
			out[name] += [{"distributor": house, **{k: row.get(k) for k in (
				"sku", "upc", "name", "current_value", "reference_value", "amount", "score")}}
				for row in raw.get(name) or []]
	# Cover days: the fewest first. Percent fallen or gone: the most first.
	out["stockout"].sort(key=lambda r: flt(r["score"]))
	for name in ("cost_drop", "fast_depletion"):
		out[name].sort(key=lambda r: -flt(r["score"]))
	return {name: rows[:SIGNAL_ROWS] for name, rows in out.items()}


def _houses(*, site_catalog_only=False) -> dict:
	"""``{house code: the first enabled Distributor buying through it}``, priority order.

	``site_catalog_only``: only the houses the site-wide catalog speaks for
	(``catalog.houses()``), which a cross-house read like the cost history needs: a
	Distributor on its own catalog service is not in it."""
	rows = catalog.houses() if site_catalog_only else frappe.get_all(
		"Distributor", filters={"enabled": 1}, fields=["name", "provider"],
		order_by="priority asc, modified asc")
	houses = {}
	for d in rows:
		houses.setdefault(d.provider, d.name)
	return houses


def _log(title) -> None:
	from ffl_integrations.notifications import safe_log_error

	safe_log_error(title, frappe.get_traceback())


def _trend(upc, sku, distributor, days):
	houses = _houses(site_catalog_only=True)
	code = next((c for c, name in houses.items() if name == distributor), None)
	key = {"upc": upc} if upc else {"distributor": code, "sku": sku} if code else None
	raw = None
	try:
		if key:
			try:
				raw = remote_catalog.cost_trend(**key, days=days)
			except remote_catalog.DataServiceRejected:
				# Everything kept is too much to return whole: the longest window that is not.
				# A key the service does not read is rejected at any window, and has no history.
				if days and days <= LONGEST_WINDOW_DAYS:
					return None
				days = LONGEST_WINDOW_DAYS
				raw = remote_catalog.cost_trend(**key, days=days)
	except remote_catalog.DataServiceRejected:
		return None
	except Exception:  # noqa: BLE001 — the page shows the offers without a chart
		_log(f"Cost history read failed ({upc or sku})")
	if not raw:
		return None
	return {"days": days, "window_start": raw.get("window_start"), "server_time": raw.get("server_time"),
		"series": [{"distributor": houses[s["distributor"]], "sku": s.get("sku"),
				# fresh / stale / incomplete / no_data: whether the last sample is current
				"state": s.get("state") or "",
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
		"on_order": _on_order(item_code),
		"sold_30": _sold(item_code, 30), "sold_90": _sold(item_code, 90),
		"price": flt(counter_list_price(item_code)), "receipts": receipts}


def _on_order(item_code) -> int:
	"""Units of the Item on open store-bound distributor orders that have not come
	in: what the house said it is sending (the larger of acked and invoiced) — or,
	on an order it has not answered yet, what was asked for — less what was received
	or covered from the shelf. A killed line sends nothing, and so does a line the
	house answered with none."""
	orders = dict(frappe.get_all("Distributor Order", fields=["name", "status"], as_list=True, filters={
		"order_type": ("in", _STORE_BOUND), "status": ("not in", _ORDER_OVER)}))
	if not orders:
		return 0
	total = 0
	for l in frappe.get_all("Distributor Order Line",
			filters={"parent": ("in", list(orders)), "item_code": item_code},
			fields=["parent", "wish_qty", "ack_qty", "invoiced_qty", "received_qty", "substituted_qty",
				"line_status"]):
		if (l.line_status or "") == "Killed":
			continue
		coming = max(cint(l.ack_qty), cint(l.invoiced_qty))
		if not coming and orders[l.parent] in _ORDER_UNANSWERED:
			coming = cint(l.wish_qty)
		total += max(0, coming - cint(l.received_qty) - cint(l.substituted_qty))
	return total


def _sold(item_code, days) -> float:
	"""Units sold in the last ``days`` days, returns taken off: the stock that left
	on sales vouchers, read from the stock ledger so each sale counts once however
	it was rung up."""
	sle = frappe.qb.DocType("Stock Ledger Entry")
	from frappe.query_builder.functions import Sum

	out = (frappe.qb.from_(sle).select(Sum(sle.actual_qty))
		.where((sle.item_code == item_code) & (sle.is_cancelled == 0)
			& sle.voucher_type.isin(_SALES_VOUCHERS)
			& (sle.posting_date >= add_days(nowdate(), -days)))).run()
	return max(0.0, -flt(out[0][0] if out else 0))
