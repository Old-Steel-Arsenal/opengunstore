"""A register ticket whose trade-ins exceed the goods: its sales tax, its tender, and the money paid out.

ERPNext lets such a ticket go below zero only when trade-in credit takes it there (compliance/negative_ticket);
the store then pays the customer the difference on the same ticket. Three things
follow, all POS Invoice hooks:

1. **Sales tax never goes negative.** ERPNext taxes a ticket line by line ("On Net Total"), so a credit larger than
   the taxable goods computes a NEGATIVE tax — a refund of tax nobody paid. Texas taxes the difference (Tax Code
   §151.007(c)(3)): 565 - 300 still taxes 265 (21.86); 565 - 5,450 taxes nothing, and the 4,885 is a purchase. So
   when a ticket carries trade-in credit and a tax row would come out below zero, that row's rate is set to 0 and
   ERPNext totals it again: the row, its per-item breakdown and the books all read 0.00, on the whole ticket, never
   carried to another. ``settle_tax_rates`` decides; it runs twice per save:

   * before_validate, after the locked-line pin: so ERPNext's own refusal of a negative total
     (is_negative_grand_total_allowed) already sees the floored total. Without it a $20 box of ammo against a $500
     trade-in would read -519.60, more than the credit, and be refused;
   * in the validate hooks, after ERPNext has totalled and before the trade-in gate reads the total: the amounts are
     final there (a pricing rule may have moved a rate after before_validate); whatever it changes is re-totalled
     and the tender checked again.

   The row keeps the rate it was floored from (``ffl_trade_in_floored_rate``) and gets exactly that back once the
   ticket no longer needs the floor, so a cart edited back to a positive ticket is taxed normally. A row nobody here
   floored is never given a rate: a 0% row set on purpose stays 0%. No template is read, so a ticket whose rows
   were typed in (no Sales Taxes and Charges Template) is floored the same way. A ticket without credit is never
   floored: a tax row below zero there is ERPNext's own business (a return).

2. **A negative ticket takes no tender** (``guard_negative_ticket``): every payment row is 0, there is no change, and
   the ticket says how the store paid (``ffl_trade_in_payout_method``; a reference unless cash). Payout to a
   customer only for guns bought from a private person — the same rule a seller payment follows.

3. **The payout is booked at submit** (``book_trade_in_payout``): cash_drawer.record_trade_in_payout, in the submit
   transaction, after the trade-ins themselves were booked (before_submit).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.setup.trade_in_credit import FLOORED_RATE_FIELD, TRADE_IN_CREDIT_ITEM, TRADE_IN_PAYOUT_METHODS

# The one charge type a trade-in floors. A tax-inclusive row is left alone: setting its rate to 0 would move the net
# price of every line, not just the tax. lazy: On Net Total, tax-exclusive only (both stores today); a tax-on-tax
# row (On Previous Row ...) follows the row it is based on, and an inclusive template would need its own rule.
_FLOORED_CHARGE = "On Net Total"


def _skip(doc) -> bool:
	"""Returns mirror the sale they refund (the tax actually collected); a consolidated copy carries the POS rows."""
	return bool(cint(doc.get("is_return")) or cint(doc.get("is_consolidated")))


def _has_credit(doc) -> bool:
	return any(r.get("item_code") == TRADE_IN_CREDIT_ITEM for r in doc.get("items") or [])


def invoice_total(doc) -> float:
	"""What the ticket comes to, as ERPNext reads it everywhere: the rounded total, else the grand total."""
	return flt(doc.get("rounded_total")) or flt(doc.get("grand_total"))


def _tax_at(doc, tax, rate: float) -> float:
	"""The tax ``tax`` would come to at ``rate``. Each line's rate as ERPNext derives it (get_item_tax_map): the row's
	rate unless the line's Item Tax Template sets this account (an exempt service is 0%). Not the line's stored
	``item_tax_rate``: ERPNext rebuilds that from the rows' current rates, which this hook may have set to 0."""
	from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX, _get_item_tax_map

	at = {"company": doc.get("company"), "taxes": [{"account_head": tax.account_head, "rate": rate}]}
	total = 0.0
	for item in doc.get("items") or []:
		line_rate = _get_item_tax_map(doc=at, tax_template=item.get("item_tax_template"), as_json=False).get(tax.account_head)
		if line_rate != NOT_APPLICABLE_TAX:
			total += flt(line_rate) * flt(item.get("rate")) * flt(item.get("qty")) / 100.0
	return total


def settle_tax_rates(doc) -> bool:
	"""Floor each On Net Total row that would come out below zero on a ticket with trade-in credit (rate 0, the old
	rate kept on the row), and give a floored row its rate back once the ticket no longer needs it. Returns whether
	a rate changed."""
	if _skip(doc):
		return False
	taxes = [t for t in doc.get("taxes") or []
		if t.charge_type == _FLOORED_CHARGE and not cint(t.get("included_in_print_rate"))]
	credit = _has_credit(doc)
	changed = False
	for tax in taxes:
		floored_from = flt(tax.get(FLOORED_RATE_FIELD))
		if floored_from and flt(tax.rate):
			tax.set(FLOORED_RATE_FIELD, 0)  # a rate was put back on the row since (a template picked again): it stands
			floored_from = 0
		rate = floored_from or flt(tax.rate)
		if not rate:
			continue  # a 0% row this hook did not make: never ours
		floor = credit and _tax_at(doc, tax, rate) < 0
		if floor and not floored_from:
			tax.set(FLOORED_RATE_FIELD, rate)
			tax.rate = 0
			changed = True
		elif floored_from and not floor:
			tax.rate = floored_from
			tax.set(FLOORED_RATE_FIELD, 0)
			changed = True
	return changed


