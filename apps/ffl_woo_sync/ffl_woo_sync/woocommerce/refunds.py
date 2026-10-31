"""Sync WooCommerce partial refunds / amount adjustments into the web-order SI.

WooCommerce keeps ``order.total`` at the GROSS amount for the life of the order;
a partial refund surfaces only as a row in the order's ``refunds`` array
(``{id, total<0}``). The NET the store actually keeps is
``order_total − Σ|refund.total|``. This module makes ERPNext mirror that net:

- :func:`record_order_refunds` records each Woo refund on the Woo Online Order
  (child table ``refunds``), **deduped by Woo refund id** — the idempotency key
  that keeps a re-delivered ``order.updated`` webhook from booking a refund
  twice;
- :func:`apply_refund_discount` lets the revenue booking be born at the net
  total when refunds already exist — a Net-Total **discount**, never a phantom
  ``-1`` firearm line (the Sales Report's qty must stay truthful). The tax and
  shipping WooCommerce gave back (read from each refund's breakdown, REST
  ``orders/<id>/refunds``) reverse the Woo tax row and the shipping instead, so
  refunded tax leaves Sales Tax Payable;
  only the goods part is a discount;
- :func:`reconcile_web_order_refund` brings an ALREADY-submitted web SI down to
  the net via **cancel + amend** when a refund lands after the sale was booked.

Why cancel+amend is A&D-safe here (the one thing that would be catastrophic):
the web SI is ``update_stock=0`` and web FFL Dispositions link to the order by
``woo_online_order``, **never by ``sales_invoice``** (fulfillment.py books them
before the SI exists). So ``Sales Invoice.on_cancel`` →
``cancel_dealer_dispositions`` (which matches on ``sales_invoice``) finds
nothing: the bound book and FastBound are untouched, and no stock moves.
:func:`_assert_amendable_web_si` is the belt-and-suspenders guard — it refuses
to cancel any invoice that *does* link a submitted disposition.

Idempotency is layered:
  1. refund rows are keyed by Woo refund id — a re-delivered webhook adds nothing;
  2. the SI's own ``grand_total`` is the source of truth — if it already equals
     the net (e.g. a refund fixed by hand), reconcile is a no-op. This is what
     keeps the two hand-corrected historical orders from being
     amended a second time.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

# _TOTAL_TOLERANCE: the amended SI must also mirror the Woo net to the cent.
from ffl_woo_sync.woocommerce.revenue import _TOTAL_TOLERANCE, _ensure_paid
from ffl_core.utils import guest_job_elevated


# ------------------------------------------------------------ parse + record


def _woo_refund_rows(body: dict) -> list[dict]:
	"""Refund rows from a WC order payload's ``refunds`` array, normalised to
	``{woo_refund_id, refund_amount(+), reason}``. Woo reports ``total`` negative
	(``"-9.99"``); we store the magnitude. Rows without an id, or with a zero
	amount, are dropped (nothing to book, nothing to dedupe on)."""
	out: list[dict] = []
	for entry in (body.get("refunds") or []):
		if not isinstance(entry, dict):
			continue
		refund_id = str(entry.get("id") or "").strip()
		if not refund_id:
			continue
		amount = abs(flt(entry.get("total")))
		# _TOTAL_TOLERANCE does double duty here as a MINIMUM bookable amount: a
		# sub-cent refund is below the same epsilon the SI total is mirrored to, so
		# it can neither move the net nor be deduped meaningfully — skip it.
		if amount <= _TOTAL_TOLERANCE:
			continue
		out.append({
			"woo_refund_id": refund_id,
			"refund_amount": amount,
			"reason": (str(entry.get("reason") or "").strip() or None),
		})
	return out


def record_order_refunds(order_doc, body: dict) -> bool:
	"""Upsert the payload's refunds onto ``order_doc.refunds``, deduped by Woo
	refund id. A new row is "Pending": its tax / shipping breakdown is read
	later, by :func:`_reconcile` in the long queue — never inside the webhook
	request, where a slow WooCommerce could hold the delivery long enough for Woo
	to disable the webhook. Returns True if any NEW refund row was added (so the
	caller persists + triggers a reconcile). A re-delivered webhook → False."""
	_refresh_breakdown_fields(order_doc)
	rows = _woo_refund_rows(body)
	if not rows:
		return False  # nothing to record — don't even touch the child table
	known = {str(r.woo_refund_id) for r in (order_doc.refunds or [])}
	changed = False
	for row in rows:
		if row["woo_refund_id"] in known:
			continue
		order_doc.append("refunds", {
			**row, "recorded_on": now_datetime(), "breakdown_status": _PENDING})
		known.add(row["woo_refund_id"])
		changed = True
	return changed


# Refund row breakdown_status. Only FETCHED rows move tax / shipping; every other
# row books the old way (its whole amount a goods discount). PENDING is re-read;
# the other two are terminal.
_FETCHED, _PENDING, _NOT_IN_WOO, _REJECTED = "Fetched", "Pending", "Not in Woo", "Rejected"
_CENT = 0.01  # slack when checking a breakdown against the refund / the order
_BREAKDOWN_FIELDS = ("breakdown_status", "tax_refunded", "shipping_refunded")


def has_pending_breakdown(order_doc) -> bool:
	return any(r.get("breakdown_status") == _PENDING for r in (order_doc.refunds or []))


def _pending_ids(order_doc) -> set[str]:
	return {str(r.woo_refund_id) for r in (order_doc.refunds or [])
		if r.get("breakdown_status") == _PENDING}


def _refresh_breakdown_fields(order_doc) -> None:
	"""Pull the breakdown fields of the saved refund rows from the database into
	this in-memory order. The reconcile job writes them on the child rows only
	(:func:`_settle_row`), so an order a webhook loaded earlier would otherwise
	write its stale "Pending" back over them when it saves."""
	names = [r.name for r in (order_doc.refunds or []) if r.get("name")]
	if not names:
		return
	current = {d.name: d for d in frappe.get_all("Woo Online Order Refund",
		filters={"name": ("in", names)}, fields=["name", *_BREAKDOWN_FIELDS])}
	for row in order_doc.refunds:
		if row.get("name") in current:
			for field in _BREAKDOWN_FIELDS:
				setattr(row, field, current[row.name].get(field))


def _settle_row(row, status: str, tax: float = 0.0, shipping: float = 0.0) -> None:
	"""Write a refund row's breakdown straight onto the child row — never a save
	of the parent order, whose ``modified`` a concurrent webhook save checks."""
	values = {"breakdown_status": status, "tax_refunded": tax, "shipping_refunded": shipping}
	for field, value in values.items():
		setattr(row, field, value)
	frappe.db.set_value("Woo Online Order Refund", row.name, values, update_modified=False)


# _fetch_woo_refunds outcomes
_READ, _GONE, _REFUSED, _DOWN = "read", "gone", "refused", "down"


def _fetch_woo_refunds(order_doc) -> tuple[str, dict | None]:
	"""Read the order's refunds from WooCommerce: (outcome, {refund id: refund}).

	- ``read``    — the listing (settle the Pending rows against it);
	- ``gone``    — 404: the order is gone from Woo; its refunds can never be read;
	- ``refused`` — 401/403 or missing/invalid settings: persistent until someone
	  fixes the credentials — rows stay Pending, one store-wide config alert, and
	  the existing cadence (deduped webhook job, 2-hourly sweep) is the only retry;
	- ``down``    — timeout, connection error, 5xx, anything else: transient —
	  rows stay Pending, one alert per order per outage."""
	from ffl_core.alerts import alert_once
	from ffl_woo_sync.woocommerce.client import (
		WooCommerceClient, WooCommerceConfigError, WooCommerceError)

	site = getattr(order_doc, "site", None) or "retail"
	try:
		client = WooCommerceClient.from_settings(site)
		return _READ, {str(r.get("id")): r for r in client.list_order_refunds(order_doc.woo_order_id)}
	except Exception as exc:
		code = exc.status_code if isinstance(exc, WooCommerceError) else None
		if code == 404:
			return _GONE, {}
		if isinstance(exc, WooCommerceConfigError) or code in (401, 403):
			alert_once(
				"Woo Online Order", order_doc.name, dedupe_on_title=True,
				title=f"WooCommerce ({site}) refused reading refunds — check the API keys",
				message=(
					f"Reading the refunds of Woo #{order_doc.woo_order_id} failed: {exc}. "
					f"Check WooCommerce Settings ({site}): store URL and a consumer key "
					"with read access to orders. Until then every web refund books as a "
					"goods discount (tax and shipping are corrected once reads work)."),
				description=(
					"WooCommerce refused the POS's refund reads — fix the WooCommerce "
					"Settings API keys."))
			return _REFUSED, None
		# One alert per outage: none while an alert for the refunds still unread
		# is on file (a successful read settles them; a later refund is a new one).
		since = min((r.recorded_on for r in (order_doc.refunds or [])
			if r.get("breakdown_status") == _PENDING and r.get("recorded_on")), default=None)
		alert_once(
			"Woo Online Order", order_doc.name, dedupe_on_title=True, since=since,
			title=f"Web order {order_doc.name}: refund breakdown not read",
			message=(
				f"Could not read the refunds of Woo #{order_doc.woo_order_id} ({exc}). "
				"The refund books as a goods discount until a later reconcile reads it "
				"(webhook, the 2-hourly backstop, or reconcile_web_order_refund) — tax "
				"and shipping are corrected then."),
			description=(
				f"Web order {order_doc.name}: WooCommerce's refund breakdown could not "
				"be read; its refund is booked as a goods discount for now."))
		return _DOWN, None


def _apply_refund_breakdowns(order_doc, listed: dict, ids: set[str]) -> bool:
	"""Settle the rows that were Pending when ``listed`` was read (``ids`` — a row
	recorded after the read is not in it, and stays Pending for the next run).
	Listed → the breakdown if :func:`_breakdown_problems` passes, else
	"Rejected" (amounts zeroed; the raw ones are in the alert); not listed →
	"Not in Woo". Both terminal outcomes book the old way and are logged once
	per refund, whatever else is open on the order. Returns True when any row
	changed."""
	from ffl_core.alerts import alert_once

	changed = False
	for row in [r for r in (order_doc.refunds or [])
			if r.get("breakdown_status") == _PENDING and str(r.woo_refund_id) in ids]:
		changed = True
		refund = listed.get(str(row.woo_refund_id))
		if refund is None:
			_settle_row(row, _NOT_IN_WOO)
			why = "WooCommerce does not list it on the order"
		else:
			tax, shipping = _refund_breakdown(refund)
			why = _breakdown_problems(order_doc, flt(row.refund_amount), tax, shipping)
			if not why:
				_settle_row(row, _FETCHED, tax, shipping)
				continue
			_settle_row(row, _REJECTED)
			why += f" (Woo reported tax {tax:.2f}, shipping {shipping:.2f})"
		alert_once(
			"Woo Online Order", order_doc.name, dedupe_on_title=True,
			title=f"Web order {order_doc.name}: refund {row.woo_refund_id} booked as a goods discount",
			message=(
				f"Woo #{order_doc.woo_order_id}, refund {row.woo_refund_id} "
				f"({flt(row.refund_amount):.2f}): {why}. It is booked the old way — the "
				"whole amount a goods discount, tax and shipping untouched. Check the "
				"refund on WooCommerce."),
			description=(
				f"Web order {order_doc.name}: a refund breakdown was missing or did not "
				"add up; review the tax/shipping booking."))
	return changed


def _breakdown_problems(order_doc, refund_amount: float, tax: float,
		shipping: float) -> str | None:
	"""Why a read breakdown can't be trusted, or None. It must stay inside the
	refund itself and — with every refund already accepted — inside the order's
	own tax and shipping (a cent of slack)."""
	order_tax = flt(getattr(order_doc, "total_tax", 0))
	order_ship = flt(getattr(order_doc, "shipping_total", 0))
	if tax < 0 or shipping < 0:
		return "a negative tax or shipping amount"
	if tax + shipping > refund_amount + _CENT:
		return f"tax + shipping exceed the refund {refund_amount:.2f}"
	accepted = refund_split(order_doc) or (0.0, 0.0)
	if accepted[0] + tax > order_tax + _CENT:
		return f"refunded tax would exceed the order's tax {order_tax:.2f}"
	if accepted[1] + shipping > order_ship + _CENT:
		return f"refunded shipping would exceed the order's shipping {order_ship:.2f}"
	return None


def _refund_breakdown(refund: dict) -> tuple[float, float]:
	"""(tax, shipping) given back by one WC refund object, as magnitudes: tax =
	Σ total_tax of its line, shipping and fee lines; shipping = Σ shipping line
	totals. A custom-amount refund has no lines → (0, 0): all goods discount."""
	tax = sum(abs(flt(line.get("total_tax")))
		for key in ("line_items", "shipping_lines", "fee_lines")
		for line in (refund.get(key) or []))
	shipping = sum(abs(flt(line.get("total"))) for line in (refund.get("shipping_lines") or []))
	return round(tax, 2), round(shipping, 2)  # cents from Woo's own strings


def refund_split(order_doc) -> tuple[float, float] | None:
	"""(Σ tax_refunded, Σ shipping_refunded) over the refunds whose breakdown was
	read and accepted, or None when none was — a legacy / not-yet-read order
	books exactly as before (everything a goods discount, tax row and shipping
	untouched)."""
	fetched = [r for r in (order_doc.refunds or []) if r.get("breakdown_status") == _FETCHED]
	if not fetched:
		return None
	return (flt(sum(flt(r.tax_refunded) for r in fetched), 2),
		flt(sum(flt(r.shipping_refunded) for r in fetched), 2))


def _woo_tax_rows(si) -> list:
	"""The Woo-collected tax row(s) on a web SI: Actual, on the sales-tax account
	(revenue._build_invoice appends exactly one; channel SIs never carry freight
	tax rows)."""
	taxes = si.get("taxes") or []
	if not taxes:
		return []
	from ffl_woo_sync.woocommerce.revenue import _sales_tax_account
	account = _sales_tax_account(si.company)
	return [t for t in taxes if t.charge_type == "Actual" and t.account_head == account]


def _refund_split_targets(si, order_doc) -> tuple[list, float | None, float | None, float]:
	"""THE refund booking rule, shared by :func:`apply_refund_discount` and
	:func:`_reconcile`'s projection so the two can't drift by a cent:
	(woo tax rows, new Woo tax total or None, new ffl_shipping_amount or None,
	goods discount). None = leave that amount as it is. The tax only moves when
	a refund actually returned tax AND the invoice has a Woo tax row to take it
	from (no row → it stays in the discount, the old way); the sales-tax account
	is only looked up in that case, so a zero-tax order books exactly as before."""
	refunded = total_refunded(order_doc)
	tax, ship = refund_split(order_doc) or (0.0, 0.0)
	rows = _woo_tax_rows(si) if tax > 0 else []
	new_tax = None
	if rows:
		new_tax = max(0.0, round(flt(getattr(order_doc, "total_tax", 0)) - tax, 2))
	else:
		tax = 0.0
	new_ship = None
	if ship > 0:
		new_ship = max(0.0, round(flt(getattr(order_doc, "shipping_total", 0)) - ship, 2))
	# round(), not flt(x, 2): cents arithmetic needs no System Settings lookup
	return rows, new_tax, new_ship, max(0.0, round(refunded - tax - ship, 2))


def total_refunded(order_doc) -> float:
	"""Sum of the recorded refund magnitudes on the order."""
	return flt(sum(flt(r.refund_amount) for r in (order_doc.refunds or [])))


def target_net(order_doc) -> float:
	"""The amount the Sales Invoice should book: gross order total minus refunds."""
	return flt(flt(order_doc.order_total) - total_refunded(order_doc))


def apply_refund_discount(si, order_doc) -> None:
	"""Reduce a (draft) web SI to the net — the grand total lands at
	``target_net`` WITHOUT touching qty (no phantom -1 firearm).

	Where the refunds' breakdown was read (:func:`refund_split`), the tax Woo gave
	back comes off the Woo tax row and the shipping off ``ffl_shipping_amount``
	(the freight hook rebuilds the Shipping row from it), both set ABSOLUTE from
	the order so a re-amend never double-counts; only the rest — the goods — is
	a Net-Total discount (:func:`_refund_split_targets`). Without a breakdown
	(legacy / not read yet / custom amount / rejected) the whole refund is the
	discount, as before. No-op when nothing was refunded."""
	if total_refunded(order_doc) <= _TOTAL_TOLERANCE:
		return
	rows, new_tax, new_ship, goods = _refund_split_targets(si, order_doc)
	if new_tax is not None:
		rows[0].tax_amount = new_tax
		for extra in rows[1:]:
			extra.tax_amount = 0
	if new_ship is not None:
		si.ffl_shipping_amount = new_ship
	si.apply_discount_on = "Net Total"
	si.discount_amount = goods


# ---------------------------------------------------------------- reconcile


def _existing_web_si(order_name: str):
	"""The live (docstatus < 2) Sales Invoice for a web order, or None."""
	return frappe.db.get_value(
		"Sales Invoice",
		{"woo_online_order": order_name, "docstatus": ("<", 2)},
		["name", "docstatus", "grand_total", "discount_amount", "ffl_shipping_amount",
			"company"],
		as_dict=True)


@guest_job_elevated()
def _reconcile(order) -> dict:
	"""Bring the web order's Sales Invoice in line with its recorded refunds.

	Enqueue target of the webhook's refund sync — runs elevated when reached from
	the Guest webhook context, since the cancel + amend + re-collect chain goes
	through ERPNext helpers that permission-check the session
	(:func:`ffl_core.utils.guest_job_elevated`).

	Idempotent + resumable. Decision tree:
	  - nothing refunded              → ``no_refund``
	  - no *submitted* SI yet         → ``no_invoice`` (it is born at net later,
	                                     via :func:`apply_refund_discount`)
	  - refunds ≥ order total         → ``full_refund_alert`` (never post $0)
	  - SI already at the net total   → ``already_net`` (protects hand-fixes),
	                                     or ``pending_breakdown`` while a refund's
	                                     breakdown is still unread
	  - SI still at gross / not split → cancel + amend to net → ``adjusted``

	First reads the breakdown of any Pending refund (HTTP, before the row lock
	so a slow WooCommerce holds no lock); the result is written under the lock.
	"""
	if isinstance(order, str):
		order = frappe.get_doc("Woo Online Order", order)
	# Snapshot which rows the read below is FOR: a refund recorded while Woo is
	# being read must not be judged against a listing taken before it existed.
	ids = _pending_ids(order)
	outcome, listed = _fetch_woo_refunds(order) if ids else (None, None)
	# Serialise concurrent reconciles for the same order — a double order.updated
	# enqueues two jobs; the loser blocks on this row lock until the winner commits,
	# then sees already_net and no-ops, instead of racing a cancel against an
	# already-cancelled SI.
	frappe.db.get_value("Woo Online Order", order.name, "name", for_update=True)
	# Always re-read under the lock: refunds (and breakdowns) saved since the
	# caller loaded the order must be in what is booked.
	order.reload()
	if outcome in (_READ, _GONE):
		_apply_refund_breakdowns(order, listed, ids)
	if total_refunded(order) <= _TOTAL_TOLERANCE:
		return {"status": "no_refund"}
	existing = _existing_web_si(order.name)
	pending = has_pending_breakdown(order)
	if not existing or int(existing.get("docstatus") or 0) != 1:
		return {"status": "no_invoice"}
	target = target_net(order)
	# Line refunds that zero the order out while woo_status is still "completed" →
	# target ≤ 0. Never post a $0/negative SI — alert for a human (this is
	# effectively a full refund the "completed" status hid). Full refunds normally
	# arrive as woo_status="refunded" and route to _handle_order_cancelled instead.
	if target <= _TOTAL_TOLERANCE:
		frappe.log_error(
			title=f"Web order {order.name}: refunds zero out a still-'completed' order",
			message=(
				f"Recorded refunds ({total_refunded(order)}) meet/exceed order_total "
				f"({order.order_total}) but woo_status is not a release status. Refusing "
				f"to amend {existing.get('name')} to $0 — review: genuine full refund, or "
				"a mis-recorded refund amount."))
		return {"status": "full_refund_alert", "sales_invoice": existing.get("name")}
	# With a refund breakdown, "in sync" also means the tax row and the shipping
	# carry it — a refund first booked as a plain discount (breakdown not read
	# yet) is re-amended once it is. Orders without one keep the grand-total-only
	# test, which is what protects the hand-fixed invoices. The invoice's tax
	# rows are read (one light query) only when a tax refund has to move.
	split = refund_split(order)
	light = frappe._dict(company=existing.get("company"), taxes=(
		frappe.get_all("Sales Taxes and Charges",
			filters={"parent": existing.get("name"), "parenttype": "Sales Invoice"},
			fields=["charge_type", "account_head", "tax_amount"])
		if split and split[0] > 0 else []))
	tax_rows, new_tax, new_ship, goods_refund = _refund_split_targets(light, order)
	tax_move = 0.0 if new_tax is None else flt(sum(flt(t.tax_amount) for t in tax_rows)) - new_tax
	ship_move = 0.0 if new_ship is None else flt(existing.get("ffl_shipping_amount")) - new_ship
	if (abs(flt(existing.get("grand_total")) - target) <= _TOTAL_TOLERANCE
			and abs(tax_move) <= _TOTAL_TOLERANCE and abs(ship_move) <= _TOTAL_TOLERANCE):
		# Never "already_net" while a breakdown is unread: the net is right but the
		# split may not be — a later read re-amends it.
		return {"status": "pending_breakdown" if pending else "already_net",
			"sales_invoice": existing.get("name")}

	# Everything that can refuse the amend is asked HERE, before a single write.
	# _amend_si_to_net cancels the collection Payment Entry first, so a refusal
	# discovered halfway rolls the whole job back and the refund goes unrecorded
	# (SI + Woo Clearing left at gross, TX tax base overstated) with nothing but
	# an Error Log to show for it.
	from ffl_core.alerts import alert_once
	from ffl_woo_sync.woocommerce.revenue import stock_leg_cancel_blockers

	held = stock_leg_cancel_blockers(existing.get("name"))
	# The amend re-posts the invoice through an ABSOLUTE Net-Total discount equal
	# to all refunds (apply_refund_discount replaces ``discount_amount``). On a
	# second partial refund the current invoice is already net of the first one,
	# so recover its pre-refund total before applying the new cumulative amount;
	# subtracting cumulative refunds from the already-net grand_total would count
	# the earlier refund twice and hold every legitimate follow-up refund. Actual
	# tax rows do not move under this discount, so the arithmetic is exact. The
	# same test runs again after insert against what ERPNext actually computed.
	# With a breakdown, the tax row and the shipping move to their new absolute
	# values and the discount carries only the goods part — the same
	# _refund_split_targets the amend applies.
	projected = (
		flt(existing.get("grand_total"))
		+ flt(existing.get("discount_amount"))
		- tax_move - ship_move
		- goods_refund
	)
	if abs(projected - target) > _TOTAL_TOLERANCE:
		held.append(
			f"Invoice {existing.get('name')} does not mirror WooCommerce: at the net "
			f"it would come to {projected:.2f} against a Woo net of {target:.2f}.")
	if held:
		alert_once(
			"Sales Invoice", existing.get("name"),
			title=f"Web refund held for {order.name}",
			message=(
				f"Woo order {order.woo_order_id} was refunded {total_refunded(order):.2f} "
				f"but its invoice was NOT amended — nothing was changed:\n\n- "
				+ "\n- ".join(held)),
			description=(
				f"Web order {order.name}: a WooCommerce refund could not be booked "
				"automatically. Resolve the reason(s) in the Error Log, then run "
				"reconcile_web_order_refund."),
		)
		return {"status": "held", "sales_invoice": existing.get("name"), "reasons": held}
	amended = _amend_si_to_net(existing.get("name"), order, target)
	return {"status": "adjusted", "sales_invoice": amended}


# A refund on an order whose invoice sits inside a frozen period cannot reconcile:
# the amend re-posts the original invoice's reversal on its own date and
# check_freezing_date refuses it, so the job fails and leaves an Error Log.
# Recovery: move Company.accounts_frozen_till_date back, run
# reconcile_web_order_refund for the order, then restore the date.
@frappe.whitelist()
def reconcile_web_order_refund(woo_online_order: str) -> dict:
	"""Reconcile a web order's Sales Invoice to its Woo refunds.

	Manual/enqueued entry point.

	Args:
	    woo_online_order: Woo Online Order name.

	Returns:
	    The reconcile result (credit notes booked / already in sync).

	Permissions:
	    Counter roles (same as the other web-order accounting actions).
	"""
	from ffl_core.api.manual_order import _ROLES

	frappe.only_for(_ROLES)
	return _reconcile(woo_online_order)


# ------------------------------------------------------ cancel + amend to net


def _assert_amendable_web_si(si) -> None:
	"""Refuse to cancel anything but a pure accounting web SI. The disposition
	check is the compliance backstop: a web SI never links a disposition (they
	link by ``woo_online_order``), but if one ever did, cancelling would reverse
	the bound book / FastBound entry — abort instead."""
	if not (getattr(si, "woo_online_order", None) or ""):
		frappe.throw(_("Refund reconcile: {0} is not a web-order invoice.").format(si.name))
	if int(getattr(si, "update_stock", 0) or 0):
		frappe.throw(_(
			"Refund reconcile: refuse to amend stock-moving invoice {0}."
		).format(si.name))
	if frappe.db.exists("FFL Disposition", {"sales_invoice": si.name, "docstatus": 1}):
		frappe.throw(_(
			"Refund reconcile: {0} links a submitted FFL Disposition — refuse to "
			"cancel a bound-book-linked invoice."
		).format(si.name))


def _reassert_web_listing_reserved(si) -> None:
	"""Re-Reserve this order's Used Firearm ``listing_status`` after a cancel reset
	them to "Listed". Reuses the status_sync submit hook (a web SI is
	``update_stock=0`` → it sets "Reserved"): the success path gets this from the
	real ``on_sales_invoice_submit`` when the amended SI submits, but the drift path
	does NOT submit, so it must call this explicitly — a shipped firearm must never
	be left showing "Listed"."""
	from ffl_core.listing.status_sync import on_sales_invoice_submit

	on_sales_invoice_submit(si)


def _amend_si_to_net(si_name: str, order, target: float) -> str:
	"""Cancel the gross web SI and re-post an amended one at the net total.

	Sequence (mirrors the counter cancel/edit flow in manual_order):
	  1. reverse the collection Payment Entries (Woo Clearing back to 0 for this
	     order) — snapshot them *before* the cancel unlinks the references;
	  2. cancel the gross SI (A&D-neutral: update_stock=0, no linked disposition);
	  3. amend a copy, applying the refund as a Net-Total discount (no fake qty);
	  4. tolerance-guard the amended total — a drift past the epsilon leaves a
	     Draft + error log (with the listing_status restored), never a silent post;
	  5. re-collect the NET into Woo Clearing via the shared ``_ensure_paid``, on the
	     SI's OWN preserved posting date (cross-month refund stays in the sale month).

	The whole amend runs under ``woo_sync_inbound`` so the cancel+resubmit does NOT
	enqueue redundant OUTBOUND Woo pushes (the gun is already sold/delisted). That
	flag does NOT gate ffl_core's ``listing_status`` state machine (step 4 handles
	the drift case that leaves).
	"""
	from ffl_core.api.manual_order import _receive_payment_entries

	si = frappe.get_doc("Sales Invoice", si_name)
	_assert_amendable_web_si(si)

	prev_inbound = frappe.flags.get("woo_sync_inbound")
	frappe.flags.woo_sync_inbound = True
	try:
		# Web collection is always a single Receive PE per SI (revenue._ensure_paid),
		# so cancelling each outright is safe here — unlike manual_order's edit flow,
		# there is no multi-allocation PE whose other references we'd have to re-thread.
		# Snapshot before the SI cancel unlinks the references.
		for pe in _receive_payment_entries(si.name):
			pe_doc = frappe.get_doc("Payment Entry", pe.name)
			pe_doc.flags.ignore_permissions = True
			pe_doc.cancel()

		# Cancelling the Receive PE(s) above changed this SI's outstanding_amount,
		# bumping its `modified` timestamp in the DB. Reload before cancel or ERPNext
		# raises TimestampMismatchError on the now-stale in-memory doc. Set
		# ignore_permissions AFTER reload — load_from_db resets the flags dict.
		# The accessory stock leg (revenue.deliver_web_stock_lines) points its
		# Delivery Note rows back at this invoice; Frappe's back-link check would
		# refuse the cancel below while that DN stands. Cancel it first (stock
		# comes back on the shelf for a moment), and re-issue it against the
		# amended invoice once that is submitted — on the SAME preserved posting
		# date, so the COGS never leaves the sale's month and the GL nets to zero.
		from ffl_woo_sync.woocommerce.revenue import (
			cancel_web_stock_lines, deliver_web_stock_lines)

		# _reconcile_impl has already run stock_leg_cancel_blockers: reaching here
		# means the leg is ours to reverse (no return, no draft, no mixed DN).
		reversed_dns = cancel_web_stock_lines(si)
		si.reload()
		si.flags.ignore_permissions = True
		si.cancel()

		amended = frappe.copy_doc(si)
		amended.amended_from = si.name
		apply_refund_discount(amended, order)
		amended.flags.ignore_permissions = True
		amended.insert()

		diff = abs(flt(amended.grand_total) - flt(target))
		if diff > _TOTAL_TOLERANCE:
			# The cancel above reset every Used Firearm on this order to "Listed"
			# (status_sync.on_sales_invoice_cancel — NOT gated by woo_sync_inbound).
			# The success path re-Reserves them via the amended submit; this drift
			# path does not submit, so restore "Reserved" here explicitly.
			_reassert_web_listing_reserved(amended)
			from ffl_core.alerts import alert_once

			alert_once(
				"Sales Invoice", amended.name,
				title=f"Web refund amend drift for {order.name}",
				message=(
					f"Amended invoice {amended.name} grand_total {amended.grand_total} != "
					f"net target {target} (diff {diff:.2f}). Left as Draft for review — the "
					f"original {si_name} is cancelled and its firearm listing_status(es) were "
					"restored to 'Reserved'. Fix the lines/discount, submit, then re-run "
					"reconcile_web_order_refund for the payment."
					+ (f"\n\n**Stock: Delivery Note(s) {', '.join(reversed_dns)} were "
						"CANCELLED to allow the amend, so the goods are back on the shelf "
						"in the system while they are not. The stock leg is re-issued "
						"only when this draft is submitted — until then this item is "
						"oversold on WooCommerce.**" if reversed_dns else "")),
				description=(
					f"Web order {order.name}: refund amend left invoice {amended.name} as a "
					"DRAFT (total mismatch)"
					+ (f", and Delivery Note(s) {', '.join(reversed_dns)} are cancelled — "
						"stock reads high until the draft is submitted." if reversed_dns
						else "") + ". Fix and submit it, then run reconcile_web_order_refund."),
			)
			return amended.name

		amended.submit()
		# The amended rows are new names, so the stock leg sees them as never
		# issued and re-issues exactly what the cancelled DN carried.
		deliver_web_stock_lines(amended)
		# Re-collect the NET on the amended SI's own (preserved) posting date so a
		# cross-month refund's Woo Clearing receipt stays in the sale's month.
		_ensure_paid(amended, order, posting_date=amended.posting_date)
		return amended.name
	finally:
		frappe.flags.woo_sync_inbound = prev_inbound


# ------------------------------------------------------------------- alerting


def alert_if_invoiced(order_doc) -> bool:
	"""A web order that goes terminal (cancelled / trash / fully refunded) AFTER
	its sale was booked must never be silent. We do NOT auto-reverse — a shipped/
	transferred firearm's sale is a human decision — but we log loudly. Returns
	True when an alert was raised."""
	si = frappe.db.get_value(
		"Sales Invoice", {"woo_online_order": order_doc.name, "docstatus": 1}, "name")
	if not si:
		return False
	frappe.log_error(
		title=f"Web order {order_doc.name} went terminal after invoicing",
		message=(
			f"Woo #{order_doc.woo_order_id} status='{order_doc.woo_status}' but Sales "
			f"Invoice {si} is already submitted. NOT auto-reversed — a shipped/"
			"transferred firearm's sale needs a human decision (cancel the sale, book a "
			"credit note, or leave as-is). Review and act manually."),
	)
	return True
