"""Item-level GunBroker listings: ammunition, optics, parts.

The channel's second listing kind, and the rules worth pinning are the ones
that differ from a per-gun listing rather than the ones it inherits:

* a firearm must never reach it — a model-level listing that says no FFL is
  required is the one mistake here that is a compliance problem;
* the quantity is stock, it moves, and GunBroker rejects zero — so selling out
  ENDS the listing instead of pushing 0, and does so without setting the "a
  person took this down" flag, which is what lets a restock put it back;
* state restrictions on the Item replace the account default rather than adding
  to it, because that is the only shape GunBroker accepts.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from ffl_integrations.gunbroker import item_listings, listings, mapping
from ffl_integrations.gunbroker.client import GunBrokerError
from ffl_integrations.tests import gunbroker_fixtures as fx

ITEM = "AMMO-9MM-115"

# The shape the sandbox produced on 2026-08-29: a listing of four sold two, went
# inactive, and pointed at a live replacement holding the other two.
LISTED_ID = "16087300"
RELISTED_ID = "16087370"


def _item(**over):
	row = {
		"item_code": ITEM, "item_name": "9mm 115gr FMJ, 50rd box",
		"item_group": "Ammunition", "is_firearm": 0, "uniform_sell_price": 0,
		"disabled": 0, "standard_rate": 21.99, "description": "<p>Brass case.</p>",
		"is_used": 0, "is_antique": 0, "firearm_type": "", "year_made": 0,
		"upc": "", "gb_category_id": 0, "image": "", "restricted_states": "",
		"gb_item_id": "", "gb_item_history": "", "gb_delisted": 0,
		"gb_end_pending": 0, "gb_push_pending": 0, "gb_push_attempted_at": None,
		"gb_price_dirty": 0, "gb_qty_pushed": 0, "gb_sync_error": "",
	}
	row.update(over)
	return frappe._dict(row)


def _context(**over):
	return {"item": _item(**over)}


def _map(rows):
	return [frappe._dict(r) for r in rows]


AMMO_ROW = {"item_group": "Ammunition", "firearm_type": "", "category_id": 3049,
	"category_name": "Handgun Ammunition", "is_cr_eligible": 0, "is_antique": 0}
PISTOL_ROW = {"item_group": "", "firearm_type": "Pistol", "category_id": 3026,
	"category_name": "Semi Auto Pistols", "is_cr_eligible": 0, "is_antique": 0}


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------

class ResolvingAnItemCategory(FrappeTestCase):
	def test_the_item_group_row_supplies_the_category(self):
		self.assertEqual(mapping.resolve_item_category(
			item_override=0, item_group="Ammunition",
			category_map=_map([PISTOL_ROW, AMMO_ROW])), 3049)

	def test_the_items_own_override_wins_over_the_group_row(self):
		self.assertEqual(mapping.resolve_item_category(
			item_override=9999, item_group="Ammunition",
			category_map=_map([AMMO_ROW])), 9999)

	def test_an_unmapped_group_resolves_to_nothing(self):
		"""Zero means the caller refuses. Falling back to a generic category is
		how ammunition ends up filed under optics."""
		self.assertEqual(mapping.resolve_item_category(
			item_override=0, item_group="Holsters",
			category_map=_map([AMMO_ROW])), 0)

	def test_a_firearm_row_is_never_handed_to_an_item(self):
		"""The two resolvers share a table and must not share rows: a gun row
		carries a firearm type and no group, and matching on the empty string
		would give every unmapped item the first gun category in the list."""
		self.assertEqual(mapping.resolve_item_category(
			item_override=0, item_group="Ammunition",
			category_map=_map([PISTOL_ROW])), 0)

	def test_an_item_row_is_never_handed_to_a_gun(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Pistol", cr_eligible=False,
			category_map=_map([AMMO_ROW])), 0)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

class TheItemPayload(FrappeTestCase):
	def _build(self, *, quantity=4, settings=None, **over):
		return mapping.build_item_listing_payload(
			ITEM, quantity=quantity,
			settings=settings or fx.settings_stub(category_map=_map([AMMO_ROW])),
			context=_context(**over))["payload"]

	def test_it_never_asks_for_an_ffl(self):
		"""Forced false regardless of the Settings checkbox — nothing listed this
		way is a firearm, and asking the buyer for a licence would route a box of
		ammunition to a dealer for a transfer that does not exist."""
		settings = fx.settings_stub(category_map=_map([AMMO_ROW]), is_ffl_required=1)
		with patch.object(mapping, "listing_flags",
				return_value=fx.flags_stub(is_ffl_required=True)):
			payload = self._build(settings=settings)
		self.assertIs(payload["IsFFLRequired"], False)

	def test_an_item_listing_never_buys_a_paid_promotion(self):
		"""Promotions belong to the single-gun listing; an item row that happened
		to carry the same column names must not turn them on."""
		payload = self._build(gb_featured=1, gb_showcase=1, gb_sponsored_onsite=1,
			gb_view_counter=1)
		self.assertNotIn("PremiumFeatures", payload)

	def test_an_item_listing_keeps_the_condition_the_item_gives(self):
		"""The per-gun condition choice belongs to the single-gun listing."""
		self.assertEqual(self._build(is_used=0, gb_condition="2 New Old Stock")["Condition"], 1)
		self.assertEqual(self._build(is_used=1, gb_condition="1 Factory New")["Condition"], 3)

	def test_the_quantity_is_the_callers(self):
		self.assertEqual(self._build(quantity=7)["Quantity"], 7)

	def test_a_quantity_of_zero_is_refused_before_it_reaches_gunbroker(self):
		"""GunBroker answers 400 to Quantity 0 ("must be an integer greater than
		0"), so the listing is never built with one."""
		with self.assertRaises(ValueError):
			self._build(quantity=0)

	def test_the_sku_is_the_bare_item_code(self):
		"""No `::serial` — that suffix is what tells the order side a line sold
		one numbered gun."""
		self.assertEqual(self._build()["SKU"], ITEM)

	def test_the_shipping_rate_is_the_accessory_one(self):
		self.assertEqual(self._build()["ShippingClassCosts"], {"Ground": 19.99})

	def test_a_rate_of_its_own_beats_the_accessory_one(self):
		self.assertEqual(
			self._build(gb_ship_cost=8.5)["ShippingClassCosts"], {"Ground": 8.5})

	def test_an_unset_accessory_rate_refuses_the_listing(self):
		"""Zero is also what an unconfigured Currency field reads as, and "buyer
		pays a fixed $0" means shipping ammunition across the country for free."""
		settings = fx.settings_stub(category_map=_map([AMMO_ROW]), accessory_ship_cost=0)
		with self.assertRaises(ValueError):
			self._build(settings=settings)

	def test_the_items_restricted_states_replace_the_account_default(self):
		"""ExcludeStates is read only when UseDefaultExcludeStates is false, so
		the per-item list cannot be additive however much one would prefer it."""
		payload = self._build(restricted_states="CA,MA,IL")
		self.assertIs(payload["UseDefaultExcludeStates"], False)
		self.assertEqual(payload["ExcludeStates"], "CA,MA,IL")

	def test_without_item_states_the_account_default_still_applies(self):
		payload = self._build()
		self.assertIs(payload["UseDefaultExcludeStates"], True)
		self.assertNotIn("ExcludeStates", payload)

	def test_an_unpriced_item_is_refused(self):
		with self.assertRaises(ValueError):
			self._build(standard_rate=0)

	def test_an_unmapped_item_is_refused(self):
		settings = fx.settings_stub(category_map=_map([PISTOL_ROW]))
		with self.assertRaises(ValueError):
			self._build(settings=settings)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

class TheItemGuards(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.settings = fx.settings_stub(category_map=_map([AMMO_ROW]))
		self._patch("ffl_integrations.gunbroker.listings._settings",
			return_value=self.settings)
		self._patch("ffl_integrations.gunbroker.listings._config_gap", return_value=None)
		self.qty = self._patch(
			"ffl_integrations.gunbroker.item_listings.sellable_qty", return_value=4)
		self._patch("ffl_integrations.gunbroker.mapping.item_candidate_image_paths",
			return_value=["/files/ammo.jpg"])

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _check(self, **over):
		return item_listings.check_item_push_guards(
			ITEM, settings=self.settings, state=dict(_item(**over)),
			context=_context(**over))

	def test_a_clean_item_passes_every_guard(self):
		self.assertIsNone(self._check())

	def test_a_rate_of_its_own_satisfies_the_shipping_guard(self):
		"""No accessory default configured, but this model has a rate of its own."""
		self.settings = fx.settings_stub(category_map=_map([AMMO_ROW]), accessory_ship_cost=0)
		self.assertEqual(self._check()["skipped"], "no_ship_cost")
		self.assertIsNone(self._check(gb_ship_cost=8.5))

	def test_a_firearm_is_refused_first(self):
		"""Ahead of every other guard: an unpriced, out-of-stock gun must still
		report as a gun, because that is the answer that stops somebody fixing
		the price and trying again."""
		refusal = self._check(is_firearm=1, standard_rate=0)
		self.assertEqual(refusal["skipped"], item_listings.IS_FIREARM)

	def test_an_item_already_listed_is_refused(self):
		self.assertEqual(self._check(gb_item_id="16087230")["skipped"], "already_listed")

	def test_an_item_ended_by_hand_stays_down(self):
		self.assertEqual(self._check(gb_delisted=1)["skipped"], "delisted")

	def test_but_an_explicit_relist_lifts_that_one_flag(self):
		self.assertIsNone(item_listings.check_item_push_guards(
			ITEM, relist=True, settings=self.settings,
			state=dict(_item(gb_delisted=1)), context=_context(gb_delisted=1)))

	def test_an_unconfirmed_attempt_blocks_a_second_one(self):
		self.assertEqual(self._check(gb_push_pending=1)["skipped"], "push_in_flight")

	def test_nothing_in_stock_is_refused(self):
		self.qty.return_value = 0
		self.assertEqual(self._check()["skipped"], "not_sellable")

	def test_a_disabled_item_is_refused(self):
		self.assertEqual(self._check(disabled=1)["skipped"], "not_sellable")

	def test_an_item_with_no_photo_is_refused(self):
		with patch("ffl_integrations.gunbroker.mapping.item_candidate_image_paths",
				return_value=[]):
			self.assertEqual(self._check()["skipped"], "no_images")

	def test_a_banned_title_is_refused_with_the_phrase_named(self):
		"""The serial and model matrices had this guard and this one did not, so
		an accessory got GunBroker's unexplained policy 400 instead."""
		refusal = self._check(item_name="9mm practice ammo do not buy")
		self.assertEqual(refusal["skipped"], listings.BANNED_TITLE)
		self.assertIn("do not buy", refusal["message"])


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------

class SyncingTheQuantity(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.client = MagicMock()
		# Stated, not left to a MagicMock: the count is only recorded once the
		# listing it went to reads back alive, and a mock that shrugs would let
		# that check pass without ever running.
		self._live(True)
		self.state = self._patch(
			"ffl_integrations.gunbroker.item_listings.item_state")
		self.qty = self._patch(
			"ffl_integrations.gunbroker.item_listings.sellable_qty")
		self.set_value = self._patch("frappe.db.set_value")

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _live(self, active, *, relisted=0, gone=False, error=False):
		if error:
			answer = {"state": "error", "item": None}
		elif gone:
			answer = {"state": "gone", "item": None}
		else:
			answer = {"state": "ok", "item": {
				"isActive": active, "relistedAsItemID": relisted}}
		self.client.item_probe.return_value = answer

	def test_an_unlisted_item_does_nothing(self):
		self.state.return_value = dict(_item())
		self.qty.return_value = 4
		item_listings.sync_item_quantity(ITEM, client=self.client)
		self.client.update_listing.assert_not_called()

	def test_an_unchanged_count_costs_no_api_call(self):
		"""This runs behind every stock movement of every listed item, and most
		movements do not change the number GunBroker holds."""
		self.state.return_value = dict(_item(gb_item_id="16087230", gb_qty_pushed=4))
		self.qty.return_value = 4
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.client.update_listing.assert_not_called()
		self.assertTrue(result["unchanged"])

	def test_a_changed_count_is_sent_and_recorded(self):
		self.state.side_effect = [
			dict(_item(gb_item_id="16087230", gb_qty_pushed=4)),
			dict(_item(gb_item_id="16087230", gb_qty_pushed=4)),
		]
		self.qty.return_value = 3
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.client.update_listing.assert_called_once_with("16087230", {"Quantity": 3})
		self.assertEqual(result["quantity"], 3)
		written = self.set_value.call_args.args[2]
		self.assertEqual(written["gb_qty_pushed"], 3)

	def test_a_failing_re_read_after_the_put_keeps_the_record(self):
		"""The PUT already landed: a channel that cannot count on the re-read must not
		turn the job into an error that throws the gb_qty_pushed write away."""
		self.state.side_effect = [
			dict(_item(gb_item_id="16087230", gb_qty_pushed=4)),
			dict(_item(gb_item_id="16087230", gb_qty_pushed=4)),
		]
		self.qty.side_effect = [3, RuntimeError("woo registrant down")]
		with patch.object(item_listings, "safe_log_error") as logged:
			result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["quantity"], 3)
		self.assertEqual(self.set_value.call_args.args[2]["gb_qty_pushed"], 3)
		logged.assert_called_once()

	def test_selling_out_ends_the_listing_rather_than_pushing_zero(self):
		self.state.return_value = dict(_item(gb_item_id="16087230", gb_qty_pushed=1))
		self.qty.return_value = 0
		with patch.object(item_listings, "end_item_listing",
				return_value={"ok": True, "confirmed": True}) as end:
			result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.client.update_listing.assert_not_called()
		self.assertEqual(result["quantity"], 0)
		# delisted=False is the whole point: stock reaching zero is not a person
		# taking the listing down, so a restock may put it back.
		self.assertIs(end.call_args.kwargs["delisted"], False)

	def test_a_relisted_listing_mid_flight_is_not_recorded_against(self):
		"""The count would be written against a listing that no longer exists,
		suppressing the next push."""
		self.state.side_effect = [
			dict(_item(gb_item_id="16087230", gb_qty_pushed=4)),
			dict(_item(gb_item_id="99999999", gb_qty_pushed=4)),
		]
		self.qty.return_value = 3
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["skipped"], "reprice_unconfirmed")
		self.set_value.assert_not_called()

	# A failed quantity push must never raise: it runs inside a stock movement.
	def test_a_failed_push_is_reported_not_raised(self):
		self.state.return_value = dict(_item(gb_item_id="16087230", gb_qty_pushed=4))
		self.qty.return_value = 3
		self.client.update_listing.side_effect = RuntimeError("boom")
		with patch.object(item_listings, "log_sync_error"):
			result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertFalse(result.get("ok"))


class TheCountHasToLandOnTheLiveListing(FrappeTestCase):
	"""GunBroker answers 200 for updates it discards, and the update most likely
	to be discarded is one sent to a listing it replaced after a partial sale.

	Until the replacement is adopted, every stock movement writes the count to a
	dead number and records it as sent — so the live listing goes on selling
	stock nothing is decrementing. The hourly sweep repairs that, but an hour of
	it is an hour of overselling, and the answer is already in the reply to a
	read this makes anyway.
	"""

	def setUp(self):
		super().setUp()
		self.client = MagicMock()
		self.enqueued: list[dict] = []
		self.state = self._patch(
			"ffl_integrations.gunbroker.item_listings.item_state",
			return_value=dict(_item(gb_item_id=LISTED_ID, gb_qty_pushed=4)))
		self._patch("ffl_integrations.gunbroker.item_listings.sellable_qty",
			return_value=3)
		self.writes = self._patch("frappe.db.set_value")
		self._patch("frappe.enqueue",
			side_effect=lambda *a, **k: self.enqueued.append(k))

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _probe(self, answer):
		self.client.item_probe.return_value = answer

	def _ended(self, relisted=0):
		self._probe({"state": "ok", "item": {
			"isActive": False, "relistedAsItemID": relisted}})

	def test_a_count_sent_to_a_replaced_listing_is_not_recorded_as_sent(self):
		self._ended(relisted=int(RELISTED_ID))
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["skipped"], "reprice_unconfirmed")
		written = {}
		for call in self.writes.call_args_list:
			payload = call.args[2]
			if isinstance(payload, dict):
				written.update(payload)
		self.assertEqual(written.get("gb_qty_pushed", 0), 0,
			"recording the count we sent would tell the next movement there is "
			"nothing to send — the replacement has been told nothing")

	def test_the_replacement_is_adopted_and_the_count_queued_again(self):
		"""GunBroker named the successor in the same reply, so there is no reason
		to wait an hour for the sweep to find it by SKU."""
		self._ended(relisted=int(RELISTED_ID))
		item_listings.sync_item_quantity(ITEM, client=self.client)
		claimed = [c.args[2] for c in self.writes.call_args_list
			if isinstance(c.args[2], dict) and c.args[2].get("gb_item_id")]
		self.assertEqual(claimed[0]["gb_item_id"], RELISTED_ID)
		self.assertEqual(self.enqueued[0]["item_code"], ITEM)

	def test_an_ended_listing_with_no_successor_named_waits_for_the_sweep(self):
		"""A 0 here is not evidence that nothing was relisted: the field has only
		ever been observed already-set or already-0, and GunBroker support has
		said not to depend on it. Nothing is adopted on a 0 — but the count is
		still not recorded against a listing that is over."""
		self._ended(relisted=0)
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["skipped"], "reprice_unconfirmed")
		self.assertEqual(self.enqueued, [])
		self.assertFalse([c for c in self.writes.call_args_list
			if isinstance(c.args[2], dict) and c.args[2].get("gb_item_id")])

	def test_a_live_listing_records_the_count(self):
		self._probe({"state": "ok", "item": {
			"isActive": True, "relistedAsItemID": 0}})
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["quantity"], 3)
		written = {}
		for call in self.writes.call_args_list:
			if isinstance(call.args[2], dict):
				written.update(call.args[2])
		self.assertEqual(written["gb_qty_pushed"], 3)

	def test_a_read_that_fails_does_not_hold_the_count_back(self):
		"""A failed read is not evidence the write missed. Treating it as one
		would leave the count unrecorded and the next movement re-sending it
		for as long as GunBroker was unwell."""
		self._probe({"state": "error", "item": None})
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertNotIn("skipped", result)
		self.assertEqual(result["quantity"], 3)

	def test_the_quantity_value_itself_is_never_compared(self):
		"""Nothing has ever measured how long GunBroker takes to reflect a count,
		so a comparison would rest on an unmeasured delay and fail-close writes
		that did land. `isActive` is the signal with observations behind it."""
		self._probe({"state": "ok", "item": {
			"isActive": True, "quantity": 99, "relistedAsItemID": 0}})
		result = item_listings.sync_item_quantity(ITEM, client=self.client)
		self.assertEqual(result["quantity"], 3)
		self.assertNotIn("skipped", result)


