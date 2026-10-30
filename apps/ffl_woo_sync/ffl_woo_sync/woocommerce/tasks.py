"""WooCommerce background tasks and scheduled reconcile.

Enqueue targets
---------------
``push_item_task(item_code)``    — full push for an item-level WC product
``push_serial_task(serial_no)``  — full push for a per-serial WC product
``push_stock_task(item_code)``   — stock-quantity-only update for an item
``push_stock_batch_task(item_codes)``
                                 — the same for many items, one request per 100
``distributor_listing_push_task(item_code)``
                                 — full push + dropship shipping class (RSR P3)
``rebucket_and_apply_serial_task(serial_no, target_item, woo_online_order)``
                                 — move a staff-picked gun into the listing Item
                                   its pool sells through, then apply the serial

Scheduled
---------
``scheduled_reconcile()``        — throttled gate; reads WooCommerce Settings and
                                   queues the sweep on the long queue
``_run_reconcile()``             — drift-detect + push for all listed products
``retry_stuck_serial_reservations()``
                                 — re-apply Woo's dealer-entered serial to rows
                                   stuck on a stale ``reservation_error``

WooCommerce Settings status fields
-----------------------------------
Status fields (``last_push_at``, ``last_push_count``, ``last_reconcile_at``,
``last_sync_error``) are written via ``frappe.db.set_value`` — NOT via
``frappe.get_single(...).save()`` — to bypass the ``track_changes=1`` audit
log on WooCommerce Settings.  Every call to ``.save()`` would generate a
Version document; with frequent reconcile runs that log would grow
continuously and produce noisy diffs.

Frequency constants mirror the RSR catalog-sync pattern in
``ffl_integrations.rsr.tasks``.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import traceback

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, time_diff_in_seconds
from rq.timeouts import JobTimeoutException

from ffl_core.firearm import _is_firearm_row
from ffl_core.reservations import (
    CHANNEL_WOO,
    channel_reservation,
    channel_reserve_columns,
    other_channel_reservation,
)
from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction

from ffl_woo_sync.woocommerce.client import resolve_site

_DISABLED = "Disabled"
_WOO_SETTINGS = "WooCommerce Settings"

# Minimum elapsed seconds before the next reconcile is allowed for each
# frequency label.  A small buffer is subtracted from exact intervals so that
# real-world cron jitter (±seconds) does not cause a run to be skipped.
_RECONCILE_MIN_INTERVAL: dict[str, int] = {
    "Every 2 Hours": 2 * 3600 - 300,       # 1 h 55 m
    "Daily":         20 * 3600,             # 20 h  (allows some daily drift)
    "Weekly":        7 * 24 * 3600 - 3600,  # 6 d 23 h
}


# ---------------------------------------------------------------------------
# Enqueue targets (dot-path called from doc_events via frappe.enqueue)
# ---------------------------------------------------------------------------

_RETIRED_SITE = "dealer"  # the dealer store, removed in 1.11.0


def _for_a_running_store(task):
    """Entry guard for a Woo push / delist job that names a store (its ``site`` argument).

    A job queued before the 1.11.0 deploy can still name the retired dealer store: it logs
    one line and does nothing, instead of an Error Log traceback per leftover job. A blank
    site is the retail store. Any other unknown site is a real bug and raises in the task
    as before (it reaches the Error Log)."""
    signature = inspect.signature(task)

    @functools.wraps(task)
    def run(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        if "site" in bound.arguments:
            site = bound.arguments["site"]
            if str(site or "").strip().lower() == _RETIRED_SITE:
                frappe.logger("ffl_woo_sync").info(
                    f"{task.__name__} skipped: the dealer store is gone")
                return None
            bound.arguments["site"] = resolve_site(site) or site  # blank -> retail; unknown stays as given
        return task(*bound.args, **bound.kwargs)

    return run


@_for_a_running_store
def push_item_task(item_code: str, site: str = "retail",
                   pos_fields_only: bool = True) -> None:
    """Push (create or update) the WooCommerce product for a single Item.

    ``pos_fields_only`` defaults to **True** because this task's ordinary
    enqueuers are MACHINES (doc_events field edits) — an update then touches
    only the POS-owned fields so wp-admin content edits survive; a first-time
    create is still full-payload.  A DELIBERATE caller that means "refresh the
    content too" must say so with ``pos_fields_only=False`` (the way
    ``serial_correction`` does for its task twin).

    Errors are absorbed — :func:`ffl_woo_sync.woocommerce.sync.push_item`
    already writes ``woo_sync_error`` to the Item record so operators can see
    what failed on the Item form.  The error is also logged via
    ``frappe.log_error`` so it appears in the Frappe Error Log for alerting.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.push_item(item_code, site=site, pos_fields_only=pos_fields_only)
    except Exception:
        frappe.log_error(
            title=f"Woo push_item_task failed: {item_code}",
            message=frappe.get_traceback(),
        )


@_for_a_running_store
def push_copy_task(item_code: str, site: str = "retail") -> None:
    """The Item's name and description onto its existing product, nothing else —
    a listing's optimized copy loaded after it was listed (``mode="copy"`` on
    ``distributor_listing_push``). Errors are logged like :func:`push_item_task`."""
    from ffl_woo_sync.woocommerce import sync
    from ffl_woo_sync.woocommerce.client import WooCommerceClient
    try:
        field = sync._f(site, "product_id")
        row = frappe.db.get_value("Item", item_code, ["item_name", "description", field],
                                  as_dict=True) or {}
        if row.get(field):
            WooCommerceClient.from_settings(site).update_product(
                row[field], {"name": row.item_name, "description": row.description or ""})
    except Exception:
        frappe.log_error(title=f"Woo push_copy_task failed: {item_code}",
                         message=frappe.get_traceback())


@_for_a_running_store
def push_items_task(item_codes: list, site: str = "retail", remedy: str | None = None) -> None:
    """Lean-push a batch of Items over ONE client and ONE attribute lookup —
    the re-push of a whole house's listings after its lead time changed.

    Per-item errors are absorbed and logged exactly like :func:`push_item_task`;
    one bad SKU never stops the batch. An unreachable store stops it once, with
    one log naming the skipped items and the remedy.
    """
    from ffl_woo_sync.woocommerce import sync
    from ffl_woo_sync.woocommerce.client import WooCommerceClient
    try:
        client = WooCommerceClient.from_settings(site)
        attr_ids = client.get_attribute_ids()
    except Exception:
        # lazy: no automatic retry (neither had the per-item jobs this
        # replaced); the operator re-saves the Distributor. Add a delayed
        # re-enqueue if store blips during lead-time edits ever bite.
        codes = list(item_codes or [])
        frappe.log_error(
            title=f"Woo re-push skipped for {len(codes)} listings: store unavailable ({site})",
            message=((remedy or "The store could not be reached, so these listings still "
                "carry the old lead time. Save the Distributor again once the store is back.")
                + "\n\n" + ", ".join(codes) + "\n\n" + frappe.get_traceback()))
        return
    for item_code in item_codes or []:
        try:
            sync.push_item(item_code, client=client, attr_ids=attr_ids, site=site,
                pos_fields_only=True)
        except Exception:
            frappe.log_error(title=f"Woo push_items_task failed: {item_code}",
                message=frappe.get_traceback())


# Items per re-push job: each job shares one client and one attribute lookup
# across its batch, and stays well inside the long queue's timeout.
DISTRIBUTOR_REPUSH_CHUNK = 100


def dropship_items_changed(item_codes) -> None:
    """``dropship_items_changed`` hook: ffl_integrations says these drop-ship
    Items' listing facts moved (a house's lead time), so their products re-push
    now instead of at the next price or stock change. Queued in batches."""
    chunk = DISTRIBUTOR_REPUSH_CHUNK
    from ffl_woo_sync.woocommerce.doc_events import _skip_push

    if _skip_push():
        return
    codes = sorted(set(item_codes or []))
    for start in range(0, len(codes), chunk):
        frappe.enqueue("ffl_woo_sync.woocommerce.tasks.push_items_task", queue="long",
                       enqueue_after_commit=True, job_name=f"woo_lead_repush_{codes[start]}",
                       item_codes=codes[start:start + chunk])


def map_meta_changed(item_codes) -> None:
    """``map_meta_changed`` hook: these Items' MAP mode changed (a brand's row in
    the Drop-ship pricing settings), so their retail products re-push to carry the
    new ``_osa_map_mode``. Queued in batches, like :func:`dropship_items_changed`."""
    chunk = DISTRIBUTOR_REPUSH_CHUNK

    codes = sorted(set(item_codes or []))
    for start in range(0, len(codes), chunk):
        frappe.enqueue("ffl_woo_sync.woocommerce.tasks.push_map_task", queue="long",
                       enqueue_after_commit=True, job_name=f"woo_map_repush_{start}",
                       item_codes=codes[start:start + chunk])


def push_map_task(item_codes: list) -> None:
    """Re-push the retail products that carry these Items' MAP meta: the Item's own
    product, or each gun's for a per-serial firearm (sold guns' archive pages are
    left alone)."""
    # Listed item-level products only, as reconcile Pass 1 picks them: an unpriced
    # (unlisted) Item would only be re-drafted, and a per-serial firearm's product
    # is each gun's.
    listed = [r.name for r in frappe.get_all("Item", filters={
        "name": ["in", item_codes], "woo_product_id": ["is", "set"], "woo_listed": 1},
        fields=["name", "is_firearm", "item_group", "uniform_sell_price"])
        if not (_is_firearm_row(r) and not r.uniform_sell_price)]
    if listed:
        push_items_task(listed, remedy="The store could not be reached, so these products "
            "still carry the old MAP mode. The next reconcile re-pushes MAP meta that drifted.")
    # lazy: one job-free push per gun; per-serial guns with a MAP are few. Batch
    # them over one client like push_items_task if a brand ever carries hundreds.
    for serial_no in frappe.get_all("Serial No", pluck="name", filters={
            "item_code": ["in", item_codes], "woo_product_id": ["is", "set"],
            "status": "Active"}):
        push_serial_task(serial_no)


