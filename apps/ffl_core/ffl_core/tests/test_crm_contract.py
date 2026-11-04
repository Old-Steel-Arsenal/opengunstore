"""The Frappe CRM internals ffl_core.crm relies on, by name. A CRM upgrade that renames one of these
fails here instead of silently turning the CRM wiring into a no-op."""
from __future__ import annotations

import inspect

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.crm import installed, setup
from ffl_core.tests.crm_support import CRMTestCase


def _fields(doctype: str) -> set[str]:
	return {f.fieldname for f in frappe.get_meta(doctype).fields}


class TestCRMContract(CRMTestCase):
	def test_crm_is_installed(self):
		self.assertTrue(installed(), "Frappe CRM ships with the POS: every site has it")
		self.assertIn("crm", frappe.get_hooks("required_apps", app_name="ffl_core"))

	def test_crm_item_hooks_honour_the_skip_flag(self):
		from crm.integrations.erpnext.item import _should_sync

		doc = frappe.new_doc("Item")
		self.assertTrue(_should_sync(doc), "same-site integration on: CRM syncs an Item save")
		doc.flags.ignore_crm_sync = True
		self.assertFalse(_should_sync(doc))

	def test_crm_still_registers_its_item_hooks(self):
		# We skip them per save and rely on their delete / rename cascade.
		events = frappe.get_hooks("doc_events").get("Item", {})
		for event, handler in (("after_insert", "crm.integrations.erpnext.item.after_insert"),
				("on_update", "crm.integrations.erpnext.item.on_update"),
				("on_trash", "crm.integrations.erpnext.item.on_trash"),
				("after_rename", "crm.integrations.erpnext.item.after_rename")):
			self.assertIn(handler, events.get(event, []), event)

	def test_crm_settings_fields_and_setup_methods(self):
		self.assertTrue({"enabled", "sync_products", "is_erpnext_in_different_site", "erpnext_company"}
			<= _fields("ERPNext CRM Settings"))
		settings = frappe.get_single("ERPNext CRM Settings")
		for name in ("create_custom_fields", "add_quotation_to_option", "create_crm_form_script"):
			self.assertTrue(callable(getattr(settings, name, None)), name)
		# Its save grants Sales User / Manager write on Item: the reason setup writes the fields.
		self.assertIn("grant_item_access_to_sales_roles", inspect.getsource(type(settings).validate))

	def test_crm_product_push_reads_sync_products(self):
		from crm.integrations.erpnext.utils import in_cascade, should_push_to_erpnext

		self.assertFalse(should_push_to_erpnext(), "sync_products is off: CRM pushes no product to Item")
		self.assertFalse(in_cascade())

	def test_product_fields(self):
		self.assertTrue({"erpnext_item_code", "standard_rate", "product_code", "product_name", "description", "image",
			"disabled"} <= _fields("CRM Product"))
		self.assertEqual(frappe.get_meta("CRM Product").autoname, "field:product_code")
		self.assertIn("crm_product_code", _fields("Item"))

	def test_lines_leads_deals_mail(self):
		self.assertTrue({"product_code", "product_name", "rate", "qty", "amount", "discount_percentage",
			"discount_amount", "net_amount", "ffl_serial_no"} <= _fields("CRM Products"))
		self.assertTrue({"products", "total", "net_total"} <= _fields("CRM Deal"))
		self.assertTrue({"email", "first_name", "last_name", "source"} <= _fields("CRM Lead"))
		self.assertTrue({"create_lead_from_incoming_email", "ffl_auto_lead"} <= _fields("Email Account"))
		self.assertTrue({"dt", "view", "script", "enabled"} <= _fields("CRM Form Script"))

	def test_crm_lead_from_mail_is_keyed_on_the_email_account_switch(self):
		from crm.utils import create_lead_from_incoming_email

		self.assertIn("create_lead_from_incoming_email", inspect.getsource(create_lead_from_incoming_email))


class TestCRMSettingsWrite(FrappeTestCase):
	def test_setup_never_grants_item_rights_to_the_counter(self):
		frappe.set_user("Administrator")
		setup.ensure_crm_settings()
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Item", "role": "Sales User", "write": 1}))
		values = frappe.db.get_value("ERPNext CRM Settings", None,
			["enabled", "is_erpnext_in_different_site", "sync_products", "erpnext_company"], as_dict=True)
		self.assertEqual((values.enabled, values.is_erpnext_in_different_site, values.sync_products), (1, 0, 0))
		self.assertTrue(values.erpnext_company)
