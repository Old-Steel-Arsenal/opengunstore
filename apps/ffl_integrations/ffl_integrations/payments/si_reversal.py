"""Give card money back on a counter Sales Invoice: void a charge, or refund part or all of it.

A counter Sales Invoice takes card money two ways — the terminal (``api.charge_sales_invoice``) and
a saved card (``saved_cards.charge_saved_card``). Each capture is one ``Card Terminal Payment`` row
plus the Receive Payment Entry that booked it. This module sends the money back through the
processor that took it and books what happened (cpa ruling, 2026-10-29):

* **Void** — the whole charge, before the processor settles it. The Payment Entry is cancelled when
  it was posted today, settles only this charge and is not bank-cleared; otherwise a Journal Entry
  today puts the amount back on the invoice (Dr Debtors / Cr the clearing account the payment went
  to). Either way the invoice owes the charge again.
* **Refund** — any part of what is left on the charge, settled or not:
  ``Return`` (goods or service given back, or the price lowered): a credit note against the invoice,
  scaled pro rata so revenue and tax come back in proportion, and a Pay Payment Entry out of the
  clearing account settling it. Only for invoices that never touched stock or the bound book —
  those go through Cancel Order / a return. Membership (deferred) lines only once the membership has
  been ended (every deferred line carries a service stop date).
  ``Collect again`` (the sale stands, the money comes another way): a Journal Entry today puts the
  amount back on the invoice; revenue and tax are untouched.

The charge row is locked BEFORE the processor is asked (``reversal_lock``, committed): if the
answer never comes back — or the request dies after money moved — the row stays locked and nothing
can move money on it until a manager checks the processor's portal and resolves it
(:func:`resolve_card_reversal`). A definite refusal clears the lock and books nothing.
"""
from __future__ import annotations

import json as _json

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, getdate, now_datetime, nowdate

from ffl_core.utils import commit as _commit
from ffl_integrations.payments import api
from ffl_integrations.payments.base import APPROVED, CardProcessorError
from ffl_integrations.payments.setup import MODE_OF_PAYMENT_NAME

VOID, REFUND = "void", "refund"
RETURN, COLLECT_AGAIN = "Return", "Collect again"
REFUND_TYPES = (RETURN, COLLECT_AGAIN)
CHARGED, VOIDED, REFUNDED = "CHARGED", "VOIDED", "REFUNDED"
MARKER = "[card-refund]"
_EPS = 0.005


# ------------------------------------------------------------------ reading the charge

def _row(terminal_payment: str):
	"""The charge row, its invoice locked first (the same serialization every charge path uses)."""
	si_name = frappe.db.get_value(api.TERMINAL_PAYMENT, terminal_payment or "", "sales_invoice")
	if not si_name:
		frappe.throw(_("Card payment {0} not found.").format(terminal_payment))
	frappe.db.get_value("Sales Invoice", si_name, "name", for_update=True)
	row = frappe.db.get_value(api.TERMINAL_PAYMENT, terminal_payment,
		["name", "sales_invoice", "amount", "status", "unique_reference", "approval_code", "card_summary",
			"payment_entry", "processor", "refunded_amount", "reversal_lock"], as_dict=True, for_update=True)
	return row, frappe.get_doc("Sales Invoice", si_name)


def _left(row) -> float:
	return flt(flt(row.amount) - flt(row.refunded_amount), 2)


def _check_live(row, si) -> None:
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(si.name))
	if row.reversal_lock:
		frappe.throw(_("A void or refund on this charge was never confirmed by the card processor. Check "
			"its portal, then use Resolve card reversal before anything else."), title=_("Unconfirmed"))
	if row.status not in (CHARGED, REFUNDED) or _left(row) <= _EPS:
		frappe.throw(_("This card charge has nothing left to give back."))
	if not row.unique_reference:
		frappe.throw(_("This card charge has no gateway reference; handle it in the processor's portal."))


def _deferred(si) -> list:
	return [i for i in si.items if cint(i.enable_deferred_revenue)]


