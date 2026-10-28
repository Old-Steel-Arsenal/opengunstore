"""Distributor generic layer — provider registry + whitelisted UI aliases.

The registry is built from the ``distributor_providers`` hook (dotted-path list
of zero-arg factories), keyed by ``provider.code``, first registration winning.
Nothing here imports a concrete provider module — the Desk page and the router
call ``distributor.api.*`` and stay provider-agnostic.
"""
from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from ffl_integrations.distributor import order_state
from ffl_integrations.distributor import freight, lanes, offers
from ffl_integrations.distributor.util import (case_note, case_pricing, dealer_only_part,
	legal_firearm)
from ffl_integrations.distributor.base import PlanningError
from ffl_integrations.distributor.offers import buy_order
from ffl_integrations.distributor.queue import enqueue_distributor

PROVIDERS_HOOK = "distributor_providers"

# Who may create / confirm / place / cancel a Distributor Order. Reads (search,
# availability, list) are open to any signed-in desk user.
ORDER_ROLES = ("Purchase Manager", "Purchase User", "Stock Manager", "System Manager")
# Who may run the counter drop-ship item-ensure (a WRITE: Item insert + stamp) —
# the same people who can ring the counter order the ensured Item is for. Single
# source in ffl_core.roles (endpoint-gates 2026-08-07 consolidation).
from ffl_core.roles import COUNTER_ROLES
# The metabox drives confirm/cancel/update_ffl, so the Distributor
# Console role must reach exactly those three. INCLUDED, not substituted — every
# incumbent ORDER_ROLE keeps its access (the LISTING_ROLES precedent). Only the
# three named gates use this; transition_order and the rest stay on ORDER_ROLES.
# The WordPress proxy's headless identity. Core owns the name (these gates are
# core); options.ensure_console_role seeds the Role.
CONSOLE_ROLE = "Distributor Console"
ORDER_ROLES_WITH_CONSOLE = ORDER_ROLES + (CONSOLE_ROLE,)


def get_providers() -> dict:
	"""Registry keyed by provider.code. First registration of a code wins."""
	providers: dict = {}
	for path in frappe.get_hooks(PROVIDERS_HOOK):
		provider = frappe.get_attr(path)()
		providers.setdefault(provider.code, provider)
	return providers


def get_provider(code: str):
	providers = get_providers()
	provider = providers.get(code)
	if provider is not None:
		return provider
	# Fallback: callers on the console/rules paths pass a Distributor NAME ('RSR'),
	# not a provider code ('rsr'). A name is not a registered code, so retry it as a
	# Distributor and resolve its provider field. quote's path (dist.provider) is
	# unaffected — it already passes a real code and hits the line above.
	mapped = frappe.db.get_value("Distributor", code, "provider")
	if mapped and mapped in providers:
		return providers[mapped]
	frappe.throw(frappe._("No distributor provider registered for code '{0}'.").format(code))


def house_setting(distributor: str, key: str, default) -> float:
	"""A numeric setting from the house's own Settings single (its provider's
	``settings_doctype``) — ``default`` for a house without one, or a value never
	set (a stated 0 is a real value, see ``util.setting_value``)."""
	from ffl_integrations.distributor.util import setting_value

	code = frappe.db.get_value("Distributor", distributor, "provider") or ""
	# get_providers, not get_provider: an unregistered code must read as "no
	# Settings", not throw (a throw leaves an error dialog behind).
	provider = get_providers().get(code)
	single = getattr(provider, "settings_doctype", "") if provider else ""
	if not single:
		return float(default)
	return setting_value(frappe.get_cached_doc(single), key, default)


def lane_block(distributor: str, mode: str, *, is_firearm: bool = False) -> str | None:
	"""Why the store's own switches say this house cannot ship a line this way —
	a phrase to put in a sentence, or None when it can.

	One answer for every caller (the counter planner, create_order, restock). A
	firearm asked for drop-ship is really the FDS lane, so it is judged as one.
	Whether the house's integration can run the lane AT ALL is a different question,
	answered by its ``capabilities`` where the plan is built (base.plan_fulfillment)
	and where the planner decides which buttons exist (:func:`_matrix_cell`).
	"""
	route = lanes.lane(lanes.FDS if (mode == lanes.DIRECT and is_firearm) else mode)
	house = frappe.db.get_value(
		"Distributor", distributor,
		["enabled", "dropship_enabled", "stocking_enabled"], as_dict=True)
	if not house:
		return frappe._("distributor {0} doesn't exist").format(distributor)
	if not house.enabled:
		return frappe._("distributor {0} is not enabled").format(distributor)
	if route.family == lanes.DROPSHIP and not house.dropship_enabled:
		return frappe._("drop-ship is switched off for this distributor")
	if route.family == lanes.STORE and not house.stocking_enabled:
		return frappe._("ship to store is switched off for this distributor")
	return None


def provider_supports(provider, capability: str) -> bool:
	"""Generic-layer capability gate — the automated-placement / drop-ship paths
	skip a provider (e.g. a human-order distributor) that omits the capability."""
	return capability in provider.capabilities()


def has_remote_catalog(provider) -> bool:
	"""Is this house's catalog served by the shared Data Service catalog service?

	Sniffed on `catalog_content_heartbeat` rather than the `capabilities()` set:
	"catalog" in capabilities only promises `catalog_row`, which a house could
	answer from its own FTP dump. The content heartbeat is the narrower and
	honest signal — only a provider reading through `remote_catalog` can produce
	one, and only such a house has anything to show on the Catalog Service page.
	Tolerates None (unregistered provider code) so callers can pass a registry
	lookup straight in."""
	return callable(getattr(provider, "catalog_content_heartbeat", None))


def default_provider_code() -> str:
	"""The provider to use when the UI doesn't name one: the top-priority enabled
	Distributor's provider, else the sole registered provider."""
	enabled = frappe.get_all(
		"Distributor", filters={"enabled": 1}, fields=["provider"],
		order_by="priority asc, modified asc")
	if enabled:
		return enabled[0].provider
	codes = list(get_providers().keys())
	if len(codes) == 1:
		return codes[0]
	# Two+ providers registered (RSR + Sports South) but no house enabled yet:
	# the top-priority Distributor ROW still names the house this site was set
	# up around, which is what a quote/search without a name should mean.
	rows = frappe.get_all("Distributor", fields=["provider"],
		order_by="priority asc, modified asc")
	for r in rows:
		if r.provider in codes:
			return r.provider
	frappe.throw(frappe._(
		"No distributor is enabled and several providers are registered — enable a "
		"Distributor in the hub or name one explicitly."))


