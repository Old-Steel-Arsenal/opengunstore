"""The marketing mailer (ffl_core.marketing.mailer): who each category reaches, one mail per campaign and
address, the claim rolled back with a mail that was not queued, the List-Unsubscribe headers on the queued
row, Frappe's second gate, the shell and its escaping, the refusals, the test email endpoint, and the scan
that keeps every other module off ``reference_doctype="Marketing Contact"``. Real queue rows throughout."""
from __future__ import annotations

import ast
import os
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import frappe

from ffl_core.marketing import CONTACT, SETTINGS, mailer, site_url, track
from ffl_core.marketing.track import click_url, open_url
from ffl_core.marketing.unsubscribe import url as unsubscribe_url
from ffl_core.marketing.contacts import (BOUNCED, COMPLAINED, NOT_SUBSCRIBED, SENT, SKIPPED, STATUSES, SUBSCRIBED,
	UNSUBSCRIBED, set_status)
from ffl_core.marketing.mailer import MARKETING, SERVICE, MarketingMailError, restore_list_unsubscribe, send
from ffl_core.marketing.signing import contact_token
from ffl_core.tests.crm_support import user
from ffl_core.tests.marketing_mail_support import (ACCOUNT, SENDER, SITE_ADDRESS, MailTestCase, contact, head_and_body,
	html_of, queue_rows, queue_rows_to, text_of)
from ffl_core.tests.marketing_support import address, events_of, status_of

CTX = {"sent_by": "tester", "link": "https://shop.example.com/new?a=1&b=2"}
MANAGER = "mkt-mail-manager@example.com"
CLERK = "mkt-mail-clerk@example.com"


def _send(email: str, category: str = MARKETING, campaign: str = "c1", context: dict | None = None) -> bool:
	return send(email, "test", "Hello", "test", CTX if context is None else context, category=category, campaign=campaign)


class TestWhoIsReached(MailTestCase):
	def test_each_category_against_each_status(self):
		expected = {MARKETING: {SUBSCRIBED}, SERVICE: {SUBSCRIBED, NOT_SUBSCRIBED}}
		for category in (MARKETING, SERVICE):
			for status in STATUSES:
				with self.subTest(category=category, status=status):
					email = contact(status)
					sent = _send(email, category)
					self.assertEqual(sent, status in expected[category])
					self.assertEqual(len(queue_rows(email)), int(sent))
					kinds = [e.kind for e in events_of(email) if e.kind in (SENT, SKIPPED)]
					self.assertEqual(kinds, [SENT] if sent else [SKIPPED])
					if not sent:
						self.assertIn('"reason": "status"', events_of(email, SKIPPED)[0].detail)

	def test_the_same_campaign_reaches_an_address_once(self):
		email = contact(SUBSCRIBED)
		self.assertTrue(_send(email, campaign="digest-1"))
		self.assertFalse(_send(email, campaign="digest-1"), "already sent in this campaign")
		self.assertEqual(len(queue_rows(email)), 1)
		self.assertEqual([e.dedupe_key for e in events_of(email, SENT)], [f"sent:digest-1:{email}"])
		self.assertTrue(_send(email, campaign="digest-2"), "another campaign is another mail")
		self.assertEqual(len(queue_rows(email)), 2)

	def test_the_sent_event_points_at_its_queue_row(self):
		email = contact(SUBSCRIBED)
		_send(email)
		event = events_of(email, SENT)[0]
		self.assertEqual((event.campaign, frappe.parse_json(event.detail)["queue"]), ("c1", queue_rows(email)[0].name))

	def test_an_unknown_address_is_an_error_not_a_skip(self):
		with self.assertRaises(frappe.DoesNotExistError):
			_send(address("nobody"))


