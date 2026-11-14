"""The Marketing Contact buttons (ffl_core.api.marketing): who may press them, the reason Resubscribe
needs, and what they leave on the log; plus the form's wiring the buttons rely on."""
from __future__ import annotations

import os

import frappe

from ffl_core.api import marketing as api
from ffl_core.marketing import CONTACT, contacts, tags
from ffl_core.marketing.contacts import BOUNCED, COMPLAINED, NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, set_status
from ffl_core.tests.crm_support import user
from ffl_core.tests.marketing_support import MarketingTestCase, address, events_of, status_of

MANAGER = "mkt-ui-manager@example.com"
CLERK = "mkt-ui-clerk@example.com"
READER = "mkt-ui-reader@example.com"


def _contact(status: str = SUBSCRIBED) -> str:
	email = address("ui")
	contacts.get_or_create(email)
	if status != NOT_SUBSCRIBED:
		set_status(email, status, channel="footer")
	return email


class TestUnsubscribe(MarketingTestCase):
	def test_a_manager_unsubscribes_with_a_reason_and_it_is_logged_with_who(self):
		user(MANAGER, "Sales Manager")
		email = _contact()
		frappe.set_user(MANAGER)
		out = api.unsubscribe(email, "Asked at the counter")
		frappe.set_user("Administrator")
		self.assertEqual(out, {"contact": email, "status": UNSUBSCRIBED, "changed": True})
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.status, doc.status_reason), (UNSUBSCRIBED, "Asked at the counter"))
		last = events_of(email)[-1]
		self.assertEqual(last.kind, UNSUBSCRIBED)
		for part in ("Asked at the counter", MANAGER):
			self.assertIn(part, last.detail)
		self.assertTrue(frappe.db.exists("Email Unsubscribe", {"email": email, "reference_name": email, "global_unsubscribe": 0}))

	def test_the_reason_is_optional_and_a_not_subscribed_contact_can_be_unsubscribed(self):
		email = _contact(NOT_SUBSCRIBED)
		self.assertEqual(api.unsubscribe(email)["status"], UNSUBSCRIBED)

	def test_a_contact_already_bounced_or_complained_is_left_as_it_is(self):
		for status in (BOUNCED, COMPLAINED):
			email = _contact(status)
			self.assertEqual(api.unsubscribe(email, "x"), {"contact": email, "status": status, "changed": False})
			self.assertEqual(status_of(email), status)

	def test_a_sales_user_cannot_press_it(self):
		user(CLERK, "Sales User")
		email = _contact()
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			api.unsubscribe(email)
		frappe.set_user("Administrator")
		self.assertEqual(status_of(email), SUBSCRIBED)

	def test_a_user_with_no_sales_role_cannot_press_it(self):
		user(READER, "Accounts User")
		email = _contact()
		frappe.set_user(READER)
		with self.assertRaises(frappe.PermissionError):
			api.unsubscribe(email)

	def test_a_contact_that_does_not_exist_is_refused_not_made(self):
		email = address("ui")
		with self.assertRaises(frappe.DoesNotExistError):
			api.unsubscribe(email)
		self.assertFalse(frappe.db.exists(CONTACT, email))


