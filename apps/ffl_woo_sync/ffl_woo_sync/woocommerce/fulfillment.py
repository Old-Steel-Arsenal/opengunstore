"""Web-order fulfillment — the online (Woo Online Order) side of the POS
"Pending Order" queue.

Counter orders flow through ffl_core.api.manual_order (Sales Invoice → Dispose &
Ship). Paid WooCommerce orders instead arrive as Woo Online Order records (the
ffl_woo_sync webhook), and are fulfilled here:

  list_pending_web_orders → the rows the Pending Order page merges in (tagged
      "web"), one per Woo Online Order still awaiting fulfillment. A firearm line
      with no serial yet shows as `awaiting_serial` (Dispose disabled) until the
      dealer enters it on the WC Edit Order screen.

  dispose_web_order → books one bound-book "FFL Transfer" FFL Disposition per
      firearm serial. The disposition's own on_submit hooks issue the stock-out
      Material Issue (ffl_core.compliance.disposition_stock) and enqueue the
      FastBound push (ffl_integrations). It then books the REVENUE: a submitted
      Sales Invoice mirroring the Woo amounts (revenue.py; update_stock=0 —
      accounting only, the stock already moved). Web sales previously never
      reached the GL.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate, now_datetime, nowdate

from ffl_core.api.manual_order import _ROLES, _has_ship_role  # canonical "may ship firearms" roles
from ffl_core.api.manual_order import overdue_limit_days, stamp_stage
from ffl_core.compliance.ffl_number import is_cr_license
from ffl_core.compliance.dealer_transfer import book_disposition
# Aliased: tests patch fulfillment._serial_company.
from ffl_core.compliance.dealer_transfer import serial_company as _serial_company
from ffl_core.firearm import _is_regulated_firearm_row  # row-level twin of is_regulated_firearm_item
from ffl_core.firearm import is_antique_item, is_own_license
from ffl_woo_sync.woocommerce.pickup import open_drafts_for_orders
from ffl_core.compliance.cr_transfer import cr_note_suffix, record_cr_verification
from ffl_woo_sync.woocommerce.cr_transfer import reject_cr_details_on_non_cr_order
from ffl_woo_sync.woocommerce.revenue import create_sales_invoice_for_web_order
from ffl_woo_sync.woocommerce.sync import _f  # site -> Serial/Item product_id field name

# WooCommerce statuses that mean the customer has PAID — WC's own
# wc_get_is_paid_statuses() default. Only a paid order is shippable. A placed-
# but-unpaid order (WC "pending" / "on-hold") still soft-reserves its serial
# (see ffl_woo_sync.woocommerce.webhook) but must NOT enter the Pending Order
# queue nor be disposed: a firearm cannot ship for an order the customer has not
# paid for. Cancelled/refunded orders are excluded too (their webhook flips
# order_status to "Cancelled"; this is the second line of defense).
_PAID_STATUSES: frozenset[str] = frozenset({"processing", "completed",
	# the storefront plugin's Awaiting Stock: a paid order whose ship-to-store goods have not
	# arrived (ShipStation plan A, 2026-09-24).
	"awaiting-stock"})


def _is_paid_woo_status(woo_status: object) -> bool:
	"""True when a Woo order's live WooCommerce status means payment was received."""
	return (woo_status or "").strip().lower() in _PAID_STATUSES


# Units this store physically holds and must therefore book out of its own bound
# book. A `fds` / `direct` unit shipped from the distributor's warehouse: it was
# never acquired into our A&D record, so there is no disposition to make (H1).
# `to_store` IS ours — it is received into stock first and then dispositioned
# exactly like any other gun.
_LOCALLY_DISPOSED_LANES: frozenset[str] = frozenset({"local", "to_store"})


def _locally_disposed_rows(serial_rows) -> list:
	"""The firearm unit rows this store disposes itself.

	A blank lane means "local": every row written before drop-ship existed has no
	lane, and reading blank as local reproduces the previous behaviour exactly.
	It also fails in the safe direction — a drop-ship row that somehow lost its
	lane surfaces as a LOUD awaiting-serial refusal rather than quietly skipping a
	gun that should have been booked out.
	"""
	return [sr for sr in (serial_rows or [])
		if (sr.get("fulfillment") or "local") in _LOCALLY_DISPOSED_LANES]


# The lane a procure-to-store firearm carries. Its serial row starts empty (the
# webhook holds nothing for a distributor-sourced unit — H1); it is filled by the
# auto-hang once the gun is received into store stock, and then disposed exactly
# like a local gun.
_TO_STORE_LANE: str = "to_store"


def plan_procured_assignments(order_serial_rows, procured_by_item) -> list:
	"""PURE. Which received procure-to-store serials fill which empty ``to_store``
	rows — ``[(row, serial)]`` (P4c auto-hang).

	``procured_by_item`` is ``{item_code: [serial, ...]}``, the real Serial No names
	a procure-to-store batch just booked (``receive_bridge.procured_serials_by_item``).

	Three rules keep the bound book and the store's own guns safe:

	* only a ``to_store`` row is filled — an ``fds`` row's serial is RSR's
	  record-only echo, and ``local`` / ``direct`` rows are never ours to hang here;
	* a row that already carries a serial is left alone (idempotent re-receive);
	* a serial already confirmed anywhere on the order is not re-hung — re-running
	  the same batch cannot spill it onto a second empty row.

	One serial per row, in row order; a surplus is left unused, and a short batch
	leaves the remaining rows empty (they stay awaiting-serial until the rest
	arrives).
	"""
	already = {(r.get("confirmed_serial") or "").strip()
		for r in (order_serial_rows or []) if (r.get("confirmed_serial") or "").strip()}
	pools: dict = {}
	for item_code, serials in (procured_by_item or {}).items():
		fresh = [s for s in (serials or []) if s and s not in already]
		if fresh:
			pools[item_code] = list(fresh)

	out = []
	for row in order_serial_rows or []:
		if (row.get("fulfillment") or "") != _TO_STORE_LANE:
			continue
		if (row.get("confirmed_serial") or "").strip():
			continue
		pool = pools.get(row.get("item_code"))
		if not pool:
			continue
		out.append((row, pool.pop(0)))
	return out


