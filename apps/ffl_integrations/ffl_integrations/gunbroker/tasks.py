"""Background jobs: the per-gun reconcile, and the hourly sweep behind it.

The sweep exists because every OTHER path here is an event, and events go
missing. A queue job dies, a webhook is never delivered, GunBroker relists a gun
on its own thirty-day clock and gives it a new item number nobody told us about.
The sweep is the only part of this integration that asks GunBroker what is
actually true and reconciles POS to the answer.

Three rules run through all of it, and each one is a bug that has already been
reasoned about at length:

**Never conclude "gone" from one look.** `/ItemsSelling` is eventually
consistent in BOTH directions — a listing ended seconds ago lingers in it for
minutes, and a listing GunBroker has just relisted takes the same few minutes to
appear. A single miss is one observation, not a verdict. Everything that would
clear local state needs two rounds and a wall-clock age.

**Match on SKU, never on item number.** GunBroker mints a new item number every
time it relists, and with `AutoRelist=4` on a thirty-day listing that is every
gun, every month. The SKU does not change; GunBroker's own guidance is to sync
inventory on it. An item-number match fails, convincingly, exactly when a relist
has happened — turning "it was relisted" into "it does not exist".

**An unreadable answer is not an answer.** A scan row carrying neither a SKU nor
an item number cannot say anything about this gun. That is `unknown`, it touches
no counter and no field, and it alerts once. The failure it prevents is a
malformed page quietly ageing a live listing into ENDED.
"""
from __future__ import annotations

import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import (
	add_to_date, cint, convert_utc_to_system_timezone, flt, now_datetime,
	time_diff_in_hours,
)

from ffl_core.firearm import item_current_price
from ffl_core.reservations import serial_shelf_state, uniform_sellable_units
from ffl_integrations.gunbroker import item_listings, listings, mapping
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerConfigError,
	GunBrokerError,
	get_field,
	parse_gb_datetime,
)
from ffl_integrations.gunbroker.crosschannel import (
	REASON_BID,
	REASON_RESERVED,
	fire_serial_channel_exit,
)
from ffl_integrations.gunbroker.doc_events import gb_item_bid_locked, gb_item_sold
from ffl_integrations.notifications import notify_admins, safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"

SWEEP_JOB = "gb_listing_sweep"
MODEL_PRICE_SWEEP_JOB = "gb_model_price_sweep"

# How many guns one round looks at. A round holds a GunBroker session and a
# shared listing index; capping it keeps a catalogue of thousands from turning
# one hourly tick into an hour of work.
SWEEP_BATCH = 300

# Consecutive unusable probe answers before the round gives up. GunBroker being
# down is one fact, and grinding through three hundred guns to rediscover it
# three hundred times is not a better answer than stopping.
ERROR_CIRCUIT_BREAK = 5

# How long a listing has to keep looking un-findable before its item number is
# let go. The two thresholds are deliberately different, and it is the KIND of
# absence that decides which applies:
#
#   404 (`gone`)          — GunBroker stopped serving the item. It does that to
#                           old items on its own schedule, so a day of agreement
#                           is the bar.
#   200 but not active    — the relist window. `/ItemsSelling` catches up in
#                           minutes, so two hourly rounds is already generous.
GONE_MIN_HOURS = 24
ENDED_MIN_HOURS = 2
MIN_STRIKES = 2

# The pause between a 404 and the re-read that double-checks it. Short: this
# guards against a single edge node answering badly, not against an outage.
GONE_RECHECK_SECONDS = 1

# How long after an item-level listing ends the sweep keeps looking at its SKU.
# An end reads `relistedAsItemID` to find what replaced the listing, and a 0
# there is not evidence that nothing did — the field has only ever been observed
# already-set or already-0, never changing, and GunBroker support has said not
# to depend on it. An end that reads 0 therefore confirms against the number it
# deleted and clears `gb_item_id`, which turns all three of the live tests below
# false. Six hours is several rounds of margin, and the set it adds is only what
# has ended recently.
END_RECHECK_HOURS = 6


# ---------------------------------------------------------------------------
# Per-serial tasks
# ---------------------------------------------------------------------------

def gb_sync_serial_listing_task(serial_no: str) -> None:
	"""Re-decide ONE gun's GunBroker listing from its current shelf state.

	The single sink for all five document events. They each know something
	different and none of them knows enough, so none of them decides: they say
	"this gun changed" and the answer is worked out here, after commit, from the
	final state.

	Sellable → do NOTHING. Not a bug: a machine never re-lists on GunBroker.
	Listing a firearm on a marketplace is a decision with a fee and a contract
	behind it, and "the gun came back so we put it up for sale again" is not a
	decision a queue job gets to make. A person presses the button.

	Not sellable → end the listing.
	"""
	try:
		if not frappe.db.get_value("Serial No", serial_no, "gb_item_id"):
			return
		state = serial_shelf_state(serial_no)
		if state["sellable"]:
			return
		listings.end_listing(serial_no, reason=_shelf_reason(state))
	except Exception:  # noqa: BLE001 — a scheduler task never raises
		safe_log_error(f"GunBroker listing sync failed: {serial_no}",
			frappe.get_traceback())


def _shelf_reason(state: dict) -> str:
	"""The shelf verdict in words an alert can carry."""
	detail = state.get("detail") or {}
	reason = state.get("reason") or "unavailable"
	if reason == "channel_reserved":
		return f"reserved by {detail.get('channel')} order {detail.get('ref') or '?'}"
	if reason == "counter_hold":
		return f"held on {detail.get('ref') or 'a counter order'}"
	if reason == "not_active":
		return f"serial status is {detail.get('status') or 'not Active'}"
	return reason.replace("_", " ")


def gb_sync_item_quantity_task(item_code: str) -> None:
	"""Tell GunBroker how many of this model are left.

	Behind every stock movement of every listed item, so the comparison that
	skips an unchanged count lives in :func:`item_listings.sync_item_quantity`
	rather than here — a receipt into a second warehouse and a sale of a
	different line in the same document both arrive as a job.
	"""
	try:
		item_listings.sync_item_quantity(item_code)
	except Exception:  # noqa: BLE001 — a queue job never raises
		safe_log_error(f"GunBroker item quantity sync failed: {item_code}",
			frappe.get_traceback())


def gb_reprice_item_task(item_code: str) -> None:
	"""Send the CURRENT price for one MODEL, if it is still marked dirty.

	No `job_id`, no `deduplicate`, for the reason :func:`gb_reprice_serial_task`
	gives: a duplicate enqueue dropped while the first job runs would lose the second
	of two quick edits. The dirty bit and reading the price at send time make it
	idempotent.
	"""
	try:
		if not frappe.db.get_value("Item", item_code, "gb_price_dirty"):
			return
		item_listings.update_item_price(item_code)
	except Exception:  # noqa: BLE001
		safe_log_error(f"GunBroker model reprice failed: {item_code}", frappe.get_traceback())


def gb_end_item_listing_task(item_code: str, reason: str = "") -> None:
	"""End one Item's listing because the Item itself changed (it stopped being a
	uniform-price gun, or was disabled), with the "a person did this" sentinel so a
	machine never puts it back."""
	try:
		item_listings.end_item_listing(
			item_code, reason=reason or "ended by POS", delisted=True)
	except Exception:  # noqa: BLE001
		safe_log_error(f"GunBroker item end task failed: {item_code}", frappe.get_traceback())


def gb_model_price_sweep() -> None:
	"""00:05 cron: a sale window that opened or closed at midnight, for every model.

	`ffl_core.item_hooks.refresh_sale_item_prices` (daily) moves the register's price
	with a direct `set_value`, which fires no document event, so the Item `on_update`
	hook that normally reprices cannot see a promotion start or end. This is the
	thing that does, a few minutes after that sweep. Gated and enqueued like the
	hourly sweep: the gate never does the work on the scheduler's thread.
	"""
	from ffl_integrations.integration_gate import gunbroker_enabled

	if not gunbroker_enabled() or not scheduled_sync_enabled():
		return
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks._run_model_price_sweep",
		queue="long", job_name=MODEL_PRICE_SWEEP_JOB, job_id=MODEL_PRICE_SWEEP_JOB,
		deduplicate=True)


def _run_model_price_sweep() -> None:
	"""Mark and queue a reprice for every live model whose price is not the one
	GunBroker was last confirmed at. Never raises — it is a scheduled job."""
	try:
		from ffl_integrations.gunbroker.doc_events import mark_model_price_dirty

		rows = frappe.db.sql(
			"""
			SELECT name FROM `tabItem`
			WHERE COALESCE(is_firearm, 0) = 1 AND COALESCE(uniform_sell_price, 0) = 1
			  AND COALESCE(gb_item_id, '') != ''
			ORDER BY name LIMIT %(limit)s
			""", {"limit": SWEEP_BATCH}, as_dict=True)
		for row in rows:
			try:
				mark_model_price_dirty(row["name"])
			except Exception:  # noqa: BLE001 — one bad model must not end the round
				safe_log_error(f"GunBroker model price sweep failed for {row['name']}",
					frappe.get_traceback())
	except Exception:  # noqa: BLE001 — a scheduler job never raises
		safe_log_error("GunBroker model price sweep failed", frappe.get_traceback())


