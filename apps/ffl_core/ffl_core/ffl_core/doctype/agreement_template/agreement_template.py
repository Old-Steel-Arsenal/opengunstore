"""Agreement Template — the text of a waiver or agreement customers sign.

``version`` goes up when the text changes after someone has already been sent or
has signed this template (any Customer Agreement that is not Void): those copies
keep the text they were given (``Customer Agreement.body_snapshot``), and the
version says which text a copy carries. Signed copies are not asked to sign
again — only ``valid_days`` ends them. To make everyone sign a new text, switch
this template off and start a new one. Flow and rules: ``ffl_core.agreements``.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class AgreementTemplate(Document):
	def validate(self):
		self.valid_days = max(0, self.valid_days or 0)
		if self.is_new():
			self.version = 1
			return
		before = self.get_doc_before_save()
		if not before:
			return
		self.version = before.version or 1
		if (before.body or "") != (self.body or "") and frappe.db.exists(
				"Customer Agreement", {"template": self.name, "status": ("!=", "Void")}):
			self.version = (before.version or 1) + 1
