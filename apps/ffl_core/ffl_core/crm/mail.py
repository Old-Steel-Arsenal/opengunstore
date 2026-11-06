"""CRM mail: which pulled mail is kept, where it goes, and who owns a lead once someone answers it.

Two kinds of Email Account are in scope (``crm.team``): a *collector*, which brings in the mail of a
team address (sales@, info@), and a *staff* mailbox (``Email Account.ffl_auto_lead`` on), which brings
in only business mail. Every other Email Account (a store's noreply, a jobs inbox) is left alone.
CRM's own "Create Lead from Incoming Emails" would make a lead for anyone, a known customer
included; :func:`take_over_lead_flag` moves that switch to ours so CRM's never runs.

For each received mail of an in-scope account, in order (:func:`decide`):

1. the same Message-ID is already in from another in-scope account, or is one we sent: skipped;
2. it is *team mail* when To / Cc / Bcc hold a team address (all of a collector's mail is);
   otherwise *direct* mail. The Delivered-To header is not kept by Frappe (Communication stores
   To / Cc / Bcc only), so a group's mail is recognised by its To / Cc;
3. team mail whose address another (enabled) Email Account collects: skipped, the collector keeps it;
4. a reply in a thread Frappe already linked: kept as linked;
   from a Customer's contact: on that contact's open deal (``crm.deals``; owned by the mailbox's user
   for direct mail, no owner for team mail);
   from a blocked domain, one of our own domains, or an automated sender (noreply and the like): skipped;
   from an existing CRM Lead: on that lead;
   team mail from someone new: a new CRM Lead with no owner (a round robin address leaves the owner to
   CRM's Assignment Rule);
   team mail on a lead or deal (new or existing, a thread reply included) shares it with every staff
   user, silently (:func:`share_with_team`), and so does a deal converted from a shared lead
   (:func:`share_converted_deal`): Frappe CRM lets a Sales User read only records they own or hold a
   ToDo on. Direct mail does not: its lead or deal stays with its owner and Sales Managers, until the
   customer answers a staff reply: that answer goes to the staff mailbox's Reply-To, the team address,
   so it is team mail and shares the record (by design: whoever answers next can take it over);
   direct mail from someone new: skipped (a staff member's own mail never enters the POS).

Screening: when an extension registers ``gunshop_crm_mail_screen``, team mail from someone new does
not become a lead in the pull. A short background job (:func:`screen_new_sender`) asks the providers
first; "not a customer" leaves the mail stored, unlinked, with a Comment saying why, and no lead. Any
other answer (customer, no opinion, a failing provider) makes the lead as before. The sender is
checked again when the job runs: a mail that was linked or a lead that appeared meanwhile goes by the
normal rules and no provider is asked. A job that fails itself makes the lead anyway. **Make lead** on
the Communication form (:func:`make_lead`) files a mail the screening left without one.

"Skipped" means never stored: the Communication ``before_insert`` raises Frappe's own
``SentEmailInInboxError``, which ``EmailAccount.receive`` answers with a plain rollback (no Error
Log, no failed count). Frappe's IMAP "ALL" sync starts each pull after the highest uid it has stored
for the account, so a skipped mail would be fetched again on every pull, and 100 skipped in a row
would stall the account; :func:`_remember_skipped_uid` keeps one marker Communication per account
(no sender, subject or body) at the highest skipped uid. "UNSEEN" sync marks a mail read when it
fetches it, and POP3 deletes it, so neither fetches a skipped mail twice.

lazy: two accounts pulling the same mail in the same second can both keep it (no lock between the
check and the insert); upgrade path: ``GET_LOCK`` on the Message-ID.

Mail inserted outside the pull (no ``flags.in_receive``) is never refused: it is routed by the same
rules after insert, a "skip" leaving it unlinked.

Owner (:func:`claim_on_reply`): the first mail a Sales User or Sales Manager sends on a CRM Lead or
Deal without an owner makes them its owner, the way CRM's own assignment does (an assignment, which
CRM mirrors onto the owner field), made as the sender so no notice goes out. Everyone's assignment
stays, so anyone can take over later by changing the owner in CRM.

Pulling: Frappe pulls every Email Account every 10 minutes, which staff mailboxes keep; a team mailbox
is also queued for a pull every minute (:func:`pull_team_mailboxes`), so customer mail arrives within about a minute.
Mail sent on a CRM Lead or Deal leaves within seconds, not at Frappe's next flush (:func:`send_crm_mail_now`).
A new lead's display name also goes to the sender's Contact when Frappe made that Contact from the mail (:func:`_name_auto_contact`), so converting the lead to a deal keeps the name.
"""
from __future__ import annotations

