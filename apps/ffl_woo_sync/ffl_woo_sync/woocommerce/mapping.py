"""WooCommerce product payload mapping.

Two layers
----------
**Pure builders** — :func:`build_simple_product_payload` / :func:`build_serial_product_payload`
    Accept plain Python values (no Frappe DB calls, no HTTP).  Fully
    unit-testable without a running site or WooCommerce store.

**Thin loaders** — :func:`item_to_product_payload` / :func:`serial_to_product_payload` / :func:`stock_for_item`
    Fetch Frappe documents / DB rows, then delegate to the pure builders.
    Reuse ffl_core helpers for pricing and stock so that logic stays in one
    canonical place.  One write lives here: before building, the two product
    loaders make the document's private photos public (a File move plus a
    rewrite of the ``image`` / gallery references — see
    :func:`_publicize_listing_images`); everything else is read-only.

Three WooCommerce product shapes
---------------------------------
1. **Non-firearm item**       → one simple product per Item (SKU = ``item_code``)
2. **Uniform-price firearm**  → one simple product per Item (SKU = ``item_code``)
3. **Per-serial firearm**     → one simple product per Serial No
                                (SKU = ``"{item_code}::{serial_no}"``, stock = 1 or 0)

Use :func:`item_to_product_payload` for shapes 1 and 2.
Use :func:`serial_to_product_payload` for shape 3.

``attr_ids``
------------
The optional ``attr_ids: dict[str, int] | None`` parameter threads global
WooCommerce attribute taxonomy IDs (``{"pa_caliber": 5, "pa_condition": 7}``)
into the builders so that pushed products link to the real WC taxonomies rather
than creating per-product custom attributes.  Callers in :mod:`.sync` fetch this
map once via ``GET /wc/v3/products/attributes`` and thread it through.  If
``None`` is passed the builders use ``id=0`` (creates per-product attributes),
which is acceptable for local testing.
"""
from __future__ import annotations

import re

import frappe

from ffl_core.firearm import (
    AMMUNITION,
    AMMO_LEAVES,
    _is_firearm_row,
    is_per_serial_priced,
    serial_sale_fields,  # same two-level rule, for the promotional price
    serial_sell_price,  # public alias for the uniform/per-serial price rule
)
from ffl_core.api.item_admin import _get_item_stock as get_item_stock
from ffl_core.custody import custody_warehouse_qty
from ffl_core.reservations import (
    CHANNEL_WOO,
    channel_reserve_columns,
    committed_units,
    other_channel_reservation,
    uniform_free_serials,
)
from ffl_core.utils import set_file_privacy

# All item_group values that classify an item as ammunition.
_AMMO_GROUPS: frozenset[str] = frozenset({AMMUNITION, *AMMO_LEAVES})


# ===========================================================================
# Pure helpers (no IO — safe to call without a Frappe site context)
# ===========================================================================

# Mirrors the ``condition_grade`` Select options (Item + Serial No custom fields).
# Keyed by the whitespace-collapsed casefolded form so hand-typed / imported
# "very  good" / "GOOD" / " Fair " rows still resolve to the canonical term.
_CONDITION_TIERS: dict[str, str] = {
    t.casefold(): t for t in ("New", "Excellent", "Very Good", "Good", "Fair")
}


def _pa_condition(
    condition_grade: str | None, is_used: bool, is_antique: bool = False,
) -> str:
    """Resolve the WooCommerce ``pa_condition`` term name; ``''`` = publish none.

    ``is_used`` decides whether a grade is believed at all:

    * not used → ``'New'``, whatever the grade says. ``condition_grade`` is not
      evidence of anything on a new gun: it is a Select that used to default to
      "Very Good" on every Item and Serial No (prod: 39 of 40 firearm Items
      created since 08-01 with ``is_used=0`` carry it, NIB suppressors included),
      so a grade that merely exists says nothing about whether staff chose it.
    * used + a valid 5-tier grade (case / spacing tolerant) → that tier.
    * used + blank or unrecognised grade → ``''``: no condition is published
      until staff grade the gun — better blank than a false "New".

    A pre-1899 antique (``is_antique``) counts as used: it is never new.
    """
    if not (is_used or is_antique):
        return "New"
    return _CONDITION_TIERS.get(" ".join((condition_grade or "").split()).casefold(), "")


# ---------------------------------------------------------------------------
# Caliber display normalization (layer B).
#
# The caliber map is frozen.  ``_CALIBER_CANONICAL`` maps
# a free-text caliber to its industry retail "simple name" used verbatim as the
# WooCommerce ``pa_caliber`` term (the storefront theme echoes the term name directly).
#
# ⚠ DISPLAY LAYER ONLY.  This is deliberately SEPARATE from the FastBound
# aggregation key table (``fastbound/normalize.py`` ``canon_caliber`` /
# ``_CALIBER``): different purpose, do NOT merge.  It must NEVER be applied to
# the FastBound push path — the A&D book records caliber exactly as marked on
# the gun (see ``fastbound/client.py``; guarded by test_caliber_fb_isolation).
#
# Keys below are written in natural form and normalized once at import via
# ``_norm_caliber_key`` so authoring stays readable and lookups are consistent.
# Item-dependent bare tokens (``.30`` / ``30`` / ``7.62``) are intentionally
# absent — string context can't disambiguate them; they are resolved on the
# POS Item / Serial No side and reach here already specific.
# ---------------------------------------------------------------------------

def _norm_caliber_key(raw: str | None) -> str:
    """Normalization key: casefold + strip + internal-whitespace collapse."""
    return " ".join((raw or "").split()).casefold()


_CALIBER_CANONICAL: dict[str, str] = {
    _norm_caliber_key(_raw): _canon
    for _raw, _canon in {
        # ── Handgun cartridges ──
        "9x19": "9mm Luger", "9MM": "9mm Luger", "9MM PARA": "9mm Luger",
        "9 Para": "9mm Luger", "9 Luger": "9mm Luger", "9x19mm": "9mm Luger",
        "9mmx19": "9mm Luger", "9": "9mm Luger",
        "380ACP": ".380 ACP", "9mm Kurz": ".380 ACP",
        "9x18": "9x18 Makarov",
        "45ACP": ".45 ACP", ".45ACP": ".45 ACP", ".45 ACP/Auto": ".45 ACP",
        ".45 Colt (.45 Long Colt)": ".45 Colt", "45 Colt (Long Colt)": ".45 Colt",
        "357Mag": ".357 Magnum", ".357 Mag": ".357 Magnum",
        ".357": ".357 Magnum", "357": ".357 Magnum",
        "44 Magnum": ".44 Magnum", ".44 Mag": ".44 Magnum", "44MAG": ".44 Magnum",
        "38 S&W SPECIAL": ".38 Special",
        ".38 S&W": ".38 S&W", "38": ".38 S&W",
        "32 ACP": ".32 ACP",
        "7.65 mm": "7.65mm Browning",
        ".25 ACP (.25 Auto /  6.35mm)": ".25 ACP",
        "10mm Auto": "10mm Auto",
        ".500 S&W Mag": ".500 S&W Magnum",
        "7.65x21mm": ".30 Luger",
        "7.62x25 mm": "7.62x25mm Tokarev",
        "7.63x25 Mauser": "7.63x25 Mauser",
        "7.62x38R": "7.62x38R Nagant",
        "8mm nambu": "8mm Nambu",
        "455": ".455 Webley",
        # ── Rifle / rimfire cartridges ──
        ".22 LR": ".22 LR", "22 LR": ".22 LR", "22": ".22 LR", "22 long rifle": ".22 LR",
        "223": ".223 Remington", ".223": ".223 Remington", ".223 Remington": ".223 Remington",
        "5.56 NATO": "5.56 NATO", "5.56mm": "5.56 NATO", "5.56x45": "5.56 NATO",
        "5.56mmx45": "5.56 NATO", "5.56mm/223": "5.56 NATO",
        ".30-06": ".30-06 Springfield", "30-06": ".30-06 Springfield",
        ".30 Carbine": ".30 Carbine", "30 CARBINE": ".30 Carbine",
        ".30-30 Win": ".30-30 Win",
        ".45-70": ".45-70 Government",
        "243": ".243 Win",
        ".308": ".308 Win", ".308/7.62x51mm (.308 Winchester)": ".308 Win",
        "7.62 NATO": ".308 Win", "7.62.N": ".308 Win",
        ".300 AAC Blackout (7.62x35mm)": ".300 AAC Blackout", ".300BLK": ".300 AAC Blackout",
        ".338 WM": ".338 Win Mag",
        "338 LAP MAG": ".338 Lapua Magnum",
        "7mm Rem": "7mm Rem",
        "7mm Mauser (7x57 Mauser /  .275 Rigby)": "7x57mm Mauser",
        "7.62X39": "7.62x39mm", "7.62X39mm": "7.62x39mm",
        "7.62x54R": "7.62x54R", "7.62X53R": "7.62x54R",
        ".303 British": ".303 British", "303 BRITISH": ".303 British",
        "8MM": "8mm Mauser", "8MM MAUSER": "8mm Mauser", "7.92 mm": "8mm Mauser",
        "8mm Mauser (7.92x57mm /  8x57 IS/JS)": "8mm Mauser",
        "8x56mm": "8x56R",
        "7.5 Swiss": "7.5x55mm Swiss", "7.5x55 Swiss": "7.5x55mm Swiss",
        "7.5x54mm French": "7.5x54mm French",
        "6.5 CM": "6.5 Creedmoor",
        "6.5x55": "6.5x55mm Swedish",
        "6.5x50mm Japanese (6.5x50 Arisaka)": "6.5x50mm Arisaka",
        "6.5x50mm Japanese (6.5×50 Arisaka)": "6.5x50mm Arisaka",
        "6.5x52mm Carcano (6.5x52mm Mannlicher-Carcano)": "6.5x52mm Carcano",
        "7.7 Japanese (7.7x58mm)": "7.7 Japanese",
        # ── Shotgun ──
        "12GA": "12 Gauge", "12 Gauge": "12 Gauge",
        "20": "20 Gauge",
        "10GA": "10 Gauge",
    }.items()
}


def _canonical_caliber(raw: str) -> str:
    """Free-text caliber → canonical retail simple name for WooCommerce display.

    A miss returns the input verbatim (never invent a mapping).  Pure function,
    no IO.  DISPLAY LAYER ONLY — must not touch the FastBound as-marked push
    path (see the module note above ``_CALIBER_CANONICAL``).
    """
    if not raw:
        return raw
    return _CALIBER_CANONICAL.get(_norm_caliber_key(raw), raw)


