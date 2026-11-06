"""ATFFormScraperProvider — scrapes the public fflezcheck.atf.gov form.

Public form, no CSRF, intended for FFL holders to verify trade partners.
Rate-limited per FFL Settings.live_check_rate_limit_per_hour.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock
from typing import ClassVar

import frappe
import requests
from bs4 import BeautifulSoup
from frappe.utils import getdate

from ffl_integrations.atf.ez_check import EZCheckResult, EZCheckUnavailable


ATF_URL = "https://fflezcheck.atf.gov/FFLEzCheck/fflSearch"
USER_AGENT = "Mozilla/5.0 (compatible; GunStorePOS/1.0)"


class _RateBucket:
	"""Process-local sliding-window rate limiter (per-hour). Not cluster-safe;
	for sites with multiple workers, a Redis-backed limiter is a follow-up."""

	def __init__(self) -> None:
		self._timestamps: deque[float] = deque()
		self._lock = Lock()

	def take(self, max_per_hour: int) -> bool:
		"""Returns True when call is allowed, False when rate-limited."""
		now = time.time()
		one_hour_ago = now - 3600
		with self._lock:
			while self._timestamps and self._timestamps[0] < one_hour_ago:
				self._timestamps.popleft()
			if len(self._timestamps) >= max_per_hour:
				return False
			self._timestamps.append(now)
			return True


_BUCKET = _RateBucket()


@dataclass
class ATFFormScraperProvider:
	name: ClassVar[str] = "ATF Form Scraper"

	def verify(self, ffl_number: str) -> EZCheckResult:
		settings = frappe.get_single("FFL Settings")
		max_per_hour = int(settings.live_check_rate_limit_per_hour or 30)
		if not _BUCKET.take(max_per_hour):
			raise EZCheckUnavailable("Local rate limit exceeded")

		regn, dist, seqn = _parse_ffl(ffl_number)
		try:
			response = requests.post(
				ATF_URL,
				data={"licsRegn": regn, "licsDis": dist, "licsSeq": seqn},
				headers={"User-Agent": USER_AGENT},
				timeout=15,
			)
		except requests.RequestException as exc:
			raise EZCheckUnavailable(f"HTTP error: {exc}") from exc

		if response.status_code >= 500:
			raise EZCheckUnavailable(f"ATF returned {response.status_code}")
		if response.status_code != 200:
			raise EZCheckUnavailable(f"Unexpected status {response.status_code}")

		try:
			# The SITE's today, not the host's — `_parse` compares the scraped
			# expiry against it, and on a UTC container serving a US store the
			# host's date runs ahead from early evening onward, expiring a partner
			# FFL a day early. Same reading `ez_check._result_from_cache` takes, so
			# the live path and the cached path cannot disagree about what day it
			# is. `_parse` still takes `today` as a parameter: it stays pure and
			# the expiry-boundary tests keep pinning the date they choose.
			return self._parse(response.text, ffl_number=ffl_number, today=getdate())
		except Exception as exc:
			raise EZCheckUnavailable(f"Parser error: {exc}") from exc

	@staticmethod
	def _parse(html: str, *, ffl_number: str, today: date) -> EZCheckResult:
		soup = BeautifulSoup(html, "html.parser")
		text = soup.get_text(" ", strip=True)

		# ATF's real not-found page: "You Entered FFL Number ... invalid and/or ...".
		# Anything else with no license fields is a page we cannot read (a
		# maintenance or blocking page served as 200), NOT a verdict on the FFL.
		lowered = text.lower()
		if "you entered ffl number" in lowered and "invalid and/or" in lowered:
			return EZCheckResult(ffl_number=ffl_number, found=False, status="NotFound")

		licensee_name = _field(text, "License Name")
		licensee_dba = _field(text, "Trade Name")
		address = _field(text, "Premise Address")
		expiry = _parse_date(_field(text, "Expiration Date"))
		# ATF EZ Check's public form doesn't return License Type — but it's
		# encoded as segment 4 of the FFL number itself.
		license_type = _license_type_from_number(ffl_number)

		if not licensee_name and not expiry:
			# verify() turns this into EZCheckUnavailable: the record stays as it was.
			raise ValueError("unrecognized EZ Check page: no license fields and no not-found marker")

		status = "Expired" if (expiry and expiry < today) else "Verified"
		return EZCheckResult(
			ffl_number=ffl_number,
			found=True,
			status=status,
			licensee_name=licensee_name,
			licensee_dba=licensee_dba,
			license_type=license_type,
			expiry_date=expiry,
			address=address,
		)


def _parse_ffl(ffl_number: str) -> tuple[str, str, str]:
	"""15-char FFL '1-99-888-01-XX-12345' → ('1', '99', '12345')."""
	parts = (ffl_number or "").strip().split("-")
	if len(parts) != 6:
		raise EZCheckUnavailable(f"Malformed FFL number: {ffl_number!r}")
	return parts[0], parts[1], parts[5]


# Known ATF EZ Check field labels — used as stop-markers so multi-word values
# (e.g. "SMITH JOHN") are not truncated by the next uppercase word in the value.
_ATF_LABELS = [
	"License Number", "Expiration Date", "License Name", "Trade Name",
	"Premise Address", "Mailing Address", "LOA Issue Date", "LOA Expiration Date",
]


# 18 USC 923 license types, keyed by the 2-digit code in FFL segment 4.
_LICENSE_TYPE_LABELS = {
	"01": "01 - Dealer in Firearms Other Than Destructive Devices",
	"02": "02 - Pawnbroker in Firearms Other Than Destructive Devices",
	"03": "03 - Collector of Curios and Relics",
	"06": "06 - Manufacturer of Ammunition for Firearms",
	"07": "07 - Manufacturer of Firearms Other Than Destructive Devices",
	"08": "08 - Importer of Firearms Other Than Destructive Devices",
	"09": "09 - Dealer in Destructive Devices",
	"10": "10 - Manufacturer of Destructive Devices",
	"11": "11 - Importer of Destructive Devices",
}


def _license_type_from_number(ffl_number: str) -> str | None:
	"""FFL number 'R-DD-CCC-TT-CK-SSSSS' → 'TT - Description'.

	Segment 4 (TT) is the ATF license type code; the public EZ Check
	form omits this from its response so we derive it from the number
	itself. Returns None for malformed numbers or unknown codes.
	"""
	parts = (ffl_number or "").strip().split("-")
	if len(parts) != 6:
		return None
	code = parts[3].strip().upper()
	return _LICENSE_TYPE_LABELS.get(code)
_STOPPERS = "|".join(re.escape(l) for l in _ATF_LABELS)


def _field(text: str, label: str) -> str | None:
	"""Pull '{label}: value' out of flattened response text. Stops at the
	next known ATF label so multi-word values are captured in full.

	Critical: do NOT consume the whitespace AFTER the colon — the stop
	lookahead anchors on `\\s+stopper`, and if we eat the leading
	whitespace before the lookahead runs, the anchor fails and the lazy
	`.*?` spills forward into the next field's body. That bug surfaced
	when Trade Name is empty: 'Trade Name : Premise Address : 100 N...'
	→ Trade Name captured 'Premise Address : 100 N...'.
	"""
	pattern = rf"{re.escape(label)}\s*:(.*?)(?=\s+(?:{_STOPPERS})\s*:|$)"
	match = re.search(pattern, text)
	if not match:
		return None
	value = match.group(1).strip()
	return value or None


def _parse_date(s: str | None) -> date | None:
	if not s:
		return None
	for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
		try:
			return datetime.strptime(s, fmt).date()
		except ValueError:
			continue
	return None
