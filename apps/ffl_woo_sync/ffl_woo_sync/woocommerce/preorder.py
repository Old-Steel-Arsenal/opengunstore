"""Pre-orders taken on the website: paid in full at checkout, held until the goods ship.

The counter's rules (``ffl_core.preorder``) with the web order as the carrier:

* **Listing** — with nothing on our shelf and slots left on the Item, the storefront sells the
  slots (``mapping.preorder_stock``: stock_quantity, ``_osa_preorder``, ``_osa_preorder_eta``).
* **Intake** — a line for such an Item (:func:`is_preorder_line`) takes lane ``preorder``: a gun
  claims a unit and names none (a blank serial row of that lane, no reservation, no no-stock
  error), and the line takes its quantity from the Item's count under the counter's own lock
  (``ffl_core.preorder.take_slots``, :func:`take_order_slots`). The rest of the order is taken as
  any web order's; the whole order waits for the pre-ordered goods and ships as one parcel.
* **Money** — once Woo says it is paid, what the buyer paid (the order's net: lines, listed tax
  and shipping) is a Receipt Journal Entry, Woo Clearing to Pre-order Deposits, dated the day it
  was paid (:func:`sync_deposit`); nothing books to sales or tax. The invoice made when the order
  ships (``revenue.create_sales_invoice_for_web_order``) is settled from Pre-order Deposits
  (:func:`settle_invoice`, an Applied entry) instead of Woo Clearing. A refund or a cancellation
  before then pays it back (a Refund entry, Pre-order Deposits to Woo Clearing, dated the day it
  is booked) and gives the slots back. The entries carry the order (``woo_online_order``) and
  what they are (``woo_preorder_entry``); what is held is read from them, never kept.
* **Arrival** — the counter's allocator (``ffl_core.preorder.allocate_item``) takes these orders
  as a second source of demand (:func:`demands`, through the ``preorder_demand_sources`` hook):
  one queue, oldest order first, whichever channel it came in on. A gun put by for a web order is
  its confirmed serial, reserved for it as any web order's gun (``woo_reserved``); counted goods
  are put by on the line (``preorder_reserved``), which the storefront's and every channel's count
  then takes off the shelf (``mapping.pending_web_line_qty``).
* **Shipping** — the Woo order sits in ``awaiting-stock`` (a paid status the storefront plugin
  registers) until everything is put by, then goes back to ``processing``
  (:func:`sync_woo_status`): from there it is an ordinary web order — Dispose, ShipStation,
  revenue.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, nowdate

from ffl_core.utils import commit, guest_job_elevated

LANE = "preorder"
DOCTYPE = "Woo Online Order"
SYNC_JOB = "ffl_woo_sync.woocommerce.preorder.sync_order"
ALLOCATE_ITEMS_JOB = "ffl_core.preorder.allocate_items"
RECEIPT, REFUND, APPLIED = "Receipt", "Refund", "Applied"
AWAITING_STOCK = "awaiting-stock"
PROCESSING = "processing"
TOL = 0.005


# ------------------------------------------------------------------ the order


def preorder_lines(order) -> list:
	# Attribute access: a Document, a frappe._dict and a test double all expose the tables.
	return [l for l in (getattr(order, "lines", None) or []) if (getattr(l, "lane", None) or "") == LANE]


def is_preorder_order(order) -> bool:
	if isinstance(order, str):
		return bool(frappe.db.exists("Woo Online Order Line", {"parent": order, "lane": LANE}))
	return bool(preorder_lines(order))


def _gun_rows(order) -> list:
	return [r for r in (getattr(order, "serials", None) or []) if (getattr(r, "fulfillment", None) or "") == LANE]


def ready(order) -> bool:
	"""Everything the order waits for on pre-order is here and put by for it (True for an order
	with no pre-order line): every gun unit of the lane names its gun, every counted line has its
	units put by. A gun's line is answered by its serial rows."""
	from ffl_core.firearm import is_firearm_item
	from ffl_core.preorder import QTY, line_kind

	for line in preorder_lines(order):
		if is_firearm_item(line.item_code):
			continue
		if line_kind(line.item_code) == QTY and flt(line.get("preorder_reserved")) < flt(line.qty) - TOL:
			return False
	return all((r.get("confirmed_serial") or "").strip() for r in _gun_rows(order))


