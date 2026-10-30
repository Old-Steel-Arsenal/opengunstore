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
			# Kept only for the bound-book entry this save makes (log_gun clears it after).
			self.id_number = None
		if self.id_document:
			# Government-ID scans are PII: private, as Receive Goods keeps them.
			from ffl_core.api.receive_goods import _ensure_private_file
			self.id_document = _ensure_private_file(self.id_document)
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

	def _fill_contact(self):
		"""The paper form's customer block, from the customer, where left blank."""
		if not self.customer:
			return
		cust = frappe.db.get_value("Customer", self.customer, ["mobile_no", "email_id"], as_dict=True) \
			or frappe._dict()
		self.phone = self.phone or cust.mobile_no
		self.email = self.email or cust.email_id
		if not self.address_line1:
			# Split like Receive Goods' seller address; a state the dropdown lacks
			# (a spelled-out "Texas") is left for staff to pick.
			from ffl_core.api.trade_in import _STATE_RE, customer_address_parts
			parts = customer_address_parts(self.customer)
			self.address_line1 = parts.get("address_line1") or None
			self.city = self.city or parts.get("city") or None
			self.zip = self.zip or parts.get("zip") or None
			state = (parts.get("state") or "").upper()
			if not self.state and _STATE_RE.match(state):
				self.state = state

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
