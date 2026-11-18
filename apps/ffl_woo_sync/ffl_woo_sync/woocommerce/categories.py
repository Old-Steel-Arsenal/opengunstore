"""The stores' product categories follow the catalog master's tree.

Each tree node (``Catalog Category``, read through ``ffl_core.catalog_tree``)
is linked to one product category per store, by id: the stored id; else the
node's ``store_hint`` slug (the store's existing term, first contact only); else a
term of the node's name under the parent's term; else, applying, a new term. The
linked term then takes the node's name, parent and order — its slug is never
touched (URLs stay) and no term is ever deleted. What cannot be linked without
guessing (two terms of that name, a term another node holds, a same-named term
under another parent) is reported instead.

A product's category is its Item's node (``catalog_category`` for a catalog Item, else
its Item Group's node) → that node's term on the store; a node not on the store
yet files under its nearest parent that is. Product pushes never create terms —
the mirror does, and (applying) then moves the products already on the store
under a node it just *created* a term for out of the parent's category
(``recategorize_site``, a job). A term *linked* to a node (by store slug or name)
moves nothing by itself: on a store's first Apply every node links at once, and
its products are rolled out by hand (lead 2026-09-27) — ``recategorize_store``
(System Manager) dry run, review the counts, then ``dry_run=0``; ``under``
limits it to subtrees.

The catalog side (the tree feed, product changes, ``m3_resync``) lives in
``ffl_integrations.catalog_master`` and reaches this module only through the two
hooks registered in this app's hooks.py (``catalog_category_store_mirror``,
``catalog_category_recategorize``): the channel apps never import each other.
"""
from __future__ import annotations

import hashlib
import time

import frappe
from frappe.utils import cint

from ffl_core.utils import commit, single_flag

from ffl_woo_sync.woocommerce.client import SITES, assert_store_site, store_entry

_UNCATEGORIZED = "uncategorized"


FOLLOW_FIELD = "follow_category_tree"
# The default per store is ``client.STORES[site]["follow_tree_default"]`` (owner 2026-09-26: the
# retail store follows the tree). Same as the docfield default; ``patches/seed_follow_category_tree``
# writes the retail row so the settings form shows it ticked (a form save would otherwise store
# the missing Check as 0).


def follows_tree(site) -> bool:
	"""Whether the category mirror (and a push's category) touches this store —
	its WooCommerce Settings › Follow the catalog category tree."""
	store = store_entry(site)
	return single_flag(store["settings"], FOLLOW_FIELD, default=store["follow_tree_default"])


def configured_stores():
	"""``[(site, client)]`` for every connected store that follows the tree."""
	from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceConfigError

	out = []
	for site in SITES:
		if not follows_tree(site):
			continue
		try:
			out.append((site, WooCommerceClient.from_settings(site)))
		except WooCommerceConfigError:
			continue
	return out


def _terms(wc) -> dict:
	"""The store's categories by id, read once per client.
	# lazy: a push whose node has no linked term (report mode, or a node the mirror
	# could not link) lists the store's categories once per client (2-3 GETs); cache
	# per site in frappe.cache if that ever shows up in push latency."""
	cached = wc.__dict__.get("_catalog_terms")
	if cached is None:
		cached = {t["id"]: t for t in wc.list_categories()}
		wc.__dict__["_catalog_terms"] = cached
	return cached


def _fold(name) -> str:
	return " ".join(str(name or "").split()).casefold()


# ---- the mirror -------------------------------------------------------------------

def mirror_stores(*, apply: bool) -> dict:
	"""Hook ``catalog_category_store_mirror``: every configured store's categories
	against the tree, ``{site: report}``. A store that cannot be reached is
	reported, the others still run."""
	out = {site: dict(_empty_report(), skipped="off") for site in SITES if not follows_tree(site)}
	for site, wc in configured_stores():
		frappe.db.savepoint("catalog_category_store")
		try:
			out[site] = mirror_site(site, wc, apply=apply)
		except Exception as exc:  # noqa: BLE001 — one store failing never stops the other
			frappe.db.rollback(save_point="catalog_category_store")
			out[site] = dict(_empty_report(), issues=[{"issue": f"store unreachable: {exc}"}])
	return out