import hashlib
import re
from functools import partial

import frappe
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.email.doctype.email_account.email_account import pull_from_email_account
from frappe.email.receive import SentEmailInInboxError
from frappe.integrations.doctype.connected_app.connected_app import has_token
from frappe.utils import cint, get_datetime, now, now_datetime, nowdate, parse_addr, split_emails
from frappe.utils.background_jobs import enqueue, get_jobs
from frappe.utils.synchronization import filelock

from ffl_core.crm import TITLE, installed, team
from ffl_core.crm.deals import open_deal_for
from ffl_core.utils import commit

SETTINGS = team.SETTINGS
LEAD = "CRM Lead"
AUTO_LEAD = team.AUTO_LEAD
SKIP = "skip"
OWNER_FIELD = {"CRM Lead": "lead_owner", "CRM Deal": "deal_owner"}
# The one Communication per Email Account that records the highest skipped uid (see module doc).
MARKER_ID = "ffl-crm-skipped-mail-marker"
# The description of the Closed ToDo that shares a record with the team (share_with_team).
SHARED_NOTE = "Team mail: shared with the team (CRM mail rules)"
MARKER_SUBJECT = "Skipped mail marker (CRM mail rules): the highest mail uid this account left out"
_SAVEPOINT = "ffl_crm_mail"
SCREEN_HOOK = "gunshop_crm_mail_screen"
SCREEN_BODY_LIMIT = 4000
NOT_A_CUSTOMER = "Not a customer inquiry"   # the Info Comment on a mail the screening left without a lead
# Machine mail. Communication keeps no headers (Auto-Submitted, List-Unsubscribe), so the
# address's local part is the signal there is.
_AUTOMATED_LOCAL_PARTS = frozenset({
	"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
	"mailer-daemon", "mailer_daemon", "postmaster", "bounce", "bounces",
})
_AUTOMATED_SUFFIXES = ("-noreply", "_noreply", "-no-reply", "_no-reply", ".noreply")


def domain_lines(value: str | None) -> list[str]:
	"""A settings field's domains: one per line, lowercase, without "@", no blanks or repeats."""
	out = []
	for line in (value or "").splitlines():
		d = line.strip().lower().lstrip("@").strip()
		if d and d not in out:
			out.append(d)
	return out


def _in(domain: str, domains: list[str]) -> bool:
	return any(domain == d or domain.endswith("." + d) for d in domains)


def is_automated(address: str) -> bool:
	local = address.split("@", 1)[0].strip().lower()
	local = re.sub(r"\+.*$", "", local)
	return local in _AUTOMATED_LOCAL_PARTS or local.endswith(_AUTOMATED_SUFFIXES)


def take_over_lead_flag(doc, method=None):
	"""Email Account validate: CRM's own lead switch becomes ours."""
	if doc.get("create_lead_from_incoming_email") and doc.meta.has_field(AUTO_LEAD):
		doc.set(AUTO_LEAD, 1)
		doc.create_lead_from_incoming_email = 0


def contacts_with_email(email: str) -> set[str]:
	"""The Contacts that hold this address, in the Email IDs table or as the Contact's own email_id."""
	contacts = set(frappe.get_all("Contact Email", filters={"email_id": email, "parenttype": "Contact"}, pluck="parent"))
	return contacts | set(frappe.get_all("Contact", filters={"email_id": email}, pluck="name"))


def _customer_contact(email: str) -> str | None:
	"""The (oldest) Contact with this email address that is linked to a Customer."""
	contacts = contacts_with_email(email)
	if not contacts:
		return None
	found = frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": ["in", list(contacts)],
		"link_doctype": "Customer"}, pluck="parent", order_by="creation asc", limit=1)
	return found[0] if found else None


def _mailbox_user(email_account: str) -> str | None:
	address = frappe.db.get_value("Email Account", email_account, "email_id")
	return address and frappe.db.get_value("User", {"email": address, "enabled": 1}, "name")


