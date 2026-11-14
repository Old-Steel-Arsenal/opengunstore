"""CRM sent mail: what staff send from their own mail client, read from each in-scope mailbox's Sent
folder and filed on its lead or deal as sent mail (crm/mail.py, "Sent folder").

The IMAP server is a fake folder object (as for the spam folder); every mail goes through the real
reader, Frappe's own ``InboundMail`` and a real Communication insert on the test site.
"""
from __future__ import annotations

import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_days, get_datetime, get_system_timezone, now_datetime

from ffl_core.crm import mail
from ffl_core.tests.crm_support import user
from ffl_core.tests.test_crm_other_mail import FakeIMAP, _server
from ffl_core.tests.test_crm_team_mail import (ADDRESS, INFO, JOBS, SALES, STAFF_A, STAFF_B, TeamMailCase,
	_customer_contact, _pull, _raw, _settings, _stored)

STAFF_USER = ADDRESS[STAFF_A]   # the staff member whose mailbox STAFF_A is
INBOX = b'(\\HasNoChildren) "/" "INBOX"'
SENT = b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'
_n = [0]


class SentIMAP(FakeIMAP):
	"""A mailbox with a Sent folder: uid → raw mail, each dated (internal date) as *dates* say."""

	def __init__(self, mails, validity="7", dates=None, folders=(INBOX, SENT)):
		super().__init__(mails, validity)
		self.dates, self.folders, self.selected = dates or {}, list(folders), None

	def list(self, *args):
		return "OK", self.folders

	def select(self, folder, readonly=False):
		self.selected = folder
		return super().select(folder, readonly)

	def uid(self, command, *args):
		if command == "search" and args[1].startswith("SINCE"):
			since = datetime.datetime.strptime(args[1].split()[1], "%d-%b-%Y").date()
			today = datetime.date.today()
			return "OK", [" ".join(str(u) for u in sorted(self.mails) if self.dates.get(u, today) >= since).encode()]
		return super().uid(command, *args)


def _sent(to: str, sender: str = STAFF_USER, cc: str = "", in_reply_to: str | None = None, references: str = "",
		when: datetime.datetime | None = None, html: str | None = None, attachment: bytes | None = None,
		headers: dict | None = None) -> bytes:
	_n[0] += 1
	msg = EmailMessage()
	msg["From"] = f"Staff Member <{sender}>"
	msg["To"] = to
	if cc:
		msg["Cc"] = cc
	msg["Subject"] = f"Re: Do you have it in stock? {_n[0]}"
	# A naive datetime here is the site's local time, as Frappe keeps it.
	msg["Date"] = format_datetime((when or now_datetime()).replace(tzinfo=ZoneInfo(get_system_timezone())))
	msg["Message-ID"] = f"<ffl-sent-{frappe.generate_hash(length=12)}@example.com>"
	if in_reply_to:
		msg["In-Reply-To"] = f"<{in_reply_to}>"
	if references:
		msg["References"] = references
	for key, value in (headers or {}).items():
		msg[key] = value
	msg.set_content("Yes, it is.\n\nCome by any time.")
	if html:
		msg.add_alternative(html, subtype="html")
	if attachment:
		msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename="quote.txt")
	return msg.as_bytes()


def _id(raw: bytes) -> str:
	return raw.decode().split("Message-ID: <", 1)[1].split(">", 1)[0]


def _filed(raw: bytes) -> list[dict]:
	return frappe.get_all("Communication", filters={"message_id": _id(raw)}, fields=["name", "sent_or_received",
		"reference_doctype", "reference_name", "sender", "sender_full_name", "recipients", "subject", "content",
		"communication_date", "creation", "owner", "email_account", "uid", "has_attachment", "delivery_status"])


def _check(account: str, imap: SentIMAP) -> None:
	with patch("frappe.email.doctype.email_account.email_account.EmailAccount.get_incoming_server",
			return_value=_server(imap)) as connect, patch.object(mail, "commit"):
		mail.check_sent_folder(account)
	connect.assert_called_once_with(in_receive=True)


def _lead(email: str) -> dict | None:
	return frappe.db.get_value("CRM Lead", {"email": email}, ["name", "status", "lead_owner", mail.CATEGORY_FIELD],
		as_dict=True)


def _shared(doctype: str, name: str) -> bool:
	return bool(frappe.db.exists("ToDo", {"reference_type": doctype, "reference_name": name, "status": "Closed",
		"description": mail.SHARED_NOTE}))


