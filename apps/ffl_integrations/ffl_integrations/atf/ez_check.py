"""Pluggable ATF EZ Check provider with TTL-based caching to ATF FFL Record."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

import frappe
from frappe.utils import getdate, now_datetime

from ffl_core.compliance.ffl_number import is_cr_license
from ffl_core.roles import COUNTER_ROLES, SYSTEM_ROLES


class EZCheckUnavailable(RuntimeError):
	"""Provider couldn't return a fresh result (HTTP error, parse failure, rate-limit)."""


@dataclass
class EZCheckResult:
	ffl_number: str
	found: bool
	status: str  # 'Verified' | 'Expired' | 'Failed' | 'NotFound'
	licensee_name: str | None = None
	licensee_dba: str | None = None
	license_type: str | None = None
	expiry_date: date | None = None
	address: str | None = None
	# Structured address, when the provider supplies one (the Data Service parses
	# server-side). Left None by the scraper — _upsert_cache then falls back
	# to splitting the flat address string locally.
	street: str | None = None
	street2: str | None = None
	city: str | None = None
	state: str | None = None
	zip: str | None = None
	raw: dict | None = None
	fetched_at: datetime = field(default_factory=now_datetime)


class EZCheckProvider(Protocol):
	name: str
	def verify(self, ffl_number: str) -> EZCheckResult: ...


def get_provider() -> EZCheckProvider:
	"""Resolve the configured provider from FFL Settings."""
	settings = frappe.get_single("FFL Settings")
	provider_name = (settings.live_check_provider or "ATF Form Scraper").strip()
	if provider_name == "ATF Form Scraper":
		from ffl_integrations.atf.scraper import ATFFormScraperProvider
		return ATFFormScraperProvider()
	if provider_name == "Data Service":
		from ffl_integrations.atf.data_service_provider import DataServiceProvider
		return DataServiceProvider()
	if provider_name == "Manual Only":
		return _ManualOnlyProvider()
	raise EZCheckUnavailable(f"Unknown live_check_provider: {provider_name}")


def verify(ffl_number: str, *, force_live: bool = False) -> EZCheckResult:
	"""Cache-first lookup. force_live skips the cache hit check but still
	caches the result. Falls back to the cached row when the provider fails."""
	cache = _load_cache(ffl_number)
	if not force_live and _is_fresh(cache):
		return _result_from_cache(cache)

	try:
		provider = get_provider()
		result = provider.verify(ffl_number)
	except EZCheckUnavailable:
		if cache:
			return _result_from_cache(cache)
		raise

	if result.found:
		_upsert_cache(result)
	elif cache and not is_cr_license(cache["ffl_number"]):
		# EZ Check no longer lists this licence (lapsed / surrendered / revoked):
		# void only the expiry so atf_record_block refuses to ship to it, and
		# keep the name and address. A Type 03 is never in EZ Check, so a
		# NotFound for one says nothing — leave the hand-entered record alone.
		frappe.db.set_value("ATF FFL Record", cache["ffl_number"],
			{"expiry_date": None, "live_verified_at": None}, update_modified=False)
	# NotFound with no row on file inserts nothing (no blank rows from typos).
	return result


@frappe.whitelist()
def re_verify_all() -> dict:
	"""Re-run live EZ Check for every ATF FFL Record.

	Useful after the scraper / cache schema changes (e.g. License Type
	derivation, address column persistence) so existing rows are filled in
	without manual clicks. Skips gracefully when a row no longer exists
	upstream.

	Returns:
	    {updated, not_found, failed, total, errors: ["<record>: <error>", ...]}.
	    not_found counts rows EZ Check did not list (C&R records are left
	    as entered; a dealer's expiry is voided).
	"""
	frappe.only_for(SYSTEM_ROLES)
	codes = frappe.get_all("ATF FFL Record", pluck="name")
	updated = not_found = failed = 0
	errors = []
	for c in codes:
		try:
			found = verify(c, force_live=True).found
			# Each row is its own live lookup; keep what is done if a later one fails.
			frappe.db.commit()
			if found:
				updated += 1
			else:
				not_found += 1
		except Exception as e:
			failed += 1
			errors.append(f"{c}: {e}")
	return {"updated": updated, "not_found": not_found, "failed": failed,
		"total": len(codes), "errors": errors}


