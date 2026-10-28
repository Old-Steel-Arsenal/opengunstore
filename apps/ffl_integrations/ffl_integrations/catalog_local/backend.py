"""``LocalCatalog`` — the catalog read side over the ``dc_*`` tables.

Implements exactly the method set of ``tests/catalog_stub.CatalogStub`` (the
in-memory reference of the Catalog API) with the same dict shapes, so
``remote_catalog._backend()`` can hand it to every consumer unchanged: rows,
search, health, item_by_upc, restrictions_entries, products_page,
products_batch, products_changes, products_facets, categories, ops_status,
ops_history, ops_flags. Where the real service returns more than the stub
(``best_offer`` on an item, ``map_policy`` on an offer, ``total_qty``/``map`` on a
product) this returns it too.

No curated category tree is published with the local catalog: ``categories()`` is
an empty tree and every product's ``category`` is None; ``legal`` is None too,
except where a house's own rule says an FFL-flagged item is not a firearm
(``_legal``).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, timedelta

import frappe

from ffl_integrations.catalog_local import store
from ffl_integrations.catalog_local.schema import BOOL_COLUMNS, MONEY_COLUMNS

CONTRACT_VERSION = "1.1"
CHANGES_PAGE_SIZE = 1000
IN_CHUNK = 500
SEARCH_TOKENS = 8

_OFFER_FIELDS = ("distributor", "sku", "upc", "manufacturer", "model", "department", "cost",
	"map", "msrp", "map_policy", "qty", "ffl_required", "dropship_blocked",
	"vendor_approval_required", "image_url", "discontinued", "status_flag",
	"short_description", "mfg_number", "caliber", "long_description", "image_name",
	"image_url_hires", "weight_lb", "prop65", "ships_ground_only", "signature_required",
	"product_class", "case_cost", "units_per_case")
_SELECT = ", ".join(f"`{c}`" for c in (*_OFFER_FIELDS, "product_key", "brand_key"))
_LIVE = f"NOT {store.deleted_sql()}"


def _iso(dt):
	return dt.replace(tzinfo=UTC).isoformat() if dt else None


def _offer(row) -> dict:
	"""A dc_offer row → the service's OfferFull (plus product_key/brand_key)."""
	out = {}
	for k, v in dict(row).items():
		if k in BOOL_COLUMNS:
			out[k] = bool(v)
		elif k in MONEY_COLUMNS or k == "case_cost":
			out[k] = float(v or 0)
		elif k in ("qty", "units_per_case"):
			out[k] = int(v or 0)
		elif k == "upc":
			out[k] = v or None
		else:
			out[k] = v if v is not None else ""
	if store.not_orderable(out.get("status_flag"), out.get("discontinued")):
		out["qty"] = 0  # not orderable, whatever the last file said
	return out


def _chunks(values, size=IN_CHUNK):
	values = list(values)
	for start in range(0, len(values), size):
		yield tuple(values[start:start + size])


def _tokens(q: str) -> list:
	# A repeated word counts once, and before the cap, so it does not crowd out a later word.
	return list(dict.fromkeys(re.findall(r"[a-z0-9]+", (q or "").lower())))[:SEARCH_TOKENS]


def _as_list(v) -> list:
	if v in (None, "", []):
		return []
	return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _truthy(v) -> bool:
	return str(v).strip().lower() in ("1", "true", "yes") if isinstance(v, str) else bool(v)


def _best(offers: list, priorities: dict):
	"""The offer we would buy from now: never a Deleted one, a current one over a
	discontinued one, then the merge rule."""
	pool = [o for o in offers if not store.is_deleted(o.get("status_flag"))]
	pool = [o for o in pool if not o.get("discontinued")] or pool
	return min(pool, key=lambda o: store.offer_sort_key(o, priorities)) if pool else None


