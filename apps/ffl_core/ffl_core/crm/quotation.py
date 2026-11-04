"""Quotation: a quote takes the store's sales tax and is never submitted.

The POS sells at the register or through Create Order. ERPNext turns only a submitted Quotation
into a Sales Order or a Sales Invoice, and neither keeps a link back to the quote, so the one
place to stop both is the quote's submit: a Sales Order or Sales Invoice made straight from a quote
would skip the 4473, the bound book and the cost of goods sold those paths book. A draft quote
prints and emails as usual.
"""
from __future__ import annotations

import frappe
from frappe import _

TAX_TEMPLATE = "Sales Taxes and Charges Template"


def refuse_submit(doc, method=None):
	"""Quotation before_submit: a quote stays a draft (it can still be printed and emailed)."""
	frappe.throw(_("A quote stays a draft: send it as it is. Close the sale at the register or with Create Order."),
		title=_("Sell at the register"))


def default_sales_taxes(doc, method=None):
	"""Quotation before_validate: a quote with no sales tax takes the company's default template
	(ERPNext's own default: ``is_default`` for the company), and ERPNext fills the rows from it.
	A quote that names a template, or carries tax rows of its own, is left as it is."""
	if doc.get("taxes_and_charges") or doc.get("taxes") or not doc.get("company"):
		return
	template = frappe.db.get_value(TAX_TEMPLATE, {"is_default": 1, "company": doc.company}, "name")
	if not template:
		return
	doc.taxes_and_charges = template
	doc.append_taxes_from_master(TAX_TEMPLATE)
