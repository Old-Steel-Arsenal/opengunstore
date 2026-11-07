"""Team mail that is not a customer inquiry: other mail, its place in the lists, the spam folder, and
the shared views."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe

from ffl_core.api.crm import crm_list_data
from ffl_core.crm import mail, setup
from ffl_core.tests.crm_support import CLERK, user
from ffl_core.tests.test_crm_team_mail import INFO, SALES, TeamMailCase, _customer_contact, _pull, _raw, _settings, _stored
from ffl_core.tests.test_extension_hooks import hooks

HERE = "ffl_core.tests.test_crm_other_mail"
ANSWERS: list = []
ASKED: list = []


def provider(mail_data):
	ASKED.append(mail_data)
	return ANSWERS[0] if ANSWERS else None


def _answer(category: str | None):
	ANSWERS.clear()
	ASKED.clear()
	if category:
		ANSWERS.append({"customer": category == "customer", "category": category, "reason": "Because."})


def _lead(email: str) -> dict | None:
	return frappe.db.get_value("CRM Lead", {"email": email}, ["name", "status", mail.CATEGORY_FIELD], as_dict=True)


class FakeIMAP:
	"""An IMAP folder: uid → raw mail. Records whether anything but a read-only look happened."""

	def __init__(self, mails: dict[int, bytes], validity: str = "7"):
		self.mails, self.validity, self.readonly, self.fetches = mails, validity, None, []

	def select(self, folder, readonly=False):
		self.readonly = readonly
		return "OK", [b"1"]

	def status(self, folder, what):
		return "OK", [f'"{folder}" (UIDVALIDITY {self.validity} UIDNEXT {max(self.mails or [0]) + 1})'.encode()]

	def uid(self, command, *args):
		if command == "search":
			first = int(args[1].split()[1].split(":")[0])
			newest = max(self.mails or [0])
			found = [u for u in sorted(self.mails) if u >= first] or ([newest] if newest else [])
			return "OK", [" ".join(map(str, found)).encode()]
		self.fetches.append(args[1])
		return "OK", [(b"header", self.mails[int(args[0])]), b")"]


def _server(imap: FakeIMAP):
	from frappe.email.receive import EmailServer

	server = MagicMock()
	server.imap = imap
	server.parse_imap_response = lambda cmd, response: EmailServer.parse_imap_response(None, cmd, response)
	return server


def _check(imap: FakeIMAP) -> None:
	with patch("frappe.email.doctype.email_account.email_account.EmailAccount.get_incoming_server",
			return_value=_server(imap)), patch.object(mail, "commit"), hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]):
		mail.check_spam_folder(INFO)


class TestOtherMail(TeamMailCase):
	def setUp(self):
		super().setUp()
		_settings(category_rules=[{"domain": "gunbroker.com", "category": "GunBroker"}])

	def test_mail_that_is_not_an_inquiry_is_kept_under_its_sender(self):
		for i, (sender, category) in enumerate((("deals@spam.example.net", "Blocked"), ("colleague@example.com", "Our store"),
				("no-reply@example.org", "Automated"), ("alerts@gunbroker.com", "GunBroker"))):
			raw = _raw(sender, SALES)
			_pull(INFO, (raw, 20 + i))
			lead = _lead(sender)
			self.assertEqual((lead.status, lead.get(mail.CATEGORY_FIELD)), (mail.OTHER_MAIL, category), sender)
			self.assertEqual(_stored(raw)[0].reference_name, lead.name)
		self.assertNoPullErrors()

	def test_a_sender_has_one_lead(self):
		_pull(INFO, (_raw("no-reply@example.org", SALES), 20))
		_pull(INFO, (_raw("no-reply@example.org", SALES), 21))
		self.assertEqual(frappe.db.count("CRM Lead", {"email": "no-reply@example.org"}), 1)

	def test_a_staff_mailbox_still_leaves_it_out(self):
		from ffl_core.tests.test_crm_team_mail import STAFF_A

		raw = _raw("no-reply@example.org", "ffltm-staff-a@example.com")
		_pull(STAFF_A, (raw, 20))
		self.assertFalse(_stored(raw))
		self.assertFalse(_lead("no-reply@example.org"))

	def test_a_customer_at_a_listed_domain_stays_a_customer(self):
		_customer_contact("buyer@gunbroker.com")
		raw = _raw("buyer@gunbroker.com", SALES)
		_pull(INFO, (raw, 20))
		self.assertEqual(_stored(raw)[0].reference_doctype, "CRM Deal")
		self.assertFalse(_lead("buyer@gunbroker.com"))

	def test_changing_the_status_makes_an_ordinary_lead(self):
		_pull(INFO, (_raw("no-reply@example.org", SALES), 20))
		lead = frappe.get_doc("CRM Lead", _lead("no-reply@example.org").name)
		lead.status = "New"
		lead.save(ignore_permissions=True)
		self.assertFalse(lead.get(mail.CATEGORY_FIELD))
		_pull(INFO, (_raw("no-reply@example.org", SALES), 21))
		self.assertEqual(_lead("no-reply@example.org").status, "New", "the next mail does not put it back")

	def test_only_managers_see_it_until_it_is_an_ordinary_lead(self):
		from ffl_core.tests.crm_support import MANAGER
		from ffl_core.tests.test_crm_team_mail import _can_read

		user(CLERK, "Sales User")
		user(MANAGER, "Sales Manager")
		_pull(INFO, (_raw("no-reply@example.org", SALES), 20))
		name = _lead("no-reply@example.org").name
		self.assertFalse(_can_read(CLERK, "CRM Lead", name))
		self.assertTrue(_can_read(MANAGER, "CRM Lead", name))
		lead = frappe.get_doc("CRM Lead", name)
		lead.status = "New"
		lead.save(ignore_permissions=True)
		self.assertTrue(_can_read(CLERK, "CRM Lead", name))

	def test_a_known_lead_at_a_rule_domain_keeps_direct_mail(self):
		from ffl_core.tests.test_crm_team_mail import STAFF_A

		lead = mail._new_lead("rep@gunbroker.com", "A Rep")
		raw = _raw("rep@gunbroker.com", "ffltm-staff-a@example.com")
		_pull(STAFF_A, (raw, 20))
		self.assertEqual(_stored(raw)[0].reference_name, lead)

	def test_the_ai_verdict_makes_other_mail_with_its_category(self):
		_answer("notification")
		raw = _raw("orders@carrier.example.org", SALES)
		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue"):
			_pull(INFO, (raw, 20))
			self.assertFalse(_lead("orders@carrier.example.org"), "not before the verdict")
			mail.screen_new_sender(_stored(raw)[0].name)
		lead = _lead("orders@carrier.example.org")
		self.assertEqual((lead.status, lead.get(mail.CATEGORY_FIELD)), (mail.OTHER_MAIL, "Notification"))
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": _stored(raw)[0].name,
			"content": ["like", mail.NOT_A_CUSTOMER + "%"]}))

	def test_a_lost_race_for_the_lead_number_keeps_the_verdict(self):
		# Another sender's job took the lead number first: the database drops our transaction (1020).
		_answer("notification")
		raw = _raw("orders@shop.example.org", SALES)
		real_route, calls = mail.route, []

		def route(*args, **kwargs):
			calls.append(1)
			if len(calls) == 1:
				raise frappe.QueryDeadlockError("Record has changed since last read in table 'tabSeries'")
			return real_route(*args, **kwargs)

		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue"):
			_pull(INFO, (raw, 20))
			with patch.object(mail, "route", side_effect=route), patch.object(frappe.db, "rollback"):
				mail.screen_new_sender(_stored(raw)[0].name)
		lead = _lead("orders@shop.example.org")
		self.assertEqual((lead.status, lead.get(mail.CATEGORY_FIELD)), (mail.OTHER_MAIL, "Notification"))
		self.assertEqual(len(calls), 2)

	def test_our_own_store_notice_is_not_filed_under_the_customer(self):
		from email.message import EmailMessage
		from email.utils import format_datetime

		from frappe.utils import now_datetime

		company = frappe.get_all("Company", pluck="company_name", limit=1)[0]
		buyer = "web-buyer@example.org"
		_customer_contact(buyer)
		msg = EmailMessage()
		msg["From"] = f"{company} <shop@mailer.example.net>"
		msg["Reply-To"] = buyer
		msg["To"] = SALES
		msg["Subject"] = "New order: #1001"
		msg["Date"] = format_datetime(now_datetime().astimezone())
		msg["Message-ID"] = f"<ffl-test-{frappe.generate_hash(length=12)}@example.org>"
		msg.set_content("You've got a new order.")
		raw = msg.as_bytes()
		_pull(INFO, (raw, 20))
		self.assertFalse(_stored(raw), "not on the buyer's deal")
		self.assertFalse(_lead(buyer))
		self.assertNoPullErrors()

	def test_no_ai_verdict_makes_an_ordinary_lead(self):
		_answer(None)
		raw = _raw("buyer@example.org", SALES)
		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue"):
			_pull(INFO, (raw, 20))
			mail.screen_new_sender(_stored(raw)[0].name)
		self.assertEqual(_lead("buyer@example.org").status, "New")


class TestLists(TeamMailCase):
	def _names(self, **kwargs) -> list[str]:
		rows = {"columns": json.dumps([{"key": "name"}]), "rows": json.dumps(["name"]), "page_length": 200,
			"page_length_count": 200, "order_by": "modified desc", "view_type": "list", **kwargs}
		return [row["name"] for row in crm_list_data(**rows)["data"]]

	def setUp(self):
		super().setUp()
		_pull(INFO, (_raw("no-reply@example.org", SALES), 20))
		_pull(INFO, (_raw("buyer@example.org", SALES), 21))
		self.other, self.ordinary = _lead("no-reply@example.org").name, _lead("buyer@example.org").name

	def test_the_leads_list_leaves_other_mail_out(self):
		names = self._names(doctype="CRM Lead", default_filters=json.dumps({"converted": 0}))
		self.assertIn(self.ordinary, names)
		self.assertNotIn(self.other, names)

	def test_the_other_mail_view_shows_only_it(self):
		names = self._names(doctype="CRM Lead", filters=json.dumps({"status": mail.OTHER_MAIL}))
		self.assertIn(self.other, names)
		self.assertNotIn(self.ordinary, names)

	def test_a_follow_up_is_mine_or_nobodys_and_waiting(self):
		user(CLERK, "Sales User", "Sales Manager")
		mine = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Mine", "email": "mine@example.org", "lead_owner": CLERK}
			).insert(ignore_permissions=True).name
		theirs = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Theirs", "email": "theirs@example.org",
			"lead_owner": "Administrator"}).insert(ignore_permissions=True).name
		frappe.db.set_value("CRM Lead", [mine, theirs, self.ordinary], "communication_status", "Open")
		view = frappe.get_doc("CRM View Settings", {"dt": "CRM Lead", "label": setup.FOLLOW_UPS, "public": 1})
		frappe.set_user(CLERK)
		try:
			names = self._names(doctype="CRM Lead", filters=view.filters, default_filters=json.dumps({"converted": 0}))
		finally:
			frappe.set_user("Administrator")
		self.assertIn(mine, names)
		self.assertIn(self.ordinary, names, "nobody owns it yet")
		self.assertNotIn(theirs, names)
		self.assertNotIn(self.other, names)

	def test_the_views_are_shared(self):
		from crm.api.views import get_views

		user(CLERK, "Sales User")
		frappe.set_user(CLERK)
		try:
			views = {(v.dt, v.label): v for v in get_views("CRM Lead") + get_views("CRM Deal")}
		finally:
			frappe.set_user("Administrator")
		for key in (("CRM Lead", mail.OTHER_MAIL), ("CRM Lead", setup.FOLLOW_UPS), ("CRM Deal", setup.FOLLOW_UPS)):
			self.assertTrue(views[key].public, key)

	def test_a_deal_follow_up_has_no_closed_stage(self):
		view = frappe.get_doc("CRM View Settings", {"dt": "CRM Deal", "label": setup.FOLLOW_UPS, "public": 1})
		self.assertEqual(json.loads(view.filters)["status"][0], "not in")
		self.assertEqual({"Won", "Lost"}, set(json.loads(view.filters)["status"][1]))

	def test_a_deleted_view_stays_deleted(self):
		frappe.db.delete("CRM View Settings", {"dt": "CRM Deal", "label": setup.FOLLOW_UPS, "public": 1})
		setup.ensure_crm_views()
		self.assertFalse(frappe.db.exists("CRM View Settings", {"dt": "CRM Deal", "label": setup.FOLLOW_UPS, "public": 1}))


class TestSpamFolder(TeamMailCase):
	def setUp(self):
		super().setUp()
		_settings(spam_folder="[Gmail]/Spam")
		frappe.db.set_global(mail.SPAM_PLACE + INFO, "")
		self.addCleanup(frappe.db.set_global, mail.SPAM_PLACE + INFO, "")

	def test_spam_leaves_no_record_and_the_folder_is_only_read(self):
		_answer("spam")
		raw = _raw("winner@lottery.example.org", SALES)
		imap = FakeIMAP({5: raw})
		_check(imap)
		self.assertTrue(imap.readonly)
		self.assertFalse(_stored(raw))
		self.assertFalse(_lead("winner@lottery.example.org"))
		self.assertFalse(frappe.db.exists("Contact", {"email_id": "winner@lottery.example.org"}))
		self.assertEqual(frappe.db.get_global(mail.SPAM_PLACE + INFO), "7:5")

	def test_a_customer_inquiry_comes_back_as_a_lead(self):
		_answer("customer")
		raw = _raw("buyer@example.org", SALES)
		_check(FakeIMAP({5: raw}))
		stored = _stored(raw)
		self.assertEqual(stored[0].reference_name, _lead("buyer@example.org").name)
		self.assertEqual(_lead("buyer@example.org").status, "New")
		self.assertEqual(frappe.db.get_value("Communication", stored[0].name, "uid"), -1,
			"a spam uid must not move the mailbox's own place")
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": stored[0].name, "content": "Found in the spam folder."}))

	def test_other_mail_keeps_its_category(self):
		_answer("marketing")
		raw = _raw("sale@brand.example.org", SALES)
		_check(FakeIMAP({5: raw}))
		lead = _lead("sale@brand.example.org")
		self.assertEqual((lead.status, lead.get(mail.CATEGORY_FIELD)), (mail.OTHER_MAIL, "Marketing"))

	def test_a_known_customer_needs_no_opinion(self):
		_answer("spam")
		_customer_contact("known@example.org")
		raw = _raw("known@example.org", SALES)
		_check(FakeIMAP({5: raw}))
		self.assertFalse(ASKED)
		self.assertEqual(_stored(raw)[0].reference_doctype, "CRM Deal")

	def test_no_opinion_keeps_the_mail_for_the_next_run(self):
		_answer(None)
		first, second = _raw("a@example.org", SALES), _raw("b@example.org", SALES)
		_check(FakeIMAP({5: first, 6: second}))
		self.assertEqual(len(ASKED), 1, "stops at the first mail with no answer")
		self.assertFalse(_stored(first))
		_answer("customer")
		_check(FakeIMAP({5: first, 6: second}))
		self.assertTrue(_stored(first))
		self.assertTrue(_stored(second))

	def test_a_mail_that_cannot_be_kept_does_not_stop_the_rest(self):
		_answer("customer")
		first, second = _raw("a@example.org", SALES), _raw("b@example.org", SALES)
		real = mail.InboundMail.process

		def fail_first(inbound):
			if inbound.from_email == "a@example.org":
				raise RuntimeError("bad attachment")
			return real(inbound)

		with patch.object(mail.InboundMail, "process", fail_first):
			_check(FakeIMAP({5: first, 6: second}))
		self.assertFalse(_stored(first))
		self.assertTrue(_stored(second))
		self.assertEqual(frappe.db.get_global(mail.SPAM_PLACE + INFO), "7:6")

	def test_a_mail_that_cannot_be_read_does_not_stop_the_rest(self):
		_answer("customer")
		second = _raw("b@example.org", SALES)
		with patch.object(mail, "InboundMail", side_effect=[ValueError("unreadable"), mail.InboundMail(second, frappe.get_doc("Email Account", INFO))]):
			_check(FakeIMAP({5: b"junk", 6: second}))
		self.assertTrue(_stored(second))

	def test_an_empty_fetch_is_tried_again(self):
		_answer("spam")
		imap = FakeIMAP({5: _raw("a@example.org", SALES)})
		with patch.object(imap, "uid", side_effect=[("OK", [b"5"]), ("OK", [None])]):
			_check(imap)
		self.assertFalse(frappe.db.get_global(mail.SPAM_PLACE + INFO))

	def test_each_run_starts_where_the_last_stopped(self):
		_answer("spam")
		imap = FakeIMAP({5: _raw("a@example.org", SALES)})
		_check(imap)
		_check(imap)
		self.assertEqual(imap.fetches, ["(BODY.PEEK[])"], "the newest mail is not read twice")

	def test_a_mail_also_in_the_inbox_is_not_kept_twice(self):
		_answer("customer")
		raw = _raw("buyer@example.org", SALES)
		_pull(INFO, (raw, 900))
		_check(FakeIMAP({5: raw}))
		self.assertEqual(len(_stored(raw)), 1)
		self.assertEqual(frappe.db.get_value("Communication", _stored(raw)[0].name, "uid"), 900)

	def test_nothing_runs_with_no_folder_or_no_provider(self):
		with patch.object(mail, "enqueue") as enqueue, hooks(mail.SCREEN_HOOK, []):
			mail.check_spam_folders()
		enqueue.assert_not_called()
		_settings(spam_folder="")
		with patch.object(mail, "enqueue") as enqueue, hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]):
			mail.check_spam_folders()
		enqueue.assert_not_called()
