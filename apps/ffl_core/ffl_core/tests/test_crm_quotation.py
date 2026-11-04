"""A quote is never submitted (so never a Sales Order or invoice); a quote without sales tax takes the store's default."""
from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from ffl_core.crm import quotation
from ffl_core.tests.crm_support import AMMO, CRMTestCase


def _company() -> str:
	return frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name", limit=1)[0]


def _customer() -> str:
	name = frappe.db.get_value("Customer", {"customer_name": "_T CRM Buyer"})
	return name or frappe.get_doc({"doctype": "Customer", "customer_name": "_T CRM Buyer",
		"customer_type": "Individual"}).insert(ignore_permissions=True).name


def _default_template() -> str:
	company = _company()
	name = frappe.db.get_value(quotation.TAX_TEMPLATE, {"is_default": 1, "company": company})
	if name:
		return name
	account = frappe.get_all("Account", filters={"company": company, "account_type": "Tax", "is_group": 0},
		pluck="name", limit=1) or frappe.get_all("Account", filters={"company": company, "is_group": 0,
		"root_type": "Liability"}, pluck="name", limit=1)
	return frappe.get_doc({"doctype": quotation.TAX_TEMPLATE, "title": "_T CRM Sales Tax", "company": company,
		"is_default": 1, "taxes": [{"charge_type": "On Net Total", "account_head": account[0],
		"description": "Sales tax", "rate": 8.75}]}).insert(ignore_permissions=True).name


def _quote(**fields):
	return frappe.get_doc({"doctype": "Quotation", "quotation_to": "Customer", "party_name": _customer(),
		"company": _company(), "transaction_date": nowdate(), "valid_till": add_days(nowdate(), 30),
		"items": [{"item_code": AMMO, "qty": 2, "rate": 24.99}], **fields}).insert(ignore_permissions=True)


class TestQuotes(CRMTestCase):
	def test_a_quote_without_tax_takes_the_default_template(self):
		template = _default_template()
		quote = _quote()
		self.assertEqual(quote.taxes_and_charges, template)
		self.assertTrue(quote.taxes)
		self.assertGreater(quote.total_taxes_and_charges, 0)

	def test_a_quote_naming_its_template_keeps_it(self):
		_default_template()
		other = frappe.copy_doc(frappe.get_doc(quotation.TAX_TEMPLATE, _default_template()))
		other.title, other.is_default = "_T CRM Other Tax", 0
		other.insert(ignore_permissions=True)
		quote = _quote(taxes_and_charges=other.name)
		self.assertEqual(quote.taxes_and_charges, other.name)

	def test_a_quote_cannot_be_submitted_so_never_becomes_an_order_or_invoice(self):
		_default_template()
		quote = _quote()
		with self.assertRaises(frappe.ValidationError) as ctx:
			quote.submit()
		self.assertIn("Create Order", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Quotation", quote.name, "docstatus"), 0)
		# ERPNext maps only a submitted quote: both conversions are refused at their source.
		from erpnext.selling.doctype.quotation.quotation import make_sales_invoice, make_sales_order

		for make in (make_sales_order, make_sales_invoice):
			with self.assertRaises(frappe.ValidationError, msg=make.__name__):
				make(quote.name)
