"""Inventory Count (stocktake) page backend.

Staff walk the shop and scan: a firearm's FastBound label (its barcode is the
serial number) or a non-serialized item's UPC (each scan adds 1). The count is
compared with what the system says is on the shelf.

* **Non-serialized items** are corrected in one step: ``finalize`` posts ONE
  Stock Reconciliation for the rows staff leave ticked.
* **Firearms are reported, never adjusted.** A missing gun may need an ATF
  theft/loss report and a person has to look into it, so a serialized item never
  goes into the reconciliation, no FFL Disposition is created and FastBound is
  never called.

Every scan is its own ``Inventory Count Entry`` row (insert-and-delete only), so
several devices can scan into the same count at once; counted quantities are
summed at query time. Write endpoints take a row lock on the count first, which
also serialises them against ``finalize``.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, formatdate, now_datetime

from ffl_core.ffl_core.doctype.inventory_count_entry.inventory_count_entry import serial_key
from ffl_core.roles import COUNT_ROLES, STOCK_ROLES
from ffl_core.utils import commit
from ffl_core.warehouses import (
	consignment_warehouse_names,
	custody_warehouse_names,
	default_stock_warehouse,
)

_ENTRY = "Inventory Count Entry"
_SAVEPOINT = "ffl_inventory_count_finalize"


# --------------------------------------------------------------------------- #
# Count + scope
# --------------------------------------------------------------------------- #

def _count_row(count: str, *, lock: bool = False, open_only: bool = False) -> frappe._dict:
	"""The count's header row. ``lock`` takes a row lock (a locking read sees the
	latest committed status, which a plain read inside a long transaction may not)."""
	row = frappe.db.get_value(
		"Inventory Count", (count or "").strip(),
		["name", "company", "status", "include_custody", "creation", "stock_reconciliation"],
		as_dict=True, for_update=lock)
	if not row:
		frappe.throw(_("Inventory Count {0} was not found.").format(count))
	if open_only and row.status != "Open":
		frappe.throw(_("{0} is {1}, so nothing more can be scanned into it. "
			"Start a new count to keep going.").format(row.name, row.status.lower()))
	return row


def _eligible_warehouses(company: str, include_custody: bool, selected=None) -> list[str]:
	"""Warehouses a count may cover: this company's own stock locations. Never
	Transit (goods not here yet) and never an outbound-consignment warehouse (not
	on our shelf); customer-custody ones only when asked for."""
	names = set(frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0, "disabled": 0}, pluck="name"))
	names -= set(frappe.get_all(
		"Warehouse", filters={"company": company, "warehouse_type": "Transit"}, pluck="name"))
	names -= consignment_warehouse_names()
	if not include_custody:
		names -= custody_warehouse_names()
	if selected:
		names &= set(selected)
	return sorted(names)


def _scope(row: frappe._dict) -> frappe._dict:
	"""The warehouses (always a list) and item groups (None = all) a count covers.
	Item groups include their descendants."""
	selected = frappe.get_all(
		"Inventory Count Warehouse",
		filters={"parent": row.name, "parenttype": "Inventory Count"}, pluck="warehouse")
	warehouses = _eligible_warehouses(row.company, bool(row.include_custody), selected)
	chosen = frappe.get_all(
		"Inventory Count Item Group",
		filters={"parent": row.name, "parenttype": "Inventory Count"}, pluck="item_group")
	groups = None
	if chosen:
		groups = set()
		for lft, rgt in frappe.db.sql(
				"SELECT lft, rgt FROM `tabItem Group` WHERE name IN %s", (tuple(chosen),)):
			groups.update(frappe.get_all(
				"Item Group", filters=[["lft", ">=", lft], ["rgt", "<=", rgt]], pluck="name"))
		groups = sorted(groups)
	return frappe._dict(warehouses=warehouses, groups=groups)


def _in_group_scope(scope: frappe._dict, item_group: str | None) -> bool:
	return scope.groups is None or item_group in scope.groups


def _is_manager() -> bool:
	return bool(set(frappe.get_roles()) & set(STOCK_ROLES))


def _parse_list(value) -> list:
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip() else []
	return [v for v in (value or []) if v]


# --------------------------------------------------------------------------- #
# Endpoints: counts
# --------------------------------------------------------------------------- #

@frappe.whitelist()
def get_scope_options(company: str | None = None) -> dict:
	"""What the New Count dialog can offer: the company and its countable warehouses.

	Args:
	    company: Company to list warehouses for; defaults to the user's company.

	Returns:
	    {company, warehouses: [name], default_warehouse}.
	"""
	frappe.only_for(COUNT_ROLES)
	company = (company or "").strip() or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		frappe.throw(_("No company is set up yet."))
	return {
		"company": company,
		"warehouses": _eligible_warehouses(company, True),
		"default_warehouse": default_stock_warehouse(company),
	}


@frappe.whitelist()
def create_count(company: str | None = None, warehouses=None, item_groups=None,
		include_custody=1, notes: str | None = None) -> dict:
	"""Start a stocktake.

	With no warehouses and no item groups it covers the whole store: every
	warehouse that holds the store's own stock, every item group. Anything outside
	the scope never shows up as a difference.

	Args:
	    company: Company to count; defaults to the user's company.
	    warehouses: Warehouse names (list or JSON) to limit the count to; empty = all.
	    item_groups: Item Group names (list or JSON) to limit the count to
	        (children included); empty = all.
	    include_custody: 1 to count customer-custody warehouses too, 0 to leave them out.
	    notes: Optional free text.

	Returns:
	    {name, company, warehouses: [name], item_groups: [name]}.
	"""
	frappe.only_for(COUNT_ROLES)
	company = (company or "").strip() or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		frappe.throw(_("No company is set up yet."))
	selected = _parse_list(warehouses)
	groups = _parse_list(item_groups)
	include_custody = cint(include_custody)
	eligible = _eligible_warehouses(company, bool(include_custody))
	bad = sorted(set(selected) - set(eligible))
	if bad:
		frappe.throw(_("These warehouses cannot be counted: {0}. Pick this company's own "
			"stock warehouses (not Transit, consignment or disabled ones).").format(", ".join(bad)))
	if not eligible:
		frappe.throw(_("There is no warehouse to count for {0}.").format(company))
	doc = frappe.get_doc({
		"doctype": "Inventory Count", "company": company, "status": "Open",
		"include_custody": include_custody, "notes": notes or "",
		"warehouses": [{"warehouse": w} for w in selected],
		"item_groups": [{"item_group": g} for g in groups],
	})
	doc.flags.ignore_permissions = True
	doc.flags.via_inventory_count = True
	doc.insert()
	return {"name": doc.name, "company": company, "warehouses": selected, "item_groups": groups}


@frappe.whitelist()
def get_counts() -> list[dict]:
	"""The most recent counts, newest first, with how many entries each holds.

	Returns:
	    [{name, status, company, creation, owner, finalized_at, stock_reconciliation,
	    entries, warehouses: [name], item_groups: [name]}].
	"""
	frappe.only_for(COUNT_ROLES)
	rows = frappe.get_all(
		"Inventory Count",
		fields=["name", "status", "company", "creation", "owner", "finalized_at",
			"stock_reconciliation"],
		order_by="creation desc", limit=50)
	if not rows:
		return []
	names = tuple(r.name for r in rows)
	entries = dict(frappe.db.sql(
		f"SELECT inventory_count, COUNT(*) FROM `tab{_ENTRY}` "
		"WHERE inventory_count IN %s GROUP BY inventory_count", (names,)))
	scope: dict[str, dict[str, list]] = {n: {"warehouses": [], "item_groups": []} for n in names}
	for dt, field, key in (("Inventory Count Warehouse", "warehouse", "warehouses"),
			("Inventory Count Item Group", "item_group", "item_groups")):
		for parent, value in frappe.db.sql(
				f"SELECT parent, `{field}` FROM `tab{dt}` WHERE parent IN %s", (names,)):
			scope[parent][key].append(value)
	for r in rows:
		r["entries"] = entries.get(r.name, 0)
		r.update(scope[r.name])
	return rows


@frappe.whitelist()
def cancel_count(count: str) -> dict:
	"""Cancel an open count without adjusting anything.

	For a wrong start or an abandoned walk-through. The scans are kept for the record.

	Args:
	    count: The Inventory Count name.

	Returns:
	    {ok}.
	"""
	frappe.only_for(STOCK_ROLES)
	row = _count_row(count, lock=True, open_only=True)
	frappe.db.set_value("Inventory Count", row.name, "status", "Cancelled")
	return {"ok": True}


# --------------------------------------------------------------------------- #
# Endpoints: scanning
# --------------------------------------------------------------------------- #

def _find_serial(code: str) -> frappe._dict | None:
	# Serial No names compare case-insensitively (the column collation), so a label
	# scanned in lower case still lands on the real record; the real name comes back.
	rows = frappe.db.sql(
		"SELECT name, item_code, status, warehouse FROM `tabSerial No` WHERE name = %s LIMIT 1",
		(code,), as_dict=True)
	return rows[0] if rows else None


def _find_item_by_upc(code: str) -> str | None:
	rows = frappe.db.sql(
		"""SELECT b.parent FROM `tabItem Barcode` b
		   JOIN `tabItem` i ON i.name = b.parent
		   WHERE b.barcode = %s ORDER BY i.disabled, b.idx LIMIT 1""", (code,))
	return rows[0][0] if rows else None


def _serial_entry(count: str, serial: str, *, lock: bool = False) -> frappe._dict | None:
	return frappe.db.get_value(
		_ENTRY, {"inventory_count": count, "serial_key": serial_key(count, serial)},
		["name", "owner", "creation", "method"], as_dict=True, for_update=lock)


def _insert_entry(count: str, **fields) -> frappe._dict:
	doc = frappe.get_doc({"doctype": _ENTRY, "inventory_count": count, **fields})
	doc.flags.ignore_permissions = True
	doc.flags.via_inventory_count = True
	doc.insert()
	return doc


def _qty_tally(count: str, item_code: str, warehouse: str, lock: bool = True) -> tuple[float, int]:
	"""(counted so far, number of entries) for one item in one warehouse.
	Locking (the default, for the write endpoints): a plain SUM may read an older snapshot
	than the rows another device committed while this request waited for the count's row lock."""
	total, n = frappe.db.sql(
		f"""SELECT COALESCE(SUM(qty_delta), 0), COUNT(*) FROM `tab{_ENTRY}`
		    WHERE inventory_count = %s AND kind = 'qty' AND item_code = %s AND warehouse = %s
		    {"FOR UPDATE" if lock else ""}""", (count, item_code, warehouse))[0]
	return flt(total), n