def to_store_ready(receive_statuses) -> bool:
	"""PURE. Have this web order's to_store legs all arrived at the store?

	True only when there IS a to_store leg and every one reads ``Received`` — the
	'arrived, ready to ship' signal the Pending Order queue shows so staff ship the parcel (an
	accessory) or dispose the gun. A pure FDS/direct order has no to_store leg and
	never reads arrived.
	"""
	statuses = list(receive_statuses or [])
	return bool(statuses) and all(s == "Received" for s in statuses)


# ---------------------------------------------------------------- frappe (P4c)


def assign_procured_serials(woo_online_order, procured_by_item) -> dict:
	"""Hang a procure-to-store batch's received serials onto the waiting web order
	(P4c auto-hang).

	Fill each empty ``to_store`` serial row with a real Serial No name just booked
	into store stock (``plan_procured_assignments``) and SOFT-RESERVE it
	(``woo_reserved=1`` / ``woo_order``), reusing the awaiting-serial mechanism:
	once every to_store row carries a serial the order stops reading
	``awaiting_serial`` and ``dispose_web_order`` can run.

	Idempotent — a re-receive finds the rows already filled (the plan returns
	nothing) and writes nothing. Called from ``receive_bridge`` by late-binding on a
	Woo-sourced Procure To Store DO, so a POS-only site (no Woo Online Order exists)
	never reaches it.
	"""
	from ffl_woo_sync.woocommerce.webhook import _reserve_serial

	order = frappe.get_doc("Woo Online Order", woo_online_order)
	# a gun must never be soft-reserved to a dead order. If the buyer cancelled
	# after the DO was placed (the goods still arrive), leave the serials unallocated
	# for staff — P4's woo_order_cancelled has already alerted ops on a placed order.
	if (order.get("order_status") or "") == "Cancelled":
		frappe.logger("ffl_woo_sync").debug(
			f"assign_procured_serials: {woo_online_order} is Cancelled — not hung")
		return {"order": woo_online_order, "assigned": [], "skipped": "cancelled"}
	assignments = plan_procured_assignments(order.serials, procured_by_item or {})
	if not assignments:
		return {"order": woo_online_order, "assigned": []}
	# Persist the serial→order binding on the rows FIRST, then soft-reserve — the same
	# order the canonical webhook flow uses (insert the order's serial rows, then
	# _reserve_serial each). The binding is the compliance-relevant durable state; the
	# reserve is a best-effort soft-hold that dispose_web_order reconciles, so a partial
	# reserve failure leaves a filled row (recoverable), never the reverse.
	for row, serial in assignments:
		row.confirmed_serial = serial
	order.flags.ignore_permissions = True
	order.save()
	for _row, serial in assignments:
		# Reuse the canonical reservation helper (byte-identical to a self-rolled
		# set_value, but it owns the "bypass doc_events via direct SQL" rationale).
		# The per-serial store PUSH that _apply_dealer_serial pairs with a reserve is
		# INTENTIONALLY omitted: a To-Store unit stays dropship_distributor-masked —
		# distributor_stock_for_item publishes the RSR catalog qty over local stock — so
		# no per-serial page shows it and the reserve changes no number on the store.
		_reserve_serial(serial, order.name)
	return {"order": order.name, "assigned": [s for _row, s in assignments]}


def _distributor_order_map(order_names) -> dict:
	"""``{woo_order_name: [{name, distributor, status, order_type, receive_status,
	web_ref}, ...]}`` — every distributor order built for each order, oldest first;
	a cancelled one stays in (the card shows it dimmed, as a counter card does).
	Feeds the queue card's leg strip (with the house's web reference, the number to
	quote when phoning the distributor) and the to_store ``arrived`` flag.

	Empty when ffl_integrations is not installed (the Distributor Order table is
	absent), so the queue degrades to its pre-P4c shape rather than raising."""
	if not order_names or not frappe.db.table_exists("Distributor Order"):
		return {}
	fields = ["name", "distributor", "status", "order_type", "receive_status", "web_ref"]
	rows = frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": "Woo Online Order",
			"source_name": ("in", list(order_names))},
		fields=["source_name", *fields], order_by="creation asc")
	out: dict = {}
	for r in rows:
		src = r.get("source_name")
		if src:
			out.setdefault(src, []).append({f: r.get(f) for f in fields})
	return out


def _to_store_receives(do_map: dict) -> dict:
	"""``{woo_order_name: [receive_status, ...]}`` of each order's Procure To Store
	distributor orders — drives the queue's ``arrived`` ("arrived, awaiting shipment") flag. A
	cancelled one never arrives, so it is left out rather than hold ``arrived``
	false forever. Orders with no live such order are absent."""
	out: dict = {}
	for name, legs in do_map.items():
		statuses = [d.get("receive_status") or "Not Received" for d in legs
			if d.get("order_type") == "Procure To Store" and d.get("status") != "Cancelled"]
		if statuses:
			out[name] = statuses
	return out


def _to_store_receive_map(order_names) -> dict:
	"""``_to_store_receives`` for these orders, read fresh."""
	return _to_store_receives(_distributor_order_map(order_names))


