"""Distributor generic framework: provider registry, capability gating,
and the RSR `plan_fulfillment` split matrix (classification divergence, firearms
and accessories never share an order, handgun 2Day, ammo Ground, >400-SKU split).

`plan_fulfillment` is a PURE function over enriched line dicts + a ship_context
— no DB, no HTTP — so this whole module runs without a site. The registry tests
exercise the hook-driven provider table (frappe.get_hooks) and so need bench.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_integrations.distributor import api as dist_api
from ffl_integrations.distributor.base import DistributorProvider, PlanningError
from ffl_integrations.distributor.rsr_provider import RSRProvider


def _acc(sku="ACC-1", item_code=None, qty=1, department="04"):
	"""An enriched accessory line (non-firearm; RSR ffl_required matches)."""
	return {
		"sku": sku,
		"item_code": item_code or sku,
		"qty": qty,
		"ffl_required": False,
		"is_firearm": False,
		"department": department,
	}


def _handgun(sku="HG-1", item_code=None, qty=1):
	return {
		"sku": sku,
		"item_code": item_code or sku,
		"qty": qty,
		"ffl_required": True,
		"is_firearm": True,
		"department": "01",  # handgun department
	}


def _long_gun(sku="LG-1", item_code=None, qty=1):
	return {
		"sku": sku,
		"item_code": item_code or sku,
		"qty": qty,
		"ffl_required": True,
		"is_firearm": True,
		"department": "05",  # long gun department (non-handgun firearm)
	}


STOCKING = {"mode": "stocking", "ship_to": None, "destination_ffl": None, "buyer_type": None}


class TestPlanFulfillmentSplit(unittest.TestCase):
	def setUp(self):
		self.provider = RSRProvider()

	def test_accessory_only_single_plan_dealer_ground(self):
		plans = self.provider.plan_fulfillment([_acc(), _acc(sku="ACC-2")], STOCKING)
		self.assertEqual(len(plans), 1)
		plan = plans[0]
		self.assertEqual(plan["account"], "dealer")
		self.assertEqual(plan["order_type"], "Stocking")
		self.assertEqual([ln["ship_method"] for ln in plan["lines"]], ["Grnd", "Grnd"])
		self.assertIsNone(plan["ship_to"])
		self.assertIsNone(plan["ship_ffl"])

	def test_handgun_forced_2day(self):
		plans = self.provider.plan_fulfillment([_handgun()], STOCKING)
		self.assertEqual(len(plans), 1)
		self.assertEqual(plans[0]["lines"][0]["ship_method"], "2Day")

	def test_long_gun_defaults_ground(self):
		# Non-handgun firearms are not force-upgraded; RSR ground is cheapest compliant.
		plans = self.provider.plan_fulfillment([_long_gun()], STOCKING)
		self.assertEqual(plans[0]["lines"][0]["ship_method"], "Grnd")

	def test_ammo_stays_ground(self):
		# Ammo (non-firearm) must ship Ground; the Ground default satisfies the rule.
		ammo = _acc(sku="AMMO-1", department="18")
		plans = self.provider.plan_fulfillment([ammo], STOCKING)
		self.assertEqual(plans[0]["lines"][0]["ship_method"], "Grnd")

	def test_guns_and_accessories_never_mix_in_one_order(self):
		# Firearms and accessories never share an order: a cart with a firearm + an accessory splits into TWO orders.
		lines = [_handgun(sku="HG-1"), _acc(sku="ACC-1")]
		plans = self.provider.plan_fulfillment(lines, STOCKING)
		self.assertEqual(len(plans), 2)
		skus = [{ln["sku"] for ln in p["lines"]} for p in plans]
		# no plan contains both a firearm and an accessory
		self.assertIn({"HG-1"}, skus)
		self.assertIn({"ACC-1"}, skus)
		for p in plans:
			kinds = {bool(ln.get("is_firearm")) for ln in _rehydrate(p, lines)}
			self.assertEqual(len(kinds), 1, "an order must be all-firearm or all-accessory")

	def test_over_400_accessory_skus_split_into_multiple_orders(self):
		lines = [_acc(sku=f"ACC-{i}") for i in range(401)]
		plans = self.provider.plan_fulfillment(lines, STOCKING)
		self.assertEqual(len(plans), 2)
		self.assertEqual(sorted(len(p["lines"]) for p in plans), [1, 400])

	def test_over_400_firearms_plus_accessory_split(self):
		lines = [_long_gun(sku=f"LG-{i}") for i in range(401)] + [_acc(sku="ACC-1")]
		plans = self.provider.plan_fulfillment(lines, STOCKING)
		# 401 firearms -> 400 + 1 (two firearm orders); accessories -> 1 order
		self.assertEqual(len(plans), 3)
		sizes = sorted(len(p["lines"]) for p in plans)
		self.assertEqual(sizes, [1, 1, 400])

	def test_classification_divergence_stops_for_manual_queue(self):
		# RSR says ffl_required but our Item is not a firearm (or vice-versa)
		# -> STOP, never silently pick a lane.
		bad = {"sku": "X", "item_code": "X", "qty": 1,
			"ffl_required": True, "is_firearm": False, "department": "01"}
		with self.assertRaises(PlanningError):
			self.provider.plan_fulfillment([bad], STOCKING)

	def test_empty_lines_returns_no_plans(self):
		self.assertEqual(self.provider.plan_fulfillment([], STOCKING), [])

	def test_plan_lines_preserve_sku_item_code_qty(self):
		plans = self.provider.plan_fulfillment([_acc(sku="ACC-9", item_code="ITEM-9", qty=7)], STOCKING)
		ln = plans[0]["lines"][0]
		self.assertEqual((ln["sku"], ln["item_code"], ln["qty"]), ("ACC-9", "ITEM-9", 7))

	def test_plan_line_without_item_code_never_uses_the_sku(self):
		line = {**_acc(sku="ACC-8"), "item_code": ""}
		ln = self.provider.plan_fulfillment([line], STOCKING)[0]["lines"][0]
		self.assertEqual(ln["item_code"], "")


def _rehydrate(plan, original_lines):
	by_sku = {ln["sku"]: ln for ln in original_lines}
	return [by_sku[ln["sku"]] for ln in plan["lines"]]


class TestCapabilityGating(unittest.TestCase):
	def test_rsr_provider_declares_place_order(self):
		self.assertIn("place_order", RSRProvider().capabilities())

	def test_generic_layer_skips_when_capability_absent(self):
		class ManualProvider(DistributorProvider):
			code = "manual"

			def capabilities(self):
				return {"search", "catalog"}  # no place_order

			def search(self, q, limit=10):
				return []

			def catalog_row(self, sku):
				return None

			def check_availability(self, lines):
				return []

			def plan_fulfillment(self, lines, ship_context):
				return []

			def place_order(self, do_name):
				raise AssertionError("must not be called for a manual provider")

			def get_order_status(self, do_name):
				return {}

		manual = ManualProvider()
		self.assertFalse(dist_api.provider_supports(manual, "place_order"))
		self.assertTrue(dist_api.provider_supports(RSRProvider(), "place_order"))


class TestProviderRegistry(unittest.TestCase):
	"""Needs bench: reads the real `distributor_providers` hook from hooks.py."""

	def test_registry_keyed_by_code(self):
		providers = dist_api.get_providers()
		self.assertIn("rsr", providers)
		self.assertIsInstance(providers["rsr"], RSRProvider)

	def test_get_provider_returns_rsr(self):
		self.assertEqual(dist_api.get_provider("rsr").code, "rsr")

	def test_unknown_provider_raises(self):
		with self.assertRaises(Exception):
			dist_api.get_provider("does-not-exist")


class TestGetProviderResolvesDistributorName(unittest.TestCase):
	"""Regression (dev-env): console/rules_engine paths pass a Distributor NAME (e.g.
	'RSR') to get_provider, while quote passes Distributor.provider ('rsr'). The name
	is not a registered code, so pricing_get/price_explain/rules_preview threw 'No
	distributor provider registered for code RSR'. get_provider now falls back: an
	unknown code is retried as a Distributor name → its provider.

	Pure: get_providers + the name lookup are both stubbed."""

	def _fake(self, *, registered, name_to_provider):
		import types

		prov = types.SimpleNamespace(code="rsr", capabilities=lambda: set())
		reg = {c: prov for c in registered}

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Distributor" and field == "provider":
				return name_to_provider.get(name)
			return None

		return prov, reg, _get_value

	def test_a_registered_code_resolves_directly(self):
		prov, reg, gv = self._fake(registered=["rsr"], name_to_provider={})
		with mock.patch.object(dist_api, "get_providers", lambda: reg), \
				mock.patch.object(dist_api.frappe.db, "get_value", gv):
			self.assertIs(dist_api.get_provider("rsr"), prov)

	def test_a_distributor_name_is_resolved_to_its_provider(self):
		prov, reg, gv = self._fake(registered=["rsr"],
			name_to_provider={"RSR": "rsr"})
		with mock.patch.object(dist_api, "get_providers", lambda: reg), \
				mock.patch.object(dist_api.frappe.db, "get_value", gv):
			self.assertIs(dist_api.get_provider("RSR"), prov)

	def test_a_truly_unknown_code_still_raises(self):
		prov, reg, gv = self._fake(registered=["rsr"], name_to_provider={})
		with mock.patch.object(dist_api, "get_providers", lambda: reg), \
				mock.patch.object(dist_api.frappe.db, "get_value", gv):
			with self.assertRaises(Exception):
				dist_api.get_provider("NOPE")
