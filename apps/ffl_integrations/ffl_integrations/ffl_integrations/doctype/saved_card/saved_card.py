"""Saved Card — a customer's card kept at the card processor's gateway (card on file).

Created only by ``ffl_integrations.payments.saved_cards`` when a terminal sale the cashier
marked "Save this card" is approved and the gateway hands back a token; the desk never creates
or edits one (in_create, read-only fields, no write permission). ``token`` is a Password field:
encrypted at rest, read only by the server when it charges the card, never sent to a browser.
A card is removed (``remove_saved_card``: the gateway forgets it, the record turns Removed and
keeps its history), never deleted while Active."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class SavedCard(Document):
	def on_trash(self):
		# Deleting an Active card would leave the token at the gateway with nothing here
		# that can remove it.
		if self.status == "Active":
			frappe.throw(_("{0} is an active saved card — remove it first, so the card processor "
				"forgets it too.").format(self.card_summary or self.name))
