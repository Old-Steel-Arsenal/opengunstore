"""Whitelisted Payroc operations called from POS UI / hooks.

Card-present sale is a 3-call cycle so the cashier can Cancel mid-flight and a
worker isn't pinned:

  charge_pos_invoice(invoice)        → create the device payment instruction,
                                        stamp card_instruction_id, return its id.
  poll_pos_charge(invoice, id, amt)  → one poll; while IN_PROGRESS returns
                                        {ok:False, status:'IN_PROGRESS'} so the
                                        frontend loops. On COMPLETED, follows the
                                        link to the Payment, reads
                                        transactionResult.resultCode, stamps
                                        card_unique_reference on approval.
  cancel_pos_charge(invoice, id)     → DELETE an in-flight instruction.

Plus discover_terminals() (admin smoke), void_pos_invoice() (back-office reverse),
and refund_pos_invoice() (Phase D). All charge paths are gated to Sales User+;
refund/void to Sales Manager+.

MULTI-CARD (2026-08): a sale may split its Credit Card tender across several
physical cards — each charge_pos_invoice(amount) call is one Card Charge
child row, and the sale is fully collected when the CHARGED rows sum to the
card tender. The single-value payroc_* fields persist as AGGREGATES of the
rows (_refresh_charge_stamps is their only writer on the row path); invoices
predating the child table have stamps but no rows and take the original
single-charge code paths throughout.
"""
from __future__ import annotations

import json as _json
import re as _re

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.compliance.payment_mode import looks_like_pan, require_tender_reference
from ffl_core.firearm import firearm_items_in
from ffl_core.utils import commit as _safe_commit
from ffl_integrations.integration_gate import card_reference_required, payroc_enabled, payroc_ready
from ffl_integrations.notifications import notify_admins, safe_log_error
from ffl_integrations.payroc.client import SETTINGS_DOCTYPE, PayrocClient, PayrocError
from ffl_integrations.payroc.setup import MODE_OF_PAYMENT_NAME


# What "approved" looks like in a Payroc transactionResult.
_APPROVED = {"A"}

# card_status Select values — ONE explicit, human-readable state label per
# invoice. The payroc_* reference fields stay the money facts (what the gateway
# gave us); the status is the single field UI/guards read instead of inferring
# state from field combinations. Every transition is written in this module.
#   IN_PROGRESS  instruction live on the terminal
#   CHARGED      fully captured
#   PARTIAL      partial capture recorded (NOT fully paid — submit is blocked)
#   DECLINED     last attempt declined
#   CANCELED     last instruction canceled / timed out / failed
#   VOIDED       capture reversed (re-chargeable)
#   REFUNDED     (return invoices) referenced card refund completed


def _check_perm():
    roles = set(frappe.get_roles())
    if not (roles & {"Sales User", "Sales Manager", "System Manager"}):
        frappe.throw(_("Card charges require Sales User role or above."),
            frappe.PermissionError)


def _check_manager(message: str):
    roles = set(frappe.get_roles())
    if not (roles & {"Sales Manager", "System Manager"}):
        frappe.throw(message, frappe.PermissionError)


# Payroc ties an order to its FIRST successful sale: once a payment instruction
# on orderId X is approved, every later instruction carrying X comes back
# "Order has already been processed." — the gateway refuses it before the card
# is ever read. Reusing the document name as the orderId therefore made the
# SECOND card of a split impossible (card 1 took
# $75, card 2 could not be sent at all). Every attempt gets its own id.
# Attempt 1 keeps the bare document name so the overwhelmingly common
# single-card receipt still matches the invoice exactly in the Payroc portal;
# the balance of a split reads <invoice>-A2, -A3 … The plain document
# name also travels in the instruction's `description`, so a suffixed order is
# still searchable by invoice either way.
#
# The "A" is load-bearing, not decoration: Frappe names an amended document
# {name}-1, {name}-2 … so a bare "-2" attempt id would be the NAME of a future
# amendment of the same invoice (cancel+amend is a routine correction here),
# and that amendment's own first charge would then hit an orderId Payroc had
# already processed — the exact refusal this function exists to prevent.
PAYROC_ORDER_ID_MAX = 24


def _order_id(doc_name: str, attempt: int) -> str:
    """The Payroc orderId for one charge attempt on `doc_name`.

    Args:
        doc_name: POS Invoice / Sales Invoice name this charge belongs to.
        attempt: 1-based attempt number (every instruction, declines included).

    Returns:
        The document name for attempt 1, else "{name}-A{attempt}" — capped at
        Payroc's 24-character orderId limit by trimming the NAME, never the
        suffix (a truncated suffix would collide with the previous attempt).
    """
    name = str(doc_name or "")
    if int(attempt or 1) <= 1:
        return name[:PAYROC_ORDER_ID_MAX]
    suffix = f"-A{int(attempt)}"
    return name[:PAYROC_ORDER_ID_MAX - len(suffix)] + suffix


def _check_refund_perm():
    _check_manager(_("Refunds require manager permission (Sales Manager)."))


def _integration_ready(require_device: bool = False) -> bool:
    """The documented go-live gate: with the integration switched OFF, no Merchant
    API Key, or (for device charges) no A80 serial, the integration is INERT — POS
    skips the gateway entirely and card rows record as plain tender (standalone
    terminal used manually). Filling in real credentials + serial on an ENABLED
    integration is what arms it.

    Kept as the module-local name every skip path (and the tests that patch it)
    already uses; the decision itself moved to ``integration_gate.payroc_ready`` so
    this module, the boot payload the POS bundle reads, and the desk buttons cannot
    drift apart on what "Payroc is off" means.
    """
    return payroc_ready(require_device=require_device)


def _txn(payment: dict) -> dict:
    return (payment or {}).get("transactionResult") or {}


def _declined(resp: dict) -> str:
    """Worldnet signals business outcome in the 200 body (resultCode A/D), not
    the HTTP status. Returns the decline message if the response is an explicit
    decline, else ""."""
    tr = _txn(resp)
    if (tr.get("resultCode") or "").upper() == "D" \
            or (tr.get("status") or "").upper() == "DECLINED":
        return tr.get("resultMessage") or tr.get("resultCode") or "declined"
    return ""


def _card_total(doc, absolute: bool = False) -> float:
    """Sum of the invoice's Credit Card tender rows — the portion the terminal
    was asked to charge (NOT grand_total, which includes cash under split tender).
    absolute=True for returns, whose card rows are negative."""
    return sum(abs(flt(p.amount)) if absolute else flt(p.amount)
               for p in (doc.get("payments") or [])
               if p.get("mode_of_payment") == MODE_OF_PAYMENT_NAME)


# ------------------------------------------------------------ multi-card rows
# One Card Charge child row per device payment instruction. The single-value
# payroc_* stamps stay on POS Invoice as AGGREGATES of the rows (legacy
# consumers — tender guard, PARTIAL submit guard, resume — keep reading them).
# Invoices from before the table exist have stamps but no rows: every lifecycle
# path below forks to the original single-charge behavior for those.


def _charge_rows(invoice_name: str) -> list:
    """Card Charge rows in entry order. Empty ⇒ legacy single-stamp era."""
    return frappe.get_all("Card Charge",
        filters={"parenttype": "POS Invoice", "parent": invoice_name},
        fields=["name", "amount", "refunded_amount", "status", "unique_reference",
                "instruction_id", "attempt", "card_summary"],
        order_by="idx asc, creation asc")


def _live_total(rows) -> float:
    return sum(flt(r.amount) for r in rows if r.status == "CHARGED")


def _card_target(inv) -> float:
    """What the cards must add up to: the Credit Card tender portion; a zero
    OR NEGATIVE card tender (card-less backend-initiated charges; returns)
    falls back to grand_total. The single accounting basis — callers must not inline
    variants of this fallback (r1 F11)."""
    ct = flt(_card_total(inv), 2)
    if ct > 0:
        return ct
    return flt(inv.get("grand_total") or inv.get("rounded_total") or 0, 2)


def _gateway_card_summary(payment: dict) -> str:
    acct = (payment or {}).get("customerAccount") or {}
    # Digits only — masking formats vary (****1111 / 1111**** / 4097**...5938)
    # and this string prints on the customer receipt (r1 F12).
    digits = _re.sub(r"\D", "", acct.get("maskedPan") or "")[-4:]
    return " ".join(p for p in [(acct.get("cardType") or "").strip(),
        f"•{digits}" if digits else ""] if p)[:120]


def _append_charge_row(invoice_name: str, *, instruction_id: str, attempt: int) -> str:
    """Row-level insert (db_insert, hash name — how Frappe names child rows):
    charge stamps must not run the parent's validate/save cycle mid-charge,
    exactly like every set_value(update_modified=False) in this module."""
    row = frappe.new_doc("Card Charge")
    row.update({
        "parenttype": "POS Invoice", "parent": invoice_name,
        "parentfield": "card_charges",
        "idx": (frappe.db.count("Card Charge",
            {"parenttype": "POS Invoice", "parent": invoice_name}) or 0) + 1,
        "status": "IN_PROGRESS", "instruction_id": instruction_id,
        "attempt": attempt,
        # Currency columns are NOT NULL — seed 0, never None.
        "amount": 0, "refunded_amount": 0,
    })
    # Explicit hash (same shape Frappe gives child rows): db_insert's own
    # autonaming for standalone child docs isn't provable across versions.
    row.name = frappe.generate_hash(length=10)
    row.db_insert()
    return row.name


def _refresh_charge_stamps(invoice_name: str) -> None:
    """Recompute the legacy single-value stamps from the child rows — the ONLY
    writer of the aggregate mirror (never write the mirror fields elsewhere on
    the row path):
      charged_amount   Σ CHARGED rows (0 when none — the column is NOT NULL)
      status           IN_PROGRESS while an instruction is live; else CHARGED /
                       PARTIAL by captured-vs-card-tender; else the last row's
                       terminal state (DECLINED / CANCELED / VOIDED)
      unique_reference the one live ref, "MULTI" across several, else None
      instruction_id   the live instruction (the resume handle), else None
    No rows ⇒ legacy invoice ⇒ hands off (legacy paths write stamps directly)."""
    rows = _charge_rows(invoice_name)
    if not rows:
        return
    in_flight = [r for r in rows if r.status == "IN_PROGRESS"]
    live = [r for r in rows if r.status == "CHARGED"]
    total = flt(_live_total(rows), 2)
    if in_flight:
        status = "IN_PROGRESS"
    elif live:
        inv = frappe.get_doc("POS Invoice", invoice_name)
        status = "CHARGED" if total >= _card_target(inv) else "PARTIAL"
    else:
        # No live money: VOIDED wins over the last attempt's DECLINED/CANCELED
        # — "money was reversed" is what a manager must see (r1 F12).
        status = ("VOIDED" if any(r.status == "VOIDED" for r in rows)
            else rows[-1].status)
    frappe.db.set_value("POS Invoice", invoice_name, {
        "card_charged_amount": total,
        "card_status": status,
        "card_unique_reference": (live[0].unique_reference if len(live) == 1
            else "MULTI" if live else None),
        "card_instruction_id": in_flight[0].instruction_id if in_flight else None,
    }, update_modified=False)


def _settle_instruction_row(client, inst: dict, invoice_name: str, row) -> dict:
    """A terminal-state instruction → write the row's outcome and refresh the
    aggregates. A PARTIAL APPROVAL IS REAL MONEY here: the row goes CHARGED at
    the authorized amount and invoice-level fullness is judged by the
    aggregates (Σ vs card tender) — per-instruction fullness died with the
    single-charge model. Returns the poll shape the frontend loops on."""
    st = inst.get("status") or "IN_PROGRESS"
    if st in ("CANCELED", "FAILURE"):
        frappe.db.set_value("Card Charge", row.name, {
            "status": "CANCELED", "amount": 0, "response": _resp_json(inst),
        }, update_modified=False)
        frappe.db.set_value("POS Invoice", invoice_name,
            "card_response", _resp_json(inst), update_modified=False)
        _refresh_charge_stamps(invoice_name)
        return {"ok": False, "status": st,
                "error": inst.get("errorMessage") or st}
    payment, tr, ref, authorized = _read_payment(client, inst)
    # The invoice-level card_response audit stamp stays live in the row era —
    # it is what an incident postmortem reads first (r1 F5).
    frappe.db.set_value("POS Invoice", invoice_name,
        "card_response", _resp_json(payment), update_modified=False)
    if (tr.get("resultCode") or "") not in _APPROVED or authorized <= 0:
        frappe.db.set_value("Card Charge", row.name, {
            "status": "DECLINED", "amount": 0, "unique_reference": ref,
            "response": _resp_json(payment),
        }, update_modified=False)
        _refresh_charge_stamps(invoice_name)
        return {"ok": False, "status": "COMPLETED", "unique_reference": ref,
                "error": tr.get("resultMessage") or tr.get("resultCode") or "declined"}
    frappe.db.set_value("Card Charge", row.name, {
        "status": "CHARGED", "amount": authorized, "unique_reference": ref,
        "card_summary": _gateway_card_summary(payment),
        "response": _resp_json(payment),
    }, update_modified=False)
    _refresh_charge_stamps(invoice_name)
    _note_last_charge()
    # Money captured — commit the durable point. Deliberately NOT routed
    # through _persist_money_fact (unlike void/refund): a failed settle write
    # self-heals — the next poll re-reads the instruction from the gateway
    # and rewrites the row, so retry-then-alert would be redundant here.
    _commit_money_point()
    inv = frappe.get_doc("POS Invoice", invoice_name)
    remaining = flt(_card_target(inv) - _live_total(_charge_rows(invoice_name)), 2)
    return {"ok": True, "status": "COMPLETED", "unique_reference": ref,
            "approval_code": tr.get("approvalCode") or "", "amount": authorized,
            "remaining": max(remaining, 0)}


