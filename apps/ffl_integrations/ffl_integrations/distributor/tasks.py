"""Distributor order polling + placement background work.

PURE core (harness-tested, no DB): ``compute_do_updates`` folds a normalised
provider order-status onto DO lines and derives the order status;
``_next_interval`` / ``_is_stale`` / ``_hold_escalation_level`` are the poll
backoff + watchdog predicates.

FRAPPE gates (make test): the ``*/30`` cron gate only enqueues live orders whose
``next_poll_at`` is due; per-order poll writes back lines + status + the next
backed-off poll time; the placement worker places a Queued order idempotently.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds

from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction
from ffl_integrations.distributor import dropship_hooks, lanes, order_state
from ffl_integrations.distributor.queue import enqueue_distributor
from ffl_integrations.notifications import notify_admins, safe_log_error

POLL_START_SECONDS = 30 * 60
POLL_MAX_SECONDS = 2 * 3600
STALE_SECONDS = 14 * 86400
# Supply-side watchdog. RSR
# refreshes quantity roughly every 5 min and our gate runs */15, so a whole hour
# without a heartbeat means the feed is genuinely broken, not between ticks.
# 90 min — deliberately LONGER than the hourly quantity feed by two watchdog
# ticks of margin. At exactly 60 min the watchdog raced the hourly sync inside
# the same */15 cron tick: both jobs land ~3 min after the tick, the heartbeat
# age reads 3600s ± milliseconds, and whenever the watchdog's read beat the
# sync's write it alerted (3× on 2026-07-29/30, every alert stamped the same
# second as the successful sync). The threshold must exceed the sync interval
# plus at least one tick plus queue jitter. Pinned by a test.
FEED_STALE_SECONDS = 90 * 60
# Catalog CONTENT (price/description) ingest watchdog — a SEPARATE heartbeat
# from the quantity feed above (RSR's catalog refresh is ~2h, much coarser than
# the 5-min quantity poll). 6h gives 3 refresh cycles of margin before alerting.
# Alert-only: content drift is a "prices may be stale" ops signal, never a
# reason to zero published stock (that stays the quantity feed's interlock).
CATALOG_CONTENT_STALE_SECONDS = 6 * 3600


def content_stale_seconds(provider) -> int:
	"""A house whose content feed is daily by design (Sports South's
	DailyItemUpdate) overrides the RSR-paced default on its provider class."""
	value = getattr(provider, "catalog_content_stale_seconds", None)
	# isinstance, not truthiness: a Mock provider in tests answers every attr.
	return int(value) if isinstance(value, (int, float)) and value > 0 else CATALOG_CONTENT_STALE_SECONDS
# feed_stale_verdict reasons. Only AGED means "we had a heartbeat and it went
# cold"; NEVER_SYNCED is a cold start, which alerts but must not zero stock (F1).
STALE_REASON_AGED = "stale"
STALE_REASON_NEVER = "never_synced"
STALE_REASON_FRESH = "fresh"
HOLD_DAY3_SECONDS = 3 * 86400
HOLD_DAY45_SECONDS = int(4.5 * 86400)

SHIPPED_LINE_STATES = {"Shipped", "Received"}
SETTLED_LINE_STATES = {"Shipped", "Received", "Killed"}
# TODO(go-live): confirm the exact RSR ItemStatus "killed/cancelled" markers
# against the Direct Connect manual (main-session only). Placeholder set.
KILLED_ITEM_STATUSES = {"KILLED", "CANCELLED", "K", "X"}


# ============================================ pure: completion / recorded serials

# Lanes whose serial numbers are a RECORD ONLY. An FDS gun ships from the
# distributor straight to the buyer's transfer dealer: it never entered our
# inventory and never appeared in our bound book, so its serial must never
# become a local stock document. Recording it keeps the order auditable per gun
# and gives support something to quote — nothing more. (H1)
RECORD_ONLY_LANES = frozenset({"fds"})


def all_legs_complete(leg_statuses) -> bool:
	"""Has every distributor leg of one web order settled? (H1)

	An order can split into several distributor orders — an FDS gun on the
	fulfillment account and a direct accessory on another. Revenue is booked once,
	when they have all settled; booking per-leg would invoice the customer twice
	for one order.

	A **Cancelled** leg counts as settled (it will never arrive), but an order
	whose legs were ALL cancelled is not a completion — nothing shipped, and
	invoicing it would book revenue for a dead order.
	"""
	statuses = list(leg_statuses or [])
	if not statuses:
		return False
	if not all(s in order_state.TERMINAL_STATES for s in statuses):
		return False
	return any(s == order_state.COMPLETE for s in statuses)


def apply_recorded_serials(order_serial_rows, do_lines) -> int:
	"""Copy the distributor's reported serials onto the web order's unit rows.

	Record-keeping only — see :data:`RECORD_ONLY_LANES`. Returns how many rows
	were filled.

	Two rules protect the bound book:

	* a row that is not in a record-only lane is never touched, so a local gun's
	  real serial can never be overwritten by a distributor echo;
	* a row that already carries a serial is never rewritten.

	Surplus serials (RSR sometimes echoes the whole line on every poll) are simply
	left unused rather than spilling onto unrelated rows.
	"""
	pools: dict = {}
	for line in do_lines or []:
		if (line.get("fulfillment") or "") not in RECORD_ONLY_LANES:
			continue
		values = [s.strip() for s in str(line.get("serials") or "").split(",") if s.strip()]
		if values:
			pools.setdefault(line.get("item_code") or line.get("sku"), []).extend(values)

	applied = 0
	for row in order_serial_rows or []:
		if (row.get("fulfillment") or "local") not in RECORD_ONLY_LANES:
			continue
		if str(row.get("confirmed_serial") or "").strip():
			continue
		pool = pools.get(row.get("item_code"))
		if not pool:
			continue
		row.confirmed_serial = pool.pop(0)
		applied += 1
	return applied


# ================================================ pure: WooCommerce order notes

# When RSR echoes a tracking number without naming a carrier we still have to
# produce a parseable sentence: the storefront plugin's regex needs a non-empty carrier
# between "shipped via" and "on", and an empty one collapses the whole match.
UNKNOWN_CARRIER = "the distributor"
# Same trap for the date: the regex's date capture `(.+?)` also needs ≥1 char,
# so an empty ship_date would collapse `on  with` and fail to match. A placeholder
# keeps the note parseable; the tracking number (what the buyer needs) still lands.
UNKNOWN_SHIP_DATE = "an unspecified date"


def format_tracking_note(carrier, ship_date, tracking_number, do_name=None) -> str:
	"""One WooCommerce order note for ONE parcel.

	CONTRACT with the storefront plugin ``includes/class-order-tracking.php:447``::

	    /shipped via\\s+(.+?)\\s+on\\s+(.+?)\\s+with tracking number\\s+(\\S+?)
	     (?:\\s*\\(Shipstation\\))?\\.?\\s*$/im

	Three properties of that regex drive this format and none is obvious:

	* ``$`` under ``/m`` with no ``/s`` means the whole sentence must sit on ONE
	  line and the tracking number must END it — nothing may follow it;
	* the carrier and date captures are non-greedy up to the next literal, so a
	  prefix is safe as long as it contains no " on " of its own (``DO-YYMM-####``
	  does not);
	* WordPress calls ``preg_match``, not ``preg_match_all``, so a note yields at
	  most ONE parcel. See :func:`build_tracking_notes`.

	``ship_date`` is passed through PHP ``strtotime`` on the other side, so it is
	emitted ISO (``YYYY-MM-DD``).
	"""
	carrier = str(carrier or "").strip() or UNKNOWN_CARRIER
	ship_date = str(ship_date or "").strip() or UNKNOWN_SHIP_DATE
	prefix = f"Distributor order {do_name} " if do_name else ""
	return (f"{prefix}shipped via {carrier} on {ship_date} "
		f"with tracking number {tracking_number}")


def build_tracking_notes(parcels, do_name=None) -> list:
	"""One note per parcel, deduped on tracking number.

	Two shipments cannot share a note (``preg_match`` would drop the second), and
	the same number must not be re-posted on every poll (the note list would grow
	without bound and the board dedups only what it can see).
	"""
	notes, seen = [], set()
	for p in parcels or []:
		number = str((p or {}).get("number") or "").strip()
		if not number or number in seen:
			continue
		seen.add(number)
		notes.append(format_tracking_note(
			p.get("carrier"), p.get("date"), number, do_name=do_name))
	return notes


def shipments_from_lines(do_lines, ship_date=None) -> list:
	"""Flatten the DO lines' ``tracking_numbers`` CSV echoes into parcels.

	RSR reports tracking per line, so a two-line order shipped in one box repeats
	the same number — deduped here rather than at note-build time so the caller
	can count real parcels.
	"""
	parcels, seen = [], set()
	for line in do_lines or []:
		line = line or {}
		for raw in str(line.get("tracking_numbers") or "").split(","):
			number = raw.strip()
			if not number or number in seen:
				continue
			seen.add(number)
			parcels.append({
				"number": number,
				"carrier": line.get("carrier") or "",
				"date": ship_date or "",
			})
	return parcels


# ============================================================ pure poll-apply

def compute_do_updates(do_lines: list, status: dict) -> dict:
	"""Fold a normalised provider order status onto DO line dicts. Returns
	{status, hold_status, lines: {sku: {...}}}. Does not mutate the inputs."""
	status = status or {}
	by_sku = {l["sku"]: l for l in status.get("lines", [])}
	hold = (status.get("hold_status") or "").strip()

	line_updates: dict = {}
	for dl in do_lines:
		s = by_sku.get(dl["sku"])
		if not s:
			continue
		upd = {
			# RSR's check-order reports no AckQty (that lives only in the
			# place-order response), so its normalised status omits the key —
			# a poll must carry the placement's ack forward, never let a
			# defaulted 0 demote every Acked line back to Pending.
			"ack_qty": int(s["ack_qty"] if s.get("ack_qty") is not None
				else (dl.get("ack_qty") or 0)),
			"invoiced_qty": int(s.get("invoiced_qty") or 0),
			"item_status": s.get("item_status") or dl.get("item_status") or "",
			"invoices": _join(s.get("invoices"), dl.get("invoices")),
			"tracking_numbers": _join(s.get("tracking_numbers"), dl.get("tracking_numbers")),
			"serials": _join(s.get("serials"), dl.get("serials")),
			# Invoiced unit cost: carried forward when the
			# house's status omits it, never demoted to 0 by a silent poll.
			"invoiced_unit_cost": (float(s["unit_cost"]) if s.get("unit_cost") not in (None, "")
				else float(dl.get("invoiced_unit_cost") or 0)),
		}
		upd["line_status"] = _line_status(dl, upd)
		line_updates[dl["sku"]] = upd

	return {
		"status": _overall_status(do_lines, line_updates, hold),
		"hold_status": hold,
		"lines": line_updates,
	}


def _join(new_values, existing_csv) -> str:
	if new_values:
		return ",".join(str(v).strip() for v in new_values if str(v).strip())
	return existing_csv or ""


def _is_killed(item_status) -> bool:
	return str(item_status or "").strip().upper() in KILLED_ITEM_STATUSES


def short_shipped_lines(do_lines) -> list:
	"""DO lines that SETTLED with fewer units SHIPPED than we ordered (M4②).

	The baseline is ``invoiced_qty`` — what RSR actually shipped — NOT
	``ack_qty``: under the live contract ack is the PLACEMENT-time commitment,
	carried forward unchanged by every poll (check-order has no AckQty), so a
	line RSR later killed or short-shipped still wears its original ack and an
	ack-based test would never fire. A settled line (Shipped/Received/Killed)
	below wish will never be filled the rest of the way. Returns
	``[{sku, wish_qty, ack_qty, shipped_qty, short_by, line_status}]``; a
	still-Pending/Acked line is excluded (it may yet ship in full).
	"""
	out = []
	for l in do_lines or []:
		if (l.get("line_status") or "Pending") not in SETTLED_LINE_STATES:
			continue
		wish = int(l.get("wish_qty") or 0)
		shipped = int(l.get("invoiced_qty") or 0)
		if shipped < wish:
			out.append({
				"sku": l.get("sku"), "wish_qty": wish,
				"ack_qty": int(l.get("ack_qty") or 0), "shipped_qty": shipped,
				"short_by": wish - shipped, "line_status": l.get("line_status") or "",
			})
	return out


def _line_status(dl: dict, upd: dict) -> str:
	if _is_killed(upd["item_status"]):
		return "Killed"
	if upd["invoiced_qty"] > 0:
		return "Shipped"
	if upd["ack_qty"] > 0:
		return "Acked"
	return "Pending"


def _overall_status(do_lines: list, line_updates: dict, hold: str) -> str:
	if hold:
		return order_state.HOLD
	statuses = []
	for dl in do_lines:
		u = line_updates.get(dl["sku"])
		statuses.append(u["line_status"] if u else (dl.get("line_status") or "Pending"))
	if not statuses:
		return order_state.PLACED
	any_shipped = any(st in SHIPPED_LINE_STATES for st in statuses)
	all_settled = all(st in SETTLED_LINE_STATES for st in statuses)
	any_acked = any(st == "Acked" or st in SHIPPED_LINE_STATES for st in statuses)
	# All lines settled (each Shipped/Received/Killed) → terminal Complete, even
	# if RSR killed some (or all) of them: nothing more will arrive. A
	# fully-killed order thus terminalises instead of lingering in Placed.
	if all_settled:
		return order_state.COMPLETE
	if any_shipped:
		return order_state.PARTIALLY_SHIPPED
	if any_acked:
		return order_state.ACKNOWLEDGED
	return order_state.PLACED


# ============================================================ pure backoff/watchdog

def _next_interval(prev_interval_seconds) -> int:
	"""Per-order poll backoff: 30m start, double each poll, capped at 2h."""
	if not prev_interval_seconds:
		return POLL_START_SECONDS
	return min(int(prev_interval_seconds) * 2, POLL_MAX_SECONDS)


def _is_stale(elapsed_seconds) -> bool:
	return elapsed_seconds >= STALE_SECONDS


def _hold_escalation_level(elapsed_seconds):
	if elapsed_seconds >= HOLD_DAY45_SECONDS:
		return "day4.5"
	if elapsed_seconds >= HOLD_DAY3_SECONDS:
		return "day3"
	return None


# ============================================================ frappe: cron gate

def poll_distributor_orders() -> None:
	"""`*/30` cron gate. Enqueues one poll per live, due order across enabled,
	poll-enabled distributors; alerts (once) on 14-day-stale orders and stops
	polling them. Self-throttling — a distributor with no due orders is a no-op."""
	distributors = frappe.get_all(
		"Distributor", filters={"enabled": 1, "poll_enabled": 1}, pluck="name")
	if not distributors:
		return
	now = now_datetime()
	due = frappe.get_all(
		"Distributor Order",
		filters={
			"distributor": ("in", distributors),
			"status": ("in", list(order_state.POLLABLE_STATES)),
		},
		fields=["name", "status", "placed_at", "next_poll_at", "distributor"],
	)
	for row in due:
		if row.next_poll_at and time_diff_in_seconds(now, row.next_poll_at) < 0:
			continue  # not due yet
		if row.placed_at and _is_stale(time_diff_in_seconds(now, row.placed_at)):
			_alert_stale(row)
			continue  # stop polling a stale order until a human looks
		enqueue_distributor(
			"ffl_integrations.distributor.tasks.poll_order",
			job_name=f"dist_poll_{row.name}", do_name=row.name)


def poll_order(do_name: str) -> None:
	"""Poll one order: pull provider status, fold onto lines, advance status,
	set the next backed-off poll time, and escalate a lingering FDS hold."""
	do = frappe.get_doc("Distributor Order", do_name)
	if do.status not in order_state.POLLABLE_STATES:
		return
	provider = _provider_for(do)
	# m6: a flaky check-order must not throw out of the job (which would leave
	# next_poll_at un-advanced and re-fire the failure every */30). Back off + alert.
	try:
		status = provider.get_order_status(do_name)
	except Exception as exc:  # noqa: BLE001
		now = now_datetime()
		advance_schedule(do, now)
		do.save(ignore_permissions=True)
		safe_commit()
		notify_admins(
			f"Distributor Order poll failed: {do_name}", str(exc),
			dedup_key=f"dist_poll_fail_{do_name}")
		return

	do_line_dicts = [_line_to_dict(l) for l in do.lines]
	res = compute_do_updates(do_line_dicts, status)

	by_sku = {l.sku: l for l in do.lines}
	for sku, upd in res["lines"].items():
		line = by_sku.get(sku)
		if not line:
			continue
		line.ack_qty = upd["ack_qty"]
		line.invoiced_qty = upd["invoiced_qty"]
		line.item_status = upd["item_status"]
		line.invoices = upd["invoices"]
		line.tracking_numbers = upd["tracking_numbers"]
		line.serials = upd["serials"]
		line.line_status = upd["line_status"]
		line.invoiced_unit_cost = upd["invoiced_unit_cost"]

	do.hold_status = res["hold_status"]
	freight_known = status.get("freight") is not None
	if freight_known:
		do.invoiced_freight = float(status["freight"])
	# A placement that answered without a WebRef (StatusCode 60 queued, or any
	# response that omitted it) leaves web_ref empty — and update-order-ffl is
	# keyed by WebRef, so an FDS hold on such an order would be unreleasable.
	# check-order echoes the WebRef; backfill the one self-heal point we have.
	if not (do.web_ref or "").strip() and (status.get("web_ref") or "").strip():
		do.web_ref = status["web_ref"].strip()
	if order_state.can_transition(do.status, res["status"]):
		do.status = res["status"]
	now = now_datetime()
	advance_schedule(do, now)  # M1: compute next_poll_at BEFORE overwriting last_poll_at
	do.save(ignore_permissions=True)
	safe_commit()
	if freight_known:
		_maybe_alert_freight(do)
	_maybe_alert_hold(do, now)
	_maybe_alert_short_ship(do)
	# The poll is the only thing that can move an order to Complete, so it is also
	# where a fully-shipped web order gets finished.
	maybe_complete_source(do)


@frappe.whitelist()
def poll_order_now(do_name: str) -> dict:
	"""Pull one Distributor Order's status from the house right now — the
	"Check status now" button, for a parcel that beat the 30-minute poll.

	Runs the same ``poll_order`` the scheduler runs (schedule advances as
	usual); a non-pollable order (Draft, Complete, Cancelled…) is left alone.

	Args:
	    do_name: Distributor Order name.

	Returns:
	    {name, status, receive_status, hold_status, lines} after the poll.

	Permissions:
	    Receive roles.
	"""
	from ffl_integrations.distributor.receive_bridge import RECEIVE_ROLES
	frappe.only_for(RECEIVE_ROLES)
	poll_order(do_name)
	do = frappe.get_doc("Distributor Order", do_name)
	return {"name": do.name, "status": do.status, "receive_status": do.receive_status,
		"hold_status": do.hold_status or "", "lines": len(do.lines)}


def place_order_job(do_name: str) -> None:
	"""Placement worker: place a Queued order idempotently (the provider uses the
	DO name as PONum and disambiguates via check-order, never blind-resubmits).
	On a terminal provider error the order goes to Error for a human."""
	# m4: lock the row so two racing confirms can't both read QUEUED and each place
	# an order. A second concurrent job blocks here until the first commits (status
	# no longer QUEUED) then returns. The RSR PONum-idempotent path remains the
	# end-to-end backstop; this closes the local TOCTOU so we never even attempt a
	# second placement.
	current = frappe.db.get_value("Distributor Order", do_name, "status", for_update=True)
	if current != order_state.QUEUED:
		return
	do = frappe.get_doc("Distributor Order", do_name)
	provider = _provider_for(do)
	try:
		result = provider.place_order(do_name)
	except Exception as exc:  # noqa: BLE001 — record + alert, never crash the worker
		# The provider works on its own copy and may have persisted fields mid-placement
		# (Sports South commits web_ref right after AddHeader); saving this stale copy
		# would wipe them — and an empty web_ref lets a retry place a second order.
		do.reload()
		_to_error(do, str(exc))
		notify_admins(
			f"{do.distributor} place-order failed: {do_name}", frappe.get_traceback(),
			dedup_key=f"dist_place_{do_name}")
		return

	# The order is now live at the house — a write-back failure here must NOT re-drive
	# placement (that would risk a double order); record + alert instead.
	try:
		do.web_ref = result.get("web_ref") or ""
		do.hold_status = result.get("hold_status") or ""
		# B1: capture the distributor's freight so the cost PI can book its Freight
		# and Forwarding line. 0 when the response omitted it.
		do.freight_cost = float(result.get("freight") or 0)
		do.raw_response = frappe.as_json(result.get("raw") or {})
		by_sku = {l.sku: l for l in do.lines}
		for ack in result.get("ack_lines") or []:
			line = by_sku.get(ack.get("sku"))
			if line:
				line.ack_qty = ack.get("ack_qty") or 0
				line.unit_cost = ack.get("unit_cost") or line.unit_cost
				line.item_status = ack.get("item_status") or ""
		new_status = order_state.HOLD if do.hold_status else order_state.PLACED
		if order_state.can_transition(do.status, new_status):
			do.status = new_status
		do.placed_at = now_datetime()
		do.next_poll_at = _compute_next_poll_at(do, do.placed_at)
		if result.get("queued"):
			# StatusCode 60: accepted but queued for after RSR's 3–5am ET
			# maintenance window — check-order won't know the PONum until the
			# queue drains, so a +30m first poll would just harvest a 98. Wait
			# out the window (2h) plus slack before the first status pull.
			do.next_poll_at = add_to_date(do.placed_at, minutes=150)
		do.save(ignore_permissions=True)
		safe_commit()
	except Exception:  # noqa: BLE001
		notify_admins(
			f"{do.distributor} order placed but write-back failed: {do_name}",
			f"The order is LIVE at {do.distributor} (web_ref {result.get('web_ref')!r}) but recording it "
			f"failed — reconcile by PONum, do NOT re-place.\n\n{frappe.get_traceback()}",
			dedup_key=f"dist_placeback_{do_name}")


# ============================================================ frappe helpers

def _provider_for(do):
	from ffl_integrations.distributor import api
	code = frappe.db.get_value("Distributor", do.distributor, "provider") or "rsr"
	return api.get_provider(code)


def _line_to_dict(line) -> dict:
	return {
		"sku": line.sku,
		"wish_qty": line.wish_qty,
		"ack_qty": line.ack_qty or 0,
		"invoiced_qty": line.invoiced_qty or 0,
		"received_qty": line.received_qty or 0,
		"serials": line.serials or "",
		"tracking_numbers": line.tracking_numbers or "",
		"invoices": line.invoices or "",
		"item_status": line.item_status or "",
		"line_status": line.line_status or "Pending",
		"invoiced_unit_cost": line.invoiced_unit_cost or 0,
	}


def _compute_next_poll_at(do, now):
	# The interval just waited = next_poll_at − anchor, where anchor is the last
	# poll time, or (for the first poll after placement, when last_poll_at is still
	# unset) the placement time. Read BEFORE last_poll_at is overwritten to `now`
	# — advance_schedule enforces that order.
	anchor = do.last_poll_at or do.placed_at
	prev = 0
	if anchor and do.next_poll_at:
		prev = max(0, time_diff_in_seconds(do.next_poll_at, anchor))
	return add_to_date(now, seconds=_next_interval(prev))


def advance_schedule(do, now) -> None:
	"""Advance the poll schedule for the poll happening at `now`. ORDER MATTERS
