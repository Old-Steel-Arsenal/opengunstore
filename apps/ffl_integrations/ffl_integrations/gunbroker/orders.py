"""Pulling GunBroker sales into POS.

GunBroker will not tell us when something sells — this phase polls. Every
fifteen minutes we ask ``/OrdersSold`` for everything modified since a
watermark, fetch each order in full, and write it down. From then on the gun is
spoken for and every other sales channel has to stop offering it, because a
GunBroker sale cannot be cancelled from the seller's side: getting this wrong
means refunding a buyer and apologising, not editing a record.

Four decisions in here are load-bearing.

**The watermark starts at now, not thirty days ago.** The production account has
years of history. A first run that reached back a month would match those old
orders' serial numbers against guns currently in stock and reserve the wrong
ones. ``orders_since_override`` exists for the one time somebody genuinely wants
history back, and it is the only way to get it.

**One order's failure does not block the batch, but it does stop the
watermark.** Orders are processed oldest-change-first and the watermark advances
only as far as the last order before the first failure — so a failure is retried
next round instead of being skipped forever. An order that has failed five times
running stops holding the line: it is marked, alerted on, and stepped over,
because one permanently broken order must not freeze every sale behind it.

**The full order is always fetched.** The ``/OrdersSold`` row does not carry
per-line serial numbers or SKUs, so there is nowhere else a line can be matched
back to a gun in stock.

**An order re-polls constantly and must not fight itself.** ``lastModifiedDate``
changes when the buyer pays, when the FFL arrives, when we ship. Each of those
brings the same order back through here, so the reservation check exempts the
order's own reservation before it does anything else — without that, every hour
the store gets an email saying a gun it sold on GunBroker is unavailable
because GunBroker has it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ffl_core.marketplace_accounts import refund_remarks_rule
from ffl_core.reservations import (
	CHANNEL_GUNBROKER,
	committed_units,
	notify_channel_commitments_changed,
	serial_shelf_state,
	uniform_free_serials,
)
from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction
from ffl_core.utils import single_flag, single_raw
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerError,
	get_field,
	gb_datetime,
	kv_key,
)
from ffl_integrations.gunbroker.crosschannel import REASON_SOLD, fire_serial_channel_exit
from ffl_integrations.integration_gate import gunbroker_enabled
from ffl_integrations.notifications import notify_admins, safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"
ORDER_DOCTYPE = "GunBroker Order"
SYNC_JOB_ID = "gb-order-sync"

# Hidden Single field holding a random token the webhook receiver rewrites on every
# nudge. See request_order_sync().
NUDGE_FIELD = "order_sync_nudge"

# Passes one job will make when nudges keep arriving while it works. A burst of
# webhooks (an auction ending with a dozen watchers) must not turn one job into an
# endless loop; whatever is left is picked up by the next nudge or the 15-minute poll.
MAX_NUDGE_PASSES = 3

# How far back of its own watermark each poll reaches. Clock skew between us and
# GunBroker is unknown and unknowable; a fifteen-minute overlap costs a handful
# of redundant reads (all idempotent on gb_order_id) and is the difference
# between "an order arrived a minute early" and "an order was never seen".
WATERMARK_OVERLAP_MINUTES = 15

MAX_PAGES = 50

# After this many consecutive failures an order stops blocking the watermark.
# Not "stops being retried" — the poll still picks it up, it just no longer
# holds every later sale hostage.
MAX_CONSECUTIVE_FAILURES = 5

# GunBroker's order status enumeration.
GB_STATUS_LABELS: dict[int, str] = {
	1: "Pending Seller Review",
	2: "Pending Buyer Confirmation",
	3: "Pending Payment (Received)",
	4: "Pending Shipment",
	5: "Complete",
	6: "Cancelled",
	7: "Pending Buyer Review",
	10: "On Layaway",
	11: "Payment In Process",
	12: "Returned",
	13: "Refund Order",
}

LAYAWAY_STATUS = 10
CANCELLED_STATUS = 6
RETURNED_STATUS = 12
REFUND_STATUS = 13

# The statuses that mean this sale is not going to complete as a sale.
UNWOUND_STATUSES = frozenset({CANCELLED_STATUS, RETURNED_STATUS, REFUND_STATUS})

# GunBroker's payment-method bitmask, as far as it is documented.
PAYMENT_METHOD_BITS: tuple[tuple[int, str], ...] = (
	(2, "Certified Funds"),
	(4, "Personal Check"),
	(8, "Visa/MasterCard"),
	(16, "COD"),
	(32, "GunTab"),
	(64, "Amex"),
	(128, "Discover"),
	(512, "PayPal"),
	(8192, "Certified Check"),
	(16384, "USPS Money Order"),
	(32768, "Money Order"),
)

_CARD_METHODS = frozenset({"Visa/MasterCard", "Amex", "Discover"})

SOURCE_GATEWAY_CHECKOUT = "Gateway (Immediate Checkout)"
SOURCE_CHECK = "Check or Money Order"
SOURCE_GATEWAY_GB_SET = "Gateway (GB-set)"
SOURCE_UNKNOWN = "Unknown"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def sync_orders() -> None:
	"""Cron entry (every 15 minutes). Gate, then enqueue; never raises.

	A scheduler tick that throws takes the whole tick down with it, including
	the other integrations' jobs, so every branch out of here is a return.
	"""
	try:
		if not gunbroker_enabled():
			return
		if not single_flag(SETTINGS_DOCTYPE, "auto_sync_enabled", default=1):
			return
		frappe.enqueue(
			"ffl_integrations.gunbroker.orders.run_order_sync",
			queue="long", job_id=SYNC_JOB_ID, deduplicate=True)
	except Exception:  # noqa: BLE001 — a gate must not break the tick
		safe_log_error("GunBroker order sync could not be enqueued",
			frappe.get_traceback())


def request_order_sync() -> None:
	"""Pull GunBroker orders as soon as a worker is free (the webhook receiver's nudge).

	``frappe.enqueue(..., job_id=SYNC_JOB_ID, deduplicate=True)`` alone is not enough
	here. Frappe counts a job that is already RUNNING as "queued", so a nudge that
	arrives while the poll is mid-way is dropped — and the poll may already have
	read ``/OrdersSold`` past the point where this order appeared, which leaves it
	waiting for the next 15-minute cron. So the nudge is also written down: a fresh
	random token in the database (not the cache, which a deploy flushes) that
	:func:`run_order_sync` compares against the one it started with, and goes round
	again when they differ.

	Committed before the enqueue so the worker that picks the job up (or the running
	one, at the end of its pass) is guaranteed to see the token.

	# lazy: a nudge landing in the few milliseconds between the running job's last
	# token check and its status flipping to finished is still dropped; the 15-minute
	# poll covers it. Upgrade path: a per-run lock row instead of a token.
	"""
	frappe.db.set_single_value(SETTINGS_DOCTYPE, NUDGE_FIELD, frappe.generate_hash(length=16),
		update_modified=False)
	safe_commit()
	frappe.enqueue("ffl_integrations.gunbroker.orders.run_order_sync",
		queue="long", job_id=SYNC_JOB_ID, deduplicate=True)


def run_order_sync() -> dict:
	"""Fetch everything changed since the watermark and write it down.

	Returns a summary for the caller/test: how many orders were seen, ingested,
	failed, and where the watermark ended up (of the last pass), plus how many
	passes ran.

	Runs again, up to :data:`MAX_NUDGE_PASSES` times, while webhook nudges keep
	arriving during a pass (see :func:`request_order_sync`).
	"""
	summary: dict = {}
	passes = 0
	while passes < MAX_NUDGE_PASSES:
		started_with = single_raw(SETTINGS_DOCTYPE, NUDGE_FIELD)
		summary = _sync_pass()
		passes += 1
		if summary.get("unreadable"):
			break  # the batch could not be read at all; a rerun would only repeat it
		# The pass committed, so this read opens a fresh snapshot and sees a nudge
		# that landed while it ran.
		if single_raw(SETTINGS_DOCTYPE, NUDGE_FIELD) == started_with:
			break
	summary["passes"] = passes
	return summary


def _sync_pass() -> dict:
	"""One poll of ``/OrdersSold`` from the watermark."""
	summary = {"seen": 0, "ingested": 0, "failed": 0, "watermark": None}
	if not gunbroker_enabled():
		return summary

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	since = _query_floor(settings)
	try:
		client = GunBrokerClient.from_settings()
		rows = _collect_rows(client, since, settings)
	except GunBrokerError as exc:  # GunBrokerConfigError is one of these
		# The batch could not be read at all. The watermark does NOT move: not
		# reading is not the same as reading nothing, and advancing here would
		# skip every order that changed during the outage.
		_record_batch_failure(exc)
		summary["unreadable"] = True
		return summary

	summary["seen"] = len(rows)
	advance_to = None
	blocked = False
	for index, row in enumerate(rows):
		outcome = _ingest_with_savepoint(client, row, index)
		if outcome == "failed":
			summary["failed"] += 1
			blocked = True
			continue
		if outcome == "ingested":
			summary["ingested"] += 1
		# "ingested" and "abandoned" both stop being an obstacle. An abandoned
		# order (five failures running) has been alerted on; leaving it pinning
		# the watermark would mean every later sale is re-fetched for ever and
		# the store never notices the newest one is fine.
		if not blocked:
			advance_to = _row_modified(row) or advance_to

	if advance_to:
		_set_watermark(advance_to, count=summary["ingested"])
		summary["watermark"] = advance_to
	safe_commit()
	return summary


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _query_floor(settings) -> datetime:
	"""The ``OrdersModifiedSinceDate`` this run asks from, as aware UTC.

	Start of life is NOW, deliberately (see the module docstring).
	``orders_since_override`` pulls the floor back when a human wants history,
	and it wins whenever it is earlier than where we would otherwise start — so
	setting that one field is enough to back-fill, without also having to clear
	the watermark by hand.

	The two fields are read in DIFFERENT timezones, on purpose. The watermark is
	machine state and is stored in UTC. The override is typed by a person into a
	Desk date control, where the only reading that could possibly be meant is
	the store's own clock. Reading either one in the other's zone shifts the
	floor by the whole offset — six hours of orders skipped, or six re-fetched.
	"""
	watermark = _load_utc(settings.get("last_orders_synced_at"))
	override = _load_site(settings.get("orders_since_override"))
	floor = watermark or override or datetime.now(timezone.utc)
	if override and override < floor:
		floor = override
	if override:
		# Consumed, once, and then cleared. Left in place it is not a back-fill
		# at all but a permanent floor: every tick from then on re-reads
		# everything modified since that moment — up to fifty pages, and a
		# GET /Orders/{id} for every row on them — for as long as nobody
		# notices. The watermark is pulled back to the floor BEFORE the field
		# is cleared, so a run that dies half way through resumes from there
		# instead of the back-fill vanishing with the field.
		_set_watermark(floor, count=None)
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "orders_since_override", None)
		_announce_backfill(floor)
	elif not watermark:
		# Persist it immediately. Without this, a run that dies mid-batch starts
		# from "now" again next tick, so the floor ends up being defined by
		# whenever a run happened to survive rather than by when we went live.
		_set_watermark(floor, count=None)
	return floor - timedelta(minutes=WATERMARK_OVERLAP_MINUTES)


def _announce_backfill(floor: datetime) -> None:
	"""Say that the override was taken, and that it is gone.

	Somebody typed a date into Settings and the next thing that happens is a
	poll reaching back weeks. Saying so is half of it; the other half is saying
	the field is now empty, so nobody re-types it wondering why the box went
	blank and starts the whole re-read again.
	"""
	notify_admins(
		_("GunBroker order back-fill has started"),
		_("The GunBroker order poll has taken Orders Since Override and is "
			"reading everything modified since {0} (store time). This is a "
			"one-off: the watermark has been moved back to that moment and the "
			"override field has been emptied, so the next poll carries on "
			"normally from wherever this run reaches.\n\nThe first round may "
			"take a while — every order it finds is fetched in full."
		).format(floor.astimezone(_site_tz_for_message()).strftime(
			"%Y-%m-%d %H:%M")),
		dedup_key="gb_orders_backfill", window_minutes=60)


def _site_tz_for_message():
	from ffl_integrations.gunbroker.client import _site_tz

	return _site_tz()


def _load_utc(value) -> datetime | None:
	"""A stored watermark as aware UTC. A naive value IS UTC here."""
	if not value:
		return None
	if isinstance(value, datetime):
		dt = value
	else:
		try:
			dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
		except ValueError:
			return None
	return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
		else dt.astimezone(timezone.utc)


def _load_site(value) -> datetime | None:
	"""An operator-entered datetime as aware UTC. A naive value is SITE time."""
	if not value:
		return None
	if isinstance(value, datetime) and value.tzinfo is not None:
		return value.astimezone(timezone.utc)
	from ffl_integrations.gunbroker.client import _site_tz

	text = value if isinstance(value, datetime) else str(value).strip()
	try:
		dt = text if isinstance(text, datetime) else datetime.fromisoformat(text)
	except ValueError:
		return None
	return dt.replace(tzinfo=_site_tz()).astimezone(timezone.utc)


def _set_watermark(value: datetime, *, count: int | None) -> None:
	"""Store the watermark as a naive UTC string.

	Naive because that is what a Frappe Datetime column holds, and UTC because
	:func:`_load_utc` reads it back that way — the pair has to agree, and the
	field's description says so for anyone reading it in the Desk.
	"""
	stamp = value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "last_orders_synced_at", stamp)
	if count is not None:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "last_order_sync_count", count)


def _record_batch_failure(exc: Exception) -> None:
	message = f"{type(exc).__name__}: {exc}"
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "last_sync_error", message[:2000])
	safe_log_error("GunBroker order sync failed", frappe.get_traceback())
	notify_admins(
		_("GunBroker order sync failed"),
		_("The GunBroker order poll could not read /OrdersSold: {0}. The "
			"watermark was not advanced, so nothing has been skipped — but no "
			"new GunBroker sale will reach POS until this clears."
		).format(message[:500]),
		dedup_key="gb_order_sync", window_minutes=60)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _collect_rows(client: GunBrokerClient, since, settings) -> list[dict]:
	"""Every ``/OrdersSold`` row since ``since``, de-duplicated, oldest first.

	Sorted here rather than by the API because no ``Sort`` code has ever been
	observed from this account (see the client). The order matters: the
	watermark only advances past orders that succeeded, and "past" is only
	meaningful if they are processed in modification order.
	"""
	page_size = int(settings.get("page_size") or 300)
	seen: dict[str, dict] = {}
	for row in client.iter_orders_sold(since, page_size=page_size, max_pages=MAX_PAGES):
		order_id = _order_id(row)
		if order_id:
			seen[order_id] = row
	rows = list(seen.values())
	rows.sort(key=lambda r: (_row_modified(r) or "", _order_id(r)))
	return rows


def _order_id(row: dict) -> str:
	value = get_field(row, "orderID", "OrderID", "orderId", default="")
	return str(value or "").strip()


def _row_modified(row: dict):
	return gb_datetime(row, "lastModifiedDate") or gb_datetime(row, "orderDate")


def _ingest_with_savepoint(client: GunBrokerClient, row: dict, index: int) -> str:
	"""Ingest one order inside a savepoint. Returns ingested/failed/abandoned.

	A savepoint, never a bare rollback: this runs in a long job that may have
	committed nothing yet, and ``frappe.db.rollback()`` would throw away every
	order already written this round (and, under test, the fixtures).
	"""
	order_id = _order_id(row)
	savepoint = f"gb_ord_{index}"
	frappe.db.savepoint(savepoint)
	try:
		_ingest_order(client, row)
		safe_commit()
		return "ingested"
	except Exception as exc:  # noqa: BLE001 — one order must not stop the batch
		frappe.db.rollback(save_point=savepoint)
		safe_log_error(f"GunBroker order {order_id} could not be ingested",
			frappe.get_traceback())
		return _record_order_failure(order_id, exc)


def _record_order_failure(order_id: str, exc: Exception) -> str:
	"""Write the failure onto the order if we have one, and say whether it still
	blocks the watermark."""
	message = f"{type(exc).__name__}: {exc}"[:1000]
	name = frappe.db.get_value(ORDER_DOCTYPE, {"gb_order_id": order_id}, "name")
	if not name:
		# Nothing was written, so there is nowhere to hang the counter. It blocks:
		# an order we cannot even create is exactly the one not to step over.
		return "failed"
	fails = int(frappe.db.get_value(ORDER_DOCTYPE, name, "sync_fail_count") or 0) + 1
	frappe.db.set_value(ORDER_DOCTYPE, name,
		{"sync_error": message, "sync_fail_count": fails}, update_modified=False)
	safe_commit()
	if fails < MAX_CONSECUTIVE_FAILURES:
		return "failed"
	notify_admins(
		_("GunBroker order {0} keeps failing to sync").format(order_id),
		_("{0} ({1}) has failed to sync {2} times running: {3}\n\nIt is now being "
			"stepped over so it stops holding back every later order. It will keep "
			"being retried, but nothing will improve on its own — open it."
		).format(name, order_id, fails, message),
		dedup_key=f"gb_order_stuck_{order_id}", window_minutes=1440)
	return "abandoned"


# ---------------------------------------------------------------------------
# One order
# ---------------------------------------------------------------------------

_CHILD_TABLES = (("serials", "GunBroker Order Serial"), ("lines", "GunBroker Order Line"))


def reload_children_for_update(order) -> None:
	"""Re-read the order's child rows with a LOCKING read, after the order row lock.

	A plain read after a lock still answers from the transaction's snapshot
	(REPEATABLE READ), which a poll job takes long before it reaches this order and
	a request takes at its first query. A staff assignment committed while we
	waited for the lock would be invisible to it, and saving the document would
	quietly write the old rows back: the unit loses its gun while the gun stays
	held for the order. ``SELECT … FOR UPDATE`` reads the latest committed rows.
	"""
	for fieldname, doctype in _CHILD_TABLES:
		rows = frappe.db.sql(
			f"SELECT * FROM `tab{doctype}` WHERE parent = %s AND parenttype = %s "
			"AND parentfield = %s ORDER BY idx FOR UPDATE",
			(order.name, ORDER_DOCTYPE, fieldname), as_dict=True)
		order.set(fieldname, rows)


def _ingest_order(client: GunBrokerClient, row: dict) -> str:
	"""Create or update one GunBroker Order from its full detail. Returns name."""
	order_id = _order_id(row)
	if not order_id:
		raise GunBrokerError("GunBroker order row carries no orderID", terminal=True)
	detail = client.get_order(order_id) or {}
	# The list row and the detail disagree about nothing that matters, but the
	# detail is the one with lines on it, so it wins wherever both have a key.
	merged = {**(row or {}), **detail}

	name = frappe.db.get_value(ORDER_DOCTYPE, {"gb_order_id": order_id}, "name")
	if name:
		# Lock before reading the document: two workers polling the same order
		# (a manual "Sync Orders Now" alongside the cron) would otherwise both
		# load it, both write, and the later write would silently discard the
		# earlier one's line matches.
		frappe.db.get_value(ORDER_DOCTYPE, name, "name", for_update=True)
		order = frappe.get_doc(ORDER_DOCTYPE, name)
		reload_children_for_update(order)
	else:
		order = frappe.new_doc(ORDER_DOCTYPE)
		order.gb_order_id = order_id

	# What this order already owed other channels, before the poll rewrites it.
	committed_before = _owed_to_other_channels(order)

	apply_order_fields(order, merged)
	apply_order_lines(order, merged)
	order.flags.ignore_permissions = True
	order.save()

	_warn_on_layaway(order)
	reserve_order_serials(order)
	_acknowledge(client, order)
	_detect_unwound(order)
	_announce_commitment_changes(committed_before, _owed_to_other_channels(order))
	return order.name


# ---------------------------------------------------------------------------
# What this channel has sold and not yet shipped (the shared pool)
# ---------------------------------------------------------------------------

def channel_committed_units(item_code: str) -> int:
	"""GunBroker's answer to ``ffl_core``'s ``channel_committed_units`` hook:
	units sold here and not yet shipped, so no other channel offers them again.

	An item-level line (ammunition, optics — a bare SKU naming a non-firearm Item)
	owes its quantity from the moment the order lands until it ships: the stock
	ledger does not move until Dispose, and GunBroker has already taken the buyer's
	money. Counted on ``Pending Fulfillment`` orders only — a shipped one has
	moved the Bin, a cancelled one never will.

	A uniform-price firearm owes a CLAIM per unit sold on a model-level listing:
	one ``GunBroker Order Serial`` row per unit, marked ``is_model_unit``, with no
	``confirmed_serial`` until staff name the gun. The row leaves the count the
	moment it is assigned (the gun is then held by the order and so is no longer
	free either) or marked refunded on GunBroker.

	Raw SQL rather than ``frappe.get_all``: the other channel's tests mock
	``get_all`` wholesale, and this is read from inside their stock rule.
	"""
	if not is_item_level_sku(item_code):
		row = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabGunBroker Order Serial` s
			INNER JOIN `tabGunBroker Order` o ON o.name = s.parent
			WHERE s.parenttype = 'GunBroker Order'
			  AND s.parentfield = 'serials'
			  AND s.item_code = %(item_code)s
			  AND s.is_model_unit = 1
			  AND (COALESCE(TRIM(s.confirmed_serial), '') = ''
			       OR COALESCE(TRIM(s.reservation_error), '') != '')
			  AND COALESCE(s.refunded_on_gb, 0) = 0
			  AND o.order_status = 'Pending Fulfillment'
			""",
			{"item_code": item_code},
		)
		return int(row[0][0] or 0) if row else 0
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(l.qty), 0) FROM `tabGunBroker Order Line` l
		INNER JOIN `tabGunBroker Order` o ON o.name = l.parent
		WHERE l.parenttype = 'GunBroker Order'
		  AND l.parentfield = 'lines'
		  AND l.sku = %(item_code)s
		  AND o.order_status = 'Pending Fulfillment'
		""",
		{"item_code": item_code},
	)
	return int(row[0][0] or 0) if row else 0


# How ``ffl_core.reservations.committed_units`` tells the channels apart.
channel_committed_units.channel = CHANNEL_GUNBROKER


def _item_level_commitments(order) -> dict[str, int]:
	"""``{item code: units}`` this order currently owes the other channels —
	its item-level lines, while it is still waiting to ship. What
	:func:`channel_committed_units` would count for it, read off the document."""
	if (order.order_status or "") != "Pending Fulfillment":
		return {}
	owed: dict[str, int] = {}
	for line in order.lines or []:
		if is_item_level_line(line):
			item_code, _serial = split_sku(line.sku or "")
			owed[item_code] = owed.get(item_code, 0) + max(0, _int(line.qty))
	return owed


def _claim_commitments(order) -> dict[str, int]:
	"""``{item code: open claims}`` — the order's model-level units that no gun is
	named for yet and that were not refunded. The claim half of
	:func:`channel_committed_units`, read off the document."""
	if (order.order_status or "") != "Pending Fulfillment":
		return {}
	owed: dict[str, int] = {}
	for row in order.serials or []:
		# A unit whose gun could not be re-held (reservation_error) is owed just like
		# an unassigned one: the buyer still needs a gun and none is held for them.
		if _int(row.is_model_unit) and not _int(row.refunded_on_gb) and row.item_code \
				and (not (row.confirmed_serial or "").strip()
					or (row.reservation_error or "").strip()):
			owed[row.item_code] = owed.get(row.item_code, 0) + 1
	return owed


def _owed_to_other_channels(order) -> dict[str, int]:
	"""Everything this order owes the other channels right now: item-level
	quantities and model-level claims (an Item is one kind or the other, so the
	two maps never share a key)."""
	return {**_item_level_commitments(order), **_claim_commitments(order)}


def _announce_commitment_changes(before: dict[str, int], after: dict[str, int]) -> None:
	"""Tell the other channels about every Item whose owed units moved.

	Only the CHANGE: an order comes back through ingest every time GunBroker
	touches it (payment, FFL, shipping), and re-announcing an unchanged one would
	queue a stock push per channel on every poll for the life of the order.
	Covers a new order, a quantity edit, and an unwound one (its status left
	``Pending Fulfillment``, so it owes nothing any more). Never raises.
	"""
	for item_code in sorted(set(before) | set(after)):
		if before.get(item_code, 0) != after.get(item_code, 0):
			notify_channel_commitments_changed(item_code, source=CHANNEL_GUNBROKER)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

def apply_order_fields(order, src: dict) -> None:
	"""Copy GunBroker's order onto ours.

	Read through ``get_field`` rather than ``src.get``: GunBroker is not
	consistent about capitalisation between endpoints, and a key read under the
	wrong spelling silently becomes a zero — which for ``complianceFee`` would
	mean posting a tax split that is wrong by the buyer's whole service fee.
	"""
	# An enum: GET /Orders reads it back as ``{"3": "Pending Payment"}``.
	status = _int(kv_key(get_field(src, "status", "orderStatus", "Status")))
	order.gb_status = status
	order.gb_status_label = GB_STATUS_LABELS.get(status, "")
	order.order_date = _site_stamp(src, "orderDate")
	order.gb_last_modified = _site_stamp(src, "lastModifiedDate")

	buyer = get_field(src, "buyer", "Buyer") or {}
	order.buyer_user_id = str(get_field(buyer, "userID", "userId", default="") or "").strip()
	order.buyer_username = str(get_field(buyer, "username", "userName", default="") or "").strip()
	order.customer_name = _text(get_field(src, "billToName", "buyerName"))
	order.customer_email = _text(get_field(src, "billToEmail", "buyerEmail"))
	order.customer_phone = _text(get_field(src, "billToPhone", "buyerPhone"))
	order.tax_exempt = 1 if get_field(src, "taxExempt") else 0

	order.ship_to_name = _text(get_field(src, "shipToName"))
	order.ship_to_line1 = _text(get_field(src, "shipToAddress1", "shipToAddress"))
	order.ship_to_line2 = _text(get_field(src, "shipToAddress2"))
	order.ship_to_city = _text(get_field(src, "shipToCity"))
	order.ship_to_state = _text(get_field(src, "shipToState"))
	order.ship_to_zip = _text(get_field(src, "shipToPostalCode", "shipToZip"))

	# The licence number is the one incoming field a human is allowed to
	# correct, so an already-corrected number is never overwritten by the
	# original the buyer typed: GunBroker is not going to change its mind, and
	# clobbering it would send the gun back to the wrong dealer.
	incoming_ffl = _text(get_field(src, "fflNumber"))
	if incoming_ffl and not (order.ffl_number or "").strip():
		order.ffl_number = incoming_ffl
	order.ffl_received = 1 if get_field(src, "fflReceived") else 0
	order.ffl_received_at = _site_stamp(src, "fflReceivedDate")
	order.ffl_status = _text(get_field(src, "fflStatus"))
	order.effl_url = _text(get_field(src, "eFFLUrl", "efflUrl"))
	order.seller_packet_url = _text(get_field(src, "sellerPacketURL", "sellerPacketUrl"))

	order.payment_received = 1 if get_field(src, "paymentReceived") else 0
	order.payment_received_at = _site_stamp(src, "paymentReceivedDate")
	methods = decode_payment_methods(get_field(src, "paymentMethods"))
	order.payment_methods = ", ".join(methods)
	order.payment_log_amount = _money(get_field(src, "paymentLogAmount"))
	order.payment_log_transaction_id = _text(get_field(src, "paymentLogTransactionID",
		"paymentLogTransactionId"))
	order.payment_log_date = _site_stamp(src, "paymentLogDateTime")
	order.payment_log_cleared = 1 if get_field(src, "paymentLogCleared") else 0
	order.payment_source = classify_payment_source(order, methods)
	_warn_on_gateway_payment(order)

	order.currency = _text(get_field(src, "currency")) or "USD"
	order.order_total = _money(get_field(src, "totalPrice", "orderTotal"))
	order.ship_cost = _money(get_field(src, "shipCost"))
	order.ship_handling_cost = _money(get_field(src, "shipHandlingCost"))
	order.ship_insurance_cost = _money(get_field(src, "shipInsuranceCost"))
	order.sales_tax_total = _money(get_field(src, "salesTaxTotal"))
	order.compliance_fee = _money(get_field(src, "complianceFee"))
	order.buyers_premium = _money(get_field(src, "buyersPremium"))
	order.coupon_value = _money(get_field(src, "couponValue"))
	order.lost_cash_discount = _money(get_field(src, "lostCashDiscount"))
	order.misc_charge = _money(get_field(src, "miscCharge"))
	order.misc_fee = _money(get_field(src, "miscFee"))

	order.cancel_date = _site_stamp(src, "cancelDate")
	order.refund_date = _site_stamp(src, "refundDate")
	order.returned_date = _site_stamp(src, "returnedDate")
	order.is_chargeback = 1 if get_field(src, "isChargeback") else 0

	order.raw_payload = frappe.as_json(src)[:100000]
	order.sync_error = ""
	order.sync_fail_count = 0

	# Our own state, which GunBroker does not own. A shipped-and-complete order
	# is still Pending Fulfillment here until the bound book says otherwise.
	if not (order.order_status or "").strip():
		order.order_status = "Pending Fulfillment"


def _site_stamp(src: dict, key: str):
	"""One GunBroker timestamp, as the site's own wall clock and tz-naive.

	:func:`gb_datetime` resolves the ambiguity in GunBroker's clocks — the
	``…UTC`` twin where there is one, Eastern where there is not — and hands
	back an AWARE UTC value. A Frappe Datetime column holds a naive value in
	site time and ``get_valid_dict`` stringifies whatever it is given, so
	assigning that aware value stores the UTC wall clock: five or six hours
	ahead of Texas, on the fields the Desk shows, the ones the Pending Order
	page groups the day's cards by, and the ones the cash-basis schedule
	compares against a local period end. Converting once, here, is what lets
	every reader downstream just read the column (LD-20).

	Deliberately NOT what the poll's own watermark does. That one is machine
	state: ``_set_watermark`` / ``_load_utc`` keep it in UTC because it is
	handed straight to a GunBroker query filter that demands UTC.
	"""
	value = gb_datetime(src, key)
	if value is None:
		return None
	from ffl_integrations.gunbroker.client import _site_tz

	return value.astimezone(_site_tz()).replace(tzinfo=None)


def apply_order_lines(order, src: dict) -> None:
	"""Rebuild the line table from ``orderItemsCollection``.

	Rebuilt rather than merged, with one exception: a line that already carries
	an accrued fee keeps it. The fee is what GunBroker bills us, it was computed
	from the price at the time of sale, and a partial refund later does not
	reduce it — recomputing on every poll would quietly re-price a liability
	that has already been journalled.
	"""
	rows = get_field(src, "orderItemsCollection", "items", "orderItems") or []
	previous = {(_text(r.gb_item_id), _text(r.sku)): r for r in (order.lines or [])}
	order.set("lines", [])
	for raw in rows:
		item_id = str(get_field(raw, "itemID", "itemId", default="") or "").strip()
		sku = _text(get_field(raw, "sku", "SKU"))
		item_code, serial_from_sku = split_sku(sku)
		line = order.append("lines", {
			"gb_item_id": item_id,
			"sku": sku,
			"title": _text(get_field(raw, "title")),
			"qty": _int(get_field(raw, "quantity")) or 1,
			"unit_price": _money(get_field(raw, "itemPrice")),
			"subtotal": _money(get_field(raw, "itemSubTotal", "itemSubtotal")),
			"line_tax": _money(get_field(raw, "salesTax")),
			"serial_number": _text(get_field(raw, "serialNumber")),
			"is_ffl_required": 1 if get_field(raw, "isFFLRequired") else 0,
		})
		if item_code and frappe.db.exists("Item", item_code):
			line.item_code = item_code
		elif serial_from_sku:
			# The SKU parsed but names an Item we do not have. Left unset rather
			# than guessed at; the matcher below records why.
			pass
		kept = previous.get((item_id, sku))
		if kept is not None:
			line.fvf_amount = kept.fvf_amount
			line.ffl_doc_fee_amount = kept.ffl_doc_fee_amount
			line.reservation_error = kept.reservation_error
	order.item_subtotal = sum(_money(line.subtotal) for line in order.lines)


def decode_payment_methods(value: Any) -> list[str]:
	"""GunBroker's payment bitmask, in words.

	Arrives either as an integer or as the single-entry object the API uses for
	every other enumeration (``{"8": "Visa / MasterCard"}``). Both are handled;
	an unrecognised bit is reported as its number rather than dropped, because a
	payment method we do not know about is exactly the thing somebody needs to
	see.
	"""
	if value in (None, "", 0):
		return []
	if isinstance(value, dict):
		mask = 0
		for key in value:
			try:
				mask |= int(key)
			except (TypeError, ValueError):
				continue
		if not mask:
			return [str(v) for v in value.values() if v]
	else:
		try:
			mask = int(value)
		except (TypeError, ValueError):
			return [str(value)]
	out = [label for bit, label in PAYMENT_METHOD_BITS if mask & bit]
	known = 0
	for bit, _label in PAYMENT_METHOD_BITS:
		known |= bit
	leftover = mask & ~known
	if leftover:
		out.append(f"Unknown ({leftover})")
	return out


def classify_payment_source(order, methods: list[str]) -> str:
	"""Where the money came from, first rule that fits.

	What we recorded ourselves always wins. Everything else is inference from
	GunBroker's own fields, and in this phase every inference that says "a
	gateway" is an anomaly worth a human: no payment gateway is connected to
	the account, so card money should not exist.
	"""
	if (order.payment_mode_used or "").strip():
		return SOURCE_CHECK if "Card" not in order.payment_mode_used \
			else SOURCE_GATEWAY_CHECKOUT
	has_log = bool(order.payment_log_transaction_id or order.payment_log_amount
		or order.payment_log_date)
	if has_log and any(m in _CARD_METHODS for m in methods):
		return SOURCE_GATEWAY_CHECKOUT
	if order.payment_received and not has_log:
		# GunBroker says paid; we recorded nothing and there is no gateway trail.
		# Usually a click on their website ahead of the cheque arriving.
		return SOURCE_GATEWAY_GB_SET
	return SOURCE_UNKNOWN


def _warn_on_gateway_payment(order) -> None:
	"""Card money on an account with no card gateway is somebody's mistake.

	Both gateway readings are alerted on, not just the GB-set one. This phase
	deliberately connected nothing: the listings offer certified funds and
	money orders only, ``card_checkout_enabled`` is off, and the dispose gate
	opens on what POS recorded rather than on GunBroker's flag. So a card
	payment source means either GunBroker set paymentReceived themselves — the
	buyer clicked something, or their support did — or somebody recorded a card
	payment here through the dormant path. Neither is wrong on its own, and
	neither can be acted on automatically: the money may genuinely be in the
	bank, and nothing in POS knows.

	The gun does not move either way (the gate reads payment_mode_used, which a
	GB-set order does not have), so this is not a hold. It is the only thing
	that tells anybody it happened: without it these orders are invisible in
	the daily audit as well, because that one reads what POS recorded too.
	"""
	source = (order.payment_source or "")
	if not source.startswith("Gateway"):
		return
	notify_admins(
		_("GunBroker order {0} looks like a gateway payment").format(
			order.gb_order_id),
		_("{0} ({1}) has payment_source '{2}'.\n\nThis phase has no payment "
			"gateway connected to the GunBroker account: the listings take "
			"certified funds and money orders only. Card money against one of "
			"them needs a human to find out what actually happened before the "
			"gun ships — POS will not open the dispose gate on GunBroker's own "
			"payment flag, so the order will sit there until somebody records "
			"what was really banked with Mark Payment Received."
		).format(order.name, order.gb_order_id, source),
		dedup_key="gb_gateway_payment_unexpected", window_minutes=1440)


def split_sku(sku: str) -> tuple[str, str | None]:
	"""``item_code::serial`` → the pair. Same rule the web store's SKUs use."""
	sku = (sku or "").strip()
	if "::" in sku:
		item_code, serial = sku.split("::", 1)
		return item_code.strip(), serial.strip() or None
	return sku, None


