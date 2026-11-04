"""GunBroker Marketplace API client (v1).

Thin HTTP wrapper around ``https://api.gunbroker.com/v1`` (production) and
``https://api.sandbox.gunbroker.com/v1`` (sandbox). Auth is two headers:
``X-DevKey`` on everything, plus ``X-AccessToken`` from
``POST /Users/AccessToken`` on anything that acts as the seller.

Usage:
    from ffl_integrations.gunbroker.client import GunBrokerClient
    client = GunBrokerClient.from_settings()
    item = client.get_item("16087230")

Scope: the listing side — auth, account/category reads, items, pictures, and
the two listing scans. The order endpoints (``/OrdersSold``, ``/Orders/{id}``,
``/Flags``, ``/Shipping``) arrive with the slice that polls orders; adding
untested methods now would be dead code with a plausible-looking signature.

Four behaviours here are load-bearing and every one of them was measured
against the sandbox rather than read out of the documentation. They are the
reason this is not a fifty-line wrapper:

1. **A 200 does not mean it worked.** ``PUT /Items/{id}`` accepts and silently
   ignores every field it does not consider editable — ``SerialNumber``,
   ``Condition``, ``CategoryID``, ``ListingDuration``, and any key it has never
   heard of — answering ``200 "Item … updated"`` either way. Anything that
   depends on a PUT having taken effect re-reads the item and checks. That is
   also why there is no "end the listing by setting IsActive=false" path: it
   returns 200 and does nothing.

2. **404 means two different things.** Cloudflare fronts the API, so a bad
   ROUTE comes back as an IIS HTML page while a missing RESOURCE comes back as
   JSON. Telling them apart matters more than it sounds: "GunBroker says this
   listing no longer exists" is a fact the sweep acts on, and misreading a
   typo'd URL (or a rate-limit page, or a network blip) as that fact is how a
   gun that is still for sale on GunBroker gets put back on the shelf and sold
   twice. Content-type first, JSON second, message text last.

3. **``gbStatusCode`` is always 0.** It appears in every response, success and
   failure alike, and was 0 in every single one observed. Nothing may branch on
   it.

4. **A date without a ``Z`` is Eastern time, not UTC.** ``/ItemsSelling`` rows
   carry both ``endingDateTime`` and ``endingDateTimeUTC``, and the first is
   exactly four hours behind the second. Reading the suffix-less one as UTC
   puts every deadline four hours wrong.

Nothing here ever logs a request body or a request header: the seller password
travels in a login body and the DevKey in a header. Failures carry the first
500 bytes of the RESPONSE and nothing else.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import frappe
from ffl_core.credentials import RedactedRepr, Secret
import requests

SETTINGS_DOCTYPE = "GunBroker Settings"

PROD_BASE = "https://api.gunbroker.com/v1"
SANDBOX_BASE = "https://api.sandbox.gunbroker.com/v1"

# Dates GunBroker serves without a timezone suffix are in its own local time,
# which is US Eastern (measured: endingDateTime is endingDateTimeUTC minus four
# hours in August, i.e. EDT). Named rather than a fixed offset so the winter
# half of the year is right too.
GB_LOCAL_TZ = ZoneInfo("America/New_York")

# How long we keep an access token before logging in again. GunBroker does not
# document a TTL; an idle token was observed to still be rejected after about
# 16 hours, so this is a self-imposed ceiling well under any plausible real one,
# not a value read from anywhere.
TOKEN_TTL_SECONDS = 1800

_MAX_ATTEMPTS = 3
_ITEM_ID_RE = re.compile(r"itemID:\s*(\d+)", re.IGNORECASE)


class GunBrokerError(RuntimeError):
	"""A GunBroker API call failed.

	``status_code`` is None for a transport failure (timeout, DNS, reset).

	``terminal`` means a human has to do something and a retry will never help:
	a validation or policy rejection, or a route that does not exist. It is NOT
	set for 5xx / 429 / network faults, which must stay retryable so a sweep
	recovers on its own once the cause clears.

	``json_body`` records whether the error response actually parsed as JSON.
	The sweep needs it: a JSON 404 is GunBroker saying "no such item", while an
	HTML 404 is our own bad URL wearing the same status code, and only the first
	is evidence about a listing.
	"""

	def __init__(self, *args, status_code: int | None = None, body: str = "",
			terminal: bool = False, json_body: bool = False):
		super().__init__(*args)
		self.status_code = status_code
		self.body = body
		self.terminal = terminal
		self.json_body = json_body


class GunBrokerConfigError(GunBrokerError):
	"""Settings missing, incomplete, or switched off."""

	def __init__(self, *args, **kwargs):
		kwargs.setdefault("terminal", True)
		super().__init__(*args, **kwargs)


# ---------------------------------------------------------------------------
# Response-shape helpers (module level: pure, and the payload builder wants them)
# ---------------------------------------------------------------------------

def get_field(src: Any, *keys: str, default: Any = None) -> Any:
	"""Read the first present key out of a GunBroker response object.

	GunBroker is inconsistent about capitalisation between what it accepts and
	what it returns (``FixedPrice`` in, ``fixedPrice`` out), so callers pass
	every spelling they might meet.

	PRESENCE, not truthiness. ``src.get(a) or src.get(b)`` looks equivalent and
	is not: ``quantity`` of 0 and ``isActive`` of False are the two values whose
	whole point is being false, and an ``or`` chain skips straight past them to
	the default. Reading "sold out" as "not stated" is a wrong answer in the
	expensive direction.
	"""
	if not isinstance(src, dict):
		return default
	for key in keys:
		if key in src:
			return src[key]
	lowered = {str(k).lower(): v for k, v in src.items()}
	for key in keys:
		if str(key).lower() in lowered:
			return lowered[str(key).lower()]
	return default


def kv_key(value: Any, default: Any = None) -> Any:
	"""The CODE out of a GunBroker enum field.

	Enums read back as a one-entry object — ``{"3": "Used"}``, ``{"8":
	"Ground"}``, ``{"1": "AS IS - No refund or exchange"}`` — not as the integer
	that was sent. Both shapes are accepted here so a caller never has to know
	which end of the round trip it is holding.
	"""
	if isinstance(value, dict):
		for k in value:
			return k
		return default
	if value is None:
		return default
	return value


def kv_value(value: Any, default: str = "") -> str:
	"""The LABEL out of a GunBroker enum field (see :func:`kv_key`)."""
	if isinstance(value, dict):
		for v in value.values():
			return str(v)
		return default
	if value is None:
		return default
	return str(value)


def parse_gb_datetime(value: Any) -> datetime | None:
	"""One GunBroker timestamp as an aware UTC datetime, or None.

	A trailing ``Z`` (or an explicit offset) is honoured. Anything WITHOUT one is
	Eastern time — GunBroker's own clock — never UTC. A naive-is-UTC reading was
	the assumption in the reference implementation this client borrows from, and
	it is four hours wrong for half the year and five for the other half.
	"""
	if isinstance(value, datetime):
		dt = value
	else:
		text = str(value or "").strip()
		if not text:
			return None
		try:
			dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
		except ValueError:
			return None
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=GB_LOCAL_TZ)
	return dt.astimezone(timezone.utc)


def gb_datetime(src: dict, key: str) -> datetime | None:
	"""A timestamp off a GunBroker object, preferring its explicit UTC twin.

	Many objects carry both ``endingDateTime`` and ``endingDateTimeUTC``. When
	the UTC one is there it is unambiguous, so it wins; otherwise the plain one
	is parsed as Eastern per :func:`parse_gb_datetime`.
	"""
	explicit = get_field(src, f"{key}UTC", f"{key}Utc")
	if explicit:
		return parse_gb_datetime(explicit)
	return parse_gb_datetime(get_field(src, key))


def utc_stamp(value: Any) -> str:
	"""A timestamp in the exact shape GunBroker's incremental filters demand.

	``OrdersModifiedSinceDate`` is validated strictly: ISO 8601, UTC, trailing
	``Z``. A local-time string, a space instead of ``T``, or an offset spelled
	``+00:00`` all come back as a 400 that says so. Frappe hands us naive
	datetimes in site time, so a naive value is read as site time and converted
	rather than relabelled — relabelling would shift the watermark by the site's
	whole UTC offset and silently skip (or re-fetch) hours of orders.
	"""
	if value in (None, ""):
		return ""
	if isinstance(value, datetime):
		dt = value
	else:
		try:
			dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
		except ValueError:
			return ""
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=_site_tz())
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _site_tz():
	"""The site's own timezone, falling back to UTC when it cannot be resolved.

	Through the app's existing resolver rather than a fourth reading of System
	Settings — it already handles the two ways this can be answered and never
	raises. The fallback is UTC on purpose: guessing Eastern (GunBroker's clock)
	would be wrong for a site in Texas, and using the machine's local zone would
	make the watermark depend on which container ran the job.
	"""
	from ffl_integrations.distributor.util import site_timezone, zone_info

	return zone_info(site_timezone()) or timezone.utc


def parse_item_id(body: Any) -> str:
	"""The new item number out of a ``POST /Items`` success response, or "".

	There is no ``itemID`` key. The number appears twice, both times inside
	prose: ``userMessage`` reads "Item listed with itemID: 16087230", and
	``links`` carries a self link whose href ends in the same digits. Both are
	tried, because guessing wrong here does not fail loudly — it leaves a real
	listing on GunBroker that this POS does not know about, and the next click
	creates a second one.
	"""
	if not isinstance(body, dict):
		return ""
	message = str(get_field(body, "userMessage", "developerMessage") or "")
	match = _ITEM_ID_RE.search(message)
	if match:
		return match.group(1)
	for link in (get_field(body, "links") or []):
		if not isinstance(link, dict):
			continue
		if str(get_field(link, "rel") or "").lower() != "self":
			continue
		href = str(get_field(link, "href") or "")
		tail = re.search(r"(\d+)\s*/?\s*$", href)
		if tail:
			return tail.group(1)
		title = str(get_field(link, "title") or "").strip()
		if title.isdigit():
			return title
	return ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# GunBroker's required User-Agent (api@gunbroker.com, 2026-04-01):
#
#     Software/Seller/Versionnumber/ApplicationName
#     e.g. FlxPoint/SellsALot/1.0/ItemSync
#
# Production enforces it by matching the Seller segment against the name the
# DevKey was registered under — a case-sensitive substring — and answers
# everything else, the login included, with "403 Unapproved User Parameter …
# Please register at …/DevKey/Create". That sentence is about this header and
# says nothing about the key: on 2026-08-29 it cost an evening chasing IP
# allow-lists and access tokens on a key that was fine. The sandbox never
# looks at the header, which is why nothing written against it noticed.
#
# The Seller segment is therefore NOT necessarily this store's trading name: it
# is the name the DevKey knows, and it changes with the key. So it lives beside
# the key in site config (``gunbroker_seller_name``), not on the Settings form —
# a store has nothing to choose there, only a way to break the connection.
# There is NO fallback to the Company's name for production: a guess is refused
# by the marketplace with the misleading DevKey 403 above, so a blank value is a
# configuration error that names the key. The sandbox never looks at the
# header, so there a neutral placeholder stands in for a blank value.
USER_AGENT_TEMPLATE = "GunStorePOS/{seller}/1.0/POS"
SANDBOX_SELLER = "Sandbox"


def build_user_agent(seller_name: str) -> str:
	# "/" separates the segments, so one inside the name would shift them all.
	return USER_AGENT_TEMPLATE.format(seller=(seller_name or "").replace("/", " ").strip())


SELLER_NAME_CONF = "gunbroker_seller_name"


def registered_seller_name(sandbox: bool) -> str:
	"""The Seller segment: the configured name; blank is an error outside the sandbox."""
	name = str(frappe.conf.get(SELLER_NAME_CONF) or "").strip()
	if name:
		return name
	if sandbox:
		return SANDBOX_SELLER
	raise GunBrokerConfigError(
		f"Set site config `{SELLER_NAME_CONF}` to the name your DevKey is registered under.")


@dataclass(repr=False)
class GunBrokerConfig(RedactedRepr):
	username: str
	password: str
	dev_key: str
	base_url: str
	sandbox: bool
	# Seconds, or a ``(connect, read)`` pair. `requests` applies a single value as
	# the ceiling for EACH phase, so `timeout=3` can cost close to six seconds —
	# fine for a background job, not fine for a caller working to a total budget.
	# The counter's pre-sale check passes a pair so the budget it promises is the
	# budget it keeps.
	timeout: int | float | tuple[float, float]
	# The User-Agent Seller segment; see build_user_agent. from_settings always
	# fills it; the default only keeps bare configs (tests, scripts) constructible.
	seller_name: str = ""

	@property
	def user_agent(self) -> str:
		return build_user_agent(self.seller_name)

	@classmethod
	def from_settings(cls) -> "GunBrokerConfig":
		# Switched off means switched off, on every path — buttons, cron jobs,
		# the counter probe, anything added later. Checked BEFORE any credential
		# is read, because credentials routinely outlive the decision to stop
		# using a vendor and a leftover DevKey must not keep this store listing
		# guns on a marketplace it has walked away from.
		from ffl_integrations.integration_gate import gunbroker_enabled

		if not gunbroker_enabled():
			raise GunBrokerConfigError(
				"GunBroker integration is switched off (GunBroker Settings → Enable "
				"GunBroker Integration).")

		settings = frappe.get_single(SETTINGS_DOCTYPE)
		sandbox = sandbox_mode()

		key = dev_key(sandbox)
		if not key:
			raise GunBrokerConfigError(
				f"The {'sandbox' if sandbox else 'production'} GunBroker DevKey is not "
				f"configured (site config `{DEV_KEY_CONF[sandbox]}`). It is not "
				f"substituted with the other environment's key.")

		username = (settings.username or "").strip()
		password = settings.get_password("password", raise_exception=False) or ""
		if not username or not password:
			raise GunBrokerConfigError(
				"GunBroker Settings: seller username and password are required.")

		base_url = (settings.base_url_override or "").strip().rstrip("/")
		if not base_url:
			base_url = SANDBOX_BASE if sandbox else PROD_BASE

		return cls(
			username=username,
			password=password,
			dev_key=key,
			base_url=base_url,
			sandbox=sandbox,
			timeout=int(settings.request_timeout_seconds or 30),
			seller_name=registered_seller_name(sandbox and base_url == SANDBOX_BASE),
		)


# The DevKey identifies the software, not the store: GunBroker issues it to
# whoever runs this POS and matches the User-Agent against the name it was
# registered under. So it lives in site config (``bench set-config``), never on
# the Settings form — a store's operators have no business seeing a platform
# credential, and a second tenant on the same platform must not be able to
# read it off a page either.
DEV_KEY_CONF = {False: "gunbroker_dev_key", True: "gunbroker_sandbox_dev_key"}


def dev_key(sandbox: bool | None = None) -> str:
	"""The platform DevKey for one environment, or "" when it is not configured.

	Each environment reads its own key and nothing falls back to the other:
	that would be a way for a half-configured sandbox to start talking to
	production, which is the one direction this must never fail in.
	"""
	if sandbox is None:
		sandbox = sandbox_mode()
	return str(frappe.conf.get(DEV_KEY_CONF[bool(sandbox)]) or "").strip()


def sandbox_mode() -> bool:
	"""Is this site pointed at the GunBroker sandbox?

	``single_flag`` with ``default=1``, deliberately, NOT
	``bool(int(settings.sandbox_mode or 0))``. A Single keeps one row per field
	that has ever been saved, and a plain read of a missing row yields 0 — which
	for this particular field means a site that has never opened the settings
	form silently selects the LIVE marketplace. The default has to survive the
	absence of a stored value, so the read has to be the one that knows the
	difference between "unchecked" and "never set".
	"""
	from ffl_core.utils import single_flag

	return single_flag(SETTINGS_DOCTYPE, "sandbox_mode", default=1)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GunBrokerClient:
	def __init__(self, config: GunBrokerConfig, session: requests.Session | None = None,
			*, retry: bool = True) -> None:
		self.config = config
		self.session = session or requests.Session()
		# Retries are hand-rolled below rather than mounted on the adapter,
		# because the rule is per-CALL and not per-status: POST /Items has to be
		# excluded outright, and urllib3's Retry cannot express "this one method
		# on this one path". A bare Session retries nothing on its own, which is
		# the right floor to build on.
		self._retry = retry

	@classmethod
	def from_settings(cls) -> "GunBrokerClient":
		return cls(GunBrokerConfig.from_settings())

	@classmethod
	def from_settings_fast(cls, *, timeout: int | float | tuple[float, float]) -> "GunBrokerClient":
		"""A client for callers something is waiting on: short timeout, no retry.

		The counter's pre-sale check runs while a customer stands at the register,
		and the retry ladder below is worth up to three attempts with back-off.
		That budget is right for a background sweep and wrong at the till.

		Callers must treat "no answer" as a normal outcome — failing fast only
		helps if the caller degrades instead of blocking.

		``timeout`` may be a ``(connect, read)`` pair; see GunBrokerConfig.
		"""
		return cls(replace(GunBrokerConfig.from_settings(), timeout=timeout), retry=False)

	# -------------------------------------------------- token

	@property
	def _token_cache_key(self) -> str:
		return f"gunbroker:access_token:{'sandbox' if self.config.sandbox else 'prod'}"

	def cached_token(self) -> str:
		"""The access token currently in the shared cache, or "" — no login."""
		try:
			return frappe.cache().get_value(self._token_cache_key) or ""
		except Exception:  # noqa: BLE001 — no cache is "no token", never a crash
			return ""

	def _store_token(self, token: str) -> None:
		"""Write (or renew) the token's cache entry.

		Called on every successful authenticated call, not only after a login.
		The counter probe refuses to make an HTTP call unless a token is already
		cached — that is what keeps it inside its budget — so if the entry were
		only refreshed at login it would expire during any quiet period and the
		counter's check would degrade to permanently doing nothing, silently.
		Renewing on use keeps a busy store's token alive for as long as it is
		being used.
		"""
		try:
			frappe.cache().set_value(
				self._token_cache_key, token, expires_in_sec=TOKEN_TTL_SECONDS)
		except Exception:  # noqa: BLE001 — caching is an optimisation, not a contract
			pass

	def _clear_token(self) -> None:
		try:
			frappe.cache().delete_value(self._token_cache_key)
		except Exception:  # noqa: BLE001
			pass

	def login(self) -> str:
		"""Exchange username/password for an access token and cache it.

		Deliberately NOT wrapped in the 401-replay logic: a 401 here means the
		credentials are wrong, and retrying a wrong password is how an account
		gets locked.

		No part of the failure path may quote the request — the password is in
		that body.
		"""
		body = self._request(
			"POST", "/Users/AccessToken",
			json_body={"Username": self.config.username,
				"Password": Secret(self.config.password)},
			authenticated=False, retry=self._retry)
		token = str(get_field(body or {}, "accessToken", "AccessToken") or "").strip()
		if not token:
			raise GunBrokerError(
				"GunBroker login succeeded but returned no accessToken.",
				terminal=True)
		self._store_token(token)
		return token

	def logout(self) -> None:
		"""Best-effort token revocation. Never raises — a token we have thrown
		away is harmless whether or not GunBroker agreed to forget it."""
		token = self.cached_token()
		if not token:
			return
		try:
			self._request("DELETE", "/Users/AccessToken", retry=False)
		except GunBrokerError:
			pass
		finally:
			self._clear_token()

	def _token(self) -> str:
		return self.cached_token() or self.login()

	# -------------------------------------------------- HTTP plumbing

	def _url(self, path: str) -> str:
		return urljoin(self.config.base_url + "/", path.lstrip("/"))

	def _headers(self, authenticated: bool) -> dict[str, str]:
		# Secret values: these headers sit in requests' own frames on any
		# failure and the Error Log prints them (ffl_core.credentials).
		headers = {
			"X-DevKey": Secret(self.config.dev_key),
			"Accept": "application/json",
			"User-Agent": self.config.user_agent,
		}
		if authenticated:
			headers["X-AccessToken"] = Secret(self._token())
		return headers

	def _request(self, method: str, path: str, *, params: dict | None = None,
			json_body: Any = None, files: list | None = None,
			authenticated: bool = True, retry: bool = True) -> Any:
		"""One API call, with the retry ladder and a single 401 replay.

		``retry`` is False for calls that must never be repeated automatically.
		The only such call today is ``POST /Items``: GunBroker enforces no
		uniqueness whatsoever on SKU or serial number (ten listings for the same
		gun coexisted happily in testing), so a retried create after an ambiguous
		failure produces a second real listing for a gun that exists once. A
		duplicate listing has to be cleaned up by hand on GunBroker; a failed
		create is a button press. The caller reconciles instead, by scanning for
		the SKU.

		"""
		retry = retry and self._retry
		attempts = _MAX_ATTEMPTS if retry else 1
		# `files` is held as a list of (name, (filename, bytes, content_type))
		# tuples rather than open file objects precisely so a replay is possible: a
		# stream is consumed by the first attempt and the retry would upload zero
		# bytes without saying so.
		replayed_401 = False
		last_exc: GunBrokerError | None = None

		attempt = 0
		while attempt < attempts:
			try:
				response = self.session.request(
					method, self._url(path),
					params=params, json=json_body, files=files,
					headers=self._headers(authenticated),
					timeout=self.config.timeout,
				)
			except requests.RequestException as exc:
				# No status at all. Do NOT let a caller read the previous call's
				# status here — a transport failure is not a 404.
				last_exc = GunBrokerError(
					f"{method} {path}: {type(exc).__name__}", status_code=None)
				if attempt + 1 < attempts:
					self._sleep_backoff(attempt)
					attempt += 1
					continue
				raise last_exc from exc

			status = response.status_code

			if status == 401 and authenticated and not replayed_401:
				# The token expired or was revoked. One clean re-login and one
				# replay; a second 401 is a credential problem, not a stale token.
				replayed_401 = True
				self._clear_token()
				self.login()
				continue

			if status == 429:
				if attempt + 1 < attempts:
					self._sleep_rate_limit(response, attempt)
					attempt += 1
					continue

			if status >= 500:
				if attempt + 1 < attempts:
					self._sleep_backoff(attempt)
					attempt += 1
					continue

			result = self._interpret(method, path, response)
			if authenticated:
				# Renew on every successful authenticated call — see _store_token.
				token = self.cached_token()
				if token:
					self._store_token(token)
			return result

		raise last_exc or GunBrokerError(f"{method} {path}: exhausted retries")

	def _sleep_backoff(self, attempt: int) -> None:
		# Jittered, so several workers that hit the same outage do not come back
		# in lockstep.
		time.sleep(2 ** attempt + random.random())

	def _sleep_rate_limit(self, response: requests.Response, attempt: int) -> None:
		"""Honour Retry-After when offered, otherwise back off linearly.

		GunBroker sends no rate-limit headers at all — not Retry-After, not a
		quota header, nothing — so in practice this is defensive: the header is
		read in case they ever start sending one.
		"""
		raw = (response.headers.get("Retry-After") or "").strip()
		try:
			delay = float(raw)
		except (TypeError, ValueError):
			delay = 5.0 * (attempt + 1)
		time.sleep(max(delay, 0.0))

	def _interpret(self, method: str, path: str, response: requests.Response) -> Any:
		"""Turn a response into a value or a typed error. Response-shape triage
		lives here so every caller inherits it (see the module docstring)."""
		status = response.status_code
		content_type = (response.headers.get("content-type") or "").lower()
		looks_json = "json" in content_type
		text = response.text or ""

		if status < 400:
			if not response.content:
				return None
			try:
				# Not always an object: POST /Pictures answers with a bare JSON
				# string. Returned as-is rather than coerced into a dict, because
				# a caller that then reads .get() on it should fail loudly here
				# rather than quietly treat every key as missing.
				return response.json()
			except ValueError:
				return text

		body = text[:500]

		if status == 404 and not looks_json:
			# An HTML 404 is Cloudflare or IIS telling us the URL does not exist.
			# That is a bug in this code, not news about a listing, and it must
			# never reach the sweep as "GunBroker no longer has this item".
			raise GunBrokerError(
				f"{method} {path} → 404 with a non-JSON body: this route does not "
				f"exist on the GunBroker API.",
				status_code=404, body=body, terminal=True, json_body=False)

		# 4xx other than 401/429 is a validation or policy rejection: GunBroker
		# has considered the request and said no. Retrying sends the identical
		# request and gets the identical no.
		terminal = 400 <= status < 500 and status not in (401, 429)
		raise GunBrokerError(
			f"{method} {path} → {status}: {body}",
			status_code=status, body=body, terminal=terminal, json_body=looks_json)

	# -------------------------------------------------- account / reference

	def server_time(self) -> dict:
		"""``GET /GunBrokerTime`` — the cheapest proof the DevKey works (no login)."""
		return self._request("GET", "/GunBrokerTime", authenticated=False) or {}

	def get_account_info(self) -> dict:
		"""``GET /Users/AccountInfo`` — echoes the seller account back."""
		return self._request("GET", "/Users/AccountInfo") or {}

	def get_listing_defaults(self) -> dict:
		"""``GET /Listing/Defaults`` — the account's own listing defaults.

		Read by Test Connection for ``gatewayEnabled``: a listing can offer card
		payment all it likes, but without a payment gateway connected to the
		GunBroker account the buyer still cannot use Immediate Checkout.
		"""
		return self._request("GET", "/Listing/Defaults") or {}

	def get_categories(self, parent_id: int | None = None) -> dict:
		"""``GET /Categories`` — one level of the category tree.

		Only a leaf category (``canContainItems`` true) can hold a listing.
		"""
		# PageSize: the default page is 25, which would cut a long level short.
		params: dict[str, Any] = {"ShowOnlyFirstLevelSubCategories": "true", "PageSize": 300}
		if parent_id is not None:
			params["ParentCategoryID"] = int(parent_id)
		return self._request("GET", "/Categories", params=params) or {}

	def get_category(self, category_id: int) -> dict:
		"""``GET /Categories/{categoryID}`` — one category, with its ``categoryPath``
		from the root (each level a ``{"id", "name", "child"}``)."""
		return self._request("GET", f"/Categories/{int(category_id)}") or {}

	# -------------------------------------------------- items

	def create_listing(self, payload: dict, pictures: list | None = None) -> Any:
		"""``POST /Items`` — publish one listing. NEVER retried automatically.

		Multipart: a ``data`` part carrying the JSON payload, plus up to EIGHT
		``picture`` parts. The ninth and beyond are dropped without a word — no
		error, no warning, just fewer photos than were sent — so the caller sends
		at most eight here and appends the rest through ``add_pictures``.

		Args:
		    payload: the listing JSON (see gunbroker.mapping.build_listing_payload).
		    pictures: up to 8 ``(filename, bytes, content_type)`` tuples.

		Returns:
		    The raw response body. The new item number is NOT a field on it —
		    use :func:`parse_item_id`.
		"""
		files: list = [("data", (None, frappe.as_json(payload), "application/json"))]
		for filename, content, content_type in (pictures or [])[:8]:
			files.append(("picture", (filename, content, content_type)))
		return self._request("POST", "/Items", files=files, retry=False)

	def update_listing(self, item_id: str, patch: dict) -> Any:
		"""``PUT /Items/{itemID}`` — send only the keys being changed.

		``FixedPrice``, ``Title`` and ``Quantity`` are the fields observed to take
		effect (the last confirmed in the sandbox on 2026-08-28: a listing of four
		read back as three after one call). Others answer 200 and change nothing,
		so a caller that needs the change to have happened must re-read the item
		and check (see :meth:`get_item`).
		"""
		return self._request("PUT", f"/Items/{item_id}", json_body=patch)

	def delete_listing(self, item_id: str) -> Any:
		"""``DELETE /Items/{itemID}`` — end one listing early.

		A 200 here is necessary but not sufficient: the caller confirms by
		re-reading the item and checking ``isActive`` is false. A 403 means this
		DevKey's authorisation for the operation has been withdrawn.
		"""
		return self._request("DELETE", f"/Items/{item_id}")

	def delete_listings(self, item_ids: list[str]) -> Any:
		"""``DELETE /Items/?itemIds=a,b`` — end several listings in one call.

		ONLY the ids echoed back under ``successfullyEndedItems`` may be treated
		as ended. The shape of the failure half of that response has never been
		observed, so nothing may parse it speculatively — anything not listed as
		succeeded goes back through :meth:`delete_listing` one at a time.

		The batch ceiling the callers use (50) is an assumption, not a measured
		limit: the only batch ever sent held two ids.
		"""
		return self._request("DELETE", "/Items/",
			params={"itemIds": ",".join(str(i) for i in item_ids)})

	def get_item(self, item_id: str) -> dict:
		"""``GET /Items/{itemID}`` — full detail for one listing.

		Works on ENDED listings too, for a while: immediately after a delete the
		item still reads back 200 with ``isActive`` false and its ``sku``
		unchanged. That is what makes "did the end actually happen?" answerable.
		"""
		return self._request("GET", f"/Items/{item_id}") or {}

	def item_probe(self, item_id: str) -> dict:
		"""Three-way answer about one listing: ``ok`` / ``gone`` / ``error``.

		Returns:
		    ``{"state": "ok", "item": {...}}`` — GunBroker answered with the item.
		    ``{"state": "gone", "item": None}`` — GunBroker answered, in JSON,
		        that there is no such item.
		    ``{"state": "error", "item": None}`` — no usable answer: timeout,
		        rate limit, 5xx, revoked authorisation, or a 404 that was an HTML
		        page rather than JSON.

		The split between ``gone`` and ``error`` is the entire point. Callers act
		on ``gone``; treating a network blip or a bad URL as ``gone`` is how a
		gun that is still live on GunBroker gets released back to the shelf and
		sold a second time. ``gone`` is granted only for a JSON 404 — nothing
		else, however 404-shaped it looks.
		"""
		try:
			return {"state": "ok", "item": self.get_item(item_id)}
		except GunBrokerError as exc:
			if exc.status_code == 404 and exc.json_body:
				return {"state": "gone", "item": None}
			return {"state": "error", "item": None}

	# -------------------------------------------------- pictures

	def add_pictures(self, item_id: str, pictures: list) -> Any:
		"""``POST /Pictures/{itemID}`` — append photos to an existing listing.

		At most EIGHT per call. A ninth does not truncate the way ``POST /Items``
		does — the whole batch is rejected with 403 and nothing is stored — so
		the caller batches.

		NEVER retried automatically, for the same reason as
		:meth:`create_listing`: this appends, so a 5xx returned after the photos
		were already stored would, on retry, store them a second time and leave
		the listing showing duplicates. Nothing about that is visible from here.

		The success body is a bare JSON string, not an object.
		"""
		files = [("picture", (filename, content, content_type))
			for filename, content, content_type in pictures]
		return self._request("POST", f"/Pictures/{item_id}", files=files, retry=False)

	def list_pictures(self, item_id: str) -> dict:
		"""``GET /Pictures?ItemID=`` — what GunBroker currently holds."""
		return self._request("GET", "/Pictures", params={"ItemID": item_id}) or {}

	def delete_picture(self, picture_id: str) -> bool:
		"""``DELETE /Pictures/{pictureID}``. Idempotent HERE, not at GunBroker.

		A second delete answers 400 "not found". That is the desired end state
		reported as a failure, so it is absorbed: a caller cleaning up must not
		have to care whether a previous attempt already got there.
		"""
		try:
			self._request("DELETE", f"/Pictures/{picture_id}")
			return True
		except GunBrokerError as exc:
			if exc.status_code == 400 and "not found" in (exc.body or "").lower():
				return False
			raise

	# -------------------------------------------------- listing scans

	def list_items_selling(self, page_index: int = 1, page_size: int = 300) -> dict:
		"""``GET /ItemsSelling`` — one page of this account's live listings.

		There is no server-side filter worth having: ``?SKU=`` is rejected as an
		invalid parameter, and ``Keywords`` searches titles only — it does not
		look at sku or serial number. So a lookup means paging and comparing
		here.

		EVENTUALLY CONSISTENT. A listing ended seconds ago still appears in this
		result for minutes. Its presence is therefore not evidence that a
		listing is live, and must never be used to overturn a confirmed delete.
		"""
		return self._request("GET", "/ItemsSelling",
			params={"PageIndex": page_index, "PageSize": page_size}) or {}

	def list_items_sold(self, page_index: int = 1, page_size: int = 300) -> dict:
		"""``GET /ItemsSold`` — one page of recently sold listings.

		The other half of "where did this listing go": a listing bought moments
		ago has already left ``/ItemsSelling`` and is only here. Searching only
		the selling list yields a confident, wrong "it does not exist".

		``?SKU=`` returns 200 on this endpoint (unlike ``/ItemsSelling``), but
		nothing observed shows it actually filtering, so callers page and compare
		here as well rather than trust a parameter that may be being ignored.
		"""
		return self._request("GET", "/ItemsSold",
			params={"PageIndex": page_index, "PageSize": page_size}) or {}

	def iter_items_selling(self, *, page_size: int = 300, max_pages: int = 50) -> Iterator[dict]:
		"""Yield rows from ``/ItemsSelling`` across pages."""
		yield from self._iter_pages(self.list_items_selling, page_size, max_pages)

	def iter_items_sold(self, *, page_size: int = 300, max_pages: int = 50) -> Iterator[dict]:
		"""Yield rows from ``/ItemsSold`` across pages."""
		yield from self._iter_pages(self.list_items_sold, page_size, max_pages)

	@staticmethod
	def _iter_pages(fetch, page_size: int, max_pages: int) -> Iterator[dict]:
		# PageIndex is 1-based. Stop on an empty page, and cap the walk: an
		# account with a large catalogue must not be able to turn one recovery
		# scan into an unbounded crawl while somebody waits on a button.
		page = 1
		while page <= max_pages:
			body = fetch(page, page_size) or {}
			rows = get_field(body, "results", "Results") or []
			if not rows:
				return
			for row in rows:
				yield row
			if len(rows) < page_size:
				return
			page += 1

	# -------------------------------------------------- orders

	def list_orders_sold(self, modified_since: Any = None, page_index: int = 1,
			page_size: int = 300) -> dict:
		"""``GET /OrdersSold`` — one page of sold orders, incrementally.

		``modified_since`` becomes ``OrdersModifiedSinceDate`` and must be ISO
		8601 UTC ending in ``Z``; anything else is a 400 with a clear message.
		Omitting it asks for GunBroker's default window, which is not what the
		poll wants and is only useful for a one-off look.

		No ``Sort`` is sent. GunBroker documents twelve sort codes and none of
		them has been observed from this account, so sending a guess risks
		400-ing the entire poll; the caller orders by ``lastModifiedDate``
		itself, which is what the watermark actually depends on. (Unverified
		item — QA samples the codes against a real order.)
		"""
		params: dict[str, Any] = {"PageIndex": page_index, "PageSize": page_size}
		stamp = utc_stamp(modified_since)
		if stamp:
			params["OrdersModifiedSinceDate"] = stamp
		return self._request("GET", "/OrdersSold", params=params) or {}

	def iter_orders_sold(self, modified_since: Any = None, *, page_size: int = 300,
			max_pages: int = 50) -> Iterator[dict]:
		"""Yield ``/OrdersSold`` rows across pages, oldest change first."""
		def fetch(page, size):
			return self.list_orders_sold(modified_since, page, size)

		yield from self._iter_pages(fetch, page_size, max_pages)

	def get_order(self, order_id: str) -> dict:
		"""``GET /Orders/{orderID}`` — the full order, including its lines.

		Mandatory, not an optimisation: the ``/OrdersSold`` row does not carry
		the per-line ``serialNumber`` / ``sku``, so the only place a line can be
		matched back to a gun in stock is ``orderItemsCollection`` here.
		"""
		return self._request("GET", f"/Orders/{order_id}") or {}

	# The flags GunBroker will accept. The four destructive ones
	# (CancelOrder / ForceComplete / RefundOrder / ReturnOrder) are deliberately
	# NOT here: a refund has to be raised on GunBroker so their tax statement and
	# state filing are recomputed with it, and this client is not going to be the
	# thing that makes bypassing that a one-liner.
	_SETTABLE_FLAGS = ("AcknowledgeOrder", "PaymentReceived", "FFLReceived",
		"OrderShipped")

	def set_order_flags(self, order_id: str, **flags: bool) -> Any:
		"""``PUT /Orders/{orderID}/Flags`` — set order flags.

		Naturally idempotent: a flag already true is a no-op, and false does not
		clear one. So a retry is always safe.
		"""
		unknown = [k for k in flags if k not in self._SETTABLE_FLAGS]
		if unknown:
			raise GunBrokerError(
				f"refusing to send GunBroker order flag(s) {', '.join(sorted(unknown))}: "
				f"only {', '.join(self._SETTABLE_FLAGS)} may be set from POS",
				terminal=True)
		body = {key: bool(value) for key, value in flags.items()}
		if not body:
			raise GunBrokerError("set_order_flags called with no flags", terminal=True)
		return self._request("PUT", f"/Orders/{order_id}/Flags", json_body=body)

	def acknowledge_order(self, order_id: str) -> Any:
		"""Take the order out of GunBroker's "pending seller review" queue."""
		return self.set_order_flags(order_id, AcknowledgeOrder=True)

	def set_order_shipping(self, order_id: str, tracking_number: str,
			carrier: int | None = None) -> Any:
		"""``PUT /Orders/{orderID}/Shipping`` — upload the tracking number.

		This does NOT mark the order shipped; GunBroker support was explicit
		that the ``OrderShipped`` flag has to be sent separately, and first.

		``Carrier`` (1 FedEx / 2 UPS / 3 USPS) is optional and is what makes
		GunBroker render a working tracking link, so it is sent whenever the
		carrier maps. For anything else the key is OMITTED — not zero, not an
		empty string, and not a guess: an unmapped carrier still gets its
		tracking number uploaded, just without the link.

		A 403 here means the order is in a state GunBroker will not accept
		shipping for (a return, say), not that the credentials are wrong.
		"""
		tracking_number = (tracking_number or "").strip()
		if not tracking_number:
			raise GunBrokerError("set_order_shipping requires a tracking number",
				terminal=True)
		body: dict[str, Any] = {"TrackingNumber": tracking_number}
		if carrier:
			body["Carrier"] = int(carrier)
		return self._request("PUT", f"/Orders/{order_id}/Shipping", json_body=body)
