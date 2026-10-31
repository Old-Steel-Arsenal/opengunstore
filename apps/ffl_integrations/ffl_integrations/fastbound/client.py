"""FastBound Account API client.

Thin HTTP wrapper around `cloud.fastbound.com/{accountNum}/api/...`. Credentials
read from the FastBound Settings single doctype. Pagination is consumer-driven
(yields page after page); retries on 429/5xx with exponential back-off.

Spec: https://cloud.fastbound.com/swagger/v1-account/swagger.json (fetch it with
scripts/fetch_fastbound_spec.sh; the copy under docs/fastbound/ is git-ignored)

Usage:
    from ffl_integrations.fastbound.client import FastBoundClient
    client = FastBoundClient.from_settings()
    for page in client.iter_items(page_size=100):
        for item in page:
            ...
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator
from urllib.parse import quote, urljoin

import frappe
from frappe import _
from ffl_core.credentials import RedactedBasicAuth, RedactedRepr
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ffl_core.compliance.ffl_number import is_cr_license
from ffl_core.firearm import ATF_FIREARM_TYPES


SETTINGS_DOCTYPE = "FastBound Settings"

# Non-4473 FFL Disposition types we push to FastBound's bound book.
# Transfers carry a recipient FFL contact (from FFL Disposition.destination_ffl);
# destruction uses FastBound's "Destroyed" disposition kind (no recipient).
# Everything else (Retail Sale / Sale / Private Party Transfer / Donation / …) is
# a 4473 / to-individual disposition and is NEVER auto-pushed — those go through
# FastBound's 4473 UI flow.
TRANSFER_DISPOSITION_TYPES = frozenset({"Dealer Transfer", "FFL Transfer", "Return"})
DESTRUCTION_DISPOSITION_TYPES = frozenset({"Scrap", "Destroyed"})
PUSHABLE_DISPOSITION_TYPES = TRANSFER_DISPOSITION_TYPES | DESTRUCTION_DISPOSITION_TYPES

# FastBound's wording when an UNCOMMITTED disposition is sitting on the item:
# "Item 00843 has already been added to another disposition." Matched (lowercased,
# fragment only — the item number and punctuation vary) so that only this 400 is
# rewritten into the abandoned-4473 advice. DISP-2026-02000.
#
# Matched against FastBoundError.body — the WHOLE response — and not against the
# message, which carries only the first 500 characters: a body that puts this
# sentence after a long validation preamble would otherwise fall through to the
# raw 400 and take the operator's only pointer with it.
_HOLD_400_FRAGMENT = "another disposition"

# How much of a response body FastBoundError keeps. Long enough that the
# sentence above survives any validation preamble FastBound puts in front of it,
# short enough that the object can never become a dump of somebody's 4473.
_ERROR_BODY_KEPT = 4000

# Writable Item fields carried over on a read-merge-write PUT /Items/{id}.
# FastBound's PUT /Items REPLACES the whole record, so the current value of each
# writable field must be re-sent or FastBound wipes it (and drops the required
# ItemNumber / Acquire_Date / AcquisitionType → 400). Everything FastBound only
# ECHOES on a GET — id, status, disposed, disposition*/dispose_* , ttsn/otsn,
# deleted, isTheftLoss/isDestroyed, acquisitionContactId, create/updateUtc,
# quickCodeUrl, attachments, notes — is deliberately excluded so it never rides
# back into the PUT. Mirrors the fastbound-mcp update_item body (verified live).
_ITEM_WRITABLE_FIELDS = (
	"itemNumber", "serial", "manufacturer", "importer", "model", "caliber",
	"type", "countryOfManufacture", "barrelLength", "totalLength", "condition",
	"cost", "price", "mpn", "upc", "sku", "location", "doNotDispose",
	"externalId", "acquisitionType", "acquire_Date", "acquire_PurchaseOrderNumber",
	"acquire_InvoiceNumber", "acquire_ShipmentTrackingNumber",
)


class FastBoundError(RuntimeError):
	"""Raised on non-recoverable FastBound API errors. Carries the HTTP
	`status_code` (None for non-HTTP failures) so callers can detect a 404
	("already gone") on the status, not by substring-matching the message —
	an error body that merely contains "404" must not be misread as not-found."""

	def __init__(self, *args, status_code: int | None = None, terminal: bool = False,
			body: str = ""):
		super().__init__(*args)
		self.status_code = status_code
		# The response body, capped at _ERROR_BODY_KEPT, for the callers that have
		# to read what FastBound SAID (see _HOLD_400_FRAGMENT). The message carries
		# only the first 500 characters, so matching on str(exc) silently stops
		# working the day FastBound puts the sentence past that cut.
		#
		# DO NOT LOG THIS. An Item or Contact response body carries 4473 PII —
		# buyer name, date of birth, address — and an Error Log row is readable by
		# anyone with the desk role and survives in the database for a year. Log
		# str(exc) instead, which is the same text already truncated for exactly
		# that reason. The cap here bounds the damage; it is not permission.
		self.body = (body or "")[:_ERROR_BODY_KEPT]
		# True when the failure is PER-RECORD terminal — needs a human and will
		# never self-heal on retry (already disposed, can't auto-adopt). The push
		# tasks fast-fail on it and the daily reconcile skips it. NOT set for global
		# / config faults (Invalid Audit User, expired key) or transient errors,
		# which must STAY retryable so reconcile recovers them once the cause clears.
		self.terminal = terminal


class FastBoundConfigError(FastBoundError):
	"""Settings missing or incomplete."""


def _said_item_is_held(exc: FastBoundError) -> bool:
	"""Did FastBound say the item is already on another disposition?

	Reads the untruncated ``body`` and falls back to the message only for an
	error raised without one (a wrapped/re-raised error, or a test fixture), so
	the match never depends on where the 500-character cut lands.
	"""
	return _HOLD_400_FRAGMENT in (getattr(exc, "body", "") or str(exc)).lower()


# The FastBound API version this client is written against, sent as
# ``x-api-version``. A constant, not a setting: the request shapes below are
# tied to it, so it is not something to change from a form.
API_VERSION = "1.0"


@dataclass(repr=False)
class FastBoundConfig(RedactedRepr):
	account_num: str
	api_key: str
	base_url: str
	api_version: str
	audit_user: str
	timeout: int

	@classmethod
	def from_settings(cls) -> "FastBoundConfig":
		# Switched off — refuse to build a client AT ALL. The doc_event hooks and
		# scheduler jobs already return early (fastbound/tasks.py); this is the net
		# under every OTHER outbound path — the manual admin tools (importer,
		# inventory_sync, reconcile), ffl_core's serial correction, and anything
		# added later. Credentials routinely outlive the decision to stop using a
		# vendor, so a store that unchecks the box and leaves its API key in the form
		# must not keep writing to a bound book it no longer keeps.
		#
		# FastBoundConfigError rather than a bare throw, so the callers that already
		# degrade quietly on "not configured" treat this as the same class of fact:
		# unchecked and un-credentialed both mean "no FastBound here".
		from ffl_integrations.integration_gate import fastbound_enabled
		if not fastbound_enabled():
			raise FastBoundConfigError(
				"FastBound integration is switched off (FastBound Settings → Enable "
				"FastBound). A&D records are being kept locally only; "
				"re-enable it to sync with the bound book.")
		s = frappe.get_single(SETTINGS_DOCTYPE)
		if not s.account_num:
			raise FastBoundConfigError("FastBound Settings: Account Number is empty.")
		api_key = s.get_password("api_key", raise_exception=False) or ""
		if not api_key:
			raise FastBoundConfigError("FastBound Settings: API key missing.")
		if not s.base_url:
			# Fail CLOSED. This used to default to https://cloud.fastbound.com —
			# the live bound book — so a blank field silently re-pointed the
			# client at production. Everything else about the request still looked
			# normal: the account number and API key guards above had already
			# passed, so a dev site would have posted real acquisitions and
			# dispositions to the real ledger, with no error and nothing in the
			# log to distinguish it. A non-production target must be an explicit
			# statement, never the residue of a field somebody happened to fill
			# in; clearing it (a mis-click, a re-install, a restored backup, a
			# fixture reload) must stop the integration, not redirect it.
			raise FastBoundConfigError(
				"FastBound Settings: Base URL is empty. Set it in FastBound Settings "
				"→ Base URL: the production ledger URL on the production site, and "
				"the FastBound test account's URL on every dev/test site. It is "
				"deliberately not defaulted — a default would silently point a new "
				"site at the live bound book.")
		return cls(
			account_num=s.account_num,
			api_key=api_key,
			base_url=s.base_url.rstrip("/"),
			api_version=API_VERSION,
			audit_user=s.default_audit_user or "import@gunshop.local",
			timeout=int(s.request_timeout_seconds or 30),
		)


class FastBoundClient:
	def __init__(self, config: FastBoundConfig, session: requests.Session | None = None) -> None:
		self.config = config
		self.session = session or self._build_session()

	@classmethod
	def from_settings(cls) -> "FastBoundClient":
		return cls(FastBoundConfig.from_settings())

	@classmethod
	def from_settings_fast(cls, *, timeout: int) -> "FastBoundClient":
		"""A client for callers that must fail FAST: short timeout, no retry adapter.

		``from_settings()`` mounts ``Retry(total=5, backoff_factor=0.6)`` — roughly 18
		seconds against an unreachable host, on top of ``timeout`` per attempt. That
		budget is right for background sync and wrong anywhere something is waiting on
		the other end: a cashier standing at the counter, or a webhook handler holding
		row locks while FastBound decides whether to answer. A bare ``Session()`` means
		one attempt per request.

		Callers must still degrade on failure — failing fast only helps if the caller
		treats "no answer" as a normal outcome.
		"""
		return cls(
			replace(FastBoundConfig.from_settings(), timeout=timeout),
			session=requests.Session(),
		)

	# -------------------------------------------------- HTTP plumbing

	@staticmethod
	def _build_session() -> requests.Session:
		s = requests.Session()
		# Retry: 5xx + 429 with exponential back-off; idempotent methods only.
		# POST gets retried explicitly inside methods that are safe (with externalId
		# dedup or short-circuit on duplicate-detection error).
		retry = Retry(
			total=5,
			backoff_factor=0.6,
			status_forcelist=(429, 500, 502, 503, 504),
			allowed_methods=frozenset(["GET", "PUT", "DELETE", "HEAD", "OPTIONS"]),
		)
		adapter = HTTPAdapter(max_retries=retry)
		s.mount("https://", adapter)
		s.mount("http://", adapter)
		return s

	def _url(self, path: str) -> str:
		# path may be "/api/Items" or "Items" — normalise.
		path = path.lstrip("/")
		if not path.startswith("api/"):
			# the fully qualified path includes accountNum: /{accountNum}/api/...
			path = f"{self.config.account_num}/api/{path}"
		else:
			path = f"{self.config.account_num}/{path}"
		return urljoin(self.config.base_url + "/", path)

	def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
		h = {
			"X-AuditUser": self.config.audit_user,
			"x-api-version": self.config.api_version,
			"Accept": "application/json",
		}
		if extra:
			h.update(extra)
		return h

	def _send(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json_body: Any | None = None,
		accept: str | None = None,
	) -> requests.Response:
		"""One authenticated call; raises FastBoundError on ≥400, else returns the
		raw response (``_request`` parses JSON, ``download_4473_pdf`` takes bytes)."""
		# FastBound auth: HTTP Basic with the API key as username and an
		# empty password. There's no separate secret — the key alone
		# is the credential. (Their swagger doesn't formalise auth, but
		# that's the documented scheme on cloud.fastbound.com.)
		response = self.session.request(
			method,
			self._url(path),
			params=params,
			json=json_body,
			headers=self._headers({"Accept": accept} if accept else None),
			# Not a bare tuple: that sits raw in Session.request's `auth` local
			# on every failure and the Error Log prints it (ffl_core.credentials).
			auth=RedactedBasicAuth(self.config.api_key, ""),
			timeout=self.config.timeout,
		)
		if response.status_code >= 400:
			raise FastBoundError(
				f"{method} {path} → {response.status_code}: {response.text[:500]}",
				status_code=response.status_code,
				body=response.text,
			)
		return response

	def _request(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json_body: Any | None = None,
	) -> Any:
		response = self._send(method, path, params=params, json_body=json_body)
		# FastBound's create endpoints (POST /Acquisitions, POST /Dispositions/CreateAndCommit, ...)
		# return 201 Created with EMPTY body + Location header pointing at
		# the new resource. Surface the new id so callers don't have to
		# re-fetch by externalId.
		if not response.content:
			loc = response.headers.get("Location")  # requests CaseInsensitiveDict
			if loc and "/" in loc:
				return {"id": loc.rsplit("/", 1)[-1]}
			return None
		try:
			return response.json()
		except ValueError:
			raise FastBoundError(f"{method} {path}: non-JSON response: {response.text[:200]}")

	# -------------------------------------------------- public methods

	def get_account(self) -> dict:
		"""Sanity check: returns the account info. Cheap canary for credentials."""
		return self._request("GET", "Account")

	def get_smartlist(self, name: str) -> list[str]:
		"""Return a FastBound SmartList — the account's current values for a field
		(FastBound presets + any custom values the dealer added).

		GET /api/SmartLists/{name} → {"smartList": [...]}. Valid names include
		AcquireType, DisposeType, Caliber, Manufacturer, Importer, ItemType,
		Condition, Location, CountryOfManufacture, LicenseType.
		"""
		body = self._request("GET", f"SmartLists/{name}")
		values = (body or {}).get("smartList") or []
		# De-dupe preserving order; drop blanks.
		seen, out = set(), []
		for v in values:
			s = str(v or "").strip()
			if s and s not in seen:
				seen.add(s)
				out.append(s)
		return out

	def iter_items(self, *, page_size: int = 100, extra_params: dict | None = None) -> Iterator[list[dict]]:
		"""Yield pages of items from the bound book.

		`GET /api/Items` returns ``{items: [...], records: <total>}``. FastBound's
		``skip`` is a PAGE index, not a record offset (per the API docs and
		verified live: take=2 skip=1 returns items 3-4, not 2-3), so advance it by
		one page per request — incrementing by ``len(items)`` reads only page 0.
		"""
		page = 0
		while True:
			params = {"skip": page, "take": page_size}
			if extra_params:
				params.update(extra_params)
			body = self._request("GET", "Items", params=params)
			items = _coerce_items(body)
			if not items:
				return
			yield items
			# Stop only on an EMPTY page. A short non-empty page is NOT a reliable
			# end signal (FastBound can return a partial intermediate page), so
			# early-returning on it would silently truncate the bound book.
			page += 1

	def _resolve_existing_contact_id(self, acq) -> str | None:
		"""Find an existing FastBound contact for this acquisition's seller so the
		push references it (ContactId) instead of creating a duplicate — the fix
		for the 'This contact already exists' 400 on the 2nd+ gun from one seller.

		Resolution order: our stable externalId (fast, exact, for contacts we've
		pushed before), then the natural key — FFL number for dealers, first+last
		name for individuals (which also adopts a contact created outside our push,
		e.g. by a prior 4473 or directly in FastBound).
		"""
		ext = _contact_external_id(acq)
		if ext:
			found = self.find_contact_by_external_id(ext)
			cid = (found or {}).get("id") or (found or {}).get("contactId")
			if cid:
				return cid
		if getattr(acq, "source_type", None) == "FFL Dealer" and getattr(acq, "source_ffl", None):
			found = self.find_contact_by_ffl_number(acq.source_ffl)
			return (found or {}).get("id") or (found or {}).get("contactId")
		first, _middle, last = _individual_name(acq)
		if first or last:
			found = self.find_contact_by_name(first, last)
			return (found or {}).get("id") or (found or {}).get("contactId")
		return None

	def _adopt_existing_item(self, acq) -> dict:
		"""The firearm's serial is already in the FastBound book — link to the
		existing item instead of failing. Resolves the item (and its parent
		acquisition) LIVE from FastBound, so the ids written to our records are
		FastBound's own, never inferred. Returns the same shape as a fresh push
		so the caller stamps fastbound_id + Serial No.fastbound_item_id and clears
		the error. Fails loud if the item can't be found, or is already disposed
		(re-stocking a sold gun needs a human, not an auto-link)."""
		serial = (acq.serial or "").strip()
		item = self.find_item_by_serial(serial)
		if not item:
			raise FastBoundError(
				f"Acquisition {acq.name}: FastBound reports serial {serial!r} already "
				f"acquired, but no matching item was found to adopt.", terminal=True)
		if item.get("disposed") or item.get("deleted"):
			raise FastBoundError(
				f"Acquisition {acq.name}: serial {serial!r} is in FastBound but "
				f"DISPOSED/deleted — refusing to auto-adopt; resolve manually.", terminal=True)
		item_id = item.get("id") or item.get("itemId")
		acq_id = self.find_acquisition_id_by_item(item_id) or item_id
		# adopted=True: acq_id is a PRE-EXISTING acquisition (it can hold other
		# guns) — the atomic receive's compensation must never delete it.
		return {"acquisitionId": acq_id, "adopted": True,
			"items": [{"fbItemId": item_id, "serial": serial}]}

	def _free_closed_cycle_external_ids(self, body: dict) -> list[dict]:
		"""Rename the externalId of every closed-cycle FastBound item that holds
		an externalId this acquisition wants, freeing the id for the new cycle.

		The rename target is "<externalId>-<holder itemNumber>" — deterministic,
		unique (item numbers are), and it reads as provenance in the book
		("75166-01074" = the 75166 that left as item #01074). A DELETED holder
		counts as closed too — its id still trips FastBound's uniqueness check
		but no live gun depends on it. Returns the renames performed
		([{item_id, item_number, from, to}]); empty means the conflict isn't
		ours to fix (caller re-raises the original error).

		A holder that is still ACTIVE is a genuine duplicate acquisition —
		never rename the id out from under a live gun; raise terminal so the
		daily retry stops burning API calls on a conflict only a human can
		resolve.

		Contract note: this write is OUTSIDE the atomic-receive compensation
		(delete_acquisition) — if a later step fails and POS rolls back, the
		rename stays. Accepted: the holder is a closed-cycle record nothing
		resolves by externalId any more, FastBound's own audit trail records
		the change, and the caller leaves a Comment on the acquisition when
		the push lands.
		"""
		renames: list[dict] = []
		for it in (body.get("items") or []):
			ext = str(it.get("externalId") or "").strip()
			if not ext:
				continue
			holder = self.find_item_by_external_id(ext)
			if not holder:
				continue
			closed = holder.get("deleted") or holder.get("disposed") \
				or self._item_is_disposed(holder)
			if not closed:
				raise FastBoundError(
					f"External ID {ext!r} is held by ACTIVE FastBound item "
					f"#{holder.get('itemNumber')} — a duplicate acquisition, not a "
					f"returned gun. Resolve which record is real, then link with "
					f"ffl_integrations.fastbound.reconcile.reconcile_record_to_fastbound "
					f"or delete the duplicate; nothing was changed.", terminal=True)
			holder_id = holder.get("id") or holder.get("itemId")
			if not holder_id:
				continue  # can't address the holder — not self-healable here
			suffix = str(holder.get("itemNumber") or "").strip() or "prev"
			new_ext = f"{ext}-{suffix}"
			self.set_item_external_id(holder_id, new_ext)
			renames.append({"item_id": holder_id,
				"item_number": holder.get("itemNumber"), "from": ext, "to": new_ext})
		return renames

	def post_acquisition(self, acq) -> dict:
		"""Push one FFL Acquisition to FastBound as a complete record.

		Uses POST /api/Acquisitions/CreateAndCommit?listAcquiredItems=true.
		For FFL Dealer sources we resolve the contact id first (FastBound
		rejects duplicate fflNumbers; an existing FB contact from a prior
		import or another integration would otherwise 400 our create) and
		pass `ContactId` instead of inline contact.

		Returns ``{acquisitionId, items: [{fbItemId, serial}, ...]}`` so the
		caller can stamp Serial No records 1:1 with the FastBound items.
		"""
		body = self._acquisition_payload(acq)
		contact_id = self._resolve_existing_contact_id(acq)
		if contact_id:
			body.pop("contact", None)
			# FB rejects camelCase here — must be PascalCase ContactId.
			body["ContactId"] = contact_id
		try:
			response = self._request(
				"POST", "Acquisitions/CreateAndCommit",
				params={"listAcquiredItems": "true"},
				json_body=body,
			) or {}
		except FastBoundError as exc:
			msg = str(exc).lower()
			# Serial already in the FastBound book (entered directly in FastBound,
			# or a prior push that committed but lost the id) — adopt the existing
			# item instead of failing forever. Resolved live, so the ids are
			# FastBound's own, never inferred.
			if "already been acquired" in msg:
				return self._adopt_existing_item(acq)
			# The externalId (= the serial) is held by an EXISTING FastBound
			# item. On a returned gun (dealer return, consignment return) that
			# holder is the PREVIOUS ownership cycle's item — disposed, done,
			# but still owning the id — and this error used to strand every
			# re-acquisition in the daily retry pool (2026-08-21 JV-return
			# incident). Free the id by renaming the closed-cycle holder and
			# retry once; an ACTIVE holder is a genuine duplicate and raises
			# terminal inside the helper.
			elif "external id" in msg and "already used" in msg:
				renames = self._free_closed_cycle_external_ids(body)
				if not renames:
					raise  # nothing rename-able found — surface the original
				response = self._request(
					"POST", "Acquisitions/CreateAndCommit",
					params={"listAcquiredItems": "true"},
					json_body=body,
				) or {}
				# POS-side trace of the bound-book rename (FastBound records its
				# own audit line): persists with the acquisition on the normal
				# path; on the atomic-receive path it rolls back with everything
				# else while the rename stays — accepted, see the helper's
				# contract note.
				# Best-effort: the push has already committed in FastBound, and
				# a failed Comment insert must never read as a failed push —
				# a non-FastBoundError here would skip the callers' error
				# handling (and the atomic receive's compensation), leaving an
				# orphan bound-book acquisition.
				try:
					acq.add_comment("Comment",
						"Reacquisition of a returned gun: freed the previous cycle's "
						"FastBound externalId — " + "; ".join(
							f"item #{r['item_number']}: {r['from']} → {r['to']}"
							for r in renames))
				except Exception:
					frappe.log_error(
						title="FastBound externalId rename: audit comment failed",
						message=f"Acquisition {acq.name}: renames {renames!r}\n"
							f"{frappe.get_traceback()}")
			else:
				# A sibling push (same seller, same batch) can create the contact
				# between our resolve and this commit — FastBound then 400s
				# "This contact already exists". Re-resolve the now-existing contact
				# and retry once by ContactId. (No inline contact in body → the error
				# isn't about our create; re-raise.)
				if "contact already exists" not in msg or "contact" not in body:
					raise
				contact_id = self._resolve_existing_contact_id(acq)
				if not contact_id:
					raise
				body.pop("contact", None)
				body["ContactId"] = contact_id
				response = self._request(
					"POST", "Acquisitions/CreateAndCommit",
					params={"listAcquiredItems": "true"},
					json_body=body,
				) or {}
		fb_id = response.get("id") or response.get("acquisitionId")
		if not fb_id:
			raise FastBoundError(f"POST /Acquisitions/CreateAndCommit returned no id: {response!r}")
		items = [
			{"fbItemId": it.get("id"), "serial": (it.get("serial") or "").strip()}
			for it in (response.get("items") or [])
			if it.get("id")
		]
		return {"acquisitionId": fb_id, "items": items}

	def delete_acquisition(self, fastbound_id: str) -> None:
		"""DELETE /api/Acquisitions/{id}. 404 (already gone) is a no-op."""
		try:
			self._request("DELETE", f"Acquisitions/{fastbound_id}")
		except FastBoundError as exc:
			# 404 in the message means the record is already absent.
			if exc.status_code == 404:
				return
			raise

	def update_item(self, item_id: str, fields: dict, *, current: dict | None = None) -> dict:
		"""PUT /api/Items/{id} — edit an already-acquired firearm's attributes in
		place (FastBound audit-logs the edit). Used to push a post-acquisition
		correction such as a per-serial manufacturer / importer fix without
		deleting and re-creating the acquisition.

		PUT /Items is a WHOLE-RECORD REPLACE, not a merge: a body carrying only
		the firearm identity block drops the required ItemNumber / Acquire_Date /
		AcquisitionType and FastBound 400s. So this is a READ-MERGE-WRITE — GET
		the current item, overlay `fields` (a None value never clobbers an
		existing value), keep only the writable fields (`_ITEM_WRITABLE_FIELDS`;
		read-only / response-only keys FastBound echoes are dropped), and PUT the
		merged body. Raises FastBoundError (404, terminal) when the item no longer
		exists rather than silently writing a replacement record.
		"""
		# `current` may be pre-fetched by the caller (e.g. update_item_serial, which
		# already read the item to decide the externalId move) — skip a redundant GET.
		if current is None:
			current = self.get_item(item_id)
		if current is None:
			raise FastBoundError(
				f"update_item: FastBound item {item_id!r} not found (404) — cannot "
				f"edit a bound-book item that is absent.",
				status_code=404, terminal=True,
			)
		# Read-merge-write: preserve the current writable fields, then overlay the
		# caller's values. A None from the caller (e.g. _firearm_item_fields sends
		# upc/mpn/sku=None when POS has no value) must NOT wipe an existing FB value.
		body = {k: current[k] for k in _ITEM_WRITABLE_FIELDS if k in current}
		for key, value in fields.items():
			if value is not None:
				body[key] = value
		return self._request(
			"PUT", f"Items/{quote(item_id, safe='')}", json_body=body) or {}

	def update_item_serial(self, item_id: str, new_serial: str, *, note: str | None = None) -> dict:
		"""Correct a bound-book item's serial in place (PUT /Items/{id}) — the
		FastBound half of a POS serial-number correction (see
		ffl_core.api.serial_correction). Thin wrapper over update_item so the
		correction flow has ONE obvious call and the audit note rides along on the
		SAME request.

		In this integration the item's **externalId IS the serial** (set at
		acquisition push: `externalId = acq.serial`; get_item_id_by_serial /
		find_item_by_serial / reconcile resolve guns via GetByExternalId). So a
		serial correction must move BOTH `serial` and `externalId` to the new value,
		or those lookups desync (item.serial=new but externalId=old →
		GetByExternalId/new 404s → FastBound<->POS divergence, the exact failure this
		feature exists to prevent). externalId is moved ONLY when it currently
		mirrors the serial (the normal case) or is blank (a FastBound-direct item we
		adopted, now made resolvable) — a custom, non-serial externalId is never
		clobbered.

		`note` is deliberately NOT in `_ITEM_WRITABLE_FIELDS`, so the read-merge-
		write never re-sends a stale note; passing it here adds a fresh audit note
		for this edit. Every other writable field is preserved. Raises FastBoundError
		(terminal 404) when the item is absent — the caller, inside its DB savepoint,
		then rolls the ERP rename back so ERP and the bound book never diverge.
		"""
		new_serial = (new_serial or "").strip()
		# Read the item ONCE here (we need the current externalId/serial to decide
		# the externalId move) and hand it to update_item so it isn't fetched twice.
		current = self.get_item(item_id)
		if current is None:
			raise FastBoundError(
				f"update_item_serial: FastBound item {item_id!r} not found (404) — "
				f"cannot correct the serial of an absent bound-book item.",
				status_code=404, terminal=True,
			)
		fields: dict[str, Any] = {"serial": new_serial}
		current_ext = (current.get("externalId") or "").strip()
		current_serial = (current.get("serial") or "").strip()
		if not current_ext or current_ext == current_serial:
			fields["externalId"] = new_serial
		if note:
			fields["note"] = note[:1000]
		return self.update_item(item_id, fields, current=current)

	def get_item(self, item_id: str) -> dict | None:
		"""GET /api/Items/{id} — the single FastBound bound-book item, or None on
		404. Used by the per-gun reconcile to compare POS against the authoritative
		FastBound record."""
		item_id = (item_id or "").strip()
		if not item_id:
			return None
		try:
			return self._request("GET", f"Items/{quote(item_id, safe='')}") or None
		except FastBoundError as exc:
			if exc.status_code == 404:
				return None
			raise

	# -------------------------------------------------- Disposition (4473) flow

	def get_disposition(self, disposition_id: str) -> dict:
		"""GET /Dispositions/{id} — webhook fallback / reconciliation."""
		return self._request("GET", f"Dispositions/{disposition_id}") or {}

	def download_4473_pdf(self, form4473_id: str) -> bytes:
		"""GET /Form4473s/Download/{id} — the signed 4473 as raw PDF bytes.

		The body is a PDF, not JSON, so this takes ``_send``'s bytes rather than
		``_request``'s parse. X-AuditUser (from _headers) is REQUIRED here —
		without it FastBound answers 400 "Invalid Audit User" before it even
		looks the id up (verified 2026-09-24). The bytes are the buyer's 4473:
		never log them.
		"""
		return self._send("GET", f"Form4473s/Download/{quote(form4473_id, safe='')}",
			accept="application/octet-stream").content

	# -------------------------------------------------- Disposition (non-4473) push

	def find_item_by_serial(self, serial: str) -> dict | None:
		"""Find the in-stock FastBound bound-book item for a serial. Returns the
		full item dict, None if none match, and RAISES on more than one in-stock
		match.

		Unlike `get_item_id_by_serial` (GetByExternalId — only finds items WE
		pushed, whose externalId IS the serial), this also finds items entered
		directly in FastBound (externalId null): `GET /Items?serial=` then verify
		client-side (FastBound may ignore the filter, as it does `?fflNumber=`).
		A serial can recur (old disposed unit + current one), so prefer the
		in-stock (non-disposed) item — that's the one to adopt.
		"""
		needle = str(serial or "").strip().casefold()
		if not needle:
			return None
		take, page, seen = 100, 0, 0
		matches: dict[str, dict] = {}
		while seen <= 5000:
			try:
				res = self._request("GET", "Items",
					params={"serial": serial, "take": take, "skip": page}) or {}
			except FastBoundError as exc:
				if exc.status_code == 404:
					break
				raise
			items = res.get("items") or res.get("data") or res.get("results") or []
			if not items:
				break
			for it in items:
				if str(it.get("serial") or "").strip().casefold() != needle:
					continue
				if it.get("deleted"):
					continue
				iid = it.get("id") or it.get("itemId")
				if iid:
					matches[iid] = it
			if len(items) < take:
				break
			page += 1
			seen += take
		else:
			raise FastBoundError(
				f"Item search for serial {serial!r} scanned {seen} items without "
				f"exhausting the list — aborting to avoid a wrong match.")
		in_stock = {k: v for k, v in matches.items() if not v.get("disposed")}
		pool = in_stock or matches
		if len(pool) > 1:
			raise FastBoundError(
				f"{len(pool)} in-stock FastBound items match serial {serial!r} — "
				f"refusing to guess which to adopt.")
		return next(iter(pool.values())) if pool else None

	def find_acquisition_id_by_item(self, item_id: str) -> str | None:
		"""The FastBound acquisition id that owns a given item id, via
		`GET /Acquisitions?itemId=`. None if not found (caller falls back)."""
		if not item_id:
			return None
		try:
			res = self._request("GET", "Acquisitions",
				params={"itemId": item_id, "take": 1, "skip": 0}) or {}
		except FastBoundError as exc:
			if exc.status_code == 404:
				return None
			raise
		acqs = _coerce_items(res) or res.get("acquisitions") or []
		for a in acqs:
			aid = a.get("id") or a.get("acquisitionId")
			if aid:
				return aid
		return None

	def get_item_id_by_serial(self, serial: str) -> str | None:
		"""Resolve a FastBound item Id from its externalId (the firearm serial set
		at acquisition push). Returns None if the serial isn't in the bound book."""
		item = self.find_item_by_external_id(serial) or {}
		return item.get("id") or item.get("itemId")

	def find_item_by_external_id(self, external_id: str) -> dict | None:
		"""The full FastBound item holding `external_id`, or None (404)."""
		external_id = (external_id or "").strip()
		if not external_id:
			return None
		try:
			# Encode the id — real firearm serials legitimately contain
			# '/', '#', '&', or spaces (e.g. 'CW/0142', 'KE-22 USA') which
			# would otherwise be parsed as extra path segments or query
			# delimiters and 404 against the wrong endpoint.
			return self._request(
				"GET", f"Items/GetByExternalId/{quote(external_id, safe='')}",
			) or None
		except FastBoundError as exc:
			if exc.status_code == 404:
				return None
			raise

	def find_item_by_number(self, item_number: str) -> dict | None:
		"""The full FastBound item with bound-book number `item_number`
		(zero-padded, e.g. "01171"), or None. Exact match only — `GET
		/Items?itemNumber=` may substring-match, so verify client-side."""
		wanted = (item_number or "").strip()
		if not wanted:
			return None
		try:
			res = self._request("GET", "Items",
				params={"itemNumber": wanted, "take": 150, "skip": 0}) or {}
		except FastBoundError as exc:
			if exc.status_code == 404:
				return None
			raise
		items = res.get("items") or res.get("data") or res.get("results") or []
		for it in items:
			if str(it.get("itemNumber") or "").strip() == wanted and not it.get("deleted"):
				return it
		return None

	def set_item_external_id(self, item_id: str, external_id: str) -> None:
		"""PUT /Items/{id}/SetExternalId — changes ONLY the externalId. Unlike
		the full read-merge-write PUT /Items/{id}, this route also works on a
		DISPOSED item, which is exactly the freeing-a-returned-gun's-id case."""
		item_id = (item_id or "").strip()
		if not item_id:
			raise FastBoundError("set_item_external_id: no item id given.")
		self._request("PUT", f"Items/{quote(item_id, safe='')}/SetExternalId",
			json_body={"externalId": external_id})

	@staticmethod
	def _item_is_disposed(item: dict) -> bool:
		"""True when a FastBound item payload reflects a committed disposition.
		The list endpoint (`iter_items`) exposes a `disposed` bool; the single-item
		GET carries a `status` object + `dispose_*` fields instead — accept any."""
		if item.get("disposed"):
			return True
		status = item.get("status")
		if isinstance(status, dict) and (status.get("name") or "").strip().lower() == "disposed":
			return True
		return bool(item.get("dispose_Date"))

	def _existing_disposition_summary(self, fb_item_id: str) -> str | None:
		"""For the 'already disposed' 400: re-fetch the item and, if it really is
		disposed, return a human summary of the EXISTING disposition —
		``FastBound item #<num>, disposed <date> as '<type>' to '<recipient>'``.
		Returns None when the item can't be fetched or isn't actually disposed, so
		the caller re-raises the original 400 (it was a different rejection)."""
		try:
			item = self._request("GET", f"Items/{fb_item_id}") or {}
		except Exception:  # noqa: BLE001 — see below
			# ANY failure, not just FastBoundError: a requests Timeout / SSLError
			# out of this best-effort lookup would otherwise propagate OUT of the
			# caller's `except FastBoundError` block and replace the real 400 with
			# a connection error, losing the rejection the operator has to act on.
			return None
		if not self._item_is_disposed(item):
			return None
		num = item.get("itemNumber") or fb_item_id
		dtype = item.get("dispositionType") or "?"
		ddate = (item.get("dispose_Date") or "")[:10] or "?"
		recipient = (
			item.get("dispose_LicenseName")
			or " ".join(filter(None, (item.get("dispose_FirstName"), item.get("dispose_LastName"))))
			or "?"
		)
		return f"FastBound item #{num}, disposed {ddate} as {dtype!r} to {recipient!r}"

	def _reacquired_item_id(self, serial: str, disposed_id: str) -> str | None:
		"""For the 'already disposed' 400: the in-stock item that re-acquired this
		gun after ``disposed_id`` left the book, or None.

		Adopted only when it is unambiguously the same gun coming back: the old
		item really is disposed, exactly one in-stock item carries the serial
		(``find_item_by_serial`` raises on more), it has the old item's
		manufacturer, and it was acquired on or after the old disposition date.
		Best-effort like the summaries: any lookup failure returns None, so the
		caller keeps the original 400."""
		try:
			old = self.get_item(disposed_id) or {}
			if not self._item_is_disposed(old):
				return None
			new = self.find_item_by_serial(serial) or {}
		except Exception:  # noqa: BLE001 — see _existing_disposition_summary
			return None
		new_id = new.get("id") or new.get("itemId")
		if not new_id or new_id == disposed_id or self._item_is_disposed(new):
			return None
		if (new.get("manufacturer") or "").strip().casefold() != (
				old.get("manufacturer") or "").strip().casefold():
			return None
		disposed_on = (old.get("dispose_Date") or "")[:10]
		acquired_on = (new.get("acquire_Date") or "")[:10]
		if not disposed_on or acquired_on < disposed_on:
			return None
		return new_id

	def _holding_disposition_summary(self, fb_item_id: str) -> str | None:
		"""For the 'has already been added to another disposition' 400: name the
		disposition that is SITTING ON the item.

		Different state, different remedy, from ``_existing_disposition_summary``
		above. There the gun has left the book and the transfer cannot happen
		again. Here it has not: an UNCOMMITTED disposition — nearly always an
		abandoned 4473 wizard — is holding the item, the bound book still shows
		it available, and the moment somebody finishes or deletes that
		disposition this push succeeds unchanged. DISP-2026-02000 is that story
		end to end, including the self-heal.

		Which is why the caller must NOT mark the resulting error terminal, and
		why this exists at all: the raw 400 names an item number and no owner, so
		an operator looking at it has nowhere to go.

		Returns None when the item can't be fetched, is genuinely disposed, or
		carries no disposition id — leaving the caller to re-raise the original.
		"""
		try:
			item = self._request("GET", f"Items/{fb_item_id}") or {}
		except Exception:  # noqa: BLE001 — a transport failure here must not
			# replace the caller's original 400 (same reason as
			# _existing_disposition_summary above; this branch adds two GETs).
			return None
		disp_id = str(item.get("dispositionId") or "").strip()
		if not disp_id or self._item_is_disposed(item):
			return None
		num = item.get("itemNumber") or fb_item_id
		detail = ""
		try:
			disp = self._request("GET", f"Dispositions/{disp_id}") or {}
		except Exception:  # noqa: BLE001 — detail is decoration; the id is the
			# answer, and a timeout fetching the detail must not lose it.
			disp = {}
		status = disp.get("status")
		if isinstance(status, dict):
			status = status.get("name")
		bits = [str(b) for b in (status, (disp.get("type") or disp.get("dispositionType")),
			str(disp.get("date") or "")[:10]) if b]
		if bits:
			detail = " — " + ", ".join(bits)
		return (f"disposition {disp_id}{detail}, holding FastBound item #{num} "
			f"(the item is NOT disposed)")

	def _cr_contact_payload(self, ffl: str, disp) -> dict:
		"""Inline FastBound contact for a Type 03 (Curio & Relic) recipient.

		Data source is the `ATF FFL Record`, which for a C&R order was written
		from values a human read off the licence copy at dispose time — not from
		the ATF directory (collectors are not in it) and not from the shipping
		address.

		Three hard requirements, each learned on the acquisition side of this
		same API (see the contact builder below, ~line 1030):

		* `lookupFFL` must be False, and is NOT read from `lookup_ffl_on_push` —
		  production has that setting on, and honouring it here is precisely the
		  400 this branch exists to avoid.
		* a premise address is required on EVERY contact, individual or FFL;
		* with lookup off, FastBound rejects an FFL contact that carries no
		  expiry.

		If any of those is missing we raise instead of sending: the request is
		already known to fail, and a failed push leaves a retryable
		`fastbound_sync_error` rather than a half-written book. We never fall
		back to an individual-style contact — that would record a
		licensee-to-licensee transfer as a sale to a non-licensee, i.e. claim a
		4473 that does not exist.
		"""
		rec = frappe.db.get_value("ATF FFL Record", ffl,
			["licensee_name", "street", "street2", "city", "state", "zip", "expiry_date"],
			as_dict=True) or {}

		expiry = rec.get("expiry_date")
		expiry_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else (str(expiry or "") or "")
		licensee = (rec.get("licensee_name") or "").strip()

		normalized = _normalize_ffl(ffl)
		contact: dict[str, Any] = {
			"externalId": f"ffl-{normalized}",
			# 15 chars, no separators — the form the live book actually stores.
			"fflNumber": normalized,
			"lookupFFL": False,
			# The collector's name goes here and ONLY here. FastBound refuses a
			# contact that carries FFL information alongside person-name fields:
			#   400 "When FFL information provided, Organization Name -AND-
			#        First Name, Middle Name, Last Name, and Suffix must be empty."
			# (measured against FastBound test account 147648, 2026-07-29; the
			# same payload minus firstName/lastName returned 201 and stored the
			# licenseName verbatim.) A Type 03 licence is issued to a person, so
			# licenseName IS that person's name — nothing is lost by omitting the
			# structured name fields. The acquisition-side builder below already
			# obeys this rule by branching FFL-vs-individual; this branch did not,
			# which would have 400'd every single C&R disposition.
			"licenseName": licensee[:100],
			"fflExpires": expiry_iso,
			"premiseAddress1": (rec.get("street") or "").strip()[:100],
			"premiseCity": (rec.get("city") or "").strip(),
			"premiseState": (rec.get("state") or "").strip(),
			"premiseZipCode": (rec.get("zip") or "").strip(),
			"premiseCountry": "US",
		}
		street2 = (rec.get("street2") or "").strip()
		if street2:
			contact["premiseAddress2"] = street2[:100]

		required = ("licenseName", "fflExpires", "premiseAddress1", "premiseCity",
			"premiseState", "premiseZipCode")
		missing = [k for k in required if not contact.get(k)]
		if missing:
			# Point the operator at the record that actually carries the C&R
			# verification, so "re-run the C&R verification on X" names a document
			# they can open. That is the channel order when there is one — a
			# disposition's own name would send them somewhere with no C&R fields
			# on it at all. GunBroker is a second C&R-shipping channel (its
			# collector buyers are the reason the channel exists), so its order
			# link counts here exactly as the Woo one does.
			where = (getattr(disp, "woo_online_order", "")
				or getattr(disp, "gunbroker_order", "")
				or disp.name)
			raise FastBoundError(
				f"C&R contact for {ffl} is incomplete (missing: {', '.join(missing)}) — "
				f"re-run the C&R verification on {where}, then retry the push.")
		return contact

	def post_disposition(self, disp) -> str:
		"""Push one non-4473 FFL Disposition to FastBound and return its id.

		POST /Dispositions/CreateAndCommit. Transfers (Dealer/FFL Transfer, Return)
		→ requestType=Regular with the disposition type + a recipient FFL contact
		built from destination_ffl. Destruction (Scrap/Destroyed) → requestType=
		Destroyed (no recipient). The firearm is referenced by its FastBound item
		Id, resolved from the serial (Serial No fast-path, else GET by externalId).
		"""
		serial = (disp.serial or "").strip()
		fb_item_id = None
		if serial and frappe.db.exists("Serial No", serial):
			fb_item_id = frappe.db.get_value("Serial No", serial, "fastbound_item_id")
		fb_item_id = fb_item_id or self.get_item_id_by_serial(serial)
		if not fb_item_id:
			raise FastBoundError(
				f"Disposition {disp.name}: firearm serial {serial!r} not found in "
				f"FastBound — was its acquisition pushed?")

		date_iso = (disp.disposition_date.isoformat()
			if hasattr(disp.disposition_date, "isoformat") else disp.disposition_date)
		body: dict[str, Any] = {
			"externalId": disp.name,
			"date": date_iso,
			"items": [{"id": fb_item_id}],
		}
		note = (disp.notes or "").strip()
		if note:
			body["note"] = note[:1000]

		if disp.disposition_type in DESTRUCTION_DISPOSITION_TYPES:
			body["requestType"] = "Destroyed"
			body["destroyed_Date"] = date_iso
			body["destroyed_Description"] = (note or disp.disposition_type)[:200]
			# FastBound requires two witnesses on a destruction (ATF rule).
			body["destroyed_Witness1"] = (getattr(disp, "destroyed_witness1", None) or "")[:100]
			body["destroyed_Witness2"] = (getattr(disp, "destroyed_witness2", None) or "")[:100]
		else:
			body["requestType"] = "Regular"
			body["type"] = (disp.disposition_type or "")[:100]
			ffl = (disp.destination_ffl or "").strip()
			if ffl:
				# Reference the existing FFL contact when FastBound already has it
				# (creating a duplicate 400s "This FFL Number already exists"); only
				# create inline when the account doesn't have the FFL yet.
				existing = self.find_contact_by_ffl_number(ffl)
				cid = (existing or {}).get("id") or (existing or {}).get("contactId")
				if cid:
					body["contactId"] = cid
				elif is_cr_license(ffl):
					# Type 03 (Curio & Relic): FastBound's lookupFFL is backed by the
					# ATF licence list, which does not include collectors — the lookup
					# cannot succeed, and its 400 would land AFTER the firearm has left
					# the building (stock out, parcel moving, nothing in the bound
					# book). Supply every field ourselves instead. See D3 / C4.
					body["contact"] = self._cr_contact_payload(ffl, disp)
				else:
					body["contact"] = {
						"externalId": f"ffl-{_normalize_ffl(ffl)}",
						"fflNumber": ffl,
						"lookupFFL": bool(int(
							frappe.db.get_single_value(SETTINGS_DOCTYPE, "lookup_ffl_on_push") or 0)),
					}

		try:
			response = self._request("POST", "Dispositions/CreateAndCommit", json_body=body)
		except FastBoundError as exc:
			# A gun back in stock that was re-booked by hand in FastBound (a returned
			# transfer, a trip to the gunsmith) is a NEW item; the Serial No still
			# points at the old, disposed one. Dispose the re-acquired item and
			# repoint the Serial No so every later lookup finds the right one.
			newer = self._reacquired_item_id(serial, fb_item_id) if exc.status_code == 400 else None
			if newer:
				frappe.db.set_value("Serial No", serial, "fastbound_item_id", newer,
					update_modified=False)
				fb_item_id = newer
				body["items"] = [{"id": newer}]
				response = self._request("POST", "Dispositions/CreateAndCommit", json_body=body)
			# FastBound refuses to dispose an item that's already disposed (e.g. a
			# transfer/return booked directly in FastBound, or a duplicate sale):
			# "Item <n> was disposed on <date>." A firearm can't be disposed twice,
			# so do NOT adopt the prior disposition's id — that would silently
			# mis-record THIS transfer as the existing one (different recipient /
			# date), burying an ATF discrepancy. Re-raise a clear, operator-
			# actionable message naming the existing disposition; the record stays
			# in fastbound_sync_error for a human to reconcile.
			elif exc.status_code == 400:
				existing = self._existing_disposition_summary(fb_item_id)
				if existing:
					raise FastBoundError(
						f"Disposition {disp.name}: firearm serial {serial!r} is already "
						f"disposed in FastBound ({existing}) and cannot be disposed again. "
						f"If that is this same transfer, link this record to it with "
						f"ffl_integrations.fastbound.reconcile.reconcile_record_to_fastbound "
						f"(pass the FastBound item number); if it is wrong, void it in "
						f"FastBound (or cancel this sale if it is a duplicate). Then retry.",
						status_code=exc.status_code,
						terminal=True,
					) from exc
				# Not disposed, but something else in FastBound is holding the
				# item: "Item 00843 has already been added to another
				# disposition." The raw 400 names the item and not the owner, so
				# the operator has nowhere to go — DISP-2026-02000 took a manual
				# FastBound hunt to find an abandoned 4473 wizard.
				#
				# Deliberately NOT terminal. The hold is temporary by nature:
				# finish or delete that disposition and this same push succeeds,
				# which is exactly how 00843 healed. Stamping [TERMINAL] would
				# take it out of reconcile_failed_pushes and leave the A&D gap
				# open until somebody noticed by hand.
				#
				# Gate on what FastBound SAID, not only on the item's state. Any
				# other 400 — a missing required field, a rejected contact — on an
				# item that happens to carry a dangling dispositionId would
				# otherwise be rewritten into "an abandoned 4473 is holding this"
				# and send the operator off to delete a disposition that was never
				# the problem, with the real rejection gone from the message.
				holder = (self._holding_disposition_summary(fb_item_id)
					if _said_item_is_held(exc) else None)
				if holder:
					raise FastBoundError(
						f"Disposition {disp.name}: firearm serial {serial!r} is already "
						f"attached to another, uncommitted disposition in FastBound "
						f"({holder}) — usually a 4473 that was started in FastBound and "
						f"abandoned. Open that disposition in FastBound and either "
						f"finish it or delete it (from POS: Pending 4473 Orders → "
						f"\"Clear Abandoned Order\"), then retry this push — the daily "
						f"reconcile will also pick it up on its own once the item is "
						f"free. FastBound said: {exc}",
						status_code=exc.status_code,
					) from exc
			if not newer:
				raise
		# CreateAndCommit returns 204 No Content — no id in the response. The
		# committed disposition's id lands on the disposed item, so re-fetch the
		# item to capture it (future-proof: also accept an id in the body).
		# Defensive: coerce non-dict responses to {} BEFORE calling .get, so a
		# future non-dict body (string error, list) can't AttributeError.
		r = response if isinstance(response, dict) else {}
		fb_id = r.get("id") or r.get("dispositionId")
		if not fb_id:
			item = self._request("GET", f"Items/{fb_item_id}") or {}
			fb_id = item.get("dispositionId")
		if not fb_id:
			raise FastBoundError(
				f"Disposition {disp.name}: CreateAndCommit succeeded but no disposition "
				f"id was found on item {fb_item_id}.")
		return fb_id

	def delete_disposition(self, fastbound_id: str) -> None:
		"""DELETE /api/Dispositions/{id}. 404 (already gone) is a no-op."""
		try:
			self._request("DELETE", f"Dispositions/{fastbound_id}")
		except FastBoundError as exc:
			if exc.status_code == 404:
				return
			raise

	def undispose_item(self, item_id: str, note: str | None = None) -> None:
		"""PUT /api/Items/{id}/Undispose — return a disposed firearm to available
		inventory. Needed because DELETE /Dispositions/{id} removes the disposition
		record but can leave the item status=Disposed with a dangling dispositionId
		(observed 2026-07-07). FastBound audit-logs the undispose."""
		self._request(
			"PUT", f"Items/{quote(item_id, safe='')}/Undispose",
			json_body={"note": note or ""})

	# -------------------------------------------------- Contacts

	def find_contact_by_external_id(self, external_id: str) -> dict | None:
		"""GET /Contacts/GetByExternalId/{externalId} — returns the contact
		dict if it exists, None on 404.
		"""
		try:
			return self._request("GET", f"Contacts/GetByExternalId/{external_id}") or None
		except FastBoundError as exc:
			if exc.status_code == 404:
				return None
			raise

	def find_contact_by_ffl_number(self, ffl_number: str) -> dict | None:
		"""Paginate /Contacts and return the first contact whose fflNumber
		matches once dashes/spaces are stripped. FastBound normalises FFL
		numbers when storing them (`5-99-888-01-9D-26128` becomes
		`599888019D26128`) and its `/Contacts` list endpoint ignores the
		fflNumber query parameter, so we filter client-side.
		"""
		needle = _normalize_ffl(ffl_number)
		if not needle:
			return None
		# FastBound `skip` is a page index, not a record offset (see iter_items);
		# advance one page per request and cap the scan at ~2000 contacts.
		take, page, seen = 100, 0, 0
		while seen <= 2000:
			try:
				res = self._request("GET", "Contacts",
					params={"take": take, "skip": page}) or {}
			except FastBoundError as exc:
				if exc.status_code == 404:
					return None
				raise
			contacts = res.get("contacts") or res.get("items") or res.get("results") or []
			if not contacts:
				return None
			for ct in contacts:
				if _normalize_ffl(ct.get("fflNumber")) == needle:
					return ct
			if len(contacts) < take:
				return None
			page += 1
			seen += take
		# Loop exited via the 2000-contact cap, NOT via exhaustion — we did not
		# scan the whole list. Returning None ("not found") here would make the
		# caller create a DUPLICATE contact (FastBound then 400s on the existing
		# FFL), so fail loudly instead.
		raise FastBoundError(
			f"Contact search for FFL {ffl_number} scanned {seen} contacts without a "
			f"match and without exhausting the list — aborting to avoid a duplicate."
		)

	def find_contact_by_name(self, first_name: str, last_name: str) -> dict | None:
		"""Find an individual contact by first + last name. Returns the matching
		contact dict, None if none match, and RAISES on more than one match.

		Like `find_contact_by_ffl_number`, FastBound's `/Contacts` list endpoint
		ignores the firstName/lastName query params, so we paginate and compare
		client-side (case/space-insensitive). We scan the WHOLE list rather than
		returning the first hit: two people sharing a name is ambiguous, and
		linking a regulated bound-book acquisition to the wrong seller is worse
		than failing loudly — the record stays stuck with a clear error instead.
		"""
		nf = str(first_name or "").strip().casefold()
		nl = str(last_name or "").strip().casefold()
		if not (nf or nl):
			return None
		take, page, seen = 100, 0, 0
		matches: dict[str, dict] = {}  # id -> contact (de-dupes a contact seen across pages)
		while seen <= 2000:
			try:
				res = self._request("GET", "Contacts", params={
					"take": take, "skip": page,
					"firstName": first_name, "lastName": last_name,
				}) or {}
			except FastBoundError as exc:
				if exc.status_code == 404:
					break
				raise
			contacts = res.get("contacts") or res.get("items") or res.get("results") or []
			if not contacts:
				break
			for ct in contacts:
				if (str(ct.get("firstName") or "").strip().casefold() == nf and
						str(ct.get("lastName") or "").strip().casefold() == nl):
					cid = ct.get("id") or ct.get("contactId")
					if cid:
						matches[cid] = ct
			if len(contacts) < take:
				break
			page += 1
			seen += take
		else:
			# Hit the cap without exhausting the list — a later page could hold
			# another match, so we can't be sure this name is unique. Fail loud.
			raise FastBoundError(
				f"Contact search for {first_name!r} {last_name!r} scanned {seen} "
				f"contacts without exhausting the list — aborting to avoid a wrong match."
			)
		if len(matches) > 1:
			raise FastBoundError(
				f"{len(matches)} FastBound contacts match {first_name!r} {last_name!r} — "
				f"refusing to guess which seller; resolve the duplicate in FastBound."
			)
		return next(iter(matches.values())) if matches else None

	@staticmethod
	def _firearm_item_fields(item_code: str, serial: str | None) -> dict:
		"""The FastBound item attribute block for one physical firearm.

		type / sku / upc / mpn come from the model-SKU Item; manufacturer /
		model / caliber are overridden by the Serial No when set — old / surplus /
		imported guns under one shared SKU can each carry a different maker (different
		arsenals), importer marking (different import batches), or even model/caliber,
		and the bound book records the physical gun, not the model.

		importer comes from the Serial No ONLY (2026-08-20) — never the Item.
		On generic surplus buckets the Item-level value is one gun's marking,
		and a wrong importer must not enter the bound book; a gun whose serial
		carries none is simply pushed without one (the field is optional).

		Shared by the acquisition push (_acquisition_payload) and the
		post-acquisition item edit (update_item) so both agree on the shape.
		"""
		item = frappe.db.get_value(
			"Item", item_code,
			["manufacturer", "model_name", "caliber", "firearm_type", "upc", "mpn"],
			as_dict=True,
		) or {}
		sn = frappe.db.get_value(
			"Serial No", serial,
			["manufacturer", "importer", "model_name", "caliber", "firearm_type"], as_dict=True,
		) if serial else None
		manufacturer = (sn and sn.get("manufacturer")) or item.get("manufacturer")
		importer = (sn or {}).get("importer")
		model = (sn and sn.get("model_name")) or item.get("model_name")
		caliber = (sn and sn.get("caliber")) or item.get("caliber")
		firearm_type = (sn and sn.get("firearm_type")) or item.get("firearm_type")

		# caliber / manufacturer / model / serial / type are the five fields
		# FastBound requires on every item; fall back to "Unknown" rather than
		# failing the push — better an imperfect A&D record than none.
		fields = {
			"manufacturer": (manufacturer or "Unknown")[:100],
			"model": (model or "Unknown")[:100],
			"serial": serial or "",
			"caliber": (caliber or "Unknown")[:100],
			"type": _fb_item_type(firearm_type),
			# Item identifiers → FastBound UPC/SKU/MPN. SKU is our Item code; UPC
			# falls back to the Item Barcode table for pre-upc-field items.
			"sku": item_code or None,
			"upc": (item.get("upc") or _upc_from_barcode(item_code)) or None,
			"mpn": item.get("mpn") or None,
		}
		# Importer is optional (imported firearms only) — send only when present,
		# never the "Unknown" placeholder used for the five required fields.
		if importer:
			fields["importer"] = str(importer)[:100]
		return fields

	@staticmethod
	def _acquisition_payload(acq) -> dict:
		"""Map our FFL Acquisition + linked Item into the FastBound
		CreateAndCommitAcquisition.Command shape.

		FastBound's `type` is a free-form string per the spec; the
		conventional values used by their UI (and accepted by the API)
		are: Individual / FFL / Manufacturer / Importer / Other.
		Our richer source_types (Trade-in / Estate / Pawn) collapse
		to Individual with the original tag preserved in `note`.
		"""
		fb_type_map = {
			"FFL Dealer": "FFL",
			"Individual": "Individual",
			"Trade-in": "Individual",
			"Estate": "Individual",
			"Pawn": "Individual",
			"Manufacturer": "Manufacturer",
			"Importer": "Importer",
		}
		date_iso = (
			acq.acquisition_date.isoformat()
			if hasattr(acq.acquisition_date, "isoformat") else acq.acquisition_date
		)

		# externalId must be unique per physical firearm. Use the serial: one
		# Item code is shared across every unit of a catalog SKU (RSR new
		# firearms), so the Item code collides on the 2nd+ unit — "External ID
		# <code> is already used on an acquired item".
		acquired_item = {
			"externalId": acq.serial or acq.item or "",
			**FastBoundClient._firearm_item_fields(acq.item, acq.serial),
		}
		if acq.cost not in (None, ""):
			acquired_item["cost"] = str(acq.cost)

		# Build the contact (seller). FastBound requires a premise address on
		# every contact (firstName/lastName for individuals or licenseName for
		# FFLs, PLUS premiseAddress1/City/State/ZipCode/Country always).
		#
		# Best-effort: pull from acq.source_address (free-text); when that's
		# blank, fall back to the linked Customer's primary Address — the one
		# chokepoint both intake flows pass through, so Receive Goods
		# Individual (which captures no free-text address) still books a real
		# address per §478.125(e). Only if BOTH are empty do
		# the "Unknown" placeholders apply, so the push doesn't 400 — the
		# dealer must then edit the contact in FastBound's UI. Future: replace
		# source_address with structured fields on FFL Acquisition.
		contact: dict[str, Any] = {}
		addr = _parse_source_address(
			acq.source_address or _customer_address_line(acq), company=acq.get("company"))
		contact.update(addr)

		if acq.source_type == "FFL Dealer" and acq.source_ffl:
			contact["fflNumber"] = acq.source_ffl
			# Setting-gated: production prefers lookupFFL=True to auto-populate
			# licenseName + address from FastBound's FFL database; test/dev
			# environments turn it off to accept FFL strings their lookup
			# doesn't recognize. Default True (set in fastbound_settings.json).
			contact["lookupFFL"] = bool(int(
				frappe.db.get_single_value("FastBound Settings", "lookup_ffl_on_push") or 0
			))
			if acq.source_name:
				contact["licenseName"] = acq.source_name[:100]
			# FastBound rejects FFL pushes without an expiry when lookup is off.
			# Look up the Supplier's ffl_expiry_date and pass it as ISO 8601.
			ffl_expires = frappe.db.get_value("Supplier",
				{"ffl_number": acq.source_ffl}, "ffl_expiry_date")
			if ffl_expires:
				iso = ffl_expires.isoformat() if hasattr(ffl_expires, "isoformat") else str(ffl_expires)
				contact["fflExpires"] = iso
		else:
			# Individual-style sources: structured first/middle/last, with a
			# source_name fallback (see _individual_name).
			first, middle, last = _individual_name(acq)
			if first:
				contact["firstName"] = first[:100]
			if middle:
				contact["middleName"] = middle[:100]
			if last:
				contact["lastName"] = last[:100]
			# Generational suffix is its own FastBound contact field — folding
			# it into a name field is how "lee IV Sampleton" would
			# end up as a last name in the bound book.
			suffix = (getattr(acq, "source_suffix", "") or "").strip()
			if suffix:
				contact["suffix"] = suffix[:20]

		# Stable externalId so a later push for the same seller resolves THIS
		# contact (GetByExternalId) instead of creating a duplicate — the bug that
		# 400'd "This contact already exists" on the 2nd+ gun from one seller.
		ext_id = _contact_external_id(acq)
		if ext_id:
			contact["externalId"] = ext_id

		# Bound-book note: source-type default, plus the internal procurement
		# source (RSR / Distributor / …) which isn't a native FastBound field.
		# Compose with " · ".join(filter(None, …)) so an empty base note (the
		# common FFL Dealer case where _note_for_source returns '') doesn't
		# produce a stray leading separator like ' · Source: RSR' in the
		# bound book.
		base_note = acq.notes or _note_for_source(acq.source_type)
		src = (acq.get("receive_source") or "").strip()
		# An amendment must be traceable as a CORRECTION in the
		# bound book, not read like a fresh acquisition — tag the predecessor
		# (acq.notes carries the human reason, stamped by the amend flow).
		amend_tag = (
			f"Amends {acq.amended_from}" if (acq.get("amended_from") or "").strip() else None
		)
		note = " · ".join(
			part for part in (amend_tag, base_note, f"Source: {src}" if src else None) if part
		)

		# Acquisition type: the user-selected FastBound AcquireType when set
		# (synced from FastBound's SmartList), else the source-type mapping.
		acq_type = (acq.get("transaction_type") or "").strip() or fb_type_map.get(acq.source_type, "Individual")

		body = {
			"externalId": acq.name,
			"date": date_iso,
			"type": acq_type,
			"note": note,
			"contact": contact,
			"items": [acquired_item],
		}
		# Bound-book purchase-order provenance: a firearm restocked through a
		# Distributor Order carries the DO name as its FastBound PO number. Optional
		# — blank for manual / individual acquisitions. The DO reaches the
		# acquisition BEFORE this push because create_receive stamps
		# `distributor_order` from its payload key ahead of the in-transaction push.
		# TODO(go-live): confirm the exact CreateAndCommit key/placement for the PO
		# number against the FastBound API manual — the acquisition uses simplified
		# top-level keys (`date`/`type`) while `acquire_PurchaseOrderNumber` is the
		# verified PUT/Items item-field name (allow-list :54); main-session only.
		po_num = (acq.get("distributor_order") or "").strip()
		if po_num:
			body["acquire_PurchaseOrderNumber"] = po_num
		return body


