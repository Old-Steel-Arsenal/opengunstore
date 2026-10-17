"""Telling GunBroker the parcel has gone.

The store buys this label itself (unlike a web order, where WooCommerce's own
plugin buys it), so the tracking number lives in ShipStation and has to be
carried back. Three calls, always in this order, and none of them optional:

1. ``PUT /Orders/{id}/Flags {"OrderShipped": true}`` — GunBroker support was
   explicit that uploading tracking does NOT set this flag, and until it is set
   the buyer's order still reads as unshipped.
2. ``PUT /Orders/{id}/Shipping`` with the tracking number, and the carrier code
   when we have one (it is what makes GunBroker render a working tracking link).
3. ``GET /Orders/{id}`` to read it back. A 200 from a PUT is not evidence:
   GunBroker accepts and silently ignores fields it does not want, which is how
   a listing update can succeed and change nothing.

Flag first because of what each ordering leaves behind when step 2 fails. Doing
shipping first leaves "a tracking number on an order that says it has not
shipped" — invisible to the buyer and with no obvious thing to retry. Doing the
flag first leaves "shipped, tracking to follow", which the buyer can see and
which names its own next step.

None of the three goes out before the store has actually bought a label. An
order reaches ``Transferred`` when its bound-book exit is booked, which is
normally a while before the parcel leaves the bench, and ``OrderShipped`` is
what the buyer reads as "it is on its way". Both the selection query and
:func:`push_tracking` itself check for the shipment, because the manual button
does not go through the query.

Each step records its own timestamp, so a retry sends only what is missing. All
three are idempotent (a flag already true is a no-op; the same tracking number
twice is the same tracking number), so re-sending is safe — the timestamps are
about not being noisy, not about correctness.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from ffl_core.utils import commit as safe_commit
from ffl_core.utils import single_flag
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerError,
	get_field,
	kv_key,
)
from ffl_integrations.gunbroker.orders import GB_STATUS_LABELS
from ffl_integrations.integration_gate import gunbroker_enabled
from ffl_integrations.notifications import notify_admins, safe_log_error

ORDER_DOCTYPE = "GunBroker Order"
SETTINGS_DOCTYPE = "GunBroker Settings"

# GunBroker's carrier enumeration. Everything else is sent WITHOUT a Carrier
# key — not a zero, not an empty string, and not a guess. The field is optional;
# an unmapped carrier still gets its tracking number across, just without the
# clickable link, and that is a note in the log rather than something to hold a
# shipment for.
CARRIER_CODES: dict[str, int] = {
	"fedex": 1,
	"ups": 2,
	"usps": 3,
	"stamps_com": 3,      # ShipStation's USPS reseller
	"stamps.com": 3,
	"endicia": 3,         # ditto, older accounts
}
CARRIER_LABELS = {0: "Unknown", 1: "FedEx", 2: "UPS", 3: "USPS"}

# How often poll_gb_tracking runs, and how many rounds of a read-back that will
# not agree are tolerated before the tracking upload is sent again.
POLL_INTERVAL_MINUTES = 10
READBACK_RETRY_ROUNDS = 3

# Stable across ticks on purpose: it is what stops a slow round having a second
# one queued behind it.
TRACKING_JOB_ID = "gb-tracking-writeback"

# Written onto sync_error whenever step 2 goes out a second time.
RESENT_NOTE = "tracking upload re-sent after the read-back kept disagreeing"

# What GunBroker is expected to move the order to once tracking lands. Read
# back and recorded, never asserted on — see push_tracking.
COMPLETE_STATUS = 5


# ---------------------------------------------------------------------------
# Scheduled poll
# ---------------------------------------------------------------------------

def poll_gb_tracking() -> dict:
	"""Cron entry (every 10 minutes). Gate, then enqueue; never raises.

	Nothing outbound happens on the tick. A round is a ShipStation read plus up
	to three GunBroker calls per order, and a Cron scheduled job runs on the
	DEFAULT queue with its 300-second budget — long enough to be cut off part
	way through, and with no resume point when it is. It goes on the long queue
	instead, deduplicated on a stable job_id so a slow round cannot have a
	second one stacked on top of it: two rounds in flight together would each
	PUT tracking for the same order.

	A scheduler tick that throws takes the whole tick with it, including the
	other integrations' jobs, so every branch out of here is a return.
	"""
	summary = {"enqueued": False}
	try:
		if not gunbroker_enabled():
			return summary
		if not single_flag(SETTINGS_DOCTYPE, "auto_sync_enabled", default=1):
			return summary
		frappe.enqueue(
			"ffl_integrations.gunbroker.shipping._run_tracking_writeback",
			queue="long", job_id=TRACKING_JOB_ID, deduplicate=True)
		summary["enqueued"] = True
	except Exception:  # noqa: BLE001 — a gate must not break the tick
		safe_log_error("GunBroker tracking write-back could not be enqueued",
			frappe.get_traceback())
	return summary


def _run_tracking_writeback() -> dict:
	"""One round: send whichever steps each selected order still owes.

	Self-throttling: an order is only selected while some step is outstanding
	AND its invoice has a ShipStation shipment, so a store with nothing
	shipping does no work and makes no calls.
	"""
	summary = {"considered": 0, "pushed": 0, "failed": 0}
	try:
		names = pending_writeback_orders()
		summary["considered"] = len(names)
		for name in names:
			try:
				result = push_tracking(name)
				if result.get("ok"):
					summary["pushed"] += 1
			except Exception:  # noqa: BLE001 — one order must not stop the round
				summary["failed"] += 1
				safe_log_error(f"GunBroker tracking write-back failed for {name}",
					frappe.get_traceback())
		safe_commit()
	except Exception:  # noqa: BLE001 — a background job must not die noisily
		safe_log_error("GunBroker tracking poll failed", frappe.get_traceback())
	return summary


def pending_writeback_orders() -> list[str]:
	"""Transferred orders, shipment raised, with a step still outstanding.

	The shipment is half the predicate and not a detail. ``Transferred`` means
	the bound-book exit is booked, NOT that the parcel has left — that is what
	the ``awaiting_ship`` stage exists to say, and the gun is normally still on
	the packing bench at that point. A ShipStation shipment against the invoice
	is the first evidence anybody has started posting it, so an order without
	one is not a candidate: selecting on ``Transferred`` alone told the buyer
	"shipped, no tracking" within minutes of the disposition.

	The three step conditions are then ORed, and leaving any of them out
	strands a real case: an order whose flag went through but whose tracking
	upload failed has a ``shipping_flag_pushed_at`` and would never be selected
	again by a query that only looks for orders missing that.
	"""
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"order_status": "Transferred", "sales_invoice": ("is", "set")},
		fields=["name", "sales_invoice", "shipping_flag_pushed_at",
			"tracking_pushed_at", "gb_ship_writeback_pending"])
	outstanding = [
		row for row in rows
		if not row.shipping_flag_pushed_at
		or not row.tracking_pushed_at
		or cint(row.gb_ship_writeback_pending)
	]
	shipped = _invoices_with_a_shipment(
		[row.sales_invoice for row in outstanding if row.sales_invoice])
	return [row.name for row in outstanding if row.sales_invoice in shipped]


def _invoices_with_a_shipment(invoices: list[str]) -> set[str]:
	"""Which of these invoices ShipStation has been given a shipment for.

	A second query rather than a join: the shipment id lives on the Sales
	Invoice and ``frappe.get_all`` cannot filter on a link target's field.
	"""
	if not invoices:
		return set()
	return set(frappe.get_all(
		"Sales Invoice",
		filters={"name": ("in", invoices), "shipstation_order_id": ("is", "set")},
		pluck="name"))


# ---------------------------------------------------------------------------
# One order
# ---------------------------------------------------------------------------

def push_tracking(gunbroker_order: str,
		client: GunBrokerClient | None = None) -> dict:
	"""Send whichever of the three steps this order still owes.

	Returns ``{ok, steps, tracking_number, carrier, warnings}``, or
	``{"ok": False, "reason": …}`` when there is nothing to send yet (no live
	label bought, no shipment on the invoice).
	"""
	order = frappe.get_doc(ORDER_DOCTYPE, gunbroker_order)
	owed = steps_owed(order)
	pending = cint(order.gb_ship_writeback_pending)
	if not owed and not pending:
		return {"ok": True, "steps": [], "reason": "nothing outstanding"}

	warnings: list[str] = []
	# A step 2 that has already landed once and is going out again because the
	# read-back would not agree — as opposed to one that has never been sent.
	resent = "shipping" in owed and bool(order.tracking_pushed_at)
	tracking = (order.tracking_number or "").strip()
	carrier = _stored_carrier(order)
	if "shipping" in owed or not tracking:
		found = _fetch_label(order)
		if not found:
			# Nothing goes out, not even the flag. OrderShipped is what the
			# buyer reads as "it is on its way", and a bought label is the
			# earliest fact that makes that true: an order can sit Transferred
			# for a day with the gun still on the bench. This is the second of
			# the two guards — pending_writeback_orders will not select such an
			# order either — because the first one is a query and this one is
			# the call site, and push_tracking_now reaches it directly.
			return {"ok": False, "steps": [],
				"reason": "no live ShipStation label yet"}
		tracking, carrier_code, warning = found
		carrier = CARRIER_CODES.get(carrier_code.strip().lower())
		if warning:
			warnings.append(warning)

	client = client or GunBrokerClient.from_settings()
	sent: list[str] = []

	if "flag" in owed:
		client.set_order_flags(order.gb_order_id, OrderShipped=True)
		frappe.db.set_value(ORDER_DOCTYPE, order.name, "shipping_flag_pushed_at",
			now_datetime(), update_modified=False)
		order.shipping_flag_pushed_at = now_datetime()
		sent.append("flag")

	if "shipping" in owed:
		if resent:
			# Without it the order looks the same after a first
			# upload and after a fourth — same timestamp, same pending flag —
			# and nobody reading it can tell whether re-sending has been tried.
			warnings.append(RESENT_NOTE)
		try:
			client.set_order_shipping(order.gb_order_id, tracking, carrier)
		except GunBrokerError as exc:
			# Recorded and returned, never raised. A throw would roll back the
			# transaction and take the pending flag and the reason with it — the
			# operator would see the error once and the order would forget it.
			_mark_pending(order, f"tracking upload failed: {exc}", warnings)
			return {"ok": False, "steps": sent, "tracking_number": tracking,
				"warnings": warnings, "detail": str(exc)}
		frappe.db.set_value(ORDER_DOCTYPE, order.name, {
			"tracking_number": tracking,
			"ship_carrier": CARRIER_LABELS.get(carrier or 0, "Unknown"),
			"tracking_pushed_at": now_datetime(),
		}, update_modified=False)
		order.tracking_number = tracking
		order.tracking_pushed_at = now_datetime()
		sent.append("shipping")

	agreed, detail, status = _read_back(client, order, tracking, carrier)
	sent.append("readback")
	if agreed:
		if status and status != COMPLETE_STATUS:
			# Recorded, never asserted on. The test plan expects GunBroker to move
			# the order from 4 Pending Shipment to 5 Order Complete once the
			# tracking lands, and their status machine is theirs: it has been
			# seen to lag. Failing a write-back that GunBroker confirmed
			# storing, because a number somewhere else has not caught up yet,
			# would put the order back in the retry set and mail the store
			# about a buyer who can already see their tracking. So the reading
			# is kept where a human and QA can see it, and nothing turns on it.
			warnings.append(f"GunBroker still reports status {status} "
				f"({GB_STATUS_LABELS.get(status, '?')}) after the write-back")
		_clear_pending(order, warnings)
	else:
		_mark_pending(order, detail, warnings)
	return {"ok": agreed, "steps": sent, "tracking_number": tracking,
		"carrier": carrier, "warnings": warnings, "detail": detail,
		"gb_status": status}


def _stored_carrier(order) -> int | None:
	"""The carrier code we last sent, back from the label we stored it under."""
	label = (order.ship_carrier or "").strip()
	for code, name in CARRIER_LABELS.items():
		if code and name == label:
			return code
	return None


def steps_owed(order) -> list[str]:
	"""Which of flag / shipping this order still owes, per the retry rules.

	Never re-sends a step that has already landed, with one exception: when the
	read-back has disagreed for three rounds running, the tracking upload goes
	out again. It is idempotent, and by then the likeliest explanation is that
	GunBroker took the 200 and dropped the value.
	"""
	owed = []
	if not order.shipping_flag_pushed_at:
		owed.append("flag")
	if not order.tracking_pushed_at:
		owed.append("shipping")
	elif cint(order.gb_ship_writeback_pending) and _readback_stuck(order):
		owed.append("shipping")
	return owed


def _readback_stuck(order) -> bool:
	"""Has the read-back been disagreeing for three poll rounds?

	Measured as elapsed time rather than counted, because there is nowhere to
	keep a counter that survives a worker dying mid-round — and a counter that
	silently resets would mean the resend never happens. Three rounds of a
	ten-minute poll is half an hour; on a poll running to schedule the two
	readings are the same, and when it is not running to schedule elapsed time
	is the more honest of the two.
	"""
	deadline = add_to_date(order.tracking_pushed_at,
		minutes=POLL_INTERVAL_MINUTES * READBACK_RETRY_ROUNDS)
	return now_datetime() >= deadline


def _fetch_label(order) -> tuple[str, str, str] | None:
	"""(tracking, carrier_code, warning) off the first live label, or None.

	Voided labels are dropped by the client, so a reprint can never win over the
	label actually on the box.
	"""
	from ffl_integrations.shipstation.client import (
		ShipStationClient, ShipStationConfigError, ShipStationError)

	shipment_id = frappe.db.get_value("Sales Invoice", order.sales_invoice,
		"shipstation_order_id")
	if not shipment_id:
		return None
	try:
		labels = ShipStationClient.from_settings().labels_for_shipment(shipment_id)
	except (ShipStationConfigError, ShipStationError):
		safe_log_error(f"GunBroker tracking: ShipStation unavailable for {order.name}",
			frappe.get_traceback())
		return None
	for label in labels:
		tracking = (label.get("tracking_number") or "").strip()
		if not tracking:
			continue
		code = (label.get("carrier_code") or "").strip()
		warning = ""
		if code and code.lower() not in CARRIER_CODES:
			warning = f"carrier not mapped: {code}"
		return tracking, code, warning
	return None


def _read_back(client: GunBrokerClient, order, tracking: str,
		carrier: int | None) -> tuple[bool, str, int]:
	"""Confirm GunBroker really stored what we sent. (agreed, detail, status).

	The status comes back as a third value rather than only inside the detail
	string: the caller records it, and a caller that has to parse prose to find
	a number will eventually parse it wrong.
	"""
	try:
		remote = client.get_order(order.gb_order_id) or {}
	except GunBrokerError as exc:
		return False, f"read-back failed: {exc}", 0
	stored = str(get_field(remote, "shipTrackingNumber", "ShipTrackingNumber",
		default="") or "").strip()
	status = cint(kv_key(get_field(remote, "status", "orderStatus"), 0))
	if stored != tracking:
		return False, (f"GunBroker reports tracking {stored or '(none)'}, "
			f"we sent {tracking}"), status
	if carrier:
		# Only when we actually sent one. An order we deliberately sent no
		# Carrier for reads back 0/Unknown, and asserting on that would fail
		# every DHL shipment for doing exactly what it was told to do.
		stored_carrier = cint(get_field(remote, "shipCarrier", "ShipCarrier",
			default=0))
		if stored_carrier != carrier:
			return False, (f"GunBroker reports carrier {stored_carrier}, "
				f"we sent {carrier}"), status
	detail = f"status {status}"
	ship_date = get_field(remote, "shipDate", "shipDateUTC")
	if ship_date:
		detail += f", ship date {ship_date}"
	return True, detail, status


def _mark_pending(order, detail: str, warnings: list[str] | None = None) -> None:
	"""The tracking number did not land. Say so, once a day, and keep trying."""
	note = "; ".join([str(detail)] + [w for w in (warnings or []) if w])
	frappe.db.set_value(ORDER_DOCTYPE, order.name, {
		"gb_ship_writeback_pending": 1,
		"sync_error": note[:1000],
	}, update_modified=False)
	notify_admins(
		_("GunBroker order {0}: the buyer has no tracking number").format(
			order.gb_order_id),
		_("{0} ({1}) shipped, but GunBroker did not take the tracking number: "
			"{2}\n\nPOS keeps retrying every ten minutes. If it does not clear, "
			"enter the number on the GunBroker order by hand — the buyer is "
			"waiting on it."
		).format(order.name, order.gb_order_id, detail),
		dedup_key=f"gb_ship_writeback_{order.gb_order_id}", window_minutes=1440)


def _clear_pending(order, warnings: list[str]) -> None:
	frappe.db.set_value(ORDER_DOCTYPE, order.name, {
		"gb_ship_writeback_pending": 0,
		"sync_error": "; ".join(warnings)[:1000],
	}, update_modified=False)