@frappe.whitelist()
def lookup_and_create(ffl_number: str) -> dict:
	"""Live-verify an FFL number via the configured EZ Check provider and
	upsert the result into ATF FFL Record. Used by the "+ Lookup by FFL #"
	button on the ATF FFL Record list view to seed a new row in one click.

	Args:
	    ffl_number: FFL number (separators tolerated; loose format check).

	Returns:
	    {ok, found, status, name, licensee_name, licensee_dba,
	    license_type, expiry_date, address, street, street2, city, state,
	    zip}.

	Raises:
	    ValidationError: The number is missing, malformed, or the provider
	        returns NotFound.
	"""
	frappe.only_for(COUNTER_ROLES)
	return _lookup_and_create(ffl_number)


def _lookup_and_create(ffl_number: str) -> dict:
	"""Gate-free body of lookup_and_create. dealer_transfer._resolve_
	destination_ffl calls THIS from the Sales Invoice / Delivery Note
	on_submit chain (which runs as the submitter — Guest for the Woo
	dispatch webhook, per test_webhook_disposal_guest_elevation), where a
	role gate would surface as a misleading 'Could not resolve FFL' block
	on a real firearm shipment. The HTTP wrapper keeps the counter gate."""
	ffl_number = (ffl_number or "").strip()
	if not ffl_number:
		frappe.throw(frappe._("FFL number is required."))
	# Loose format check — full ATF format is 1-99-999-99-9X-99999 but
	# users often paste with various separators. Accept any input with at
	# least 9 digits.
	if sum(1 for c in ffl_number if c.isdigit()) < 9:
		frappe.throw(frappe._(
			"'{0}' doesn't look like an FFL number. Expected format: 1-99-999-99-9X-99999"
		).format(ffl_number))

	try:
		result = verify(ffl_number, force_live=True)
	except EZCheckUnavailable as e:
		frappe.throw(frappe._("EZ Check provider unavailable: {0}").format(str(e)))

	if not result.found or result.status == "NotFound":
		frappe.throw(frappe._("FFL number '{0}' not found in ATF EZ Check.").format(ffl_number))

	# Pull the parsed address components from the cache row that verify()
	# just upserted — the JS caller uses these to fill Primary Address
	# fields in the Supplier Quick Entry dialog.
	addr = frappe.db.get_value(
		"ATF FFL Record", result.ffl_number,
		["street", "street2", "city", "state", "zip"],
		as_dict=True,
	) or {}

	return {
		"ok": True,
		"found": True,
		"status": result.status,
		"name": result.ffl_number,
		"licensee_name": result.licensee_name,
		"licensee_dba": result.licensee_dba,
		"license_type": result.license_type,
		"expiry_date": result.expiry_date.isoformat() if result.expiry_date else None,
		"address": result.address,
		"street": addr.get("street") or None,
		"street2": addr.get("street2") or None,
		"city": addr.get("city") or None,
		"state": addr.get("state") or None,
		"zip": addr.get("zip") or None,
	}


def _load_cache(ffl_number: str) -> dict | None:
	row = frappe.db.get_value(
		"ATF FFL Record", ffl_number,
		["ffl_number", "licensee_name", "licensee_dba", "license_type",
		 "expiry_date", "street", "street2", "city", "state", "zip",
		 "loaded_at", "live_verified_at"],
		as_dict=True,
	)
	return row or None


def _is_fresh(cache: dict | None) -> bool:
	if not cache or not cache.get("live_verified_at"):
		return False
	settings = frappe.get_single("FFL Settings")
	ttl_minutes = int(settings.live_check_cache_ttl_minutes or 1440)
	lva = cache["live_verified_at"]
	# Frappe may return a datetime or a string; normalise to datetime.
	if isinstance(lva, str):
		lva = datetime.fromisoformat(lva)
	delta = now_datetime() - lva
	return delta < timedelta(minutes=ttl_minutes)


