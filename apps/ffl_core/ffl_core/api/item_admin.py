"""Item admin endpoints — force-delete with dependency cleanup.

Standard ERPNext refuses to delete an Item that is linked from any
submitted document. For a single-store gun shop in active development
that means cancelling Purchase Receipts, Stock Entries, Serial Nos, FFL
Acquisitions, etc. one by one through the UI before retrying.

This module exposes two whitelisted endpoints used by the Item-form
"Delete" button (see public/js/item_form.js):

  preview_delete(item_codes)
      Returns the count of linked rows per doctype that would be
      cancelled or deleted. Read-only. Used to populate the confirm
      dialog so the operator sees exactly what they're about to undo.

  force_delete(item_codes, cancel_submitted=False)
      Cascading delete. If `cancel_submitted` is False and any submitted
      docs reference the item, raises ValidationError so the caller can
      re-prompt. With `cancel_submitted=True`, cancels in dependency
      order (POS Invoice → Sales Invoice → Stock Entry → Purchase
      Receipt → Serial No → FFL Acquisition / Disposition → Item).

Both endpoints are gated to System Manager.
"""
from __future__ import annotations

import json as _json
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from ffl_core.roles import STOCK_ROLES
from ffl_core.api._helpers import default_warehouse as _default_warehouse


# Doctypes that hold submittable references to an Item, in cancel order.
# Inner-list element is (doctype, item_field, parent_join_for_child_table).
# When the item field lives on a child table we cancel the parent.
_LINKED_SUBMITTABLE = [
	# (doctype, where_clause_template, parent_doctype_to_cancel)
	("POS Invoice Item", "item_code IN %(codes)s", "POS Invoice"),
	("Sales Invoice Item", "item_code IN %(codes)s", "Sales Invoice"),
	("Delivery Note Item", "item_code IN %(codes)s", "Delivery Note"),
	("Stock Entry Detail", "item_code IN %(codes)s", "Stock Entry"),
	("Purchase Receipt Item", "item_code IN %(codes)s", "Purchase Receipt"),
	("Purchase Invoice Item", "item_code IN %(codes)s", "Purchase Invoice"),
	("Purchase Order Item", "item_code IN %(codes)s", "Purchase Order"),
	("Sales Order Item", "item_code IN %(codes)s", "Sales Order"),
]

# Direct (non-submittable, or own-row) records that just need to be deleted.
# NOTE: column names differ — Item Group's standard rows use `item_code`, but
# FFL Acquisition / Disposition use `item`, and Batch uses `item`. Each entry
# carries its own WHERE template so the column name is explicit.
_LINKED_SIMPLE = [
	("Stock Ledger Entry", "item_code IN %(codes)s"),
	("Bin", "item_code IN %(codes)s"),
	("Serial No", "item_code IN %(codes)s"),
	("Batch", "item IN %(codes)s"),
	("Item Price", "item_code IN %(codes)s"),
	("FFL Acquisition", "item IN %(codes)s"),
	("FFL Disposition", "item IN %(codes)s"),
]


def _check_perm():
	"""Force-delete is destructive. Gate behind System Manager."""
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Force-delete is restricted to System Manager."), frappe.PermissionError)


def _normalize_codes(item_codes) -> list[str]:
	if isinstance(item_codes, str):
		try:
			parsed = _json.loads(item_codes)
			if isinstance(parsed, list):
				return [c for c in (s.strip() for s in parsed) if c]
		except _json.JSONDecodeError:
			# treat as comma-separated
			return [c for c in (s.strip() for s in item_codes.split(",")) if c]
	if isinstance(item_codes, list):
		return [c for c in (s.strip() for s in item_codes) if c]
	frappe.throw(_("item_codes must be a list or comma-separated string"))


