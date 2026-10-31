"""In-store pickup: hand a web order to the counter, already paid.

A web order whose receiving FFL is our own licence is a pickup, not a shipment
(:func:`ffl_core.firearm.is_own_license`). ``dispose_web_order`` refuses those —
booking an FFL transfer to ourselves would put a disposition that never happened
into the bound book. This module is what staff do instead.

For a firearm, the counter path is not one option among several; it is the only
one the rest of the system is built for. A retail 4473 hangs off a **POS
Invoice**: it is where ``start_4473_for_pos_invoice`` begins, what
``_ensure_disposition_coverage`` looks for when FastBound's webhook returns, and
its absence is what makes ``_sync_fb_direct_disposal_stock`` alarm "4473 DISPOSAL
WITH NO POS INVOICE" and *hold* the gun in stock rather than issue it. A web order
on 2026-08-15 ran the 4473 in FastBound with no invoice behind it and landed
in exactly that state: bound book correct, POS holding a gun it had already sold.

So Pickup opens a DRAFT POS Invoice — already paid, because the buyer paid on
WooCommerce and the register must not ask again — and the ordinary counter flow
takes it from there: 4473, NICS, Complete Order.

An order with no regulated firearm gets no invoice: there is no 4473 to run, so a
POS Invoice would be ceremony around a stock-out.

Building a POS Invoice server-side is not the same as building one in the POS
UI, and the differences are load-bearing — see :func:`_profile_defaults` and
:func:`_open_counter_invoice`. They were all found by review after the first cut
shipped; each one is called out where it bites.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.api.manual_order import _ROLES
from ffl_core.firearm import is_antique_item, is_own_license, is_regulated_firearm_item
# The shift / profile-default rules are shared with the counter order's own
# pickup ticket (ffl_core.api.counter_pickup); the private names are kept so
# this module reads — and is patched in tests — as before.
from ffl_core.pos_draft import (
	PROFILE_DEFAULTS as _PROFILE_DEFAULTS,
	open_drafts_by,
	open_shift as _open_shift,
	profile_defaults as _profile_defaults,
)
from ffl_core.utils import set_missing_values_keeping_taxes
from ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order import (
	mark_transferred,
	unmark_transferred,
)
from ffl_woo_sync.woocommerce.revenue import (
	WOO_MODE_OF_PAYMENT,
	_TOTAL_TOLERANCE,
	_default_company,
	_ensure_mode_of_payment,
	_resolve_web_customer,
	_sales_tax_account,
	build_invoice_items,
)

_PENDING = "Pending Fulfillment"
_TRANSFERRED = "Transferred"

def _open_draft_for_order(woo_online_order: str) -> str | None:
	"""An existing, still-open counter invoice for this web order, if any.

	The idempotency key. Two staff on two screens, or one impatient double-click,
	must not produce two drafts: a second draft is a second 4473 target for one
	gun, and `_ensure_disposition_coverage` would then have to pick.
	"""
	return open_drafts_by("woo_online_order", [woo_online_order]).get(woo_online_order)


def open_drafts_for_orders(names: list[str]) -> dict[str, str]:
	"""``{woo order: draft POS Invoice}`` for many orders in ONE query.

	The Pending Order page renders every pending order on load and on every
	refresh; asking per row turned that into an N+1. Batched here (the counter
	order's ticket shares the query: ffl_core.pos_draft.open_drafts_by).
	"""
	return open_drafts_by("woo_online_order", names)


def _refuse_unless_ready(order) -> None:
	"""Every reason a pickup must not be rung yet, in one place.

	Each mirrors a refusal `dispose_web_order` already makes for the shipping
	path; a pickup is a different exit, not a laxer one.
	"""
	from ffl_woo_sync.woocommerce.fulfillment import (
		_is_paid_woo_status,
		_to_store_receive_map,
		to_store_ready,
	)
	from ffl_woo_sync.woocommerce.webhook import _UNSETTLED_DROPSHIP_STATUSES

	if not _is_paid_woo_status(order.woo_status):
		# The payment row below posts the full order total against the Woo
		# clearing account. On an unpaid order that books money never collected.
		frappe.throw(_(
			"{0} has not been paid on WooCommerce (status: {1})."
		).format(order.name, order.woo_status or "—"))
	if (order.get("dropship_status") or "").strip() in _UNSETTLED_DROPSHIP_STATUSES:
		frappe.throw(_(
			"{0} still has an outstanding distributor order. Ringing it now would "
			"book the sale before anything shipped."
		).format(order.name), title=_("Distributor Leg Outstanding"))
	if order.refunds:
		# The invoice below mirrors the ORIGINAL total. revenue.py nets a refunded
		# order down via apply_refund_discount; rather than half-copy that, refuse
		# and let the operator sort the refund out first.
		frappe.throw(_(
			"{0} carries a WooCommerce refund. Ringing it at the counter would "
			"collect the gross. Settle the refund first."
		).format(order.name), title=_("Refunded Order"))
	legs = _to_store_receive_map([order.name]).get(order.name)
	if legs and not to_store_ready(legs):
		frappe.throw(_(
			"{0} is waiting on a gun being procured to the store — it has not "
			"arrived yet. Receive it first."
		).format(order.name), title=_("Not Arrived"))


def _open_counter_invoice(order) -> str:
	"""Build the DRAFT POS Invoice for a firearm pickup and return its name."""
	company = _default_company()
	shift = _open_shift(company)
	payload = {
		"doctype": "POS Invoice",
		"company": company,
		"pos_profile": shift.pos_profile,
		"customer": _resolve_web_customer(order),
		# NOT set_posting_time: the draft opens at the Pickup click and is
		# submitted when the 4473 clears, possibly days later. Left to ERPNext,
		# the posting date refreshes to the day of the submit, so the stock
		# issue, revenue and sales tax book on the day the gun actually left —
		# the same day as its FastBound 4473 disposition.
		"po_no": order.woo_order_id,
		"woo_online_order": order.name,
		# Shared with the revenue path rather than re-derived: that builder also
		# carries the supplier-lane branch (`delivered_by_supplier` + blank
		# warehouse + distributor pin) which a bespoke copy here silently dropped,
		# making a drop-ship line try to issue stock we never received and lose
		# its `is_distributor_dropship_row` exemption.
		"items": build_invoice_items(order),
		# Pinned, NOT inherited, and re-asserted after set_missing_values below
		# together with the `taxes` rows: an empty template stops a later
		# POS-UI touch (`set_pos_data` -> for_validate=False) from pulling the
		# profile's tax template in and recomputing over the carried
		# WooCommerce figure. Pinning it here is only half the job —
		# `set_pos_fields` copies the profile's value over it on the way in,
		# so the pin was overwritten on every pickup until the call below
		# started defending it. Whether a given register carries a template is
		# not something this code can assume: `setup/store_setup` configures
		# one, but nothing runs that module automatically, and the production site has
		# two profiles of which only one has the field set. The defence below
		# therefore does not depend on the answer.
		"taxes_and_charges": "",
		"remarks": _("In-store pickup for web order {0} (Woo #{1}). Paid on "
			"WooCommerce — do not collect payment.").format(
			order.name, order.woo_order_id),
	}
	payload.update(_profile_defaults(shift.pos_profile))
	payload["taxes"] = _invoice_taxes(order, company)

	doc = frappe.get_doc(payload)
	doc.flags.ignore_permissions = True
	set_missing_values_keeping_taxes(doc)
	doc.calculate_taxes_and_totals()
	_attach_payment(doc, order)
	doc.insert()
	return doc.name


def _invoice_taxes(order, company: str) -> list[dict]:
	"""WooCommerce's own tax, plus shipping as a real charge.

	Both are "Actual" rows carrying WooCommerce's figures verbatim: the buyer was
	charged those, and a recomputation landing a cent away books something they
	did not pay.

	Shipping has to be a CHARGE, not just an addend on the payment. An earlier
	cut paid `order_total` (which includes shipping) against an invoice totalling
	only lines + tax; ERPNext takes the excess as `change_amount` and posts it out
	of the drawer via the profile's cash account — so the till was short the
	shipping and the shipping revenue was never booked at all. The Sales Invoice
	path avoids this with `ffl_shipping_amount`, a custom field that exists on
	Sales Order / Sales Invoice and NOT on POS Invoice. (That field is the
	customer-paid shipping; income is always the taxes table's Shipping row.)
	"""
	from ffl_core.setup.shipping_charge import ensure_shipping_account

	rows: list[dict] = []
	shipping = flt(order.shipping_total)
	if shipping:
		rows.append({
			"charge_type": "Actual",
			"account_head": ensure_shipping_account(company),
			"description": _("Shipping collected by WooCommerce (Woo #{0})").format(
				order.woo_order_id),
			"tax_amount": shipping,
		})
	tax = flt(order.total_tax)
	if tax:
		rows.append({
			"charge_type": "Actual",
			"account_head": _sales_tax_account(company),
			"description": _("Sales tax collected by WooCommerce (Woo #{0})").format(
				order.woo_order_id),
			"tax_amount": tax,
		})
	return rows


def _attach_payment(doc, order) -> None:
	"""Mark the invoice paid for exactly what it totals, having first proved that
	total matches what WooCommerce charged.

	The amount comes from the invoice's OWN grand total, never from
	`order_total`: any gap between them is money that would post as change out of
	the drawer. The mirror check is what makes that safe — line rates are
	`line_total / qty`, which rounds on a multi-qty line, and an unmirrored cent
	either silently leaves the till short or trips
	`validate_full_payment`'s "Partial Payment in POS Transactions are not
	allowed" AT SUBMIT — with the buyer at the counter and the 4473 already run.
	Same tolerance the revenue path uses for the same comparison.
	"""
	total = flt(doc.grand_total)
	expected = flt(order.order_total)
	if abs(total - expected) > _TOTAL_TOLERANCE:
		frappe.throw(_(
			"Counter total {0} does not match what WooCommerce charged ({1}) for "
			"{2}. Ringing it would take the difference out of the till. Check the "
			"order's lines, shipping and tax."
		).format(frappe.bold(total), frappe.bold(expected), order.name),
			title=_("Total Mismatch"))
	doc.set("payments", [{
		"mode_of_payment": _ensure_mode_of_payment(doc.company),
		"amount": total,
	}])


def _unresolved_local_unit(order):
	"""``(kind, item_code)`` for the first serial row that owes a serial, else None.

	Mirrors ``_auto_clear_non_firearm_if_completed``'s pair of guards, in its
	order: a NON-antique serial row has its own exit and must not leave through
	here at all; an antique one may, but only once it names a gun, because
	``antique._issue_antique_units`` skips a blank row and the order would close
	with the rifle still on the shelf.

	An earlier cut ran only the blank-serial half, unconditionally — so a
	non-antique row produced a refusal that told staff to go looking for an
	antique that was not there.

	Lane set imported from ``fulfillment`` rather than restated: a second copy is
	a copy that drifts. Lazily, because ``fulfillment`` imports this module.
	"""
	from ffl_woo_sync.woocommerce.fulfillment import _LOCALLY_DISPOSED_LANES

	for sr in (order.serials or []):
		if not is_antique_item(sr.item_code):
			return ("regulated", sr.item_code)
		if ((sr.get("fulfillment") or "local") in _LOCALLY_DISPOSED_LANES
				and not (sr.get("confirmed_serial") or "").strip()):
			return ("blank_antique", sr.item_code)
	return None


def _finish_non_firearm_pickup(order) -> dict:
	"""Close a pickup that carries no regulated firearm: no 4473, so no invoice
	to open — the order just needs its stock out, its listing retired and its
	revenue booked.

	Mirrors the BRANCH ``_book_revenue_after_auto_clear`` takes, not the
	auto-clear wrapper itself. That wrapper is gated on
	``woo_status == "completed"``, and its premise — "WooCommerce handles the
	shipping; clear the order once WC says it went" — is the one thing a pickup is
	never: the buyer is at the counter while WooCommerce may say `processing` for
	days. An earlier cut called it anyway, and AFTER setting Transferred, so the
	wrapper's own first-line `order_status` guard bailed and the order left the
	queue with stock, listing and revenue all untouched.

	Runs inline rather than enqueued, unlike its webhook-side twin. That twin
	defers because a webhook must answer fast and cannot report failure to
	anybody; this is a button a person just pressed, and they should learn now
	whether it worked. A throw rolls the whole thing back — nothing half-done to
	retry, and pressing Pickup again is the retry.

	Ordering matches the antique path's contract: Transferred first (both
	downstream helpers refuse an order that is not), then stock out -> retire
	listing -> book revenue, revenue last so a throw there cannot roll back an
	issued gun.
	"""
	from ffl_woo_sync.woocommerce.revenue import book_web_order_completion

	unresolved = _unresolved_local_unit(order)
	if unresolved and unresolved[0] == "regulated":
		frappe.throw(_(
			"{0} carries a serialised non-antique unit ({1}) that owes its own "
			"bound-book exit — it cannot be closed from here."
		).format(order.name, unresolved[1]), title=_("Not a Plain Pickup"))
	if unresolved:
		frappe.throw(_(
			"{0} has an antique unit with no serial yet. Enter the serial on the "
			"WooCommerce order first — without it the rifle never leaves stock, and "
			"closing the order here would book the sale with the gun still on the "
			"shelf."
		).format(order.name), title=_("Serial Required"))

	order.transferred_at = mark_transferred(order.name)
	order.order_status = _TRANSFERRED
	# Antique retire vs plain revenue booking: decided once, in
	# revenue.web_completion_job (the webhook auto-clear enqueues the same answer).
	book_web_order_completion(order)
	return {"ok": True, "mode": "closed", "pos_invoice": None,
		"order_status": _TRANSFERRED}


def _pickup(order) -> dict:
	"""The pickup itself, off an already-loaded order.

	Deliberately NOT whitelisted, and it takes the DOCUMENT. An earlier cut
	whitelisted a single function with an `_order_doc` "test seam" parameter —
	Frappe splats `frappe.form_dict` into whitelisted methods, so that let a
	caller drive invoice creation off request-supplied lines, serials and totals
	instead of the persisted order, and the OpenAPI spec advertised the parameter.
	Same split `revenue.create_sales_invoice_for_web_order` /
	`create_web_invoice_now` and `antique.retire_antique_web_order` /
	`retire_antique_web_order_now` already use, for the same reason.
	"""
	if not is_own_license(order.ffl_license or ""):
		frappe.throw(_(
			"{0} is not an in-store pickup — it ships to {1}. Use Dispose."
		).format(order.name, order.ffl_business_name or order.ffl_license or "?"),
			title=_("Not a Pickup"))
	if order.order_status != _PENDING:
		frappe.throw(_(
			"{0} is not pending fulfillment (status: {1})."
		).format(order.name, order.order_status))

	existing = _open_draft_for_order(order.name)
	if existing:
		return {"ok": True, "mode": "counter", "pos_invoice": existing,
			"order_status": order.order_status}

	_refuse_unless_ready(order)

	if not any(is_regulated_firearm_item(ln.item_code) for ln in (order.lines or [])):
		return _finish_non_firearm_pickup(order)

	return {"ok": True, "mode": "counter",
		"pos_invoice": _open_counter_invoice(order),
		"order_status": order.order_status}


@frappe.whitelist()
def pickup_web_order(woo_online_order: str) -> dict:
	"""Hand an in-store-pickup web order to the counter.

	Opens a DRAFT POS Invoice for a firearm order — already paid, serials carried
	from WooCommerce where staff typed them — so the cashier can run the 4473 and
	Complete Order the ordinary way. An order with no regulated firearm needs no
	4473 and is finished on the spot instead.

	The web order deliberately STAYS in the Pending Order queue until that counter
	invoice is submitted (see :func:`close_web_order_on_pos_submit`). Retiring it
	at the click would hide the one case that needs watching: a draft opened and
	abandoned, with the gun still in stock, the money still unbooked, and the
	customer possibly already holding the rifle.

	Args:
	    woo_online_order: Woo Online Order name (must be Pending Fulfillment and
	        addressed to this store's own FFL).

	Returns:
	    {ok, mode: "counter"|"closed", pos_invoice, order_status}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	return _pickup(frappe.get_doc("Woo Online Order", woo_online_order))


def _release_reserved_serials(order_name: str) -> None:
	"""Drop the web soft-reserve on the guns this order held.

	`_reserve_serial` stamps {woo_reserved, woo_order} when the order claims a
	gun, and BOTH sibling exits clear it — `dispose_web_order` and
	`retire_antique_web_order` each do so explicitly. The pickup path did not, so
	a sold gun kept a phantom hold: if it ever came back (cancel/return), the
	counter refused to sell it ("reserved by web order …") and the web
	auto-assign query, which filters `woo_reserved: 0`, would never offer it
	again.
	"""
	for serial in frappe.get_all(
		"Serial No", filters={"woo_order": order_name}, pluck="name"
	):
		frappe.db.set_value("Serial No", serial,
			{"woo_reserved": 0, "woo_order": ""}, update_modified=False)


def require_web_tender_on_submit(doc, method: str | None = None) -> None:
	"""POS Invoice before_submit — a pickup ticket is settled by WooCommerce, in
	full, and by nothing else.

	The buyer paid online; the ticket's only tender is the WooCommerce row
	:func:`_attach_payment` wrote. The register used to lose that row: ERPNext's
	`set_default_payment` zeroed it on the first cart recompute (it is not the
	profile's default mode), the resume path then saw no tender and reset the
	rows to the profile's, and the payment screen offered the full total on the
	card terminal — to a buyer who had already paid. The client now keeps the
	row; this is the rule behind it for every other path (a stale tab, a Desk
	submit, the API).

	Strict on purpose, the same mirror `_attach_payment` checks at creation: the
	ticket must still total what WooCommerce charged, and carry nothing but that
	tender. A counter tender here is the customer paying twice; a WooCommerce
	amount off the order total books money nobody collected. Extra goods go on a
	ticket of their own.

	The order itself is re-checked too — :func:`_refuse_unless_ready`, the same
	refusals Pickup made when it opened this draft. The 4473 can take days; a
	buyer who cancelled or was refunded online in the meantime must not leave
	with the gun while the full amount is booked as collected.
	"""
	link = (doc.get("woo_online_order") or "").strip()
	if not link or doc.get("is_return"):
		return
	order = frappe.get_doc("Woo Online Order", link)
	if order.order_status != _PENDING:
		frappe.throw(_(
			"{0} is no longer waiting for pickup (status: {1}). Do not hand the gun "
			"over; ask a manager."
		).format(link, order.order_status), title=_("Web Order Changed"))
	_refuse_unless_ready(order)
	charged = flt(order.order_total)
	web = sum(flt(p.amount) for p in (doc.payments or [])
		if p.mode_of_payment == WOO_MODE_OF_PAYMENT)
	counter = sorted({p.mode_of_payment for p in (doc.payments or [])
		if p.mode_of_payment != WOO_MODE_OF_PAYMENT and flt(p.amount)})
	if (not counter and abs(web - charged) <= _TOTAL_TOLERANCE
			and abs(flt(doc.grand_total) - charged) <= _TOTAL_TOLERANCE):
		return
	frappe.throw(_(
		"{0} is web order {1}, already paid on WooCommerce ({2}). It can only be "
		"completed with the WooCommerce payment for exactly that amount — this "
		"ticket has WooCommerce {3}, total {4}{5}. Take no payment at the counter; "
		"ring any extra items on a separate ticket, and ask a manager to fix this one."
	).format(doc.name, link, frappe.bold(charged), frappe.bold(web),
		frappe.bold(flt(doc.grand_total)),
		_(", and counter payment by {0}").format(", ".join(counter)) if counter else ""),
		title=_("Already Paid Online"))


def close_web_order_on_pos_submit(doc, method: str | None = None) -> None:
	"""POS Invoice on_submit — retire the web order the counter just closed.

	Submit is the moment the sale is real: the 4473 has cleared its gate, stock
	is issued and the revenue is booked. Only then does the order leave the
	Pending Order queue.

	A raw write, deliberately: the Woo Online Order carries no doc_events, and
	loading and saving it here would drag order validation into a POS submit
	that has nothing to do with it.
	"""
	link = (getattr(doc, "woo_online_order", None) or "").strip()
	if not link:
		return
	if frappe.db.get_value("Woo Online Order", link, "order_status") != _PENDING:
		return  # already retired (a cancel, or a re-submit after amend)
	mark_transferred(link)
	_release_reserved_serials(link)


def reopen_web_order_on_pos_cancel(doc, method: str | None = None) -> None:
	"""POS Invoice on_cancel — put the web order back in the queue.

	ERPNext returns the gun to stock on cancel; without this the order stayed
	`Transferred` for ever, invisible in the Pending Order queue with the rifle
	back on the shelf and the sale unbooked — and Pickup could not be pressed
	again, because it refuses an order that is not Pending Fulfillment.

	The reservation is NOT re-taken: the gun is free stock again and the order
	will re-claim one through the ordinary path if it needs to.
	"""
	link = (getattr(doc, "woo_online_order", None) or "").strip()
	if not link:
		return
	if frappe.db.get_value("Woo Online Order", link, "order_status") != _TRANSFERRED:
		return
	unmark_transferred(link, status=_PENDING)
