"""Item-level listings: one GunBroker listing standing for a quantity of stock.

The channel's second listing kind. A gun gets a listing of its own because it
is one physical object with a serial number and a bound-book entry; a case of
ammunition, a red dot or a magazine does not — those are interchangeable units,
and the store sells them the way the web store does, as one listing carrying a
count.

What that changes, and why each is here rather than in :mod:`listings`:

* **Nothing here is a per-gun firearm.** ``IsFFLRequired`` is forced false for
  ammunition, optics and parts: no serial is reserved, no disposition is booked
  and no bound-book entry exists. The guard below refuses a gun priced one by one
  outright. A UNIFORM-priced gun is the one firearm that lists here, as a MODEL
  (see "Models" below): its guard matrix, payload and quantity rule are its own,
  because it needs a licence at the far end and a serial picked at fulfilment.
* **The quantity moves.** A per-gun listing is one unit forever; this one
  tracks stock, so every ledger movement can change it. That makes "how many are
  left" the thing most likely to be wrong, and an auction site punishes it
  differently from a web store: an oversell there is a refund, a fee and a
  seller rating, not an apologetic email. Hence the push on the ledger event
  rather than the two-hourly reconcile the web store settles for.
* **Sold out ends the listing.** GunBroker refuses ``Quantity: 0`` outright
  (``The field Quantity must be an integer greater than 0``), so there is no
  "listed but unavailable" state to fall back on.

Models. A uniform-price firearm Item (one product, N interchangeable guns, one
price) lists as ONE listing carrying the number of guns that are sellable, under
the bare item code as its SKU. Its quantity is not the Bin: it is the shared
pool's ``uniform_sellable_units`` (free guns less every channel's sold-and-unnamed
units), pushed by :func:`sync_model_quantity`, whose formula also has to allow for
sales GunBroker has taken that no order has brought in yet (see there). Its price
follows the web store's, promotion window included, and an Item that stops being
a uniform-price gun ends its listing. The mechanics below serve both kinds.

The post / reprice / end mechanics themselves are NOT duplicated here: they are
the same three transactions with the same failure ladders, and they live in
:mod:`listings` parameterised by doctype. This module is the item-level guard
matrix, the quantity rules and the four entry points.
"""
from __future__ import annotations

import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, time_diff_in_seconds

from ffl_core.firearm import _is_firearm_row, item_current_price
from ffl_core.reservations import committed_units, uniform_sellable_units
from ffl_core.utils import commit
from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerConfigError,
	get_field,
)
from ffl_integrations.notifications import safe_log_error
from ffl_integrations.gunbroker.listings import (
	ALREADY_LISTED,
	BAD_LISTING_TERMS,
	CATEGORY_AMBIGUOUS,
	DELISTED,
	DROPSHIP_PRICED,
	NO_CATEGORY,
	NO_IMAGES,
	NO_SHIP_COST,
	NOT_SELLABLE,
	PER_SERIAL_LIVE,
	PUSH_IN_FLIGHT,
	QUANTITY_UNREADABLE,
	UNPRICED,
	skip,
)

ITEM_DOCTYPE = "Item"

# How long after a listing ends before a restock may put it back. Long enough
# that a morning's sell-out and an afternoon delivery are one listing rather
# than two; short enough that a day's stock is never off the channel.
RELIST_COOLDOWN_SECONDS = 4 * 60 * 60

# Item-level's own skip reason. The rest are shared with the per-gun matrix so
# a caller matching on names does not need to know which kind it asked about.
IS_FIREARM = "is_firearm"

_ITEM_STATE_FIELDS = (
	"item_code", "item_name", "item_group", "is_firearm", "uniform_sell_price", "disabled",
	"has_serial_no", "standard_rate", "gb_item_id", "gb_item_history", "gb_delisted",
	"gb_end_pending", "gb_push_pending", "gb_push_attempted_at", "gb_price_dirty",
	"gb_qty_pushed", "gb_sync_error", "gb_synced_at",
	"gb_qty_basis", "gb_qty_basis_id", "gb_qty_basis_ingested", "gb_price_pushed",
)

# How many replacement hops a model's quantity read will follow from the listing
# the baseline was written to. Every partial sale (and every thirty-day auto
# relist) is one hop, so this is "sales GunBroker took since our last write, none
# of them brought in yet" — far beyond any day's real traffic.
# lazy: a longer chain reads as unreadable (no PUT, the sweep retries), not as a
# guess; raise the limit if a very busy listing ever needs it.
MODEL_CHAIN_LIMIT = 10


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def item_state(item_code: str, *, for_update: bool = False) -> dict:
	"""The Item's listing-relevant columns, one read."""
	row = frappe.db.get_value(
		ITEM_DOCTYPE, item_code, list(_ITEM_STATE_FIELDS),
		as_dict=True, for_update=for_update)
	return dict(row) if row else {}


