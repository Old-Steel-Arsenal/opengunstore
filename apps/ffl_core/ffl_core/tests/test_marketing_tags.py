"""Marketing Contact tags (ffl_core.marketing.tags) on Frappe's own Tag / Tag Link: slugs kept exactly,
audiences read from Tag Link, a column wide enough for many tags, and tagging 1,500 contacts at once."""
from __future__ import annotations

import time

import frappe

from ffl_core.marketing import CONTACT, contacts, setup, tags
from ffl_core.tests.marketing_support import MarketingTestCase, address

BULK = 1500
# lazy: a generous ceiling for a shared CI box, not a benchmark; upgrade path: none needed below ~10k contacts.
BULK_SECONDS = 60


def _contact(status: str = contacts.NOT_SUBSCRIBED) -> str:
	email = address("tag")
	contacts.get_or_create(email)
	if status != contacts.NOT_SUBSCRIBED:
		contacts.set_status(email, status)
	return email


def _links(email: str) -> list[str]:
	return frappe.get_all("Tag Link", filters={"document_type": CONTACT, "document_name": email},
		pluck="tag", order_by="creation asc, name asc")


class TestTags(MarketingTestCase):
	def test_slugs_are_kept_exactly_in_both_places_frappe_reads_them(self):
		email = _contact()
		mixed = "Glock-Fans-" + frappe.generate_hash(length=4)   # not one this module ever makes; Tag stores it as given
		self.assertEqual(tags.add(email, tags.NEW_ARRIVALS, "insider-list", mixed, tags.NEW_ARRIVALS),
			[tags.NEW_ARRIVALS, "insider-list", mixed])
		self.assertEqual(tags.of(email), [tags.NEW_ARRIVALS, "insider-list", mixed])
		self.assertEqual(frappe.db.get_value(CONTACT, email, "_user_tags"), f"new-arrivals,insider-list,{mixed}")
		self.assertEqual(_links(email), [tags.NEW_ARRIVALS, "insider-list", mixed])
		self.assertEqual(frappe.db.get_value("Tag", mixed, "name"), mixed)
		self.assertEqual(tags.add(email, tags.NEW_ARRIVALS), [], "a tag already there is not added twice")

	def test_a_tag_master_in_another_case_does_not_recase_the_slug(self):
		slug = "vip-" + frappe.generate_hash(length=4)
		frappe.get_doc({"doctype": "Tag", "name": slug.upper()}).insert(ignore_permissions=True)
		email = _contact()
		tags.add(email, slug)
		self.assertEqual((tags.of(email), _links(email)), ([slug], [slug]))

	def test_remove_takes_the_tag_off_both_places(self):
		email = _contact()
		tags.add(email, "a-1", "b-2", "c-3")
		self.assertEqual(tags.remove(email, "b-2", "zz"), ["b-2"])
		self.assertEqual((tags.of(email), _links(email)), (["a-1", "c-3"], ["a-1", "c-3"]))

	def test_contacts_with_reads_exact_tags_and_filters_by_status(self):
		sub, unsub, other = _contact(contacts.SUBSCRIBED), _contact(contacts.UNSUBSCRIBED), _contact(contacts.SUBSCRIBED)
		gun = "gun-" + frappe.generate_hash(length=4)
		tags.add(sub, gun)
		tags.add(unsub, gun)
		tags.add(other, gun + "smith")
		self.assertEqual(tags.contacts_with(gun), sorted([sub, unsub]), "gun does not match gunsmith")
		self.assertEqual(tags.contacts_with(gun, statuses=[contacts.SUBSCRIBED]), [sub])

	def test_public_input_keeps_only_slugs(self):
		self.assertEqual(tags.clean(["new-arrivals", "Bad Tag", "ok-2", "new-arrivals", "a" * 61, "x_y", "", None, 5, "UP"]),
			["new-arrivals", "ok-2"])
		self.assertEqual(tags.clean("new-arrivals"), [])
		self.assertEqual((tags.interest("rifles"), tags.interest("Rifles"), tags.interest("a" * 52)),
			("interest-rifles", None, None))

	def test_the_tags_column_holds_many_tags(self):
		setup.ensure()   # idempotent: already text on a migrated site
		kind = frappe.db.sql("""select data_type from information_schema.columns where table_schema = database()
			and table_name = %s and column_name = '_user_tags'""", f"tab{CONTACT}")[0][0]
		self.assertEqual(kind.lower(), "text")
		email = _contact()
		many = [f"interest-category-number-{i:02d}" for i in range(30)]
		tags.add(email, *many)
		self.assertEqual(tags.of(email), many)

	def test_tagging_1500_contacts(self):
		emails = [_contact() for _ in range(BULK)]
		slug = "bulk-" + frappe.generate_hash(length=6)
		started = time.monotonic()
		for email in emails:
			tags.add(email, slug, tags.NEW_ARRIVALS)
		elapsed = time.monotonic() - started
		print(f"\ntagged {BULK} contacts with 2 tags in {elapsed:.2f}s")
		self.assertEqual(len(tags.contacts_with(slug)), BULK)
		self.assertLess(elapsed, BULK_SECONDS)