: next_poll_at is derived from the CURRENT schedule (the interval just
	waited) BEFORE last_poll_at is overwritten to `now`."""
	do.next_poll_at = _compute_next_poll_at(do, now)
	do.last_poll_at = now


def _to_error(do, message: str) -> None:
	if order_state.can_transition(do.status, order_state.ERROR):
		do.status = order_state.ERROR
	do.error = (message or "")[:2000]
	do.save(ignore_permissions=True)
	safe_commit()


def _alert_stale(row) -> None:
	notify_admins(
		f"Distributor Order stale >14d: {row.name}",
		f"{row.name} ({row.distributor}) has been in status {row.status} since "
		f"{row.placed_at} with no completion. Polling paused — please investigate.",
		dedup_key=f"dist_stale_{row.name}")


def _maybe_alert_short_ship(do) -> None:
	"""Alert (once per DO) when a line settled below what we ordered (M4②).

	v1 only ALERTS — the partial refund is manual (docs/rsr-fds-after-sale-sop.md).
	A customer who paid for N and received M must not be silently left short.
	Dedup per DO so a multi-poll settle does not re-nag."""
	short = short_shipped_lines([_line_to_dict(l) for l in do.lines])
	if not short:
		return
	rows = "\n".join(
		f"  {s['sku']}: ordered {s['wish_qty']}, shipped {s['shipped_qty']} "
		f"(acked {s['ack_qty']}, short {s['short_by']}, {s['line_status']})" for s in short)
	notify_admins(
		f"Distributor order short-shipped: {do.name}",
		f"{do.name} ({do.distributor}) settled below the ordered quantity:\n\n{rows}\n\n"
		f"Arrange a partial refund for the un-shipped units — this is a manual step "
		f"(docs/rsr-fds-after-sale-sop.md). The cost invoice already books only the "
		f"acknowledged quantity.",
		dedup_key=f"dist_short_{do.name}")


def freight_variance(invoiced, estimated, limit) -> float | None:
	"""The gap between what the house invoiced for freight and what we booked at
	placement, when it exceeds the house's limit; None when unknown or within."""
	if invoiced is None:
		return None
	gap = round(float(invoiced) - float(estimated or 0), 2)
	return gap if abs(gap) > float(limit or 0) else None


