"""Remote catalog transport — the ONLY module that knows /v1/catalog wire
shapes. Providers call these functions; every call first asks ``_backend()``
which catalog this request reads:

- tests: the in-memory stub (tests/catalog_stub.py); reaching the network
  without one raises CatalogStubMissing so an unmigrated test fails with
  instructions;
- ``Data Service Settings.catalog_source`` = Local: ``LocalCatalog``, this
  site's own tables (ffl_integrations/catalog_local) — then no catalog call
  reaches ``data_service/client.py``;
- the catalog capability is off FOR THIS READ (no Local catalog, and no service
  the read would go to: the house's own for a named house, else Data Service
  Settings): ``CapabilityOff`` (``data_service.require``), quietly — callers that
  degrade when the service is down degrade the same way, and nothing is sent;
- otherwise None: the Data Service over HTTP, as before."""
from __future__ import annotations

from urllib.parse import quote

import frappe

from ffl_integrations.data_service import client
from ffl_integrations.data_service.client import (  # noqa: F401 — re-export
	DataServiceForbidden,
	DataServiceUnavailable,
)

SEARCH_TIMEOUT_SECONDS = 3
ROWS_BATCH_MAX = 200
CACHE_TTL_SECONDS = 60

_STUB_ATTR = "dist_catalog_stub"
# Test-only opt-in: the capability model's own tests want the real require(CATALOG).
REAL_CAPABILITY_FLAG = "catalog_real_capability_in_test"


class CatalogStubMissing(RuntimeError):
	pass


def _backend(house: str | None = None):
	"""The catalog backend for this call: the test stub, the local catalog, or
	None (HTTP); ``CapabilityOff`` when the read cannot be served. The one choke
	point every catalog read goes through (``catalog.py`` and
	``catalog_master.py`` included). ``house``: the house code a named-house read
	sends (``client``'s ``distributor=``) — None for a cross-house read, which only
	Data Service Settings serves (``data_service.capability``).

	Tests keep the contract extensions rely on: no stub and not Local means
	``CatalogStubMissing`` — never ``CapabilityOff``, whatever the test site's
	Data Service settings say. A test of the capability model itself sets
	``frappe.flags.catalog_real_capability_in_test`` to get the real answer."""
	in_test = getattr(frappe.flags, "in_test", False)
	if in_test:
		stub = getattr(frappe.flags, _STUB_ATTR, None)
		if stub is not None:
			return stub
	from ffl_integrations.data_service import CATALOG, LOCAL, capability, require

	if in_test and not getattr(frappe.flags, REAL_CAPABILITY_FLAG, False):
		if capability(CATALOG) != LOCAL:
			raise CatalogStubMissing(
				"a test reached the remote catalog without a stub — call "
				"ffl_integrations.tests.catalog_stub.install() and seed_catalog() first")
		value = LOCAL
	else:
		value = require(CATALOG, notify=False, house=house or None)
	if value == LOCAL:
		from ffl_integrations.catalog_local.backend import LocalCatalog

		return LocalCatalog()
	return None


# The name extensions already call (osa_dropship listing_price reads raw
# products through it): kept so they follow the same backend.
_stub = _backend


def _memo() -> dict:
	try:
		memo = getattr(frappe.local, "_remote_catalog_memo", None)
		if memo is None:
			memo = {}
			frappe.local._remote_catalog_memo = memo
		return memo
	except Exception:  # noqa: BLE001 — no request context
		return {}


def rows(distributor: str, skus: list) -> dict:
	backend = _backend(distributor)
	if backend is not None:
		return backend.rows(distributor, skus)
	out: dict = {}
	for start in range(0, len(skus), ROWS_BATCH_MAX):
		batch = list(skus)[start:start + ROWS_BATCH_MAX]
		data = client.post("/v1/catalog/rows", {"distributor": distributor, "skus": batch},
			distributor=distributor)
		for r in data.get("rows") or []:
			out[r["sku"]] = r
	return out


def prefetch(distributor: str, skus: list) -> None:
	fetched = rows(distributor, skus)
	memo = _memo()
	for sku in skus:
		memo[(distributor, sku)] = fetched.get(sku)