def _last_qty_entry(count: str, item_code: str, warehouse: str, lock: bool = False) -> tuple:
	"""(name, qty_delta) of the newest quantity entry for one item, or (None, None)."""
	last = frappe.db.sql(
		f"""SELECT name, qty_delta FROM `tab{_ENTRY}`
		    WHERE inventory_count = %s AND kind = 'qty' AND item_code = %s AND warehouse = %s
		    ORDER BY creation DESC, name DESC LIMIT 1 {"FOR UPDATE" if lock else ""}""",
		(count, item_code, warehouse))
	return (last[0][0], flt(last[0][1])) if last else (None, None)


def _can_see_order_refs() -> bool:
	"""Order and invoice numbers on a held gun are shown only to who the Item form's
	Pending pill shows them to (reservations.reserved_counter_serials)."""
	from ffl_core.api.manual_order import _has_ship_role

	return _has_ship_role()


def _shelf_label(state: dict, show_refs: bool = False) -> str:
	"""A short human reason a gun is not simply 'on the shelf' (from
	reservations.serial_shelf_state), or '' when nothing special applies. Order
	references are left out unless ``show_refs``."""
	reason, detail = state.get("reason"), state.get("detail") or {}
	if reason == "custody":
		return _("Customer's gun (held for pickup)")
	if reason == "consigned_out":
		return _("Out on consignment")
	if reason == "sold_unknown":
		return _("May have sold on GunBroker")
	if reason == "channel_reserved":
		ref = detail.get("ref") if show_refs else ""
		return _("Held for {0} order {1}").format(detail.get("channel") or "", ref or "").strip()
	if reason == "counter_hold":
		return (detail.get("tooltip") if show_refs else "") or _("On an open counter order")
	if reason == "not_active":
		return _("Already sold or removed ({0})").format(detail.get("status") or "")
	if reason == "no_warehouse":
		return _("Not in any warehouse (already shipped out)")
	if reason == "not_found":
		return _("Serial is no longer on file")
	return ""


