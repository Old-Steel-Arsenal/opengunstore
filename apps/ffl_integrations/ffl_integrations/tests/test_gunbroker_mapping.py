"""Serial No → GunBroker listing payload.

These rules are worth testing hard for one reason: GunBroker will not let most
of them be corrected. `PUT /Items` changes the price and the title and silently
ignores the serial number, the condition, the category and the duration. A
payload that is wrong in any of those is fixed by ending the listing and posting
a new one, under a new item number — so "we can patch it later" is not available
here the way it is for the web store.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import mapping
from ffl_integrations.tests import gunbroker_fixtures as fx


def _category_map(rows):
	return [frappe._dict(r) for r in rows]


def _context(**over):
	serial = {
		"name": "SN-001", "item_code": "COLT-PYTHON", "item_name": "Colt Python 357",
		"description": "", "status": "Active", "condition_grade": "",
		"firearm_type": "Revolver", "year_made": 0, "image": "", "sell_price": 1200,
	}
	item = {
		"item_name": "Colt Python", "description": "<p>A revolver.</p>", "is_used": 1,
		"is_antique": 0, "is_firearm": 1, "firearm_type": "Revolver", "year_made": 0,
		"upc": "", "uniform_sell_price": 0, "standard_rate": 0, "gb_category_id": 0,
		"image": "",
	}
	serial.update(over.pop("serial", {}))
	item.update(over.pop("item", {}))
	return {"serial": frappe._dict(serial), "item": frappe._dict(item)}


class Titles(FrappeTestCase):
	def test_a_short_title_is_passed_through_untouched(self):
		title, warnings = mapping.gb_title("Colt Python 357 Magnum 6in Blued")
		self.assertEqual(title, "Colt Python 357 Magnum 6in Blued")
		self.assertEqual(warnings, [])

	def test_a_long_title_is_cut_at_a_word_boundary(self):
		raw = ("Colt Python 357 Magnum Six Inch Blued Revolver With Original Box "
			"And Paperwork Excellent")
		title, warnings = mapping.gb_title(raw)
		self.assertLessEqual(len(title), mapping.TITLE_MAX)
		self.assertIn("title_truncated", warnings)
		self.assertFalse(title.endswith(" "))
		self.assertTrue(raw.startswith(title),
			"the kept part must be a prefix of the original — a cut mid-word would "
			"invent a token")
		self.assertNotIn(title[-1], ",;:-/&")

	def test_seventy_five_characters_exactly_is_not_truncated(self):
		"""78 characters is a hard field rejection; 75 is fine. Off by one here
		costs a listing, so the boundary is pinned."""
		raw = "x" * 75
		title, warnings = mapping.gb_title(raw)
		self.assertEqual(len(title), 75)
		self.assertEqual(warnings, [])

	def test_asterisks_quotes_and_capitals_survive(self):
		"""Measured: GunBroker neither strips nor rejects any of them.

		Normalising would be actively harmful — title-casing turns CZ into Cz, FN
		into Fn and AR-15 into Ar-15, mangling exactly the tokens buyers search.
		"""
		raw = 'CZ 75 SP-01 *MATCHING* "as new" AR-15'
		title, _warnings = mapping.gb_title(raw)
		self.assertEqual(title, raw)

	def test_markup_and_runs_of_whitespace_are_collapsed(self):
		title, _warnings = mapping.gb_title("<b>Colt</b>   Python\n\n357")
		self.assertEqual(title, "Colt Python 357")

	def test_a_banned_phrase_is_found_case_insensitively(self):
		self.assertEqual(
			mapping.banned_title_phrase("Test DO NOT BUY probe", "do not buy"),
			"do not buy")

	def test_each_configured_line_is_a_separate_phrase(self):
		self.assertEqual(
			mapping.banned_title_phrase("A replica pistol", "do not buy\nreplica"),
			"replica")

	def test_a_clean_title_matches_nothing(self):
		self.assertEqual(mapping.banned_title_phrase("Colt Python", "do not buy"), "")


class Condition(FrappeTestCase):
	def test_new_and_used(self):
		self.assertEqual(mapping.gb_condition(0), mapping.CONDITION_FACTORY_NEW)
		self.assertEqual(mapping.gb_condition(1), mapping.CONDITION_USED)

	def test_an_antique_is_never_factory_new(self):
		"""A pre-1899 gun whose Item still says is_used=0 (the data gap) is Used."""
		self.assertEqual(mapping.gb_condition(0, 1), mapping.CONDITION_USED)
		self.assertEqual(mapping.gb_condition(0, True), mapping.CONDITION_USED)
		self.assertEqual(mapping.gb_condition(0, 0), mapping.CONDITION_FACTORY_NEW)
		self.assertEqual(mapping.gb_condition(0), mapping.CONDITION_FACTORY_NEW)

	def test_an_ungraded_second_hand_gun_is_simply_used(self):
		"""condition_grade is a within-used gradation and describes the prose.
		Inventing a New Old Stock code from a blank grade would write a guess into
		a field GunBroker will not let us change."""
		self.assertEqual(mapping.gb_condition(1), 3)


class CuriosAndRelics(FrappeTestCase):
	def test_fifty_years_old_qualifies(self):
		self.assertTrue(mapping.is_cr_eligible(1960, today_year=2026))

	def test_the_boundary_is_inclusive(self):
		self.assertTrue(mapping.is_cr_eligible(1976, today_year=2026))
		self.assertFalse(mapping.is_cr_eligible(1977, today_year=2026))

	def test_a_missing_or_nonsense_year_is_not_a_claim(self):
		for value in (None, 0, "", "abc", 99):
			self.assertFalse(mapping.is_cr_eligible(value, today_year=2026), value)


class CategoryResolution(FrappeTestCase):
	MAP = _category_map([
		{"firearm_type": "Revolver", "is_cr_eligible": 0, "category_id": 2325},
		{"firearm_type": "Revolver", "is_cr_eligible": 1, "category_id": 2323},
		{"firearm_type": "Rifle", "is_cr_eligible": 0, "category_id": 3025},
		{"firearm_type": "Rifle", "is_cr_eligible": 0, "is_antique": 1, "category_id": 3021},
	])

	def test_an_antique_takes_only_the_antique_row(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Rifle", cr_eligible=True,
			category_map=self.MAP, is_antique=True), 3021)

	def test_an_antique_with_no_antique_row_resolves_to_nothing(self):
		"""Never the modern row, and never the C&R row: an antique listed in a
		modern category with "no FFL required" is the shape GunBroker's policy
		team looks for."""
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Revolver", cr_eligible=True,
			category_map=self.MAP, is_antique=True), 0)

	def test_a_modern_gun_never_takes_the_antique_row(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Rifle", cr_eligible=False,
			category_map=self.MAP), 3025)
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Rifle", cr_eligible=False,
			category_map=_category_map([{"firearm_type": "Rifle", "is_antique": 1,
				"category_id": 3021}])), 0)

	def test_the_item_override_wins_for_an_antique_too(self):
		self.assertEqual(mapping.resolve_category(
			item_override=9999, firearm_type="Revolver", cr_eligible=False,
			category_map=self.MAP, is_antique=True), 9999)

	def test_the_item_override_wins(self):
		self.assertEqual(mapping.resolve_category(
			item_override=9999, firearm_type="Revolver", cr_eligible=False,
			category_map=self.MAP), 9999)

	def test_the_plain_row_is_used_by_default(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Revolver", cr_eligible=False,
			category_map=self.MAP), 2325)

	def test_a_cr_row_is_preferred_for_an_old_gun(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Revolver", cr_eligible=True,
			category_map=self.MAP), 2323)

	def test_an_old_gun_with_no_cr_row_falls_back_to_the_plain_row(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Rifle", cr_eligible=True,
			category_map=self.MAP), 3025)

	def test_an_unmapped_type_resolves_to_nothing(self):
		"""Zero, never a generic default.

		A firearm in the wrong GunBroker category is invisible to the buyers who
		wanted it at best, and a policy violation against the account at worst.
		The caller refuses the listing.
		"""
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Receiver", cr_eligible=False,
			category_map=self.MAP), 0)

	def test_a_blank_firearm_type_resolves_to_nothing(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="", cr_eligible=False,
			category_map=self.MAP), 0)

	def test_an_empty_map_resolves_to_nothing(self):
		self.assertEqual(mapping.resolve_category(
			item_override=0, firearm_type="Revolver", cr_eligible=False,
			category_map=[]), 0)


class ListingTerms(FrappeTestCase):
	"""What the Send dialog may choose, checked once more where it cannot be
	bypassed. The rules are GunBroker's."""

	def _auction(self, **over):
		spec = {"type": "Auction", "starting_bid": 900, "duration": 7, "auto_relist": 2}
		spec.update(over)
		return mapping.validate_listing_spec(spec)

	def test_a_fixed_price_spec_keeps_only_what_applies(self):
		terms = mapping.validate_listing_spec(
			{"type": "Fixed Price", "price": "1200", "title": "<b>Colt</b>  Python",
				"starting_bid": 5, "duration": 3})
		self.assertEqual(terms["price"], 1200.0)
		self.assertEqual(terms["title"], "Colt Python")
		self.assertEqual(terms["starting_bid"], 0.0)
		self.assertEqual(terms["duration"], 0)

	def test_nothing_at_all_is_a_fixed_price_listing(self):
		self.assertEqual(mapping.validate_listing_spec(None)["type"], "Fixed Price")

	def test_an_auction_needs_a_starting_bid(self):
		with self.assertRaises(ValueError):
			self._auction(starting_bid=0)

	def test_the_length_is_one_gunbroker_offers(self):
		for days in (1, 3, 5, 7, 10, 14):
			self.assertEqual(self._auction(duration=days)["duration"], days)
		with self.assertRaises(ValueError):
			self._auction(duration=30)

	def test_no_reserve_on_the_two_shortest_auctions(self):
		with self.assertRaises(ValueError):
			self._auction(duration=3, reserve_price=1000)
		self.assertEqual(self._auction(duration=5, reserve_price=1000)["reserve_price"], 1000.0)

	def test_the_reserve_is_not_below_the_starting_bid(self):
		with self.assertRaises(ValueError):
			self._auction(reserve_price=800)

	def test_buy_now_covers_the_starting_bid_and_the_reserve(self):
		with self.assertRaises(ValueError):
			self._auction(buy_now_price=850)
		with self.assertRaises(ValueError):
			self._auction(reserve_price=1000, buy_now_price=950)
		self.assertEqual(self._auction(reserve_price=1000, buy_now_price=1000)["buy_now_price"], 1000.0)

	def test_the_relist_rule_is_one_of_the_two_auction_ones(self):
		with self.assertRaises(ValueError):
			self._auction(auto_relist=4)

	def test_nothing_is_negative(self):
		with self.assertRaises(ValueError):
			mapping.validate_listing_spec({"type": "Fixed Price", "price": -1})

	def test_an_unknown_type_is_refused(self):
		with self.assertRaises(ValueError):
			mapping.validate_listing_spec({"type": "Dutch"})


