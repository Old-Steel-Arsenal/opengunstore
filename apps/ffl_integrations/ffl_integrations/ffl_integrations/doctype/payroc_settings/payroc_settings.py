"""Payroc Settings — single doctype for credentials + endpoint config."""
from __future__ import annotations

from frappe.model.document import Document


class PayrocSettings(Document):
	def validate(self):
		# One card processor takes cards at a time (payments.registry): arming Payroc (switched
		# on WITH a key) while another processor is on would leave the POS not knowing where a
		# swipe goes. Switched on without a key does not count, as in the registry.
		if self.enabled and self.get_password("api_key", raise_exception=False):
			from ffl_integrations.payments.registry import refuse_second_active

			refuse_second_active("payroc")
