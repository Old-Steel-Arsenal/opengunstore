"""Service items (FFL transfer fee, gunsmithing, …) are sales-tax-exempt.

setup/tax.py builds a 0% "Non-Taxable Service" Item Tax Template per company,
sets it as the default tax of the "Service" Item Group, and tags the
TRANSFER-FEE item. These tests pin both the wiring AND the behaviour that
matters: ERPNext's own per-line resolver (get_item_tax_template — the function
calculate_taxes_and_totals feeds off) returns the 0% template for a service
line and nothing for goods, so goods still tax at the template's 8.25%.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext.stock.get_item_details import get_item_tax_template

from ffl_core.setup import tax
from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM


def _company() -> str:
	return frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")


def _abbr(company: str) -> str:
	return frappe.get_cached_value("Company", company, "abbr")


def _ensure_default_sales_tax(company: str) -> None:
	"""Guarantee the company has a default Sales Taxes and Charges Template.

	A fresh ERPNext setup may already seed one (CI's company carries "ST 6%");
	only create a test template when none exists, so the assertions key off
	whatever the company actually charges — exactly the account(s) tax.ensure()
	is supposed to zero."""
	if frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1, "disabled": 0}, "name",
	):
		return
	abbr = _abbr(company)
	acct = f"_Test Sales Tax - {abbr}"
	if not frappe.db.exists("Account", acct):
		parent = (
			frappe.db.get_value("Account", {"company": company, "account_name": "Duties and Taxes"}, "name")
			or frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 1}, "name")
		)
		frappe.get_doc({
			"doctype": "Account", "account_name": "_Test Sales Tax", "company": company,
			"parent_account": parent, "account_type": "Tax", "root_type": "Liability", "is_group": 0,
		}).insert(ignore_permissions=True)
	frappe.get_doc({
		"doctype": "Sales Taxes and Charges Template",
		"title": "_Test TX Sales Tax 8.25%", "company": company, "is_default": 1,
		"taxes": [{
			"charge_type": "On Net Total", "account_head": acct,
			"description": "Test Sales Tax 8.25%", "rate": 8.25,
		}],
	}).insert(ignore_permissions=True)


def _ensure_item(code: str, item_group: str, *, is_stock: int, is_firearm: int) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": item_group, "stock_uom": "Nos",
			"is_stock_item": is_stock, "has_serial_no": 0,
			"is_sales_item": 1, "is_firearm": is_firearm,
		}).insert(ignore_permissions=True)
	return code


class ServiceTaxExemption(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap, transfer_fee
		test_bootstrap.run()
		item_groups.ensure()       # full tree incl. the new "Service" group
		transfer_fee.ensure()      # the TRANSFER-FEE service item (now into Service)
		cls.company = _company()
		_ensure_default_sales_tax(cls.company)
		cls.exempt_name = f"{tax.EXEMPT_TEMPLATE_TITLE} - {_abbr(cls.company)}"
		tax.ensure()               # build exempt template + wire group + TRANSFER-FEE
		from ffl_core.setup import trade_in_credit
		trade_in_credit.ensure()   # TRADE-IN-CREDIT item + attach the same 0% exemption
		# tax.ensure saved the group / item; drop stale cached docs so the
		# resolver below reads the freshly-attached templates.
		frappe.clear_cache()
		# the account head(s) the company's default template charges — exactly
		# what the exempt template must zero (whatever they are, ST 6% / TX 8.25%).
		cls.tax_accounts = tax._company_sales_tax_accounts(cls.company)

	# ---------------------------------------------------------------- wiring

	def test_service_group_exists_and_is_not_a_firearm_group(self):
		from ffl_core.firearm import ALL_FIREARM_GROUPS
		self.assertTrue(frappe.db.exists("Item Group", tax.SERVICE_GROUP))
		self.assertNotIn(tax.SERVICE_GROUP, ALL_FIREARM_GROUPS)

	def test_exempt_template_is_zero_on_the_sales_tax_account(self):
		self.assertTrue(self.tax_accounts, "test company should charge sales tax")
		self.assertTrue(frappe.db.exists("Item Tax Template", self.exempt_name))
		itt = frappe.get_doc("Item Tax Template", self.exempt_name)
		self.assertEqual(itt.company, self.company)
		rates = {row.tax_type: row.tax_rate for row in itt.taxes}
		# every account the company charges sales tax on is zeroed by the template
		for acct in self.tax_accounts:
			self.assertIn(acct, rates)
			self.assertEqual(rates[acct], 0)

	def test_service_group_default_tax_is_the_exempt_template(self):
		group = frappe.get_doc("Item Group", tax.SERVICE_GROUP)
		self.assertIn(self.exempt_name, {t.item_tax_template for t in group.taxes})

	def test_transfer_fee_moved_to_service_and_tagged(self):
		item = frappe.get_doc("Item", TRANSFER_FEE_ITEM)
		self.assertEqual(item.item_group, tax.SERVICE_GROUP)
		self.assertIn(self.exempt_name, {t.item_tax_template for t in item.taxes})

	def test_ensure_is_idempotent(self):
		before = frappe.db.count("Item Tax Template", {"company": self.company})
		out = tax.ensure()
		after = frappe.db.count("Item Tax Template", {"company": self.company})
		self.assertEqual(before, after)
		# A second run must change NOTHING: not just skip creating templates, but
		# also not re-sync rows or append duplicate group / item tax links.
		self.assertEqual(out, {"templates": 0, "templates_synced": 0, "group_links": 0,
			"transfer_fee_moved": 0, "transfer_fee_links": 0, "trade_in_credit_links": 0,
			"oos_templates": 0, "dangling_links": 0})

	# ----------------------------------------------- behaviour (calc input)

	def test_service_item_in_group_resolves_to_zero_tax(self):
		"""Any item dropped into the Service group is auto-exempt — proving a
		future gunsmithing-labor item needs no manual tax setup. (ERPNext copies
		the group's default tax templates onto the new item on insert, so the
		exemption holds via the item's own row or the group walk; the 0% rate is
		pinned by test_exempt_template_is_zero_*.)"""
		code = _ensure_item("_TEST-GUNSMITH-LABOR", tax.SERVICE_GROUP, is_stock=0, is_firearm=0)
		item = frappe.get_cached_doc("Item", code)
		self.assertEqual(get_item_tax_template({"company": self.company}, item), self.exempt_name)

	def test_transfer_fee_resolves_to_zero_tax(self):
		item = frappe.get_cached_doc("Item", TRANSFER_FEE_ITEM)
		self.assertEqual(get_item_tax_template({"company": self.company}, item), self.exempt_name)

	def test_goods_item_resolves_to_no_exemption(self):
		"""A firearm/goods item gets NO item tax template, so it keeps the
		Sales Taxes and Charges template's full 8.25%."""
		code = _ensure_item("_TEST-GOODS-PISTOL", "Pistol", is_stock=1, is_firearm=1)
		item = frappe.get_cached_doc("Item", code)
		self.assertIsNone(get_item_tax_template({"company": self.company}, item))

	def test_service_line_computes_zero_tax_while_goods_is_taxed(self):
		"""End-to-end through ERPNext's calculate_taxes_and_totals: a Sales Invoice
		carrying the company's sales-tax template taxes the goods line but NOT the
		exempt service line — the dollar outcome the resolver tests imply. Proves
		the exemption actually computes $0, not just that config is wired."""
		svc = _ensure_item("_TEST-SVC-LINE", tax.SERVICE_GROUP, is_stock=0, is_firearm=0)
		goods = _ensure_item("_TEST-GOODS-LINE", "Pistol", is_stock=1, is_firearm=1)
		tmpl_name = frappe.db.get_value(
			"Sales Taxes and Charges Template",
			{"company": self.company, "is_default": 1, "disabled": 0}, "name")
		template = frappe.get_doc("Sales Taxes and Charges Template", tmpl_name)
		pct = sum(flt(t.rate) for t in template.taxes if t.charge_type == "On Net Total")
		self.assertGreater(pct, 0, "test template should charge a percentage sales tax")

		GOODS_AMT, SVC_AMT = 500.0, 25.0
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		si.conversion_rate = 1
		si.taxes_and_charges = tmpl_name
		for code, amt in ((svc, SVC_AMT), (goods, GOODS_AMT)):
			row = si.append("items", {"item_code": code, "qty": 1, "rate": amt})
			t = get_item_tax_template({"company": self.company}, frappe.get_cached_doc("Item", code))
			if t:
				row.item_tax_template = t
		for t in template.taxes:
			si.append("taxes", {
				"charge_type": t.charge_type, "account_head": t.account_head,
				"description": t.description or t.account_head, "rate": t.rate,
				"cost_center": t.cost_center,
			})
		si.run_method("calculate_taxes_and_totals")

		total_tax = round(sum(flt(t.tax_amount) for t in si.taxes), 2)
		# Goods only: if the service line were taxed, total would be the larger figure.
		self.assertEqual(total_tax, round(GOODS_AMT * pct / 100.0, 2))
		self.assertNotEqual(total_tax, round((GOODS_AMT + SVC_AMT) * pct / 100.0, 2))

	# ----------------------------------------- trade-in credit (taxed, reduces base)

	def test_trade_in_credit_carries_no_exempt_template(self):
		"""TRADE-IN-CREDIT must NOT carry the 0% template: the negative line has
		to participate in the sales-tax base so a trade-in allowance reduces the
		taxable amount (TX Tax Code §151.007(c)(3))."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		item = frappe.get_doc("Item", TRADE_IN_CREDIT_ITEM)
		self.assertNotIn(self.exempt_name, {t.item_tax_template for t in item.taxes})

	def test_trade_in_credit_resolves_to_no_exemption(self):
		"""No item-level override → the credit line follows the invoice's own
		Sales Taxes and Charges template, same as goods."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		item = frappe.get_cached_doc("Item", TRADE_IN_CREDIT_ITEM)
		self.assertIsNone(get_item_tax_template({"company": self.company}, item))

	def test_unwire_strips_existing_exempt_links(self):
		"""A site that ran the old wiring (prod) carries the exempt link on the
		item; tax.ensure() on migrate must strip it so deploy self-heals."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		item = frappe.get_doc("Item", TRADE_IN_CREDIT_ITEM)
		item.append("taxes", {"item_tax_template": self.exempt_name})
		item.save(ignore_permissions=True)
		removed = tax._unwire_trade_in_credit()
		self.assertEqual(removed, 1)
		item.reload()
		self.assertNotIn(self.exempt_name, {t.item_tax_template for t in item.taxes})

	def test_trade_in_taxes_net_of_trade_in(self):
		"""End-to-end through calculate_taxes_and_totals: a gun sold WITH a
		trade-in credit is taxed on the DIFFERENCE (price net of trade-in), per
		TX Tax Code §151.007(c)(3) — an even trade computes $0 tax. Repro shape:
		gun 1330.89, trade-in -869 → tax on 461.89, NOT on 1330.89."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		gun = _ensure_item("_TEST-TI-GUN", "Pistol", is_stock=1, is_firearm=1)
		tmpl_name = frappe.db.get_value(
			"Sales Taxes and Charges Template",
			{"company": self.company, "is_default": 1, "disabled": 0}, "name")
		template = frappe.get_doc("Sales Taxes and Charges Template", tmpl_name)
		pct = sum(flt(t.rate) for t in template.taxes if t.charge_type == "On Net Total")
		self.assertGreater(pct, 0, "test template should charge a percentage sales tax")

		GUN_AMT, CREDIT_AMT = 1330.89, -869.0
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		si.conversion_rate = 1
		si.taxes_and_charges = tmpl_name
		for code, amt in ((gun, GUN_AMT), (TRADE_IN_CREDIT_ITEM, CREDIT_AMT)):
			row = si.append("items", {"item_code": code, "qty": 1, "rate": amt})
			t = get_item_tax_template({"company": self.company}, frappe.get_cached_doc("Item", code))
			if t:
				row.item_tax_template = t
		for t in template.taxes:
			si.append("taxes", {
				"charge_type": t.charge_type, "account_head": t.account_head,
				"description": t.description or t.account_head, "rate": t.rate,
				"cost_center": t.cost_center,
			})
		si.run_method("calculate_taxes_and_totals")

		total_tax = round(sum(flt(t.tax_amount) for t in si.taxes), 2)
		# Tax on the NET (1330.89 - 869 = 461.89), NOT the full gun price.
		self.assertEqual(total_tax, round((GUN_AMT + CREDIT_AMT) * pct / 100.0, 2))
		self.assertNotEqual(total_tax, round(GUN_AMT * pct / 100.0, 2))

	def test_even_trade_computes_zero_tax(self):
		"""The even-trade case that triggered this fix (a past invoice):
		gun 639 + trade-in -639 → taxable base 0 → tax $0."""
		from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
		gun = _ensure_item("_TEST-TI-GUN", "Pistol", is_stock=1, is_firearm=1)
		tmpl_name = frappe.db.get_value(
			"Sales Taxes and Charges Template",
			{"company": self.company, "is_default": 1, "disabled": 0}, "name")
		template = frappe.get_doc("Sales Taxes and Charges Template", tmpl_name)

		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		si.conversion_rate = 1
		si.taxes_and_charges = tmpl_name
		for code, amt in ((gun, 639.0), (TRADE_IN_CREDIT_ITEM, -639.0)):
			row = si.append("items", {"item_code": code, "qty": 1, "rate": amt})
			t = get_item_tax_template({"company": self.company}, frappe.get_cached_doc("Item", code))
			if t:
				row.item_tax_template = t
		for t in template.taxes:
			si.append("taxes", {
				"charge_type": t.charge_type, "account_head": t.account_head,
				"description": t.description or t.account_head, "rate": t.rate,
				"cost_center": t.cost_center,
			})
		si.run_method("calculate_taxes_and_totals")

		self.assertEqual(round(sum(flt(t.tax_amount) for t in si.taxes), 2), 0.0)


class DanglingItemTaxTemplateLinks(FrappeTestCase):
	"""An Item Tax row whose template is gone, and the repair that removes it.

	Written against the real defect rather than around it (docs/testing.md §7: a
	guard that has never failed is a claim, not a check). The first test seeds the
	orphan and watches ERPNext's resolver blow up on it; the second runs the
	repair and watches the same call come back. Between them they pin why
	`_prune_dangling_template_links` exists — without the first one, a future
	reader has no way to tell whether the prune is load-bearing or tidiness.

	How the orphan gets there in the wild: the Service group and TRANSFER-FEE are
	GLOBAL rows carrying one link per company, and nothing removed a link when its
	company's template did. Committed link, vanished template, and every service
	line on the site stops calculating.
	"""

	MISSING = "Non-Taxable Service - _NOSUCHCOMPANY"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()
		cls.company = _company()
		# tax.ensure only builds an exempt template for a company that charges
		# sales tax, and the repair test asserts the surviving link still
		# resolves — so say what the company charges rather than inherit it.
		_ensure_default_sales_tax(cls.company)

	def setUp(self):
		super().setUp()
		self.assertFalse(frappe.db.exists("Item Tax Template", self.MISSING))
		# Built through the framework with links unchecked, not hand-written SQL:
		# the point is a row the DB accepted and the resolver cannot use, which is
		# exactly what `ignore_links` produces.
		row = frappe.new_doc("Item Tax")
		row.parenttype = "Item Group"
		row.parent = tax.SERVICE_GROUP
		row.parentfield = "taxes"
		row.item_tax_template = self.MISSING
		row.idx = 99
		row.insert(ignore_permissions=True, ignore_links=True)
		self.orphan = row.name
		frappe.clear_document_cache("Item Group", tax.SERVICE_GROUP)

	def _service_item(self, code: str) -> str:
		"""A service item created AFTER the orphan. Since ERPNext 16.55 the item
		keeps no copy of the group's tax rows: the resolver walks up to the group
		on every line, which is how one bad row reaches every item in it."""
		if frappe.db.exists("Item", code):
			frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		return _ensure_item(code, tax.SERVICE_GROUP, is_stock=0, is_firearm=0)

	def test_a_dangling_link_breaks_the_erpnext_resolver(self):
		code = self._service_item("_TEST-DANGLING-SVC")
		item = frappe.get_cached_doc("Item", code)
		self.assertNotIn(self.MISSING, {t.item_tax_template for t in item.taxes})
		with self.assertRaises(TypeError) as ctx:
			get_item_tax_template({"company": self.company}, item)
		self.assertIn("cannot unpack", str(ctx.exception))

	def test_ensure_refuses_to_prune_above_the_ceiling(self):
		"""Hundreds of orphans on a prod migrate means templates were renamed or
		dropped without their child links — not that a company retired — and a
		site-wide DELETE would take every operator-set per-item override with them.
		Above the ceiling the prune must remove NOTHING (the setUp orphan included)
		and say so in the Error Log, template names attached, so the breakage stays
		visible instead of being tidied away."""
		extra = []
		for i in range(tax._PRUNE_CEILING):
			row = frappe.new_doc("Item Tax")
			row.parenttype = "Item Group"
			row.parent = tax.SERVICE_GROUP
			row.parentfield = "taxes"
			row.item_tax_template = f"{self.MISSING}-{i}"
			row.idx = 100 + i
			row.insert(ignore_permissions=True, ignore_links=True)
			extra.append(row.name)
		frappe.clear_document_cache("Item Group", tax.SERVICE_GROUP)
		before = frappe.db.count("Error Log")

		self.assertEqual(tax.ensure()["dangling_links"], 0)

		self.assertTrue(frappe.db.exists("Item Tax", self.orphan))
		self.assertTrue(all(frappe.db.exists("Item Tax", n) for n in extra),
			"above the ceiling not one row may go")
		self.assertGreaterEqual(frappe.db.count("Error Log"), before + 1)
		# frappe.log_error(title=...) lands the title in Error Log.method, not a
		# `title` column.
		logs = frappe.get_all("Error Log", filters={"method": ("like", "%NOT pruned%")},
			fields=["error"], order_by="creation desc", limit=1)
		self.assertTrue(logs, "the refusal must be logged")
		self.assertIn(self.MISSING, logs[0].error,
			"the log must name what the rows pointed at, or nobody can repair them")

	def test_ensure_prunes_it_and_the_resolver_works_again(self):
		removed = tax.ensure()["dangling_links"]
		self.assertGreaterEqual(removed, 1)
		self.assertFalse(frappe.db.exists("Item Tax", self.orphan))
		code = self._service_item("_TEST-PRUNED-SVC")
		item = frappe.get_cached_doc("Item", code)
		self.assertNotIn(self.MISSING, {t.item_tax_template for t in item.taxes})
		# and the exemption still resolves — the prune took the orphan, not the row
		# the group is actually wired with.
		self.assertEqual(
			get_item_tax_template({"company": self.company}, item),
			f"{tax.EXEMPT_TEMPLATE_TITLE} - {_abbr(self.company)}")


if __name__ == "__main__":
	unittest.main()
