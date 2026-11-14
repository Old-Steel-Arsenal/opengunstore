"""Marketing Contact status (ffl_core.marketing.contacts): the one-way order, the suppression ranks, the
manager's way back, the reference-level unsubscribe, a new address inheriting a suppression, and the
DocType refusing a status edit that does not go through set_status. Real documents throughout."""
from __future__ import annotations

import frappe

from ffl_core.marketing import CONTACT, contacts
from ffl_core.marketing.contacts import (BOUNCED, COMPLAINED, NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, SUPPRESSED,
	set_status)
from ffl_core.tests.crm_support import user
from ffl_core.tests.marketing_support import MarketingTestCase, address, events_of, status_of

MANAGER = "mkt-manager@example.com"
CLERK = "mkt-clerk@example.com"


def _contact(status: str = NOT_SUBSCRIBED) -> str:
	email = address()
	contacts.get_or_create(email)
	if status != NOT_SUBSCRIBED:
		set_status(email, status)
	return email


def _unsubscribe_rows(email: str) -> list[dict]:
	return frappe.get_all("Email Unsubscribe", filters={"email": email},
		fields=["reference_doctype", "reference_name", "global_unsubscribe"])


class TestEmailAndCreation(MarketingTestCase):
	def test_an_address_is_stored_trimmed_and_lowercase_and_found_again_in_any_case(self):
		email = address()
		doc = contacts.get_or_create(f"  {email.upper()} ", first_name="Pat", source="footer")
		self.assertEqual((doc.name, doc.email, doc.status, doc.source), (email, email, NOT_SUBSCRIBED, "footer"))
		again = contacts.get_or_create(email.title(), first_name="Other", last_name="Buyer", source="slidein")
		self.assertEqual(again.name, email)
		self.assertEqual((again.first_name, again.last_name, again.source), ("Pat", "Buyer", "footer"),
			"blanks are filled; a name or the first source is never overwritten")
		self.assertEqual(frappe.db.count(CONTACT, {"email": email}), 1)

	def test_not_an_address_is_refused(self):
		for bad in ("", "nobody", "a@b@c.org", "two@example.org, three@example.org", "x" * 140 + "@example.org",
				"<a@example.org>"):
			self.assertEqual(contacts.normalize_email(bad), "", bad)
			with self.assertRaises(frappe.InvalidEmailAddressError):
				contacts.get_or_create(bad)
		self.assertEqual(contacts.normalize_email("O'Brien@Example.org"), "o'brien@example.org")

	def test_a_desk_form_cannot_set_the_status_or_change_the_source(self):
		email = address()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": CONTACT, "email": email, "status": SUBSCRIBED}).insert()
		doc = frappe.get_doc({"doctype": CONTACT, "email": email.upper(), "source": "pos"}).insert()
		self.assertEqual((doc.name, doc.status), (email, NOT_SUBSCRIBED))
		doc.status = SUBSCRIBED
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc.reload()
		doc.source, doc.first_name = "elsewhere", "Pat"
		doc.save()
		self.assertEqual((status_of(email), doc.source, doc.first_name), (NOT_SUBSCRIBED, "pos", "Pat"))


class TestStatusOrder(MarketingTestCase):
	def test_every_move_up_the_order_is_taken_and_logged(self):
		order = [NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, BOUNCED, COMPLAINED]
		for i, start in enumerate(order):
			for target in order[i + 1:]:
				email = _contact(start)
				self.assertTrue(set_status(email, target, reason="test"), (start, target))
				self.assertEqual(status_of(email), target)
				self.assertEqual(events_of(email)[-1].kind, target)

	def test_no_automatic_move_down_the_order(self):
		order = [NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, BOUNCED, COMPLAINED]
		for i, start in enumerate(order):
			for target in order[:i]:
				email = _contact(start)
				logged = len(events_of(email))
				self.assertFalse(set_status(email, target, channel="footer"), (start, target))
				self.assertEqual(status_of(email), start, (start, target))
				self.assertEqual(len(events_of(email)), logged, "a refused move logs nothing")

	def test_a_higher_suppression_is_never_replaced_by_a_lower_one(self):
		email = _contact(COMPLAINED)
		for lower in (BOUNCED, UNSUBSCRIBED):
			self.assertFalse(set_status(email, lower))
		self.assertEqual(status_of(email), COMPLAINED)
		email = _contact(BOUNCED)
		self.assertFalse(set_status(email, UNSUBSCRIBED))
		self.assertTrue(set_status(email, COMPLAINED))

	def test_consent_is_recorded_and_refreshed_only_forward(self):
		email = _contact()
		set_status(email, SUBSCRIBED, channel="footer", occurred_at="2026-01-02 10:00:00")
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((str(doc.consent_at), doc.consent_channel), ("2026-01-02 10:00:00", "footer"))
		self.assertFalse(set_status(email, SUBSCRIBED, channel="slidein", occurred_at="2026-03-01 09:00:00"))
		self.assertFalse(set_status(email, SUBSCRIBED, channel="checkout", occurred_at="2025-12-01 09:00:00"))
		doc.reload()
		self.assertEqual((str(doc.consent_at), doc.consent_channel), ("2026-03-01 09:00:00", "slidein"))
		self.assertEqual([e.kind for e in events_of(email)], [SUBSCRIBED], "a refresh is not a status change")

	def test_an_unknown_status_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			set_status(_contact(), "Paused")


