"""Submit-time gates for manually-settled tenders: the money moved somewhere this
system cannot see, so the operator has to record the OTHER system's confirmation.

Three tenders need this, for the same reason:

* **Zelle** — the customer sends money in the Zelle app and the cashier marks it
  received. Without the bank/app confirmation number the deposit cannot be
  reconciled against the sale.
* **Check/Money Order** — the paper goes to the bank later; its number is what
  ties the deposit back to the sale.
* **Credit Card with the Payroc integration switched off** — the card is swiped on
  a standalone terminal, so nothing comes back from a gateway and the only link
  between the sale and that terminal's batch report is what the cashier types.

Both store it in the standard ``Sales Invoice Payment.reference_no`` (surfaced to
staff as the "Transaction Number"), and both go through ``require_tender_reference``
so the document-level rules cannot drift apart between them.

Wired into POS Invoice + Sales Invoice validate: Zelle and Check/Money Order
from ffl_core's hooks, the
card one from ffl_integrations' — the Credit Card mode of payment and the switch
that governs it both belong to that app, so only the tender-agnostic half lives
here.
"""
from __future__ import annotations

import re
from collections.abc import Callable

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.setup.payments import CHECK_MONEY_ORDER, ZELLE

_PAN_SEPARATORS = re.compile(r"[ \-]")


def looks_like_pan(value: str) -> bool:
	"""True when the string is plausibly a card NUMBER — the one thing that must
	never land in an accounting voucher's reference field (it would turn the GL
	into cardholder data). Shape: 13-19 digits after stripping spaces/dashes,
	AND Luhn-valid — the checksum is what keeps ordinary long references (order
	numbers, bank confirmations) from tripping this, since a random digit string
	fails Luhn 9 times out of 10 and a real approval code is 6-8 characters."""
	digits = _PAN_SEPARATORS.sub("", (value or "").strip())
	if not digits.isdigit() or not 13 <= len(digits) <= 19:
		return False
	total = 0
	for i, ch in enumerate(reversed(digits)):
		d = int(ch)
		if i % 2 == 1:
			d = d * 2 - 9 if d > 4 else d * 2
		total += d
	return total % 10 == 0


def require_tender_reference(doc, *, mode_of_payment: str,
		message: Callable[[float], str], include_refunds: bool = False) -> None:
	"""Reject a submit whose `mode_of_payment` rows carry no ``reference_no``.

	The document-level rules live here ONCE, because they are properties of how
	POS builds invoices rather than of any particular tender:

	* **SUBMIT only.** A mid-edit draft legitimately has no number yet — POS
	  prompts at Complete Order and the Create Order page collects it inline — and
	  blocking a draft save would strand the cashier mid-cart.
	* **Consolidated invoices exempt.** POS Invoice Merge Log sums payments by
	  (account, mode) and drops the per-invoice reference_no; each source POS
	  Invoice already cleared this gate at its own submit.
	* **Zero rows skipped**, so an empty tender line never blocks anything.
	* **Refund rows skipped unless ``include_refunds``** — the two callers differ
	  here, deliberately; see each one's docstring.

	Args:
		doc: The invoice being validated.
		mode_of_payment: Which tender rows this gate applies to.
		message: Called with the row amount, returns the already-translated error.
			A callable rather than a format string so translators get whole
			sentences instead of grammatical fragments.
		include_refunds: Also require a reference on negative rows.
	"""
	if int(getattr(doc, "docstatus", 0) or 0) != 1:
		return
	if int(getattr(doc, "is_consolidated", 0) or 0):
		return
	for row in (doc.get("payments") or []):
		if getattr(row, "mode_of_payment", "") != mode_of_payment:
			continue
		amount = flt(getattr(row, "amount", 0))
		if amount == 0:
			continue
		if amount < 0 and not include_refunds:
			continue
		reference = (getattr(row, "reference_no", "") or "").strip()
		if not reference:
			frappe.throw(message(amount))
		if looks_like_pan(reference):
			frappe.throw(_(
				"The reference on the {0} row of {1} looks like a full card "
				"number. Record the approval / confirmation code — never the "
				"card number."
			).format(mode_of_payment,
				frappe.format_value(abs(amount), {"fieldtype": "Currency"})))


def require_zelle_transaction_no(doc, method: str | None = None) -> None:
	"""Every positive Zelle tender must carry its bank/app confirmation number.

	Returns are not gated: a return's negative Zelle row is money the store pushes
	back by its own action, and there is no incoming deposit to reconcile it
	against.
	"""
	require_tender_reference(
		doc,
		mode_of_payment=ZELLE,
		message=lambda amount: _(
			"Enter the Zelle Transaction Number for the Zelle payment of {0} "
			"before completing the sale."
		).format(frappe.format_value(amount, {"fieldtype": "Currency"})),
	)


def require_check_money_order_number(doc, method: str | None = None) -> None:
	"""Every positive Check/Money Order tender must carry its check or money
	order number. Returns are not gated, for Zelle's reason above."""
	require_tender_reference(
		doc,
		mode_of_payment=CHECK_MONEY_ORDER,
		message=lambda amount: _(
			"Enter the check or money order number for the Check/Money Order "
			"payment of {0} before completing the sale."
		).format(frappe.format_value(amount, {"fieldtype": "Currency"})),
	)


def require_full_payment(doc, method: str | None = None) -> None:
	"""A POS sale is submitted only once its whole total is recorded as paid.

	ERPNext makes the same check (validate_full_payment) but skips it when the POS
	Profile allows partial payment — and the store's profiles must, because the
	register refuses a $0 counter pickup ticket otherwise. Without this gate a
	ticket half-paid in cash went through, consolidated at once, and the rest
	(charged on the Virtual Terminal) sat as an unexplained customer balance.
	The test is ERPNext's own outstanding amount, which its before_submit sets (validate still sees the
	draft's 0), hence a before_submit hook. Returns and tickets at or below zero are not sales
	the customer pays for, so they pass. Paying in parts stays possible: the draft keeps each payment (Recent
	Orders → Edit to take the next one) and completes once the total is paid."""
	from ffl_core.compliance.negative_trade_in import invoice_total

	if cint(doc.get("is_return")):
		return
	total = invoice_total(doc)
	# POS Invoice.set_outstanding_amount is total - paid; a write-off within the profile's limit covers the rest.
	short = flt(doc.get("outstanding_amount")) - flt(doc.get("write_off_amount"))
	if total <= 0 or short <= 0.01:
		return
	fmt = lambda v: frappe.format_value(v, {"fieldtype": "Currency"})  # noqa: E731
	message = _(
		"This sale comes to {0} but {1} of it is not paid yet, so it cannot be completed. If the customer pays the "
		"rest later, leave it as a draft: what was paid stays on the order, and you collect the rest from Recent "
		"Orders → Edit.").format(fmt(total), fmt(short))
	if "ffl_integrations" in frappe.get_installed_apps():  # the card integration, and its manual-settlement box
		message += " " + _("A card charged outside the register (Virtual Terminal) goes in the Credit Card amount, "
			"with a manager ticking Manual Card Settlement and entering the approval code.")
	frappe.throw(message)
