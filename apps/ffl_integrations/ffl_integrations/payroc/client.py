"""Payroc Merchant API client — Basic→JWT auth + card-present device instructions.

Auth (per developers.worldnetpayments.com/apis/merchant):

    GET {base}/api/v1/account/authenticate
      header `Authorization: Basic <Merchant API Key>`   (key sent RAW, not base64)
    → 200 { "token": "<JWT>", "tokenType": "Bearer", "expiresIn": <HOURS>, ... }

    Subsequent calls: `Authorization: Bearer <JWT>`. A Merchant-Level key under an
    ISV integration also needs `X-Integration-ID`. We cache the JWT in-process for
    `expiresIn` hours (minus skew); a fresh worker mints its own.

Card-present sale on a physical PAX device (Payroc "device instructions"):

    POST {base}/api/v1/transaction/devices/{serialNumber}/paymentInstructions
      body { terminal, order:{orderId, currency, totalAmount}, autoCapture,
             processAsSale, customizationOptions:{entryMethod} }
      → 202 { paymentInstructionId, status: IN_PROGRESS|COMPLETED|FAILURE|CANCELED, links[] }
    GET  {base}/api/v1/transaction/paymentInstructions/{id}      — poll
    DELETE …/paymentInstructions/{id}                            — cancel in-flight
    GET  <links[].href to the Payment>                           — read transactionResult

The device serialNumber (URL) and the gateway terminal number (body) are TWO
distinct identifiers. Amounts are DECIMAL major units (42.50), not integer cents.
Card data stays on the terminal — out of PCI scope.

Refund / reverse reference a prior payment by its `uniqueReference` (10 chars):

    POST  …/transaction/payments/{uniqueReference}/refunds   { refundAmount, refundReason }
    PATCH …/transaction/payments/{uniqueReference}/reverse   { reversalAmount?, reversalReason? }

There is NO discrete /void — pre-settlement reversal is `reverse`; settled is `refunds`.

Card on file ("Secure Credentials"): a device sale carrying
`credentialOnFile {tokenize: true, merchantReference, usageAgreement}` keeps the card at the
gateway when it is approved; the Payment then carries the token (`credentialsNumber`, BIN
296753) in `transactionResult.storedPaymentCredentials`. A later merchant-initiated charge is

    POST {base}/api/v1/transaction/payments
      body { channel: MOTO, terminal, order:{orderId, currency, totalAmount,
             standingInstructions:{sequence: SUBSEQUENT, processingModel, referenceDataOfFirstTxn}},
             customerAccount:{payloadType: SECURE_CREDENTIALS, credentialsNumber} }
      → 201 Payment (transactionResult.resultCode A / D …)

    DELETE {base}/api/v1/customer/credentials/{merchantReference}?terminal=…   — remove a card
    GET    {base}/api/v1/account/terminals/{terminal}  — terminalFeatures.secureCredentials.enable
"""
from __future__ import annotations

import time
from dataclasses import dataclass
import threading
from typing import Any
from urllib.parse import quote, urlencode

import frappe
import requests
from ffl_core.credentials import RedactedRepr, Secret
from ffl_integrations.payments.base import CardProcessorError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SETTINGS_DOCTYPE = "Payroc Settings"

# Refresh the JWT a bit before it actually expires.
TOKEN_REFRESH_SKEW_SECONDS = 60

# Merchant-initiated charges of a saved card: SECURE_CREDENTIALS payloads are accepted on the WEB
# and MOTO channels only (POS is card-present). A charge the store keys for a customer who is not
# there is mail / telephone order.
STORED_CARD_CHANNEL = "MOTO"

PROD_BASE = "https://payments.worldnettps.com/merchant"
SANDBOX_BASE = "https://testpayments.worldnettps.com/merchant"


class PayrocError(CardProcessorError):
    """Raised on non-recoverable Payroc API errors.

    ``outcome_known`` (from CardProcessorError) distinguishes the two failure
    families a money caller must treat differently:
      True  — the gateway DEFINITELY did not process money for this call
              (HTTP 4xx: request rejected; auth/config failure: request never
              sent). Retrying is safe.
      False — the outcome at the processor is unknowable from here (2xx with
              a non-JSON body, HTTP 5xx: the request may have been processed
              before the failure). A refund caller must arm its re-refund
              lock instead of inviting a retry that could double-refund.
    """


