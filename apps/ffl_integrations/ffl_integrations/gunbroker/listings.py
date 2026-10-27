"""Listing lifecycle: post, reprice, end, and recover from a lost response.

The guard matrix at the top of :func:`push_serial` is the important part of
this module. Eighteen reasons a gun does not go on GunBroker, in a fixed order,
every one of them answered from local data before a client is built — so a
refusal costs no HTTP call and never reads a credential. The order is the
priority order: the first match is what the operator is told, and the reasons
are arranged so the most specific and most actionable answer wins.

Refusals come back as a dict, never as a `frappe.throw`. A throw rolls the
request back, which would erase the `gb_sync_error` the guard has just written
— the operator would see the reason once, and the form would forget it.

The other thing to understand here is why posting a listing is two
transactions. `POST /Items` is not idempotent and GunBroker enforces no
uniqueness of any kind — ten listings for the same SKU and serial number
coexisted happily in testing. So the dangerous failure is not "the post
failed", it is "the post succeeded and we never found out": a timeout on a
multipart upload of eight photos, or a 200 whose body we could not parse. If
the record of having tried were rolled back with the failure, the form would go
back to looking untouched, the next click would sail through every guard, and
GunBroker would end up with two live listings for one physical gun — which has
to be cleaned up by hand there.

So the attempt is committed BEFORE the request goes out (transaction A, under a
row lock held for milliseconds), and the outcome is written after (transaction
B, no lock, no HTTP inside the lock). Transaction B catches everything: on any
failure it either leaves the pending flag standing or clears it deliberately,
and it never re-raises.
"""
from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from ffl_core.custody import is_custody_serial
from ffl_core.firearm import effective_serial_price
from ffl_core.reservations import serial_shelf_state
from ffl_core.utils import commit
from ffl_integrations.gunbroker import mapping
from ffl_integrations.gunbroker.client import (
	DEV_KEY_CONF,
	GunBrokerClient,
	GunBrokerConfigError,
	GunBrokerError,
	dev_key,
	get_field,
	parse_item_id,
	sandbox_mode,
)
from ffl_integrations.notifications import notify_admins, safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"

# Skip reasons, in guard order. Names are part of the API — the form, the MCP
# tools and the tests all match on them.
DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"
NOT_ACTIVE = "not_active"
TRANSFER_CUSTODY = "transfer_custody"
CONSIGNOR_MISSING = "consignor_missing"
UNIFORM_ITEM = "uniform_item"
# A model (uniform-price gun) listing's own refusals.
PER_SERIAL_LIVE = "per_serial_live"
CATEGORY_AMBIGUOUS = "category_ambiguous"
DROPSHIP_PRICED = "dropship_priced"
DELISTED = "delisted"
UNPRICED = "unpriced"
BAD_LISTING_TERMS = "bad_listing_terms"
AUCTION_PRICE_LOCKED = "auction_price_locked"
AUCTION_HAS_BIDS = "auction_has_bids"
NOT_SELLABLE = "not_sellable"
NO_CATEGORY = "no_category"
NO_SHIP_COST = "no_ship_cost"
BANNED_TITLE = "banned_title"
NO_IMAGES = "no_images"
ALREADY_LISTED = "already_listed"
SOLD_UNKNOWN = "sold_unknown"
PUSH_IN_FLIGHT = "push_in_flight"
# Not guards — outcomes of a push that got past the guards.
AUTH_FAILED = "auth_failed"
PUSH_FAILED = "push_failed"
PUSH_REJECTED = "push_rejected"
DUPLICATE_LISTING = "duplicate_listing"
UNLISTED = "unlisted"
REPRICE_FAILED = "reprice_failed"
REPRICE_UNCONFIRMED = "reprice_unconfirmed"
PICTURES_FAILED = "pictures_failed"
# A quantity push that could not be worked out safely, so nothing was sent.
QUANTITY_UNREADABLE = "quantity_unreadable"

# How long a recorded attempt with no confirmed item number is left alone before
# the sweep is allowed to conclude nothing was created. Long enough to cover a
# slow multipart upload and GunBroker's own indexing delay; the cost of waiting
# is one gun that cannot be re-listed for an hour, and the cost of not waiting
# is a duplicate listing somebody has to go and delete.
PUSH_GRACE_HOURS = 1

_SERIAL_STATE_FIELDS = (
	"item_code", "status", "gb_item_id", "gb_item_history", "gb_delisted",
	"gb_end_pending", "gb_push_pending", "gb_push_attempted_at", "gb_price_dirty",
	"gb_sold_unknown", "gb_gone_strikes", "gb_gone_first_at", "gb_reserved",
	"gb_order", "gb_sync_error", "gb_listing_type", "gb_listing_title",
	"gb_bid_locked", "gb_bid_count",
)

# Most recent item numbers kept on the serial. A GunBroker relist mints a new
# one every 30 days, so this grows on its own; twenty is plenty to trace a
# listing's history and small enough to stay readable in a Small Text field.
ITEM_HISTORY_LIMIT = 20

# How many times ending a listing will follow GunBroker's own replacement for it
# before giving up and asking for a person. One hop is the real case (a sale that
# left stock behind, relisted while we were not looking); more than that means
# something is minting listings faster than we can end them, and the honest
# answer to that is an alert rather than a loop.
RELIST_CHASE_LIMIT = 3


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def skip(reason: str, message: str, **extra) -> dict:
	"""A guard refusal, in the shape every caller expects."""
	return {"ok": False, "skipped": reason, "message": message, **extra}


def _settings():
	return frappe.get_single(SETTINGS_DOCTYPE)


def serial_state(serial_no: str, *, for_update: bool = False) -> dict:
	"""The GunBroker-owned columns of one Serial No."""
	return frappe.db.get_value(
		"Serial No", serial_no, list(_SERIAL_STATE_FIELDS),
		as_dict=True, for_update=for_update) or frappe._dict()


def listing_state(row: dict) -> str:
	"""Which of the seven listing states this gun is in.

	Derived, never stored. A status column would be a second copy of facts the
	flags already carry, and the two would drift the first time a job died between
	writing one and the other.
	"""
	if row.get("gb_sold_unknown"):
		return "sold_unknown"
	if row.get("gb_reserved") and row.get("gb_order"):
		return "sold"
	if row.get("gb_end_pending"):
		return "end_pending"
	if row.get("gb_push_pending"):
		return "push_pending"
	# `gb_reserved` without a `gb_order` is the brief window inside the order
	# poll's own transaction. Reporting it as `listed` would say the gun is on
	# sale at the moment it stopped being.
	if row.get("gb_item_id") and not row.get("gb_reserved"):
		return "listed"
	if row.get("gb_reserved"):
		return "sold"
	if row.get("gb_delisted"):
		return "ended"
	return "unlisted"


def _push_history(existing: str | None, item_id: str) -> str:
	ids = [i.strip() for i in str(existing or "").split(",") if i.strip()]
	if item_id and (not ids or ids[-1] != str(item_id)):
		ids.append(str(item_id))
	return ",".join(ids[-ITEM_HISTORY_LIMIT:])


def _clear_gone_tracking() -> dict:
	"""The reset that must accompany every fresh item number.

	`gb_gone_strikes` / `gb_gone_first_at` record how long a listing has been
	un-findable, and the sweep declares a listing dead once it has been missing
	for two rounds AND at least a day. Both must be cleared whenever the gun gets
	a NEW listing — otherwise a stale `first_at` from weeks ago is already older
	than a day, and the very first transient hiccup against the new listing
	satisfies the age test on the spot and marks a live listing as ended.
	"""
	return {
		"gb_gone_strikes": 0,
		"gb_gone_first_at": None,
		# A fresh item number is a fresh auction: no bids yet, nothing promised.
		"gb_bid_locked": 0,
		"gb_bid_count": 0,
		"gb_current_bid": 0,
		"gb_auction_ends_at": None,
	}


def _log_sync_error(serial_no: str, message: str) -> None:
	frappe.db.set_value("Serial No", serial_no, "gb_sync_error", str(message)[:1000])


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _config_gap(settings) -> str:
	"""Which credential/setting is missing, in operator words, or ""."""
	missing = []
	if not (settings.username or "").strip():
		missing.append(_("seller username"))
	if not (settings.get_password("password", raise_exception=False) or ""):
		missing.append(_("seller password"))
	sandbox = sandbox_mode()
	if not dev_key(sandbox):
		# Not a form field: the key is the platform's and is set in site config.
		missing.append(_("{0} DevKey (site config {1})").format(
			_("sandbox") if sandbox else _("production"), DEV_KEY_CONF[sandbox]))
	if not (settings.postal_code or "").strip():
		missing.append(_("ship-from postal code"))
	if not mapping.payment_methods():
		# Not a credential, but the same kind of gap: a listing offering no way
		# to pay costs a listing fee and produces nothing, and cheques and money
		# orders are the only route this phase accepts.
		missing.append(_("at least one payment method"))
	return ", ".join(missing)