class ConditionOverride(FrappeTestCase):
	"""New Old Stock is only ever a person's choice for one gun."""

	def test_nothing_chosen_follows_the_item(self):
		self.assertEqual(mapping.gb_condition(0), 1)
		self.assertEqual(mapping.gb_condition(1), 3)
		self.assertEqual(mapping.gb_condition(0, 0, 0), 1)
		self.assertEqual(mapping.gb_condition(1, 0, ""), 3)

	def test_a_choice_for_this_gun_wins_over_the_item(self):
		self.assertEqual(mapping.gb_condition(0, 0, 2), 2)
		self.assertEqual(mapping.gb_condition(1, 0, 2), 2)
		self.assertEqual(mapping.gb_condition(1, 0, 1), 1)
		self.assertEqual(mapping.gb_condition(0, 0, 3), 3)

	def test_an_antique_is_never_new(self):
		for choice in (0, 1, 2, 3):
			self.assertEqual(mapping.gb_condition(0, 1, choice), 3)

	def test_the_spec_takes_empty_or_one_of_the_three(self):
		for empty in (None, "", 0, "0"):
			self.assertEqual(mapping.validate_listing_spec({"condition": empty})["condition"], 0)
		self.assertEqual(mapping.validate_listing_spec({})["condition"], 0)
		for code in (1, 2, 3, "2"):
			self.assertEqual(mapping.validate_listing_spec({"condition": code})["condition"], int(code))

	def test_the_stored_label_reads_as_its_code(self):
		# The Serial No Select stores "2 New Old Stock"; echoing it back must not
		# silently mean "follow the item".
		for label, code in (("1 Factory New", 1), ("2 New Old Stock", 2), ("3 Used", 3)):
			self.assertEqual(mapping.validate_listing_spec({"condition": label})["condition"], code)

	def test_any_other_condition_is_refused(self):
		for bad in (4, 5, -1, "9", "New Old Stock", "abc"):
			with self.assertRaises(ValueError):
				mapping.validate_listing_spec({"condition": bad})

	def test_an_auction_may_carry_one_too(self):
		terms = mapping.validate_listing_spec({"type": "Auction", "starting_bid": 900,
			"duration": 7, "auto_relist": 2, "condition": 2})
		self.assertEqual(terms["condition"], 2)