class RelistingOnRestock(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.state = self._patch("ffl_integrations.gunbroker.item_listings.item_state")
		self.qty = self._patch(
			"ffl_integrations.gunbroker.item_listings.sellable_qty", return_value=6)
		self.push = self._patch(
			"ffl_integrations.gunbroker.item_listings.push_item",
			return_value={"ok": True, "gb_item_id": "1", "quantity": 6})

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def test_a_model_that_sold_out_goes_back_up(self):
		self.state.return_value = dict(_item(gb_item_history="16087230"))
		self.assertTrue(item_listings.relist_on_restock(ITEM)["ok"])
		self.push.assert_called_once()

	def test_a_model_that_was_never_listed_stays_off(self):
		"""Otherwise the first receipt of every product in the catalogue would
		put it on GunBroker."""
		self.state.return_value = dict(_item())
		self.assertEqual(
			item_listings.relist_on_restock(ITEM)["skipped"], "never_listed")
		self.push.assert_not_called()

	def test_a_listing_a_person_ended_stays_ended(self):
		"""A machine does not undo a person's decision."""
		self.state.return_value = dict(_item(gb_item_history="16087230", gb_delisted=1))
		self.assertEqual(
			item_listings.relist_on_restock(ITEM)["skipped"], "not_eligible")
		self.push.assert_not_called()

	def test_an_item_still_listed_is_left_alone(self):
		self.state.return_value = dict(
			_item(gb_item_id="16087230", gb_item_history="16000000"))
		self.assertEqual(
			item_listings.relist_on_restock(ITEM)["skipped"], "not_eligible")
		self.push.assert_not_called()


# ---------------------------------------------------------------------------
# The order side
# ---------------------------------------------------------------------------

class TellingTheTwoListingKindsApart(FrappeTestCase):
	"""``is_item_level_line`` decides whether a sold line owes a serial number.

	Getting it wrong in one direction spams the store with "match it by hand"
	on every box of ammunition; in the other it silently waves a gun through
	with no bound-book entry. Both halves of the test are that second case.
	"""

	def _line(self, sku):
		return frappe._dict({"sku": sku, "serial_number": "", "gb_item_id": "1"})

	def test_a_bare_item_code_naming_a_non_firearm_is_item_level(self):
		from ffl_integrations.gunbroker import orders

		with patch("frappe.db.get_value", return_value=0):
			self.assertTrue(orders.is_item_level_line(self._line(ITEM)))

	def test_a_serial_sku_is_never_item_level(self):
		from ffl_integrations.gunbroker import orders

		self.assertFalse(orders.is_item_level_line(self._line("COLT-PYTHON::SN-001")))

	def test_an_unknown_sku_is_not_treated_as_stock(self):
		"""A SKU naming no Item here is a listing we did not create, or an Item
		since renamed. Waving it through as "nothing to reserve" would swallow a
		gun sale; it must reach the serial matcher and raise the alert."""
		from ffl_integrations.gunbroker import orders

		with patch("frappe.db.get_value", return_value=None):
			self.assertFalse(orders.is_item_level_line(self._line("GONE-SKU")))

	def test_a_bare_sku_naming_a_firearm_is_not_treated_as_stock(self):
		"""Should be impossible — guns list per serial — but if one ever appears
		it keeps going down the serial path rather than shipping unrecorded."""
		from ffl_integrations.gunbroker import orders

		with patch("frappe.db.get_value", return_value=1):
			self.assertFalse(orders.is_item_level_line(self._line("COLT-PYTHON")))


# ---------------------------------------------------------------------------
# Publish and end — the two transactions that change GunBroker's state
# ---------------------------------------------------------------------------

class PublishingAnItemListing(FrappeTestCase):
	"""``push_item`` end to end with the HTTP layer stubbed.

	Worth its own class because the interesting behaviour is not the payload
	(covered above) but the bookkeeping around a call that may not answer:
	the attempt is committed BEFORE it goes out, and what happens to that flag
	afterwards decides whether the model can ever be listed again.
	"""

	def setUp(self):
		super().setUp()
		self.writes: list[tuple] = []
		self.settings = fx.settings_stub(category_map=_map([AMMO_ROW]))
		self._patch("ffl_integrations.gunbroker.listings._settings",
			return_value=self.settings)
		self._patch("ffl_integrations.gunbroker.listings._config_gap", return_value=None)
		self._patch("ffl_integrations.gunbroker.mapping.item_listing_context",
			return_value=_context())
		self._patch("ffl_integrations.gunbroker.mapping.item_candidate_image_paths",
			return_value=["/files/ammo.jpg"])
		self._patch("ffl_integrations.gunbroker.mapping.item_picture_parts",
			return_value={"parts": [], "warnings": []})
		self._patch("ffl_integrations.gunbroker.item_listings.sellable_qty",
			return_value=5)
		self._patch("ffl_integrations.gunbroker.item_listings.commit")
		self.state = self._patch(
			"ffl_integrations.gunbroker.item_listings.item_state",
			return_value=dict(_item()))
		self._patch("frappe.db.set_value",
			side_effect=lambda *a, **k: self.writes.append(a))
		self._patch("frappe.db.get_value", return_value=None)

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _written(self) -> dict:
		"""Every field written to the Item this call, merged in write order."""
		out: dict = {}
		for call in self.writes:
			if call[0] != "Item":
				continue
			payload = call[2]
			out.update(payload if isinstance(payload, dict) else {payload: call[3]})
		return out

	def test_a_successful_push_records_the_number_and_the_count(self):
		client = MagicMock()
		client.create_listing.return_value = {
			"userMessage": "Item listed with itemID: 16087300"}
		result = item_listings.push_item(ITEM, client=client)
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_item_id"], "16087300")
		self.assertEqual(result["quantity"], 5)
		written = self._written()
		self.assertEqual(written["gb_item_id"], "16087300")
		self.assertEqual(written["gb_qty_pushed"], 5)
		self.assertEqual(written["gb_push_pending"], 0)

	def test_the_attempt_is_recorded_before_the_call_goes_out(self):
		"""GunBroker enforces no uniqueness and POST /Items is not idempotent,
		so a lost response must not leave the form looking untouched."""
		client = MagicMock()
		order: list[str] = []
		self.writes.clear()

		def _create(*_a, **_k):
			order.append("http")
			return {"userMessage": "Item listed with itemID: 16087300"}

		client.create_listing.side_effect = _create
		with patch("frappe.db.set_value",
				side_effect=lambda *a, **k: order.append(f"write:{a[2]}")):
			item_listings.push_item(ITEM, client=client)
		self.assertTrue(order[0].startswith("write:"))
		self.assertIn("gb_push_pending", order[0])
		self.assertEqual(order[1], "http")

	def test_a_second_press_while_one_is_in_flight_is_refused(self):
		"""The re-read under the lock, not the guard pass above it: two clicks a
		moment apart both clear the first check."""
		client = MagicMock()
		self.state.return_value = dict(_item(gb_push_pending=1))
		result = item_listings.push_item(ITEM, client=client)
		client.create_listing.assert_not_called()
		self.assertEqual(result["skipped"], "push_in_flight")

	def test_a_refused_listing_clears_the_flag_so_it_can_be_retried(self):
		"""A 4xx with a body means no listing exists — holding the flag would
		lock the model out for an hour over a typo."""
		from ffl_integrations.gunbroker.client import GunBrokerError

		client = MagicMock()
		error = GunBrokerError("category not valid")
		error.terminal, error.status_code, error.json_body = True, 400, {"m": 1}
		error.body = "category not valid"
		client.create_listing.side_effect = error
		result = item_listings.push_item(ITEM, client=client)
		self.assertEqual(result["skipped"], "push_rejected")
		self.assertEqual(self._written()["gb_push_pending"], 0)

	def test_an_unanswered_call_keeps_the_flag_standing(self):
		"""A timeout may well have created a listing. The sweep settles it; a
		cleared flag here is how the next click creates the second one."""
		from ffl_integrations.gunbroker.client import GunBrokerError

		client = MagicMock()
		error = GunBrokerError("timeout")
		error.terminal, error.status_code, error.json_body = False, None, None
		client.create_listing.side_effect = error
		result = item_listings.push_item(ITEM, client=client)
		self.assertEqual(result["skipped"], "push_failed")
		self.assertEqual(self._written()["gb_push_pending"], 1)


class EndingAnItemListing(FrappeTestCase):
	def test_confirming_the_end_archives_the_number_and_zeroes_the_count(self):
		writes: list[tuple] = []
		with patch("frappe.db.set_value", side_effect=lambda *a, **k: writes.append(a)):
			item_listings.confirm_item_ended(
				ITEM, "16087300", dict(_item(gb_item_id="16087300")))
		payload = writes[0][2]
		self.assertEqual(payload["gb_item_id"], "")
		self.assertIn("16087300", payload["gb_item_history"])
		self.assertEqual(payload["gb_delisted"], 1)
		self.assertEqual(payload["gb_qty_pushed"], 0)

	def test_selling_out_ends_it_without_the_manual_flag(self):
		"""`gb_delisted` means a person took it down. Stock reaching zero is not
		a person, and marking it as one would stop the restock ever putting it
		back — the whole point of the automatic path."""
		writes: list[tuple] = []
		with patch("frappe.db.set_value", side_effect=lambda *a, **k: writes.append(a)):
			item_listings.confirm_item_ended(
				ITEM, "16087300", dict(_item(gb_item_id="16087300")), delisted=False)
		self.assertEqual(writes[0][2]["gb_delisted"], 0)
		self.assertIn("16087300", writes[0][2]["gb_item_history"])


class WatchingTheSkuAfterAnEnd(FrappeTestCase):
	"""A 0 in `relistedAsItemID` is not evidence that nothing was relisted, and
	that is the hole the end-chase alone cannot close.

	An end that reads 0 confirms against the number it deleted, and
	`confirm_item_ended` clears `gb_item_id` — at which point all three of the
	candidate query's live tests go false and nothing would ever look at that SKU
	again — while a replacement, if there is one, is live and unwatched.
	Reachable by an ordinary sequence: GunBroker sells part of a listing, and the
	counter sells the remainder.
	"""

	def setUp(self):
		super().setUp()
		from ffl_integrations.gunbroker import tasks

		self.tasks = tasks
		self.rnd = MagicMock()
		self.rnd.errors = 0
		self.rnd.selling = tasks._Scan(iter([]))

	def _scan(self, rows):
		self.rnd.selling = self.tasks._Scan(iter(rows))

	def _selling(self, item_id, sku=ITEM):
		return {**fx.selling_row(item_id=int(item_id), sku=sku), "quantity": 2}

	def _row(self, **over):
		row = {"item_code": ITEM, "gb_item_id": "", "gb_push_pending": 0,
			"gb_push_attempted_at": None, "gb_end_pending": 0,
			"gb_item_history": LISTED_ID, "gb_qty_pushed": 0, "gb_delisted": 0}
		row.update(over)
		return row

	def test_a_listing_that_appeared_after_the_end_is_claimed_and_ended(self):
		writes: list[tuple] = []
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "item_state",
					return_value=frappe._dict(
						{"gb_item_id": "", "gb_item_history": LISTED_ID})), \
				patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(self.rnd, self._row())
		self.assertEqual(writes[0][2]["gb_item_id"], RELISTED_ID)
		end.assert_called_once()

	def test_the_number_we_just_ended_is_not_re_adopted_from_the_lag(self):
		"""The listing we took down goes on appearing in `/ItemsSelling` for
		minutes. Reading that as a new listing would claim it back, end it, and
		do the same again every round for as long as the window lasts."""
		self._scan([self._selling(LISTED_ID)])
		with patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row())
		end.assert_not_called()
		write.assert_not_called()

	def test_a_model_that_sold_out_keeps_its_right_to_come_back(self):
		"""`gb_delisted` is the sentinel that stops a machine re-listing what a
		person took down. Ending the replacement must carry the model's own
		answer, not the default — setting it here would strand a sold-out
		consumable off the channel for good."""
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "item_state",
					return_value=frappe._dict(
						{"gb_item_id": "", "gb_item_history": LISTED_ID})), \
				patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value"):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_delisted=0))
		self.assertIs(end.call_args.kwargs["delisted"], False)

	def test_a_model_a_person_took_down_stays_down(self):
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "item_state",
					return_value=frappe._dict(
						{"gb_item_id": "", "gb_item_history": LISTED_ID})), \
				patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value"):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_delisted=1))
		self.assertIs(end.call_args.kwargs["delisted"], True)

	def test_a_push_still_in_flight_is_never_taken_for_a_stray_listing(self):
		"""The listing this scan finds is very likely the one the push CREATED —
		the number simply has not been written back yet. Ending it would undo a
		publish that worked, and `PUSH_GRACE_HOURS` exists precisely so that
		nothing acts inside this window. Every model `relist_on_restock` can
		reach has a non-empty history, so this is not a corner: it is what a lost
		POST response looks like on any model that has been listed before.
		"""
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(
				gb_push_pending=1, gb_push_attempted_at=now_datetime()))
		end.assert_not_called()
		write.assert_not_called()

	def test_a_model_that_was_never_listed_is_not_touched(self):
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_history=""))
		end.assert_not_called()
		write.assert_not_called()