def _empty_report() -> dict:
	"""A store report with nothing in it — the shape :func:`mirror_site` returns."""
	return {"actions": [], "issues": [], "waiting": [], "twin_risk": [], "linked": {},
		"created": [], "unlinked_terms": []}


def on_store(site) -> list:
	"""``[(doctype, name, item code, product id)]``: every product on this store —
	Items (reconcile Pass 1, ``tasks.listed_item_filters``) and per-serial guns
	(Pass 2, ``tasks.listed_serial_filters``). Reconcile's Python-side
	skips apply too: no per-serial firearm Item (``is_per_serial_priced``)."""
	from ffl_woo_sync.woocommerce.sync import _f
	from ffl_woo_sync.woocommerce.tasks import listed_item_filters, listed_serial_filters

	from ffl_core.firearm import is_per_serial_priced

	pid = _f(site, "product_id")
	item_filters = listed_item_filters(site)
	# Pass 1 skips a per-serial firearm Item (its guns are the products; a stale
	# item-level id on it is not on sale) — so does this.
	out = [("Item", r.name, r.name, r[pid])
		for r in frappe.get_all("Item", filters=item_filters,
			fields=["name", pid, "is_firearm", "item_group", "uniform_sell_price", "variant_of"])
		# ...and a variant: its id is a variation, whose categories are its parent's.
		if not is_per_serial_priced(r) and not r.variant_of]
	serials = frappe.get_all("Serial No", filters=listed_serial_filters(site),
		fields=["name", "item_code", pid])
	out += [("Serial No", s.name, s.item_code, s[pid]) for s in serials]
	return out


def _item_nodes(codes) -> dict:
	"""``{item code: node}`` — :func:`item_category` for many Items in one read."""
	from ffl_core.catalog_tree import node_for_group

	codes = list({c for c in codes if c})
	if not codes:
		return {}
	by_group: dict = {}
	out = {}
	for code, slug, group in frappe.get_all("Item", filters={"name": ["in", codes]},
			fields=["name", "catalog_category", "item_group"], as_list=True):
		if not slug and group:
			if group not in by_group:
				by_group[group] = node_for_group(group)
			slug = by_group[group]
		if slug:
			out[code] = slug
	return out


def listed_nodes(site) -> set:
	"""The nodes with at least one product on this store in their subtree: every
	product :func:`on_store` lists, filed under its Item's node (``Catalog
	Category``, else its Item Group's), and all that node's ancestors.

	"On the store" is reconcile's own predicate, not a new one: a product a human
	took off the shop keeps its id but is not listed; a consigned-out or custody
	gun is not on sale; a sold gun's SOLD archive page (Pass 3a, status not
	Active) is a record, not stock — none of these gets a node its term."""
	from ffl_core.catalog_tree import lineage, parents

	parent_of = parents()
	out = set()
	for slug in set(_item_nodes({row[2] for row in on_store(site)}).values()):
		out.update(lineage(slug, parent_of))
	return out


def mirror_site(site, wc, *, apply: bool) -> dict:
	"""Link every node to a term on one store and (``apply``) make each linked term
	carry the node's name, parent and order; create the active nodes the store
	lacks that have a product listed on it in their subtree (owner 2026-09-26: no
	empty store categories — the others are ``waiting``). An inactive node is never
	linked or created (one already linked stays as it is). Returns ``{actions,
	issues, waiting, unlinked_terms, linked}`` — in report mode the actions are
	what applying would do; ``linked``: slug → term id as linked (or as it would
	be).

	A node is not created beside an unlinked store category of the same parent
	that holds products (likely the same category under another name — a twin
	would strand its products): that is reported, to be linked with a store slug
	in the workbench. A write the store refuses is that node's issue; the rest go
	on."""
	if not apply:
		return _mirror_site(site, wc, apply=False, hold=set())
	hold = set(mirror_site(site, wc, apply=False)["twin_risk"])
	report = _mirror_site(site, wc, apply=True, hold=hold)
	if report["created"]:
		# The products already on the store under a node that just got its term sit
		# in a parent's category (a push files under the nearest linked parent):
		# move them now, not at their next full push — Woo hides an empty category.
		# Created terms only (lead 2026-09-27): a first link by store slug or name
		# (every node, on a store's first Apply) moves nothing — that store's
		# products are rolled out through recategorize_store's dry run.
		_enqueue_recategorize(site, report["created"])
	return report