class PaidPromotions(FrappeTestCase):
	"""GunBroker's four paid promotions, chosen with the rest of the terms."""

	def test_no_choice_is_all_four_off_for_either_kind(self):
		off = {"featured": False, "showcase": False,
			"sponsored_onsite": False, "view_counter": False}
		self.assertEqual(mapping.validate_listing_spec(None)["premium"], off)
		self.assertEqual(mapping.validate_listing_spec({"type": "Fixed Price"})["premium"], off)
		self.assertEqual(mapping.validate_listing_spec(
			{"type": "Auction", "starting_bid": 900, "duration": 7, "auto_relist": 2,
				"premium": None})["premium"], off)

	def test_a_fixed_price_listing_may_carry_them(self):
		terms = mapping.validate_listing_spec({"type": "Fixed Price", "price": 1200,
			"premium": {"featured": 1, "view_counter": "1", "showcase": 0}})
		self.assertEqual(terms["premium"], {"featured": True, "showcase": False,
			"sponsored_onsite": False, "view_counter": True})

	def test_an_auction_may_carry_them(self):
		terms = mapping.validate_listing_spec({"type": "Auction", "starting_bid": 900,
			"duration": 7, "auto_relist": 2, "premium": {"sponsored_onsite": True}})
		self.assertEqual(terms["premium"]["sponsored_onsite"], True)
		self.assertEqual(terms["premium"]["featured"], False)

	def test_a_promotion_that_does_not_exist_is_refused_not_dropped(self):
		"""A typo that silently bought nothing would look like a success."""
		with self.assertRaises(ValueError):
			mapping.validate_listing_spec({"premium": {"featurd": 1}})
		with self.assertRaises(ValueError):
			mapping.validate_listing_spec({"premium": {"is_highlighted": 1}})

	def test_premium_must_be_an_object(self):
		with self.assertRaises(ValueError):
			mapping.validate_listing_spec({"premium": "featured"})


