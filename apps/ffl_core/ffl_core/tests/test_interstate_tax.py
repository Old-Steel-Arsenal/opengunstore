"""Out-of-state tax exemption — destination-based, server-enforced.

Texas charges sales tax when possession transfers in Texas; goods SHIPPED out
of state are exempt. Address.state is free text and production data already
mixes "TX"/"Texas", so the engine normalizes state spellings and decides per
document from the SHIPPING address (no shipping address = in-store pickup =
always taxed). Deliberately not built on ERPNext Tax Rule (exact-string,
client-trigger-driven, and a footgun config can zero-tax walk-ins).
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import interstate_tax
from ffl_core.compliance.interstate_tax import normalize_us_state


class NormalizeState(FrappeTestCase):
	def test_usps_codes_pass_through(self):
		self.assertEqual(normalize_us_state("TX"), "TX")
		self.assertEqual(normalize_us_state(" tx "), "TX")
		self.assertEqual(normalize_us_state("CA"), "CA")

	def test_full_names_map_to_codes(self):
		self.assertEqual(normalize_us_state("Texas"), "TX")
		self.assertEqual(normalize_us_state("texas"), "TX")
		self.assertEqual(normalize_us_state("California"), "CA")
		self.assertEqual(normalize_us_state("New Mexico"), "NM")
		self.assertEqual(normalize_us_state("District of Columbia"), "DC")

	def test_unknown_values_return_none(self):
		self.assertIsNone(normalize_us_state("Ontario"))
		self.assertIsNone(normalize_us_state(""))
		self.assertIsNone(normalize_us_state(None))


class HomeState(FrappeTestCase):
	"""The home state is the Company's own address (the one its receipts print):
	each store runs its own site, in whatever state its address says."""

	def _home(self, state):
		from unittest.mock import patch

		from ffl_core.compliance import interstate_tax as it

		addr = None
		if state is not None:
			addr = frappe.db.get_value("Address", {"address_title": "_Test Home State"}, "name")
			if addr:
				frappe.db.set_value("Address", addr, "state", state, update_modified=False)
			else:
				addr = frappe.get_doc({"doctype": "Address", "address_title": "_Test Home State",
					"address_type": "Billing", "address_line1": "1 Main", "city": "Corona",
					"state": state, "country": "United States"}).insert(ignore_permissions=True).name
		frappe.local.request_cache.clear()  # home_state is cached per request
		try:
			with patch("ffl_core.store_identity.company_address", return_value=addr):
				return it.home_state("Any Co"), it.freight_taxable("Any Co")
		finally:
			frappe.local.request_cache.clear()

	def test_the_companys_address_decides(self):
		self.assertEqual(self._home("California"), ("CA", False))
		self.assertEqual(self._home("TX"), ("TX", True))
		self.assertEqual(self._home("Nevada"), ("NV", True), "untested states tax freight")

	def test_no_readable_address_raises_instead_of_taxing_as_texas(self):
		"""Silently taxing a California store as Texas was the bug this fixed: no
		address, or one whose state cannot be read, is a clear error that names
		what to set — never a guessed home state."""
		for state in (None, "Calif", ""):
			with self.assertRaises(frappe.ValidationError, msg=repr(state)) as caught:
				self._home(state)
			self.assertIn("Set the company's address", str(caught.exception))

	def test_check_company_address_is_a_pure_read_that_says_how_to_fix_it(self):
		from unittest.mock import patch

		from ffl_core.compliance import interstate_tax as it

		self.assertIsNone(self._check("Nevada"))
		for state in (None, "Calif"):
			msg = self._check(state)
			self.assertIn("Company › Addresses", msg)
			self.assertIn("US state", msg)
			self.assertNotIn("interstate", msg)

	def _check(self, state):
		from unittest.mock import patch

		from ffl_core.compliance import interstate_tax as it

		addr = None
		if state is not None:
			addr = frappe.get_doc({"doctype": "Address", "address_title": "_Test Check Addr",
				"address_type": "Billing", "address_line1": "1 Main", "city": "Salem",
				"state": state, "country": "United States"}).insert(ignore_permissions=True).name
		frappe.local.request_cache.clear()
		with patch("ffl_core.store_identity.company_address", return_value=addr):
			return it.check_company_address("Any Co")

	def test_the_after_migrate_check_alerts_once_per_company_without_an_address(self):
		from unittest.mock import patch

		from ffl_core.setup import company_address as setup_check

		with patch("ffl_core.compliance.interstate_tax.check_company_address",
				side_effect=lambda c: None if c == "FFL Test" else "Set the company's address."), \
				patch.object(frappe, "get_all", side_effect=lambda dt, **k: ["FFL Test", "_Co B"]
					if dt == "Company" else []), \
				patch("ffl_core.alerts.alert_once") as alert:
			setup_check.ensure()
		self.assertEqual(alert.call_count, 1)
		self.assertEqual(alert.call_args.args[:2], ("Company", "_Co B"))

	def test_the_report_is_made_again_once_the_todo_is_closed_and_the_address_still_missing(self):
		"""No dedupe on an Error Log title: an open ToDo about the Company holds the
		report back, closing it without a fix brings the report back at the next
		migrate, and a fixed address ends it."""
		from unittest.mock import patch

		from ffl_core.setup import company_address as setup_check

		company = frappe.get_all("Company", pluck="name", limit=1)[0]

		def open_todos():
			return frappe.get_all("ToDo", filters={"reference_type": "Company",
				"reference_name": company, "status": "Open"}, pluck="name")

		broken = {"on": True}
		with patch("ffl_core.compliance.interstate_tax.check_company_address",
				side_effect=lambda c: "Set the company's address." if c == company and broken["on"] else None), \
				patch("ffl_core.alerts.alert_assignees", return_value=["Administrator"]):
			setup_check.ensure()
			first = open_todos()
			self.assertEqual(len(first), 1)
			setup_check.ensure()
			self.assertEqual(open_todos(), first, "an open ToDo keeps the report from repeating")
			frappe.db.set_value("ToDo", first[0], "status", "Closed")
			setup_check.ensure()
			again = open_todos()
			self.assertEqual(len(again), 1)
			self.assertNotEqual(again, first, "closing the ToDo re-arms the report")
			frappe.db.set_value("ToDo", again[0], "status", "Closed")
			broken["on"] = False
			setup_check.ensure()
			self.assertEqual(open_todos(), [], "a fixed address is not reported")

	def test_the_fix_message_is_one_literal_translatable_string(self):
		"""Both call sites go through address_fix(), whose _() holds the literal text."""
		from pathlib import Path

		from ffl_core.compliance import interstate_tax as it

		src = Path(it.__file__).read_text(encoding="utf-8")
		self.assertIn('_("Set the company\'s address (with a US state) in Company › Addresses.")', src)
		self.assertNotIn("_(ADDRESS_FIX)", src)
		self.assertEqual(it.address_fix().count("Company › Addresses"), 1)
		import csv

		zh = Path(it.__file__).parents[1] / "translations" / "zh.csv"
		keys = {row[0] for row in csv.reader(zh.open(encoding="utf-8")) if row}
		self.assertIn("Set the company's address (with a US state) in Company › Addresses.", keys)

	def test_a_company_without_an_address_blocks_the_interstate_decision(self):
		"""The document path that needed the home state is the one that stops:
		a US-bound shipment cannot be classified in/out of state."""
		from types import SimpleNamespace
		from unittest.mock import patch

		from ffl_core.compliance import interstate_tax as it

		doc = SimpleNamespace(shipping_address_name="ship-1", company="_Co")
		frappe.local.request_cache.clear()
		with patch.object(frappe.db, "get_value",
				return_value=frappe._dict(state="Nevada", country="United States")), \
				patch("ffl_core.store_identity.company_address", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				it._ships_out_of_state(doc)
		frappe.local.request_cache.clear()

	def test_the_real_company_address_chain(self):
		"""No patch: the Address linked to the Company (the one its receipts print)
		— ERPNext marks it is_your_company_address — decides, and the counter's
		quote config says so."""
		from ffl_core.api.manual_order import quote_config
		from ffl_core.compliance import interstate_tax as it

		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		# Inserting a primary address clears the flag on the Company's current one
		# (Address.validate_preferred_address); put it back afterwards.
		primaries = frappe.get_all("Address", filters=[
			["Dynamic Link", "link_doctype", "=", "Company"],
			["Dynamic Link", "link_name", "=", company], ["is_primary_address", "=", 1]],
			pluck="name")
		addr = frappe.get_doc({"doctype": "Address", "address_title": "_Test CA Store",
			"address_type": "Shop", "address_line1": "1 Main", "city": "Corona",
			"state": "California", "country": "United States", "is_primary_address": 1,
			"links": [{"link_doctype": "Company", "link_name": company}]}
			).insert(ignore_permissions=True)
		try:
			frappe.local.request_cache.clear()
			self.assertEqual(it.home_state(company), "CA")
			cfg = quote_config(company)
			self.assertEqual((cfg["home_state"], cfg["freight_taxable"]), ("CA", False))
			self.assertEqual(cfg["us_states"]["TEXAS"], "TX")
		finally:
			frappe.delete_doc("Address", addr.name, force=True, ignore_permissions=True)
			for name in primaries:
				frappe.db.set_value("Address", name, "is_primary_address", 1)
			frappe.local.request_cache.clear()


class InterstateTaxOnDocs(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls._ensure_tax_account_and_default_template()
		from ffl_core.setup import tax as tax_setup
		tax_setup.ensure()
		cls.default_template = f"Texas Sales Tax - {cls.abbr}"
		cls.oos_template = f"Out of State Sales - {cls.abbr}"
		cls._ensure_customer("_Test Interstate Buyer")
		if not frappe.db.exists("Item", "ITX-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "ITX-ACC", "item_name": "ITX-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_tax_account_and_default_template(cls) -> None:
		acct_name = frappe.db.get_value(
			"Account", {"company": cls.company, "account_name": "ITX Sales Tax"}, "name")
		if not acct_name:
			parent = frappe.db.get_value(
				"Account", {"company": cls.company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft")
			acct = frappe.get_doc({
				"doctype": "Account", "account_name": "ITX Sales Tax",
				"company": cls.company, "parent_account": parent,
				"account_type": "Tax",
			})
			acct.insert(ignore_permissions=True)
			acct_name = acct.name
		cls.tax_account = acct_name
		if not frappe.db.exists("Sales Taxes and Charges Template",
				f"Texas Sales Tax - {cls.abbr}"):
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": "Texas Sales Tax", "company": cls.company, "is_default": 1,
				"taxes": [{"charge_type": "On Net Total", "account_head": acct_name,
					"rate": 8.25, "description": "TX Sales Tax"}],
			}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_customer(cls, name: str) -> None:
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

	def _address(self, title: str, state: str, country: str = "United States") -> str:
		name = frappe.db.get_value("Address", {"address_title": title}, "name")
		if name:
			frappe.db.set_value("Address", name, {"state": state, "country": country},
				update_modified=False)
			return name
		doc = frappe.get_doc({
			"doctype": "Address", "address_title": title, "address_type": "Shipping",
			"address_line1": "1 Test St", "city": "Testville",
			"state": state, "country": country,
			"links": [{"link_doctype": "Customer",
				"link_name": "_Test Interstate Buyer"}],
		})
		doc.insert(ignore_permissions=True)
		return doc.name

	def _so(self, shipping_address: str | None) -> "frappe.Document":
		"""Build the SO the way production reaches the hook: ERPNext's
		set_missing_values pre-fills the company default template before
		validate (it short-circuits under frappe.in_test, so set it
		explicitly here)."""
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = "_Test Interstate Buyer"
		so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		so.taxes_and_charges = self.default_template
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		so.set("taxes", get_taxes_and_charges(
			"Sales Taxes and Charges Template", self.default_template))
		so.append("items", {"item_code": "ITX-ACC", "qty": 1, "rate": 100})
		if shipping_address:
			so.shipping_address_name = shipping_address
		so.flags.ignore_permissions = True
		so.insert()
		return so

	def test_out_of_state_shipping_zeroes_tax(self):
		so = self._so(self._address("ITX-CA", "California"))
		self.assertEqual(so.taxes_and_charges, self.oos_template)
		self.assertEqual(float(so.total_taxes_and_charges or 0), 0.0)

	def test_out_of_state_usps_code_also_exempt(self):
		so = self._so(self._address("ITX-OK", "OK"))
		self.assertEqual(so.taxes_and_charges, self.oos_template)

	def test_texas_full_name_keeps_default_tax(self):
		so = self._so(self._address("ITX-TXFULL", "Texas"))
		self.assertEqual(so.taxes_and_charges, self.default_template)
		self.assertGreater(float(so.total_taxes_and_charges or 0), 0.0)

	def test_texas_abbr_keeps_default_tax(self):
		so = self._so(self._address("ITX-TXABBR", "tx"))
		self.assertEqual(so.taxes_and_charges, self.default_template)

	def test_no_shipping_address_keeps_default_tax(self):
		so = self._so(None)
		self.assertEqual(so.taxes_and_charges, self.default_template)
		self.assertGreater(float(so.total_taxes_and_charges or 0), 0.0)

	def test_non_us_address_keeps_default_tax(self):
		so = self._so(self._address("ITX-ON", "Ontario", country="Canada"))
		self.assertEqual(so.taxes_and_charges, self.default_template)

	def test_manually_set_exempt_template_reverts_on_texas_doc(self):
		"""Staff picking the 0% template on a TX/no-address doc gets corrected."""
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = "_Test Interstate Buyer"
		so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 7)
		so.taxes_and_charges = self.oos_template
		so.append("items", {"item_code": "ITX-ACC", "qty": 1, "rate": 100})
		so.flags.ignore_permissions = True
		so.insert()
		self.assertEqual(so.taxes_and_charges, self.default_template)
		self.assertGreater(float(so.total_taxes_and_charges or 0), 0.0)

	def test_pos_guard_blocks_exempt_template_without_oos_shipping(self):
		from ffl_core.compliance.interstate_tax import guard_pos_exemption
		from types import SimpleNamespace
		doc = SimpleNamespace(
			doctype="POS Invoice", company=self.company, is_return=0,
			taxes_and_charges=self.oos_template, shipping_address_name=None,
		)
		with self.assertRaises(frappe.ValidationError):
			guard_pos_exemption(doc)

	def test_pos_guard_allows_default_template(self):
		from ffl_core.compliance.interstate_tax import guard_pos_exemption
		from types import SimpleNamespace
		doc = SimpleNamespace(
			doctype="POS Invoice", company=self.company, is_return=0,
			taxes_and_charges=self.default_template, shipping_address_name=None,
		)
		guard_pos_exemption(doc)  # no raise


if __name__ == "__main__":
	unittest.main()


class StateListAtBoot(FrappeTestCase):

	def test_the_address_forms_get_every_code_in_name_order(self):
		boot = frappe._dict()
		interstate_tax.extend_bootinfo(boot)
		self.assertEqual(boot.ffl_us_states[:4], ["AL", "AK", "AZ", "AR"])
		self.assertEqual(set(boot.ffl_us_states), interstate_tax.US_CODES)