@_for_a_running_store
def push_serial_task(serial_no: str, site: str = "retail", relist: bool = False,
                     pos_fields_only: bool = True) -> None:
    """Push (create or update) the WooCommerce product for a single Serial No.

    ``relist`` threads through to :func:`sync.push_serial`'s hidden-serial guard
    and defaults to **False**, so the ordinary enqueue targets (doc_events field
    edits, reconcile) can never republish a gun the owner hid.  Only callers that
    mean "put this back on sale" — currently the reservation-release path in
    ``webhook._release_serial`` — pass ``relist=True``.

    ``pos_fields_only`` defaults to **True**: the ordinary enqueuers (doc_events
    field edits, webhook reserve/release) are machines, and their updates must
    not rewrite wp-admin content.  The relist=True release path still works
    lean — status/stock are POS-owned keys, so the update republishes.  A
    DELIBERATE caller whose whole point is refreshing content must pass
    ``pos_fields_only=False``: today that is ``ffl_core.api.serial_correction``,
    which re-pushes so the corrected serial reaches the store's title / SKU /
    ``_osa_serial`` — lean would strand the listing on the OLD serial and break
    ``webhook._map_sku_to_pos`` for its web orders.

    Errors are absorbed — :func:`ffl_woo_sync.woocommerce.sync.push_serial`
    already writes ``woo_sync_error`` to the Serial No record so operators can
    see what failed on the Serial No form.  The error is also logged via
    ``frappe.log_error`` so it appears in the Frappe Error Log for alerting.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.push_serial(serial_no, site=site, _relist=relist,
                         pos_fields_only=pos_fields_only)
    except Exception:
        frappe.log_error(
            title=f"Woo push_serial_task failed: {serial_no}",
            message=frappe.get_traceback(),
        )
        # woo_sync_error written to Serial No by sync.push_serial; no re-raise needed


@_for_a_running_store
def push_stock_task(item_code: str, site: str = "retail") -> None:
    """Update ``stock_quantity`` only for an item-level WooCommerce product.

    Uses the lightweight :func:`~ffl_woo_sync.woocommerce.sync.push_stock`
    path to avoid rebuilding the full product payload for a stock-only event.
    Errors are absorbed and logged via ``frappe.log_error``.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.push_stock(item_code, site=site)
    except Exception as exc:
        report_stock_push_failure(item_code, site, exc, frappe.get_traceback())


@_for_a_running_store
def push_stock_batch_task(item_codes: list[str], site: str = "retail") -> None:
    """Update ``stock_quantity`` for many item-level products with ``products/batch``.

    Enqueued once per request / job by the ``distributor_listing_push`` hook
    (see :func:`~ffl_woo_sync.woocommerce.distributor.queue_stock_refresh`)
    instead of one :func:`push_stock_task` per item. Splits the list into
    ``sync.STOCK_BATCH_SIZE`` chunks — the one place that chunks — and sends one
    request per chunk. Each quantity is read when the chunk runs, on the single
    serial ``long`` worker, so it cannot be overtaken into a stale write.

    A failure is logged and reported for EVERY item it touched through the path
    :func:`push_stock_task` uses (Error Log + ``distributor_listing_push_failed``),
    so the listing records it and its own sync retries; nothing is re-queued here
    and the task does not raise.
    """
    from ffl_woo_sync.woocommerce import sync

    codes = list(dict.fromkeys(c for c in item_codes or [] if c))
    for start in range(0, len(codes), sync.STOCK_BATCH_SIZE):
        chunk = codes[start:start + sync.STOCK_BATCH_SIZE]
        try:
            results = sync.push_stock_batch(chunk, site=site)
        except Exception as exc:  # unexpected: never leave a chunk unreported
            message = frappe.get_traceback()
            results = dict.fromkeys(chunk, exc)
        else:
            message = None
        for code, outcome in results.items():
            if isinstance(outcome, Exception):
                report_stock_push_failure(
                    code, site, outcome,
                    message or "".join(traceback.format_exception(outcome)))
        safe_commit()  # the failure records written above


def report_stock_push_failure(item_code: str, site: str, exc: Exception, message: str) -> None:
    """A stock push failed: Error Log (``Woo push_stock_task failed: <item>``) and
    the ``distributor_listing_push_failed`` hook — one path for the single-item
    and the batch task."""
    frappe.log_error(title=f"Woo push_stock_task failed: {item_code}", message=message)
    _report_listing_push_failure(item_code, site, exc)


def _report_listing_push_failure(item_code: str, site: str, exc: Exception) -> None:
    """A queued push failed: tell the listing pipeline through the
    ``distributor_listing_push_failed`` hook (ffl_integrations registers it; this
    app imports nothing of it), so a drop-ship listing records the error and its
    own sync retries — a delisted product's zero would otherwise wait for the
    reconcile. Answers nothing for an Item without a listing. Best effort: the
    Error Log above already holds the failure."""
    error = f"{type(exc).__name__}: {exc}"[:2000]
    for path in frappe.get_hooks("distributor_listing_push_failed"):
        try:
            frappe.get_attr(path)(item_code, site, error)
        except Exception:
            frappe.log_error(
                title=f"distributor_listing_push_failed hook failed: {item_code}",
                message=frappe.get_traceback(),
            )


@_for_a_running_store
def distributor_listing_push_task(
    item_code: str, shipping_class: str = "", site: str = "retail",
    pos_fields_only: bool = False,
) -> None:
    """Product push for a dropship listing (RSR P3).

    Enqueue target of the ``distributor_listing_push`` hook — see
    :mod:`ffl_woo_sync.woocommerce.distributor`.  It is :func:`sync.push_item`
    plus the shipping-class slug; price, stock, brand and restricted
    states are read off the Item, which the caller has committed by the time
    this runs (``enqueue_after_commit=True``).

    ``pos_fields_only`` defaults to **False** (deliberate listing actions —
    bulk create, first push, mode flips, forced re-syncs — are full pushes).
    The ordinary ``*/15`` sync sets it True for a routine price drift on an
    already-synced listing, so the cron can no longer rewrite wp-admin content
    every fifteen minutes; lean still delivers price, stock,
    shipping class and the machine-owned meta (restricted states, fulfillment
    mode) — see ``sync._POS_OWNED_KEYS``.

    Errors are absorbed and logged exactly like :func:`push_item_task`:
    ``sync.push_item`` has already written ``woo_sync_error`` onto the Item, and
    one bad SKU must not kill the worker mid-way through a listing batch.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.push_item(
            item_code, site=site, shipping_class=shipping_class, listing=True,
            pos_fields_only=pos_fields_only)
    except Exception as exc:
        frappe.log_error(
            title=f"Woo distributor_listing_push_task failed: {item_code}",
            message=frappe.get_traceback(),
        )
        _report_listing_push_failure(item_code, site, exc)


@_for_a_running_store
def delist_serial_task(serial_no: str, site: str = "retail") -> None:
    """Set the WooCommerce product for a Serial No to ``draft`` + stock 0.

    Enqueue target for every serial delist (another channel took the gun, a
    non-sold exit, a reconcile repair).  Mirrors the error-handling
    pattern of :func:`push_serial_task` — errors are absorbed and logged via
    ``frappe.log_error`` so the background job does not crash the worker.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.delist_serial(serial_no, site=site)
    except Exception:
        frappe.log_error(
            title=f"Woo delist_serial_task failed: {serial_no} ({site})",
            message=frappe.get_traceback(),
        )


def _flag_rebucket_failure(
    serial_row_name: str | None, serial_no: str, target_item: str
) -> None:
    """Replace the reassuring in-flight note on a failed re-bucket with the truth.

    Without this the order keeps saying "POS is moving the gun, no action needed"
    forever while nothing is moving it — the queue would be lying to the person
    who has to ship the rifle. Best-effort and swallowing: the caller is already
    in a failure path, and a second failure there must not hide the first.
    """
    if not serial_row_name:
        return
    try:
        frappe.db.set_value(
            "Woo Online Order Serial", serial_row_name, "reservation_error",
            f"POS could not move serial {serial_no!r} into {target_item!r}. "
            f"Reassign the Serial No by hand, or pick another gun — see the "
            f"Error Log for why.")
        safe_commit()
    except Exception:  # noqa: BLE001 — never mask the original failure
        pass