def _resp_json(resp) -> str:
    """The card_response audit stamp, truncated to a sane field size."""
    return _json.dumps(resp or {})[:1000]


def _throw_if_declined(resp: dict, invoice_name: str, message_template: str,
        row_name: str | None = None):
    """Worldnet can decline a refund/reverse inside a 200 body — record the
    response (invoice-level audit stamp, plus the charge row when given) and
    throw so success-path stamps never run on a decline."""
    declined = _declined(resp)
    if declined:
        frappe.db.set_value("POS Invoice", invoice_name,
            "card_response", _resp_json(resp), update_modified=False)
        if row_name:
            frappe.db.set_value("Card Charge", row_name,
                "response", _resp_json(resp), update_modified=False)
        frappe.throw(message_template.format(declined))


def _read_payment(client, inst: dict):
    """Follow a COMPLETED instruction's HATEOAS link and read the outcome —
    the ONE shared gateway reader for both charge models (r1 F5); the
    full-vs-partial judgment stays with each caller."""
    href = client.payment_link(inst)
    payment = client.retrieve_payment(href) if href else inst
    tr = _txn(payment)
    ref = (payment or {}).get("uniqueReference") or ""
    authorized = flt(tr.get("authorizedAmount") or 0)
    return payment, tr, ref, authorized


def _note_last_charge() -> None:
    try:
        frappe.db.set_single_value("Payroc Settings", "last_charge_at",
            frappe.utils.now_datetime())
    except Exception:
        pass


def _commit_money_point() -> None:
    """Durable checkpoint right after external money moved — a later throw's
    request-level rollback must not un-book gateway facts (r1 F1; the same
    failure class as a past incident). One-line alias over the
    canonical test-aware commit (ffl_core.utils.commit, no-op inside the test
    transaction) so call sites read as what they mean (r2-M2)."""
    _safe_commit()


# The two REFUND LOCK sentinels _lock_and_throw stamps into
# card_refund_reference. Everything else in that field is a real gateway
# refund reference (or the legacy "UNKNOWN"/"MULTI" success markers) — a
# completed refund, which must never be "unlocked" back to retryable.
# Mirrored in ffl_core/public/js/card_charges.js (isRefundLock /
# isUnlockableLock) — change BOTH sides together.
REFUND_LOCK_UNKNOWN = "UNKNOWN-OUTCOME"
REFUND_LOCK_PARTIAL_PREFIX = "PARTIAL:"


def _is_refund_lock(value: str | None) -> bool:
    v = value or ""
    return v == REFUND_LOCK_UNKNOWN or v.startswith(REFUND_LOCK_PARTIAL_PREFIX)


def _is_unlockable_lock(value: str | None) -> bool:
    """POSITIVE gate for unlock_refund_lock, same whitelist shape as the JS
    isUnlockableLock: only a zero-booked UNKNOWN-OUTCOME lock may unlock. A
    blacklist ("not PARTIAL") would default-ALLOW any future third sentinel —
    fail-open on the side that can move money."""
    return (value or "") == REFUND_LOCK_UNKNOWN


def _outcome_known(e: Exception) -> bool:
    """The ONE definite-vs-unknown failure sorter for refund paths (row loop
    and legacy pre-arm both key retry-vs-lock off it; a drifted copy of this
    predicate is a compliance divergence)."""
    return isinstance(e, PayrocError) and getattr(e, "outcome_known", True)


# Money comparisons in the refund gates use an epsilon, not flt(x,2) equality
# like the charge-side math: sums cross documents/eras here and cent-rounding
# artifacts must not block a legitimate refund (same value as ffl_core
# manual_order's paid tolerance).
_MONEY_EPSILON = 0.005

# Payroc server-owned POS Invoice scalars — written exclusively by this
# module via frappe.db.set_value(update_modified=False), which never bumps
# the modified timestamp, so Frappe's stale-save conflict check cannot
# protect them (the past invoice class; canonical pattern:
# ffl_core.compliance.webhook_fields). A cashier's browser that loaded the
# doc BEFORE a refund attempt still holds the pre-lock values — its next
# full-doc save would null the re-refund lock and the booked fact.
# card_manual_settlement(_reason) are deliberately absent: those are
# USER-declared via the form.
_SERVER_OWNED_SCALARS = (
    "card_unique_reference",
    "card_instruction_id",
    "card_refund_reference",
    "card_refunded_amount",
    "card_attempt",
    "card_status",
    "card_charged_amount",
    "card_response",
)


def protect_money_scalars(doc, method=None):
    """doc_events on POS Invoice — hooked on validate (which also runs on
    submit) AND on before_cancel / before_update_after_submit, exactly like
    restore_charge_rows and for the same reason: frappe dispatches per
    _action and before_save runs on NEITHER submit nor cancel.
    Restores the payroc server-owned scalar family from DB truth —
    the scalar sibling of the row restore. New docs are normalized by
    restore_charge_rows' islocal branch instead (no DB truth exists yet)."""
    if doc.get("__islocal") or not doc.name:
        return
    db = frappe.db.get_value("POS Invoice", doc.name,
        _SERVER_OWNED_SCALARS, as_dict=True)
    if not db:
        return
    for field in _SERVER_OWNED_SCALARS:
        if doc.get(field) != db.get(field):
            doc.set(field, db.get(field))


def _money_logger():
    # File-based (site log), transaction-free: the one channel that survives
    # any DB failure. Same pattern as ffl_core.compliance.disposition_lock.
    return frappe.logger("ffl_integrations.payroc_money", allow_site=True)


def _persist_money_fact(write_fn, *, action: str, invoice: str, facts: dict,
        fact_label: str | None = None) -> None:
    """Persist the DB record of a gateway money FACT that is already true,
    guaranteeing it can never vanish without trace (
    the failure class: gateway reversed, DB rolled back,
    zero alerting). ``fact_label`` names what is already true for operator
    messages/logs — default "Gateway operation succeeded" fits void/refund money moves;
    the refund-lock arm passes its own (there the gateway DECLINED or is
    unknown — "success" would misinform the manager).

    Layers, in order:
      1. file-log the facts BEFORE writing — if every DB write below fails,
         the site log still holds the case file (refs/amounts; the raw
         gateway body stays in DB fields + Error Log, not the file log);
      2. run ``write_fn()`` under a savepoint and commit (the money point).
         On failure roll back TO THE SAVEPOINT ONLY (never the whole
         transaction — earlier request state, e.g. previously committed-and-
         continued refund slices' session, and test-transaction fixtures must
         survive) and retry once: lock waits and transient drops are the
         realistic causes and usually clear immediately;
      3. both attempts failed → durable trace (safe_log_error + commit) +
         ops alert (notify_admins, throttled, never raises), then throw an
         operator message that says DO NOT RE-RUN the gateway operation — an
         operator retry after this point is how a double refund happens.

    ``write_fn`` must be idempotent (plain UPDATEs): a retry re-runs it whole.
    The settle path (_settle_instruction_row) deliberately does NOT come
    through here — poll retries re-read the gateway and rewrite the row, so
    a failed settle write self-heals on the next poll.
    Known residue: a MariaDB deadlock (1213) rolls back the entire
    transaction server-side, killing the savepoint — the rollback-to-savepoint
    then fails and we skip straight to layer 3. Acceptable: single-row stamp
    UPDATEs make 1213 exotic here, and layer 3 still records the facts."""
    label = fact_label or _("Gateway operation succeeded")
    log = _money_logger()
    slim = {k: (str(v)[:200] if k == "response" else v) for k, v in facts.items()}
    log.info(f"{action} money-fact {invoice}: {_json.dumps(slim, default=str)[:1200]}")
    savepoint = "payroc_money_fact"
    for attempt in (1, 2):
        try:
            frappe.db.savepoint(savepoint)
            write_fn()
            _commit_money_point()
            return
        except Exception:
            log.warning(
                f"{action} {invoice}: money-fact DB write failed (attempt {attempt})",
                exc_info=True)
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                log.error(f"{action} {invoice}: savepoint rollback failed — not retrying",
                    exc_info=True)
                break
    # Every wiring point must surface ITS credential here:
    # void/void-legacy → unique_reference; refund-final/-legacy →
    # refund_references/unique_reference; refund-slice → card;
    # refund-lock → booked_refs (which slices already landed — exactly what
    # the back office needs). Lists render joined, not as Python reprs.
    raw_refs = (facts.get("unique_reference") or facts.get("refund_references")
        or facts.get("booked_refs") or facts.get("card") or "")
    refs = ", ".join(str(r) for r in raw_refs) \
        if isinstance(raw_refs, (list, tuple)) else str(raw_refs)
    try:
        safe_log_error(
            f"Payroc {action}: gateway money moved, DB stamp FAILED ({invoice})",
            "The gateway-side fact is TRUE but its DB record could not be "
            "written after retry. Reconcile manually against the Payroc "
            f"portal before any retry.\nfacts: {_json.dumps(facts, default=str)[:2000]}")
        # The scariest money event in the system — page somebody (throttled;
        # contracted never to raise). Beyond the Error Log floor:
        # Also the anchor of the accountant SOP.
        # MUST run BEFORE the commit: sendmail only INSERTS an Email Queue
        # row — left uncommitted it would die in frappe.throw's request
        # rollback while the non-transactional Redis throttle key survives
        # and suppresses the next hour's attempts.
        notify_admins(
            f"Payroc {action}: money fact NOT recorded ({invoice})",
            _("{0} (ref {1}), but the DB did not take the write either time. Operator "
                "retries have been stopped; reconcile against the Payroc back office by "
                "hand and stamp the record. facts: {2}").format(
                label, refs, _json.dumps(facts, default=str)[:1500]),
            dedup_key=f"payroc-money-fact:{invoice}")
        _safe_commit()   # makes the Error Log AND the alert email durable
    except Exception:
        # safe_log_error / notify_admins are contracted never to raise — the
        # realistic thrower here is the commit itself.
        log.error(f"{action} {invoice}: durable-trace commit failed", exc_info=True)
    frappe.throw(_(
        "{0} ({1}, reference {2}), but the system failed twice to record it in the books. Do "
        "not retry the refund/void — that would move money again; note this invoice number "
        "{3}, hand it to the back office, and reconcile against the Payroc back office by hand."
            ).format(
        label, action, refs or "?", invoice))


def _resolve_completed(client, inst: dict, invoice_name: str, expected_amount) -> dict:
    """A COMPLETED instruction → follow the HATEOAS link to the Payment, read the
    outcome, and stamp card_unique_reference exactly once. Shared by
    poll_pos_charge / charge_pos_invoice(resume) / cancel_pos_charge so a captured
    charge is reconciled once and never double-created. Returns the poll shape."""
    payment, tr, ref, authorized = _read_payment(client, inst)
    frappe.db.set_value("POS Invoice", invoice_name,
        "card_response", _resp_json(payment), update_modified=False)

    approved = (tr.get("resultCode") or "") in _APPROVED
    if not approved:
        frappe.db.set_value("POS Invoice", invoice_name,
            "card_status", "DECLINED", update_modified=False)
        return {"ok": False, "status": "COMPLETED", "unique_reference": ref,
                "error": tr.get("resultMessage") or tr.get("resultCode") or "declined"}

    # "Full" requires a positive authorized amount equal (to the cent) to the
    # expected total. A 0 / partial capture is recorded (ref stamped, so it can be
    # reversed) but never auto-submitted. expected_amount must be supplied to
    # claim a full charge — otherwise treat it as partial (fail safe).
    exp = flt(expected_amount) if expected_amount not in (None, "") else None
    full = authorized > 0 and exp is not None and flt(authorized, 2) == flt(exp, 2)
    if not full:
        if ref:
            # PARTIAL marks the stamp as NOT fully paid — the frontend
            # short-circuit and the before_submit hook both refuse to submit.
            frappe.db.set_value("POS Invoice", invoice_name, {
                "card_unique_reference": ref,
                "card_status": "PARTIAL",
                "card_charged_amount": authorized,
            }, update_modified=False)
        return {"ok": False, "partial": True, "status": "COMPLETED",
                "unique_reference": ref, "authorized_amount": authorized}

    frappe.db.set_value("POS Invoice", invoice_name, {
        "card_unique_reference": ref,
        "card_status": "CHARGED",
        # The captured amount — held orders (charged before the 4473 cleared)
        # re-verify the cart against it at submit time.
        "card_charged_amount": authorized,
    }, update_modified=False)
    _note_last_charge()
    return {"ok": True, "status": "COMPLETED", "unique_reference": ref,
            "approval_code": tr.get("approvalCode") or "", "amount": authorized}


