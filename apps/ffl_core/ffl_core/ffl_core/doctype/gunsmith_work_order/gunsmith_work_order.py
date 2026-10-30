"""Gunsmith Work Order — a customer's gun left for repair. Flow and rules: ``ffl_core.gunsmith``.

The status moves only through ``ffl_core.gunsmith`` (which sets ``flags.via_work_order``).
A logged gun's details are what the bound book holds, so they stop changing once logged;
a closed job (Picked Up / Cancelled) stops changing at all.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, formatdate

_FIREARM_FIELDS = ("manufacturer", "model", "caliber", "serial_number", "firearm_type")


class GunsmithWorkOrder(Document):
	def validate(self):
		from ffl_core.gunsmith import CLOSED

		before = self.get_doc_before_save()
		via = self.flags.via_work_order
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company") \
				or frappe.db.get_single_value("Global Defaults", "default_company")
		self.serial_number = (self.serial_number or "").strip()
		self._fill_contact()
		from ffl_core.feature_gate import customer_id_numbers_enabled
		if not customer_id_numbers_enabled() and not (self.log_in_bound_book and not self.purchase_receipt):
			# Kept only for the bound-book entry this save makes (log_gun clears it after);
			# the ID scan carries the same number, so it goes too.
			self.id_number = self.id_document = None
		if self.id_document:
			# Government-ID scans are PII: private, as Receive Goods keeps them.
			from ffl_core.api.receive_goods import _ensure_private_file
			self.id_document = _ensure_private_file(self.id_document)
		self._labor_line(before)
		self._no_firearm_charges()
		for row in self.get("lines") or []:
			row.qty = flt(row.qty) or 1
			row.amount = flt(row.qty * flt(row.rate), 2)
		self.final_cost = flt(sum(flt(r.amount) for r in self.get("lines") or []), 2)
		if before is None:
			if not via:
				self.status = "Received"
			return
		if not via:
			if self.status != before.status:
				frappe.throw(_("The status changes with the buttons on the form (Start work, Mark ready, Pick up, "
					"Cancel)."))
			if before.status in CLOSED:
				frappe.throw(_("This work order is {0} and can no longer be changed.").format(_(before.status)))
		if before.purchase_receipt:
			if not self.log_in_bound_book:
				frappe.throw(_("This gun is in the bound book; a logged work order stays logged."))
			changed = [self.meta.get_label(f) for f in _FIREARM_FIELDS if (self.get(f) or "") != (before.get(f) or "")]
			if changed:
				frappe.throw(_("The gun is in the bound book as received, so {0} cannot change here.").format(
					", ".join(changed)))

	def on_update(self):
		if self.log_in_bound_book and not self.purchase_receipt and self.status not in ("Picked Up", "Cancelled"):
			from ffl_core.gunsmith import log_gun
			log_gun(self)

	def on_trash(self):
		if self.purchase_receipt or self.deposit_invoice or self.pickup_invoice or self.intake_agreement:
			frappe.throw(_("A work order with a bound-book entry, an invoice or a signed authorization is kept. "
				"Cancel it instead."), frappe.PermissionError)

	def _labor_line(self, before):
		"""A job starts with the labor charge at the estimate, so a scanned part never leaves
		the labor off the bill; it follows the estimate until someone prices it by hand."""
		from ffl_core.setup.gunsmith import labor_item

		labor = labor_item()
		if before is None:
			# First, whatever was scanned in before the first save.
			if not any(r.item_code == labor for r in self.get("lines") or []) and frappe.db.exists("Item", labor):
				self.append("lines", {"item_code": labor, "qty": 1, "rate": flt(self.estimated_cost)})
				self.lines.insert(0, self.lines.pop())
				for i, r in enumerate(self.lines, 1):
					r.idx = i
			return
		if flt(self.estimated_cost) != flt(before.estimated_cost):
			for row in self.get("lines") or []:
				if row.item_code == labor and flt(row.rate) == flt(before.estimated_cost):
					row.rate = flt(self.estimated_cost)

	def _no_firearm_charges(self):
		"""A charge is labor or a part; a firearm on the bill (a scanned serial tag, often the
		customer's own gun) would sell a gun through the work order."""
		from ffl_core.firearm import is_firearm_item

		guns = sorted({r.item_code for r in self.get("lines") or [] if is_firearm_item(r.item_code)})
		if guns:
			frappe.throw(_("{0} is a firearm, not a part. Remove it from the charges.").format(
				", ".join(guns)), title=_("Charges"))

	def _fill_contact(self):
		"""The paper form's customer block, from the customer, where left blank."""
		if not self.customer:
			return
		cust = frappe.db.get_value("Customer", self.customer, ["mobile_no", "email_id"], as_dict=True) \
			or frappe._dict()
		self.phone = self.phone or cust.mobile_no
		self.email = self.email or cust.email_id
		if not self.address_line1:
			# Split like Receive Goods' seller address; a state the dropdown lacks is left
			# for staff to pick.
			from ffl_core.api.trade_in import customer_address_parts
			from ffl_core.compliance.interstate_tax import US_CODES
			parts = customer_address_parts(self.customer)
			self.address_line1 = parts.get("address_line1") or None
			self.city = self.city or parts.get("city") or None
			self.zip = self.zip or parts.get("zip") or None
			if not self.state and parts.get("state") in US_CODES:
				self.state = parts["state"]

	def agreement_context(self) -> dict:
		"""Placeholders for the intake authorization (agreements.document_context)."""
		cur = frappe.get_cached_value("Company", self.company, "default_currency") if self.company else None
		money = lambda v: fmt_money(flt(v), currency=cur) if flt(v) else ""
		return {
			"work_order": self.name, "manufacturer": self.manufacturer, "model": self.model,
			"caliber": self.caliber, "serial_no": self.serial_number, "firearm_type": self.firearm_type,
			"accessories": self.accessories, "condition": self.condition, "work_requested": self.work_requested,
			# Paid towards the job so far, else what was paid at intake.
			"estimated_cost": money(self.estimated_cost),
			"deposit": money(flt(self.deposit_held) or self.deposit_amount),
			"estimated_completion": formatdate(self.estimated_completion) if self.estimated_completion else "",
		}
