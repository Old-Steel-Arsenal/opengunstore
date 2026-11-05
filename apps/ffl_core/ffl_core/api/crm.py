"""Frappe CRM endpoints: the CRM Contact page's Email button (``ffl_core.crm.setup.CONTACT_SCRIPT``)
opens the contact's deal to write to them, since CRM shows and sends mail only on a lead or a deal;
**Connect my mailbox** on a staff member's own My Settings (``public/js/user_form.js``) starts the
sign-in that connects their own mailbox; **Make lead** on a Communication (``public/js/communication_form.js``)
files a mail the AI screening left without a lead."""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.crm import deals, mail
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


@frappe.whitelist(methods=["POST"])
def connect_my_mailbox(email_account: str | None = None, success_uri: str | None = None) -> str:
	"""Start connecting your own mailbox to the POS: the address of the provider's sign-in page.

	For a staff member whose mailbox an administrator set up as an OAuth Email Account with them as
	its connected user; they sign in to their own mail account, not the administrator. Frappe's own
	Authorize button needs read access to the Connected App, which staff do not have.

	Args:
	    email_account: The Email Account to connect. Optional when you have only one.
	    success_uri: Where to land after signing in (a path on this site). Default: CRM.

	Returns:
	    str: The sign-in address to open. Refused for an Email Account whose connected user is
	    someone else, or that does not sign in with OAuth.
	"""
	frappe.only_for(CRM_ROLES)
	user = frappe.session.user
	filters = {"auth_method": "OAuth", "connected_user": user}
	if email_account:
		filters["name"] = email_account
	# Staff have no read right on Email Account: looked up for the caller's own mailbox only.
	accounts = frappe.get_all("Email Account", filters=filters, fields=["name", "connected_app"], limit=2)
	if not accounts:
		frappe.throw(_("No mailbox is set up for you to connect. Ask your administrator to add it."),
			frappe.PermissionError)
	if len(accounts) > 1:
		frappe.throw(_("You have more than one mailbox. Pick the one to connect."), title=_("Pick a mailbox"))
	if not accounts[0].connected_app:
		frappe.throw(_("Your mailbox has no sign-in app yet. Ask your administrator to finish setting it up."))
	app = frappe.get_doc("Connected App", accounts[0].connected_app)
	# As the caller themselves: Frappe checks no permission for a user's own flow.
	return app.initiate_web_application_flow(user=user, success_uri=success_uri or "/crm")


@frappe.whitelist(methods=["POST"])
def make_lead_from_mail(communication: str) -> dict:
	"""Make the lead for a mail the AI screening judged not to be from a customer.

	The mail goes by the normal mail rules with the screening skipped: a new lead shared with the team,
	or the sender's deal if they have become a customer.

	Args:
	    communication: The Communication's name.

	Returns:
	    dict: {"doctype": "CRM Lead" or "CRM Deal", "name": its name} (open it at /crm/leads/<name> or
	    /crm/deals/<name>). Refused for a mail that is already linked, was not screened out, or is not
	    received mail.
	"""
	frappe.only_for(CRM_ROLES)
	if not isinstance(communication, str) or not frappe.db.exists("Communication", communication):
		frappe.throw(_("This mail no longer exists."), frappe.DoesNotExistError)
	doctype, name = mail.make_lead(communication)
	return {"doctype": doctype, "name": name}
