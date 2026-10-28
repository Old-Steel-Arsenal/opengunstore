"""Small shared helpers for the distributor layer.

These live here rather than in either caller because both the generic layer and a
provider need them, and the layering rule (generic code must never import a
concrete provider) would otherwise force two copies — which is exactly what
happened before (`listing.chunk` vs `rsr_provider._chunks`,
`listing.normalize_states` vs `rsr_provider._split_states`).
"""
from __future__ import annotations


# The catalog service's product classes (the Data Service's `catalog_offers.product_class`) —
# the one class vocabulary; the POS keeps no class table of its own.
PRODUCT_CLASSES = ("Handgun", "Long Gun", "Other Firearm", "NFA", "Gun Parts",
	"Ammunition", "Accessory")
# The classes that are firearms under the law (4473, bound book, FFL checkout).
LEGAL_FIREARM_CLASSES = frozenset({"Handgun", "Long Gun", "Other Firearm", "NFA"})
# The classes that are certainly NOT firearms. A class outside both sets (blank, a
# newer service's value, a typo) never clears a flagged row.
NON_FIREARM_CLASSES = frozenset({"Gun Parts", "Accessory", "Ammunition"})

# Houses whose own `ffl_required` is the law's answer row by row (Sports South sets
# it from the item type: a lower parts kit 0, a receiver 1; Lipsey's sets it on
# exactly its firearm and NFA items, receivers and barreled actions included). RSR's
# is its shipping rule by department — every barrel and upper says 1 — so its class
# decides. Only the FALLBACK for a product the catalog master has no legal answer for.
FFL_FLAG_IS_LEGAL = frozenset({"ss", "lipseys"})
LEGAL_CACHE_SECONDS = 600


def legal_firearm(row, house: str | None = None) -> bool:
	"""Whether a catalog row is a firearm under the law.

	The catalog master's ``legal.is_firearm`` for the row's product decides when it
	has one (catalog master M3b): it is already the strict / curated answer — a
	tightening on a product no house flags makes it a firearm (the FDS lane,
	a serialized promotion); a relax exists only once the owner approved it (e.g.
	Sports South lights). The row's own ``legal`` (a product read attaches it)
	is used as is; otherwise the product is looked up (``master_legal``).

	Fallback, when the master has no answer or cannot be reached (fail-safe): no
	house ships a firearm without an FFL, so an unflagged row never is one; a
	flagged row is one unless the house's flag is only a shipping rule (RSR) AND
	the service classes the row as certainly not a firearm (Gun Parts, Accessory,
	Ammunition) — an unknown or missing class keeps it a firearm.
	``house``: the provider code; defaults to the row's own ``distributor``."""
	row = row or {}
	house = house or row.get("distributor") or ""
	legal = row.get("legal") if "legal" in row else master_legal(row, house)
	if legal and legal.get("is_firearm") is not None:
		return bool(legal["is_firearm"])
	if not row.get("ffl_required"):
		return False
	if house in FFL_FLAG_IS_LEGAL:
		return True
	return str(row.get("product_class") or "").strip() not in NON_FIREARM_CLASSES


def master_key(row, house) -> str | None:
	"""The catalog master's product key for an offer row: its UPC, else
	``<house>:<sku>``."""
	upc = str(row.get("upc") or "").strip()
	sku = str(row.get("sku") or row.get("rsr_stock_number") or "").strip()
	return upc or (f"{house}:{sku}" if house and sku else None)


def master_legal(row, house):
	"""The master's ``legal`` for the row's product, or None (unclassified, not in
	the catalog, or the service unreachable). Memoized per request, and for
	``LEGAL_CACHE_SECONDS`` site-wide outside tests."""
	key = master_key(row or {}, house)
	return prefetch_legal([key]).get(key) if key else None


def prefetch_legal(keys) -> dict:
	"""``{key: legal or None}`` for many product keys in one batch read — call it
	before looping ``legal_firearm`` over a list of rows."""
	import frappe

	from ffl_integrations.distributor import remote_catalog

	memo = remote_catalog._memo()
	out, missing = {}, []
	use_cache = not getattr(frappe.flags, "in_test", False)
	for key in dict.fromkeys(k for k in keys or [] if k):
		if ("legal", key) in memo:
			out[key] = memo[("legal", key)]
			continue
		cached = frappe.cache().get_value(f"catalog_master_legal:{key}") if use_cache else None
		if cached is not None:
			out[key] = memo[("legal", key)] = cached or None
		else:
			missing.append(key)
	if missing:
		from ffl_integrations.distributor import catalog

		try:
			answers = catalog.legal_of(missing)
		except (remote_catalog.DataServiceUnavailable, remote_catalog.CatalogStubMissing):
			return {**out, **{k: None for k in missing}}   # fallback; not memoized
		for key in missing:
			legal = answers.get(key) or None
			out[key] = memo[("legal", key)] = legal
			if use_cache:
				frappe.cache().set_value(f"catalog_master_legal:{key}", legal or {},
					expires_in_sec=LEGAL_CACHE_SECONDS)
	return out


