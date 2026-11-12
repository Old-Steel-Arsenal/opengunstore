"""Sales Report: an excise tax collected beside sales tax is reported as its own figure.

A store with California's firearm and ammunition excise tax (CFET) has the excise account in its
default sales-tax template (rate 0 there; the goods' Item Tax Template charges 11%). The report used
to read every account of that template as sales tax, so a 500 pistol at 8.75% + 11% showed 98.75 of
sales tax. It is now 43.75 sales tax and 55.00 excise.

The split is for reporting only: `_company_sales_tax_accounts` still lists the excise account,
because the 0% service template is built from it — a service line must stay free of both taxes.

One stand-in company, rolled back with the class.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from ffl_core.compliance import le_excise_exempt as le
from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.setup import tax

COMPANY, ABBR = "_Test Excise Store Co", "TXS"
CUSTOMER = "_Test Excise Customer"
GUN, SERVICE = "_TEST-XS-PISTOL", "_TEST-XS-SERVICE"
SALES_RATE, EXCISE_RATE = 8.75, 11


def _account(name: str) -> str:
	full = f"{name} - {ABBR}"
	if not frappe.db.exists("Account", full):
		frappe.get_doc({"doctype": "Account", "account_name": name, "company": COMPANY, "is_group": 0,
			"parent_account": f"Duties and Taxes - {ABBR}", "account_type": "Tax"}).insert(ignore_permissions=True)
	return full


class ExciseStoreFixtures(FrappeTestCase):
	"""A store whose default template carries sales tax and the excise row, the way store_setup builds it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		if not frappe.db.exists("Company", COMPANY):
			frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": ABBR,
				"default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True)
		cls.company = COMPANY
		cls.sales_tax = _account("Sales Tax Payable")
		cls.excise = _account(tax.EXCISE_ACCOUNT_NAME)
		cls.template = frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template", "title": "_Test Excise Sales Tax", "company": COMPANY,
			"is_default": 1, "taxes": [
				{"charge_type": "On Net Total", "account_head": cls.sales_tax, "rate": SALES_RATE,
					"description": "Sales Tax"},
				{"charge_type": "On Net Total", "account_head": cls.excise, "rate": 0,
					"description": "Excise - firearm and ammunition lines"},
			]}).insert(ignore_permissions=True).name
		goods = frappe.get_doc({"doctype": "Item Tax Template", "title": "_Test Excise Goods", "company": COMPANY,
			"taxes": [{"tax_type": cls.sales_tax, "tax_rate": SALES_RATE},
				{"tax_type": cls.excise, "tax_rate": EXCISE_RATE}]}).insert(ignore_permissions=True).name
		# The 0% service template and its Service-group link, from the full account list.
		tax.ensure()
		cls.service_template = frappe.db.get_value("Item Tax Template",
			{"company": COMPANY, "title": tax.EXEMPT_TEMPLATE_TITLE}, "name")
		group = "_Test Excise Goods Group"
		if not frappe.db.exists("Item Group", group):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": group,
				"parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", GUN):
			frappe.get_doc({"doctype": "Item", "item_code": GUN, "item_name": GUN, "item_group": group,
				"stock_uom": "Nos", "is_stock_item": 0, "taxes": [{"item_tax_template": goods}]}
				).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", SERVICE):
			frappe.get_doc({"doctype": "Item", "item_code": SERVICE, "item_name": SERVICE,
				"item_group": tax.SERVICE_GROUP, "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc({"doctype": "Customer", "customer_name": CUSTOMER, "customer_group": "Individual",
				"territory": "All Territories"}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.db.savepoint("excise_case")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="excise_case")

	def _si(self, *lines, submit=True, **fields):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		si = frappe.get_doc({"doctype": "Sales Invoice", "company": COMPANY, "customer": CUSTOMER,
			"posting_date": nowdate(), "set_posting_time": 1, "taxes_and_charges": self.template,
			"items": [{"item_code": code, "qty": 1, "rate": rate} for code, rate in lines], **fields})
		si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", self.template))
		si.insert(ignore_permissions=True)
		if submit:
			si.submit()
		return si