def rebucket_and_apply_serial_task(
    serial_no: str, target_item: str, woo_online_order: str,
    serial_row_name: str | None = None, site: str = "retail"
) -> None:
    """Move a staff-picked gun into the listing Item its pool declares it sells
    through, then re-apply WooCommerce's serial to the waiting order row.

    Enqueue target for :func:`webhook._queue_rebucket`, which explains why this
    cannot run inside the webhook: the move submits two Stock Entries and commits,
    and the webhook holds ``for_update`` locks over a half-processed order.

    Ordering inside the job is load-bearing:

    * the order row is locked and re-checked BEFORE the move. It can be cancelled
      between the webhook and this job, and moving a gun for a dead order leaves
      the rifle in the wrong Item with nothing pointing at it.
    * the move is skipped when the gun is already there, which makes the job
      IDEMPOTENT. The retry sweep re-enqueues it every pass until the row pins,
      and `reassign_serial_to_item` would throw "already under" on each of those
      — turning ordinary recovery into a stream of Error Logs.
    * the re-apply goes through :func:`webhook._apply_serials_from_woo`, which
      re-reads the serials from WooCommerce. Staff may have corrected the entry
      while the job sat in the queue; the row is theirs, not this job's memory.

    On failure the transaction is rolled back and the row is re-flagged. Note the
    limit of that rollback: `reassign_serial_to_item` commits its own two legs, so
    a move that succeeded before a later step failed STAYS DONE. That is the safe
    direction — the gun is in the listing Item, the row is unpinned, and the next
    sweep pins it — but it is not "all or nothing", and reading it that way is how
    someone later adds a step here that must not be half-applied.

    Runs as Administrator: the webhook that queued it ran as Guest, and
    ``reassign_serial_to_item`` requires Serial No write.
    """
    from ffl_woo_sync.woocommerce import webhook

    prev_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        # Both inbound flags together (see webhook.woo_site): the REST re-read has
        # to hit the originating store, and the save must not echo back to Woo.
        with webhook.woo_site(site):
            if frappe.db.get_value(
                "Woo Online Order", woo_online_order, "order_status", for_update=True
            ) != "Pending Fulfillment":
                return
            if frappe.db.get_value("Serial No", serial_no, "item_code") != target_item:
                frappe.get_attr(
                    "ffl_integrations.fastbound.importer.reassign_serial_to_item"
                )(serial_no, target_item)
            order = frappe.get_doc("Woo Online Order", woo_online_order)
            if webhook._apply_serials_from_woo(order):
                order.flags.ignore_permissions = True
                order.save()
        safe_commit()
    except Exception:
        frappe.db.rollback()
        _flag_rebucket_failure(serial_row_name, serial_no, target_item)
        frappe.log_error(
            title=(f"Woo rebucket_and_apply_serial_task failed: "
                   f"{serial_no} -> {target_item} ({woo_online_order})"),
            message=frappe.get_traceback(),
        )
    finally:
        frappe.set_user(prev_user)


