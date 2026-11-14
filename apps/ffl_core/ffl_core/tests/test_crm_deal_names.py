"""A CRM Deal the POS opens for a contact carries the person's name as a converted deal does
(crm.deals.person_name: the Contact, else its Customer, else the email), and the one-shot patch names
the deals made before. Real documents, real patch."""
from __future__ import annotations

import frappe

from ffl_core.crm.deals import open_deal_for
from ffl_core.patches import name_crm_deals
from ffl_core.tests.crm_support import CRMTestCase

NAMES = ["first_name", "last_name", "lead_name"]


def _contact(email: str, first: str, last: str | None = None, customer: str | None = None) -> str:
	return frappe.get_doc({"doctype": "Contact", "first_name": first, "last_name": last,
		"email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer}] if customer else []}).insert(ignore_permissions=True).name


def _customer(name: str) -> str:
	return frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": "Individual"}
		).insert(ignore_permissions=True).name


def _names(deal: str) -> tuple:
	return frappe.db.get_value("CRM Deal", deal, NAMES)


def _blank(deal: str, **keep) -> None:
	frappe.db.set_value("CRM Deal", deal, {**dict.fromkeys(NAMES, ""), **keep}, update_modified=False)


class TestDealNames(CRMTestCase):
	def test_the_contacts_name(self):
		deal = open_deal_for(_contact("names-a@buyer.test", "Dana", "Reyes"))
		self.assertEqual(_names(deal), ("Dana", "Reyes", "Dana Reyes"))

	def test_an_address_for_a_first_name_falls_back_to_the_customer(self):
		customer = _customer("_T Lee Morgan Smith")
		deal = open_deal_for(_contact("names-b@buyer.test", "names-b@buyer.test", customer=customer))
		self.assertEqual(_names(deal), ("_T", "Lee Morgan Smith", "_T Lee Morgan Smith"))

	def test_no_name_and_no_customer_takes_the_email(self):
		deal = open_deal_for(_contact("names.c@buyer.test", "names.c@buyer.test"))
		self.assertEqual(_names(deal), ("names.c", "", "names.c"))

	def test_the_deals_list_finds_it_by_the_persons_name(self):
		deal = open_deal_for(_contact("names-d@buyer.test", "Quinn", "Halvorsen"))
		# What a CRM list filter on the Full Name column asks (Data: like %text%).
		self.assertIn(deal, frappe.get_list("CRM Deal", filters={"lead_name": ["like", "%halvorsen%"]}, pluck="name"))

	def test_the_patch_names_blank_deals_keeps_named_ones_and_is_idempotent(self):
		customer = _customer("_T Robin Vale")
		by_contact = open_deal_for(_contact("names-e@buyer.test", "Ellis", "Park"))
		by_customer = open_deal_for(_contact("names-f@buyer.test", "names-f@buyer.test", customer=customer))
		by_email = open_deal_for(_contact("names.g@buyer.test", "names.g@buyer.test"))
		half = open_deal_for(_contact("names-h@buyer.test", "Ignored", "Name"))
		named = open_deal_for(_contact("names-i@buyer.test", "Contact", "Name"))
		for deal in (by_contact, by_customer, by_email):
			_blank(deal)
		_blank(half, first_name="Kept", last_name="Surname")
		frappe.db.set_value("CRM Deal", named, dict(zip(NAMES, ("Set", "By Hand", "Set By Hand"))), update_modified=False)
		modified = {d: frappe.db.get_value("CRM Deal", d, "modified") for d in (by_contact, by_customer, by_email, half)}

		name_crm_deals.execute()
		self.assertEqual(_names(by_contact), ("Ellis", "Park", "Ellis Park"))
		self.assertEqual(_names(by_customer), ("_T", "Robin Vale", "_T Robin Vale"))
		self.assertEqual(_names(by_email), ("names.g", "", "names.g"))
		self.assertEqual(_names(half), ("Kept", "Surname", "Kept Surname"))
		self.assertEqual(_names(named), ("Set", "By Hand", "Set By Hand"))
		for deal, when in modified.items():
			self.assertEqual(frappe.db.get_value("CRM Deal", deal, "modified"), when)

		frappe.db.set_value("Contact", frappe.db.get_value("CRM Contacts", {"parent": by_contact}, "contact"),
			"first_name", "Changed")
		name_crm_deals.execute()
		self.assertEqual(_names(by_contact), ("Ellis", "Park", "Ellis Park"), "a named deal is left as it is")
