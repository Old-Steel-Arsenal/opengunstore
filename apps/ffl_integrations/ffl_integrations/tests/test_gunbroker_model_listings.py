"""Putting a uniform-price gun MODEL on GunBroker (R3-17, 18, 26's server side).

The listing side of the model-level project: until now POS could not list a gun
whose price is the same for every unit, because nothing could say how many were
for sale, which licence the buyer needs, or which serial to hand over. These tests
pin the parts that decide whether a listing may exist and what it says:

* ``TheModelPayload`` — one listing standing for N guns: a quantity, the licence
  requirement (off only for an antique), the bare item code as SKU, no serial, the
  web store's price today, paid promotions only when a person asked for them.
* ``TheModelGuards`` — each reason a model does not go up, in the order the operator
  hears them, and that none of them builds a client.
* ``AGunPricedOneByOneStaysRefused`` — the other kind of firearm keeps its refusal,
  and a relist can no longer slip a uniform-price gun's serial past the per-gun matrix.
* ``TheSendDialogsServerSide`` — what the dialog is offered and what it may record.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ffl_integrations import hooks
from ffl_integrations.gunbroker import client_api, item_listings, listings, mapping
from ffl_integrations.gunbroker.client import GunBrokerClient
from ffl_integrations.tests import gunbroker_fixtures as fx
from ffl_integrations.tests.gb_model_fixtures import AMMO, MODEL, SINGLE
from ffl_integrations.tests.gb_model_listing_fixtures import PISTOL_ROW, ModelListingCase

RIFLE_CR_ROW = {"item_group": "", "firearm_type": "Rifle", "category_id": 3025,
	"category_name": "Curios & Relics", "is_cr_eligible": 1, "is_antique": 0}
RIFLE_ROW = {"item_group": "", "firearm_type": "Rifle", "category_id": 3024,
	"category_name": "Semi Auto Rifles", "is_cr_eligible": 0, "is_antique": 0}
ANTIQUE_ROW = {"item_group": "", "firearm_type": "Rifle", "category_id": 3030,
	"category_name": "Antique Rifles", "is_cr_eligible": 0, "is_antique": 1}


def _map(*rows):
	return [frappe._dict(r) for r in rows]


def _item(**over) -> frappe._dict:
	row = {
		"item_code": MODEL, "item_name": "CZ 75 Grade A", "item_group": "Pistol",
		"description": "<p>Surplus CZ 75.</p>", "is_firearm": 1, "uniform_sell_price": 1,
		"is_used": 1, "is_antique": 0, "firearm_type": "Pistol", "year_made": 0,
		"upc": "", "mpn": "", "standard_rate": 499.0, "sale_price": 0, "sale_from": None,
		"sale_to": None, "gb_category_id": 0, "gb_ship_cost": 0, "restricted_states": "",
		"image": "", "gb_item_id": "", "gb_delisted": 0, "gb_qty_pushed": 0,
		"gb_featured": 0, "gb_showcase": 0, "gb_sponsored_onsite": 0, "gb_view_counter": 0,
	}
	row.update(over)
	return frappe._dict(row)


class TheModelPayload(FrappeTestCase):
	def _build(self, *, quantity=3, premium=False, settings=None, **over):
		settings = settings or fx.settings_stub(category_map=_map(PISTOL_ROW))
		with patch.object(mapping, "listing_flags", return_value=fx.flags_stub()):
			return mapping.build_model_listing_payload(
				MODEL, quantity=quantity, premium=premium, settings=settings,
				context={"item": _item(**over)})

	def test_it_stands_for_a_quantity_of_guns_and_asks_for_a_licence(self):
		payload = self._build(quantity=6)["payload"]
		self.assertEqual(payload["Quantity"], 6)
		self.assertIs(payload["IsFFLRequired"], True)
		self.assertEqual(payload["CategoryID"], 3026)
		self.assertEqual(payload["FixedPrice"], 499.0)

	def test_its_sku_is_the_bare_item_code_and_it_names_no_serial(self):
		"""The order side reads "no ::serial" as "a unit of a model" and files a claim;
		a SerialNumber would also advertise one particular gun."""
		payload = self._build()["payload"]
		self.assertEqual(payload["SKU"], MODEL)
		self.assertNotIn("SerialNumber", payload)

	def test_an_antique_asks_for_no_licence_and_lists_as_used(self):
		settings = fx.settings_stub(category_map=_map(ANTIQUE_ROW))
		payload = self._build(settings=settings, firearm_type="Rifle", is_antique=1,
			is_used=0)["payload"]
		self.assertIs(payload["IsFFLRequired"], False)
		self.assertEqual(payload["Condition"], mapping.CONDITION_USED)
		self.assertEqual(payload["CategoryID"], 3030)

	def test_the_condition_follows_the_item_not_a_per_gun_choice(self):
		self.assertEqual(self._build(is_used=0)["payload"]["Condition"],
			mapping.CONDITION_FACTORY_NEW)
		self.assertEqual(self._build(is_used=1)["payload"]["Condition"],
			mapping.CONDITION_USED)

	def test_the_price_is_the_web_stores_price_today_promotion_included(self):
		running = dict(sale_price=399, sale_from=add_days(nowdate(), -2),
			sale_to=add_days(nowdate(), 2))
		self.assertEqual(self._build(**running)["payload"]["FixedPrice"], 399.0)
		ended = dict(sale_price=399, sale_from=add_days(nowdate(), -9),
			sale_to=add_days(nowdate(), -1))
		self.assertEqual(self._build(**ended)["payload"]["FixedPrice"], 499.0)
		not_yet = dict(sale_price=399, sale_from=add_days(nowdate(), 3), sale_to=None)
		self.assertEqual(self._build(**not_yet)["payload"]["FixedPrice"], 499.0)

	def test_a_sale_at_or_above_the_regular_price_is_not_a_sale(self):
		"""The same rule the register and WooCommerce apply."""
		self.assertEqual(self._build(sale_price=520)["payload"]["FixedPrice"], 499.0)

	def test_shipping_is_the_handgun_or_long_gun_default_by_type_and_its_own_wins(self):
		self.assertEqual(self._build()["payload"]["ShippingClassCosts"], {"Ground": 34.99})
		rifle = fx.settings_stub(category_map=_map(RIFLE_ROW))
		self.assertEqual(self._build(settings=rifle, firearm_type="Rifle")
			["payload"]["ShippingClassCosts"], {"Ground": 45.0})
		self.assertEqual(self._build(gb_ship_cost=12.5)["payload"]["ShippingClassCosts"],
			{"Ground": 12.5})

	def test_paid_promotions_only_when_a_person_asked_for_them(self):
		ticked = dict(gb_featured=1, gb_view_counter=1)
		self.assertNotIn("PremiumFeatures", self._build(premium=False, **ticked)["payload"],
			"a machine relist must not spend the operator's money")
		self.assertEqual(self._build(premium=True, **ticked)["payload"]["PremiumFeatures"],
			{"IsFeaturedItem": True, "HasViewCounter": True})
		self.assertNotIn("PremiumFeatures", self._build(premium=True)["payload"])

	def test_the_items_own_category_outranks_the_map(self):
		self.assertEqual(self._build(gb_category_id=4242)["payload"]["CategoryID"], 4242)

	def test_the_items_state_restrictions_replace_the_account_default(self):
		payload = self._build(restricted_states="CA,MA")["payload"]
		self.assertIs(payload["UseDefaultExcludeStates"], False)
		self.assertEqual(payload["ExcludeStates"], "CA,MA")

	def test_a_year_the_map_files_by_age_decides_a_relic_category(self):
		settings = fx.settings_stub(category_map=_map(RIFLE_ROW, RIFLE_CR_ROW))
		self.assertEqual(self._build(settings=settings, firearm_type="Rifle",
			year_made=1943)["payload"]["CategoryID"], 3025)
		self.assertEqual(self._build(settings=settings, firearm_type="Rifle",
			year_made=2015)["payload"]["CategoryID"], 3024)

	def test_what_cannot_be_built_is_refused_rather_than_guessed(self):
		for over, why in (
				({"quantity": 0}, "quantity"), ({"standard_rate": 0}, "price"),
				({"firearm_type": "Shotgun"}, "category"), ({"gb_ship_cost": 0}, None)):
			if why is None:
				settings = fx.settings_stub(category_map=_map(PISTOL_ROW),
					handgun_ship_cost=0, long_gun_ship_cost=0)
				with self.assertRaises(ValueError):
					self._build(settings=settings)
				continue
			with self.subTest(why=why), self.assertRaises(ValueError):
				self._build(**over)
		relic = fx.settings_stub(category_map=_map(RIFLE_ROW, RIFLE_CR_ROW))
		with self.assertRaises(ValueError):
			self._build(settings=relic, firearm_type="Rifle", year_made=0)

	def test_the_ammunition_payload_is_untouched(self):
		"""The shared core serves both kinds; the quantity listing of a box of ammo
		still asks for no licence, no serial and no promotion."""
		settings = fx.settings_stub(category_map=_map({
			"item_group": "Ammunition", "firearm_type": "", "category_id": 3049,
			"category_name": "Handgun Ammunition", "is_cr_eligible": 0, "is_antique": 0}))
		item = _item(item_code=AMMO, is_firearm=0, uniform_sell_price=0,
			item_group="Ammunition", firearm_type="", gb_featured=1)
		with patch.object(mapping, "listing_flags", return_value=fx.flags_stub()):
			payload = mapping.build_item_listing_payload(
				AMMO, quantity=4, settings=settings, context={"item": item})["payload"]
		self.assertIs(payload["IsFFLRequired"], False)
		self.assertEqual((payload["SKU"], payload["Quantity"]), (AMMO, 4))
		self.assertNotIn("SerialNumber", payload)
		self.assertNotIn("PremiumFeatures", payload)
		self.assertEqual(payload["ShippingClassCosts"], {"Ground": 19.99})


class TheModelGuards(FrappeTestCase):
	"""Each test trips exactly one guard, from an otherwise listable model."""

	def setUp(self):
		self.settings = fx.settings_stub(category_map=_map(PISTOL_ROW))
		self.item = _item()
		self.state = {
			"item_code": MODEL, "item_name": "CZ 75 Grade A", "item_group": "Pistol",
			"is_firearm": 1, "uniform_sell_price": 1, "disabled": 0, "has_serial_no": 1,
			"standard_rate": 499.0, "gb_item_id": "", "gb_item_history": "",
			"gb_delisted": 0, "gb_end_pending": 0, "gb_push_pending": 0,
		}
		self.live_serial = ""
		self.dropship = False
		self.sellable = 3
		self._patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True)
		self._patch_object(listings, "_config_gap", return_value="")
		self._patch_object(item_listings, "uniform_sellable_units",
			side_effect=lambda _code: self._sellable())
		self._patch_object(item_listings, "_live_serial_listing",
			side_effect=lambda _code: self.live_serial)
		self._patch_object(item_listings, "_dropship_priced",
			side_effect=lambda _code: self.dropship)
		self._patch_object(mapping, "item_candidate_image_paths", return_value=["/files/a.jpg"])
		self.from_settings = self._patch_object(
			GunBrokerClient, "from_settings", return_value=MagicMock())

	def _sellable(self):
		if isinstance(self.sellable, Exception):
			raise self.sellable
		return self.sellable

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

	def _check(self, **kw):
		return item_listings.check_model_push_guards(
			MODEL, settings=self.settings, state=self.state,
			context={"item": self.item}, **kw)

	def _assert_refused(self, reason, **kw):
		result = self._check(**kw)
		self.assertIsNotNone(result, f"expected {reason}, got a clean pass")
		self.assertEqual(result["skipped"], reason)
		self.assertFalse(result["ok"])
		self.assertTrue(result["message"].strip())
		self.assertFalse(self.from_settings.called,
			f"{reason} built a client — a refusal must cost no HTTP call")

	def test_a_listable_model_passes_and_builds_no_client(self):
		self.assertIsNone(self._check())
		self.assertFalse(self.from_settings.called)

	def test_switched_off(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=False):
			self._assert_refused(listings.DISABLED)

	def test_not_configured(self):
		with patch.object(listings, "_config_gap", return_value="seller password"):
			self._assert_refused(listings.NOT_CONFIGURED)

	def test_a_gun_priced_one_by_one_is_not_a_model(self):
		self.state["uniform_sell_price"] = 0
		self._assert_refused(item_listings.IS_FIREARM)

	def test_disabled(self):
		self.state["disabled"] = 1
		self._assert_refused(listings.NOT_SELLABLE)

	def test_a_drop_ship_priced_item_is_refused(self):
		self.dropship = True
		self._assert_refused(listings.DROPSHIP_PRICED)

	def test_taken_down_by_hand_needs_a_person(self):
		self.state["gb_delisted"] = 1
		self._assert_refused(listings.DELISTED)

	def test_an_explicit_relist_lifts_that_one_sentinel_only(self):
		self.state["gb_delisted"] = 1
		self.assertIsNone(self._check(relist=True))
		self.state["gb_item_id"] = "16087299"
		self._assert_refused(listings.ALREADY_LISTED, relist=True)

	def test_already_listed(self):
		self.state["gb_item_id"] = "16087299"
		self._assert_refused(listings.ALREADY_LISTED)

	def test_an_attempt_nobody_knows_the_outcome_of(self):
		self.state["gb_push_pending"] = 1
		self._assert_refused(listings.PUSH_IN_FLIGHT)

	def test_a_gun_of_the_model_already_listed_by_serial(self):
		self.live_serial = "SN-123"
		self._assert_refused(listings.PER_SERIAL_LIVE)
		self.assertIn("SN-123", self._check()["message"])

	def test_unpriced(self):
		self.item["standard_rate"] = 0
		self._assert_refused(listings.UNPRICED)

	def test_nothing_free_to_sell(self):
		self.sellable = 0
		self._assert_refused(listings.NOT_SELLABLE)

	def test_a_pool_that_cannot_be_read_is_a_refusal_not_a_zero_or_a_guess(self):
		self.sellable = RuntimeError("woo is down")
		with patch.object(item_listings, "safe_log_error"):
			self._assert_refused(listings.QUANTITY_UNREADABLE)

	def test_no_shipping_cost(self):
		self.settings = fx.settings_stub(category_map=_map(PISTOL_ROW), handgun_ship_cost=0)
		self._assert_refused(listings.NO_SHIP_COST)

	def test_no_category(self):
		self.settings = fx.settings_stub(category_map=[])
		self._assert_refused(listings.NO_CATEGORY)

	def test_a_type_filed_by_age_with_no_year_is_ambiguous(self):
		self.settings = fx.settings_stub(category_map=_map(RIFLE_ROW, RIFLE_CR_ROW))
		self.item.update(firearm_type="Rifle", year_made=0)
		self._assert_refused(listings.CATEGORY_AMBIGUOUS)

	def test_a_year_or_an_override_or_an_antique_resolves_the_ambiguity(self):
		self.settings = fx.settings_stub(category_map=_map(RIFLE_ROW, RIFLE_CR_ROW, ANTIQUE_ROW))
		self.item.update(firearm_type="Rifle", year_made=1943)
		self.assertIsNone(self._check())
		self.item.update(year_made=0, gb_category_id=3024)
		self.assertIsNone(self._check())
		self.item.update(gb_category_id=0, is_antique=1)
		self.assertIsNone(self._check())

	def test_a_banned_title(self):
		self.item["item_name"] = "CZ 75 do not buy"
		self._assert_refused(listings.BANNED_TITLE)

	def test_no_photo(self):
		with patch.object(mapping, "item_candidate_image_paths", return_value=[]):
			self._assert_refused(listings.NO_IMAGES)

	def test_an_auction_is_refused_a_model_lists_at_a_fixed_price(self):
		self._assert_refused(listings.BAD_LISTING_TERMS, listing_type="Auction")

	def test_the_order_is_the_priority_order(self):
		"""Several things wrong at once: the operator hears the first, most
		actionable one."""
		self.state.update(disabled=1, gb_item_id="1")
		self.dropship = True
		self.assertEqual(self._check()["skipped"], listings.NOT_SELLABLE)
		self.state["disabled"] = 0
		self.assertEqual(self._check()["skipped"], listings.DROPSHIP_PRICED)
		self.dropship = False
		self.assertEqual(self._check()["skipped"], listings.ALREADY_LISTED)

	def test_the_item_guard_hands_a_model_to_the_model_matrix(self):
		"""`push_item`, the button and the machine relist all enter through
		check_item_push_guards: a uniform-price gun must reach this matrix there."""
		self.sellable = 0
		result = item_listings.check_item_push_guards(
			MODEL, settings=self.settings, state=self.state, context={"item": self.item})
		self.assertEqual(result["skipped"], listings.NOT_SELLABLE)
		self.assertIn("free to sell", result["message"])


class AGunPricedOneByOneStaysRefused(FrappeTestCase):
	def setUp(self):
		self.settings = fx.settings_stub(category_map=_map(PISTOL_ROW))
		patcher = patch.object(listings, "_config_gap", return_value="")
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_the_item_guard_still_refuses_it(self):
		result = item_listings.check_item_push_guards(
			SINGLE, settings=self.settings,
			state={"item_code": SINGLE, "is_firearm": 1, "uniform_sell_price": 0})
		self.assertEqual(result["skipped"], item_listings.IS_FIREARM)

	def test_a_missing_credential_is_a_refusal_dict_not_a_string(self):
		"""The config check used to hand its message string straight back as the
		answer, which every caller then indexed as a dict."""
		with patch.object(listings, "_config_gap", return_value="seller password"):
			result = item_listings.check_item_push_guards(
				AMMO, settings=self.settings, state={"item_code": AMMO, "is_firearm": 0})
		self.assertEqual(result["skipped"], listings.NOT_CONFIGURED)
		self.assertIn("seller password", result["message"])

	def test_a_relist_no_longer_slips_a_uniform_serial_past_the_per_gun_matrix(self):
		"""Guard 6 used to be lifted by `relist`, which is meant for the hand-ended
		sentinel only: it put ONE gun of a model on GunBroker under its own SKU beside
		(or instead of) the model listing whose quantity that gun is part of."""
		context = {
			"serial": frappe._dict({"name": "SN-1", "item_code": MODEL, "item_name": "CZ",
				"status": "Active", "firearm_type": "Pistol", "year_made": 0}),
			"item": frappe._dict({"is_firearm": 1, "uniform_sell_price": 1,
				"firearm_type": "Pistol", "item_name": "CZ"}),
		}
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch.object(listings, "is_custody_serial", return_value=False), \
				patch.object(listings, "_has_field", return_value=False), \
				patch.object(listings.mapping, "listing_context", return_value=context):
			for relist in (False, True):
				with self.subTest(relist=relist):
					result = listings.check_push_guards(
						"SN-1", relist=relist, settings=self.settings, context=context)
					self.assertEqual(result["skipped"], listings.UNIFORM_ITEM)


class ASerialOfTheModelAlreadyListed(ModelListingCase):
	def test_any_live_pending_or_ending_serial_listing_blocks_the_model(self):
		gun = self.stock("PSL")
		self.assertEqual(item_listings._live_serial_listing(MODEL), "")
		for field, value in (("gb_item_id", "16087001"), ("gb_push_pending", 1),
				("gb_end_pending", 1)):
			with self.subTest(field=field):
				frappe.db.set_value("Serial No", gun, field, value)
				self.assertEqual(item_listings._live_serial_listing(MODEL), gun)
				frappe.db.set_value("Serial No", gun, field, "" if field == "gb_item_id" else 0)
		self.assertEqual(item_listings._live_serial_listing(MODEL), "")

	def test_the_drop_ship_hook_is_asked_by_item(self):
		with patch.object(frappe, "get_hooks", return_value=["x.y.web_price"]), \
				patch.object(frappe, "get_attr", return_value=lambda code: 450.0):
			self.assertTrue(item_listings._dropship_priced(MODEL))
		with patch.object(frappe, "get_hooks", return_value=["x.y.web_price"]), \
				patch.object(frappe, "get_attr", return_value=lambda code: None):
			self.assertFalse(item_listings._dropship_priced(MODEL))
		with patch.object(frappe, "get_hooks", return_value=[]):
			self.assertFalse(item_listings._dropship_priced(MODEL))


class TheSendDialogsServerSide(ModelListingCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value("Item", MODEL, "gb_featured", 1, update_modified=False)

	def test_the_offer_shows_guns_price_category_shipping_and_recorded_promotions(self):
		self.stock_n(3)
		offer = item_listings.listing_offer(MODEL)
		self.assertTrue(offer["is_model"])
		self.assertEqual(offer["sellable"], 3)
		self.assertEqual((offer["price"], offer["regular_price"], offer["on_sale"]),
			(499.0, 499.0, False))
		self.assertEqual(offer["category"],
			{"id": 3026, "name": "Semi Auto Pistols", "ambiguous": False})
		self.assertEqual(offer["shipping"]["effective"], 34.99)
		self.assertEqual(offer["premium"], {"featured": True, "showcase": False,
			"sponsored_onsite": False, "view_counter": False})

	def test_the_offer_flags_a_running_promotion_and_when_it_ends(self):
		self.stock_n(1)
		frappe.db.set_value("Item", MODEL, {
			"sale_price": 399, "sale_from": add_days(nowdate(), -1),
			"sale_to": add_days(nowdate(), 5)}, update_modified=False)
		offer = item_listings.listing_offer(MODEL)
		self.assertTrue(offer["on_sale"])
		self.assertEqual((offer["price"], offer["regular_price"]), (399.0, 499.0))
		self.assertEqual(offer["sale_to"], add_days(nowdate(), 5))

	def test_a_pool_that_cannot_be_read_opens_the_dialog_with_no_number(self):
		with patch.object(item_listings, "uniform_sellable_units",
				side_effect=RuntimeError("down")):
			self.assertIsNone(item_listings.listing_offer(MODEL)["sellable"])

	def test_the_endpoint_is_stock_roles_only_and_returns_the_offer(self):
		with patch.object(client_api.frappe, "only_for") as only_for:
			self.stock_n(1)
			self.assertEqual(client_api.item_listing_offer(MODEL)["sellable"], 1)
		only_for.assert_called_once()

	def test_the_four_promotions_are_recorded_all_at_once_so_unticking_clears(self):
		item_listings.set_listing_spec(MODEL, {"type": "Fixed Price", "premium": {
			"showcase": 1, "view_counter": 1}})
		row = frappe.db.get_value("Item", MODEL,
			["gb_featured", "gb_showcase", "gb_sponsored_onsite", "gb_view_counter"],
			as_dict=True)
		self.assertEqual(tuple(row.values()), (0, 1, 0, 1))

	def test_a_spec_without_premium_keeps_what_is_recorded(self):
		item_listings.set_listing_spec(MODEL, {"type": "Fixed Price"})
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_featured"), 1)

	def test_an_unknown_promotion_is_refused_rather_than_silently_buying_nothing(self):
		with self.assertRaises(frappe.ValidationError):
			item_listings.set_listing_spec(MODEL, {"premium": {"billboard": 1}})

	def test_an_auction_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			item_listings.set_listing_spec(MODEL, {"type": "Auction", "starting_bid": 100})

	def test_a_title_price_or_condition_is_refused_not_half_honoured(self):
		for key in ("title", "price", "condition"):
			with self.subTest(key=key), self.assertRaises(frappe.ValidationError):
				item_listings.set_listing_spec(MODEL, {key: 1})

	def test_promotions_on_a_non_model_item_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			item_listings.set_listing_spec(AMMO, {"premium": {"featured": 1}})

	def test_send_is_the_one_call_that_buys_the_promotions(self):
		with patch.object(client_api.frappe, "only_for"), \
				patch.object(item_listings, "push_item", return_value={"ok": True}) as push:
			client_api.push_item_now(MODEL, relist=1, listing='{"premium": {"showcase": 1}}')
		push.assert_called_once_with(MODEL, relist=True, premium=True)
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_showcase"), 1)

	def test_a_refused_send_records_the_reason_on_the_item(self):
		with patch.object(client_api.frappe, "only_for"):
			result = client_api.push_item_now(MODEL)  # nothing on the shelf, GunBroker off
		self.assertFalse(result["ok"])
		self.assertTrue(frappe.db.get_value("Item", MODEL, "gb_sync_error"))


class TheModelListingIsWiredIn(FrappeTestCase):
	"""A dotted path in hooks.py is only checked when it fires; a rename lands in
	production silently. And the shared keys must stay lists so a merge appends."""

	def test_the_new_handlers_resolve(self):
		for path in (
				"ffl_integrations.gunbroker.doc_events.on_item_update",
				"ffl_integrations.gunbroker.doc_events.on_invoice_draft_change",
				"ffl_integrations.gunbroker.tasks.gb_model_price_sweep",
				"ffl_integrations.gunbroker.tasks.gb_reprice_item_task",
				"ffl_integrations.gunbroker.tasks.gb_end_item_listing_task"):
			self.assertTrue(callable(frappe.get_attr(path)), path)

	def test_the_item_hook_and_the_draft_ticket_hooks_are_registered_as_lists(self):
		self.assertEqual(hooks.doc_events["Item"]["on_update"],
			["ffl_integrations.gunbroker.doc_events.on_item_update"])
		pos = hooks.doc_events["POS Invoice"]
		self.assertIn("ffl_integrations.gunbroker.doc_events.on_invoice_draft_change",
			pos["on_update"])
		self.assertEqual(pos["on_trash"][0], "ffl_integrations.payments.api.block_locked_return_trash",
			"the Payroc guard stays first on a delete")
		self.assertIn("ffl_integrations.gunbroker.doc_events.on_invoice_draft_change",
			pos["on_trash"])

	def test_the_midnight_price_sweep_is_scheduled_after_the_sale_price_refresh(self):
		self.assertIn("ffl_integrations.gunbroker.tasks.gb_model_price_sweep",
			hooks.scheduler_events["cron"]["5 0 * * *"])
