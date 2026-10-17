"""Server-side pin for LOCKED invoice lines (POS Invoice + Sales Invoice
before_validate).

A TRADE-IN-CREDIT row is minus the acquisition's cost. It is not a price, but
ERPNext treats every row as priced: the client re-prices rows from Item Price /
pricing-rule responses whenever they land (transaction.js
_set_values_for_item_list, apply_rule_on_other_items) and the server derives a
margin or discount whenever rate != price_list_rate
(taxes_and_totals.calculate_item_rate). A past incident: a $180
credit saved as rate +180 / price_list_rate -869 / margin 1049.

This hook is the authority. It runs in before_validate — BEFORE ERPNext's
validate calls set_missing_values and calculate_taxes_and_totals (frappe
document.run_before_save_methods, then Document.hook composes the controller
method first and doc_events after) — so every total, tax and payment check
sees the locked amount, on draft saves from any client (POS, desk form, REST,
MCP) and on submit. It pins:

  rate              = the locked amount, at the row's rate precision
  price_list_rate   = 0   — calculate_item_rate strips margin/discount and keeps
                            rate when price_list_rate is 0; pricing rules only
                            move rate when price_list_rate is set
                            (accounts_controller.apply_pricing_rule_on_items);
                            and POS never merges a second same-value line into
                            this row (get_item_from_frm matches on
                            price_list_rate === rate)
  margin / discount = cleared
  ffl_locked_rate   = the locked amount — what the POS client re-pins the row
                      from, needing no read on FFL Acquisition / Sales Order
  income_account    = (credit lines) Stock Adjustment, the account the trade-in's
                      Material Receipt credits — never
                      the POS profile's Sales account ERPNext hands a register line

The submit gate (compliance/trade_in_credit) stays as the independent second
check.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.utils import flt

from ffl_core.setup.gunsmith import DEPOSIT_ITEM
from ffl_core.setup.locked_lines import LOCKED_RATE_FIELD
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

_CLEAR = {
	"price_list_rate": 0,
	"margin_type": None,
	"margin_rate_or_amount": 0,
	"discount_percentage": 0,
	"discount_amount": 0,
	# A locked line is not discountable: with a rule attached ERPNext re-derives
	# the row on every validate and, on a consolidated Sales Invoice's submit
	# pass, zeroes `rate` for rule-bearing rows (set_missing_item_details'
	# pricing_rule_removed branch) — the gate must never read that 0.
	"pricing_rules": None,
}


def rate_precision(doctype: str) -> int:
	"""Decimal places of the item row's `rate` (POS Invoice → POS Invoice Item)."""
	meta = frappe.get_meta(f"{doctype} Item")
	return get_field_precision(meta.get_field("rate")) or 2


def expected_rate(doc, row) -> float | None:
	"""The amount `row` is locked to, or None when the row is not a locked line
	(or its source is unreadable — the submit gate decides then)."""
	from ffl_core import gunsmith
	from ffl_core.compliance import trade_in_credit

	if getattr(row, "item_code", None) == TRADE_IN_CREDIT_ITEM:
		return trade_in_credit.expected_credit_rate(doc, row)
	if getattr(row, "item_code", None) == DEPOSIT_ITEM:
		return gunsmith.expected_deposit_rate(doc, row)
	return None


def row_rate(doc, row) -> float:
	"""The row's amount per unit as the books see it. On a consolidated Sales
	Invoice ERPNext keeps `net_rate`/`net_amount` from the POS rows and may
	zero `rate` on its submit pass (rule-bearing rows); everywhere else `rate`
	is the figure the cashier sees and the one every re-pricer rewrites."""
	if int(getattr(doc, "is_consolidated", 0) or 0) and getattr(row, "net_rate", None) is not None:
		return flt(row.net_rate)
	return flt(getattr(row, "rate", 0))


def locked_rate_on_row(row) -> float | None:
	"""The lock stamped on the row, or None. A locked amount is never 0 (a credit
	is minus a cost), so 0 — the column's NOT NULL default on rows that predate
	the field — means unlocked."""
	v = flt(getattr(row, LOCKED_RATE_FIELD, None))
	return v or None


def _set(row, field: str, value) -> None:
	row.set(field, value) if hasattr(row, "set") else setattr(row, field, value)


def pin_row(row, rate: float) -> bool:
	"""Set the row to exactly `rate` with no pricing residue. Returns whether
	anything changed."""
	changed = False
	want = {"rate": rate, LOCKED_RATE_FIELD: rate, **_CLEAR}
	for field, value in want.items():
		have = getattr(row, field, None)
		if field in ("margin_type", "pricing_rules"):
			differs = bool(have) != bool(value)
		else:
			differs = flt(have) != flt(value)
		if differs:
			_set(row, field, value)
			changed = True
	return changed