def refuse_unless_ready(order) -> None:
	"""The shipping and pickup exits: a pre-order leaves only once all of it is here."""
	if not ready(order):
		frappe.throw(_("{0} is a pre-order whose goods have not all arrived. It ships once they are put by "
			"for it.").format(order.name), title=_("Pre-order"))


# ------------------------------------------------------------------ intake (webhook)


def is_preorder_line(item_code: str, spent: int = 0) -> bool:
	"""Whether a web order line of *item_code* is a pre-order: the storefront sells it on
	pre-order (``mapping.preorder_open``) and none is left on our shelf — the store's own
	sellable count (``mapping.stock_for_item``) less what this order has already spent of it.
	A count that cannot be read is not a pre-order: the line takes the ordinary path, which
	says what went wrong."""
	from ffl_woo_sync.woocommerce import mapping

	row = mapping.preorder_item(item_code)
	if not row or not mapping.preorder_open(item_code, row):
		return False
	try:
		local = int(mapping.stock_for_item(item_code) or 0)
	except Exception:
		return False
	return local - int(spent or 0) <= 0


def take_order_slots(order) -> None:
	"""Take the pre-order lines' quantities from their Items' counts (``take_slots``: the Item row
	locked, all or nothing per Item). A line records what it took (``preorder_slots``), which is
	what a cancellation gives back. Too few left (another order took them first) is recorded on
	the line for staff, never raised: the order is paid and must be recorded."""
	from ffl_core.preorder import take_slots

	from ffl_woo_sync.woocommerce.webhook import _log_error

	lines = [l for l in preorder_lines(order) if not cint(l.get("preorder_slots")) and l.item_code]
	wanted: dict[str, int] = {}
	for l in lines:
		wanted[l.item_code] = wanted.get(l.item_code, 0) + cint(l.qty)
	for item_code in sorted(wanted):
		mine = [l for l in lines if l.item_code == item_code]
		try:
			take_slots({item_code: wanted[item_code]})
		except frappe.ValidationError as exc:
			frappe.clear_last_message()
			reason = _("Pre-order oversold: {0} Staff action required.").format(str(exc))
			for l in mine:
				l.reservation_error = reason
			_log_error(title=f"Woo webhook: pre-order oversold for {item_code}",
				message=f"Woo order {order.get('woo_order_id')}: {exc}")
			continue
		for l in mine:
			l.preorder_slots = cint(l.qty)
			l.reservation_error = ""


def after_order_saved(order, body: dict | None = None) -> None:
	"""After a web order is recorded or updated: a pre-order keeps when Woo says it was paid
	(``paid_at``, the receipt's date) and queues :func:`sync_order` after the commit. Never raises."""
	try:
		if not preorder_lines(order):
			return
		paid = (body or {}).get("date_paid")
		if paid and not order.get("paid_at"):
			order.paid_at = get_datetime(paid)
			frappe.db.set_value(DOCTYPE, order.name, "paid_at", order.paid_at, update_modified=False)
		frappe.enqueue(SYNC_JOB, queue="short", enqueue_after_commit=True, order=order.name)
	except Exception:
		frappe.log_error(title=f"Web pre-order not synced: {order.get('name')}", message=frappe.get_traceback())