def _stock_target(item_code: str, warehouse: str | None,
		serial_msg: str) -> tuple[str, str]:
	"""The gate add_stock and set_stock share: a non-serialized stock Item and a
	warehouse (the configured default when blank) with a company. Returns
	(warehouse, company); `serial_msg` ({0} = item_code) names the right path
	for a serial-tracked Item."""
	item = frappe.db.get_value(
		"Item", item_code, ["has_serial_no", "is_stock_item"], as_dict=True)
	if not item:
		frappe.throw(_("Item '{0}' not found.").format(item_code))
	if not item.is_stock_item:
		frappe.throw(_("Item '{0}' is not a stock item.").format(item_code))
	if item.has_serial_no:
		frappe.throw(serial_msg.format(item_code))

	warehouse = (warehouse or "").strip() or _default_warehouse()
	if not warehouse:
		frappe.throw(_("No warehouse configured."))
	# Stock Entry's company must match the warehouse's company. Derive from
	# the warehouse rather than the user-default to avoid mismatches when
	# a non-default warehouse is passed explicitly.
	company = frappe.db.get_value("Warehouse", warehouse, "company")
	if not company:
		frappe.throw(_("Warehouse '{0}' has no company.").format(warehouse))
	return warehouse, company


@frappe.whitelist()
def add_stock(item_code: str, qty, warehouse: str | None = None,
		rate=None, remarks: str | None = None) -> dict:
	"""Quick stock receipt — creates and submits a Material Receipt Stock Entry.

	Used by the "+ Add Stock" button on the Item form for everyday adjustments
	(found inventory, opening stock, manual top-up). Firearms tracked by
	Serial No are rejected — those go through Receive Goods so the FFL
	compliance flow runs.

	Args:
	    item_code: Non-serialized stock Item to receive.
	    qty: Units to add, > 0.
	    warehouse: Optional target; defaults to the configured warehouse.
	    rate: Optional per-unit valuation; non-numeric or <= 0 is ignored.
	    remarks: Optional Stock Entry remarks.

	Returns:
	    {ok, name, qty, warehouse}.

	Raises:
	    ValidationError: qty <= 0, missing item, non-stock item,
	        serial-tracked item, or no warehouse available.

	Permissions:
	    Stock Manager / System Manager.
	"""
	frappe.only_for(STOCK_ROLES)
	item_code = (item_code or "").strip()
	if not item_code:
		frappe.throw(_("item_code is required."))

	try:
		qty_f = float(qty)
	except (TypeError, ValueError):
		frappe.throw(_("Qty must be a number."))
	if qty_f <= 0:
		frappe.throw(_("Qty must be greater than zero."))

	warehouse, company = _stock_target(item_code, warehouse, _(
		"Item '{0}' requires serial numbers per unit. Use the Receive Goods "
		"page so each unit gets a Serial No record and the FFL Acquisition "
		"is created."))

	rate_f = None
	if rate not in (None, "", 0):
		try:
			rate_f = float(rate)
			if rate_f <= 0:
				rate_f = None
		except (TypeError, ValueError):
			rate_f = None

	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": company,
		"items": [{
			"item_code": item_code,
			"qty": qty_f,
			"t_warehouse": warehouse,
			"basic_rate": rate_f,
		}],
		"remarks": (remarks or "").strip() or None,
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()

	return {
		"ok": True,
		"name": se.name,
		"qty": qty_f,
		"warehouse": warehouse,
	}