def _mirror_site(site, wc, *, apply: bool, hold: set) -> dict:
	from ffl_core.catalog_tree import nodes

	field = store_entry(site)["term_field"]
	terms = _terms(wc)
	by_slug = {t["slug"]: t for t in terms.values()}
	rows = nodes()
	claimed = {}
	for r in rows:
		tid = cint(r.get(field))
		if tid in terms:
			claimed.setdefault(tid, r.slug)
	linked: dict = {}   # slug -> term id, "new" (report: to be created) or None
	retired = set()     # unlinked inactive nodes and their unlinked descendants (rows come parents first)
	actions, issues, creates, waiting = [], [], [], []
	created = []        # applying: nodes whose term this run created
	has_products = listed_nodes(site)

	def act(r, what, detail):
		actions.append({"category": r.slug, "path": r.path or r.node_name, "action": what,
			"detail": detail})

	def issue(r, detail):
		issues.append({"category": r.slug, "path": r.path or r.node_name, "issue": detail})

	for r in rows:
		parent_tid = linked.get(r.parent_slug) if r.parent_slug else 0
		stored = cint(r.get(field))
		tid, via = None, None
		if stored and stored in terms:
			tid, via = stored, "id"
		elif stored:
			issue(r, f"store category #{stored} is gone — linking again")
			if apply:
				frappe.db.set_value("Catalog Category", r.slug, field, 0, update_modified=False)
		if tid is None and not r.active:
			linked[r.slug] = None
			retired.add(r.slug)
			continue   # retired: never linked or created on a store
		if tid is None and r.store_hint and r.store_hint in by_slug:
			tid, via = by_slug[r.store_hint]["id"], "store slug"
		if tid is None:
			same = [t for t in terms.values() if _fold(t["name"]) == _fold(r.node_name)]
			under = [t for t in same if isinstance(parent_tid, int) and t["parent"] == parent_tid]
			if len(under) > 1:
				issue(r, "two store categories of this name under its parent: "
					+ ", ".join(f"#{t['id']}" for t in under) + " — not linked")
				linked[r.slug] = None
				continue
			if under:
				tid, via = under[0]["id"], "name"
			elif same:
				issue(r, "a store category of this name sits under another parent: "
					+ ", ".join(f"#{t['id']} ({t['slug']})" for t in same)
					+ " — not linked; give the node that store slug in the workbench to link it")
				linked[r.slug] = None
				continue
		if tid is not None and claimed.get(tid, r.slug) != r.slug:
			issue(r, f"store category #{tid} ({terms[tid]['slug']}) is already linked to "
				f"{claimed[tid]} — not linked")
			linked[r.slug] = None
			continue
		if tid is None:
			linked[r.slug] = None
			if r.parent_slug in retired:
				retired.add(r.slug)
				continue   # under a retired node: not created, and not an issue
			# Products first: a node with none waits whatever its parent is — its
			# parent has none either (the set is closed over ancestors), so testing
			# the parent first would report a whole empty subtree as issues.
			if r.slug not in has_products:
				waiting.append({"category": r.slug, "path": r.path or r.node_name})
				continue   # created by the tick after its first product is listed
			if parent_tid is None:
				issue(r, "its parent is not on the store — not created")
				continue
			if r.slug in hold:
				issue(r, "not created: an unlinked store category of a like name with products "
					"sits under its parent — if it is this category, give the node its store slug "
					"in the workbench")
				continue
			act(r, "create", f"{r.node_name} under "
				+ (_term_label(terms, parent_tid) if parent_tid != "new" else "a new parent"))
			creates.append((r, parent_tid))
			if apply:
				try:
					tid = _create(wc, r.node_name, parent_tid, r.position)
				except Exception as exc:  # noqa: BLE001 — this node's issue, the rest go on
					issue(r, f"the store refused to create it: {str(exc)[:200]}")
					continue
				terms[tid] = {"id": tid, "name": r.node_name, "slug": "", "parent": parent_tid,
					"menu_order": cint(r.position), "count": 0}
				claimed[tid] = r.slug
				frappe.db.set_value("Catalog Category", r.slug, field, tid, update_modified=False)
				linked[r.slug] = tid
				created.append(r.slug)
			else:
				linked[r.slug] = "new"
			continue
		claimed[tid] = r.slug
		linked[r.slug] = tid
		term = terms[tid]
		if via != "id":
			act(r, "link", f"#{tid} {term['name']} ({term['slug']}) by {via}")
			if apply:
				frappe.db.set_value("Catalog Category", r.slug, field, tid, update_modified=False)
		if not r.active:
			continue  # retired: stays where it is, never renamed or deleted
		changes = {}
		if term["name"] != r.node_name:
			changes["name"] = r.node_name
		if isinstance(parent_tid, int) and term["parent"] != parent_tid:
			changes["parent"] = parent_tid
		if cint(term.get("menu_order")) != cint(r.position):
			changes["menu_order"] = cint(r.position)
		if parent_tid is None and r.parent_slug and r.parent_slug not in retired:
			issue(r, "its parent is not linked on the store — not moved")
		if changes:
			act(r, "update", _describe(terms, term, changes))
			if apply:
				try:
					wc.put(f"products/categories/{tid}", json=changes)
					term.update(changes)
				except Exception as exc:  # noqa: BLE001 — e.g. a name another term has
					issue(r, f"the store refused the update of #{tid}: {str(exc)[:200]}")
	# A new term beside an unlinked one of the same parent that holds products.
	twin_risk = []
	for r, parent_tid in creates:
		if not isinstance(parent_tid, int):
			continue
		near = [t for t in terms.values() if t["parent"] == parent_tid and t["id"] not in claimed
			and t["count"] and t["slug"] != _UNCATEGORIZED and _like(t["name"], r.node_name)]
		if near:
			twin_risk.append(r.slug)
			issue(r, "would be created beside an unlinked store category of a like name with products: "
				+ ", ".join(f"#{t['id']} {t['name']} ({t['slug']}, {t['count']})" for t in near)
				+ " — not created; if one is this category, give the node its store slug in the workbench")
	if twin_risk and not apply:
		actions[:] = [a for a in actions if not (a["action"] == "create" and a["category"] in twin_risk)]
	if apply:
		commit()
	return {"actions": actions, "issues": issues, "waiting": waiting, "twin_risk": twin_risk,
		"linked": {k: v for k, v in linked.items() if isinstance(v, int) and v},
		"created": created,
		"unlinked_terms": [
			{"id": t["id"], "name": t["name"], "slug": t["slug"],
				"parent": _term_label(terms, t["parent"]), "products": t["count"]}
			for t in sorted(terms.values(), key=lambda t: t["id"])
			if t["id"] not in claimed and t["slug"] != _UNCATEGORIZED]}


