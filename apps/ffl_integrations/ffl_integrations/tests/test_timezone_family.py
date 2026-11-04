"""The HOST's clock standing in for the SITE's, and the family of
places that made the same mistake.

One root: `datetime.astimezone()` with no argument converts to the *operating
system's* zone. Written on a laptop where the OS and the site agree it looks
correct; deployed in a container (OS on UTC, site on a US zone) it shifts every
converted instant by a full UTC offset — and, for the heartbeats, in the
direction that makes a stopped feed read as a live one. QA measured an age of
**-12244 s** for a heartbeat that was really **2155 s** old.

The reason this warranted its own run rather than a one-line patch is what sits
downstream of the arithmetic: `tasks.check_feed_staleness` runs every 15 minutes
and, for a distributor that opted in, pushes its published quantities to zero
when the feed goes stale, precisely so an outage cannot keep selling stock we
can no longer verify. Understating the age by five hours against a 90-minute
threshold delayed that interlock by about five and a half hours.

These tests are written against `tz_fixture.site_tz`, which forces the site and
the host onto DIFFERENT zones — the case that hid the bug. On a machine where
they happen to agree, none of this can fail, which is why the fixture never lets
them agree.

Site-free by construction (plain `unittest.TestCase`, no DB): the conversion is
a pure function of the wire string and one System Settings value, so it should
be provable without a bench.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import pathlib
import unittest
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar
from unittest import mock
from zoneinfo import ZoneInfo

import frappe
import frappe.utils

from ffl_integrations.atf import ez_check, scraper
from ffl_integrations.distributor import remote_catalog, tasks
from ffl_integrations.distributor import util as dist_util
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests.tz_fixture import site_tz

# The exact wire value, site zone and clock reading from the QA report, so the
# numbers below can be checked against it line by line.
QA_WIRE = "2026-08-05T22:17:29Z"
QA_SITE = "America/New_York"          # EDT in August → UTC-4
QA_HOST = "UTC"                       # what the container actually runs on
QA_NOW = datetime(2026, 8, 5, 18, 53, 24)   # `now_datetime()`, i.e. site-local
QA_TRUE_AGE = 2155.0                  # 22:17:29Z is 18:17:29 in New York
QA_BUGGY_AGE = -12245.0               # what the old body produced (QA saw -12244)


class TestParseServiceTsLandsOnTheSitesClock(unittest.TestCase):
	"""The root. Everything else in the family is this function's blast radius."""

	def test_the_qa_measurement_is_reproduced_and_corrected(self):
		"""The headline number, end to end.

		Asserting the buggy value too is deliberate: it proves the fixture really
		does reproduce the reported conditions, so the green result above it means
		"fixed" rather than "never reachable here".
		"""
		with site_tz(QA_SITE, os_tz=QA_HOST):
			heartbeat = remote_catalog.parse_service_ts(QA_WIRE)
		age = (QA_NOW - heartbeat).total_seconds()
		self.assertAlmostEqual(age, QA_TRUE_AGE, places=3)
		self.assertNotAlmostEqual(age, QA_BUGGY_AGE, places=0)
		self.assertGreater(age, 0, "an age that goes negative is the bug's signature")

	def test_the_host_timezone_does_not_move_the_answer(self):
		"""Same instant, same site, three hosts. The host is not an input."""
		answers = set()
		for host in ("UTC", "Asia/Tokyo", "Pacific/Auckland"):
			with self.subTest(host=host), site_tz(QA_SITE, os_tz=host):
				answers.add(remote_catalog.parse_service_ts(QA_WIRE))
		self.assertEqual(answers, {datetime(2026, 8, 5, 18, 17, 29)})

	def test_the_site_timezone_does_move_it(self):
		"""The mirror. Without it, hard-coding one constant would pass the test
		above — and prod is on Chicago, not New York."""
		with site_tz("America/Chicago", os_tz="UTC"):     # CDT → UTC-5
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 17, 17, 29))
		with site_tz("UTC", os_tz="Asia/Tokyo"):
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 22, 17, 29))

	def test_the_offset_follows_dst_rather_than_being_frozen(self):
		"""Both halves of the year on one site. A fixed-offset shortcut — the
		obvious cheap fix — gets one of these wrong, and would do it silently for
		half of every year."""
		with site_tz(QA_SITE, os_tz="UTC"):
			self.assertEqual(remote_catalog.parse_service_ts("2026-01-15T22:17:29Z"),
				datetime(2026, 1, 15, 17, 17, 29))   # EST, UTC-5
			self.assertEqual(remote_catalog.parse_service_ts("2026-08-05T22:17:29Z"),
				datetime(2026, 8, 5, 18, 17, 29))    # EDT, UTC-4

	def test_the_result_is_naive_because_its_consumers_are(self):
		"""`feed_stale_verdict` subtracts this from `now_datetime()`, which is
		naive; handing back an aware datetime would raise inside the `*/15` cron
		instead of returning a wrong number — a different, louder bug, but still
		one this function must not create."""
		with site_tz(QA_SITE, os_tz="UTC"):
			self.assertIsNone(remote_catalog.parse_service_ts(QA_WIRE).tzinfo)

	def test_all_three_utc_spellings_land_on_one_instant(self):
		"""The service spells UTC three ways across its examples. The naive spelling is
		the one that changed: it used to be passed through as though it were
		already site-local, which moved the instant by an offset."""
		with site_tz(QA_SITE, os_tz="UTC"):
			answers = {remote_catalog.parse_service_ts(v) for v in (
				"2026-08-05T22:17:29Z",
				"2026-08-05T22:17:29+00:00",
				"2026-08-05T22:17:29")}
		self.assertEqual(answers, {datetime(2026, 8, 5, 18, 17, 29)})

	def test_a_non_utc_offset_on_the_wire_is_honoured(self):
		"""We normalise "Z", we do not assume it: an offset-bearing value names
		its own instant and that instant is what gets converted."""
		with site_tz(QA_SITE, os_tz="UTC"):
			self.assertEqual(remote_catalog.parse_service_ts("2026-08-05T22:17:29+02:00"),
				datetime(2026, 8, 5, 16, 17, 29))    # 20:17:29Z → 16:17:29 EDT

	def test_none_and_blank_stay_none(self):
		"""A distributor that has never synced has no heartbeat, and `None` is how
		`feed_stale_verdict` recognises `never_synced` — which alerts but must not
		zero stock."""
		with site_tz(QA_SITE, os_tz="UTC"):
			self.assertIsNone(remote_catalog.parse_service_ts(None))
			self.assertIsNone(remote_catalog.parse_service_ts(""))

	def test_an_unresolvable_site_zone_still_lands_on_now_datetimes_clock(self):
		"""A typo in System Settings must not send us back to the host's zone.

		UTC is the obvious fallback and the wrong one: UTC *is* the host zone on
		every container we deploy, so "fall back to UTC" would reinstate exactly
		the skew being removed, in exactly the deployment where it bites. The
		fallback instead measures the offset against `now_datetime()` — the clock
		the callers compare against — so the contract holds even when the zone
		name does not resolve.
		"""
		def site_clock_four_hours_behind_utc():
			"""What `now_datetime()` answers on a New York site, expressed without
			naming a zone — the fallback has no resolvable zone to work from."""
			return datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4)

		with site_tz("Not/AZone", os_tz="UTC"), mock.patch.object(
				frappe.utils, "now_datetime", site_clock_four_hours_behind_utc):
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 18, 17, 29))

	def test_the_fallback_is_not_silently_doing_the_work(self):
		"""The test above proves the fallback works; this proves it is a FALLBACK.
		With a resolvable zone the clock must be irrelevant — otherwise a broken
		`now_datetime()` would be quietly steering every conversion."""
		def a_clock_nobody_should_read():
			raise AssertionError("the resolvable-zone path must not read the clock")

		with site_tz(QA_SITE, os_tz="UTC"), mock.patch.object(
				frappe.utils, "now_datetime", a_clock_nobody_should_read):
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 18, 17, 29))

	def test_the_zone_is_read_through_the_shared_helper_not_a_fourth_copy(self):
		"""Reuse, pinned. `console.py`, `catalog_service.py` and this function all
		answer "what zone is the site on?" through `distributor.util`; a private
		copy here is how the two halves of the page drifted apart in the first
		place."""
		with mock.patch.object(dist_util, "site_timezone",
				return_value="America/Chicago") as spy, site_tz(QA_SITE, os_tz="UTC"):
			# The fixture says New York, the helper says Chicago — if the function
			# reads the helper (it must), Chicago wins.
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 17, 17, 29))
		spy.assert_called()