# ---------------------------------------------------------------------------
# Model display normalization (layer B, same shape as the caliber table above).
#
# One firearm model reaches this boundary spelled several ways (the POS records
# what the intake operator typed: "M1", "U.S. RIFLE M1", "U.S. Rifle M1"), and
# WooCommerce creates one ``pa_model`` term per distinct string — so the shop's
# Model filter grew one checkbox per spelling for the same gun.  This table
# folds the known spellings onto the retail display name used verbatim as the
# term (the storefront theme echoes the term name directly).
#
# ⚠ DISPLAY LAYER ONLY — same separation as ``_CALIBER_CANONICAL``: this must
# never reach the FastBound push path (the A&D book records the model as marked
# on the gun).  It is deliberately NOT the FastBound table
# (``ffl_integrations.fastbound.normalize`` ``canon_model`` / ``_MODEL``), which
# emits squashed *aggregation keys* ("M1CARBINE") that group bound-book imports
# into Items — not names a shopper reads.  Reusing it is also barred outright:
# this app must not import ``ffl_integrations`` (house rule, see
# hooks.py:154-156, tasks.py:739, webhook.py:805).
#
# So the same equivalences live in two tables, and the duplicated-authority
# rule applies: **edit both or none.**  Teaching only one side a new spelling
# means the two layers disagree about what is the same gun — pinned by
# ``ModelTableAgreement`` in test_caliber_fb_isolation (same pattern as
# ``TestDropshipStatusAgreement`` for the dropship status set).
#
# ⚠ Lookups are on the WHOLE value, never a substring: "M1" folds, but
# "M1911A1", "M16 A2", "M11-A1" and "AUG A3/M1" are untouched.  A wrong entry
# silently merges two different guns on the shop, so add only true
# equivalences — and keep the M1 Garand ("M1", "U.S. Rifle M1") apart from the
# M1 Carbine ("U.S. Carbine M1"), which is a different firearm.
#
# NOT mapped on purpose: bare "U.S. Rifle" (Item ``M1 D Day``).  The phrase is
# equally the M1903's service name, so string context cannot disambiguate it —
# it is resolved on the POS side, like the bare caliber tokens above.
#
# ``_MODEL_NEEDS_CORROBORATION`` is the escape hatch for a key that IS worth
# mapping but is not safe on the model string alone.  Bare "M1" is the M1 Garand
# for every rifle in the book except one: an IBM M1 Carbine whose bound-book
# model really is "M1" (as-marked — the A&D record is right and must not be
# edited to suit the shop).  The rest of the same record settles it, so the
# display layer reads it rather than folding blind.  This is the layer doing its
# job: the book records what is stamped on the gun, the shop shows the name a
# buyer searches.
#
# ⚠ Corroboration reads THREE fields, not one, and that is deliberate.  Caliber
# alone is too thin: the carbine's own Item carries eight serials of which seven
# record the caliber as a bare ".30" — the same bare token ~40 M1 Garands use,
# so ".30" can never be a carbine signal.  Only that one serial happens to spell
# it ".30 Carbine", i.e. the discriminator would be the minority spelling on the
# very Item it has to protect, and a routine caliber tidy-up would silently turn
# a carbine into a Garand on the shop.  Item code and item name are independent
# of that tidy-up, so a single field going quiet is not enough to break it.
# ---------------------------------------------------------------------------

def _norm_model_key(raw: str | None) -> str:
    """Normalization key for the model table: upper-case, letters+digits only.

    Deliberately STRONGER than ``_norm_caliber_key``, which keeps punctuation
    because punctuation carries meaning in a caliber (".38" and "38" are listed
    separately on purpose).  A model name's punctuation is pure typing noise and
    the POS proves it: the same store recorded one carbine as "U.S. CARBINE M1"
    and another as "U.S.CARBINE M1".  Folding it here means a new spacing/dot
    variant of a mapped model cannot silently grow a second shop filter entry.
    """
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


_MODEL_CANONICAL: dict[str, str] = {
    _norm_model_key(_raw): _canon
    for _raw, _canon in {
        # ── M1 Garand ── one "M1 Garand" checkbox for every spelling.
        "M1": "M1 Garand",
        "U.S. Rifle M1": "M1 Garand",
    }.items()
}

# Every canonical display name, reachable from its own spelling variants.
#
# The display name has to be a fixed point: once a shop term is called
# "M1 Garand", a record whose model reads "m1 garand" or "M1-Garand" must reach
# the SAME term — WooCommerce matches attribute options by exact term *name*
# (get_term_by('name'), then wp_insert_term on a miss), so a case or spacing
# variant would mint a second term and re-open the very split this table closes.
#
# DERIVED from the table rather than written as identity rows, deliberately:
# an identity row is an entry the FastBound alias table would then have to
# agree with (see ModelTableAgreement), which would drag a retail-display
# convenience into the bound-book layer for no compliance reason.
_MODEL_DISPLAY_FIXED_POINTS: dict[str, str] = {
    _norm_model_key(_display): _display for _display in _MODEL_CANONICAL.values()
}


# Calibers that mean "M1 Carbine".  Kept here rather than added to
# ``_CALIBER_CANONICAL`` because that table is frozen authority (the caliber map); this
# is a local recognition set, not a new display mapping.  ".30 Carbine" is the
# canonical output of that table (so "30 CARBINE" arrives here already folded);
# 7.62x33 is the same cartridge's metric name and reaches us unmapped.
_CARBINE_CALIBERS: frozenset[str] = frozenset(
    _norm_caliber_key(_c) for _c in (".30 Carbine", "7.62x33", "7.62x33mm")
)


def _looks_like_a_carbine(caliber: str, item_code: str) -> bool:
    """True when the record says "carbine" in a field that would know.

    Two signals, because the caliber alone can go quiet: seven of the eight
    serials on the carbine's Item record the bare ".30" that ~40 M1 Garands
    also use, so a routine caliber tidy-up would erase that signal.  The item
    code survives it — it is the Item's identity ("M1CARBINE-30" vs "M1-30"),
    not free text.

    ⚠ Item *name* is deliberately NOT a signal.  On the per-serial path it is
    the per-gun marketing title an operator writes ("Springfield Armory M1
    Garand .30-06 — 1945, All-SA, Excellent"), so it swings both ways: a Tanker
    Garand described as "carbine-length" would stop folding and mint a second
    term, silently, on a copy edit.
    """
    canon = _canonical_caliber((caliber or "").strip())
    if _norm_caliber_key(canon) in _CARBINE_CALIBERS:
        return True
    return "CARBINE" in (item_code or "").upper()


# Model keys that are NOT safe on the model string alone — see the module note.
_MODEL_NEEDS_CORROBORATION: frozenset[str] = frozenset({_norm_model_key("M1")})


def _canonical_model(raw: str, caliber: str = "", item_code: str = "") -> str:
    """Free-text model → canonical retail display name for WooCommerce.

    A miss returns the input verbatim (never invent a mapping).  Pure function,
    no IO.  DISPLAY LAYER ONLY — must not touch the FastBound as-marked push
    path (see the module note above ``_MODEL_CANONICAL``).

    ``caliber`` and ``item_code`` are the same record's other identifying
    fields.  They are read only for the keys in ``_MODEL_NEEDS_CORROBORATION``;
    every other spelling folds (or does not) on the model string alone, so
    passing nothing keeps the plain behaviour.
    """
    if not raw:
        return raw
    key = _norm_model_key(raw)
    if key in _MODEL_NEEDS_CORROBORATION and _looks_like_a_carbine(
        caliber, item_code
    ):
        return raw
    if key in _MODEL_CANONICAL:
        return _MODEL_CANONICAL[key]
    return _MODEL_DISPLAY_FIXED_POINTS.get(key, raw)


def _is_ammo(item_group: str | None) -> bool:
    return (item_group or "") in _AMMO_GROUPS


# Separators an operator (or a distributor feed) might use in a state list.
_STATE_SPLIT_RE = re.compile(r"[,;/|\s]+")


def _normalize_states(raw: str | None) -> str:
    """``" ca ; ny, ca "`` → ``"CA,NY"``.

    Ship-restriction lists arrive as free text (``Item.restricted_states``, a
    Small Text an operator types into).  The consumer is a string comparison in
    the storefront's cart validator, so the value pushed to WooCommerce has to
    be canonical: upper-cased, single-comma separated, no duplicates, order
    preserved.  Codes are NOT validated against a state table — an unrecognised
    token is data to carry, not to silently drop.

    Returns ``""`` when nothing survives, which callers read as "no restriction
    list on this product" (they omit the meta key entirely).
    """
    seen: list[str] = []
    for token in _STATE_SPLIT_RE.split(raw or ""):
        code = token.strip().upper()
        if code and code not in seen:
            seen.append(code)
    return ",".join(seen)


def _build_images(image_srcs: list[str]) -> list[dict]:
    """Build a WooCommerce ``images`` list from a list of pre-resolved URLs."""
    return [{"src": src} for src in image_srcs if (src or "").strip()]


def _meta_data(pairs: dict) -> list[dict]:
    """Convert ``{key: value}`` to a WooCommerce ``meta_data`` list."""
    return [{"key": k, "value": str(v)} for k, v in pairs.items()]


# Extension-contributed product meta.  An extension owns fields core knows
# nothing about (CA compliance flags, say) and needs them on the shop page; the
# alternative is extensions patching the payload builders, which is exactly the
# core-fork the extension system exists to prevent.
#
# Provider: ``(doctype, name) -> {meta_key: value}``, where doctype is "Item"
# (shapes 1–2) or "Serial No" (shape 3).  Keys MUST start with "_" (WordPress
# treats a leading underscore as hidden/protected meta, which is what every
# machine-owned key here already is) and MUST NOT collide with a core key —
# a collision is dropped, not merged, because core's value is the one the
# checkout and the SOLD lifecycle read.
#
# Anything a provider returns is machine-derived by definition, so the keys are
# also POS-owned for the lean-update allowlist (see sync._POS_OWNED_META_KEYS);
# an extension that could write a key on a full push but not on a lean one
# would drift silently, which is the bug class that allowlist exists to avoid.
_EXTENSION_META_KEY_RE = re.compile(r"^_[a-z0-9_]{2,60}$")


def extension_product_meta(doctype: str, name: str) -> dict[str, str]:
    """Collect ``gunshop_woo_product_meta`` contributions for one document.

    A provider that raises is logged and skipped: an extension must not be able
    to stop a listing push, and the listing is still correct without its flag.
    """
    out: dict[str, str] = {}
    for fn in frappe.get_hooks("gunshop_woo_product_meta"):
        try:
            got = frappe.get_attr(fn)(doctype, name) or {}
        except Exception:
            frappe.log_error(
                title="gunshop_woo_product_meta provider failed",
                message=f"{fn} for {doctype} {name}\n{frappe.get_traceback()}",
            )
            continue
        for key, value in dict(got).items():
            key = str(key)
            if not _EXTENSION_META_KEY_RE.match(key):
                frappe.log_error(
                    title="gunshop_woo_product_meta rejected a key",
                    message=f"{fn} returned {key!r} (must match ^_[a-z0-9_]{{2,60}}$)",
                )
                continue
            out[key] = "" if value is None else str(value)
    return out


