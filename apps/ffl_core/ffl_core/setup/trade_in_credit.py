"""Idempotent creation of the trade-in credit item + invoice-row link field.

The money side of a trade-in. firearm_intake books the GUN (Item + FFL
Acquisition + Material Receipt); the TRADE-IN-CREDIT line books the
CONSIDERATION — a locked negative line on the buyer's POS invoice, so revenue
nets down by the allowance and the drawer reconciles.

Sales tax: the trade-in line is TAXED like goods — it carries NO exempt Item
Tax Template (setup/tax._unwire_trade_in_credit strips any), so its negative
amount reduces the invoice's taxable base and the gun is taxed on the
DIFFERENCE (price net of trade-in); an even trade computes $0 tax. Per TX Tax
Code §151.007(c)(3), the "sales price" excludes the value of property taken in
trade. This REVERSES
the earlier full-price treatment and restores the original
design. (The item's group is otherwise irrelevant to tax — with no item-level
template the line follows the invoice's Sales Taxes and Charges template.)

Mirrors setup/transfer_fee's ensure-pattern: called lazily from the API path
and from hooks.after_migrate — deliberately NOT after_install, where a fresh
site doesn't have the UOM master yet and the Item insert dies on link
validation (the transfer-fee item hit the same landmine). Like tax.ensure, the
migrate path must never raise: a thrown hook aborts the whole `bench migrate`.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.cash_drawer import PAYOUT_METHODS as TRADE_IN_PAYOUT_METHODS
from ffl_core.utils import commit

TRADE_IN_CREDIT_ITEM = "TRADE-IN-CREDIT"
TRADE_IN_STOCK_ENTRY_FIELD = "ffl_trade_in_stock_entry"
TRADE_IN_SELLER_FIELD = "ffl_trade_in_customer"

# Taxable leaf group from the canonical taxonomy (setup/item_groups.py) —
# see module docstring for why this must NOT be "Service".
_CREDIT_ITEM_GROUP = "Misc Accessory"

# The row-level link that makes a credit line auditable and lockable: the
# compliance gate (compliance/trade_in_credit.py) verifies rate == -acq.cost
# and blocks spending the same acquisition twice. read_only so the cashier
# can't repoint it; no_copy=0 so POS→Sales Invoice consolidation carries it.
_ROW_FIELDS = {
	dt: [
		{
			"fieldname": "ffl_trade_in_acquisition",
			"fieldtype": "Link",
			"options": "FFL Acquisition",
			"label": "Trade-in Acquisition",
			"read_only": 1,
			"insert_after": "item_name",
		},
		# A trade-in of accessories / ammo (no serial, no bound book): the credit line locks to the DRAFT Material
		# Receipt holding them instead, submitted at checkout like a gun's acquisition (trade_in._park_accessories).
		{
			"fieldname": TRADE_IN_STOCK_ENTRY_FIELD,
			"fieldtype": "Link",
			"options": "Stock Entry",
			"label": "Trade-in Receipt",
			"read_only": 1,
			"insert_after": "ffl_trade_in_acquisition",
		},
	]
	for dt in ("POS Invoice Item", "Sales Invoice Item")
}

# Marks a Material Receipt as accessories / ammo taken in trade, and from whom: the credit gate accepts only a
# Stock Entry carrying it.
_STOCK_ENTRY_FIELDS = {
	"Stock Entry": [{
		"fieldname": TRADE_IN_SELLER_FIELD, "fieldtype": "Link", "options": "Customer",
		"label": "Trade-in From", "read_only": 1, "no_copy": 1, "insert_after": "stock_entry_type",
	}],
}


# A ticket whose trade-ins exceed the goods goes negative and the store pays the customer the difference
# (compliance/trade_in_credit.negative_trade_in_ok). How it was paid, and the Cash Drawer Entry that booked it.
# no_copy on all three: a return / amended copy must never carry the original payout.
# The methods are a seller payout's: TRADE_IN_PAYOUT_METHODS is cash_drawer.PAYOUT_METHODS under the contract's name.
_HEADER_FIELDS = {
	dt: [
		{
			"fieldname": "ffl_trade_in_payout_method", "fieldtype": "Select", "label": "Paid to Customer By",
			"options": "\n" + "\n".join(TRADE_IN_PAYOUT_METHODS), "insert_after": "payments", "no_copy": 1,
			"description": "Only when trade-ins exceed the sale: how the store paid the customer the difference.",
		},
		{
			"fieldname": "ffl_trade_in_payout_reference", "fieldtype": "Data", "label": "Payout Reference",
			"insert_after": "ffl_trade_in_payout_method", "no_copy": 1,
			"description": "Check number / Zelle confirmation / ACH reference (required unless paid in cash).",
		},
		{
			"fieldname": "ffl_trade_in_payout_entry", "fieldtype": "Link", "options": "Cash Drawer Entry",
			"label": "Payout Entry", "read_only": 1, "no_copy": 1, "insert_after": "ffl_trade_in_payout_reference",
		},
	]
	for dt in ("POS Invoice", "Sales Invoice")
}


# The rate a trade-in took off a tax row (compliance/negative_trade_in): set when the row is floored to 0, cleared when
# the rate comes back. Only a row carrying it is ever given a rate back: a 0% row someone else set stays 0%.
# Copied with the row (an amended ticket can still restore it); returns and merged copies are never settled.
FLOORED_RATE_FIELD = "ffl_trade_in_floored_rate"
_TAX_ROW_FIELDS = {
	"Sales Taxes and Charges": [{
		"fieldname": FLOORED_RATE_FIELD, "fieldtype": "Float", "label": "Rate Before Trade-in Floor",
		"insert_after": "rate", "read_only": 1, "hidden": 1,
		"description": "Trade-ins took this ticket's taxable amount below zero, so this row's rate was set to 0.",
	}],
}


def credit_income_account(company: str) -> str | None:
	"""Where a TRADE-IN-CREDIT line books: the company's Stock
	Adjustment account, the one the trade-in's Material Receipt credits, so the gun's receipt and the credit net to
	zero and a ticket below zero never shows negative revenue. None when the company has no such account."""
	from ffl_core.api.cost_correction import _stock_adjustment_accounts

	muted, frappe.flags.mute_messages = frappe.flags.mute_messages, True  # "no account" is an answer here
	try:
		return _stock_adjustment_accounts(company)[0]
	except frappe.ValidationError:
		return None
	finally:
		frappe.flags.mute_messages = muted


def _ensure_credit_income_accounts() -> int:
	"""Point the credit item's Item Default income account at Stock Adjustment for every company. Only the
	item's default changes: lines already on invoices keep the account they were booked to. Returns how many
	companies changed (0 at steady state).

	Written row by row, not through Item.save: saving fills a new row's default warehouse from the site default,
	which belongs to one company only, and the Item then refuses the row of any other company."""
	rows = {r.company: r for r in frappe.get_all(
		"Item Default", filters={"parent": TRADE_IN_CREDIT_ITEM, "parenttype": "Item"},
		fields=["name", "company", "income_account", "idx"])}
	last = max((r.idx for r in rows.values()), default=0)
	changed = 0
	for company in frappe.get_all("Company", pluck="name"):
		account = credit_income_account(company)
		row = rows.get(company)
		if not account or (row and row.income_account == account):
			continue
		if row:
			frappe.db.set_value("Item Default", row.name, "income_account", account, update_modified=False)
		else:
			last += 1
			frappe.get_doc({
				"doctype": "Item Default", "name": frappe.generate_hash(length=10), "parent": TRADE_IN_CREDIT_ITEM,
				"parenttype": "Item", "parentfield": "item_defaults", "idx": last, "company": company,
				"income_account": account,
			}).db_insert()
		changed += 1
	if changed:
		frappe.clear_document_cache("Item", TRADE_IN_CREDIT_ITEM)  # ERPNext reads item defaults off the cached Item
	return changed


def ensure() -> dict:
	"""Create the TRADE-IN-CREDIT item and invoice-row fields, and book the item to Stock Adjustment. Idempotent.
	Never raises (runs in after_migrate): failures are logged and skipped —
	the lazy call on the next trade-in retries anyway."""
	try:
		create_custom_fields(_ROW_FIELDS, ignore_validate=True)
		create_custom_fields(_HEADER_FIELDS, ignore_validate=True)
		create_custom_fields(_TAX_ROW_FIELDS, ignore_validate=True)
		create_custom_fields(_STOCK_ENTRY_FIELDS, ignore_validate=True)
		created = 0
		if not frappe.db.exists("Item", TRADE_IN_CREDIT_ITEM):
			if not frappe.db.exists("Item Group", _CREDIT_ITEM_GROUP):
				from ffl_core.setup.item_groups import ensure as ensure_item_groups
				ensure_item_groups()
			frappe.get_doc({
				"doctype": "Item",
				"item_code": TRADE_IN_CREDIT_ITEM,
				"item_name": "Trade-in Credit",
				"item_group": _CREDIT_ITEM_GROUP,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"has_serial_no": 0,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"is_firearm": 0,
				"description": "Trade-in allowance credited against a sale. Always a negative "
					"line locked to its FFL Acquisition — added via the POS Trade-in dialog.",
			}).insert(ignore_permissions=True)
			created = 1
		# Make sure NO 0% exemption template is attached: the credit line must
		# participate in the tax base so a trade-in reduces the taxable amount
		# (see module docstring). Runs on every call, not just creation, so a
		# site that ran the old wiring self-heals on the next migrate /
		# trade-in; tax.ensure runs earlier in after_migrate, so the exempt
		# templates it strips already exist by the time this looks them up.
		from ffl_core.setup.tax import _unwire_trade_in_credit
		_unwire_trade_in_credit()
		# Lock field + Item Price tombstones (setup/locked_lines): the credit
		# item must never carry a live price ERPNext could re-price rows from.
		from ffl_core.setup import locked_lines
		locked_lines.ensure()
		defaults_set = _ensure_credit_income_accounts()
		commit()
		return {"created": created, "defaults_set": defaults_set}
	except Exception:
		frappe.log_error(
			title="trade_in_credit.ensure failed",
			message=frappe.get_traceback(),
		)
		return {"created": 0, "error": 1}
