"""Membership Plan — what a membership costs and how it bills. Rules: ``ffl_core.membership``."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, escape_html, flt


def _make_item(name: str) -> str:
	"""A non-stock service item named *name* (a free code: "name", then "name 2", ...); saving the
	plan then points it at the membership accounts and sets its price (membership.sync_plan_item).
	Its tax follows the store's default, like any other service item."""
	from ffl_core.setup.tax import SERVICE_GROUP

	if not frappe.db.exists("Item Group", SERVICE_GROUP):
		from ffl_core.setup.item_groups import ensure as ensure_item_groups
		ensure_item_groups()
	code, n = name.strip()[:130], 1
	while frappe.db.exists("Item", code):
		n += 1
		code = f"{name.strip()[:126]} {n}"
	# The Item's own name, not *code*: a store naming items by series gets a series name.
	return frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": SERVICE_GROUP,
		"stock_uom": "Nos", "is_stock_item": 0, "has_serial_no": 0, "is_sales_item": 1, "is_purchase_item": 0,
		"include_item_in_manufacturing": 0, "description": code}).insert(ignore_permissions=True).name


class MembershipPlan(Document):
	def validate(self):
		if flt(self.price) <= 0:
			frappe.throw(_("Set the price per period."))
		if flt(self.registration_fee) < 0:
			frappe.throw(_("The registration fee cannot be negative."))
		# A store does not make the billed items by hand: an empty one is made here.
		if not self.item:
			self.item = _make_item(self.plan_name)
		if flt(self.registration_fee) > 0 and not self.registration_item:
			self.registration_item = _make_item(_("{0} registration fee").format(self.plan_name))
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
		before = self.get_doc_before_save()
		turned_on = cint(self.active) and not (before and cint(before.active))
		if turned_on and not frappe.db.exists("Membership Plan", {"active": 1, "name": ("!=", self.name)}):
			from ffl_core.setup.membership import apply_deferred_settings

			changed = apply_deferred_settings()
			if changed:
				frappe.msgprint("<br>".join([_("Memberships defer their revenue day by day, so Accounts Settings "
					"were changed:")] + [escape_html(c) for c in changed]), title=_("Accounts Settings"),
					indicator="blue")
