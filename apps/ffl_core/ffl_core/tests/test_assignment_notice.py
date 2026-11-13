"""The assignment notice (ffl_core.assignment_notice): which assignments ask their person to say
they saw them, what the person is shown, and the acknowledgement.

Real ToDos through Frappe's own assign_to, CRM's CRM Task and our alert / follow-up helpers, on
the test site; every test runs inside a savepoint rolled back after it."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_core import alerts
from ffl_core import assignment_notice as an
from ffl_core.setup import assignment_notice as setup
from ffl_core.tests.crm_support import user
from ffl_core.tests.test_staff_switch import _hooks_list

BOSS = "an-boss@example.com"
CLERK = "an-clerk@example.com"
OTHER = "an-other@example.com"
SAVEPOINT = "ffl_assignment_notice_test"


def _note(title="Restock the case") -> str:
	return frappe.get_doc({"doctype": "Note", "title": title, "public": 1}).insert(ignore_permissions=True).name


def _assign(to, note, by=BOSS, description="<p>Count the <b>.22</b> boxes &amp; reorder</p>"):
	"""*by* assigns *note* to *to* through the desk's own Assign; returns the ToDo."""
	frappe.set_user(by)
	try:
		assign_to.add({"assign_to": [to], "doctype": "Note", "name": note, "description": description})
	finally:
		frappe.set_user("Administrator")
	return frappe.get_last_doc("ToDo", filters={"reference_name": note, "allocated_to": to})


def _todo(**fields):
	return frappe.get_doc({"doctype": "ToDo", "description": "x", **fields}).insert(ignore_permissions=True)


def _as(who, fn, *args, **kw):
	frappe.set_user(who)
	try:
		return fn(*args, **kw)
	finally:
		frappe.set_user("Administrator")


class AssignmentNoticeCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup.ensure()
		user(BOSS, "System Manager", "Sales Manager")
		user(CLERK, "Sales User")
		user(OTHER, "Sales User")
		boss = frappe.get_doc("User", BOSS)
		boss.update({"first_name": "Pat", "last_name": "Boss"})
		boss.save(ignore_permissions=True)
		getattr(frappe.local, "fullnames", {}).pop(BOSS, None)   # get_fullname's per-process cache

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()

	def stamp(self, todo):
		return frappe.db.get_value("ToDo", todo, [an.REQUIRED, an.SEEN], as_dict=True)