def _stock_or_book(si) -> str | None:
	"""Why a credit note from here would leave stock or the bound book wrong, or None."""
	if cint(si.update_stock):
		return _("it moved stock")
	codes = list({i.item_code for i in si.items})
	if codes and frappe.get_all("Item", filters={"name": ("in", codes), "is_stock_item": 1}, limit=1):
		return _("it sells stock items")
	if any(i.get("serial_no") or i.get("serial_and_batch_bundle") or i.get("delivery_note") for i in si.items):
		return _("it carries serial numbers or a delivery")
	if frappe.db.exists("Delivery Note Item", {"against_sales_invoice": si.name, "docstatus": 1}):
		return _("it has a delivery note")
	if frappe.db.exists("DocType", "FFL Disposition") and frappe.db.exists(
			"FFL Disposition", {"sales_invoice": si.name, "docstatus": 1}):
		return _("it has an FFL disposition")
	return None


def _clearing_account(row, si) -> str:
	"""Where the charge's money sits: the account its Payment Entry received into."""
	account = row.payment_entry and frappe.db.get_value("Payment Entry", row.payment_entry, "paid_to")
	if not account:
		from ffl_core.api.manual_order import _mode_of_payment_account
		account = _mode_of_payment_account(MODE_OF_PAYMENT_NAME, si.company)
	if not account:
		frappe.throw(_("No account is set for {0} on company {1}.").format(MODE_OF_PAYMENT_NAME, si.company))
	return account


# ------------------------------------------------------------------ bookings

def _back_on_invoice(si, amount: float, account: str, reference: str, remark: str) -> str:
	"""Journal Entry today: Dr the customer against the invoice, Cr the clearing account — the invoice
	owes *amount* again (cpa C1/C2)."""
	je = frappe.get_doc({
		"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": si.company,
		"posting_date": nowdate(), "cheque_no": reference or si.name, "cheque_date": nowdate(),
		"user_remark": remark[:1000],
		"accounts": [
			{"account": si.debit_to, "party_type": "Customer", "party": si.customer,
				"debit_in_account_currency": amount, "reference_type": "Sales Invoice", "reference_name": si.name},
			{"account": account, "credit_in_account_currency": amount},
		],
	})
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def _cancellable_payment(row) -> bool:
	"""cpa C1: the charge's Payment Entry was posted today, settles exactly this charge on this invoice,
	and no bank reconciliation cleared it."""
	if not row.payment_entry:
		return False
	pe = frappe.db.get_value("Payment Entry", row.payment_entry,
		["docstatus", "posting_date", "paid_amount", "clearance_date"], as_dict=True)
	if not pe or pe.docstatus != 1 or pe.clearance_date or getdate(pe.posting_date) != getdate(nowdate()):
		return False
	if abs(flt(pe.paid_amount) - flt(row.amount)) > _EPS:
		return False
	refs = frappe.get_all("Payment Entry Reference", filters={"parent": row.payment_entry},
		fields=["reference_doctype", "reference_name", "allocated_amount"])
	return len(refs) == 1 and refs[0].reference_doctype == "Sales Invoice" \
		and refs[0].reference_name == frappe.db.get_value(api.TERMINAL_PAYMENT, row.name, "sales_invoice") \
		and abs(flt(refs[0].allocated_amount) - flt(row.amount)) <= _EPS


def _book_void(row, si, reference: str, reason: str) -> dict:
	if _cancellable_payment(row):
		pe = frappe.get_doc("Payment Entry", row.payment_entry)
		pe.flags.ignore_permissions = True
		pe.cancel()
		entry_type, entry = "Payment Entry", pe.name
	else:
		entry_type, entry = "Journal Entry", _back_on_invoice(si, flt(row.amount), _clearing_account(row, si),
			reference, _("Void of card charge {0} on {1}: {2}").format(row.unique_reference, si.name, reason))
	frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, {"status": VOIDED, "refunded_amount": flt(row.amount),
		"reversal_entry_type": entry_type, "reversal_entry": entry, "reversal_lock": None}, update_modified=False)
	_note(si, _("{0} Voided card charge {1} ({2}, {3}): {4}. Booked: {5} {6}.").format(MARKER,
		row.unique_reference, row.card_summary or _("card"), _money(row.amount), reason, entry_type, entry))
	return {"entry_type": entry_type, "entry": entry}


