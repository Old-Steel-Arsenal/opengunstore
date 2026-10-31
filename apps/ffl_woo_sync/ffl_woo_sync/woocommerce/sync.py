"""WooCommerce push / delist / relist orchestration.

Public surface
--------------
* :func:`push_item`      — create or update an item-level WC product
* :func:`push_serial`    — create or update a per-serial WC product
* :func:`push_all_serials` — push every Active Serial No for an item
* :func:`push_stock`     — lightweight: update stock_quantity only
* :func:`delist_item`    — set item WC product to ``status=draft`` + qty 0
* :func:`delist_serial`  — set serial WC product to ``status=draft`` + qty 0
* :func:`archive_serial_sold` — SOLD archive: keep a sold serial's page live
  (``publish`` + qty 0 + ``_osa_sold_*`` meta) instead of drafting it to a 404
* :func:`route_serial_exit` — archive-or-delist decision for a serial leaving
  the shelf; the single source of truth every caller must ask
* :func:`relist_item`    — re-publish a delisted item (full push)
* :func:`relist_serial`  — re-publish a delisted serial (full push)

All public functions accept an optional ``client`` argument so callers (tests,
``client_api.py``) can inject a pre-built :class:`.WooCommerceClient` and avoid
per-call config reads.  When ``None`` is passed a fresh client is built from the
WooCommerce Settings doctype.

``attr_ids`` pre-fetching
--------------------------
Each push function calls :meth:`.WooCommerceClient.get_attribute_ids` once
(``GET /wc/v3/products/attributes → {slug: id}``) and threads the result into
the mapping layer so pushed products link to the real ``pa_caliber`` /
``pa_condition`` global WooCommerce taxonomies.  Callers that process *many*
records in a loop (e.g. :func:`push_all_serials`, ``reconcile_now``) should
pre-fetch and pass ``attr_ids=`` directly to avoid an extra HTTP round-trip per
record.

Retry policy
------------
Each WooCommerce HTTP call is wrapped in :func:`_push_with_retry`: up to
``_retries`` additional attempts (default **2**, so up to 3 total) with
exponential back-off on **transient** errors (network failure or HTTP 5xx).
4xx errors (bad payload, auth failure) fail immediately — retrying a
deterministic rejection wastes operator time.  On permanent failure
``woo_sync_error`` is written to the Frappe record and the exception
is re-raised so callers can surface it.

Write-back
----------
On success each function writes to the Frappe DB via ``frappe.db.set_value``
so the Item / Serial No document reflects the current sync state without a
full doc reload.
"""
from __future__ import annotations

import re
import time

import frappe
import requests

from ffl_core.custody import is_custody_serial
from ffl_core.warehouses import is_consigned_out
from ffl_core.firearm import _is_firearm_row
from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction
from ffl_core.reservations import (
    CHANNEL_WOO,
    channel_reservation,
    other_channel_reservation,
)

from ffl_woo_sync.woocommerce.client import (
    RETAIL_SITE,
    SITES,
    WooCommerceClient,
    WooCommerceError,
    store_entry,
)
from ffl_woo_sync.woocommerce import mapping


# Serial No statuses that mean "this gun sold and left the building".
#
# ERPNext derives the terminal status from the VOUCHER that moved the stock, not
# from the business meaning of the sale — see
# ``erpnext.stock.serial_batch_bundle.get_status_for_serial_nos``: a negative SLE
# yields ``Delivered`` UNLESS it came from a Stock Entry whose purpose is in
# ``CONSUMED_SERIAL_NO_STOCK_ENTRY_PURPOSES`` (which includes **Material Issue**),
# in which case it yields ``Consumed``.  Our two exit paths therefore land on
# different statuses for the same event:
#
#   counter / B2B  → POS Invoice / Sales Invoice        → ``Delivered``
#   web order      → FFL Disposition → Material Issue   → ``Consumed``
#                    (ffl_core.compliance.disposition_stock.issue_stock_on_submit)
#
# Both are sales.  Shipping only ``Delivered`` made every WEB sale route to
# delist — the flagship "web sale → SOLD page" path failed silently in prod
# while the counter path worked.  Keep this a TUPLE: a bare string would make
# ``in`` a substring test, so ``"Deliver"`` would read as sold.
SOLD_STATUS = ("Delivered", "Consumed")
ACTIVE_STATUS = "Active"

# The ONLY channel that carries SOLD archive pages: RETAIL_SITE.

# Outcomes of :func:`route_serial_exit`.
EXIT_ARCHIVE = "archive"
EXIT_DELIST = "delist"
EXIT_SKIP = "skip"  # already drafted on purpose — leave the page (and its 301) alone

# The third exclusion axis (2026-07-20): a customer's gun held in custody for an
# FFL transfer. Deliberately NOT built from `delisted` (which a human may undo —
# nobody here may list someone else's property) nor from `unpriced` (which asks
# to be fixed by pricing it — this record is correct and still unsellable).
# Its structural sibling is `consigned_out`: cross-site, flag-driven, and Active
# indefinitely, so no status filter can ever find it.
TRANSFER_CUSTODY = "transfer_custody"

# A uniform-priced firearm lists as ONE item-level product carrying a quantity
# in stock — it has no per-serial product at all (routing rule, authoritative in
# client_api.push_item_now: firearm & uniform_sell_price=0 → per-serial;
# uniform_sell_price=1 / non-firearm → item-level).  So a per-serial product must
# never be MINTED for such an Item.
#
# The 2026-07-21 flood: a caliber-cleanup
# batch drove a bulk per-serial push over ~40 uniform Items and, because
# push_serial had no such guard, created 122 ghost `item_code::serial` products —
# each publish + instock, double-counting stock against the item-level model post
# (German P226 Grade B: model post 6 + 7 ghosts = 13 buyable for 7 real guns).
#
# Like TRANSFER_CUSTODY this is a STRUCTURAL fact about the parent Item, not a
# "should we list it now?" question — so `_relist` cannot lift it.  Scoped to the CREATE path only: a legacy per-serial
# product that ALREADY exists (product_id set — e.g. one held for an in-flight
# order) stays reachable for update / delist / oos so the system can manage
# it down; only a brand-new create is refused.
UNIFORM_ITEM = "uniform_item"

# Another SALES CHANNEL holds this gun — a GunBroker buyer, say. Answered by `ffl_core.reservations.channel_reservation`, the
# one predicate every channel gate in both apps reads, so adding a third channel
# later means adding a row there and nothing here.
#
# Ranked with TRANSFER_CUSTODY rather than with `delisted`: `_relist` may not
# lift it. "May a machine republish this?" is a question about our intent;
# "has somebody already bought it?" is a question about the world, and a button
# cannot answer it. The GunBroker side carries the mirror-image guard against
# a Woo-reserved gun, so neither channel can publish over the other's sale.
CHANNEL_RESERVED = "channel_reserved"

# At a partner FFL on consignment (`ffl_core.warehouses.is_consigned_out`: the
# `consigned_out` flag OR a consignment warehouse). Active the whole time it is
# away, so only reconcile Pass 3b ever took its page down — and the listing flow
# could publish it again at stock 1 (7237k / 157598, consigned 2026-09-16, pushed
# later). Same shape and rank as CHANNEL_RESERVED: `_relist` cannot lift it, a
# CREATE or a full push is refused, and a machine (`pos_fields_only`) update of an
# existing product passes only because its payload is stock 0 + draft — which is
# re-checked against the built payload, not assumed.
CONSIGNED_OUT = "consigned_out"

# WHY THIS APP ASKS `other_channel_reservation(own=CHANNEL_WOO)` and not
# `channel_reservation`, at every read point in it:
#
# A gun held by one of OUR OWN web orders is mid-fulfilment — stock 0, page
# still PUBLISHED, and a SOLD archive page waiting for it at the end. A gun held
# by GunBroker has left this store's shelf entirely and its page must go to
# draft. Reading our own reservation as "another channel has it" takes the
# listing down for every pending web order, and the gun then loses that archive
# page for good, because `route_serial_exit` answers EXIT_SKIP for anything
# already carrying `{site}_delisted=1`.


def _f(site: str, key: str) -> str:
    """Return the Frappe field name for *key* on the given *site* channel (``client.STORES``);
    ``client.UnknownStoreError`` for a store this POS does not run."""
    return store_entry(site)["fields"][key]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client(client: WooCommerceClient | None, site: str = "retail") -> WooCommerceClient:
    return client if client is not None else WooCommerceClient.from_settings(site)


def _now() -> str:
    return str(frappe.utils.now_datetime())


def _today() -> str:
    """Today as an ISO date — the value stamped into ``_osa_sold_at``."""
    return frappe.utils.nowdate()


