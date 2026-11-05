"""CRM team mail: the copy of a pulled mail CRM keeps, private mail left out, team mail from someone new
as an owner-less lead, the first reply taking it, staff Reply-To and team mailboxes set up, and the
self-serve mailbox connection.

Mail is pulled through Frappe's own ``EmailAccount.receive`` and ``InboundMail`` from raw messages;
only the IMAP fetch is replaced (``get_inbound_mails``), and the pull's per-mail commit / rollback
are mapped onto a savepoint inside the test transaction (docs/testing.md: no real commit).
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import PropertyMock, patch

import frappe
from frappe.email.doctype.email_account.email_account import EmailAccount, get_max_email_uid
from frappe.email.receive import InboundMail
from frappe.utils import now_datetime

from ffl_core.crm import mail, team
from ffl_core.tests.crm_support import CLERK, MANAGER, CRMTestCase, user

STAFF_A = "_T Team Staff A"
STAFF_B = "_T Team Staff B"
INFO = "_T Team Info"
JOBS = "_T Team Jobs"
ADDRESS = {STAFF_A: "ffltm-staff-a@example.com", STAFF_B: "ffltm-staff-b@example.com", INFO: "ffltm-info@example.com",
	JOBS: "ffltm-jobs@example.com"}
SALES = "ffltm-sales@example.com"
PULL = "ffl_test_pull"
_counter = [0]


def _account(name: str, auto: int = 0, imap_all: int = 0) -> str:
	if not frappe.db.exists("Email Account", name):
		frappe.get_doc({"doctype": "Email Account", "email_account_name": name, "email_id": ADDRESS[name],
			"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(ignore_permissions=True)
	# Set past the account's own save, which would connect to a mail server.
	frappe.db.set_value("Email Account", name, {mail.AUTO_LEAD: auto, "use_imap": imap_all,
		"email_sync_option": "ALL" if imap_all else "UNSEEN", "no_failed": 0, "enable_incoming": 1}, update_modified=False)
	return name


def _settings(**values) -> None:
	doc = frappe.get_doc(mail.SETTINGS)
	doc.update(values)
	doc.save(ignore_permissions=True)


def _raw(sender: str, to: str, cc: str = "", message_id: str | None = None, in_reply_to: str | None = None) -> bytes:
	_counter[0] += 1
	msg = EmailMessage()
	msg["From"] = sender
	msg["To"] = to
	if cc:
		msg["Cc"] = cc
	msg["Subject"] = f"Do you have it in stock? {_counter[0]}"
	msg["Date"] = format_datetime(now_datetime().astimezone())
	msg["Message-ID"] = f"<{message_id or f'ffl-test-{frappe.generate_hash(length=12)}'}@example.org>"
	if in_reply_to:
		msg["In-Reply-To"] = f"<{in_reply_to}>"
	msg.set_content("Hello, is it still for sale?")
	return msg.as_bytes()


def _message_id(raw: bytes) -> str:
	return raw.decode().split("Message-ID: <", 1)[1].split(">", 1)[0]


def _pull(account: str, *mails: tuple[bytes, int]) -> None:
	"""One run of ``EmailAccount.receive`` over these (raw, uid) mails."""
	doc = frappe.get_doc("Email Account", account)
	inbound = [InboundMail(raw, doc, str(uid)) for raw, uid in mails]
	real_rollback = frappe.db.rollback

	def commit(*args, **kwargs):
		frappe.db.savepoint(PULL)

	def rollback(*, save_point=None, chain=False):
		if save_point:
			return real_rollback(save_point=save_point)
		real_rollback(save_point=PULL)
		frappe.db.after_rollback.run()   # what Database.rollback runs after a real ROLLBACK

	frappe.db.savepoint(PULL)
	with patch.object(EmailAccount, "get_inbound_mails", return_value=inbound), \
			patch.object(frappe.db, "commit", side_effect=commit), \
			patch.object(frappe.db, "rollback", side_effect=rollback):
		doc.receive()


def _stored(raw: bytes) -> list[dict]:
	return frappe.get_all("Communication", filters={"message_id": _message_id(raw), "sent_or_received": "Received"},
		fields=["name", "email_account", "reference_doctype", "reference_name"])


def _assignees(doctype: str, name: str) -> list[str]:
	"""Who is assigned the record (one row each; a repeat would show twice)."""
	return sorted(frappe.get_all("ToDo", filters={"reference_type": doctype, "reference_name": name,
		"status": ["!=", "Cancelled"]}, pluck="allocated_to"))


def _can_read(user: str, doctype: str, name: str) -> bool:
	"""CRM's own permission rule, both ways it applies: the record itself and the list."""
	doc = frappe.has_permission(doctype, "read", doc=name, user=user)
	frappe.set_user(user)
	try:
		listed = bool(frappe.get_list(doctype, filters={"name": name}, pluck="name"))
	finally:
		frappe.set_user("Administrator")
	return doc and listed