class TestTheFallbackSaysSoWhenItIsCarryingTheConversion(unittest.TestCase):
	"""The fallback works well enough that nothing about
	the site looks wrong — which is exactly why it has to leave a trace.

	The design question ("keep the derived-offset fallback, or raise?") was ruled
	in favour of keeping it, on the condition that being on it is observable.
	Silent correctness is how a config fault survives for months.
	"""

	def setUp(self):
		frappe.cache().delete_value(remote_catalog._ZONE_WARNING_KEY)

	tearDown = setUp

	def test_being_on_the_fallback_is_logged(self):
		with site_tz("Not/AZone", os_tz="UTC"), mock.patch(
				"ffl_integrations.notifications.safe_log_error") as log:
			remote_catalog.parse_service_ts(QA_WIRE)
		log.assert_called_once()
		title, message = log.call_args[0]
		self.assertIn("timezone", title.lower())
		# The message has to name the bad value and the thing to fix, or whoever
		# reads it still has to go source-diving to act on it.
		self.assertIn("Not/AZone", message)
		self.assertIn("System Settings", message)

	def test_it_is_said_once_not_once_per_timestamp(self):
		"""The `*/15` watchdog converts several heartbeats a tick and the page
		refreshes every 60s. An un-throttled warning would bury the Error Log in
		copies of one standing fact, which gets read as noise — the same outcome
		as staying silent, plus the cost."""
		with site_tz("Not/AZone", os_tz="UTC"), mock.patch(
				"ffl_integrations.notifications.safe_log_error") as log:
			for _ in range(5):
				remote_catalog.parse_service_ts(QA_WIRE)
		log.assert_called_once()

	def test_a_resolvable_zone_says_nothing_at_all(self):
		"""The normal path must stay quiet, or the warning means nothing."""
		with site_tz(QA_SITE, os_tz="UTC"), mock.patch(
				"ffl_integrations.notifications.safe_log_error") as log:
			remote_catalog.parse_service_ts(QA_WIRE)
		log.assert_not_called()

	def test_a_broken_logger_does_not_break_the_conversion(self):
		"""Observability must not become a new way for the `*/15` cron to fail.
		The heartbeat still has to come back correct."""
		with site_tz("Not/AZone", os_tz="UTC"), mock.patch(
				"ffl_integrations.notifications.safe_log_error",
				side_effect=RuntimeError("Error Log is itself broken")), \
				mock.patch.object(frappe.utils, "now_datetime",
					lambda: datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4)):
			self.assertEqual(remote_catalog.parse_service_ts(QA_WIRE),
				datetime(2026, 8, 5, 18, 17, 29))


