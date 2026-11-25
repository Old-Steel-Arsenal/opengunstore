"""The daily "who to answer" email (ffl_core.crm.digest): what is in the queue, its order, who gets which
rows, the switch and the recipients, the mail itself, and why the mail never comes back into CRM as
customer mail. Real leads, deals, mail, tasks and Email Queue rows; nothing is stubbed."""
from __future__ import annotations

from datetime import timedelta
from email import message_from_string

import frappe
from frappe.utils import add_days, now_datetime, parse_addr

from ffl_core.api import crm as api
from ffl_core.crm import digest, mail, memory
from ffl_core.tests.crm_support import CRMTestCase, user
from ffl_core.tests.test_crm_deal_pos import _contact, _deal
from ffl_core.tests.test_crm_team_mail import STAFF_A, _account, _settings

OWNER_A = "digest-owner-a@example.com"
OWNER_B = "digest-owner-b@example.com"
EVERYONE = "digest-everyone@example.com"
OUTGOING = "_T Digest Outgoing"
OUTGOING_ADDRESS = "digest-store-out@example.com"
_n = [0]


def _email() -> str:
	_n[0] += 1
	return f"digest-{_n[0]}-{frappe.generate_hash(length=6)}@example.org"


def _lead(owner: str | None = None, status: str = "New", email: str | None = None) -> str:
	name = mail._new_lead(email or _email(), "Pat Buyer")
	frappe.db.set_value("CRM Lead", name, {"lead_owner": owner, "status": status}, update_modified=False)
	return name


def _mail(doctype: str, name: str, received: bool = True, ago: int = 0, sender: str | None = None,
		subject: str = "Is it in stock?") -> str:
	"""An email on the record, *ago* days old (and an hour apart from another made the same day)."""
	_n[0] += 1
	address = sender or (_email() if received else "sales@example.com")
	return frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
		"communication_medium": "Email", "sent_or_received": "Received" if received else "Sent", "sender": address,
		"recipients": "sales@example.com" if received else _email(), "subject": subject, "content": "<p>Hello</p>",
		"communication_date": now_datetime() - timedelta(days=ago, minutes=-_n[0]), "reference_doctype": doctype,
		"reference_name": name}).insert(ignore_permissions=True).name


def _task(doctype: str, name: str, title: str = "Call back", status: str = "Todo") -> str:
	return frappe.get_doc({"doctype": "CRM Task", "title": title, "status": status, "reference_doctype": doctype,
		"reference_docname": name}).insert(ignore_permissions=True).name


def _names(rows: list[dict]) -> list[str]:
	return [r["name"] for r in rows]


