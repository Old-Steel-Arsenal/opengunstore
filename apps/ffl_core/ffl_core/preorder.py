"""Counter pre-orders: goods sold before they are in the store, paid for in full or in part now.

* **The order** — a submitted Sales Order marked ``ffl_is_preorder`` (Create Order's Pre-order
  lines, :func:`create_preorder`). No Sales Invoice is made: nothing is sold until the goods are
  handed over, so nothing books to sales, tax, cost of goods or stock before then. A Sales Order
  takes no serial and no 4473 (``compliance/firearm``), so a gun is ordered without one.
* **How many** — ``Item.ffl_preorder_available``, kept by staff: a pre-order takes its lines'
  quantities from it under a row lock (:func:`take_slots`), refusing when too few are left; a
  cancelled pre-order gives them back (:func:`on_order_cancel`). An item with stock on our own
  shelves is sold from stock instead (:func:`local_stock`).
* **Money** — a PREORDER-DEPOSIT line on a register sale (``ffl_core.deposits``, the mechanism
  gunsmith deposits use): any amount up to what the order still owes with tax, as often as the
  customer likes (:func:`open_payment`), booked to Pre-order Deposits and never taxed. What is
  held for an order is read from the submitted POS Invoices (:func:`held`).
* **Arrival** — a Purchase Receipt or Material Receipt queues :func:`allocate_arrival` after it
  commits (the trigger the want list uses): what came in goes to the open pre-orders waiting for
  it, oldest order first (:func:`allocate_item`) — a gun by its serial (``ffl_preorder_serial`` on
  the order's line, a counter hold of kind ``preorder``: no other sale, register or website takes
  it), anything else by quantity (``ffl_preorder_reserved``, off the shelf every channel counts:
  the website through ``channel_committed_units``, the register through
  :func:`refuse_reserved_units`). Whatever is left over is ordinary stock. Staff can put another
  free gun of the same item by for a line (:func:`assign_serial`).
* **Hand-over** — once everything on the order is here (stage ``ready_pickup``), Open pickup
  (:func:`open_pickup`) makes the register ticket: the goods at the order's prices, taxed as any
  sale that day, and one negative PREORDER-DEPOSIT line releasing what is held, up to what the
  ticket comes to (:func:`fit_release` keeps it there as the register changes the ticket). The rest
  is paid at the register; a gun takes its 4473 there as any gun. The ticket's submit hands the
  order over (``Completed``), and is the only way a pre-order is billed (:func:`refuse_billing_outside_handover`).
  Anything still held after that (a smaller bill) is refunded like a cancelled order's money.
* **Cancel** — :func:`cancel_preorder`: the order is cancelled, its quantities go back, what was
  put by for it goes back on the shelf, and every payment is returned in full the way it came in
  (draft returns the register checks out). Nothing is kept back.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, escape_html, flt, fmt_money, getdate, now, nowdate

from ffl_core import deposits
from ffl_core.deposits import PREORDER, TOL
from ffl_core.roles import COUNTER_ROLES
from ffl_core.setup import preorder as setup
from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD, locked_items
from ffl_core.setup.preorder import (AVAILABLE_FIELD, ETA_FIELD, FLAG_FIELD, LINK_FIELD, PREORDER_ITEM,
	RESERVED_FIELD, SERIAL_FIELD)
from ffl_core.utils import commit

DOCTYPE = "Sales Order"
# The label this module's units carry among the channels' committed units (reservations.committed_units).
CHANNEL = "preorder"
HOLD_KIND = "preorder"
ALLOCATE_JOB = "ffl_core.preorder.allocate_arrival"
_ARRIVAL_SAVEPOINT = "ffl_preorder_arrival"
# What an order line waits for: a serial-numbered unit, a counted quantity, or nothing (a non-stock item).
SERIAL, QTY, NONE = "serial", "qty", "none"


# ------------------------------------------------------------------ helpers


def _money(value, company=None) -> str:
	return fmt_money(flt(value), currency=frappe.get_cached_value("Company", company, "default_currency")
		if company else None)


def is_preorder(name: str | None) -> bool:
	return bool(name) and bool(cint(frappe.db.get_value(DOCTYPE, name, FLAG_FIELD)))


def _order(name: str, write: bool = True):
	"""The pre-order, after the caller's role gate, checked against the user's permission on it."""
	if not is_preorder(name):
		frappe.throw(_("{0} is not a pre-order.").format(escape_html(name or "")))
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write" if write else "read")
	return doc


def held(name: str, exclude: str | None = None, *, for_update: bool = False) -> float:
	"""Money held for pre-order *name*: its payment lines on submitted POS Invoices, returns negative."""
	return deposits.held(PREORDER, name, exclude, for_update=for_update)


def line_kind(item_code: str) -> str:
	"""What an order line of *item_code* waits for: :data:`SERIAL`, :data:`QTY` or :data:`NONE`."""
	row = frappe.get_cached_value("Item", item_code, ["has_serial_no", "is_stock_item"], as_dict=True) or {}
	if cint(row.get("has_serial_no")):
		return SERIAL
	return QTY if cint(row.get("is_stock_item")) else NONE


def _ready(line) -> bool:
	"""Everything this order line needs is here and put by for it."""
	kind = line_kind(line.item_code)
	if kind == SERIAL:
		return bool(line.serial)
	if kind == QTY:
		return flt(line.reserved) >= flt(line.qty) - TOL
	return True


def _open_lines(item_code: str | None = None, orders: list[str] | None = None, *, for_update: bool = False) -> list:
	"""The lines of open pre-orders (submitted, not handed over or closed), oldest order first, with
	what is put by for each (``serial``, ``reserved``). *for_update*: a locking read, which sees the
	latest committed rows rather than this transaction's snapshot."""
	if not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
		return []
	conds, values = [], {}
	if item_code:
		conds.append("i.item_code = %(item)s")
		values["item"] = item_code
	if orders is not None:
		if not orders:
			return []
		conds.append("o.name IN %(orders)s")
		values["orders"] = tuple(orders)
	return frappe.db.sql(f"""
		SELECT i.name AS row, i.parent AS sales_order, i.idx, i.item_code, i.item_name, i.qty, i.rate,
			i.`{SERIAL_FIELD}` AS serial, COALESCE(i.`{RESERVED_FIELD}`, 0) AS reserved,
			o.customer, o.customer_name, o.company, o.creation AS placed
		FROM `tabSales Order Item` i INNER JOIN `tabSales Order` o ON o.name = i.parent
		WHERE o.docstatus = 1 AND o.`{FLAG_FIELD}` = 1 AND o.status NOT IN ('Completed', 'Closed')
			{"AND " + " AND ".join(conds) if conds else ""}
		ORDER BY o.creation, o.name, i.idx{" FOR UPDATE" if for_update else ""}""", values, as_dict=True)


def handed_over(order) -> bool:
	"""A pre-order whose goods went out on its hand-over ticket (:func:`hand_over`)."""
	return cint(order.get("docstatus")) == 1 and flt(order.get("per_delivered")) >= 100