def gb_relist_item_task(item_code: str) -> None:
	"""Put a sold-out model back on GunBroker now that stock has arrived.

	Only reaches anything that has been listed before and was not taken down by
	hand; :func:`item_listings.relist_on_restock` re-checks both, because the
	job runs after commit and the row it was queued on may have moved since.
	"""
	try:
		item_listings.relist_on_restock(item_code)
	except Exception:  # noqa: BLE001
		safe_log_error(f"GunBroker item relist failed: {item_code}",
			frappe.get_traceback())


def gb_end_listing_task(serial_no: str, reason: str = "") -> None:
	"""End one listing, named explicitly rather than derived from shelf state.

	Separate from :func:`gb_sync_serial_listing_task` because some reasons to end
	a listing are not "the gun is unavailable" — a corrected serial number is the
	case today, where the gun is perfectly sellable and the LISTING is the thing
	that has gone wrong.
	"""
	try:
		listings.end_listing(serial_no, reason=reason or "ended by POS")
	except Exception:  # noqa: BLE001
		safe_log_error(f"GunBroker end listing task failed: {serial_no}",
			frappe.get_traceback())


def gb_reprice_serial_task(serial_no: str) -> None:
	"""Send the CURRENT price for one gun, if it is still marked dirty.

	No `job_id`, no `deduplicate`, deliberately. Frappe drops a duplicate enqueue
	while the first job is running, so two quick price edits would leave the
	second one unsent and the listing showing the first price with nothing marked
	dirty — a mispriced live listing that no sweep would notice. The dirty bit
	plus reading the price at send time already make this idempotent; a job id
	would only add a way to lose an edit.
	"""
	try:
		if not frappe.db.get_value("Serial No", serial_no, "gb_price_dirty"):
			return
		listings.update_price(serial_no)
	except Exception:  # noqa: BLE001
		safe_log_error(f"GunBroker reprice failed: {serial_no}", frappe.get_traceback())


def gb_warm_token() -> None:
	"""Put a GunBroker session in the cache so the till's next probe can use it.

	The counter probe refuses to log in inline — a login plus a read is most of
	its whole budget — so it asks for this instead and answers "unknown" once.
	"""
	try:
		client = GunBrokerClient.from_settings()
		if not client.cached_token():
			client.login()
	except Exception:  # noqa: BLE001 — warming is opportunistic
		pass


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def scheduled_sync_enabled() -> bool:
	"""Has the operator left the scheduled jobs switched on?

	`Run Scheduled Sync` is the form's own promise: "uncheck to stop the
	scheduled jobs (order polling, listing sweep) while leaving manual buttons
	available". So it gates the SWEEP and nothing else — not the document events
	that take a listing down when a gun is sold, and not the counter's refusal.
	Those are neither scheduled nor manual, and switching off a background job
	must not disarm the protection against selling a gun twice.

	Read through `single_flag`, deliberately NOT as
	``int(settings.auto_sync_enabled or 0)`` — which is what the RSR and
	WooCommerce gates do, and what this would have copied. A Single casts every
	Check through `cint()` as it loads, so a field nobody has saved arrives as 0
	and is indistinguishable from one deliberately unchecked. With that reading
	the docfield default of 1 never applies on a site that has stored ANY row for
	this Single, and the sweep would simply never run — silently, on exactly the
	sites that already have GunBroker configured.
	"""
	from ffl_core.utils import single_flag

	return single_flag(SETTINGS_DOCTYPE, "auto_sync_enabled", default=1)


def gb_listing_sweep() -> None:
	"""Hourly cron gate. Switched off → no outbound work at all.

	Two switches, and they mean different things. `Enable GunBroker Integration`
	off means nothing anywhere reaches GunBroker. `Run Scheduled Sync` off means
	the background jobs stop while the buttons keep working — the setting to
	reach for when the marketplace is misbehaving and somebody wants to drive it
	by hand for an afternoon.

	The gate only enqueues; it never does the work on the scheduler's thread and
	never raises. `job_id` + `deduplicate` so a round that overruns the hour
	cannot have a second one stacked on top of it.
	"""
	from ffl_integrations.integration_gate import gunbroker_enabled

	if not gunbroker_enabled() or not scheduled_sync_enabled():
		return
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks._run_sweep",
		queue="long", job_name=SWEEP_JOB, job_id=SWEEP_JOB, deduplicate=True)


