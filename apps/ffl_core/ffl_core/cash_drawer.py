"""Cash drawer: what the counter should be holding, what was counted, where the rest went.

The books carry one cash account per company (``Cash - <abbr>``). It is the drawer's
ledger, so the drawer's *expected* amount is simply that account's balance: whatever the
day's sales, refunds and payments did to it, plus the few things that take cash out of the
drawer without a sale. ERPNext's own closing figure is not used for cash: it only sees
invoices (a firearm sale is merged at once, a counter order is not a POS invoice at all)
and has no cash row on a day without a cash sale.

Everything that moves cash outside a sale is a ``Cash Drawer Entry`` and posts one Journal
Entry whose remark starts with a fixed word, so the weekly report and the accountant can
tell each kind apart:

========================  ======================================================================
CASH-DEPOSIT              Dr Operating Bank / Cr Cash
CASH-FROMBANK             Dr Cash / Cr Operating Bank (cash taken out of the bank to fill the drawer)
CASH-OWNER                an owner draw (Dr Due from Owner / Cr Cash): no longer made, old ones still read
CASH-EXPENSE              Dr <allowed expense account> / Cr Cash (receipt optional, capped)
CASH-PAYOUT               Dr Stock Adjustment / Cr Cash or Operating Bank (a seller paid for a gun)
CASH-TRADEIN              Dr Debtors (the customer) / Cr Cash or Operating Bank (a register ticket whose trade-ins
                          came to more than the sale: the store paid the customer the difference)
CASH-GOODS                Dr Stock Received But Not Billed / Cr Cash or Operating Bank (a private seller paid for what
                          Receive Goods took in on one Purchase Receipt; see ``goods_payout_due``)
CASH-VARIANCE             a count that differs from the books: Dr/Cr Cash Over/Short vs Cash
CASH-CUTOFF               the first count ever: sets the books to the physical drawer, once;
                          CASH-CUTOFF TRUE-UP: a later count books what corrections to the days before it changed
========================  ======================================================================

None of these touches Sales, Sales Tax Payable, COGS or Stock In Hand; only CASH-TRADEIN touches Debtors, and it nets
the customer's balance on that ticket to zero; CASH-GOODS nets the receipt's Stock Received But Not Billed to zero.

Expected cash = the Cash account balance (cancelled entries excluded, nothing created after
the count) + the cash of POS invoices that are submitted but not yet merged into a Sales
Invoice (the merge may be queued, so their cash is not in the ledger yet; it moves from
"pending" to the ledger without changing the total) - what corrections to the days before
the first count changed in the books since (never in the drawer; the next count books it to
the cut-off as a TRUE-UP).

``close_day`` counts the drawer: it closes the open POS shifts, books the difference and
carries the counted amount forward as the next opening. The 23:00 job (api.pos_close) never
counts: it leaves an *uncounted* close, books nothing, and the next real count absorbs
every day in between.
"""
from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, flt, get_datetime, getdate, now_datetime, nowdate

VARIANCE = "CASH-VARIANCE"
CUTOFF = "CASH-CUTOFF"
PAYOUT = "CASH-PAYOUT"
DEPOSIT = "CASH-DEPOSIT"
FROM_BANK = "CASH-FROMBANK"
OWNER = "CASH-OWNER"  # old owner draws only
EXPENSE = "CASH-EXPENSE"
TRADE_IN_PAYOUT = "CASH-TRADEIN"
GOODS_PAYOUT = "CASH-GOODS"
REMARK_PREFIXES = (VARIANCE, CUTOFF, PAYOUT, DEPOSIT, FROM_BANK, EXPENSE, OWNER, TRADE_IN_PAYOUT, GOODS_PAYOUT)

DEFAULT_THRESHOLD = 20.0
DEFAULT_EXPENSE_CAP = 200.0

# The accounts a petty expense may go to until the shop picks its own list in Cash
# Drawer Settings. Names without the company suffix: the suffix is the company's abbr.
DEFAULT_EXPENSE_ACCOUNTS = (
	"Print and Stationery",
	"Office Maintenance Expenses",
	"Freight and Forwarding Charges",
	"Postal Expenses",
	"Meals",
	"Entertainment Expenses",
	"Miscellaneous Expenses",
)

# Expense accounts that are never petty cash, whatever the settings list says.
_FORBIDDEN_NAMES = frozenset({
	"cost of goods sold", "stock adjustment", "salary", "payroll taxes", "tax expense",
	"write off", "round off", "bad debts", "depreciation", "exchange gain/loss",
	"expenses included in valuation", "cash over/short",
})
_FORBIDDEN_TYPES = frozenset({
	"Cost of Goods Sold", "Stock Adjustment", "Round Off", "Depreciation",
	"Expenses Included In Valuation", "Expenses Included In Asset Valuation",
})

# A deposit goes to the shop's own bank account, never to a processor's clearing account.
_CLEARING = re.compile(r"\b(clearing|payroc|woo|woocommerce|gunbroker)\b", re.I)

# How a seller was paid. Cash leaves the drawer; the rest leave the bank.
PAYOUT_METHODS = ("Cash", "Zelle", "Check", "ACH")

ENTRY_TYPES = ("Deposit", "From Bank", "Expense", "Payout", "Payout Adjustment", "Trade-in Payout", "Goods Payout")
# "Owner Draw" stays a valid stored value (with its withdrawer) so an old entry still reads and saves; none is made any more.

_ACCOUNT_SPECS = {
	"over_short": ("Cash Over/Short", "Indirect Expenses", ""),
	# Its own account, not Entertainment: meals are 50% deductible, entertainment is not.
	"meals": ("Meals", "Indirect Expenses", ""),
	"bank": ("Operating Bank", "Bank Accounts", "Bank"),
}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def _setting(field: str, default):
	"""A Cash Drawer Settings value, or ``default`` while the shop has never saved the page (a Single
	that was never saved reads 0 for a Currency field, which would silently mean "no limit")."""
	rows = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = 'Cash Drawer Settings' AND field = %s", (field,))
	return default if not rows or rows[0][0] in (None, "") else rows[0][0]


def variance_threshold() -> float:
	return flt(_setting("variance_threshold", DEFAULT_THRESHOLD))


def expense_cap() -> float:
	return flt(_setting("expense_cap", DEFAULT_EXPENSE_CAP))


def expense_account_list(company: str) -> list[str]:
	"""Accounts a petty expense may be booked to for this company: the shop's own list when
	it has rows for the company, else the standard list. Anything that is not a plain expense
	account (or is on the forbidden list) is dropped."""
	rows = frappe.get_all(
		"Cash Drawer Expense Account", filters={"parent": "Cash Drawer Settings"},
		pluck="account", order_by="idx")
	chosen = [a for a in rows if a and frappe.db.get_value("Account", a, "company") == company]
	if not chosen:
		abbr = _abbr(company)
		chosen = [f"{n} - {abbr}" for n in DEFAULT_EXPENSE_ACCOUNTS]
	return [a for a in chosen if expense_account_problem(a, company) is None]


def expense_account_problem(account: str, company: str | None = None) -> str | None:
	"""Why this account cannot take a petty expense, or None when it can."""
	info = frappe.db.get_value(
		"Account", account, ["account_name", "account_type", "root_type", "is_group", "company", "disabled"],
		as_dict=True)
	if not info:
		return _("{0} is not an account.").format(account)
	if company and info.company != company:
		return _("{0} belongs to another company.").format(account)
	if info.is_group or info.disabled or info.root_type != "Expense":
		return _("{0} is not an expense account you can post to.").format(account)
	name = (info.account_name or "").strip().lower()
	if name in _FORBIDDEN_NAMES or (info.account_type or "") in _FORBIDDEN_TYPES:
		return _("{0} cannot be paid from the cash drawer.").format(account)
	return None


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

def _abbr(company: str) -> str:
	abbr = frappe.get_cached_value("Company", company, "abbr")
	if not abbr:
		frappe.throw(_("Company {0} was not found.").format(company))
	return abbr


def cash_account(company: str) -> str:
	account = frappe.db.get_value("Company", company, "default_cash_account") or f"Cash - {_abbr(company)}"
	if not frappe.db.exists("Account", account):
		frappe.throw(_("{0} has no cash account ({1}). Ask your accountant to add it.").format(company, account))
	return account


def _bank_problem(account: str, company: str) -> str | None:
	"""Why this cannot be the shop's own bank account (None when it can): a real, ungrouped Bank account of the
	company that is not a processor's or marketplace's clearing account."""
	info = frappe.db.get_value(
		"Account", account, ["account_name", "account_type", "is_group", "company", "disabled"], as_dict=True)
	if (not info or info.company != company or info.account_type != "Bank" or info.is_group or info.disabled
			or _CLEARING.search(info.account_name or "")):
		return _("Cash goes to the shop's own bank account, not to {0}.").format(account)
	return None


def resolve_bank(company: str) -> str | None:
	"""The bank account cash goes to, without creating or changing anything: the company's usable default bank
	account, else the one its Zelle / ACH mode of payment maps to, else an existing ``Operating Bank``. None when
	the company has none yet."""
	candidates = [frappe.db.get_value("Company", company, "default_bank_account")]
	for mop in ("Zelle", "ACH"):
		candidates.append(frappe.db.get_value("Mode of Payment Account", {"parent": mop, "company": company}, "default_account"))
	candidates.append(f"Operating Bank - {_abbr(company)}")
	return next((a for a in candidates if a and _bank_problem(a, company) is None), None)


def ensure_accounts(company: str, *, create_bank: bool = True) -> dict:
	"""Create what the cash drawer posts to if the company lacks it. Idempotent. For the write paths, the patch
	and after_migrate only: a page load never calls it.

	The bank side is ``resolve_bank``; when the company has none, ``Operating Bank`` is created (``create_bank``
	False leaves that to the first cash entry: right after a migrate on a new site ``store_setup`` has not made the
	store's own bank account yet, and there must not be two). The company's default bank account is filled in when it
	is empty and a bank was found. Returns {over_short, cash, bank}; bank is None when there is none."""
	from ffl_core.setup.store_setup import _ensure_account

	config = {"COMPANY_NAME": company, "COMPANY_ABBR": _abbr(company)}
	out = {key: _ensure_account(config, name, parent, account_type)
		for key, (name, parent, account_type) in _ACCOUNT_SPECS.items() if key != "bank"}
	out["cash"] = cash_account(company)
	out["bank"] = resolve_bank(company)
	if not out["bank"] and create_bank:
		name, parent, account_type = _ACCOUNT_SPECS["bank"]
		out["bank"] = _ensure_account(config, name, parent, account_type)
	if out["bank"] and not frappe.db.get_value("Company", company, "default_bank_account"):
		frappe.db.set_value("Company", company, "default_bank_account", out["bank"])
	return out


def bank_account(company: str, accounts: dict | None = None) -> str:
	return (accounts or ensure_accounts(company))["bank"]


def deposit_account(company: str, requested: str | None = None, accounts: dict | None = None) -> str:
	"""The bank account a deposit goes to: the shop's own, never a processor's clearing account."""
	if not requested:
		return bank_account(company, accounts)
	problem = _bank_problem(requested, company)
	if problem:
		frappe.throw(problem)
	return requested


def over_short_account(company: str, accounts: dict | None = None) -> str:
	return (accounts or ensure_accounts(company))["over_short"]


# --------------------------------------------------------------------------- #
# Expected cash
# --------------------------------------------------------------------------- #

def drawer_expected(company: str, cutoff=None, account: str | None = None) -> frappe._dict:
	"""What the drawer should hold at ``cutoff`` (default: now): {account, gl, pending, history, expected, cutoff}.

	The ledger balance plus the cash of POS invoices that are submitted but not merged into a Sales Invoice yet
	(ERPNext posts nothing for them until the merge, and a merge of ten or more is queued), less ``history``: what
	corrections to the days before the first count changed in the books since (``history_change``). That money was
	never in the drawer; the next count books it to the cut-off."""
	cutoff = cutoff or now_datetime()
	account = account or cash_account(company)
	gl = frappe.db.sql(
		"""SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry`
		WHERE company = %s AND account = %s AND is_cancelled = 0 AND creation <= %s""",
		(company, account, cutoff))[0][0]
	pending = flt(sum(r.amount for r in pending_pos_rows(company, account, created_before=cutoff)), 2)
	history = history_change(company, account)
	return frappe._dict(account=account, gl=flt(gl, 2), pending=pending, history=history,
		expected=flt(flt(gl) + pending - history, 2), cutoff=cutoff)


# The Journal Entries that book a history change to the cut-off (``close_day``; one made by hand counts too when its
# remark says so). A literal inside SQL that also takes parameters: the percent signs are doubled.
_TRUEUP_JES = "SELECT name FROM `tabJournal Entry` WHERE user_remark LIKE 'CASH-CUTOFF%%TRUE-UP%%'"


def cutoff_close(company: str) -> frappe._dict | None:
	"""The company's first count (the cut-off) that still stands, or None before it."""
	rows = frappe.get_all("Cash Drawer Close", filters={"company": company, "status": "Closed", "is_cutoff": 1},
		fields=_CLOSE_FIELDS, order_by="business_date asc, creation asc", limit=1)
	return rows[0] if rows else None


def history_change(company: str, account: str, first: frappe._dict | None = None) -> float:
	"""How much the books' history changed after the first count and is not booked to the cut-off yet (
	pre-cut-off history): history now - what the first count saw + the true-ups already booked. 0 before the
	first count.

	History is every cash line the first count saw (made before it, a register ticket rung up before it, merged or not)
	plus any line dated before the cut-off day that was added later. A state, not a window: a cancelled, amended,
	added or reposted line is caught whenever it happened, and nothing is counted twice."""
	first = first or cutoff_close(company)
	if not first:
		return 0.0
	values = {"c": company, "a": account, "t": first.counted_at, "d": first.business_date}
	history = frappe.db.sql(
		"""SELECT IFNULL(SUM(g.debit - g.credit), 0) FROM `tabGL Entry` g
		WHERE g.company = %(c)s AND g.account = %(a)s AND g.is_cancelled = 0
			AND NOT (g.voucher_type = 'Journal Entry' AND g.voucher_no IN (
				SELECT j.name FROM `tabJournal Entry` j WHERE j.company = %(c)s AND j.user_remark LIKE 'CASH-CUTOFF%%'))
			AND (g.creation <= %(t)s OR g.posting_date < %(d)s
				OR (g.voucher_type = 'Sales Invoice' AND g.voucher_no IN (
					SELECT i.consolidated_invoice FROM `tabPOS Invoice` i WHERE i.company = %(c)s AND i.docstatus = 1
						AND i.creation <= %(t)s AND IFNULL(i.consolidated_invoice, '') != '')))""", values)[0][0]
	history = flt(history) + sum(flt(r.amount) for r in pending_pos_rows(company, account, created_before=first.counted_at))
	booked = frappe.db.sql(
		f"""SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE company = %(c)s AND account = %(a)s
		AND is_cancelled = 0 AND voucher_type = 'Journal Entry' AND voucher_no IN ({_TRUEUP_JES})""", values)[0][0]
	# lazy: a cut-off-day counter sale (dated the cut-off day, made before the count) that is reposted later looks like
	# a change, and so does a shift left open over the first count and merged with later tickets; both are corner
	# cases of the first day only -- tag such lines at the first count if they ever show up.
	return flt(history - flt(first.expected) + flt(booked), 2)