def counter_hold_rows(item_code: str) -> dict:
	"""``reservations.counter_holds`` entries for the guns of *item_code* put by for open pre-orders:
	``{serial: {source, ref (the order), kind "preorder", tooltip, url, customer}}``."""
	if not item_code:
		return {}
	return {l.serial: {"source": HOLD_KIND, "ref": l.sales_order, "kind": HOLD_KIND,
		"tooltip": _("Held for pre-order {0}").format(l.sales_order), "url": f"/app/sales-order/{l.sales_order}",
		"customer": l.customer} for l in _open_lines(item_code) if l.serial}


def reserved_qty(item_code: str, *, only: str | None = None, exclude: str | None = None,
		for_update: bool = False) -> float:
	"""Units of *item_code* put by for open pre-orders (*only* one order's, or all but *exclude*)."""
	return flt(sum(flt(l.reserved) for l in _open_lines(item_code, for_update=for_update)
		if (not only or l.sales_order == only) and l.sales_order != exclude), 6)


def channel_committed_units(item_code: str) -> int:
	"""``channel_committed_units`` hook: the units put by for open pre-orders, which no channel may
	sell. A gun is held by its serial instead (a counter hold), so it counts nothing here."""
	return int(reserved_qty(item_code)) if line_kind(item_code) == QTY else 0


channel_committed_units.channel = CHANNEL


def is_handover(doc) -> bool:
	"""A register ticket handing a pre-order over: linked to it, not a return, paying nothing towards it."""
	name = doc.get(LINK_FIELD)
	if doc.doctype != "POS Invoice" or not name or cint(doc.get("is_return")) or cint(doc.get("is_consolidated")):
		return False
	if any(r.item_code == PREORDER_ITEM and flt(r.get("amount") or r.get("rate")) > 0 for r in doc.get("items") or []):
		return False
	return is_preorder(name)


def handover_reserved(doc, item_code: str) -> float:
	"""Units of *item_code* put by for the pre-order *doc* hands over (none on any other ticket)."""
	return reserved_qty(item_code, only=doc.get(LINK_FIELD)) if is_handover(doc) else 0.0


def shelf_qty(item_code: str, company: str) -> float:
	"""Units of a counted item on the company's own shelves: the Bin of its sellable warehouses
	(``storage.sellable_warehouses``) less register sales not merged into the stock ledger yet."""
	from ffl_core.compliance.web_hold import unmerged_register_qty
	from ffl_core.storage import sellable_warehouses

	warehouses = sellable_warehouses(company)
	if not warehouses:
		return 0.0
	return flt(frappe.db.sql("""SELECT COALESCE(SUM(actual_qty), 0) FROM `tabBin`
		WHERE item_code = %s AND warehouse IN %s""", (item_code, tuple(warehouses)))[0][0]) \
		- flt(unmerged_register_qty(item_code))


def _free_gun(serial: str, item_code: str, holds: dict, excluded: set) -> bool:
	"""A gun of *item_code* on our own shelf that nothing holds (``reservations.serial_shelf_state``)."""
	from ffl_core.reservations import serial_shelf_state
	from ffl_core.storage import on_our_shelf

	row = frappe.db.get_value("Serial No", serial, ["item_code", "status", "warehouse", "consigned_out"], as_dict=True)
	if not row or row.item_code != item_code:
		return False
	if not on_our_shelf(row.status, row.warehouse, row.consigned_out, excluded=excluded):
		return False
	return bool(serial_shelf_state(serial, holds=holds)["sellable"])


def local_stock(item_code: str, company: str) -> float:
	"""Units of *item_code* the counter could sell now: a serial-numbered item's free guns on our own
	shelves (``storage.on_our_shelf``, nothing holding them), anything else's shelf quantity less what
	is put by for pre-orders."""
	from ffl_core.reservations import counter_holds
	from ffl_core.storage import excluded_warehouses

	kind = line_kind(item_code)
	if kind == SERIAL:
		holds, excluded = counter_holds(item_code), excluded_warehouses()
		return float(sum(1 for s in frappe.get_all("Serial No", filters={"item_code": item_code, "status": "Active"},
			pluck="name") if _free_gun(s, item_code, holds, excluded)))
	if kind == NONE:
		return 0.0
	return max(0.0, shelf_qty(item_code, company) - reserved_qty(item_code))


def take_slots(wanted: dict[str, int]) -> None:
	"""Take *wanted* ({item_code: qty}) from the items' pre-order counts, or refuse. Each item's row
	is read with a lock (``SELECT … FOR UPDATE``): a locking read sees the latest committed count,
	where a plain read under REPEATABLE READ would see this transaction's snapshot, and a second
	order for the last unit waits here until the first commits — then finds none left. Items are
	locked in one order, so two orders never wait on each other crosswise.

	MariaDB may instead refuse that locking read with error 1020 ("Record has changed since last
	read") when another order changed the row after this transaction's snapshot: the same race,
	told the other way. Either way this order takes nothing."""
	for item_code in sorted(wanted):
		name = frappe.db.get_value("Item", item_code, "item_name") or item_code
		try:
			have = cint(frappe.db.get_value("Item", item_code, AVAILABLE_FIELD, for_update=True))
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.throw(_("{0} was just pre-ordered at another counter. Check what is left and try again.").format(
				escape_html(name)), title=_("Pre-order"))
		if have < wanted[item_code]:
			frappe.throw(_("Only {0} of {1} can still be pre-ordered.").format(have, escape_html(name)),
				title=_("Pre-order"))
		# The Item's modified moves too (here and on give-back, on_order_cancel): a form opened before
		# this is refused on save rather than writing its stale count back.
		frappe.db.set_value("Item", item_code, AVAILABLE_FIELD, have - wanted[item_code])


def _wanted(rows) -> dict[str, int]:
	out: dict[str, int] = {}
	for r in rows:
		out[r.item_code] = out.get(r.item_code, 0) + cint(r.qty)
	return out


# ------------------------------------------------------------------ Create Order


@frappe.whitelist()
def preorder_options(item_code: str, company: str | None = None) -> dict:
	"""Whether Create Order may offer *item_code* as a pre-order line: pre-order slots left and none
	on our shelves.

	Args:
	    item_code: The Item.
	    company: The order's company; else the default one.

	Returns:
	    {"available": slots left, "eta": the expected date or None, "in_stock": units on our shelves,
	    "can_preorder": bool}.
	"""
	from ffl_core.api.manual_order import _default_company

	frappe.only_for(COUNTER_ROLES)
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item {0} not found.").format(escape_html(item_code or "")))
	if not frappe.db.has_column("Item", AVAILABLE_FIELD):
		return {"available": 0, "eta": None, "in_stock": 0, "can_preorder": False}
	row = frappe.db.get_value("Item", item_code, [AVAILABLE_FIELD, ETA_FIELD], as_dict=True)
	stock = local_stock(item_code, company or _default_company())
	available = cint(row.get(AVAILABLE_FIELD))
	return {"available": available, "eta": row.get(ETA_FIELD), "in_stock": stock,
		"can_preorder": available > 0 and stock <= 0 and item_code not in locked_items()}


