"""Consolidate a firearm POS Invoice the moment the sale completes.

ERPNext POS Invoices defer ALL stock + GL posting to consolidation, which
normally happens in a batch at POS closing (see api.pos_close). That means a
sold/transferred firearm stays in ERPNext inventory until the nightly close —
unacceptable for an FFL, where the gun must leave the books the instant it's
dispositioned (4473 done).

So: when a POS Invoice that contains a firearm is submitted (which, for a
firearm, can only happen AFTER the 4473 + NICS gate clears — see
compliance.firearm), we consolidate THAT one invoice immediately by building +
submitting a one-invoice `POS Invoice Merge Log` — the exact doc ERPNext's
nightly close creates (create_merge_logs). Its on_submit creates + submits the
consolidated Sales Invoice, which posts the stock ledger entry now, so the
serial goes out at sale.

We build the merge log directly (rather than calling
`consolidate_pos_invoices([invoice])`) because that helper only fills the merge
log's `company` from a POS Closing Entry — with no closing entry it sets
company=None, and the merge log's `company` is mandatory, so it would throw on
every real sale. We set `company` from the POS Invoice instead.

This is safe alongside the nightly POS close: the closing entry's invoice query
excludes already-consolidated POS Invoices (build_invoice_query filters
`IfNull(consolidated_invoice,'')==''`), so the gun's invoice is never
double-consolidated. Non-firearm sales (ammo, accessories) are untouched and
still consolidate at the nightly close; if that close leaves one behind (it is
owner-scoped — see _find_stuck_pos_invoices), the periodic sweep picks it up
the next day. The duplicate-receipt guard in receipts.send_sale_receipt
already skips the consolidated Sales Invoice.
"""
from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, flt, getdate, now_datetime, nowdate, nowtime

from ffl_core.compliance.firearm import canonicalize_serial_string
from ffl_core.roles import COUNTER_ROLES


def consolidate_firearm_invoice_on_submit(doc, method: str | None = None) -> None:
	"""POS Invoice on_submit hook: if the invoice sells/transfers a firearm,
	consolidate it immediately (in the background, after the submit commits) so
	the gun leaves ERPNext stock at sale rather than at the nightly close.

	A negative trade-in ticket (the store paid the customer the difference)
	merges at once too: its payout is set against the merged invoice there
	(compliance/trade_in_settlement), and a lone merge keeps it out of the
	nightly close's per-customer batch.

	No-op for already-consolidated invoices, returns, and other non-firearm sales
	(those keep flowing through the nightly POS close unchanged)."""
	if doc.get("is_consolidated") or doc.get("consolidated_invoice"):
		return
	if doc.get("is_return"):
		return  # a return puts a gun back, not out — no FFL stock-out urgency
	if not (_has_firearm_line(doc) or flt(doc.get("grand_total")) < 0):
		return
	frappe.enqueue(
		"ffl_core.api.pos_consolidate.consolidate_pos_invoice_now",
		queue="short",
		enqueue_after_commit=True,
		pos_invoice=doc.name,
	)


@frappe.whitelist()
def consolidate_pos_invoice_now(pos_invoice: str) -> dict:
	"""Consolidate one submitted POS Invoice immediately (out-of-band).

	Out-of-band from the POS closing batch, so its stock + Sales Invoice
	post now — a sold serialized gun must leave stock at sale, not at the
	nightly close. Idempotent: no-ops if the invoice is missing, not
	submitted, already consolidated, or a return whose original sale is
	not yet consolidated (a return becomes a credit note against the
	original's consolidated Sales Invoice, so the original must go first —
	the sweep orders them that way; see reconsolidate_stuck_pos_invoices).
	A merge that loses a row race to a concurrent write, or times out waiting
	for a lock, is rolled back and run again: 3 attempts in all.

	Args:
	    pos_invoice: Submitted POS Invoice name.

	Returns:
	    {consolidated: bool, pos_invoice, [sales_invoice]}.

	Permissions:
	    Sales User / Sales Manager / Stock Manager / System Manager.
	"""
	# Whitelisted, so guard it: POS staff only (the on_submit enqueue runs this as
	# the cashier — Sales User — and the cron/admin path bypasses via only_for).
	frappe.only_for(COUNTER_ROLES)
	# A retry rolls the whole transaction back, so it is only safe while the caller
	# has nothing pending in it — true of both callers (the on_submit job, and the
	# sweep, which commits each invoice). A caller with writes pending gets the
	# error instead of having them silently discarded.
	retry_safe = not frappe.db.transaction_writes
	attempt = 1
	while True:
		try:
			return _consolidate_once(pos_invoice)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			# MariaDB 1020 "Record has changed since last read" / 1213 deadlock / 1205
			# lock wait timeout: a row the merge writes was committed by another
			# transaction after this one's snapshot, or stayed locked by one — on a
			# counter sale, the Woo serial push stamping Serial No.woo_synced_at while
			# the merge flips the same row to Delivered, or a
			# concurrent Sales Invoice bumping the Company's monthly-sales total.
			# frappe.db.sql wraps these codes in QueryDeadlockError/QueryTimeoutError,
			# neither a db.InternalError, so execute_job's own deadlock retry never
			# fires and the sale sat on the books until the 15-min sweep. A full
			# rollback ends the transaction, so the next attempt reads a fresh
			# snapshot (a savepoint would keep the stale one). The last attempt's
			# error goes to the caller, whose failure path rolls back.
			if not retry_safe or attempt == _ROW_RACE_ATTEMPTS:
				raise
			frappe.db.rollback()
			time.sleep(attempt)
			attempt += 1