class PayrocConfigError(PayrocError):
    """Settings missing or incomplete."""


@dataclass(repr=False)
class PayrocConfig(RedactedRepr):
    api_key: str
    base_url: str
    default_terminal: str | None
    default_serial_number: str | None
    default_currency: str
    integration_id: str | None
    timeout: int
    saved_card_terminal: str | None = None

    @classmethod
    def from_settings(cls) -> "PayrocConfig":
        s = frappe.get_single(SETTINGS_DOCTYPE)
        api_key = s.get_password("api_key", raise_exception=False) or ""
        if not api_key:
            raise PayrocConfigError("Payroc Settings: Merchant API Key missing.")
        sandbox = bool(int(s.sandbox_mode or 0))
        base = (s.base_url_override or "").strip() or (SANDBOX_BASE if sandbox else PROD_BASE)
        return cls(
            api_key=api_key,
            base_url=base.rstrip("/"),
            default_terminal=(s.default_terminal or "").strip() or None,
            default_serial_number=(s.get("device_serial_number") or "").strip() or None,
            default_currency=(s.default_currency or "USD").strip(),
            integration_id=(s.get("integration_id") or "").strip() or None,
            timeout=int(s.request_timeout_seconds or 30),
            saved_card_terminal=(s.get("saved_card_terminal") or "").strip() or None,
        )


@dataclass
class _BearerCache:
    """In-process JWT cache. Survives across calls in the same worker."""
    token: str = ""
    expires_at: float = 0.0  # epoch seconds

    def is_valid(self) -> bool:
        return bool(self.token) and time.time() < (self.expires_at - TOKEN_REFRESH_SKEW_SECONDS)


# Module-level cache. RQ workers each get their own. A lock guards the mint so
# concurrent gthreads in one worker don't race the refresh (and blank the token).
_bearer = _BearerCache()
_bearer_lock = threading.Lock()