_NOT_A_NAME_WORD = frozenset({"other", "and", "the", "of", "for", "a"})
# lazy: -ves plurals of -f/-fe words by table (Gloves/Sleeves/Valves just drop the s); add a word when a store needs it
_SAME_WORD = {"ammo": "ammunition", "knives": "knife"}


def _stem(word) -> str:
	"""English plurals folded to one form: Accessories/Accessory, Batteries/Battery,
	Knives/Knife, Uppers/Upper, Gloves/Glove."""
	word = _SAME_WORD.get(word, word)
	if len(word) <= 3:
		return word
	if word.endswith("ies"):
		return word[:-3] + "y"
	return word[:-1] if word.endswith("s") else word


def _words(name) -> set:
	return {_stem(w) for w in _fold(name).replace("&", " ").replace("/", " ").split()} - _NOT_A_NAME_WORD


def _like(store_name, node_name) -> bool:
	"""A store category that may be this node under another name: they share a
	name word, plurals folded ("Uppers" ~ "Uppers & Conversion Kits", "Knives" ~
	"Knife", "Ammo" ~ "Ammunition"; "Service" is nothing like "Ammunition").
	Limits: whole words only — no synonyms beyond ``_SAME_WORD`` ("Optics" ~
	"Scopes" is missed), no irregular plurals, no prefixes ("Mag" ~ "Magazines"
	is missed); a miss lets the mirror create the node beside that store category."""
	return bool(_words(store_name) & _words(node_name))


