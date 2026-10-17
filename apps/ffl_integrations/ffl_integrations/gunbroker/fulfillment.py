"""What the Item form's Pending badge shows for GunBroker.

The badge already merges two hold registries — the counter's and
WooCommerce's — and a gun sold on GunBroker belongs beside them: it is Active,
in its warehouse, and looks like ordinary shelf stock to anybody reading the
serials tab. Without this lane the one channel whose sale cannot be cancelled
is the one channel the form says nothing about.

Display only. Every decision reads `ffl_core.reservations.channel_reservation`
instead, for the reason its docstring gives: this function answers an
under-privileged caller with a silent ``{}``, and a gate handed ``{}`` concludes
"nothing is held" and lets the gun through.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from ffl_core.api.manual_order import (
	_ROLES,
	_require_valid_ffl,
	_shipstation_enabled,
	invoice_ship_address,
	overdue_limit_days,
	stamp_stage,
	shipstation_pushed,
)
from ffl_core.compliance.cr_transfer import (
	_persist_cr_details,
	_validate_cr_details,
	ensure_cr_ffl_record,
	reject_cr_details_on_non_cr_order,
)
from ffl_core.firearm import (
	is_antique_item,
	is_own_license,
	is_regulated_firearm_item,
	issue_antique_serial,
)
from ffl_core.reservations import (
	CHANNEL_GUNBROKER,
	lock_item_reservations,
	notify_channel_commitments_changed,
	uniform_free_serials,
)
from ffl_core.utils import commit as safe_commit
from ffl_integrations.gunbroker import mapping as gb_mapping
from ffl_integrations.gunbroker import orders as gb_orders
from ffl_integrations.notifications import notify_admins, safe_log_error


@frappe.whitelist()
def reserved_gb_serials(item_code: str) -> dict:
	"""GunBroker hold registry for one Item's serials — the DISPLAY half.

	Args:
	    item_code: Item whose serials to check.

	Returns:
	    ``{serial: {source, ref, kind, tooltip, url}}`` — only held serials
	    appear. ``kind`` is ``gb_sold`` for a gun with a GunBroker order against
	    it, or ``gb_sold_unknown`` for one the sweep believes was sold there
	    while the order has not arrived yet.

	Permissions:
	    Canonical shipping roles, matching the counter and web registries this
	    merges with — it names which regulated serials sit on which order. A
	    caller WITHOUT the role gets an empty ``{}`` back silently, so an empty
	    result from THIS function is not proof of "no holds".
	"""
	from ffl_core.api.manual_order import _has_ship_role

	if not _has_ship_role():
		return {}
	item_code = (item_code or "").strip()
	if not item_code:
		return {}
	meta = frappe.get_meta("Serial No")
	if not meta.has_field("gb_reserved"):
		return {}

	out: dict[str, dict] = {}
	for row in frappe.get_all(
			"Serial No",
			filters={"item_code": item_code, "gb_reserved": 1},
			fields=["name", "gb_order"]):
		ref = row.get("gb_order") or ""
		out[row["name"]] = {
			"source": "gunbroker", "ref": ref, "kind": "gb_sold",
			"tooltip": _("Sold on GunBroker (order {0})").format(ref or "—"),
			"url": f"/app/gunbroker-order/{ref}" if ref else "",
		}
	if not meta.has_field("gb_sold_unknown"):
		return out
	# The doubtful ones matter MORE on this form, not less: nothing else on the
	# page distinguishes them from shelf stock, and the counter will refuse to
	# sell them — so somebody looking at the serials tab needs to know why.
	for row in frappe.get_all(
			"Serial No",
			filters={"item_code": item_code, "gb_sold_unknown": 1},
			fields=["name"]):
		out.setdefault(row["name"], {
			"source": "gunbroker", "ref": "", "kind": "gb_sold_unknown",
			"tooltip": _("May already be sold on GunBroker — check before selling"),
			"url": "",
		})
	return out


# ===========================================================================
# Fulfilling a GunBroker order
# ===========================================================================
#
# The counter's Pending Order page grew a third source. A GunBroker card is
# not a web card with a different label: the store buys the shipping label
# itself (WooCommerce's own plugin buys the web one), the money arrives as a
# cheque somebody has to record, and nothing here may be edited or cancelled —
# a GunBroker sale cannot be unwound from the seller's side.
#
# dispose_gb_order is the bound-book gate, and it is fail-closed three times
# over before it looks at anything else. The listing guards already refuse an
# antique, an NFA item and a gun in transfer custody, but guards are what you
# pass on the way in and this is what happens on the way out — a hand-edited
# field, a listing made on the website, or a gun that changed category since it
# was listed all arrive here having passed nothing. What gets written from this
# function is a federal record.

ORDER_DOCTYPE = "GunBroker Order"

MODE_CHECK = "GunBroker Check"
MODE_CARD = "GunBroker Card"

STAGE_AWAITING_PAYMENT = "awaiting_payment"
STAGE_READY_TO_DISPOSE = "ready_to_dispose"
STAGE_AWAITING_SHIP = "awaiting_ship"
STAGE_SHIPPED = "shipped"


# ---------------------------------------------------------------------------
# The Pending Order card
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_pending_gb_orders() -> list[dict]:
	"""Rows for the Pending Order page: GunBroker orders still needing work.

	Shaped to match ffl_core.api.manual_order.list_pending_dispositions so the
	page renders three sources uniformly; these carry source="gunbroker".

	An order stays listed until its tracking number has been written back to
	GunBroker — which is later than a web order leaves, on purpose: the store
	buys this label, so "disposed" is the middle of the job here, not the end.

	Returns:
	    Pending-order cards (source="gunbroker"), shaped like
	    list_pending_dispositions rows.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	names = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"order_status": ("in", ("Pending Fulfillment", "Transferred"))},
		pluck="name", order_by="creation asc")
	ss_enabled = _shipstation_enabled()
	limit_days = overdue_limit_days()
	today = nowdate()
	# One pool for the whole page: the guns suggested to one card must not be
	# suggested to the next, or two orders would be offered the same rifle.
	# Oldest order first (``names`` is creation-ordered), so the order that has
	# waited longest gets the gun that has been on the shelf longest.
	pool = _SuggestionPool()
	out: list[dict] = []
	for name in names:
		order = frappe.get_doc(ORDER_DOCTYPE, name)
		row = _pending_row(order, ss_enabled=ss_enabled, limit_days=limit_days, today=today,
			pool=pool)
		if row:
			out.append(row)
	return out