def _scan_serial(row, scope, serial: frappe._dict) -> dict:
	from ffl_core.reservations import serial_shelf_state

	existing = _serial_entry(row.name, serial.name, lock=True)
	item = frappe.db.get_value("Item", serial.item_code, ["item_name", "item_group"], as_dict=True) or {}
	expected = (serial.status == "Active" and serial.warehouse in scope.warehouses
		and _in_group_scope(scope, item.get("item_group")))
	out = {"serial": serial.name, "item_code": serial.item_code, "item_name": item.get("item_name"),
		"warehouse": serial.warehouse, "expected": expected}
	if existing:
		return {**out, "result": "already", "entry": existing.name,
			"message": _("{0} is already counted.").format(serial.name)}
	try:
		entry = _insert_entry(
			row.name, kind="serial", method="scan", serial_no=serial.name,
			item_code=serial.item_code, warehouse=serial.warehouse)
	except frappe.UniqueValidationError:
		# Another device got there in the same instant; the unique key caught it.
		return {**out, "result": "already",
			"message": _("{0} is already counted.").format(serial.name)}
	out.update(result="serial", entry=entry.name, message=_("Counted {0}.").format(serial.name))
	if not expected:
		label = _shelf_label(serial_shelf_state(serial.name), _can_see_order_refs()) or _("It is not on this count's shelf list")
		out["warning"] = _("{0} was counted, but it should not be here: {1}.").format(serial.name, label)
	return out


def _scan_item(row, scope, item_code: str, warehouse: str | None) -> dict:
	item = frappe.db.get_value(
		"Item", item_code,
		["item_name", "item_group", "is_stock_item", "has_serial_no", "has_batch_no", "disabled"],
		as_dict=True)
	base = {"item_code": item_code, "item_name": item.item_name}
	reason = _skip_reason(item)
	if reason:
		return {**base, "result": "error", "message": _("{0} cannot be counted here: {1}.").format(
			item.item_name, reason)}
	if not _in_group_scope(scope, item.item_group):
		return {**base, "result": "error", "message": _(
			"{0} is in {1}, which is not part of this count.").format(item.item_name, item.item_group)}
	warehouse = _resolve_warehouse(row, scope, warehouse)
	entry = _insert_entry(row.name, kind="qty", method="scan", item_code=item_code,
		warehouse=warehouse, qty_delta=1)
	counted = _qty_tally(row.name, item_code, warehouse)[0]
	return {**base, "result": "item", "warehouse": warehouse, "counted": counted, "entry": entry.name,
		"last_delta": 1,
		"message": _("{0}: {1} counted.").format(item.item_name, f"{counted:g}")}


def _resolve_warehouse(row, scope, warehouse: str | None) -> str:
	warehouse = (warehouse or "").strip()
	if warehouse:
		if warehouse not in scope.warehouses:
			frappe.throw(_("{0} is not part of this count.").format(warehouse))
		return warehouse
	if len(scope.warehouses) == 1:
		return scope.warehouses[0]
	default = default_stock_warehouse(row.company)
	if default in scope.warehouses:
		return default
	frappe.throw(_("Pick the warehouse you are counting in, then scan again."))


@frappe.whitelist()
def scan(count: str, code: str, warehouse: str | None = None) -> dict:
	"""Record one scan: a firearm's serial number or an item's barcode.

	The code is tried as a serial number first (case and surrounding spaces ignored),
	then as an item barcode (UPC). A serial adds the gun once; scanning it again changes nothing. A UPC adds 1 to
	that item in ``warehouse``. A code that matches neither is saved as an unknown
	barcode so it shows in the report.

	Args:
	    count: The Inventory Count name; must be open.
	    code: What the scanner typed.
	    warehouse: Where a UPC scan is being counted. Optional when the count covers
	        one warehouse or the store's default warehouse is part of it.

	Returns:
	    {result: serial|already|item|unknown|error, message, serial, item_code,
	    item_name, warehouse, expected, counted, entry (the row to undo), warning}. ``result`` error
	    means nothing was saved (the message says why).
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count, lock=True, open_only=True)
	code = (code or "").strip()
	if not code:
		return {"result": "error", "message": _("Nothing was scanned.")}
	scope = _scope(row)
	serial = _find_serial(code)
	if serial:
		return _scan_serial(row, scope, serial)
	item_code = _find_item_by_upc(code)
	if item_code:
		return _scan_item(row, scope, item_code, warehouse)
	entry = _insert_entry(row.name, kind="unknown", method="scan", code=code)
	return {"result": "unknown", "entry": entry.name, "code": code, "message": _(
		"{0} is not a serial number or barcode the system knows. It is saved in the "
		"report; if it is a product, add its barcode to the item.").format(code)}


@frappe.whitelist()
def set_qty(count: str, item_code: str, warehouse: str, qty) -> dict:
	"""Type in the counted quantity for an item, for something with no barcode.

	Saved as the difference from what is already counted, so scans from other
	devices at the same moment are not overwritten.

	Args:
	    count: The Inventory Count name; must be open.
	    item_code: A non-serialized stock item.
	    warehouse: The warehouse it was counted in; must be part of the count.
	    qty: The quantity found, 0 or more.

	Returns:
	    {ok, counted, entry, last_entry, last_delta}; entry is null (and nothing
	    changes) when the item was already counted at this quantity.
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count, lock=True, open_only=True)
	qty = flt(qty)
	if qty < 0:
		frappe.throw(_("Enter the quantity you counted (0 or more)."))
	scope = _scope(row)
	item = frappe.db.get_value(
		"Item", item_code,
		["item_name", "item_group", "is_stock_item", "has_serial_no", "has_batch_no", "disabled"],
		as_dict=True)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(item_code))
	reason = _skip_reason(item)
	if reason:
		frappe.throw(_("{0} cannot be counted here: {1}.").format(item.item_name, reason))
	if not _in_group_scope(scope, item.item_group):
		frappe.throw(_("{0} is not part of this count.").format(item.item_name))
	warehouse = _resolve_warehouse(row, scope, warehouse)
	current, entries = _qty_tally(row.name, item_code, warehouse)
	if entries and abs(qty - current) < 1e-9:
		return {"ok": True, "counted": current, "entry": None, "last_entry": None, "last_delta": None}
	# A first entry of 0 is saved too (qty_delta 0): it is what makes "none on the
	# shelf" a count rather than an item nobody walked to.
	entry = _insert_entry(row.name, kind="qty", method="manual", item_code=item_code,
		warehouse=warehouse, qty_delta=qty - current)
	return {"ok": True, "counted": qty, "entry": entry.name, "last_entry": entry.name,
		"last_delta": qty - current}


