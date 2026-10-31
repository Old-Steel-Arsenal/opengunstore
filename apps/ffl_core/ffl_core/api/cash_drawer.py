"""Cash Drawer page backend: today's drawer, the count, and the cash that leaves it.

All the rules (expected cash, the count, the journal entries, the limits) live in
``ffl_core.cash_drawer``; these endpoints only check who is asking and shape the answers
for the page. Counting the drawer and the cash entries are counter work; undoing anything
is for managers.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, get_datetime, getdate, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.roles import CASH_LOG_ROLES, CASH_MANAGER_ROLES, COUNTER_ROLES

# Entry types a manager may undo from the page. A seller payment for a gun is fixed by a System Manager
# (undo, then record the payment again), because it is tied to the acquisition; a seller payment for a
# Receive Goods receipt is a manager's, like any other cash entry.
_UNDOABLE = frozenset({"Deposit", "From Bank", "Expense", "Owner Draw", "Goods Payout"})  # an old owner draw can still be undone
# Lines that move the balance without being cash in or out: a count difference or the first count.
_NOT_CASH = ("variance", "cutoff", "cutoff_trueup")


def _company(company: str | None) -> str:
	company = (company or "").strip() or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("No company is set up yet."))
	return company


def _is_manager() -> bool:
	return bool(set(frappe.get_roles()) & set(CASH_MANAGER_ROLES))


@frappe.whitelist()
def get_today(company: str | None = None) -> dict:
	"""Everything the Cash Drawer page shows.

	What the drawer should hold now, what moved it today or since the last close (whichever is later), today's close,
	today's seller payments by bank, and the choices for the forms. Read only: it never creates an account or changes a default.

	Args:
	    company: Company to show; defaults to the user's company.

	Returns:
	    {company, is_manager, expected, gl_balance, pending, history, opening, last_close, closed_today,
	    can_undo_close, first_count, open_shifts, bank_account, threshold, expense_cap,
	    expense_accounts, cash_in, cash_out (the lines' cash in and out, both positive; count differences and the
	    first count are neither), lines: [{kind, label, direction, voucher_type, voucher_no, amount, when, detail,
	    entry, can_undo, can_fix, acquisition, purchase_receipt}], older_lines, older_net, other,
	    bank_payouts: [{entry, acquisition, purchase_receipt, serial, amount, method, detail, can_fix}],
	    unpaid_goods_receipts: [{purchase_receipt, customer, amount, method, posting_date, can_fix}] (Receive Goods
	    purchases from private sellers received since cash tracking started with no seller payment standing: never
	    paid, or undone; method is the last one recorded, if any), payout_methods}.
	"""
	frappe.only_for(COUNTER_ROLES)
	company = _company(company)
	exp = cd.drawer_expected(company)
	account = exp.account
	closes = cd.active_closes(company)
	last = closes[0] if closes else None
	since = last.closed_at if last else None
	today_start = get_datetime(nowdate())
	window = max(get_datetime(since), today_start) if since else today_start
	# Corrections to the days before the first count never moved the drawer: not listed (``history`` says how much).
	first = cd.cutoff_close(company)
	cutoff_day = first.business_date if first else None
	rows = [r for r in cd.cash_ledger_rows(company, created_after=window, account=account, from_date=cutoff_day)
		if r.kind != "cutoff_trueup"] + cd.pending_pos_rows(company, account, created_after=since)
	rows.sort(key=lambda r: str(r.creation))
	older_lines, older_net = cd.ledger_between(company, account, since, window, from_date=cutoff_day)
	counted = next((c for c in closes if c.is_counted), None)
	manager = _is_manager()
	system_manager = "System Manager" in frappe.get_roles()
	lines = []
	for r in rows:
		e = r.entry
		# An entry a count has already relied on can no longer be undone.
		locked = bool(e and cd.relied_on(counted, e.creation))
		lines.append({
			"kind": r.kind, "label": _(cd.KIND_LABELS[r.kind]), "direction": "in" if r.amount > 0 else "out",
			"voucher_type": r.voucher_type, "voucher_no": r.voucher_no, "amount": flt(r.amount, 2),
			"when": str(r.creation), "detail": cd.entry_detail(e) if e else (r.party or ""), "entry": e.name if e else None,
			"can_undo": bool(manager and e and e.entry_type in _UNDOABLE and not locked),
			# A recorded seller payment can be re-recorded with another method: a gun's by a System Manager, a
			# receipt's by a manager until a count relies on it (the server refuses it then, like an undo).
			"can_fix": _can_fix(e, manager, system_manager, locked),
			"acquisition": e.acquisition if e else None,
			"purchase_receipt": e.purchase_receipt if e else None,
		})
	opening = flt(last.carry_forward) if last else 0.0
	cash_in, cash_out = _in_out(lines)
	today = nowdate()
	closed_today = next((c for c in closes if c.business_date and str(c.business_date) == today and c.is_counted), None)
	bank_payouts = [{
		"entry": e.name, "acquisition": e.acquisition, "purchase_receipt": e.purchase_receipt, "serial": e.serial,
		"amount": flt(e.amount, 2), "method": e.payment_method, "detail": cd.entry_detail(e),
		"can_fix": _can_fix(e, manager, system_manager, cd.relied_on(counted, e.creation)),
		# A receipt's payment by bank is undone like a cash one (no count relies on a bank payment's own line, but the
		# same rule as the drawer lines keeps it simple: undoable until the next count).
		"can_undo": bool(manager and e.entry_type in _UNDOABLE and not cd.relied_on(counted, e.creation)),
	} for e in frappe.get_all(
		"Cash Drawer Entry", filters={"company": company, "entry_type": ["in", ["Payout", "Goods Payout"]], "status": "Posted",
			"payment_method": ["!=", "Cash"], "creation": [">=", today_start]},
		fields=["name", "acquisition", "serial", "amount", "payment_method", "entry_type", "reference", "memo",
			"expense_account", "receipt", "purchase_receipt", "customer", "creation"], order_by="creation")]
	return {
		"company": company, "is_manager": manager,
		"expected": exp.expected, "gl_balance": exp.gl, "pending": exp.pending, "history": exp.history, "opening": opening,
		"last_close": last, "closed_today": closed_today,
		"can_undo_close": bool(manager and last and last.is_counted),
		"first_count": not counted,
		"open_shifts": frappe.db.count("POS Opening Entry", {"company": company, "status": "Open", "docstatus": 1}),
		"bank_account": cd.resolve_bank(company),
		"threshold": cd.variance_threshold(), "expense_cap": cd.expense_cap(),
		"expense_accounts": [
			{"name": a, "label": frappe.db.get_value("Account", a, "account_name") or a}
			for a in cd.expense_account_list(company)],
		"lines": lines, "older_lines": older_lines, "older_net": older_net,
		"cash_in": cash_in, "cash_out": cash_out,
		# What neither the list nor the older lines explain (cash of a sale that merged after the last close...).
		"other": flt(exp.expected - opening - older_net - sum(flt(l["amount"]) for l in lines), 2),
		"bank_payouts": bank_payouts,
		"unpaid_goods_receipts": [{
			"purchase_receipt": r.name, "customer": r.customer, "amount": flt(r.amount, 2), "method": r.method,
			"posting_date": str(r.posting_date), "can_fix": manager} for r in (cd.receipts_to_pay(company) if manager else [])],
		"payout_methods": list(cd.PAYOUT_METHODS),
	}


def _in_out(lines: list[dict]) -> tuple[float, float]:
	"""Cash in and cash out (both positive) of a list of drawer lines, leaving _NOT_CASH kinds out."""
	moves = [flt(l["amount"], 2) for l in lines if l["kind"] not in _NOT_CASH]
	return flt(sum(a for a in moves if a > 0), 2), flt(-sum(a for a in moves if a < 0), 2)


def _can_fix(entry, manager: bool, system_manager: bool, locked: bool) -> bool:
	"""Whether this user may record a seller payment again with another method: a gun's by a System Manager (the server
	allows it after a count too), a receipt's by a manager while no count relies on it."""
	if not entry:
		return False
	return bool((system_manager and entry.entry_type == "Payout")
		or (manager and entry.entry_type == "Goods Payout" and not locked))