def _skipped(email: str) -> bool:
	domain = email.rsplit("@", 1)[-1].lower()
	settings = frappe.get_cached_doc(SETTINGS)
	return (_in(domain, domain_lines(settings.blocked_sender_domains)) or _in(domain, domain_lines(settings.own_domains))
		or is_automated(email))


def _addresses(doc) -> set[str]:
	out = set()
	for field in ("recipients", "cc", "bcc"):
		for part in split_emails(doc.get(field) or ""):
			address = parse_addr(part)[1]
			if address and "@" in address:
				out.add(address.strip().lower())
	return out


def _duplicate(doc) -> bool:
	"""The same mail is already in: one we sent, or received by another in-scope account. (The same
	account's own repeat is Frappe's: ``InboundMail.is_exist_in_system``.)"""
	message_id = doc.get("message_id")
	if not message_id:
		return False
	mine = {"name": ["!=", doc.name]} if doc.name else {}
	if frappe.db.exists("Communication", {"message_id": message_id, "sent_or_received": "Sent", **mine}):
		return True
	others = [a for a in team.in_scope_accounts() if a != doc.email_account]
	return bool(others) and bool(frappe.db.exists("Communication", {"message_id": message_id,
		"sent_or_received": "Received", "email_account": ["in", others], **mine}))


def _collected_elsewhere(row, account: str) -> bool:
	"""Another Email Account brings in this address's mail. A collector that no longer pulls mail does
	not count: its address's mail would then be lost everywhere."""
	return bool(row.collector) and row.collector != account \
		and bool(frappe.db.get_value("Email Account", row.collector, "enable_incoming"))


def _team_row(doc, account: str):
	"""(row, skip): the team address this mail counts for (None: direct mail), and whether another
	account collects it."""
	rows = team.rows()
	hit = [r for r in rows if r.address in _addresses(doc)]
	if hit:
		ours = [r for r in hit if r.collector == account]
		if ours:
			return ours[0], False
		open_rows = [r for r in hit if not _collected_elsewhere(r, account)]
		return (open_rows[0], False) if open_rows else (None, True)
	mine = [r for r in rows if r.collector == account]
	if mine:   # a collector: all its mail is team mail (a Bcc, a group that hides its address)
		own = (frappe.db.get_value("Email Account", account, "email_id") or "").lower()
		return next((r for r in mine if r.address == own), mine[0]), False
	return None, False


def decide(doc) -> tuple | None:
	"""What to do with a received mail: None (not ours to touch), ("skip", reason), ("keep", row) as
	Frappe linked it, ("deal", contact, owner, row) or ("lead", existing lead or None, row); *row* is
	the team address it counts for, None for direct mail."""
	if doc.get("communication_type") != "Communication" or doc.get("sent_or_received") != "Received":
		return None
	account = doc.get("email_account")
	if not account or not team.in_scope(account):
		return None
	if _duplicate(doc):
		return (SKIP, "already in")
	row, collected = _team_row(doc, account)
	if collected:
		return (SKIP, "collected by another account")
	if doc.get("reference_doctype"):
		return ("keep", row)
	name, email = parse_addr(doc.get("sender") or "")
	email = (email or "").strip().lower()
	if "@" not in email:
		return (SKIP, "no sender address")
	contact = _customer_contact(email)
	if contact:
		return ("deal", contact, None if row else _mailbox_user(account), row)
	if _skipped(email):
		return (SKIP, "blocked sender")
	lead = frappe.db.get_value(LEAD, {"email": email}, "name")
	if lead or row:
		return ("lead", lead, row)
	return (SKIP, "direct mail from someone new")


def _split_name(full_name: str | None) -> tuple[str, str]:
	"""A display name as (first name, last name): the first word, then the rest."""
	parts = (full_name or "").split()
	return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def _new_lead(email: str, full_name: str | None) -> str:
	lead = frappe.new_doc(LEAD)
	lead.email = email
	first, last = _split_name(full_name)
	named = bool(first) and (full_name or "").strip().lower() != email
	lead.first_name = first or email.split("@", 1)[0]
	lead.last_name = last
	if frappe.db.exists("CRM Lead Source", "Email"):
		lead.source = "Email"
	# No owner: a pool lead waits for its first reply, a round robin one for CRM's Assignment Rule.
	# The mail pull runs as the system, not as a CRM user; CRM's own lead creation inserts the same way.
	lead.insert(ignore_permissions=True)
	if named:
		_name_auto_contact(email, first, last)
	return lead.name