def _notices() -> int:
	"""Assignment notices of both kinds: Frappe's (bell and email) and CRM's own."""
	return frappe.db.count("Notification Log", {"type": "Assignment"}) + frappe.db.count("CRM Notification")


def _customer_contact(email: str) -> str:
	customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"_T Team Buyer {email}",
		"customer_type": "Individual"}).insert(ignore_permissions=True)
	return frappe.get_doc({"doctype": "Contact", "first_name": "Known", "email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert(ignore_permissions=True).name


class TeamMailCase(CRMTestCase):
	def setUp(self):
		super().setUp()
		_account(STAFF_A, auto=1, imap_all=1)
		_account(STAFF_B, auto=1, imap_all=1)
		_account(INFO, imap_all=1)
		_account(JOBS)
		_settings(blocked_sender_domains="spam.example.net", own_domains="example.com",
			team_addresses=[{"address": "ffltm-info@example.com", "address_type": team.MAILBOX, "collector": INFO},
				{"address": SALES, "address_type": team.GROUP, "collector": INFO}],
			primary_team_address=SALES)
		self.errors = frappe.db.count("Error Log")
		self.notices = _notices()
		self.queued = frappe.db.count("Email Queue")
		self.unhandled = frappe.db.count("Unhandled Email")

	def tearDown(self):
		super().tearDown()
		frappe.clear_document_cache(mail.SETTINGS, mail.SETTINGS)

	def assertNoPullErrors(self):
		self.assertEqual(frappe.db.count("Error Log"), self.errors)
		self.assertEqual(frappe.db.count("Unhandled Email"), self.unhandled)
		for account in (STAFF_A, STAFF_B, INFO):
			self.assertFalse(frappe.db.get_value("Email Account", account, "no_failed"), account)


class TestIncoming(TeamMailCase):
	def test_one_copy_of_a_mail_two_mailboxes_get(self):
		_customer_contact("known@example.org")
		raw = _raw("Known <known@example.org>", "ffltm-staff-a@example.com, ffltm-staff-b@example.com")
		_pull(STAFF_B, (raw, 7))
		_pull(STAFF_A, (raw, 41))
		_pull(STAFF_A, (raw, 41))   # fetched again by the next pull
		[kept] = _stored(raw)
		self.assertEqual((kept.email_account, kept.reference_doctype), (STAFF_B, "CRM Deal"))
		self.assertNoPullErrors()
		# The IMAP "ALL" pull starts after the skipped uid, not at it.
		self.assertEqual(get_max_email_uid(STAFF_A), 42)
		self.assertEqual(frappe.db.count("Communication", {"email_account": STAFF_A, "message_id": mail.MARKER_ID}), 1)
		_pull(STAFF_A, (_raw("someone@example.org", "ffltm-staff-a@example.com"), 45))
		self.assertEqual(get_max_email_uid(STAFF_A), 46)
		self.assertNoPullErrors()

	def test_direct_mail_from_someone_new_is_not_stored(self):
		raw = _raw("Stranger <friend@example.org>", "ffltm-staff-a@example.com")
		_pull(STAFF_A, (raw, 3))
		self.assertFalse(_stored(raw))
		self.assertFalse(frappe.get_all("CRM Lead", filters={"email": "friend@example.org"}))
		self.assertNoPullErrors()

	def test_direct_mail_from_a_customer_goes_on_their_open_deal(self):
		contact = _customer_contact("buyer@example.org")
		_pull(STAFF_A, (raw := _raw("buyer@example.org", "ffltm-staff-a@example.com"), 4))
		[kept] = _stored(raw)
		self.assertEqual(kept.reference_doctype, "CRM Deal")
		self.assertIn(contact, [c.contact for c in frappe.get_doc("CRM Deal", kept.reference_name).contacts])

	def test_direct_mail_from_a_known_lead_and_a_thread_reply_are_kept(self):
		lead = mail._new_lead("lead@example.org", "Lead Person")
		_pull(STAFF_A, (raw := _raw("lead@example.org", "ffltm-staff-a@example.com"), 5))
		self.assertEqual(_stored(raw)[0].reference_name, lead)
		# A reply to mail sent from CRM (Frappe threads it by In-Reply-To), from an address CRM does not know.
		sent = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Email", "sent_or_received": "Sent", "subject": "Your order",
			"sender": "ffltm-staff-a@example.com", "recipients": "lead@example.org", "reference_doctype": "CRM Lead",
			"reference_name": lead, "message_id": "ffl-test-thread@example.com"}).insert(ignore_permissions=True)
		reply = _raw("other.address@example.org", "ffltm-staff-a@example.com", in_reply_to=sent.message_id)
		_pull(STAFF_A, (reply, 6))
		self.assertEqual((_stored(reply)[0].reference_doctype, _stored(reply)[0].reference_name), ("CRM Lead", lead))

	def test_team_mail_from_someone_new_is_one_lead_without_owner(self):
		raw = _raw("Jane Q Public <jane@example.org>", SALES)
		_pull(INFO, (raw, 10))
		[kept] = _stored(raw)
		self.assertEqual(kept.reference_doctype, "CRM Lead")
		lead = frappe.get_doc("CRM Lead", kept.reference_name)
		self.assertEqual((lead.email, lead.first_name, lead.last_name), ("jane@example.org", "Jane", "Q Public"))
		self.assertFalse(lead.lead_owner)
		# Shared with the whole team, without notifying anyone, and once: a second mail adds nothing.
		self.assertEqual(_assignees("CRM Lead", lead.name), sorted(team.staff_users()))
		self.assertIn(CLERK, team.staff_users())
		self.assertTrue(_can_read(CLERK, "CRM Lead", lead.name))
		_pull(INFO, (_raw("jane@example.org", SALES), 12))
		self.assertEqual(_assignees("CRM Lead", lead.name), sorted(team.staff_users()), "no duplicate")
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, "lead_owner"))
		self.assertEqual(_notices(), self.notices)
		# Nobody is an open assignee: Frappe mails later inbound mail to open assignees only.
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, "_assign"))
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "CRM Lead", "reference_name": lead.name,
			"status": "Open"}))
		self.assertEqual(frappe.db.count("Email Queue"), self.queued)
		# A collector's mail is team mail even when no team address shows (a Bcc).
		bcc = _raw("bcc.buyer@example.org", "someone.else@example.org")
		_pull(INFO, (bcc, 11))
		self.assertEqual(_stored(bcc)[0].reference_doctype, "CRM Lead")
		self.assertNoPullErrors()

	def test_blocked_own_and_machine_team_mail_is_not_stored(self):
		for i, sender in enumerate(("deals@spam.example.net", "colleague@example.com", "no-reply@example.org")):
			raw = _raw(sender, SALES)
			_pull(INFO, (raw, 20 + i))
			self.assertFalse(_stored(raw), sender)
		self.assertNoPullErrors()

	def test_team_mail_is_left_to_its_collector(self):
		raw = _raw("new.buyer@example.org", "ffltm-staff-a@example.com", cc=SALES)
		_pull(STAFF_A, (raw, 30))
		self.assertFalse(_stored(raw), "info@ collects sales@: staff-a leaves it")
		_pull(INFO, (raw, 31))
		self.assertEqual([c.email_account for c in _stored(raw)], [INFO])
		self.assertNoPullErrors()

	def test_without_a_working_collector_staff_keep_team_mail_once(self):
		frappe.db.set_value("Email Account", INFO, "enable_incoming", 0, update_modified=False)
		raw = _raw("new.buyer@example.org", f"{SALES}, ffltm-staff-b@example.com")
		_pull(STAFF_A, (raw, 32))
		_pull(STAFF_B, (raw, 33))
		[kept] = _stored(raw)
		self.assertEqual((kept.email_account, kept.reference_doctype), (STAFF_A, "CRM Lead"))

	def test_mailboxes_outside_crm_are_left_alone(self):
		raw = _raw("applicant@example.org", "ffltm-jobs@example.com")
		_pull(JOBS, (raw, 1))
		[kept] = _stored(raw)
		self.assertFalse(kept.reference_doctype)
		self.assertFalse(frappe.get_all("CRM Lead", filters={"email": "applicant@example.org"}))
		self.assertFalse(frappe.db.exists("Communication", {"email_account": JOBS, "message_id": mail.MARKER_ID}))

	def test_a_failure_of_the_rules_keeps_the_mail(self):
		raw = _raw("friend@example.org", "ffltm-staff-a@example.com")
		with patch.object(mail, "decide", side_effect=RuntimeError("rules broken")):
			_pull(STAFF_A, (raw, 50))
		self.assertEqual(len(_stored(raw)), 1, "kept, unlinked, as before the rules")


