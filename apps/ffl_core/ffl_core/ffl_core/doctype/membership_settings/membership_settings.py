"""Membership Settings: the renewal job's switch, the yearly reminder, and the Needs attention list
(rendered by the form; rules in ffl_core.membership)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MembershipSettings(Document):
	def validate(self):
		if cint(self.yearly_reminder_days) < 0:
			frappe.throw(_("The yearly renewal reminder cannot be a negative number of days."))
