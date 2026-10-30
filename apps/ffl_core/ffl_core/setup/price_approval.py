"""Fields for a manager's approval of a lowered price (ffl_core/price_approval.py).

On POS Invoice, Sales Invoice and Sales Order, in their own collapsible "Price Approval"
section right after the invoice discount section (not inside it: Sales Invoice and Sales Order
hide that section when additional discounts are off, which hid the record with it):

* ``ffl_price_approved_by`` / ``ffl_price_approved_on`` | ``ffl_price_approval_reason`` —
  read-only, shown once a price on the document was approved; written only by the validate gate
  (``price_approval.guard_approval_fields`` puts back whatever a save sends).
* ``ffl_price_approval_token`` — hidden: the screen carries a manager's approval token in on the
  save; the gate empties it, so nothing is stored.

All ``no_copy``: a duplicate, an amendment or a return is a new sale and is judged on its own.
Idempotent; after_install and after_migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.price_approval import BY, GATED, ON, REASON, TOKEN_FIELD

_SHOWN = f"eval:doc.{BY}"
SECTION, COLUMN = "ffl_price_approval_section", "ffl_price_approval_column"
# The last field of each doctype's discount section, so the new section starts after it.
ANCHOR = {
	"Sales Invoice": "additional_discount_account",
	"Sales Order": "discount_amount",
	"POS Invoice": "discount_amount",
}

FIELDS = [
	{"fieldname": SECTION, "fieldtype": "Section Break", "label": "Price Approval",
		"collapsible": 1, "depends_on": _SHOWN},
	{"fieldname": BY, "fieldtype": "Link", "options": "User", "label": "Price Approved By",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": SECTION},
	{"fieldname": ON, "fieldtype": "Datetime", "label": "Price Approved On",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": BY},
	{"fieldname": COLUMN, "fieldtype": "Column Break", "insert_after": ON},
	{"fieldname": REASON, "fieldtype": "Data", "label": "Price Approval Reason",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": COLUMN},
	{"fieldname": TOKEN_FIELD, "fieldtype": "Data", "label": "Price Approval Token", "hidden": 1,
		"no_copy": 1, "print_hide": 1, "report_hide": 1, "insert_after": REASON},
]


def fields_for(doctype: str) -> list[dict]:
	return [{**f, "module": "FFL Core", "insert_after": f.get("insert_after") or ANCHOR[doctype]}
		for f in FIELDS]


def ensure() -> None:
	create_custom_fields({dt: fields_for(dt) for dt in GATED}, ignore_validate=True)
	for dt in GATED:
		frappe.clear_cache(doctype=dt)
