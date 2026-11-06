"""The Frappe CRM internals ffl_core.crm relies on, by name. A CRM upgrade that renames one of these
fails here instead of silently turning the CRM wiring into a no-op."""
from __future__ import annotations

import inspect
import os

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

	def test_deal_fields_the_pos_link_reads_and_writes(self):
		self.assertTrue({"erpnext_customer", "deal_value", "net_total", "status", "closed_date", "status_change_log",
			"contacts", "deal_owner", "email", "mobile_no", "phone", "first_name", "last_name", "lead_name",
			"organization"} <= _fields("CRM Deal"))
		self.assertTrue({"contact", "is_primary"} <= _fields("CRM Contacts"))
		self.assertEqual(frappe.get_meta("CRM Contacts").get_field("contact").fieldtype, "Link")
		self.assertTrue({"type", "position"} <= _fields("CRM Deal Status"))
		self.assertTrue({"title", "description", "priority", "status", "assigned_to", "reference_doctype",
			"reference_docname"} <= _fields("CRM Task"))
		options = frappe.get_meta("CRM Task").get_field("status").options
		self.assertIn("Todo", options)
		self.assertIn("High", frappe.get_meta("CRM Task").get_field("priority").options)

	def test_crm_sets_closed_date_and_logs_a_status_change_in_validate(self):
		from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal

		source = inspect.getsource(CRMDeal.validate)
		self.assertIn("add_status_change_log", source)
		self.assertIn("closed_date", source)

	def test_the_deal_side_panel_is_a_list_of_sections(self):
		# crm.setup adds the customer card's section the way CRM adds its Lost Reason one.
		from crm.fcrm.doctype.utils import add_or_remove_lost_reason_section_in_sidepanel

		source = inspect.getsource(add_or_remove_lost_reason_section_in_sidepanel)
		self.assertIn('f"{doctype}-Side Panel"', source)
		self.assertIn("contacts_section", source)
		self.assertEqual(frappe.get_meta("CRM Fields Layout").autoname, "format:{dt}-{type}")
		self.assertEqual(setup.SIDE_PANEL, "CRM Deal-Side Panel")

	def test_frappe_assign_takes_ignore_permissions(self):
		# crm.orders assigns the owner's task for a user with no CRM rights; CRM's own after_insert
		# path checks the session user.
		from crm.fcrm.doctype.crm_task.crm_task import CRMTask
		from frappe.desk.form.assign_to import _add

		self.assertIn("ignore_permissions", inspect.signature(_add).parameters)
		self.assertIn("assign", inspect.getsource(CRMTask.assign_to))

	def test_the_frontend_renders_what_the_deal_script_sends(self):
		# setFieldHtml fills an HTML field on the side panel (DOMPurify) and createDialog takes `html`.
		root = frappe.get_app_path("crm", "..", "frontend", "src")
		if not os.path.isdir(root):
			self.skipTest("a release build without the frontend's source")
		reads = {
			"data/script.js": "setFieldHtml",
			"components/SidePanelLayout.vue": "fieldHtmlMap",
			"components/Controls/HtmlControl.vue": "sanitizeHTML",
			"utils/dialogs.jsx": "dialog.html",
		}
		for path, needle in reads.items():
			with open(os.path.join(root, path)) as handle:
				self.assertIn(needle, handle.read(), path)

	def test_crm_lead_from_mail_is_keyed_on_the_email_account_switch(self):
		from crm.utils import create_lead_from_incoming_email

		self.assertIn("create_lead_from_incoming_email", inspect.getsource(create_lead_from_incoming_email))


	def test_frappe_mail_pull_internals_the_skip_relies_on(self):
		# crm.mail refuses a mail by raising SentEmailInInboxError from Communication before_insert
		# during the pull, and remembers its uid after the pull's rollback (see crm.mail).
		from frappe.database.database import Database
		from frappe.email.doctype.email_account.email_account import EmailAccount, get_max_email_uid
		from frappe.email.receive import InboundMail

		receive = inspect.getsource(EmailAccount.receive)
		handler = receive.split("except SentEmailInInboxError:", 1)[1].split("except", 1)[0]
		self.assertIn("frappe.db.rollback()", handler)
		self.assertNotIn("log_error", handler)
		self.assertIn("flags.in_receive = True", inspect.getsource(InboundMail._build_communication_doc))
		self.assertIn("after_rollback.run()", inspect.getsource(Database.rollback))
		max_uid = inspect.getsource(get_max_email_uid)
		self.assertIn('"email_account": email_account', max_uid)
		self.assertNotIn("communication_type", max_uid, "the marker is an Automated Message")
		self.assertTrue({"add_reply_to_header", "reply_to_addresses", "connected_user", "connected_app",
			"email_sync_option"} <= _fields("Email Account"))


class TestCRMSettingsWrite(FrappeTestCase):
	def test_setup_never_grants_item_rights_to_the_counter(self):
		frappe.set_user("Administrator")
		setup.ensure_crm_settings()
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "Item", "role": "Sales User", "write": 1}))
		values = frappe.db.get_value("ERPNext CRM Settings", None,
			["enabled", "is_erpnext_in_different_site", "sync_products", "erpnext_company"], as_dict=True)
		self.assertEqual((values.enabled, values.is_erpnext_in_different_site, values.sync_products), (1, 0, 0))
		self.assertTrue(values.erpnext_company)
