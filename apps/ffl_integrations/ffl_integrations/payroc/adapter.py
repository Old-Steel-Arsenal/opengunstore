"""Payroc as a card processor — the gateway half of the card flow.

Everything here talks to Payroc (Worldnet) and turns its answers into the core's
:class:`~ffl_integrations.payments.base.CardResult`; nothing here writes a document. The money
bookkeeping around it (charge rows, locks, refunds across cards, terminal payments) is
``ffl_integrations.payments.api``. Registered through the ``gunshop_card_processors`` hook like
any other processor (ffl_integrations/hooks.py).

Payroc shapes the adapter keeps to itself: the per-attempt orderId, the device payment
instruction lifecycle (IN_PROGRESS / COMPLETED / FAILURE / CANCELED, then a HATEOAS link to the
Payment), ``transactionResult.resultCode`` (A = approved, D = declined) inside 200 bodies, and
the masked-PAN card summary.

Card on file: a sale started with ``save_card`` asks Payroc to tokenize the card; the token
(``transactionResult.storedPaymentCredentials.credentialsNumber``) comes back to the core only
as ``CardResult.stored_credential`` — every ``raw`` body this adapter hands out has it masked
(:func:`_mask_credentials`), and no error message carries it.
"""
from __future__ import annotations

import copy as _copy
import re as _re

import frappe
import requests
from frappe import _
from frappe.utils import flt

from ffl_integrations import integration_gate
from ffl_integrations.payments.base import (
	APPROVED,
	CANCELED,
	DECLINED,
	PENDING,
	UNKNOWN,
	CardProcessor,
	CardProcessorError,
	CardResult,
)
from ffl_integrations.payroc.client import SETTINGS_DOCTYPE, PayrocClient, PayrocError

# What "approved" looks like in a Payroc transactionResult.
_APPROVED = {"A"}

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


def _txn(payment: dict) -> dict:
	return (payment or {}).get("transactionResult") or {}


# A merchant-initiated charge answers synchronously; only these resultCodes are a definite no
# (D declined, R refer to issuer, C pick up card). E (accepted, result unknown), P (partial) or
# no code at all is not something to guess at: the core keeps the charge locked and looks it up.
_DECLINED_CODES = {"D", "R", "C"}
_MASK = "[saved card]"


def _stored_credentials(payment: dict) -> dict:
	"""The Secure Credentials Payroc created with this payment (empty when none)."""
	return _txn(payment).get("storedPaymentCredentials") or {}


def _mask_credentials(body):
	"""A deep copy of ``body`` with every ``credentialsNumber`` value masked: the token is a
	card-number-shaped secret and ``raw`` is stored on charge rows and shown in the desk."""
	def walk(node):
		if isinstance(node, dict):
			return {k: (_MASK if k == "credentialsNumber" and v else walk(v)) for k, v in node.items()}
		if isinstance(node, list):
			return [walk(v) for v in node]
		return node
	return walk(_copy.deepcopy(body)) if body else body


def _without(text: str, secret: str) -> str:
	return text.replace(secret, _MASK) if secret else text


def _declined(resp: dict) -> str:
	"""Worldnet signals business outcome in the 200 body (resultCode A/D), not
	the HTTP status. Returns the decline message if the response is an explicit
	decline, else ""."""
	tr = _txn(resp)
	if (tr.get("resultCode") or "").upper() == "D" \
			or (tr.get("status") or "").upper() == "DECLINED":
		return tr.get("resultMessage") or tr.get("resultCode") or "declined"
	return ""


def _gateway_card_summary(payment: dict) -> str:
	acct = (payment or {}).get("customerAccount") or {}
	# Digits only — masking formats vary (****1111 / 1111**** / 4097**...5938)
	# and this string prints on the customer receipt (r1 F12).
	digits = _re.sub(r"\D", "", acct.get("maskedPan") or "")[-4:]
	return " ".join(p for p in [(acct.get("cardType") or "").strip(),
		f"•{digits}" if digits else ""] if p)[:120]


