"""Flow A receive bridge — Distributor Order → Receive Goods pre-fill.

A stocking / procure-to-store order is received into inventory and the A&D book
through the SAME path a manual receive uses: ``ffl_core.api.receive_goods.create_receive``.
Per [DIVERGENCE-3] that path is deliberately STAFF-GATED
(``frappe.only_for(("Stock Manager","System Manager"))``) and A&D acquisition is a
compliance action, so the bridge is a **pre-fill for a human to review and submit**
— it is NEVER a cron auto-submit.

Batch arrivals: an order can ship over several RSR invoices. The receivable qty
for a line is ``invoiced_qty − received_qty`` (what RSR has shipped that we have
not yet booked); each receive bumps ``received_qty`` so re-receiving the same DO
only books the new increment. Firearm serials (RSR-provided) are apportioned to
the current batch and guarded ``len(serials) == qty`` before create_receive's own
serial check ever runs.

Both of create_receive's own line guards are pre-checked here, on the DO, rather
than left to fire at the door: the serial count, and (since 2026-08-08) the unit
cost. This path is server-to-server — the DO's Receive dialog is a read-only
preview with no cost box and no "it was free" checkbox — so a refusal that names
the order line an operator can edit is the only one they can act on, and a
firearm that can be received into neither stock nor the A&D book is a compliance
problem, not a UX one.

**Preconditions a caller must meet** (both refuse with ``frappe.throw``, so the
message reaches the operator rather than depending on a traceback setting):

1. every receivable firearm line carries exactly one RSR serial per unit; and
2. every receivable line carries a non-zero ``unit_cost`` — since 2026-08-08 the
   bridge no longer invents ``0`` for a blank one. A genuinely free distributor
   line cannot be received through this path at all: it has to go through the
   Receive Goods page, where $0 is confirmed with a reason.

Callers therefore see a refusal for a shape that used to be accepted (silently,
at $0). That is the point — the DO line's Unit Cost is editable, the door's
error was not actionable.

Pure helpers (``_receivable_lines`` / ``_assert_serial_counts`` /
``_assert_unit_costs`` / ``_shape_receive_payload``) unit-test without a site.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, get_datetime, now_datetime, nowdate

from ffl_core.firearm import _is_firearm_row
from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction
from ffl_integrations.distributor import lanes

RECEIVE_ROLES = ("Stock Manager", "System Manager")  # mirrors create_receive's own gate

_FIREARM_DETAIL_FIELDS = ("manufacturer", "model_name", "firearm_type", "caliber", "importer")


# ============================================================ pure

def _csv_list(value) -> list:
	if not value:
		return []
	if isinstance(value, (list, tuple)):
		return [str(v).strip() for v in value if str(v).strip()]
	return [s.strip() for s in str(value).split(",") if s.strip()]


def expected_qty(line: dict) -> int:
	"""Units the house said it is sending on this order line: the larger of acked and invoiced
	(a parcel can beat the poll, so an acked line already counts). A killed line sends nothing."""
	if (line.get("line_status") or "") == "Killed":
		return 0
	return max(int(line.get("ack_qty") or 0), int(line.get("invoiced_qty") or 0))


def outstanding_qty(line: dict) -> int:
	"""Units of this order line that will still turn up at the store with nobody
	to take them: what the house said it is sending (``expected_qty``), less what we
	booked, less what the counter already covered from shelf stock
	(``substituted_qty``). Never negative."""
	taken = int(line.get("received_qty") or 0) + int(line.get("substituted_qty") or 0)
	return max(0, expected_qty(line) - taken)


def rank_candidates(rows: list) -> list:
	"""Which open order lines a scanned unit may belong to, in the order they get
	it: earliest-placed order first (FIFO — two customers who ordered the same
	model are served in the order they ordered), then line order within an order.
	Lines with nothing outstanding are dropped; the rest carry ``outstanding``."""
	out = []
	for r in rows or []:
		n = outstanding_qty(r)
		if n > 0:
			out.append({**r, "outstanding": n})
	out.sort(key=lambda r: (r.get("placed_at") is None, str(r.get("placed_at") or ""),
		int(r.get("idx") or 0)))
	return out


def _receivable_lines(line_dicts: list) -> list:
	"""Lines with un-booked shipped qty, annotated with this batch's receive_qty
	and (for firearms) the serials apportioned to this batch."""
	out = []
	for line in line_dicts:
		invoiced = int(line.get("invoiced_qty") or 0)
		received = int(line.get("received_qty") or 0)
		increment = invoiced - received
		if increment <= 0:
			continue
		serials = _csv_list(line.get("serials"))
		# Cumulative serials — take the slice belonging to this batch.
		batch_serials = serials[received:invoiced] if line.get("is_firearm") else []
		out.append({**line, "receive_qty": increment, "receive_serials": batch_serials})
	return out


def _assert_serial_counts(receivable: list) -> None:
	"""Firearm receive requires exactly one serial per unit (create_receive's guard,
	pre-checked here so the DO surfaces the shortfall before opening the page)."""
	for line in receivable:
		if line.get("is_firearm") and len(line["receive_serials"]) != line["receive_qty"]:
			# frappe.throw, not raise: same reason as the "nothing to receive"
			# guard below (:187). A bare exception reaches the operator only if
			# the site happens to be showing tracebacks; a throw is a message.
			frappe.throw(frappe._(
				"SKU {0}: firearm receive needs {1} serial number(s) from the distributor, got {2}. "
				"Receive it on the Receive Goods page and scan the serial off each gun."
			).format(repr(line.get("sku")), line["receive_qty"],
				len(line["receive_serials"])))


def _has_unit_cost(line) -> bool:
	"""Does this DO line actually carry what the goods cost us?

	`unit_cost` is a Currency field, so "never captured" and "free" are the SAME
	stored 0 — the very collapse the receive-side zero-cost guard exists to undo.
	Since the DO cannot tell them apart, a falsy unit_cost is read as UNKNOWN, the
	safe half: an unknown cost is a question a human answers on the order line,
	while a genuinely-free distributor line is rare and can be received from the
	Receive Goods page, where $0 can be confirmed with a reason.
	"""
	try:
		return float(line.get("unit_cost") or 0) > 0
	except (TypeError, ValueError):
		return False


def _assert_unit_costs(receivable: list) -> None:
	"""Every receivable line must know what it cost (create_receive's zero-cost
	guard, pre-checked here for the same reason the serial guard is: the DO is
	where the shortfall can actually be fixed).

	Without this the failure lands at the door instead. `receive_from_do` is a
	server-to-server path — the Distributor Order's Receive dialog is a read-only
	preview, so there is NO checkbox anywhere in it for a receiver to confirm a
	$0 — and a receive that cannot complete is a firearm that entered neither
	stock nor the A&D book while physically standing in the shop. So the refusal
	is moved to the earliest point that names something the operator can change:
	the order line's Unit Cost, which is editable in the grid.
	"""
	missing = [line.get("sku") for line in receivable if not _has_unit_cost(line)]
	if missing:
		# frappe.throw, not raise (see _assert_serial_counts): this message names
		# the exact field the operator has to fill, so it has to actually reach
		# them rather than depend on a site setting that shows tracebacks.
		frappe.throw(frappe._(
			"No unit cost on {0} line(s) of this order: {1}. Enter Unit Cost on those "
			"order lines (it is on the distributor's invoice) and receive again — "
			"stock cannot be booked at a cost nobody knows. If a line really was "
			"free, receive it from the Receive Goods page, where a $0 is confirmed "
			"with a reason."
		).format(len(missing), ", ".join(repr(s) for s in missing)))


def _receive_deltas(receivable: list) -> list:
	"""(line_name, increment) per receivable line — keyed by the DO LINE, never
	aggregated by item_code, so two lines sharing an item_code bump independently."""
	return [(line["line_name"], line["receive_qty"]) for line in receivable]


def procured_serials_by_item(receivable: list) -> dict:
	"""``{item_code: [serial, ...]}`` for the FIREARM lines received in this batch
	(P4c auto-hang).

	These are the real Serial No names create_receive just booked into store stock;
	on a procure-to-store DO with a Woo source they are hung on the waiting web
	order's empty ``to_store`` serial rows (``fulfillment.assign_procured_serials``).
	Accessory lines contribute nothing — a to_store accessory has no serial row to
	fill; its order is simply marked arrived. Two lines sharing an item_code merge,
	mirroring how the auto-hang fills that item_code's rows from one pool.
	"""
	out: dict = {}
	for line in receivable or []:
		if line.get("is_firearm") and line.get("receive_serials"):
			out.setdefault(line.get("item_code"), []).extend(line["receive_serials"])
	return out


def _shape_receive_payload(do_name: str, distributor: dict, receivable: list,
		warehouse: str | None) -> dict:
	"""Build the create_receive payload (strict schema) from receivable lines.
	`distributor_order` is provenance carried onto the receive."""
	items = []
	for line in receivable:
		item = {
			"item_code": line.get("item_code"),
			"qty": line["receive_qty"],
			"serials": line["receive_serials"],
			"service_need": 1 if line.get("service_need") else 0,
		}
		# `rate` is OMITTED, not sent as 0, when the DO line has no cost. The old
		# `float(line.get("unit_cost") or 0)` invented a number: it told
		# create_receive "this gun was free" when what we actually knew was
		# nothing — the same invented-number trap options._local_unit_cost
		# already refuses ("None rather than 0 when nothing is known"). Omitting
		# it lets the receive guard see the true state and say so; _assert_unit_costs
		# normally catches it one step earlier, on the order line.
		# The invoiced figure when the poll has it (what the house actually
		# charged), else the order-line cost — same preference as receive_lines.
		if float(line.get("invoiced_unit_cost") or 0) > 0:
			item["rate"] = float(line["invoiced_unit_cost"])
		elif _has_unit_cost(line):
			item["rate"] = float(line["unit_cost"])
		if line.get("is_firearm"):
			for field in _FIREARM_DETAIL_FIELDS:
				item[field] = line.get(field) or ""
		items.append(item)
	payload = {
		"supplier": distributor.get("supplier"),
		"source": (distributor.get("code") or "").strip() or "Distributor",
		"acquisition_type": "Purchase",
		"distributor_order": do_name,  # provenance → stamped onto FFL Acquisition post-receive
		"items": items,
	}
	if warehouse:
		payload["warehouse"] = warehouse
	return payload


# ============================================================ frappe (staff-gated)

def _receiving_distributor(do):
	"""The house behind this order (or outside-POS invoice: anything with
	``.distributor`` and ``.name``), refused when it carries no Supplier.

	Both booking paths read ``Distributor.supplier`` and create_receive requires
	one, so a blank used to surface at the door as a bare "Supplier is required."
	— about the Receive Goods page's Supplier box, which an ORDER bucket never
	reads (it books against the house, not the page header), leaving the receiver
	staring at a filled-in field the refusal named. Named here instead, on the
	record someone can actually fix. Not a doctype ``reqd``: a Distributor is
	seeded supplier-less on purpose (rsr_seed) and stays that way until go-live.
	"""
	distributor = frappe.get_doc("Distributor", do.distributor)
	if not (distributor.supplier or "").strip():
		frappe.throw(frappe._(
			"Distributor {0} has no Supplier set, so {1} cannot be received. Open the "
			"Distributor record and set Supplier — the vendor these goods are billed "
			"from — then submit again. (The Supplier box on this page applies only to "
			"lines received without an order.)"
		).format(distributor.name, do.name))
	return distributor


def _prepare(do):
	"""Shared prep for both entry points: enrich lines, compute this batch's
	receivable increment, guard firearm serial counts, and shape the create_receive
	payload. Returns (payload, receivable)."""
	if not lanes.stocks_goods(do.order_type):
		frappe.throw(frappe._("{0} is a drop-ship order: the distributor shipped it to the "
			"buyer, so nothing arrives at the store to receive.").format(do.name))
	distributor = _receiving_distributor(do)
	line_dicts = [_line_with_item_meta(line) for line in do.lines]
	receivable = _receivable_lines(line_dicts)
	if not receivable:
		frappe.throw(frappe._("Nothing new to receive on {0} — no shipped-but-unbooked qty.").format(do.name))
	_assert_serial_counts(receivable)
	_assert_unit_costs(receivable)
	payload = _shape_receive_payload(
		do.name, {"code": distributor.code, "supplier": distributor.supplier},
		receivable, warehouse=None)
	return payload, receivable


_INCOMING_ORDER_TYPES = ("Stocking", "Procure To Store")
_INCOMING_DEAD_STATES = ("Draft", "Queued", "Cancelled", "Error")


def _incoming_line_rows(item_code: str | None = None) -> list:
	"""Open store-bound order lines (one dict per DO line, parent fields folded
	in) — the candidate pool every incoming lookup reads."""
	filters = {"parent": ("in", frappe.get_all("Distributor Order",
		filters={"order_type": ("in", _INCOMING_ORDER_TYPES),
			"status": ("not in", _INCOMING_DEAD_STATES)}, pluck="name") or [""])}
	if item_code:
		filters["item_code"] = item_code
	lines = frappe.get_all("Distributor Order Line", filters=filters,
		fields=["name", "parent", "idx", "item_code", "sku", "ack_qty", "invoiced_qty",
			"received_qty", "substituted_qty", "line_status", "unit_cost",
			"invoiced_unit_cost", "serials", "tracking_numbers"])
	if not lines:
		return []
	parents = {p.name: p for p in frappe.get_all("Distributor Order",
		filters={"name": ("in", sorted({l.parent for l in lines}))},
		fields=["name", "distributor", "order_type", "status", "hold_status", "placed_at",
			"source_doctype", "source_name", "web_ref", "receive_status"])}
	out = []
	for l in lines:
		p = parents.get(l.parent)
		if not p:
			continue
		out.append({
			"do_name": p.name, "line_name": l.name, "idx": l.idx, "item_code": l.item_code,
			"sku": l.sku, "distributor": p.distributor, "order_type": p.order_type,
			"status": p.status, "hold_status": p.hold_status or "", "placed_at": p.placed_at,
			"source_doctype": p.source_doctype or "", "source_name": p.source_name or "",
			"web_ref": p.web_ref or "", "receive_status": p.receive_status,
			"ack_qty": l.ack_qty, "invoiced_qty": l.invoiced_qty, "received_qty": l.received_qty,
			"substituted_qty": l.substituted_qty, "line_status": l.line_status,
			"unit_cost": l.unit_cost, "invoiced_unit_cost": l.invoiced_unit_cost,
			"serials": _csv_list(l.serials), "tracking_numbers": _csv_list(l.tracking_numbers),
		})
	return out


def _customer_label(source_doctype: str, source_name: str) -> str:
	"""Who the goods are for, for the receiver's screen. Restock has nobody."""
	if source_doctype == "Sales Invoice" and source_name:
		return frappe.db.get_value("Sales Invoice", source_name, "customer_name") or source_name
	if source_doctype == "Woo Online Order" and source_name:
		return frappe.db.get_value("Woo Online Order", source_name, "customer_name") or source_name
	return ""


