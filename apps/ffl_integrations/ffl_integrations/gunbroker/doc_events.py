"""Everything that makes a GunBroker listing react to what happens in POS.

Two jobs, and they pull in opposite directions.

**Ending listings.** A gun sold at the counter, promised to a web order, or
moved out of stock must not stay buyable on GunBroker. Nothing in ERPNext emits
one tidy "this gun is spoken for" event, so five different documents are
watched: the stock ledger (the gun physically moved), Sales and POS Invoices
(the counter parked it, payment and 4473 pending, before any stock moves), a
Woo Online Order (a web order reserved it with a `db.set_value` that fires no
document event at all), and the Serial No itself (price and status edits). All
five funnel into ONE task, which re-asks the single shelf-state question rather
than trusting whatever the event happened to know.

**Refusing counter sales.** The other direction, and the more important one:
`block_sale_of_sold_gb_listing` runs on `before_submit` and stops a cashier
selling a gun GunBroker has already sold. GunBroker gives a seller no way to
cancel — the fix for a double sale is a refund and an apology — so this is the
one place in the integration that fails CLOSED.

It fails closed on exactly one thing: GunBroker saying, in a reply we
understood, that the listing has a buyer. Everything else — a timeout, a 403,
a listing GunBroker has stopped serving, a budget that ran out — warns and lets
the sale through. A gate that stops the register when a marketplace is slow is
a gate somebody switches off.

The first statement of the gate is an exemption, and it is not optional. A
consolidated POS invoice is submitted by a BACKGROUND JOB minutes after the
customer has left, and a throw there fails the consolidation rather than
stopping a sale — the exact shape of a previous outage. Same for a return: it
puts a gun BACK, and refusing it protects nobody. Every compliance hook in
ffl_core carries the same first line.
"""
from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.firearm import item_current_price
from ffl_core.reservations import committed_units, uniform_free_serials
from ffl_core.serials import serials_for_line
from ffl_integrations.gunbroker.client import GunBrokerClient, get_field
from ffl_integrations.notifications import notify_admins, safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"

# Shared by the hard gate and the scan-time advisory, so one gun probed twice in
# the same ticket costs one request. Keyed on the ITEM number, not the serial:
# that is what the request is about, and two serials never share one.
PROBE_CACHE_PREFIX = "gb:probe:"
PROBE_CACHE_TTL = 3

# Connect phase ceiling for a probe. The read phase gets whatever is left of the
# ticket's budget; connecting is either fast or not happening.
PROBE_CONNECT_TIMEOUT = 1.0

# Fields the gate and the sync task read off a Serial No.
_GATE_FIELDS = ("item_code", "gb_item_id", "gb_sold_unknown", "gb_bid_locked")

# The probe verdict for a live auction that has a bid. Not "sold" — no order
# exists and none may ever (the reserve can go unmet) — but the gun is
# promised, and a promise blocks the counter the same way a sale does.
STATE_BID_LOCKED = "bid_locked"


# ---------------------------------------------------------------------------
# Shared gating
# ---------------------------------------------------------------------------

def _skip() -> bool:
	"""True when nothing GunBroker-facing should happen in this context.

	`gb_sync_inbound` is GunBroker's OWN echo flag and deliberately not the Woo
	one: sharing a flag would mean an order poll from GunBroker also silenced
	every WooCommerce push, and the two channels have to be able to react to
	each other — that is the entire point of the cross-channel work.
	"""
	if frappe.flags.get("gb_sync_inbound"):
		return True
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
		return True
	from ffl_integrations.integration_gate import gunbroker_enabled

	return not gunbroker_enabled()


def _exempt_invoice(doc) -> bool:
	"""The three invoice shapes no GunBroker hook may act on.

	`is_consolidated` / `consolidated_invoice` — the consolidation of a day's POS
	invoices runs as a background job long after the sale. It is not a new sale
	and there is no cashier to warn; a throw here fails the job instead, leaving
	the day unconsolidated.

	`is_return` — a return puts the gun back on the shelf. Blocking it, or
	ending a listing over it, is backwards.
	"""
	return bool(
		doc.get("is_consolidated")
		or doc.get("consolidated_invoice")
		or doc.get("is_return"))


def _settings():
	return frappe.get_single(SETTINGS_DOCTYPE)


def _enqueue_sync(serial_no: str) -> None:
	"""Queue the one task that re-decides this gun's listing, after commit.

	`job_id` + `deduplicate` because the same gun genuinely arrives here several
	times for one event — an invoice submit fires the invoice hook and then a
	stock ledger entry per line — and each of those would otherwise be its own
	round trip to GunBroker.
	"""
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_sync_serial_listing_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-end:{serial_no}", deduplicate=True,
		serial_no=serial_no)