# Merge attempts before a lost row race goes to the caller (the job's Error Log,
# or the sweep's failure path); the back-off between them is 1 s, then 2 s.
_ROW_RACE_ATTEMPTS = 3


def _consolidate_once(pos_invoice: str) -> dict:
	"""One attempt of consolidate_pos_invoice_now, in the caller's transaction:
	lock + re-check the invoice, build + submit its one-invoice merge log, commit."""
	# for_update locks the POS Invoice row for the duration of this consolidation:
	# a concurrent caller (the */15 sweep racing the nightly close, or a double
	# enqueue) blocks, then re-reads consolidated_invoice as set and no-ops —
	# closing the TOCTOU window that would otherwise build two merge logs / SIs.
	inv = frappe.db.get_value(
		"POS Invoice", pos_invoice,
		["name", "company", "customer", "posting_date", "grand_total", "is_return",
		 "return_against", "consolidated_invoice", "docstatus"],
		as_dict=True,
		for_update=True,
	)
	if not inv or inv.docstatus != 1 or inv.consolidated_invoice:
		return {"consolidated": False, "pos_invoice": pos_invoice}
	if inv.is_return and not (inv.return_against and frappe.db.get_value(
			"POS Invoice", inv.return_against, "consolidated_invoice")):
		# A return consolidates into a credit note against its ORIGINAL's
		# consolidated Sales Invoice (ERPNext's merge log insists the original is
		# Consolidated first — validate_pos_invoice_status), so until the original
		# has gone through there is nothing to build against. Leave it: the sweep
		# lists originals before returns and re-offers the return next run.
		return {"consolidated": False, "pos_invoice": pos_invoice, "waiting_on": inv.return_against}

	# Build + submit the one-invoice merge log directly with `company` set (the
	# field is mandatory; ERPNext only fills it from a POS Closing Entry, so the
	# no-closing-entry consolidate_pos_invoices path would set it None and throw).
	# This mirrors create_merge_logs(); its on_submit posts the consolidated
	# Sales Invoice + stock ledger entry, taking the serial out at sale.
	_repair_serial_case(pos_invoice)
	# ERPNext's merge reads the invoice with get_cached_doc and moves that object's tax and payment rows onto the new
	# Sales Invoice, renaming them. A rollback (a lost row race above) does not undo that on the cached object, so a
	# retry would merge the renamed rows and fail on the tax details ("KeyError: <tax row>"). Read it afresh.
	frappe.clear_document_cache("POS Invoice", pos_invoice)
	merge_log = frappe.new_doc("POS Invoice Merge Log")
	merge_log.posting_date = inv.posting_date
	merge_log.posting_time = nowtime()
	merge_log.merge_invoices_based_on = "Customer"  # reqd; we group this one customer
	merge_log.company = inv.company
	merge_log.customer = inv.customer
	merge_log.append("pos_invoices", {
		"pos_invoice": inv.name,
		"customer": inv.customer,
		"posting_date": inv.posting_date,
		"grand_total": inv.grand_total,
		"is_return": inv.is_return,
		"return_against": inv.return_against,
	})
	merge_log.flags.ignore_permissions = True
	merge_log.save()
	merge_log.submit()
	frappe.db.commit()

	si = frappe.db.get_value("POS Invoice", pos_invoice, "consolidated_invoice")
	return {"consolidated": bool(si), "pos_invoice": pos_invoice, "sales_invoice": si}