# ---------------------------------------------------------------------------
# Sold-state model (single source of truth for the whole app)
# ---------------------------------------------------------------------------
#
# Two facts describe a per-serial firearm's shop presence:
#
#   Serial No.status          — the AUTHORITY on whether the gun sold.
#   {site}_sold_archived      — a record that the WC page is CURRENTLY rendered
#                               as SOLD (publish + stock 0 + ``_osa_sold_*``).
#
# The two predicates below are mutually exclusive by construction, so no caller
# has to re-derive "is it sold?" and get it subtly different:
#
#   in SOLD_STATUS       → sold          (archive / keep the page SOLD;
#                                         Delivered = counter, Consumed = web)
#   flag & not Active    → sold          (status hand-edited off a sold value —
#                                         don't let a sold page drift back into
#                                         an unmanaged zombie)
#   flag & Active        → RELISTED      (returns / undispose — clear the sale)
#   no flag & Active     → ordinary live listing

def is_sold_state(status: object, sold_archived: object = 0) -> bool:
    """True when the storefront page must present as SOLD."""
    if (status or "") in SOLD_STATUS:
        return True
    return bool(int(sold_archived or 0)) and (status or "") != ACTIVE_STATUS


def is_relisted_state(status: object, sold_archived: object = 0) -> bool:
    """True when a previously-archived gun is back on the shelf, so the sale
    must be wiped from the listing (sentinel + ``_osa_sold_*`` meta)."""
    return bool(int(sold_archived or 0)) and (status or "") == ACTIVE_STATUS


def route_serial_exit(serial_no: str, *, status: object = None, site: str = RETAIL_SITE) -> str:
    """Decide what happens to a per-serial firearm's WC page when the gun
    leaves the shelf — the SINGLE SOURCE OF TRUTH for every caller.

    First match wins:

    1. Status not in :data:`SOLD_STATUS` → :data:`EXIT_DELIST`.  It was removed, not sold
       (non-Active for another reason, or Active-but-consigned-out).
    2. Sold, but ``{site}_delisted=1`` → :data:`EXIT_SKIP`.
       Something deliberate already drafted this page — another channel taking
       the gun (a GunBroker sale), or an operator pulling it from the web — and it carries a
       an auto-301.  Republishing it would resurrect a page the business took
       down AND delete that redirect.  Either 200 **or** 301 is acceptable.  This
       generalizes Pass 3a's ``delisted=0`` to every call site, so the flag
       means one thing everywhere: *do not auto-republish*.  The backfill
       is the single, owner-gated exception — which is why this guard lives here
       and NOT in :func:`archive_serial_sold` (a sink-level check would refuse
       exactly the serials the backfill exists to rescue, SP000279 among them).
    3. Sold and still live → :data:`EXIT_ARCHIVE`.  Includes a consigned-out gun
       that sold at the partner store, which is why the sold check runs BEFORE any
       ``consigned_out`` reasoning.

    Accepted side effect: a gun manually pulled from the web
    and later sold at the counter stays draft + 301 instead of gaining a SOLD
    page.  The backfill can surface it explicitly if the owner wants it.

    ``status`` may be passed in by callers that already SELECTed it (reconcile,
    the SLE hook) to avoid a per-row query.
    """
    # NOTE (custody, 2026-07-20): a custody gun leaving stock went HOME, not to a
    # buyer, so it must never gain a "SOLD for $X" page — but that check does NOT
    # live here. This function has a deliberate, tested read budget (zero reads on
    # the common non-sold path, exactly one on the sold path), and a custody
    # lookup would spend a read on every reconciled row to change the answer for
    # almost none of them. It is enforced instead where it is free or already
    # needed: sync_serial_listing_task decides from the Serial No row it has
    # already fetched, reconcile Pass 3b selects custody guns into the delist arm
    # directly, and push_serial refuses to publish one in the first place.
    if status is None:
        status = frappe.db.get_value("Serial No", serial_no, "status")
    if (status or "") not in SOLD_STATUS:
        return EXIT_DELIST
    # Only the archive decision consults the delist sentinel — a non-sold serial
    # keeps its existing delist behaviour, drafted or not.
    if frappe.db.get_value("Serial No", serial_no, _f(site, "delisted")):
        return EXIT_SKIP
    return EXIT_ARCHIVE


def _apply_category(wc: WooCommerceClient, payload: dict, item_code: str,
        site: str = RETAIL_SITE) -> None:
    """Set ``payload["categories"]`` to the store's category for the Item, in place.

    The category is the catalog master's tree node the Item files under (its
    ``catalog_category``, else its Item Group's node) → that node's linked term on
    this store (``woocommerce.categories``). No-op when the store does not follow
    the tree (WooCommerce Settings › Follow the catalog category tree), the Item has no node or
    the store has no term for it or any parent: an update then keeps the
    product's categories as they are. WooCommerce requires category **ids** on
    products. This lives in the sync layer (not ``mapping.py``) because it
    needs a live client; ``mapping.py`` stays pure / IO-free.
    """
    from ffl_woo_sync.woocommerce import categories

    if not categories.follows_tree(site):
        return  # this store's categories are not the tree's (its setting): leave them
    slug = categories.item_category(item_code)
    term = categories.term_for(wc, site, slug) if slug else None
    if term:
        payload["categories"] = [{"id": term}]


# Cache keys are per SITE: should a second store ever be connected, it is a
# separate WooCommerce install that can differ in version and in which shipping
# classes exist, so one store's answer must never stand in for another's.
_UNSUPPORTED_BRANDS_KEY = "woo_sync:brands_unsupported:{site}"
_SHIPPING_CLASSES_KEY = "woo_sync:shipping_classes_ok:{site}"
# A listing batch is hundreds of SKUs spread over as many jobs, each with its own
# client, so the per-client memo buys nothing there.  These two facts about the
# STORE (does it do brands; do the shipping classes exist) are cached process-wide.
_TAXONOMY_CACHE_TTL = 3600


def _forget_taxonomy_cache(site: str | None = None) -> None:
    """Drop the cross-process taxonomy facts (tests, and after a manual fix).

    ``site=None`` clears every channel — what a test or an operator fixing the
    store actually wants.
    """
    sites = [site] if site else list(SITES)
    for template in (_UNSUPPORTED_BRANDS_KEY, _SHIPPING_CLASSES_KEY):
        for one in sites:
            frappe.cache().delete_value(template.format(site=one))


def _degraded(title: str, message: str) -> None:
    """Record a degradation where a human will actually find it.

    ``frappe.logger`` writes to a bench log file nobody opens, and the success
    path clears ``{site}_sync_error``, so a silently degraded push leaves no
    trace on the Item either.  Shipping class in particular is money: a product
    listed without its class ships at the store's default rate, and 500 SKUs
    could do it unnoticed.
    """
    frappe.logger("woo.sync").warning(f"{title}: {message}")
    try:
        frappe.log_error(title=title[:140], message=message)
    except Exception as exc:
        # The reporter must never be louder than what it reports.  Writing an
        # Error Log is itself a document insert: it can fail (locked table, a
        # half-installed site, a caller that has stubbed the DB out), and
        # letting that escape would turn "this product has no brand term" into
        # "the product push crashed" — precisely the inversion this function
        # exists to prevent.  The warning above has already been emitted.
        frappe.logger("woo.sync").warning(f"log_error failed for {title!r}: {exc}")


DROPSHIP_FIELD = "dropship_distributor"


def _dropship_distributor(item_code: str) -> str:
    """``Item.dropship_distributor``, or ``""`` when the site has no such column.

    ``dropship_distributor`` is an ffl_integrations custom field: reading it on a
    site without that app is a SQL error, so the column is probed first with
    ``frappe.db.has_column`` (cached per table) — the same guard
    ``ffl_core.warehouses.custody_warehouse_names`` uses.

    Deliberately *not* a ``get_meta`` field probe: building a Meta reads through
    ``frappe.db.get_value``, which drags Frappe's whole document loader into
    every unit test that stubs that function, and this read exists precisely to
    be cheap and side-effect free.

    Type-narrowed to ``str`` because that is what a Link column holds.  Anything
    else means "the read did not answer", never "yes, this is a dropship item" —
    and a wrong yes writes a brand term onto a product nobody asked to change.
    """
    if not frappe.db.has_column("Item", DROPSHIP_FIELD):
        return ""
    value = frappe.db.get_value("Item", item_code, DROPSHIP_FIELD)
    return value.strip() if isinstance(value, str) else ""


def _is_distributor_listing(item_code: str, *, forced: bool = False) -> bool:
    """Is this push part of the dropship listing pipeline?

    Gates the two fields that must not leak into ordinary retail pushes:
    the ``product_brand`` term and the always-present
    ``_osa_restricted_states`` meta.  ``brands`` is replace-semantics on a live
    taxonomy, so an unattended reconcile writing it store-wide would overwrite
    hand-curated brand assignments with POS spellings — a site-wide decision
    that deserves its own change, not a ride along with P3-dropship.

    True when:

    * ``forced`` — the push came through ``distributor_listing_push``; or
    * the Item has a ``dropship_distributor`` — so reconcile / doc_event pushes
      of a dropship SKU stay consistent (a cleared field would otherwise never
      reach the store).

    Nothing else, and deliberately so.  This runs on **every** item push, so it
    is two cheap reads and no document/Meta machinery.  A settings-singleton
    read looks innocent, but ``frappe.db.get_single_value`` builds a Meta for
    the Single doctype, and building a Meta loads a DocType *document* through
    ``frappe.db.get_value`` — which drags Frappe's document loader into every
    caller, and into every test that stubs that function.  A store-wide brand
    rollout is a deliberate separate change; it can bring its own switch, and
    its own G4 evidence, then.
    """
    return bool(forced or _dropship_distributor(item_code))