@_for_a_running_store
def archive_serial_sold_task(
    serial_no: str, sold_price: float | None = None, site: str = "retail"
) -> None:
    """Put a SOLD serial's WooCommerce product into the SOLD archive state
    (``publish`` + stock 0 + ``_osa_sold_*`` meta) instead of drafting it.

    Enqueue target for web fulfillment's per-serial branch.  Mirrors the
    error-handling pattern of :func:`delist_serial_task` — errors are absorbed
    and logged so the background job does not crash the worker.  A failed
    archive is picked up by reconcile Pass 3's archive arm on the next sweep
    (the ``{site}_sold_archived`` sentinel is only set on success).
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.archive_serial_sold(serial_no, sold_price=sold_price, site=site)
    except Exception:
        frappe.log_error(
            title=f"Woo archive_serial_sold_task failed: {serial_no} ({site})",
            message=frappe.get_traceback(),
        )


@_for_a_running_store
def sync_serial_listing_task(serial_no: str, site: str = "retail") -> None:
    """Reconcile ONE serial's WooCommerce listing with its committed stock
    state: sellable (Active, in a warehouse, not web-reserved) → push the
    product; gone (delivered/consumed/reserved) → retire it per
    :func:`sync.route_serial_exit` — SOLD archive if it sold, delist otherwise.

    Enqueued by the SLE hook whenever a LISTED per-serial firearm moves, and
    by the Sales Invoice / POS Invoice hooks when a counter order parks or
    releases a gun. ERPNext v16 flips Serial No status via a direct QB UPDATE
    (serial_batch_bundle.py) that never fires Serial No.on_update, so the
    stock ledger is the only reliable exit/return signal — this covers
    counter sales (Create Order dispose, POS), web-order stock-outs, and
    cancellations alike. Runs after commit, so the status flip has landed
    and the decision is made on the final state.

    This is the THIRD routing call site, and the one that
    matters most for counter sales: without it a gun sold at the register drafts
    to a 404 and only bounces back to a SOLD page on the next 2-hourly sweep —
    the worst possible window for a page that has accrued rankings. It is also
    how SP000279 was drafted in the first place.

    No ``sold_price`` is passed: the rule reserves the realized figure for web
    fulfillment, the one path carrying an exact order line.
    """
    from ffl_core.reservations import serial_shelf_state, shelf_state_columns

    from ffl_woo_sync.woocommerce import sync
    try:
        # ONE rulebook for "can this gun be sold right now", shared with the
        # counter, the consignment picker and every other channel. It absorbed
        # this function's own four-key test verbatim and added the rest: a
        # customer's transfer gun, an open counter hold, ANOTHER sales channel's
        # reservation, and GunBroker's "may already be sold" flag. Before it,
        # each caller carried its own copy and a gun sold on a second channel was
        # invisible to this one — a listing that stayed buyable after the sale.
        # SELECTed here and handed to the predicate so the read budget is what it
        # always was: one row, reused for the exit routing below.
        row = frappe.db.get_value(
            "Serial No", serial_no, shelf_state_columns(), as_dict=True)
        if not row:
            return
        state = serial_shelf_state(serial_no, row=row)
        # A customer's transfer gun is Active, has a warehouse and was never
        # reserved — byte for byte it looks like ordinary shelf stock — so it is
        # DELISTED rather than routed: it is not ours to sell and never sold.
        # (serial_shelf_state reads the flag off the row for the same reason this
        # function always did: the location half would buy a warehouse query per
        # serial, and push_serial checks both keys before publishing anyway.)
        if state["reason"] == "custody":
            sync.delist_serial(serial_no, site=site)
            return
        # A PARKED POS sale (Draft POS Invoice — a 4473 waiting on NICS, a
        # customer gone to the cash machine) is the one hold that is neither a
        # departure nor a sale: the gun is on the shelf, the order is cancelled
        # often, and the page is one a customer may be looking at right now. So
        # it gets the treatment a pending WEB order gets — stock 0, page intact
        # — rather than the delist the committed holds get.
        #
        # Two things make the distinction load-bearing rather than cosmetic.
        # `delist_serial` stamps `{site}_delisted`, and `route_serial_exit` rule
        # 2 reads that flag as "the business took this page down": a gun parked
        # here and then SOLD at the register would reach EXIT_SKIP and forfeit
        # the SOLD archive page it had earned — silently, on the commonest
        # firearm sale there is. And a 301'd page for a gun that is back on sale
        # twenty minutes later spends rankings for nothing.
        #
        # The committed holds keep delisting, unchanged: a submitted Sales
        # Invoice with a destination FFL means the gun is leaving the building
        # (settled earlier), and a Draft Consignment Out means
        # it is staged for a partner dealer. `kind` comes off the hold record in
        # `reservations.counter_holds`; only a Draft POS Invoice reads "pos".
        #
        # No `_relist`: a page an operator deliberately pulled stays pulled.
        if state["reason"] == "counter_hold" and \
                (state["detail"] or {}).get("kind") == "pos":
            sync.push_serial(serial_no, site=site, pos_fields_only=True)
            return
        sellable = state["sellable"]
        if sellable:
            # A serial that has become sellable again — returned, undisposed,
            # reserve cancelled, counter hold released — is a deliberate state
            # transition back onto the shelf, so it may clear a machine delist.
            # (An owner-hidden gun sitting still never reaches here: this task
            # only runs off a stock movement or a counter-hold change.)
            # pos_fields_only: a stock-movement trigger is a machine push — the
            # relist works lean because status/stock are POS-owned keys.
            sync.push_serial(serial_no, site=site, _relist=True, pos_fields_only=True)
            return
        exit_route = sync.route_serial_exit(serial_no, status=row.get("status"), site=site)
        if exit_route == sync.EXIT_ARCHIVE:
            sync.archive_serial_sold(serial_no, site=site)
        elif exit_route == sync.EXIT_DELIST:
            sync.delist_serial(serial_no, site=site)
        elif exit_route == sync.EXIT_SKIP:
            # EXIT_SKIP: already drafted on purpose — leave the
            # page and its 301 alone. Not even a redundant delist PUT.
            #
            # ...PROVIDED the store agrees. `{site}_delisted` is a POS-side
            # sentinel, and it can lie: serial 7008 (2026-09-03) carried
            # delisted=1 while its page was publish + stock 1, because the
            # relist push that should have cleared the flag had already PUT to
            # WC when its POS write-back hit a 1020. Trusting the flag there
            # left a sold gun buyable, and every reconcile arm filters on the
            # same flag, so nothing ever came back for it. One GET settles it:
            # a page that is really hidden keeps its draft + 301; a page that is
            # live gets the SOLD archive it was owed (archive_serial_sold clears
            # the stale sentinel, so this heals the record as well as the page).
            _archive_if_still_live(serial_no, site=site)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        # 1020 "Record has changed since last read" / 1205 lock wait timeout:
        # ERPNext's own Serial No status flip landed between this job's read
        # and its write-back — the counter-sale race that stranded 7008. The WC
        # PUT (if any) has gone out; the row READ is what is stale, and so is
        # the decision built on it. So unlike push_stock's handler (sync.py,
        # which rolls back and re-stamps because only its bookkeeping lost the
        # race and the decision was never in doubt), the right move here is to
        # re-run the whole decision: raise RetryBackgroundJobError and let
        # execute_job retry on the committed row (≤5×, back-off; it rolls back
        # for us). Neither class is a db.InternalError, so without this
        # conversion nothing retries. Swallowing it into an Error Log is how the
        # sale's exit routing never ran at all.
        #
        # Re-running repeats the WC PUT; that is safe because every path in
        # here is a full-state PUT keyed on an existing product_id (both
        # enqueue points guard on it), never a CREATE. If all 5 attempts lose,
        # execute_job logs under the bare method name — search
        # "sync_serial_listing_task", not this task's own title, for that case.
        raise frappe.RetryBackgroundJobError(
            f"Serial No {serial_no} changed under sync_serial_listing_task; retrying"
        )
    except Exception:
        frappe.log_error(
            title=f"Woo sync_serial_listing_task failed: {serial_no} ({site})",
            message=frappe.get_traceback(),
        )


def _archive_if_still_live(serial_no: str, *, site: str) -> None:
    """EXIT_SKIP's reality check: archive a sold serial whose page is still
    live despite ``{site}_delisted=1``. A hidden (or missing) page is left
    exactly as EXIT_SKIP intends."""
    from ffl_woo_sync.woocommerce import sync
    from ffl_woo_sync.woocommerce.client import WooCommerceError

    woo_id = frappe.db.get_value("Serial No", serial_no, sync._f(site, "product_id"))
    if not woo_id:
        return
    client = sync._client(None, site)
    try:
        wc = client.get_product(woo_id)
    except WooCommerceError as exc:
        # Dangling product_id: the page was deleted on the store (same shape
        # push_stock downgrades). Nothing to archive, and not an error worth
        # an Error Log per sale — leave the record as EXIT_SKIP found it.
        if exc.status_code == 404:
            frappe.logger("woo.sync").warning(
                f"exit check: WC product {woo_id} for Serial No {serial_no!r} "
                f"returned 404 — nothing live to archive.")
            return
        raise
    # A PUBLISH decision, so ask the positive question. is_hidden_on_woo is
    # fail-open on purpose (a payload missing `status` must not delist the
    # catalogue) — borrowing it here would flip that into "missing status ⇒
    # live ⇒ republish a deliberately drafted page and delete its 301".
    if (wc or {}).get("status") != "publish":
        return
    sync.archive_serial_sold(serial_no, client=client, site=site)


@_for_a_running_store
def delist_item_task(item_code: str, site: str = "retail") -> None:
    """Set the WooCommerce product for an Item to ``draft`` + stock 0.

    Mirrors the error-handling pattern of :func:`push_item_task`.
    """
    from ffl_woo_sync.woocommerce import sync
    try:
        sync.delist_item(item_code, site=site)
    except Exception as exc:
        frappe.log_error(
            title=f"Woo delist_item_task failed: {item_code} ({site})",
            message=frappe.get_traceback(),
        )
        _report_listing_push_failure(item_code, site, exc)


# ---------------------------------------------------------------------------
# Scheduled reconcile
# ---------------------------------------------------------------------------

def _due(last_dt, min_interval_seconds: int) -> bool:
    """Return True when enough time has elapsed since ``last_dt`` (or it is unset).

    Mirrors the ``_due`` helper in ``ffl_integrations.rsr.tasks``.
    """
    if not last_dt:
        return True
    elapsed = time_diff_in_seconds(now_datetime(), get_datetime(last_dt))
    return elapsed >= min_interval_seconds


def _wc_meta_value(wc: dict, key: str):
    """Return the value of WC product meta ``key`` (from ``meta_data``), else None."""
    return next(
        (m.get("value") for m in (wc.get("meta_data") or []) if m.get("key") == key),
        None,
    )


def _map_meta_drifted(wc: dict, item) -> bool:
    """Retail: the MAP meta the storefront hides by differs from the POS's
    (``mapping.map_meta``) — how a lost MAP / brand-mode re-push heals. An absent
    key reads as empty, so a product with no MAP never counts as drifted."""
    from ffl_woo_sync.woocommerce import mapping

    return any((_wc_meta_value(wc, key) or "") != value
               for key, value in mapping.map_meta(item).items())


def _wc_sale_drifted(wc: dict, sale_price, sale_from, sale_to) -> bool:
    """True when the store's sale trio differs from what POS would push.

    The expected side comes from the SAME builder every push uses
    (``mapping.sale_price_payload``) — comparator and payload can't drift
    apart. wp-admin's own formats converge here rather than false-loop: a
    hand-entered "22" heals once to "22.00", after which the two sides
    compare equal forever. WooCommerce flips an on-sale price on the window
    dates by itself, so date rollovers cause no drift and no push.
    """
    from ffl_woo_sync.woocommerce import mapping

    expected = mapping.sale_price_payload(sale_price, sale_from, sale_to)
    got = {
        "sale_price": (wc.get("sale_price") or "").strip(),
        "date_on_sale_from": wc.get("date_on_sale_from") or "",
        "date_on_sale_to": wc.get("date_on_sale_to") or "",
    }
    return got != expected


REPAIR_PUSH = "push"
REPAIR_DELIST = "delist"


def _drift_repair(sn) -> str:
    """Which repair does this drifted per-serial product need?

    Named, rather than left as a condition inside the reconcile loop, because it
    is the decision the whole cross-channel change turns on and it needs to be
    testable without standing up the entire sweep.

    A gun ANOTHER channel has sold is healed DOWNWARDS — draft plus stock 0 —
    not by re-pushing its product page. Two reasons, and either alone settles
    it: re-publishing a page for a gun somebody has already bought is not a
    repair, and it is the same route `sync_serial_listing_task` takes for the
    same class of gun (`route_serial_exit` -> `EXIT_DELIST`), so the hourly
    sweep becomes a real second chance for a delist that failed at the time of
    the sale instead of a differently-shaped no-op.

    OUR OWN web order is not such a channel, and asking the wrong predicate here
    is a quiet disaster rather than a visible one: every gun on a pending web
    order would be drafted within the hour AND stamped `{site}_delisted=1`,
    which `route_serial_exit` reads as "deliberately taken down" — so the gun
    completes its sale with no SOLD archive page and no row left for Pass 3a to
    rescue. A web order's gun heals the ordinary way: published, stock 0.

    Everything else keeps the ordinary lean push.
    """
    return (REPAIR_DELIST
            if other_channel_reservation(sn.name, own=CHANNEL_WOO, row=sn)
            else REPAIR_PUSH)


def _delist_or_filters() -> dict:
    """Pass 3b's net: every way a LISTED gun can have left the shelf.

    Each arm catches something the others cannot. `status != Active` is the
    ordinary sold/removed case. `consigned_out` and `is_transfer_gun` both stay
    Active indefinitely, so status can never reach them.

    The OTHER channels' arms are the same shape again: a gun bought on GunBroker
    is Active, in its warehouse, and looks like shelf stock to every other filter
    here. Without them, a delist that failed at the time of the sale has no
    second chance — Pass 2 only looks at guns whose WC state DRIFTED, and a
    product sitting correctly at publish/stock-1 for a gun that is now sold is
    not drift by any comparison Pass 2 makes. That is precisely how a gun sold on
    a marketplace stays buyable on the storefront forever.

    THIS channel's own flag is deliberately NOT an arm. A `woo_reserved` gun is
    on a pending web order of ours: it belongs on the storefront, published at
    stock 0, until the order completes and it becomes a SOLD archive page. Fish
    for it here and the hourly sweep drafts every gun being packed and stamps
    `{site}_delisted=1` on it — after which `route_serial_exit` answers EXIT_SKIP
    at fulfilment and the archive page is lost for good.

    `has_column`-guarded per channel: during a deploy this code is live before
    `bench migrate` has added the column, and the hourly sweep must not 500 for
    the length of that window.
    """
    filters = {
        "status": ["!=", "Active"],
        "consigned_out": 1,
        "is_transfer_gun": 1,
    }
    from ffl_core.reservations import CHANNEL_RESERVE_FIELDS

    for flag_field, _ref_field, channel in CHANNEL_RESERVE_FIELDS:
        if channel == CHANNEL_WOO:
            continue
        if frappe.db.has_column("Serial No", flag_field):
            filters[flag_field] = 1
    return filters


def listed_item_filters(site: str) -> dict:
    """Reconcile Pass 1's "this Item's product is on the store" predicate.

    A product id alone is not enough: ``mark_woo_delisted`` keeps the id when a
    human takes the product off the shop and clears ``{site}_listed`` instead.
    Shared with the
    category mirror (``categories.listed_nodes``) so both read one definition.
    Pass 1 also skips per-serial firearm Items in Python (no item-level product);
    that skip is not in here — harmless to the mirror: ``push_item`` never builds
    an item-level product for one, so such an Item carries no listed product id.
    """
    from ffl_woo_sync.woocommerce.sync import _f

    return {_f(site, "product_id"): ["!=", ""], _f(site, "listed"): 1}


def listed_serial_filters(site: str) -> dict:
    """Reconcile Pass 2's "this gun is on sale on the store" predicate.

    status=Active: a sold gun (``SOLD_STATUS``) is Pass 3a's SOLD archive page, not
    stock on sale. consigned_out=0: a consigned-out gun stays Active (it's still
    on hand — just at a partner FFL) and must not be reconciled as if it were
    ordinary sellable inventory. delisted=0: never re-push a gun
    the owner hid — push_serial refuses these anyway, but excluding
    them also saves a pointless WC GET per hidden gun per sweep and stops the
    sweep reading their deliberate draft state as "drift". is_transfer_gun=0: a
    customer's gun is not ours to put on the storefront at all.
    Shared with the category mirror (``categories.listed_nodes``).
    """
    from ffl_woo_sync.woocommerce.sync import _f

    return {_f(site, "product_id"): ["!=", ""], "status": "Active",
            "consigned_out": 0, "is_transfer_gun": 0, _f(site, "delisted"): 0}


def _reconcile_site(site: str) -> None:
    """Gate-and-run reconcile for a single site (``'retail'``).

    Reads the site's settings doctype, checks the ``auto_sync_enabled`` flag,
    ``reconcile_frequency``, and throttle interval, then queues
    :func:`_run_reconcile` (:func:`enqueue_reconcile`) when all gates pass.
    """
    from ffl_woo_sync.woocommerce.client import store_entry
    settings_dt = store_entry(site)["settings"]
    s = frappe.get_single(settings_dt)
    if not int(s.auto_sync_enabled or 0):
        return
    freq = (s.reconcile_frequency or "Daily").strip()
    if freq == _DISABLED:
        return
    min_secs = _RECONCILE_MIN_INTERVAL.get(freq, 20 * 3600)
    if not _due(s.last_reconcile_at, min_secs):
        return
    enqueue_reconcile(site)


# The sweep reads every listed product off the store and asks the stock hooks
# about each one: ~900 products took ~10 minutes in 2026-09, twice the default
# queue's 300 s, so it died part-way through Pass 1 every run and Passes 2-3
# never ran. It runs on the long queue with its own budget instead.
RECONCILE_QUEUE = "long"
RECONCILE_TIMEOUT = 1800
# lazy: one job holds the long queue's single worker for the whole sweep (1800 s cap, a
# batched sweep of ~1,000 products is a few minutes); past ~3,000 split Pass 1 into chunked jobs.


def reconcile_job_id(site: str) -> str:
    """One reconcile per store at a time: every enqueue uses this id, so
    ``deduplicate`` drops a second run while one is queued or running."""
    return f"woo-reconcile:{site}"


def enqueue_reconcile(site: str = "retail"):
    """Queue :func:`_run_queued_reconcile` (the gate again, then :func:`_run_reconcile`)
    for ``site`` on the long queue — never two at once (a run already queued or
    running wins; returns None then)."""
    return frappe.enqueue(
        "ffl_woo_sync.woocommerce.tasks._run_queued_reconcile",
        queue=RECONCILE_QUEUE,
        timeout=RECONCILE_TIMEOUT,
        job_id=reconcile_job_id(site),
        deduplicate=True,
        site=site,
    )


def _run_queued_reconcile(site: str = "retail") -> None:
    """The queued job: :func:`_run_reconcile`, unless the operator stopped it after
    the gate queued it (``auto_sync_enabled`` off, or ``reconcile_frequency``
    Disabled) — on the single-worker long queue it can wait behind other jobs."""
    from ffl_woo_sync.woocommerce.client import store_entry
    settings_dt = store_entry(site)["settings"]
    s = frappe.db.get_value(settings_dt, settings_dt,
        ["auto_sync_enabled", "reconcile_frequency"], as_dict=True) or {}
    if not int(s.get("auto_sync_enabled") or 0) or (s.get("reconcile_frequency") or "").strip() == _DISABLED:
        return
    _run_reconcile(site)


def scheduled_reconcile() -> None:
    """Throttled gate for the WooCommerce product reconcile.

    Called by the ``0 */2 * * *`` cron (every 2 hours — the finest supported
    frequency).  Reads WooCommerce Settings and queues :func:`_run_reconcile`
    on the long queue (:func:`enqueue_reconcile`) only when all gates pass:

    1. ``auto_sync_enabled`` is **1**.
    2. ``reconcile_frequency`` is not **"Disabled"**.
    3. Enough time has elapsed since ``last_reconcile_at`` (``_due()``).
    """
    _reconcile_site("retail")


# First-party hook (docs/extensions/hooks.md): ``fn(item_codes) -> context manager | None``,
# called once with every Pass-1 Item before the loop and held open around it, so
# a stock source can read what the whole pass needs in a few batched calls
# instead of one per Item.
STOCK_BATCH_HOOK = "distributor_stock_batch"


def _read_products(client, ids) -> dict:
    """``{str(product id): product}`` for a reconcile pass, read in pages of 100
    (``client.get_products``). Best effort: an id it does not answer — trashed,
    deleted, a page that failed — is read on its own by :func:`_product`, so a
    missing or binned product is handled exactly as the one-by-one sweep did."""
    if not ids:
        return {}
    try:
        got = client.get_products(ids)
    except JobTimeoutException:
        raise
    except Exception as exc:
        frappe.logger("woo.sync").warning(
            f"reconcile: batched product read failed, reading one by one: {str(exc)[:200]}")
        return {}
    return got if isinstance(got, dict) else {}


def _product(client, prefetched: dict, woo_id):
    """The product the batch read for ``woo_id``, else ``client.get_product``."""
    wc = prefetched.get(str(woo_id).strip())
    return wc if wc is not None else client.get_product(woo_id)


def _reread(client, prefetched: dict, woo_id, wc):
    """``wc`` read again on its own before the sweep acts on a row (a delist mark
    or a drift push), when it came from the pass's batch read: that snapshot can be
    minutes old, and a product a human drafted — or republished — since must be
    read as it is now. Once per row (the id leaves ``prefetched``); a row read one
    by one is already fresh."""
    return client.get_product(woo_id) if prefetched.pop(str(woo_id).strip(), None) is not None else wc


def _open_stock_batches(item_codes: list) -> contextlib.ExitStack:
    """Enter every :data:`STOCK_BATCH_HOOK` provider's context for ``item_codes``.
    A provider that raises (or answers something that is not a context manager)
    is logged and left out: the pass then runs unbatched for it — slower, same
    answers."""
    stack = contextlib.ExitStack()
    for path in frappe.get_hooks(STOCK_BATCH_HOOK) or []:
        try:
            batch = frappe.get_attr(path)(list(item_codes))
            if batch is not None:
                stack.enter_context(batch)
        except JobTimeoutException:
            stack.close()
            raise
        except Exception:
            frappe.log_error(title=f"Woo reconcile: {STOCK_BATCH_HOOK} provider failed ({path})",
                             message=frappe.get_traceback())
    return stack


def _close_stock_batches(stack: contextlib.ExitStack) -> None:
    """Leave the providers' contexts; a provider failing on the way out is logged,
    never allowed to cost the passes after Pass 1."""
    try:
        stack.close()
    except JobTimeoutException:
        raise
    except Exception:
        frappe.log_error(title=f"Woo reconcile: {STOCK_BATCH_HOOK} provider failed on exit",
                         message=frappe.get_traceback())


def _run_reconcile(site: str = "retail") -> None:
    """Compare POS stock/price to WooCommerce and push any drifted records.

    Every push this sweep makes is ``pos_fields_only``: it heals stock, price,
    and lifecycle state, and never rewrites wp-admin content (title,
    description, images, attributes) — see ``sync._POS_OWNED_KEYS``.

    Runs on the long queue (:func:`enqueue_reconcile`). Passes 1 and 2 read the
    store's products in pages of 100 (:func:`_read_products`; an id the page did
    not answer is read on its own), and Pass 1 runs inside every
    :data:`STOCK_BATCH_HOOK` provider's context so the drop-ship stock source
    reads the catalog once per pass instead of once per Item.

    **Pass 1 — item-level products** (non-firearm + uniform-price firearm):
    Fetches each listed product from WooCommerce, compares ``stock_quantity``,
    ``regular_price`` and the sale trio (``sale_price`` + schedule dates)
    against the POS ground truth, and calls
    :func:`~ffl_woo_sync.woocommerce.sync.push_item` when they differ.

    **Pass 2 — per-serial products** (Active Serial Nos with a site product_id):
    Compares ``stock_quantity`` (always 0 or 1) against the serial's
    ``woo_reserved`` flag and pushes via
    :func:`~ffl_woo_sync.woocommerce.sync.push_serial` on mismatch.

    **Pass 3 — listed serials no longer Active**:
    Finds Serial Nos that still have a site ``product_id`` but whose ``status``
    is not Active (sold — Delivered or Consumed — etc.) and delists them
    (``draft`` + stock 0)
    so they stop showing as available on the WooCommerce shop.

    On completion writes ``last_reconcile_at``, ``last_push_count``, and
    ``last_sync_error`` (and ``last_push_at`` when pushes were made) to the
    site's Settings doctype via ``frappe.db.set_value`` to bypass
    ``track_changes=1``.
    """
    from ffl_core.firearm import serial_sale_fields
    from ffl_woo_sync.woocommerce import mapping, sync
    from ffl_woo_sync.woocommerce.client import (
        WooCommerceClient, WooCommerceConfigError, store_entry,
    )
    from ffl_woo_sync.woocommerce.sync import SOLD_STATUS, _f

    settings_dt = store_entry(site)["settings"]

    try:
        client = WooCommerceClient.from_settings(site)
        attr_ids = client.get_attribute_ids()
    except WooCommerceConfigError as exc:
        frappe.db.set_value(settings_dt, settings_dt, {
            "last_reconcile_at": now_datetime(),
            "last_sync_error": str(exc)[:1000],
        })
        return

    pushed = 0
    errors: list[str] = []

    # ── Pass 1: item-level products ──────────────────────────────────────────
    item_filters = listed_item_filters(site)
    item_fields = ["name", _f(site, "product_id"), "is_firearm", "item_group", "uniform_sell_price", "standard_rate",
                   "sale_price", "sale_from", "sale_to", *mapping.MAP_ITEM_FIELDS]

    # Per-serial firearms have no item-level WC product — handled in pass 2.
    items = [item for item in frappe.get_all("Item", filters=item_filters, fields=item_fields)
             if not (_is_firearm_row(item) and not item.uniform_sell_price)]
    products = _read_products(client, [item[_f(site, "product_id")] for item in items])
    batches = _open_stock_batches([item.name for item in items])
    try:
        for item in items:
            try:
                wc = _product(client, products, item[_f(site, "product_id")])
                if wc is None:
                    continue
                # Price first, THEN human intent — the order is load-bearing.
                # An unpriced product is drafted by the guard below, and if the
                # hidden check ran first it would read OUR draft as a human delist
                # on the next sweep, laundering a machine action into a human one.
                price = mapping.web_price_for_item(item.name, item.standard_rate)
                if price <= 0:
                    # push_item refuses and drafts the $0 product; it
                    # also clears `listed`, so this item leaves Pass 1 afterwards.
                    sync.push_item(item.name, client=client, attr_ids=attr_ids, site=site,
                                   pos_fields_only=True)
                    # Counted: `last_push_count` / `last_push_at` mean "we wrote to
                    # WooCommerce this sweep", and drafting a $0 product IS such a
                    # write. Leaving it uncounted reported "nothing happened" on a
                    # sweep that had just pulled products off the shop.
                    pushed += 1
                    continue
                if sync.is_hidden_on_woo(wc):
                    wc = _reread(client, products, item[_f(site, "product_id")], wc)
                    if wc is None:
                        continue
                    if sync.is_hidden_on_woo(wc):
                        sync.mark_woo_delisted("Item", item.name, site=site)
                        continue
                # These comparators are the OTHER half of the POS-owned contract:
                # sync._POS_OWNED_KEYS decides what a lean push may WRITE, the
                # comparisons here decide when one FIRES — a new POS-owned field
                # that should self-heal needs both (see the allowlist's comment).
                wc_stock = int(wc.get("stock_quantity") or 0)
                wc_price = (wc.get("regular_price") or "0").strip()
                pos_stock = mapping.stock_for_item(item.name)
                pos_price = f"{price:.2f}"
                drifted = wc_stock != pos_stock or wc_price != pos_price
                if _wc_sale_drifted(
                    wc, item.get("sale_price"), item.get("sale_from"), item.get("sale_to"),
                ):
                    drifted = True
                if _map_meta_drifted(wc, item):
                    drifted = True
                if drifted:
                    wc = _reread(client, products, item[_f(site, "product_id")], wc)
                    if wc is None:
                        continue
                    if sync.is_hidden_on_woo(wc):
                        sync.mark_woo_delisted("Item", item.name, site=site)
                        continue
                    # pos_fields_only: reconcile heals stock/price drift — it must
                    # never rewrite wp-admin content (2026-08-12 overwrite incident).
                    sync.push_item(item.name, client=client, attr_ids=attr_ids, site=site,
                                   pos_fields_only=True)
                    pushed += 1
            except JobTimeoutException:
                raise
            except Exception as exc:
                errors.append(f"{item.name}: {str(exc)[:200]}")
    finally:
        _close_stock_batches(batches)

    # ── Pass 2: per-serial products ──────────────────────────────────────────
    serial_fields = ["name", _f(site, "product_id"), "woo_reserved", "item_code",
                     "sale_sell_price", "sale_from", "sale_to"]
    # Every OTHER channel's reservation columns, so Pass 2's "POS ground truth"
    # below sees a gun sold on GunBroker as stock 0. Without them the self-heal
    # runs BACKWARDS on exactly the case it exists for: a GunBroker sale whose
    # delist failed leaves woo_delisted=0 and stock 1 on both sides, reconcile
    # reads that as agreement, and the gun stays buyable on the storefront
    # indefinitely — the same drift it fixes within one sweep when the holder is
    # a web order. has_column-guarded because a site without ffl_integrations has
    # no such columns and a missing one would 500 the sweep.
    serial_fields += [c for c in channel_reserve_columns()
                      if c not in serial_fields
                      and frappe.db.has_column("Serial No", c)]
    serials = frappe.get_all(
        "Serial No",
        filters=listed_serial_filters(site),
        fields=serial_fields,
    )
    # Parent-Item sale meta for the retail sale comparator: the serial payload
    # builder resolves the promo through the same two-level rule as the regular
    # price (uniform parent → Item triple), so the comparator must too — one
    # batched read instead of a per-serial Item query. Skipped entirely when no
    # serial carries an item_code (nothing to resolve, and no wasted query).
    sale_parents: dict[str, "frappe._dict"] = {}
    codes = sorted({s.item_code for s in serials if s.item_code})
    if codes:
        sale_parents = {
            r.name: r
            for r in frappe.get_all(
                "Item",
                filters={"name": ["in", codes]},
                fields=["name", "uniform_sell_price", "sale_price", "sale_from", "sale_to",
                        *mapping.MAP_ITEM_FIELDS],
            )
        }
    serial_products = _read_products(client, [sn[_f(site, "product_id")] for sn in serials])
    for sn in serials:
        try:
            wc = _product(client, serial_products, sn[_f(site, "product_id")])
            if wc is None:
                continue
            # Price first, THEN human intent (see Pass 1 for why the order
            # matters).  The price check is ALSO the only thing that catches the
            # SG 551 case: POS(Active, stock 1) and WC(published, stock 1) agree
            # perfectly, so there is no drift to detect — being unpriced IS the
            # defect, and the old sweep had no way to see it.
            if sync.effective_serial_price(sn.name) <= 0:
                # Only when it is still ON SALE. A serial has no `listed` flag to
                # clear (the Item branch self-limits that way), so without this
                # check an unpriced gun would be re-drafted on EVERY sweep,
                # forever, and `last_push_count` would never fall back to 0 —
                # re-creating, in a new place, the noise that was meant to go away.
                #
                # Deliberately does NOT write `delisted`: this draft is OUR doing,
                # and recording it as a human delist is exactly the laundering the
                # unpriced-before-hidden ordering exists to prevent.
                if not sync.is_hidden_on_woo(wc):
                    sync.push_serial(sn.name, client=client, attr_ids=attr_ids, site=site,
                                     pos_fields_only=True)
                    pushed += 1  # a WC write happened — see Pass 1
                continue
            if sync.is_hidden_on_woo(wc):
                wc = _reread(client, serial_products, sn[_f(site, "product_id")], wc)
                if wc is None:
                    continue
                if sync.is_hidden_on_woo(wc):
                    sync.mark_woo_delisted("Serial No", sn.name, site=site)
                    continue
            # Comparator ↔ allowlist contract: see the Pass 1 note.
            wc_stock = int(wc.get("stock_quantity") or 0)
            # POS ground truth: 1 when active and no sales channel holds it;
            # 0 otherwise. The channel flags are not per store, and the row
            # already carries them from serial_fields.
            #
            # ANY channel, ours included — unlike `_drift_repair` two lines
            # below, and the difference is not an oversight. "How many are for
            # sale" is 0 for a gun on our own pending web order just as much as
            # for one sold on GunBroker; that has been this line's answer since
            # long before a second channel existed (`0 if sn.woo_reserved`).
            # What differs between the two holds is the REPAIR, not the count.
            pos_stock = 0 if channel_reservation(sn.name, row=sn) else 1
            drifted = wc_stock != pos_stock
            # Sale trio — resolved through the same two-level rule the payload
            # builder uses.
            parent = sale_parents.get(sn.item_code) or frappe._dict()
            if _wc_sale_drifted(wc, *serial_sale_fields(
                parent.get("uniform_sell_price"),
                parent.get("sale_price"), parent.get("sale_from"), parent.get("sale_to"),
                sn.get("sale_sell_price"), sn.get("sale_from"), sn.get("sale_to"),
            )):
                drifted = True
            if parent.get("name") and _map_meta_drifted(wc, parent):
                drifted = True
            if drifted:
                wc = _reread(client, serial_products, sn[_f(site, "product_id")], wc)
                if wc is None:
                    continue
                if sync.is_hidden_on_woo(wc):
                    sync.mark_woo_delisted("Serial No", sn.name, site=site)
                    continue
                if _drift_repair(sn) == REPAIR_DELIST:
                    sync.delist_serial(sn.name, client=client, site=site)
                else:
                    # pos_fields_only — same rule as Pass 1: heal the drift,
                    # never the content.
                    sync.push_serial(sn.name, client=client, attr_ids=attr_ids,
                                     site=site, pos_fields_only=True)
                pushed += 1
        except JobTimeoutException:
            raise
        except Exception as exc:
            errors.append(f"{sn.name}: {str(exc)[:200]}")

    # ── Pass 3: listed serials that left the shelf ───────────────────────────
    # A per-serial firearm that was sold (status in SOLD_STATUS — Delivered from
    # the counter, Consumed from a web order's Material Issue) or otherwise went
    # non-Active while still listed on WooCommerce will show as in-stock on the
    # shop indefinitely if not caught here.  The safety net splits along the
    # routing rule of sync.route_serial_exit — SOLD guns keep a live
    # page, everything else is drafted:
    #
    #   3a  status in SOLD_STATUS → archive (publish + stock 0 + sold meta)
    #   3b  the rest               → delist  (draft + stock 0)
    #
    # Each arm has its own sentinel (woo_sold_archived / woo_delisted, set by
    # the sync function on success) so a handled gun does NOT re-trigger a
    # redundant WC PUT on every reconcile cycle — keeping push_count accurate
    # and reducing unnecessary API pressure.  push_serial clears the sentinels
    # on a genuine relist, so a re-listed gun that later leaves again re-enters
    # the right arm.

    # ── Pass 3a: sold serials → SOLD archive ─────────────────────────────────
    # No sold_price: reconcile has no order line, and the rule forbids passing an
    # asking price off as the realized one.  All three immediate routing call
    # sites archive at exit time and set the sentinel, so a gun only reaches this
    # arm when one of them was missed or failed.
    #
    # delisted=0 is deliberate.  A serial that is BOTH sold and
    # already drafted is a historical record from before this lifecycle existed;
    # republishing it here would flip an unknown number of pages to `publish`
    # unsupervised within 2 hours of deploy, bypassing the owner gate that the
    # backfill exists to provide.  Those surface only through that confirmed
    # backfill.  Nothing NEW lands in this state — call site 3 archives counter
    # sales directly — so the population is finite and historical.
    sold_listed = frappe.get_all(
        "Serial No",
        filters={
            _f(site, "product_id"): ["!=", ""],
            _f(site, "sold_archived"): 0,
            _f(site, "delisted"): 0,
            "status": ["in", list(SOLD_STATUS)],
        },
        fields=["name", _f(site, "product_id"), "status", "is_transfer_gun"],
    )
    for sn in sold_listed:
        try:
            # A custody gun must never become a SOLD page: its handback runs
            # through the POS transfer invoice, so it ends up wearing a
            # sold-shaped status and would otherwise be published as "SOLD for
            # $X" — a gun this store never sold, at a price it never charged.
            #
            # ROUTED here rather than filtered out of the query. An
            # earlier revision excluded it with is_transfer_gun=0 and trusted
            # Pass 3b to catch it — but 3b's base filters pin
            # status NOT IN SOLD_STATUS, so a sold-shaped custody gun matched
            # NEITHER arm and its listing would have stayed published, in stock,
            # forever. Branching inside the one arm that already selects these
            # rows keeps both cases covered without a second query.
            if sn.get("is_transfer_gun"):
                sync.delist_serial(sn.name, client=client, site=site)
            else:
                sync.archive_serial_sold(sn.name, client=client, site=site)
            pushed += 1
        except JobTimeoutException:
            raise
        except Exception as exc:
            errors.append(f"{sn.name}: {str(exc)[:200]}")

    # ── Pass 3b: non-sold exits → delist (draft) ─────────────────────────────
    # status not in SOLD_STATUS: without it a sold gun would be drafted here AND
    # archived by 3a, flapping its page between 404 and 200 every sweep.
    # sold_archived=0: an already-archived page must never be drafted — the
    # whole point is that it keeps serving 200 with its accrued rankings.
    #
    # OR consigned_out=1: a consigned-out gun stays Active the whole time
    # it's at the dealer (unlike a sold gun, which eventually goes non-
    # Active), so the plain status!=Active check can never catch it —
    # it needs its own arm of the safety net.
    # 3a owns the sold statuses, so they stay out of here (else the same page would be
    # drafted by one arm and published by the other, flapping between 404 and 200).
    delist_filters: dict = {
        _f(site, "product_id"): ["!=", ""],
        _f(site, "delisted"): 0,
        _f(site, "sold_archived"): 0,
        "status": ["not in", list(SOLD_STATUS)],
    }
    inactive_listed = frappe.get_all(
        "Serial No",
        filters=delist_filters,
        or_filters=_delist_or_filters(),
        fields=["name", _f(site, "product_id"), "status"],
    )
    for sn in inactive_listed:
        try:
            sync.delist_serial(sn.name, client=client, site=site)
            pushed += 1
        except JobTimeoutException:
            raise
        except Exception as exc:
            errors.append(f"{sn.name}: {str(exc)[:200]}")

    # ── Write status fields via db.set_value (bypasses track_changes audit log) ──
    error_text = "\n".join(errors[:10]) if errors else ""
    status: dict = {
        "last_reconcile_at": now_datetime(),
        "last_push_count": pushed,
        "last_sync_error": error_text,
    }
    if pushed:
        status["last_push_at"] = now_datetime()
    frappe.db.set_value(settings_dt, settings_dt, status)


# ---------------------------------------------------------------------------
# Stuck serial-reservation retry — the missing trigger
# ---------------------------------------------------------------------------

# Stop auto-retrying a rejection this old. Mirrors the distributor sweep's
# SCAN_MAX_AGE_DAYS idea: past this point the cause is not "the other order
# hasn't released the gun yet", it is something only a human will clear.
_RETRY_MAX_AGE_DAYS = 14
# Ceiling per run. Each order costs one synchronous GET /orders/{id} (30s timeout,
# 3 retries), so an unbounded batch against a slow store would run until the
# worker killed it — mid-sweep, with nobody told.
_RETRY_BATCH_LIMIT = 200
# Cap on how many aged-out orders one give-up report names. They are counted with
# a separate query, so they never consume a retry slot.
_RETRY_AGED_LIMIT = 200
# Aged-out orders stay aged out forever by construction, so the give-up report is
# throttled to daily. Same cache-gate shape sync.py uses for its degraded-store
# notice — the in-app answer to "say it once", since notify_admins' dedup_key
# lives in ffl_integrations and this app may not import it.
_AGED_ALERT_KEY = "woo_serial_retry_aged_reported"
_AGED_ALERT_TTL = 24 * 3600

# Shared predicate for "a serial row that is genuinely stuck". Written once and
# used by BOTH the retry query and the aged-out count so the two can never drift
# into disagreeing about what stuck means.
_STUCK_FROM_WHERE = """
        FROM `tabWoo Online Order Serial` s
        INNER JOIN `tabWoo Online Order` o ON o.name = s.parent
        WHERE s.parenttype = 'Woo Online Order'
          AND o.order_status = 'Pending Fulfillment'
          AND COALESCE(s.reservation_error, '') != ''
          AND COALESCE(s.confirmed_serial, '') = ''
          AND COALESCE(NULLIF(s.fulfillment, ''), 'local') IN %(lanes)s
          AND COALESCE(s.reservation_error, '') NOT LIKE %(no_stock)s
