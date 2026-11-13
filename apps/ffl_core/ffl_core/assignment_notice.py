"""Assignment notice: a person assigned a task is shown it and says they saw it.

Every assignment is a ToDo (desk Assign, CRM Task / Lead / Deal, Assignment Rules, the MCP and
the AI assistant acting for a user), so two ToDo hooks cover them all:

* :func:`stamp` (before_validate) decides once, at insert, whether this ToDo asks its person for
  an acknowledgement (``ffl_ack_required``; :func:`asks`). A ToDo that existed before this shipped
  carries no stamp and never asks. Re-assigning a ToDo (its ``allocated_to`` edited) is a new
  assignment: stamped again, the acknowledgement cleared. Any other save keeps both fields as
  stored — a client cannot set them; the time is written only by :func:`acknowledge`.
* :func:`announce` (on_update) tells the person's open desk tabs (realtime) to look again.

The desk (``public/js/assignment_notice.js``) gets the list in the boot (:func:`extend_bootinfo`)
and from :func:`pending`; one button acknowledges everything listed (:func:`acknowledge`), which
leaves a timeline comment on the document for the person who assigned it.
"""
from __future__ import annotations

import html
import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, get_fullname, now_datetime

REQUIRED, SEEN = "ffl_ack_required", "ffl_acknowledged_on"
EVENT = "ffl_assignment"

# Request-local marks a caller sets around its assign call (:func:`marked`).
SYSTEM_ALERT = "ffl_system_alert"   # alerts.alert_once: one alert to every manager
FOLLOW_UP = "ffl_follow_up"         # crm.deals.new_task: the system's follow-up for one person

# The owner's two switches.
# System alerts (alerts.alert_once) ask for an acknowledgement:
SYSTEM_ALERTS_ASK = False
# Automatic follow-ups for one person (CRM system tasks, Assignment Rule round-robin) ask:
FOLLOW_UPS_ASK = True

# lazy: 50 oldest per person, a second batch shows once these are acknowledged; page it if a store ever piles up more.
LIMIT = 50
TEXT_CAP = 500


@contextmanager
def marked(flag: str):
	"""Mark the ToDos inserted inside the block as *flag* (:data:`SYSTEM_ALERT`, :data:`FOLLOW_UP`)."""
	before = frappe.flags.get(flag)
	frappe.flags[flag] = True
	try:
		yield
	finally:
		frappe.flags[flag] = before


def asks(doc) -> bool:
	"""Whether this ToDo, as it is being assigned now, asks its person to acknowledge it: Open,
	given to someone, who is not the one assigning it; system alerts and automatic follow-ups per
	the switches above."""
	if doc.status != "Open" or not doc.allocated_to:
		return False
	if doc.allocated_to == (doc.assigned_by or frappe.session.user):
		return False
	if frappe.flags.get(SYSTEM_ALERT):
		return SYSTEM_ALERTS_ASK
	if frappe.flags.get(FOLLOW_UP) or doc.assignment_rule:
		return FOLLOW_UPS_ASK
	return True


def stamp(doc, method=None) -> None:
	"""ToDo before_validate: stamp a new assignment; any other save keeps the stored values."""
	before = None if doc.is_new() else (doc.get_doc_before_save()
		or frappe.db.get_value("ToDo", doc.name, ["allocated_to", REQUIRED, SEEN], as_dict=True))
	if before is not None and before.get("allocated_to") == doc.allocated_to:
		doc.set(REQUIRED, before.get(REQUIRED))
		doc.set(SEEN, before.get(SEEN))
		return
	doc.set(REQUIRED, 1 if asks(doc) else 0)
	doc.set(SEEN, None)
	doc.flags.ffl_ack_announce = bool(doc.get(REQUIRED))


def announce(doc, method=None) -> None:
	"""ToDo on_update: a stamped assignment reaches the person's open desk tabs after the commit."""
	if doc.flags.pop("ffl_ack_announce", False):
		frappe.publish_realtime(EVENT, user=doc.allocated_to, after_commit=True)


def _text(description: str | None) -> str:
	"""HTML (ToDo.description; a title Frappe stored escaped) as one line of plain text, capped."""
	text = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", description or "")).split())
	return text if len(text) <= TEXT_CAP else text[: TEXT_CAP - 1] + "…"


