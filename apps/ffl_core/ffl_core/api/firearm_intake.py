"""Firearm Intake API.

	lookup_serial(serial)  (whitelisted)
		Tells the caller whether this serial is already known. If it is, the UI
		shows "this gun is already in the bound book" and offers links — we
		never silently overwrite a serial.

	create_intake(payload)  (not an endpoint; the POS Trade-in calls it)
		Atomic ingest: Item (Used Firearm group) + FFL Acquisition (submitted) +
		Stock Entry (Material Receipt, +1). Wrapped in a savepoint so a failure
		anywhere unwinds the whole thing.

All hard validation (required fields, source-type → ID-required matrix) lives
here on the server. The UI's required-* asterisks are decoration; the server
is the legal-correctness firewall.
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime
from ffl_core import storage
from ffl_core.api._helpers import coerce_payload
from ffl_core.roles import COUNTER_ROLES
from ffl_core.firearm import (
	ensure_brand,
	ensure_caliber,
	serial_firearm_attr_updates,
	serialized_receipt_row,
	used_firearm_item_doc,
)

REQUIRED_FIELDS = (
	"serial",
	"manufacturer",
	"model_name",
	"source_type",
	"acquisition_cost",
	"condition_grade",
)
# retail_price is intentionally optional — at intake time the operator
# often hasn't priced the unit yet. Standard_rate falls through to None
# and is set later via the Item form.
#
# The "which source types are individuals" rule lives once in
# ffl_core.acquisition.INDIVIDUAL_SOURCES; imported where needed.


@frappe.whitelist()
def lookup_serial(serial: str) -> dict:
	"""Look up a firearm by serial to short-circuit duplicate intakes.

	Resolves via the canonical Serial No table first, then falls back to
	FFL Acquisition.serial in case the Item has been deleted.

	Args:
	    serial: Serial number (whitespace-stripped; blank returns not-found).

	Returns:
	    {found: false} — nothing on file;
	    {found: true, source: "erpnext", item, acquisition} — already an
	    Item (acquisition may be null);
	    {found: true, source: "acquisition_only", acquisition} — an
	    FFL Acquisition exists but its Item is gone.
	"""
	frappe.only_for(COUNTER_ROLES)
	return _lookup_serial(serial)


def _lookup_serial(serial: str) -> dict:
	"""Gate-free body of lookup_serial for internal callers (create_intake's
	duplicate-serial check runs under ITS caller's gate, not this one's)."""
	serial = (serial or "").strip()
	if not serial:
		return {"found": False}

	# Resolve via the canonical Serial No table. Every firearm is
	# has_serial_no=1 now (the Used Firearm / Item.firearm_serial path was
	# unified); the Serial No row points at its Item, and the Item carries
	# the metadata we want to show.
	item_code = frappe.db.get_value("Serial No", serial, "item_code")
	if item_code:
		item = frappe.db.get_value(
			"Item", item_code,
			["name", "item_name", "manufacturer", "model_name", "listing_status"],
			as_dict=True,
		)
		if item:
			acq = frappe.db.get_value(
				"FFL Acquisition",
				{"item": item["name"], "docstatus": 1},
				["name", "acquisition_date", "source_type", "fastbound_id"],
				as_dict=True,
			)
			return {
				"found": True,
				"source": "erpnext",
				"item": item,
				"acquisition": acq,
			}

	# Match against any FFL Acquisition.serial in case the Item has been deleted.
	acq = frappe.db.get_value(
		"FFL Acquisition",
		{"serial": serial},
		["name", "item", "docstatus"],
		as_dict=True,
	)
	if acq:
		return {"found": True, "source": "acquisition_only", "acquisition": acq}

	return {"found": False}


def _check_serial_free(p: dict) -> None:
	"""Refuse a trade-in of a serial the store still holds (it is in a warehouse) or whose
	bound-book stay is still open. A gun the store disposed of and now takes back is a new
	stay (a closed cycle, acquisition._existing): it is filed under the Item its Serial No
	already belongs to, since ERPNext receives a serial only under its own Item. Writes
	that Item into ``p["item_code"]``."""
	from ffl_core.acquisition import TRADE_IN_SOURCE, _existing
	serial = (p.get("serial") or "").strip()
	if not serial:
		return
	sn = frappe.db.get_value("Serial No", serial, ["item_code", "warehouse"], as_dict=True)
	if sn and sn.item_code:
		if sn.warehouse or _existing(sn.item_code, serial):
			frappe.throw(_("Serial '{0}' is already on file (item {1}) and the store still holds it.").format(
				serial, sn.item_code))
		if p.get("item_code") and p["item_code"] != sn.item_code:
			frappe.throw(_("Serial '{0}' was filed under item {1} before. Take it in under that item.").format(
				serial, sn.item_code))
		p["item_code"] = sn.item_code
		return
	# No Serial No: an acquisition whose Item is gone. A pending trade-in draft is not one --
	# re-running the dialog reuses or replaces it (trade_in._refresh_pending_draft / _buy_now).
	acq = next((a.name for a in frappe.get_all("FFL Acquisition", {"serial": serial},
		["name", "docstatus", "source_type"]) if not (a.docstatus == 0 and a.source_type == TRADE_IN_SOURCE)), None)
	if acq:
		frappe.throw(_("Serial '{0}' is already on file (acquisition {1}).").format(serial, acq))


def create_intake(payload, defer_booking: bool = False) -> dict:
	"""Atomically intake a used firearm: Item + FFL Acquisition + stock receipt.

	Atomic create — a failure anywhere unwinds the whole transaction. A
	duplicate serial (lookup_serial hit) is refused outright.

	Args:
	    payload: Intake fields — required: serial, manufacturer, model_name,
	        source_type, acquisition_cost, condition_grade; plus the
	        source-type-dependent ID fields and optional pricing. Dict or
	        JSON string (frappe.call sends a string).
	        Optional ``storage_location``: the Storage Location the gun is put in
	        (a slot must be free; refused up front otherwise).
	    defer_booking: True (the POS trade-in-with-credit path) creates the
	        Item and a DRAFT FFL Acquisition only — no bound-book entry, no
	        FastBound push, no Material Receipt / Serial No — and parks the
	        per-unit stamp data on the acquisition. complete_intake()
	        finishes the booking when the POS checkout actually submits
	        (the traded-in gun must not enter
	        the book while the sale can still be abandoned).

	Returns:
	    {"ok": True, "item_code": ..., "acquisition": ...}.

	Raises:
	    ValidationError: Any field problem (required-field matrix and
	        source-type → ID rules are enforced server-side).

	Permissions:
	    None of its own: not an endpoint. Its one caller, the POS Trade-in
	    (api/trade_in.create_trade_in_intake, a counter job), gates the door. A
	    booked private-seller purchase made here would have no way to record its
	    seller payment (a pay-now buy is a receipt paid with it,
	    api/trade_in._buy_now). Every write below runs with ignore_permissions.
	"""
	import json as _json
	payload = coerce_payload(payload)

	_validate_required(payload)
	_validate_source(payload)
	# Refuse a taken slot now, before anything is written (re-checked, softly, when a
	# deferred trade-in books at checkout).
	storage.validate_placements(_placements(payload), _company())

	# Idempotency at the serial level — same serial twice means human error.
	_check_serial_free(payload)

	savepoint = "intake_savepoint"
	frappe.db.savepoint(savepoint)
	try:
		item_code = _resolve_item(payload)
		if frappe.utils.cint(defer_booking):
			acq_name = _create_acquisition(
				item_code, payload, submit_now=False,
				pending_intake_payload=_json.dumps(_parked_stamp_payload(payload)),
			)
		else:
			acq_name = _create_acquisition(item_code, payload)
			_create_stock_receipt(item_code, payload)
			_stamp_serial(payload)
			storage.apply_placements(_placements(payload))
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	return {
		"ok": True,
		"item_code": item_code,
		"acquisition": acq_name,
	}


# Per-unit facts _stamp_serial needs at booking time that live nowhere on the
# FFL Acquisition itself (attrs may legitimately differ from the shared model
# SKU's — the bound book records the physical gun). Parked as JSON on the draft
# acquisition, consumed and cleared by complete_intake.
_PARKED_STAMP_KEYS = (
	"condition_grade", "retail_price", "manufacturer", "importer",
	"model_name", "caliber", "firearm_type", "barrel_length", "finish",
	"year_made", "storage_location",
)


def _parked_stamp_payload(p: dict) -> dict:
	return {k: p.get(k) for k in _PARKED_STAMP_KEYS if p.get(k) not in (None, "")}


def complete_intake(acquisition: str) -> dict:
	"""Finish a deferred trade-in intake at checkout time.

	Submits the parked draft FFL Acquisition (bound book; the FastBound push is
	enqueued after commit by ffl_integrations), materialises the Material
	Receipt (which creates the Serial No), and stamps the parked per-unit
	facts. Runs inside the POS Invoice submit transaction — any failure aborts
	the whole checkout, and the after-commit push can never fire for a rolled-
	back booking.

	Idempotent: an already-submitted acquisition is a no-op ({booked: False}),
	so legacy immediately-booked rows and double submits are safe."""
	doc = frappe.get_doc("FFL Acquisition", acquisition)
	if doc.docstatus == 1:
		return {"ok": True, "booked": False}
	if doc.docstatus == 2:
		frappe.throw(_(
			"Trade-in acquisition {0} is cancelled — remove the credit line or "
			"re-run the Trade-in dialog."
		).format(acquisition))

	import json as _json
	try:
		parked = _json.loads(doc.pending_intake_payload or "{}")
	except ValueError:
		parked = {}
	p = {
		**parked,
		"item_code": doc.item,
		"serial": doc.serial,
		"acquisition_cost": float(doc.cost or 0),
	}
	# acquisition_date stays the DRAFT's stamp — the moment the store took
	# physical possession at the counter (dialog / draft-refresh time). The
	# bound book records possession, not the checkout that paid for it;
	# overwriting with now() would misdate the entry AND mask a late booking
	# (§478.125(e)'s next-business-day clock runs from possession — see
	# ffl_integrations alert_stale_trade_in_drafts, the watchdog for drafts
	# that outlive that clock).
	doc.pending_intake_payload = None
	doc.flags.ignore_permissions = True
	doc.submit()
	_create_stock_receipt(doc.item, p, company=doc.company)
	# A gun taken back after a sale is a new stay: start it from "the store's own gun",
	# as every Purchase Receipt does (compliance/purchase.py), or the last stay's
	# consignor / pay-on-sale / custody stamps would bill or block its next sale.
	from ffl_core.compliance.purchase import _STORE_OWNED, _acquired_stamps
	frappe.db.set_value("Serial No", doc.serial, {
		**_STORE_OWNED, **_acquired_stamps(frappe._dict(), frappe._dict()), "needs_service": 0,
	}, update_modified=False)
	_stamp_serial(p)
	# The slot picked at the counter may have been taken since. This runs inside the
	# checkout, so a taken slot must never fail it: the gun stays unassigned and the
	# clerk is told to place it.
	for problem in storage.place_softly(lambda: _placements(p)):
		frappe.msgprint(_("The gun is booked, but not in a location: {0}").format(problem),
			alert=True, indicator="orange")
	return {"ok": True, "booked": True}


def _placements(p: dict) -> list[dict]:
	"""Where this intake's gun goes: [] when no location was named."""
	return storage.plan_placements(
		item_code=p.get("item_code") or "", qty=1, serials=[(p.get("serial") or "").strip()],
		storage_location=p.get("storage_location"))