"""


def _site_sync_enabled(site: str) -> bool:
    """True when this store's Settings has ``auto_sync_enabled``.

    The same master switch :func:`_reconcile_site` and
    ``order_audit._gate_and_audit`` read before touching a store. A sweep that
    skipped it would keep calling a shop the owner has deliberately
    disconnected — and since the REST helper logs an Error Log on every config
    failure, an unconfigured store would be spammed forever.

    A blank site is the retail store, as everywhere. Only a site with no store is
    answered False — a row whose ``site`` is not one we serve has nothing to sync
    to. A Settings read that blows up is left to raise, the way the sibling gates
    do: turning a database fault into a silent "this store is switched off" would
    hide an outage behind a counter.
    """
    from ffl_woo_sync.woocommerce.client import STORES, resolve_site
    site = resolve_site(site)
    if site is None:
        return False
    return bool(int(frappe.get_single(STORES[site]["settings"]).auto_sync_enabled or 0))


def _pinned_count(order) -> int:
    """How many of the order's serial rows currently carry a confirmed serial."""
    return sum(1 for r in (order.serials or []) if (r.confirmed_serial or "").strip())


def _report_aged_out(aged: list) -> None:
    """Name the orders this sweep has stopped retrying — once a day, not once a run.

    Silence would make "no longer retried" indistinguishable from "nothing
    wrong". But an unthrottled report is its own failure: aged-out rows stay aged
    out by construction, so logging every run means twelve identical entries a
    day forever — the exact "permanent failure becomes permanent noise" this
    cut-off exists to prevent. Throttled through the cache, the same shape
    ``sync.py`` uses for its unsupported-brand notice; ``notify_admins``' own
    ``dedup_key`` lives in ffl_integrations, which this app must not import.
    """
    if frappe.cache().get_value(_AGED_ALERT_KEY):
        return
    frappe.log_error(
        title=f"Woo serial retry gave up on {len(aged)} stale order(s)",
        message=(
            f"These orders have carried the same serial rejection for more than "
            f"{_RETRY_MAX_AGE_DAYS} days and are no longer retried automatically:\n\n"
            + "\n".join(r["name"] for r in aged)
            + "\n\nFix the serial on the WooCommerce order, or once the cause is "
            "cleared run ffl_woo_sync.woocommerce.tasks."
            "retry_stuck_serial_reservations by hand."
        ),
    )
    # Persist the report BEFORE shutting the gate. The gate is in redis and does
    # not roll back; the Error Log insert does. Closing it first meant a failure
    # on the very first order could roll the report away while the gate stayed
    # shut for 24h — losing that day's give-up list entirely.
    safe_commit()
    frappe.cache().set_value(_AGED_ALERT_KEY, 1, expires_in_sec=_AGED_ALERT_TTL)