class ShippingCost(FrappeTestCase):
	def test_handguns_take_the_handgun_rate(self):
		for ftype in ("Pistol", "Revolver"):
			self.assertEqual(mapping.ship_cost_for(
				ftype, long_gun_cost=45.0, handgun_cost=34.99), 34.99)

	def test_everything_else_takes_the_long_gun_rate(self):
		for ftype in ("Rifle", "Shotgun", "Other Firearm", ""):
			self.assertEqual(mapping.ship_cost_for(
				ftype, long_gun_cost=45.0, handgun_cost=34.99), 45.0)

	def test_an_unset_rate_reads_as_zero(self):
		"""Which the caller refuses on. An unconfigured Currency field is 0, and
		"buyer pays a fixed $0" means shipping firearms across the country free."""
		self.assertEqual(mapping.ship_cost_for(
			"Rifle", long_gun_cost=None, handgun_cost=34.99), 0.0)

	def test_a_rate_of_its_own_beats_the_default(self):
		"""The person pressing Send decides; Settings is what they were offered."""
		self.assertEqual(mapping.listing_ship_cost(60, 45.0), 60.0)
		self.assertEqual(mapping.listing_ship_cost("60", 45.0), 60.0)

	def test_nothing_of_its_own_means_the_default(self):
		"""Empty and 0 are the same answer: a Currency field nobody typed in
		reads 0. And a default of 0 stays 0, for the caller to refuse."""
		for own in (None, "", 0, 0.0):
			self.assertEqual(mapping.listing_ship_cost(own, 45.0), 45.0)
		self.assertEqual(mapping.listing_ship_cost(0, 0), 0.0)


class PaymentMethods(FrappeTestCase):
	def test_this_phase_offers_exactly_cheques_and_money_orders(self):
		methods = mapping.payment_methods(flags=fx.flags_stub())
		self.assertEqual(set(methods), {"CertifiedCheck", "USPSMoneyOrder", "MoneyOrder"})
		self.assertNotIn("VisaMastercard", methods)

	def test_card_boxes_are_ignored_while_card_checkout_is_off(self):
		"""A ticked-but-inert box would advertise a payment route the buyer finds
		unusable at checkout."""
		methods = mapping.payment_methods(flags=fx.flags_stub(
			card_checkout_enabled=0, pay_visa_mastercard=1, pay_amex=1))
		self.assertNotIn("VisaMastercard", methods)
		self.assertNotIn("AmericanExpress", methods)

	def test_turning_the_card_switch_on_is_the_only_change_needed(self):
		methods = mapping.payment_methods(flags=fx.flags_stub(
			card_checkout_enabled=1, pay_visa_mastercard=1))
		self.assertIn("VisaMastercard", methods)

	def test_paypal_is_never_offered(self):
		"""GunBroker banned it for firearms in 2022, so it is not even a setting."""
		methods = mapping.payment_methods(flags=fx.flags_stub())
		self.assertNotIn("PayPal", methods)


class Descriptions(FrappeTestCase):
	def test_a_per_gun_note_is_escaped_and_line_broken(self):
		html, _warnings = mapping.gb_description(
			serial_description="Bore is bright.\n<not a tag>",
			item_description="<p>ignored</p>")
		self.assertIn("<br>", html)
		self.assertIn("&lt;not a tag&gt;", html)
		self.assertNotIn("<p>ignored</p>", html)

	def test_the_item_description_is_used_when_the_gun_has_none(self):
		html, _warnings = mapping.gb_description(
			serial_description="", item_description="<p>A revolver.</p>")
		self.assertIn("<p>A revolver.</p>", html)

	def test_the_footer_is_appended_either_way(self):
		for serial_desc in ("", "Bore is bright."):
			html, _warnings = mapping.gb_description(
				serial_description=serial_desc, item_description="<p>x</p>",
				footer="<p>Transfers to your FFL.</p>")
			self.assertIn("Transfers to your FFL.", html)

	def test_insecure_links_are_upgraded(self):
		"""GunBroker serves listings over https and browsers block mixed content:
		the seller sees a fine listing and the buyer sees a broken one."""
		html, warnings = mapping.gb_description(
			serial_description="",
			item_description='<a href="http://example.com/terms">Terms</a>')
		self.assertIn('href="https://example.com/terms"', html)
		self.assertIn("upgraded_to_https", warnings)

	def test_an_image_on_an_unreachable_host_is_removed_not_upgraded(self):
		"""Rewriting a loopback URL to https just makes it fail differently."""
		html, warnings = mapping.gb_description(
			serial_description="",
			item_description='<p>See <img src="http://localhost:8000/x.jpg"> here</p>')
		self.assertNotIn("img", html)
		self.assertIn("insecure_image_removed", warnings)
		self.assertIn("here", html, "only the image goes, not the surrounding text")

	def test_http_in_running_text_is_reported_not_refused(self):
		"""It is prose, not an asset. Refusing a whole listing over a URL somebody
		typed into a sentence would be absurd."""
		html, warnings = mapping.gb_description(
			serial_description="Read about it at http://example.com/history",
			item_description="")
		self.assertIn("plain_http_in_text", warnings)
		self.assertIn("http://example.com/history", html)


class EnumCodes(FrappeTestCase):
	def test_the_leading_integer_is_taken(self):
		self.assertEqual(mapping._enum_code("4 Relist Fixed Price", default=1), 4)
		self.assertEqual(mapping._enum_code("8 Buyer pays fixed", default=2), 8)
		self.assertEqual(mapping._enum_code("30", default=60), 30)

	def test_a_value_with_no_code_falls_back_rather_than_sending_zero(self):
		"""Zero is a validation rejection several seconds and one confusing
		message later."""
		self.assertEqual(mapping._enum_code("", default=4), 4)
		self.assertEqual(mapping._enum_code(None, default=4), 4)
		self.assertEqual(mapping._enum_code("Relist Fixed Price", default=4), 4)


