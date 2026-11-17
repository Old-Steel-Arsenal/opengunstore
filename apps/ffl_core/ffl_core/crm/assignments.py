"""A finished CRM Lead or Deal takes its assignments off its people's My tasks page.

Frappe CRM gives a lead's or deal's owner an Open ToDo, and so do our mail rules, web orders and sell
requests; nothing in CRM closes it. Here:

* a deal whose status becomes Won or Lost (its CRM Deal Status type) closes its Open assignments;
  back to an open status (a web order cancelled, ``crm.web_orders``, or staff by hand), the
  assignments this rule closed open again;
* a lead converted to a deal, or moved to Other mail (``crm.mail.OTHER_MAIL``), closes its Open
  assignments; a lead moved off Other mail gets back the ones this rule closed.

Only the record's own assignments: ToDos whose reference is the lead or deal. A CRM Task's ToDos
and a manager's own closed or cancelled ones are left alone.

*Closed*, never Cancelled: Frappe CRM lets a Sales User read a record they hold a ToDo on that is
not Cancelled (``crm.permissions.org_hierarchy``), and ``crm.mail.share_with_team`` shares team mail
with Closed ToDos the same way. A ToDo this rule closed carries :data:`MARK` (a hidden ToDo field,
``setup/assignment_notice``), so a reopened record reopens only those: one its person closed on My
tasks stays closed. A save that takes the ToDo off Closed clears the mark
(``assignment_notice.stamp``).

The ToDos are written as rows (no ToDo hooks), then the record's assignee list (``_assign``) is
recomputed the way Frappe's ToDo does: through the hooks each closing would post an "Assignment of
X removed" line on the timeline, and a reopening the same wrong line again. The status change is
the record's own history.

Runs on ``on_change``, which follows a save and also CRM's ``db_set`` (its lead conversion marks the
lead converted that way, after it copied the lead's open assignees to the deal). Never fails the
save: a failure is rolled back and logged.
"""
from __future__ import annotations

import frappe

from ffl_core.assignment_notice import CLOSED_WITH_RECORD as MARK
from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import CLOSED, DEAL, STATUS
from ffl_core.crm.mail import LEAD, OTHER_MAIL

_SAVEPOINT = "ffl_crm_assignments"


def deal_changed(doc, method=None) -> None:
	"""CRM Deal on_change: Won / Lost closes, back to an open status reopens."""
	if not doc.has_value_changed("status"):
		return
	_apply(doc, close=frappe.db.get_value(STATUS, doc.status, "type") in CLOSED)


def lead_changed(doc, method=None) -> None:
	"""CRM Lead on_change: converted or Other mail closes; off Other mail reopens."""
	finished = bool(doc.get("converted")) or doc.get("status") == OTHER_MAIL
	if finished and (doc.has_value_changed("converted") or doc.has_value_changed("status")):
		_apply(doc, close=True)
	elif not finished and doc.has_value_changed("status"):
		before = doc.get_doc_before_save()
		if before and before.get("status") == OTHER_MAIL:
			_apply(doc, close=False)


def _apply(doc, close: bool) -> None:
	if not installed() or not frappe.db.has_column("ToDo", MARK):
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if set_assignments(doc.doctype, doc.name, close):
			# The record's in-memory copy too: a later save in this request writes it back.
			doc.set("_assign", frappe.db.get_value(doc.doctype, doc.name, "_assign"))
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Assignments not {'closed' if close else 'reopened'}: {doc.doctype} {doc.name}",
			message=frappe.get_traceback())


def set_assignments(doctype: str, name: str, close: bool) -> list[str]:
	"""Close the record's Open assignments (and mark them), or reopen the ones marked. Returns the
	ToDos changed."""
	filters = {"reference_type": doctype, "reference_name": name}
	filters.update({"status": "Open"} if close else {"status": "Closed", MARK: 1})
	todos = frappe.get_all("ToDo", filters=filters, pluck="name")
	if not todos:
		return []
	frappe.db.set_value("ToDo", {"name": ["in", todos]}, {"status": "Closed" if close else "Open", MARK: int(close)})
	# Frappe's own recount of who is assigned (ToDo.update_in_reference), once for the record.
	todo = frappe.new_doc("ToDo")
	todo.update({"reference_type": doctype, "reference_name": name})
	todo.update_in_reference()
	return todos


def finished_records() -> list[tuple[str, str]]:
	"""(doctype, name) of every finished deal and lead that still has an Open assignment: the
	backfill's work list (patches/close_finished_crm_assignments)."""
	closed = frappe.get_all(STATUS, filters={"type": ["in", list(CLOSED)]}, pluck="name")
	open_on = lambda doctype: set(frappe.get_all("ToDo", filters={"reference_type": doctype, "status": "Open"},
		pluck="reference_name", distinct=True))
	out = []
	deals = open_on(DEAL)
	if deals and closed:
		out += [(DEAL, n) for n in frappe.get_all(DEAL, filters={"name": ["in", list(deals)], "status": ["in", closed]},
			pluck="name", order_by="name asc")]
	leads = open_on(LEAD)
	if leads:
		out += [(LEAD, n) for n in frappe.get_all(LEAD, filters={"name": ["in", list(leads)]},
			or_filters={"converted": 1, "status": OTHER_MAIL}, pluck="name", order_by="name asc")]
	return out