def _upc_from_barcode(item_code: str | None) -> str | None:
	"""Fallback UPC for items promoted before Item.upc was populated: the first
	UPC-type row in the standard Item Barcode table (RSR always writes it there)."""
	if not item_code:
		return None
	return frappe.db.get_value(
		"Item Barcode", {"parent": item_code, "barcode_type": "UPC"}, "barcode")


def _normalize_ffl(value: str | None) -> str:
	"""Strip non-alphanumeric characters from an FFL number so dashes/spaces
	don't defeat equality checks. FastBound stores `599888019D26128`; our
	suppliers often record it as `5-99-888-01-9D-26128`."""
	if not value:
		return ""
	return "".join(ch for ch in str(value) if ch.isalnum()).upper()


def _individual_name(acq) -> tuple[str, str, str]:
	"""(first, middle, last) for an individual seller. Prefer the structured
	first/middle/last captured at receive; fall back to splitting the free-text
	source_name (trade-in / imported records) only when those are blank — a
	one-word name would otherwise miss the last name and FastBound 400s.

	Shared by the acquisition payload (the contact we send) and the contact
	resolver (the name we search by) so the two can't drift apart."""
	first = (getattr(acq, "source_first_name", "") or "").strip()
	middle = (getattr(acq, "source_middle_name", "") or "").strip()
	last = (getattr(acq, "source_last_name", "") or "").strip()
	if not (first and last):
		parts = (getattr(acq, "source_name", "") or "").strip().split(None, 1)
		first = first or (parts[0] if parts else "")
		last = last or (parts[1] if len(parts) > 1 else "")
	return first, middle, last