class TheItemCandidateSet(FrappeTestCase):
	"""Against the real query, on real rows — the fix for the async window is a
	WHERE clause, and a WHERE clause asserted as a substring is not asserted."""

	def setUp(self):
		super().setUp()
		from ffl_integrations.gunbroker import tasks

		self.tasks = tasks
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		self.codes = []
		for suffix in ("RECENT", "STALE"):
			code = f"GB-SWEEP-{suffix}"
			if not frappe.db.exists("Item", code):
				frappe.get_doc({
					"doctype": "Item", "item_code": code, "item_name": code,
					"item_group": group, "stock_uom": "Nos", "is_stock_item": 1,
				}).insert(ignore_permissions=True)
			self.codes.append(code)

	def _mark(self, code, *, hours_ago):
		# `is_firearm` explicitly, not by choosing a group: `ffl_core` derives it
		# from the Item Group on insert, and this test is about the WHERE clause,
		# not about that mapping.
		frappe.db.set_value("Item", code, {
			"is_firearm": 0,
			"gb_item_id": "", "gb_push_pending": 0, "gb_end_pending": 0,
			"gb_item_history": LISTED_ID,
			"gb_synced_at": add_to_date(now_datetime(), hours=-hours_ago),
		}, update_modified=False)

	def test_a_model_whose_listing_just_ended_is_still_watched(self):
		self._mark("GB-SWEEP-RECENT", hours_ago=1)
		self._mark("GB-SWEEP-STALE",
			hours_ago=self.tasks.END_RECHECK_HOURS + 1)
		selected = {r["item_code"] for r in self.tasks.item_sweep_candidates()}
		self.assertIn("GB-SWEEP-RECENT", selected,
			"an end can complete before GunBroker has relisted behind it, and "
			"nothing else would ever look at this SKU again")
		self.assertNotIn("GB-SWEEP-STALE", selected,
			"and the window is bounded, or the set grows without end")

	def test_the_recently_ended_are_not_left_at_the_back_of_the_queue(self):
		"""Confirming an end sets `gb_synced_at` to now, so ordering by it alone
		puts these rows last — and past `SWEEP_BATCH` listings they would never
		reach a batch inside their six hours. That would switch the whole
		recently-ended state off silently, on exactly the catalogues big enough
		to need it."""
		self._mark("GB-SWEEP-RECENT", hours_ago=1)
		frappe.db.set_value("Item", "GB-SWEEP-STALE", {
			"is_firearm": 0, "gb_item_id": LISTED_ID, "gb_push_pending": 0,
			"gb_end_pending": 0, "gb_item_history": "",
			"gb_synced_at": add_to_date(now_datetime(), hours=-48),
		}, update_modified=False)
		order = [r["item_code"] for r in self.tasks.item_sweep_candidates()]
		self.assertLess(order.index("GB-SWEEP-RECENT"), order.index("GB-SWEEP-STALE"),
			"the short-lived set goes first; the long rotation can wait a round")