def _history_lines(company: str, account: str, first: frappe._dict, since) -> list[tuple[str, float]]:
	"""Best-effort list of the vouchers behind a history change since ``since``, for the true-up's remark: lines dated
	before the cut-off day made since then, a cancellation's reversal included (it carries the cancelled line's
	date and the opposite amount)."""
	rows = frappe.db.sql(
		"""SELECT voucher_no, SUM(debit - credit) FROM `tabGL Entry`
		WHERE company = %s AND account = %s AND posting_date < %s AND creation > %s
		GROUP BY voucher_no HAVING ABS(SUM(debit - credit)) >= 0.005 ORDER BY MIN(creation)""",
		(company, account, first.business_date, since or first.counted_at))
	return [(r[0], flt(r[1], 2)) for r in rows]


def _post_trueup(company: str, amount: float, close_name: str, accounts: dict, cash: str, since) -> str:
	"""Book a history change to the cut-off: Dr the bank / Cr Cash (or the other way), dated the cut-off day so every
	day after it still opens at the count before; on the count day when that period is frozen. Never blocks a count."""
	from ffl_core.api.cost_correction import _reject_if_period_frozen

	first = cutoff_close(company)
	posting_date, frozen_note = first.business_date, ""
	muted, frappe.flags.mute_messages = frappe.flags.mute_messages, True  # a frozen period is an answer here, not an error to show
	try:
		_reject_if_period_frozen(company, first.business_date, what="a cash drawer entry")
	except frappe.ValidationError:
		posting_date, frozen_note = nowdate(), " (posted on the count day: the cut-off date is frozen)"
	finally:
		frappe.flags.mute_messages = muted
	changes = _history_lines(company, cash, first, since)
	listed = "; ".join(f"{name} {amt:+,.2f}" for name, amt in changes[:20]) + (
		f"; {len(changes) - 20} more" if len(changes) > 20 else "")
	if not changes or abs(sum(a for _n, a in changes) - amount) >= 0.005:
		listed = f"{listed}; list incomplete: ledger rows reposted or deleted" if listed else "list incomplete"
	bank = bank_account(company, accounts)
	lines = [(bank, amount, 0), (cash, 0, amount)] if amount > 0 else [(cash, -amount, 0), (bank, 0, -amount)]
	return _post_je(company, lines, f"{CUTOFF} TRUE-UP {close_name}: {listed}{frozen_note}", posting_date, source=close_name)


def ledger_sum(company: str, account: str, *, to_date=None, before=None, from_date=None) -> float:
	"""The Cash account's net (debit − credit) by posting date: before / from / to. The one copy: the log's and the
	weekly report's opening and movement."""
	conditions, values = ["company = %s", "account = %s", "is_cancelled = 0"], [company, account]
	if before:
		conditions.append("posting_date < %s")
		values.append(before)
	if to_date:
		conditions.append("posting_date <= %s")
		values.append(to_date)
	if from_date:
		conditions.append("posting_date >= %s")
		values.append(from_date)
	return flt(frappe.db.sql(
		f"SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE {' AND '.join(conditions)}", values)[0][0], 2)


# --------------------------------------------------------------------------- #
# Closes
# --------------------------------------------------------------------------- #

_CLOSE_FIELDS = [
	"name", "company", "business_date", "is_counted", "counted", "expected", "opening_amount", "variance",
	"carry_forward", "is_cutoff", "reason", "counted_by", "counted_at", "closed_at", "days_covered", "covers_from",
	"variance_je", "cutoff_je", "trueup_je", "pos_closing_entries", "status", "creation", "modified",
]
_CLAIM_MINUTES = 5


def active_closes(company: str, **filters) -> list[frappe._dict]:
	"""This company's closes that have not been undone, newest first."""
	return frappe.get_all(
		"Cash Drawer Close", filters={"company": company, "status": "Closed", **filters},
		fields=_CLOSE_FIELDS, order_by="business_date desc, creation desc")


def last_close(company: str, exclude: str | None = None) -> frappe._dict | None:
	for row in active_closes(company):
		if row.name != exclude:
			return row
	return None


def _fmt(amount) -> str:
	return f"${flt(amount):,.2f}"


def money(value, what: str, *, allow_zero: bool = False) -> float:
	"""A strict amount: a number, to the cent, positive (or zero when allowed)."""
	try:
		amount = flt(float(value), 2)
	except (TypeError, ValueError):
		amount = float("nan")
	if amount != amount or abs(amount) == float("inf"):
		frappe.throw(_("{0} must be a number.").format(what))
	if amount < 0:
		frappe.throw(_("{0} cannot be negative.").format(what))
	if amount == 0 and not allow_zero:
		frappe.throw(_("{0} must be more than zero.").format(what))
	return amount


def lock_drawer(company: str, wait: int | None = None) -> None:
	"""The cash drawer's lock: one close or cash entry at a time per company (a locking read on the Company row; the
	rest wait). ``wait`` bounds the wait in seconds (MariaDB ``FOR UPDATE WAIT n``) and turns a drawer that stays busy
	into a plain refusal. A Receive Goods purchase that pays its seller takes it before it writes any stock, the same
	order as a count (drawer first, then the stock rows its register shifts touch), so the two never deadlock.

	lazy: a negative register ticket (its payout after the POS Invoice's stock) still takes stock rows first and this
	lock second, so it and a Receive Goods purchase of the same SKU at the same moment can deadlock (MariaDB rolls one
	back, a "busy" refusal); upgrade path: the register's payout takes this lock in before_submit."""
	if not wait or frappe.db.db_type != "mariadb":
		frappe.db.get_value("Company", company, "name", for_update=True)
		return
	try:
		frappe.db.sql(f"SELECT name FROM `tabCompany` WHERE name = %s FOR UPDATE WAIT {cint(wait)}", (company,))
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		# frappe.db.sql wraps MariaDB's lock-wait timeout (1205) and deadlock (1213) in these; is_timedout() only
		# recognises the raw driver error, never the wrapped one.
		frappe.throw(_("The cash drawer is busy (a count or another cash entry is being saved). Try again in a moment."))


def _post_je(company: str, lines: list[tuple], remark: str, posting_date: str | None = None,
		source: str | None = None) -> str:
	"""Submit a Journal Entry of (account, debit, credit) lines, each optionally followed by a dict of further row
	fields (a party on a Debtors line). P&L rows carry the company's cost center. ``source`` -- the Cash Drawer Entry
	or Cash Drawer Close it books -- is stamped on the entry (``ffl_cash_drawer_source``), so it is cancelled only from
	the Cash Drawer page (``refuse_drawer_je_cancel``)."""
	from ffl_core.api.cost_correction import _reject_if_period_frozen

	posting_date = posting_date or nowdate()
	_reject_if_period_frozen(company, posting_date, what="a cash drawer entry")
	cost_center = frappe.get_cached_value("Company", company, "cost_center")
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = posting_date
	je.user_remark = remark
	je.remark = remark
	if source:
		je.ffl_cash_drawer_source = source
	for account, debit, credit, *extra in lines:
		row = {"account": account, "debit_in_account_currency": flt(debit, 2), "credit_in_account_currency": flt(credit, 2)}
		if frappe.db.get_value("Account", account, "root_type") in ("Expense", "Income"):
			row["cost_center"] = cost_center
		if extra:
			row.update(extra[0])
		je.append("accounts", row)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def _cancel_je(name: str | None) -> None:
	"""Cancel a journal entry the cash drawer made. The one sanctioned way (``refuse_drawer_je_cancel``): the callers
	(undoing an entry, undoing a count) keep the drawer entry's status and the "not after a count" rule."""
	if name and frappe.db.get_value("Journal Entry", name, "docstatus") == 1:
		je = frappe.get_doc("Journal Entry", name)
		je.flags.ignore_permissions = True
		je.flags.cash_drawer_cancel = True
		je.cancel()


def drawer_record_of(je: str, source: str | None) -> frappe._dict | None:
	"""The cash drawer record a journal entry books -- its stamp (``ffl_cash_drawer_source``), and only when that
	record really points back to this entry (a Cash Drawer Entry's journal_entry, a Close's variance / cut-off /
	true-up); a stale or stray stamp locks nothing. {kind: "entry" | "close", ...} or None."""
	if not source:
		return None
	lookups = [("entry", "Cash Drawer Entry", ["name", "status", "entry_type", "invoice", "journal_entry", "company",
		"creation"]), ("close", "Cash Drawer Close", ["name", "status", "is_counted", "company", "variance_je", "cutoff_je",
		"trueup_je"])]
	if source.startswith("CDC-"):  # the naming series says which (only an order: both are still checked)
		lookups.reverse()
	for kind, doctype, fields in lookups:
		row = frappe.db.get_value(doctype, source, fields, as_dict=True)
		if row and je in ((row.journal_entry,) if kind == "entry" else (row.variance_je, row.cutoff_je, row.trueup_je)):
			return frappe._dict(row, kind=kind)
	return None


def _drawer_refusal(je: str, rec: frappe._dict) -> str | None:
	"""Why a journal entry the drawer made cannot be cancelled, reversed or amended outside the Cash Drawer page,
	naming the way that works; None for a leftover (its entry already undone, its count already taken back)."""
	if rec.kind == "entry":
		if rec.status != "Posted":
			return None
		if rec.entry_type == "Trade-in Payout":
			return _("{0} is the payout of register ticket {1}. Cancel that ticket instead (cancel its merged invoice first, "
				"if it was merged): its payout goes with it.").format(je, rec.invoice)
		if rec.entry_type in _PAYOUT_TYPES:
			return _("{0} is the seller payment {1}. A System Manager changes or undoes it on the Cash Drawer page.").format(
				je, rec.name)
		if counted_since(rec.company, rec.creation):
			return _("{0} was booked by the cash drawer ({1}), and a count was taken after it. Ask a manager to undo that "
				"count on the Cash Drawer page first, or tell your accountant.").format(je, rec.name)
		return _("{0} was booked by the cash drawer ({1}). Undo it on the Cash Drawer page instead.").format(je, rec.name)
	if rec.status != "Closed":
		return None
	latest = last_close(rec.company)
	if not cint(rec.is_counted) or (latest and latest.name != rec.name):
		return _("{0} was booked by the cash drawer close {1}, which cannot be undone any more (only the latest counted day "
			"can). Tell your accountant.").format(je, rec.name)
	return _("{0} was booked by the cash drawer count {1}. Undo that count on the Cash Drawer page instead.").format(je, rec.name)


def refuse_drawer_je_cancel(doc, method=None) -> None:
	"""Journal Entry before_cancel: a journal entry the cash drawer made is cancelled only through the Cash Drawer
	page's own paths (``_cancel_je``): Undo on the entry, a System Manager's fix of a seller payment, undoing the count,
	cancelling a trade-in ticket. Cancelled anywhere else (Desk, an import, a script) it would leave the entry Posted
	with no books behind it and quietly change what a count relied on (10-03: an old seller payment switched from Cash
	to Zelle in Desk); the refusal names the way that works (``_drawer_refusal``). Every other journal entry is left
	alone, and so is a leftover. A correction recipe in bench console that must cancel one sets
	``je.flags.cash_drawer_cancel = True`` (docs/cash-drawer.md)."""
	if doc.flags.cash_drawer_cancel:
		return
	rec = drawer_record_of(doc.name, doc.get("ffl_cash_drawer_source"))
	refusal = rec and _drawer_refusal(doc.name, rec)
	if refusal:
		frappe.throw(refusal)


def refuse_drawer_je_copies(doc, method=None) -> None:
	"""Journal Entry validate: Desk's "Reverse Journal Entry" of one the drawer made would undo it without cancelling
	it, and an Amend of one the page cancelled would put it back with nothing watching (the stamp is not copied). Both
	are refused while the drawer record stands (``_drawer_refusal``); an amendment of a cancelled one always, since its
	entry or count was taken back on purpose."""
	if doc.flags.cash_drawer_cancel:
		return
	original = doc.get("reversal_of")
	if original:
		rec = drawer_record_of(original, frappe.db.get_value("Journal Entry", original, "ffl_cash_drawer_source"))
		refusal = rec and _drawer_refusal(original, rec)
		if refusal:
			frappe.throw(refusal)
	amended = doc.get("amended_from")
	if amended and frappe.db.get_value("Journal Entry", amended, "ffl_cash_drawer_source"):
		frappe.throw(_("{0} was booked by the cash drawer and taken back on purpose. Record it again on the Cash Drawer "
			"page instead of amending it.").format(amended))


def stamp_draft_payment(doc, method=None) -> None:
	"""POS Invoice validate: Take Payment on a draft (a part payment, or the tender taken before the 4473 clears) sets
	``ffl_cash_taken_at``, and from then on the drawer counts the draft's cash (``pending_pos_rows``). The browser only
	asks for the stamp; the time is the server's. A screen loaded before the stamp never clears it.
	lazy: a draft that already held cash before this stamp existed counts only after its next Take Payment or its
	submit; stamp such drafts by hand (or with a patch) if one is found."""
	if doc.docstatus != 0 or not doc.has_value_changed("ffl_cash_taken_at"):
		return
	before = doc.get_doc_before_save()
	doc.ffl_cash_taken_at = now_datetime() if doc.get("ffl_cash_taken_at") else (before and before.get("ffl_cash_taken_at"))