def _delete_entry(entry: frappe._dict) -> None:
	if entry.owner != frappe.session.user and not _is_manager():
		frappe.throw(_("{0} added that, so only they or a Stock Manager can remove it.").format(entry.owner))
	frappe.db.delete(_ENTRY, {"name": entry.name})


@frappe.whitelist()
def toggle_serial(count: str, serial: str, found) -> dict:
	"""Tick or untick a gun by hand, for a gun whose label will not scan.

	Args:
	    count: The Inventory Count name; must be open.
	    serial: The serial number.
	    found: 1 to mark the gun as counted, 0 to remove its count. Taking away
	        someone else's scan needs a Stock Manager.

	Returns:
	    {ok, found, entry}.
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count, lock=True, open_only=True)
	sn = _find_serial((serial or "").strip())
	if not sn:
		frappe.throw(_("Serial {0} was not found.").format(serial))
	existing = _serial_entry(row.name, sn.name, lock=True)
	if cint(found):
		if existing:
			return {"ok": True, "found": True, "entry": existing.name}
		try:
			entry = _insert_entry(row.name, kind="serial", method="manual", serial_no=sn.name,
				item_code=sn.item_code, warehouse=sn.warehouse)
		except frappe.UniqueValidationError:
			return {"ok": True, "found": True, "entry": None}
		return {"ok": True, "found": True, "entry": entry.name}
	if existing:
		_delete_entry(existing)
	return {"ok": True, "found": False, "entry": None}


@frappe.whitelist()
def undo(entry: str) -> dict:
	"""Remove one scan or typed entry from an open count.

	Args:
	    entry: The Inventory Count Entry name. Already gone counts as done.

	Returns:
	    {ok, kind, serial_no, code, item_code, warehouse, counted, last_entry,
	    last_delta}: what the removal leaves behind (for a quantity entry, the item's
	    new counted total and its newest remaining entry). Only the person who made the
	    entry, or a Stock Manager, may remove it.
	"""
	frappe.only_for(COUNT_ROLES)
	e = frappe.db.get_value(
		_ENTRY, entry, ["name", "inventory_count", "owner", "kind", "serial_no", "code",
			"item_code", "warehouse"], as_dict=True)
	if not e:
		return {"ok": True, "kind": None}
	_count_row(e.inventory_count, lock=True, open_only=True)
	_delete_entry(e)
	out = {"ok": True, "kind": e.kind, "serial_no": e.serial_no, "code": e.code,
		"item_code": e.item_code, "warehouse": e.warehouse}
	if e.kind == "qty":
		out["counted"], _n = _qty_tally(e.inventory_count, e.item_code, e.warehouse)
		out["last_entry"], out["last_delta"] = _last_qty_entry(
			e.inventory_count, e.item_code, e.warehouse, lock=True)
	return out


# --------------------------------------------------------------------------- #
# State + variance
# --------------------------------------------------------------------------- #

def _entries_summary(count: str) -> frappe._dict:
	"""Everything scanned so far, folded: gun -> who/when, (item, warehouse) -> counted,
	plus the unknown barcodes. One read; lazy: all of a count's rows each time, fine for
	a few thousand entries — upgrade is a GROUP BY once counts routinely pass ~10k rows."""
	rows = frappe.db.sql(
		f"""SELECT name, kind, method, item_code, serial_no, code, warehouse, qty_delta, owner, creation
		    FROM `tab{_ENTRY}` WHERE inventory_count = %s ORDER BY creation, name""",
		(count,), as_dict=True)
	found: dict[str, dict] = {}
	qty: dict[tuple[str, str], dict] = {}
	unknown: list[dict] = []
	for r in rows:
		if r.kind == "serial":
			found[r.serial_no] = {"entry": r.name, "by": r.owner, "at": str(r.creation),
				"method": r.method, "item_code": r.item_code}
		elif r.kind == "qty":
			q = qty.setdefault((r.item_code, r.warehouse), {"counted": 0.0})
			q["counted"] = flt(q["counted"] + flt(r.qty_delta), 6)
			q.update(last_entry=r.name, last_delta=flt(r.qty_delta), last_at=r.creation)
		else:
			unknown.append({"entry": r.name, "code": r.code, "by": r.owner, "at": str(r.creation)})
	return frappe._dict(found=found, qty=qty, unknown=unknown, total=len(rows))


def _expected_serials(scope: frappe._dict) -> list[dict]:
	"""Every serialized unit the system says is on the shelf in scope: Active, in a
	scoped warehouse (same basis as the Firearms In Stock report). Each carries why it
	might not be simply on the shelf (held for an order, a customer's gun, ...)."""
	if not scope.warehouses:
		return []
	from ffl_core.custody import custody_serials
	from ffl_core.reservations import counter_holds, serial_shelf_state, shelf_state_columns

	cols = ", ".join(f"sn.`{c}`" for c in shelf_state_columns())
	params: dict = {"wh": tuple(scope.warehouses)}
	group_sql = ""
	if scope.groups is not None:
		if not scope.groups:
			return []
		group_sql = "AND i.item_group IN %(groups)s"
		params["groups"] = tuple(scope.groups)
	guns = frappe.db.sql(
		f"""SELECT sn.name AS serial_no, {cols}, i.item_name,
		           COALESCE(NULLIF(sn.manufacturer, ''), i.manufacturer) AS manufacturer,
		           COALESCE(NULLIF(sn.model_name, ''), i.model_name) AS model_name,
		           COALESCE(NULLIF(sn.caliber, ''), i.caliber) AS caliber
		    FROM `tabSerial No` sn
		    JOIN `tabItem` i ON i.name = sn.item_code AND i.has_serial_no = 1
		    WHERE sn.status = 'Active' AND sn.warehouse IN %(wh)s {group_sql}
		    ORDER BY manufacturer, model_name, sn.name""", params, as_dict=True)
	# lazy: one counter_holds() per distinct Item (a handful of queries each) — fine
	# for a few hundred models; upgrade is one batched hold query across items.
	holds: dict[str, dict] = {}
	in_custody = custody_serials([g.item_code for g in guns])
	show_refs = _can_see_order_refs()
	out = []
	for g in guns:
		if g.item_code not in holds:
			holds[g.item_code] = counter_holds(g.item_code)
		state = serial_shelf_state(g.serial_no, row=g, holds=holds[g.item_code])
		if state["sellable"] and g.serial_no in in_custody:
			state = {"reason": "custody", "detail": {}}
		out.append({
			"serial": g.serial_no, "item_code": g.item_code, "item_name": g.item_name,
			"manufacturer": g.manufacturer or "", "model_name": g.model_name or "",
			"caliber": g.caliber or "", "warehouse": g.warehouse,
			"reason": state.get("reason") or "", "label": _shelf_label(state, show_refs),
			"public_label": _shelf_label(state),
		})
	return out


def _expected_items(scope: frappe._dict) -> dict[tuple[str, str], dict]:
	"""Stock the system holds per (item, warehouse) for non-serialized stock items in
	scope. Disabled and batch-tracked items are left out (a reconciliation cannot take
	them); a scanned one is reported as skipped instead."""
	if not scope.warehouses or scope.groups == []:
		return {}
	params: dict = {"wh": tuple(scope.warehouses)}
	group_sql = ""
	if scope.groups is not None:
		group_sql = "AND i.item_group IN %(groups)s"
		params["groups"] = tuple(scope.groups)
	rows = frappe.db.sql(
		f"""SELECT b.item_code, b.warehouse, b.actual_qty, i.item_name, i.item_group
		    FROM `tabBin` b JOIN `tabItem` i ON i.name = b.item_code
		    WHERE b.warehouse IN %(wh)s AND b.actual_qty != 0
		      AND i.is_stock_item = 1 AND i.has_serial_no = 0 AND i.has_batch_no = 0 AND i.disabled = 0 {group_sql}""",
		params, as_dict=True)
	return {(r.item_code, r.warehouse): {
		"item_code": r.item_code, "item_name": r.item_name, "item_group": r.item_group,
		"warehouse": r.warehouse, "expected": flt(r.actual_qty, 3)} for r in rows}


def _item_rows(scope: frappe._dict, summary: frappe._dict) -> list[dict]:
	"""One row per (item, warehouse): expected next to counted, with the UPC to show
	and the last entry to undo."""
	rows = _expected_items(scope)
	for (item_code, warehouse), q in summary.qty.items():
		if (item_code, warehouse) not in rows:
			item = frappe.db.get_value("Item", item_code, ["item_name", "item_group"], as_dict=True) or {}
			rows[(item_code, warehouse)] = {
				"item_code": item_code, "item_name": item.get("item_name") or item_code,
				"item_group": item.get("item_group"), "warehouse": warehouse, "expected": 0.0}
	upc = {}
	if rows:
		upc = dict(frappe.db.sql(
			"SELECT parent, MIN(barcode) FROM `tabItem Barcode` WHERE parent IN %s GROUP BY parent",
			(tuple({k[0] for k in rows}),)))
	out = []
	for key, r in rows.items():
		q = summary.qty.get(key)
		out.append({**r, "upc": upc.get(r["item_code"]) or "",
			"counted": q["counted"] if q else 0.0,
			"last_entry": q["last_entry"] if q else None,
			"last_delta": q["last_delta"] if q else None})
	out.sort(key=lambda r: (r["item_name"] or "", r["warehouse"]))
	return out


def _movements(baseline: dict, as_of) -> tuple[dict, dict]:
	"""What the stock ledger and the POS did to each (item, warehouse) since the count
	last looked at it. ``baseline`` maps key -> that moment (the key's last entry, or the
	count's creation for a key never scanned). All figures are signed like ledger
	entries: a sale is negative, a return positive.

	Returns (moved, later):
	  * moved: what sold or arrived while staff were still walking the shop — what the
	    counted quantity is missing.
	  * later: movement after ``as_of`` that the Bin already holds. The reconciliation is
	    posted AT ``as_of``, where it did not exist yet, so ``Bin - later`` is the stock at
	    that moment — what ERPNext compares the counted figure with.

	Two rules decide when something happened "after the scan":
	  * An ordinary ledger entry counts when it was RECORDED after the scan (its creation),
	    not when it is dated: a Purchase Receipt back-dated to the supplier's invoice and
	    entered after the count is goods that reached the shelf after the count. Whether it
	    is after ``as_of`` is a question of its date, because that is where the
	    reconciliation sits in the ledger.
	  * A POS sale is placed by the POS Invoice's own date and time, never by the consolidated
	    Sales Invoice's ledger entries (dated at the register close, hours after the sale, or
	    back-dated by the sweep). Those entries are left out of the ledger pass and the POS
	    lines stand in for them; a sale rung up at or before the scan is already in the
	    counted quantity. A POS sale not yet consolidated is not in the Bin and is left out
	    here altogether: finalize refuses to adjust an item that has one (_open_register_sales).
	# lazy: one pass over each table for the scoped items since the earliest baseline,
	filtered per key in Python; upgrade is a derived-table join if counts get huge.
	"""
	moved: dict = {}
	later: dict = {}
	if not baseline:
		return moved, later
	items = tuple({k[0] for k in baseline})
	warehouses = tuple({k[1] for k in baseline})
	lowest = min(baseline.values())

	def add(target: dict, key, qty) -> None:
		target[key] = flt(target.get(key, 0.0) + flt(qty), 6)

	for item_code, warehouse, at, created, qty in frappe.db.sql(
			"""SELECT sle.item_code, sle.warehouse, sle.posting_datetime, sle.creation, sle.actual_qty
			   FROM `tabStock Ledger Entry` sle
			   LEFT JOIN `tabSales Invoice` si ON si.name = sle.voucher_no AND sle.voucher_type = 'Sales Invoice'
			   WHERE sle.is_cancelled = 0 AND (sle.creation > %s OR sle.posting_datetime > %s)
			     AND sle.item_code IN %s AND sle.warehouse IN %s
			     AND IFNULL(si.is_consolidated, 0) = 0""", (lowest, as_of, items, warehouses)):
		key = (item_code, warehouse)
		if key not in baseline:
			continue
		if at > as_of:
			add(later, key, qty)
		elif created > baseline[key]:
			add(moved, key, qty)
	for table, qty_col in (("POS Invoice Item", "stock_qty"), ("Packed Item", "qty")):
		for item_code, warehouse, posted, qty in frappe.db.sql(
				f"""SELECT c.item_code, c.warehouse, TIMESTAMP(p.posting_date, p.posting_time), c.`{qty_col}`
				    FROM `tabPOS Invoice` p JOIN `tab{table}` c ON c.parent = p.name
				    WHERE p.docstatus = 1 AND c.docstatus = 1 AND c.parenttype = 'POS Invoice'
				      AND IFNULL(p.consolidated_invoice, '') != ''
				      AND TIMESTAMP(p.posting_date, p.posting_time) > %s
				      AND c.item_code IN %s AND c.warehouse IN %s""", (lowest, items, warehouses)):
			key = (item_code, warehouse)
			if key not in baseline or posted <= baseline[key]:
				continue
			if posted > as_of:
				add(later, key, -qty)
			else:
				add(moved, key, -qty)
	return moved, later


def _open_register_sales(keys, as_of) -> dict:
	"""{(item, warehouse): [{invoice, posted, qty}]} for submitted POS Invoices rung up at
	or before ``as_of`` that have not been consolidated (``qty`` in stock units, negative
	on a return). ERPNext posts a POS sale to stock only when its register closes, so the
	Bin still holds those units and a quantity set now would be wrong by exactly that
	much — whichever way it is worked out."""
	out: dict = {}
	if not keys:
		return out
	items = tuple({k[0] for k in keys})
	warehouses = tuple({k[1] for k in keys})
	for table, qty_col in (("POS Invoice Item", "stock_qty"), ("Packed Item", "qty")):
		for name, item_code, warehouse, posted, qty in frappe.db.sql(
				f"""SELECT p.name, c.item_code, c.warehouse, TIMESTAMP(p.posting_date, p.posting_time),
				           c.`{qty_col}`
				    FROM `tabPOS Invoice` p JOIN `tab{table}` c ON c.parent = p.name
				    WHERE p.docstatus = 1 AND c.docstatus = 1 AND c.parenttype = 'POS Invoice'
				      AND IFNULL(p.consolidated_invoice, '') = ''
				      AND TIMESTAMP(p.posting_date, p.posting_time) <= %s
				      AND c.item_code IN %s AND c.warehouse IN %s""", (as_of, items, warehouses)):
			if (item_code, warehouse) in keys:
				out.setdefault((item_code, warehouse), []).append(
					{"invoice": name, "posted": posted, "qty": flt(qty)})
	return out


def _register_message(open_sales: dict, rows) -> str | None:
	"""The refusal for rows that cannot be adjusted while register sales are open, or None."""
	hit = {(r["item_code"], r["warehouse"]): open_sales[(r["item_code"], r["warehouse"])]
		for r in rows if (r["item_code"], r["warehouse"]) in open_sales}
	if not hit:
		return None
	invoices = {s["invoice"]: s["posted"] for sales in hit.values() for s in sales}
	names = sorted({r["item_name"] for r in rows if (r["item_code"], r["warehouse"]) in hit})
	return _("{0} register sales from {1} are not closed yet (items: {2}). Close the register — "
		"or wait for the nightly close at 11 PM — then finalize. Scanning is not affected.").format(
			len(invoices), formatdate(min(invoices.values())), ", ".join(names[:8])
			+ ("…" if len(names) > 8 else ""))


def _skip_reason(i) -> str | None:
	"""Why an item cannot be counted or reconciled here, or None (one predicate for the
	scan, the typed quantity and the variance)."""
	if i.disabled:
		return _("the item is disabled")
	if i.has_serial_no:
		return _("it is tracked by serial number — scan the serial on the item itself")
	if i.has_batch_no:
		return _("it is tracked by batch — set its quantity by hand with a Stock Reconciliation")
	if not i.is_stock_item:
		return _("it is not a stock item")
	return None


def _row_rate(item_code: str, warehouse: str, on_hand: float) -> float:
	"""The valuation rate for a reconciliation row. Where the warehouse holds stock the
	reconciliation must keep ITS rate, or posting would revalue what is already there;
	otherwise (found stock in an empty bin) the item's own cost, as Adjust Quantity does.
	0 means there is no cost on file."""
	from ffl_core.api.item_admin import local_unit_cost

	if on_hand > 0:
		rate = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse},
			"valuation_rate"))
		if rate:
			return rate
	return flt(local_unit_cost(item_code))