def on_order_cancelled(order_name: str) -> None:
	"""Woo cancelled / refunded / trashed the order (``webhook._handle_order_cancelled``, before
	it releases the order's guns): its slots go back, what was put by for it is let go (its guns
	are released by the caller; the next order waiting gets them once this commits), and the money
	is paid back by :func:`sync_order`. The Items are locked first, the order the allocator and
	the intake take them in."""
	from ffl_core.preorder import give_back_slots
	from ffl_core.reservations import lock_item_reservations

	# Raw SQL, like the handler's own claim read: it runs on every cancel webhook, and the
	# wholesale ``frappe.get_all`` mocks of the webhook suite must not answer it.
	if not frappe.db.has_column("Woo Online Order Line", "preorder_slots"):
		return
	# Only an order still waiting: one that shipped keeps what it used (a terminal status after
	# that is alerted on, never reversed — webhook._handle_order_cancelled).
	status = frappe.db.sql("SELECT order_status FROM `tabWoo Online Order` WHERE name = %s", order_name)
	if not status or status[0][0] != "Pending Fulfillment":
		return
	lines = frappe.db.sql("""SELECT name, item_code, preorder_slots, preorder_reserved
		FROM `tabWoo Online Order Line` WHERE parenttype = %s AND parent = %s AND lane = %s""",
		(DOCTYPE, order_name, LANE), as_dict=True)
	if not lines:
		return
	guns = frappe.db.sql("""SELECT name, item_code, confirmed_serial FROM `tabWoo Online Order Serial`
		WHERE parenttype = %s AND parent = %s AND fulfillment = %s""", (DOCTYPE, order_name, LANE), as_dict=True)
	for item_code in sorted({l.item_code for l in lines if l.item_code}):
		lock_item_reservations(item_code)
	slots: dict[str, int] = {}
	freed = set()
	for l in lines:
		slots[l.item_code] = slots.get(l.item_code, 0) + cint(l.preorder_slots)
		if flt(l.preorder_reserved):
			freed.add(l.item_code)
		frappe.db.set_value("Woo Online Order Line", l.name, {"preorder_slots": 0, "preorder_reserved": 0},
			update_modified=False)
	for g in guns:
		if (g.confirmed_serial or "").strip():
			freed.add(g.item_code)
			frappe.db.set_value("Woo Online Order Serial", g.name, "confirmed_serial", "", update_modified=False)
	give_back_slots(slots)
	if freed:
		frappe.enqueue(ALLOCATE_ITEMS_JOB, queue="short", enqueue_after_commit=True, item_codes=sorted(freed),
			company=_company())
	frappe.enqueue(SYNC_JOB, queue="short", enqueue_after_commit=True, order=order_name)


# ------------------------------------------------------------------ arrival (ffl_core.preorder)


def _paid_statuses() -> tuple:
	from ffl_woo_sync.woocommerce.fulfillment import _PAID_STATUSES

	return tuple(_PAID_STATUSES)


def demands(item_code: str) -> list:
	"""``preorder_demand_sources`` hook: what paid web pre-orders still wait for of *item_code* —
	one entry per gun unit without its gun, or per counted line short of units — read with a lock
	(the allocator decides on it). Placed = when the order was recorded."""
	from ffl_core.firearm import is_firearm_item
	from ffl_core.preorder import QTY, line_kind

	if not frappe.db.has_column("Woo Online Order Line", "preorder_reserved"):
		return []
	values = {"item": item_code, "paid": _paid_statuses(), "lane": LANE}
	if is_firearm_item(item_code):
		rows = frappe.db.sql("""
			SELECT s.name AS `row`, s.parent AS name, o.creation AS placed
			FROM `tabWoo Online Order Serial` s INNER JOIN `tabWoo Online Order` o ON o.name = s.parent
			WHERE s.parenttype = 'Woo Online Order' AND s.item_code = %(item)s AND s.fulfillment = %(lane)s
				AND COALESCE(TRIM(s.confirmed_serial), '') = '' AND o.order_status = 'Pending Fulfillment'
				AND o.woo_status IN %(paid)s
			ORDER BY o.creation, o.name, s.idx FOR UPDATE""", values, as_dict=True)
		return [frappe._dict(doctype=DOCTYPE, name=r.name, row=r.row, need=1, placed=r.placed, reserved=0.0)
			for r in rows]
	if line_kind(item_code) != QTY:
		return []
	rows = frappe.db.sql("""
		SELECT l.name AS `row`, l.parent AS name, l.qty, COALESCE(l.preorder_reserved, 0) AS reserved,
			o.creation AS placed
		FROM `tabWoo Online Order Line` l INNER JOIN `tabWoo Online Order` o ON o.name = l.parent
		WHERE l.parenttype = 'Woo Online Order' AND l.item_code = %(item)s AND l.lane = %(lane)s
			AND o.order_status = 'Pending Fulfillment' AND o.woo_status IN %(paid)s
		ORDER BY o.creation, o.name, l.idx FOR UPDATE""", values, as_dict=True)
	return [frappe._dict(doctype=DOCTYPE, name=r.name, row=r.row, need=flt(r.qty) - flt(r.reserved),
		placed=r.placed, reserved=flt(r.reserved)) for r in rows if flt(r.qty) - flt(r.reserved) > TOL]


