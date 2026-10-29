"""Daily Payroc reconcile — ask the gateway what became of every card charge
the POS booked as captured.

A charge can be voided or refunded OUTSIDE the POS — on the PAX terminal's own
menu or in the Payroc merchant portal — and the POS never hears about it: the
invoice keeps saying paid while the money never arrives.
One charge ($50, 2026-09-01) sat as CHARGED for four weeks after an operator voided it
at the gateway. This job reads each recent captured charge back and flags any
the gateway no longer holds as captured (voided / reversed / uncaptured).

Read-only on the ledger: a mismatch is an accounting decision (collect again,
write off, record the cash that replaced it), so the job only leaves a comment
on the invoice and emails the admins — it never re-books money.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, now_datetime

from ffl_integrations.integration_gate import payroc_ready
from ffl_integrations.notifications import notify_admins, safe_log_error
from ffl_integrations.payments.api import _sibling_returns
from ffl_integrations.payments.registry import LEGACY_PROCESSOR
from ffl_integrations.payroc.client import PayrocClient
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME

# Gateway statuses of a capture the store still holds: READY = captured, in the
# open batch; COMPLETE = settled. PENDING is NOT held — it needs another
# capture before it settles (READY → PENDING is a portal-side change).
HELD_STATUSES = {"READY", "COMPLETE"}
# Charge-row statuses that took money (declined / canceled / in-progress
# attempts never did, so they don't make a sale "multi-card").
CAPTURED_ROW_STATUSES = ["CHARGED", "VOIDED", "REFUNDED"]
# The multi-card aggregate stamp on POS Invoice.card_unique_reference.
MULTI_STAMP = "MULTI"
MARKER = "[payroc-reconcile]"
PROCESSOR = "payroc"
# Draft-invoice flags carry this after the reference, so a draft flagged while
# held is flagged again (books wording) once it is submitted.
DRAFT_TAG = "(draft)"


def _captured_charges(since, until=None) -> list[frappe._dict]:
	"""Every card capture the POS booked since `since` (and before `until`, when
	given), one row per gateway reference: POS Invoice charge rows, the legacy
	single-stamp POS Invoices that predate them, and counter Sales Invoice
	terminal payments."""
	out: dict[str, frappe._dict] = {}
	created = ("between", [since, until]) if until else (">=", since)
	# `until` is exclusive (the next midnight); posting_date BETWEEN is inclusive.
	posted = ("between", [since.date(), add_days(until, -1).date()]) if until \
		else (">=", since.date())
	# docstatus < 2: a charge-first-then-4473 sale holds its CHARGED row on a
	# DRAFT invoice. Excluding drafts left a draft held longer than the window
	# unchecked for good (its row is too old by the time it submits).
	# Cancelled invoices (2) stay out.
	for r in frappe.get_all("Card Charge",
			filters={"status": "CHARGED", "creation": created,
				"parenttype": "POS Invoice", "docstatus": ("<", 2)},
			fields=["unique_reference as ref", "amount", "refunded_amount",
				"parent as name", "docstatus", "processor"]):
		# Only Payroc's own captures: another card processor's references mean nothing
		# to this gateway (blank = recorded before charges named their processor).
		if r.ref and (r.pop("processor", None) or LEGACY_PROCESSOR) == PROCESSOR:
			out.setdefault(r.ref, frappe._dict(r, doctype="POS Invoice", draft=r.docstatus == 0))
	# docstatus < 2 as above: a legacy single-stamp sale can sit as a held draft too.
	for r in frappe.get_all("POS Invoice",
			filters={"card_status": ("in", ["CHARGED", "PARTIAL"]),
				"posting_date": posted, "docstatus": ("<", 2),
				"card_unique_reference": ("not in", ["", MULTI_STAMP])},
			fields=["card_unique_reference as ref", "card_charged_amount as amount",
				"card_refunded_amount as refunded_amount", "name", "docstatus"]):
		# Legacy single-stamp invoices only — charge rows (above) own the rest.
		if r.ref and not frappe.db.exists("Card Charge",
				{"parent": r.name, "parenttype": "POS Invoice"}):
			out.setdefault(r.ref, frappe._dict(r, doctype="POS Invoice", draft=r.docstatus == 0))
	# A refunded row (si_reversal) is still a capture: its refunded_amount says how much went back.
	# A voided one is not checked (the POS already says the money is gone).
	for r in frappe.get_all("Card Terminal Payment",
			filters={"status": ("in", ["CHARGED", "REFUNDED"]), "creation": created,
				"unique_reference": ("is", "set")},
			fields=["unique_reference as ref", "amount", "refunded_amount", "sales_invoice as name",
				"processor"]):
		if (r.pop("processor", None) or LEGACY_PROCESSOR) != PROCESSOR:
			continue
		out.setdefault(r.ref, frappe._dict(r, refunded_amount=flt(r.refunded_amount),
			doctype="Sales Invoice", draft=False))
	# A counter order refunded through Cancel Order books a Pay PE ("Refund for
	# cancelled <SI>") — attributable only when the SI took ONE card capture.
	# lazy: an Edit (update_order) moves the order to a new SI, so a later
	# Cancel Order refund on the replacement is not seen here (an extra alert);
	# Follow-up: a structured replaced-by link to read the lineage.
	for c in out.values():
		if c.doctype == "Sales Invoice" and frappe.db.count("Card Terminal Payment",
				{"sales_invoice": c.name, "status": ("in", CAPTURED_ROW_STATUSES)}) <= 1:
			c.refunded_amount = max(flt(c.refunded_amount), _cancel_order_refund(c.name))
	# Invoice-level refunds (return invoices) only attribute unambiguously to a
	# sale with ONE card capture; a multi-card sale keeps each row's own
	# refunded_amount — an unattributable portal refund may then over-alert,
	# never mask another card's void.
	per_invoice: dict[str, int] = {}
	for c in out.values():
		if c.doctype == "POS Invoice":
			per_invoice[c.name] = per_invoice.get(c.name, 0) + 1
	for c in out.values():
		if c.doctype == "POS Invoice" and per_invoice[c.name] == 1 and \
				frappe.db.count("Card Charge", {"parent": c.name,
					"parenttype": "POS Invoice", "status": ("in", CAPTURED_ROW_STATUSES)}) <= 1:
			c.refunded_amount = max(flt(c.refunded_amount), _pos_recorded_refund(c.name))
	return list(out.values())


def _cancel_order_refund(sales_invoice: str) -> float:
	"""Card money refunded through Cancel Order on a counter Sales Invoice — the
	Pay PE it books ("Refund for cancelled <SI>.")."""
	return sum(flt(x) for x in frappe.get_all("Payment Entry",
		filters={"payment_type": "Pay", "docstatus": 1,
			"mode_of_payment": MODE_OF_PAYMENT_NAME,
			"remarks": ("like", f"Refund for cancelled {sales_invoice}.%")},
		pluck="paid_amount"))


def _pos_recorded_refund(invoice: str) -> float:
	"""Card money the POS has already recorded as refunded against a sale, read
	from its RETURN invoices: refund_pos_invoice stamps card_refunded_amount
	there (not on the sale), and a manager-confirmed portal refund
	(confirm_out_of_band_refund) only sets card_oob_refund_reference, so its
	card tender row is the amount.
	Every sibling return's PUSHED money counts, any docstatus — the same
	invariant as api._block_orphaned_sibling_refunds: what a cancelled or
	locked-draft return pushed is still pushed, and restore_charge_rows zeroes
	it on an amended successor.
	"""
	rets = _sibling_returns(invoice, "", ["name", "docstatus", "amended_from",
		"card_refunded_amount", "card_oob_refund_reference"])
	pushed = {r.name: flt(r.card_refunded_amount) for r in rets}
	total, portal = 0.0, {}
	for r in rets:
		recorded = pushed[r.name]
		# An amendment made before restore_charge_rows zeroed it (2026-08-04)
		# inherited its predecessor's amount: count that money once. (A real
		# second refund of the same amount is then missed — an extra alert only.)
		if recorded and abs(pushed.get(r.amended_from, -1) - recorded) < 0.005:
			recorded = 0.0
		oob = (r.card_oob_refund_reference or "").strip()
		# A portal confirmation is evidence only on a return that was submitted
		# (1, or 2 after it): a draft's card tender row is still editable.
		if oob and int(r.docstatus) != 0:
			card = abs(sum(flt(a) for a in frappe.get_all("Sales Invoice Payment",
				filters={"parent": r.name, "parenttype": "POS Invoice",
					"mode_of_payment": MODE_OF_PAYMENT_NAME}, pluck="amount")))
			# Per return the larger of pushed and tender (one refund, two records);
			# amend keeps the no_copy portal reference, so one portal refund can sit
			# on a cancelled return AND its amendment: count each reference once.
			portal[oob] = max(portal.get(oob, 0.0), recorded, card)
		else:
			total += recorded
	return total + sum(portal.values())


def _gateway_problem(payment: dict, charge) -> str | None:
	"""Why the gateway's view of this capture disagrees with the POS, or None."""
	# lazy: status only. A refund made in the portal AFTER settlement leaves the
	# payment COMPLETE and is a separate gateway resource the Payment read does
	# not carry — detecting it needs Payroc's refund/transaction search.
	tr = (payment or {}).get("transactionResult") or {}
	status = str(tr.get("status") or "").upper()
	if status in HELD_STATUSES:
		return None
	# A full POS refund before settlement is a reversal: the gateway shows VOID
	# and the POS already recorded the whole amount back — not a mismatch.
	if flt(charge.refunded_amount) + 0.005 >= flt(charge.amount):
		return None
	return f"gateway status {status or 'missing'}"