class TestClaimAndQueueTogether(MailTestCase):
	def test_a_mail_that_was_not_queued_leaves_no_claim_and_can_be_sent_again(self):
		email = contact(SUBSCRIBED)
		with patch.object(frappe, "sendmail", side_effect=RuntimeError("queue down")) as sendmail:
			with self.assertRaises(RuntimeError):
				_send(email)
		sendmail.assert_called_once()
		self.assertEqual(events_of(email, SENT), [], "no 'Sent' without a queued mail")
		self.assertEqual(queue_rows(email), [])
		self.assertTrue(_send(email), "the retry sends")
		self.assertEqual((len(events_of(email, SENT)), len(queue_rows(email))), (1, 1))

	def test_a_template_that_fails_to_render_leaves_no_claim(self):
		email = contact(SUBSCRIBED)
		with self.assertRaises(Exception):   # the test template needs sent_by and link (StrictUndefined)
			_send(email, context={})
		self.assertEqual(events_of(email, SENT), [])
		self.assertEqual(queue_rows(email), [])

	def test_frappes_second_gate_drops_the_mail_and_the_claim(self):
		# Subscribed on the contact, but an Email Unsubscribe for it (a race with an unsubscribe).
		email = contact(SUBSCRIBED)
		frappe.get_doc({"doctype": "Email Unsubscribe", "email": email, "reference_doctype": CONTACT,
			"reference_name": email, "global_unsubscribe": 0}).insert(ignore_permissions=True)
		self.assertFalse(_send(email))
		self.assertEqual(queue_rows(email), [])
		self.assertEqual(events_of(email, SENT), [], "the claim is rolled back with it")
		self.assertIn("dropped_by_queue", events_of(email, SKIPPED)[0].detail)


class TestSecondGate(MailTestCase):
	"""A suppressed contact's reference-level Email Unsubscribe stops mail on the contact, never other mail
	to the same address (a receipt, an agreement link)."""

	def test_mail_on_the_contact_is_dropped_and_other_mail_to_the_address_is_not(self):
		email = contact(SUBSCRIBED)
		set_status(email, UNSUBSCRIBED)
		on_contact = frappe.sendmail(recipients=[email], sender=SENDER, subject="Sale", message="<p>Sale</p>",
			reference_doctype=CONTACT, reference_name=email, delayed=True)
		self.assertFalse(on_contact, "Frappe drops the recipient at queue time")
		self.assertEqual(queue_rows(email), [])
		receipt = frappe.sendmail(recipients=[email], sender=SENDER, subject="Your receipt",
			message="<p>Thanks</p>", reference_doctype="User", reference_name="Administrator", delayed=True)
		self.assertTrue(receipt)
		self.assertIn(receipt.name, queue_rows_to(email))
		plain = frappe.sendmail(recipients=[email], sender=SENDER, subject="Agreement", message="<p>Sign</p>", delayed=True)
		self.assertTrue(plain)
		self.assertFalse(frappe.db.exists("Email Unsubscribe", {"email": email, "global_unsubscribe": 1}))


