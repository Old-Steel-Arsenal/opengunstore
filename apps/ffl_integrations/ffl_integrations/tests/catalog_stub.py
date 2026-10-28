"""In-memory catalog for tests — replaces inserting `RSR Catalog Item` docs.

seed_catalog() accepts POS-loader-era dicts (rsr_stock_number / dealer_price /
map_price / quantity / department_number aliases) so migrating a test is a
mechanical swap: delete the get_doc(...RSR Catalog Item...).insert() helper,
call seed_catalog() with the same dicts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe

# ---------------------------------------------------------------- ops face
# Mirrors the service's `catalog/ops.py` constants. DELIBERATELY a
# second copy, not an import: this is a fake of a REMOTE service, and a fake that
# reads the real thresholds from our side could never catch the two sides
# drifting apart. The POS page shows both sides' thresholds for the
# same reason.
OPS_FEEDS = ("quantity", "catalog", "restrictions", "deleted", "attributes")
OPS_FEED_THRESHOLD_SECONDS = {
	"quantity": 5400, "catalog": 21600, "restrictions": 21600,
	"deleted": 21600, "attributes": 21600,
}
# Default heartbeat ages, seconds behind `ops_server_time`. All fresh: a test
# that wants a stale/never-synced feed says so with `set_ops_feed`.
_OPS_DEFAULT_AGES = {"quantity": 312, "catalog": 4100, "restrictions": 4100,
	"deleted": 4100, "attributes": 4100}
_CATALOG_DUE_AFTER_SECONDS = 2 * 3600  # service: last catalog success + 2h

def heartbeat_ts(age_seconds: float = 0) -> str:
	"""A `health()` heartbeat, spelled the way the SERVICE spells it: ISO 8601
	**with an explicit offset**, naming a real instant `age_seconds` ago.

	Use this for every `last_*_sync_at` / `catalog_version` value handed to
	`set_health`. A bare `now_datetime().isoformat()` is not a shortcut for it —
	it is a different value, and the difference took CI down.

	What happened: `now_datetime()` is the SITE's wall clock and `.isoformat()`
	drops the marker, so the stub was posting a naive string where the real
	service posts `2026-08-06T02:30:36-05:00`. `remote_catalog.parse_service_ts`
	reads a naive value as UTC — correct, because the service speaks
	UTC — and then converts to the site's zone. So a heartbeat seeded as "now"
	came back as `now - site_offset`: on the dev site (America/New_York, UTC-4)
	a brand-new heartbeat read as four hours old, sailed past the 90-minute
	threshold, and `test_fresh_heartbeat_is_quiet` found its distributor in the
	stale list. The old host-zone bug had been masking it, because that code
	passed naive values straight through.

	The lesson is the stub-fidelity one, not the timezone one: **a fake must
	answer in the same shape as the thing it fakes.** Two timestamp dialects were
	in use — `_ops_ts` (offset-bearing, right) for the ops face and hand-rolled
	bare strings for the health face — and the tests using the wrong one were
	only ever passing because production had a matching bug.

	Anchored to `datetime.now(UTC)` rather than `now_datetime()` on purpose: an
	aware UTC instant is what the service actually sends, and it is correct on
	any site zone and any host zone. That is what stops this from being a
	tripwire that only fires in certain deployments.
	"""
	return (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()


_ALIASES = {
	"rsr_stock_number": "sku", "dealer_price": "cost", "map_price": "map",
	"quantity": "qty", "department_number": "department",
}
_DEFAULTS = {
	"upc": "", "manufacturer": "", "model": "", "department": "",
	"cost": 0.0, "map": 0.0, "msrp": 0.0, "qty": 0,
	"ffl_required": False, "dropship_blocked": False,
	"vendor_approval_required": False, "image_url": "", "discontinued": False,
	"status_flag": "", "short_description": "", "mfg_number": "", "caliber": "",
	"long_description": "", "image_name": "", "image_url_hires": "",
	"weight_lb": 0.0, "prop65": False, "ships_ground_only": False,
	"signature_required": False,
	# RSR's full-case price PER UNIT and case size (0 = not sold by the case)
	"case_cost": 0.0, "units_per_case": 0,
}
_BOOL_KEYS = {"ffl_required", "dropship_blocked", "vendor_approval_required",
	"discontinued", "prop65", "ships_ground_only", "signature_required"}


# The service's product class (the Data Service's `catalog_offers.product_class`),
# as its full offer rows and products carry it — the POS keeps no class table.
_SERVICE_CLASSES = (
	("rsr", {"01", "02"}, "Handgun"), ("rsr", {"03", "05"}, "Long Gun"),
	("rsr", {"18"}, "Ammunition"),
	("ss", {"26", "64", "96"}, "Handgun"), ("ss", {"40", "48", "57", "58", "59", "94"}, "Long Gun"),
	("ss", {"63", "65", "66", "67", "93"}, "Other Firearm"),
)
_SERVICE_SS_AMMO = {"4", "5", "6", "49", "69", "70", "71", "72", "90", "91"}


def service_class(distributor, department, ffl_required) -> str:
	dept = str(department or "").strip()
	for house, codes, klass in _SERVICE_CLASSES:
		if distributor == house and dept in codes:
			return klass
	if ffl_required:
		return "Other Firearm"
	if distributor == "ss" and dept in _SERVICE_SS_AMMO:
		return "Ammunition"
	return "Accessory"


def _normalize(raw: dict) -> dict:
	row = dict(_DEFAULTS)
	for key, value in raw.items():
		key = _ALIASES.get(key, key)
		if key in _BOOL_KEYS:
			value = bool(value)
		row[key] = value
	row["upc"] = (row.get("upc") or "").strip() or None
	if "sku" not in row or not row["sku"]:
		raise ValueError("seed_catalog row needs sku / rsr_stock_number")
	return row



def _brand_key(value) -> str:
	"""The service's brand fold (osa_dropship pricing.brand_key; parity pinned on
	the service's side): case, punctuation and '&' vs 'and' folded, spelling kept."""
	import re

	s = str(value or "").strip().lower().replace("&", " and ")
	return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