def _resolve(distributor: str | None):
	code = distributor or default_provider_code()
	return get_provider(code)


def _default_distributor_name() -> str | None:
	"""The enabled Distributor doc behind ``default_provider_code()`` — the SAME
	canonical chain ``search`` resolves through, so a caller that searched without
	naming a house always quotes/ensures against the house that answered.

	``order_by`` mirrors default_provider_code()'s "priority asc, modified asc"
	(explicit hub order, oldest-first tiebreak for unset rows): with several
	enabled rows on one provider an unordered get_value picks arbitrarily — on
	the shared dev site it picked a leftover TEST row over the real one
	(2026-08-01)."""
	code = default_provider_code()
	return (frappe.db.get_value(
		"Distributor", {"provider": code, "enabled": 1}, "name",
		order_by="priority asc, modified asc")
		# same fallback default_provider_code() takes when no house is enabled
		or frappe.db.get_value("Distributor", {"provider": code}, "name",
			order_by="priority asc, modified asc"))


def _distributor_name(distributor: str | None) -> str | None:
	"""Distributor docname from either a docname or a provider code, else the
	default house. ``search``/``quote``/``ensure`` all accept both spellings."""
	given = (str(distributor or "")).strip()
	if given:
		if frappe.db.exists("Distributor", given):
			return given
		return frappe.db.get_value(
			"Distributor", {"provider": given, "enabled": 1}, "name")
	return _default_distributor_name()


# ----------------------------------------------------------------- whitelisted

@frappe.whitelist()
def search(query: str, limit: int = 10, distributor: str | None = None) -> list:
	"""Typeahead over the resolved provider's catalog (RSR reuses rsr/search.py).

	Open read, no only_for: catalog-level data carries no PII, and the console role
	depends on this openness. Do NOT add a gate here — the only effect would
	be to break the counter Create Order panel, with no security gain.

	Clamped to the service contract (`CatalogSearchRequest`: `q` min_length=1,
	`limit` 1..50) — same shape as rsr/search.py's shim — so an empty/blank query
	or an out-of-range limit never reaches the transport at all.

	Args:
	    query: Search term (blank returns []).
	    limit: Row cap, clamped to 1–50 (default 10).
	    distributor: Optional Distributor name; defaults to the resolved
	        primary.

	Returns:
	    Catalog rows, each tagged with its `distributor` provenance.

	Permissions:
	    Open read for any signed-in desk user (load-bearing, see above).
	"""
	query = (query or "").strip()
	if not query:
		return []
	limit = min(max(1, int(limit or 10)), 50)
	if distributor:
		dist_name = _distributor_name(distributor) or ""
		rows = _resolve(distributor).search(query, limit)
		for r in rows:
			r.setdefault("distributor", dist_name)
		return rows
	return product_search(query, limit)


@frappe.whitelist()
def catalog_by_upc(upc: str) -> list:
	"""The enabled houses' catalog rows for one UPC — the Receive Goods page's
	fallback when a scanned barcode matches no Item, so the Item can be created
	from the catalog instead of typed.

	Args:
	    upc: The scanned barcode.

	Returns:
	    [{distributor, sku, upc, item_name, manufacturer, model, cost, map, msrp,
	    qty, ffl_required, department, image_url}] — one row per house that
	    lists it; a house whose catalog is down is simply absent.

	Permissions:
	    Open read for any signed-in desk user (same as `search`).
	"""
	from ffl_integrations.data_service import CATALOG, is_off

	upc = (upc or "").strip()
	if not upc or is_off(CATALOG):   # no catalog: the page shows the "not set up" notice
		return []
	return offers.offers_for(upc=upc)


def product_search(query: str, limit: int) -> list:
	"""Unnamed search: ONE row per product (UPC) across every enabled house,
	tagged ``distributor: ""`` — the picker labels it "API" and the sourcing
	decision waits for the Fulfillment planner. Only offers from enabled
	Distributors count (a disabled house is invisible, as with named search).

	Row shape keeps the legacy keys (sku/cost/qty from the best offer) so the
	picker renders it unchanged, plus ``upc``, ``sources`` (house names) and
	``offers`` ([{distributor, sku, cost, qty}]) for the pending card."""
	from ffl_integrations.data_service import CATALOG, is_off
	from ffl_integrations.distributor import remote_catalog

	# A cross-house read: only Data Service Settings (or Local) serves it — a house's
	# own service does not. Off: the picker shows the notice, nothing is logged.
	if is_off(CATALOG, house=None):
		return []
	houses = {d.provider: d.name for d in frappe.get_all(
		"Distributor", filters={"enabled": 1}, fields=["name", "provider"],
		order_by="priority asc, modified asc")}
	if not houses:
		return []
	try:
		hits = remote_catalog.search(None, query, limit)
	except remote_catalog.DataServiceUnavailable:
		frappe.log_error(title="Catalog search degraded (Data Service down)",
			message=frappe.get_traceback())
		return []
	picked = []
	for hit in hits:
		offers = [o for o in (hit.get("offers") or []) if o.get("distributor") in houses]
		if offers:
			# Buy order first, house priority on ties: the page defaults a cart line
			# to the first offer that fills its qty, so it never re-ranks on its own.
			offers.sort(key=lambda o, rank=list(houses): (buy_order(o), rank.index(o["distributor"])))
			picked.append((hit, offers, offers[0]))
	# Search offers carry no class; the flagged best offers get theirs in one read a house.
	remote_catalog.classify([best for _h, _o, best in picked])
	from ffl_integrations.distributor.util import master_key, prefetch_legal
	prefetch_legal([master_key(best, best.get("distributor")) for _h, _o, best in picked])
	rows = []
	for hit, offers, best in picked:
		rows.append({
			"upc": hit.get("upc") or best.get("upc") or "",
			"sku": best.get("sku"),
			"item_name": hit.get("description") or best.get("short_description") or "",
			"manufacturer": hit.get("manufacturer") or best.get("manufacturer") or "",
			"model": hit.get("model") or best.get("model") or "",
			"image_url": hit.get("image_url") or best.get("image_url") or "",
			"ffl_required": bool(hit.get("ffl_required", best.get("ffl_required"))),
			# A firearm under the law (the best offer's service class) — what the
			# FIREARM pill and the lane follow; ffl_required is the house's shipping rule.
			"is_firearm": legal_firearm(best),
			"department": best.get("department") or "",
			"cost": best.get("cost"), "map": best.get("map"), "msrp": best.get("msrp"),
			"qty": sum(int(o.get("qty") or 0) for o in offers),
			"distributor": "",
			"sources": [houses[o["distributor"]] for o in offers],
			"offers": [{"distributor": houses[o["distributor"]], "sku": o.get("sku"),
				"cost": o.get("cost"), "qty": int(o.get("qty") or 0)} for o in offers],
		})
	return rows[:limit]