def _create(wc, name, parent_tid, position) -> int:
	from ffl_woo_sync.woocommerce.client import WooCommerceError, _conflict_resource_id, _is_term_exists

	try:
		created = wc.post("products/categories", json={"name": name, "parent": parent_tid,
			"menu_order": cint(position)}) or {}
		return int(created["id"])
	except WooCommerceError as exc:
		# Two runs racing: the term is there — take the id the store hands back.
		if _is_term_exists(exc) and _conflict_resource_id(exc):
			return _conflict_resource_id(exc)
		raise


def _term_label(terms, tid) -> str:
	if not tid:
		return "the top level"
	t = terms.get(tid)
	return f"#{tid} {t['name']}" if t else f"#{tid}"


def _describe(terms, term, changes) -> str:
	parts = []
	if "name" in changes:
		parts.append(f"rename {term['name']} → {changes['name']}")
	if "parent" in changes:
		parts.append(f"move from {_term_label(terms, term['parent'])} to {_term_label(terms, changes['parent'])}")
	if "menu_order" in changes:
		parts.append(f"order {term.get('menu_order')} → {changes['menu_order']}")
	return f"#{term['id']} ({term['slug']}): " + "; ".join(parts)


# ---- products -------------------------------------------------------------------

def item_category(item_code) -> str | None:
	"""The tree node an Item's website product files under: the master's category
	for a catalog Item, else its Item Group's node."""
	from ffl_core.catalog_tree import node_for_group

	row = frappe.db.get_value("Item", item_code, ["catalog_category", "item_group"], as_dict=True)
	if not row:
		return None
	return row.catalog_category or node_for_group(row.item_group)


def term_for(wc, site, slug) -> int | None:
	"""The store's category id for a node: its linked term, else (the mirror has not
	linked it yet — report mode, or a node born since the last tick) the term the
	mirror would link, by the mirror's own rules (read only, conflicts included);
	walking up to the nearest parent on the store. Never creates a term."""
	from ffl_core.catalog_tree import node

	seen = set()
	while slug and slug not in seen:
		seen.add(slug)
		r = node(slug)
		if not r:
			return None
		tid = cint(r.get(store_entry(site)["term_field"])) or _would_link(wc, site).get(slug)
		if tid:
			return tid
		slug = r.parent_slug
	return None


def _would_link(wc, site) -> dict:
	"""slug → term id as a report-only mirror pass would link them; once per client.
	# lazy: in report mode (or for a node the mirror could not link) the first push
	# per client runs a whole report pass — the store's category list plus
	# listed_nodes' Item / Serial No scans; memoise per site in frappe.cache with a
	# short TTL if push latency ever shows it."""
	memo = wc.__dict__.setdefault("_catalog_would_link", {})
	if site not in memo:
		memo[site] = _mirror_site(site, wc, apply=False, hold=set())["linked"]
	return memo[site]