def _repair_serial_case(pos_invoice: str) -> None:
	"""Rewrite this submitted POS Invoice's line serials to the stored Serial No
	case before merging. Lines rung before the validate-time canonicalizer
	shipped (or that slipped past it) would otherwise fail every retry with
	ERPNext's case-sensitive bundle check — see compliance.firearm.
	canonical_serial_no. Plain db write on a child row: no ledger, no GL."""
	rows = frappe.get_all("POS Invoice Item",
		filters={"parent": pos_invoice, "serial_no": ["!=", ""]},
		fields=["name", "serial_no"])
	for r in rows:
		fixed = canonicalize_serial_string(r.serial_no)
		if fixed != r.serial_no:
			frappe.db.set_value("POS Invoice Item", r.name, "serial_no", fixed,
				update_modified=False)


def _has_firearm_line(doc) -> bool:
	"""True if any line is a firearm Item (antiques included). Immediate
	consolidation is an INVENTORY concern — a serialized gun must leave stock at
	sale, not at the nightly close, or the same serial can be sold twice in that
	window. Antiques are serialized firearms too, so they take this path; their
	bound-book/4473 exemptions are enforced separately via is_regulated_firearm_item."""
	from ffl_core.firearm import is_firearm_item

	return any(is_firearm_item(row.get("item_code")) for row in (doc.get("items") or []))


# --------------------------------------------------------------- safety net
# Immediate consolidation runs in a background job after the sale commits, so a
# failure there is invisible: the POS Invoice stays "Paid" and a sold firearm
# silently never leaves stock (the SEV that stranded a real sale when the
# consolidated Sales Invoice rejected a negative trade-in line). The nightly
# close is no backstop either — it's per-opening, one-shot, and owner-scoped, so
# it skips another clerk's invoice on a shared register and never revisits a
# closed shift. This sweep is the backstop: retry every stranded invoice and, if
# it still won't consolidate, raise a visible ToDo instead of burying it.

# Only sweep invoices that have "settled" (older than this) so the periodic run
# never races the on_submit immediate-consolidation enqueue on a fresh sale.
_STUCK_SETTLE_MINUTES = 10

# Bounds for the "missed close" branch of _find_stuck_pos_invoices. LOOKBACK: how
# far back a leftover is still recent enough to back-post automatically — older
# than that, which accounting period it belongs to is a human's call (the rollout
# enumeration of prod found nothing unconsolidated once the four known invoices
# were fixed by hand). DEAD_SHIFT: how long an Open opening keeps holding its
# invoices before it is treated as abandoned. BATCH: invoices per run — the
# scheduler fires every 15 min, so a backlog drains at 200/h instead of in one
# unbounded burst of back-dated stock/GL postings. MAX_ALERTS: first-time ToDos
# (each notifies every manager) per run; the rest are summarised in one log.
_SWEEP_LOOKBACK_DAYS = 30
_SWEEP_DEAD_SHIFT_DAYS = 3
_SWEEP_BATCH = 50
_SWEEP_BATCH_MAX = 500  # ceiling for a manual sweep's `batch` argument
_SWEEP_MAX_ALERTS_PER_RUN = 5