@frappe.whitelist(methods=["POST"])
def create_preorder(payload: str | dict) -> dict:
	"""Take a pre-order at the counter: a submitted Sales Order marked as one, its quantities taken
	from the items' pre-order counts. No invoice and no money yet — payments go through
	:func:`open_payment`. The customer collects here.

	Args:
	    payload: {customer, items: [{item_code, qty, rate}], notes?, pos_profile?, price_approval?} —
	        dict or JSON string. A serial-numbered item is one unit per line. Every item must have pre-order slots left
	        and none on our shelves.

	Returns:
	    {"sales_order", "customer", "grand_total": what the order comes to with tax, "eta"}.
	"""
	from ffl_core import price_approval
	from ffl_core.api._helpers import coerce_payload
	from ffl_core.api.manual_order import (_apply_default_tax_template, _default_pos_profile, _resolve_pos_profile,
		_store_warehouse)

	frappe.only_for(COUNTER_ROLES)
	if not frappe.db.exists("Item", PREORDER_ITEM):
		setup.ensure()
	data = coerce_payload(payload)
	customer = str(data.get("customer") or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Pick the buyer (Customer) for this order."))
	profile_name = _resolve_pos_profile(str(data.get("pos_profile") or "").strip() or None) or _default_pos_profile()
	profile = profile_name and frappe.db.get_value("POS Profile", profile_name, ["company", "warehouse"], as_dict=True)
	if not profile:
		frappe.throw(_("No POS Profile is configured — set one up before creating orders."))
	company = profile.company
	warehouse = profile.warehouse or _store_warehouse(company)

	rows = []
	locked = locked_items()
	items = data.get("items") or []
	if not isinstance(items, list):
		frappe.throw(_("The order's lines did not come through. Try again."))
	for i, raw in enumerate(items, 1):
		if not isinstance(raw, dict):
			frappe.throw(_("Line {0} is not an item.").format(i))
		item_code = str(raw.get("item_code") or "").strip()
		if not item_code:
			continue
		if not frappe.db.exists("Item", item_code) or item_code in locked:
			frappe.throw(_("Line {0}: {1} cannot be pre-ordered.").format(i, escape_html(item_code)))
		qty, rate = flt(raw.get("qty") or 0), flt(raw.get("rate") or 0)
		if qty <= 0 or qty != cint(qty):
			frappe.throw(_("Line {0}: the quantity is a whole number above 0.").format(i))
		if line_kind(item_code) == SERIAL and qty != 1:
			frappe.throw(_("Line {0}: a gun (or anything with a serial number) is one unit per line.").format(i))
		if rate < 0:
			frappe.throw(_("Line {0}: the price cannot be negative.").format(i))
		if local_stock(item_code, company) > 0:
			frappe.throw(_("{0} is in stock. Sell it from stock instead of pre-ordering it.").format(
				escape_html(frappe.db.get_value("Item", item_code, "item_name") or item_code)), title=_("Pre-order"))
		rows.append(frappe._dict(item_code=item_code, qty=cint(qty), rate=rate))
	if not rows:
		frappe.throw(_("Add at least one item to the order."))

	take_slots(_wanted(rows))

	token = str(data.get("price_approval") or "").strip()
	approval = None
	if token:
		try:
			approval = price_approval.take_token(token)
		except price_approval.PriceApprovalRequired:
			frappe.clear_last_message()   # worth nothing; the validate gate still refuses a price that needs one

	etas = [getdate(e) for e in frappe.get_all("Item", filters={"name": ["in", [r.item_code for r in rows]]},
		pluck=ETA_FIELD) if e]
	eta = max(etas) if etas else None
	so = frappe.new_doc(DOCTYPE)
	price_approval.attach(so, approval)
	so.company = company
	so.customer = customer
	so.set(FLAG_FIELD, 1)
	so.delivery_date = eta if eta and eta >= getdate(nowdate()) else add_days(nowdate(), 7)
	so.set_warehouse = warehouse
	for r in rows:
		so.append("items", {"item_code": r.item_code, "qty": r.qty, "rate": r.rate})
	if not cint(frappe.db.get_value("Customer", customer, "is_tax_exempt")):
		_apply_default_tax_template(so, company)
	so.flags.ignore_permissions = True
	so.insert()
	so.submit()
	notes = str(data.get("notes") or "").strip()
	if notes:
		so.add_comment("Comment", escape_html(notes))
	return {"sales_order": so.name, "customer": so.customer, "grand_total": flt(so.grand_total),
		"eta": str(eta) if eta else None}


# ------------------------------------------------------------------ payments


@frappe.whitelist(methods=["POST"])
def open_payment(sales_order: str, amount: float) -> dict:
	"""A payment towards a pre-order, as a draft register invoice the register checks out (any
	tender). One open payment draft per order: a second call returns the one already open.

	Args:
	    sales_order: The pre-order (submitted).
	    amount: What the customer pays now: above 0, no more than the order's total with tax less
	        what is already held.

	Returns:
	    {"pos_invoice", "already": bool, "amount": the draft's payment amount, "notice": for a draft
	    already open at another amount, the sentence that says so (shown before it opens), else None}.
	"""
	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 1:
		frappe.throw(_("Pre-order {0} is cancelled; it takes no payment.").format(so.name))
	amount = flt(amount, 2)
	existing = deposits.open_draft(PREORDER, so.name, "deposit")
	if existing:
		have = deposits.draft_amount(PREORDER, existing)
		notice = None if abs(have - amount) <= TOL else _(
			"A payment of {0} for {1} is already open. It opens now; finish or delete it first.").format(
				_money(have, so.company), so.name)
		return {"pos_invoice": existing, "already": True, "amount": have, "notice": notice}
	if amount <= 0:
		frappe.throw(_("Enter the amount the customer pays now."))
	due = flt(flt(so.grand_total) - held(so.name), 2)
	if amount > due + TOL:
		frappe.throw(_("Only {0} is left to pay on {1}.").format(_money(due, so.company), so.name))
	inv = deposits.new_invoice(PREORDER, so, [deposits.line(PREORDER, so, amount,
		_("Payment towards pre-order {0}").format(so.name))])
	inv.insert()
	return {"pos_invoice": inv.name, "already": False, "amount": amount, "notice": None}


# ------------------------------------------------------------------ arrival


def on_stock_receipt(doc, method=None) -> None:
	"""Purchase Receipt / Stock Entry on_submit: goods some open pre-order is waiting for queue
	:func:`allocate_arrival` for after the commit (as ``crm.wants.on_stock_receipt`` does). After the
	commit, so a gun a ship-to-store order is waiting for is already hung on that order
	(``receive_bridge``, same request) and held, and this never fails or slows the receipt. Never raises."""
	try:
		if doc.doctype == "Stock Entry" and doc.get("stock_entry_type") != "Material Receipt":
			return
		if doc.get("is_return") or not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
			return
		codes = sorted({r.item_code for r in doc.get("items") or [] if r.get("item_code")})
		if not codes or not frappe.db.sql(f"""SELECT 1 FROM `tabSales Order Item` i
				INNER JOIN `tabSales Order` o ON o.name = i.parent
				WHERE o.docstatus = 1 AND o.`{FLAG_FIELD}` = 1 AND o.status NOT IN ('Completed', 'Closed')
					AND i.item_code IN %s LIMIT 1""", (tuple(codes),)):
			return
		frappe.enqueue(ALLOCATE_JOB, queue="short", enqueue_after_commit=True, doctype=doc.doctype, name=doc.name)
	except Exception:
		frappe.log_error(title=f"Pre-order arrival not queued: {doc.doctype} {doc.name}", message=frappe.get_traceback())


def allocate_arrival(doctype: str, name: str) -> dict:
	"""The job: what *doctype* *name* put on our own shelves goes to the pre-orders waiting for it
	(:func:`allocate_item`), item by item, each committed on its own. A failure on one item is rolled
	back and logged, and leaves the others alone. Safe to run twice: a second run finds nothing waiting
	or nothing free. Returns ``{item_code: [what each order got]}``."""
	from ffl_core.serials import serials_for_line
	from ffl_core.storage import sellable_warehouses

	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1:
		return {}
	shelves = set(sellable_warehouses(doc.company))
	arrived: dict[str, dict] = {}
	for row in doc.get("items") or []:
		warehouse = row.get("t_warehouse") if doctype == "Stock Entry" else row.get("warehouse")
		if not row.get("item_code") or warehouse not in shelves:
			continue
		got = arrived.setdefault(row.item_code, {"serials": [], "qty": 0.0})
		got["serials"].extend(serials_for_line(row) or [])
		got["qty"] += flt(row.get("transfer_qty") or row.get("stock_qty") or row.get("qty"))
	out = {}
	for item_code in sorted(arrived):
		# Another arrival's job may have changed these orders after this transaction began reading (MariaDB
		# 1020) or hold the lock too long: rolled back and run again on a fresh transaction, 3 tries in all.
		for attempt in range(3):
			commit()   # the lock below is then this transaction's first read
			frappe.db.savepoint(_ARRIVAL_SAVEPOINT)
			try:
				out[item_code] = allocate_item(item_code, doc.company, **arrived[item_code])
				commit()
				break
			except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
				frappe.db.rollback()   # the whole transaction: the retry must not read its snapshot
				if attempt == 2:
					frappe.log_error(title=f"Pre-order arrival not put by: {item_code} on {doctype} {name}",
						message=frappe.get_traceback())
			except Exception:
				frappe.db.rollback(save_point=_ARRIVAL_SAVEPOINT)
				frappe.log_error(title=f"Pre-order arrival not put by: {item_code} on {doctype} {name}",
					message=frappe.get_traceback())
				break
	return out


def _counter_demands(item_code: str) -> list:
	"""What counter pre-orders still wait for of *item_code*: one entry per line short of goods (read
	with a lock: the allocator decides on it)."""
	kind = line_kind(item_code)
	out = []
	for l in _open_lines(item_code, for_update=True):
		need = (0 if l.serial else 1) if kind == SERIAL else max(0.0, flt(l.qty) - flt(l.reserved))
		if need > TOL:
			out.append(frappe._dict(doctype=DOCTYPE, name=l.sales_order, row=l.row, need=need, placed=l.placed,
				reserved=flt(l.reserved)))
	return out


def _fill_counter_line(demand, *, serial: str | None = None, qty: float = 0) -> None:
	if serial:
		frappe.db.set_value("Sales Order Item", demand.row, SERIAL_FIELD, serial, update_modified=False)
	else:
		frappe.db.set_value("Sales Order Item", demand.row, RESERVED_FIELD, flt(demand.reserved + qty, 6),
			update_modified=False)


# Where the waiting comes from, and how each is filled. One queue across them, oldest first.
# lazy: counter pre-orders only; upgrade path: web pre-orders join with their own demand and fill
# functions here, and the oldest order still comes first whichever channel it came in on.
DEMAND_SOURCES = (_counter_demands,)
FILL = {DOCTYPE: _fill_counter_line}


def _demands(item_code: str) -> list:
	return sorted((d for source in DEMAND_SOURCES for d in source(item_code)), key=lambda d: (d.placed, d.name))


def allocate_item(item_code: str, company: str, serials=(), qty: float = 0) -> list[dict]:
	"""Put what just arrived of *item_code* by for the orders waiting for it, oldest order first: each
	gun among *serials* that nothing holds to the next line short of a gun; up to *qty* units of
	anything else, no more than the shelf has free once every channel's units are off it. Under the
	Item's reservation lock (``reservations.lock_item_reservations``, the one a web order or a gun
	change takes), so two arrivals never fill the same line twice. Returns [{doctype, name, serial |
	qty}]."""
	from ffl_core.crm.holds import fire_changed
	from ffl_core.reservations import (committed_units, counter_holds, lock_item_reservations,
		notify_channel_commitments_changed)
	from ffl_core.storage import excluded_warehouses

	lock_item_reservations(item_code)
	waiting = _demands(item_code)
	if not waiting:
		return []
	kind = line_kind(item_code)
	given = []
	if kind == SERIAL:
		holds, excluded = counter_holds(item_code), excluded_warehouses()
		pool = [s for s in dict.fromkeys(serials or ()) if _free_gun(s, item_code, holds, excluded)]
		for demand in waiting:
			if not pool:
				break
			serial = pool.pop(0)
			FILL[demand.doctype](demand, serial=serial)
			given.append({"doctype": demand.doctype, "name": demand.name, "serial": serial})
	elif kind == QTY:
		# Units already sold on another channel and not shipped are not ours to put by (raises when a
		# channel cannot answer: nothing is put by then, and the next arrival or a gun change tries again).
		others = sum(committed_units(item_code, exclude=CHANNEL).values())
		left = min(flt(qty), shelf_qty(item_code, company) - reserved_qty(item_code, for_update=True) - others)
		for demand in waiting:
			if left <= TOL:
				break
			take = min(demand.need, left)
			FILL[demand.doctype](demand, qty=take)
			given.append({"doctype": demand.doctype, "name": demand.name, "qty": take})
			left -= take
	for g in given:
		frappe.get_doc(g["doctype"], g["name"]).add_comment("Comment", _("Arrived and put by: {0}").format(
			escape_html(g.get("serial") or f"{flt(g.get('qty')):g} × {item_code}")))
		if g.get("serial"):
			fire_changed(g["serial"])
	if given:
		notify_channel_commitments_changed(item_code, source=CHANNEL)
	return given


# ------------------------------------------------------------------ the gun on a line


def _gun_lines(so) -> list:
	return [r for r in so.items if line_kind(r.item_code) == SERIAL]


@frappe.whitelist()
def serial_choices(sales_order: str) -> dict:
	"""The guns a pre-order's lines could have: for each serial-numbered line, the one put by for it
	and every other of the same item on our shelf that nothing holds.

	Args:
	    sales_order: An open pre-order.

	Returns:
	    {"lines": [{"row", "item_code", "item_name", "serial", "choices": [serial, ...]}]}.
	"""
	from ffl_core.reservations import counter_holds
	from ffl_core.storage import excluded_warehouses

	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order, write=False)
	excluded = excluded_warehouses()
	out = []
	# lazy: every Active serial of the item is judged (a few dozen at most per model); upgrade path:
	# filter by warehouse in the query.
	for r in _gun_lines(so):
		holds = counter_holds(r.item_code)
		free = [s for s in frappe.get_all("Serial No", filters={"item_code": r.item_code, "status": "Active"},
			order_by="creation asc", pluck="name") if _free_gun(s, r.item_code, holds, excluded)]
		current = r.get(SERIAL_FIELD)
		out.append({"row": r.name, "item_code": r.item_code, "item_name": r.item_name, "serial": current,
			"choices": ([current] if current else []) + free})
	return {"lines": out}


