"""Submit-time gate + checkout booking for trade-in credit rows.

A TRADE-IN-CREDIT line is money out the door, so it must be tamper-evident:
every credit row has to trace to a Trade-in FFL Acquisition (a gun) or a
trade-in Material Receipt (accessories / ammo, trade_in._park_accessories),
match its value exactly (qty 1, rate = -cost), and either can be spent once. Without
this, a cashier could type any discount into a "trade-in" line — the exact
sweethearting hole the credit feature is meant to close.

Wired into POS Invoice + Sales Invoice validate (hooks.py). Like the 4473
gate, it fires at SUBMIT only: mid-edit carts transiently mismatch (price-list
refreshes, row edits), and blocking a draft save would strand the cashier.
Returns are exempt — a return legitimately carries the mirrored positive row.

Deferred booking: on a POS Invoice the linked
acquisition may still be a DRAFT at gate time — the trade-in dialog parks it
unbooked so an abandoned cart never leaves a phantom gun in the bound book.
book_trade_ins_on_submit (POS Invoice before_submit) completes those bookings
in the same transaction; a Sales Invoice has no booking hook, so its gate
keeps requiring a submitted acquisition (the consolidated copy of a POS row
arrives already booked).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM, TRADE_IN_SELLER_FIELD, TRADE_IN_STOCK_ENTRY_FIELD


def accessory_receipt_value(stock_entry: str) -> float | None:
	"""What a trade-in Material Receipt of accessories / ammo is worth: the sum of its lines at the cost entered
	in the dialog. None when it is no such receipt (missing, not a Material Receipt, or not marked a trade-in)."""
	se = frappe.db.get_value("Stock Entry", stock_entry,
		["stock_entry_type", TRADE_IN_SELLER_FIELD], as_dict=True)
	if not se or se.stock_entry_type != "Material Receipt" or not se.get(TRADE_IN_SELLER_FIELD):
		return None
	rows = frappe.get_all("Stock Entry Detail", filters={"parent": stock_entry, "parenttype": "Stock Entry"},
		fields=["qty", "basic_rate"])
	return sum(flt(r.qty) * flt(r.basic_rate) for r in rows)


def credit_source(row) -> tuple[str, str] | None:
	"""(doctype, name) the credit row is locked to: its FFL Acquisition, else its trade-in Stock Entry."""
	acq = getattr(row, "ffl_trade_in_acquisition", None)
	if acq:
		return "FFL Acquisition", acq
	se = getattr(row, TRADE_IN_STOCK_ENTRY_FIELD, None)
	return ("Stock Entry", se) if se else None


def _source_value(doctype: str, name: str) -> float | None:
	if doctype == "FFL Acquisition":
		cost = frappe.db.get_value("FFL Acquisition", name, "cost")
		return None if cost is None else flt(cost)
	return accessory_receipt_value(name)


def expected_credit_rate(doc, row) -> float | None:
	"""What a credit row must be: minus the linked acquisition's cost, at the
	row's rate precision. A consolidated copy carries the original row's lock
	(the amount was fixed when that sale submitted). A return is never pinned:
	it mirrors the row it refunds (after consolidation, the booked net rate).
	None when there is nothing to lock to — the submit gate refuses the row then."""
	from ffl_core.compliance.locked_lines import locked_rate_on_row, rate_precision
	source = credit_source(row)
	if not source or int(getattr(doc, "is_return", 0) or 0):
		return None
	if int(getattr(doc, "is_consolidated", 0) or 0):
		return locked_rate_on_row(row)
	cost = _source_value(*source)
	if cost is None:
		return None
	return flt(-flt(cost), rate_precision(getattr(doc, "doctype", "POS Invoice")))


def _credit_rows(doc) -> list:
	return [r for r in (doc.items or [])
		if getattr(r, "item_code", "") == TRADE_IN_CREDIT_ITEM]


def negative_trade_in_ok(doc) -> bool:
	"""May this invoice go below zero? Only when trade-in credit is what pushes it there: the store then pays
	the customer the difference on the same ticket.

	True when: not a return; at least one TRADE-IN-CREDIT row; every other row is non-negative (a
	negative price never makes a ticket negative); no header discount; and the shortfall is no
	more than the credit (-grand_total <= sum of credits). Cheap and pure: ERPNext asks on every validate.
	The per-acquisition rules (individual seller, not already paid out, spent once) stay in the submit gate
	(validate_trade_in_credit_rows)."""
	if int(getattr(doc, "is_return", 0) or 0):
		return False
	rows = _credit_rows(doc)
	if not rows:
		return False
	if any(flt(r.amount) < 0 for r in (doc.items or []) if getattr(r, "item_code", "") != TRADE_IN_CREDIT_ITEM):
		return False
	if flt(getattr(doc, "discount_amount", 0)):
		return False
	credit = sum(-flt(r.amount) for r in rows)
	return -flt(getattr(doc, "base_grand_total", 0) or getattr(doc, "grand_total", 0)) <= credit + 0.005


def validate_trade_in_credit_rows(doc, method: str | None = None) -> None:
	if int(getattr(doc, "docstatus", 0) or 0) != 1:
		return  # drafts may transiently mismatch — gate at submit, like 4473
	if int(getattr(doc, "is_return", 0) or 0):
		return
	rows = _credit_rows(doc)
	if not rows:
		return

	# POS Invoice: a Draft acquisition is legitimate mid-submit (the
	# before_submit hook books it); anywhere else it means the booking hook
	# never ran — refuse.
	draft_ok = getattr(doc, "doctype", "") == "POS Invoice"

	seen: set[str] = set()
	for row in rows:
		source = credit_source(row)
		if not source or (getattr(row, "ffl_trade_in_acquisition", None)
				and getattr(row, TRADE_IN_STOCK_ENTRY_FIELD, None)):
			frappe.throw(_(
				"Row #{0}: a Trade-in Credit line must be added through the POS "
				"Trade-in dialog — it has no linked trade-in record."
			).format(row.idx))
		doctype, acq_name = source
		if acq_name in seen:
			frappe.throw(_(
				"Row #{0}: trade-in {1} appears more than once on this invoice."
			).format(row.idx, acq_name))
		seen.add(acq_name)

		docstatus = frappe.db.get_value(doctype, acq_name, "docstatus")
		value = _source_value(doctype, acq_name)
		if docstatus not in ((0, 1) if draft_ok else (1,)) or value is None:
			frappe.throw(_(
				"Row #{0}: trade-in {1} is missing or not submitted."
			).format(row.idx, acq_name))
		if flt(getattr(row, "qty", 0)) != 1:
			frappe.throw(_(
				"Row #{0}: a Trade-in Credit line must have quantity 1."
			).format(row.idx))
		# Compare at the row's rate precision: ERPNext rounds rate on every
		# save, while an API caller may file a sub-cent cost (180.005).
		from ffl_core.compliance.locked_lines import rate_precision, row_rate
		p = rate_precision(getattr(doc, "doctype", "POS Invoice"))
		if flt(row_rate(doc, row), p) != flt(-value, p):
			frappe.throw(_(
				"Row #{0}: Trade-in Credit must equal the recorded trade-in value: "
				"expected {1}, found {2}. Edit the trade-in ({3}) "
				"instead of the invoice line."
			).format(row.idx, flt(-value, p), flt(row_rate(doc, row), p), acq_name))
		_require_unspent(acq_name, doc, "ffl_trade_in_acquisition" if doctype == "FFL Acquisition"
			else TRADE_IN_STOCK_ENTRY_FIELD)
		if doctype != "FFL Acquisition":
			continue  # accessories taken in trade are never paid out: a pay-now buy is a Purchase Receipt
		acq = frappe.db.get_value("FFL Acquisition", acq_name, ["purchase_receipt"], as_dict=True)
		# A gun received on a Purchase Receipt (Receive Goods, or the Trade-in dialog's pay-now buy) is a purchase, paid
		# with the receipt (a Goods Payout keyed to the receipt, not the acquisition); a credit is only ever a dialog
		# trade-in, which has no receipt.
		if acq.purchase_receipt or frappe.db.exists(
				"Cash Drawer Entry", {"acquisition": acq_name, "entry_type": "Payout", "status": "Posted"}):
			frappe.throw(_(
				"Row #{0}: the seller of trade-in {1} was already paid out, so it cannot also be a credit on a sale."
			).format(row.idx, acq_name))

	# Below zero only when the trade-ins are what take it there; the store then pays the customer the
	# difference on this ticket (compliance/negative_trade_in). ERPNext asked the same question in its own
	# validate; asking again here keeps the gate whole for a caller that skips it.
	if flt(getattr(doc, "grand_total", 0)) < 0 and not negative_trade_in_ok(doc):
		frappe.throw(_(
			"This sale comes to {0}: only trade-in credit may take a sale below zero, and never by more than the "
			"credit. Remove the other negative line or the invoice discount."
		).format(flt(getattr(doc, "grand_total", 0))))


def book_trade_ins_on_submit(doc, method: str | None = None) -> None:
	"""POS Invoice before_submit: the checkout completing IS the trade
	completing — book every deferred trade-in the credit rows reference
	(submit the draft acquisition → Material Receipt → per-unit stamp; the
	FastBound push is enqueued after commit). Runs inside the submit
	transaction: if booking fails the checkout aborts whole, and a submit that
	fails later rolls the booking back before anything reaches FastBound.
	No-ops for returns and for already-booked (legacy / cash-buy) rows."""
	if int(getattr(doc, "is_return", 0) or 0):
		return
	from ffl_core.api.firearm_intake import complete_intake
	for row in _credit_rows(doc):
		acq_name = getattr(row, "ffl_trade_in_acquisition", None)
		if acq_name:
			complete_intake(acq_name)
		elif getattr(row, TRADE_IN_STOCK_ENTRY_FIELD, None):
			_book_accessories(getattr(row, TRADE_IN_STOCK_ENTRY_FIELD))


def _book_accessories(stock_entry: str) -> None:
	"""Submit the parked Material Receipt of accessories / ammo taken in trade: they enter stock at the cost the
	dialog recorded (Dr Stock / Cr Stock Adjustment, where the credit line books — the same pair as a gun's
	receipt). Idempotent: an already-submitted receipt is left alone."""
	se = frappe.get_doc("Stock Entry", stock_entry)
	if se.docstatus == 1:
		return
	if se.docstatus == 2:
		frappe.throw(_("Trade-in receipt {0} is cancelled — remove the credit line or re-run the Trade-in "
			"dialog.").format(stock_entry))
	se.flags.ignore_permissions = True
	se.submit()


def _require_unspent(acq_name: str, doc, field: str = "ffl_trade_in_acquisition") -> None:
	# POS Invoice only: the consolidated Sales Invoice legitimately carries a
	# copy of the spent row, so running the reuse lookup there would false-
	# positive against the very POS invoice being consolidated.
	if getattr(doc, "doctype", "") != "POS Invoice":
		return
	used = frappe.db.sql(
		"""
		SELECT pii.parent
		FROM `tabPOS Invoice Item` pii
		INNER JOIN `tabPOS Invoice` pi ON pi.name = pii.parent
		WHERE pii.`{field}` = %(acq)s
		  AND pi.docstatus = 1
		  AND COALESCE(pi.is_return, 0) = 0
		  AND pi.name != %(self)s
		LIMIT 1
		""".format(field=field),
		{"acq": acq_name, "self": getattr(doc, "name", "") or ""},
	)
	if used:
		frappe.throw(_(
			"Trade-in {0} was already credited on invoice {1} — a trade-in can "
			"only be spent once."
		).format(acq_name, used[0][0]))
