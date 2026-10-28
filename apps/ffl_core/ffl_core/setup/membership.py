"""Idempotent setup for memberships (rules in ``ffl_core.membership``).

* **Accounts** (after_migrate, every company): ``Deferred Membership Revenue`` (a CURRENT
  liability: dues paid for months not yet served) and ``Membership Revenue`` (income). A plan's
  item books to them (``membership.sync_plan_item``); ERPNext's own deferred revenue moves the
  dues from the first to the second day by day.
* **Deferred accounting settings** are NOT touched here. A store's Accounts Settings are its
  own: they change only when the store saves its first active Membership Plan
  (:func:`apply_deferred_settings`), and then only "book by Days" and "process automatically",
  with a message naming what changed. ``book_deferred_entries_via_journal_entry`` is never
  touched at all.

Lifecycle like the other setup modules: after_migrate, never after_install (the accounts need
a chart a fresh site does not have yet), and the migrate path never raises.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.setup.customer_deposits import _liability_parent
from ffl_core.utils import commit

DEFERRED_ACCOUNT_TITLE = "Deferred Membership Revenue"
INCOME_ACCOUNT_TITLE = "Membership Revenue"
ACCOUNTS_SETTINGS = "Accounts Settings"
# What a membership needs from Accounts Settings: dues recognized per day of service, and the
# month-end processing switched on (ERPNext's own monthly job).
DEFERRED_SETTINGS = {
	"book_deferred_entries_based_on": "Days",
	"automatically_process_deferred_accounting_entry": 1,
}


def account(company: str, title: str) -> str | None:
	"""The company's leaf account named *title*, if created."""
	return frappe.db.get_value("Account", {"company": company, "account_name": title, "is_group": 0}, "name")


def accounts(company: str) -> tuple[str | None, str | None]:
	"""(deferred revenue account, income account) of *company*, creating them when missing."""
	_ensure_accounts(company)
	return account(company, DEFERRED_ACCOUNT_TITLE), account(company, INCOME_ACCOUNT_TITLE)


def ensure() -> dict:
	"""Create both accounts on every company. Idempotent; never raises (after_migrate)."""
	created = {"accounts": 0}
	try:
		for company in frappe.get_all("Company", pluck="name"):
			created["accounts"] += _ensure_accounts(company)
		commit()
	except Exception:
		frappe.log_error(title="membership setup failed", message=frappe.get_traceback())
		created["error"] = 1
	return created


def _ensure_accounts(company: str) -> int:
	made = 0
	for title, root_type, parent in (
			(DEFERRED_ACCOUNT_TITLE, "Liability", _liability_parent),
			(INCOME_ACCOUNT_TITLE, "Income", _income_parent)):
		if account(company, title):
			continue
		group = parent(company)
		if not group:
			continue   # chartless company (mid-setup): the next migrate retries
		frappe.get_doc({"doctype": "Account", "account_name": title, "company": company,
			"parent_account": group, "root_type": root_type, "is_group": 0}).insert(ignore_permissions=True)
		made += 1
	return made


def _income_parent(company: str) -> str | None:
	"""Direct Income on a standard chart, else the first income group."""
	return frappe.db.get_value("Account", {"company": company, "root_type": "Income", "is_group": 1,
		"account_name": ("like", "Direct Income%")}, "name", order_by="lft") \
		or frappe.db.get_value("Account", {"company": company, "root_type": "Income", "is_group": 1},
			"name", order_by="lft")


def apply_deferred_settings() -> list[str]:
	"""Set the deferred revenue settings a membership needs where they differ. Returns one
	line per change (empty when nothing changed). Never touches the journal-entry switch."""
	changed = []
	meta = frappe.get_meta(ACCOUNTS_SETTINGS)
	for field, want in DEFERRED_SETTINGS.items():
		have = frappe.db.get_single_value(ACCOUNTS_SETTINGS, field)
		if str(have if have is not None else "") == str(want):
			continue
		frappe.db.set_single_value(ACCOUNTS_SETTINGS, field, want)
		label = _(meta.get_label(field))
		changed.append(_("{0}: {1}").format(label, _("Yes") if want == 1 else _(str(want))))
	if changed:
		frappe.clear_cache(doctype=ACCOUNTS_SETTINGS)
	return changed
