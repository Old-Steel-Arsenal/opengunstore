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

from ffl_woo_sync.woocommerce import mapping
from ffl_woo_sync.woocommerce.client import WooCommerceClient, assert_store_site
from ffl_woo_sync.woocommerce.sync import _f


def _template_attributes(template: str) -> list[str]:
	if not frappe.db.exists("Item", template):
		frappe.throw(_("Item {0} does not exist.").format(template))
	if not frappe.db.get_value("Item", template, "has_variants"):
		frappe.throw(_("Item {0} is not a template (Has Variants is off).").format(template))
	names = frappe.get_all("Item Variant Attribute",
		filters={"parenttype": "Item", "parent": template}, order_by="idx asc", pluck="attribute")
	if not names:
		frappe.throw(_("Template {0} has no variant attributes.").format(template))
	return names


def _check_variant(code: str, values, template: str, names: list[str]) -> list[str]:
	"""Why *code* cannot become a variant of *template* with *values* (empty: it can)."""
	row = frappe.db.get_value("Item", code, ["has_variants", "variant_of"], as_dict=True)
	if not row:
		return [f"{code}: no such Item"]
	errors = []
	if row.has_variants:
		errors.append(f"{code}: is a template itself")
	if row.variant_of and row.variant_of != template:
		errors.append(f"{code}: already a variant of {row.variant_of}")
	if not isinstance(values, dict) or set(values) != set(names):
		return errors + [f"{code}: needs exactly these attributes: {', '.join(names)}"]
	for name in names:
		value = str(values[name] or "").strip()
		numeric = frappe.db.get_value("Item Attribute", name, "numeric_values")
		if not value:
			errors.append(f"{code}: {name} is blank")
		elif not numeric and not frappe.db.exists("Item Attribute Value",
				{"parent": name, "attribute_value": value}):
			errors.append(f"{code}: {value!r} is not a value of Item Attribute {name}")
	return errors


@frappe.whitelist()
def make_variants(template: str, variants, dry_run=1) -> dict:
	"""Make existing Items variants of a template, keeping their item codes.

	Sets each Item's ``variant_of`` and its attribute values. Nothing is written
	when any Item fails a check (unknown Item, already another template's variant,
	missing or unknown attribute value, two Items with the same values).

	Args:
	    template: The template Item (Has Variants on, with its attributes).
	    variants: ``{item_code: {attribute: value}}`` (a dict or its JSON).
	    dry_run: 1 (default) only checks; 0 writes.

	Returns:
	    {ok, dry_run, template, errors, variants}: ``variants`` is the item codes
	    that are (or would be) variants of the template.
	"""
	frappe.only_for("System Manager")
	if isinstance(variants, str):
		variants = json.loads(variants)
	dry_run = frappe.utils.cint(dry_run)
	names = _template_attributes(template)
	errors = []
	seen: dict[tuple, str] = {}
	existing = {code: {r.attribute: r.attribute_value for r in frappe.get_all(
		"Item Variant Attribute", filters={"parenttype": "Item", "parent": code},
		fields=["attribute", "attribute_value"])}
		for code in mapping.template_variants(template) if code not in variants}
	for code, values in {**existing, **(variants or {})}.items():
		if code in (variants or {}):
			errors += _check_variant(code, values, template, names)
		combo = tuple(str((values or {}).get(n) or "").strip() for n in names)
		if combo in seen:
			errors.append(f"{code}: same attributes as {seen[combo]}")
		seen.setdefault(combo, code)
	out = {"ok": not errors, "dry_run": bool(dry_run), "template": template,
		"errors": errors, "variants": sorted(variants or {})}
	if errors or dry_run:
		return out
	for code, values in variants.items():
		frappe.db.delete("Item Variant Attribute", {"parenttype": "Item", "parent": code})
		for idx, name in enumerate(names, start=1):
			frappe.get_doc({
				"doctype": "Item Variant Attribute", "parenttype": "Item", "parentfield": "attributes",
				"parent": code, "idx": idx, "attribute": name,
				"attribute_value": str(values[name]).strip(), "variant_of": template,
			}).db_insert()
		frappe.db.set_value("Item", code, {"variant_of": template, "has_variants": 0})
		frappe.get_doc("Item", code).add_comment("Comment",
			_("Made a variant of {0} ({1}).").format(template,
				", ".join(f"{n}: {values[n]}" for n in names)))
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
	links = {template: woo_product_id, **matched}
	for code, woo_id in links.items():
		frappe.db.set_value("Item", code, {
			pid: woo_id, _f(site, "listed"): 1, _f(site, "delisted"): 0, _f(site, "sync_error"): "",
		})
	return out