def _package_index(rows: list) -> dict:
	"""``{do_name: "n/m"}`` — this order is the n-th of m parcels for its source.

	``m`` counts every placed (non-Draft/Queued/Cancelled/Error) order of the
	source, received ones included — ``rows`` comes from the unfiltered
	``_incoming_line_rows()``, not the outstanding-only candidate list — so the
	number reads "parcel n of m" for the WHOLE order: a receiver seeing "2/2"
	knows this is the last piece and the order is now whole. Tie-break matches
	``rank_candidates``: unplaced sorts last."""
	by_source: dict = {}
	for r in rows:
		key = (r["source_doctype"], r["source_name"])
		if r["source_name"]:
			by_source.setdefault(key, set()).add(
				(r.get("placed_at") is None, str(r.get("placed_at") or ""), r["do_name"]))
	out = {}
	for key, dos in by_source.items():
		ordered = [name for *_, name in sorted(dos)]
		for i, name in enumerate(ordered, start=1):
			out[name] = f"{i}/{len(ordered)}"
	return out


@frappe.whitelist()
def match_incoming(item_code: str) -> dict:
	"""Open store-bound Distributor Order lines a scanned unit of this item could
	belong to, earliest order first.

	The Receive Goods page asks this on every UPC scan: one candidate opens the
	"hold it for this order?" prompt, several list them, none means plain
	restock. Only lines with units still outstanding (``outstanding_qty``) are
	returned, so a line already received or covered from shelf stock never
	claims a parcel.

	Args:
	    item_code: Item code the barcode resolved to.

	Returns:
	    {item_code, candidates: [{do_name, line_name, distributor, supplier,
	    order_type, source_doctype, source_name, customer_name, outstanding,
	    unit_cost, serials, package_index, placed_at}]}.

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	item_code = (item_code or "").strip()
	if not item_code:
		return {"item_code": "", "candidates": []}
	rows = rank_candidates(_incoming_line_rows(item_code))
	# Parcel numbering counts EVERY open order of the source, not just this item's.
	index = _package_index(_incoming_line_rows()) if rows else {}
	suppliers = {d.name: d.supplier for d in frappe.get_all("Distributor",
		filters={"name": ("in", sorted({r["distributor"] for r in rows}) or [""])},
		fields=["name", "supplier"])}
	return {"item_code": item_code, "candidates": [{
		"do_name": r["do_name"], "line_name": r["line_name"], "distributor": r["distributor"],
		"supplier": suppliers.get(r["distributor"]), "order_type": r["order_type"],
		"source_doctype": r["source_doctype"], "source_name": r["source_name"],
		"customer_name": _customer_label(r["source_doctype"], r["source_name"]),
		"outstanding": r["outstanding"],
		"unit_cost": float(r.get("invoiced_unit_cost") or r.get("unit_cost") or 0),
		"serials": r["serials"], "package_index": index.get(r["do_name"], ""),
		"placed_at": r["placed_at"],
	} for r in rows]}


@frappe.whitelist()
def list_incoming() -> dict:
	"""Every store-bound Distributor Order with units still outstanding, grouped
	by the customer order it serves (restock last), for the Receive Goods
	"Incoming shipments" panel.

	Each order carries what the house said it is sending, what we booked, the
	tracking numbers the poll saw, and its lines with per-line outstanding units
	and the serials the house echoed — enough to load a whole customer order or
	one parcel into the receive session and tick units off as they are scanned.
	Restock also lists the outside-POS store invoices (a parcel ordered off-system:
	``do_name`` blank, ``invoice_name`` set, ``lines`` empty until ``stage_invoice``).

	Returns:
	    {groups: [{source_doctype, source_name, customer_name, label,
	    orders: [{do_name, invoice_name, invoice_number, outside_pos, distributor,
	    supplier, customer_name, status, hold_status, web_ref, placed_at,
	    source_doctype, source_name, tracking_numbers, expected, received,
	    outstanding, lines: [{line_name, item_code, item_name, sku, is_firearm,
	    outstanding, unit_cost, serials}]}]}]}.

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	rows = rank_candidates(_incoming_line_rows())
	invoices = _incoming_invoices()
	if not rows and not invoices:
		return {"groups": []}
	codes = sorted({r["item_code"] for r in rows if r["item_code"]})
	items = {i.name: i for i in frappe.get_all("Item", filters={"name": ("in", codes or [""])},
		fields=["name", "item_name", "is_firearm", "item_group"])}
	suppliers = {d.name: d.supplier for d in frappe.get_all("Distributor",
		filters={"name": ("in", sorted({r["distributor"] for r in rows}) or [""])},
		fields=["name", "supplier"])}
	labels: dict = {}
	orders: dict = {}
	for r in rows:
		key = (r["source_doctype"], r["source_name"]) if r["source_name"] else ("", "")
		if key not in labels:
			labels[key] = _customer_label(key[0], key[1])
		o = orders.setdefault(r["do_name"], {
			"do_name": r["do_name"], "distributor": r["distributor"],
			"supplier": suppliers.get(r["distributor"]), "customer_name": labels[key],
			"status": r["status"], "hold_status": r["hold_status"], "web_ref": r["web_ref"],
			"placed_at": r["placed_at"], "source_doctype": key[0], "source_name": key[1],
			"tracking_numbers": [], "expected": 0, "received": 0, "outstanding": 0, "lines": []})
		meta = items.get(r["item_code"])
		o["lines"].append({
			"line_name": r["line_name"], "item_code": r["item_code"],
			"item_name": (meta.item_name if meta else None) or r["sku"], "sku": r["sku"],
			"is_firearm": _is_firearm_row(meta), "outstanding": r["outstanding"],
			"unit_cost": float(r.get("invoiced_unit_cost") or r.get("unit_cost") or 0),
			"serials": r["serials"]})
		o["expected"] += max(int(r["ack_qty"] or 0), int(r["invoiced_qty"] or 0))
		o["received"] += int(r["received_qty"] or 0)
		o["outstanding"] += r["outstanding"]
		for t in r["tracking_numbers"]:
			if t not in o["tracking_numbers"]:
				o["tracking_numbers"].append(t)
	groups: dict = {}
	for o in [*orders.values(), *invoices]:
		key = (o["source_doctype"], o["source_name"])
		g = groups.setdefault(key, {
			"source_doctype": key[0], "source_name": key[1], "customer_name": labels.get(key, ""),
			"label": key[1] or "Restock", "orders": []})
		g["orders"].append(o)
	ordered = sorted(groups.values(), key=lambda g: (g["label"] == "Restock", g["label"]))
	return {"groups": ordered}


