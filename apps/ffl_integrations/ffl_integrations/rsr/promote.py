"""Promote an RSR Catalog Item to an Item the gun shop actually stocks.

Called from the Receive Goods page when the clerk picks an RSR row that
isn't yet in the local Item catalog. Copies fields, files the Item under the
catalog master's category (``catalog_master.classify`` — no department table
here), and writes the UPC into the Item Barcode child table so future scans hit
the local Item lookup without falling through to RSR.
"""
from __future__ import annotations

import json as _json

import frappe
from frappe import _
from frappe.utils import cint

from ffl_core.accessory_groups import (
	AMMO_RE_PISTOL as _AMMO_RE_PISTOL, AMMO_RE_RIFLE as _AMMO_RE_RIFLE,
	AMMO_RE_RIMFIRE as _AMMO_RE_RIMFIRE, AMMO_RE_SHOTGUN as _AMMO_RE_SHOTGUN, ammo_group,
)
from ffl_core.firearm import ensure_brand as _ensure_brand, ensure_caliber as _ensure_caliber
from ffl_core.roles import STOCK_ROLES
from ffl_integrations.distributor.util import legal_firearm


# RSR department → ATF Form 4473 Section B "Type" value. The dept alone
# can't distinguish Pistol-vs-Revolver or Rifle-vs-Shotgun, so we pick
# the more common default (Pistol / Rifle) and leave the operator to
# flip on the Item form if it's the rarer variant.
_FIREARM_TYPE_BY_DEPT = {
	"01": "Pistol",           # Handguns (default to Pistol; flip to Revolver as needed)
	"02": "Pistol",           # Used Handguns
	"03": "Rifle",            # Used Long Guns (default to Rifle; flip to Shotgun)
	"05": "Rifle",            # Sporting Long Guns
	"06": "Other Firearm",    # SOTS — NFA. Item Group = NFA carries the specific type
	"07": "Other Firearm",    # Black Powder Firearms (muzzleloaders)
	"41": "Receiver",         # Upper Receivers / Conv Kits
	"42": "Receiver",         # SBR Uppers
	"43": "Receiver",         # Upper/Conv Kits-High Cap
}

# Which RSR departments deliver pre-owned inventory (sets is_used=1).
_USED_DEPTS = {"02", "03"}


# The uppers / barrels / conversion-kit departments. RSR flags every row in them
# ffl_required — its own shipping rule — but most are gun parts under the law
# only a row the catalog service classes as a firearm
# (a serialized upper) is promoted as one.
_PARTS_FIREARM_DEPTS = {"41", "42", "43"}


def _firearm_type_from_dept(dept: str | None, row: dict | None = None) -> str | None:
	"""Best-effort guess for Item.firearm_type Select. Returns None for non-firearms —
	with the catalog row (``row``), any row that is no firearm under the law (an
	uppers-department gun part, a black-powder antique)."""
	dept = (dept or "").strip()
	if row is not None and not legal_firearm(row, "rsr"):
		return None
	return _FIREARM_TYPE_BY_DEPT.get(dept) or None


def _is_used_from_dept(dept: str | None) -> int:
	"""1 for Used Handguns / Used Long Guns; 0 otherwise. RSR sells new
	stock by default, but departments 02/03 are explicitly used inventory."""
	return 1 if (dept or "").strip() in _USED_DEPTS else 0


_RSR_VARIANT_RE = __import__("re").compile(r"_1(\.jpg|\.jpeg|\.png)$", __import__("re").IGNORECASE)


def _rsr_variant_urls(primary_url: str | None) -> list[tuple[str, int]]:
	"""Yield candidate RSR image URLs for variants 1/2/3.

	RSR's CDN follows the convention `<stock>_<n>.jpg` where n=1 is the
	primary photo and n=2/3 are additional angles. The catalog file only
	emits the primary URL, so we synthesize the others by swapping the
	`_1` suffix. Variants that don't exist return 404; the Item form's
	gallery hides broken thumbnails via onerror, so we don't HEAD-check.
	"""
	if not primary_url:
		return []
	m = _RSR_VARIANT_RE.search(primary_url)
	if not m:
		# image_name overrode the default — can't safely synthesize variants
		# (we'd risk replacing the wrong substring).
		return [(primary_url, 1)]
	base, ext = primary_url[: m.start()], m.group(1)
	return [(f"{base}_{n}{ext}", n) for n in (1, 2, 3)]


