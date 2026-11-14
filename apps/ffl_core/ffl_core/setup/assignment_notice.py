"""The three ToDo fields of the assignment notice (ffl_core/assignment_notice.py).

* ``ffl_ack_required`` — hidden: stamped at insert when the assignment asks its person to say
  they saw it.
* ``ffl_acknowledged_on`` — "Seen On", read-only: when they did; shown on the ToDo to whoever
  assigned it.
* ``ffl_reminded_on`` — hidden: the last day (site time zone) the daily reminder of a seen, still
  Open assignment was dismissed.

All three written only by the server (the ToDo hook and the acknowledge endpoint) and ``no_copy``.
Idempotent; after_install and after_migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.assignment_notice import REMINDED, REQUIRED, SEEN

FIELDS = [
	{"fieldname": REQUIRED, "fieldtype": "Check", "label": "Acknowledgement Required", "hidden": 1,
		"read_only": 1, "no_copy": 1, "print_hide": 1, "report_hide": 1, "insert_after": "assigned_by_full_name"},
	{"fieldname": SEEN, "fieldtype": "Datetime", "label": "Seen On", "read_only": 1, "no_copy": 1,
		"print_hide": 1, "insert_after": REQUIRED},
	{"fieldname": REMINDED, "fieldtype": "Date", "label": "Reminded On", "hidden": 1, "read_only": 1,
		"no_copy": 1, "print_hide": 1, "report_hide": 1, "insert_after": SEEN},
]


def ensure() -> None:
	create_custom_fields({"ToDo": [{**f, "module": "FFL Core"} for f in FIELDS]}, ignore_validate=True)
	frappe.clear_cache(doctype="ToDo")