def _listed_serials(rows) -> list[str]:
	"""The serials on these document rows that GunBroker currently carries.

	Filtering here rather than in the task keeps a counter sale of five
	accessories and one un-listed rifle from queueing six pointless jobs.
	"""
	out: list[str] = []
	for row in rows:
		for serial_no in serials_for_line(row):
			if frappe.db.get_value("Serial No", serial_no, "gb_item_id"):
				out.append(serial_no)
	return out


# ---------------------------------------------------------------------------
# Stock Ledger Entry
# ---------------------------------------------------------------------------

def on_sle_submit(doc, method=None) -> None:
	"""A gun physically moved. If GunBroker has it listed, re-decide the listing."""
	_from_sle(doc)


def on_sle_cancel(doc, method=None) -> None:
	"""A reversed stock movement changes the answer just as the movement did."""
	_from_sle(doc)


def _from_sle(sle) -> None:
	if _skip():
		return
	for serial_no in _listed_serials([sle]):
		_enqueue_sync(serial_no)
	_from_sle_item(sle)


def _from_sle_item(sle) -> None:
	"""The item-level half of a stock movement: see :func:`sync_item_level_listing`."""
	item_code = (getattr(sle, "item_code", None) or "").strip()
	if not item_code:
		return
	sync_item_level_listing(item_code)


def sync_item_level_listing(item_code: str) -> None:
	"""The item-level half: a listed model's count changed, or a sold-out one
	came back into stock.

	Two shapes, one read of the Item. A model GunBroker currently carries has
	its quantity re-sent (the task compares before spending a call, because this
	fires on every movement of every listed item). A model it does NOT carry is
	only interesting if it used to — that is the restock path, and it is gated
	on the same row so an ordinary receipt of an unlisted product costs one
	indexed read and nothing else.

	Reached from a stock movement (:func:`_from_sle_item`), from another
	channel's change to what it has committed
	(``crosschannel.channel_commitments_changed``) and from a counter or web hold
	(:func:`on_invoice_change`, :func:`on_woo_order_change`) — the count is the shelf
	less both, so any of them moves it.

	"Item-level" here means anything that lists as a quantity: ammunition, optics
	and parts, AND a uniform-price gun's MODEL listing, whose quantity is the shared
	pool of guns rather than a Bin. A gun priced one by one is left alone — it
	lists by serial and has its own task.
	"""
	row = frappe.db.get_value(
		"Item", item_code,
		["is_firearm", "uniform_sell_price", "gb_item_id", "gb_item_history", "gb_delisted"],
		as_dict=True) or {}
	_dispatch_item_sync(item_code, row)


def _dispatch_item_sync(item_code: str, row: dict) -> None:
	if not row or (row.get("is_firearm") and not row.get("uniform_sell_price")):
		return
	if row.get("gb_item_id"):
		_enqueue_item_quantity(item_code)
	elif (row.get("gb_item_history") or "").strip() and not row.get("gb_delisted"):
		_enqueue_item_relist(item_code)


def sync_model_listings(item_codes) -> None:
	"""Re-decide the model listing of every uniform-price gun among these Items.

	One query for the lot — the callers are invoice and order hooks, which fire on
	every save of a counter ticket, and a ticket of accessories must cost one
	indexed read, not one per line.
	"""
	codes = tuple(sorted({(c or "").strip() for c in item_codes} - {""}))
	if not codes:
		return
	rows = frappe.db.sql(
		"""
		SELECT name AS item_code, is_firearm, uniform_sell_price, gb_item_id,
		       gb_item_history, gb_delisted
		FROM `tabItem`
		WHERE name IN %(codes)s
		  AND COALESCE(is_firearm, 0) = 1 AND COALESCE(uniform_sell_price, 0) = 1
		  AND (COALESCE(gb_item_id, '') != ''
		       OR (COALESCE(gb_item_history, '') != '' AND COALESCE(gb_delisted, 0) = 0))
		""", {"codes": codes}, as_dict=True)
	for row in rows:
		_dispatch_item_sync(row["item_code"], row)


def _enqueue_item_quantity(item_code: str) -> None:
	"""Queue the quantity push, after commit and deduplicated per item.

	One stock document routinely produces several ledger entries for the same
	item (a transfer is two, a repost more), and a counter sale of three
	different calibres of the same ammunition is three lines of one Item.
	"""
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_sync_item_quantity_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-qty:{item_code}", deduplicate=True,
		item_code=item_code)