class ExciseSplit(ExciseStoreFixtures):

	def _rows(self, si, view):
		_c, data, *_ = sr.execute({"from_date": str(si.posting_date), "to_date": str(si.posting_date), "view": view})
		return [r for r in data if r.get("invoice") == si.name]

	def test_the_accounts_split_into_sales_tax_and_excise(self):
		self.assertEqual(tax.sales_tax_and_excise_accounts(COMPANY), ([self.sales_tax], [self.excise]))

	def test_the_service_template_still_covers_the_excise_account(self):
		"""Regression guard: the 0% service template is built from `_company_sales_tax_accounts`, which must keep
		the excise account — dropping it there would start charging excise on every service line."""
		self.assertEqual(tax._company_sales_tax_accounts(COMPANY), [self.sales_tax, self.excise])
		self.assertEqual(sorted((r.tax_type, flt(r.tax_rate)) for r in
			frappe.get_doc("Item Tax Template", self.service_template).taxes),
			sorted([(self.sales_tax, 0.0), (self.excise, 0.0)]))

	def test_a_pistol_reports_sales_tax_and_excise_apart(self):
		"""500 pistol: sales tax 43.75 and excise 55.00 (it read 98.75 of sales tax before)."""
		si = self._si((GUN, 500))
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 98.75, places=2)
		order = next(r for r in self._rows(si, "Order Detail") if r["row_kind"] == "order")
		self.assertEqual((flt(order["sales_tax"], 2), flt(order["excise_tax"], 2)), (43.75, 55.0))
		self.assertFalse(order["tax_mismatch"])
		line = next(r for r in self._rows(si, "Product") if r["row_kind"] == "product")
		self.assertEqual((flt(line["line_tax"], 2), flt(line["excise_tax"], 2)), (43.75, 55.0))
		self.assertFalse([r for r in self._rows(si, "Product") if r["row_kind"] == "unclassified"])

	def test_a_service_line_carries_neither_tax(self):
		si = self._si((GUN, 500), (SERVICE, 100))
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 98.75, places=2)
		lines = {r["item_code"]: r for r in self._rows(si, "Product") if r["row_kind"] == "product"}
		self.assertEqual((lines[SERVICE]["line_tax"], lines[SERVICE]["excise_tax"]), (0, 0))
		self.assertEqual((flt(lines[GUN]["line_tax"], 2), flt(lines[GUN]["excise_tax"], 2)), (43.75, 55.0))

	def test_the_excise_card_shows_only_when_excise_was_collected(self):
		total = {"is_total": True, "gross_merch": 500.0, "cost": 0, "profit": 500.0, "sales_tax": 43.75,
			"shipping": 0, "excise_tax": 55.0}
		labels = [c["label"] for c in sr._report_summary("Product", [total])]
		self.assertEqual(labels[4:7], ["Sales Tax", "Excise Tax", "Shipping"])
		self.assertNotIn("Excise Tax", [c["label"] for c in sr._report_summary("Product", [{**total, "excise_tax": 0}])])

	def test_the_peace_officer_account_counts_as_excise(self):
		"""FFL Settings' exempt account is excise even outside the default template — but only for its own company."""
		other = _account("_Test Other Excise")
		was = frappe.db.get_single_value("FFL Settings", le.SETTING)
		try:
			frappe.db.set_single_value("FFL Settings", le.SETTING, other)
			self.assertEqual(tax.sales_tax_and_excise_accounts(COMPANY)[1], sorted([self.excise, other]))
			ft = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
			if ft and tax._company_sales_tax_accounts(ft):
				self.assertNotIn(other, tax.sales_tax_and_excise_accounts(ft)[1])
		finally:
			frappe.db.set_single_value("FFL Settings", le.SETTING, was)
