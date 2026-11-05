"""Team addresses: the shared addresses customers write to (sales@, info@), and the Email Account
setup that makes staff answer as the team.

Lead Mail Settings › Team addresses lists them. Each row is a ``Mailbox`` (a real mailbox with its
own Email Account) or a ``Group`` (a group or alias with no mailbox of its own), optionally with the
Email Account that collects its mail. An Email Account that collects some address is a *collector*;
any other account with ``Email Account.ffl_auto_lead`` on is a *staff* (personal) mailbox. The mode
is derived from the table, not stored on the account, so an account can never be marked a collector
without an address it collects (or the other way round).

Incoming rules (which copy of a mail is kept, what becomes a lead) are in ``crm.mail``. This module
also keeps two Email Account settings in step with the table, on every save of the settings, at
migrate, and (Reply-To only) when an Email Account is saved:

* Reply-To: every staff mailbox sends with Reply-To = the settings' primary team address
  (``add_reply_to_header`` on, ``reply_to_addresses`` that one address). Collectors are never
  touched: a mailbox-type team address answers as itself. With no primary address set, nothing is
  written.
* User Emails: every enabled Sales User / Sales Manager gets the Email Account of each mailbox-type
  team address in their User Emails, which is what lets Frappe CRM's mail editor pick it as From
  when they answer mail sent to that address (and shows them its mail).

Manual, per store (docs/guide/manager/settings.md): creating the Email Accounts and the Connected
App, the sync option (IMAP, "ALL": "UNSEEN" marks every mail it reads as read in the staff member's
own mailbox), turning ``ffl_auto_lead`` on per staff mailbox, the CRM Assignment Rule for a round
robin address, and removing a User Emails row from someone who leaves (rows are only ever added).
"""
from __future__ import annotations

import frappe

from ffl_core.crm import TITLE

SETTINGS = "Lead Mail Settings"
AUTO_LEAD = "ffl_auto_lead"
GROUP, MAILBOX = "Group", "Mailbox"
POOL, ROUND_ROBIN = "Pool", "Round robin"
REPLY_TO = "Reply To Address"
# Who works CRM mail: these users answer as a mailbox-type team address (crm.mail owner rule too).
STAFF_ROLES = ("Sales User", "Sales Manager")


def rows() -> list:
	"""The team address rows (addresses lowercase, as the settings save them)."""
	if not frappe.get_meta(SETTINGS).has_field("team_addresses"):
		return []   # code ahead of the migrate that adds the table
	return list(frappe.get_cached_doc(SETTINGS).get("team_addresses") or [])


def primary() -> str:
	if not frappe.get_meta(SETTINGS).has_field("primary_team_address"):
		return ""
	return frappe.get_cached_doc(SETTINGS).get("primary_team_address") or ""


def collectors() -> set[str]:
	return {r.collector for r in rows() if r.collector}


def in_scope(account: str) -> bool:
	"""Whether CRM mail rules apply to this Email Account: its CRM switch is on, or it collects a team
	address. Mail of every other account (a store's noreply, a jobs inbox) is never touched."""
	if not frappe.get_meta("Email Account").has_field(AUTO_LEAD):
		return False   # CRM installed, its migrate not run yet
	return account in collectors() or bool(frappe.db.get_value("Email Account", account, AUTO_LEAD))


def in_scope_accounts() -> list[str]:
	if not frappe.get_meta("Email Account").has_field(AUTO_LEAD):
		return []
	return sorted(set(frappe.get_all("Email Account", filters={AUTO_LEAD: 1}, pluck="name")) | collectors())


def _staff_mailboxes() -> list[str]:
	if not frappe.get_meta("Email Account").has_field(AUTO_LEAD):
		return []
	skip = collectors()
	return [n for n in frappe.get_all("Email Account", filters={AUTO_LEAD: 1}, pluck="name", order_by="name asc")
		if n not in skip]


# Reply-To -----------------------------------------------------------------------------------------

