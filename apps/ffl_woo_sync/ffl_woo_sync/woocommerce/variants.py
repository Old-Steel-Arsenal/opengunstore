"""Bringing existing Items and an existing WooCommerce variable product under the
template → variable product sync (``mapping.variant_kind``).

Two one-off steps, both dry-run by default:

* :func:`make_variants` — turn existing Items into variants of a template. ERPNext
  only creates *new* variant Items (``variant_of`` is set-only-once), but an Item
  already listed, ordered or on pre-order must keep its item code: it is the SKU
  the store and every order line carry.
* :func:`adopt_variable_product` — point the template at a variable product built
  by hand in wp-admin and each variant at its variation (matched by SKU = item
  code), so the next push updates those instead of creating new ones.
"""
from __future__ import annotations

import json

import frappe
from frappe import _

from ffl_core import item_options
from ffl_woo_sync.woocommerce import mapping
from ffl_woo_sync.woocommerce.client import WooCommerceClient, assert_store_site
from ffl_woo_sync.woocommerce.sync import _f


def _template_attributes(template: str) -> list[str]:
	return item_options.template_options(template)


def simple_listings(template: str, item_codes) -> dict[str, str]:
	"""{item_code: its simple product id} for Items of *item_codes* that hold a retail product id
	and are not yet variants of *template* — the ids grouping clears."""
	pid = _f("retail", "product_id")
	cleared = {}
	for code in item_codes:
		row = frappe.db.get_value("Item", code, [pid, "variant_of"], as_dict=True) or {}
		if row.get(pid) and row.get("variant_of") != template:
			cleared[code] = str(row[pid])
	return cleared


def _clear(item_codes) -> None:
	for code in item_codes:
		frappe.db.set_value("Item", code, {_f("retail", "product_id"): None, _f("retail", "listed"): 0})


def before_grouping(template: str, item_codes: list[str]) -> None:
	"""``ffl_item_options_grouping``: Items about to become variants of *template* lose their
	simple product's id (see :func:`make_variants`)."""
	_clear(simple_listings(template, item_codes))


def variants_added(template: str, item_codes: list[str]) -> None:
	"""``ffl_item_options_added``: new variants of a template already on the store get their
	variations (``tasks.push_new_variations_task``). A template not on the store: nothing — its
	first push creates them all."""
	row = frappe.db.get_value("Item", template, [_f("retail", "product_id"), _f("retail", "delisted")],
		as_dict=True) or {}
	if not row.get(_f("retail", "product_id")) or row.get(_f("retail", "delisted")):
		return
	frappe.enqueue("ffl_woo_sync.woocommerce.tasks.push_new_variations_task", queue="long",
		enqueue_after_commit=True, job_id=f"woo-new-variations::{template}", deduplicate=True,
		template=template)


@frappe.whitelist()
def make_variants(template: str, variants, dry_run=1) -> dict:
	"""Make existing Items variants of a template, keeping their item codes.

	Sets each Item's ``variant_of`` and its attribute values. Nothing is written
	when any Item fails a check (unknown Item, already another template's variant,
	missing or unknown attribute value, the same values as another requested Item
	or an existing variant).

	An Item that is listed as its own simple product loses that link
	(``{site}_product_id`` / ``listed`` cleared, returned in ``cleared``): as a
	variant its id must be a variation of the template's product, so a stale
	simple-product id would be sent as a variation id. The simple product itself
	is left on the store — draft or trash it there, or link the variant to its
	variation with :func:`adopt_variable_product`.

	Args:
	    template: The template Item (Has Variants on, with its attributes).
	    variants: ``{item_code: {attribute: value}}`` (a dict or its JSON).
	    dry_run: 1 (default) only checks; 0 writes.

	Returns:
	    {ok, dry_run, template, errors, variants, cleared}: ``variants`` is the item
	    codes that are (or would be) variants of the template; ``cleared`` is
	    {item_code: the simple product id it held} for Items that were not yet
	    variants.
	"""
	frappe.only_for("System Manager")
	if isinstance(variants, str):
		variants = json.loads(variants)
	dry_run = frappe.utils.cint(dry_run)
	names = _template_attributes(template)
	variants = variants or {}
	errors = item_options.grouping_errors(template, variants, names)
	cleared = simple_listings(template, variants)
	out = {"ok": not errors, "dry_run": bool(dry_run), "template": template,
		"errors": errors, "variants": sorted(variants), "cleared": cleared}
	if errors or dry_run:
		return out
	_clear(cleared)
	item_options.attach_variants(template, variants, names)
	return out


@frappe.whitelist()
def adopt_variable_product(template: str, woo_product_id, site: str = "retail", dry_run=1) -> dict:
	"""Link a template to a variable product that already exists on the store, and
	each variant to the variation whose SKU is its item code.

	Afterwards, pushing the template updates that product and those variations;
	a variant with no variation there gets a new one, and a variation with no
	variant is left as it is (``woo_only``).

	Args:
	    template: The template Item.
	    woo_product_id: The variable product's id on the store.
	    site: "retail" (default; the only store).
	    dry_run: 1 (default) only reads the store and reports; 0 writes the ids.

	Returns:
	    {ok, dry_run, template, product, matched, pos_only, woo_only, replaced}:
	    ``matched`` is {item_code: variation id}; ``replaced`` the product ids the
	    Items held before.
	"""
	frappe.only_for("System Manager")
	assert_store_site(site)
	dry_run = frappe.utils.cint(dry_run)
	_template_attributes(template)
	woo_product_id = str(woo_product_id or "").strip()
	if not woo_product_id.isdigit():
		frappe.throw(_("A WooCommerce product id is required."))
	wc = WooCommerceClient.from_settings(site)
	product = wc.get_product(woo_product_id) or {}
	if product.get("type") != "variable":
		frappe.throw(_("WooCommerce product {0} is not a variable product ({1}).").format(
			woo_product_id, product.get("type") or _("no type")))
	by_sku = {str(v.get("sku") or "").strip(): str(v["id"])
		for v in wc.list_variations(woo_product_id).values()}
	codes = mapping.template_variants(template)
	matched = {c: by_sku[c] for c in codes if c in by_sku}
	pid = _f(site, "product_id")
	replaced = {code: old for code in [template, *matched]
		if (old := str(frappe.db.get_value("Item", code, pid) or "")) and old != (
			woo_product_id if code == template else matched[code])}
	out = {
		"ok": True, "dry_run": bool(dry_run), "template": template,
		"product": {"id": woo_product_id, "name": product.get("name"), "status": product.get("status")},
		"matched": matched,
		"pos_only": [c for c in codes if c not in by_sku],
		"woo_only": sorted(s or "(no SKU)" for s in by_sku if s not in matched),
		"replaced": replaced,
	}
	if dry_run:
		return out
	if codes and not matched:
		frappe.throw(_("No variation of WooCommerce product {0} has the item code of a variant of "
			"{1} as its SKU: nothing to adopt.").format(woo_product_id, template))
	links = {template: woo_product_id, **matched}
	for code, woo_id in links.items():
		frappe.db.set_value("Item", code, {
			pid: woo_id, _f(site, "listed"): 1, _f(site, "delisted"): 0, _f(site, "sync_error"): "",
		})
	return out
