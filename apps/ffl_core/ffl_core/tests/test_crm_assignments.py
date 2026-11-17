"""A finished CRM deal or lead takes its assignments off My tasks (ffl_core.crm.assignments), and the
one-shot patch does the same for the ones already finished."""
from __future__ import annotations

import json

import frappe
from frappe.desk.form.assign_to import _add

from ffl_core import assignment_notice
from ffl_core.assignment_notice import CLOSED_WITH_RECORD as MARK
from ffl_core.crm import mail
from ffl_core.crm.deals import DEAL, STATUS, _statuses, new_task
from ffl_core.patches import close_finished_crm_assignments
from ffl_core.tests.crm_support import CLERK, MANAGER, CRMTestCase

LEAD = "CRM Lead"


def _status(type_: str) -> str:
	return frappe.db.get_value(STATUS, {"type": type_}, "name", order_by="position asc")


def _todos(doctype: str, name: str) -> dict[str, tuple[str, int]]:
	"""{allocated_to: (status, mark)} of the record's own ToDos."""
	return {r.allocated_to: (r.status, r.get(MARK)) for r in frappe.get_all("ToDo",
		filters={"reference_type": doctype, "reference_name": name}, fields=["allocated_to", "status", MARK])}


def _assign(doctype: str, name: str, user: str) -> None:
	_add({"assign_to": [user], "doctype": doctype, "name": name}, ignore_permissions=True)


def _readable(user: str, doctype: str, name: str) -> bool:
	"""CRM's permission rule (crm.permissions.org_hierarchy), on the record and in its list."""
	frappe.set_user(user)
	try:
		return bool(frappe.has_permission(doctype, "read", doc=name, user=user)
			and frappe.get_list(doctype, filters={"name": name}, pluck="name"))
	finally:
		frappe.set_user("Administrator")


def _deal() -> str:
	"""A deal owned by MANAGER with CLERK as a second assignee (not the owner: only the ToDo lets CLERK read it)."""
	deal = frappe.get_doc({"doctype": DEAL, "status": _statuses(closed=False)[0], "deal_owner": MANAGER}
		).insert(ignore_permissions=True).name
	_assign(DEAL, deal, CLERK)
	frappe.db.set_value(DEAL, deal, "deal_owner", MANAGER, update_modified=False)
	return deal


def _set_status(doctype: str, name: str, status: str) -> None:
	doc = frappe.get_doc(doctype, name)
	doc.status = status
	if doctype == DEAL and frappe.db.get_value(STATUS, status, "type") == "Lost":
		doc.lost_reason, doc.lost_notes = "Other", "test"
		doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)


class TestDealAssignments(CRMTestCase):
	def test_a_won_deal_closes_its_assignments_and_stays_readable(self):
		deal = _deal()
		self.assertEqual(_todos(DEAL, deal), {MANAGER: ("Open", 0), CLERK: ("Open", 0)})
		before = assignment_notice.open_count(CLERK)
		_set_status(DEAL, deal, _status("Won"))
		self.assertEqual(_todos(DEAL, deal), {MANAGER: ("Closed", 1), CLERK: ("Closed", 1)})
		self.assertEqual(assignment_notice.open_count(CLERK), before - 1, "off My tasks")
		self.assertEqual(frappe.db.get_value(DEAL, deal, "_assign") or "", "", "no one listed as assigned")
		self.assertTrue(_readable(CLERK, DEAL, deal), "a Closed ToDo still lets the Sales User read the deal")
		self.assertEqual(frappe.db.get_value(DEAL, deal, "deal_owner"), MANAGER)

	def test_reopening_a_lost_deal_reopens_only_what_the_rule_closed(self):
		deal = _deal()
		# CLERK closes their own assignment on My tasks first: that one stays closed.
		frappe.set_user(CLERK)
		try:
			assignment_notice.done(frappe.db.get_value("ToDo", {"reference_name": deal, "allocated_to": CLERK}))
		finally:
			frappe.set_user("Administrator")
		task = new_task("Call back", "", MANAGER, deal)
		other = _deal()
		_set_status(DEAL, deal, _status("Lost"))
		self.assertEqual(_todos(DEAL, deal), {MANAGER: ("Closed", 1), CLERK: ("Closed", 0)})
		self.assertEqual(_todos("CRM Task", task), {MANAGER: ("Open", 0)}, "a task's ToDo is not the deal's")
		self.assertEqual(_todos(DEAL, other), {MANAGER: ("Open", 0), CLERK: ("Open", 0)}, "another deal's")
		_set_status(DEAL, deal, _statuses(closed=False)[0])
		self.assertEqual(_todos(DEAL, deal), {MANAGER: ("Open", 0), CLERK: ("Closed", 0)})
		self.assertEqual(frappe.db.get_value(DEAL, deal, "_assign"), json.dumps([MANAGER]))

	def test_a_rule_closed_todo_opened_by_hand_loses_its_mark(self):
		deal = _deal()
		_set_status(DEAL, deal, _status("Won"))
		todo = frappe.get_doc("ToDo", {"reference_name": deal, "allocated_to": CLERK})
		todo.status = "Open"
		todo.save(ignore_permissions=True)
		todo.status = "Closed"
		todo.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, MARK), 0)
		_set_status(DEAL, deal, _statuses(closed=False)[0])
		self.assertEqual(_todos(DEAL, deal)[CLERK], ("Closed", 0), "its person closed it the second time")

	def test_a_save_that_keeps_the_status_leaves_new_assignments_alone(self):
		deal = _deal()
		_set_status(DEAL, deal, _status("Won"))
		_assign(DEAL, deal, MANAGER)   # a follow-up on the won deal
		doc = frappe.get_doc(DEAL, deal)
		doc.probability = 50
		doc.save(ignore_permissions=True)
		self.assertIn(("Open", 0), [(r.status, r.get(MARK)) for r in frappe.get_all("ToDo",
			filters={"reference_name": deal, "allocated_to": MANAGER}, fields=["status", MARK])])