@frappe.whitelist(methods=["POST"])
def assign_serial(sales_order: str, row: str, serial: str) -> dict:
	"""Put another gun of the same item by for one line of an open pre-order (the one it had goes
	back on the shelf), or a first one when none has arrived for it.

	Args:
	    sales_order: The pre-order (open, not handed over).
	    row: Its Sales Order Item row.
	    serial: A gun of that item on our shelf that nothing holds.

	Returns:
	    {"serial": the gun now put by, "was": the one it had or None}.
	"""
	from ffl_core.crm.holds import fire_changed
	from ffl_core.reservations import counter_holds, lock_item_reservations, notify_channel_commitments_changed
	from ffl_core.storage import excluded_warehouses

	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 1 or handed_over(so):
		frappe.throw(_("Pre-order {0} is closed.").format(so.name))
	line = next((r for r in _gun_lines(so) if r.name == row), None)
	if not line:
		frappe.throw(_("That line of {0} takes no serial number.").format(so.name))
	if deposits.open_draft(PREORDER, so.name, "pickup"):
		frappe.throw(_("The pickup for {0} is open at the register. Delete it first, then change the gun.").format(so.name))
	serial = (serial or "").strip()
	commit()   # then the lock is this transaction's first read (MariaDB 1020 otherwise)
	lock_item_reservations(line.item_code)
	was = frappe.db.get_value("Sales Order Item", line.name, SERIAL_FIELD)
	if serial == was:
		return {"serial": serial, "was": was}
	if not serial or not _free_gun(serial, line.item_code, counter_holds(line.item_code), excluded_warehouses()):
		frappe.throw(_("{0} is not a free {1} on the shelf.").format(escape_html(serial), escape_html(line.item_name)))
	frappe.db.set_value("Sales Order Item", line.name, SERIAL_FIELD, serial, update_modified=False)
	so.add_comment("Comment", _("Gun changed: {0} → {1}").format(escape_html(was or "—"), escape_html(serial)))
	for s in (was, serial):
		if s:
			fire_changed(s)
	notify_channel_commitments_changed(line.item_code, source=CHANNEL)
	return {"serial": serial, "was": was}