def stamp_cash_row(closing, *, expected: float, opening: float, counted: float | None, is_counted: bool) -> None:
	"""Put the drawer's figures on the POS Closing Entry's Cash row (adding the row when the day had no cash
	sale). ERPNext's ``difference`` is counted minus (opening + expected); here expected is already the whole
	drawer, so it is counted minus expected.

	``counted`` None = nobody counted: a closing amount somebody typed on the row is kept (a close from ERPNext's
	own POS screen), otherwise it repeats the expected figure. The row is created only when missing."""
	row = next((r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash"), None)
	if row is None:
		row = closing.append("payment_reconciliation", {"mode_of_payment": "Cash"})
	if counted is None:
		counted = flt(row.closing_amount) or expected
	row.opening_amount = flt(opening, 2)
	row.expected_amount = flt(expected, 2)
	row.closing_amount = flt(counted, 2)
	row.difference = flt(counted - expected, 2)
	closing.ffl_cash_counted = 1 if is_counted else 0
	closing.flags.ffl_cash_stamped = True


def carry_forward_opening(company: str) -> float:
	"""The drawer's opening: what the last close carried forward (its counted cash, or the ledger
	balance when that close was not counted); 0 before the first close."""
	last = last_close(company)
	return flt(last.carry_forward) if last else 0.0


def _shift_cash(closing) -> float:
	"""This shift's own cash as ERPNext computed it (its invoices' cash less change): 0 when the shift sold none."""
	row = next((r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash"), None)
	return flt(row.expected_amount) if row else 0.0


def stamp_closing_entry(closing, method=None) -> None:
	"""POS Closing Entry before_insert: every closing carries a Cash row, and none of them is a count.

	* ``flags.ffl_cash_drawer`` (the 23:00 job's last shift of a company): the drawer's figures from the ledger.
	* ``flags.ffl_cash_zero`` (the earlier shifts of that run): a zero row, so ERPNext's sums are not multiplied.
	* anything else, a close from ERPNext's own POS screen, one shift at a time: the shift's own cash (ERPNext's figure
	  where it has a Cash row, else 0), opening 0, and what the cashier typed stays.

	A closing made by ``close_day`` stamped its own. The Cash Drawer Close is the only record of a count."""
	if closing.flags.ffl_cash_stamped or not closing.company:
		return
	if closing.flags.ffl_cash_zero:
		stamp_cash_row(closing, expected=0, opening=0, counted=0, is_counted=False)
	elif closing.flags.ffl_cash_drawer:
		try:
			exp = drawer_expected(closing.company)
		except frappe.ValidationError:
			return  # a company with no cash account: nothing to stamp
		stamp_cash_row(
			closing, expected=exp.expected, opening=carry_forward_opening(closing.company), counted=None, is_counted=False)
	else:
		stamp_cash_row(closing, expected=_shift_cash(closing), opening=0, counted=None, is_counted=False)


def close_open_shifts(company: str, *, expected: float, opening: float) -> tuple[list[str], list[str], str | None]:
	"""Close every open POS shift of the company, the way the 23:00 job does; the latest shift carries the drawer's
	figures on its Cash row (as not counted yet), the others a zero row. A shift that cannot close is reported, never raised: the cash
	count does not wait for it. Returns (POS Closing Entry names, error messages, the name of the closing that
	carries the drawer's figures, if it closed).

	ERPNext's merge of a closing's invoices COMMITS the open transaction when it finishes and
	ROLLS IT ALL BACK when it fails (outside tests), so nothing this request wrote before the
	call is guaranteed to survive it, and the company lock is gone after it. ``close_day`` therefore
	does its writes after this call and takes the lock again."""
	openings = frappe.db.sql_list(
		"""SELECT poe.name FROM `tabPOS Opening Entry` poe
		LEFT JOIN `tabPOS Closing Entry` pce ON pce.pos_opening_entry = poe.name AND pce.docstatus IN (0, 1)
		WHERE poe.docstatus = 1 AND poe.status = 'Open' AND pce.name IS NULL AND poe.company = %s
		ORDER BY poe.period_start_date, poe.creation""", (company,))
	names, errors, latest_name = [], [], None
	# ERPNext checks the SESSION user's rights to the POS Profile while it builds a closing; a cashier may not
	# have them, and the 23:00 job runs as Administrator too. The shift is closed for the store, not by the
	# cashier: who counted is recorded on the Cash Drawer Close.
	#
	# Only the session's user is swapped, never ``frappe.set_user``: that also overwrites the session id with
	# the user name, and the caller's real session would be lost when the request ends.
	session = frappe.local.session
	counter_user = session.user
	session.user = "Administrator"
	try:
		for n, opening_name in enumerate(openings):
			try:
				names.append(_close_one_shift(opening_name, expected, opening, latest=n == len(openings) - 1))
				if n == len(openings) - 1:
					latest_name = names[-1]
			except Exception as e:
				errors.append(f"{opening_name}: {e}")
	finally:
		session.user = counter_user
	return names, errors, latest_name


def _close_one_shift(opening_name: str, expected: float, opening: float, latest: bool) -> str:
	from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

	savepoint = "ffl_cash_close_shift"
	frappe.db.savepoint(savepoint)
	try:
		closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", opening_name))
		for p in closing.payment_reconciliation:
			p.closing_amount = flt(p.expected_amount)  # card and other tenders: trusted, as at 23:00
			p.difference = 0
		# Stamped as NOT counted, with closing = expected: the count is written onto it only once the Cash Drawer
		# Close exists (``_mark_counted``), so a failure in between never leaves a closing claiming a count.
		if latest:
			stamp_cash_row(closing, expected=expected, opening=opening, counted=expected, is_counted=False)
		else:
			stamp_cash_row(closing, expected=0, opening=0, counted=0, is_counted=False)
		closing.flags.ignore_permissions = True
		closing.insert()
		closing.submit()
		return closing.name
	except Exception:
		trace = frappe.get_traceback()
		try:
			frappe.db.rollback(save_point=savepoint)
		except Exception:
			pass  # ERPNext's merge already rolled the whole transaction back: nothing left to rewind
		frappe.log_error(title=f"Cash close: POS shift {opening_name} did not close", message=trace)
		raise


def _uncounted_since_last_count(company: str, exclude: str | None = None) -> list[frappe._dict]:
	out = []
	for row in active_closes(company):
		if row.name == exclude:
			continue
		if row.is_counted:
			break
		out.append(row)
	return out


def preview_close(company: str, counted) -> dict:
	"""What closing now would do, without writing anything."""
	counted = money(counted, _("Counted cash"), allow_zero=True)
	exp = drawer_expected(company)
	first = not active_closes(company, is_counted=1)
	variance = flt(counted - exp.expected, 2)
	threshold = variance_threshold()
	return {
		"expected": exp.expected, "counted": counted, "variance": variance, "threshold": threshold,
		"first_count": first,
		"needs_reason": (not first) and abs(variance) >= threshold,
	}


def _day_rows(company: str, day: str) -> list[frappe._dict]:
	"""The close rows of that day that hold it: finished (Closed) or being made right now (Closing)."""
	return frappe.get_all(
		"Cash Drawer Close", filters={"company": company, "business_date": day, "status": ["in", ["Closed", "Closing"]]},
		fields=_CLOSE_FIELDS, order_by="creation desc")


def _mark_counted(names: list[str], latest: str | None, counted: float, expected: float, close_name: str) -> None:
	"""The Cash Drawer Close is written: now the POS closings may say a count was taken, and which."""
	for name in names:
		frappe.db.set_value("POS Closing Entry", name, {"ffl_cash_counted": 1, "ffl_cash_drawer_close": close_name}, update_modified=False)
	row = frappe.db.get_value("POS Closing Entry Detail", {"parent": latest, "mode_of_payment": "Cash"}, "name") if latest else None
	if row:
		frappe.db.set_value("POS Closing Entry Detail", row, {
			"closing_amount": flt(counted, 2), "difference": flt(counted - expected, 2)}, update_modified=False)


def close_day(company: str, counted, reason: str | None = None, expected_seen=None) -> dict:
	"""Count the drawer for ``company`` today. See the module docstring.

	Every refusal comes before any POS shift is merged, and the day is claimed (a Cash Drawer Close in status
	Closing, unique per company and day) before the first merge, which is the point where ERPNext releases the company
	lock by committing."""
	counted = money(counted, _("Counted cash"), allow_zero=True)
	reason = (reason or "").strip()
	accounts = ensure_accounts(company)
	lock_drawer(company)

	today = nowdate()
	rows = _day_rows(company, today)
	done = next((r for r in rows if r.status == "Closed" and r.is_counted), None)
	if done:
		frappe.throw(_("Today's cash was already counted at {0} ({1}). A manager can undo that count if it was wrong.").format(
			get_datetime(done.counted_at).strftime("%-I:%M %p"), _fmt(done.counted)))
	claim = next((r for r in rows if r.status == "Closing"), None)
	if claim and claim.counted_by != frappe.session.user and get_datetime(claim.modified) > add_to_date(
			now_datetime(), minutes=-_CLAIM_MINUTES):
		frappe.throw(_("{0} is closing the day right now. Reload the page in a few minutes to see their count.").format(
			claim.counted_by))

	from ffl_core.api.cost_correction import _reject_if_period_frozen
	_reject_if_period_frozen(company, today, what="a cash count")

	cutoff = now_datetime()
	exp = drawer_expected(company, cutoff, account=accounts.get("cash"))
	if expected_seen not in (None, "") and abs(flt(expected_seen) - exp.expected) >= 0.005:
		frappe.throw(_("The cash the drawer should hold changed while you were counting (now {0}). "
			"Check the new figure on screen, then confirm again.").format(_fmt(exp.expected)))

	first = not active_closes(company, is_counted=1)
	variance = flt(counted - exp.expected, 2)
	if not first and abs(variance) >= variance_threshold() and not reason:
		frappe.throw(_("The drawer is {0} {1} the books. Write a short reason, then close the day.").format(
			_fmt(abs(variance)), _("over") if variance > 0 else _("short of")))

	existing = next((r for r in rows if r.status == "Closed"), None) or claim  # the day's uncounted close, or our claim
	earlier = _uncounted_since_last_count(company, exclude=existing.name if existing else None)
	previous = last_close(company, exclude=existing.name if existing else None)
	opening = flt(previous.carry_forward) if previous else 0.0

	# Claim the day: from here on a second close of the same day stops at the unique key.
	close = frappe.get_doc("Cash Drawer Close", existing.name) if existing else frappe.new_doc("Cash Drawer Close")
	close.update({"company": company, "business_date": today, "status": "Closing", "counted_by": frappe.session.user,
		"counted_at": cutoff})
	close.flags.ignore_permissions = True
	if existing:
		close.save()
	else:
		close.insert()

	pos_names, pos_errors, latest_name = close_open_shifts(company, expected=exp.expected, opening=opening)

	# The merge above may have committed or rolled back (and with a rollback our claim is gone): take the lock again,
	# look once more, then write.
	lock_drawer(company)
	accounts = ensure_accounts(company)
	rows = _day_rows(company, today)
	if any(r.status == "Closed" and r.is_counted for r in rows):
		frappe.throw(_("Someone else just counted today's cash. Reload the page to see their count."))
	mine = next((r for r in rows if r.status == "Closing"), None) or next((r for r in rows if r.status == "Closed"), None)
	close = frappe.get_doc("Cash Drawer Close", mine.name) if mine else frappe.new_doc("Cash Drawer Close")
	close.update({
		"company": company, "business_date": today, "status": "Closing", "is_counted": 1, "counted": counted,
		"expected": exp.expected, "opening_amount": opening, "variance": variance, "carry_forward": counted,
		"is_cutoff": 1 if first else 0, "reason": reason or None, "counted_by": frappe.session.user,
		"counted_at": cutoff, "days_covered": len(earlier) + 1,
		"covers_from": earlier[-1].business_date if earlier else None,
		"pos_closing_entries": ", ".join(pos_names) or None,
	})
	close.flags.ignore_permissions = True
	if mine:
		close.save()
	else:
		close.insert()

	cash = exp.account
	remark_ref = pos_names[0] if pos_names else close.name
	if not first and abs(exp.history) >= 0.01:
		# Corrections to the days before the first count: never in the drawer, so booked to the cut-off, before the
		# difference (which is then counted against an expected figure that already leaves them out).
		since = next(iter(active_closes(company, is_counted=1)), None)
		close.trueup_je = _post_trueup(company, exp.history, close.name, accounts, cash, since.closed_at if since else None)
	if first:
		# The books start here: set them to the physical drawer once, against the bank, not as a loss.
		gap = flt(exp.expected - counted, 2)
		if abs(gap) >= 0.01:
			bank = bank_account(company, accounts)
			lines = [(bank, gap, 0), (cash, 0, gap)] if gap > 0 else [(cash, -gap, 0), (bank, 0, -gap)]
			close.cutoff_je = _post_je(company, lines, f"{CUTOFF} {remark_ref}: drawer set to the first count",
				source=close.name)
	elif abs(variance) >= 0.01:
		over_short = over_short_account(company, accounts)
		amount = abs(variance)
		lines = [(over_short, amount, 0), (cash, 0, amount)] if variance < 0 else [(cash, amount, 0), (over_short, 0, amount)]
		close.variance_je = _post_je(company, lines, f"{VARIANCE} {remark_ref}", source=close.name)
	close.status = "Closed"
	close.closed_at = now_datetime()
	close.save()
	_mark_counted(pos_names, latest_name, counted, exp.expected, close.name)
	return {
		"close": close.name, "expected": exp.expected, "counted": counted, "variance": variance,
		"first_count": first, "days_covered": close.days_covered, "covers_from": close.covers_from,
		"variance_je": close.variance_je, "cutoff_je": close.cutoff_je, "trueup_je": close.trueup_je,
		"pos_closing_entries": pos_names, "pos_errors": pos_errors,
	}


def ensure_uncounted_close(company: str) -> str | None:
	"""The 23:00 job: today has no counted close, so leave an uncounted one. It books nothing; the
	next count covers it. Returns its name, or None when today already has a close."""
	lock_drawer(company)
	today = nowdate()
	if _day_rows(company, today):
		return None
	try:
		exp = drawer_expected(company)
	except frappe.ValidationError:
		return None
	close = frappe.get_doc({
		"doctype": "Cash Drawer Close", "company": company, "business_date": today, "status": "Closed",
		"is_counted": 0, "counted": 0, "expected": exp.expected, "opening_amount": carry_forward_opening(company),
		"variance": 0, "carry_forward": exp.expected, "closed_at": now_datetime(), "days_covered": 1,
	})
	close.flags.ignore_permissions = True
	close.insert()
	return close.name


def undo_close(name: str) -> dict:
	"""Take back the latest count: its variance, cut-off and true-up entries are cancelled and the day reads as
	not counted. The POS shifts it closed stay closed."""
	company = frappe.db.get_value("Cash Drawer Close", name, "company")
	if not company:
		frappe.throw(_("{0} was not found.").format(name))
	lock_drawer(company)
	row = frappe.db.get_value("Cash Drawer Close", name, ["company", "status", "is_counted"], as_dict=True, for_update=True)
	if row.status != "Closed":
		frappe.throw(_("That count was already undone."))
	if not row.is_counted:
		frappe.throw(_("That day was never counted, so there is nothing to undo."))
	latest = last_close(row.company)
	if latest and latest.name != name:
		frappe.throw(_("Only the latest close can be undone, and a later day has been closed since."))
	close = frappe.get_doc("Cash Drawer Close", name)
	_cancel_je(close.variance_je)
	_cancel_je(close.cutoff_je)
	_cancel_je(close.trueup_je)
	close.status = "Undone"
	close.flags.ignore_permissions = True
	close.save()
	return {"close": name, "status": "Undone"}


# --------------------------------------------------------------------------- #
# Cash Drawer Entry (deposit / cash from the bank / expense / seller payout)
# --------------------------------------------------------------------------- #

_IMMUTABLE = (
	"entry_type", "company", "amount", "posting_date", "journal_entry", "expense_account", "withdrawer",
	"memo", "reference", "receipt", "acquisition", "serial", "payment_method", "debit_account", "credit_account",
	"status", "invoice", "customer", "purchase_receipt",
)
_NUMBERS = frozenset({"amount"})
_RECEIPT_MAX_AGE_DAYS = 1


def _unchanged(field: str, before, after) -> bool:
	if field in _NUMBERS:
		return abs(flt(before) - flt(after)) < 0.005
	return str(before or "").strip() == str(after or "").strip()


def _receipt_file(doc) -> str:
	"""The File document behind an expense's receipt: uploaded by the person recording it a moment ago and not
	attached to anything else. Returns its name."""
	url = (doc.receipt or "").strip()
	oldest = add_to_date(now_datetime(), days=-_RECEIPT_MAX_AGE_DAYS)
	for f in frappe.get_all(
			"File", filters={"file_url": url}, order_by="creation desc",
			fields=["name", "owner", "creation", "attached_to_doctype", "attached_to_name"]):
		free = not f.attached_to_doctype or (
			f.attached_to_doctype == "Cash Drawer Entry" and f.attached_to_name == doc.name)
		if f.owner == frappe.session.user and free and get_datetime(f.creation) >= oldest:
			return f.name
	frappe.throw(_("Attach a photo of the receipt you are uploading now (taken today, by you, not used anywhere else), "
		"or leave it empty."))


def validate_entry(doc) -> None:
	"""Cash Drawer Entry validate: the rules of each kind, and no edits once it has posted."""
	if not doc.is_new():
		if not doc.flags.cash_drawer_internal:
			before = frappe.db.get_value("Cash Drawer Entry", doc.name, list(_IMMUTABLE), as_dict=True) or {}
			for field in _IMMUTABLE:
				if not _unchanged(field, before.get(field), doc.get(field)):
					frappe.throw(_("A cash entry cannot be changed. Undo it and record it again."))
		return
	if doc.entry_type not in ENTRY_TYPES:
		frappe.throw(_("Pick what the cash was for."))
	doc.status = "Posted"
	if doc.entry_type == "Payout Adjustment":
		doc.amount = flt(doc.amount, 2)
		if not doc.amount:
			frappe.throw(_("There is nothing to adjust."))
	else:
		doc.amount = money(doc.amount, _("Amount"))
	doc.posting_date = doc.posting_date or nowdate()
	company = doc.company
	ensure_accounts(company)
	cash = cash_account(company)
	doc.memo = (doc.memo or "").strip() or None
	doc.reference = (doc.reference or "").strip() or None
	if doc.entry_type == "Deposit":
		# A caller may name the bank account in debit_account; deposit_account refuses a clearing account.
		doc.debit_account, doc.credit_account = deposit_account(company, doc.debit_account), cash
	elif doc.entry_type == "From Bank":
		# The mirror of a deposit: the same bank account, never a processor's clearing account.
		doc.debit_account, doc.credit_account = cash, deposit_account(company, doc.credit_account)
	elif doc.entry_type == "Expense":
		if not doc.memo:
			frappe.throw(_("Write what the money was spent on."))
		doc.receipt = (doc.receipt or "").strip() or None
		doc.flags.receipt_file = _receipt_file(doc) if doc.receipt else None
		if doc.receipt and frappe.db.exists("Cash Drawer Entry", {"receipt": doc.receipt, "status": "Posted"}):
			frappe.throw(_("That receipt was already used for another cash expense. Attach this purchase's own receipt."))
		cap = expense_cap()
		if doc.amount > cap:
			frappe.throw(_("Cash expenses are limited to {0} each. Pay this one by card or from the bank.").format(
				_fmt(cap)))
		if doc.expense_account not in expense_account_list(company):
			problem = expense_account_problem(doc.expense_account or "", company) if doc.expense_account else None
			frappe.throw(problem or _("{0} is not on the list of accounts cash expenses can go to.").format(
				doc.expense_account or _("That account")))
		doc.debit_account, doc.credit_account = doc.expense_account, cash
	elif doc.entry_type == "Trade-in Payout":
		_validate_trade_in_payout(doc, cash)
	elif doc.entry_type == "Goods Payout":
		_validate_goods_payout(doc, cash)
	else:  # Payout / Payout Adjustment
		_validate_payout(doc, cash)


def check_payout_method(method: str | None, reference: str | None, who: str, reference_required: bool = True,
		fields: tuple[str, str] | None = None) -> None:
	"""How the store paid someone: one of PAYOUT_METHODS, and a bank payment (Zelle / Check / ACH) carries its
	reference (a gun's own payout, recorded without one, passes ``reference_required=False``). ``who`` names the payee
	in the message; ``fields`` (method field, reference field) name what an API caller sends. The one copy of the rule:
	the pages read PAYOUT_METHODS from the server."""
	method_field, reference_field = (f" ({f})" for f in fields) if fields else ("", "")
	if method not in PAYOUT_METHODS:
		frappe.throw(_("Say how the {0} was paid{1}: {2}.").format(who, method_field, ", ".join(PAYOUT_METHODS)))
	if reference_required and method != "Cash" and not (reference or "").strip():
		frappe.throw(_("A {0} payment needs its reference{1}: the check number, Zelle confirmation or ACH reference.").format(
			method, reference_field))


def _validate_payout(doc, cash: str) -> None:
	method = doc.payment_method
	check_payout_method(method, doc.reference, _("seller"), reference_required=False)
	if not doc.acquisition:
		frappe.throw(_("A seller payment belongs to a firearm acquisition."))
	from ffl_core.api.cost_correction import _stock_adjustment_accounts

	adjustment, _cc = _stock_adjustment_accounts(doc.company)
	paid_from = cash if method == "Cash" else bank_account(doc.company)
	if doc.entry_type == "Payout":
		doc.payout_key = doc.acquisition
	if flt(doc.amount) > 0:
		doc.debit_account, doc.credit_account = adjustment, paid_from
	else:
		doc.debit_account, doc.credit_account = paid_from, adjustment


def post_entry_je(doc) -> None:
	"""Cash Drawer Entry after_insert: post the Journal Entry and link it."""
	amount = abs(flt(doc.amount))
	prefix = {"Deposit": DEPOSIT, "From Bank": FROM_BANK, "Expense": EXPENSE,
		"Trade-in Payout": TRADE_IN_PAYOUT, "Goods Payout": GOODS_PAYOUT}.get(doc.entry_type, PAYOUT)
	debit = (doc.debit_account, amount, 0)
	if doc.entry_type in ("Deposit", "From Bank"):
		tail = doc.reference or ""
	elif doc.entry_type == "Expense":
		tail = f"{doc.expense_account} {doc.memo}"
	elif doc.entry_type == "Trade-in Payout":
		tail = f"{doc.invoice} ({' '.join(p for p in (doc.payment_method, doc.reference) if p)})"
		debit = (*debit, {"party_type": "Customer", "party": doc.customer})
	elif doc.entry_type == "Goods Payout":
		tail = f"{doc.purchase_receipt} ({' '.join(p for p in (doc.payment_method, doc.reference) if p)})"
	else:
		tail = f"{doc.acquisition} {doc.serial or ''}"
	je = _post_je(doc.company, [debit, (doc.credit_account, 0, amount)], f"{prefix} {tail}".strip(), doc.posting_date,
		source=doc.name)
	doc.db_set("journal_entry", je, update_modified=False)
	if doc.entry_type == "Expense" and doc.flags.receipt_file:
		# The receipt belongs to this entry from now on, and is private: saved through the File document so
		# Frappe moves the file itself, and the entry keeps the address it ended up at.
		receipt = frappe.get_doc("File", doc.flags.receipt_file)
		receipt.attached_to_doctype = "Cash Drawer Entry"
		receipt.attached_to_name = doc.name
		receipt.is_private = 1
		receipt.flags.ignore_permissions = True
		receipt.save()
		doc.db_set("receipt", receipt.file_url, update_modified=False)


def _new_entry(_flags: dict | None = None, **values) -> frappe._dict:
	doc = frappe.new_doc("Cash Drawer Entry")
	doc.update(values)
	doc.flags.ignore_permissions = True
	doc.flags.update(_flags or {})
	lock_drawer(doc.company)
	doc.insert()
	return frappe._dict(entry=doc.name, journal_entry=doc.journal_entry, amount=doc.amount, company=doc.company)


def record_deposit(company: str, amount, reference: str | None = None, bank_account_name: str | None = None) -> frappe._dict:
	return _new_entry(
		entry_type="Deposit", company=company, amount=amount, reference=reference, debit_account=bank_account_name)


def record_from_bank(company: str, amount, reference: str | None = None) -> frappe._dict:
	"""Cash taken out of the bank and put in the drawer: Dr Cash / Cr the bank (a deposit the other way round)."""
	return _new_entry(entry_type="From Bank", company=company, amount=amount, reference=reference)


def record_expense(company: str, amount, expense_account: str, memo: str, receipt: str | None = None) -> frappe._dict:
	return _new_entry(
		entry_type="Expense", company=company, amount=amount, expense_account=expense_account, memo=memo, receipt=receipt)


_PAYOUT_TYPES = ("Payout", "Payout Adjustment")


def _void_one(doc, when=None) -> None:
	_cancel_je(doc.journal_entry)
	doc.flags.cash_drawer_internal = True
	doc.status = "Voided"
	doc.voided_by = frappe.session.user
	doc.voided_on = when or now_datetime()
	doc.payout_key = None
	doc.flags.ignore_permissions = True
	doc.save()


def _require_system_manager() -> None:
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only a System Manager can change a seller payment that was recorded."), frappe.PermissionError)


def record_trade_in_payout(invoice) -> frappe._dict:
	"""Book the money the store paid the customer of a negative trade-in ticket.

	``invoice`` is the submitting POS Invoice doc (grand_total < 0). Creates one Cash Drawer Entry
	entry_type="Trade-in Payout", amount = -grand_total, payment_method = invoice.ffl_trade_in_payout_method,
	reference = invoice.ffl_trade_in_payout_reference (required unless Cash), invoice = invoice.name,
	customer = invoice.customer, payout_key = invoice.name (once per ticket), posting_date = invoice.posting_date.
	JE (remark prefix TRADE_IN_PAYOUT + invoice name): Dr Debtors (party = customer) / Cr Cash, or Cr the bank
	(bank_account) for Zelle / Check / ACH. Stamps invoice.ffl_trade_in_payout_entry. Same transaction as the
	submit. Returns {entry, journal_entry}.

	"grand_total" is the ticket's total as ERPNext reads it: the rounded total when there is one. Booking a ticket
	that already has its payout returns that payout (a retried submit pays nobody twice)."""
	from ffl_core.compliance.negative_trade_in import invoice_total

	done = frappe.db.get_value("Cash Drawer Entry", {"payout_key": invoice.name}, ["name", "journal_entry"], as_dict=True)
	if done:
		made = frappe._dict(entry=done.name, journal_entry=done.journal_entry)
	else:
		made = _new_entry(
			entry_type="Trade-in Payout", company=invoice.company, amount=-invoice_total(invoice),
			payment_method=invoice.get("ffl_trade_in_payout_method"), reference=invoice.get("ffl_trade_in_payout_reference"),
			invoice=invoice.name, customer=invoice.customer, posting_date=invoice.posting_date)
	if invoice.get("ffl_trade_in_payout_entry") != made.entry:
		invoice.db_set("ffl_trade_in_payout_entry", made.entry, update_modified=False)
	return made


def _validate_trade_in_payout(doc, cash: str) -> None:
	"""A trade-in payout is exactly what its submitted register ticket owes the customer for its trade-ins, paid once,
	the way the ticket says (method, and its reference unless cash): Dr the ticket's Debtors account / Cr Cash or the
	bank."""
	from erpnext.accounts.party import get_party_account

	from ffl_core.compliance.negative_trade_in import invoice_total
	from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM

	method = doc.payment_method
	check_payout_method(method, doc.reference, _("customer"))
	ticket = frappe.db.get_value(
		"POS Invoice", doc.invoice or "", ["docstatus", "is_return", "company", "customer", "grand_total", "rounded_total",
			"debit_to", "ffl_trade_in_payout_method", "ffl_trade_in_payout_reference"], as_dict=True)
	if (not ticket or ticket.docstatus != 1 or ticket.is_return or ticket.company != doc.company
			or not frappe.db.exists("POS Invoice Item", {"parent": doc.invoice, "item_code": TRADE_IN_CREDIT_ITEM})):
		frappe.throw(_("A trade-in payout belongs to a submitted register ticket of {0} with trade-in credit.").format(
			doc.company))
	owed = flt(-invoice_total(ticket), 2)
	if owed <= 0 or abs(flt(doc.amount) - owed) >= 0.005:
		frappe.throw(_("{0} owes the customer {1}, not {2}.").format(doc.invoice, _fmt(max(owed, 0)), _fmt(doc.amount)))
	if method != ticket.ffl_trade_in_payout_method or (doc.reference or "") != (ticket.ffl_trade_in_payout_reference or "").strip():
		frappe.throw(_("{0} says the customer was paid by {1}{2}; record the payout the same way.").format(
			doc.invoice, ticket.ffl_trade_in_payout_method or _("(nothing)"),
			f" ({ticket.ffl_trade_in_payout_reference})" if ticket.ffl_trade_in_payout_reference else ""))

	doc.customer = ticket.customer
	doc.payout_key = doc.invoice
	doc.debit_account = ticket.debit_to or get_party_account("Customer", ticket.customer, doc.company)
	doc.credit_account = cash if method == "Cash" else bank_account(doc.company)


def void_trade_in_payout(invoice_name: str) -> dict | None:
	"""Undo the payout of a negative trade-in ticket when that ticket is cancelled — the cash comes back to the
	drawer (void_entry). Refused once a count was taken after it. Never touches the trade-in acquisitions /
	Material Receipts. None when the ticket had no payout."""
	posted = trade_in_payouts([invoice_name])
	return void_entry(posted[0].name) if posted else None


def trade_in_payouts(invoices) -> list[frappe._dict]:
	"""The posted trade-in payouts of these register tickets: {name, company, creation, journal_entry, invoice,
	customer, debit_account} — debit_account is the ticket's own Debtors account the payout was booked to."""
	if not invoices:
		return []
	return frappe.get_all(
		"Cash Drawer Entry",
		filters={"entry_type": "Trade-in Payout", "status": "Posted", "invoice": ["in", list(invoices)]},
		fields=["name", "company", "creation", "journal_entry", "invoice", "customer", "debit_account"],
		order_by="creation")


def void_entry(name: str) -> dict:
	"""Undo a cash entry: its Journal Entry is cancelled, so the cash is back in the drawer's books.

	Not once a count has been taken since it was made (that count already relied on it). A seller payment
	is tied to its acquisition and its cost corrections: only a System Manager undoes one, and the corrections
	booked on it go with it. A seller payment for a Receive Goods receipt is undone like any cash entry, and the
	cost-correction differences paid with it (``adjust_payout_for_cost_correction``) go with it."""
	company = frappe.db.get_value("Cash Drawer Entry", name, "company")
	if not company:
		frappe.throw(_("{0} was not found.").format(name))
	lock_drawer(company)
	row = frappe.db.get_value(
		"Cash Drawer Entry", name, ["company", "status", "creation", "entry_type", "acquisition"], as_dict=True, for_update=True)
	if row.status == "Voided":
		frappe.throw(_("That entry was already undone."))
	if row.entry_type in _PAYOUT_TYPES:
		_require_system_manager()
	elif counted_since(row.company, row.creation):
		frappe.throw(_("A count was taken after that entry, so it cannot be undone any more. Ask a manager to undo the "
			"count first, or tell your accountant."))
	doc = frappe.get_doc("Cash Drawer Entry", name)
	if row.entry_type == "Payout":
		doc.net_before_undo = flt(sum(flt(r.amount) for r in _posted_payouts(row.acquisition)), 2)
		for other in frappe.get_all("Cash Drawer Entry", filters={
				"acquisition": row.acquisition, "entry_type": "Payout Adjustment", "status": "Posted"}, pluck="name"):
			_void_one(frappe.get_doc("Cash Drawer Entry", other))
	when = now_datetime()
	if row.entry_type == "Goods Payout":
		for other in _goods_adjustments(doc.purchase_receipt):
			_void_one(frappe.get_doc("Cash Drawer Entry", other.name), when)
	_void_one(doc, when)
	return {"entry": name, "status": "Voided"}


def relied_on(count, creation) -> bool:
	"""Whether that count (a Cash Drawer Close) relied on something made at ``creation``: it was made before the count.
	The one rule behind every "refused once a count was taken after it"."""
	return bool(count and count.closed_at and get_datetime(creation) <= get_datetime(count.closed_at))


def counted_since(company: str, creation) -> bool:
	"""Whether a drawer count was taken after ``creation`` (the latest count, ``relied_on``)."""
	return relied_on(next(iter(active_closes(company, is_counted=1)), None), creation)


# --------------------------------------------------------------------------- #
# Seller payouts (trade-ins)
# --------------------------------------------------------------------------- #

def _posted_payouts(acquisition: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Cash Drawer Entry",
		filters={"acquisition": acquisition, "status": "Posted", "entry_type": ["in", list(_PAYOUT_TYPES)]},
		fields=["name", "company", "amount", "payment_method", "entry_type", "serial", "acquisition"], order_by="creation")


def tracking_started():
	"""When cash tracking went live on this site (set once by the migrate step); None when it never ran."""
	value = _setting("tracking_started", None)
	return get_datetime(value) if value else None


def _credited_on(acquisition: str) -> str | None:
	"""The submitted invoice that spent this acquisition as a trade-in credit, if any."""
	for table in ("POS Invoice", "Sales Invoice"):
		row = frappe.db.sql(
			f"""SELECT i.name FROM `tab{table} Item` it JOIN `tab{table}` i ON i.name = it.parent
			WHERE it.ffl_trade_in_acquisition = %s AND i.docstatus = 1 AND IFNULL(i.is_return, 0) = 0 LIMIT 1""",
			(acquisition,))
		if row:
			return row[0][0]
	return None


def current_acquisition(serial: str) -> str | None:
	"""The gun's current acquisition: the newest booked one (a re-acquired serial has older ones with their own payments)."""
	return frappe.db.get_value(
		"FFL Acquisition", {"serial": serial, "docstatus": 1}, "name", order_by="acquisition_date desc, creation desc")


def record_payout(acquisition: str, method: str, replace: bool = False) -> frappe._dict:
	"""Book again the money paid to the seller of an acquired firearm, recorded the old way (before a pay-now buy became a
	receipt paid with it): Dr Stock Adjustment / Cr Cash (or the bank), once per acquisition, so the gun's Stock
	Adjustment (its Material Receipt) nets to zero. Only for a gun that had one: a first payment is refused.

	A purchase is either paid out or spent as a credit on a sale, never both; purchases from before cash tracking
	started are the accountant's. ``replace`` (a System Manager fixing a wrong method) undoes the recorded payment
	and its cost corrections and books the same net amount again with the new method; after a plain undo the
	last net is booked again, not the original cost."""
	acq = frappe.db.get_value(
		"FFL Acquisition", acquisition, ["name", "docstatus", "company", "cost", "serial", "source_type", "creation",
			"purchase_receipt"], as_dict=True, for_update=True)
	if not acq or acq.docstatus != 1:
		frappe.throw(_("{0} is not a booked acquisition, so there is nothing to pay for yet.").format(acquisition))
	from ffl_core.acquisition import INDIVIDUAL_SOURCES

	if acq.source_type not in INDIVIDUAL_SOURCES:
		frappe.throw(_("{0} was not bought from a private seller, so no seller payment is recorded for it.").format(acquisition))
	if flt(acq.cost) <= 0:
		frappe.throw(_("{0} has no cost, so there is nothing to pay.").format(acquisition))
	spent_on = _credited_on(acq.name)
	if spent_on:
		frappe.throw(_("{0} was applied as a credit on {1}, so the seller was not paid out for it.").format(acquisition, spent_on))
	company = acq.company or frappe.defaults.get_user_default("Company")
	amount = flt(acq.cost, 2)
	existing = frappe.db.get_value("Cash Drawer Entry", {"payout_key": acq.name, "status": "Posted"}, "name")
	if existing:
		if not replace:
			frappe.throw(_("The payment for {0} is already recorded.").format(acquisition))
		_require_system_manager()
		lock_drawer(company)
		amount = flt(sum(flt(r.amount) for r in _posted_payouts(acq.name)), 2)
		void_entry(existing)
	else:
		receipt = acq.purchase_receipt and frappe.db.get_value("Purchase Receipt", acq.purchase_receipt, [
			"ffl_acquisition_source", "ffl_acquisition_type", "ffl_transfer_for_customer", "is_ffl_transfer"], as_dict=True)
		if receipt and receipt_pays_seller(receipt):
			# Paid, undone or still owed: a gun on a receipt that pays its seller is paid through the receipt's seller
			# payment only (record_goods_payout), never on its own as well -- unless the gun had its own payout before
			# (made the old way) and the receipt's payment does not cover it: that one is booked again as before.
			had_own = frappe.db.exists("Cash Drawer Entry", {"acquisition": acq.name, "entry_type": "Payout"})
			covering = _receipt_payment_covering(acq.purchase_receipt, acq.name)
			if covering or not had_own:
				frappe.throw(_("{0} came in on {1}, which pays its seller through the receipt's seller payment{2}. Record or "
					"change that payment on the Cash Drawer page instead.").format(
						acquisition, acq.purchase_receipt, f" ({covering})" if covering else ""))
		undone = frappe.db.get_value(
			"Cash Drawer Entry", {"acquisition": acq.name, "entry_type": "Payout", "status": "Voided"},
			"net_before_undo", order_by="creation desc")
		if undone is None:
			# A gun's own seller payment is history (a pay-now buy is a receipt paid with it now, api/trade_in._buy_now):
			# one recorded the old way can still be undone, booked again, have its method changed and its cost corrected,
			# but no new one is made.
			started = tracking_started()
			if started and get_datetime(acq.creation) < started:
				frappe.throw(_("This purchase is from before cash tracking started. Your accountant handles it in the books."))
			frappe.throw(_("{0} has no seller payment recorded. A seller is paid with the gun's receipt now (the Trade-in "
				"dialog or Receive Goods); tell your accountant how this one was paid.").format(acquisition))
		amount = flt(undone, 2) or amount  # an undone payment comes back as it last stood, corrections included
	return _new_entry(
		entry_type="Payout", company=company, amount=amount, payment_method=method,
		acquisition=acq.name, serial=acq.serial)


def adjust_payout_for_cost_correction(serial: str, new_cost: float) -> str | None:
	"""A corrected cost on a gun whose seller payment was recorded: book the difference the same way, so the
	gun's Stock Adjustment stays at zero. Only the gun's CURRENT acquisition counts. A gun paid with its Receive Goods
	receipt (``goods_payout_cover``) counts its share of that payment as paid, and its difference is linked to the
	receipt, so undoing or changing the receipt's payment takes it along. Returns the Journal Entry, or None when
	nothing is owed."""
	acquisition = current_acquisition(serial)
	rows = _posted_payouts(acquisition) if acquisition else []
	first = next((r for r in rows if r.entry_type == "Payout"), None)
	extra = {}
	if first:
		paid = flt(sum(flt(r.amount) for r in rows), 2)
	else:
		first = goods_payout_cover(serial)
		if not first:
			_refuse_while_receipt_payment_undone(acquisition)
			return None
		# The differences already paid for this gun with its receipt -- by receipt and serial, not acquisition: an
		# amendment's takeover and a payment recorded again link them to the receipt that stands now.
		paid = flt(first.share + sum(flt(r.amount) for r in _goods_adjustments(first.purchase_receipt) if r.serial == serial), 2)
		extra = {"purchase_receipt": first.purchase_receipt}
	delta = flt(flt(new_cost) - paid, 2)
	if abs(delta) < 0.01:
		return None
	made = _new_entry(
		entry_type="Payout Adjustment", company=first.company, amount=delta, payment_method=first.payment_method,
		acquisition=acquisition, serial=serial, **extra)
	return made.journal_entry


def _refuse_while_receipt_payment_undone(acquisition: str | None) -> None:
	"""A gun on a pay-now receipt whose seller payment is undone (and not recorded again): a cost correction now would
	book nothing, and recording the payment again later pays the receipt's original price -- the difference would be
	lost. So it waits for the payment to be recorded again."""
	receipt = acquisition and frappe.db.get_value("FFL Acquisition", acquisition, "purchase_receipt")
	if not receipt:
		return
	chain = [r.name for r in receipt_chain(receipt)]
	statuses = set(frappe.get_all("Cash Drawer Entry", filters={"entry_type": "Goods Payout",
		"purchase_receipt": ["in", chain]}, pluck="status"))
	if "Voided" in statuses and "Posted" not in statuses:
		frappe.throw(_("The seller payment for receipt {0} was undone and not recorded again. Record it again on the Cash "
			"Drawer page first, then correct this gun's cost.").format(receipt))


# --------------------------------------------------------------------------- #
# Seller payments (Receive Goods)
# --------------------------------------------------------------------------- #

# The Receive Goods acquisition types (FastBound's AcquireType, free text) of a purchase the store pays its private
# seller for at receipt. An allow-list, matched like firearm.CONSIGNMENT_ACQUISITION_TYPES (trimmed, any case):
# * "" -- the type is optional on the page;
# * "purchase";
# * "individual" -- FastBound's type for an acquisition from a non-licensee (fastbound/client.py fb_type_map sends it
#   for every individual source when none is picked); a gun
#   bought from a private seller and paid by check is one.
# Everything else pays nobody here and keeps receiving as before: Consignment (the consignor is paid when the gun
# sells), Gunsmithing (the customer's own gun), a transfer, and any spelling nobody has looked at yet. New pay-now
# spellings get added here, on purpose, with a test.
PAY_NOW_ACQUISITION_TYPES = frozenset({"", "purchase", "individual"})


def receipt_pays_seller(receipt) -> bool:
	"""Whether a Receive Goods receipt (a Purchase Receipt, or the same fields in a dict) is a purchase the store pays
	its private seller for at receipt: the "Individual" acquisition source, a pay-now acquisition type, and not a
	customer's gun (the FFL Transfer box, a transfer type or a pickup customer).

	A pay-on-sale supplier never comes this way: pay-on-sale is stamped only on a receipt from an FFL-dealer source
	(compliance/purchase.py ``_owner_stamps``), and an individual receipt's supplier is the "Individual Seller"
	placeholder."""
	from ffl_core.firearm import is_transfer_intake

	acquisition_type = receipt.get("ffl_acquisition_type")
	return bool(receipt.get("ffl_acquisition_source") == "Individual"
		and (acquisition_type or "").strip().lower() in PAY_NOW_ACQUISITION_TYPES
		and not is_transfer_intake(acquisition_type, receipt.get("ffl_transfer_for_customer"), receipt.get("is_ffl_transfer")))


def _goods_receipt(name: str | None):
	"""The Purchase Receipt a goods payout pays for: submitted, not a return, and paid to its seller at receipt
	(``receipt_pays_seller``)."""
	receipt = frappe.get_doc("Purchase Receipt", name) if name and frappe.db.exists("Purchase Receipt", name) else None
	if not receipt or receipt.docstatus != 1 or cint(receipt.is_return) or not receipt_pays_seller(receipt):
		frappe.throw(_("{0} is not a submitted purchase from a private seller, so no seller payment is recorded for it.").format(
			name or _("That receipt")))
	return receipt


def seller_payment_at_receipt(receipt, lines) -> frappe._dict:
	"""The one decision, before a Receive Goods receipt is submitted, of whether and how much its private seller is
	paid: {pays, amount}. ``receipt`` holds the receipt's fields (``receipt_pays_seller``), ``lines`` its (qty, rate)
	pairs. Nothing on a receipt being received is paid yet -- its acquisitions are made when it is submitted -- so every
	line counts; after submit ``goods_payout_due`` judges the same lines by the receipt's own acquisitions and comes to
	the same amount. The Receive Goods dialog (``seller_payment_preview``) and ``create_receive`` both ask this."""
	amount = flt(sum(receipt_line_amount(qty, rate) for qty, rate in lines), 2)
	return frappe._dict(pays=bool(receipt_pays_seller(receipt) and amount > 0), amount=amount)


def receipt_line_amount(qty, rate) -> float:
	"""What a Purchase Receipt line books for ``qty`` x ``rate``: the rate rounded to the line's rate precision and the
	product to its amount precision, as ERPNext rounds them (100 rounds at 0.125 book 13.00 at a 2-decimal rate, not
	12.50), so the dialog shows exactly what the receipt books. create_receive checks the two agree before it pays."""
	from frappe.model.meta import get_field_precision

	meta = frappe.get_meta("Purchase Receipt Item")
	rate = flt(rate, get_field_precision(meta.get_field("rate")))
	return flt(flt(qty) * rate, get_field_precision(meta.get_field("amount")))


def goods_payout_due(receipt) -> frappe._dict:
	"""What paying the private seller of a Receive Goods purchase covers: every line at what the receipt booked for it (its
	net amount, credited to Stock Received But Not Billed, less anything returned to the seller), except a gun whose acquisition ON THIS RECEIPT already carries
	its own seller payment (a ``record_payout`` made the old way). An older acquisition of the same serial -- a gun bought
	back -- is another purchase and changes nothing. Returns {amount, excluded: [{serial, acquisition, entry, amount}]}.

	The receipt's guns are paid here and not through ``record_payout``: the receipt put them into stock against Stock
	Received But Not Billed, while ``record_payout`` books against Stock Adjustment (it pairs with the trade-in dialog's
	Material Receipt), which would leave both accounts open. ``record_payout`` refuses a gun on such a receipt.

	lazy: the receipt's net amount is what the seller got (Receive Goods adds no purchase taxes); upgrade path: read
	the grand total if Receive Goods ever adds taxes to a private purchase."""
	from ffl_core.serials import serials_for_lines

	own = {a.serial: a.name for a in frappe.get_all("FFL Acquisition", filters={"purchase_receipt": receipt.name,
		"docstatus": 1}, fields=["name", "serial"])}
	paid = {p.acquisition: p.name for p in frappe.get_all("Cash Drawer Entry", filters={"entry_type": "Payout",
		"status": "Posted", "acquisition": ["in", list(own.values()) or [""]]}, fields=["name", "acquisition"])}
	# Goods given back to the seller on a submitted purchase return against this receipt are not owed. Read from the
	# returns themselves: the line's returned_qty is not reliably kept up to date.
	returned = dict(frappe.db.sql(
		"""SELECT i.purchase_receipt_item, SUM(-i.qty) FROM `tabPurchase Receipt Item` i
		JOIN `tabPurchase Receipt` p ON p.name = i.parent
		WHERE p.docstatus = 1 AND p.is_return = 1 AND p.return_against = %s GROUP BY i.purchase_receipt_item""",
		(receipt.name,)))
	amount, excluded = 0.0, []
	for row, serials in zip(receipt.items, serials_for_lines(receipt.items)):
		back = flt(returned.get(row.name))
		line = flt(row.base_net_amount, 2) if not back else flt(flt(row.base_net_rate) * (flt(row.qty) - back), 2)
		left_out = [frappe._dict(serial=x, acquisition=own[x], entry=paid[own[x]], amount=flt(row.base_net_rate, 2))
			for x in serials if own.get(x) in paid]
		if left_out:
			line = 0.0 if len(left_out) >= len(serials) else flt(line - sum(e.amount for e in left_out), 2)
			excluded += left_out
		amount = flt(amount + line, 2)
	return frappe._dict(amount=amount, excluded=excluded)


def _srbnb(company: str) -> str:
	srbnb = frappe.get_cached_value("Company", company, "stock_received_but_not_billed")
	if not srbnb:
		frappe.throw(_("{0} has no Stock Received But Not Billed account. Ask your accountant to set it on the company.").format(
			company))
	return srbnb


def _validate_goods_payout(doc, cash: str) -> None:
	"""A goods payout is exactly what its receipt owes the seller (``goods_payout_due``), paid once, the way the cashier
	said (method, and its reference unless cash): Dr Stock Received But Not Billed / Cr Cash or the bank. It is dated
	the day the money leaves (the entry's own date, today by default), not the receipt's date, which can be back-dated;
	the memo keeps the receipt's date and lists any gun left out because it was paid on its own."""
	check_payout_method(doc.payment_method, doc.reference, _("seller"))
	# record_goods_payout hands over the receipt and what it owes (worked out once); anything else reads them here.
	receipt = doc.flags.goods_receipt if getattr(doc.flags.goods_receipt, "name", None) == doc.purchase_receipt else (
		_goods_receipt(doc.purchase_receipt))
	if receipt.company != doc.company:
		frappe.throw(_("{0} belongs to {1}.").format(receipt.name, receipt.company))
	due = doc.flags.goods_due if doc.flags.goods_receipt is receipt and doc.flags.goods_due else goods_payout_due(receipt)
	if due.amount <= 0 or abs(flt(doc.amount) - due.amount) >= 0.005:
		frappe.throw(_("{0} owes the seller {1}, not {2}.").format(receipt.name, _fmt(max(due.amount, 0)), _fmt(doc.amount)))
	doc.customer = receipt.get("ffl_seller_customer")
	doc.payout_key = receipt.name
	doc.memo = "; ".join([f"Receipt {receipt.name} dated {receipt.posting_date}"] + ([
		"Not included, paid on its own: " + ", ".join(f"{e.serial} ({e.acquisition}, {e.entry}) {_fmt(e.amount)}"
			for e in due.excluded)] if due.excluded else []))
	doc.debit_account = _srbnb(doc.company)
	doc.credit_account = cash if doc.payment_method == "Cash" else bank_account(doc.company)


def _receipt_payment_covering(receipt: str, acquisition: str) -> str | None:
	"""The receipt's standing seller payment when it paid for this gun, else None. A gun left out because it had its own
	payout then is named in that payment's memo as "(acquisition, entry)" (``_validate_goods_payout``). lazy: read back
	from the memo; upgrade path: a child table of the guns each payment covers."""
	paid = frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": receipt, "entry_type": "Goods Payout",
		"status": "Posted"}, ["name", "memo"], as_dict=True)
	if not paid or re.search(rf"\({re.escape(acquisition)}, ", paid.memo or ""):
		return None
	return paid.name


def _goods_adjustments(receipt: str, status: str = "Posted") -> list[frappe._dict]:
	"""The cost-correction differences paid with a receipt's seller payment (Payout Adjustments linked to it)."""
	return frappe.get_all("Cash Drawer Entry", filters={
		"entry_type": "Payout Adjustment", "purchase_receipt": receipt, "status": status},
		fields=["name", "acquisition", "serial", "amount", "voided_on"], order_by="creation")


def _own_acquisition(receipt: str, serial: str) -> str | None:
	"""The gun's acquisition made by this receipt, else its current one."""
	return frappe.db.get_value("FFL Acquisition", {"purchase_receipt": receipt, "serial": serial, "docstatus": 1},
		"name") or current_acquisition(serial)


def receipt_chain(receipt: str | None) -> list[frappe._dict]:
	"""The amendment line a receipt belongs to, oldest first: [{name, docstatus}] -- the receipts it amends and the
	ones that amend it. The one walker for "the receipts these goods were received on"."""
	if not receipt:
		return []
	root, seen = receipt, {receipt}
	while True:
		parent = frappe.db.get_value("Purchase Receipt", root, "amended_from")
		if not parent or parent in seen:
			break
		seen.add(parent)
		root = parent
	chain, name, seen = [], root, set()
	while name and name not in seen:
		seen.add(name)
		docstatus = frappe.db.get_value("Purchase Receipt", name, "docstatus")
		if docstatus is None:
			break
		chain.append(frappe._dict(name=name, docstatus=docstatus))
		name = frappe.db.get_value("Purchase Receipt", {"amended_from": name}, "name", order_by="creation desc")
	return chain


def _refuse_goods_payout(doc, chain: list[str], method: str | None, reference: str | None,
		existing: str | None = None) -> frappe._dict:
	"""Every refusal of paying the seller of receipt ``doc`` (on its amendment line ``chain``), before anything is
	written: the method, a "paid outside the system" mark, the goods still paid on another receipt of the line, the
	accounts, a frozen period, a receipt that owes nothing now, and -- for a payment the receipt does not already have
	(``existing``) -- the same goods paid on a cancelled receipt of this seller (``payment_left_on_a_cancelled_receipt``).
	Returns what the receipt owes (``goods_payout_due``)."""
	from ffl_core.api.cost_correction import _reject_if_period_frozen

	check_payout_method(method, reference, _("seller"))
	outside = _paid_outside(chain)
	if outside:
		frappe.throw(_("The seller of {0} was paid outside the system ({1}). Clear that mark first if a payment should be "
			"recorded here.").format(outside.name, outside.note))
	elsewhere = frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": ["in", [n for n in chain if n != doc.name]
		or [""]], "entry_type": "Goods Payout", "status": "Posted"}, ["name", "purchase_receipt"], as_dict=True)
	if elsewhere:
		frappe.throw(_("These goods are still paid with {0} on {1}. Undo it on the Cash Drawer page first.").format(
			elsewhere.name, elsewhere.purchase_receipt))
	_srbnb(doc.company)
	cash_account(doc.company)
	_reject_if_period_frozen(doc.company, nowdate(), what="a cash drawer entry")
	due = goods_payout_due(doc)
	if due.amount <= 0:
		frappe.throw(_("{0} owes the seller nothing now (every gun on it was paid on its own), so no payment is recorded{1}.")
			.format(doc.name, _(" and {0} stays as it is").format(existing) if existing else ""))
	if not existing:
		left = payment_left_on_a_cancelled_receipt(doc.get("ffl_seller_customer"), due.amount, exclude=chain)
		if left:
			frappe.throw(_("{0} was already paid {1} for receipt {2}, which was cancelled; that payment ({3}) still stands. If "
				"these are the same goods, amend {2} instead of receiving them again: the amendment takes the payment over. If "
				"the seller gave the money back, undo {3} on the Cash Drawer page first.").format(
					doc.get("ffl_seller_customer"), _fmt(left.amount), left.purchase_receipt, left.name))
	return due