def _variance_items(row, scope: frappe._dict, summary: frappe._dict, as_of=None) -> tuple[list, list]:
	"""(rows, skipped). Rows: every non-serialized (item, warehouse) in scope whose
	counted quantity differs from the system's. An item never scanned counts as 0.
	Skipped: items a reconciliation cannot take — scanned but disabled, batch-tracked, no
	longer a stock item or now serialized; or counted ABOVE the books with no cost on file
	(a write-up at zero cost would put stock on the books for nothing) — listed so staff
	adjust them by hand while the rest still posts.

	Sales during the count are allowed for. The quantity the reconciliation sets is
	``counted + what the stock ledger and the POS did after the item was last scanned``
	(see _movements), floored at 0, and the "system" quantity it is compared with is the
	stock AT ``as_of``. Each row carries the register sales still open on it
	(``open_register_sales``): those are not in the Bin yet, so the row cannot be adjusted
	until the register closes.
	# lazy: assumes an item is counted in one go; if its counting is spread over a
	long stretch the figure is approximate. Upgrade: lock the item while it is counted.
	"""
	as_of = as_of or now_datetime()
	expected = _expected_items(scope)
	keys = set(expected) | set(summary.qty)
	if not keys:
		return [], []
	info = {}
	scanned_items = tuple({k[0] for k in summary.qty})
	if scanned_items:
		for i in frappe.db.sql(
				"""SELECT name, item_name, disabled, is_stock_item, has_serial_no, has_batch_no
				   FROM `tabItem` WHERE name IN %s""", (scanned_items,), as_dict=True):
			info[i.name] = i
	skipped = []
	for key in sorted(summary.qty):
		i = info.get(key[0])
		reason = _skip_reason(i) if i else _("the item no longer exists")
		if reason:
			keys.discard(key)
			skipped.append({"item_code": key[0], "item_name": i.item_name if i else key[0],
				"warehouse": key[1], "counted": summary.qty[key]["counted"], "reason": reason})
	baseline = {k: summary.qty[k]["last_at"] if k in summary.qty else row.creation for k in keys}
	moved, later = _movements(baseline, as_of)
	open_sales = _open_register_sales(keys, as_of)
	bins = {}
	if keys - set(expected):  # scanned, but nothing in the Bin the expected list saw
		for item_code, warehouse, qty in frappe.db.sql(
				"SELECT item_code, warehouse, actual_qty FROM `tabBin` WHERE item_code IN %s AND warehouse IN %s",
				(tuple({k[0] for k in keys}), tuple({k[1] for k in keys}))):
			bins[(item_code, warehouse)] = flt(qty, 3)
	out = []
	for key in sorted(keys):
		exp = expected.get(key)
		# The stock at the moment the reconciliation is dated: the Bin, less what posted after as_of.
		on_hand = flt((exp["expected"] if exp else bins.get(key, 0.0)) - later.get(key, 0.0), 3)
		q = summary.qty.get(key)
		counted = q["counted"] if q else 0.0
		target = max(0.0, flt(counted + moved.get(key, 0.0), 3))
		if abs(target - on_hand) < 1e-9:
			continue
		item_name = exp["item_name"] if exp else info[key[0]].item_name
		rate = _row_rate(key[0], key[1], on_hand)
		if target > on_hand and not rate:
			skipped.append({"item_code": key[0], "item_name": item_name, "warehouse": key[1],
				"counted": counted, "reason": _("no cost on file — set the item's valuation rate, "
					"then adjust by hand")})
			continue
		out.append({"item_code": key[0], "item_name": item_name,
			"warehouse": key[1], "expected": on_hand, "counted": counted,
			"moved": flt(moved.get(key, 0.0), 3), "adjust_to": target, "rate": rate,
			"difference": flt(target - on_hand, 3), "never_scanned": q is None,
			"open_register_sales": [{"invoice": x["invoice"], "posted": str(x["posted"])}
				for x in open_sales.get(key, [])]})
	out.sort(key=lambda r: (r["item_name"] or "", r["warehouse"]))
	return out, skipped