# ---------------------------------------------------------------------------
# Line → serial matching and reservation
# ---------------------------------------------------------------------------

def reserve_order_serials(order) -> None:
	"""Match each line to a gun and hold it, or say clearly why not.

	The order is saved either way. A line that cannot be matched is a problem
	for a human, not a reason to drop a sale on the floor — GunBroker has
	already taken the buyer's money.

	Three kinds of line, told apart by the SKU:

	* item-level (a bare SKU naming a non-firearm): a quantity, no serial row;
	* model-level (a bare SKU naming a uniform-price firearm): one serial row per
	  unit with no gun named — a CLAIM — until staff assign one;
	* per-gun (``item_code::serial``): matched to that gun and held.

	The serial table is rebuilt on every poll, so what a person decided must be
	carried across the rebuild: a model-level unit keeps the gun staff assigned to
	it (and re-confirms the hold, which is idempotent), and keeps its refunded
	mark. Losing either on a re-poll would turn a finished assignment back into an
	open claim — hourly, for the life of the order.
	"""
	if (order.order_status or "") != "Pending Fulfillment":
		return
	claimed: dict[str, str] = {}
	# What staff decided about each model-level unit, keyed by its identity —
	# copied out BEFORE the table is cleared, because clearing drops the rows.
	prev = {(_text(r.gb_item_id), _int(r.unit_index)): {
			"confirmed_serial": _text(r.confirmed_serial),
			"assigned_by": r.assigned_by, "assigned_at": r.assigned_at,
			"refunded_on_gb": _int(r.refunded_on_gb),
		} for r in (order.serials or []) if _int(r.is_model_unit)}
	order.set("serials", [])
	for index, line in enumerate(order.lines or []):
		if is_item_level_line(line):
			# Nothing to match: an item-level listing sold N interchangeable
			# units of a model, and none of them is a numbered object. No serial
			# row, no reservation, and — the point of the branch — no unmatched
			# alert. Without it every box of ammunition sold would arrive as
			# "match it by hand before disposing", hourly, for an order that has
			# nothing to match.
			line.reservation_error = ""
			continue
		if is_model_unit_line(line):
			_reserve_model_line(order, line, prev, claimed)
			continue
		serial, why = _match_serial(line, claimed)
		if not serial:
			line.reservation_error = why
			_alert_unmatched(order, line, why)
			continue
		error, newly_held = _hold_serial(order, serial)
		order.append("serials", {
			# The gun's own Item wins over the line's when the SKU named one we
			# do not have: the disposition needs an Item and the serial knows
			# which one it is, so falling back here is the difference between a
			# bound-book entry and a validation error half way through disposing.
			"item_code": line.item_code or _serial_item(serial),
			"gb_item_id": line.gb_item_id,
			"unit_index": index,
			"confirmed_serial": serial,
			"reservation_error": error or "",
		})
		claimed[serial] = line.gb_item_id
		line.reservation_error = error or ""
		if error:
			_alert_conflict(order, serial, error)
		elif newly_held:
			# Only on the transition. This order comes back through here every
			# time GunBroker touches it, and re-announcing an exit we announced
			# an hour ago would queue a redundant delist against both storefronts
			# on every poll, for the whole life of the order.
			_announce_exit(serial)
	_release_dropped_units(order, prev)
	order.flags.ignore_permissions = True
	order.save()
	_alert_if_oversold(order)


