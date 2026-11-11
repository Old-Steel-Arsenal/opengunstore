"""CRM Want: what a customer wants that the store does not have. Matching and the arrival task: ffl_core.crm.wants."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CRMWant(Document):
	def validate(self):
		from ffl_core.crm.deals import customer_of
		from ffl_core.crm.wants import keyword_tokens

		self.keywords = (self.keywords or "").strip()
		if not self.item and not keyword_tokens(self.keywords):
			frappe.throw(_("Fill in keywords with at least one letter or digit, an item, or both."), title=_("What do they want?"))
		self.want_owner = self.want_owner or frappe.session.user
		if not self.customer:
			self.customer = customer_of(self.contact)