def _cdn_url_exists(url: str) -> bool:
	"""HEAD-probe an RSR CDN URL. The _2/_3 gallery variants routinely don't
	exist, and WooCommerce sideloads every image server-side on product
	create — one 404 variant fails the whole push with a 400
	(woocommerce_product_image_upload_error; first hit on the 2026-08-01 dev
	E2E). The Item form hid dead variants client-side via onerror, which is
	why this check didn't exist before the Woo path went live.

	True in tests (no network); a transient probe failure drops the variant
	— the gallery is cosmetic, a broken push is not."""
	import frappe

	if getattr(frappe.flags, "in_test", False):
		return True
	try:
		import requests

		return requests.head(url, allow_redirects=True, timeout=4).status_code == 200
	except Exception:
		return False


def _checked_image(rsr) -> str | None:
	"""The main Item.image, only when the CDN actually serves it — a dead
	main image fails a Woo product create the same way a dead variant does."""
	url = rsr.get("image_url") or None
	return url if (url and _cdn_url_exists(url)) else None


def _append_rsr_image_variants(doc, primary_url: str | None) -> bool:
	"""Append RSR _1/_2/_3 image URLs to `doc.image_gallery`, skipping URLs
	already present and URLs the CDN doesn't actually serve (see
	`_cdn_url_exists`). Returns True if at least one row was added.

	First row gets is_primary=1 only when the gallery was empty before we
	started, so existing primary rows aren't demoted by a later backfill.
	"""
	if not primary_url:
		return False
	existing = {(row.image or "") for row in (doc.get("image_gallery") or [])}
	added = False
	for url, idx in _rsr_variant_urls(primary_url):
		if url in existing:
			continue
		if not _cdn_url_exists(url):
			continue
		doc.append("image_gallery", {
			"image": url,
			"is_primary": 1 if idx == 1 and not existing else 0,
			"sort_order": idx,
			"caption": f"RSR image {idx}",
		})
		added = True
	return added


# Generic category words that appear in the ammo regexes for sub-category
# routing — they shouldn't be returned as a caliber on a firearm Item.
_NOT_A_CALIBER = frozenset({
	"RIFLE", "PISTOL", "HANDGUN", "SHOTGUN", "SHOTSHELL", "SHOT SHELL",
	"RIMFIRE", "BUCKSHOT", "BIRDSHOT", "SLUG", "GAUGE",
})


def _extract_caliber(text: str | None) -> str | None:
	"""Run the firearm-caliber regexes against `text` (typically the RSR
	long_description) and return a normalized caliber string, or None.

	RSR's 77-column feed has no structured caliber field — the caliber
	lives inside the product description text (e.g. "RUGER SEC-9 9MM 4\""
	or "REM 700 308 WIN BL/SYN 24"). We reuse the same regexes already
	defined for ammo sub-category routing — the same regex that finds
	"9MM" in an ammo box's name also finds it in a pistol's name.

	Order matters: shotgun gauges and rimfire calibers are distinct from
	pistol/rifle so they go first. Pistol before rifle because pistol
	calibers are more specific (less likely to false-match on a rifle
	description). Skip matches that are generic category words ("RIFLE",
	"PISTOL", etc.) — those are alternatives in the regex for category
	routing, not actual calibers.
	"""
	if not text:
		return None
	for rgx in (_AMMO_RE_SHOTGUN, _AMMO_RE_RIMFIRE, _AMMO_RE_PISTOL, _AMMO_RE_RIFLE):
		# Try each non-generic match in this regex — first occurrence wins.
		# re.finditer iterates left-to-right; skip generic-word hits and
		# keep looking for a real caliber on the same regex.
		for m in rgx.finditer(text):
			raw = " ".join(m.group(0).upper().split())
			if raw in _NOT_A_CALIBER:
				continue
			return raw
	return None


# Departments where caliber extraction should run. The shotgun regex contains
# substrings like "410" / "12GA" / "SLUG" that bleed into rifle/pistol product
# names if we run it indiscriminately. Restrict to non-firearm-accessory depts.
_CALIBER_BEARING_DEPTS = frozenset({
	"01", "02", "03", "05", "06", "07",  # firearms (handgun / long gun / NFA / black powder)
	"18",                                 # ammo
	"41", "42", "43",                     # uppers / receivers
})