def sweep_candidates(limit: int = SWEEP_BATCH) -> list[dict]:
	"""The guns this round looks at: anything GunBroker might still be holding.

	Three states, and no fourth. A gun that has LEFT the channel — sold, or
	ended on purpose — is not swept: the owner confirmed (2026-08-26) that
	GunBroker does not relist a gun after a sale or after a delete, so there is
	nothing left to watch for and re-selecting them would grow this set without
	bound.
	"""
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, gb_item_id, gb_push_pending, gb_push_attempted_at,
		       gb_end_pending, gb_gone_strikes, gb_gone_first_at, gb_item_history,
		       gb_price_dirty, gb_reserved, gb_order, gb_sold_unknown,
		       gb_listing_type, gb_bid_locked
		FROM `tabSerial No`
		WHERE COALESCE(gb_item_id, '') != ''
		   OR COALESCE(gb_push_pending, 0) = 1
		   OR COALESCE(gb_end_pending, 0) = 1
		ORDER BY COALESCE(gb_synced_at, creation) ASC
		LIMIT %(limit)s
		""",
		{"limit": int(limit)}, as_dict=True)
	return rows


class _Round:
	"""One sweep round: a shared client, a shared listing index, a strike count.

	The index is the reason this is a class. Every SKU-matched arm below — the
	404 branches, the relist check, the post-end re-check — needs the same
	answer to "what is GunBroker currently selling?", and pulling fifty pages
	per gun instead of once per round is the difference between a job and an
	outage. Both scans are fetched at most once, and only when something
	actually asks.
	"""

	def __init__(self, client: GunBrokerClient) -> None:
		self.client = client
		self._selling: _Scan | None = None
		self._sold: _Scan | None = None
		self.errors = 0

	@property
	def page_size(self) -> int:
		"""The configured page size, so every scan in the round pages the same
		way `recover_item_id` does — one setting, one behaviour."""
		try:
			return int(frappe.db.get_single_value(
				SETTINGS_DOCTYPE, "page_size") or 300)
		except Exception:  # noqa: BLE001 — a settings read must not end a round
			return 300

	@property
	def selling(self) -> "_Scan":
		if self._selling is None:
			self._selling = _Scan(
				self.client.iter_items_selling(page_size=self.page_size))
		return self._selling

	@property
	def sold(self) -> "_Scan":
		if self._sold is None:
			self._sold = _Scan(self.client.iter_items_sold(page_size=self.page_size))
		return self._sold


class _Scan:
	"""One paged listing scan, indexed by SKU — and honest about not knowing.

	The distinction that earns this its own class: an EMPTY answer and an
	UNREADABLE answer look identical once you have thrown the rows away, and they
	mean opposite things. An account with nothing for sale genuinely says "this
	gun is not listed". A page of rows carrying neither a SKU nor an item number
	says nothing at all — and treating it as "not listed" is how a live listing
	gets aged into ENDED by a malformed response.
	"""

	def __init__(self, rows) -> None:
		# EVERY row per SKU, not the first. GunBroker permits two live listings
		# for one gun and a lost response followed by a retry is exactly how a
		# gun gets them — so "how many" is an answer this index has to be able
		# to give. Keeping only the first turns that into a silent auto-claim of
		# whichever one paged in earlier.
		self.by_sku: dict[str, list[dict]] = {}
		self.seen = 0
		usable_rows = 0
		for row in rows:
			self.seen += 1
			sku = get_field(row, "sku", "SKU")
			item_id = get_field(row, "itemID", "ItemID", "itemId")
			if sku is not None or item_id is not None:
				usable_rows += 1
			if sku:
				self.by_sku.setdefault(str(sku), []).append(row)
		# No rows at all is a perfectly good "no". Rows that answered nothing is
		# not an answer.
		self.usable = bool(usable_rows) or self.seen == 0

	def match(self, sku: str) -> dict | None:
		"""The first row carrying this SKU, for the callers that only need to
		know whether the SKU is still alive — a relist mints a new number under
		the same SKU, so "which row" does not change that answer."""
		rows = self.by_sku.get(sku)
		return rows[0] if rows else None

	def matches(self, sku: str) -> list[dict]:
		"""Every row carrying this SKU. The COUNT is the answer for the claim
		path: see :func:`_recover_from_round`."""
		return list(self.by_sku.get(sku) or [])

	@staticmethod
	def item_id_of(row: dict) -> str:
		"""The row's item number, or "" when it does not carry a usable one.

		Validated through the same predicate the claim path uses, so a row with a
		label, a zero or a stray dict where the number should be is treated as
		"this row cannot tell me the number" — never claimed. `_claim_item_id`
		raises on a bad value rather than writing it, and the sweep must not be
		the thing that makes it raise.
		"""
		return listings._numeric_id(get_field(row, "itemID", "ItemID", "itemId"))


def _run_sweep() -> None:
	"""One sweep round. Never raises — this is a scheduler job.

	EVERYTHING is inside the try, including selecting the candidates and sending
	the circuit-breaker alert. The candidate query reads `gb_*` columns directly,
	so on a site where the app is live but `bench migrate` has not run yet it
	raises before the loop is even entered — and an hourly job that dies with a
	bare traceback tells nobody anything.
	"""
	try:
		_sweep_round()
	except Exception:  # noqa: BLE001 — a scheduler job never raises
		safe_log_error("GunBroker listing sweep failed", frappe.get_traceback())
		notify_admins(
			_("GunBroker listing sweep failed"),
			_("The hourly GunBroker listing reconcile did not complete. Listings "
			  "were not checked this round; it runs again in an hour. The Error "
			  "Log has the details."),
			# The registered TASK-LEVEL key: `gb_sweep_circuit_break`
			# is the business branch — GunBroker answered badly five times running —
			# while this one means the round itself fell over, which is a different
			# thing to be told and a different thing to go and look at.
			dedup_key="gb_listing_sweep", window_minutes=1440)


def _sweep_round() -> None:
	try:
		client = GunBrokerClient.from_settings()
	except (GunBrokerConfigError, GunBrokerError) as exc:
		safe_log_error("GunBroker sweep could not start", str(exc))
		return
	rnd = _Round(client)
	# Its own guard, and it runs FIRST only so a long serial round cannot starve
	# it. The candidate query reads Item columns that do not exist until migrate
	# has run, and the compliance-bearing firearm pass below must not die with
	# it on a site mid-deploy.
	try:
		_sweep_items(rnd)
	except Exception:  # noqa: BLE001
		safe_log_error("GunBroker item sweep pass failed", frappe.get_traceback())
	for row in sweep_candidates():
		if rnd.errors >= ERROR_CIRCUIT_BREAK:
			notify_admins(
				_("GunBroker sweep stopped early"),
				_("The GunBroker listing sweep gave up after {0} consecutive "
				  "unreadable answers. Listings were not reconciled this round; it "
				  "runs again in an hour."
				  ).format(ERROR_CIRCUIT_BREAK),
				dedup_key="gb_sweep_circuit_break", window_minutes=60)
			return
		try:
			_sweep_one(rnd, row)
		except Exception:  # noqa: BLE001 — one bad gun must not end the round
			safe_log_error(f"GunBroker sweep failed for {row.get('name')}",
				frappe.get_traceback())


def item_sweep_candidates(limit: int = SWEEP_BATCH) -> list[dict]:
	"""The item-level listings this round looks at — ammunition, optics and parts,
	and the MODEL listings of uniform-price guns (a gun priced one by one lists by
	serial and is the other query's).

	The first three states are the per-gun query's, for the same reason:
	something GunBroker may still be holding, or a local flag nothing else will
	ever clear.

	The fourth has no per-gun equivalent. A model whose listing ENDED recently is
	kept under observation for :data:`END_RECHECK_HOURS`, because an end can
	complete before GunBroker has relisted what the last sale left behind — and
	the moment `gb_item_id` is cleared, the first three tests all go false and
	nothing would ever look at that SKU again. Bounded by the timestamp so it
	does not accumulate every model ever listed.

	Those recently-ended rows sort FIRST, and that is load-bearing rather than
	tidy. `gb_synced_at` is set to now when an end is confirmed, so ordering by
	it alone puts them at the very back — and past :data:`SWEEP_BATCH` listings
	they would never make it into a batch before their six hours ran out, which
	turns the fourth state off silently on exactly the catalogues big enough to
	need it. The set that jumps the queue is small and short-lived by
	construction.
	"""
	return frappe.db.sql(
		"""
		SELECT name AS item_code, gb_item_id, gb_push_pending, gb_push_attempted_at,
		       gb_end_pending, gb_item_history, gb_qty_pushed, gb_delisted,
		       is_firearm, uniform_sell_price
		FROM `tabItem`
		WHERE (COALESCE(is_firearm, 0) = 0 OR COALESCE(uniform_sell_price, 0) = 1)
		  AND (COALESCE(gb_item_id, '') != ''
		       OR COALESCE(gb_push_pending, 0) = 1
		       OR COALESCE(gb_end_pending, 0) = 1
		       OR (COALESCE(gb_item_history, '') != ''
		           AND gb_synced_at >= %(recheck_from)s))
		ORDER BY COALESCE(gb_item_id, '') = '' DESC,
		         COALESCE(gb_synced_at, creation) ASC
		LIMIT %(limit)s
		""",
		{"limit": int(limit),
		 "recheck_from": add_to_date(now_datetime(), hours=-END_RECHECK_HOURS)},
		as_dict=True)


def _sweep_items(rnd: _Round) -> None:
	"""The item-level pass: clear the states nothing else can, and fix the count.

	Three things, each of them a state that would otherwise be terminal:

	1. **An unconfirmed push.** `gb_push_pending` is what stops a second listing
	   being created for a model that may already have one. Nothing but this
	   clears it, so without the pass a single lost response takes the model off
	   GunBroker permanently AND leaves an orphan listing there.
	2. **An end that was not confirmed.** The listing is still buyable while the
	   stock behind it is gone. The button already alerted once; this is what
	   actually retries.
	3. **An item number GunBroker has moved on from.** A sale that leaves stock
	   behind does not just decrement the listing: GunBroker ENDS it and relists
	   the remainder under a new number. Every field here addresses the old one
	   until this pass adopts the new one.
	4. **Quantity drift.** The push happens on the stock event, and events go
	   missing — a dead queue job, a restart mid-call. Drift is the one that
	   oversells, so it is checked against what GunBroker itself reports rather
	   than against our own record of what we sent.

	Deliberately NOT here: concluding a listing has vanished. That needs the
	two-round strike machinery the per-gun path has, and a wrong verdict
	un-lists live stock.
	# lazy: no gone-detection for item listings, follow-up in the run notes.
	"""
	for row in item_sweep_candidates():
		if rnd.errors >= ERROR_CIRCUIT_BREAK:
			return
		try:
			_sweep_one_item(rnd, row)
		except Exception:  # noqa: BLE001 — one bad item must not end the round
			safe_log_error(
				f"GunBroker item sweep failed for {row.get('item_code')}",
				frappe.get_traceback())
	_relist_ended_models()


def _relist_ended_models() -> None:
	"""Put back a MODEL that sold out and has stock again, once its cool-off is over.

	The stock event that normally does this (`doc_events.sync_item_level_listing`)
	is refused inside the cool-off — a morning's sell-out and an afternoon delivery
	are one listing, not two — and nothing would ask again until the next movement,
	which for a model can be days. So the hourly round asks. Only models that have
	been listed before, were not taken down by hand and are not mid-push;
	`relist_on_restock` re-checks all of that and the stock, and never buys the paid
	promotions (a machine does not spend the operator's money).
	# lazy: bounded by SWEEP_BATCH models; a catalogue of more would need paging.
	"""
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabItem`
		WHERE COALESCE(is_firearm, 0) = 1 AND COALESCE(uniform_sell_price, 0) = 1
		  AND COALESCE(gb_item_id, '') = '' AND COALESCE(gb_item_history, '') != ''
		  AND COALESCE(gb_delisted, 0) = 0 AND COALESCE(gb_push_pending, 0) = 0
		  AND COALESCE(disabled, 0) = 0
		LIMIT %(limit)s
		""", {"limit": SWEEP_BATCH})
	for (item_code,) in rows:
		try:
			item_listings.relist_on_restock(item_code)
		except Exception:  # noqa: BLE001 — one bad model must not end the round
			safe_log_error(f"GunBroker model relist failed for {item_code}",
				frappe.get_traceback())


def _sweep_one_item(rnd: _Round, row: dict) -> None:
	item_code = row["item_code"]
	sku = mapping.item_listing_sku(item_code)

	if row.get("gb_push_pending") and _push_grace_expired(row):
		_resolve_stale_item_push(rnd, item_code, sku)
		row = item_listings.item_state(item_code)
		row["item_code"] = item_code

	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		# Nothing is listed. The one thing left to check is whether an end
		# completed before GunBroker had finished relisting behind it.
		_recheck_item_after_end(rnd, item_code, sku, row)
		return

	# FIRST, because everything below addresses `gb_item_id` and a relist has
	# very likely moved it.
	live_id = _adopt_item_listing(rnd, item_code, sku, item_id, row)

	if row.get("gb_end_pending"):
		# The stock behind this listing has gone and the listing is still
		# buyable. Retrying the end is the whole job; a quantity push on top
		# would be telling GunBroker how many of something we are not selling.
		#
		# Retried even when the adopt above could not settle the number: the end
		# confirms itself against the item it deletes and follows a relist from
		# there, so it is safe on a number the scan could not vouch for.
		item_listings.end_item_listing(
			item_code, reason=_("swept: the end was never confirmed"))
		return

	if live_id is None:
		# The SKU is live but the scan cannot say under which number. Sending a
		# quantity to the one we happen to hold is precisely the write the adopt
		# step exists to prevent.
		return

	# Against GunBroker's own number, not our record of what we sent: a push
	# that was silently discarded leaves those two agreeing and the listing
	# wrong, which is exactly the case that oversells.
	remote_qty = _remote_quantity(rnd, sku, live_id)
	if item_listings.is_model(row):
		_check_model_drift(rnd, item_code, sku, live_id, remote_qty)
		return
	local_qty = item_listings.sellable_qty(item_code)
	if remote_qty is None or remote_qty == local_qty:
		return
	item_listings.sync_item_quantity(item_code, client=rnd.client, force=True)


def _check_model_drift(rnd: _Round, item_code: str, sku: str, live_id: str,
		remote_qty: int | None) -> None:
	"""A live MODEL listing against what it should hold and charge.

	The quantity is checked with the same formula the push uses
	(``item_listings.expected_model_quantity``: the pool less the units GunBroker
	sold that no order has brought in yet), NOT against the pool alone — the pool
	alone would call every GunBroker sale awaiting its order a drift, and "fix" it by
	pushing the sold unit back. A scan that does not carry the listing says nothing
	(None), as it does for any item.

	The price is checked against the row the same scan already holds, so the hourly
	pass costs no request for it. Per-gun listings keep their own rule.
	"""
	if remote_qty is not None:
		try:
			target = uniform_sellable_units(item_code)
		except Exception:  # noqa: BLE001 — a channel that cannot answer: keep what was sent
			safe_log_error(f"GunBroker model drift check skipped for {item_code}",
				frappe.get_traceback())
		else:
			state = item_listings.item_state(item_code)
			expected = item_listings.expected_model_quantity(
				item_code, state, remote_qty, target)
			if remote_qty != expected:
				item_listings.sync_item_quantity(item_code, client=rnd.client, force=True)
	if not rnd.selling.usable:
		return
	scan = next((r for r in rnd.selling.matches(sku)
		if _Scan.item_id_of(r) == live_id), None)
	remote_price = get_field(scan or {}, "fixedPrice", "FixedPrice")
	if remote_price is None:
		return
	local_price = flt(item_current_price(item_code))
	if local_price > 0 and abs(flt(remote_price) - local_price) > 0.005:
		frappe.db.set_value("Item", item_code, "gb_price_dirty", 1, update_modified=False)
		frappe.enqueue(
			"ffl_integrations.gunbroker.tasks.gb_reprice_item_task",
			queue="short", enqueue_after_commit=True, item_code=item_code)


def _adopt_item_listing(rnd: _Round, item_code: str, sku: str, item_id: str,
		row: dict) -> str | None:
	"""Which listing this SKU is really live under — adopting it if it has moved.

	The item-level twin of :func:`_adopt_live_listing`, and it exists because of
	a difference the per-gun path does not have. A gun's listing holds one unit,
	so a sale empties it and GunBroker relists nothing; the only thing that mints
	a new number there is the thirty-day clock, once a month. An item-level
	listing holds a COUNT, and a sale that leaves stock behind makes GunBroker
	end the listing and put the REMAINDER back up under a new number
	(``relistedAsItemID`` on the old one). That is every partial sale.

	Until the new number is adopted, the quantity push, the reprice and the end
	all address a listing that no longer exists, while the live one goes on
	selling stock nothing is decrementing — and an end confirmed against the dead
	number clears `gb_item_id`, which drops the model out of
	:func:`item_sweep_candidates` and leaves the live listing orphaned for good.

	Returns the number to act on, or None when the scan cannot say — in which
	case nothing should be sent this round.
	"""
	if not rnd.selling.usable:
		return None
	rows = rnd.selling.matches(sku)
	if not rows:
		# Not on the selling list. That is not a verdict — `/ItemsSelling` lags a
		# relist by minutes — so nothing is adopted and nothing is forgotten. The
		# quantity check downstream reads the same miss and stops there.
		return item_id
	ids = [_Scan.item_id_of(r) for r in rows]
	if not all(ids):
		# A row matched the SKU but cannot say its own number. Same verdict as
		# `_recover_from_round`: a partial answer settles nothing.
		return None
	# Numbers already in the history are ones this model has been on and left.
	# `/ItemsSelling` keeps listing them for minutes afterwards — observed: a
	# listing read back inactive was still in the same scan — so without this the
	# number we came FROM reads as a second live listing, and every round after
	# an adopt reports a duplicate that is not there.
	seen = listings.history_ids(row)
	candidates = sorted(set(ids) - {item_id} - seen)
	if not candidates:
		return item_id
	if len(candidates) > 1:
		# Two listings under one SKU that are neither ours nor anything we have
		# ended. Do not choose — adopting either leaves the other live,
		# unwatched and sellable to a buyer no cancel can save.
		_alert_duplicate(item_listings.ITEM_DOCTYPE, item_code, candidates)
		return None
	if item_id in ids:
		# OURS and one number we have never seen, both on the selling list. Two
		# readings with opposite meanings: ours has just been replaced and the
		# list has not caught up, or there really are two. Only the item itself
		# can say which, and guessing either way loses something: guess "lag" and
		# a real duplicate goes unreported, guess "duplicate" and every partial
		# sale raises a false alarm that then swallows the real one for a day.
		state = _our_listing_state(rnd, item_id)
		if state is None:
			return None
		if state["live"]:
			_alert_duplicate(item_listings.ITEM_DOCTYPE, item_code,
				sorted(set(ids) - seen))
			return None
		if state["successor"] and state["successor"] != candidates[0]:
			# Ours has ended and named a replacement that is NOT the row we
			# matched. Two answers that disagree settle nothing.
			return None
	item_listings.claim_item_listing(item_code, candidates[0], previous=item_id)
	return candidates[0]


def _our_listing_state(rnd: _Round, item_id: str) -> dict | None:
	"""What the item itself says about the listing we hold, or None if it did not.

	`/ItemsSelling` cannot answer this — it is the list that lags — so the item
	is asked directly. `gone` counts as over: GunBroker stops serving old item
	numbers and does not do that to a listing still taking money. The None is the
	point of the signature: an unanswered probe must not become either verdict,
	because one of them alerts and the other adopts.
	"""
	probe = rnd.client.item_probe(item_id)
	if probe["state"] == "error":
		rnd.errors += 1
		return None
	rnd.errors = 0
	if probe["state"] == "gone":
		return {"live": False, "successor": ""}
	item = probe.get("item") or {}
	return {
		"live": get_field(item, "isActive", "IsActive") is not False,
		"successor": listings._numeric_id(
			get_field(item, "relistedAsItemID", "RelistedAsItemID")),
	}


def _recheck_item_after_end(rnd: _Round, item_code: str, sku: str,
		row: dict) -> None:
	"""Keep watching a SKU whose listing has ended, for one that was not ours.

	The item-level twin of :func:`_recheck_after_end`, and it has to run in its
	own round rather than inline: an end reads `relistedAsItemID` to find what
	replaced the listing, a 0 there does not mean nothing did, and an end that
	reads 0 confirms against the number it deleted. If a replacement then
	surfaces, nothing points at it. The end cannot see that coming from one
	read. This is what looks again afterwards, by SKU, which does not depend on
	that field at all.

	Everything already in `gb_item_history` is ignored, and that is the whole
	safety of it: `/ItemsSelling` goes on showing a listing for minutes after it
	ends, so re-adopting a number we have already taken down would be reading
	that lag as a new listing — and then ending, re-listing and re-ending the
	same dead number every round.
	"""
	if row.get("gb_push_pending"):
		# A push whose answer never came back. The listing this scan is about to
		# find is very likely the one that push CREATED — the number simply has
		# not been written back yet — and ending it would undo a publish that
		# worked. That case belongs to `_resolve_stale_item_push`, which waits
		# out `PUSH_GRACE_HOURS` first and only ever claims, never ends; the
		# whole point of that grace period is that nothing acts inside it.
		return
	history = listings.history_ids(row)
	if not history or not rnd.selling.usable:
		return
	found = {i for i in (_Scan.item_id_of(r) for r in rnd.selling.matches(sku)) if i}
	unaccounted = sorted(found - history)
	if not unaccounted:
		return
	if len(unaccounted) > 1:
		_alert_duplicate(item_listings.ITEM_DOCTYPE, item_code, unaccounted)
		return
	item_listings.claim_item_listing(item_code, unaccounted[0])
	# Ended with the sentinel the model already carried: a listing put back by
	# `relist_on_restock` after a sell-out must stay able to come back, and one a
	# person took down must stay down.
	item_listings.end_item_listing(
		item_code, client=rnd.client, delisted=bool(row.get("gb_delisted")),
		reason=_("swept: a GunBroker listing appeared under this SKU after the "
			"last one was ended"))


def _remote_quantity(rnd: _Round, sku: str, item_id: str) -> int | None:
	"""What GunBroker holds on OUR listing, or None when the scan cannot say.

	Matched on the item number rather than taking the SKU's first row, because
	during a relist the scan carries the number we came from as well and its
	count belongs to the dead listing. A scan that does not carry our number
	cannot say anything about it — which is a None, not the other row's answer.
	"""
	if not rnd.selling.usable:
		return None
	row = next((r for r in rnd.selling.matches(sku)
		if _Scan.item_id_of(r) == item_id), None)
	if row is None:
		return None
	quantity = get_field(row, "quantity", "Quantity")
	# flt first: GunBroker has answered with "5.0" shaped values elsewhere, and
	# int("5.0") raises — which would read as "the scan cannot say" and silently
	# turn the drift check off for that item.
	try:
		return int(frappe.utils.flt(quantity))
	except (TypeError, ValueError):
		return None


def _resolve_stale_item_push(rnd: _Round, item_code: str, sku: str) -> None:
	"""What became of an item-level listing attempt nobody got an answer to."""
	found = _recover_from_round(rnd, item_code, sku)
	if found is None:
		found = _recover_item_listing(rnd, sku)
	state = found["state"]
	if state == "claimed":
		item_listings.claim_item_listing(item_code, found["item_id"])
		return
	if state == "duplicate":
		_alert_duplicate(item_listings.ITEM_DOCTYPE, item_code, found["ids"])
		return
	if state == "unknown":
		notify_admins(
			_("GunBroker listing scan unusable"),
			_("The GunBroker scan for item {0} came back with rows carrying "
			  "neither a SKU nor an item number, so it cannot say whether the "
			  "listing exists. Nothing was changed."
			  ).format(item_code),
			dedup_key=f"gb_scan_unusable_{item_code}", window_minutes=1440)
		return
	# A miss, from a scan that could answer: nothing was created. Clearing the
	# flag is what lets the operator send it again.
	frappe.db.set_value("Item", item_code, {
		"gb_push_pending": 0,
		"gb_push_attempted_at": None,
	}, update_modified=False)


def _recover_item_listing(rnd: _Round, sku: str) -> dict:
	"""The long way round for one item SKU, when the round's index cannot say.

	`listings.recover_item_id` is serial-shaped (it builds the SKU from the
	Serial No and reads its stored item number), so the item side asks the sold
	scan directly rather than pretending to be a gun.
	"""
	# A SKU the selling scan matched but whose rows carry no readable item
	# number is UNKNOWN, never a miss: answering "nothing was created" clears
	# the pending flag, and the next push then mints a second live listing for
	# the same SKU. The per-gun path is covered here by recover_item_id's
	# item-number match; the item path has only the SKU, so it has to be
	# careful with it.
	if rnd.selling.usable and rnd.selling.matches(sku):
		return {"state": "unknown", "ids": []}
	if not rnd.sold.usable:
		return {"state": "unknown", "ids": []}
	rows = rnd.sold.matches(sku)
	ids = [i for i in (_Scan.item_id_of(r) for r in rows) if i]
	if len(ids) == 1:
		return {"state": "claimed", "item_id": ids[0], "ids": ids}
	if len(ids) > 1:
		return {"state": "duplicate", "ids": ids}
	return {"state": "miss", "ids": []}


def _sweep_one(rnd: _Round, row: dict) -> None:
	serial_no = row["name"]
	sku = _sku(row, serial_no)

	# 1. An unconfirmed listing attempt, old enough to be worth chasing.
	if row.get("gb_push_pending") and _push_grace_expired(row):
		_resolve_stale_push(rnd, serial_no, sku)
		row = listings.serial_state(serial_no)
		row["name"] = serial_no

	item_id = str(row.get("gb_item_id") or "")
	if item_id:
		_reconcile_listing(rnd, serial_no, sku, item_id, row)
		row = listings.serial_state(serial_no)
		row["name"] = serial_no
		item_id = str(row.get("gb_item_id") or "")

	# 2. The other direction: POS says this gun is not sellable and GunBroker
	#    still has it up. Runs LAST so it acts on the item number this round may
	#    just have adopted, rather than on a stale one.
	if not item_id:
		return
	state = serial_shelf_state(serial_no)
	if state["sellable"]:
		return
	if _unsellable_because_of_gunbroker(state):
		# The gun is unsellable BECAUSE GunBroker has a buyer for it. Ending the
		# listing over that would be answering GunBroker with its own news — and
		# GunBroker does not permit withdrawing a listing that has been bought, so
		# the call fails, raises an end-pending flag and alerts somebody about a
		# listing that is behaving exactly as it should.
		return
	listings.end_listing(serial_no, reason=f"swept: {_shelf_reason(state)}")


def _unsellable_because_of_gunbroker(state: dict) -> bool:
	"""Is GunBroker itself the reason POS says this gun cannot be sold?

	Two shapes: a matched order (`channel_reserved` naming this channel) and the
	sweep's own "may already be sold there" flag. Both mean the marketplace has
	the gun, which is the one reason to leave its listing alone.
	"""
	if state.get("reason") == "sold_unknown":
		return True
	return (state.get("reason") == "channel_reserved"
		and (state.get("detail") or {}).get("channel") == "gunbroker")


def _sku(row: dict, serial_no: str) -> str:
	from ffl_integrations.gunbroker import mapping

	return mapping.listing_sku(row.get("item_code"), serial_no)


def _push_grace_expired(row: dict) -> bool:
	"""Has an unconfirmed attempt been unconfirmed for long enough to chase?

	An EMPTY `gb_push_attempted_at` means yes, immediately. It has to: the field
	arrived later than the flag, so historical rows carry the flag with no
	timestamp, and a rule that read a missing timestamp as "not old enough" would
	strand those guns permanently — the one outcome the grace period exists to
	avoid.
	"""
	attempted = row.get("gb_push_attempted_at")
	if not attempted:
		return True
	return attempted <= listings.stale_push_cutoff()


def _resolve_stale_push(rnd: _Round, serial_no: str, sku: str) -> None:
	"""T10 from the sweep: work out what became of a listing attempt.

	Unlike the button, the sweep also scans the SOLD list. A listing bought
	minutes after it was created has already left `/ItemsSelling`, and a
	selling-only scan would report a confident "nothing was created" about a gun
	that is now somebody else's.
	"""
	# The round already holds both scans, indexed by SKU, so answer from those
	# rather than paging `/ItemsSelling` and `/ItemsSold` again per gun.
	# `recover_item_id` is still the fallback: it carries an itemID-based match
	# the SKU index cannot express, and this arm only runs for the few guns with
	# an unconfirmed attempt.
	found = _recover_from_round(rnd, serial_no, sku)
	if found is None:
		found = listings.recover_item_id(serial_no, scan_sold=True, client=rnd.client)
	state = found["state"]
	if state == "claimed":
		listings._claim_item_id(serial_no, found["item_id"])
		return
	if state == "duplicate":
		_alert_duplicate("Serial No", serial_no, found["ids"])
		return
	if state == "unknown":
		notify_admins(
			_("GunBroker listing scan unusable"),
			_("The GunBroker scan for serial {0} came back with rows carrying "
			  "neither a SKU nor an item number, so it cannot say whether the "
			  "listing exists. Nothing was changed."
			  ).format(serial_no),
			dedup_key=f"gb_scan_unusable_{serial_no}", window_minutes=1440)
		return
	if found.get("sold"):
		_mark_sold_unknown(serial_no, "", reason="found in the GunBroker sold list")
		return
	# Both scans answered, and neither had it. Nothing was created — release the
	# gun so the button works again.
	frappe.db.set_value("Serial No", serial_no, {
		"gb_push_pending": 0,
		"gb_push_attempted_at": None,
		"gb_sync_error": "",
	})


def _recover_from_round(rnd: _Round, serial_no: str, sku: str) -> dict | None:
	"""Answer T10 from this round's shared scans, or None to fall back.

	Returns the same shape `recover_item_id` does, and reaches the same verdict
	from the same rows — `claimed` for exactly one live listing, `duplicate` for
	two or more, `miss` when neither scan has it. A fast path that collapses one
	of those states is not a fast path; it is a second, quieter implementation
	with a different opinion.

	None means the index could not settle it — an unusable scan, or a SKU hit
	with no readable item number — and the caller re-asks GunBroker the long way
	rather than guessing from a partial answer.
	"""
	# Ordered so the sold scan is only fetched when the selling scan did not
	# settle it. `_Round.sold` pulls pages on first access; asking it up front
	# would cost every round a scan it usually does not need.
	if not rnd.selling.usable:
		return None
	live = rnd.selling.matches(sku)
	if live:
		item_ids = [_Scan.item_id_of(row) for row in live]
		if not all(item_ids):
			# A row matched the SKU but cannot say its own number. The long way
			# has one more thing to try here — it falls back to the item number
			# already cached locally — so hand it over rather than deciding.
			return None
		if len(item_ids) > 1:
			# TWO live listings for one gun. Do not choose. `recover_item_id`
			# has answered `duplicate` here since it was written, for a reason
			# spelled out in its docstring: GunBroker permits duplicates, so
			# this is a real situation, and picking one hides it — leaving the
			# other live, unwatched, and sellable to a second buyer that no
			# seller-side cancel can save.
			#
			# Counted per matching ROW, exactly as `_scan_for_sku` counts them:
			# the two paths must reach the same verdict from the same pages, and
			# a fast path that is merely ALMOST the slow one is worse than none.
			return {"state": "duplicate", "item_id": None, "ids": item_ids,
				"sold": False}
		return {"state": "claimed", "item_id": item_ids[0], "ids": item_ids,
			"sold": False}
	if not rnd.sold.usable:
		return None
	if rnd.sold.match(sku) is not None:
		return {"state": "miss", "item_id": None, "ids": [], "sold": True}
	return {"state": "miss", "item_id": None, "ids": [], "sold": False}


def _reconcile_listing(rnd: _Round, serial_no: str, sku: str, item_id: str,
		row: dict) -> None:
	probe = rnd.client.item_probe(item_id)
	state = probe["state"]

	if state == "error":
		rnd.errors += 1
		return
	rnd.errors = 0

	if state == "gone":
		# One more look before believing it. An ended listing reads back 200 with
		# `isActive` false for a good while, so a JSON 404 this soon is more often
		# an edge node than a fact. If the second read answers, it is an ordinary
		# `ok` and goes through the same branches as any other.
		time.sleep(GONE_RECHECK_SECONDS)
		probe = rnd.client.item_probe(item_id)
		if probe["state"] == "error":
			rnd.errors += 1
			return
		if probe["state"] == "gone":
			_handle_gone(rnd, serial_no, sku, item_id, row)
			return

	_handle_ok(rnd, serial_no, sku, item_id, row, probe.get("item") or {})


# ---------------------------------------------------------------------------
# The `gone` arm — GunBroker answered 404, twice
# ---------------------------------------------------------------------------

def _handle_gone(rnd: _Round, serial_no: str, sku: str, item_id: str,
		row: dict) -> None:
	"""Three branches, all matched on SKU, none of which may guess.

	The failure this arm is shaped around: GunBroker stops serving an item's
	number, POS concludes the gun was never listed, resets it to UNLISTED, and
	the counter sells a gun that was bought on GunBroker fifteen minutes ago. So
	no branch here clears the item number on the strength of one look, and the
	branch that clears it at all marks the gun ENDED — never UNLISTED, because
	UNLISTED invites the machine to list it again.
	"""
	if _adopt_live_listing(rnd, serial_no, sku, item_id):
		# It is alive, and most likely under a NEW number: GunBroker relists a
		# fixed-price listing every thirty days and mints a new one each time.
		# That is the ordinary monthly event, not a problem.
		return

	if rnd.sold.match(sku):
		_mark_sold_unknown(serial_no, item_id,
			reason="the GunBroker sold list shows this SKU")
		return

	if not rnd.selling.usable or not rnd.sold.usable:
		# A scan came back with rows that carry neither a SKU nor an item number.
		# That is not "the listing is not there" — it is "GunBroker did not answer
		# the question", and an unanswered question may never move the state
		# machine towards forgetting a listing.
		_alert_unusable_scan(serial_no)
		return

	# Same alert key as the `ok` arm's version of this verdict: both mean "the
	# listing ended somewhere other than here, and POS will not put it back".
	# Reaching this point WITHOUT telling anybody was the worst of both worlds —
	# the gun is marked ENDED, no machine will ever relist it, and nobody knows.
	_age_out(serial_no, item_id, row,
		min_hours=GONE_MIN_HOURS,
		alert_key=f"gb_ended_externally_{serial_no}",
		why=_("GunBroker has stopped serving item {0} and the SKU is in neither "
			"the selling nor the sold list.").format(item_id))


# ---------------------------------------------------------------------------
# The `ok` arm — GunBroker answered with the item
# ---------------------------------------------------------------------------

def _handle_ok(rnd: _Round, serial_no: str, sku: str, item_id: str, row: dict,
		item: dict) -> None:
	if gb_item_bid_locked(item):
		# A live auction with a bid. Not sold — no order, and the reserve may
		# go unmet — but promised, which locks the counter and the web store.
		_clear_gone_counters(serial_no, row)
		_lock_for_bids(serial_no, row,
			bids=cint(get_field(item, "bids", "Bids") or 0),
			current_bid=flt(get_field(item, "currentBid", "CurrentBid") or 0),
			ends_at=_ends_at(get_field(item, "endingDate", "EndingDate")))
		return
	if gb_item_sold(item):
		_handle_sold(serial_no, item_id, row)
		return

	is_active = get_field(item, "isActive", "IsActive")
	if is_active is not False:
		# Live and un-sold: the normal case. THIS is what clears the counters —
		# and only this. Clearing them for an inactive listing too would cancel
		# out the increment three lines down, pinning the count at one forever and
		# making the ENDED threshold unreachable.
		_clear_gone_counters(serial_no, row)
		if row.get("gb_bid_locked"):
			# The bids are gone (withdrawn, or the listing was re-read after a
			# relist). The promise goes with them.
			_unlock_bids(serial_no)
		if get_field(item, "isFixedPrice", "IsFixedPrice") is not False:
			# An auction's price belongs to the bidders; there is no drift.
			_check_price_drift(serial_no, item)
		return

	# Not active, not sold — and from here that has two completely different
	# meanings depending on whether anybody asked for it.

	if row.get("gb_end_pending"):
		# WE asked for this listing to end, so an inactive listing is the expected
		# answer and one round is enough — there is nothing ambiguous to wait out.
		# The alert already went out when the end was requested (T4); a second one
		# would be the same news twice.
		#
		# Checked BEFORE the relist branches below, deliberately. Finding a live
		# listing under this SKU does not mean "adopt it and carry on" for a gun
		# we are trying to take OFF the market — it means end that one too, which
		# is what the re-check does.
		_finish_ended(serial_no, item_id, row)
		_recheck_after_end(rnd, serial_no, sku, item_id)
		return

	# Nobody asked. So before calling it ended, ask whether GunBroker has simply
	# relisted it — with auto-relist on a thirty-day listing EVERY unsold gun
	# passes through exactly this shape once a month, and the old item's
	# `relistedAsItemID` reads 0 for some of that window.
	# `_numeric_id` also handles the 0, which is not evidence that nothing was
	# relisted: the field has only ever been observed already-set or already-0.
	# The SKU branches below are what does not depend on it.
	relisted_as = listings._numeric_id(
		get_field(item, "relistedAsItemID", "RelistedAsItemID"))
	if relisted_as:
		listings._claim_item_id(serial_no, relisted_as, previous=item_id)
		return

	if _adopt_live_listing(rnd, serial_no, sku, item_id):
		return

	if not rnd.selling.usable:
		_alert_unusable_scan(serial_no)
		return

	# Either somebody ended it on GunBroker, or this is the relist window and
	# `/ItemsSelling` has not caught up yet — and from here those look identical.
	# Two rounds and two hours apart tell them apart; one look does not, and
	# getting it wrong marks a gun that is live on GunBroker as ENDED while the
	# counter is free to sell it.
	if str(row.get("gb_listing_type") or "") == mapping.LISTING_AUCTION:
		# An auction that ran out with no winner is the ordinary end of an
		# auction, not something that went wrong: no alert, a note on the gun,
		# and the daily audit lists it. The same two-round patience applies —
		# with relist-until-sold the replacement appears in this window.
		_age_out(serial_no, item_id, row,
			min_hours=ENDED_MIN_HOURS, alert_key=None,
			why="", note=AUCTION_UNSOLD_NOTE)
		return
	_age_out(serial_no, item_id, row,
		min_hours=ENDED_MIN_HOURS,
		alert_key=f"gb_ended_externally_{serial_no}",
		why=_("GunBroker shows item {0} as no longer active, with no sale and no "
			"relisted listing under the same SKU.").format(item_id))


def _handle_sold(serial_no: str, item_id: str, row: dict) -> None:
	"""GunBroker says this listing has a buyer.

	If the order has already been polled in, the reservation is recorded and
	there is nothing to do. If it has not, the poll has missed it — so the gun is
	flagged fail-closed, the poll is kicked immediately rather than waited for,
	and a person is told.
	"""
	if row.get("gb_reserved") and row.get("gb_order"):
		return
	_mark_sold_unknown(serial_no, item_id,
		reason="GunBroker reports the listing as sold")


AUCTION_UNSOLD_NOTE = "auction ended without a sale"
AUCTION_WATCH_JOB_ID = "gb-auction-watch"


def _ends_at(raw):
	"""GunBroker's ending time as a naive site-time datetime, or None."""
	if not raw:
		return None
	try:
		parsed = parse_gb_datetime(raw)
	except Exception:  # noqa: BLE001 — a display field must not fail a round
		return None
	if parsed is None:
		return None
	return convert_utc_to_system_timezone(parsed).replace(tzinfo=None)


def _lock_for_bids(serial_no: str, row: dict, *, bids: int, current_bid: float,
		ends_at) -> None:
	"""Record the bids, and on the FIRST one take the gun off every other channel.

	`gb_bid_locked` is a channel hold (ffl_core.reservations lists it beside
	`gb_reserved`): the counter picker, the web order placer and the counter's
	hard gate all read it, so setting it is what makes the promise bind. The
	web storefronts are told through the same exit hook a sale uses.
	"""
	updates: dict[str, Any] = {
		"gb_bid_count": int(bids), "gb_current_bid": float(current_bid),
	}
	if ends_at is not None:
		updates["gb_auction_ends_at"] = ends_at
	newly = not row.get("gb_bid_locked")
	if newly:
		updates["gb_bid_locked"] = 1
	frappe.db.set_value("Serial No", serial_no, updates, update_modified=False)
	if newly:
		fire_serial_channel_exit(serial_no, reason=REASON_BID)


def _unlock_bids(serial_no: str) -> None:
	"""The bids are gone and the gun is the store's again.

	Only the GunBroker side of the hold is released here. A gun the web
	store took down on the first bid stays down until somebody puts it back
	— the same rule as every other released channel hold.
	"""
	frappe.db.set_value("Serial No", serial_no, {
		"gb_bid_locked": 0, "gb_bid_count": 0, "gb_current_bid": 0,
	}, update_modified=False)


def gb_auction_watch() -> dict:
	"""Cron entry (every 15 minutes). Gate, then enqueue; never raises.

	The hourly sweep would notice a bid eventually; an hour is long enough
	for the gun to be sold at the counter in between. This is one page read
	of `/ItemsSelling`, which carries the bid count of every live listing, so
	the lock lands within fifteen minutes of the first bid. Skipped entirely
	when no auction is live.
	"""
	# Imported here, as the sweep's gate does: the gate module imports this
	# package, and a module-level import would close the loop.
	from ffl_core.utils import single_flag
	from ffl_integrations.integration_gate import gunbroker_enabled

	summary = {"enqueued": False}
	try:
		if not gunbroker_enabled():
			return summary
		if not single_flag(SETTINGS_DOCTYPE, "auto_sync_enabled", default=1):
			return summary
		if not frappe.db.exists("Serial No", {
				"gb_listing_type": mapping.LISTING_AUCTION, "gb_item_id": ("!=", "")}):
			return summary
		frappe.enqueue(
			"ffl_integrations.gunbroker.tasks._run_auction_watch",
			queue="short", job_id=AUCTION_WATCH_JOB_ID, deduplicate=True)
		summary["enqueued"] = True
	except Exception:  # noqa: BLE001 — a gate must not break the tick
		safe_log_error("GunBroker auction watch could not be enqueued",
			frappe.get_traceback())
	return summary


def _run_auction_watch(client: GunBrokerClient | None = None) -> dict:
	"""One look at every live auction's bid count."""
	summary = {"auctions": 0, "locked": 0, "released": 0}
	try:
		summary.update(_auction_watch_round(client))
	except Exception:  # noqa: BLE001 — a scheduler job never raises
		safe_log_error("GunBroker auction watch failed", frappe.get_traceback())
	return summary


def _auction_watch_round(client: GunBrokerClient | None = None) -> dict:
	auctions = frappe.get_all("Serial No",
		filters={"gb_listing_type": mapping.LISTING_AUCTION, "gb_item_id": ("!=", "")},
		fields=["name", "gb_item_id", "gb_bid_locked", "gb_bid_count"])
	summary = {"auctions": len(auctions), "locked": 0, "released": 0}
	if not auctions:
		return summary
	client = client or GunBrokerClient.from_settings()
	rnd = _Round(client)
	scan = rnd.selling
	if not scan.usable:
		return summary
	by_id: dict[str, dict] = {}
	for rows in scan.by_sku.values():
		for selling in rows:
			item_id = _Scan.item_id_of(selling)
			if item_id:
				by_id[item_id] = selling
	for row in auctions:
		selling = by_id.get(str(row.get("gb_item_id") or ""))
		if selling is None:
			# Not in the live list: ended, sold or relisted. The hourly sweep
			# reads the item itself and settles which.
			continue
		outcome = apply_auction_row(row, selling)
		summary["locked"] += outcome == BID_LOCKED
		summary["released"] += outcome == BID_RELEASED
	return summary


BID_LOCKED = "locked"        # the first bid: the gun was just taken off the other channels
BID_HELD = "held"            # bids, and the lock was already on
BID_RELEASED = "released"    # the bids are gone and the lock was lifted


def apply_auction_row(row: dict, selling: dict) -> str:
	"""The per-gun step of the bid watch: one auction's live row decides its lock.

	Shared by the 15-minute watch (``selling`` = the gun's `/ItemsSelling` row) and
	the WinningBidReceived webhook (``selling`` = the same facts from the event), so
	"what a bid does to a gun" is written once.

	Args:
	    row: The gun's ``name``, ``gb_bid_locked`` as last read.
	    selling: Needs ``isFixedPrice`` (anything but an explicit False is skipped —
	        a fixed-price listing has no bids), ``bidCount``, and optionally
	        ``currentBid`` / ``endingDateTimeUTC``.

	Returns:
	    ``BID_LOCKED`` / ``BID_HELD`` / ``BID_RELEASED``, or "" when nothing changed.
	"""
	if get_field(selling, "isFixedPrice", "IsFixedPrice") is not False:
		return ""
	bids = cint(get_field(selling, "bidCount", "BidCount") or 0)
	if bids > 0:
		_lock_for_bids(row["name"], row,
			bids=bids,
			current_bid=flt(get_field(selling, "currentBid", "CurrentBid") or 0),
			ends_at=_ends_at(get_field(selling, "endingDateTimeUTC", "EndingDateTimeUTC")))
		return BID_HELD if row.get("gb_bid_locked") else BID_LOCKED
	if row.get("gb_bid_locked"):
		if frappe.cache.get_value(_webhook_lock_key(row["name"])):
			# The webhook saw a bid minutes ago; /ItemsSelling is eventually consistent
			# and can still say 0. Releasing now would put a gun with a live bid back
			# on the counter.
			return BID_HELD
		_unlock_bids(row["name"])
		return BID_RELEASED
	return ""


# How long a webhook-seen bid outranks a zero bid count in /ItemsSelling.
# lazy: kept in the cache, so a deploy (cache flush) shortens it to the old
# behaviour; upgrade path is a gb_bid_seen_at field on the Serial No.
WEBHOOK_LOCK_GRACE_SECONDS = 30 * 60


def _webhook_lock_key(serial_no: str) -> str:
	return f"gunbroker:webhook_bid_lock:{serial_no}"


def gb_bid_received_task(serial_no: str, bid_count: int = 1, current_bid: float = 0,
		ends_at: str = "") -> None:
	"""A WinningBidReceived webhook named this gun: lock it now, not at the next watch.

	The event is trusted only as far as "a bid exists on a listing carrying this
	gun's SKU". A gun the record contradicts — not listed, not an auction — is
	handed to the full watch, which asks GunBroker; one already held by an order
	needs nothing. Idempotent: a second delivery for the same bid finds the lock
	already on.
	"""
	try:
		row = frappe.db.get_value("Serial No", serial_no,
			["name", "gb_item_id", "gb_listing_type", "gb_reserved", "gb_bid_locked"],
			as_dict=True)
		if row and row.get("gb_reserved"):
			return  # already spoken for by an order; the hold binds without a bid lock
		if (not row or not row.get("gb_item_id")
				or row.get("gb_listing_type") != mapping.LISTING_AUCTION):
			gb_auction_watch()
			return
		frappe.cache.set_value(_webhook_lock_key(serial_no), 1,
			expires_in_sec=WEBHOOK_LOCK_GRACE_SECONDS)
		apply_auction_row(row, {
			"isFixedPrice": False,
			"bidCount": max(cint(bid_count), 1),  # the event IS a bid
			"currentBid": current_bid,
			"endingDateTimeUTC": ends_at,
		})
	except Exception:  # noqa: BLE001 — a queue job never raises
		safe_log_error(f"GunBroker bid webhook failed: {serial_no}", frappe.get_traceback())


def _check_price_drift(serial_no: str, item: dict) -> None:
	"""A live listing whose price is not ours gets marked dirty for the reprice
	task. Cheap: the item detail is already in hand."""
	from ffl_core.firearm import effective_serial_price

	remote = flt(get_field(item, "fixedPrice", "FixedPrice") or 0)
	local = flt(effective_serial_price(serial_no))
	if local <= 0 or abs(remote - local) <= 0.005:
		return
	frappe.db.set_value("Serial No", serial_no, "gb_price_dirty", 1,
		update_modified=False)
	# Setting the flag is not sending the price. Its only other writer is a
	# human editing `sell_price`, which is also the only thing that enqueues the
	# reprice — so a drift the sweep found and merely flagged would sit on a live
	# listing at the wrong price until somebody happened to edit that gun again.
	# The sweep is the one thing that noticed; it has to be the one that acts.
	#
	# No `job_id`, no `deduplicate`: Frappe drops a duplicate enqueue while the
	# first job is running, which would silently lose the second of two quick
	# changes and leave a mispriced listing with nothing marked dirty. The dirty
	# bit plus reading the price at send time already make this idempotent.
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_reprice_serial_task",
		queue="short", enqueue_after_commit=True, serial_no=serial_no)