@contextlib.contextmanager
def _site_is_a_day_behind_the_host(module):
	"""Put `module`'s idea of "today" one day BEHIND the host's, and yield it.

	This is the compliance half of the family, and it needs a different fixture
	from the rest of this file. `site_tz()` moves what `dist_util.site_timezone()`
	reports, which is enough for the heartbeat conversion because that code reads
	the zone through `dist_util`. It is NOT enough here, and a test built on it
	would be permanently green for two different reasons depending on where it
	runs — which is worse than no test at all:

	* in the site-free harness `now_datetime()` is `datetime.now()`, the HOST's
	  clock, so `getdate()` follows the host TZ and the two dates never diverge;
	* on a real bench `frappe.utils.data.get_datetime` resolves `now_datetime`
	  through its OWN module global, so patching `frappe.utils.get_system_timezone`
	  does not reach it either.

	So we inject at the seam the code under test actually reads — the module-level
	`getdate` binding — and we derive the injected date FROM the host's, one day
	back. That is exactly the production condition: a UTC container has already
	rolled over midnight while the US site it serves has not. Deriving rather than
	hard-coding also means the divergence exists at every real instant, so the
	test cannot drift into a same-date state and quietly stop testing anything.

	Asserts the divergence it just created, because a fixture that silently failed
	to diverge is the exact failure mode being guarded against.
	"""
	host_today = date.today()
	site_today = host_today - timedelta(days=1)
	assert site_today != host_today, "the fixture did not create the divergence"
	# Identity check: it must be Frappe's site-aware helper we are standing in
	# for, not some local date function that would make this proof vacuous.
	assert module.getdate is frappe.utils.getdate, (
		f"{module.__name__}.getdate is not frappe.utils.getdate — this fixture "
		"proves the site's date is consulted, which means nothing if the name it "
		"replaces was never the site's date to begin with")

	def fake_getdate(value=None, *a, **kw):
		# With no argument `getdate()` means "the site's today"; with one it is
		# just type normalisation and must stay real.
		return site_today if value is None else frappe.utils.getdate(value, *a, **kw)

	with mock.patch.object(module, "getdate", fake_getdate):
		yield site_today, host_today