def _after_the_fact(resp: dict) -> CardResult:
	"""A reverse / refund answer: approved unless the 200 body is an explicit decline."""
	declined = _declined(resp)
	return CardResult(status=DECLINED if declined else APPROVED, message=declined,
		unique_reference=(resp or {}).get("refundReference") or (resp or {}).get("uniqueReference") or "",
		raw=resp)


class PayrocProcessor(CardProcessor):
	code = "payroc"
	label = "Payroc"
	settings_doctype = SETTINGS_DOCTYPE
	enabled_default = 1
	health_fields = (
		("Token refreshed", "last_token_refreshed_at"),
		("Last charge", "last_charge_at"),
	)

	def __init__(self) -> None:
		self._client = None

	@property
	def client(self) -> PayrocClient:
		if self._client is None:
			self._client = PayrocClient.from_settings()
		return self._client

	# ------------------------------------------------------------ switch

	# Through the module, not imported names: the switch and the go-live gate keep one
	# definition (integration_gate), and whoever patches it there reaches this too.
	def enabled(self) -> bool:
		return integration_gate.payroc_enabled()

	def ready(self, require_device: bool = False) -> bool:
		return integration_gate.payroc_ready(require_device=require_device)

	def prepare(self) -> None:
		self.client                           # noqa: B018 — builds it; config errors raise here

	# ------------------------------------------------------------ card-present charge

	def start_charge(self, *, document, attempt, amount, terminal, description, operator,
			save_card=None):
		cof = None
		if save_card:
			cof = {"tokenize": True, "merchantReference": save_card["reference"],
				"usageAgreement": save_card.get("usage") or "UNSCHEDULED"}
		created = self.client.create_payment_instruction(
			amount=amount, order_id=_order_id(document, attempt), terminal=terminal,
			operator=operator, description=description, credential_on_file=cof)
		iid = (created or {}).get("paymentInstructionId") or ""
		if not iid:
			frappe.throw(_("Payroc returned no paymentInstructionId."))
		return iid, (created or {}).get("status") or ""

	def poll(self, handle: str) -> CardResult:
		"""IN_PROGRESS → PENDING; CANCELED / FAILURE → CANCELED (gateway_status keeps which);
		COMPLETED → follow the HATEOAS link to the Payment and read transactionResult."""
		inst = self.client.poll_payment_instruction(handle) or {}
		st = inst.get("status") or "IN_PROGRESS"
		if st == "IN_PROGRESS":
			return CardResult(status=PENDING, gateway_status=st, raw=inst)
		if st in ("CANCELED", "FAILURE"):
			return CardResult(status=CANCELED, gateway_status=st,
				message=inst.get("errorMessage") or st, raw=inst)
		if st != "COMPLETED":
			# lazy: Payroc documents four instruction states; anything else is reported as
			# unknown (the core keeps polling) rather than guessed at.
			return CardResult(status=UNKNOWN, gateway_status=st, raw=inst)
		href = self.client.payment_link(inst)
		payment = self.client.retrieve_payment(href) if href else inst
		return self._payment_result(payment, gateway_status=st)

	def _payment_result(self, payment: dict, *, gateway_status: str = "",
			undecided: str = DECLINED) -> CardResult:
		"""A Payment → CardResult. ``undecided`` is the status for a resultCode that is neither
		approved nor a definite decline (the device flow has always read it as declined)."""
		tr = _txn(payment)
		code = (tr.get("resultCode") or "").upper()
		approved = code in _APPROVED
		status = APPROVED if approved else DECLINED if code in _DECLINED_CODES else undecided
		token = (_stored_credentials(payment).get("credentialsNumber") or "") if approved else ""
		acct = (payment or {}).get("customerAccount") or {}
		return CardResult(
			status=status,
			amount=flt(tr.get("authorizedAmount") or 0) if approved else 0.0,
			unique_reference=(payment or {}).get("uniqueReference") or "",
			approval_code=tr.get("approvalCode") or "",
			card_summary=_gateway_card_summary(payment) if approved else "",
			# The gateway's result text either way: an approval for 0 is refused with it too.
			message=tr.get("resultMessage") or tr.get("resultCode") or "declined",
			gateway_status=gateway_status, raw=_mask_credentials(payment),
			stored_credential=str(token).strip(),
			card_expiry=str(acct.get("expiryDate") or "") if approved else "")

	def cancel(self, handle: str) -> CardResult:
		resp = self.client.cancel_payment_instruction(handle)
		return CardResult(status=CANCELED, gateway_status="CANCELED", raw=resp)

	# ------------------------------------------------------------ after the charge

	def void(self, unique_reference: str, *, reason: str) -> CardResult:
		# Payroc has no discrete /void: an unsettled capture is reversed.
		return _after_the_fact(self.client.reverse(unique_reference, reversal_reason=reason))

	def refund(self, unique_reference: str, *, amount, reason: str, operator: str) -> CardResult:
		return _after_the_fact(self.client.refund(unique_reference, refund_amount=amount,
			refund_reason=reason, operator=operator))

	# ------------------------------------------------------------ card on file

	supports_card_on_file = True

	def order_reference(self, document: str, attempt: int) -> str:
		return _order_id(document, attempt)

	def charge_stored(self, *, token, amount, document, attempt, description, operator,
			usage="UNSCHEDULED", initial_reference=""):
		"""POST payments (MOTO, SECURE_CREDENTIALS). A 4xx is a definite no; a 5xx, an
		unreadable answer, a timeout or a dropped connection may have charged the card
		(outcome unknown — the core looks the order up before anything else). The token is
		scrubbed from every message on the way out."""
		try:
			payment = self.client.charge_stored_credential(
				credentials_number=token, amount=amount, order_id=_order_id(document, attempt),
				usage=usage or "UNSCHEDULED", first_reference=initial_reference or None,
				description=description, operator=operator)
		except PayrocError as e:
			raise PayrocError(_without(str(e), token), outcome_known=e.outcome_known) from None
		except requests.RequestException as e:
			# The POST may have reached Payroc before the connection broke.
			raise CardProcessorError(_without(f"Payroc did not answer: {type(e).__name__}", token),
				outcome_known=False) from None
		return self._payment_result(payment or {}, undecided=UNKNOWN)

	def find_stored_charge(self, reference: str) -> CardResult:
		"""GET transactions?orderId= — the sale filed under ``reference``: approved and still held
		(read back in full by its uniqueReference), declined, or UNKNOWN. UNKNOWN with
		gateway_status FOUND = Payroc holds a sale under that id that cannot be settled from here
		(an undecided or partial result, or an approval already reversed): never "no record"."""
		from ffl_integrations.payroc.reconcile import HELD_STATUSES

		rows = (self.client.find_transaction(order_id=reference) or {}).get("data") or []
		sales = [t for t in rows if str(t.get("type") or "SALE").upper() == "SALE"
			and str(t.get("orderId") or reference) == reference]
		approved = next((t for t in sales if str(t.get("resultCode") or "").upper() in _APPROVED), None)
		# A status the list leaves out is not held against it; one it names must still be held.
		held = approved is not None and (not approved.get("status")
			or str(approved["status"]).upper() in HELD_STATUSES)
		if approved and held and approved.get("uniqueReference"):
			return self._payment_result(self.client.retrieve_payment(approved["uniqueReference"]) or {},
				undecided=UNKNOWN)
		if sales and all(str(t.get("resultCode") or "").upper() in _DECLINED_CODES for t in sales):
			return CardResult(status=DECLINED, message=sales[0].get("resultMessage") or "declined",
				unique_reference=sales[0].get("uniqueReference") or "", raw=_mask_credentials(sales[0]))
		if sales:
			return CardResult(status=UNKNOWN, gateway_status="FOUND", message=_(
				"Payroc holds a sale under {0} that cannot be recorded automatically — check it in "
				"the Payroc portal.").format(reference), raw=_mask_credentials({"data": rows}))
		return CardResult(status=UNKNOWN, message=_("Payroc holds no answer for {0} yet.").format(reference),
			raw=_mask_credentials({"data": rows}))

	def delete_stored(self, reference: str) -> None:
		"""A card Payroc no longer holds (404) is already removed."""
		try:
			self.client.delete_credentials(reference)
		except PayrocError as e:
			if " 404" in str(e):
				return
			raise

	# ------------------------------------------------------------ setup / health

	def list_terminals(self) -> dict:
		"""The authenticate response lists the key's allowedTerminals — a zero-risk
		connectivity + pairing check."""
		return self.client.account_summary()

	def probe(self) -> list[dict]:
		"""Rides list_terminals — the authenticate call that lists the key's allowed
		terminals — rather than a second HTTP path that could drift from the one the
		charge flow trusts. Not ready after the switch said ON can only mean credentials
		(the one other thing the inert gate tests), an ``{"error": ...}`` body is the gateway
		refusing, and a normal body proves auth end to end."""
		if not self.enabled():
			return [{"label": _("Switch"), "ok": None, "detail": _(
				"Payroc is switched off — nothing was probed. The POS records "
				"Credit Card as a plain tender; the swipe happens on the "
				"standalone terminal.")}]
		checks = [{"label": _("Switch"), "ok": True, "detail": _("enabled")}]
		if not self.ready():
			checks.append({"label": _("Credentials"), "ok": False, "detail": _(
				"Merchant API Key is missing — open Credentials and set it.")})
			return checks
		resp = self.list_terminals() or {}
		if resp.get("error"):
			checks.append({"label": _("Gateway"), "ok": False,
				"detail": str(resp["error"])[:300]})
			return checks
		terminals = resp.get("allowedTerminals") or []
		checks.append({"label": _("Gateway"), "ok": True, "detail": _(
			"authenticated — {0} terminal(s) allowed on this key").format(len(terminals))})
		default_terminal = frappe.db.get_single_value(SETTINGS_DOCTYPE, "default_terminal")
		checks.append({"label": _("Default terminal"),
			"ok": True if default_terminal else None,
			"detail": default_terminal or _("not set")})
		checks.append(self._saved_cards_check(default_terminal))
		return checks

	def _saved_cards_check(self, terminal) -> dict:
		"""Whether the default terminal may keep cards (terminalFeatures.secureCredentials.enable).
		Read-only and informational: an error reads as unknown, never as a failed probe."""
		label = _("Saved cards")
		if not terminal:
			return {"label": label, "ok": None, "detail": _("unknown — no default terminal")}
		try:
			features = (self.client.load_terminal(terminal) or {}).get("terminalFeatures") or {}
			enabled = (features.get("secureCredentials") or {}).get("enable")
		except Exception as e:  # noqa: BLE001 — the probe never fails on this line
			return {"label": label, "ok": None,
				"detail": _("unknown — could not read the terminal ({0})").format(str(e)[:150])}
		if enabled is True:
			return {"label": label, "ok": True, "detail": _("enabled on terminal {0}").format(terminal)}
		if enabled is False:
			return {"label": label, "ok": None, "detail": _(
				"not enabled on terminal {0} — ask Payroc to turn on Secure Credentials before "
				"switching card on file on").format(terminal)}
		return {"label": label, "ok": None, "detail": _("unknown — the terminal does not say")}

	def note_charge(self) -> None:
		try:
			frappe.db.set_single_value(SETTINGS_DOCTYPE, "last_charge_at",
				frappe.utils.now_datetime())
		except Exception:
			pass


def processor() -> PayrocProcessor:
	"""``gunshop_card_processors`` factory."""
	return PayrocProcessor()