def _enqueue_item_relist(item_code: str) -> None:
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_relist_item_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-relist:{item_code}", deduplicate=True,
		item_code=item_code)


# ---------------------------------------------------------------------------
# Sales Invoice / POS Invoice
# ---------------------------------------------------------------------------

def on_invoice_change(doc, method=None) -> None:
	"""A counter order parked a gun (submit) or released it (cancel).

	Stock has not moved — the gun is Active on its shelf, waiting for payment
	and a 4473 — but it is promised, and a promised gun must come off GunBroker
	straight away rather than at the next hourly sweep. Cancel runs through the
	same task, which finds the gun sellable again and does nothing: ending is a
	one-way door here (a machine never re-lists).
	"""
	if _skip() or _exempt_invoice(doc):
		return
	for serial_no in _listed_serials(doc.get("items") or []):
		_enqueue_sync(serial_no)
	# A model's count is the pool of free guns, and a gun on an open ticket is not
	# free. No serial is looked at: the model listing has none to end.
	sync_model_listings(row.get("item_code") for row in (doc.get("items") or []))


def on_invoice_draft_change(doc, method=None) -> None:
	"""A DRAFT counter ticket was saved or deleted: a gun on it is held, or no longer.

	``POS Invoice.on_update`` and ``on_trash``. A parked POS sale holds its guns from
	the first save (`reservations.counter_holds`), long before anything submits, and
	a model listing that goes on offering the held gun is the oversell the counter
	exists to avoid. Only the model half runs here: a per-gun listing is ended when
	the ticket SUBMITS (:func:`on_invoice_change`), and saving a draft many times a
	minute must not become a GunBroker round trip for every gun on it.
	"""
	if frappe.utils.cint(doc.get("docstatus")) != 0 or _exempt_invoice(doc) or _skip():
		return
	sync_model_listings(row.get("item_code") for row in (doc.get("items") or []))


# ---------------------------------------------------------------------------
# Woo Online Order
# ---------------------------------------------------------------------------

def on_woo_order_change(doc, method=None) -> None:
	"""A web order took a gun GunBroker is also selling.

	This hook exists because the web reservation is written with
	``db.set_value(update_modified=False)`` — no stock ledger entry, no invoice,
	no Serial No save. Not one of the other four hooks fires. Without it the
	single most common way a gun becomes unavailable would be invisible here
	until the hourly sweep, and under the manual end-listing strategy "we alert
	an hour late" means "a person acts an hour late" — an hour in which the gun
	can be bought on GunBroker and cannot be un-bought.

	BOTH serial columns are read. A web order that names a gun writes
	`confirmed_serial`; one that merely claims a unit writes
	`stock_hold_serial`, and the picker behind it excludes reserved guns but NOT
	listed ones — being on GunBroker is not being reserved — so a placeholder
	lands on a listed gun routinely. A handler watching only `confirmed_serial`
	would miss precisely that case.
	"""
	if _skip():
		return
	seen: set[str] = set()
	for row in (doc.get("serials") or []):
		for field in ("confirmed_serial", "stock_hold_serial"):
			serial_no = (row.get(field) or "").strip()
			if not serial_no or serial_no in seen:
				continue
			seen.add(serial_no)
			if frappe.db.get_value("Serial No", serial_no, "gb_item_id"):
				_enqueue_sync(serial_no)
	# A web order that reserves (or releases) a gun of a model GunBroker also lists
	# changes the pool that model's quantity is cut from.
	sync_model_listings(row.get("item_code") for row in (doc.get("serials") or []))


# ---------------------------------------------------------------------------
# Serial No
# ---------------------------------------------------------------------------

# Changes that must reach GunBroker. `sale_price` and the image gallery are
# deliberately absent: this phase does not mirror the storefront's promotional
# windows onto GunBroker (a marketplace fee makes a sale price a bad trade), and
# a photo edit is not worth a listing update.
_SERIAL_WATCHED = ("sell_price", "gb_list_price", "gb_listing_title", "status", "consigned_out")
# The three that change what a live listing shows; any of them marks it dirty.
_LISTING_TEXT_FIELDS = ("sell_price", "gb_list_price", "gb_listing_title")