class TestLeadAssignments(CRMTestCase):
	def _lead(self, email: str) -> str:
		lead = mail._new_lead(email, "Close Me")
		_assign(LEAD, lead, MANAGER)
		return lead

	def test_conversion_closes_the_leads_assignment_and_the_deal_keeps_its_owner(self):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		lead = self._lead("convert.close@example.org")
		deal = convert_to_deal(lead=lead)
		self.assertEqual(_todos(LEAD, lead), {MANAGER: ("Closed", 1)})
		self.assertEqual(_todos(DEAL, deal), {MANAGER: ("Open", 0)})
		self.assertTrue(_readable(MANAGER, LEAD, lead))

	def test_other_mail_closes_and_back_reopens(self):
		lead = self._lead("other.close@example.org")
		_set_status(LEAD, lead, mail.OTHER_MAIL)
		self.assertEqual(_todos(LEAD, lead), {MANAGER: ("Closed", 1)})
		back = frappe.get_all("CRM Lead Status", filters={"name": ["!=", mail.OTHER_MAIL], "type": "Open"},
			pluck="name", order_by="position asc", limit=1)[0]
		_set_status(LEAD, lead, back)
		self.assertEqual(_todos(LEAD, lead)[MANAGER], ("Open", 0))

	def test_shared_team_todos_stay_as_they_are(self):
		lead = self._lead("team.close@example.org")
		mail.share_with_team(LEAD, lead)
		shared = frappe.get_all("ToDo", filters={"reference_name": lead, "description": mail.SHARED_NOTE},
			fields=["name", "status", MARK])
		_set_status(LEAD, lead, mail.OTHER_MAIL)
		self.assertEqual(frappe.get_all("ToDo", filters={"reference_name": lead, "description": mail.SHARED_NOTE},
			fields=["name", "status", MARK]), shared)


class TestPatch(CRMTestCase):
	def test_closes_what_is_already_finished_once(self):
		won, open_deal = _deal(), _deal()
		task = new_task("Deliver", "", MANAGER, won)
		# Finished before the hooks existed: written as rows.
		frappe.db.set_value(DEAL, won, "status", _status("Won"))
		converted = mail._new_lead("patch.convert@example.org", "Patch Convert")
		_assign(LEAD, converted, MANAGER)
		frappe.db.set_value(LEAD, converted, "converted", 1)
		other = mail._new_lead("patch.other@example.org", "Patch Other")
		_assign(LEAD, other, CLERK)
		frappe.db.set_value(LEAD, other, "status", mail.OTHER_MAIL)
		live = mail._new_lead("patch.live@example.org", "Patch Live")
		_assign(LEAD, live, CLERK)

		close_finished_crm_assignments.execute()
		self.assertEqual(_todos(DEAL, won), {MANAGER: ("Closed", 1), CLERK: ("Closed", 1)})
		self.assertEqual(_todos(LEAD, converted), {MANAGER: ("Closed", 1)})
		self.assertEqual(_todos(LEAD, other), {CLERK: ("Closed", 1)})
		self.assertEqual(_todos(DEAL, open_deal), {MANAGER: ("Open", 0), CLERK: ("Open", 0)})
		self.assertEqual(_todos(LEAD, live), {CLERK: ("Open", 0)})
		self.assertEqual(_todos("CRM Task", task), {MANAGER: ("Open", 0)})
		self.assertTrue(_readable(CLERK, DEAL, won))

		snapshot = frappe.get_all("ToDo", fields=["name", "status", MARK, "modified"], order_by="name asc")
		close_finished_crm_assignments.execute()
		self.assertEqual(frappe.get_all("ToDo", fields=["name", "status", MARK, "modified"], order_by="name asc"),
			snapshot, "a second run changes nothing")
