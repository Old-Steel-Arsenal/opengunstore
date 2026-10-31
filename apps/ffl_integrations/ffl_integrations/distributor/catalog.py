"""The cross-distributor product catalog — the POS's one reader of the Data
Service's ``/v1/catalog/products*``: one product per UPC (``<house>:<sku>`` for an
offer with no UPC) carrying every house's live offer on it.

The listing rules read products here, and so does ``offers``; the source switcher
and the Drop-ship Center follow. A provider keeps what is its own house's business —
ordering, shipping, state restrictions, building an Item from a SKU.

Each offer comes back in the POS catalog-row shape (``remote_normalised``) with the
service's ``product_class`` and ``brand_key`` — the one definition of both — and is
tagged with the enabled Distributor that buys through its house (``distributor``) and
that house's code (``house``). An offer from a house no enabled Distributor uses is
dropped: nobody here can sell it.
"""
from __future__ import annotations

import frappe

from ffl_integrations.data_service import client
from ffl_integrations.distributor import remote_catalog
from ffl_integrations.distributor.remote_catalog_mixin import remote_normalised

PATH = "/v1/catalog/products"
PAGE_LIMIT = 500
BATCH_MAX = 200


def houses(*, dropship_only=False) -> list:
	"""The enabled Distributors this catalog speaks for ``[{name, provider}]``,
	priority order. ``dropship_only``: only those that drop-ship.

	A house reading its own catalog service (``catalog_endpoint``), or whose
	provider has no remote catalog, is not in the site's product catalog: its
	offers here would be another account's, or none. With the local catalog the
	endpoint override does not apply: every house is read from this site."""
	from ffl_integrations.catalog_local import is_local
	from ffl_integrations.distributor.api import get_providers, has_remote_catalog

	filters = {"enabled": 1}
	if dropship_only:
		filters["dropship_enabled"] = 1
	registry = get_providers()
	local = is_local()
	return [d for d in frappe.get_all("Distributor", filters=filters,
			fields=["name", "provider", "catalog_endpoint"], order_by="priority asc, modified asc")
		if (local or not (d.catalog_endpoint or "").strip())
			and has_remote_catalog(registry.get(d.provider))]


def products_page(filters=None, *, cursor=None, limit=PAGE_LIMIT, in_houses=None,
		sort=None, offset=0, count=False) -> dict:
	"""One page of products: ``{products, cursor, total}``.

	``filters`` match OFFERS, as the service defines them (a product is listed when
	one of its offers meets every filter): brand, product_class, distributor (house
	codes), department (``<house>:<code>``), min_cost, max_cost, in_stock,
	ffl_required, q. ``in_houses``: the ``houses()`` offers are kept for (default:
	every enabled Distributor). ``sort``: key order (the default) pages by
	``cursor``; brand, cost (the cheapest offer) and qty (the most stock) page by
	``offset``. ``count``: the total, on a first page only (else None)."""
	params = {k: v for k, v in (filters or {}).items() if v not in (None, "", [])}
	params["limit"] = min(int(limit), PAGE_LIMIT)
	if cursor:
		params["cursor"] = cursor
	if sort and sort != "key":
		params["sort"] = sort
		if offset:
			params["offset"] = int(offset)
	if count:
		params["count"] = True
	backend = remote_catalog._backend()
	data = backend.products_page(params) if backend is not None else client.get(PATH, _wire(params))
	tagged = houses() if in_houses is None else in_houses
	return {"products": [_product(p, tagged) for p in data.get("products") or []],
		"cursor": data.get("cursor"), "total": data.get("total")}


def iter_products(filters=None, *, in_houses=None):
	"""Every product matching ``filters``, key order."""
	cursor = None
	while True:
		page = products_page(filters, cursor=cursor, limit=PAGE_LIMIT, in_houses=in_houses)
		yield from page["products"]
		cursor = page["cursor"]
		if not cursor:
			return


def batch(keys, *, in_houses=None) -> dict:
	"""``{requested key: product}`` — unknown keys are absent. A 13-digit UPC
	with a leading zero comes back under the key it was asked by."""
	keys = list(dict.fromkeys(k for k in keys or [] if k))
	tagged = houses() if in_houses is None else in_houses
	backend = remote_catalog._backend()
	out: dict = {}
	for start in range(0, len(keys), BATCH_MAX):
		chunk = keys[start:start + BATCH_MAX]
		data = (backend.products_batch(chunk) if backend is not None
			else client.post(f"{PATH}/batch", {"keys": chunk}))
		by_key = {p["key"]: p for p in data.get("products") or []}
		renamed = data.get("keys") or {}
		for key in chunk:
			product = by_key.get(renamed.get(key, key))
			if product:
				out[key] = _product(product, tagged)
	return out