def _freight_limit(distributor: str) -> float:
	from ffl_integrations.distributor.api import house_setting
	return house_setting(distributor, "freight_variance_alert", 5)


def _maybe_alert_freight(do) -> None:
	"""Alert (once per DO) when the invoiced freight strays from the placement
	figure by more than the house's limit — the one check
	of whether the Freight tab's rules match what the house actually charges."""
	gap = freight_variance(do.invoiced_freight, do.freight_cost, _freight_limit(do.distributor))
	if gap is None:
		return
	notify_admins(
		f"Freight differs from the order: {do.name}",
		f"{do.name} ({do.distributor}) was booked with freight {float(do.freight_cost or 0):.2f} "
		f"but the invoice says {float(do.invoiced_freight or 0):.2f} (gap {gap:+.2f}). Check the "
		f"house's freight rules in its Settings → Freight tab.",
		dedup_key=f"dist_freight_{do.name}")


def _maybe_alert_hold(do, now) -> None:
	if do.status != order_state.HOLD or not do.placed_at:
		return
	level = _hold_escalation_level(time_diff_in_seconds(now, do.placed_at))
	if not level:
		return
	notify_admins(
		f"FDS Hold escalation ({level}): {do.name}",
		f"{do.name} ({do.distributor}) has been on hold '{do.hold_status}' since "
		f"{do.placed_at}. Attach/replace the ShipFFL to release it.",
		dedup_key=f"dist_hold_{do.name}_{level}")


