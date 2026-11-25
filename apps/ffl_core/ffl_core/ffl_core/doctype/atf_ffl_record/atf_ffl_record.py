"""ATF FFL Record — local cache of the ATF FFL list (CSV bulk + live EZ Check upserts)."""
from __future__ import annotations

from frappe.model.document import Document


class ATFFFLRecord(Document):
	def on_update(self):
		if self.has_value_changed("cfd_number"):
			from ffl_core.atf_link import push_cfd_number

			push_cfd_number(self.name, (self.cfd_number or "").strip() or None)