def _name_auto_contact(email: str, first: str, last: str) -> None:
	"""The sender's Contact that Frappe made from the mail (``get_contacts``, named after the address
	as "Jsmith42") takes the display name the lead has, so converting the lead to a deal (CRM copies
	the Contact's name onto the lead) keeps it. Only a Contact still as Frappe made it: its first name
	is the address's local part, no last name, linked to nothing (no Customer, no deal). Only the name
	fields change, never the document's name. Never stops the lead: a failure is logged."""
	frappe.db.savepoint("ffl_crm_contact")
	try:
		auto = frappe.unscrub(email.split("@", 1)[0])
		for name in sorted(contacts_with_email(email)):
			contact = frappe.db.get_value("Contact", name, ["first_name", "last_name"], as_dict=True)
			if not contact or contact.first_name != auto or contact.last_name \
					or frappe.db.exists("Dynamic Link", {"parenttype": "Contact", "parent": name}):
				continue
			frappe.db.set_value("Contact", name, {"first_name": first, "last_name": last,
				"full_name": f"{first} {last}".strip()}, update_modified=False)
			frappe.clear_document_cache("Contact", name)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_contact")
		frappe.log_error(title=f"[{TITLE}] Contact not named from the mail: {email}", message=frappe.get_traceback())


def _link(doc, doctype: str, name: str) -> None:
	doc.reference_doctype = doctype
	doc.reference_name = name
	doc.save(ignore_permissions=True)   # the system's own mail record, as CRM's handler saves it


def route(doc, decision=None, screened: bool = False) -> tuple[str, str] | None:
	"""Where an incoming mail goes: (doctype, name) it was linked to, or None. Team mail from someone new
	waits for :func:`screen_new_sender` when a screening provider is installed, unless *screened*."""
	if decision is None:
		decision = decide(doc)
	if not decision or decision[0] not in ("keep", "deal", "lead"):
		return None
	if decision[0] == "keep":
		target = (doc.reference_doctype, doc.reference_name)
		row = decision[1]
	elif decision[0] == "deal":
		target = ("CRM Deal", open_deal_for(decision[1], owner=decision[2]))
		row = decision[3]
		_link(doc, *target)
	else:
		lead, row = decision[1], decision[2]
		if not lead:
			if row and not screened and _queue_screening(doc):
				return None
			name, email = parse_addr(doc.get("sender") or "")
			lead = _new_lead(email.strip().lower(), doc.get("sender_full_name") or name)
		target = (LEAD, lead)
		_link(doc, *target)
	if row and target[0] in OWNER_FIELD:
		frappe.db.savepoint("ffl_crm_share")
		try:
			share_with_team(*target)
		except Exception:   # the mail stays where it went; the next team mail tries again
			frappe.db.rollback(save_point="ffl_crm_share")
			frappe.log_error(title=f"[{TITLE}] Team mail not shared: {target[0]} {target[1]}",
				message=frappe.get_traceback())
	return target if decision[0] != "keep" else None


def skip_unwanted(doc, method=None):
	"""Communication before_insert, during a mail pull only: refuse the mail :func:`decide` skips.
	A failure of the rules themselves keeps the mail (as before them): never lose a mail on our bug."""
	if not doc.flags.get("in_receive") or not installed():
		return
	try:
		decision = decide(doc)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Incoming mail rules failed", message=frappe.get_traceback())
		return
	doc.flags.ffl_crm_decision = decision
	if decision and decision[0] == SKIP:
		_remember_skipped_uid(doc)
		raise SentEmailInInboxError(decision[1])


def _remember_skipped_uid(doc) -> None:
	"""IMAP "ALL" sync: after the pull's rollback, raise the account's marker to this mail's uid."""
	uid = cint(doc.get("uid"))
	if uid <= 0:
		return
	account = frappe.db.get_value("Email Account", doc.email_account, ["use_imap", "email_sync_option"], as_dict=True)
	if account and account.use_imap and account.email_sync_option == "ALL":
		frappe.db.after_rollback.add(partial(_write_marker, doc.email_account, uid))


