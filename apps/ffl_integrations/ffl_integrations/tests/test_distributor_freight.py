"""Freight estimates for the Create Order fulfillment plan.

The two houses' published rules as pure functions over a Settings-shaped object
(RSR freight policy; Sports South open-account shipping rules), plus the endpoint
that prices the planner's CURRENT choice as the real orders it will become —
grouped by (house, lane), split by the provider, summed.
"""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from ffl_integrations.distributor import api as dist_api
from ffl_integrations.distributor import rsr_provider, ss_provider


def _ln(cost, qty=1, *, gun=False, method="Grnd", dept="10"):
	return {"unit_cost": cost, "qty": qty, "is_firearm": gun, "ship_method": method,
		"department": dept}


class RSRFreightRules(unittest.TestCase):
	def s(self, **kw):
		return frappe._dict({"store_free_freight": 0, **kw})

	def test_dropship_is_flat_rate_whatever_the_value(self):
		f = rsr_provider.estimate_freight
		self.assertEqual(f([_ln(20)], "direct", self.s()), 10.0)
		self.assertEqual(f([_ln(2000)], "direct", self.s()), 10.0)

	def test_dropship_handgun_is_2day_plus_one_signature(self):
		lines = [_ln(500, gun=True, method="2Day", dept="01")]
		self.assertEqual(rsr_provider.estimate_freight(lines, "fds", self.s()), 20.0)

	def test_two_methods_charge_the_higher(self):
		lines = [_ln(20), _ln(500, gun=True, method="2Day")]
		self.assertEqual(rsr_provider.estimate_freight(lines, "fds", self.s()), 20.0)

	def test_store_order_under_500_pays_the_rate(self):
		self.assertEqual(rsr_provider.estimate_freight([_ln(120, 2)], "to_store", self.s()), 10.0)

	def test_store_order_over_500_is_free_but_a_gun_still_pays_signature(self):
		f = rsr_provider.estimate_freight
		self.assertEqual(f([_ln(251, 2)], "to_store", self.s()), 0.0)
		self.assertEqual(f([_ln(546, gun=True, method="2Day")], "to_store", self.s()), 5.0)
		# exactly $500 is not "more than $500"
		self.assertEqual(f([_ln(500)], "to_store", self.s()), 10.0)

	def test_free_store_freight_switch_costs_nothing(self):
		lines = [_ln(40, gun=True, method="2Day")]
		self.assertEqual(rsr_provider.estimate_freight(lines, "to_store",
			self.s(store_free_freight=1)), 0.0)
		# …and never touches drop-ship
		self.assertEqual(rsr_provider.estimate_freight(lines, "fds",
			self.s(store_free_freight=1)), 20.0)

	def test_a_stated_zero_is_a_value_not_unset(self):
		"""Review #5: rate 0 means free, not "use the default"."""
		s = self.s(rate_ground=0, free_freight_threshold=0)
		self.assertEqual(rsr_provider.estimate_freight([_ln(20)], "direct", s), 0.0)
		self.assertEqual(rsr_provider.estimate_freight([_ln(1)], "to_store", s), 0.0)

	def test_settings_override_the_defaults(self):
		s = self.s(rate_ground=12, free_freight_threshold=1000)
		self.assertEqual(rsr_provider.estimate_freight([_ln(600)], "to_store", s), 12.0)


class SSStoreFreightRules(unittest.TestCase):
	S = frappe._dict()

	def test_handguns_free_from_three(self):
		f = ss_provider.estimate_store_freight
		self.assertEqual(f([_ln(300, 2, dept="26")], self.S), 10.95)
		self.assertEqual(f([_ln(300, 3, dept="26")], self.S), 0.0)

	def test_handguns_free_from_750(self):
		f = ss_provider.estimate_store_freight
		self.assertEqual(f([_ln(749, dept="96")], self.S), 10.95)
		self.assertEqual(f([_ln(750, dept="96")], self.S), 0.0)

	def test_long_guns_and_accessories_five_under_500(self):
		f = ss_provider.estimate_store_freight
		self.assertEqual(f([_ln(499, dept="40")], self.S), 5.0)
		self.assertEqual(f([_ln(250, 2, dept="4")], self.S), 0.0)

	def test_one_parcel_pays_the_higher_charge(self):
		lines = [_ln(400, dept="26"), _ln(600, dept="40")]
		self.assertEqual(ss_provider.estimate_store_freight(lines, self.S), 10.95)

	def test_stated_zeros_are_honoured(self):
		s = frappe._dict(store_other_rate=0, store_free_handgun_qty=0)
		self.assertEqual(ss_provider.estimate_store_freight([_ln(10, dept="40")], s), 0.0)
		self.assertEqual(ss_provider.estimate_store_freight([_ln(10, dept="26")], s), 0.0)

	def test_empty_order_is_free(self):
		self.assertEqual(ss_provider.estimate_store_freight([], self.S), 0.0)