def on_serial_update(doc, method=None) -> None:
	"""A listed gun's price or availability changed on the Serial No itself.

	A price change sets the dirty bit and lets the reprice task read the CURRENT
	price when it runs — never the value captured at trigger time. Two edits in
	quick succession then publish the second one; a job carrying a snapshot
	would publish whichever won the race, which is how a gun ends up listed at a
	price nobody chose.
	"""
	if _skip():
		return
	if not doc.get("gb_item_id"):
		return
	changed = [f for f in _SERIAL_WATCHED if doc.has_value_changed(f)]
	if not changed:
		return
	if any(field in changed for field in _LISTING_TEXT_FIELDS):
		frappe.db.set_value("Serial No", doc.name, "gb_price_dirty", 1,
			update_modified=False)
		frappe.enqueue(
			"ffl_integrations.gunbroker.tasks.gb_reprice_serial_task",
			queue="short", enqueue_after_commit=True, serial_no=doc.name)
	if "status" in changed or "consigned_out" in changed:
		_enqueue_sync(doc.name)


# ---------------------------------------------------------------------------
# Item (a model's price and standing)
# ---------------------------------------------------------------------------

# Changes to an Item that move the price its model listing should carry. The web
# store's price is `standard_rate` unless a sale window is open, so any of the four
# can change what GunBroker should show.
_MODEL_PRICE_FIELDS = ("standard_rate", "sale_price", "sale_from", "sale_to")


def on_item_update(doc, method=None) -> None:
	"""Item ``on_update``: keep a MODEL's GunBroker listing in step with its Item.

	Two outcomes, and only for an Item that has a model listing live (a firearm
	Item carrying ``gb_item_id`` — ammunition and parts are left exactly as they
	were, and a gun priced gun by gun lists by serial):

	* **Still a uniform-price gun, enabled:** if the price the web store charges
	  now differs from the one GunBroker was last confirmed at, mark it dirty and
	  queue the reprice (the task reads the price when it RUNS, so two quick edits
	  publish the second). A save that changed no price field costs one read.
	* **No longer one — price per gun, not a firearm any more, or disabled:** the
	  listing sells guns the model no longer stands for, so it is ended and an
	  alert goes out. Ended with the "taken down by hand" sentinel, because a person
	  did this: a machine must not put it back if the Item is switched back on.

	Sale windows that open or close with no save are the nightly
	``gb_model_price_sweep``'s job, not this hook's.
	"""
	# The cheap test first: this runs on every save of every Item on the site.
	if not doc.get("gb_item_id") or _skip():
		return
	before = doc.get_doc_before_save()
	was_model = bool(before and frappe.utils.cint(before.get("is_firearm"))
		and frappe.utils.cint(before.get("uniform_sell_price")))
	uniform_gun = bool(frappe.utils.cint(doc.get("is_firearm"))
		and frappe.utils.cint(doc.get("uniform_sell_price")))
	if not (was_model or uniform_gun):
		return
	if not uniform_gun or frappe.utils.cint(doc.get("disabled")):
		_end_model_for_item_change(doc, uniform_gun)
		return
	if flt(item_current_price(doc.name, row=doc)) <= 0:
		# Unpriced now: GunBroker would go on selling at the last price it was sent.
		_end_model_for_item_change(doc, uniform_gun, why=_("the Item has no price"))
		return
	if not (doc.is_new() or any(doc.has_value_changed(f) for f in _MODEL_PRICE_FIELDS)):
		return
	mark_model_price_dirty(doc.name, row=doc)


def mark_model_price_dirty(item_code: str, *, row=None) -> bool:
	"""Queue a reprice when the model's current price is not the one on GunBroker.

	Returns whether one was queued. No ``job_id``: Frappe drops a duplicate enqueue
	while the first job is running, which would lose the second of two quick edits;
	the dirty bit plus reading the price at send time make this idempotent.
	"""
	if row is None:
		row = frappe.db.get_value(
			"Item", item_code,
			["standard_rate", "sale_price", "sale_from", "sale_to", "gb_price_pushed"],
			as_dict=True) or {}
	price = flt(item_current_price(item_code, row=row))
	pushed = flt(row.get("gb_price_pushed"))
	if price <= 0 or abs(price - pushed) <= 0.005:
		return False
	frappe.db.set_value("Item", item_code, "gb_price_dirty", 1, update_modified=False)
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_reprice_item_task",
		queue="short", enqueue_after_commit=True, item_code=item_code)
	return True


