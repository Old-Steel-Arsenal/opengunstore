"""Card on file — save a card while the customer pays at the terminal, charge it later.

Processor-neutral, like ``payments.api`` (whose charge rows, settle paths and Payment Entry
booking this module reuses): the gateway is reached only through the card processor's optional
card-on-file methods (``CardProcessor.supports_card_on_file``, ``charge_stored``,
``find_stored_charge``, ``delete_stored``).

* **Switch** — Settings → Payments, stored on the ``Card On File Settings`` Single: off by
  default, and on only with a card-on-file authorization (an Agreement Template). Off, nothing
  here runs and every card flow is exactly what it was.
* **Save** — the cashier ticks "Save this card" on a terminal charge (register or counter order).
  :func:`save_request` is the server gate the charge endpoints run before any gateway call: the
  switch, a processor that can keep cards, a real customer (not the walk-in) and that customer's
  signed authorization. The charge row records the intent (``save_card``, ``save_reference``);
  when the sale is approved and the gateway hands back a token, :func:`record_from_charge`
  writes one ``Saved Card`` (idempotent on the merchant reference) and links it from the row.
  Saving never blocks, delays or undoes the sale: an approval without a token, or a save that
  fails, leaves the money exactly where the charge put it and says "charged but not saved".
* **Charge** — :func:`run_saved_card_charge` (whitelisted as :func:`charge_saved_card`) charges a
  saved card against a submitted Sales Invoice's balance, merchant-initiated. The Card Terminal
  Payment row is written and committed BEFORE the gateway call; an approval settles and books
  through ``api._settle_terminal_payment`` (the counter's one booking path). An answer that
  never came back leaves the row IN_PROGRESS — the invoice is locked against another saved-card
  charge until a later call looks the order up at the gateway and settles it. Nothing here ever
  sends the same charge twice.
* **Remove** — :func:`remove_saved_card`: the gateway forgets the card first; only then does the
  record turn Removed. A Removed card is never charged again.

The token lives only in ``Saved Card.token`` (a Password field: encrypted at rest). It is read
just before a charge, never returned by an endpoint, never logged, and scrubbed from every
gateway body stored on a charge row.
"""
from __future__ import annotations

import json as _json
import re

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, escape_html, flt, get_datetime, now_datetime
from frappe.utils.password import get_decrypted_password

from ffl_core.utils import commit as _commit
from ffl_core.utils import single_flag
from ffl_integrations.notifications import safe_log_error
from ffl_integrations.payments.base import APPROVED, DECLINED

SETTINGS = "Card On File Settings"
SAVED_CARD = "Saved Card"
ACTIVE, REMOVED = "Active", "Removed"
# lazy: every card is saved for UNSCHEDULED use (charge a balance when one is due); RECURRING
# arrives with membership renewals, which will pass it through save_request.
DEFAULT_USAGE = "UNSCHEDULED"
# Who may charge or remove a saved card: the managers who settle balances.
MANAGER_ROLES = ("Sales Manager", "Accounts Manager", "System Manager")
# Who may see a customer's saved cards (summary only — never the token).
READ_ROLES = ("Sales User", "Sales Manager", "Accounts Manager", "System Manager")
# A saved-card charge whose answer was lost and that the gateway still does not know after this
# long never reached it (the gateway answers within the request timeout, 30 s by default).
LOOKUP_GRACE_MINUTES = 5
_SIGNED = ("signed", "expiring")
_MASK = "[saved card]"


# ---------------------------------------------------------------- switch and gate


def settings() -> tuple[bool, str | None]:
	"""(switched on, authorization template). The template is returned even while the switch is
	off: a sale started with consent settles under the template it was checked against."""
	enabled = bool(single_flag(SETTINGS, "card_on_file_enabled", default=0))
	template = frappe.db.get_single_value(SETTINGS, "card_on_file_template") or None
	return enabled, template


def offer_enabled() -> bool:
	"""Whether the counter shows "Save this card" at all (boot flag): switched on, an
	authorization picked, and the processor taking new money can keep cards. Never raises."""
	try:
		enabled, template = settings()
		if not (enabled and template):
			return False
		from ffl_integrations.payments import registry
		p = registry.active_processor()
		return bool(p and p.supports_card_on_file)
	except Exception:  # noqa: BLE001 — a boot flag, read on every login
		return False


def _real_customer(customer: str | None) -> bool:
	"""A named customer — not the walk-in / web-store aggregate, not a register's default."""
	if not customer or not frappe.db.exists("Customer", customer):
		return False
	from ffl_core.customer_profile import _system_customer_names

	if customer in _system_customer_names():
		return False
	return not frappe.db.exists("POS Profile", {"customer": customer})


