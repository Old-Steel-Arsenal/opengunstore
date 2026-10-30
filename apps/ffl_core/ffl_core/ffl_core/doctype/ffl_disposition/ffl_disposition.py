"""FFL Disposition — A&D 'D' side. Records every firearm leaving our FFL.

Triggered automatically when a Sales Invoice with firearm lines is submitted
(ffl_core.compliance.firearm hooks insert one disposition per firearm line).
Triggered manually for FFL-to-FFL transfers / scrap / return.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.compliance.mode import paper_4473_mode
from ffl_core.roles import COUNTER_4473_ROLES
from ffl_core.serials import only_active_serial


# Disposition types that exist in FastBound's DisposeType SmartList (and may be
# synced into the dropdown) but should NEVER be created manually in our system.
# Sale       — duplicate of Retail Sale (which itself is webhook-only).
# Donation / Private Party Transfer — out of scope; if needed, handle directly
#   in FastBound, not in this system.
_MANUAL_FORBIDDEN_DISPOSITIONS = frozenset({
	"Sale", "Donation", "Private Party Transfer",
})


class FFLDisposition(Document):
	def validate(self) -> None:
		if not self.serial:
			self.serial = only_active_serial(self.item) or self.serial
		self._validate_manual_override()
		self._reject_manual_retail_sale()
		self._reject_unsupported_disposition_types()
		self._enforce_compliance_for_retail_sale()
		self._reject_ffl_transfer_without_destination()
		self._require_witnesses_for_destruction()
		self._require_serial_belongs_to_item()

	def _require_serial_belongs_to_item(self) -> None:
		"""serial is fieldtype=Autocomplete (Data + suggestions) — Frappe does
		NOT validate that the saved value is in the suggestion list, so typos
		(e.g. 'SN-0O1' instead of 'SN-001') would silently pass and 404 at the
		FastBound push. Verify on submit that the serial exists in the Serial
		No table AND belongs to the selected Item. Drafts stay open so the
		operator can stage and pick the serial last."""
		if self.docstatus != 1:
			return
		serial = (self.serial or "").strip()
		if not serial:
			frappe.throw(
				_("Serial number is required to submit a disposition."),
				title=_("Serial Required"),
			)
		sn_item = frappe.db.get_value("Serial No", serial, "item_code")
		if not sn_item:
			frappe.throw(
				_("Serial No {0} does not exist. Pick from the in-stock list "
					"or receive the firearm first.").format(serial),
				title=_("Unknown Serial"),
			)
		if self.item and sn_item != self.item:
			frappe.throw(
				_("Serial No {0} belongs to Item {1}, not {2}.").format(
					serial, sn_item, self.item),
				title=_("Serial / Item Mismatch"),
			)

	def _reject_unsupported_disposition_types(self) -> None:
		"""Block manual creation of disposition types that should be handled
		elsewhere (Sale collapsed into Retail Sale; Donation / Private Party
		Transfer handled directly in FastBound). Webhook-imported docs carry
		fastbound_id and are allowed through."""
		if self.disposition_type not in _MANUAL_FORBIDDEN_DISPOSITIONS:
			return
		if (self.fastbound_id or "").strip():
			return  # came in from FastBound — leave it alone
		frappe.throw(
			_("Disposition type {0} is not supported in this system. "
				"If you need this kind of transfer, handle it directly in FastBound.")
				.format(self.disposition_type),
			title=_("Disposition Type Not Supported"),
		)

	def _reject_manual_retail_sale(self) -> None:
		"""Retail Sale dispositions are owned by FastBound's 4473 flow — the
		webhook delivers them with `fastbound_id` already set. A Retail Sale
		without a fastbound_id therefore HAS to be a manual UI / REST creation
		that bypasses 4473 + NICS gating; reject it so no rogue 4473-bypass
		record can land in the bound book.

		The sanctioned exception is a hand-asserted 4473 (manual_override=1) — a
		4473 that demonstrably happened somewhere this system cannot see:

		  * a manager override, when FastBound is down or its disposition was
		    deleted and the sale must still complete;
		  * a paper 4473 recorded at the counter, in a store that keeps no
		    electronic book at all (FFL Settings → Paper 4473 Mode).

		Both are audited — who and why, enforced in _validate_manual_override — so
		they are allowed through without a fastbound_id. The marking is for the
		humans reading the book afterwards, NOT a second gate:
		covered_serials_for_invoice filters on type/status/docstatus and never reads
		manual_override, so an asserted disposition satisfies the submit gate exactly
		like a FastBound-committed one — which is precisely how both paths unblock a
		sale."""
		if self.disposition_type != "Retail Sale":
			return
		if (self.fastbound_id or "").strip():
			return  # came in from FastBound webhook
		if self.get("manual_override"):
			return  # manager force-complete — audited via _validate_manual_override
		frappe.throw(
			_("Retail Sale dispositions are created by FastBound after 4473 + "
				"NICS completion — do not create them manually."),
			title=_("4473 Handled by FastBound"),
		)

	def _validate_manual_override(self) -> None:
		"""Guard the manager-override path: a Retail Sale booked without a
		FastBound disposition (because FastBound was unavailable / its record was
		deleted) must carry a reason and be minted by a manager.

		Defense in depth — the whitelisted POS method role-gates the action too,
		but this stops a direct REST / script insert from minting a 4473 bypass
		without authority or an audit reason."""
		if not self.get("manual_override"):
			return
		if self.disposition_type != "Retail Sale":
			frappe.throw(
				_("Manual override applies only to a Retail Sale disposition."),
				title=_("Invalid Override"),
			)
		if not (self.override_reason or "").strip():
			frappe.throw(
				_("A manual-override disposition requires an Override Reason."),
				title=_("Override Reason Required"),
			)
		# Role gate only when the record is first created — later (rare) system
		# re-saves must not be blocked by the saving context's roles.
		#
		# WHO may mint one depends on what minting one MEANS in this store:
		#
		#   * FastBound store (the default): a manual-override Retail Sale is an
		#     EXCEPTION — the electronic 4473 exists but did not come back — and a
		#     manager owns exceptions.
		#   * Paper 4473 Mode: it is the store's ORDINARY record of a sale. The
		#     cashier who filled the paper form in records it, exactly as they
		#     already can through fastbound.pos_4473.attest_paper_4473; demanding a
		#     manager would put one behind every single gun sale, and this guard
		#     would silently reject the counter's own attestation.
		#
		# Either way the record still carries WHO and WHY — that is what makes a
		# hand-asserted disposition auditable, and it is enforced above.
		if self.is_new():
			roles = set(frappe.get_roles())
			if paper_4473_mode():
				if not (roles & set(COUNTER_4473_ROLES)):
					frappe.throw(
						_("Recording a paper 4473 requires a counter role "
							"(Sales User or above)."),
						frappe.PermissionError,
					)
			elif not (roles & {"Sales Manager", "System Manager"}):
				frappe.throw(
					_("Manual-override dispositions require the Sales Manager or "
						"System Manager role."),
					frappe.PermissionError,
				)

	def _reject_ffl_transfer_without_destination(self) -> None:
		# Return is pushed as a Regular request via the FastBound client, and
		# Regular requires a contact in the payload — without destination_ffl
		# the push 400s with 'contact required'. Treat Return the same as
		# transfers for the destination gate.
		#
		# Gate at submit only (mirroring _require_witnesses_for_destruction)
		# so managers can stage a Draft transfer while looking up the
		# receiving dealer via the 'Lookup FFL via ATF' button.
		if self.docstatus != 1:
			return
		if self.disposition_type in ("FFL Transfer", "Dealer Transfer", "Return") \
				and not (self.destination_ffl or "").strip():
			frappe.throw(
				_("Destination FFL number is required for {0} disposition.").format(self.disposition_type),
				title=_("Destination FFL Required"),
			)

	def _require_witnesses_for_destruction(self) -> None:
		"""Backstop the form-level mandatory_depends_on with a Python validator.
		REST / script callers can otherwise insert+submit a destruction
		disposition with empty witnesses, then push an ATF-non-compliant
		record to FastBound."""
		if self.disposition_type not in ("Scrap", "Destroyed"):
			return
		# Only block at actual submit — drafts may stage witnesses later.
		if self.docstatus != 1:
			return
		missing = [
			label for label, value in (
				("Witness 1", self.get("destroyed_witness1")),
				("Witness 2", self.get("destroyed_witness2")),
			) if not (value or "").strip()
		]
		if missing:
			frappe.throw(
				_("{0} disposition requires both destruction witnesses ({1} missing).")
					.format(self.disposition_type, ", ".join(missing)),
				title=_("Destruction Witnesses Required"),
			)

	def _enforce_compliance_for_retail_sale(self) -> None:
		if self.disposition_type != "Retail Sale":
			return
		# Only block at actual submit — pending draft dispositions (created
		# by the POS 4473 button before FastBound 4473 + NICS complete) MUST
		# be allowed to exist with form_4473_status='Pending' / nics_status='N/A'.
		if self.docstatus != 1:
			return
		# Mirrors the gating rule on Sales Invoice — keeps the bound book
		# legitimate even if a disposition is built directly (script, REST).
		if self.form_4473_status != "Completed":
			frappe.throw(
				_("Retail Sale disposition requires 4473 status = 'Completed'."),
				title=_("4473 Required"),
			)
		if self.nics_status != "Proceed":
			frappe.throw(
				_("Retail Sale disposition requires NICS status = 'Proceed'."),
				title=_("NICS Proceed Required"),
			)