class _FakeProvider:
	"""Real-shaped plan split (guns apart from accessories) + a recorded estimate."""

	def __init__(self, freight):
		self.freight = freight
		self.calls = []

	def plan_fulfillment(self, lines, ctx):
		guns = [l for l in lines if l["is_firearm"]]
		rest = [l for l in lines if not l["is_firearm"]]
		order_type = {"to_store": "Procure To Store", "fds": "Dropship Firearm",
			"direct": "Dropship Accessory"}[ctx["mode"]]
		return [{"order_type": order_type, "lines": [
			{"sku": l["sku"], "qty": l["qty"], "ship_method": "Grnd"} for l in group]}
			for group in (guns, rest) if group]

	def estimate_order_freight(self, lines, mode, options=None):
		self.calls.append((mode, [l["sku"] for l in lines]))
		return self.freight(lines, mode)


class FulfillmentFreightEndpoint(unittest.TestCase):
	def setUp(self):
		self.providers = {
			"RSR": _FakeProvider(lambda lines, mode: 0.0 if mode == "to_store" else 10.0),
			"SS": _FakeProvider(lambda lines, mode: None),
		}
		for p in [
			mock.patch.object(dist_api.frappe, "only_for", lambda *a, **k: None),
			mock.patch.object(dist_api.frappe, "get_cached_doc",
				lambda dt, name: frappe._dict(provider=name)),
			mock.patch.object(dist_api, "get_provider", lambda code: self.providers[code]),
		]:
			p.start()
			self.addCleanup(p.stop)

	def test_mixed_cart_is_the_sum_of_its_real_orders(self):
		out = dist_api.fulfillment_freight([
			{"distributor": "RSR", "mode": "to_store", "sku": "GLK", "qty": 1, "cost": 500, "ffl_required": 1},
			{"distributor": "RSR", "mode": "to_store", "sku": "MAG", "qty": 2, "cost": 20},
			{"distributor": "RSR", "mode": "direct", "sku": "OPTIC", "qty": 1, "cost": 300},
			{"distributor": "RSR", "mode": "direct", "sku": "SIG", "qty": 1, "cost": 600, "ffl_required": 1},
		])
		kinds = sorted((o["mode"], o["order_type"]) for o in out["orders"])
		self.assertEqual(kinds, [
			("direct", "Dropship Accessory"), ("fds", "Dropship Firearm"),
			("to_store", "Procure To Store"), ("to_store", "Procure To Store")])
		self.assertEqual(out["total"], 20.0)   # two drop-ship orders × $10, store free
		self.assertTrue(out["complete"])
		store_mag = next(o for o in out["orders"] if o["mode"] == "to_store" and o["line_count"] == 1
			and o["merchandise"] == 40.0)
		self.assertEqual(store_mag["freight"], 0.0)

	def test_a_house_without_an_estimate_marks_the_total_partial(self):
		out = dist_api.fulfillment_freight([
			{"distributor": "SS", "mode": "direct", "sku": "X", "qty": 1, "cost": 10},
			{"distributor": "RSR", "mode": "direct", "sku": "Y", "qty": 1, "cost": 10},
		])
		self.assertFalse(out["complete"])
		self.assertEqual(out["total"], 10.0)
		self.assertIsNone(next(o for o in out["orders"] if o["distributor"] == "SS")["freight"])

	def test_lines_without_a_house_or_sku_are_ignored(self):
		out = dist_api.fulfillment_freight('[{"distributor": "", "sku": "A"}, {"distributor": "RSR"}]')
		self.assertEqual(out, {"orders": [], "total": 0.0, "complete": True})


class _OptionsProvider(_FakeProvider):
	"""A house with per-order delivery options: signature is on by default for guns;
	each chosen option costs $2 (insurance $3) on top of $10."""

	def delivery_options(self, firearm_order):
		return {"signature": bool(firearm_order), "adult_signature": False, "insurance": False}

	def estimate_order_freight(self, lines, mode, options=None):
		self.calls.append((mode, [l["sku"] for l in lines], options))
		o = options or {}
		return 10.0 + 2 * bool(o.get("signature")) + 2 * bool(o.get("adult_signature")) \
			+ 3 * bool(o.get("insurance"))


