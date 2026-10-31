"""In-store storage locations: where things sit in the shop.

A TRACKING layer on top of the one sellable warehouse. Nothing here creates a
Warehouse, a Stock Entry, a Stock Ledger Entry or a GL entry: stock and money stay
exactly where they were, and a location change never touches them.

State lives in three places, and ``move()`` is the only function that changes any
of them -- each change writes one ``Storage Move`` log row in the SAME transaction:

* ``Serial No.storage_location``  -- where one firearm sits.
* ``Storage Location Qty``        -- how many units of a non-serialized item sit in a
  location (Open locations only).
* ``Storage Move``                -- the append-only log.

Two kinds of location, set by the zone:

* **Slots**  -- one firearm per position, and only a firearm.
* **Open**   -- no limit; firearms and everything else.

"Unassigned" is never stored: it is the warehouse's stock minus what the locations
hold (``unassigned_*``). A negative number (a sale took stock the locations still
claim) is a "to confirm" condition, not an error.

Location tracking never blocks a sale or a stock movement: the sale hooks take
what the location really holds and leave the rest to the "to confirm" list.
"""
from __future__ import annotations

import json
import math
import re

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, format_datetime, get_datetime

from ffl_core.utils import commit, split_serials

from ffl_core.warehouses import consignment_warehouse_names, custody_warehouse_names

KIND_SLOTS = "Slots"
KIND_OPEN = "Open"

_QTY_DT = "Storage Location Qty"
_PRECISION = 3
# lazy: one zone create / add is capped so a typo ("3000") cannot mint thousands of
# documents in one request; raise the cap if a store ever needs a bigger zone.
MAX_POSITIONS_PER_CALL = 500
# How a two-sided Slots zone's numbers run on the map (Storage Zone.numbering; the first is the
# default): like house numbers, odd down one side and even facing them, or in order. One side or
# three and more always count in order.
NUMBERINGS = ("Odd / even", "In order")
_ZONE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,38}$")


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def location_row(name: str, *, lock: bool = False) -> frappe._dict:
	"""A location's header, or a plain-English error. ``lock`` takes a row lock (a
	locking read sees the latest committed state, a plain one may not)."""
	name = (name or "").strip()
	row = frappe.db.get_value(
		"Storage Location", name,
		["name", "company", "zone", "label", "kind", "disabled", "position"],
		as_dict=True, for_update=lock) if name else None
	if not row:
		frappe.throw(_("Storage location {0} was not found.").format(name or "—"))
	# A location in a disabled zone is disabled everywhere; the zone's own switch never
	# rewrites the locations' (so re-enabling the zone cannot undo a position disabled alone).
	row.disabled = 1 if (row.disabled or frappe.db.get_value("Storage Zone", row.zone, "disabled")) else 0
	return row


def excluded_warehouses() -> set[str]:
	"""Warehouses that are NOT our own shelves: an outbound-consignment warehouse, customer
	custody, Transit (goods not here yet) and disabled ones. The ONE definition: a gun in one
	of these has left our shelf, and stock in one is not ours to put in a location."""
	names = consignment_warehouse_names() | custody_warehouse_names()
	names |= set(frappe.get_all("Warehouse", filters={"warehouse_type": "Transit"}, pluck="name"))
	names |= set(frappe.get_all("Warehouse", filters={"disabled": 1}, pluck="name"))
	return names


def sellable_warehouses(company: str) -> list[str]:
	"""The company's own shelves: its non-group warehouses minus :func:`excluded_warehouses`."""
	own = set(frappe.get_all("Warehouse", filters={"company": company, "is_group": 0}, pluck="name"))
	return sorted(own - excluded_warehouses())


def on_our_shelf(status, warehouse, consigned_out=None, *, excluded: set | None = None) -> bool:
	"""Is this gun an Active one on our own shelf: not sold, not flagged or warehoused as out
	on consignment, not in custody / Transit / a disabled warehouse. Pass ``excluded`` (from
	:func:`excluded_warehouses`) when looping."""
	from ffl_core.warehouses import is_consigned_out
	if status != "Active" or not warehouse:
		return False
	excluded = excluded_warehouses() if excluded is None else excluded
	return warehouse not in excluded and not is_consigned_out(
		consigned_out=consigned_out, warehouse=warehouse, names=excluded)


def gun_in_slot(location: str, *, lock: bool = False, exclude: str | None = None) -> str | None:
	"""The Active gun sitting in ``location`` (a Slots position), if any."""
	sql = ("SELECT name FROM `tabSerial No` WHERE storage_location = %s AND status = 'Active'"
		+ (" AND name != %s" if exclude else "") + " LIMIT 1" + (" FOR UPDATE" if lock else ""))
	rows = frappe.db.sql(sql, (location, exclude) if exclude else (location,))
	return rows[0][0] if rows else None


def qty_at(location: str, item_code: str, *, lock: bool = False) -> float:
	from ffl_core.ffl_core.doctype.storage_location_qty.storage_location_qty import qty_row_name
	return flt(frappe.db.get_value(
		_QTY_DT, qty_row_name(location, item_code), "qty", for_update=lock) or 0)


def located_qty(company: str, item_codes=None, *, lock: bool = False) -> dict[str, float]:
	"""{item_code: units the company's locations hold}. ``lock`` reads the latest committed
	rows (and locks them) instead of this transaction's snapshot."""
	sql = "SELECT item_code, SUM(qty) FROM `tabStorage Location Qty` WHERE company = %s AND qty > 0"
	args: list = [company]
	if item_codes:
		sql += " AND item_code IN %s"
		args.append(tuple(item_codes))
	return {code: flt(q) for code, q in frappe.db.sql(
		sql + " GROUP BY item_code" + (" FOR UPDATE" if lock else ""), args)}


def stock_qty(company: str, item_codes=None, *, lock: bool = False) -> dict[str, float]:
	"""{item_code: units in the company's sellable warehouses} for non-serialized
	stock items. ``lock`` reads the latest Bin rows instead of this transaction's snapshot."""
	warehouses = sellable_warehouses(company)
	if not warehouses:
		return {}
	sql = ("SELECT b.item_code, SUM(b.actual_qty) FROM `tabBin` b "
		"JOIN `tabItem` i ON i.name = b.item_code "
		"WHERE b.warehouse IN %s AND IFNULL(i.has_serial_no, 0) = 0 AND i.is_stock_item = 1")
	args: list = [tuple(warehouses)]
	if item_codes:
		sql += " AND b.item_code IN %s"
		args.append(tuple(item_codes))
	return {code: flt(q) for code, q in frappe.db.sql(
		sql + " GROUP BY b.item_code" + (" FOR UPDATE" if lock else ""), args)}


def unassigned_qty(company: str, item_code: str, *, lock: bool = False) -> float:
	"""Warehouse stock of a non-serialized item that is in no location. Negative =
	the locations claim more than the warehouse holds (to confirm)."""
	have = stock_qty(company, [item_code], lock=lock).get(item_code, 0.0)
	return flt(have - located_qty(company, [item_code], lock=lock).get(item_code, 0.0), _PRECISION)


def sole_location(info: dict) -> str | None:
	"""THE rule for preselecting where loose stock comes from: exactly one location holds
	the item and nothing of it is unassigned. Anything else needs the clerk's word. Shared
	by the POS cart, Create Order and the Woo / GunBroker Dispose dialog."""
	locs = (info or {}).get("locations") or []
	if len(locs) == 1 and flt((info or {}).get("unassigned")) <= 0:
		return locs[0]["location"]
	return None