class CatalogStub:
	# Page size (per house) for `products_changes()`. Large by default so most tests
	# see every change in one page; a test that exercises the change feed's
	# multi-page "more" loop sets it small (e.g. `self.stub.changes_page_size = 2`).
	changes_page_size = 10_000

	# `/v1/catalog/ops/status` server clock. Fixed so age arithmetic in tests is
	# deterministic; `set_ops_now()` moves it.
	ops_server_time = "2026-07-31T00:05:12+00:00"

	def __init__(self):
		self.offers: dict[tuple, dict] = {}       # (distributor, sku) -> row
		self.seq = 0
		self.change_seq: dict[tuple, int] = {}
		self.restr: dict[tuple, list] = {}
		self.health_overrides: dict[str, dict] = {}
		# product key -> optimized copy {title, description} (the Data Service's optimized-copy tables)
		self.optimized: dict[str, dict] = {}
		# ops face
		self.known_distributors: set = {"rsr"}     # has a credentials row service-side
		self.ops_overrides: dict[str, dict] = {}   # distributor -> status field overrides
		self.ops_feed_ages: dict[str, dict] = {}   # distributor -> {feed: age_s | None}
		self.sync_runs: dict[str, list] = {}       # distributor -> newest-first runs
		self.ops_error: Exception | None = None    # raise from every ops_* call
		# catalog master (Data Service): the category tree and per-product attrs
		self.tree: dict[str, dict] = {}            # slug -> node (no path / changed)
		self.tree_seq = 0
		self.master: dict[str, dict] = {}          # product key -> {category, legal}
		self.categories_error: Exception | None = None

	def upsert(self, distributor: str, row: dict):
		key = (distributor, row["sku"])
		if self.offers.get(key) != row:
			self.seq += 1
			self.change_seq[key] = self.seq
		self.offers[key] = row

	# ---- endpoint mirrors -------------------------------------------------
	@staticmethod
	def _full(distributor, row) -> dict:
		"""A full offer row as the service sends it."""
		return dict(row, distributor=distributor,
			product_class=row.get("product_class")
				or service_class(distributor, row["department"], row["ffl_required"]))

	def rows(self, distributor, skus):
		want = set(skus)
		return {sku: self._full(distributor, row)
			for (d, sku), row in self.offers.items()
			if d == distributor and sku in want}

	def search(self, distributor, q, limit):
		"""``distributor=None`` searches every house and merges offers by UPC —
		the service's ProductHit contract (best_offer = cheapest known cost,
		unknown cost sorts last)."""
		ql = q.lower()
		by_key: dict = {}
		for (d, sku), row in sorted(self.offers.items()):
			if (distributor and d != distributor) or row["status_flag"] == "Deleted":
				continue
			hay = " ".join([sku, row["upc"] or "", row["mfg_number"],
				row["manufacturer"], row["model"], row["short_description"]]).lower()
			if ql not in hay:
				continue
			# A search offer is the service's OfferOut summary: no product_class.
			offer = {k: v for k, v in row.items() if k != "product_class"}
			offer["distributor"] = d
			key = row["upc"] or f"{d}:{sku}"
			hit = by_key.setdefault(key, {"upc": row["upc"], "manufacturer": row["manufacturer"],
				"model": row["model"], "description": row["short_description"],
				"image_url": row["image_url"], "ffl_required": row["ffl_required"],
				"offers": []})
			hit["offers"].append(offer)
		hits = []
		for hit in by_key.values():
			hit["best_offer"] = min(hit["offers"], key=lambda o: (
				not (o.get("qty") or 0) > 0, (o.get("cost") or 0) <= 0, o.get("cost") or 0))
			hits.append(hit)
		return hits[:limit]

	def health(self, distributor):
		base = {"distributor": distributor,
			"last_catalog_sync_at": "2026-07-31T00:00:00+00:00",
			"last_quantity_sync_at": "2026-07-31T00:00:00+00:00",
			"last_restrictions_sync_at": None,
			"catalog_version": "2026-07-31T00:00:00+00:00",
			"offers_total": sum(1 for (d, _s) in self.offers if d == distributor),
			"offers_active": sum(1 for (d, _s), r in self.offers.items()
				if d == distributor and not r["discontinued"] and r["status_flag"] != "Deleted")}
		base.update(self.health_overrides.get(distributor, {}))
		return base

	def set_health(self, distributor="rsr", **overrides):
		"""Override `health()` fields.

		Timestamps passed here MUST be offset-bearing ISO — build them with
		`heartbeat_ts()`. See that function for why a bare string is a bug in the
		test rather than a convenience."""
		self.health_overrides.setdefault(distributor, {}).update(overrides)

	def item_by_upc(self, upc):
		if not upc:  # None/"" would otherwise match every orphan (upc=None) row
			return None
		offers = [self._full(d, row) for (d, _s), row in sorted(self.offers.items())
			if row["upc"] == upc]
		if not offers:
			return None
		first = offers[0]
		return {"product": {"upc": upc, "manufacturer": first["manufacturer"],
			"model": first["model"], "description": first["short_description"],
			"image_url": first["image_url"], "ffl_required": first["ffl_required"],
			"content_source": first["distributor"], "content_locked": False},
			"offers": offers}

	# ---- products face (/v1/catalog/products*) ----------------------------
	# The same seeded offers, one product per UPC (``<house>:<sku>`` without one).
	# ``brand_key`` is the POS's own fold (the service took it from here, and the
	# parity is pinned on its side); ``product_class`` is the service's.

	def _product_offers(self):
		out: dict = {}
		for (d, sku), row in sorted(self.offers.items()):
			if row["status_flag"] == "Deleted":
				continue
			key = row["upc"] or f"{d}:{sku}"
			out.setdefault(key, []).append(dict(self._full(d, row), product_key=key,
				brand_key=_brand_key(row["manufacturer"])))
		return out

	@staticmethod
	def _offer_meets(o, f) -> bool:
		if o["discontinued"]:
			return False
		if f.get("brand") and o["brand_key"] not in {_brand_key(b) for b in f["brand"]}:
			return False
		if f.get("product_class") and o["product_class"] not in f["product_class"]:
			return False
		if f.get("distributor") and o["distributor"] not in f["distributor"]:
			return False
		if f.get("department") and f"{o['distributor']}:{o['department']}" not in f["department"]:
			return False
		if f.get("min_cost") is not None and o["cost"] < float(f["min_cost"]):
			return False
		if f.get("max_cost") is not None and not 0 < o["cost"] <= float(f["max_cost"]):
			return False
		if f.get("ffl_required") is not None and bool(o["ffl_required"]) != bool(f["ffl_required"]):
			return False
		if f.get("in_stock") and o["qty"] <= 0:
			return False
		q = (f.get("q") or "").strip()
		if q and q not in (o["upc"], o["sku"], o["mfg_number"]) and q.lower() not in o["sku"].lower() \
				and not all(w in o["short_description"].lower() for w in q.lower().split()):
			return False
		return True

	def _product(self, key, offers) -> dict:
		lead = offers[0]
		opt = self.optimized.get(key) or {}
		attrs = self.master.get(key) or {}
		slug = attrs.get("category")
		return {"key": key, "upc": lead["upc"], "manufacturer": lead["manufacturer"],
			"model": lead["model"], "description": opt.get("title") or lead["short_description"],
			"long_description": opt.get("description") or lead.get("long_description") or "",
			"content_optimized": bool(opt), "image_url": lead["image_url"],
			"ffl_required": any(o["ffl_required"] for o in offers), "offers": offers,
			"category": {"slug": slug, "name": self.tree.get(slug, {}).get("name", slug),
				"path": self._path(slug)} if slug else None,
			"legal": attrs.get("legal"), "fits_platform": []}

	# ---- catalog master (/v1/catalog/categories, product attrs) -----------
	def seed_tree(self, nodes):
		"""``[(slug, name, parent, store_hint)]`` or node dicts, parents first."""
		for n in nodes:
			if not isinstance(n, dict):
				slug, name, parent, hint = (list(n) + [None, None])[:4]
				n = {"slug": slug, "name": name, "parent": parent, "store_hint": hint}
			siblings = [x for x in self.tree.values() if x["parent"] == n.get("parent")]
			node = {"slug": n["slug"], "name": n["name"], "parent": n.get("parent"),
				"level": (self.tree[n["parent"]]["level"] + 1) if n.get("parent") else 1,
				"position": n.get("position") or len(siblings) + 1, "active": n.get("active", True),
				"menu": n.get("menu", True), "store_hint": n.get("store_hint")}
			self.tree_seq += 1
			self.tree[node["slug"]] = dict(node, seq=self.tree_seq)

	def edit_node(self, slug, **changes):
		"""A workbench edit: rename / move (``parent``) / hint / deactivate — one
		new sequence number, as the service's trigger stamps it."""
		node = self.tree[slug]
		if "parent" in changes:
			changes["level"] = (self.tree[changes["parent"]]["level"] + 1) if changes["parent"] else 1
		self.tree_seq += 1
		node.update(changes, seq=self.tree_seq)

	def _path(self, slug):
		out, seen = [], set()
		while slug and slug in self.tree and slug not in seen:
			seen.add(slug)
			out.append({"slug": slug, "name": self.tree[slug]["name"]})
			slug = self.tree[slug]["parent"]
		return list(reversed(out))

	# Page size of the ``?since=`` feed; a test of the paging loop makes it small.
	categories_page_size = 5000

	def categories(self, since=None):
		"""``GET /v1/catalog/categories`` — full read, or the nodes changed after
		``since`` (``categories_page_size`` a page, oldest change first) with their
		unchanged ancestors."""
		if self.categories_error:
			raise self.categories_error
		ordered = sorted(self.tree.values(), key=lambda n: (n["level"], n["position"], n["slug"]))
		cursor, more = self.tree_seq, False
		if since:
			by_seq = sorted((n for n in ordered if n["seq"] > int(since)), key=lambda n: n["seq"])
			more = len(by_seq) > self.categories_page_size
			by_seq = by_seq[:self.categories_page_size]
			cursor = by_seq[-1]["seq"] if by_seq else int(since)
			changed = {n["slug"] for n in by_seq}
			keep = set(changed)
			for slug in changed:
				keep |= {p["slug"] for p in self._path(slug)}
			ordered = [n for n in ordered if n["slug"] in keep]
		else:
			changed = {n["slug"] for n in ordered}
		return {"categories": [], "nodes": [dict(n, path=self._path(n["slug"]),
				changed=n["slug"] in changed) for n in ordered],
			"cursor": cursor, "more": more}

	def set_master(self, key, category, *, is_firearm=False, nfa_type="none", is_antique=False):
		"""The master's category / legal fields for a product; its offers are
		re-stamped, so ``/products/changes`` reports it (as the service does)."""
		self.master[key] = {"category": category, "legal": {"is_firearm": is_firearm,
			"nfa_type": nfa_type, "is_antique": is_antique}}
		_clear_memo()   # the per-request legal memo (util.prefetch_legal)
		for (d, sku), row in self.offers.items():
			if (row["upc"] or f"{d}:{sku}") == key:
				self.seq += 1
				self.change_seq[(d, sku)] = self.seq

	def products_page(self, params):
		"""``GET /v1/catalog/products``, the filters the POS sends: key order by
		cursor, or brand / cost / qty (over the matching offers) by offset."""
		self.products_calls = getattr(self, "products_calls", 0) + 1
		products = self._product_offers()
		meeting = {k: [o for o in offers if self._offer_meets(o, params)]
			for k, offers in products.items()}
		matched = sorted(k for k, offers in meeting.items() if offers)
		total = len(matched) if params.get("count") and not params.get("cursor") \
			and not params.get("offset") else None
		sort, limit = params.get("sort") or "key", int(params.get("limit") or 100)
		if sort == "key":
			if params.get("cursor"):
				matched = [k for k in matched if k > params["cursor"]]
			page = matched[:limit]
			cursor = page[-1] if len(page) == limit else None
		else:
			rank = {"brand": lambda k: min(o["brand_key"] for o in meeting[k]),
				"cost": lambda k: min((o["cost"] for o in meeting[k] if o["cost"] > 0),
					default=float("inf")),
				"qty": lambda k: -max(o["qty"] for o in meeting[k])}[sort]
			matched.sort(key=lambda k: (rank(k), k))
			start = int(params.get("offset") or 0)
			page, cursor = matched[start:start + limit], None
		return {"products": [self._product(k, products[k]) for k in page],
			"cursor": cursor, "total": total}

	def products_facets(self, distributor=None):
		"""``GET /v1/catalog/products/facets``: over live offers (one house's with
		``distributor``), products counted. A department's name is its
		``department_names[(house, code)]`` entry."""
		live = {k: [o for o in offers if not o["discontinued"]
				and (not distributor or o["distributor"] == distributor)]
			for k, offers in self._product_offers().items()}
		brands, spell, classes, depts = {}, {}, {}, {}
		for key, offers in live.items():
			for bk in {o["brand_key"] for o in offers if o["brand_key"]}:
				brands.setdefault(bk, set()).add(key)
			for o in offers:
				if o["brand_key"]:
					spell.setdefault(o["brand_key"], []).append(o["manufacturer"])
				classes.setdefault(o["product_class"], set()).add(key)
				if o["department"]:
					depts.setdefault((o["distributor"], o["department"]), set()).add(key)
		names = getattr(self, "department_names", {})
		return {"brands": [{"key": k, "name": max(sorted(spell[k]), key=spell[k].count),
				"products": len(v)} for k, v in sorted(brands.items())],
			"classes": [{"key": k, "products": len(v)} for k, v in sorted(classes.items())],
			"departments": [{"key": f"{h}:{d}", "distributor": h, "department": d,
				"name": names.get((h, d), ""), "products": len(v)}
				for (h, d), v in sorted(depts.items())]}

	def products_batch(self, keys):
		products = self._product_offers()
		return {"products": [self._product(k, products[k]) for k in dict.fromkeys(keys)
			if k in products], "keys": {}}

	def products_changes(self, since):
		"""``GET /v1/catalog/products/changes``: the product key of every offer
		changed since ``since`` (``rsr:3;ss:5`` — one sequence per house), each
		once; ``changes_page_size`` per house per page. A Deleted offer names its
		product too."""
		pos = {h: int(n) for h, n in (p.split(":") for p in filter(None, since.split(";")))}
		keys, more = [], False
		for house in sorted({d for d, _s in self.change_seq} | set(pos)):
			rows = sorted((seq, sku) for (d, sku), seq in self.change_seq.items()
				if d == house and seq > pos.get(house, 0))
			more = more or len(rows) > self.changes_page_size
			rows = rows[:self.changes_page_size]
			keys += [self.offers[(house, sku)]["upc"] or f"{house}:{sku}" for _seq, sku in rows]
			if rows:
				pos[house] = rows[-1][0]
		return {"keys": list(dict.fromkeys(keys)),
			"cursor": ";".join(f"{h}:{pos[h]}" for h in sorted(pos)), "more": more}

	def restrictions_entries(self, distributor, sku):
		return self.restr.get((distributor, sku), [])

	def seed_restrictions(self, sku, entries, distributor="rsr"):
		self.restr[(distributor, sku)] = entries

	# ---- ops face mirrors -------------------------------------
	# Derived from the SAME seeded offers as the data plane, so a test that seeds
	# three SKUs sees three SKUs on both faces and the page's two panels can be
	# checked against each other (verification F5).

	def _ops_rows(self, distributor):
		return [row for (d, _s), row in sorted(self.offers.items()) if d == distributor]

	def _ops_active(self, rows):
		return [r for r in rows
			if not r["discontinued"] and r["status_flag"] != "Deleted"]

	def _ops_known(self, distributor):
		return (distributor in self.known_distributors
			or distributor in self.ops_overrides
			or bool(self._ops_rows(distributor)))

	def _ops_ts(self, age_seconds):
		"""`ops_server_time` minus an age, as the service spells timestamps."""
		if age_seconds is None:
			return None
		base = datetime.fromisoformat(self.ops_server_time)
		if base.tzinfo is None:
			base = base.replace(tzinfo=UTC)
		return (base - timedelta(seconds=age_seconds)).isoformat()

	def set_ops_now(self, iso: str):
		"""Move the service clock (ISO 8601 with offset)."""
		self.ops_server_time = iso

	def set_ops_feed(self, feed: str, age_seconds, distributor="rsr"):
		"""Age one feed's heartbeat. `age_seconds=None` → never_synced."""
		if feed not in OPS_FEED_THRESHOLD_SECONDS:
			raise ValueError(f"unknown feed {feed!r}; expected one of {OPS_FEEDS}")
		self.known_distributors.add(distributor)
		self.ops_feed_ages.setdefault(distributor, {})[feed] = age_seconds

	def set_ops(self, distributor="rsr", **overrides):
		"""Override any top-level `ops/status` field (and register the code as
		known service-side)."""
		self.known_distributors.add(distributor)
		self.ops_overrides.setdefault(distributor, {}).update(overrides)

	def seed_sync_runs(self, runs, distributor="rsr"):
		"""Newest-first `sync_runs` rows for `ops/history` (and the failure
		summary on `ops/status`). Only `status` is required."""
		self.known_distributors.add(distributor)
		normalized = []
		for raw in runs:
			run = {"finished_at": self._ops_ts(600), "source": f"{distributor}_quantity",
				"status": "success", "rows_total": 0, "duration_seconds": 0,
				"error_class": ""}
			run.update(raw)
			normalized.append(run)
		self.sync_runs[distributor] = normalized
		return normalized

	def set_ops_error(self, exc: Exception | None):
		"""Make every ops_* call raise — how a test drives the page's degraded
		states (service down / `catalog:ops` not granted)."""
		self.ops_error = exc

	def ops_status(self, distributor, since=None):
		if self.ops_error:
			raise self.ops_error
		known = self._ops_known(distributor)
		rows = self._ops_rows(distributor)
		active = self._ops_active(rows)
		if known:
			ages = dict(_OPS_DEFAULT_AGES)
			ages.update(self.ops_feed_ages.get(distributor, {}))
		else:
			# No credentials row and no offers: the service answers 200 with an
			# all-zero body rather than 404 (verification D3).
			ages = {feed: None for feed in OPS_FEEDS}
		feeds = []
		for feed in OPS_FEEDS:
			age = ages.get(feed)
			threshold = OPS_FEED_THRESHOLD_SECONDS[feed]
			if age is None:
				stale, reason = True, "never_synced"
			elif age >= threshold:
				stale, reason = True, "stale"
			else:
				stale, reason = False, "fresh"
			feeds.append({"feed": feed, "last_success_at": self._ops_ts(age),
				"age_seconds": age, "threshold_seconds": threshold,
				"stale": stale, "reason": reason})
		runs = self.sync_runs.get(distributor, [])
		failures = [r for r in runs if r["status"] != "success"]
		catalog_age = ages.get("catalog")
		out = {
			"distributor": distributor,
			"known": known,
			"ingest_enabled": known,
			"server_time": self.ops_server_time,
			"feeds": feeds,
			"offers_total": len(rows),
			"offers_active": len(active),
			"products_total": len({r["upc"] for r in rows if r["upc"]}),
			# Every seeded row counts as "changed recently" — the stub has no
			# per-row clock. A test that needs a specific number sets it.
			"changed_24h": len(rows),
			"change_seq_max": max([seq for (d, _s), seq in self.change_seq.items()
				if d == distributor], default=0),
			"next_catalog_due_at": (None if catalog_age is None
				else self._ops_ts(catalog_age - _CATALOG_DUE_AFTER_SECONDS)),
			"failures_24h": len(failures),
			"last_failure": ({"source": failures[0]["source"],
				"at": failures[0]["finished_at"],
				"error_class": failures[0]["error_class"]} if failures else None),
			"cursor_lag": (None if since is None else {
				"since": int(since),
				"offers_pending": sum(1 for (d, _s), seq in self.change_seq.items()
					if d == distributor and seq > int(since))}),
		}
		out.update(self.ops_overrides.get(distributor, {}))
		return out

	def ops_history(self, distributor, limit=20):
		if self.ops_error:
			raise self.ops_error
		runs = list(self.sync_runs.get(distributor, []))[:limit]
		return {"distributor": distributor, "runs": runs}

	def ops_flags(self, distributor):
		if self.ops_error:
			raise self.ops_error
		rows = self._ops_rows(distributor)
		active = self._ops_active(rows)
		orphans = [r for r in rows if not r["upc"]]
		departments: dict = {}
		for r in active:
			bucket = departments.setdefault(
				r["department"], {"number": r["department"],
					# The stub has no department dictionary; the service resolves
					# names from its own table. Shape, not prose, is what's pinned.
					"name": r["department"], "ffl_required": False, "offers": 0})
			bucket["offers"] += 1
			bucket["ffl_required"] = bucket["ffl_required"] or bool(r["ffl_required"])
		top = sorted(departments.values(),
			key=lambda d: (-d["offers"], d["number"]))[:10]
		return {
			"distributor": distributor,
			"active": len(active),
			"in_stock": sum(1 for r in active if r["qty"] > 0),
			"discontinued": sum(1 for r in rows if r["discontinued"]),
			"deleted": sum(1 for r in rows if r["status_flag"] == "Deleted"),
			"upc_have": sum(1 for r in rows if r["upc"]),
			"upc_orphan": len(orphans),
			"flags": {
				"map_zero": sum(1 for r in active if not r["map"]),
				"cost_over_map": sum(1 for r in active
					if r["map"] and r["cost"] > r["map"]),
				"no_image": sum(1 for r in active if not r["image_url"]),
				"orphan_upc": len(orphans),
			},
			"departments": top,
		}