# ============================================================ feed watchdog (P3)

def feed_stale_verdict(heartbeat, now, threshold_seconds=FEED_STALE_SECONDS) -> dict:
	"""PURE. Is a distributor's catalog/quantity feed too old to sell from?

	Returns ``{"stale": bool, "age_seconds": float|None, "reason": str}``. A feed
	that never synced is stale (``never_synced``) — "no heartbeat yet" must not read
	as healthy.
	"""
	if not heartbeat:
		return {"stale": True, "age_seconds": None, "reason": STALE_REASON_NEVER}
	age = time_diff_in_seconds(now, heartbeat)
	if age >= threshold_seconds:
		return {"stale": True, "age_seconds": age, "reason": STALE_REASON_AGED}
	return {"stale": False, "age_seconds": age, "reason": STALE_REASON_FRESH}


def _provider_by_code(code):
	from ffl_integrations.distributor import api

	return api.get_provider(code)


def _heartbeat_for(provider):
	"""(`verdict_input`, `supported`) for one provider.

	`feed_heartbeat` is an OPTIONAL provider method (same pattern as
	`verify_transfer_dealer` / `update_order_ffl`): a distributor we order from by
	phone has no feed to age out, so it is skipped rather than alerted on.
	"""
	if not _has_feed(provider):
		return None, False
	return provider.feed_heartbeat(), True


