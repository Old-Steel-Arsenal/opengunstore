"""Drop ``field_order`` Property Setters so ``insert_after`` rules the layout.

Saving Item in Customize Form (2026-06-25 on prod) stored a ``field_order``
Property Setter that pins every field then present to its position at that
moment. Meta.sort_fields honours it ahead of ``insert_after``, so the Item form
reorganisation in ffl_core's custom_field fixtures only moved fields the
setter had never seen (Channels tab rendered empty and was hidden; Used /
Quantity stayed ahead of Firearm). Deleting the setter hands ordering back to
the fixtures. The same holds for every core form whose layout our custom
fields arrange by ``insert_after`` (``DOCTYPES``). Idempotent; wired into
after_migrate (not patches.txt, which runs once) so a later Customize Form save
cannot silently re-pin the layout past the next deploy.
"""
from __future__ import annotations

import frappe

# Core forms our custom_field fixtures lay out by insert_after.
DOCTYPES = (
	"Item", "Customer", "Supplier", "Serial No", "Purchase Receipt",
	"Sales Invoice", "POS Invoice", "Sales Order",
)


def ensure():
	for name, doctype in frappe.db.get_all(
			"Property Setter",
			filters={"doc_type": ("in", DOCTYPES), "property": "field_order"},
			fields=["name", "doc_type"], as_list=True):
		frappe.delete_doc("Property Setter", name, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype=doctype)
		print(f"item_layout.ensure: removed {doctype} field_order setter {name}")