def _caliber_from_rsr(rsr: dict) -> str | None:
	"""Resolve caliber for an RSR item.

	Prefer the structured `caliber` field (loaded from RSR's official
	attributes-all.txt, field 21) — it's authoritative. Fall back to the
	description regex (gated by department to avoid bleeding shotgun
	"410"/"SLUG" matches into unrelated SKUs) for items whose attributes
	feed hasn't populated a caliber yet.
	"""
	structured = (rsr.get("caliber") or "").strip()
	if structured:
		return structured
	if (rsr.get("department_number") or "").strip() not in _CALIBER_BEARING_DEPTS:
		return None
	for key in ("long_description", "short_description"):
		c = _extract_caliber(rsr.get(key))
		if c:
			return c
	return None


def _ammo_subcategory(rsr: dict) -> str | None:
	"""The ammunition leaf of an RSR ammunition row (department 18) — Pistol / Rifle /
	Shotgun / Rimfire Ammo — from its model and descriptions, or None (the caller
	keeps the parent "Ammunition"). Used by the legacy group migrator."""
	if (rsr.get("department_number") or "").strip() != "18":
		return None
	return ammo_group(" ".join([(rsr.get("model") or ""), (rsr.get("short_description") or ""),
		(rsr.get("long_description") or "")]).strip())


def _map_offer_row(r: dict) -> dict:
	"""Remote OfferFull (the shape `remote_catalog.rows` / `cached_row` /
	`item_by_upc`'s offers all share) → the LEGACY `RSR Catalog Item` column
	names the rest of this module was written against. One mapping, reused by
	every remote read path below."""
	return {
		"rsr_stock_number": r["sku"], "upc": r["upc"] or "",
		"manufacturer": r["manufacturer"], "model": r["model"],
		"mfg_number": r["mfg_number"], "short_description": r["short_description"],
		"long_description": r["long_description"],
		"department_number": r["department"], "msrp": float(r["msrp"] or 0),
		"map_price": float(r["map"] or 0), "dealer_price": float(r["cost"] or 0),
		"image_url": r["image_url"], "image_url_hires": r["image_url_hires"],
		"ffl_required": bool(r["ffl_required"]), "caliber": r["caliber"],
		"product_class": r.get("product_class") or "",
	}


def _fetch_rsr_row(rsr_stock_number: str) -> dict | None:
	"""One RSR catalog row by stock number, via the remote catalog (Data Service),
	mapped to the legacy key names. None on miss."""
	from ffl_integrations.distributor import remote_catalog
	r = remote_catalog.cached_row("rsr", (rsr_stock_number or "").strip())
	if not r:
		return None
	return _map_offer_row(r)


@frappe.whitelist()
def promote_to_item(payload) -> dict:
	"""Build + insert an Item from an RSR Catalog Item.

	Args:
	    payload: JSON string from JS, or dict from Python:
	        {
	          "rsr_stock_number": "GLK19GEN5",
	          "item_code": "GLK19GEN5",     # optional — defaults to rsr.upc, else rsr_stock_number
	          "item_name": "...",           # optional
	          "item_group": "Barrels",      # optional override; default: the catalog master's category
	          "has_serial_no": 1,           # optional override; default heuristic
	          "standard_rate": 649.99,      # optional; defaults to MSRP
	          "distributor": "Sports South", # optional; a non-RSR house promotes the
	                                        # SKU through its own provider
	        }

	Returns:
	    {item_code, item_name, item_group, has_serial_no, is_firearm,
	    last_rate, stock_uom, found: True} — same shape as
	    ffl_core.api.receive_goods.lookup_item / create_item.
	"""
	from ffl_integrations.data_service import CATALOG, require

	frappe.only_for(STOCK_ROLES)
	require(CATALOG)
	# A scan-first UPC lookup offers every house's row, tagged with its Distributor:
	# a non-RSR SKU promotes through its own house, not the RSR catalog.
	if isinstance(payload, str):
		try:
			payload = _json.loads(payload)
		except _json.JSONDecodeError:
			frappe.throw(_("Invalid payload (not JSON)"))
	distributor = isinstance(payload, dict) and payload.get("distributor")
	if distributor:
		from ffl_integrations.distributor.api import get_provider

		provider = get_provider(distributor)
		if provider.code != "rsr":
			opts = {k: v for k, v in payload.items() if k not in ("rsr_stock_number", "distributor")}
			return provider.promote_to_item(payload.get("rsr_stock_number"), opts)
	return _promote_to_item(payload)


