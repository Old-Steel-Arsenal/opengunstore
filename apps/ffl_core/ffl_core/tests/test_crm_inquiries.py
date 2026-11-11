"""A website inquiry on CRM (ffl_core.crm.inquiries): a lead or a deal, a note, the gun as a product line,
once per event. Real documents; nothing is stubbed but the CRM-not-installed switch."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.crm import inquiries, mail, setup
from ffl_core.tests.crm_support import AMMO, PER_GUN, CRMTestCase, product_of
from ffl_core.tests.test_crm_deal import _gun
from ffl_core.tests.test_crm_team_mail import _assignees, _customer_contact

_n = [0]


def _email() -> str:
	_n[0] += 1
	return f"inq-{_n[0]}-{frappe.generate_hash(length=6)}@example.org"


def _file(**over) -> dict:
	_n[0] += 1
	args = {"kind": "ask", "event_id": f"evt-{_n[0]}-{frappe.generate_hash(length=6)}", "email": _email(),
		"full_name": "Pat Buyer", "phone": "555-0100", "message": "Is it still for sale?"}
	args.update(over)
	return inquiries.file_inquiry(**args)


def _notes(doctype: str, name: str) -> list[dict]:
	return frappe.get_all("FCRM Note", filters={"reference_doctype": doctype, "reference_docname": name},
		fields=["title", "content"], order_by="creation asc")


def _rows(doctype: str, name: str) -> list[tuple]:
	return [(r.product_code, r.qty, r.get("ffl_serial_no")) for r in frappe.get_doc(doctype, name).products]


class TestInquiryFiling(CRMTestCase):
	def test_each_kind_from_a_new_address_makes_a_shared_lead_with_the_gun(self):
		serial = _gun("_T-CRM-INQ-1", 899)
		for kind, label in (("map_price", "price request"), ("sell_gun", "sell-your-gun request"), ("ask", "question")):
			out = _file(kind=kind, sku=f"{PER_GUN}::{serial}")
			self.assertEqual((out["filed"], out["doctype"], out["created"]), (True, "CRM Lead", True))
			lead = frappe.get_doc("CRM Lead", out["name"])
			self.assertEqual((lead.source, lead.first_name, lead.last_name, lead.status, lead.lead_owner),
				("Website", "Pat", "Buyer", "New", None))
			self.assertEqual(lead.get(mail.CATEGORY_FIELD), None)
			(note,) = _notes("CRM Lead", lead.name)
			self.assertTrue(note.title.startswith(f"Website {label} (evt-"), note.title)
			self.assertIn("Is it still for sale?", note.content)
			self.assertIn("555-0100", note.content)
			self.assertEqual(_rows("CRM Lead", lead.name), [(product_of(PER_GUN), 1, serial)])
			self.assertTrue(_assignees("CRM Lead", lead.name), "a new lead is the team's")

	def test_a_customer_gets_their_open_deal_or_a_new_shared_one(self):
		email = _email()
		_customer_contact(email)
		serial = _gun("_T-CRM-INQ-2", 899)
		first = _file(email=email, sku=f"{PER_GUN}::{serial}")
		self.assertEqual((first["doctype"], first["created"]), ("CRM Deal", True))
		deal = first["name"]
		self.assertTrue(_assignees("CRM Deal", deal))
		self.assertEqual(_rows("CRM Deal", deal), [(product_of(PER_GUN), 1, serial)])
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "total"), 899)
		second = _file(email=email, kind="map_price", sku=f"{AMMO}")
		self.assertEqual((second["name"], second["created"]), (deal, False))
		self.assertEqual(len(_notes("CRM Deal", deal)), 2)
		self.assertEqual(len(_rows("CRM Deal", deal)), 2)
		self.assertFalse(frappe.db.exists("CRM Lead", {"email": email}), "a customer never gets a lead")

	def test_a_deal_someone_owns_keeps_its_owner_and_is_not_shared(self):
		from ffl_core.tests.test_crm_deal_pos import OWNER, _contact, _deal
		from ffl_core.tests.crm_support import user

		user(OWNER, "Sales User")
		email = _email()
		contact = _contact(email)
		frappe.get_doc({"doctype": "Dynamic Link", "parenttype": "Contact", "parent": contact, "parentfield": "links",
			"link_doctype": "Customer", "link_name": frappe.get_doc({"doctype": "Customer", "customer_name": "_T Inq Owner",
			"customer_type": "Individual"}).insert(ignore_permissions=True).name}).db_insert()
		deal = _deal(contact, owner=OWNER, status="Talking")
		out = _file(email=email)
		self.assertEqual((out["name"], out["created"]), (deal.name, False))
		after = frappe.db.get_value("CRM Deal", deal.name, ["deal_owner", "status"])
		self.assertEqual(after, (OWNER, "Talking"))
		self.assertEqual(_assignees("CRM Deal", deal.name), [OWNER], "the owner only: not shared with the team")

	def test_a_known_lead_is_reused_with_its_status_and_the_gun_not_repeated(self):
		email = _email()
		serial = _gun("_T-CRM-INQ-3", 899)
		a = _file(email=email, sku=f"{PER_GUN}::{serial}")
		frappe.db.set_value("CRM Lead", a["name"], "status", "Contacted")
		b = _file(email=email, kind="map_price", sku=f"{PER_GUN}::{serial}")
		self.assertEqual((b["name"], b["created"]), (a["name"], False))
		self.assertEqual(frappe.db.get_value("CRM Lead", a["name"], "status"), "Contacted")
		self.assertEqual(len(_notes("CRM Lead", a["name"])), 2)
		self.assertEqual(_rows("CRM Lead", a["name"]), [(product_of(PER_GUN), 1, serial)])

	def test_other_mail_and_converted_leads_are_not_reused(self):
		email = _email()
		other = mail._new_lead(email, "No Reply", "Automated")
		out = _file(email=email)
		self.assertNotEqual(out["name"], other)
		converted = frappe.get_doc("CRM Lead", out["name"])
		converted.db_set("converted", 1)
		self.assertNotEqual(_file(email=email)["name"], out["name"])

	def test_the_same_event_files_once(self):
		serial = _gun("_T-CRM-INQ-4", 899)
		args = {"kind": "sell_gun", "event_id": "evt-twice", "email": _email(), "sku": f"{PER_GUN}::{serial}"}
		first = inquiries.file_inquiry(**args)
		leads, notes = frappe.db.count("CRM Lead"), frappe.db.count("FCRM Note")
		again = inquiries.file_inquiry(**args)
		self.assertEqual((again["doctype"], again["name"], again["created"]), ("CRM Lead", first["name"], False))
		self.assertEqual((frappe.db.count("CRM Lead"), frappe.db.count("FCRM Note")), (leads, notes))
		self.assertEqual(len(_rows("CRM Lead", first["name"])), 1)

	def test_an_unknown_sku_is_only_noted(self):
		out = _file(sku="NO-SUCH-ITEM::SN-1")
		self.assertEqual(_rows("CRM Lead", out["name"]), [])
		self.assertIn("NO-SUCH-ITEM::SN-1", _notes("CRM Lead", out["name"])[0].content)

	def test_a_gun_no_longer_in_stock_or_not_ours_is_a_line_without_the_gun_and_saves(self):
		sold = _gun("_T-CRM-INQ-5", 899, status="Delivered")
		for email in (_email(), (lambda e: (_customer_contact(e), e)[1])(_email())):
			out = _file(email=email, sku=f"{PER_GUN}::{sold}")
			self.assertEqual(_rows(out["doctype"], out["name"]), [(product_of(PER_GUN), 1, None)])
		out = _file(sku=f"{PER_GUN}::SN-THAT-DOES-NOT-EXIST")
		self.assertEqual(_rows(out["doctype"], out["name"]), [(product_of(PER_GUN), 1, None)])

	def test_text_is_stored_escaped_capped_and_only_https_photos_are_linked(self):
		out = _file(kind="sell_gun", full_name="<b>Eve</b> O'Neil", message="<script>alert(1)</script>" + "x" * 9000,
			url="javascript:alert(1)", sku="<img>",
			details={"Make": "Colt <i>", "Condition": "Good", "<k>": "v"},
			photos=["https://cdn.example.org/a.jpg", "http://cdn.example.org/b.jpg", "javascript:x"] + ["https://x.example/%d" % i for i in range(30)])
		(note,) = _notes("CRM Lead", out["name"])
		self.assertNotIn("<script>", note.content)
		self.assertNotIn("<img>", note.content)
		self.assertNotIn("javascript:", note.content)
		self.assertIn("&lt;script&gt;", note.content)
		self.assertIn("<b>Make</b>: Colt &lt;i&gt;", note.content)
		self.assertNotIn("b.jpg", note.content)
		self.assertEqual(note.content.count('<a href="https://'), inquiries.MAX_PHOTOS)
		self.assertLess(len(note.content), inquiries.CAP["message"] + 3000)

	def test_a_bad_kind_or_address_is_refused_and_files_nothing(self):
		before = (frappe.db.count("CRM Lead"), frappe.db.count("FCRM Note"))
		for over in ({"kind": "complaint"}, {"email": ""}, {"email": "not-an-email"}, {"email": "a@example.org, b@example.org"},
				{"event_id": " "}):
			with self.assertRaises(frappe.ValidationError, msg=str(over)):
				_file(**over)
		self.assertEqual((frappe.db.count("CRM Lead"), frappe.db.count("FCRM Note")), before)

	def test_nothing_is_filed_while_crm_is_not_installed(self):
		before = frappe.db.count("CRM Lead")
		with patch.object(inquiries, "installed", return_value=False):
			out = _file()
		self.assertFalse(out["filed"])
		self.assertEqual(frappe.db.count("CRM Lead"), before)

	def test_nothing_is_emailed(self):
		queued = frappe.db.count("Email Queue")
		_file()
		self.assertEqual(frappe.db.count("Email Queue"), queued)

	def test_a_lead_keeps_its_gun_and_serial_when_it_becomes_a_deal(self):
		serial = _gun("_T-CRM-INQ-6", 899)
		out = _file(sku=f"{PER_GUN}::{serial}")
		deal = frappe.get_doc("CRM Lead", out["name"]).convert_to_deal()
		self.assertEqual(_rows("CRM Deal", deal), [(product_of(PER_GUN), 1, serial)])
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "total"), 899, "priced by the register on the way")

	def test_the_lead_sources_are_made_once_and_the_msgid_field_is_there(self):
		for source in setup.LEAD_SOURCES:
			frappe.delete_doc("CRM Lead Source", source, force=True, ignore_permissions=True)
		setup.ensure_lead_sources()
		setup.ensure_lead_sources()
		for source in setup.LEAD_SOURCES:
			self.assertEqual(frappe.db.count("CRM Lead Source", {"source_name": source}), 1)
		self.assertTrue(frappe.get_meta("CRM Lead").has_field("ffl_gb_msgid"))