def _has_feed(provider) -> bool:
	"""Does this house have a feed to age out (an optional ``feed_heartbeat``, as
	``RemoteCatalogMixin`` gives every catalog-reading house)? Asked without calling
	it: a house ordered by phone has none and is skipped, catalog or no catalog."""
	return callable(getattr(provider, "feed_heartbeat", None))


def _catalog_off_for(code) -> bool:
	"""This house's catalog reads are confirmed off (its own service, else the
	site's; ``unknown`` is not off). No house: the site as a whole."""
	from ffl_integrations.data_service import CATALOG, is_off

	return is_off(CATALOG, house=code) if code else is_off(CATALOG)


def check_feed_staleness() -> dict:
	"""`*/15` cron gate. Alerts (deduped per distributor) when a drop-ship
	distributor's feed heartbeat ages past FEED_STALE_SECONDS, and — when that
	distributor opts in — pushes its listings to zero stock so a feed outage can't
	keep selling inventory we can no longer verify.

	A house whose catalog reads are off (capability ``catalog`` for its house: no
	Local catalog, no service its reads would go to) has nothing to verify its feed
	against: when it has a feed at all (``_has_feed``) it is stale by definition —
	alerted, and its zero-stock interlock applies as for a feed that aged out (the
	policy is the house's own switch); a house without a feed is skipped exactly as
	with a catalog. ``off``: every checked house's catalog is off (a site with no
	enabled drop-ship house is silent). The watchdog keeps no state of its own to
	clear (its alert throttles expire)."""
	distributors = frappe.get_all(
		"Distributor", filters={"enabled": 1, "dropship_enabled": 1},
		fields=["name", "provider"])
	now = now_datetime()
	stale, skipped, off = [], [], []
	for row in distributors:
		name, code = row["name"], row["provider"]
		if code and _catalog_off_for(code):
			off.append(name)
			if _no_catalog_alarm(name, code):
				stale.append(name)
			else:
				skipped.append(name)
			continue
		probe_error = None
		# Reset EVERY iteration, before the try — not just on failure. If
		# `_provider_by_code` itself raises (a free-text Distributor.provider typo,
		# a provider-factory failure), `provider` must still be a real name bound to
		# `None`, never left unassigned (crashes on the very first distributor with
		# UnboundLocalError at the `getattr(provider, ...)` content-check below) or
		# leaked from a PRIOR, unrelated distributor's successful iteration.
		provider = None
		try:
			provider = _provider_by_code(code)
			heartbeat, supported = _heartbeat_for(provider)
		except Exception as exc:  # noqa: BLE001
			# Cannot verify freshness → assume the worst: treat this distributor as
			# stale so the alert and the zero-stock interlock both engage. A probe
			# failure is a DIFFERENT diagnosis from "the feed never ran", so it is
			# reported as such rather than as "never synced".
			heartbeat, supported, probe_error = None, True, exc
			safe_log_error(f"Distributor feed heartbeat failed: {name}", str(exc))
		if not supported:
			skipped.append(name)
			continue
		verdict = feed_stale_verdict(heartbeat, now)
		if verdict["stale"]:
			stale.append(name)
			if probe_error is not None:
				age_txt = f"unknown — heartbeat probe failed ({probe_error})"
			else:
				age_txt = ("never synced" if verdict["age_seconds"] is None
					else f"{int(verdict['age_seconds'] // 60)} min old")
			# F1: only a feed that HAD a heartbeat and aged out may zero stock — the
			# same judgement `is_feed_stale` uses. A cold start (quantity feed never
			# ran, e.g. the catalog feed loaded first) still ALERTS but must not blank
			# the catalogue; otherwise the gate and the stock source disagree every 15
			# min and the whole drop-ship catalogue oscillates 0 ↔ N, pushing that
			# churn to Woo. The published listings are the drop-ship layer's.
			zero_stock = dropship_hooks.feed_stale(
				name, aged=verdict["reason"] == STALE_REASON_AGED)
			notify_admins(
				f"Distributor feed stale: {name}",
				f"{name}'s catalog/quantity feed heartbeat is {age_txt} (threshold "
				f"{FEED_STALE_SECONDS // 60} min). Published drop-ship quantities are no "
				f"longer trustworthy.\n\nZero-stock interlock: "
				f"{'ON — listings are being pushed to 0' if zero_stock else 'OFF'}.",
				dedup_key=f"dist_feed_stale_{name}")

		# Catalog CONTENT age check — independent of the quantity verdict above, so
		# a distributor with a perfectly fresh quantity heartbeat can still surface a
		# stale content ingest. Only probed when the heartbeat probe itself succeeded
		# (probe_error is None): if we couldn't even ask, the quantity path above
		# already alerted "transport down" and a second content alert would be noise.
		content_fn = getattr(provider, "catalog_content_heartbeat", None)
		if callable(content_fn) and probe_error is None:
			try:
				content_hb = content_fn()
			except Exception as exc:  # noqa: BLE001 — a broken content probe must not
				# crash the tick. NOTE this is NOT the same case the quantity path's
				# except above covers: we only get here when probe_error IS None (the
				# quantity heartbeat probe just succeeded), so nothing has alerted about
				# this distributor yet — still log it, or a broken content probe fails
				# silently forever.
				content_hb = None
				safe_log_error(
					f"Distributor catalog content heartbeat failed: {name}", str(exc))
			if content_hb is not None:
				threshold = content_stale_seconds(provider)
				content_verdict = feed_stale_verdict(
					content_hb, now, threshold_seconds=threshold)
				if content_verdict["reason"] == STALE_REASON_AGED:
					notify_admins(
						f"Distributor catalog content stale: {name}",
						f"{name}'s catalog content ingest is "
						f"{int(content_verdict['age_seconds'] // 60)} min old "
						f"(threshold {threshold // 60} min). "
						f"Quantity heartbeat is fine — prices/content may be drifting. "
						f"Alert only; no interlock.",
						dedup_key=f"dist_catalog_stale_{name}")
	out = {"stale": stale, "skipped": skipped, "checked": len(distributors)}
	if len(off) == len(distributors) and (off or _catalog_off_for(None)):
		out["off"] = True
	return out


