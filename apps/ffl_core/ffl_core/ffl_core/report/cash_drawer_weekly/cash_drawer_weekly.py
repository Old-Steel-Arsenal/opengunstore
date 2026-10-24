"""Cash Drawer Weekly — one row per day of the cash drawer, for the bookkeeper.

Every figure comes from the Cash account's ledger, classified by what moved it (a register sale, a counter
order, a customer payment, a refund, a seller paid for a gun or for received goods, a bank deposit, cash from the
bank, a cash expense, a count difference). The foot of the report proves it:

* the sum of the daily movements equals the Cash account's movement over the same dates (difference 0.00);
* the ledger balance at the end of the last day equals the last count, when that day was counted;
* any ledger line that did not come from the drawer screens, a sale or a payment is listed as unclassified
  (it should be empty: after the first count nobody posts to Cash by hand).

The first count of a company sets the books to the physical drawer (CASH-CUTOFF); a later correction to the days
before it is booked to it at the next count (CASH-CUTOFF TRUE-UP, dated the cut-off day). Each one in the range is
printed on the first lines, for the accountant, in its own column apart from the count differences (whose total is
the Cash Over/Short account's). A correction not booked yet is shown at the foot and left out of the books-vs-count
check.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, getdate

from ffl_core import cash_drawer as cd

_MAX_DAYS = 400

# (fieldname, label, which ledger kinds fill it)
_MONEY = (
	("cash_pos", "Register sales", ("sale_pos",)),
	("cash_counter", "Counter orders", ("sale_counter",)),
	("cash_payments", "Customer payments", ("payment_in", "order_payment")),
	("refunds", "Cash refunds", ("refund",)),
	("payouts", "Paid to sellers", ("payout", "trade_in_payout", "goods_payout")),
	("deposits", "Bank deposits", ("deposit",)),
	("from_bank", "Cash from bank", ("from_bank",)),
	("owner", "Owner took (old entries)", ("owner",)),
	("expenses", "Cash expenses", ("expense",)),
	("count_adjust", "Count difference", ("variance",)),
	("first_count", "First count & history", ("cutoff", "cutoff_trueup")),
	("other", "Unclassified", ("unclassified",)),
)
_MONEY_FIELDS = [m[0] for m in _MONEY]
# Kinds listed in another kind's detail column: a customer paid for trade-ins, and a seller paid for received goods,
# sit with the seller payments.
_DETAIL = {"trade_in_payout": "payout", "goods_payout": "payout"}


def _columns() -> list[dict]:
	cols = [{"fieldname": "day", "label": _("Day"), "fieldtype": "Data", "width": 110},
		{"fieldname": "opening", "label": _("Opening (books)"), "fieldtype": "Currency", "width": 120}]
	for field, label, _kinds in _MONEY:
		cols.append({"fieldname": field, "label": _(label), "fieldtype": "Currency", "width": 110})
		if field == "cash_payments":
			cols.append({"fieldname": "receipt_docs", "label": _("Receipt vouchers"), "fieldtype": "Data", "width": 180})
		elif field == "refunds":
			cols.append({"fieldname": "refund_docs", "label": _("Refund vouchers"), "fieldtype": "Data", "width": 160})
		elif field == "payouts":
			cols.append({"fieldname": "payout_detail", "label": _("Seller payments (acquisition or receipt, serial, seller)"),
				"fieldtype": "Data", "width": 240})
		elif field == "deposits":
			cols.append({"fieldname": "deposit_detail", "label": _("Deposit reference"), "fieldtype": "Data", "width": 140})
		elif field == "from_bank":
			cols.append({"fieldname": "from_bank_detail", "label": _("Withdrawal reference"), "fieldtype": "Data", "width": 140})
		elif field == "owner":
			cols.append({"fieldname": "owner_detail", "label": _("Taken by"), "fieldtype": "Data", "width": 140})
		elif field == "expenses":
			cols.append({"fieldname": "expense_detail", "label": _("Expenses (account, memo, receipt)"),
				"fieldtype": "Data", "width": 260})
	cols += [
		{"fieldname": "closing_gl", "label": _("Closing (books)"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "expected", "label": _("Expected"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "counted", "label": _("Counted"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "variance", "label": _("Difference"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "variance_je", "label": _("Difference entry"), "fieldtype": "Link", "options": "Journal Entry", "width": 140},
		{"fieldname": "reason", "label": _("Reason"), "fieldtype": "Data", "width": 200},
		{"fieldname": "counted_flag", "label": _("Counted?"), "fieldtype": "Data", "width": 220},
		{"fieldname": "counted_by", "label": _("Counted by / at"), "fieldtype": "Data", "width": 200},
		{"fieldname": "note", "label": _("Note"), "fieldtype": "Data", "width": 360},
	]
	return cols


def _seller_names(acquisitions: set[str]) -> dict[str, str]:
	if not acquisitions:
		return {}
	rows = frappe.get_all("FFL Acquisition", filters={"name": ["in", list(acquisitions)]}, fields=["name", "source_name"])
	return {r.name: r.source_name or "" for r in rows}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company, from_date, to_date = filters.company, getdate(filters.from_date), getdate(filters.to_date)
	if not company:
		frappe.throw(_("Pick a company."))
	if to_date < from_date:
		frappe.throw(_("The end date is before the start date."))
	if date_diff(to_date, from_date) > _MAX_DAYS:
		frappe.throw(_("Pick a range of {0} days or less.").format(_MAX_DAYS))
	account = cd.cash_account(company)
	rows = cd.cash_ledger_rows(company, from_date=from_date, to_date=to_date)
	sellers = _seller_names({r.entry.acquisition for r in rows if r.kind == "payout" and r.entry})
	closes = {}
	for c in cd.active_closes(company, business_date=["between", [from_date, to_date]]):
		closes.setdefault(str(c.business_date), c)  # newest first: one per day anyway
	threshold = cd.variance_threshold()

	by_day: dict[str, list] = {}
	for r in rows:
		by_day.setdefault(str(r.posting_date), []).append(r)

	opening = cd.ledger_sum(company, account, before=from_date)
	data = []
	totals = {f: 0.0 for f in _MONEY_FIELDS}
	day_sum = 0.0
	cutoff_notes = []
	day = from_date
	while day <= to_date:
		key = str(day)
		out = {"day": key, "opening": opening, "row_kind": "day", "receipt_docs": "", "refund_docs": "",
			"payout_detail": "", "deposit_detail": "", "from_bank_detail": "", "owner_detail": "", "expense_detail": ""}
		details = {"payout": [], "deposit": [], "from_bank": [], "owner": [], "expense": []}
		for f in _MONEY_FIELDS:
			out[f] = 0.0
		receipts, refunds = [], []
		for r in by_day.get(key, []):
			field = next(f for f, _l, kinds in _MONEY if r.kind in kinds)
			out[field] = flt(out[field] + r.amount, 2)
			if r.kind in ("sale_pos", "sale_counter", "payment_in", "order_payment"):
				receipts.append(r.voucher_no)
			elif r.kind == "refund":
				refunds.append(r.voucher_no)
			elif _DETAIL.get(r.kind, r.kind) in details:
				details[_DETAIL.get(r.kind, r.kind)].append(cd.entry_detail(r.entry, sellers.get(r.entry.acquisition) if r.entry else None))
			elif r.kind in ("cutoff", "cutoff_trueup"):
				cutoff_notes.append(r)
		out["receipt_docs"] = ", ".join(receipts)
		out["refund_docs"] = ", ".join(refunds)
		for k in ("payout", "deposit", "from_bank", "owner", "expense"):
			out[f"{k}_detail"] = "; ".join(d for d in details[k] if d)
		net = flt(sum(out[f] for f in _MONEY_FIELDS), 2)
		out["closing_gl"] = flt(opening + net, 2)
		close = closes.get(key)
		if close:
			out["expected"] = close.expected
			out["counted"] = close.counted if close.is_counted else None
			out["variance"] = close.variance if close.is_counted else None
			out["variance_je"] = close.variance_je or close.cutoff_je
			out["reason"] = close.reason or ""
			if close.is_counted:
				out["counted_flag"] = _("Yes") if (close.days_covered or 1) <= 1 else _(
					"Yes: covers {0} days, from {1}").format(close.days_covered, close.covers_from)
				out["counted_by"] = f"{close.counted_by} {str(close.counted_at)[:16]}"
				if close.is_cutoff:
					out["note"] = _("First count: the books were set to the drawer.")
					later = [r for r in by_day.get(key, []) if r.kind == "cutoff_trueup"]
					if later:
						out["note"] += " " + _("History later re-measured by {0} ({1}).").format(
							cd._fmt(sum(r.amount for r in later)), ", ".join(r.voucher_no for r in later))
				elif abs(flt(close.variance)) >= threshold:
					out["flagged"] = 1
			else:
				out["counted_flag"] = _("No")
		for f in _MONEY_FIELDS:
			totals[f] = flt(totals[f] + out[f], 2)
		day_sum = flt(day_sum + net, 2)
		opening = out["closing_gl"]
		data.append(out)
		day = add_days(day, 1)

	rows_out = []
	for r in cutoff_notes:
		rows_out.append({"day": _(cd.KIND_LABELS[r.kind]), "row_kind": "note", "first_count": r.amount,
			"note": _("{0} on {1} (made {2}), {3}: {4}. Hand this entry to the accountant.").format(
				r.voucher_no, r.posting_date, str(r.creation)[:10], cd._fmt(r.amount), r.remark)})
	rows_out += data

	total = {"day": _("Total"), "row_kind": "check", **totals}
	rows_out.append(total)

	ledger_movement = cd.ledger_sum(company, account, from_date=from_date, to_date=to_date)
	rows_out.append({"day": _("Daily lines add up to"), "row_kind": "check", "closing_gl": day_sum})
	rows_out.append({"day": _("Cash ledger movement"), "row_kind": "check", "closing_gl": ledger_movement,
		"note": _("The Cash account over the same dates.")})
	rows_out.append({"day": _("Difference"), "row_kind": "check", "closing_gl": flt(day_sum - ledger_movement, 2),
		"note": _("Must be 0.00.")})
	end_balance = cd.ledger_sum(company, account, to_date=to_date)
	last = closes.get(str(to_date))
	rows_out.append({"day": _("Books at the end of {0}").format(to_date), "row_kind": "check", "closing_gl": end_balance})
	# A correction to the days before the first count that the next count has not booked yet: in every balance after
	# the day it is dated, never in the drawer.
	history = cd.history_change(company, account)
	if abs(history) >= 0.005:
		rows_out.append({"day": _("History changed after the first count, not yet booked"), "row_kind": "check",
			"closing_gl": history, "note": _("Booked to the first count at the next count.")})
	if last and last.is_counted:
		rows_out.append({"day": _("Counted on {0}").format(to_date), "row_kind": "check", "closing_gl": last.counted})
		rows_out.append({"day": _("Difference"), "row_kind": "check", "closing_gl": flt(end_balance - flt(last.counted) - history, 2),
			"note": _("Must be 0.00 unless cash moved after the count, or register sales are still being posted.")})
	else:
		rows_out.append({"day": _("Last day not counted"), "row_kind": "note",
			"note": _("The books are compared with a count only for a counted day.")})
	unclassified = [r for r in rows if r.kind == "unclassified"]
	if unclassified:
		for r in unclassified:
			rows_out.append({"day": str(r.posting_date), "row_kind": "check", "other": r.amount,
				"note": _("Unclassified: {0} {1}").format(r.voucher_type, r.voucher_no)})
	else:
		rows_out.append({"day": _("Unclassified ledger lines"), "row_kind": "note", "note": _("None.")})
	columns = _columns()
	if not any(r.kind == "owner" for r in rows):  # owner draws are no longer made: show the column only for old ones
		columns = [c for c in columns if c["fieldname"] not in ("owner", "owner_detail")]
	return columns, rows_out
