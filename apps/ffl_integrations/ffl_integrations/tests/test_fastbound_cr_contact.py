"""FastBound contact for a Type 03 (C&R) recipient.

Why this needs its own shape: FastBound's `lookupFFL` asks FastBound to fill the
contact in from the ATF FFL database. That database does not list Type 03
collectors, so on a C&R number the lookup fails and the whole
Dispositions/CreateAndCommit 400s. The failure lands AFTER the firearm has left
the building — stock is out, the shipment is moving, and the bound book has no
disposition. That is the exact shape of an ATF violation, so the C&R branch
forces `lookupFFL: false` and supplies every field itself.

Production has `lookup_ffl_on_push = 1` (lead's read-only survey of the live
account), so these tests pin the setting to 1 and still demand False. A test
that left the setting at 0 would pass against code that simply read the setting.

The other half is E3: the non-03 path must stay byte-identical — same minimal
contact, same setting-driven lookupFFL, and `fflNumber` still the hyphenated
original. Normalization happens ONLY in the new branch; "fixing" the old path
here would be a second, unreviewed change to every dealer transfer.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig, FastBoundError

CR = "1-23-456-03-7C-89012"
CR_NORMALIZED = "123456037C89012"
DEALER = "5-99-888-01-9D-26128"


def _client() -> FastBoundClient:
	cfg = FastBoundConfig(account_num="TEST", api_key="k", base_url="https://example.invalid",
		api_version="1.0", audit_user="u", timeout=5)
	return FastBoundClient(cfg, session=MagicMock())


def _req_side_effect(disp_id="fbdisp-1"):
	def fn(method, path, **kw):
		if method == "POST" and path == "Dispositions/CreateAndCommit":
			return None
		if method == "GET" and path.startswith("Items/"):
			return {"dispositionId": disp_id}
		return None
	return fn


def _disp(**over):
	base = dict(name="DISP-CR-1", serial="CRSN-1", disposition_date="2026-07-29T10:00:00",
		notes="", disposition_type="FFL Transfer", destination_ffl=CR, fastbound_id=None)
	base.update(over)
	return SimpleNamespace(**base)


def _atf_record(**over):
	rec = {
		"licensee_name": "Jane Q. Collector",
		"street": "123 Main St", "street2": "Apt 4",
		"city": "Austin", "state": "TX", "zip": "78701",
		"expiry_date": "2028-07-31",
	}
	rec.update(over)
	return rec


class _Harness:
	"""Runs post_disposition with FastBound + the DB stubbed, and captures the
	body that would have gone on the wire."""

	def __init__(self, testcase, record=None, existing_contact=None, lookup_setting=1):
		self.tc = testcase
		self.record = record if record is not None else _atf_record()
		self.existing_contact = existing_contact
		self.lookup_setting = lookup_setting

	def run(self, disp=None):
		disp = disp or _disp()
		client = _client()
		self.request = MagicMock(side_effect=_req_side_effect())

		def get_value(doctype, name, fields=None, **kw):
			if doctype == "ATF FFL Record":
				if isinstance(fields, str):
					return self.record.get(fields)
				return dict(self.record)
			if doctype == "Serial No":
				return "fb-item-1"
			return None

		with patch.object(client, "_request", self.request), \
			 patch.object(client, "find_contact_by_ffl_number",
				return_value=self.existing_contact), \
			 patch("frappe.db.exists", return_value=True), \
			 patch("frappe.db.get_value", side_effect=get_value), \
			 patch("frappe.db.get_single_value", return_value=self.lookup_setting):
			self.result = client.post_disposition(disp)
		return self

	@property
	def body(self):
		for call in self.request.call_args_list:
			if call.args[:2] == ("POST", "Dispositions/CreateAndCommit"):
				return call.kwargs["json_body"]
		raise AssertionError("no CreateAndCommit request was made")

	@property
	def posted(self):
		return any(c.args[:2] == ("POST", "Dispositions/CreateAndCommit")
			for c in self.request.call_args_list)


class CrContactShapeTest(FrappeTestCase):
	def test_lookup_ffl_is_forced_false_even_when_the_setting_says_true(self):
		"""The whole point. Setting is 1 (as in production) — must still be False."""
		body = _Harness(self, lookup_setting=1).run().body
		self.assertIs(body["contact"]["lookupFFL"], False)

	def test_ffl_number_is_the_15_char_normalized_form(self):
		body = _Harness(self).run().body
		self.assertEqual(body["contact"]["fflNumber"], CR_NORMALIZED)
		self.assertNotIn("-", body["contact"]["fflNumber"])

	def test_external_id_follows_the_existing_convention(self):
		body = _Harness(self).run().body
		self.assertEqual(body["contact"]["externalId"], f"ffl-{CR_NORMALIZED}")

	def test_identity_comes_from_the_verified_record(self):
		body = _Harness(self).run().body
		c = body["contact"]
		self.assertEqual(c["licenseName"], "Jane Q. Collector")

	def test_person_name_fields_are_never_sent_alongside_an_ffl_number(self):
		"""MEASURED, not reasoned (FastBound test account 147648, 2026-07-29).

		Sending firstName/lastName together with fflNumber returns
		    400 "When FFL information provided, Organization Name -AND- First
		         Name, Middle Name, Last Name, and Suffix must be empty."
		so the earlier shape would have 400'd EVERY C&R disposition. The same
		payload with those keys removed returned 201 and stored licenseName
		verbatim.

		The keys must be ABSENT, not empty strings — whether FastBound also
		accepts "" was never measured, so this pins the shape we actually
		verified rather than one we assume is equivalent.
		"""
		for record in (_atf_record(), _atf_record(licensee_name="Cher")):
			c = _Harness(self, record=record).run().body["contact"]
			for key in ("firstName", "middleName", "lastName", "suffix",
					"organizationName"):
				self.assertNotIn(key, c)
			# The name is not lost — a Type 03 licence is issued to a person, so
			# licenseName IS that person's name.
			self.assertTrue(c["licenseName"])

	def test_premise_address_is_complete_and_us(self):
		body = _Harness(self).run().body
		c = body["contact"]
		self.assertEqual(c["premiseAddress1"], "123 Main St")
		self.assertEqual(c["premiseAddress2"], "Apt 4")
		self.assertEqual(c["premiseCity"], "Austin")
		self.assertEqual(c["premiseState"], "TX")
		self.assertEqual(c["premiseZipCode"], "78701")
		self.assertEqual(c["premiseCountry"], "US")

	def test_blank_address_2_is_omitted_entirely(self):
		body = _Harness(self, record=_atf_record(street2="")).run().body
		self.assertNotIn("premiseAddress2", body["contact"])

	def test_expiry_is_sent_as_an_iso_date(self):
		"""FastBound rejects an FFL contact with no expiry when lookup is off —
		the acquisition side already learned this (client.py comment)."""
		body = _Harness(self).run().body
		self.assertEqual(body["contact"]["fflExpires"], "2028-07-31")

	def test_date_object_expiry_is_serialized(self):
		from datetime import date
		body = _Harness(self, record=_atf_record(expiry_date=date(2028, 7, 31))).run().body
		self.assertEqual(body["contact"]["fflExpires"], "2028-07-31")

	def test_existing_contact_is_reused_instead_of_rebuilt(self):
		"""Creating a duplicate 400s 'This FFL Number already exists'."""
		h = _Harness(self, existing_contact={"id": "c-77"}).run()
		self.assertEqual(h.body["contactId"], "c-77")
		self.assertNotIn("contact", h.body)


class IncompleteCrContactTest(FrappeTestCase):
	"""Never degrade, never guess — and never send a doomed request."""

	def _assert_refuses(self, record, missing_hint):
		h = _Harness(self, record=record)
		with self.assertRaises(FastBoundError) as cm:
			h.run()
		self.assertIn("incomplete", str(cm.exception))
		self.assertIn(missing_hint, str(cm.exception))
		self.assertFalse(h.posted, "a doomed request was still sent to FastBound")

	def test_missing_licensee_name_refuses_before_any_http(self):
		self._assert_refuses(_atf_record(licensee_name=""), "licenseName")

	def test_missing_expiry_refuses(self):
		self._assert_refuses(_atf_record(expiry_date=""), "fflExpires")

	def test_missing_street_refuses(self):
		self._assert_refuses(_atf_record(street=""), "premiseAddress1")

	def test_missing_city_state_zip_refuse(self):
		self._assert_refuses(_atf_record(city=""), "premiseCity")
		self._assert_refuses(_atf_record(state=""), "premiseState")
		self._assert_refuses(_atf_record(zip=""), "premiseZipCode")

	def test_missing_atf_record_entirely_refuses(self):
		self._assert_refuses({}, "licenseName")

	def test_error_names_the_order_to_retry(self):
		h = _Harness(self, record=_atf_record(licensee_name=""))
		with self.assertRaises(FastBoundError) as cm:
			h.run(_disp(woo_online_order="WOO-ORD-00042"))
		self.assertIn("WOO-ORD-00042", str(cm.exception))

	def test_never_degrades_to_an_individual_contact(self):
		"""Recording a licensee-to-licensee transfer as a sale to an individual
		would claim a 4473 that does not exist. Refusing is the only safe move."""
		h = _Harness(self, record=_atf_record(licensee_name=""))
		with self.assertRaises(FastBoundError):
			h.run()
		self.assertFalse(h.posted)


class NonCrPathUnchangedTest(FrappeTestCase):
	"""E3 — the dealer path must not notice this feature exists."""

	def test_dealer_contact_keeps_the_minimal_three_key_shape(self):
		body = _Harness(self, lookup_setting=1).run(_disp(destination_ffl=DEALER)).body
		self.assertEqual(set(body["contact"]), {"externalId", "fflNumber", "lookupFFL"})

	def test_dealer_ffl_number_keeps_its_hyphens(self):
		body = _Harness(self).run(_disp(destination_ffl=DEALER)).body
		self.assertEqual(body["contact"]["fflNumber"], DEALER)

	def test_dealer_lookup_ffl_still_follows_the_setting(self):
		on = _Harness(self, lookup_setting=1).run(_disp(destination_ffl=DEALER)).body
		off = _Harness(self, lookup_setting=0).run(_disp(destination_ffl=DEALER)).body
		self.assertIs(on["contact"]["lookupFFL"], True)
		self.assertIs(off["contact"]["lookupFFL"], False)

	def test_dealer_transfer_needs_no_atf_record(self):
		"""A missing record must not start blocking ordinary dealer transfers."""
		h = _Harness(self, record={}).run(_disp(destination_ffl=DEALER))
		self.assertTrue(h.posted)


class LicenseTypeLabelConsistencyTest(unittest.TestCase):
	"""One spelling per column, pinned across the two apps that write it.

	`ATF FFL Record.license_type` has two writers: the eZ Check importer (this
	app) and the C&R verification (ffl_woo_sync). If they disagree, nothing
	errors — an equality match in a report, a Desk filter or a `== "03"` just
	quietly returns fewer rows.

	This lives here, not in ffl_core, so no production cross-app import is
	created for the sake of one string: ffl_integrations already depends on
	ffl_core, so it is the natural place to assert the two agree.
	"""

	def test_cr_label_matches_the_ez_check_importers_spelling(self):
		from ffl_core.compliance.ffl_number import CR_LICENSE_TYPE_LABEL
		from ffl_integrations.atf.scraper import _LICENSE_TYPE_LABELS
		self.assertEqual(CR_LICENSE_TYPE_LABEL, _LICENSE_TYPE_LABELS["03"])

	def test_the_shared_reader_extracts_03_from_that_label(self):
		"""How the column is actually consumed — atf_link._license_code takes the
		leading two digits, so the label form still answers "is this an 03?"."""
		from ffl_core.atf_link import _license_code
		from ffl_core.compliance.ffl_number import CR_LICENSE_TYPE_LABEL
		self.assertEqual(_license_code(CR_LICENSE_TYPE_LABEL), "03")


class BaseUrlFailsClosedTest(FrappeTestCase):
	"""A blank Base URL must stop the integration, not silently aim it at prod.

	Pre-existing defect, not introduced by the C&R work. `from_settings` used to
	fall back to https://cloud.fastbound.com — the live bound book — when the
	field was empty. The account-number and API-key guards above it had already
	passed by then, so a dev site with a cleared Base URL would have posted real
	acquisitions and dispositions to the real ledger using its own key, with no
	error raised and nothing in the request log to tell it apart from normal
	traffic. A mis-click, a re-install, a restored backup or a fixture reload was
	enough to trigger it.

	This is a quieter prod channel than SSH: SSH at least looks like a
	production action, whereas this looks like an empty field.
	"""

	def _settings(self, base_url):
		s = MagicMock()
		s.account_num = "TEST"
		s.get_password.return_value = "k"
		s.base_url = base_url
		s.api_version = "1.0"
		s.default_audit_user = "u"
		s.request_timeout_seconds = 30
		return s

	def test_blank_base_url_raises_instead_of_defaulting_to_production(self):
		from ffl_integrations.fastbound.client import FastBoundConfig, FastBoundConfigError

		for blank in ("", None):
			with self.subTest(value=blank):
				with patch("frappe.get_single", return_value=self._settings(blank)):
					with self.assertRaises(FastBoundConfigError) as cm:
						FastBoundConfig.from_settings()
				self.assertIn("Base URL", str(cm.exception))

	def test_no_request_can_be_made_when_the_base_url_is_blank(self):
		"""The point is not the exception, it is that nothing reaches the wire —
		same standard as the incomplete-C&R-contact case."""
		from ffl_integrations.fastbound.client import (
			FastBoundClient,
			FastBoundConfigError,
		)

		session = MagicMock()
		with patch("frappe.get_single", return_value=self._settings("")):
			with self.assertRaises(FastBoundConfigError):
				FastBoundClient.from_settings()
		self.assertFalse(session.request.called)
		self.assertFalse(session.post.called)
		self.assertFalse(session.get.called)

	def test_an_explicit_base_url_is_still_honoured_and_normalised(self):
		from ffl_integrations.fastbound.client import FastBoundConfig

		with patch("frappe.get_single", return_value=self._settings("https://sandbox.example/")):
			cfg = FastBoundConfig.from_settings()
		self.assertEqual(cfg.base_url, "https://sandbox.example")
