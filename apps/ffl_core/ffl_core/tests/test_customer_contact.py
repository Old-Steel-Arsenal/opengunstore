"""Email / Phone on the Customer form go to its primary Contact (ffl_core.customer_contact)."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


def _email() -> str:
	return f"t{frappe.generate_hash(length=8)}@example.com"


def _customer(**fields):
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test Contact {frappe.generate_hash(length=6)}",
		"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories", **fields})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _save(name: str, **fields):
	doc = frappe.get_doc("Customer", name)
	doc.update(fields)
	doc.flags.ignore_permissions = True
	doc.save()
	return doc


class CustomerEmailPhone(FrappeTestCase):
	def test_a_new_customer_gets_a_primary_contact(self):
		email = _email()
		doc = _customer(ffl_email=email, ffl_phone="5125550101")
		self.assertTrue(doc.customer_primary_contact)
		contact = frappe.get_doc("Contact", doc.customer_primary_contact)
		self.assertEqual((contact.email_id, contact.mobile_no), (email, "5125550101"))
		self.assertEqual(frappe.db.get_value("Customer", doc.name, "ffl_email"), email, "the form shows it")

	def test_a_customer_without_a_contact_gets_one_on_edit(self):
		doc = _customer()
		self.assertFalse(doc.customer_primary_contact)
		email = _email()
		doc = _save(doc.name, ffl_email=email)
		self.assertEqual(frappe.db.get_value("Contact", doc.customer_primary_contact, "email_id"), email)
		self.assertEqual(frappe.db.get_value("Customer", doc.name, "email_id"), email)

	def test_an_edit_becomes_primary_and_keeps_the_old_value(self):
		old, new = _email(), _email()
		doc = _customer(ffl_email=old, ffl_phone="5125550102")
		doc = _save(doc.name, ffl_email=new, ffl_phone="5125550103")
		contact = frappe.get_doc("Contact", doc.customer_primary_contact)
		self.assertEqual((contact.email_id, contact.mobile_no), (new, "5125550103"))
		self.assertEqual({r.email_id for r in contact.email_ids}, {old, new})
		self.assertEqual({r.phone for r in contact.phone_nos}, {"5125550102", "5125550103"})
		self.assertEqual(frappe.db.get_value("Customer", doc.name, ["email_id", "mobile_no"]), (new, "5125550103"))

	def test_a_save_without_the_fields_or_with_blanks_changes_nothing(self):
		email = _email()
		doc = _customer(ffl_email=email)
		_save(doc.name, customer_details="note")
		_save(doc.name, ffl_email="", ffl_phone="")
		self.assertEqual(frappe.db.get_value("Contact", doc.customer_primary_contact, "email_id"), email)

	def test_a_bad_email_is_refused(self):
		doc = _customer()
		with self.assertRaises(frappe.ValidationError):
			_save(doc.name, ffl_email="not-an-email")

	def test_an_edit_on_the_contact_reaches_the_form(self):
		doc = _customer(ffl_email=_email())
		contact = frappe.get_doc("Contact", doc.customer_primary_contact)
		new = _email()
		contact.append("email_ids", {"email_id": new})
		for row in contact.email_ids:
			row.is_primary = int(row.email_id == new)
		contact.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Customer", doc.name, ["email_id", "ffl_email"]), (new, new))

	def test_backfill_fills_older_customers(self):
		from ffl_core.customer_contact import backfill

		email = _email()
		doc = _customer(ffl_email=email)
		frappe.db.set_value("Customer", doc.name, "ffl_email", "", update_modified=False)
		backfill()
		self.assertEqual(frappe.db.get_value("Customer", doc.name, "ffl_email"), email)