_SHIPPED_UNDISPOSED_KEY = "woo_shipped_undisposed_reported"
_SHIPPED_UNDISPOSED_TTL = 24 * 3600
_SHIPPED_UNDISPOSED_GRACE_HOURS = 12

# A web order WooCommerce considers gone while POS still holds it open. One
# predicate, so the alarm and any future auto-repair cannot drift apart about
# what "shipped but not disposed" means.
#
# `woo_status = 'completed'` is the signal: the store's own ShipStation plugin
# flips it when the label is used. The grace period is because a normally-flowing
# order passes through exactly this state between the shipment and the Dispose
# click — without it every healthy order would alarm for a few minutes.
_SHIPPED_UNDISPOSED_SQL = """
        SELECT o.name, o.woo_order_id, o.customer_name, o.modified
        FROM `tabWoo Online Order` o
        WHERE o.order_status = 'Pending Fulfillment'
          AND LOWER(COALESCE(o.woo_status, '')) = 'completed'
          AND o.modified < DATE_SUB(NOW(), INTERVAL %(grace)s HOUR)
        ORDER BY o.modified
"""


def _report_shipped_undisposed(rows: list) -> None:
    """Name the orders WooCommerce says shipped while POS still has them open.

    A shipped firearm with no `FFL Disposition` is a missing A&D bound-book
    entry, and the gap is silent: POS believes the rifle is still on the shelf
    (`status=Active`, in `Stores - <ABBR>`) and may even keep it reserved for that
    order. Seven Mosin orders sat like that through 2026-08-14 and were found by
    hand, while investigating something else.

    Throttled to once a day through the cache, and committed BEFORE the gate is
    shut, for the reasons :func:`_report_aged_out` documents at length: the
    condition is permanent by construction until somebody acts, so an unthrottled
    report is twelve identical Error Logs a day; and closing the redis gate first
    can lose the report while keeping the gate shut, because redis does not roll
    back and the Error Log insert does.
    """
    if frappe.cache().get_value(_SHIPPED_UNDISPOSED_KEY):
        return
    frappe.log_error(
        title=f"⚠️ {len(rows)} web order(s) shipped on WooCommerce, never disposed in POS",
        message=(
            "WooCommerce reports these orders completed — the store's ShipStation "
            "plugin marks an order completed when its label is used — but POS "
            "still has them Pending Fulfillment. No FFL Disposition was booked, "
            "so the A&D bound book is missing an entry for a firearm that has "
            "left the building.\n\n"
            + "\n".join(
                f"{r['name']} (Woo #{r['woo_order_id']}, {r['customer_name'] or '?'})"
                for r in rows)
            + "\n\nDispose each one in POS. If a serial was never entered, which "
            "rifle went in the box is recorded nowhere — reconcile against "
            "ShipStation and FastBound before booking."
        ),
    )
    safe_commit()
    frappe.cache().set_value(_SHIPPED_UNDISPOSED_KEY, 1,
        expires_in_sec=_SHIPPED_UNDISPOSED_TTL)