class Stamping(AssignmentNoticeCase):
	def test_assigned_by_someone_else_asks(self):
		t = _assign(CLERK, _note())
		self.assertEqual(self.stamp(t.name)[an.REQUIRED], 1)
		self.assertIsNone(self.stamp(t.name)[an.SEEN])

	def test_self_assigned_unassigned_and_closed_do_not_ask(self):
		self.assertEqual(self.stamp(_assign(BOSS, _note()).name)[an.REQUIRED], 0, "self-assigned")
		self.assertEqual(self.stamp(_todo().name)[an.REQUIRED], 0, "nobody's")
		closed = _as(BOSS, _todo, allocated_to=CLERK, status="Closed")
		self.assertEqual(self.stamp(closed.name)[an.REQUIRED], 0, "inserted Closed")
		desk_new = _as(BOSS, _todo, allocated_to=CLERK)   # a ToDo typed in the form: assigned_by blank
		self.assertEqual(self.stamp(desk_new.name)[an.REQUIRED], 1)

	def test_system_alert_does_not_ask(self):
		note = _note()
		self.assertTrue(alerts.alert_once("Note", note, title="an-test alert", message="m",
			description="Stuck", assignees=[CLERK]))
		t = frappe.get_last_doc("ToDo", filters={"reference_name": note, "allocated_to": CLERK})
		self.assertEqual(self.stamp(t.name)[an.REQUIRED], int(an.SYSTEM_ALERTS_ASK))
		self.assertFalse(frappe.flags.get(an.SYSTEM_ALERT), "the mark ends with the call")
		# The next assignment in the same request is a person's again.
		self.assertEqual(self.stamp(_assign(OTHER, note).name)[an.REQUIRED], 1)

	def test_crm_task_assignment_and_follow_up_ask(self):
		frappe.set_user(BOSS)
		try:
			task = frappe.get_doc({"doctype": "CRM Task", "title": "Call back", "status": "Todo",
				"assigned_to": CLERK}).insert()
		finally:
			frappe.set_user("Administrator")
		t = frappe.get_last_doc("ToDo", filters={"reference_type": "CRM Task", "reference_name": task.name})
		self.assertEqual(t.allocated_to, CLERK)
		self.assertEqual(self.stamp(t.name)[an.REQUIRED], 1)

		from ffl_core.crm.deals import new_task

		name = new_task("Payment failed", "Call them", assignee=CLERK)
		t = frappe.get_last_doc("ToDo", filters={"reference_type": "CRM Task", "reference_name": name})
		self.assertEqual(self.stamp(t.name)[an.REQUIRED], int(an.FOLLOW_UPS_ASK))
		self.assertFalse(frappe.flags.get(an.FOLLOW_UP))

	def test_a_client_cannot_set_either_field(self):
		t = _as(BOSS, _todo, allocated_to=BOSS, **{an.REQUIRED: 1, an.SEEN: now_datetime()})
		self.assertEqual(self.stamp(t.name), {an.REQUIRED: 0, an.SEEN: None})
		t = _assign(CLERK, _note())
		t.set(an.REQUIRED, 0)
		t.set(an.SEEN, now_datetime())
		t.description = "edited"
		t.save(ignore_permissions=True)
		self.assertEqual(self.stamp(t.name), {an.REQUIRED: 1, an.SEEN: None})

	def test_reassigning_is_a_new_assignment(self):
		t = _assign(CLERK, _note())
		_as(CLERK, an.acknowledge, [t.name])
		self.assertIsNotNone(self.stamp(t.name)[an.SEEN])
		frappe.set_user(BOSS)
		try:
			t = frappe.get_doc("ToDo", t.name)
			t.allocated_to = OTHER
			with patch("frappe.publish_realtime") as pub:
				t.save()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.stamp(t.name), {an.REQUIRED: 1, an.SEEN: None})
		pub.assert_any_call(an.EVENT, user=OTHER, after_commit=True)
		self.assertEqual([r["name"] for r in _as(OTHER, an.pending)], [t.name])
		# Given back to the one who assigned it: nothing to acknowledge.
		t.reload()
		t.allocated_to = BOSS
		t.save(ignore_permissions=True)
		self.assertEqual(self.stamp(t.name)[an.REQUIRED], 0)

	def test_realtime_only_for_a_stamped_assignment(self):
		with patch("frappe.publish_realtime") as pub:
			_assign(CLERK, _note())
			_assign(BOSS, _note())
		calls = [c for c in pub.call_args_list if c.args and c.args[0] == an.EVENT]
		self.assertEqual([c.kwargs["user"] for c in calls], [CLERK])