class EndingAListingGunBrokerHasRelisted(FrappeTestCase):
	"""A sale that leaves stock behind does not decrement an item-level listing.

	GunBroker ends it and puts the REMAINDER back up under a new item number,
	naming it in `relistedAsItemID` (sandbox, 2026-08-29: item 16088311 sold two
	of four, went inactive, and pointed at a live 16088370 holding the other
	two). Only an item-level listing can be in that shape — a gun's listing
	holds one unit, so a sale empties it and nothing is relisted.

	Ending the number POS holds therefore reads back `isActive: false` and looks
	exactly like success, while the replacement stays live with no stock behind
	it. Worse, confirming clears `gb_item_id`, and that drops the model out of
	`item_sweep_candidates` — so the sweep never looks at it again and the live
	listing is orphaned for good. It is the one way this integration can lose a
	listing permanently, which is why the end follows the chain itself rather
	than leaving it to the next hourly round.
	"""

	def setUp(self):
		super().setUp()
		self.row = dict(_item(gb_item_id=LISTED_ID, gb_qty_pushed=4))
		self.alerts: list[str] = []
		self._patch("ffl_integrations.gunbroker.listings._settings",
			return_value=fx.settings_stub())
		self._patch("ffl_integrations.gunbroker.listings.notify_admins",
			side_effect=lambda _s, _m, **kw: self.alerts.append(kw["dedup_key"]))
		self._patch("ffl_integrations.gunbroker.item_listings.item_state",
			side_effect=lambda *_a, **_k: dict(self.row))
		self._patch("frappe.db.set_value", side_effect=self._write)

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		mock = patcher.start()
		self.addCleanup(patcher.stop)
		return mock

	def _write(self, _doctype, _name, field, value=None, **_kw):
		self.row.update(field if isinstance(field, dict) else {field: value})

	def _client(self, chain: dict) -> MagicMock:
		"""A client answering `get_item` from a {item number: its successor} map."""
		client = MagicMock()
		client.get_item.side_effect = lambda item_id: fx.item_detail(
			str(item_id), is_active=False, relisted_as=int(chain[str(item_id)]))
		return client

	def _deleted(self, client) -> list:
		return [call.args[0] for call in client.delete_listing.call_args_list]

	def test_the_replacement_is_ended_too_and_only_then_is_the_end_confirmed(self):
		client = self._client({LISTED_ID: RELISTED_ID, RELISTED_ID: 0})
		result = item_listings.end_item_listing(ITEM, client=client)
		self.assertEqual(self._deleted(client), [LISTED_ID, RELISTED_ID])
		self.assertTrue(result["confirmed"])
		self.assertEqual(result["gb_item_id"], RELISTED_ID)
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)
		self.assertEqual(self.row["gb_qty_pushed"], 0)
		for number in (LISTED_ID, RELISTED_ID):
			self.assertIn(number, self.row["gb_item_history"],
				"both numbers are kept: the history is the only trace either "
				"listing ever existed")

	def test_a_listing_nothing_replaced_is_still_one_call(self):
		client = self._client({LISTED_ID: 0})
		result = item_listings.end_item_listing(ITEM, client=client)
		self.assertEqual(self._deleted(client), [LISTED_ID])
		self.assertTrue(result["confirmed"])
		self.assertEqual(self.row["gb_item_history"], LISTED_ID)

	def test_the_record_names_the_live_listing_when_the_second_end_fails(self):
		"""The claim happens BEFORE the replacement is ended, so an interrupted
		chase leaves `gb_item_id` pointing at the listing that is still up. The
		other order would leave the record naming a dead listing and the live one
		unreachable — which is the bug, not a smaller version of it."""
		client = self._client({LISTED_ID: RELISTED_ID, RELISTED_ID: 0})
		client.delete_listing.side_effect = [
			None, GunBrokerError("gateway timeout", status_code=504)]
		with patch.object(item_listings, "log_sync_error"):
			result = item_listings.end_item_listing(ITEM, client=client)
		self.assertFalse(result.get("confirmed"))
		self.assertEqual(self.row["gb_item_id"], RELISTED_ID)
		self.assertEqual(self.row["gb_end_pending"], 1)
		self.assertIn(LISTED_ID, self.row["gb_item_history"])

	def test_the_chase_asks_for_a_person_rather_than_looping(self):
		"""Something minting listings faster than they can be ended is not a
		thing to keep answering; it is a thing to be told about."""
		numbers = iter(["16087371", "16087372", "16087373", "16087374"])
		client = MagicMock()
		client.get_item.side_effect = lambda item_id: fx.item_detail(
			str(item_id), is_active=False, relisted_as=int(next(numbers)))
		result = item_listings.end_item_listing(ITEM, client=client)
		self.assertFalse(result.get("confirmed"))
		self.assertEqual(client.delete_listing.call_count,
			listings.RELIST_CHASE_LIMIT)
		self.assertEqual(self.row["gb_end_pending"], 1)
		self.assertEqual(self.row["gb_item_id"], "16087373",
			"and it names the last listing it actually moved onto")