def _sends() -> tuple:
	return (frappe.db.count("Email Queue"), frappe.db.count("Notification Log"), frappe.db.count("CRM Notification"))


class SentMailCase(TeamMailCase):
	def setUp(self):
		super().setUp()
		user(STAFF_USER, "Sales User")
		for account in (STAFF_A, STAFF_B, INFO):
			frappe.db.set_global(mail.SENT_PLACE + account, "")
			self.addCleanup(frappe.db.set_global, mail.SENT_PLACE + account, "")
		# CRM marks a record answered on a sent mail only with this on (its SLA setting).
		frappe.db.set_single_value("FCRM Settings", "auto_mark_replied_on_response", 1)
		frappe.db.set_single_value("FCRM Settings", "auto_reopen_on_new_communication", 1)
		# Outgoing mail on, as on a store's mailbox: a send would queue a row here.
		frappe.db.set_value("Email Account", STAFF_A, {"enable_outgoing": 1, "smtp_server": "smtp.example.com",
			"smtp_port": 587, "use_tls": 1, "login_id": STAFF_USER, "default_outgoing": 0}, update_modified=False)
		self.sends = _sends()
		# Nothing in this module may mail anyone.
		sendmail = patch("frappe.sendmail", side_effect=AssertionError("a sent-folder mail was sent"))
		sendmail.start()
		self.addCleanup(sendmail.stop)

	def assertNothingSent(self):
		self.assertEqual(_sends(), self.sends, "no Email Queue row, notification or CRM notification")