def extension_meta_keys() -> frozenset[str]:
    """Meta keys extensions may write, for the lean-update allowlist.

    Declared by ``gunshop_woo_product_meta_keys`` (a list of key strings) rather
    than derived from a provider call: the allowlist is consulted per push
    without a document in hand, and a key that only appears once a provider has
    an answer would be dropped from exactly the lean pushes that need it.
    """
    keys = set()
    for key in frappe.get_hooks("gunshop_woo_product_meta_keys") or []:
        if _EXTENSION_META_KEY_RE.match(str(key)):
            keys.add(str(key))
    return frozenset(keys)


def _with_extension_meta(payload: dict, doctype: str, name: str) -> dict:
    """Merge extension meta into a built payload, core keys winning."""
    extra = extension_product_meta(doctype, name)
    if not extra:
        return payload
    rows = list(payload.get("meta_data") or [])
    taken = {row.get("key") for row in rows}
    rows.extend(
        {"key": key, "value": value}
        for key, value in extra.items()
        if key not in taken
    )
    payload["meta_data"] = rows
    return payload


# MAP price control: the retail storefront (the storefront plugin's MapPrice) hides a price below
# the Item's MAP according to the brand's mode. Retail only.
# Both keys go on every retail push, empty
# when there is no MAP, so a MAP taken off an Item reaches the store too.
MAP_META_KEYS = ("_osa_map_price", "_osa_map_mode")
# The Item fields map_meta reads: the push triggers (doc_events) and reconcile's
# batched rows both use this one list.
MAP_ITEM_FIELDS = ("map_price", "brand", "manufacturer", "is_used")


# The mode a site without a registrant carries: the website price never goes
# below MAP. Called "Floor" before; the storefront reads either name.
MAP_MODE_DEFAULT = "MAP is minimum price"


def map_mode_for_item(item_code, brand) -> str:
    """The ``map_mode_for_item`` hook's answer (ffl_integrations' brand table);
    ``MAP_MODE_DEFAULT`` on a site without a registrant."""
    for fn in frappe.get_hooks("map_mode_for_item"):
        mode = frappe.get_attr(fn)(item_code, brand)
        if mode:
            return str(mode)
    return MAP_MODE_DEFAULT


def map_meta(item) -> dict:
    """The retail MAP meta for an Item (a doc or a row with name, map_price, brand,
    manufacturer, is_used) — what the push writes and reconcile expects. A used gun
    carries no new-product MAP."""
    map_price = 0.0 if item.get("is_used") else float(item.get("map_price") or 0)
    if map_price <= 0:
        return dict.fromkeys(MAP_META_KEYS, "")
    return dict(zip(MAP_META_KEYS, (f"{map_price:.2f}", map_mode_for_item(
        item.get("name"), item.get("brand") or item.get("manufacturer")))))


def _with_map_meta(payload: dict, item) -> dict:
    meta = map_meta(item)
    rows = [r for r in payload.get("meta_data") or [] if r.get("key") not in meta]
    payload["meta_data"] = rows + _meta_data(meta)
    return payload


def _wc_attributes(
    attrs: list[tuple[str, str, str]],
    attr_ids: dict[str, int] | None = None,
) -> list[dict]:
    """Build WooCommerce ``attributes`` from ``[(name, slug, value), …]``.

    If ``attr_ids`` is provided (a ``{slug: id}`` map of global WC attribute
    taxonomy IDs), the correct global ID is used so pushed products link to the
    real WC taxonomies (enabling taxonomy filtering / shop organisation).
    Without it, ``id=0`` is used, which makes WooCommerce create per-product
    custom attributes that do not participate in taxonomy filtering.
    """
    ids = attr_ids or {}
    return [
        {
            "id": ids.get(slug, 0),
            "name": name,
            "slug": slug,
            "visible": True,
            "options": [value],
        }
        for name, slug, value in attrs
    ]


# ===========================================================================
# URL helper — requires frappe.utils (not pure; lives near the thin loaders)
# ===========================================================================

def _resolve_image(image_field: str | None) -> str:
    """Convert a Frappe-relative image path to an absolute URL.

    Paths starting with ``/`` are prefixed with the site URL via
    ``frappe.utils.get_url()``.  Already-absolute URLs are returned unchanged.
    Not pure: calls into Frappe's URL resolver.
    """
    src = (image_field or "").strip()
    if src.startswith("/"):
        site_url = frappe.utils.get_url().rstrip("/")
        src = f"{site_url}{src}"
    return src


# Private-upload guard.  WooCommerce sideloads every ``images[].src`` with an
# anonymous GET; a Frappe ``/private/files/…`` URL answers 403 and the whole
# product POST fails with ``woocommerce_product_image_upload_error`` (400).
# The desk uploader defaults attachments to private unless the field says
# otherwise (ffl_core sets ``make_attachment_public`` on the photo fields),
# and API / MCP callers may still upload private — so the push flips a private
# photo public rather than failing.  Scope is deliberately narrow: ONLY the
# file referenced by ``image`` / an ``image_gallery`` row is touched, never
# the document's other attachments (4473s, ID scans — the inverse lives in
# ``ffl_core.api.receive_goods._ensure_private_file``; both directions go
# through ``ffl_core.utils.set_file_privacy``).
_PRIVATE_FILES_PREFIX = "/private/files/"
_PUBLIC_FILES_PREFIX = "/files/"


def _publicize_file(path: str, *, where: str, owner: tuple[str, str]) -> str:
    """Return a public URL for ``path``, flipping the File behind a
    ``/private/files/…`` path public on the way.

    Public paths and absolute URLs come back unchanged with no DB access.
    :func:`ffl_core.utils.set_file_privacy` does the flip (with
    ``verify_attached_field=True``: only a parent field that really holds the
    URL is rewritten — a gallery upload's File row says ``attached_to_field=
    "image"``, the child fieldname, which must not clobber the parent's cover).

    No File row for the private path usually means the flip already happened
    under another reference to the same photo (``Item.image`` is mirrored into
    the gallery by ``ffl_core.item_hooks.sync_item_image_gallery``, and Frappe
    rewrote every row on the first flip) — :func:`_public_sibling` resolves that
    case, scoped to ``owner``.

    Anything unrecoverable — no File row on either side, bytes missing on disk,
    a name clash in ``/files``, a site that lets only System Managers publish
    files, a row whose ``is_private`` and ``file_url`` disagree — raises
    ``frappe.ValidationError`` naming ``where`` so ``woo_sync_error`` carries an
    actionable message instead of Woo's opaque 400.  Not pure: reads/writes File.

    Known transient: two workers pushing the same document at once can race the
    first flip (the second one's snapshot still says private while the bytes
    have moved) and report "Cannot find file … on disk"; the next push finds
    the public URL and succeeds.
    """
    src = (path or "").strip()
    if not src.startswith(_PRIVATE_FILES_PREFIX):
        return src
    try:
        new_url = set_file_privacy(src, private=False, verify_attached_field=True)
    except (FileNotFoundError, FileExistsError, frappe.ValidationError,
            frappe.PermissionError) as exc:
        frappe.throw(f"{where}: could not make image {src} public — {exc}")
    if new_url is None:
        new_url = _public_sibling(src, where=where, owner=owner)
    if new_url.startswith(_PRIVATE_FILES_PREFIX):
        frappe.throw(
            f"{where}: the File record for {src} is marked public but is still "
            f"served from {new_url} — its is_private / file_url disagree; fix the "
            "File record."
        )
    return new_url


def _public_sibling(src: str, *, where: str, owner: tuple[str, str]) -> str:
    """Resolve a stale private reference to the public File it became.

    Frappe keeps the basename when it moves a file between the two folders
    (``File.handle_is_private_changed``, file.py:330-334), so the public twin
    of ``/private/files/x.jpg`` is ``/files/x.jpg``.  A same-named file is NOT
    proof of the same photo, though — ``generate_file_name`` de-duplicates
    within one folder only, and camera defaults like ``IMG_2685.jpg`` collide —
    so the twin is accepted only when its File row is attached to the owning
    document; anything else fails loudly rather than publish another gun's
    picture.  The accepted twin still goes through ``set_file_privacy`` so its
    state is asserted by the one home, not by a rebuilt string.
    """
    sibling = _PUBLIC_FILES_PREFIX + src.rsplit("/", 1)[-1]
    doctype, name = owner
    # Several File rows can share one file_url (Frappe de-duplicates identical
    # bytes into one file on disk, and update_existing_file_docs rewrites every
    # row sharing the content hash), so ask for THIS document's row first.
    own_row = frappe.db.get_value(
        "File",
        {"file_url": sibling, "attached_to_doctype": doctype, "attached_to_name": name},
        "name",
    )
    if own_row:
        resolved = set_file_privacy(sibling, private=False)
        if resolved:
            frappe.logger("woo.sync").warning(
                f"{where}: stale private reference {src} resolved to {resolved}"
            )
            return resolved
    row = frappe.db.get_value(
        "File", {"file_url": sibling},
        ["name", "attached_to_doctype", "attached_to_name"], as_dict=True,
    )
    elsewhere = (
        f" (a public file of the same name exists but belongs to "
        f"{row.attached_to_doctype} {row.attached_to_name})"
        if row and row.attached_to_doctype else
        " (a public file of the same name exists but is attached to nothing)" if row else ""
    )
    frappe.throw(
        f"{where}: image {src} is a private file with no File record{elsewhere} — "
        "remove it and upload the photo again."
    )


def _publicize_listing_images(doc, *, doctype: str) -> tuple[list, str]:
    """Return ``(gallery_rows, image)`` for ``doc`` with private photos made public.

    Every reference is rewritten with ``frappe.db.set_value(...,
    update_modified=False)``: no ``on_update`` fires (no push recursion) and the
    returned image / in-memory rows carry the new URLs so the payload built right
    after uses them.  Docs with only public photos cost zero queries.

    Two Frappe side effects are compensated:

    * A photo referenced twice (``Item.image`` is mirrored into the gallery by
      ``sync_item_image_gallery``) is flipped once — a per-call memo answers the
      second reference, and :func:`_publicize_file` resolves a stale private
      reference whose File is already public (scoped to this document).
    * Frappe's flip rewrites the parent's ``attached_to_field`` with the file's
      new URL; the flip runs with ``verify_attached_field=True`` so a gallery
      row's file (``attached_to_field="image"``, the child fieldname) cannot
      clobber the cover, and the cover is re-asserted after the gallery loop as
      cheap insurance against any other write-back path.  Frappe's write-back
      uses ``update_modified=True``, so a flip does bump the parent's
      ``modified`` — the push's own status write-back in ``sync.push_*`` does
      the same, so this is not a new form-staleness risk.

    The top-level ``image`` is rewritten here even though Frappe rewrites it
    itself when the File row carries ``attached_to_*``: a photo pasted into the
    field from elsewhere has no such row data and would otherwise keep the
    dead private URL.
    """
    where = f"{doctype} {doc.name}"
    owner = (doctype, doc.name)
    memo: dict[str, str] = {}

    def _public(path: str, label: str) -> str:
        if path not in memo:
            memo[path] = _publicize_file(path, where=label, owner=owner)
        return memo[path]

    image = (doc.get("image") or "").strip()
    if image.startswith(_PRIVATE_FILES_PREFIX):
        new_url = _public(image, f"{where} image")
        if new_url != image:
            frappe.db.set_value(doctype, doc.name, "image", new_url, update_modified=False)
            image = new_url

    rows = list(doc.get("image_gallery") or [])
    gallery_changed = False
    for row in rows:
        current = (getattr(row, "image", None) or "").strip()
        if not current.startswith(_PRIVATE_FILES_PREFIX):
            continue
        new_url = _public(current, f"{where} gallery row {getattr(row, 'idx', '?')}")
        if new_url != current:
            frappe.db.set_value(
                "Item Image Gallery", row.name, "image", new_url, update_modified=False
            )
            row.image = new_url
            gallery_changed = True

    if gallery_changed:
        # Re-assert the cover (see docstring); a no-op when nothing touched it.
        # Explicit even for an empty cover: the document's own state is the
        # truth, not a File side effect.  Also the parent write that refreshes
        # the parent's document cache after the child-row rewrites above.
        frappe.db.set_value(doctype, doc.name, "image", image, update_modified=False)
    return rows, image