def _refund_payment(si, cn_name: str, amount: float, account: str, reference: str, reason: str) -> str:
	"""Pay Payment Entry out of the clearing account, settling the credit note (a return's outstanding
	is negative: ERPNext allocates a refund to it as a negative amount)."""
	cn_total = flt(frappe.db.get_value("Sales Invoice", cn_name, "grand_total"))
	pe = frappe.get_doc({
		"doctype": "Payment Entry", "payment_type": "Pay", "company": si.company, "posting_date": nowdate(),
		"mode_of_payment": MODE_OF_PAYMENT_NAME, "party_type": "Customer", "party": si.customer,
		"paid_from": account, "paid_to": si.debit_to, "paid_amount": amount, "received_amount": amount,
		"source_exchange_rate": 1, "target_exchange_rate": 1, "reference_no": reference or cn_name,
		"reference_date": nowdate(), "custom_remarks": 1,
		"remarks": _("Card refund {0} on {1}: {2}").format(reference, si.name, reason)[:1000],
		"references": [{"reference_doctype": "Sales Invoice", "reference_name": cn_name,
			"total_amount": cn_total, "outstanding_amount": cn_total, "allocated_amount": -amount}],
	})
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _book_refund(row, si, amount: float, refund_type: str, reference: str, reason: str) -> dict:
	account = _clearing_account(row, si)
	cn_name = None
	if refund_type == RETURN:
		cn = _credit_note(si, amount, reason)
		_quietly(lambda: _muted(cn.submit))
		cn_name = cn.name
		# The refund already left: book the credit note for exactly what went back.
		entry_type, entry = "Payment Entry", _refund_payment(si, cn.name, amount, account, reference, reason)
	else:
		entry_type, entry = "Journal Entry", _back_on_invoice(si, amount, account, reference,
			_("Card refund {0} on {1} (collect again): {2}").format(reference, si.name, reason))
	refunded = flt(flt(row.refunded_amount) + amount, 2)
	frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, {"refunded_amount": refunded,
		"status": REFUNDED if refunded + _EPS >= flt(row.amount) else CHARGED, "reversal_lock": None},
		update_modified=False)
	_note(si, _("{0} Refunded {1} of card charge {2} ({3}) — {4}: {5}. Gateway reference {6}. Booked: {7}{8}.").format(
		MARKER, _money(amount), row.unique_reference, row.card_summary or _("card"), _(refund_type), reason,
		reference or "—", f"{_('Credit Note')} {cn_name}, " if cn_name else "", f"{entry_type} {entry}"))
	return {"entry_type": entry_type, "entry": entry, "credit_note": cn_name}


def _credit_note(si, amount: float, reason: str):
	"""Credit note (inserted, not submitted) taking *amount* (gross) back from *si*.

	The whole invoice, never returned before → ERPNext's own return against it. Anything else (part
	of the sale, or a sale already partly returned) → a credit note of the same lines and taxes, each
	line one unit at its share of the amount, not linked by return_against: ERPNext counts returned
	quantities, and a price cut returns none. Its remarks name the invoice. Deferred lines book
	straight to revenue (a refund is not spread over months)."""
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
	from ffl_core.price_approval import mark_system_priced

	total = flt(si.rounded_total or si.grand_total)
	ratio = min(1.0, amount / total) if total else 0
	whole = ratio >= 1 - 1e-9 and not frappe.db.exists("Sales Invoice",
		{"return_against": si.name, "docstatus": ("<", 2)})
	cn = make_sales_return(si.name)
	cn.update({"set_posting_time": 1, "posting_date": nowdate(), "ignore_pricing_rule": 1,
		"disable_rounded_total": 1, "remarks": _("Card refund on {0}: {1}").format(si.name, reason)[:1000]})
	if not whole:
		cn.return_against = None
		cn.set("items", [])
		for line in si.items:
			cn.append("items", {"item_code": line.item_code, "item_name": line.item_name,
				"description": line.description, "uom": line.uom, "conversion_factor": 1, "qty": -1,
				"rate": flt(flt(line.net_amount) * ratio, 2), "income_account": line.income_account,
				"cost_center": line.cost_center, "item_tax_template": line.item_tax_template,
				"item_tax_rate": line.item_tax_rate, "enable_deferred_revenue": 0})
		for tax in cn.taxes:
			if tax.charge_type == "Actual":
				tax.tax_amount = flt(flt(tax.tax_amount) * ratio, 2)
		cn.discount_amount = cn.additional_discount_percentage = 0
		cn.calculate_taxes_and_totals()
		# Cent rounding per line: put the residual on the largest line (net of its tax share);
		# its qty is -1, so raising the rate lowers grand_total.
		gap = flt(-amount - flt(cn.grand_total), 2)
		if abs(gap) >= 0.01 and cn.items:
			big = max(cn.items, key=lambda i: flt(i.rate))
			gross = flt(cn.grand_total) / flt(cn.net_total) if flt(cn.net_total) else 1
			big.rate = flt(flt(big.rate) - gap / gross, 2)   # qty is -1
			cn.calculate_taxes_and_totals()
	mark_system_priced(cn)
	cn.flags.ignore_permissions = True
	_muted(cn.insert)
	return cn


