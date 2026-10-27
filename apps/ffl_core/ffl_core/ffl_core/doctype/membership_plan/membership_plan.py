"""Membership Plan — what a membership costs and how it bills. Rules: ``ffl_core.membership``."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, flt


class MembershipPlan(Document):
	def validate(self):
		if flt(self.price) <= 0:
			frappe.throw(_("Set the price per period."))
		if flt(self.registration_fee) < 0:
			frappe.throw(_("The registration fee cannot be negative."))
		if flt(self.registration_fee) > 0 and not self.registration_item:
			frappe.throw(_("Pick the item the registration fee is billed as."))
		if self.registration_item and self.registration_item == self.item:
			frappe.throw(_("The registration fee needs its own item, not the plan's billed item."))
		for code in filter(None, (self.item, self.registration_item)):
			self._check_item(code)
		self._check_not_another_plans_item()
		if cint(self.grace_enabled) and cint(self.grace_days) <= 0:
			frappe.throw(_("Set how many grace days a failed renewal gets (more than 0), or switch the grace "
				"period off."))
		if not cint(self.grace_enabled):
			self.grace_days = 0
		if self.terms_template:
			tpl = frappe.db.get_value("Agreement Template", self.terms_template, ["scope", "active"], as_dict=True)
			if not tpl or tpl.scope != "Per document":
				frappe.throw(_("{0} must be a Per document agreement: it is signed for each membership.").format(
					escape_html(self.terms_template)))
		before = self.get_doc_before_save()
		if before and (before.item != self.item or before.billing_period != self.billing_period) \
				and frappe.db.exists("Membership", {"plan": self.name}):
			frappe.throw(_("This plan already has members: its billed item and billing period stay. Make a new "
				"plan instead."))

	def _check_item(self, code: str) -> None:
		row = frappe.db.get_value("Item", code, ["is_stock_item", "has_serial_no", "is_firearm", "disabled"],
			as_dict=True)
		if not row:
			frappe.throw(_("Item {0} not found.").format(escape_html(code)))
		if cint(row.is_stock_item) or cint(row.has_serial_no) or cint(row.is_firearm):
			frappe.throw(_("{0} must be a non-stock service item.").format(escape_html(code)))
		if cint(row.disabled):
			frappe.throw(_("{0} is disabled.").format(escape_html(code)))

	def _check_not_another_plans_item(self) -> None:
		for other in frappe.get_all("Membership Plan", filters={"name": ("!=", self.name or "")},
				fields=["name", "item", "registration_item"]):
			if self.item in (other.item, other.registration_item):
				frappe.throw(_("{0} is already billed by plan {1}: each plan needs its own item.").format(
					escape_html(self.item), escape_html(other.name)))
			if self.registration_item and self.registration_item == other.item:
				frappe.throw(_("{0} is plan {1}'s billed item and cannot be a registration item.").format(
					escape_html(self.registration_item), escape_html(other.name)))

	def on_update(self):
		from ffl_core.membership import sync_plan_item

		sync_plan_item(self)
		if cint(self.active) and not frappe.db.exists("Membership Plan", {"active": 1, "name": ("!=", self.name)}):
			from ffl_core.setup.membership import apply_deferred_settings

			changed = apply_deferred_settings()
			if changed:
				frappe.msgprint("<br>".join([_("Memberships defer their revenue day by day, so Accounts Settings "
					"were changed:")] + [escape_html(c) for c in changed]), title=_("Accounts Settings"),
					indicator="blue")