def payment_left_on_a_cancelled_receipt(customer: str | None, amount: float,
		exclude: list[str] | None = None) -> frappe._dict | None:
	"""A seller payment still standing on a cancelled receipt of this seller, for the same amount: the same goods being
	received again instead of amended. lazy: matched on seller and amount only, so a genuinely new purchase of the same
	amount waits for that payment to be undone or its receipt amended; upgrade path: ask the cashier which it is."""
	if not customer or flt(amount) <= 0:
		return None
	rows = frappe.db.sql(
		"""SELECT e.name, e.purchase_receipt, e.amount FROM `tabCash Drawer Entry` e
		JOIN `tabPurchase Receipt` r ON r.name = e.purchase_receipt
		WHERE e.entry_type = 'Goods Payout' AND e.status = 'Posted' AND r.docstatus = 2 AND e.customer = %s
			AND ABS(e.amount - %s) < 0.005 AND e.purchase_receipt NOT IN %s
		ORDER BY e.creation DESC LIMIT 1""", (customer, flt(amount, 2), tuple(exclude or [""])), as_dict=True)
	return rows[0] if rows else None


def record_goods_payout(receipt: str, method: str, reference: str | None = None, replace: bool = False) -> frappe._dict:
	"""Book what the store paid the private seller of a Receive Goods purchase: one Cash Drawer Entry "Goods Payout",
	Dr Stock Received But Not Billed / Cr Cash, or Cr the bank for Zelle / Check / ACH (reference required), for
	``goods_payout_due``, once per receipt (payout_key = the receipt), dated today. ``create_receive`` calls it in the
	receive's own transaction, before the bound-book push, after taking the drawer lock first (``lock_drawer``).

	``replace`` is a manager's: changing the method (the payment recorded is undone and booked again, refused once a
	count relied on it, like any undo), recording again after an undo, or recording the first payment of a receipt that
	never had one (received before payments were booked at receive, or made outside Receive Goods) -- only for a receipt
	received since cash tracking started; older ones are the accountant's. Every path runs the same refusals
	(``_refuse_goods_payout``), so a receipt typed in Desk for goods already paid on a cancelled receipt is refused here
	too. The cost-correction differences paid with the payment are booked again with it, from whichever receipt of the
	amendment line they were undone on. Nothing is undone unless the new payment is booked in the same transaction.
	Returns {entry, journal_entry, amount, company, excluded}."""
	doc = _goods_receipt(receipt)
	chain = [r.name for r in receipt_chain(doc.name)]
	existing = frappe.db.get_value("Cash Drawer Entry", {"payout_key": doc.name, "status": "Posted"}, "name")
	if existing and not replace:
		frappe.throw(_("The seller payment for {0} is already recorded ({1}).").format(doc.name, existing))
	carry = []
	if existing:
		carry = [(a.serial, a.amount) for a in _goods_adjustments(doc.name)]
	elif replace:
		last = frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": ["in", chain], "entry_type": "Goods Payout",
			"status": "Voided"}, ["name", "voided_on", "purchase_receipt"], as_dict=True, order_by="voided_on desc")
		if last:
			carry = [(a.serial, a.amount) for a in _goods_adjustments(last.purchase_receipt, "Voided")
				if a.voided_on == last.voided_on]
		else:
			started = tracking_started()
			if started and get_datetime(doc.creation) < started:
				frappe.throw(_("{0} was received before cash tracking started, so its seller payment is not recorded here. "
					"Your accountant handles it in the books.").format(doc.name))
	due = _refuse_goods_payout(doc, chain, method, reference, existing=existing)
	if existing:
		void_entry(existing)
	made = _new_entry(
		_flags={"goods_receipt": doc, "goods_due": due},
		entry_type="Goods Payout", company=doc.company, amount=due.amount, payment_method=method, reference=reference,
		purchase_receipt=doc.name)
	for serial, amount in carry:
		_new_entry(entry_type="Payout Adjustment", company=doc.company, amount=amount, payment_method=method,
			acquisition=_own_acquisition(doc.name, serial), serial=serial, purchase_receipt=doc.name)
	made.excluded = due.excluded
	return made