@frappe.whitelist()
def build_receive_payload(do_name: str) -> dict:
	"""Pre-fill the Receive Goods page from a Distributor Order (read-only).

	DIVERGENCE-3: reads the DO, enriches lines from their Item, computes
	this batch's receivable increment, and guards firearm serial counts —
	but does NOT submit anything.

	Args:
	    do_name: Distributor Order name.

	Returns:
	    A create_receive-shaped payload pre-filled from the DO.

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	do = frappe.get_doc("Distributor Order", do_name)
	payload, _ = _prepare(do)
	return payload


@frappe.whitelist()
def receive_from_do(do_name: str, payload=None) -> dict:
	"""Receive a Distributor Order: create + submit the Purchase Receipt.

	Staff action from the pre-filled Receive Goods page: creates + submits
	the Purchase Receipt (fires the FastBound acquisition push — with the
	DO stamped onto each acquisition BEFORE the push via the payload's
	`distributor_order` key, M2), bumps received_qty per line on the DO,
	advances receive_status, and idempotently backstops the
	DO→acquisition link.

	Args:
	    do_name: Distributor Order name.
	    payload: Optional edited create_receive payload; defaults to the
	        freshly built pre-fill — dict or JSON string.

	Returns:
	    The receive result (Purchase Receipt name + updated DO state).

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	do = frappe.get_doc("Distributor Order", do_name)
	built_payload, receivable = _prepare(do)
	if payload is None:
		payload = built_payload
	elif isinstance(payload, str):
		payload = frappe.parse_json(payload)

	return _book_batch(do, payload, receivable)