def _pending_row(order, *, ss_enabled: bool, limit_days: int = 7, today=None,
		pool: "_SuggestionPool | None" = None) -> dict | None:
	stage = order_stage(order)
	if stage == STAGE_SHIPPED and order.tracking_pushed_at:
		return None  # finished: the buyer has a tracking number on GunBroker
	invoice = _invoice_row(order.sales_invoice)
	pending, disposed_serials, serial_errors = _serial_lists(order)
	# Item-level lines have no serial to list, so the card names them by
	# quantity instead. Only while the order is still open: once it is
	# Transferred the stock has gone, and leaving them on the card would read
	# as work still outstanding.
	stock_units = ([] if (order.order_status or "") == "Transferred" else [
		{"item_code": line.item_code, "qty": max(1, cint(line.qty or 1))}
		for line in _stock_lines(order)])
	# Read once and threaded through: this page reloads after every action, and
	# the same record answers the expiry badge and the ship-to preview.
	ffl = _ffl_record(order)
	expiry, expired = _ffl_expiry(ffl)
	# Model-level units: one line per unit, each needing a named gun before Dispose.
	# Gone once the order has shipped (the guns are on the bound book by then).
	assign_units, assign_candidates = ([], {}) if (order.order_status or "") == "Transferred" \
		else _assign_units(order, pool or _SuggestionPool())
	# All antiques: no dealer at the far end, the parcel goes to the buyer, and
	# there is no licence to name, verify or expire. Read once, used below.
	direct = ships_direct(order)
	preview = _ship_to_preview(order, ffl, invoice, direct=direct)
	row = {
		"source": "gunbroker",
		"gunbroker_order": order.name,
		"gb_order_id": order.gb_order_id,
		"gb_status_label": order.gb_status_label or "",
		# GunBroker's own four-step stage (payment → dispose → ship → shipped);
		# the page-wide section comes from ffl_core's one rulebook (stamp_stage,
		# below), fed the flags it reads: awaiting_payment, ffl_expired /
		# serial_errors, ship_pushed.
		"channel_stage": stage,
		"awaiting_payment": stage == STAGE_AWAITING_PAYMENT,
		# The page sorts and groups on the date part, so this is the order date,
		# not the creation of our mirror of it.
		"posting_date": order.order_date or order.creation,
		"customer": order.customer or order.customer_name or order.name,
		"customer_name": order.customer_name or order.buyer_username or "",
		"is_business": False,
		"paid": bool(order.payment_received),
		"paid_total": flt(order.payment_amount),
		"outstanding_amount": flt(invoice.get("outstanding_amount")),
		"grand_total": flt(order.order_total),
		"order_total": flt(order.order_total),
		"currency": order.currency or "USD",
		"shipping_total": flt(order.ship_cost) + flt(order.ship_handling_cost)
			+ flt(order.ship_insurance_cost),
		"discount_total": 0,
		"total_tax": flt(order.sales_tax_total),
		"needs_disposition": bool(pending) or bool(stock_units)
			or any(not u["refunded"] for u in assign_units),
		"has_disposition": bool(disposed_serials),
		"disposed": (order.order_status or "") == "Transferred",
		"disposed_serials": sorted(disposed_serials),
		# GunBroker never leaves us waiting for a serial: a listing IS one gun,
		# and a line that matched nothing reports through serial_errors instead.
		# A model-level unit is not "waiting" either: it sits in the normal
		# section with its own assign row, because the Waiting section is for
		# things nobody here can act on yet and this one is staff's to do now.
		"awaiting_serial": False,
		# Model-level units, one per unit sold: {row, item_code, unit_label,
		# serial, suggested, suggested_label, refunded, error, ...}, and the FIFO
		# free guns per Item for the pick list. The card disables Dispose until
		# every unit has a gun or is marked refunded.
		"assign_units": assign_units,
		"assign_candidates": assign_candidates,
		"serial_errors": serial_errors,
		"pending_serials": pending,
		# Ammunition, optics, parts: "2 x AMMO-9MM-115" rather than a serial.
		"stock_units": stock_units,
		# In-store pickup is refused outright on this channel (there is no
		# counter 4473 flow behind a GunBroker order), so the page must never
		# render the Pickup button for one.
		"store_pickup": False,
		"pickup_invoice": None,
		"ships_direct": direct,
		"destination_ffl": "" if direct else (order.atf_ffl_record or order.ffl_number or ""),
		"destination_ffl_name": "" if direct else (ffl.get("licensee_name")
			or order.atf_ffl_record or order.ffl_number or ""),
		"ffl_expiry_date": None if direct else expiry,
		"ffl_expired": False if direct else expired,
		"is_cr": bool(order.is_cr_transfer),
		"cr_prefill": _cr_prefill(order) if order.is_cr_transfer else None,
		"sales_invoice": order.sales_invoice or None,
		"sales_orders": [],
		"legs": None,
		"note": "",
		# Shown as a plain warning, not as the raw exception: staff need to know
		# the card may be stale, the traceback is on the order for whoever fixes it.
		"sync_error": order.sync_error or "",
		"sync_fail_count": cint(order.sync_fail_count),
		"last_payment_mode": order.payment_mode_used or "",
		"ship_pushed": shipstation_pushed(invoice.get("shipstation_order_id"),
			invoice.get("shipstation_status")),
		# The tracking number is written back by the poll, from the label the
		# store bought. There is no honest manual equivalent, so the button that
		# would claim one is not offered.
		"can_mark_shipped": False,
		"shipstation_enabled": ss_enabled,
		"shipstation_order_id": invoice.get("shipstation_order_id") or "",
		"ship_items": _ship_items(order) if ss_enabled else [],
		"has_ship_address": bool(preview),
		"ship_to_preview": preview,
		# Web-only keys the page reads unconditionally. Present and empty, so a
		# GunBroker row can never be mistaken for a web one by a truthiness test.
		"site": None,
		"woo_order_id": None,
		"woo_status": None,
		"woo_online_order": None,
	}
	return stamp_stage(row, order.order_date or order.creation, limit_days, today)


def order_stage(order) -> str:
	"""Which of the four things this order is waiting for.

	The awaiting_payment → ready_to_dispose boundary is payment_mode_used, the
	same fact dispose_gb_order gates on. Reading GunBroker's own payment flag
	here instead would put a Dispose button on a card the server then refuses.
	"""
	if not (order.payment_mode_used or "").strip():
		return STAGE_AWAITING_PAYMENT
	if (order.order_status or "") != "Transferred":
		return STAGE_READY_TO_DISPOSE
	invoice = _invoice_row(order.sales_invoice)
	if not shipstation_pushed(invoice.get("shipstation_order_id"),
			invoice.get("shipstation_status")):
		return STAGE_AWAITING_SHIP
	return STAGE_SHIPPED


def _invoice_row(sales_invoice: str | None) -> dict:
	if not sales_invoice:
		return {}
	return frappe.db.get_value(
		"Sales Invoice", sales_invoice,
		["name", "outstanding_amount", "shipstation_order_id", "shipstation_status"],
		as_dict=True) or {}


def _serial_lists(order) -> tuple[list[dict], set[str], list[dict]]:
	"""(still to dispose, already disposed, why a line matched nothing)."""
	disposed = set(frappe.get_all(
		"FFL Disposition",
		filters={"gunbroker_order": order.name, "docstatus": 1},
		pluck="serial"))
	# An antique has no disposition to find. It is done once it has left stock
	# — the issue in dispose_gb_order is what moves it out of Active — so that
	# is what its "already handled" test reads.
	for row in (order.serials or []):
		serial = (row.confirmed_serial or "").strip()
		if serial and serial not in disposed and is_antique_item(row.item_code) and (
				frappe.db.get_value("Serial No", serial, "status") or "Active") != "Active":
			disposed.add(serial)
	pending = [
		{"item_code": row.item_code, "serial": row.confirmed_serial}
		for row in (order.serials or [])
		if (row.confirmed_serial or "").strip() and row.confirmed_serial not in disposed
	]
	errors = [
		{"item_code": row.item_code or row.sku or "?", "error": row.reservation_error}
		for row in (order.lines or []) if (row.reservation_error or "").strip()
	]
	return pending, disposed, errors


def _ffl_record(order) -> dict:
	if not order.atf_ffl_record:
		return {}
	return frappe.db.get_value(
		"ATF FFL Record", order.atf_ffl_record,
		["licensee_name", "licensee_dba", "street", "street2", "city", "state",
			"zip", "phone", "expiry_date"],
		as_dict=True) or {}


def _ffl_expiry(record: dict) -> tuple[object, bool]:
	"""(expiry, expired). Unresolved reads as unknown, never as fine."""
	expiry = (record or {}).get("expiry_date")
	if not expiry:
		return None, False
	return expiry, getdate(expiry) < getdate(nowdate())


def _ship_to_preview(order, ffl: dict, invoice: dict, *, direct: bool = False) -> dict | None:
	"""What the label would say — the receiving dealer's premises.

	Never the buyer's address for a regulated gun: a GunBroker firearm sale
	ships to a licensed dealer, and previewing the buyer's address would be
	previewing a parcel that must not be sent. The one exception is ``direct``
	— an order that is all antiques has no dealer, and the buyer's own address
	off the order (the same one the invoice will carry) is the honest preview.
	"""
	if ffl:
		return {
			"company": ffl.get("licensee_dba") or ffl.get("licensee_name"),
			"line1": ffl.get("street"),
			"line2": ffl.get("street2"),
			"city": ffl.get("city"),
			"state": ffl.get("state"),
			"zip": ffl.get("zip"),
			"phone": ffl.get("phone"),
		}
	if direct and (order.ship_to_line1 or "").strip():
		return {
			"company": None,
			"line1": order.ship_to_line1,
			"line2": order.ship_to_line2 or None,
			"city": order.ship_to_city,
			"state": order.ship_to_state,
			"zip": order.ship_to_zip,
			"phone": order.customer_phone,
		}
	if invoice.get("name"):
		addr = invoice_ship_address(frappe.get_doc("Sales Invoice", invoice["name"]))
		if addr:
			return {"company": None, **addr}
	return None


def _ship_items(order) -> list[dict]:
	"""The Ship dialog's payload — every gun on the order, disposed or not."""
	by_item: dict[str, list[str]] = {}
	for row in (order.serials or []):
		serial = (row.confirmed_serial or "").strip()
		if serial and row.item_code:
			by_item.setdefault(row.item_code, []).append(serial)
	out = []
	for item_code, serials in by_item.items():
		out.append({
			"item_code": item_code,
			"item_name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
			"qty": len(serials),
			"serials": serials,
		})
	return out


def _cr_prefill(order) -> dict:
	"""What the C&R dialog opens with.

	The expiry is deliberately NOT carried across orders — staff read it off the
	licence every time, because one valid last month may have lapsed since.
	"""
	return {
		"license_number": order.ffl_number or "",
		"contact_name": order.cr_contact_name or order.customer_name or "",
		"license_expiry": order.cr_license_expiry or None,
		"premise_street": order.cr_premise_street or order.ship_to_line1 or "",
		"premise_street2": order.cr_premise_street2 or order.ship_to_line2 or "",
		"premise_city": order.cr_premise_city or order.ship_to_city or "",
		"premise_state": order.cr_premise_state or order.ship_to_state or "",
		"premise_zip": order.cr_premise_zip or order.ship_to_zip or "",
	}