def _write_marker(email_account: str, uid: int) -> None:
	"""Runs right after ``EmailAccount.receive`` rolled the skipped mail back, in a transaction of its
	own: written and committed here (the next mail's commit may never come)."""
	try:
		found = frappe.db.get_value("Communication", {"email_account": email_account, "message_id": MARKER_ID},
			["name", "uid"], as_dict=True)
		if found:
			if cint(found.uid) < uid:
				frappe.db.set_value("Communication", found.name, "uid", uid, update_modified=False)
		else:
			# Row only: no hooks, no notification, nothing for CRM to show (no sender, no reference).
			frappe.get_doc({"doctype": "Communication", "communication_type": "Automated Message",
				"communication_medium": "Email", "sent_or_received": "Received", "email_account": email_account,
				"message_id": MARKER_ID, "uid": uid, "subject": MARKER_SUBJECT, "status": "Closed", "seen": 1,
				"communication_date": now()}).db_insert()
		commit()
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Skipped mail marker not written: {email_account}",
			message=frappe.get_traceback())


def on_incoming(doc, method=None):
	"""Communication after_insert. Never fails the mail pull: a failure is rolled back and logged."""
	if installed():
		_route_logged(doc, doc.flags.get("ffl_crm_decision"))


def _route_logged(doc, decision, screened: bool = False) -> None:
	frappe.db.savepoint(_SAVEPOINT)
	try:
		route(doc, decision, screened)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		doc.reference_doctype = doc.reference_name = None   # as stored again
		frappe.log_error(title=f"[{TITLE}] Incoming mail not routed: {doc.name}", message=frappe.get_traceback())


def _queue_screening(doc) -> bool:
	"""Whether the lead for this mail is left to :func:`screen_new_sender` (queued once the mail commits)."""
	if not frappe.get_hooks(SCREEN_HOOK):
		return False
	try:
		frappe.enqueue("ffl_core.crm.mail.screen_new_sender", queue="short", enqueue_after_commit=True,
			job_id=f"ffl_crm_mail_screen:{doc.name}", deduplicate=True, communication=doc.name)
	except Exception:   # no queue to wait on: no screening, the lead is made now
		frappe.log_error(title=f"[{TITLE}] Mail screening not queued: {doc.name}", message=frappe.get_traceback())
		return False
	return True


def screen_new_sender(communication: str) -> None:
	"""Background job: the lead for team mail from someone new, after the ``gunshop_crm_mail_screen``
	providers have judged it. Jobs for one sender run one at a time, so two mails from the same new
	sender make one lead, and the second is not screened. A failure of the job itself (the lock, the
	database) is logged and the lead is made anyway: the mail ends as a lead or as a "not a customer"
	note, never as nothing."""
	try:
		sender = frappe.db.get_value("Communication", communication, "sender")
		if not sender:
			return
		email = parse_addr(sender)[1].strip().lower()
		with filelock("ffl_crm_mail_screen_" + hashlib.md5(email.encode()).hexdigest(), timeout=120):
			commit()   # a fresh snapshot: what the job that held the lock before us wrote is visible
			_screen(communication)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Mail screening failed: {communication}", message=frappe.get_traceback())
		try:
			doc = frappe.get_doc("Communication", communication)
			_route_logged(doc, decide(doc), screened=True)
		except Exception:
			frappe.log_error(title=f"[{TITLE}] Screened mail not routed: {communication}", message=frappe.get_traceback())


def _screen(communication: str) -> None:
	doc = frappe.get_doc("Communication", communication)
	decision = decide(doc)
	if decision and decision[0] == "lead" and not decision[1]:
		reason = _not_a_customer(doc, decision[2])
		if reason:
			doc.add_comment("Info", reason)
			return
	# A lead or customer that appeared meanwhile, or a mail linked since: the normal rules, no provider.
	_route_logged(doc, decision, screened=True)


def screened_out(doc) -> bool:
	"""A received mail the screening judged "not a customer": unlinked, with that note on it."""
	return bool(doc.get("communication_type") == "Communication" and doc.get("sent_or_received") == "Received"
		and not doc.get("reference_doctype") and frappe.db.exists("Comment", {"reference_doctype": "Communication",
		"reference_name": doc.name, "comment_type": "Info", "content": ["like", NOT_A_CUSTOMER + "%"]}))


def mark_screened_out(doc, method=None):
	"""Communication onload: tells the form to show **Make lead**."""
	if installed() and screened_out(doc):
		doc.set_onload("ffl_screened_out", 1)