def _end_model_for_item_change(doc, still_a_gun: bool, why: str = "") -> None:
	if not why:
		if frappe.utils.cint(doc.get("disabled")):
			why = _("the Item was disabled")
		elif still_a_gun:
			why = _("the Item is no longer a firearm priced as one product")
		else:
			why = _("the Item is no longer priced as one product for all its guns")
	frappe.db.set_value("Item", doc.name, "gb_sync_error",
		f"listing ended: {why}", update_modified=False)
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_end_item_listing_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-end-item:{doc.name}", deduplicate=True,
		item_code=doc.name, reason=why)
	notify_admins(
		_("GunBroker listing for {0} is being ended").format(doc.name),
		_("{0}: {1}, so its GunBroker model listing (item {2}) is being ended. It "
		  "will not go back up on its own — send it again by hand if that was a "
		  "mistake.").format(doc.name, why, doc.get("gb_item_id")),
		dedup_key=f"gb_model_ended_{doc.name}", window_minutes=1440)


# ---------------------------------------------------------------------------
# The "has somebody already bought it?" probe
# ---------------------------------------------------------------------------

def gb_item_sold(item: dict) -> bool:
	"""Does this item detail say the listing has a buyer?

	A UNION of four candidates, on purpose, and it is wider than it needs to be
	because nobody has yet read back a fixed-price listing AFTER a Buy Now — the
	sandbox account has no second buyer to press the button. Only the un-sold
	shape has been observed.

	So each candidate is a reasonable reading of "sold" and any of them counts.
	The asymmetry is the point: a false positive refuses one counter sale and a
	person checks GunBroker; a false negative sells a gun that already belongs
	to somebody else. Once a real post-sale read-back exists, the
	candidates that turn out not to fire get deleted and this narrows.
	"""
	if not item:
		return False
	if get_field(item, "hasWinningBid", "HasWinningBid"):
		return True
	if get_field(item, "buyer", "Buyer"):
		return True
	# Bids on a FIXED-PRICE listing are one of the sold candidates; bids on an
	# auction are the auction doing what auctions do, and are answered by
	# `gb_item_bid_locked` instead. An auction that ended WITH a winner says so
	# through hasWinningBid / buyer above.
	if flt(get_field(item, "bids", "Bids") or 0) > 0 \
			and get_field(item, "isFixedPrice", "IsFixedPrice") is not False:
		return True
	is_active = get_field(item, "isActive", "IsActive")
	quantity = flt(get_field(item, "quantity", "Quantity") or 0)
	return is_active is False and quantity == 0


def gb_item_bid_locked(item: dict) -> bool:
	"""Is this a live auction with at least one bid?

	The first bid is the moment the gun stops being the store's to sell
	elsewhere: GunBroker will hold the seller to the high bid when the clock
	runs out. Until then nothing has been sold, so this is a state of its own,
	and it can go away again — bids are retracted, reserves go unmet.
	"""
	if not item:
		return False
	if get_field(item, "isFixedPrice", "IsFixedPrice") is not False:
		return False
	if get_field(item, "isActive", "IsActive") is False:
		return False
	return flt(get_field(item, "bids", "Bids") or 0) > 0


def _probe_cache_get(item_id: str):
	local = getattr(frappe.local, "_gb_probe_cache", None)
	if local is not None and item_id in local:
		return local[item_id]
	try:
		return frappe.cache().get_value(PROBE_CACHE_PREFIX + item_id)
	except Exception:  # noqa: BLE001 — no cache just means no shortcut
		return None


def _probe_cache_set(item_id: str, verdict: dict) -> None:
	local = getattr(frappe.local, "_gb_probe_cache", None)
	if local is None:
		local = {}
		frappe.local._gb_probe_cache = local
	local[item_id] = verdict
	try:
		frappe.cache().set_value(
			PROBE_CACHE_PREFIX + item_id, verdict, expires_in_sec=PROBE_CACHE_TTL)
	except Exception:  # noqa: BLE001
		pass


def _warm_token() -> None:
	"""Get a token ready for the NEXT scan, off the critical path.

	A probe with no cached token does not log in inline: a login plus a read is
	most of a till's whole budget. It answers "unknown", and this puts a token in
	the cache so the next gun scanned gets a real answer.
	"""
	try:
		frappe.enqueue("ffl_integrations.gunbroker.tasks.gb_warm_token",
			queue="short", job_id="gb-warm-token", deduplicate=True)
	except Exception:  # noqa: BLE001 — warming is opportunistic
		pass


def _fast_client(budget: float) -> tuple[GunBrokerClient | None, str]:
	"""A client for the till's probe, or ``(None, why)`` when there is no safe one.

	Timeouts bounded by what is left of the ticket's budget, and NO login inline:
	a probe with no cached token answers "unknown" and warms one for the next. Shared
	by the per-gun probe and the model probe so the two cannot disagree about how
	much a till is allowed to wait.
	"""
	try:
		client = GunBrokerClient.from_settings_fast(
			timeout=(min(PROBE_CONNECT_TIMEOUT, budget), budget))
	except Exception as exc:  # noqa: BLE001 — switched off, or half-configured
		return None, str(exc)
	if not client.cached_token():
		_warm_token()
		return None, "no cached GunBroker session"
	return client, ""