class ReservingAnOrderWithItemLevelLines(FrappeTestCase):
	"""An item-level line must not raise the unmatched alert.

	The predicate is tested above; this is the branch that uses it. Without it
	every box of ammunition sold arrives as "match it by hand before disposing"
	— hourly, for an order with nothing to match — and the order never becomes
	disposable because the reservation error gates it.
	"""

	def _order_with(self, *lines):
		order = fx.FakeDoc(name="GB-ORD-00009", order_status="Pending Fulfillment")
		order.set("lines", [])
		for sku, item_code in lines:
			order.append("lines", {"sku": sku, "item_code": item_code,
				"gb_item_id": "1", "serial_number": "", "qty": 2,
				"reservation_error": ""})
		order.set("serials", [])
		return order

	def test_an_item_level_line_is_neither_matched_nor_alerted(self):
		from ffl_integrations.gunbroker import orders

		order = self._order_with((ITEM, ITEM))
		with patch.object(orders, "is_item_level_line", return_value=True), \
				patch.object(orders, "_alert_unmatched") as alert, \
				patch.object(orders, "_match_serial") as match:
			order.save = MagicMock()
			orders.reserve_order_serials(order)
		alert.assert_not_called()
		match.assert_not_called()
		self.assertEqual(list(order.serials), [])
		self.assertEqual(order.lines[0].reservation_error, "")

	def test_a_gun_line_on_the_same_order_still_has_to_match(self):
		"""The branch is per line, not per order: a mixed order's rifle keeps
		every gate it had."""
		from ffl_integrations.gunbroker import orders

		order = self._order_with((ITEM, ITEM), ("COLT::SN-1", "COLT"))
		with patch.object(orders, "is_item_level_line",
					side_effect=lambda line: line.item_code == ITEM), \
				patch.object(orders, "_alert_unmatched") as alert, \
				patch.object(orders, "_match_serial",
					return_value=(None, "no such serial")):
			order.save = MagicMock()
			orders.reserve_order_serials(order)
		alert.assert_called_once()
		self.assertEqual(order.lines[0].reservation_error, "")
		self.assertEqual(order.lines[1].reservation_error, "no such serial")


