"""Freeze the effective consignment commission % onto the invoice line, and
block returns of consignment sales.

compliance/purchase.py stamps is_consignment / consignment_commission_pct
onto the Serial No at intake — that's the store-wide/serial-level default.
The commission actually owed on a GIVEN sale can differ (a manager overrides
it per sale with a different rate), so the per-line consignment_commission_pct
(POS Invoice Item / Sales Invoice Item) is left editable and
defaults from the serial when the operator leaves it blank OR enters 0 — a 0
counts as "unset", never a real 0% (a 0%-commission consignment
sale does not exist; see ffl_core.consignment.effective_commission_pct).

This validate-time freeze makes that resolution permanent on the line: after
this runs, every consignment line carries a non-null consignment_commission_pct,
so later reporting and GL booking read one settled value instead of
re-deriving it from the Serial No (which is mutable and shouldn't retroactively
change the commission on an already-sold line).

Wired into POS Invoice + Sales Invoice `before_validate` (hooks.py) rather
than `validate`, where the other consignment/trade-in gates sit: ERPNext's
own core validate() (calculate_taxes_and_totals -> round_floats_in) casts
every Currency/Float/Percent field on each item row through flt(), turning a
truly-blank consignment_commission_pct (None) into 0.0. Under the unified
0-semantics above that coercion is harmless — a blank and an explicit 0 both
resolve to the serial rate — but before_validate still fires earlier in
Document.run_before_save_methods(), ahead of that pass, so the resolved
commission lands on the row as soon as the gun is added to the (draft) cart,
before core recalculation (the same reason Item.before_validate runs
sync_upc_barcode — see hooks.py).

Unlike the submit-only 4473/trade-in gates, freeze_consignment_commission
mostly doesn't reject anything — it only fills a blank — so it deliberately
runs on every save (draft included, before_validate even more so): the POS
cart should show the resolved commission as soon as the gun is added, not
only after Complete Order. The one thing it does reject is an out-of-range
resolved %: a valid consignment commission is (0, 100] (100% = store keeps
all, consignor owed 0), so a typo'd override (e.g. 200 instead of 20) — or a
resolved 0 from a mis-stamped serial with no usable rate — must not silently
freeze onto the line and flow into GL/reporting.

block_consignment_returns, below, is a genuine submit/validate-time gate (see
its own docstring) — owner policy is that consignment sales are never
returnable, so it throws rather than filling anything in.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.consignment import effective_commission_pct
from ffl_core.serials import serials_for_line


def freeze_consignment_commission(doc, method: str | None = None) -> None:
	for row in (doc.items or []):
		consigned = []  # (serial, own rate) for every consigned gun on this line
		for serial in _line_serials(row):
			info = frappe.db.get_value(
				"Serial No", serial,
				["is_consignment", "consignment_commission_pct"], as_dict=True,
			)
			if info and info.is_consignment:
				consigned.append((serial, flt(info.consignment_commission_pct)))
		if not consigned:
			# A % on a line whose gun is not consigned means nothing to the payable,
			# but it would say "consignment sale" to everything that reads the sale
			# later (ffl_core.consignment.sold_on_consignment) — never keep one.
			if flt(row.get("consignment_commission_pct")):
				row.consignment_commission_pct = 0
			continue
		# Each gun's own resolved commission must be a REAL rate in (0, 100]. A
		# resolved <= 0 is unreachable in normal operation — effective_commission_pct
		# treats a 0/blank line AND a 0/blank serial as unset, and
		# validate_consignment_serial backfills a blank/0 serial rate to the store
		# default — so a 0 here means neither the line nor that gun's serial carries
		# a usable rate (a mis-stamped serial). Surface it loudly instead of
		# silently freezing a 0 that would book the whole sale price as COGS
		# (profit 0) and split GL vs. report (found in an accountant audit). 100%
		# (store keeps all, consignor owed 0) stays valid — post_consignment_payable
		# simply books no payable.
		#
		# This runs BEFORE the conflict check below so that a gun whose rate was
		# never stamped is diagnosed as the mis-stamp it is: it is not a rate
		# conflict, and splitting the line cannot fix it — each half would just
		# hit this same throw one step later.
		for serial, own in consigned:
			resolved = effective_commission_pct(row, own)
			if resolved <= 0 or resolved > 100:
				frappe.throw(_(
					"Consignment commission % for {0} (gun {1}) must be greater than 0 "
					"and at most 100 (resolved to {2}). Set a commission rate on the "
					"Serial No or the sale line — a 0% consignment sale is not supported."
				).format(row.item_code, serial, resolved))
		# consignment_commission_pct is a LINE-level field and
		# effective_commission_pct gives the line's value priority over each
		# serial's, so whatever is frozen here applies to EVERY gun on the line.
		# Two guns carrying different rates therefore cannot share a line: the
		# frozen value would mask the second gun's own rate and pay its consignor
		# at the first gun's rate. Compare the guns' OWN rates (not the resolved
		# ones — by the second save the line already carries a frozen value that
		# would make any two guns look identical), and only REAL rates: a blank
		# one is "no opinion", not a competing rate, so an override may supply it.
		# An override cannot settle a genuine disagreement, though — one typed
		# number can't honour two consignors' different contracts — so the
		# conflict wins over it.
		rates = {own for _, own in consigned if own > 0}
		if len(rates) > 1:
			frappe.throw(_(
				"The guns on line {0} ({1}) carry different consignment commission rates "
				"({2}). One line can only freeze one rate, and it would be applied to "
				"every gun on the line — put each gun on its own line."
			).format(row.idx, ", ".join(serial for serial, _ in consigned),
				", ".join(f"{rate:g}%" for rate in sorted(rates))))
		# Every gun on the line resolved to the same in-range rate above (the
		# override, or the one real serial rate), so the value frozen here is
		# already range-checked — no second check needed.
		row.consignment_commission_pct = effective_commission_pct(row, next(iter(rates), 0))


def block_consignment_returns(doc, method: str | None = None) -> None:
	"""Owner policy: consignment guns are NOT returnable.

	A consignment sale already committed a specific, commission-derived
	payable to a specific consignor (post_consignment_payable, B1) — possibly
	already paid out via a native Payment Entry (see
	docs/consignment-payout.md). Letting a return quietly re-open that (does
	the payable claw back? was the consignor already paid?) is exactly the
	ambiguity the owner wants to avoid, so returns are disallowed outright:
	the only sanctioned way to unwind a consignment sale is to CANCEL the
	original invoice, which reverse_consignment_payable already reverses
	automatically.

	Fires on every `validate` — draft save AND submit — rather than gating on
	docstatus == 1 the way the submit-only gates elsewhere in this app do
	(require_serial_for_firearm, trade_in_credit): those allow
	a mid-edit cart to transiently mismatch because an ordinary sale-in-
	progress can legitimately pass through an inconsistent intermediate
	state. A consignment return has no such legitimate transient state — it
	is never allowed — so blocking as early as the first draft save surfaces
	the mistake to the cashier immediately instead of only at Submit.

	A consignment line = a line whose gun was sold ON CONSIGNMENT by the sale being
	returned (ffl_core.consignment.sold_on_consignment: that sale's frozen line %;
	a later receipt of the same gun — a trade-in — rewrites its Serial No stamp,
	and must neither lift the block nor put it on a sale that was the store's own).
	With no sale to read (a credit note without Return Against, or a gun not on
	it) the Serial No's is_consignment decides, as before. ANY consigned
	gun anywhere on the invoice blocks it — a qty>1 line holds several serials in
	one row, and a consignment gun riding along in position 2+ is no more
	returnable than one sitting first. Reuses _line_serials so both storage paths
	(newline text / Serial and Batch Bundle) are covered.

	Wired into POS Invoice + Sales Invoice `validate` (hooks.py).
	"""
	if not int(getattr(doc, "is_return", 0) or 0):
		return
	from ffl_core.consignment import sold_on_consignment
	sale = getattr(doc, "return_against", None)
	sold = None
	for row in (doc.items or []):
		for serial in _line_serials(row):
			stamped = frappe.db.get_value("Serial No", serial, "is_consignment")
			if sale and sold is None:
				sold = _sold_lines(doc.doctype, sale)
			if sold and serial.upper() in sold["pct"]:
				consigned = sold_on_consignment(sold["pct"][serial.upper()], sold["date"], stamped)
			else:
				consigned = stamped
			if consigned:
				frappe.throw(_(
					"Consignment sales cannot be returned (invoice {0}, gun {1}). Cancel "
					"the original sale instead."
				).format(doc.name, serial))


def validate_consignment_serial(doc, method: str | None = None) -> None:
	"""On a Serial No flagged as consignment, require a consignor and fill a
	blank commission % from the store default.

	is_consignment / consignor are EDITABLE on the Serial No form (setup/
	consignment.py) so staff can flag an in-stock gun that was missed at intake
	— or correct one — directly, without a back-end edit. This guard keeps every
	such flag payable: a consignment gun with no consignor is money owed to
	nobody, and a blank commission % would book a 100%-to-consignor sale (store
	profit 0). So require the consignor, and default a blank/zero % to FFL
	Settings' consignment_default_commission_pct — the same value intake stamps.

	Only fills a blank/zero %; a real per-gun rate the operator typed is left
	alone. Wired onto Serial No `validate` (hooks.py). The intake path
	(compliance/purchase.create_acquisitions_for_firearm_lines) writes via
	db.set_value, which bypasses validate, so this fires only on form/REST
	saves — exactly the manual-flag path it exists to guard.
	"""
	if not getattr(doc, "is_consignment", 0):
		return
	consignor = getattr(doc, "consignor", None)
	if not (consignor or "").strip():
		frappe.throw(_(
			"Select a Consignor for a consignment firearm — the sale proceeds "
			"(net of commission) are owed to them."
		))
	if not flt(getattr(doc, "consignment_commission_pct", None)):
		from ffl_core.setup.consignment import default_commission_pct
		doc.consignment_commission_pct = default_commission_pct()


def _sold_lines(doctype: str, sale: str) -> dict:
	"""The sale being returned, as block_consignment_returns reads it: its
	posting date and, per serial (upper-cased — a return line may spell it in
	another case), the commission % frozen on the line that sold it."""
	item_doctype = "POS Invoice Item" if doctype == "POS Invoice" else "Sales Invoice Item"
	pct: dict[str, float] = {}
	for row in frappe.get_all(item_doctype, filters={"parent": sale},
			fields=["serial_no", "serial_and_batch_bundle", "consignment_commission_pct"]):
		for serial in _line_serials(frappe._dict(row)):
			pct[serial.upper()] = flt(row.get("consignment_commission_pct"))
	return {"date": frappe.db.get_value(doctype, sale, "posting_date"), "pct": pct}


def post_consignment_payable(doc, method: str | None = None) -> None:
	"""Post the consignor's payable + COGS at sale (B1 — submit hook).

	For every consignment line on the invoice, book Dr COGS / Cr Consignment
	Payable[party_type=Supplier, party=consignor] for consignor_share(P, pct)
	— P the line's base_net_amount, pct the frozen effective commission %
	(freeze_consignment_commission, above, already resolved and locked it onto
	the line before this runs at submit). One Journal Entry per invoice,
	covering every consignment line on it; the JE carries
	ffl_consignment_source = doc.name so a later reversal can find it.

	This is what makes GL profit on a consignment sale equal the commission:
	revenue posts at the full sale price (P) same as any other line, but this
	JE separately expenses the consignor's share as COGS — leaving P minus
	share, i.e. the commission, as profit. Mirrors the Sales Report's own cost
	formula (report/sales_report.py's _line_cost: net * (1 - commission%))
	so GL and the report reconcile to the same number — **for a line whose guns
	are all consigned at one rate**, where the per-gun shares booked here sum
	back to net * (1 - commission%).

	They do NOT reconcile on a MIXED multi-serial line (a consigned gun and a
	store-owned gun sharing one qty>1 row). This side is right: only the
	consigned gun's own unit share is booked. The report's _line_cost still
	takes the line's FIRST consigned serial and charges the WHOLE line at that
	rate (returning early, so the owned gun's real purchase_rate never lands),
	which overstates cost. That is a pre-existing defect of the same family as
	this fix — the report reads first-serial-wins too — and repairing it belongs
	to the audited formula, not here. Until then this docstring's
	promise is qualified, not absolute.

	Wired onto POS Invoice + Sales Invoice on_submit (hooks.py).

	Skips:
	  - is_return: a return's reversal is Task B2's job, not this hook's.
	  - is_consolidated Sales Invoice: ERPNext's POS consolidation
		(api/pos_consolidate.py) builds + submits a REAL consolidated Sales
		Invoice from an already-submitted POS Invoice, which fires THIS SAME
		on_submit hook again on the same consignment line. Without this skip
		the payable would post twice for one physical sale. Mirrors the
		Sales Report's own dedup (it excludes is_consolidated=1 SIs from
		revenue — they're a roll-up of a POS Invoice already counted) and the
		identical is_consolidated skip already in
		compliance.firearm.require_4473_and_nics_for_firearm_sale.
	  - a line with no serial, a serial that isn't flagged is_consignment, or
		one with no consignor recorded (defensive — a partially-stamped serial
		must not crash a sale).
	  - a line whose computed share is <= 0 (e.g. an explicit 100% commission
		override) — nothing owed, nothing to post.
	"""
	if int(getattr(doc, "is_return", 0) or 0):
		return
	if int(getattr(doc, "is_consolidated", 0) or 0):
		return

	from ffl_core.consignment import consignor_share
	from ffl_core.setup.consignment import payable_account

	company = doc.company
	payable = payable_account(company)
	if not payable:
		return

	pairs = []  # (serial, consignor, cogs_account, cost_center, share)
	for row in (doc.items or []):
		serials = _line_serials(row)
		if not serials:
			continue
		# One gun = one consignor = one payable, but the MONEY is recorded per
		# line: base_net_amount covers all qty units on it. So each gun is owed
		# out of its own unit's share of the line, never the whole line's — that
		# over-credited whichever consignor happened to be first while the rest
		# got nothing. qty is the number of units the amount covers; the serial
		# count is the fallback for lightweight docs that carry no qty.
		units = flt(row.get("qty")) or len(serials)
		unit_net = flt(row.base_net_amount) / units
		for serial in serials:
			info = frappe.db.get_value(
				"Serial No", serial,
				["is_consignment", "consignment_commission_pct", "consignor"], as_dict=True,
			)
			# is_consignment is always stamped atomically with consignor (both
			# read-only fields, set together by compliance/purchase.py at intake
			# and by the backfill patch), so a consignment serial with no
			# consignor should never actually occur — this skip is defensive. If
			# that invariant ever drifts, GL COGS silently stops (skipped here)
			# while the Sales Report's cost is still computed, so the two
			# would diverge.
			if not info or not info.is_consignment or not info.consignor:
				continue
			pct = effective_commission_pct(row, info.consignment_commission_pct)
			share = consignor_share(unit_net, pct)
			if share <= 0:
				continue
			cogs = row.get("expense_account") or frappe.get_cached_value(
				"Company", company, "default_expense_account")
			cc = row.get("cost_center") or frappe.get_cached_value("Company", company, "cost_center")
			if not cogs or not cc:
				frappe.throw(_(
					"Consignment sale {0}: cannot book the consignor payable for {1} — no "
					"COGS account/cost center is configured. Set the company's default "
					"expense account and cost center."
				).format(doc.name, row.item_code))
			pairs.append((serial, info.consignor, cogs, cc, flt(share)))

	if not pairs:
		return

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = doc.posting_date
	je.ffl_consignment_source = doc.name
	je.user_remark = f"Consignment payable for {doc.doctype} {doc.name}"
	for serial, consignor, cogs, cc, share in pairs:
		# The gun rides both rows of its pair in the standard Journal Entry
		# Account.user_remark (no custom field, no fixture): one line can now
		# produce several pairs whose amounts are FRACTIONS of it, so which gun a
		# given share belongs to has to be readable off the row rather than
		# reverse-engineered from the number. ERPNext appends it to the GL
		# Entry's remarks (journal_entry.py: [d.user_remark, self.remark]), so
		# the source-invoice remark is kept, not replaced.
		je.append("accounts", {"account": cogs, "debit_in_account_currency": share,
			"cost_center": cc, "user_remark": serial})
		je.append("accounts", {
			"account": payable, "credit_in_account_currency": share,
			"cost_center": cc, "party_type": "Supplier", "party": consignor,
			"user_remark": serial,
		})
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()


def reverse_consignment_payable(doc, method: str | None = None) -> None:
	"""Cancel the consignor-payable JE(s) B1 posted for this invoice (B2 —
	on_cancel hook).

	Finds every submitted Journal Entry tagged ffl_consignment_source ==
	doc.name and cancels it — ERPNext's own Journal Entry.cancel() posts the
	reversing GL automatically, so the payable + COGS unwind the same way any
	other cancelled JE would.

	Idempotent / safe by construction: filters on docstatus == 1, so a
	non-consignment invoice (no JE ever posted) or an already-reversed one
	(JE already at docstatus 2) both simply find nothing and no-op — no
	is_return/is_consolidated checks needed here the way post_consignment_payable
	has them, since those cases never got a JE in the first place.

	Wired onto POS Invoice + Sales Invoice on_cancel (hooks.py).
	"""
	names = frappe.get_all("Journal Entry",
		filters={"ffl_consignment_source": doc.name, "docstatus": 1}, pluck="name")
	for name in names:
		je = frappe.get_doc("Journal Entry", name)
		je.flags.ignore_permissions = True
		je.cancel()


def _line_serials(row) -> list[str]:
	"""Every DISTINCT gun on one line, in order.

	A firearm line is NOT always qty 1: web-order fulfilment
	(ffl_woo_sync/woocommerce/revenue.py) and counter multi-gun orders
	(api/manual_order.py) both put several serials in ONE qty>1 row. Each gun
	carries its own is_consignment stamp, its own consignor and its own rate, so
	every guard here iterates guns rather than lines — reading only the first
	serial let a consigned gun in position 2+ escape the return block, the
	commission freeze and its consignor's payable alike.

	Reuses serials_for_line (ffl_core.serials) so both storage paths — newline
	text and Serial and Batch Bundle — are covered, and dedupes because one gun
	can only be blocked, rated or paid for once per line.
	"""
	return list(dict.fromkeys(serials_for_line(row)))
