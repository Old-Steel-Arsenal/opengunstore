"""One-click FastBound bound-book importer.

Pulls every Item from FastBound's bound book and idempotently mirrors it into
ERPNext as:
1. an `Item` (group = Used Firearm) with firearm metadata,
2. an `FFL Acquisition` record (submitted) tagged with the FastBound id, and
3. a `Stock Entry` (Material Receipt) putting one unit on hand at the default
   warehouse — so the firearm shows up in stock balance immediately.

Idempotency: the FastBound item id is stored on the FFL Acquisition's
`fastbound_id`; re-runs detect it and skip. Partial failures don't poison
the batch — each item is upserted in its own transaction and errors are
collected for the report.

Run via the whitelisted entry-point so the UI can call it:
    @frappe.whitelist()
    ffl_integrations.fastbound.importer.import_bound_book
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ffl_core.roles import SYSTEM_ROLES

try:
	import barcodenumber
except ImportError:  # pragma: no cover — ships with ERPNext, guard anyway
	barcodenumber = None

from ffl_core.utils import commit as safe_commit
from ffl_core.firearm import (
	ensure_brand as _ensure_brand,
	ensure_caliber as _ensure_caliber,
	is_antique_item,
	is_firearm_item,
	issue_serial,
	serialized_receipt_row,
	used_firearm_item_doc,
)

from ffl_integrations.fastbound import normalize as _normalize
from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfigError,
	FastBoundError,
)


DEFAULT_ITEM_CODE_PREFIX = "UF-FB-"


@dataclass
class ImportStats:
	fetched: int = 0
	created: int = 0
	updated: int = 0           # already-imported guns re-synced from FastBound
	skipped: int = 0
	disposed: int = 0          # skipped as not-in-stock (disposed/deleted in FastBound)
	errors: list[dict] = field(default_factory=list)
	corrected: list[dict] = field(default_factory=list)  # structural fixes applied (serial rename / item move)
	regroup: list[dict] = field(default_factory=list)    # couldn't auto-move (not a clean in-stock import) → manual
	projected: dict = field(default_factory=dict)   # dry-run only: item_code → grouping

	def as_dict(self) -> dict:
		return {
			"fetched": self.fetched,
			"created": self.created,
			"updated": self.updated,
			"skipped": self.skipped,
			"disposed_skipped": self.disposed,
			"errors": self.errors[:50],  # cap to keep frontend payload sane
			"error_count": len(self.errors),
			"corrected": self.corrected,
			"regroup_needed": self.regroup,
			"projected_items": len(self.projected),
			"projected": self.projected,
		}


AGGREGATE_AUTO = "auto"     # SKU > UPC > MPN > mfg+model+caliber slug > fb_id
AGGREGATE_MODEL = "model"   # model+caliber slug only (maker-agnostic); maker → Serial No
_VALID_AGGREGATE = {AGGREGATE_AUTO, AGGREGATE_MODEL}


@frappe.whitelist()
def import_bound_book(
	page_size: int = 100,
	dry_run: int = 0,
	commit_each: int = 1,
	aggregate_by: str = AGGREGATE_AUTO,
	skip_disposed: int = 1,
) -> dict:
	"""Import / re-sync the FastBound bound book into ERPNext.

	Re-running is a SYNC: already-imported guns (matched by FastBound id) are re-synced
	from FastBound, which stays authoritative. A corrected maker/importer re-stamps the
	Serial No; a corrected serial renames it (stock + A&D follow); a corrected
	model/caliber re-keys the gun onto the right Item (reverse + re-create). Every
	structural fix is listed in `corrected`; a gun that can't be safely auto-moved
	(sold/transferred — not a clean in-stock import) is listed in `regroup_needed`.

	Args:
	    page_size: FastBound page size per fetch (default 100).
	    dry_run: 1 writes nothing and returns a `projected` map
	        (item_code → count / makers / serials) to preview the grouping
	        before committing.
	    commit_each: 1 (default, production) commits after each successful
	        item so a crash mid-import doesn't lose work; 0 (tests) relies
	        on the caller to manage transactions.
	    aggregate_by: How FB items group into ERPNext Items —
	        "auto" (default): SKU → UPC → MPN → model-slug → fb_id
	        fallback; "model": the model+caliber slug ONLY — SKU/UPC/MPN
	        and manufacturer are ignored — so the same model from different
	        manufacturers/importers (surplus M1 Garands, Mausers, …) folds
	        into one Item; each gun's true maker + importer is stamped on
	        its Serial No. Spelling variants are canonicalised (see
	        fastbound.normalize).
	    skip_disposed: 1 (default) imports only CURRENT INVENTORY — guns
	        FastBound marks disposed (sold) or deleted are skipped (the
	        importer creates acquisitions + receipts, not dispositions, so
	        importing a sold gun would wrongly re-stock it). Pass 0 to
	        import the entire historical book.

	Returns:
	    Import stats dict (created/synced/corrected/regroup_needed counts;
	    plus `projected` on a dry run).
	"""
	frappe.only_for(SYSTEM_ROLES)
	dry = bool(int(dry_run))
	commit = bool(int(commit_each))
	skip_disp = bool(int(skip_disposed))
	mode = (aggregate_by or AGGREGATE_AUTO).strip().lower()
	if mode not in _VALID_AGGREGATE:
		frappe.throw(_("Invalid aggregate_by mode: {0}").format(mode))

	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError as exc:
		frappe.throw(_("FastBound is not configured: {0}").format(exc))

	stats = ImportStats()
	dry_items: list[dict] = []
	# Resolve the receipt warehouse ONCE — it's constant for the whole run, so
	# re-running the POS-Profile / Warehouse lookup per gun is pure waste.
	warehouse = None
	if not dry:
		company = frappe.defaults.get_user_default("Company") or _first_company()
		warehouse = _default_warehouse(company)
	for page in client.iter_items(page_size=int(page_size)):
		for fb_item in page:
			stats.fetched += 1
			if skip_disp and (fb_item.get("disposed") or fb_item.get("deleted")):
				stats.disposed += 1
				continue
			try:
				if dry:
					dry_items.append(fb_item)
					stats.skipped += 1
					continue
				outcome = _upsert_one(fb_item, aggregate_by=mode, stats=stats, warehouse=warehouse)
				if outcome == "created":
					stats.created += 1
				elif outcome == "updated":
					stats.updated += 1
				else:
					stats.skipped += 1
				if commit:
					frappe.db.commit()
			except Exception as exc:
				stats.errors.append({
					"serial": fb_item.get("serial"),
					"fastbound_id": fb_item.get("id"),
					"error": str(exc)[:300],
				})
				if commit:
					frappe.db.rollback()

	if dry:
		stats.projected = _preview_grouping(dry_items, aggregate_by=mode)
	elif commit:
		_record_run(stats)

	return stats.as_dict()


def _preview_grouping(items: list[dict], *, aggregate_by: str = AGGREGATE_AUTO) -> dict:
	"""Group fb items by the Item code they'd land on — WITHOUT writing anything.

	The dry-run projection: returns {item_code: {"count", "makers", "serials"}}
	using the very same _item_code_for the real import uses, so the preview can't
	drift from the actual grouping. (Disposed items are filtered out upstream.)
	"""
	out: dict[str, dict] = {}
	for it in items:
		code = _item_code_for(it, aggregate_by=aggregate_by)
		entry = out.setdefault(code, {"count": 0, "makers": [], "serials": []})
		entry["count"] += 1
		maker = _normalize.canon_manufacturer((it.get("manufacturer") or "").strip())
		if maker and maker not in entry["makers"]:
			entry["makers"].append(maker)
		serial = (it.get("serial") or "").strip()
		if serial:
			entry["serials"].append(serial)
	return out


# ---------------------------------------------------------------- internals

def _upsert_one(fb_item: dict[str, Any], *, aggregate_by: str = AGGREGATE_AUTO,
		stats: ImportStats | None = None, warehouse: str | None = None) -> str:
	"""Mirror one FastBound item; returns 'created', 'updated', or 'skipped'.

	Does NOT commit — the caller (`import_bound_book`) decides the transaction
	boundary. In production we commit after each item so partial imports survive
	a crash; in tests we skip commit so FrappeTestCase rollback works.
	"""
	fb_id = (fb_item.get("id") or "").strip()
	if not fb_id:
		raise ValueError("FastBound item is missing 'id'")

	# Already imported? Re-sync it instead of skipping — FastBound is authoritative,
	# so a later correction (maker/importer, serial typo, model/caliber) must reach POS.
	if frappe.db.exists("FFL Acquisition", {"fastbound_id": fb_id}):
		return _resync_existing(fb_item, aggregate_by=aggregate_by, stats=stats)

	_create_from_scratch(fb_item, aggregate_by=aggregate_by, warehouse=warehouse)
	return "created"


def _create_from_scratch(fb_item: dict[str, Any], *, aggregate_by: str,
		warehouse: str | None = None) -> str:
	"""Item (created if absent) + FFL Acquisition + serialized stock receipt for one
	gun. The aggregation key (SKU > UPC > MPN > model-slug > fb-id, or model+caliber
	in model mode) decides which Item it rolls into."""
	item_code = _item_code_for(fb_item, aggregate_by=aggregate_by)
	_ensure_item(item_code, fb_item, aggregate_by=aggregate_by)
	_create_acquisition(item_code, fb_item)
	_create_stock_receipt(item_code, fb_item, warehouse=warehouse)
	return item_code


def _resync_existing(fb_item: dict[str, Any], *, aggregate_by: str,
		stats: ImportStats | None = None) -> str:
	"""Re-sync an already-imported gun from the CURRENT FastBound record so a FB
	correction reaches POS. The gun is located by its FastBound id (stable across a
	serial edit). Returns 'updated', or 'skipped' if it can't be located.

	This is an ATTRIBUTE reconciliation, never a structural move:
	- maker / importer / model / caliber changed → re-stamp the Serial No in place
	  (FastBound is authoritative; these are per-unit fields, no stock movement).
	- serial changed → rename the Serial No (keeps stock + A&D) and carry the new
	  serial onto its FFL Acquisition.
	- model / caliber now maps to a DIFFERENT Item (the gun is mis-bucketed) → it is
	  ONLY reported (regroup_needed) for a human to reassign. The sync NEVER moves a
	  gun between Items itself — that's the manual reassign's job. Auto-moving would
	  mean reverse+recreate, which is where per-unit data (price, photos, …) is lost.

	That last rule is about THIS path, not a house rule: FastBound inferring a new
	bucket from a corrected model is a guess, and which listing a gun belongs to is
	a merchandising call. The Woo webhook does move guns automatically, but only
	along a pairing an Item states outright (`Item.sells_through_item`), and via
	`reassign_serial_to_item`, which is lossless — the reverse+recreate this rule
	was written against is long gone.
	"""
	fb_id = (fb_item.get("id") or "").strip()
	fields = ["name", "item_code"]
	sn = frappe.db.get_value("Serial No", {"fastbound_item_id": fb_id}, fields, as_dict=True)
	if not sn:
		# Older import without the id stamped — fall back to the FastBound serial.
		fb_serial = (fb_item.get("serial") or "").strip()
		if fb_serial:
			sn = frappe.db.get_value("Serial No", fb_serial, fields, as_dict=True)
	if not sn:
		return "skipped"

	cur_serial, cur_item = sn.name, sn.item_code
	new_serial = (fb_item.get("serial") or "").strip()

	# Serial correction → rename in place; keeps stock + A&D intact (non-destructive).
	if new_serial and new_serial != cur_serial:
		if frappe.db.exists("Serial No", new_serial):
			# The corrected serial already names a DIFFERENT gun locally — a real
			# conflict we can't auto-resolve. Surface it for a human.
			if stats is not None:
				stats.regroup.append({"serial": cur_serial, "from": cur_item, "to": cur_item,
					"reason": "corrected serial {0} already exists".format(new_serial)})
		else:
			frappe.rename_doc("Serial No", cur_serial, new_serial, force=True)
			for acq in frappe.get_all("FFL Acquisition", filters={"serial": cur_serial}, pluck="name"):
				frappe.db.set_value("FFL Acquisition", acq, "serial", new_serial, update_modified=False)
			if stats is not None:
				stats.corrected.append({"serial": new_serial, "change": "renamed",
					"from": cur_serial, "to": new_serial})
			cur_serial = new_serial

	# Align this gun's per-unit facts (maker / importer / model / caliber) to
	# FastBound — pure field writes, no stock movement, no data loss.
	_stamp_serial_attrs(cur_serial, fb_item)

	# Advisory only: if FB's (now-aligned) model/caliber maps to a DIFFERENT Item
	# than where the gun sits, it's mis-bucketed. Report it so a human can reassign;
	# the sync deliberately never moves a gun between Items itself.
	new_item = _item_code_for(fb_item, aggregate_by=aggregate_by)
	if new_item and cur_item and new_item != cur_item and stats is not None:
		stats.regroup.append({"serial": cur_serial, "from": cur_item, "to": new_item,
			"reason": "model/caliber now maps to {0}; reassign if desired".format(new_item)})

	return "updated"


def _safe_to_rekey(serial: str, status: str | None = None) -> bool:
	"""Auto-reverse only a clean import: the gun is in stock (Active) and carries no
	FFL Disposition (not sold / transferred). Anything else, a human resolves.
	Pass `status` to reuse an already-fetched value instead of re-querying."""
	if status is None:
		status = frappe.db.get_value("Serial No", serial, "status")
	if status != "Active":
		return False
	return not frappe.db.exists("FFL Disposition", {"serial": serial, "docstatus": ("<", 2)})


@frappe.whitelist()
def reassign_serial_to_item(serial: str, target_item: str) -> dict:
	"""Move one in-stock firearm serial from its current Item to `target_item`,
	keeping the SAME Serial No and FFL Acquisition records — so the gun's price,
	photos, condition, maker/importer/model/caliber, A&D history and FastBound
	linkage all survive (nothing is deleted, so nothing can be dropped).

	ERPNext ties a serial to one Item, so the stock moves with two ledger legs:
	issue the serial out of the old Item, flip the serial's item_code, then receive
	it into the target. The A&D record's `item` is re-pointed in place. FastBound is
	NOT touched — reassign is a POS-internal re-bucketing; the bound book only changes
	on an explicit serial edit. A per-gun WooCommerce listing, where the gun has one,
	IS delisted: it belonged to the old Item, and leaving it live sells the same rifle
	twice. Whitelisted for the Serial No form's "Reassign Item" action, for resolving
	the sync's `regroup_needed` list, and for the Woo webhook's declared cross-Item
	pick (`ffl_woo_sync.woocommerce.tasks.rebucket_and_apply_serial_task`).

	Args:
	    serial: In-stock firearm Serial No name.
	    target_item: Destination firearm Item code.

	Returns:
	    {ok, serial, from, to, fastbound_item_id}.
	"""
	frappe.has_permission("Serial No", "write", throw=True)
	serial = (serial or "").strip()
	target_item = (target_item or "").strip()
	if not serial:
		frappe.throw(_("No serial number provided."))

	sn = frappe.db.get_value(
		"Serial No", serial,
		["item_code", "warehouse", "status", "purchase_rate", "fastbound_item_id"], as_dict=True)
	if not sn:
		# Confirm existence before has_permission — the permission machinery can
		# raise DoesNotExistError on a missing name, masking this clean message.
		frappe.throw(_("Serial No {0} not found.").format(serial))
	frappe.has_permission("Serial No", "write", doc=serial, throw=True)
	if not target_item or not frappe.db.exists("Item", target_item):
		frappe.throw(_("Target item {0} not found.").format(target_item))
	if sn.item_code == target_item:
		frappe.throw(_("Serial {0} is already under {1}.").format(serial, target_item))
	if not is_firearm_item(target_item):
		frappe.throw(_("{0} is not a firearm Item.").format(target_item))
	# A serial already in the bound book (a FastBound id, or a submitted FFL
	# Acquisition) can't be re-bucketed under an antique (off-book) Item — that
	# would orphan a live A&D entry under a GCA-exempt SKU. block_antique_on_booked_item
	# can't catch this (is_antique doesn't change on reassign), so guard here.
	if is_antique_item(target_item) and (
		(sn.fastbound_item_id or "").strip()
		or frappe.db.exists("FFL Acquisition", {"serial": serial, "docstatus": 1})
	):
		frappe.throw(_("Cannot reassign {0} to antique Item {1}: the serial is in the bound book. "
			"Antique items must have no bound-book entries.").format(serial, target_item))
	if not _safe_to_rekey(serial, status=sn.status):
		frappe.throw(_("Cannot reassign {0} — it isn't a clean in-stock unit (sold or transferred).").format(serial))

	old_item, warehouse = sn.item_code, sn.warehouse
	# Two-leg stock move, SAME Serial No throughout. The item_code flip is a deliberate
	# raw write that must sit BETWEEN the legs: the issue bundle validates the serial
	# against the old Item, then the receipt leg re-activates it under the target (so a
	# Serial No validate() / via_stock_ledger save() isn't needed). The Serial No row is
	# never deleted, so its price / photos / condition / maker / model carry across.
	issue = issue_serial(old_item, serial, warehouse)
	frappe.db.set_value("Serial No", serial, "item_code", target_item, update_modified=False)
	# In at what it went out at — the ledger's valuation, not Serial No.purchase_rate. The
	# two can differ (a cost typed on the Serial No form after the receipt), and receiving at
	# purchase_rate revalues the gun into Stock Adjustment (229580, 2026-10-08: out at 1512,
	# in at 1050.63).
	value = frappe.db.get_value("Stock Entry Detail", {"parent": issue}, "valuation_rate")
	_create_stock_receipt(
		target_item, {"serial": serial, "cost": value or sn.purchase_rate or 0}, warehouse=warehouse)
	# The receipt may restamp purchase_rate from its rate; the gun keeps its own figure.
	frappe.db.set_value("Serial No", serial, "purchase_rate", sn.purchase_rate, update_modified=False)

	# A&D record: re-point its Item in place (no cancel/recreate) — keeps the record's
	# identity, timestamp and fastbound_id; a raw write, so no FastBound re-push fires.
	for acq in frappe.get_all(
			"FFL Acquisition", filters={"serial": serial, "docstatus": 1}, pluck="name"):
		frappe.db.set_value("FFL Acquisition", acq, "item", target_item, update_modified=False)

	# A per-gun WooCommerce product belongs to the gun's OLD Item. Left live, the
	# gun is now sellable TWICE: on its own product page, and through the target
	# Item's listing, which counts it in stock from this moment. Nothing else
	# delists it — the outbound push is driven by the Item, and the Item no longer
	# knows about this product. Item-level listings are untouched: drafting one
	# would pull the whole model off the storefront.
	# Best-effort, and last on purpose: a Woo-side problem must never undo a stock
	# move that already landed (same stance as receive_bridge's auto-hang).
	try:
		if (frappe.db.get_value("Serial No", serial, "woo_product_id") or "").strip():
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.delist_serial_task",
				queue="short", enqueue_after_commit=True,
				serial_no=serial, site="retail")
	except Exception:  # noqa: BLE001 — ffl_woo_sync absent, or its fields not migrated
		frappe.log_error(
			title=f"Reassign could not delist the per-gun listing for {serial}",
			message=frappe.get_traceback())

	safe_commit()
	return {"ok": True, "serial": serial, "from": old_item, "to": target_item,
		"fastbound_item_id": sn.fastbound_item_id}


def _ensure_item(item_code: str, fb_item: dict[str, Any], *, aggregate_by: str = AGGREGATE_AUTO) -> str:
	"""Create the ERPNext Item `item_code` (group = Used Firearm) if absent."""
	manufacturer = (fb_item.get("manufacturer") or "").strip()
	model = (fb_item.get("model") or "").strip()
	caliber_name = (fb_item.get("caliber") or "").strip()
	firearm_type = _map_firearm_type(fb_item.get("type"))

	if frappe.db.exists("Item", item_code):
		return item_code

	caliber_link = _ensure_caliber(caliber_name) if caliber_name else None
	# In model mode the Item is maker-agnostic — one model+caliber = one Item across
	# manufacturers/importers — so it carries no Item-level manufacturer and names
	# itself by model + caliber. Each gun's true maker is stamped on its Serial No.
	if aggregate_by == AGGREGATE_MODEL:
		item_brand = None
		item_name = " ".join(p for p in (model, caliber_name) if p).strip() or item_code
	else:
		item_brand = _ensure_brand(manufacturer) if manufacturer else None
		item_name = " ".join(p for p in (manufacturer, model) if p).strip() or item_code

	# No per-Item firearm_serial on aggregated SKUs — the per-gun serial
	# lives on the Serial No record stamped at Stock Entry submit.
	doc = frappe.get_doc(used_firearm_item_doc(
		item_code=item_code,
		item_name=item_name,
		description=_describe(fb_item),
		manufacturer=item_brand,
		model_name=model,
		caliber=caliber_link,
		firearm_type=firearm_type,
	))
	# sync_upc_barcode (Item validate hook) mirrors upc into the Item Barcode
	# table. Skip a malformed UPC — ERPNext rejects it and would fail the
	# whole item's import.
	upc = (fb_item.get("upc") or "").strip()
	if upc and _is_valid_upca(upc):
		doc.upc = upc
	doc.flags.ignore_permissions = True
	doc.insert()
	return item_code


def _is_valid_upca(code: str) -> bool:
	if not (code.isdigit() and len(code) == 12):
		return False
	if barcodenumber is None:
		return True  # lib unavailable → shape check stands
	return bool(barcodenumber.check_code("upca", code))


def _item_code_for(fb_item: dict[str, Any], *, aggregate_by: str = AGGREGATE_AUTO) -> str:
	"""Resolve the Item code N guns roll up into. Priority (auto mode):
	SKU > UPC > MPN > manufacturer+model+caliber slug > UF-FB-<id> fallback.
	In "model" mode, SKU/UPC/MPN and the *manufacturer* are skipped — the key is
	the model+caliber slug — so the same model from different manufacturers /
	importers rolls into ONE Item (each gun's true maker + importer is kept on its
	Serial No). Caliber stays in the key so e.g. a "1903" pistol and a "1903" rifle
	don't collide.
	"""
	if aggregate_by == AGGREGATE_AUTO:
		for key in ("sku", "upc", "mpn"):
			raw = (fb_item.get(key) or "").strip()
			if raw:
				return raw[:140]
		slug = _slug_from_attrs(
			fb_item.get("manufacturer"),
			fb_item.get("model"),
			fb_item.get("caliber"),
		)
	else:  # AGGREGATE_MODEL — model + caliber only (manufacturer excluded), each
		# canonicalised so spelling variants (45 AUTO/45ACP, M1911A1/M1911 A1,
		# U.S. Rifle M1/M1) fold together. See fastbound.normalize.
		slug = "-".join(p for p in (
			_normalize.canon_model(fb_item.get("model")),
			_normalize.canon_caliber(fb_item.get("caliber")),
		) if p)
	if slug:
		return slug
	fb_id = fb_item["id"]
	return f"{DEFAULT_ITEM_CODE_PREFIX}{fb_id[:12]}"


def _slug_from_attrs(*parts: str | None) -> str:
	"""Uppercase alnum + dashes; ignores "Unknown" / empty parts."""
	chunks: list[str] = []
	for raw in parts:
		txt = (raw or "").strip()
		if not txt or txt.lower() == "unknown":
			continue
		slug = "".join(c if c.isalnum() else "-" for c in txt.upper())
		slug = "-".join(s for s in slug.split("-") if s)
		if slug:
			chunks.append(slug)
	return "-".join(chunks)[:140]


def _map_firearm_type(fb_type: str | None) -> str | None:
	"""FastBound `type` strings → our firearm_type vocabulary.

	Maps the more specific FastBound classifications (revolver vs pistol,
	rifle vs shotgun, the NFA classes) directly so we keep the ATF
	distinction. Falls back to "Other Firearm" for anything unrecognized.

	NFA checks run FIRST: "Short Barreled Rifle" contains "rifle" and would
	otherwise fold into the generic long-gun buckets. The SBR/SBS/AOW
	abbreviations match only as whole tokens so letters buried inside another
	word never reclassify an item.
	"""
	if not fb_type:
		return None
	t = fb_type.lower()
	# "Suppressor ready / capable / compatible / host" is host-gun marketing
	# copy and "suppressor cover / sleeve / wrap / pouch" is an accessory —
	# strip the phrase first so the host type wins (or, for a bare accessory,
	# nothing matches and it lands in the neutral "Other Firearm" bucket).
	# If this table grows again, move it to the normalize.py pattern.
	t = re.sub(
		r"\b(?:suppressor|silencer)s?[\s/-]*"
		r"(?:ready|capable|compatible|host|covers?|sleeves?|wraps?|pouch(?:es)?)\b",
		" ", t,
	)
	if re.search(r"\bsilencers?\b|\bsuppressors?\b", t):
		return "Silencer"
	if "machine gun" in t or "machinegun" in t:
		return "Machine Gun"
	short_barreled = "short barrel" in t or "short-barrel" in t
	if (short_barreled and "shotgun" in t) or re.search(r"\bsbs\b", t):
		return "Short Barreled Shotgun"
	if (short_barreled and "rifle" in t) or re.search(r"\bsbr\b", t):
		return "Short Barreled Rifle"
	if "destructive" in t:
		return "Destructive Device"
	if "any other weapon" in t or re.search(r"\baow\b", t):
		return "Any Other Weapon"
	if "revolver" in t:
		return "Revolver"
	if "pistol" in t or "handgun" in t:
		return "Pistol"
	if "shotgun" in t:
		return "Shotgun"
	if "rifle" in t or "carbine" in t:
		return "Rifle"
	if "receiver" in t or "frame" in t:
		return "Receiver"
	return "Other Firearm"


def _describe(fb_item: dict) -> str:
	parts = []
	for key in ("manufacturer", "model", "caliber", "type", "barrelLength", "finish"):
		val = fb_item.get(key)
		if val:
			parts.append(f"{key}: {val}")
	return " | ".join(parts)


def _create_acquisition(item_code: str, fb_item: dict) -> str:
	from ffl_core.acquisition import record_firearm_acquisition

	# Importer doesn't follow acquisitionContactId — saves one HTTP per item.
	# Source maps to Individual by default (see _map_acquisition_source); the
	# bound book on FastBound's side stays authoritative.
	source_type = _map_acquisition_source(fb_item)
	src_name = "FastBound Import"
	src_ffl = None
	id_required = source_type in {"Individual", "Trade-in", "Estate", "Pawn"}
	return record_firearm_acquisition(
		item_code=item_code,
		serial=(fb_item.get("serial") or "").strip(),
		source_type=source_type,
		source_name=src_name,
		source_ffl=src_ffl,
		source_id_type="Driver License" if id_required else None,
		source_id_number="IMPORTED" if id_required else None,
		cost=fb_item.get("cost") or 0,
		company=frappe.defaults.get_user_default("Company") or _first_company(),
		fastbound_id=fb_item["id"],
		notes="Imported from FastBound bound book.",
		acquisition_date=_parse_dt(fb_item.get("acquire_Date")),
	)


def _map_acquisition_source(fb_item: dict) -> str:
	# FastBound's actual field name is "acquisitionType" (verified live).
	# "ffl" intentionally NOT mapped — we don't follow acquisitionContactId
	# to grab the source FFL number, so FFL Dealer would fail our own
	# validate (source_ffl required). Stay on Individual; the FastBound
	# A&D record is authoritative.
	src_type = (fb_item.get("acquisitionType") or "").lower()
	mapping = {
		"individual": "Individual",
		"manufacturer": "Manufacturer",
		"importer": "Importer",
		"estate": "Estate",
		"pawn": "Pawn",
		"trade": "Trade-in",
	}
	for key, val in mapping.items():
		if key in src_type:
			return val
	return "Individual"


def _create_stock_receipt(item_code: str, fb_item: dict, *, warehouse: str | None = None) -> str:
	# A given warehouse decides the company (reassign's receipt leg must land where
	# issue_serial took the gun out); otherwise the user's default.
	company = (frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None) \
		or frappe.defaults.get_user_default("Company") or _first_company()
	warehouse = warehouse or _default_warehouse(company)
	# 0.01 default — Material Receipt rejects qty=1 rate=0 when the
	# imported Item has no valuation_rate yet.
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": company,
		"items": [serialized_receipt_row(
			item_code=item_code, qty=1, warehouse=warehouse,
			basic_rate=float(fb_item.get("cost") or 0) or 0.01,
			serial=(fb_item.get("serial") or "").strip() or None,
		)],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	# Stamp per-unit attributes onto the Serial No the receipt just materialised.
	serial = (fb_item.get("serial") or "").strip()
	if serial and frappe.db.exists("Serial No", serial):
		_stamp_serial_attrs(serial, fb_item)
	return se.name


def _stamp_serial_attrs(serial: str, fb_item: dict) -> None:
	"""Record per-unit attributes on the just-materialised Serial No:

	- fastbound_item_id — inventory_sync reconciles in-stock guns against the
	  bound book by it (and the report deep-links by it), so imported guns need it.
	- manufacturer + importer — THIS gun's own maker/importer. Kept per-serial so a
	  model-aggregated (maker-agnostic) Item still surfaces each unit's true
	  manufacturer in the bound book — the "same model, different maker/importer"
	  case (surplus M1 Garands, Mausers, …).
	- model + caliber — THIS gun's true model/caliber as recorded in FastBound. Kept
	  per-serial (Item is just the bucket) so the sync can keep them aligned to the
	  bound book without ever moving the gun between Items.

	FastBound is authoritative: each field is forced to the FB value when present,
	so this doubles as the sync's attribute reconciliation.
	"""
	updates: dict[str, str] = {}
	fb_id = (fb_item.get("id") or "").strip()
	if fb_id:
		updates["fastbound_item_id"] = fb_id
	mfg = (fb_item.get("manufacturer") or "").strip()
	if mfg and mfg.lower() != "unknown":
		# Canonical maker name (H&R, Colt, …) so the bound book shows one
		# consistent maker per Serial No. Serial No.manufacturer is a Brand link.
		updates["manufacturer"] = _ensure_brand(_normalize.canon_manufacturer(mfg))
	importer_name = (fb_item.get("importer") or "").strip()
	if importer_name:
		updates["importer"] = importer_name
	model = (fb_item.get("model") or "").strip()
	if model and model.lower() != "unknown":
		updates["model_name"] = model
	caliber_name = (fb_item.get("caliber") or "").strip()
	if caliber_name and caliber_name.lower() != "unknown":
		# Serial No.caliber is a Caliber link; create the record on first sight.
		updates["caliber"] = _ensure_caliber(caliber_name)
	if updates:
		frappe.db.set_value("Serial No", serial, updates, update_modified=False)


def _default_warehouse(company: str) -> str:
	# Prefer the POS Profile warehouse if one is configured for this company.
	pos = frappe.db.get_value("POS Profile", {"company": company}, "warehouse")
	if pos:
		return pos
	# Fallback delegates to ffl_core.warehouses — must never hand back a
	# per-dealer outbound-consignment warehouse.
	from ffl_core.warehouses import default_stock_warehouse
	warehouse = default_stock_warehouse(company)
	if warehouse:
		return warehouse
	raise FastBoundError(f"No warehouse configured for company '{company}'")


def _first_company() -> str:
	row = frappe.get_all("Company", pluck="name", limit=1)
	if not row:
		raise FastBoundError("No Company configured; run setup_dev_site first.")
	return row[0]


def _parse_dt(value: str | None) -> datetime | None:
	if not value:
		return None
	try:
		# FastBound dates come ISO 8601-ish. MariaDB rejects tz-aware datetimes
		# in DATETIME columns, so we drop the offset after parsing.
		dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return dt.replace(tzinfo=None)
	except (ValueError, TypeError):
		return None


def _record_run(stats: ImportStats) -> None:
	# Direct field update — bypasses validate() so writing status fields doesn't
	# require account_num to be populated (which it always should be by the time
	# an import runs anyway, but keeping this loose is more robust to test fixtures
	# and partially-configured environments).
	error_text = (
		"\n".join(f"{e['serial'] or e['fastbound_id']}: {e['error']}" for e in stats.errors[:50])
		if stats.errors else ""
	)
	frappe.db.set_value(
		"FastBound Settings",
		"FastBound Settings",
		{
			"last_import_at": now_datetime(),
			"last_import_count": stats.created,
			"last_import_errors": error_text,
		},
		update_modified=False,
	)
	safe_commit()