# ------------------------------------------------------------------ hand-over


@frappe.whitelist(methods=["POST"])
def open_pickup(sales_order: str) -> dict:
	"""Hand an arrived pre-order over: the draft register ticket with its goods at the order's
	prices, taxed as any sale today, and what is held coming off them (:func:`fit_release`). The
	register takes the rest and the gun's 4473; the ticket's submit hands the order over.

	Args:
	    sales_order: An open pre-order whose goods are all here.

	Returns:
	    {"pos_invoice", "already": bool (the ticket was open already)}.
	"""
	from ffl_core.price_approval import mark_system_priced

	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 1 or handed_over(so):
		frappe.throw(_("Pre-order {0} is closed.").format(so.name))
	existing = deposits.open_draft(PREORDER, so.name, "pickup")
	if existing:
		return {"pos_invoice": existing, "already": True}
	if deposits.open_draft(PREORDER, so.name, "deposit"):
		frappe.throw(_("A payment for {0} is still open at the register. Complete or delete it first.").format(so.name))
	lines = _open_lines(orders=[so.name])
	waiting = [l for l in lines if not _ready(l)]
	if waiting:
		frappe.throw(_("Not here yet: {0}.").format(", ".join(escape_html(l.item_name or l.item_code) for l in waiting)),
			title=_("Pre-order"))
	items = []
	for l in lines:
		row = {"item_code": l.item_code, "qty": l.qty, "rate": l.rate, "price_list_rate": l.rate,
			"discount_percentage": 0, "discount_amount": 0, LINK_FIELD: so.name}
		if l.serial:
			row.update(serial_no=l.serial, use_serial_batch_fields=1,
				warehouse=frappe.db.get_value("Serial No", l.serial, "warehouse"))
		items.append(row)
	have = held(so.name)
	if have > TOL:
		# What is held, at most; fit_release brings it down to what the ticket comes to.
		items.append(deposits.line(PREORDER, so, -have, _("Paid towards pre-order {0}").format(so.name)))
	inv = deposits.new_invoice(PREORDER, so, items)
	# The order's prices are the bill: no manager approval for them here (a lower price at the register asks).
	mark_system_priced(inv)
	inv.insert()
	# Pricing may have moved a line on the way in; the order's prices are the bill.
	want = {l.item_code: flt(l.rate) for l in lines}
	moved = False
	for r in inv.items:
		if r.item_code != PREORDER_ITEM and r.item_code in want and flt(r.rate) != want[r.item_code]:
			r.rate = r.price_list_rate = want[r.item_code]
			r.discount_amount = r.discount_percentage = 0
			moved = True
	if moved:
		inv.save()
	return {"pos_invoice": inv.name, "already": False}


def fit_release(doc, method=None) -> None:
	"""POS Invoice before_validate, after the hooks that change its tax: a hand-over's release line
	takes off what is held for its pre-order, never more than the ticket comes to — so an exemption
	ticked at the register, a dearer or cheaper tax rate than when the money was taken, or a line
	taken off never leaves the ticket below zero. What it does not take off stays held (refunded
	after). Sets the line's locked amount with it, which the locked-line gate then holds it to."""
	from ffl_core.compliance.locked_lines import pin_row

	if doc.doctype != "POS Invoice" or cint(doc.get("is_return")) or cint(doc.get("is_consolidated")):
		return
	rows = [r for r in doc.get("items") or [] if r.item_code == PREORDER_ITEM
		and flt(r.get(LOCKED_RATE_FIELD)) < 0 and r.get(LINK_FIELD)]
	if len(rows) != 1:
		return   # the payment guard says what is wrong
	row = rows[0]
	have = held(row.get(LINK_FIELD), exclude=doc.name)
	pin_row(row, 0)
	doc.calculate_taxes_and_totals()
	release = flt(min(have, max(0.0, flt(doc.grand_total))), 2)
	pin_row(row, -release)