def _recheck_after_end(rnd: _Round, serial_no: str, sku: str, item_id: str) -> None:
	"""After closing out an end we asked for, check the SKU once more.

	Costs nothing — it reads this round's shared index and sends no request. It
	is not here to catch an automatic relist (the owner confirmed GunBroker does
	not relist an ended or sold gun); it is here to catch a PERSON having
	re-listed the same gun by hand on GunBroker, and to catch an item number this
	round adopted that disagrees with the one now live.
	"""
	live = rnd.selling.match(sku)
	live_item_id = _Scan.item_id_of(live) if live else ""
	if not live_item_id or live_item_id == item_id:
		return
	listings._claim_item_id(serial_no, live_item_id, previous=item_id)
	# Ending it again goes through the full contract — delete, then read back
	# `isActive` false. "We sent a delete" is not evidence it happened.
	listings.end_listing(serial_no, client=rnd.client, reason=(
		"a live GunBroker listing reappeared under the same SKU after this gun's "
		"listing was ended"))


# ---------------------------------------------------------------------------
# Shared state transitions
# ---------------------------------------------------------------------------

def _adopt_live_listing(rnd: _Round, serial_no: str, sku: str,
		item_id: str) -> bool:
	"""Is GunBroker still selling this SKU? If so, make sure we point at it.

	Returns True when the listing is alive — whatever its number — so both arms
	can stop right there.

	The SKU is the key and the item number is only the payload. GunBroker's own
	guidance is to sync on SKU because a relist mints a new number and leaves the
	SKU alone, so a row that matches the SKU but carries no readable number still
	proves the listing exists; it just cannot tell us what to call it. Counting
	that as "not found" would be reading the ONE fact the row does carry as its
	opposite.
	"""
	live = rnd.selling.match(sku)
	if live is None:
		return False
	live_item_id = _Scan.item_id_of(live)
	if live_item_id and live_item_id != item_id:
		listings._claim_item_id(serial_no, live_item_id, previous=item_id)
	else:
		_clear_gone_counters(serial_no, listings.serial_state(serial_no))
	return True