def _reference(doctype: str | None, name: str | None) -> str | None:
	"""The document's readable title, or None when there is none (no reference, or it is gone)."""
	if not (doctype and name) or not frappe.db.exists("DocType", doctype):
		return None
	field = frappe.get_meta(doctype).get_title_field()
	row = frappe.db.get_value(doctype, name, list({"name", field}), as_dict=True)
	return (_text(row.get(field)) or name) if row else None


def pending_for(user: str) -> list[dict]:
	"""*user*'s Open assignments that ask for an acknowledgement and have none yet, oldest first."""
	if not frappe.get_meta("ToDo").has_field(REQUIRED):   # before the migrate that adds the fields
		return []
	rows = frappe.get_all("ToDo",
		filters={"allocated_to": user, "status": "Open", REQUIRED: 1, SEEN: ("is", "not set")},
		fields=["name", "assigned_by", "owner", "reference_type", "reference_name", "description",
			"priority", "date", "creation"],
		order_by="creation asc", limit=LIMIT)
	out = []
	for r in rows:
		title = _reference(r.reference_type, r.reference_name)
		out.append({
			"name": r.name,
			"assigned_by": get_fullname(r.assigned_by or r.owner),
			"reference_type": r.reference_type if title else None,
			"reference_name": r.reference_name if title else None,
			"title": title,
			"description": _text(r.description),
			"priority": r.priority,
			"date": str(r.date) if r.date else None,
			"creation": str(r.creation),
		})
	return out


def extend_bootinfo(bootinfo) -> None:
	"""The signed-in person's assignments waiting for an acknowledgement, for the first paint."""
	if frappe.session.user == "Guest":
		return
	bootinfo.ffl_assignments = pending_for(frappe.session.user)


@frappe.whitelist()
def pending() -> list[dict]:
	"""Your assignments waiting for you to say you saw them, oldest first (at most 50).

	Returns:
	    [{"name": ToDo, "assigned_by": full name, "reference_type", "reference_name", "title" (all
	    three None when the document is gone or there is none), "description": plain text,
	    "priority", "date": due date or None, "creation"}].
	"""
	return pending_for(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def acknowledge(names: list[str] | str) -> list[dict]:
	"""Say you saw these assignments. Each one is yours or the call is refused; one that is closed,
	cancelled, gone, already acknowledged or never asked is skipped. The time is stored on the ToDo
	and the document gets a timeline comment.

	Args:
	    names: ToDo names (a list, or its JSON), at most 50.

	Returns:
	    What is still waiting, as :func:`pending` returns it.
	"""
	if isinstance(names, str):
		try:
			names = frappe.parse_json(names)
		except ValueError:
			names = None
	if not isinstance(names, list) or len(names) > LIMIT or not all(isinstance(n, str) for n in names):
		frappe.throw(_("Send at most {0} assignments.").format(LIMIT))
	user = frappe.session.user
	rows = frappe.db.get_values("ToDo", {"name": ("in", names)},
		["name", "allocated_to", "status", "reference_type", "reference_name", REQUIRED, SEEN],
		as_dict=True, for_update=True) if names else []
	if any(r.allocated_to != user for r in rows):
		frappe.throw(_("Only the person it is assigned to can say they saw it."), frappe.PermissionError)
	now = now_datetime()
	who = get_fullname(user)
	for r in rows:
		if r.status != "Open" or not cint(r.get(REQUIRED)) or r.get(SEEN):
			continue
		frappe.db.set_value("ToDo", r.name, SEEN, now, update_modified=False)
		_comment(r.reference_type, r.reference_name, who)
	return pending_for(user)


def _comment(doctype: str | None, name: str | None, who: str) -> None:
	"""'<who> saw this assignment' on the document's timeline; never fails the acknowledgement."""
	if not (doctype and name) or not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name):
		return
	frappe.db.savepoint("ffl_ack_comment")
	try:
		frappe.get_doc(doctype, name).add_comment("Info", _("{0} saw this assignment").format(who))
	except Exception:
		frappe.db.rollback(save_point="ffl_ack_comment")
		from ffl_core.alerts import safe_log_error

		safe_log_error(f"Assignment comment failed: {doctype} {name}", frappe.get_traceback())
