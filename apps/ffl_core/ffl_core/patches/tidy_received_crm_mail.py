"""One-shot: the mail already on a CRM lead or deal reads as it was written.

A plain text mail keeps its line breaks, every link opens in a new tab and a subject folded over
two lines is on one; new mail gets this as it comes in (``crm.mail.tidy_received_mail``). Idempotent: a tidied body is left as it is.
"""
from __future__ import annotations

import frappe

from ffl_core.crm.mail import mail_body_html, one_line


def execute() -> None:
	for r in frappe.get_all("Communication", filters={"sent_or_received": "Received",
			"communication_medium": "Email", "reference_doctype": ("in", ("CRM Lead", "CRM Deal"))},
			fields=["name", "content", "subject"]):
		tidy = {"content": mail_body_html(r.content), "subject": one_line(r.subject)}
		changed = {k: v for k, v in tidy.items() if v != r[k]}
		if changed:
			frappe.db.set_value("Communication", r.name, changed, update_modified=False)
