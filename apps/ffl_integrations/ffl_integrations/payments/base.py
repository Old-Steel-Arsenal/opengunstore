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

Card on file (optional): a processor that can keep a card at its gateway sets
``supports_card_on_file`` and implements ``charge_stored`` / ``find_stored_charge`` /
``delete_stored``; its ``start_charge`` then also takes ``save_card``. The defaults say "not
supported" (the flag is False and the methods raise ``NotImplementedError``), and the core only
passes ``save_card`` to a processor that set the flag — so an adapter written before card on file
keeps working unchanged. The core keeps the saved card itself (``Saved Card``, token encrypted;
``payments.saved_cards``); the gateway's token never leaves the server.
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
	raw               the gateway's body as parsed JSON; the core stores ``json.dumps(raw)`` (1 KB).
	                  Never put a stored-card token in it — the adapter masks it first
	stored_credential the gateway's token for a card saved by this charge (card on file only;
	                  "" when the card was not saved). Stored encrypted, never shown
	card_expiry       the card's expiry as MMYY, when the gateway sends one
	"""

	status: str
	amount: float = 0.0
	unique_reference: str = ""
	approval_code: str = ""
	card_summary: str = ""
	message: str = ""
	gateway_status: str = ""
	raw: Any = None
	stored_credential: str = ""
	card_expiry: str = ""


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
	# Card on file: True when start_charge takes ``save_card`` and the three stored-card
	# methods below are implemented.
	supports_card_on_file: bool = False

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
			description: str, operator: str, save_card: dict | None = None) -> tuple[str, str]:
		"""Put ``amount`` on the card device for ``document`` (a POS Invoice / Sales Invoice name);
		``attempt`` counts every instruction ever started for that document, declines included.

		``save_card`` (card on file only; passed only when ``supports_card_on_file``):
		``{"reference": <merchant reference>, "usage": "RECURRING" | "UNSCHEDULED"}`` — keep the
		card at the gateway if the sale is approved; ``poll`` then answers its token in
		``CardResult.stored_credential``.

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

	# ------------------------------------------------------------ card on file (optional)

	def order_reference(self, document: str, attempt: int) -> str:
		"""The id the gateway files attempt ``attempt`` on ``document`` under. The core records it
		before a saved-card charge goes out, so a charge whose answer was lost can be looked up."""
		return document if int(attempt or 1) <= 1 else f"{document}-A{int(attempt)}"

	def charge_stored(self, *, token: str, amount: float, document: str, attempt: int,
			description: str, operator: str, usage: str = "UNSCHEDULED",
			initial_reference: str = "") -> CardResult:
		"""Charge a saved card, merchant-initiated (no card present), synchronously.

		``token`` is ``CardResult.stored_credential`` from the sale that saved the card; ``usage``
		the agreement it was saved under; ``initial_reference`` that sale's ``unique_reference``
		(the first transaction of the stored-credential series). The gateway's id for this
		charge is ``order_reference(document, attempt)``. APPROVED (``amount`` = authorized),
		DECLINED, or UNKNOWN when the gateway answered but did not settle it. Raises
		:class:`CardProcessorError` with ``outcome_known`` set honestly — False means the charge
		may have gone through and the core looks it up before ever charging again. Never puts the
		token in an error message or in ``raw``."""
		raise NotImplementedError

	def find_stored_charge(self, reference: str) -> CardResult:
		"""Look up a saved-card charge by ``order_reference``: APPROVED / DECLINED as the gateway
		holds it, UNKNOWN when it holds nothing under that id (yet)."""
		raise NotImplementedError

	def delete_stored(self, reference: str) -> None:
		"""Remove a saved card at the gateway by the merchant reference it was saved under.
		Raises :class:`CardProcessorError` when the gateway refused or could not be reached; a
		card the gateway no longer holds counts as removed."""
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