class SerialAttributes(FrappeTestCase):
	def test_the_guns_own_value_wins_over_the_models(self):
		ctx = _context(serial={"firearm_type": "Revolver"},
			item={"firearm_type": "Pistol"})
		self.assertEqual(
			mapping.serial_attr(ctx["serial"], ctx["item"], "firearm_type"), "Revolver")

	def test_the_model_fills_in_a_blank(self):
		ctx = _context(serial={"firearm_type": ""}, item={"firearm_type": "Pistol"})
		self.assertEqual(
			mapping.serial_attr(ctx["serial"], ctx["item"], "firearm_type"), "Pistol")


class BuildListingPayload(FrappeTestCase):
	def _build(self, **over):
		return self._build_full(**over)["payload"]

	def _build_full(self, **over):
		settings = fx.settings_stub(category_map=_category_map([
			{"firearm_type": "Revolver", "is_cr_eligible": 0, "category_id": 2325},
			{"firearm_type": "Rifle", "is_cr_eligible": 0, "category_id": 3025},
		]), **over.pop("settings", {}))
		flags = fx.flags_stub(**over.pop("flags", {}))
		context = _context(**over)
		with patch.object(mapping, "effective_serial_price", return_value=1200.0), \
				patch.object(mapping, "listing_flags", return_value=flags):
			return mapping.build_listing_payload(
				"SN-001", settings=settings, context=context)

	def test_a_fixed_price_listing_relists_itself_until_it_sells(self):
		"""AutoRelist 4, not 2.

		2 is the auction value and does nothing on a fixed-price listing — which
		would quietly mean every unsold gun falls off GunBroker after 30 days. The
		assertion is on what is SENT, because GunBroker does not echo this field
		back and it cannot be changed by an update.
		"""
		self.assertEqual(self._build()["AutoRelist"], 4)

	def test_the_listing_shape_this_phase_committed_to(self):
		payload = self._build()
		self.assertEqual(payload["ListingDuration"], 30)
		self.assertEqual(payload["Quantity"], 1)
		self.assertIs(payload["IsFFLRequired"], True)
		self.assertIs(payload["WillShipInternational"], False)
		self.assertEqual(payload["WhoPaysForShipping"], 8)
		self.assertEqual(payload["InspectionPeriod"], 1)
		self.assertIs(payload["UseDefaultExcludeStates"], True)
		self.assertEqual(payload["ShippingClassesSupported"], {"Ground": True})

	def test_the_sku_is_the_stable_key(self):
		"""GunBroker keeps SKU across its own relists while issuing a new item
		number each time, and recommends it for inventory sync. Everything that
		has to find this listing again matches on it."""
		payload = self._build()
		self.assertEqual(payload["SKU"], "COLT-PYTHON::SN-001")
		self.assertEqual(payload["SerialNumber"], "SN-001")

	def test_a_sku_over_the_50_char_limit_goes_out_in_the_short_form(self):
		code = "DAVIDE PEDERSOLI S.TRAPDOOR CARBINE .45-70 X"  # 52 chars with ::SN-001
		payload = self._build(serial={"item_code": code})
		self.assertEqual(payload["SKU"], "::SN-001")

	def test_the_price_is_the_resolved_sell_price(self):
		self.assertEqual(self._build()["FixedPrice"], 1200.0)

	def test_payment_methods_carry_exactly_the_three(self):
		self.assertEqual(
			set(self._build()["PaymentMethods"]),
			{"CertifiedCheck", "USPSMoneyOrder", "MoneyOrder"})

	def test_a_handgun_ships_at_the_handgun_rate(self):
		self.assertEqual(
			self._build()["ShippingClassCosts"], {"Ground": 34.99})

	def test_a_long_gun_ships_at_the_long_gun_rate(self):
		payload = self._build(
			serial={"firearm_type": "Rifle"}, item={"firearm_type": "Rifle"})
		self.assertEqual(payload["ShippingClassCosts"], {"Ground": 45.0})

	def test_a_gun_with_a_rate_of_its_own_ships_at_that_rate(self):
		payload = self._build(serial={"gb_ship_cost": 60})
		self.assertEqual(payload["ShippingClassCosts"], {"Ground": 60.0})

	def test_a_gun_with_no_rate_of_its_own_ships_at_the_default(self):
		payload = self._build(serial={"gb_ship_cost": 0})
		self.assertEqual(payload["ShippingClassCosts"], {"Ground": 34.99})

	def test_a_free_shipping_gun_goes_out_seller_pays_at_zero(self):
		"""The shape the sandbox accepted and read back as freeShippingAvailable."""
		payload = self._build(serial={"gb_free_shipping": 1, "gb_ship_cost": 0})
		self.assertEqual(payload["WhoPaysForShipping"], 2)
		self.assertEqual(payload["ShippingClassCosts"], {"Ground": 0.0})

	def test_free_shipping_needs_no_store_default(self):
		payload = self._build(serial={"gb_free_shipping": 1},
			settings={"long_gun_ship_cost": 0, "handgun_ship_cost": 0})
		self.assertEqual(payload["WhoPaysForShipping"], 2)

	def test_a_fixed_price_of_its_own_beats_the_pos_price(self):
		self.assertEqual(self._build(serial={"gb_list_price": 1500})["FixedPrice"], 1500.0)
		self.assertEqual(self._build(serial={"gb_list_price": 0})["FixedPrice"], 1200.0)

	def test_a_title_of_its_own_is_sent_as_typed(self):
		payload = self._build(serial={"gb_listing_title": "Colt Python 357, 6in, boxed"})
		self.assertEqual(payload["Title"], "Colt Python 357, 6in, boxed")

	def test_an_auction_starts_from_a_bid_and_carries_no_fixed_price(self):
		"""GunBroker reads the presence of StartingBid as "auction"; a FixedPrice
		alongside it would make the listing something neither kind."""
		payload = self._build(serial={
			"gb_listing_type": "Auction", "gb_starting_bid": 900,
			"gb_auction_duration": 7, "gb_auction_auto_relist": 2})
		self.assertEqual(payload["StartingBid"], 900.0)
		self.assertNotIn("FixedPrice", payload)
		self.assertNotIn("ReservePrice", payload)
		self.assertNotIn("BuyNowPrice", payload)
		self.assertEqual(payload["ListingDuration"], 7)
		self.assertEqual(payload["AutoRelist"], 2)

	def test_an_auctions_reserve_and_buy_now_go_only_when_set(self):
		payload = self._build(serial={
			"gb_listing_type": "Auction", "gb_starting_bid": 900,
			"gb_reserve_price": 1000, "gb_buy_now_price": 1400,
			"gb_auction_duration": 10, "gb_auction_auto_relist": 1})
		self.assertEqual(payload["ReservePrice"], 1000.0)
		self.assertEqual(payload["BuyNowPrice"], 1400.0)
		self.assertEqual(payload["AutoRelist"], 1)

	def test_an_auction_without_terms_falls_back_to_settings(self):
		"""Length and relist rule come from Settings when the gun carries none —
		the case for a push that did not go through the dialog."""
		payload = self._build(serial={"gb_listing_type": "Auction", "gb_starting_bid": 500})
		self.assertEqual(payload["ListingDuration"], 7)
		self.assertEqual(payload["AutoRelist"], 2)

	def test_an_auction_without_a_starting_bid_is_a_programming_error(self):
		with self.assertRaises(ValueError):
			self._build(serial={"gb_listing_type": "Auction", "gb_starting_bid": 0})

	def test_condition_comes_off_the_item_only(self):
		"""`is_used` exists on the Item and nowhere else. Reading it through the
		serial-first helper would silently resolve to nothing and list every
		second-hand gun as Factory New."""
		self.assertEqual(self._build(item={"is_used": 1})["Condition"], 3)
		self.assertEqual(self._build(item={"is_used": 0})["Condition"], 1)

	def test_an_antique_flagged_new_is_still_listed_used(self):
		self.assertEqual(self._build(
			item={"is_used": 0, "is_antique": 1, "gb_category_id": 3021})["Condition"], 3)

	def test_a_gun_with_no_choice_of_its_own_follows_the_item(self):
		self.assertEqual(self._build(item={"is_used": 1})["Condition"], 3)
		self.assertEqual(self._build(item={"is_used": 0})["Condition"], 1)
		self.assertEqual(self._build(item={"is_used": 1}, serial={"gb_condition": ""})["Condition"], 3)

	def test_new_old_stock_goes_out_when_chosen_for_the_gun(self):
		"""The Select stores code-plus-label; the code is what is sent."""
		payload = self._build(item={"is_used": 1}, serial={"gb_condition": "2 New Old Stock"})
		self.assertEqual(payload["Condition"], 2)
		self.assertEqual(self._build(serial={"gb_condition": "3 Used"})["Condition"], 3)
		self.assertEqual(self._build(item={"is_used": 1},
			serial={"gb_condition": "1 Factory New"})["Condition"], 1)

	def test_an_antique_is_listed_used_whatever_was_chosen_and_says_so(self):
		item = {"is_used": 0, "is_antique": 1, "gb_category_id": 3021}
		built = self._build_full(item=item, serial={"gb_condition": "2 New Old Stock"})
		self.assertEqual(built["payload"]["Condition"], 3)
		self.assertIn("condition_antique_used", built["warnings"])
		built = self._build_full(item=item)
		self.assertEqual(built["payload"]["Condition"], 3)
		self.assertNotIn("condition_antique_used", built["warnings"])

	def test_no_paid_promotion_is_sent_unless_one_is_on(self):
		self.assertNotIn("PremiumFeatures", self._build())
		self.assertNotIn("PremiumFeatures", self._build(serial={
			"gb_featured": 0, "gb_showcase": 0, "gb_sponsored_onsite": 0, "gb_view_counter": 0}))

	def test_only_the_promotions_that_are_on_are_sent(self):
		payload = self._build(serial={"gb_featured": 1, "gb_view_counter": 1})
		self.assertEqual(payload["PremiumFeatures"],
			{"IsFeaturedItem": True, "HasViewCounter": True})
		payload = self._build(serial={"gb_showcase": 1, "gb_sponsored_onsite": 1})
		self.assertEqual(payload["PremiumFeatures"],
			{"IsShowCaseItem": True, "IsSponsoredOnsite": True})

	def test_all_four_go_out_as_gunbroker_names_them(self):
		payload = self._build(serial={"gb_featured": 1, "gb_showcase": 1,
			"gb_sponsored_onsite": 1, "gb_view_counter": 1})
		self.assertEqual(payload["PremiumFeatures"], {
			"IsFeaturedItem": True, "IsShowCaseItem": True,
			"IsSponsoredOnsite": True, "HasViewCounter": True})

	def test_an_auction_carries_them_too(self):
		payload = self._build(serial={"gb_listing_type": "Auction", "gb_starting_bid": 900,
			"gb_auction_duration": 7, "gb_auction_auto_relist": 2, "gb_showcase": 1})
		self.assertEqual(payload["PremiumFeatures"], {"IsShowCaseItem": True})

	def test_the_premium_and_deprecated_fields_are_never_sent(self):
		payload = self._build()
		for key in ("StandardTextID", "Characteristics", "SubTitle", "FreedomCoin",
				"SeeItemDesc", "IsFeatured", "IsHighlighted", "IsBold"):
			self.assertNotIn(key, payload)

	def test_a_blank_upc_is_omitted_rather_than_sent_empty(self):
		self.assertNotIn("UPC", self._build())
		self.assertEqual(self._build(item={"upc": "012345678905"})["UPC"], "012345678905")

	def test_the_part_number_rides_along_for_the_catalogue(self):
		self.assertNotIn("MfgPartNumber", self._build())
		self.assertEqual(self._build(item={"mpn": " 551-2SP "})["MfgPartNumber"], "551-2SP")

	def test_a_gun_without_a_upc_says_so(self):
		"""Without one GunBroker fills no Manufacturer / Model / Caliber, and the
		API cannot set them: the person who sent it has to, on the listing."""
		def warnings(**item):
			settings = fx.settings_stub(category_map=_category_map([
				{"firearm_type": "Revolver", "is_cr_eligible": 0, "category_id": 2325}]))
			with patch.object(mapping, "effective_serial_price", return_value=1200.0), \
					patch.object(mapping, "listing_flags", return_value=fx.flags_stub()):
				return mapping.build_listing_payload(
					"SN-001", settings=settings, context=_context(item=item))["warnings"]
		self.assertIn("no_upc", warnings())
		self.assertNotIn("no_upc", warnings(upc="012345678905"))

	def test_exclude_states_ride_only_when_the_account_default_is_off(self):
		self.assertNotIn("ExcludeStates", self._build())
		payload = self._build(
			flags={"use_default_exclude_states": 0},
			settings={"exclude_states": "CA,NY"})
		self.assertEqual(payload["ExcludeStates"], "CA,NY")

	def test_an_antique_never_requires_an_ffl_whatever_the_settings_say(self):
		payload = self._build(item={"is_antique": 1, "gb_category_id": 3021},
			flags={"is_ffl_required": True})
		self.assertIs(payload["IsFFLRequired"], False)
		self.assertEqual(payload["CategoryID"], 3021)

	def test_a_modern_gun_keeps_the_settings_flag(self):
		self.assertTrue(self._build(flags={"is_ffl_required": True})["IsFFLRequired"])

	def test_an_antique_with_no_antique_category_raises_rather_than_guessing(self):
		with self.assertRaises(ValueError):
			self._build(item={"is_antique": 1})

	def test_an_unresolvable_category_raises_rather_than_guessing(self):
		"""A backstop, not the operator-facing refusal — the push guards catch
		this before a client is ever built."""
		with self.assertRaises(ValueError):
			self._build(serial={"firearm_type": "Receiver"},
				item={"firearm_type": "Receiver"})

	def test_an_unset_shipping_rate_raises(self):
		with self.assertRaises(ValueError):
			self._build(settings={"long_gun_ship_cost": 0, "handgun_ship_cost": 0})