def seller_payments_by_serial(serials: list[str]) -> dict[str, frappe._dict]:
	"""How each gun's seller was paid, for its current acquisition, in a constant number of queries: {serial: {own,
	goods}} -- ``own`` its own payout, ``goods`` the name of the receipt payment that paid for it (None when it was paid on
	its own or not at all)."""
	acquisitions = {}
	for a in frappe.get_all("FFL Acquisition", filters={"serial": ["in", list(serials) or [""]], "docstatus": 1},
			fields=["name", "serial", "purchase_receipt"], order_by="acquisition_date desc, creation desc"):
		acquisitions.setdefault(a.serial, a)
	own = set(frappe.get_all("Cash Drawer Entry", filters={"entry_type": "Payout", "status": "Posted",
		"acquisition": ["in", [a.name for a in acquisitions.values()] or [""]]}, pluck="acquisition"))
	receipts = [a.purchase_receipt for a in acquisitions.values() if a.purchase_receipt]
	goods = {g.purchase_receipt: g for g in frappe.get_all("Cash Drawer Entry", filters={"entry_type": "Goods Payout",
		"status": "Posted", "purchase_receipt": ["in", receipts or [""]]}, fields=["name", "purchase_receipt", "memo"])}
	out = {}
	for serial in serials:
		a = acquisitions.get(serial)
		has_own = bool(a and a.name in own)
		paid = None if has_own or not a else goods.get(a.purchase_receipt)
		# A gun the payment left out because it was paid on its own then (named in its memo, ``_receipt_payment_covering``)
		# was not paid by it, even after that own payment was undone.
		if paid and re.search(rf"\({re.escape(a.name)}, ", paid.memo or ""):
			paid = None
		out[serial] = frappe._dict(own=has_own, goods=paid.name if paid else None)
	return out


