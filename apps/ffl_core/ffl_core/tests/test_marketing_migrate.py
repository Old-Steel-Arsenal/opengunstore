"""The FluentCRM import (ffl_core.marketing.migrate) on a synthetic export of the plugin's real shape:
a dry run writes nothing, the real run writes exactly what the dry run reported, a second run changes
nothing, a suppression on the POS is never loosened, and back-in-stock notes become requests by
product id, link or name (Notified when marked so), the rest reported."""
from __future__ import annotations

import json
import os
import tempfile

import frappe

from ffl_core.marketing import CONTACT, EVENT, REQUEST, contacts, migrate, requests, tags
from ffl_core.tests.crm_support import make_item
from ffl_core.tests.marketing_support import MarketingTestCase, address, events_of, make_customer, requests_of, status_of

DASH = "—"


def _row(email: str, status: str = "subscribed", **over) -> dict:
	row = {"email": email, "first_name": "Pat", "last_name": "Buyer", "status": status, "contact_type": "lead",
		"source": "New Arrivals capture (footer)", "created_at": "2025-03-01 10:00:00", "tags": ["new-arrivals"],
		"lists": ["newsletter"], "hashes": {"managed": "m-" + email.lower()[:12], "plain": "p-" + email.lower()[:12]},
		"unsubscribe_reason": None, "custom": {}, "bis_notes": []}
	row.update(over)
	return row


def _export(rows: list[dict], campaigns: list[dict] | None = None) -> dict:
	return {"exported_at": "2026-11-13T18:00:00Z", "site": "https://shop.example", "fluentcrm_version": "2.9",
		"counts": {"contacts": len(rows)}, "contacts": rows, "campaigns": campaigns or []}


def _counts() -> tuple[int, int, int, int, int]:
	return (frappe.db.count(CONTACT), frappe.db.count(EVENT), frappe.db.count(REQUEST), frappe.db.count("Tag Link"),
		frappe.db.count("Email Unsubscribe"))


class TestCustomerLink(MarketingTestCase):
	def test_an_imported_address_of_a_customer_is_linked_to_it(self):
		email = address("cust")
		customer = make_customer(email)
		migrate.import_document(_export([_row(email)]), write=True)
		self.assertEqual(status_of(email), contacts.SUBSCRIBED)
		self.assertEqual(frappe.db.get_value(CONTACT, email, "customer"), customer.name,
			"the legacy-hash and status saves after the insert must not write the empty link back")


class TestNotes(MarketingTestCase):
	def test_the_three_note_shapes_and_the_notified_marker(self):
		self.assertEqual(migrate.parse_note(f"Asked about: Mosin M91/30 (#4521) {DASH} https://shop.example/p/mosin"),
			{"web_product_id": "4521", "url": "https://shop.example/p/mosin", "label": "Mosin M91/30",
			"notified": False, "notified_at": None})
		self.assertEqual(migrate.parse_note(f"Asked about: SKS Type 56 {DASH} https://shop.example/p/sks"),
			{"web_product_id": "", "url": "https://shop.example/p/sks", "label": "SKS Type 56", "notified": False,
			"notified_at": None})
		self.assertEqual(migrate.parse_note("Asked about: Garand"),
			{"web_product_id": "", "url": "", "label": "Garand", "notified": False, "notified_at": None})
		notified = migrate.parse_note(f"Asked about: Garand {DASH} https://shop.example/p/garand\n"
			f"[Notified 2026-10-01 12:30 {DASH} product #777]")
		self.assertEqual((notified["web_product_id"], notified["notified"], str(notified["notified_at"])),
			("777", True, "2026-10-01 12:30:00"), "a legacy note learns its product id from the notified marker")
		for junk in ("", None, "Called about a rifle", "Asked about:   "):
			self.assertIsNone(migrate.parse_note(junk), junk)