class TestHeaders(MailTestCase):
	def test_the_queued_row_has_list_unsubscribe_and_one_click(self):
		email = contact(SUBSCRIBED)
		_send(email)
		row = queue_rows(email)[0]
		head, _ = head_and_body(row.message)
		lines = head.split("\r\n")
		url = f"<{frappe.utils.get_url('/unsubscribe')}?t={contact_token(email)}>"
		self.assertIn(f"List-Unsubscribe: {url}", lines)
		self.assertIn("List-Unsubscribe-Post: List-Unsubscribe=One-Click", lines)
		self.assertNotIn("X-List-Unsubscribe", head)
		self.assertTrue(row.sender.endswith(f"<{SENDER}>"))
		self.assertIn("/unsubscribe?t=", html_of(row.message), "the footer link is the same address")

	def test_the_text_part_ends_with_the_store_and_a_written_out_unsubscribe_link(self):
		from ffl_core.store_identity import company_brand
		from ffl_core.utils import default_company

		email = contact(SUBSCRIBED, first_name="Pat")
		_send(email, context={**CTX, "link": "https://shop.example.com/new?a=1&b=2"})
		text = text_of(queue_rows(email)[0].message)
		brand = company_brand(default_company())
		self.assertIn("Hello Pat,", text, "the body text is what Frappe derives from the HTML")
		self.assertFalse(brand["phone"], "the test store has no phone, so the store block is name + address")
		self.assertTrue(text.endswith(f"\n\n{brand['name']}\n{brand['address_line']}\n\nUnsubscribe: {unsubscribe_url(email)}"), text)
		self.assertEqual(text.count("Unsubscribe:"), 1, "the shell's footer is not repeated")
		self.assertNotIn("<", text)

	def test_only_the_header_names_change_and_only_on_marketing_rows(self):
		head = ("Content-Type: multipart/mixed\r\nX-List-Unsubscribe: <https://pos/unsubscribe?t=a.b>\r\n"
			"X-List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\nX-Frappe-Site: https://pos\r\nSubject: x")
		body = "\r\n\r\n--b\r\nX-List-Unsubscribe: in the body stays\r\nX-List-Unsubscribe-Post: so does this\r\n"
		doc = frappe._dict(reference_doctype=CONTACT, message=head + body)
		restore_list_unsubscribe(doc)
		new_head, new_body = doc.message.split("\r\n\r\n", 1)
		self.assertEqual("\r\n\r\n" + new_body, body, "the body is untouched")
		self.assertEqual(new_head, head.replace("X-List-Unsubscribe", "List-Unsubscribe"))
		self.assertIn("X-Frappe-Site", new_head)
		other = frappe._dict(reference_doctype="Sales Invoice", message=head + body)
		restore_list_unsubscribe(other)
		self.assertEqual(other.message, head + body, "other mail keeps Frappe's X- headers")

	def test_other_mail_asking_for_the_header_keeps_the_x_prefix(self):
		email = address("mail")
		queued = frappe.sendmail(recipients=[email], sender=SENDER, subject="x", message="<p>x</p>",
			reference_doctype="User", reference_name="Administrator", email_headers={"List-Unsubscribe": "<https://x>"})
		head, _ = head_and_body(frappe.db.get_value("Email Queue", queued.name, "message"))
		self.assertIn("X-List-Unsubscribe: <https://x>", head.split("\r\n"))

	def test_the_hook_is_registered_beside_the_crm_one(self):
		from ffl_core.tests.test_crm_setup import _hooks_py

		events = _hooks_py()["doc_events"]["Email Queue"]
		self.assertEqual(events["before_insert"], "ffl_core.marketing.mailer.restore_list_unsubscribe")
		self.assertEqual(events["after_insert"], "ffl_core.crm.mail.send_crm_mail_now")


class TestShell(MailTestCase):
	def _html(self, email: str, context: dict | None = None) -> str:
		return mailer.render("test", CTX if context is None else context, email=email, campaign="c1", subject="S&W <sale>")

	def test_the_shell_carries_the_store_and_the_unsubscribe_link(self):
		from ffl_core.store_identity import company_brand
		from ffl_core.utils import default_company

		email = contact(SUBSCRIBED)
		brand = company_brand(default_company())
		html = self._html(email)
		self.assertTrue(brand["address_line"], "the test company has an address (test_bootstrap)")
		self.assertIn(frappe.utils.escape_html(brand["address_line"]), html)
		self.assertIn(f'href="{frappe.utils.get_url("/unsubscribe")}?t={contact_token(email)}"', html)
		self.assertIn("<title>S&amp;W &lt;sale&gt;</title>", html)
		self.assertNotIn("<img", html.split("<body", 1)[1].split("</td>", 1)[0], "no logo: the name as text")
		self.assertNotIn("fonts.googleapis", html)

	def test_a_logo_is_shown_when_the_company_has_one(self):
		from ffl_core.utils import default_company

		frappe.db.set_value("Company", default_company(), "company_logo", "/files/store-logo.png")
		html = self._html(contact(SUBSCRIBED))
		self.assertIn(f'src="{frappe.utils.get_url("/files/store-logo.png")}"', html)

	def test_every_value_is_escaped(self):
		email = contact(SUBSCRIBED, first_name="O'Brien", last_name="S&W")
		html = self._html(email, {"sent_by": "<b>x</b> S&W O'Brien", "link": "https://shop.example.com/?a=1&b=2"})
		self.assertIn("Hello O&#39;Brien,", html)
		self.assertIn("by &lt;b&gt;x&lt;/b&gt; S&amp;W O&#39;Brien.", html)
		self.assertNotIn("<b>x</b>", html)
		self.assertIn('href="https://shop.example.com/?a=1&amp;b=2"', html, "no click tracking: the link as is, escaped")

	def test_tracking_follows_the_settings(self):
		email = contact(SUBSCRIBED)
		html = self._html(email)
		self.assertNotIn("marketing.track", html)
		self.set_single(SETTINGS, "track_opens", 1)
		self.set_single(SETTINGS, "track_clicks", 1)
		html = self._html(email)
		self.assertIn("ffl_core.marketing.track.open?c=c1&amp;t=", html)
		self.assertIn("ffl_core.marketing.track.click?u=https%3A%2F%2Fshop.example.com", html)

	def test_the_queued_mail_is_our_html_not_frappes_wrapper(self):
		email = contact(SUBSCRIBED, first_name="Pat")
		_send(email)
		html = html_of(queue_rows(email)[0].message)
		self.assertIn("Hello Pat,", html)
		self.assertNotIn("email-container", html, "Frappe's standard wrapper is not used")