def _credit_note_total(si, amount: float, reason: str) -> float:
	"""What the credit note for *amount* comes to, checked by ERPNext — built and thrown away, so no
	draft sits in the books while the processor is asked."""
	frappe.db.savepoint("card_refund_cn")
	try:
		return flt(-flt(_credit_note(si, amount, reason).grand_total), 2)
	finally:
		frappe.db.rollback(save_point="card_refund_cn")


# ------------------------------------------------------------------ the gateway step

def _lock(row, action: str, **facts) -> None:
	"""Committed before the processor is asked: a lost answer (or a dead request) leaves the charge
	locked for a manager, never open to a second void or refund."""
	frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, "reversal_lock", _json.dumps({"action": action,
		**facts, "by": frappe.session.user, "at": str(now_datetime())}, default=str), update_modified=False)
	_commit()


def _unlock(row) -> None:
	frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, "reversal_lock", None, update_modified=False)
	_commit()


def _ask(row, call, refused: str):
	"""Run the processor *call*. Approved → its result. A definite no → unlock and throw *refused*.
	An unknown outcome → leave the lock and throw the check-the-portal message."""
	try:
		res = call()
	except Exception as e:  # noqa: BLE001 — sorted by what the processor knows
		if isinstance(e, CardProcessorError) and api._outcome_known(e):
			_unlock(row)
			frappe.throw(refused.format(escape_html(str(e)[-300:])))
		frappe.throw(_("The card processor did not confirm it, so the money may or may not have moved. Do not "
			"try again: check the processor's portal for {0}, then use Resolve card reversal on {1}.").format(
			row.unique_reference, row.sales_invoice), title=_("Not confirmed"))
	if res.status != APPROVED:
		_unlock(row)
		frappe.throw(refused.format(escape_html(res.message or _("declined"))))
	return res


# ------------------------------------------------------------------ endpoints

@frappe.whitelist(methods=["POST"])
def void_sales_invoice_charge(terminal_payment: str, reason: str) -> dict:
	"""Void a card charge on a counter Sales Invoice: the processor reverses the whole capture
	(before it settles) and the invoice owes the amount again.

	The charge's Payment Entry is cancelled when it was posted today, settles only this charge and
	is not bank-cleared; otherwise a Journal Entry today books the reversal. Membership invoices
	are refunded instead (type Return).

	Args:
	    terminal_payment: The Card Terminal Payment row of the charge.
	    reason: Why (kept on the invoice timeline and the booking).

	Returns:
	    {ok, status, entry_type, entry, outstanding}.
	"""
	api._check_refund_perm()
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the charge is voided."))
	row, si = _row(terminal_payment)
	_check_live(row, si)
	if flt(row.refunded_amount) > _EPS:
		frappe.throw(_("Part of this charge was already refunded; refund the rest instead of voiding."))
	if _deferred(si):
		frappe.throw(_("{0} is a membership invoice: end the membership, then refund the charge (type Return) "
			"so the credit note takes the revenue back.").format(si.name))
	p = _ready_processor(row)
	_lock(row, VOID, amount=flt(row.amount))
	res = _ask(row, lambda: p.void(row.unique_reference, reason=reason[:100]),
		_("{0} did not void the charge — it may already be settled; refund it instead. {{0}}").format(p.label))
	return _finish(VOID, row, si, p, res.unique_reference or row.unique_reference, reason)