# RSR standard product image → its 300dpi high-res counterpart.
#   Standard:  https://img.rsrgroup.com/pimages/<name>.jpg            (~400px)
#   High-res:  https://img.rsrgroup.com/highres-pimages/<name>_HR.jpg (~2000px)
# RSR ships HR for MOST but not all images, so HEAD-probe and fall back to the
# standard URL when no _HR exists.  WooCommerce sideloads whichever URL we return
# onto our own media library — we never hot-link — honouring RSR's "transfer to
# your own server" terms.  Results are memoised per URL for the worker's life so a
# bulk re-push doesn't re-probe the same image.
_RSR_PIMAGE_RE = re.compile(
    r"^(?P<host>https?://img\.rsrgroup\.com)/pimages/(?P<name>.+)\.(?P<ext>jpe?g)$",
    re.IGNORECASE,
)
_HIGHRES_CACHE: dict[str, str] = {}


def _to_highres(url: str) -> str:
    """Return the RSR 300dpi ``_HR`` image URL when one exists, else ``url``.

    Non-RSR URLs (uploaded media, relative paths) pass through unchanged.  Any
    probe failure (timeout, network error, non-200) falls back to the original
    URL — upgrading image quality must never break a product push.
    """
    m = _RSR_PIMAGE_RE.match(url or "")
    if not m:
        return url
    if url in _HIGHRES_CACHE:
        return _HIGHRES_CACHE[url]
    hr_url = f"{m['host']}/highres-pimages/{m['name']}_HR.{m['ext']}"
    resolved = url
    try:
        import requests
        if requests.head(hr_url, timeout=5, allow_redirects=True).status_code == 200:
            resolved = hr_url
    except Exception:
        resolved = url
    _HIGHRES_CACHE[url] = resolved
    return resolved


def _resolve_image_list(
    gallery_rows,
    fallback_image: str | None = None,
) -> list[str]:
    """Resolve image URLs from an ``image_gallery`` Table field.

    The row flagged ``is_primary=1`` is placed **first** so WooCommerce treats
    it as the featured/main product image — WooCommerce promotes ``images[0]``
    to the product's featured image.  Remaining rows follow in their original
    order.  When the gallery yields no images, falls back to ``fallback_image``
    (also resolved).

    Works with both Frappe Document rows (``row.image``, ``row.is_primary``)
    and plain dicts / ``frappe._dict``.

    The ORDERING half now lives in ``ffl_core.images.ordered_gallery_paths`` —
    GunBroker publishes the same galleries and promotes the first uploaded
    picture to its thumbnail exactly as WooCommerce promotes ``images[0]``, so
    "which photo leads" is one rule for both channels. Only the WooCommerce-
    specific resolution (absolute site URL + the RSR high-res probe) stays here:
    GunBroker uploads raw bytes and has no use for either.
    """
    from ffl_core.images import ordered_gallery_paths

    return [_to_highres(_resolve_image(p))
            for p in ordered_gallery_paths(gallery_rows, fallback_image)]


def sale_price_payload(sale_price, sale_from=None, sale_to=None) -> dict:
    """The three WooCommerce sale keys, ALWAYS all present.

    ``""`` is the only value WooCommerce reliably clears on — JSON ``null``
    for the date keys is silently IGNORED by the WC REST controller (PHP
    ``isset()`` drops it; verified against WC v3), so omitting or nulling a
    key would strand yesterday's schedule on the shop forever. Emitting the
    full trio every push is what makes POS the authority: clearing the sale
    in POS clears it on the store.

    Dates are calendar days in the store's timezone; ``date_on_sale_to``
    runs to end-of-day (23:59:59) exactly like wp-admin's own scheduler, so
    a sale "through the 30th" still holds at 9 PM on the 30th. WooCommerce
    itself flips the storefront price on these dates — no push needed on
    the day a window opens or closes.
    """
    price = float(sale_price or 0)
    if price <= 0:
        return {"sale_price": "", "date_on_sale_from": "", "date_on_sale_to": ""}
    return {
        "sale_price": f"{price:.2f}",
        "date_on_sale_from": f"{sale_from}T00:00:00" if sale_from else "",
        "date_on_sale_to": f"{sale_to}T23:59:59" if sale_to else "",
    }


# ===========================================================================
# Pure builder — non-firearm OR uniform-price firearm (one WC product per Item)
# ===========================================================================

def build_simple_product_payload(
    *,
    item_code: str,
    item_name: str,
    description: str = "",
    standard_rate: float = 0.0,
    sale_price: float | None = None,
    sale_from=None,
    sale_to=None,
    stock_qty: int = 0,
    is_firearm: bool = False,
    is_antique: bool = False,
    is_used: bool = False,
    condition_grade: str = "",
    caliber: str = "",
    item_group: str = "",
    upc: str = "",
    manufacturer: str = "",
    model_name: str = "",
    image_srcs: list[str] | None = None,
    attr_ids: dict[str, int] | None = None,
    restricted_states: str | None = None,
    fulfillment_mode: str | None = None,
    lead_days: str | None = None,
    brand_id: int | None = None,
    shipping_class: str | None = None,
) -> dict:
    """Build a WooCommerce product payload for a non-firearm or uniform-price firearm.

    SKU is set to ``item_code``.  All arguments are plain Python values —
    no Frappe document or DB access occurs inside this function.

    Catalogue fields map to native WooCommerce structures instead of ``_osa_*`` meta:
    UPC → ``global_unique_id`` (the GTIN field); Caliber / Condition / Manufacturer /
    Model → ``pa_caliber`` / ``pa_condition`` / ``pa_manufacturer`` / ``pa_model``
    attributes (Caliber+Condition for firearms, Caliber for ammo, Manufacturer/
    Model whenever set). The storefront theme reads these attributes + GTIN directly.

    ``is_antique``
        The Item's pre-1899 / GCA-exempt flag.  It narrows exactly ONE key —
        ``_firearm_product`` ("Requires FFL Shipment") — and nothing else: an
        antique is still a firearm in the catalogue, so ``is_firearm`` keeps
        driving the ``pa_caliber`` / ``pa_condition`` attributes and the gun
        stays filterable on the storefront.

    ``attr_ids`` — optional ``{slug: id}`` map of global WC taxonomy IDs;
    threaded into :func:`_wc_attributes` so attributes link to the real
    WooCommerce taxonomies.

    Dropship / listing fields (RSR P3 — all three default to "not set", so an
    ordinary retail push emits none of them and cannot clear what the store
    already holds):

    ``restricted_states``
        CSV of state codes a product may not ship to → ``_osa_restricted_states``
        meta (normalised by :func:`_normalize_states`).  ``None`` ⇒ **no key at
        all** (not a listing push); any string — including ``""`` — is emitted,
        because "" is the only way to *clear* a restriction pushed earlier.
    ``brand_id``
        WooCommerce ``product_brand`` **term id** → ``brands``.  A term, not a
        ``pa_*`` attribute — the MAP-pricing plugin matches on the brand
        taxonomy.  The id is resolved (and the term created) by the sync layer,
        exactly like the item-group category, so this builder stays IO-free.
    ``shipping_class``
        Shipping-class **slug** (``dropship-handgun`` /
        ``dropship-firearm`` / ``dropship-accessory``).
    """
    ammo = _is_ammo(item_group)

    # ``_firearm_product`` is the storefront plugin's "Requires FFL Shipment" flag, so its
    # predicate is a GCA-*regulated* firearm — not "is a gun". An antique
    # (pre-1899) is a firearm for inventory/serial purposes but the GCA does not
    # regulate its transfer, so demanding an FFL for one is simply wrong.
    # Composed here rather than via ``_is_regulated_firearm_row`` because this
    # builder is pure: the caller already resolved the firearm test (including
    # the item-group fallback) into ``is_firearm``, so ``is_firearm and not
    # is_antique`` IS that helper's rule evaluated where the data lives.
    # ``is_antique`` defaults False ⇒ omitting it reproduces the old behaviour.
    meta: dict[str, str] = {
        "_firearm_product": "yes" if (is_firearm and not is_antique) else "no",
        "_ammunition_product": "yes" if ammo else "no",
    }
    attributes: list[tuple[str, str, str]] = []

    # Caliber / Condition / Manufacturer / Model → WooCommerce attributes
    # (pa_* taxonomies via attr_ids). The storefront theme + condition scale read these
    # taxonomies directly, so no duplicate _osa_* meta is written.
    if is_firearm:
        condition = _pa_condition(condition_grade, is_used, is_antique)
        if caliber:
            attributes.append(("Caliber", "pa_caliber", _canonical_caliber(caliber)))
        if condition:
            attributes.append(("Condition", "pa_condition", condition))
    elif ammo and caliber:
        attributes.append(("Caliber", "pa_caliber", _canonical_caliber(caliber)))

    if (manufacturer or "").strip():
        attributes.append(("Manufacturer", "pa_manufacturer", manufacturer.strip()))
    if (model_name or "").strip():
        attributes.append(("Model", "pa_model", _canonical_model(
            model_name.strip(), caliber, item_code)))

    # None = "not this kind of push, don't touch the key"; a string (including
    # "") = "this is the authoritative list".  Emitting "" is what makes the meta
    # clearable: WooCommerce meta is only ever added or overwritten, so omitting
    # the key on an empty list left yesterday's restrictions on the shop for ever
    # (the validator reads "" as "no restrictions").
    if restricted_states is not None:
        meta["_osa_restricted_states"] = _normalize_states(restricted_states)

    # Same None-vs-string contract: None = not a listing push, leave the
    # key alone; a string = the authoritative fulfilment mode ("Direct" /
    # "To Store"), ALWAYS emitted on listing pushes so a mode flip reaches the
    # shop. wp-admin renders it as the product's drop-ship badge.
    if fulfillment_mode is not None:
        meta["_osa_fulfillment_mode"] = str(fulfillment_mode).strip()

    # Same contract again: "" on a listing push clears a lead time the house no
    # longer promises; None on every other push leaves the shop's value alone.
    if lead_days is not None:
        meta["_osa_lead_days"] = str(lead_days).strip()

    payload: dict = {
        "name": item_name or item_code,
        "sku": item_code,
        "type": "simple",
        "status": "publish",
        "regular_price": f"{float(standard_rate or 0):.2f}",
        # The sale trio is always present — "" clears (see sale_price_payload).
        **sale_price_payload(sale_price, sale_from, sale_to),
        "description": description or "",
        "manage_stock": True,
        "stock_quantity": max(0, int(stock_qty or 0)),
        "meta_data": _meta_data(meta),
    }

    # UPC → WooCommerce's native "GTIN, UPC, EAN, or ISBN" field.
    if (upc or "").strip():
        payload["global_unique_id"] = upc.strip()

    images = _build_images(image_srcs or [])
    if images:
        payload["images"] = images
    # ``attributes`` on a PUT is the AUTHORITATIVE list: WooCommerce unsets (and
    # strips the terms of) any attribute missing from it, but only when the key
    # is present. A firearm with no condition and no other attribute would send
    # none and leave a stale pa_condition behind, so firearms always send it.
    if attributes or is_firearm:
        payload["attributes"] = _wc_attributes(attributes, attr_ids=attr_ids)

    # WooCommerce term ids are positive; 0 means "unresolved" and sending it
    # would blank the product's brand instead of leaving it alone.
    if brand_id:
        payload["brands"] = [{"id": int(brand_id)}]

    slug = (shipping_class or "").strip().lower()
    if slug:
        payload["shipping_class"] = slug

    return payload