def _reserve_model_line(order, line, prev: dict, claimed: dict[str, str]) -> None:
	"""One serial row per unit of a model-level line.

	A unit is an open claim until staff name a gun for it; nothing is held and no
	storefront is told anything, because no particular gun is spoken for yet — the
	count each channel offers drops through the claim, not through a flag on a gun.
	"""
	item_code = line.item_code or split_sku(line.sku or "")[0]
	item_id = _text(line.gb_item_id)
	line.reservation_error = ""
	for unit in range(max(1, _int(line.qty))):
		old = prev.pop((item_id, unit), None) or {}
		serial = old.get("confirmed_serial") or ""
		row = order.append("serials", {
			"item_code": item_code,
			"gb_item_id": line.gb_item_id,
			"unit_index": unit,
			"is_model_unit": 1,
			"confirmed_serial": serial,
			"assigned_by": old.get("assigned_by"),
			"assigned_at": old.get("assigned_at"),
			"refunded_on_gb": old.get("refunded_on_gb") or 0,
			"reservation_error": "",
		})
		if not serial:
			continue
		# Re-confirm the hold the assignment took. Idempotent when it is still
		# ours; a failure here means somebody else has the gun since, and it is
		# written on THIS row and the line, so the card and the dispose gate say so.
		error, newly_held = _hold_serial(order, serial)
		claimed[serial] = line.gb_item_id
		row.reservation_error = error or ""
		if error:
			line.reservation_error = line.reservation_error or error
			_alert_conflict(order, serial, error)
		elif newly_held:
			_announce_exit(serial)