def check_push_guards(serial_no: str, *, relist: bool = False,
		settings=None, context: dict | None = None) -> dict | None:
	"""Every reason not to list this gun that can be answered locally.

	Guards 1 through 15 of the matrix, in order. Guard 16 (an unconfirmed
	previous attempt) is NOT here: it is the only one that needs a network call,
	so it is run by the caller, after this returns clean and after the row lock
	has been released.

	Returns:
	    A skip dict, or None when the gun may be listed.
	"""
	from ffl_integrations.integration_gate import gunbroker_enabled

	# 1. Switched off.
	if not gunbroker_enabled():
		return skip(DISABLED, _(
			"GunBroker is switched off. Turn it on in GunBroker Settings first."))

	settings = settings or _settings()

	# 2. Switched on but not usable. Name the missing pieces — "not configured"
	#    on its own sends somebody hunting through a form with thirty fields.
	gap = _config_gap(settings)
	if gap:
		return skip(NOT_CONFIGURED, _(
			"GunBroker is not configured yet — missing: {0}.").format(gap))

	ctx = context or mapping.listing_context(serial_no)
	serial, item = ctx["serial"], ctx["item"]
	if not serial.get("item_code"):
		return skip(NOT_ACTIVE, _("Serial {0} does not exist.").format(serial_no))

	# 3. Not in stock.
	if (serial.get("status") or "") != "Active":
		return skip(NOT_ACTIVE, _(
			"Serial {0} is {1}, not Active — only a gun on the shelf can be listed."
		).format(serial_no, serial.get("status") or _("unknown")))

	# 4. Not ours to sell. Checked early and unconditionally: a customer's
	#    transfer gun is excluded by a different KIND of reason than the rest,
	#    and no operator override can make it the store's property.
	if is_custody_serial(serial_no):
		return skip(TRANSFER_CUSTODY, _(
			"Serial {0} is a customer's firearm held for an FFL transfer, not store "
			"inventory. It cannot be listed for sale."
		).format(serial_no))

	firearm_type = str(mapping.serial_attr(serial, item, "firearm_type") or "").strip()

	# No antique or NFA guard here, on purpose. An antique lists with no FFL
	# required and retires through a stock issue instead of a disposition
	# (fulfillment.dispose_gb_order); an NFA item lists and disposes like any
	# other gun — the Form 3/4 is filed in ATF eForms, outside this system.

	# 5. Consigned IN with no consignor. A consigned gun otherwise lists like
	#    store stock: the GunBroker sale books a Sales Invoice with the serial on
	#    the line, which freezes the commission and posts the consignor payable
	#    like a counter sale, and consignment sales are never returnable. But
	#    that payable is skipped when no consignor is recorded, and nobody is at
	#    the counter to notice on an online sale — so the consignor must be set
	#    before the gun goes up.
	if _has_field("Serial No", "is_consignment"):
		consigned = frappe.db.get_value("Serial No", serial_no,
			["is_consignment", "consignor"], as_dict=True) or {}
		if consigned.get("is_consignment") and not consigned.get("consignor"):
			return skip(CONSIGNOR_MISSING, _(
				"Serial {0} came in on consignment but has no consignor recorded, so "
				"a GunBroker sale would owe nobody. Set the Consignor on the Serial "
				"No first."
			).format(serial_no))

	# 6. Uniform-priced model: sold as one shared product, not per gun. Lists as a
	#    MODEL from the Item form instead. `relist` does not lift this, unlike the
	#    delisted sentinel below: it would put one gun of the model on GunBroker
	#    under its own SKU, beside (or instead of) the model listing whose quantity
	#    that gun is already part of.
	if item.get("is_firearm") and item.get("uniform_sell_price"):
		return skip(UNIFORM_ITEM, _(
			"{0} is priced per model, not per gun, so it is not listed by serial "
			"number. List the model from its Item form."
		).format(serial.get("item_code")))

	state = serial_state(serial_no)

	# 7. Somebody ended this listing by hand. A machine never puts it back;
	#    a person does, by pressing the button again.
	if state.get("gb_delisted") and not relist:
		return skip(DELISTED, _(
			"This listing was ended by hand. Press Send to GunBroker again to list it "
			"afresh — GunBroker will issue a new item number."))

	# 8. No price. A listing at $0 is a real offer at $0. An auction is priced
	#    by its starting bid, which 8a checks with the rest of its terms.
	listing_type = str(serial.get("gb_listing_type") or mapping.LISTING_FIXED)
	if listing_type == mapping.LISTING_FIXED and mapping.fixed_listing_price(
			serial, effective_serial_price(serial_no)) <= 0:
		return skip(UNPRICED, _(
			"Serial {0} has no sell price — GunBroker would list it at $0. Price it "
			"first, then send it again."
		).format(serial_no))

	# 8a. Auction terms GunBroker would refuse, or that were never set. The
	#     dialog checks these as they are typed; this is the copy the push
	#     itself trusts, for the callers that are not the dialog.
	if listing_type == mapping.LISTING_AUCTION:
		try:
			mapping.auction_terms(serial, settings)
		except ValueError as exc:
			return skip(BAD_LISTING_TERMS, _(
				"The auction terms for serial {0} are not valid: {1}. Press Send to "
				"GunBroker and set them again."
			).format(serial_no, exc))

	# 9. Spoken for somewhere else. THE cross-channel check: web order, counter
	#     hold, consignment, or a GunBroker sale we already know about.
	shelf = serial_shelf_state(serial_no)
	if not shelf.get("sellable"):
		return skip(NOT_SELLABLE, _shelf_message(serial_no, shelf),
			reason_detail=shelf.get("reason"))

	# 10. No category. Never guessed — see mapping.resolve_category.
	is_antique = bool(item.get("is_antique"))
	category_id = mapping.serial_category(serial, item, settings)
	if category_id <= 0:
		if is_antique:
			return skip(NO_CATEGORY, _(
				"No GunBroker antique category is configured for firearm type {0!r}. "
				"An antique never takes the modern category: set a GunBroker Category "
				"ID on the Item, or add a row flagged Antique to the category map in "
				"GunBroker Settings."
			).format(firearm_type or _("(blank)")))
		return skip(NO_CATEGORY, _(
			"No GunBroker category is configured for firearm type {0!r}. Set a "
			"GunBroker Category ID on the Item, or add a row to the category map in "
			"GunBroker Settings."
		).format(firearm_type or _("(blank)")))

	# 11. No shipping cost. Zero would mean shipping firearms for free — unless
	#     somebody chose exactly that at Send.
	if not mapping.ships_free(serial) and mapping.listing_ship_cost(
			serial.get("gb_ship_cost"), mapping.ship_cost_for(
			firearm_type,
			long_gun_cost=settings.get("long_gun_ship_cost"),
			handgun_cost=settings.get("handgun_ship_cost"))) <= 0:
		return skip(NO_SHIP_COST, _(
			"This gun has no GunBroker shipping cost of its own and the long gun / "
			"handgun default is not set in GunBroker Settings, so the buyer would "
			"be charged $0 to ship a firearm."))

	# 12. Title policy. GunBroker's own rejection never says which words were the
	#     problem, so this one is checked here or not at all.
	title, _title_warnings = mapping.gb_title(
		serial.get("item_name") or item.get("item_name") or serial.get("item_code"))
	phrase = mapping.banned_title_phrase(title, settings.get("title_banned_phrases"))
	if phrase:
		return skip(BANNED_TITLE, _(
			"The title contains {0!r}, which GunBroker rejects under its listing "
			"policies. Rename the Item or the serial and try again."
		).format(phrase))

	# 13. No photos. Answered from the gallery rows alone — deliberately WITHOUT
	#     reading any of them. Resolving the bytes means disk reads and, for a
	#     gallery row holding an absolute URL, an outbound HTTP request; a gun
	#     about to be refused for being already listed should not pay for that,
	#     and a guard refusal is contracted to cost no network call. The bytes
	#     are read later, before the lock, and an all-unreadable gallery lands
	#     back on this same reason there.
	if not mapping.candidate_image_paths(serial_no, settings=settings, context=ctx):
		return skip(NO_IMAGES, _(
			"No usable photos for serial {0}. Add at least one before listing it "
			"(GunBroker takes JPG, PNG and GIF)."
		).format(serial_no))

	return None


def _shelf_message(serial_no: str, shelf: dict) -> str:
	"""Say WHO has the gun, not just that somebody does."""
	reason = shelf.get("reason")
	detail = shelf.get("detail") or {}
	if reason == "channel_reserved":
		return _("Serial {0} is reserved by {1} order {2} — release it there first."
			).format(serial_no, detail.get("channel") or "?", detail.get("ref") or "?")
	if reason == "counter_hold":
		return _("Serial {0} is on an open counter order ({1})."
			).format(serial_no, detail.get("ref") or "?")
	if reason == "consigned_out":
		return _("Serial {0} is out on consignment.").format(serial_no)
	if reason == "sold_unknown":
		return _("Serial {0} may already be sold on GunBroker.").format(serial_no)
	return _("Serial {0} is not available to sell right now ({1})."
		).format(serial_no, reason or "?")


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:  # noqa: BLE001 — a meta failure must not decide a guard
		return False


# ---------------------------------------------------------------------------
# T10 — recovery scan
# ---------------------------------------------------------------------------

def recover_item_id(serial_no: str, *, scan_sold: bool = False,
		client: GunBrokerClient | None = None) -> dict:
	"""Find the listing GunBroker may already hold for this gun.

	Matching is on SKU, not on item number, and that is the whole design. Item
	numbers are not stable: GunBroker issues a new one every time it relists —
	which for a fixed-price listing is every thirty days, and again after a sale
	— while the SKU it was posted under stays put. GunBroker's own guidance is to
	sync inventory on SKU for exactly this reason. Matching on a cached item
	number would fail, correctly-looking, every time a relist has happened.

	Args:
	    serial_no: The Serial No to look for.
	    scan_sold: Also scan recently-sold listings. The sweep sets this; a
	        button press does not. A listing bought moments ago has already left
	        the selling list, so a selling-only scan says "nothing there" about a
	        gun that has just been sold.
	    client: An existing client (optional).

	Returns:
	    ``{"state": ..., "item_id": str|None, "ids": [...], "sold": bool}`` where
	    state is one of:

	    ``claimed``   exactly one live listing carries this SKU.
	    ``duplicate`` two or more do. Nothing is written and a human is told —
	                  GunBroker permits duplicates, so this is a real situation
	                  and picking one automatically would hide it.
	    ``miss``      GunBroker was asked and had nothing.
	    ``unknown``   GunBroker answered with rows that carry neither a SKU nor a
	                  comparable item number, so the question was not actually
	                  answered. Distinct from ``miss`` on purpose: every caller
	                  treats ``miss`` as licence to clear local state, and doing
	                  that on the strength of an unreadable answer is how a live
	                  listing gets forgotten about.
	"""
	client = client or GunBrokerClient.from_settings()
	row = serial_state(serial_no)
	sku = mapping.listing_sku(row.get("item_code"), serial_no)
	local_item_id = str(row.get("gb_item_id") or "")
	page_size = frappe.utils.cint(_settings().get("page_size") or 0) or 300

	matches, usable = _scan_for_sku(
		client.iter_items_selling(page_size=page_size), sku, local_item_id)
	if usable is False:
		return {"state": "unknown", "item_id": None, "ids": [], "sold": False}

	if len(matches) == 1:
		return {"state": "claimed", "item_id": matches[0], "ids": matches, "sold": False}
	if len(matches) > 1:
		return {"state": "duplicate", "item_id": None, "ids": matches, "sold": False}

	if scan_sold:
		sold_matches, sold_usable = _scan_for_sku(
			client.iter_items_sold(page_size=page_size), sku, local_item_id)
		if sold_usable is False:
			return {"state": "unknown", "item_id": None, "ids": [], "sold": False}
		if sold_matches:
			return {"state": "miss", "item_id": None, "ids": sold_matches, "sold": True}

	return {"state": "miss", "item_id": None, "ids": [], "sold": False}


