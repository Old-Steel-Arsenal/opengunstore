"""Card Terminal Payment — one device payment instruction run against a counter
Sales Invoice (Create Order's card tender, Pending Order's Record Payment), on the
card processor named in `processor`. The POS side keeps its own child rows on POS
Invoice (`card_charges`) because they are tied to the register's tender rows; a
counter order is on account and has no tender row to tie to, so its card charges
live here, each with the Payment Entry that booked it. Written only by
ffl_integrations.payments.api — the desk never creates or edits one (in_create +
read-only fields)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CardTerminalPayment(Document):
	def on_trash(self):
		# A charged card is a money fact; the record is the only in-system tie to
		# the gateway reference. Declined/cancelled attempts may go.
		if self.status == "CHARGED":
			frappe.throw(_(
				"{0} records a card charge ({1}) — it cannot be deleted."
			).format(self.name, self.unique_reference or "?"))
