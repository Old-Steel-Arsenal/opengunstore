"""A website sell-your-gun request (ffl_core.crm.sell_requests): its own deal, the gun as fields and a card,
the photos copied in, the offer, and the gun received through the register's own trade-in.

Real documents on the real database. Only the boundaries are replaced: the website's HTTP answers
(``requests.get``), FastBound (as in test_trade_in) and the job queue, whose recorded jobs are run by hand
as the worker would after the commit."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe

from ffl_core.crm import inquiries, sell_requests, setup
from ffl_core.crm.inquiries import EVENT_FIELD
from ffl_core.tests.crm_support import CRMTestCase
from ffl_core.tests.test_crm_team_mail import _assignees, _customer_contact

S = sell_requests
DEAL = "CRM Deal"
STORE = "https://shop.example.com"
SECRET = "test-secret-123"


def _secret(value: str) -> None:
	from frappe.utils.password import set_encrypted_password

	set_encrypted_password("WooCommerce Settings", "WooCommerce Settings", value, "outbound_webhook_secret")
_n = [0]


def _email() -> str:
	_n[0] += 1
	return f"sell-{_n[0]}-{frappe.generate_hash(length=6)}@example.org"


def _details(**over) -> dict:
	details = {"Reference": "OSA-T1A2B3", "Type": "Pistol", "Condition": "Very good", "Manufacturer": "Glock",
		"Model": "19 Gen 5", "Caliber": "9mm", "Original box": "Yes", "Serial number": "(not provided)",
		"Accessories": "(none listed)", "Prefers": "Phone call", "Address": "1 Sample Rd, Springfield TX 75000",
		"Barrel": "4 in"}
	details.update(over)
	return details


def _sell(email: str | None = None, **over) -> dict:
	_n[0] += 1
	args = {"kind": "sell_gun", "event_id": f"evt-sell-{_n[0]}-{frappe.generate_hash(length=6)}",
		"email": email or _email(), "full_name": "Pat Seller", "phone": "555-0100", "message": "Grandpa's gun.",
		"details": _details(), "photos": [f"{STORE}/wp-admin/admin-ajax.php?action=p&filename=a.jpg&sig=x"]}
	args.update(over)
	with patch.object(frappe, "enqueue"):
		return inquiries.file_inquiry(**args)


def _notes(deal: str) -> list:
	return frappe.get_all("FCRM Note", filters={"reference_doctype": DEAL, "reference_docname": deal},
		fields=["title", "content", EVENT_FIELD], order_by="creation asc")


def _png() -> bytes:
	from io import BytesIO

	from PIL import Image

	out = BytesIO()
	Image.new("RGB", (2, 2)).save(out, "PNG")
	return out.getvalue()


def _response(status=200, ctype="image/png", body=None):
	body = _png() if body is None else body
	r = MagicMock()
	r.status_code = status
	r.headers = {"Content-Type": ctype}
	r.raw.read.side_effect = lambda n, decode_content=True: body[:n]
	r.__enter__ = lambda self: self
	r.__exit__ = lambda self, *a: None
	return r


class SellCase(CRMTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup.ensure_custom_fields()
		frappe.clear_cache(doctype=DEAL)

	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Lead Mail Settings", "crm_pos_link", 1)
		frappe.db.set_single_value("WooCommerce Settings", "store_url", STORE)
		_secret(SECRET)


class TheRequestIsItsOwnDeal(SellCase):
	def test_a_customer_with_an_open_deal_gets_a_new_sell_deal_with_the_fields(self):
		email = _email()
		contact = _customer_contact(email)
		from ffl_core.crm.deals import open_deal_for

		buying = open_deal_for(contact)
		out = _sell(email)
		self.assertEqual((out["doctype"], out["created"]), (DEAL, True))
		deal = frappe.get_doc(DEAL, out["name"])
		self.assertNotEqual(deal.name, buying, "never filed on the deal the customer is buying on")
		self.assertEqual(deal.get(S.SELL), 1)
		self.assertTrue(deal.erpnext_customer, "matched to their customer as before")
		self.assertEqual((deal.lead_name, deal.first_name), ("Sell: Glock 19 Gen 5 · Known", "Known"),
			"titled by the gun and the person; the name fields stay the person's")
		got = {label: deal.get(field) for label, field in S.FIELDS.items()}
		self.assertEqual(got["Manufacturer"], "Glock")
		self.assertEqual(got["Model"], "19 Gen 5")
		self.assertEqual(got["Condition"], "Very good")
		self.assertEqual(got["Reference"], "OSA-T1A2B3")
		self.assertIsNone(got["Serial number"], "a blank the form spelled out is no value")
		self.assertIsNone(got["Accessories"])
		self.assertEqual(deal.deal_value, 0)
		self.assertEqual(deal.products, [])
		(note,) = _notes(deal.name)
		self.assertEqual(note.title, "Sell-your-gun request OSA-T1A2B3")
		self.assertTrue(note[EVENT_FIELD].startswith("evt-sell-"))
		self.assertIn("Grandpa", note.content)
		self.assertIn("<b>Barrel</b>: 4 in", note.content, "an unknown label stays in the note")
		self.assertNotIn("Manufacturer", note.content)
		self.assertTrue(_assignees(DEAL, deal.name), "shared with the team")
		self.assertEqual(json.loads(deal.get(S.PHOTOS))[0]["url"][:len(STORE)], STORE)

	def test_two_requests_two_deals_and_a_repeat_makes_none(self):
		email = _email()
		first = _sell(email, event_id="evt-sell-repeat")
		deals = frappe.db.count(DEAL)
		again = _sell(email, event_id="evt-sell-repeat")
		self.assertEqual((again["name"], again["created"], frappe.db.count(DEAL)), (first["name"], False, deals))
		other = _sell(email)
		self.assertNotEqual(other["name"], first["name"])

	def test_a_stranger_gets_a_deal_and_a_contact_not_a_lead(self):
		email = _email()
		out = _sell(email, full_name="<b>Eve</b> Sample")
		self.assertEqual(out["doctype"], DEAL)
		self.assertFalse(frappe.db.exists("CRM Lead", {"email": email}))
		self.assertEqual(frappe.db.get_value(DEAL, out["name"], "email"), email)
		contact = frappe.get_doc(DEAL, out["name"]).contacts[0].contact
		self.assertEqual(frappe.db.get_value("Contact", contact, ["first_name", "last_name"]), ("b", "Eve /b Sample"),
			"a name Frappe refuses in a document name loses its angle brackets (web_orders._contact_of)")

	def test_the_title_leaves_out_what_is_missing_and_the_naming_patch_keeps_it(self):
		from ffl_core.patches import name_crm_deals

		self.assertEqual(frappe.db.get_value(DEAL, _sell(details=_details(Model=""))["name"], "lead_name"),
			"Sell: Glock · Pat Seller")
		bare = _sell(details=_details(Manufacturer="(not provided)", Model=""))["name"]
		self.assertEqual(frappe.db.get_value(DEAL, bare, "lead_name"), "Sell · Pat Seller")
		name_crm_deals.execute()
		self.assertEqual(frappe.db.get_value(DEAL, bare, ["lead_name", "first_name", "last_name"]),
			("Sell · Pat Seller", "Pat", "Seller"))

	def test_photos_are_queued_after_the_commit(self):
		with patch.object(frappe, "enqueue") as enqueue:
			out = inquiries.file_inquiry(kind="sell_gun", event_id="evt-sell-q-" + frappe.generate_hash(length=6),
				email=_email(), details=_details(), photos=[f"{STORE}/a.jpg"])
		(call,) = [c for c in enqueue.call_args_list if c.args[0] == S.PHOTO_JOB]
		self.assertEqual((call.kwargs["deal"], call.kwargs["enqueue_after_commit"]), (out["name"], True))


class TheCard(SellCase):
	def test_every_value_is_escaped_and_copied_photos_are_thumbnails(self):
		deal = _sell(details=_details(Model="<script>x</script>", Accessories="<img src=x onerror=1>"))["name"]
		doc = frappe.get_doc(DEAL, deal)
		doc.set(S.PHOTOS, json.dumps([{"url": f"{STORE}/a.jpg", "file": "/private/files/a.jpg"},
			{"url": f'{STORE}/b.jpg"><script>'}]))
		html = S.card(doc)["html"]
		self.assertNotIn("<script>", html)
		self.assertNotIn("<img src=x", html)
		self.assertIn("&lt;script&gt;", html)
		self.assertIn('<img src="/private/files/a.jpg"', html)
		self.assertIn("Photo 2", html, "a photo not copied is a link")
		self.assertIn("Prefers Phone call", html)


class PhotoCopy(SellCase):
	def _deal(self, *urls) -> str:
		deal = _sell(photos=list(urls))["name"]
		return deal

	def test_only_the_store_host_is_fetched_and_copied_private(self):
		ours, other = f"{STORE}/wp-admin/admin-ajax.php?f=1", "https://evil.example.net/a.jpg"
		deal = self._deal(ours, other, "https://shop.example.com:8443/a.jpg")
		with patch("requests.get", return_value=_response()) as get:
			made = S.copy_photos(deal)
		self.assertEqual([c.args[0] for c in get.call_args_list], [ours], "no other host, no other port")
		self.assertEqual(get.call_args.kwargs["allow_redirects"], False)
		(url,) = made
		f = frappe.get_doc("File", {"file_url": url})
		self.assertEqual((f.is_private, f.attached_to_doctype, f.attached_to_name), (1, DEAL, deal))
		photos = S.photos_of(frappe.get_doc(DEAL, deal))
		self.assertEqual([bool(p.get("file")) for p in photos], [True, False, False])
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", f"%photos not copied yet: {deal}%"]}))
		with patch("requests.get") as get:   # run again: what was copied is not fetched again
			S.copy_photos(deal)
		self.assertEqual(get.call_count, 0)

	def test_a_refused_answer_keeps_the_link_and_logs_once(self):
		deal = self._deal(f"{STORE}/1.jpg", f"{STORE}/2.jpg", f"{STORE}/3.jpg", f"{STORE}/4.jpg")
		answers = [_response(status=403, ctype="text/html"), _response(status=302), _response(ctype="text/html"),
			_response(body=b"x" * (S.MAX_BYTES + 5))]
		logs = frappe.db.count("Error Log")
		with patch("requests.get", side_effect=answers):
			self.assertEqual(S.copy_photos(deal), [])
		self.assertEqual(frappe.db.count("Error Log"), logs + 1)
		self.assertTrue(all(not p.get("file") for p in S.photos_of(frappe.get_doc(DEAL, deal))))

	def test_no_store_url_fetches_nothing(self):
		deal = self._deal(f"{STORE}/1.jpg")
		frappe.db.set_single_value("WooCommerce Settings", "store_url", "")
		with patch("requests.get") as get:
			S.copy_photos(deal)
		get.assert_not_called()


class SignedPhotoRequest(SellCase):
	def test_the_signature_is_the_websites(self):
		self.assertEqual(S._signature(SECRET, "1700000000", "sell-abc 1.jpg"),
			"cd87cad2cb35199d519cc2f69a3a5b7d743c05bd9dcc64313a047020ee262bd6")

	def test_the_link_is_asked_for_as_received_with_the_signature_of_its_decoded_filename(self):
		url = f"{STORE}/wp-admin/admin-ajax.php?action=ffl_core_sell_photo&filename=sell-abc%201.jpg&sig=h"
		deal = _sell(photos=[url])["name"]
		with patch("requests.get", return_value=_response()) as get, patch.object(S.time, "time", return_value=1700000000.7):
			S.copy_photos(deal)
		self.assertEqual(get.call_args.args[0], url)
		self.assertEqual(get.call_args.kwargs["headers"], {"x-osa-timestamp": "1700000000",
			"x-osa-signature": "cd87cad2cb35199d519cc2f69a3a5b7d743c05bd9dcc64313a047020ee262bd6"})
		self.assertEqual(get.call_args.kwargs["allow_redirects"], False)

	def test_a_blank_secret_fetches_nothing_and_logs_once(self):
		deal = _sell(photos=[f"{STORE}/1.jpg"])["name"]
		_secret("")
		logs = frappe.db.count("Error Log")
		with patch("requests.get") as get:
			S.copy_photos(deal)
			S.copy_photos(deal)
		get.assert_not_called()
		self.assertEqual(frappe.db.count("Error Log"), logs + 1)
		with patch.object(frappe, "enqueue"):
			self.assertIn(deal, S.retry_photos(), "still waiting: tried again daily")

	def test_403_until_the_website_checks_signatures_then_copied_without_a_step(self):
		deal = _sell(photos=[f"{STORE}/1.jpg"])["name"]
		logs = frappe.db.count("Error Log")
		for _day in range(3):
			with patch("requests.get", return_value=_response(status=403, ctype="text/html")):
				self.assertEqual(S.copy_photos(deal), [])
		self.assertEqual(frappe.db.count("Error Log"), logs + 1, "one log, not one a day")
		with patch.object(frappe, "enqueue") as enqueue:
			self.assertIn(deal, S.retry_photos())
		self.assertEqual(enqueue.call_args.kwargs["deal"], deal)
		with patch("requests.get", return_value=_response()):
			(made,) = S.copy_photos(deal)
		self.assertTrue(made.startswith("/private/files/"))
		with patch.object(frappe, "enqueue"):
			self.assertNotIn(deal, S.retry_photos(), "nothing left to copy")

	def test_404_is_final(self):
		deal = _sell(photos=[f"{STORE}/1.jpg"])["name"]
		logs = frappe.db.count("Error Log")
		with patch("requests.get", return_value=_response(status=404, ctype="text/html")):
			S.copy_photos(deal)
		self.assertEqual(frappe.db.count("Error Log"), logs, "a removed photo is no failure to report")
		with patch("requests.get") as get:
			S.copy_photos(deal)
		get.assert_not_called()
		with patch.object(frappe, "enqueue"):
			self.assertNotIn(deal, S.retry_photos())
		self.assertIn("Photo 1", S.card(frappe.get_doc(DEAL, deal))["html"], "the link stays")


class OfferAndReceipt(SellCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site

		dev_site.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in these tests"))
		p.start()
		self.addCleanup(p.stop)

	def _deal(self, **details):
		email = _email()
		_customer_contact(email)
		return frappe.get_doc(DEAL, _sell(email, details=_details(**details))["name"])

	def test_the_offer_is_its_own_field_never_the_deal_value(self):
		deal = self._deal()
		with self.assertRaises(frappe.ValidationError):
			S.accepted(deal)   # no offer yet
		S.make_offer(deal, 425)
		with self.assertRaises(frappe.ValidationError):
			S.make_offer(deal, 0)
		row = frappe.db.get_value(DEAL, deal.name, [S.OFFER, "deal_value"], as_dict=True)
		self.assertEqual((row[S.OFFER], row.deal_value), (425, 0))
		deal.reload()
		S.accepted(deal)
		first = frappe.db.get_value(DEAL, deal.name, S.ACCEPTED)
		self.assertTrue(first)
		frappe.db.set_value(DEAL, deal.name, S.ACCEPTED, "2026-01-02")
		deal.reload()
		S.accepted(deal)
		self.assertEqual(str(frappe.db.get_value(DEAL, deal.name, S.ACCEPTED)), "2026-01-02", "the first day stays")
		self.assertIn("waiting for the gun", S.card(deal)["html"])
		deal.save(ignore_permissions=True)   # a later save keeps the value off the Won board
		self.assertEqual(frappe.db.get_value(DEAL, deal.name, "deal_value"), 0)

	def test_open_order_and_hold_are_refused(self):
		from ffl_core.crm import customer, holds

		deal = self._deal()
		with self.assertRaisesRegex(frappe.ValidationError, "offered to the store"):
			customer.open_order(deal)
		with self.assertRaisesRegex(frappe.ValidationError, "offered to the store"):
			holds.hold_guns(deal, ["X"])

	def test_the_intake_payload_and_nothing_is_booked_before_the_trade_in(self):
		deal = self._deal(**{"Serial number": "SR-T-1", "Type": "Receiver / Frame", "Condition": "Poor"})
		S.make_offer(deal, 300)
		deal.reload()
		counts = lambda: [frappe.db.count(d) for d in ("Stock Entry", "FFL Acquisition", "Purchase Receipt", "Serial No")]  # noqa: E731
		before = counts()
		got = S.intake(deal)
		self.assertEqual(got, {"deal": deal.name, "source_name": deal.erpnext_customer, "manufacturer": "Glock",
			"model_name": "19 Gen 5", "caliber": "9mm", "firearm_type": "Receiver", "serial_no": "SR-T-1",
			"acquisition_cost": 300}, "no condition: staff grade the gun in hand, never from the seller's words")
		self.assertEqual(counts(), before)
		deal.db_set("erpnext_customer", None)
		with self.assertRaisesRegex(frappe.ValidationError, "customer"):
			S.intake(deal)

	def _trade_in(self, deal, suffix: str, **over):
		from ffl_core.api import trade_in

		payload = {"serial_no": f"SR-{suffix}", "manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
			"firearm_type": "Pistol", "condition_grade": "Good", "id_type": "Driver License", "id_number": f"DL-{suffix}",
			"source_name": deal.erpnext_customer, "source_first_name": "Pat", "source_last_name": "Seller",
			"source_address": "1 Sample Rd, Springfield TX 75000", "acquisition_cost": 300, "payout_method": "Cash",
			"crm_deal": deal.name, **over}
		with patch.object(frappe, "enqueue") as enqueue:
			result = trade_in.create_trade_in_intake(payload)
		jobs = [c.kwargs for c in enqueue.call_args_list if c.args and c.args[0] == S.LINK_JOB]
		return result, jobs

	def test_a_paid_trade_in_links_and_wins_the_deal_after_it_commits(self):
		deal = self._deal()
		S.make_offer(deal, 300)
		result, (job,) = self._trade_in(deal, self._testMethodName[-12:])
		self.assertTrue(job.pop("enqueue_after_commit"))
		self.assertNotEqual(frappe.db.get_value("CRM Deal Status", frappe.db.get_value(DEAL, deal.name, "status"), "type"),
			"Won", "nothing on the deal inside the trade-in's transaction")
		job.pop("queue")
		self.assertTrue(S.link_intake(**job))
		row = frappe.db.get_value(DEAL, deal.name, ["status", S.ACQUISITION, S.RECEIPT, "deal_value"], as_dict=True)
		self.assertEqual(frappe.db.get_value("CRM Deal Status", row.status, "type"), "Won")
		self.assertEqual((row[S.ACQUISITION], row[S.RECEIPT]), (result["acquisition"], result["purchase_receipt"]))
		self.assertEqual(row.deal_value, 0, "buying is no sale")
		self.assertIn("Received", S.card(frappe.get_doc(DEAL, deal.name))["html"])

	def test_a_credit_trade_in_wins_the_deal_when_its_acquisition_is_booked(self):
		deal = self._deal()
		result, (job,) = self._trade_in(deal, self._testMethodName[-12:], apply_credit=1)
		job.pop("enqueue_after_commit"), job.pop("queue")
		self.assertTrue(S.link_intake(**job))
		status = lambda: frappe.db.get_value("CRM Deal Status", frappe.db.get_value(DEAL, deal.name, "status"), "type")  # noqa: E731
		self.assertNotEqual(status(), "Won", "not booked until checkout")
		frappe.db.set_value("FFL Acquisition", result["acquisition"], "docstatus", 1)   # what checkout's submit does
		with patch.object(frappe, "enqueue") as enqueue:
			S.acquisition_submitted(frappe.get_doc("FFL Acquisition", result["acquisition"]))
		(call,) = enqueue.call_args_list
		S.win_if_booked(call.kwargs["deal"])
		self.assertEqual(status(), "Won")

	def test_a_trade_in_from_another_seller_is_not_linked(self):
		deal = self._deal()
		other = frappe.get_doc({"doctype": "Customer", "customer_name": "_T Sell Other", "customer_type": "Individual"}
			).insert(ignore_permissions=True).name
		_result, (job,) = self._trade_in(deal, self._testMethodName[-12:], source_name=other)
		job.pop("enqueue_after_commit"), job.pop("queue")
		self.assertFalse(S.link_intake(**job))
		self.assertFalse(frappe.db.get_value(DEAL, deal.name, S.ACQUISITION))


class Upgrade(SellCase):
	def _legacy(self, lead: str, details: dict | None, event: str) -> str:
		content = inquiries._note_html("555-0100", "Old request", "", "", details or {},
			[f"{STORE}/a.jpg"] if details else [])
		return frappe.get_doc({"doctype": "FCRM Note", "title": f"Website sell-your-gun request ({event})",
			"content": content, "reference_doctype": "CRM Lead", "reference_docname": lead}).insert(ignore_permissions=True).name

	def test_old_notes_become_sell_deals_once_and_bad_ones_are_listed(self):
		from ffl_core.crm.mail import _new_lead
		from ffl_core.patches import crm_sell_request_deals

		email = _email()
		lead = _new_lead(email, "Old Seller")
		good = self._legacy(lead, _details(), "evt-old-sell")
		bad = self._legacy(lead, None, "evt-old-bad")
		logs = frappe.db.count("Error Log")
		with patch.object(frappe, "enqueue") as enqueue:
			crm_sell_request_deals.execute()
			crm_sell_request_deals.execute()
		note = frappe.get_doc("FCRM Note", good)
		self.assertEqual((note.reference_doctype, note.title, note.get(EVENT_FIELD)),
			(DEAL, "Sell-your-gun request OSA-T1A2B3", "evt-old-sell"))
		deal = frappe.get_doc(DEAL, note.reference_docname)
		self.assertEqual((deal.get(S.SELL), deal.get(S.FIELDS["Model"]), deal.email), (1, "19 Gen 5", email))
		self.assertEqual(frappe.db.count(DEAL, {S.SELL: 1, "email": email}), 1, "a second run makes no second deal")
		self.assertEqual([c.kwargs["deal"] for c in enqueue.call_args_list if c.args[0] == S.PHOTO_JOB], [deal.name])
		self.assertEqual(frappe.db.get_value("FCRM Note", bad, ["reference_doctype", "title"]),
			("CRM Lead", "Website sell-your-gun request (evt-old-bad)"))
		self.assertEqual(frappe.db.count("Error Log"), logs + 2, "one list per run")


class Setup(SellCase):
	def test_view_card_section_and_buttons(self):
		frappe.db.set_global(setup.SELL_VIEW_FLAG, None)
		frappe.db.delete("CRM View Settings", {"label": setup.SELL_VIEW})
		setup.ensure_sell_view()
		setup.ensure_sell_view()
		view = frappe.get_all("CRM View Settings", filters={"label": setup.SELL_VIEW, "public": 1},
			fields=["filters", "columns"])
		self.assertEqual(len(view), 1)
		self.assertEqual(json.loads(view[0].filters), {S.SELL: 1})
		keys = [c["key"] for c in json.loads(view[0].columns)]
		self.assertEqual(keys[1:7], [S.FIELDS["Manufacturer"], S.FIELDS["Model"], S.FIELDS["Caliber"],
			S.FIELDS["Condition"], S.OFFER, "status"])
		js = setup.deal_script_js(False)
		for name in ("make_sell_offer", "sell_offer_accepted", "sell_gun_intake", "point-of-sale?ffl_crm_deal=", S.CARD):
			self.assertIn(name, js)
		self.assertEqual(frappe.get_meta(DEAL).get_field(S.CARD).depends_on, f"eval:doc.{S.SELL}")
