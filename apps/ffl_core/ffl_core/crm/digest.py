"""The daily "who to answer" email: the customers on CRM leads and deals who wait for a reply, longest
waiting first. No AI call.

The queue (:func:`build`): every CRM Lead (not converted, not Other mail) and CRM Deal whose status is of
type Open or Ongoing, where the newest email on the record is a Received one from a sender that is not
automated (``crm.mail.is_automated``), plus any such record with an open CRM Task. A website inquiry is
also a Received mail on its record (``crm.inquiries._file_mail``), so it counts. The queue is read from
the records' own mail and tasks and is never stored. What it carries is put through
``crm.sensitive.redact`` first (the subject, the task titles and the record's summary).

Who gets what (:func:`send`, every day at 08:00 site time): a record's owner gets their own records; the
addresses in Lead Mail Settings › ``crm_digest_recipients`` get every record, ownerless ones included.
One mail per address, one plain table, from the default outgoing Email Account. Nobody with an empty
list is mailed.

It is never filed back into CRM as customer mail:

* ``frappe.sendmail`` here has no reference document and no Communication, so the POS itself stores
  nothing. The mail comes back only through a mailbox the CRM mail rules read.
* Pulled by a staff mailbox (``Email Account.ffl_auto_lead``) it is direct mail, and ``crm.mail.decide``
  keeps direct mail only from a Customer's contact or a known lead. A sender on one of Lead Mail
  Settings › Our own domains, or one that looks automated (noreply@), is skipped there as "blocked
  sender" before any lead is looked up; any other address that is no customer and has no lead is
  skipped as "direct mail from someone new".
* Mail to a team address would be team mail, which makes a lead for a sender nobody knows (or, from
  an own domain, other mail): so a team address is never a recipient (:func:`recipients`, and Lead Mail
  Settings refuses one on save).
* A copy in the sending mailbox's Sent folder is read by ``crm.mail.check_sent_folders`` when that
  mailbox is one of ours. The mail carries ``Auto-Submitted: auto-generated`` (Frappe writes it as
  ``X-Auto-Submitted``), which ``crm.mail._machine_written`` skips whatever its recipients are.

lazy: the mail and task rows of every open record are read at once, with no limit; upgrade path: read
the newest mail per record in SQL, or keep a waiting-since field on the record, if a store ever has
tens of thousands of open leads.
"""
from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import date_diff, escape_html, formatdate, get_datetime, get_fullname, get_url, getdate, nowdate, parse_addr, validate_email_address

from ffl_core.crm import TITLE, installed, memory, sensitive, team
from ffl_core.crm.deals import DEAL, STATUS as DEAL_STATUS, person_name
from ffl_core.crm.mail import LEAD, OTHER_MAIL, OWNER_FIELD, is_automated
from ffl_core.utils import single_flag, single_raw

SETTINGS = "Lead Mail Settings"
SWITCH, RECIPIENTS = "crm_daily_digest", "crm_digest_recipients"
LEAD_STATUS = "CRM Lead Status"
TASK = "CRM Task"
OPEN_STATUS_TYPES = ["Open", "Ongoing"]
OPEN_TASK_STATUSES = ["Backlog", "Todo", "In Progress"]
PATH = {LEAD: "/crm/leads/", DEAL: "/crm/deals/"}
MACHINE_HEADERS = {"Auto-Submitted": "auto-generated"}   # Frappe sends it as X-Auto-Submitted


def on() -> bool:
	"""Lead Mail Settings › crm_daily_digest, off until a manager turns it on."""
	return installed() and single_flag(SETTINGS, SWITCH, default=0)


def parse_recipients(value: str | None) -> list[str]:
	"""A settings field's addresses: separated by commas, semicolons, spaces or new lines; lowercase, no repeats."""
	out = []
	for part in re.split(r"[,;\s]+", value or ""):
		address = part.strip().lower()
		if address and address not in out:
			out.append(address)
	return out


def _team_addresses() -> set[str]:
	return {r.address for r in team.rows()}


def recipients() -> list[str]:
	"""The addresses that get every record: Lead Mail Settings' list without what is not an address or
	is a team address."""
	teams = _team_addresses()
	return [a for a in parse_recipients(single_raw(SETTINGS, RECIPIENTS))
		if validate_email_address(a, throw=False) == a and a not in teams]