def _alert_unusable_scan(serial_no: str) -> None:
	notify_admins(
		_("GunBroker listing scan unusable"),
		_("A GunBroker scan came back with rows carrying neither a SKU nor an "
		  "item number, so nothing could be concluded about serial {0}. No fields "
		  "were changed."
		  ).format(serial_no),
		dedup_key=f"gb_scan_unusable_{serial_no}", window_minutes=1440)


def _clear_gone_counters(serial_no: str, row: dict) -> None:
	if not row.get("gb_gone_strikes") and not row.get("gb_gone_first_at"):
		return
	frappe.db.set_value("Serial No", serial_no,
		{"gb_gone_strikes": 0, "gb_gone_first_at": None})


def _age_out(serial_no: str, item_id: str, row: dict, *, min_hours: int,
		alert_key: str | None, why: str, note: str = "") -> None:
	"""Count one observation that GunBroker does not have this gun, and act only
	once there are enough of them, far enough apart.

	Both conditions, both required. Strikes alone are not a clock — guns with an
	end pending are probed by the fifteen-minute order poll as well, so their
	strikes accumulate four times faster than an hourly gun's. The timestamp is
	what makes the threshold mean a duration.
	"""
	strikes = int(row.get("gb_gone_strikes") or 0) + 1
	first_at = row.get("gb_gone_first_at") or now_datetime()
	frappe.db.set_value("Serial No", serial_no, {
		"gb_gone_strikes": strikes,
		"gb_gone_first_at": first_at,
	})
	if strikes < MIN_STRIKES:
		return
	if time_diff_in_hours(now_datetime(), first_at) < min_hours:
		return

	_finish_ended(serial_no, item_id, row, note=note)
	if alert_key:
		notify_admins(
			_("GunBroker listing for {0} has ended").format(serial_no),
			_("{0} POS has marked it ENDED and will NOT relist it automatically — "
			  "somebody has to press Send to GunBroker if it should go back up. "
			  "Check GunBroker to see what happened."
			  ).format(why),
			dedup_key=alert_key, window_minutes=1440)


