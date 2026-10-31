"""One-shot operational alerts: Error Log + a manager ToDo, deduped.

The shape every "a document is stuck and a human must look" path in the FFL
apps needs, factored out of ``pos_consolidate._alert_stuck`` so the web
stock leg and the web revenue backstop reuse it instead of re-implementing it
(and dropping the dedupe order, which is what turns a persistent failure into
one identical Error Log per cron tick — ``tasks._report_aged_out`` documents
that trap).

Order matters: the open-ToDo check comes FIRST and gates the log too, so a
condition that persists across runs is logged + assigned exactly once until a
person closes the ToDo. The ToDo goes through ``assign_to.add`` — a bare ToDo
insert sets no allocated_to and sends no notification, so nobody would see it.
"""
from __future__ import annotations

import frappe
from frappe.utils.user import get_system_managers


# Who hears about a stranded document by default.
ALERT_ROLES = ("Stock Manager", "Sales Manager", "System Manager")


def alert_assignees(roles=ALERT_ROLES) -> list[str]:
	"""Enabled human users holding any of ``roles`` — by default the inventory /
	sales managers + system managers. Administrator and Guest are excluded (the
	scheduler runs as Administrator; alerting itself helps nobody)."""
	users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", list(roles)], "parenttype": "User"},
		pluck="parent", distinct=True,
	)
	out = []
	for u in users:
		if u in ("Administrator", "Guest"):
			continue
		if not frappe.db.get_value("User", u, "enabled"):
			continue
		out.append(u)
	return sorted(set(out))


def safe_log_error(title: str, message: str) -> None:
	"""``frappe.log_error`` that cannot take its caller down with it.

	It raises on its own when ``frappe.flags.currently_saving`` is uninitialised
	(worker / scheduler contexts, the bench test runner) — and every caller is
	already in a failure path (or a webhook that must still ack), where a raising
	logger turns "one thing went wrong" into "the job died". Falls back to the file
	logger and never raises. The one copy for all three apps: ffl_integrations and
	ffl_woo_sync re-export it under their historical names."""
	try:
		frappe.log_error(title=title, message=message)
	except Exception:
		try:
			frappe.logger("ffl_core.alerts", allow_site=True).error(
				f"{title}: {message[:1000]}")
		except Exception:
			pass


_log = safe_log_error


def alert_recipients() -> list[str]:
	"""Who gets operational-alert email: FFL Settings → Alert Recipients
	(comma / newline separated), else every enabled System Manager, so an alert is
	never silently dropped. Not :func:`alert_assignees` — that is the desk-ToDo
	rule (several roles, user names)."""
	raw = frappe.db.get_single_value("FFL Settings", "alert_recipients") or ""
	emails = [e.strip() for e in raw.replace("\n", ",").split(",") if e.strip()]
	return emails or get_system_managers(only_name=True)


def alert_once(reference_type: str, reference_name: str, *, title: str, message: str,
		description: str, priority: str = "High", assignees: list[str] | None = None,
		dedupe_on_title: bool = False, since=None) -> bool:
	"""Log ``title``/``message`` and assign a ``priority`` ToDo on the document to
	the managers — once. Returns True when this call raised a NEW alert, False
	when the document already carries an open ToDo (nothing logged, nothing
	assigned). ``assignees`` overrides :func:`alert_assignees` (an explicit empty
	list means "log only, nobody to assign").

	``dedupe_on_title``: "once" means once per TITLE instead — an Error Log with
	this title (created at or after ``since``, when given) already exists. For an
	alert that must not be swallowed by an unrelated open ToDo on the same
	document; give each distinct event its own title."""
	from frappe.desk.form.assign_to import add as assign_to_add

	if dedupe_on_title:
		if frappe.db.exists("Error Log", {"method": title,
				**({"creation": (">=", since)} if since else {})}):
			return False
	elif frappe.db.exists(
		"ToDo",
		{"reference_type": reference_type, "reference_name": reference_name, "status": "Open"},
	):
		return False
	_log(title, message)
	if assignees is None:
		assignees = alert_assignees()
	if not assignees:
		return True  # logged; nobody to assign to
	try:
		assign_to_add({
			"assign_to": assignees,
			"doctype": reference_type,
			"name": reference_name,
			"priority": priority,
			"notify": 1,
			"description": description,
		})
	except Exception:
		# The assignment reaches the desk + mail; every caller is already in a
		# failure path and the Error Log above is the part that must survive.
		_log(f"Alert assignment failed: {reference_type} {reference_name}",
			frappe.get_traceback())
	return True


def notify_staff(doctype: str, name: str, subject: str, roles, match: dict | None = None) -> None:
	"""Put ``subject`` in the desk bell (Notification Log) of every enabled user
	holding one of ``roles``, linked to the document.

	In-app only: type "Alert" is in Frappe's ``notification_skip_email_types``, so
	the log never sends its own email. Once per (document, subject) and recipient —
	callers word the subject from the event and the status, so a redelivered event
	does not ring twice. Everything happens in a job after the commit (inline under
	tests, like Frappe's own enqueue_create_notification): the caller's transaction
	only queues it, and a failure lands in the Error Log, never in the caller.

	``match``: fields the document must still carry when the job runs (e.g. the
	GunBroker order id). A savepoint rollback undoes an insert AND its naming-series
	step, so the next insert can take the same name; the name alone would then ring
	for a different document."""
	try:
		frappe.enqueue("ffl_core.alerts.ring_staff", now=frappe.in_test,
			enqueue_after_commit=not frappe.in_test,
			doctype=doctype, name=name, subject=subject, roles=list(roles), match=match or {})
	except Exception:
		_log(f"Staff notification failed: {doctype} {name}", frappe.get_traceback())


def ring_staff(doctype: str, name: str, subject: str, roles: list[str], match: dict | None = None) -> None:
	"""The job behind :func:`notify_staff`."""
	from frappe.desk.doctype.notification_log.notification_log import make_notification_logs
	from frappe.utils import escape_html

	# A savepoint rollback (the GunBroker poll's per-order one) undoes the insert
	# but not the queued job: no bell for a document that never landed.
	if not frappe.db.exists(doctype, {"name": name, **(match or {})}):
		return
	users = alert_assignees(roles)
	if not users:
		return
	make_notification_logs(frappe._dict({
		"type": "Alert",
		# The bell renders the subject as HTML; names in it come from web buyers.
		"subject": escape_html(subject),
		"document_type": doctype,
		"document_name": name,
		"dedupe_on": ["document_type", "document_name", "subject"],
	}), frappe.get_all("User", filters={"name": ("in", users)}, pluck="email"))
