"""CRM Deal Hold: one gun held for one CRM deal. Written only by ffl_core.crm.holds (the Deal page,
the counter's manager override and the sweep); the DocType itself is read-only to staff."""
from __future__ import annotations

from frappe.model.document import Document


class CRMDealHold(Document):
	pass