class TestRefusals(MailTestCase):
	def test_switched_off(self):
		self.set_single(SETTINGS, "enabled", 0)
		with self.assertRaises(MarketingMailError):
			_send(contact(SUBSCRIBED))

	def test_no_account_or_an_account_that_does_not_send(self):
		email = contact(SUBSCRIBED)
		self.set_single(SETTINGS, "email_account", None)
		with self.assertRaises(MarketingMailError):
			_send(email)
		self.set_single(SETTINGS, "email_account", ACCOUNT)
		frappe.db.set_value("Email Account", ACCOUNT, "enable_outgoing", 0, update_modified=False)
		with self.assertRaises(MarketingMailError):
			_send(email)
		self.assertEqual(events_of(email, SENT), [])

	def test_a_store_without_an_address(self):
		with patch("ffl_core.marketing.mailer.company_brand", return_value={"name": "Store", "address_line": ""}):
			with self.assertRaises(MarketingMailError):
				_send(contact(SUBSCRIBED))

	def test_a_background_send_without_a_site_address_is_refused(self):
		"""No web request and no host_name: Frappe would build ``http://<site name>`` links nobody can open."""
		email = contact(SUBSCRIBED)
		self.set_site_address(None)
		self.assertFalse(frappe.request)
		with self.assertRaises(MarketingMailError) as raised:
			_send(email)
		self.assertIn("host_name", str(raised.exception))
		self.assertEqual((events_of(email, SENT), queue_rows(email)), ([], []), "nothing claimed, nothing queued")
		self.set_site_address(SITE_ADDRESS)
		self.assertTrue(_send(email), "the same send goes once the site has an address")

	def test_a_send_from_a_web_request_needs_no_host_name(self):
		"""A dev site, or the test email pressed in the Desk: the request says where the site is."""
		email = contact(SUBSCRIBED)
		self.set_site_address(None)
		with self.web_request("POST", "/api/method/x", {}):
			frappe.local.request.environ["HTTP_HOST"] = "pos.dev.test"
			self.assertTrue(_send(email))
			self.assertEqual(urlsplit(site_url("/unsubscribe")).hostname, "pos.dev.test")
		html = html_of(queue_rows(email)[0].message)
		self.assertRegex(html, r'href="http://pos\.dev\.test(:\d+)?/unsubscribe\?t=')

	def test_every_marketing_link_is_built_from_the_site_address(self):
		email = contact(SUBSCRIBED)
		for link, path in ((site_url("/unsubscribe"), "/unsubscribe"), (unsubscribe_url(email), "/unsubscribe"),
				(open_url(email, "c1"), track.OPEN_PATH), (click_url("https://shop.example.com/", email, "c1"), track.CLICK_PATH)):
			with self.subTest(path=path):
				parts = urlsplit(link)
				self.assertEqual((parts.scheme, parts.hostname, parts.path), ("https", "pos.store.test", path))

	def test_bad_arguments(self):
		email = contact(SUBSCRIBED)
		for kwargs in ({"category": "bulk"}, {"campaign": ""}, {"campaign": "two\nlines"}, {"campaign": "x" * 81}):
			with self.subTest(**kwargs), self.assertRaises(frappe.ValidationError):
				send(email, "test", "Hi", "test", CTX, **{"category": MARKETING, "campaign": "c1", **kwargs})
		for template in ("../base", "nope", "base.html"):
			with self.subTest(template=template), self.assertRaises(frappe.ValidationError):
				send(email, "test", "Hi", template, CTX, category=MARKETING, campaign="c1")
		self.assertEqual(queue_rows(email), [])