class TestFflExpiryIsJudgedOnTheShopsCalendar(unittest.TestCase):
	"""The compliance face of that family.

	`atf/ez_check._result_from_cache` and `atf/scraper.verify` decide whether a
	partner FFL is still valid. Both compared the licence's expiry date against
	the HOST's `date.today()`. On a UTC container serving a US shop the host has
	already rolled over from early evening onward, so for those hours a licence
	that is valid *through today* in the shop's calendar was reported `Expired`.

	The direction is the safe one — a false block, never a false transfer — but
	it is still a wrong answer on a federal compliance check, and the live path
	(scraper) and the cached path (ez_check) could hand back different verdicts
	for the same licence in the same minute.

	The AST sweep can only stop `date.today()` from reappearing. It cannot see
	"the right helper called with the wrong day", so the semantics get pinned
	here. Each test states what the OLD code answered, and the adversarial re-run
	confirms it: restore `date.today()` and the two `_is_verified_*` cases fail.
	"""

	FFL = "1-99-888-01-XX-12345"

	# ---------------------------------------------------------------- cached path

	def _cache_row(self, expiry):
		return {"ffl_number": self.FFL, "licensee_name": "SMITH JOHN",
			"expiry_date": expiry, "city": "ANYTOWN", "state": "TX"}

	def test_a_licence_expiring_on_the_shops_today_is_verified_not_expired(self):
		"""THE case. Host has ticked over to tomorrow, shop has not, licence runs
		to the end of the shop's today. Old code: `Expired`."""
		with _site_is_a_day_behind_the_host(ez_check) as (site_today, host_today):
			result = ez_check._result_from_cache(self._cache_row(site_today))
			self.assertEqual(result.status, "Verified",
				f"licence expires {site_today} (the shop's today); the host thinks "
				f"it is already {host_today}, which is not the shop's business")

	def test_a_licence_that_expired_before_the_shops_today_is_still_expired(self):
		"""The mirror. Without it, "always return Verified" would pass the test
		above — and on this face that would be the dangerous direction."""
		with _site_is_a_day_behind_the_host(ez_check) as (site_today, _host):
			result = ez_check._result_from_cache(
				self._cache_row(site_today - timedelta(days=1)))
			self.assertEqual(result.status, "Expired")

	def test_a_licence_expiring_on_the_hosts_today_is_verified_too(self):
		"""A day of headroom either way, so the boundary is pinned on both sides
		rather than at a single point."""
		with _site_is_a_day_behind_the_host(ez_check) as (_site, host_today):
			self.assertEqual(
				ez_check._result_from_cache(self._cache_row(host_today)).status,
				"Verified")

	def test_a_string_expiry_is_normalised_rather_than_raising(self):
		"""Frappe hands a Date column back as `date` today, but a string down any
		other read path used to make this comparison raise TypeError instead of
		answering — on a compliance check, mid-verification."""
		with _site_is_a_day_behind_the_host(ez_check) as (site_today, _host):
			self.assertEqual(
				ez_check._result_from_cache(
					self._cache_row(site_today.isoformat())).status,
				"Verified")
			self.assertEqual(
				ez_check._result_from_cache(
					self._cache_row((site_today - timedelta(days=1)).isoformat())).status,
				"Expired")

	# ------------------------------------------------------------------ live path

	@staticmethod
	def _atf_html(expiry) -> str:
		"""The real EZ Check fixture with only its expiry moved, so the parser is
		still exercised on the markup ATF actually returns."""
		here = pathlib.Path(scraper.__file__).parent.parent / "tests" / "fixtures"
		html = (here / "ez_check_verified.html").read_text(encoding="utf-8")
		return html.replace("12/31/2028", expiry.strftime("%m/%d/%Y"))

	@contextlib.contextmanager
	def _atf_answers(self, expiry):
		"""`verify()` with its IO patched: settings read, rate limiter and the
		outbound POST. No network, and no `_parse` spy — the assertion is on the
		VERDICT `verify()` returns, which is what a compliance decision consumes.
		"""
		response = mock.Mock(status_code=200, text=self._atf_html(expiry))
		with mock.patch.object(scraper.frappe, "get_single", return_value=mock.Mock(
					live_check_rate_limit_per_hour=100000)), \
				mock.patch.object(scraper.requests, "post", return_value=response):
			yield

	def test_the_live_check_reads_the_same_today_as_the_cached_one(self):
		"""Both paths, one licence, one minute: they must not disagree. This is
		the half that made the defect worse than a day-boundary rounding error."""
		with _site_is_a_day_behind_the_host(scraper) as (site_today, _host), \
				self._atf_answers(site_today):
			live = scraper.ATFFormScraperProvider().verify(self.FFL)
		self.assertEqual(live.status, "Verified")
		self.assertEqual(live.expiry_date, site_today)

		with _site_is_a_day_behind_the_host(ez_check) as (site_today2, _h):
			cached = ez_check._result_from_cache(self._cache_row(site_today2))
		self.assertEqual(live.status, cached.status)

	def test_the_live_check_still_calls_a_genuinely_lapsed_licence_expired(self):
		"""The live path's mirror."""
		with _site_is_a_day_behind_the_host(scraper) as (site_today, _host), \
				self._atf_answers(site_today - timedelta(days=1)):
			self.assertEqual(
				scraper.ATFFormScraperProvider().verify(self.FFL).status, "Expired")

	def test_the_pure_parser_keeps_taking_today_as_a_parameter(self):
		"""`_parse` stays pure — the date is injected, never read inside. That is
		what lets the existing boundary tests pin a date of their choosing, and it
		is the property that keeps this defect confined to the two call sites."""
		with _site_is_a_day_behind_the_host(scraper) as (site_today, host_today):
			verdict = scraper.ATFFormScraperProvider._parse(
				self._atf_html(site_today), ffl_number=self.FFL, today=host_today)
		# Same licence, same instant, caller-supplied host date → the old answer.
		# Proof that the fix lives in WHICH date the caller passes, not in `_parse`.
		self.assertEqual(verdict.status, "Expired")


