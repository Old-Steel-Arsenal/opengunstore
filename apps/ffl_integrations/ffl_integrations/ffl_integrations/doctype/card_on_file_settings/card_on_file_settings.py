"""Card On File Settings — whether the counter may offer "Save this card", and which signed
authorization saving needs. Edited from Settings → Payments (``payments.hub``); the rules that
use it are ``ffl_integrations.payments.saved_cards``."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CardOnFileSettings(Document):
	def validate(self):
		from ffl_core.agreements import PER_CUSTOMER, TEMPLATE

		if not self.card_on_file_enabled:
			return
		if not self.card_on_file_template:
			frappe.throw(_("Pick the card-on-file authorization customers sign before switching "
				"saving cards on."))
		tpl = frappe.db.get_value(TEMPLATE, self.card_on_file_template,
			["active", "scope"], as_dict=True)
		if not tpl or not tpl.active:
			frappe.throw(_("{0} is switched off.").format(self.card_on_file_template))
		if tpl.scope != PER_CUSTOMER:
			frappe.throw(_("{0} must be signed once per customer (scope Per customer).").format(
				self.card_on_file_template))