def _book_batch(do, payload: dict, batch: list) -> dict:
	"""Book one receive (Purchase Receipt + A&D), bump the order, hang the goods.

	``batch`` is the receivable-shaped list (line_name, receive_qty, item_code,
	sku, is_firearm, receive_serials …) the bump and the hangs read — the
	whole-order pre-fill for the DO form, or just the scanned units for the
	scan-first page. Shared so the two entry points can't drift on what happens
	after the receipt commits."""
	from ffl_core.api.receive_goods import create_receive
	result = create_receive(payload)

	_bump_received(do, batch)
	# Idempotent fallback: create_receive already stamps distributor_order pre-push
	# from the payload key; re-assert here in case a caller supplied a payload
	# without it (ERPNext-side link only — too late for FastBound, but never wrong).
	_stamp_acquisitions(result.get("name"), do.name)
	# P4c — procure-to-store auto-hang: assign the guns just booked to the waiting
	# web order (clears awaiting_serial so dispose_web_order can run). Runs AFTER the
	# receipt commits, so a hang failure never rolls back a compliance acquisition.
	_maybe_hang_web_order(do, batch)
	_maybe_hang_counter_order(do, batch, result.get("name"))
	return result


_LINE_ITEM_PASSTHROUGH = ("manufacturer", "model_name", "firearm_type", "caliber", "importer",
	"service_need", "zero_cost_confirmed", "zero_cost_reason", "zero_cost_note", "sell_price",
	"is_antique", "year_made", "storage_location")


def _payload_item(it: dict, item_code: str, meta: dict, qty: int, serials: list,
		default_rate) -> dict:
	"""One create_receive item from a scanned line: the receiver's ``rate`` when
	given, else ``default_rate`` (what the distributor billed); a 0 is OMITTED so the
	receive's own zero-cost guard sees the true state. Firearms carry their detail."""
	rate = it.get("rate")
	if rate in (None, ""):
		rate = default_rate
	item = {"item_code": item_code, "qty": qty, "serials": serials}
	if float(rate or 0) > 0:
		item["rate"] = float(rate)
	for key in _LINE_ITEM_PASSTHROUGH:
		if it.get(key) not in (None, ""):
			item[key] = it[key]
	if meta.get("is_firearm"):
		for field in _FIREARM_DETAIL_FIELDS:
			item.setdefault(field, meta.get(field) or "")
	return item


