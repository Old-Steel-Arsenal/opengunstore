"""Unsubscribing (ffl_core.marketing.unsubscribe, www/unsubscribe) and tracking (ffl_core.marketing.track):
the token, the page (GET changes nothing, POST unsubscribes), one-click, old links by hash, bad links, a
page that never says whether an address is on file, the open pixel recorded once, and click links that
redirect only where they were signed to."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.website.serve import get_response, get_response_content

from ffl_core.marketing import SETTINGS, contacts, track
from ffl_core.marketing import unsubscribe as unsubscribe_page
from ffl_core.marketing.contacts import (BOUNCED, CLICKED, NOT_SUBSCRIBED, OPENED, SUBSCRIBED, UNSUBSCRIBED,
	set_status)
from ffl_core.marketing.mailer import MARKETING, send
from ffl_core.marketing.signing import CLICK, contact_token, email_of, sign
from ffl_core.marketing.unsubscribe import unsubscribe
from ffl_core.tests.marketing_mail_support import MailTestCase, contact
from ffl_core.tests.marketing_support import address, events_of, status_of

ASK = "Stop marketing emails from"
DONE = "You are unsubscribed"
INVALID = "This unsubscribe link is not valid"


class TestToken(MailTestCase):
	def test_a_token_stands_for_its_address_and_nothing_else(self):
		email = address()
		token = contact_token(email)
		head, signature = token.split(".")
		self.assertEqual(len(signature), 32)
		self.assertEqual(email_of(token), email)
		other = contact_token(address())
		for bad in (f"{head}.{other.split('.')[1]}", f"{other.split('.')[0]}.{signature}", head, token + "0",
				f"{head}.{signature[:-1]}x", "", None, 42, "a.b.c", "!!!." + signature):
			with self.subTest(bad=bad):
				self.assertIsNone(email_of(bad))

	def test_the_key_comes_from_the_site_encryption_key(self):
		email = address()
		token = contact_token(email)
		with _conf(encryption_key="another-site-key"):
			self.assertIsNone(email_of(token), "another site's key does not verify this site's token")
		self.assertEqual(email_of(token), email)


class _conf:
	"""Swap a site_config value for the duration of a with block."""

	def __init__(self, **values):
		self.values = values

	def __enter__(self):
		self.saved = {k: frappe.local.conf.get(k) for k in self.values}
		frappe.local.conf.update(self.values)

	def __exit__(self, *exc):
		frappe.local.conf.update(self.saved)


class TestUnsubscribePage(MailTestCase):
	def _page(self, method: str, form: dict, user: str = "Guest") -> str:
		with self.web_request(method, "/unsubscribe", form, user):
			return get_response_content("/unsubscribe")

	def test_get_shows_a_button_and_changes_nothing(self):
		email = contact(SUBSCRIBED)
		html = self._page("GET", {"t": contact_token(email)})
		self.assertIn(ASK, html)
		self.assertIn('<form method="post">', html)
		self.assertIn(f'name="t" value="{contact_token(email)}"', html)
		self.assertEqual(status_of(email), SUBSCRIBED)
		self.assertEqual(events_of(email, UNSUBSCRIBED), [])

	def test_post_unsubscribes_with_the_reason(self):
		email = contact(SUBSCRIBED)
		html = self._page("POST", {"t": contact_token(email), "reason": "too_many"})
		self.assertIn(DONE, html)
		self.assertEqual(status_of(email), UNSUBSCRIBED)
		self.assertEqual(frappe.db.get_value("Marketing Contact", email, "status_reason"), "Too many emails")
		detail = frappe.parse_json(events_of(email, UNSUBSCRIBED)[0].detail)
		self.assertEqual((detail["via"], detail["reason"]), ("page", "too_many"))
		self.assertTrue(frappe.db.exists("Email Unsubscribe", {"email": email, "reference_name": email, "global_unsubscribe": 0}))

	def test_one_click_post_needs_no_page_and_no_session(self):
		email = contact(NOT_SUBSCRIBED)
		html = self._page("POST", {"t": contact_token(email), "List-Unsubscribe": "One-Click"})
		self.assertIn(DONE, html)
		self.assertEqual(status_of(email), UNSUBSCRIBED)
		self.assertEqual(frappe.parse_json(events_of(email, UNSUBSCRIBED)[0].detail)["via"], "one_click")

	def test_an_unknown_reason_is_dropped(self):
		email = contact(SUBSCRIBED)
		self._page("POST", {"t": contact_token(email), "reason": "<script>"})
		self.assertEqual(frappe.db.get_value("Marketing Contact", email, "status_reason"), "Unsubscribe link")

	def test_a_suppressed_contact_stays_where_it_is(self):
		email = contact(BOUNCED)
		self.assertIn(DONE, self._page("POST", {"t": contact_token(email)}))
		self.assertEqual(status_of(email), BOUNCED, "Bounced outranks Unsubscribed")

	def test_bad_links(self):
		email = contact(SUBSCRIBED)
		token = contact_token(email)
		for form in ({}, {"t": token[:-1] + ("0" if token[-1] != "0" else "1")}, {"t": "garbage"},
				{"legacy": "short"}, {"legacy": "0" * 32}, {"legacy": "<script>alert(1)</script>"}):
			for method in ("GET", "POST"):
				with self.subTest(form=form, method=method):
					html = self._page(method, form)
					self.assertIn(INVALID, html)
					self.assertNotIn("<script>alert", html)
		self.assertEqual(status_of(email), SUBSCRIBED)

	def test_an_address_with_no_contact_looks_the_same(self):
		email = address("nobody")
		self.assertIn(ASK, self._page("GET", {"t": contact_token(email)}))
		self.assertIn(DONE, self._page("POST", {"t": contact_token(email)}))
		self.assertFalse(frappe.db.exists("Marketing Contact", email), "nothing is created")

	def test_old_links_find_the_contact_by_either_hash(self):
		for field in ("legacy_hash", "legacy_hash_2"):
			with self.subTest(field=field):
				email = contact(SUBSCRIBED)
				legacy = frappe.generate_hash(length=32)
				frappe.db.set_value("Marketing Contact", email, field, legacy, update_modified=False)
				html = self._page("GET", {"legacy": legacy})
				self.assertIn(ASK, html)
				self.assertIn(f'name="legacy" value="{legacy}"', html)
				self.assertEqual(status_of(email), SUBSCRIBED)
				self.assertIn(DONE, self._page("POST", {"legacy": legacy}))
				self.assertEqual(status_of(email), UNSUBSCRIBED)
				self.assertEqual(frappe.parse_json(events_of(email, UNSUBSCRIBED)[0].detail)["via"], "legacy_link")

	def test_the_store_name_is_escaped(self):
		from ffl_core.utils import default_company

		frappe.db.set_value("Company", default_company(), "company_name", "<b>S&W</b> Guns", update_modified=False)
		html = self._page("GET", {"t": contact_token(address())})
		self.assertIn("&lt;b&gt;S&amp;W&lt;/b&gt; Guns", html)
		self.assertNotIn("<b>S&W</b>", html)

	def test_a_signed_in_browser_gets_the_csrf_token_in_the_form(self):
		with self.web_request("GET", "/unsubscribe", {"t": contact_token(address())}, "Administrator"):
			frappe.local.session.data.csrf_token = "csrf-of-this-session"   # a real session has one
			html = get_response_content("/unsubscribe")
		self.assertIn('name="csrf_token" value="csrf-of-this-session"', html)
		self.assertNotIn('name="csrf_token"', self._page("GET", {"t": contact_token(address())}))

	def test_a_signed_token_is_never_rate_limited_but_guessing_is(self):
		limit = unsubscribe_page.LIMIT
		first, last = contact(SUBSCRIBED), contact(SUBSCRIBED)
		with self.web_request("POST", "/unsubscribe", {}):   # one IP address for all of it
			frappe.local.request_ip = f"readers-{frappe.generate_hash(length=10)}"
			for _ in range(limit + 5):   # a provider's one-click posts, and readers: all from few addresses
				self.assertEqual(unsubscribe_page.page({"t": contact_token(first)}, "POST"), {"state": "done"})
			self.assertEqual(unsubscribe_page.page({"t": contact_token(last)}, "GET")["state"], "ask")
			self.assertEqual(unsubscribe_page.page({"t": contact_token(last)}, "POST"), {"state": "done"})
			self.assertEqual((status_of(first), status_of(last)), (UNSUBSCRIBED, UNSUBSCRIBED))
		with self.web_request("GET", "/unsubscribe", {}):
			frappe.local.request_ip = f"guesser-{frappe.generate_hash(length=10)}"   # the counter lives in redis, an hour
			for _ in range(limit):
				self.assertEqual(unsubscribe_page.page({"legacy": "0" * 32}, "GET"), {"state": "invalid"})
			with self.assertRaises(frappe.RateLimitExceededError):
				unsubscribe_page.page({"legacy": "0" * 32}, "GET")
			with self.assertRaises(frappe.RateLimitExceededError):
				unsubscribe_page.page({"t": "not.a-token"}, "POST")
			self.assertEqual(unsubscribe_page.page({"t": contact_token(address())}, "GET")["state"], "ask",
				"a valid token still gets through from an address that has been guessing")

	def test_unsubscribe_ignores_an_address_with_no_contact(self):
		self.assertFalse(unsubscribe(address("nobody")))
		self.assertFalse(unsubscribe(None))


class TestOpen(MailTestCase):
	def setUp(self):
		super().setUp()
		self.set_single(SETTINGS, "track_opens", 1)
		self.email = contact(SUBSCRIBED)
		send(self.email, "test", "Hi", "test", {"sent_by": "x", "link": "https://shop.example.com/"},
			category=MARKETING, campaign="c1")

	def test_the_first_open_is_recorded_once(self):
		token = contact_token(self.email)
		for _ in range(3):
			response = track.open(c="c1", t=token)
			self.assertEqual((response.status_code, response.mimetype, response.get_data()), (200, "image/gif", track.PIXEL))
		self.assertIn("no-store", response.headers["Cache-Control"])
		events = events_of(self.email, OPENED)
		self.assertEqual([(e.campaign, e.dedupe_key) for e in events], [("c1", f"opened:c1:{self.email}")])

	def test_nothing_is_recorded_for_a_bad_token_or_a_campaign_not_sent(self):
		other = contact(SUBSCRIBED)
		for c, t in (("c1", "bad.token"), ("c2", contact_token(self.email)), ("c1", contact_token(other)), (None, None)):
			with self.subTest(c=c, t=t):
				self.assertEqual(track.open(c=c, t=t).get_data(), track.PIXEL)
		self.assertEqual(events_of(self.email, OPENED) + events_of(other, OPENED), [])

	def test_switched_off_the_pixel_still_answers_and_records_nothing(self):
		self.set_single(SETTINGS, "track_opens", 0)
		self.assertEqual(track.open(c="c1", t=contact_token(self.email)).get_data(), track.PIXEL)
		self.assertEqual(events_of(self.email, OPENED), [])


class TestClick(MailTestCase):
	URL = "https://shop.example.com/p/123?utm=mail&x=1"

	def setUp(self):
		super().setUp()
		self.set_single(SETTINGS, "track_clicks", 1)
		self.email = contact(SUBSCRIBED)
		send(self.email, "test", "Hi", "test", {"sent_by": "x", "link": self.URL}, category=MARKETING, campaign="c1")

	def _link(self) -> dict:
		link = track.click_url(self.URL, self.email, "c1")
		return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}

	def test_a_signed_link_redirects_and_the_first_click_is_recorded(self):
		args = self._link()
		self.assertEqual(args["u"], self.URL)
		for _ in range(2):
			response = track.click(**args)
			self.assertEqual((response.status_code, response.headers["Location"]), (302, self.URL))
		events = events_of(self.email, CLICKED)
		self.assertEqual(len(events), 1)
		self.assertEqual(frappe.parse_json(events[0].detail)["url"], self.URL)

	def test_a_link_that_is_not_signed_for_its_target_goes_nowhere(self):
		args = self._link()
		for change in ({"u": "https://evil.example.net/"}, {"s": "0" * 32}, {"s": None}, {"u": self.URL + "&y=2"}):
			with self.subTest(change=change):
				response = track.click(**{**args, **change})
				self.assertEqual(response.status_code, 400)
				self.assertNotIn("Location", response.headers)
		self.assertEqual(events_of(self.email, CLICKED), [])

	def test_only_web_addresses_even_when_signed(self):
		for target in ("javascript:alert(1)", "data:text/html,x", "//evil.example.net/", "ftp://x.example/",
				"https://x.example/\" onmouseover=\"x", "https://x.example/\r\nSet-Cookie: a=b", "https:///nohost", "https://x.example/`x"):
			with self.subTest(target=target):
				response = track.click(u=target, c="c1", t=contact_token(self.email), s=sign(CLICK, target))
				self.assertEqual(response.status_code, 400)
				self.assertNotIn("Location", response.headers)

	def test_a_bad_token_still_follows_a_signed_link_and_records_nothing(self):
		args = {**self._link(), "t": "bad.token"}
		self.assertEqual(track.click(**args).status_code, 302)
		self.assertEqual(events_of(self.email, CLICKED), [])

	def test_untracked_links_are_left_alone(self):
		follow = track.tracker(self.email, "c1", clicks=True)
		self.assertTrue(follow(self.URL).startswith(frappe.utils.get_url(track.CLICK_PATH)))
		self.assertEqual(follow("mailto:shop@example.com"), "mailto:shop@example.com")
		self.assertEqual(track.tracker(self.email, "c1", clicks=False)(self.URL), self.URL)
