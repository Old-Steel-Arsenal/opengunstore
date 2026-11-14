"""One-shot: a CRM Deal the POS opened for a contact carries the person's name, as a deal CRM converts
from a lead does (``crm.deals.person_name``: the primary contact, else the linked Customer, else the
email). Only a deal with no lead_name is touched, and a first or last name it holds is kept; no
hooks run and ``modified`` stays. Idempotent: a named deal is left as it is.
"""
from __future__ import annotations

import frappe

from ffl_core.crm.deals import DEAL, person_name


def execute() -> None:
	if not frappe.db.exists("DocType", DEAL):
		return
	with_customer = frappe.db.has_column(DEAL, "erpnext_customer")
	deals = frappe.get_all(DEAL, filters={"lead_name": ["is", "not set"]},
		fields=["name", "first_name", "last_name", "email", *(["erpnext_customer"] if with_customer else [])])
	if not deals:
		return
	contacts = {}
	for row in frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "parent": ["in", [d.name for d in deals]]},
			fields=["parent", "contact", "is_primary"], order_by="is_primary desc, idx asc"):
		contacts.setdefault(row.parent, row.contact)   # the primary one, else the first
	# lazy: one UPDATE per unnamed deal (a store has hundreds at most), set-based SQL if that grows
	for d in deals:
		if d.first_name or d.last_name:
			name = {"lead_name": " ".join(n for n in (d.first_name, d.last_name) if n)}
		else:
			name = person_name(contacts.get(d.name), d.get("erpnext_customer"), d.email)
		if name["lead_name"]:
			frappe.db.set_value(DEAL, d.name, name, update_modified=False)