@frappe.whitelist()
def receive_lines(do_name: str, items, warehouse: str | None = None) -> dict:
	"""Receive SOME units of a Distributor Order — the ones a receiver just
	scanned — into stock and the A&D book, and hold them for the waiting order.

	The scan-first Receive Goods page submits one call per order it touched.
	Each item names the order line, the units, and (for a firearm) the serial(s)
	the receiver read off the gun — the distributor's echoed serial is a hint on
	screen, never what gets booked. Cost is the line's invoiced unit cost when the
	poll has it, else the order-line cost; a $0 line must say why, exactly as on
	the page. Over-receiving a line (more than ``outstanding_qty``) is refused.

	Args:
	    do_name: Distributor Order name.
	    items: [{line_name, qty, serials?, rate?, manufacturer?, model_name?,
	        firearm_type?, caliber?, importer?, service_need?, zero_cost_*?}] —
	        list or JSON string.
	    warehouse: Optional target warehouse (defaults like create_receive).

	Returns:
	    create_receive's result ({ok, name, total}) plus ``do_name``.

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	if isinstance(items, str):
		items = frappe.parse_json(items)
	do = frappe.get_doc("Distributor Order", do_name)
	if not lanes.stocks_goods(do.order_type):
		frappe.throw(frappe._("{0} is a drop-ship order: nothing arrives at the store.").format(do.name))
	if do.status in _INCOMING_DEAD_STATES:
		frappe.throw(frappe._("{0} is {1} — nothing is expected from it.").format(do.name, do.status))
	distributor = _receiving_distributor(do)
	by_name = {line.name: line for line in do.lines}
	batch, payload_items = [], []
	taken: dict = {}  # units already claimed on each line by earlier items of THIS call
	for it in items or []:
		line = by_name.get((it.get("line_name") or "").strip())
		if not line:
			frappe.throw(frappe._("{0}: unknown order line {1}.").format(do.name, it.get("line_name")))
		meta = _line_with_item_meta(line)
		qty = int(it.get("qty") or 0)
		room = outstanding_qty(meta) - taken.get(line.name, 0)
		if qty <= 0 or qty > room:
			frappe.throw(frappe._(
				"{0} {1}: {2} unit(s) scanned but only {3} outstanding on this order."
			).format(do.name, line.sku, qty, room))
		serials = _csv_list(it.get("serials"))
		if meta["is_firearm"] and len(serials) != qty:
			frappe.throw(frappe._("{0}: a firearm needs one serial per unit ({1} for {2}).")
				.format(line.sku, len(serials), qty))
		taken[line.name] = taken.get(line.name, 0) + qty
		payload_items.append(_payload_item(it, line.item_code, meta, qty, serials,
			line.invoiced_unit_cost or line.unit_cost))
		batch.append({**meta, "receive_qty": qty, "receive_serials": serials})
	if not batch:
		frappe.throw(frappe._("Nothing to receive."))
	payload = {
		"supplier": distributor.supplier,
		"source": (distributor.code or "").strip() or "Distributor",
		"acquisition_type": "Purchase",
		"distributor_order": do.name,
		"items": payload_items,
	}
	if warehouse:
		payload["warehouse"] = warehouse
	result = _book_batch(do, payload, batch)
	result["do_name"] = do.name
	return result


# ============================================================ outside-POS invoices
#
# A parcel the shop ordered off-system (the distributor's website, the phone) has no
# Distributor Order, but its invoice is mirrored (``distributor.invoices``). One
# that shipped TO THE STORE (``destination == Store``) and carries tracking is
# received from that invoice, through the same create_receive path as an order.
# Drop-ship and Unknown invoices are never receivable, and neither is one that is
# probably a POS order's parcel whose invoice number the status poll has not linked.

INVOICE_DT = "Distributor Invoice"
INVOICE_LINE_DT = "Distributor Invoice Line"
_INVOICE_HEADER_FIELDS = ["name", "distributor", "invoice_number", "po_number", "order_ref",
	"outside_pos", "distributor_order", "destination", "received", "received_receipts"]
_INVOICE_LINE_FIELDS = ["name", "idx", "sku", "description", "qty", "unit_cost", "serials", "upc",
	"item_code", "received_qty"]


def open_order_index() -> dict:
	"""``{distributor: {sku or item_code: DO name}}`` over the open store-bound
	order lines with units still outstanding — the very lines the Incoming panel lists
	as orders. An unmatched invoice carrying one of these is (most likely) that order's
	parcel: RSR invoices name no PO, so it stays unlinked until the status poll
	records the invoice number on the order line. Only houses whose invoices do NOT
	carry our PO / order number (``provider.invoices_carry_po``): for the others an
	unmatched invoice really was placed outside the POS, so it is never blocked here."""
	from ffl_integrations.distributor.api import get_providers

	providers = get_providers()
	blind = {d.name for d in frappe.get_all("Distributor", fields=["name", "provider"])
		if not getattr(providers.get(d.provider), "invoices_carry_po", False)}
	index: dict = {}
	for r in rank_candidates(_incoming_line_rows()):
		if r["distributor"] not in blind:
			continue
		keys = index.setdefault(r["distributor"], {})
		for key in (r["sku"], r["item_code"]):
			if key:
				keys.setdefault(key, r["do_name"])
	return index


def likely_order(index: dict, distributor: str, lines, extra_codes=()) -> str:
	"""The open order (see ``open_order_index``) that shares a SKU or Item with these
	invoice lines, or "". Only lines that still have units to book count (a credit line
	or a fully received one does not claim anything)."""
	keys = index.get(distributor) or {}
	if not keys:
		return ""
	for ln in lines:
		if int(ln.get("qty") or 0) <= 0 or int(ln.get("qty") or 0) - int(ln.get("received_qty") or 0) <= 0:
			continue
		for key in (ln.get("sku"), ln.get("item_code"), *extra_codes):
			if key and key in keys:
				return keys[key]
	return ""


def _incoming_invoices() -> list:
	"""Unreceived Store invoices no POS order matches or (probably) owns, with
	tracking and units still to book, shaped like the order dicts ``list_incoming``
	returns (``do_name`` blank, ``invoice_name`` set, no ``lines`` — ``stage_invoice``
	resolves those).
	# lazy: only invoices first mirrored in the last LOOKBACK_DAYS are offered; one
	# that never gets received (a line the catalog cannot resolve) ages off the list
	# rather than pinning it. Add a "dismiss" action if the shop wants them sooner."""
	from ffl_integrations.distributor.invoices import LOOKBACK_DAYS

	invoices = frappe.get_all(INVOICE_DT, filters={
		"outside_pos": 1, "destination": lanes.DEST_STORE, "received": 0,
		"tracking_numbers": ["is", "set"], "creation": [">=", add_days(nowdate(), -LOOKBACK_DAYS)]},
		fields=["name", "distributor", "invoice_number", "invoice_date", "tracking_numbers"],
		order_by="invoice_date asc, creation asc")
	if not invoices:
		return []
	by_parent: dict = {}
	for ln in frappe.get_all(INVOICE_LINE_DT, filters={
			"parenttype": INVOICE_DT, "parent": ["in", [i.name for i in invoices]]},
			fields=["parent", "sku", "item_code", "qty", "received_qty"]):
		by_parent.setdefault(ln.parent, []).append(ln)
	suppliers = {d.name: d.supplier for d in frappe.get_all("Distributor",
		filters={"name": ("in", sorted({i.distributor for i in invoices}))},
		fields=["name", "supplier"])}
	open_orders = open_order_index()
	out = []
	for inv in invoices:
		lines = by_parent.get(inv.name, [])
		expected = sum(max(0, int(l.qty or 0)) for l in lines)
		received = sum(int(l.received_qty or 0) for l in lines if int(l.qty or 0) > 0)
		outstanding = sum(max(0, int(l.qty or 0) - int(l.received_qty or 0)) for l in lines)
		if outstanding <= 0 or likely_order(open_orders, inv.distributor, lines):
			continue
		out.append({
			"do_name": "", "invoice_name": inv.name, "invoice_number": inv.invoice_number,
			"outside_pos": 1, "distributor": inv.distributor, "supplier": suppliers.get(inv.distributor),
			"customer_name": "", "status": "Invoiced", "hold_status": "", "web_ref": "",
			"placed_at": inv.invoice_date, "source_doctype": "", "source_name": "",
			"tracking_numbers": _csv_list(inv.tracking_numbers), "expected": expected,
			"received": received, "outstanding": outstanding, "lines": []})
	return out


def _needs_serials(meta: dict) -> bool:
	"""create_receive wants a serial per unit of a serial-tracked Item, and a firearm
	is one serial per unit whatever its tracking flag says."""
	return bool(meta.get("has_serial_no") or meta.get("is_firearm"))


def _invoice_state(invoice_name: str, lock: bool):
	"""``(invoice, lines)``. ``lock``: read under row locks — the header and its lines
	— so a second submit of the same parcel waits and then sees what the first booked
	(Frappe's connection is REPEATABLE READ: a plain read after the wait would still
	show the old quantities)."""
	inv = frappe.db.get_value(INVOICE_DT, invoice_name, _INVOICE_HEADER_FIELDS,
		as_dict=True, for_update=lock)
	if not inv:
		frappe.throw(frappe._("Invoice {0} not found.").format(invoice_name))
	lines = frappe.db.get_values(INVOICE_LINE_DT,
		{"parent": invoice_name, "parenttype": INVOICE_DT}, _INVOICE_LINE_FIELDS,
		as_dict=True, order_by="idx asc", for_update=lock)
	return inv, lines


def _assert_receivable(inv, lines, extra_codes=(), index=None) -> None:
	"""Refuse anything that is not a receivable parcel: already received, not shipped
	to the store, belonging to a POS order (linked, or — same house, an open store-bound
	order with units outstanding for one of its SKUs — awaiting the status poll's link).
	Database reads only, so it can run under the lock. ``index``: the request's
	``open_order_index()`` (built once by the caller; built here when omitted)."""
	if inv.received:
		frappe.throw(frappe._("Invoice {0} has already been received.").format(inv.invoice_number))
	if inv.destination != lanes.DEST_STORE:
		frappe.throw(frappe._("Invoice {0} did not ship to the store ({1}): nothing arrives to receive.")
			.format(inv.invoice_number, inv.destination or lanes.DEST_UNKNOWN))
	if not inv.outside_pos or inv.distributor_order:
		frappe.throw(frappe._("Invoice {0} belongs to order {1} — receive it there.")
			.format(inv.invoice_number, inv.distributor_order or ""))
	# A POS order may have claimed it since the last sync (the status poll records
	# invoice numbers on order lines): receiving here as well would double-book.
	from ffl_integrations.distributor.invoices import match_order, order_with_invoice

	order = match_order(inv.distributor, inv, {}) or order_with_invoice(inv.distributor, inv.invoice_number)
	if order:
		frappe.throw(frappe._("Invoice {0} belongs to order {1} — receive it there.")
			.format(inv.invoice_number, order))
	order = likely_order(open_order_index() if index is None else index,
		inv.distributor, lines, extra_codes)
	if order:
		frappe.throw(frappe._(
			"Invoice {0} looks like the parcel for open order {1} (same item, not yet linked by the "
			"status poll) — receive that order instead. If it really is a separate parcel, finish "
			"or cancel the order first.").format(inv.invoice_number, order))


