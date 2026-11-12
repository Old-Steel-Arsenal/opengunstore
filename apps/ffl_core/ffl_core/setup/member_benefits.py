"""Line fields for member prices (rules in ``ffl_core.membership_benefits``).

The same fields on POS Invoice Item and Sales Invoice Item, none ``no_copy``: POS consolidation and
returns carry them (``map_child_doc`` copies fields by name), so a return keeps the benefit of the
line it returns. Fields only, safe on install and on every migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

BENEFIT_FIELD = "ffl_member_benefit"
MEMBERSHIP_FIELD = "ffl_membership"
BENEFICIARY_FIELD = "ffl_benefit_customer"
MEMBER_RATE_FIELD = "ffl_member_rate"
REASON_FIELD = "ffl_discount_reason"
STAMP_FIELDS = (BENEFIT_FIELD, MEMBERSHIP_FIELD, BENEFICIARY_FIELD, MEMBER_RATE_FIELD)

_FIELDS = [
	{"fieldname": REASON_FIELD, "fieldtype": "Data", "label": "Discount Reason", "read_only": 1, "no_copy": 0,
		"insert_after": "discount_amount", "description": "Why this line is below its list price (a member price)."},
	{"fieldname": BENEFIT_FIELD, "fieldtype": "Data", "label": "Member Benefit", "read_only": 1, "hidden": 1,
		"no_copy": 0, "insert_after": REASON_FIELD},
	{"fieldname": MEMBERSHIP_FIELD, "fieldtype": "Link", "options": "Membership", "label": "Membership",
		"read_only": 1, "no_copy": 0, "insert_after": BENEFIT_FIELD},
	{"fieldname": BENEFICIARY_FIELD, "fieldtype": "Link", "options": "Customer", "label": "Member",
		"read_only": 1, "no_copy": 0, "insert_after": MEMBERSHIP_FIELD,
		"description": "Whose membership priced this line."},
	{"fieldname": MEMBER_RATE_FIELD, "fieldtype": "Currency", "options": "currency", "label": "Member Rate",
		"read_only": 1, "hidden": 1, "no_copy": 0, "insert_after": BENEFICIARY_FIELD},
]


def ensure() -> None:
	"""Create the line fields. Idempotent; never raises (install / migrate)."""
	try:
		create_custom_fields({dt: [dict(f) for f in _FIELDS] for dt in ("POS Invoice Item", "Sales Invoice Item")},
			ignore_validate=True)
	except Exception:
		frappe.log_error(title="member_benefits setup failed", message=frappe.get_traceback())