@frappe.whitelist()
def reconsolidate_stuck_pos_invoices(older_than_minutes: int | None = None,
		batch: int | None = None) -> dict:
	"""Retry every submitted POS Invoice that never consolidated (the sweep).

	Covers two ways a sale gets stranded: a firearm invoice whose immediate
	on-submit consolidation failed (always swept, never capped), and any
	invoice — returns included — from the last 30 days that the nightly
	POS close left behind (the close is owner-scoped, so a sale rung up by
	a clerk other than the shift opener is skipped — see
	_find_stuck_pos_invoices). The missed-close part works in batches of
	50 per run, originals before their returns, invoices already carrying
	a stuck ToDo last; idempotent per invoice (consolidate_pos_invoice_now
	no-ops the already-done ones). A failure is logged AND surfaced as a
	ToDo (at most 5 new ToDos per run, plus one summary log when more
	failed); a return held for an original the sweep can never reach is
	surfaced the same way. Each run then closes those ToDos for every
	invoice that has merged since, however it merged. Runs from the
	scheduler (hooks.scheduler_events) and is whitelisted for an on-demand
	sweep.

	Args:
	    older_than_minutes: Only sweep invoices older than this settle
	        window (default 10), so the sweep never races the on-submit
	        enqueue on a fresh sale.
	    batch: Missed-close invoices to take this run (default 50, clamped
	        to 1–500) — for a manual sweep of a known backlog.

	Returns:
	    {checked, consolidated: [names], failed: [names], held: [names]}
	    — held = returns waiting for their original to consolidate first.

	Permissions:
	    Sales Manager / Stock Manager / System Manager.
	"""
	# Scheduler runs as Administrator (only_for bypasses); guard the manual path.
	frappe.only_for(("Sales Manager", "Stock Manager", "System Manager"))
	minutes = int(older_than_minutes if older_than_minutes is not None else _STUCK_SETTLE_MINUTES)
	names = _find_stuck_pos_invoices(
		minutes, batch=max(1, min(int(batch), _SWEEP_BATCH_MAX)) if batch is not None else None)
	consolidated: list[str] = []
	failed: list[str] = []
	held: list[str] = []
	alerted = 0
	for name in names:
		try:
			out = consolidate_pos_invoice_now(name)
			if out.get("consolidated"):
				consolidated.append(name)
			elif "waiting_on" in out:
				# A return waiting for its original. Normal when the original is
				# earlier in this same run (or in tonight's close); NOT normal when
				# the original is beyond this sweep's reach — then nobody will ever
				# unblock it, so say so instead of re-holding it silently forever.
				held.append(name)
				if (_original_out_of_reach(out.get("waiting_on"))
						and alerted < _SWEEP_MAX_ALERTS_PER_RUN
						and _alert_stuck(name, reason=(
							f"return is waiting on original {out.get('waiting_on')!r}, which this "
							f"sweep can never consolidate (missing, cancelled, or older than "
							f"{_SWEEP_LOOKBACK_DAYS} days) — consolidate or cancel it by hand"))):
					alerted += 1
					frappe.db.commit()
		except Exception:
			# The failed consolidate left a partial write (a submitted merge log,
			# maybe a draft consolidated SI + serial bundle) pending in the shared
			# transaction — roll it back so a later invoice's commit (or the job-end
			# commit) doesn't persist the orphan. Earlier successes already committed
			# (consolidate_pos_invoice_now commits each), so this undoes ONLY the
			# current failure. Commit the alert afterwards so a later failure's
			# rollback can't drop it. One bad invoice must not abort the rest.
			frappe.db.rollback()
			failed.append(name)
			if held and held[-1] == name:
				held.remove(name)  # the held-branch alert itself threw: failed, not both
			# Cap the fan-out: a backlog failing en masse (say, a closed
			# accounting period) must not become one High ToDo + email per
			# invoice per run. _alert_stuck dedups per invoice, so the cap only
			# spreads first-time alerts across runs; nothing is lost.
			if alerted < _SWEEP_MAX_ALERTS_PER_RUN and _alert_stuck(name):
				alerted += 1
			frappe.db.commit()
	if len(failed) > _SWEEP_MAX_ALERTS_PER_RUN:
		frappe.log_error(
			title=f"POS sweep: {len(failed)} invoices failed to consolidate this run",
			message=(
				f"New ToDos this run: {alerted} (cap {_SWEEP_MAX_ALERTS_PER_RUN}); the rest "
				"already have an open ToDo or get theirs on a later run. Failed: "
				+ ", ".join(failed)
			),
		)
	_close_healed_alerts()
	return {"checked": len(names), "consolidated": consolidated, "failed": failed, "held": held}


def _original_out_of_reach(original: str | None) -> bool:
	"""True when a held return's original can never be consolidated by this
	sweep — missing, cancelled/draft, or dated before the lookback floor — so
	waiting for it is pointless and a human has to look."""
	if not original:
		return True
	row = frappe.db.get_value("POS Invoice", original, ["docstatus", "posting_date"], as_dict=True)
	if not row or row.docstatus != 1:
		return True
	return getdate(row.posting_date) < getdate(add_days(nowdate(), -_SWEEP_LOOKBACK_DAYS))