# ------------------------------------------------------------------ validation


def _validate_required(p: dict) -> None:
	missing = [f for f in REQUIRED_FIELDS if not _has(p, f)]
	if missing:
		frappe.throw(_("Missing required fields: {0}").format(", ".join(missing)))


def _validate_source(p: dict) -> None:
	from ffl_core.acquisition import INDIVIDUAL_SOURCES
	src = p.get("source_type", "")
	if src == "FFL Dealer" and not _has(p, "source_ffl"):
		frappe.throw(_("FFL Dealer source requires an FFL number."))
	if src in INDIVIDUAL_SOURCES and not (_has(p, "source_id_type") and _has(p, "source_id_number")):
		frappe.throw(_("Source type '{0}' requires ID type and ID number.").format(src))
	if not _has(p, "source_name"):
		frappe.throw(_("Source name is required."))


def _has(p: dict, field: str) -> bool:
	val = p.get(field)
	if val is None:
		return False
	if isinstance(val, str):
		return bool(val.strip())
	if isinstance(val, (int, float)):
		return True
	return bool(val)


# ------------------------------------------------------------------ creators


def _ensure_masters(manufacturer: str, caliber_name: str) -> None:
	"""Auto-create the Brand / Caliber master records a firearm references."""
	ensure_brand(manufacturer)
	ensure_caliber(caliber_name)