@frappe.whitelist()
def check_availability(lines, distributor: str | None = None) -> list:
	"""Live qty/price second-confirm before a Draft order is placed.

	Open read (no only_for) by design — see ``search``; the console depends on it
	and the counter uses it. Do not add a gate.

	Args:
	    lines: [{sku, qty}] — list or JSON string.
	    distributor: Optional Distributor name.

	Returns:
	    Provider availability rows (live qty/price per line).

	Permissions:
	    Open by design — any authenticated
	    desk user; see `search`.
	"""
	if isinstance(lines, str):
		lines = frappe.parse_json(lines)
	return _resolve(distributor).check_availability(lines or [])


@frappe.whitelist()
def quote(item_code=None, sku=None, distributor=None) -> dict:
	"""Per-item drop-ship quote for the Create Order panel and the counter
	server-side eligibility check.

	``fulfillment_mode`` is the RESOLVED judgement (Direct / To Store)
	through the canonical chain — consumers judging drop-shippability read IT,
	not the raw flags. ``restricted_states`` = states where the distributor's
	own restriction feed blocks drop-ship of this SKU — display only, and
	NOT the operator's ``Item.restricted_states`` compliance field.
	``suggested_price`` is the SAME chain the listing
	pipeline prices with (``listing_price.price_for``) — never a second pricing path —
	as the COUNTER price: the website's MAP floor does not apply at the counter
 so it can sit below the listing's website price. Reads the
	provider's LOCAL catalog (no HTTP), so it is cheap enough per keystroke; the
	real-time RSR re-confirm stays at placement (``place_order_job``).

	A read — open to any signed-in desk user (like ``search`` / ``check_availability``).
	This openness is load-bearing: it is WHY the counter register can quote without
	the console role. Do not add only_for here (the one effect is to break
	Create Order, with no security gain since catalog data has no PII).

	Permissions:
	    Open by design — any authenticated desk
	    user; catalog data carries no PII and the counter panel depends on it.

	Args:
	    item_code: Local Item code (resolves the distributor stamp when
	        `distributor` is omitted).
	    sku: Distributor SKU (either this or item_code must resolve).
	    distributor: Optional Distributor name.

	Returns:
	    {found, sku, upc, item_code, distributor, cost, map, msrp,
	    suggested_price, qty, restricted_states, fulfillment_mode,
	    dropship_blocked, vendor_approval_required, ffl_required,
	    manufacturer, model, department} — or {found: False, ...} when not in catalog.
	    A site with no distributor catalog answers HTTP 417 (CapabilityOff).
	"""
	from ffl_integrations.data_service import CATALOG, require
	from ffl_integrations.distributor import dropship_hooks

	require(CATALOG)

	item_code = (str(item_code or "")).strip() or None
	sku = (str(sku or "")).strip() or None
	dist_name = (str(distributor or "")).strip() or (
		frappe.db.get_value("Item", item_code, "dropship_distributor") if item_code else None)
	if not dist_name:
		# The Create Order panel searches by SKU alone (no item / no distributor named).
		# Resolve through the SAME canonical path `search` uses — default_provider_code()
		# — then take that provider's enabled Distributor. Picking "first enabled
		# Distributor" independently would let a multi-distributor site quote one house
		# while the typeahead searched another.
		dist_name = _default_distributor_name()
	if not dist_name:
		frappe.throw(frappe._(
			"No distributor is configured — enable one, or set Item.dropship_distributor."))
	# Cached: the metabox quotes once per order line, and Distributor is master data
	# that cannot change mid-request.
	dist = frappe.get_cached_doc("Distributor", dist_name)
	provider = get_provider(dist.provider)

	# Scoped to THIS distributor: quoting house B must never price
	# B's cost through house A's markup override and lock.
	listing = offers.listing_for(item_code, dist_name, fields=("sku", "markup_override_pct",
		"price_locked", "last_pushed_price", "fulfillment_mode"))
	sku = sku or (listing.get("sku") if listing else None) or item_code

	# No listing row maps item_code → distributor sku for the counter-ensure
	# population, so `sku` degrades to the item_code (the row's UPC under the
	# promote identity chain) and offers.catalog_row falls back to the UPC.
	row = offers.catalog_row(provider, sku=sku, item_code=item_code)
	if row and row.get("sku"):
		sku = row["sku"]  # re-key: restrictions + the response quote the DISTRIBUTOR sku
	if not row:
		return {"found": False, "sku": sku, "item_code": item_code, "distributor": dist_name}

	# `restricted_states` on a quote means "states where the DISTRIBUTOR
	# blocks drop-ship of this SKU somewhere" — RSR's authoritative restriction
	# feed, not the operator's Item compliance field and not the old guessed
	# catalog column. Optional provider method (the feed_heartbeat shape): a
	# provider without a restriction feed quotes an empty list.
	blocked_fn = getattr(provider, "blocked_states", None)
	blocked_states = (blocked_fn(row.get("sku") or sku) or []) \
		if callable(blocked_fn) else []

	listing = listing or frappe._dict(
		markup_override_pct=None, price_locked=0, last_pushed_price=0)

	# The RESOLVED fulfilment mode, through the one canonical chain
	# (materialised listing mode → brand policy → distributor flags → Direct).
	# This is what judgement consumers (counter eligibility, the Create Order
	# panel) must read — never the raw flags, which a `Direct OK` brand
	# authorization legitimately overrides. The raw flags stay in the response
	# as FACTS for display ("flag overridden by brand authorization").
	from ffl_integrations.distributor import policy as _policy
	from ffl_integrations.distributor import router as _router

	fulfillment_mode = _router.accessory_mode(
		listing.get("fulfillment_mode"),
		brand_policy=_policy.brand_policy_for(row.get("manufacturer"), dist_name),
		dropship_blocked=bool(row.get("dropship_blocked")),
		vendor_approval_required=bool(row.get("vendor_approval_required")))
	fulfillment_reason = "" if fulfillment_mode == "Direct" else (
		_policy.brand_policy_reason(row.get("manufacturer"), dist_name,
			distributor_name=dist.distributor_name or dist_name)
		or ("distributor block — ship to store only" if row.get("dropship_blocked")
		else "vendor approval required — ship to store only"))

	return {
		"found": True,
		"distributor": dist_name,
		"sku": row.get("sku") or sku,
		"upc": row.get("upc") or "",
		"item_code": item_code,
		"cost": flt(row.get("cost")),
		"map": flt(row.get("map")),
		"msrp": flt(row.get("msrp")),
		"suggested_price": dropship_hooks.suggested_price(dist, row, listing),
		"qty": int(row.get("qty") or 0),
		"restricted_states": blocked_states,
		"fulfillment_mode": fulfillment_mode,
		"fulfillment_reason": fulfillment_reason,
		"dropship_blocked": bool(row.get("dropship_blocked")),
		"vendor_approval_required": bool(row.get("vendor_approval_required")),
		"ffl_required": bool(row.get("ffl_required")),
		"is_firearm": legal_firearm(row, provider.code),
		"manufacturer": row.get("manufacturer") or "",
		"model": row.get("model") or "",
		"department": row.get("department") or "",
	}