# A negative trade-in ticket whose merge a manager cancelled is on its way to being cancelled itself (ERPNext
# makes the merge go first; the ticket's cancel then voids its payout): re-merging it would undo that. Its
# un-merge is deliberate, so neither sweep branch takes it; consolidate_pos_invoice_now still does when called.
_UNMERGED_ON_PURPOSE = """pi.grand_total < 0 AND EXISTS (
		    SELECT 1 FROM `tabPOS Invoice Reference` ref
		    INNER JOIN `tabPOS Invoice Merge Log` ml ON ml.name = ref.parent
		    WHERE ref.pos_invoice = pi.name AND ml.docstatus = 2
		  )"""


def _find_stuck_pos_invoices(older_than_minutes: int, batch: int | None = None) -> list[str]:
	"""Submitted POS Invoices with no consolidated Sales Invoice that are either

	  (a) a non-return FIREARM sale (Item.is_firearm OR a legacy firearm Item
	      Group, mirroring is_firearm_item) or negative trade-in ticket
	      (grand_total < 0) created longer ago than the settle window — these
	      consolidate immediately on submit (consolidate_firearm_invoice_on_submit),
	      so only an async failure can strand one; or
	  (b) ANY invoice — returns included — dated before today and within the
	      last _SWEEP_LOOKBACK_DAYS, whose register (pos_profile) has no
	      still-Open, non-abandoned POS Opening Entry that started at or before
	      the sale — i.e. the nightly close for that shift already ran (or can
	      never run) and left the invoice behind.

	(b) exists because ERPNext's POS Closing Entry is owner-scoped: its invoice
	query filters `POS Invoice.owner == opening.user`
	(pos_closing_entry.py, build_invoice_query) and its validate even rejects
	another clerk's invoice ("POS Invoice isn't created by user …"). On a shared
	register opened by one account and rung up by another, every non-firearm
	sale — and every return — is skipped by the close and, the close being
	one-shot per opening, never revisited: the sale shows in the Sales Report
	(which reads POS Invoices) but its stock and GL never post, and nothing
	alerts. Real incident 2026-08-21/25: four accessory/ammo sales sat "Paid"
	for days with stock still on hand.

	Two queries, on purpose. (a) is the compliance path — a sold serial must
	leave stock — and its size is naturally bounded by "same-day immediate
	consolidations that failed", so it is NEVER capped. (b) is bounded, and a
	shared cap would let a backlog of permanently failing past-day invoices
	(a closed accounting period, say) sit at the head of every batch and
	silently starve (a) — invoices never tried never fail, so never alert.
	(a) rows come first in the result.

	Bounds and ordering of (b):
	  - An invoice is HELD while an Open, non-abandoned opening on its register
	    started at or before the sale AND was opened by the invoice's own owner:
	    that shift's close hasn't run yet (e.g. the 23:00 cron missed a night)
	    and WILL take the invoice. The owner test is the whole point — the close
	    is owner-scoped, so a shift opened by someone else can never take it,
	    and holding for that close just delays the sale a day for nothing
	    (2026-08-26: the 8/25 incident recurred the next day — cashier A rang up
	    on the register cashier B had opened — and this sweep left it until the
	    morning).
	  - Same-day invoices are otherwise left to tonight's close (its cash
	    reconciliation is not pre-empted); the one exception is the case above —
	    a same-day sale under ANOTHER clerk's open shift is swept as soon as the
	    settle window passes, because no close owns it.
	  - An opening left Open longer than _SWEEP_DEAD_SHIFT_DAYS is treated as
	    abandoned and holds nothing, so a dead shift can't bury sales silently
	    (auto_close_open_pos only logs its own failures). Consequence: invoices
	    taken from an abandoned shift no longer count toward that shift's
	    expected amounts if its POS Closing Entry is submitted later. So the
	    worst-case delay for a missed sale is the next day, or
	    _SWEEP_DEAD_SHIFT_DAYS days while its shift stays Open.
	  - Nothing older than _SWEEP_LOOKBACK_DAYS is back-posted automatically —
	    that far back the question is which accounting period it belongs to,
	    not whether to post it.
	  - Order: originals before returns (is_return ASC) so a return meets its
	    original consolidated within one run; within each of those groups,
	    invoices that already carry an open stuck ToDo LAST (tried, alerted, in
	    someone's hands — they must not keep occupying the slots of invoices
	    nobody has looked at yet); then chronological. At most `batch` (default
	    _SWEEP_BATCH) rows.
	  - The Open-shift guard deliberately mirrors ERPNext's closing-entry query
	    (build_invoice_query: Timestamp(posting_date, posting_time) window +
	    pos_profile equality). An invoice with a NULL pos_profile or posting_time
	    matches no opening here AND can enter no closing entry there, so the
	    sweep is its only path; the two predicates must move together.

	Neither branch takes a negative trade-in ticket whose merge log was cancelled
	(_UNMERGED_ON_PURPOSE): a manager is cancelling it.

	Settle on `creation`, not `modified`, so an unrelated edit to a stuck invoice
	can't keep resetting its retry clock."""
	from ffl_core.firearm import ALL_FIREARM_GROUPS, firearm_item_sql

	now = now_datetime()
	today = nowdate()
	cutoff = add_to_date(now, minutes=-older_than_minutes)
	firearm = frappe.db.sql(
		"""
		SELECT pi.name
		FROM `tabPOS Invoice` pi
		WHERE pi.docstatus = 1
		  AND pi.is_return = 0
		  AND COALESCE(pi.consolidated_invoice, '') = ''
		  AND pi.creation < %(cutoff)s
		  AND NOT ({unmerged_on_purpose})
		  AND (
		    pi.grand_total < 0  -- a negative trade-in ticket merges on submit as well
		    OR EXISTS (
		      SELECT 1 FROM `tabPOS Invoice Item` pii
		      INNER JOIN `tabItem` it ON it.name = pii.item_code
		      WHERE pii.parent = pi.name
		        AND {firearm}
		    )
		  )
		ORDER BY pi.posting_date, pi.posting_time, pi.creation
		""".format(firearm=firearm_item_sql("it"), unmerged_on_purpose=_UNMERGED_ON_PURPOSE),
		{"cutoff": cutoff, "firearm_groups": tuple(ALL_FIREARM_GROUPS)},
	)
	missed = frappe.db.sql(
		"""
		SELECT pi.name
		FROM `tabPOS Invoice` pi
		WHERE pi.docstatus = 1
		  AND COALESCE(pi.consolidated_invoice, '') = ''
		  AND pi.creation < %(cutoff)s
		  AND pi.posting_date >= %(floor)s
		  AND NOT ({unmerged_on_purpose})
		  AND (
		    pi.posting_date < %(today)s
		    OR EXISTS (
		      -- same day, but the register's open shift belongs to another clerk:
		      -- tonight's owner-scoped close will skip this invoice, so take it now
		      SELECT 1 FROM `tabPOS Opening Entry` poe
		      WHERE poe.docstatus = 1
		        AND poe.status = 'Open'
		        AND poe.pos_profile = pi.pos_profile
		        AND poe.period_start_date <= TIMESTAMP(pi.posting_date, pi.posting_time)
		        AND poe.period_start_date >= %(dead_shift)s
		        AND poe.user <> pi.owner
		    )
		  )
		  AND NOT EXISTS (
		    -- "the register's live shift covering this sale" — same rule, in SQL, as
		    -- pos_session.open_shift_for_profile (which warn_owner_shift_mismatch uses
		    -- at the counter); change the two together.
		    SELECT 1 FROM `tabPOS Opening Entry` poe
		    WHERE poe.docstatus = 1
		      AND poe.status = 'Open'
		      AND poe.pos_profile = pi.pos_profile
		      AND poe.period_start_date <= TIMESTAMP(pi.posting_date, pi.posting_time)
		      AND poe.period_start_date >= %(dead_shift)s
		      AND poe.user = pi.owner
		  )
		ORDER BY
		  pi.is_return,
		  EXISTS (
		    SELECT 1 FROM `tabToDo` td
		    WHERE td.reference_type = 'POS Invoice'
		      AND td.reference_name = pi.name
		      AND td.status = 'Open'
		  ),
		  pi.posting_date, pi.posting_time, pi.creation
		LIMIT %(batch)s
		""".format(unmerged_on_purpose=_UNMERGED_ON_PURPOSE),
		{
			"cutoff": cutoff,
			"today": today,
			"floor": add_days(today, -_SWEEP_LOOKBACK_DAYS),
			"dead_shift": add_to_date(now, days=-_SWEEP_DEAD_SHIFT_DAYS),
			"batch": int(batch if batch is not None else _SWEEP_BATCH),
		},
	)
	out = [r[0] for r in firearm]
	seen = set(out)
	out.extend(r[0] for r in missed if r[0] not in seen)
	return out