def _variance_serials(scope: frappe._dict, summary: frappe._dict, expected: list[dict]) -> dict:
	"""Missing (expected, not found), unexpected (scanned, not on the shelf list) and
	unknown barcodes — reported only, never adjusted."""
	from ffl_core.reservations import serial_shelf_state

	expected_names = {g["serial"] for g in expected}
	show_refs = _can_see_order_refs()
	missing = [g for g in expected if g["serial"] not in summary.found]
	unexpected = []
	for serial, f in summary.found.items():
		if serial in expected_names:
			continue
		state = serial_shelf_state(serial)
		unexpected.append({"serial": serial, "item_code": f["item_code"],
			"item_name": frappe.db.get_value("Item", f["item_code"], "item_name") or f["item_code"],
			"label": _shelf_label(state, show_refs) or _("Not on this count's shelf list"),
			"public_label": _shelf_label(state) or _("Not on this count's shelf list"), "by": f["by"]})
	unknown: dict[str, dict] = {}
	for u in summary.unknown:
		g = unknown.setdefault(u["code"], {"code": u["code"], "times": 0})
		g["times"] += 1
	return {"missing": missing, "unexpected": sorted(unexpected, key=lambda r: r["serial"]),
		"unknown": sorted(unknown.values(), key=lambda r: r["code"])}