@frappe.whitelist()
def ensure_dropship_item(sku: str, distributor: str | None = None, claim=1) -> dict:
	"""Counter Create Order seam: make sure this catalog SKU has a local Item that
	can carry a drop-ship line — dual-source guard → idempotent promote →
	``dropship_distributor`` stamp — WITHOUT a web listing (that stays Flow B's
	``create_listing``). Closes the gap where an un-promoted SKU produced a
	counter line with no item_code that ``_validate_items`` then refused.

	A WRITE (Item insert + stamp), so unlike search/quote it is role-gated — to
	the same roles that may ring the counter order itself (manual_order._ROLES).
	An Item already stamped by ANOTHER distributor is returned as it is, never
	re-homed: the counter line orders from the house it names, while the stamp
	keeps driving the web listing, web-order routing and stock publishing.

	Args:
	    sku: Distributor SKU (required).
	    distributor: Optional Distributor name; defaults to the resolved
	        primary.
	    claim: 1 (default) stamps the Item's ``dropship_distributor`` for this
	        house when it has none and no listing holds it (a listed
	        product's stamp is its listing's); 0 only promotes it — a ship-to-store line
	        buys the goods into our own stock and says nothing about who
	        drop-ships the Item on the web.

	Returns:
	    {item_code, item_name, is_firearm, claimed_by} — claimed_by is the
	    house the Item's drop-ship stamp names after the call (empty when
	    nothing claims it).

	Permissions:
	    Counter roles (Sales User / Sales Manager / Stock Manager / System
	    Manager).
	"""
	from ffl_integrations.data_service import CATALOG, require

	frappe.only_for(COUNTER_ROLES)
	require(CATALOG)
	sku = (str(sku or "")).strip()
	if not sku:
		frappe.throw(frappe._("sku is required."))
	dist_name = _distributor_name(distributor)
	if not dist_name:
		frappe.throw(frappe._(
			"No distributor is configured — enable one, or set Item.dropship_distributor."))
	provider = get_provider(frappe.get_cached_doc("Distributor", dist_name).provider)
	row = provider.catalog_row(sku)
	if not row:
		frappe.throw(frappe._("SKU {0} is not in {1}'s catalog.").format(sku, dist_name))
	# Function-local: items imports api at module scope.
	from ffl_integrations.distributor.items import ensure_item_for_dropship
	from ffl_core.firearm import is_firearm_item

	# Multi-distributor safety: `_stamp_item` OVERWRITES the stamp, so the Item the
	# promoter resolves this row to (UPC first, else the stock number) is used as it
	# is when another house already sources it — never re-homed. The counter line
	# carries its own house (distributor-settings-layout E); the stamp stays the web
	# channel's.
	item_code = (row.get("upc") or "").strip() or sku
	owner = frappe.db.get_value("Item", item_code, "dropship_distributor")
	# A listed product's stamp is its listing's (automatic sourcing moves it with the
	# listing, and a pinned-off listing has none): the counter never writes it.
	claim = bool(cint(claim)) and not offers.listing_for(item_code, None, any_house=True)
	if not owner or owner == dist_name:
		item_code = ensure_item_for_dropship(dist_name, sku, provider=provider, row=row,
			claim=claim)
		owner = dist_name if claim else (owner or "")
	return {
		"item_code": item_code,
		"item_name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
		"is_firearm": bool(is_firearm_item(item_code)),
		"claimed_by": owner,
	}


@frappe.whitelist()
def fulfillment_matrix(lines) -> dict:
	"""One-shot batch quote for the Create Order Fulfillment planner.

	Every line × (local stock + each
	enabled distributor). Open read (like search/quote). Column-level
	degradation — one failing house only greys out its own column; the
	local column and the rest render normally.

	Args:
	    lines: [{"item_code": str?, "sku": str?, "upc": str?, "qty": int}] —
	        list or JSON string. A line with only ``upc`` (an "API" pick
	        whose house is not chosen yet) resolves per column by UPC.

	Returns:
	    {"columns": [distributor, ...], "rows": [{item_code, sku, qty,
	    local: {in_stock, feasible}, is_firearm, channels: [cell, ...], preselect,
	    preselect_mode}]} — is_firearm is what the server enforces for the
	    line's own Item (None without one); cell = {distributor, sku, cost,
	    store_cost (the per-unit cost shipped to the store: full cases at the
	    house's case price), case_note? (the case-price hint), qty, department,
	    mode: "direct"|"to_store" (the house's default), modes: {direct,
	    to_store: {feasible, reason}}, ffl_required, feasible, reason?} or
	    {distributor, error: True}.

	Permissions:
	    Open by design — any authenticated
	    desk user; see `search`.
	"""
	from ffl_core.api.item_admin import _get_item_stock as get_item_stock
	from ffl_core.firearm import is_firearm_item

	if isinstance(lines, str):
		lines = frappe.parse_json(lines)
	dists = frappe.get_all("Distributor", filters={"enabled": 1},
		fields=["name", "provider", "dropship_enabled", "stocking_enabled"],
		order_by="priority asc, modified asc")
	providers = {}
	for d in dists:
		try:
			providers[d.name] = get_provider(d.provider)
		except Exception:  # noqa: BLE001 — a house with no registered provider degrades to an error column
			providers[d.name] = None
	item_codes = [(l.get("item_code") or "").strip()
		for l in (lines or []) if (l.get("item_code") or "").strip()]
	stock = get_item_stock(item_codes) if item_codes else {}
	rows = []
	for l in (lines or []):
		qty = max(1, int(l.get("qty") or 1))
		item_code = (l.get("item_code") or "").strip() or None
		in_stock = float(stock.get(item_code) or 0) if item_code else 0.0
		row = {
			"item_code": item_code, "sku": (l.get("sku") or "").strip() or None,
			"upc": (l.get("upc") or "").strip() or None,
			"qty": qty,
			"local": {"in_stock": in_stock, "feasible": in_stock >= qty},
			"is_firearm": (is_firearm_item(item_code)
				if item_code and frappe.db.exists("Item", item_code) else None),
			"channels": [],
		}
		for d in dists:
			try:
				row["channels"].append(_matrix_cell(providers.get(d.name), d.name, l, qty,
					dropship_enabled=bool(d.dropship_enabled),
					stocking_enabled=bool(d.stocking_enabled)))
			except Exception:  # noqa: BLE001 — degrade per column: one house must never kill the dialog
				row["channels"].append({"distributor": d.name, "error": True})
		row["preselect"] = _matrix_preselect(row)
		row["preselect_mode"] = _matrix_preselect_mode(row)
		rows.append(row)
	return {"columns": [d.name for d in dists], "rows": rows}