def _scan_for_sku(rows, sku: str, local_item_id: str) -> tuple[list[str], bool | None]:
	"""Walk a listing scan looking for one SKU.

	Returns ``(matched item ids, usable)`` where ``usable`` is False only when
	rows came back and NONE of them carried either a SKU or an item number —
	i.e. the scan cannot answer the question. ``None`` means there were no rows
	at all, which is a perfectly good "no".
	"""
	matches: list[str] = []
	seen = 0
	usable_rows = 0
	unusable_match = False
	for row in rows:
		seen += 1
		row_sku = get_field(row, "sku", "SKU")
		row_item = get_field(row, "itemID", "ItemID", "itemId")
		if row_sku is not None or row_item is not None:
			usable_rows += 1
		if not (row_sku and str(row_sku) == sku):
			continue
		item_id = _numeric_id(row_item) or _numeric_id(local_item_id)
		if not item_id:
			# The SKU matched but the row carries no item number we can use, and
			# there is nothing cached locally either — which is the norm in the
			# lost-response case this scan exists for. That is NOT a claim: it
			# says "something with this SKU is listed" without saying what, and
			# adopting it would write an empty item number over the pending flag
			# and hand the gun back to the next click as if it were unlisted.
			unusable_match = True
			continue
		matches.append(item_id)
	if unusable_match or (seen and not usable_rows):
		return [], False
	return matches, (True if seen else None)


def dup_listing_key(doctype: str, name: str) -> str:
	"""The 24-hour dedup key for "GunBroker has two live listings under one SKU".

	One function because two paths report that situation — the button's guard 16
	below, and the sweep's claim — and a key that differs between them turns one
	situation into two alerts a day. Namespaced by doctype so a duplicate on an
	Item is not swallowed as a repeat of one on a Serial No of the same name.
	"""
	return f"gb_dup_listing_{doctype}:{name}"


def _numeric_id(value: Any) -> str:
	"""A GunBroker item number as a string, or "" if it is not one.

	Item numbers are positive integers. Anything else — None, "", 0, a dict, a
	stray label — is not an identifier, and the one thing that must never happen
	is treating it as one: it is written straight into `gb_item_id`, and an empty
	or bogus value there reads downstream as "this gun is not listed".
	"""
	text = str(value or "").strip()
	return text if text.isdigit() and int(text) > 0 else ""


def claim_item_id(doctype: str, name: str, item_id: str, *, previous: str = "",
		sku: str = "", state: dict | None = None, clear_extra: dict | None = None,
		update_modified: bool = True) -> None:
	"""Adopt an item number GunBroker gave us, recording the hand-over.

	The only place a scanned or relisted item number is written back, so the
	check that it IS one lives here rather than being left to each caller.
	Writing an empty `gb_item_id` while clearing the pending flag would turn an
	unresolved lost-response into an apparently unlisted record, and the next
	click would post a second listing.

	Shared by both listing kinds, so ``clear_extra`` carries the fields only one
	of them has — the per-gun gone-tracking counters, the item-level pushed count
	— exactly as :func:`post_listing` does. ``state`` is the record's current
	columns when the caller already has them, which is only about saving a read:
	the two fields wanted here are the two every caller has just looked at.
	"""
	item_id = _numeric_id(item_id)
	if not item_id:
		raise ValueError(
			f"refusing to claim a non-numeric GunBroker item id for {name!r}")
	row = state if state is not None else (frappe.db.get_value(
		doctype, name, ["gb_item_id", "gb_item_history"], as_dict=True) or {})
	old = previous or str(row.get("gb_item_id") or "")
	updates: dict[str, Any] = {
		"gb_item_id": str(item_id),
		"gb_push_pending": 0,
		"gb_push_attempted_at": None,
		"gb_sync_error": "",
		"gb_synced_at": now_datetime(),
		**(clear_extra or {}),
	}
	if old and old != str(item_id):
		updates["gb_item_history"] = _push_history(row.get("gb_item_history"), old)
		frappe.logger("gunbroker").info(
			f"gb itemID changed by GB relist: {old} -> {item_id} "
			f"(sku {sku or name})")
	frappe.db.set_value(doctype, name, updates, update_modified=update_modified)


def _claim_item_id(serial_no: str, item_id: str, *, previous: str = "") -> None:
	"""The per-gun claim: :func:`claim_item_id` plus the gone-tracking reset."""
	row = serial_state(serial_no)
	claim_item_id(
		"Serial No", serial_no, item_id, previous=previous, state=row,
		sku=mapping.listing_sku(row.get("item_code"), serial_no),
		clear_extra=_clear_gone_tracking())


# ---------------------------------------------------------------------------
# T1 / T8 — post a listing
# ---------------------------------------------------------------------------

SHIP_COST_DOCTYPES = ("Serial No", "Item")


def set_ship_cost(doctype: str, name: str, value: Any) -> float:
	"""Save a listing's own buyer-paid shipping rate. 0 clears it: the default applies.

	The field is ``non_negative`` on the form; this is the same rule for the
	API path, where nothing else stands between a typed figure and a listing.
	"""
	if doctype not in SHIP_COST_DOCTYPES:
		frappe.throw(_("A GunBroker shipping cost belongs on a Serial No or an Item, not {0}.")
			.format(doctype))
	rate = float(frappe.utils.flt(value or 0))
	if rate < 0:
		frappe.throw(_("A shipping cost cannot be negative."))
	frappe.db.set_value(doctype, name, "gb_ship_cost", rate, update_modified=False)
	return rate


def set_free_shipping(serial_no: str, value: Any) -> bool:
	"""Mark a gun's listing free shipping (seller pays), or take that off.

	A flag of its own rather than a 0 rate: 0 already means "follow the
	default", and a figure typed by mistake must never become a free listing.
	"""
	free = bool(frappe.utils.cint(value))
	frappe.db.set_value("Serial No", serial_no, "gb_free_shipping", int(free),
		update_modified=False)
	return free


def ship_cost_summary(doctype: str, name: str) -> dict:
	"""What a listing of this record would charge for shipping, and why.

	``own`` is the rate saved on the record (0 for none), ``default`` the store
	rate for its kind, ``effective`` the one a listing sent now would carry.
	The Send button shows these so the person pressing it decides with the
	default in front of them.
	"""
	if doctype not in SHIP_COST_DOCTYPES:
		frappe.throw(_("A GunBroker shipping cost belongs on a Serial No or an Item, not {0}.")
			.format(doctype))
	settings = _settings()
	free = False  # free shipping is a gun-level choice only
	if doctype == "Item":
		item = frappe.db.get_value(
			"Item", name, ["gb_ship_cost", "is_firearm", "firearm_type"], as_dict=True) or {}
		own = item.get("gb_ship_cost")
		# A model (a gun) ships at the long gun / handgun rate, everything else at
		# the accessory one.
		default = mapping.model_ship_cost({"firearm_type": item.get("firearm_type")}, settings) \
			if item.get("is_firearm") else settings.get("accessory_ship_cost")
	else:
		ctx = mapping.listing_context(name)
		own = ctx["serial"].get("gb_ship_cost")
		free = mapping.ships_free(ctx["serial"])
		default = mapping.ship_cost_for(
			str(mapping.serial_attr(ctx["serial"], ctx["item"], "firearm_type") or ""),
			long_gun_cost=settings.get("long_gun_ship_cost"),
			handgun_cost=settings.get("handgun_ship_cost"))
	own = float(frappe.utils.flt(own or 0))
	default = float(frappe.utils.flt(default or 0))
	return {"own": own, "default": default, "free": free,
		"effective": 0.0 if free else mapping.listing_ship_cost(own, default)}


CATEGORY_CHOICES_TTL = 24 * 3600
# How long a failed read is remembered, so an outage costs one timeout and one
# Error Log per ten minutes rather than one per dialog opened.
CATEGORY_CHOICES_RETRY = 10 * 60