def _probe_item(item_id: str, *, budget: float) -> dict:
	"""One item probe inside a caller's remaining time. Never raises.

	Returns ``{"state": …, "item_id": …}`` where state is:

	``sold``      GunBroker says this listing has a buyer.
	``clear``     GunBroker says it is live and un-sold.
	``ended``     GunBroker answered, and the listing is not active but not sold
	              either — withdrawn, or inside a relist window where the old
	              item number is dead and the new one is not visible yet.
	``unknown``   No usable answer: 404, timeout, 403, no token, no budget.

	`ended` and `unknown` are separate states with the SAME consequence (warn,
	do not block) because the messages differ: one sends a person to look at a
	listing, the other to look at the network.
	"""
	cached = _probe_cache_get(item_id)
	if cached:
		return cached
	if budget <= 0:
		return {"state": "unknown", "item_id": item_id, "detail": "no time left"}
	client, detail = _fast_client(budget)
	if client is None:
		return {"state": "unknown", "item_id": item_id, "detail": detail}
	try:
		probe = client.item_probe(item_id)
	except Exception as exc:  # noqa: BLE001 — a probe never breaks a sale
		return {"state": "unknown", "item_id": item_id, "detail": str(exc)}

	if probe["state"] != "ok":
		verdict = {"state": "unknown", "item_id": item_id, "detail": probe["state"]}
		_probe_cache_set(item_id, verdict)
		return verdict
	item = probe.get("item") or {}
	if gb_item_bid_locked(item):
		verdict = {"state": STATE_BID_LOCKED, "item_id": item_id}
	elif gb_item_sold(item):
		verdict = {"state": "sold", "item_id": item_id}
	elif get_field(item, "isActive", "IsActive") is False:
		verdict = {"state": "ended", "item_id": item_id}
	else:
		verdict = {"state": "clear", "item_id": item_id}
	_probe_cache_set(item_id, verdict)
	return verdict


def probe_gb_sale(serial_no: str, *, timeout: int = 3) -> dict | None:
	"""Probe ONE serial. The entry point the scan-time advisory uses.

	Returns None when this gun has no GunBroker listing at all — the common
	case, and it costs one indexed read and no network.
	"""
	if _skip():
		return None
	row = frappe.db.get_value(
		"Serial No", serial_no, list(_GATE_FIELDS), as_dict=True) or {}
	if row.get("gb_sold_unknown"):
		return {"state": "sold", "item_id": "", "local": True}
	item_id = str(row.get("gb_item_id") or "")
	if row.get("gb_bid_locked"):
		return {"state": STATE_BID_LOCKED, "item_id": item_id, "local": True}
	if not item_id:
		return None
	return _probe_item(item_id, budget=float(timeout))


def model_sale_verdict(item_code: str, *, selling: int = 1,
		ignore_hold_ref: str | None = None, budget: float = 3) -> dict | None:
	"""Would selling ``selling`` of this MODEL's guns at the counter oversell it?

	A model's pool is local: free guns less what every channel has told us it sold.
	GunBroker may have sold more than it has told us — a buyer's purchase reaches the
	order poll or the webhook after the fact — and those units are still counted as
	free. The same remote read the quantity push does (the live listing's count
	against the baseline our last write recorded, ``item_listings.unseen_units``) says
	how many, and this asks whether selling the gun at the counter leaves the pool
	short of them.

	Refuses (``state="refuse"``) only on a reply that was understood and shows
	``free - owed - unseen < selling``. Everything else — GunBroker unreachable, no
	cached session, the budget spent, a chain too long to follow — is
	``state="unresolved"``: warn and let the sale through, the same call the per-gun
	gate makes, because a till that stops when a marketplace is slow is a till
	somebody works around. None when there is nothing to say (not a listed model, or
	nothing unseen).

	Args:
	    item_code: The Item of the gun being sold.
	    selling: How many guns of it this sale takes.
	    ignore_hold_ref: The ticket selling them, whose own hold is not a rival.
	    budget: Seconds this probe may spend in total.
	"""
	from ffl_integrations.gunbroker import item_listings

	row = item_listings.item_state(item_code)
	if not (item_listings.is_model(row) and row.get("gb_item_id")):
		return None
	if budget <= 0:
		return {"state": "unresolved", "item_code": item_code, "detail": "no time left"}
	client, detail = _fast_client(budget)
	if client is None:
		return {"state": "unresolved", "item_code": item_code, "detail": detail}
	listing = item_listings.read_model_listing(
		client, row, deadline=time.monotonic() + budget)
	if listing["state"] == "unreadable":
		return {"state": "unresolved", "item_code": item_code,
			"detail": "GunBroker's listing could not be read"}
	chain = listing["chain"]
	if not (row.get("gb_qty_basis_id") and chain and chain[0] == str(row["gb_qty_basis_id"])):
		return None  # no baseline to measure GunBroker's own sales against
	unseen = item_listings.unseen_units(item_code, row, listing["remote"], chain)
	if not unseen:
		return None
	item_listings._nudge_order_sync(after_commit=False)
	try:
		free = len(uniform_free_serials(item_code, ignore_hold_ref=ignore_hold_ref))
		owed = sum(committed_units(item_code).values())
	except Exception:  # noqa: BLE001 — the pool could not be read: warn, do not block
		return {"state": "unresolved", "item_code": item_code,
			"detail": "a sales channel could not say what it owes"}
	if free - owed - unseen >= max(1, int(selling)):
		return None
	return {"state": "refuse", "item_code": item_code, "unseen": unseen,
		"message": _(
			"GunBroker has already sold {0} unit(s) of {1} that have not come through "
			"as orders yet, and only {2} free gun(s) remain after the orders already "
			"owed — selling this one at the counter would leave a GunBroker buyer "
			"without a gun. Wait for the order to come in (it was just requested) or "
			"cancel this line."
		).format(unseen, item_code, max(0, free - owed))}