class TestFiling(SentMailCase):
	def _team_lead(self, sender: str = "jane@example.org") -> tuple[str, bytes]:
		raw = _raw(f"Jane Buyer <{sender}>", SALES)
		_pull(INFO, (raw, 70))
		return _stored(raw)[0].reference_name, raw

	def test_a_reply_in_a_crm_thread_goes_on_its_lead_as_sent_and_answers_it(self):
		lead, question = self._team_lead()
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "communication_status"), "Open")
		# The question came in three days ago, the answer went out two days ago.
		frappe.db.set_value("Communication", _stored(question)[0].name, "creation", add_days(now_datetime(), -3),
			update_modified=False)
		when = add_days(now_datetime(), -2).replace(microsecond=0)
		raw = _sent("Jane Buyer <jane@example.org>", in_reply_to=_id(question), when=when,
			html="<p>Yes, it is.</p><p>See <a href='https://example.org'>here</a></p>")
		imap = SentIMAP({11: raw})
		_check(STAFF_A, imap)
		self.assertTrue(imap.readonly)
		self.assertEqual(imap.selected, '"[Gmail]/Sent Mail"')
		self.assertEqual(imap.fetches, ["(BODY.PEEK[])"])
		[comm] = _filed(raw)
		self.assertEqual((comm.sent_or_received, comm.reference_doctype, comm.reference_name), ("Sent", "CRM Lead", lead))
		self.assertEqual((comm.sender, comm.sender_full_name), (STAFF_USER, "Staff Member"))
		self.assertIn("jane@example.org", comm.recipients)
		self.assertTrue(comm.subject.startswith("Re: Do you have it in stock?"))
		self.assertEqual(get_datetime(comm.communication_date), when)
		self.assertEqual(get_datetime(comm.creation), when, "dated as sent, on CRM's timeline too")
		self.assertIn('<a target="_blank"', comm.content, "the body tidied as a received mail's is")
		self.assertEqual((comm.uid, comm.email_account, comm.delivery_status), (-1, STAFF_A, "Sent"))
		self.assertEqual(frappe.db.get_value("Communication", comm.name, "in_reply_to"), _stored(question)[0].name)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "communication_status"), "Replied")
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), STAFF_USER,
			"the first person to answer owns an owner-less lead, as for a reply from CRM")
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "7:11")
		self.assertNothingSent()
		self.assertNoPullErrors()

	def test_an_older_reply_leaves_a_newer_customer_mail_unanswered(self):
		lead, question = self._team_lead()
		raw = _sent("jane@example.org", in_reply_to=_id(question), when=add_days(now_datetime(), -3))
		_check(STAFF_A, SentIMAP({11: raw}))
		self.assertEqual(_filed(raw)[0].reference_name, lead)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "communication_status"), "Open")

	def test_references_find_the_thread_and_a_collector_does_not_take_the_owner(self):
		lead, question = self._team_lead()
		raw = _sent("Jane <jane@example.org>", sender=ADDRESS[INFO],
			references=f"<unknown-{frappe.generate_hash(length=6)}@example.org> <{_id(question)}>")
		_check(INFO, SentIMAP({3: raw}))
		self.assertEqual(_filed(raw)[0].reference_name, lead)
		self.assertFalse(frappe.db.get_value("CRM Lead", lead, "lead_owner"))
		self.assertTrue(_shared("CRM Lead", lead))

	def test_a_customer_recipient_goes_on_their_open_deal(self):
		_customer_contact("known@example.org")
		raw = _sent("colleague@example.com, Known <known@example.org>")
		_check(STAFF_A, SentIMAP({4: raw}))
		[comm] = _filed(raw)
		self.assertEqual(comm.reference_doctype, "CRM Deal")
		self.assertEqual(frappe.db.get_value("CRM Deal", comm.reference_name, "deal_owner"), STAFF_USER)
		self.assertEqual(frappe.db.get_value("CRM Deal", comm.reference_name, "lead_name"), "Known")
		self.assertFalse(_shared("CRM Deal", comm.reference_name), "a staff member's mail is not team mail")
		self.assertNothingSent()

	def test_an_existing_lead_and_an_other_mail_lead_keep_their_place(self):
		lead = mail._new_lead("pat@example.org", "Pat")
		other = mail._new_lead("billing@vendor.example.net", "Billing", "Marketing")
		first, second = _sent("pat@example.org", sender=ADDRESS[INFO]), _sent("billing@vendor.example.net",
			sender=ADDRESS[INFO])
		_check(INFO, SentIMAP({5: first, 6: second}))
		self.assertEqual(_filed(first)[0].reference_name, lead)
		self.assertEqual(_filed(second)[0].reference_name, other)
		self.assertEqual(frappe.db.get_value("CRM Lead", other, "status"), mail.OTHER_MAIL)
		self.assertTrue(_shared("CRM Lead", lead))
		self.assertFalse(_shared("CRM Lead", other), "other mail stays with Sales Managers")

	def test_blocked_automated_and_own_recipients_are_skipped(self):
		mails = {7: _sent("deals@spam.example.net", sender=ADDRESS[INFO]),
			8: _sent("noreply@shop.example.org", sender=ADDRESS[INFO]),
			9: _sent("colleague@example.com", sender=ADDRESS[INFO])}
		_check(INFO, SentIMAP(mails))
		for raw in mails.values():
			self.assertFalse(_filed(raw))
		for email in ("deals@spam.example.net", "noreply@shop.example.org", "colleague@example.com"):
			self.assertFalse(_lead(email), email)
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + INFO), "7:9")
		self.assertNoPullErrors()

	def test_machine_written_mail_is_never_filed(self):
		lead, question = self._team_lead()
		_customer_contact("known@example.org")
		marks = [{"Auto-Submitted": "auto-replied"}, {"Auto-Submitted": "auto-generated; reason=x"},
			{"Precedence": "bulk"}, {"Precedence": "list"}, {"Precedence": "junk"}, {"Precedence": "auto_reply"},
			{"List-Id": "<news.example.com>"}, {"List-Unsubscribe": "<mailto:leave@example.com>"}]
		counts = {d: frappe.db.count(d) for d in ("Communication", "Contact", "CRM Lead", "CRM Deal")}
		for account, sender in ((STAFF_A, STAFF_USER), (INFO, ADDRESS[INFO])):
			mails = {}
			for i, headers in enumerate(marks):
				mails[100 + 3 * i] = _sent("jane@example.org", sender=sender, in_reply_to=_id(question), headers=headers)
				mails[101 + 3 * i] = _sent("known@example.org", sender=sender, headers=headers)
				mails[102 + 3 * i] = _sent(f"new{i}@example.org", sender=sender, headers=headers)
			_check(account, SentIMAP(mails))
			self.assertEqual({d: frappe.db.count(d) for d in counts}, counts, account)
		normal = _sent("jane@example.org", in_reply_to=_id(question), headers={"Auto-Submitted": "no"})
		_check(STAFF_A, SentIMAP({200: normal}))
		self.assertEqual(_filed(normal)[0].reference_name, lead)

	def test_a_collectors_mail_to_someone_new_is_a_shared_lead_without_owner(self):
		_settings(category_rules=[{"domain": "brand.example.net", "category": mail._ensure_category("Vendor")}])
		new, vendor = _sent("Sam Smith <sam@example.org>", sender=ADDRESS[INFO]), _sent("rep@brand.example.net",
			sender=ADDRESS[INFO])
		_check(INFO, SentIMAP({12: new, 13: vendor}))
		lead = _lead("sam@example.org")
		self.assertEqual((lead.status, lead.lead_owner), ("New", None))
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, "first_name"), "Sam")
		self.assertEqual(_filed(new)[0].reference_name, lead.name)
		self.assertTrue(_shared("CRM Lead", lead.name))
		vendor_lead = _lead("rep@brand.example.net")
		self.assertEqual((vendor_lead.status, vendor_lead.get(mail.CATEGORY_FIELD)), (mail.OTHER_MAIL, "Vendor"))
		self.assertNothingSent()

	def test_a_staff_members_mail_to_someone_new_leaves_no_trace(self):
		counts = {d: frappe.db.count(d) for d in ("Communication", "Contact", "CRM Lead", "CRM Deal", "File", "Error Log")}
		raw = _sent("Friend <friend@example.org>", attachment=b"private notes")
		_check(STAFF_A, SentIMAP({20: raw}))
		self.assertEqual({d: frappe.db.count(d) for d in counts}, counts)
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "7:20", "passed over, not read again")

	def test_a_mail_sent_from_crm_is_not_filed_twice(self):
		lead = mail._new_lead("jane@example.org", "Jane")
		from frappe.core.doctype.communication.email import make

		comm = make(doctype="CRM Lead", name=lead, content="Yes.", subject="Re: stock", sender=STAFF_USER,
			recipients="jane@example.org", send_email=False)["name"]
		message_id = frappe.db.get_value("Communication", comm, "message_id")
		raw = _sent("jane@example.org")
		raw = raw.replace(_id(raw).encode(), message_id.encode())
		_check(STAFF_A, SentIMAP({30: raw}))
		self.assertEqual(frappe.db.count("Communication", {"message_id": message_id}), 1)

	def test_the_same_mail_reaching_a_team_inbox_later_is_not_filed_again(self):
		_customer_contact("known@example.org")
		raw = _sent("known@example.org", cc=SALES)
		_check(STAFF_A, SentIMAP({31: raw}))
		self.assertEqual(len(_filed(raw)), 1)
		_pull(INFO, (raw, 91))
		self.assertEqual([c.sent_or_received for c in _filed(raw)], ["Sent"], "the team inbox's copy is one we sent")
		self.assertNoPullErrors()

	def test_attachments_are_kept_as_a_received_mails_are(self):
		_customer_contact("known@example.org")
		raw = _sent("known@example.org", attachment=b"quote: 1 rifle")
		_check(STAFF_A, SentIMAP({32: raw}))
		[comm] = _filed(raw)
		self.assertTrue(comm.has_attachment)
		self.assertEqual(frappe.get_all("File", filters={"attached_to_doctype": "Communication",
			"attached_to_name": comm.name}, pluck="file_name"), ["quote.txt"])