def goods_payout_cover(serial: str) -> frappe._dict | None:
	"""The receipt payment that paid for this gun -- {name, company, payment_method, purchase_receipt, share}, share being
	the gun's line rate on the receipt -- or None (no such payment, or the gun was paid on its own)."""
	from ffl_core.serials import serials_for_lines

	entry = seller_payments_by_serial([serial])[serial].goods
	if not entry:
		return None
	paid = frappe.db.get_value("Cash Drawer Entry", entry, ["name", "company", "payment_method", "purchase_receipt"],
		as_dict=True)
	rows = frappe.get_all("Purchase Receipt Item", filters={"parent": paid.purchase_receipt, "parenttype": "Purchase Receipt"},
		fields=["base_net_rate", "serial_no", "serial_and_batch_bundle"])
	paid.share = next((flt(r.base_net_rate, 2) for r, found in zip(rows, serials_for_lines(rows)) if serial in found), 0.0)
	return paid


def take_over_goods_payout_on_amend(doc, method=None) -> None:
	"""Purchase Receipt on_submit: an amendment takes over the seller payment still standing on the receipt it amends
	(and the cost-correction differences paid with it, relinked to the amendment's own acquisitions) when it owes the
	seller the same amount -- the money left once, for these goods. Otherwise the payment stays on the cancelled receipt
	and the person submitting is told how to fix it (undo it, then record this receipt's payment on the Cash Drawer
	page)."""
	if not doc.get("amended_from") or cint(doc.get("is_return")):
		return
	paid = frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": doc.amended_from, "entry_type": "Goods Payout",
		"status": "Posted"}, ["name", "amount", "memo", "customer", "company"], as_dict=True)
	if not paid:
		return
	if (paid.customer or None) != (doc.get("ffl_seller_customer") or None) or paid.company != doc.company:
		frappe.msgprint(_("The seller payment {0} ({1}) stays on the cancelled receipt {2}: it paid {3}, and this receipt's "
			"seller is {4}. Undo {0} on the Cash Drawer page, then record this receipt's payment there.").format(
				paid.name, _fmt(paid.amount), doc.amended_from, paid.customer or _("nobody named"),
				doc.get("ffl_seller_customer") or _("nobody named")), indicator="orange")
		return
	# lazy: a gun the original left out because it was paid on its own counts again here (its own payout sits on the
	# original's cancelled acquisition), so such a receipt is never taken over and a manager undoes and records again;
	# upgrade path: none needed for new receipts -- record_payout makes no new own payouts -- so only older ones remain.
	due = goods_payout_due(doc).amount if receipt_pays_seller(doc) else 0.0
	if abs(due - flt(paid.amount)) >= 0.005:
		frappe.msgprint(_("The seller payment {0} ({1}) stays on the cancelled receipt {2}: this receipt owes the seller {3}. "
			"Undo {0} on the Cash Drawer page, then record this receipt's payment there.").format(
				paid.name, _fmt(paid.amount), doc.amended_from, _fmt(due)), indicator="orange")
		return
	note = f"Moved from {doc.amended_from} to {doc.name} (dated {doc.posting_date}) when it was amended"
	frappe.db.set_value("Cash Drawer Entry", paid.name, {"purchase_receipt": doc.name, "payout_key": doc.name,
		"memo": "; ".join(p for p in (paid.memo, note) if p)}, update_modified=False)
	for adj in _goods_adjustments(doc.amended_from):
		frappe.db.set_value("Cash Drawer Entry", adj.name, {"purchase_receipt": doc.name,
			"acquisition": _own_acquisition(doc.name, adj.serial) or adj.acquisition}, update_modified=False)
	frappe.get_doc("Cash Drawer Entry", paid.name).add_comment("Info", note)