def make_lead(communication: str) -> tuple[str, str]:
	"""A mail the screening left without a lead gets one, by the normal rules (a customer's mail goes on
	their deal): (doctype, name) it was linked to. Refused for any other mail."""
	doc = frappe.get_doc("Communication", communication)
	# No Communication permission check: Sales Users have none (CRM reads mail through its own API), and
	# a screened-out mail is team mail by construction. The caller's CRM role is the endpoint's gate.
	if not screened_out(doc):
		frappe.throw(_("Only a mail that was checked and judged not to be from a customer, and has no lead yet, can be turned into a lead."),
			title=_("Not a screened mail"))
	target = route(doc, decide(doc), screened=True)
	if not target:
		frappe.throw(_("The mail rules would not file this mail anywhere now (a blocked or automated sender)."),
			title=_("No lead made"))
	return target


def _not_a_customer(doc, row) -> str | None:
	"""The first provider's "not a customer" answer as a sentence; None when none says so."""
	name, email = parse_addr(doc.get("sender") or "")
	mail = {"communication": doc.name, "sender": email.strip().lower(), "sender_name": doc.get("sender_full_name") or name,
		"to": sorted(_addresses(doc)), "subject": doc.get("subject") or "",
		"body": html_to_plain_text(doc.get("content") or "")[:SCREEN_BODY_LIMIT], "team_address": row.address}
	for hook in frappe.get_hooks(SCREEN_HOOK):
		try:
			answer = frappe.get_attr(hook)(dict(mail))
		except Exception:
			frappe.log_error(title=f"{SCREEN_HOOK} provider failed: {hook}")
			continue
		if isinstance(answer, dict) and answer.get("customer") is False:
			return f"{NOT_A_CUSTOMER} ({answer.get('category') or 'other'}): {answer.get('reason') or 'no reason given'}"
	return None


def _assign_quietly(doctype: str, name: str, user: str) -> None:
	"""A CRM assignment of *user* to the record, made as *user*: Frappe's assignment notice and CRM's
	both skip a self-assignment, so nobody is notified or emailed."""
	from frappe.desk.form.assign_to import _add as assign

	args = {"assign_to": [user], "doctype": doctype, "name": name, "assigned_by": user}
	prev = frappe.session.user
	if prev == user:   # the sender's own request: switching would reset its session
		assign(args, ignore_permissions=True)
		return
	frappe.set_user(user)
	try:
		assign(args, ignore_permissions=True)
	finally:
		frappe.set_user(prev)


def share_with_team(doctype: str, name: str) -> list[str]:
	"""Team mail on a CRM Lead / Deal: every staff user (Sales User / Sales Manager) can read it.

	Frappe CRM lets a Sales User read a lead or deal they own or hold a ToDo on that is not Cancelled
	(``crm.permissions.org_hierarchy``). Each staff user gets one *Closed* ToDo on the record, written
	as a row (no hooks): an Open one would also make Frappe email every later inbound mail on the
	record to them (``Communication.mail_cc`` copies open assignees), list them as assignees, post an
	"assigned" line per person on the timeline, notify them, and make the last one the owner (CRM's
	ToDo hook). The owner and the record's own assignment stay as they were. Returns the users added.

	lazy: re-checked on every team mail, so a user hired later gets an existing team lead on its next
	team mail, not before; upgrade path: a backfill over open team leads on User insert."""
	have = set(frappe.get_all("ToDo", filters={"reference_type": doctype, "reference_name": name,
		"status": ["!=", "Cancelled"]}, pluck="allocated_to"))
	missing = [u for u in team.staff_users() if u not in have]
	for user in missing:
		frappe.get_doc({"doctype": "ToDo", "status": "Closed", "allocated_to": user, "assigned_by": "Administrator",
			"reference_type": doctype, "reference_name": name, "date": nowdate(),
			"description": SHARED_NOTE}).db_insert()
	return missing


def share_converted_deal(doc, method=None):
	"""CRM Deal after_insert: a deal made from a lead the team shares is shared too. CRM's conversion
	copies only the lead's open assignees, and moves the lead's mail onto the deal. Never fails the
	conversion: a failure is rolled back and logged."""
	if not doc.get("lead") or not installed():
		return
	if not frappe.db.exists("ToDo", {"reference_type": LEAD, "reference_name": doc.lead, "status": "Closed",
			"description": SHARED_NOTE}):
		return
	frappe.db.savepoint("ffl_crm_share")
	try:
		share_with_team("CRM Deal", doc.name)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_share")
		frappe.log_error(title=f"[{TITLE}] Converted deal not shared: {doc.name}", message=frappe.get_traceback())