def category_choices(category_id: int) -> list[dict]:
	"""The GunBroker leaf categories next to ``category_id``: the ones a gun
	filed under it can be moved to at Send ("Other Rifles" → "Semi Auto
	Rifles", "Bolt Action Rifles", …).

	Read from GunBroker (the category, for its parent; the parent, for its
	children) and cached for a day. When GunBroker cannot be reached the list
	is just the category itself, so the dialog still opens.
	"""
	key = (f"gunbroker:category_choices:{'sandbox' if sandbox_mode() else 'prod'}"
		f":{category_id}")
	cache = frappe.cache()
	cached = cache.get_value(key)
	if cached:
		return cached
	own = [{"id": category_id, "name": ""}]
	try:
		client = GunBrokerClient.from_settings_fast(timeout=(3, 8))
		node = client.get_category(category_id)
		path = []
		step = get_field(node, "categoryPath")
		while step:
			path.append(step)
			step = get_field(step, "child")
		own[0]["name"] = str(get_field(node, "categoryName") or "")
		if len(path) < 2:  # no such category, or a top level: nothing beside it
			cache.set_value(key, own, expires_in_sec=CATEGORY_CHOICES_RETRY)
			return own
		parent = frappe.utils.cint(get_field(path[-2], "id"))
		children = get_field(client.get_categories(parent), "results") or []
	except Exception as exc:  # noqa: BLE001 — a picker never blocks a listing
		safe_log_error("GunBroker category list failed", f"{category_id}: {exc}")
		cache.set_value(key, own, expires_in_sec=CATEGORY_CHOICES_RETRY)
		return own
	choices = [
		{"id": frappe.utils.cint(get_field(c, "categoryID")),
			"name": str(get_field(c, "categoryName") or "")}
		for c in children if get_field(c, "canContainItems")]
	if not any(c["id"] == category_id for c in choices):
		choices.insert(0, own[0])
	cache.set_value(key, choices, expires_in_sec=CATEGORY_CHOICES_TTL)
	return choices


def category_offer(serial: dict, item: dict, settings) -> dict:
	"""The category a gun would list under now, and the ones it may move to.

	No choice for an antique, or for any gun of a type the map routes to
	Curios & Relics by age. The choice is saved on the Item and outranks the
	per-gun C&R rule, so picking for a 1985 gun would take the 1943 gun of the
	same model out of C&R, and picking for the 1943 gun would put the 1985
	one in. Antique and C&R also sit next to each other on GunBroker, and an
	antique filed as C&R with no FFL required is the listing GunBroker's
	policy team looks for. A modern gun is never offered an antique or C&R
	category, mapped as one or (antique) named as one.
	"""
	current = mapping.serial_category(serial, item, settings)
	if current <= 0:
		return {"id": 0, "options": []}
	rows = settings.get("category_map") or []
	names = mapping.category_map_names(rows)
	firearm_type = str(mapping.serial_attr(serial, item, "firearm_type") or "")
	if item.get("is_antique") or mapping.routes_by_age(rows, firearm_type):
		options = [{"id": current, "name": ""}]
	else:
		held = (mapping.category_map_ids(rows, "is_antique")
			| mapping.category_map_ids(rows, "is_cr_eligible"))
		options = [c for c in category_choices(current) if c["id"] == current
			or (c["id"] not in held and "antique" not in c["name"].lower())]
	return {"id": current, "options": [
		{"id": c["id"], "name": c["name"] or names.get(c["id"], "")} for c in options]}


def set_listing_category(serial_no: str, category_id: Any) -> dict | None:
	"""Save the category chosen at Send on the gun's Item, so every gun of
	that model lists there from now on. Only a category the dialog offered is
	accepted.

	Returns what :func:`settle_listing_category` needs once the push has
	answered, or None when nothing changed.
	"""
	chosen = frappe.utils.cint(category_id)
	ctx = mapping.listing_context(serial_no)
	serial, item = ctx["serial"], ctx["item"]
	offer = category_offer(serial, item, _settings())
	if chosen == offer["id"]:
		return None
	if chosen not in {c["id"] for c in offer["options"]}:
		frappe.throw(_("GunBroker category {0} is not one this gun can be listed under.")
			.format(chosen), title=_("GunBroker category"))
	item_code = serial.get("item_code")
	frappe.db.set_value("Item", item_code, "gb_category_id", chosen, update_modified=False)
	return {"serial_no": serial_no, "item_code": item_code, "chosen": chosen,
		"previous": frappe.utils.cint(item.get("gb_category_id"))}


def settle_listing_category(moved: dict, listed: bool) -> None:
	"""Keep the model's new category only when the gun actually listed.

	A refused push leaves the Item as it was, so a Send that did nothing
	changes nothing. A kept one is noted on the Item: it moves every gun of
	the model, and the field write alone leaves no trace of who or why.
	"""
	if not listed:
		frappe.db.set_value("Item", moved["item_code"], "gb_category_id", moved["previous"],
			update_modified=False)
		return
	frappe.get_doc("Item", moved["item_code"]).add_comment("Info", _(
		"GunBroker category set to {0} from Send to GunBroker on serial {1}."
	).format(moved["chosen"], moved["serial_no"]))


def set_listing_spec(serial_no: str, spec: dict | None) -> dict:
	"""Record what the Send dialog chose, on the gun, for the push to read.

	The type and the auction figures are written every time — they describe
	the listing about to be made. The title and the fixed price are written
	only when the caller sent them: an empty title or a zero price CLEARS the
	override, so the listing follows the item name and the POS price again.
	The paid promotions are written (all four, so unticking clears) only when
	the spec carries ``premium``; without it the recorded ones stand. So is the
	condition: written only when the spec carries ``condition``, where 0 or
	empty clears the override and the gun follows its Item again.
	"""
	try:
		terms = mapping.validate_listing_spec(spec)
	except ValueError as exc:
		frappe.throw(_("Serial {0} cannot be listed: {1}.").format(serial_no, exc),
			title=_("Listing terms"))
	updates: dict[str, Any] = {
		"gb_listing_type": terms["type"],
		"gb_starting_bid": terms["starting_bid"],
		"gb_reserve_price": terms["reserve_price"],
		"gb_buy_now_price": terms["buy_now_price"],
		"gb_auction_duration": terms["duration"],
		"gb_auction_auto_relist": terms["auto_relist"],
	}
	raw = spec or {}
	if "title" in raw:
		updates["gb_listing_title"] = terms["title"]
	if "price" in raw:
		updates["gb_list_price"] = terms["price"]
	if "condition" in raw:
		updates["gb_condition"] = mapping.CONDITION_LABELS.get(terms["condition"], "")
	if raw.get("premium") is not None:
		for key, (field, _gb_key) in mapping.PREMIUM_FEATURES.items():
			updates[field] = int(terms["premium"][key])
	frappe.db.set_value("Serial No", serial_no, updates, update_modified=False)
	return terms


def listing_offer(serial_no: str) -> dict:
	"""Everything the Send dialog shows before somebody decides.

	The figures a listing would carry if nothing were changed — title, price,
	terms, shipping — each with where it came from, plus the choices GunBroker
	allows, so the dialog offers exactly what the push will accept.
	"""
	settings = _settings()
	ctx = mapping.listing_context(serial_no)
	serial, item = ctx["serial"], (ctx["item"] or {})
	derived_title, _warnings = mapping.gb_title(
		serial.get("item_name") or item.get("item_name") or serial.get("item_code"))
	pos_price = float(frappe.utils.flt(effective_serial_price(serial_no)))
	own_price = float(frappe.utils.flt(serial.get("gb_list_price") or 0))
	flt, cint = frappe.utils.flt, frappe.utils.cint
	is_antique = bool(item.get("is_antique"))
	derived_condition = mapping.gb_condition(item.get("is_used"), is_antique)
	return {
		"title": serial.get("gb_listing_title") or derived_title,
		"derived_title": derived_title,
		"price": own_price or pos_price,
		"pos_price": pos_price,
		"type": (serial.get("gb_listing_type") or settings.get("default_listing_type")
			or mapping.LISTING_FIXED),
		"types": list(mapping.LISTING_TYPES),
		"starting_bid": float(flt(serial.get("gb_starting_bid") or 0)) or pos_price,
		"reserve_price": float(flt(serial.get("gb_reserve_price") or 0)),
		"buy_now_price": float(flt(serial.get("gb_buy_now_price") or 0)),
		"duration": cint(serial.get("gb_auction_duration"))
			or cint(settings.get("auction_duration") or 7),
		"durations": list(mapping.AUCTION_DURATIONS),
		"auto_relist": cint(serial.get("gb_auction_auto_relist")) or mapping._enum_code(
			settings.get("auction_auto_relist"), default=mapping.AUCTION_RELIST_UNTIL_SOLD),
		"auto_relist_options": [
			{"value": mapping.AUCTION_RELIST_UNTIL_SOLD, "label": _("Relist until sold")},
			{"value": mapping.AUCTION_NO_RELIST, "label": _("Do not relist")},
		],
		"shipping": ship_cost_summary("Serial No", serial_no),
		"category": category_offer(serial, item, settings),
		"premium": mapping.premium_flags(serial),
		"condition": mapping._enum_code(serial.get("gb_condition"), default=0),
		"derived_condition": derived_condition,
		"condition_options": [derived_condition] if is_antique else list(mapping.CONDITION_CODES),
	}


def listing_price(serial_no: str) -> float:
	"""The fixed price a live listing should show right now."""
	row = frappe.db.get_value("Serial No", serial_no, ["gb_list_price"], as_dict=True) or {}
	return mapping.fixed_listing_price(row, effective_serial_price(serial_no))


def listing_title(serial_no: str) -> str:
	"""The title typed for this gun's listing, or "" when it follows the item name."""
	raw = frappe.db.get_value("Serial No", serial_no, "gb_listing_title") or ""
	return mapping.gb_title(raw)[0] if raw else ""


