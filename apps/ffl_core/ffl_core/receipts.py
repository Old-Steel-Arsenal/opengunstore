"""Customer sale-receipt email on Sales Invoice / POS Invoice submit.

The on_submit hook only ENQUEUES (after the submit commits); the actual template
render + PDF attach + send run in a background task, so a receipt failure can
never slow or roll back the sale.
"""
from __future__ import annotations

import frappe

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice

_TEMPLATE = "Sale Receipt"

# Customer receipt print format per invoice doctype (firearm-clean: no UOM / In-Words,
# store logo + store address embedded). Falls back to the doctype default if unmapped.
_RECEIPT_PRINT_FORMAT = {"Sales Invoice": "Sales Receipt", "POS Invoice": "POS Receipt"}


def _customer_email(doc) -> str:
	email = (doc.get("contact_email") or "").strip()
	if not email and doc.get("customer"):
		email = (frappe.db.get_value("Customer", doc.get("customer"), "email_id") or "").strip()
	return email


def send_sale_receipt(doc, method=None) -> None:
	"""on_submit hook: queue the receipt email AFTER the submit commits. No-op if
	the customer has no email (avoids enqueuing a job that would do nothing).

	Skip the consolidated Sales Invoice that POS closing creates from the day's
	POS Invoices (``is_consolidated=1``): the POS Invoice already emailed the
	receipt at sale time, so sending again here double-mails the customer. Direct
	(non-POS) Sales Invoices have ``is_consolidated=0`` and still send."""
	# The Edit rebuild (manual_order.update_order) sets this: an edited order must
	# never auto re-email a receipt — a buyer swap would mail buyer B's firearm
	# receipt to buyer A, and any edit would duplicate-mail. Staff re-send the
	# corrected invoice deliberately from the SI. This on_submit hook enqueues
	# in-process during the rebuild's submit, so the in-process flag is reliable.
	if frappe.flags.get("ffl_suppress_sale_receipt"):
		return
	if doc.doctype == "Sales Invoice" and doc.get("is_consolidated"):
		return
	# Channel-mirrored invoices: the marketplace already emailed the buyer their
	# order confirmation at checkout — don't double-mail. Mirrors interstate_tax /
	# customer_tax_exempt, which skip the same invoices. Both channels book the
	# real buyer (with their email) onto the invoice, so without this guard every
	# web and GunBroker sale would send a second, POS-branded receipt. A web
	# order's in-store pickup ticket (a POS Invoice) is the same sale.
	if is_channel_mirrored_invoice(doc):
		return
	# A counter pickup's 4473 ticket (api/counter_pickup): the sale, its price
	# and its receipt all belong to the counter order the ticket links; the
	# ticket itself totals $0. Mailing it would send the buyer a second,
	# POS-branded receipt listing their gun at $0.00.
	if doc.doctype == "POS Invoice" and (doc.get("ffl_counter_order") or "").strip():
		return
	if not _customer_email(doc):
		return
	frappe.enqueue(
		"ffl_core.receipts.send_sale_receipt_task",
		queue="long", enqueue_after_commit=True,
		doctype=doc.doctype, name=doc.name,
	)


@frappe.task(queue="long", timeout=120)
def send_sale_receipt_task(doctype: str, name: str) -> None:
	"""Render the Sale Receipt template + attach the invoice PDF and email the
	customer. Runs in the background; a failure here is an isolated failed job and
	never affects the already-committed sale."""
	doc = frappe.get_doc(doctype, name)
	email = _customer_email(doc)
	if not email:
		return
	template = frappe.get_doc("Email Template", _TEMPLATE)
	context = {"doc": doc, "customer_name": doc.get("customer_name") or doc.get("customer")}
	body = template.response_html if template.use_html else template.response
	frappe.sendmail(
		recipients=[email],
		subject=frappe.render_template(template.subject, context),
		message=frappe.render_template(body, context),
		reference_doctype=doctype,
		reference_name=name,
		attachments=[frappe.attach_print(
			doctype, name,
			print_format=_RECEIPT_PRINT_FORMAT.get(doctype) or doc.meta.default_print_format or "Standard",
			lang="en")],
		now=False,
	)