def _brand_id_for_item(
    wc: WooCommerceClient, item_code: str, *, site: str = "retail"
) -> int | None:
    """Resolve ``Item.brand`` to a WooCommerce ``product_brand`` term id.

    A live client, create-if-absent, memoized — it returns the id rather than writing the payload, because
    the builder takes ``brand_id`` as a plain value and stays IO-free.

    Returns ``None``, and never raises, when the item has no brand or the store
    cannot answer: ``/products/brands`` needs WooCommerce ≥ 9.4 (or the Brands
    plugin) and 404s otherwise.  A brand is metadata; losing it must not take a
    product push down with it — including on a transport error, which the client
    does NOT wrap in :class:`WooCommerceError`.

    A 404 is remembered for an hour: it is a fact about the STORE, not about
    this item, and re-probing it once per SKU is pure latency.  Any other
    failure is per-item and is deliberately not cached.
    """
    brand = (frappe.db.get_value("Item", item_code, "brand") or "").strip()
    if not brand:
        return None
    unsupported_key = _UNSUPPORTED_BRANDS_KEY.format(site=site)
    if frappe.cache().get_value(unsupported_key):
        return None
    try:
        return wc.get_or_create_brand(brand)
    except (WooCommerceError, requests.RequestException) as exc:
        if getattr(exc, "status_code", None) == 404:
            frappe.cache().set_value(
                unsupported_key, 1, expires_in_sec=_TAXONOMY_CACHE_TTL)
            _degraded(
                "Woo brands unsupported on this store",
                f"GET /products/brands → 404 while resolving {brand!r} for Item "
                f"{item_code!r}. Products still push; the brand term is omitted. "
                "WooCommerce >= 9.4 (or the Brands plugin) is required.",
            )
            return None
        frappe.logger("woo.sync").warning(
            f"brand term {brand!r} unresolved for Item {item_code!r} — "
            f"pushing without a brand ({exc})"
        )
        return None


def _ensure_dropship_shipping_classes(
    wc: WooCommerceClient, table: dict[str, str], *, site: str = "retail"
) -> set[str]:
    """Ensure every dropship shipping class exists; return the slugs the store now has.

    One GET of the whole table plus a create for whatever is missing, cached
    across processes for an hour — three slugs that change approximately never,
    which a listing batch would otherwise re-check for every SKU it pushes.
    """
    cache_key = _SHIPPING_CLASSES_KEY.format(site=site)
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return set(cached)

    index = wc.list_shipping_classes()
    for slug, label in table.items():
        if slug not in index:
            index[slug] = wc.get_or_create_shipping_class(slug, name=label)
    slugs = sorted(index)
    frappe.cache().set_value(cache_key, slugs, expires_in_sec=_TAXONOMY_CACHE_TTL)
    return set(slugs)


