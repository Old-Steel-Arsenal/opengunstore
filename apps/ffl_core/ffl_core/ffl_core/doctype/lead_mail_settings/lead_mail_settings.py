"""Lead Mail Settings: which incoming staff mail never becomes a CRM lead (rules in ffl_core.crm.mail)."""
from __future__ import annotations

from frappe.model.document import Document


class LeadMailSettings(Document):
	def validate(self):
		from ffl_core.crm.mail import domain_lines

		# One clean domain per line (lowercase, no "@", no blanks or repeats).
		for field in ("blocked_sender_domains", "own_domains"):
			self.set(field, "\n".join(domain_lines(self.get(field))))