class AnUpgradedSiteWhoseSettingsHaveOneStoredRow(FrappeTestCase):
	"""Driven through the real read path this time.

	The situation: a patch writes the `sandbox_mode` row. From that moment the
	Single has stored rows, so `load_from_db` stops applying docfield defaults —
	and `_fix_numeric_types` casts every Check through `cint()` unconditionally,
	so every OTHER checkbox reads back as **0**, not as None and not as its
	default. Read off the document, a firearm listing would then say no FFL is
	required, drop the account's state exclusions, and offer no way to pay.

	The first version of these tests mocked the settings document into returning
	None and passed while the production path was untouched, because Frappe
	cannot produce that state. So the seam here is `tabSingles` itself: the flags
	resolve through `single_raw_map` → `single_flags` → `listing_flags`, exactly
	as they do in production, and the only thing stubbed is the query result.
	"""

	def _flags(self, **stored):
		with patch("frappe.db.sql", return_value=fx.singles_rows(**stored)):
			return mapping.listing_flags()

	def test_only_the_sandbox_row_exists_and_the_defaults_still_hold(self):
		flags = self._flags(sandbox_mode="1")
		self.assertIs(flags["is_ffl_required"], True,
			"a listing that says no FFL is required reads to a buyer as one that "
			"can ship to their door")
		self.assertIs(flags["use_default_exclude_states"], True,
			"false here with no explicit list means every state restriction stops "
			"applying")

	def test_a_firearm_listing_still_offers_a_way_to_pay(self):
		"""Cheques and money orders are the only route this phase accepts. An
		empty PaymentMethods is a listing nobody can buy, at full listing fee."""
		methods = mapping.payment_methods(flags=self._flags(sandbox_mode="1"))
		self.assertEqual(
			set(methods), {"CertifiedCheck", "USPSMoneyOrder", "MoneyOrder"})

	def test_an_explicit_zero_is_still_honoured(self):
		"""The defence must not become "ignore the operator". A stored 0 is a
		stated choice; an absent row is not."""
		flags = self._flags(sandbox_mode="1", is_ffl_required="0")
		self.assertIs(flags["is_ffl_required"], False)

	def test_card_methods_stay_off_when_nothing_is_stored(self):
		"""Defaults cut both ways: the card switch ships OFF, so an unsaved value
		must not turn card checkout on."""
		methods = mapping.payment_methods(flags=self._flags(sandbox_mode="1"))
		for key in ("VisaMastercard", "AmericanExpress", "Discover"):
			self.assertNotIn(key, methods)

	def test_a_stored_one_is_read_as_on(self):
		flags = self._flags(sandbox_mode="1", pay_visa_mastercard="1",
			card_checkout_enabled="1")
		self.assertIs(flags["pay_visa_mastercard"], True)
		self.assertIn("VisaMastercard", mapping.payment_methods(flags=flags))

	def test_the_flags_come_from_the_stored_rows_not_the_document(self):
		"""The assertion that would have caught the first attempt.

		A settings document reports every unsaved checkbox as 0. If the payload
		read the document, this would come back False.
		"""
		settings = fx.settings_stub(is_ffl_required=None)
		self.assertEqual(settings.get("is_ffl_required"), 0,
			"the stub must mirror Frappe: a Check never reads back as None")
		self.assertIs(self._flags(sandbox_mode="1")["is_ffl_required"], True)

	def test_passing_a_settings_document_is_refused(self):
		"""The trap this signature replaced.

		A Settings document answers `.get()` too, so handing one over used to run
		perfectly while every unsaved Check read as 0 — the exact bug two rounds
		of review were spent removing, reappearing at a different address.
		"""
		with self.assertRaises(TypeError):
			mapping.payment_methods(flags=fx.settings_stub())