def install() -> CatalogStub:
	stub = CatalogStub()
	frappe.flags.dist_catalog_stub = stub
	_clear_memo()
	# A fresh catalog: counts kept from another test's (or run's) catalog do not apply.
	frappe.cache.delete_keys("dist_rule_count:")
	return stub


def uninstall():
	frappe.flags.dist_catalog_stub = None
	_clear_memo()


def _clear_memo() -> None:
	"""remote_catalog.cached_row()'s per-request memo is documented (in
	remote_catalog.py) to outlive uninstall() BY DESIGN — a real web request
	gets a fresh frappe.local, so the memo naturally resets on every request
	boundary and this is a legitimate same-request cache. `bench run-tests` has
	no such boundary between test methods, so without this a stale memo entry
	seeded by an EARLIER test's stub (e.g. GLK19 in test_rsr_provider_remote.py
	/ test_rsr_search_shim.py / test_remote_catalog.py) silently shadows a
	brand-new stub's answer for the same (distributor, sku) key in a LATER
	test that reuses the same mnemonic SKU (test_rsr_promote.py,
	test_distributor_dual_source_guard.py — found via a full `make test` run,
	Task 8). Clearing it on every install()/uninstall() makes each test's own
	stub the one source of truth, matching what a real request would give it
	for free."""
	try:
		frappe.local._remote_catalog_memo = {}
	except Exception:  # noqa: BLE001 — no request/test context; nothing to clear
		pass