def _resolve_item(p: dict) -> str:
	"""Return the Item to file this gun under.

	If the operator supplied an explicit ``item_code`` (a shared model SKU,
	e.g. "Glock19 USED"), file under it — reuse it if it exists, otherwise
	create it ONCE as a model-level item (model attributes only; the per-gun
	condition / price / description go on the Serial No, see _stamp_serial).
	With no item_code, fall back to the legacy one-Item-per-gun auto-mint."""
	explicit = (p.get("item_code") or "").strip()
	if not explicit:
		return _create_item(p)
	if frappe.db.exists("Item", explicit):
		return explicit  # reuse the shared SKU — never overwrite its model fields
	return _create_model_item(explicit, p)


def _create_model_item(item_code: str, p: dict) -> str:
	"""Create a shared used-model SKU carrying only model-level attributes.
	Per-gun values (condition, retail price, description) are intentionally NOT
	set here — they live on each Serial No."""
	manufacturer = (p.get("manufacturer") or "").strip()
	model = (p.get("model_name") or "").strip()
	caliber_name = (p.get("caliber") or "").strip()
	_ensure_masters(manufacturer, caliber_name)
	display = " ".join(part for part in (manufacturer, model) if part)
	display = f"{display} (Used)" if display else item_code
	doc = frappe.get_doc(used_firearm_item_doc(
		item_code=item_code,
		item_name=display,
		description=" | ".join(part for part in (manufacturer, model, caliber_name) if part),
		manufacturer=manufacturer,
		model_name=model,
		caliber=caliber_name,
		firearm_type=p.get("firearm_type"),
		importer=(p.get("importer") or "").strip() or None,
	))
	doc.flags.ignore_permissions = True
	doc.insert()
	return item_code