def _settle_change(doc) -> None:
	"""Change as ERPNext works it out (paid - total + write-off, when paid exceeds the total), never below a ticket
	that is itself below zero: there the store pays, it gives no change. Never refuses (unlike
	POSInvoice.validate_change_amount, which wants a change account)."""
	total = invoice_total(doc)
	paid = flt(doc.get("paid_amount"))
	change = paid - total + flt(doc.get("write_off_amount")) if total >= 0 and paid > total else 0
	doc.change_amount = flt(change, doc.precision("change_amount"))
	doc.base_change_amount = flt(change * flt(doc.get("conversion_rate") or 1), doc.precision("base_change_amount"))


def floor_trade_in_tax_before(doc, method: str | None = None) -> None:
	"""POS Invoice before_validate (after the locked-line pin): settle the rates before ERPNext totals the ticket.

	A ticket that may go below zero also gets a paid amount of 0 when its caller sent none (an API caller; the
	register always sends one): ERPNext works out change before it fills the paid amount, and a total below zero
	is the one case where it subtracts from it."""
	settle_tax_rates(doc)
	if doc.get("paid_amount") is None and _has_credit(doc):
		doc.paid_amount = doc.base_paid_amount = 0


def floor_trade_in_tax(doc, method: str | None = None) -> None:
	"""POS Invoice validate, ahead of the trade-in gate: settle again on ERPNext's final line amounts. Anything it
	changes is totalled again, and what ERPNext checked on the old total — change, full payment — is checked again."""
	if not settle_tax_rates(doc):
		return
	doc.calculate_taxes_and_totals()
	_settle_change(doc)
	doc.validate_full_payment()


def guard_negative_ticket(doc, method: str | None = None) -> None:
	"""POS Invoice validate: a ticket below zero takes no tender and says how the store paid the customer.

	Every save: no change (ERPNext would make the whole payout "change" — paid 0 over a total of -4,885). At submit:
	every payment row 0, a payout method, a reference unless cash, and every credited gun bought from a private
	person. A ticket at or above zero carries no payout method (a cart that went negative and back)."""
	if _skip(doc):
		return
	if invoice_total(doc) >= 0:
		if doc.get("ffl_trade_in_payout_method") or doc.get("ffl_trade_in_payout_reference"):
			doc.ffl_trade_in_payout_method = None
			doc.ffl_trade_in_payout_reference = None
		return
	_settle_change(doc)
	if cint(doc.get("docstatus")) != 1:
		return  # a cart mid-edit: checked at checkout, like the 4473 gate
	owed = -invoice_total(doc)
	paid = [p for p in doc.get("payments") or [] if flt(p.amount)]
	if paid:
		frappe.throw(_(
			"This sale's trade-ins come to more than what the customer buys: the store pays the customer {0}, so "
			"no payment is taken. Set every payment amount to 0 ({1}).").format(
				frappe.format_value(owed, {"fieldtype": "Currency"}), ", ".join(p.mode_of_payment for p in paid)),
			title=_("The store pays the customer"))
	pay_by = doc.get("ffl_trade_in_payout_method")
	if pay_by not in TRADE_IN_PAYOUT_METHODS:
		frappe.throw(_("Say how the store paid the customer the {0}: {1}.").format(
			frappe.format_value(owed, {"fieldtype": "Currency"}), ", ".join(TRADE_IN_PAYOUT_METHODS)),
			title=_("The store pays the customer"))
	reference = (doc.get("ffl_trade_in_payout_reference") or "").strip()
	doc.ffl_trade_in_payout_reference = reference or None
	if pay_by != "Cash" and not reference:
		frappe.throw(_("A {0} payment needs its reference: the check number, the Zelle confirmation or the ACH "
			"reference.").format(pay_by), title=_("The store pays the customer"))
	from ffl_core.acquisition import INDIVIDUAL_SOURCES
	for row in doc.get("items") or []:
		acq = row.get("ffl_trade_in_acquisition") if row.get("item_code") == TRADE_IN_CREDIT_ITEM else None
		if acq and frappe.db.get_value("FFL Acquisition", acq, "source_type") not in INDIVIDUAL_SOURCES:
			frappe.throw(_(
				"Row #{0}: trade-in {1} was not bought from a private person, so the store cannot pay the customer "
				"for it on this ticket.").format(row.idx, acq), title=_("The store pays the customer"))


def book_trade_in_payout(doc, method: str | None = None) -> None:
	"""POS Invoice on_submit: book what the store paid the customer of a ticket below zero (once per ticket)."""
	if _skip(doc) or invoice_total(doc) >= 0:
		return
	from ffl_core.cash_drawer import record_trade_in_payout
	record_trade_in_payout(doc)
