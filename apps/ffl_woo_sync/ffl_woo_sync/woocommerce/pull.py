"""Woo → POS product pull (operator-triggered content overwrite).

The reverse of the push pipeline, for exactly ONE product at a time, and only
on an operator's explicit click — the storefront plugin's product-page "overwrite POS
from WooCommerce" button.
Nothing in this module is scheduled or hooked; there is no bulk entrypoint on
purpose.  The whitelisted wrapper lives in ``client_api.pull_product_from_woo``.

What it writes (title + description + price + images):

=============  ======================  =====================================
Woo field      Item branch             Serial No branch (per-serial gun)
=============  ======================  =====================================
name           ``item_name``           ``item_name`` (per-gun listing title)
description    ``description`` (HTML   ``description`` — converted to PLAIN
               stored as-is)           TEXT (the field is a plain Text field;
                                       storing HTML would be double-escaped
                                       by the next full push)
regular_price  ``standard_rate`` AND   ``sell_price`` (only when the parent
               the selling Item Price  is NOT uniform-priced — under uniform
               via the canonical       pricing the serial tag is ignored by
               ``_set_item_selling_    ``firearm.serial_sell_price``, so the
               price`` helper (a bare  write would be a silent no-op; the
               ``standard_rate`` write pull records a ``price_note`` instead)
               does not change what
               the register charges)
sale_price /   ``sale_price`` +        ``sale_sell_price`` + ``sale_from`` /
date_on_sale_* ``sale_from`` /         ``sale_to`` (same uniform-parent guard
               ``sale_to`` — written   as the regular price). Unlike the
               UNCONDITIONALLY: a      regular price, 0/empty is meaningful —
               cleared wp-admin sale   it means "no sale", so the pull adopts
               must clear POS too      it instead of skipping
images         ``image_gallery`` rebuilt from downloaded copies; first image
               becomes ``is_primary`` and the single ``image`` field
=============  ======================  =====================================

Stock and attributes are deliberately NOT pulled: stock's SSOT is the POS,
and attributes/categories are derived from POS master data.

Canonical pieces reused rather than re-rolled: the SKU split
is ``webhook._map_sku_to_pos`` (the same rule web orders resolve by), the
echo guard is ``webhook.woo_site`` (BOTH inbound flags, save/restore), HTML
flattening is ``frappe.core.utils.html_to_plain_text`` (script/style
stripped, blocks → newlines, inline stays inline), and the Item price lands
through ``receive_goods._set_item_selling_price``.

Note on Serial No fields: ``item_name`` (fetch_if_empty) and ``description``
survive the save only because ffl_core's property setters cleared their
ERPNext ``fetch_from``/read-only defaults — ``test_pull_from_woo`` pins that
fixture contract so a fixture regression cannot silently revert this feature.
"""
from __future__ import annotations

import re

import frappe
import requests
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.utils import flt

from ffl_woo_sync.woocommerce.mapping import listing_web_price

# Import caps — a product gallery is a handful of photos, not an archive.
MAX_IMAGES = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image

# Wall-clock budget for the IMAGE segment.  The pull runs synchronously
# inside the caller's HTTP request (the plugin AJAX proxy allows 90s), and
# the request's total is roughly WC GET (request_timeout_seconds, default 30)
# + this budget + one in-flight image (≤10s) + the save — the 90s ceiling and
# the web tier's own read timeout must cover that sum (deploy checklist item).
# Whatever the budget cuts off is recorded in ``image_errors`` — never
# silently dropped — and a time-truncated set never replaces the gallery.
IMAGE_TIME_BUDGET_SECONDS = 45
_IMAGE_TIMEOUT_SECONDS = 10  # per image; a hung CDN must not eat the budget

# Block-level OPEN tags get a newline inserted before them (see html_to_text).
_OPEN_BLOCK = re.compile(r"(?i)<(?=(?:p|div|li|ul|ol|h[1-6]|tr|table|blockquote)\b)")

