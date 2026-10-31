"""Counter drop-ship — the ``ffl_counter_order_distributor`` hook.

Fired by ``ffl_core.api.manual_order.create_order`` INSIDE the counter order's
transaction (after ``si.submit()`` + ``reload()``). It builds the Draft Distributor
Order(s) the counter sale needs and RAISES on any problem, so a failure rolls the
whole counter sale back — no half-built order, complete context at build time, and
no webhook race (the DO exists the instant the invoice does).

It reuses the web router's planner rather than growing a second one: a
Sales-Invoice snapshot → :func:`router.plan_route` → ``api.create_do_from_plan``
with a ``("Sales Invoice", …)`` Dynamic Link source. The only counter-specific bits
are the snapshot (built from the create_order context, NOT re-read off the SI — it
keeps the ORIGINAL ship-to) and the ``is_paid`` routing override (the Draft DO is
always created at the counter; the paid GATE lives at Queue,
``api._source_fully_paid``).

**Layering.** This module imports ``router`` (same app). It never imports
``ffl_woo_sync``. It is reached only through the ``ffl_counter_order_distributor`` hook,
which ``ffl_core`` fires by name — so ``ffl_core`` does not import this either.
"""
from __future__ import annotations

import frappe

from ffl_integrations.distributor import offers, router
from ffl_integrations.notifications import notify_admins


def _counter_snapshot(sales_invoice_name: str, context: dict) -> dict:
	"""A Sales-Invoice drop-ship order in the shape :func:`router.plan_route` reasons
	over.

	Lanes use the router's OWN ``classify_lane`` (firearm → FDS, accessory → direct):
	a counter line that passed create_order eligibility is direct-shippable, so a
	brand-policy To-Store item never reaches here. ``is_paid=True`` because the Draft
	DO is created at the counter regardless — the paid gate is at Queue
	(:func:`api._source_fully_paid`), so a deposit order still lands in the queue.
	"""
	context = context or {}
	lines = []
	for ln in (context.get("lines") or []):
		item_code = ln.get("item_code")
		if ln.get("lane") == router.LANE_TO_STORE:
			# P2 ship to store: the operator chose the house in the plan, so the lane
			# and distributor are FACTS of the line — never re-derived from the Item's
			# drop-ship stamp or listing mode (which may name another house, or none).
			lines.append({
				"sku": ln.get("sku") or _planned_sku(item_code, ln.get("distributor")),
				"item_code": item_code,
				"qty": int(ln.get("qty") or 1),
				"lane": router.LANE_TO_STORE,
				"distributor": ln.get("distributor"),
			})
			continue
		# Drop-ship: the house is the line's (create_order's planned pick, or the
		# invoice row on a rebuild); only a line naming none falls back to the Item's
		# stamp. Its SKU and listing mode are read for THAT house — the Item's one
		# Distributor Listing row (autonamed by item) may be another house's.
		distributor = ln.get("distributor") or frappe.db.get_value(
			"Item", item_code, "dropship_distributor")
		is_firearm = ln.get("is_firearm")
		if is_firearm is None:
			is_firearm = bool(frappe.db.get_value("Item", item_code, "is_firearm"))
		fulfillment_mode = (offers.listing_for(item_code, distributor,
			fields=("fulfillment_mode",)) or {}).get("fulfillment_mode") or "Direct"
		lane = router.classify_lane(
			dropship_distributor=distributor, is_firearm=is_firearm,
			fulfillment_mode=fulfillment_mode)
		sku = ln.get("sku") or (_planned_sku(item_code, distributor) if distributor
			else item_code)  # no house at all: a local lane the route refuses anyway
		lines.append({
			"sku": sku,
			"item_code": item_code,
			"qty": int(ln.get("qty") or 1),
			"lane": lane,
			"distributor": distributor,
		})
	return {
		"name": sales_invoice_name,
		"doctype": "Sales Invoice",
		"woo_status": None,
		"is_paid": True,
		# Counter default = FillOrKill **Y**: the buyer is standing at
		# the counter, so a short-ship is re-quoted on the spot rather than back-ordered.
		"fill_or_kill": True,
		"ship_to": context.get("ship_to"),            # the ORIGINAL payload ship-to
		"destination_ffl": _ffl_block(context.get("destination_ffl")),
		"consumer_name": context.get("consumer_name") or "",
		"contact_num": context.get("contact_num") or "",
		"lines": lines,
	}