def _already_flagged(charge) -> bool:
	# A draft is done once flagged either way; a submitted invoice only by a
	# submitted-wording flag ("<ref>:"), so a flagged draft that was submitted
	# anyway is raised again as booked money.
	pattern = f"{MARKER} {charge.ref}%" if charge.get("draft") else f"{MARKER} {charge.ref}:%"
	return bool(frappe.db.exists("Comment", {
		"reference_doctype": charge.doctype, "reference_name": charge.name,
		"comment_type": "Comment", "content": ("like", pattern)}))


_DRAFT_ACTION = ("do not submit it — the card row still shows CHARGED in the POS; "
	"have a manager sort out the charge before taking payment again. Nothing is "
	"booked yet, so do not book a correction")


def _alert_row(f: dict) -> str:
	tail = f" — DRAFT: {_DRAFT_ACTION}" if f["draft"] else ""
	return (f"<li>{f['doctype']} {f['name']} — {f['ref']} ${f['amount']:.2f}: "
		f"{f['problem']} (operator {f['operator']}){tail}</li>")


def reconcile_payroc_charges(days: int = 7) -> list[dict]:
	"""Scheduler entry (daily). Returns the NEW mismatches found this run.
	Backfill: bench execute ffl_integrations.payroc.reconcile.reconcile_payroc_charges --kwargs "{'days': 90}"
	"""
	if not payroc_ready():
		return []
	# lazy: one GET per charge in the window (~a week of card sales); page by
	# date or use a Payroc batch/settlement report if the volume ever grows.
	client = PayrocClient.from_settings()
	found = []
	for charge in _captured_charges(add_days(now_datetime(), -int(days))):
		if _already_flagged(charge):
			continue
		try:
			payment = client.retrieve_payment(charge.ref)
		except Exception:
			safe_log_error(f"Payroc reconcile: could not read {charge.ref}",
				frappe.get_traceback())
			continue
		problem = _gateway_problem(payment, charge)
		if not problem:
			continue
		found.append({"doctype": charge.doctype, "name": charge.name,
			"ref": charge.ref, "amount": flt(charge.amount), "problem": problem,
			"operator": (payment or {}).get("operator") or "?",
			"draft": bool(charge.get("draft"))})
	if not found:
		return found
	rows = "".join(_alert_row(f) for f in found)
	sent = notify_admins(
		f"Payroc: {len(found)} card charge(s) no longer captured at the gateway",
		"<p>These card charges were taken in the POS — on submitted invoices "
		"(booked as paid) or on drafts not yet submitted — but Payroc no longer "
		"holds them as captured (voided or reversed outside the POS):</p>"
		f"<ul>{rows}</ul><p>Each invoice has a comment. Submitted invoices: "
		"confirm with the operator what happened, then correct the books. "
		f"Drafts: {_DRAFT_ACTION}.</p>",
		dedup_key=f"payroc-reconcile-{frappe.generate_hash(length=8)}", window_minutes=1)
	if not sent:
		# No comment → the next run finds these again and retries the alert.
		safe_log_error("Payroc reconcile: alert not sent", rows)
		return found
	for f in found:
		if f["draft"]:
			note = (f"{MARKER} {f['ref']} {DRAFT_TAG}: card charge of {f['amount']:.2f} "
				f"taken on this DRAFT, but {f['problem']} (gateway operator "
				f"{f['operator']}). {_DRAFT_ACTION[0].upper()}{_DRAFT_ACTION[1:]}.")
		else:
			note = (f"{MARKER} {f['ref']}: POS booked {f['amount']:.2f} as captured, "
				f"but {f['problem']} (gateway operator {f['operator']}). The money may "
				"not have arrived — check with the operator and have the books corrected.")
		frappe.get_doc(f["doctype"], f["name"]).add_comment("Comment", note)
	frappe.db.commit()
	return found