@frappe.whitelist(methods=["POST"])
def charge_pos_invoice(invoice_name: str, terminal: str | None = None,
        amount=None, description: str | None = None) -> dict:
    """Start a card charge on the Payroc A80 terminal for a POS invoice.

    Creates a device payment instruction; the frontend then polls
    poll_pos_charge() until the terminal returns. Reuses an in-flight
    instruction rather than double-charging.

    Args:
        invoice_name: POS Invoice being tendered.
        terminal: Optional terminal id; defaults to the configured one.
        amount: Optional charge amount; defaults to the card-tender
            target.
        description: Optional statement descriptor.

    Returns:
        {ok, instruction_id, status, amount} — or {"skip": True} when the
        integration is unconfigured.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    if not _integration_ready(require_device=True):
        # Go-live gate: unconfigured integration is inert — the frontend
        # submits normally and the card row records as plain tender.
        return {"skip": True}
    invoice_name = (invoice_name or "").strip()
    if not invoice_name:
        frappe.throw(_("invoice_name is required."))
    if not frappe.db.exists("POS Invoice", invoice_name):
        frappe.throw(_("POS Invoice {0} not found.").format(invoice_name))
    # Concurrency: two tills / a double-click must serialize on this invoice —
    # everything below is read-check-insert and is only safe under the parent
    # row lock (r1 F6). Released with the request's transaction — or EARLIER,
    # by any mid-request money commit: the settle branch below re-takes it.
    frappe.db.get_value("POS Invoice", invoice_name, "name", for_update=True)
    inv = frappe.get_doc("POS Invoice", invoice_name)
    rows = _charge_rows(invoice_name)
    client = PayrocClient.from_settings()

    if not rows:
        # ---- legacy single-stamp era (invoice pre-dates the child table) ----
        if inv.get("card_unique_reference"):
            frappe.throw(_("POS Invoice {0} is already charged ({1})."
                ).format(invoice_name, inv.card_unique_reference))
        prev = inv.get("card_instruction_id")
        if prev:
            # Idempotency: resume/reconcile the legacy in-flight instruction
            # rather than ever creating a second one.
            inst = client.poll_payment_instruction(prev) or {}
            st = inst.get("status") or "IN_PROGRESS"
            if st == "IN_PROGRESS":
                return {"ok": True, "instruction_id": prev,
                        "status": "IN_PROGRESS", "amount": flt(amount) or _card_target(inv)}
            if st == "COMPLETED":
                res = _resolve_completed(client, inst, invoice_name,
                    flt(amount) if amount is not None else _card_target(inv))
                if res.get("ok") or res.get("partial"):
                    frappe.throw(_("POS Invoice {0}: the previous instruction has already been "
                        "charged (reconciled {1}); to refund, void it from the back office."
                            ).format(invoice_name, res.get("unique_reference") or "?"))
                # COMPLETED-but-declined → fresh attempt (falls through to rows).
            # FAILURE / CANCELED → fresh attempt (falls through to rows).

    # Idempotency: never a SECOND live instruction. Settle any in-flight row
    # first; if the terminal still holds it, hand the id back for polling.
    in_flight = [r for r in rows if r.status == "IN_PROGRESS"]
    if in_flight:
        row = in_flight[0]
        inst = client.poll_payment_instruction(row.instruction_id) or {}
        if (inst.get("status") or "IN_PROGRESS") == "IN_PROGRESS":
            return {"ok": True, "instruction_id": row.instruction_id,
                    "status": "IN_PROGRESS", "amount": flt(amount) or 0}
        _settle_instruction_row(client, inst, invoice_name, row)
        # A captured settle just ran _commit_money_point — InnoDB released the
        # F6 parent-row lock with that commit, so the read-check-create below
        # would race a second till. Re-take the lock, re-read
        # the doc + rows, AND re-judge in-flight: another till may have minted
        # an instruction in the unlocked window, and "never a SECOND live
        # instruction" must hold against the fresh rows, not the stale check
        # above.
        frappe.db.get_value("POS Invoice", invoice_name, "name", for_update=True)
        inv = frappe.get_doc("POS Invoice", invoice_name)
        rows = _charge_rows(invoice_name)
        foreign = [r for r in rows if r.status == "IN_PROGRESS"]
        if foreign:
            return {"ok": True, "instruction_id": foreign[0].instruction_id,
                    "status": "IN_PROGRESS", "amount": flt(amount) or 0}

    # Multi-card accounting: each instruction charges a SLICE of the card
    # tender; the sale is fully collected when the CHARGED rows sum to it.
    target = _card_target(inv)
    remaining = flt(target - _live_total(rows), 2)
    if remaining <= 0:
        frappe.throw(_("POS Invoice {0}: the card amount has already been charged in full ({1} "
            "in total); to refund, void it from the back office."
                ).format(invoice_name, flt(_live_total(rows), 2)))
    amt = flt(amount) if amount is not None else remaining
    if amt <= 0:
        frappe.throw(_("Amount must be > 0 (got {0}).").format(amt))
    if flt(amt, 2) > remaining:
        frappe.throw(_("This amount {0} exceeds the card balance still to be collected ({1}) — "
            "the parts of a multi-card split cannot add up to more than the card total due."
            ).format(flt(amt, 2), remaining))

    # The counter advances only once the instruction is RECORDED (below), which
    # is deliberate: a create whose outcome is unknown (timeout / non-JSON 2xx,
    # PayrocError.outcome_known False) may have reached the terminal and taken
    # the money, and reusing its orderId is what makes Payroc refuse the next
    # swipe. Fail closed — a blocked terminal that a manager clears with the
    # portal + Manual Card Settlement beats minting a fresh id and charging a
    # customer who may have already paid.
    attempt = int(inv.get("card_attempt") or 0) + 1
    created = client.create_payment_instruction(
        amount=amt, order_id=_order_id(invoice_name, attempt), terminal=terminal,
        operator=frappe.session.user,
        description=description or f"POS Invoice {invoice_name}")
    iid = (created or {}).get("paymentInstructionId") or ""
    if not iid:
        frappe.throw(_("Payroc returned no paymentInstructionId."))
    try:
        _append_charge_row(invoice_name, instruction_id=iid, attempt=attempt)
        stamps = {"card_attempt": attempt}
        if inv.get("card_manual_settlement"):
            # New money through the integration invalidates a prior
            # manual-settlement declaration — the manager re-declares if it
            # still holds (r1 F3).
            stamps.update({"card_manual_settlement": 0,
                "card_manual_settlement_reason": None})
        frappe.db.set_value("POS Invoice", invoice_name, stamps,
            update_modified=False)
        _refresh_charge_stamps(invoice_name)
    except Exception:
        # The instruction is LIVE on the terminal but nothing reached the DB —
        # cancel it best-effort so money can't be captured unrecorded (r1 F12).
        try:
            client.cancel_payment_instruction(iid)
        except Exception:
            pass
        raise
    return {"ok": True, "instruction_id": iid,
            "status": (created or {}).get("status") or "IN_PROGRESS", "amount": amt}


@frappe.whitelist(methods=["POST"])
def poll_pos_charge(invoice_name: str, instruction_id: str, expected_amount=None) -> dict:
    """Poll a Payroc payment instruction once.

    IN_PROGRESS → {ok: False} (frontend loops). COMPLETED → fetch the
    Payment, verify full approval, stamp the reference on the invoice.

    Args:
        invoice_name: POS Invoice being tendered.
        instruction_id: The instruction returned by charge_pos_invoice.
        expected_amount: Optional approval-amount check.

    Returns:
        {ok: True, ...settled details} on completion; {ok: False,
        status: "IN_PROGRESS"} while pending; {ok: False, status, error}
        on terminal failure.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    client = PayrocClient.from_settings()
    inst = client.poll_payment_instruction(instruction_id) or {}
    status = inst.get("status") or "IN_PROGRESS"
    if status == "IN_PROGRESS":
        return {"ok": False, "status": "IN_PROGRESS"}

    rows = _charge_rows(invoice_name)
    if rows:
        row = next((r for r in rows if r.instruction_id == instruction_id), None)
        if row is None:
            # A row-era invoice must NEVER fall into the legacy resolver — it
            # writes the mirror fields directly and would desync the
            # aggregates from the rows (r1 F5).
            frappe.throw(_("POS Invoice {0} has no card record for instruction {1} — refresh "
                "the page and try again."
                ).format(invoice_name, instruction_id))
        return _settle_instruction_row(client, inst, invoice_name, row)

    # ---- legacy single-stamp era ----
    if status in ("CANCELED", "FAILURE"):
        frappe.db.set_value("POS Invoice", invoice_name, {
            "card_response": _resp_json(inst),
            "card_status": "CANCELED",
        }, update_modified=False)
        return {"ok": False, "status": status,
                "error": inst.get("errorMessage") or status}
    # COMPLETED → reconcile the Payment (stamp ref, verify full approval).
    return _resolve_completed(client, inst, invoice_name, expected_amount)