def _promote_to_item(payload) -> dict:
	"""Gate-free body of promote_to_item. The RSR provider adapter
	(rsr_provider.promote_to_item) calls THIS from ensure_dropship_item
	(counter Create Order) and the listing pipeline (Item/Purchase/Console
	roles) — none of which hold STOCK_ROLES, so they must not hit the HTTP
	wrapper's gate. Direct HTTP promotion keeps the STOCK gate above."""
	from ffl_core.firearm import is_firearm_item

	if isinstance(payload, str):
		try:
			payload = _json.loads(payload)
		except _json.JSONDecodeError:
			frappe.throw(_("Invalid payload (not JSON)"))
	if not isinstance(payload, dict):
		frappe.throw(_("Invalid payload"))

	rsr_stock_number = (payload.get("rsr_stock_number") or "").strip()
	if not rsr_stock_number:
		frappe.throw(_("rsr_stock_number is required."))

	rsr = _fetch_rsr_row(rsr_stock_number)
	if not rsr:
		frappe.throw(_("RSR Catalog Item '{0}' not found.").format(rsr_stock_number))

	# Identity chain: explicit override > UPC > stock number. UPC-keyed by
	# default so Woo SKU == item_code == UPC; orphans (no UPC) fall back to the
	# RSR stock number. `Distributor Listing.sku` keeps the stock number
	# regardless of which branch wins, and router.py's `sku = listing.sku or
	# item_code` bridges the two — zero generic-layer change needed.
	item_code = (payload.get("item_code") or rsr.get("upc") or rsr_stock_number).strip()
	if not payload.get("item_code") and not frappe.db.exists("Item", item_code):
		item_code = barcode_owner(rsr.get("upc")) or item_code
	if frappe.db.exists("Item", item_code):
		# Idempotent: re-promote returns the existing item.
		_ensure_barcode(item_code, rsr.get("upc"))
		return _item_response(item_code)

	# The catalog master files it (its category's Item Group, the law's answer on
	# firearms); the title / class rules only when the master has no answer.
	from ffl_integrations.catalog_master import classify
	dept = (rsr.get("department_number") or "").strip()
	filed = classify(rsr, "rsr")
	item_group = (payload.get("item_group") or filed.item_group or "").strip()
	firearm_type = _firearm_type_from_dept(dept, rsr) if filed.is_firearm else None

	if filed.is_firearm:
		# EVERY firearm is serial-tracked, used included (depts 02/03) — whatever
		# the dialog sent (the 2026-09-16 suppressor promoted without serials).
		has_serial_no = 1
	else:
		has_serial_no = 1 if cint(payload.get("has_serial_no")) else 0

	item_name = (payload.get("item_name") or rsr.get("short_description") or rsr_stock_number).strip()
	# Item.standard_rate is the SELLING-side default consumed by POS / Selling Settings.
	# Prefer MAP (RSR-enforced minimum advertised price) over MSRP when MAP is set;
	# fall back to MSRP otherwise. Override via payload always wins.
	selling_default = rsr.get("map_price") or rsr.get("msrp") or 0
	standard_rate = float(payload.get("standard_rate") or selling_default)

	mfg_name = rsr.get("manufacturer")
	brand = _ensure_brand(mfg_name) if mfg_name else None
	caliber = _ensure_caliber(_caliber_from_rsr(rsr))

	doc = frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": item_name,
		"item_group": item_group,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"has_serial_no": has_serial_no,
		"standard_rate": standard_rate or None,
		"description": rsr.get("long_description") or rsr.get("short_description") or None,
		"brand": brand,
		"image": _checked_image(rsr),
		# Custom firearm-detail fields (see ffl_core/fixtures/custom_field.json).
		# manufacturer is a Link → Brand (despite the field name); mirrors `brand`.
		"manufacturer": brand,
		"model_name": (rsr.get("model") or "").strip() or None,
		"mpn": (rsr.get("mfg_number") or "").strip() or None,
		"uniform_sell_price": 1 if cint(payload.get("uniform_sell_price") or 0) else 0,
		"caliber": caliber,
		"firearm_type": firearm_type,
		"is_firearm": 1 if filed.is_firearm else 0,
		"catalog_category": filed.category,
		"is_used": _is_used_from_dept(dept),
		"dealer_price": float(rsr.get("dealer_price") or 0) or None,
		"map_price": float(rsr.get("map_price") or 0) or None,
		"msrp": float(rsr.get("msrp") or 0) or None,
		"listing_status": "Draft",
	})
	if has_serial_no and not doc.get("serial_no_series"):
		doc.serial_no_series = f"{item_code}-.####"
	if rsr.get("upc"):
		doc.append("barcodes", {"barcode": rsr["upc"], "barcode_type": "UPC"})
	# Seed image gallery with _1/_2/_3 variants; the CDN 404s missing ones
	# and the Item form's onerror hides them, so we don't HEAD-check here.
	_append_rsr_image_variants(doc, rsr.get("image_url"))
	doc.flags.ignore_permissions = True
	doc.insert()

	return _item_response(item_code)


