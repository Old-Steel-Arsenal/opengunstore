"""Serial No → GunBroker listing payload.

Pure functions wherever the rule allows it, with the database and file reads
pushed to the two entry points (:func:`build_listing_payload`,
:func:`picture_parts`). The push path takes a row lock, and the lock must be
held for milliseconds — so the expensive half, reading photo bytes off disk,
is a separate call the caller makes OUTSIDE the lock.

Two things about GunBroker shape what is here more than the docs do:

* **Most of a listing cannot be edited after it is posted.** ``PUT /Items``
  changes the price and the title. Serial number, condition, category and
  duration are accepted with a 200 and ignored. So getting the payload right
  the first time is not tidiness — a wrong category means ending the listing
  and posting a new one, under a new item number.
* **Titles are policy-filtered, opaquely.** A title containing a banned phrase
  is rejected with a generic policy message that never names the phrase.
  Checking locally first turns "GunBroker said no, good luck" into "your title
  contains 'do not buy'".
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Any

import frappe

from ffl_core.firearm import PISTOL, REVOLVER, current_price, effective_serial_price
from ffl_core.images import ordered_gallery_paths
from ffl_core.utils import single_flags

SETTINGS_DOCTYPE = "GunBroker Settings"

# GunBroker's hard limit, enforced as a field validation: 78 characters comes
# back as "must be a string with a maximum length of 75".
TITLE_MAX = 75

# Condition codes. From the Item alone only 1 and 3 are reachable: this POS
# records "used or not", and inventing a 2 from a blank condition_grade would be
# a guess written into a field that cannot be corrected without relisting. New
# Old Stock is therefore only ever a person's choice, made per gun at Send.
CONDITION_FACTORY_NEW = 1
CONDITION_NEW_OLD_STOCK = 2
CONDITION_USED = 3
CONDITION_CODES = (CONDITION_FACTORY_NEW, CONDITION_NEW_OLD_STOCK, CONDITION_USED)
# The Serial No `gb_condition` Select stores code-plus-label, like the Settings ones.
CONDITION_LABELS = {CONDITION_FACTORY_NEW: "1 Factory New",
	CONDITION_NEW_OLD_STOCK: "2 New Old Stock", CONDITION_USED: "3 Used"}

# A firearm is a curio or relic once it is 50 years old (27 CFR 478.11). Used
# only to prefer a C&R category row when the store has configured one.
CR_AGE_YEARS = 50

HANDGUN_TYPES: frozenset[str] = frozenset({PISTOL, REVOLVER})

# Every Check on GunBroker Settings whose value shapes the listing payload, with
# the default its DocType JSON declares.
#
# Read through `single_flags`, NOT off the settings document, and the difference
# is not stylistic. `BaseDocument._fix_numeric_types` casts every Check through
# `cint()` unconditionally when a Single loads, so a field nobody has saved
# arrives as `0` — indistinguishable from one somebody deliberately unchecked.
# And a Single applies its docfield defaults only while it has NO stored rows at
# all, so the first row written (by a patch, or a seed) silently turns every
# other Check on the form to 0. Off a document read, `is_ffl_required` would
# then be false on a firearm listing. The raw `tabSingles` row can tell the two
# apart; that is the whole reason `ffl_core.utils.single_flag` exists, and this
# is its batched sibling — one query for the lot.
LISTING_FLAG_DEFAULTS: dict[str, int] = {
	"is_ffl_required": 1,
	"will_ship_international": 0,
	"use_default_exclude_states": 1,
	"card_checkout_enabled": 0,
	"pay_certified_check": 1,
	"pay_usps_money_order": 1,
	"pay_money_order": 1,
	"pay_personal_check": 0,
	"pay_cod": 0,
	"pay_visa_mastercard": 0,
	"pay_amex": 0,
	"pay_discover": 0,
}

# Image formats GunBroker accepts. PNG is on this list because it was tested,
# not assumed: earlier notes claimed PNGs were silently dropped, and a single
# PNG uploaded and read back fine.
ALLOWED_IMAGE_EXTS: dict[str, str] = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".gif": "image/gif",
}

# Photos sent with POST /Items. The ninth onward are dropped silently — no
# error, just fewer pictures than were sent — so the split is enforced here.
PICTURES_PER_REQUEST = 8

# One remote image. Generous enough for a real photo, small enough that a
# mistyped URL pointing at something enormous cannot stall a push.
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024

# The watermark GunBroker photos carry (GunBroker Settings > Listings > Watermark):
# a rounded, half-transparent white panel in the lower right corner holding the
# store logo and, under it, a line of text. Sized off the photo's width so it
# reads the same on a 1000 px and a 4000 px upload.
WATERMARK_WIDTH = 0.30
WATERMARK_PANEL_ALPHA = 0.55
WATERMARK_INK_ALPHA = 0.75

_HTTP_ATTR_RE = re.compile(r'\b(src|href)\s*=\s*(["\'])http://([^"\']*)\2', re.IGNORECASE)
# Either scheme — a private host is unreachable for a buyer over https too, and
# the function that uses this is about reachability, not about mixed content.
_ANY_SCHEME_ATTR_RE = re.compile(
	r'\b(src|href)\s*=\s*(["\'])https?://([^"\']*)\2', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_PRIVATE_HOST_RE = re.compile(
	r"^(localhost|0\.0\.0\.0|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+"
	r"|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
	r"|[^/]*\.local)(?::\d+)?$",
	re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def gb_title(raw: str) -> tuple[str, list[str]]:
	"""A listing title within GunBroker's 75-character field limit.

	Strips markup, collapses whitespace, and — if it still does not fit — cuts at
	a word boundary rather than mid-word, then trims any punctuation left dangling
	at the cut.

	What it deliberately does NOT do is normalise the text. Asterisks, double
	quotes and ALL CAPS all survive a round trip untouched, so there is nothing to
	clean up; and title-casing would turn CZ into Cz, FN into Fn and AR-15 into
	Ar-15 — mangling exactly the tokens a buyer searches for.

	Returns:
	    ``(title, warnings)``; warnings contains ``title_truncated`` if it was cut.
	"""
	text = frappe.utils.strip_html(str(raw or ""))
	text = re.sub(r"\s+", " ", text).strip()
	if len(text) <= TITLE_MAX:
		return text, []
	cut = text[:TITLE_MAX + 1]
	space = cut.rfind(" ")
	trimmed = (cut[:space] if space > 0 else text[:TITLE_MAX]).rstrip(" ,;:-–—/&")
	return trimmed, ["title_truncated"]


def banned_title_phrase(title: str, banned_phrases: str | None) -> str:
	"""The first banned phrase this title contains, or "".

	Case-insensitive, substring, one phrase per configured line. GunBroker's own
	rejection is a 400 that quotes the policy page and never says which words
	offended it, so checking here is the only way an operator learns what to fix.
	"""
	haystack = str(title or "").lower()
	for line in str(banned_phrases or "").splitlines():
		phrase = line.strip().lower()
		if phrase and phrase in haystack:
			return phrase
	return ""


def gb_condition(is_used: Any, is_antique: Any = False, override: Any = 0) -> int:
	"""GunBroker's Condition code: 1 Factory New, 2 New Old Stock or 3 Used.

	``condition_grade`` (Excellent / Very Good / …) deliberately does not feed
	this. It is a within-used gradation and describes the prose, not the code; an
	ungraded second-hand gun is Used, full stop. A pre-1899 antique is never
	Factory New, whatever ``is_used`` says (same rule as the Woo ``pa_condition``).

	``override`` is the code somebody chose for this one gun at Send; 0 or any
	other value follows the Item. An antique ignores it: the antique rule wins,
	so a choice made for an antique cannot list it as new.
	"""
	if is_antique:
		return CONDITION_USED
	code = frappe.utils.cint(override)
	if code in CONDITION_CODES:
		return code
	return CONDITION_USED if is_used else CONDITION_FACTORY_NEW


def is_cr_eligible(year_made: Any, *, today_year: int | None = None) -> bool:
	"""True when a gun is old enough to be a curio or relic (50+ years).

	Age is the only C&R test this POS can apply on its own — the other two routes
	(ATF's published C&R list, and a museum-interest certification) are per-model
	facts nobody has recorded here. Used ONLY to prefer a C&R category row when
	the store has configured one; it never decides whether a gun may be listed.
	"""
	from ffl_core.firearm import sane_year_made

	year = sane_year_made(year_made)
	if not year:
		return False
	current = today_year or frappe.utils.now_datetime().year
	return (current - year) >= CR_AGE_YEARS


def resolve_category(*, item_override: Any, firearm_type: str,
		cr_eligible: bool, category_map, is_antique: bool = False) -> int:
	"""The GunBroker category ID for one gun, or 0 when there is no rule for it.

	Order: the Item's own override wins; then a C&R row for this firearm type if
	the gun is old enough and the store configured one; then the plain row for the
	type.

	An antique (``is_antique``) takes ONLY a row flagged ``is_antique`` for its
	type — never the plain or C&R row, because GunBroker files pre-1899 guns
	under their own Antique categories and a modern-category listing that says
	no FFL is required is exactly the shape their policy team looks for. The
	converse holds too: an antique row is never handed to a modern gun.

	Zero means "no rule", and the caller REFUSES the listing rather than falling
	back to something generic. A firearm in the wrong GunBroker category is not a
	cosmetic problem: at best it is invisible to the buyers who would have bought
	it, at worst it is a policy violation against the account.
	"""
	override = frappe.utils.cint(item_override or 0)
	if override > 0:
		return override

	ftype = (firearm_type or "").strip()
	if not ftype:
		return 0
	plain = 0
	for row in (category_map or []):
		row_type = str(_row_get(row, "firearm_type") or "").strip()
		if row_type != ftype:
			continue
		category_id = frappe.utils.cint(_row_get(row, "category_id") or 0)
		if category_id <= 0:
			continue
		row_antique = bool(_row_get(row, "is_antique"))
		if is_antique:
			if row_antique:
				return category_id
			continue
		if row_antique:
			continue
		if _row_get(row, "is_cr_eligible"):
			if cr_eligible:
				return category_id
		elif not plain:
			plain = category_id
	return plain


def resolve_item_category(*, item_override: Any, item_group: str, category_map) -> int:
	"""The GunBroker category ID for one non-firearm Item, or 0 when no rule fits.

	Ammunition, optics and parts have no ``firearm_type`` to key on, so they are
	mapped by Item Group instead: the Item's own override wins, then the
	``category_map`` row naming that group. Exact match only — a group tree walk
	would quietly file "Ammunition > Steel Case" under whatever its parent says,
	and GunBroker's ammo categories are not interchangeable.

	Kept separate from :func:`resolve_category` rather than folded into it: the
	two never overlap (a gun row carries ``firearm_type`` and no group, an item
	row the reverse), and entangling them would put the C&R / antique precedence
	rules on a code path that has no guns on it.

	Zero means "no rule", and the caller REFUSES the listing. Same reasoning as
	the firearm side: the wrong category is invisible to buyers at best and a
	policy problem at worst.
	"""
	override = frappe.utils.cint(item_override or 0)
	if override > 0:
		return override

	group = (item_group or "").strip()
	if not group:
		return 0
	for row in (category_map or []):
		if str(_row_get(row, "item_group") or "").strip() != group:
			continue
		category_id = frappe.utils.cint(_row_get(row, "category_id") or 0)
		if category_id > 0:
			return category_id
	return 0


def ship_cost_for(firearm_type: str, *, long_gun_cost: Any, handgun_cost: Any) -> float:
	"""The flat Ground shipping charged for this gun.

	Two rates, matching the website's two shipping classes, so a buyer is quoted
	the same postage whichever channel they buy through.

	Zero is returned when the rate is unset, and the caller refuses the listing —
	deliberately, because 0 is also what an unconfigured Currency field reads as,
	and "buyer pays a fixed $0" means shipping firearms across the country for
	free.
	"""
	raw = handgun_cost if (firearm_type or "").strip() in HANDGUN_TYPES else long_gun_cost
	return float(frappe.utils.flt(raw or 0))


def listing_ship_cost(own: Any, default: Any) -> float:
	"""The buyer-paid Ground rate one listing carries: its own, else the default.

	The person pressing Send decides the rate; the Settings figure is what they
	are offered. Empty and 0 both mean "nothing of its own" — a Currency field
	nobody has typed in reads 0 — so the default fills them, and a default of
	0 is refused by the caller exactly as before.
	"""
	rate = float(frappe.utils.flt(own or 0))
	return rate if rate > 0 else float(frappe.utils.flt(default or 0))


# GunBroker's "Seller pays for shipping". A gun marked free shipping goes out
# with this and a Ground cost of 0; GunBroker reads it back as
# freeShippingAvailable (checked on the sandbox, 2026-10-08).
WHO_PAYS_SELLER = 2


LISTING_FIXED = "Fixed Price"
LISTING_AUCTION = "Auction"
LISTING_TYPES = (LISTING_FIXED, LISTING_AUCTION)
# GunBroker's auction lengths, in days. 4, 6, 8, 9, 11, 12 and 13 are legal
# too; these are the ones worth a line in a dropdown.
AUCTION_DURATIONS = (1, 3, 5, 7, 10, 14)
# GunBroker refuses a reserve on its two shortest auctions.
NO_RESERVE_DURATIONS = (1, 3)
AUCTION_RELIST_UNTIL_SOLD = 2
AUCTION_NO_RELIST = 1
AUCTION_RELIST_OPTIONS = (AUCTION_NO_RELIST, AUCTION_RELIST_UNTIL_SOLD)

# GunBroker's paid promotions, the only four the Send dialog offers:
# spec key -> (Serial No Check field, key inside the payload's PremiumFeatures).
# GunBroker bills each one, and `POST /Items` is the only call that can set
# them (`PUT /Items/{id}` takes none), so they are chosen when the listing is
# made and never again.
PREMIUM_FEATURES = {
	"featured": ("gb_featured", "IsFeaturedItem"),
	"showcase": ("gb_showcase", "IsShowCaseItem"),
	"sponsored_onsite": ("gb_sponsored_onsite", "IsSponsoredOnsite"),
	"view_counter": ("gb_view_counter", "HasViewCounter"),
}


def validate_listing_spec(spec: dict | None) -> dict:
	"""Normalise what the Send dialog chose, refusing what GunBroker would.

	The dialog checks the same rules so the person sees them as they type;
	this is the copy that cannot be bypassed, because the dialog is one of
	several ways to reach the push (the MCP tools and the import skill are
	the others). The rules are GunBroker's: an auction starts from a bid, a
	reserve is refused on a one- or three-day auction, and a Buy Now that is
	below the reserve or the opening bid is a listing nobody can use.

	Returns:
	    The spec with every number a float, the duration and relist code ints,
	    and the title stripped of markup. A fixed-price spec keeps only what
	    applies to it; the auction figures are left at 0. ``condition`` is the
	    GunBroker code chosen for this gun (1, 2 or 3), or 0 to follow the Item.
	    ``premium`` is always
	    the four paid-promotion flags as bools (all False when the spec names
	    none); a name that is not one of the four is refused rather than
	    ignored, because a typo would otherwise silently buy nothing.
	"""
	raw = spec or {}
	kind = str(raw.get("type") or LISTING_FIXED).strip()
	if kind not in LISTING_TYPES:
		raise ValueError(f"unknown listing type {kind!r}")
	terms = {
		"type": kind,
		"title": re.sub(r"\s+", " ", frappe.utils.strip_html(str(raw.get("title") or ""))).strip(),
		"price": float(frappe.utils.flt(raw.get("price") or 0)),
		"starting_bid": float(frappe.utils.flt(raw.get("starting_bid") or 0)),
		"reserve_price": float(frappe.utils.flt(raw.get("reserve_price") or 0)),
		"buy_now_price": float(frappe.utils.flt(raw.get("buy_now_price") or 0)),
		"duration": frappe.utils.cint(raw.get("duration") or 0),
		"auto_relist": frappe.utils.cint(raw.get("auto_relist") or 0),
	}
	for key in ("price", "starting_bid", "reserve_price", "buy_now_price"):
		if terms[key] < 0:
			raise ValueError(f"the {key.replace('_', ' ')} cannot be negative")
	premium = raw.get("premium") or {}
	if not isinstance(premium, dict):
		raise ValueError("premium must be an object of promotion names")
	unknown = sorted(set(premium) - set(PREMIUM_FEATURES))
	if unknown:
		raise ValueError(f"unknown paid promotion {unknown[0]!r}; the choices are "
			+ ", ".join(PREMIUM_FEATURES))
	# _enum_code also reads the stored label ("2 New Old Stock"), so a caller echoing
	# the Serial No field back is not silently turned into "follow the item".
	raw_condition = raw.get("condition")
	condition = _enum_code(raw_condition, default=0) if raw_condition not in (None, "") else 0
	if raw_condition not in (None, "", 0, "0") and condition not in CONDITION_CODES:
		raise ValueError("the condition must be 1 (Factory New), 2 (New Old Stock) "
			"or 3 (Used), or empty to follow the item")
	terms["condition"] = condition
	terms["premium"] = {key: bool(frappe.utils.cint(premium.get(key))) for key in PREMIUM_FEATURES}
	if kind == LISTING_FIXED:
		terms.update(starting_bid=0.0, reserve_price=0.0, buy_now_price=0.0,
			duration=0, auto_relist=0)
		return terms
	if terms["starting_bid"] <= 0:
		raise ValueError("an auction needs a starting bid")
	if terms["duration"] not in AUCTION_DURATIONS:
		raise ValueError("the auction length must be one of "
			+ ", ".join(str(d) for d in AUCTION_DURATIONS) + " days")
	if terms["auto_relist"] not in AUCTION_RELIST_OPTIONS:
		raise ValueError("auto-relist must be 2 (relist until sold) or 1 (do not relist)")
	if terms["reserve_price"]:
		if terms["duration"] in NO_RESERVE_DURATIONS:
			raise ValueError("GunBroker does not allow a reserve on a 1- or 3-day auction")
		if terms["reserve_price"] < terms["starting_bid"]:
			raise ValueError("the reserve cannot be below the starting bid")
	floor = max(terms["starting_bid"], terms["reserve_price"])
	if terms["buy_now_price"] and terms["buy_now_price"] < floor:
		raise ValueError("Buy Now must be at least the starting bid and the reserve")
	return terms


def fixed_listing_price(serial: dict, pos_price: Any) -> float:
	"""The fixed price a listing of this gun carries: its own, else the POS price.

	`gb_list_price` is what somebody typed into the Send dialog for THIS gun on
	GunBroker; empty means the listing follows the POS sell price, including
	every later change to it. The POS price is passed in rather than read here
	so each caller resolves it through its own `effective_serial_price`.
	"""
	own = float(frappe.utils.flt((serial or {}).get("gb_list_price") or 0))
	return own if own > 0 else float(frappe.utils.flt(pos_price or 0))


def auction_terms(serial: dict, settings) -> dict:
	"""The auction figures saved on the gun, with Settings filling the gaps."""
	return validate_listing_spec({
		"type": LISTING_AUCTION,
		"starting_bid": serial.get("gb_starting_bid"),
		"reserve_price": serial.get("gb_reserve_price"),
		"buy_now_price": serial.get("gb_buy_now_price"),
		"duration": frappe.utils.cint(serial.get("gb_auction_duration"))
			or frappe.utils.cint(settings.get("auction_duration") or 7),
		"auto_relist": frappe.utils.cint(serial.get("gb_auction_auto_relist"))
			or _enum_code(settings.get("auction_auto_relist"), default=AUCTION_RELIST_UNTIL_SOLD),
	})


def listing_flags() -> dict[str, bool]:
	"""The Settings checkboxes that shape a listing, read from the stored rows.

	One query. See :data:`LISTING_FLAG_DEFAULTS` for why this does not read them
	off the settings document.
	"""
	return single_flags(SETTINGS_DOCTYPE, LISTING_FLAG_DEFAULTS)


def payment_methods(*, flags: dict[str, bool] | None = None) -> dict[str, bool]:
	"""The ``PaymentMethods`` object, built from the Settings checkboxes.

	Only the enabled ones are sent. PayPal is not offered at all — GunBroker
	banned it for firearms in 2022 — and neither is ``SeeItemDesc`` (deprecated)
	nor ``FreedomCoin``.

	Card methods are suppressed unless card checkout is switched on, so a
	checkbox left ticked from a future configuration cannot advertise a payment
	route the buyer would find unusable at checkout.
	"""
	if flags is None:
		flags = listing_flags()
	elif not isinstance(flags, dict):
		# A Settings DOCUMENT also answers `.get()`, so passing one here would run
		# perfectly and silently reinstate the bug this whole path exists to fix:
		# every unsaved Check reads back as 0 off a document, so the defaults
		# vanish and a listing goes out with no payment method and no FFL flag.
		# Keyword-only above stops it arriving positionally; this stops it
		# arriving at all.
		raise TypeError(
			"payment_methods() takes resolved flags from listing_flags(), not a "
			f"settings document (got {type(flags).__name__})")
	card_ok = flags.get("card_checkout_enabled", False)
	# (settings field, GunBroker key, needs the card switch)
	mapping_table = (
		("pay_certified_check", "CertifiedCheck", False),
		("pay_usps_money_order", "USPSMoneyOrder", False),
		("pay_money_order", "MoneyOrder", False),
		("pay_personal_check", "Check", False),
		("pay_cod", "COD", False),
		("pay_visa_mastercard", "VisaMastercard", True),
		("pay_amex", "AmericanExpress", True),
		("pay_discover", "Discover", True),
	)
	out: dict[str, bool] = {}
	for field, gb_key, needs_card in mapping_table:
		if needs_card and not card_ok:
			continue
		if flags.get(field):
			out[gb_key] = True
	return out


def gb_description(*, serial_description: str, item_description: str,
		footer: str = "") -> tuple[str, list[str]]:
	"""The listing body, plus the store's boilerplate.

	A per-gun description is plain text typed by staff, so its newlines become
	``<br>`` and its content is HTML-escaped. An Item description is already HTML
	and passes through. The footer is appended either way.

	Every ``http://`` asset is then dealt with, because GunBroker serves listings
	over https and a browser silently blocks mixed content — the seller sees a
	fine listing and the buyer sees a broken one:

	* an image on a private or loopback host is REMOVED, http or https alike. It
	  could never load for a buyer either way, so upgrading its scheme would only
	  make it fail differently.
	* everything else is upgraded to ``https://``.
	* a bare ``http://`` left in running text is reported and kept — it is prose,
	  not an asset, and refusing a whole listing over a URL somebody typed into a
	  sentence would be absurd.

	Returns:
	    ``(html, warnings)``.
	"""
	warnings: list[str] = []
	text = (serial_description or "").strip()
	if text:
		body = "<br>\n".join(frappe.utils.escape_html(line) for line in text.splitlines())
	else:
		body = item_description or ""

	if footer:
		body = f"{body}\n{footer}" if body else footer

	body, removed = _strip_unreachable_images(body)
	if removed:
		warnings.append("insecure_image_removed")

	body, upgraded = _HTTP_ATTR_RE.subn(
		lambda m: f'{m.group(1)}={m.group(2)}https://{m.group(3)}{m.group(2)}', body)
	if upgraded:
		warnings.append("upgraded_to_https")

	if "http://" in body:
		warnings.append("plain_http_in_text")

	return body, warnings


def _strip_unreachable_images(html: str) -> tuple[str, int]:
	"""Drop <img> tags whose source is on a host no buyer can reach."""
	removed = 0

	def _replace(match: re.Match) -> str:
		nonlocal removed
		tag = match.group(0)
		attr = _ANY_SCHEME_ATTR_RE.search(tag)
		if attr and _PRIVATE_HOST_RE.match(attr.group(3).split("/")[0]):
			removed += 1
			return ""
		return tag

	return _IMG_TAG_RE.sub(_replace, html), removed


def _row_get(row, key: str):
	if isinstance(row, dict):
		return row.get(key)
	return getattr(row, key, None)


# ---------------------------------------------------------------------------
# Database-facing entry points
# ---------------------------------------------------------------------------

_SERIAL_FIELDS = ("name", "item_code", "item_name", "description", "status",
	"condition_grade", "firearm_type", "year_made", "image", "sell_price", "gb_ship_cost",
	"gb_free_shipping", "gb_listing_title", "gb_list_price", "gb_listing_type", "gb_starting_bid",
	"gb_reserve_price", "gb_buy_now_price", "gb_auction_duration", "gb_auction_auto_relist",
	"gb_condition", *(field for field, _key in PREMIUM_FEATURES.values()))
_ITEM_FIELDS = ("item_name", "description", "is_used", "is_antique", "is_firearm",
	"firearm_type", "year_made", "upc", "mpn", "uniform_sell_price", "standard_rate",
	"gb_category_id", "image")

# The item-level listing reads the same Item row plus what only it needs: the
# group that resolves its category, the state list that shapes ExcludeStates,
# and the listing state it keeps on itself.
_ITEM_LISTING_FIELDS = _ITEM_FIELDS + ("item_code", "item_group", "restricted_states",
	"gb_item_id", "gb_delisted", "gb_qty_pushed", "gb_ship_cost",
	# What only a MODEL (uniform-price gun) listing reads: the price window the web
	# store follows, and the paid promotions the Send dialog recorded on the Item.
	"sale_price", "sale_from", "sale_to",
	*(field for field, _key in PREMIUM_FEATURES.values()))


def listing_context(serial_no: str) -> dict:
	"""The Serial No + parent Item rows every rule here needs, read once.

	Both rows in one place so a guard and the payload builder that runs after it
	are looking at the same data, and so the read count does not grow quietly as
	rules are added.
	"""
	serial = frappe.db.get_value(
		"Serial No", serial_no, list(_SERIAL_FIELDS), as_dict=True) or frappe._dict()
	item_code = serial.get("item_code")
	item = (frappe.db.get_value(
		"Item", item_code, list(_ITEM_FIELDS), as_dict=True) if item_code else None
	) or frappe._dict()
	return {"serial": serial, "item": item}


def serial_attr(serial: dict, item: dict, field: str):
	"""A per-gun attribute, serial value first and the model's as a fallback.

	The pair exists because both levels really do carry these: a gun records its
	own type/grade/year when it is known, and inherits the model's when it is not.
	``is_used`` is NOT one of them — it lives only on the Item — so it must not be
	read through here or it will silently resolve to nothing.
	"""
	value = serial.get(field)
	if value in (None, "", 0):
		return item.get(field)
	return value


def serial_category(serial: dict, item: dict, settings) -> int:
	"""The GunBroker category one gun lists under, or 0 when no rule fits.

	The single place the push guard, the payload and the Send dialog ask, so
	the category a person is shown is the one the listing gets.
	"""
	return resolve_category(
		item_override=item.get("gb_category_id"),
		firearm_type=str(serial_attr(serial, item, "firearm_type") or ""),
		cr_eligible=is_cr_eligible(serial_attr(serial, item, "year_made")),
		category_map=settings.get("category_map") or [],
		is_antique=bool(item.get("is_antique")))


def category_map_ids(category_map, flag: str) -> set[int]:
	"""The category ids of the map rows ticked ``flag`` ("is_antique", "is_cr_eligible")."""
	return {frappe.utils.cint(_row_get(r, "category_id"))
		for r in (category_map or []) if _row_get(r, flag)}


def routes_by_age(category_map, firearm_type: str) -> bool:
	"""True when the map has a C&R row for this firearm type, so which
	category a gun of the type gets depends on its own year."""
	ftype = (firearm_type or "").strip()
	return any(_row_get(r, "is_cr_eligible")
		and str(_row_get(r, "firearm_type") or "").strip() == ftype
		for r in (category_map or []))


def category_map_names(category_map) -> dict[int, str]:
	"""category id → the name the store gave it in the map."""
	return {frappe.utils.cint(_row_get(r, "category_id")): _row_get(r, "category_name") or ""
		for r in (category_map or [])}


def ships_free(serial: dict) -> bool:
	"""True when this gun's listing is marked free shipping (seller pays)."""
	return bool(frappe.utils.cint(serial.get("gb_free_shipping")))


def premium_flags(serial: dict) -> dict[str, bool]:
	"""The paid promotions recorded on this gun, by spec key."""
	return {key: bool(frappe.utils.cint(serial.get(field)))
		for key, (field, _gb_key) in PREMIUM_FEATURES.items()}


def build_listing_payload(serial_no: str, *, settings=None, context: dict | None = None) -> dict:
	"""The full ``POST /Items`` payload for one gun.

	Assumes the push guards have already run: it will raise rather than invent a
	category or a shipping cost, but "raise" here is a programming-error backstop,
	not the operator-facing refusal. The refusal happens in listings.py, before
	any of this is reached and before any credential is read.

	Args:
	    serial_no: The Serial No to list.
	    settings: An already-loaded GunBroker Settings doc (optional).
	    context: An already-loaded :func:`listing_context` (optional).

	Returns:
	    ``{"payload": {...}, "warnings": [...]}``. Pictures are NOT included —
	    they are bytes off disk and are fetched separately by
	    :func:`picture_parts`, so the caller can do that work outside its row lock.
	"""
	settings = settings or frappe.get_single("GunBroker Settings")
	ctx = context or listing_context(serial_no)
	serial, item = ctx["serial"], ctx["item"]
	warnings: list[str] = []

	# A title typed into the Send dialog is sent as typed (within the field
	# limit and the banned-phrase check); otherwise the item name is.
	title, title_warnings = gb_title(
		serial.get("gb_listing_title") or serial.get("item_name")
		or item.get("item_name") or serial.get("item_code"))
	warnings.extend(title_warnings)

	description, desc_warnings = gb_description(
		serial_description=serial.get("description") or "",
		item_description=item.get("description") or "",
		footer=settings.get("description_footer") or "")
	warnings.extend(desc_warnings)

	firearm_type = str(serial_attr(serial, item, "firearm_type") or "")
	is_antique = bool(item.get("is_antique"))
	category_id = serial_category(serial, item, settings)
	if category_id <= 0:
		raise ValueError(f"No GunBroker category resolves for {serial_no!r}")

	free_shipping = ships_free(serial)
	ship_cost = 0.0 if free_shipping else listing_ship_cost(serial.get("gb_ship_cost"), ship_cost_for(
		firearm_type,
		long_gun_cost=settings.get("long_gun_ship_cost"),
		handgun_cost=settings.get("handgun_ship_cost")))
	if ship_cost <= 0 and not free_shipping:
		raise ValueError(f"No GunBroker shipping cost configured for {serial_no!r}")

	# Fixed price or auction, decided at Send and recorded on the gun. An
	# auction carries no FixedPrice at all: GunBroker reads the presence of
	# StartingBid as "this is an auction", and its length and relist rule are
	# the auction ones, not the thirty-day fixed-price ones.
	if str(serial.get("gb_listing_type") or LISTING_FIXED) == LISTING_AUCTION:
		terms = auction_terms(serial, settings)
		pricing: dict[str, Any] = {"StartingBid": terms["starting_bid"]}
		if terms["reserve_price"]:
			pricing["ReservePrice"] = terms["reserve_price"]
		if terms["buy_now_price"]:
			pricing["BuyNowPrice"] = terms["buy_now_price"]
		duration, auto_relist = terms["duration"], terms["auto_relist"]
	else:
		price = fixed_listing_price(serial, effective_serial_price(serial_no))
		if price <= 0:
			raise ValueError(f"{serial_no!r} has no sell price")
		pricing = {"FixedPrice": price}
		duration = frappe.utils.cint(settings.get("listing_duration") or 30)
		auto_relist = _enum_code(settings.get("auto_relist"), default=4)

	flags = listing_flags()
	methods = payment_methods(flags=flags)
	if not methods:
		# Backstop; the config guard refuses this before a client is built. A
		# listing with no payment method is one nobody can buy — it costs a
		# listing fee and produces nothing.
		raise ValueError("no GunBroker payment method is enabled")

	own_condition = _enum_code(serial.get("gb_condition"), default=0)
	condition = gb_condition(item.get("is_used"), is_antique, own_condition)
	if own_condition in (CONDITION_FACTORY_NEW, CONDITION_NEW_OLD_STOCK) and is_antique:
		warnings.append("condition_antique_used")

	payload = {
		"CategoryID": category_id,
		"Title": title,
		"Description": description,
		# is_used is Item-level only — there is no per-serial column for it. A
		# gun's own choice at Send (gb_condition) wins, except for an antique.
		"Condition": condition,
		"CountryCode": (settings.get("country_code") or "US").strip(),
		"PostalCode": (settings.get("postal_code") or "").strip(),
		# One physical gun, one listing. Multi-quantity listings would need a
		# serial per unit at sale time, which this channel does not do.
		"Quantity": 1,
		"ListingDuration": duration,
		**pricing,
		"AutoRelist": auto_relist,
		# Defaulted to ON, and read so that "never saved" still means on. A
		# listing that says no FFL is required is read by buyers as one that can
		# ship to their door — which is precisely right for an antique, and is
		# forced here regardless of the Settings checkbox: a pre-1899 gun needs
		# no licence at the far end, and asking the buyer for one would only
		# route it to a dealer for a transfer that does not exist.
		"IsFFLRequired": False if is_antique else flags["is_ffl_required"],
		"WillShipInternational": flags["will_ship_international"],
		"WhoPaysForShipping": WHO_PAYS_SELLER if free_shipping
			else _enum_code(settings.get("who_pays_shipping"), default=8),
		"ShippingClassesSupported": {"Ground": True},
		"ShippingClassCosts": {"Ground": ship_cost},
		"PaymentMethods": methods,
		"InspectionPeriod": _enum_code(settings.get("inspection_period"), default=1),
		# Also defaulted ON: false here with no explicit list means the account's
		# state restrictions stop applying and a gun ships somewhere it must not.
		"UseDefaultExcludeStates": flags["use_default_exclude_states"],
		# The stable identity of this listing. GunBroker keeps SKU unchanged
		# across its own relists while issuing a new item number each time, and
		# recommends it as the key for inventory sync — so everything that has to
		# find "the listing for this gun" later matches on this string.
		"SKU": listing_sku(serial.get("item_code"), serial_no),
		"SerialNumber": serial_no,
	}
	if not payload["UseDefaultExcludeStates"] and settings.get("exclude_states"):
		payload["ExcludeStates"] = settings.get("exclude_states")
	# Paid promotions go in only for the flags that are on, and the object is
	# left out when none are: GunBroker bills per feature and a POST is the only
	# moment they can be bought. A POS-side relist re-POSTs this payload, so it
	# buys the recorded ones again — the Send dialog pre-ticks them to show it.
	premium = {PREMIUM_FEATURES[key][1]: True
		for key, on in premium_flags(serial).items() if on}
	if premium:
		payload["PremiumFeatures"] = premium
	if not _add_product_codes(payload, item):
		# GunBroker fills Manufacturer / Model / Caliber from its own catalogue
		# by UPC, and the API has no other way to set them, so without one the
		# person who sent the gun adds them on the listing.
		warnings.append("no_upc")

	return {"payload": payload, "warnings": warnings}


def _add_product_codes(payload: dict, item: dict) -> bool:
	"""UPC and manufacturer part number, the keys GunBroker's catalogue fills a
	listing's characteristics from. Blank ones are left out, not sent empty.
	Returns whether a UPC went in."""
	upc = (item.get("upc") or "").strip()
	if upc:
		payload["UPC"] = upc
	mpn = (item.get("mpn") or "").strip()
	if mpn:
		payload["MfgPartNumber"] = mpn
	return bool(upc)


# GunBroker refuses a SKU over 50 characters ("SKU cannot exceed 50 characters");
# the limit is not in its API docs.
SKU_MAX_LEN = 50
# The item-level short form: a prefix of the item code, then ``~`` and this many hex
# digits of the full code's SHA-1, padded out to exactly SKU_MAX_LEN.
_ITEM_SKU_HASH_LEN = 8
_ITEM_SKU_SHORT_RE = re.compile(r"^(.+)~([0-9a-f]{%d})$" % _ITEM_SKU_HASH_LEN)


def listing_sku(item_code: str, serial_no: str) -> str:
	"""``item_code::serial`` — the same SKU shape the web store uses.

	When that is longer than GunBroker accepts, the item code is dropped and the
	SKU is ``::serial``: the serial alone names the gun, and ``orders.split_sku``
	reads the item code back off the Serial No. Only a gun whose full SKU is too
	long gets the short form, so the SKU of every listing already up is unchanged.
	"""
	sku = f"{item_code}::{serial_no}"
	return sku if len(sku) <= SKU_MAX_LEN else f"::{serial_no}"


def item_sku_hash(item_code: str) -> str:
	"""The digest that ties an item-level short SKU to its full item code."""
	return hashlib.sha1(item_code.encode("utf-8")).hexdigest()[:_ITEM_SKU_HASH_LEN]


def short_item_sku_parts(sku: str) -> tuple[str, str] | None:
	"""``(prefix, digest)`` when ``sku`` has the item-level short form, else None."""
	if len(sku) != SKU_MAX_LEN:
		return None
	m = _ITEM_SKU_SHORT_RE.match(sku)
	return (m.group(1), m.group(2)) if m else None


def item_listing_context(item_code: str) -> dict:
	"""The Item row an item-level listing needs, read once.

	The serial-level twin (:func:`listing_context`) reads two rows because a gun
	carries half its own description; a box of ammunition is entirely described
	by its Item, so there is only one.
	"""
	item = frappe.db.get_value(
		"Item", item_code, list(_ITEM_LISTING_FIELDS), as_dict=True) or frappe._dict()
	if item and not item.get("item_code"):
		item["item_code"] = item_code
	return {"item": item}


def build_item_listing_payload(item_code: str, *, quantity: int, settings=None,
		context: dict | None = None) -> dict:
	"""The full ``POST /Items`` payload for one item-level listing.

	The non-firearm half of the channel: ammunition, optics, parts — one listing
	standing for a quantity of interchangeable units, rather than one listing per
	physical gun.

	Three things are decided differently from the firearm payload, and each has a
	reason the caller cannot override:

	* ``IsFFLRequired`` is FALSE, always. Nothing here is a firearm — the guard
	  refuses the listing before this point if it is — so asking the buyer for a
	  licence would route a box of ammunition to a dealer for a transfer that
	  does not exist.
	* ``Quantity`` is the caller's, not 1. GunBroker rejects 0 outright (``The
	  field Quantity must be an integer greater than 0``), so "sold out" is
	  expressed by ending the listing, never by pushing a zero.
	* ``ExcludeStates`` comes from the Item when it names any. Ammunition that
	  cannot lawfully be shipped to a state should not be offered to that
	  state's buyers in the first place; the account-wide default list is the
	  fallback, not the ceiling.

	Args:
	    item_code: The Item to list.
	    quantity: Units to offer. Must be positive.
	    settings: An already-loaded GunBroker Settings (optional).
	    context: An already-loaded :func:`item_listing_context` (optional).

	Returns:
	    ``{"payload": {...}, "warnings": [...]}``. Pictures are fetched
	    separately, as on the firearm side.
	"""
	settings = settings or frappe.get_single("GunBroker Settings")
	ctx = context or item_listing_context(item_code)
	item = ctx["item"]
	warnings: list[str] = []

	quantity = frappe.utils.cint(quantity)
	if quantity <= 0:
		raise ValueError(f"{item_code!r} has nothing to list (quantity {quantity})")

	title, title_warnings = gb_title(item.get("item_name") or item_code)
	warnings.extend(title_warnings)

	description, desc_warnings = gb_description(
		serial_description="",
		item_description=item.get("description") or "",
		footer=settings.get("description_footer") or "")
	warnings.extend(desc_warnings)

	category_id = resolve_item_category(
		item_override=item.get("gb_category_id"),
		item_group=item.get("item_group") or "",
		category_map=settings.get("category_map") or [])
	if category_id <= 0:
		raise ValueError(f"No GunBroker category resolves for {item_code!r}")

	# The default is read straight off Settings rather than through
	# ship_cost_for(): that helper picks between the two firearm rates by type,
	# and nothing here has a type.
	ship_cost = listing_ship_cost(item.get("gb_ship_cost"), settings.get("accessory_ship_cost"))
	if ship_cost <= 0:
		raise ValueError(f"No GunBroker accessory shipping cost configured for {item_code!r}")

	price = float(frappe.utils.flt(item.get("standard_rate") or 0))
	if price <= 0:
		raise ValueError(f"{item_code!r} has no sell price")

	payload, _has_upc = _quantity_listing_payload(
		item_code, item, settings, title=title, description=description,
		category_id=category_id, condition=gb_condition(
			item.get("is_used"), item.get("is_antique")),
		quantity=quantity, price=price, ship_cost=ship_cost, ffl_required=False)

	return {"payload": payload, "warnings": warnings}


def _quantity_listing_payload(item_code: str, item: dict, settings, *, title: str,
		description: str, category_id: int, condition: int, quantity: int, price: float,
		ship_cost: float, ffl_required: bool) -> dict:
	"""The ``POST /Items`` body of a listing that stands for a QUANTITY of one Item.

	Shared by the two kinds of such listing — ammunition, optics and parts
	(:func:`build_item_listing_payload`) and a uniform-price gun model
	(:func:`build_model_listing_payload`) — which differ only in the figures
	passed in: a bare-SKU quantity listing has no serial number, one fixed price,
	one shipping rate and the Item's own state restrictions whichever it is.
	"""
	flags = listing_flags()
	methods = payment_methods(flags=flags)
	if not methods:
		raise ValueError("no GunBroker payment method is enabled")

	payload = {
		"CategoryID": category_id,
		"Title": title,
		"Description": description,
		"Condition": condition,
		"CountryCode": (settings.get("country_code") or "US").strip(),
		"PostalCode": (settings.get("postal_code") or "").strip(),
		"Quantity": quantity,
		"ListingDuration": frappe.utils.cint(settings.get("listing_duration") or 30),
		"FixedPrice": price,
		"AutoRelist": _enum_code(settings.get("auto_relist"), default=4),
		"IsFFLRequired": ffl_required,
		"WillShipInternational": flags["will_ship_international"],
		"WhoPaysForShipping": _enum_code(settings.get("who_pays_shipping"), default=8),
		"ShippingClassesSupported": {"Ground": True},
		"ShippingClassCosts": {"Ground": ship_cost},
		"PaymentMethods": methods,
		"InspectionPeriod": _enum_code(settings.get("inspection_period"), default=1),
		"UseDefaultExcludeStates": flags["use_default_exclude_states"],
		"SKU": item_listing_sku(item_code),
	}
	states = (item.get("restricted_states") or "").strip()
	if states:
		# The Item's own list REPLACES the account default rather than adding to
		# it, because that is the only shape GunBroker accepts: ExcludeStates is
		# read solely when UseDefaultExcludeStates is false.
		payload["UseDefaultExcludeStates"] = False
		payload["ExcludeStates"] = states
	elif not payload["UseDefaultExcludeStates"] and settings.get("exclude_states"):
		payload["ExcludeStates"] = settings.get("exclude_states")
	return payload, _add_product_codes(payload, item)


def model_category(item: dict, settings) -> int:
	"""The GunBroker category a uniform-price MODEL lists under, or 0 for none.

	The Item's own override wins; otherwise the firearm-type row of the category
	map, with the model's year deciding a Curios & Relics row and an antique taking
	only the antique one — the same rules as a single gun's
	(:func:`serial_category`), asked of the Item alone.
	"""
	return serial_category({}, item, settings)


def model_category_ambiguous(item: dict, settings) -> bool:
	"""True when the category depends on a year the Item does not carry.

	A type the map routes to Curios & Relics by age lists a 1943 gun and a 1985 gun
	in different categories, and a model with no year recorded and no category of
	its own picked cannot be filed under either: guessing puts a possible relic in
	the modern category (invisible to the buyers for it, or a policy problem) or
	the reverse. An antique has its own row and is never ambiguous.
	"""
	if item.get("gb_category_id") or item.get("is_antique"):
		return False
	from ffl_core.firearm import sane_year_made

	return bool(
		routes_by_age(settings.get("category_map") or [], str(item.get("firearm_type") or ""))
		and not sane_year_made(item.get("year_made")))


def model_price(item: dict) -> float:
	"""The fixed price a model listing should carry today: the web store's own
	price, promotion window included (:func:`ffl_core.firearm.current_price`)."""
	return float(current_price(
		item.get("standard_rate"), item.get("sale_price"),
		item.get("sale_from"), item.get("sale_to")))


def model_ship_cost(item: dict, settings) -> float:
	"""The buyer-paid Ground rate for a model: the Item's own, else the long gun /
	handgun default for its firearm type."""
	return listing_ship_cost(item.get("gb_ship_cost"), ship_cost_for(
		str(item.get("firearm_type") or ""),
		long_gun_cost=settings.get("long_gun_ship_cost"),
		handgun_cost=settings.get("handgun_ship_cost")))


def build_model_listing_payload(item_code: str, *, quantity: int, premium: bool = False,
		settings=None, context: dict | None = None) -> dict:
	"""The full ``POST /Items`` payload for a uniform-price firearm MODEL.

	One listing standing for N interchangeable guns: a quantity, no serial number,
	the bare item code as its SKU (the order side reads "no ``::serial``" as "a unit
	of a model" and files a claim), and ``IsFFLRequired`` from Settings — forced
	false for an antique, exactly as a single antique gun's is.

	Args:
	    item_code: The Item to list.
	    quantity: Units to offer. Must be positive (GunBroker rejects 0).
	    premium: Whether to buy the paid promotions recorded on the Item. True
	        only for a person pressing Send: GunBroker bills each one and a POST is
	        the only call that can set them, so the machine paths that re-list a
	        sold-out model on restock (``relist_on_restock``, the sweep) pass False
	        and never spend money the operator did not just agree to.
	    settings: An already-loaded GunBroker Settings (optional).
	    context: An already-loaded :func:`item_listing_context` (optional).

	Returns:
	    ``{"payload": {...}, "warnings": [...]}``. Pictures are fetched separately.
	"""
	settings = settings or frappe.get_single("GunBroker Settings")
	ctx = context or item_listing_context(item_code)
	item = ctx["item"]
	warnings: list[str] = []

	quantity = frappe.utils.cint(quantity)
	if quantity <= 0:
		raise ValueError(f"{item_code!r} has nothing to list (quantity {quantity})")

	title, title_warnings = gb_title(item.get("item_name") or item_code)
	warnings.extend(title_warnings)
	description, desc_warnings = gb_description(
		serial_description="",
		item_description=item.get("description") or "",
		footer=settings.get("description_footer") or "")
	warnings.extend(desc_warnings)

	category_id = model_category(item, settings)
	if category_id <= 0:
		raise ValueError(f"No GunBroker category resolves for {item_code!r}")
	if model_category_ambiguous(item, settings):
		raise ValueError(f"{item_code!r} has no year and no category of its own")
	ship_cost = model_ship_cost(item, settings)
	if ship_cost <= 0:
		raise ValueError(f"No GunBroker shipping cost configured for {item_code!r}")
	price = model_price(item)
	if price <= 0:
		raise ValueError(f"{item_code!r} has no sell price")

	is_antique = bool(item.get("is_antique"))
	flags = listing_flags()
	payload, has_upc = _quantity_listing_payload(
		item_code, item, settings, title=title, description=description,
		category_id=category_id,
		# The per-gun condition choice is a Serial No field; a model follows its
		# Item (used or not, antique always Used).
		condition=gb_condition(item.get("is_used"), is_antique),
		quantity=quantity, price=price, ship_cost=ship_cost,
		ffl_required=False if is_antique else flags["is_ffl_required"])
	if premium:
		promotions = {PREMIUM_FEATURES[key][1]: True
			for key, on in premium_flags(item).items() if on}
		if promotions:
			payload["PremiumFeatures"] = promotions
	if not has_upc:
		warnings.append("no_upc")
	return {"payload": payload, "warnings": warnings}


def item_listing_sku(item_code: str) -> str:
	"""The bare ``item_code`` — an item-level listing stands for no single unit.

	``split_sku`` already reads a SKU with no ``::`` as (item_code, None), so the
	order side tells the two listing kinds apart by whether a serial came back.

	An item code longer than GunBroker accepts is cut to a prefix plus ``~`` and a
	digest of the whole code (exactly :data:`SKU_MAX_LEN` long); ``orders.split_sku``
	finds the Item by prefix and digest. Shorter codes are unchanged.
	"""
	item_code = (item_code or "").strip()
	if len(item_code) <= SKU_MAX_LEN:
		return item_code
	digest = item_sku_hash(item_code)
	return f"{item_code[:SKU_MAX_LEN - len(digest) - 1]}~{digest}"


def _enum_code(value: Any, *, default: int) -> int:
	"""The leading integer of a Settings Select like ``"4 Relist Fixed Price"``.

	The Select stores code-plus-label so the form is readable; GunBroker wants the
	code. A value that has lost its numeric prefix falls back to the default
	rather than sending 0, which GunBroker would reject as a validation error
	several seconds and one confusing message later.
	"""
	match = re.match(r"\s*(\d+)", str(value or ""))
	return int(match.group(1)) if match else default


def picture_parts(serial_no: str, *, settings=None, context: dict | None = None) -> dict:
	"""The photo bytes for one listing, in publish order.

	Reads the gun's own gallery, falling back to the model Item's, resolves each
	path to bytes, and returns them as multipart-ready tuples.

	Bytes, not file handles, deliberately: an expired access token forces the
	whole request to be replayed, and a consumed stream would replay as zero
	bytes — a listing that posted with no photos and no error.

	Returns:
	    ``{"parts": [(filename, bytes, content_type), ...], "warnings": [...]}``
	    — ``parts`` in publish order, the primary photo first (GunBroker makes the
	    first uploaded picture the thumbnail).

	An empty ``parts`` is a normal outcome here, not an error: GunBroker itself
	is happy to list a gun with no photos at all. Refusing that is OUR rule, and
	it is applied by the caller.
	"""
	paths, warnings = _resolve_gallery(
		serial_no, settings=settings, context=context)
	return _picture_parts(paths, warnings, settings)


def candidate_image_paths(serial_no: str, *, settings=None,
		context: dict | None = None) -> list[str]:
	"""The image paths this listing WOULD publish, without reading any of them.

	Split out from :func:`picture_parts` so the "has this gun got photos?" guard
	can be answered from the database alone. Resolving the bytes means reading
	files off disk and, for a gallery row holding an absolute URL, an outbound
	HTTP request — work that a gun which is going to be refused for being
	already listed should never pay for, and which would break the promise that
	a guard refusal costs no network call.

	Paths whose extension GunBroker will not accept are dropped here too: that
	is a string test, and it means an all-unsupported gallery reads as "no
	photos" at the guard rather than after the fact.

	Args:
	    serial_no: The Serial No to publish.
	    settings: An already-loaded GunBroker Settings doc (optional).
	    context: An already-loaded :func:`listing_context` (optional).

	Returns:
	    Ordered paths, primary first, capped at the configured per-listing limit.
	"""
	return _resolve_gallery(serial_no, settings=settings, context=context)[0]


def _resolve_gallery(serial_no: str, *, settings=None,
		context: dict | None = None) -> tuple[list[str], list[str]]:
	"""``(paths, warnings)`` — the shared half of the two entry points above.

	One implementation so the guard and the upload cannot disagree about which
	photos count, and so the operator hears about a skipped format once, from
	whichever of them ran.
	"""
	settings = settings or frappe.get_single("GunBroker Settings")
	ctx = context or listing_context(serial_no)
	serial, item = ctx["serial"], ctx["item"]
	warnings: list[str] = []

	paths = ordered_gallery_paths(
		_gallery_rows("Serial No", serial_no), serial.get("image"))
	if not paths and serial.get("item_code"):
		paths = ordered_gallery_paths(
			_gallery_rows("Item", serial.get("item_code")), item.get("image"))

	return _usable_gallery(paths, settings, warnings)


def _usable_gallery(paths: list[str], settings, warnings: list[str]
		) -> tuple[list[str], list[str]]:
	"""Drop what GunBroker will not take, then cap at the configured limit.

	Shared by both gallery resolvers so a per-gun and an item-level listing
	cannot disagree about which photos count or how many of them go up.
	"""
	usable = [p for p in paths if _image_content_type(p)]
	if len(usable) < len(paths):
		warnings.append("image_format_skipped")

	limit = frappe.utils.cint(settings.get("max_pictures") or 16)
	if len(usable) > limit:
		warnings.append("pictures_truncated")
		usable = usable[:limit]
	return usable, warnings


def _resolve_item_gallery(item_code: str, *, settings=None,
		context: dict | None = None) -> tuple[list[str], list[str]]:
	"""``(paths, warnings)`` for an item-level listing — the Item's own photos.

	No serial fallback to consider: the listing stands for the model, so the
	model's gallery is the whole of it.
	"""
	settings = settings or frappe.get_single("GunBroker Settings")
	ctx = context or item_listing_context(item_code)
	item = ctx["item"]
	paths = ordered_gallery_paths(_gallery_rows("Item", item_code), item.get("image"))
	return _usable_gallery(paths, settings, [])


def item_picture_parts(item_code: str, *, settings=None,
		context: dict | None = None) -> dict:
	"""The photo bytes for one item-level listing. Twin of :func:`picture_parts`."""
	paths, warnings = _resolve_item_gallery(item_code, settings=settings, context=context)
	return _picture_parts(paths, warnings, settings)


def _picture_parts(paths: list[str], warnings: list[str], settings) -> dict:
	"""Read each path, watermark it when the store has set one up, and collect
	the multipart tuples; shared by both listing kinds."""
	mark = watermark_spec(settings or frappe.get_single(SETTINGS_DOCTYPE))
	parts: list[tuple[str, bytes, str]] = []
	for path in paths:
		part, reason = _image_part(path)
		if part and mark:
			part, failed = _watermarked(part, mark)
			if failed:
				warnings.append("watermark_failed")
		if part:
			parts.append(part)
		elif reason:
			warnings.append(reason)
	return {"parts": parts, "warnings": warnings}


def watermark_spec(settings) -> dict | None:
	"""The configured watermark, or None when the store has none.

	A logo that cannot be read is logged and left out rather than failing the
	listing: the photos still go up, with the text alone if there is any.
	"""
	logo_path = str(settings.get("watermark_image") or "").strip()
	text = str(settings.get("watermark_text") or "").strip()
	logo = b""
	if logo_path:
		try:
			logo = _local_file_bytes(logo_path) if logo_path.startswith(("/files/", "/private/files/")) \
				else _remote_image_bytes(logo_path)
		except Exception:  # noqa: BLE001 — a missing logo must not stop a listing
			frappe.log_error(title="GunBroker: could not read the watermark logo",
				message=f"{logo_path}\n{frappe.get_traceback()}")
	if not logo and not text:
		return None
	return {"logo": logo, "text": text}


def _watermarked(part: tuple[str, bytes, str], mark: dict) -> tuple[tuple[str, bytes, str], bool]:
	"""``(part, failed)``: the photo with the watermark, as a JPEG; the original
	photo and ``failed`` when it could not be drawn (a photo is never dropped
	over its watermark)."""
	filename, content, _content_type = part
	try:
		content = apply_watermark(content, mark["logo"], mark["text"])
	except Exception:  # noqa: BLE001
		frappe.log_error(title="GunBroker: could not watermark a photo",
			message=f"{filename}\n{frappe.get_traceback()}")
		return part, True
	return (filename.rsplit(".", 1)[0] + ".jpg", content, "image/jpeg"), False


def apply_watermark(content: bytes, logo: bytes, text: str) -> bytes:
	"""Draw the watermark panel onto one photo and return it as a JPEG.

	The logo is read as dark ink on a light ground (a black-on-white logo file):
	its darkness becomes the ink's opacity, so the white around it disappears.
	The photo is turned upright first, because the EXIF orientation it relied on
	is not carried over to the re-encoded file.
	"""
	from PIL import Image, ImageDraw, ImageFont, ImageOps

	photo = Image.open(io.BytesIO(content))
	img = ImageOps.exif_transpose(photo).convert("RGBA")
	width, height = img.size
	box_w = int(width * WATERMARK_WIDTH)
	pad = max(6, box_w // 18)
	inner = box_w - 2 * pad
	ink = None
	if logo:
		mark = Image.open(io.BytesIO(logo))
		if mark.mode in ("RGBA", "LA", "PA") or "transparency" in mark.info:
			# A transparent ground has no colour of its own (often black): lay the
			# logo on white first, or the whole box would read as ink.
			ground = Image.new("RGBA", mark.size, (255, 255, 255, 255))
			mark = Image.alpha_composite(ground, mark.convert("RGBA"))
		mark = mark.convert("L")
		mark = mark.resize((inner, max(1, round(mark.height * inner / mark.width))), Image.LANCZOS)
		ink = ImageOps.invert(mark).point(lambda v: int(v * WATERMARK_INK_ALPHA))
	font = ImageFont.load_default(size=max(10, inner // 11))
	left, top, right, bottom = font.getbbox(text) if text else (0, 0, 0, 0)
	logo_h = ink.height if ink else 0
	text_h = bottom - top
	gap = pad // 2 if ink and text else 0
	box_h = pad + logo_h + gap + text_h + pad
	x0, y0 = width - box_w - pad, height - box_h - pad

	overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
	draw = ImageDraw.Draw(overlay)
	draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=pad,
		fill=(255, 255, 255, int(255 * WATERMARK_PANEL_ALPHA)))
	if ink:
		overlay.paste((0, 0, 0, 255), (x0 + pad, y0 + pad), ink)
	if text:
		draw.text((x0 + (box_w - (right - left)) // 2, y0 + pad + logo_h + gap - top), text,
			font=font, fill=(0, 0, 0, int(255 * WATERMARK_INK_ALPHA)))
	out = io.BytesIO()
	Image.alpha_composite(img, overlay).convert("RGB").save(out, "JPEG", quality=90,
		icc_profile=photo.info.get("icc_profile"))
	return out.getvalue()


def item_candidate_image_paths(item_code: str, *, settings=None,
		context: dict | None = None) -> list[str]:
	"""What an item-level listing WOULD publish, read from the database alone.

	Twin of :func:`candidate_image_paths`; same reason for existing — the "has
	it got photos?" guard must not read files or reach the network.
	"""
	return _resolve_item_gallery(item_code, settings=settings, context=context)[0]


def _image_content_type(path: str) -> str:
	"""The MIME type GunBroker will accept for this path, or "".

	The filename is the only format signal available: what GunBroker hands back
	on a later read has no extension at all, so an unrecognised one cannot be
	resolved by asking.
	"""
	filename = str(path or "").strip().split("?")[0].rstrip("/").rsplit("/", 1)[-1]
	if "." not in filename:
		return ""
	return ALLOWED_IMAGE_EXTS.get("." + filename.rsplit(".", 1)[-1].lower(), "")


def _gallery_rows(doctype: str, name: str) -> list[dict]:
	"""The image_gallery child rows for one Serial No / Item, in table order.

	Read straight off the child table rather than through ``get_doc``: this runs
	on a push path that has already read the parent's scalar fields, and loading
	the whole document again to reach one table would be the expensive way to ask
	a cheap question.
	"""
	if not name:
		return []
	return frappe.get_all(
		"Item Image Gallery",
		filters={"parenttype": doctype, "parent": name},
		# Ordered by idx — the row order the operator arranged in the grid.
		# `sort_order` exists on the child table but nothing has ever written it,
		# so selecting it would only suggest it was consulted.
		fields=["image", "is_primary"],
		order_by="idx asc") or []


def _image_part(path: str) -> tuple[tuple[str, bytes, str] | None, str]:
	"""One gallery path as a multipart tuple, or (None, warning-reason)."""
	src = str(path or "").strip()
	if not src:
		return None, ""

	filename = src.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "image"
	content_type = _image_content_type(src)
	if not content_type:
		return None, "image_format_skipped"

	try:
		if src.startswith("/files/") or src.startswith("/private/files/"):
			content = _local_file_bytes(src)
		elif src.startswith("http://") or src.startswith("https://"):
			content = _remote_image_bytes(src)
		else:
			return None, "image_unreadable"
	except Exception:  # noqa: BLE001 — one bad photo must not fail the listing
		frappe.log_error(
			title="GunBroker: could not read listing image",
			message=f"{src}\n{frappe.get_traceback()}")
		return None, "image_unreadable"

	if not content:
		return None, "image_unreadable"
	return (filename, content, content_type), ""


def _local_file_bytes(file_url: str) -> bytes:
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		return b""
	return frappe.get_doc("File", name).get_content()


def _remote_image_bytes(url: str) -> bytes:
	import requests

	with requests.get(url, timeout=15, stream=True) as response:
		response.raise_for_status()
		content = response.raw.read(MAX_REMOTE_IMAGE_BYTES + 1, decode_content=True)
	if len(content) > MAX_REMOTE_IMAGE_BYTES:
		# Truncating would upload a corrupt file that GunBroker either rejects or,
		# worse, accepts as a broken image on a live listing.
		raise ValueError(f"image at {url} exceeds {MAX_REMOTE_IMAGE_BYTES} bytes")
	return content