def push_serial(serial_no: str, *, relist: bool = False,
		client: GunBrokerClient | None = None) -> dict:
	"""Publish one gun on GunBroker.

	Args:
	    serial_no: The Serial No to list.
	    relist: The operator explicitly asked to list a gun that had been ended
	        by hand. Lifts the `delisted` sentinel and NOTHING else. It cannot
	        lift custody, price or cross-channel guards — those answer "is this
	        the store's gun to sell", not "should a machine republish it" — and
	        it cannot lift `already_listed`, because a gun that is still listed
	        has nothing to re-list.
	    client: An existing client (optional).

	Returns:
	    ``{"ok": True, "gb_item_id": ..., "gb_url": ..., "warnings": [...]}`` on success, or a
	    skip dict. Never raises for an expected failure.
	"""
	settings = _settings()
	context = mapping.listing_context(serial_no)

	refusal = check_push_guards(
		serial_no, relist=relist, settings=settings, context=context)
	if refusal:
		_log_sync_error(serial_no, refusal["message"])
		return refusal

	built = mapping.build_listing_payload(serial_no, settings=settings, context=context)
	warnings: list[str] = list(built["warnings"])
	payload = built["payload"]

	state = serial_state(serial_no)

	# 14. Already listed. A pure local read, and ahead of the scan below so the
	#     common case never spends an HTTP call.
	#
	#     `relist` does NOT lift this, and that is the whole point of the guard.
	#     Re-listing means putting back a gun somebody took down — its legal
	#     starting point is an ENDED or SOLD gun, whose `gb_item_id` is already
	#     empty, so a re-list never needs to bypass this. What lifting it would
	#     buy is the one outcome the two-phase transaction and the SKU recovery
	#     scan exist to prevent: a second live listing for a gun that exists
	#     once, on a marketplace that enforces no uniqueness and gives a seller
	#     no way to cancel a sale. The button cannot reach this (it only shows
	#     when `gb_item_id` is empty) but `relist` is a public parameter of the
	#     whitelisted method, which the MCP tool and the import skill both call.
	if state.get("gb_item_id"):
		return skip(ALREADY_LISTED, _(
			"Serial {0} is already listed on GunBroker (item {1}). Use Reprice to "
			"change the price, or End the listing first if you want to re-list it."
		).format(serial_no, state.get("gb_item_id")), gb_item_id=state.get("gb_item_id"))

	# 15. Possibly already sold there. Also local, and fail-closed: this flag is
	#     set when GunBroker stopped serving the listing while the sold list
	#     showed the same SKU.
	if state.get("gb_sold_unknown"):
		return skip(SOLD_UNKNOWN, _(
			"GunBroker cannot find this listing but shows the same SKU as sold — it "
			"may already have been bought. Check on GunBroker, and if it was not "
			"sold, use Clear \"Sold Unknown\" first."))

	# --- transaction 0: everything that can fail BEFORE anything leaves this
	#     machine. Bad credentials, an unbuildable client and unreadable photos
	#     all mean GunBroker certainly has no listing — so each returns its own
	#     reason and NONE of them touches the pending flag. Recording an attempt
	#     for a request that was never sent locks the gun out for an hour and
	#     shows the operator "the last attempt is unconfirmed" instead of "your
	#     password is wrong".
	pictures = mapping.picture_parts(serial_no, settings=settings, context=context)
	warnings.extend(pictures["warnings"])
	if not pictures["parts"]:
		# The guard passed on paths, so every one of them failed to read. Same
		# reason, because it is the same problem to the operator: this gun has no
		# photos that can be published.
		refusal = skip(NO_IMAGES, _(
			"None of the photos for serial {0} could be read, so nothing was sent."
		).format(serial_no))
		_log_sync_error(serial_no, refusal["message"])
		return refusal

	try:
		client = client or GunBrokerClient.from_settings()
		# Force the token now rather than on the first call inside transaction B:
		# a login failure discovered there would already have the pending flag
		# standing behind it.
		client.cached_token() or client.login()
	except GunBrokerConfigError as exc:
		refusal = skip(NOT_CONFIGURED, str(exc))
		_log_sync_error(serial_no, refusal["message"])
		return refusal
	except GunBrokerError as exc:
		refusal = skip(AUTH_FAILED, _(
			"GunBroker did not accept the stored credentials, so nothing was sent: "
			"{0}"
		).format(exc))
		_log_sync_error(serial_no, str(exc))
		return refusal

	# 16. An earlier attempt whose outcome was never confirmed. The only guard
	#     that costs a network call, and it runs with NO row lock held — the
	#     scan pages through up to fifty requests, and holding the Serial No
	#     lock across that would deadlock against the web store's own
	#     `for_update` on the same row.
	if state.get("gb_push_pending"):
		return _resolve_pending_push(serial_no, state, client=client)

	# --- transaction A: claim the attempt, then get out of the way -----------
	locked = serial_state(serial_no, for_update=True)
	if locked.get("gb_push_pending"):
		# Somebody else got here between the read above and this lock.
		commit()
		return skip(PUSH_IN_FLIGHT, _(
			"Another listing attempt for serial {0} is already in progress."
		).format(serial_no))
	if locked.get("gb_item_id"):
		# Re-checked under the lock, and `relist` does not lift it here either —
		# see guard 14. Somebody else may have listed this gun between the read
		# above and this lock.
		commit()
		return skip(ALREADY_LISTED, _(
			"Serial {0} is already listed on GunBroker (item {1})."
		).format(serial_no, locked.get("gb_item_id")), gb_item_id=locked.get("gb_item_id"))

	frappe.db.set_value("Serial No", serial_no, {
		"gb_push_pending": 1,
		"gb_push_attempted_at": now_datetime(),
		"gb_sync_error": "",
	})
	# A REAL commit, and the single most important line in this module: it is
	# what makes the record of the attempt outlive a failure of the request that
	# follows. (A no-op under the test runner, by house rule — the tests assert
	# the ordering instead.)
	commit()

	# --- transaction B: talk to GunBroker, then record what happened ---------
	return _post_listing(serial_no, payload, pictures["parts"],
		warnings=warnings, relist=relist, client=client, settings=settings)


def _resolve_pending_push(serial_no: str, state: dict, *,
		client: GunBrokerClient | None = None) -> dict:
	"""Guard 16: work out what became of an attempt we never got an answer to.

	A button press NEVER posts a second listing here, whatever the scan says.
	The worst case for waiting is a gun that cannot be re-listed for an hour
	until the sweep clears the flag; the worst case for guessing is two live
	listings for one gun, which someone has to go and delete on GunBroker — and
	which, if both sell, is a gun sold twice.
	"""
	try:
		client = client or GunBrokerClient.from_settings()
		found = recover_item_id(serial_no, scan_sold=False, client=client)
	except (GunBrokerError, GunBrokerConfigError) as exc:
		safe_log_error("GunBroker recovery scan failed", str(exc))
		return skip(PUSH_IN_FLIGHT, _(
			"An earlier listing attempt for serial {0} has not been confirmed yet, "
			"and GunBroker could not be reached to check. Try again shortly."
		).format(serial_no))

	if found["state"] == "claimed":
		_claim_item_id(serial_no, found["item_id"])
		# Not a skip: the gun genuinely IS listed, and the operator needs the item
		# number. The warning says the listing was adopted rather than created now.
		return {"ok": True, "gb_item_id": found["item_id"],
			"warnings": ["claimed_existing"]}

	if found["state"] == "duplicate":
		ids = ", ".join(found["ids"])
		message = _(
			"GunBroker has more than one live listing for serial {0} (items {1}). "
			"End the extra one there before listing again."
		).format(serial_no, ids)
		_log_sync_error(serial_no, f"duplicate listings on GunBroker: {ids}")
		notify_admins(
			_("Duplicate GunBroker listings for {0}").format(serial_no), message,
			dedup_key=dup_listing_key("Serial No", serial_no), window_minutes=1440)
		return skip(DUPLICATE_LISTING, message, ids=found["ids"])

	if found["state"] == "unknown":
		message = _(
			"GunBroker's listing scan came back without SKUs, so it cannot say "
			"whether serial {0} is listed. Nothing was changed."
		).format(serial_no)
		_log_sync_error(serial_no, "listing scan returned no comparable keys")
		notify_admins(
			_("GunBroker listing scan unusable"), message,
			dedup_key=f"gb_scan_unusable_{serial_no}", window_minutes=1440)
		return skip(PUSH_IN_FLIGHT, message)

	return skip(PUSH_IN_FLIGHT, _(
		"The result of the last listing attempt for serial {0} has not been "
		"confirmed yet. The system re-checks within the hour; if it turns out "
		"nothing was listed, send it again after that."
	).format(serial_no))


def _post_listing(serial_no: str, payload: dict, pictures: list, *,
		warnings: list[str], relist: bool, client: GunBrokerClient,
		settings) -> dict:
	"""Transaction B for one gun. See :func:`post_listing` for the reasoning."""
	return post_listing(
		"Serial No", serial_no, payload, pictures,
		warnings=warnings, relist=relist, client=client, settings=settings,
		subject=_("serial {0}").format(serial_no),
		log_error=lambda message: _log_sync_error(serial_no, message),
		clear_extra=_clear_gone_tracking())