@frappe.whitelist()
def set_stock(item_code: str, new_qty, warehouse: str | None = None,
		reason: str | None = None) -> dict:
	"""Adjust stock to an absolute quantity via Stock Reconciliation.

	Used by the "Adjust Quantity" button on the Item form when the user
	wants to set on-hand to a specific number (post-physical-count, found
	inventory correction, write-off). For positive deltas a Material
	Receipt is faster (see add_stock); set_stock is the right primitive
	when the user already knows the absolute target.

	The quantity entered is what is on the shelf now. A register sale not yet
	closed has left the shelf but is still in the Bin (ERPNext posts it at the
	register close), so the reconciliation sets the Bin to the entered quantity
	plus those units; once the close posts them the Bin reads the entered figure.

	Args:
	    item_code: Non-serialized stock Item to adjust.
	    new_qty: Units physically on the shelf now, >= 0.
	    warehouse: Optional target; defaults to the configured warehouse.
	    reason: Optional audit note (stored as a Comment on the Stock
	        Reconciliation).

	Returns:
	    {ok, name (Stock Reconciliation #), prev_qty, new_qty (as entered),
	    open_register_qty (units on register sales not closed yet), delta
	    (what the reconciliation changed the Bin by), warehouse}.

	Raises:
	    ValidationError: Serial-tracked item, invalid qty, missing
	        warehouse, or no qty change.

	Permissions:
	    Stock Manager / System Manager.
	"""
	frappe.only_for(STOCK_ROLES)
	item_code = (item_code or "").strip()
	if not item_code:
		frappe.throw(_("item_code is required."))
	try:
		new_qty_f = float(new_qty)
	except (TypeError, ValueError):
		frappe.throw(_("Quantity must be a number."))
	if new_qty_f < 0:
		frappe.throw(_("Quantity cannot be negative."))

	warehouse, company = _stock_target(item_code, warehouse, _(
		"Item '{0}' is serial-tracked. Each unit is its own Serial No "
		"record — adjusting bulk qty here would orphan serial entries. "
		"Use Receive Goods to add or Stock Entry to remove specific units."))

	# A register sale stays in the Bin until the register closes (a return's
	# negative qty: out of it), so the Bin must hold the shelf count plus those units.
	from ffl_core.api.inventory_count import _open_register_sales, _row_rate
	open_qty = flt(sum(s["qty"] for s in _open_register_sales(
		{(item_code, warehouse)}, now_datetime()).get((item_code, warehouse), [])), 6)
	target = max(0.0, flt(new_qty_f + open_qty, 6))

	# Read current qty so we can short-circuit no-ops
	prev_qty = float(frappe.db.get_value(
		"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
	) or 0)
	if abs(prev_qty - target) < 1e-9:
		frappe.throw(_("Stock is already {0} at {1}; nothing to adjust.")
			.format(new_qty_f, warehouse))

	# An explicit rate on a reconciliation row revalues everything already in
	# the bin, so keep the bin's own rate when it holds stock (the item's cost
	# otherwise). 0 = no cost on file: fine for a write-off, not for new units.
	rate = _row_rate(item_code, warehouse, prev_qty)
	if target > prev_qty and not rate:
		frappe.throw(_("No cost on file for {0}: set its valuation rate, or use + Add Stock "
			"with a rate.").format(item_code))

	sr = frappe.get_doc({
		"doctype": "Stock Reconciliation",
		"company": company,
		"purpose": "Stock Reconciliation",
		"items": [{
			"item_code": item_code,
			"warehouse": warehouse,
			"qty": target,
			"valuation_rate": rate,
			"allow_zero_valuation_rate": 0 if rate else 1,
		}],
	})
	sr.flags.ignore_permissions = True
	sr.insert()
	sr.submit()

	# Add a comment with the reason if provided (Stock Recon doesn't have
	# a remarks field on the parent — using a Comment keeps the trace).
	if reason and reason.strip():
		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Stock Reconciliation",
			"reference_name": sr.name,
			"content": reason.strip(),
		}).insert(ignore_permissions=True)

	return {
		"ok": True,
		"name": sr.name,
		"prev_qty": prev_qty,
		"new_qty": new_qty_f,
		"open_register_qty": open_qty,
		"delta": target - prev_qty,
		"warehouse": warehouse,
	}