# ---------------------------------------------------------------------------
# Recording the money
# ---------------------------------------------------------------------------

@frappe.whitelist()
def mark_payment_received(gunbroker_order: str, mode_of_payment: str,
		amount: float, reference_no: str | None = None,
		received_on: str | None = None,
		replace_posted_receipt: int = 0) -> dict:
	"""Record that a GunBroker buyer's payment has been banked.

	This is the fact the Dispose gate opens on, and it is deliberately not
	GunBroker's own "payment received" flag: that flag is a click on their
	website, and a gun must not leave the building on the strength of it.

	No Payment Entry is created here. GunBroker orders are paid before they are
	invoiced, and the invoice does not exist until the guns are disposed — so
	there is nothing to book the receipt against yet. The values recorded here
	are replayed onto a Payment Entry the moment the invoice is raised.

	Args:
	    gunbroker_order: GunBroker Order name.
	    mode_of_payment: "GunBroker Check" or "GunBroker Card". Card is refused
	        while the account has no payment gateway connected.
	    amount: What was actually banked, which is not always what was owed.
	    reference_no: Cheque or money-order number. Required for a cheque —
	        without it the deposit cannot be traced on a bank statement.
	    received_on: The day it went INTO the bank, not the day it arrived in
	        the post. Defaults to today.
	    replace_posted_receipt: Record over an order whose receipt has already
	        been posted. Refused without it, and alerted on with it.

	Returns:
	    {ok, mode_of_payment, amount, received_on, difference}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order)
	_refuse_to_overwrite_a_posted_receipt(order, replace_posted_receipt)
	mode = (mode_of_payment or "").strip()
	if mode not in (MODE_CHECK, MODE_CARD):
		frappe.throw(_("{0} is not a GunBroker payment method. Use {1} or {2}.")
			.format(mode or "—", MODE_CHECK, MODE_CARD))
	if mode == MODE_CARD and not _card_checkout_enabled():
		frappe.throw(_(
			"This phase does not take card payments on GunBroker: no payment "
			"gateway is connected to the account, so there is no card money to "
			"record. If a card payment really did arrive, that is worth "
			"investigating before it is booked."
		), title=_("Card Checkout Is Off"))
	amount = flt(amount)
	if amount <= 0:
		frappe.throw(_("Enter the amount that was banked."))
	reference_no = (reference_no or "").strip()
	if mode == MODE_CHECK and not reference_no:
		frappe.throw(_(
			"Enter the cheque or money-order number. It is the only thing that "
			"ties this order to a line on the bank statement."))
	_require_a_usable_mode_of_payment(mode)

	# Defaulted, never left blank. The cash-basis schedule selects on
	# 'deposited on or before period end', and NULL is not <= anything — an
	# order with no date would be invisible on the schedule the accountant
	# works from while the gun had already shipped and the money was in the
	# bank. That is precisely the entry not to lose.
	received = getdate(received_on) if received_on else getdate(nowdate())
	if received > getdate(nowdate()):
		frappe.throw(_("A payment cannot be deposited in the future."))

	difference = flt(amount - flt(order.order_total), 2)
	order.payment_mode_used = mode
	order.payment_reference_no = reference_no
	order.payment_amount = amount
	order.payment_received_on = str(received)
	order.payment_received = 1
	if not order.payment_received_at:
		order.payment_received_at = now_datetime()
	order.payment_source = gb_orders.SOURCE_CHECK if mode == MODE_CHECK \
		else gb_orders.SOURCE_GATEWAY_CHECKOUT
	order.flags.ignore_permissions = True
	order.save()

	if abs(difference) >= 0.01:
		_warn_amount_difference(order, amount, difference)
	_flag_payment_received(order)
	return {"ok": True, "mode_of_payment": mode, "amount": amount,
		"received_on": str(received), "difference": difference}


def _require_a_usable_mode_of_payment(mode: str) -> None:
	"""Fail here, where nothing has happened yet, not after the gun has gone.

	Recording the payment is what opens the Dispose gate, and the receipt it
	promises is not written until the invoice is raised — which is after the
	dispositions are booked and the parcel is on its way. So a Mode of Payment
	with no account behind it surfaces as a logged error on an order that is
	already shipped, invoiced and showing the full amount outstanding.

	Two ways it gets there. The mode may not exist at all: the seed that builds
	it runs on install and on migrate, and the ordinary go-live order is to
	migrate an empty site and fill the deposit account in afterwards. Or it may
	exist pointing at the account that was configured the day it was made, while
	Settings has since been changed — the money then lands, quietly, in the old
	account.

	Both are one save of GunBroker Settings away from being fixed, which is what
	this says.
	"""
	from ffl_core.api.manual_order import _mode_of_payment_account
	from ffl_integrations.gunbroker.revenue import _default_company

	company = _default_company()
	account = (_mode_of_payment_account(mode, company) or "").strip()
	configured = (frappe.db.get_single_value(
		"GunBroker Settings",
		"check_deposit_account" if mode == MODE_CHECK else "card_clearing_account"
	) or "").strip()
	if not account:
		frappe.throw(_(
			"{0} has no account against it for {1}, so no receipt could be "
			"posted for this payment — and the gun would already have shipped by "
			"the time anybody found out.\n\nOpen GunBroker Settings and save it "
			"once: that points the payment modes at the accounts named there.\n\n"
			"If an order has already reached that state, its invoice was posted "
			"with the full amount outstanding and the reason is in the Error Log "
			"under 'GunBroker order … : revenue invoice failed'. Fix the setting, "
			"then run Create Invoice on the order to lay down the receipt."
		).format(mode, company), title=_("Payment Method Not Set Up"))
	if configured and account != configured:
		frappe.throw(_(
			"{0} still deposits into {1}, but GunBroker Settings now says {2}. "
			"The receipt would land in the old account.\n\nOpen GunBroker "
			"Settings and save it once to re-point the payment mode."
		).format(mode, account, configured), title=_("Deposit Account Changed"))


def _refuse_to_overwrite_a_posted_receipt(order, override) -> None:
	"""A second recording against a posted receipt is a divergence, not an edit.

	The values written here are replayed onto a Payment Entry when the invoice
	is raised, and ``revenue._ensure_paid`` books exactly one: it returns
	immediately when ``payment_entry`` is set, on purpose, so a retry cannot
	post the receipt twice. Recording again therefore changes the amount, the
	date and the cheque number on the ORDER while the entry on the ledger keeps
	the old ones — permanently, silently, and on the two fields the year-end
	journal and the cash-basis schedule read.

	Correcting a posted receipt is an accounting job (cancel and re-book the
	entry), not a form edit. The override exists because refusing outright
	would leave nowhere to go; it alerts, because somebody should know.
	"""
	from ffl_integrations.gunbroker import revenue

	if not revenue._live_entry(order.payment_entry):
		# Same reading as everywhere else. A cancelled entry is not a
		# receipt: a bounced cheque whose entry was cancelled and whose money
		# was then chased down, and recording the replacement is the correct
		# thing to do — refusing it, and then
		# mailing "the ledger was not changed" when the operator overrode the
		# refusal, was wrong twice over.
		return
	if not cint(override):
		frappe.throw(_(
			"{0} already has a posted receipt ({1}) for {2}. Recording a payment "
			"again would change what this order says was banked while the entry "
			"on the ledger keeps saying the old figure, and nothing would ever "
			"reconcile the two.\n\nIf the receipt itself is wrong, cancel and "
			"re-book it — that is an accounting correction. If you are certain "
			"you want the order's record changed anyway, call this again with "
			"replace_posted_receipt."
		).format(order.name, order.payment_entry,
			frappe.format_value(flt(order.payment_amount),
				{"fieldtype": "Currency"})),
			title=_("Receipt Already Posted"))
	notify_admins(
		_("GunBroker order {0}: recorded payment changed after posting").format(
			order.gb_order_id),
		_("{0} ({1}) already had Payment Entry {2} against it, and {3} recorded "
			"the payment again with replace_posted_receipt.\n\nThe entry on the "
			"ledger was NOT changed. The order and the receipt now disagree "
			"until somebody cancels and re-books the entry."
		).format(order.name, order.gb_order_id, order.payment_entry,
			frappe.session.user),
		dedup_key=f"gb_receipt_overwritten_{order.gb_order_id}",
		window_minutes=1440)


@frappe.whitelist()
def correct_payment_details(gunbroker_order: str, received_on: str,
		reference_no: str | None = None) -> dict:
	"""Repair the deposit date (and cheque number) recorded on a GunBroker order.

	For the row that cannot otherwise be fixed. The four payment fields are
	read-only on the form — they are a record of what was banked, not something
	to retype — and the document refuses to save when a mode is recorded with no
	deposit date. A row in that state (a direct database write, an import, a
	half-finished edit) is reported by the daily audit and then cannot be
	corrected through the Desk at all: the field will not accept typing and
	opening the form and saving throws.

	Deliberately narrow: the date and the reference number, nothing else. The
	amount, the mode and whether the order is paid are what was banked — those
	are mark_payment_received's, and once a receipt is posted they are an
	accounting correction rather than an edit.

	Args:
	    gunbroker_order: GunBroker Order name.
	    received_on: The day the money went into the bank. Not in the future.
	    reference_no: Cheque or money-order number, when that is the wrong part.

	Returns:
	    {ok, received_on, reference_no}.

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order)
	received = getdate(received_on) if received_on else None
	if not received:
		frappe.throw(_("Enter the day the money went into the bank."))
	if received > getdate(nowdate()):
		frappe.throw(_("A payment cannot be deposited in the future."))
	before = (str(order.payment_received_on or "—"),
		order.payment_reference_no or "—")
	order.payment_received_on = str(received)
	if reference_no is not None:
		order.payment_reference_no = (reference_no or "").strip()
	order.flags.ignore_permissions = True
	order.save()
	order.add_comment("Comment", _(
		"Payment details corrected by {0}: deposited {1} (was {2}), reference "
		"{3} (was {4})."
	).format(frappe.session.user, order.payment_received_on, before[0],
		order.payment_reference_no or "—", before[1]))
	return {"ok": True, "received_on": str(received),
		"reference_no": order.payment_reference_no or ""}


