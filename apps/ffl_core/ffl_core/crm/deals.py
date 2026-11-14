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


def _split_name(full_name: str | None) -> tuple[str, str]:
	"""A display name as (first name, last name): the first word, then the rest."""
	parts = (full_name or "").split()
	return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def person_name(contact: str | None, customer: str | None = None, email: str | None = None,
		organization: str | None = None) -> dict:
	"""Who a deal is for, in the fields CRM's lead conversion fills (first_name, last_name, lead_name):
	the Contact's own name, else the Customer's, else the organization's (lead_name only), else the
	local part of *email*. lead_name is the deal's title (setup.DEAL_PROPERTIES)."""
	first, last = frappe.db.get_value("Contact", contact, ["first_name", "last_name"]) if contact else (None, None)
	if not first or "@" in first:   # an address standing in for a name is no name
		first, last = _split_name(customer and frappe.db.get_value("Customer", customer, "customer_name"))
	if not first and organization:
		return {"first_name": "", "last_name": "", "lead_name": organization}
	if not first and email:
		first, last = email.split("@", 1)[0], ""
	last = last or ""
	return {"first_name": first or "", "last_name": last, "lead_name": " ".join(n for n in (first, last) if n)}


def name_deal(doc, method=None) -> None:
	"""CRM Deal validate: a deal with no lead_name (its title) gets one, so no deal shows as its number:
	from the first and last name it holds, else by :func:`person_name` from its primary contact, linked
	Customer, organization and email. A lead_name that is set is never changed."""
	if doc.get("lead_name"):
		return
	if doc.get("first_name") or doc.get("last_name"):
		doc.lead_name = " ".join(n for n in (doc.first_name, doc.last_name) if n)
		return
	rows = doc.get("contacts") or []
	contact = next((r.contact for r in rows if r.is_primary), None) or (rows[0].contact if rows else None)
	doc.update(person_name(contact, doc.get("erpnext_customer"), doc.get("email"), doc.get("organization")))


def open_deal_for(contact: str, owner: str | None = None, title: str | None = None, new: bool = False) -> str:
	"""The newest open deal *contact* is on, or a new one with them as its primary contact
	(owned by *owner* when that is an enabled user, linked to their Customer when they have one),
	named after them, or titled *title* when given. *new*: always a new one. Returns the deal's name."""
	on = [] if new else frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": contact}, pluck="parent")
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
	# Named as a deal CRM converts from a lead is, so the person shows on it and searches find it.
	deal.update(person_name(contact, customer, frappe.db.get_value("Contact", contact, "email_id")))
	if title:
		deal.lead_name = title
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