class TestResubscribe(MarketingTestCase):
	def test_a_manager_with_a_reason_puts_a_suppressed_contact_back(self):
		user(MANAGER, "Sales Manager")
		for status in (UNSUBSCRIBED, BOUNCED, COMPLAINED):
			email = _contact(status)
			frappe.set_user(MANAGER)
			out = api.resubscribe(email, "Wrote to say sorry")
			frappe.set_user("Administrator")
			self.assertEqual(out, {"contact": email, "status": SUBSCRIBED})
			last = events_of(email)[-1]
			self.assertEqual(last.kind, SUBSCRIBED)
			self.assertIn("Wrote to say sorry", last.detail)
			self.assertIn(MANAGER, last.detail)
			self.assertFalse(frappe.db.exists("Email Unsubscribe", {"email": email, "reference_name": email}))

	def test_the_reason_is_required(self):
		email = _contact(UNSUBSCRIBED)
		for reason in (None, "", "   "):
			with self.assertRaises(frappe.MandatoryError):
				api.resubscribe(email, reason)
		self.assertEqual(status_of(email), UNSUBSCRIBED)

	def test_only_a_suppressed_contact_can_be_resubscribed(self):
		for status in (NOT_SUBSCRIBED, SUBSCRIBED):
			email = _contact(status)
			with self.assertRaises(frappe.ValidationError):
				api.resubscribe(email, "because")
			self.assertEqual(status_of(email), status)

	def test_a_sales_user_cannot_press_it(self):
		user(CLERK, "Sales User")
		email = _contact(UNSUBSCRIBED)
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			api.resubscribe(email, "because")
		frappe.set_user("Administrator")
		self.assertEqual(status_of(email), UNSUBSCRIBED)

	def test_a_contact_that_does_not_exist_is_refused(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.resubscribe(address("ui"), "because")


def _customer(email: str):
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test Mkt {frappe.generate_hash(length=6)}",
		"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories", "ffl_email": email})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


class TestCustomerCard(MarketingTestCase):
	"""The Marketing line on the Customer form."""

	def _linked_customer(self, status: str = SUBSCRIBED):
		email = address("card")
		customer = _customer(email)
		contacts.get_or_create(email)
		set_status(email, status, channel="pos")
		tags.add(email, "in-store", "new-arrivals")
		return customer.name, email

	def test_a_customer_with_a_contact_gets_the_line_the_deal_card_shows(self):
		from ffl_core.crm.customer import _marketing_html

		name, email = self._linked_customer()
		card = api.customer_card(name)
		self.assertEqual(card, {"status": SUBSCRIBED, "tags": ["in-store", "new-arrivals"], "contact": email,
			"text": "Marketing email: Subscribed · in-store, new-arrivals"})
		self.assertIn(card["text"], _marketing_html({"status": card["status"], "tags": card["tags"]}), "one wording")

	def test_a_customer_with_no_contact_or_no_address_gets_nothing(self):
		self.assertIsNone(api.customer_card(_customer(address("card")).name))
		doc = frappe.get_doc({"doctype": "Customer", "customer_name": "_Test Mkt No Mail", "customer_type": "Individual",
			"customer_group": "Individual", "territory": "All Territories"})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertIsNone(api.customer_card(doc.name))
		self.assertIsNone(api.customer_card("no such customer"))

	def test_nothing_while_marketing_is_off(self):
		name, _email = self._linked_customer()
		self.set_single("Marketing Settings", "enabled", 0)
		self.assertIsNone(api.customer_card(name))

	def test_a_role_that_cannot_read_marketing_contacts_gets_nothing(self):
		name, _email = self._linked_customer()
		user(READER, "Accounts User")
		frappe.set_user(READER)
		self.assertFalse(frappe.has_permission(CONTACT, "read"))
		self.assertIsNone(api.customer_card(name))

	def test_a_sales_user_may_read_it_and_a_suppressed_status_shows(self):
		name, email = self._linked_customer(UNSUBSCRIBED)
		user(CLERK, "Sales User")
		frappe.set_user(CLERK)
		card = api.customer_card(name)
		self.assertEqual((card["status"], card["contact"]), (UNSUBSCRIBED, email))

	def test_the_customer_form_asks_for_it(self):
		path = frappe.get_app_path("ffl_core", "public", "js", "customer_form.js")
		with open(path, encoding="utf-8") as fh:
			script = fh.read()
		self.assertIn("ffl_core.api.marketing.customer_card", script)
		self.assertIn("fflRenderMarketing(frm);", script)
		self.assertIn("escape_html(card.text)", script)