class TheItemSweep(FrappeTestCase):
	"""The states nothing else can clear.

	Each of these was terminal before the sweep pass existed: an unconfirmed
	push locked the model out of the channel for good, an unconfirmed end left
	a listing buyable with no stock behind it, and quantity drift was invisible.
	"""

	def setUp(self):
		super().setUp()
		from ffl_integrations.gunbroker import tasks

		self.tasks = tasks
		self.rnd = MagicMock()
		self.rnd.errors = 0
		# A REAL `_Scan`, not a MagicMock attribute. A blocker was caught in this
		# very pass because every sweep test mocked the index away, and a
		# MagicMock answers `matches()` with a truthy object whatever the code
		# asks it. The default is the ordinary case: one live listing, under the
		# number the Item already holds.
		self._scan([self._selling(LISTED_ID)])

	def _selling(self, item_id, sku=ITEM, quantity=4):
		return {**fx.selling_row(item_id=int(item_id), sku=sku),
			"quantity": quantity}

	def _scan(self, rows):
		self.rnd.selling = self.tasks._Scan(iter(rows))

	def _probe(self, *, active=None, relisted=0, gone=False, error=False):
		"""What `GET /Items/{id}` says about the listing the Item still holds."""
		if error:
			answer = {"state": "error", "item": None}
		elif gone:
			answer = {"state": "gone", "item": None}
		else:
			answer = {"state": "ok", "item": {
				"isActive": active, "relistedAsItemID": relisted}}
		self.rnd.client.item_probe.return_value = answer

	def _stored(self, **over):
		"""What `frappe.db.get_value` hands the claim: the Item as it stands."""
		row = {"gb_item_id": LISTED_ID, "gb_item_history": ""}
		row.update(over)
		return frappe._dict(row)

	def _row(self, **over):
		row = {"item_code": ITEM, "gb_item_id": "", "gb_push_pending": 0,
			"gb_push_attempted_at": None, "gb_end_pending": 0,
			"gb_item_history": "", "gb_qty_pushed": 0}
		row.update(over)
		return row

	def test_a_stale_push_that_created_nothing_has_its_flag_cleared(self):
		"""Otherwise the model can never be listed again — the flag is what the
		guard reads, and nothing else writes it back to 0."""
		writes: list[tuple] = []
		with patch.object(self.tasks, "_push_grace_expired", return_value=True), \
				patch.object(self.tasks, "_recover_from_round",
					return_value={"state": "miss", "ids": []}), \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._row()), \
				patch("frappe.db.set_value", side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_push_pending=1))
		self.assertEqual(writes[0][2]["gb_push_pending"], 0)

	def test_a_stale_push_that_did_create_a_listing_adopts_it(self):
		writes: list[tuple] = []
		with patch.object(self.tasks, "_push_grace_expired", return_value=True), \
				patch.object(self.tasks, "_recover_from_round",
					return_value={"state": "claimed", "item_id": "16087300"}), \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._row(gb_item_id="16087300")), \
				patch.object(self.tasks, "_remote_quantity", return_value=4), \
				patch.object(self.tasks.item_listings, "sellable_qty", return_value=4), \
				patch("frappe.db.set_value", side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_push_pending=1))
		self.assertEqual(writes[0][2]["gb_item_id"], "16087300")

	def test_an_unconfirmed_end_is_retried(self):
		with patch.object(self.tasks.item_listings, "end_item_listing") as end, \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id="16087300", gb_end_pending=1))
		end.assert_called_once()
		# Not both: telling GunBroker a quantity for something we are trying to
		# stop selling is the wrong order of business.
		sync.assert_not_called()

	def test_drift_against_gunbrokers_own_number_forces_a_push(self):
		"""Not against gb_qty_pushed: a push that was silently discarded leaves
		our record and the listing agreeing while the listing is wrong."""
		with patch.object(self.tasks, "_remote_quantity", return_value=7), \
				patch.object(self.tasks.item_listings, "sellable_qty", return_value=4), \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id="16087300", gb_qty_pushed=4))
		self.assertIs(sync.call_args.kwargs["force"], True)

	def test_an_agreeing_count_costs_no_call(self):
		with patch.object(self.tasks, "_remote_quantity", return_value=4), \
				patch.object(self.tasks.item_listings, "sellable_qty", return_value=4), \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id="16087300", gb_qty_pushed=4))
		sync.assert_not_called()

	def test_an_unreadable_scan_changes_nothing(self):
		"""An answer we cannot read is not an answer. Acting on it would push a
		quantity based on a malformed page."""
		self._scan([{"title": "neither a SKU nor an item number"}])
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=4), \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=9))
		sync.assert_not_called()
		write.assert_not_called()

	# -----------------------------------------------------------------------
	# Adopting the number GunBroker moved the listing to
	# -----------------------------------------------------------------------

	def test_a_relisted_number_is_adopted_before_anything_is_sent(self):
		"""The defect this pass was reopened for (sandbox, 2026-08-29).

		GunBroker answered a partial sale by ENDING item 16088311 and putting
		the two remaining boxes back up as 16088370 — same SKU, new number. POS
		went on addressing the dead one, so the live listing never heard another
		quantity and the stock behind it was sellable twice over.
		"""
		writes: list[tuple] = []
		self._scan([self._selling(RELISTED_ID, quantity=2)])
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=2), \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._stored()), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=4))
		written = writes[0][2]
		self.assertEqual(written["gb_item_id"], RELISTED_ID)
		self.assertIn(LISTED_ID, written["gb_item_history"],
			"the dead number is kept — it is how the old listing is traced")
		self.assertEqual(written["gb_qty_pushed"], 0,
			"the replacement has been told nothing, so no count may be assumed "
			"— a stale one would let the next movement skip its push")

	def test_adopting_a_number_is_not_an_edit_of_the_product(self):
		"""Every GunBroker write on an Item leaves `modified` alone on purpose:
		GunBroker renumbering a listing is not somebody changing the product, and
		a bumped timestamp says it was."""
		calls: list[dict] = []
		self._scan([self._selling(RELISTED_ID, quantity=2)])
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=2), \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._stored()), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: calls.append(k)):
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=4))
		self.assertIs(calls[0].get("update_modified"), False)

	def test_the_number_it_already_holds_is_not_rewritten(self):
		writes: list[tuple] = []
		with patch.object(self.tasks, "_remote_quantity", return_value=4), \
				patch.object(self.tasks.item_listings, "sellable_qty", return_value=4), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=4))
		self.assertEqual(writes, [], "an hourly no-op must write nothing")

	def test_the_drift_check_runs_against_the_listing_just_adopted(self):
		"""Adopting the number and then comparing against the old listing's
		count would be half a fix. Nothing here is patched between the scan and
		the verdict: the quantity comes off the replacement's own row."""
		writes: list[tuple] = []
		self._scan([self._selling(RELISTED_ID, quantity=2)])
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=5), \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._stored()), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=4))
		self.assertEqual(writes[0][2]["gb_item_id"], RELISTED_ID)
		self.assertIs(sync.call_args.kwargs["force"], True)

	def test_two_listings_neither_of_which_is_ours_are_never_chosen_between(self):
		"""Same verdict as the per-gun claim path, for the same reason: adopting
		either leaves the other live, unwatched, and sellable."""
		self._scan([self._selling("16087371"), self._selling("16087372")])
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch("frappe.db.set_value"):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		sync.assert_not_called()
		self.rnd.client.item_probe.assert_not_called()
		self.assertEqual(alert.call_args.kwargs["dedup_key"],
			f"gb_dup_listing_Item:{ITEM}")
		self.assertIn(ITEM, alert.call_args.args[0],
			"the alert names the Item — a serial number that does not exist "
			"sends whoever reads it looking in the wrong place")

	def test_ours_and_one_other_is_the_relist_lag_when_ours_has_ended(self):
		"""`/ItemsSelling` keeps showing a listing for minutes after it ends, so
		during a relist it lists BOTH. Calling that a duplicate would raise a
		false alarm on every partial sale — and burn the alert's daily dedup key,
		so a real duplicate that week would be swallowed."""
		writes: list[tuple] = []
		self._scan([self._selling(LISTED_ID), self._selling(RELISTED_ID, quantity=2)])
		self._probe(active=False)
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=2), \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch.object(self.tasks.item_listings, "item_state",
					return_value=self._stored()), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: writes.append(a)):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		alert.assert_not_called()
		self.assertEqual(writes[0][2]["gb_item_id"], RELISTED_ID)

	def test_the_number_we_came_from_is_not_a_second_listing(self):
		"""The round after an adopt, `/ItemsSelling` still carries the listing we
		left — observed: a listing reading back inactive was in the same scan.
		Without the history filter that reads as a duplicate every hour, and the
		daily dedup key then swallows a real one."""
		self._scan([self._selling(LISTED_ID), self._selling(RELISTED_ID, quantity=2)])
		with patch.object(self.tasks.item_listings, "sellable_qty", return_value=2), \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(
				gb_item_id=RELISTED_ID, gb_item_history=LISTED_ID, gb_qty_pushed=2))
		alert.assert_not_called()
		write.assert_not_called()
		sync.assert_not_called()
		self.rnd.client.item_probe.assert_not_called()

	def test_a_replacement_the_item_itself_disowns_is_not_adopted(self):
		"""Ours has ended and names a different successor than the row matched.
		Two answers that disagree settle nothing."""
		self._scan([self._selling(LISTED_ID), self._selling(RELISTED_ID)])
		self._probe(active=False, relisted=16087399)
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		write.assert_not_called()
		alert.assert_not_called()
		sync.assert_not_called()

	def test_ours_and_one_other_is_a_real_duplicate_while_ours_is_still_live(self):
		self._scan([self._selling(LISTED_ID), self._selling(RELISTED_ID)])
		self._probe(active=True)
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch("frappe.db.set_value"):
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		sync.assert_not_called()
		self.assertEqual(alert.call_args.kwargs["dedup_key"],
			f"gb_dup_listing_Item:{ITEM}")

	def test_an_unanswerable_probe_settles_nothing(self):
		"""A timeout is not evidence that our listing ended. Adopting on the
		strength of one would hand the SKU to a listing chosen by a network
		fault."""
		self._scan([self._selling(LISTED_ID), self._selling(RELISTED_ID)])
		self._probe(error=True)
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch.object(self.tasks, "notify_admins") as alert, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		sync.assert_not_called()
		alert.assert_not_called()
		write.assert_not_called()
		self.assertEqual(self.rnd.errors, 1, "and it counts towards the breaker")

	def test_the_end_pending_retry_starts_from_the_number_just_claimed(self):
		"""The whole value of running the adopt FIRST. Retrying the end from the
		dead number is what walks into the async `relistedAsItemID` window —
		which is the one way an item-level listing is lost for good."""
		order: list[str] = []
		self._scan([self._selling(RELISTED_ID)])
		with patch.object(self.tasks.item_listings, "item_state",
					return_value=self._stored()), \
				patch("frappe.db.set_value",
					side_effect=lambda *a, **k: order.append(
						f"claim:{a[2].get('gb_item_id')}"
						if isinstance(a[2], dict) else "write")), \
				patch.object(self.tasks.item_listings, "end_item_listing",
					side_effect=lambda *a, **k: order.append("end")):
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_end_pending=1))
		self.assertEqual(order, [f"claim:{RELISTED_ID}", "end"])

	def test_a_matching_row_that_cannot_say_its_number_sends_nothing(self):
		"""A partial answer settles nothing — same as `_recover_from_round`."""
		self._scan([{"sku": ITEM, "title": "no item number on this row"}])
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_qty_pushed=9))
		sync.assert_not_called()
		write.assert_not_called()

	def test_a_sku_that_is_not_on_the_selling_list_is_left_exactly_as_it_was(self):
		"""`/ItemsSelling` lags a relist by minutes, so a miss is one
		observation and not a verdict. Nothing is adopted and nothing is
		forgotten."""
		self._scan([self._selling(RELISTED_ID, sku="SOMETHING-ELSE")])
		with patch.object(self.tasks.item_listings, "sync_item_quantity") as sync, \
				patch("frappe.db.set_value") as write:
			self.tasks._sweep_one_item(self.rnd, self._row(gb_item_id=LISTED_ID))
		sync.assert_not_called()
		write.assert_not_called()

	def test_an_unconfirmed_end_is_retried_even_when_the_scan_cannot_say(self):
		"""The end confirms itself against the listing it deletes and follows a
		relist from there, so it must not be gated on the adopt step — that
		would strand an unconfirmed end on any malformed page."""
		self._scan([{"title": "neither a SKU nor an item number"}])
		with patch.object(self.tasks.item_listings, "end_item_listing") as end:
			self.tasks._sweep_one_item(
				self.rnd, self._row(gb_item_id=LISTED_ID, gb_end_pending=1))
		end.assert_called_once()