def recompute_item_actual_qty(item_code: str) -> float:
	"""Sum tabBin.actual_qty for an Item across warehouses and write back
	to Item.actual_qty (custom field, surfaced in the list view).

	Called from the Stock Ledger Entry on_submit / on_cancel hook so the
	list-view "Quantity" column stays in sync with stock movements.

	Returns the new total qty.
	"""
	if not item_code:
		return 0.0
	if not frappe.db.exists("Item", item_code):
		return 0.0
	row = frappe.db.sql(
		"SELECT COALESCE(SUM(actual_qty), 0) FROM `tabBin` WHERE item_code=%s",
		(item_code,),
	)
	qty = float(row[0][0]) if row else 0.0
	# update_modified=False keeps the Item's modified timestamp clean — stock
	# changes shouldn't bump the doc's modified-by audit.
	frappe.db.set_value("Item", item_code, "actual_qty", qty, update_modified=False)
	return qty


def on_stock_ledger_entry_change(doc, method=None) -> None:
	"""hooks.py doc_events handler — keeps Item.actual_qty in sync.

	Normal SLEs (receipts, issues, transfers) record the real change in
	`actual_qty`, so we apply it as an atomic delta-SQL rather than
	`SELECT SUM(Bin) → write`. ERPNext submits the SLE — firing this hook —
	BEFORE it updates the Bin in the same transaction (see make_sl_entries:
	make_entry/sle.submit() runs, then update_bin_qty()), so a SUM here would
	miss this very SLE's contribution. The delta skips Bin entirely and is
	race-free.

	Stock Reconciliation is the exception: it records the post-count absolute
	in `qty_after_transaction` and leaves `actual_qty` at 0, so the delta path
	is blind to it ("adjust quantity to N" would leave Item.actual_qty stale).
	For those we enqueue a Bin-sum recompute deferred to after_commit — by then
	every Bin is settled, so the SUM is correct (a synchronous SUM here would
	hit the same stale-Bin race described above).

	Residual drift (manual DB edits, a missed hook) is healed by
	recompute_all_item_actual_qty(), runnable any time without side effects.
	"""
	# No exists() probe per SLE (a bulk receipt fires hundreds): the delta
	# UPDATE below is `WHERE name=%s`, so it harmlessly no-ops on a missing Item.
	if not doc.item_code:
		return
	if doc.voucher_type == "Stock Reconciliation":
		frappe.enqueue(
			"ffl_core.api.item_admin.recompute_item_actual_qty",
			item_code=doc.item_code,
			enqueue_after_commit=True,
			queue="short",
		)
		return
	delta = float(doc.actual_qty or 0)
	if method == "on_cancel":
		delta = -delta
	if delta == 0:
		return
	frappe.db.sql(
		"UPDATE `tabItem` SET actual_qty = COALESCE(actual_qty, 0) + %s WHERE name = %s",
		(delta, doc.item_code),
	)


