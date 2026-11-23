"""``ffl_core.setup.test_bootstrap`` repairs a test company that has no sales-tax template.

A narrow ``bench run-tests --module ...`` on a site that was never bootstrapped leaves a half-created
Company without ERPNext's sales-tax seed, and no later bootstrap used to put it back.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import test_bootstrap
from ffl_core.setup.dev_site import COMPANY

TEMPLATE = "Sales Taxes and Charges Template"


class DefaultSalesTax(FrappeTestCase):
	def test_a_company_without_its_sales_tax_seed_gets_it_back(self):
		company = frappe.db.get_value("Company", {"company_name": COMPANY}, "name")
		abbr = frappe.db.get_value("Company", company, "abbr")
		seed = f"US ST 6% - {abbr}"
		# The half-created Company: no template of ERPNext's seed, so no default either.
		frappe.db.set_value(TEMPLATE, {"company": company}, "is_default", 0)
		for name in frappe.get_all(TEMPLATE, filters={"company": company, "title": ["like", "US ST %"]}, pluck="name"):
			frappe.delete_doc(TEMPLATE, name, force=1, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(TEMPLATE, seed))

		test_bootstrap._ensure_default_sales_tax()

		self.assertEqual(frappe.db.get_value(TEMPLATE, {"company": company, "is_default": 1}, "name"), seed)
		self.assertEqual(frappe.db.get_value("Sales Taxes and Charges", {"parent": seed}, "rate"), 6)

	def test_a_company_with_a_default_template_is_left_alone(self):
		company = frappe.db.get_value("Company", {"company_name": COMPANY}, "name")
		self.assertTrue(frappe.db.exists(TEMPLATE, {"company": company, "is_default": 1}), "the bootstrap seeds one")
		before = frappe.db.count(TEMPLATE)
		with patch("erpnext.setup.setup_wizard.operations.taxes_setup.setup_taxes_and_charges") as seed:
			test_bootstrap._ensure_default_sales_tax()
		seed.assert_not_called()
		self.assertEqual(frappe.db.count(TEMPLATE), before)