@frappe.whitelist(methods=["POST"])
def cancel_pos_charge(invoice_name: str, instruction_id: str) -> dict:
    """Cancel an in-flight Payroc instruction (race-safe).

    If the cancel RACES an approval (card just captured), the capture is
    reconciled instead of stranding it / letting a retry double-charge.

    Args:
        invoice_name: POS Invoice being tendered.
        instruction_id: The instruction to cancel.

    Returns:
        {ok: True, status: "CANCELED"} — or the reconciled settle result
        when the approval won the race.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    client = PayrocClient.from_settings()
    inst = client.poll_payment_instruction(instruction_id) or {}
    status = inst.get("status") or "IN_PROGRESS"
    rows = _charge_rows(invoice_name)
    row = next((r for r in rows if r.instruction_id == instruction_id), None)
    if rows and row is None:
        # Same fork rule as poll: a row-era invoice never touches the legacy
        # resolver (r1 F5).
        frappe.throw(_("POS Invoice {0} has no card record for instruction {1} — refresh the "
            "page and try again."
            ).format(invoice_name, instruction_id))
    if status == "COMPLETED":
        # The cancel RACED an approval — reconcile the capture instead of
        # stranding it / letting a retry double-charge.
        if row is not None:
            res = _settle_instruction_row(client, inst, invoice_name, row)
            res["reconciled"] = True
            return res
        # ---- legacy single-stamp era ----
        inv = frappe.get_doc("POS Invoice", invoice_name)
        res = _resolve_completed(client, inst, invoice_name, _card_target(inv))
        res["reconciled"] = True
        return res
    resp = client.cancel_payment_instruction(instruction_id)
    if row is not None:
        frappe.db.set_value("Card Charge", row.name, {
            "status": "CANCELED", "amount": 0, "response": _resp_json(resp),
        }, update_modified=False)
        _refresh_charge_stamps(invoice_name)
        return {"ok": True, "status": "CANCELED"}
    frappe.db.set_value("POS Invoice", invoice_name, {
        "card_response": _resp_json(resp),
        "card_status": "CANCELED",
    }, update_modified=False)
    return {"ok": True, "status": "CANCELED"}


# --------------------------------------------------------------------------
# Terminal payment against a counter Sales Invoice (Create Order / Pending
# Order → Record Payment). The POS charge model above is tied to the
# register's Credit Card tender rows; a counter order is on account, so a card
# taken on the terminal for it is its own record (Card Terminal Payment) and
# books as a Payment Entry through the counter's ONE collection path
# (ffl_core.api.manual_order.record_payment) once the gateway confirms it.
# Order of events is create-order-first: the invoice exists before any money
# moves, so a decline or a walk-away leaves an on-account order the counter
# can retry or settle another way — never money without an invoice.
# --------------------------------------------------------------------------
TERMINAL_PAYMENT = "Card Terminal Payment"
_TP_FIELDS = ["name", "sales_invoice", "amount", "status", "instruction_id",
    "unique_reference", "approval_code", "card_summary", "payment_entry"]


def _terminal_payments(sales_invoice: str) -> list:
    return frappe.get_all(TERMINAL_PAYMENT, filters={"sales_invoice": sales_invoice},
        fields=_TP_FIELDS, order_by="creation asc")


def _terminal_payment_row(sales_invoice: str, instruction_id: str):
    row = frappe.db.get_value(TERMINAL_PAYMENT,
        {"sales_invoice": sales_invoice, "instruction_id": instruction_id},
        _TP_FIELDS, as_dict=True)
    if not row:
        frappe.throw(_("{0} has no terminal payment for instruction {1} — refresh and retry."
            ).format(sales_invoice, instruction_id))
    return row


def _terminal_payment_result(row) -> dict:
    """The poll-shaped answer for a record already settled (a racing poll or
    cancel got there first) — read from the record, never re-booked."""
    if row.status == "CHARGED":
        outstanding = flt(frappe.db.get_value("Sales Invoice", row.sales_invoice,
            "outstanding_amount"))
        return {"ok": True, "status": "COMPLETED", "unique_reference": row.unique_reference,
                "approval_code": row.approval_code or "", "amount": flt(row.amount),
                "payment_entry": row.payment_entry, "outstanding": outstanding,
                "paid": outstanding <= 0.01}
    return {"ok": False, "status": row.status,
            "error": _("Card declined.") if row.status == "DECLINED" else row.status}


def _settle_terminal_payment(client, inst: dict, row) -> dict:
    """A terminal-state instruction → the record's outcome, and for an approval
    the Payment Entry. Money first, ledger second: the CHARGED stamp is
    committed before the booking so a failed booking can never hide a capture."""
    if row.status != "IN_PROGRESS":
        return _terminal_payment_result(row)
    st = inst.get("status") or "IN_PROGRESS"
    if st in ("CANCELED", "FAILURE"):
        frappe.db.set_value(TERMINAL_PAYMENT, row.name, {
            "status": "CANCELED", "response": _resp_json(inst)}, update_modified=False)
        return {"ok": False, "status": st, "error": inst.get("errorMessage") or st}
    payment, tr, ref, authorized = _read_payment(client, inst)
    if (tr.get("resultCode") or "") not in _APPROVED or authorized <= 0:
        frappe.db.set_value(TERMINAL_PAYMENT, row.name, {
            "status": "DECLINED", "unique_reference": ref, "response": _resp_json(payment),
        }, update_modified=False)
        return {"ok": False, "status": "COMPLETED", "unique_reference": ref,
                "error": tr.get("resultMessage") or tr.get("resultCode") or "declined"}
    approval = tr.get("approvalCode") or ""
    frappe.db.set_value(TERMINAL_PAYMENT, row.name, {
        "status": "CHARGED", "amount": authorized, "unique_reference": ref,
        "approval_code": approval, "card_summary": _gateway_card_summary(payment),
        "response": _resp_json(payment),
    }, update_modified=False)
    _note_last_charge()
    _commit_money_point()
    pe = _book_terminal_payment(row.name, row.sales_invoice, authorized, ref or approval)
    outstanding = flt(frappe.db.get_value("Sales Invoice", row.sales_invoice,
        "outstanding_amount"))
    return {"ok": True, "status": "COMPLETED", "unique_reference": ref,
            "approval_code": approval, "amount": authorized, "payment_entry": pe,
            "outstanding": outstanding, "paid": outstanding <= 0.01}


def _book_terminal_payment(row_name: str, sales_invoice: str, amount: float,
        reference: str) -> str | None:
    """The ledger half of a captured terminal payment: a Payment Entry against
    the Sales Invoice through the counter's one collection path (record_payment
    — the same booking Pending Order's Record Payment makes, reference = the
    gateway's uniqueReference). Runs under _persist_money_fact: the gateway
    already holds the money, so a failed booking leaves a durable trace and an
    operator message that says do not re-swipe, never a silent gap."""
    from ffl_core.api.manual_order import record_payment
    booked: dict = {}

    def write():
        outstanding = flt(frappe.db.get_value("Sales Invoice", sales_invoice,
            "outstanding_amount"))
        # authorized ≤ requested ≤ outstanding at request time; a racing Record
        # Payment could have shrunk the balance since — book what the invoice
        # still owes and leave the excess visible in the alert facts.
        # record_payment demands a reference for a card on the live terminal; a
        # captured charge must still book if the gateway sent neither a
        # uniqueReference nor an approval code (the caller passes whichever it has).
        out = record_payment(sales_invoice, MODE_OF_PAYMENT_NAME,
            amount=min(flt(amount), outstanding), transaction_number=reference or row_name)
        frappe.db.set_value(TERMINAL_PAYMENT, row_name, "payment_entry",
            out["payment_entry"], update_modified=False)
        booked["pe"] = out["payment_entry"]

    _persist_money_fact(write, action="terminal-payment", invoice=sales_invoice,
        facts={"unique_reference": reference, "amount": amount, "record": row_name},
        fact_label=_("The card was charged on the terminal"))
    return booked.get("pe")


@frappe.whitelist(methods=["POST"])
def charge_sales_invoice(sales_invoice: str, amount=None, terminal: str | None = None) -> dict:
    """Start a card charge on the Payroc terminal against a counter Sales Invoice.

    The counter's card tender for an on-account order (Create Order right
    after the order books; Pending Order → Record Payment for a balance).
    Creates a device payment instruction the frontend then polls with
    poll_sales_invoice_charge(); the approval books a Payment Entry. Reuses
    an in-flight instruction rather than double-charging, and never starts
    one while an earlier approval on this invoice is unrecorded.

    Args:
        sales_invoice: Submitted Sales Invoice with an outstanding balance.
        amount: Charge amount, at most the outstanding balance (blank = the
            whole balance; less = a deposit).
        terminal: Optional terminal id; defaults to the configured one.

    Returns:
        {ok, instruction_id, status, amount} — {"skip": True} when the
        integration is unconfigured; an earlier approval found unrecorded
        comes back settled with already=True.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    if not _integration_ready(require_device=True):
        return {"skip": True}
    sales_invoice = (sales_invoice or "").strip()
    if not sales_invoice or not frappe.db.exists("Sales Invoice", sales_invoice):
        frappe.throw(_("Sales Invoice {0} not found.").format(sales_invoice))
    # Two screens / a double-click must serialize on this invoice — read-check-
    # insert below is only safe under the row lock (same rule as the POS charge).
    frappe.db.get_value("Sales Invoice", sales_invoice, "name", for_update=True)
    si = frappe.db.get_value("Sales Invoice", sales_invoice,
        ["docstatus", "outstanding_amount", "customer_name"], as_dict=True)
    if si.docstatus != 1:
        frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
    client = PayrocClient.from_settings()

    in_flight = [r for r in _terminal_payments(sales_invoice) if r.status == "IN_PROGRESS"]
    if in_flight:
        row = in_flight[0]
        inst = client.poll_payment_instruction(row.instruction_id) or {}
        if (inst.get("status") or "IN_PROGRESS") == "IN_PROGRESS":
            return {"ok": True, "instruction_id": row.instruction_id,
                    "status": "IN_PROGRESS", "amount": flt(row.amount)}
        res = _settle_terminal_payment(client, inst, row)
        if res.get("ok"):
            # Money already taken on the earlier attempt — hand it back settled
            # instead of ever minting a second live instruction.
            res["already"] = True
            return res
        # Declined / cancelled → a fresh attempt. The settle may have committed
        # (releasing the row lock): re-take it and re-judge in-flight.
        frappe.db.get_value("Sales Invoice", sales_invoice, "name", for_update=True)
        si = frappe.db.get_value("Sales Invoice", sales_invoice,
            ["docstatus", "outstanding_amount", "customer_name"], as_dict=True)
        foreign = [r for r in _terminal_payments(sales_invoice) if r.status == "IN_PROGRESS"]
        if foreign:
            return {"ok": True, "instruction_id": foreign[0].instruction_id,
                    "status": "IN_PROGRESS", "amount": flt(foreign[0].amount)}

    outstanding = flt(si.outstanding_amount, 2)
    if outstanding <= 0.01:
        frappe.throw(_("{0} is already paid.").format(sales_invoice))
    amt = flt(amount) if amount not in (None, "") else outstanding
    if amt <= 0:
        frappe.throw(_("Amount must be > 0 (got {0}).").format(amt))
    if flt(amt, 2) > outstanding + 0.01:
        frappe.throw(_("Payment {0} exceeds the outstanding balance {1}.").format(
            frappe.format_value(amt, {"fieldtype": "Currency"}),
            frappe.format_value(outstanding, {"fieldtype": "Currency"})))

    # Same per-attempt orderId rule as the POS path — a counter order can take a
    # second card too (each attempt is one Card Terminal Payment row).
    attempt = len(_terminal_payments(sales_invoice)) + 1
    created = client.create_payment_instruction(
        amount=amt, order_id=_order_id(sales_invoice, attempt), terminal=terminal,
        operator=frappe.session.user,
        description=f"{sales_invoice} — {si.customer_name or ''}".strip(" —"))
    iid = (created or {}).get("paymentInstructionId") or ""
    if not iid:
        frappe.throw(_("Payroc returned no paymentInstructionId."))
    try:
        rec = frappe.get_doc({"doctype": TERMINAL_PAYMENT, "sales_invoice": sales_invoice,
            "amount": amt, "status": "IN_PROGRESS", "instruction_id": iid})
        rec.flags.ignore_permissions = True
        rec.insert()
    except Exception:
        # The instruction is LIVE on the terminal but nothing reached the DB —
        # cancel it best-effort so money can't be captured unrecorded.
        try:
            client.cancel_payment_instruction(iid)
        except Exception:
            pass
        raise
    return {"ok": True, "instruction_id": iid,
            "status": (created or {}).get("status") or "IN_PROGRESS", "amount": amt}