def _result_from_cache(cache: dict) -> EZCheckResult:
	address = ", ".join(p for p in (cache.get("street"), cache.get("street2"),
		cache.get("city"), cache.get("state"), cache.get("zip")) if p)
	expiry = cache.get("expiry_date")
	# `getdate()` (the SITE's today), not `date.today()` (the host's). A licence
	# is valid through its expiry date in the shop's calendar, and on a UTC
	# container serving a US store the host rolls over hours early — so from
	# roughly 6pm onward a partner FFL expiring today was reported Expired while
	# the shop still had a day of validity left. Wrong in the safe direction
	# (a false block, never a false transfer), but wrong on the compliance face.
	#
	# `getdate(expiry)` on the left for the same reason `_is_fresh` normalises
	# `live_verified_at`: Frappe hands a Date column back as a `date` today, but
	# a string down any other read path would make this comparison raise
	# TypeError instead of answering. On a compliance check, normalising both
	# sides costs nothing.
	status = "Expired" if expiry and getdate(expiry) < getdate() else "Verified"
	return EZCheckResult(
		ffl_number=cache["ffl_number"],
		found=True,
		status=status,
		licensee_name=cache.get("licensee_name"),
		licensee_dba=cache.get("licensee_dba"),
		license_type=cache.get("license_type"),
		expiry_date=expiry,
		address=address or None,
	)


def _upsert_cache(result: EZCheckResult) -> None:
	# Prefer the provider's structured address (the Data Service parses it
	# server-side with the same splitter); fall back to splitting the flat
	# string for providers that only return one line.
	if result.street or result.city or result.zip:
		address_parts = {
			"street": result.street,
			"street2": result.street2,
			"city": result.city,
			"state": result.state,
			"zip": result.zip,
		}
	else:
		address_parts = _parse_address(result.address) if result.address else {}
	values = {
		"licensee_name": result.licensee_name or "",
		"licensee_dba": result.licensee_dba or "",
		"license_type": result.license_type or "",
		"expiry_date": result.expiry_date,
		"street": address_parts.get("street") or "",
		"street2": address_parts.get("street2") or "",
		"city": address_parts.get("city") or "",
		"state": address_parts.get("state") or "",
		"zip": address_parts.get("zip") or "",
		"loaded_from": "ez-check-live",
		"loaded_at": now_datetime(),
		"live_verified_at": now_datetime(),
	}
	if frappe.db.exists("ATF FFL Record", result.ffl_number):
		frappe.db.set_value("ATF FFL Record", result.ffl_number, values, update_modified=False)
	else:
		try:
			frappe.get_doc({
				"doctype": "ATF FFL Record",
				"ffl_number": result.ffl_number,
				**values,
			}).insert(ignore_permissions=True)
		except frappe.exceptions.DuplicateEntryError:
			# Concurrent insert raced ahead; fall back to update.
			frappe.db.set_value("ATF FFL Record", result.ffl_number, values, update_modified=False)
	frappe.db.commit()


class _ManualOnlyProvider:
	name = "Manual Only"
	def verify(self, ffl_number: str) -> EZCheckResult:
		raise EZCheckUnavailable("Manual Only mode — live verification disabled.")


# Common US street-type tokens — used as the boundary between street and city
# in ATF address parsing. ATF's space-separated format ("100 N MAGNOLIA AVE
# HUBBARD TX - 76648") doesn't put commas between components, so we split on
# whichever street-type token appears last.
_STREET_TYPES = {
	"AVE", "AVENUE", "ST", "STREET", "BLVD", "BOULEVARD",
	"RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
	"WAY", "CT", "COURT", "PL", "PLACE",
	"PKWY", "PARKWAY", "HWY", "HIGHWAY", "FM",
	"TER", "TERRACE", "CIR", "CIRCLE", "TRL", "TRAIL",
	"SQ", "SQUARE", "ALY", "ALLEY",
}