def item_locations(company: str, item_codes: list[str]) -> dict[str, dict]:
	"""For pickers: per item, the ENABLED locations that hold it (qty > 0; a disabled location
	or one in a disabled zone is never offered, so never auto-picked) and the
	units in no location. ``{item: {"locations": [{location, label, zone, qty}],
	"unassigned": n, "total": n, "sole": the one place it can only have come from, or None}}``."""
	item_codes = [c for c in dict.fromkeys(item_codes or []) if c]
	out = {c: {"locations": [], "unassigned": 0.0, "total": 0.0, "sole": None} for c in item_codes}
	if not item_codes:
		return out
	rows = frappe.db.sql(
		"SELECT q.item_code, q.location, l.label, l.zone, q.qty FROM `tabStorage Location Qty` q "
		"JOIN `tabStorage Location` l ON l.name = q.location "
		"JOIN `tabStorage Zone` z ON z.name = l.zone "
		"WHERE q.company = %s AND q.qty > 0 AND q.item_code IN %s "
		"AND l.disabled = 0 AND z.disabled = 0 ORDER BY l.zone, l.position",
		(company, tuple(item_codes)), as_dict=True)
	for r in rows:
		out[r.item_code]["locations"].append(
			{"location": r.location, "label": r.label, "zone": r.zone, "qty": flt(r.qty)})
	have = stock_qty(company, item_codes)
	# Units sitting in a disabled place are not offered as a source, but they are still in a
	# location: "unassigned" counts every held unit.
	located = located_qty(company, item_codes)
	for code in item_codes:
		held = located.get(code, 0.0)
		out[code]["total"] = flt(have.get(code, 0.0), _PRECISION)
		out[code]["unassigned"] = flt(have.get(code, 0.0) - held, _PRECISION)
		out[code]["sole"] = sole_location(out[code])
	return out


def parse_picks(locations) -> dict[str, str]:
	"""A Dispose call's ``locations`` ({order line key: location}, dict or JSON string) as a
	clean {str: str}, blanks dropped."""
	if isinstance(locations, str):
		locations = frappe.parse_json(locations) if locations.strip() else {}
	return {str(k): (v or "").strip() for k, v in (locations or {}).items() if (v or "").strip()}


def dispose_pick_lines(company: str, lines: list[dict]) -> list[dict]:
	"""What the Dispose dialog asks for each loose-stock line of a web / GunBroker order.

	``lines``: [{line, item_code, qty, chosen?}]. Returns them with ``item_name``,
	``locations``, ``unassigned`` and ``chosen`` (a location already recorded for the line,
	else the sole place the item can only have come from)."""
	info = item_locations(company, [l["item_code"] for l in lines])
	names = {r.name: r.item_name for r in frappe.get_all(
		"Item", filters={"name": ("in", [l["item_code"] for l in lines] or [""])},
		fields=["name", "item_name"])}
	return [{"line": str(l["line"]), "item_code": l["item_code"],
		"item_name": names.get(l["item_code"]) or l["item_code"], "qty": l["qty"],
		"locations": info[l["item_code"]]["locations"], "unassigned": info[l["item_code"]]["unassigned"],
		"chosen": l.get("chosen") or info[l["item_code"]]["sole"]} for l in lines]


def unassigned_counts(company: str) -> dict:
	"""How many things are in no location / over-claimed -- counts only, for the map header."""
	warehouses = sellable_warehouses(company)
	guns = cint(frappe.db.sql(
		"SELECT COUNT(*) FROM `tabSerial No` WHERE status = 'Active' AND warehouse IN %s "
		"AND IFNULL(storage_location, '') = ''", (tuple(warehouses),))[0][0]) if warehouses else 0
	have, held = stock_qty(company), located_qty(company)
	diffs = [flt(have.get(c, 0.0) - held.get(c, 0.0), _PRECISION) for c in set(have) | set(held)]
	return {"guns": guns, "items": sum(1 for d in diffs if d > 0), "to_confirm": sum(1 for d in diffs if d < 0)}


def unassigned_summary(company: str) -> dict:
	"""Everything not in a location, and everything the locations over-claim.

	``guns``: Active guns in the company's warehouses with no location.
	``items``: non-serialized stock in the warehouse but in no location (qty > 0).
	``to_confirm``: items whose locations claim MORE than the warehouse holds, i.e.
	something was sold or shipped without saying which location it came from
	(``over`` = how many).
	"""
	warehouses = sellable_warehouses(company)
	guns = []
	if warehouses:
		guns = frappe.db.sql(
			"SELECT sn.name AS serial, sn.item_code, i.item_name, sn.warehouse, sn.manufacturer, "
			"sn.model_name, sn.caliber FROM `tabSerial No` sn JOIN `tabItem` i ON i.name = sn.item_code "
			"WHERE sn.status = 'Active' AND sn.warehouse IN %s AND IFNULL(sn.storage_location, '') = '' "
			"ORDER BY i.item_name, sn.name", (tuple(warehouses),), as_dict=True)
	have = stock_qty(company)
	held = located_qty(company)
	# lazy: lists every item that needs a place or a word; paginate if a shop ever has thousands.
	diff_codes = [c for c in set(have) | set(held)
		if flt(have.get(c, 0.0) - held.get(c, 0.0), _PRECISION) != 0]
	names = {r.name: r.item_name for r in frappe.get_all(
		"Item", filters={"name": ("in", diff_codes or [""])}, fields=["name", "item_name"])}
	items, to_confirm = [], []
	for code in sorted(diff_codes):
		diff = flt(have.get(code, 0.0) - held.get(code, 0.0), _PRECISION)
		row = {"item_code": code, "item_name": names.get(code) or code,
			"stock": flt(have.get(code, 0.0)), "located": flt(held.get(code, 0.0))}
		if diff > 0:
			items.append({**row, "unassigned": diff})
		else:
			to_confirm.append({**row, "over": -diff})
	return {"guns": guns, "items": items, "to_confirm": to_confirm}


# --------------------------------------------------------------------------- #
# The one writer
# --------------------------------------------------------------------------- #

def _log(*, kind, reason, company, serial_no=None, item_code=None, qty=None,
		from_location=None, to_location=None, voucher_type=None, voucher_no=None,
		voucher_detail=None, note=None) -> str:
	doc = frappe.get_doc({
		"doctype": "Storage Move", "kind": kind, "reason": reason, "company": company,
		"serial_no": serial_no, "item_code": item_code, "qty": qty,
		"from_location": from_location or None, "to_location": to_location or None,
		"voucher_type": voucher_type, "voucher_no": voucher_no,
		"voucher_detail": voucher_detail, "note": note,
	})
	doc.flags.via_storage = True
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _add_qty(location: str, company: str, item_code: str, delta: float) -> float:
	"""Add ``delta`` to a (location, item) row under a row lock; returns the new
	quantity. Never leaves a negative."""
	from ffl_core.ffl_core.doctype.storage_location_qty.storage_location_qty import qty_row_name
	name = qty_row_name(location, item_code)
	current = frappe.db.get_value(_QTY_DT, name, "qty", for_update=True)
	if current is None:
		if delta < 0:
			frappe.throw(_("{0} holds none of {1}.").format(location, item_code))
		doc = frappe.get_doc({"doctype": _QTY_DT, "company": company, "location": location,
			"item_code": item_code, "qty": flt(delta, _PRECISION)})
		doc.flags.via_storage = True
		doc.flags.ignore_permissions = True
		try:
			doc.insert()
		except frappe.DuplicateEntryError:
			# Another request created the row between our read and insert: take the
			# lock on it and add to what is there.
			current = frappe.db.get_value(_QTY_DT, name, "qty", for_update=True)
			return _add_qty(location, company, item_code, delta) if current is not None else \
				frappe.throw(_("Could not update {0}, please try again.").format(location))
		return flt(delta, _PRECISION)
	new = flt(flt(current) + delta, _PRECISION)
	if new < 0:
		frappe.throw(_("{0} holds only {1} of {2}.").format(location, flt(current), item_code))
	frappe.db.set_value(_QTY_DT, name, "qty", new, update_modified=True)
	return new


def move(*, reason: str, serial_no: str | None = None, item_code: str | None = None,
		qty: float | None = None, from_location: str | None = None,
		to_location: str | None = None, voucher_type: str | None = None,
		voucher_no: str | None = None, voucher_detail: str | None = None,
		note: str | None = None, allow_short: bool = False,
		check_unassigned: bool = False) -> frappe._dict | None:
	"""Change where something sits. THE only writer of location state.

	A firearm: pass ``serial_no``; ``to_location`` None clears it (it left the
	store's locations). A quantity of a non-serialized item: pass ``item_code`` and
	``qty``; ``from_location`` None means "from the units in no location",
	``to_location`` None means "out" (sold, shipped, gone). The log row and the
	Serial No / quantity row change in the same transaction, and no stock or
	accounting document is ever created.

	``allow_short`` (sales and shipments): take what the location really holds
	instead of refusing -- a stale pick must never block a sale; the remainder shows
	on the "to confirm" list. ``check_unassigned`` (hand moves): refuse to place
	more than is unassigned.

	Returns ``{"move": <log name>, "qty": <moved>, "kind": ...}`` or None when
	there was nothing to do (already there, or nothing left to take).
	"""
	if reason not in ("receive", "move", "sale", "ship", "out", "return", "count", "adjust", "confirm"):
		frappe.throw(_("Unknown location change reason {0}.").format(reason))
	if serial_no:
		return _move_serial(reason, serial_no, to_location, voucher_type, voucher_no,
			voucher_detail, note)
	return _move_qty(reason, item_code, flt(qty), from_location, to_location, voucher_type,
		voucher_no, voucher_detail, note, allow_short, check_unassigned)