class TestTeamVisibility(TeamMailCase):
	def test_a_customers_team_mail_shares_their_deal_and_keeps_its_owner(self):
		_customer_contact("team.buyer@example.org")
		_pull(STAFF_A, (_raw("team.buyer@example.org", "ffltm-staff-a@example.com"), 70))
		deal = frappe.get_all("Communication", filters={"sender": "team.buyer@example.org"}, pluck="reference_name")[0]
		self.assertFalse(_can_read(CLERK, "CRM Deal", deal), "direct mail: not shared")
		frappe.db.set_value("CRM Deal", deal, "deal_owner", MANAGER)
		_pull(INFO, (raw := _raw("team.buyer@example.org", SALES), 71))
		self.assertEqual(_stored(raw)[0].reference_name, deal, "the same open deal")
		self.assertTrue(_can_read(CLERK, "CRM Deal", deal))
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "deal_owner"), MANAGER)
		self.assertEqual(_notices(), self.notices)

	def test_a_colleagues_direct_mail_lead_stays_theirs(self):
		colleague = user("crm-clerk-2@example.com", "Sales User")
		lead = mail._new_lead("their.lead@example.org", "Their Lead")
		frappe.db.set_value("CRM Lead", lead, "lead_owner", colleague)
		_pull(STAFF_A, (raw := _raw("their.lead@example.org", "ffltm-staff-a@example.com"), 72))
		self.assertEqual(_stored(raw)[0].reference_name, lead)
		self.assertFalse(_can_read(CLERK, "CRM Lead", lead))
		self.assertTrue(_can_read(colleague, "CRM Lead", lead))
		self.assertEqual(_assignees("CRM Lead", lead), [])

	def test_the_notice_count_sees_an_ordinary_assignment(self):
		# Control for the "nobody is notified" checks: an assignment made by someone else is noticed.
		from frappe.desk.form.assign_to import _add

		lead = mail._new_lead("control@example.org", "Control")
		_add({"assign_to": [CLERK], "doctype": "CRM Lead", "name": lead}, ignore_permissions=True)
		self.assertGreater(_notices(), self.notices)


	def test_a_deal_converted_from_a_team_lead_stays_shared(self):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		_pull(INFO, (raw := _raw("Convert Me <convert.me@example.org>", SALES), 75))
		lead = _stored(raw)[0].reference_name
		deal = convert_to_deal(lead=lead)
		self.assertTrue(_can_read(CLERK, "CRM Deal", deal))
		self.assertEqual(_notices(), self.notices)
		# A deal from a lead that is not shared is not shared either.
		private = mail._new_lead("private.convert@example.org", "Private Convert")
		frappe.db.set_value("CRM Lead", private, "lead_owner", MANAGER)
		self.assertFalse(_can_read(CLERK, "CRM Deal", convert_to_deal(lead=private)))


