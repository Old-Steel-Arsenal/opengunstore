"""Distributor freight estimates: the one place that asks what distributors will
charge to ship a set of lines.

Each house prices its own orders (``DistributorProvider.estimate_order_freight``).
This module groups lines the way they will be ordered, by (distributor, lane) and
then by the house's own split, and asks the house to price each resulting order.
The counter's Fulfillment planner (``api.fulfillment_freight``) and extensions
(drop-ship sourcing) both read freight here, so a new distributor only has to
implement ``estimate_order_freight``. A house with flat per-order rates can return
:func:`flat_rate` over its Settings.

An estimate only: the distributor's invoiced freight always replaces it.
"""
from __future__ import annotations

from typing import Optional

import frappe
from frappe.utils import flt

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.base import PlanningError
from ffl_integrations.distributor.util import legal_firearm


def estimate_orders(lines, options=None) -> dict:
	"""The freight of the orders ``lines`` would become.

	``lines``: [{distributor, mode: "direct"|"to_store", sku, item_code?, qty, cost,
	department, ffl_required, is_firearm?}]; a firearm under the law rides FDS
	whatever ``mode`` says. ``options``: {options_key: {signature, adult_signature,
	insurance}}, the delivery options picked per order; an order without an
	entry uses its house's defaults.

	Returns {"orders": [{distributor, mode, order_type, line_count, merchandise,
	freight, firearm, options_key, options}], "total", "complete"}. ``freight`` is
	None, and ``complete`` False, for an order its house can't price or a lane it
	can't ship. Reads nothing from the catalog."""
	from ffl_integrations.distributor.api import DELIVERY_OPTIONS, delivery_options_key, get_provider

	options = options if isinstance(options, dict) else {}
	groups: dict = {}
	for ln in (lines or []):
		distributor = (ln.get("distributor") or "").strip()
		sku = (ln.get("sku") or "").strip()
		if not distributor or not sku:
			continue
		# A firearm under the law rides FDS; the house's own ffl_required flag (a
		# barrel it only sends to dealers) does not make one.
		house = frappe.get_cached_value("Distributor", distributor, "provider")
		gun = bool(ln["is_firearm"]) if "is_firearm" in ln else legal_firearm(ln, house)
		mode = "to_store" if ln.get("mode") == "to_store" else ("fds" if gun else "direct")
		groups.setdefault((distributor, mode), []).append({
			"sku": sku, "item_code": ln.get("item_code") or "",
			"qty": max(1, int(ln.get("qty") or 1)),
			"unit_cost": flt(ln.get("cost")), "department": ln.get("department") or "",
			# One flag for both: the firearm-classification cross-check belongs to real ordering, not
			# to a price estimate.
			"ffl_required": bool(ln.get("ffl_required")), "is_firearm": gun, "legal_firearm": gun,
		})
	orders, total, complete = [], 0.0, True
	for (distributor, mode), group in sorted(groups.items()):
		provider = get_provider(frappe.get_cached_doc("Distributor", distributor).provider)
		try:
			planned = provider.plan_fulfillment(group, {"mode": mode})
		except PlanningError:
			# This house cannot ship these lines this way (it does not run the lane, or
			# the two firearm classifications disagree). An estimate, not an order:
			# report it as unpriced and let the operator see the refusal where they
			# choose the channel.
			complete = False
			continue
		by_sku = {ln["sku"]: ln for ln in group}
		for plan in planned:
			priced = [{**by_sku[pl["sku"]], "qty": pl["qty"], "ship_method": pl["ship_method"]}
				for pl in plan["lines"]]
			firearm = any(ln.get("is_firearm") for ln in priced)
			key = delivery_options_key(distributor, plan["order_type"], firearm)
			chosen = getattr(provider, "delivery_options", lambda _f: None)(firearm)
			if chosen is not None and key in options:
				chosen = {k: bool((options.get(key) or {}).get(k)) for k in DELIVERY_OPTIONS}
			# options= only for a house with delivery options: a provider written to the
			# older two-argument signature still prices its orders.
			freight = provider.estimate_order_freight(priced, mode) if chosen is None \
				else provider.estimate_order_freight(priced, mode, options=chosen)
			if freight is None:
				complete = False
			else:
				total += freight
			orders.append({
				"distributor": distributor, "mode": mode, "order_type": plan["order_type"],
				"line_count": len(priced),
				"merchandise": round(sum(ln["unit_cost"] * ln["qty"] for ln in priced), 2),
				"freight": freight,
				"firearm": firearm, "options_key": key, "options": chosen,
			})
	return {"orders": orders, "total": round(total, 2), "complete": complete}


def flat_rate(lines: list, mode: str, *, consumer: Optional[dict] = None,
		store: Optional[dict] = None) -> Optional[float]:
	"""Freight for ONE order from a house's flat per-order rates.

	``consumer`` prices drop-ship orders (direct / FDS) and ``store`` prices orders
	shipped to the shop, each as ``{"firearm": rate, "accessory": rate}``. A house
	never mixes the two in one order. None when the house states no rate for this
	kind of order. Pure."""
	table = consumer if lanes.ships_to_consumer(mode) else store
	kind = "firearm" if any(ln.get("is_firearm") for ln in lines) else "accessory"
	rate = (table or {}).get(kind)
	return None if rate is None else round(float(rate), 2)