def warn_owner_shift_mismatch(doc, method=None) -> None:
	"""POS Invoice validate (submit only): tell the clerk when the register's
	open shift belongs to another account.

	ERPNext's POS Closing Entry only merges the shift opener's OWN invoices, so
	a sale rung up under a different login is skipped by tonight's close — its
	stock and GL post later, via the sweep (see _find_stuck_pos_invoices).
	Twice in two days (8/25, 8/26) that was the answer to "we sold it and the
	system still shows it". Never blocks the sale (the customer is at the
	counter); the alert is the nudge toward the operating rule — open the
	shift and ring sales under the same account."""
	if cint(doc.docstatus) != 1 or not cint(doc.get("is_pos")) or not doc.get("pos_profile"):
		return
	from ffl_core.api.pos_session import open_shift_for_profile

	clerk = doc.get("owner") or frappe.session.user
	# Same bounds as the sweep's covering-shift rule (_find_stuck_pos_invoices):
	# a shift that started after this sale, or one abandoned for longer than the
	# dead-shift window, does not "own" the sale — and then the sweep's same-day
	# branch would not take it either, so promising the sweep would be wrong.
	shift = open_shift_for_profile(
		doc.pos_profile, at=now_datetime(), dead_after_days=_SWEEP_DEAD_SHIFT_DAYS)
	if not shift or shift.user == clerk:
		return
	frappe.msgprint(
		_(
			"Register {0} is open under {1}, but this sale is rung up as {2}. "
			"Tonight's POS close only merges the opener's own sales, so this one "
			"posts its stock and ledger later through the 15-minute sweep. Open the "
			"shift and ring sales under the same account."
		).format(doc.pos_profile, shift.user, clerk),
		alert=True, indicator="orange",
	)