def _matrix_cell(provider, dist_name: str, line: dict, qty: int, *,
		dropship_enabled: bool = True, stocking_enabled: bool = True) -> dict:
	"""One row × one house cell. The sku resolution chain is the same as quote: explicit sku → that house's listing
	sku → sku-keyed catalog → UPC fallback; the mode resolves through the same chain (materialised
	listing mode → brand policy → raw flags). A house with drop-ship switched off
	offers ship to store only — create_order refuses its drop-ship lines."""
	if provider is None:
		return {"distributor": dist_name, "error": True}
	sku = (line.get("sku") or "").strip() or None
	item_code = (line.get("item_code") or "").strip() or None
	listing = offers.listing_for(item_code, dist_name, fields=("sku", "fulfillment_mode"))
	sku = sku or (listing.get("sku") if listing else None)
	row = offers.catalog_row(provider, sku=sku, item_code=item_code,
		upc=(line.get("upc") or "").strip() or None)
	if not row:
		return {"distributor": dist_name, "feasible": False, "reason": "not carried"}

	from ffl_integrations.distributor import policy as _policy
	from ffl_integrations.distributor import router as _router
	listing_mode = listing.get("fulfillment_mode") if listing else None
	resolved = _router.accessory_mode(
		listing_mode,
		brand_policy=_policy.brand_policy_for(row.get("manufacturer"), dist_name),
		dropship_blocked=bool(row.get("dropship_blocked")),
		vendor_approval_required=bool(row.get("vendor_approval_required")))
	# P2: every carried cell offers BOTH ways to fulfil — the operator picks per
	# line. Ship to store only needs stock; drop-ship also needs the house to
	# drop-ship and the resolved mode to be Direct, and when either isn't, the
	# reason says why in the clerk's words.
	# Which lanes this house can run at all — a house that cannot drop-ship (or
	# cannot ship to the store) offers no button for it, rather than a greyed-out one
	# it could never honour.
	caps = provider.capabilities()
	firearm_line = legal_firearm(row, provider.code)
	offers_dropship = ("fds" if firearm_line else "dropship") in caps
	offers_to_store = "to_store" in caps
	if not dropship_enabled:
		direct_block = "drop-ship is switched off for this distributor — ship to store only"
	elif dealer_only_part(row, provider.code):
		# The counter has no refused-order fallback (only the web router re-orders a
		# refused part to the store): never plan a buyer's-door order the
		# house may refuse.
		direct_block = "this distributor ships it only to a dealer — ship to store"
	elif resolved == "Direct":
		direct_block = ""
	else:
		direct_block = (_policy.brand_policy_reason(row.get("manufacturer"), dist_name)
			or ("distributor block — ship to store only" if row.get("dropship_blocked")
			else "vendor approval required — ship to store only"))
	cell = {
		"distributor": dist_name,
		"sku": row.get("sku"),
		"cost": flt(row.get("cost")),
		"qty": int(row.get("qty") or 0),
		"ffl_required": bool(row.get("ffl_required")),
		"is_firearm": firearm_line,
		# The freight estimator's lane input (handgun departments ship air).
		"department": row.get("department") or "",
	}
	short = "" if cell["qty"] >= qty else "insufficient distributor stock"
	store_block = "" if stocking_enabled else (
		"ship to store is switched off for this distributor")
	cell["modes"] = {}
	if offers_dropship:
		cell["modes"]["direct"] = {"feasible": not (direct_block or short),
			"reason": direct_block or short}
	if offers_to_store:
		cell["modes"]["to_store"] = {"feasible": not (store_block or short),
			"reason": store_block or short}
	# Shipped to the shop, full cases buy at the house's case price (RSR); a
	# drop-ship line always pays the unit cost.
	priced = case_pricing(qty, cell["cost"], row.get("case_cost"), row.get("units_per_case")) \
		if cell["modes"].get("to_store", {}).get("feasible") else None
	cell["store_cost"] = priced["unit_cost"] if priced else cell["cost"]
	if priced:
		cell["case_note"] = case_note(priced)
	cell["feasible"] = any(m["feasible"] for m in cell["modes"].values())
	# The house's default for this line: drop-ship when it can, else ship to store.
	cell["mode"] = ("direct" if cell["modes"].get("direct", {}).get("feasible")
		else ("to_store" if cell["modes"].get("to_store", {}).get("feasible") else None))
	if not cell["feasible"]:
		cell["reason"] = short or direct_block or store_block or (
			"this distributor cannot fill the line")
	elif not cell["modes"].get("direct", {}).get("feasible"):
		cell["reason"] = direct_block or store_block
	return cell


def _matrix_preselect(row: dict):
	"""Rule chain: enough local stock → local; otherwise the cheapest feasible cell (drop-ship or to-store),
	ties go to the earlier column (= Distributor priority ascending, adjustable on the hub page); nothing feasible → None.
	The chosen house's mode: see :func:`_matrix_preselect_mode`."""
	if row["local"]["feasible"]:
		return "local"
	best = None
	for idx, cell in enumerate(row["channels"]):
		if not cell.get("feasible"):
			continue
		cost = cell.get("store_cost") if cell.get("mode") == "to_store" else cell.get("cost")
		key = (flt(cost), idx)
		if best is None or key < best[0]:
			best = (key, cell["distributor"])
	return best[1] if best else None


def _matrix_preselect_mode(row: dict):
	"""The preselected house's mode: drop-ship when it can, else ship to store."""
	cell = next((c for c in row["channels"] if c.get("distributor") == row.get("preselect")), None)
	return cell.get("mode") if cell else None