class TestOwnerOnReply(TeamMailCase):
	def _send(self, doctype: str, name: str, as_user: str) -> None:
		from frappe.core.doctype.communication.email import make

		frappe.set_user(as_user)
		try:
			make(doctype=doctype, name=name, content="Yes, it is.", subject="Re: stock", sender="ffltm-staff-a@example.com",
				recipients="jane@example.org", send_email=False)
		finally:
			frappe.set_user("Administrator")

	def test_the_first_reply_owns_a_pool_lead(self):
		lead = mail._new_lead("jane@example.org", "Jane")
		self.assertFalse(frappe.db.get_value("CRM Lead", lead, "lead_owner"))
		self._send("CRM Lead", lead, MANAGER)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), MANAGER)
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "CRM Lead", "reference_name": lead,
			"allocated_to": MANAGER, "status": "Open"}), "assigned, as CRM assigns an owner")
		other = user("crm-manager-2@example.com", "Sales Manager")
		self._send("CRM Lead", lead, other)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), MANAGER, "an owner is never replaced")

	def test_the_first_reply_on_a_team_lead_owns_it_and_everyone_keeps_it(self):
		_pull(INFO, (raw := _raw("jane@example.org", SALES), 80))
		lead = _stored(raw)[0].reference_name
		self._send("CRM Lead", lead, CLERK)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), CLERK)
		self.assertEqual(sorted(set(_assignees("CRM Lead", lead))), sorted(team.staff_users()))
		self.assertEqual(frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": lead,
			"status": "Open"}, pluck="allocated_to"), [CLERK], "the owner is the one open assignee")
		self.assertTrue(_can_read(MANAGER, "CRM Lead", lead))
		self.assertEqual(_notices(), self.notices)

	def test_a_sales_user_sees_only_their_own_leads(self):
		# Frappe CRM's own rule (crm.permissions.org_hierarchy): a Sales User reads only leads they own
		# or hold a ToDo on, which is why team mail is shared (crm.mail.share_with_team). This test
		# fails when CRM changes that rule; the sharing then needs another look.
		lead = mail._new_lead("pool@example.org", "Pool")
		self.assertFalse(frappe.has_permission("CRM Lead", "read", doc=lead, user=CLERK))
		self.assertTrue(frappe.has_permission("CRM Lead", "read", doc=lead, user=MANAGER))
		frappe.db.set_value("CRM Lead", lead, "lead_owner", CLERK)
		self.assertTrue(frappe.has_permission("CRM Lead", "read", doc=lead, user=CLERK))

	def test_a_reply_in_the_senders_own_request_keeps_its_session(self):
		lead = mail._new_lead("session@example.org", "Session")
		frappe.set_user(CLERK)
		try:
			frappe.session.data["ffl_probe"] = 1
			mail._assign_quietly("CRM Lead", lead, CLERK)
			self.assertEqual((frappe.session.user, frappe.session.data.get("ffl_probe")), (CLERK, 1))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(_assignees("CRM Lead", lead), [CLERK])
		self.assertEqual(_notices(), self.notices)

	def test_the_first_reply_owns_a_deal_and_the_system_never_does(self):
		deal = frappe.get_doc("CRM Deal", frappe.db.get_value("Communication", {"name": _deal_comm()}, "reference_name"))
		self.assertFalse(deal.deal_owner)
		self._send("CRM Deal", deal.name, "Administrator")
		self.assertFalse(frappe.db.get_value("CRM Deal", deal.name, "deal_owner"))
		self._send("CRM Deal", deal.name, MANAGER)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, "deal_owner"), MANAGER)


