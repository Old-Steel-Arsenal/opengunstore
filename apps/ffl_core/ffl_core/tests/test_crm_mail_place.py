"""CRM mail: a mailbox's first IMAP pull keeps its place when its first mail is skipped, and Frappe's
renumbering statement locks only that mailbox's mail (crm/mail.py ``_write_marker``,
crm/setup.py ``ensure_mail_account_index``).

The pull runs Frappe's own ``EmailAccount.receive``, ``get_inbound_mails`` and ``EmailServer``
(``check_imap_uidvalidity`` included) against a fake IMAP connection; the pull's commit / rollback
are mapped onto a savepoint, as in test_crm_team_mail. The lock test uses two connections of its own
with committed rows it removes again, outside the test transaction.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import frappe
from frappe.email.doctype.email_account.email_account import EmailAccount, get_max_email_uid
from frappe.email.receive import EmailServer

from ffl_core.crm import mail, setup
from ffl_core.tests.test_crm_team_mail import ADDRESS, STAFF_A, TeamMailCase, _raw, _stored

PULL = "ffl_test_place_pull"
STRANGER = "stranger-{}@example.org"
KNOWN = "ffl-place-lead@example.org"


class FakeIMAP:
	"""One folder: uid → raw mail. Answers what Frappe's ``EmailServer`` asks, and records the fetches."""

	def __init__(self, mails: dict[int, bytes], validity: str = "7"):
		self.mails, self.validity, self.fetched = dict(mails), validity, []

	def select(self, folder, readonly=False):
		return "OK", [str(len(self.mails)).encode()]

	def status(self, folder, what):
		return "OK", [f'{folder} (UIDVALIDITY {self.validity} UIDNEXT {max(self.mails or [0]) + 1})'.encode()]

	def uid(self, command, *args):
		if command == "search":
			first, _sep, last = args[1].split()[1].partition(":")
			last = max(self.mails or [0]) if last in ("*", "") else int(last)
			found = [u for u in sorted(self.mails) if int(first) <= u <= last]
			if not found and args[1].endswith(":*") and self.mails:
				found = [max(self.mails)]   # "n:*" always answers the newest mail
			return "OK", [" ".join(map(str, found)).encode()]
		uid = int(args[0])
		self.fetched.append(uid)
		raw = self.mails[uid]
		return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

	def logout(self):
		return "BYE", []


def _pull(account: str, imap: FakeIMAP) -> None:
	"""One run of Frappe's ``EmailAccount.receive`` against *imap*, commits and rollbacks on a savepoint."""
	doc = frappe.get_doc("Email Account", account)
	real_rollback = frappe.db.rollback

	def connect(server):
		server.imap = imap
		return True

	def commit(*args, **kwargs):
		frappe.db.savepoint(PULL)

	def rollback(*, save_point=None, chain=False):
		if save_point:
			return real_rollback(save_point=save_point)
		real_rollback(save_point=PULL)
		frappe.db.value_cache.clear()
		frappe.db.after_rollback.run()   # what Database.rollback runs after a real ROLLBACK

	frappe.db.savepoint(PULL)
	with patch.object(EmailServer, "connect_imap", connect), \
			patch.object(frappe.db, "commit", side_effect=commit), \
			patch.object(frappe.db, "rollback", side_effect=rollback):
		doc.receive()


def _inbox(account: str) -> dict:
	return frappe.db.get_value("IMAP Folder", {"parent": account, "folder_name": "INBOX"},
		["uidvalidity", "uidnext"], as_dict=True)


def _marker_uid(account: str):
	return frappe.db.get_value("Communication", {"email_account": account, "message_id": mail.MARKER_ID}, "uid")


def _stranger(n: int) -> bytes:
	return _raw(STRANGER.format(n), ADDRESS[STAFF_A])


