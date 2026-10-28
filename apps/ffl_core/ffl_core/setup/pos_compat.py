"""Compatibility shims for ERPNext POS quirks at the pinned version."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.roles import COUNTER_ROLES

# Every flag a permission row carries; the drift check compares rows on these.
_PERM_FLAGS = ("select", "read", "write", "create", "delete", "submit", "cancel", "amend",
	"report", "export", "import", "share", "print", "email", "mask")

# Durable site state (tabDefaultValue, frappe.db.get_global): no schema, survives Error Log
# retention, rolled back with the transaction.
_GRANTED_KEY = "ffl_pos_profile_select_granted"  # roles the grant below has dealt with, once each
# (doctype, role, permissions): ensure_cashier_pos_profile_select's grant, one row per counter role.
CASHIER_GRANTS = tuple(("POS Profile", role, ("select",)) for role in COUNTER_ROLES)
# drift digests already reported, every doctype check_perm_drift covers (the name predates all but POS Profile)
_DRIFT_SEEN_KEY = "ffl_pos_profile_perm_drift_seen"
_DRIFT_TITLE = "{doctype} permissions differ from ERPNext"


def _get_list(key: str) -> list:
	import json

	try:
		return json.loads(frappe.db.get_global(key) or "[]")
	except ValueError:
		return []


def _set_list(key: str, values: list) -> None:
	import json

	frappe.db.set_global(key, json.dumps(values))


def perm_rows(table: str, parent: str = "POS Profile") -> list[tuple]:
	"""``parent``'s rules in ``table`` (DocPerm or Custom DocPerm), one tuple per row:
	(role, permlevel, if_owner, flags in _PERM_FLAGS order). A list, not a dict, so a
	duplicated row is still two rows."""
	from frappe.utils import cint

	return [(r.role, cint(r.permlevel), cint(r.if_owner), tuple(cint(r.get(f)) for f in _PERM_FLAGS))
		for r in frappe.get_all(table, filters={"parent": parent}, fields=["*"])]


def ensure_cashier_pos_profile_select() -> None:
	"""Give every counter role (roles.COUNTER_ROLES) `select` on POS Profile, once.

	ERPNext v16.50 gates the register on the POS Profile: get_stock_availability
	(which POS Invoice.validate_stock_availablility also calls in-process, as the
	session user, for every stock line on submit), get_items, get_pos_profile_data,
	get_parent_item_group, item_group_query, create_opening_voucher, the barcode
	search and the closing form's get_cashiers each demand select-or-read on it.
	Stock ERPNext grants that to Accounts Manager / Accounts User (read) and Sales
	Manager (select) only, so a counter role without them (Sales User, Stock Manager,
	System Manager) could no longer submit a sale. `select` is the grant upstream
	gave Sales Manager for the same checks: it passes them without opening the
	profile's form or list (its accounts and write-off settings). Accounts User would
	pass too, but it brings read on the books.

	A role is left alone when a level-0 rule in force already gives it select or
	read, and also when it carries an owner-only rule: that is a deliberate
	restriction someone set, and a plain row next to it would widen it.

	Once per role: a role dealt with here (granted, or found already covered) is
	recorded in _GRANTED_KEY and never looked at again, so a rule an administrator
	deletes afterwards stays deleted. Delete that key to run the grant again.

	Custom DocPerm is frappe's own route (the Role Permissions Manager writes the
	same rows). Its catch: the first custom row copies POS Profile's stock rows into
	Custom DocPerm, and from then on an ERPNext change to those rows no longer
	reaches the site (check_perm_drift reports it; docs/testing.md) -- so
	nothing is written while the rows in force already grant it.
	"""
	handled = _get_list(_GRANTED_KEY)
	for doctype, role, ptypes in CASHIER_GRANTS:
		if role in handled or not frappe.db.exists("Role", role):
			continue
		handled.append(role)
		# Custom DocPerm rows, once a doctype has any, replace its DocPerm rows wholesale.
		in_force = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
		rows = frappe.get_all(in_force, filters={"parent": doctype, "role": role, "permlevel": 0},
			fields=["if_owner", "select", "read"])
		if any(r.select or r.read for r in rows if not r.if_owner) or any(r.if_owner for r in rows):
			continue
		# a rule without select or read gains select; with none, a select-only row is made
		_grant(doctype, role, ptypes, existing=bool(rows))
	_set_list(_GRANTED_KEY, handled)


def _grant(doctype: str, role: str, ptypes: tuple[str, ...], existing: bool) -> None:
	"""Give ``role`` ``ptypes`` on ``doctype`` at level 0, through Role Permissions Manager's own Custom DocPerm
	(setup_custom_perms copies the stock rows first, so no other role's rule is lost). ``existing``: the role's
	level-0 rule gains the permissions and keeps the rest. Otherwise a new row is made carrying exactly
	``ptypes``: add_permission's row defaults read and export to 1, so every flag is set in the same single
	save (a select grant must not open the form and list). Each save clears the doctype's
	cache (Custom DocPerm.on_update)."""
	from frappe.core.doctype.custom_docperm.custom_docperm import update_custom_docperm
	from frappe.permissions import add_permission, setup_custom_perms

	if existing:
		setup_custom_perms(doctype)
		name = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0})
		update_custom_docperm(name, dict.fromkeys(ptypes, 1))
	else:
		name = add_permission(doctype, role, 0, ptypes[0])
		update_custom_docperm(name, {flag: int(flag in ptypes) for flag in _PERM_FLAGS})


_ROLE_GRANTS_KEY = "ffl_role_grants_done"  # "doctype|role" of every grant below already dealt with
# (doctype, role, permissions, add_missing). User decisions 2026-10-08:
#   POS Invoice report for Sales User -- frappe.desk.query_report runs a report only for someone with
#     report permission on its ref doctype; both 4473 reports point at POS Invoice, where stock ERPNext
#     gives Sales User read only, so the counter could neither open them nor see their Overview block
#     or the Pending 4473 card (gun_shop_cards._may_open, the same rule).
#   FFL Acquisition read + report for Sales User / Sales Manager -- the Transfer pickups report. The
#     doctype JSON grants it; this only matters on a site whose FFL Acquisition rules are Custom DocPerm.
#   Serial and Batch Bundle for Sales User -- the POS Trade-in is a counter job (
#     the counter here is Sales User; roles.TRADE_IN_ROLES). Its stock receipt (a pay-now buy's
#     Purchase Receipt, a credit trade-in's Material Receipt at checkout) makes the gun's Serial and Batch
#     Bundle, an ERPNext-internal write the framework checks against the SESSION user whatever flags the
#     document carries; stock ERPNext gives Sales User select only there. The bundle gets what Stock User
#     has, short of cancel and delete. Sales User already has the rest the receipt checks (Item read, Account
#     select) from stock ERPNext; Sales Manager is given nothing. (The new Item's selling price needs no grant: firearm_intake._create_item makes no
#     Item Price, per-gun priced firearms carry none -- item_hooks.sync_standard_selling_price.)
# add_missing: what to make of a site with Custom DocPerm on the doctype but no level-0 row for the
# role. False where the stock rules give the role a row (POS Invoice): someone removed it, leave it.
# True where the stock row is new with this grant (FFL Acquisition): the custom copy predates it.
# Serial and Batch Bundle is False: ERPNext 16.50 ships Sales User a select-only row
# there, so on a site with Custom DocPerm a missing row means an administrator removed it, and this module's
# rule for that is to leave it. check_perm_drift reports the missing shipped row, so it is not silent.
# A row this makes carries exactly the permissions listed (_grant); a row that exists is only ever added to.
ROLE_GRANTS = (
	("POS Invoice", "Sales User", ("report",), False),
	("FFL Acquisition", "Sales User", ("read", "report"), True),
	("FFL Acquisition", "Sales Manager", ("read", "report"), True),
	("Serial and Batch Bundle", "Sales User", ("read", "write", "create", "submit"), False),
)


_REGISTER_GRANTS_KEY = "ffl_register_grants_done"  # same record as _ROLE_GRANTS_KEY, for REGISTER_GRANTS
# What a counter login holding only Sales User (+ Stock User), or only Sales Manager, needs to run the register
# end to end. ERPNext v16 gives Sales User read on POS Invoice and nothing on POS Opening Entry, POS Closing
# Entry or Sales Invoice; Sales Manager gets no write on POS Invoice and no Item, Account, POS Settings or Stock
# Settings access. Every real login used to hold Accounts User as well, so the gap never showed. Each row is the
# least a register step needs (test_counter_register_perms walks the loop and fails without any one of them);
# none gives cancel, amend, Journal Entry, Payment Entry or the ledger.
#   POS Invoice write create submit  -- checkout saves, then submits, the sale (and a return)
#   POS Invoice delete               -- Recent Orders > Delete Order on a DRAFT; Frappe never deletes a submitted
#                                       document, and cancel stays closed. It would also delete a CANCELLED
#                                       invoice, so guard_cancelled_delete (on_trash) keeps that to managers.
#                                       The recycle bin mirrors this right.
#                                       (The receipt needs no grant: printview takes read OR print.)
#   POS Opening Entry read write create submit -- the Create POS Opening Entry dialog; write too, because
#                                       submitting a closing re-saves the opening to mark it Closed
#   POS Closing Entry read write create submit -- Close the POS
#   Sales Invoice read write create submit -- read: Recent Orders lists Sales Invoices beside POS Invoices
#                                       (get_past_order_list errors without it). create / write / submit:
#                                       consolidating the sales (the closing's job, and the immediate merge of
#                                       a firearm sale) creates and submits the Sales Invoice as the SESSION
#                                       user, and ERPNext does not lift the permission check there. There is
#                                       no narrower grant: Custom DocPerm cannot say "only from a merge".
#   POS Profile read                 -- the opening dialog fills its payment rows with frappe.client.get and the
#                                       item grid waits on get_value("selling_price_list"); the select
#                                       CASHIER_GRANTS gave only passes the server-side checks
# Not given, on purpose: what Sales User already has from ERPNext (Item, Account, POS Settings, Stock Settings,
# Serial and Batch Bundle) is not copied to Sales Manager, which would turn those doctypes into Custom DocPerm on
# every site. A Sales Manager runs the register as Sales Manager + Sales User, as for the trade-in
# (roles.TRADE_IN_ROLES); the role profiles always pair them.
# No if_owner: the register is one shared shift (api/pos_session), so another clerk's opening and sales are
# read and closed here. Sales Manager already has the full POS Opening / Closing Entry rows. add_missing is
# False where the stock rules give the role a row, True where they give it none.
REGISTER_GRANTS = (
	("POS Invoice", "Sales User", ("write", "create", "delete", "submit"), False),
	("POS Invoice", "Sales Manager", ("write", "create", "delete", "submit"), False),
	("POS Opening Entry", "Sales User", ("read", "write", "create", "submit"), True),
	("POS Closing Entry", "Sales User", ("read", "write", "create", "submit"), True),
	("POS Profile", "Sales User", ("read",), False),
	("POS Profile", "Sales Manager", ("read",), False),
	("Sales Invoice", "Sales User", ("read", "write", "create", "submit"), True),
	("Sales Invoice", "Sales Manager", ("read", "write", "create", "submit"), True),
)


def ensure_role_grants() -> None:
	"""after_install / after_migrate: the permissions in ROLE_GRANTS and REGISTER_GRANTS, each dealt with once.

	Same route and caveats as ensure_cashier_pos_profile_select above, through the same writer (_grant):
	Role Permissions Manager's own Custom DocPerm, the stock rows copied first so no other role's rule is
	lost. Nothing is written while the rules in force already grant it -- on a site without Custom
	DocPerm for FFL Acquisition, its JSON does. A role carrying an owner-only rule is left alone, and so
	is a removed row (see add_missing). A row made here carries the listed permissions and nothing else;
	an existing row only gains them. Once per (doctype, role): a rule an administrator removes after
	the grant stays removed. Once a doctype has Custom DocPerm rows, ERPNext's later changes to its
	stock rules no longer reach the site; check_perm_drift reports them.

	REGISTER_GRANTS has its own record: POS Invoice / Sales User was dealt with (for report) before it existed.
	"""
	_apply_grants(ROLE_GRANTS, _ROLE_GRANTS_KEY)
	_apply_grants(REGISTER_GRANTS, _REGISTER_GRANTS_KEY)


def guard_cancelled_delete(doc, method: str | None = None) -> None:
	"""POS Invoice on_trash: a cancelled invoice (docstatus 2) is deleted by a manager only.

	REGISTER_GRANTS gives the counter delete so a clerk can drop a draft (Delete Order); Frappe itself refuses
	only a submitted invoice, so the same right would let a clerk erase a cancelled one. Drafts stay open."""
	if doc.docstatus == 2 and not {"Sales Manager", "System Manager"} & set(frappe.get_roles()):
		frappe.throw(_("Only a Sales Manager or System Manager can delete a cancelled POS Invoice."),
			frappe.PermissionError)


def _apply_grants(grants: tuple, record_key: str) -> None:
	handled = _get_list(record_key)
	for doctype, role, ptypes, add_missing in grants:
		key = f"{doctype}|{role}"
		if key in handled or not frappe.db.exists("Role", role) or not frappe.db.exists("DocType", doctype):
			continue
		handled.append(key)
		custom = bool(frappe.db.exists("Custom DocPerm", {"parent": doctype}))
		rows = frappe.get_all("Custom DocPerm" if custom else "DocPerm", fields=["if_owner", *ptypes],
			filters={"parent": doctype, "role": role, "permlevel": 0})
		if any(r.if_owner for r in rows) or any(all(r.get(p) for p in ptypes) for r in rows):
			continue
		if not rows and custom and not add_missing:
			continue
		_grant(doctype, role, ptypes, existing=bool(rows))
	_set_list(record_key, handled)


# The doctypes check_perm_drift compares: the ones this module turns into Custom DocPerm for a real need (the
# counter's POS Profile select, the trade-in's stock bundle, the register's loop). FFL Acquisition grants predate it.
_DRIFT_DOCTYPES = ("POS Profile", "Serial and Batch Bundle", "POS Invoice", "POS Opening Entry",
	"POS Closing Entry", "Sales Invoice")


def _our_grants() -> dict[tuple[str, str], set[str]]:
	"""{(doctype, role): permissions} this module adds, read off CASHIER_GRANTS, ROLE_GRANTS and REGISTER_GRANTS."""
	ours: dict[tuple[str, str], set[str]] = {}
	for doctype, role, ptypes, *_add_missing in CASHIER_GRANTS + ROLE_GRANTS + REGISTER_GRANTS:
		ours.setdefault((doctype, role), set()).update(ptypes)
	return ours


def check_perm_drift() -> None:
	"""after_migrate: report rules that no longer match ERPNext's shipped ones, on each of _DRIFT_DOCTYPES.

	Once a doctype carries Custom DocPerm rows (ensure_cashier_pos_profile_select and
	ensure_role_grants make them), ERPNext's own rule changes stop reaching the site. Per
	doctype, this compares the two tables as multisets (a duplicated row counts twice),
	leaving out our own grants (_our_grants: a row we made, or our permissions added to
	ERPNext's row), and reports each distinct difference once: an Error Log plus a ToDo
	for the System Managers. A newer difference closes the older ToDo on the same doctype,
	so it notifies them again. Its digest is kept in _DRIFT_SEEN_KEY, which an Error Log
	purge does not touch, so a difference someone has reviewed (closing the ToDo is the
	acknowledgement) is never reported again.

	It reports instead of re-syncing on purpose: a difference is just as often a rule
	someone changed in the Role Permissions Manager, and nothing on the site tells the
	two apart (Permission Log skips migrate, install and patch writes), so a re-sync
	could silently widen access to a doctype that holds the store's accounts, or undo
	a restriction. ERPNext ships its own mirror patches for customised doctypes
	(16.50's mirror_select/release_perms_to_custom_docperm), so this is the backstop.
	"""
	import hashlib
	from collections import Counter

	from ffl_core.alerts import alert_assignees, alert_once

	ours = _our_grants()
	seen = _get_list(_DRIFT_SEEN_KEY)
	for doctype in _DRIFT_DOCTYPES:
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype}):
			continue
		shipped, site = perm_rows("DocPerm", doctype), perm_rows("Custom DocPerm", doctype)
		compared = []
		for row in site:
			role, level, owner, flags = row
			granted = ours.get((doctype, role))
			if granted and (level, owner) == (0, 0):
				stock = [s for s in shipped if s[:3] == row[:3]]
				have = {f for f, v in zip(_PERM_FLAGS, flags) if v}
				if not stock and have <= granted:
					continue  # a row we made, on a role ERPNext ships nothing for (a grant not made yet is no drift)
				if len(stock) == 1:
					shipped_flags = {f for f, v in zip(_PERM_FLAGS, stock[0][3]) if v}
					if have >= shipped_flags and have - shipped_flags <= granted:
						row = stock[0]  # our permissions, added to ERPNext's own rule
			compared.append(row)

		def describe(row, n):
			role, level, owner, flags = row
			granted = ", ".join(f for f, v in zip(_PERM_FLAGS, flags) if v) or "nothing"
			return f"{role}, level {level}{' (owner only)' if owner else ''}: {granted}{f' (x{n})' if n > 1 else ''}"

		missing = Counter(shipped) - Counter(compared)
		extra = Counter(compared) - Counter(shipped)
		diffs = sorted([f"ERPNext ships {describe(r, n)}; this site lacks it" for r, n in missing.items()]
			+ [f"this site has {describe(r, n)}; ERPNext ships no such rule" for r, n in extra.items()])
		if not diffs:
			continue
		detail = "\n".join(diffs)
		# POS Profile's digest stays the detail's alone, as before the other doctypes were covered, so a
		# difference already reviewed there is not reported again; another doctype's carries its name.
		digest = hashlib.sha1((detail if doctype == "POS Profile" else f"{doctype}\n{detail}").encode()).hexdigest()[:12]
		if digest in seen:
			continue
		base = _DRIFT_TITLE.format(doctype=doctype)
		title = f"{base} ({digest})"
		# superseded: its ToDo is closed so the new one is a new assignment, and notifies
		for todo in frappe.get_all("ToDo", pluck="name", filters={"reference_type": "DocType",
				"reference_name": doctype, "status": "Open", "description": ("like", f"%{base} (%")}):
			frappe.db.set_value("ToDo", todo, "status", "Cancelled")
		alert_once(
			"DocType", doctype, title=title,
			message=(f"{doctype}'s rules on this site are frozen Custom DocPerm rows, so ERPNext's own "
				f"rules no longer apply. They differ here:\n{detail}\n\nIf a difference was not made on "
				"purpose, apply ERPNext's rule in Role Permissions Manager. Close the ToDo once reviewed: "
				"this difference is not reported again."),
			description=f"{title}: see the Error Log of the same title.",
			assignees=[u for u in alert_assignees() if "System Manager" in frappe.get_roles(u)],
			dedupe_on_title=True,
		)
		seen.append(digest)
		# lazy: the last 50 digests are kept; an older one recurring is reported again
		_set_list(_DRIFT_SEEN_KEY, seen[-50:])


def ensure_pos_invoice_mode() -> None:
	"""Pin POS Settings.invoice_type to "POS Invoice".

	ERPNext v16 defaults fresh sites to "Sales Invoice" mode, which makes
	creating any POS Invoice throw ("Sales Invoice mode is activated in
	POS…"). This entire app is built on POS Invoice — the 4473/NICS gate,
	the transfer-in builder, trade-in credits, immediate consolidation — so
	Sales Invoice mode is not a supported configuration. Idempotent.
	"""
	current = frappe.db.get_single_value("POS Settings", "invoice_type")
	if current == "POS Invoice":
		return
	frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")


def ensure_pos_invoice_compat_fields() -> None:
	"""Mirror Sales Invoice's `is_created_using_pos` onto POS Invoice.

	ERPNext 16.17.0's shared `update_multi_mode_option` (sales_invoice.py) reads
	`doc.is_created_using_pos`, but that field exists only on Sales Invoice — not
	POS Invoice. So `reset_mode_of_payments` (fired when the POS recalls a Draft
	via Recent Orders → Edit) raises AttributeError, breaking the resume flow.

	Add the field to POS Invoice (Check, default 0, hidden) so the attribute
	resolves; it only drives a cosmetic "payment methods refreshed" toast, so the
	default value is behaviourally neutral. Idempotent. Remove if a later ERPNext
	adds the field to POS Invoice natively.
	"""
	create_custom_fields(
		{
			"POS Invoice": [
				{
					"fieldname": "is_created_using_pos",
					"fieldtype": "Check",
					"label": "Is created using POS",
					"default": "0",
					"hidden": 1,
					"print_hide": 1,
					"read_only": 1,
					"insert_after": "is_pos",
				}
			]
		},
		ignore_validate=True,
	)