def _finish_ended(serial_no: str, item_id: str, row: dict, *, note: str = "") -> None:
	"""T5: the listing is over. Item number into history, ENDED, not UNLISTED.

	`gb_delisted` is what stops any machine path putting this gun back up. The
	distinction between ENDED and UNLISTED is the whole safety property: UNLISTED
	means "never listed, feel free", and a gun that has been through this arm has
	been listed and may have been sold.
	"""
	updates: dict[str, Any] = {
		"gb_item_id": "",
		"gb_end_pending": 0,
		"gb_delisted": 1,
		"gb_gone_strikes": 0,
		"gb_gone_first_at": None,
		"gb_synced_at": now_datetime(),
		"gb_bid_locked": 0,
	}
	if note:
		updates["gb_sync_error"] = note
	if item_id:
		updates["gb_item_history"] = listings._push_history(
			row.get("gb_item_history"), item_id)
	frappe.db.set_value("Serial No", serial_no, updates)


def _mark_sold_unknown(serial_no: str, item_id: str, *, reason: str) -> None:
	"""The gun is very probably sold on GunBroker and the order is not in yet.

	Fail CLOSED everywhere at once: the flag makes `serial_shelf_state` report it
	unsellable, which takes it out of the counter picker, the web order picker
	and the consignment picker, and makes the counter's hard gate refuse it
	without sending a single request. Cleared by the order poll matching it, or
	by a person confirming on GunBroker and pressing the button.

	The other storefronts are told immediately rather than waiting for the order
	to arrive. Between "probably sold" and "definitely sold" the gun is still
	buyable on the web, and that gap is exactly where a second sale happens.
	"""
	updates: dict[str, Any] = {
		"gb_sold_unknown": 1,
		"gb_sync_error": f"may already be sold on GunBroker: {reason}",
		"gb_synced_at": now_datetime(),
		"gb_gone_strikes": 0,
		"gb_gone_first_at": None,
		# The doubt flag holds the gun from here; the auction's promise, if
		# there was one, has run its course.
		"gb_bid_locked": 0,
	}
	if item_id:
		row = listings.serial_state(serial_no)
		updates["gb_item_id"] = ""
		updates["gb_item_history"] = listings._push_history(
			row.get("gb_item_history"), item_id)
	frappe.db.set_value("Serial No", serial_no, updates)

	fire_serial_channel_exit(serial_no, reason=REASON_RESERVED)
	_enqueue_order_sync()
	notify_admins(
		_("Serial {0} may have sold on GunBroker").format(serial_no),
		_("{0} POS has taken it off the shelf and off the other storefronts, and "
		  "the counter will refuse to sell it. Check GunBroker: if there is an "
		  "order it will be picked up by the next poll; if there is not, clear the "
		  "flag on the Serial No."
		  ).format(reason.capitalize() + "."),
		dedup_key=f"gb_sold_unmatched_{serial_no}", window_minutes=1440)