def _release_dropped_units(order, prev: dict) -> None:
	"""Units that were in the table before this poll and are not any more (the
	line's quantity fell, or the line went): give back the gun staff had named,
	if this order still holds it, and say so — a person assigned that gun."""
	for (item_id, unit), old in prev.items():
		serial = old.get("confirmed_serial") or ""
		if not serial:
			continue
		release_serial_if_held_by(order.name, serial)
		notify_admins(
			_("GunBroker order {0}: a unit was dropped").format(order.gb_order_id),
			_("{0} ({1}) no longer has unit {2} of GunBroker item {3}, so serial {4} "
				"was released from it. Check the order on GunBroker."
			).format(order.name, order.gb_order_id, unit + 1, item_id, serial),
			dedup_key=f"gb_unit_dropped_{order.gb_order_id}", window_minutes=1440)


def _alert_if_oversold(order) -> None:
	"""Say so when this order's claims cannot all be filled from the free guns.

	The claim is recorded either way — GunBroker has taken the money and cannot
	cancel from our side — but somebody has to know NOW that a buyer will be
	short, while a refund or a restock can still fix it. Worked out from the free
	pool and the owed units directly: ``uniform_sellable_units`` is floored at
	zero and so cannot tell "sold out" from "oversold". A channel that cannot
	answer is logged and skipped; an alert is never worth losing the order for.
	"""
	for item_code in sorted(_claim_commitments(order)):
		try:
			free = len(uniform_free_serials(item_code))
			owed = sum(committed_units(item_code).values())
		except Exception:  # noqa: BLE001 — see above
			safe_log_error(f"GunBroker oversell check failed for {item_code}",
				frappe.get_traceback())
			continue
		if owed <= free:
			continue
		notify_admins(
			_("GunBroker order {0}: {1} is oversold").format(order.gb_order_id, item_code),
			_("{0} ({1}) sold a unit of {2}, but only {3} free gun(s) remain against "
				"{4} unit(s) owed across all channels. The order was recorded anyway. "
				"Restock, or refund the buyer on GunBroker and mark the unit "
				"'refunded on GunBroker' on the Pending Order card."
			).format(order.name, order.gb_order_id, item_code, free, owed),
			dedup_key=f"gb_oversold_{order.gb_order_id}_{item_code}", window_minutes=1440)