def cached_row(distributor: str, sku: str):
	# _backend() first, always — frappe.local's memo outlives uninstall(), so a
	# memo lookup done BEFORE checking for a stub can silently return a stale
	# cached value instead of raising CatalogStubMissing.
	backend = _backend(distributor)
	memo = _memo()
	key = (distributor, sku)
	if key in memo:
		return memo[key]
	if backend is None:  # Redis only in front of HTTP; the local tables are the cache
		cache_key = f"rc_row:{distributor}:{sku}"
		hit = frappe.cache().get_value(cache_key)
		if hit is not None:
			row = None if hit == "__miss__" else hit
			memo[key] = row
			return row
	row = rows(distributor, [sku]).get(sku)
	memo[key] = row
	if backend is None:
		frappe.cache().set_value(
			f"rc_row:{distributor}:{sku}", row if row is not None else "__miss__",
			expires_in_sec=CACHE_TTL_SECONDS)
	return row


def search(distributor: str | None, q: str, limit: int = 10) -> list:
	"""Catalog typeahead → ``[ProductHit]`` (one per UPC, ``offers[]`` per house,
	``best_offer``). ``distributor`` scopes the service-side search to one
	house; ``None`` searches every house the service carries — the counter's
	unnamed search wants exactly that: one row per product, sourcing decided
	in the planner."""
	backend = _backend(distributor)
	if backend is not None:
		return backend.search(distributor, q, limit)
	body = {"q": q, "limit": limit}
	if distributor:
		body["distributor"] = distributor
	# scoped → that house's service; unscoped → the site-wide service
	data = client.post("/v1/catalog/search", body, timeout=SEARCH_TIMEOUT_SECONDS,
		distributor=distributor)
	return data.get("results") or []


def classify(offers) -> None:
	"""A search hit's offers are summaries without ``product_class``: give the
	flagged ones theirs from the full rows (one batched read a house) so
	``legal_firearm`` sees the class, in place. An unreachable service leaves them
	classless — ``legal_firearm`` then keeps a flagged row a firearm."""
	need: dict = {}
	for o in offers or []:
		if o.get("ffl_required") and "product_class" not in o and o.get("sku") and o.get("distributor"):
			need.setdefault(o["distributor"], []).append(o)
	for distributor, group in need.items():
		try:
			full = rows(distributor, [o["sku"] for o in group])
		except DataServiceUnavailable:
			continue
		for o in group:
			o["product_class"] = (full.get(o["sku"]) or {}).get("product_class") or ""


def health(distributor: str) -> dict:
	backend = _backend(distributor)
	if backend is not None:
		return backend.health(distributor)
	cache_key = f"rc_health:{distributor}"
	hit = frappe.cache().get_value(cache_key)
	if hit:
		return hit
	data = client.get("/v1/catalog/health", {"distributor": distributor}, distributor=distributor)
	frappe.cache().set_value(cache_key, data, expires_in_sec=CACHE_TTL_SECONDS)
	return data


def item_by_upc(upc: str):
	if not upc:  # guards a literal "None"/empty URL segment on the real path too
		return None
	backend = _backend()
	if backend is not None:
		return backend.item_by_upc(upc)
	try:
		# Percent-encode the path segment (the atf provider's precedent) so the
		# signed path (signed_headers signs the path WITHOUT the query string)
		# equals the path actually sent for any UPC value — an unescaped "/" or
		# "%" would otherwise change the request path out from under the signature.
		return client.get(f"/v1/catalog/item/{quote(str(upc), safe='')}")
	except DataServiceUnavailable as exc:
		if "404" in str(exc):
			return None
		raise