class PictureParts(FrappeTestCase):
	def _parts(self, paths, *, settings=None, sizes=None):
		settings = settings or fx.settings_stub()
		rows = [{"image": p, "is_primary": 1 if i == 0 else 0} for i, p in enumerate(paths)]
		with patch.object(mapping, "_gallery_rows", return_value=rows), \
				patch.object(mapping, "_local_file_bytes",
					side_effect=lambda url: (sizes or {}).get(url, b"bytes")):
			return mapping.picture_parts(
				"SN-001", settings=settings, context=_context())

	def test_the_primary_photo_leads(self):
		"""GunBroker makes the first uploaded picture the thumbnail."""
		result = self._parts(["/files/main.jpg", "/files/side.jpg"])
		self.assertEqual([p[0] for p in result["parts"]], ["main.jpg", "side.jpg"])

	def test_png_is_kept(self):
		"""Measured, against an earlier note claiming PNGs were dropped: a single
		PNG uploaded and read back fine."""
		result = self._parts(["/files/a.png"])
		self.assertEqual([p[2] for p in result["parts"]], ["image/png"])

	def test_an_unsupported_format_is_skipped_with_a_warning(self):
		"""Dropped on the filename, before anything is read — but the operator
		still hears about it, because a photo silently missing from a listing is
		the kind of thing nobody notices for weeks."""
		result = self._parts(["/files/a.webp", "/files/b.jpg"])
		self.assertEqual([p[0] for p in result["parts"]], ["b.jpg"])
		self.assertIn("image_format_skipped", result["warnings"])

	def test_the_guard_sees_the_same_photos_the_upload_will(self):
		"""One resolver behind both, so "this gun has photos" and "these are the
		photos" can never disagree — which would show up as a listing that passed
		the guard and published nothing."""
		rows = [{"image": "/files/a.webp", "is_primary": 1},
			{"image": "/files/b.jpg", "is_primary": 0}]
		with patch.object(mapping, "_gallery_rows", return_value=rows):
			paths = mapping.candidate_image_paths(
				"SN-001", settings=fx.settings_stub(), context=_context())
		self.assertEqual(paths, ["/files/b.jpg"])

	def test_the_per_listing_cap_truncates_and_says_so(self):
		result = self._parts(
			[f"/files/{i}.jpg" for i in range(20)],
			settings=fx.settings_stub(max_pictures=16))
		self.assertEqual(len(result["parts"]), 16)
		self.assertIn("pictures_truncated", result["warnings"])

	def test_the_cap_applies_before_any_bytes_are_read(self):
		"""Reading four photos that will be thrown away is four disk reads, or
		four HTTP fetches, for nothing."""
		reads: list[str] = []
		rows = [{"image": f"/files/{i}.jpg", "is_primary": 1 if i == 0 else 0}
			for i in range(20)]
		with patch.object(mapping, "_gallery_rows", return_value=rows), \
				patch.object(mapping, "_local_file_bytes",
					side_effect=lambda url: reads.append(url) or b"bytes"):
			mapping.picture_parts("SN-001", settings=fx.settings_stub(max_pictures=16),
				context=_context())
		self.assertEqual(len(reads), 16)

	def test_an_unreadable_file_is_dropped_not_fatal(self):
		result = self._parts(["/files/gone.jpg", "/files/ok.jpg"],
			sizes={"/files/gone.jpg": b""})
		self.assertEqual([p[0] for p in result["parts"]], ["ok.jpg"])
		self.assertIn("image_unreadable", result["warnings"])

	def test_no_usable_photos_is_an_empty_list_not_an_error(self):
		"""GunBroker is happy to list a gun with no photos. Refusing is OUR rule
		and it is applied by the caller, not here."""
		result = self._parts([])
		self.assertEqual(result["parts"], [])
