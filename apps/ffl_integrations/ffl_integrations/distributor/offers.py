"""One product's offers across the distributors — the single place that turns an
Item / SKU / UPC into each house's catalog row.

The quote, the Create Order fulfillment matrix and the Receive Goods UPC lookup
each carried their own copy of this chain; they share it here. A product's offers
across houses come from the cross-distributor catalog (``catalog``); one house's
row for a SKU still comes from that house's provider.
"""
from __future__ import annotations

import frappe

from ffl_integrations.distributor import dropship_hooks, remote_catalog


def listing_for(item_code, distributor, fields=("sku",), *, any_house=False):
	"""The Item's website listing at THIS distributor (a dict of ``fields``), or
	None — through the drop-ship layer (``dropship_hooks.listing_for``).

	Scoped to the house (a blank one matches nothing): the listing is named by
	item, so an unscoped read answers with whichever house happens to own the row —
	pricing house B's cost through house A's markup override and lock. Only
	``any_house`` asks "is it listed anywhere"."""
	return dropship_hooks.listing_for(item_code, distributor, fields, any_house=any_house)


def item_upc(item_code):
	"""The Item's UPC barcode, else the item code itself (Items are named by UPC
	under the promote identity chain)."""
	return frappe.db.get_value(
		"Item Barcode", {"parent": item_code, "barcode_type": "UPC"}, "barcode") or item_code


def catalog_row(provider, *, sku=None, item_code=None, upc=None):
	"""One house's catalog row for a product, or None when it does not carry it.

	The chain: SKU (else the item code) against the SKU-keyed catalog, then the
	UPC — explicit, else the Item's UPC barcode, else the item code. A provider
	without a UPC lookup answers only by SKU."""
	key = sku or item_code
	row = provider.catalog_row(key) if key else None
	if not row and (item_code or upc):
		by_upc = getattr(provider, "catalog_row_by_upc", None)
		if callable(by_upc):
			row = by_upc(upc or item_upc(item_code))
	return row


def with_own_offer(listing, offers, *, strict=False) -> list:
	"""A product's offers, plus the listing's own when they do not carry it (its
	house reads its own catalog service, or its SKU left the product): that house's
	row, if it still has one. ``listing``: a listing doc or row, or None.
	``strict``: a failed read raises (a decision must tell "cannot read" from "does
	not carry"); a view shows it as missing."""
	from ffl_integrations.distributor.api import get_provider

	offers = list(offers)
	if listing and not any((o["distributor"], o["sku"]) == (listing.get("distributor"),
			listing.get("sku")) for o in offers):
		try:
			# The provider itself, not console._catalog_row: that one's refusal is
			# queued for the page as a dialog even when it is caught here.
			row = get_provider(listing.get("distributor")).catalog_row(listing.get("sku"))
		except Exception:  # noqa: BLE001 — a view shows a missing row as missing
			if strict:
				raise
			row = None
		if row:
			offers.append(dict(row, distributor=listing.get("distributor")))
	return offers


def buy_order(offer) -> tuple:
	"""Sort key for the offer to buy from: in stock first, then a known cost, then
	the cheapest. Ties are the caller's (distributor priority)."""
	cost = float(offer.get("cost") or 0)
	return (int(offer.get("qty") or 0) <= 0, cost <= 0, cost)


def offers_for(*, upc, houses=None) -> list:
	"""Every enabled house's offer on one UPC, priority order:
	``[{**row, distributor}]`` — one per house (its first SKU on the UPC). Read
	from the cross-distributor product (``catalog``): one round trip, whatever the
	number of houses. A house outside that catalog (its own catalog service, or a
	provider with its own feed) answers through its provider. ``houses``:
	Distributor rows (name, provider) to answer for. A lookup, never an error:
	a house that fails, or the catalog down, is simply absent."""
	from ffl_integrations.distributor import catalog
	from ffl_integrations.distributor.api import get_provider

	if houses is None:
		houses = frappe.get_all("Distributor", filters={"enabled": 1},
			fields=["name", "provider"], order_by="priority asc, modified asc")
	covered = {h["name"] for h in catalog.houses()}
	found = {}
	try:
		product = catalog.product(upc, in_houses=[h for h in houses if h["name"] in covered])
		# An offer carries no name of its own: the product's title rides on each,
		# or the receive-time promote dialog names the Item after the manufacturer.
		name = (product or {}).get("description") or ""
		for offer in (product or {}).get("offers") or []:
			found.setdefault(offer["distributor"], {**offer, "item_name": name})
	except remote_catalog.DataServiceUnavailable:
		pass
	except Exception:  # noqa: BLE001 — a malformed answer is logged, the scan goes on
		from ffl_integrations.notifications import safe_log_error

		safe_log_error(f"Catalog UPC lookup failed ({upc})", frappe.get_traceback())
	for h in houses:
		if h["name"] in covered:
			continue
		try:
			row = catalog_row(get_provider(h["provider"]), upc=upc)
		except Exception:  # noqa: BLE001 — see docstring
			row = None
		if row:
			found[h["name"]] = {**row, "distributor": h["name"]}
	return [found[h["name"]] for h in houses if h["name"] in found]