def fill(demand, *, serial: str | None = None, qty: float = 0) -> None:
	"""Put a gun or units by for one web demand: the gun becomes the unit's confirmed serial and
	is reserved for the order (``woo_reserved``, as any web order's gun); units go on the line. An
	order with everything here then goes back to ``processing`` on Woo (:func:`sync_order`)."""
	from ffl_woo_sync.woocommerce.webhook import _reserve_serial

	if serial:
		frappe.db.set_value("Woo Online Order Serial", demand.row, {"confirmed_serial": serial,
			"stock_hold_serial": "", "reservation_error": ""}, update_modified=False)
		_reserve_serial(serial, demand.name)
	else:
		frappe.db.set_value("Woo Online Order Line", demand.row, "preorder_reserved",
			flt(flt(demand.reserved) + flt(qty), 6), update_modified=False)
	if ready(frappe.get_doc(DOCTYPE, demand.name)):
		frappe.enqueue(SYNC_JOB, queue="short", enqueue_after_commit=True, order=demand.name)


def waiting(item_codes) -> bool:
	"""Whether a web pre-order still open has a line of any of *item_codes* (the receipt's gate)."""
	if not item_codes or not frappe.db.has_column("Woo Online Order Line", "preorder_reserved"):
		return False
	return bool(frappe.db.sql("""SELECT 1 FROM `tabWoo Online Order Line` l
		INNER JOIN `tabWoo Online Order` o ON o.name = l.parent
		WHERE l.parenttype = 'Woo Online Order' AND l.lane = %s AND l.item_code IN %s
			AND o.order_status = 'Pending Fulfillment' LIMIT 1""", (LANE, tuple(item_codes))))


demands.doctype = DOCTYPE
demands.fill = fill
demands.waiting = waiting


# ------------------------------------------------------------------ money


def _company() -> str:
	from ffl_woo_sync.woocommerce.revenue import _default_company

	return _default_company()


def _deposit_account(company: str) -> str:
	from ffl_core.setup import preorder as setup

	account = setup.preorder_account(company)
	if not account:
		setup.ensure()
		account = setup.preorder_account(company)
	if not account:
		frappe.throw(_("{0} has no {1} account yet. Run bench migrate, or ask a manager.").format(
			company, setup.ACCOUNT_TITLE))
	return account


def _journal(order, company: str, posting_date, kind: str, lines: list, remark: str) -> str:
	"""Submit a Journal Entry for *order* of (account, debit, credit[, row fields]) lines."""
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	je.user_remark = remark
	je.woo_online_order = order.name
	je.woo_preorder_entry = kind
	for account, debit, credit, *extra in lines:
		row = {"account": account, "debit_in_account_currency": flt(debit, 2),
			"credit_in_account_currency": flt(credit, 2)}
		if extra:
			row.update(extra[0])
		je.append("accounts", row)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def deposit_totals(order_name: str, account: str) -> frappe._dict:
	"""What the order's submitted entries put through *account* (Pre-order Deposits): received
	(Receipt), paid back (Refund), applied to its bill (Applied) — each a positive amount — and
	``held``, what is left."""
	out = frappe._dict(receipts=0.0, refunds=0.0, applied=0.0)
	for kind, amount in frappe.db.sql("""
			SELECT je.woo_preorder_entry, COALESCE(SUM(a.credit - a.debit), 0)
			FROM `tabJournal Entry Account` a INNER JOIN `tabJournal Entry` je ON je.name = a.parent
			WHERE je.docstatus = 1 AND je.woo_online_order = %s AND a.account = %s
			GROUP BY je.woo_preorder_entry""", (order_name, account)):
		key = {RECEIPT: "receipts", REFUND: "refunds", APPLIED: "applied"}.get(kind)
		if key:
			out[key] = flt(abs(flt(amount)), 2)
	out.held = flt(out.receipts - out.refunds - out.applied, 2)
	return out