def _card_checkout_enabled() -> bool:
	from ffl_core.utils import single_flag

	return single_flag("GunBroker Settings", "card_checkout_enabled", default=0)


def _warn_amount_difference(order, amount: float, difference: float) -> None:
	short = difference < 0
	frappe.msgprint(_(
		"Banked {0} against an order total of {1} — {2} by {3}. The invoice will "
		"still be raised for the full amount; the balance shows up on the daily "
		"GunBroker audit until it is settled."
	).format(frappe.format_value(amount, {"fieldtype": "Currency"}),
		frappe.format_value(flt(order.order_total), {"fieldtype": "Currency"}),
		_("short") if short else _("over"),
		frappe.format_value(abs(difference), {"fieldtype": "Currency"})),
		indicator="orange", alert=True)
	notify_admins(
		_("GunBroker order {0} was paid {1}").format(
			order.gb_order_id, _("short") if short else _("over")),
		_("{0} ({1}): banked {2}, order total {3}, difference {4}. Recorded by "
			"{5}."
		).format(order.name, order.gb_order_id, amount, flt(order.order_total),
			difference, frappe.session.user),
		dedup_key=f"gb_payment_diff_{order.gb_order_id}", window_minutes=1440)


def _flag_payment_received(order) -> None:
	"""Tell GunBroker the money arrived. Best effort — the local record stands.

	A failure here must not stop the store disposing the order: the flag is
	courtesy to the buyer, while the record we just wrote is what governs
	whether the gun may ship.
	"""
	from ffl_integrations.gunbroker.client import GunBrokerClient, GunBrokerError

	try:
		GunBrokerClient.from_settings().set_order_flags(
			order.gb_order_id, PaymentReceived=True)
	except GunBrokerError:
		safe_log_error(
			f"GunBroker payment flag failed for {order.gb_order_id}",
			frappe.get_traceback())


# ---------------------------------------------------------------------------
# Naming the gun for a model-level unit
# ---------------------------------------------------------------------------
#
# A model-level listing sells N interchangeable guns under one GunBroker item.
# The order owes a UNIT per piece sold; which gun it is gets decided at the rack,
# by a person, before Dispose. Until then the unit is a claim (see
# ``orders.channel_committed_units``): it reserves no Serial No, and every
# channel's available count already has it taken off.
#
# Assigning turns a claim into a hold: the gun is reserved for this order
# exactly as a per-gun listing's gun is, and from that moment everything
# downstream (disposition, bound book, invoice) sees an ordinary matched gun.

# lazy: 25 guns in the pick list, the scan field reaches any other free gun; a
# search box over the whole shelf is the upgrade if a model ever holds hundreds.
CANDIDATE_LIMIT = 25


def unit_key(row) -> str:
	"""A unit's identity: ``gb_item_id:unit_index``. The child row's own name is
	NOT used because every poll rebuilds the serial table and re-issues them, so
	a name read off the card could be gone by the time a button is pressed."""
	return f"{(row.gb_item_id or '').strip()}:{cint(row.unit_index)}"


def _cr_eligible_serials(item_code: str, serials) -> set[str]:
	"""The subset of ``serials`` old enough to be a curio or relic (the same
	50-year rule the listing side applies; a gun's own year wins over its model's)."""
	serials = list(serials)
	if not serials:
		return set()
	item = {"year_made": frappe.db.get_value("Item", item_code, "year_made")}
	rows = frappe.get_all("Serial No", filters={"name": ("in", serials)},
		fields=["name", "year_made"])
	return {r["name"] for r in rows if gb_mapping.is_cr_eligible(
		gb_mapping.serial_attr(r, item, "year_made"))}


class _SuggestionPool:
	"""The free guns of each Item, oldest first, handed out at most once.

	One instance serves a whole page of cards, so two orders for the same model
	are not offered the same gun, and the order that has waited longest gets the
	gun that has been on the shelf longest. A suggestion is only a suggestion: it
	holds nothing until a person confirms it.
	"""

	def __init__(self):
		self._all: dict[str, list[str]] = {}
		self._left: dict[str, list[str]] = {}
		self._received: dict[str, dict] = {}
		self._cr: dict[str, set[str]] = {}

	def _load(self, item_code: str) -> None:
		if item_code in self._all:
			return
		free = list(uniform_free_serials(item_code))
		self._all[item_code] = free
		self._left[item_code] = list(free)
		self._received[item_code] = {r["name"]: r["creation"] for r in frappe.get_all(
			"Serial No", filters={"name": ("in", free)}, fields=["name", "creation"])} \
			if free else {}

	def _eligible(self, item_code: str, serials: list[str], cr_only: bool) -> list[str]:
		if not cr_only:
			return serials
		if item_code not in self._cr:
			self._cr[item_code] = _cr_eligible_serials(item_code, self._all[item_code])
		return [s for s in serials if s in self._cr[item_code]]

	def take(self, item_code: str, *, cr_only: bool = False) -> str:
		"""The next free gun for a unit, or '' when there is none that fits."""
		self._load(item_code)
		for serial in self._eligible(item_code, self._left[item_code], cr_only):
			self._left[item_code].remove(serial)
			return serial
		return ""

	def label(self, item_code: str, serial: str) -> str:
		self._load(item_code)
		stamp = self._received[item_code].get(serial)
		day = str(stamp)[:10] if stamp else ""
		return _("{0} · received {1}").format(serial, day) if day else serial

	def candidates(self, item_code: str, *, cr_only: bool = False) -> list[dict]:
		"""The pick list: the oldest free guns, whether or not a card has been
		offered them (a person may override a suggestion with any free gun)."""
		self._load(item_code)
		picks = self._eligible(item_code, self._all[item_code], cr_only)[:CANDIDATE_LIMIT]
		return [{"serial": s, "label": self.label(item_code, s)} for s in picks]


def _assign_units(order, pool: _SuggestionPool) -> tuple[list[dict], dict]:
	"""(the card's unit rows, the pick list per Item) for an order's model-level units."""
	rows = [r for r in (order.serials or []) if cint(r.is_model_unit)]
	if not rows:
		return [], {}
	rows.sort(key=lambda r: ((r.gb_item_id or ""), cint(r.unit_index)))
	of_line: dict[str, int] = {}
	for r in rows:
		of_line[r.gb_item_id or ""] = of_line.get(r.gb_item_id or "", 0) + 1
	cr_only = bool(order.is_cr_transfer)
	names: dict[str, str] = {}
	units: list[dict] = []
	for r in rows:
		item_code = r.item_code or ""
		if item_code not in names:
			names[item_code] = frappe.db.get_value("Item", item_code, "item_name") or item_code
		serial = (r.confirmed_serial or "").strip()
		refunded = bool(cint(r.refunded_on_gb))
		suggested = "" if (serial or refunded) else pool.take(item_code, cr_only=cr_only)
		units.append({
			"row": unit_key(r),
			"item_code": item_code,
			"unit_label": _("{0} · unit {1} of {2}").format(
				names[item_code], cint(r.unit_index) + 1, of_line[r.gb_item_id or ""]),
			"serial": serial,
			"suggested": suggested,
			"suggested_label": pool.label(item_code, suggested) if suggested else "",
			"refunded": refunded,
			"error": (r.reservation_error or "").strip(),
			"assigned_by": r.assigned_by or "",
			"assigned_at": r.assigned_at,
		})
	candidates = {code: pool.candidates(code, cr_only=cr_only)
		for code in dict.fromkeys(u["item_code"] for u in units)}
	return units, candidates