PAID_OUTSIDE_FIELD = "ffl_seller_paid_outside"


def _paid_outside(receipts: list[str]) -> frappe._dict | None:
	"""The receipt among these marked as paid outside the system, with its note: {name, note}, else None."""
	row = frappe.db.get_value("Purchase Receipt", {"name": ["in", receipts or [""]], PAID_OUTSIDE_FIELD: ["is", "set"]},
		["name", PAID_OUTSIDE_FIELD], as_dict=True)
	return frappe._dict(name=row.name, note=row.get(PAID_OUTSIDE_FIELD)) if row else None


def mark_paid_outside(receipt: str, note: str | None) -> dict:
	"""A manager says the private seller of this pay-now receipt was paid outside the system -- a hand journal entry,
	a check booked in QuickBooks: nothing is booked, the receipt leaves the unpaid list (``receipts_to_pay``) and
	``record_goods_payout`` refuses it (and its amendments) until the mark is cleared. The note says how (required);
	who marked it and when go into the receipt's audit comment. Refused for a receipt paid in the system."""
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Write how the seller was paid (the journal entry, the check number)."))
	doc = _goods_receipt(receipt)
	chain = [r.name for r in receipt_chain(doc.name)]
	paid = frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": ["in", chain], "entry_type": "Goods Payout",
		"status": "Posted"}, ["name", "purchase_receipt"], as_dict=True)
	if paid:
		frappe.throw(_("{0} is paid in the system ({1} on {2}). Undo that payment first if the seller was paid another "
			"way.").format(doc.name, paid.name, paid.purchase_receipt))
	if doc.get(PAID_OUTSIDE_FIELD):
		frappe.throw(_("{0} is already marked as paid outside the system: {1}.").format(doc.name, doc.get(PAID_OUTSIDE_FIELD)))
	frappe.db.set_value("Purchase Receipt", doc.name, PAID_OUTSIDE_FIELD, note, update_modified=False)
	doc.add_comment("Info", _("Seller paid outside the system: {0} (marked by {1})").format(note, frappe.session.user))
	return {"purchase_receipt": doc.name, "note": note, "by": frappe.session.user, "on": str(now_datetime())}


def clear_paid_outside(receipt: str) -> dict:
	"""Take back a "paid outside the system" mark: the receipt is listed and payable again. Audited like the mark."""
	# The mark is honoured on the whole amendment line (``_paid_outside``), so it is cleared on the whole line: clearing
	# only the amendment would leave the cancelled original's mark refusing the payment.
	marked = frappe.get_all("Purchase Receipt", filters={
		"name": ["in", [r.name for r in receipt_chain(receipt)] or [""]], PAID_OUTSIDE_FIELD: ["is", "set"]},
		fields=["name", PAID_OUTSIDE_FIELD])
	if not marked:
		frappe.throw(_("{0} is not marked as paid outside the system.").format(receipt or _("That receipt")))
	for m in marked:
		frappe.db.set_value("Purchase Receipt", m.name, PAID_OUTSIDE_FIELD, None, update_modified=False)
		frappe.get_doc("Purchase Receipt", m.name).add_comment("Info", _(
			"Seller paid outside the system: mark cleared (was: {0}) by {1}").format(m.get(PAID_OUTSIDE_FIELD), frappe.session.user))
	return {"purchase_receipt": receipt, "cleared": True, "receipts": [m.name for m in marked]}


def receipts_to_pay(company: str, limit: int = 50) -> list[frappe._dict]:
	"""Receive Goods purchases from private sellers, received since cash tracking started, that owe the seller and have
	no seller payment standing -- never paid (received before payments were booked at receive, or made outside Receive
	Goods) or paid and undone -- newest first, in one query (the rule's last part, ``receipt_pays_seller``, is applied to
	its rows). A receipt whose goods are still paid on the receipt it amends, or marked as paid outside the system (it
	or the receipt it amends: ``mark_paid_outside``), is not listed. lazy: the newest ``limit``, and one amendment step
	back; widen if managers ever need more."""
	started = tracking_started()
	rows = frappe.db.sql(
		"""SELECT r.name, r.ffl_seller_customer AS customer, r.base_net_total AS amount, r.posting_date,
			EXISTS (SELECT 1 FROM `tabPurchase Receipt` x WHERE x.return_against = r.name AND x.is_return = 1
				AND x.docstatus = 1) AS has_return,
			r.ffl_acquisition_source, r.ffl_acquisition_type, r.ffl_transfer_for_customer, r.is_ffl_transfer,
			(SELECT e.payment_method FROM `tabCash Drawer Entry` e WHERE e.entry_type = 'Goods Payout'
				AND e.purchase_receipt IN (r.name, IFNULL(r.amended_from, '')) ORDER BY e.creation DESC LIMIT 1) AS method
		FROM `tabPurchase Receipt` r
		WHERE r.company = %(c)s AND r.docstatus = 1 AND IFNULL(r.is_return, 0) = 0
			AND r.ffl_acquisition_source = 'Individual' AND r.base_net_total > 0
			AND LOWER(TRIM(IFNULL(r.ffl_acquisition_type, ''))) IN %(types)s
			AND (%(started)s IS NULL OR r.creation >= %(started)s)
			AND NOT EXISTS (SELECT 1 FROM `tabCash Drawer Entry` e WHERE e.entry_type = 'Goods Payout' AND e.status = 'Posted'
				AND e.purchase_receipt IN (r.name, IFNULL(r.amended_from, '')))
			AND NOT EXISTS (SELECT 1 FROM `tabPurchase Receipt` o WHERE o.name IN (r.name, IFNULL(r.amended_from, ''))
				AND IFNULL(o.ffl_seller_paid_outside, '') != '')
		ORDER BY r.creation DESC LIMIT %(n)s""",
		{"c": company, "types": tuple(PAY_NOW_ACQUISITION_TYPES), "started": started, "n": cint(limit)}, as_dict=True)
	rows = [r for r in rows if receipt_pays_seller(r)]
	# A gun paid on its own (the old way), or goods returned to the seller, are not owed: such a receipt shows what it
	# still owes (``goods_payout_due``), and one that owes nothing is not listed. One more query; only those are read.
	with_own = set(frappe.db.sql_list(
		"""SELECT DISTINCT a.purchase_receipt FROM `tabFFL Acquisition` a JOIN `tabCash Drawer Entry` e
			ON e.acquisition = a.name AND e.entry_type = 'Payout' AND e.status = 'Posted'
		WHERE a.docstatus = 1 AND a.purchase_receipt IN %s""", (tuple(r.name for r in rows) or ("",),)))
	for r in rows:
		if r.name in with_own or cint(r.has_return):
			r.amount = goods_payout_due(frappe.get_doc("Purchase Receipt", r.name)).amount
	return [r for r in rows if flt(r.amount) > 0]


def unsettled_seller_receipts(from_date, to_date) -> list[frappe._dict]:
	"""Purchases from private sellers, dated in the range, whose Stock Received But Not Billed does not net to zero: the
	receipt's credit (less its submitted returns' debits) against the seller payment standing on it. Paid in full nets
	to zero; a seller still owed (never paid, or the payment undone) is negative; a payment left on a cancelled receipt
	is positive. The month-end check: the list must be empty. Receipts that pay their seller at receipt
	(``receipt_pays_seller``), received since cash tracking started and not marked paid outside; a gun paid on its own
	the old way (a Payout) counts as paid at its line's rate, as ``goods_payout_due`` leaves it out.
	[{name, docstatus, posting_date, customer, left}].

	Not the account's balance: dealer receipts are never billed, so SRBNB as a whole is not kept.
	Four queries whatever the range (the receipts, their returns, their payments, those vouchers' SRBNB lines), plus a
	read of each receipt with a gun paid the old way. lazy: the paid-outside mark is looked for one amendment step back,
	like receipts_to_pay, and a line for a non-stock item (paid, but no SRBNB credit) would show as settled-plus;
	upgrade path: walk the amendment chain / read the line's stock flag, if either ever turns up."""
	receipts = [r for r in frappe.db.sql(
		"""SELECT r.name, r.docstatus, r.posting_date, r.company, r.ffl_seller_customer AS customer,
			r.ffl_acquisition_source, r.ffl_acquisition_type, r.ffl_transfer_for_customer, r.is_ffl_transfer,
			EXISTS (SELECT 1 FROM `tabFFL Acquisition` a JOIN `tabCash Drawer Entry` e ON e.acquisition = a.name
				AND e.entry_type = 'Payout' AND e.status = 'Posted'
				WHERE a.purchase_receipt = r.name AND a.docstatus = 1) AS has_own
		FROM `tabPurchase Receipt` r
		WHERE r.docstatus IN (1, 2) AND IFNULL(r.is_return, 0) = 0 AND r.ffl_acquisition_source = 'Individual'
			AND r.posting_date BETWEEN %(from)s AND %(to)s
			AND (%(started)s IS NULL OR r.creation >= %(started)s)
			AND NOT EXISTS (SELECT 1 FROM `tabPurchase Receipt` o WHERE o.name IN (r.name, IFNULL(r.amended_from, ''))
				AND IFNULL(o.ffl_seller_paid_outside, '') != '')
		ORDER BY r.posting_date, r.name""",
		{"from": from_date, "to": to_date, "started": tracking_started()}, as_dict=True) if receipt_pays_seller(r)]
	if not receipts:
		return []
	names = tuple(r.name for r in receipts)
	# Each voucher that moves a receipt's SRBNB, and the receipt it belongs to: the receipt, its submitted returns, and
	# the seller payment standing on it.
	owner = {r.name: r.name for r in receipts}
	owner.update(frappe.db.sql("""SELECT name, return_against FROM `tabPurchase Receipt`
		WHERE return_against IN %s AND is_return = 1 AND docstatus = 1""", (names,)))
	owner.update(frappe.db.sql("""SELECT journal_entry, purchase_receipt FROM `tabCash Drawer Entry`
		WHERE entry_type = 'Goods Payout' AND status = 'Posted' AND purchase_receipt IN %s AND journal_entry IS NOT NULL""",
		(names,)))
	srbnb = {r.company: frappe.get_cached_value("Company", r.company, "stock_received_but_not_billed") for r in receipts}
	left = dict.fromkeys(names, 0.0)
	for voucher, account, net in frappe.db.sql("""SELECT voucher_no, account, SUM(debit - credit) FROM `tabGL Entry`
			WHERE voucher_no IN %s AND account IN %s AND is_cancelled = 0 GROUP BY voucher_no, account""",
			(tuple(owner), tuple(a for a in srbnb.values() if a) or ("",))):
		left[owner[voucher]] += flt(net)
	out = []
	for r in receipts:
		if r.has_own and r.docstatus == 1:
			left[r.name] += sum(e.amount for e in goods_payout_due(frappe.get_doc("Purchase Receipt", r.name)).excluded)
		if abs(left[r.name]) >= 0.005:
			out.append(frappe._dict(name=r.name, docstatus=r.docstatus, posting_date=r.posting_date, customer=r.customer,
				left=flt(left[r.name], 2)))
	return out