def barcode_owner(upc) -> str | None:
	"""The Item already carrying this UPC as a barcode — one named otherwise (by an
	older stock number, by hand) is still that product: promoting it again would
	fail on the duplicate barcode. None for a blank UPC."""
	upc = (upc or "").strip()
	return frappe.db.get_value("Item Barcode",
		{"barcode": upc, "parenttype": "Item"}, "parent") if upc else None


def _ensure_barcode(item_code: str, barcode: str | None) -> None:
	"""Append the RSR UPC to an existing Item if it isn't already there."""
	if not barcode:
		return
	exists = frappe.db.exists("Item Barcode", {"parent": item_code, "barcode": barcode})
	if not exists:
		item = frappe.get_doc("Item", item_code)
		item.append("barcodes", {"barcode": barcode, "barcode_type": "UPC"})
		item.flags.ignore_permissions = True
		item.save()


def _item_response(item_code: str) -> dict:
	from ffl_core.firearm import is_firearm_item
	doc = frappe.db.get_value(
		"Item", item_code,
		["item_code", "item_name", "item_group", "has_serial_no", "stock_uom",
		 "standard_rate", "last_purchase_rate", "uniform_sell_price",
		 "manufacturer", "model_name", "caliber", "firearm_type"],
		as_dict=True,
	) or {}
	resolved_code = doc.get("item_code") or item_code
	# Cost prefill priority: real purchase history → RSR dealer price → 0.
	# Never fall through to standard_rate — that's the selling price (MAP/MSRP).
	last_rate = float(doc.get("last_purchase_rate") or rsr_dealer_price(resolved_code) or 0)
	return {
		"found": True,
		"item_code": resolved_code,
		"item_name": doc.get("item_name") or item_code,
		"item_group": doc.get("item_group"),
		"has_serial_no": bool(doc.get("has_serial_no")),
		"is_firearm": is_firearm_item(resolved_code),
		"last_rate": last_rate,
		"stock_uom": doc.get("stock_uom") or "Nos",
		"uniform_sell_price": bool(doc.get("uniform_sell_price")),
		"standard_rate": float(doc.get("standard_rate") or 0),
		"manufacturer": doc.get("manufacturer"),
		"model_name": doc.get("model_name"),
		"caliber": doc.get("caliber"),
		"firearm_type": doc.get("firearm_type"),
		# No importer: the receive card must start it blank — it is a per-gun
		# marking, never an Item-level default (2026-08-20, matches
		# receive_goods._firearm_fields).
	}