def parse_service_ts(value):
	"""Service ISO timestamp (UTC) → naive datetime on the SITE's clock,
	comparable with ``frappe.utils.now_datetime()``.

	Comparability is the entire contract — every consumer subtracts the result
	from `now_datetime()` — and it used to be broken. The old body said
	``dt.astimezone().replace(tzinfo=None)``, and a bare `astimezone()` converts
	to the *host process's* zone, not the site's. On a developer laptop where the
	two agree that is invisible; on every container we deploy (OS on UTC, site on
	a US zone) it silently shifted every heartbeat by a full UTC offset in the
	direction that makes a dead feed look ALIVE. QA measured an age of -12244 s
	for a heartbeat that was really 2155 s old (QA BUG-1).

	That is not cosmetic here. This value feeds `tasks.feed_stale_verdict`, and
	through it the `*/15` zero-stock interlock whose job is "a feed outage must
	not keep selling stock we can no longer verify". Understating the age by 5 h
	against a 90-minute threshold meant the interlock engaged ~5.5 h late, and
	the shop kept publishing quantities from a feed that had stopped answering.

	Lives here because "how the service spells a timestamp on the wire" is a wire
	shape, and this module owns those. It is the only copy — which matters,
	because the two heartbeats it produces are read by three independent judges:
	the `*/15` watchdog and its interlock, `console._feed_stale_verdict` (the WP apply gate),
	and the Catalog Service page. One conversion, so they cannot disagree."""
	if not value:
		return None
	from datetime import UTC, datetime

	# The service spells UTC as "+00:00" today but the service's documented examples use the "Z"
	# form, so normalise instead of trusting one spelling. (This is NOT a version
	# shim — all three apps pin `requires-python = ">=3.14"`, where
	# `fromisoformat` already accepts "Z"; it is defence against the wire, which
	# we do not control.)
	text = str(value)
	if text.endswith(("Z", "z")):
		text = text[:-1] + "+00:00"
	dt = datetime.fromisoformat(text)
	if dt.tzinfo is None:
		# A naive value means UTC, not "already local": the service
		# speaks UTC, and reading it as site-local would move the instant by an
		# offset. Same ruling `catalog_service._parse_wire_utc` records — one
		# vocabulary for one wire.
		dt = dt.replace(tzinfo=UTC)
	return _on_the_sites_clock(dt)


def _on_the_sites_clock(moment):
	"""An aware instant → naive wall clock in the zone `now_datetime()` uses.

	Reuses `distributor.util.site_timezone` / `zone_info` — the same pair
	`console.py` and `catalog_service.py` read the site's zone through — rather
	than adding a fourth way to ask the question. Going through `ZoneInfo` (not a
	fixed offset) is what keeps this right across a DST boundary.
	"""
	from datetime import UTC, datetime, timedelta

	from ffl_integrations.distributor import util as dist_util

	name = dist_util.site_timezone()
	zone = dist_util.zone_info(name)
	if zone is not None:
		return moment.astimezone(zone).replace(tzinfo=None)
	# Unresolvable site zone (empty/typo'd System Settings, missing tzdata).
	# Falling back to UTC here would quietly restore the very skew we are
	# removing, because UTC *is* the host zone on every container we run. So
	# measure the offset against `now_datetime()` itself: agreeing with the clock
	# our callers compare against is what this function promises, and that stays
	# true however the site answers.
	from frappe.utils import now_datetime

	_warn_site_zone_unresolvable(name)
	skew = now_datetime() - datetime.now(UTC).replace(tzinfo=None)
	# Real zone offsets are whole minutes; the remainder is just the gap between
	# the two clock reads, so round it away and the result is exact.
	return (moment.astimezone(UTC).replace(tzinfo=None)
		+ timedelta(minutes=round(skew.total_seconds() / 60)))


_ZONE_WARNING_KEY = "remote_catalog_site_zone_unresolvable"
_ZONE_WARNING_TTL_SECONDS = 3600