def _cannot_prove_actionless(doc) -> str:
	"""Why this web order must STAY in the queue — ``""`` when POS can PROVE it has
	nothing to do.

	Reached once no serial row on the order is an ACTION item (see the caller: an
	antique's row is not). Having no actionable row is not by itself the proof,
	because that happens in two very different situations:

	  1. every line really is non-regulated (an antique, an accessory) — there is no
	     bound-book entry to make and WooCommerce/ShipStation ships the parcel;
	  2. the firearm line was never RECOGNISED — ``_process_lines`` skips a blank-SKU
	     WooCommerce line before it even appends the ``lines`` row, and
	     ``is_regulated_firearm_item`` answers False for an item_code that resolves to
	     no Item at all (``get_value`` → None → ``_is_regulated_firearm_row(None)``).

	Only (1) may be dropped. Case (2) is a data accident on the COMPLIANCE surface:
	dropped silently it still exits through ``_auto_clear_non_firearm_if_completed``
	(→ Transferred) and books a serial-less Sales Invoice — a real gun leaves the shop
	with zero dispositions in the bound book and no human ever having seen the row.
	The incident was only ever reported BECAUSE it sat here as an ugly zero-button row;
	on this surface fail-loud beats a tidy UI.

	So the drop needs POSITIVE proof: the order HAS commerce lines, every line's
	item_code resolves to a real Item, and none of them is GCA-regulated.
	"""
	if not doc.lines:
		# _process_lines appends nothing at all for a blank-SKU line, so "no lines"
		# is the fingerprint of a payload we failed to parse — zero evidence, which
		# is not the same thing as evidence of nothing.
		return "order carries no commerce lines at all"

	unresolved: list[str] = []
	regulated: list[str] = []
	for ln in doc.lines:
		item_code = (ln.item_code or "").strip()
		# ONE cached read answers both questions, and it is the SAME read
		# ffl_core.firearm.is_regulated_firearm_item() makes — except that here a
		# MISSING row counts as "unknown" instead of silently as "not a firearm",
		# which is the entire point of this function.
		row = frappe.db.get_value(
			"Item", item_code, ["is_firearm", "item_group", "is_antique"],
			as_dict=True, cache=True,
		) if item_code else None
		if not row:
			unresolved.append(item_code or "(blank)")
		elif _is_regulated_firearm_row(row):
			regulated.append(item_code)

	if unresolved:
		return "item_code(s) resolve to no Item: " + ", ".join(sorted(set(unresolved)))
	if regulated:
		# A regulated gun that produced no serial row is a webhook defect, not an
		# idle order — it still owes a bound-book disposition.
		return ("regulated firearm line(s) with no serial row: "
			+ ", ".join(sorted(set(regulated))))
	return ""