@frappe.whitelist()
def preview_close(company: str, counted) -> dict:
	"""What closing the day with this count would do, without recording anything.

	Args:
	    company: Company whose drawer is counted.
	    counted: The cash counted in the drawer, in dollars.

	Returns:
	    {expected, counted, variance, threshold, first_count, needs_reason}; variance is counted minus
	    expected (negative = short).
	"""
	frappe.only_for(COUNTER_ROLES)
	return cd.preview_close(_company(company), counted)


@frappe.whitelist()
def close_day(company: str, counted, reason: str | None = None, expected_seen=None) -> dict:
	"""Count the drawer and close the day.

	Closes the open POS shifts, books any difference from the books and carries the count forward as tomorrow's opening.
	A company's very first count sets the books to the physical drawer (against the bank) instead of booking a difference;
	a later count first books any correction made since to the days before the first count (a true-up, also against
	the bank), since that money was never in the drawer.

	Args:
	    company: Company whose drawer is counted.
	    counted: The cash counted in the drawer, in dollars (0 is allowed).
	    reason: Why the drawer is over or short; required when the difference reaches the limit set in
	        Cash Drawer Settings (it never blocks a close below it).
	    expected_seen: The expected cash the screen showed; the close is refused if the books moved since.

	Returns:
	    {close, expected, counted, variance, first_count, days_covered, covers_from, variance_je,
	    cutoff_je, trueup_je, pos_closing_entries, pos_errors}.
	"""
	frappe.only_for(COUNTER_ROLES)
	return cd.close_day(_company(company), counted, reason, expected_seen)


