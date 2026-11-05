"""Lead Mail Settings (CRM mail settings): the store's team addresses and the domains whose mail never
becomes a CRM lead. Incoming rules in ffl_core.crm.mail, team addresses in ffl_core.crm.team."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address


class LeadMailSettings(Document):
	def validate(self):
		from ffl_core.crm.mail import domain_lines
		from ffl_core.crm.team import FULL_SYNC_NEEDED, MAILBOX, full_sync

		# One clean domain per line (lowercase, no "@", no blanks or repeats).
		for field in ("blocked_sender_domains", "own_domains"):
			self.set(field, "\n".join(domain_lines(self.get(field))))

		seen = set()
		for row in self.get("team_addresses") or []:
			row.address = (row.address or "").strip().lower()
			if not validate_email_address(row.address):
				frappe.throw(_("Row {0}: {1} is not an email address.").format(row.idx, frappe.bold(row.address)))
			if row.address in seen:
				frappe.throw(_("{0} is listed twice.").format(frappe.bold(row.address)))
			seen.add(row.address)
			if row.address_type == MAILBOX and not row.collector:
				frappe.throw(_("Row {0}: a mailbox needs the Email Account that collects it.").format(row.idx))
			if row.collector:
				account = frappe.db.get_value("Email Account", row.collector,
					["enable_incoming", "use_imap", "email_sync_option"], as_dict=True)
				if account and account.enable_incoming and not full_sync(account):
					frappe.throw(_(FULL_SYNC_NEEDED).format(frappe.bold(row.collector)), title=_("Sync all mail"))
		self.primary_team_address = (self.primary_team_address or "").strip().lower()
		if self.primary_team_address and self.primary_team_address not in seen:
			frappe.throw(_("The Reply-To address must be one of the team addresses."))

	def on_update(self):
		from ffl_core.crm.team import apply

		frappe.clear_document_cache(self.doctype, self.name)   # apply() reads the cached settings
		apply()