def _contact_external_id(acq) -> str | None:
	"""Stable, unique externalId for the seller contact so re-pushes resolve the
	same FastBound contact (GetByExternalId) instead of creating a duplicate.

	Individuals key off the linked Customer (`source_customer`). FFL dealers keep
	their existing FFL-number resolution path untouched — return None so nothing
	about that flow changes."""
	if getattr(acq, "source_type", None) == "FFL Dealer":
		return None
	cust = (acq.get("source_customer") if hasattr(acq, "get")
		else getattr(acq, "source_customer", None)) or ""
	cust = str(cust).strip()
	return f"customer-{cust}" if cust else None


def _customer_address_line(acq) -> str:
	"""Fallback seller address for individual-style acquisitions that carry a
	source_customer but no free-text source_address (Receive Goods Individual;
	legacy records on re-push): the Customer's primary Address composed into
	the single-line shape _parse_source_address handles. Empty string when
	there's nothing usable — the placeholder path then applies as before."""
	cust = (acq.get("source_customer") if hasattr(acq, "get")
		else getattr(acq, "source_customer", None)) or ""
	cust = str(cust).strip()
	if not cust:
		return ""
	from ffl_core.api.trade_in import customer_address_line
	return customer_address_line(cust)


_ADDRESS_PLACEHOLDER = {
	"premiseAddress1": "Unknown - please edit in FastBound",
	"premiseCity": "Unknown",
	"premiseZipCode": "00000",
	"premiseCountry": "United States",
}


