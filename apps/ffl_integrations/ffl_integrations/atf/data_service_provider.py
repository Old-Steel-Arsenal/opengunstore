"""DataServiceProvider — EZ Check via the shared data service.

Talks to the Data Service instead of scraping
fflezcheck.atf.gov locally. The service runs the very same parser this app's
ATFFormScraperProvider uses (ported verbatim, pinned by shared fixtures), so
answers agree — what centralizing buys is ONE cache and ONE polite rate
budget shared with the web store, plus a single place to fix things when ATF
changes their page.

HMAC scheme mirrors the WP plugin's ApiClient:
    sign = HMAC_SHA256(secret, client_id + ts + METHOD + path + body)
sent as x-client-id / x-timestamp / x-signature headers.

The provider never sends force=true: the service's shared cache IS the
point — its TTL bounds staleness for every consumer at once. A POS-side
``verify(force_live=True)`` still refreshes the POS's own row from the
service (possibly served from the service's cache), which also means bulk
re-verification sweeps stay within the shared ATF budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import ClassVar
from urllib.parse import quote, urlsplit

# kept: existing tests patch this module's `requests.post` — `requests` is a
# singleton, so patching it here also intercepts the shared client's calls.
import requests

from ffl_integrations.atf.ez_check import EZCheckResult, EZCheckUnavailable
from ffl_integrations.data_service import client as service_client

REQUEST_TIMEOUT_SECONDS = 15
SEARCH_TIMEOUT_SECONDS = 3  # typeahead path — fail fast, callers fall back to local


def _post(path: str, payload: dict, *, timeout: float) -> dict:
	try:
		return service_client.post(path, payload, timeout=timeout)
	except service_client.DataServiceBudgetExhausted as exc:
		raise EZCheckUnavailable("Data Service: shared EZ Check budget exhausted") from exc
	except service_client.DataServiceUnavailable as exc:
		raise EZCheckUnavailable(str(exc)) from exc


@dataclass
class DataServiceProvider:
	name: ClassVar[str] = "Data Service"

	def verify(self, ffl_number: str) -> EZCheckResult:
		# rawurlencode the licence so a crafted value can't change the signed
		# path — same defence as the WP client.
		path = f"/v1/ffl/{quote(ffl_number, safe='')}/verify"
		data = _post(path, {"force": False}, timeout=REQUEST_TIMEOUT_SECONDS)

		expiry = data.get("expiry_date")
		return EZCheckResult(
			ffl_number=data.get("ffl_number") or ffl_number,
			found=bool(data.get("found")),
			status=data.get("status") or "NotFound",
			licensee_name=data.get("licensee_name"),
			licensee_dba=data.get("licensee_dba"),
			license_type=data.get("license_type"),
			expiry_date=date.fromisoformat(expiry) if expiry else None,
			address=data.get("address"),
			street=data.get("street"),
			street2=data.get("street2"),
			city=data.get("city"),
			state=data.get("state"),
			zip=data.get("zip"),
			raw=data,
		)


def search_remote(query: str, limit: int = 20, *, zip_code: str | None = None,
		radius_miles: float = 10) -> list[dict]:
	"""Full-listing typeahead against the service (82k rows vs the local
	landed cache). Returns rows in the local search()'s dict shape, each
	flagged _remote — callers merge with local rows and fall back to
	local-only when this raises EZCheckUnavailable. With ``zip_code`` it is
	the service's geo search: dealers within ``radius_miles`` of that ZIP."""
	q = (query or "").strip()
	zip5 = (zip_code or "").strip()[:5]
	if not q and not zip5:
		return []
	digits = sum(1 for ch in q if ch.isdigit())
	payload: dict = {"limit": limit}
	if zip5:
		payload.update(zip=zip5, radius_miles=radius_miles)
	if q and digits >= 9:
		payload["license_number"] = q
	elif q:
		payload["name"] = q

	data = _post("/v1/ffl/search", payload, timeout=SEARCH_TIMEOUT_SECONDS)
	rows = []
	for r in data.get("results") or []:
		rows.append({
			"ffl_number": r.get("license_number"),
			"license_type": r.get("type_code"),
			"licensee_name": r.get("license_name"),
			"licensee_dba": r.get("trade_name"),
			"street": r.get("premise_street"),
			"street2": None,
			"city": r.get("premise_city"),
			"state": r.get("premise_state"),
			"zip": r.get("premise_zip"),
			"phone": r.get("voice_phone"),
			"expiry_date": None,  # the monthly listing carries no real date
			"_remote": True,
		})
	return rows