def _planned_sku(item_code, distributor):
	"""The distributor's SKU for a line that arrived without one (ship to store, or
	a drop-ship line rebuilt from its invoice) — the same resolution chain the quote
	uses, scoped to that house (its listing → catalog → UPC)."""
	from ffl_integrations.distributor import api

	q = api.quote(item_code, distributor=distributor)
	if not q.get("found") or not q.get("sku"):
		frappe.throw(frappe._("{0} is not in {1}'s catalog — reopen the fulfillment plan.")
			.format(item_code, distributor))
	return q["sku"]


def order_line_to_store(item_code, distributor, qty, is_firearm) -> str | None:
	"""Draft a ship-to-store Distributor Order for one line at this house (the
	counter's "order it from another house instead") — its name, or None when the
	house could not plan it. The SKU resolves as `_planned_sku` does."""
	from ffl_integrations.distributor import api

	created = api._create_orders(distributor, [{"item_code": item_code,
		"sku": _planned_sku(item_code, distributor), "qty": qty, "is_firearm": is_firearm}],
		{"mode": "to_store"})
	return created[0]["name"] if created else None


def _ffl_block(value):
	"""Normalise a context ``destination_ffl``.

	create_order passes the already-resolved block; the ``on_submit`` rebuild only
	has the ATF record name off the invoice, which resolves through the counter's
	own snapshot builder (``manual_order._counter_ffl_block``) so there is one copy.
	"""
	if isinstance(value, dict):
		return value
	from ffl_core.api.manual_order import _counter_ffl_block

	return _counter_ffl_block(value)


def ffl_counter_order_distributor(sales_invoice_name: str, context: dict) -> None:
	"""``ffl_counter_order_distributor`` hook registrant.

	Builds the Draft Distributor Order(s) for a counter drop-ship sale. Raises on any
	problem → the create_order transaction rolls back. RSR is never called here (that
	is placement, gated on full payment).
	"""
	from ffl_integrations.distributor import api
	from ffl_integrations.distributor.base import PlanningError

	context = context or {}
	if not (context.get("lines") or []):
		return

	snapshot = _counter_snapshot(sales_invoice_name, context)
	plan = router.plan_route(snapshot)
	if not plan["route"]:
		# create_order validated the FFL through the canonical chain and holds the SI
		# in-transaction, so this is rare — but a sale we cannot fulfil must roll back,
		# not book. (Individual buyers only; Business is filtered out upstream.)
		frappe.throw(frappe._(
			"This counter order can't be drop-shipped ({0}) — remove the distributor "
			"line or fix the destination.").format(
				router.REASON_TEXT.get(plan["reason"], plan["reason"])))

	# Idempotency: re-firing for a source that already has a live order must
	# create nothing. Reuses the router's own `groups_to_create`, which dedups on
	# (lane, distributor) and is SPLIT-SAFE — an H4 (>400 SKU) plan legitimately
	# yields several DOs for one group, so a naive per-plan dedup would collapse them.
	todo = router.groups_to_create(
		plan["groups"], router.existing_orders("Sales Invoice", sales_invoice_name))
	for group in todo:
		distributor = group.get("distributor")
		if not distributor:
			continue
		dist = frappe.get_doc("Distributor", distributor)
		provider = api.get_provider(dist.provider)
		enriched = [api.enrich_line(provider, ln) for ln in group["lines"]]
		try:
			plans = provider.plan_fulfillment(enriched, group["ship_context"])
		except PlanningError as exc:
			# The two firearm classifications disagree. At the counter the buyer
			# is present and there is no webhook to retry, so roll the sale back with a
			# clear reason rather than park it.
			frappe.throw(frappe._(
				"A drop-ship line can't be classified for ordering: {0}").format(exc))
		by_sku = {ln["sku"]: ln for ln in enriched}
		chosen = context.get("delivery_options") or {}
		for pln in plans:
			firearm = any(by_sku.get(pl["sku"], {}).get("is_firearm") for pl in pln["lines"])
			key = api.delivery_options_key(distributor, pln["order_type"], firearm)
			supports = getattr(provider, "delivery_options", lambda _f: None)(firearm) is not None
			picked = chosen.get(key) if supports else None
			api.create_do_from_plan(
				dist, pln, by_sku,
				source=("Sales Invoice", sales_invoice_name), lane=group["lane"],
				delivery_options=picked)


