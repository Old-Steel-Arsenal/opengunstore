"""Website events (ffl_core.marketing.events.receive): accepted / rejected / neither, one savepoint per
event, a repeated id processed once, the off switch, public input capped and filtered, and the two
handlers' records. The endpoint and its role gate: ffl_woo_sync.tests.test_web_events."""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from ffl_core.marketing import CONTACT, EVENT, REQUEST, SETTINGS, contacts, events, requests, tags
from ffl_core.tests.crm_support import make_item
from ffl_core.tests.marketing_support import (MarketingTestCase, address, events_of, make_customer, requests_of,
	status_of)

_n = [0]


def _event(type_: str, data, **over) -> dict:
	_n[0] += 1
	event = {"id": f"{frappe.generate_hash(length=8)}-{_n[0]}", "type": type_,
		"occurred_at": "2026-11-13T18:04:05+00:00", "data": data}
	event.update(over)
	return event


def _subscribed(email: str, **data) -> dict:
	return _event("contact.subscribed", {"email": email, "source": "New Arrivals capture (slidein)", **data})


def _alert(email: str, **data) -> dict:
	return _event("alert.requested", {"email": email, **data})


class TestBatch(MarketingTestCase):
	def test_a_repeated_id_is_accepted_again_and_processed_once(self):
		email = address()
		event = _subscribed(email, tags=["new-arrivals"])
		self.assertEqual(events.receive([event]), {"accepted": [event["id"]], "rejected": []})
		self.assertEqual(events.receive(json.dumps([event, event])), {"accepted": [event["id"]] * 2, "rejected": []})
		(only,) = events_of(email)
		self.assertEqual((only.kind, only.dedupe_key), (contacts.SUBSCRIBED, f"web:{event['id']}"))

	def test_each_refusal_has_its_code_and_does_not_spoil_the_batch(self):
		good, other = address(), address()
		batch = [
			_event("checkout.updated", {"email": good}),
			_subscribed("not-an-address"),
			_alert(good, kind="restock"),
			_alert(good, kind="back_in_stock"),             # names no product
			_alert(good, kind="model", model_label="Mosin"),  # no model key
			_event("contact.subscribed", ["a list"]),
			_subscribed(good),
			_subscribed(other, tags="new-arrivals"),
			_event("contact.subscribed", {"email": good}, id="x" * 81),
		]
		out = events.receive(batch)
		self.assertEqual(out["accepted"], [batch[6]["id"], batch[7]["id"]])
		self.assertEqual([(r["id"], r["reason"]) for r in out["rejected"]], [
			(batch[0]["id"], "unknown_type"), (batch[1]["id"], "invalid_email"), (batch[2]["id"], "invalid_data"),
			(batch[3]["id"], "invalid_data"), (batch[4]["id"], "invalid_data"), (batch[5]["id"], "invalid_data"),
			("x" * 81, "invalid_data")])
		self.assertEqual((status_of(good), status_of(other)), (contacts.SUBSCRIBED, contacts.SUBSCRIBED))
		self.assertFalse(frappe.db.exists(REQUEST, {"contact": good}), "a refused alert leaves nothing")
		self.assertNotIn(good, json.dumps(out), "a refusal never echoes the data")

	def test_an_unexpected_failure_is_in_neither_list_and_leaves_nothing_behind(self):
		bad, good = address(), address()
		real_add = tags.add

		def add(contact, *names):
			real_add(contact, *names)
			if contact == bad:
				raise RuntimeError("tag store down")

		batch = [_subscribed(bad, tags=["new-arrivals"]), _subscribed(good, tags=["new-arrivals"]), {"type": "x"}, "junk"]
		logged = frappe.db.count("Error Log", {"method": "Marketing web event failed"})
		with patch.object(tags, "add", side_effect=add):
			out = events.receive(batch)
		self.assertEqual(out, {"accepted": [batch[1]["id"]], "rejected": []})
		self.assertFalse(frappe.db.exists(CONTACT, bad), "the failed event's writes are rolled back")
		self.assertFalse(frappe.db.exists("Tag Link", {"document_name": bad}))
		self.assertEqual(status_of(good), contacts.SUBSCRIBED)
		self.assertEqual(frappe.db.count("Error Log", {"method": "Marketing web event failed"}), logged + 1)
		log = frappe.get_last_doc("Error Log", filters={"method": "Marketing web event failed"})
		self.assertIn(batch[0]["id"], log.error)
		self.assertNotIn(bad, log.error, "the log names the event, not the visitor")
		# Sent again later, it goes through.
		self.assertEqual(events.receive([batch[0]])["accepted"], [batch[0]["id"]])
		self.assertEqual(status_of(bad), contacts.SUBSCRIBED)

	def test_marketing_off_refuses_the_whole_call(self):
		self.set_single(SETTINGS, "enabled", 0)
		email = address()
		with self.assertRaises(frappe.ValidationError):
			events.receive([_subscribed(email)])
		self.assertFalse(frappe.db.exists(CONTACT, email))

	def test_not_a_list_or_too_many_is_refused(self):
		for bad in ({"id": "1"}, "nonsense", [_subscribed(address()) for _ in range(events.MAX_EVENTS + 1)]):
			with self.assertRaises(frappe.ValidationError):
				events.receive(bad)