def _placeholder_address(company: str | None) -> dict[str, str]:
	"""The "Unknown" address for a contact with none, in the acquisition's own Company's state.

	FastBound requires a state on every contact, and the only honest one is the
	store's own (the state of that Company's address). A Company with no readable
	address makes ``home_state`` raise its fix-it message, so the push stops with
	"Set the company's address ..." instead of FastBound answering a 400. An
	acquisition with no Company at all gets its own clear error, not an IndexError.
	"""
	if not company:
		frappe.throw(
			_("Set up a Company with an address (with a US state) in Company › Addresses "
				"before sending this record to FastBound."),
			frappe.ValidationError, title=_("Company missing"))
	from ffl_core.compliance.interstate_tax import home_state

	return {**_ADDRESS_PLACEHOLDER, "premiseState": home_state(company)}


# US address tail: "City Name ST 12345" or "City Name ST 12345-6789".
# Greedy city allows multi-word ("New York"); state is 2 uppercase letters;
# zip is 5 or 9 digits.
import re as _re
_ADDR_TAIL_RE = _re.compile(r"^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$")


def _parse_source_address(raw: str | None, company: str | None = None) -> dict[str, str]:
	"""Parse FFL Acquisition.source_address (Long Text) into the structured
	fields FastBound requires on every contact.

	Recognized shapes (in priority order):

	  1. ``addr1\\ncity\\nstate\\nzip[\\ncountry]``    (Supplier Quick Entry)
	  2. ``addr1, city ST 12345[-6789]``               (single-line cashier)
	  3. ``addr1, city, state, zip[, country]``        (4+ comma parts)

	``company`` is the acquisition's own Company: the "Unknown" placeholder takes
	its state from that Company's address.

	Long-term fix: structured fields on FFL Acquisition (`source_*` columns).
	"""
	if not raw or not raw.strip():
		return _placeholder_address(company)

	# 1. Newline-separated (Supplier Quick Entry joins with \n).
	if "\n" in raw:
		parts = [p.strip() for p in raw.split("\n") if p.strip()]
		if len(parts) >= 4:
			return _build_address(parts)

	# 2. Single-line "addr1, city ST zip" pattern (most common cashier shape).
	if "," in raw:
		head, _, tail = raw.strip().rpartition(",")
		m = _ADDR_TAIL_RE.match(tail.strip())
		if m and head.strip():
			city, state, zip_ = m.groups()
			return {
				"premiseAddress1": head.strip()[:100],
				"premiseCity": city.strip()[:100],
				"premiseState": state,
				"premiseZipCode": zip_,
				"premiseCountry": "United States",
			}
		# Fallback: pure 4-part comma split.
		parts = [p.strip() for p in raw.split(",") if p.strip()]
		if len(parts) >= 4:
			return _build_address(parts)

	out = _placeholder_address(company)
	out["premiseAddress1"] = raw.strip()[:100]
	return out