@frappe.whitelist(methods=["POST"])
def refund_sales_invoice_charge(terminal_payment: str, amount, refund_type: str, reason: str) -> dict:
	"""Refund part or all of a card charge on a counter Sales Invoice to the card it came from.

	Return: a credit note takes the amount back from the sale, pro rata over its lines and tax, and
	a Pay Payment Entry settles it (only for invoices that never touched stock or the bound book;
	membership lines once the membership has ended). Collect again: a Journal Entry puts the amount
	back on the invoice to be collected another way.

	Args:
	    terminal_payment: The Card Terminal Payment row of the charge.
	    amount: How much to give back, at most what is left on the charge. A Return refunds the credit
	        note's total, which can differ from this by a cent of rounding.
	    refund_type: "Return" or "Collect again".
	    reason: Why (kept on the invoice timeline and the bookings).

	Returns:
	    {ok, amount, gateway_reference, entry_type, entry, credit_note, outstanding}.
	"""
	api._check_refund_perm()
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the money is given back."))
	if refund_type not in REFUND_TYPES:
		frappe.throw(_("Pick the refund type: Return or Collect again."))
	amt = flt(amount, 2)
	if amt <= 0:
		frappe.throw(_("Amount must be more than zero."))
	row, si = _row(terminal_payment)
	_check_live(row, si)
	left = _left(row)
	if amt > left + _EPS:
		frappe.throw(_("Only {0} is left to refund on this charge.").format(_money(left)))
	if refund_type == RETURN:
		why = _stock_or_book(si)
		if why:
			frappe.throw(_("{0} cannot be refunded as a return from here because {1}. Use Cancel Order or a sales "
				"return for it.").format(si.name, why))
		if any(not i.service_stop_date for i in _deferred(si)):
			frappe.throw(_("{0} is a membership invoice: end the membership first (Cancel → End it today), then "
				"refund.").format(si.name))
		cn_amt = _credit_note_total(si, amt, reason)
		# Never refund a different amount than the credit note takes back.
		if cn_amt <= 0 or abs(cn_amt - amt) > 0.01 or cn_amt > left + _EPS:
			frappe.throw(_("A credit note for {0} on {1} comes to {2}. Enter a different amount, or use Collect "
				"again.").format(_money(amt), si.name, _money(cn_amt)))
		amt = cn_amt
	elif _deferred(si):
		frappe.throw(_("{0} is a membership invoice: refund it as a Return.").format(si.name))
	p = _ready_processor(row)
	_lock(row, REFUND, amount=amt, refund_type=refund_type)
	res = _ask(row, lambda: p.refund(row.unique_reference, amount=amt, reason=reason[:100],
		operator=frappe.session.user),
		_("{0} refused the refund — nothing was refunded. {{0}}").format(p.label))
	return _finish(REFUND, row, si, p, res.unique_reference, reason, amount=amt, refund_type=refund_type)