def _authorization(customer: str, template: str | None) -> dict:
	from ffl_core.agreements import agreement_status

	if not template:
		return {"state": "missing", "agreement": None}
	return agreement_status(customer, template)


def _refusal(customer: str | None, p) -> str | None:
	"""Why saving a card is not possible right now (None = it is), the signature aside."""
	enabled, template = settings()
	if not (enabled and template):
		return _("Saving cards is switched off (Settings → Payments → Card on file).")
	if p is None or not p.supports_card_on_file:
		return _("{0} cannot save cards.").format(p.label if p else _("No card processor"))
	if not _real_customer(customer):
		return _("Pick the customer first — a card cannot be saved for the walk-in customer.")
	return None


def save_request(customer: str | None, p) -> dict:
	"""The server gate for "Save this card", run by the charge endpoints BEFORE any gateway call.

	Throws unless saving is switched on, ``p`` can keep cards, ``customer`` is a real customer and
	has signed the card-on-file authorization. Returns the ``save_card`` argument for
	``CardProcessor.start_charge``: a fresh merchant reference and the usage agreement."""
	why = _refusal(customer, p)
	if why:
		frappe.throw(why, title=_("Card not saved"))
	template = settings()[1]
	if _authorization(customer, template)["state"] not in _SIGNED:
		frappe.throw(_("{0} has not signed {1} yet — the card can only be saved after the "
			"customer signs it.").format(escape_html(customer), escape_html(template)),
			title=_("Authorization needed"))
	return {"reference": _mint_reference(customer), "usage": DEFAULT_USAGE}


def _mint_reference(customer: str) -> str:
	"""``<site>-<customer>-<hash8>``, ≤ 200 characters: unique per card for good (the gateway
	never lets a deleted card's reference be used again), readable in the processor's portal."""
	site = re.sub(r"[^A-Za-z0-9]", "", frappe.local.site or "")[:24] or "site"
	who = re.sub(r"[^A-Za-z0-9._-]+", "_", customer).strip("_")[:150] or "customer"
	return f"{site}-{who}-{frappe.generate_hash(length=8)}"[:200]


@frappe.whitelist()
def card_on_file_offer(customer: str | None = None) -> dict:
	"""Whether the counter may offer "Save this card" for this customer, and where the
	customer stands with the authorization (the dialog polls it while the customer signs).

	Args:
	    customer: Customer name (document id).

	Returns:
	    {offer: bool, reason: text when not offered, template, state: signed / expiring /
	    expired / pending / missing, agreement: the copy the state comes from}.
	"""
	frappe.only_for(READ_ROLES)
	from ffl_integrations.payments import registry
	try:
		p = registry.active_processor()
	except Exception:  # noqa: BLE001 — two processors on: the charge itself says so
		p = None
	why = _refusal(customer, p)
	template = settings()[1]
	out = {"offer": why is None, "reason": why or "", "template": template, "state": "missing",
		"agreement": None}
	if why is None:
		status = _authorization(customer, template)
		out.update(state=status["state"], agreement=status.get("agreement"))
	return out


# ---------------------------------------------------------------- saving after an approval


def scrub(raw, secret: str | None):
	"""``raw`` (a gateway body) with every occurrence of ``secret`` masked — the processor-neutral
	backstop behind the adapter's own masking, applied before a body is stored."""
	if not secret or raw is None:
		return raw
	try:
		return _json.loads(_json.dumps(raw, default=str).replace(secret, _MASK))
	except Exception:  # noqa: BLE001
		return {"note": "gateway body withheld (could not be scrubbed)"}


def _scrub_text(text: str, secret: str | None) -> str:
	return text.replace(secret, _MASK) if secret else text