def _collected_on_invoice(order_name: str) -> bool:
	"""A submitted Payment Entry collected one of the order's invoices into Woo Clearing (the
	ordinary web path): that money never went through Pre-order Deposits, so it is not received
	again here."""
	return bool(frappe.db.sql("""SELECT 1 FROM `tabPayment Entry Reference` r
		INNER JOIN `tabPayment Entry` pe ON pe.name = r.parent
		INNER JOIN `tabSales Invoice` si ON si.name = r.reference_name
		WHERE pe.docstatus = 1 AND r.reference_doctype = 'Sales Invoice' AND si.woo_online_order = %s
			AND si.docstatus = 1 LIMIT 1""", order_name))


def sync_deposit(order) -> dict:
	"""Make what the order's entries hold in Pre-order Deposits match what the buyer has paid and
	not been refunded: the order's net (``refunds.target_net``) once Woo says it is paid, nothing
	once it is cancelled. Short → a Receipt, Woo Clearing to Pre-order Deposits, dated the day it
	was paid (the first) or today; over → a Refund back to Woo Clearing, today, no more than is
	still held (money already applied to the bill is the invoice's to reverse). Under the order's
	row lock; safe to run any number of times."""
	from ffl_woo_sync.woocommerce.fulfillment import _is_paid_woo_status
	from ffl_woo_sync.woocommerce.refunds import target_net
	from ffl_woo_sync.woocommerce.revenue import _ensure_clearing_account

	name = order if isinstance(order, str) else order.name
	frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	order = frappe.get_doc(DOCTYPE, name)
	if not preorder_lines(order):
		return {"status": "not_preorder"}
	company = _company()
	deposits = _deposit_account(company)
	t = deposit_totals(order.name, deposits)
	received = flt(t.receipts - t.refunds, 2)
	paid = _is_paid_woo_status(order.woo_status) and order.order_status != "Cancelled"
	target = flt(target_net(order), 2) if paid else 0.0
	tag = _("Web pre-order {0} (Woo #{1})").format(order.name, order.woo_order_id)
	if target > received + TOL:
		if _collected_on_invoice(order.name):
			frappe.log_error(title=f"Web pre-order {order.name}: invoice collected outside Pre-order Deposits",
				message=f"Paid {target}, held {received}: a Payment Entry already collected its invoice into "
					"Woo Clearing, so nothing more is received into Pre-order Deposits. Review the order's entries.")
			return {"status": "collected_elsewhere"}
		amount = flt(target - received, 2)
		first = t.receipts <= TOL
		when = getdate(order.paid_at) if (first and order.get("paid_at")) else getdate(nowdate())
		je = _journal(order, company, when, RECEIPT,
			[(_ensure_clearing_account(company), amount, 0), (deposits, 0, amount)],
			_("{0}: paid on WooCommerce, held until it ships.").format(tag))
		return {"status": "received", "journal_entry": je, "amount": amount, "first": first}
	if received > target + TOL:
		amount = flt(min(received - target, t.held), 2)
		if amount <= TOL:
			return {"status": "applied"}
		je = _journal(order, company, getdate(nowdate()), REFUND,
			[(deposits, amount, 0), (_ensure_clearing_account(company), 0, amount)],
			_("{0}: refunded on WooCommerce.").format(tag))
		return {"status": "refunded", "journal_entry": je, "amount": amount}
	return {"status": "in_sync"}


def settle_invoice(si, order) -> float:
	"""The order shipped and its invoice is booked: what is held for it pays the invoice — an
	Applied entry, Pre-order Deposits to the customer's receivable against the invoice, on the
	invoice's date — instead of a Woo Clearing collection. Returns the amount applied."""
	deposits = _deposit_account(si.company)
	held = deposit_totals(order.name, deposits).held
	amount = flt(min(held, flt(si.outstanding_amount)), 2)
	if amount <= TOL:
		return 0.0
	_journal(order, si.company, si.posting_date, APPLIED, [
		(deposits, amount, 0),
		(si.debit_to, 0, amount, {"party_type": "Customer", "party": si.customer,
			"reference_type": "Sales Invoice", "reference_name": si.name}),
	], _("Web pre-order {0} (Woo #{1}): paid from Pre-order Deposits on {2}.").format(
		order.name, order.woo_order_id, si.name))
	return amount


