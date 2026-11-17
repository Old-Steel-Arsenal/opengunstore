"""One-shot: the assignments left Open on CRM deals already Won or Lost, and on leads already
converted or moved to Other mail, are closed the way the CRM hooks close them from now on
(``crm.assignments``): Closed and marked, so a deal reopened later gives them back. Idempotent: a
second run finds no Open assignment on a finished record. A record that fails is logged and left Open.
"""
from __future__ import annotations

import frappe

from ffl_core.crm import TITLE, installed
from ffl_core.crm.assignments import finished_records, set_assignments
from ffl_core.setup import assignment_notice


def execute() -> None:
	if not installed():
		return
	# The marker field is otherwise added at after_migrate, after the patches.
	assignment_notice.ensure()
	# lazy: one query per finished record with an Open assignment (dozens at most), set-based SQL if that grows
	for doctype, name in finished_records():
		frappe.db.savepoint("ffl_close_finished_crm")
		try:
			set_assignments(doctype, name, close=True)
		except Exception:
			frappe.db.rollback(save_point="ffl_close_finished_crm")
			frappe.log_error(title=f"[{TITLE}] Assignments not closed: {doctype} {name}", message=frappe.get_traceback())