def sellable_qty(item_code: str) -> int:
	"""How many units to offer, floored at zero.

	Counted in the ONE warehouse the order will ship from
	(``default_stock_warehouse``, which excludes the consignment and custody
	warehouses), not summed across every Bin. The all-Bin figure the Item badge
	shows would offer stock sitting in a consigned-out or in-transit warehouse
	that ``_issue_stock_units`` cannot issue from — and the shortfall would not
	surface until a buyer had paid and somebody pressed Dispose.

	Less what every channel has SOLD and not yet shipped
	(``reservations.committed_units``): the Bin only drops when the goods leave,
	so between a sale and its Dispose the shelf still holds units that are spoken
	for. Without this a quantity push after a GunBroker sale put the sold boxes
	straight back on the listing (GunBroker decrements its own count at purchase,
	we push the Bin), and a web or GunBroker order for the same stock was invisible
	to the other. That includes GunBroker's own open orders. The Bin stays this
	channel's own base (the web store counts all Bins less custody); only the
	subtracted term is shared.

	A channel that cannot answer RAISES, and so does this: a quantity computed
	without it is larger than the truth. Every writer is a job that logs and keeps
	what it last sent.

	No safety margin is held back. That is a decision, not an oversight: the
	counter, the web store and GunBroker share one pile of stock, and reserving
	two units from each channel would make the last few of everything
	permanently unsellable everywhere. The oversell window is squeezed instead
	by pushing on the ledger event, which closes it to seconds.

	Fractional stock is floored — GunBroker counts in whole units, and offering
	"1" of 1.4 boxes rounds the safe way.

	Item-level means a non-firearm; a uniform-price gun's quantity (the shared
	pool of serials) arrives with model listings and is not read here.
	"""
	from ffl_core.warehouses import default_stock_warehouse

	warehouse = default_stock_warehouse(_company())
	if not warehouse:
		return 0
	total = flt(frappe.db.get_value(
		"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0)
	return max(0, int(total) - sum(committed_units(item_code).values()))


def _company() -> str:
	"""The company whose warehouse this channel sells out of — the same one the
	revenue invoice is raised against, so the count and the issue cannot come
	from different sets of books."""
	from ffl_integrations.gunbroker import revenue

	return revenue._default_company()


def is_model(row: dict) -> bool:
	"""True for a uniform-price firearm — the one gun that lists by model.

	The same rule as ``ffl_core.reservations.is_uniform_firearm_item`` (which
	``orders.is_model_unit_line`` calls), over an already-fetched row: the
	firearm test is ``ffl_core.firearm._is_firearm_row`` (flag or firearm
	group), so the listing side and the order side cannot disagree about which
	Items are models.
	"""
	return bool(row) and _is_firearm_row(row) and bool(cint(row.get("uniform_sell_price")))


def listing_quantity(item_code: str, row: dict | None = None) -> int:
	"""How many units this Item's listing should offer right now.

	A model offers the shared pool's sellable guns; everything else the default
	warehouse's Bin less what the channels owe (:func:`sellable_qty`). Raises when a
	channel cannot answer — both rules do, and every writer treats that as "do not
	push", never as zero.
	"""
	row = row if row is not None else item_state(item_code)
	if is_model(row):
		return uniform_sellable_units(item_code)
	return sellable_qty(item_code)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def check_item_push_guards(item_code: str, *, relist: bool = False,
		settings=None, state: dict | None = None,
		context: dict | None = None) -> dict | None:
	"""Every reason this Item does not go on GunBroker, in priority order.

	Answered from local data before a client exists, like the per-gun matrix:
	a refusal costs no HTTP call and reads no credential. Returns a skip dict,
	or None when the listing may proceed.

	Args:
	    item_code: The Item to list.
	    relist: The operator explicitly asked to re-list something that had been
	        ended by hand. Lifts the ``delisted`` sentinel and nothing else.
	    settings: An already-loaded GunBroker Settings (optional).
	    state: An already-loaded :func:`item_state` (optional).
	    context: An already-loaded item listing context (optional).

	Returns:
	    A skip dict, or None.
	"""
	settings = settings or listings._settings()
	gap = listings._config_gap(settings)
	if gap:
		return skip(listings.NOT_CONFIGURED, _(
			"GunBroker is not configured yet — missing: {0}.").format(gap))

	row = state if state is not None else item_state(item_code)
	if not row:
		return skip(NOT_SELLABLE, _("There is no Item {0}.").format(item_code))

	# 1. A firearm is somebody else's listing kind. A gun priced one by one goes
	#    through push_serial; a uniform-priced one lists as a MODEL, with a guard
	#    matrix of its own (a licence at the far end, a serial chosen when one
	#    sells). Ahead of everything else because listing a gun as no-FFL-required
	#    is the one mistake here that is a compliance problem rather than a lost
	#    sale — nothing below this line is ever reached by a firearm.
	if row.get("is_firearm"):
		if is_model(row):
			return check_model_push_guards(
				item_code, relist=relist, settings=settings, state=row, context=context)
		return skip(IS_FIREARM, _(
			"{0} is a firearm priced gun by gun. Guns are listed one at a time from "
			"the Serial No form; only a model priced as one product lists by model."
		).format(item_code))

	if row.get("disabled"):
		return skip(NOT_SELLABLE, _("{0} is disabled.").format(item_code))

	# A serialized non-firearm (a scope with its own serial, say) cannot leave
	# through a quantity issue — issue_stock_qty refuses it. Caught here rather
	# than at Dispose, which is after the buyer has paid.
	if row.get("has_serial_no"):
		return skip(NOT_SELLABLE, _(
			"{0} is tracked by serial number, so it cannot be listed as a "
			"quantity. List it one unit at a time."
		).format(item_code))

	# 2. Somebody ended this listing by hand. A machine never puts it back —
	#    only the operator pressing the button again, which is what `relist` is.
	if row.get("gb_delisted") and not relist:
		return skip(DELISTED, _(
			"{0} was taken off GunBroker by hand. Use Send to GunBroker again to "
			"re-list it."
		).format(item_code))

	# 3. Already listed. A local read, ahead of anything that costs a call.
	if row.get("gb_item_id"):
		return skip(ALREADY_LISTED, _(
			"{0} is already on GunBroker as item {1}."
		).format(item_code, row.get("gb_item_id")))

	# 4. An attempt whose outcome nobody knows. Posting again is how one model
	#    ends up with two live listings — GunBroker enforces no uniqueness.
	if row.get("gb_push_pending"):
		return skip(PUSH_IN_FLIGHT, _(
			"An earlier listing attempt for {0} has not been confirmed yet. The "
			"system re-checks within the hour."
		).format(item_code))

	if flt(row.get("standard_rate") or 0) <= 0:
		return skip(UNPRICED, _("{0} has no sell price.").format(item_code))

	if sellable_qty(item_code) <= 0:
		return skip(NOT_SELLABLE, _(
			"{0} has nothing in stock. GunBroker rejects a listing of zero, so "
			"there is nothing to send until some arrives."
		).format(item_code))

	ctx = context or mapping.item_listing_context(item_code)
	if mapping.listing_ship_cost(
			ctx["item"].get("gb_ship_cost"), settings.get("accessory_ship_cost")) <= 0:
		return skip(NO_SHIP_COST, _(
			"{0} has no GunBroker shipping cost of its own and no accessory / ammo "
			"default is set in GunBroker Settings, so it cannot be listed."
		).format(item_code))

	if mapping.resolve_item_category(
			item_override=ctx["item"].get("gb_category_id"),
			item_group=ctx["item"].get("item_group") or "",
			category_map=settings.get("category_map") or []) <= 0:
		return skip(NO_CATEGORY, _(
			"No GunBroker category is configured for {0}. Add a row for its Item "
			"Group in GunBroker Settings, or set a GunBroker Category ID on the "
			"Item itself."
		).format(item_code))

	blocked = listings._title_guard(ctx["item"].get("item_name") or item_code, settings,
		_("Rename the Item and try again."))
	if blocked:
		return blocked

	if not mapping.item_candidate_image_paths(item_code, settings=settings, context=ctx):
		return skip(NO_IMAGES, _(
			"{0} has no usable photo. GunBroker will take the listing without one, "
			"but nobody buys from an empty picture box."
		).format(item_code))

	return None


def check_model_push_guards(item_code: str, *, relist: bool = False, settings=None,
		state: dict | None = None, context: dict | None = None,
		listing_type: str = mapping.LISTING_FIXED) -> dict | None:
	"""Every reason this uniform-price gun MODEL does not go on GunBroker.

	The matrix of :func:`check_item_push_guards` plus what only a gun adds, in
	priority order and answered from local data before a client exists. Not
	checked here, on purpose: whether each gun is free to sell. That is the
	quantity (a model with nothing sellable is refused), not a guard on the model.

	Args:
	    item_code: The Item to list.
	    relist: The operator explicitly asked to re-list a model they ended by
	        hand. Lifts the ``delisted`` sentinel and nothing else.
	    settings: An already-loaded GunBroker Settings (optional).
	    state: An already-loaded :func:`item_state` (optional).
	    context: An already-loaded item listing context (optional).
	    listing_type: The kind of listing asked for. A model lists at a fixed
	        price only.

	Returns:
	    A skip dict, or None when the listing may proceed.
	"""
	from ffl_integrations.integration_gate import gunbroker_enabled

	if not gunbroker_enabled():
		return skip(listings.DISABLED, _(
			"GunBroker is switched off. Turn it on in GunBroker Settings first."))
	settings = settings or listings._settings()
	gap = listings._config_gap(settings)
	if gap:
		return skip(listings.NOT_CONFIGURED, _(
			"GunBroker is not configured yet — missing: {0}.").format(gap))

	row = state if state is not None else item_state(item_code)
	if not row:
		return skip(NOT_SELLABLE, _("There is no Item {0}.").format(item_code))

	# 1. Not a model at all. A gun priced one by one keeps the wording the Item
	#    guard has always used; anything else belongs to the quantity guard.
	if not is_model(row):
		return skip(IS_FIREARM if row.get("is_firearm") else NOT_SELLABLE, _(
			"{0} is not a firearm priced as one product, so it is not listed by model."
		).format(item_code))

	if row.get("disabled"):
		return skip(NOT_SELLABLE, _("{0} is disabled.").format(item_code))

	# 2. Priced by a distributor markup. The website shows that price, not the
	#    Item's, and the stock is not on our shelf: a model listing would sell the
	#    drop-ship price's gun at the Item's tag and from a quantity of free guns
	#    that is not the supply behind it.
	if _dropship_priced(item_code):
		return skip(DROPSHIP_PRICED, _(
			"{0} is priced by the drop-ship markup rules, so it cannot be listed as a "
			"model — GunBroker would show a price and a quantity that are not the "
			"drop-ship offer's."
		).format(item_code))

	# 3. Somebody ended this listing by hand. A machine never puts it back.
	if row.get("gb_delisted") and not relist:
		return skip(DELISTED, _(
			"{0} was taken off GunBroker by hand. Use Send to GunBroker again to "
			"re-list it."
		).format(item_code))

	# 4. Already listed, and 5. an attempt nobody knows the outcome of: local reads,
	#    and the two ways one model ends up with two live listings.
	if row.get("gb_item_id"):
		return skip(ALREADY_LISTED, _(
			"{0} is already on GunBroker as item {1}."
		).format(item_code, row.get("gb_item_id")))
	if row.get("gb_push_pending"):
		return skip(PUSH_IN_FLIGHT, _(
			"An earlier listing attempt for {0} has not been confirmed yet. The "
			"system re-checks within the hour."
		).format(item_code))

	# 6. A gun of this model is already on GunBroker under its own serial. Its
	#    sale would owe nothing to this model's quantity and the buyer could pick
	#    the same gun from either listing.
	live = _live_serial_listing(item_code)
	if live:
		return skip(PER_SERIAL_LIVE, _(
			"Serial {0} of {1} is already listed (or being listed or ended) on GunBroker "
			"on its own. End that listing first — one gun cannot be sold by serial and "
			"by model at once."
		).format(live, item_code))

	ctx = context or mapping.item_listing_context(item_code)
	item = ctx["item"]

	if mapping.model_price(item) <= 0:
		return skip(UNPRICED, _("{0} has no sell price.").format(item_code))

	try:
		sellable = uniform_sellable_units(item_code)
	except Exception:  # noqa: BLE001 — a channel that cannot answer must not read as zero
		safe_log_error(f"GunBroker model quantity unreadable for {item_code}",
			frappe.get_traceback())
		return skip(QUANTITY_UNREADABLE, _(
			"The number of {0} that can be sold could not be worked out, so nothing "
			"was sent. Try again shortly."
		).format(item_code))
	if sellable <= 0:
		return skip(NOT_SELLABLE, _(
			"{0} has no gun free to sell — in stock, not held and not already sold "
			"on a channel. GunBroker rejects a listing of zero."
		).format(item_code))

	if mapping.model_ship_cost(item, settings) <= 0:
		return skip(NO_SHIP_COST, _(
			"{0} has no GunBroker shipping cost of its own and the long gun / handgun "
			"default is not set in GunBroker Settings, so the buyer would be charged "
			"$0 to ship a firearm."
		).format(item_code))

	if mapping.model_category(item, settings) <= 0:
		return skip(NO_CATEGORY, _(
			"No GunBroker category is configured for firearm type {0!r}. Set a GunBroker "
			"Category ID on the Item, or add a row to the category map in GunBroker "
			"Settings."
		).format(str(item.get("firearm_type") or "") or _("(blank)")))
	if mapping.model_category_ambiguous(item, settings):
		return skip(CATEGORY_AMBIGUOUS, _(
			"{0} is a type the category map files by the gun's age (Curios & Relics), "
			"and the Item has no year made. Fill in the year, or set a GunBroker "
			"Category ID on the Item, so it is not filed in the wrong category."
		).format(item_code))

	blocked = listings._title_guard(item.get("item_name") or item_code, settings,
		_("Rename the Item and try again."))
	if blocked:
		return blocked

	if not mapping.item_candidate_image_paths(item_code, settings=settings, context=ctx):
		return skip(NO_IMAGES, _(
			"{0} has no usable photo. GunBroker will take the listing without one, "
			"but nobody buys from an empty picture box."
		).format(item_code))

	if listing_type != mapping.LISTING_FIXED:
		return skip(BAD_LISTING_TERMS, _(
			"A model lists at a fixed price. An auction has one winner, and a model "
			"is sold to as many buyers as there are guns."))
	return None


def _dropship_priced(item_code: str) -> bool:
	"""Does a drop-ship layer price this Item for the website? (the
	``distributor_web_price`` hook — empty on a site with no such extension)."""
	for path in frappe.get_hooks("distributor_web_price"):
		if frappe.get_attr(path)(item_code) is not None:
			return True
	return False


def _live_serial_listing(item_code: str) -> str:
	"""The first gun of this Item with a GunBroker listing of its own that is live,
	being created or being ended — or ""."""
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabSerial No`
		WHERE item_code = %s
		  AND (COALESCE(gb_item_id, '') != ''
		       OR COALESCE(gb_push_pending, 0) = 1
		       OR COALESCE(gb_end_pending, 0) = 1)
		LIMIT 1
		""", (item_code,))
	return rows[0][0] if rows else ""


# ---------------------------------------------------------------------------
# T1 — publish
# ---------------------------------------------------------------------------

def push_item(item_code: str, *, relist: bool = False, premium: bool = False,
		client: GunBrokerClient | None = None) -> dict:
	"""Publish one Item on GunBroker as a quantity listing.

	Args:
	    item_code: The Item to list.
	    relist: Lift the `delisted` sentinel set by a manual End Listing. It
	        lifts nothing else — not the firearm refusal, not the price or stock
	        guards, and not `already_listed`, which has nothing to re-list.
	    premium: Buy the paid promotions recorded on the Item (a model only —
	        ammunition and parts never buy any). True only for a person pressing
	        Send: GunBroker bills each one and nothing else can add one later, so
	        the machine paths that re-list a sold-out model (:func:`relist_on_restock`,
	        the sweep) leave it False and spend nothing the operator did not just
	        agree to.
	    client: An existing client (optional).

	Returns:
	    ``{"ok": True, "gb_item_id": ..., "quantity": n, "warnings": [...]}`` or
	    a skip dict. Never raises for an expected failure.
	"""
	settings = listings._settings()
	context = mapping.item_listing_context(item_code)
	model = is_model(context["item"])

	refusal = check_item_push_guards(
		item_code, relist=relist, settings=settings, context=context)
	if refusal:
		log_sync_error(item_code, refusal["message"])
		return refusal

	if model:
		quantity = uniform_sellable_units(item_code)
		built = mapping.build_model_listing_payload(
			item_code, quantity=quantity, premium=premium, settings=settings, context=context)
	else:
		quantity = sellable_qty(item_code)
		built = mapping.build_item_listing_payload(
			item_code, quantity=quantity, settings=settings, context=context)
	warnings: list[str] = list(built["warnings"])

	pictures = mapping.item_picture_parts(item_code, settings=settings, context=context)
	warnings.extend(pictures["warnings"])

	# Transaction A: record the attempt BEFORE it goes out, under a short lock.
	# Same reasoning as the per-gun path — a post whose outcome is lost must not
	# look untouched to the next click.
	locked = item_state(item_code, for_update=True)
	if locked.get("gb_push_pending") or locked.get("gb_item_id"):
		commit()
		return check_item_push_guards(item_code, relist=relist, settings=settings,
			state=locked, context=context) or skip(ALREADY_LISTED, _(
				"{0} was listed by another session a moment ago.").format(item_code))
	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		"gb_push_pending": 1,
		"gb_push_attempted_at": now_datetime(),
	}, update_modified=False)
	commit()

	try:
		client = client or GunBrokerClient.from_settings()
	except GunBrokerConfigError as exc:
		frappe.db.set_value(ITEM_DOCTYPE, item_code, {
			"gb_push_pending": 0, "gb_push_attempted_at": None}, update_modified=False)
		log_sync_error(item_code, str(exc))
		return skip(listings.AUTH_FAILED, str(exc))

	result = listings.post_listing(
		ITEM_DOCTYPE, item_code, built["payload"], pictures["parts"],
		warnings=warnings, relist=relist, client=client, settings=settings,
		subject=_("item {0}").format(item_code),
		log_error=lambda message: log_sync_error(item_code, message))
	if result.get("ok"):
		# The count that went out, so a later ledger movement can tell whether
		# anything actually changed before spending a call to say so.
		sent = {"gb_qty_pushed": quantity}
		if model:
			# The baseline the next quantity push measures GunBroker's own sales
			# against (see sync_model_quantity), and the price the nightly sweep
			# compares with the web store's.
			new_id = str(result.get("gb_item_id") or "")
			sent.update({
				"gb_qty_basis": quantity, "gb_qty_basis_id": new_id,
				"gb_qty_basis_ingested": _ingested_units(item_code, {new_id}),
				"gb_price_pushed": built["payload"]["FixedPrice"], "gb_price_dirty": 0})
		frappe.db.set_value(ITEM_DOCTYPE, item_code, sent, update_modified=False)
		result["quantity"] = quantity
		if model:
			_requeue_if_moved(item_code, quantity)
	return result


def _requeue_if_moved(item_code: str, sent: int) -> None:
	"""A model's sellable count can move while a push is in flight, and the enqueue
	that would have caught it was very likely dropped (Frappe counts a STARTED job
	as already queued). Reading it again here is what keeps that sale from sitting
	unseen until the next unrelated movement."""
	try:
		again = uniform_sellable_units(item_code)
	except Exception:  # noqa: BLE001 — the post landed; the sweep re-checks
		safe_log_error(f"GunBroker quantity re-read failed for {item_code}", frappe.get_traceback())
		return
	if again != sent:
		enqueue_quantity_sync(item_code)


def enqueue_quantity_sync(item_code: str, suffix: str = "") -> None:
	"""Queue the one quantity task for an Item, after commit, one per Item.

	The same ``job_id`` every trigger uses, so a receipt, a counter hold and a
	GunBroker claim for one model arriving together are one round trip.
	"""
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_sync_item_quantity_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-qty:{item_code}{suffix}", deduplicate=True,
		item_code=item_code)


# ---------------------------------------------------------------------------
# The Send dialog's side (models)
# ---------------------------------------------------------------------------

def listing_offer(item_code: str) -> dict:
	"""Everything the Send dialog shows for a model before somebody decides.

	Args:
	    item_code: The Item about to be listed.

	Returns:
	    The figures a listing sent now would carry: how many guns are sellable
	    (None when a channel could not answer), the price GunBroker would show and
	    whether a promotion is making it lower than the regular one, the category
	    (and whether it is ambiguous), the shipping, and the paid promotions
	    recorded on the Item.
	"""
	settings = listings._settings()
	ctx = mapping.item_listing_context(item_code)
	item = ctx["item"]
	try:
		sellable = uniform_sellable_units(item_code)
	except Exception:  # noqa: BLE001 — the dialog opens; the push re-checks
		sellable = None
	price = mapping.model_price(item)
	regular = float(flt(item.get("standard_rate") or 0))
	on_sale = 0 < price < regular
	category_id = mapping.model_category(item, settings)
	rows = settings.get("category_map") or []
	title, _warnings = mapping.gb_title(item.get("item_name") or item_code)
	return {
		"is_model": is_model(item),
		"title": title,
		"sellable": sellable,
		"price": price,
		"regular_price": regular,
		"on_sale": on_sale,
		"sale_to": str(item.get("sale_to") or "") if on_sale else "",
		"category": {
			"id": category_id,
			"name": mapping.category_map_names(rows).get(category_id, ""),
			"ambiguous": mapping.model_category_ambiguous(item, settings),
		},
		"shipping": listings.ship_cost_summary(ITEM_DOCTYPE, item_code),
		"premium": mapping.premium_flags(item),
	}


def set_listing_spec(item_code: str, spec: dict | None) -> None:
	"""Record what the Send dialog chose for a model's listing, for the push to read.

	Only the four paid promotions are the person's choice here; the title, price,
	category and condition of a model follow its Item. So a spec that asks for
	anything else is refused rather than half honoured — a price typed into a
	dialog that is then ignored is a listing nobody meant. The promotions are
	written (all four, so unticking clears) only when the spec carries ``premium``.
	"""
	raw = spec or {}
	extra = sorted(set(raw) - {"type", "premium"})
	if extra:
		frappe.throw(_("A model's {0} follows its Item and cannot be set when sending.")
			.format(extra[0]), title=_("Listing terms"))
	try:
		terms = mapping.validate_listing_spec(raw)
	except ValueError as exc:
		frappe.throw(_("{0} cannot be listed: {1}.").format(item_code, exc),
			title=_("Listing terms"))
	if terms["type"] != mapping.LISTING_FIXED:
		frappe.throw(_("A model is listed at a fixed price, not as an auction."),
			title=_("Listing terms"))
	if raw.get("premium") is None:
		return
	if not is_model(item_state(item_code)) and any(terms["premium"].values()):
		frappe.throw(_("Paid promotions are only for a model listing; {0} is not one.")
			.format(item_code), title=_("Listing terms"))
	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		field: int(terms["premium"][key])
		for key, (field, _gb_key) in mapping.PREMIUM_FEATURES.items()},
		update_modified=False)


# ---------------------------------------------------------------------------
# T2 — reprice
# ---------------------------------------------------------------------------

def update_item_price(item_code: str, *, client: GunBrokerClient | None = None) -> dict:
	"""Push the Item's current selling rate to its GunBroker listing.

	A model follows the web store's price, promotion window included
	(:func:`ffl_core.firearm.item_current_price`), and records the figure the
	listing was read back at (``gb_price_pushed``). Anything else follows
	``standard_rate``.
	"""
	model = is_model(item_state(item_code))
	if model:
		def read_price() -> float:
			return flt(item_current_price(item_code))
	else:
		def read_price() -> float:
			return flt(frappe.db.get_value(ITEM_DOCTYPE, item_code, "standard_rate") or 0)
	result = listings.update_price_for(
		ITEM_DOCTYPE, item_code, client=client,
		subject=_("item {0}").format(item_code),
		read_locked=lambda: item_state(item_code, for_update=True),
		read_price=read_price,
		log_error=lambda message: log_sync_error(item_code, message))
	if model and result.get("ok"):
		frappe.db.set_value(ITEM_DOCTYPE, item_code,
			"gb_price_pushed", result["fixed_price"], update_modified=False)
	return result


# ---------------------------------------------------------------------------
# T2b — quantity
# ---------------------------------------------------------------------------

def sync_item_quantity(item_code: str, *, client: GunBrokerClient | None = None,
		force: bool = False) -> dict:
	"""Tell GunBroker how many are left, or end the listing when none are.

	The hot path of this module: it runs behind every stock movement of every
	listed item, and most movements do not change the number GunBroker holds
	(a receipt into a second warehouse, a reposted ledger entry, a sale of a
	different item in the same document). So the first thing it does is
	compare, and the common case costs one indexed read and no API call.

	Selling out ends the listing rather than pushing zero, which GunBroker
	rejects. It ends it WITHOUT setting the `delisted` sentinel — that flag
	means "a person took this down", and stock reaching zero is not a person.
	Leaving it clear is what lets :func:`relist_on_restock` put the listing
	back when more arrives.
	"""
	row = item_state(item_code)
	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		return {"ok": True, "skipped": listings.UNLISTED}
	if is_model(row):
		return sync_model_quantity(item_code, client=client, force=force, row=row)

	quantity = sellable_qty(item_code)
	if quantity <= 0:
		result = end_item_listing(
			item_code, reason=_("sold out — GunBroker does not accept a quantity of zero"),
			client=client, delisted=False)
		result["quantity"] = 0
		return result

	if not force and cint(row.get("gb_qty_pushed") or 0) == quantity:
		return {"ok": True, "quantity": quantity, "unchanged": True}

	try:
		client = client or GunBrokerClient.from_settings()
		client.update_listing(item_id, {"Quantity": quantity})
	except Exception as exc:  # noqa: BLE001 — a stock hook must never fail a sale
		log_sync_error(item_code, f"quantity update failed: {exc}")
		return skip(listings.REPRICE_FAILED, _(
			"Could not update the GunBroker quantity for item {0}: {1}"
		).format(item_code, exc))

	# GunBroker answers 200 for updates it discarded, and the update most likely
	# to have been discarded is one addressed to a listing it replaced after its
	# last partial sale. So the listing is read back — but only for `isActive`,
	# which is a property of the LISTING and has read true immediately in every
	# recorded observation. Deliberately NOT the count: nothing has ever measured
	# how long GunBroker takes to reflect one, and a comparison resting on an
	# unmeasured delay would fail-close writes that did land.
	moved_to = _listing_moved_on(client, item_id)
	if moved_to is not None:
		if moved_to:
			# The replacement is named, so there is no reason to wait an hour for
			# the sweep to find it by SKU: adopt it and queue the count again.
			claim_item_listing(item_code, moved_to, previous=item_id)
			frappe.enqueue(
				"ffl_integrations.gunbroker.tasks.gb_sync_item_quantity_task",
				queue="short", enqueue_after_commit=True,
				job_id=f"gb-qty:{item_code}:{moved_to}", deduplicate=True,
				item_code=item_code)
		return skip(listings.REPRICE_UNCONFIRMED, _(
			"The GunBroker listing for item {0} is no longer the live one, so the "
			"count was not recorded against it."
		).format(item_code), verified=False, quantity=quantity)

	# Re-read before recording: the listing may have been ended or relisted
	# under a new number while the call was in flight, and recording the count
	# against a listing that no longer exists would suppress the next push.
	current = item_state(item_code)
	if str(current.get("gb_item_id") or "") != item_id:
		return skip(listings.REPRICE_UNCONFIRMED, _(
			"The GunBroker listing for item {0} changed while the quantity was "
			"being sent. It will be sent again on the next movement."
		).format(item_code), verified=False)

	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		"gb_qty_pushed": quantity,
		"gb_synced_at": now_datetime(),
		"gb_sync_error": "",
	}, update_modified=False)

	# Stock can move while the call above is in flight, and the enqueue that
	# would have caught it was very likely dropped: the doc_event deduplicates
	# on item_code, and Frappe counts a STARTED job as already enqueued — so a
	# sale during this HTTP round trip queues nothing at all. Re-reading here is
	# what keeps that sale from sitting unseen until the next unrelated movement
	# (the hourly sweep is the outer net, not the first one).
	try:
		again = sellable_qty(item_code)
	except Exception:  # noqa: BLE001 — the PUT landed; keep its record, the sweep re-checks
		safe_log_error(f"GunBroker quantity re-read failed for {item_code}", frappe.get_traceback())
		again = quantity
	if again != quantity:
		frappe.enqueue(
			"ffl_integrations.gunbroker.tasks.gb_sync_item_quantity_task",
			queue="short", enqueue_after_commit=True,
			job_id=f"gb-qty:{item_code}:{again}", deduplicate=True,
			item_code=item_code)
	return {"ok": True, "quantity": quantity}


def sync_model_quantity(item_code: str, *, client: GunBrokerClient | None = None,
		force: bool = False, row: dict | None = None) -> dict:
	"""Tell GunBroker how many guns of this MODEL can still be promised.

	The number is the shared pool's (``target`` below: free guns less every
	channel's sold-and-unnamed units) MINUS the sales GunBroker has taken that no
	order has brought in yet. That second term is the whole difficulty: GunBroker
	decrements its own count at the moment of purchase, but the order only reaches
	us on the next poll or webhook, so for a while the pool still counts a gun that
	is already sold. Pushing the pool's figure then would put it straight back on
	the listing.

	So the remote count is read first, and what GunBroker has lost since our last
	write is worked out from the baseline that write recorded::

	    unseen = max(0, basis - ingested - remote)
	    push   = max(0, target - unseen)

	``basis`` / ``gb_qty_basis_id`` are the quantity and listing our last POST or
	PUT landed on; ``ingested`` is what GunBroker orders for this SKU on that
	listing and its replacements have since brought in (any status — a cancelled
	order was still one GunBroker took the unit for, and the claim it leaves is
	released through the pool, not here); ``remote`` is the live listing's count. A
	partial sale ends the listing and relists the remainder under a new number
	(``relistedAsItemID``), so the live one is found by following that chain from
	the baseline's number — and a write addressed to a dead number is silently
	discarded, which makes the PUT a compare-and-set: it is read back for
	``isActive`` and anything but a clean answer records nothing.

	``unseen > 0`` also asks for an order sync, so the sale is brought in without
	waiting for the next poll.

	When the remote count cannot be read nothing is sent: a stale answer is a
	number that may be too HIGH. The one write that stays safe unread is the end —
	the pool says nothing at all is sellable.

	Args:
	    item_code: The listed model.
	    client: An existing client (optional).
	    force: Send even when GunBroker already holds the computed number (the
	        sweep, after it has seen a drift).
	    row: An already-loaded :func:`item_state` (optional).

	Returns:
	    ``{"ok": True, "quantity": n}`` (with ``unchanged`` when GunBroker already
	    held it), the end result when nothing is sellable, or a skip dict.
	"""
	row = row if row is not None else item_state(item_code)
	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		return {"ok": True, "skipped": listings.UNLISTED}

	try:
		target = uniform_sellable_units(item_code)
	except Exception:  # noqa: BLE001 — a channel that cannot answer must not read as zero
		safe_log_error(f"GunBroker model quantity unreadable for {item_code}",
			frappe.get_traceback())
		log_sync_error(item_code, "quantity not sent: a sales channel could not say how many it owes")
		return skip(QUANTITY_UNREADABLE, _(
			"Could not work out how many {0} can be sold, so the GunBroker quantity "
			"was left as it is."
		).format(item_code))
	if target <= 0:
		result = end_item_listing(
			item_code, reason=_("sold out — GunBroker does not accept a quantity of zero"),
			client=client, delisted=False)
		result["quantity"] = 0
		return result

	try:
		client = client or GunBrokerClient.from_settings()
	except (GunBrokerConfigError, GunBrokerError) as exc:
		log_sync_error(item_code, f"quantity not sent: {exc}")
		return skip(QUANTITY_UNREADABLE, str(exc))

	listing = read_model_listing(client, row)
	if listing["state"] == "ended" and item_id not in listing["chain"] \
			and row.get("gb_qty_basis_id"):
		# The baseline's listing ended without leading to the one we hold (a number
		# the sweep adopted by SKU, say). Read the one we hold, with no baseline: an
		# honest "cannot tell what was sold" is zero here, never a guess.
		listing = read_model_listing(client, {**row, "gb_qty_basis_id": ""})
	if listing["state"] == "unreadable":
		log_sync_error(item_code, "quantity not sent: GunBroker's listing could not be read")
		return skip(QUANTITY_UNREADABLE, _(
			"Could not read the GunBroker listing for {0}, so its quantity was left "
			"as it is."
		).format(item_code))

	chain = listing["chain"]
	remote = listing["remote"]
	baseline = bool(row.get("gb_qty_basis_id")) and chain[0] == str(row["gb_qty_basis_id"])
	unseen = unseen_units(item_code, row, remote, chain) if baseline else 0
	if unseen:
		_nudge_order_sync()

	if listing["state"] == "ended":
		return _model_listing_over(item_code, row, listing, unseen=unseen, baseline=baseline)

	live_id = listing["live_id"]
	push = max(0, target - unseen)
	if push <= 0:
		# Every gun still free is one GunBroker has already sold. The orders will
		# say so within minutes; until they do the listing is selling stock that is
		# not there.
		result = end_item_listing(
			item_code, reason=_("every remaining unit is already sold on GunBroker"),
			client=client, delisted=False)
		result["quantity"] = 0
		return result

	if live_id != item_id:
		claim_item_listing(item_code, live_id, previous=item_id)
	if not force and remote == push:
		if unseen == 0:
			_record_baseline(item_code, push, live_id)
		elif cint(row.get("gb_qty_pushed")) != push or live_id != item_id:
			# The baseline stays (see below); only the "what it holds now" display moves.
			frappe.db.set_value(ITEM_DOCTYPE, item_code, "gb_qty_pushed", push,
				update_modified=False)
		return {"ok": True, "quantity": push, "unchanged": True}

	try:
		client.update_listing(live_id, {"Quantity": push})
	except Exception as exc:  # noqa: BLE001 — a stock hook must never fail a sale
		log_sync_error(item_code, f"quantity update failed: {exc}")
		return skip(listings.REPRICE_FAILED, _(
			"Could not update the GunBroker quantity for item {0}: {1}"
		).format(item_code, exc))

	# GunBroker answers 200 for updates it discarded, and the one most likely to
	# have been discarded is addressed to a listing that sold in the meantime. See
	# sync_item_quantity for why only `isActive` is read back, not the count.
	moved_to = _listing_moved_on(client, live_id)
	if moved_to is not None:
		if moved_to:
			claim_item_listing(item_code, moved_to, previous=live_id)
			enqueue_quantity_sync(item_code, f":{moved_to}")
		return skip(listings.REPRICE_UNCONFIRMED, _(
			"The GunBroker listing for item {0} is no longer the live one, so the "
			"count was not recorded against it."
		).format(item_code), verified=False, quantity=push)

	current = item_state(item_code)
	if str(current.get("gb_item_id") or "") != live_id:
		return skip(listings.REPRICE_UNCONFIRMED, _(
			"The GunBroker listing for item {0} changed while the quantity was "
			"being sent. It will be sent again on the next movement."
		).format(item_code), verified=False)

	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		"gb_qty_pushed": push,
		"gb_qty_basis": push,
		"gb_qty_basis_id": live_id,
		"gb_qty_basis_ingested": _ingested_units(item_code, {live_id}),
		"gb_synced_at": now_datetime(),
		"gb_sync_error": "",
	}, update_modified=False)
	_requeue_if_moved(item_code, target)
	return {"ok": True, "quantity": push}


def _record_baseline(item_code: str, quantity: int, listing_id: str) -> None:
	"""Re-anchor the baseline on what GunBroker holds now. Only when nothing is
	unseen: otherwise the sales it would forget are exactly the ones still owed."""
	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		"gb_qty_basis": quantity, "gb_qty_basis_id": str(listing_id),
		"gb_qty_basis_ingested": _ingested_units(item_code, {str(listing_id)}),
		"gb_qty_pushed": quantity}, update_modified=False)


def _model_listing_over(item_code: str, row: dict, listing: dict, *, unseen: int,
		baseline: bool) -> dict:
	"""The model's listing has ended on GunBroker and names no replacement.

	Two very different reasons look the same from here. Every unit sold (the orders
	are in, nothing is left to list): the listing is over, and the record says so
	without a call — a restock then relists it. Or a partial sale whose remainder
	GunBroker has relisted under a number it has not told us (the hourly sweep finds
	it by SKU): saying "ended" now would let a restock post a SECOND live listing.
	Only the orders can tell them apart, which is what ``unseen`` is: units the
	listing lost that no order explains. No baseline, no way to tell, so wait.
	"""
	if unseen or not baseline:
		return skip(listings.REPRICE_UNCONFIRMED, _(
			"The GunBroker listing for item {0} has ended and its sales are not all "
			"in yet. The quantity was not sent; the sweep will look for what replaced it."
		).format(item_code), verified=False)
	row = item_state(item_code)
	confirm_item_ended(item_code, str(row.get("gb_item_id") or ""), row, delisted=False)
	frappe.enqueue(
		"ffl_integrations.gunbroker.tasks.gb_relist_item_task",
		queue="short", enqueue_after_commit=True,
		job_id=f"gb-relist:{item_code}", deduplicate=True, item_code=item_code)
	return {"ok": True, "ended": True, "quantity": 0}


def read_model_listing(client: GunBrokerClient, row: dict, *,
		deadline: float | None = None) -> dict:
	"""Find the live listing a model's quantity baseline leads to, and its count.

	Walks from the listing the baseline was written to (else the one held) along
	``relistedAsItemID`` — one hop per partial sale or thirty-day relist — until a
	listing that is still active.

	Args:
	    client: A GunBroker client.
	    row: The model's :func:`item_state`.
	    deadline: A ``time.monotonic()`` instant after which no further call is made
	        (the counter's budget).

	Returns:
	    ``{"state": "live", "live_id", "remote", "chain": [ids walked]}``;
	    ``{"state": "ended", "chain", "remote": 0}`` when the walk ends on an
	    inactive listing that names no replacement (or that GunBroker no longer
	    serves); or ``{"state": "unreadable", "chain"}`` when a call failed, the
	    answer made no sense, or the chain ran past :data:`MODEL_CHAIN_LIMIT`. The
	    caller acts on the first two and sends nothing on the third.
	"""
	chain: list[str] = []
	current = str(row.get("gb_qty_basis_id") or row.get("gb_item_id") or "")
	while current and len(chain) < MODEL_CHAIN_LIMIT:
		if deadline is not None and time.monotonic() >= deadline:
			break
		chain.append(current)
		try:
			probe = client.item_probe(current)
		except Exception:  # noqa: BLE001 — a read behind a stock movement
			break
		if probe["state"] == "error":
			break
		if probe["state"] == "gone":
			return {"state": "ended", "chain": chain, "remote": 0}
		item = probe.get("item") or {}
		if get_field(item, "isActive", "IsActive") is not False:
			try:
				remote = int(flt(get_field(item, "quantity", "Quantity")))
			except (TypeError, ValueError):
				break
			return {"state": "live", "live_id": current, "remote": remote, "chain": chain}
		successor = listings._numeric_id(
			get_field(item, "relistedAsItemID", "RelistedAsItemID"))
		if not successor or successor in chain:
			return {"state": "ended", "chain": chain, "remote": 0}
		current = successor
	return {"state": "unreadable", "chain": chain, "remote": 0}


def _local_chain_ids(row: dict) -> set[str]:
	"""The listing numbers the record itself knows lead from the baseline to now:
	the baseline's, every one the history names after it, and the one held."""
	basis_id = str(row.get("gb_qty_basis_id") or "")
	ids = {basis_id, str(row.get("gb_item_id") or "")} - {""}
	history = [i.strip() for i in str(row.get("gb_item_history") or "").split(",") if i.strip()]
	if basis_id in history:
		ids |= set(history[history.index(basis_id):])
	return ids


def _ingested_units(item_code: str, listing_ids) -> int:
	"""Units GunBroker orders for this model have brought in on these listings,
	whatever the order's status.

	Raw SQL: the Woo tests mock ``get_all`` wholesale, and this is read from inside
	a stock rule they reach.
	"""
	ids = tuple(sorted({str(i) for i in listing_ids if i}))
	if not ids:
		return 0
	rows = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(l.qty), 0) FROM `tabGunBroker Order Line` l
		WHERE l.parenttype = 'GunBroker Order' AND l.parentfield = 'lines'
		  AND l.sku = %(sku)s AND l.gb_item_id IN %(ids)s
		""", {"sku": mapping.item_listing_sku(item_code), "ids": ids})
	return int(rows[0][0] or 0) if rows else 0


def unseen_units(item_code: str, row: dict, remote: int, chain=()) -> int:
	"""Units GunBroker has sold from this model that no order has brought in yet:
	``max(0, basis - ingested - remote)``. 0 when no baseline was ever recorded
	(a listing adopted rather than posted) — there is nothing to measure against.

	Args:
	    item_code: The model.
	    row: Its :func:`item_state`.
	    remote: The live listing's count.
	    chain: The listing numbers a read walked (joined with what the record knows).
	"""
	if not row.get("gb_qty_basis_id"):
		return 0
	# Only orders that came in AFTER the baseline was written: whatever was already
	# ingested on that listing then is inside ``basis`` already. Under GunBroker's
	# end-and-relist behaviour that is always 0 (a listing with a sale is dead);
	# if it ever decrements in place instead, counting the older orders again would
	# make ``unseen`` too small and put a sold unit back on the listing.
	ingested = _ingested_units(item_code, set(chain) | _local_chain_ids(row)) \
		- cint(row.get("gb_qty_basis_ingested"))
	return max(0, cint(row.get("gb_qty_basis")) - max(0, ingested) - remote)


def expected_model_quantity(item_code: str, row: dict, remote: int, target: int) -> int:
	"""What the live listing SHOULD hold given the pool's ``target`` — the same
	formula as :func:`sync_model_quantity`, from the record alone (no GunBroker
	call), for the hourly drift check. ``remote`` is the scan's count."""
	return max(0, target - unseen_units(item_code, row, remote))


def _nudge_order_sync(*, after_commit: bool = True) -> None:
	"""Bring GunBroker's sales in now rather than at the next poll.

	``after_commit=False`` for a caller about to REFUSE something (the counter's
	gate raises, which rolls its request back and would drop a job waiting for the
	commit): the job reads and writes nothing of that request's.
	"""
	frappe.enqueue("ffl_integrations.gunbroker.orders.request_order_sync",
		queue="short", enqueue_after_commit=after_commit,
		job_id="gb-order-nudge", deduplicate=True)


def _listing_moved_on(client: GunBrokerClient, item_id: str) -> str | None:
	"""Is the listing we just wrote to over, and did GunBroker say what replaced it?

	Three answers, and the third is why this is not a bool. ``None`` means the
	listing is live OR GunBroker did not say — a failed read is not evidence that
	a write missed, and refusing to record a count over a timeout would leave the
	next movement thinking nothing had been sent. ``""`` means over with nothing
	named, which is NOT the same as "nothing was relisted": that reading is what
	the sweep's SKU scan is for. A number means over, and that is the replacement
	— the only one of the three that has been observed directly.
	"""
	try:
		probe = client.item_probe(item_id)
	except Exception:  # noqa: BLE001 — this runs behind a stock movement
		return None
	if probe["state"] == "error":
		return None
	if probe["state"] == "gone":
		return ""
	item = probe.get("item") or {}
	if get_field(item, "isActive", "IsActive") is not False:
		return None
	return listings._numeric_id(
		get_field(item, "relistedAsItemID", "RelistedAsItemID"))


def relist_on_restock(item_code: str, *, client: GunBrokerClient | None = None) -> dict:
	"""Put a sold-out item back on GunBroker when stock arrives.

	Only for something that has been listed before (`gb_item_history` carries
	its old item numbers) and was NOT taken down by hand (`gb_delisted` is 0).
	Both halves matter: the first is what keeps this from listing every item in
	the catalogue the first time it is received, and the second is the rule that
	a machine does not undo a person's decision.

	Consumables are the reason this exists at all — a case of ammunition sells
	out and is reordered continuously, and requiring somebody to notice and press
	a button every time is how a channel quietly stops carrying half the range.
	"""
	row = item_state(item_code)
	if row.get("gb_item_id") or row.get("gb_delisted") or row.get("gb_push_pending"):
		return {"ok": True, "skipped": "not_eligible"}
	if not (row.get("gb_item_history") or "").strip():
		return {"ok": True, "skipped": "never_listed"}
	try:
		available = listing_quantity(item_code, row)
	except Exception:  # noqa: BLE001 — a channel that cannot answer is not "plenty"
		safe_log_error(f"GunBroker relist quantity unreadable for {item_code}",
			frappe.get_traceback())
		return {"ok": True, "skipped": "quantity_unreadable"}
	if available <= 0:
		return {"ok": True, "skipped": "no_stock"}
	# A relist mints a NEW GunBroker item number and resets the listing's age
	# and its watchers. A fast-moving consumable can sell out and be restocked
	# several times in a day, and putting it back within minutes each time
	# churns through item numbers for no gain — the buyer who was watching has
	# lost the listing either way.
	last = row.get("gb_synced_at")
	if last and time_diff_in_seconds(now_datetime(), last) < RELIST_COOLDOWN_SECONDS:
		return {"ok": True, "skipped": "cooling_off"}
	# Never with the paid promotions (premium stays False): a machine does not spend
	# the operator's money. Only a person pressing Send buys them.
	return push_item(item_code, client=client)


# ---------------------------------------------------------------------------
# T3 — end
# ---------------------------------------------------------------------------

def end_item_listing(item_code: str, *, reason: str | None = None,
		client: GunBrokerClient | None = None, delisted: bool = True) -> dict:
	"""End this Item's GunBroker listing.

	Args:
	    item_code: The Item whose listing ends.
	    reason: What to record in the alert if it cannot be confirmed.
	    client: An existing client (optional).
	    delisted: Whether to set the "a person took this down" sentinel. True
	        for the button, FALSE when the caller is
	        :func:`sync_item_quantity` reacting to stock hitting zero — that is
	        not a person, and marking it as one would stop the item ever coming
	        back on its own.
	"""
	return listings.end_listing_for(
		ITEM_DOCTYPE, item_code, item_state(item_code),
		reason=reason, client=client,
		subject=_("item {0}").format(item_code),
		log_error=lambda message: log_sync_error(item_code, message),
		claim=lambda item_id, previous: claim_item_listing(
			item_code, item_id, previous=previous),
		# Read the row back rather than closing over one from before the call:
		# ending a listing GunBroker had already relisted moves the record onto
		# the replacement first, and confirming from a stale row would drop the
		# old number out of the history that claim had just written.
		confirm=lambda item_id: confirm_item_ended(
			item_code, item_id, item_state(item_code), delisted=delisted))


def claim_item_listing(item_code: str, item_id: str, *, previous: str = "") -> None:
	"""Point this Item at an item number GunBroker gave us.

	`gb_qty_pushed` is reset because it is a record of what THIS listing was
	told, and the replacement has been told nothing. Leaving the old count
	standing is what would let :func:`sync_item_quantity` decide a later movement
	needs no call — against a listing that never heard the first one.

	`update_modified` stays off for the same reason every other GunBroker write
	on an Item does: GunBroker renumbering a listing is not somebody editing the
	product, and bumping the timestamp would say it was.

	A model's quantity baseline (`gb_qty_basis` / `gb_qty_basis_id`) is NOT reset:
	it names the listing OUR last write landed on, and the replacement is exactly
	the successor its units moved to. Resetting it would forget the sales GunBroker
	took in between, which is the whole thing it exists to remember.
	"""
	listings.claim_item_id(
		ITEM_DOCTYPE, item_code, item_id, previous=previous,
		state=item_state(item_code),
		clear_extra={"gb_qty_pushed": 0}, update_modified=False)


def confirm_item_ended(item_code: str, item_id: str, row: dict, *,
		delisted: bool = True) -> None:
	"""Record that an item-level listing really is over.

	The item number moves into the history rather than being dropped: it is what
	tells a restock this model has been on GunBroker before, and it is the only
	way to trace an old listing afterwards.
	"""
	frappe.db.set_value(ITEM_DOCTYPE, item_code, {
		"gb_item_id": "",
		"gb_item_history": listings._push_history(row.get("gb_item_history"), item_id),
		"gb_delisted": 1 if delisted else 0,
		"gb_end_pending": 0,
		"gb_qty_pushed": 0,
		# A model's baseline belongs to the listing that just ended; the next
		# listing is a new epoch with its own.
		"gb_qty_basis": 0,
		"gb_qty_basis_id": "",
		"gb_qty_basis_ingested": 0,
		"gb_price_pushed": 0,
		"gb_synced_at": now_datetime(),
		"gb_sync_error": "",
	}, update_modified=False)


# ---------------------------------------------------------------------------
# T4 — status
# ---------------------------------------------------------------------------

def item_listing_status(item_code: str, *,
		client: GunBrokerClient | None = None) -> dict:
	"""Local state plus, when listed, what GunBroker currently says."""
	row = item_state(item_code)
	out = listings.listing_status_for(row, client=client)
	try:
		out["quantity_local"] = listing_quantity(item_code, row)
	except Exception:  # noqa: BLE001 — a status read must not fail on a silent channel
		out["quantity_local"] = None
	out["quantity_pushed"] = cint(row.get("gb_qty_pushed") or 0)
	return out


# ---------------------------------------------------------------------------
# Small shared helper
# ---------------------------------------------------------------------------

def log_sync_error(item_code: str, message: str) -> None:
	"""Record why the last push was refused, where the operator will see it.

	Written with `update_modified=False`: a sync failure is not an edit of the
	Item, and bumping its timestamp would make every failed push look like
	somebody changed the product.
	"""
	frappe.db.set_value(ITEM_DOCTYPE, item_code, "gb_sync_error",
		str(message or "")[:1000], update_modified=False)