def _no_catalog_alarm(name, code) -> bool:
	"""``check_feed_staleness`` for a drop-ship house with no catalog: one with a
	feed sells quantities nothing can verify (alert + its zero-stock switch; True);
	one without a feed (ordered by phone) has nothing to verify (False, skipped). A
	provider that cannot be resolved is assumed to have a feed, as the probe path
	assumes the worst."""
	try:
		has_feed = _has_feed(_provider_by_code(code))
	except Exception:  # noqa: BLE001 — cannot tell: alert rather than stay silent
		has_feed = True
	if not has_feed:
		return False
	zero_stock = dropship_hooks.feed_stale(name, aged=True)
	notify_admins(
		f"Distributor feed stale: {name}",
		f"{name} is set to drop-ship, but this site has no distributor catalog for it "
		f"(Data Service Settings: connect the Data Service, or set Catalog source to "
		f"Local), so its published quantities cannot be verified.\n\nZero-stock "
		f"interlock: {'ON — listings are being pushed to 0' if zero_stock else 'OFF'}.",
		dedup_key=f"dist_feed_stale_{name}")
	return True


def is_feed_stale(distributor_name) -> bool:
	"""Feed-staleness for the stock source. Cached per request: a reconcile pass
	asks this once per item and the answer can't change mid-pass."""
	cache = _stale_cache()
	if distributor_name in cache:
		return cache[distributor_name]
	verdict_stale = False
	try:
		code = frappe.db.get_value("Distributor", distributor_name, "provider")
		if code and _catalog_off_for(code):
			# No catalog for this house: an enabled drop-ship house with a feed has
			# nothing to verify its quantities against, so it is stale (the watchdog
			# alerts); a house without a feed, or not drop-shipping, has none to judge.
			flags = frappe.db.get_value("Distributor", distributor_name, ["enabled", "dropship_enabled"])
			verdict_stale = bool(flags and all(flags) and _has_feed(_provider_by_code(code)))
		elif code:
			heartbeat, supported = _heartbeat_for(_provider_by_code(code))
			if supported:
				# Only a feed that HAD a heartbeat and then aged out may zero
				# published stock. A cold start (never synced) must not blank the
				# catalogue on day one — the watchdog still ALERTS on never_synced,
				# which is the right channel for "the feed has never run".
				verdict_stale = feed_stale_verdict(
					heartbeat, now_datetime())["reason"] == STALE_REASON_AGED
	except Exception:  # noqa: BLE001 — a broken heartbeat must not break stock pushes
		# m4: this deliberately fails in the OPPOSITE direction to
		# check_feed_staleness above, which treats an unreadable heartbeat as stale.
		# There, being wrong costs one spurious alert. Here, being wrong costs the
		# whole catalogue: this runs per item inside push_stock/reconcile, so treating
		# a transient DB hiccup as "stale" would publish 0 across every listing at
		# once. We therefore keep selling on the last known quantity and let the
		# watchdog (which alerts) be the authority on staleness.
		verdict_stale = False
		notify_admins(
			f"Feed staleness check failed: {distributor_name}",
			f"Could not determine feed freshness for {distributor_name}; continuing to "
			f"publish the last known quantities. The zero-stock interlock is NOT "
			f"engaged for this pass.\n\n{frappe.get_traceback()}",
			dedup_key=f"dist_feed_check_fail_{distributor_name}", window_minutes=1440)
	cache[distributor_name] = verdict_stale
	return verdict_stale