def _cancel_entries(names) -> list[str]:
	for name in names:
		je = frappe.get_doc("Journal Entry", name)
		je.flags.ignore_permissions = True
		je.cancel()
	return list(names)


def unsettle_invoice(si) -> list[str]:
	"""Cancel the Applied entries settling *si* (a refund amends the invoice: the amended one is
	settled again, ``revenue._ensure_paid``). Returns the cancelled entries."""
	return _cancel_entries(frappe.db.sql_list("""SELECT DISTINCT je.name FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` a ON a.parent = je.name
		WHERE je.docstatus = 1 AND je.woo_preorder_entry = %s AND a.reference_type = 'Sales Invoice'
			AND a.reference_name = %s""", (APPLIED, si.name)))


def release_for_pickup(ticket) -> str | None:
	"""An in-store pickup's register ticket was submitted: it takes the buyer's WooCommerce payment
	into Woo Clearing again (``pickup._attach_payment``), so what the order holds moves from
	Pre-order Deposits back to Woo Clearing on the ticket's date (an Applied entry) — the money
	counted once, released on the day the goods went out."""
	link = (ticket.get("woo_online_order") or "").strip()
	if not link or not is_preorder_order(link):
		return None
	order = frappe.get_doc(DOCTYPE, link)
	from ffl_woo_sync.woocommerce.revenue import _ensure_clearing_account

	deposits = _deposit_account(ticket.company)
	held = deposit_totals(order.name, deposits).held
	if held <= TOL:
		return None
	return _journal(order, ticket.company, ticket.posting_date, APPLIED,
		[(deposits, held, 0), (_ensure_clearing_account(ticket.company), 0, held)],
		_("Web pre-order {0} (Woo #{1}): picked up on {2}.").format(order.name, order.woo_order_id, ticket.name))


def restore_on_pickup_cancel(ticket) -> list[str]:
	"""The pickup ticket was cancelled: its release is undone (the money is held again)."""
	link = (ticket.get("woo_online_order") or "").strip()
	if not link:
		return []
	return _cancel_entries(frappe.db.sql_list("""SELECT je.name FROM `tabJournal Entry` je
		WHERE je.docstatus = 1 AND je.woo_online_order = %s AND je.woo_preorder_entry = %s
			AND NOT EXISTS (SELECT 1 FROM `tabJournal Entry Account` a WHERE a.parent = je.name
				AND a.reference_type = 'Sales Invoice')""", (link, APPLIED)))


# ------------------------------------------------------------------ the job


@guest_job_elevated()
def sync_order(order: str) -> dict:
	"""The job every pre-order event queues: the deposit (:func:`sync_deposit`), then the Woo
	status (:func:`sync_woo_status`), after a commit so no lock is held over the HTTP call. A
	first payment also asks the allocator for what is already free. Safe to run twice."""
	doc = frappe.get_doc(DOCTYPE, order)
	if not preorder_lines(doc):
		return {"status": "not_preorder"}
	out = sync_deposit(doc)
	if out.get("status") == "received" and out.get("first"):
		frappe.enqueue(ALLOCATE_ITEMS_JOB, queue="short", enqueue_after_commit=True,
			item_codes=sorted({l.item_code for l in preorder_lines(doc) if l.item_code}), company=_company())
	commit()
	out["woo_status"] = sync_woo_status(order)
	return out


