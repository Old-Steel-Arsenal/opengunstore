"""Idempotent setup for the "Customer Deposits" LIABILITY account, one per company.

Money taken against goods not yet handed over is owed to the customer, not
earned income. The account is looked up by name elsewhere (the GunBroker
cash-basis report), so every company keeps one.

Same lifecycle rules as the other setup modules (see the CI fresh-install
landmines memory): after_migrate, NEVER after_install (the account insert needs
a chart a fresh site doesn't have yet), and the migrate path never raises.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit

DEPOSIT_ACCOUNT_TITLE = "Customer Deposits"


def deposit_account(company: str, title: str = DEPOSIT_ACCOUNT_TITLE) -> str | None:
	"""The company's Customer Deposits liability account (or the one named *title*), if created."""
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_name": title, "is_group": 0},
		"name",
	)


def ensure() -> dict:
	"""Create the account on every company. Idempotent; never raises (runs in
	after_migrate — failures are logged, the next migrate retries)."""
	created = {"accounts": 0}
	try:
		for company in frappe.get_all("Company", pluck="name"):
			if _ensure_account(company):
				created["accounts"] += 1
		commit()
	except Exception:
		frappe.log_error(
			title="customer_deposits.ensure failed",
			message=frappe.get_traceback(),
		)
	return created


def _ensure_account(company: str, title: str = DEPOSIT_ACCOUNT_TITLE) -> bool:
	existing = deposit_account(company, title)
	if existing:
		_heal_parent(company, existing)
		return False
	parent = _liability_parent(company)
	if not parent:
		return False  # chartless company (mid-setup) — the next migrate retries
	frappe.get_doc({
		"doctype": "Account",
		"account_name": title,
		"company": company,
		"parent_account": parent,
		"root_type": "Liability",
		"is_group": 0,
	}).insert(ignore_permissions=True)
	return True


def _heal_parent(company: str, account: str) -> None:
	"""Move an account created under the wrong liability group — but only while it
	has never been posted to. A balance sheet that files it as long-term debt is
	the reason; an account with history is the accountant's to move,
	not a migration's."""
	parent = frappe.db.get_value("Account", account, "parent_account")
	want = _liability_parent(company)
	if not want or parent == want or not str(parent or "").startswith("Non-Current"):
		return
	if frappe.db.exists("GL Entry", {"account": account, "is_cancelled": 0}):
		return
	doc = frappe.get_doc("Account", account)
	doc.parent_account = want
	doc.flags.ignore_permissions = True
	doc.save()


def _liability_parent(company: str) -> str | None:
	"""Where the deposit account hangs: CURRENT liabilities.

	A customer deposit is settled when the goods are handed over — weeks, not
	years — so it belongs with the current liabilities. Taking the
	first liability group in tree order instead put it under Non-Current
	Liabilities on a standard chart, where the balance sheet reads it as
	long-term debt."""
	current = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Liability", "is_group": 1,
			"account_name": ("like", "Current Liabilit%")},
		"name",
		order_by="lft",
	)
	return current or frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Liability", "is_group": 1},
		"name",
		order_by="lft",
	)