def _move_serial(reason, serial_no, to_location, voucher_type, voucher_no, voucher_detail, note):
	sn = frappe.db.get_value(
		"Serial No", (serial_no or "").strip(),
		["name", "item_code", "status", "warehouse", "company", "storage_location", "consigned_out"],
		as_dict=True, for_update=True)  # held through the move and its log row
	if not sn:
		frappe.throw(_("Serial number {0} is not on file.").format(serial_no))
	current = (sn.storage_location or "").strip() or None
	to_location = (to_location or "").strip() or None
	if current == to_location:
		return None
	if to_location:
		loc = location_row(to_location, lock=True)
		if loc.disabled:
			frappe.throw(_("{0} is disabled. Pick another location.").format(loc.label))
		if not _in_our_stock(sn):
			frappe.throw(_("{0} is not in the store's stock, so it cannot be put in {1}.").format(
				sn.name, loc.label))
		if sn.company and loc.company != sn.company:
			frappe.throw(_("{0} belongs to a different company than {1}.").format(sn.name, loc.label))
		if loc.kind == KIND_SLOTS:
			from ffl_core.firearm import is_firearm_item
			if not is_firearm_item(sn.item_code):
				frappe.throw(_("{0} is a single-firearm slot, and {1} is not a firearm. "
					"Use a display case, rack or safe instead.").format(loc.label, sn.name))
			other = gun_in_slot(to_location, lock=True, exclude=sn.name)
			if other:
				frappe.throw(_("{0} already holds {1}. A slot holds one firearm: pick a free slot, "
					"or move {1} out first.").format(loc.label, other))
		company = loc.company
	else:
		company = sn.company
	frappe.db.set_value("Serial No", sn.name, "storage_location", to_location, update_modified=False)
	name = _log(kind="serial", reason=reason, company=company, serial_no=sn.name,
		item_code=sn.item_code, qty=1, from_location=current, to_location=to_location,
		voucher_type=voucher_type, voucher_no=voucher_no, voucher_detail=voucher_detail, note=note)
	return frappe._dict(move=name, qty=1, kind="serial")


def _move_qty(reason, item_code, qty, from_location, to_location, voucher_type, voucher_no,
		voucher_detail, note, allow_short, check_unassigned):
	item_code = (item_code or "").strip()
	from_location = (from_location or "").strip() or None
	to_location = (to_location or "").strip() or None
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} was not found.").format(item_code or "—"))
	if qty <= 0:
		frappe.throw(_("The quantity to move must be more than zero."))
	if frappe.db.get_value("Item", item_code, "has_serial_no"):
		frappe.throw(_("{0} is tracked by serial number. Move it by scanning its serial number.")
			.format(item_code))
	if from_location and from_location == to_location:
		return None
	if not from_location and not to_location:
		frappe.throw(_("A move needs a place to take from or put into."))
	# One writer at a time per item: placing from the unassigned units reads stock minus what
	# the locations hold, and two requests must not both see the same free units. The item
	# row is the key (always taken first, then the locations in a fixed order).
	frappe.db.get_value("Item", item_code, "name", for_update=True)
	rows = {n: location_row(n, lock=True) for n in sorted(filter(None, {from_location, to_location}))}
	company = (rows[to_location] if to_location else rows[from_location]).company
	if from_location and to_location and rows[from_location].company != rows[to_location].company:
		frappe.throw(_("{0} and {1} belong to different companies.").format(
			rows[from_location].label, rows[to_location].label))
	if to_location:
		dest = rows[to_location]
		# Units coming BACK (a return, a cancelled sale) go where they came from even if that
		# place was disabled since: refusing would strand them in "unassigned" for no reason.
		if dest.disabled and reason != "return":
			frappe.throw(_("{0} is disabled. Pick another location.").format(dest.label))
		if dest.kind == KIND_SLOTS:
			frappe.throw(_("{0} is a single-firearm slot, and {1} is not a firearm. "
				"Use a display case, rack or safe instead.").format(dest.label, item_code))
	if from_location:
		have = qty_at(from_location, item_code, lock=True)
		if have + 1e-9 < qty:
			if not allow_short:
				frappe.throw(_("{0} holds only {1} of {2}. Check the quantity, or pick the location "
					"that really has it.").format(rows[from_location].label, flt(have), item_code))
			qty = have
		if qty <= 0:
			return None
	elif to_location and check_unassigned:
		free = unassigned_qty(company, item_code, lock=True)
		if free + 1e-9 < qty:
			frappe.throw(_("Only {0} of {1} is not in a location yet, so {2} cannot be placed. "
				"Check the quantity.").format(max(free, 0), item_code, flt(qty)))
	qty = flt(qty, _PRECISION)
	if from_location:
		_add_qty(from_location, company, item_code, -qty)
	if to_location:
		_add_qty(to_location, company, item_code, qty)
	name = _log(kind="qty", reason=reason, company=company, item_code=item_code, qty=qty,
		from_location=from_location, to_location=to_location, voucher_type=voucher_type,
		voucher_no=voucher_no, voucher_detail=voucher_detail, note=note)
	return frappe._dict(move=name, qty=qty, kind="qty")


def _in_our_stock(sn) -> bool:
	return on_our_shelf(sn.status, sn.warehouse, sn.get("consigned_out"))


def assign_serial(serial_no: str, location: str | None, *, reason: str = "move", **kw):
	"""Put a firearm in a location (None = take it out of its location)."""
	return move(reason=reason, serial_no=serial_no, to_location=location, **kw)


def put_qty(item_code: str, location: str, qty: float, *, reason: str = "move", **kw):
	"""Add units of a non-serialized item to a location (from the unassigned units)."""
	return move(reason=reason, item_code=item_code, qty=qty, to_location=location, **kw)


def take_qty(item_code: str, location: str, qty: float, *, reason: str = "out", **kw):
	"""Take units out of a location (sold, shipped, gone)."""
	return move(reason=reason, item_code=item_code, qty=qty, from_location=location, **kw)


# --------------------------------------------------------------------------- #
# Placing what just arrived
# --------------------------------------------------------------------------- #

def _next_free_slots(start: frappe._dict, count: int, taken: set[str], *,
		inclusive: bool = True) -> list[str]:
	"""``count`` free enabled slots in ``start``'s zone in position order, beginning with
	``start`` itself (or, not ``inclusive``, with the first one after it). Two queries however
	many slots are looked at: the zone's positions, and which of them hold a gun."""
	if start.disabled:
		return []
	rows = frappe.get_all("Storage Location", filters={
		"zone": start.zone, "disabled": 0, "position": (">=" if inclusive else ">", start.position)},
		fields=["name", "position"], order_by="position asc")
	occupied = {r[0] for r in frappe.db.sql(
		"SELECT DISTINCT sn.storage_location FROM `tabSerial No` sn "
		"JOIN `tabStorage Location` l ON l.name = sn.storage_location "
		"WHERE l.zone = %s AND sn.status = 'Active'", (start.zone,))}
	free = []
	for r in rows:
		if r.name in taken or r.name in occupied:
			continue
		free.append(r.name)
		if len(free) == count:
			break
	return free


def plan_row_slots(rows: list[dict]) -> dict[str, list[str]]:
	"""Slots for every pending receive row at once, so no two rows are offered the same slot.

	``rows`` in receipt order: ``{key, count, start?}`` fills ``start`` and the next free
	slots, ``{key, count, after?}`` takes the first free ones after ``after`` (the
	suggestion for the next row). An Open location needs no slots (``[]``); a ``start`` that
	is already taken gets ``[]`` too. Returns ``{key: [location, ...]}``. Nothing is written."""
	taken: set[str] = set()
	out: dict[str, list[str]] = {}
	for r in rows:
		key, count = str(r["key"]), max(1, cint(r.get("count")))
		anchor = (r.get("start") or r.get("after") or "").strip()
		out[key] = []
		if not anchor:
			continue
		loc = location_row(anchor)
		if loc.kind != KIND_SLOTS or loc.disabled:
			continue
		if r.get("start") and (loc.name in taken or gun_in_slot(loc.name)):
			continue
		out[key] = _next_free_slots(loc, count, taken, inclusive=bool(r.get("start")))
		taken.update(out[key])
	return out


