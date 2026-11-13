"""A contact's open CRM Deal: where CRM shows and answers mail (its Contact page has no mail).

A customer's incoming mail (``crm.mail``) and the Email button on a CRM Contact page
(``api.crm.open_deal``) both land on the contact's newest deal that is not won or lost, made when
there is none, so the conversation sits where CRM lets staff read and reply to it.
"""
from __future__ import annotations

import frappe
from frappe.desk.form.assign_to import _add as assign

from ffl_core.assignment_notice import FOLLOW_UP, marked

DEAL = "CRM Deal"
STATUS = "CRM Deal Status"
CLOSED = ("Won", "Lost")


def _statuses(closed: bool) -> list[str]:
	op = "in" if closed else "not in"
	return frappe.get_all(STATUS, filters={"type": [op, list(CLOSED)]}, pluck="name", order_by="position asc")


def is_closed(status: str | None) -> bool:
	return bool(status) and frappe.db.get_value(STATUS, status, "type") in CLOSED


def customer_of(contact: str) -> str | None:
	"""The Customer this Contact is linked to. None when it has several: choosing is the employee's."""
	found = frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": contact,
		"link_doctype": "Customer"}, pluck="link_name", limit=2)
	return found[0] if len(found) == 1 else None


def open_deal_for(contact: str, owner: str | None = None) -> str:
	"""The newest open deal *contact* is on, or a new one with them as its primary contact
	(owned by *owner* when that is an enabled user, linked to their Customer when they have one).
	Returns the deal's name."""
	on = frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": contact}, pluck="parent")
	if on:
		found = frappe.get_all(DEAL, filters={"name": ["in", on], "status": ["not in", _statuses(closed=True) or [""]]},
			pluck="name", order_by="modified desc", limit=1)
		if found:
			return found[0]
	deal = frappe.new_doc(DEAL)
	deal.status = _statuses(closed=False)[0]
	if owner and frappe.db.get_value("User", owner, "enabled"):
		deal.deal_owner = owner
	customer = customer_of(contact)
	if customer and deal.meta.has_field("erpnext_customer"):
		deal.erpnext_customer = customer
	deal.append("contacts", {"contact": contact, "is_primary": 1})
	# CRM's DocType, made for a mail the system received or for the user who asked (their own
	# read right on the Contact is checked by the endpoint); CRM's own lead-from-mail inserts so too.
	deal.insert(ignore_permissions=True)
	return deal.name


def new_task(title: str, description: str, assignee: str | None = None, deal: str | None = None, **fields) -> str:
	"""A CRM Task (Todo unless *fields* say otherwise), on *deal* when given, assigned to *assignee* when
	given. Returns its name. For a mail, a web order or a stock arrival the system reacts to: the caller
	need hold no CRM rights."""
	if deal:
		fields.update(reference_doctype=DEAL, reference_docname=deal)
	task = frappe.get_doc({"doctype": "CRM Task", "title": title, "description": description,
		"status": "Todo", **fields}).insert(ignore_permissions=True)
	# Not by assigned_to on the insert: CRM's after_insert checks the session user's rights on the
	# task, and a Stock Manager cancelling an order has none.
	if assignee:
		# Marked a follow-up: assignment_notice.FOLLOW_UPS_ASK decides whether it pops.
		with marked(FOLLOW_UP):
			assign({"assign_to": [assignee], "doctype": "CRM Task", "name": task.name, "description": title},
				ignore_permissions=True)
	return task.name