def _public_report(report: dict) -> dict:
	"""The serial report as it is stored: labels without order or invoice references."""
	def plain(rows):
		return [{**{k: v for k, v in r.items() if k != "public_label"}, "label": r["public_label"]}
			for r in rows]

	return {**report, "missing": plain(report["missing"]), "unexpected": plain(report["unexpected"])}


@frappe.whitelist()
def get_state(count: str, counts_only=0) -> dict:
	"""Everything the page shows for one count.

	Args:
	    count: The Inventory Count name.
	    counts_only: 1 for the light version the page polls every few seconds: only
	        what has been scanned (found guns, counted quantities, unknown barcodes).
	        A cursor ("changes since") cannot show an Undo, so this re-sends the
	        whole folded tally, which is small.

	Returns:
	    {status, server_time, found: {serial: {entry, by, at, method, item_code}},
	    counted: [{item_code, warehouse, counted, last_entry, last_delta}],
	    unknown: [{entry, code, by, at}]} and, unless counts_only, also {company,
	    warehouses, default_warehouse, stock_reconciliation, serials: [expected guns
	    with label], items: [{item_code, item_name, warehouse, upc, expected, counted,
	    last_entry, last_delta}]}.
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count)
	summary = _entries_summary(row.name)
	out = {
		"status": row.status, "server_time": str(now_datetime()), "found": summary.found,
		"counted": [{"item_code": k[0], "warehouse": k[1], "counted": q["counted"],
			"last_entry": q["last_entry"], "last_delta": q["last_delta"]}
			for k, q in summary.qty.items()],
		"unknown": summary.unknown,
	}
	if cint(counts_only):
		return out
	scope = _scope(row)
	out.update(
		company=row.company, warehouses=scope.warehouses, item_groups=scope.groups,
		default_warehouse=default_stock_warehouse(row.company),
		stock_reconciliation=row.stock_reconciliation,
		serials=_expected_serials(scope), items=_item_rows(scope, summary))
	return out


@frappe.whitelist()
def get_item_row(count: str, item_code: str, warehouse: str) -> dict:
	"""One row of the Items list, for an item another device (or a typed entry) just
	added that this page has no row for yet — cheaper than reloading the whole state.

	Args:
	    count: The Inventory Count name.
	    item_code: The item.
	    warehouse: The warehouse it was counted in.

	Returns:
	    {item_code, item_name, item_group, warehouse, expected, upc, counted, last_entry,
	    last_delta}.
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count)
	item = frappe.db.get_value("Item", item_code, ["item_name", "item_group"], as_dict=True)
	if not item:
		frappe.throw(_("Item {0} was not found.").format(item_code))
	counted, entries = _qty_tally(row.name, item_code, warehouse, lock=False)
	last_entry, last_delta = _last_qty_entry(row.name, item_code, warehouse)
	return {"item_code": item_code, "item_name": item.item_name, "item_group": item.item_group,
		"warehouse": warehouse, "upc": frappe.db.get_value("Item Barcode", {"parent": item_code}, "barcode") or "",
		"expected": flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"), 3),
		"counted": counted if entries else 0.0, "last_entry": last_entry, "last_delta": last_delta}


