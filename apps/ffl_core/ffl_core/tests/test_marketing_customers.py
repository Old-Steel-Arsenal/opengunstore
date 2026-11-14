"""Store customers on the marketing list (ffl_core.marketing.customers): the intake switch, a new
and an existing contact, a suppression never undone, the shop's own domain, a changed address, a hook
that fails, the customer link (one, none, several, a merge, a delete), the customer card and the
backfill. Real Customers, Contacts and Marketing Contacts throughout."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core import customer_profile
from ffl_core.marketing import CONTACT, SETTINGS, customers, tags
from ffl_core.marketing.contacts import NOT_SUBSCRIBED, SUBSCRIBED, UNSUBSCRIBED, get_or_create, set_status
from ffl_core.tests.marketing_support import MarketingTestCase, address, events_of, make_customer as _customer, status_of

SHOP = "shop.example.test"
STORE_URL = ("WooCommerce Settings", "store_url")


def _save(name: str, **fields):
	doc = frappe.get_doc("Customer", name)
	doc.update(fields)
	doc.flags.ignore_permissions = True
	doc.save()
	return doc


def _contact_with(customer: str, *emails: str) -> str:
	doc = frappe.get_doc({"doctype": "Contact", "first_name": "Mkt",
		"email_ids": [{"email_id": e, "is_primary": int(i == 0)} for i, e in enumerate(emails)],
		"links": [{"link_doctype": "Customer", "link_name": customer}]})
	doc.insert(ignore_permissions=True)
	return doc.name


def _linked(email: str):
	return frappe.db.get_value(CONTACT, email, "customer")


def _made(email: str) -> str | None:
	"""The customer link of the contact for *email*, made now if there is none."""
	get_or_create(email)
	return _linked(email)


def _errors() -> int:
	return frappe.db.count("Error Log")


class IntakeCase(MarketingTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(SETTINGS, "auto_subscribe_customers", 1)
		self.set_single(*STORE_URL, f"https://www.{SHOP}/")


class TestIntake(IntakeCase):
	def test_a_new_customer_with_an_address_is_subscribed_tagged_and_linked(self):
		email = address("cust")
		customer = _customer(email, name="_Test Mkt Ann Lee")
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.status, doc.source, doc.consent_channel, doc.customer),
			(SUBSCRIBED, "pos", "pos", customer.name))
		self.assertEqual((doc.first_name, doc.last_name), ("_Test", "Mkt Ann Lee".split()[-1]))
		self.assertEqual(sorted(tags.of(email)), [tags.IN_STORE, tags.NEW_ARRIVALS])
		last = events_of(email)[-1]
		self.assertEqual(last.kind, SUBSCRIBED)
		self.assertIn("pos", last.detail)

	def test_it_is_off_unless_the_switch_and_the_module_are_on(self):
		self.set_single(SETTINGS, "auto_subscribe_customers", 0)
		off_switch = address("cust")
		_customer(off_switch)
		self.assertFalse(frappe.db.exists(CONTACT, off_switch))
		self.set_single(SETTINGS, "auto_subscribe_customers", 1)
		self.set_single(SETTINGS, "enabled", 0)
		module_off = address("cust")
		_customer(module_off)
		self.assertFalse(frappe.db.exists(CONTACT, module_off))

	def test_the_switchs_default_is_off(self):
		field = next(f for f in frappe.get_meta(SETTINGS).fields if f.fieldname == "auto_subscribe_customers")
		self.assertEqual(str(field.default), "0")

	def test_an_existing_contact_keeps_its_status_and_only_gains_the_store_tag_and_link(self):
		email = address("cust")
		get_or_create(email, first_name="Kept", source="alert")
		tags.add(email, "back-in-stock")
		customer = _customer(email)
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.status, doc.source, doc.first_name, doc.customer), (NOT_SUBSCRIBED, "alert", "Kept", customer.name))
		self.assertEqual(sorted(tags.of(email)), ["back-in-stock", tags.IN_STORE])
		self.assertEqual(events_of(email), [])

	def test_a_suppressed_contact_is_not_touched(self):
		email = address("cust")
		get_or_create(email)
		set_status(email, UNSUBSCRIBED)
		_customer(email)
		self.assertEqual(status_of(email), UNSUBSCRIBED)
		self.assertNotIn(tags.NEW_ARRIVALS, tags.of(email))
		self.assertEqual([e.kind for e in events_of(email)], [UNSUBSCRIBED])

	def test_a_subscribed_contact_gets_the_store_tag_without_a_second_subscribe_event(self):
		email = address("cust")
		get_or_create(email)
		set_status(email, SUBSCRIBED, channel="footer")
		_customer(email)
		self.assertEqual([e.kind for e in events_of(email)], [SUBSCRIBED])
		self.assertEqual(tags.of(email), [tags.IN_STORE])

	def test_saving_again_changes_nothing(self):
		email = address("cust")
		customer = _customer(email)
		before = (len(events_of(email)), tags.of(email))
		_save(customer.name, customer_group="Individual")
		self.assertEqual((len(events_of(email)), tags.of(email)), before)

	def test_the_shops_own_domain_a_blank_and_a_bad_address_are_left_out(self):
		staff = f"clerk-{frappe.generate_hash(length=6)}@{SHOP}"
		_customer(staff)
		self.assertFalse(frappe.db.exists(CONTACT, staff))
		self.assertFalse(frappe.db.exists(CONTACT, ""))
		self.assertEqual(customers.add_customer(""), customers.SKIPPED)
		self.assertEqual(customers.add_customer("not an address"), customers.SKIPPED)
		self.assertEqual(customers.add_customer(f"x@mail.{SHOP}", host=SHOP), customers.CREATED, "a subdomain is a shopper")

	def test_a_disabled_customer_is_not_listed(self):
		email = address("cust")
		customer = _customer(email)
		frappe.db.delete(CONTACT, {"name": email})
		_save(customer.name, disabled=1)
		self.assertFalse(frappe.db.exists(CONTACT, email))

	def test_an_address_typed_on_an_existing_customer_is_listed(self):
		customer = _customer()
		email = address("cust")
		_save(customer.name, ffl_email=email)
		self.assertEqual(status_of(email), SUBSCRIBED)
		self.assertEqual(_linked(email), customer.name)

	def test_a_primary_contacts_new_address_is_listed(self):
		customer = _customer(address("cust"))
		contact = frappe.get_doc("Contact", frappe.db.get_value("Customer", customer.name, "customer_primary_contact"))
		email = address("cust")
		contact.email_ids = []
		contact.append("email_ids", {"email_id": email, "is_primary": 1})
		contact.save(ignore_permissions=True)
		self.assertEqual(status_of(email), SUBSCRIBED)
		self.assertEqual(_linked(email), customer.name)


class TestChangedAddress(IntakeCase):
	def test_a_new_address_takes_the_old_ones_unsubscribe_and_is_not_subscribed(self):
		old, new = address("cust"), address("cust")
		customer = _customer(old)
		set_status(old, UNSUBSCRIBED)
		_save(customer.name, ffl_email=new)
		self.assertEqual(status_of(new), UNSUBSCRIBED)
		self.assertNotIn(tags.NEW_ARRIVALS, tags.of(new))

	def test_a_subscription_does_not_follow_the_person(self):
		old, new = address("cust"), address("cust")
		customer = _customer(old)
		_save(customer.name, ffl_email=new)
		self.assertEqual(status_of(old), SUBSCRIBED)
		self.assertEqual(status_of(new), SUBSCRIBED, "the new address is a store customer's own, listed as any new one")
		self.assertIn("pos", events_of(new)[-1].detail)

	def test_the_unsubscribe_follows_with_the_intake_switch_off(self):
		self.set_single(SETTINGS, "auto_subscribe_customers", 0)
		old, new = address("cust"), address("cust")
		get_or_create(old)
		set_status(old, UNSUBSCRIBED)
		customer = _customer(old)
		_save(customer.name, ffl_email=new)
		self.assertEqual(status_of(new), UNSUBSCRIBED)
		self.assertEqual(tags.of(new), [])


class TestHooksNeverBlockASave(IntakeCase):
	def test_a_failing_intake_is_logged_and_the_customer_still_saves(self):
		email = address("cust")
		errors = _errors()
		with patch.object(customers, "add_customer", side_effect=RuntimeError("boom")):
			customer = _customer(email)
		self.assertTrue(frappe.db.exists("Customer", customer.name))
		self.assertFalse(frappe.db.exists(CONTACT, email))
		self.assertGreater(_errors(), errors)

	def test_a_failing_contact_hook_is_logged_and_the_contact_still_saves(self):
		customer = _customer(address("cust"))
		errors = _errors()
		with patch.object(customers, "relink", side_effect=RuntimeError("boom")):
			contact = _contact_with(customer.name, address("other"))
		self.assertTrue(frappe.db.exists("Contact", contact))
		self.assertGreater(_errors(), errors)

	def test_a_half_written_intake_is_undone(self):
		email = address("cust")
		with patch.object(customers, "relink", side_effect=RuntimeError("boom")):
			_customer(email)
		self.assertFalse(frappe.db.exists(CONTACT, email), "the contact made before the failure is rolled back with it")

	def test_a_contact_made_by_the_website_stands_when_linking_fails(self):
		email = address("web")
		errors = _errors()
		with patch.object(customers, "relink", side_effect=RuntimeError("boom")):
			get_or_create(email)
		self.assertTrue(frappe.db.exists(CONTACT, email))
		self.assertGreater(_errors(), errors)


class TestCustomerLink(MarketingTestCase):
	def test_one_customer_by_its_address(self):
		email = address("link")
		customer = _customer(email)
		self.assertEqual(_made(email), customer.name)
		self.assertEqual(customers.resolve_customer(email.upper()), customer.name)

	def test_none_when_nobody_has_the_address(self):
		email = address("link")
		self.assertIsNone(customers.resolve_customer(email))
		self.assertIsNone(_made(email))

	def test_two_customers_with_the_address_link_to_neither(self):
		email = address("link")
		_customer(email)
		_customer(email)
		self.assertIsNone(customers.resolve_customer(email))
		self.assertIsNone(_made(email))

	def test_an_address_on_a_contact_finds_its_customer(self):
		customer = _customer(address("link"))
		other = address("link")
		_contact_with(customer.name, other)
		self.assertEqual(_made(other), customer.name)

	def test_an_address_on_the_contacts_of_two_customers_links_to_neither(self):
		shared = address("link")
		_contact_with(_customer().name, shared)
		_contact_with(_customer().name, shared)
		self.assertIsNone(_made(shared))

	def test_saving_a_customer_or_contact_links_a_contact_made_earlier(self):
		email = address("link")
		get_or_create(email)
		customer = _customer()
		self.assertIsNone(_linked(email))
		_contact_with(customer.name, email)
		self.assertEqual(_linked(email), customer.name)

	def test_a_second_owner_clears_the_link(self):
		email = address("link")
		first = _customer(email)
		get_or_create(email)
		self.assertEqual(_linked(email), first.name)
		_customer(email)
		self.assertIsNone(_linked(email))

	def test_a_merge_leaves_the_surviving_customer_linked(self):
		src_email, tgt_email = address("link"), address("link")
		src, tgt = _customer(src_email), _customer(tgt_email)
		get_or_create(src_email)
		get_or_create(tgt_email)
		self.assertEqual(_linked(src_email), src.name)
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertEqual((_linked(src_email), _linked(tgt_email)), (tgt.name, tgt.name))

	def test_a_merged_away_address_with_no_contact_yet_links_through_the_alias(self):
		src_email, tgt_email = address("link"), address("link")
		src, tgt = _customer(src_email), _customer(tgt_email)
		customer_profile.merge_customer(src.name, tgt.name)
		self.assertEqual(_made(src_email), tgt.name)

	def test_deleting_a_customer_is_not_blocked_by_its_marketing_contact(self):
		email = address("link")
		customer = _customer(email)
		get_or_create(email)
		frappe.delete_doc("Customer", customer.name, force=False, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Customer", customer.name))
		self.assertTrue(frappe.db.exists(CONTACT, email))
		self.assertIsNone(_linked(email))


class TestCustomerCard(MarketingTestCase):
	def test_none_when_the_module_is_off_or_there_is_no_contact(self):
		customer = _customer(address("card"))
		self.assertIsNone(customers.card(customer.name))
		get_or_create(customer.email_id)
		self.set_single(SETTINGS, "enabled", 0)
		self.assertIsNone(customers.card(customer.name))

	def test_status_and_tags_of_the_customers_own_address(self):
		email = address("card")
		customer = _customer(email)
		get_or_create(email)
		set_status(email, SUBSCRIBED, channel="pos")
		tags.add(email, tags.IN_STORE, "gunsmith")
		self.assertEqual(customers.card(customer.name), {"status": SUBSCRIBED, "tags": [tags.IN_STORE, "gunsmith"]})

	def test_the_customers_own_address_wins_over_another_linked_one(self):
		own, other = address("card"), address("card")
		customer = _customer(own)
		_contact_with(customer.name, other)
		get_or_create(other)
		set_status(other, UNSUBSCRIBED)
		get_or_create(own)
		self.assertEqual(customers.card(customer.name)["status"], NOT_SUBSCRIBED)

	def test_the_deal_card_shows_the_line_and_a_suppressed_one_in_red(self):
		from ffl_core.crm import customer as deal_customer

		email = address("card")
		customer = _customer(email)
		get_or_create(email)
		set_status(email, UNSUBSCRIBED)
		tags.add(email, tags.IN_STORE)
		summary = deal_customer.customer_summary(frappe._dict(erpnext_customer=customer.name, name="no-such-deal"))
		self.assertEqual(summary["marketing"], {"status": UNSUBSCRIBED, "tags": [tags.IN_STORE]})
		self.assertIn("Marketing email: Unsubscribed · in-store", summary["html"])
		self.assertIn("#b91c1c", summary["html"].split("Marketing email")[0].rsplit("<div", 1)[1])

	def test_a_customer_with_no_contact_has_marketing_none_and_no_line(self):
		from ffl_core.crm import customer as deal_customer

		customer = _customer()
		summary = deal_customer.customer_summary(frappe._dict(erpnext_customer=customer.name, name="no-such-deal"))
		self.assertIsNone(summary["marketing"])
		self.assertNotIn("Marketing email", summary["html"])


class TestBackfill(MarketingTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(*STORE_URL, f"https://{SHOP}")

	def _scene(self):
		"""Customers made with the switch off: a new address (on two customers), one already on the
		list, the shop's own, one with none, and a disabled one."""
		self.new, self.known, self.staff = address("bf"), address("bf"), f"clerk-{frappe.generate_hash(length=6)}@{SHOP}"
		get_or_create(self.known)
		for email in (self.new, self.known, self.staff, None, self.new):
			_customer(email)
		_save(_customer(address("bf")).name, disabled=1)

	def test_a_dry_run_counts_and_writes_nothing(self):
		before = backfill_counts()
		self._scene()
		contacts = frappe.db.count(CONTACT)
		report = customers.backfill_customers()
		self.assertTrue(report["dry_run"])
		self.assertEqual(frappe.db.count(CONTACT), contacts)
		delta = {k: report[k] - before[k] for k in before}
		self.assertEqual(delta, {"customers": 5, "create": 1, "existing": 2, "skipped_no_email": 1, "skipped_store_domain": 1,
			"skipped_invalid": 0, "failed": 0}, "6 made, 1 disabled; the second customer on the new address counts as existing")

	def test_anything_but_an_explicit_zero_is_a_dry_run(self):
		self._scene()
		contacts = frappe.db.count(CONTACT)
		for value in (1, "1", None, "no", True):
			self.assertTrue(customers.backfill_customers(dry_run=value)["dry_run"])
		self.assertEqual(frappe.db.count(CONTACT), contacts)

	def test_a_real_run_lists_them_the_way_the_hook_does(self):
		before = backfill_counts()
		self._scene()
		report = customers.backfill_customers(dry_run=0)
		self.assertFalse(report["dry_run"])
		self.assertEqual({k: report[k] - before[k] for k in before}, {"customers": 5, "create": 1, "existing": 2,
			"skipped_no_email": 1, "skipped_store_domain": 1, "skipped_invalid": 0, "failed": 0})
		doc = frappe.get_doc(CONTACT, self.new)
		self.assertEqual((doc.status, doc.source), (SUBSCRIBED, "pos"))
		self.assertEqual(sorted(tags.of(self.new)), [tags.IN_STORE, tags.NEW_ARRIVALS])
		self.assertIsNone(doc.customer, "two customers share the address, so it is linked to neither")
		self.assertEqual(status_of(self.known), NOT_SUBSCRIBED, "one already on the list keeps its status")
		self.assertEqual(tags.of(self.known), [tags.IN_STORE])
		self.assertFalse(frappe.db.exists(CONTACT, self.staff))
		again = customers.backfill_customers(dry_run=0)
		self.assertEqual(again["create"], 0, "a second run creates nothing")

	def test_a_real_run_needs_the_module_on(self):
		self.set_single(SETTINGS, "enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			customers.backfill_customers(dry_run=0)
		self.assertTrue(customers.backfill_customers()["dry_run"])

	def test_one_bad_customer_does_not_stop_the_rest(self):
		_customer(address("bf"))
		_customer(address("bf"))
		real = customers.add_customer
		calls = []

		def flaky(email, **kw):
			calls.append(email)
			if len(calls) == 1:
				raise RuntimeError("boom")
			return real(email, **kw)

		with patch.object(customers, "add_customer", side_effect=flaky):
			report = customers.backfill_customers(dry_run=0)
		self.assertEqual(report["failed"], 1)
		self.assertEqual(report["errors"][0]["error"], "RuntimeError")
		self.assertGreaterEqual(report["create"], 1)


def backfill_counts() -> dict:
	report = customers.backfill_customers()
	return {k: report[k] for k in ("customers", "create", "existing", "skipped_no_email", "skipped_store_domain",
		"skipped_invalid", "failed")}
