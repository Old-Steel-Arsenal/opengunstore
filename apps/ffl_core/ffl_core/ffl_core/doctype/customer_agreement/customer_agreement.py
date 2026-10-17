"""Customer Agreement — one customer's copy of an Agreement Template.

Written only by ``ffl_core.agreements`` (request, sign, paper copy, void, expiry),
which sets ``flags.via_agreements``; the form is read-only and a copy is never
deleted. Staff may still attach files to it (a paper scan before it is
recorded). Flow and rules: ``ffl_core.agreements``.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CustomerAgreement(Document):
	def validate(self):
		if not self.flags.via_agreements:
			frappe.throw(_("Agreements are requested, signed and voided from the customer, not edited here."),
				frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("An agreement is never deleted. Void it instead."), frappe.PermissionError)