def plan_placements(*, item_code: str, qty: float, serials: list[str] | None,
		storage_location: str | None = None, serial_locations: dict | None = None) -> list[dict]:
	"""Turn "this line goes to <location>" into explicit placements.

	A line of several guns going to a Slots location fills that slot and the next
	free ones in the same zone, in position order. Anything else needs one Open
	location. Returns ``[{"serial", "location"}]`` for guns or
	``[{"item_code", "qty", "location"}]`` for a quantity; nothing is written.
	"""
	serial_locations = {k.strip(): (v or "").strip() for k, v in (serial_locations or {}).items()
		if (k or "").strip()}
	start = (storage_location or "").strip()
	if serials:
		serials = [s.strip() for s in serials if (s or "").strip()]
		plan, rest = [], []
		for s in serials:
			if serial_locations.get(s):
				plan.append({"serial": s, "location": serial_locations[s]})
			else:
				rest.append(s)
		if rest and start:
			loc = location_row(start)
			if loc.kind == KIND_OPEN:
				plan += [{"serial": s, "location": start} for s in rest]
			else:
				# The slot the receiver named must itself be free: silently skipping to the
				# next one would put a gun somewhere nobody chose.
				if loc.disabled:
					frappe.throw(_("{0} is disabled. Pick another location.").format(loc.label))
				other = gun_in_slot(loc.name)
				if other:
					frappe.throw(_("{0} already holds {1}. A slot holds one firearm: pick a free "
						"slot, or move {1} out first.").format(loc.label, other))
				taken = {p["location"] for p in plan}
				slots = _next_free_slots(loc, len(rest), taken)
				if len(slots) < len(rest):
					frappe.throw(_("{0} has only {1} free slot(s) from {2} on, and {3} guns need a "
						"place. Pick another starting slot, or split the line.").format(
						loc.zone, len(slots), loc.label, len(rest)))
				plan += [{"serial": s, "location": slot} for s, slot in zip(rest, slots, strict=True)]
		return plan
	if start and flt(qty) > 0:
		return [{"item_code": item_code, "qty": flt(qty), "location": start}]
	return []


def validate_placements(placements: list[dict], company: str | None = None) -> None:
	"""Read-only dry run of apply_placements for what can be checked before the
	receipt exists: the location is real, enabled and of the right kind, and a slot
	is free and not used twice. Raises the same plain-English errors move() would."""
	used: dict[str, str] = {}
	for p in placements:
		loc = location_row(p["location"])
		if company and loc.company != company:
			frappe.throw(_("{0} belongs to a different company than this receipt. Pick one of this "
				"store's locations.").format(loc.label))
		if loc.disabled:
			frappe.throw(_("{0} is disabled. Pick another location.").format(loc.label))
		if p.get("serial"):
			if loc.kind == KIND_SLOTS:
				if loc.name in used:
					frappe.throw(_("{0} is used for two guns ({1} and {2}). A slot holds one firearm.")
						.format(loc.label, used[loc.name], p["serial"]))
				used[loc.name] = p["serial"]
				other = gun_in_slot(loc.name)
				if other and other != p["serial"]:
					frappe.throw(_("{0} already holds {1}. A slot holds one firearm: pick a free "
						"slot, or move {1} out first.").format(loc.label, other))
		elif loc.kind == KIND_SLOTS:
			frappe.throw(_("{0} is a single-firearm slot, and {1} is not a firearm. "
				"Use a display case, rack or safe instead.").format(loc.label, p["item_code"]))


def place_softly(make_placements) -> list[str]:
	"""Plan (``make_placements()`` may refuse, e.g. a slot taken meanwhile) and apply placements
	where a refusal must never fail the request it rides on (a deferred trade-in booking at
	checkout). Returns the plain-English problems; the red error a refusal queued is dropped."""
	mark = _message_mark()
	try:
		return apply_placements(make_placements(), strict=False)
	except frappe.ValidationError as exc:
		_drop_messages(mark)
		return [str(exc)]


def apply_placements(placements: list[dict], *, voucher_type: str | None = None,
		voucher_no: str | None = None, strict: bool = True) -> list[str]:
	"""Write what plan_placements planned, after the stock exists. ``strict`` raises
	on the first problem (the receive is then rolled back whole); otherwise a
	placement that no longer fits is skipped and reported, the thing stays
	unassigned. Returns the plain-English problems (always empty when strict)."""
	problems: list[str] = []
	for p in placements:
		sp = f"storage_placement_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(sp)
		mark = _message_mark()
		try:
			if p.get("serial"):
				move(reason="receive", serial_no=p["serial"], to_location=p["location"],
					voucher_type=voucher_type, voucher_no=voucher_no)
			else:
				move(reason="receive", item_code=p["item_code"], qty=p["qty"], to_location=p["location"],
					voucher_type=voucher_type, voucher_no=voucher_no)
		except frappe.ValidationError as exc:
			if strict:
				raise
			frappe.db.rollback(save_point=sp)
			_drop_messages(mark)  # swallowed: must not show as a red error on a good request
			problems.append(str(exc))
		else:
			frappe.db.release_savepoint(sp)
	return problems


# --------------------------------------------------------------------------- #
# Stock leaving and coming back (sale, shipment, cancel, return)
# --------------------------------------------------------------------------- #

def validate_open_location(item_code: str, location, company: str | None = None) -> str | None:
	"""The location a plain stock line says its units are taken from, or None.

	A pick that is wrong (not a location, one of another company, a single-firearm slot)
	is refused: the caller's payload is broken. A location that was DISABLED since the
	picker was drawn is not an error but a stale pick: it is dropped (None), so the line
	comes out of the unassigned units and the sale goes through -- selling is never blocked
	by tracking. How many the location holds is the picker's job; when it holds less than
	the line, the stock leaves what it has and the rest shows on the "to confirm" list. A
	serialized or non-stock item has no location to name, so it is ignored."""
	location = (str(location or "")).strip()
	if not location:
		return None
	item = frappe.db.get_value("Item", item_code, ["has_serial_no", "is_stock_item"], as_dict=True)
	if not item or cint(item.has_serial_no) or not cint(item.is_stock_item):
		return None
	loc = location_row(location)
	if company and loc.company != company:
		frappe.throw(_("{0} belongs to a different company than this order. Pick one of this "
			"store's locations for {1}.").format(loc.label, item_code))
	if loc.kind != KIND_OPEN:
		frappe.throw(_("{0} holds one firearm only. Pick a display case, rack or safe for {1}.")
			.format(loc.label, item_code))
	return None if loc.disabled else loc.name


def _plain_stock_row(row) -> bool:
	"""A line of ordinary stock: a quantity of a non-serialized stock item from our own
	shelf (not drop-shipped, not a firearm line)."""
	if cint(row.get("delivered_by_supplier")) or not (row.get("warehouse") or "").strip():
		return False
	item = frappe.get_cached_value("Item", row.item_code, ["is_stock_item", "has_serial_no"], as_dict=True)
	return bool(item and cint(item.is_stock_item) and not cint(item.has_serial_no))


def stock_document_submitted(doc, method: str | None = None) -> None:
	"""Sales Invoice / Delivery Note on_submit: the units a line says it came from
	leave that location -- in the same moment the stock ledger moves them.

	A Sales Invoice only moves stock when it updates stock (a POS sale reaches that
	when it consolidates; a deferred counter order moves it through its Delivery
	Note). A return puts the units back. A line with no location, or a location that
	holds less than the line, never blocks: the sale stands and the difference shows
	up on the "to confirm" list.
	"""
	if doc.doctype == "Sales Invoice" and not cint(doc.get("update_stock")):
		return
	doc_company = doc.get("company")
	for row in doc.get("items") or []:
		loc = (row.get("ffl_storage_location") or "").strip()
		# Stock units, not the selling UOM: 2 boxes of 50 move 100 units.
		qty = flt(row.get("stock_qty")) or flt(row.get("qty")) * (flt(row.get("conversion_factor")) or 1)
		if not loc or not qty or not _plain_stock_row(row):
			continue
		leaving = qty > 0  # a return's lines carry a negative quantity
		# A location of another company is never touched by this document's stock.
		if doc_company and frappe.db.get_value("Storage Location", loc, "company") != doc_company:
			continue
		if not leaving:
			qty = -_returnable(doc, row, loc, abs(qty))
			if not qty:
				continue
		kw = dict(item_code=row.item_code, qty=abs(qty), voucher_type=doc.doctype,
			voucher_no=doc.name, voucher_detail=row.name)
		if leaving:
			never_block(lambda kw=kw, loc=loc: move(
				reason="sale", from_location=loc, allow_short=True, **kw))
		else:
			never_block(lambda kw=kw, loc=loc: move(reason="return", to_location=loc, **kw))