def _legal(offers) -> dict | None:
	"""The product's legal answer where a house's own rule says an FFL-flagged item is
	not a firearm (the Data Service's owner rules, e.g. Sports South lights: a driver's
	``never_firearm_departments``). When every FFL-flagged live offer of the product sits
	in such a department of its own house, the product is not a firearm but still ships
	only to a dealer (its flag stands). Everywhere else None — the consumers' "not
	classified" path, which reads the flag and the class."""
	from ffl_integrations.catalog_local import sync

	drivers = sync.load_drivers()
	flagged = [o for o in offers if o["ffl_required"]]

	def owner_ruled(o):
		return str(o.get("department") or "").strip() in getattr(
			drivers.get(o["distributor"]), "never_firearm_departments", frozenset())

	return {"is_firearm": False, "nfa_type": "none", "is_antique": False} if flagged and all(
		owner_ruled(o) for o in flagged) else None


class LocalCatalog:
	changes_page_size = CHANGES_PAGE_SIZE

	# ---------------------------------------------------------------- data plane
	def rows(self, distributor, skus) -> dict:
		"""``{requested sku: offer}``. Matched on the stored spelling
		(``store.norm_sku``: trimmed, column length) and returned under the
		spelling asked for, so ``rows(h, [s]).get(s)`` finds it."""
		asked: dict = {}
		for s in skus or []:
			if s and (norm := store.norm_sku(s)):
				asked.setdefault(norm, []).append(str(s))
		out = {}
		for chunk in _chunks(asked):
			for r in frappe.db.sql(f"SELECT {_SELECT} FROM dc_offer WHERE distributor = %(d)s "
					"AND sku IN %(s)s", {"d": distributor, "s": chunk}, as_dict=True):
				o = _offer(r)
				o.pop("product_key"), o.pop("brand_key")
				for spelling in dict.fromkeys(asked.get(o["sku"], ())):
					out[spelling] = dict(o)
		return out

	def search(self, distributor, q, limit) -> list:
		"""Token AND over ``search_text`` plus exact identifiers (UPC, SKU, MFG#);
		one ProductHit per product key. A token matches anywhere in a word, so a
		number inside a distributor code still finds the product (SS spells the
		Holosun 507 "HS507COMP"). Ranking: exact identifiers, then one score added
		up per query word: 3 when it is a whole word of the brand (more than the
		model and title together, so the brand's own product leads a third-party
		part that names it), plus 1 each when it starts a word of the model or
		title (so "glock 19" ranks the GLOCK model 19 above a holster numbered
		15190, and "mag" does not count as the brand Magpul), then orderable stock.
		# lazy: LIKE '%tok%' scans the live offers and REGEXP scores the matches
		# (measured on the real RSR + Sports South feeds, ~89k rows: about 100 ms,
		# 150 ms for a one-letter query, the same as before the ranking); switch to
		# InnoDB FULLTEXT boolean mode if that grows (needs innodb_ft_min_token_size=2
		# and an empty stopword list)."""
		q = (q or "").strip()
		tokens = _tokens(q)
		if not q:
			return []
		params = {"q": q, "sku": store.norm_sku(q), "upc": store.normalize_upc(q)[0] or q,
			"d": distributor, "limit": max(1, int(limit or 10))}
		exact = "(upc = %(upc)s OR sku = %(sku)s OR mfg_number = %(q)s)"
		like = " AND ".join(f"search_text LIKE %(t{n})s" for n in range(len(tokens)))
		params.update({f"t{n}": f"%{t}%" for n, t in enumerate(tokens)})
		# Tokens are [a-z0-9]+ only, so they are safe inside a regular expression.
		params.update({f"w{n}": f"(^|[^a-z0-9]){t}" for n, t in enumerate(tokens)})
		params.update({f"b{n}": f"(^| ){t}( |$)" for n, t in enumerate(tokens)})
		score = " + ".join(f"3 * (brand_key REGEXP %(b{n})s) + (model REGEXP %(w{n})s)"
			f" + (short_description REGEXP %(w{n})s)" for n in range(len(tokens))) or "0"
		scope = " AND distributor = %(d)s" if distributor else ""
		keys = [r[0] for r in frappe.db.sql(f"""SELECT product_key FROM dc_offer
			WHERE {_LIVE}{scope} AND ({exact}{f" OR ({like})" if like else ""})
			GROUP BY product_key ORDER BY MAX({exact}) DESC, MAX({score}) DESC,
				MAX({store.ORDERABLE_QTY_SQL} > 0) DESC, product_key LIMIT %(limit)s""", params)]
		if not keys:
			return []
		offers: dict = {}
		for r in frappe.db.sql(f"""SELECT {_SELECT} FROM dc_offer WHERE product_key IN %(k)s
				AND {_LIVE}{scope} ORDER BY distributor, sku""", {"k": tuple(keys), "d": distributor},
				as_dict=True):
			o = _offer(r)
			offers.setdefault(o.pop("product_key"), []).append(o)
			o.pop("brand_key")
		content = self._content([k for k in keys if ":" not in k])
		priorities = store.priority_map()
		hits = []
		for key in keys:
			group = offers.get(key)
			if not group:
				continue
			best = _best(group, priorities)
			lead, p = best or group[0], content.get(key)
			hits.append({"upc": lead["upc"],
				"manufacturer": p.manufacturer if p else lead["manufacturer"],
				"model": p.model if p else lead["model"],
				"description": p.description if p else lead["short_description"],
				"image_url": p.image_url if p else lead["image_url"],
				"ffl_required": bool(p.ffl_required) if p else any(o["ffl_required"] for o in group),
				"offers": group, "best_offer": best})
		return hits

	def health(self, distributor) -> dict:
		def last(*lanes):
			return frappe.db.sql("""SELECT MAX(finished_at) FROM dc_sync_run
				WHERE source IN %(s)s AND status = 'success'""",
				{"s": tuple(f"{distributor}_{ln}" for ln in lanes)})[0][0]

		catalog = last("catalog", "snapshot")
		total, active = frappe.db.sql(f"""SELECT COUNT(*), COALESCE(SUM(NOT discontinued AND {_LIVE}), 0)
			FROM dc_offer WHERE distributor = %s""", (distributor,))[0]
		return {"distributor": distributor, "contract_version": CONTRACT_VERSION,
			"last_catalog_sync_at": _iso(catalog),
			"last_quantity_sync_at": _iso(last("quantity")),
			"last_restrictions_sync_at": _iso(last("restrictions")),
			"catalog_version": _iso(catalog) or "",
			"offers_total": int(total or 0), "offers_active": int(active or 0)}

	def item_by_upc(self, upc):
		upc = store.normalize_upc(upc)[0] if upc else None
		if not upc:
			return None
		offers = [_offer(r) for r in frappe.db.sql(f"""SELECT {_SELECT} FROM dc_offer
			WHERE upc = %s ORDER BY distributor, sku""", (upc,), as_dict=True)]
		if not offers:
			return None
		for o in offers:
			o.pop("product_key"), o.pop("brand_key")
		best = _best(offers, store.priority_map())
		lead, p = best or offers[0], self._content([upc]).get(upc)
		return {"product": {"upc": upc,
				"manufacturer": p.manufacturer if p else lead["manufacturer"],
				"model": p.model if p else lead["model"],
				"description": p.description if p else lead["short_description"],
				"long_description": (p.long_description if p else lead["long_description"]) or "",
				"image_url": p.image_url if p else lead["image_url"],
				"ffl_required": bool(p.ffl_required) if p else any(o["ffl_required"] for o in offers),
				"content_source": (p.content_source if p else lead["distributor"]) or "",
				"content_locked": False},
			"offers": offers, "best_offer": best}

	def restrictions_entries(self, distributor, sku) -> list:
		row = frappe.db.sql("SELECT entries FROM dc_restriction WHERE distributor = %s AND sku = %s",
			(distributor, store.norm_sku(sku)))
		return json.loads(row[0][0]) if row else []

	# ---------------------------------------------------------------- products face
	def products_page(self, params) -> dict:
		"""``/products``: offers filtered, one product per key; ``sort=key`` pages
		by ``cursor``, brand / cost / qty by ``offset``; ``count`` on a first page."""
		where, args = self._where(params or {})
		sort, limit = (params or {}).get("sort") or "key", max(1, int((params or {}).get("limit") or 100))
		cursor, offset = (params or {}).get("cursor"), int((params or {}).get("offset") or 0)
		cond = " AND ".join(where)
		if sort == "key":
			page_cond = cond + (" AND product_key > %(cursor)s" if cursor else "")
			keys = [r[0] for r in frappe.db.sql(f"""SELECT product_key FROM dc_offer WHERE {page_cond}
				GROUP BY product_key ORDER BY product_key LIMIT %(limit)s""",
				dict(args, cursor=cursor, limit=limit))]
			next_cursor = keys[-1] if len(keys) == limit else None
		else:
			order = {"brand": "MIN(brand_key), product_key",
				"cost": "ISNULL(MIN(CASE WHEN cost > 0 THEN cost END)), "
					"MIN(CASE WHEN cost > 0 THEN cost END), product_key",
				"qty": f"MAX({store.ORDERABLE_QTY_SQL}) DESC, product_key"}.get(sort)
			if order is None:
				raise ValueError(f"unknown sort {sort!r}")
			keys = [r[0] for r in frappe.db.sql(f"""SELECT product_key FROM dc_offer WHERE {cond}
				GROUP BY product_key ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s""",
				dict(args, limit=limit, offset=offset))]
			next_cursor = None
		total = None
		if (params or {}).get("count") and not cursor and not offset:
			total = int(frappe.db.sql(f"SELECT COUNT(DISTINCT product_key) FROM dc_offer WHERE {cond}",
				args)[0][0])
		return {"products": self._assemble(keys), "cursor": next_cursor, "total": total}

	def products_batch(self, keys) -> dict:
		canonical = {}
		for raw in keys or []:
			k = str(raw or "").strip()
			if k:
				canonical[raw] = k if ":" in k else (store.normalize_upc(k)[0] or k)
		return {"products": self._assemble(sorted(set(canonical.values()))),
			"keys": {k: v for k, v in canonical.items() if k != v}}

	def products_changes(self, since) -> dict:
		"""Products whose offers changed after ``since`` (``rsr:12;ss:5``, one
		sequence per house), each once; ``changes_page_size`` rows per house per
		page. A Deleted offer names its product; so does a UPC move (the old key)."""
		pos = _parse_cursor(since)
		houses = sorted({r[0] for r in frappe.db.sql("SELECT distributor FROM dc_meta")} | set(pos))
		keys, more, n = [], False, int(self.changes_page_size)
		for house in houses:
			rows = frappe.db.sql("""(SELECT product_key, change_seq FROM dc_offer
					WHERE distributor = %(h)s AND change_seq > %(a)s ORDER BY change_seq LIMIT %(n)s)
				UNION ALL
				(SELECT product_key, change_seq FROM dc_product_move
					WHERE distributor = %(h)s AND change_seq > %(a)s ORDER BY change_seq LIMIT %(n)s)
				ORDER BY change_seq LIMIT %(n)s""", {"h": house, "a": pos.get(house, 0), "n": n + 1})
			more = more or len(rows) > n
			rows = rows[:n]
			keys += [r[0] for r in rows]
			if rows:
				pos[house] = int(rows[-1][1])
		return {"keys": list(dict.fromkeys(keys)), "cursor": _format_cursor(pos), "more": more}

	def products_facets(self, distributor=None) -> dict:
		cond = f"{_LIVE} AND NOT discontinued" + (" AND distributor = %(d)s" if distributor else "")
		args = {"d": distributor}
		spell: dict = {}
		for bk, name, n in frappe.db.sql(f"""SELECT brand_key, manufacturer, COUNT(*) FROM dc_offer
				WHERE {cond} AND brand_key != '' GROUP BY brand_key, manufacturer""", args):
			spell.setdefault(bk, []).append((-int(n), name))
		brands = [{"key": bk, "name": min(spell.get(bk) or [(0, bk)])[1], "products": int(c)}
			for bk, c in frappe.db.sql(f"""SELECT brand_key, COUNT(DISTINCT product_key) FROM dc_offer
				WHERE {cond} AND brand_key != '' GROUP BY brand_key ORDER BY brand_key""", args)]
		classes = [{"key": k, "products": int(c)} for k, c in frappe.db.sql(f"""SELECT product_class,
			COUNT(DISTINCT product_key) FROM dc_offer WHERE {cond} GROUP BY product_class
			ORDER BY product_class""", args)]
		departments = [{"key": f"{h}:{d}", "distributor": h, "department": d, "name": name or "",
				"products": int(c)}
			for h, d, name, c in frappe.db.sql(f"""SELECT distributor, department,
				MAX(department_name), COUNT(DISTINCT product_key) FROM dc_offer
				WHERE {cond} AND department != '' GROUP BY distributor, department
				ORDER BY distributor, department""", args)]
		return {"brands": brands, "classes": classes, "departments": departments,
			"categories": [], "platforms": []}

	def categories(self, since=None) -> dict:
		"""No curated tree ships with the local catalog: the category mirror finds
		nothing to do and stores keep their own Item Groups."""
		return {"categories": [], "nodes": [], "cursor": int(since or 0), "more": False}

	# ---------------------------------------------------------------- ops face
	def _feeds(self, distributor) -> list:
		from ffl_integrations.catalog_local.sync import load_drivers

		driver = load_drivers().get(distributor)
		if driver is not None:
			return [ln.name for ln in driver.lanes()]
		prefix = f"{distributor}_"
		return sorted({s[len(prefix):] for (s,) in frappe.db.sql(
			"SELECT DISTINCT source FROM dc_sync_run WHERE source LIKE %s", (_like_prefix(prefix),))})

	def ops_status(self, distributor, since=None) -> dict:
		from ffl_integrations.catalog_local.sync import DEFAULT_FEED_STALE_SECONDS, load_drivers

		driver = load_drivers().get(distributor)
		now = store.utc_now()
		feeds_named = self._feeds(distributor)
		lasts = dict(frappe.db.sql("""SELECT source, MAX(finished_at) FROM dc_sync_run
			WHERE source IN %(s)s AND status = 'success' GROUP BY source""",
			{"s": tuple(f"{distributor}_{f}" for f in feeds_named) or ("",)}))
		feeds = []
		for f in feeds_named:
			last = lasts.get(f"{distributor}_{f}")
			threshold = driver.stale_after(f) if driver else DEFAULT_FEED_STALE_SECONDS
			age = int((now - last).total_seconds()) if last else None
			stale, reason = ((True, "never_synced") if age is None
				else (True, "stale") if age >= threshold else (False, "fresh"))
			feeds.append({"feed": f, "last_success_at": _iso(last), "age_seconds": age,
				"threshold_seconds": threshold, "stale": stale, "reason": reason})
		total, active, products, changed, seq_max = frappe.db.sql(f"""SELECT COUNT(*),
				COALESCE(SUM(NOT discontinued AND {_LIVE}), 0), COUNT(DISTINCT upc),
				COALESCE(SUM(updated_at >= %(day)s), 0), MAX(change_seq)
			FROM dc_offer WHERE distributor = %(d)s""",
			{"d": distributor, "day": now - timedelta(hours=24)})[0]
		like = _like_prefix(f"{distributor}_")
		failures = frappe.db.sql("""SELECT source, finished_at, error_class FROM dc_sync_run
			WHERE source LIKE %s AND status != 'success' AND finished_at >= %s
			ORDER BY finished_at DESC, id DESC""", (like, now - timedelta(hours=24)))
		known = driver is not None or bool(total)
		return {"distributor": distributor, "known": known,
			"ingest_enabled": bool(driver is not None and _safe(driver.configured)),
			"server_time": _iso(now), "feeds": feeds,
			"offers_total": int(total or 0), "offers_active": int(active or 0),
			"products_total": int(products or 0), "changed_24h": int(changed or 0),
			"change_seq_max": int(seq_max) if seq_max is not None else None,
			"next_catalog_due_at": None, "failures_24h": len(failures),
			"last_failure": ({"source": failures[0][0], "at": _iso(failures[0][1]),
				"error_class": failures[0][2]} if failures else None),
			"cursor_lag": None if since is None else {"since": int(since),
				"offers_pending": int(frappe.db.sql("""SELECT COUNT(*) FROM dc_offer
					WHERE distributor = %s AND change_seq > %s""", (distributor, int(since)))[0][0])}}

	def ops_history(self, distributor, limit=20) -> dict:
		like = _like_prefix(f"{distributor}_")
		runs = [{"finished_at": _iso(fin), "source": src, "status": st, "rows_total": int(n or 0),
				"duration_seconds": int((fin - start).total_seconds()) if fin and start else None,
				"error_class": err or ""}
			for src, st, start, fin, n, err in frappe.db.sql("""SELECT source, status, started_at,
				finished_at, rows_total, error_class FROM dc_sync_run WHERE source LIKE %s
				ORDER BY finished_at DESC, id DESC LIMIT %s""", (like, int(limit)))]
		return {"distributor": distributor, "runs": runs}

	def ops_flags(self, distributor) -> dict:
		live = f"NOT discontinued AND {_LIVE}"
		(active, in_stock, discontinued, deleted, upc_have, orphan, map_zero, over_map,
			no_image) = (int(x or 0) for x in frappe.db.sql(f"""SELECT
				SUM({live}), SUM(qty > 0 AND {store.ORDERABLE_SQL}), SUM(discontinued),
				SUM({store.deleted_sql()}), SUM(upc IS NOT NULL), SUM(upc IS NULL),
				SUM({live} AND map = 0), SUM({live} AND map > 0 AND cost > map),
				SUM({live} AND image_url = '')
			FROM dc_offer WHERE distributor = %s""", (distributor,))[0])
		departments = [{"number": d, "name": name or d, "ffl_required": bool(ffl), "offers": int(n)}
			for d, name, ffl, n in frappe.db.sql(f"""SELECT department, MAX(department_name),
				MAX(ffl_required), COUNT(*) FROM dc_offer WHERE distributor = %s AND {live}
				GROUP BY department ORDER BY COUNT(*) DESC, department LIMIT 10""", (distributor,))]
		return {"distributor": distributor, "active": active, "in_stock": in_stock,
			"discontinued": discontinued, "deleted": deleted, "upc_have": upc_have,
			"upc_orphan": orphan, "flags": {"map_zero": map_zero, "cost_over_map": over_map,
				"no_image": no_image, "orphan_upc": orphan},
			"departments": departments}

	# ---------------------------------------------------------------- helpers
	@staticmethod
	def _content(upcs) -> dict:
		out = {}
		for chunk in _chunks(upcs):
			for r in frappe.db.sql("""SELECT upc, manufacturer, model, description, image_url,
					long_description, content_source, ffl_required FROM dc_product
					WHERE upc IN %(u)s""", {"u": chunk}, as_dict=True):
				out[r.upc] = r
		return out

	@staticmethod
	def _where(f: dict) -> tuple[list, dict]:
		"""Offer-level filters (a product is listed when one of its offers meets
		all of them), as the service and the stub define them."""
		where, args = [_LIVE], {}
		if not _truthy(f.get("include_discontinued")):
			where.append("NOT discontinued")
		if brands := _as_list(f.get("brand")):
			where.append("brand_key IN %(brand)s")
			args["brand"] = tuple(store.brand_key(b) for b in brands)
		if houses := _as_list(f.get("distributor")):
			where.append("distributor IN %(house)s")
			args["house"] = tuple(houses)
		if classes := _as_list(f.get("product_class")):
			where.append("product_class IN %(klass)s")
			args["klass"] = tuple(classes)
		if depts := [str(d).split(":", 1) for d in _as_list(f.get("department")) if ":" in str(d)]:
			where.append("(" + " OR ".join(f"(distributor = %(dh{n})s AND department = %(dd{n})s)"
				for n in range(len(depts))) + ")")
			for n, (h, d) in enumerate(depts):
				args[f"dh{n}"], args[f"dd{n}"] = h, d
		if f.get("min_cost") not in (None, ""):
			where.append("cost >= %(min_cost)s")
			args["min_cost"] = float(f["min_cost"])
		if f.get("max_cost") not in (None, ""):
			where.append("cost > 0 AND cost <= %(max_cost)s")
			args["max_cost"] = float(f["max_cost"])
		if _truthy(f.get("in_stock")):
			where.append(f"qty > 0 AND {store.ORDERABLE_SQL}")
		if f.get("ffl_required") not in (None, ""):
			where.append("ffl_required = %(ffl)s")
			args["ffl"] = 1 if _truthy(f["ffl_required"]) else 0
		q = str(f.get("q") or "").strip()
		if q:
			tokens = _tokens(q)
			like = " AND ".join(f"search_text LIKE %(qt{n})s" for n in range(len(tokens)))
			where.append("(upc = %(q_upc)s OR sku = %(q_sku)s OR mfg_number = %(q)s"
				+ (f" OR ({like}))" if like else ")"))
			args.update({"q": q, "q_sku": store.norm_sku(q), "q_upc": store.normalize_upc(q)[0] or q})
			args.update({f"qt{n}": f"%{t}%" for n, t in enumerate(tokens)})
		return where, args

	def _assemble(self, keys) -> list:
		"""Every live offer on these products with the merged content, in
		``keys`` order (unknown keys absent)."""
		if not keys:
			return []
		offers: dict = {}
		for chunk in _chunks(keys):
			for r in frappe.db.sql(f"""SELECT {_SELECT} FROM dc_offer WHERE product_key IN %(k)s
					AND {_LIVE} ORDER BY distributor, sku""", {"k": chunk}, as_dict=True):
				o = _offer(r)
				offers.setdefault(o["product_key"], []).append(o)
		content = self._content([k for k in keys if ":" not in k])
		priorities = store.priority_map()
		out = []
		for key in keys:
			group = offers.get(key)
			if not group:
				continue
			best = _best(group, priorities)
			lead, p = best or group[0], content.get(key)
			current = [o for o in group if not o["discontinued"]]
			maps = sorted((o["map"], priorities.get(o["distributor"], store.UNCONFIGURED_PRIORITY),
				o["distributor"]) for o in current if o["map"] > 0)
			out.append({"key": key, "upc": lead["upc"],
				"manufacturer": p.manufacturer if p else lead["manufacturer"],
				"model": p.model if p else lead["model"],
				"description": p.description if p else lead["short_description"],
				"long_description": (p.long_description if p else lead["long_description"]) or "",
				"content_optimized": False,
				"image_url": p.image_url if p else lead["image_url"],
				"ffl_required": bool(p.ffl_required) if p else any(o["ffl_required"] for o in group),
				"content_source": (p.content_source if p else lead["distributor"]) or None,
				"content_locked": False,
				"offers": group, "best_offer": best,
				"total_qty": sum(max(o["qty"], 0) for o in current),
				"map": maps[0][0] if maps else None, "map_source": maps[0][2] if maps else None,
				"category": None, "legal": _legal(group), "fits_platform": []})
		return out


def _like_prefix(prefix: str) -> str:
	"""A LIKE pattern matching strings that start with ``prefix`` literally
	(``_`` and ``%`` are wildcards otherwise: ``rsr_%`` would match ``rsr2_x``)."""
	return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _safe(fn) -> bool:
	try:
		return bool(fn())
	except Exception:  # noqa: BLE001
		return False


_CURSOR_PART = re.compile(r"[a-z0-9_-]+:[0-9]+", re.ASCII)


def _parse_cursor(token) -> dict:
	"""``rsr:120;ss:95`` → ``{house: seq}``. A malformed part is an error, never a
	silent restart from 0 (that would replay a whole house as changed)."""
	out = {}
	for part in filter(None, (p.strip() for p in str(token or "").split(";"))):
		if not _CURSOR_PART.fullmatch(part):
			raise ValueError(f"malformed cursor part {part!r}")
		house, seq = part.split(":")
		out[house] = int(seq)
	return out


def _format_cursor(pos: dict) -> str:
	return ";".join(f"{h}:{pos[h]}" for h in sorted(pos))