def _alert_stuck(pos_invoice: str, reason: str | None = None) -> bool:
	"""On the FIRST sweep that finds an invoice stuck, log the cause once and
	ASSIGN a high-priority ToDo to the managers (``ffl_core.alerts.alert_once`` —
	deduped on an existing open ToDo for the invoice, so a persistently-stuck
	invoice is logged + notified once, not on every 15-min run). Returns True
	when this call raised a new alert, False when the invoice already had one —
	the sweep counts only new alerts against its per-run cap."""
	from ffl_core.alerts import alert_once

	return alert_once(
		"POS Invoice", pos_invoice,
		title=f"POS Invoice {pos_invoice} stuck unconsolidated",
		message=reason or frappe.get_traceback()
			or f"consolidate_pos_invoice_now failed for {pos_invoice}",
		description=(
			f"POS Invoice {pos_invoice} {_STUCK_TODO_MARK} — its sale (and "
			"any firearm stock-out) has not posted to the books. "
			+ (f"Cause: {reason} " if reason else "Check the Error Log for the cause; ")
			+ "it retries automatically every 15 min, or run "
			"Reconsolidate Stuck POS Invoices once the cause is resolved."
		),
	)


# In every stuck alert's ToDo description — _close_healed_alerts finds them by it.
_STUCK_TODO_MARK = "could not be consolidated"


def _close_healed_alerts() -> None:
	"""Close the stuck ToDos (_alert_stuck) of every POS Invoice that has merged
	since — by this sweep, a later one, the nightly close or a manual run.
	alert_once never closes them, so a healed sale kept one open High ToDo per
	manager. Another alert on the same invoice (an
	abandoned-4473 ToDo) lacks the mark and stays open. Housekeeping at the end
	of the sweep: a failure is logged, never raised."""
	try:
		names = frappe.db.sql(
			"""
			SELECT td.name FROM `tabToDo` td
			JOIN `tabPOS Invoice` pi ON pi.name = td.reference_name
			WHERE td.reference_type = 'POS Invoice' AND td.status = 'Open'
			  AND td.description LIKE %(mark)s
			  AND IFNULL(pi.consolidated_invoice, '') != ''
			""",
			{"mark": f"%{_STUCK_TODO_MARK}%"}, pluck=True)
		for name in names:
			todo = frappe.get_doc("ToDo", name)
			todo.status = "Closed"
			# A save, not db.set_value: ToDo.on_update drops the manager from the
			# invoice's _assign (the list's Assigned To filter + avatars). Not
			# assign_to.set_status either — that also notifies every manager.
			todo.save(ignore_permissions=True)
		if names:
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.logger("ffl_core.pos_consolidate", allow_site=True).error(
			f"Closing healed POS stuck ToDos failed: {frappe.get_traceback()}")