# ---------------------------------------------------------------------------
# The hard gate
# ---------------------------------------------------------------------------

def _model_lines(rows) -> dict[str, int]:
	"""``{item code: guns}`` a ticket sells of Items that have a live MODEL listing.

	Args:
	    rows: The ticket's ``(serial_no, Serial No columns)`` pairs, already read by
	        the gate — the serials are not resolved a second time.
	"""
	selling: dict[str, int] = {}
	for _serial_no, state in rows:
		item_code = (state.get("item_code") or "").strip()
		if item_code:
			selling[item_code] = selling.get(item_code, 0) + 1
	if not selling:
		return {}
	listed = frappe.db.sql(
		"""
		SELECT name FROM `tabItem`
		WHERE name IN %(codes)s AND COALESCE(is_firearm, 0) = 1
		  AND COALESCE(uniform_sell_price, 0) = 1 AND COALESCE(gb_item_id, '') != ''
		""", {"codes": tuple(selling)})
	return {code: selling[code] for (code,) in listed}


def block_sale_of_sold_gb_listing(doc, method=None) -> None:
	"""Refuse to submit an invoice selling a gun GunBroker has already sold.

	Runs on `Sales Invoice.before_submit` and `POS Invoice.before_submit`,
	alongside — and always AFTER — Payroc's partial-capture guard. Local checks
	before network ones: that guard is pure arithmetic and this one is an HTTP
	request.

	The whole ticket shares ONE time budget (`probe_timeout_seconds`). Guns are
	probed one at a time until it runs out; whatever is left unprobed is
	reported in a single warning and the sale proceeds. A per-request timeout
	would have been no promise at all — three guns at three seconds each is nine
	seconds at the till, and the promise made to the owner was three.

	A GunBroker order's own invoice is not a counter sale: the buyer GunBroker
	reports IS this order's buyer. Probing it found its own listing sold and
	refused the revenue booking after Dispose, stranding the order with no
	invoice and nothing on its card to ship it with.
	"""
	if _skip() or _exempt_invoice(doc) or doc.get("gunbroker_order"):
		return

	rows = []
	for row in (doc.get("items") or []):
		for serial_no in serials_for_line(row):
			state = frappe.db.get_value(
				"Serial No", serial_no, list(_GATE_FIELDS), as_dict=True)
			if state:
				rows.append((serial_no, state))

	# Local, and therefore free and immune to a network problem: a gun already
	# flagged as probably-sold-on-GunBroker is refused before anything is sent.
	# This is the one refusal that cannot be talked out of by an outage.
	flagged = [sn for sn, state in rows if state.get("gb_sold_unknown")]
	if flagged:
		frappe.throw(_(
			"Serial {0} may already have been sold on GunBroker — the listing has "
			"gone and the sold list shows the same gun, but the order has not come "
			"through yet. Check GunBroker before selling it at the counter. If it "
			"was not sold there, clear the flag on the Serial No first."
		).format(", ".join(flagged)), title=_("GunBroker sale in doubt"))

	# The bid lock is local and costs no request: the sweep or the auction
	# watch set it when the first bid arrived, and it stays until the auction
	# is over. Refused before any listing is probed.
	promised = [f"{sn} (item {state.get('gb_item_id') or '?'})" for sn, state in rows
		if state.get("gb_bid_locked")]
	if promised:
		frappe.throw(_(
			"Serial {0} has bids in a GunBroker auction — it is promised to the "
			"high bidder until the auction ends. Cancel this line."
		).format(", ".join(promised)), title=_("Promised on GunBroker"))
	listed = [(sn, str(state.get("gb_item_id") or "")) for sn, state in rows
		if state.get("gb_item_id")]
	# Guns of a MODEL listing carry no listing of their own: what could already be
	# sold there is a count, asked of the Item below.
	models = _model_lines(rows)
	if not listed and not models:
		return
	budget = flt(_settings().get("probe_timeout_seconds") or 3)
	deadline = time.monotonic() + budget
	sold: list[str] = []
	unresolved: list[str] = []
	for serial_no, item_id in listed:
		verdict = _probe_item(item_id, budget=deadline - time.monotonic())
		if verdict["state"] == STATE_BID_LOCKED:
			promised.append(f"{serial_no} (item {item_id})")
		elif verdict["state"] == "sold":
			sold.append(f"{serial_no} (item {item_id})")
		elif verdict["state"] != "clear":
			unresolved.append(f"{serial_no} (item {item_id})")
	if promised:
		frappe.throw(_(
			"Serial {0} has bids in a GunBroker auction — it is promised to the "
			"high bidder until the auction ends. Cancel this line."
		).format(", ".join(promised)), title=_("Promised on GunBroker"))
	if sold:
		frappe.throw(_(
			"Serial {0} already has a buyer on GunBroker — do not sell it at the "
			"counter. Cancel this line."
		).format(", ".join(sold)), title=_("Already sold on GunBroker"))

	refusals = []
	for item_code, count in models.items():
		verdict = model_sale_verdict(
			item_code, selling=count, ignore_hold_ref=doc.name,
			budget=deadline - time.monotonic())
		if not verdict:
			continue
		if verdict["state"] == "refuse":
			refusals.append(verdict["message"])
		else:
			unresolved.append(f"{item_code} (model listing)")
	if refusals:
		frappe.throw("<br>".join(refusals), title=_("Sold on GunBroker"))

	if unresolved:
		# Fail OPEN, loudly. Everything that lands here is an absence of
		# evidence, not evidence of a sale: a listing GunBroker has stopped
		# serving, a relist window where the old item number is dead, a timeout,
		# a budget that ran out. Refusing on any of those hands a marketplace
		# outage the power to close the register.
		message = _(
			"Could not confirm on GunBroker whether these guns have been bought: "
			"{0}. They may have been ended or automatically relisted. The sale has "
			"NOT been blocked — check GunBroker before handing the gun over."
		).format(", ".join(unresolved))
		frappe.msgprint(message, title=_("GunBroker not reached"), indicator="orange")
		safe_log_error("GunBroker pre-sale probe unresolved", message)


