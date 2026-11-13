"""Card Reader (child of a card processor's settings Single, ``card_readers``). One row per card
device the store charges on; each computer remembers its own (payments.card_readers). An empty
table means the processor's one configured device, as before."""
from __future__ import annotations

from frappe.model.document import Document


class CardReader(Document):
	pass