@frappe.whitelist()
def variance(count: str) -> dict:
	"""The difference between what was counted and what the system says.

	Non-serialized rows are what ``finalize`` would adjust. Firearms are only ever
	listed. A finalized count returns the report saved when it was finalized.

	Args:
	    count: The Inventory Count name.

	Returns:
	    {status, stock_reconciliation, serial: {missing, unexpected, unknown},
	    items: [{item_code, item_name, warehouse, expected, counted, moved, adjust_to,
	    difference, never_scanned, open_register_sales: [{invoice, posted}]}],
	    skipped: [{item_code, item_name, warehouse, counted, reason}], quantity_counted,
	    entries}. ``moved`` is what sold or arrived since the item was counted.
	    ``skipped`` items cannot be reconciled (adjust them by hand).
	    ``open_register_sales`` lists POS sales not yet closed: a row with any cannot be
	    adjusted (finalize refuses) until the register closes. ``quantity_counted`` is
	    false for a firearms-only count, which finishes with the firearm report alone.
	"""
	frappe.only_for(COUNT_ROLES)
	row = _count_row(count)
	if row.status == "Finalized":
		saved = frappe.db.get_value("Inventory Count", row.name, "serial_report")
		return {"status": row.status, "stock_reconciliation": row.stock_reconciliation,
			"serial": json.loads(saved) if saved else {"missing": [], "unexpected": [], "unknown": []},
			"items": [], "skipped": [], "quantity_counted": False, "entries": 0}
	scope = _scope(row)
	summary = _entries_summary(row.name)
	items, skipped = _variance_items(row, scope, summary)
	return {
		"status": row.status, "stock_reconciliation": row.stock_reconciliation,
		"serial": _variance_serials(scope, summary, _expected_serials(scope)),
		"items": items, "skipped": skipped, "entries": summary.total,
		"quantity_counted": bool(summary.qty)}


# --------------------------------------------------------------------------- #
# Finalize
# --------------------------------------------------------------------------- #

@frappe.whitelist()
def finalize(count: str, item_rows=None) -> dict:
	"""Finalize a count: adjust the non-serialized differences and save the firearm report.

	Posts one Stock Reconciliation for the non-serialized differences, dated the moment
	the figures were worked out, so anything sold afterwards stacks on top of it.
	Refused, with nothing posted, while a register sale (POS Invoice not yet closed) is
	open on any row being adjusted: ERPNext takes that stock out only when the register
	closes, so a quantity set now would be wrong by exactly that much.
	Firearms are never adjusted here, no disposition is created and FastBound is not
	called: a missing gun needs a person to look into it. A count with no quantity
	entries at all (firearms only) finishes without a reconciliation.

	Args:
	    count: The Inventory Count name; must be open and have at least one scan.
	    item_rows: The rows to adjust, as [{item_code, warehouse}] (list or JSON), so
	        staff can untick rows on the page. Left out = every row with a difference
	        EXCEPT items nobody scanned: those are only adjusted (to 0) when listed
	        here. Quantities are always worked out again here, never taken from the caller.

	Returns:
	    {ok, stock_reconciliation, adjusted: [{item_code, warehouse, expected,
	    adjust_to}], skipped: [{item_code, item_name, warehouse, counted, reason}],
	    serial: {missing, unexpected, unknown}}.
	"""
	frappe.only_for(STOCK_ROLES)
	from ffl_core.api.cost_correction import (
		_reject_if_period_frozen,
		_stock_adjustment_accounts,
	)

	# REPEATABLE READ: a transaction's snapshot is fixed by its first plain SELECT, and
	# this request already ran some (session, roles) before reaching here — so once the
	# count lock is granted, a plain read would still show the data as it was BEFORE the
	# wait, missing scans a device committed just ahead of us. Ending the transaction
	# here (nothing is written yet) makes the lock read below the first statement of a
	# fresh one; every read after it takes its snapshot after the lock is ours, and the
	# scans (which hold that same lock until they commit) are all visible.
	commit()
	row = _count_row(count, lock=True, open_only=True)
	# The moment the figures are worked out, and the moment the reconciliation is posted
	# at. Stock Ledger Entries dated after it land on top of the reconciliation instead of
	# being counted twice, so the figures leave them out. lazy: an entry dated before this
	# moment but committed after our reads is missed — a window of milliseconds.
	as_of = now_datetime()
	scope = _scope(row)
	summary = _entries_summary(row.name)
	# The checks that need no writing come first, so a refusal leaves nothing behind.
	if not summary.total:
		frappe.throw(_("Nothing has been counted yet. Scan something first — finalizing an "
			"empty count would write every item off to zero."))
	items, skipped = _variance_items(row, scope, summary, as_of)
	if not summary.qty:
		items = []  # firearms only: no quantity was counted, so nothing to adjust
	elif item_rows is None:
		items = [r for r in items if not r["never_scanned"]]
	else:
		chosen = {(r.get("item_code"), r.get("warehouse")) for r in _parse_list(item_rows)}
		items = [r for r in items if (r["item_code"], r["warehouse"]) in chosen]
	posting_date = as_of.date().isoformat()
	if items:
		_reject_if_period_frozen(row.company, posting_date, "a stock count adjustment")
		expense_account, cost_center = _stock_adjustment_accounts(row.company)
		blocked = _register_message(
			{(r["item_code"], r["warehouse"]): r["open_register_sales"] for r in items if r["open_register_sales"]},
			items)
		if blocked:
			frappe.throw(blocked)
	serial_report = _variance_serials(scope, summary, _expected_serials(scope))

	frappe.db.savepoint(_SAVEPOINT)
	try:
		sr_name = None
		if items:
			sr_rows = [{"item_code": r["item_code"], "warehouse": r["warehouse"],
				"qty": r["adjust_to"], "valuation_rate": r["rate"],
				# Only ever a decrease at a zero-cost bin: an increase with no cost is set aside above.
				"allow_zero_valuation_rate": 0 if r["rate"] else 1} for r in items]
			sr = frappe.get_doc({
				"doctype": "Stock Reconciliation", "company": row.company,
				"purpose": "Stock Reconciliation", "set_posting_time": 1,
				"posting_date": posting_date, "posting_time": as_of.strftime("%H:%M:%S.%f"),
				"expense_account": expense_account, "cost_center": cost_center, "items": sr_rows})
			sr.flags.ignore_permissions = True
			sr.insert()
			sr.submit()
			sr.add_comment("Comment", _("Posted by inventory count {0}, finalized by {1}.").format(
				row.name, frappe.session.user))
			sr_name = sr.name
		# Saved without order references: the report is read later by anyone with the count.
		frappe.db.set_value("Inventory Count", row.name, {
			"status": "Finalized", "stock_reconciliation": sr_name,
			"finalized_by": frappe.session.user, "finalized_at": now_datetime(),
			"serial_report": json.dumps(_public_report(serial_report), default=str)})
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		raise
	return {"ok": True, "stock_reconciliation": sr_name, "serial": serial_report, "skipped": skipped,
		"adjusted": [{k: r[k] for k in ("item_code", "warehouse", "expected", "adjust_to")}
			for r in items]}