# The four ways to ask the HOST what time or zone it is on. Each is correct
# Python and wrong here, because a Frappe site carries its own `time_zone` and
# every container we deploy runs on UTC regardless of where the shop is.
_HOST_CLOCK_CALLS = {
	"astimezone": "converts to the host's zone — pass a zone, e.g. "
		"`zone_info(site_timezone())`",
	"now": "reads the host's clock — use `frappe.utils.now_datetime()`, or pass a "
		"zone when the zone is deliberately not the site's",
	"today": "reads the host's date — use `frappe.utils.getdate()`",
	"utcnow": "naive UTC invites exactly this class of mistake — use an aware "
		"`datetime.now(UTC)`",
}
# An escape hatch for a call site where the host's clock genuinely IS the right
# answer. Requiring the reason on the line means the next reader gets it without
# a git blame — and makes every exemption visible to this same scan.
_EXEMPT_MARKER = "tz-exempt:"


def _host_clock_call_lines(source: str) -> list[tuple[int, str]]:
	"""PURE: `(lineno, attr)` for every no-argument host-clock call in `source`.

	Parses rather than greps. The first cut of this guard was a regex and it fired
	on the docstrings *explaining the bug* — prose that should absolutely keep
	naming the wrong idiom. An AST sees code and only code, so the explanation and
	the offence stop being the same string.
	"""
	found = []
	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.Call) or node.args or node.keywords:
			continue      # `astimezone(zone)` / `datetime.now(ZoneInfo(...))` are fine
		func = node.func
		if isinstance(func, ast.Attribute) and func.attr in _HOST_CLOCK_CALLS:
			found.append((node.lineno, func.attr))
	# `ast.walk` is breadth-first, so sort: an offender list is read top-to-bottom
	# against the file it names.
	return sorted(found)