def post_listing(doctype: str, name: str, payload: dict, pictures: list, *,
		warnings: list[str], relist: bool, client: GunBrokerClient, settings,
		subject: str, log_error, clear_extra: dict | None = None) -> dict:
	"""Post one listing, then record the outcome — including "we don't know".

	The three failure shapes get three different answers, and the difference
	matters because the pending flag is what stops a second listing:

	* GunBroker REFUSED it (a JSON 4xx — policy, validation): no listing exists,
	  so the flag is cleared and the operator can fix the cause and retry
	  immediately. Leaving it set would lock the gun out for an hour over a
	  typo.
	* No definite answer (timeout, 5xx, network, an unparseable body): a listing
	  may well exist. The flag STAYS, and the recovery scan sorts it out.
	* The listing was created but the extra photos failed: it exists and we know
	  its number. Record it, clear the flag, report the photos as a warning —
	  the gun is on GunBroker either way, and pretending otherwise is what
	  produces a duplicate.

	Shared by both listing kinds: a per-gun listing keyed on Serial No and an
	item-level one keyed on Item write the same ``gb_*`` fields and want the
	same three answers, so the difference is carried in ``subject`` (how the
	thing is named in a message), ``log_error`` (where the reason is recorded)
	and ``clear_extra`` (per-doctype fields to reset on success).
	"""
	head, tail = pictures[:mapping.PICTURES_PER_REQUEST], pictures[mapping.PICTURES_PER_REQUEST:]
	try:
		try:
			body = client.create_listing(payload, head)
		finally:
			# The post takes seconds and the row was read before it; a sibling job
			# (the Woo push a save enqueued, a quantity sync) may write it meanwhile,
			# and every outcome below is recorded by a write that, from the old
			# snapshot, is MariaDB 1020, which Frappe does not retry. Start a fresh
			# snapshot; the callers commit their attempt record before posting, so
			# nothing else is pending here.
			commit()
	except GunBrokerError as exc:
		if exc.terminal and exc.status_code and 400 <= exc.status_code < 500 and exc.json_body:
			frappe.db.set_value(doctype, name, {
				"gb_push_pending": 0,
				"gb_push_attempted_at": None,
				"gb_sync_error": str(exc)[:1000],
			})
			return skip(PUSH_REJECTED, _(
				"GunBroker refused this listing: {0}"
			).format(exc.body or str(exc)))
		# Anything else: we do NOT know whether a listing was created.
		log_error(str(exc))
		return skip(PUSH_FAILED, _(
			"GunBroker did not confirm the listing for {0}. It may or may not "
			"have been created — the system will check and, if nothing was listed, "
			"you can send it again."
		).format(subject))
	except Exception as exc:  # noqa: BLE001 — see the docstring: never re-raise
		safe_log_error("GunBroker listing push failed", frappe.get_traceback())
		log_error(str(exc))
		return skip(PUSH_FAILED, _(
			"The listing for {0} could not be confirmed: {1}"
		).format(subject, exc))

	item_id = parse_item_id(body if isinstance(body, dict) else {})
	if not item_id:
		# A 200 whose body we cannot read is the dangerous case: GunBroker almost
		# certainly created the listing. Keep the flag; the scan will find it.
		log_error(f"no itemID in response: {str(body)[:900]}")
		return skip(PUSH_FAILED, _(
			"GunBroker accepted the listing for {0} but did not return an item "
			"number. The system will find it and link it up shortly."
		).format(subject))

	updates: dict[str, Any] = {
		"gb_item_id": item_id,
		"gb_listed_at": now_datetime(),
		"gb_synced_at": now_datetime(),
		"gb_push_pending": 0,
		"gb_push_attempted_at": None,
		"gb_sync_error": "",
		"gb_end_pending": 0,
		**(clear_extra or {}),
	}
	# Guard 14 means the field should be empty here. Should — a listing created
	# in the window between that check and this write would otherwise be
	# overwritten leaving no trace of the number, which is the one thing that
	# makes a stray listing findable afterwards. Recording it costs a read.
	before = frappe.db.get_value(
		doctype, name, ["gb_item_id", "gb_item_history"], as_dict=True) or {}
	previous = str(before.get("gb_item_id") or "")
	if previous and previous != item_id:
		updates["gb_item_history"] = _push_history(
			before.get("gb_item_history"), previous)
		safe_log_error(
			"GunBroker: listing id replaced on push",
			f"{subject}: {previous} -> {item_id}")
	if relist:
		updates["gb_delisted"] = 0
	frappe.db.set_value(doctype, name, updates)
	# The number is the one thing a live listing cannot be found again without:
	# make it durable before the photo uploads below spend more seconds, after
	# which a write (a photo failure, the caller's own bookkeeping) could again
	# meet a row a sibling job changed and roll it back with the transaction.
	commit()

	# Photos beyond the eight that rode along with the post. A failure here is a
	# warning, never a rollback — the listing is live, and reporting it as failed
	# is what produces a duplicate on the next click.
	for batch_start in range(0, len(tail), mapping.PICTURES_PER_REQUEST):
		batch = tail[batch_start:batch_start + mapping.PICTURES_PER_REQUEST]
		try:
			client.add_pictures(item_id, batch)
		except Exception as exc:  # noqa: BLE001
			safe_log_error("GunBroker extra picture upload failed", str(exc))
			warnings.append("pictures_partial")
			# Recorded on the record too, not just in the return value: the
			# operator who has to add the missing photos on GunBroker may well be
			# looking at this form tomorrow, long after the alert scrolled past.
			log_error(f"listing {item_id} created; some photos did not upload: {exc}")
			break

	_bump_push_counter(settings)
	return {"ok": True, "gb_item_id": item_id, "gb_url": gb_item_url(item_id),
		"warnings": warnings}


def _bump_push_counter(settings) -> None:
	try:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, {
			"last_listing_push_at": now_datetime(),
			"last_listing_push_count": frappe.utils.cint(
				settings.get("last_listing_push_count") or 0) + 1,
		})
	except Exception:  # noqa: BLE001 — a status counter must never fail a push
		pass


# ---------------------------------------------------------------------------
# T2 — reprice
# ---------------------------------------------------------------------------

def update_price(serial_no: str, *, client: GunBrokerClient | None = None) -> dict:
	"""Push a gun's current sell price to GunBroker. See :func:`update_price_for`."""
	return update_price_for(
		"Serial No", serial_no, client=client,
		subject=_("serial {0}").format(serial_no),
		read_locked=lambda: serial_state(serial_no, for_update=True),
		read_price=lambda: listing_price(serial_no),
		read_title=lambda: listing_title(serial_no),
		log_error=lambda message: _log_sync_error(serial_no, message))


def replace_pictures(doctype: str, name: str, *, client: GunBrokerClient | None = None) -> dict:
	"""Swap a live listing's photos for the ones a new push would send (watermark included).

	The first batch of new photos (the primary among them) goes up before any old
	one comes off, so the listing is never left without photos; the old ones come
	off next, then the rest go up, so it never holds more than its old photos plus
	one batch. GunBroker renumbers a listing's pictures on every upload and
	delete, so the old ones are found again by file name each time, never by a
	picture id read earlier.

	GunBroker keeps the search-result thumbnail it took when the listing was
	created; only the listing's own gallery changes (sandbox, 10-11).

	Returns:
	    ``{"ok": True, "gb_item_id": ..., "uploaded": n, "removed": n, "warnings": [...]}``
	    or a skip dict.
	"""
	is_serial = doctype == "Serial No"
	fields = ["gb_item_id", "gb_bid_count", "gb_bid_locked"] if is_serial else ["gb_item_id"]
	row = frappe.db.get_value(doctype, name, fields, as_dict=True) or {}
	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		return skip(UNLISTED, _("{0} is not listed on GunBroker.").format(name))
	if row.get("gb_bid_count") or row.get("gb_bid_locked"):
		return skip(AUCTION_HAS_BIDS, _(
			"{0} is an auction with bids; its photos are left as the bidders saw them.").format(name))
	built = mapping.picture_parts(name) if is_serial else mapping.item_picture_parts(name)
	if not built["parts"]:
		return skip(NO_IMAGES, _("{0} has no photos to send.").format(name))
	try:
		client = client or GunBrokerClient.from_settings()
		old = {p.get("fileName") for p in (client.list_pictures(item_id).get("results") or [])}
		parts = built["parts"]
		batches = [parts[i:i + mapping.PICTURES_PER_REQUEST]
			for i in range(0, len(parts), mapping.PICTURES_PER_REQUEST)]
		client.add_pictures(item_id, batches[0])
		removed = 0
		for _attempt in range(len(old)):
			current = client.list_pictures(item_id).get("results") or []
			stale = next((p for p in current if p.get("fileName") in old), None)
			if not stale:
				break
			removed += bool(client.delete_picture(stale["pictureID"]))
		for batch in batches[1:]:
			client.add_pictures(item_id, batch)
	except (GunBrokerError, GunBrokerConfigError) as exc:
		return skip(PICTURES_FAILED, _("The photos of {0} could not be replaced: {1}").format(name, exc))
	return {"ok": True, "gb_item_id": item_id, "uploaded": len(parts), "removed": removed,
		"warnings": built["warnings"]}