_EXT_BY_CTYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def html_to_text(value: str | None) -> str:
    """Flatten storefront HTML to plain text — frappe's converter, POS-tuned.

    Delegates to :func:`frappe.core.utils.html_to_plain_text` (BeautifulSoup:
    ``script``/``style`` dropped, ``<br>`` and block tags become newlines,
    inline tags stay inline) with one preface: literal newlines in the source
    are HTML *whitespace*, not breaks, so they are flattened to spaces first.
    That keeps the push↔pull round trip stable — the push side renders each
    plain-text line as ``<br>\\n``, and without the preface that ``\\n``
    doubled every line into a blank line on the first pull.
    """
    flat = (value or "").replace("\r", " ").replace("\n", " ")
    # frappe's converter adds newlines at block CLOSE only; bare text glued to
    # a block OPEN tag ("A</p>B<div>C") would run together, and page-builder
    # markup is div-heavy — break before block opens too, then delegate.
    flat = _OPEN_BLOCK.sub("\n<", flat)
    return html_to_plain_text(flat)


def _file_name(src: str, idx: int, ctype: str) -> str:
    """A filesystem-safe name for a downloaded gallery image."""
    base = (src.split("?", 1)[0].rsplit("/", 1)[-1] or "").strip()
    if not base or "." not in base:
        base = f"woo-image-{idx + 1}{_EXT_BY_CTYPE.get(ctype, '.img')}"
    return base


def _import_images(images: list, doctype: str, name: str) -> tuple[list[str], list[str], bool]:
    """Download WC gallery images and attach them to the POS record.

    Returns ``(file_urls, errors, budget_truncated)`` — order preserved.  A single bad image is
    recorded and skipped, never fatal: the operator asked for "everything Woo
    has", and nine photos plus one error beats zero photos plus one error.
    Only http(s) URLs with an ``image/*`` content type under
    ``MAX_IMAGE_BYTES`` are accepted; the loop stops at ``MAX_IMAGES`` and at
    ``IMAGE_TIME_BUDGET_SECONDS``, and BOTH cutoffs leave an entry in
    ``errors`` so a truncated import can never read as a complete one.
    ``File.insert`` de-duplicates by content hash, so re-pulling the same
    photos re-uses the stored files instead of piling up copies.
    """
    import time

    saved: list[str] = []
    errors: list[str] = []
    budget_truncated = False
    images = images or []
    if len(images) > MAX_IMAGES:
        errors.append(_(
            "{0} more images skipped — only the first {1} are imported."
        ).format(len(images) - MAX_IMAGES, MAX_IMAGES))
    deadline = time.monotonic() + IMAGE_TIME_BUDGET_SECONDS
    for idx, img in enumerate(images[:MAX_IMAGES]):
        src = ((img or {}).get("src") or "").strip()
        if time.monotonic() > deadline:
            budget_truncated = True
            errors.append(_(
                "time budget ({0}s) exhausted — {1} image(s) not imported."
            ).format(IMAGE_TIME_BUDGET_SECONDS, len(images[:MAX_IMAGES]) - idx))
            break
        if not src.lower().startswith(("http://", "https://")):
            errors.append(f"{src or '(blank src)'}: unsupported URL")
            continue
        try:
            resp = requests.get(src, timeout=_IMAGE_TIMEOUT_SECONDS)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            # Raster allowlist, not image/*: image/svg+xml would store a
            # scriptable document as a public file on the POS origin (XSS
            # face) — the extension map doubles as the allowed set.
            if ctype not in _EXT_BY_CTYPE:
                errors.append(f"{src}: unsupported image type ({ctype or 'no content-type'})")
                continue
            content = resp.content
            if len(content) > MAX_IMAGE_BYTES:
                errors.append(f"{src}: larger than {MAX_IMAGE_BYTES // (1024 * 1024)}MB")
                continue
            file_doc = frappe.new_doc("File")
            file_doc.file_name = _file_name(src, idx, ctype)
            file_doc.attached_to_doctype = doctype
            file_doc.attached_to_name = name
            file_doc.is_private = 0
            file_doc.content = content
            file_doc.insert()
            saved.append(file_doc.file_url)
        except Exception as exc:
            errors.append(f"{src}: {str(exc)[:120]}")
    return saved, errors, budget_truncated


