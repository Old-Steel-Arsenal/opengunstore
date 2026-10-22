"""The card processor contract — what a payment gateway adapter implements.

The core keeps every piece of money bookkeeping (``payments.api``: charge rows and their
aggregate stamps on POS Invoice, refund locks, the refund split across cards, sibling-return
guards, the out-of-band refund confirmation, the counter's terminal payments and their Payment
Entries, the doc_event guards). An adapter only talks to its gateway: start a charge on a
device, poll it, cancel it, reverse it, refund it. It never writes a document.

Register a zero-arg factory returning a :class:`CardProcessor` through the
``gunshop_card_processors`` hook (docs/extensions/hooks.md); the integrations app registers
Payroc (``ffl_integrations.payroc.adapter.processor``) the same way. At most one processor is
switched on per site — the single "Credit Card" Mode of Payment is what they share — and every
charge row records the ``code`` of the processor that took it, so void / refund / poll go back
to that processor even after the store has moved to another.

Room for later: card-on-file charges would be one more method with a default that raises
``NotImplementedError`` — no existing adapter has to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

APPROVED, DECLINED, PENDING, CANCELED, UNKNOWN = "APPROVED", "DECLINED", "PENDING", "CANCELED", "UNKNOWN"


class CardProcessorError(RuntimeError):
	"""A gateway call failed.

	``outcome_known`` sorts the two failure families a money caller treats differently:
	  True  — the gateway DEFINITELY did not move money for this call (request rejected, or never
	          sent). Retrying is safe.
	  False — what happened at the gateway cannot be known from here (a server error, a reply that
	          could not be read, a timeout after the request left). A refund caller arms its
	          re-refund lock instead of inviting a retry that could refund twice.
	"""

	def __init__(self, message: str, *, outcome_known: bool = True) -> None:
		super().__init__(message)
		self.outcome_known = outcome_known


@dataclass
class CardResult:
	"""A gateway answer in the core's words.

	status            APPROVED | DECLINED | PENDING | CANCELED | UNKNOWN. A finished instruction
	                  the cardholder approved is APPROVED whatever ``amount`` says — the core
	                  judges a zero or partial authorization itself.
	amount            authorized / captured amount in major units (0 when not approved)
	unique_reference  the gateway's id of the transaction (for a refund: of the refund)
	approval_code     the issuer's approval code, when the gateway sends one
	card_summary      card brand + last digits, printed on the receipt (never a full number)
	message           the gateway's result / failure text for the operator
	gateway_status    the processor's own word for a finished instruction that did not approve
	                  (Payroc: CANCELED / FAILURE); echoed back to the POS in poll answers
	raw               the gateway's body as parsed JSON; the core stores ``json.dumps(raw)`` (1 KB)
	"""

	status: str
	amount: float = 0.0
	unique_reference: str = ""
	approval_code: str = ""
	card_summary: str = ""
	message: str = ""
	gateway_status: str = ""
	raw: Any = None


class CardProcessor:
	"""One card gateway. Subclass it, fill the class attributes, implement the methods.

	``code`` is stored on every charge row (Card Charge / Card Terminal Payment ``processor``):
	choose it once and never change it. ``settings_doctype`` is the processor's own Single
	(credentials, its ``enabled`` switch, the ``health_fields`` timestamps the Payments page shows).
	"""

	code: str = ""
	label: str = ""
	kind: str = "Credit Card"
	settings_doctype: str = ""
	enabled_default: int = 1
	# (label, fieldname) pairs read off the settings Single: "is money actually flowing".
	health_fields: tuple[tuple[str, str], ...] = ()

	# ------------------------------------------------------------ switch

	def enabled(self) -> bool:
		"""The store's switch for this processor (its settings Single ``enabled``)."""
		from ffl_core.utils import single_flag

		return bool(single_flag(self.settings_doctype, "enabled", default=self.enabled_default))

	def ready(self, require_device: bool = False) -> bool:
		"""Switched on AND holding usable credentials (and, with ``require_device``, a card device).
		Not ready = inert: the POS records the Credit Card tender as plain tender."""
		raise NotImplementedError

	def prepare(self) -> None:
		"""Load credentials / build the client now. The core calls it right before the first
		gateway step of a request, so a configuration error surfaces before any money moves."""

	# ------------------------------------------------------------ card-present charge

	def start_charge(self, *, document: str, attempt: int, amount: float, terminal: str | None,
			description: str, operator: str) -> tuple[str, str]:
		"""Put ``amount`` on the card device for ``document`` (a POS Invoice / Sales Invoice name);
		``attempt`` counts every instruction ever started for that document, declines included.

		Returns (handle, status): the handle ``poll`` / ``cancel`` take, and the gateway's status
		word for the new instruction ("" = in progress). Throws when the gateway returned no handle.
		"""
		raise NotImplementedError

	def poll(self, handle: str) -> CardResult:
		"""One look at a started charge: PENDING while the device holds the card, else the outcome."""
		raise NotImplementedError

	def cancel(self, handle: str) -> CardResult:
		"""Cancel a charge still on the device (``raw`` = the gateway's answer)."""
		raise NotImplementedError

	# ------------------------------------------------------------ after the charge

	def void(self, unique_reference: str, *, reason: str) -> CardResult:
		"""Reverse an unsettled capture in full. APPROVED = reversed; DECLINED = the gateway
		refused inside a success reply. Raises :class:`CardProcessorError` when the call failed."""
		raise NotImplementedError

	def refund(self, unique_reference: str, *, amount: float, reason: str, operator: str) -> CardResult:
		"""Refund ``amount`` of a capture back to its card. APPROVED (``unique_reference`` = the
		refund's reference) or DECLINED; raises :class:`CardProcessorError` with ``outcome_known``
		set honestly — the core locks the return when it is False."""
		raise NotImplementedError

	# ------------------------------------------------------------ setup / health

	def list_terminals(self) -> dict:
		"""The terminals / devices the credentials may use (the gateway's own shape), or
		{"error": <text>} when the gateway refused."""
		raise NotImplementedError

	def probe(self) -> list[dict]:
		"""Read-only connection checks for the Payments page: [{label, ok (True/False/None), detail}]."""
		raise NotImplementedError

	def note_charge(self) -> None:
		"""Record a successful capture for the health line (e.g. a "last charge" timestamp)."""
