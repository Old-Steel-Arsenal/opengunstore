"""Idempotent setup for consignment: schema + Consignment Payable account +
default commission %.

Commission `c%` is the store's cut (its profit); the consignor is owed
`P·(1-c%)`, where `P` is the line net (pre-tax) price. Store-wide default is
DEFAULT_COMMISSION_PCT (FFL Settings.consignment_default_commission_pct,
adjustable); an individual consigned Serial No can override it via its own
consignment_commission_pct.

Creates:
  - Serial No: is_consignment (Check), consignment_commission_pct (Percent),
	consignor (Link -> Supplier) — flags a gun as consigned, its commission
	rate, and who it's owed to.
  - POS Invoice Item / Sales Invoice Item: consignment_commission_pct
	(Percent) — the commission rate locked onto the sale line at sale time,
	so later payable-booking logic doesn't have to chase the Serial No.
  - FFL Settings: consignment_default_commission_pct (Percent) — the
	store-wide default, seeded to DEFAULT_COMMISSION_PCT.
  - a "Consignment Payable" LIABILITY account per company: money owed to
	consignors is a liability until paid out, mirroring
	setup/customer_deposits.py's Customer Deposits account.
  - Journal Entry: ffl_consignment_source (Data, read-only) — the source
	POS/Sales Invoice name, stamped by compliance/consignment.post_consignment_payable
 so a later reversal can find the JE to cancel.

Mirrors setup/trade_in_credit.py / setup/customer_deposits.py's ensure-pattern:
lazy + after_migrate, NEVER raises (a thrown hook aborts the whole
`bench migrate`) — failures are logged and skipped.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt

from ffl_core.setup.customer_deposits import _liability_parent
from ffl_core.utils import commit

CONSIGNMENT_PAYABLE_TITLE = "Consignment Payable"
DEFAULT_COMMISSION_PCT = 20.0


def default_commission_pct() -> float:
	"""The store-wide consignment commission %: FFL Settings' value, or
	DEFAULT_COMMISSION_PCT when it is blank/zero. Intake stamping and the
	Serial No form backfill both read it here so they never disagree."""
	return flt(frappe.db.get_single_value("FFL Settings", "consignment_default_commission_pct")) or DEFAULT_COMMISSION_PCT

_SERIAL_FIELDS = {
	"Serial No": [
		{
			"fieldname": "is_consignment",
			"fieldtype": "Check",
			"label": "Consignment",
			"insert_after": "needs_service",
			# Editable (0): staff can flag an in-stock gun missed at intake directly
			# on the form. validate_consignment_serial (compliance/consignment.py)
			# then requires a consignor + defaults the commission %. Explicit 0 so
			# create_custom_fields flips the previously read-only field on migrate.
			"read_only": 0,
		},
		{
			"fieldname": "consignment_commission_pct",
			"fieldtype": "Percent",
			"label": "Consignment Commission %",
			"insert_after": "is_consignment",
			"depends_on": "is_consignment",
		},
		{
			"fieldname": "consignor",
			"fieldtype": "Link",
			"options": "Supplier",
			"label": "Consignor",
			"insert_after": "consignment_commission_pct",
			"depends_on": "is_consignment",
			# Editable (0) alongside is_consignment so staff can name the consignor
			# when flagging a gun on the form; required by validate_consignment_serial.
			"read_only": 0,
		},
	]
}
_LINE_FIELDS = {
	dt: [
		{
			"fieldname": "consignment_commission_pct",
			"fieldtype": "Percent",
			"label": "Consignment Commission %",
			"insert_after": "warehouse",
		}
	]
	for dt in ("POS Invoice Item", "Sales Invoice Item")
}
_PR_ITEM_FIELDS = {
	"Purchase Receipt Item": [
		{
			"fieldname": "ffl_consignment_commission_pct",
			"fieldtype": "Percent",
			"label": "Consignment Commission %",
			# Per-line commission entered on Receive Goods when Source=Consignment;
			# create_acquisitions_for_firearm_lines (compliance/purchase.py) stamps
			# it onto each serial at intake (blank/0 → the store default). ffl_-
			# prefixed like the sibling intake fields (ffl_sell_price/ffl_dealer_price).
			"insert_after": "ffl_dealer_price",
		}
	]
}
_SETTINGS = {
	"FFL Settings": [
		{
			"fieldname": "consignment_default_commission_pct",
			"fieldtype": "Percent",
			"label": "Default Consignment Commission %",
			"insert_after": "receive_sources",
			"default": str(DEFAULT_COMMISSION_PCT),
		}
	]
}
_JOURNAL_ENTRY_FIELDS = {
	"Journal Entry": [
		{
			# Stamped by compliance/consignment.post_consignment_payable with
			# the source POS/Sales Invoice name, so a later reversal can find
			# — and cancel — the JE this invoice posted without scanning user_remark.
			"fieldname": "ffl_consignment_source",
			"fieldtype": "Data",
			"label": "Consignment Source Invoice",
			"read_only": 1,
			"insert_after": "user_remark",
		}
	]
}


def payable_account(company: str) -> str | None:
	"""The company's Consignment Payable liability account, if created."""
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_name": CONSIGNMENT_PAYABLE_TITLE, "is_group": 0},
		"name",
	)


def ensure() -> dict:
	"""Create the consignment fields + Consignment Payable account. Idempotent;
	never raises (runs in after_migrate — failures are logged, the lazy call
	on the next consignment intake retries)."""
	try:
		create_custom_fields(
			{**_SERIAL_FIELDS, **_LINE_FIELDS, **_PR_ITEM_FIELDS, **_SETTINGS,
				**_JOURNAL_ENTRY_FIELDS},
			ignore_validate=True,
		)
		for company in frappe.get_all("Company", pluck="name"):
			_ensure_payable_account(company)
		commit()
		return {"ok": 1}
	except Exception:
		frappe.log_error(title="consignment.ensure failed", message=frappe.get_traceback())
		return {"error": 1}


def _ensure_payable_account(company: str) -> str | None:
	existing = frappe.db.get_value(
		"Account",
		{"company": company, "account_name": CONSIGNMENT_PAYABLE_TITLE, "is_group": 0},
	)
	if existing:
		return existing
	parent = _liability_parent(company)
	if not parent:
		return None  # chartless company (mid-setup) — lazy retry later
	return frappe.get_doc({
		"doctype": "Account",
		"account_name": CONSIGNMENT_PAYABLE_TITLE,
		"parent_account": parent,
		"company": company,
		"root_type": "Liability",
		"is_group": 0,
		"account_type": "Payable",
	}).insert(ignore_permissions=True).name