@frappe.whitelist()
def poll_sales_invoice_charge(sales_invoice: str, instruction_id: str) -> dict:
    """Poll a terminal charge started by charge_sales_invoice once.

    IN_PROGRESS → {ok: False} (frontend loops). On approval the record goes
    CHARGED and a Payment Entry books against the invoice; the answer carries
    the new balance.

    Args:
        sales_invoice: The Sales Invoice being charged.
        instruction_id: The instruction returned by charge_sales_invoice.

    Returns:
        {ok: True, unique_reference, approval_code, amount, payment_entry,
        outstanding, paid} on approval; {ok: False, status: "IN_PROGRESS"}
        while pending; {ok: False, status, error} on decline / cancel.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    client = PayrocClient.from_settings()
    inst = client.poll_payment_instruction(instruction_id) or {}
    if (inst.get("status") or "IN_PROGRESS") == "IN_PROGRESS":
        return {"ok": False, "status": "IN_PROGRESS"}
    return _settle_terminal_payment(client, inst,
        _terminal_payment_row(sales_invoice, instruction_id))


@frappe.whitelist(methods=["POST"])
def cancel_sales_invoice_charge(sales_invoice: str, instruction_id: str) -> dict:
    """Cancel an in-flight terminal charge on a Sales Invoice (race-safe).

    A cancel that races an approval settles the approval instead — the card
    was charged, so it is recorded and booked, never stranded.

    Args:
        sales_invoice: The Sales Invoice being charged.
        instruction_id: The instruction to cancel.

    Returns:
        {ok: True, status: "CANCELED"} — or the settled approval with
        reconciled=True when the approval won the race.

    Permissions:
        POS charge roles (_check_perm).
    """
    _check_perm()
    client = PayrocClient.from_settings()
    row = _terminal_payment_row(sales_invoice, instruction_id)
    inst = client.poll_payment_instruction(instruction_id) or {}
    if (inst.get("status") or "IN_PROGRESS") == "COMPLETED":
        res = _settle_terminal_payment(client, inst, row)
        res["reconciled"] = True
        return res
    resp = client.cancel_payment_instruction(instruction_id)
    if row.status == "IN_PROGRESS":
        frappe.db.set_value(TERMINAL_PAYMENT, row.name, {
            "status": "CANCELED", "response": _resp_json(resp)}, update_modified=False)
    return {"ok": True, "status": "CANCELED"}


@frappe.whitelist()
def discover_terminals() -> dict:
    """List the API key's allowed Payroc terminals (connectivity smoke test).

    The authenticate response lists the key's allowedTerminals — a
    zero-risk connectivity + pairing check.

    Returns:
        The raw authenticate response JSON, {"error": <body>} on a non-OK
        response, or {"skip": True} when the integration is switched off /
        unconfigured.

    Permissions:
        Manager role.
    """
    _check_manager(_("Terminal discovery requires manager role."))
    if not _integration_ready():
        # Same inert contract as charge/refund: an admin poking this on a store
        # that runs on a standalone terminal should get a plain answer, not a live
        # request to a gateway the store has said it is not using.
        return {"skip": True}
    return PayrocClient.from_settings().account_summary()


@frappe.whitelist(methods=["POST"])
def void_pos_invoice(invoice_name: str, reason: str | None = None,
        unique_reference: str | None = None) -> dict:
    """Reverse an unsettled Payroc charge (back-office; per-card on multi-card).

    Payroc has no /void — this books the gateway reversal. Multi-card
    invoices void PER CARD: pass the charge row's unique_reference when
    more than one live capture exists.

    Args:
        invoice_name: POS Invoice carrying the charge.
        reason: Optional audit reason.
        unique_reference: The specific charge row to void (required when
            several live captures exist).

    Returns:
        {ok: True, unique_reference, response}.

    Permissions:
        Refund/void roles (_check_refund_perm).
    """
    _check_refund_perm()
    rows = _charge_rows(invoice_name)
    if rows:
        live = [r for r in rows if r.status == "CHARGED"]
        if not live:
            frappe.throw(_("POS Invoice {0} has no Payroc payment to void.").format(invoice_name))
        if unique_reference:
            target = next((r for r in live if r.unique_reference == unique_reference), None)
            if target is None:
                frappe.throw(_("POS Invoice {0} has no charge {1} that can be voided."
                    ).format(invoice_name, unique_reference))
        elif len(live) == 1:
            target = live[0]
        else:
            frappe.throw(_("This invoice was split across several cards ({0} charges on record) "
                "— say which one to void by its unique_reference: {1}"
                ).format(len(live), ", ".join(r.unique_reference for r in live)))
        if flt(target.refunded_amount) > 0:
            frappe.throw(_("This charge has already been partly refunded ({0}) and cannot be "
                "voided as a whole; process a return refund instead."
                ).format(flt(target.refunded_amount)))
        try:
            resp = PayrocClient.from_settings().reverse(
                target.unique_reference, reversal_reason=reason or "void")
        except PayrocError as e:
            frappe.throw(_("Void failed: the Payroc gateway refused it (the transaction may "
                "already be settled; process a return refund instead). {0}"
                ).format(str(e)[-200:]))
        # On decline the card is still captured — the row stays armed.
        _throw_if_declined(resp, invoice_name,
            _("Void refused by the gateway ({0}) — the charge is still valid and the references "
                "have not been cleared."), row_name=target.name)
        def _write_void_row():
            frappe.db.set_value("Card Charge", target.name, {
                # amount stays for the audit trail; the aggregates only sum CHARGED.
                "status": "VOIDED", "response": _resp_json(resp),
            }, update_modified=False)
            frappe.db.set_value("POS Invoice", invoice_name,
                "card_response", _resp_json(resp), update_modified=False)
            _refresh_charge_stamps(invoice_name)
        # Money reversed at the gateway — the row flip must land durably, or
        # fail with a durable trace + a DO-NOT-RETRY stop.
        _persist_money_fact(_write_void_row, action="void", invoice=invoice_name,
            facts={"unique_reference": target.unique_reference, "response": resp})
        _clear_payment_hold(invoice_name)
        return {"ok": True, "unique_reference": target.unique_reference, "response": resp}

    # ---- legacy single-stamp era ----
    ref = frappe.db.get_value("POS Invoice", invoice_name, "card_unique_reference")
    if not ref:
        frappe.throw(_("POS Invoice {0} has no Payroc payment to void.").format(invoice_name))
    try:
        resp = PayrocClient.from_settings().reverse(ref, reversal_reason=reason or "void")
    except PayrocError as e:
        # A gateway HTTP error (e.g. the txn already settled / not reversible).
        # The card is still captured → keep the stamps, and surface a clean
        # message instead of a raw traceback (matters for the manager UI).
        frappe.throw(_("Void failed: the Payroc gateway refused it (the transaction may already "
            "be settled; process a return refund instead). {0}"
            ).format(str(e)[-200:]))
    # On decline the card is still captured — keep the stamps so the re-charge
    # guard stays armed; clearing them would open a double-charge window.
    _throw_if_declined(resp, invoice_name, _("Void refused by the gateway ({0}) — the charge is "
        "still valid and the references have not been cleared."))
    # Clear the stamps so a reversed invoice (e.g. a partial-approval recovery)
    # can be charged again from POS instead of dead-locking on "already charged".
    # card_charged_amount must be 0, never None: Currency columns are NOT NULL,
    # and this UPDATE runs AFTER the gateway reversal — a 1048 here rolls back the
    # stamp clear while the money is already reversed.
    # _persist_money_fact is that incident's structural answer: retry the
    # write, and if it still fails leave a durable trace + stop the operator.
    # It also gives this legacy path the money-point commit it never had.
    _persist_money_fact(lambda: frappe.db.set_value("POS Invoice", invoice_name, {
            "card_response": _resp_json(resp),
            "card_unique_reference": None,
            "card_instruction_id": None,
            "card_status": "VOIDED",
            "card_charged_amount": 0,
        }, update_modified=False),
        action="void-legacy", invoice=invoice_name,
        facts={"unique_reference": ref, "response": resp})
    _clear_payment_hold(invoice_name)
    return {"ok": True, "unique_reference": ref, "response": resp}


def _clear_payment_hold(invoice_name: str) -> None:
    """A void ends the charge-before-4473 hold: the order is no longer fully
    tendered, so the payment screen must offer Take Payment again (the charge
    loop then collects only the card balance still owed — recorded cash / Zelle
    rows are preserved on the draft by the tender guard, never re-collected).

    Kept OUT of the money-fact durable block on purpose: the flag is UI state,
    not a money fact, and a site that has not migrated the ffl_core fixture
    yet must not turn a successful gateway reversal into a 1054 crash
    (a past incident). Column-guarded, best-effort."""
    try:
        if not frappe.db.has_column("POS Invoice", "ffl_payment_held"):
            return
        frappe.db.set_value("POS Invoice", invoice_name, "ffl_payment_held", 0,
            update_modified=False)
    except Exception:
        frappe.log_error(title="payroc: could not clear ffl_payment_held",
            message=f"{invoice_name}\n{frappe.get_traceback()}")


def _original_gateway_charged(return_invoice_name: str) -> str:
    """The original sale's gateway reference, or "" when it never took gateway
    money. Defensive by design — an unknown docname, a return with no original,
    and a cash / standalone-terminal sale all answer "" — because every caller
    treats "" as "nothing to protect" and the long-standing contract of the
    switched-off path is "unconfigured => skip, touching nothing"."""
    if not return_invoice_name or not frappe.db.exists("POS Invoice", return_invoice_name):
        return ""
    original = frappe.db.get_value("POS Invoice", return_invoice_name, "return_against")
    if not original:
        return ""
    live = [r for r in _charge_rows(original) if r.status == "CHARGED"]
    ref = frappe.db.get_value("POS Invoice", original, "card_unique_reference")
    if not live and not ref:
        return ""
    return ref or (live[0].unique_reference if live else "?")


def _oob_refund_instruction(reference: str) -> str:
    return _(
        "This sale was charged on the integrated card terminal ({0}), but the "
        "Payroc integration is switched off, so the refund cannot be sent from "
        "here. Refund it in the Payroc portal first; then a MANAGER opens this "
        "return in the desk view and uses <b>Confirm Portal Refund</b> to record "
        "that refund's reference. Do NOT re-enable the integration and retry: "
        "POS has no record of a portal refund, so it would send a second one."
    ).format(reference)


def _refuse_unconfirmed_gateway_refund(return_invoice_name: str) -> None:
    """Stop an inert refund from silently swallowing money that DID leave.

    "Integration switched off" normally means the gateway is inert and the return
    submits like any other. But ``enabled`` is a one-click, freely reversible
    operator action, so "sale charged on the terminal -> integration switched off
    -> customer returns it" is an ordinary sequence, not a freak accident.
    Skipping there books a refund in the ledger while the card is never credited
    — money going OUT is the one direction where inert is not a safe default.

    The exit is a MANAGER ACTION, not a field. An earlier cut let the operator
    type the portal refund's reference on the return's card line — but the
    standalone-terminal flow force-collects that very field for its own reason,
    so in the default configuration the guard was satisfied before anyone read
    it: a control that clears itself is not a control. The exit is
    :func:`confirm_out_of_band_refund`: manager role, mandatory reason, the
    portal reference on its own field, and a timeline entry — the shape
    ``card_manual_settlement`` and ``unlock_refund_lock`` already use.

    The ops alert stays (throttled): a refused refund at the counter must also
    reach whoever actually holds the portal login, or the customer waits on a
    cashier who has no way to move the money.
    """
    reference = _original_gateway_charged(return_invoice_name)
    if not reference:
        return
    if (frappe.db.get_value("POS Invoice", return_invoice_name,
            "card_oob_refund_reference") or "").strip():
        return  # a manager has confirmed the portal refund — evidenced, proceed
    message = _oob_refund_instruction(reference)
    notify_admins(
        subject=_("Card refund needs a portal refund first: Payroc is switched off"),
        message=f"{return_invoice_name}: {message}",
        dedup_key=f"payroc-off-refund:{return_invoice_name}",
    )
    frappe.throw(message, title=_("Card refund cannot be sent"))


def _sibling_returns(original: str, exclude: str, fields: list) -> list:
    """The one sibling-return scan both refund gates share:
    every return against ``original`` except the one being processed, any
    docstatus — cancelled returns' money facts still count."""
    return frappe.get_all("POS Invoice",
        filters={"return_against": original, "name": ["!=", exclude]},
        fields=fields)


def _cc_tender_total(parents: list) -> float:
    """Σ |Credit Card tender rows| across the given POS Invoices — the one
    entitlement reader both refund gates share."""
    if not parents:
        return 0.0
    return flt(sum(abs(flt(a)) for a in frappe.get_all(
        "Sales Invoice Payment",
        filters={"parenttype": "POS Invoice", "parent": ["in", parents],
                 "mode_of_payment": MODE_OF_PAYMENT_NAME},
        pluck="amount")), 2)


def _block_orphaned_sibling_refunds(original: str, exclude: str) -> None:
    """Money may only follow SUBMITTED return documents.

    ``card_refunded_amount`` records what each return actually pushed back
    at the gateway. The per-original-card caps bound HOW MUCH can ever go
    back per card, but not WHICH document the money belongs to: abandon a
    locked draft whose slices partly landed, open a fresh return for the
    same goods, and the fresh full-amount refund re-pays what the draft
    already pushed (the r1-blocker 250-on-a-150-return math through a side
    door — deletion is guarded by on_trash, but a SECOND return needs no
    delete). The invariant enforced here: Σ booked across sibling returns
    (any docstatus — a cancelled return's pushed money is still pushed;
    frappe's amend RETAINS no_copy fields, so restore_charge_rows zeroes the
    money facts on every NEW doc and an amended successor starts at 0 by
    hook, not by field property) must stay within Σ card entitlement of
    SUBMITTED siblings. Orphaned money blocks every new refund on that
    original until the orphan document is submitted (drafts) or
    amended-and-resubmitted (cancelled), which restores its entitlement —
    deliberate operational pressure, not a dead end.

    Known slack (deliberate): this is an AGGREGATE invariant, not per-
    document pairing — a submitted sibling with unused entitlement can
    numerically cover another document's orphaned booking. Pairing would
    reject legitimate netting across documents; the aggregate keeps the
    only bound that is money-true (Σ pushed ≤ Σ documented) and the
    per-card caps bound the totals."""
    sibs = _sibling_returns(original, exclude,
        ["name", "docstatus", "card_refunded_amount"])
    booked = flt(sum(flt(s.card_refunded_amount) for s in sibs), 2)
    if booked <= 0:
        return
    submitted = [s.name for s in sibs if int(s.docstatus) == 1]
    entitled = _cc_tender_total(submitted)
    if booked - entitled > _MONEY_EPSILON:
        from frappe.utils import fmt_money
        orphans = [s.name for s in sibs
            if int(s.docstatus) != 1 and flt(s.card_refunded_amount) > 0]
        if not orphans:
            # The imbalance can also sit on SUBMITTED siblings (booked more
            # than their own tender rows now say — e.g. CC rows edited after
            # the refund): name those instead of an unhelpful "?".
            own_booked = {s.name: flt(s.card_refunded_amount) for s in sibs}
            orphans = [n for n in submitted
                if own_booked.get(n, 0) - _cc_tender_total([n]) > _MONEY_EPSILON]
        frappe.throw(_("Card refunds already booked against original invoice {0} ({1}) exceed "
            "what its submitted returns entitle ({2}) — money has gone out but the return "
            "documents were never settled. Deal with these documents before refunding anything "
            "new: {3} (draft: submit it; cancelled: amend and resubmit; or reconcile in the "
            "back office)."
            ).format(original, fmt_money(booked), fmt_money(entitled),
                     ", ".join(orphans) or "?"))