def record_from_charge(p, res, *, row_doctype: str, row, customer: str, source_doctype: str,
		source_name: str) -> dict:
	"""After an approved charge whose row asked to save the card: write the Saved Card once and
	link it from the row. Never raises — the money is already captured and committed.

	Returns {} when the row did not ask; else {card_saved, save_message, saved_card?}."""
	if not cint(row.get("save_card")):
		return {}
	token = (res.stored_credential or "").strip()
	reference = (row.get("save_reference") or "").strip()
	if not token or not reference:
		return {"card_saved": False, "save_message": _(
			"Card charged but not saved: {0} did not keep the card.").format(p.label)}
	savepoint = "saved_card"
	try:
		frappe.db.savepoint(savepoint)
		name = _create_saved_card(p, res, token, reference, row_doctype, row, customer,
			source_doctype, source_name)
		_commit()
	except Exception as e:  # noqa: BLE001 — saving must never undo a captured sale
		try:
			frappe.db.rollback(save_point=savepoint)
		except Exception:  # noqa: BLE001
			pass
		safe_log_error(f"Card on file: card charged but not saved ({source_name})",
			_scrub_text(frappe.get_traceback(), token))
		return {"card_saved": False, "save_message": _(
			"Card charged but not saved: {0}").format(_scrub_text(str(e), token)[:200])}
	if not name:
		# No signed authorization any more (voided while the customer paid): never keep the card.
		try:
			p.delete_stored(reference)
		except Exception:  # noqa: BLE001
			safe_log_error(f"Card on file: unsaved card left at the gateway ({source_name})",
				f"Merchant reference {reference} on {p.label}: the customer's authorization was "
				"gone when the sale settled, and removing the card at the gateway failed. Remove "
				"it in the processor's portal.")
		return {"card_saved": False, "save_message": _(
			"Card charged but not saved: the customer's card-on-file authorization is not signed.")}
	return {"card_saved": True, "saved_card": name,
		"save_message": _("Card saved for later charges ({0}).").format(res.card_summary or name)}


def _create_saved_card(p, res, token, reference, row_doctype, row, customer, source_doctype,
		source_name) -> str | None:
	existing = frappe.db.get_value(SAVED_CARD, {"merchant_reference": reference}, "name")
	if existing:
		_link(row_doctype, row.name, existing)
		return existing
	template = settings()[1]
	consent = _authorization(customer, template)
	if consent["state"] not in _SIGNED or not consent.get("agreement"):
		return None
	has_default = frappe.db.exists(SAVED_CARD, {"customer": customer, "status": ACTIVE, "is_default": 1})
	doc = frappe.get_doc({
		"doctype": SAVED_CARD, "customer": customer, "processor": p.code, "token": token,
		"merchant_reference": reference, "card_summary": res.card_summary or "",
		"card_expiry": res.card_expiry or "", "usage": DEFAULT_USAGE, "status": ACTIVE,
		"is_default": 0 if has_default else 1, "authorization": consent["agreement"],
		"source_doctype": source_doctype, "source_name": source_name,
		"initial_reference": res.unique_reference or "",
	})
	doc.flags.ignore_permissions = True
	try:
		doc.insert()
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		# A racing poll of the same approval saved it first (merchant_reference is unique).
		existing = frappe.db.get_value(SAVED_CARD, {"merchant_reference": reference}, "name")
		if not existing:
			raise
		_link(row_doctype, row.name, existing)
		return existing
	_link(row_doctype, row.name, doc.name)
	return doc.name


def _link(row_doctype: str, row_name: str, card: str) -> None:
	frappe.db.set_value(row_doctype, row_name, "saved_card", card, update_modified=False)


# ---------------------------------------------------------------- reading


@frappe.whitelist()
def list_saved_cards(customer: str) -> dict:
	"""A customer's active saved cards — summary, expiry, default — for the Customer form and the
	Charge saved card picker. Never the token.

	Args:
	    customer: Customer name (document id).

	Returns:
	    {cards: [{name, card_summary, card_expiry, is_default, usage, saved_on}], can_manage:
	    whether the caller may charge or remove them}.
	"""
	frappe.only_for(READ_ROLES)
	frappe.has_permission("Customer", "read", doc=customer, throw=True)
	cards = frappe.get_all(SAVED_CARD, filters={"customer": customer, "status": ACTIVE},
		fields=["name", "card_summary", "card_expiry", "is_default", "usage", "creation"],
		order_by="is_default desc, creation desc")
	return {"cards": [{"name": c.name, "card_summary": c.card_summary, "card_expiry": c.card_expiry,
		"is_default": cint(c.is_default), "usage": c.usage, "saved_on": str(c.creation)} for c in cards],
		"can_manage": bool(set(frappe.get_roles()) & set(MANAGER_ROLES))}


# ---------------------------------------------------------------- charging a saved card


def _lock_message(label: str) -> str:
	return _("{0} did not confirm the saved-card charge, so it may or may not have gone through. "
		"Do not charge this card again and do not take another payment for this amount: click "
		"Charge saved card again in a few minutes — it looks the charge up at the gateway and "
		"records it.").format(label)


