"""CRM Customer Memory: what a customer has told the store. Written by ffl_core.crm.memory; Sales Users
reach it only through the core's endpoints, which check their rights on the lead or deal."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class CRMCustomerMemory(Document):
	def validate(self):
		"""No ID numbers, eligibility details, prices or stock in any row. A staff save is refused; the
		core's own write (an AI answer, a merge) drops an unlocked row that fails instead."""
		from ffl_core.crm.sensitive import has_price_or_stock, has_sensitive

		dropped = 0
		for row in list(self.get("items") or []):
			if not any(has_sensitive(v) or has_price_or_stock(v) for v in (row.text, row.keywords)):
				continue
			if self.flags.get("ffl_written_by_core") and not cint(row.locked):
				self.remove(row)
				dropped += 1
				continue
			frappe.throw(_("Leave out ID numbers, eligibility details, prices and stock."), title=_("Not saved"))
		self.flags.ffl_rows_dropped = dropped