def _locked_invoice(invoice_name: str, extra_codes=(), index=None):
	"""``(invoice, lines)`` under row locks, refusing what is not receivable."""
	inv, lines = _invoice_state(invoice_name, True)
	_assert_receivable(inv, lines, extra_codes, index)
	return inv, lines


def _local_item(sku: str, upc: str) -> str | None:
	"""An Item the shop already has for this product when the house's catalog does not
	answer: the one carrying the UPC as a barcode, or named by the UPC, or by the SKU."""
	from ffl_integrations.rsr.promote import barcode_owner

	for code in (barcode_owner(upc), upc, sku):
		if code and frappe.db.exists("Item", code):
			return code
	return None


def _resolve_line_item(provider, line) -> tuple:
	"""``(item_code, reason)`` from the house's catalog: the row by SKU, else by the
	line's UPC, promoted through the house's own promoter — idempotent, and it stamps
	nothing on the Item (a receipt says nothing about who drop-ships it); a newly made
	Item gets our optimized copy. ``item_code`` None carries the reason. Remote calls
	happen here — never call it while holding the invoice locks."""
	from ffl_integrations.distributor import offers
	from ffl_integrations.distributor.items import _apply_product_copy

	promote = getattr(provider, "promote_to_item", None)
	if not callable(promote):
		return None, "the house cannot promote catalog rows to Items"
	sku, upc = (line.sku or "").strip(), (line.upc or "").strip()
	row = offers.catalog_row(provider, sku=sku, upc=upc or None)
	if not row:
		return None, "not in the house's catalog"
	started = now_datetime()
	item_code = (promote(row.get("sku") or sku, {}) or {}).get("item_code")
	if not item_code:
		return None, "the catalog promotion returned no Item"
	if get_datetime(frappe.db.get_value("Item", item_code, "creation")) >= started:
		_apply_product_copy(item_code)
	return item_code, ""


_CATALOG_UNREADABLE = "the catalog could not be read"


def _free_unmatched(line, reasons) -> bool:
	"""A $0 line the catalog and the shop both answered "no such item" for: a
	distributor's promo freebie that is not in its catalog (a pistol case thrown in
	with the guns). There is nothing to book it as and no cost to account for, so it
	never holds its invoice open. A lookup that failed is not an answer: that line
	still waits. ``reasons``: ``_resolve_lines``' unmatched reasons."""
	reason = reasons.get(line.name)
	return (float(line.unit_cost or 0) <= 0 and not line.item_code and bool(reason)
		and not reason.startswith(_CATALOG_UNREADABLE))


def _resolve_lines(inv, lines, only=None) -> tuple:
	"""Find (or make) the Item of every line that still has units to book and no
	``item_code`` yet: ``({line name: item_code}, {line name: reason})``. The house's
	catalog first, then an Item the shop already has; each line under its own
	savepoint, so one failing line cannot poison the rest. No locks held."""
	provider = None
	resolved, reasons = {}, {}
	for ln in lines:
		if ln.item_code or (only is not None and ln.name not in only):
			continue
		if int(ln.qty or 0) - int(ln.received_qty or 0) <= 0:
			continue
		provider = provider or _invoice_provider(inv)
		frappe.db.savepoint("inv_resolve")
		messages = len(frappe.local.message_log)
		try:
			code, reason = _resolve_line_item(provider, ln)
		except Exception as exc:  # noqa: BLE001 — reported for this line; the others still go
			frappe.db.rollback(save_point="inv_resolve")
			# A swallowed frappe.throw left its message queued for the browser: drop the
			# ones this attempt added, so the page shows only the "unmatched" summary.
			del frappe.local.message_log[messages:]
			frappe.log_error(title=f"Invoice line item lookup failed: {ln.sku}",
				message=frappe.get_traceback())
			code, reason = None, f"{_CATALOG_UNREADABLE} ({type(exc).__name__})"
		code = code or _local_item((ln.sku or "").strip(), (ln.upc or "").strip())
		if code:
			resolved[ln.name] = code
		else:
			reasons[ln.name] = reason
	return resolved, reasons


def _invoice_provider(invoice):
	from ffl_integrations.distributor.api import get_provider

	return get_provider(frappe.get_cached_doc("Distributor", invoice.distributor).provider)