def run_saved_card_charge(sales_invoice: str, saved_card: str, amount=None,
		operator: str | None = None) -> dict:
	"""Charge a saved card against a submitted Sales Invoice (the service behind
	:func:`charge_saved_card`; no role check of its own, for server-side callers).

	Refused before the gateway when the card is not Active, belongs to another customer, the
	invoice is not submitted or owes nothing, or ``amount`` exceeds the balance. A saved-card
	charge on this invoice still waiting for its answer is looked up and settled instead of a new
	one. Returns the counter's terminal-payment answer ({ok, status, unique_reference,
	approval_code, amount, payment_entry, outstanding, paid}), plus resolved=True when an earlier
	charge was settled instead."""
	from ffl_integrations.payments import api

	sales_invoice = (sales_invoice or "").strip()
	if not sales_invoice or not frappe.db.exists("Sales Invoice", sales_invoice):
		frappe.throw(_("Sales Invoice {0} not found.").format(sales_invoice))
	card = frappe.db.get_value(SAVED_CARD, saved_card or "", ["name", "customer", "status",
		"processor", "usage", "initial_reference", "card_summary"], as_dict=True)
	if not card:
		frappe.throw(_("Saved card {0} not found.").format(saved_card))
	if card.status != ACTIVE:
		frappe.throw(_("{0} was removed and can no longer be charged.").format(
			card.card_summary or card.name))
	p = api._processor(card.processor)
	if not p.supports_card_on_file:
		frappe.throw(_("{0} cannot charge saved cards.").format(p.label))
	# Two screens / a double click serialize on the invoice (same rule as the terminal charge).
	frappe.db.get_value("Sales Invoice", sales_invoice, "name", for_update=True)
	si = frappe.db.get_value("Sales Invoice", sales_invoice,
		["docstatus", "outstanding_amount", "customer", "customer_name"], as_dict=True)
	if si.docstatus != 1:
		frappe.throw(_("{0} is not a submitted invoice.").format(sales_invoice))
	if si.customer != card.customer:
		frappe.throw(_("{0} is another customer's card.").format(card.card_summary or card.name))

	rows = api._terminal_payments(sales_invoice)
	waiting = [r for r in rows if r.status == "IN_PROGRESS"]
	if waiting:
		row = waiting[0]
		if not row.saved_card:
			frappe.throw(_("A card charge is running on the terminal for {0} — finish or cancel it "
				"first.").format(sales_invoice))
		return _resolve_waiting(api, row)

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

	p.prepare()   # a configuration error surfaces before a record exists
	attempt = len(rows) + 1
	reference = p.order_reference(sales_invoice, attempt)
	rec = frappe.get_doc({"doctype": api.TERMINAL_PAYMENT, "sales_invoice": sales_invoice,
		"amount": amt, "status": "IN_PROGRESS", "instruction_id": reference,
		"processor": p.code, "saved_card": card.name})
	rec.flags.ignore_permissions = True
	rec.insert()
	# The record is durable BEFORE money can move: whatever happens to this request, the
	# invoice now shows a charge in flight and the next call looks it up instead of charging.
	_commit()
	token = get_decrypted_password(SAVED_CARD, card.name, "token", raise_exception=False) or ""
	if not token:
		frappe.db.set_value(api.TERMINAL_PAYMENT, rec.name, {"status": "CANCELED",
			"response": api._resp_json({"error": "saved card has no token"})}, update_modified=False)
		_commit()
		frappe.throw(_("{0} cannot be charged: its gateway token is missing.").format(
			card.card_summary or card.name))
	try:
		res = p.charge_stored(token=token, amount=amt, document=sales_invoice, attempt=attempt,
			description=f"{sales_invoice} — {si.customer_name or ''}".strip(" —"),
			operator=operator or frappe.session.user, usage=card.usage or DEFAULT_USAGE,
			initial_reference=card.initial_reference or "")
	except Exception as e:  # noqa: BLE001 — sorted below; the token is scrubbed from what is kept
		message = _scrub_text(str(e), token)[-300:]
		if api._outcome_known(e):
			frappe.db.set_value(api.TERMINAL_PAYMENT, rec.name, {"status": "CANCELED",
				"response": api._resp_json({"error": message})}, update_modified=False)
			_commit()
			frappe.throw(_("{0} refused the saved-card charge — nothing was charged. {1}").format(
				p.label, escape_html(message)))
		frappe.db.set_value(api.TERMINAL_PAYMENT, rec.name, "response",
			api._resp_json({"error": message, "outcome": "unknown"}), update_modified=False)
		_commit()
		frappe.throw(_lock_message(p.label), title=_("Charge not confirmed"))
	res.raw = scrub(res.raw, token)
	if res.status not in (APPROVED, DECLINED):
		frappe.db.set_value(api.TERMINAL_PAYMENT, rec.name, "response", api._resp_json(res.raw),
			update_modified=False)
		_commit()
		frappe.throw(_lock_message(p.label), title=_("Charge not confirmed"))
	return api._settle_terminal_payment(p, res, api._terminal_payment_row(sales_invoice, reference))