class TestContactSubscribed(MarketingTestCase):
	def test_a_new_address_is_subscribed_tagged_and_its_consent_recorded(self):
		email = address()
		event = _subscribed(email.upper(), first_name="Jane", last_name="Doe", page_url="https://shop.example/x",
			ip="203.0.113.7", tags=["new-arrivals", "Bad Tag", "a" * 61], interests=["rifles", "No Good"],
			coupon={"code": "WELCOME-AB12", "amount": 10, "expires_at": "2026-12-13"})
		events.receive([event])
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.status, doc.first_name, doc.last_name, doc.source, doc.consent_channel),
			(contacts.SUBSCRIBED, "Jane", "Doe", "New Arrivals capture (slidein)", "web"))
		self.assertEqual(str(doc.consent_at), str(contacts.to_system_datetime(event["occurred_at"])))
		self.assertEqual(tags.of(email), ["new-arrivals", "interest-rifles"])
		(logged,) = events_of(email)
		detail = json.loads(logged.detail)
		self.assertEqual((detail["page_url"], detail["ip"], detail["coupon"], detail["coupon_amount"]),
			("https://shop.example/x", "203.0.113.7", "WELCOME-AB12", "10"))

	def test_a_new_contact_for_a_customers_address_stays_linked_to_the_customer(self):
		email = address("cust")
		customer = make_customer(email)
		events.receive([_subscribed(email)])
		self.assertEqual(status_of(email), contacts.SUBSCRIBED)
		self.assertEqual(frappe.db.get_value(CONTACT, email, "customer"), customer.name,
			"the status save after the insert must not write the empty link back")

	def test_a_suppressed_contact_stays_suppressed_untagged_and_the_form_is_logged_as_skipped(self):
		for status in (contacts.UNSUBSCRIBED, contacts.BOUNCED, contacts.COMPLAINED):
			with self.subTest(status=status):
				email = address()
				contacts.get_or_create(email)
				contacts.set_status(email, status)
				event = _subscribed(email, tags=["new-arrivals"], interests=["rifles"], page_url="https://shop.example/x",
					ip="203.0.113.7", coupon={"code": "WELCOME-AB12"})
				self.assertEqual(events.receive([event])["accepted"], [event["id"]])
				self.assertEqual(status_of(email), status)
				self.assertEqual(tags.of(email), [], "a suppressed contact is not tagged")
				self.assertIsNone(frappe.db.get_value(CONTACT, email, "consent_at"))
				self.assertEqual(events_of(email, contacts.SUBSCRIBED), [])
				(skipped,) = events_of(email, contacts.SKIPPED)
				self.assertEqual(skipped.dedupe_key, f"web:{event['id']}")
				self.assertEqual(json.loads(skipped.detail), {"reason": "suppressed", "kept_status": status,
					"source": "New Arrivals capture (slidein)", "page_url": "https://shop.example/x", "ip": "203.0.113.7",
					"coupon": "WELCOME-AB12"})
				events.receive([event])   # sent again: nothing more is recorded
				self.assertEqual(len(events_of(email, contacts.SKIPPED)), 1)

	def test_a_contact_already_subscribed_is_tagged_logged_and_its_consent_time_refreshed(self):
		email = address()
		first = _event("contact.subscribed", {"email": email, "source": "Slide-in", "tags": ["new-arrivals"]},
			occurred_at="2026-11-01T10:00:00+00:00")
		again = _event("contact.subscribed", {"email": email, "source": "Footer form", "tags": ["in-store"]},
			occurred_at="2026-11-10T10:00:00+00:00")
		events.receive([first, again])
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.status, doc.consent_channel), (contacts.SUBSCRIBED, "web"))
		self.assertEqual(str(doc.consent_at), str(contacts.to_system_datetime("2026-11-10T10:00:00+00:00")))
		self.assertEqual(tags.of(email), ["new-arrivals", "in-store"])
		logged = events_of(email)
		self.assertEqual([e.kind for e in logged], [contacts.SUBSCRIBED, contacts.SUBSCRIBED])
		self.assertEqual(json.loads(logged[1].detail)["source"], "Footer form")
		self.assertNotIn("kept_status", logged[1].detail)

	def test_public_input_is_stored_as_plain_capped_text_and_urls_must_be_plain_http(self):
		email = address()
		events.receive([_subscribed(email, first_name="S&W " + "J" * 300, last_name="O'Brien", source="x" * 300,
			page_url="javascript:alert(1)", ip="1" * 100, coupon={"code": "A&B<1>"})])
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.first_name, doc.last_name), ("S&W " + "J" * 136, "O'Brien"), "plain text, not escaped")
		self.assertEqual(len(doc.source), 140)
		detail = json.loads(events_of(email)[0].detail)
		self.assertNotIn("page_url", detail)
		self.assertEqual((len(detail["ip"]), detail["coupon"]), (45, "A&B<1>"))

	def test_control_characters_and_newlines_are_removed_from_text(self):
		email = address()
		events.receive([_subscribed(email, first_name="Pat\r\nrick\x00", last_name="\tBuyer\x7f", source="Footer\nform"),
			_alert(email, kind="back_in_stock", product_name="Mosin\nM91/30\x1b", sku="SKU\x00-1")])
		doc = frappe.get_doc(CONTACT, email)
		self.assertEqual((doc.first_name, doc.last_name, doc.source, doc.consent_channel),
			("Patrick", "Buyer", "Footerform", "web"))
		(req,) = requests_of(email)
		self.assertEqual((req.label, req.sku), ("MosinM91/30", "SKU-1"))
		self.assertEqual(json.loads(events_of(email)[0].detail)["source"], "Footerform")
		for bad in ("javascript:alert(1)", "/relative", 'https://e.example/"onmouseover=x', "https://e.example/a b",
				"https://e.example/" + "u" * 500):
			self.assertEqual(events.url(bad), "", bad)
		self.assertEqual(events.url(" HTTPS://e.example/p?a=1&b=2 "), "HTTPS://e.example/p?a=1&b=2")