def _message_mark() -> int:
	return len(frappe.local.message_log or [])


def _drop_messages(mark: int) -> None:
	"""Forget the messages a swallowed throw queued (and only those): a red error for
	something we handled would sit on a successful request."""
	if frappe.local.message_log is not None:
		del frappe.local.message_log[mark:]


def _returnable(doc, row, loc: str, wanted: float) -> float:
	"""How many of a return line's units may go back into ``loc``: only what the ORIGINAL sale
	line really took out of it (its Storage Move rows, found by the line the return points at),
	less what earlier returns of that line already put back. The rest of the returned stock
	stays unassigned. A return with no original line to read puts nothing back."""
	link = "sales_invoice_item" if doc.doctype == "Sales Invoice" else "dn_detail"
	orig_detail = row.get(link)
	if not orig_detail:
		return 0.0
	child = f"{doc.doctype} Item"
	orig_parent = frappe.db.get_value(child, orig_detail, "parent")
	taken = flt(frappe.db.sql(
		"SELECT COALESCE(SUM(qty), 0) FROM `tabStorage Move` WHERE kind = 'qty' AND reason = 'sale' "
		"AND voucher_type = %s AND voucher_no = %s AND voucher_detail = %s AND from_location = %s",
		(doc.doctype, orig_parent, orig_detail, loc))[0][0])
	earlier = flt(frappe.db.sql(
		f"SELECT COALESCE(SUM(-stock_qty), 0) FROM `tab{child}` WHERE {link} = %s AND docstatus = 1 "
		"AND parent != %s AND stock_qty < 0", (orig_detail, doc.name))[0][0])
	return max(0.0, min(flt(wanted), taken - earlier))


def never_block(fn) -> bool:
	"""Run a location write that must not break the document it rides on: its own
	savepoint, rolled back and logged on failure; the messages a failed attempt queued are
	dropped. Returns whether it succeeded."""
	sp = f"storage_hook_{frappe.generate_hash(length=6)}"
	frappe.db.savepoint(sp)
	mark = _message_mark()
	try:
		fn()
	except Exception:
		frappe.db.rollback(save_point=sp)
		_drop_messages(mark)
		frappe.log_error(title="Storage location update skipped", message=frappe.get_traceback())
		return False
	frappe.db.release_savepoint(sp)
	return True


def stock_document_cancelled(doc, method: str | None = None) -> None:
	"""on_cancel of a stock document (Sales Invoice, Delivery Note, Purchase Receipt, Stock
	Entry): put back (or take again) whatever it moved."""
	never_block(lambda: reverse_voucher(doc.doctype, doc.name))


def reverse_voucher(voucher_type: str, voucher_no: str) -> int:
	"""Undo the quantity moves a document made, by what is still outstanding. Safe to
	call twice: the second call finds nothing left. Returns the rows reversed."""
	rows = frappe.get_all("Storage Move", filters={
		"voucher_type": voucher_type, "voucher_no": voucher_no, "kind": "qty"},
		fields=["item_code", "qty", "from_location", "to_location", "voucher_detail"])
	net: dict[tuple, float] = {}
	for r in rows:
		if r.to_location:
			net[(r.item_code, r.to_location, r.voucher_detail)] = \
				net.get((r.item_code, r.to_location, r.voucher_detail), 0.0) + flt(r.qty)
		if r.from_location:
			net[(r.item_code, r.from_location, r.voucher_detail)] = \
				net.get((r.item_code, r.from_location, r.voucher_detail), 0.0) - flt(r.qty)
	done = 0
	for (item_code, loc, detail), qty in net.items():
		qty = flt(qty, _PRECISION)
		if qty == 0:
			continue
		kw = dict(item_code=item_code, qty=abs(qty), voucher_type=voucher_type,
			voucher_no=voucher_no, voucher_detail=detail, note="reversal")
		# One line per savepoint: a line that cannot be replayed is logged and left alone
		# (its units simply stay "unassigned" / on the to-confirm list) and never stops the
		# others. Units going back may enter a disabled location (see _move_qty).
		if qty > 0:  # it put units in: take them out again (what is left of them)
			ok = never_block(lambda kw=kw, loc=loc: move(
				reason="out", from_location=loc, allow_short=True, **kw))
		else:        # it took units out: put them back
			ok = never_block(lambda kw=kw, loc=loc: move(reason="return", to_location=loc, **kw))
		done += 1 if ok else 0
	return done


# --------------------------------------------------------------------------- #
# A firearm leaves stock
# --------------------------------------------------------------------------- #

def release_departed(serials: list[str] | None = None) -> int:
	"""Clear the location of every gun that is no longer on our shelf: sold,
	disposed, transferred, out on consignment, in custody, or without a warehouse.

	ERPNext updates Serial No status and warehouse with direct SQL, so no Serial No
	hook can see a gun leave -- this runs from the Stock Ledger Entry hook for the
	serials that entry touched, and from the hourly sweep for everything else.
	Returns how many were cleared."""
	sql = ("SELECT name, status, warehouse, consigned_out FROM `tabSerial No` "
		"WHERE IFNULL(storage_location, '') != ''")
	args: tuple = ()
	if serials is not None:
		if not serials:
			return 0
		sql += " AND name IN %s"
		args = (tuple(serials),)
	rows = frappe.db.sql(sql, args, as_dict=True)
	if not rows:
		return 0
	excluded = excluded_warehouses()
	cleared = 0
	for r in rows:
		if on_our_shelf(r.status, r.warehouse, r.consigned_out, excluded=excluded):
			continue
		if move(reason="out", serial_no=r.name, to_location=None, note="left stock"):
			cleared += 1
	return cleared


def _serials_of(bundle, text) -> list[str]:
	found = split_serials(text)
	if bundle:
		found += [s for s in frappe.get_all("Serial and Batch Entry", filters={"parent": bundle},
			pluck="serial_no") if s]
	return found


def on_stock_ledger_entry(doc, method: str | None = None) -> None:
	"""Stock Ledger Entry on_submit / on_cancel: a serialized entry may have taken a
	gun off the shelf. Cheap for everything else (no serial, no work).

	A Stock Entry is left to :func:`stock_entry_posted`: a transfer between two of our own
	warehouses posts its outgoing leg (warehouse emptied, status Delivered) BEFORE its
	incoming one, and judging the gun in that gap would clear a location it keeps."""
	if doc.get("voucher_type") == "Stock Entry":
		return
	serials = _serials_of(doc.get("serial_and_batch_bundle"), doc.get("serial_no"))
	if serials:
		never_block(lambda: release_departed(serials))


def stock_entry_posted(doc, method: str | None = None) -> None:
	"""Stock Entry on_submit / on_cancel, after ALL its ledger entries: release only the
	guns that are really off our shelves now (a transfer between own warehouses keeps its
	location, one into a consignment / custody warehouse or out of stock clears it)."""
	serials: list[str] = []
	for row in doc.get("items") or []:
		serials += _serials_of(row.get("serial_and_batch_bundle"), row.get("serial_no"))
	if serials:
		never_block(lambda: release_departed(sorted(set(serials))))


def sweep_departed() -> None:
	"""Hourly backstop for release_departed: guns that left by any path the entry
	hook did not see."""
	release_departed(None)


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #

def _label(zone_name: str, kind: str, position: int) -> str:
	"""A Slots position is the zone name plus its number (A1); an Open zone is ONE location and
	carries the zone's own name (Rack 1)."""
	return f"{zone_name}{position}" if kind == KIND_SLOTS else zone_name