def update_price_for(doctype: str, name: str, *, subject: str, read_locked,
		read_price, log_error, client: GunBrokerClient | None = None,
		read_title=None) -> dict:
	"""Push the current price to GunBroker and confirm it took.

	The price is re-read from the database HERE rather than being passed in, and
	that is deliberate: a price can be edited twice in quick succession, and a job
	carrying a value captured at trigger time would publish whichever edit
	happened to win the race. Reading it at the moment of sending means the last
	edit is the one that lands.

	A 200 from GunBroker is not accepted as proof. The item is read back and the
	price compared, because this API answers 200 to updates it silently discards
	— and an unverified reprice would clear the dirty flag while leaving the old
	price on a live listing.
	"""
	# --- snapshot under the lock, then get out of the way -------------------
	# The same discipline the push path follows, for the same reason and on the
	# SAME row: this lock contends with the web store's `_serial_availability`
	# for_update, and the two calls below each carry a 30-second timeout on top
	# of a three-attempt back-off ladder. Held across that, a checkout or a
	# counter reservation touching this gun waits on MariaDB until
	# innodb_lock_wait_timeout and comes back with a database error instead of a
	# refusal it can explain. This runs on every price edit once the doc_event is
	# wired up, so it is not a corner.
	row = read_locked()
	item_id = str(row.get("gb_item_id") or "")
	price = read_price()
	title = read_title() if read_title else ""
	commit()

	if not item_id:
		return skip(DELISTED if row.get("gb_delisted") else UNLISTED, _(
			"There is no live GunBroker listing for {0}.").format(subject))
	if str(row.get("gb_listing_type") or "") == mapping.LISTING_AUCTION:
		# The bidders set an auction's price. The dirty flag is left alone on
		# purpose: it is cleared when the gun is listed afresh, and until then
		# it records that the POS price and the listing disagree.
		return skip(AUCTION_PRICE_LOCKED, _(
			"{0} is on GunBroker as an auction, so its price is set by the "
			"bidders. To change the starting bid, end the auction and list it again."
		).format(subject))
	if price <= 0:
		return skip(UNPRICED, _(
			"No sell price is set for {0}, so there is nothing to send."
		).format(subject))

	# --- talk to GunBroker with no lock held --------------------------------
	# Title rides along when the gun has one of its own: GunBroker takes it in
	# the same PUT, and a title edited after listing would otherwise never go.
	changes: dict[str, Any] = {"FixedPrice": price}
	if title:
		changes["Title"] = title
	client = client or GunBrokerClient.from_settings()
	try:
		client.update_listing(item_id, changes)
		remote = client.get_item(item_id)
	except (GunBrokerError, GunBrokerConfigError) as exc:
		log_error(str(exc))
		return skip(REPRICE_FAILED, _(
			"Could not update the GunBroker price for {0}: {1}"
		).format(subject, exc))

	confirmed = frappe.utils.flt(get_field(remote, "fixedPrice", "FixedPrice") or 0)
	if abs(confirmed - price) > 0.005:
		# The dirty flag stays set on purpose, so the next sweep tries again.
		message = _(
			"GunBroker accepted the price change for {0} but still shows "
			"{1} instead of {2}."
		).format(subject, confirmed, price)
		log_error(message)
		return skip(REPRICE_UNCONFIRMED, message, verified=False)

	# --- re-take the lock to write back -------------------------------------
	# The listing may have been ended, or GunBroker may have relisted it under a
	# new number, while the calls above were in flight. Clearing the dirty flag
	# then would record "GunBroker has the current price" about a listing that no
	# longer exists — and the price we confirmed would never be sent to the one
	# that replaced it.
	current = read_locked()
	if str(current.get("gb_item_id") or "") != item_id:
		commit()
		message = _(
			"The GunBroker listing for {0} changed while the price was "
			"being sent (was {1}, now {2}). The new price will be sent again on "
			"the next sweep."
		).format(subject, item_id, current.get("gb_item_id") or _("none"))
		log_error(message)
		return skip(REPRICE_UNCONFIRMED, message, verified=False)

	frappe.db.set_value(doctype, name, {
		"gb_price_dirty": 0,
		"gb_synced_at": now_datetime(),
		"gb_sync_error": "",
	})
	commit()
	return {"ok": True, "fixed_price": price, "verified": True}


# ---------------------------------------------------------------------------
# T3 / T4 — end a listing
# ---------------------------------------------------------------------------

def end_listing(serial_no: str, *, reason: str | None = None,
		client: GunBrokerClient | None = None) -> dict:
	"""End this gun's GunBroker listing. See :func:`end_listing_for`.

	Refused outright for an auction that has bids. Ending one from here would
	mean either cancelling somebody's bid or selling to them early, and which
	of those GunBroker does is not POS's call — the person makes it on
	GunBroker, where both choices are offered with their consequences.
	"""
	row = serial_state(serial_no)
	if row.get("gb_bid_locked"):
		message = _(
			"Serial {0} is an auction with bids on GunBroker (item {1}). POS will "
			"not end it: cancelling bids or selling to the high bidder early is a "
			"decision to make on GunBroker itself."
		).format(serial_no, row.get("gb_item_id") or "?")
		_log_sync_error(serial_no, message)
		return skip(AUCTION_HAS_BIDS, message)
	return end_listing_for(
		"Serial No", serial_no, row,
		reason=reason, client=client,
		subject=_("serial {0}").format(serial_no),
		log_error=lambda message: _log_sync_error(serial_no, message),
		claim=lambda item_id, previous: _claim_item_id(
			serial_no, item_id, previous=previous),
		# Read back rather than closing over the row above: when the end has
		# followed a relist, the claim has already written the old number into
		# the history, and confirming from a stale row would drop it again.
		confirm=lambda item_id: confirm_ended(
			serial_no, item_id, serial_state(serial_no)))


def end_listing_for(doctype: str, name: str, row: dict, *, reason: str | None,
		client: GunBrokerClient | None, subject: str, log_error, confirm,
		claim) -> dict:
	"""End one GunBroker listing, whichever kind of record owns it.

	Two strategies. `Delete Endpoint` (the default) sends the delete and then
	CONFIRMS by re-reading the item: success means a 200 AND `isActive` reading
	back false, because this API returns 200 for things it did not do.
	`Manual Only` sends nothing and raises a flag for a human.

	A 403 is treated as a class of its own: it means GunBroker has withdrawn this
	account's authorisation for the operation, which no retry fixes. The setting
	is put back to `Manual Only` so subsequent guns raise flags instead of piling
	up failed calls, and one alert is sent. Every OTHER failure leaves the setting
	alone — a timeout is not a permissions change.

	One thing this must never do is treat the listing's continued presence in
	`/ItemsSelling` as evidence the delete failed. That list lags by minutes; an
	ended listing sits in it for a while afterwards. The item's own `isActive` is
	the authority.

	Ending a listing is not always one call. A sale that leaves stock behind does
	not end an item-level listing — GunBroker ends it and relists the REMAINDER
	under a new number, naming that number in `relistedAsItemID`. Deleting the
	number we hold then reads back inactive and looks exactly like success, while
	the replacement stays live with no stock behind it and no field pointing at
	it. So the read-back is checked for a replacement, and the end follows the
	chain until there is none. ``claim`` is what moves the record onto each
	replacement first, so an interrupted chase leaves the record naming the
	listing that is still live rather than the one already gone.

	Shared by the per-gun and item-level listings: ``confirm`` writes the ended
	state onto whichever record owns it, and ``subject`` names it in the alerts.
	"""
	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		return skip(UNLISTED, _(
			"There is no live GunBroker listing for {0}.").format(subject))

	settings = _settings()
	strategy = settings.get("end_strategy") or "Delete Endpoint"
	if strategy == "Manual Only":
		return _mark_end_pending(doctype, name, item_id, subject=subject, reason=reason,
			strategy="manual_only",
			why=_("the End Listing Strategy setting is Manual Only"))

	target = item_id
	for _hop in range(RELIST_CHASE_LIMIT):
		try:
			client = client or GunBrokerClient.from_settings()
			client.delete_listing(target)
		except GunBrokerConfigError as exc:
			# Distinct from a failed call twice over: nobody should go hunting
			# through network logs because the integration was switched off, AND
			# nothing will retry this on its own — the hourly sweep is gated on the
			# same switch. `strategy="manual_only"` is what stops the form
			# promising a retry that cannot happen.
			log_error(str(exc))
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason, strategy="manual_only",
				why=_("GunBroker is not set up: {0}").format(exc))
		except GunBrokerError as exc:
			if exc.status_code == 403:
				_downgrade_end_strategy(subject, target)
				# The setting has just been changed, so this really is the manual
				# strategy now — and the caller is told the change happened rather
				# than discovering it later on the Settings form.
				return _mark_end_pending(doctype, name, target, subject=subject,
					reason=reason, strategy="manual_only", downgraded=True,
					why=_("GunBroker refused the end-listing call (not authorised)"))
			log_error(str(exc))
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason,
				why=_("GunBroker did not confirm the end-listing call"))
		except Exception as exc:  # noqa: BLE001
			safe_log_error("GunBroker end listing failed", frappe.get_traceback())
			log_error(str(exc))
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason,
				why=_("the end-listing call could not be completed"))

		try:
			remote = client.get_item(target)
		except GunBrokerConfigError as exc:
			log_error(str(exc))
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason, strategy="manual_only",
				why=_("GunBroker is not set up: {0}").format(exc))
		except GunBrokerError as exc:
			log_error(str(exc))
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason,
				why=_("the listing could not be re-read to confirm it ended"))

		if get_field(remote, "isActive", "IsActive") is not False:
			return _mark_end_pending(doctype, name, target, subject=subject,
				reason=reason,
				why=_("GunBroker still shows the listing as active"))

		# Inactive is not the same as over. A 0 here is not evidence that nothing
		# was relisted — the field has been observed carrying a number and
		# carrying 0, never changing between them, and GunBroker's own support
		# has said not to lean on it. So a 0 ends the chase and hands the SKU to
		# the sweep, which keeps watching this model for
		# `tasks.END_RECHECK_HOURS` after the end for exactly this reason.
		successor = _numeric_id(
			get_field(remote, "relistedAsItemID", "RelistedAsItemID"))
		if not successor or successor == target:
			confirm(target)
			return {"ok": True, "strategy": "delete_endpoint", "confirmed": True,
				"gb_item_id": target}

		# Move the record onto the replacement BEFORE ending it, so a failure on
		# the next pass leaves `gb_item_id` naming the listing that is live.
		claim(successor, target)
		target = successor

	return _mark_end_pending(doctype, name, target, subject=subject, reason=reason,
		why=_("GunBroker relisted this listing again each time it was ended"))


def confirm_ended(serial_no: str, item_id: str, row: dict) -> None:
	"""Record that a listing really is over: ENDED, and never UNLISTED.

	The distinction is the whole safety property. UNLISTED means "this gun has
	never been on GunBroker, help yourself", and a machine path will happily
	list it again; ENDED means it HAS been listed and may have been bought, so
	only a human pressing the button puts it back.

	Two callers reach this: the End Listing button, when GunBroker confirms the
	delete by reading back inactive, and the fifteen-minute end-pending poll,
	when a delete that could not be confirmed at the time turns out to have
	landed after all. Both are the same transition and must write the same
	fields — an end that clears the item number but leaves `gb_delisted` at 0
	is a gun the sweep will re-adopt.
	"""
	frappe.db.set_value("Serial No", serial_no, {
		"gb_item_id": "",
		"gb_item_history": _push_history(row.get("gb_item_history"), item_id),
		"gb_delisted": 1,
		"gb_end_pending": 0,
		"gb_synced_at": now_datetime(),
		"gb_sync_error": "",
		**_clear_gone_tracking(),
	})