class TestAlertRequested(MarketingTestCase):
	def test_a_back_in_stock_request_names_the_product_once_while_open(self):
		item = make_item("_T-MKT-BIS", woo_product_id="880123").name
		email = address()
		first = _alert(email, kind="back_in_stock", product_id=880123, sku="WEB-SKU-1", product_name="Mosin M91/30 & bayonet",
			url="https://shop.example/p/mosin")
		again = _alert(email, kind="back_in_stock", product_id=880123, product_name="Mosin M91/30")
		self.assertEqual(events.receive([first, again])["accepted"], [first["id"], again["id"]])
		(req,) = requests_of(email)
		self.assertEqual((req.kind, req.status, req.item, req.web_product_id, req.sku, req.label, req.url),
			(requests.BACK_IN_STOCK, requests.OPEN, item, "880123", "WEB-SKU-1", "Mosin M91/30 & bayonet",
				"https://shop.example/p/mosin"))
		self.assertEqual([e.kind for e in events_of(email)], [contacts.REQUESTED, contacts.REQUESTED])
		self.assertEqual(status_of(email), contacts.NOT_SUBSCRIBED, "asking for an alert is not a subscription")
		self.assertEqual(frappe.db.get_value(CONTACT, email, "source"), "alert")

	def test_a_model_alert_by_key_and_no_new_arrivals_tag(self):
		email = address()
		events.receive([_alert(email, kind="model", model_key="mosin-nagant", model_label="Mosin-Nagant")])
		(req,) = requests_of(email)
		self.assertEqual((req.kind, req.model_key, req.label, req.item), (requests.MODEL_ALERT, "mosin-nagant", "Mosin-Nagant", None))
		self.assertEqual(tags.of(email), [])

	def test_the_item_is_found_from_the_sku(self):
		item = make_item("_T-MKT-SKU").name
		email = address()
		events.receive([_alert(email, kind="back_in_stock", sku=f"{item}::SER-1", product_name="A gun")])
		self.assertEqual(requests_of(email)[0].item, item)

	def test_an_existing_subscriber_keeps_their_status(self):
		email = address()
		contacts.get_or_create(email, source="footer")
		contacts.set_status(email, contacts.SUBSCRIBED)
		events.receive([_alert(email, kind="model", model_key="sks")])
		self.assertEqual((status_of(email), frappe.db.get_value(CONTACT, email, "source")), (contacts.SUBSCRIBED, "footer"))
		self.assertEqual(frappe.db.count(EVENT, {"contact": email}), 2)