def _deal_comm() -> str:
	"""A customer's team mail: their deal, without an owner."""
	_customer_contact("team.buyer@example.org")
	raw = _raw("team.buyer@example.org", SALES)
	_pull(INFO, (raw, 60))
	return _stored(raw)[0].name


def _reply_to(account: str) -> tuple[list[str], int]:
	return (frappe.get_all("Reply To Address", filters={"parenttype": "Email Account", "parent": account},
		pluck="email", order_by="idx asc"), frappe.db.get_value("Email Account", account, "add_reply_to_header"))


class TestSetup(TeamMailCase):
	def test_staff_mailboxes_reply_to_the_primary_team_address(self):
		self.assertEqual(_reply_to(STAFF_A), ([SALES], 1))
		self.assertEqual(_reply_to(STAFF_B), ([SALES], 1))
		self.assertEqual(_reply_to(INFO)[0], [], "a team mailbox answers as itself")
		self.assertEqual(_reply_to(JOBS)[0], [], "outside CRM")
		self.assertEqual(team.sync_reply_to(), [], "idempotent")
		# A manager's change on one mailbox is put back by the next save; a new primary reaches every one.
		frappe.db.delete("Reply To Address", {"parenttype": "Email Account", "parent": STAFF_B})
		_settings(primary_team_address="ffltm-info@example.com")
		self.assertEqual(_reply_to(STAFF_A), (["ffltm-info@example.com"], 1))
		self.assertEqual(_reply_to(STAFF_B), (["ffltm-info@example.com"], 1))

	def test_a_staff_mailbox_saved_later_gets_the_reply_to(self):
		doc = frappe.get_doc("Email Account", STAFF_B)
		doc.set("reply_to_addresses", [])
		doc.add_reply_to_header = 0
		team.reply_to_on_validate(doc)
		self.assertEqual(([r.email for r in doc.reply_to_addresses], doc.add_reply_to_header), ([SALES], 1))
		info = frappe.get_doc("Email Account", INFO)
		team.reply_to_on_validate(info)
		self.assertFalse(info.reply_to_addresses)

	def test_staff_get_the_team_mailbox_in_their_user_emails(self):
		for user in (CLERK, MANAGER):
			self.assertTrue(frappe.db.exists("User Email", {"parent": user, "email_account": INFO}), user)
		self.assertFalse(frappe.db.exists("User Email", {"parent": "Administrator", "email_account": INFO}))
		self.assertFalse(frappe.db.exists("User Email", {"email_account": STAFF_A, "parent": CLERK}),
			"a group address has no mailbox to answer from")
		self.assertEqual(team.sync_user_emails(), [], "idempotent")

	def test_crm_mailboxes_must_sync_all_mail(self):
		for account, auto in ((STAFF_B, 1), (INFO, 0)):   # a staff mailbox, a collector
			for use_imap, option in ((0, "UNSEEN"), (1, "UNSEEN")):   # POP3, IMAP unread only
				doc = frappe.get_doc("Email Account", account)
				doc.update({mail.AUTO_LEAD: auto, "use_imap": use_imap, "email_sync_option": option})
				with self.assertRaises(frappe.ValidationError, msg=f"{account} {use_imap} {option}"):
					team.require_full_sync(doc)
		doc = frappe.get_doc("Email Account", STAFF_B)
		team.require_full_sync(doc)   # IMAP, all mail
		doc.update({"enable_incoming": 0, "use_imap": 0})
		team.require_full_sync(doc)   # not pulling: nothing to lose
		jobs = frappe.get_doc("Email Account", JOBS)
		team.require_full_sync(jobs)   # outside CRM: any sync
		frappe.db.set_value("Email Account", INFO, "email_sync_option", "UNSEEN", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			_settings(primary_team_address=SALES)

	def test_settings_keep_team_addresses_clean(self):
		_settings(team_addresses=[{"address": " Parts@Example.com ", "address_type": team.GROUP}],
			primary_team_address="PARTS@example.com")
		doc = frappe.get_doc(mail.SETTINGS)
		self.assertEqual((doc.team_addresses[0].address, doc.primary_team_address), ("parts@example.com",) * 2)
		for rows, primary in (([{"address": "ffltm-info@example.com", "address_type": team.MAILBOX}], ""),
				([{"address": "a@example.com"}, {"address": "A@example.com"}], ""),
				([{"address": "a@example.com"}], "b@example.com")):
			with self.assertRaises(frappe.ValidationError):
				_settings(team_addresses=rows, primary_team_address=primary)


class TestConnectMyMailbox(TeamMailCase):
	APP = "_T Team Mail OAuth"

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Connected App", self.APP):
			frappe.get_doc({"doctype": "Connected App", "provider_name": self.APP, "name": self.APP,
				"client_id": "test-client", "authorization_uri": "https://accounts.example.com/o/oauth2/auth",
				"token_uri": "https://accounts.example.com/token",
				"scopes": [{"scope": "https://mail.example.com/"}]}).insert(ignore_permissions=True)
		self.app = frappe.db.get_value("Connected App", {"provider_name": self.APP}, "name")
		frappe.db.set_value("Email Account", STAFF_A, {"auth_method": "OAuth", "connected_app": self.app,
			"connected_user": CLERK}, update_modified=False)

	def _connect(self, as_user: str, **kwargs):
		from ffl_core.api.crm import connect_my_mailbox

		frappe.set_user(as_user)
		try:
			with patch.object(frappe.db, "commit"):
				return connect_my_mailbox(**kwargs)
		finally:
			frappe.set_user("Administrator")

	def test_staff_connect_their_own_mailbox_only(self):
		url = self._connect(CLERK)
		self.assertTrue(url.startswith("https://accounts.example.com/o/oauth2/auth?"), url)
		self.assertTrue(frappe.db.exists("Token Cache", f"{self.app}-{CLERK}"))
		self.assertEqual(frappe.db.get_value("Token Cache", f"{self.app}-{CLERK}", "user"), CLERK)
		with self.assertRaises(frappe.PermissionError):
			self._connect(MANAGER, email_account=STAFF_A)   # connected_user is the clerk
		with self.assertRaises(frappe.PermissionError):
			self._connect(MANAGER)
		frappe.db.set_value("Email Account", STAFF_A, "auth_method", "Basic", update_modified=False)
		with self.assertRaises(frappe.PermissionError):
			self._connect(CLERK)


class TestPullTeamMailboxes(TeamMailCase):
	"""The every-minute pull of team mailboxes; only the enqueue and the queue lookup are patched."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Email Account", INFO, "awaiting_password", 0, update_modified=False)
		self.queued_jobs: list[str] = []
		self.enqueue = patch.object(mail, "enqueue").start()
		self.get_jobs = patch.object(mail, "get_jobs", side_effect=lambda **kw: {frappe.local.site: self.queued_jobs}).start()
		self.addCleanup(patch.stopall)

	def _job(self, name: str) -> str:
		return f"pull_from_email_account|{name}"

	def test_a_collector_is_queued_like_frappes_own_pull(self):
		mail.pull_team_mailboxes()
		self.enqueue.assert_called_once_with(mail.pull_from_email_account, "short", event="all",
			job_name=self._job(INFO), email_account=INFO)

	def test_staff_mailboxes_are_left_to_frappes_own_pull(self):
		mail.pull_team_mailboxes()
		queued = [c.kwargs["email_account"] for c in self.enqueue.call_args_list]
		self.assertEqual(queued, [INFO])   # STAFF_A / STAFF_B are in scope but collect nothing

	def test_incoming_off_or_awaiting_password_is_not_queued(self):
		for field, value in (("enable_incoming", 0), ("awaiting_password", 1)):
			frappe.db.set_value("Email Account", INFO, field, value, update_modified=False)
			mail.pull_team_mailboxes()
			self.enqueue.assert_not_called()
			frappe.db.set_value("Email Account", INFO, field, 1 - value, update_modified=False)

	def test_oauth_without_a_token_is_not_queued(self):
		app = frappe.get_doc({"doctype": "Connected App", "provider_name": "_T Team Poll OAuth", "client_id": "test-client",
			"authorization_uri": "https://accounts.example.com/o/oauth2/auth", "token_uri": "https://accounts.example.com/token",
			"scopes": [{"scope": "https://mail.example.com/"}]}).insert(ignore_permissions=True).name
		frappe.db.set_value("Email Account", INFO, {"auth_method": "OAuth", "connected_app": app, "connected_user": CLERK},
			update_modified=False)
		mail.pull_team_mailboxes()
		self.enqueue.assert_not_called()
		# The app's own sign-in (no per-user token) is not that case.
		frappe.db.set_value("Email Account", INFO, "backend_app_flow", 1, update_modified=False)
		mail.pull_team_mailboxes()
		self.enqueue.assert_called_once()

	def test_a_job_already_queued_is_not_queued_twice(self):
		self.queued_jobs.append(self._job(INFO))
		mail.pull_team_mailboxes()
		self.enqueue.assert_not_called()

	def test_no_collectors_means_no_queue_lookup(self):
		_settings(team_addresses=[], primary_team_address="")
		mail.pull_team_mailboxes()
		self.get_jobs.assert_not_called()
		self.enqueue.assert_not_called()

	def test_nothing_without_crm(self):
		with patch.object(mail, "installed", return_value=False):
			mail.pull_team_mailboxes()
		self.get_jobs.assert_not_called()
		self.enqueue.assert_not_called()

	def test_it_is_scheduled_every_minute(self):
		from ffl_core.tests.test_crm_setup import _hooks_py

		self.assertIn("ffl_core.crm.mail.pull_team_mailboxes", _hooks_py()["scheduler_events"]["cron"]["* * * * *"])


class TestSendCrmMailNow(TeamMailCase):
	"""A mail sent on a CRM Lead / Deal is sent by a short job right after commit, not at the flush."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Email Account", STAFF_A, {"enable_outgoing": 1, "smtp_server": "smtp.example.com", "smtp_port": 587,
			"use_tls": 1, "login_id": ADDRESS[STAFF_A], "default_outgoing": 0, "always_use_account_email_id_as_sender": 1},
			update_modified=False)
		self.lead = mail._new_lead("jane@example.org", "Jane")
		self.enqueue = patch.object(mail, "enqueue").start()
		self.addCleanup(patch.stopall)

	def _send(self, doctype: str | None = "CRM Lead", name: str | None = None, **kwargs) -> str:
		"""A mail sent from the CRM form, through Frappe's own make(); returns the Communication."""
		from frappe.core.doctype.communication.email import make

		return make(doctype=doctype, name=name or self.lead, content="Yes, it is.", subject="Re: stock",
			sender=ADDRESS[STAFF_A], recipients="jane@example.org", send_email=True, **kwargs)["name"]

	def _queue_of(self, communication: str) -> str:
		return frappe.db.get_value("Email Queue", {"communication": communication}, "name")

	def test_crm_mail_is_queued_for_sending_after_commit(self):
		queue = self._queue_of(self._send())
		self.assertTrue(queue)
		self.enqueue.assert_called_once_with(mail.send_queued_crm_mail, "short", enqueue_after_commit=True,
			job_id=f"send_crm_mail|{queue}", deduplicate=True, name=queue)
		self.assertEqual(frappe.db.get_value("Email Queue", queue, "status"), "Not Sent", "sent by the job, not here")

	def test_other_mail_keeps_the_flush(self):
		# Not on a CRM record, a scheduled mail, a received mail, and a row with no Communication.
		self._send("User", CLERK)
		self._send(send_after=frappe.utils.add_to_date(now_datetime(), days=1))
		self.enqueue.assert_not_called()
		comm = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication", "sent_or_received": "Received",
			"subject": "In", "content": "In", "sender": "jane@example.org", "reference_doctype": "CRM Lead",
			"reference_name": self.lead}).insert(ignore_permissions=True).name
		for communication in (comm, None):
			frappe.get_doc({"doctype": "Email Queue", "sender": ADDRESS[STAFF_A], "message": "x", "communication": communication,
				"recipients": [{"recipient": "jane@example.org"}]}).insert(ignore_permissions=True)
		self.enqueue.assert_not_called()

	def test_a_queue_failure_does_not_break_the_send(self):
		self.enqueue.side_effect = RuntimeError("redis is down")
		errors = frappe.db.count("Error Log")
		queue = self._queue_of(self._send())
		self.assertEqual(frappe.db.get_value("Email Queue", queue, "status"), "Not Sent", "the flush will send it")
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)

	def test_the_job_sends_a_not_sent_row_and_leaves_a_sent_one(self):
		queue = self._queue_of(self._send())
		frappe.flags.testing_email = True   # Frappe skips the SMTP send in tests unless this is set
		self.addCleanup(frappe.flags.pop, "testing_email", None)
		# send() commits when it takes the row ("Sending"); not in the test transaction.
		with patch.object(frappe.db, "commit"), patch("frappe.email.smtp.SMTPServer.session", new_callable=PropertyMock) as session:
			session.return_value.has_extn.return_value = False
			mail.send_queued_crm_mail(queue)
			self.assertEqual(frappe.db.get_value("Email Queue", queue, "status"), "Sent")
			self.assertEqual(session.return_value.sendmail.call_count, 1)
			mail.send_queued_crm_mail(queue)   # the flush got there first, or the job runs twice
			mail.send_queued_crm_mail("no such row")
			self.assertEqual(session.return_value.sendmail.call_count, 1, "a Sent row is not sent again")