@frappe.whitelist()
def record_deposit(company: str, amount, reference: str | None = None) -> dict:
	"""Record cash taken to the bank: Dr Operating Bank / Cr Cash.

	Args:
	    company: Company whose drawer the cash left.
	    amount: Dollars deposited.
	    reference: Optional deposit slip or bank reference.

	Returns:
	    {entry, journal_entry, amount, company}.
	"""
	frappe.only_for(COUNTER_ROLES)
	return cd.record_deposit(_company(company), amount, reference)


@frappe.whitelist()
def record_expense(company: str, amount, expense_account: str, memo: str, receipt: str | None = None) -> dict:
	"""Record a small expense paid in cash: Dr the expense account / Cr Cash.

	Needs a memo (a receipt photo is optional), stays under the cash-expense limit and goes only to an allowed expense account.

	Args:
	    company: Company whose drawer paid.
	    amount: Dollars paid, tax included.
	    expense_account: One of the accounts in the expense list of the page.
	    memo: What the money was spent on.
	    receipt: The uploaded receipt's file URL, if any.

	Returns:
	    {entry, journal_entry, amount, company}.
	"""
	frappe.only_for(COUNTER_ROLES)
	return cd.record_expense(_company(company), amount, expense_account, memo, receipt)


@frappe.whitelist()
def record_from_bank(company: str, amount, reference: str | None = None) -> dict:
	"""Record cash taken out of the bank and put in the drawer: Dr Cash / Cr Operating Bank.

	Args:
	    company: Company whose drawer the cash went into.
	    amount: Dollars added to the drawer.
	    reference: Optional withdrawal slip or bank reference.

	Returns:
	    {entry, journal_entry, amount, company}.
	"""
	frappe.only_for(COUNTER_ROLES)
	return cd.record_from_bank(_company(company), amount, reference)


@frappe.whitelist()
def get_log(company: str | None = None, from_date: str | None = None, to_date: str | None = None) -> dict:
	"""Every cash movement in a date range, oldest first, with the drawer's running balance.

	Sales, refunds, seller payments, deposits, cash from the bank, expenses, count differences and the
	first count, plus register sales not posted yet. The balance starts from the books' cash before
	``from_date``.

	Args:
	    company: Company to show; defaults to the user's company.
	    from_date: First posting date (YYYY-MM-DD); defaults to 6 days before to_date.
	    to_date: Last posting date (YYYY-MM-DD); defaults to today.

	Returns:
	    {company, from_date, to_date, opening, closing, cash_in, cash_out, lines: [{posting_date, when,
	    kind, label, detail, by, voucher_type, voucher_no, amount, balance}]}.
	"""
	frappe.only_for(CASH_LOG_ROLES)
	company = _company(company)
	to_date = getdate(to_date or nowdate())
	from_date = getdate(from_date) if from_date else add_days(to_date, -6)
	if from_date > to_date:
		frappe.throw(_("The start date is after the end date."))
	if date_diff(to_date, from_date) > 366:
		frappe.throw(_("Show at most a year at a time."))
	opening, rows = cd.log_rows(company, cd.cash_account(company), from_date, to_date)
	balance, lines = flt(opening, 2), []
	for r in rows:
		amount = flt(r.amount, 2)
		balance = flt(balance + amount, 2)
		lines.append({
			"posting_date": str(r.posting_date), "when": str(r.creation), "kind": r.kind,
			"label": _(cd.KIND_LABELS[r.kind]), "detail": cd.entry_detail(r.entry) if r.entry else (r.party or ""),
			"by": r.get("owner"), "voucher_type": r.voucher_type, "voucher_no": r.voucher_no,
			"amount": amount, "balance": balance,
		})
	cash_in, cash_out = _in_out(lines)
	return {"company": company, "from_date": str(from_date), "to_date": str(to_date), "opening": flt(opening, 2),
		"closing": balance, "cash_in": cash_in, "cash_out": cash_out, "lines": lines}


