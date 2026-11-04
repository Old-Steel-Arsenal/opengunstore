"""The Email button on a CRM Contact page: the contact's open deal, made once; the endpoint's role and
Contact read checks; the button itself (a CRM Form Script)."""
from __future__ import annotations

import frappe

from ffl_core.api import crm as api
from ffl_core.crm import setup
from ffl_core.tests.crm_support import CRMTestCase, user


def _contact(email: str = "walkin@buyer.test") -> str:
	return frappe.get_doc({"doctype": "Contact", "first_name": "Walk", "last_name": "In",
		"email_ids": [{"email_id": email, "is_primary": 1}]}).insert(ignore_permissions=True).name


class TestContactEmail(CRMTestCase):
	def test_email_opens_one_deal_and_reuses_it(self):
		contact = _contact()
		deal = api.open_deal(contact)
		doc = frappe.get_doc("CRM Deal", deal)
		self.assertEqual([(c.contact, c.is_primary) for c in doc.contacts], [(contact, 1)])
		self.assertEqual(api.open_deal(contact), deal)

	def test_a_closed_deal_is_not_reused(self):
		contact = _contact("closed@buyer.test")
		deal = api.open_deal(contact)
		lost = frappe.get_all("CRM Deal Status", filters={"type": "Lost"}, pluck="name", limit=1)[0]
		frappe.db.set_value("CRM Deal", deal, "status", lost)
		self.assertNotEqual(api.open_deal(contact), deal)

	def test_a_contact_without_email_gets_no_deal(self):
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Phone", "last_name": "Only",
			"phone_nos": [{"phone": "5550100", "is_primary_phone": 1}]}).insert(ignore_permissions=True).name
		with self.assertRaisesRegex(frappe.ValidationError, "no email address"):
			api.open_deal(contact)
		self.assertFalse(frappe.get_all("CRM Contacts", filters={"parenttype": "CRM Deal", "contact": contact}))

	def test_a_contact_the_user_cannot_see_gets_no_deal(self):
		mine, theirs = _contact("mine@buyer.test"), _contact("theirs@buyer.test")
		seller = user("crm-seller@example.com", "Sales User")
		# The seller may see one contact only.
		frappe.get_doc({"doctype": "User Permission", "user": seller, "allow": "Contact", "for_value": mine,
			"apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.set_user(seller)
		try:
			with self.assertRaises(frappe.PermissionError):
				api.open_deal(theirs)
			self.assertTrue(api.open_deal(mine))
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(frappe.get_all("CRM Contacts", filters={"parenttype": "CRM Deal", "contact": theirs}))

	def test_a_user_without_a_crm_role_is_refused(self):
		contact = _contact("norole@buyer.test")
		stock = user("crm-stock@example.com", "Stock User")
		frappe.set_user(stock)
		try:
			with self.assertRaises(frappe.PermissionError):
				api.open_deal(contact)
		finally:
			frappe.set_user("Administrator")

	def test_an_unknown_contact_is_refused(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.open_deal("_T-NO-SUCH-CONTACT")

	def test_the_button_is_a_crm_form_script(self):
		frappe.db.delete("CRM Form Script", {"name": setup.CONTACT_SCRIPT})
		setup.ensure_contact_script()
		setup.ensure_contact_script()
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.CONTACT_SCRIPT, ["dt", "view", "enabled"]),
			("Contact", "Form", 1))
		self.assertIn("ffl_core.api.crm.open_deal", frappe.db.get_value("CRM Form Script", setup.CONTACT_SCRIPT, "script"))
		frappe.db.set_value("CRM Form Script", setup.CONTACT_SCRIPT, "enabled", 0)
		setup.ensure_contact_script()
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.CONTACT_SCRIPT, "enabled"), 1)