def repin_dropship_lines(doc, method=None) -> None:
	"""``Sales Invoice: validate`` doc_event — keep a drop-ship line warehouse-less.

	create_order re-pins these rows itself, but on an ``is_pos`` invoice ERPNext's
	``set_pos_fields`` runs inside the CONTROLLER's own validate — after that manual
	re-pin — and refills ``warehouse`` from the POS Profile. frappe runs
	``doc_events['validate']`` handlers AFTER the controller's validate, so this is
	the durable last word (the same ordering P4 relied on to keep the drop-ship PI's
	COGS off SRBNB).

	Scoped by :func:`is_distributor_dropship_row` — both flags required — so a local
	row and a P4c procure-to-store row keep their warehouse.
	"""
	from ffl_core.compliance.firearm import is_distributor_dropship_row

	for row in (getattr(doc, "items", None) or []):
		if is_distributor_dropship_row(row):
			row.warehouse = None


def _recover_ship_to(doc):
	"""The buyer's destination rebuilt from a submitted invoice's shipping Address.

	Returns None when it is absent OR partial — the router's standing rule is that a
	half address is worse than no order at all, so the caller refuses the submit
	rather than drop-shipping to an incomplete destination.
	"""
	addr = getattr(doc, "shipping_address_name", None)
	if not addr:
		return None
	row = frappe.db.get_value(
		"Address", addr,
		["address_line1", "address_line2", "city", "state", "pincode"], as_dict=True)
	if not row:
		return None
	block = {
		"line1": (row.get("address_line1") or "").strip(),
		"line2": (row.get("address_line2") or "").strip(),
		"city": (row.get("city") or "").strip(),
		"state": (row.get("state") or "").strip(),
		"zip": (row.get("pincode") or "").strip(),
	}
	if not (block["line1"] and block["city"] and block["state"] and block["zip"]):
		return None
	return block


def _recover_counter_context(doc, lines, to_store_rows=()) -> dict:
	"""Rebuild the hook context from a submitted invoice alone, or throw.

	A drop-ship firearm leg needs the destination FFL (it ships to the licence); a
	drop-ship accessory leg needs the buyer's address. Either missing = we cannot say
	where the goods go, and guessing is not an option on a channel with no cancel API.
	A procure-to-store row ships to the store, so it needs neither — only its house.
	"""
	rebuilt, needs_ffl, needs_ship_to = [], False, False
	for row in lines:
		item_code = row.get("item_code")
		is_gun = bool(frappe.db.get_value("Item", item_code, "is_firearm"))
		needs_ffl = needs_ffl or is_gun
		needs_ship_to = needs_ship_to or not is_gun
		rebuilt.append({
			"lane": router.LANE_DIRECT if not is_gun else router.LANE_FDS,
			"item_code": item_code,
			"qty": row.get("qty") or 1,
			"rate": row.get("rate") or 0,
			"distributor": row.get("dropship_distributor"),
			"is_firearm": is_gun,
		})

	if needs_ffl and needs_ship_to:
		# This rebuild would have to place an
		# FDS firearm leg AND a direct accessory leg, but a submitted invoice offers
		# only ONE shipping Address, and on a firearm order that Address is the FFL
		# PREMISE. Rebuilding would quietly ship the accessory to the transfer dealer
		# instead of the buyer's door. Refuse and let Create Order rebuild it properly,
		# where the original ship-to is still in hand.
		frappe.throw(frappe._(
			"{0} mixes a drop-ship firearm with a drop-ship accessory, so the "
			"distributor order can't be rebuilt from the invoice alone (the accessory's "
			"delivery address can't be recovered). Cancel it and re-create the sale "
			"from Create Order.").format(doc.name))

	destination_ffl = (getattr(doc, "ffl_destination_ffl", None) or "").strip() or None
	if needs_ffl and not destination_ffl:
		frappe.throw(frappe._(
			"{0} has a drop-ship firearm but no destination FFL, so the distributor "
			"order can't be rebuilt. Cancel it and re-create the sale from Create "
			"Order.").format(doc.name))

	ship_to = _recover_ship_to(doc) if needs_ship_to else None
	if needs_ship_to and not ship_to:
		frappe.throw(frappe._(
			"{0} has a drop-ship accessory but no complete shipping address "
			"(street, city, state and ZIP), so the distributor order can't be "
			"rebuilt. Cancel it and re-create the sale from Create Order."
		).format(doc.name))

	rebuilt += [{
		"lane": router.LANE_TO_STORE,
		"item_code": row.get("item_code"),
		"qty": row.get("qty") or 1,
		"rate": row.get("rate") or 0,
		"distributor": row.ffl_to_store_distributor.strip(),
	} for row in to_store_rows]

	return {
		"lines": rebuilt,
		"ship_to": ship_to,
		"destination_ffl": destination_ffl,
		"buyer_type": getattr(doc, "ffl_buyer_type", None) or "Individual",
	}