def _locked_unit(gunbroker_order: str, row_name: str):
	"""The order (row-locked) and the model-level unit ``row`` names on it.

	Lock order for everything that moves a reservation: the Order, then the Item's
	mutex (``lock_item_reservations``), then the Serial No (inside ``_hold_serial``).
	The poll takes the Order first as well, so an assignment and a re-poll of the
	same order queue behind each other instead of the poll's rebuild of the serial
	table throwing the assignment away. ``for_update`` loads the document with a
	locking read, so what is checked below is what is true now rather than a
	snapshot from before the lock.
	"""
	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order, for_update=True)
	# The children through a locking read too: a poll that rebuilt them while we
	# waited is otherwise invisible here, and `_set_unit` would write to a row name
	# that no longer exists.
	gb_orders.reload_children_for_update(order)
	if (order.order_status or "") != "Pending Fulfillment":
		frappe.throw(_("{0} is not pending fulfillment (status: {1}).").format(
			order.name, order.order_status or "—"))
	if cint(order.gb_status) in gb_orders.UNWOUND_STATUSES:
		frappe.throw(_("{0} is {1} on GunBroker — there is nothing to assign.").format(
			order.name, order.gb_status_label or cint(order.gb_status)))
	key = (row_name or "").strip()
	unit = next((r for r in (order.serials or []) if cint(r.is_model_unit)
		and (unit_key(r) == key or r.name == key)), None)
	if not unit:
		frappe.throw(_("{0} has no model-level unit '{1}' (the order may have been "
			"updated from GunBroker — reload the page).").format(order.name, key))
	lock_item_reservations(unit.item_code)
	return order, unit


def _refuse_if_already_booked(order, serial: str) -> None:
	"""A gun whose disposition is already booked for this order cannot be swapped:
	that is a bound-book entry, and undoing it is not an assignment's job."""
	if serial and frappe.db.exists("FFL Disposition", {
			"serial": serial, "gunbroker_order": order.name, "docstatus": ("<", 2)}):
		frappe.throw(_("{0} is already on the bound book for {1}; it cannot be changed "
			"here.").format(serial, order.name))


def _set_unit(unit, **values) -> None:
	frappe.db.set_value("GunBroker Order Serial", unit.name, values, update_modified=False)
	unit.update(values)


def _mirror_unit_error_on_line(order, unit) -> None:
	"""The line carries the first error of its units, because the card's red
	badge and Dispose's unresolved-lines gate both read the line."""
	errors = [r.reservation_error for r in (order.serials or [])
		if (r.gb_item_id or "") == (unit.gb_item_id or "")
		and cint(r.is_model_unit) and (r.reservation_error or "").strip()]
	for line in (order.lines or []):
		if (line.gb_item_id or "") == (unit.gb_item_id or "") and line.name:
			frappe.db.set_value("GunBroker Order Line", line.name, "reservation_error",
				errors[0] if errors else "", update_modified=False)
			line.reservation_error = errors[0] if errors else ""


def _card_after(order_name: str) -> dict | None:
	"""The refreshed card for an order, as the queue would list it."""
	order = frappe.get_doc(ORDER_DOCTYPE, order_name)
	return _pending_row(order, ss_enabled=_shipstation_enabled(),
		limit_days=overdue_limit_days(), today=nowdate(), pool=_SuggestionPool())


@frappe.whitelist()
def assign_gb_unit_serial(gunbroker_order: str, row_name: str, serial_no: str = "") -> dict:
	"""Name the gun that fills one unit of a model-level GunBroker order.

	Reserves the gun for the order (the claim becomes a hold), tells the other
	storefronts it is gone, and records who assigned it and when. Naming a
	different gun for a unit that already has one swaps them: the old gun is
	released only if this order holds it. An empty ``serial_no`` unassigns the
	unit, which makes it an open claim again. Calling it again with the gun the
	unit already has changes nothing.

	Refused: a gun of another model (a model pooled under this one must be filed
	under it first), a customer's consignment or transfer gun, one in custody,
	one the order already holds for another unit, one that is not free (held by a
	counter order or another channel, out on consignment, possibly already sold on
	GunBroker), and, on a Type 03 collector order, a gun not old enough to be a
	curio or relic. Nothing is changed on a refusal.

	Args:
	    gunbroker_order: GunBroker Order name (must be Pending Fulfillment).
	    row_name: The unit, as the card gives it: ``<gunbroker item id>:<unit number>``.
	    serial_no: The gun's Serial No. Empty unassigns the unit.

	Returns:
	    {ok, serial, row: the refreshed Pending Order card}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	serial = (serial_no or "").strip()
	order, unit = _locked_unit(gunbroker_order, row_name)
	if cint(unit.refunded_on_gb):
		frappe.throw(_("This unit is marked refunded on GunBroker. Undo that first if a "
			"gun should go out for it."))
	current = (unit.confirmed_serial or "").strip()

	if not serial:
		if current:
			_refuse_if_already_booked(order, current)
			gb_orders.release_serial_if_held_by(order.name, current)
			_set_unit(unit, confirmed_serial="", assigned_by=None, assigned_at=None,
				reservation_error="")
			_mirror_unit_error_on_line(order, unit)
			notify_channel_commitments_changed(unit.item_code, source=CHANNEL_GUNBROKER)
		return {"ok": True, "serial": "", "row": _card_after(order.name)}

	if serial == current and not (unit.reservation_error or "").strip():
		held = frappe.db.get_value("Serial No", serial, ["gb_reserved", "gb_order"],
			as_dict=True) or {}
		if held.get("gb_reserved") and (held.get("gb_order") or "") == order.name:
			return {"ok": True, "serial": serial, "row": _card_after(order.name)}  # idempotent

	_refuse_if_already_booked(order, current)
	_check_assignable(order, unit, serial)
	error, newly_held = gb_orders._hold_serial(order, serial)
	if error:
		frappe.throw(error, title=_("Cannot Assign"))
	# The new gun is held; only now let go of the old one, so a refusal above
	# never leaves the unit with nothing.
	if current and current != serial:
		gb_orders.release_serial_if_held_by(order.name, current)
	_set_unit(unit, confirmed_serial=serial, assigned_by=frappe.session.user,
		assigned_at=now_datetime(), reservation_error="")
	_mirror_unit_error_on_line(order, unit)
	if newly_held:
		gb_orders._announce_exit(serial)
	notify_channel_commitments_changed(unit.item_code, source=CHANNEL_GUNBROKER)
	return {"ok": True, "serial": serial, "row": _card_after(order.name)}


def _check_assignable(order, unit, serial: str) -> None:
	"""Everything about ``serial`` that makes it the wrong gun for this unit —
	judged before it is held. Throws; changes nothing."""
	# Column-guarded like the shelf-state read: a custom field belongs to another
	# setup step and a deploy runs the code before migrate adds it.
	sn = frappe.db.get_value("Serial No", serial,
		["item_code", *[f for f in ("is_consignment", "is_transfer_gun")
			if frappe.db.has_column("Serial No", f)]], as_dict=True)
	if not sn:
		frappe.throw(_("No such serial number: {0}.").format(serial), title=_("Cannot Assign"))
	if (sn.item_code or "") != (unit.item_code or ""):
		sister = (frappe.db.get_value("Item", sn.item_code, "sells_through_item") or "").strip()
		frappe.throw(
			(_("{0} is filed under {1}, not {2}. File it under {2} first.")
				if sister == unit.item_code else
				_("{0} is a {1}, but this unit is for {2}."))
			.format(serial, sn.item_code, unit.item_code), title=_("Wrong Model"))
	# A customer's gun is not ours to sell: its cost, its proceeds and its paperwork
	# all differ (the same reason a model-level listing refuses them).
	if cint(sn.is_consignment) or cint(sn.is_transfer_gun):
		frappe.throw(_("{0} is a customer's gun (consignment or transfer), not store "
			"stock, so it cannot fill a GunBroker sale.").format(serial),
			title=_("Not Store Stock"))
	from ffl_core.custody import is_custody_serial

	if is_custody_serial(serial):
		frappe.throw(_("{0} is in customer custody, so it cannot fill a GunBroker "
			"sale.").format(serial), title=_("Not Store Stock"))
	for other in (order.serials or []):
		if other.name != unit.name and (other.confirmed_serial or "").strip() == serial:
			frappe.throw(_("{0} is already assigned to another item on {1}.").format(
				serial, order.name), title=_("Already Assigned"))
	if order.is_cr_transfer and not _cr_eligible_serials(unit.item_code, [serial]):
		frappe.throw(_("{0} goes to a Type 03 (curio and relic) collector, and {1} is not "
			"old enough to be a curio or relic.").format(order.name, serial),
			title=_("Not Curio & Relic"))


@frappe.whitelist()
def mark_gb_unit_refunded(gunbroker_order: str, row_name: str, refunded: int = 1) -> dict:
	"""Mark one unit of a model-level order as refunded on GunBroker.

	For an oversold order: the buyer paid for N, only N-1 guns exist, and the
	buyer is refunded for the missing one on GunBroker's side. A unit marked
	refunded stops counting as a claim, gets no gun, no bound-book entry and no
	invoice line; the seller fee is still accrued on the whole GunBroker line,
	because GunBroker does not reduce it for a refund. The invoice then no longer
	agrees with what GunBroker charged and stays in Draft for a person to check
	the refund against the statement.

	Marking a unit that already has a gun releases the gun (if this order holds
	it). ``refunded=0`` undoes the mark.

	Args:
	    gunbroker_order: GunBroker Order name (must be Pending Fulfillment).
	    row_name: The unit, as the card gives it: ``<gunbroker item id>:<unit number>``.
	    refunded: 1 to mark (default), 0 to undo.

	Returns:
	    {ok, refunded, row: the refreshed Pending Order card}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	mark = bool(cint(refunded))
	order, unit = _locked_unit(gunbroker_order, row_name)
	if bool(cint(unit.refunded_on_gb)) == mark:
		return {"ok": True, "refunded": mark, "row": _card_after(order.name)}  # idempotent
	if mark:
		current = (unit.confirmed_serial or "").strip()
		_refuse_if_already_booked(order, current)
		if current:
			gb_orders.release_serial_if_held_by(order.name, current)
		_set_unit(unit, confirmed_serial="", refunded_on_gb=1,
			assigned_by=frappe.session.user, assigned_at=now_datetime(), reservation_error="")
	else:
		_set_unit(unit, refunded_on_gb=0, assigned_by=None, assigned_at=None)
	_mirror_unit_error_on_line(order, unit)
	notify_channel_commitments_changed(unit.item_code, source=CHANNEL_GUNBROKER)
	return {"ok": True, "refunded": mark, "row": _card_after(order.name)}