def legal_of(keys) -> dict:
	"""``{key: legal or None}`` — only the catalog master's legal answer for each
	product (``util.prefetch_legal``): the law for a row, not a product read for
	sourcing or pricing. Same endpoint as ``batch``; unknown keys are absent.
	``in_houses=[]``: no Distributor read, no offers tagged — only ``legal`` is kept."""
	return {k: p["legal"] for k, p in batch(keys, in_houses=[]).items()}


def product(key, *, in_houses=None):
	"""One product, or None."""
	return batch([key], in_houses=in_houses).get(key) if key else None


def products_of(listings, houses) -> dict:
	"""``{listing name: product}`` — each listing's product in the catalog (the
	one its own offer sits on now). A listing whose house the catalog does not
	cover (``houses``), or whose SKU left it, is absent."""
	codes = {h["name"]: h["provider"] for h in houses}
	by_house: dict = {}
	for l in listings:
		if l.get("distributor") in codes:
			by_house.setdefault(codes[l.get("distributor")], []).append(l)
	keys = {}
	for code, group in by_house.items():
		found = keys_for(code, [l.get("sku") for l in group])
		keys.update({l.get("name"): found[l.get("sku")] for l in group if l.get("sku") in found})
	products = batch(set(keys.values()), in_houses=houses)
	return {name: products[key] for name, key in keys.items() if key in products}


def changes(since=None) -> dict:
	"""One page of ``/products/changes``: ``{keys, cursor, more}`` — the products
	whose offers changed (or that an offer left) since ``since``, each once; pass
	``cursor`` back while ``more``. The cursor holds one sequence number per
	house; blank starts every house from the beginning."""
	backend = remote_catalog._backend()
	if backend is not None:
		return backend.products_changes(since or "")
	return client.get(f"{PATH}/changes", {"since": since or ""})


def facets(code=None) -> dict:
	"""``{brands: [{key, name, products}], classes: [{key, products}], departments:
	[{key, distributor, department, name, products}]}`` — what the filters can be
	set to, over live offers (not discontinued, not deleted), counting products.
	``code``: one house's offers only; else brands and classes span every house (a
	department is always one house's, its ``distributor`` the house code)."""
	params = {"distributor": code} if code else {}
	backend = remote_catalog._backend()
	return (backend.products_facets(**params) if backend is not None
		else client.get(f"{PATH}/facets", params))


def count(filters, *, in_houses=None) -> int:
	"""How many products ``filters`` match (`products_page`'s filters). ``in_houses``
	[] skips the Distributor read that tags a page's offers (a count has none)."""
	return int(products_page(filters, limit=1, in_houses=in_houses, count=True)["total"] or 0)


def keys_for(code, skus) -> dict:
	"""``{sku: product key}`` for a house's SKUs (``code``: the house) — the UPC its
	row names now, else ``<house>:<sku>``. A SKU gone from its catalog is absent."""
	return {sku: str(row.get("upc") or "").strip() or f"{code}:{sku}"
		for sku, row in (remote_catalog.rows(code, list(skus)) if skus else {}).items()}


def _wire(params) -> dict:
	"""Booleans as the service reads them; lists go out as repeated params."""
	return {k: ("true" if v else "false") if isinstance(v, bool) else v
		for k, v in params.items()}


def _product(raw, tagged) -> dict:
	"""The product with its offers tagged — one per Distributor on the offer's
	house, in the Distributors' priority order."""
	offers = []
	for house in tagged:
		for offer in raw.get("offers") or []:
			if offer["distributor"] != house["provider"]:
				continue
			offers.append(dict(remote_normalised(offer), brand_key=offer.get("brand_key") or "",
				product_class=offer.get("product_class") or "", house=offer["distributor"],
				distributor=house["name"],
				# The product's master legal answer rides on each offer, so
				# legal_firearm needs no second read (None: not classified).
				legal=raw.get("legal")))
	return {"key": raw["key"], "upc": raw.get("upc") or "",
		"manufacturer": raw.get("manufacturer") or "", "model": raw.get("model") or "",
		"description": raw.get("description") or "", "image_url": raw.get("image_url") or "",
		"long_description": raw.get("long_description") or "",
		"content_optimized": bool(raw.get("content_optimized")),
		"ffl_required": bool(raw.get("ffl_required")), "offers": offers,
		# The catalog master (Data Service): {slug, name, path} and {is_firearm,
		# nfa_type, is_antique}, null until the product is classified.
		"category": raw.get("category"), "legal": raw.get("legal"),
		"fits_platform": raw.get("fits_platform") or []}