@frappe.whitelist()
def alert_shipped_undisposed_orders() -> dict:
    """Report web orders WooCommerce says shipped while POS still holds them open.

    A shipped firearm with no disposition is a missing A&D entry, and nothing
    surfaced it before — the seven such orders on 2026-08-14 were found by hand.

    Reports only; it never disposes anything. Which rifle went in the box is
    exactly what POS does not know in this state, and guessing it is the mistake
    the whole serial-handling rework exists to stop.

    Returns:
        {checked, reported} — `reported` is 0 when the daily throttle suppressed
        an otherwise non-empty report.

    Permissions:
        Counter roles (manual path; the scheduler runs as Administrator).
    """
    from ffl_core.api.manual_order import _ROLES

    if frappe.session.user != "Administrator":
        frappe.only_for(_ROLES)
    rows = frappe.db.sql(_SHIPPED_UNDISPOSED_SQL,
        {"grace": _SHIPPED_UNDISPOSED_GRACE_HOURS}, as_dict=True)
    if not rows:
        return {"checked": 0, "reported": 0}
    throttled = bool(frappe.cache().get_value(_SHIPPED_UNDISPOSED_KEY))
    _report_shipped_undisposed(rows)
    return {"checked": len(rows), "reported": 0 if throttled else len(rows)}