def _resolve_waiting(api, row) -> dict:
	"""A saved-card charge with no recorded answer: ask the gateway what it holds under the
	charge's order reference and settle that — never charge again in this call."""
	p = api._processor(row.processor or "")
	p.prepare()
	res = p.find_stored_charge(row.instruction_id)
	if res.status in (APPROVED, DECLINED):
		out = api._settle_terminal_payment(p, res, row)
		out["resolved"] = True
		if out.get("ok"):
			out["already"] = True
		return out
	created = frappe.db.get_value(api.TERMINAL_PAYMENT, row.name, "creation")
	if get_datetime(created) > add_to_date(now_datetime(), minutes=-LOOKUP_GRACE_MINUTES):
		frappe.throw(_lock_message(p.label), title=_("Charge not confirmed"))
	# Long past the gateway's own timeout and it holds nothing under that id: the charge never
	# reached it. Close the record; the next click charges afresh under a new order id.
	frappe.db.set_value(api.TERMINAL_PAYMENT, row.name, {"status": "CANCELED",
		"response": api._resp_json({"lookup": "not found at the gateway", "raw": res.raw})},
		update_modified=False)
	_commit()
	return {"ok": False, "status": "CANCELED", "resolved": True, "error": _(
		"The earlier saved-card charge never reached {0} — nothing was charged. Charge the card "
		"again if the balance is still due.").format(p.label)}


@frappe.whitelist(methods=["POST"])
def charge_saved_card(sales_invoice: str, saved_card: str, amount=None) -> dict:
	"""Charge a customer's saved card against a submitted Sales Invoice (merchant-initiated).

	Refused before the gateway when the card was removed or is another customer's, the invoice
	is not submitted or owes nothing, or the amount exceeds the balance. The approval books a
	Payment Entry exactly like a terminal payment; a decline books nothing. An answer that never
	came back locks the invoice against another saved-card charge until a later call looks the
	charge up at the gateway and records it.

	Args:
	    sales_invoice: Submitted Sales Invoice with an outstanding balance.
	    saved_card: Saved Card name (must be Active and the invoice customer's).
	    amount: Amount to charge, at most the outstanding balance (blank = the whole balance).

	Returns:
	    {ok, status, unique_reference, approval_code, amount, payment_entry, outstanding, paid}
	    on approval; {ok: False, status, error} on a decline; resolved=True when an earlier
	    charge was settled instead of a new one.
	"""
	frappe.only_for(MANAGER_ROLES)
	return run_saved_card_charge(sales_invoice, saved_card, amount=amount)


# ---------------------------------------------------------------- removing


@frappe.whitelist(methods=["POST"])
def remove_saved_card(saved_card: str, reason: str) -> dict:
	"""Remove a saved card: the card processor forgets it, then the record turns Removed.

	A gateway failure leaves the card Active and says so. A Removed card can never be charged.
	When the default card goes, the newest remaining card becomes the default.

	Args:
	    saved_card: Saved Card name.
	    reason: Why it is removed (kept on the record).

	Returns:
	    {ok: True, status: "Removed"}.
	"""
	frappe.only_for(MANAGER_ROLES)
	from ffl_integrations.payments import api

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Say why the card is removed."))
	card = frappe.db.get_value(SAVED_CARD, saved_card or "", ["name", "customer", "status",
		"processor", "merchant_reference", "is_default", "card_summary"], as_dict=True, for_update=True)
	if not card:
		frappe.throw(_("Saved card {0} not found.").format(saved_card))
	if card.status == REMOVED:
		return {"ok": True, "status": REMOVED}
	p = api._processor(card.processor)
	try:
		p.prepare()
		p.delete_stored(card.merchant_reference)
	except Exception as e:  # noqa: BLE001 — the card stays saved; the manager sees why
		frappe.throw(_("{0} could not remove {1} — it is still saved. {2}").format(
			p.label, card.card_summary or card.name, escape_html(str(e)[-200:])))
	frappe.db.set_value(SAVED_CARD, card.name, {"status": REMOVED, "is_default": 0,
		"removed_by": frappe.session.user, "removed_on": now_datetime(), "removed_reason": reason})
	if cint(card.is_default):
		newest = frappe.get_all(SAVED_CARD, filters={"customer": card.customer, "status": ACTIVE},
			pluck="name", order_by="creation desc", limit=1)
		if newest:
			frappe.db.set_value(SAVED_CARD, newest[0], "is_default", 1)
	# The gateway already forgot the card: keep the record in step with it.
	_commit()
	return {"ok": True, "status": REMOVED}