class TestReading(SentMailCase):
	def test_the_first_run_reads_only_the_last_30_days(self):
		_customer_contact("known@example.org")
		old, recent = _sent("known@example.org"), _sent("known@example.org")
		today = datetime.date.today()
		imap = SentIMAP({40: old, 41: recent}, dates={40: today - datetime.timedelta(days=45),
			41: today - datetime.timedelta(days=5)})
		_check(STAFF_A, imap)
		self.assertFalse(_filed(old))
		self.assertTrue(_filed(recent))
		self.assertEqual(len(imap.fetches), 1)

	def test_an_empty_first_window_starts_at_the_next_mail(self):
		_customer_contact("known@example.org")
		old = _sent("known@example.org")
		_check(STAFF_A, SentIMAP({40: old}, dates={40: datetime.date.today() - datetime.timedelta(days=60)}))
		self.assertFalse(_filed(old))
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "")
		new = _sent("known@example.org")
		_check(STAFF_A, SentIMAP({40: old, 41: new}, dates={40: datetime.date.today() - datetime.timedelta(days=60)}))
		self.assertFalse(_filed(old))
		self.assertTrue(_filed(new))

	def test_each_run_starts_where_the_last_stopped_and_a_rerun_files_nothing_twice(self):
		_customer_contact("known@example.org")
		raw = _sent("known@example.org")
		imap = SentIMAP({50: raw})
		_check(STAFF_A, imap)
		_check(STAFF_A, imap)
		self.assertEqual(imap.fetches, ["(BODY.PEEK[])"])
		frappe.db.set_global(mail.SENT_PLACE + STAFF_A, "")   # read again from the start
		_check(STAFF_A, SentIMAP({50: raw}))
		self.assertEqual(len(_filed(raw)), 1)

	def test_renumbered_uids_start_the_folder_again(self):
		_customer_contact("known@example.org")
		frappe.db.set_global(mail.SENT_PLACE + STAFF_A, "7:500")
		raw = _sent("known@example.org")
		_check(STAFF_A, SentIMAP({3: raw}, validity="8"))
		self.assertTrue(_filed(raw), "a lower uid in a new UIDVALIDITY is read")
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "8:3")

	def test_a_failing_mail_is_passed_over(self):
		_customer_contact("known@example.org")
		bad, good = _sent("known@example.org"), _sent("known@example.org")
		real = mail._insert_sent

		def fail_first(message, email_account, place):
			if message.message_id == _id(bad):
				raise RuntimeError("bad attachment")
			return real(message, email_account, place)

		errors = frappe.db.count("Error Log")
		with patch.object(mail, "_insert_sent", fail_first):
			_check(STAFF_A, SentIMAP({60: bad, 61: good}))
		self.assertFalse(_filed(bad))
		self.assertTrue(_filed(good))
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "7:61")

	def test_a_lost_race_keeps_the_mail_for_the_next_run(self):
		_customer_contact("known@example.org")
		raw = _sent("known@example.org")
		real_rollback = frappe.db.rollback

		def rollback(*, save_point=None, chain=False):
			return real_rollback(save_point=save_point or "ffl_crm_sent")

		with patch.object(mail, "_insert_sent", side_effect=frappe.QueryDeadlockError("tabSeries")), \
				patch.object(frappe.db, "rollback", side_effect=rollback):
			_check(STAFF_A, SentIMAP({70: raw}))
		self.assertFalse(_filed(raw))
		self.assertNotEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), "7:70")
		_check(STAFF_A, SentIMAP({70: raw}))
		self.assertTrue(_filed(raw))

	def test_no_sent_folder_is_logged_once(self):
		errors = frappe.db.count("Error Log")
		_check(STAFF_A, SentIMAP({1: _sent("known@example.org")}, folders=[INBOX]))
		_check(STAFF_A, SentIMAP({1: _sent("known@example.org")}, folders=[INBOX]))
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		self.assertEqual(frappe.db.get_global(mail.SENT_PLACE + STAFF_A), mail.NO_SENT_FOLDER)
		_customer_contact("known@example.org")
		raw = _sent("known@example.org")
		_check(STAFF_A, SentIMAP({2: raw}))
		self.assertTrue(_filed(raw), "a folder found later is read")

	def test_the_folder_is_found_by_its_special_use_flag(self):
		cases = [([b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'], "[Gmail]/Sent Mail"),
			([b'(\\HasNoChildren) "." INBOX', b'(\\Sent \\HasNoChildren) "." Sent'], "Sent"),
			([b'(\\HasNoChildren \\Sent) NIL "Sent \\"Items\\""'], 'Sent "Items"'),
			([b'(\\HasNoChildren) "/" "Sent"', (b"literal", b"x")], None)]
		for folders, expected in cases:
			self.assertEqual(mail._sent_folder(_server(SentIMAP({}, folders=folders))), expected, folders)

	def test_the_cron_queues_each_in_scope_imap_mailbox(self):
		for account in (STAFF_A, INFO, JOBS):
			frappe.db.set_value("Email Account", account, "awaiting_password", 0, update_modified=False)
		frappe.db.set_value("Email Account", STAFF_B, "awaiting_password", 1, update_modified=False)
		with patch.object(mail, "enqueue") as enqueue:
			mail.check_sent_folders()
		self.assertEqual(sorted(c.kwargs["account"] for c in enqueue.call_args_list), sorted([STAFF_A, INFO]))
		self.assertNotIn(JOBS, [c.kwargs["account"] for c in enqueue.call_args_list])
		self.assertEqual(enqueue.call_args_list[0].args, ("ffl_core.crm.mail.check_sent_folder", "long"))
		with patch.object(mail, "enqueue") as enqueue, patch.object(mail, "installed", return_value=False):
			mail.check_sent_folders()
		enqueue.assert_not_called()

	def test_it_is_scheduled_every_10_minutes(self):
		from ffl_core.tests.test_crm_setup import _hooks_py

		self.assertIn("ffl_core.crm.mail.check_sent_folders", _hooks_py()["scheduler_events"]["cron"]["*/10 * * * *"])