def is_item_level_line(line) -> bool:
	"""True when this line sold stock by the unit rather than one numbered gun.

	The SKU tells the two listing kinds apart on its own: a per-gun listing
	carries ``item_code::serial``, an item-level one the bare item code. That is
	the primary test and the reliable one, because we minted both and GunBroker
	keeps a SKU verbatim across its relists.

	Two conditions have to hold together, and both are deliberate:

	* the SKU names an Item that EXISTS here — an unrecognised SKU is a listing
	  we did not create (or an Item since renamed), and treating that as
	  "item-level, nothing to reserve" would silently swallow a gun sale;
	* that Item is not a firearm — a bare-SKU listing for a gun should be
	  impossible, but if one ever appears the line must keep going down the
	  serial-matching path and raise the unmatched alert, not be quietly waved
	  through as stock.
	"""
	return is_item_level_sku(line.sku or "")


def is_item_level_sku(sku: str) -> bool:
	""":func:`is_item_level_line`'s test, on a bare SKU (the webhook receiver has no line)."""
	item_code, serial = split_sku(sku)
	if serial or not item_code:
		return False
	is_firearm = frappe.db.get_value("Item", item_code, "is_firearm")
	if is_firearm is None:  # no such Item
		return False
	return not frappe.utils.cint(is_firearm)