class ReadingTheRemoteQuantity(FrappeTestCase):
	"""``_remote_quantity`` against a REAL ``_Scan``.

	A blocker was caught here that 53 green tests had hidden: every sweep test
	patched this helper out, so a call to a method `_Scan` does not have raised
	on every round and the drift check — the only backstop against overselling —
	never ran once. The lesson is narrow and worth keeping: the one function
	that actually touches the scan must be driven by a scan.
	"""

	def _round(self, rows):
		from ffl_integrations.gunbroker import tasks

		rnd = MagicMock()
		rnd.selling = tasks._Scan(iter(rows))
		return rnd, tasks

	def _row(self, sku, quantity, item_id=LISTED_ID):
		return {"sku": sku, "itemID": int(item_id), "quantity": quantity}

	def test_it_reads_the_quantity_off_our_own_row(self):
		rnd, tasks = self._round([self._row(ITEM, 6), self._row("OTHER", 2)])
		self.assertEqual(tasks._remote_quantity(rnd, ITEM, LISTED_ID), 6)

	def test_the_listing_we_came_from_does_not_answer_for_the_one_we_hold(self):
		"""During a relist the scan carries both, and the dead one's count is
		not ours. Taking the SKU's first row would compare our stock against a
		listing nobody can buy from."""
		rnd, tasks = self._round([
			self._row(ITEM, 9, item_id=LISTED_ID),
			self._row(ITEM, 2, item_id=RELISTED_ID)])
		self.assertEqual(tasks._remote_quantity(rnd, ITEM, RELISTED_ID), 2)

	def test_a_scan_without_our_number_answers_nothing(self):
		rnd, tasks = self._round([self._row(ITEM, 9, item_id=LISTED_ID)])
		self.assertIsNone(tasks._remote_quantity(rnd, ITEM, RELISTED_ID))

	def test_a_decimal_shaped_value_still_reads(self):
		"""int("5.0") raises, and a raised value reads as "the scan cannot say"
		— which turns the drift check off for that item without telling anyone."""
		rnd, tasks = self._round([self._row(ITEM, "5.0")])
		self.assertEqual(tasks._remote_quantity(rnd, ITEM, LISTED_ID), 5)

	def test_a_sku_the_scan_does_not_carry_answers_nothing(self):
		rnd, tasks = self._round([self._row("OTHER", 2)])
		self.assertIsNone(tasks._remote_quantity(rnd, ITEM, LISTED_ID))

	def test_an_unusable_scan_answers_nothing(self):
		"""Rows with neither a SKU nor an item number say nothing about anything;
		treating that as "quantity unknown" is what stops a malformed page from
		driving a push."""
		rnd, tasks = self._round([{"title": "no keys here"}])
		self.assertIsNone(tasks._remote_quantity(rnd, ITEM, LISTED_ID))