@frappe.whitelist()
def migrate_legacy_item_groups() -> dict:
	"""One-shot migrator for the Item Group restructure.

	Moves Items out of the legacy flat groups ("New Firearm", "Used Firearm",
	parent "Ammunition") into the type-based leaf groups, and sets `is_used`
	from the old group name. Idempotent — safe to re-run.

	Returns:
	    {firearm_moved, ammo_moved, total}.
	"""
	frappe.only_for(STOCK_ROLES)
	firearm_moved = 0
	ammo_moved = 0

	# Firearms: pick leaf group from firearm_type via the canonical mapping
	# in ffl_core.firearm._TYPE_TO_GROUP (handles both ATF Form-4473 values
	# and legacy aliases like "Handgun" / "Long Gun" / "Other").
	from ffl_core.firearm import _TYPE_TO_GROUP
	rows = frappe.db.sql(
		"""
		SELECT name, item_group, firearm_type
		FROM `tabItem`
		WHERE item_group IN ('New Firearm', 'Used Firearm')
		""", as_dict=True)
	for r in rows:
		ft = (r.get("firearm_type") or "").strip()
		new_group = _TYPE_TO_GROUP.get(ft)
		updates = {"is_used": 1 if r["item_group"] == "Used Firearm" else 0}
		if new_group and frappe.db.exists("Item Group", new_group):
			updates["item_group"] = new_group
		frappe.db.set_value("Item", r["name"], updates, update_modified=False)
		firearm_moved += 1

	# Ammunition: re-classify items under the parent "Ammunition" using the
	# remote catalog row's description if available. Local Item rows first,
	# then one remote_catalog.rows batch for their codes, joined in Python —
	# no more SQL JOIN against the now-retired local RSR Catalog Item table.
	from ffl_integrations.distributor import remote_catalog

	item_rows = frappe.db.sql(
		"SELECT name FROM `tabItem` WHERE item_group = 'Ammunition'", as_dict=True)
	codes = [r["name"] for r in item_rows]
	remote_rows = remote_catalog.rows("rsr", codes) if codes else {}
	for r in item_rows:
		raw = remote_rows.get(r["name"])
		if not raw:
			continue
		mapped = _map_offer_row(raw)
		if not mapped.get("short_description") and not mapped.get("long_description"):
			continue
		leaf = _ammo_subcategory(mapped)
		if leaf and frappe.db.exists("Item Group", leaf):
			frappe.db.set_value("Item", r["name"], "item_group", leaf, update_modified=False)
			ammo_moved += 1

	frappe.db.commit()
	return {"firearm_moved": firearm_moved, "ammo_moved": ammo_moved, "total": firearm_moved + ammo_moved}


@frappe.whitelist()
def backfill_from_rsr(item_codes: list[str] | str | None = None) -> dict:
	"""Update RSR-linked Items in place with firearm details that promote_to_item
	now writes (manufacturer, model_name, firearm_type, dealer_price, image,
	image_gallery primary). Skips fields the user has already set non-empty.

	Args:
	    item_codes: Optional list or comma-separated string — only those
	        Items are touched. Otherwise scans a local-only candidate net —
	        every Item with `dropship_distributor` set, plus every Item
	        carrying any barcode — and resolves each through
	        `_rsr_for_item` (remote); non-RSR candidates simply come back
	        None and count as skipped.

	Returns:
	    {updated, skipped, total}.
	"""
	from ffl_integrations.data_service import CATALOG, require

	frappe.only_for(STOCK_ROLES)
	require(CATALOG)
	if isinstance(item_codes, str):
		item_codes = [c.strip() for c in item_codes.split(",") if c.strip()]
	targets: set[str] = set(item_codes or [])

	if not targets:
		# Local-only candidate net (no RSR Catalog Item join — that table is no
		# longer the source of truth): Items explicitly linked to a distributor
		# for drop-ship, unioned with every Item carrying ANY barcode. Both are
		# coarse supersets on purpose — `_rsr_for_item` below is what actually
		# confirms an RSR link; a false-positive candidate just resolves to
		# None and counts as skipped, at the cost of one extra remote lookup.
		distributor_linked = frappe.db.sql(
			"SELECT name FROM `tabItem` WHERE ifnull(dropship_distributor, '') != ''",
			as_dict=True)
		barcoded = frappe.db.sql(
			"SELECT DISTINCT parent AS name FROM `tabItem Barcode`", as_dict=True)
		targets.update(d["name"] for d in (distributor_linked + barcoded))

	updated = 0
	skipped = 0
	for code in targets:
		try:
			if _backfill_one(code):
				updated += 1
			else:
				skipped += 1
		except Exception as e:
			frappe.log_error(title=f"RSR backfill failed for {code}", message=str(e))
			skipped += 1
	frappe.db.commit()
	return {"updated": updated, "skipped": skipped, "total": len(targets)}


