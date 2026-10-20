"""The receipt email typed on a counter order is saved onto a buyer who has
none on file, so the next order fills it in."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order


class RememberCustomerEmailTest(FrappeTestCase):
	def _customer(self, email=None):
		if not frappe.db.exists("Customer Group", "Individual"):
			frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "Individual",
				"is_group": 0}).insert(ignore_permissions=True)
		name = "Remember Email " + frappe.generate_hash(length=6)
		cust = frappe.get_doc({"doctype": "Customer", "customer_name": name,
			"customer_type": "Individual",
			"customer_group": "Individual"}).insert(ignore_permissions=True)
		if email:
			contact = frappe.get_doc({"doctype": "Contact", "first_name": name,
				"email_ids": [{"email_id": email, "is_primary": 1}],
				"links": [{"link_doctype": "Customer", "link_name": cust.name}]}).insert(ignore_permissions=True)
			frappe.db.set_value("Customer", cust.name,
				{"email_id": email, "customer_primary_contact": contact.name})
		return cust.name

	def _email(self, customer):
		return frappe.db.get_value("Customer", customer, "email_id")

	def test_buyer_without_email_gets_it(self):
		cust = self._customer()
		manual_order._remember_customer_email(cust, "buyer@example.com")
		self.assertEqual(self._email(cust), "buyer@example.com")
		contact = frappe.get_doc("Contact",
			frappe.db.get_value("Customer", cust, "customer_primary_contact"))
		self.assertEqual(contact.email_id, "buyer@example.com")
		# The next order's lookup (the page reads Customer.email_id) finds it.
		manual_order._remember_customer_email(cust, "buyer@example.com")
		contact.reload()
		self.assertEqual(len(contact.email_ids), 1)

	def test_existing_contact_without_email_gets_it_as_primary(self):
		cust = self._customer()
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "X",
			"phone_nos": [{"phone": "5551234567", "is_primary_phone": 1}],
			"links": [{"link_doctype": "Customer", "link_name": cust}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust, "customer_primary_contact", contact.name)
		manual_order._remember_customer_email(cust, "second@example.com")
		contact.reload()
		self.assertEqual(contact.email_id, "second@example.com")
		self.assertEqual(self._email(cust), "second@example.com")

	def test_existing_email_is_not_overwritten(self):
		cust = self._customer("onfile@example.com")
		manual_order._remember_customer_email(cust, "oneoff@example.com")
		self.assertEqual(self._email(cust), "onfile@example.com")

	def test_store_inbox_and_garbage_are_not_saved(self):
		cust = self._customer()
		with self.subTest("invalid"):
			manual_order._remember_customer_email(cust, "not an email")
			self.assertFalse(self._email(cust))
		real_exists = frappe.db.exists

		def exists(doctype, *args, **kwargs):
			if doctype == "Email Account":
				return args[0] == {"email_id": "info@store.example"}
			return real_exists(doctype, *args, **kwargs)

		with self.subTest("store inbox"), mock.patch.object(frappe.db, "exists", exists):
			manual_order._remember_customer_email(cust, "info@store.example")
			self.assertFalse(self._email(cust))
		with self.subTest("another buyer's address"):
			self._customer("filler@example.com")
			manual_order._remember_customer_email(cust, "filler@example.com")
			self.assertFalse(self._email(cust))