class PayrocClient:
    # Polling a payment instruction is a normal GET (no server-side long-poll);
    # the caller spins. Keep the default request timeout.
    POLL_TIMEOUT_SECONDS = 35

    def __init__(self, config: PayrocConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or self._build_session()

    @classmethod
    def from_settings(cls) -> "PayrocClient":
        return cls(PayrocConfig.from_settings())

    # -------------------------------------------------- HTTP plumbing

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            # Only retry idempotent GETs. Payroc/Worldnet has NO idempotency
            # header, so auto-retrying a lost POST/PATCH (create/refund/reverse)
            # would double-charge; those are guarded per-invoice in the DB and a
            # failed POST surfaces as an error for a deliberate manual retry.
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    # -------------------------------------------------- auth

    def _bearer_token(self) -> str:
        if _bearer.is_valid():
            return _bearer.token
        with _bearer_lock:
            # Re-check inside the lock — another thread may have just refreshed.
            if _bearer.is_valid():
                return _bearer.token
            # `token_*` names are load-bearing: the response object and the
            # parsed body both carry the fresh JWT, and frappe's traceback
            # sanitizer redacts locals by NAME; the header VALUE is a Secret so
            # it prints as nothing in requests' own frames (ffl_core.credentials).
            token_response = self.session.get(
                f"{self.config.base_url}/api/v1/account/authenticate",
                headers={"Authorization": Secret(f"Basic {self.config.api_key}")},
                timeout=self.config.timeout,
            )
            if token_response.status_code >= 400:
                raise PayrocError(
                    f"Payroc authenticate {token_response.status_code}: "
                    f"{token_response.text[:500]}"
                )
            token_body = token_response.json()
            token = token_body.get("token") or ""
            if not token:
                detail = {k: str(v)[:80] for k, v in token_body.items()
                          if not any(mark in k.lower()
                                     for mark in ("token", "key", "secret", "jwt"))}
                raise PayrocError(
                    f"Payroc authenticate returned no token: {detail}")
            # expiresIn is in HOURS (Payroc), not seconds.
            hours = float(token_body.get("expiresIn") or 1)
            _bearer.token = token
            _bearer.expires_at = time.time() + hours * 3600
            try:
                frappe.db.set_single_value(
                    SETTINGS_DOCTYPE, "last_token_refreshed_at", frappe.utils.now_datetime()
                )
            except Exception:
                pass  # best-effort; never block a charge on this
            return token

    def account_summary(self) -> dict:
        """GET /account/authenticate for the ACCOUNT payload (terminals and
        devices), not just the JWT — the shape Terminal Discovery shows a
        manager. Goes through the same credential carrier as everything else:
        a hand-rolled plain-str header here is a Merchant API Key in the Error
        Log (r2 F1)."""
        token_response = self.session.get(
            f"{self.config.base_url}/api/v1/account/authenticate",
            headers={"Authorization": Secret(f"Basic {self.config.api_key}")},
            timeout=self.config.timeout,
        )
        if not token_response.ok:
            return {"error": token_response.text[:500]}
        return token_response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        timeout: int | None = None,
    ) -> Any:
        # `path` may be a relative API path OR an absolute HATEOAS href.
        url = path if path.startswith("http") else f"{self.config.base_url}/{path.lstrip('/')}"
        # `token_headers`: the NAME trips frappe's sanitizer in this frame;
        # the Secret VALUE covers requests' frames (ffl_core.credentials).
        token_headers = {
            "Authorization": Secret(f"Bearer {self._bearer_token()}"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.integration_id:
            token_headers["X-Integration-ID"] = self.config.integration_id
        eff_timeout = timeout or self.config.timeout
        response = self.session.request(
            method, url, json=json_body, headers=token_headers, timeout=eff_timeout
        )
        if response.status_code == 401:
            # JWT may have expired mid-flight; force a refresh and retry once.
            # Invalidate (don't blank) so a concurrent thread never sends "Bearer ".
            _bearer.expires_at = 0.0
            token_headers["Authorization"] = Secret(f"Bearer {self._bearer_token()}")
            response = self.session.request(
                method, url, json=json_body, headers=token_headers, timeout=eff_timeout
            )
        if response.status_code >= 400:
            # 4xx = the gateway rejected the request (nothing processed);
            # 5xx = it may have processed before failing — outcome unknown.
            raise PayrocError(
                f"Payroc {method} {path} {response.status_code}: {response.text[:500]}",
                outcome_known=response.status_code < 500,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # A 2xx we can't parse: the processor most likely DID act (it
            # answered success-ish) — never classify this as a definite no.
            raise PayrocError(
                f"Payroc {method} {path}: non-JSON response: {response.text[:200]}",
                outcome_known=False,
            )

    # -------------------------------------------------- card-present device instructions

    def create_payment_instruction(self, *, amount, order_id: str,
            serial_number: str | None = None, terminal: str | None = None,
            currency: str | None = None, description: str | None = None,
            operator: str | None = None, auto_capture: bool = True,
            process_as_sale: bool = False, credential_on_file: dict | None = None) -> dict:
        """POST devices/{serial}/paymentInstructions — start a card sale on the
        physical PAX. Returns the 202 body (paymentInstructionId, status, links).

        process_as_sale=False + auto_capture=True = Auth + auto-capture: money is
        taken, AND the txn stays reversible in the open batch (our void/reverse
        path needs that). process_as_sale=True is a one-step "Sale" (COMPLETE,
        NOT adjustable even in the open batch) — and our terminal 380572001
        rejected it with X_422_011 "Terminal does not support Sale transactions".

        credential_on_file: ``{"tokenize": True, "merchantReference", "usageAgreement"}`` to keep
        the card at the gateway if the sale is approved (sent as ``credentialOnFile``)."""
        if float(amount) <= 0:
            raise PayrocError(f"amount must be > 0, got {amount}")
        serial = serial_number or self.config.default_serial_number
        if not serial:
            raise PayrocError("No device serialNumber configured (settings or argument).")
        term = terminal or self.config.default_terminal
        if not term:
            raise PayrocError("No gateway terminal configured (settings or argument).")
        order: dict[str, Any] = {
            "orderId": str(order_id)[:24],
            "currency": (currency or self.config.default_currency).upper(),
            "totalAmount": round(float(amount), 2),
        }
        if description:
            order["description"] = description[:1024]
        body: dict[str, Any] = {
            "terminal": term,
            "order": order,
            "autoCapture": bool(auto_capture),
            "processAsSale": bool(process_as_sale),
            "customizationOptions": {"entryMethod": "DEVICE_READ"},
        }
        if operator:
            body["operator"] = operator[:50]
        if credential_on_file:
            body["credentialOnFile"] = dict(credential_on_file)
        return self._request(
            "POST", f"/api/v1/transaction/devices/{serial}/paymentInstructions",
            json_body=body,
        )

    def poll_payment_instruction(self, instruction_id: str) -> dict:
        """GET the instruction status. Caller loops until status leaves IN_PROGRESS
        (COMPLETED | FAILURE | CANCELED)."""
        return self._request(
            "GET", f"/api/v1/transaction/paymentInstructions/{instruction_id}",
            timeout=self.POLL_TIMEOUT_SECONDS,
        )

    def cancel_payment_instruction(self, instruction_id: str) -> dict:
        """DELETE an in-flight instruction (cashier Cancel / overall timeout)."""
        return self._request(
            "DELETE", f"/api/v1/transaction/paymentInstructions/{instruction_id}"
        )

    def retrieve_payment(self, href_or_ref: str) -> dict:
        """GET the Payment resource. Accepts an absolute HATEOAS href OR a bare
        uniqueReference."""
        if str(href_or_ref).startswith("http"):
            return self._request("GET", href_or_ref)
        return self._request("GET", f"/api/v1/transaction/payments/{href_or_ref}")

    # -------------------------------------------------- refund / reverse

    def refund(self, unique_reference: str, *, refund_amount, refund_reason: str,
            operator: str | None = None) -> dict:
        """POST payments/{ref}/refunds — referenced refund (full or partial) back
        to the original card. On an unsettled txn the gateway silently reverses."""
        if float(refund_amount) <= 0:
            raise PayrocError(f"refund_amount must be > 0, got {refund_amount}")
        body: dict[str, Any] = {
            "refundAmount": round(float(refund_amount), 2),
            "refundReason": (refund_reason or "Refund")[:100],
        }
        if operator:
            body["operator"] = operator[:50]
        return self._request(
            "POST", f"/api/v1/transaction/payments/{unique_reference}/refunds",
            json_body=body,
        )

    def reverse(self, unique_reference: str, *, reversal_amount=None,
            reversal_reason: str | None = None, operator: str | None = None) -> dict:
        """PATCH payments/{ref}/reverse — void/reverse an unsettled payment.
        Empty body = full reversal. (Payroc has no discrete /void.)"""
        body: dict[str, Any] = {}
        if reversal_amount is not None:
            body["reversalAmount"] = round(float(reversal_amount), 2)
        if reversal_reason:
            body["reversalReason"] = reversal_reason[:100]
        if operator:
            body["operator"] = operator[:50]
        return self._request(
            "PATCH", f"/api/v1/transaction/payments/{unique_reference}/reverse",
            json_body=body or None,
        )

    # -------------------------------------------------- card on file (Secure Credentials)

    def charge_stored_credential(self, *, credentials_number: str, amount, order_id: str,
            usage: str, first_reference: str | None = None, currency: str | None = None,
            description: str | None = None, operator: str | None = None) -> dict:
        """POST payments — a merchant-initiated charge of a saved card (auth + auto-capture,
        so it stays reversible in the open batch like the device sales). Returns the Payment.

        The charge is marked as a SUBSEQUENT transaction of the stored-credential series
        (``order.standingInstructions``; ``processingModel`` = the agreement the card was saved
        under, ``referenceDataOfFirstTxn`` = the sale that saved it). Goes out on
        ``saved_card_terminal`` when Payroc Settings names one, else the default terminal.
        Never retried here (POST): a lost answer is looked up by orderId instead."""
        if float(amount) <= 0:
            raise PayrocError(f"amount must be > 0, got {amount}")
        term = self.config.saved_card_terminal or self.config.default_terminal
        if not term:
            raise PayrocError("No gateway terminal configured (settings or argument).")
        standing: dict[str, Any] = {"sequence": "SUBSEQUENT", "processingModel": usage}
        if first_reference:
            standing["referenceDataOfFirstTxn"] = {"uniqueReference": first_reference}
        order: dict[str, Any] = {
            "orderId": str(order_id)[:24],
            "currency": (currency or self.config.default_currency).upper(),
            "totalAmount": round(float(amount), 2),
            "standingInstructions": standing,
        }
        if description:
            order["description"] = description[:1024]
        body: dict[str, Any] = {
            "channel": STORED_CARD_CHANNEL,
            "terminal": term,
            "order": order,
            "customerAccount": {"payloadType": "SECURE_CREDENTIALS",
                "credentialsNumber": credentials_number},
            "autoCapture": True,
            "processAsSale": False,
        }
        if operator:
            body["operator"] = operator[:50]
        return self._request("POST", "/api/v1/transaction/payments", json_body=body)

    def delete_credentials(self, merchant_reference: str) -> None:
        """DELETE customer/credentials/{merchantReference} — a logical removal at the gateway
        (the reference can never be reused). Only the terminal that saved the card may delete
        it: the device sales run on the default terminal, so that is the owner."""
        term = self.config.default_terminal
        if not term:
            raise PayrocError("No gateway terminal configured (settings or argument).")
        self._request("DELETE", f"/api/v1/customer/credentials/{quote(merchant_reference, safe='')}?"
            + urlencode({"terminal": term}))

    def load_terminal(self, terminal: str | None = None) -> dict:
        """GET account/terminals/{terminal} — the terminal's configuration (read-only; the
        Payments page probe reads ``terminalFeatures.secureCredentials.enable``)."""
        term = terminal or self.config.default_terminal
        if not term:
            raise PayrocError("No gateway terminal configured (settings or argument).")
        return self._request("GET", f"/api/v1/account/terminals/{quote(term, safe='')}") or {}

    def find_transaction(self, *, order_id: str) -> dict:
        """GET transactions?orderId= — look up a prior txn (HATEOAS links give the
        exact refund/reverse URLs)."""
        return self._request(
            "GET", f"/api/v1/transaction/transactions?orderId={order_id}"
        )

    def search_transactions(self, *, after: str, before: str, max_pages: int = 200,
            time_budget: float | None = None) -> tuple[list[dict], bool]:
        """GET transactions?afterDate=&beforeDate= — every gateway transaction in
        the window (sales, refunds, declines; counter and web orders alike),
        newest first. The dates must be ISO 8601 WITH an offset
        ("2026-09-22T00:00:00-05:00"); a bare date is a 400. Pages hold 10 rows,
        chained by the response's `next` cursor.

        Returns (rows, truncated) — truncated when max_pages or time_budget
        (seconds) ran out first.
        """
        started = time.monotonic()
        base = ("/api/v1/transaction/transactions?afterDate=" + quote(after, safe="")
            + "&beforeDate=" + quote(before, safe=""))
        rows: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            page = self._request("GET", base + (f"&next={quote(cursor, safe='')}"
                if cursor else "")) or {}
            rows.extend(page.get("data") or [])
            cursor = page.get("next")
            if not cursor:
                return rows, False
            if time_budget is not None and time.monotonic() - started > time_budget:
                return rows, True
        return rows, True

    # -------------------------------------------------- helpers

    @staticmethod
    def payment_link(instruction: dict) -> str | None:
        """The Payment resource href from a completed instruction's links[]. Prefer
        rel='payment'; else the first /payments/{ref} href that is NOT a /refunds
        or /reverse sub-resource (avoids following a related link as the Payment)."""
        links = (instruction or {}).get("links") or []
        for link in links:
            if (link.get("rel") or "").lower() == "payment" and link.get("href"):
                return link["href"]
        for link in links:
            href = link.get("href") or ""
            if "/payments/" in href and "/refunds" not in href and "/reverse" not in href:
                return href
        return None