class FulfillmentFreightDeliveryOptions(unittest.TestCase):
	"""Create Order picks signature / adult signature / insurance per distributor order:
	the freight answer names each order's key and its options, and re-prices with them."""

	def setUp(self):
		self.providers = {"SS": _OptionsProvider(None), "RSR": _FakeProvider(lambda lines, mode: 10.0)}
		for p in [
			mock.patch.object(dist_api.frappe, "only_for", lambda *a, **k: None),
			mock.patch.object(dist_api.frappe, "get_cached_doc",
				lambda dt, name: frappe._dict(provider=name)),
			mock.patch.object(dist_api, "get_provider", lambda code: self.providers[code]),
		]:
			p.start()
			self.addCleanup(p.stop)
		self.cart = [
			{"distributor": "SS", "mode": "direct", "sku": "GUN", "qty": 1, "cost": 400, "ffl_required": 1},
			{"distributor": "SS", "mode": "direct", "sku": "MAG", "qty": 1, "cost": 20},
			{"distributor": "RSR", "mode": "direct", "sku": "OPTIC", "qty": 1, "cost": 300},
		]

	def _order(self, out, key):
		return next(o for o in out["orders"] if o["options_key"] == key)

	def test_each_order_reports_its_key_and_default_options(self):
		out = dist_api.fulfillment_freight(self.cart)
		gun = self._order(out, "SS|Dropship Firearm|1")
		mag = self._order(out, "SS|Dropship Accessory|0")
		self.assertEqual((gun["options"]["signature"], gun["freight"]), (True, 12.0))
		self.assertEqual((mag["options"]["signature"], mag["freight"]), (False, 10.0))
		self.assertEqual(out["total"], 32.0)

	def test_picked_options_reprice_that_order_only(self):
		out = dist_api.fulfillment_freight(self.cart, options='{"SS|Dropship Accessory|0": '
			'{"insurance": true}, "SS|Dropship Firearm|1": {"signature": false}}')
		self.assertEqual(self._order(out, "SS|Dropship Accessory|0")["freight"], 13.0)
		self.assertEqual(self._order(out, "SS|Dropship Firearm|1")["options"],
			{"signature": False, "adult_signature": False, "insurance": False})
		self.assertEqual(self._order(out, "SS|Dropship Firearm|1")["freight"], 10.0)

	def test_a_house_without_options_is_estimated_as_before(self):
		out = dist_api.fulfillment_freight(self.cart, options={"RSR|Dropship Accessory|0": {"insurance": True}})
		rsr = self._order(out, "RSR|Dropship Accessory|0")
		self.assertIsNone(rsr["options"])
		self.assertEqual(rsr["freight"], 10.0)
		self.assertEqual(self.providers["RSR"].calls, [("direct", ["OPTIC"])])


class FlatRate(unittest.TestCase):
	"""`freight.flat_rate`: one per-order rate by lane family and order kind."""

	def test_by_lane_and_kind(self):
		from ffl_integrations.distributor.freight import flat_rate

		rates = dict(consumer={"firearm": 9.95, "accessory": 8}, store={"accessory": 0})
		gun, mag = [{"is_firearm": True, "qty": 3}], [{"is_firearm": False, "qty": 5}]
		self.assertEqual(flat_rate(gun, "fds", **rates), 9.95, "per order, not per unit")
		self.assertEqual(flat_rate(mag, "direct", **rates), 8.0)
		self.assertEqual(flat_rate(mag, "to_store", **rates), 0.0, "a stated 0 is free")
		self.assertIsNone(flat_rate(gun, "to_store", **rates), "no rate: no estimate")
		self.assertIsNone(flat_rate(mag, "stocking", consumer=rates["consumer"]))


class LipseysFreight(unittest.TestCase):
	def test_drop_ship_flat_rates_from_settings(self):
		from ffl_integrations.distributor.lipseys_provider import estimate_freight

		gun, mag = [{"is_firearm": True, "qty": 2}], [{"is_firearm": False, "qty": 1}]
		self.assertEqual(estimate_freight(gun, "fds", frappe._dict()), 9.95, "defaults")
		self.assertEqual(estimate_freight(mag, "direct", frappe._dict()), 8.0)
		own = frappe._dict(rate_dropship_firearm=12, rate_dropship_accessory=0)
		self.assertEqual(estimate_freight(gun, "fds", own), 12.0)
		self.assertEqual(estimate_freight(mag, "direct", own), 0.0)
		self.assertIsNone(estimate_freight(mag, "to_store", own), "store orders: not estimated")