def _alert_duplicate(doctype: str, name: str, ids: list) -> None:
	"""Two live listings under one SKU, whichever kind of record owns it.

	Named by doctype rather than "serial": the item-level callers pass an Item
	code, and an alert about a serial number that does not exist sends whoever
	reads it looking in the wrong place.
	"""
	joined = ", ".join(str(i) for i in ids)
	# Not an edit of the record: what GunBroker is holding is an observation
	# about it, and bumping `modified` would show up as somebody having changed
	# the product or the gun.
	frappe.db.set_value(doctype, name, "gb_sync_error",
		f"duplicate listings on GunBroker: {joined}", update_modified=False)
	# The doctype is joined on OUTSIDE the translated string: a sentence built
	# around an untranslated schema name reads badly in every language.
	subject = f"{doctype} {name}"
	notify_admins(
		_("Duplicate GunBroker listings for {0}").format(name),
		_("GunBroker has more than one live listing for {0} (items {1}). "
		  "End the extra one there; POS will not choose between them."
		  ).format(subject, joined),
		# Shared with the button's own duplicate guard: one situation, one key.
		dedup_key=listings.dup_listing_key(doctype, name), window_minutes=1440)


ORDER_SYNC_PATH = "ffl_integrations.gunbroker.orders.sync_orders"


def _enqueue_order_sync() -> None:
	"""Pull GunBroker orders NOW rather than at the next scheduled poll.

	Every caller has just concluded a gun is probably sold, and the fifteen
	minutes until the next poll is fifteen minutes of a flagged gun and a worried
	operator.

	Resolved by name and skipped if absent: order polling arrives in the next
	change, and until it does an enqueue of a missing path would fail in a worker
	with a traceback that says nothing about why.
	"""
	try:
		frappe.get_attr(ORDER_SYNC_PATH)
	except Exception:  # noqa: BLE001 — not wired up yet
		return
	frappe.enqueue(ORDER_SYNC_PATH, queue="short", enqueue_after_commit=True,
		job_id="gb-sync-orders", deduplicate=True)