def _outgoing() -> None:
	"""A default outgoing Email Account, so frappe.sendmail has somewhere to send from."""
	if not frappe.db.exists("Email Account", OUTGOING):
		frappe.get_doc({"doctype": "Email Account", "email_account_name": OUTGOING, "email_id": OUTGOING_ADDRESS,
			"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(ignore_permissions=True)
	frappe.db.set_value("Email Account", {"default_outgoing": 1}, "default_outgoing", 0, update_modified=False)
	frappe.db.set_value("Email Account", OUTGOING, {"enable_outgoing": 1, "default_outgoing": 1,
		"smtp_server": "smtp.invalid", "smtp_port": 587}, update_modified=False)
	frappe.local.outgoing_email_account = {}


def _queued(address: str) -> list:
	"""The Email Queue rows addressed to *address*."""
	parents = frappe.get_all("Email Queue Recipient", filters={"recipient": address}, pluck="parent")
	return frappe.get_all("Email Queue", filters={"name": ["in", parents or [""]]},
		fields=["name", "sender", "message"]) if parents else []


class DigestCase(CRMTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for u in (OWNER_A, OWNER_B):
			user(u, "Sales User")

	def setUp(self):
		super().setUp()
		_outgoing()
		_settings(crm_daily_digest=1, crm_digest_recipients="")

	def tearDown(self):
		frappe.local.outgoing_email_account = {}
		super().tearDown()


class TestQueue(DigestCase):
	def test_a_lead_waiting_for_a_reply_is_in_with_what_the_email_shows(self):
		lead = _lead(owner=OWNER_A)
		_mail("CRM Lead", lead, received=True, ago=3, subject="Is it in stock? ID 12345678")
		_mail("CRM Lead", lead, received=True, ago=1)
		frappe.db.set_value("CRM Lead", lead, memory.SUMMARY, "Wants a 9mm carry pistol", update_modified=False)
		_task("CRM Lead", lead, "Check the safe")
		(row,) = [r for r in digest.build() if r["name"] == lead]
		self.assertEqual((row["doctype"], row["link"], row["owner"]), ("CRM Lead", f"/crm/leads/{lead}", OWNER_A))
		self.assertEqual((row["customer"], row["days"], row["messages"]), ("Pat Buyer", 1, 2))
		self.assertEqual((row["tasks"], row["summary"]), (["Check the safe"], "Wants a 9mm carry pistol"))
		self.assertEqual(row["subject"], "Is it in stock?")
		self.assertTrue(row["received"] > now_datetime() - timedelta(days=2))

	def test_what_crm_sensitive_blanks_is_blanked_in_the_queue(self):
		lead = _lead()
		_mail("CRM Lead", lead, subject="My driver's license no. A1234567890 please")
		_task("CRM Lead", lead, "Card 4111 1111 1111 1111 on file")
		(row,) = [r for r in digest.build() if r["name"] == lead]
		self.assertNotIn("A1234567890", row["subject"])
		self.assertIn("[removed]", row["subject"])
		self.assertNotIn("4111", row["tasks"][0])

	def test_a_deal_waiting_for_a_reply_is_in_with_its_customer_name(self):
		contact = _contact(_email(), first_name="Dana")
		deal = _deal(contact, owner=OWNER_B, status="Talking")
		_mail("CRM Deal", deal.name, ago=2)
		(row,) = [r for r in digest.build() if r["name"] == deal.name]
		self.assertEqual((row["doctype"], row["link"], row["customer"], row["owner"], row["days"]),
			("CRM Deal", f"/crm/deals/{deal.name}", "Dana", OWNER_B, 2))

	def test_answered_other_mail_converted_closed_and_automated_are_left_out(self):
		answered = _lead()
		_mail("CRM Lead", answered, received=True, ago=2)
		_mail("CRM Lead", answered, received=False, ago=1)
		other = _lead(status=mail.OTHER_MAIL)
		_mail("CRM Lead", other)
		converted = _lead()
		_mail("CRM Lead", converted)
		frappe.db.set_value("CRM Lead", converted, "converted", 1)
		lost_lead = _lead(status="Unqualified")
		_mail("CRM Lead", lost_lead)
		won_deal = _deal(owner=None)
		frappe.db.set_value("CRM Deal", won_deal.name, "status", "Won")
		_mail("CRM Deal", won_deal.name)
		lost_deal = _deal(owner=None)
		frappe.db.set_value("CRM Deal", lost_deal.name, "status", "Lost")
		_mail("CRM Deal", lost_deal.name)
		robot = _lead()
		_mail("CRM Lead", robot, sender="noreply@example.org")
		quiet = _lead()   # no mail at all
		inside = _lead()
		_mail("CRM Lead", inside)
		skipped = {answered, other, converted, lost_lead, won_deal.name, lost_deal.name, robot, quiet}
		names = set(_names(digest.build()))
		self.assertIn(inside, names)
		self.assertEqual(names & skipped, set())

	def test_an_open_task_keeps_a_record_in_even_when_it_is_answered_and_a_done_one_does_not(self):
		answered = _lead()
		_mail("CRM Lead", answered, received=True, ago=2)
		_mail("CRM Lead", answered, received=False, ago=1)
		_task("CRM Lead", answered, "Order the part")
		_task("CRM Lead", answered, "Old call", status="Done")
		done_only = _lead()
		_task("CRM Lead", done_only, status="Canceled")
		closed = _lead(status="Unqualified")
		_task("CRM Lead", closed)
		rows = {r["name"]: r for r in digest.build()}
		self.assertEqual(rows[answered]["tasks"], ["Order the part"])
		self.assertEqual((rows[answered]["subject"], rows[answered]["received"], rows[answered]["days"],
			rows[answered]["messages"]), ("", None, None, 0))
		self.assertNotIn(done_only, rows)
		self.assertNotIn(closed, rows)

	def test_messages_counts_the_customers_mail_since_the_stores_last(self):
		lead = _lead()
		_mail("CRM Lead", lead, received=True, ago=6)
		_mail("CRM Lead", lead, received=False, ago=5)
		_mail("CRM Lead", lead, received=True, ago=4)
		_mail("CRM Lead", lead, received=True, ago=3, sender="noreply@example.org")
		_mail("CRM Lead", lead, received=True, ago=2)
		(row,) = [r for r in digest.build() if r["name"] == lead]
		self.assertEqual(row["messages"], 2)

	def _converted(self) -> tuple[str, str]:
		"""A lead that became a deal, as CRM's conversion leaves them: the mail stays on the lead."""
		lead = _lead()
		deal = _deal(owner=None)
		frappe.db.set_value("CRM Lead", lead, "converted", 1)
		frappe.db.set_value("CRM Deal", deal.name, "lead", lead)
		return lead, deal.name

	def test_a_deal_counts_the_mail_its_lead_got_before_it_was_converted(self):
		lead, deal = self._converted()
		_mail("CRM Lead", lead, ago=4, subject="Still available?")
		_task("CRM Lead", lead, "Check the safe")
		rows = {r["name"]: r for r in digest.build()}
		self.assertNotIn(lead, rows)
		self.assertEqual((rows[deal]["subject"], rows[deal]["messages"], rows[deal]["days"], rows[deal]["tasks"]),
			("Still available?", 1, 4, ["Check the safe"]))

	def test_a_reply_sent_on_the_lead_before_conversion_answers_the_deal(self):
		lead, deal = self._converted()
		_mail("CRM Lead", lead, received=True, ago=4)
		_mail("CRM Lead", lead, received=False, ago=3)
		self.assertNotIn(deal, _names(digest.build()))

	def test_a_new_customer_mail_on_the_deal_after_a_reply_on_the_lead_waits_alone(self):
		lead, deal = self._converted()
		_mail("CRM Lead", lead, received=True, ago=4)
		_mail("CRM Lead", lead, received=False, ago=3)
		_mail("CRM Deal", deal, received=True, ago=1)
		(row,) = [r for r in digest.build() if r["name"] == deal]
		self.assertEqual((row["messages"], row["days"]), (1, 1))

	def test_a_website_inquiry_counts_as_a_message_waiting(self):
		from ffl_core.crm import inquiries

		out = inquiries.file_inquiry(kind="ask", event_id=f"evt-{frappe.generate_hash(length=8)}", email=_email(),
			full_name="Pat Buyer", message="Is it still for sale?")
		self.assertIn(out["name"], _names(digest.build()))

	def test_longest_waiting_first_and_task_only_records_last(self):
		newer, older, oldest = _lead(), _lead(), _lead()
		_mail("CRM Lead", newer, ago=1)
		_mail("CRM Lead", older, ago=5)
		_mail("CRM Lead", oldest, ago=9)
		task_late, task_early = _lead(), _lead()
		for name in (task_late, task_early):
			_mail("CRM Lead", name, received=False, ago=20)
		_task("CRM Lead", task_early)
		_task("CRM Lead", task_late)
		mine = [n for n in _names(digest.build()) if n in {newer, older, oldest, task_late, task_early}]
		self.assertEqual(mine, [oldest, older, newer, task_early, task_late])

	def test_build_for_a_user_is_that_users_records_only(self):
		a, b, nobody = _lead(owner=OWNER_A), _lead(owner=OWNER_B), _lead()
		for name in (a, b, nobody):
			_mail("CRM Lead", name)
		self.assertEqual(_names(digest.build(user=OWNER_A)), [a])
		self.assertTrue({a, b, nobody} <= set(_names(digest.build())))


class TestRecipientsAndSwitch(DigestCase):
	def _sent_to(self, *addresses: str) -> dict[str, list]:
		return {a: _queued(a) for a in addresses}

	def test_each_owner_gets_their_records_and_the_recipients_get_every_one(self):
		_settings(crm_digest_recipients=f"{EVERYONE}")
		a, b, nobody = _lead(owner=OWNER_A), _lead(owner=OWNER_B), _lead()
		for name in (a, b, nobody):
			_mail("CRM Lead", name)
		plan = digest._plan(digest.build())
		self.assertEqual(set(_names(plan[OWNER_A])) & {a, b, nobody}, {a})
		self.assertEqual(set(_names(plan[OWNER_B])) & {a, b, nobody}, {b})
		self.assertTrue({a, b, nobody} <= set(_names(plan[EVERYONE])), "ownerless records go to the recipients")
		digest.send()
		for address in (OWNER_A, OWNER_B, EVERYONE):
			self.assertEqual(len(_queued(address)), 1, address)
		for lead, address, absent in ((a, OWNER_A, b), (b, OWNER_B, a)):
			text = _queued(address)[0].message
			self.assertIn(lead, text)
			self.assertNotIn(absent, text)
		self.assertTrue(all(n in _queued(EVERYONE)[0].message for n in (a, b, nobody)))

	def test_a_recipient_who_owns_records_gets_one_mail_with_everything(self):
		_settings(crm_digest_recipients=OWNER_A)
		a, b = _lead(owner=OWNER_A), _lead(owner=OWNER_B)
		for name in (a, b):
			_mail("CRM Lead", name)
		digest.send()
		(queued,) = _queued(OWNER_A)
		self.assertTrue(a in queued.message and b in queued.message)

	def test_a_disabled_owner_is_not_mailed_but_the_recipients_still_get_the_record(self):
		_settings(crm_digest_recipients=EVERYONE)
		lead = _lead(owner=OWNER_A)
		_mail("CRM Lead", lead)
		frappe.db.set_value("User", OWNER_A, "enabled", 0)
		digest.send()
		self.assertEqual(_queued(OWNER_A), [])
		self.assertEqual(len(_queued(EVERYONE)), 1)

	def test_the_recipients_setting_takes_commas_and_lines_and_drops_junk_and_team_addresses(self):
		_settings(team_addresses=[{"address": "digest-team@example.com", "address_type": "Group"}],
			primary_team_address="digest-team@example.com")
		frappe.db.set_single_value(mail.SETTINGS, digest.RECIPIENTS,
			f"{EVERYONE}, Other@Example.com;\nnot-an-address\ndigest-team@example.com\n{EVERYONE}")
		self.assertEqual(digest.recipients(), [EVERYONE, "other@example.com"])

	def test_lead_mail_settings_refuses_a_bad_or_team_address_and_tidies_the_list(self):
		_settings(team_addresses=[{"address": "digest-team@example.com", "address_type": "Group"}],
			primary_team_address="digest-team@example.com")
		with self.assertRaises(frappe.ValidationError):
			_settings(crm_digest_recipients=f"{EVERYONE}\nnot-an-address")
		with self.assertRaises(frappe.ValidationError):
			_settings(crm_digest_recipients="Digest-Team@example.com")
		_settings(crm_digest_recipients=f"  {EVERYONE.upper()} , second@example.com ")
		self.assertEqual(frappe.db.get_single_value(mail.SETTINGS, digest.RECIPIENTS), f"{EVERYONE}\nsecond@example.com")

	def test_switch_off_sends_nothing(self):
		_settings(crm_daily_digest=0, crm_digest_recipients=EVERYONE)
		_mail("CRM Lead", _lead(owner=OWNER_A))
		self.assertEqual(digest.send(), 0)
		self.assertEqual((_queued(EVERYONE), _queued(OWNER_A)), ([], []))

	def test_an_empty_list_sends_nothing(self):
		_settings(crm_digest_recipients=EVERYONE)
		frappe.db.delete("CRM Lead")
		frappe.db.delete("CRM Deal")
		self.assertEqual(digest.build(), [])
		self.assertEqual(digest.send(), 0)
		self.assertEqual(_queued(EVERYONE), [])

	def test_an_owner_with_nothing_waiting_is_not_mailed(self):
		_settings(crm_digest_recipients="")
		_mail("CRM Lead", _lead(owner=OWNER_A))
		answered = _lead(owner=OWNER_B)
		_mail("CRM Lead", answered, received=True, ago=2)
		_mail("CRM Lead", answered, received=False, ago=1)
		digest.send()
		self.assertEqual(len(_queued(OWNER_A)), 1)
		self.assertEqual(_queued(OWNER_B), [])

	def test_it_is_scheduled_at_eight_site_time(self):
		self.assertIn("ffl_core.crm.digest.send", frappe.get_hooks("scheduler_events")["cron"]["0 8 * * *"])


class TestTheMail(DigestCase):
	def _one(self, *leads) -> tuple:
		_settings(crm_digest_recipients=EVERYONE)
		digest.send()
		(queued,) = _queued(EVERYONE)
		return queued, message_from_string(queued.message)

	def test_one_table_with_the_link_and_every_value_escaped(self):
		lead = _lead(owner=OWNER_A)
		_mail("CRM Lead", lead)
		row = {**[r for r in digest.build() if r["name"] == lead][0], "customer": "<i>Pat</i>",
			"subject": "<script>x</script> & more", "tasks": ["<b>call</b>"], "summary": "\"quoted\" <u>"}
		html = digest.table([row])
		self.assertEqual(html.count("<table"), 1)
		self.assertIn(f'href="{frappe.utils.get_url(f"/crm/leads/{lead}")}"', html)
		for raw in ("<script>", "<i>", "<b>call", "<u>"):
			self.assertNotIn(raw, html)
		self.assertIn("&lt;script&gt;x&lt;/script&gt; &amp; more", html)
		self.assertEqual(digest._subject(1), "1 customer waiting for a reply")
		self.assertEqual(digest._subject(7), "7 customers waiting for a reply")

	def test_it_is_queued_from_the_default_outgoing_account_with_the_count_in_the_subject(self):
		leads = [_lead() for _i in range(3)]
		for lead in leads:
			_mail("CRM Lead", lead)
		queued, parsed = self._one()
		count = len(digest.build())
		self.assertEqual(parsed["Subject"], f"{count} customers waiting for a reply")
		self.assertEqual(parse_addr(queued.sender)[1], OUTGOING_ADDRESS)
		self.assertTrue(all(lead in queued.message for lead in leads))

	def test_the_mail_is_marked_machine_written_for_the_sent_folder_rule(self):
		_mail("CRM Lead", _lead())
		_queued_row, parsed = self._one()
		self.assertTrue(mail._machine_written(parsed), "check_sent_folders skips it whoever it was sent to")

	def test_a_copy_pulled_by_a_staff_mailbox_is_never_filed_as_customer_mail(self):
		"""The mail the POS sends, as a staff mailbox's pull would see it: no lead, no deal."""
		_account(STAFF_A, auto=1, imap_all=1)
		staff = frappe.db.get_value("Email Account", STAFF_A, "email_id")
		_mail("CRM Lead", _lead())
		queued, _parsed = self._one()
		sender = parse_addr(queued.sender)[1]
		copy = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Email", "sent_or_received": "Received", "sender": sender,
			"sender_full_name": parse_addr(queued.sender)[0], "recipients": staff, "subject": "1 customer waiting",
			"email_account": STAFF_A, "content": "<table></table>"})
		self.assertEqual(mail.decide(copy), (mail.SKIP, "direct mail from someone new"))
		_settings(own_domains=sender.rsplit("@", 1)[-1])
		self.assertEqual(mail.decide(copy), (mail.SKIP, "blocked sender"))
		self.assertFalse(frappe.db.exists("CRM Lead", {"email": sender}))


class TestReplyQueueEndpoint(DigestCase):
	def test_it_returns_the_callers_own_rows_the_same_as_the_email_has(self):
		mine, theirs, nobody = _lead(owner=OWNER_A), _lead(owner=OWNER_B), _lead()
		for name in (mine, theirs, nobody):
			_mail("CRM Lead", name)
		expected = digest.build(user=OWNER_A)
		frappe.set_user(OWNER_A)
		try:
			rows = api.reply_queue()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(_names(rows), [mine])
		self.assertEqual(rows, expected)

	def test_someone_without_a_crm_role_is_refused(self):
		user("digest-nobody@example.com", "Website Manager")
		frappe.set_user("digest-nobody@example.com")
		try:
			with self.assertRaises(frappe.PermissionError):
				api.reply_queue()
		finally:
			frappe.set_user("Administrator")