def _warn_site_zone_unresolvable(name) -> None:
	"""Leave a trace when the fallback above is carrying the conversion.

	An unreadable `System Settings.time_zone` is a real configuration fault, and
	the fallback is good enough that nothing else about the site looks wrong —
	which is exactly why it has to say so. Silent correctness is how a config
	fault survives for months.

	Rate-limited to once an hour because the alternative is worse than silence:
	the `*/15` watchdog converts several heartbeats per tick and the Catalog
	Service page refreshes every 60s, so an un-throttled warning would bury the
	Error Log in copies of one standing fact and get read as noise.

	Reuses `notifications.safe_log_error` (contracted never to raise, falls back
	to the file logger when `frappe.log_error` itself blows up in a scheduler
	context) — observability must not become a new way for the conversion to
	fail. The whole thing is belt-and-braces wrapped for the same reason.
	"""
	try:
		if frappe.cache().get_value(_ZONE_WARNING_KEY):
			return
		frappe.cache().set_value(_ZONE_WARNING_KEY, 1,
			expires_in_sec=_ZONE_WARNING_TTL_SECONDS)
		from ffl_integrations.notifications import safe_log_error

		safe_log_error(
			"Site timezone unresolvable — distributor heartbeat ages are derived",
			f"System Settings.time_zone is {name!r}, which does not resolve to a "
			"known zone (empty, misspelt, or missing tzdata). Heartbeat timestamps "
			"from the catalog service are still being placed on the site's clock, "
			"but by measuring the offset against now_datetime() rather than by "
			"reading the zone — correct for a recent heartbeat, and up to an hour "
			"out for one that spans a DST change. Set a valid IANA zone (e.g. "
			"America/Chicago) to remove the guesswork. This warning is logged at "
			"most once an hour.")
	except Exception:  # noqa: BLE001 — a warning must never break the conversion
		pass


# ------------------------------------------------------------------ ops face
# /v1/catalog/ops/* — the RUN-HEALTH face of the catalog service,
# behind its own `catalog:ops` scope. Not part of the data plane: nothing here
# feeds pricing/stock decisions, it only renders the Catalog Service page, so a
# failure degrades a panel and never a sale. All three are cached for
# CACHE_TTL_SECONDS: the page auto-refreshes every 60s and several staff may
# watch it at once — without the cache that is one round trip per viewer per
# refresh against a service we share with the WP side (risk R8).


_OPS_FORBIDDEN = "__forbidden__"


def _ops_get(cache_key: str, path: str, params: dict) -> dict:
	"""Cached GET for the three ops endpoints, INCLUDING a negative entry for 403.

	"This client has no `catalog:ops`" is not a transient error — after the G1
	OQ-2 ruling it is the STEADY STATE for every tenant that did not buy the
	scope. Caching only the success path meant a page that refreshes itself
	every 60s would keep firing requests the service is certain to refuse, one
	per viewer, forever. The negative entry expires on the same TTL, so granting
	the scope still takes effect within a minute."""
	from ffl_integrations.data_service import CATALOG, require

	# the ops* callers asked _backend(); a direct caller is guarded too
	require(CATALOG, notify=False, house=(params or {}).get("distributor") or None)
	hit = frappe.cache().get_value(cache_key)
	if hit == _OPS_FORBIDDEN:
		raise DataServiceForbidden(f"Data Service returned 403 for {path}")
	if hit:
		return hit
	try:
		data = client.get(path, params, distributor=(params or {}).get("distributor"))
	except DataServiceForbidden:
		frappe.cache().set_value(cache_key, _OPS_FORBIDDEN,
			expires_in_sec=CACHE_TTL_SECONDS)
		raise
	frappe.cache().set_value(cache_key, data, expires_in_sec=CACHE_TTL_SECONDS)
	return data


def ops_status(distributor: str, since: int | None = None) -> dict:
	backend = _backend(distributor)
	if backend is not None:
		return backend.ops_status(distributor, since)
	params = {"distributor": distributor}
	if since is not None:
		params["since"] = int(since)
	return _ops_get(f"rc_ops_status:{distributor}:{since}",
		"/v1/catalog/ops/status", params)


def ops_history(distributor: str, limit: int = 20) -> dict:
	backend = _backend(distributor)
	if backend is not None:
		return backend.ops_history(distributor, limit)
	return _ops_get(f"rc_ops_history:{distributor}:{limit}",
		"/v1/catalog/ops/history", {"distributor": distributor, "limit": int(limit)})


def ops_flags(distributor: str) -> dict:
	backend = _backend(distributor)
	if backend is not None:
		return backend.ops_flags(distributor)
	return _ops_get(f"rc_ops_flags:{distributor}",
		"/v1/catalog/ops/flags", {"distributor": distributor})


def restrictions_entries(distributor: str, sku: str) -> list:
	backend = _backend(distributor)
	if backend is not None:
		return backend.restrictions_entries(distributor, sku)
	data = client.get(
		f"/v1/catalog/restrictions/{quote(str(distributor), safe='')}"
		f"/{quote(str(sku), safe='')}", distributor=distributor)
	return data.get("entries") or []