@frappe.whitelist()
def stage_invoice(invoice_name: str) -> dict:
	"""Lay an outside-POS store invoice out for the Receive Goods page: each line's
	local Item (found in the house's catalog by SKU, else UPC, and created through the
	catalog promoter when the shop has none; an existing local Item when the catalog
	has no row), the units still to book, the invoiced unit cost and — for a
	firearm — the serials the distributor sent that are not in the books yet.

	Args:
	    invoice_name: Distributor Invoice name.

	Returns:
	    {invoice_name, invoice_number, distributor, unmatched: [{sku, reason}],
	    lines: [{line_name, item_code, item_name, sku, is_firearm, has_serial_no,
	    outstanding, unit_cost, serials, manufacturer, model_name, firearm_type,
	    caliber}], received} — ``unmatched`` lists lines no Item could be found or
	    made for (left off ``lines``; receive those as plain stock). ``received``:
	    true when the only units left were $0 unmatched lines (a promo freebie), so
	    the invoice was closed here.

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	# Remote catalog calls first, holding nothing; then lock, re-validate, book-keep.
	inv, lines = _invoice_state(invoice_name, False)
	index = open_order_index()  # once per request
	_assert_receivable(inv, lines, index=index)
	resolved, reasons = _resolve_lines(inv, lines)
	inv, lines = _locked_invoice(invoice_name, set(resolved.values()), index)
	_receiving_distributor(inv)
	out, unmatched = [], []
	for ln in lines:
		room = int(ln.qty or 0) - int(ln.received_qty or 0)
		if room <= 0:
			continue
		item_code = ln.item_code or resolved.get(ln.name)
		if not item_code:
			unmatched.append({"sku": ln.sku, "reason": reasons.get(ln.name) or "no matching item"})
			continue
		if item_code != ln.item_code:
			frappe.db.set_value(INVOICE_LINE_DT, ln.name, "item_code", item_code,
				update_modified=False)
		meta = _item_meta(item_code)
		# Same tests as create_receive: the firearm predicate (flag or legacy group)
		# decides the detail fields, has_serial_no the serial count — a firearm is
		# always one serial per unit.
		slots = _needs_serials(meta)
		out.append({
			"line_name": ln.name, "item_code": item_code,
			"item_name": meta.get("item_name") or ln.sku,
			"sku": ln.sku, "is_firearm": bool(meta.get("is_firearm")),
			"has_serial_no": slots, "outstanding": room,
			"unit_cost": float(ln.unit_cost or 0),
			**{f: meta.get(f) or "" for f in _FIREARM_DETAIL_FIELDS if f != "importer"},
			# Serials the distributor sent that are not in the books yet: one already
			# received (typed off the box, in any order) is not offered again.
			"serials": [x for x in _csv_list(ln.serials) if not frappe.db.exists("Serial No", x)][:room]
				if slots else []})
	# Everything left is a freebie nobody can book: nothing arrives to receive.
	received = not out and all(_free_unmatched(ln, reasons) for ln in lines
		if int(ln.qty or 0) - int(ln.received_qty or 0) > 0)
	if received:
		frappe.db.set_value(INVOICE_DT, inv.name, "received", 1)
	return {"invoice_name": inv.name, "invoice_number": inv.invoice_number,
		"distributor": inv.distributor, "unmatched": unmatched, "lines": out, "received": received}


@frappe.whitelist()
def receive_invoice_lines(invoice_name: str, items, warehouse: str | None = None) -> dict:
	"""Receive units of an outside-POS store invoice into stock and the A&D book —
	``receive_lines`` for a parcel that has no Distributor Order.

	The Receive Goods page submits one call per staged invoice. Each item names the
	invoice line, the units and (for a firearm) the serial(s) read off the gun. Cost
	is the invoiced unit cost unless the receiver changed it; a $0 line must say why,
	exactly as on the page. A line cannot be received beyond its invoiced quantity
	less what earlier receives booked, and the invoice is marked received when every
	line is in. Booked against the Distributor's Supplier, Purchase, source = the
	house; the receipt's remarks name the invoice. Refused for an invoice that
	already shipped elsewhere, was received, that a POS order now claims, or that
	looks like the parcel of an open POS order.

	Args:
	    invoice_name: Distributor Invoice name.
	    items: [{line_name, qty, serials?, rate?, manufacturer?, model_name?,
	        firearm_type?, caliber?, importer?, service_need?, sell_price?,
	        is_antique?, year_made?, zero_cost_*?}] — list or JSON string.
	    warehouse: Optional target warehouse (defaults like create_receive).

	Returns:
	    create_receive's result ({ok, name, total}) plus ``invoice_name`` and
	    ``received`` (True once every line is booked).

	Permissions:
	    Receive roles.
	"""
	frappe.only_for(RECEIVE_ROLES)
	if isinstance(items, str):
		items = frappe.parse_json(items)
	# Items the lines still lack are found before the locks are taken (remote calls).
	inv, lines = _invoice_state(invoice_name, False)
	index = open_order_index()  # once per request
	_assert_receivable(inv, lines, index=index)
	wanted = {(it.get("line_name") or "").strip() for it in items or []}
	# The $0 lines with no Item are tried too: one nobody can match does not hold it open.
	free = {ln.name for ln in lines if float(ln.unit_cost or 0) <= 0 and not ln.item_code}
	resolved, reasons = _resolve_lines(inv, lines, only=wanted | free)
	inv, lines = _locked_invoice(invoice_name,
		{code for name, code in resolved.items() if name in wanted}, index)
	distributor = _receiving_distributor(inv)
	by_name = {ln.name: ln for ln in lines}
	taken: dict = {}
	payload_items = []
	for it in items or []:
		line = by_name.get((it.get("line_name") or "").strip())
		if not line:
			frappe.throw(frappe._("{0}: unknown invoice line {1}.").format(inv.invoice_number, it.get("line_name")))
		qty = int(it.get("qty") or 0)
		room = int(line.qty or 0) - int(line.received_qty or 0) - taken.get(line.name, 0)
		if qty <= 0 or qty > room:
			frappe.throw(frappe._(
				"Invoice {0} {1}: {2} unit(s) scanned but only {3} left to receive on it."
			).format(inv.invoice_number, line.sku, qty, max(room, 0)))
		item_code = line.item_code or resolved.get(line.name)
		if not item_code:
			frappe.throw(frappe._(
				"Invoice {0}: no Item could be found or made for SKU {1} ({2}). Receive it as plain "
				"stock instead.").format(inv.invoice_number, line.sku, reasons.get(line.name) or "no match"))
		if item_code != line.item_code:
			frappe.db.set_value(INVOICE_LINE_DT, line.name, "item_code", item_code, update_modified=False)
		meta = _item_meta(item_code)
		serials = _csv_list(it.get("serials"))
		if _needs_serials(meta) and len(serials) != qty:
			frappe.throw(frappe._("{0}: a firearm needs one serial per unit ({1} for {2}).")
				.format(line.sku, len(serials), qty))
		payload_items.append(_payload_item(it, item_code, meta, qty, serials, line.unit_cost))
		taken[line.name] = taken.get(line.name, 0) + qty
	if not payload_items:
		frappe.throw(frappe._("Nothing to receive."))
	payload = {
		"supplier": distributor.supplier,
		"source": (distributor.code or "").strip() or "Distributor",
		"acquisition_type": "Purchase",
		"notes": f"{distributor.name} invoice {inv.invoice_number}",
		"items": payload_items,
	}
	if warehouse:
		payload["warehouse"] = warehouse
	from ffl_core.api.receive_goods import create_receive
	result = create_receive(payload)

	for name, n in taken.items():
		frappe.db.set_value(INVOICE_LINE_DT, name, "received_qty",
			int(by_name[name].received_qty or 0) + n, update_modified=False)
	done = all(int(ln.received_qty or 0) + taken.get(ln.name, 0) >= int(ln.qty or 0)
		for ln in lines if int(ln.qty or 0) > 0 and not _free_unmatched(ln, reasons))
	frappe.db.set_value(INVOICE_DT, inv.name, {
		"received": 1 if done else 0,
		"received_receipts": ", ".join(filter(None, [inv.received_receipts, result.get("name")]))})
	safe_commit()
	result["invoice_name"] = inv.name
	result["received"] = done
	return result


def _maybe_hang_web_order(do, receivable: list) -> None:
	"""On a Woo-sourced **Procure To Store** receive, hang the received firearm
	serials onto the waiting web order's empty ``to_store`` rows (P4c C2).

	Gated three ways so nothing else is touched:

	* only ``Procure To Store`` — a Stocking receive has no web order to hang onto;
	* only a ``Woo Online Order`` source — a **counter** (Sales Invoice) source has
	  its own bridge (:func:`_maybe_hang_counter_order`), and a POS-only site has no
	  Woo Online Order at all, so ``ffl_woo_sync`` is never reached through the
	  late-bound call;
	* only when the batch actually booked firearm serials — an accessory-only batch
	  has nothing to hang; its order is marked arrived by the queue's own derivation.

	A hang failure is alerted, not raised: the Purchase Receipt + FFL Acquisition
	already committed, and undoing a bound-book acquisition because a web-order write
	failed would be far worse than a serial a human assigns by hand from the WC Edit
	Order screen (the fallback path that already exists).
	"""
	if lanes.mode_of(do.order_type) != lanes.TO_STORE:
		return
	if (do.source_doctype or "") != "Woo Online Order" or not do.source_name:
		return
	procured = procured_serials_by_item(receivable)
	if not procured:
		return
	try:
		assign = frappe.get_attr(
			"ffl_woo_sync.woocommerce.fulfillment.assign_procured_serials")
		assign(do.source_name, procured)
	except Exception:  # noqa: BLE001 — see docstring: never undo the acquisition
		from ffl_integrations.notifications import notify_admins

		notify_admins(
			f"Procure-to-store auto-hang failed: {do.name}",
			f"{do.name} was received into stock, but assigning the serial(s) to web "
			f"order {do.source_name} failed. Assign them by hand on the WooCommerce "
			f"Edit Order screen.\n\n{frappe.get_traceback()}",
			dedup_key=f"dist_hang_{do.name}")


def _booked_firearm_serials(pr_name: str | None) -> dict:
	"""``{item_code: [serial, ...]}`` for the firearms the Purchase Receipt actually
	booked — the receive's truth, not the Distributor Order's pre-fill (a caller may
	submit a corrected payload)."""
	from ffl_core.firearm import is_firearm_item
	from ffl_core.serials import serials_for_line

	out: dict = {}
	if not pr_name:
		return out
	for row in frappe.get_all("Purchase Receipt Item", filters={"parent": pr_name},
			fields=["item_code", "serial_no", "serial_and_batch_bundle"]):
		if is_firearm_item(row.item_code):
			out.setdefault(row.item_code, []).extend(serials_for_line(row))
	return out


def _maybe_hang_counter_order(do, receivable: list, pr_name: str | None = None) -> None:
	"""On a counter (Sales Invoice) **Procure To Store** receive, hold what arrived
	for the customer who bought it.

	Firearm serials are written onto the order's awaiting ship-to-store lines
	(``manual_order.assign_procured_serials``) — that is the counter hold, and the
	gun turns up on the Pending Order card ready to Dispose. Every arrival (guns and
	accessories) is noted on the invoice so the order's timeline says it is in.

	Same failure rule as the web bridge: the Purchase Receipt and FFL Acquisition
	already committed, so a hang problem alerts and never undoes the receive. A
	cancelled invoice gets nothing hung — the guns are simply store stock — and ops
	are told, since goods arrived for a sale that no longer exists.
	"""
	if lanes.mode_of(do.order_type) != lanes.TO_STORE:
		return
	if (do.source_doctype or "") != "Sales Invoice" or not do.source_name:
		return
	from ffl_integrations.notifications import notify_admins

	try:
		from ffl_core.api.manual_order import assign_procured_serials

		result = assign_procured_serials(do.source_name, _booked_firearm_serials(pr_name),
			distributor=do.distributor)
		if result.get("skipped"):
			notify_admins(
				f"Ship-to-store goods arrived for a closed order: {do.name}",
				f"{do.name} was received into stock, but counter order {do.source_name} "
				f"is {result['skipped']} — nothing was held. The goods are ordinary store "
				f"stock now; sort out the customer by hand.",
				dedup_key=f"dist_hang_counter_{do.name}")
			return
		arrived = ", ".join(
			f"{line.get('item_code') or line.get('sku')} ×{line['receive_qty']}" for line in receivable)
		held = result.get("assigned") or []
		note = f"Ship to store {do.name} arrived: {arrived}."
		if held:
			note += f" Held for this order: serial {', '.join(held)}."
		frappe.get_doc("Sales Invoice", do.source_name).add_comment("Comment", note)
		_maybe_open_pickup_ticket(do, held)
	except Exception:  # noqa: BLE001 — never undo the acquisition (see docstring)
		notify_admins(
			f"Ship-to-store hold failed: {do.name}",
			f"{do.name} was received into stock, but holding it for counter order "
			f"{do.source_name} failed. Assign the serial on the order by hand.\n\n"
			f"{frappe.get_traceback()}",
			dedup_key=f"dist_hang_counter_{do.name}")


def _maybe_open_pickup_ticket(do, held: list) -> None:
	"""A counter PICKUP whose gun just arrived: open its 4473 ticket (a $0 draft
	POS Invoice, ffl_core.api.counter_pickup) so the customer can be rung
	through when they walk in. Only when a register is open — otherwise the
	queue's Pickup button opens it later; the hold above already protects the
	gun. Same failure rule as the hang: alert, never undo the receive."""
	if not held:
		return
	try:
		from ffl_core.api.counter_pickup import maybe_open_pickup_invoice

		ticket = maybe_open_pickup_invoice(do.source_name)
		if ticket:
			frappe.msgprint(frappe._("Counter pickup ticket {0} opened for {1}.").format(
				ticket, do.source_name), alert=True)
	except Exception:  # noqa: BLE001 — the gun is held; the ticket can be opened from the queue
		from ffl_integrations.notifications import notify_admins

		notify_admins(
			f"Counter pickup ticket failed: {do.name}",
			f"{do.name} was received and held for counter order {do.source_name}, but "
			f"opening its pickup POS Invoice failed. Press Pickup on the Pending Order "
			f"page instead.\n\n{frappe.get_traceback()}",
			dedup_key=f"dist_pickup_ticket_{do.name}")


def _item_meta(item_code) -> dict:
	"""The firearm classification / detail a receive needs, from the Item."""
	if not item_code:
		return {}
	row = frappe.db.get_value(
		"Item", item_code,
		["item_name", "is_firearm", "item_group", "has_serial_no", "manufacturer", "model_name",
			"firearm_type", "caliber"],
		as_dict=True)
	if not row:
		return {}
	# The firearm test create_receive applies (the flag, or a legacy firearm Item Group):
	# a gun the flag missed still needs its serial and detail fields at the door.
	row["is_firearm"] = _is_firearm_row(row)
	return row


def _line_with_item_meta(line) -> dict:
	"""DO line + the firearm classification / detail the receive needs, from Item."""
	meta = _item_meta(line.item_code)
	return {
		"line_name": line.name,
		"sku": line.sku,
		"item_code": line.item_code,
		"line_status": line.line_status or "Pending",
		"wish_qty": line.wish_qty,
		"ack_qty": line.ack_qty or 0,
		"invoiced_qty": line.invoiced_qty or 0,
		"received_qty": line.received_qty or 0,
		"substituted_qty": line.substituted_qty or 0,
		"serials": line.serials or "",
		"unit_cost": line.unit_cost or 0,
		"invoiced_unit_cost": line.invoiced_unit_cost or 0,
		"description": line.description or "",
		"is_firearm": bool(meta.get("is_firearm")),
		# Fresh RSR restock stock is not flagged for gunsmith service; staff toggle
		# per-serial later if needed. (ffl_service_need lives on Purchase Receipt
		# Item / Serial No, not Item — so it is never read here.)
		"service_need": False,
		"manufacturer": meta.get("manufacturer") or "",
		"model_name": meta.get("model_name") or "",
		"firearm_type": meta.get("firearm_type") or "",
		"caliber": meta.get("caliber") or "",
		# Never the Item's importer (2026-08-20): an import marking is a per-gun
		# fact, and this automated path has no operator to catch a wrong one —
		# a restocked gun books without an importer rather than with the SKU's.
		"importer": "",
	}


def _bump_received(do, receivable: list) -> None:
	"""Add this batch's increment to each DO line, keyed by the child-row name so
	two lines sharing an item_code bump independently."""
	deltas: dict = {}
	for name, delta in _receive_deltas(receivable):  # a line may appear in several items
		deltas[name] = deltas.get(name, 0) + delta
	for line in do.lines:
		delta = deltas.get(line.name)
		if delta:
			line.received_qty = (line.received_qty or 0) + delta
	# Received when every line is fully booked OR Killed — a killed line will never
	# arrive, so it must not pin receive_status at Partial forever.
	fully = all(
		(line.received_qty or 0) >= (line.wish_qty or 0) or line.line_status == "Killed"
		for line in do.lines)
	do.receive_status = "Received" if fully else "Partial"
	do.save(ignore_permissions=True)
	safe_commit()


def _stamp_acquisitions(pr_name: str, do_name: str) -> None:
	"""Link each FFL Acquisition created by this receive back to the DO (provenance).
	The `distributor_order` field is an ffl_integrations custom field on FFL
	Acquisition; skip silently if the receive produced no firearm acquisitions."""
	if not pr_name:
		return
	for acq in frappe.get_all("FFL Acquisition", filters={"purchase_receipt": pr_name}, pluck="name"):
		frappe.db.set_value("FFL Acquisition", acq, "distributor_order", do_name)