def _downgrade_end_strategy(subject: str, item_id: str) -> None:
	"""Put the setting back to Manual Only after a 403, and say so once."""
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "end_strategy", "Manual Only")
	notify_admins(
		_("GunBroker end-listing is no longer authorised"),
		_("GunBroker refused DELETE /Items for item {0} ({1}) with 403. The "
		  "End Listing Strategy has been set back to Manual Only, so listings must "
		  "now be ended by hand on GunBroker until the authorisation is restored — "
		  "until each one is, it can still be bought."
		  ).format(item_id, subject),
		dedup_key="gb_delete_unauthorized", window_minutes=1440)


def gb_item_url(item_id: str) -> str:
	"""Where a person goes to end this listing by hand."""
	host = "sandbox.gunbroker.com" if sandbox_mode() else "www.gunbroker.com"
	return f"https://{host}/item/{item_id}"


def _mark_end_pending(doctype: str, name: str, item_id: str, *, subject: str,
		reason: str | None, why: str, strategy: str = "delete_endpoint",
		downgraded: bool = False) -> dict:
	"""T4: record that this listing still needs ending, and tell somebody.

	Every path through here means the same thing to the person at the counter —
	the gun has left the shelf and its listing is still buyable — so they all
	return the same shape: `pending_manual`, a link to the listing, and a message
	that says so. The distinction the return still carries is `strategy`, because
	"the setting says do this by hand" and "the automatic attempt failed" differ
	in whether the system will try again on its own.

	The alert says the listing can still be bought. That is the whole content of
	the message: an unended listing on a gun that is no longer available is a
	sale waiting to happen that cannot be filled.
	"""
	url = gb_item_url(item_id)
	message = _(
		"GunBroker listing {0} for {1} has NOT been ended — {2}. "
		"Until somebody ends it on GunBroker it can still be bought, and the stock "
		"is no longer available to sell. End it at {3}."
	).format(item_id, subject, why, url)

	frappe.db.set_value(doctype, name, {"gb_end_pending": 1})
	notify_admins(
		_("End GunBroker listing {0} by hand").format(item_id),
		f"{message}\n\n" + _("Reason given: {0}").format(reason or _("not stated")),
		dedup_key=f"gb_end_{doctype}:{name}", window_minutes=1440)

	result = {
		"ok": True,
		"strategy": strategy,
		"confirmed": False,
		"pending_manual": True,
		"gb_item_id": item_id,
		"gb_url": url,
		"message": message,
		"detail": why,
	}
	if downgraded:
		result["downgraded"] = True
	return result


# ---------------------------------------------------------------------------
# Read-only status
# ---------------------------------------------------------------------------

def listing_status(serial_no: str, *, client: GunBrokerClient | None = None) -> dict:
	"""Local state plus, when there is a listing, what GunBroker currently says."""
	return listing_status_for(serial_state(serial_no), client=client)


def listing_status_for(row: dict, *, client: GunBrokerClient | None = None) -> dict:
	"""The same read for either listing kind — the row is all it needs."""
	out: dict[str, Any] = {
		"ok": True,
		"state": listing_state(row),
		"gb_item_id": row.get("gb_item_id") or "",
		"gb_sync_error": row.get("gb_sync_error") or "",
		"remote": None,
	}
	item_id = str(row.get("gb_item_id") or "")
	if not item_id:
		return out
	try:
		client = client or GunBrokerClient.from_settings()
		probe = client.item_probe(item_id)
	except (GunBrokerError, GunBrokerConfigError) as exc:
		out["remote"] = {"state": "error", "detail": str(exc)}
		return out
	item = probe.get("item") or {}
	out["remote"] = {
		"state": probe["state"],
		"is_active": get_field(item, "isActive", "IsActive"),
		"has_winning_bid": get_field(item, "hasWinningBid", "HasWinningBid"),
		"quantity": get_field(item, "quantity", "Quantity"),
		"fixed_price": get_field(item, "fixedPrice", "FixedPrice"),
	}
	return out


def clear_sold_unknown(serial_no: str, note: str) -> dict:
	"""Lift the "may already be sold on GunBroker" flag, on a human's say-so.

	Requires a note, and records it as a Comment. This flag is fail-closed
	protection against selling a gun twice; clearing it is somebody asserting
	they checked GunBroker and it was not sold, and that assertion should be
	attributable afterwards.
	"""
	row = serial_state(serial_no)
	if not row.get("gb_sold_unknown"):
		return {"ok": True, "changed": False}
	frappe.db.set_value("Serial No", serial_no, {
		"gb_sold_unknown": 0, "gb_sync_error": ""})
	frappe.get_doc("Serial No", serial_no).add_comment(
		"Comment",
		_("Cleared the GunBroker \"sold unknown\" flag after checking GunBroker: {0}"
		  ).format(note))
	return {"ok": True, "changed": True}


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def _account_label(account: Any) -> str:
	"""The human-readable name of the GunBroker account a call just reached.

	``GET /Users/AccountInfo`` splits the person from the account: the top level
	is the PERSON (``firstName``/``lastName``, addresses, emails) and the ACCOUNT
	sits one level down in ``userSummary`` — ``userID``, ``username``,
	``accountStatus``. Reading ``userName`` off the top level, which is what this
	did at first, finds nothing and reports an empty string.

	Three sources, in falling order of how specifically they answer "which
	account is this":

	1. ``userSummary.username`` — the account handle, what the sandbox returns;
	2. a top-level ``userName``/``username`` — not observed today, but it is
	   what the code read before, and keeping it costs one line;
	3. ``firstName lastName`` — the person, when no username is stated anywhere.

	Empty string if none of them are there. This field exists so an operator can
	see WHICH account they just touched before they trust a listing push; a
	wrong-but-plausible name would be worse than a blank, so nothing here
	invents one — but throwing would also lose the rest of the report, which is
	the part that says whether the credentials work at all.
	"""
	# `get_field` returns the default for a non-dict, so the nested read is safe
	# whether `userSummary` is absent, null, or something unexpected.
	name = get_field(get_field(account, "userSummary"), "username", "userName")
	if not name:
		name = get_field(account, "userName", "username")
	if not name:
		parts = (get_field(account, "firstName") or "",
			get_field(account, "lastName") or "")
		name = " ".join(str(p).strip() for p in parts if str(p).strip())
	return str(name or "").strip()


UNAPPROVED_CALLER = "Unapproved User Parameter"


def explain_unapproved(message: str, user_agent: str = "") -> str:
	"""GunBroker's 403 for a User-Agent it does not recognise blames the DevKey.

	"Unapproved User Parameter … Please register at …/DevKey/Create" is what
	production sends when the User-Agent does not carry the name the DevKey was
	registered under. The key can be registered, approved and in use elsewhere
	at that moment; the login fails the same way as everything else; and the
	sandbox never checks, so a configuration proven there proves nothing here.
	Reporting the sentence verbatim sent the go-live after a new key, then an
	IP allow-list, then an access token — none of which was the problem.
	"""
	if UNAPPROVED_CALLER not in message:
		return message
	hint = _(
		"GunBroker refused the caller, not the request. Production insists that "
		"the User-Agent header carry the application name this DevKey was "
		"registered under — verbatim, case-sensitive — and answers everything "
		"else, the login included, with exactly this message; it says nothing "
		"about the key. The header is built in GunBroker's Software/Seller/"
		"Version/Application format and its Seller segment (GunBroker Settings "
		"→ Seller name) has to change with the DevKey. The sandbox does not "
		"check it, so a configuration that worked there proves nothing here.")
	if user_agent:
		hint += "\n" + _("This store sent: {0}").format(user_agent)
	return "{0}\n\n{1}".format(message, hint)


def test_connection() -> dict:
	"""Prove the credentials work, and say which environment they reached."""
	sandbox = sandbox_mode()
	result: dict[str, Any] = {"ok": False, "sandbox": sandbox, "warnings": []}
	try:
		client = GunBrokerClient.from_settings()
	except GunBrokerConfigError as exc:
		result["error"] = str(exc)
		return result

	result["base_url"] = client.config.base_url
	try:
		time_body = client.server_time()
		result["server_time"] = str(
			get_field(time_body, "gunBrokerTime", "GunBrokerTime") or "")
		account = client.get_account_info()
		result["account"] = _account_label(account)
		defaults = client.get_listing_defaults()
	except (GunBrokerError, GunBrokerConfigError) as exc:
		result["error"] = explain_unapproved(str(exc), client.config.user_agent)
		return result

	gateway = bool(get_field(defaults, "gatewayEnabled", "GatewayEnabled"))
	card_on = bool(_settings().get("card_checkout_enabled"))
	result["gateway_enabled"] = gateway
	# Only report the combinations that are actually a problem. Cards off and no
	# gateway is the intended configuration for this phase; warning about it
	# every time somebody tests the connection would train people to skip
	# reading the warnings.
	if card_on and not gateway:
		result["warnings"].append(_(
			"Card checkout is switched on here, but the GunBroker account has no "
			"payment gateway connected — buyers still cannot pay by card."))
	elif gateway and not card_on:
		result["warnings"].append(_(
			"The GunBroker account has a payment gateway connected, but this store "
			"is set to take cheques and money orders only."))

	result["ok"] = True
	return result


def stale_push_cutoff():
	"""The moment before which an unconfirmed attempt may be written off.

	Exposed so the sweep and the tests share one definition of the grace period
	rather than each carrying their own copy of the number.
	"""
	return add_to_date(now_datetime(), hours=-PUSH_GRACE_HOURS)