@frappe.whitelist()
def list_pending_web_orders() -> list[dict]:
	"""Rows for the Pending Order page: paid Woo Online Orders still awaiting
	fulfillment. Shaped to match ffl_core.api.manual_order.list_pending_dispositions
	so the page renders both sources uniformly (these carry source="woo").

	An order POS cannot act on is omitted — no firearm serial row, no procure-to-store
	leg, and positive proof that no line is a regulated firearm (see the guard in the
	loop). An order that merely LOOKS actionless because we failed to recognise its
	firearm line stays listed and is logged.

	Returns:
	    Pending-order cards (source="woo"), shaped like
	    list_pending_dispositions rows.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	limit_days = overdue_limit_days()
	today = nowdate()
	orders = frappe.get_all(
		"Woo Online Order",
		filters={"order_status": "Pending Fulfillment"},
		fields=["name", "site", "woo_order_id", "customer_name", "atf_ffl_record",
			"ffl_license", "ffl_business_name", "ffl_expiration", "creation",
			"woo_status", "currency", "order_total", "total_tax", "shipping_total",
			"discount_total"],
		order_by="creation asc",
	)
	# P4c: which of these orders have their procure-to-store guns/accessories in the
	# store now (receive_status Received) → the 'arrived, ready to ship' flag. One batched
	# read rather than a per-order query.
	do_map = _distributor_order_map([o.name for o in orders])
	receive_map = _to_store_receives(do_map)
	# Same reason, same shape: the counter draft an earlier Pickup opened. Asked
	# per row this was an N+1 on a page that reloads after every action.
	#
	# Scoped to the pickup rows, which also means it issues NO query at all on the
	# ordinary day when none of them is one — `is_own_license` reads tabSingles
	# directly, so this pass adds no `frappe.get_all`. That matters beyond speed:
	# several existing tests drive this function with an ORDERED
	# `patch("frappe.get_all", side_effect=[...])`, and an unconditional extra
	# read walks off the end of their sequence.
	pickup_names = {o.name for o in orders if is_own_license(o.ffl_license or "")}
	pickup_map = open_drafts_for_orders(sorted(pickup_names))
	out: list[dict] = []
	for o in orders:
		# Only PAID orders are shippable. Skip a placed-but-unpaid order (WC
		# pending / on-hold) — reserved but not payable-to-ship — and defensively
		# any non-paid status (e.g. a cancelled order the query above already drops).
		if not _is_paid_woo_status(o.woo_status):
			continue
		doc = frappe.get_doc("Woo Online Order", o.name)
		# Nothing for POS to DO → the order must not sit in this queue rendering
		# zero action buttons. Three conditions, each load-bearing:
		#   *no serial rows* — the webhook writes one row per REGULATED firearm
		#     unit, so an antique (GCA-exempt) or pure-accessory order has none.
		#     Dispose needs a serial and Ship/Pay/Edit/Cancel render for counter
		#     rows only, so such a row is inert. WooCommerce/ShipStation ships
		#     the parcel, and webhook._auto_clear_non_firearm_if_completed retires
		#     the order (→ Transferred) once WC marks it completed — dropping it
		#     from the queue strands nothing.
		#   *and no to_store leg* — a procure-to-store parcel has no serial row
		#     EITHER, but the STORE physically ships it, so staff must still see the
		#     order — its distributor-order pill (to store / arrived) is what the
		#     card shows for it; dropping it would silently lose a parcel the shop
		#     owes a customer.
		#   *and POS can PROVE the order is actionless* — see
		#     _cannot_prove_actionless: an empty serials table also happens when the
		#     firearm line was never recognised, and dropping THAT silently would
		#     retire a gun with no bound-book disposition. Unprovable ⇒ stays visible
		#     with an audit trace, which is how the incident got reported in the first place.
		to_store_legs = receive_map.get(o.name)
		# The lane recorded at checkout, not just a live DO: a to_store line whose
		# DO was cancelled is still a parcel owed until it is re-ordered (or moved
		# back to the store's own stock, which relanes it `local`).
		to_store_lane = any((ln.get("lane") or "") == _TO_STORE_LANE for ln in (doc.lines or []))
		# An ANTIQUE serial row is not an action item. Antiques carry serial rows for
		# inventory reasons (reservation, the invoice's serial, the delist target —
		# see webhook._process_lines), but they owe no bound-book exit, so a row of
		# one renders the same zero action buttons that put the incident order in this queue.
		# They retire through antique.retire_antique_web_order instead. Every other
		# row still counts, so this reduces to the old `not doc.serials` test for
		# every order that has no antique on it.
		actionable_rows = [sr for sr in (doc.serials or [])
			if not is_antique_item(sr.item_code)]
		# …with one exception. An antique that names NO gun is not "nothing to do":
		# since POS stopped reserving a placeholder for it, nothing takes that rifle
		# out of stock or off the storefront, and both exits now refuse the order
		# until the serial arrives. Dropping it out of the queue here would leave it
		# with no badge, no error and no sweep to rescue it.
		unresolved_antiques = [sr for sr in (doc.serials or [])
			if is_antique_item(sr.item_code)
			and (sr.get("fulfillment") or "local") in _LOCALLY_DISPOSED_LANES
			and not (sr.confirmed_serial or "").strip()]
		if not actionable_rows and not to_store_legs and not to_store_lane and not unresolved_antiques:
			reason = _cannot_prove_actionless(doc)
			if not reason:
				continue
			# Error Log, not merely frappe.logger: the logger only reaches logs/*.log
			# on the server, which nobody tails — an audit trace nobody can find is
			# decoration, and this row's whole purpose is fail-loud. _log_error is the
			# app's existing Desk-searchable channel (and falls back to the logger if
			# writing the Error Log itself fails). Imported inside the function because
			# that is this module's established shape for reaching webhook (see
			# assign_procured_serials → _reserve_serial); webhook does not import this
			# module, so there is no cycle either way.
			from ffl_woo_sync.woocommerce.webhook import _log_error

			detail = (
				f"{o.name} (Woo #{o.woo_order_id}) has no firearm serial row, but POS "
				f"cannot prove it is actionless — keeping it in the Pending Order "
				f"queue for a human: {reason}")
			_log_error(
				title=f"Pending Order: unverifiable web order {o.name}",
				message=(
					f"{detail}\n\n"
					"An order is dropped from the queue only when every line resolves to "
					"a real Item and none is GCA-regulated. This one failed that test, so "
					"it stays visible rather than disappearing silently. Resolve the "
					"item_code(s) above, or dispose the firearm by hand if one was sold."
				),
			)
			frappe.logger("woo.fulfillment").warning(detail)
		pending: list[dict] = []
		serial_errors: list[dict] = []
		awaiting = False
		# Lanes of the units still without a serial. Only a local unit's comes from
		# WooCommerce; a to_store unit's comes with the distributor receive and an
		# fds/direct unit's with the distributor's ship notice — the card must not
		# send staff to Woo for those.
		awaiting_lanes: set = set()
		# ACTIONABLE rows only — an antique's row must not reach this dialog. Listing it
		# under "serials to dispose" would tell staff a GCA-exempt gun is about to enter
		# the bound book (dispose_web_order filters it out, so the dialog would simply be
		# lying).
		#
		# It used to follow that an unresolved antique must not set `awaiting` either —
		# "it owes no bound-book entry, so it has no standing to hold up a gun that
		# does". That rested on the antique row carrying a placeholder, which kept the
		# rifle reserved and off the storefront while the row waited. Since POS stopped
		# guessing which unit a uniform-price order gets, it carries none, so BOTH exits
		# now refuse such an order and the loop further down badges it. It still stays
		# out of `pending`.
		for sr in actionable_rows:
			serial = (sr.confirmed_serial or "").strip()
			if serial:
				pending.append({"item_code": sr.item_code, "serial": serial})
			else:
				awaiting = True
				awaiting_lanes.add(sr.get("fulfillment") or "local")
				# A serial DID arrive from WooCommerce but was rejected — surface
				# why, or the row reads as "Woo never sent it" and stalls silently.
				err = (sr.reservation_error or "").strip()
				if err:
					serial_errors.append({"item_code": sr.item_code, "error": err})
		# An antique stays out of `pending` — it books no disposition, so listing it
		# under "serials to dispose" would be a lie — but one that names NO gun is
		# still shown as awaiting, and says why. Both exits refuse the order while
		# it is in this state (`dispose_web_order`,
		# `webhook._auto_clear_non_firearm_if_completed`), so staff need to see what
		# is holding it.
		for sr in unresolved_antiques:
			awaiting = True
			awaiting_lanes.add(sr.get("fulfillment") or "local")
			# Split by lane: a `to_store` unit's serial comes from the DISTRIBUTOR
			# receive (`plan_procured_assignments`), not from WooCommerce. Telling
			# staff to type one on the Woo order would be taken at its word — and
			# `_apply_serials_from_woo`'s lane filter accepts `to_store`, so POS would
			# reserve one of OUR rifles against a unit the distributor is sending.
			if (sr.get("fulfillment") or "local") == _TO_STORE_LANE:
				msg = _(
					"Antique unit is awaiting the serial of the rifle being procured "
					"to the store. It books no disposition, but the serial is what "
					"takes the rifle out of stock and off the storefront — it arrives "
					"with the distributor receive, not from WooCommerce."
				)
			else:
				msg = _(
					"Antique unit is awaiting its serial from WooCommerce. It books no "
					"disposition, but the serial is what takes the rifle out of stock "
					"and off the storefront."
				)
			serial_errors.append({"item_code": sr.item_code, "error": msg})
		expiry, expired = _ffl_status(o)
		is_cr = is_cr_license(o.ffl_license or "")
		store_pickup = o.name in pickup_names
		out.append({
			"source": "woo",
			"site": o.site,
			"woo_online_order": o.name,
			"woo_order_id": o.woo_order_id,
			"woo_status": o.woo_status,
			"customer": o.customer_name or o.name,
			"customer_name": o.customer_name,
			"is_business": False,
			# C&R (Type 03): the dispose dialog grows a verification block for
			# these, pre-filled from what we already know. The expiry is
			# deliberately NOT carried across orders — staff must read it off the
			# licence every time, because a licence valid last month may have
			# lapsed since.
			"is_cr": is_cr,
			"cr_prefill": {
				"license_number": doc.ffl_license or "",
				"contact_name": doc.cr_contact_name or doc.customer_name or "",
				"license_expiry": doc.cr_license_expiry or None,
				"premise_street": doc.cr_premise_street or doc.ship_to_line1 or "",
				"premise_street2": doc.cr_premise_street2 or doc.ship_to_line2 or "",
				"premise_city": doc.cr_premise_city or doc.ship_to_city or "",
				"premise_state": doc.cr_premise_state or doc.ship_to_state or "",
				"premise_zip": doc.cr_premise_zip or doc.ship_to_zip or "",
			} if is_cr else None,
			"destination_ffl": o.atf_ffl_record or o.ffl_license,
			"destination_ffl_name": o.ffl_business_name or o.atf_ffl_record or o.ffl_license,
			"ffl_expiry_date": expiry,
			"ffl_expired": expired,
			"posting_date": o.creation,
			"currency": o.currency,
			"order_total": flt(o.order_total),
			"total_tax": flt(o.total_tax),
			"shipping_total": flt(o.shipping_total),
			"discount_total": flt(o.discount_total),
			"grand_total": flt(o.order_total),
			"outstanding_amount": 0,
			"paid": True,  # Woo orders are paid before they reach us
			# The buyer picked OUR licence in WooCommerce's FFL list: they are
			# collecting at the counter. Dispose is withheld — it would book a
			# transfer to ourselves — but the order STAYS in the queue, because
			# one you can neither dispose nor see is worse than one you merely
			# cannot dispose. The badge is what stops the next person
			# re-discovering this by pressing the button.
			"store_pickup": store_pickup,
			# The counter invoice Pickup already opened, if any. Drives the
			# button's state: Pickup opens one, and once open the row links to it
			# instead — so a cashier who walked away mid-4473 comes back to the
			# same draft rather than a button that looks like nothing happened.
			"pickup_invoice": pickup_map.get(o.name) if store_pickup else None,
			"needs_disposition": bool(pending) and not store_pickup,
			"awaiting_serial": awaiting,
			"awaiting_serial_lanes": sorted(awaiting_lanes),
			"pending_serials": pending,
			"serial_errors": serial_errors,
			# 'Arrived, ready to ship': a to_store leg is linked, and (arrived) it has been
			# received into the store — staff ship the accessory / dispose the gun.
			"to_store": bool(to_store_legs) or to_store_lane,
			"arrived": to_store_ready(to_store_legs),
			"distributor_orders": do_map.get(o.name, []),
		})
		# Section / reason / age: the one rulebook in ffl_core.
		stamp_stage(out[-1], o.creation, limit_days, today)
	return out




@frappe.whitelist()
def get_dispose_stock_lines(woo_online_order: str) -> list[dict]:
	"""The loose-stock lines of a web order and where in the store each can be taken
	from, for the "where did these come from?" step of Dispose.

	Args:
	    woo_online_order: Woo Online Order name.

	Returns:
	    [{line, item_code, item_name, qty, locations: [{location, label, qty}],
	    unassigned, chosen}] -- one per line that ships from the shelf as loose stock.
	    ``chosen`` is the location already recorded for the line, or the sole place the
	    item can only have come from (exactly one holds it, none is unassigned).
	"""
	frappe.only_for(_ROLES)
	from ffl_core import storage
	from ffl_woo_sync.woocommerce.revenue import _default_company, local_stock_lines

	order = frappe.get_doc("Woo Online Order", woo_online_order)
	return storage.dispose_pick_lines(_default_company(), [
		{"line": l.woo_line_id, "item_code": l.item_code, "qty": flt(l.qty),
			"chosen": l.get("storage_location")} for l in local_stock_lines(order)])


def _stamp_line_locations(order, locations) -> None:
	"""Record where staff took each loose-stock line from (``{woo_line_id: location}``)
	on the order's lines, so the invoice and its delivery carry it. Anything that is not
	one of this order's shelf lines is refused rather than ignored."""
	from ffl_core import storage
	from ffl_woo_sync.woocommerce.revenue import _default_company, local_stock_lines

	company = _default_company()
	picks = storage.parse_picks(locations)
	if not picks:
		return
	shelf = {str(l.woo_line_id): l for l in local_stock_lines(order)}
	for line_id, loc in picks.items():
		line = shelf.get(line_id)
		if not line:
			frappe.throw(_("Line {0} is not a stock line of {1}, so it has no location to pick.").format(
				line_id, order.name))
		checked = storage.validate_open_location(line.item_code, loc, company)
		frappe.db.set_value("Woo Online Order Line", line.name, "storage_location", checked,
			update_modified=False)
		line.storage_location = checked