def _make_locations(zone: frappe._dict, start: int, count: int) -> list[str]:
	abbr = frappe.get_cached_value("Company", zone.company, "abbr")
	labels = [_label(zone.zone_name, zone.kind, n) for n in range(start, start + count)]
	clash = frappe.get_all("Storage Location", filters={
		"name": ("in", [f"{abbr}-{l}" for l in labels])}, pluck="label")
	if clash:
		frappe.throw(_("A location called {0} already exists in another zone. Give this zone a "
			"different name.").format(clash[0]))
	names = []
	for offset, label in enumerate(labels):
		doc = frappe.get_doc({"doctype": "Storage Location", "company": zone.company,
			"zone": zone.name, "label": label, "position": start + offset, "kind": zone.kind})
		doc.flags.via_storage = True
		doc.flags.ignore_permissions = True
		doc.insert()
		names.append(doc.name)
	return names


def _layout(sides, numbering: str | None) -> tuple[int, str]:
	"""A Slots zone's sides and numbering on the map, checked."""
	n = cint(sides)
	if not 0 <= n <= MAX_SLOT_ROWS or str(sides or 0).strip() != str(n):  # "2.7" / "two"
		frappe.throw(_("Sides go from 1 to {0}. Leave it empty to fit the slots to the shape on the "
			"map.").format(MAX_SLOT_ROWS))
	numbering = numbering or NUMBERINGS[0]
	if numbering not in NUMBERINGS:
		frappe.throw(_("Numbering is {0}.").format(" or ".join(NUMBERINGS)))
	return n, numbering


def set_zone_layout(zone_name: str, sides, numbering: str | None) -> dict:
	"""Change how a Slots zone is drawn on the map: its sides and numbering. Nothing moves --
	every slot keeps its name and what is in it."""
	zone = frappe.db.get_value("Storage Zone", zone_name, ["name", "kind"], as_dict=True, for_update=True)
	if not zone:
		frappe.throw(_("Zone {0} was not found.").format(zone_name))
	if zone.kind != KIND_SLOTS:
		frappe.throw(_("Only a Slots zone has sides; an Open zone is one location."))
	sides, numbering = _layout(sides, numbering)
	frappe.db.set_value("Storage Zone", zone.name, {"slot_lines": sides, "numbering": numbering},
		update_modified=True)
	return {"zone": zone.name, "sides": sides, "numbering": numbering}


def create_zone(company: str, zone_name: str, kind: str, count: int,
		sort_order: int | None = None, sides: int = 0, numbering: str | None = None) -> dict:
	"""Create a zone. A Slots zone gets ``count`` positions named A1..An (one firearm each). An
	Open zone is exactly ONE location named like the zone ("Rack 1"): ``count`` is ignored, and
	each rack, case or safe is created as its own zone. ``sides`` and ``numbering`` only shape
	how a Slots zone is drawn on the map (0 sides = fit to the shape; see NUMBERINGS)."""
	zone_name = (zone_name or "").strip()
	count = 1 if kind == KIND_OPEN else cint(count)
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} was not found.").format(company))
	if not _ZONE_NAME.match(zone_name):
		frappe.throw(_("Give the zone a short name (letters, numbers, spaces and dashes), like "
			"A or Display Case."))
	if kind not in (KIND_SLOTS, KIND_OPEN):
		frappe.throw(_("A zone is either Slots (one firearm per position) or Open (no limit)."))
	if not 1 <= count <= MAX_POSITIONS_PER_CALL:
		frappe.throw(_("A zone needs between 1 and {0} positions.").format(MAX_POSITIONS_PER_CALL))
	sides, numbering = _layout(sides, numbering)
	if kind == KIND_OPEN and sides:
		frappe.throw(_("Only a Slots zone has sides; an Open zone is one location."))
	abbr = frappe.get_cached_value("Company", company, "abbr")
	if frappe.db.exists("Storage Zone", f"{abbr}-{zone_name}"):
		frappe.throw(_("A zone called {0} already exists. Pick another name.").format(zone_name))
	if kind == KIND_OPEN and frappe.db.exists("Storage Location", f"{abbr}-{zone_name}"):
		frappe.throw(_("A location called {0} already exists. Pick another name.").format(zone_name))
	if sort_order is None:
		sort_order = cint(frappe.db.sql(
			"SELECT MAX(sort_order) FROM `tabStorage Zone` WHERE company = %s", (company,))[0][0]) + 1
	zone = frappe.get_doc({"doctype": "Storage Zone", "company": company, "zone_name": zone_name,
		"kind": kind, "slot_count": count, "slot_lines": sides, "numbering": numbering, "sort_order": sort_order})
	zone.flags.via_storage = True
	zone.flags.ignore_permissions = True
	zone.insert()
	_make_locations(zone, 1, count)
	return {"zone": zone.name, "positions": count}


def add_positions(zone_name: str, count: int) -> dict:
	"""Append ``count`` positions to a Slots zone (never renumbers or removes). An Open zone is
	one location, so it takes none."""
	count = cint(count)
	if not 1 <= count <= MAX_POSITIONS_PER_CALL:
		frappe.throw(_("Add between 1 and {0} positions at a time.").format(MAX_POSITIONS_PER_CALL))
	zone = frappe.db.get_value("Storage Zone", zone_name,
		["name", "company", "zone_name", "kind", "slot_count", "disabled"], as_dict=True, for_update=True)
	if not zone:
		frappe.throw(_("Zone {0} was not found.").format(zone_name))
	if zone.kind == KIND_OPEN:
		frappe.throw(_("Open zones are one location — create another zone instead."))
	if zone.disabled:
		frappe.throw(_("{0} is disabled. Enable it before adding positions.").format(zone.zone_name))
	last = cint(frappe.db.sql(
		"SELECT MAX(position) FROM `tabStorage Location` WHERE zone = %s", (zone.name,))[0][0])
	_make_locations(zone, last + 1, count)
	frappe.db.set_value("Storage Zone", zone.name, "slot_count", last + count, update_modified=True)
	return {"zone": zone.name, "positions": last + count, "added": count}


def contents(location_names: list[str], *, lock: bool = False) -> dict[str, dict]:
	"""{location: {"guns": n, "qty": units}} for the given locations. ``lock`` reads with row
	locks (the latest committed state, held until the transaction ends)."""
	out = {n: {"guns": 0, "qty": 0.0} for n in location_names}
	if not location_names:
		return out
	tail = " FOR UPDATE" if lock else ""
	for loc, in frappe.db.sql(
			"SELECT storage_location FROM `tabSerial No` WHERE status = 'Active' "
			"AND storage_location IN %s" + tail, (tuple(location_names),)):
		out[loc]["guns"] += 1
	for loc, q in frappe.db.sql(
			"SELECT location, qty FROM `tabStorage Location Qty` WHERE qty > 0 "
			"AND location IN %s" + tail, (tuple(location_names),)):
		out[loc]["qty"] += flt(q)
	return out


# Our own log and quantity rows may outlive a deleted location (the log keeps the name as
# history; empty quantity rows go with it). Any OTHER document naming it would fail its next
# save (Frappe validates every Link), so such a location is disabled, never deleted.
_OWN_DOCTYPES = ("Storage Move", "Storage Location Qty")


def _named_elsewhere(names: list[str]) -> tuple[str, str] | None:
	"""(location label, doctype) of the first document outside our own log that names one of
	``names``, whatever its status -- a sale line, a web order line, a Serial No."""
	fields = frappe.get_all("DocField", filters={"fieldtype": "Link", "options": "Storage Location"},
		fields=["parent", "fieldname"]) + frappe.get_all("Custom Field",
		filters={"fieldtype": "Link", "options": "Storage Location"}, fields=["dt as parent", "fieldname"])
	for f in fields:
		if f.parent in _OWN_DOCTYPES or frappe.get_meta(f.parent).issingle:
			continue
		hit = frappe.db.get_value(f.parent, {f.fieldname: ("in", names)}, f.fieldname)
		if hit:
			return frappe.db.get_value("Storage Location", hit, "label"), f.parent
	return None


