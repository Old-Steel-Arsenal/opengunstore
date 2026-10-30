"""Native shipping charge: custom field + dedicated income account.

Shipping is a service, not a sold item. The Create Order page records an
operator-entered amount on the ffl_shipping_amount custom field (the source of
truth); ffl_core.compliance.shipping_charge materializes it into a Sales Taxes
and Charges "Actual" row on validate. This module seeds the field on Sales
Order + Sales Invoice (mirrors setup/buyer_type.py) and a per-company
"Shipping Income" account head for the charge to post to.

Wired into hooks.after_migrate; back-filled by patches/seed_shipping_charge.py.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SHIPPING_ACCOUNT_NAME = "Shipping Income"

_FIELD = {
	"fieldname": "ffl_shipping_amount",
	"fieldtype": "Currency",
	"label": "Shipping Amount",
	# Right column of Taxes and Charges, under Incoterm / Named Place: the left column already
	# holds the tax fields, so anchoring there left Incoterm alone on the right.
	"insert_after": "named_place",
	"read_only": 1,
	"print_hide": 1,
	"description": "Charged on this order as the Shipping row in Sales Taxes and Charges.",
}
# The freight tax basis, stamped at creation from FFL Settings and copied with
# the document (amend / return / SO → SI), so an order keeps the basis it was
# sold under whatever the switch says later.
_INCLUSIVE_FIELD = {
	"fieldname": "ffl_shipping_tax_inclusive",
	"fieldtype": "Check",
	"label": "Shipping Price Includes Sales Tax",
	"insert_after": "ffl_shipping_amount",
	"read_only": 1,
	"print_hide": 1,
	"description": "The Shipping Amount already includes its sales tax.",
}
_FIELDS = {dt: [dict(_FIELD), dict(_INCLUSIVE_FIELD)] for dt in ("Sales Order", "Sales Invoice")}


def ensure() -> None:
	"""Create the ffl_shipping_amount + ffl_shipping_tax_inclusive fields on
	SO+SI and the per-company Shipping Income account. Idempotent."""
	try:
		create_custom_fields(_FIELDS, ignore_validate=True)
	except Exception:
		frappe.log_error(
			title="ffl_core.setup.shipping_charge: field ensure failed",
			message=frappe.get_traceback())
	ensure_shipping_tax_switch()
	for company in frappe.get_all("Company", pluck="name"):
		try:
			ensure_shipping_account(company)
		except Exception:
			frappe.log_error(
				title="ffl_core.setup.shipping_charge: account ensure failed",
				message=frappe.get_traceback())


def ensure_shipping_tax_switch() -> None:
	"""Store FFL Settings' "Shipping Price Includes Sales Tax" ON default when
	the site has no row for it yet (fresh install — patches don't run there —
	or a site that predates the switch), so the form shows what single_flag
	reads and a first Save can't silently write 0. A stated value is kept."""
	from ffl_core.utils import single_raw
	if single_raw("FFL Settings", "shipping_price_includes_tax") is None:
		frappe.db.set_single_value("FFL Settings", "shipping_price_includes_tax", 1)


def shipping_account_name(company: str) -> str:
	"""The shipping income account's name, derived from the company abbr — no DB
	write/existence check. Cheap enough for the validate hot path (every SO/SI);
	call ensure_shipping_account before actually posting to it."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	return f"{SHIPPING_ACCOUNT_NAME} - {abbr}"


def ensure_shipping_account(company: str) -> str:
	"""The income account head for the shipping charge row, under the company's
	Indirect Income group (else its root Income group). Idempotent; returns the
	account name."""
	name = shipping_account_name(company)
	if frappe.db.exists("Account", name):
		return name
	acct = frappe.get_doc({
		"doctype": "Account",
		"account_name": SHIPPING_ACCOUNT_NAME,
		"company": company,
		"parent_account": _income_parent(
			company, frappe.get_cached_value("Company", company, "abbr")),
		"is_group": 0,
	})
	acct.flags.ignore_permissions = True
	acct.insert()
	return acct.name


def _income_parent(company: str, abbr: str) -> str:
	for candidate in (f"Indirect Income - {abbr}", f"Income - {abbr}"):
		if frappe.db.exists("Account", {"name": candidate, "company": company, "is_group": 1}):
			return candidate
	grp = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Income", "is_group": 1},
		"name", order_by="lft")
	if not grp:
		frappe.throw(_("No Income parent account for {0}").format(company))
	return grp