@frappe.whitelist()
def recompute_all_item_actual_qty() -> dict:
	"""One-shot reconciliation: refresh Item.actual_qty for every Item from
	current Bin sums in a single bulk UPDATE. Useful after migrate to backfill
	the new field, or after data imports / direct-DB tweaks.

	Two statements:
	1. UPDATE Item JOIN (SUM Bin) — set qty for items that have Bin rows
	2. UPDATE Item — zero out items with no Bin rows (e.g. after Force Delete)

	Returns:
	    {"with_bin": <count of items that have Bin rows>}.

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	frappe.db.sql(
		"""
		UPDATE `tabItem` i
		JOIN (
			SELECT item_code, SUM(actual_qty) AS qty
			FROM `tabBin` GROUP BY item_code
		) b ON b.item_code = i.name
		SET i.actual_qty = b.qty
		WHERE COALESCE(i.actual_qty, 0) != b.qty
		""")
	frappe.db.sql(
		"""
		UPDATE `tabItem` i
		LEFT JOIN (SELECT item_code FROM `tabBin` GROUP BY item_code) b
			ON b.item_code = i.name
		SET i.actual_qty = 0
		WHERE b.item_code IS NULL AND COALESCE(i.actual_qty, 0) != 0
		""")
	with_bin = frappe.db.sql(
		"SELECT COUNT(DISTINCT item_code) FROM `tabBin`")[0][0]
	frappe.db.commit()
	return {"with_bin": int(with_bin)}


@frappe.whitelist()
def get_item_stock(item_codes) -> dict:
	"""Return {item_code: total_actual_qty} summed across all warehouses.

	Reads from `tabBin` (the per-warehouse stock-on-hand cache). Items with
	no Bin row return 0. Designed for the Item list view + Item form to
	show a stock badge without forcing the user to open Stock Balance.

	Args:
	    item_codes: A list, JSON-encoded list, or comma-separated string.

	Returns:
	    {item_code: total_actual_qty} — every requested code is present.
	"""
	# A read-only stock badge on the Item list/form — gate on Item READ,
	# not a fixed role list, so everyone who can open an Item (Item Manager,
	# Sales/Purchase User, ...) sees it while a role-less caller is refused
	# (STOCK_ROLES was too narrow for a read badge).
	frappe.has_permission("Item", "read", throw=True)
	return _get_item_stock(item_codes)


def _get_item_stock(item_codes) -> dict:
	"""Gate-free body of get_item_stock — internal callers (woo mapping,
	distributor options/listing/planner) run under webhook/scheduler or
	their own endpoint's gate, so the HTTP wrapper's role check must not
	apply to them."""
	codes = _normalize_codes(item_codes)
	if not codes:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT item_code, SUM(actual_qty) AS qty
		FROM `tabBin`
		WHERE item_code IN %(codes)s
		GROUP BY item_code
		""",
		{"codes": tuple(codes)},
		as_dict=True,
	)
	out = {c: 0 for c in codes}
	for r in rows:
		out[r["item_code"]] = float(r["qty"] or 0)
	return out


def local_unit_cost(item_code):
	"""What our own on-hand stock cost us, per unit — or None when we cannot tell.

	Display-only: the in-store row of the web-order options sorts into the same
	ascending ladder as the distributor candidates, and the counter shows an
	order line's cost. Serialized guns are deliberately NOT costed per
	serial here — this row compares one ORDER LINE against distributor offers, and the
	per-serial figure belongs to the A&D/report side where it is audited.

	Bins first, weighted by quantity: Bin.valuation_rate is what stock actually cost,
	whereas Item.valuation_rate is only the default applied to new stock and is 0 on
	most items. A flat mean across bins would let one unit in a cheap warehouse drag
	the whole ladder down, so the average is weighted.

	None rather than 0 when nothing is known: 0 would sort the in-store row to the TOP
	of a cheapest-first ladder and read as free — the same invented-number trap as
	quoting unknown freight as zero."""
	try:
		qty_total = 0.0
		value_total = 0.0
		for b in frappe.get_all(
				"Bin", filters={"item_code": item_code},
				fields=["actual_qty", "valuation_rate"]) or []:
			qty = float(b.get("actual_qty") or 0)
			if qty <= 0:
				continue
			qty_total += qty
			value_total += qty * float(b.get("valuation_rate") or 0)
		if qty_total and value_total:
			return value_total / qty_total
		for field in ("valuation_rate", "last_purchase_rate"):
			rate = float(frappe.db.get_value("Item", item_code, field) or 0)
			if rate:
				return rate
		return None
	except Exception:  # noqa: BLE001 — a cost we cannot read must not sink the panel
		frappe.log_error(
			title=f"local unit cost read failed ({item_code})",
			message=frappe.get_traceback())
		return None