def hand_over(name: str, invoice: str) -> None:
	"""The pre-order's goods went out on *invoice*: delivered and billed in full (``Completed``)."""
	so = frappe.get_doc(DOCTYPE, name)
	for r in so.items:
		r.db_set({"delivered_qty": r.qty, "billed_amt": r.amount}, update_modified=False)
	so.db_set({"per_delivered": 100, "per_billed": 100}, update_modified=False)
	so.set_status(update=True)
	so.add_comment("Comment", _("Handed over on {0}.").format(invoice))


def _take_back(name: str, invoice: str) -> None:
	"""Its hand-over ticket was cancelled: the order is open again, its goods still put by for it."""
	so = frappe.get_doc(DOCTYPE, name)
	for r in so.items:
		r.db_set({"delivered_qty": 0, "billed_amt": 0}, update_modified=False)
	so.db_set({"per_delivered": 0, "per_billed": 0}, update_modified=False)
	so.set_status(update=True)
	so.add_comment("Comment", _("Hand-over {0} cancelled.").format(invoice))


# ------------------------------------------------------------------ cancel


@frappe.whitelist(methods=["POST"])
def cancel_preorder(sales_order: str, reason: str) -> dict:
	"""Cancel a pre-order and pay back everything held for it: the order is cancelled, its
	quantities go back on the items, and each payment gets a draft return for its full amount, the
	way it was paid. The register checks the returns out.

	Args:
	    sales_order: The pre-order (submitted, nothing handed over).
	    reason: Why.

	Returns:
	    {"cancelled": True, "refunds": [{"pos_invoice", "amount"}]}.
	"""
	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 1:
		frappe.throw(_("Pre-order {0} is already cancelled.").format(so.name))
	if not (reason or "").strip():
		frappe.throw(_("Say why the pre-order is cancelled."))
	if deposits.open_draft(PREORDER, so.name, "deposit"):
		frappe.throw(_("A payment for {0} is still open at the register. Complete or delete it first.").format(so.name))
	if deposits.open_draft(PREORDER, so.name, "pickup"):
		frappe.throw(_("The pickup for {0} is open at the register. Delete it first.").format(so.name))
	if flt(so.per_delivered) or flt(so.per_billed):
		frappe.throw(_("Goods on {0} have already been handed over or billed.").format(so.name))
	so.add_comment("Comment", _("Cancelled: {0}").format(escape_html(reason.strip())))
	so.flags.ignore_permissions = True
	so.flags.ffl_preorder_cancel = True
	so.cancel()
	return {"cancelled": True, "refunds": _refund_all(so)}


@frappe.whitelist(methods=["POST"])
def refund_preorder(sales_order: str) -> dict:
	"""The draft returns paying back what a pre-order still holds once it is cancelled, or handed over
	for less than was paid — the open ones, or new ones for every payment not returned yet.

	Args:
	    sales_order: A cancelled or handed-over pre-order.

	Returns:
	    {"refunds": [{"pos_invoice", "amount"}]}.
	"""
	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 2 and not handed_over(so):
		frappe.throw(_("Cancel the pre-order to refund it (Cancel & refund); once handed over, what is left is refunded."))
	refunds = _refund_all(so)
	if not refunds and held(so.name) > TOL:
		frappe.throw(_("{0} of {1} is on a payment line that was already returned once, and a payment line can "
			"be returned only once at the register. Ask a manager.").format(_money(held(so.name), so.company), so.name),
			title=_("Refund"))
	return {"refunds": refunds}


def _refund_all(so) -> list[dict]:
	out = [{"pos_invoice": name, "amount": -deposits.draft_amount(PREORDER, name)}
		for name in deposits.open_drafts(PREORDER, so.name, "refund")]
	left = held(so.name)
	for source, amount in deposits.refundable_payments(PREORDER, so.name):
		take = min(left, amount)
		if take <= TOL:
			break
		ret = deposits.refund_return(PREORDER, so.name, source, take)
		out.append({"pos_invoice": ret.name, "amount": take})
		left = flt(left - take, 2)
	return out


# ------------------------------------------------------------------ the queue


@frappe.whitelist()
def list_preorders(customer: str | None = None) -> list[dict]:
	"""Pre-orders for the Pending Order page: open ones waiting for their goods (stage ``waiting``) or
	with everything here (``ready_pickup``), and cancelled or handed-over ones still holding money to
	pay back (``blocked``).

	Args:
	    customer: Only this customer's. Default: everyone's.

	Returns:
	    [{"source": "preorder", "sales_order", "customer", "customer_name", "posting_date", "grand_total",
	    "held", "outstanding_amount", "paid", "cancelled", "handed_over", "ready", "eta",
	    "items": [{"row", "item_code", "item_name", "qty", "serial", "reserved", "kind", "ready"}],
	    "drafts": {"payment", "pickup", "refunds": [...]}, "stage", "blocked_reason", "age_days",
	    "overdue"}].
	"""
	from ffl_core.api.manual_order import STAGE_BLOCKED, STAGE_READY_PICKUP, STAGE_WAITING, age_days

	frappe.only_for(COUNTER_ROLES)
	if not frappe.db.has_column(DOCTYPE, FLAG_FIELD) or not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
		return []
	filters = {FLAG_FIELD: 1, "docstatus": ["in", [1, 2]], "status": ["!=", "Closed"]}
	if customer:
		filters["customer"] = customer
	# lazy: every pre-order is read, one held read per order; a store has tens open. Upgrade path:
	# one aggregate over POS Invoice Item for the page, and closed ones by held > 0.
	orders = frappe.get_all(DOCTYPE, filters=filters, fields=["name", "customer", "customer_name",
		"transaction_date", "grand_total", "docstatus", "company", "per_delivered"],
		order_by="transaction_date asc, creation asc")
	if not orders:
		return []
	lines: dict[str, list] = {}
	for r in frappe.get_all("Sales Order Item", filters={"parent": ["in", [o.name for o in orders]]},
			fields=["parent", "name", "item_code", "item_name", "qty", SERIAL_FIELD, RESERVED_FIELD], order_by="idx"):
		lines.setdefault(r.parent, []).append(frappe._dict(row=r.name, item_code=r.item_code, item_name=r.item_name,
			qty=r.qty, serial=r.get(SERIAL_FIELD), reserved=flt(r.get(RESERVED_FIELD))))
	etas = dict(frappe.get_all("Item", filters={"name": ["in", list({l.item_code for ls in lines.values() for l in ls})]},
		fields=["name", ETA_FIELD], as_list=True)) if lines else {}
	today = nowdate()
	out = []
	for o in orders:
		have = held(o.name)
		cancelled = o.docstatus == 2
		done = handed_over(o)
		if (cancelled or done) and have <= TOL:
			continue
		items = lines.get(o.name, [])
		for l in items:
			l.kind = line_kind(l.item_code)
			l.ready = _ready(l)
		ready = not cancelled and not done and all(l.ready for l in items)
		item_etas = [getdate(etas[l.item_code]) for l in items if etas.get(l.item_code)]
		outstanding = 0.0 if cancelled or done else max(0.0, flt(flt(o.grand_total) - have, 2))
		reason = ""
		if cancelled:
			reason = _("Cancelled — refund {0}").format(_money(have, o.company))
		elif done:
			reason = _("Handed over — refund {0}").format(_money(have, o.company))
		out.append({
			"source": "preorder", "sales_order": o.name, "customer": o.customer, "customer_name": o.customer_name,
			"posting_date": o.transaction_date, "grand_total": flt(o.grand_total), "held": have,
			"outstanding_amount": outstanding, "paid": outstanding <= TOL, "cancelled": cancelled, "handed_over": done,
			"ready": ready, "eta": str(max(item_etas)) if item_etas else None, "items": items,
			"drafts": {"payment": deposits.open_draft(PREORDER, o.name, "deposit"),
				"pickup": deposits.open_draft(PREORDER, o.name, "pickup"),
				"refunds": deposits.open_drafts(PREORDER, o.name, "refund")},
			"stage": STAGE_BLOCKED if reason else (STAGE_READY_PICKUP if ready else STAGE_WAITING),
			"blocked_reason": reason,
			# A pre-order waits for its goods by design: its age is shown, never flagged overdue.
			"age_days": age_days(o.transaction_date, today), "overdue": False,
		})
	return out