# ---------------------------------------------------------------------------
# Serial renumbering
# ---------------------------------------------------------------------------

def on_serial_renamed(old_name: str, new_name: str) -> None:
	"""A listed gun's serial number was corrected. End the listing.

	GunBroker's `SerialNumber` cannot be changed: the field is accepted by a PUT
	and silently ignored, so a listing keeps advertising the wrong number
	forever. Publishing a firearm under a serial that is not the one on the
	receiver is not something to leave running, so the listing is ended and a
	person re-lists it — with the correction visible in both the error field and
	the alert, so nobody has to guess what happened.
	"""
	if _skip():
		return
	if not frappe.db.get_value("Serial No", new_name, "gb_item_id"):
		return
	# update_modified=False: this runs in a background job, and bumping
	# `modified` would hand a TimestampMismatch to anybody who happens to have
	# that Serial No open. Same posture as every other machine write here.
	frappe.db.set_value(
		"Serial No", new_name, "gb_sync_error",
		f"serial changed — relist required ({old_name} -> {new_name}); "
		f"GunBroker cannot edit a listing's serial number",
		update_modified=False)
	_enqueue_sync_end(new_name, reason=(
		f"serial changed from {old_name} to {new_name}"))


def _enqueue_sync_end(serial_no: str, *, reason: str) -> None:
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_end_listing_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-end:{serial_no}", deduplicate=True,
		serial_no=serial_no, reason=reason)