@frappe.whitelist()
def fulfillment_freight(lines, options=None) -> dict:
	"""Estimated distributor freight for the Fulfillment planner's CURRENT choice.

	The planner re-asks on every selection change. Lines are grouped exactly as
	the counter hook will order them — by (distributor, lane) and then the
	provider's own split (firearms and accessories never share an order) — and
	each resulting order is priced by the provider's freight rules, so a mixed
	drop-ship + ship-to-store cart is the sum of its real orders. Nothing is
	read from the catalog: the matrix already sent cost / department /
	ffl_required, and this is a display estimate (the invoiced freight wins).

	Args:
	    lines: [{distributor, mode: "direct"|"to_store", sku, item_code?, qty,
	        cost, department, ffl_required}] — list or JSON string; local
	        lines are simply not sent.
	    options: Optional {options_key: {signature, adult_signature, insurance}}
	        — the delivery options picked per order (list or JSON string);
	        an order without an entry uses its house's defaults.

	Returns:
	    {"orders": [{distributor, mode, order_type, line_count, merchandise,
	    freight, firearm, options_key, options}], "total": float, "complete":
	    bool} — freight is None (and complete False) for a house that can't
	    estimate; options is None for a house with no delivery options to choose.

	Permissions:
	    Counter roles — the planner belongs to Create Order.
	"""
	frappe.only_for(COUNTER_ROLES)
	if isinstance(lines, str):
		lines = frappe.parse_json(lines)
	if isinstance(options, str):
		options = frappe.parse_json(options)
	return freight.estimate_orders(lines, options)


@frappe.whitelist()
def create_orders(distributor: str, lines, ship_context=None) -> list:
	"""Plan + create the Draft Distributor Order(s) for a cart.

	`plan_fulfillment` may split one cart into several orders (no
	firearm/accessory mixing / H4 >400) — each becomes its own Draft DO. A
	classification divergence raises (order held for a human). Never
	places anything: v1 is a human-confirmed queue.

	Args:
	    distributor: Distributor name.
	    lines: Cart lines [{sku/item_code, qty, ...}] — list or JSON
	        string.
	    ship_context: Optional {mode: "stocking"|"to_store"|"direct"|"fds",
	        ...} — the lane to order on (see distributor/lanes.py); defaults
	        to stocking.

	Returns:
	    [{name, order_type, account, line_count}] — one per created Draft
	    DO.

	Permissions:
	    Distributor order roles.
	"""
	frappe.only_for(ORDER_ROLES)
	if isinstance(lines, str):
		lines = frappe.parse_json(lines)
	if isinstance(ship_context, str):
		ship_context = frappe.parse_json(ship_context)
	return _create_orders(distributor, lines, ship_context)


def _create_orders(distributor: str, lines, ship_context=None) -> list:
	"""Gate-free body of ``create_orders`` — the counter's Re-source action runs
	it under its own (counter-role) gate, the way the counter hook already
	creates orders without Purchase roles."""
	ctx = ship_context or {"mode": lanes.DEFAULT_MODE}
	block = lane_block(distributor, ctx.get("mode") or lanes.DEFAULT_MODE,
		is_firearm=any(ln.get("is_firearm") if "is_firearm" in ln else legal_firearm(ln,
			frappe.get_cached_value("Distributor", distributor, "provider")) for ln in (lines or [])))
	if block:
		frappe.throw(frappe._("Can't order from {0}: {1}.").format(distributor, block))

	dist = frappe.get_doc("Distributor", distributor)
	provider = get_provider(dist.provider)
	from ffl_integrations.distributor.items import catalog_item

	enriched = [enrich_line(provider, ln) for ln in (lines or [])]
	for ln, raw in zip(enriched, lines or []):
		if not ln["item_code"]:
			# No local Item yet: make it from the house's catalog, as invoice receiving
			# does — an order line without an Item could never be received against.
			ln["item_code"] = catalog_item(provider, ln["sku"], raw.get("upc") or "")[0] or ""
	try:
		plans = provider.plan_fulfillment(enriched, ctx)
	except PlanningError as exc:
		frappe.throw(frappe._(str(exc)))

	enriched_by_sku = {ln["sku"]: ln for ln in enriched}
	created = []
	for plan in plans:
		do = create_do_from_plan(dist, plan, enriched_by_sku)
		created.append({"name": do.name, "order_type": do.order_type, "account": do.account,
			"line_count": len(do.lines)})
	return created


def enrich_line(provider, line: dict) -> dict:
	"""Attach the firearm-classification flags + catalog economics the split rules
	need: ``legal_firearm`` (the catalog service's class — what the law says) is
	authoritative for the lane; our ``is_firearm`` is the cross-check; the
	house's own ``ffl_required`` rides along for the order it places."""
	sku = line.get("sku")
	row = provider.catalog_row(sku) or {}
	item_code = line.get("item_code") or ""
	if not item_code:
		# A cart line from the cross-house search carries no Item: use the one the
		# shop already has for this product (``_create_orders`` makes one if none).
		from ffl_integrations.distributor.items import local_item
		item_code = local_item(sku, row.get("upc") or line.get("upc") or "") or ""
	our_is_firearm = None
	if item_code:
		our_is_firearm = frappe.db.get_value("Item", item_code, "is_firearm")
	is_legal = legal_firearm(row, provider.code)
	is_firearm = bool(our_is_firearm) if our_is_firearm is not None else is_legal
	return {
		"sku": sku,
		"item_code": item_code,
		"qty": int(line.get("qty") or line.get("wish_qty") or 0),
		"ffl_required": bool(row.get("ffl_required")),
		"legal_firearm": is_legal,
		"is_firearm": is_firearm,
		"department": row.get("department") or "",
		"unit_cost": row.get("cost") or 0,
		# The case price, for a line that ships to the store (create_do_from_plan).
		"case_cost": row.get("case_cost") or 0,
		"units_per_case": row.get("units_per_case") or 0,
		"description": line.get("description") or row.get("model") or "",
	}


DELIVERY_OPTIONS = ("signature", "adult_signature", "insurance")


def delivery_options_key(distributor: str, order_type: str, firearm: bool) -> str:
	"""Which order a set of delivery options belongs to — the house, its order type
	and whether it is the firearm half of a split cart — the same on the planner's
	freight answer and on the counter hook that creates the order."""
	return f"{distributor}|{order_type}|{1 if firearm else 0}"


