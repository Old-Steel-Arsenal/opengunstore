"""One-shot: the mail already on a CRM lead or deal reads as it was written.

A plain text mail keeps its line breaks and every link opens in a new tab; new mail gets this as
it comes in (``crm.mail.tidy_received_mail``). Idempotent: a tidied body is left as it is.
"""
from __future__ import annotations

import frappe

from ffl_core.crm.mail import mail_body_html


def execute() -> None:
	for r in frappe.get_all("Communication", filters={"sent_or_received": "Received",
			"communication_medium": "Email", "reference_doctype": ("in", ("CRM Lead", "CRM Deal"))},
			fields=["name", "content"]):
		content = mail_body_html(r.content)
		if content != r.content:
			frappe.db.set_value("Communication", r.name, "content", content, update_modified=False)
