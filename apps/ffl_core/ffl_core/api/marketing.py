"""Marketing Contact buttons on the Desk form: Unsubscribe and Resubscribe.

Both go through ``marketing.contacts.set_status``, the one function that changes a contact's status;
the form's status field is read-only. A contact that is not on file is refused, never made.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html

from ffl_core.marketing import CONTACT
from ffl_core.marketing import contacts
from ffl_core.roles import MARKETING_MANAGER_ROLES


def _existing(contact) -> str:
	name = contacts.normalize_email(contact)
	if not name or not frappe.db.exists(CONTACT, name):
		frappe.throw(_("No marketing contact {0}.").format(escape_html(contacts.clip(contact))), frappe.DoesNotExistError)
	return name


@frappe.whitelist(methods=["POST"])
def unsubscribe(contact: str, reason: str | None = None) -> dict:
	"""Stop marketing email to a contact.

	Its status becomes Unsubscribed, with who did it and the reason on the log. A contact already
	Unsubscribed, Bounced or Complained is left as it is.

	Args:
	    contact: The Marketing Contact (its email address).
	    reason: Why, in a few words. Optional.

	Returns:
	    {"contact": the address, "status": the status now, "changed": False when it was already
	    suppressed}.
	"""
	frappe.only_for(MARKETING_MANAGER_ROLES)
	name = _existing(contact)
	changed = contacts.set_status(name, contacts.UNSUBSCRIBED, reason=reason,
		detail=contacts.event_detail(reason=reason, by=frappe.session.user, via="desk"))
	return {"contact": name, "status": frappe.db.get_value(CONTACT, name, "status"), "changed": changed}


@frappe.whitelist(methods=["POST"])
def resubscribe(contact: str, reason: str) -> dict:
	"""Put a suppressed contact back to Subscribed.

	For a contact that unsubscribed, bounced or complained. The reason is required and goes on the log
	with who did it.

	Args:
	    contact: The Marketing Contact (its email address).
	    reason: Why this contact may be emailed again.

	Returns:
	    {"contact": the address, "status": "Subscribed"}. Refused for a contact that is not suppressed.
	"""
	frappe.only_for(MARKETING_MANAGER_ROLES)
	name = _existing(contact)
	if not contacts.is_suppressed(frappe.db.get_value(CONTACT, name, "status")):
		frappe.throw(_("Only a contact that unsubscribed, bounced or complained can be resubscribed."))
	contacts.set_status(name, contacts.SUBSCRIBED, reason=reason, manual=True, channel="manual")
	return {"contact": name, "status": frappe.db.get_value(CONTACT, name, "status")}
