"""Frappe CRM endpoints: the CRM Contact page's Email button (``ffl_core.crm.setup.CONTACT_SCRIPT``)
opens the contact's deal to write to them, since CRM shows and sends mail only on a lead or a deal."""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.crm import deals
from ffl_core.roles import CRM_ROLES


@frappe.whitelist(methods=["POST"])
def open_deal(contact: str) -> str:
	"""The contact's open deal, made when they have none, to write to them from CRM.

	Args:
	    contact: The Contact's name.

	Returns:
	    str: The CRM Deal's name (open it at /crm/deals/<name>#emails). Refused for a contact
	    without an email address.
	"""
	frappe.only_for(CRM_ROLES)
	if not isinstance(contact, str) or not frappe.db.exists("Contact", contact):
		frappe.throw(_("This contact no longer exists."), frappe.DoesNotExistError)
	frappe.has_permission("Contact", "read", doc=contact, throw=True)
	if not (frappe.db.get_value("Contact", contact, "email_id")
			or frappe.db.exists("Contact Email", {"parenttype": "Contact", "parent": contact})):
		frappe.throw(_("This contact has no email address. Add one to the contact first."),
			title=_("No email address"))
	return deals.open_deal_for(contact, owner=frappe.session.user)
