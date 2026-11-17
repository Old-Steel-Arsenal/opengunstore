"""One-shot: a received mail stored with no recipients (GunBroker's member mail has no To the POS
keeps) gets its Email Account's address, as ``crm.mail.fill_recipients`` does for new mail: Frappe CRM's
Reply fails on a mail without. Every such mail, not only GunBroker's. No hooks run and ``modified``
stays. Idempotent: a mail with recipients is left alone.
"""
from __future__ import annotations

import frappe


def execute() -> None:
	frappe.db.sql("""
		UPDATE `tabCommunication` c
		JOIN `tabEmail Account` a ON a.name = c.email_account
		SET c.recipients = a.email_id
		WHERE c.sent_or_received = 'Received' AND c.communication_medium = 'Email'
			AND IFNULL(TRIM(c.recipients), '') = '' AND IFNULL(a.email_id, '') != ''
	""")