def reply_to_on_validate(doc, method=None):
	"""Email Account validate: a staff mailbox gets the primary team address as Reply-To on its own save
	(a mailbox added after the settings were saved). In memory: the account's own save writes it."""
	address = primary()
	if not address or not doc.get(AUTO_LEAD) or doc.name in collectors():
		return
	if [r.email for r in doc.get("reply_to_addresses") or []] != [address]:
		doc.set("reply_to_addresses", [{"email": address}])
	doc.add_reply_to_header = 1


def sync_reply_to() -> list[str]:
	"""Every staff mailbox's Reply-To = the primary team address. Written row by row, not by saving the
	Email Account: its save connects to the mail server. Returns the accounts it changed."""
	address = primary()
	if not address:
		return []
	changed = []
	for account in _staff_mailboxes():
		current = frappe.get_all(REPLY_TO, filters={"parenttype": "Email Account", "parent": account,
			"parentfield": "reply_to_addresses"}, pluck="email", order_by="idx asc")
		header = frappe.db.get_value("Email Account", account, "add_reply_to_header")
		if current == [address] and header:
			continue
		frappe.db.delete(REPLY_TO, {"parenttype": "Email Account", "parent": account, "parentfield": "reply_to_addresses"})
		frappe.get_doc({"doctype": REPLY_TO, "parenttype": "Email Account", "parent": account,
			"parentfield": "reply_to_addresses", "idx": 1, "email": address}).db_insert()
		frappe.db.set_value("Email Account", account, "add_reply_to_header", 1, update_modified=False)
		frappe.clear_document_cache("Email Account", account)
		changed.append(account)
	return changed


# User Emails --------------------------------------------------------------------------------------

def staff_users() -> list[str]:
	users = set(frappe.get_all("Has Role", filters={"parenttype": "User", "role": ["in", list(STAFF_ROLES)]},
		pluck="parent"))
	users -= {"Administrator", "Guest"}
	if not users:
		return []
	return frappe.get_all("User", filters={"name": ["in", sorted(users)], "enabled": 1, "user_type": "System User"},
		pluck="name", order_by="name asc")


def sync_user_emails() -> list[tuple[str, str]]:
	"""Each mailbox-type team address's Email Account in every staff user's User Emails. Added the way
	Frappe adds a user's own mailbox (``setup_user_email_inbox``); never removed. Returns (user, account)
	pairs it added.

	lazy: runs on a settings save and at migrate only, so a user hired later gets the mailbox at the
	next of either, and someone who leaves keeps the row until removed by hand; upgrade path: a User
	on_update hook that inserts / deletes the one row."""
	accounts = [r.collector for r in rows() if r.address_type == MAILBOX and r.collector
		and frappe.db.exists("Email Account", r.collector)]
	if not accounts:
		return []
	added = []
	users = staff_users()
	for account in dict.fromkeys(accounts):
		info = frappe.db.get_value("Email Account", account,
			["email_id", "awaiting_password", "enable_outgoing", "auth_method"], as_dict=True)
		have = set(frappe.get_all("User Email", filters={"parenttype": "User", "email_account": account}, pluck="parent"))
		for user in users:
			if user in have:
				continue
			frappe.db.savepoint("ffl_crm_user_email")
			try:
				doc = frappe.get_doc("User", user)
				doc.append("user_emails", {"email_account": account, "email_id": info.email_id,
					"awaiting_password": info.awaiting_password or 0, "enable_outgoing": info.enable_outgoing or 0,
					"used_oauth": 1 if info.auth_method == "OAuth" else 0})
				doc.save(ignore_permissions=True)   # as Frappe's own setup_user_email_inbox saves it
				added.append((user, account))
			except Exception:
				frappe.db.rollback(save_point="ffl_crm_user_email")
				frappe.log_error(title=f"[{TITLE}] Team mailbox not added to {user}", message=frappe.get_traceback())
	return added


def apply() -> dict:
	"""Settings saved / migrate: Reply-To and User Emails in step with the team addresses. Idempotent."""
	return {"reply_to": sync_reply_to(), "user_emails": sync_user_emails()}
