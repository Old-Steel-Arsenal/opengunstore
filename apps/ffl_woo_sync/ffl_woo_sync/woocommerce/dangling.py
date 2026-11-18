"""woo_audit_dangling_ids — find (and optionally clear) Woo product ids whose
WooCommerce product was deleted on the store.

A "dangling" id is a stored ``woo_product_id`` on an Item or Serial No
whose WooCommerce product now 404s. It causes recurring push_stock / push_item
404s (which ``sync.py`` downgrades to warnings) with no self-healing — the CZ-75
incident. This is the durable garbage collector for them:

  * report-only by default; ``fix=1`` is the explicit write gate;
  * clears an id by nulling it + a Comment on the record + a warning log;
  * NEVER re-creates a product — a deletion on Woo may be deliberate, and silently
    re-listing it would be reverse-pollution;
  * a non-404 API error is a real failure and propagates, never mis-recorded.

Callable over HTTP and via
``bench execute ffl_woo_sync.woocommerce.dangling.woo_audit_dangling_ids``.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from ffl_woo_sync.woocommerce.client import (
    SITES,
    WooCommerceClient,
    WooCommerceConfigError,
    WooCommerceError,
    assert_store_site,
)
from ffl_woo_sync.woocommerce import mapping
from ffl_woo_sync.woocommerce.sync import _f, _item_target

_MAX_BATCH = 200
# The doctypes that carry a woo_product_id (fixtures/custom_field.json).
_DOCTYPES = ("Item", "Serial No")


def _clamp_batch_size(n) -> int:
    """1..200, defaulting to 50 — a fat-finger clamp on batch_size, not a rate limit."""
    n = cint(n) or 50
    return max(1, min(n, _MAX_BATCH))


def _candidates(doctype: str, field: str) -> list:
    """Rows of *doctype* whose *field* (a stored Woo product id) is non-blank.

    ``["!=", ""]`` excludes both the empty string and NULL in SQL, so a never-listed
    record is skipped.
    """
    return frappe.get_all(doctype, filters={field: ["!=", ""]}, fields=["name", field])


def _is_dangling(client, woo_id: str, parent: str = "") -> bool:
    """True iff the WC product for *woo_id* (with *parent*: that variable product's
    variation) was deleted (404). A non-404 error is a
    real API failure and is re-raised — never silently classed as 'deleted' (which
    would clear a live listing under fix=True)."""
    try:
        if parent:
            client.get_variation(parent, woo_id)
        else:
            client.get_product(woo_id)
        return False
    except WooCommerceError as exc:
        if exc.status_code == 404:
            return True
        raise


def _clear(doctype: str, name: str, field: str, woo_id: str, site: str) -> None:
    """Null the dangling id and leave an audit trail. No commit here — the HTTP /
    bench request boundary owns the transaction (docs/testing.md)."""
    frappe.db.set_value(doctype, name, field, None, update_modified=False)
    frappe.get_doc(doctype, name).add_comment(
        "Comment",
        _("woo_audit_dangling_ids cleared a dangling {0}: WC product {1} was deleted "
          "on the {2} store (404). Re-list to create a fresh product.").format(
            field, woo_id, site),
    )
    frappe.logger("woo.audit").warning(
        f"woo_audit_dangling_ids: cleared {doctype} {name!r} {field}={woo_id!r} "
        f"({site} store) — WC product was deleted."
    )


def _audit_site(site: str, batch_size: int, fix: bool, dangling: list) -> int:
    """Verify one store's stored product ids, appending to *dangling*.

    The client is built lazily — on the first batch that actually has rows — so a
    store with nothing listed on it never needs to be configured at all.

    Args:
        site: "retail".
        batch_size: Already clamped; bounds each verification chunk.
        fix: Null each dangling id (with a Comment + warning log).
        dangling: Accumulator, appended to in place.

    Returns:
        How many ids were verified against WooCommerce.

    Raises:
        WooCommerceConfigError: no usable client for this store. Raised by the
            build, i.e. before anything on this store has been checked or
            cleared, which is what lets the caller drop the store whole.
    """
    client = None
    field = _f(site, "product_id")
    checked = 0
    for doctype in _DOCTYPES:
        rows = _candidates(doctype, field)
        for start in range(0, len(rows), batch_size):
            if client is None:
                client = WooCommerceClient.from_settings(site)
            for row in rows[start:start + batch_size]:
                woo_id = str(row.get(field) or "").strip()
                if not woo_id:
                    continue
                parent = ""
                if doctype == "Item":
                    kind, _template, parent = _item_target(row["name"], site)
                    if kind == mapping.VARIANT and not parent:
                        continue  # nothing to ask under; the template's own id is audited
                checked += 1
                if _is_dangling(client, woo_id, parent):
                    dangling.append({
                        "doctype": doctype, "name": row["name"],
                        "field": field, "site": site, "woo_id": woo_id,
                    })
                    if fix:
                        _clear(doctype, row["name"], field, woo_id, site)
    return checked


@frappe.whitelist()
def woo_audit_dangling_ids(fix=0, batch_size=50, site=None):
    """Audit stored Woo product ids for ones whose WC product was deleted (404).

    Scans every connected store by default, and a store it cannot build a client
    for is reported as skipped rather than aborting the run: letting the config
    error out of here would 500 the request AND roll back any repairs already
    made in the same transaction.

    Args:
        fix: Report-only by default; 1 (or True) nulls each dangling id
            and leaves a Comment + warning log.
        batch_size: Clamped to 1..200; bounds each verification chunk — a
            fat-finger clamp on the parameter, not a rate limit; every
            candidate is checked.
        site: Limits to one store — "retail", the only one (default: all).
            Any other value raises a ValidationError.

    Returns::

        {
          "checked": <int>,                 # ids verified against WooCommerce
          "dangling": [{"doctype","name","field","site","woo_id"}, ...],
          "fixed": <int>,                   # ids nulled (0 unless fix)
          "skipped": [{"site","reason"}, ...],   # stores with no usable client
          "counts": {"by_doctype": {...}, "by_site": {...}},
        }
    """
    frappe.only_for("System Manager")
    fix = bool(cint(fix))
    batch_size = _clamp_batch_size(batch_size)
    if site:
        assert_store_site(site)
    sites = [site] if site else list(SITES)

    dangling: list = []
    skipped: list = []
    checked = 0

    for s in sites:
        try:
            checked += _audit_site(s, batch_size, fix, dangling)
        except WooCommerceConfigError as exc:
            skipped.append({"site": s, "reason": str(exc)})

    by_doctype: dict = {}
    by_site: dict = {}
    for d in dangling:
        by_doctype[d["doctype"]] = by_doctype.get(d["doctype"], 0) + 1
        by_site[d["site"]] = by_site.get(d["site"], 0) + 1

    return {
        "checked": checked,
        "dangling": dangling,
        "fixed": len(dangling) if fix else 0,
        "skipped": skipped,
        "counts": {"by_doctype": by_doctype, "by_site": by_site},
    }