def _stamp_serial(p: dict) -> None:
	"""Write this gun's per-unit values onto its Serial No (materialised by the
	Material Receipt): manufacturer / importer / model / caliber, condition
	grade, sell price, per-gun description, and cost (purchase_rate).

	The firearm attrs MUST be stamped here rather than left to the
	inherit_serial_firearm_attrs before_save hook: ERPNext materialises Serial
	No rows via frappe.db.bulk_insert (make_serial_nos), which bypasses doc
	events — so the hook never fires for an intake-created serial and the
	per-gun record (the bound book's source of truth) would stay blank.

	Cost is stamped here for that reason plus a second one: ERPNext does not
	flow the receipt's basic_rate onto Serial No.purchase_rate on its own (the
	same gap compliance/purchase.py patches for Purchase Receipts), and the
	Sales Report reads purchase_rate for firearm COGS — leaving it 0 makes the
	gun resell at zero cost / 100% margin."""
	serial = (p.get("serial") or "").strip()
	if not serial:
		return  # set_value below no-ops if the Serial No somehow doesn't exist
	updates: dict[str, Any] = serial_firearm_attr_updates(p)
	if p.get("condition_grade"):
		updates["condition_grade"] = p["condition_grade"]
	if p.get("retail_price") not in (None, ""):
		updates["sell_price"] = float(p["retail_price"])
	# > 0 only, matching the Purchase-Receipt path (compliance/purchase.py): a
	# genuinely costless intake stays flagged as missing on the Sales Report
	# rather than showing a fake $0 cost.
	cost = p.get("acquisition_cost")
	if cost is not None and float(cost) > 0:
		updates["purchase_rate"] = float(cost)
	desc = _describe(p)
	if desc:
		updates["description"] = desc
	if updates:
		frappe.db.set_value("Serial No", serial, updates, update_modified=False)


def _create_item(p: dict) -> str:
	manufacturer = (p.get("manufacturer") or "").strip()
	model = (p.get("model_name") or "").strip()
	serial = (p.get("serial") or "").strip()
	caliber_name = (p.get("caliber") or "").strip()
	_ensure_masters(manufacturer, caliber_name)

	# Item code: short, predictable, useful in lists.
	item_code = _next_item_code(serial)
	display = " ".join(part for part in (manufacturer, model) if part) or item_code

	# Serial is NOT passed to used_firearm_item_doc — the canonical serial
	# lives on the Serial No record stamped by the Stock Entry below.
	doc = frappe.get_doc(used_firearm_item_doc(
		item_code=item_code,
		item_name=display,
		description=_describe(p),
		manufacturer=manufacturer,
		model_name=model,
		caliber=caliber_name,
		firearm_type=p.get("firearm_type"),
		dealer_price=p.get("dealer_price"),
		importer=(p.get("importer") or "").strip() or None,
		barrel_length=p.get("barrel_length") or None,
		finish=p.get("finish") or None,
		year_made=p.get("year_made") or None,
		condition_grade=p.get("condition_grade"),
	))
	doc.flags.ignore_permissions = True
	doc.insert()
	# The retail price is a reference on the Item, set after the insert: given to insert(), ERPNext's
	# Item.after_insert would make a selling Item Price from it (as the session user, a permission the
	# counter lacks), and a per-gun priced firearm must carry none (item_hooks.sync_standard_selling_price).
	# The gun's own price is its Serial No's sell_price (_stamp_serial).
	if p.get("retail_price") not in (None, ""):
		frappe.db.set_value("Item", item_code, "standard_rate", frappe.utils.flt(p["retail_price"]), update_modified=False)
	return item_code


