"""Marketing Contact: someone the store may email, keyed by lowercase email. Status rules and the only
way to change the status: ffl_core.marketing.contacts.set_status."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class MarketingContact(Document):
	def autoname(self):
		from ffl_core.marketing.contacts import normalize_email

		self.email = normalize_email(self.email) or self.email
		self.name = self.email

	def validate(self):
		from ffl_core.marketing.contacts import NOT_SUBSCRIBED, normalize_email

		address = normalize_email(self.email)
		if not address:
			frappe.throw(_("Not a valid email address: {0}").format(frappe.utils.escape_html(self.email or "")),
				frappe.InvalidEmailAddressError)
		self.email = address
		if not self.is_new() and self.email != self.name:
			frappe.throw(_("A marketing contact's email cannot change; add the new address as its own contact."))
		before = self.get_doc_before_save()
		old = (before.status if before else None) or NOT_SUBSCRIBED
		if (self.status or NOT_SUBSCRIBED) != old and not self.flags.via_set_status:
			frappe.throw(_("Change a marketing contact's status with Unsubscribe or Resubscribe, not by editing it."),
				title=_("Status not changed"))
		if before and before.source and self.source != before.source:
			self.source = before.source   # the first source is kept