def _stale_cache() -> dict:
	try:
		cache = getattr(frappe.local, "_dist_feed_stale_cache", None)
		if cache is None:
			cache = {}
			frappe.local._dist_feed_stale_cache = cache
		return cache
	except Exception:  # noqa: BLE001 — no request context (harness / bench execute)
		return {}


# ==================================================== frappe: completion path

def woo_attr(dotted: str):
	"""Late-bind a ``ffl_woo_sync`` callable by dotted path.

	``ffl_integrations`` must stay installable and usable on a POS-only site,
	which has no webshop and therefore no ``ffl_woo_sync`` package on disk.
	A function-local ``import ffl_woo_sync…`` would still be a
	latent ImportError on that supported configuration, so the two web-fulfilment
	call sites resolve their dependency the way frappe resolves hooks: by name, at
	the moment of use.

	Reached from web-order paths only (:func:`_notify_shop_of_status` and
	the drop-ship layer's web-order completion), which cannot run without a Woo Online
	Order — so on a POS-only site this is never called at all.
	"""
	return frappe.get_attr(dotted)

def source_legs(doctype: str, name: str) -> list:
	return frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": doctype, "source_name": name},
		fields=["name", "status", "order_type"])


def on_distributor_order_update(doc, method=None) -> None:
	"""``Distributor Order: on_update`` doc_event.

	Two jobs. First, tell the shop about a status change on a web order's leg
	(Placed / Hold / Partially Shipped / Complete / Cancelled / Error) — after
	commit, so the shop never hears about a state that then rolled back.
	Second, the poll is not the only way a DO reaches Complete — an operator can
	move it by hand (a phone order, an exception fix), and that must finish the
	source web order too. Only a Complete order is worth checking for that.
	"""
	_notify_staff_of_shipment(doc)
	_notify_shop_of_status(doc)
	if (doc.get("status") or "") != order_state.COMPLETE:
		return
	maybe_complete_source(doc)


def _notify_staff_of_shipment(doc) -> None:
	"""Ring the counter's bell when goods coming INTO the store ship (Partially
	Shipped / Complete) — Stocking and Procure To Store only; a drop-ship goes
	straight to the buyer. The distributor gives no arrival date, so "shipped"
	is the last thing we can tell anyone. Never raises."""
	from ffl_core.alerts import notify_staff
	from ffl_core.roles import COUNTER_ROLES

	try:
		status = doc.get("status")
		if (status not in (order_state.PARTIALLY_SHIPPED, order_state.COMPLETE)
				or doc.get("order_type") not in ("Stocking", "Procure To Store")
				or doc.get("receive_status") == "Received"):  # closed by hand after receiving
			return
		before_fn = getattr(doc, "get_doc_before_save", None)
		before = before_fn() if callable(before_fn) else None
		if not before or before.get("status") == status:
			return
		what = "shipped part of" if status == order_state.PARTIALLY_SHIPPED else "shipped"
		who = f" for {doc.consumer_name}" if doc.get("consumer_name") else ""
		# COUNTER_ROLES already holds the receiving roles (receive_bridge.RECEIVE_ROLES).
		notify_staff(doc.doctype, doc.name, f"{doc.distributor} {what} {doc.name}{who}", COUNTER_ROLES)
	except Exception:
		safe_log_error(f"Shipment notification failed: {doc.get('name')}", frappe.get_traceback())


def _notify_shop_of_status(doc) -> None:
	"""Enqueue the drop-ship status event for a web order's leg on a real
	transition, or on a new tracking number within one status (see
	``ffl_woo_sync.woocommerce.status_events``).

	Only a genuine Document carries ``get_doc_before_save``; the plain dicts the
	unit tests hand in (and any caller without a before-image) are not a
	transition we can vouch for, so they stay silent. The job is resolved by
	dotted name at run time — ``ffl_woo_sync`` is absent on a POS-only site, and
	so is any Woo Online Order, so the guard on ``source_doctype`` keeps this
	from ever enqueueing there.
	"""
	if doc.get("source_doctype") != "Woo Online Order" or not doc.get("source_name"):
		return
	before_fn = getattr(doc, "get_doc_before_save", None)
	if not callable(before_fn):
		return
	before = before_fn()
	before_status = before.get("status") if before else None
	should = woo_attr("ffl_woo_sync.woocommerce.status_events.should_notify")
	numbers = woo_attr("ffl_woo_sync.woocommerce.status_events.tracking_numbers")
	if not should(before_status, doc.get("status"),
			numbers(before.get("lines") if before else None), numbers(doc.get("lines"))):
		return
	enqueue_distributor(
		"ffl_woo_sync.woocommerce.status_events.post_status_event",
		job_name=f"dist_status_event_{doc.name}_{doc.get('status')}",
		do_name=doc.name, enqueue_after_commit=True)