def is_model_unit_line(line) -> bool:
	"""True when this line sold units of a uniform-price firearm MODEL.

	A bare SKU (no ``::serial``) naming an Item that exists, is a firearm and is
	priced as one product for all its guns. Disjoint from
	:func:`is_item_level_line` (which needs a NON-firearm), so a model-level line
	can never be issued from stock by quantity — it leaves under dispositions, one
	per gun. A bare SKU naming a per-gun-priced firearm is neither, and keeps
	going down the serial-matching path to the unmatched alert: such a listing
	could only be made by hand on the website, and waving it through would ship a
	gun with nobody having named it.
	"""
	item_code, serial = split_sku(line.sku or "")
	if serial or not item_code:
		return False
	item = frappe.db.get_value(
		"Item", item_code, ["is_firearm", "uniform_sell_price"], as_dict=True)
	return bool(item and _int(item.get("is_firearm")) and _int(item.get("uniform_sell_price")))


def _match_serial(line, claimed: dict[str, str]) -> tuple[str | None, str]:
	"""Which gun this line sold, by SKU then by serial number.

	SKU first because it is ours: we minted ``item_code::serial`` when the
	listing went up and GunBroker keeps it verbatim across relists. The
	serialNumber field is the fallback for a listing created before this
	integration, or by hand on the website.
	"""
	_item_code, serial = split_sku(line.sku or "")
	if serial and _serial_exists(serial):
		if serial in claimed:
			return None, _("Serial {0} is already claimed by another line on this "
				"order (item {1}).").format(serial, claimed[serial])
		return serial, ""
	reported = (line.serial_number or "").strip()
	if reported and _serial_exists(reported):
		if reported in claimed:
			return None, _("Serial {0} is already claimed by another line on this "
				"order (item {1}).").format(reported, claimed[reported])
		return reported, ""
	return None, _(
		"Unmatched GunBroker item {0}: neither the SKU ({1}) nor the reported "
		"serial ({2}) names a Serial No in POS. Match it by hand before disposing."
	).format(line.gb_item_id or "?", line.sku or "—", reported or "—")