def recategorize(item_code, ctx=None) -> list:
	"""Hook ``catalog_category_recategorize``: point the Item's product on each store at
	its category — the category only (a full push would also rewrite the title,
	description and images staff curate in wp-admin). ``ctx``: a dict the caller
	keeps for one run, so the store clients (and their term lists) are built once.
	Each store on its own: one that refuses (or has no category for it) is logged
	and the product takes its category at its next push. Returns the sites written.

	Writes the tree category alone, so a non-tree category (Clearance, Open Box)
	on the product is dropped — as a full push does; only :func:`recategorize_site`
	keeps them (one rule for all three is still to be decided)."""
	from ffl_woo_sync.woocommerce import mapping
	from ffl_woo_sync.woocommerce.sync import _f

	slug = item_category(item_code)
	if not slug or mapping.variant_kind(item_code)[0] == mapping.VARIANT:
		return []  # a variation has no categories: its parent (the template) carries them
	ctx = {} if ctx is None else ctx
	stores = ctx.get("woo_stores")
	if stores is None:
		stores = ctx["woo_stores"] = configured_stores()
	done = []
	for site, wc in stores:
		product_id = frappe.db.get_value("Item", item_code, _f(site, "product_id"))
		if not product_id:
			continue
		try:
			tid = term_for(wc, site, slug)
			if not tid:
				frappe.log_error(title=f"Category not on the {site} store: {item_code}"[:140],
					message=f"{slug} has no category on the {site} store (nor any parent): "
						"the product keeps its categories. See the category mirror report.")
				continue
			wc.update_product(product_id, {"categories": [{"id": tid}]})
			done.append(site)
		except Exception:  # noqa: BLE001 — one store never stops the other
			frappe.log_error(title=f"Store category not updated ({site}): {item_code}"[:140],
				message=frappe.get_traceback())
	return done


# ---- the products already on a store ----------------------------------------------

SAMPLE = 50
MOVE_SAMPLE = 5
# Between two product PUTs of a catch-up run: ~4 writes a second keeps a full store
# (a few thousand products, minutes) under the host's rate limit. Tests set it to 0.
PACE_SECONDS = 0.25


def _store_categories(wc, product_ids) -> dict:
	"""``{product id: {category ids}}`` as the store has them, 100 products a GET.
	A product the store no longer has is left out, and so is an id that is not a
	number (one would make the store refuse the whole GET)."""
	ids = [str(p) for p in dict.fromkeys(product_ids) if str(p or "").isdigit()]
	out = {}
	for i in range(0, len(ids), 100):
		chunk = ids[i:i + 100]
		for row in wc.get("products", params={"include": ",".join(chunk), "per_page": 100,
				"status": "any", "_fields": "id,categories"}) or []:
			out[str(row["id"])] = {int(c["id"]) for c in row.get("categories") or []}
	return out


def _tree_terms(site, wc) -> set:
	"""The store's categories that belong to the tree — the terms nodes are linked
	to — plus Uncategorized (Woo's placeholder). Only these are ever replaced on a
	product; any other category on it (Clearance, Open Box …) stays."""
	field = store_entry(site)["term_field"]
	out = {cint(t) for t in frappe.get_all("Catalog Category", filters={field: [">", 0]}, pluck=field)}
	return out | {t["id"] for t in _terms(wc).values() if t["slug"] == _UNCATEGORIZED}