def _crm_staff(user: str | None) -> bool:
	if not user or user in ("Administrator", "Guest") or not frappe.db.get_value("User", user, "enabled"):
		return False
	return bool(set(frappe.get_roles(user)) & set(team.STAFF_ROLES))


def claim_on_reply(doc, method=None):
	"""Communication after_insert: the first mail sent on an owner-less CRM Lead / Deal makes its sender
	the owner. Never fails the send: a failure is rolled back and logged."""
	if not installed() or doc.get("communication_type") != "Communication" \
			or doc.get("sent_or_received") != "Sent" or doc.get("communication_medium") != "Email":
		return
	doctype, name = doc.get("reference_doctype"), doc.get("reference_name")
	field = OWNER_FIELD.get(doctype)
	if not field or not name or not _crm_staff(doc.owner) or frappe.db.get_value(doctype, name, field):
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if not frappe.db.exists("ToDo", {"reference_type": doctype, "reference_name": name,
				"allocated_to": doc.owner, "status": "Open"}):
			# CRM's ToDo after_insert makes the assignee the owner, as when a manager assigns in CRM.
			_assign_quietly(doctype, name, doc.owner)
		if not frappe.db.get_value(doctype, name, field):
			frappe.db.set_value(doctype, name, field, doc.owner, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Owner not set from a reply: {doctype} {name}", message=frappe.get_traceback())


def pull_team_mailboxes():
	"""Cron, every minute: queue a pull of each team mailbox (collector), the way Frappe's own
	``pull()`` does for every Email Account every 10 minutes. The same job name keeps a pull still
	queued or running from being queued twice; an account that failed to connect is left to Frappe's pull."""
	if not installed():
		return
	names = team.collectors()
	if not names:
		return
	accounts = frappe.get_all("Email Account", filters={"name": ["in", sorted(names)], "enable_incoming": 1,
		"awaiting_password": 0}, fields=["name", "auth_method", "backend_app_flow", "connected_app", "connected_user"])
	if not accounts:
		return
	queued = get_jobs(site=frappe.local.site, key="job_name")[frappe.local.site]
	for account in accounts:
		if account.auth_method == "OAuth" and not account.backend_app_flow \
				and not has_token(account.connected_app, account.connected_user):
			continue   # nothing to sign in with yet, as in Frappe's pull()
		job_name = f"pull_from_email_account|{account.name}"
		if job_name in queued:
			continue
		# Frappe disables an account after 5 failed connects within 2 hours (cleared by a good connect):
		# every minute that is minutes of outage, so a failing account is left to Frappe's 10-minute pull.
		if frappe.get_doc("Email Account", account.name).get_failed_attempts_count():
			continue
		enqueue(pull_from_email_account, "short", event="all", job_name=job_name, email_account=account.name)


def send_crm_mail_now(doc, method=None):
	"""Email Queue after_insert: mail sent on a CRM Lead or Deal is sent by a short job right after the
	request commits, not at Frappe's next flush (every few minutes). Scheduled mail (``send_after``)
	and every other mail keep Frappe's batching. Never fails the send: if the job cannot be queued,
	the row stays Not Sent and the flush sends it."""
	try:
		if not installed() or not doc.get("communication") or doc.get("send_after"):
			return
		comm = frappe.db.get_value("Communication", doc.communication, ["sent_or_received", "reference_doctype"], as_dict=True)
		if not comm or comm.sent_or_received != "Sent" or comm.reference_doctype not in OWNER_FIELD:
			return
		enqueue(send_queued_crm_mail, "short", enqueue_after_commit=True, job_id=f"send_crm_mail|{doc.name}",
			deduplicate=True, name=doc.name)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] CRM mail not queued for sending: {doc.get('name')}", message=frappe.get_traceback())


def send_queued_crm_mail(name: str) -> None:
	"""The job of :func:`send_crm_mail_now`: sends the Email Queue row as Frappe's flush does (the row
	locked, then ``send()``, which keeps the status and the retries). A row that is gone, already sent,
	being sent by the flush, or scheduled for later is left alone."""
	if not frappe.db.exists("Email Queue", name):
		return
	queue = frappe.get_doc("Email Queue", name, for_update=True)   # a flush racing this waits, then sees "Sending"
	if queue.send_after and get_datetime(queue.send_after) > now_datetime():
		return
	queue.send()