def create_do_from_plan(dist, plan: dict, enriched_by_sku: dict, *,
		source=None, lane=None, delivery_options=None):
	"""Build one Draft Distributor Order from a provider plan.

	``source`` is the ``(doctype, name)`` Dynamic Link back to whatever caused the
	order — a Woo Online Order for the web flow, a Sales Invoice for the
	counter one. ``lane`` stamps every line's ``fulfillment`` so the completion
	path knows which units it may dispose locally and which are the distributor's
	(H1).
	"""
	do = frappe.new_doc("Distributor Order")
	do.distributor = dist.name
	do.order_type = plan["order_type"]
	do.account = plan["account"]
	# What the house calls that account — shown on the order; the lane key stays
	# the machine's business.
	try:
		do.account_number = get_provider(dist.provider).account_number(plan["account"])
	except Exception:  # noqa: BLE001 — a display stamp never blocks an order
		do.account_number = ""
	do.fill_or_kill = 1 if plan.get("fill_or_kill") else 0
	do.status = order_state.DRAFT
	# Pin the booking company at creation so the cost PI posts to a
	# definite company rather than re-deriving it later.
	from ffl_integrations.distributor.accounting import default_company
	do.company = default_company()
	if source:
		do.source_doctype, do.source_name = source
	# Destination, per the plan's mode. Only the fields that mode populates are
	# written, so an FDS order never carries a consumer street address.
	ship_to = plan.get("ship_to") or {}
	if ship_to:
		do.ship_address_line1 = ship_to.get("line1") or ""
		do.ship_address_line2 = ship_to.get("line2") or ""
		do.ship_address_city = ship_to.get("city") or ""
		do.ship_address_state = ship_to.get("state") or ""
		do.ship_address_zip = ship_to.get("zip") or ""
	dealer = plan.get("transfer_dealer") or {}
	if dealer:
		do.transfer_ffl_license = dealer.get("license") or ""
		do.transfer_ffl_premise_street = dealer.get("premise_street") or ""
		do.transfer_ffl_premise_city = dealer.get("premise_city") or ""
		do.transfer_ffl_premise_state = dealer.get("premise_state") or ""
		do.transfer_ffl_premise_zip = dealer.get("premise_zip") or ""
		if dealer.get("ffl_record"):
			do.transfer_ffl_record = dealer["ffl_record"]
	if plan.get("consumer_name"):
		do.consumer_name = plan["consumer_name"]
	if plan.get("contact_num"):
		do.contact_num = plan["contact_num"]
	do.shipping_service = plan.get("shipping_service") or ""
	do.shipping_service_label = plan.get("shipping_service_label") or ""
	if delivery_options is not None:
		# Picked in Create Order: sent as they are instead of the house's settings.
		do.delivery_options_set = 1
		do.delivery_signature = 1 if delivery_options.get("signature") else 0
		do.delivery_adult_signature = 1 if delivery_options.get("adult_signature") else 0
		do.delivery_insurance = 1 if delivery_options.get("insurance") else 0
	to_store = lanes.stocks_goods(plan["order_type"])
	for pln in plan["lines"]:
		src = enriched_by_sku.get(pln["sku"], {})
		# An estimate either way: the house's acknowledged Cost replaces it on
		# placement, and its invoice price replaces that.
		priced = case_pricing(pln["qty"], src.get("unit_cost"), src.get("case_cost"),
			src.get("units_per_case")) if to_store else None
		do.append("lines", {
			"sku": pln["sku"],
			"item_code": pln.get("item_code") or src.get("item_code"),
			"description": src.get("description"),
			"wish_qty": pln["qty"],
			"unit_cost": priced["unit_cost"] if priced else (src.get("unit_cost") or 0),
			"case_note": case_note(priced),
			"ship_method": pln["ship_method"],
			"line_status": "Pending",
			"fulfillment": lane or "",
		})
	do.insert()  # the controller's before_insert fills the provider's default service
	return do


def _shipping_options_for(do) -> list:
	"""``[{code, label, estimate, default}]`` from the provider's OPTIONAL
	``shipping_options(do)`` hook; [] when the provider has none."""
	provider = get_provider(frappe.get_cached_doc("Distributor", do.distributor).provider)
	fn = getattr(provider, "shipping_options", None)
	if not callable(fn):
		return []
	try:
		return list(fn(do) or [])
	except Exception:  # noqa: BLE001 — a settings gap must not block DO creation
		frappe.log_error(title=f"shipping_options failed for {do.name or 'new DO'}",
			message=frappe.get_traceback())
		return []


@frappe.whitelist()
def shipping_options(do_name: str) -> list:
	"""Carrier services the distributor offers for this order, with the
	flat-rate estimate, so the operator can choose before Confirm.

	Args:
	    do_name: Distributor Order name.

	Returns:
	    [{code, label, estimate, default, selected}] — empty when the
	    provider decides shipping itself.

	Permissions:
	    Distributor order/console roles.
	"""
	frappe.only_for(ORDER_ROLES_WITH_CONSOLE)
	do = frappe.get_doc("Distributor Order", do_name)
	out = []
	for o in _shipping_options_for(do):
		o = dict(o)
		o["selected"] = (o.get("code") or "") == (do.shipping_service or "")
		out.append(o)
	return out


@frappe.whitelist()
def set_shipping_service(do_name: str, code: str) -> dict:
	"""Choose the carrier service for a Draft Distributor Order.

	Only a Draft order may change (Queued and later are at or past the
	distributor); the code must be one the provider offers for this order.

	Args:
	    do_name: Distributor Order name.
	    code: Service code from ``shipping_options`` ('' = the provider's
	        empty-code service, e.g. Sports South Ground Economy).

	Returns:
	    {"name", "shipping_service", "shipping_service_label"}.

	Permissions:
	    Distributor order/console roles.
	"""
	frappe.only_for(ORDER_ROLES_WITH_CONSOLE)
	do = frappe.get_doc("Distributor Order", do_name)
	if do.status != order_state.DRAFT:
		frappe.throw(frappe._("Shipping can only be changed on a Draft order ({0} is {1}).")
			.format(do_name, do.status))
	code = "" if code is None else str(code).strip()
	match = next((o for o in _shipping_options_for(do) if (o.get("code") or "") == code), None)
	if match is None:
		frappe.throw(frappe._("{0} is not a shipping service the distributor offers for this order.")
			.format(code or frappe._("(blank)")))
	do.shipping_service = code
	do.shipping_service_label = match["label"]
	do.save()
	return {"name": do.name, "shipping_service": code,
		"shipping_service_label": match["label"]}


def leg_needs_full_payment(order_type: str | None) -> bool:
	"""Whether a counter order must be settled before THIS kind of distributor leg
	is placed. A drop-ship leg leaves for the buyer or their FFL and RSR has no
	cancel API, so it waits for full payment. A ship-to-store leg does not: the
	goods land in our own stock, so an order the customer never settles is shelf
	inventory, not a parcel gone to a stranger — the
	hand-over itself still requires the balance (api/counter_pickup.pickup_block).
	Shared with ffl_core's queue row (``releasable`` / blocked hint) so the
	Confirm gate and the button can never disagree."""
	return not lanes.stocks_goods(order_type or "")


