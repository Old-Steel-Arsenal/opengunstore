"""ATFFormScraperProvider parser + cache layer tests. No real network."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_integrations.atf import ez_check
from ffl_integrations.atf.scraper import ATFFormScraperProvider, _license_type_from_number


HERE = os.path.dirname(__file__)


def _fixture(name: str) -> str:
	with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
		return f.read()


class ScraperParser(FrappeTestCase):
	def test_parse_verified_response(self):
		result = ATFFormScraperProvider._parse(_fixture("ez_check_verified.html"),
			ffl_number="1-99-888-01-XX-12345", today=date(2026, 5, 4))
		self.assertTrue(result.found)
		self.assertEqual(result.status, "Verified")
		self.assertEqual(result.licensee_name, "SMITH JOHN")
		self.assertEqual(result.licensee_dba, "JOHN'S GUNS")
		# License Type is derived from segment 4 of the FFL number (the public
		# EZ Check form doesn't return it). FFL "1-99-888-01-XX-12345" → "01".
		self.assertEqual(result.license_type,
			"01 - Dealer in Firearms Other Than Destructive Devices")
		self.assertEqual(result.expiry_date, date(2028, 12, 31))
		self.assertEqual(result.address, "123 MAIN ST, ANYTOWN, TX 75001")

	def test_parse_not_found_response(self):
		result = ATFFormScraperProvider._parse(_fixture("ez_check_not_found.html"),
			ffl_number="9-99-999-99-9X-99999", today=date(2026, 5, 4))
		self.assertFalse(result.found)
		self.assertEqual(result.status, "NotFound")

	def test_parse_expired_response(self):
		result = ATFFormScraperProvider._parse(_fixture("ez_check_expired.html"),
			ffl_number="6-04-123-01-XX-11111", today=date(2026, 5, 4))
		self.assertTrue(result.found)
		self.assertEqual(result.status, "Expired")
		self.assertEqual(result.expiry_date, date(2024, 12, 31))

	def test_empty_trade_name_does_not_bleed_into_next_field(self):
		"""Regression for the regex stop-anchor bug: when Trade Name is
		blank, the previous parser captured 'Premise Address : <addr>' as
		the Trade Name value. With the fix in _field, blank fields stay
		blank."""
		# Real-shape ATF response: blank Trade Name between License Name
		# and Premise Address (matches what production rows showed).
		text = (
			"License Number : 5-99-888-07-7L-12044 "
			"Expiration Date : 11/01/2027 "
			"License Name : CROSSED RIFLES ARMORY, LLC "
			"Trade Name : "
			"Premise Address : 100 N MAGNOLIA AVE HUBBARD TX - 76648 "
			"Mailing Address : 155 LA TIERRA HUBBARD TX - 76648"
		)
		from bs4 import BeautifulSoup
		# Wrap in HTML so the parser flows through its normal path
		html = f"<html><body>{text}</body></html>"
		result = ATFFormScraperProvider._parse(
			html, ffl_number="5-99-888-07-7L-12044", today=date(2026, 5, 4))
		self.assertEqual(result.licensee_name, "CROSSED RIFLES ARMORY, LLC")
		self.assertIsNone(result.licensee_dba)  # blank, NOT 'Premise Address: ...'
		self.assertEqual(result.address, "100 N MAGNOLIA AVE HUBBARD TX - 76648")

	def test_license_type_derived_from_ffl_number_segment_4(self):
		# Common license type codes
		self.assertEqual(_license_type_from_number("1-99-888-01-7L-12044"),
			"01 - Dealer in Firearms Other Than Destructive Devices")
		self.assertEqual(_license_type_from_number("1-99-888-07-XX-12345"),
			"07 - Manufacturer of Firearms Other Than Destructive Devices")
		self.assertEqual(_license_type_from_number("5-99-888-03-XX-99999"),
			"03 - Collector of Curios and Relics")
		# Unknown / malformed
		self.assertIsNone(_license_type_from_number("1-99-888-99-XX-12345"))  # 99 unknown
		self.assertIsNone(_license_type_from_number("not-an-ffl"))  # too few segments
		self.assertIsNone(_license_type_from_number(""))


class AddressParser(FrappeTestCase):
	"""ez_check._parse_address splits the ATF 'STREET [UNIT] CITY ST - ZIP'
	premise string. A secondary unit (UNIT/STE/APT/# …) that sits between the
	street and the city must land in its own `street2`, NOT get swept into the
	city (the bug that showed UNIT 103 inside the City field)."""

	def test_unit_after_street_type_goes_to_street2(self):
		r = ez_check._parse_address("100 MAIN ST UNIT 103 SPRINGFIELD NY - 10001")
		self.assertEqual(r["street"], "100 MAIN ST")
		self.assertEqual(r["street2"], "UNIT 103")
		self.assertEqual(r["city"], "SPRINGFIELD")
		self.assertEqual(r["state"], "NY")
		self.assertEqual(r["zip"], "10001")

	def test_no_unit_leaves_street2_empty(self):
		r = ez_check._parse_address("100 N MAGNOLIA AVE HUBBARD TX - 76648")
		self.assertEqual(r["street"], "100 N MAGNOLIA AVE")
		self.assertEqual(r.get("street2", ""), "")
		self.assertEqual(r["city"], "HUBBARD")

	def test_suite_designator(self):
		r = ez_check._parse_address("12 OAK ST STE 5 DALLAS TX - 75001")
		self.assertEqual(r["street"], "12 OAK ST")
		self.assertEqual(r["street2"], "STE 5")
		self.assertEqual(r["city"], "DALLAS")

	def test_pound_designator_combined_token(self):
		r = ez_check._parse_address("5 ELM RD #2 RENO NV - 89500")
		self.assertEqual(r["street"], "5 ELM RD")
		self.assertEqual(r["street2"], "#2")
		self.assertEqual(r["city"], "RENO")

	def test_multi_word_city_with_unit(self):
		r = ez_check._parse_address("10 BAY AVE APT 4 GROSSE POINTE MI - 48230")
		self.assertEqual(r["street"], "10 BAY AVE")
		self.assertEqual(r["street2"], "APT 4")
		self.assertEqual(r["city"], "GROSSE POINTE")

	def test_texas_fm_road_not_mistaken_for_unit(self):
		# "FM 936" is the road; no unit. City must stay clean, street2 empty.
		r = ez_check._parse_address("2721 FM 936 HUBBARD TX - 76648")
		self.assertEqual(r["street"], "2721 FM 936")
		self.assertEqual(r.get("street2", ""), "")
		self.assertEqual(r["city"], "HUBBARD")

	def test_city_named_like_designator_not_split(self):
		# 'KEY WEST' is a city, not a 'KEY' unit. A designator word with no unit
		# identifier after it must NOT trigger a split.
		r = ez_check._parse_address("100 OCEAN DR KEY WEST FL - 33040")
		self.assertEqual(r["street"], "100 OCEAN DR")
		self.assertEqual(r.get("street2", ""), "")
		self.assertEqual(r["city"], "KEY WEST")

	def test_unmatched_format_falls_back_to_street(self):
		# No ' - ZIP' tail → whole string preserved as street, no crash.
		r = ez_check._parse_address("123 MAIN ST, ANYTOWN, TX 75001")
		self.assertEqual(r["street"], "123 MAIN ST, ANYTOWN, TX 75001")
		self.assertEqual(r.get("street2", ""), "")


class CacheLayer(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Sane TTL + scraper provider — RESTORED afterwards: this used to flip the
		# shared dev site's provider switch off "Data Service" on every test run (see
		# site_fixtures.snapshot_single_values).
		from ffl_integrations.tests.site_fixtures import snapshot_single_values
		cls.addClassCleanup(snapshot_single_values(
			"FFL Settings", ("live_check_cache_ttl_minutes", "live_check_provider")))
		frappe.db.set_single_value("FFL Settings", "live_check_cache_ttl_minutes", 60)
		frappe.db.set_single_value("FFL Settings", "live_check_provider", "ATF Form Scraper")

	def setUp(self):
		# Clean any test FFL row.
		if frappe.db.exists("ATF FFL Record", "1-99-888-01-XX-12345"):
			frappe.db.delete("ATF FFL Record", {"ffl_number": "1-99-888-01-XX-12345"})

	def test_cache_miss_calls_provider_and_stores(self):
		stub = ez_check.EZCheckResult(
			ffl_number="1-99-888-01-XX-12345", found=True, status="Verified",
			licensee_name="SMITH JOHN", expiry_date=date(2028, 12, 31),
		)
		with patch.object(ATFFormScraperProvider, "verify", return_value=stub) as mocked:
			r = ez_check.verify("1-99-888-01-XX-12345")
		self.assertEqual(r.status, "Verified")
		self.assertEqual(mocked.call_count, 1)
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-01-XX-12345"))

	def test_cache_hit_within_ttl_skips_provider(self):
		# Pre-seed cache with a fresh live_verified_at.
		frappe.get_doc({
			"doctype": "ATF FFL Record",
			"ffl_number": "1-99-888-01-XX-12345",
			"licensee_name": "Cached Name",
			"loaded_from": "ez-check-live",
			"loaded_at": now_datetime(),
			"live_verified_at": now_datetime(),
		}).insert(ignore_permissions=True)

		with patch.object(ATFFormScraperProvider, "verify") as mocked:
			r = ez_check.verify("1-99-888-01-XX-12345")
		self.assertEqual(mocked.call_count, 0)
		self.assertEqual(r.licensee_name, "Cached Name")

	def test_cache_miss_outside_ttl_re_fetches(self):
		# Pre-seed cache with a stale live_verified_at (2h old, TTL 60min).
		frappe.get_doc({
			"doctype": "ATF FFL Record",
			"ffl_number": "1-99-888-01-XX-12345",
			"licensee_name": "Stale Name",
			"loaded_from": "ez-check-live",
			"loaded_at": now_datetime() - timedelta(hours=2),
			"live_verified_at": now_datetime() - timedelta(hours=2),
		}).insert(ignore_permissions=True)

		stub = ez_check.EZCheckResult(
			ffl_number="1-99-888-01-XX-12345", found=True, status="Verified",
			licensee_name="Fresh Name", expiry_date=date(2028, 12, 31),
		)
		with patch.object(ATFFormScraperProvider, "verify", return_value=stub) as mocked:
			r = ez_check.verify("1-99-888-01-XX-12345")
		self.assertEqual(mocked.call_count, 1)
		self.assertEqual(r.licensee_name, "Fresh Name")

	def test_provider_failure_falls_back_to_cache(self):
		# Pre-seed cache (any age).
		frappe.get_doc({
			"doctype": "ATF FFL Record",
			"ffl_number": "1-99-888-01-XX-12345",
			"licensee_name": "Cached",
			"loaded_from": "csv-bulk",
			"loaded_at": now_datetime() - timedelta(days=10),
		}).insert(ignore_permissions=True)

		def boom(*_a, **_k):
			raise ez_check.EZCheckUnavailable("ATF down")

		with patch.object(ATFFormScraperProvider, "verify", side_effect=boom):
			r = ez_check.verify("1-99-888-01-XX-12345", force_live=True)
		self.assertTrue(r.found)
		self.assertEqual(r.licensee_name, "Cached")
		self.assertEqual(r.status, "Verified")  # cache rows are treated as Verified

	def test_unit_address_split_into_street2_on_store(self):
		"""End-to-end store path: a premise with a secondary unit caches street2
		separately and a clean city — not 'UNIT 103' swept into City."""
		stub = ez_check.EZCheckResult(
			ffl_number="1-99-888-01-XX-12345", found=True, status="Verified",
			licensee_name="BANG INC", expiry_date=date(2028, 12, 31),
			address="100 MAIN ST UNIT 103 SPRINGFIELD NY - 10001",
		)
		with patch.object(ATFFormScraperProvider, "verify", return_value=stub):
			ez_check.verify("1-99-888-01-XX-12345", force_live=True)
		row = frappe.db.get_value(
			"ATF FFL Record", "1-99-888-01-XX-12345",
			["street", "street2", "city"], as_dict=True)
		self.assertEqual(row.street, "100 MAIN ST")
		self.assertEqual(row.street2, "UNIT 103")
		self.assertEqual(row.city, "SPRINGFIELD")

	def test_lookup_and_create_returns_summary_dict(self):
		"""The list-view "+ Lookup by FFL #" wrapper returns a JSON summary
		and upserts the row."""
		stub = ez_check.EZCheckResult(
			ffl_number="1-99-888-01-XX-12345", found=True, status="Verified",
			licensee_name="SMITH JOHN", expiry_date=date(2028, 12, 31),
			address="123 Main, Anytown, IL 60000",
		)
		with patch.object(ATFFormScraperProvider, "verify", return_value=stub):
			out = ez_check.lookup_and_create("1-99-888-01-XX-12345")
		self.assertTrue(out["ok"])
		self.assertEqual(out["status"], "Verified")
		self.assertEqual(out["name"], "1-99-888-01-XX-12345")
		self.assertEqual(out["licensee_name"], "SMITH JOHN")
		self.assertEqual(out["expiry_date"], "2028-12-31")
		self.assertTrue(frappe.db.exists("ATF FFL Record", "1-99-888-01-XX-12345"))

	def test_lookup_and_create_rejects_obvious_garbage(self):
		"""<9 digits → rejected before hitting the provider."""
		with patch.object(ATFFormScraperProvider, "verify") as mocked:
			with self.assertRaises(frappe.ValidationError):
				ez_check.lookup_and_create("not-an-ffl")
		self.assertEqual(mocked.call_count, 0)

	def test_lookup_and_create_throws_on_not_found(self):
		stub = ez_check.EZCheckResult(
			ffl_number="9-99-999-99-9X-99999", found=False, status="NotFound",
		)
		with patch.object(ATFFormScraperProvider, "verify", return_value=stub):
			with self.assertRaises(frappe.ValidationError):
				ez_check.lookup_and_create("9-99-999-99-9X-99999")


class NotFoundKeepsRecord(FrappeTestCase):
	"""A NotFound from EZ Check must not blank the ATF FFL Record. Type 03
	collectors are never in EZ Check, so their hand-entered rows stay as they
	are; a dealer that dropped out keeps name/address but loses its expiry so
	atf_record_block refuses to ship to it; an unknown number inserts nothing."""

	CR = "5-99-888-03-XX-54321"
	DEALER = "1-99-888-01-XX-54321"
	UNKNOWN = "9-99-999-01-9X-54321"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_integrations.tests.site_fixtures import snapshot_single_values
		cls.addClassCleanup(snapshot_single_values("FFL Settings", ("live_check_provider",)))
		frappe.db.set_single_value("FFL Settings", "live_check_provider", "ATF Form Scraper")

	def setUp(self):
		frappe.db.delete("ATF FFL Record", {"ffl_number": ("in", [self.CR, self.DEALER, self.UNKNOWN])})
		for ffl in (self.CR, self.DEALER):
			frappe.get_doc({
				"doctype": "ATF FFL Record",
				"ffl_number": ffl,
				"licensee_name": "KEEP ME",
				"street": "1 MAIN ST",
				"city": "AUSTIN",
				"state": "TX",
				"zip": "78701",
				"expiry_date": date(2029, 1, 31),
				"loaded_from": "manual",
				"loaded_at": now_datetime(),
			}).insert(ignore_permissions=True)

	def _not_found(self, ffl):
		stub = ez_check.EZCheckResult(ffl_number=ffl, found=False, status="NotFound")
		return patch.object(ATFFormScraperProvider, "verify", return_value=stub)

	def test_cr_record_untouched(self):
		with self._not_found(self.CR):
			r = ez_check.verify(self.CR, force_live=True)
		self.assertFalse(r.found)
		row = frappe.db.get_value("ATF FFL Record", self.CR,
			["licensee_name", "street", "state", "expiry_date"], as_dict=True)
		self.assertEqual(row.licensee_name, "KEEP ME")
		self.assertEqual(row.street, "1 MAIN ST")
		self.assertEqual(row.state, "TX")
		self.assertEqual(row.expiry_date, date(2029, 1, 31))

	def test_dealer_keeps_name_but_expiry_voided_and_blocked(self):
		from ffl_core.compliance.dealer_transfer import FFL_BLOCK_EXPIRED, atf_record_block
		self.assertIsNone(atf_record_block(self.DEALER))
		with self._not_found(self.DEALER):
			ez_check.verify(self.DEALER, force_live=True)
		row = frappe.db.get_value("ATF FFL Record", self.DEALER,
			["licensee_name", "street", "expiry_date"], as_dict=True)
		self.assertEqual(row.licensee_name, "KEEP ME")
		self.assertEqual(row.street, "1 MAIN ST")
		self.assertIsNone(row.expiry_date)
		self.assertEqual(atf_record_block(self.DEALER)[0], FFL_BLOCK_EXPIRED)

	def test_unknown_number_inserts_nothing(self):
		with self._not_found(self.UNKNOWN):
			with self.assertRaises(frappe.ValidationError):
				ez_check.lookup_and_create(self.UNKNOWN)
		self.assertFalse(frappe.db.exists("ATF FFL Record", self.UNKNOWN))