@frappe.whitelist(methods=["POST"])
def refund_pos_invoice(return_invoice_name: str, reason: str | None = None) -> dict:
    """Refund a POS return's card portion to the original card(s).

    Payroc referenced refund of the Credit Card portion of a POS RETURN
    invoice. Firearm returns are blocked (bound-book reversal is a
    separate project). Multi-card originals refund across rows within
    per-original-card caps.

    Args:
        return_invoice_name: The POS RETURN invoice.
        reason: Optional audit reason.

    Returns:
        {ok: True, refund_reference, amount, original}; {"skip": True} when the
        integration is switched off / unconfigured, or the original never took
        gateway money.

    Raises:
        ValidationError: When the integration is switched off but the ORIGINAL
            was charged on the terminal and no manager has confirmed the portal
            refund yet (confirm_out_of_band_refund is the exit — ops are also
            emailed). Also: firearm returns (bound-book reversal is a separate
            project), a repeat refund of an already-refunded return, or a refund
            whose sibling returns' booked money exceeds their documented card
            entitlement.

    Permissions:
        Manager-gated refund roles (gate at entry).
    """
    _check_refund_perm()
    return_invoice_name = (return_invoice_name or "").strip()
    if not _integration_ready():
        # Inert — with exactly one exception, checked before we agree to skip:
        # a gateway-charged original needs the manager's portal-refund
        # confirmation, or nothing anywhere shows the card was ever credited.
        _refuse_unconfirmed_gateway_refund(return_invoice_name)
        return {"skip": True}
    ret = frappe.get_doc("POS Invoice", return_invoice_name)

    original = ret.get("return_against")
    if not original:
        frappe.throw(_("Return invoice {0} has no original invoice in return_against."
            ).format(return_invoice_name))
    orig_rows = [r for r in _charge_rows(original) if r.status == "CHARGED"]
    ref = frappe.db.get_value("POS Invoice", original, "card_unique_reference")
    if not orig_rows and not ref:
        # Original wasn't charged through Payroc (cash / manual-terminal sale) —
        # nothing to refund via the gateway; the return proceeds normally.
        return {"skip": True}

    # Idempotency guard (mirror of charge_pos_invoice's "already charged"):
    # Worldnet has no idempotency header, so a second call = a second refund.
    # A LOCK sentinel is NOT "already refunded" — saying so misled the counter
    # into a Payroc-portal goose chase and hid
    # the manager exits this module now has.
    # LOCKING READ of the guard field itself (not a row lock + plain doc
    # read): under REPEATABLE READ the locking read returns latest committed
    # for the locked row, while a subsequent plain SELECT would keep the
    # transaction's old snapshot — the blocked second caller would read the
    # pre-lock value and double-refund (prod is MariaDB 10.6,
    # no snapshot-isolation error as a backstop). Serializes double-clicks /
    # two tills against the pre-arm below.
    prior_ref = frappe.db.get_value("POS Invoice", return_invoice_name,
        "card_refund_reference", for_update=True)
    if _is_refund_lock(prior_ref):
        if prior_ref == REFUND_LOCK_UNKNOWN:
            frappe.throw(_("The automatic refund for return invoice {0} is locked ({1}): the "
                "outcome of the last attempt is unknown, and possibly nothing was refunded. A "
                "manager should check in the Payroc back office, then retry with Unlock Card "
                "Refund."
                ).format(return_invoice_name, prior_ref))
        frappe.throw(_("The automatic refund for return invoice {0} is locked ({1}): some "
            "slices are already booked; handle the remaining amount by hand in the Payroc back "
            "office (this lock cannot be released — it is the only record of the amount "
            "already refunded on this invoice)."
            ).format(return_invoice_name, prior_ref))
    if prior_ref:
        frappe.throw(_("Return invoice {0} has already been refunded ({1}) and cannot be "
            "refunded twice."
            ).format(return_invoice_name, prior_ref))

    # Block firearm returns — direct to back-office.
    codes = [r.item_code for r in (ret.get("items") or []) if r.get("item_code")]
    if firearm_items_in(codes):
        frappe.throw(_("Firearm returns go through the back office / a separate process "
            "(bound-book disposition reversal); the POS does not handle them."))

    # Refund amount = |sum of Credit Card payment rows| on the return, capped at
    # the original's captured card amount so we never push back more than was taken.
    cc_amt = _card_total(ret, absolute=True)
    if cc_amt <= 0:
        frappe.throw(_("Return invoice {0} has no Credit Card refund amount."
            ).format(return_invoice_name))
    # Money may only follow SUBMITTED return documents — one gate
    # for both eras, ahead of the per-era caps.
    _block_orphaned_sibling_refunds(original, exclude=return_invoice_name)
    if orig_rows:
        return _refund_across_rows(ret, original, orig_rows, cc_amt, reason)

    # ---- legacy single-stamp era ----
    orig_cc = _card_total(frappe.get_doc("POS Invoice", original))
    if orig_cc <= 0:
        # Fail safe: a capture exists (ref above) but the card rows don't show
        # it — skipping the cap here would allow an unbounded refund.
        frappe.throw(_("The Credit Card row amount of original invoice {0} cannot be "
            "determined, so the POS cannot refund the card; use the back office."
            ).format(original))
    # Cumulative cap: prior referenced refunds (sibling returns against the same
    # original) count against what's left — N partial returns must not sum past
    # the original capture.
    prior = [p for p in _sibling_returns(original, return_invoice_name,
        ["name", "card_refund_reference"]) if p.card_refund_reference]
    prior_refunded = _cc_tender_total([p.name for p in prior])
    remaining = flt(orig_cc, 2) - flt(prior_refunded, 2)
    if flt(cc_amt, 2) > remaining:
        # A sibling stuck on a refund LOCK counts at its full card amount —
        # deliberately (its refund may have landed; under-counting could
        # over-refund). But then the rejection must say WHICH sibling and
        # that a manager can resolve it, or the operator dead-ends.
        locked = [p.name for p in prior if _is_refund_lock(p.card_refund_reference)]
        hint = (_("Of these, {0} are in a refund lock (outcome undecided, counted as fully "
            "refunded); a manager should check in the Payroc back office and then deal with "
            "it: an UNKNOWN-OUTCOME lock can be released with Unlock Card Refund to retry, and "
            "the rest of a PARTIAL one is handled by hand in the back office."
            ).format(", ".join(locked)) if locked else "")
        frappe.throw(_("Refund amount {0} exceeds what remains refundable on the original card "
            "charge {1} (originally charged {2}, already refunded {3}). {4}"
            ).format(cc_amt, remaining, orig_cc, prior_refunded, hint))

    # An AMENDED original is an inherited envelope: frappe's amend copy
    # RETAINS the payroc stamps (real single-card reference or the MULTI
    # marker) while the charge rows are stripped on insert and every
    # sibling-scan keys off the NEW name — all three caps would start from
    # zero against a capture that already funded refunds under the
    # predecessor (MULTI alone additionally cannot be
    # refunded — it is not a gateway reference). Route the whole family to
    # the back office, which reconciles against the predecessor chain.
    if ref == "MULTI" or frappe.db.get_value("POS Invoice", original, "amended_from"):
        frappe.throw(_("Original invoice {0} is an amended copy; its card references and refund "
            "allowance belong to the document it replaced. The POS cannot refund it directly — "
            "handle it in the Payroc back office against its predecessor.").format(original))

    client = PayrocClient.from_settings()
    # PRE-ARM: stamp the lock BEFORE the gateway call and make it
    # durable. From here on, EVERY failure mode leaves the re-refund guard
    # armed by default — a timeout (the legacy path's old naked hole), a
    # crash mid-call, or a post-success DB double failure:
    # this path has no row ledger, so an unstamped success + blind retry was
    # a real second refund. Definite non-refunds disarm explicitly below.
    # A failure WRITING the pre-arm aborts before any money moves — plain
    # throw, freely retryable.
    frappe.db.set_value("POS Invoice", return_invoice_name,
        "card_refund_reference", REFUND_LOCK_UNKNOWN, update_modified=False)
    _commit_money_point()
    _money_logger().info(
        f"refund-legacy pre-arm {return_invoice_name}: {REFUND_LOCK_UNKNOWN} "
        f"(original {original}, amount {cc_amt})")

    def _disarm(extra: dict | None = None, cause: str = "") -> None:
        # Definite non-refund: clear the pre-armed lock so ops can retry.
        # If the clear itself fails, the lock stays — safe direction — but
        # the operator must hear the LOCKED story with its manager exit AND
        # the original gateway cause, not a raw 500.
        stamps = {"card_refund_reference": None}
        stamps.update(extra or {})
        try:
            frappe.db.set_value("POS Invoice", return_invoice_name, stamps,
                update_modified=False)
            _commit_money_point()
            _money_logger().info(
                f"refund-legacy disarm {return_invoice_name}: {cause[:200]}")
        except Exception:
            _money_logger().error(
                f"refund-legacy disarm FAILED {return_invoice_name} — lock stays; "
                f"original cause: {cause[:300]}", exc_info=True)
            frappe.throw(_("Refund not executed (cause: {0}), but clearing the pre-armed lock "
                "failed — this invoice stays locked. A manager should check in the Payroc back "
                "office (there should be no refund), then retry with Unlock Card Refund."
                ).format(cause or "?"))

    try:
        resp = client.refund(ref, refund_amount=cc_amt,
            refund_reason=reason or f"Return {return_invoice_name}",
            operator=frappe.session.user)
    except Exception as e:
        if _outcome_known(e):
            _disarm(cause=str(e)[-160:])
            frappe.throw(_("Refund failed: the gateway rejected the request and no money moved; "
                "once the fault is fixed you can simply retry. ({0})"
                ).format(str(e)[-120:]))
        # Outcome unknown — the pre-armed lock stays; manager verifies at the
        # portal and unlocks (UNKNOWN-OUTCOME is the unlockable sentinel).
        frappe.throw(_("Refund gateway error: outcome unknown (it may have been refunded). To "
            "prevent a double refund the automatic refund on this invoice is now locked; check "
            "the actual status in the Payroc back office first. ({0})").format(str(e)[-120:]))
    declined = _declined(resp)
    if declined:
        # Declined inside a 200 — a definite non-refund. Deliberately NOT
        # _throw_if_declined: that helper only records; this
        # path must also CLEAR the pre-armed lock in the same durable write.
        _disarm({"card_response": _resp_json(resp)}, cause=f"declined: {declined}")
        frappe.throw(_("Refund refused by the gateway ({0}).").format(declined))
    # Money moved — overwrite the pre-armed lock with the real facts.
    # On a DB double failure the overwrite never lands and the committed
    # pre-arm UNKNOWN-OUTCOME keeps guarding.
    _persist_money_fact(lambda: frappe.db.set_value("POS Invoice", return_invoice_name, {
            # Never stamp "" — both the re-refund guard above and the cumulative cap
            # key off this field being set.
            "card_refund_reference": (resp or {}).get("refundReference")
                or (resp or {}).get("uniqueReference") or "UNKNOWN",
            "card_response": _resp_json(resp),
            "card_status": "REFUNDED",
            # THIS return's booked fact — legacy is one full-amount shot.
            "card_refunded_amount": cc_amt,
        }, update_modified=False),
        action="refund-legacy", invoice=return_invoice_name,
        facts={"original": original, "amount": cc_amt,
               "unique_reference": (resp or {}).get("refundReference")
                   or (resp or {}).get("uniqueReference") or "",
               "response": resp})
    return {"ok": True, "refund_reference": (resp or {}).get("refundReference") or "",
            "amount": cc_amt, "original": original}