def maybe_complete_source(do) -> None:
	"""When EVERY leg of the source web order has settled, run the completion path
	once. Reached from both the poll and the manual on_update doc_event.

	Guarded by ``all_legs_complete`` rather than by this order alone: an order
	split into an FDS gun and a direct accessory must be invoiced once, at the
	end, not twice.
	"""
	doctype = do.get("source_doctype")
	name = do.get("source_name")
	if not name or doctype not in ("Woo Online Order", "Sales Invoice"):
		return
	legs = source_legs(doctype, name)
	if not all_legs_complete([l["status"] for l in legs]):
		return
	# after_commit: this can be called from the on_update doc_event, INSIDE the
	# DO's save transaction — the job must not read the DO before its Complete
	# status is durable. job_name dedups the poll and doc_event paths firing together.
	if doctype == "Sales Invoice":
		# Counter: the SI already booked revenue at the counter, so completion is
		# lighter — cost PI + tracking comment/email, no Transferred/revenue SI.
		enqueue_distributor(
			"ffl_integrations.distributor.tasks.complete_counter_order",
			job_name=f"dist_complete_counter_{name}", sales_invoice=name,
			enqueue_after_commit=True)
		return
	# A web order: its completion belongs to the drop-ship layer that routed it.
	dropship_hooks.source_settled(doctype, name)


def book_cost_invoices(docs, _source) -> list:
	"""Draft cost PI per distributor order (never submits).

	``_source`` (the Woo order / counter invoice this ran for) is accepted but unused —
	the cost is a property of the Distributor Order alone, which is exactly why both
	the web and counter completion paths can share this (named with a
	leading underscore so the signature stops implying a Woo-only dependency).

	Runs as soon as the distributor leg is done — its cost is real then, regardless
	of whether the order's local leg is still pending dispose.
	"""
	from ffl_integrations.distributor import accounting

	invoices = []
	for do in docs:
		try:
			invoices.append(accounting.ensure_cost_invoice(do.name))
		except Exception:  # noqa: BLE001
			notify_admins(
				f"Drop-ship cost invoice failed: {do.name}",
				frappe.get_traceback(), dedup_key=f"dist_pi_{do.name}")
	return invoices


def counter_tracking_lines(docs) -> list:
	"""Plain human lines describing a completed counter order's parcels — shared by
	the SI comment and the customer email (v1 plain template). One line per parcel;
	a firearm leg's serial(s) surface on their own line. PURE — no side effects."""
	out = []
	for do in docs:
		parcels = shipments_from_lines([line_to_record(l) for l in (do.lines or [])])
		for parcel in parcels:
			out.append(f"{do.name}: tracking {parcel['number']}")
		serials = [s.strip() for l in (do.lines or [])
			for s in str(l.get("serials") or "").split(",") if s.strip()]
		if serials and parcels:
			out.append(f"{do.name} serial(s): {', '.join(serials)}")
	return out


def complete_counter_order(sales_invoice: str) -> dict:
	"""Finish a fully-shipped counter drop-ship order.

	The counter Sales Invoice already booked revenue at the counter, so this is
	lighter than the web path — no Transferred, no revenue SI. It books the RSR COST
	(Draft PI, reused from the web path), records the shipment on the invoice
	as a comment, and emails the customer a plain tracking note (v1) — drop-ship
	legs only; a procure-to-store leg's parcel is addressed to the store.

	Idempotent: the cost PI dedups on ``(distributor_order, bill_no)``; the comment
	and email report only parcels not already recorded on each DO's ``notes_posted``
, so a re-run (several legs completing in one sweep, or a manual DO re-save)
	can neither double-comment nor re-email.
	"""
	legs = source_legs("Sales Invoice", sales_invoice)
	if not all_legs_complete([l["status"] for l in legs]):
		return {"status": "incomplete"}
	docs = [frappe.get_doc("Distributor Order", l["name"]) for l in legs
		if l["status"] == order_state.COMPLETE]

	cost_invoices = book_cost_invoices(docs, sales_invoice)

	# A procure-to-store leg ships distributor → store; the store hands the goods
	# over, so its tracking never reaches the customer (the web path's C2 rule).
	fresh = [do for do in docs
		if not lanes.stocks_goods(do.get("order_type")) and _has_unreported_parcel(do)]
	lines = counter_tracking_lines(fresh)
	if lines:
		_post_counter_comment(sales_invoice, lines)
		_email_counter_customer(sales_invoice, lines)
		for do in fresh:
			_mark_parcels_reported(do)
	return {"status": "complete", "cost_invoices": cost_invoices, "reported": len(lines)}


def _do_parcel_numbers(do) -> list:
	return [p["number"] for p in shipments_from_lines(
		[line_to_record(l) for l in (do.lines or [])])]


def _has_unreported_parcel(do) -> bool:
	already = {s for s in str(do.get("notes_posted") or "").split(",") if s}
	return any(n not in already for n in _do_parcel_numbers(do))


def _mark_parcels_reported(do) -> None:
	already = {s for s in str(do.get("notes_posted") or "").split(",") if s}
	frappe.db.set_value(
		"Distributor Order", do.name, "notes_posted",
		",".join(sorted(already | set(_do_parcel_numbers(do)))), update_modified=False)


def _post_counter_comment(sales_invoice: str, lines: list) -> None:
	frappe.get_doc("Sales Invoice", sales_invoice).add_comment(
		"Comment", "Drop-ship shipped:\n" + "\n".join(lines))


def _email_counter_customer(sales_invoice: str, lines: list) -> None:
	email = frappe.db.get_value("Sales Invoice", sales_invoice, "contact_email")
	if not email:
		return
	frappe.sendmail(
		recipients=[email],
		subject="Your order has shipped",
		message=("Good news — your order has shipped from our distributor.\n\n"
			+ "\n".join(lines)
			+ "\n\nThank you for your business."))


def line_to_record(line) -> dict:
	return {
		"item_code": line.item_code,
		"sku": line.sku,
		"serials": line.serials or "",
		"fulfillment": line.fulfillment or "",
		"tracking_numbers": line.tracking_numbers or "",
	}


def woo_client(order):
	"""The WooCommerce client for the order's store, or None when unconfigured.

	A WooCommerce outage must never strand completion — the caller alerts/retries.
	"""
	try:
		return woo_attr(
			"ffl_woo_sync.woocommerce.client.WooCommerceClient").from_settings(
			order.get("site") or "retail")
	except Exception:  # noqa: BLE001 — store not configured on this site
		return None

