"""Shared data-service HMAC client — ONE connection (Data Service Settings) for
every domain consumer: the ATF EZ Check provider and the distributor remote catalog. The
scheme mirrors the WP plugin's ApiClient and the service's auth.py:
    sign = HMAC_SHA256(secret, client_id + ts + METHOD + path + body)
The signed path deliberately excludes the query string (service contract)."""
from __future__ import annotations

import hashlib
import hmac
import time

import frappe
import requests

DEFAULT_TIMEOUT_SECONDS = 15


class DataServiceUnavailable(RuntimeError):
	"""Transport failure: unconfigured, unreachable, non-200, or non-JSON."""


class DataServiceBudgetExhausted(DataServiceUnavailable):
	"""429 — a shared server-side budget is exhausted."""


class DataServiceNotFound(DataServiceUnavailable):
	"""404 — the service does not have what was asked for (an unknown UPC, a product
	with no history). The same subclass trick: an answer, not an outage."""


class DataServiceRejected(DataServiceUnavailable):
	"""422 — the service will not answer this request as asked (a malformed key, a
	window too large to return whole). Asking again the same way fails the same way."""


class DataServiceForbidden(DataServiceUnavailable):
	"""403 — this client's credential is valid but lacks the scope for the path.

	Same subclass-of-DataServiceUnavailable trick as the 429 above, so every existing
	`except DataServiceUnavailable` keeps catching it and nothing downstream changes.
	Callers that must tell "not entitled" apart from "service is down" catch this
	first: multi-tenant scope packages are per-client (`catalog:ops` is opt-in),
	so a 403 is a PROVISIONING fact to state plainly, not an
	outage to alarm about.
	"""


def get_connection(distributor: str | None = None) -> tuple[str, str, str]:
	"""(endpoint, client_id, secret). With a distributor CODE, the Distributor
	row for that provider may name its own catalog service (docs/catalog-api:
	one store reading two services); otherwise — and for every cross-house
	read — the site-wide service on Data Service Settings."""
	if distributor:
		own = _distributor_connection(distributor)
		if own:
			return own
	settings = frappe.get_single("Data Service Settings")
	endpoint = (settings.endpoint or "").strip().rstrip("/")
	client_id = (settings.client_id or "").strip()
	secret = settings.get_password("secret", raise_exception=False) or ""
	if not endpoint or not client_id or not secret:
		raise DataServiceUnavailable(
			"Data service is not configured (endpoint, client id and secret are all "
			"required on Data Service Settings).")
	return endpoint, client_id, secret


def own_service_row(code: str) -> str | None:
	"""The Distributor row whose own catalog service a read for house ``code``
	uses: the first row for that provider, priority order, DISABLED rows included,
	that names an endpoint. None: the read goes to the site-wide service (Data
	Service Settings). The one selection both this client and the capability model
	(``data_service._house_service``) use, so they never disagree."""
	# Filter in Python: a Distributor.catalog_endpoint is NULL on rows older
	# than the column, and a SQL `!= ''` on NULL does not answer the way the
	# fallback needs (a NULL/blank endpoint = "use the site service").
	rows = frappe.get_all("Distributor", filters={"provider": code},
		fields=["name", "catalog_endpoint"], order_by="priority asc, modified asc")
	return next((r.name for r in rows if (r.catalog_endpoint or "").strip()), None)


def _distributor_connection(code: str):
	name = own_service_row(code)
	if not name:
		return None
	doc = frappe.get_cached_doc("Distributor", name)
	endpoint = (doc.get("catalog_endpoint") or "").strip().rstrip("/")
	client_id = (doc.get("catalog_client_id") or "").strip()
	secret = doc.get_password("catalog_secret", raise_exception=False) or ""
	if not (endpoint and client_id and secret):
		raise DataServiceUnavailable(
			f"Distributor {name}: catalog service endpoint, client id and secret are "
			f"all required (or leave the endpoint blank to use Data Service Settings).")
	return endpoint, client_id, secret