@frappe.whitelist()
def dispose_web_order(woo_online_order: str, cr_details=None, locations=None) -> dict:
	"""Dispose the firearms on a paid Woo Online Order (web Dispose action).

	No ShipStation push here: the store's own ShipStation WooCommerce
	plugin creates the shipment. Books one submitted "FFL Transfer"
	disposition per not-yet-disposed firearm serial; each disposition's own
	on_submit issues the stock-out and enqueues the FastBound push. No
	Sales Invoice is created. Idempotent via the {serial, woo_online_order}
	guard.

	Refuses an in-store pickup — an order whose receiving FFL is this store's own
	licence (``FFL Settings.own_ffl_license``). There is no far-end dealer to
	transfer to; the buyer collects at the counter under a retail 4473, and
	booking it here would write a disposition to ourselves into the bound book.

	Args:
	    woo_online_order: Woo Online Order name (must be Pending
	        Fulfillment).
	    cr_details: Staff verification captured in the dispose dialog for a
	        Type 03 (Curio & Relic) order — see cr_transfer.py. Defaults to
	        None and is only consulted for a C&R order. Dict from Python or
	        JSON string from the browser.
	    locations: Optional {woo_line_id: Storage Location} — where in the store
	        staff took each loose-stock line from (see get_dispose_stock_lines).
	        The units leave that location with the order's delivery; a line left out
	        goes to the "to confirm" list on the Storage Locations page.

	Returns:
	    {ok, dispositions: [names], disposed_count, sales_invoice,
	    invoice_status, ...}.

	Permissions:
	    Counter roles.
	"""
	frappe.only_for(_ROLES)
	order = frappe.get_doc("Woo Online Order", woo_online_order)
	# In-store pickup: the buyer chose OUR licence in WooCommerce's FFL list and is
	# collecting at the counter. This function books an FFL Transfer to the order's
	# FFL, which here is us — "we transferred this gun to ourselves". That entry
	# never happened, it takes the gun off our book while it sits on the shelf, and
	# it leaves the disposition that DOES happen (a retail sale to a non-licensee,
	# with a 4473) unwritten. Refuse, ahead of every other gate: an unpaid pickup
	# must be told it is a pickup, not sent to fix payment and then hit this.
	#
	# Deliberately does NOT book the retail sale instead. FFL Disposition's
	# _reject_manual_retail_sale reserves Retail Sale records for FastBound's 4473
	# flow, and that rule is precisely why no 4473-bypass record can exist. The
	# counter owns this sale; POS only stops the wrong one being written here.
	if is_own_license(order.ffl_license or ""):
		frappe.throw(_(
			"{0} is an in-store pickup: the buyer chose this store as the receiving "
			"dealer, so nothing ships and there is no far-end dealer to transfer to. "
			"Run the 4473 at the counter when they collect — that records the sale "
			"to them and takes the gun off the book. Booking it here would put a "
			"disposition to ourselves in the bound book."
		).format(woo_online_order), title=_("In-Store Pickup"))
	if order.order_status != "Pending Fulfillment":
		frappe.throw(_(
			"{0} is not pending fulfillment (status: {1})."
		).format(woo_online_order, order.order_status))
	if not _is_paid_woo_status(order.woo_status):
		frappe.throw(_(
			"{0} has not been paid on WooCommerce (status: {1}); an unpaid order "
			"cannot be shipped."
		).format(woo_online_order, order.woo_status or "—"))

	local_rows = _locally_disposed_rows(order.serials)
	# An antique carries a serial row for inventory reasons but owes NO bound-book
	# exit. It must never reach the disposition builder below: that would write into
	# the A&D book a gun which does not exist in ATF's eyes. Its inventory leg (stock
	# issue + listing retirement) runs further down, so a MIXED order — an antique
	# alongside a regulated gun — still finishes in this one click.
	antique_rows = [sr for sr in local_rows if is_antique_item(sr.item_code)]
	serial_rows = [sr for sr in local_rows if not is_antique_item(sr.item_code)]
	if not serial_rows:
		if antique_rows:
			# Checked BEFORE the distributor branch: an antique order does have
			# locally-held units, so "every unit ships from the distributor" would be
			# a plainly false explanation for staff staring at a gun on the shelf.
			frappe.throw(_(
				"{0} carries only antique firearm(s), which are not GCA firearms — "
				"there is no disposition to book and no FFL to ship to. It retires "
				"itself (stock, listing and invoice) once WooCommerce marks the order "
				"completed AND every locally-held unit names its serial — until then "
				"the rifle would never leave stock."
			).format(woo_online_order), title=_("Antique — No Disposition"))
		if order.serials:
			# Every unit on the order ships from a distributor. There is no
			# bound-book exit to book — completion runs off the distributor order's
			# own poll, not this button.
			frappe.throw(_(
				"{0} has no locally-held firearm to dispose — every unit ships from "
				"the distributor. It completes when the distributor order does."
			).format(woo_online_order), title=_("Distributor Fulfilled"))
		frappe.throw(_(
			"{0} has no firearm line to dispose."
		).format(woo_online_order), title=_("Nothing to Dispose"))
	# Refuse to partially dispose: if ANY unit still lacks its serial we must not
	# ship the rest and mark the whole order Transferred (a bound-book gap).
	if any(not (sr.confirmed_serial or "").strip() for sr in serial_rows):
		frappe.throw(_(
			"{0} still has firearm unit(s) awaiting a serial from WooCommerce — "
			"all serials must be entered before shipping."
		).format(woo_online_order), title=_("Awaiting Serial"))
	# The SAME rule for antiques, which `serial_rows` deliberately excludes. An
	# antique owes no bound-book exit, but it is still one rifle on the shelf, and
	# `antique._issue_antique_units` skips a row that names no gun — so disposing
	# here would mark the order Transferred and book its revenue while the rifle
	# never moved. Worse: a claimed unit is released the moment the order leaves
	# Pending Fulfillment, so a SOLD antique would go back on sale. The old
	# placeholder hid that by holding a reservation for ever; a claim does not.
	# `webhook._auto_clear_non_firearm_if_completed` guards the other exit — this
	# rule has to be written at BOTH or it is not a rule.
	# Keyed on `confirmed_serial` ALONE, deliberately: `_issue_antique_units`
	# (antique.py) skips on that field alone, so a hold-only row would sail past a
	# wider predicate and produce exactly the outcome this guard exists to prevent.
	# `antique_rows` already comes from `_locally_disposed_rows`, so a drop-ship
	# row — blank by design — cannot false-positive it.
	if any(not (sr.confirmed_serial or "").strip() for sr in antique_rows):
		frappe.throw(_(
			"{0} still has antique unit(s) with no serial. An antique books no "
			"disposition, but its serial is still what takes the rifle out of stock "
			"and off the storefront — enter it on the WooCommerce order first."
		).format(woo_online_order), title=_("Awaiting Serial"))
	firearm_serials = [(sr.item_code, sr.confirmed_serial.strip()) for sr in serial_rows]

	# Where the loose-stock lines came from -- checked now, before anything is booked, so
	# a bad pick is refused with nothing to undo. The locations ride onto the invoice rows
	# and its delivery when the revenue is booked below.
	_stamp_line_locations(order, locations)

	# --- C&R (Type 03) verification -------------------------------------------
	# A collector's licence is not in the ATF directory, so nothing upstream can
	# supply the destination's name, address or expiry — a human reads them off
	# the licence at this moment. Runs BEFORE the destination is resolved,
	# because it is what makes the destination resolvable at all.
	# Derived from the licence number itself, not from the stored flag: the
	# number is the contract's single source of truth, and a flag can be stale
	# (an order migrated from before this feature, or one whose number was
	# corrected by a path that did not recompute it). Deriving here means the
	# gate cannot be escaped by a wrong flag.
	if is_cr_license(order.ffl_license or ""):
		# Validates, persists, saves, comments and commits (the commit is the
		# boundary that keeps the verification when the booking below fails).
		record_cr_verification(order, cr_details)
	else:
		# Refuse verification data aimed at the wrong order rather than silently
		# stamping collector details onto an ordinary dealer transfer.
		reject_cr_details_on_non_cr_order(cr_details, order)

	# Destination FFL: resolve (find-or-create via eZ Check) + the shippable gate
	# (unexpired + complete address). Throws if it can't be resolved/validated.
	destination = _resolve_and_validate_ffl(order)

	created: list[str] = []
	disposed: list[tuple[str, str]] = []  # (item_code, serial) actually booked here
	for item_code, serial in firearm_serials:
		name = book_disposition(
			serial=serial, item=item_code, link_field="woo_online_order",
			link_name=order.name, destination=destination,
			disposition_type="FFL Transfer", company=_serial_company(serial),
			notes=_disposition_note(order),
			# This module's clock, so tests that pin it keep pinning the date.
			disposition_date=now_datetime())
		if not name:
			continue  # idempotent — already booked
		created.append(name)
		disposed.append((item_code, serial))

	# Release the soft-reserve on disposed serials (the Material Issue just took
	# them out of stock — status Consumed on this path, Delivered on the counter's
	# stock — the reservation flag is moot, clear it for cleanliness).
	for _item_code, serial in disposed:
		frappe.db.set_value("Serial No", serial, {"woo_reserved": 0, "woo_order": ""},
			update_modified=False)

	from ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order import (
		mark_transferred)

	# Status + revenue date together, through the doctype's own front door.
	order.transferred_at = mark_transferred(order.name)  # the booking below reads it

	# Retire each shipped serial on the ORIGINATING store (per-serial SOLD
	# archive or delist per the routing rule / item-level destock — see
	# _delist_disposed). Web sales are the only path that knows the EXACT
	# realized price, so it is resolved here and threaded through.
	_delist_disposed(disposed, order.site, _sold_prices(order))

	# MIXED order: the regulated units are booked, but any antique unit alongside
	# them still owes its inventory leg, and no disposition will ever do it (that is
	# the whole point of an antique). Same helper the auto-clear path uses, so both
	# routes retire an antique identically. Stock first, then the listing — the
	# archive routing only sends a serial to the SOLD page once it is out of stock.
	if antique_rows:
		from ffl_woo_sync.woocommerce.antique import _issue_antique_units

		antique_issued = _issue_antique_units(order)
		if antique_issued:
			_delist_disposed(antique_issued, order.site, _sold_prices(order))

	# Book the revenue (revenue.py). A failure here must NOT roll back the
	# dispositions that just hit the bound book — log it and leave the retry to
	# create_web_invoice_now; the {SI.woo_online_order} key makes it resumable.
	order.order_status = "Transferred"  # reflect mark_transferred above
	order.atf_ffl_record = destination  # ditto (_resolve_and_validate_ffl wrote db only)
	try:
		invoice_result = create_sales_invoice_for_web_order(order)
	except Exception:
		frappe.log_error(
			title=f"Web order {order.name}: revenue Sales Invoice failed",
			message=frappe.get_traceback())
		invoice_result = {"sales_invoice": None, "status": "error"}

	return {"ok": True, "dispositions": created, "disposed_count": len(created),
		"sales_invoice": invoice_result.get("sales_invoice"),
		"invoice_status": invoice_result.get("status")}


