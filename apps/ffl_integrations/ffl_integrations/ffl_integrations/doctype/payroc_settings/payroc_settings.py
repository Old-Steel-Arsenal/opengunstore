"""Payroc Settings — single doctype for credentials + endpoint config."""
from __future__ import annotations

from frappe.model.document import Document


class PayrocSettings(Document):
	def validate(self):
		# One card processor takes cards at a time (payments.registry): switching Payroc on
		# while another processor is on would leave the POS not knowing where a swipe goes.
		if self.enabled and self.has_value_changed("enabled"):
			from ffl_integrations.payments.registry import refuse_second_active

			refuse_second_active("payroc")
