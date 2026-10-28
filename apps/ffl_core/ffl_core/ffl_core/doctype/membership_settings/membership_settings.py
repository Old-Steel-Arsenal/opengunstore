"""Membership Settings: the renewal job's switch and the yearly reminder (rules in ffl_core.membership;
the Needs attention list is on the Memberships page)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MembershipSettings(Document):
	def validate(self):
		if cint(self.yearly_reminder_days) < 0:
			frappe.throw(_("The yearly renewal reminder cannot be a negative number of days."))