def seed_catalog(rows, distributor="rsr") -> CatalogStub:
	stub = getattr(frappe.flags, "dist_catalog_stub", None) or install()
	for raw in rows:
		stub.upsert(distributor, _normalize(dict(raw)))
	return stub


# The catalog master's tree as served by the Data Service — the nodes
# the POS tests need, with the seeded store hints: (slug, name, parent, hint).
TREE_A = [
	("firearms", "Firearms", None, "firearms"),
	("parts", "Parts", None, "parts"),
	("optics", "Optics", None, "optic"),
	("magazines", "Magazines", None, "magazines"),
	("accessories", "Accessories", None, "accessory"),
	("ammunition", "Ammunition", None, None),
	("firearms-pistols", "Pistols", "firearms", "pistol"),
	("firearms-revolvers", "Revolvers", "firearms", "revolver"),
	("firearms-rifles", "Rifles", "firearms", "rifle"),
	("firearms-shotguns", "Shotguns", "firearms", "shotgun"),
	("firearms-receivers-frames", "Receivers & Frames", "firearms", None),
	("firearms-nfa", "NFA", "firearms", "nfa"),
	("firearms-muzzleloaders-antiques", "Muzzleloaders & Antiques", "firearms", None),
	("firearms-other", "Other Firearms", "firearms", "other-firearm"),
	("parts-uppers", "Uppers & Conversion Kits", "parts", None),
	("parts-barrels", "Barrels", "parts", None),
	("parts-triggers", "Triggers", "parts", "triggers"),
	("parts-handguards", "Handguards & Rails", "parts", "handguards-rails"),
	("parts-stocks-grips", "Stocks & Grips", "parts", "stocks-grips"),
	("parts-gas-system", "Gas System Parts", "parts", None),
	("optics-red-dots", "Red Dots & Reflex", "optics", None),
	("magazines-rifle", "Rifle Magazines", "magazines", None),
	("accessories-weapon-lights", "Weapon Lights", "accessories", "lights"),
	("accessories-bipods-rests", "Bipods & Rests", "accessories", None),
	("accessories-knives-tools", "Knives & Tools", "accessories", "knife"),
	("accessories-other", "Other Accessories", "accessories", "other-accessories"),
	("optics-mounts-rings", "Optic Mounts & Rings", "accessories", "optic-mounts-adapters"),
	("ammunition-handgun", "Handgun Ammunition", "ammunition", None),
	("firearms-suppressors", "Suppressors", "firearms-nfa", "suppressor"),
	("firearms-other-nfa-sbr", "SBR", "firearms-nfa", "sbr"),
	("parts-barrels-rifle", "Rifle Barrels", "parts-barrels", None),
]


@contextmanager
def data_service_configured(configured: bool = True):
	"""Data Service Settings as if filled in (or blank): for tests of the real HTTP
	path (``client`` patched), whose catalog capability must not read as off on a
	site with no connection. The cached capabilities are dropped on both sides."""
	from ffl_integrations import data_service

	data_service.clear_capabilities()
	try:
		with patch.object(data_service, "_service_configured", return_value=configured):
			yield
	finally:
		data_service.clear_capabilities()