def _serial_item(serial: str) -> str:
	return frappe.db.get_value("Serial No", serial, "item_code") or ""


def _serial_exists(serial: str) -> bool:
	return bool(serial) and bool(frappe.db.exists("Serial No", serial))


def _hold_serial(order, serial: str) -> tuple[str, bool]:
	"""Reserve one gun for this order under a row lock.

	Returns ``(reason_it_could_not, newly_held)`` — "" for the first when the
	gun is ours.

	The self-exemption is the whole reason this is a function and not three
	lines inline. An order comes back through here every time GunBroker touches
	it — payment, FFL, shipment — and without that check the order's own
	reservation reads as "held by somebody else", which would put a false
	conflict on the line and mail the store about it, hourly, for ever.

	The lock is taken first and everything else is read inside it. The whole
	point is that between "is this gun free?" and "then it is mine" nobody else
	gets to answer the same question.
	"""
	locked = frappe.db.get_value("Serial No", serial,
		["name", "gb_reserved", "gb_order", "gb_bid_locked"], as_dict=True, for_update=True)
	if not locked:
		return _("Serial {0} no longer exists in POS.").format(serial), False
	if locked.get("gb_reserved") and (locked.get("gb_order") or "") == order.name:
		return "", False  # already ours — idempotent success
	if locked.get("gb_bid_locked"):
		# The auction's promise has become this order. Lifted BEFORE the shelf
		# check, which would otherwise report the gun as held — by us.
		frappe.db.set_value("Serial No", serial, "gb_bid_locked", 0, update_modified=False)
	state = serial_shelf_state(serial)
	if not state.get("sellable"):
		return _shelf_conflict(serial, state), False
	frappe.db.set_value("Serial No", serial,
		{"gb_reserved": 1, "gb_order": order.name}, update_modified=False)
	return "", True


def _shelf_conflict(serial: str, state: dict) -> str:
	detail = state.get("detail") or {}
	reason = state.get("reason") or "unavailable"
	if reason == "channel_reserved":
		return _("Held by {0} {1} — GunBroker has a buyer for serial {2} as well. "
			"Release the other channel or refund one of them."
		).format(detail.get("channel") or "another channel",
			detail.get("ref") or "", serial)
	if reason == "counter_hold":
		return _("Serial {0} is on counter order {1} — GunBroker has a buyer for "
			"it too."
		).format(serial, detail.get("ref") or "?")
	return _("Serial {0} is not sellable ({1}) — GunBroker has a buyer for it.").format(
		serial, reason)


def _announce_exit(serial: str) -> None:
	"""Tell the other storefronts to stop offering this gun. Never raises."""
	fire_serial_channel_exit(serial, reason=REASON_SOLD)


def _alert_unmatched(order, line, why: str) -> None:
	notify_admins(
		_("GunBroker order {0}: a line matched no gun").format(order.gb_order_id),
		_("{0} ({1})\n\n{2}\n\nThe order was recorded anyway — losing a sale is "
			"worse than recording one that needs a human."
		).format(order.name, order.gb_order_id, why),
		dedup_key=f"gb_unmatched_{order.gb_order_id}", window_minutes=1440)


def _alert_conflict(order, serial: str, why: str) -> None:
	notify_admins(
		_("GunBroker order {0}: serial {1} is spoken for").format(
			order.gb_order_id, serial),
		_("{0} ({1})\n\n{2}\n\nGunBroker sales cannot be cancelled from our side, "
			"so somebody has to decide which buyer gets the gun — now, not at "
			"packing time."
		).format(order.name, order.gb_order_id, why),
		dedup_key=f"gb_conflict_{order.gb_order_id}", window_minutes=60)


# ---------------------------------------------------------------------------
# Post-ingest reactions
# ---------------------------------------------------------------------------

def _warn_on_layaway(order) -> None:
	"""Layaway money is a deposit, not revenue — and this store does not offer it.

	A layaway order on the account is therefore either an old listing's policy
	or a mistake. Either way it must not be disposed: the fulfillment gate
	refuses it, and this is how somebody finds out.
	"""
	if int(order.gb_status or 0) != LAYAWAY_STATUS:
		return
	notify_admins(
		_("GunBroker order {0} is on layaway").format(order.gb_order_id),
		_("{0} ({1}) came back from GunBroker with status 10 (On Layaway). "
			"Instalments are a customer deposit, not a sale, so POS will not let "
			"this order be disposed and no invoice will be raised for it. Check "
			"the listing's payment terms on GunBroker."
		).format(order.name, order.gb_order_id),
		dedup_key=f"gb_layaway_{order.gb_order_id}", window_minutes=1440)