@frappe.whitelist()
def preview_delete(item_codes) -> dict:
	"""Preview a force_delete: per-doctype counts of rows that would be touched.

	Args:
	    item_codes: A list, JSON-encoded list, or comma-separated string.

	Returns:
	    {item_codes, submitted: [{doctype, count}] (must be cancelled
	    first), drafts: [{doctype, count}] (deleted without cancel),
	    simple: [{doctype, count}] (direct delete), missing: [...]}.

	Permissions:
	    System Manager.
	"""
	_check_perm()
	codes = _normalize_codes(item_codes)
	if not codes:
		return {"item_codes": [], "submitted": [], "drafts": [], "simple": [], "missing": []}

	missing = [c for c in codes if not frappe.db.exists("Item", c)]
	codes = [c for c in codes if c not in missing]

	# All codes were missing → nothing exists to scan; return early.
	if not codes:
		return {"item_codes": [], "submitted": [], "drafts": [], "simple": [], "missing": missing}

	submitted: list[dict] = []
	drafts: list[dict] = []
	for child_dt, where, parent_dt in _LINKED_SUBMITTABLE:
		if not frappe.db.table_exists(child_dt):
			continue
		# Group by docstatus on the parent so we can show submitted vs draft.
		rows = frappe.db.sql(
			f"""
			SELECT p.name, p.docstatus
			FROM `tab{child_dt}` c
			JOIN `tab{parent_dt}` p ON p.name = c.parent
			WHERE c.{where}
			GROUP BY p.name, p.docstatus
			""",
			{"codes": tuple(codes)},
			as_dict=True,
		)
		s_count = sum(1 for r in rows if r["docstatus"] == 1)
		d_count = sum(1 for r in rows if r["docstatus"] == 0)
		c_count = sum(1 for r in rows if r["docstatus"] == 2)
		if s_count:
			submitted.append({"doctype": parent_dt, "count": s_count})
		if d_count:
			drafts.append({"doctype": parent_dt, "count": d_count})
		if c_count:
			# Cancelled docs are still linked but won't block delete (we'll just
			# remove the Item; cancelled docs retain their cancelled-row history).
			pass

	simple: list[dict] = []
	for dt, where in _LINKED_SIMPLE:
		if not frappe.db.table_exists(dt):
			continue
		n = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tab{dt}` WHERE {where}",
			{"codes": tuple(codes)},
		)[0][0]
		if n:
			simple.append({"doctype": dt, "count": n})

	return {
		"item_codes": codes,
		"submitted": submitted,   # [{doctype, count}] — must be cancelled first
		"drafts": drafts,         # [{doctype, count}] — will be deleted (no cancel needed)
		"simple": simple,         # [{doctype, count}] — direct delete
		"missing": missing,
	}


@frappe.whitelist()
def force_delete(item_codes, cancel_submitted: bool | int | str = False) -> dict:
	"""DESTRUCTIVE: cancel referencing docs, drop links, delete Items outright.

	Cancels referencing submitted docs, deletes referencing drafts, wipes
	simple linked rows (Stock Ledger, Serial No, Bin, Batch, Item Price,
	FFL Acquisition/Disposition), then deletes the Items. Cancelled-doc
	history rows are left alone — they're audit trail.

	Args:
	    item_codes: A list, JSON-encoded list, or comma-separated string.
	    cancel_submitted: Must be truthy to proceed when submitted docs
	        reference the Items; the UI re-prompts with True.

	Returns:
	    {ok, deleted: [...], cancelled: [{doctype, name}], deleted_drafts:
	    [{doctype, name}], simple: [{doctype, count}], errors: [...],
	    missing: [...]} — ok is False when any per-row step errored.

	Raises:
	    ValidationError: cancel_submitted is falsy and submitted docs exist.

	Permissions:
	    System Manager only (destructive).
	"""
	_check_perm()
	codes = _normalize_codes(item_codes)
	if not codes:
		return {"ok": True, "deleted": [], "cancelled": [], "deleted_drafts": [], "simple": [], "errors": []}

	cancel_submitted = bool(int(cancel_submitted)) if isinstance(cancel_submitted, str) else bool(cancel_submitted)

	preview = preview_delete(codes)
	# Drop codes that don't exist — preview already enumerated them as missing.
	codes = [c for c in codes if c not in (preview.get("missing") or [])]
	if not codes:
		return {"ok": True, "deleted": [], "cancelled": [], "deleted_drafts": [], "simple": [], "errors": [], "missing": preview.get("missing") or []}
	if preview["submitted"] and not cancel_submitted:
		labels = ", ".join(f"{d['doctype']} ({d['count']})" for d in preview["submitted"])
		frappe.throw(_(
			"Item is referenced by submitted documents: {0}. Pass cancel_submitted=True to cancel them automatically."
		).format(labels))

	cancelled: list[dict] = []
	deleted_drafts: list[dict] = []
	simple_summary: list[dict] = []
	errors: list[str] = []

	# 1. Cancel submitted parents (in declared cancel order) and delete drafts.
	for child_dt, where, parent_dt in _LINKED_SUBMITTABLE:
		if not frappe.db.table_exists(child_dt):
			continue
		rows = frappe.db.sql(
			f"""
			SELECT DISTINCT p.name, p.docstatus
			FROM `tab{child_dt}` c
			JOIN `tab{parent_dt}` p ON p.name = c.parent
			WHERE c.{where} AND p.docstatus < 2
			""",
			{"codes": tuple(codes)},
			as_dict=True,
		)
		for r in rows:
			try:
				doc = frappe.get_doc(parent_dt, r["name"])
				if r["docstatus"] == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
					cancelled.append({"doctype": parent_dt, "name": r["name"]})
				elif r["docstatus"] == 0:
					frappe.delete_doc(parent_dt, r["name"], force=1, ignore_permissions=True)
					deleted_drafts.append({"doctype": parent_dt, "name": r["name"]})
			except Exception as e:
				errors.append(f"{parent_dt} {r['name']}: {e}")

	# 2. Delete simple referenced rows (Stock Ledger, Serial No, Bin, Batch,
	#    Item Price, FFL Acq/Disp). Cancelled-doc-history rows (docstatus=2)
	#    on submittables are left alone — they're audit trail.
	for dt, where in _LINKED_SIMPLE:
		if not frappe.db.table_exists(dt):
			continue
		try:
			n_before = frappe.db.sql(
				f"SELECT COUNT(*) FROM `tab{dt}` WHERE {where}",
				{"codes": tuple(codes)},
			)[0][0]
			if not n_before:
				continue
			# For Serial No / Batch / FFL Acq/Disp / Item Price use ORM delete
			# so on_trash hooks fire. Stock Ledger Entry + Bin we wipe directly
			# because they're internal ledger rows that wouldn't normally be
			# deleted via UI.
			if dt in {"Stock Ledger Entry", "Bin"}:
				frappe.db.sql(
					f"DELETE FROM `tab{dt}` WHERE {where}",
					{"codes": tuple(codes)},
				)
			else:
				names = frappe.db.sql_list(
					f"SELECT name FROM `tab{dt}` WHERE {where}",
					{"codes": tuple(codes)},
				)
				for name in names:
					try:
						frappe.delete_doc(dt, name, force=1, ignore_permissions=True, ignore_on_trash=True)
					except Exception as e:
						errors.append(f"{dt} {name}: {e}")
			simple_summary.append({"doctype": dt, "count": n_before})
		except Exception as e:
			errors.append(f"{dt} (bulk): {e}")

	# 3. Finally delete the Items themselves.
	deleted: list[str] = []
	for code in codes:
		try:
			frappe.delete_doc("Item", code, force=1, ignore_permissions=True, ignore_on_trash=True)
			deleted.append(code)
		except Exception as e:
			errors.append(f"Item {code}: {e}")

	frappe.db.commit()
	return {
		"ok": not errors,
		"deleted": deleted,
		"cancelled": cancelled,
		"deleted_drafts": deleted_drafts,
		"simple": simple_summary,
		"errors": errors,
	}