def check_settings(doc, method=None) -> None:
	"""Lead Mail Settings validate: the recipients are clean addresses, one per line, none of them a team
	address (the mail would come back as team mail)."""
	teams = _team_addresses()
	addresses = parse_recipients(doc.get(RECIPIENTS))
	for address in addresses:
		if validate_email_address(address, throw=False) != address:
			frappe.throw(_("{0} is not an email address.").format(frappe.bold(address)))
		if address in teams:
			frappe.throw(_("{0} is a team address. Use a staff member's own address.").format(frappe.bold(address)))
	if doc.meta.has_field(RECIPIENTS):
		doc.set(RECIPIENTS, "\n".join(addresses))


# ── the queue ──

def _open_statuses(status_doctype: str) -> list[str]:
	return [s for s in frappe.get_all(status_doctype, filters={"type": ["in", OPEN_STATUS_TYPES]}, pluck="name")
		if s != OTHER_MAIL]


def _candidates(doctype: str, user: str | None) -> list[frappe._dict]:
	meta = frappe.get_meta(doctype)
	owner = OWNER_FIELD[doctype]
	fields = ["name", "status", "lead_name", "email", "organization", owner]
	fields += [f for f in ("first_name", "last_name", "erpnext_customer") if meta.has_field(f)]
	if frappe.db.has_column(doctype, memory.SUMMARY):
		fields.append(memory.SUMMARY)
	filters = {"status": ["in", _open_statuses(LEAD_STATUS if doctype == LEAD else DEAL_STATUS) or [""]]}
	if doctype == LEAD:
		filters["converted"] = 0
	if user:
		filters[owner] = user
	return frappe.get_all(doctype, filters=filters, fields=fields)


def _address(sender: str | None) -> str:
	return (parse_addr(sender or "")[1] or "").strip().lower()


def _mail_by_record(doctype: str, names: list[str]) -> dict[str, list]:
	"""Each record's emails, newest first."""
	by: dict[str, list] = {}
	if not names:
		return by
	for m in frappe.get_all("Communication", filters={"reference_doctype": doctype, "reference_name": ["in", names],
			"communication_type": "Communication", "communication_medium": "Email"},
			fields=["reference_name", "sender", "subject", "sent_or_received", "communication_date", "creation"],
			order_by="communication_date desc, creation desc"):
		by.setdefault(m.reference_name, []).append(m)
	return by


def _tasks_by_record(doctype: str, names: list[str]) -> dict[str, list]:
	by: dict[str, list] = {}
	if not names:
		return by
	for t in frappe.get_all(TASK, filters={"reference_doctype": doctype, "reference_docname": ["in", names],
			"status": ["in", OPEN_TASK_STATUSES]}, fields=["reference_docname", "title", "creation"], order_by="creation asc"):
		by.setdefault(t.reference_docname, []).append(t)
	return by


def _customer(doctype: str, rec) -> str:
	if doctype == LEAD:
		return rec.lead_name or " ".join(n for n in (rec.get("first_name"), rec.get("last_name")) if n) or rec.email or rec.name
	contact = frappe.db.get_value("CRM Contacts", {"parenttype": DEAL, "parent": rec.name, "is_primary": 1}, "contact")
	return person_name(contact, rec.get("erpnext_customer"), rec.email, rec.organization)["lead_name"] or rec.lead_name or rec.name


def _row(doctype: str, rec, mails: list, tasks: list) -> dict | None:
	"""This record's row in the queue, or None when nobody waits on it and no task is open."""
	newest = mails[0] if mails else None
	waiting = bool(newest) and newest.sent_or_received == "Received" and not is_automated(_address(newest.sender))
	if not waiting and not tasks:
		return None
	since_sent = 0
	for m in mails:   # the customer's messages after the store's last mail
		if m.sent_or_received == "Sent":
			break
		since_sent += not is_automated(_address(m.sender))
	received = get_datetime(newest.communication_date or newest.creation) if waiting else None
	return {"doctype": doctype, "name": rec.name, "link": PATH[doctype] + rec.name,
		"customer": _customer(doctype, rec), "owner": rec.get(OWNER_FIELD[doctype]),
		"subject": sensitive.redact(newest.subject or "") if waiting else "", "received": received,
		"days": date_diff(nowdate(), getdate(received)) if waiting else None, "messages": since_sent if waiting else 0,
		"tasks": [sensitive.redact(t.title or "") for t in tasks], "summary": sensitive.redact(rec.get(memory.SUMMARY) or ""),
		"_task_since": get_datetime(tasks[0].creation) if tasks else None}


