"""Membership — one customer's membership of a plan. Flow and rules: ``ffl_core.membership``.

Written only through ``ffl_core.membership`` (which sets ``flags.via_membership``): the status,
the period and the renewal card follow payments and the panel's buttons, never a form edit.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class Membership(Document):
	def validate(self):
		from ffl_core.membership import LIVE

		if not self.flags.via_membership and not self.is_new():
			before = self.get_doc_before_save()
			watched = ("status", "auto_renew", "saved_card", "current_period_start", "current_period_end",
				"start_date", "grace_until", "cancel_at_period_end")
			if before and any(self.get(f) != before.get(f) for f in watched):
				frappe.throw(_("A membership changes from the customer's Memberships panel."))
		if self.status in LIVE and frappe.db.exists("Membership", {"customer": self.customer, "plan": self.plan,
				"status": ("in", LIVE), "name": ("!=", self.name or "")}):
			frappe.throw(_("{0} already has a membership of {1} that has not ended.").format(
				frappe.bold(self.customer_name or self.customer), frappe.bold(self.plan)), title=_("Already a member"))

	def agreement_context(self) -> dict:
		"""Placeholders for the plan's terms (agreements.document_context)."""
		from ffl_core.membership import agreement_context

		return agreement_context(self)