class TestFirstPullKeepsItsPlace(TeamMailCase):
	"""A staff mailbox enabled with an INBOX row and no saved UIDVALIDITY (its first pull still to come)."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Email Account", STAFF_A, {"email_server": "imap.example.invalid", "use_ssl": 1,
			"initial_sync_count": "100", "awaiting_password": 0}, update_modified=False)
		frappe.db.delete("IMAP Folder", {"parent": STAFF_A})
		frappe.get_doc({"doctype": "IMAP Folder", "parent": STAFF_A, "parenttype": "Email Account",
			"parentfield": "imap_folder", "folder_name": "INBOX", "idx": 1}).db_insert()
		mail._new_lead(KNOWN, "Known Lead")

	def test_a_skipped_first_mail_does_not_undo_the_first_sync(self):
		known = _raw(f"Known Lead <{KNOWN}>", ADDRESS[STAFF_A])
		imap = FakeIMAP({1: _stranger(1), 2: known, 3: _stranger(3)})
		_pull(STAFF_A, imap)
		self.assertEqual(sorted(imap.fetched), [1, 2, 3])
		# The place is saved although the first mail was skipped (it was lost with the rollback before).
		self.assertEqual(_inbox(STAFF_A), {"uidvalidity": "7", "uidnext": "4"})
		[kept] = _stored(known)
		self.assertEqual(kept.reference_doctype, "CRM Lead")
		# The kept mail's uid survives the later skip; the marker holds the highest skipped uid.
		self.assertEqual(frappe.db.get_value("Communication", kept.name, "uid"), 2)
		self.assertEqual(_marker_uid(STAFF_A), 3)
		self.assertEqual(get_max_email_uid(STAFF_A), 4)
		self.assertNoPullErrors()

		# The next pull is an ordinary one: only the new mail, nothing renumbered.
		imap.mails[4] = _stranger(4)
		imap.fetched.clear()
		_pull(STAFF_A, imap)
		self.assertEqual(imap.fetched, [4])
		self.assertEqual(frappe.db.get_value("Communication", kept.name, "uid"), 2)
		self.assertEqual(_marker_uid(STAFF_A), 4)
		self.assertEqual(_inbox(STAFF_A).uidvalidity, "7")
		self.assertNoPullErrors()

	def test_old_uids_are_renumbered_as_frappe_does(self):
		"""Mail stored under an old numbering (and an old marker) must not set where the pull starts."""
		old = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Email", "sent_or_received": "Received", "email_account": STAFF_A,
			"sender": KNOWN, "subject": "Old mail", "uid": 500, "message_id": f"old-{uuid.uuid4().hex}@example.org"})
		old.db_insert()
		mail._write_marker(STAFF_A, 900)
		_pull(STAFF_A, FakeIMAP({1: _stranger(1), 2: _stranger(2)}))
		self.assertEqual(frappe.db.get_value("Communication", old.name, "uid"), -1)
		self.assertEqual(_marker_uid(STAFF_A), 2)
		self.assertEqual(get_max_email_uid(STAFF_A), 3)
		self.assertEqual(_inbox(STAFF_A), {"uidvalidity": "7", "uidnext": "3"})
		self.assertNoPullErrors()

	def test_a_changed_uidvalidity_is_kept_too(self):
		frappe.db.set_value("IMAP Folder", {"parent": STAFF_A}, {"uidvalidity": "5", "uidnext": 40})
		mail._write_marker(STAFF_A, 39)
		_pull(STAFF_A, FakeIMAP({1: _stranger(1)}, validity="8"))
		self.assertEqual(_inbox(STAFF_A), {"uidvalidity": "8", "uidnext": "2"})
		self.assertEqual(_marker_uid(STAFF_A), 1)
		self.assertNoPullErrors()

	def test_a_settled_account_is_not_renumbered_by_a_skip(self):
		frappe.db.set_value("IMAP Folder", {"parent": STAFF_A}, {"uidvalidity": "7", "uidnext": 10})
		known = _raw(f"Known Lead <{KNOWN}>", ADDRESS[STAFF_A])
		imap = FakeIMAP({9: known, 10: _stranger(10)})
		mail._write_marker(STAFF_A, 8)
		_pull(STAFF_A, imap)
		self.assertEqual(imap.fetched, [9, 10])
		[kept] = _stored(known)
		self.assertEqual(frappe.db.get_value("Communication", kept.name, "uid"), 9)
		self.assertEqual(_marker_uid(STAFF_A), 10)
		self.assertNoPullErrors()


class TestRenumberingLocksOneMailbox(TeamMailCase):
	def test_the_index_is_made_and_kept(self):
		self.assertTrue(frappe.db.get_column_index("tabCommunication", "email_account", unique=False))
		self.assertEqual(frappe.get_meta("Communication").get_field("email_account").search_index, 1)
		self.assertFalse(setup.ensure_mail_account_index())   # already there: no DDL

	def test_frappes_renumbering_leaves_other_mailboxes_writable(self):
		"""Frappe's statement for one mailbox, held open, as during a first pull's fetch: another
		mailbox's mail can still be updated and inserted (it waited for the lock and failed before)."""
		import MySQLdb

		conf = frappe.conf
		kw = dict(host=conf.db_host or "127.0.0.1", port=int(conf.db_port or 3306), user=conf.db_user or conf.db_name,
			passwd=conf.db_password, db=conf.db_name)
		tag = "ffl-lock-" + frappe.generate_hash(length=6)
		one, other = MySQLdb.connect(**kw), MySQLdb.connect(**kw)
		a, b = one.cursor(), other.cursor()
		insert = ("insert into tabCommunication (name, communication_medium, email_account, uid, creation, modified)"
			" values (%s, 'Email', %s, 5, now(), now())")
		rows = [(f"{tag}-{i}", f"{tag}-busy-{i % 3}") for i in range(600)] + [(f"{tag}-x{i}", f"{tag}-new") for i in range(3)]
		a.executemany(insert, rows)
		one.commit()
		try:
			a.execute("analyze table tabCommunication")
			a.fetchall()
			a.execute("update tabCommunication set uid = -1 where communication_medium = 'Email' and email_account = %s",
				(f"{tag}-new",))
			self.assertEqual(a.rowcount, 3)
			b.execute("set session innodb_lock_wait_timeout = 2")
			started = time.monotonic()
			b.execute("update tabCommunication set uid = 6 where name = %s", (f"{tag}-1",))
			b.execute(insert, (f"{tag}-late", f"{tag}-busy-0"))
			self.assertLess(time.monotonic() - started, 1.5)
			other.rollback()
			one.rollback()
		finally:
			other.rollback()
			one.rollback()
			a.execute("delete from tabCommunication where name like %s", (f"{tag}-%",))
			one.commit()
			one.close()
			other.close()