def carry_forward_amended_orders(doc) -> int:
	"""A Distributor Order belongs to the LIVE invoice, not a cancelled one.

	Partial refunds are performed as **cancel + amend** (house SOP).
	That leaves SI-1 cancelled while DO-A — the order carrying the
	ack cost and its cost Purchase Invoice — still points at it. Two real
	consequences, both of which this one re-point fixes:

	* the Sales Report resolves the amended invoice's drop-ship line to NO cost
	  (``cost=0``, ``missing_cost=True``), so the report-vs-PI tie-out breaks
	  after EVERY amend — and amend is the routine action, not an edge case;
	* :func:`guard_sales_invoice_submit` would find no order for the amended invoice
	  and rebuild a phantom Draft one, which if confirmed is a SECOND real purchase
	  of goods already bought and already owed for.

	Every order that is **not Cancelled** moves — including COMPLETE ones. A Cancelled
	order is history and stays with the invoice it was withdrawn under.

	⚠️ Deliberately NOT ``order_state.LIVE_AT_DISTRIBUTOR`` here, even though that is
	the canonical "may be real at the distributor" set. That set **excludes
	COMPLETE**, and COMPLETE is precisely the case this function exists for: goods
	shipped, cost real, PI raised. Switching to the constant to "use the canonical
	one" would silently delete the main use case of the carry-forward. (`before_cancel` refuses to cancel
	while an order is LIVE and withdraws Draft/Queued ones, so COMPLETE is what
	actually survives to be carried.)

	The ``amended_from`` chain is walked to the root (an amend of an amend) with a
	seen-set, so a malformed cycle cannot hang the submit.

	Returns the number of orders carried forward.
	"""
	ancestor = getattr(doc, "amended_from", None)
	moved, seen = 0, set()
	while ancestor and ancestor not in seen:
		seen.add(ancestor)
		moved += repoint_orders(ancestor, doc.name, "invoice amended")
		ancestor = frappe.db.get_value("Sales Invoice", ancestor, "amended_from")
	return moved


def repoint_orders(from_invoice: str, to_invoice: str, why: str) -> int:
	"""Move every live Distributor Order from one counter invoice to another.

	The one write the amend and the edit carry-forward share. A Cancelled order stays
	where it is — it is history under the invoice it was withdrawn on.
	"""
	from ffl_integrations.distributor import order_state

	moved = 0
	for row in frappe.get_all(
			"Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": from_invoice,
				"status": ("!=", order_state.CANCELLED)},
			fields=["name"]):
		frappe.db.set_value("Distributor Order", row["name"], "source_name",
			to_invoice, update_modified=False)
		# Audit trail: a purchase's ownership moving from one
		# invoice to another is exactly the link an accountant reconciles along
		# (DO → PI → SI), and set_value writes no Version. Leave a readable trace
		# on the order itself; best-effort, never worth failing a submit over.
		try:
			frappe.get_doc("Distributor Order", row["name"]).add_comment(
				"Comment",
				f"Carried forward from {from_invoice} to {to_invoice} ({why}).")
		except Exception:  # noqa: BLE001
			frappe.log_error(
				title=f"carry-forward comment failed: {row['name']}",
				message=frappe.get_traceback())
		moved += 1
	return moved


def carry_forward_replaced_orders(old_invoice: str, new_invoice: str) -> int:
	"""``ffl_counter_order_replaced`` hook registrant — an EDITED counter order.

	Editing is cancel + rebuild, so the goods a house is already sending would be
	left on the cancelled invoice: unreceivable (the Receive page works from the
	live order), invisible to the cost tie-out, and sitting under a dead sale.
	They move to the invoice that replaced it, exactly as they move across an
	amend. ``ffl_core`` never re-orders those lines (they are excluded from the
	rebuild's ordering context), so nothing is bought twice.
	"""
	return repoint_orders(old_invoice, new_invoice, "order edited")