class TestImport(MarketingTestCase):
	def setUp(self):
		super().setUp()
		self.item = make_item("_T-MKT-MIG", woo_product_id="904521").name
		self.sub, self.unsub, self.bounced, self.spam, self.pending = (address("mig") for _ in range(5))
		# Already on the POS: one unsubscribed (the export says subscribed), one subscribed (the export says bounced).
		self.pos_unsub, self.pos_sub = address("mig"), address("mig")
		contacts.get_or_create(self.pos_unsub, source="footer")
		contacts.set_status(self.pos_unsub, contacts.UNSUBSCRIBED, reason="footer link")
		contacts.get_or_create(self.pos_sub, source="footer")
		contacts.set_status(self.pos_sub, contacts.SUBSCRIBED, channel="footer")
		tags.add(self.pos_sub, "in-store")
		rows = [
			_row(self.sub.upper(), tags=["new-arrivals", "back-in-stock", "sent-oct-digest", "Bad Tag"],
				lists=["newsletter", "customers", "VIP List"], custom={"interests": "Rifles, Hand Guns"}, bis_notes=[
					{"description": f"Asked about: Mosin M91/30 (#904521) {DASH} https://shop.example/p/mosin",
						"created_at": "2026-09-01 09:00:00"},
					{"description": f"Asked about: Mosin M91/30 (#904521) {DASH} https://shop.example/p/mosin",
						"created_at": "2026-09-05 09:00:00"},   # the same product again, while open: one request
					{"description": f"Asked about: SKS {DASH} https://shop.example/p/sks\n[Notified 2026-10-01 12:30 {DASH} product #55]",
						"created_at": "2026-08-01 09:00:00"},
					{"description": "Asked about: S&W Model 29", "created_at": "2026-08-02 09:00:00"},
					{"description": "Called the store about a rifle", "created_at": "2026-08-03 09:00:00"},
				]),
			_row(self.unsub, "unsubscribed", unsubscribe_reason="Too many emails & offers", tags=[]),
			_row(self.bounced, "bounced"),
			_row(self.spam, "spammed"),
			_row(self.pending, "pending", lists=["insider-list", "vip-list"], custom={"interests": ["handguns"]}),
			_row(self.pos_unsub, "subscribed"),
			_row(self.pos_sub, "bounced"),
			_row("not an address", "subscribed"),
			_row(self.sub, "subscribed"),   # the same address twice
		]
		campaigns = [{"campaign": "oct-digest", "sent": [self.sub, self.unsub, "stranger@example.net"], "opened": [self.sub]}]
		self.export = _export(rows, campaigns)

	def test_a_dry_run_writes_nothing_and_reports_what_the_real_run_writes(self):
		before = _counts()
		dry = migrate.import_document(self.export, write=False)
		self.assertEqual(_counts(), before)
		self.assertEqual((dry["contacts"], dry["create"], dry["update"], dry["unchanged"]), (9, 5, 2, 0))
		self.assertEqual((dry["invalid"], dry["duplicates"]), (["not an address"], [self.sub]))
		self.assertEqual(dry["file_by_status"], {"bounced": 2, "pending": 1, "spammed": 1, "subscribed": 2, "unsubscribed": 1})
		self.assertEqual(dry["by_status"], {"Bounced": 2, "Complained": 1, "Not Subscribed": 1, "Subscribed": 1,
			"Unsubscribed": 2})
		self.assertEqual(dry["by_tag"]["new-arrivals"], 6)
		self.assertEqual((dry["by_tag"]["customers"], dry["by_tag"]["interest-rifles"], dry["by_tag"]["interest-hand-guns"],
			dry["by_tag"]["interest-handguns"], dry["by_tag"]["insider-list"]), (1, 1, 1, 1, 1))
		self.assertEqual(dry["by_tag"]["vip-list"], 1, "any list that is a slug becomes a tag")
		self.assertNotIn("sent-oct-digest", dry["by_tag"])
		self.assertEqual((dry["skipped_tags"], dry["skipped_lists"]), ({"Bad Tag": 1}, {"VIP List": 1}))
		self.assertEqual(sorted((c["email"], c["pos"], c["import"], c["result"]) for c in dry["conflicts"]), sorted([
			(self.pos_unsub, "Unsubscribed", "Subscribed", "Unsubscribed"),
			(self.pos_sub, "Subscribed", "Bounced", "Bounced")]))
		self.assertEqual(dry["requests"], {"Notified": 1, "Open": 2, "existing": 1})
		self.assertEqual(dry["unparsed_notes"], [{"email": self.sub, "note": "Called the store about a rifle"}])
		self.assertEqual(dry["events"], {"Bounced": 2, "Complained": 1, "Imported": 7, "Opened": 1, "Sent": 2,
			"Subscribed": 1, "Unsubscribed": 1})
		self.assertEqual(dry["campaign_emails_unknown"], 1)

		real = migrate.import_document(self.export, write=True)
		self.assertEqual({**real, "dry_run": True}, dry, "the real run writes exactly what the dry run reported")
		self.assertFalse(real["dry_run"])
		self.assertEqual(frappe.db.count(CONTACT) - before[0], 5)
		self.assertEqual(frappe.db.count(REQUEST) - before[2], 3)
		self.assertEqual(frappe.db.count(EVENT) - before[1], sum(dry["events"].values()))

	def test_what_the_real_run_writes(self):
		migrate.import_document(self.export, write=True)
		doc = frappe.get_doc(CONTACT, self.sub)
		self.assertEqual((doc.status, doc.source, doc.first_name, doc.consent_channel, str(doc.consent_at)),
			(contacts.SUBSCRIBED, migrate.SOURCE, "Pat", migrate.SOURCE, "2025-03-01 10:00:00"))
		self.assertEqual((doc.legacy_hash, doc.legacy_hash_2), ("m-" + self.sub[:12], "p-" + self.sub[:12]))
		self.assertEqual(tags.of(self.sub), ["new-arrivals", "back-in-stock", "newsletter", "customers", "interest-rifles",
			"interest-hand-guns"])
		self.assertEqual([(e.kind, e.campaign) for e in events_of(self.sub) if e.campaign],
			[(contacts.SENT, "oct-digest"), (contacts.OPENED, "oct-digest")])
		unsub = frappe.get_doc(CONTACT, self.unsub)
		self.assertEqual((unsub.status, unsub.status_reason), (contacts.UNSUBSCRIBED, "Too many emails & offers"))
		self.assertEqual((status_of(self.bounced), status_of(self.spam), status_of(self.pending)),
			(contacts.BOUNCED, contacts.COMPLAINED, contacts.NOT_SUBSCRIBED))
		self.assertEqual(frappe.get_all("Email Unsubscribe", filters={"email": self.spam},
			fields=["reference_doctype", "global_unsubscribe"]), [{"reference_doctype": CONTACT, "global_unsubscribe": 0}])
		self.assertEqual(tags.of(self.pending), ["new-arrivals", "insider-list", "vip-list", "interest-handguns"])

		self.assertEqual(status_of(self.pos_unsub), contacts.UNSUBSCRIBED, "the POS's unsubscribe is never loosened")
		self.assertEqual(status_of(self.pos_sub), contacts.BOUNCED, "the export's bounce overrides the POS's subscription")
		self.assertEqual(tags.of(self.pos_sub), ["in-store", "new-arrivals", "newsletter"], "tags are only added")
		self.assertEqual(frappe.db.get_value(CONTACT, self.pos_sub, "source"), "footer", "the first source stays")

		by_label = {r.label: r for r in requests_of(self.sub)}
		self.assertEqual(set(by_label), {"Mosin M91/30", "SKS", "S&W Model 29"})
		mosin, sks, garand = by_label["Mosin M91/30"], by_label["SKS"], by_label["S&W Model 29"]
		self.assertEqual((mosin.status, mosin.web_product_id, mosin.item, mosin.url, str(mosin.requested_at)),
			(requests.OPEN, "904521", self.item, "https://shop.example/p/mosin", "2026-09-01 09:00:00"))
		self.assertEqual((sks.status, sks.web_product_id, sks.url, str(sks.notified_at)),
			(requests.NOTIFIED, "55", "https://shop.example/p/sks", "2026-10-01 12:30:00"))
		self.assertEqual((garand.status, garand.web_product_id, garand.url), (requests.OPEN, None, None))

	def test_a_second_run_changes_nothing(self):
		migrate.import_document(self.export, write=True)
		after_first = _counts()
		statuses = {e: status_of(e) for e in (self.sub, self.unsub, self.pos_unsub, self.pos_sub)}
		again = migrate.import_document(self.export, write=True)
		self.assertEqual(_counts(), after_first)
		self.assertEqual({e: status_of(e) for e in statuses}, statuses)
		self.assertEqual((again["create"], again["update"], again["unchanged"]), (0, 0, 7))
		self.assertEqual((again["events"], again["requests"]), ({}, {"existing": 4}))
		self.assertEqual([(c["email"], c["result"]) for c in again["conflicts"]], [(self.pos_unsub, "Unsubscribed")],
			"the kept unsubscribe is still reported, nothing else differs")

	def test_import_fluentcrm_reads_a_file_and_writes_only_on_an_explicit_zero(self):
		fd, path = tempfile.mkstemp(suffix=".json")
		with os.fdopen(fd, "w") as fh:
			json.dump(self.export, fh)
		self.addCleanup(os.remove, path)
		before = _counts()
		for not_zero in (1, "1", None, "False", "no", True):
			self.assertTrue(migrate.import_fluentcrm(path, dry_run=not_zero)["dry_run"], not_zero)
		self.assertTrue(migrate.import_fluentcrm(path)["dry_run"])
		self.assertEqual(_counts(), before)
		self.assertFalse(migrate.import_fluentcrm(path, dry_run="0")["dry_run"])
		self.assertEqual(status_of(self.spam), contacts.COMPLAINED)

	def test_not_an_export_is_refused(self):
		for bad in ({"error": "fluentcrm_missing"}, {"contacts": "x"}, []):
			with self.assertRaises(frappe.ValidationError):
				migrate.import_document(bad, write=False)