def _delete_empty_locations(names: list[str]) -> None:
	"""Delete positions that hold nothing and that no other document names. Their rows are
	locked first, so a move into one waits and then finds it gone. Past Storage Move rows keep
	the name as plain history (a later undo or reversal into a deleted place leaves the units
	unassigned, like any other replay that cannot land)."""
	if not names:
		return
	frappe.db.sql("SELECT name FROM `tabStorage Location` WHERE name IN %s ORDER BY name FOR UPDATE",
		(tuple(names),))
	busy = [n for n, c in contents(names, lock=True).items() if c["guns"] or c["qty"]]
	if busy:
		labels = frappe.get_all("Storage Location", filters={"name": ("in", busy)},
			pluck="label", order_by="position asc")
		frappe.throw(_("{0} still holds something. Move it out first, then delete.").format(
			", ".join(labels[:5]) + (" …" if len(labels) > 5 else "")))
	named = _named_elsewhere(names)
	if named:
		frappe.throw(_("{0} is recorded on past documents ({1}), so it cannot be deleted. Disable "
			"it instead.").format(*named))
	frappe.db.delete(_QTY_DT, {"location": ("in", names)})  # only empty rows are left
	for n in names:
		frappe.delete_doc("Storage Location", n, force=True, ignore_permissions=True,
			flags={"via_storage": True})


def remove_positions(zone_name: str, count: int) -> dict:
	"""Delete the last ``count`` slots of a Slots zone (A30, A29, ...), so the numbering stays
	A1..An. They must be empty. A zone keeps at least one slot -- delete the zone instead."""
	count = cint(count)
	# REPEATABLE READ: end the request's transaction first (nothing is written yet), so the
	# plain reads after the zone lock see what was committed before it, not an older snapshot.
	# lazy: a document naming a place that commits between the reference check and our commit
	# is missed (milliseconds, a manager-only action); a locking read on the sale-line tables
	# would close it at the cost of locking them.
	commit()
	zone = frappe.db.get_value("Storage Zone", zone_name,
		["name", "zone_name", "kind"], as_dict=True, for_update=True)
	if not zone:
		frappe.throw(_("Zone {0} was not found.").format(zone_name))
	if zone.kind == KIND_OPEN:
		frappe.throw(_("Open zones are one location — delete the zone instead."))
	names = frappe.get_all("Storage Location", filters={"zone": zone.name}, pluck="name",
		order_by="position desc")
	if not 1 <= count < len(names):
		frappe.throw(_("{0} has {1} slots: remove between 1 and {2}. To remove them all, delete "
			"the zone.").format(zone.zone_name, len(names), len(names) - 1))
	_delete_empty_locations(names[:count])
	left = len(names) - count
	frappe.db.set_value("Storage Zone", zone.name, "slot_count", left, update_modified=True)
	return {"zone": zone.name, "positions": left, "removed": count}


def delete_zone(zone_name: str, company: str) -> dict:
	"""Delete a zone and all its positions, and take it off its floor plan. Everything in it
	must be moved out first."""
	commit()  # a fresh snapshot, as in remove_positions
	_lock_company(company)  # plans are written under it ("Floor plans" below)
	zone = frappe.db.get_value("Storage Zone", zone_name, ["name", "zone_name", "company"], as_dict=True,
		for_update=True)
	if not zone:
		frappe.throw(_("Zone {0} was not found.").format(zone_name))
	if zone.company != company:
		frappe.throw(_("Zone {0} belongs to another company.").format(zone.zone_name))
	names = frappe.get_all("Storage Location", filters={"zone": zone.name}, pluck="name")
	_delete_empty_locations(names)
	take_zone_off_plans(company, zone.name)
	frappe.delete_doc("Storage Zone", zone.name, force=True, ignore_permissions=True,
		flags={"via_storage": True})
	return {"zone": zone.name, "removed": len(names)}


def set_disabled(zone: str | None, location: str | None, disabled: bool) -> dict:
	"""Disable (or re-enable) a zone, or one position. A disabled zone disables every position
	in it WITHOUT touching their own switches, so re-enabling the zone brings back exactly the
	positions that were enabled before. Disabling needs the place to be empty -- move what is
	in it first."""
	disabled = 1 if cint(disabled) else 0
	if bool(zone) == bool(location):
		frappe.throw(_("Name a zone or a location, not both."))
	if zone:
		names = frappe.get_all("Storage Location", filters={"zone": zone}, pluck="name")
		if not frappe.db.exists("Storage Zone", zone):
			frappe.throw(_("Zone {0} was not found.").format(zone))
	else:
		location_row(location)
		names = [location]
	if disabled:
		busy = [(n, c) for n, c in contents(names).items() if c["guns"] or c["qty"]]
		if busy:
			frappe.throw(_("{0} still holds something. Move it out first, then disable.").format(
				frappe.db.get_value("Storage Location", busy[0][0], "label")))
	if zone:
		frappe.db.set_value("Storage Zone", zone, "disabled", disabled, update_modified=True)
	else:
		frappe.db.set_value("Storage Location", location, "disabled", disabled, update_modified=True)
	return {"disabled": disabled, "locations": len(names)}


# --------------------------------------------------------------------------- #
# Floor plans
# --------------------------------------------------------------------------- #
# A plan is one room of the store, drawn in feet. Its ``layout`` holds every shape on it:
# {"shapes": [{type, zone?, text?, x, y, w, h, r, rows?}]} -- x/y the centre, w/h the size,
# r the rotation in degrees, rows how many lines of slots a Slots zone has across its short
# side (they run along the long side; absent = auto).
# A zone sits on at most one plan; delete_zone takes it off (under the same lock as plan
# writes), so a plan never names a zone that is gone and a re-created zone of the same name
# starts unplaced.
#
# Locks: every plan write takes the company row first, then plan rows (create: company;
# save: company -> its plan -> the other plans; delete: company -> its plan; delete_zone:
# company -> the zone -> the plans). One order, so writers never wait on each other in a
# circle. The company comes from the caller, so the lock is the transaction's first read:
# MariaDB's snapshot isolation refuses a locking read of a row changed after the
# transaction's first plain read ("Record has changed since last read", 1020), so no plain
# read may come before the locks.
# lazy: the company row is shared -- a Sales Invoice submit also writes it (monthly sales), so
# a plan save and a sale can each wait for the other's transaction (milliseconds; never a
# cycle: plan writes take nothing a sale holds). cash_drawer locks the same row. If it ever
# shows, switch to a named lock (GET_LOCK) per company.

_PLAN_DT = "Storage Floor Plan"
SHAPE_TYPES = ("zone", "wall", "door", "area", "label")
_TEXT_SHAPES = ("area", "label")
# lazy: caps keep one save small -- a store has tens of shapes; raise them if one ever needs more.
MAX_SHAPES = 400
MIN_PLAN_FT, MAX_PLAN_FT = 4.0, 500.0
MIN_SHAPE_FT = 0.25
MAX_SHAPE_TEXT = 40
MAX_SLOT_ROWS = 50
MAX_PLAN_NAME = 40


def plan_shapes(layout) -> list[dict]:
	"""The shapes of a stored layout (a JSON string or dict; empty when unset)."""
	if isinstance(layout, str):
		try:
			layout = json.loads(layout)
		except ValueError:
			layout = None
	shapes = layout.get("shapes") if isinstance(layout, dict) else None
	return [s for s in shapes if isinstance(s, dict)] if isinstance(shapes, list) else []


def _feet(value, what: str) -> float:
	try:
		v = float(value)
	except (TypeError, ValueError):
		v = float("nan")
	if not math.isfinite(v):
		frappe.throw(_("{0} must be a number.").format(what))
	return round(v, 2)


def _plan_header(company: str, plan: str | None, plan_name, width, height) -> tuple[str, float, float]:
	name = (plan_name or "").strip()
	if not 1 <= len(name) <= MAX_PLAN_NAME or "<" in name or ">" in name:
		frappe.throw(_("Give the plan a name of up to {0} characters (no < or >), like Showroom or "
			"Back Room.").format(MAX_PLAN_NAME))
	w, h = _feet(width, _("Width")), _feet(height, _("Depth"))
	if not (MIN_PLAN_FT <= w <= MAX_PLAN_FT and MIN_PLAN_FT <= h <= MAX_PLAN_FT):
		frappe.throw(_("A plan is between {0} and {1} feet on each side.").format(
			int(MIN_PLAN_FT), int(MAX_PLAN_FT)))
	# A locking read: the caller holds the company lock, and a plain read could see an older
	# snapshot taken before it.
	if frappe.db.sql(f"SELECT name FROM `tab{_PLAN_DT}` WHERE company = %s AND plan_name = %s "
			"AND name != %s FOR UPDATE", (company, name, plan or "")):
		frappe.throw(_("A plan called {0} already exists. Pick another name.").format(name))
	return name, w, h


