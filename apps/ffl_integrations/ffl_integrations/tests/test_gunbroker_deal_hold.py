"""A gun on GunBroker and a CRM deal hold (ffl_core.crm.holds). The store's rule: a gun on sale on
GunBroker is not held (end the listing first); the hold never touches GunBroker. Once held, the gun
is a counter hold every GunBroker gate already refuses, and an order that lands on it says a deal
holds it. Real holds through the real endpoint; GunBroker's flags are set on the gun, no call goes out."""
from __future__ import annotations

import frappe

from ffl_core.crm import holds
from ffl_core.reservations import serial_shelf_state
from ffl_core.tests.deal_hold_support import DealHoldCase

from ffl_integrations.gunbroker import crosschannel
from ffl_integrations.gunbroker.orders import _shelf_conflict

LISTED = {"gb_item_id": "1122334455"}


class TestGunBrokerDealHold(DealHoldCase):
	def _try(self, **flags) -> dict:
		serial = self._gun()
		if flags:
			frappe.db.set_value("Serial No", serial, flags, update_modified=False)
		return self._hold(self._deal(serial), serial)

	def test_the_refusal_is_registered(self):
		self.assertIn("ffl_integrations.gunbroker.crosschannel.deal_hold_refusal",
			frappe.get_hooks(holds.HOOK_REFUSAL))

	def test_a_live_listing_is_refused(self):
		for flags in (LISTED, {"gb_push_pending": 1}, {**LISTED, "gb_end_pending": 1}):
			out = self._try(**flags)
			self.assertEqual(out["held"], [], flags)
			self.assertIn("is listed on GunBroker", out["refused"][0]["reason"], flags)
			self.assertIn("End the listing first, then hold it.", out["refused"][0]["reason"])
		self.assertIn("(item 1122334455)", self._try(**LISTED)["refused"][0]["reason"])

	def test_a_gun_that_may_have_sold_there_is_refused(self):
		out = self._try(gb_sold_unknown=1)
		self.assertEqual(out["held"], [])

	def test_never_listed_or_ended_may_be_held(self):
		self.assertEqual(len(self._try()["held"]), 1)
		self.assertEqual(len(self._try(gb_delisted=1)["held"]), 1)

	def test_the_provider_reads_flags_only(self):
		serial = self._gun()
		self.assertIsNone(crosschannel.deal_hold_refusal(serial))
		frappe.db.set_value("Serial No", serial, LISTED, update_modified=False)
		self.assertIn("1122334455", crosschannel.deal_hold_refusal(serial))

	def test_a_held_gun_is_off_gunbroker_and_an_order_on_it_names_the_deal(self):
		serial = self._gun()
		deal = self._deal(serial)
		self._held(deal, serial)
		state = serial_shelf_state(serial)
		self.assertFalse(state["sellable"], "the listing guard (step 9) refuses it")
		self.assertEqual((state["reason"], state["detail"]["kind"]), ("counter_hold", "crm_deal"))
		text = _shelf_conflict(serial, state)
		self.assertIn(f"held for deal {deal}", text)
		self.assertIn("a manager can release the hold", text)