def note_goods_payout_on_cancel(doc, method=None) -> None:
	"""Purchase Receipt on_cancel: the seller's payment stays booked (the money left whatever the receipt says now), and
	the person cancelling is told what happens next."""
	paid = frappe.db.get_value(
		"Cash Drawer Entry", {"purchase_receipt": doc.name, "entry_type": "Goods Payout", "status": "Posted"}, "name")
	if paid:
		frappe.msgprint(_("The seller payment {0} for this receipt stays recorded. Amend this receipt and the amendment takes "
			"it over (when it owes the seller the same amount). If the seller gave the money back, undo it on the Cash Drawer "
			"page. Receiving the same goods again on a new receipt is refused while it stands.").format(paid),
			indicator="orange")


# --------------------------------------------------------------------------- #
# What moved the drawer (page + weekly report)
# --------------------------------------------------------------------------- #

# Plain English; translated where they are shown (``_()`` at import time has no language yet).
KIND_LABELS = {
	"sale_pos": "Cash sale (register)",
	"sale_counter": "Cash sale (counter order)",
	"order_payment": "Cash paid on an open order",
	"payment_in": "Customer payment",
	"refund": "Cash refund",
	"payout": "Seller paid for a gun",
	"trade_in_payout": "Customer paid for trade-ins",
	"goods_payout": "Seller paid for received goods",
	"deposit": "Bank deposit",
	"from_bank": "Cash added from bank",
	"owner": "Owner took cash (old entry)",
	"expense": "Cash expense",
	"variance": "Count difference",
	"cutoff": "First count",
	"cutoff_trueup": "Books corrected for days before the first count",
	"unclassified": "Other (not from the drawer screens)",
}


def _kind_of_remark(remark: str | None) -> str | None:
	text = (remark or "").strip()
	for prefix, kind in ((VARIANCE, "variance"), (CUTOFF, "cutoff"), (PAYOUT, "payout"), (DEPOSIT, "deposit"),
			(FROM_BANK, "from_bank"), (EXPENSE, "expense"), (OWNER, "owner"), (TRADE_IN_PAYOUT, "trade_in_payout"),
			(GOODS_PAYOUT, "goods_payout")):
		if text.startswith(prefix):
			return "cutoff_trueup" if kind == "cutoff" and "TRUE-UP" in text.upper() else kind
	return None


def cash_ledger_rows(company: str, *, from_date=None, to_date=None, created_after=None,
		account: str | None = None) -> list[frappe._dict]:
	"""Every non-cancelled Cash account line in the window, one row per sale / payment / entry, classified:
	{posting_date, creation, voucher_type, voucher_no, amount (into the drawer is +), kind, party, remark, entry,
	owner (who posted it)}."""
	account = account or cash_account(company)
	conditions, values = ["company = %s", "account = %s", "is_cancelled = 0"], [company, account]
	if from_date:
		conditions.append("posting_date >= %s")
		values.append(from_date)
	if to_date:
		conditions.append("posting_date <= %s")
		values.append(to_date)
	if created_after:
		conditions.append("creation > %s")
		values.append(created_after)
	rows = frappe.db.sql(
		f"""SELECT posting_date, creation, voucher_type, voucher_no, (debit - credit) AS amount, owner
		FROM `tabGL Entry` WHERE {' AND '.join(conditions)} ORDER BY posting_date, creation""", values, as_dict=True)
	by_type: dict[str, set] = {}
	for r in rows:
		by_type.setdefault(r.voucher_type, set()).add(r.voucher_no)

	def fetch(doctype, fields):
		names = by_type.get(doctype)
		if not names:
			return {}
		got = frappe.get_all(doctype, filters={"name": ["in", list(names)]}, fields=["name", *fields])
		return {g.name: g for g in got}

	invoices = fetch("Sales Invoice", ["is_return", "is_consolidated", "customer"])
	payments = fetch("Payment Entry", ["payment_type", "party"])
	journals = fetch("Journal Entry", ["user_remark"])
	entries = {}
	if journals:
		for e in frappe.get_all("Cash Drawer Entry", filters={"journal_entry": ["in", list(journals)]},
				fields=["name", "journal_entry", "entry_type", "acquisition", "serial", "reference", "withdrawer",
					"expense_account", "memo", "receipt", "payment_method", "amount", "creation", "invoice", "customer",
					"purchase_receipt"]):
			entries[e.journal_entry] = e
	# One line per sale or payment: a counter sale paid with $100 for $80 posts the $100 and the $20 change as two
	# ledger lines, and the change is part of the sale, not a refund.
	merged: dict[tuple, frappe._dict] = {}
	single = []
	for r in rows:
		if r.voucher_type in ("Sales Invoice", "Payment Entry"):
			key = (r.voucher_type, r.voucher_no)
			if key in merged:
				merged[key].amount = flt(merged[key].amount + r.amount, 2)
				merged[key].creation = max(merged[key].creation, r.creation)
				continue
			merged[key] = r
		single.append(r)
	rows = single
	for r in rows:
		r.amount = flt(r.amount, 2)
		r.party = r.remark = r.entry = None
		kind = "unclassified"
		if r.voucher_type == "Sales Invoice" and r.voucher_no in invoices:
			si = invoices[r.voucher_no]
			r.party = si.customer
			# A refund is a return / credit note; a sale that nets to nothing or less is not one.
			if si.is_return:
				kind = "refund"
			elif r.amount > 0:
				kind = "sale_pos" if si.is_consolidated else "sale_counter"
		elif r.voucher_type == "Payment Entry" and r.voucher_no in payments:
			pe = payments[r.voucher_no]
			r.party = pe.party
			kind = "refund" if pe.payment_type == "Pay" else ("payment_in" if pe.payment_type == "Receive" else "unclassified")
		elif r.voucher_type == "Journal Entry" and r.voucher_no in journals:
			r.remark = journals[r.voucher_no].user_remark
			kind = _kind_of_remark(r.remark) or "unclassified"
			r.entry = entries.get(r.voucher_no)
		r.kind = kind
	return rows


_LOG_MERGE_DAYS = 7  # lazy: a register shift left open longer than this before its merge keeps one merged line; widen if shifts ever run that long


def log_rows(company: str, account: str, from_date, to_date) -> tuple[float, list[frappe._dict]]:
	"""(opening balance, rows) for the cash log of ``from_date``..``to_date``, register sales one POS invoice each.

	The nightly merge books a shift's POS invoices as one Sales Invoice dated like the LAST of them, so a shift that
	crossed midnight lands on the next day in the ledger. The log dates every register sale by its own POS invoice
	instead -- before and after the merge alike, so a day's lines and balances never move. A merged invoice whose
	parts do not add up to its cash line stays one line on its own date."""
	ledger = cash_ledger_rows(company, from_date=from_date, to_date=add_days(to_date, _LOG_MERGE_DAYS), account=account)
	names = [r.voucher_no for r in ledger if r.voucher_type == "Sales Invoice"]
	merged = set(frappe.get_all("Sales Invoice", pluck="name", filters={"name": ["in", names or [""]], "is_consolidated": 1}))
	parts: dict[str, list] = {}
	if merged:
		for p in pending_pos_rows(company, account, merged_into=list(merged)):
			parts.setdefault(p.merged_into, []).append(p)
	opening = ledger_sum(company, account, before=from_date)
	rows = []
	for r in ledger:
		mine = parts.get(r.voucher_no)
		if mine and abs(sum(flt(p.amount) for p in mine) - flt(r.amount)) < 0.005:
			for p in mine:
				day = getdate(p.posting_date)
				if day < from_date:
					opening += flt(p.amount)  # sold before the window, merged inside it
				elif day <= to_date:
					rows.append(p)
		elif getdate(r.posting_date) <= to_date:
			rows.append(r)
	for p in pending_pos_rows(company, account):
		day = getdate(p.posting_date)
		if day < from_date:
			opening += flt(p.amount)
		elif day <= to_date:
			rows.append(p)
	rows.sort(key=lambda r: (str(r.posting_date), str(r.creation)))
	return flt(opening, 2), rows


def ledger_between(company: str, account: str, after, before, from_date=None) -> tuple[int, float]:
	"""(number of ledger lines, their net) on the Cash account created after ``after`` (None = from the start) up
	to ``before``: what the page leaves out of its list. ``from_date`` (the cut-off day) leaves out the history
	changes: lines dated before it and the true-ups that book them."""
	row = frappe.db.sql(
		f"""SELECT COUNT(*), IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry`
		WHERE company = %(c)s AND account = %(a)s AND is_cancelled = 0 AND creation <= %(b)s
			AND (%(after)s IS NULL OR creation > %(after)s)
			AND (%(d)s IS NULL OR (posting_date >= %(d)s
				AND NOT (voucher_type = 'Journal Entry' AND voucher_no IN ({_TRUEUP_JES}))))""",
		{"c": company, "a": account, "b": before, "after": after, "d": from_date})[0]
	return cint(row[0]), flt(row[1], 2)


def pending_pos_rows(company: str, account: str | None = None, created_after=None, created_before=None,
		merged_into: list[str] | None = None) -> list[frappe._dict]:  # lazy: scans unmerged POS invoices, which leave the set as they merge; index POS Invoice (company, consolidated_invoice) if it ever gets slow
	"""The cash of POS invoices not merged yet (tendered cash less change), one row each, same shape as
	``cash_ledger_rows``. The one place that SQL lives: ``drawer_expected`` sums these. A return is a refund.
	A draft counts once Take Payment stamped it (``stamp_draft_payment``: a part payment, or paid before the 4473
	clears), dated by that stamp; ERPNext posts nothing for a draft, yet the cash is in the drawer. Its submit keeps it
	here as a sale, so it is never counted twice.

	``merged_into`` (Sales Invoice names) instead lists the POS invoices already merged into those invoices, each with
	``merged_into``: the log shows a register's sales one by one, not as the night's merged total.
	lazy: consolidated_invoice has no index; a year's log scans POS Invoice once -- add an index if it gets slow."""
	account = account or cash_account(company)
	default_cash = frappe.db.get_value("Company", company, "default_cash_account") or account
	# Column-guarded: the app code is live before ``bench migrate`` adds the stamp. A draft is dated by its stamp, not by
	# posting_date, which ERPNext moves to today on every draft save.
	# lazy: a draft re-stamped by a later Take Payment lists all its cash at the new time, and its submit moves it to the
	# submit day in the log; booking Take Payment cash as a Payment Entry (needs a CPA ruling) would date each part once.
	taken = not merged_into and frappe.db.has_column("POS Invoice", "ffl_cash_taken_at")
	when = "IF(i.docstatus = 0, i.ffl_cash_taken_at, i.creation)" if taken else "i.creation"
	day = "IF(i.docstatus = 0, DATE(i.ffl_cash_taken_at), i.posting_date)" if taken else "i.posting_date"
	return frappe.db.sql(
		"""SELECT {day} AS posting_date, {when} AS creation, 'POS Invoice' AS voucher_type, i.name AS voucher_no,
			i.customer AS party, i.owner, i.consolidated_invoice AS merged_into,
			IF(i.docstatus = 0, 'order_payment', IF(i.is_return, 'refund', 'sale_pos')) AS kind, NULL AS remark, NULL AS entry,
			(IFNULL((SELECT SUM(p.amount) FROM `tabSales Invoice Payment` p WHERE p.parent = i.name
					AND p.parenttype = 'POS Invoice'
					AND (p.account = %(a)s OR (IFNULL(p.account, '') = '' AND p.mode_of_payment = 'Cash'))), 0)
				- IF(IFNULL(NULLIF(i.account_for_change_amount, ''), %(d)s) = %(a)s, i.change_amount, 0)) AS amount
		FROM `tabPOS Invoice` i
		WHERE i.company = %(c)s AND {status} AND {merged}
			AND (%(after)s IS NULL OR {when} > %(after)s) AND (%(before)s IS NULL OR {when} <= %(before)s)
		HAVING amount != 0 ORDER BY creation""".format(
			day=day, when=when,
			status="(i.docstatus = 1 OR (i.docstatus = 0 AND i.ffl_cash_taken_at IS NOT NULL))" if taken else "i.docstatus = 1",
			merged="i.consolidated_invoice IN %(into)s" if merged_into else "IFNULL(i.consolidated_invoice, '') = ''"),
		{"a": account, "d": default_cash, "c": company, "after": created_after, "before": created_before,
			"into": tuple(merged_into or ("",))}, as_dict=True)


def entry_detail(entry, seller: str | None = None) -> str:
	"""The one-line description of a cash entry, for the page and the weekly report."""
	if not entry:
		return ""
	if entry.entry_type in ("Payout", "Payout Adjustment"):
		return " ".join(p for p in (entry.acquisition, entry.serial, seller, f"({entry.payment_method})") if p)
	if entry.entry_type in ("Deposit", "From Bank"):
		return entry.reference or ""
	if entry.entry_type == "Trade-in Payout":
		return " ".join(p for p in (entry.invoice, entry.customer, f"({entry.payment_method})", entry.reference) if p)
	if entry.entry_type == "Goods Payout":
		return " ".join(p for p in (entry.purchase_receipt, entry.customer, f"({entry.payment_method})", entry.reference) if p)
	if entry.entry_type == "Owner Draw":  # an old one
		return " · ".join(p for p in (entry.withdrawer, entry.memo) if p)
	account = frappe.get_cached_value("Account", entry.expense_account, "account_name") or entry.expense_account
	return f"{account}: {entry.memo or ''}{' (receipt attached)' if entry.receipt else ''}"