def _refund_across_rows(ret, original: str, orig_rows: list, cc_amt: float,
        reason: str | None) -> dict:
    """Referenced refunds for a multi-card original, allocated NEWEST CHARGE
    FIRST (reverse-order refund, decided by the owner): each card refunds up to its remaining
    (amount − refunded_amount), then allocation crosses to the previous card.
    Row-level refunded_amount is stamped IMMEDIATELY after each gateway
    success — the per-row counters ARE the cumulative cap across sibling
    returns. A decline mid-allocation must NOT be wholesale-retryable (the
    already-refunded slices would double-refund): if anything went through,
    the return's refund_reference is stamped PARTIAL:… which arms the
    re-refund guard, and the residue goes to the back office."""
    remaining_by_row = [(r, flt(r.amount, 2) - flt(r.refunded_amount, 2))
        for r in orig_rows]
    total_avail = flt(sum(a for _r, a in remaining_by_row), 2)
    if flt(cc_amt, 2) > total_avail:
        frappe.throw(_("Refund amount {0} exceeds the combined remaining refundable amount of "
            "the original invoice's cards, {1}."
            ).format(flt(cc_amt, 2), total_avail))
    client = PayrocClient.from_settings()
    rest = flt(cc_amt, 2)
    refs: list[str] = []
    # THIS return's booked fact: accumulated per slice inside the
    # same durable write as the row ledger — feeds the orphan-refund gate.
    booked = flt(frappe.db.get_value("POS Invoice", ret.name,
        "card_refunded_amount") or 0, 2)

    def _lock_and_throw(resp, message, arm=False):
        # A failed slice AFTER successful ones: the successes are already
        # committed; arm the re-refund guard durably BEFORE frappe.throw's
        # request-level rollback wipes this transaction — otherwise a retry
        # re-refunds the slices that already went through (r1 F1, the
        # A past invoice failure class). arm=True forces the lock even
        # with zero successes — for outcome-UNKNOWN failures (r2-B2).
        stamps = {}
        if resp is not None:
            stamps["card_response"] = _resp_json(resp)
        if refs or arm:
            stamps["card_refund_reference"] = (
                (REFUND_LOCK_PARTIAL_PREFIX + ",".join(refs)) if refs
                else REFUND_LOCK_UNKNOWN)[:140]
        if stamps:
            # The lock is itself a money fact: silently failing to arm it
            # would leave the return retryable while slices went (or may have
            # gone) through — the same failure class applied to the guard. On a
            # double write failure this throws its DO-NOT-RETRY message
            # instead of `message`, which is strictly the scarier truth.
            # fact_label: here the gateway DECLINED or is unknown — "succeeded"
            # would tell the manager the refund landed.
            _persist_money_fact(lambda: frappe.db.set_value(
                    "POS Invoice", ret.name, stamps, update_modified=False),
                action="refund-lock", invoice=ret.name,
                facts={"stamps": stamps, "booked_refs": list(refs)},
                fact_label=_("Refund not completed, outcome pending verification (the re-refund "
                    "lock should have been set)"))
        frappe.throw(message)

    for row, avail in reversed(remaining_by_row):
        if rest <= 0:
            break
        take = min(flt(avail, 2), rest)
        if take <= 0:
            continue
        try:
            resp = client.refund(row.unique_reference, refund_amount=take,
                refund_reason=reason or f"Return {ret.name}",
                operator=frappe.session.user)
        except Exception as e:
            # ONE failure sorter (the two arm=True branches
            # were verbatim copies). A definite non-refund is only a
            # PayrocError that KNOWS the gateway did not act (HTTP 4xx, auth
            # failure — request rejected or never sent): those stay retryable
            # when nothing was booked, because a business-hours credential
            # expiry must not mass-lock every return.
            # Everything else — network Timeout/ConnectionError (r2-B2),
            # non-JSON 2xx, HTTP 5xx — is OUTCOME-UNKNOWN:
            # the refund may have landed, so the lock arms unconditionally.
            known = _outcome_known(e)
            if known:
                message = _(
                    "Refund failed: {1} on card {0} was not refunded (the gateway rejected the "
                    "request and no money moved). {2} refunds before this one are already "
                    "booked; if that is 0, you can simply retry once the fault is fixed. ({3})"
                    ).format(row.unique_reference, take, len(refs), str(e)[-120:])
            else:
                message = _(
                    "Refund gateway error: the outcome of {1} on card {0} is unknown (it may "
                    "have been refunded). {2} refunds before this one are already booked; to "
                    "prevent a double refund the automatic refund on this invoice is now "
                    "locked, so check the actual status of this one in the Payroc back office "
                    "first. ({3})"
                    ).format(row.unique_reference, take, len(refs), str(e)[-120:])
            _lock_and_throw(None, message, arm=not known)
        if _declined(resp):
            _lock_and_throw(resp,
                _("Refund refused by the gateway ({0}): {2} on card {1} was not refunded, and "
                  "{3} refunds before this one are already booked. To prevent a double refund "
                  "the automatic refund on this invoice is now locked; handle the remainder by "
                  "hand in the Payroc back office."
                  ).format(_declined(resp), row.unique_reference, take, len(refs)))
        # MONEY MOVED — this slice's ledger entry must survive any later
        # slice's throw and the request rollback that follows it (r1 F1);
        # and a DB failure here must stop the operator, not vanish.
        # Row ledger and the return's booked fact land in the SAME
        # durable write: absolute values computed OUTSIDE the closure keep
        # the retry idempotent (never += twice).
        new_refunded = flt(row.refunded_amount) + take
        booked_after = flt(booked + take, 2)

        def _write_slice(row_name=row.name, nr=new_refunded, ba=booked_after):
            frappe.db.set_value("Card Charge", row_name, "refunded_amount",
                nr, update_modified=False)
            frappe.db.set_value("POS Invoice", ret.name,
                "card_refunded_amount", ba, update_modified=False)

        _persist_money_fact(_write_slice,
            action="refund-slice", invoice=ret.name,
            facts={"unique_reference": (resp or {}).get("refundReference")
                       or (resp or {}).get("uniqueReference") or "",
                   "card": row.unique_reference, "amount": take,
                   "row": row.name, "response": resp})
        booked = booked_after
        refs.append((resp or {}).get("refundReference")
            or (resp or {}).get("uniqueReference") or "UNKNOWN")
        rest = flt(rest - take, 2)
    # card_refunded_amount is deliberately absent here: the row era books
    # it per-slice inside each slice's durable write; the legacy success
    # stamp writes it whole because legacy has no slices.
    _persist_money_fact(lambda: frappe.db.set_value("POS Invoice", ret.name, {
            "card_refund_reference": refs[0] if len(refs) == 1 else "MULTI",
            "card_response": _json.dumps({"refund_references": refs})[:1000],
            "card_status": "REFUNDED",
        }, update_modified=False),
        action="refund-final", invoice=ret.name,
        facts={"refund_references": refs})
    return {"ok": True,
            "refund_reference": refs[0] if len(refs) == 1 else "MULTI",
            "refund_references": refs, "amount": cc_amt, "original": original}


@frappe.whitelist(methods=["POST"])
def unlock_refund_lock(return_invoice_name: str, reason: str) -> dict:
    """Manager unlock for a return whose automatic card refund armed the
    UNKNOWN-OUTCOME re-refund lock (the amplifier: the
    lock had no product-side unlock, so a locked return died in bench
    console).

    ONLY ``UNKNOWN-OUTCOME`` unlocks — zero slices booked, so a full-amount
    retry is exactly the original entitlement. A ``PARTIAL:*`` sentinel is
    the ONE record of how much this return already pushed back; clearing it
    re-arms a full-amount retry that the per-ORIGINAL-card caps cannot bound
    (an original with headroom would refund the booked slices again).
    Its residue goes to back-office manual processing.
    A real refund reference means the refund COMPLETED and never unlocks.

    The unlock is an attestation, not a reconciliation: the gateway may
    actually have refunded (rows still show headroom), so the manager must
    verify in the Payroc portal FIRST — the reason is required and the whole
    action lands in the document timeline (who/when/why/previous lock),
    mirroring manager_override_4473's audit posture.

    Args:
        return_invoice_name: The locked POS RETURN invoice.
        reason: Required attestation — what the manager verified in the
            Payroc portal.

    Returns:
        {ok: True, cleared: <previous lock value>, invoice}.

    Permissions:
        Manager gate (roles per the derived Auth line) + required reason —
        the reason must record what was verified in the Payroc portal.
    """
    _check_manager(_("Unlocking a refund requires manager permission (Sales Manager)."))
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is required to unlock — first check the actual status of each "
            "refund on this invoice in the Payroc back office, then write your findings here."))
    return_invoice_name = (return_invoice_name or "").strip()
    if not frappe.db.exists("POS Invoice", return_invoice_name):
        frappe.throw(_("POS Invoice {0} not found.").format(return_invoice_name))
    # The endpoint is the authoritative gate — re-assert what the button
    # checks. Locked returns are normally DRAFTS (the refund runs before
    # submit), so drafts and submitted docs both qualify; cancelled never.
    meta = frappe.db.get_value("POS Invoice", return_invoice_name,
        ["is_return", "docstatus"], as_dict=True)
    if not meta.is_return:
        frappe.throw(_("{0} is not a return invoice, so it has no refund lock to release."
            ).format(return_invoice_name))
    if int(meta.docstatus) == 2:
        frappe.throw(_("Return invoice {0} is cancelled; there is nothing to unlock."
            ).format(return_invoice_name))
    # Serialize against a refund attempt arming the lock mid-flight.
    current = frappe.db.get_value("POS Invoice", return_invoice_name,
        "card_refund_reference", for_update=True)
    if not _is_unlockable_lock(current):
        # POSITIVE gate first (whitelist, mirroring the JS button) — the
        # branches below only pick the right refusal message.
        if current and current.startswith(REFUND_LOCK_PARTIAL_PREFIX):
            frappe.throw(_("The lock {1} on return invoice {0} cannot be released: it is the "
                "only record of the amount already pushed back, and unlocking to retry would "
                "refund the booked slices again. Handle the remaining amount by hand in the "
                "Payroc back office."
                ).format(return_invoice_name, current))
        if current:
            frappe.throw(_("{1} on return invoice {0} is a real refund reference, not a lock — "
                "a refund that completed cannot be unlocked and refunded again."
                    ).format(return_invoice_name, current))
        frappe.throw(_("Return invoice {0} has no refund lock, so there is nothing to unlock."
            ).format(return_invoice_name))
    # Timeline audit FIRST, same transaction as the clear: either both land
    # or neither does.
    frappe.get_doc("POS Invoice", return_invoice_name).add_comment("Comment", _(
        "The Payroc refund lock was released by {0}. Previous lock value: {1}. Reason "
        "(including what was found at the gateway): {2}. Unlocking presumes it was verified "
        "that the gateway did not process the refund; a retry after unlocking will refund this "
        "invoice's full amount again."
        ).format(frappe.session.user, current, reason))
    frappe.db.set_value("POS Invoice", return_invoice_name,
        "card_refund_reference", None, update_modified=False)
    _commit_money_point()
    _money_logger().info(
        f"refund-unlock {return_invoice_name}: cleared {current!r} "
        f"by {frappe.session.user}: {reason[:500]}")
    return {"ok": True, "cleared": current, "invoice": return_invoice_name}


def block_locked_return_trash(doc, method=None):
    """doc_events on_trash on POS Invoice: a return that carries
    ANY refund money fact must not be deleted — a lock sentinel, a completed
    refund reference, or a nonzero booked amount. Deleting the document
    erases the record both refund gates count (the legacy cumulative cap
    keys off sibling references, the orphan gate off sibling booked —
    probing showed that deleting a refunded DRAFT
    blinds both and re-opens a full re-refund). The lock cases additionally
    skip the unlock's portal-verification attestation. bench console
    (delete_doc ignore_on_trash / raw SQL) stays the documented last-resort
    escape for a reconciled-and-documented case."""
    ref = doc.get("card_refund_reference")
    booked = flt(doc.get("card_refunded_amount"))
    if not ref and booked <= 0:
        return
    if ref == REFUND_LOCK_UNKNOWN:
        frappe.throw(_("Return invoice {0} carries a refund lock (UNKNOWN-OUTCOME) and cannot "
            "be deleted — deleting it would bypass the manager's check and reopen the invoice "
            "for another refund. A manager should check in the Payroc back office and release "
            "it with Unlock Card Refund, or reconcile in the back office."
            ).format(doc.name))
    if ref and ref.startswith(REFUND_LOCK_PARTIAL_PREFIX):
        frappe.throw(_("Return invoice {0} carries a refund lock ({1}) and cannot be deleted — "
            "together with the refunded amount recorded on this invoice it is the evidence of "
            "the money already refunded. Handle the remainder by hand in the Payroc back "
            "office; after reconciling, a manager submits this invoice in Desk to finish it."
            ).format(doc.name, ref))
    frappe.throw(_("Return invoice {0} already has card refund records (reference {1}, refunded "
        "{2}) and cannot be deleted — the refund gate works out each invoice's remaining "
        "refundable amount from these records, and deleting them would let the same goods be "
        "refunded again. Submit or amend this invoice; a reconciled exception goes through the "
        "bench as a last resort.").format(
        doc.name, ref or "-", flt(booked)))