def _rebuild_gallery(doc, file_urls: list[str]) -> None:
    """Replace the record's ``image_gallery`` rows with the downloaded set.

    Old rows only lose their REFERENCE — the physical files they pointed at
    are left alone (they may be shared with another record).
    First image becomes primary and also fills the single ``image`` field,
    mirroring how ``mapping._resolve_image_list`` reads them back.
    """
    doc.set("image_gallery", [])
    for idx, url in enumerate(file_urls):
        doc.append("image_gallery", {
            "image": url,
            "is_primary": 1 if idx == 0 else 0,
            "sort_order": idx,
        })
    doc.image = file_urls[0]


def pull_product_from_woo(sku: str, site: str = "retail") -> dict:
    """Overwrite a POS record's listing content from its WooCommerce product.

    ``sku`` is the WooCommerce product SKU: ``item_code`` for an item-level
    product, ``item_code::serial_no`` for a per-serial firearm — resolved by
    the same :func:`webhook._map_sku_to_pos` rule web orders use, so the two
    surfaces can never split a SKU differently.

    Refusals (all ``frappe.throw``): unknown record, SKU/serial parent
    mismatch, missing write permission, product never pushed (no stored
    ``{site}_product_id``), product deleted on the store (dangling id).
    """
    from ffl_core.api.receive_goods import _set_item_selling_price
    from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceError
    from ffl_woo_sync.woocommerce.sync import _f
    from ffl_woo_sync.woocommerce.webhook import _map_sku_to_pos, woo_site

    sku = (sku or "").strip()
    if not sku:
        frappe.throw(_("A product SKU is required."))

    item_code, serial_no = _map_sku_to_pos(sku)
    if serial_no:
        doctype, name = "Serial No", serial_no
    else:
        doctype, name = "Item", item_code

    if not frappe.db.exists(doctype, name):
        frappe.throw(_("{0} {1} does not exist in the POS.").format(_(doctype), name))
    from ffl_woo_sync.woocommerce.mapping import variant_kind
    if doctype == "Item" and variant_kind(name)[0]:
        frappe.throw(_("{0} is part of a variable product on WooCommerce; pulling is for "
            "simple products and per-serial guns.").format(name))
    if doctype == "Serial No":
        parent = frappe.db.get_value("Serial No", name, "item_code") or ""
        if parent != item_code:
            frappe.throw(_(
                "SKU {0} names item {1}, but Serial No {2} belongs to {3}."
            ).format(sku, item_code, name, parent or _("no item")))
    if not frappe.has_permission(doctype, ptype="write"):
        frappe.throw(
            _("You need write permission on {0} to pull from WooCommerce.").format(_(doctype)),
            frappe.PermissionError,
        )

    pid_field = _f(site, "product_id")
    woo_id = frappe.db.get_value(doctype, name, pid_field) or ""
    if not woo_id:
        frappe.throw(_(
            "{0} {1} has no {2} — the product was never pushed to this site, so there is nothing to pull."
        ).format(_(doctype), name, pid_field))

    client = WooCommerceClient.from_settings(site)
    try:
        wc = client.get_product(woo_id)
    except WooCommerceError as exc:
        if exc.status_code == 404:
            # Dangling product_id: the WC product behind the stored id was
            # deleted on the store. Same shape (and same durable fix) as
            # push_item's 404 arm — run woo_audit_dangling_ids to clear it.
            frappe.throw(_(
                "WooCommerce product {0} was deleted on the store — the stored {1} is dangling. "
                "Run woo_audit_dangling_ids to clear it."
            ).format(woo_id, pid_field))
        raise
    if not wc:
        frappe.throw(_(
            "WooCommerce returned no product for id {0} — nothing to pull."
        ).format(woo_id))

    doc = frappe.get_doc(doctype, name)
    updated: dict = {}
    price_note = None

    title = (wc.get("name") or "").strip()
    if title:
        doc.item_name = title
        updated["item_name"] = title

    desc_html = wc.get("description") or ""
    if doctype == "Item":
        # Item.description is already HTML on both sides — store verbatim.
        if desc_html.strip():
            doc.description = desc_html
            updated["description"] = _("{0} characters (HTML)").format(len(desc_html))
    else:
        text = html_to_text(desc_html)
        if text:
            doc.description = text
            updated["description"] = _("{0} characters (plain text)").format(len(text))

    price = flt(wc.get("regular_price") or 0)
    if price > 0 and doctype == "Item" and listing_web_price(name) is not None:
        # A drop-ship listing's regular price is its WEBSITE price (markup rules,
        # MAP floor) — adopting it would overwrite the store's own tag with it.
        price_note = _(
            "price not written: this is a drop-ship listing, whose website price "
            "comes from the Drop-ship Center markup rules, not the store's tag."
        )
    elif price > 0:
        if doctype == "Item":
            # Applied through the canonical helper AFTER the save below: a bare
            # standard_rate write is not what the register charges (an existing
            # selling Item Price wins — item_hooks only hard-syncs uniform
            # items), and the helper's targeted db writes would be clobbered if
            # the full-doc save ran after them with the stale in-memory rate.
            updated["selling_price"] = price
            # In-memory too (harmless next to the helper's own db write): the
            # sale-price validate hook compares the pulled sale against the
            # doc's rate, and the STALE rate would falsely reject an adoption
            # whose sale sits between the old and new regular price.
            doc.standard_rate = price
        elif frappe.db.get_value("Item", item_code, "uniform_sell_price"):
            price_note = _(
                "price not written: the parent Item is uniform-priced, so the serial's "
                "own tag is ignored — set the Item's price instead."
            )
        else:
            doc.sell_price = price
            updated["sell_price"] = price

    # Sale trio — adopted UNCONDITIONALLY (unlike the regular price): 0/empty
    # means "no sale running", so a sale cleared in wp-admin must clear POS
    # too, or the next lean push / reconcile would put yesterday's promo back
    # on the store. Dates keep only their calendar day — POS schedules in
    # whole days, and the push side re-expands them (00:00:00 / 23:59:59).
    sale = flt(wc.get("sale_price") or 0)
    sale_from = (wc.get("date_on_sale_from") or "")[:10] or None
    sale_to = (wc.get("date_on_sale_to") or "")[:10] or None
    sale_uniform_note = None
    if doctype == "Item":
        doc.sale_price = sale
        doc.sale_from = sale_from
        doc.sale_to = sale_to
        updated["sale"] = _("{0} ({1} – {2})").format(
            sale or _("none"), sale_from or _("open"), sale_to or _("open"))
    elif frappe.db.get_value("Item", item_code, "uniform_sell_price"):
        # Same silent-no-op guard as the regular price: under uniform pricing
        # the serial's own sale tag is ignored by firearm.serial_sale_fields.
        if sale > 0:
            sale_uniform_note = _(
                "sale price not written: the parent Item is uniform-priced, so the "
                "serial's own sale tag is ignored — set the sale on the Item instead."
            )
    else:
        doc.sale_sell_price = sale
        doc.sale_from = sale_from
        doc.sale_to = sale_to
        updated["sale"] = _("{0} ({1} – {2})").format(
            sale or _("none"), sale_from or _("open"), sale_to or _("open"))

    file_urls, image_errors, budget_truncated = _import_images(
        wc.get("images") or [], doctype, name)
    if budget_truncated:
        # A time-truncated set is NON-DETERMINISTIC (a retry may stop at a
        # different photo), so swapping it in could leave the gallery stuck on
        # an arbitrary partial forever. Keep the old gallery, say so, retry
        # later. Deterministic outcomes (the >20 cap, per-image failures)
        # still rebuild with what succeeded — a retry converges to the same
        # set, and the errors list already tells the operator what dropped.
        image_errors.append(_(
            "gallery left unchanged — the import was cut off by the time "
            "budget; try again when the store responds faster."
        ))
        file_urls = []
    if file_urls:
        _rebuild_gallery(doc, file_urls)
        updated["images"] = len(file_urls)

    # Canonical echo guard — BOTH inbound flags (loop guard + originating
    # site), save/restore semantics, shared with the order webhook.
    with woo_site(site):
        doc.save()
        if "selling_price" in updated:
            # AFTER the save, inside the guard: the full-doc save would clobber
            # the helper's targeted writes if it ran second, and this is still
            # an inbound-caused POS write, so it belongs under the echo guard
            # (no Item Price doc_events exist today — this is belt/braces).
            _set_item_selling_price(item_code, price)

    result = {
        "doctype": doctype,
        "name": name,
        "updated": updated,
        "images_imported": len(file_urls),
        "image_errors": image_errors,
    }
    if price_note:
        result["price_note"] = price_note
    if sale_uniform_note:
        result["sale_note"] = sale_uniform_note
    return result