def _build_address(parts: list[str]) -> dict[str, str]:
	return {
		"premiseAddress1": parts[0][:100],
		"premiseCity": parts[1][:100],
		"premiseState": parts[2][:100],
		"premiseZipCode": parts[3][:20],
		"premiseCountry": (parts[4] if len(parts) > 4 else "United States")[:100],
	}


def _coerce_items(body: Any) -> list[dict]:
	"""FastBound paginated responses sometimes wrap the list (`{items: [...]}`)
	and sometimes return a bare array. Handle both."""
	if body is None:
		return []
	if isinstance(body, list):
		return body
	if isinstance(body, dict):
		for key in ("items", "data", "results"):
			val = body.get(key)
			if isinstance(val, list):
				return val
	return []


def _note_for_source(source_type: str) -> str:
	"""Surface non-FFL non-individual sources to FastBound via note since
	their type taxonomy is narrower than ours."""
	if source_type in {"Estate", "Pawn"}:
		return source_type
	return ""


_FB_PASSTHROUGH_TYPES = frozenset(ATF_FIREARM_TYPES)


def _fb_item_type(firearm_type: str | None) -> str:
	"""Our firearm_type vocabulary → FastBound's item type string.

	FastBound's type is a free-form string per spec but the values their
	UI uses (and the bound book renders) match ATF wording directly, so
	every canonical vocabulary value — the NFA classifications included —
	passes through unchanged. Only legacy aliases and junk fold.
	"""
	t = (firearm_type or "").strip()
	if t in _FB_PASSTHROUGH_TYPES:
		return t
	# Legacy aliases — items created before the ATF migration.
	return {
		"Handgun": "Pistol",
		"Long Gun": "Rifle",
		"Other": "Other Firearm",
	}.get(t, "Other Firearm")