def restore_charge_rows(doc, method=None):
    """doc_events validate on POS Invoice — the charge table's ONLY writers
    are the lifecycle functions in this module (db_insert / db.set_value). A
    client save carries whatever rows its page happened to load: Frappe's
    child-table sync would DELETE rows missing from that payload and write
    stale statuses back (e.g. resurrect a VOIDED charge). DB truth always
    wins here; the client can never add, drop, or mutate a charge row
    (r1 F2 / A past incident).

    Hooked on validate (NOT before_save: validate also runs on submit) AND on
    before_cancel / before_update_after_submit — Frappe's cancel and
    update-after-submit actions skip validate but still run update_children
    off the client payload (r2-M1)."""
    if doc.get("__islocal") or not doc.name:
        if doc.flags.from_restore:
            # A Deleted Document restore re-inserts the invoice's OWN rows —
            # the money facts of a capture that already happened. Stripping
            # them here left a past invoice (2026-09-08) with the
            # CHARGED stamps but no row, so the next card would have jammed
            # block_partial_submit (Σrows ≠ tender). Both restore paths
            # (frappe's deleted_document.restore and
            # ffl_core.api.pos_recycle_bin) raise this flag.
            return
        # A BRAND-NEW doc can own no charge rows AND no booked money — the
        # lifecycle writers only touch persisted invoices. Any payload rows
        # here are foreign: copied in by make_return_doc/amend/duplicate
        # before the card_charges field carried no_copy, or hand-built
        # through the API. Skipping (the earlier behavior) let a return of
        # a row-era sale INSERT the original's rows under its own name —
        # phantom Σcaptured that jammed block_partial_submit on every such
        # return. Likewise, frappe's amend RETAINS no_copy values
        # (in-container proof + regression tests in
        # ffl_core test_consignment_out_reinvoice), so
        # an amended return would inherit its predecessor's
        # card_refunded_amount and the orphan gate would double-count the
        # same pushed money — zero it here BY HOOK, not by field property.
        # The refund LOCK is deliberately NOT cleared: an inherited lock on
        # an amend copy is fail-safe (guarded until a manager acts).
        if doc.get("card_charges"):
            _money_logger().info(
                f"POS Invoice (new, {doc.name or 'unnamed'}): stripped "
                f"{len(doc.get('card_charges'))} foreign charge row(s) from "
                "insert payload — a new invoice owns none.")
            doc.set("card_charges", [])
        if flt(doc.get("card_refunded_amount")):
            _money_logger().info(
                f"POS Invoice (new, {doc.name or 'unnamed'}, amended_from="
                f"{doc.get('amended_from') or '-'}): zeroed inherited "
                f"card_refunded_amount {flt(doc.get('card_refunded_amount'))}")
            doc.set("card_refunded_amount", 0)
        return
    db_rows = frappe.get_all("Card Charge",
        filters={"parenttype": "POS Invoice", "parent": doc.name},
        fields=["*"], order_by="idx asc")
    if not db_rows and not (doc.get("card_charges") or []):
        return

    def _row_shape(r):
        # EVERY server-owned field — a partial shape here let the gateway
        # handles (unique_reference etc.) be written through.
        return (r.get("name"), r.get("status"), flt(r.get("amount")),
            flt(r.get("refunded_amount")), r.get("unique_reference") or "",
            r.get("instruction_id") or "", int(r.get("attempt") or 0),
            r.get("card_summary") or "", r.get("response") or "")

    payload = [_row_shape(r) for r in (doc.get("card_charges") or [])]
    truth = [_row_shape(r) for r in db_rows]
    if payload != truth:
        # Audited restore — the same shape as the 4473 webhook-field
        # protection: a divergent payload is an event worth a trace.
        safe_log_error(
            "Payroc charge rows restored from DB on save",
            f"POS Invoice {doc.name}: client payload carried {payload}; "
            f"DB truth {truth} was restored.")
    # Restore UNCONDITIONALLY — the comparison only decides the audit trace,
    # never whether the DB truth is applied.
    doc.set("card_charges", [])
    for r in db_rows:
        r.pop("doctype", None)
        doc.append("card_charges", r)


def block_partial_submit(doc, method=None):
    """doc_events before_submit on POS Invoice — server-side enforcement that
    what the gateway captured matches what the tender claims. The POS frontend
    short-circuit is only UX; desk submits and direct API calls land here.
    All row reads come from the DB (_charge_rows), never the submitted
    payload — a stale or hand-built payload must not disarm the gate (r1 F2).
      1. an in-flight instruction always blocks (checked before any override);
      2. manual-settlement override: a MANAGER declared out-of-band card money
         (Virtual Terminal etc.) — role-checked server-side, reason mandatory;
      3. charge rows present → Σ CHARGED must equal the Credit Card tender to
         the cent (the gate a past incident showed was missing);
         card-less backend-initiated charges compare against _card_target;
      4. legacy single-stamp era → the original PARTIAL rule, unchanged."""
    rows = _charge_rows(doc.name) if doc.name else []
    if any(r.status == "IN_PROGRESS" for r in rows):
        frappe.throw(_("A card instruction is still in progress on the terminal; wait for it to "
            "finish or cancel it, then submit."))
    if doc.get("card_manual_settlement"):
        # The decision is that a MANAGER may override — enforced hard on the server,
        # not left to the UI.
        _check_manager(_("Submitting over a Manual Card Settlement requires manager permission "
            "(Sales Manager)."))
        if not (doc.get("card_manual_settlement_reason") or "").strip():
            frappe.throw(_("A reason is required when Manual Card Settlement is ticked (who "
                "collected the payment, where, the amount and the receipt)."))
        return
    if rows:
        live = flt(_live_total(rows), 2)
        # Credit Card tender rows first — no blanket grand_total fallback, or a
        # cash sale carrying one declined card attempt could never submit. The
        # fallback applies only when money WAS captured with no card tender
        # row (backend-initiated charges, r1 F11).
        expected = flt(_card_total(doc), 2)
        if live > 0 and expected <= 0:
            expected = flt(_card_target(doc), 2)
        if flt(live, 2) != expected:
            frappe.throw(_("The card amount actually charged ({0}) ≠ the card amount due ({1}) "
                "— the order was changed after the charge, or some money went through outside "
                "the system. Keep swiping to collect the difference, or have a manager tick "
                "Manual Card Settlement and give a reason, then submit."
                ).format(live, expected))
        return
    if doc.get("card_status") == "PARTIAL":
        frappe.throw(_("Only part of the amount was approved on this invoice's card (PARTIAL), "
            "so it cannot be submitted as paid in full; a manager must void it in the back "
            "office and collect again."))


def require_card_reference_on_manual_tender(doc, method=None):
    """doc_events validate on POS Invoice / Sales Invoice — a card run on the
    STANDALONE terminal must carry the approval code that ties it to that
    terminal's batch report.

    Only live when Payroc is switched off AND the operator asked for it
    (integration_gate.card_reference_required). With the gateway running there is
    nothing to type: the approval code comes back from the device and is already
    stored on the invoice as a money fact.

    The document-level rules (submit-only, consolidated exempt, zero rows
    skipped, PAN-shaped references refused) are ffl_core's
    ``require_tender_reference`` — the same ones the Zelle Transaction Number
    gate runs on, so the two cannot drift apart. Only what is tender-specific
    stays here: the mode of payment, the switch, the wording, and two exemptions
    the checks required —

    * **In-flight sales are exempt.** An invoice that already carries gateway
      money facts was charged BEFORE the switch flipped; its reference is a
      money fact on the document and no terminal receipt exists to copy a code
      from. Demanding one would strand every order caught mid-transition.
    * **Returns of gateway-charged sales are exempt** (their negative rows,
      that is). The money goes back through the Payroc portal and is evidenced
      by the manager's confirmation (:func:`confirm_out_of_band_refund`), not
      by an approval code — there is no terminal refund receipt. Other returns
      ARE gated: a standalone-terminal refund reverses revenue and sales tax,
      and without a reference nothing shows the money ever left the merchant
      account. Zelle differs here deliberately — see its gate.

    Args:
        doc: The POS Invoice / Sales Invoice being validated.
        method: Frappe doc_event name (unused).
    """
    if not card_reference_required():
        return
    if (getattr(doc, "card_unique_reference", "") or "").strip() or any(
            (getattr(r, "status", "") or "") == "CHARGED"
            for r in (doc.get("card_charges") or [])):
        return
    include_refunds = True
    if int(getattr(doc, "is_return", 0) or 0) and _original_gateway_charged(
            getattr(doc, "name", "") or ""):
        include_refunds = False

    def _message(amount: float) -> str:
        if amount < 0:
            return _(
                "Enter the approval code from the terminal for the {0} refund of {1}. "
                "The card is refunded on a standalone terminal, so this is the only "
                "record that the money actually went back."
            ).format(MODE_OF_PAYMENT_NAME,
                frappe.format_value(abs(amount), {"fieldtype": "Currency"}))
        return _(
            "Enter the approval code from the terminal for the {0} payment of {1}. "
            "Cards are being taken on a standalone terminal, so this is the only "
            "thing tying the sale to that terminal's batch."
        ).format(MODE_OF_PAYMENT_NAME,
            frappe.format_value(amount, {"fieldtype": "Currency"}))

    require_tender_reference(doc, mode_of_payment=MODE_OF_PAYMENT_NAME,
        message=_message, include_refunds=include_refunds)


def block_unconfirmed_gateway_refund(doc, method=None):
    """doc_events validate on POS Invoice — the desk-submit side of the guard
    ``refund_pos_invoice`` enforces for the POS client.

    A return of a gateway-charged sale, with Payroc switched off, must carry a
    manager's portal-refund confirmation before it may submit. The POS client
    path hits the same rule inside ``refund_pos_invoice``; without this hook a
    desk user submitting the return directly would never pass through that
    function at all, and the one enforcement point everyone meets is submit.

    Args:
        doc: The POS Invoice being validated.
        method: Frappe doc_event name (unused).
    """
    if int(getattr(doc, "docstatus", 0) or 0) != 1:
        return
    if not int(getattr(doc, "is_return", 0) or 0):
        return
    if payroc_enabled():
        return  # live gateway: refund_pos_invoice owns this path end to end
    if (getattr(doc, "card_oob_refund_reference", "") or "").strip():
        return
    reference = _original_gateway_charged(getattr(doc, "name", "") or "")
    if not reference:
        return
    frappe.throw(_oob_refund_instruction(reference),
        title=_("Card refund cannot be sent"))


@frappe.whitelist(methods=["POST"])
def confirm_out_of_band_refund(return_invoice_name: str, portal_reference: str,
        reason: str) -> dict:
    """Record that a gateway-charged sale's refund was issued in the Payroc portal.

    The manager-level exit for the switched-off refund guard. Deliberately its
    own field plus a timeline entry — never the tender row's ``reference_no``,
    which the standalone-terminal flow collects for a different purpose and
    which therefore cannot double as this guard's evidence (a guard whose
    evidence another rule force-collects clears itself).

    Args:
        return_invoice_name: The POS RETURN invoice being confirmed.
        portal_reference: The refund's reference from the Payroc portal.
        reason: Why the refund went out of band (audited, mandatory).

    Returns:
        {ok: True, invoice, portal_reference}.

    Raises:
        ValidationError: Missing reference/reason, a PAN-shaped reference, Payroc
            switched on (the live path owns refunds then), unknown or cancelled
            document, not a return, an original that never took gateway money, or
            a confirmation that already exists.

    Permissions:
        Manager role (_check_manager).
    """
    _check_manager(_("Confirming a portal refund requires manager role."))
    return_invoice_name = (return_invoice_name or "").strip()
    portal_reference = (portal_reference or "").strip()
    reason = (reason or "").strip()
    if not portal_reference or not reason:
        frappe.throw(_("Both the portal refund reference and a reason are required."))
    if looks_like_pan(portal_reference):
        frappe.throw(_(
            "That looks like a full card number. Enter the portal refund's "
            "reference — never the card number."))
    if payroc_enabled():
        frappe.throw(_(
            "Payroc is switched on — refunds go through the gateway, so there is "
            "nothing to confirm out of band."))
    meta = frappe.db.get_value("POS Invoice", return_invoice_name,
        ["is_return", "docstatus", "card_oob_refund_reference"], as_dict=True)
    if not meta:
        frappe.throw(_("Unknown POS Invoice {0}.").format(return_invoice_name))
    if not int(meta.is_return or 0):
        frappe.throw(_("{0} is not a return — portal refunds are confirmed on the "
            "return document.").format(return_invoice_name))
    if int(meta.docstatus) == 2:
        frappe.throw(_("{0} is cancelled; there is nothing to confirm on it."
            ).format(return_invoice_name))
    if (meta.card_oob_refund_reference or "").strip():
        frappe.throw(_("{0} already carries portal-refund confirmation {1} — a "
            "second confirmation would hide the first.").format(
            return_invoice_name, meta.card_oob_refund_reference))
    reference = _original_gateway_charged(return_invoice_name)
    if not reference:
        frappe.throw(_(
            "The original sale behind {0} was never charged through the gateway — "
            "there is no portal refund to confirm. A standalone-terminal refund is "
            "recorded by its approval code on the card line instead."
        ).format(return_invoice_name))
    # Timeline FIRST, same transaction as the field write: either both land or
    # neither does (the unlock_refund_lock precedent).
    frappe.get_doc("POS Invoice", return_invoice_name).add_comment("Comment", _(
        "Portal refund confirmed by {0}. Portal reference: {1}. Reason: {2}. "
        "Original gateway charge: {3}."
    ).format(frappe.session.user, portal_reference, reason, reference))
    frappe.db.set_value("POS Invoice", return_invoice_name,
        "card_oob_refund_reference", portal_reference, update_modified=False)
    _money_logger().info(
        f"oob-refund-confirm {return_invoice_name}: {portal_reference!r} "
        f"by {frappe.session.user}: {reason[:500]}")
    return {"ok": True, "invoice": return_invoice_name,
        "portal_reference": portal_reference}