class TheRelistCooldown(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.push = patch(
			"ffl_integrations.gunbroker.item_listings.push_item",
			return_value={"ok": True}).start()
		self.addCleanup(patch.stopall)
		patch("ffl_integrations.gunbroker.item_listings.sellable_qty",
			return_value=6).start()

	def test_a_restock_minutes_after_the_listing_ended_waits(self):
		"""A relist mints a new item number and resets the listing's age, so a
		consumable that sells out and is topped up twice a day would otherwise
		burn through item numbers for nothing."""
		from frappe.utils import add_to_date, now_datetime

		row = dict(_item(gb_item_history="16087230",
			gb_synced_at=add_to_date(now_datetime(), minutes=-30)))
		with patch("ffl_integrations.gunbroker.item_listings.item_state",
				return_value=row):
			self.assertEqual(
				item_listings.relist_on_restock(ITEM)["skipped"], "cooling_off")
		self.push.assert_not_called()

	def test_once_the_cooldown_has_passed_it_goes_back_up(self):
		from frappe.utils import add_to_date, now_datetime

		row = dict(_item(gb_item_history="16087230",
			gb_synced_at=add_to_date(now_datetime(), hours=-5)))
		with patch("ffl_integrations.gunbroker.item_listings.item_state",
				return_value=row):
			self.assertTrue(item_listings.relist_on_restock(ITEM)["ok"])
		self.push.assert_called_once()

	def test_a_model_with_no_timestamp_is_not_held_back(self):
		"""An Item listed before the field existed carries none; reading that as
		"too soon" would strand it."""
		row = dict(_item(gb_item_history="16087230", gb_synced_at=None))
		with patch("ffl_integrations.gunbroker.item_listings.item_state",
				return_value=row):
			self.assertTrue(item_listings.relist_on_restock(ITEM)["ok"])
