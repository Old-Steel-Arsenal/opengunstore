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
* **Cancel** — :func:`cancel_preorder`: the order is cancelled, its quantities go back, and every
  payment is returned in full the way it came in (draft returns the register checks out).
  Nothing is kept back.

Handing the goods over (and releasing what is held on that bill) is not here yet: until it is, a
pre-order payment line only ever adds money or returns it.
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
from ffl_core.setup.preorder import AVAILABLE_FIELD, ETA_FIELD, FLAG_FIELD, LINK_FIELD, PREORDER_ITEM

DOCTYPE = "Sales Order"


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


def local_stock(item_code: str, company: str) -> float:
	"""Units of *item_code* on our own shelves (``storage.on_our_shelf``): a gun's Active serials,
	anything else's quantity in the company's sellable warehouses. What the counter could sell now."""
	from ffl_core.firearm import is_firearm_item
	from ffl_core.storage import excluded_warehouses, on_our_shelf, sellable_warehouses

	if is_firearm_item(item_code):
		excluded = excluded_warehouses()
		return float(sum(1 for s in frappe.get_all("Serial No", filters={"item_code": item_code, "status": "Active"},
			fields=["warehouse", "consigned_out"]) if on_our_shelf("Active", s.warehouse, s.consigned_out,
				excluded=excluded)))
	warehouses = sellable_warehouses(company)
	if not warehouses:
		return 0.0
	return flt(frappe.db.sql("""SELECT COALESCE(SUM(actual_qty), 0) FROM `tabBin`
		WHERE item_code = %s AND warehouse IN %s""", (item_code, tuple(warehouses)))[0][0])


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
	        dict or JSON string. A gun is one unit per line. Every item must have pre-order slots left
	        and none on our shelves.

	Returns:
	    {"sales_order", "customer", "grand_total": what the order comes to with tax, "eta"}.
	"""
	from ffl_core import price_approval
	from ffl_core.api._helpers import coerce_payload
	from ffl_core.api.manual_order import (_apply_default_tax_template, _default_pos_profile, _resolve_pos_profile,
		_store_warehouse)
	from ffl_core.firearm import is_firearm_item

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
		if is_firearm_item(item_code) and qty != 1:
			frappe.throw(_("Line {0}: a gun is one unit per line.").format(i))
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
	if flt(so.per_delivered) or flt(so.per_billed):
		frappe.throw(_("Goods on {0} have already been handed over or billed.").format(so.name))
	so.add_comment("Comment", _("Cancelled: {0}").format(escape_html(reason.strip())))
	so.flags.ignore_permissions = True
	so.flags.ffl_preorder_cancel = True
	so.cancel()
	return {"cancelled": True, "refunds": _refund_all(so)}


@frappe.whitelist(methods=["POST"])
def refund_preorder(sales_order: str) -> dict:
	"""The draft returns paying back what a cancelled pre-order still holds — the open ones, or new
	ones for every payment not returned yet.

	Args:
	    sales_order: A cancelled pre-order.

	Returns:
	    {"refunds": [{"pos_invoice", "amount"}]}.
	"""
	frappe.only_for(COUNTER_ROLES)
	so = _order(sales_order)
	if so.docstatus != 2:
		frappe.throw(_("Cancel the pre-order to refund it (Cancel & refund)."))
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
	"""Pre-orders for the Pending Order page: every open one (stage ``waiting``), and cancelled ones
	still holding money to pay back (``blocked``).

	Args:
	    customer: Only this customer's. Default: everyone's.

	Returns:
	    [{"source": "preorder", "sales_order", "customer", "customer_name", "posting_date", "grand_total",
	    "held", "outstanding_amount", "paid", "cancelled", "eta", "items": [{"item_code", "item_name", "qty"}],
	    "drafts": {"payment", "refunds": [...]}, "stage", "blocked_reason", "age_days", "overdue"}].
	"""
	from ffl_core.api.manual_order import STAGE_BLOCKED, STAGE_WAITING, age_days

	frappe.only_for(COUNTER_ROLES)
	if not frappe.db.has_column(DOCTYPE, FLAG_FIELD):
		return []
	filters = {FLAG_FIELD: 1, "docstatus": ["in", [1, 2]], "status": ["not in", ["Completed", "Closed"]]}
	if customer:
		filters["customer"] = customer
	# lazy: every open pre-order and every cancelled one, one held read per order; a store has tens.
	# Upgrade path: one aggregate over POS Invoice Item for the page, and cancelled ones by held > 0.
	orders = frappe.get_all(DOCTYPE, filters=filters, fields=["name", "customer", "customer_name",
		"transaction_date", "grand_total", "docstatus", "company"], order_by="transaction_date asc, creation asc")
	if not orders:
		return []
	lines: dict[str, list] = {}
	for r in frappe.get_all("Sales Order Item", filters={"parent": ["in", [o.name for o in orders]]},
			fields=["parent", "item_code", "item_name", "qty"], order_by="idx"):
		lines.setdefault(r.pop("parent"), []).append(r)
	etas = dict(frappe.get_all("Item", filters={"name": ["in", list({l.item_code for ls in lines.values() for l in ls})]},
		fields=["name", ETA_FIELD], as_list=True)) if lines else {}
	today = nowdate()
	out = []
	for o in orders:
		have = held(o.name)
		cancelled = o.docstatus == 2
		if cancelled and have <= TOL:
			continue
		items = lines.get(o.name, [])
		item_etas = [getdate(etas[l.item_code]) for l in items if etas.get(l.item_code)]
		outstanding = 0.0 if cancelled else max(0.0, flt(flt(o.grand_total) - have, 2))
		out.append({
			"source": "preorder", "sales_order": o.name, "customer": o.customer, "customer_name": o.customer_name,
			"posting_date": o.transaction_date, "grand_total": flt(o.grand_total), "held": have,
			"outstanding_amount": outstanding, "paid": outstanding <= TOL, "cancelled": cancelled,
			"eta": str(max(item_etas)) if item_etas else None, "items": items,
			"drafts": {"payment": deposits.open_draft(PREORDER, o.name, "deposit"),
				"refunds": deposits.open_drafts(PREORDER, o.name, "refund")},
			"stage": STAGE_BLOCKED if cancelled else STAGE_WAITING,
			"blocked_reason": _("Cancelled — refund {0}").format(_money(have, o.company)) if cancelled else "",
			# A pre-order waits for its goods by design: its age is shown, never flagged overdue.
			"age_days": age_days(o.transaction_date, today), "overdue": False,
		})
	return out


# ------------------------------------------------------------------ hooks


def guard_payment_lines(doc, method=None) -> None:
	"""POS Invoice / Sales Invoice validate: a PREORDER-DEPOSIT line comes only from a pre-order,
	for that order's customer, once per invoice: a payment at the amount stamped when it was made,
	no more than the order still owes with tax, never taxed; a return no more than is held. A
	consolidated Sales Invoice is a copy and is left alone; a Sales Invoice made by hand may not carry
	one. Until hand-over is built, a pre-order line never releases money (no negative payment line)."""
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
		order = frappe.db.get_value(DOCTYPE, name, ["customer", "docstatus", "grand_total", "company"], as_dict=True,
			for_update=submitting)
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
	if flt(row.qty) != 1 or amount <= 0 or locked <= 0 or abs(amount - locked) > TOL:
		frappe.throw(_("A payment line for {0} comes from its pre-order (Take payment), one at the amount it was "
			"made for. Remove the line and add it again.").format(name), title=_("Pre-order payment"))
	if cint(order.docstatus) != 1:
		frappe.throw(_("Pre-order {0} is cancelled; it takes no payment.").format(name))
	due = flt(flt(order.grand_total) - have, 2)
	if amount > due + TOL:
		frappe.throw(_("Only {0} is left to pay on {1}.").format(_money(due, order.company), name),
			title=_("Pre-order payment"))


def before_invoice_cancel(doc, method=None) -> None:
	"""POS Invoice before_cancel: a payment already paid back stays (cancel the return first)."""
	for name in deposits.sources_on(PREORDER, doc):
		if held(name, exclude=doc.name) < -TOL:
			frappe.throw(_("The payment on {0} has already been refunded. Cancel that return first.").format(name),
				title=_("Payment already refunded"))


def before_order_cancel(doc, method=None) -> None:
	"""Sales Order before_cancel: a pre-order holding money is cancelled with its refunds (Cancel &
	refund on Pending Order), never on its own."""
	if not cint(doc.get(FLAG_FIELD)) or doc.flags.get("ffl_preorder_cancel"):
		return
	if held(doc.name) > TOL or deposits.open_draft(PREORDER, doc.name, "deposit"):
		frappe.throw(_("{0} holds the customer's money. Use Cancel & refund on Pending Order.").format(doc.name),
			title=_("Pre-order"))


def on_order_cancel(doc, method=None) -> None:
	"""Sales Order on_cancel: a cancelled pre-order's quantities go back on its items."""
	if not cint(doc.get(FLAG_FIELD)):
		return
	# Added in place (never read and written back), and the Item's modified moves as take_slots's does.
	for item_code, qty in sorted(_wanted(doc.items).items()):
		frappe.db.sql(f"""UPDATE `tabItem` SET `{AVAILABLE_FIELD}` = `{AVAILABLE_FIELD}` + %s,
			modified = %s, modified_by = %s WHERE name = %s""", (qty, now(), frappe.session.user, item_code))
		frappe.clear_document_cache("Item", item_code)
