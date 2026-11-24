"""GunBroker hides the middle of the receiving dealer's licence.

The order sync puts the full number back from the national listing, so the
own-store pickup check, C&R detection and Dispose all see a real licence.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.atf import data_service_provider
from ffl_integrations.atf.ez_check import EZCheckResult, EZCheckUnavailable
from ffl_integrations.gunbroker import orders
from ffl_integrations.tests import gunbroker_fixtures as fx

MASKED = "1-54-XXX-XX-XX-27865"
FULL = "1-54-820-01-8G-27865"
SEARCH = "ffl_integrations.atf.data_service_provider.search_remote"


def _row(number):
	return {"ffl_number": number, "licensee_name": "BIG RIDGE OUTDOORS LLC"}


class TestUnmask(FrappeTestCase):
	def test_a_masked_licence_is_recognised(self):
		self.assertTrue(orders.is_masked_licence(MASKED))
		self.assertFalse(orders.is_masked_licence(FULL))
		self.assertFalse(orders.is_masked_licence(""))

	def test_the_one_licence_that_fits_near_the_ship_to_zip_is_taken(self):
		rows = [_row("1-54-820-01-8G-11111"), _row(FULL), _row("2-54-820-01-8G-27865")]
		with patch(SEARCH, return_value=rows) as search:
			self.assertEqual(orders.unmask_licence(MASKED, "22980-5718"), FULL)
		self.assertEqual(search.call_args.kwargs["zip_code"], "22980-5718")

	def test_two_fits_leave_it_masked(self):
		with patch(SEARCH, return_value=[_row(FULL), _row("1-54-111-07-9A-27865")]):
			self.assertEqual(orders.unmask_licence(MASKED, "22980"), MASKED)

	def test_no_fit_leaves_it_masked(self):
		with patch(SEARCH, return_value=[_row("1-54-820-01-8G-11111")]):
			self.assertEqual(orders.unmask_licence(MASKED, "22980"), MASKED)

	def test_the_service_down_leaves_it_masked(self):
		with patch(SEARCH, side_effect=EZCheckUnavailable("down")):
			self.assertEqual(orders.unmask_licence(MASKED, "22980"), MASKED)

	def test_no_zip_makes_no_call(self):
		with patch(SEARCH) as search:
			self.assertEqual(orders.unmask_licence(MASKED, ""), MASKED)
		search.assert_not_called()


class TestTheSyncStoresTheFullLicence(FrappeTestCase):
	def _apply(self, order, incoming):
		with patch.object(orders, "unmask_licence", return_value=FULL) as unmask:
			orders.apply_order_fields(order, fx.order_detail(ffl_number=incoming))
		return unmask

	def test_a_new_masked_licence_is_stored_in_full(self):
		order = fx.FakeDoc(ffl_number="")
		unmask = self._apply(order, MASKED)
		self.assertEqual(order.ffl_number, FULL)
		self.assertEqual(unmask.call_args.args, (MASKED, "78701"))

	def test_a_stored_masked_licence_is_retried(self):
		order = fx.FakeDoc(ffl_number=MASKED)
		self._apply(order, MASKED)
		self.assertEqual(order.ffl_number, FULL)

	def test_a_full_licence_already_stored_is_left_alone(self):
		order = fx.FakeDoc(ffl_number="1-99-888-01-2A-34567")
		unmask = self._apply(order, MASKED)
		self.assertEqual(order.ffl_number, "1-99-888-01-2A-34567")
		unmask.assert_not_called()


class TestGeoSearchPayload(FrappeTestCase):
	def test_a_zip_search_sends_the_zip_and_radius_without_a_name(self):
		with patch.object(data_service_provider, "_post", return_value={"results": []}) as post:
			data_service_provider.search_remote("", limit=200, zip_code="22980-5718")
		payload = post.call_args.args[1]
		self.assertEqual(payload, {"limit": 200, "zip": "22980", "radius_miles": 10})


class TestANewDealerIsLookedUpOnTheSync(FrappeTestCase):
	"""The card names the dealer from its ATF record, so the sync fetches one."""

	DEALER = "9-99-123-01-8E-54321"

	def _queued(self, **fields):
		order = fx.FakeDoc(**{"ffl_number": self.DEALER, "atf_ffl_record": "", **fields})
		with patch("frappe.enqueue") as enqueue:
			orders._queue_dealer_lookup(order)
		return enqueue

	def test_an_unseen_dealer_is_queued_after_the_poll_commits(self):
		enqueue = self._queued()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["ffl_number"], self.DEALER)
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_nothing_to_look_up_queues_nothing(self):
		for fields in ({"ffl_number": ""}, {"ffl_number": MASKED},
				{"ffl_number": "5-74-000-03-9A-12345"},  # Type 03: never in eZ Check
				{"atf_ffl_record": self.DEALER}):
			with self.subTest(**fields):
				self._queued(**fields).assert_not_called()

	def test_a_dealer_already_on_file_is_not_looked_up_again(self):
		frappe.get_doc({"doctype": "ATF FFL Record", "ffl_number": self.DEALER,
			"licensee_name": "ON FILE"}).insert(ignore_permissions=True,
				ignore_if_duplicate=True)
		self._queued().assert_not_called()

	def test_the_lookup_writes_the_record(self):
		frappe.db.delete("ATF FFL Record", self.DEALER)
		found = EZCheckResult(ffl_number=self.DEALER, found=True, status="Verified",
			licensee_name="RIVERBEND ARMS LLC", licensee_dba="RIVERBEND ARMS",
			expiry_date=date(2028, 5, 1),
			address="12 MAIN ST SPRINGFIELD IL - 62701")
		provider = MagicMock()
		provider.verify.return_value = found
		# _upsert_cache commits for real; a test must not.
		with patch("ffl_integrations.atf.ez_check.get_provider", return_value=provider), \
				patch("frappe.db.commit"):
			orders.cache_dealer_licence(self.DEALER)
		self.assertEqual(frappe.db.get_value("ATF FFL Record", self.DEALER,
			"licensee_name"), "RIVERBEND ARMS LLC")

	def test_an_outage_is_left_for_the_next_poll(self):
		frappe.db.delete("ATF FFL Record", self.DEALER)
		provider = MagicMock()
		provider.verify.side_effect = EZCheckUnavailable("down")
		with patch("ffl_integrations.atf.ez_check.get_provider", return_value=provider):
			orders.cache_dealer_licence(self.DEALER)
		self.assertFalse(frappe.db.exists("ATF FFL Record", self.DEALER))