def guard_sales_invoice_submit(doc, method=None) -> None:
	"""``Sales Invoice: on_submit`` doc_event.

	A submitted invoice carrying drop-ship lines but NO live Distributor Order is a
	silent hole — the customer has paid for goods nobody ordered. create_order's
	in-transaction hook covers the counter flow, but two doors bypass it: an **amend**
	(the amended invoice submits on its own) and a **hand-built desktop invoice**.

	Recoverable context → rebuild the DRAFT order (never auto-place — v1 is a
	human-confirmed queue) and alert, so the sale stands and an operator sees it.
	Unrecoverable → refuse the submit and point at Create Order.
	"""
	from ffl_integrations.distributor import order_state

	# create_order fires the hook itself immediately after submit; don't race it.
	if doc.flags.get("ffl_counter_distributor_hook_pending"):
		return
	# The carry-forward runs FIRST — before the drop-ship-line check, not just before
	# the existing-order probe. A partial refund that removes the
	# drop-ship line entirely still leaves a real, shipped, already-invoiced order
	# behind; if we returned early on "no drop-ship lines", that order would stay
	# pinned to the CANCELLED invoice and the tie-out would break from the other
	# side. The order belongs to the live invoice FAMILY, whatever lines survive.
	# On an ordinary (non-amended) invoice this is a no-op.
	carry_forward_amended_orders(doc)

	from ffl_integrations.distributor import lanes

	live = frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": "Sales Invoice", "source_name": doc.name,
			"status": ("!=", order_state.CANCELLED)},
		fields=["order_type", "distributor"])
	# Each kind of leg is checked on its own: a carried-forward drop-ship order must
	# not hide a withdrawn procure-to-store one (or the reverse). A to-store row
	# always names its house, so that leg is checked per house.
	stocked_houses = {o.distributor for o in live if lanes.stocks_goods(o.order_type)}
	lines = [] if any(not lanes.stocks_goods(o.order_type) for o in live) else [
		r for r in (doc.items or []) if r.get("delivered_by_supplier")]
	to_store_rows = [r for r in (doc.items or [])
		if (r.get("ffl_to_store_distributor") or "").strip()
		and r.ffl_to_store_distributor.strip() not in stocked_houses]
	if not (lines or to_store_rows):
		return

	context = _recover_counter_context(doc, lines, to_store_rows)
	ffl_counter_order_distributor(doc.name, context)
	notify_admins(
		f"Rebuilt the distributor order for invoice {doc.name}",
		f"{doc.name} was submitted outside Create Order (an amend, or a hand-built "
		f"invoice) with distributor lines and no distributor order. A DRAFT order was "
		f"rebuilt from the invoice and is waiting in the queue — confirm it there. "
		f"Nothing was placed automatically.",
		dedup_key=f"counter_rebuild_{doc.name}")


def guard_sales_invoice_cancel(doc, method=None) -> None:
	"""``Sales Invoice: before_cancel`` doc_event.

	A counter drop-ship invoice has no local disposition / ShipStation record, so the
	ordinary edit guard can't see it. Once its Distributor Order is PLACED at RSR
	there is no cancel API and the goods are en route, so cancelling — or amending,
	which cancels first — must be refused and ops alerted. Orders not yet placed
	(Draft / Queued) are instead withdrawn, so a cancelled sale never leaves a
	confirmable Draft order that could be turned into a real purchase.
	"""
	from ffl_integrations.distributor import cancellation

	dos = frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": "Sales Invoice", "source_name": doc.name},
		fields=["name", "status"])
	if not dos:
		return

	plan = cancellation.classify(dos)
	if (getattr(doc, "flags", None) or {}).get("ffl_edit_carry_forward"):
		# An EDIT cancels this invoice only to rebuild it, and hands every surviving
		# order to the invoice that replaces it (carry_forward_replaced_orders) —
		# nothing is abandoned and nothing is re-ordered, so there is no return to
		# coordinate and no shipped-order alert to raise. Draft/Queued orders are
		# still withdrawn below, as on any cancel; the rebuild re-creates them.
		plan = dict(plan, live=[], shipped=[])
	if plan["live"]:
		# Counter terminal action = REFUSE. The goods are moving and there is no
		# cancel API, so the operator must resolve the return BEFORE the paperwork
		# is undone (the web path, which cannot block a shop-side cancel, alerts).
		cancellation.alert_live(
			plan["live"], source_label="Counter invoice", source_name=doc.name)
		frappe.throw(frappe._(
			"Can't cancel {0}: distributor order(s) {1} are already live at the "
			"distributor and can't be recalled through the API. Coordinate the return "
			"first, then reconcile.").format(doc.name, ", ".join(plan["live"])))

	cancellation.withdraw_orders(
		plan["withdraw"], f"Counter invoice {doc.name} was cancelled.")
	# A Complete leg does NOT block the cancel — but it must not be silent.
	cancellation.alert_shipped(
		plan["shipped"], source_label="counter invoice", source_name=doc.name)