def _ensure_shipping_class(
    wc: WooCommerceClient, slug: str, *, site: str = "retail"
) -> bool:
    """Make sure a shipping-class term exists before a product references it.

    WooCommerce silently ignores an unknown shipping-class slug on a product, so
    an unensured slug yields a "successful" push whose product then quietly ships
    at the default rate.  Returns False — the caller drops the slug — instead of
    raising when the term cannot be created: the listing is still worth having,
    and :func:`_degraded` makes the loss visible in the Error Log rather than in
    a bench log file.

    A slug outside the table (the caller's choice — see
    ``distributor._clean_slug``) is created on demand.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        return False

    from ffl_woo_sync.woocommerce.distributor import (
        DROPSHIP_SHIPPING_CLASSES, shipping_class_name,
    )

    try:
        known = _ensure_dropship_shipping_classes(
            wc, DROPSHIP_SHIPPING_CLASSES, site=site)
        if slug in known:
            return True
        wc.get_or_create_shipping_class(slug, name=shipping_class_name(slug))
        return True
    except (WooCommerceError, requests.RequestException) as exc:
        _degraded(
            f"Woo shipping class {slug!r} unresolved",
            f"Could not resolve or create the shipping class {slug!r} ({exc}). "
            "The product is pushed WITHOUT a shipping class and will charge the "
            "store's default shipping rate until this is fixed.",
        )
        return False


def _verify_shipping_class(result: dict, slug: str, item_code: str) -> None:
    """Compare what WooCommerce echoed back against what was asked for.

    The only place the silent drop is observable: WooCommerce accepts a product
    carrying an unknown shipping-class slug, stores nothing, and returns 200.
    Without this check the failure surfaces as a customer charged the wrong
    shipping, weeks later.
    """
    if not slug or not isinstance(result, dict) or "shipping_class" not in result:
        return
    got = (result.get("shipping_class") or "").strip().lower()
    if got != slug:
        _degraded(
            f"Woo shipping class not applied: {item_code}",
            f"Pushed shipping_class={slug!r} but WooCommerce stored {got!r}. "
            "The product will charge the store's default shipping rate.",
        )


_DEAD_IMAGE = re.compile(r"Error getting remote image (\S+?)\.?\s+Error", re.I)
_DEAD_IMAGE_TRIES = 3


def _without_dead_images(item_code: str, payload: dict, send) -> dict:
    """``send()``, and when WooCommerce refuses the product because a remote
    image is gone (``woocommerce_product_image_upload_error`` — a distributor CDN
    that dropped a gallery variant after we recorded it), drop that URL from the
    payload and from the Item's gallery, then send again (a few times: more than
    one image can be dead). Any other error, or one naming an image the payload
    does not carry, is raised as it was."""
    for attempt in range(_DEAD_IMAGE_TRIES + 1):
        try:
            return send()
        except WooCommerceError as exc:
            match = _DEAD_IMAGE.search(str(exc))
            images = payload.get("images") or []
            dead = match.group(1).replace("\\/", "/") if match else None
            if (attempt == _DEAD_IMAGE_TRIES or not dead
                    or not any(i.get("src") == dead for i in images)):
                raise
            payload["images"] = [i for i in images if i.get("src") != dead]
            frappe.db.delete("Item Image Gallery", {"parent": item_code, "parenttype": "Item",
                "image": dead})
            if frappe.db.get_value("Item", item_code, "image") == dead:
                frappe.db.set_value("Item", item_code, "image",
                    payload["images"][0]["src"] if payload["images"] else None,
                    update_modified=False)
            frappe.logger("woo.sync").info(f"{item_code}: dropped dead image {dead}")
    raise AssertionError("unreachable")


def _push_with_retry(fn, *, retries: int = 2) -> dict:
    """Call ``fn()`` (a zero-arg callable that makes ONE WC HTTP call) with
    bounded exponential back-off retry on transient failures.

    - **Transient** (retried): network error (``status_code=None``) or HTTP 5xx.
    - **Deterministic** (fail-fast): HTTP 4xx — bad payload / auth / not-found.
      Retrying a 4xx just keeps the operator waiting for a predictable failure.

    Mirrors the ``ffl_integrations.fastbound.tasks`` retry contract.
    """
    last_exc: WooCommerceError | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except WooCommerceError as exc:
            transient = exc.status_code is None or exc.status_code >= 500
            if transient and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            last_exc = exc
            break
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Woo-side delist detection
# ---------------------------------------------------------------------------
#
# ``{site}_delisted`` means "machines must not republish this", and an earlier fix
# taught every push path to honour it.  But nothing ever RAISED it from what the
# store actually showed — it could only be set by our own delist paths, so a
# product a human trashed in wp-admin was invisible to the guard.  Reconcile
# read WC(stock 0) vs POS(Active, stock 1), called it drift, and pushed: which
# un-trashed two SG 551 rifles the same night staff had removed them
# (incident 2026-07-19).
#
# A product the store says is not for sale was taken down on
# purpose.  Do not republish; write the flag back so the decision is durable and
# every existing guard picks it up.  Re-listing needs an explicit woo_push.

# Every WooCommerce product status that is not "on sale to the public".
# `trash` and `draft` are the two staff actually use; `pending` and `private`
# are included because they mean the same thing to a shopper.
HIDDEN_WC_STATUSES = frozenset({"draft", "trash", "pending", "private"})


def is_hidden_on_woo(wc_product: dict | None) -> bool:
    """True when the STORE says this product is not publicly for sale.

    Deliberately reads a positive list of hidden statuses rather than
    ``!= "publish"``: a WC response that omits ``status`` (a partial payload, a
    plugin filtering fields) must NOT be read as hidden, or one malformed
    response would delist the entire catalogue on the next sweep.
    """
    return (wc_product or {}).get("status") in HIDDEN_WC_STATUSES


def mark_woo_delisted(doctype: str, name: str, *, site: str) -> None:
    """Record that a human took this product off the shop.

    Writes the sentinel every push path already honours, plus a visible reason.
    ``{site}_listed`` is cleared for Items so reconcile Pass 1 (which selects on
    ``listed=1``) stops re-reading it; Serial No has no ``listed`` field and is
    excluded by Pass 2's ``delisted=0`` filter instead.
    """
    # Neutral by design: state WHAT was observed, not WHY. Reconcile sees a
    # product that is not live; it cannot tell a hand-trashed one from a draft
    # this code created moments earlier (an unpriced gun that has since been
    # priced can reach here). Asserting "a human deliberately delisted this"
    # would be a guess presented as a fact, on the record, in the operator's UI.
    writeback = {
        _f(site, "delisted"): 1,
        _f(site, "sync_error"): (
            "Not republished: this product is not live on WooCommerce. Push it "
            "to WooCommerce explicitly to relist it."
        ),
    }
    if doctype == "Item":
        writeback[_f(site, "listed")] = 0
    frappe.db.set_value(doctype, name, writeback)


# ---------------------------------------------------------------------------
# Price guard
# ---------------------------------------------------------------------------
#
# A gun with no price must never reach the storefront.  ``Serial No.sell_price``
# is a NOT-NULL Currency column, so "nobody set a tag price" reads as 0.00 —
# indistinguishable from a real price unless something checks.  Nothing did, so
# two SG 551 rifles ($5,400 cost) were published at $0, hand-trashed by staff,
# and republished by the 00:00 reconcile the same night (incident 2026-07-19).
#
# Skip the push, force any existing zero-priced product to draft,
# and make the reason visible in ``{site}_sync_error`` rather than only a log.

UNPRICED = "unpriced"

# The one shape "this product is off sale" takes on WooCommerce. Three call
# sites now write it (_force_draft, delist_item, delist_serial); keeping it in
# one place stops them drifting apart, which would leave products in two
# subtly different "hidden" states that reconcile reads differently.
OFF_SALE_PAYLOAD = {"status": "draft", "stock_quantity": 0}


# The two-field pricing rule moved to ffl_core (2026-08, GunBroker channel):
# GunBroker's FixedPrice must be the same number the storefront and the counter
# would charge, and a second copy of a pricing rule is how one gun ends up
# listed at two prices on two channels. Re-exported under the original name so
# every call site here — and every test that patches
# `ffl_woo_sync.woocommerce.sync.effective_serial_price` — keeps working
# unchanged; this module's own callers resolve it through the module global.
from ffl_core.firearm import effective_serial_price  # noqa: E402  (re-export)


def effective_item_price(item_code: str) -> float:
    """The price an item-level WC product would carry — what
    :func:`mapping.item_to_product_payload` puts in ``regular_price``
    (:func:`mapping.web_price_for_item`)."""
    return mapping.web_price_for_item(item_code)


def _force_draft(wc: WooCommerceClient, woo_id: str, *, _retries: int = 2) -> None:
    """Take a product off sale without deleting it — same shape as
    :func:`delist_serial`, so an un-sellable product always lands in one
    recognisable state."""
    _push_with_retry(
        lambda: wc.update_product(woo_id, dict(OFF_SALE_PAYLOAD)),
        retries=_retries,
    )


def _refuse_unpriced(
    doctype: str,
    name: str,
    *,
    client: WooCommerceClient | None,
    site: str,
    _retries: int,
    extra_writeback: dict | None = None,
) -> dict:
    """Refuse to publish an unpriced record, and stop the store selling it.

    Skipping the push alone would have fixed nothing in the incident: the $0
    products were ALREADY live, so a silent skip leaves them exactly as they
    were.  Any existing product is therefore forced to draft.  No product id →
    no HTTP at all (nothing exists to take down).
    """
    woo_id = frappe.db.get_value(doctype, name, _f(site, "product_id")) or ""
    base = (
        "Not published: no sell price is set. WooCommerce would list this at $0. "
        "Set a price, then push it to WooCommerce explicitly."
    )

    # Reason FIRST, HTTP second: the unpriced-listing guard's promise is that the reason is visible on
    # the Desk form, and writing it only after the call would drop it in exactly
    # the case where an operator most needs to know why nothing happened.
    #
    # But the first write must not claim the take-down already happened. If
    # WooCommerce is unreachable, _force_draft raises — and on the reconcile /
    # task paths that exception is caught per-record and the transaction still
    # commits, so a past-tense claim would PERSIST on a $0 product that is in
    # fact still live and purchasable. That is the incident's own failure shape
    # (a $0 gun for sale) with the Desk asserting the opposite. Intent now,
    # fact only once it is a fact.
    writeback = dict(extra_writeback or {})
    writeback[_f(site, "sync_error")] = (
        f"{base} The existing product will be taken off sale." if woo_id else base
    )
    frappe.db.set_value(doctype, name, writeback)

    if woo_id:
        _force_draft(_client(client, site), woo_id, _retries=_retries)
        # Reached only on success — now the past tense is true.
        frappe.db.set_value(
            doctype, name, _f(site, "sync_error"),
            f"{base} The existing product was taken off sale (set to draft).",
        )
    return {"skipped": UNPRICED, "name": name, "site": site}


# ---------------------------------------------------------------------------
# POS-owned fields (machine updates)
# ---------------------------------------------------------------------------

# The payload keys a MACHINE-triggered update (doc_events tasks, scheduled
# reconcile, webhook reserve/release) may write to an EXISTING WooCommerce
# product.  Everything else — name, description, images, attributes,
# categories, brands, shipping class, SKU/GTIN, content meta — is Woo-owned
# after first publish: staff curate those in wp-admin, and the 2026-08-12
# incident was automation overwriting exactly that curation on every drift
# push.  ``status`` stays POS-owned because lifecycle (publish / draft / SOLD
# archive) is decided here: the machine relist paths depend on a machine update being able to set
# ``publish`` (a full payload always carried it, so this widens nothing).
#
# Fails safe by design: a key missing from this allowlist simply stops
# syncing automatically (it remains reachable through the manual push
# buttons, which stay full-payload) — when POS grows a new POS-owned field,
# add it HERE, never by widening a machine call site back to full.  Adding it
# here is NOT the whole job: the reconcile drift comparators (tasks.py Pass 1
# stock/price/sale/MAP, Pass 2 stock/sale/MAP) decide when a lean push
# fires at all, so a new POS-owned field that should self-heal needs a
# comparator too — the two lists must move together.
#
# Related single-purpose payload shapes (each a strict subset of this list —
# keep it that way, per the OFF_SALE_PAYLOAD "one place so call sites cannot
# drift" rule): OFF_SALE_PAYLOAD (draft + stock 0), push_stock
# ({stock_quantity}), archive_serial_sold (status + stock + sold meta).
#
# `sale_price` + its schedule dates joined the list when the promo became a
# first-class POS field (Item.sale_price / Serial No.sale_sell_price + the
# sale_from / sale_to window): POS price authority now covers the
# sale exactly like the regular price — set it in POS, and a wp-admin edit or
# loss self-heals on reconcile. The builders emit the trio on EVERY payload
# ("" clears; see mapping.sale_price_payload), so a lean push carrying it is
# always authoritative, never a partial echo.
_POS_OWNED_KEYS = frozenset({
    "status", "regular_price", "manage_stock", "stock_quantity",
    "sale_price", "date_on_sale_from", "date_on_sale_to",
    # Ops-owned shipping class. Ordinary retail payloads never carry
    # the key (the builder only emits it when the listing pipeline passes a
    # slug), so listing it widens nothing for them — it exists so the lean
    # `*/15` distributor drift push still delivers a shipping-class change.
    "shipping_class",
})

# Machine-owned meta a lean update may write.  WooCommerce meta_data is an
# upsert, so keys filtered out here keep their stored values on the shop —
# which is exactly the protection for hand-curated meta (SEO plugins etc.).
# Every key the payload BUILDERS emit is POS/machine-derived — none is
# wp-admin-curated content — so the whole builder set is listed:
#   * sold lifecycle: must stay writable so a machine relist of a returned gun
#     can blank the SOLD banner (clear_sold);
#   * compliance/ops flags (_firearm_product / _ammunition_product /
#     _osa_restricted_states / _osa_fulfillment_mode): checkout enforcement
#     reads these, so losing their drift self-heal is NOT a safe failure:
#     restricted states / fulfillment mode also keep
#     _is_distributor_listing's documented guarantee for dropship SKUs;
#   * _osa_serial: identity, immutable outside serial_correction (which pushes
#     full anyway).
# The allowlist still fails safe for any FUTURE builder meta key: unknown keys
# are blocked here until someone consciously classifies them.
_POS_OWNED_META_KEYS = frozenset({
    "_osa_sold_at", "_osa_sold_price",
    "_firearm_product", "_ammunition_product", "_osa_serial",
    "_osa_restricted_states", "_osa_fulfillment_mode", "_osa_lead_days",
    *mapping.MAP_META_KEYS,
})


def _pos_owned_payload(payload: dict) -> dict:
    """Reduce a full product payload to its POS-owned fields.

    Applied by :func:`push_item` / :func:`push_serial` when a machine-triggered
    update (``pos_fields_only=True``) targets an existing product.  The full
    payload is built first and filtered here — rather than building a lean one —
    so every derivation (stock math, SOLD flags, MAP meta) stays in one
    place and a filtered push can never disagree with a full one.
    """
    lean = {k: v for k, v in payload.items() if k in _POS_OWNED_KEYS}
    # Extension keys are machine-derived too (mapping.extension_product_meta),
    # so they ride along — otherwise an extension's flag would survive a full
    # push and silently revert on the next lean one.
    allowed = _POS_OWNED_META_KEYS | mapping.extension_meta_keys()
    meta = [
        m for m in payload.get("meta_data") or []
        if m.get("key") in allowed
    ]
    if meta:
        lean["meta_data"] = meta
    return lean


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def push_item(
    item_code: str,
    *,
    client: WooCommerceClient | None = None,
    attr_ids: dict[str, int] | None = None,
    _retries: int = 2,
    site: str = "retail",
    _relist: bool = False,
    shipping_class: str = "",
    listing: bool = False,
    pos_fields_only: bool = False,
) -> dict:
    """Create or update the WooCommerce product for an Item.

    - If the site's ``product_id`` field is blank → ``create_product`` (POST).
    - If set → ``update_product`` (PUT).

    Write-back on success:
        ``{site}_product_id``, ``{site}_listed=1``, ``{site}_synced_at``,
        ``{site}_sync_error=""``

    On error:
        ``{site}_sync_error`` is set to the exception message; the exception is
        re-raised so the caller can surface it.

    Raises :class:`ValueError` for per-serial firearms (no item-level WC
    product; callers should use :func:`push_all_serials` instead).

    WooCommerce HTTP calls are retried up to ``_retries`` times on transient
    failures (5xx / network); 4xx errors fail immediately.

    Two guards run before any HTTP, mirroring :func:`push_serial`:

    * ``{site}_delisted`` set → refuse unless ``_relist``.  A product
      a human pulled off the shop stays off until a human puts it back.
    * no price (:func:`effective_item_price` <= 0) → refuse, NOT overridable
 see
      :func:`_refuse_unpriced`.  ``{site}_listed`` is cleared alongside so the
      reconcile sweep (which selects on ``listed=1``) stops re-offering it.

    ``shipping_class`` — a dropship shipping-class slug, passed by the
    ``distributor_listing_push`` path.  Default ``""`` means "send no
    ``shipping_class`` key at all", so ordinary retail pushes and the reconcile
    sweep cannot clear a class the store already assigned.

    ``listing`` — this push is part of the dropship listing pipeline, which
    unlocks the ``product_brand`` term and the always-present
    ``_osa_restricted_states`` meta.  It is only ever *widened* here (a dropship
    Item is a listing push whoever triggered it) — see
    :func:`_is_distributor_listing`.

    ``pos_fields_only`` — the push was triggered by a MACHINE (doc_events
    task, scheduled reconcile) rather than a human.  When the product already
    exists the payload is reduced to the POS-owned fields (``_POS_OWNED_KEYS``)
    so wp-admin content edits survive automation; a CREATE stays full-payload
    whoever asked.  Manual entrypoints (``push_item_now``, ``reconcile_now``,
    relist, the listing pipeline) leave it False.
    """
    # A blank item_code must never reach the reads below. Frappe treats a falsy
    # docname as "no filter", so every one of them (delisted, the firearm-routing
    # row, standard_rate) would resolve an ARBITRARY Item — the guards would then
    # decide this product's fate from an unrelated SKU's price and flags. Same
    # failure shape as the serial-side lookup; caught once, here, for all three.
    # ``push_item_now`` is whitelisted, so item_code="" arrives from outside.
    if not item_code:
        raise ValueError("push_item() requires an item_code")

    # Checked before the client is built, so a refusal costs no HTTP and no
    # config read.
    if not _relist and frappe.db.get_value("Item", item_code, _f(site, "delisted")):
        return {"skipped": "delisted", "item_code": item_code, "site": site}

    # The price guard must NOT swallow the per-serial-firearm case.  Such an
    # Item legitimately has standard_rate 0 (each gun carries its own tag) and
    # has no item-level product at all, so "unpriced" is meaningless for it —
    # and `reconcile_now` DEPENDS on the ValueError that
    # `mapping.item_to_product_payload` raises further down to route these to
    # push_all_serials instead.  Refusing here would swap that exception for a
    # skip dict, silently breaking the routing and stamping a misleading
    # "no sell price" error on every per-serial firearm SKU.
    meta = frappe.db.get_value(
        "Item", item_code, ["is_firearm", "item_group", "uniform_sell_price"], as_dict=True
    ) or frappe._dict()
    is_per_serial_firearm = _is_firearm_row(meta) and not meta.get("uniform_sell_price")

    if not is_per_serial_firearm and effective_item_price(item_code) <= 0:
        return _refuse_unpriced(
            "Item", item_code, client=client, site=site, _retries=_retries,
            extra_writeback={_f(site, "listed"): 0},
        )

    wc = _client(client, site)
    if attr_ids is None:
        attr_ids = wc.get_attribute_ids()
    try:
        woo_id = frappe.db.get_value("Item", item_code, _f(site, "product_id")) or ""
        # Machine update vs create: only an EXISTING product carries Woo-owned
        # content to protect — a create is full-payload whoever asked for it.
        # shipping_class is deliberately NOT blanked under lean: it is a
        # POS-owned key (see _POS_OWNED_KEYS) and the lean `*/15` distributor
        # drift push is exactly how a class change reaches the store.
        lean = pos_fields_only and bool(woo_id)
        # Live taxonomy resolution happens BEFORE the build: the builder takes
        # plain values, and a shipping class WooCommerce does not know is worse
        # than none at all (it accepts the product and drops the slug).
        if shipping_class and not _ensure_shipping_class(wc, shipping_class, site=site):
            shipping_class = ""
        # Is this the dropship listing pipeline?  Gates the two fields that must
        # not leak into ordinary retail pushes: the brand term
        # and the always-present restricted-states meta.
        # Runs under lean too — LOAD-BEARING there, not waste: listing=True is
        # what makes the builder emit _osa_restricted_states /
        # _osa_fulfillment_mode, both on the lean meta allowlist (compliance
        # self-heal). _is_distributor_listing is also this rule's precursor
        # (MAJOR-1: unattended pushes must not clear hand-curated brand) —
        # brand resolution alone stays skipped under lean, below.
        listing = _is_distributor_listing(item_code, forced=listing)
        # A per-serial firearm has no item-level product at all — the build below
        # raises so the caller routes to push_all_serials.  Resolving its brand
        # would spend a round-trip, and possibly create a term, for a product
        # that is never going to exist.
        brand_id = (
            _brand_id_for_item(wc, item_code, site=site)
            if listing and not is_per_serial_firearm and not lean
            else None
        )
        payload = mapping.item_to_product_payload(
            item_code,
            attr_ids=attr_ids,
            site=site,
            brand_id=brand_id,
            shipping_class=shipping_class,
            listing=listing,
        )
        # Built full, filtered after: the ValueError the builder raises for a
        # per-serial firearm must keep flowing to callers either way, and the
        # payload derivations stay in one place (see _pos_owned_payload).
        if lean:
            payload = _pos_owned_payload(payload)
        else:
            _apply_category(wc, payload, item_code, site)
        if woo_id:
            result = _without_dead_images(item_code, payload, lambda: _push_with_retry(
                lambda: wc.update_product(woo_id, payload), retries=_retries
            ))
        else:
            result = _without_dead_images(item_code, payload, lambda: _push_with_retry(
                lambda: wc.create_product(payload), retries=_retries
            ))
            woo_id = str(result["id"])
        _verify_shipping_class(result, shipping_class, item_code)
        writeback = {
            _f(site, "product_id"): woo_id,
            _f(site, "listed"): 1,
            _f(site, "synced_at"): _now(),
            _f(site, "sync_error"): "",
        }
        if _relist:
            # ONLY here — the same rule the serial guard learned the hard way in
            # an earlier incident: an ordinary push that cleared the sentinel would erase
            # the record that a human had ever hidden this product.
            writeback[_f(site, "delisted")] = 0
        # The store call took seconds and the row was read before it; a sibling
        # push (the save's own enqueued push_item_task, a sweep) may have stamped it
        # meanwhile, and a write from that snapshot is MariaDB 1020, which Frappe
        # does not retry. Every write-back below starts a fresh snapshot; what the
        # commit makes durable is only bookkeeping that already matches the store.
        safe_commit()
        frappe.db.set_value("Item", item_code, writeback)
        return result
    except WooCommerceError as exc:
        safe_commit()  # a fresh snapshot for the error write (see the write-back above)
        if exc.status_code == 404:
            # Dangling product_id: the WC product behind the stored id was deleted
            # on the store, so update_product 404s forever. Keep raising (callers
            # decide), but make the message actionable — the durable fix is the
            # audit GC, not a retry (reconcile_now buckets this; push_item_task's
            # log then carries the pointer).
            msg = (
                f"Item {item_code!r}: {_f(site, 'product_id')} points to a WC product "
                f"that returned 404 — it was deleted on Woo. Run woo_audit_dangling_ids "
                f"to clear the dangling id."
            )
            frappe.db.set_value("Item", item_code, _f(site, "sync_error"), msg)
            raise WooCommerceError(msg, status_code=404) from exc
        frappe.db.set_value("Item", item_code, _f(site, "sync_error"), str(exc))
        raise
    except Exception as exc:
        safe_commit()
        frappe.db.set_value("Item", item_code, _f(site, "sync_error"), str(exc))
        raise


def push_serial(
    serial_no: str,
    *,
    client: WooCommerceClient | None = None,
    attr_ids: dict[str, int] | None = None,
    _retries: int = 2,
    site: str = "retail",
    _relist: bool = False,
    pos_fields_only: bool = False,
) -> dict:
    """Create or update the WooCommerce product for a Serial No.

    - If the site's ``product_id`` field is blank → ``create_product`` (POST).
    - If set → ``update_product`` (PUT).

    Write-back on success:
        ``{site}_product_id``, ``{site}_synced_at``, ``{site}_sync_error=""``

    On error:
        ``{site}_sync_error`` is set to the exception message (truncated to
        1000 chars); the exception is re-raised so the caller can surface it.

    ``pos_fields_only`` — the push was triggered by a MACHINE (doc_events task,
    scheduled reconcile, webhook reserve/release, stock-movement listing sync)
    rather than a human.  When the product already exists the payload is
    reduced to the POS-owned fields (``_POS_OWNED_KEYS`` — status/stock/price
    plus lifecycle meta) so wp-admin content edits survive automation; a CREATE
    stays full-payload whoever asked.  Manual entrypoints (``push_serial_now``,
    ``push_all_serials``, relist, ``reconcile_now``) leave it False.
    """
    # NOT OURS TO SELL. Checked first and unconditionally: a customer's transfer
    # gun is excluded by a different KIND of reason than the two guards below —
    # they ask "should this be listed right now?", this one answers "this is not
    # the store's property". So `_relist` cannot lift it (the operator's "Push to
    # WooCommerce" button is refused too, which is the point). Checked BEFORE the client is built, so a refused
    # push costs no HTTP and no config read.
    if is_custody_serial(serial_no):
        return {"skipped": TRANSFER_CUSTODY, "serial_no": serial_no, "site": site}

    # Another sales channel already has this gun. NOT one of our own web orders —
    # those are mid-fulfilment and their product page stays published at stock 0.
    # PUBLISHING a gun another channel has bought puts it back on a shelf, and a
    # GunBroker sale cannot be unwound by us.
    #
    # It refuses a PUBLISH, not every write, and the distinction is the whole
    # point of where it sits. An unconditional refusal also swallowed the
    # self-heal: reconcile finds such a gun still showing stock 1 on the shop,
    # calls in here to fix it, gets refused, and the storefront goes on selling
    # a gun that is already sold — the exact drift reconcile exists to close,
    # running backwards on the one case that needs it.
    #
    # So a MACHINE update of an EXISTING product (``pos_fields_only`` with a
    # product id) is allowed through: for such a gun the payload carries
    # ``stock_quantity: 0`` and ``status: draft``, so it can only take the
    # product down — and that invariant is re-checked against the built payload
    # below rather than assumed. Anything else — a CREATE (there is nothing to
    # take down; minting a page for a gun another channel sold is not a repair),
    # a full content push, or the form's "Send to WooCommerce" button — is
    # refused.
    #
    # NOT liftable by ``_relist``, and that is deliberate. ``_relist`` lets a
    # human answer "may a machine republish this?"; this guard answers "has
    # somebody already bought it?", which no button changes — the same
    # distinction the unpriced guard below spells out, and the same rank as
    # custody. The button passes ``_relist=True`` and NOT ``pos_fields_only``,
    # so it still lands here. Mirror of the GunBroker side's ``not_sellable``
    # guard: one door each, both bolted.
    #
    # Checked BEFORE the client is built, so a refusal costs no HTTP.
    _held = other_channel_reservation(serial_no, own=CHANNEL_WOO)
    if _held and not (pos_fields_only and frappe.db.get_value(
            "Serial No", serial_no, _f(site, "product_id"))):
        return {"skipped": CHANNEL_RESERVED, "serial_no": serial_no, "site": site,
                "channel": _held["channel"], "ref": _held["ref"]}

    # NEVER MINT a per-serial product for a uniform-priced firearm.
    # This is the sink every push path funnels
    # through, so the guard here also covers every bulk entrypoint (push_all_serials,
    # reconcile_now, reconcile Pass 2) without duplicating the rule — the same way
    # the custody / unpriced guards do.
    #
    # CREATE-ONLY: gated on the SITE's product_id being blank.  A legacy per-serial
    # product that already exists (e.g. a held one) keeps flowing to update / delist /
    # oos — the system must be able to manage an existing listing down; only a new
    # create is refused.  Not gated on `_relist`: a uniform Item never had a
    # per-serial product to relist, so the operator button is refused too (which is
    # why the UI also HIDES that button — belt and suspenders).  Checked BEFORE
    # the client is built so a refusal costs no HTTP and no config read; the Item
    # lookup runs ONLY on the create path (blank product_id), so ordinary updates
    # pay a single extra read.
    _pid_field = _f(site, "product_id")
    _sn = frappe.db.get_value(
        "Serial No", serial_no, [_pid_field, "item_code", "consigned_out", "warehouse"],
        as_dict=True,
    ) or {}
    # Out on consignment — see CONSIGNED_OUT. Down-only: a machine update of an
    # existing product may take the page down; nothing else gets through.
    _consigned = is_consigned_out(
        consigned_out=_sn.get("consigned_out"), warehouse=_sn.get("warehouse"))
    if _consigned and not (pos_fields_only and _sn.get(_pid_field)):
        return {"skipped": CONSIGNED_OUT, "serial_no": serial_no, "site": site}
    if not _sn.get(_pid_field):
        _item = frappe.db.get_value(
            "Item", _sn.get("item_code"),
            ["is_firearm", "item_group", "uniform_sell_price"], as_dict=True,
        ) if _sn.get("item_code") else None
        if _item and _is_firearm_row(_item) and bool(_item.get("uniform_sell_price")):
            return {"skipped": UNIFORM_ITEM, "serial_no": serial_no, "site": site}

    # A gun the owner pulled off the web stays off until a HUMAN puts it back.
    # ``{site}_delisted`` has always carried that meaning, but only the EXIT
    # router honoured it; every push path published straight over the top of it.
    # The first live reconcile therefore republished all six hidden guns —
    # three of them at ``sell_price=0`` (incident 2026-07-18).
    #
    # Fail closed: refuse by default, and make the callers that genuinely mean
    # "relist" say so.  Checked BEFORE the client is built so a refused push
    # costs no HTTP and no config read.
    if not _relist and frappe.db.get_value("Serial No", serial_no, _f(site, "delisted")):
        return {"skipped": "delisted", "serial_no": serial_no, "site": site}

    # A gun with no price must never reach the storefront (incident
    # 2026-07-19).  Deliberately placed AFTER the hidden-serial guard but
    # deliberately NOT gated on ``_relist``: the two guards answer different
    # questions.  ``_relist`` lets a human answer "may a machine republish
    # this?" — it cannot make an unpriced gun priced.  So the operator's "Push
    # to WooCommerce" button is refused here too, which is the point: that
    # button is exactly how a $0 listing would otherwise walk back onto the shop.
    if effective_serial_price(serial_no) <= 0:
        return _refuse_unpriced(
            "Serial No", serial_no, client=client, site=site, _retries=_retries,
        )

    wc = _client(client, site)
    if attr_ids is None:
        attr_ids = wc.get_attribute_ids()
    try:
        payload = mapping.serial_to_product_payload(serial_no, attr_ids=attr_ids, site=site)
        state = frappe.db.get_value(
            "Serial No", serial_no, ["status", _f(site, "sold_archived")], as_dict=True
        ) or {}
        # woo_id from the row already read by the uniform guard — the third
        # read this function used to make said nothing the first didn't.
        woo_id = _sn.get(_pid_field) or ""
        # Machine update vs create — same rule as push_item: only an existing
        # product carries Woo-owned content to protect.
        lean = pos_fields_only and bool(woo_id)
        if lean:
            payload = _pos_owned_payload(payload)
        else:
            item_code = _sn.get("item_code")
            if item_code:
                _apply_category(wc, payload, item_code, site)
        # The invariant the guard above rests on, asked of the payload that is
        # actually about to be sent rather than assumed from the caller's flag.
        # The guard lets a machine update of a channel-held gun through BECAUSE
        # such a payload can only take the product down; that property is
        # produced somewhere else entirely (mapping's status/stock derivation),
        # and nothing but this line keeps the two in step. The test is worded
        # as "would this publish, or raise stock" — so ask the payload, after
        # `_pos_owned_payload` has had its say, because `status` is a POS-owned
        # key and a lean push carries it.
        #
        # Reached only on the lean path (anything else with `_held` set was
        # refused before the client was built), so nothing has been sent yet.
        if _held and (payload.get("status") == "publish"
                or int(payload.get("stock_quantity") or 0) > 0):
            return {"skipped": CHANNEL_RESERVED, "serial_no": serial_no, "site": site,
                    "channel": _held["channel"], "ref": _held["ref"],
                    "detail": "payload would publish or restock a channel-held gun"}
        # Same invariant for a consigned gun, except that one SOLD at the partner
        # keeps its SOLD archive page (publish at stock 0, `route_serial_exit`).
        if _consigned and (int(payload.get("stock_quantity") or 0) > 0 or (
                payload.get("status") == "publish"
                and not is_sold_state(state.get("status"), state.get(_f(site, "sold_archived"))))):
            return {"skipped": CONSIGNED_OUT, "serial_no": serial_no, "site": site,
                    "detail": "payload would publish or restock a consigned-out gun"}
        if woo_id:
            result = _push_with_retry(
                lambda: wc.update_product(woo_id, payload), retries=_retries
            )
        else:
            result = _push_with_retry(
                lambda: wc.create_product(payload), retries=_retries
            )
            woo_id = str(result["id"])
        writeback = {
            _f(site, "product_id"): woo_id,
            _f(site, "synced_at"): _now(),
            _f(site, "sync_error"): "",
        }
        if _relist:
            # ONLY here.  Clearing this unconditionally is what let an ordinary
            # push erase the record that a gun had been hidden — after which
            # nothing remembered, and the next sweep republished it.
            writeback[_f(site, "delisted")] = 0
            writeback[_f(site, "channel_delisted")] = 0
        # Sold-aware sentinel handling.  A post-sale gallery/detail edit re-pushes
        # a SOLD gun; clearing the archive sentinel there would make reconcile
        # re-archive the same gun on every 2-hourly sweep.  Only a genuine relist
        # (back to Active) clears it — paired with the ``_osa_sold_*`` blanking
        # that mapping puts in the payload.
        if not is_sold_state(state.get("status"), state.get(_f(site, "sold_archived"))):
            writeback[_f(site, "sold_archived")] = 0
        safe_commit()  # a fresh snapshot for the write-back (see push_item)
        frappe.db.set_value("Serial No", serial_no, writeback)
        return result
    except Exception as exc:
        safe_commit()
        frappe.db.set_value("Serial No", serial_no, _f(site, "sync_error"), str(exc)[:1000])
        raise


def push_all_serials(
    item_code: str,
    *,
    client: WooCommerceClient | None = None,
    attr_ids: dict[str, int] | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> list[dict]:
    """Push every Active Serial No for ``item_code``.

    Uses a shared pre-built client and pre-fetched attribute IDs to minimise
    HTTP round-trips.  Returns the list of WC API responses (one per serial).
    Per-serial errors are re-raised immediately (caller decides how to handle).
    """
    wc = _client(client, site)
    if attr_ids is None:
        attr_ids = wc.get_attribute_ids()
    serials = frappe.get_all(
        "Serial No",
        filters={"item_code": item_code, "status": "Active"},
        pluck="name",
    )
    results = []
    for sn in serials:
        results.append(push_serial(sn, client=wc, attr_ids=attr_ids, _retries=_retries, site=site))
    return results


def push_stock(
    item_code: str,
    *,
    client: WooCommerceClient | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict:
    """Update only the ``stock_quantity`` for an item-level WC product.

    Lighter than a full :func:`push_item` — useful for stock-adjustment events.
    Raises :class:`ValueError` if the item has no site product_id field set
    (push it first) or if it is a per-serial firearm. A 404 from WooCommerce (the
    product was deleted on the store → dangling product_id) is downgraded to a
    warning and returns ``{"skipped": "dangling_product_id", ...}`` instead of
    raising, so the caller doesn't record an ERROR.
    """
    wc = _client(client, site)
    woo_id = frappe.db.get_value("Item", item_code, _f(site, "product_id")) or ""
    if not woo_id:
        raise ValueError(
            f"Item {item_code!r} has no {_f(site, 'product_id')} — call push_item() first."
        )
    qty = mapping.stock_for_item(item_code)
    try:
        result = _push_with_retry(
            lambda: wc.update_product(woo_id, {"stock_quantity": qty}), retries=_retries
        )
    except WooCommerceError as exc:
        # A dangling product_id: the WC product behind this id was deleted on the
        # store, so update_product returns 404 forever. Downgrade the recurring
        # failure from an ERROR (push_stock_task's frappe.log_error) to a warning
        # and skip — there is no live listing whose stock could be wrong, so this
        # is log noise, not a real sync failure. Durable dangling-id cleanup (GC +
        # unlisting) is tracked separately; do NOT re-raise so the caller doesn't
        # record an Error Log. Any other HTTP error is a genuine failure — re-raise.
        if exc.status_code == 404:
            frappe.logger("woo.sync").warning(
                f"push_stock: WC product {woo_id} for Item {item_code!r} returned 404 — "
                f"the product was removed on the store; stock push skipped "
                f"(dangling {_f(site, 'product_id')})."
            )
            return {"skipped": "dangling_product_id", "item_code": item_code, "woo_id": woo_id}
        raise
    _stamp_stock_synced(item_code, site)
    return result


def _stamp_stock_synced(item_code: str, site: str) -> None:
    """Bookkeeping after a stock quantity landed on WooCommerce (synced_at + clear
    sync_error), tolerant of the MariaDB 1020 race."""
    stamp = {
        _f(site, "synced_at"): _now(),
        _f(site, "sync_error"): "",
    }
    try:
        frappe.db.set_value("Item", item_code, stamp)
    except frappe.QueryDeadlockError:
        # MariaDB 1020 "Record has changed since last read in table 'tabItem'" —
        # Frappe maps it through is_deadlocked() to QueryDeadlockError (the prod
        # traceback of 2026-08-26 15:23:28 for IRAY-RH25 ends in exactly
        # "database.py:278 raise frappe.QueryDeadlockError(e) from e", so this is
        # the class, not an assumption):
        # the Item row was written by another transaction (an SLE hook stamping
        # the same fields, a desk save) while this job's snapshot stayed open
        # across the WooCommerce round-trip. The store already holds the new
        # quantity — only this bookkeeping stamp lost the race, and re-raising
        # filed it as "push_stock_task failed" (a dozen times since 2026-08-15)
        # for a push that had in fact landed. Drop the stale snapshot and stamp
        # again. A full rollback, not a savepoint: under snapshot isolation the
        # read view belongs to the transaction, and only ending it refreshes the
        # view — a savepoint rollback would fail the same way. That is safe
        # because the callers (push_stock_task, push_stock_batch) do no other
        # pending writes — the batch commits after every item — so a caller
        # that adds pending writes before this call must not rely on them
        # surviving.
        frappe.db.rollback()
        frappe.db.set_value("Item", item_code, stamp)


# lazy: WooCommerce's default cap on one `products/batch` request (`create` +
# `update` + `delete` together); a store that raised it via the
# `woocommerce_rest_batch_items_limit` filter could take more per request.
STOCK_BATCH_SIZE = 100


def push_stock_batch(
    item_codes: list[str],
    *,
    client: WooCommerceClient | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict[str, object]:
    """:func:`push_stock` for up to :data:`STOCK_BATCH_SIZE` items in ONE HTTP request.

    The caller chunks (``tasks.push_stock_batch_task``). Product id and quantity
    are read HERE, at run time, per item (after a commit so the REPEATABLE READ
    snapshot is fresh) — the same freshness a per-item ``push_stock_task`` has.
    Returns ``{item_code: outcome}``: the WC product dict / a ``{"skipped": ...}``
    dict on success, or an Exception the caller must report:

    * an item whose id / quantity cannot be read (bad id, per-serial firearm)
      gets that error — the rest of the chunk still goes out;
    * the whole request failed (``WooCommerceError`` after retries, or a
      ``requests`` timeout / connection error) → every item in it gets that
      error, with no per-item fallback (it would multiply requests against a
      rate-limited or blocked store); nothing is re-queued here;
    * one entry errored inside a 200 batch response → that item alone falls back
      to :func:`push_stock` (retries, 404-dangling downgrade — unchanged).

    Bookkeeping per successful item is :func:`_stamp_stock_synced`, committed at
    once so a later item's 1020 rollback cannot undo earlier stamps.
    """
    wc = _client(client, site)
    # Ends the open transaction so the reads below are on a fresh snapshot
    # (REPEATABLE READ). No-op under the test runner (ffl_core.utils.commit).
    safe_commit()
    results: dict[str, object] = {}
    by_id: dict[str, list[str]] = {}
    updates: list[dict] = []
    for code in dict.fromkeys(c for c in item_codes if c):
        try:
            woo_id = frappe.db.get_value("Item", code, _f(site, "product_id")) or ""
            if not woo_id:
                results[code] = {"skipped": "no_product_id", "item_code": code}
                continue
            update = {"id": int(woo_id), "stock_quantity": mapping.stock_for_item(code)}
        except Exception as exc:  # one bad item must not sink the chunk
            results[code] = exc
            continue
        by_id.setdefault(str(update["id"]), []).append(code)
        updates.append(update)
    if not updates:
        return results
    try:
        resp = _push_with_retry(lambda: wc.batch_update_products(updates), retries=_retries)
    except (WooCommerceError, requests.RequestException) as exc:
        for codes in by_id.values():
            results.update(dict.fromkeys(codes, exc))
        return results
    entries = resp.get("update") if isinstance(resp, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        queue = by_id.get(str(entry.get("id"))) if isinstance(entry, dict) else None
        if not queue:
            continue
        code = queue.pop(0)
        try:
            if entry.get("error"):
                results[code] = push_stock(code, client=wc, _retries=_retries, site=site)
            else:
                _stamp_stock_synced(code, site)
                results[code] = entry
            safe_commit()  # keep this stamp if a later item's 1020 rolls back
        except Exception as exc:
            results[code] = exc
    for queue in by_id.values():  # asked for, never answered
        for code in queue:
            results[code] = WooCommerceError(
                f"Item {code!r} missing from the products/batch response")
    return results


# ---------------------------------------------------------------------------
# Delist
# ---------------------------------------------------------------------------

def delist_item(
    item_code: str,
    *,
    client: WooCommerceClient | None = None,
    _retries: int = 2,
    site: str = "retail",
    _deliberate: bool = False,
) -> dict:
    """Set the item WC product to ``status=draft`` and ``stock_quantity=0``.

    Write-back on success:
        ``{site}_listed=0``, ``{site}_synced_at``, ``{site}_sync_error=""``
        (plus ``{site}_delisted=1`` when *_deliberate*)

    ``_deliberate`` marks a HUMAN delist — the operator's "Delist from
    WooCommerce" button — and is the only case that raises the republish
    sentinel.  Without it the item channel keeps the hole an earlier fix closed for
    serials: a hand-delisted Item is republished by the next edit to any
    ``_ITEM_WOO_FIELDS`` value, because ``push_item`` reads ``delisted=0``.

    It is a PARAMETER rather than unconditional (which is what ``delist_serial``
    does) because the automated caller must not raise it: ``delist_item_task``
    (a machine take-down through ``distributor.distributor_listing_push``). If
    that raised the sentinel, the same pipeline listing the product again would
    enqueue ``push_item_task`` → ``push_item`` with no ``_relist`` → refused, and
    the take-down would silently become one-way.  Serials have no equivalent trap: their automated
    return path (``sync_serial_listing_task``) already passes ``_relist=True``.
    """
    wc = _client(client, site)
    woo_id = frappe.db.get_value("Item", item_code, _f(site, "product_id")) or ""
    if not woo_id:
        raise ValueError(
            f"Item {item_code!r} has no {_f(site, 'product_id')} — nothing to delist."
        )
    result = _push_with_retry(
        lambda: wc.update_product(woo_id, dict(OFF_SALE_PAYLOAD)),
        retries=_retries,
    )
    writeback = {
        _f(site, "listed"): 0,
        _f(site, "synced_at"): _now(),
        _f(site, "sync_error"): "",
    }
    if _deliberate:
        writeback[_f(site, "delisted")] = 1
    frappe.db.set_value("Item", item_code, writeback)
    return result


def delist_serial(
    serial_no: str,
    *,
    client: WooCommerceClient | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict:
    """Set the serial WC product to ``status=draft`` and ``stock_quantity=0``.

    Write-back on success:
        ``{site}_synced_at``, ``{site}_delisted=1``

    Raises :class:`ValueError` if the site product_id field is blank.
    """
    wc = _client(client, site)
    woo_id = frappe.db.get_value("Serial No", serial_no, _f(site, "product_id")) or ""
    if not woo_id:
        raise ValueError(
            f"Serial No {serial_no!r} has no {_f(site, 'product_id')} — nothing to delist."
        )
    result = _push_with_retry(
        lambda: wc.update_product(woo_id, dict(OFF_SALE_PAYLOAD)),
        retries=_retries,
    )
    frappe.db.set_value("Serial No", serial_no, {
        _f(site, "synced_at"): _now(),
        _f(site, "delisted"): 1,  # sentinel: Pass 3 skips this serial on subsequent reconciles
    })
    return result


# ---------------------------------------------------------------------------
# SOLD archive (the terminal state for a gun that SOLD)
# ---------------------------------------------------------------------------

def archive_serial_sold(
    serial_no: str,
    *,
    sold_price: float | None = None,
    client: WooCommerceClient | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict:
    """Put a SOLD per-serial firearm's WC product into the SOLD archive state:
    ``status=publish`` + ``stock_quantity=0`` + ``_osa_sold_*`` meta.

    The page STAYS live (200, indexable, keeps its accrued rankings/backlinks)
    and simply stops being purchasable — as opposed to :func:`delist_serial`,
    which drafts the product into a 404.  Which of the two a caller wants is
    never decided locally: ask :func:`route_serial_exit`.

    ``sold_price`` is written to ``_osa_sold_price`` ONLY when the caller has the
    exact realized figure (web fulfillment passes the order line total).  POS and
    reconcile archives leave it unset so the storefront renders a bare "Sold"
    rather than passing an asking price off as a realized one.

    Write-back on success:
        ``{site}_synced_at``, ``{site}_sold_archived=1``, ``{site}_delisted=0``

    The two sentinels are kept mutually exclusive: an archived product is
    PUBLISHED, so leaving ``delisted=1`` behind would be a lie — and that exact
    contradiction (delisted=1 on a live page) is what made reconcile skip the
    SP000279 zombie forever.

    Only the three lifecycle keys are sent — an archive is a state flip, not a
    re-push, so it must not overwrite curated title/price/description content.

    Raises :class:`ValueError` if the site product_id field is blank (the gun was
    never listed, so there is no page to preserve).
    """
    wc = _client(client, site)
    woo_id = frappe.db.get_value("Serial No", serial_no, _f(site, "product_id")) or ""
    if not woo_id:
        raise ValueError(
            f"Serial No {serial_no!r} has no {_f(site, 'product_id')} — nothing to archive."
        )
    meta = {"_osa_sold_at": _today()}
    if sold_price is not None:
        meta["_osa_sold_price"] = f"{float(sold_price):.2f}"
    result = _push_with_retry(
        lambda: wc.update_product(woo_id, {
            "status": "publish",
            "stock_quantity": 0,
            "meta_data": mapping._meta_data(meta),
        }),
        retries=_retries,
    )
    frappe.db.set_value("Serial No", serial_no, {
        _f(site, "synced_at"): _now(),
        _f(site, "sold_archived"): 1,  # sentinel: reconcile's archive arm skips it hereafter
        _f(site, "delisted"): 0,
    })
    return result


# ---------------------------------------------------------------------------
# Relist (re-publish after a delist)
# ---------------------------------------------------------------------------

def relist_item(
    item_code: str,
    *,
    client: WooCommerceClient | None = None,
    attr_ids: dict[str, int] | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict:
    """Re-publish a delisted item by doing a full :func:`push_item`.

    This refreshes all fields (name, price, stock, images, attributes) in
    addition to setting ``status=publish``, which is the desired behaviour after
    an inventory or pricing change.
    Asking to relist IS the deliberate human act the hidden-product guard waits
    for, so this overrides it (the item-level twin of :func:`relist_serial`).
    """
    return push_item(item_code, client=client, attr_ids=attr_ids, _retries=_retries,
                     site=site, _relist=True)


def relist_serial(
    serial_no: str,
    *,
    client: WooCommerceClient | None = None,
    attr_ids: dict[str, int] | None = None,
    _retries: int = 2,
    site: str = "retail",
) -> dict:
    """Re-publish a delisted serial by doing a full :func:`push_serial`.

    This is one of the four callers allowed to override the hidden-serial guard
    (see :func:`push_serial`): asking to relist IS the deliberate act the guard
    waits for."""
    return push_serial(serial_no, client=client, attr_ids=attr_ids,
                       _retries=_retries, site=site, _relist=True)