class TestNoHostClockCallSurvivesInOurApps(unittest.TestCase):
	"""Acceptance ③, made permanent instead of a one-off sweep.

	The family existed because one wrong idiom got copied from file to file. A
	report saying "I swept it, it's clean" is true on the day it is written and
	unenforceable the day after; a test keeps being true. Scans only OUR three
	app packages — `frappe` and `erpnext` are read-only to us and what they do
	with their own clocks is not ours to police.
	"""

	def _our_app_roots(self):
		roots = []
		for name in ("ffl_core", "ffl_integrations", "ffl_woo_sync"):
			try:
				module = importlib.import_module(name)
			except ImportError:      # an app not installed on this bench
				continue
			roots.append(pathlib.Path(module.__path__[0]))
		return roots

	def test_no_production_module_asks_the_host_for_a_site_fact(self):
		roots = self._our_app_roots()
		self.assertTrue(roots, "found none of our apps — the scan would pass vacuously")
		offenders = []
		for root in roots:
			for path in sorted(root.rglob("*.py")):
				# Tests are exempt wholesale: several of them reproduce the old
				# idiom on purpose, to prove it wrong.
				if "tests" in path.parts or path.name.startswith("test_"):
					continue
				lines = path.read_text(encoding="utf-8").splitlines()
				for lineno, attr in _host_clock_call_lines("\n".join(lines)):
					if _EXEMPT_MARKER in lines[lineno - 1]:
						continue
					offenders.append(f"{path}:{lineno}: .{attr}() — "
						f"{_HOST_CLOCK_CALLS[attr]}")
		self.assertEqual(offenders, [], "\n".join([
			"", "a host-clock call reached production code (the whole family).",
			f"If the host really is the right clock here, say why with a "
			f"`# {_EXEMPT_MARKER} …` note on the line.", *offenders]))

	def test_the_scanner_catches_the_line_that_was_removed(self):
		"""A guard that cannot fail is decoration. First case is verbatim the line
		deleted from `parse_service_ts`."""
		caught = _host_clock_call_lines("\n".join([
			"dt = dt.astimezone().replace(tzinfo=None)",
			"stub = datetime.now().strftime('%Y%m%d')",
			"today = date.today()",
			"t = datetime.utcnow()",
		]))
		self.assertEqual([attr for _lineno, attr in caught],
			["astimezone", "now", "today", "utcnow"])

	def test_the_scanner_leaves_the_correct_forms_alone(self):
		"""The zoned calls are the FIX, so a guard that flagged them would push
		people back toward the bug."""
		self.assertEqual(_host_clock_call_lines("\n".join([
			"moment.astimezone(zone).replace(tzinfo=None)",
			"moment.astimezone(UTC)",
			"datetime.now(ZoneInfo(MAINTENANCE_TZ))",
			"frappe.utils.now_datetime()",
			"time.time()",
		])), [])

	def test_an_exempted_line_is_forgiven_only_with_its_reason_attached(self):
		source = "x = dt.astimezone()   # tz-exempt: the host IS the right clock here"
		self.assertEqual(len(_host_clock_call_lines(source)), 1,
			"the scanner still SEES it — forgiveness happens at the line, not the AST")
		lineno = _host_clock_call_lines(source)[0][0]
		self.assertIn(_EXEMPT_MARKER, source.splitlines()[lineno - 1])


# The `health()` fields that carry an instant. Anything the service spells as a
# timestamp has to arrive here spelled the same way.
_HEALTH_TIMESTAMP_FIELDS = ("last_quantity_sync_at", "last_catalog_sync_at",
	"last_restrictions_sync_at", "catalog_version")


