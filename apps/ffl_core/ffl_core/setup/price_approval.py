"""Fields for a manager's approval of a lowered price (ffl_core/price_approval.py).

On POS Invoice, Sales Invoice and Sales Order, beside the invoice discount:

* ``ffl_price_approved_by`` / ``ffl_price_approval_reason`` / ``ffl_price_approved_on`` —
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

FIELDS = [
	{"fieldname": BY, "fieldtype": "Link", "options": "User", "label": "Price Approved By",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": "discount_amount"},
	{"fieldname": REASON, "fieldtype": "Data", "label": "Price Approval Reason",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": BY},
	{"fieldname": ON, "fieldtype": "Datetime", "label": "Price Approved On",
		"read_only": 1, "no_copy": 1, "print_hide": 1, "depends_on": _SHOWN, "insert_after": REASON},
	{"fieldname": TOKEN_FIELD, "fieldtype": "Data", "label": "Price Approval Token", "hidden": 1,
		"no_copy": 1, "print_hide": 1, "report_hide": 1, "insert_after": ON},
]


def ensure() -> None:
	create_custom_fields({dt: [{**f, "module": "FFL Core"} for f in FIELDS] for dt in GATED}, ignore_validate=True)
	for dt in GATED:
		frappe.clear_cache(doctype=dt)