# ------------------------------------------------------------------ internals


def _sold_prices(order) -> dict[str, float]:
	"""``{serial: realized per-unit price}`` from the order's WooCommerce lines.

	Joined on `woo_line_id`, the only link between the `serials` child table (which
	carries the confirmed serial) and the `lines` table (which carries the money).
	`line_total` is WC's post-discount, EX-tax line amount — the apples-to-apples
	figure against the listing's own ex-tax `regular_price`, so "Sold for $X" can
	never read higher than the price the page advertised. A multi-unit line splits
	evenly across its units.

	A zero/absent amount is OMITTED rather than reported as 0.00: a comp or
	warranty replacement must not render "Sold for $0.00" (a figure is
	shown only when it is exact and real)."""
	by_line = {str(ln.woo_line_id): ln for ln in (order.lines or []) if ln.woo_line_id}
	prices: dict[str, float] = {}
	for sr in (order.serials or []):
		serial = (sr.confirmed_serial or "").strip()
		line = by_line.get(str(sr.woo_line_id or ""))
		if not serial or line is None:
			continue
		qty = int(line.qty or 0)
		price = flt(line.line_total) / qty if qty > 0 else flt(line.unit_price)
		if price > 0:
			prices[serial] = price
	return prices


def _delist_disposed(disposed: list[tuple[str, str]], site: str | None,
		sold_prices: dict[str, float] | None = None) -> None:
	"""Retire each just-shipped (item_code, serial) on the ORIGINATING `site`. The
	store only went to stock 0 on reservation and stays `publish` until the
	woo_reconcile sweep. A PER-SERIAL firearm's own product is retired; a uniform-price / ITEM-LEVEL
	firearm instead re-pushes its parent Item's (now-decremented) stock, so the
	shared listing isn't wrongly pulled while other units remain — and an
	item-level serial (no per-serial product_id) no longer hands delist_serial a
	ValueError. No-op when neither the serial nor its Item is listed on `site`.

	Which retirement a per-serial gun gets is NOT decided here: sync.route_serial_exit
	is the single source of truth. A disposed web order normally routes
	to `archive` — the gun sold, so its page stays live as a SOLD archive (200,
	keeps its rankings) instead of drafting to a 404 — and carries the exact
	realized price from `sold_prices`. Anything that reaches disposal without a
	sold status (Delivered = counter/SI, Consumed = web order's Material Issue)
	still delists, unchanged."""
	from ffl_woo_sync.woocommerce import sync

	resolved_site = site or "retail"
	pid_field = _f(resolved_site, "product_id")
	sold_prices = sold_prices or {}
	for item_code, serial in disposed:
		if frappe.db.get_value("Serial No", serial, pid_field):
			exit_route = sync.route_serial_exit(serial, site=resolved_site)
			if exit_route == sync.EXIT_ARCHIVE:
				frappe.enqueue(
					"ffl_woo_sync.woocommerce.tasks.archive_serial_sold_task",
					queue="short", serial_no=serial, site=resolved_site,
					sold_price=sold_prices.get(serial),
					enqueue_after_commit=True,
				)
			elif exit_route == sync.EXIT_DELIST:
				frappe.enqueue(
					"ffl_woo_sync.woocommerce.tasks.delist_serial_task",
					queue="short", serial_no=serial, site=resolved_site,
					enqueue_after_commit=True,
				)
			# EXIT_SKIP: the page was already drafted on purpose before the sale —
			# another channel took the gun (a GunBroker sale delists it), or staff
			# pulled it from the web — so the rule keeps it draft + 301
			# rather than resurrecting it as a SOLD page.
		elif item_code and frappe.db.get_value("Item", item_code, pid_field):
			frappe.enqueue(
				"ffl_woo_sync.woocommerce.tasks.push_stock_task",
				queue="short", item_code=item_code, site=resolved_site,
				enqueue_after_commit=True,
			)