# ===========================================================================
# Pure builder — per-serial firearm (one WC product per Serial No)
# ===========================================================================

def build_serial_product_payload(
    *,
    item_code: str,
    serial_no: str,
    item_name: str,
    description: str = "",
    sell_price: float | None = None,
    sale_price: float | None = None,
    sale_from=None,
    sale_to=None,
    is_antique: bool = False,
    is_used: bool = False,
    condition_grade: str = "",
    caliber: str = "",
    upc: str = "",
    manufacturer: str = "",
    model_name: str = "",
    woo_reserved: bool = False,
    channel_reserved: bool = False,
    counter_held: bool = False,
    consigned_out: bool = False,
    is_active: bool = True,
    sold_archived: bool = False,
    clear_sold: bool = False,
    image_srcs: list[str] | None = None,
    attr_ids: dict[str, int] | None = None,
) -> dict:
    """Build a WooCommerce product payload for one physical (per-serial) firearm.

    SKU is ``"{item_code}::{serial_no}"``.  Stock is **1** when the serial is
    Active and not reserved; **0** when ``woo_reserved``, ``channel_reserved``
    or ``counter_held`` is true, the serial is not Active, or ``sold_archived``
    is set.

    ``channel_reserved`` also sends the product to ``draft`` — see the comment
    at the status derivation for why that differs from the SOLD archive's
    publish-at-stock-0.

    ``consigned_out`` is "this gun is at a partner FFL on consignment"
    (``ffl_core.warehouses.is_consigned_out``): still ``Active``, but not on our
    shelf, so stock 0 and ``draft`` exactly like ``channel_reserved`` — the page
    the reconcile safety net (Pass 3b) and ``route_serial_exit`` give it. A gun
    that SOLD at the partner keeps the SOLD archive shape (``sold_archived``
    wins), as ``route_serial_exit`` rules.

    ``channel_reserved`` is "some OTHER sales channel has this gun" — a
    GunBroker order, today. Separate from ``woo_reserved`` rather than folded
    into it because the two are written by different apps and this builder is a
    pure function: the caller resolves both and passes facts. Defaults False, so
    every existing caller — including the Item-level payload, which has no
    serial to ask about — keeps its behaviour by omission.

    ``counter_held`` is "somebody in the SHOP is buying this gun" — a parked POS
    sale awaiting its 4473, a submitted Create Order, a draft consignment
    (``ffl_core.reservations.counter_holds``).  Such a gun is still ``Active``
    in a warehouse and reserved by nobody, so without this key every field here
    reads "sellable": that is how the gun on DISP-2026-02000 stayed buyable
    online for the eleven days its 4473 sat pending.  It rides the SAME lane as
    ``woo_reserved`` — stock 0, page stays ``publish`` — because it is our own
    store holding it: the page is not a lie, the gun is simply spoken for, and
    it comes back the moment the counter order is cancelled.

    It is a FLOOR, not the whole policy. Which page treatment a hold earns is
    ``tasks.sync_serial_listing_task``'s call (a parked POS sale keeps its page;
    a committed Sales Invoice hold is delisted). This key only guarantees that
    no push path can hand the storefront a buyable gun somebody is already
    buying — which is what ``serial_to_product_payload`` did for every caller
    until 2026-09-10, reconcile and the manual push button included.

    SOLD lifecycle — the two flags are mutually exclusive and come
    from ``sync.is_sold_state`` / ``sync.is_relisted_state``:

    ``sold_archived``
        The gun sold; its page stays ``publish`` at stock 0.  ``_osa_sold_*``
        meta is deliberately NOT emitted: WooCommerce ``meta_data`` is an UPSERT,
        so keys absent from the payload keep their stored values — that is what
        makes a post-sale gallery edit re-push safely instead of wiping the
        storefront's "Sold for $X" (the SP000279 zombie fix).  Stock 0 is also
        guaranteed by ``is_active`` today; pinning it here keeps a future change
        to that derivation from handing the storefront a buyable sold gun.

    ``clear_sold``
        The gun came back (return / undispose).  ``_osa_sold_*`` are emitted
        BLANK — because of that same upsert rule, omitting them would strand the
        page reading SOLD forever.  Consumers must treat empty as absent
        (PHP ``!empty()``), not test for key existence.

    All arguments are plain Python values — no Frappe document or DB access.

    UPC → ``global_unique_id`` (GTIN); Caliber / Condition / Manufacturer / Model →
    ``pa_*`` attributes; ``_osa_serial`` carries the serial number
    (no attribute equivalent).

    ``is_antique``
        The PARENT Item's pre-1899 / GCA-exempt flag (antique-ness is a per-SKU
        fact — a pre-1899 model — so it lives on the Item, never the Serial No).
        It narrows exactly ONE key, ``_firearm_product`` ("Requires FFL
        Shipment"); the gun keeps its ``pa_*`` attributes and ``_osa_serial``.

    ``attr_ids`` — optional ``{slug: id}`` map of global WC taxonomy IDs.
    """
    condition = _pa_condition(condition_grade, is_used, is_antique)
    stock_qty = 0 if (
        sold_archived or woo_reserved or channel_reserved or counter_held
        or consigned_out or not is_active) else 1
    # A gun another channel has sold goes to DRAFT, not to the publish-at-stock-0
    # shape a SOLD gun gets. The two look similar and mean opposite things: the
    # SOLD archive is a deliberate storefront page ("sold for $X") that keeps its
    # rankings, while this gun has not been sold HERE and may yet come back if
    # the other channel's order is cancelled. Advertising it as sold on this
    # store would be a lie, and leaving it published at stock 0 leaves a
    # buyable-looking page for a gun that is spoken for.
    #
    # This is also what lets the machine self-heal path stay open: with the
    # status following the stock, a lean push for such a gun cannot publish
    # anything, so `push_serial` can allow it through instead of refusing and
    # leaving the storefront showing stock 1 forever.
    status = "draft" if ((channel_reserved or consigned_out) and not sold_archived) else "publish"

    # ``_firearm_product`` = the storefront plugin's "Requires FFL Shipment". Every product
    # from this builder is a gun, so the only question is whether the GCA
    # regulates its transfer: an antique (pre-1899) is exempt and must not
    # demand an FFL. ``is_antique`` defaults False ⇒ 'yes', exactly the value
    # this key was hard-coded to before the fix, so no caller changes behaviour
    # by omission.
    meta: dict[str, str] = {
        "_firearm_product": "no" if is_antique else "yes",
        "_ammunition_product": "no",
        "_osa_serial": serial_no,
    }
    if clear_sold:
        meta["_osa_sold_at"] = ""
        meta["_osa_sold_price"] = ""

    attributes: list[tuple[str, str, str]] = []
    if caliber:
        attributes.append(("Caliber", "pa_caliber", _canonical_caliber(caliber)))
    if condition:
        attributes.append(("Condition", "pa_condition", condition))
    if (manufacturer or "").strip():
        attributes.append(("Manufacturer", "pa_manufacturer", manufacturer.strip()))
    if (model_name or "").strip():
        attributes.append(("Model", "pa_model", _canonical_model(
            model_name.strip(), caliber, item_code)))

    payload: dict = {
        "name": item_name or item_code,
        "sku": f"{item_code}::{serial_no}",
        "type": "simple",
        "status": status,
        "regular_price": f"{float(sell_price or 0):.2f}",
        # The sale trio is always present — "" clears (see sale_price_payload).
        **sale_price_payload(sale_price, sale_from, sale_to),
        "description": description or "",
        "manage_stock": True,
        "stock_quantity": stock_qty,
        "meta_data": _meta_data(meta),
        "attributes": _wc_attributes(attributes, attr_ids=attr_ids),
    }

    # UPC → WooCommerce's native "GTIN, UPC, EAN, or ISBN" field.
    if (upc or "").strip():
        payload["global_unique_id"] = upc.strip()

    images = _build_images(image_srcs or [])
    if images:
        payload["images"] = images

    return payload


# ===========================================================================
# Thin loaders (fetch Frappe data, then delegate to pure builders)
# ===========================================================================

