"""Incoming staff mail inserted outside a mail pull: customer mail on their open deal, team mail from
unknown senders becomes one lead each, direct mail from them and our own / blocked / machine senders
are left alone, and CRM's own lead switch is taken over. The pull itself: test_crm_team_mail."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.crm import mail
from ffl_core.tests.crm_support import CRMTestCase

INBOX = "_T CRM Staff Inbox"
TEAM = "sales@store.test"


def _inbox(auto: int = 1) -> str:
	if not frappe.db.exists("Email Account", INBOX):
		doc = frappe.get_doc({"doctype": "Email Account", "email_account_name": INBOX, "email_id": "staff@store.test",
			"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1})
		doc.insert(ignore_permissions=True)
	frappe.db.set_value("Email Account", INBOX, mail.AUTO_LEAD, auto, update_modified=False)
	return INBOX


def _receive(sender: str, full_name: str | None = None, account: str | None = None, **fields):
	return frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
		"communication_medium": "Email", "sent_or_received": "Received", "sender": sender,
		"sender_full_name": full_name, "email_account": account or INBOX, "subject": "Do you have it?",
		"content": "Hello", **fields}).insert(ignore_permissions=True)


def _customer_with_contact(email: str) -> str:
	customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_T CRM Known Buyer",
		"customer_type": "Individual"}).insert(ignore_permissions=True)
	frappe.get_doc({"doctype": "Contact", "first_name": "Known", "email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert(ignore_permissions=True)
	return customer.name


def _contact_of(customer: str) -> str:
	return frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "link_doctype": "Customer",
		"link_name": customer}, pluck="parent")[0]


def _settings(**values) -> None:
	doc = frappe.get_doc(mail.SETTINGS)
	doc.update(values)
	doc.save(ignore_permissions=True)


def _leads(email: str) -> list[str]:
	return frappe.get_all("CRM Lead", filters={"email": email}, pluck="name")


class TestIncomingMail(CRMTestCase):
	def setUp(self):
		super().setUp()
		_inbox()
		_settings(blocked_sender_domains="spam.test\n@Bulk.Test", own_domains="store.test",
			team_addresses=[{"address": TEAM, "address_type": "Group"}], primary_team_address="")

	def tearDown(self):
		super().tearDown()
		# The rollback takes the settings back; the cached copy the save left does not go with it.
		frappe.clear_document_cache(mail.SETTINGS, mail.SETTINGS)

	def test_an_unknown_sender_to_a_team_address_becomes_one_lead(self):
		comm = _receive("Jane Q Public <Jane@Buyer.test>", "Jane Q Public", recipients=TEAM)
		leads = _leads("jane@buyer.test")
		self.assertEqual(len(leads), 1)
		lead = frappe.get_doc("CRM Lead", leads[0])
		self.assertEqual((lead.first_name, lead.last_name), ("Jane", "Q Public"))
		if frappe.db.exists("CRM Lead Source", "Email"):
			self.assertEqual(lead.source, "Email")
		self.assertEqual((comm.reference_doctype, comm.reference_name), ("CRM Lead", leads[0]))
		again = _receive("jane@buyer.test")   # direct mail from a known lead
		self.assertEqual(_leads("jane@buyer.test"), leads)
		self.assertEqual(frappe.db.get_value("Communication", again.name, "reference_name"), leads[0])

	def test_direct_mail_from_an_unknown_sender_makes_nothing(self):
		comm = _receive("friend@buyer.test")
		self.assertFalse(_leads("friend@buyer.test"))
		self.assertFalse(frappe.db.get_value("Communication", comm.name, "reference_doctype"))

	def test_a_customer_mail_goes_on_their_open_deal(self):
		customer = _customer_with_contact("known@buyer.test")
		comm = _receive("Known <KNOWN@buyer.test>")
		doctype, deal = frappe.db.get_value("Communication", comm.name, ["reference_doctype", "reference_name"])
		self.assertEqual(doctype, "CRM Deal")
		doc = frappe.get_doc("CRM Deal", deal)
		self.assertEqual([c.contact for c in doc.contacts], [_contact_of(customer)])
		if doc.meta.has_field("erpnext_customer"):
			self.assertEqual(doc.erpnext_customer, customer)
		self.assertFalse(_leads("known@buyer.test"))
		# The next mail joins the same deal while it is open; once it is won, a new one is opened.
		self.assertEqual(frappe.db.get_value("Communication", _receive("known@buyer.test").name, "reference_name"), deal)
		won = frappe.get_all("CRM Deal Status", filters={"type": "Won"}, pluck="name", limit=1)[0]
		frappe.db.set_value("CRM Deal", deal, "status", won)
		later = frappe.db.get_value("Communication", _receive("known@buyer.test").name, "reference_name")
		self.assertNotEqual(later, deal)

	def test_a_customer_deal_belongs_to_the_mailbox_user(self):
		user = "staff@store.test"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "Staff", "send_welcome_email": 0}).insert(
				ignore_permissions=True)
		_customer_with_contact("owner@buyer.test")
		deal = frappe.db.get_value("Communication", _receive("owner@buyer.test").name, "reference_name")
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "deal_owner"), user)

	def test_blocked_own_and_machine_senders_are_other_mail(self):
		for sender, category in (("deals@spam.test", "Blocked"), ("x@news.bulk.test", "Blocked"),
				("colleague@store.test", "Our store"), ("no-reply@shop.test", "Automated"),
				("alerts-noreply@bank.test", "Automated"), ("noreply+tag@shop.test", "Automated")):
			comm = _receive(sender, recipients=TEAM)
			[lead] = _leads(sender)
			self.assertEqual(frappe.db.get_value("Communication", comm.name, "reference_name"), lead)
			self.assertEqual(frappe.db.get_value("CRM Lead", lead, ["status", mail.CATEGORY_FIELD]), (mail.OTHER_MAIL, category))

	def test_a_mailbox_without_the_switch_makes_nothing(self):
		_inbox(auto=0)
		_receive("stranger@buyer.test")
		self.assertFalse(_leads("stranger@buyer.test"))

	def test_sent_mail_is_left_alone(self):
		_receive("out@buyer.test", sent_or_received="Sent")
		self.assertFalse(_leads("out@buyer.test"))

	def test_a_routing_failure_never_fails_the_mail_pull(self):
		with patch.object(mail, "_new_lead", side_effect=RuntimeError("CRM down")):
			comm = _receive("broken@buyer.test", recipients=TEAM)
		self.assertTrue(frappe.db.exists("Communication", comm.name))
		self.assertFalse(frappe.db.get_value("Communication", comm.name, "reference_doctype"))

	def test_crm_own_lead_switch_is_taken_over(self):
		_inbox(auto=0)
		doc = frappe.get_doc("Email Account", INBOX)
		doc.create_lead_from_incoming_email = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Email Account", INBOX, ["create_lead_from_incoming_email", mail.AUTO_LEAD]),
			(0, 1))

	def test_settings_keep_one_clean_domain_per_line(self):
		self.assertEqual(frappe.db.get_single_value(mail.SETTINGS, "blocked_sender_domains"), "spam.test\nbulk.test")
