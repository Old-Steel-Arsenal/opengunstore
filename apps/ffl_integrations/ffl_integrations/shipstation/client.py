"""ShipStation V2 API client.

Thin HTTP wrapper around ``https://api.shipstation.com/v2`` (single ``API-Key``
header). Credentials read from the ShipStation Settings single doctype; sandbox
keys start with ``TEST_`` and hit the same base URL.

Usage:
    from ffl_integrations.shipstation.client import ShipStationClient
    client = ShipStationClient.from_settings()
    shipment = client.create_shipment(body)   # POST /v2/shipments
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import frappe
from ffl_core.credentials import RedactedRepr, Secret
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SETTINGS_DOCTYPE = "ShipStation Settings"
DEFAULT_BASE_URL = "https://api.shipstation.com/v2"


class ShipStationError(RuntimeError):
	"""Raised on non-recoverable ShipStation API errors. Carries the HTTP
	`status_code` (None for non-HTTP failures) so callers can branch on the
	status instead of substring-matching the message."""

	def __init__(self, *args, status_code: int | None = None):
		super().__init__(*args)
		self.status_code = status_code


class ShipStationConfigError(ShipStationError):
	"""Settings missing or incomplete."""


@dataclass(repr=False)
class ShipStationConfig(RedactedRepr):
	api_key: str
	base_url: str
	store_id: str
	ship_from_warehouse_id: str
	default_parcel_weight_oz: float
	timeout: int
	sandbox_mode: bool

	@classmethod
	def from_settings(cls) -> "ShipStationConfig":
		s = frappe.get_single(SETTINGS_DOCTYPE)
		api_key = s.get_password("api_key", raise_exception=False) or ""
		if not api_key:
			raise ShipStationConfigError("ShipStation Settings: API key missing.")
		base_url = (s.base_url_override or DEFAULT_BASE_URL).rstrip("/")
		# The API-Key header is a bearer credential — never send it cleartext.
		# http:// is allowed only for local development proxies (same rule as
		# the WooCommerce client's HTTPS guard).
		if base_url.startswith("http://") and not re.match(
				r"^http://(localhost|127\.\d+\.\d+\.\d+)([:/]|$)", base_url):
			raise ShipStationConfigError(
				"ShipStation Settings: Base URL Override must use HTTPS — the "
				"API key would otherwise be sent in plaintext.")
		return cls(
			api_key=api_key,
			base_url=base_url,
			store_id=(s.store_id or "").strip(),
			ship_from_warehouse_id=(s.ship_from_warehouse_id or "").strip(),
			default_parcel_weight_oz=float(s.default_parcel_weight_oz or 16),
			timeout=int(s.request_timeout_seconds or 30),
			sandbox_mode=bool(s.sandbox_mode),
		)


class ShipStationClient:
	def __init__(self, config: ShipStationConfig, session: requests.Session | None = None) -> None:
		self.config = config
		self.session = session or self._build_session()

	@classmethod
	def from_settings(cls) -> "ShipStationClient":
		return cls(ShipStationConfig.from_settings())

	# -------------------------------------------------- HTTP plumbing

	def _build_session(self) -> requests.Session:
		s = requests.Session()
		# V2 auth is the bare API-Key header — no basic-auth pair, no bearer.
		# Secret: Session.headers is expanded on every requests frame the
		# Error Log prints (ffl_core.credentials).
		s.headers["API-Key"] = Secret(self.config.api_key)
		s.headers["Accept"] = "application/json"
		# Retry 429/5xx with back-off on safe methods only: POST /shipments
		# creates a resource, and a blind retry after a half-completed POST
		# could double-create the shipment in ShipStation.
		retry = Retry(
			total=3,
			backoff_factor=0.5,
			status_forcelist=(429, 500, 502, 503, 504),
			allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
		)
		adapter = HTTPAdapter(max_retries=retry)
		s.mount("https://", adapter)
		s.mount("http://", adapter)
		return s

	def _url(self, path: str) -> str:
		return urljoin(self.config.base_url + "/", path.lstrip("/"))

	def _request(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json_body: Any | None = None,
	) -> Any:
		try:
			response = self.session.request(
				method,
				self._url(path),
				params=params,
				json=json_body,
				timeout=self.config.timeout,
			)
		except requests.RequestException as exc:
			# Timeouts/DNS/TLS failures must surface as ShipStationError so the
			# whitelisted layer's except clauses turn an outage into a clean
			# message instead of a raw traceback. status_code=None marks
			# non-HTTP failures.
			raise ShipStationError(
				f"{method} {path} → network error: {exc}") from exc
		if response.status_code >= 400:
			raise ShipStationError(
				f"{method} {path} → {response.status_code}: {response.text[:500]}",
				status_code=response.status_code,
			)
		if not response.content:
			return None
		try:
			return response.json()
		except ValueError as exc:
			raise ShipStationError(
				f"{method} {path} → non-JSON response: {response.text[:500]}"
			) from exc

	# -------------------------------------------------- domain methods

	def create_shipment(self, shipment: dict) -> dict:
		"""POST one shipment envelope to /v2/shipments and return the created
		shipment dict. ShipStation reports per-shipment validation problems
		inside an HTTP-200 body — those are failures ONLY when no shipment was
		created: if a shipment_id came back, the shipment is live and raising
		would orphan it (nothing persisted, retry double-creates), so
		warning-level errors ride along on the returned dict instead."""
		body = self._request("POST", "shipments", json_body={"shipments": [shipment]}) or {}
		shipments = body.get("shipments") or []
		first = shipments[0] if shipments else {}
		if not first.get("shipment_id"):
			errors = first.get("errors") or []
			detail = "; ".join(
				(e.get("message") if isinstance(e, dict) else str(e)) or str(e)
				for e in errors
			) or "no shipment_id in response"
			raise ShipStationError(f"ShipStation rejected the shipment: {detail}")
		return first

	def find_shipment_by_external_id(self, external_id: str) -> dict | None:
		"""First non-cancelled shipment carrying this external_shipment_id, or
		None. The push path pre-checks this so a lost-response retry adopts the
		shipment ShipStation already created instead of double-creating it
		(cancelled ones don't count — a voided push must not block a re-push)."""
		body = self._request("GET", "shipments",
			params={"external_shipment_id": external_id}) or {}
		for shipment in body.get("shipments") or []:
			if (shipment.get("shipment_status") or "").lower() != "cancelled":
				return shipment
		return None

	def cancel_shipment(self, shipment_id: str) -> None:
		"""PUT /v2/shipments/{id}/cancel — void a shipment that hasn't shipped
		(204, no body). ShipStation refuses once a label exists (the label has
		to be voided first); that surfaces as a ShipStationError for the caller
		to relay to the operator.

		PUT, not POST. This was POST until 2026-09-11 and the endpoint answers
		a POST with 405 Method Not Allowed — so the void leg of Cancel Order
		had never once succeeded, and nobody found out until an operator
		cancelled an already-pushed order: the invoice
		cancelled correctly and the ShipStation shipment stayed live, one
		bought label away from mailing a gun against a cancelled order. The
		verb is `put` in the published contract (ShipEngine operationId
		`cancel_shipments`; ShipStation V2 serves the same operations under
		/v2 with the same verbs) and every other call in this class already
		matches it."""
		self._request("PUT", f"shipments/{shipment_id}/cancel")

	def labels_for_shipment(self, shipment_id: str) -> list[dict]:
		"""GET /v2/labels?shipment_id=… — the labels bought for a shipment, with
		voided ones dropped. Read-only, so it rides the GET-only retry policy
		safely (a re-GET can't double-create anything). The consignment tracking
		poll (shipstation/tasks.py) reads `tracking_number` / `carrier_code` /
		`tracking_url` off the first live label to write tracking back to the CONO.

		A voided label is a dead reprint — filtered on either `status == 'voided'`
		or a truthy `voided` flag — so its stale tracking number can never win over
		a live label."""
		body = self._request("GET", "labels", params={"shipment_id": shipment_id}) or {}
		return _live(body.get("labels"))

	def labels_for_tracking(self, tracking_number: str) -> list[dict]:
		"""GET /v2/labels?tracking_number=… — the live labels carrying this
		tracking number, whichever shipment they were bought on. For a label
		bought on a shipment the POS did not create (staff built the order in
		ShipStation by hand), where labels_for_shipment has nothing to find."""
		body = self._request("GET", "labels", params={"tracking_number": tracking_number}) or {}
		return _live(body.get("labels"))

	def probe_connection(self) -> dict:
		"""Cheap credentials canary: GET /v2/carriers."""
		return self._request("GET", "carriers") or {}

	def list_warehouses(self) -> list[dict]:
		"""GET /v2/warehouses — the ship-from locations. The Settings form's
		Test Connection surfaces these so ship_from_warehouse_id can be filled
		without leaving the desk (the create-shipment API requires one)."""
		body = self._request("GET", "warehouses") or {}
		return body.get("warehouses") or []


def _live(labels: list[dict] | None) -> list[dict]:
	"""Drop voided labels (status 'voided' or a truthy `voided` flag)."""
	return [
		label for label in (labels or [])
		if (label.get("status") or "").lower() != "voided" and not label.get("voided")
	]