def _next_item_code(serial: str) -> str:
	# UF-{YYYYMMDD}-{serial-tail-6} — short, sortable, locatable by serial.
	#
	# `now_datetime()`, not `datetime.now()`: the date baked into this code is the
	# date staff will read it as — the day the gun was taken in — and that is the
	# SHOP's date. On a UTC container serving a US store the host has already
	# rolled over from early evening onward, so every intake after ~6pm was
	# stamped tomorrow. Cosmetic next to the watchdog, but it
	# is the code an acquisition gets looked up by.
	tail = "".join(c for c in serial if c.isalnum())[-6:].upper() or "X"
	stub = f"UF-{now_datetime().strftime('%Y%m%d')}-{tail}"
	candidate = stub
	suffix = 1
	while frappe.db.exists("Item", candidate):
		suffix += 1
		candidate = f"{stub}-{suffix}"
	return candidate


def _describe(p: dict) -> str:
	parts = []
	for key in ("manufacturer", "model_name", "caliber", "firearm_type", "barrel_length", "finish", "year_made"):
		val = p.get(key)
		if val:
			parts.append(f"{key}: {val}")
	return " | ".join(parts)


def _create_acquisition(item_code: str, p: dict, *, submit_now: bool = True,
		pending_intake_payload: str | None = None) -> str:
	from ffl_core.acquisition import INDIVIDUAL_SOURCES, record_firearm_acquisition

	src = p["source_type"]
	return record_firearm_acquisition(
		item_code=item_code,
		serial=(p.get("serial") or "").strip(),
		source_type=src,
		source_name=(p.get("source_name") or "").strip(),
		source_address=(p.get("source_address") or "").strip(),
		source_ffl=(p.get("source_ffl") or "").strip() if src == "FFL Dealer" else None,
		source_id_type=p.get("source_id_type") if src in INDIVIDUAL_SOURCES else None,
		source_id_number=(p.get("source_id_number") or "").strip() if src in INDIVIDUAL_SOURCES else None,
		source_customer=(p.get("source_customer") or "").strip() or None,
		source_first_name=(p.get("source_first_name") or "").strip() or None,
		source_middle_name=(p.get("source_middle_name") or "").strip() or None,
		source_last_name=(p.get("source_last_name") or "").strip() or None,
		source_suffix=(p.get("source_suffix") or "").strip() or None,
		cost=p.get("acquisition_cost") or 0,
		company=_company(),
		notes=(p.get("notes") or "").strip(),
		submit_now=submit_now,
		pending_intake_payload=pending_intake_payload,
	)


def _create_stock_receipt(item_code: str, p: dict, *, company: str | None = None) -> None:
	# The deferred-booking path passes the acquisition's own company: at POS
	# submit time the session user is the cashier, whose user-default Company
	# may be unset — the dialog-time default must win.
	company = company or _company()
	warehouse = _default_warehouse(company)
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": company,
		"items": [serialized_receipt_row(
			item_code=item_code, qty=1, warehouse=warehouse,
			basic_rate=p.get("acquisition_cost") or 0,
			serial=(p.get("serial") or "").strip() or None,
		)],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()


def _company() -> str:
	row = frappe.defaults.get_user_default("Company") or _first("Company")
	if not row:
		frappe.throw(_("No Company configured. Run setup first."))
	return row


def _default_warehouse(company: str) -> str:
	pos = frappe.db.get_value("POS Profile", {"company": company}, "warehouse")
	if pos:
		return pos
	# Fallback delegates to ffl_core.warehouses — must never hand back a
	# per-dealer outbound-consignment warehouse.
	from ffl_core.warehouses import default_stock_warehouse
	warehouse = default_stock_warehouse(company)
	if not warehouse:
		frappe.throw(_("No warehouse configured for company {0}.").format(company))
	return warehouse


def _first(doctype: str) -> str | None:
	row = frappe.get_all(doctype, pluck="name", limit=1)
	return row[0] if row else None