@frappe.whitelist()
def retry_stuck_serial_reservations() -> dict:
    """Re-apply the serial WooCommerce still holds to rows stuck on a STALE
    ``reservation_error``.

    A rejection is a snapshot of the moment it was written ("that gun is held by
    WOO-ORD-x"). The only thing that ever retried it was the next
    ``order.updated`` webhook — so when the obstacle cleared (the other order
    cancelled, or swapped to a different gun) nothing re-ran, and a Woo order that
    has gone ``completed`` sends no further webhook. The row then sat in the
    Pending Order queue indefinitely, quoting a reason that had stopped being
    true, and staff had no way to clear it but to re-save the serial on Woo.
    (2026-08-07: WOO-ORD-01488 was refused C2322, which WOO-ORD-01491 released
    eight minutes later; only an unrelated update two hours on unstuck it.)

    Whitelisted so staff can run it on demand rather than waiting out the cron —
    which also gives the aged-out orders below a manual way back in.

    Bounded on every axis, because this is an unattended writer that calls an
    external store:

    * ``LIMIT`` per run, oldest first;
    * orders older than ``_RETRY_MAX_AGE_DAYS`` are reported **once** and then
      left alone — a sweep that retries forever is how a permanent failure turns
      into permanent noise (the lesson ``_fb_error_is_terminal`` encodes);
    * out-of-stock rejections are excluded outright: no dealer serial exists on
      Woo to re-apply, so re-reading that order could never fix it;
    * per-site ``auto_sync_enabled`` gate, one Settings read per site.

    Returns:
        A summary dict (retried / cleared / aged-out / skipped counts) so
        a manual run says what it did.

    Permissions:
        Counter roles (manual path; the scheduler runs as Administrator).
    """
    from ffl_core.api.manual_order import _ROLES

    from ffl_woo_sync.woocommerce import sync, webhook
    from ffl_woo_sync.woocommerce.fulfillment import _LOCALLY_DISPOSED_LANES

    # The scheduler runs as Administrator, which only_for waves through; this
    # guards the MANUAL path. Whitelisted without it, any logged-in user — dealer
    # portal accounts included — could drive a writer that pins guns, takes stock
    # reservations and posts to WooCommerce. Same canonical role set the rest of
    # this app's firearm-touching endpoints use.
    frappe.only_for(_ROLES)

    params = {
        "no_stock": f"{webhook._NO_STOCK_REASON}%",
        "lanes": tuple(_LOCALLY_DISPOSED_LANES),
        "max_age_days": _RETRY_MAX_AGE_DAYS,
        "limit": _RETRY_BATCH_LIMIT,
        "aged_limit": _RETRY_AGED_LIMIT,
    }
    # The age cut-off is applied IN SQL, before the limit — never after it.
    # Aged-out orders never leave the candidate set (nothing clears them), so
    # taking the oldest N first and discarding the aged ones afterwards would let
    # them fill every slot and starve the fresh ones. Past N of them the sweep
    # would go quietly no-op — failing in exactly the way the bug it fixes fails.
    # Age is measured on the SERIAL ROW's modified stamp — roughly how long this
    # rejection has stood — not on the order's creation. An order placed months
    # ago and only refused a serial today is a fresh problem; judging it by
    # creation would retire it before its first retry, and an order can sit for
    # weeks waiting on a gun.
    #
    # KNOWN LIMIT: `modified` is a proxy, not a true
    # first-refused clock. Frappe stamps every child row on any parent save, so a
    # rejection whose TEXT changes — `_reject_serial_row` keys idempotency on the
    # whole reason string, and that string embeds the holding order number and a
    # stock-derived suggestion — refreshes this stamp and never ages out. It stays
    # bounded by the per-run LIMIT and cannot mis-pin a gun; the cost is that such
    # an order keeps costing one REST call per run. The real fix is a stable
    # dedupe key (which also stops the repeat Woo notes); a true clock would need
    # a new field, which is ruled out.
    stuck = frappe.db.sql(
        f"""
        SELECT o.name, o.site, o.woo_order_id
        {_STUCK_FROM_WHERE}
        GROUP BY o.name, o.site, o.woo_order_id
        HAVING MAX(s.modified) >= DATE_SUB(NOW(), INTERVAL %(max_age_days)s DAY)
        ORDER BY MAX(s.modified)
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )
    # Aged-out orders get their own query so they never occupy a retry slot.
    # HAVING keeps the two sets disjoint: a multi-gun order with one fresh
    # rejection stays in the retry set rather than being written off.
    aged = frappe.db.sql(
        f"""
        SELECT o.name
        {_STUCK_FROM_WHERE}
        GROUP BY o.name
        HAVING MAX(s.modified) < DATE_SUB(NOW(), INTERVAL %(max_age_days)s DAY)
        ORDER BY MAX(s.modified)
        LIMIT %(aged_limit)s
        """,
        params,
        as_dict=True,
    )
    out = {"checked": 0, "pinned": 0, "re_rejected": 0, "aged_out": len(aged),
           "site_disabled": 0, "not_pending": 0, "failed": 0}
    if aged:
        _report_aged_out(aged)
    if not stuck:
        return out

    enabled: dict[str, bool] = {}
    for row in stuck:
        try:
            site = row.get("site") or sync.RETAIL_SITE
            # The gate reads that store's Settings, which can itself fail. Keep it
            # INSIDE the per-order guard: raised out here, one unreadable store
            # would strand every remaining order behind it — including all the
            # ones on a perfectly healthy site.
            if site not in enabled:
                enabled[site] = _site_sync_enabled(site)
            if not enabled[site]:
                out["site_disabled"] += 1
                continue
            # Both inbound flags, set and restored together (see webhook.woo_site):
            # woo_sync_site scopes the REST read to the order's store — and
            # woo_sync_inbound keeps the save from echoing back to Woo.
            with webhook.woo_site(site):
                # Fetch BEFORE taking any lock. This call can burn ~90s against a
                # slow store (30s timeout × 3 retries), and holding an order-row X
                # lock across it would stall a concurrent cancel/update webhook.
                # A stalled webhook is not a delayed webhook: woo_event returns
                # HTTP 200 even when its handler fails, so WooCommerce never
                # retries and the whole event — FFL change, refund row and all —
                # is LOST. Keeping I/O outside the lock also shrinks the window
                # for the lock-order inversion noted in the PR.
                fetched = webhook._fetch_order_serials_and_status(
                    str(row.get("woo_order_id") or "").strip())
                out["checked"] += 1
                # Only now take the lock, so the status re-check and the
                # reservation write land in one transaction with no I/O between
                # them. A cancel that arrived while we were on the network wins.
                if frappe.db.get_value(
                    "Woo Online Order", row["name"], "order_status", for_update=True
                ) != "Pending Fulfillment":
                    out["not_pending"] += 1
                    safe_commit()  # release the row lock before moving on
                    continue
                order = frappe.get_doc("Woo Online Order", row["name"])
                before = _pinned_count(order)
                if webhook._apply_serials_from_woo(order, fetched=fetched):
                    order.flags.ignore_permissions = True
                    order.save()
                    gained = _pinned_count(order) - before
                    out["pinned"] += gained
                    if not gained:
                        # A change that pinned no gun is a NEW rejection reason.
                        # Counting it as success would read as "fixed" to whoever
                        # ran this by hand — the opposite of what happened.
                        out["re_rejected"] += 1
            safe_commit()
        except Exception as exc:  # noqa: BLE001
            # One unreachable store or one bad order must not strand every other
            # stuck order behind it.
            frappe.db.rollback()
            out["failed"] += 1
            frappe.log_error(
                title=f"Woo serial retry failed for {row['name']}",
                message=str(exc),
            )
    return out