def _detect_unwound(order) -> None:
	"""Cancelled / returned / refunded: release or alert, never auto-reverse.

	Before the gun ships there is nothing on the ledger, so releasing the hold
	and closing the order is the whole job. Afterwards there is an invoice, a
	bound-book exit and money — and GunBroker recomputes its own tax statement
	when a refund is raised THERE, so a POS-side reversal would leave us paying
	tax on a sale that no longer exists. A human books it, following the
	store's GunBroker refund procedure; the alert adds the one rule the
	cash-basis report depends on (`marketplace_accounts.refund_remarks_rule`).
	"""
	status = int(order.gb_status or 0)
	if status not in UNWOUND_STATUSES:
		return
	if order.sales_invoice:
		notify_admins(
			_("GunBroker order {0} was {1} after invoicing").format(
				order.gb_order_id, order.gb_status_label or status),
			_("{0} ({1}) is now '{2}' on GunBroker, but Sales Invoice {3} is "
				"already posted. Nothing has been reversed automatically; which "
				"entries are right depends on whether the gun came back. Book the "
				"refund following your GunBroker refund procedure. {4}"
			).format(order.name, order.gb_order_id, order.gb_status_label or status,
				order.sales_invoice, refund_remarks_rule(order.gb_order_id)),
			dedup_key=f"gb_refund_{order.gb_order_id}", window_minutes=1440)
		return
	if (order.order_status or "") == "Transferred":
		# Shipped but not yet invoiced. The A&D exit stands; only the money is
		# unresolved, so this is the same conversation as above.
		notify_admins(
			_("GunBroker order {0} was {1} after shipping").format(
				order.gb_order_id, order.gb_status_label or status),
			_("{0} ({1}) is now '{2}' on GunBroker and the gun has already left "
				"under a booked disposition. Nothing has been reversed. Book the "
				"refund following your GunBroker refund procedure. {3}"
			).format(order.name, order.gb_order_id, order.gb_status_label or status,
				refund_remarks_rule(order.gb_order_id)),
			dedup_key=f"gb_refund_{order.gb_order_id}", window_minutes=1440)
		return
	release_order_serials(order, order_status="Cancelled")
	_announce_cancellation(order)


def _announce_cancellation(order) -> None:
	"""Cancelled before it was invoiced. Say so — especially if it was paid.

	Putting the gun back on the shelf is the whole of the POS-side job when
	nothing was booked, which is why this branch used to end there. But the
	money is a separate question: a cheque may already have been deposited, and
	the order carries the fact (`payment_mode_used`) while the ledger carries
	nothing at all — no invoice, no receipt, no refund. Nobody was told, and
	the only thing that would eventually notice is a daily check whose wording
	("paid and not shipped") describes something else.

	The refund is deliberately not automated: it is two unreferenced Payment
	Entries and a decision about who is owed what.
	"""
	banked = (order.payment_mode_used or "").strip()
	if banked:
		subject = _("GunBroker order {0} was cancelled after payment was banked "
			"— refund due").format(order.gb_order_id)
		body = _(
			"{0} ({1}) is cancelled on GunBroker and the gun is back on the "
			"shelf. The money is not: {2} was banked on {3} against cheque {4} "
			"({5}), and there is no invoice and no receipt for it — so nothing "
			"on the ledger reflects either the sale or the refund.\n\nIt is an "
			"unreferenced Receive entry reversed by a Pay entry, not an invoice "
			"credit, because no invoice was ever raised. Book the refund "
			"following your GunBroker refund procedure. {6}"
		).format(order.name, order.gb_order_id, order.payment_amount,
			order.payment_received_on or "—", order.payment_reference_no or "—",
			banked, refund_remarks_rule(order.gb_order_id, receive_too=True))
	else:
		subject = _("GunBroker order {0} was cancelled").format(order.gb_order_id)
		body = _(
			"{0} ({1}) is cancelled on GunBroker before it was invoiced. Nothing "
			"was booked and nothing is owed; the gun has been released and is "
			"back on sale on the other channels."
		).format(order.name, order.gb_order_id)
	notify_admins(subject, body,
		dedup_key=f"gb_cancel_{order.gb_order_id}", window_minutes=1440)


def release_serial_if_held_by(order_name: str, serial: str) -> bool:
	"""Drop one gun's GunBroker hold, but only if THIS order holds it. Never
	another order's or another channel's. True when something was released."""
	serial = (serial or "").strip()
	if not serial:
		return False
	held = frappe.db.get_value("Serial No", serial, ["gb_reserved", "gb_order"],
		as_dict=True) or {}
	if held.get("gb_reserved") and (held.get("gb_order") or "") == order_name:
		frappe.db.set_value("Serial No", serial,
			{"gb_reserved": 0, "gb_order": ""}, update_modified=False)
		return True
	return False


def release_order_serials(order, *, order_status: str | None = None) -> list[str]:
	"""Drop this order's holds. Only its own — never another channel's."""
	released = []
	for row in (order.serials or []):
		serial = (row.confirmed_serial or "").strip()
		if serial and release_serial_if_held_by(order.name, serial):
			released.append(serial)
	if order_status and (order.order_status or "") != order_status:
		frappe.db.set_value(ORDER_DOCTYPE, order.name, "order_status", order_status,
			update_modified=False)
		order.order_status = order_status
	return released


def _acknowledge(client: GunBrokerClient, order) -> None:
	"""Take the order out of GunBroker's pending-seller-review queue.

	Low stakes both ways, which is why it is automatic: not acknowledging risks
	a review timeout, and acknowledging an order we have recorded says nothing
	untrue. A 403 means the DevKey is not cleared for it — that is a
	configuration fact, not a transient one, so it turns the behaviour off
	rather than retrying every fifteen minutes for ever.
	"""
	if order.acknowledged_at or not single_flag(SETTINGS_DOCTYPE, "auto_acknowledge",
			default=1):
		return
	try:
		client.acknowledge_order(order.gb_order_id)
	except GunBrokerError as exc:
		if exc.status_code == 403:
			frappe.db.set_single_value(SETTINGS_DOCTYPE, "auto_acknowledge", 0)
			notify_admins(
				_("GunBroker will not let us acknowledge orders"),
				_("PUT /Orders/{0}/Flags came back 403. Automatic acknowledgement "
					"has been switched off in GunBroker Settings so it stops "
					"retrying; acknowledge orders on the website, or ask GunBroker "
					"to authorise the DevKey for order flags."
				).format(order.gb_order_id),
				dedup_key="gb_ack_unauthorized", window_minutes=1440)
			return
		safe_log_error(f"GunBroker acknowledge failed for {order.gb_order_id}",
			frappe.get_traceback())
		return
	frappe.db.set_value(ORDER_DOCTYPE, order.name, "acknowledged_at", now_datetime(),
		update_modified=False)
	order.acknowledged_at = now_datetime()


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
	return str(value or "").strip()


def _int(value: Any) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


def _money(value: Any) -> float:
	from frappe.utils import flt

	return flt(value or 0)