def _backfill_one(item_code: str) -> bool:
	"""Apply RSR fields to a single Item. Returns True if anything changed."""
	rsr = _rsr_for_item(item_code)
	if not rsr:
		return False
	doc = frappe.get_doc("Item", item_code)
	dirty = False

	def _set_if_empty(field: str, value):
		nonlocal dirty
		if value in (None, "", 0):
			return
		current = doc.get(field)
		if current in (None, "", 0):
			doc.set(field, value)
			dirty = True

	mfg = (rsr.get("manufacturer") or "").strip()
	brand = _ensure_brand(mfg) if mfg else None
	_set_if_empty("brand", brand)
	_set_if_empty("manufacturer", brand)
	_set_if_empty("model_name", (rsr.get("model") or "").strip() or None)
	_set_if_empty("mpn", (rsr.get("mfg_number") or "").strip() or None)
	# Skip the regex + DB roundtrip when caliber is already set — backfill
	# runs across thousands of items, and _ensure_caliber + _caliber_from_rsr
	# aren't free.
	if not doc.get("caliber"):
		_set_if_empty("caliber", _ensure_caliber(_caliber_from_rsr(rsr)))
	_set_if_empty("firearm_type", _firearm_type_from_dept(rsr.get("department_number"), rsr))
	_set_if_empty("dealer_price", float(rsr.get("dealer_price") or 0) or None)
	_set_if_empty("map_price", float(rsr.get("map_price") or 0) or None)
	_set_if_empty("msrp", float(rsr.get("msrp") or 0) or None)
	_set_if_empty("image", rsr.get("image_url"))
	_set_if_empty("description", rsr.get("long_description") or rsr.get("short_description"))
	# is_used: RSR depts 02/03 mark items as Used. Only fill if currently empty/0
	# AND we have a positive signal (don't accidentally flip user-entered Used items).
	dept_used = _is_used_from_dept(rsr.get("department_number"))
	if dept_used and not int(doc.get("is_used") or 0):
		doc.is_used = 1
		dirty = True
	if not doc.get("listing_status"):
		doc.listing_status = "Draft"
		dirty = True

	# Backfill image gallery with variant URLs; pre-existing rows are kept.
	if _append_rsr_image_variants(doc, rsr.get("image_url")):
		dirty = True

	# Ensure UPC is in barcode child table for future scans.
	if rsr.get("upc"):
		exists = any(b.barcode == rsr["upc"] for b in (doc.get("barcodes") or []))
		if not exists:
			doc.append("barcodes", {"barcode": rsr["upc"], "barcode_type": "UPC"})
			dirty = True

	if dirty:
		doc.flags.ignore_permissions = True
		doc.save()
	return dirty


def _rsr_for_item(item_code: str) -> dict | None:
	"""Find the RSR catalog row linked to an Item: by item_code first (matches
	rsr_stock_number directly — true for orphans and any explicit-item_code
	promote), else by each Item Barcode UPC (the common case post-migration,
	where item_code IS the UPC and the stock number only lives in the remote
	catalog / Distributor Listing.sku)."""
	row = _fetch_rsr_row(item_code)
	if row:
		return row
	from ffl_integrations.distributor import remote_catalog
	barcodes = frappe.db.sql("SELECT barcode FROM `tabItem Barcode` WHERE parent=%s LIMIT 5",
		(item_code,), as_dict=True)
	for r in barcodes:
		bc = (r.get("barcode") or "").strip()
		if not bc:
			continue
		hit = remote_catalog.item_by_upc(bc)
		if not hit:
			continue
		for offer in hit.get("offers") or []:
			if offer.get("distributor") == "rsr":
				return _map_offer_row(offer)
	return None