def _disposition_note(order) -> str:
	"""Bound-book note for a web-order disposition.

	The C&R variant records WHO verified the licence and WHEN, because on this
	path there is no dealer at the far end and no 4473 — the store's own check is
	the entire paper trail, so it belongs in the book. FastBound truncates the
	note at 1000 chars; both variants are far short of that.
	"""
	base = _("Auto-booked from web order {0} (Woo #{1}).").format(
		order.name, order.woo_order_id)
	if not is_cr_license(order.ffl_license or ""):
		return base
	return base + " " + cr_note_suffix(order)


def _resolve_and_validate_ffl(order) -> str:
	"""Resolve the receiving ATF FFL Record for a web order (hard — throws if it
	can't), run the shippable-FFL gate (unexpired + complete address), and cache
	the resolved record onto the order's atf_ffl_record."""
	from ffl_core.api.manual_order import _require_valid_ffl
	from ffl_core.compliance.dealer_transfer import _resolve_destination_ffl

	dest = (order.atf_ffl_record or "").strip()
	if not dest:
		license_no = (order.ffl_license or "").strip()
		if not license_no:
			frappe.throw(_(
				"Order {0} has no receiving FFL on file — add it before shipping."
			).format(order.name), title=_("No Destination FFL"))
		if is_cr_license(license_no):
			# A Type 03 collector is NOT in the ATF directory, so the eZ Check
			# path below cannot resolve one — and must not be asked to, or the
			# operator gets "Destination FFL Unresolved" (go check the eZ Check
			# connection) when the real answer is "nobody has verified this
			# licence yet". By this point the verification step has already
			# written the record and set atf_ffl_record, so an empty one here
			# means dispose was invoked without it.
			frappe.throw(_(
				"C&R order {0} has not been verified — click Dispose and complete "
				"the C&R form (license number, expiration, collector name, "
				"licensed premises)."
			).format(order.name), title=_("C&R Verification Required"))
		dest = _resolve_destination_ffl(license_no)  # throws if unresolved
	_require_valid_ffl(dest)  # expiry + complete-address gate; throws if invalid
	if (order.atf_ffl_record or "") != dest:
		frappe.db.set_value("Woo Online Order", order.name, "atf_ffl_record", dest,
			update_modified=False)
	return dest


