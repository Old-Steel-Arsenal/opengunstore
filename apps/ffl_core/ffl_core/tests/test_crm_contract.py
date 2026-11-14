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
		# The two gates hooks.py overrides: the Quotations tab and the Create Quotation button.
		module = inspect.getmodule(type(settings))
		for name in ("can_view_quotations", "can_create_quotations"):
			self.assertTrue(callable(getattr(module, name, None)), name)
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
		self.assertTrue({"dt", "filters", "kanban_columns", "public", "pinned", "route_name", "user"} <= _fields("CRM View Settings"))
		self.assertTrue({"title", "content", "reference_doctype", "reference_docname"} <= _fields("FCRM Note"))
		self.assertTrue({"filters", "default_filters", "doctype"} <= set(inspect.signature(__import__("crm.api.doc", fromlist=["get_data"]).get_data).parameters))
		self.assertIn("email", _fields("CRM Lead"))
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

	def test_a_deal_fetches_a_rate_only_when_its_currency_is_not_crms(self):
		# crm.setup.ensure_crm_currency keeps the two equal so that no deal depends on a public API.
		from crm.fcrm.doctype.crm_deal.crm_deal import CRMDeal

		source = inspect.getsource(CRMDeal.update_exchange_rate)
		self.assertIn('"FCRM Settings", "currency"', source)
		self.assertIn("self.currency != system_currency", source)

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

	def test_contact_add_phone_signature(self):
		# crm.customer._keep_phone puts a new customer's phone on the deal's Contact.
		from frappe.contacts.doctype.contact.contact import Contact

		self.assertTrue({"phone", "is_primary_mobile_no"} <= set(inspect.signature(Contact.add_phone).parameters))

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


class TestMarketingMailContract(FrappeTestCase):
	"""The Frappe mail internals ffl_core.marketing.mailer relies on. An upgrade that changes one fails here
	instead of silently sending List-Unsubscribe as X-List-Unsubscribe, letting a suppressed contact's mail
	through, stopping a customer's receipts, or sending marketing mail from the store's transactional account."""

	MARKETING_ACCOUNT, MARKETING_ADDRESS = "_T Contract Marketing", "contract-news@store.test"
	DEFAULT_ACCOUNT, DEFAULT_ADDRESS = "_T Contract Default", "contract-receipts@store.test"

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("mkt_contract")
		frappe.local.outgoing_email_account = {}

	def tearDown(self):
		frappe.db.rollback(save_point="mkt_contract")
		frappe.local.outgoing_email_account = {}

	def test_custom_headers_get_an_x_prefix(self):
		# Why mailer.restore_list_unsubscribe renames the two headers on the queued row.
		from frappe.email.email_body import EMail

		mail = EMail(sender="a@store.test", recipients=["b@example.com"], subject="x",
			email_account=frappe.new_doc("Email Account"))
		mail.add_headers({"List-Unsubscribe": "<https://pos/unsubscribe>", "X-Kept": "1"})
		self.assertEqual(mail.msg_root["X-List-Unsubscribe"], "<https://pos/unsubscribe>")
		self.assertIsNone(mail.msg_root["List-Unsubscribe"])
		self.assertEqual(mail.msg_root["X-Kept"], "1")
		self.assertIn("message", _fields("Email Queue"), "the queued MIME text the hook rewrites")

	def test_a_mails_text_part_comes_only_from_a_templates_txt_file(self):
		# Why mailer.send passes template="marketing" (templates/emails/marketing.{html,txt}): sendmail has
		# no text argument of its own, and the pair must hand HTML and text through unchanged.
		import inspect

		from frappe.utils.jinja import get_email_from_template

		parameters = inspect.signature(frappe.sendmail).parameters
		self.assertIn("template", parameters)
		self.assertNotIn("text_content", parameters)
		html, text = "<p>S&W <b>sale</b></p>", "S&W sale\nUnsubscribe: https://pos.test/unsubscribe?t=a.b"
		self.assertEqual(get_email_from_template("marketing", {"html": html, "text": text}), (html, text))

	def test_the_queue_drops_reference_level_and_global_unsubscribes_only(self):
		from frappe.email.doctype.email_queue.email_queue import QueueBuilder

		from ffl_core.marketing import CONTACT
		from ffl_core.marketing.contacts import get_or_create

		same, other_ref, global_, kept, someone = (f"contract-{k}-{frappe.generate_hash(length=6)}@example.com"
			for k in ("same", "other", "global", "kept", "someone"))
		for email in (same, someone):
			get_or_create(email)   # the reference is a Dynamic Link
		for email, ref_name, is_global in ((same, same, 0), (other_ref, someone, 0), (global_, None, 1)):
			frappe.get_doc({"doctype": "Email Unsubscribe", "email": email, "reference_doctype": None if is_global else CONTACT,
				"reference_name": ref_name, "global_unsubscribe": is_global}).insert(ignore_permissions=True)
		# Every address in one builder whose reference is the first one's: same reference + global drop.
		builder = QueueBuilder(recipients=[same, other_ref, global_, kept], reference_doctype=CONTACT, reference_name=same)
		self.assertEqual(set(builder.get_unsubscribed_user_emails()), {same, global_})
		# Another reference: only the global one.
		builder = QueueBuilder(recipients=[same, global_, kept], reference_doctype="User", reference_name="Administrator")
		self.assertEqual(set(builder.get_unsubscribed_user_emails()), {global_})

	def test_the_sending_account_is_matched_by_the_sender_address(self):
		from frappe.email.doctype.email_account.email_account import EmailAccount

		for name, address, default in ((self.MARKETING_ACCOUNT, self.MARKETING_ADDRESS, 0),
				(self.DEFAULT_ACCOUNT, self.DEFAULT_ADDRESS, 1)):
			if not frappe.db.exists("Email Account", name):
				frappe.get_doc({"doctype": "Email Account", "email_account_name": name, "email_id": address,
					"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(ignore_permissions=True)
			frappe.db.set_value("Email Account", name, {"enable_outgoing": 1, "default_outgoing": default,
				"smtp_server": "smtp.invalid", "smtp_port": 587}, update_modified=False)
		frappe.db.set_value("Email Account", {"default_outgoing": 1, "name": ["!=", self.DEFAULT_ACCOUNT]},
			"default_outgoing", 0, update_modified=False)
		found = EmailAccount.find_outgoing(match_by_email=f"Store <{self.MARKETING_ADDRESS}>")
		self.assertEqual(found.name, self.MARKETING_ACCOUNT)
		frappe.local.outgoing_email_account = {}
		self.assertEqual(EmailAccount.find_outgoing(match_by_email=None).name, self.DEFAULT_ACCOUNT)
		# A matching account that does not send falls back to the default without a word: why the mailer
		# checks Enable Outgoing itself.
		frappe.db.set_value("Email Account", self.MARKETING_ACCOUNT, "enable_outgoing", 0, update_modified=False)
		frappe.local.outgoing_email_account = {}
		self.assertEqual(EmailAccount.find_outgoing(match_by_email=self.MARKETING_ADDRESS).name, self.DEFAULT_ACCOUNT)