def build(user: str | None = None) -> list[dict]:
	"""The queue, longest waiting first (a record with only an open task follows, oldest task first).
	*user*: only the records that user owns. Each row has ``doctype``, ``name``, ``link`` (the CRM page's
	path), ``customer``, ``owner`` (a user or None), ``subject`` and ``received`` of the waiting message
	(None when only a task keeps the record here), ``days`` waiting, ``messages`` from the customer since
	the store's last mail, ``tasks`` (the open tasks' titles) and ``summary``."""
	if not installed():
		return []
	rows = []
	for doctype in (LEAD, DEAL):
		recs = _candidates(doctype, user)
		names = [r.name for r in recs]
		mails, tasks = _mail_by_record(doctype, names), _tasks_by_record(doctype, names)
		rows += filter(None, (_row(doctype, r, mails.get(r.name, []), tasks.get(r.name, [])) for r in recs))
	rows.sort(key=lambda r: (r["received"] is None, r["received"] or r["_task_since"], r["name"]))
	for r in rows:
		del r["_task_since"]
	return rows


# ── the mail ──

def _subject(count: int) -> str:
	return _("1 customer waiting for a reply") if count == 1 else _("{0} customers waiting for a reply").format(count)


def _cell(value) -> str:
	return f'<td style="border:1px solid #ccc;padding:4px 8px;vertical-align:top">{value}</td>'


def table(rows: list[dict]) -> str:
	"""The email's one table; every value escaped."""
	head = "".join(f'<th style="border:1px solid #ccc;padding:4px 8px;text-align:left">{escape_html(_(h))}</th>'
		for h in ("Customer", "Owner", "Waiting message", "Date", "Days", "Messages", "Tasks", "Summary"))
	body = []
	for r in rows:
		body.append("<tr>" + "".join((
			_cell(f'<a href="{escape_html(get_url(r["link"]))}">{escape_html(r["customer"])}</a><br>{escape_html(r["name"])}'),
			_cell(escape_html(get_fullname(r["owner"]) if r["owner"] else "")),
			_cell(escape_html(r["subject"])),
			_cell(escape_html(formatdate(r["received"]) if r["received"] else "")),
			_cell("" if r["days"] is None else str(r["days"])),
			_cell(str(r["messages"] or "")),
			_cell("<br>".join(escape_html(t) for t in r["tasks"])),
			_cell(escape_html(r["summary"])))) + "</tr>")
	return f'<table style="border-collapse:collapse;font-size:13px"><tr>{head}</tr>{"".join(body)}</table>'


def _plan(rows: list[dict]) -> dict[str, list[dict]]:
	"""Address -> the rows that address gets: every row for the settings' recipients, an owner's own rows
	for an enabled owner with an address (the settings' list wins when it holds them too)."""
	teams = _team_addresses()
	plan = {address: rows for address in recipients()}
	for user in sorted({r["owner"] for r in rows if r["owner"]}):
		address = (frappe.db.get_value("User", {"name": user, "enabled": 1}, "email") or "").strip().lower()
		if address and address not in teams and address not in plan:
			plan[address] = [r for r in rows if r["owner"] == user]
	return {a: rs for a, rs in plan.items() if rs}


def send() -> int:
	"""Scheduler, 08:00 site time: queue the email for each address that has a non-empty list. A failure
	for one address is logged and the others still go. Returns how many were queued."""
	if not on():
		return 0
	sent = 0
	for address, rows in _plan(build()).items():
		try:
			frappe.sendmail(recipients=[address], subject=_subject(len(rows)), message=table(rows), delayed=True,
				email_headers=MACHINE_HEADERS)
			sent += 1
		except Exception:
			frappe.log_error(title=f"[{TITLE}] Daily reply list not queued: {address}", message=frappe.get_traceback())
	return sent