class TestSendTestEmail(MailTestCase):
	def test_a_manager_gets_a_test_at_their_own_address(self):
		from ffl_core.api.marketing_mail import send_test_email

		manager = user(MANAGER, "Sales Manager")
		frappe.set_user(manager)
		result = send_test_email()
		frappe.set_user("Administrator")
		self.assertEqual((result["queued"], result["email"], result["status"]), (True, MANAGER, NOT_SUBSCRIBED))
		self.assertEqual(len(queue_rows(MANAGER)), 1)
		frappe.set_user(manager)
		self.assertTrue(send_test_email()["queued"], "each test is its own campaign")
		frappe.set_user("Administrator")

	def test_an_unsubscribed_address_gets_no_test(self):
		from ffl_core.api.marketing_mail import send_test_email

		manager = user(MANAGER, "Sales Manager")
		frappe.set_user(manager)
		send_test_email()
		frappe.set_user("Administrator")
		set_status(MANAGER, UNSUBSCRIBED)
		frappe.set_user(manager)
		result = send_test_email()
		frappe.set_user("Administrator")
		self.assertEqual((result["queued"], result["status"]), (False, UNSUBSCRIBED))

	def test_staff_without_a_manager_role_are_refused(self):
		from ffl_core.api.marketing_mail import send_test_email

		frappe.set_user(user(CLERK, "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			send_test_email()
		frappe.set_user("Administrator")
		self.assertEqual(queue_rows(CLERK), [])


# The scan: no module outside ffl_core.marketing sends on a Marketing Contact.
_SENDERS = {"sendmail", "QueueBuilder", "make"}
_REF_KWARGS = {"reference_doctype", "doctype"}


def marketing_sends(source: str) -> list[int]:
	"""Line numbers of mail calls (``sendmail`` / ``QueueBuilder`` / Communication ``make``) whose reference
	doctype is ``"Marketing Contact"`` or the ``CONTACT`` constant."""
	lines = []
	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.Call):
			continue
		func = node.func
		name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
		if name not in _SENDERS:
			continue
		for kw in node.keywords:
			value = kw.value
			if kw.arg in _REF_KWARGS and (
				(isinstance(value, ast.Constant) and value.value == CONTACT)
				or (isinstance(value, ast.Name) and value.id == "CONTACT")
				or (isinstance(value, ast.Attribute) and value.attr == "CONTACT")):
				lines.append(node.lineno)
	return lines


class TestOnlyTheMailerSends(MailTestCase):
	def test_no_module_outside_marketing_sends_on_a_marketing_contact(self):
		apps = Path(frappe.get_app_path("ffl_core")).parent.parent
		offenders = []
		for app in ("ffl_core", "ffl_integrations", "ffl_woo_sync"):
			root = apps / app / app
			for path in root.rglob("*.py"):
				rel = path.relative_to(root)
				if rel.parts[0] == "marketing" or "tests" in rel.parts or path.name.startswith("test_"):
					continue
				offenders += [f"{app}/{rel}:{n}" for n in marketing_sends(path.read_text(encoding="utf-8"))]
		self.assertEqual(offenders, [], "send marketing mail through ffl_core.marketing.mailer.send")

	def test_the_scan_catches_each_form(self):
		for code in ('frappe.sendmail(recipients=[e], reference_doctype="Marketing Contact", reference_name=e)',
				"sendmail(recipients=[e], doctype=CONTACT, name=e)",
				"QueueBuilder(recipients=[e], reference_doctype=marketing.CONTACT).process()",
				'make(doctype="Marketing Contact", name=e, send_email=1)'):
			with self.subTest(code=code):
				self.assertEqual(marketing_sends(code), [1])
		self.assertEqual(marketing_sends('frappe.sendmail(recipients=[e], reference_doctype="Customer")'), [])
		self.assertTrue(marketing_sends(Path(mailer.__file__).read_text()), "the mailer itself is what it finds")