@frappe.whitelist()
def undo_entry(entry: str) -> dict:
	"""Undo a cash entry (deposit, cash from the bank, expense or seller payment): its Journal Entry is cancelled.

	A seller payment for a Receive Goods receipt is undone like the others (the money is back where it came from); a
	trade-in ticket's payout is taken back by cancelling the ticket.

	Refused once a count was taken after the entry.

	Args:
	    entry: The Cash Drawer Entry name.

	Returns:
	    {entry, status}.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	if frappe.db.get_value("Cash Drawer Entry", entry, "entry_type") == "Trade-in Payout":
		# Its ticket stays submitted: cancelling the ticket is what takes the payout back.
		frappe.throw(_("This is what a register ticket paid the customer for trade-ins. Cancel that ticket instead."))
	return cd.void_entry(entry)


@frappe.whitelist()
def undo_close(close: str) -> dict:
	"""Undo the latest count of the drawer: its difference (or first-count) entry is cancelled.

	The day reads as not counted again; the POS shifts it closed stay closed.

	Args:
	    close: The Cash Drawer Close name; it must be the latest close of its company.

	Returns:
	    {close, status}.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	return cd.undo_close(close)


@frappe.whitelist()
def record_payout(acquisition: str, method: str) -> dict:
	"""Change the method of a gun's own seller payment (made the old way, before a pay-now buy became a receipt paid with
	it), or book one that was undone again.

	Books Dr Stock Adjustment / Cr Cash (or the bank) at the net last paid. A first payment is refused: a seller is paid
	with the gun's receipt (record_goods_payout). Changing a recorded payment is for a System Manager.

	Args:
	    acquisition: The FFL Acquisition name (a trade-in or other purchase from a private seller).
	    method: Cash, Zelle, Check or ACH.

	Returns:
	    {entry, journal_entry, amount, company}.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	return cd.record_payout(acquisition, method, replace=True)


@frappe.whitelist()
def mark_paid_outside(purchase_receipt: str, note: str) -> dict:
	"""Mark a Receive Goods purchase as paid to its private seller outside the system (a hand journal entry, a check
	booked in QuickBooks).

	Books nothing. The receipt leaves the list of purchases with no seller payment, and no seller payment can be
	recorded for it until the mark is cleared. Refused for a receipt paid in the system.

	Args:
	    purchase_receipt: The Purchase Receipt Receive Goods made for the purchase.
	    note: How the seller was paid (the journal entry, the check number); required.

	Returns:
	    {purchase_receipt, note, by, on}.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	return cd.mark_paid_outside(purchase_receipt, note)


@frappe.whitelist()
def clear_paid_outside(purchase_receipt: str) -> dict:
	"""Take back a "paid outside the system" mark: the purchase is listed and its seller payment can be recorded again.

	The mark is cleared on every receipt of the amendment line that carries it (an amendment answers to its cancelled
	original's mark), so clearing it on the amendment really clears it.

	Args:
	    purchase_receipt: The Purchase Receipt that carries the mark, or an amendment of it.

	Returns:
	    {purchase_receipt, cleared, receipts}; receipts are the ones the mark was taken off.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	return cd.clear_paid_outside(purchase_receipt)


@frappe.whitelist()
def record_goods_payout(purchase_receipt: str, method: str, reference: str | None = None) -> dict:
	"""Record how the private seller of a Receive Goods purchase was paid: change the method, record it again after an
	undo, or record the first payment of a receipt that never had one.

	Books Dr Stock Received But Not Billed / Cr Cash (or the bank) for what the receipt owes the seller, replacing the
	payment recorded, if any. Refused once a count was taken after that payment, for a receipt received before cash
	tracking started (the accountant's), for one marked as paid outside the system, when the receipt owes nothing
	now, and when the same goods are still paid on a cancelled receipt of this seller (a new receipt typed for goods
	already paid: undo that payment, or amend the cancelled receipt, instead); a refusal leaves the payment recorded
	as it was.

	Args:
	    purchase_receipt: The Purchase Receipt Receive Goods made for the purchase.
	    method: Cash, Zelle, Check or ACH.
	    reference: The check number, Zelle confirmation or ACH reference; required unless Cash.

	Returns:
	    {entry, journal_entry, amount, company, excluded}: the payment booked.
	"""
	frappe.only_for(CASH_MANAGER_ROLES)
	return cd.record_goods_payout(purchase_receipt, method, reference, replace=True)