def store_drift(site, wc, *, under=None) -> dict:
	"""The products on this store (:func:`on_store`) whose tree categories
	(:func:`_tree_terms`) are not exactly their node's term (:func:`term_for`):
	e.g. filed under a parent before the node had its term. Categories outside the
	tree are not compared and are kept (lead 2026-09-27).

	``under``: only the products filed in these nodes' subtrees (``None``: the
	whole store; an empty list: nothing).

	Returns ``{checked, drift: [{doctype, name, item, product_id, category, now,
	want, set}], no_term, no_node, gone}`` (``set``: the categories to write — the
	node's term plus the product's categories outside the tree; ``no_term``:
	products whose node has no category on the store, nor any parent — they keep
	theirs; ``no_node``: products whose Item files under no node; ``gone``: products
	the store did not return — deleted there, or an id that is not a number).
	# lazy: every product is read first, then written; a wp-admin category edit made
	# in between (minutes, on a whole store) is overwritten with the categories read.
	# Read and write per 100-product chunk if that ever bites."""
	from ffl_core.catalog_tree import lineage, parents

	rows = on_store(site)
	slugs = _item_nodes({r[2] for r in rows})
	no_node = sum(1 for r in rows if not slugs.get(r[2]))
	rows = [r for r in rows if slugs.get(r[2])]
	if under is not None:
		roots, parent_of = set(under), parents()
		rows = [r for r in rows if roots.intersection(lineage(slugs[r[2]], parent_of))]
	now = _store_categories(wc, [r[3] for r in rows])
	want: dict = {}
	tree = _tree_terms(site, wc) if rows else set()
	drift, no_term, gone = [], 0, 0
	for doctype, name, item, pid in rows:
		if str(pid) not in now:
			gone += 1
			continue   # gone from the store: reconcile's business
		slug = slugs[item]
		if slug not in want:
			want[slug] = term_for(wc, site, slug)
		tid = want[slug]
		if not tid:
			no_term += 1
			continue
		cats = now[str(pid)]
		if cats & (tree | {tid}) == {tid}:
			continue
		drift.append({"doctype": doctype, "name": name, "item": item, "product_id": str(pid),
			"category": slug, "now": sorted(cats), "want": tid,
			"set": sorted((cats - tree - {tid}) | {tid})})
	return {"checked": len(rows), "drift": drift, "no_term": no_term, "no_node": no_node,
		"gone": gone, "tree": tree}


def _moves(site, wc, found) -> list:
	""":func:`store_drift`'s products counted by (doctype, tree categories now →
	the term they get), largest first, each with a few product ids."""
	terms = _terms(wc)
	groups: dict = {}
	for row in found["drift"]:
		now = tuple(t for t in row["now"] if t in found["tree"])
		key = (row["doctype"], now, row["want"])
		g = groups.setdefault(key, {"doctype": row["doctype"],
			"now": ", ".join(_term_label(terms, t) for t in now) or "(none in the tree)",
			"want": _term_label(terms, row["want"]), "count": 0, "sample": []})
		g["count"] += 1
		if len(g["sample"]) < MOVE_SAMPLE:
			g["sample"].append(row["product_id"])
	return sorted(groups.values(), key=lambda g: (-g["count"], g["doctype"], g["want"], g["now"]))


def _enqueue_recategorize(site, under=None):
	"""Queue :func:`recategorize_site` once: a second request for the same store and
	scope while one is queued or running is dropped (a double click, a tick beside
	a Settings-button Apply)."""
	scope = "all" if under is None else hashlib.sha1(
		",".join(sorted(set(under))).encode()).hexdigest()[:12]
	from ffl_core.catalog_tree import legacy_job_queued

	job_id = f"catalog_category_recategorize:{site}:{scope}"
	if legacy_job_queued(job_id):
		return
	frappe.enqueue("ffl_woo_sync.woocommerce.categories.recategorize_site", queue="long",
		timeout=3600, job_id=job_id, deduplicate=True, site=site, under=under, now=frappe.flags.in_test)