def placement_payment_block(do) -> str | None:
	"""The counter paid-gate, as the throwable message or None: a Sales-Invoice-
	sourced Distributor Order that needs full payment (leg_needs_full_payment)
	may only leave Draft once its invoice is settled, so a deposit drop-ship order
	sits in the queue until the balance is recorded (Pending Order → Record
	Payment). A web (Woo) order has its own paid gate at route time, so this is a
	no-op for it."""
	if do.get("source_doctype") != "Sales Invoice" or not do.get("source_name"):
		return None
	if not leg_needs_full_payment(do.get("order_type")):
		return None
	outstanding = frappe.db.get_value(
		"Sales Invoice", do.get("source_name"), "outstanding_amount")
	if flt(outstanding) <= 0.01:
		return None
	return frappe._(
		"Counter order {0} is not paid in full — record the balance from Pending "
		"Order before placing it with the distributor.").format(do.get("source_name"))


@frappe.whitelist()
def confirm_order(do_name: str) -> dict:
	"""Confirm a Draft Distributor Order: Draft → Queued + enqueue placement.

	Human confirmation (the v1 queue gate); the placement worker is
	idempotent. A counter-sourced drop-ship DO must be fully paid first
	(placement_payment_block); a ship-to-store leg may be placed on a deposit.

	Args:
	    do_name: Distributor Order name.

	Returns:
	    {"name": ..., "status": "Queued"}.

	Permissions:
	    Distributor order/console roles.
	"""
	frappe.only_for(ORDER_ROLES_WITH_CONSOLE)
	do = frappe.get_doc("Distributor Order", do_name)
	block = placement_payment_block(do)
	if block:
		frappe.throw(block)
	do.status = order_state.QUEUED  # controller.validate enforces legality
	do.save()
	enqueue_distributor(
		"ffl_integrations.distributor.tasks.place_order_job",
		job_name=f"dist_place_{do.name}", do_name=do.name)
	return {"name": do.name, "status": do.status}


@frappe.whitelist()
def cancel_order(do_name: str, reason: str | None = None, confirmed=False) -> dict:
	"""Cancel a Distributor Order here (alerts ops when it is still live at the house).

	Cancelling never reaches the distributor, so an order that is live there
	(Placed+) has to be cancelled with the house by phone or email.
	Left unconfirmed, ops are alerted to arrange that; ``confirmed`` records
	that the house has already agreed and skips the alert.

	Args:
	    do_name: Distributor Order name.
	    reason: Optional reason, stored as hold_reason.
	    confirmed: The distributor has confirmed the cancellation (phone /
	        email); no ops alert, and a standard reason when none is given.

	Returns:
	    {"name": ..., "status": "Cancelled"}.

	Permissions:
	    Distributor order/console roles.
	"""
	frappe.only_for(ORDER_ROLES_WITH_CONSOLE)
	do = frappe.get_doc("Distributor Order", do_name)
	confirmed = bool(cint(confirmed))
	if confirmed and not reason:
		reason = frappe._("Distributor confirmed the cancellation")
	if do.status in order_state.POLLABLE_STATES and not confirmed:
		from ffl_integrations.notifications import notify_admins
		notify_admins(
			f"Distributor Order cancelled after placement: {do.name}",
			f"{do.name} was Cancelled while live at {do.distributor} ({do.status}). "
			f"Cancelling here does not reach the house — arrange the cancellation/return "
			f"with them directly.",
			dedup_key=f"dist_cancel_{do.name}")
	do.status = order_state.CANCELLED
	if reason:
		do.hold_reason = reason
	do.save()
	return {"name": do.name, "status": do.status}


@frappe.whitelist()
def transition_order(do_name: str, to_status: str) -> dict:
	"""Generic ops state transition for a Distributor Order.

	The DO state machine still enforces transition legality.

	Args:
	    do_name: Distributor Order name.
	    to_status: Target status.

	Returns:
	    {"name": ..., "status": <new status>}.

	Permissions:
	    Distributor order roles.
	"""
	frappe.only_for(ORDER_ROLES)
	do = frappe.get_doc("Distributor Order", do_name)
	do.status = to_status
	do.save()
	return {"name": do.name, "status": do.status}


@frappe.whitelist()
def list_orders(status: str | None = None, distributor: str | None = None, limit: int = 50,
		open_only=0, order_type: str | None = None, with_lines=0) -> list:
	"""List Distributor Orders, newest-modified first — every house and every
	order type in one list (the Distributor Orders page's tracking tab).

	Args:
	    status: Optional exact status filter.
	    distributor: Optional Distributor filter.
	    limit: Row cap (default 50, max 500).
	    open_only: 1 = leave out Complete and Cancelled orders.
	    order_type: Optional exact order type (Stocking, Dropship Firearm, ...).
	    with_lines: 1 = add each order's ``lines`` [{sku, description, wish_qty,
	        invoiced_qty, received_qty, line_status, tracking_numbers, invoices}].

	Returns:
	    [{name, distributor, order_type, account, account_number, status,
	    hold_status, web_ref, total_cost, placed_at, next_poll_at, receive_status,
	    source_doctype, source_name, creation, last_poll_at, invoiced_freight,
	    error}] — plus ``lines`` when asked.
	"""
	frappe.only_for(ORDER_ROLES)
	filters = {}
	if status:
		filters["status"] = status
	elif cint(open_only):
		filters["status"] = ["not in", sorted(order_state.TERMINAL_STATES)]
	if distributor:
		filters["distributor"] = distributor
	if order_type:
		filters["order_type"] = order_type
	orders = frappe.get_all(
		"Distributor Order", filters=filters,
		fields=["name", "distributor", "order_type", "account", "account_number", "status",
			"hold_status", "web_ref", "total_cost", "placed_at", "next_poll_at", "receive_status",
			"source_doctype", "source_name", "creation", "last_poll_at", "invoiced_freight",
			"error"],
		order_by="modified desc", limit=min(max(1, cint(limit) or 50), 500))
	if cint(with_lines) and orders:
		by_parent: dict = {}
		for ln in frappe.get_all("Distributor Order Line",
				filters={"parent": ["in", [o.name for o in orders]],
					"parenttype": "Distributor Order"},
				fields=["parent", "sku", "description", "wish_qty", "invoiced_qty",
					"received_qty", "line_status", "tracking_numbers", "invoices"],
				order_by="parent asc, idx asc"):
			by_parent.setdefault(ln.pop("parent"), []).append(ln)
		for o in orders:
			o["lines"] = by_parent.get(o.name, [])
	return orders