def sync_woo_status(order_name: str) -> str | None:
	"""Woo's status for a paid pre-order still to ship: ``awaiting-stock`` while goods are missing
	(the storefront plugin's paid status; ShipStation holds it), ``processing`` once everything is
	put by, so it ships as any web order. Only between those two — any other status is Woo's or
	staff's. An order with ship-to-store lines is left to that lane's own status rules. Never
	raises; returns the status written, if any."""
	from ffl_woo_sync.woocommerce.client import WooCommerceClient

	order = frappe.get_doc(DOCTYPE, order_name)
	if order.order_status != "Pending Fulfillment" or not preorder_lines(order):
		return None
	if any((l.get("lane") or "") == "to_store" for l in order.lines):
		return None
	current = (order.woo_status or "").strip().lower()
	if current not in (PROCESSING, AWAITING_STOCK):
		return None
	want = PROCESSING if ready(order) else AWAITING_STOCK
	if want == current:
		return None
	try:
		WooCommerceClient.from_settings(order.site or "retail").update_order(order.woo_order_id, {"status": want})
	except Exception:
		frappe.log_error(title=f"Web pre-order {order.name}: Woo status not set to {want}",
			message=frappe.get_traceback())
		return None
	frappe.db.set_value(DOCTYPE, order.name, "woo_status", want, update_modified=False)
	return want


# ------------------------------------------------------------------ the sub-ledger


def deposit_rows(company: str, as_of) -> list[dict]:
	"""``preorder_deposit_rows`` hook (the Pre-order Deposits report): one row per web pre-order
	with an entry on the account dated on or before *as_of*, in the counter rows' shape."""
	from ffl_core.ffl_core.report.pre_order_deposits.pre_order_deposits import aging_bucket
	from ffl_core.setup.preorder import preorder_account
	from frappe.utils import date_diff

	account = preorder_account(company)
	if not account or not frappe.db.has_column("Journal Entry", "woo_preorder_entry"):
		return []
	by: dict[str, frappe._dict] = {}
	for r in frappe.db.sql("""
			SELECT je.woo_online_order AS ord, je.woo_preorder_entry AS kind, je.posting_date,
				SUM(a.credit - a.debit) AS amount
			FROM `tabJournal Entry Account` a INNER JOIN `tabJournal Entry` je ON je.name = a.parent
			WHERE je.docstatus = 1 AND je.company = %s AND je.posting_date <= %s AND a.account = %s
				AND COALESCE(je.woo_online_order, '') != ''
			GROUP BY je.name, je.woo_online_order, je.woo_preorder_entry, je.posting_date""",
			(company, getdate(as_of), account), as_dict=True):
		o = by.setdefault(r.ord, frappe._dict(collected=0.0, refunded=0.0, applied=0.0, first=None))
		amount = flt(r.amount)
		if r.kind == RECEIPT:
			o.collected += amount
			o.first = min(o.first, r.posting_date) if o.first else r.posting_date
		elif r.kind == REFUND:
			o.refunded -= amount
		else:
			o.applied -= amount
	if not by:
		return []
	orders = {o.name: o for o in frappe.get_all(DOCTYPE, filters={"name": ["in", list(by)]},
		fields=["name", "customer", "customer_name", "order_status"])}
	items: dict[str, list[str]] = {}
	for r in frappe.get_all("Woo Online Order Line", filters={"parent": ["in", list(by)], "parenttype": DOCTYPE},
			fields=["parent", "item_code", "qty"], order_by="idx"):
		items.setdefault(r.parent, []).append(f"{flt(r.qty):g} × {frappe.db.get_value('Item', r.item_code, 'item_name') or r.item_code}")
	out = []
	for name, o in by.items():
		doc = orders.get(name) or frappe._dict()
		balance = flt(o.collected - o.refunded - o.applied, 2)
		status = {"Cancelled": _("Cancelled"), "Transferred": _("Completed")}.get(doc.order_status, _("Open"))
		out.append({
			"sales_order": name, "ref_doctype": DOCTYPE, "channel": _("Web"), "customer": doc.customer,
			"customer_name": doc.customer_name, "items": ", ".join(items.get(name, [])),
			"first_payment": o.first, "collected": flt(o.collected, 2), "refunded": flt(o.refunded, 2),
			"applied": flt(o.applied, 2), "balance": balance, "status": status,
			"aging": aging_bucket(date_diff(getdate(as_of), o.first)) if balance > TOL and o.first else "",
		})
	return out