def _clean_layout(company: str, plan: str | None, width: float, height: float,
		shapes) -> tuple[list[dict], list[str]]:
	"""The shapes as they will be stored, checked one by one, plus the zones dropped because
	they were deleted while the plan was being drawn. Anything else wrong is refused. Messages
	name a zone as the user sees it and escape whatever the user typed (Desk shows them as HTML)."""
	if not isinstance(shapes, list):
		frappe.throw(_("The plan's shapes must be a list."))
	if len(shapes) > MAX_SHAPES:
		frappe.throw(_("A plan holds at most {0} shapes.").format(MAX_SHAPES))
	wanted = {str(s.get("zone") or "") for s in shapes if isinstance(s, dict) and s.get("type") == "zone"}
	zones = {z.name: z for z in frappe.get_all("Storage Zone", filters={"name": ("in", list(wanted))},
		fields=["name", "company", "zone_name"])} if wanted else {}
	big = max(width, height)
	out: list[dict] = []
	gone: list[str] = []
	seen: set[str] = set()
	for s in shapes:
		if not isinstance(s, dict) or s.get("type") not in SHAPE_TYPES:
			frappe.throw(_("Every shape on a plan is a zone, wall, door, area or label."))
		z = zones.get(str(s.get("zone") or "")) if s["type"] == "zone" else None
		if s["type"] == "zone" and not z:
			gone.append(str(s.get("zone") or ""))  # deleted while the plan was being drawn
			continue
		what = z.zone_name if z else escape_html(str(s.get("text") or s["type"]))
		c = {"type": s["type"], "x": _feet(s.get("x"), "x"), "y": _feet(s.get("y"), "y"),
			"w": _feet(s.get("w"), _("Width")), "h": _feet(s.get("h"), _("Depth")),
			"r": round(_feet(s.get("r") or 0, _("Rotation")) % 360, 2)}
		if not (0 <= c["x"] <= width and 0 <= c["y"] <= height):
			frappe.throw(_("{0} is outside the plan. Move it inside, or make the plan bigger.").format(what))
		if not (MIN_SHAPE_FT <= c["w"] <= big and MIN_SHAPE_FT <= c["h"] <= big):
			frappe.throw(_("{0} must be between {1} and {2} feet on each side.").format(what, MIN_SHAPE_FT, big))
		if z:
			if z.company != company:
				frappe.throw(_("Zone {0} belongs to another company.").format(z.zone_name))
			if z.name in seen:
				frappe.throw(_("Zone {0} is on the plan twice.").format(z.zone_name))
			seen.add(z.name)
			c["zone"] = z.name
			if s.get("rows") not in (None, ""):
				c["rows"] = cint(s.get("rows"))
				if not 1 <= c["rows"] <= MAX_SLOT_ROWS:
					frappe.throw(_("{0}: lines of slots must be between 1 and {1}.").format(what, MAX_SLOT_ROWS))
		elif c["type"] in _TEXT_SHAPES:
			c["text"] = str(s.get("text") or "").strip()
			if len(c["text"]) > MAX_SHAPE_TEXT or "<" in c["text"] or ">" in c["text"]:
				frappe.throw(_("Keep text on the plan to {0} characters, without < or >: {1}").format(
					MAX_SHAPE_TEXT, escape_html(c["text"])))
		out.append(c)
	# The caller holds the company lock, so no other plan can take a zone while this one checks.
	for other in frappe.db.sql(f"SELECT name, plan_name, layout FROM `tab{_PLAN_DT}` "
			"WHERE company = %s AND name != %s FOR UPDATE", (company, plan or ""), as_dict=True):
		clash = sorted({s.get("zone") for s in plan_shapes(other.layout) if s.get("type") == "zone"} & seen)
		if clash:
			frappe.throw(_("Zone {0} is already on the {1} plan. Take it off there first.").format(
				zones[clash[0]].zone_name, escape_html(other.plan_name)))
	return out, list(dict.fromkeys(gone))


def take_zone_off_plans(company: str, zone: str) -> None:
	"""Remove a zone's shape from every plan of the company. The caller holds the company lock.
	A plan's version is left as it was, so a manager drawing it meanwhile can still save (the
	deleted zone is just dropped from that save)."""
	for p in frappe.db.sql(f"SELECT name, layout FROM `tab{_PLAN_DT}` WHERE company = %s FOR UPDATE",
			(company,), as_dict=True):
		shapes = plan_shapes(p.layout)
		kept = [s for s in shapes if not (s.get("type") == "zone" and s.get("zone") == zone)]
		if len(kept) != len(shapes):
			frappe.db.set_value(_PLAN_DT, p.name, "layout", json.dumps({"shapes": kept}, allow_nan=False),
				update_modified=False)


def _lock_company(company: str) -> None:
	"""One plan write at a time per company (a locking read; the rest wait). Also the
	existence check."""
	if not frappe.db.get_value("Company", company, "name", for_update=True):
		frappe.throw(_("Company {0} was not found.").format(company))


def create_floor_plan(company: str, plan_name: str, width=60, height=40) -> dict:
	"""Start an empty plan for one room of the store."""
	commit()  # a fresh snapshot after the lock, as in remove_positions
	_lock_company(company)
	name, w, h = _plan_header(company, None, plan_name, width, height)
	doc = frappe.get_doc({"doctype": _PLAN_DT, "company": company, "plan_name": name, "width": w,
		"height": h, "layout": json.dumps({"shapes": []})})
	doc.flags.via_storage = True
	doc.flags.ignore_permissions = True
	doc.insert()
	return {"plan": doc.name, "modified": str(doc.modified)}


def _lock_plan(company: str, plan: str | None) -> frappe._dict:
	"""Company lock, then the plan's row (see "Locks" above)."""
	commit()
	_lock_company(company)
	row = frappe.db.get_value(_PLAN_DT, plan, ["name", "company", "modified"], as_dict=True,
		for_update=True) if plan else None
	if not row:
		frappe.throw(_("This plan was deleted. Reload the page."))
	if row.company != company:
		frappe.throw(_("This plan belongs to another company."))
	return row


def save_floor_plan(company: str, plan: str, plan_name: str, width, height, shapes, modified) -> dict:
	"""Replace a plan's name, size and shapes. ``modified`` is the version the editor started
	from: if someone saved the plan since, nothing is written."""
	row = _lock_plan(company, plan)
	try:
		started = get_datetime(modified) if modified else None
	except Exception:
		started = None
	if started != row.modified:
		frappe.throw(_("Someone else saved this plan at {0}. Reload to see their changes, then make "
			"yours again.").format(format_datetime(row.modified)), title=_("Plan changed"))
	name, w, h = _plan_header(row.company, plan, plan_name, width, height)
	clean, gone = _clean_layout(row.company, plan, w, h, shapes)
	frappe.db.set_value(_PLAN_DT, plan, {"plan_name": name, "width": w, "height": h,
		"layout": json.dumps({"shapes": clean}, allow_nan=False)}, update_modified=True)
	return {"plan": plan, "modified": str(frappe.db.get_value(_PLAN_DT, plan, "modified")),
		"shapes": len(clean), "dropped": gone}


def delete_floor_plan(company: str, plan: str) -> dict:
	"""Delete a plan. Its zones are untouched; they are just on no plan any more."""
	_lock_plan(company, plan)
	frappe.delete_doc(_PLAN_DT, plan, force=True, ignore_permissions=True, flags={"via_storage": True})
	return {"plan": plan}


# --------------------------------------------------------------------------- #
# Only move() writes where a gun sits
# --------------------------------------------------------------------------- #

def guard_serial_location(doc, method: str | None = None) -> None:
	"""Serial No validate: ``storage_location`` can only be changed by :func:`move` (which
	writes it with a direct database update, so this never fires for it). A save through REST,
	``frappe.client.set_value`` or the desk form that changes it -- or inserts a Serial No
	already placed -- would skip the slot rule, the log and the locks, so it is refused."""
	if doc.flags.via_storage:
		return
	before = "" if doc.is_new() else (doc.get_doc_before_save() or frappe._dict()).get("storage_location")
	if (doc.get("storage_location") or "") != (before or ""):
		frappe.throw(_("A gun's storage location is changed from the Storage Locations page, not "
			"on the Serial No itself."), title=_("Use Storage Locations"))