def signed_headers(client_id: str, secret: str, method: str, path: str, body: bytes) -> dict:
	# tz-exempt: Unix epoch names an instant and carries no zone at all — so the
	# service's replay window compares like with like no matter what timezone
	# either end is configured for. Not a site-clock read: `now_datetime()` here
	# would hand the server a wall clock it cannot check.
	ts = str(int(time.time()))
	msg = f"{client_id}{ts}{method}{path}".encode() + body
	sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
	return {"x-client-id": client_id, "x-timestamp": ts, "x-signature": sig,
		"content-type": "application/json"}


def _request(method: str, path: str, *, params=None, payload=None, timeout: float,
		distributor: str | None = None):
	endpoint, client_id, secret = get_connection(distributor)
	body = frappe.as_json(payload, indent=None).encode() if payload is not None else b""
	headers = signed_headers(client_id, secret, method, path, body)
	url = endpoint + path
	try:
		# Dispatch through requests.get/requests.post (rather than the generic
		# requests.request) so callers that patch requests.get/.post — as the
		# existing ATF provider tests do, via this module's `requests` alias,
		# since it's the same singleton module object either way — keep working.
		if method == "GET":
			response = requests.get(url, params=params, headers=headers, timeout=timeout)
		else:
			response = requests.post(url, params=params, data=body or None,
				headers=headers, timeout=timeout)
	except requests.RequestException as exc:
		raise DataServiceUnavailable(f"Data Service unreachable: {exc}") from exc
	_raise_for_status(response, path)
	try:
		return response.json()
	except ValueError as exc:
		raise DataServiceUnavailable(f"Data Service returned non-JSON: {exc}") from exc


def _raise_for_status(response, path: str) -> None:
	if response.status_code == 429:
		raise DataServiceBudgetExhausted("Data Service: shared budget exhausted")
	if response.status_code == 403:
		# Message shape deliberately identical to the generic branch below — only
		# the TYPE carries the extra meaning, so no caller can start matching on
		# prose. Same for the 404 and 422 below.
		raise DataServiceForbidden(f"Data Service returned 403 for {path}")
	if response.status_code == 404:
		raise DataServiceNotFound(f"Data Service returned 404 for {path}")
	if response.status_code == 422:
		raise DataServiceRejected(f"Data Service returned 422 for {path}")
	if response.status_code != 200:
		raise DataServiceUnavailable(f"Data Service returned {response.status_code} for {path}")


def get_bytes(path: str, *, max_bytes: int, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bytes:
	"""A signed GET of a file (not JSON): streamed, cut off past ``max_bytes`` or
	``timeout`` seconds in all (requests' own timeout is per read), no redirects
	(the signed headers go to the service only)."""
	endpoint, client_id, secret = get_connection()
	deadline = time.monotonic() + timeout
	try:
		response = requests.get(endpoint + path, headers=signed_headers(client_id, secret, "GET", path, b""),
			timeout=timeout, stream=True, allow_redirects=False)
		with response:
			_raise_for_status(response, path)
			data = bytearray()
			for chunk in response.iter_content(64 * 1024):
				data += chunk
				if len(data) > max_bytes:
					raise DataServiceUnavailable(f"Data Service sent more than {max_bytes} bytes for {path}")
				if time.monotonic() > deadline:
					raise DataServiceUnavailable(f"Data Service took more than {timeout:g}s to send {path}")
			return bytes(data)
	except requests.RequestException as exc:
		raise DataServiceUnavailable(f"Data Service unreachable: {exc}") from exc


def post(path: str, payload: dict, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
		distributor: str | None = None) -> dict:
	return _request("POST", path, payload=payload, timeout=timeout, distributor=distributor)


def get(path: str, params: dict | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
		distributor: str | None = None) -> dict:
	return _request("GET", path, params=params, timeout=timeout, distributor=distributor)
