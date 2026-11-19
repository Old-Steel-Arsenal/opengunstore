"""The company's default sales-tax template in the test fixtures, and the 0% service template synced from it.

``setup.tax`` zeroes only the accounts of the company's DEFAULT template on the "Non-Taxable Service" template, so
which template is the default decides whether service, deposit and pre-order payment lines are taxed. A module that
depends on that states its own default (inside its class transaction); a test that commits a different default puts
the previous one back before it ends (:func:`restore_default_template`), or every later module on the site sells under
a template whose accounts are no longer exempt.
"""
from __future__ import annotations

import frappe

TEMPLATE = "Sales Taxes and Charges Template"


def default_template(company: str) -> str | None:
	return frappe.db.get_value(TEMPLATE, {"company": company, "is_default": 1}, "name")


def make_default_template(company: str, name: str) -> str:
	"""Make the existing template *name* the company's default and sync the 0% template to it (setup.tax)."""
	assert frappe.db.exists(TEMPLATE, {"name": name, "company": company}), \
		f"no sales-tax template {name!r} on {company!r} to make the default"
	frappe.db.sql(f"UPDATE `tab{TEMPLATE}` SET is_default = 0 WHERE company = %s", company)
	frappe.db.set_value(TEMPLATE, name, "is_default", 1)
	from ffl_core.setup import tax
	tax.ensure()
	return name


def default_template_from_rows(company: str, title: str, rows: list[tuple[str, float]]) -> str:
	"""A template *title* charging *rows* (account, rate) on net total, made when missing, then made the default."""
	name = f"{title} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if not frappe.db.exists(TEMPLATE, name):
		frappe.get_doc({"doctype": TEMPLATE, "title": title, "company": company,
			"taxes": [{"charge_type": "On Net Total", "account_head": a, "rate": r, "description": a}
				for a, r in rows]}).insert(ignore_permissions=True)
	return make_default_template(company, name)


def restore_default_template(company: str, name: str | None) -> None:
	"""Put back *name* (what :func:`default_template` said before a test changed it) as the default, re-sync the
	0% template, and commit. For a test that committed its own default: call it after the test's own clean-up has
	committed, so this commit carries the restore and nothing else."""
	if name:
		make_default_template(company, name)
	else:
		frappe.db.sql(f"UPDATE `tab{TEMPLATE}` SET is_default = 0 WHERE company = %s", company)
	frappe.db.commit()