def _finish(action: str, row, si, p, reference: str, reason: str, *, amount=None, refund_type=None) -> dict:
	"""Book an approved void/refund under the money-fact guard (the money already moved)."""
	out: dict = {}

	def write():
		fresh = frappe.db.get_value(api.TERMINAL_PAYMENT, row.name,
			["name", "amount", "unique_reference", "card_summary", "payment_entry", "refunded_amount", "status"],
			as_dict=True)
		out.update(_book_void(fresh, si, reference, reason) if action == VOID
			else _book_refund(fresh, si, amount, refund_type, reference, reason))

	api._persist_money_fact(write, action=f"si-{action}", invoice=si.name,
		facts={"unique_reference": reference or row.unique_reference, "charge": row.unique_reference,
			"amount": amount if amount is not None else row.amount, "refund_type": refund_type},
		processor_label=p.label)
	return {"ok": True, "status": VOIDED if action == VOID else frappe.db.get_value(
		api.TERMINAL_PAYMENT, row.name, "status"), "amount": flt(amount if amount is not None else row.amount),
		"gateway_reference": reference, **out,
		"outstanding": flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount"))}


@frappe.whitelist(methods=["POST"])
def resolve_card_reversal(terminal_payment: str, went_through, note: str, portal_reference: str | None = None) -> dict:
	"""Settle a void or refund the card processor never confirmed, after looking in its portal.

	Args:
	    terminal_payment: The locked Card Terminal Payment row.
	    went_through: 1 when the portal shows the void/refund happened (it is booked now, exactly as an
	        approval would have been); 0 when it did not (the charge is unlocked, nothing is booked).
	    note: What was checked in the portal.
	    portal_reference: The portal's reference for the void/refund (required when it went through).

	Returns:
	    {ok, booked, ...the booking when booked}.
	"""
	api._check_refund_perm()
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Say what you checked in the processor's portal."))
	row, si = _row(terminal_payment)
	if not row.reversal_lock:
		frappe.throw(_("This card charge has no unconfirmed void or refund."))
	lock = _json.loads(row.reversal_lock)
	if not cint(went_through):
		_unlock(row)
		_note(si, _("{0} The {1} of card charge {2} did not happen (checked: {3}).").format(
			MARKER, _(lock.get("action")), row.unique_reference, note))
		return {"ok": True, "booked": False}
	if si.docstatus != 1:
		frappe.throw(_("{0} is no longer a submitted invoice: book the {1} by hand with the back office.").format(
			si.name, _(lock.get("action"))))
	reference = (portal_reference or "").strip()
	if not reference:
		frappe.throw(_("Enter the portal's reference for the {0}.").format(_(lock.get("action"))))
	p = api._processor(row.processor)
	reason = _("{0} (confirmed in the portal by {1}: {2})").format(lock.get("action"), frappe.session.user, note)
	if lock.get("action") == VOID:
		out = _finish(VOID, row, si, p, reference, reason)
	else:
		out = _finish(REFUND, row, si, p, reference, reason, amount=flt(lock.get("amount")),
			refund_type=lock.get("refund_type"))
	return {**out, "booked": True}


@frappe.whitelist()
def card_charges(sales_invoice: str) -> dict:
	"""The card charges on a Sales Invoice that a manager can void or refund, for the form.

	Args:
	    sales_invoice: The Sales Invoice.

	Returns:
	    {charges: [{name, amount, refunded_amount, left, status, unique_reference, card_summary,
	    locked, lock, can_void}], membership, return_blocked}.
	"""
	api._check_refund_perm()
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	rows = frappe.get_all(api.TERMINAL_PAYMENT, filters={"sales_invoice": si.name,
		"status": ("in", (CHARGED, REFUNDED))}, fields=["name", "amount", "refunded_amount", "status",
		"unique_reference", "card_summary", "reversal_lock"], order_by="creation asc")
	deferred = bool(_deferred(si))
	charges = []
	for r in rows:
		left = _left(r)
		if left <= _EPS and not r.reversal_lock:
			continue
		charges.append({"name": r.name, "amount": flt(r.amount), "refunded_amount": flt(r.refunded_amount),
			"left": left, "status": r.status, "unique_reference": r.unique_reference,
			"card_summary": r.card_summary, "locked": bool(r.reversal_lock),
			"lock": _json.loads(r.reversal_lock) if r.reversal_lock else None,
			"can_void": not deferred and flt(r.refunded_amount) <= _EPS})
	return {"charges": charges, "membership": deferred, "return_blocked": _stock_or_book(si)}


# ------------------------------------------------------------------ small things

def _money(v) -> str:
	return frappe.format_value(flt(v), {"fieldtype": "Currency"})


def _note(si, text: str) -> None:
	si.add_comment("Comment", text)


def _ready_processor(row):
	"""The processor that took the charge, prepared — or a plain refusal before anything is locked."""
	p = api._processor(row.processor)
	if not api._integration_ready(processor=p.code):
		frappe.throw(_("{0} is switched off or not set up, so the POS cannot send this. Give the money back in "
			"its portal.").format(p.label))
	try:
		p.prepare()
	except Exception as e:  # noqa: BLE001 — a setup problem, nothing was sent
		frappe.throw(_("{0} is not ready: {1}").format(p.label, escape_html(str(e)[-300:])))
	return p


def _muted(fn) -> None:
	"""ERPNext's informational pop-ups about the credit note (e.g. "Update Outstanding for Self") are
	not for the person refunding: the timeline note says what was booked. Dropped only on success, so
	a refusal still shows its reason."""
	before = len(frappe.local.message_log or [])
	fn()
	del frappe.local.message_log[before:]


def _quietly(fn) -> None:
	"""No Sale Receipt email for a credit note (receipts.py)."""
	suppressed = frappe.flags.get("ffl_suppress_sale_receipt")
	frappe.flags.ffl_suppress_sale_receipt = True
	try:
		fn()
	finally:
		frappe.flags.ffl_suppress_sale_receipt = suppressed