class TestEndpoints(MarketingTestCase):
	def test_the_two_buttons_are_post_only_whitelisted_endpoints(self):
		for fn in (api.unsubscribe, api.resubscribe):
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
		self.assertIn(api.customer_card, frappe.whitelisted)

	def test_the_status_field_is_read_only_so_only_the_buttons_change_it(self):
		field = frappe.get_meta(CONTACT).get_field("status")
		self.assertEqual(field.read_only, 1)
		email = _contact()
		doc = frappe.get_doc(CONTACT, email)
		doc.status = UNSUBSCRIBED
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)


class TestDeskFiles(MarketingTestCase):
	"""The Desk scripts are plain files next to the DocTypes; a rename or a typo is caught here, not in a browser."""

	def _file(self, doctype: str, suffix: str) -> str:
		folder = frappe.scrub(doctype)
		path = frappe.get_app_path("ffl_core", "ffl_core", "doctype", folder, f"{folder}{suffix}")
		self.assertTrue(os.path.exists(path), path)
		with open(path, encoding="utf-8") as fh:
			return fh.read()

	def test_the_form_calls_the_two_endpoints_and_lists_the_log_with_a_limit(self):
		script = self._file("Marketing Contact", ".js")
		for method in ("ffl_core.api.marketing.unsubscribe", "ffl_core.api.marketing.resubscribe"):
			self.assertIn(method, script)
		self.assertIn("Marketing Event", script)
		self.assertIn("Marketing Request", script)
		self.assertIn("EVENT_LIMIT", script)
		self.assertIn("escape_html", script)

	def test_the_list_defaults_to_subscribed(self):
		script = self._file("Marketing Contact", "_list.js")
		self.assertIn("['status', '=', 'Subscribed']", script)

	def test_the_settings_page_names_the_sender_the_mail_unit_provides(self):
		script = self._file("Marketing Settings", ".js")
		self.assertIn("ffl_core.api.marketing_mail.send_test_email", script)
		self.assertIn("Failed to get method", script, "a server without the sender is told plainly")

	def test_the_filters_the_list_offers(self):
		meta = frappe.get_meta(CONTACT)
		for fieldname in ("status", "source", "customer"):
			self.assertEqual(meta.get_field(fieldname).in_standard_filter, 1, fieldname)


class TestWiring(MarketingTestCase):
	def test_the_customer_and_contact_hooks_are_in_the_lists_they_share(self):
		events = frappe.get_hooks("doc_events")
		self.assertIn("ffl_core.customer_contact.contact_changed", events["Contact"]["on_update"])
		self.assertIn("ffl_core.marketing.customers.contact_saved", events["Contact"]["on_update"])
		self.assertIn("ffl_core.customer_profile.privatize_id_attachments", events["Customer"]["on_update"])
		self.assertIn("ffl_core.marketing.customers.customer_saved", events["Customer"]["on_update"])
		self.assertIn("ffl_core.customer_profile.refuse_delete_with_family", events["Customer"]["on_trash"])
		self.assertIn("ffl_core.marketing.customers.customer_trashed", events["Customer"]["on_trash"])
		self.assertEqual(events["Customer"]["after_rename"], ["ffl_core.marketing.customers.customer_renamed"])

	def test_the_sidebar_link_follows_the_module_switch(self):
		from ffl_core.setup import gunshop_sidebar

		self.assertNotIn("Marketing Contact", gunshop_sidebar.integrations_off())
		self.set_single("Marketing Settings", "enabled", 0)
		self.assertIn("Marketing Contact", gunshop_sidebar.integrations_off())
		self.assertIn("ffl_core.setup.gunshop_sidebar.on_settings_update",
			frappe.get_hooks("doc_events")["Marketing Settings"]["on_update"])

	def test_the_settings_workspace_links_marketing_settings(self):
		import json

		path = frappe.get_app_path("ffl_core", "ffl_core", "workspace", "settings", "settings.json")
		with open(path, encoding="utf-8") as fh:
			links = [l.get("link_to") for l in json.load(fh)["links"]]
		self.assertIn("Marketing Settings", links)
		self.assertTrue(frappe.db.exists("Workspace Link", {"parent": "Settings", "link_to": "Marketing Settings"}),
			"the migrate or the patch put it on a site that already had the workspace")