class TestTheCatalogStubSpellsTimestampsLikeTheService(unittest.TestCase):
	"""The stub-fidelity half of this family — and the one that actually broke CI.

	`parse_service_ts` reads a naive value as UTC, because the service
	speaks UTC and the real wire carries an offset (`2026-08-06T02:30:36-05:00`).
	The catalog stub's `health()` face was being seeded by hand with
	`now_datetime().isoformat()` — the SITE's wall clock with the marker dropped.
	Those are different values, and the difference is the site's UTC offset.

	The result on a real site (dev is America/New_York, UTC-4): a heartbeat seeded
	as "right now" was read back as four hours old, cleared the 90-minute
	threshold, and `test_fresh_heartbeat_is_quiet` found its distributor in the
	stale list. The old host-zone bug had masked it exactly — that code handed
	naive values straight through — so fixing production is what surfaced it.

	The stub already had a correct dialect for its OTHER face (`_ops_ts`, offset-
	bearing). Two dialects in one fake, and the tests on the wrong one only passed
	because production shared their mistake. So the rule this class pins is not
	about timezones at all: **a fake answers in the shape of the thing it fakes.**
	"""

	def _stub(self):
		return catalog_stub.CatalogStub()

	def test_every_health_timestamp_carries_an_offset(self):
		"""Covers the stub's built-in defaults."""
		health = self._stub().health("rsr")
		for field in _HEALTH_TIMESTAMP_FIELDS:
			value = health.get(field)
			if value is None:
				continue          # never-synced is a legitimate answer
			with self.subTest(field=field):
				self.assertIsNotNone(datetime.fromisoformat(value).tzinfo,
					f"{field}={value!r} is naive; the service always sends an offset "
					"and `parse_service_ts` will read this as UTC")

	def test_seeded_heartbeats_carry_an_offset_too(self):
		"""Covers what tests put IN — the half that broke. `set_health` is the
		door every hand-written heartbeat comes through."""
		stub = self._stub()
		stub.set_health("rsr", last_quantity_sync_at=catalog_stub.heartbeat_ts(),
			last_catalog_sync_at=catalog_stub.heartbeat_ts(3 * 3600))
		health = stub.health("rsr")
		for field in ("last_quantity_sync_at", "last_catalog_sync_at"):
			with self.subTest(field=field):
				self.assertIsNotNone(
					datetime.fromisoformat(health[field]).tzinfo)

	@staticmethod
	def _site_now(zone_name):
		"""The site's wall clock right now, built from the zone database.

		Deliberately NOT `frappe.utils.now_datetime()`. On a real bench that IS
		the site's clock and either would do — but in the site-free harness it is
		`datetime.now()`, the HOST's, so every zone below would be reported broken
		for a reason that has nothing to do with the code under test. The thing
		being proved here is what `heartbeat_ts` puts on the wire, and that stays
		independent of this reference either way.
		"""
		return datetime.now(UTC).astimezone(ZoneInfo(zone_name)).replace(tzinfo=None)

	def test_a_seeded_heartbeat_reads_back_at_the_age_it_was_given(self):
		"""The property the watchdog actually depends on, stated directly: seed an
		age, read that age back. `heartbeat_ts(0)` must not be four hours old."""
		for age in (0, 3 * 3600):
			with self.subTest(age=age), site_tz("America/New_York", os_tz="UTC"):
				read_back = remote_catalog.parse_service_ts(catalog_stub.heartbeat_ts(age))
				measured = (self._site_now("America/New_York") - read_back).total_seconds()
				self.assertAlmostEqual(measured, age, delta=60)

	def test_it_reads_back_the_same_age_whatever_zone_the_site_is_on(self):
		"""The regression proper. The broken spelling was right on a UTC site and
		wrong everywhere else, which is how it survived review and died in CI.
		Zones east AND west of UTC, so a fix that merely flipped a sign cannot
		pass, and one that hard-codes an offset cannot either.
		"""
		for site in ("America/New_York", "America/Chicago", "Asia/Kolkata",
				"Australia/Sydney", "UTC"):
			with self.subTest(site=site), site_tz(site, os_tz="UTC"):
				read_back = remote_catalog.parse_service_ts(catalog_stub.heartbeat_ts())
				drift = abs((self._site_now(site) - read_back).total_seconds())
				self.assertLess(drift, 60,
					f"a brand-new heartbeat read as {drift:.0f}s old on a {site} site")

	def test_the_bare_spelling_is_what_it_is_protecting_against(self):
		"""Shows the guard is load-bearing rather than decorative: the exact
		expression that was in `_Base` drifts by the site's offset, which is why
		`heartbeat_ts` exists instead of a comment asking people to be careful."""
		site = "America/New_York"
		with site_tz(site, os_tz="UTC"):
			bare = self._site_now(site).isoformat()          # what `_Base` used to build
			self.assertIsNone(datetime.fromisoformat(bare).tzinfo)
			drift = (self._site_now(site)
				- remote_catalog.parse_service_ts(bare)).total_seconds()
		# The heartbeat reads as old as the site is behind UTC the moment it is made:
		# 4h under EDT, 5h under EST, so take the offset in force now from the zone.
		offset = ZoneInfo(site).utcoffset(datetime.now(UTC)).total_seconds()
		self.assertAlmostEqual(drift, -offset, delta=60)
		self.assertGreater(drift, tasks.FEED_STALE_SECONDS,
			"this is what put a fresh feed past the staleness threshold")