# USPS secondary-unit designators. In ATF's space-separated format a unit sits
# between the street and the city ("100 MAIN ST UNIT 103 SPRINGFIELD NY -
# 10001"); the unit belongs on its own line (street2), not swept into the city.
_SECONDARY_UNIT_DESIGNATORS = {
	"APT", "BLDG", "BUILDING", "DEPT", "FL", "FLOOR", "HNGR", "LOT",
	"OFC", "OFFICE", "PH", "RM", "ROOM", "SLIP", "SPC", "SPACE", "STE",
	"SUITE", "TRLR", "UNIT", "#",
}


def _looks_like_unit_id(tok: str) -> bool:
	"""A unit identifier — '103', '5', 'A', 'B12'. Has a digit, or is a 1-2 char
	code. Tells 'UNIT 103' (a real unit) apart from a city word like 'SPRINGFIELD'."""
	t = tok.rstrip(",.")
	return any(ch.isdigit() for ch in t) or (len(t) <= 2 and t.isalnum())


def _parse_address(addr: str) -> dict:
	"""ATF address 'STREET CITY ST - ZIP' → {street, city, state, zip}.

	Falls back to {street: addr} when the format doesn't match (so the
	full string is still preserved). Cities can be multi-word, so we
	split on the last street-type token rather than counting words.
	"""
	if not addr:
		return {}
	addr = addr.strip()
	zip_split = addr.rsplit(" - ", 1)
	if len(zip_split) != 2 or not zip_split[1].strip().split()[0][:5].isdigit():
		return {"street": addr}
	zip_code = zip_split[1].strip().split()[0]
	rest = zip_split[0].rstrip().rstrip(",").rstrip()

	tokens = rest.split()
	if len(tokens) < 3:
		return {"street": addr}
	state = tokens[-1]
	if len(state) != 2 or not state.isalpha():
		return {"street": addr}
	body = tokens[:-1]  # everything before the state

	# Find the LAST street-type token; everything after it (and before
	# the state) is the city. If no street-type token is found, fall
	# back to "last word = city".
	split_idx = None
	for i, tok in enumerate(body):
		if tok.upper().rstrip(",.") in _STREET_TYPES:
			split_idx = i + 1
	if split_idx is None or split_idx >= len(body):
		split_idx = max(1, len(body) - 1)
	# Texas Farm-to-Market roads: "2721 FM 936 HUBBARD TX". The "FM" is
	# in our street-type set, but "936" right after is the road number,
	# not the city. Bump the split past pure-digit tokens that sit
	# between the street-type and a real word.
	while split_idx < len(body) - 1 and body[split_idx].isdigit():
		split_idx += 1

	street_tokens = body[:split_idx]
	city_tokens = body[split_idx:]

	# Peel a leading secondary-unit designator (UNIT 103, STE 5, #2 …) off the
	# city into street2 — so the unit gets its own line and the city stays clean.
	street2 = ""
	if city_tokens:
		head = city_tokens[0].rstrip(",.")
		head_has_digit = any(ch.isdigit() for ch in head)
		is_designator = head.upper() in _SECONDARY_UNIT_DESIGNATORS or head.startswith("#")
		next_is_id = len(city_tokens) > 1 and _looks_like_unit_id(city_tokens[1])
		# Split only when an actual unit identifier is present — embedded in the
		# token ("#2") or right after it ("UNIT 103"). A bare designator word
		# followed by a city word ("KEY WEST") is a city, not a unit.
		if is_designator and (head_has_digit or next_is_id):
			unit = [city_tokens[0]]
			tail = city_tokens[1:]
			# A bare keyword ("UNIT", "STE", "#") takes the following identifier;
			# a self-contained token ("#2") already carries it.
			if not head_has_digit and next_is_id:
				unit.append(tail[0])
				tail = tail[1:]
			street2 = " ".join(unit).rstrip(",").strip()
			city_tokens = tail

	street = " ".join(street_tokens).rstrip(",").strip()
	city = " ".join(city_tokens).rstrip(",").strip()
	out = {
		"street": street or rest,
		"city": city,
		"state": state.upper(),
		"zip": zip_code,
	}
	if street2:
		out["street2"] = street2
	return out