class Pending(AssignmentNoticeCase):
	def test_what_the_person_is_shown(self):
		note = _note("Restock the case")
		t = _assign(CLERK, note)
		rows = _as(CLERK, an.pending)
		self.assertEqual(len(rows), 1)
		r = rows[0]
		self.assertEqual(r["name"], t.name)
		self.assertEqual(r["assigned_by"], "Pat Boss")
		self.assertEqual((r["reference_type"], r["reference_name"], r["title"]), ("Note", note, "Restock the case"))
		self.assertEqual(r["description"], "Count the .22 boxes & reorder")

	def test_title_is_plain_text(self):
		note = _note("Smith & Wesson <b>case</b>")
		_assign(CLERK, note)
		self.assertEqual(_as(CLERK, an.pending)[0]["title"], "Smith & Wesson case")

	def test_only_your_own_and_never_an_old_row(self):
		mine = _assign(CLERK, _note())
		_assign(OTHER, _note())
		old = _assign(CLERK, _note())
		frappe.db.set_value("ToDo", old.name, an.REQUIRED, 0)   # a ToDo from before this shipped
		self.assertEqual([r["name"] for r in _as(CLERK, an.pending)], [mine.name])

	def test_long_text_is_capped(self):
		_assign(CLERK, _note(), description="<div>" + "word " * 300 + "</div>")
		text = _as(CLERK, an.pending)[0]["description"]
		self.assertEqual(len(text), an.TEXT_CAP)
		self.assertTrue(text.endswith("…"))

	def test_boot_carries_the_list(self):
		self.assertIn("ffl_core.assignment_notice.extend_bootinfo", _hooks_list("extend_bootinfo"))
		self.assertIn("ffl_core.setup.assignment_notice.ensure", _hooks_list("after_install"))
		self.assertIn("ffl_core.setup.assignment_notice.ensure", _hooks_list("after_migrate"))
		t = _assign(CLERK, _note())
		boot = frappe._dict()
		_as(CLERK, an.extend_bootinfo, boot)
		self.assertEqual([r["name"] for r in boot.ffl_assignments], [t.name])


class Acknowledge(AssignmentNoticeCase):
	def comments(self, note):
		return frappe.get_all("Comment", filters={"reference_doctype": "Note", "reference_name": note,
			"comment_type": "Info"}, pluck="content")

	def test_acknowledge_records_time_and_comment_once(self):
		note = _note()
		t = _assign(CLERK, note)
		self.assertEqual(_as(CLERK, an.acknowledge, [t.name]), [])
		self.assertIsNotNone(self.stamp(t.name)[an.SEEN])
		seen = self.stamp(t.name)[an.SEEN]
		self.assertEqual(self.comments(note), ["an-clerk saw this assignment"])
		# Twice (a second tab): harmless, nothing changes.
		self.assertEqual(_as(CLERK, an.acknowledge, frappe.as_json([t.name])), [])
		self.assertEqual(self.stamp(t.name)[an.SEEN], seen)
		self.assertEqual(len(self.comments(note)), 1)
		self.assertEqual(frappe.db.get_value("ToDo", t.name, "status"), "Open", "acknowledging is not closing")

	def test_someone_elses_is_refused_and_nothing_written(self):
		mine = _assign(CLERK, _note())
		theirs = _assign(OTHER, _note())
		with self.assertRaises(frappe.PermissionError):
			_as(CLERK, an.acknowledge, [mine.name, theirs.name])
		self.assertIsNone(self.stamp(mine.name)[an.SEEN])
		self.assertIsNone(self.stamp(theirs.name)[an.SEEN])

	def test_closed_cancelled_and_unknown_are_skipped(self):
		closed, cancelled, still = (_assign(CLERK, _note()) for _ in range(3))
		frappe.db.set_value("ToDo", closed.name, "status", "Closed")
		frappe.db.set_value("ToDo", cancelled.name, "status", "Cancelled")
		left = _as(CLERK, an.acknowledge, [closed.name, cancelled.name, "no-such-todo"])
		self.assertEqual([r["name"] for r in left], [still.name])
		self.assertIsNone(self.stamp(closed.name)[an.SEEN])
		self.assertIsNone(self.stamp(cancelled.name)[an.SEEN])

	def test_document_gone_still_acknowledges(self):
		note = _note()
		t = _assign(CLERK, note)
		frappe.db.delete("Note", note)   # the ToDo outlives it (no hooks)
		row = _as(CLERK, an.pending)[0]
		self.assertEqual((row["reference_type"], row["reference_name"], row["title"]), (None, None, None))
		self.assertEqual(_as(CLERK, an.acknowledge, [t.name]), [])
		self.assertIsNotNone(self.stamp(t.name)[an.SEEN])

	def test_input_is_checked(self):
		for bad in ([1], "not json", ["x"] * (an.LIMIT + 1)):
			with self.assertRaises(frappe.ValidationError):
				_as(CLERK, an.acknowledge, bad)
		self.assertEqual(_as(CLERK, an.acknowledge, []), [])