class TestContactNamedFromMail(TeamMailCase):
	"""The Contact Frappe makes from a mail takes the display name the lead has, so Convert to Deal keeps it."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Email Account", INFO, "create_contact", 1, update_modified=False)

	def _contact(self, email: str) -> dict:
		(name,) = mail._contacts_of(email)
		return frappe.db.get_value("Contact", name, ["name", "first_name", "last_name", "full_name"], as_dict=True)

	def _mail_from(self, display: str, address: str) -> str:
		"""Team mail from someone new through a real pull; returns the lead."""
		_pull(INFO, (raw := _raw(f"{display} <{address}>" if display else address, SALES), 90))
		(comm,) = _stored(raw)
		self.assertEqual(comm.reference_doctype, "CRM Lead")
		return comm.reference_name

	def test_an_auto_made_contact_takes_the_display_name_and_survives_convert_to_deal(self):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		lead = self._mail_from("Jane Q Public", "jqp99@example.org")
		contact = self._contact("jqp99@example.org")
		self.assertEqual((contact.first_name, contact.last_name, contact.full_name), ("Jane", "Q Public", "Jane Q Public"))
		self.assertEqual(contact.name, "Jqp99", "the document keeps the name Frappe gave it")
		with patch.object(frappe.db, "commit"):
			deal = convert_to_deal(lead=lead)
		self.assertEqual(tuple(frappe.db.get_value("CRM Lead", lead, ["first_name", "last_name"])), ("Jane", "Q Public"))
		deal_contact = frappe.get_doc("CRM Deal", deal).contacts[0].contact
		self.assertEqual(deal_contact, "Jqp99")
		self.assertEqual(frappe.db.get_value("Contact", deal_contact, "full_name"), "Jane Q Public")

	def test_a_hand_named_contact_is_left_alone(self):
		frappe.get_doc({"doctype": "Contact", "first_name": "Robert", "last_name": "Smith",
			"email_ids": [{"email_id": "hand55@example.org", "is_primary": 1}]}).insert(ignore_permissions=True)
		self._mail_from("Bob S", "hand55@example.org")
		self.assertEqual(tuple(self._contact("hand55@example.org")[k] for k in ("first_name", "last_name")), ("Robert", "Smith"))

	def test_no_display_name_leaves_the_contact(self):
		self._mail_from("", "nodisp@example.org")   # Frappe fills the display name with the address
		self.assertEqual(self._contact("nodisp@example.org").first_name, "Nodisp")
		mail._new_lead("nodisp@example.org", "nodisp@example.org")   # a display name that is the address
		self.assertEqual(self._contact("nodisp@example.org").first_name, "Nodisp")

	def test_a_contact_linked_to_anything_is_left_alone(self):
		customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_T Team Linked", "customer_type": "Individual"}
			).insert(ignore_permissions=True).name
		for address, link in (("linked1@example.org", ("Customer", customer)),
				("linked2@example.org", ("Company", frappe.db.get_value("Company", {}, "name")))):
			frappe.get_doc({"doctype": "Contact", "first_name": frappe.unscrub(address.split("@")[0]),
				"email_ids": [{"email_id": address, "is_primary": 1}],
				"links": [{"link_doctype": link[0], "link_name": link[1]}]}).insert(ignore_permissions=True)
			mail._new_lead(address, "Jane Public")
			self.assertEqual(self._contact(address).first_name, frappe.unscrub(address.split("@")[0]), address)
		# an auto-looking Contact someone gave a last name
		frappe.get_doc({"doctype": "Contact", "first_name": "Lastname3", "last_name": "Given",
			"email_ids": [{"email_id": "lastname3@example.org", "is_primary": 1}]}).insert(ignore_permissions=True)
		mail._new_lead("lastname3@example.org", "Jane Public")
		self.assertEqual(self._contact("lastname3@example.org").first_name, "Lastname3")

	def test_a_failure_does_not_stop_the_lead(self):
		errors = frappe.db.count("Error Log")
		with patch.object(mail, "_contacts_of", side_effect=RuntimeError("boom")):
			lead = mail._new_lead("fail1@example.org", "Jane Public")
		self.assertTrue(frappe.db.exists("CRM Lead", lead))
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