@frappe.whitelist()
def lookup_for_item_form(scan: str) -> dict:
	"""Resolve a UPC / RSR stock # / mfg # to a field map ready for the Item form.

	Used by the client script on the Item form (public/js/item_form.js).
	Does NOT insert anything — the Item form stays in 'unsaved' state and the
	user can still tweak fields before save.

	Args:
	    scan: UPC / RSR stock number / manufacturer part number.

	Returns:
		{found: True, defaults: {item_code, item_name, ...}, rsr: {...}}
		on hit, or {found: False, scan: ...} on miss.
	"""
	# The Item form's RSR scan-to-fill helper — used WHILE creating/editing
	# an Item, so gate on Item WRITE (covers Item Manager, whom a fixed
	# STOCK_ROLES list wrongly locked out of their own form; r2 code M3).
	from ffl_integrations.data_service import CATALOG, require

	frappe.has_permission("Item", "write", throw=True)
	require(CATALOG)
	scan = (scan or "").strip()
	if not scan:
		return {"found": False}
	from ffl_integrations.distributor import remote_catalog
	# Remote text search (stock #, UPC, mfg #, description all indexed
	# server-side) — take the first hit, then re-fetch the FULL row so the
	# defaults dict below has every field (a search hit is a thin summary
	# shape). Note: Deleted rows no longer surface here — acceptable, they are
	# unorderable anyway.
	hits = remote_catalog.search("rsr", scan, limit=1)
	if not hits:
		return {"found": False, "scan": scan}
	rsr = _fetch_rsr_row(hits[0]["best_offer"]["sku"])
	if not rsr:
		return {"found": False, "scan": scan}

	dept = (rsr.get("department_number") or "").strip()
	from ffl_integrations.catalog_master import classify
	filed = classify(rsr, "rsr")
	mfg_name = (rsr.get("manufacturer") or "").strip() or None
	brand = _ensure_brand(mfg_name) if mfg_name else None
	firearm_type = _firearm_type_from_dept(dept, rsr) if filed.is_firearm else None

	defaults = {
		# UPC-first, matching promote_to_item's own identity chain (Task 4) — a
		# stock-number default here would let the Item form insert an Item under a
		# DIFFERENT name than promote_to_item would give the exact same catalog row.
		"item_code": rsr.get("upc") or rsr["rsr_stock_number"],
		"item_name": (rsr.get("short_description") or rsr["rsr_stock_number"]).strip(),
		"item_group": filed.item_group,
		"is_firearm": 1 if filed.is_firearm else 0,
		"catalog_category": filed.category,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		# Every firearm is serial-tracked, used included — same rule as _promote_to_item.
		"has_serial_no": 1 if filed.is_firearm else 0,
		"standard_rate": float(rsr.get("map_price") or rsr.get("msrp") or 0) or None,
		"description": rsr.get("long_description") or rsr.get("short_description") or None,
		"brand": brand,
		"image": _checked_image(rsr),
		"manufacturer": brand,
		"model_name": (rsr.get("model") or "").strip() or None,
		"caliber": _ensure_caliber(_caliber_from_rsr(rsr)),
		"firearm_type": firearm_type,
		"is_used": _is_used_from_dept(dept),
		"dealer_price": float(rsr.get("dealer_price") or 0) or None,
		"map_price": float(rsr.get("map_price") or 0) or None,
		"msrp": float(rsr.get("msrp") or 0) or None,
		"listing_status": "Draft",
		"upc": rsr.get("upc") or None,
		"mpn": (rsr.get("mfg_number") or "").strip() or None,
	}
	return {
		"found": True,
		"defaults": defaults,
		"rsr": {
			"rsr_stock_number": rsr["rsr_stock_number"],
			"manufacturer": rsr.get("manufacturer"),
			"model": rsr.get("model"),
			"department_number": dept,
			"msrp": float(rsr.get("msrp") or 0),
			"map_price": float(rsr.get("map_price") or 0),
			"dealer_price": float(rsr.get("dealer_price") or 0),
			"image_url": rsr.get("image_url"),
			"image_url_hires": rsr.get("image_url_hires"),
			"ffl_required": int(rsr.get("ffl_required") or 0),
			"is_firearm": bool(filed.is_firearm),
			"category": filed.category,
		},
	}


def rsr_dealer_price(item_code: str) -> float:
	"""Return RSR dealer_price for an Item if it can be linked to a catalog row,
	via the same item_code-then-UPC match `_rsr_for_item` uses.

	Never raises on API failure: the Data Service being unreachable degrades this to
	a plain 0.0 (manual cost entry) rather than blocking a receive.
	"""
	if not item_code:
		return 0.0
	from ffl_integrations.distributor.remote_catalog import DataServiceUnavailable
	try:
		rsr = _rsr_for_item(item_code)
	except DataServiceUnavailable:
		return 0.0
	return float(rsr.get("dealer_price") or 0) if rsr else 0.0