def _ffl_status(o) -> tuple[object, bool]:
	"""(expiry_date, expired) for the list display. Uses the resolved ATF FFL
	Record when present; otherwise the flat ffl_expiration text best-effort
	(ATF date codes like '7C' aren't parseable → expiry unknown, not expired —
	the dispose step resolves + gates it for real). "Unresolved" is every web
	order's normal state at listing time and nothing reads it, so it is not a
	row key (an earlier `ffl_unverified` flag blocked the whole web queue
	under a false "expired" hint before anything consumed it honestly)."""
	if o.atf_ffl_record:
		exp = frappe.db.get_value("ATF FFL Record", o.atf_ffl_record, "expiry_date")
		if exp:
			return exp, getdate(exp) < getdate(nowdate())
		return None, True
	raw = (o.ffl_expiration or "").strip()
	# Parse with get_datetime, NOT getdate: getdate() reports an invalid date via
	# frappe.throw, which appends to message_log even when the raise is caught —
	# the message then rides back in _server_messages (HTTP 200) and the Pending
	# Order page pops up a spurious "<code> is not a valid date string." for ATF
	# expiration codes (e.g. "9C"). get_datetime() never throws into message_log;
	# a non-date code raises a plain ParserError we swallow → unverified, log clean.
	exp = None
	if raw:
		try:
			dt = get_datetime(raw)
			exp = dt.date() if dt else None
		except Exception:
			exp = None
	if exp:
		return exp, exp < getdate(nowdate())
	return None, False


@frappe.whitelist()
def reserved_web_serials(item_code: str) -> dict:
	"""Web-order hold registry for one Item's in-stock serials.

	Serials held by a Woo Online Order still in 'Pending Fulfillment':
	confirmed_serial (kind="woo_assigned") and stock_hold_serial
	(kind="woo_held"). Drives the 'Pending' pill on the Item form Serial
	Numbers tab.

	Args:
	    item_code: Item whose in-stock serials to check.

	Returns:
	    {serial: {source, ref, kind, tooltip, url}} — only held serials
	    appear.

	Permissions:
	    Canonical shipping roles (passive read, same gate as
	    reserved_counter_serials). A caller WITHOUT the role gets an empty
	    {} back silently — no error is raised — so an empty result is not
	    proof of "no holds".
	"""
	# Gate on the canonical shipping roles — this exposes which serials are reserved
	# on which web order. Same passive-read gate as reserved_counter_serials.
	if not _has_ship_role():
		return {}
	rows = frappe.db.sql(
		"""
		SELECT s.confirmed_serial, s.stock_hold_serial, s.parent
		FROM `tabWoo Online Order Serial` s
		INNER JOIN `tabWoo Online Order` o ON o.name = s.parent
		WHERE s.item_code = %(item_code)s
		  AND o.order_status = 'Pending Fulfillment'
		  AND (COALESCE(s.confirmed_serial, '') != ''
		       OR COALESCE(s.stock_hold_serial, '') != '')
		""",
		{"item_code": item_code},
		as_dict=True,
	)
	out: dict[str, dict] = {}
	for r in rows:
		url = f"/app/woo-online-order/{r['parent']}"
		conf = (r.get("confirmed_serial") or "").strip()
		held = (r.get("stock_hold_serial") or "").strip()
		if conf:
			out[conf] = {
				"source": "woo", "ref": r["parent"], "kind": "woo_assigned",
				"tooltip": _("Assigned to Woo order {0}").format(r["parent"]),
				"url": url,
			}
		if held:
			out.setdefault(held, {
				"source": "woo", "ref": r["parent"], "kind": "woo_held",
				"tooltip": _("Held for Woo order {0} (awaiting confirmed serial)").format(r["parent"]),
				"url": url,
			})
	return out
