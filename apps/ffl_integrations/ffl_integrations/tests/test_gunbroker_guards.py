"""The sixteen reasons a gun does not go on GunBroker.

Two properties are asserted throughout, and they matter more than the individual
reasons:

* **Order.** The first match is what the operator is told. The reasons are
  arranged most-specific-first, so "this is a customer's transfer gun" wins over
  "something is holding it" — one of those is actionable and the other is not.
* **No client is ever built.** Every guard but the last is answered from local
  data, so a refusal costs no HTTP call and reads no credential. Asserted by
  patching `GunBrokerClient.from_settings` and checking it was never called,
  which is stronger than counting requests: it catches a guard that merely reads
  the settings before deciding.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.gunbroker.client import GunBrokerClient
from ffl_integrations.tests import gunbroker_fixtures as fx

CATEGORY_MAP = [frappe._dict(
	{"firearm_type": "Revolver", "is_cr_eligible": 0, "category_id": 2325})]


class GuardMatrix(FrappeTestCase):
	"""Each test trips exactly one guard, from an otherwise listable gun."""

	def setUp(self):
		self.settings = fx.settings_stub(category_map=CATEGORY_MAP)
		self.context = {
			"serial": frappe._dict({
				"name": "SN-001", "item_code": "COLT-PYTHON",
				"item_name": "Colt Python 357", "description": "", "status": "Active",
				"condition_grade": "", "firearm_type": "Revolver", "year_made": 0,
				"image": "", "sell_price": 1200,
			}),
			"item": frappe._dict({
				"item_name": "Colt Python", "description": "<p>x</p>", "is_used": 1,
				"is_antique": 0, "is_firearm": 1, "firearm_type": "Revolver",
				"year_made": 0, "upc": "", "uniform_sell_price": 0,
				"standard_rate": 0, "gb_category_id": 0, "image": "",
			}),
		}
		self.state = {
			"item_code": "COLT-PYTHON", "status": "Active", "gb_item_id": "",
			"gb_item_history": "", "gb_delisted": 0, "gb_end_pending": 0,
			"gb_push_pending": 0, "gb_push_attempted_at": None, "gb_price_dirty": 0,
			"gb_sold_unknown": 0, "gb_gone_strikes": 0, "gb_gone_first_at": None,
			"gb_reserved": 0, "gb_order": "", "gb_sync_error": "",
		}
		self.shelf = {"sellable": True, "reason": None, "detail": {}}

		self._patch("ffl_integrations.integration_gate.gunbroker_enabled",
			return_value=True)
		self._keys(fx.dev_keys())
		self._patch_object(listings, "_settings", side_effect=lambda: self.settings)
		self._patch_object(mapping, "listing_context",
			side_effect=lambda _sn: self.context)
		self._patch_object(listings, "serial_state",
			side_effect=lambda _sn, **_kw: frappe._dict(self.state))
		self._patch_object(listings, "is_custody_serial", return_value=False)
		self._patch_object(listings, "effective_serial_price", return_value=1200.0)
		self._patch_object(listings, "serial_shelf_state",
			side_effect=lambda _sn, **_kw: self.shelf)
		# Guard 13 asks the gallery for candidate paths. It resolves no bytes, so
		# a stubbed answer here is enough — and the gun under test is one that
		# HAS photos, so every other guard gets its turn.
		self._patch_object(mapping, "candidate_image_paths",
			return_value=["/files/main.jpg"])
		self._patch("frappe.db.get_value", return_value=0)
		# A fully configured store: sandbox on, the three cheque/money-order
		# methods stored. Guard 2 resolves both `sandbox_mode` and the payment
		# methods out of `tabSingles`, so this drives the real read path rather
		# than short-circuiting it.
		self._patch("frappe.db.sql", side_effect=fx.singles_sql(
			sandbox_mode=1, pay_certified_check=1, pay_usps_money_order=1,
			pay_money_order=1))
		# The single most important assertion in this file: a refusal must never
		# get as far as building a client.
		self.from_settings = self._patch_object(
			GunBrokerClient, "from_settings", return_value=MagicMock())

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, **kw):
		patcher = patch.object(target, attr, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _keys(self, patcher):
		"""Site-config DevKeys for the rest of the test (inner overrides win)."""
		patcher.start()
		self.addCleanup(patcher.stop)

	def _refusal(self, **kw):
		result = listings.check_push_guards(
			"SN-001", settings=self.settings, context=self.context, **kw)
		return result

	def _assert_refused(self, reason, **kw):
		result = self._refusal(**kw)
		self.assertIsNotNone(result, f"expected {reason}, got a clean pass")
		self.assertEqual(result["skipped"], reason)
		self.assertFalse(result["ok"])
		self.assertTrue(result["message"].strip(),
			"every refusal needs a sentence somebody can act on")
		self.assertFalse(self.from_settings.called,
			f"guard {reason} built a GunBroker client — a refusal must cost no "
			f"HTTP call and read no credential")
		return result

	# --- the happy path this whole file departs from ------------------------

	def test_a_listable_gun_passes_every_guard(self):
		self.assertIsNone(self._refusal())

	# --- 1..16 --------------------------------------------------------------

	def test_01_integration_switched_off(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled",
				return_value=False):
			self._assert_refused(listings.DISABLED)

	def test_02_missing_configuration_names_what_is_missing(self):
		self.settings = fx.settings_stub(category_map=CATEGORY_MAP, postal_code="")
		result = self._assert_refused(listings.NOT_CONFIGURED)
		self.assertIn("postal code", result["message"])

	def test_02_missing_sandbox_key_is_never_covered_by_the_production_key(self):
		self._keys(fx.dev_keys(production="prod-key", sandbox=""))
		result = self._assert_refused(listings.NOT_CONFIGURED)
		self.assertIn("sandbox DevKey", result["message"])
		self.assertIn("gunbroker_sandbox_dev_key", result["message"],
			"the key is not on the form any more — say where it goes")

	def test_02_no_payment_method_is_a_configuration_gap(self):
		"""A listing offering no way to pay costs a listing fee and produces
		nothing. Cheques and money orders are the only route this phase accepts,
		so all three being off means the store cannot sell at all — fail closed
		rather than publish it.

		Reaching this takes three EXPLICIT zeros: the defaults are on, so an
		operator has to have unticked them.
		"""
		with patch("frappe.db.sql", side_effect=fx.singles_sql(
				sandbox_mode=1, pay_certified_check=0, pay_usps_money_order=0,
				pay_money_order=0)):
			result = self._assert_refused(listings.NOT_CONFIGURED)
		self.assertIn("payment method", result["message"])

	def test_02_an_unconfigured_store_still_gets_the_default_payment_methods(self):
		"""At the guard: with only the sandbox row stored, the
		three cheque/money-order defaults must still apply — otherwise this guard
		would refuse every push on a freshly migrated site."""
		with patch("frappe.db.sql", side_effect=fx.singles_sql(sandbox_mode=1)):
			self.assertIsNone(self._refusal())

	def test_03_not_active(self):
		self.context["serial"]["status"] = "Delivered"
		self._assert_refused(listings.NOT_ACTIVE)

	def test_04_customer_transfer_gun(self):
		with patch.object(listings, "is_custody_serial", return_value=True):
			result = self._assert_refused(listings.TRANSFER_CUSTODY)
		self.assertIn("customer", result["message"].lower())

	def test_04_custody_outranks_a_mere_hold(self):
		"""Both are true; the operator needs the one that is not fixable by
		releasing something."""
		self.shelf = {"sellable": False, "reason": "counter_hold", "detail": {}}
		with patch.object(listings, "is_custody_serial", return_value=True):
			self._assert_refused(listings.TRANSFER_CUSTODY)

	def test_an_antique_is_listable_under_an_antique_category(self):
		# Pre-1899: no bound-book entry and no FFL at the far end. It lists with
		# IsFFLRequired off and retires through a stock issue, so nothing here
		# refuses it — provided the store has said which Antique category.
		self.context["item"]["is_antique"] = 1
		self.settings = fx.settings_stub(category_map=CATEGORY_MAP + [frappe._dict(
			{"firearm_type": "Revolver", "is_cr_eligible": 0, "is_antique": 1,
				"category_id": 3021})])
		self.assertIsNone(self._refusal())

	def test_an_antique_with_only_a_modern_category_is_refused_on_category(self):
		# Never the modern row: an antique in a modern category with "no FFL
		# required" on it is the listing GunBroker's policy team looks for.
		self.context["item"]["is_antique"] = 1
		result = self._assert_refused(listings.NO_CATEGORY)
		self.assertIn("antique", result["message"].lower())

	def test_every_nfa_type_is_listable(self):
		# Lists and transfers like any other gun; the Form 3/4 is filed in ATF
		# eForms, outside this system (UD-14).
		types = ("Silencer", "Short Barreled Rifle", "Short Barreled Shotgun",
			"Machine Gun", "Destructive Device", "Any Other Weapon")
		self.settings = fx.settings_stub(category_map=[frappe._dict(
			{"firearm_type": t, "is_cr_eligible": 0, "category_id": 3100 + i})
			for i, t in enumerate(types)])
		for ftype in types:
			with self.subTest(ftype):
				self.context["serial"]["firearm_type"] = ftype
				self.context["item"]["firearm_type"] = ftype
				self.assertIsNone(self._refusal())

	def test_05_a_consigned_gun_is_listable(self):
		with patch("frappe.db.get_value", return_value=1):
			self.assertIsNone(self._refusal())

	def test_06_uniform_priced_model(self):
		self.context["item"]["uniform_sell_price"] = 1
		self._assert_refused(listings.UNIFORM_ITEM)

	def test_07_hand_delisted(self):
		self.state["gb_delisted"] = 1
		self._assert_refused(listings.DELISTED)

	def test_07_an_explicit_relist_lifts_that_one_sentinel(self):
		self.state["gb_delisted"] = 1
		self.assertIsNone(self._refusal(relist=True))

	def test_07_a_relist_cannot_lift_custody(self):
		"""`relist` answers "may a machine republish this?". It cannot make a
		customer's gun the store's property."""
		self.state["gb_delisted"] = 1
		with patch.object(listings, "is_custody_serial", return_value=True):
			self._assert_refused(listings.TRANSFER_CUSTODY, relist=True)

	def test_07_a_relist_cannot_make_an_unpriced_gun_priced(self):
		self.state["gb_delisted"] = 1
		with patch.object(listings, "effective_serial_price", return_value=0.0):
			self._assert_refused(listings.UNPRICED, relist=True)

	def test_08_unpriced(self):
		with patch.object(listings, "effective_serial_price", return_value=0.0):
			result = self._assert_refused(listings.UNPRICED)
		self.assertIn("$0", result["message"])

	def test_09_held_by_another_channel_names_the_holder(self):
		self.shelf = {"sellable": False, "reason": "channel_reserved",
			"detail": {"channel": "woo", "ref": "WOO-ORD-00042"}}
		result = self._assert_refused(listings.NOT_SELLABLE)
		self.assertIn("woo", result["message"])
		self.assertIn("WOO-ORD-00042", result["message"])

	def test_09_a_counter_hold_names_the_order(self):
		self.shelf = {"sellable": False, "reason": "counter_hold",
			"detail": {"ref": "ACC-SINV-0001"}}
		result = self._assert_refused(listings.NOT_SELLABLE)
		self.assertIn("ACC-SINV-0001", result["message"])

	def test_10_no_category(self):
		self.context["serial"]["firearm_type"] = "Receiver"
		self.context["item"]["firearm_type"] = "Receiver"
		result = self._assert_refused(listings.NO_CATEGORY)
		self.assertIn("Category", result["message"])

	def test_11_no_shipping_cost(self):
		self.settings = fx.settings_stub(
			category_map=CATEGORY_MAP, long_gun_ship_cost=0, handgun_ship_cost=0)
		self._assert_refused(listings.NO_SHIP_COST)

	def test_11_free_shipping_needs_no_rate(self):
		self.settings = fx.settings_stub(
			category_map=CATEGORY_MAP, long_gun_ship_cost=0, handgun_ship_cost=0)
		self.context["serial"]["gb_free_shipping"] = 1
		self.assertIsNone(self._refusal())

	def test_08a_an_auction_without_terms_is_refused(self):
		self.context["serial"]["gb_listing_type"] = "Auction"
		self.context["serial"]["gb_starting_bid"] = 0
		result = self._assert_refused(listings.BAD_LISTING_TERMS)
		self.assertIn("starting bid", result["message"])

	def test_08a_an_auction_with_terms_passes(self):
		self.context["serial"].update({
			"gb_listing_type": "Auction", "gb_starting_bid": 900,
			"gb_auction_duration": 7, "gb_auction_auto_relist": 2})
		self.assertIsNone(self._refusal())

	def test_08_a_fixed_price_of_its_own_satisfies_the_price_guard(self):
		"""No POS price, but a price typed for this listing."""
		with patch.object(listings, "effective_serial_price", return_value=0):
			self.context["serial"]["gb_list_price"] = 1500
			self.assertIsNone(self._refusal())

	def test_11_a_rate_on_the_gun_satisfies_the_guard(self):
		"""No default configured, but this gun has a rate of its own."""
		self.settings = fx.settings_stub(
			category_map=CATEGORY_MAP, long_gun_ship_cost=0, handgun_ship_cost=0)
		self.context["serial"]["gb_ship_cost"] = 50
		self.assertIsNone(self._refusal())

	def test_13_no_photos_costs_no_disk_or_network_read(self):
		"""GunBroker itself lists guns with no photos; this is OUR rule. Answered
		from the gallery rows alone — reading the bytes would mean disk IO and,
		for a gallery row holding an absolute URL, an outbound request, which a
		refusal is contracted not to cost."""
		with patch.object(mapping, "candidate_image_paths", return_value=[]), \
				patch.object(mapping, "picture_parts") as read_bytes:
			result = self._assert_refused(listings.NO_IMAGES)
		self.assertFalse(read_bytes.called,
			"the guard read the photo bytes to decide there were none")
		self.assertIn("JPG", result["message"])

	def test_12_banned_title_phrase_is_caught_before_it_is_sent(self):
		"""GunBroker's own rejection quotes the policy page and never says which
		words offended it, so this is the only place an operator learns."""
		self.context["serial"]["item_name"] = "Test DO NOT BUY probe listing"
		result = self._assert_refused(listings.BANNED_TITLE)
		self.assertIn("do not buy", result["message"])


class GuardsThatNeedTheWholePush(FrappeTestCase):
	"""The last four guards sit after the payload is built, so they are reached
	through `push_serial` rather than `check_push_guards`."""

	def setUp(self):
		self.settings = fx.settings_stub(category_map=CATEGORY_MAP)
		self.state = {
			"item_code": "COLT-PYTHON", "gb_item_id": "", "gb_item_history": "",
			"gb_delisted": 0, "gb_push_pending": 0, "gb_sold_unknown": 0,
			"gb_push_attempted_at": None,
		}
		self.pictures = {"parts": [("a.jpg", b"x", "image/jpeg")], "warnings": []}

		for target, kw in (
			("frappe.db.set_value", {}),
			("frappe.db.set_single_value", {}),
		):
			patcher = patch(target, **kw)
			patcher.start()
			self.addCleanup(patcher.stop)

		self._patch_object(listings, "_settings", side_effect=lambda: self.settings)
		self._patch_object(listings, "check_push_guards", return_value=None)
		self._patch_object(mapping, "listing_context", return_value={})
		self._patch_object(mapping, "picture_parts",
			side_effect=lambda *a, **k: self.pictures)
		self._patch_object(mapping, "build_listing_payload",
			return_value={"payload": {"Title": "x"}, "warnings": []})
		self._patch_object(listings, "serial_state",
			side_effect=lambda _sn, **_kw: frappe._dict(self.state))
		self._patch_object(listings, "commit")
		self.from_settings = self._patch_object(
			GunBrokerClient, "from_settings", return_value=MagicMock())

	def _patch_object(self, target, attr, **kw):
		patcher = patch.object(target, attr, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def test_13_no_usable_photos(self):
		"""GunBroker itself lists guns with no photos. This is our rule — an
		unillustrated used firearm does not sell and the listing fee is charged
		either way. Do not "fix" this by deleting it."""
		self.pictures = {"parts": [], "warnings": ["image_format_skipped"]}
		result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.NO_IMAGES)
		self.assertFalse(self.from_settings.called)

	def test_14_already_listed(self):
		self.state["gb_item_id"] = "16087230"
		result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.ALREADY_LISTED)
		self.assertEqual(result["gb_item_id"], "16087230")
		self.assertFalse(self.from_settings.called)

	def test_15_possibly_already_sold_costs_no_http_call(self):
		"""Fail-closed on a LOCAL flag. Whether GunBroker is reachable has no
		bearing on whether this gun might already be somebody else's."""
		self.state["gb_sold_unknown"] = 1
		result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.SOLD_UNKNOWN)
		self.assertFalse(self.from_settings.called)

	def test_14_already_listed_is_checked_before_the_scan(self):
		"""Both true at once: the answer must be the one that needs no network.

		Otherwise the cheapest, most common refusal in the matrix would spend an
		HTTP round trip to arrive at a conclusion already sitting in the row.
		"""
		self.state["gb_item_id"] = "16087230"
		self.state["gb_push_pending"] = 1
		result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.ALREADY_LISTED)
		self.assertFalse(self.from_settings.called)

	def test_15_sold_unknown_is_checked_before_the_scan(self):
		self.state["gb_sold_unknown"] = 1
		self.state["gb_push_pending"] = 1
		result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.SOLD_UNKNOWN)
		self.assertFalse(self.from_settings.called)

	def test_14_a_relist_can_never_lift_already_listed(self):
		"""The blocker r1 found.

		`relist` is a public parameter of the whitelisted method, which the MCP
		tool and the import skill both call. Letting it through here would post a
		second listing for a gun that already has one — on a marketplace that
		enforces no uniqueness and gives a seller no way to cancel a sale, so the
		cleanup is a manual deletion at best and a refund and an apology at worst.
		A re-list has nothing to bypass anyway: its legal starting point is an
		ENDED or SOLD gun, whose item number is already empty.
		"""
		self.state["gb_item_id"] = "16087230"
		self.state["gb_delisted"] = 1
		result = listings.push_serial("SN-001", relist=True)
		self.assertEqual(result["skipped"], listings.ALREADY_LISTED)
		self.assertEqual(result["gb_item_id"], "16087230")
		self.assertFalse(self.from_settings.called,
			"a client was built — the refusal should cost nothing at all")

	def test_16_an_unconfirmed_attempt_is_the_only_guard_that_may_call_out(self):
		with patch.object(listings, "recover_item_id",
				return_value={"state": "miss", "item_id": None, "ids": [], "sold": False}):
			self.state["gb_push_pending"] = 1
			result = listings.push_serial("SN-001")
		self.assertEqual(result["skipped"], listings.PUSH_IN_FLIGHT)
		self.assertTrue(self.from_settings.called,
			"guard 16 is the documented exception: it scans GunBroker by SKU")