# ---------------------------------------------------------------------------
# The bound-book exit
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_dispose_stock_lines(gunbroker_order: str) -> list[dict]:
	"""The loose-stock lines of a GunBroker order and where in the store each can be
	taken from, for the "where did these come from?" step of Dispose.

	Args:
	    gunbroker_order: GunBroker Order name.

	Returns:
	    [{line, item_code, item_name, qty, locations: [{location, label, qty}],
	    unassigned, chosen}] -- one per item-level line (ammunition, optics, parts).
	    ``line`` is the GunBroker item id the dispose call keys its picks by;
	    ``chosen`` is the sole place the item can only have come from (exactly one holds
	    it, none is unassigned).
	"""
	frappe.only_for(_ROLES)
	from ffl_core import storage

	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order)
	return storage.dispose_pick_lines(_order_company(order), [
		{"line": l.gb_item_id or "", "item_code": l.item_code, "qty": max(1, cint(l.qty or 1))}
		for l in _stock_lines(order)])


@frappe.whitelist()
def dispose_gb_order(gunbroker_order: str, cr_details=None, locations=None) -> dict:
	"""Book the bound-book exit for a paid GunBroker order (the Dispose action).

	One submitted "FFL Transfer" disposition per regulated gun, each of which
	issues its own stock-out and queues its own FastBound push. Idempotent on
	{serial, gunbroker_order}. An ANTIQUE on the order gets no disposition —
	it is GCA-exempt, so it is issued straight out of stock instead
	(``_retire_antique_units``). Whether a dealer is involved at all is
	``needs_dealer``: an order that is all antiques and names no licence ships
	to the buyer, and that decision is written to ``ships_direct`` for the
	invoice and the card to read. A serialized unit that is neither a
	regulated firearm nor an antique is refused outright. The revenue invoice
	is raised at the end and a failure there does not roll back the
	dispositions — create_gb_invoice_now retries.

	Args:
	    gunbroker_order: GunBroker Order name (must be Pending Fulfillment).
	    cr_details: Staff verification captured in the dispose dialog for a
	        Type 03 (Curio & Relic) order. Dict from Python, JSON string from
	        the browser. Only consulted for a C&R order, and refused on one that
	        is not.
	    locations: Optional {gb_item_id: Storage Location} -- where in the store
	        staff took each item-level line from (see get_dispose_stock_lines). The
	        units leave that location; a line left out goes to the "to confirm" list
	        on the Storage Locations page.

	Returns:
	    {ok, dispositions: [names], disposed_count, retired: [antique serials
	    issued out with no disposition], issued: [item-level quantities taken
	    out of stock], sales_invoice, invoice_status}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order)

	_refuse_what_must_never_be_booked(order)
	_require_ready_to_dispose(order)

	regulated, antiques, others = _split_units(order)
	# Where the loose stock came from -- checked now, before a disposition is booked, so a bad
	# pick is refused with nothing to undo (the Woo side does the same).
	picks = _checked_picks(order, _stock_lines(order), locations)
	if others:
		frappe.throw(_(
			"{0} carries serialized unit(s) that are neither a regulated firearm "
			"nor an antique: {1}. Dispose books a bound-book transfer or an "
			"antique stock issue and nothing else — deal with these by hand "
			"before disposing the order."
		).format(order.name, ", ".join(
			f"{(row.item_code or '?')} / {row.confirmed_serial}" for row in others)),
			title=_("Not a Firearm"))
	dealer = needs_dealer(order)
	created: list[str] = []
	disposed: list[str] = []
	destination = ""
	if dealer:
		destination = _resolve_and_validate_ffl(order, cr_details)
		if regulated:
			created, disposed = _book_dispositions(order, destination, rows=regulated)
	else:
		# Nothing here needs a dealer. Collector details keyed into the dialog
		# would be meaningless, so they are refused rather than silently
		# dropped — the same rule the dealer path applies.
		reject_cr_details_on_non_cr_order(cr_details, order, license_field="ffl_number")
	retired = _retire_antique_units(order, antiques)
	# Item-level lines: a quantity leaves stock with no serial, no disposition
	# and nothing pushed to FastBound. Deliberately after the antiques and
	# before the status flip, so a shortfall raises while the order is still
	# Pending Fulfillment and the whole step can be retried.
	issued = _issue_stock_units(order, _stock_lines(order), picks)

	for serial in disposed + retired:
		frappe.db.set_value("Serial No", serial,
			{"gb_reserved": 0, "gb_order": ""}, update_modified=False)
	# ships_direct is the decision just taken, written down for everything
	# after this point (the invoice's ship-to, its remarks, the card) to read
	# instead of re-deriving it from the Items.
	frappe.db.set_value(ORDER_DOCTYPE, order.name,
		{"order_status": "Transferred", "ships_direct": int(not dealer)},
		update_modified=False)
	order.order_status = "Transferred"
	order.ships_direct = int(not dealer)
	if destination:
		order.atf_ffl_record = destination

	from ffl_integrations.gunbroker import revenue

	try:
		result = revenue.create_sales_invoice_for_gb_order(order)
	except Exception:  # noqa: BLE001 — a booked bound-book exit is never rolled back
		safe_log_error(f"GunBroker order {order.name}: revenue invoice failed",
			frappe.get_traceback())
		result = {"sales_invoice": None, "status": "error"}
	return {"ok": True, "dispositions": created, "disposed_count": len(created),
		"retired": retired, "issued": issued,
		"sales_invoice": result.get("sales_invoice"),
		"invoice_status": result.get("status")}


def _refuse_what_must_never_be_booked(order) -> None:
	"""The one thing that must not reach the bound book, checked first.

	A buyer who chose THIS store as the receiving dealer is a counter pickup,
	not a shipment. Antiques and NFA items used to be refused here as well: an
	antique now leaves through a stock issue with no disposition
	(``_retire_antique_units``), and an NFA item transfers like any other gun
	— its Form 3/4 is filed in ATF eForms, outside this system.
	"""
	if is_own_license(order.ffl_number or ""):
		frappe.throw(_(
			"{0} names this store as the receiving dealer. Nothing ships and "
			"there is no far-end dealer to transfer to — booking it here would "
			"put a disposition to ourselves in the bound book while the sale that "
			"really happens (a 4473 at the counter) went unrecorded. Run the 4473 "
			"when the buyer collects."
		).format(order.name), title=_("In-Store Pickup"))


def _require_ready_to_dispose(order) -> None:
	if (order.order_status or "") != "Pending Fulfillment":
		frappe.throw(_("{0} is not pending fulfillment (status: {1}).").format(
			order.name, order.order_status or "—"))
	if cint(order.gb_status) == gb_orders.LAYAWAY_STATUS:
		frappe.throw(_(
			"{0} is on layaway. Instalments are a customer deposit, not a sale — "
			"there is no revenue to book and the gun stays here until GunBroker "
			"reports it paid in full."
		).format(order.name), title=_("On Layaway"))
	if not (order.payment_mode_used or "").strip():
		frappe.throw(_(
			"No payment has been recorded for {0} in POS. GunBroker's own "
			"'payment received' flag is a click on their website; it is not "
			"evidence the money reached us. Use Mark Payment Received first — "
			"that is also what puts the receipt on the ledger when the invoice "
			"is raised."
		).format(order.name), title=_("Payment Not Recorded"))
	# GunBroker's flag, minus the antiques: a pre-1899 gun needs no licence
	# whatever the listing said, and waiting for one would wait for ever. The
	# Item is read through the serial row, so a hand-made listing whose line
	# names no Item is still recognised.
	needs_ffl = any(
		cint(line.is_ffl_required) and not is_antique_item(_unit_item_code(line, order))
		for line in (order.lines or []))
	if needs_ffl and not cint(order.ffl_received):
		frappe.throw(_(
			"{0} is still waiting for the receiving dealer's licence. GunBroker "
			"marks the order 'FFL Received' when their Master FFL service has "
			"collected it."
		).format(order.name), title=_("FFL Not Received"))
	_require_model_units_named(order)
	if not any((r.confirmed_serial or "").strip() for r in (order.serials or [])) \
			and not _stock_lines(order):
		frappe.throw(_(
			"{0} has no gun matched to it. Every line failed to match a Serial No "
			"— fix the matches before disposing, or there is nothing to book."
		).format(order.name), title=_("Nothing to Dispose"))
	unresolved = [row.reservation_error for row in (order.lines or [])
		if (row.reservation_error or "").strip()]
	if unresolved:
		frappe.throw(_(
			"{0} still has unresolved line(s): {1}\n\nDisposing now would ship "
			"part of the order and mark the whole thing transferred, leaving a "
			"gap in the bound book."
		).format(order.name, "; ".join(unresolved)), title=_("Unresolved Lines"))


def _require_model_units_named(order) -> None:
	"""Every model-level unit has a gun that is still this order's, or is refunded.

	The rest of Dispose reads the serial table and books what has a serial, so a
	unit with no gun would simply be left out: the order would ship one gun short
	and be marked transferred, with the buyer charged for two. And a gun that has
	since been taken by another order (a hold released and re-taken, a hand edit)
	would be booked out of the bound book under the wrong sale.
	"""
	units = [r for r in (order.serials or []) if cint(r.is_model_unit)]
	open_units = [r for r in units
		if not (r.confirmed_serial or "").strip() and not cint(r.refunded_on_gb)]
	if open_units:
		frappe.throw(_(
			"{0} has {1} unit(s) with no gun assigned yet ({2}). Assign a gun to each "
			"on the Pending Order card, or mark the unit refunded on GunBroker if it "
			"cannot be filled."
		).format(order.name, len(open_units),
			", ".join(sorted({r.item_code or "?" for r in open_units}))),
			title=_("Units Not Assigned"))
	for row in units:
		serial = (row.confirmed_serial or "").strip()
		if not serial:
			continue
		held = frappe.db.get_value("Serial No", serial, ["gb_reserved", "gb_order"],
			as_dict=True) or {}
		if not (held.get("gb_reserved") and (held.get("gb_order") or "") == order.name):
			frappe.throw(_(
				"{0} is assigned to {1}, but is no longer held for it (held by: {2}). "
				"Assign a gun again."
			).format(serial, order.name, held.get("gb_order") or _("nothing")),
				title=_("Gun Not Held"))
		if (row.reservation_error or "").strip():
			frappe.throw(_("{0}: {1}").format(serial, row.reservation_error),
				title=_("Unresolved Lines"))


def _resolve_and_validate_ffl(order, cr_details) -> str:
	"""The receiving dealer, resolved and gated. Throws if it cannot be.

	A Type 03 collector is not in the ATF directory, so nothing upstream can
	supply their name, address or expiry — a human reads them off the licence in
	the dispose dialog. That branch runs BEFORE resolution, because it is what
	makes resolution possible at all.
	"""
	from ffl_core.compliance.dealer_transfer import _resolve_destination_ffl

	if order.is_cr_transfer:
		details = _validate_cr_details(cr_details, order)  # throws; nothing written
		# license_field, not the default: GunBroker Order has no `ffl_license`
		# column, and Document.__setattr__ accepts any attribute while save()
		# keeps only real fields — so a corrected licence number would be
		# silently dropped while atf_ffl_record was really cleared.
		_persist_cr_details(order, details, license_field="ffl_number")
		order.atf_ffl_record = ensure_cr_ffl_record(
			ffl_number=details["license_number"],
			licensee_name=details["contact_name"],
			street=details["premise_street"],
			street2=details["premise_street2"],
			city=details["premise_city"],
			state=details["premise_state"],
			zip_code=details["premise_zip"],
			expiry_date=details["license_expiry"],
			phone=order.customer_phone or "",
		)
		order.flags.ignore_permissions = True
		order.save()
		order.add_comment("Comment", _(
			"C&R verification recorded by {0}: {1} / {2} / {3} {4}."
		).format(frappe.session.user, details["license_number"],
			details["contact_name"], details["premise_city"],
			details["premise_state"]))
		# Commit boundary: the verification is a finished unit of work. Without
		# this, a failure in the disposition below would roll it back and the
		# operator would have to read and re-key the licence.
		safe_commit()
	else:
		reject_cr_details_on_non_cr_order(cr_details, order,
			license_field="ffl_number")

	dest = (order.atf_ffl_record or "").strip()
	if not dest:
		licence = (order.ffl_number or "").strip()
		if not licence:
			frappe.throw(_(
				"{0} has no receiving FFL. GunBroker's Master FFL service supplies "
				"it once the dealer sends their licence in."
			).format(order.name), title=_("No Destination FFL"))
		dest = _resolve_destination_ffl(licence)  # throws if unresolved
	_require_valid_ffl(dest)  # expiry + complete address; throws if invalid
	if (order.atf_ffl_record or "") != dest:
		frappe.db.set_value(ORDER_DOCTYPE, order.name, "atf_ffl_record", dest,
			update_modified=False)
	return dest


def _unit_item_code(line, order) -> str:
	"""The Item a line resolves to: its serial row's first, its own second.

	A listing made by hand on the GunBroker website carries a SKU naming no
	Item we hold, so ``apply_order_lines`` leaves ``line.item_code`` empty — yet
	the gun still matched on its serial number and ``reserve_order_serials`` put
	the serial's own Item on the serial row. That row is the truth; the line is
	only the fallback for a line that matched nothing at all.
	"""
	for row in (order.serials or []):
		if (row.gb_item_id or "") == (line.gb_item_id or "") and (row.item_code or "").strip():
			return (row.item_code or "").strip()
	return (line.item_code or "").strip()


def _split_units(order) -> tuple[list, list, list]:
	"""The order's matched guns as (regulated, antique, other) serial rows.

	The two canonical predicates, in that order: ``is_regulated_firearm_item``
	is what the bound-book and 4473 layer gate on; ``is_antique_item`` is the
	GCA-exempt case. Anything else carrying a serial (an optic sold with its
	serial in the SKU) is neither a disposition nor an antique issue, and comes
	back separately so the caller refuses it instead of guessing.
	"""
	regulated: list = []
	antiques: list = []
	others: list = []
	for row in (order.serials or []):
		if not (row.confirmed_serial or "").strip():
			continue
		code = (row.item_code or "").strip()
		if is_regulated_firearm_item(code):
			regulated.append(row)
		elif is_antique_item(code):
			antiques.append(row)
		else:
			others.append(row)
	return regulated, antiques, others


def _stock_lines(order) -> list:
	"""The order's item-level lines: a quantity of stock, no numbered unit.

	These come off an item-level listing (a bare item-code SKU) — ammunition,
	optics, parts. They carry no serial row at all, which is why every gate
	that asks "is every unit accounted for?" has to consult this as well as
	``order.serials``: an all-ammunition order has an empty serials table and
	is nonetheless complete.
	"""
	return [line for line in (order.lines or []) if gb_orders.is_item_level_line(line)]


class _CheckedPicks(dict):
	"""{gb_item_id: validated location} -- picks that have passed _checked_picks."""


def _checked_picks(order, lines, locations) -> "_CheckedPicks":
	"""Parse and validate a Dispose call's picks against this order's stock lines and this
	store's locations (shared parse_picks + validate_open_location). A pick that is wrong is
	refused; one for a location disabled since the dialog was drawn is dropped (the units come
	out of the unassigned stock). Runs BEFORE anything is booked."""
	from ffl_core import storage

	picks = storage.parse_picks(locations)
	known = {str(l.gb_item_id or ""): l for l in lines}
	company = _order_company(order) if picks else None
	out = _CheckedPicks()
	for key, loc in picks.items():
		line = known.get(key)
		if not line:
			frappe.throw(_("Line {0} is not a stock line of {1}, so it has no location to pick.")
				.format(key, order.name))
		checked = storage.validate_open_location(line.item_code, loc, company)
		if checked:
			out[key] = checked
	return out


def _issue_stock_units(order, lines, locations=None) -> list[dict]:
	"""Take each item-level line's quantity out of stock. No bound book, no FFL.

	The same shape as the antique retirement above and for the same reason: the
	Sales Invoice is raised with ``update_stock=0`` (the guns on it left under
	their dispositions), so something has to move the stock that did NOT leave
	that way, and a Material Issue is what this channel already uses for a unit
	with no disposition behind it.

	Not idempotent by itself — a quantity carries no state of its own to
	re-read, unlike a serial whose status says whether it has gone. What makes
	a retry safe is the caller: ``dispose_gb_order`` flips the order to
	Transferred in the same request, and the gate refuses a second run. The
	stock entry names the order so a duplicate is at least traceable if one is
	ever forced by hand.

	A shortfall raises, deliberately: issuing stock the store does not have
	would book a negative on-hand and hide a picking error that somebody has to
	resolve before the parcel goes out.
	"""
	if not lines:
		return []
	from ffl_core import storage
	from ffl_core.firearm import issue_stock_qty
	from ffl_core.warehouses import default_stock_warehouse

	# ``locations`` arrives already checked (dispose_gb_order does it up front); a direct call
	# gets the same check here. Either way a bad pick is refused before any stock moves.
	picks = locations if isinstance(locations, _CheckedPicks) else _checked_picks(order, lines, locations)

	company = _order_company(order)
	warehouse = default_stock_warehouse(company)
	if not warehouse:
		frappe.throw(_(
			"No default stock warehouse is configured for {0}, so the stock on "
			"{1} cannot be issued."
		).format(company, order.name), title=_("Warehouse Missing"))

	# This is a SALE, so the cost is cost of goods sold — the same account the
	# counter and the web store debit for the same box of ammunition. Left to
	# the platform default a Material Issue lands in stock_adjustment_account,
	# which is where shrinkage and stocktake differences live: the totals would
	# still be right (both sit under Direct Expenses) but one product would be
	# filed two ways depending on which channel sold it, and month-end would
	# need a permanent hand-written carve-out to unpick it.
	expense_account = frappe.get_cached_value("Company", company, "default_expense_account")

	issued: list[dict] = []
	for line in lines:
		qty = max(1, cint(line.qty or 1))
		entry = issue_stock_qty(
			line.item_code, qty, warehouse,
			expense_account=expense_account,
			remarks=_("GunBroker Order #{0} Item #{1}. Sold by the unit — no "
				"serial number, no bound-book entry."
				).format(order.gb_order_id or order.name, line.gb_item_id or "?"))
		issued.append({"item_code": line.item_code, "qty": qty, "stock_entry": entry})
		# The units leave the location staff took them from. Tracking only: it never
		# fails the shipment (a stale pick takes what the location has; the rest shows
		# on the "to confirm" list).
		pick = picks.get(str(line.gb_item_id or ""))
		if pick:
			storage.never_block(lambda line=line, qty=qty, pick=pick, entry=entry: storage.move(
				reason="ship", item_code=line.item_code, qty=qty, from_location=pick,
				allow_short=True, voucher_type="Stock Entry", voucher_no=entry))
	return issued


def _order_company(order) -> str:
	"""The company the stock leaves. Same source the revenue invoice uses, so a
	two-company site cannot issue from one and invoice the other."""
	from ffl_integrations.gunbroker import revenue

	return revenue._default_company()


def needs_dealer(order) -> bool:
	"""Whether a receiving FFL is part of shipping this order.

	Yes when any unit is a regulated firearm, when any line cannot be classified
	at all (fail closed), or when the order names a dealer licence — a buyer who
	supplied one gets the parcel there, antiques included, rather than having
	that licence silently ignored.
	"""
	if (order.ffl_number or "").strip() or (order.atf_ffl_record or "").strip():
		return True
	regulated, _antiques, _others = _split_units(order)
	if regulated:
		return True
	for line in (order.lines or []):
		code = _unit_item_code(line, order)
		if not code or is_regulated_firearm_item(code):
			return True
	return False


def ships_direct(order) -> bool:
	"""True when this order goes to the buyer's own address, not to a dealer.

	Once the order is Transferred this is the decision Dispose took and wrote
	down (``ships_direct`` on the order): the invoice was built from it, and
	the card must not say something else. Before that it is computed live —
	every matched unit an antique, nothing unclassifiable, no dealer named.
	"""
	if (order.order_status or "") == "Transferred":
		return bool(cint(order.ships_direct))
	matched = any((row.confirmed_serial or "").strip() for row in (order.serials or []))
	# An item-level line is "matched" the moment it exists — it names a quantity
	# of an Item, and there is nothing further to resolve. Without this an
	# all-ammunition order would read as undecided and the card would offer a
	# dealer that no part of the order needs.
	if not matched and not _stock_lines(order):
		return False
	return not needs_dealer(order)


def _retire_antique_units(order, rows) -> list[str]:
	"""Take each antique on the order out of stock — no disposition, no FastBound.

	The same shape as the web store's antique retirement: the Material Issue is
	what moves the serial out of Active, and everything downstream (the listing
	end, the web store's SOLD routing) keys off that stock movement exactly as
	it does after a disposition. Idempotent through issue_antique_serial.

	Three outcomes per gun, and only the third is a problem: issued now; already
	out (a retry after a failed invoice, or staff issued it by hand); or still
	Active with no warehouse to issue from. The last is a gun this order holds
	that stock cannot account for. It is reported rather than swallowed — a
	disposition would at least have left a submitted document behind, and this
	path leaves nothing.
	"""
	retired: list[str] = []
	for row in rows:
		serial = (row.confirmed_serial or "").strip()
		issued = issue_antique_serial(serial, remarks=_(
			"GunBroker Order #{0} Item #{1}. Antique (GCA-exempt): no FFL "
			"Disposition and no bound-book entry."
		).format(order.gb_order_id, row.gb_item_id or "?"))
		if not issued and frappe.db.get_value("Serial No", serial, "status") == "Active":
			safe_log_error(
				f"GunBroker order {order.name}: antique {serial} could not be issued",
				f"Serial No {serial} is still Active but has no warehouse to issue "
				"from; no Stock Entry was created.")
			notify_admins(_("GunBroker antique not issued"), _(
				"Order {0}: antique {1} is still Active but has no warehouse, so no "
				"stock issue was booked. Find the gun before shipping anything."
			).format(order.name, serial), dedup_key=f"gb_antique_not_issued_{serial}")
		retired.append(serial)
	return retired


def _book_dispositions(order, destination: str, *,
		rows=None) -> tuple[list[str], list[str]]:
	created: list[str] = []
	disposed: list[str] = []
	for row in (order.serials if rows is None else rows) or []:
		serial = (row.confirmed_serial or "").strip()
		if not serial:
			continue
		if frappe.db.exists("FFL Disposition", {
			"serial": serial, "gunbroker_order": order.name, "docstatus": ("<", 2),
		}):
			continue  # idempotent — already booked
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "FFL Transfer",
			"company": _serial_company(serial),
			"destination_ffl": destination,
			"item": row.item_code,
			"serial": serial,
			"gunbroker_order": order.name,
			"notes": _disposition_note(order, row),
		})
		disp.flags.ignore_permissions = True
		disp.insert()
		disp.submit()
		created.append(disp.name)
		disposed.append(serial)
	return created, disposed


def _serial_company(serial: str) -> str | None:
	warehouse = frappe.db.get_value("Serial No", serial, "warehouse")
	return frappe.db.get_value("Warehouse", warehouse, "company") if warehouse else None


def _disposition_note(order, row) -> str:
	"""The bound-book note. Carries the order and item numbers because the
	month-end reconciliation against GunBroker's statement matches on them."""
	base = _("GunBroker Order #{0} Item #{1}.").format(
		order.gb_order_id, row.gb_item_id or "?")
	if not order.is_cr_transfer:
		return base
	verified_at = order.cr_verified_at
	stamp = (verified_at.strftime("%Y-%m-%d")
		if hasattr(verified_at, "strftime") else str(verified_at or "")[:10])
	return base + " " + _(
		"C&R (Type 03) transfer — shipped to the collector's own licensed "
		"premises. Verified by {0} on {1}."
	).format(order.cr_verified_by or "—", stamp or "—")


# ---------------------------------------------------------------------------
# Shipping
# ---------------------------------------------------------------------------

@frappe.whitelist()
def push_tracking_now(gunbroker_order: str) -> dict:
	"""Write this order's tracking number back to GunBroker now.

	The scheduled poll does this on its own every ten minutes; this is for the
	operator who has just bought the label and wants the buyer to see it.

	Args:
	    gunbroker_order: GunBroker Order name (disposed, with a ShipStation
	        shipment against its invoice).

	Returns:
	    {ok, tracking_number, carrier, steps} — or {ok: False, reason} when
	    there is no live label yet.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	from ffl_integrations.gunbroker import shipping

	return shipping.push_tracking(gunbroker_order)
