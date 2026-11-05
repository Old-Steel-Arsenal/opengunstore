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
from unittest.mock import patch

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


def _customer_contact(email: str) -> str:
	customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"_T Team Buyer {email}",
		"customer_type": "Individual"}).insert(ignore_permissions=True)
	return frappe.get_doc({"doctype": "Contact", "first_name": "Known", "email_ids": [{"email_id": email, "is_primary": 1}],
		"links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert(ignore_permissions=True).name


class TeamMailCase(CRMTestCase):
	def setUp(self):
		super().setUp()
		_account(STAFF_A, auto=1, imap_all=1)
		_account(STAFF_B, auto=1)
		_account(INFO)
		_account(JOBS)
		_settings(blocked_sender_domains="spam.example.net", own_domains="example.com",
			team_addresses=[{"address": "ffltm-info@example.com", "address_type": team.MAILBOX, "collector": INFO},
				{"address": SALES, "address_type": team.GROUP, "collector": INFO}],
			primary_team_address=SALES)
		self.errors = frappe.db.count("Error Log")
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
		self.assertFalse(frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": lead.name,
			"status": "Open"}))
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

	def test_a_sales_user_sees_only_their_own_leads(self):
		# Frappe CRM's own rule (crm.permissions.org_hierarchy): a Sales User reads only leads they own
		# or are assigned, so the pool of owner-less leads is worked by Sales Managers. This test fails
		# when CRM changes that rule; the guide's "who answers team mail" then needs another look.
		lead = mail._new_lead("pool@example.org", "Pool")
		self.assertFalse(frappe.has_permission("CRM Lead", "read", doc=lead, user=CLERK))
		self.assertTrue(frappe.has_permission("CRM Lead", "read", doc=lead, user=MANAGER))
		frappe.db.set_value("CRM Lead", lead, "lead_owner", CLERK)
		self.assertTrue(frappe.has_permission("CRM Lead", "read", doc=lead, user=CLERK))

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