def recategorize_site(site, under=None) -> dict:
	"""Job (long queue): :func:`store_drift`'s products pointed at their node's term,
	keeping their categories outside the tree (category only, no other field). Paced
	(:data:`PACE_SECONDS`) and each PUT retried on a network error or 5xx
	(``sync._push_with_retry``); a product the store still refuses is logged and the
	rest go on, and a run with failures leaves one summary in the Error Log. A
	second run finds nothing to write, so re-running is the recovery (also after a
	timeout). A store that does not follow the tree is left alone. Returns ``{moved,
	failed}``.
	# lazy: _push_with_retry fails fast on 4xx, a 429 included — the pacing keeps
	# under the host's limit and a re-run picks up what a 429 refused; retry 429
	# there (for every push) if the Error Log shows them."""
	from ffl_woo_sync.woocommerce.sync import _push_with_retry

	wc = dict(configured_stores()).get(site)
	if wc is None:
		return {"moved": 0, "failed": 0, "skipped": "off"}
	moved = failed = 0
	for row in store_drift(site, wc, under=under)["drift"]:
		if moved or failed:
			time.sleep(PACE_SECONDS)
		try:
			_push_with_retry(lambda: wc.update_product(row["product_id"],
				{"categories": [{"id": t} for t in row["set"]]}))
			moved += 1
		except Exception:  # noqa: BLE001 — one product never stops the rest
			failed += 1
			frappe.log_error(title=f"Store category not updated ({site}): {row['name']}"[:140],
				message=frappe.get_traceback())
	if failed:
		frappe.log_error(title=f"Store categories: {failed} not updated ({site})"[:140],
			message=f"moved {moved}, failed {failed}, under {under or 'the whole store'}. "
				"The products are in the Error Log one by one; run recategorize_store again "
				"(it only writes what is still wrong).")
	return {"moved": moved, "failed": failed}


def _parse_under(under):
	"""``under`` from a request: a list, a JSON list or a comma-separated string of
	node slugs — every one must be a node. Empty = the whole store (``None``)."""
	if isinstance(under, str):
		under = under.strip()
		under = frappe.parse_json(under) if under.startswith("[") else under.split(",")
	under = [str(s).strip() for s in (under or []) if str(s).strip()]
	if not under:
		return None
	unknown = sorted(set(under) - set(frappe.get_all("Catalog Category",
		filters={"name": ["in", under]}, pluck="name")))
	if unknown:
		frappe.throw(f"Unknown categories: {', '.join(unknown)}.")
	return under


@frappe.whitelist()
def recategorize_store(site: str | None = None, dry_run: int = 1, under=None) -> dict:
	"""Put the products on the store(s) in their tree category: the products whose
	tree categories are not their node's term — e.g. filed under a parent before
	their category was created on the store, or every product on a store's first
	Apply (linking a term moves nothing by itself). Categories outside the tree
	(Clearance, Open Box …) are kept. Per-serial guns count as products and are
	counted apart (``by_doctype``).

	Limit: a product's full push (and the catalog feed's re-categorise) still
	writes its tree category alone, dropping Clearance / Open Box — only this
	catch-up keeps them.

	Args:
	    site: "retail" (the only store); empty = every store that follows the tree.
	    dry_run: 1 (default) reports only; 0 moves them in a background job
	        (category only, paced; a product the store refuses lands in the Error Log).
	    under: node slugs (a list, JSON list or comma-separated) — only the
	        products filed in their subtrees; empty = the whole store.

	Returns:
	    {site: {checked, to_move, by_doctype: {Item, Serial No}, moves: [{doctype,
	    now, want, count, sample: [product ids]}] (grouped by the tree categories
	    now → the one to get, largest first), no_term, no_node, gone, sample:
	    [{doctype, name, item, product_id, category, now, want, set}] (the first
	    50)}}; applying adds queued: true per store. A store that does not follow
	    the tree (or is not connected): {skipped: "off" | "not connected"}.

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	if site:
		assert_store_site(site)
	under = _parse_under(under)
	stores = dict(configured_stores())
	out = {}
	for s in ([site] if site else SITES):
		wc = stores.get(s)
		if wc is None:
			out[s] = {"skipped": "not connected" if follows_tree(s) else "off"}
			continue
		found = store_drift(s, wc, under=under)
		by_doctype = {"Item": 0, "Serial No": 0}
		for row in found["drift"]:
			by_doctype[row["doctype"]] += 1
		out[s] = {"checked": found["checked"], "to_move": len(found["drift"]),
			"by_doctype": by_doctype, "moves": _moves(s, wc, found),
			"no_term": found["no_term"], "no_node": found["no_node"], "gone": found["gone"],
			"sample": found["drift"][:SAMPLE]}
		if not cint(dry_run) and found["drift"]:
			_enqueue_recategorize(s, under)
			out[s]["queued"] = True
	return out