def dealer_only_part(row, house: str | None = None) -> bool:
	"""A row the house ships only to a dealer (``ffl_required``) that is not a
	firearm under the law — an RSR barrel or upper. It cannot go to a buyer's door
	on the house's firearm drop-ship lane, and the house refuses it on the plain one."""
	return bool((row or {}).get("ffl_required")) and not legal_firearm(row, house)


# A planned line's promise that its caller re-orders it to the store when the house
# refuses it on the buyer's-door lane (osa_dropship's web router) — the one case a
# dealer-only part may be planned Direct (``base.assert_lane_fits``).
TO_STORE_ON_REFUSAL = "to_store_on_refusal"


def chunk(items, size):
	"""Yield successive `size`-length batches. Empty input yields nothing."""
	items = list(items or [])
	if size <= 0:
		raise ValueError("chunk size must be positive")
	for i in range(0, len(items), size):
		yield items[i:i + size]


def normalize_states(states) -> list:
	"""CSV / newline-separated string, or an iterable, → upper-cased codes."""
	if isinstance(states, str):
		states = states.replace("\n", ",").split(",")
	return [str(s).strip().upper() for s in (states or []) if str(s).strip()]


def site_timezone() -> str:
	"""The SITE's IANA timezone name (e.g. 'America/New_York'), or '' if unknowable.

	The site's zone, never the host's: a Frappe site carries its own `time_zone`
	in System Settings and `now_datetime()` answers in THAT zone, so a container
	running on UTC — which is every deployment we have — will disagree with the
	site by a full offset. Anything that turns a UTC instant into something a
	human reads must come through here.
	"""
	import frappe

	getter = getattr(frappe.utils, "get_system_timezone", None)
	if callable(getter):
		try:
			return str(getter() or "")
		except Exception:  # noqa: BLE001 — falls through to the System Settings read
			pass
	return str(frappe.db.get_single_value("System Settings", "time_zone") or "")


def zone_info(name):
	"""`ZoneInfo` for an IANA name, or None if it cannot be resolved.

	Never raises: a missing tzdata or a typo in System Settings must degrade a
	displayed timestamp, not blow up the caller."""
	if not name:
		return None
	try:
		from zoneinfo import ZoneInfo

		return ZoneInfo(name)
	except Exception:  # noqa: BLE001 — unknown zone / missing tzdata
		return None


def setting_value(settings, key, default) -> float:
	"""A numeric setting, or ``default`` only when it was never set. A stated 0 is a
	real value (free freight, no threshold) — ``getattr(...) or default`` would
	silently replace it."""
	value = getattr(settings, key, None)
	return float(default if value in (None, "") else value)


def case_pricing(qty, unit_cost, case_cost, units_per_case) -> dict | None:
	"""A ship-to-store line bought by the full case: ``qty`` units at ``unit_cost``,
	where the house sells ``units_per_case`` units a case at ``case_cost`` A UNIT
	(RSR's DealerCasePrice is the per-unit price of a full case, not the case's total).

	Only full cases take the case price; the rest of the line stays at the unit
	cost. None when the house has no case price for it (no case size, a case price
	of 0, or one not below the unit cost) — the line then costs ``unit_cost``.

	-> ``{units_per_case, case_cost, cases, to_fill, line_cost, unit_cost}``:
	``cases`` the full cases in the line, ``to_fill`` the units that would fill the
	next one (0 when the line is whole cases), ``line_cost`` the line's total and
	``unit_cost`` the per-unit figure a Distributor Order line stores (both rounded
	to cents). An estimate: the house's order acknowledgement and invoice replace it."""
	n = int(units_per_case or 0)
	c = float(case_cost or 0)
	u = float(unit_cost or 0)
	q = int(qty or 0)
	if n <= 0 or q <= 0 or not 0 < c < u:
		return None
	cases = q // n
	rest = q - cases * n
	line_cost = round(cases * n * c + rest * u, 2)
	return {"units_per_case": n, "case_cost": round(c, 2), "cases": cases,
		"to_fill": n - rest if rest else 0, "line_cost": line_cost,
		"unit_cost": round(line_cost / q, 2)}


def case_note(priced: dict | None) -> str:
	"""The operator's one-line hint for a ``case_pricing`` answer ("" for None):
	"Case of 50 at $7.83 each — 20 more fill a case", "2 cases at $7.83 each", or
	"2 cases at $7.83 each — 30 more fill another" (the price is per unit, as the
	house quotes it)."""
	if not priced:
		return ""
	cases, more = priced["cases"], priced["to_fill"]
	price = f"${priced['case_cost']:,.2f} each"
	if not cases:
		return f"Case of {priced['units_per_case']} at {price} — {more} more fill a case"
	head = f"{cases} case{'s' if cases > 1 else ''} at {price}"
	return f"{head} — {more} more fill another" if more else head
