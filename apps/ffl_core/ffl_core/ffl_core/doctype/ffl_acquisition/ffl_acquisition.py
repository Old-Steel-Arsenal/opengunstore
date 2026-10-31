"""FFL Acquisition — A&D 'A' side. Records every firearm coming into our FFL.

Submitted = recorded in the bound book. Amended via cancel + new-with-amended_from.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.acquisition import INDIVIDUAL_SOURCES
from ffl_core.serials import only_active_serial


class FFLAcquisition(Document):
	def validate(self) -> None:
		if not self.serial:
			self.serial = only_active_serial(self.item) or self.serial
		self._reject_dealer_source_without_ffl()
		self._reject_individual_source_without_id()

	def _reject_dealer_source_without_ffl(self) -> None:
		if self.source_type == "FFL Dealer" and not (self.source_ffl or "").strip():
			frappe.throw(
				_("Source FFL number is required when source type is 'FFL Dealer'."),
				title=_("FFL Required"),
			)

	def _reject_individual_source_without_id(self) -> None:
		if self.source_type in INDIVIDUAL_SOURCES and not (
			self.source_id_type and self.source_id_number
		):
			frappe.throw(
				_("ID type and number are required for source type '{0}'.").format(self.source_type),
				title=_("ID Required"),
			)
