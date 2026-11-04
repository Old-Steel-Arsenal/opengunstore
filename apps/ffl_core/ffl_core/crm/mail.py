"""Incoming staff mail: a customer's mail goes on the customer, an unknown sender becomes a lead.

Each employee's own mailbox is one Email Account. CRM's own "Create Lead from Incoming Emails"
would make a lead for anyone, a known customer included; :func:`take_over_lead_flag` moves that
switch to ours (``Email Account.ffl_auto_lead``) so CRM's never runs. On an account with ours on,
a received mail that is not linked to anything yet:

1. from an email address of a Contact linked to a Customer: linked to that contact's open deal
   (made when there is none, owned by the mailbox's user; ``crm.deals``), no lead — CRM
   shows and answers mail only on a lead or a deal;
2. from a blocked domain, one of our own domains, or an automated sender (noreply and the
   like): nothing;
3. from the address of an existing CRM Lead: linked to that lead;
4. otherwise: a new CRM Lead (name from the sender, source "Email" when the store has it), and
   the mail linked to it.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import parse_addr

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import open_deal_for

SETTINGS = "Lead Mail Settings"
LEAD = "CRM Lead"
AUTO_LEAD = "ffl_auto_lead"
_SAVEPOINT = "ffl_crm_mail"
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


def _customer_contact(email: str) -> str | None:
	"""The (oldest) Contact with this email address that is linked to a Customer."""
	contacts = set(frappe.get_all("Contact Email", filters={"email_id": email, "parenttype": "Contact"}, pluck="parent"))
	contacts |= set(frappe.get_all("Contact", filters={"email_id": email}, pluck="name"))
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


def _new_lead(email: str, full_name: str | None) -> str:
	lead = frappe.new_doc(LEAD)
	lead.email = email
	parts = (full_name or "").split()
	lead.first_name = parts[0] if parts else email.split("@", 1)[0]
	lead.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
	if frappe.db.exists("CRM Lead Source", "Email"):
		lead.source = "Email"
	# The mail pull runs as the system, not as a CRM user; CRM's own lead creation inserts the same way.
	lead.insert(ignore_permissions=True)
	return lead.name


def _link(doc, doctype: str, name: str) -> None:
	doc.reference_doctype = doctype
	doc.reference_name = name
	doc.save(ignore_permissions=True)   # the system's own mail record, as CRM's handler saves it


def route(doc) -> tuple[str, str] | None:
	"""Where an incoming mail goes: (doctype, name) it was linked to, or None."""
	if doc.get("communication_type") != "Communication" or doc.get("sent_or_received") != "Received":
		return None
	if doc.get("reference_doctype") or not doc.get("email_account"):
		return None
	if not frappe.get_meta("Email Account").has_field(AUTO_LEAD):
		return None   # CRM installed, its migrate not run yet: no switch is on
	if not frappe.db.get_value("Email Account", doc.email_account, AUTO_LEAD):
		return None
	name, email = parse_addr(doc.get("sender") or "")
	email = (email or "").strip().lower()
	if "@" not in email:
		return None
	contact = _customer_contact(email)
	if contact:
		deal = open_deal_for(contact, owner=_mailbox_user(doc.email_account))
		_link(doc, "CRM Deal", deal)
		return ("CRM Deal", deal)
	if _skipped(email):
		return None
	lead = frappe.db.get_value(LEAD, {"email": email}, "name") or _new_lead(email, doc.get("sender_full_name") or name)
	_link(doc, LEAD, lead)
	return (LEAD, lead)


def on_incoming(doc, method=None):
	"""Communication after_insert. Never fails the mail pull: a failure is rolled back and logged."""
	if not installed():
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		route(doc)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		doc.reference_doctype = doc.reference_name = None   # as stored again
		frappe.log_error(title=f"[{TITLE}] Incoming mail not routed: {doc.name}", message=frappe.get_traceback())
