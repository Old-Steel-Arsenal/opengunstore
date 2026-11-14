"""One-shot: every CRM Deal with no lead_name (now the deal's title) gets one by the rule a deal gets
it on save (``crm.deals.name_deal``): its own first and last name, else its primary contact, linked
Customer, organization or email. A name that is set is kept; no hooks run and ``modified`` stays.
Idempotent: a named deal is left as it is.
"""
from __future__ import annotations

import frappe

from ffl_core.crm.deals import DEAL, name_deal

FIELDS = ("first_name", "last_name", "lead_name")


def execute() -> None:
	if not frappe.db.exists("DocType", DEAL):
		return
	# lazy: one load and one UPDATE per unnamed deal (a store has hundreds at most), set-based SQL if that grows
	for name in frappe.get_all(DEAL, filters={"lead_name": ["is", "not set"]}, pluck="name"):
		doc = frappe.get_doc(DEAL, name)
		name_deal(doc)
		if doc.lead_name:
			frappe.db.set_value(DEAL, name, {f: doc.get(f) or "" for f in FIELDS}, update_modified=False)