# ------------------------------------------------------------------ hooks


def guard_payment_lines(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice validate: a PREORDER-DEPOSIT line comes only from a pre-order,
	for that order's customer, once per invoice: a payment at the amount stamped when it was made,
	no more than the order still owes with tax, never taxed; a release (negative, on the hand-over
	ticket) on an open order, no more than is held; a return no more than is held. A consolidated
	Sales Invoice is a copy and is left alone; a Sales Invoice made by hand may not carry one."""
	rows = [r for r in doc.get("items") or [] if r.item_code == PREORDER_ITEM]
	if not rows or cint(doc.get("is_consolidated")):
		return
	if doc.doctype != "POS Invoice":
		frappe.throw(_("A pre-order payment is taken at the register, from its pre-order."))
	if len(rows) > 1:
		frappe.throw(_("One pre-order payment line per invoice."))
	row = rows[0]
	name = row.get(LINK_FIELD)
	if not name or not is_preorder(name):
		frappe.throw(_("A pre-order payment line comes from its pre-order (Take payment on Pending Order), not from "
			"the item list. Remove the line."), title=_("Pre-order payment"))
	submitting = cint(doc.docstatus) == 1
	# Submitting: lock the order so two payments for it are checked one after the other, and read
	# what is held with a lock too — under REPEATABLE READ a plain read is this transaction's
	# snapshot, taken before the lock, and would miss a payment committed in between. The lines are
	# found by the indexed ffl_preorder, so the lock covers this order's payments.
	# lazy: relies on the optimizer picking the ffl_preorder index; on a full scan the lock would
	# cover every pre-order payment line for the length of this checkout. Upgrade path: a per-order
	# running total row updated in place.
	# MariaDB may refuse either locking read (1020: the row changed after this snapshot) or time out:
	# another payment for the same order is in flight, and this one is simply tried again.
	try:
		order = frappe.db.get_value(DOCTYPE, name, ["customer", "docstatus", "grand_total", "company", "per_delivered"],
			as_dict=True, for_update=submitting)
		have = held(name, exclude=doc.name, for_update=submitting)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.throw(_("Another payment for {0} is being taken right now. Try again.").format(name),
			title=_("Pre-order payment"))
	if doc.customer != order.customer:
		frappe.throw(_("Pre-order {0} belongs to another customer.").format(name))
	doc.set(LINK_FIELD, name)
	amount = flt(row.amount, 2)
	deposits.refuse_taxed_line(doc, row, _("pre-order payment"))
	if cint(doc.is_return):
		if -amount > have + TOL:
			frappe.throw(_("Only {0} is held for {1}.").format(_money(have, doc.company), name))
		return
	locked = flt(row.get(LOCKED_RATE_FIELD), 2)
	if amount <= 0:
		# A release: what is held coming off the hand-over ticket (open_pickup, fit_release).
		if flt(row.qty) != 1 or locked >= 0 or abs(amount - locked) > TOL:
			frappe.throw(_("What is held for {0} comes off its pickup ticket (Open pickup on Pending Order), with "
				"its goods on the ticket.").format(name), title=_("Pre-order payment"))
		if cint(order.docstatus) != 1 or handed_over(order):
			frappe.throw(_("Pre-order {0} is closed; nothing of it is handed over.").format(name))
		if -amount > have + TOL:
			frappe.throw(_("Only {0} is held for {1}.").format(_money(have, doc.company), name))
		return
	if flt(row.qty) != 1 or locked <= 0 or abs(amount - locked) > TOL:
		frappe.throw(_("A payment line for {0} comes from its pre-order (Take payment), one at the amount it was "
			"made for. Remove the line and add it again.").format(name), title=_("Pre-order payment"))
	if cint(order.docstatus) != 1 or handed_over(order):
		frappe.throw(_("Pre-order {0} is closed; it takes no payment.").format(name))
	due = flt(flt(order.grand_total) - have, 2)
	if amount > due + TOL:
		frappe.throw(_("Only {0} is left to pay on {1}.").format(_money(due, order.company), name),
			title=_("Pre-order payment"))


def before_invoice_cancel(doc, method=None) -> None:
	"""POS Invoice before_cancel: a payment already paid back, or already taken off the hand-over
	ticket, stays (cancel that one first)."""
	for name in deposits.sources_on(PREORDER, doc):
		if held(name, exclude=doc.name) < -TOL:
			frappe.throw(_("The payment on {0} has already been refunded or taken off its pickup ticket. Cancel that "
				"one first.").format(name), title=_("Payment already used"))


def before_order_cancel(doc, method=None) -> None:
	"""Sales Order before_cancel: a pre-order holding money is cancelled with its refunds (Cancel &
	refund on Pending Order), never on its own."""
	if not cint(doc.get(FLAG_FIELD)) or doc.flags.get("ffl_preorder_cancel"):
		return
	if held(doc.name) > TOL or deposits.open_draft(PREORDER, doc.name, "deposit"):
		frappe.throw(_("{0} holds the customer's money. Use Cancel & refund on Pending Order.").format(doc.name),
			title=_("Pre-order"))


def on_order_cancel(doc, method=None) -> None:
	"""Sales Order on_cancel: a cancelled pre-order's quantities go back on its items, and what was
	put by for it goes back on the shelf (no entry: it never left stock)."""
	from ffl_core.crm.holds import fire_changed
	from ffl_core.reservations import notify_channel_commitments_changed

	if not cint(doc.get(FLAG_FIELD)):
		return
	# Added in place (never read and written back), and the Item's modified moves as take_slots's does.
	for item_code, qty in sorted(_wanted(doc.items).items()):
		frappe.db.sql(f"""UPDATE `tabItem` SET `{AVAILABLE_FIELD}` = `{AVAILABLE_FIELD}` + %s,
			modified = %s, modified_by = %s WHERE name = %s""", (qty, now(), frappe.session.user, item_code))
		frappe.clear_document_cache("Item", item_code)
	if not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
		return
	freed = set()
	for r in doc.items:
		serial, reserved = r.get(SERIAL_FIELD), flt(r.get(RESERVED_FIELD))
		if serial or reserved:
			frappe.db.set_value("Sales Order Item", r.name, {SERIAL_FIELD: None, RESERVED_FIELD: 0}, update_modified=False)
			freed.add(r.item_code)
			if serial:
				fire_changed(serial)
	for item_code in sorted(freed):
		notify_channel_commitments_changed(item_code, source=CHANNEL)


def on_invoice_submit(doc, method=None) -> None:
	"""POS Invoice on_submit: a hand-over ticket hands its pre-order over (:func:`hand_over`)."""
	from ffl_core.reservations import notify_channel_commitments_changed

	if not is_handover(doc):
		return
	name = doc.get(LINK_FIELD)
	hand_over(name, doc.name)
	for item_code in sorted({r.item_code for r in frappe.get_all("Sales Order Item", filters={"parent": name},
			fields=["item_code"]) if line_kind(r.item_code) == QTY}):
		notify_channel_commitments_changed(item_code, source=CHANNEL)


def on_invoice_cancel(doc, method=None) -> None:
	"""POS Invoice on_cancel: a cancelled hand-over ticket opens its pre-order again."""
	if not is_handover(doc):
		return
	name = doc.get(LINK_FIELD)
	if flt(frappe.db.get_value(DOCTYPE, name, "per_delivered")) >= 100:
		_take_back(name, doc.name)


def require_whole_order(doc, method=None) -> None:
	"""POS Invoice before_submit: a hand-over ticket hands over everything on its pre-order, to its
	customer, while the order is open — a pre-order is handed over once, whole."""
	if not is_handover(doc):
		return
	name = doc.get(LINK_FIELD)
	order = frappe.db.get_value(DOCTYPE, name, ["customer", "docstatus", "per_delivered"], as_dict=True)
	if doc.customer != order.customer:
		frappe.throw(_("Pre-order {0} belongs to another customer.").format(name))
	if cint(order.docstatus) != 1 or handed_over(order):
		frappe.throw(_("Pre-order {0} is closed; nothing of it is handed over.").format(name))
	want = _wanted(frappe.get_all("Sales Order Item", filters={"parent": name}, fields=["item_code", "qty"]))
	have: dict[str, float] = {}
	for r in doc.items:
		if r.item_code != PREORDER_ITEM:
			have[r.item_code] = have.get(r.item_code, 0) + flt(r.qty)
	missing = [code for code, qty in want.items() if have.get(code, 0) < qty]
	if missing:
		frappe.throw(_("Hand everything on {0} over on this ticket. Missing: {1}.").format(name, ", ".join(
			escape_html(frappe.db.get_value("Item", c, "item_name") or c) for c in missing)), title=_("Pre-order"))


def refuse_held_serials(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice / Delivery Note validate: a gun put by for a pre-order goes out only
	on that pre-order's hand-over ticket. Returns, merges and web / GunBroker mirrors pass (their sale
	was made already). Change the gun on the pre-order first (:func:`assign_serial`) to sell this one."""
	from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
	from ffl_core.serials import serials_for_lines

	if doc.get("is_return") or doc.get("is_consolidated") or is_channel_mirrored_invoice(doc):
		return
	if frappe.flags.in_import or frappe.flags.in_patch or frappe.flags.in_migrate:
		return
	if not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
		return
	serials = [s for line in serials_for_lines(doc.get("items") or []) for s in line]
	if not serials:
		return
	own = doc.get(LINK_FIELD) if doc.doctype == "POS Invoice" else None
	for r in frappe.db.sql(f"""SELECT i.`{SERIAL_FIELD}` AS serial, o.name, o.customer_name FROM `tabSales Order Item` i
			INNER JOIN `tabSales Order` o ON o.name = i.parent
			WHERE i.`{SERIAL_FIELD}` IN %s AND o.docstatus = 1 AND o.`{FLAG_FIELD}` = 1
				AND o.status NOT IN ('Completed', 'Closed')""", (tuple(serials),), as_dict=True):
		if r.name != own:
			frappe.throw(_("{0} is held for pre-order {1} ({2}). Change the gun on that pre-order first.").format(
				escape_html(r.serial), r.name, escape_html(r.customer_name or "")), title=_("Held for a pre-order"))


def refuse_reserved_units(doc, method=None) -> None:
	"""POS Invoice before_submit, and a Sales Invoice that moves stock: units put by for a pre-order are
	not the counter's to sell — a line may take no more than the shelf less what other pre-orders have
	put by (a hand-over ticket sells its own). No manager override: the customer has paid towards them."""
	if doc.get("is_return") or doc.get("is_consolidated") or doc.get("woo_online_order"):
		return
	if doc.doctype == "Sales Invoice" and not doc.get("update_stock"):
		return
	if not frappe.db.has_column("Sales Order Item", SERIAL_FIELD):
		return
	own = doc.get(LINK_FIELD) if is_handover(doc) else None
	wanted: dict[str, float] = {}
	for r in doc.get("items") or []:
		if r.get("item_code") and r.item_code != PREORDER_ITEM and not r.get("serial_no") \
				and not r.get("serial_and_batch_bundle"):
			wanted[r.item_code] = wanted.get(r.item_code, 0) + flt(r.get("stock_qty") or r.get("qty"))
	short = []
	for item_code, qty in sorted(wanted.items()):
		if line_kind(item_code) != QTY:
			continue
		others = reserved_qty(item_code, exclude=own)
		if others <= TOL:
			continue
		free = shelf_qty(item_code, doc.company) - others
		if qty > free + TOL:
			short.append(_("{0}: selling {1}, only {2} free — the rest is put by for pre-orders.").format(
				escape_html(item_code), f"{qty:g}", f"{max(free, 0):g}"))
	if short:
		frappe.throw("<br>".join(short), title=_("Put by for a pre-order"))


def refuse_billing_outside_handover(doc, method=None) -> None:
	"""Sales Invoice / Delivery Note / POS Invoice validate: a pre-order is billed and delivered only
	by its hand-over ticket. Made from the order on the Desk, an invoice would book the sale outside
	it and leave the money held against the order for good."""
	names = {r.get("sales_order") or r.get("against_sales_order") for r in doc.get("items") or []}
	for name in sorted(n for n in names if n):
		if is_preorder(name):
			frappe.throw(_("{0} is a pre-order. Hand it over from Pending Order (Open pickup): the register bills it "
				"there and takes off what is held.").format(name), title=_("Pre-order"))