def refuse_header_discount(doc) -> None:
	"""No invoice-level discount on an invoice that carries a locked line. ERPNext
	spreads a header discount over every row's net amount, the negative locked
	line included (taxes_and_totals.apply_discount_amount), so the credit would
	book at less than its source — and consolidation copies that net rate into
	the Sales Invoice, where the submit gate refuses it. The rule is to
	discount the goods line instead. Returns mirror a sale that may
	predate the rule; consolidated copies carry no header discount (the merge
	zeroes it) — neither is checked."""
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return
	if not (flt(getattr(doc, "additional_discount_percentage", 0))
			or flt(getattr(doc, "discount_amount", 0))):
		return
	from ffl_core.setup.locked_lines import locked_items
	codes = locked_items()
	if any(getattr(r, "item_code", None) in codes for r in (getattr(doc, "items", None) or [])):
		frappe.throw(_(
			"This sale has a trade-in credit or gunsmith deposit line, so it can't take an "
			"invoice discount — typed, from a coupon or from a transaction pricing rule (it would "
			"shrink the credit). Remove it and put the discount on the item line instead; a pricing "
			"rule or coupon must exclude this sale's items."), title=_("Discount the item, not the invoice"))


def pin_locked_lines(doc, method: str | None = None) -> bool:
	"""before_validate for POS Invoice / Sales Invoice. Refuses an invoice-level
	discount next to a locked line; otherwise never throws: a row whose source
	cannot be read is left for the submit gate."""
	refuse_header_discount(doc)
	consolidated = int(getattr(doc, "is_consolidated", 0) or 0)
	changed = False
	credit_account = None
	for row in getattr(doc, "items", None) or []:
		rate = expected_rate(doc, row)
		if not rate:   # not locked, source unreadable, or nothing left to lock to: the gate decides
			continue
		if consolidated:
			# A copy of submitted POS rows that ERPNext does not re-total
			# (calculate_item_values returns early): moving `rate` would leave
			# rate x qty != the booked amount. Only drop the rule residue — on its
			# submit pass ERPNext zeroes `rate` on rule-bearing rows.
			if getattr(row, "pricing_rules", None):
				_set(row, "pricing_rules", None)
				changed = True
			continue
		changed = pin_row(row, rate) or changed
		if getattr(row, "item_code", None) == TRADE_IN_CREDIT_ITEM:
			# A credit books to Stock Adjustment: ERPNext hands a register line the POS profile's income
			# account over the item's own default (get_item_details, POS branch), and that is Sales.
			credit_account = credit_account or _credit_income_account(doc)
			if credit_account and getattr(row, "income_account", None) != credit_account:
				_set(row, "income_account", credit_account)
				changed = True
		elif getattr(row, "item_code", None) == DEPOSIT_ITEM:
			# Held for the customer, never revenue: Customer Deposits, whatever the profile's Sales account.
			from ffl_core.setup.customer_deposits import deposit_account
			account = deposit_account(getattr(doc, "company", None) or "")
			if account and getattr(row, "income_account", None) != account:
				_set(row, "income_account", account)
				changed = True
	return changed


def _credit_income_account(doc) -> str | None:
	"""The TRADE-IN-CREDIT item's default income account for the invoice's company (setup/trade_in_credit sets it
	to Stock Adjustment), else the company's Stock Adjustment account itself."""
	from ffl_core.setup.trade_in_credit import credit_income_account

	company = getattr(doc, "company", None)
	if not company:
		return None
	return frappe.db.get_value(
		"Item Default", {"parent": TRADE_IN_CREDIT_ITEM, "parenttype": "Item", "company": company},
		"income_account") or credit_income_account(company)


def assert_locked_lines(doc, method: str | None = None) -> None:
	"""validate (after ERPNext's own): ERPNext re-applies pricing rules inside its
	validate, after the before_validate pin — a Rate-type rule covering a locked
	item rewrites its price, a Transaction rule or coupon sets an invoice discount
	that spreads onto it. Refuse, naming the fix, rather than book a shifted
	amount. Prod has no pricing rules (2026-10-03); this fires only if one is
	added that covers TRADE-IN-CREDIT."""
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return
	refuse_header_discount(doc)
	p = rate_precision(getattr(doc, "doctype", "POS Invoice"))
	for row in getattr(doc, "items", None) or []:
		want = expected_rate(doc, row)
		if not want or flt(getattr(row, "rate", 0), p) == flt(want, p):
			continue
		frappe.throw(_(
			"Row #{0}: a pricing rule changed the {1} line to {2} (it must be {3}). Exclude "
			"{1} from that pricing rule.").format(
				getattr(row, "idx", "?"), getattr(row, "item_code", ""),
				flt(getattr(row, "rate", 0), p), flt(want, p)),
			title=_("Pricing rule on a locked line"))