def unassigned_web_claims(item_code: str) -> int:
    """How many units of *item_code* a pending web order owes without naming a rifle.

    A uniform-price firearm is one WooCommerce product standing for several
    interchangeable guns, so an order for it owes a UNIT, not a particular rifle.
    POS used to answer that by parking the oldest free gun on the row — a guess
    staff's own pick then contradicted, which is how seven paid orders deadlocked
    in production on 2026-08-13. It no longer guesses: the row records the claim
    and names nothing.

    Which leaves this. :func:`stock_for_item` counts a uniform-price firearm's
    stock as Serial Nos that are FREE, and a claim reserves none — so without this
    subtraction the storefront would go on advertising rifles that are already
    spoken for. The claim spends a unit; it just cannot say which one.

    Raw SQL rather than ``frappe.get_all`` on purpose: the wholesale
    ``frappe.get_all`` mocks in this app's tests would answer it with serial
    fixtures. ``ffl_core/custody.py`` states the same rule, and ``custody_serials``
    — called from the very function this feeds — already follows it.

    Deliberate divergences from its sibling
    :func:`fulfillment.reserved_web_serials`, each load-bearing:

    * **No ``site`` predicate.** One physical rifle, one shelf: an order's claim
      comes off the count whichever store took it, and :func:`stock_for_item` takes
      no site either. Every neighbouring query in this app IS site-scoped, so this
      reads like an omission and is not one.
    * **``local`` lane only, blank read as local.** Do not reach for
      ``_LOCALLY_DISPOSED_LANES`` — it also contains ``to_store``, which is
      procured from the distributor rather than held off the shelf. Drop-ship rows
      are blank/blank by design and would otherwise subtract for ever.
    * **``TRIM``.** Python consumers compare ``.strip()``ed values; untrimmed SQL
      reads a whitespace serial as a named one, yielding neither a reservation nor
      a claim.
    * **No ``reservation_error`` filter.** A row that failed for lack of stock
      still owes its paid unit. Today that reads the same either way (the count is
      already 0 and clamps), but the moment a crate lands the listing would
      advertise the new rifles while that order still owes one — an oversell aimed
      squarely at the orders already stuck.
    """
    row = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabWoo Online Order Serial` s
        INNER JOIN `tabWoo Online Order` o ON o.name = s.parent
        WHERE s.parenttype = 'Woo Online Order'
          AND s.item_code = %(item_code)s
          AND o.order_status = 'Pending Fulfillment'
          AND COALESCE(NULLIF(s.fulfillment, ''), 'local') = 'local'
          AND COALESCE(TRIM(s.confirmed_serial), '') = ''
          AND COALESCE(TRIM(s.stock_hold_serial), '') = ''
        """,
        {"item_code": item_code},
    )
    return int(row[0][0]) if row else 0


def pending_web_line_qty(item_code: str) -> int:
    """Units of *item_code* that web sales have taken off the shelf but whose
    Bin exit has not posted yet — the non-serialized twin of
    :func:`unassigned_web_claims`.

    A non-firearm web sale moves no Bin until its stock leg posts
    (``revenue.deliver_web_stock_lines``, at WC *completed*), so between checkout
    and that Delivery Note the Bin still counts the sold unit. WooCommerce
    decremented itself at checkout; every outbound writer reads
    :func:`stock_for_item`; and without this subtraction reconcile Pass 1 reads
    that very gap as drift and "heals" the sold unit back onto the storefront —
    A one-of-a-kind item (resurrected to instock 44
    minutes after it sold, 2026-08-28) was the incident. An earlier fix closed the post-completion
    accounting half of this hole; this closes the storefront half.

    Three legs, one per stage of the fulfillment pipeline, mutually exclusive by
    construction (a submitted SI exists only for a ``Transferred`` order):

    1. **Lines on ``Pending Fulfillment`` orders** — checkout happened,
       completion has not. Unbounded in time: an open order is an open order.
    2. **Lines on ``Transferred`` orders with no submitted Sales Invoice** —
       the completion job is in flight, crashed before booking, or parked its
       draft (``revenue`` ``draft_total_mismatch``). Audit twin:
       ``order_audit._transferred_without_invoice``.
    3. **Submitted web-SI accessory rows not yet covered by submitted,
       non-return Delivery Notes** — the stock leg failed or a human holds it.
       Audit twin: ``order_audit._invoiced_without_delivery``, same row
       predicates (not drop-shipped, carries a warehouse); a draft/cancelled/
       return DN deliberately leaves the row owed, exactly as there.

    Legs 2–3 take the audit's ``_BACKSTOP_LOOKBACK_DAYS`` floor: while the
    2-hourly backstop is still auto-posting the exit, the unit stays off the
    shelf; past the floor the stuck document belongs to the humans its
    ``alert_once`` already paged and to the month-end control — an ancient
    unresolved row must not delist a restocked item for ever, and installing
    this on history that predates the revenue/stock legs must not delist half
    the accessory catalogue on day one.

    Scope, each choice mirroring the sibling unless said otherwise:

    * **``Cancelled`` never counts.** The unit went genuinely back on sale. No
      paid predicate, same as the sibling: an unpaid checkout already holds
      WooCommerce's own stock, and its cancellation webhook releases the rows
      here the same way.
    * **No site predicate.** One shelf (sibling's rule verbatim).
    * **Supplier-shipped lines never count (legs 1–2).** A line recorded at
      checkout on the ``direct`` / ``fds`` lane is filled from a distributor's
      warehouse and never takes a unit off this shelf; a ``to_store`` line IS
      the store's parcel — its received unit is spoken for — and a line taken
      before lanes were recorded (blank) still counts, as it always did. Leg 3
      is lane-aware the same way: the SI row's own ``delivered_by_supplier``
      flag excludes supplier-shipped units.
      ``SUM(qty)``, not ``COUNT``: accessory lines sell in multiples.
    * **Non-firearm callers only.** A firearm's availability is already spent
      through its serial rows (reservations + claims); counting its order LINES
      too would subtract the same gun twice. :func:`stock_for_item` routes every
      firearm away from the Bin branch, which is the only caller.

    Raw SQL for the sibling's stated reason: the wholesale ``frappe.get_all``
    mocks in this app's tests would answer it with fixture rows.
    """
    from frappe.utils import add_to_date, now_datetime

    from ffl_woo_sync.woocommerce.order_audit import _BACKSTOP_LOOKBACK_DAYS

    floor = add_to_date(now_datetime(), days=-_BACKSTOP_LOOKBACK_DAYS)
    # has_column: during a deploy the new code can run before migrate adds it.
    lane_filter = ("AND COALESCE(l.lane, '') NOT IN ('direct', 'fds')"
                   if frappe.db.has_column("Woo Online Order Line", "lane") else "")

    open_lines = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(l.qty), 0) FROM `tabWoo Online Order Line` l
        INNER JOIN `tabWoo Online Order` o ON o.name = l.parent
        WHERE l.parenttype = 'Woo Online Order'
          AND l.parentfield = 'lines'
          AND l.item_code = %(item_code)s
          {lane_filter}
          AND (
            o.order_status = 'Pending Fulfillment'
            OR (
              o.order_status = 'Transferred'
              AND COALESCE(o.transferred_at, o.creation) >= %(floor)s
              AND NOT EXISTS (
                SELECT 1 FROM `tabSales Invoice` si
                WHERE si.woo_online_order = o.name AND si.docstatus = 1
              )
            )
          )
        """,
        {"item_code": item_code, "floor": floor},
    )
    owed_si_rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(GREATEST(r.qty - COALESCE((
                SELECT SUM(d.qty)
                FROM `tabDelivery Note Item` d
                INNER JOIN `tabDelivery Note` dn ON dn.name = d.parent
                WHERE d.si_detail = r.name AND d.docstatus = 1 AND dn.is_return = 0
            ), 0), 0)), 0)
        FROM `tabSales Invoice Item` r
        INNER JOIN `tabSales Invoice` si ON si.name = r.parent
        WHERE r.parenttype = 'Sales Invoice'
          AND r.parentfield = 'items'
          AND r.item_code = %(item_code)s
          AND si.docstatus = 1
          AND si.update_stock = 0
          AND COALESCE(si.woo_online_order, '') <> ''
          AND si.posting_date >= %(floor)s
          AND COALESCE(r.delivered_by_supplier, 0) = 0
          AND COALESCE(r.warehouse, '') <> ''
          AND r.qty > 0
        """,
        {"item_code": item_code, "floor": floor},
    )
    return int(open_lines[0][0] or 0) + int(owed_si_rows[0][0] or 0)


def channel_committed_units(item_code: str) -> int:
    """The web store's answer to ``ffl_core``'s ``channel_committed_units`` hook:
    units sold on the web and not yet shipped, so another channel does not offer
    them again.

    A uniform-price gun owes its :func:`unassigned_web_claims` (a claim names no
    rifle yet); an accessory owes its open web lines (:func:`pending_web_line_qty`,
    whose Bin exit posts only at completion); a per-serial gun owes nothing — its
    availability is already spent through the serial it reserves.
    """
    item_row = frappe.db.get_value(
        "Item", item_code, ["is_firearm", "item_group", "uniform_sell_price"],
        as_dict=True, cache=True)
    if not item_row or is_per_serial_priced(item_row):
        return 0
    if _is_firearm_row(item_row):
        return unassigned_web_claims(item_code)
    return pending_web_line_qty(item_code)


# How ``ffl_core.reservations.committed_units`` tells the channels apart (and
# lets this one leave itself out of a count it already makes).
channel_committed_units.channel = CHANNEL_WOO


def free_units(item_code: str) -> int:
    """How many :func:`free_serials` a uniform-price firearm Item has."""
    return len(free_serials(item_code))


def free_serials(item_code: str) -> list[str]:
    """In-stock units of a uniform-price firearm Item that NOTHING is holding —
    the free pool before any channel's claims come off it.

    The rulebook moved to ``ffl_core.reservations.uniform_free_serials`` when a
    second channel (GunBroker) began selling the same guns: ONE pool, read by the
    storefront count, ``webhook._pick_oldest_unreserved_serial``, the counter's
    sale gates (``reservations.channel_claim_block_reason``) and every other
    channel's quantity. This name stays so those callers, and the tests that patch it, do
    not move.
    """
    return uniform_free_serials(item_code)


def stock_for_item(item_code: str) -> int:
    """Resolve the on-hand quantity for an *item-level* WooCommerce product.

    Stock rules:

    - **Non-firearm** → total Bin qty (via ``ffl_core.api.item_admin.get_item_stock``),
      minus custody, minus units already sold on web orders still awaiting
      fulfillment (:func:`pending_web_line_qty` — their Bin exit posts at
      completion, not at checkout).
    - **Uniform-price firearm** → count of ``Active`` Serial Nos whose
      ``woo_reserved`` flag is 0 (not yet held by a Woo order).
    - **Per-serial firearm** → raises :class:`ValueError` (each serial is its own
      WooCommerce product with stock 1 or 0; use :func:`serial_to_product_payload`).

    **Distributor stock source (RSR).**  The hook loop below
    is deliberately the FIRST statement — ahead of the per-serial-firearm raise
    and ahead of every local branch.  ``ffl_integrations`` registers
    ``fn(item_code) -> int | None``; the first non-``None`` answer is the stock,
    ``None`` falls through to the local rules unchanged.

    Why here, and only here: a dropship item has no local stock at all, so every
    local branch would answer 0.  All three writers — :func:`sync.push_stock`,
    reconcile Pass 1 and the SLE doc_event — take their quantity from this
    function, so this single point is what stops reconcile from "correcting" a
    dropship listing down to zero.  Placing the loop before the raise
    is what lets a dropship *gun* answer with a quantity: that raise is a
    statement about the store's own serialised inventory, which a dropship gun
    is not part of ([DIVERGENCE-4]).

    Exceptions from a source propagate on purpose.  Swallowing one would silently
    fall back to the local 0 and delist the dropship catalogue on a single bad
    poll; a raise instead surfaces in the Error Log with the listing untouched.
    """
    for fn in frappe.get_hooks("distributor_stock_source"):
        qty = frappe.get_attr(fn)(item_code)
        if qty is not None:
            # Clamped HERE, at the funnel: the formula's own
            # `min(distributor qty − buffer, cap)` goes negative whenever the
            # distributor holds fewer than the buffer.  The full push clamps
            # again in the builder, but the stock-only push sends this value
            # raw — WooCommerce would store 0 and every later reconcile sweep
            # would read the difference as drift and re-push, for ever.
            return max(0, int(qty))
    return local_stock_for_item(item_code)


def local_stock_for_item(item_code: str, *, item_row=None, free=None) -> int:
    """The store's own sellable quantity of an item-level product — every rule of
    :func:`stock_for_item` below its distributor hook (raises the same
    :class:`ValueError` for a per-serial firearm). ``item_row`` / ``free``: the
    Item row and a uniform-price gun's free-unit count, when the caller has them."""
    if item_row is None:
        item_row = frappe.db.get_value(
            "Item",
            item_code,
            ["is_firearm", "item_group", "uniform_sell_price"],
            as_dict=True,
            cache=True,
        )

    if is_per_serial_priced(item_row):
        raise ValueError(
            f"{item_code!r} is a per-serial firearm — stock is per-serial (1 or 0); "
            "call serial_to_product_payload() instead."
        )

    if _is_firearm_row(item_row):
        free = free_units(item_code) if free is None else free
        # A uniform-price order owes a UNIT, not a rifle: since POS stopped
        # guessing which one, a pending claim reserves nothing and would otherwise
        # be invisible here — see :func:`unassigned_web_claims`.
        #
        # Clamped at the funnel, exactly like the distributor branch above and for
        # the same reason: a negative would be stored by WooCommerce as 0 and read
        # by every later reconcile sweep as drift, re-pushing for ever. Negative
        # means the shelf is already oversold, so it is logged rather than merely
        # swallowed — clamping is precisely what would otherwise hide it.
        #
        # Other channels' claims come off the same pool (GunBroker sells these
        # guns too). This channel's own are counted just above, so it is left out
        # of the sum. A channel that cannot answer raises out of here — the push
        # task absorbs it and WooCommerce keeps the number it has, which is the
        # safe side of "unknown".
        claimed = unassigned_web_claims(item_code)
        elsewhere = sum(committed_units(item_code, exclude=CHANNEL_WOO).values())
        if claimed + elsewhere > free:
            frappe.logger("woo.sync").warning(
                f"stock_for_item({item_code!r}): {claimed} pending web claim(s) "
                f"and {elsewhere} claimed elsewhere against {free} free unit(s) — "
                f"the shelf is oversold; reporting 0 to WooCommerce."
            )
        return max(0, free - claimed - elsewhere)

    # Non-firearm: a plain Bin sum across EVERY warehouse, custody included —
    # so whatever is parked in custody has to come back off it. No-op,
    # and no query, on a site with no custody warehouse.
    total = max(0, int(get_item_stock([item_code]).get(item_code) or 0))
    total = max(0, total - custody_warehouse_qty(item_code))
    # A web sale's Bin exit posts at completion (the DN stock leg), so units
    # sold on orders still awaiting fulfillment are in the Bin but not on the
    # shelf. Clamped at the funnel like every branch above, and oversold is
    # logged rather than merely swallowed — the uniform-firearm branch's rule,
    # for the same reason.
    # Units sold on other channels and not yet shipped come off the same Bin
    # (GunBroker's open ammunition orders); this channel's own are `committed`.
    committed = pending_web_line_qty(item_code) + sum(
        committed_units(item_code, exclude=CHANNEL_WOO).values())
    if committed > total:
        frappe.logger("woo.sync").warning(
            f"stock_for_item({item_code!r}): {committed} unit(s) on pending web "
            f"order(s) against {total} in the Bin — the shelf is oversold; "
            f"reporting 0 to WooCommerce."
        )
    return max(0, total - committed)


def store_stock_for_item(item_code: str) -> dict | None:
    """``store_stock_for_item`` hook: the store's own sellable stock of an Item, for
    ffl_integrations' source switcher when the store's stock competes with the
    distributors — ``{"qty": local_stock_for_item, "serials": ...}``, so the quantity
    it compares is the one the storefront would publish (web lines on the
    distributors' lanes never take a unit off this shelf — see
    :func:`pending_web_line_qty`). ``serials``: a uniform-price firearm's
    :func:`free_serials` (its cost is theirs), else None.

    None — the store takes no part — for a gun priced serial by serial
    (``is_per_serial_priced``, the one test both apps share) and for a read that
    fails (logged once an hour): never raises, so no pass or view fails on it."""
    try:
        item_row = frappe.db.get_value(
            "Item", item_code, ["is_firearm", "item_group", "uniform_sell_price"],
            as_dict=True, cache=True)
        if not item_row or is_per_serial_priced(item_row):
            return None
        serials = free_serials(item_code) if _is_firearm_row(item_row) else None
        return {"qty": local_stock_for_item(item_code, item_row=item_row,
                free=None if serials is None else len(serials)),
            "serials": serials}
    except Exception:
        try:
            key = f"woo_store_stock_failed:{item_code}"
            if not frappe.cache().get_value(key):
                frappe.cache().set_value(key, 1, expires_in_sec=3600)
                frappe.log_error(title=f"Store stock unreadable ({item_code})",
                    message=frappe.get_traceback())
        except Exception:
            pass  # the log is best effort
        return None


def web_price_for_item(item_code: str, standard_rate=None) -> float:
    """The regular price an *item-level* WooCommerce product carries.

    ``ffl_integrations`` registers ``distributor_web_price``
    (``fn(item_code) -> float | None``): a drop-ship listing's website price,
    computed from the distributor's cost by the markup rules — which the store's
    own price tag (``Item.standard_rate``) deliberately is not.
    The first non-``None`` answer wins; ``None`` — every item that
    is not a drop-ship listing — is ``standard_rate``, exactly as before.

    One function for every writer and checker of that price (the push payload,
    push_item's unpriced guard, reconcile's drift check), so they cannot
    disagree about what the store should show.  ``standard_rate`` may be passed
    when the caller already holds it.
    """
    price = listing_web_price(item_code)
    if price is not None:
        return price
    if standard_rate is None:
        standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
    return float(standard_rate or 0)


def listing_web_price(item_code: str) -> float | None:
    """The ``distributor_web_price`` answer alone — a drop-ship listing's website
    price, or ``None`` when the Item is priced by its own tag."""
    for fn in frappe.get_hooks("distributor_web_price"):
        price = frappe.get_attr(fn)(item_code)
        if price is not None:
            return float(price)
    return None


def dropship_source(item_code: str, *, lead: bool = True) -> dict | None:
    """Who ships this Item: ``{distributor, sku, fulfillment_mode, lead_days}`` from
    ffl_integrations' ``dropship_source_for_item`` hook, or None when the store
    does (or the site has no registrant). The dict is the whole contract — this
    app never reads the Distributor Listing table itself. ``lead``: False when
    the caller has no use for the lead time (it comes back blank)."""
    for fn in frappe.get_hooks("dropship_source_for_item"):
        # ``lead`` only when declined, so a registrant written to fn(item_code)
        # still answers the push.
        return frappe.get_attr(fn)(item_code, **({} if lead else {"lead": False}))
    return None


# "Not read yet" for the listing-meta helpers below, whose ``src`` may be None
# (the store ships it).
_UNREAD = object()


def _listing_source(item_code: str) -> dict | None:
    """``dropship_source`` for the listing push: None when the store ships it; a
    failing answer is ``{}`` (the Direct default, no lead time) rather than a
    failed push — logged, once an hour per item. (Checkout lets the same failure
    raise: an order line must not be recorded under a lane nobody answered for.)"""
    try:
        return dropship_source(item_code)
    except Exception:
        try:
            key = f"woo_dropship_source_failed:{item_code}"
            if not frappe.cache().get_value(key):
                frappe.cache().set_value(key, 1, expires_in_sec=3600)
                frappe.log_error(title=f"Drop-ship source unreadable ({item_code})",
                    message=frappe.get_traceback())
        except Exception:
            pass  # the log is best effort; the push goes on
        return {}


def _listing_fulfillment_mode(item_code: str, src=_UNREAD) -> str:
    """The item's fulfillment mode ("Direct" / "To Store"), defaulting to "Direct".

    The resolved mode rides the listing push into WooCommerce as ``_osa_fulfillment_mode``
    so wp-admin can badge which products drop-ship and which are procured to
    the store. Read through the ``dropship_source_for_item`` hook
    (:func:`dropship_source`); a failing answer, or one naming no mode, is
    "Direct". The store shipping it (automatic sourcing sells its own stock:
    the hook's None) is "" — the storefront reads anything but Direct / To Store
    as store stock, so it promises no drop-ship.
    """
    src = _listing_source(item_code) if src is _UNREAD else src
    if src is None:
        return ""
    return str(src.get("fulfillment_mode") or "Direct").strip() or "Direct"


def _listing_lead_days(item_code: str, src=_UNREAD) -> str:
    """The item's lead time as ``"min-max"`` business days, or ``""``.

    Rides the listing push into WooCommerce as ``_osa_lead_days`` so the product
    page can promise "3–7 business days" for a drop-ship. ffl_integrations
    formats it (hook, see :func:`_listing_fulfillment_mode`); any failure
    degrades to ``""`` — the shop shows no lead time rather than a wrong one.
    """
    src = _listing_source(item_code) if src is _UNREAD else src
    return str((src or {}).get("lead_days") or "")


def _item_attribute_inputs(item) -> dict:
    """Builder kwargs that decide an Item product's ``pa_*`` attributes.

    ONE place for them, shared by :func:`item_to_product_payload` and
    :func:`item_attributes_payload`, so a full push and an attributes-only
    backfill can never disagree about what the product's attributes are.
    """
    return {
        "is_firearm": _is_firearm_row(item),
        # Read off the Item doc already in hand rather than via
        # ffl_core.firearm.is_antique_item(), which would spend a second DB read
        # on a fact this document carries. Paired with is_firearm, the builder
        # applies exactly _is_regulated_firearm_row's rule.
        "is_antique": bool(item.get("is_antique")),
        "is_used": bool(item.get("is_used")),
        "condition_grade": (item.get("condition_grade") or ""),
        "caliber": (item.get("caliber") or ""),
        "item_group": (item.get("item_group") or ""),
        "manufacturer": (item.get("manufacturer") or ""),
        "model_name": (item.get("model_name") or ""),
    }


def _serial_attribute_inputs(serial, item) -> dict:
    """Builder kwargs that decide a per-serial product's ``pa_*`` attributes
    (shared by :func:`serial_to_product_payload` and
    :func:`serial_attributes_payload` — see :func:`_item_attribute_inputs`).

    Caliber / Manufacturer / Model: serial-level first, fall back to item-level.
    Antique-ness is a per-SKU fact, so it comes off the PARENT Item (the same
    doc ``is_used`` is read from), never the Serial No; the grade is per gun.
    """
    return {
        "is_antique": bool(item.get("is_antique")),
        "is_used": bool(item.get("is_used")),
        "condition_grade": (serial.get("condition_grade") or ""),
        "caliber": serial.get("caliber") or item.get("caliber") or "",
        "manufacturer": serial.get("manufacturer") or item.get("manufacturer") or "",
        "model_name": serial.get("model_name") or item.get("model_name") or "",
    }


def item_attributes_payload(item_code: str, attr_ids: dict[str, int] | None = None) -> dict:
    """``{"attributes": [...]}`` for a uniform-price firearm Item's product — and
    nothing else.

    For the condition backfill: a machine (lean) push never carries
    ``attributes`` (Woo-owned after first publish), so a grade / ``is_used`` fix
    would otherwise never reach an existing product. Built through
    :func:`build_simple_product_payload` from the same inputs the full push uses,
    so the list is exactly what a manual full push would send — but name,
    description, images and price never leave this function.
    """
    item = frappe.get_doc("Item", item_code)
    payload = build_simple_product_payload(
        item_code=item_code, item_name=item_code, attr_ids=attr_ids,
        **_item_attribute_inputs(item))
    return {"attributes": payload["attributes"]} if "attributes" in payload else {}


def serial_attributes_payload(serial_no: str, attr_ids: dict[str, int] | None = None) -> dict:
    """``{"attributes": [...]}`` for a per-serial firearm's product — and nothing
    else. The serial twin of :func:`item_attributes_payload`."""
    serial = frappe.get_doc("Serial No", serial_no)
    item = frappe.get_doc("Item", serial.item_code)
    payload = build_serial_product_payload(
        item_code=serial.item_code, serial_no=serial_no, item_name=serial.item_code,
        attr_ids=attr_ids, **_serial_attribute_inputs(serial, item))
    return {"attributes": payload["attributes"]}


def item_to_product_payload(
    item_code: str,
    attr_ids: dict[str, int] | None = None,
    site: str = "retail",
    brand_id: int | None = None,
    shipping_class: str | None = None,
    listing: bool = False,
) -> dict:
    """Fetch an Item by code and return a WooCommerce product payload.

    Uses :func:`build_simple_product_payload` for non-firearm items and
    uniform-price firearms.  Raises :class:`ValueError` for per-serial
    firearms — use :func:`serial_to_product_payload` for those.

    ``site`` — ``'retail'`` (default, the only store) selects the product-id
    fields; the MAP meta is retail-only.

    ``attr_ids`` — optional ``{slug: id}`` map of global WC taxonomy IDs
    (fetched once by :func:`ffl_woo_sync.woocommerce.sync._get_attr_ids` and
    threaded through to avoid per-item HTTP round-trips).

    ``brand_id`` / ``shipping_class`` — resolved by the caller (the sync layer
    owns the live client) and passed through to the builder.

    ``listing`` — this is a dropship listing push, the only kind that writes
    ``_osa_restricted_states`` (and then ALWAYS, so clearing the field reaches
    the store).  A plain retail push leaves that meta untouched.  The field
    is an ffl_integrations custom field, so ``item.get`` (never
    ``frappe.db.get_value``) reads it — on a site without that app it is absent,
    which is indistinguishable from "no restrictions".
    """
    item = frappe.get_doc("Item", item_code)

    is_firearm_flag = _is_firearm_row(item)
    uniform = bool(item.get("uniform_sell_price"))

    if is_firearm_flag and not uniform:
        raise ValueError(
            f"Item {item_code!r} is a per-serial firearm — "
            "call serial_to_product_payload() for each Serial No instead."
        )

    qty = stock_for_item(item_code)  # safe: raises already handled above
    gallery, image = _publicize_listing_images(item, doctype="Item")
    image_srcs = _resolve_image_list(gallery, image)

    src = _listing_source(item_code) if listing else None
    payload = build_simple_product_payload(
        item_code=item_code,
        item_name=item.item_name or item_code,
        description=item.description or "",
        standard_rate=web_price_for_item(item_code, item.standard_rate),
        sale_price=item.get("sale_price"),
        sale_from=item.get("sale_from"),
        sale_to=item.get("sale_to"),
        stock_qty=qty,
        **_item_attribute_inputs(item),
        upc=(item.get("upc") or ""),
        image_srcs=image_srcs,
        attr_ids=attr_ids,
        restricted_states=(item.get("restricted_states") or "") if listing else None,
        fulfillment_mode=_listing_fulfillment_mode(item_code, src) if listing else None,
        lead_days=_listing_lead_days(item_code, src) if listing else None,
        brand_id=brand_id,
        shipping_class=shipping_class,
    )
    if listing and src is None:
        # The store ships it (automatic sourcing sells its own stock): the drop-ship
        # shipping class comes off, and the store's own shipping applies.
        payload["shipping_class"] = ""
    return _with_extension_meta(_with_map_meta(payload, item), "Item", item_code)


def serial_to_product_payload(
    serial_no: str,
    attr_ids: dict[str, int] | None = None,
    site: str = "retail",
) -> dict:
    """Fetch a Serial No by name and return a WooCommerce product payload.

    Uses :func:`build_serial_product_payload`.  The parent Item is fetched to
    resolve item-level attributes (name, description, uniform pricing, caliber,
    ``is_used``, image).

    Title (``name``): a uniform-price firearm shares one parent-Item product, so
    its title comes from ``Item.item_name``.  A per-serial firearm is one product
    per physical gun, so each uses its own ``Serial No.item_name`` (the per-gun
    title), falling back to the Item name then the item code.

    Pricing via ``ffl_core.firearm.serial_sell_price``:  uniform-price items
    use ``Item.standard_rate``; per-serial items use ``Serial No.sell_price``.

    Caliber: serial-level first, falls back to item-level.

    Images: serial ``image_gallery`` rows first; if empty, falls back to the
    serial's single ``image`` field, then the item's ``image_gallery``, then the
    item's single ``image`` field.

    ``site`` — ``'retail'`` (default, the only store) selects the product-id
    fields; the MAP meta is retail-only.

    ``attr_ids`` — optional ``{slug: id}`` map of global WC taxonomy IDs.
    """
    # Imported here, not at module scope, for the same reason `free_units` does
    # it: the rulebook is patched per-test as `ffl_core.reservations.counter_holds`,
    # which only reaches a call-time lookup.
    from ffl_core.reservations import counter_holds
    from ffl_core.warehouses import is_consigned_out

    serial = frappe.get_doc("Serial No", serial_no)
    item = frappe.get_doc("Item", serial.item_code)

    uniform = bool(item.get("uniform_sell_price"))

    sell_price = serial_sell_price(
        item.get("uniform_sell_price"),
        item.get("standard_rate"),
        serial.get("sell_price"),
    )
    # Promotional price: same two-level rule as the regular price above.
    sale_price, sale_from, sale_to = serial_sale_fields(
        item.get("uniform_sell_price"),
        item.get("sale_price"), item.get("sale_from"), item.get("sale_to"),
        serial.get("sale_sell_price"), serial.get("sale_from"), serial.get("sale_to"),
    )

    # Product title (``name``): a uniform-price firearm is sold as one shared
    # parent-Item product, so its title comes from the Item.  A per-serial firearm
    # is one product per physical gun, so each uses its own Serial No.item_name
    # (the per-gun title), falling back to the Item name then the item code.
    if uniform:
        product_name = item.item_name or serial.item_code
    else:
        product_name = serial.get("item_name") or item.item_name or serial.item_code

    # Image resolution: serial gallery → serial image → item gallery → item image.
    # Private uploads are flipped public first — WooCommerce sideloads anonymously.
    gallery, image = _publicize_listing_images(serial, doctype="Serial No")
    image_srcs = _resolve_image_list(gallery, image)
    if not image_srcs:
        gallery, image = _publicize_listing_images(item, doctype="Item")
        image_srcs = _resolve_image_list(gallery, image)

    # Description: per-serial first, fall back to the parent item's.  Mirrors the
    # serial-wins-with-item-fallback rule used above for caliber / manufacturer /
    # model.  Serial No.description is a plain-text Text field, so render its
    # newlines → <br> and HTML-escape it for the storefront; the item's
    # description is already HTML and passes through untouched.
    serial_desc = (serial.get("description") or "").strip()
    if serial_desc:
        description = "<br>\n".join(
            frappe.utils.escape_html(line) for line in serial_desc.splitlines()
        )
    else:
        description = item.description or ""

    # SOLD lifecycle.  Imported lazily: sync imports this module at
    # module scope, so a top-level import back into sync would be circular.
    # The archive flag is PER SITE.
    from ffl_woo_sync.woocommerce.sync import _f, is_relisted_state, is_sold_state
    archived = serial.get(_f(site, "sold_archived"))

    payload = build_serial_product_payload(
        item_code=serial.item_code,
        serial_no=serial_no,
        item_name=product_name,
        description=description,
        sell_price=float(sell_price or 0),
        sale_price=sale_price,
        sale_from=sale_from,
        sale_to=sale_to,
        **_serial_attribute_inputs(serial, item),
        upc=(item.get("upc") or ""),
        woo_reserved=bool(serial.get("woo_reserved")),
        # ANOTHER channel's hold — resolved through the one predicate rather
        # than by naming a field this app does not own. Without it a gun sold on
        # GunBroker keeps publishing stock 1 on the storefront until the delist
        # lands, and if that delist fails, forever.
        #
        # `own=CHANNEL_WOO` is load-bearing, and the two parameters below it are
        # why the distinction cannot be collapsed: `woo_reserved` and
        # `channel_reserved` both zero the stock, but only `channel_reserved`
        # sends the page to DRAFT. Our own pending web order must keep a
        # published page — the customer is looking at it, and it becomes the
        # SOLD archive when the order completes.
        channel_reserved=other_channel_reservation(
            serial_no, own=CHANNEL_WOO,
            row={f: serial.get(f) for f in channel_reserve_columns()},
        ) is not None,
        # An open COUNTER order. `stock_for_item`'s uniform-price branch has
        # subtracted these since then; this per-serial path — the OTHER half of
        # the same registry — never did, which is the direct trigger of
        # DISP-2026-02000: a gun parked on a Draft POS Invoice with a pending
        # 4473 kept publishing stock 1 for eleven days and sold online.
        #
        # `counter_holds`, NOT the whitelisted `reserved_counter_serials`: this
        # runs inside the Guest webhook request too, and the gated wrapper
        # answers a Guest with a silent {} that reads as "nothing is held"
        # (reservations.py:110 spells out why {} is the worst possible answer
        # for a caller that DECIDES something).
        counter_held=bool(counter_holds(serial.item_code).get(serial_no)),
        # At a partner FFL on consignment (flag or location — the two-key rule):
        # Active, but not ours to sell here. Without it a consigned gun pushed by
        # the listing flow went out publish + stock 1 (7237k / 157598, 2026-10).
        consigned_out=is_consigned_out(
            consigned_out=serial.get("consigned_out"), warehouse=serial.get("warehouse")),
        is_active=(serial.status == "Active"),
        sold_archived=is_sold_state(serial.status, archived),
        clear_sold=is_relisted_state(serial.status, archived),
        image_srcs=image_srcs,
        attr_ids=attr_ids,
    )
    return _with_extension_meta(_with_map_meta(payload, item), "Serial No", serial_no)