class TestManagerPutsBack(MarketingTestCase):
	def test_a_manager_with_a_reason_puts_a_suppressed_contact_back_to_subscribed(self):
		user(MANAGER, "Sales Manager")
		for suppressed in SUPPRESSED:
			email = _contact(suppressed)
			frappe.set_user(MANAGER)
			self.assertTrue(set_status(email, SUBSCRIBED, manual=True, reason="Asked in store to be emailed again"))
			frappe.set_user("Administrator")
			doc = frappe.get_doc(CONTACT, email)
			self.assertEqual((doc.status, doc.consent_channel, doc.status_reason),
				(SUBSCRIBED, "manual", "Asked in store to be emailed again"))
			last = events_of(email)[-1]
			self.assertEqual(last.kind, SUBSCRIBED)
			self.assertIn(MANAGER, last.detail)
			self.assertIn(suppressed, last.detail)
			self.assertFalse(_unsubscribe_rows(email), "the put-back removes the mail block")

	def test_no_reason_no_manager_or_no_suppression_is_refused(self):
		email = _contact(UNSUBSCRIBED)
		with self.assertRaises(frappe.MandatoryError):
			set_status(email, SUBSCRIBED, manual=True)
		user(CLERK, "Sales User")
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			set_status(email, SUBSCRIBED, manual=True, reason="please")
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			set_status(email, NOT_SUBSCRIBED, manual=True, reason="please")
		with self.assertRaises(frappe.ValidationError):
			set_status(_contact(SUBSCRIBED), NOT_SUBSCRIBED, manual=True, reason="please")
		with self.assertRaises(frappe.ValidationError):
			set_status(_contact(COMPLAINED), UNSUBSCRIBED, manual=True, reason="please")
		self.assertEqual(status_of(email), UNSUBSCRIBED)


class TestMailBlock(MarketingTestCase):
	def test_a_suppression_files_one_reference_level_unsubscribe_never_a_global_one(self):
		email = _contact(SUBSCRIBED)
		set_status(email, UNSUBSCRIBED, reason="footer link")
		set_status(email, COMPLAINED)
		self.assertEqual([(r.reference_doctype, r.reference_name, r.global_unsubscribe) for r in _unsubscribe_rows(email)],
			[(CONTACT, email, 0)])

	def test_a_suppressed_address_still_gets_ordinary_mail_but_not_marketing_mail(self):
		email = _contact(SUBSCRIBED)
		set_status(email, UNSUBSCRIBED)
		muted = frappe.flags.mute_emails
		frappe.flags.mute_emails = True   # a dummy outgoing account; nothing leaves the site
		try:
			receipt = frappe.sendmail(recipients=[email], subject="Your receipt", message="Thanks for shopping",
				now=False)
			marketing = frappe.sendmail(recipients=[email], subject="New arrivals", message="Look",
				reference_doctype=CONTACT, reference_name=email, now=False)
		finally:
			frappe.flags.mute_emails = muted
		self.assertTrue(receipt, "mail without the contact as reference is queued")
		self.assertEqual([r.recipient for r in receipt.recipients], [email])
		self.assertFalse(marketing, "Frappe drops the suppressed recipient of mail sent on the contact")


class TestNewAddress(MarketingTestCase):
	def test_a_new_address_inherits_a_suppression_but_not_a_subscription(self):
		old, new = _contact(BOUNCED), address()
		self.assertTrue(contacts.carry_suppression(old, new))
		self.assertEqual(status_of(new), BOUNCED)
		self.assertEqual([(r.reference_name, r.global_unsubscribe) for r in _unsubscribe_rows(new)], [(new, 0)])
		subscribed, other = _contact(SUBSCRIBED), address()
		self.assertFalse(contacts.carry_suppression(subscribed, other))
		self.assertFalse(frappe.db.exists(CONTACT, other))
		already = _contact(SUBSCRIBED)
		self.assertTrue(contacts.carry_suppression(_contact(COMPLAINED), already))
		self.assertEqual(status_of(already), COMPLAINED, "an unsubscribe follows the person")
