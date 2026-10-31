"""Gun parts vs firearms (header nav v2).

The catalog service's class says what a thing is under the law; a house's
``ffl_required`` is only its shipping rule (RSR sends every barrel and upper to a
dealer). Parts get their own Item Groups and website category, and are never
firearms."""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.accessory_groups import parts_group
from ffl_integrations.distributor import rsr_provider as rsr
from ffl_integrations.distributor.base import PlanningError
from ffl_integrations.distributor.util import (
	LEGAL_FIREARM_CLASSES, PRODUCT_CLASSES, dealer_only_part, legal_firearm,
)
from ffl_integrations.tests import catalog_stub


class LegalFirearm(unittest.TestCase):
	def test_unflagged_row_is_never_a_firearm(self):
		# No house ships a firearm without an FFL — whatever its class says (prod
		# the Data Service classes every SS receivers-CATID row "Other Firearm", kits too).
		for house in ("rsr", "ss"):
			self.assertFalse(legal_firearm({"product_class": "Other Firearm", "ffl_required": False}, house))
		self.assertFalse(legal_firearm(None))

	def test_rsr_flag_is_a_shipping_rule_the_class_can_clear(self):
		for klass in ("Gun Parts", "Accessory", "Ammunition"):
			self.assertFalse(legal_firearm({"product_class": klass, "ffl_required": True}, "rsr"), klass)
		for klass in ("Handgun", "Long Gun", "Other Firearm", "NFA", "", "Receiver", "other firearm"):
			# An unknown, blank or oddly-cased class never clears a flagged row.
			self.assertTrue(legal_firearm({"product_class": klass, "ffl_required": True}, "rsr"), klass)

	def test_ss_flag_is_the_law_without_a_master_answer(self):
		# The fallback (no master legal for the product): Sports South's flag is the law.
		self.assertTrue(legal_firearm({"product_class": "Gun Parts", "ffl_required": True}, "ss"))
		self.assertTrue(legal_firearm({"product_class": "Gun Parts", "ffl_required": True,
			"distributor": "ss"}))

	def test_the_masters_legal_answer_decides_when_there_is_one(self):
		row = {"product_class": "Accessory", "ffl_required": True, "legal": {"is_firearm": False}}
		self.assertFalse(legal_firearm(row, "ss"))
		row = {"product_class": "Accessory", "ffl_required": False, "legal": {"is_firearm": True}}
		self.assertTrue(legal_firearm(row, "rsr"))
		# A product read with no master answer yet (legal None) falls back.
		self.assertTrue(legal_firearm({"ffl_required": True, "legal": None}, "ss"))

	def test_dealer_only_part(self):
		self.assertTrue(dealer_only_part({"product_class": "Gun Parts", "ffl_required": True}, "rsr"))
		self.assertFalse(dealer_only_part({"product_class": "Other Firearm", "ffl_required": True}, "rsr"))
		self.assertFalse(dealer_only_part({"product_class": "Gun Parts", "ffl_required": False}, "rsr"))

	def test_vocabulary(self):
		self.assertIn("Gun Parts", PRODUCT_CLASSES)
		self.assertIn("NFA", PRODUCT_CLASSES)
		self.assertLessEqual(LEGAL_FIREARM_CLASSES, set(PRODUCT_CLASSES))
		self.assertNotIn("Gun Parts", LEGAL_FIREARM_CLASSES)


class PartsGroup(unittest.TestCase):
	CASES = [
		# (title, department default, expected leaf) — real distributor titles
		("RADIAN UPPER 14.5\" 6MM ARC GREY", "Uppers", "Uppers"),
		("Magpul BTR Arm Brace – AR Pistol, Mil-Spec Carbine Receiver Extension, Black", None, "Stocks & Grips"),
		("SB TACTICAL SBA3 PISTOL STABILIZING BRACE BLK", "Misc Accessory", "Stocks & Grips"),
		("FIGHT MCR DUAL FD UPR 556 MLOK 16.25", "Uppers", "Uppers"),
		("CMMG 28B260CAB Resolute  22 ARC 16.10\" 7075-T6 Aluminum 15\" Handguard", "Uppers", "Uppers"),
		("BCM MK2 11.5\" 5.56 KMR-A10 M-LOK", "Uppers", "Uppers"),
		("LANTAC SPADA-ML 15\" M-LOK HNDGUARD", "Uppers", "Handguards & Rails"),
		("Faxon 16\" 5.56 Barrel", "Uppers", "Barrels"),
		("GEISSELE CHF CHROME LINED 5.56 13.9\"", "Uppers", "Barrels"),
		("BALLISTIC BBL 5.56 14.5\" CAR M4 1/7", "Uppers", "Barrels"),
		("CMC Triggers BBL556003 AR Barrel  5.56 NATO 16\" Black", "Barrels", "Barrels"),
		("WMD NIB-X BCG WITHOUT HAMMER 556", None, "Bolt Carrier Groups & Charging Handles"),
		("RISE RAVE FLAT TRIGGER W/LPK", None, "Triggers"),
		("CMC Triggers 81500 Lower Parts Kit  AR-15 Multi-Caliber", "Lower Parts & Kits", "Lower Parts & Kits"),
		("KAK AR-15 LOWER PARTS KIT COMPLETE", None, "Lower Parts & Kits"),
		("OCL OPS/AE OTB MUZZLE BRAKE HEAT TRT", None, "Muzzle Devices"),
		("Dead Air LT303 HUB Direct Thread Mount 3/4\"x24", "Barrels", "Muzzle Devices"),
		("HOGUE 9MM RUB GRP PAD 6RD FDE", None, "Stocks & Grips"),
		# Not gun parts at all, whatever part word they carry.
		("Cole-tac MP202 Metal Python Suppressor Cover Fits 7.50\"", "Barrels", None),
		("AR-15 Ejection Port Dust Cover", "Uppers", None),
		("CVA MUZZLELOADER CLEANING JAG", None, None),
		("Muzzleloader Powder Flask", None, None),
		("Master Lock Trigger Lock", None, None),
		("Wheeler AR-15 Upper Receiver Vise Block", None, None),
		("Barrel Crowning Tool", None, None),
		("BTLR CRK SHOTGUN PLUG 12/20 2.75/3\"", None, None),
		("BCM LOW PROFILE GAS BLOCK(.750 BBL)", None, None),
		# Prod audit 2026-09-26: ammo, sights and a suppressor that part words caught.
		("CCI UPPERCUT 22LR", None, None),
		("Holosun Lower 1/3 Co-Witness Mount", None, None),
		("Holosun HS507COMP 507 Comp Red Dot Sight Green 2 MOA", None, None),
		("CMMG ZEROED 46CAL SUP HUB MNT", "Muzzle Devices", None),
		("SilencerCo Omega 36M Suppressor", "Barrels", None),
		("A2 Flash Suppressor 1/2x28", None, "Muzzle Devices"),
		("Faxon 16\" 5.56 Gov 1:8", None, "Barrels"),
	]

	def test_titles(self):
		for title, default, want in self.CASES:
			self.assertEqual(parts_group(title, default), want, title)


class ClassificationGuard(unittest.TestCase):
	"""The classification guard compares the law's answer with our Item, not the house's shipping flag."""

	def setUp(self):
		self.provider = rsr.RSRProvider()

	def _line(self, **kw):
		return {"sku": "X", "item_code": "X", "qty": 1, "department": "42", **kw}

	def test_dealer_only_barrel_is_no_divergence(self):
		# RSR ships its barrels to dealers only (ffl_required) — still not a firearm.
		self.provider.assert_classification([self._line(
			ffl_required=True, legal_firearm=False, is_firearm=False)])

	def test_catalog_firearm_on_a_non_firearm_item_is_held(self):
		with self.assertRaises(PlanningError):
			self.provider.assert_classification([self._line(
				ffl_required=True, legal_firearm=True, is_firearm=False)])

	def test_line_without_legal_flag_keeps_the_old_rule(self):
		with self.assertRaises(PlanningError):
			self.provider.assert_classification([self._line(ffl_required=True, is_firearm=False)])


class DealerOnlyLane(unittest.TestCase):
	"""A part the house ships only to a dealer is planned to a buyer's door only
	when the caller re-orders a refused one to the store."""

	def test_direct_refused_to_store_allowed(self):
		from ffl_integrations.distributor import lanes
		provider = rsr.RSRProvider()
		ln = {"sku": "B", "item_code": "B", "qty": 1, "ffl_required": True,
			"legal_firearm": False, "is_firearm": False}
		with self.assertRaises(PlanningError):
			provider.assert_lane_fits(lanes.lane(lanes.DIRECT), [ln])
		provider.assert_lane_fits(lanes.lane(lanes.TO_STORE), [ln])

	def test_direct_allowed_when_the_caller_falls_back_to_store(self):
		from ffl_integrations.distributor import lanes
		from ffl_integrations.distributor.util import TO_STORE_ON_REFUSAL
		ln = {"sku": "B", "item_code": "B", "qty": 1, "ffl_required": True,
			"legal_firearm": False, "is_firearm": False, TO_STORE_ON_REFUSAL: True}
		rsr.RSRProvider().assert_lane_fits(lanes.lane(lanes.DIRECT), [ln])


class LegalFlagPassThrough(unittest.TestCase):
	"""The planner's freight estimate and the order it creates carry the law's
	answer (``legal_firearm``), not the house's dealer-only flag."""

	BARREL = {"sku": "BBL", "ffl_required": True, "product_class": "Gun Parts",
		"department": "42", "cost": 150.0, "model": "BBL"}

	class _Provider:
		code = "rsr"

		def __init__(self, row=None):
			self.row, self.planned = row, []

		def catalog_row(self, sku):
			return self.row

		def plan_fulfillment(self, lines, ctx):
			self.planned.append((ctx["mode"], [dict(ln) for ln in lines]))
			return [{"order_type": "Procure To Store", "lines": [
				{"sku": ln["sku"], "qty": ln["qty"], "ship_method": "Grnd"} for ln in lines]}]

		def estimate_order_freight(self, lines, mode, options=None):
			return 10.0

	def _patch(self, provider, **extra):
		from unittest import mock
		from ffl_integrations.distributor import api
		for p in [
			mock.patch.object(api.frappe, "only_for", lambda *a, **k: None),
			mock.patch.object(api.frappe, "get_cached_value", lambda *a, **k: "rsr"),
			mock.patch.object(api.frappe, "get_cached_doc", lambda *a, **k: frappe._dict(provider="rsr")),
			mock.patch.object(api, "get_provider", lambda code: provider),
			*[mock.patch.object(api, k, v) for k, v in extra.items()],
		]:
			p.start()
			self.addCleanup(p.stop)
		return api

	def test_freight_barrel_is_no_firearm(self):
		provider = self._Provider()
		api = self._patch(provider)
		out = api.fulfillment_freight([dict(self.BARREL, distributor="RSR", mode="to_store", qty=1)])
		self.assertFalse(out["orders"][0]["firearm"])
		line = provider.planned[0][1][0]
		self.assertTrue(line["ffl_required"])
		self.assertFalse(line["legal_firearm"])
		self.assertFalse(line["is_firearm"])

	def test_create_orders_barrel_is_no_firearm(self):
		provider, seen = self._Provider(self.BARREL), {}

		def lane_block(distributor, mode, *, is_firearm=False):
			seen["is_firearm"] = is_firearm
			return None

		api = self._patch(provider, lane_block=lane_block,
			create_do_from_plan=lambda dist, plan, by_sku: frappe._dict(
				name="DO-T", order_type=plan["order_type"], account="dealer", lines=plan["lines"]))
		from unittest import mock
		with mock.patch.object(api.frappe, "get_doc", lambda *a, **k: frappe._dict(provider="rsr")):
			created = api._create_orders("RSR", [{"sku": "BBL", "qty": 1, "ffl_required": True,
				"product_class": "Gun Parts"}], {"mode": "to_store"})
		self.assertEqual(created[0]["name"], "DO-T")
		self.assertFalse(seen["is_firearm"])
		line = provider.planned[0][1][0]
		self.assertTrue(line["ffl_required"])
		self.assertFalse(line["legal_firearm"])
		self.assertFalse(line["is_firearm"])


class SearchFirearmFlag(FrappeTestCase):
	"""A search offer carries no class: the flag comes from the full row's class."""

	def setUp(self):
		catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "SRCH-BBL", "upc": "", "department_number": "42",
			 "short_description": "SRCHTEST BBL 5.56 16", "ffl_required": 1,
			 "product_class": "Gun Parts", "dealer_price": 150.0, "quantity": 3},
			{"rsr_stock_number": "SRCH-GUN", "upc": "", "department_number": "05",
			 "short_description": "SRCHTEST RIFLE 5.56", "ffl_required": 1,
			 "dealer_price": 500.0, "quantity": 3},
		], distributor="rsr")

	def tearDown(self):
		catalog_stub.uninstall()

	def test_named_search_rows(self):
		rows = {r["sku"]: r for r in rsr.RSRProvider().search("SRCHTEST", 10)}
		self.assertFalse(rows["SRCH-BBL"]["is_firearm"])
		self.assertTrue(rows["SRCH-BBL"]["ffl_required"])
		self.assertTrue(rows["SRCH-GUN"]["is_firearm"])

	def test_unnamed_search_rows(self):
		from unittest import mock
		from ffl_integrations.distributor import api
		with mock.patch.object(api.frappe, "get_all",
				lambda *a, **k: [frappe._dict(provider="rsr", name="RSR-T")]):
			rows = {r["sku"]: r for r in api.product_search("SRCHTEST", 10)}
		self.assertFalse(rows["SRCH-BBL"]["is_firearm"])
		self.assertTrue(rows["SRCH-BBL"]["ffl_required"])
		self.assertTrue(rows["SRCH-GUN"]["is_firearm"])


class PromoteParts(FrappeTestCase):
	"""Promotion files a gun part in Parts and never as a firearm; a serialized
	receiver the service classes a firearm stays one."""

	CODES = ("PARTS-RSR-BBL", "PARTS-RSR-UPR-GUN", "PARTS-RSR-COVER", "PARTS-SS-LPK",
		"PARTS-SS-UPPER", "PARTS-SS-RECEIVER", "PARTS-SS-FLAGGED", "PARTS-SS-AIRGUN", "PARTS-SS-LIGHT")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		from ffl_integrations.setup import rsr_seed
		test_bootstrap.run()
		rsr_seed.run()
		item_groups.ensure()

	def setUp(self):
		for code in self.CODES:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "PARTS-RSR-BBL", "upc": "", "department_number": "42",
			 "short_description": "BALLISTIC BBL 5.56 14.5\" CAR M4 1/7", "ffl_required": 1,
			 "product_class": "Gun Parts", "dealer_price": 150.0, "map_price": 200.0},
			{"rsr_stock_number": "PARTS-RSR-UPR-GUN", "upc": "", "department_number": "41",
			 "short_description": "TAC SOL PAC-LITE UPPER 22LR 4.5\"", "ffl_required": 1,
			 "product_class": "Other Firearm", "dealer_price": 250.0, "map_price": 300.0},
			{"rsr_stock_number": "PARTS-RSR-COVER", "upc": "", "department_number": "41",
			 "short_description": "AR-15 EJECTION PORT DUST COVER BLK", "ffl_required": 1,
			 "product_class": "Gun Parts", "dealer_price": 10.0, "map_price": 15.0},
		], distributor="rsr")
		catalog_stub.seed_catalog([
			{"sku": "PARTS-SS-LPK", "upc": "", "department": "66", "ffl_required": 0,
			 "short_description": "CMMG 55CA601 Lower Parts Kit Gun Builders Kit Black AR15",
			 # As the Data Service classes the whole CATID today.
			 "product_class": "Other Firearm", "cost": 29.0, "map": 40.0},
			{"sku": "PARTS-SS-UPPER", "upc": "", "department": "63", "ffl_required": 0,
			 "short_description": "Rock River Arms LAR-15M Complete Upper 5.56 16\"",
			 "product_class": "Other Firearm", "cost": 500.0, "map": 650.0},
			{"sku": "PARTS-SS-RECEIVER", "upc": "", "department": "63", "ffl_required": 1,
			 "short_description": "Faxon FF22RECEIVER01 Rimfire FF-22 Receiver Kit for 10/22",
			 "product_class": "Other Firearm", "cost": 120.0, "map": 160.0},
			# SS flags it and the service classes it a firearm, though its CATID is
			# no firearm one.
			{"sku": "PARTS-SS-FLAGGED", "upc": "", "department": "13", "ffl_required": 1,
			 "short_description": "Flagged Frame Assembly", "product_class": "Other Firearm",
			 "cost": 90.0, "map": 120.0},
			# SS flags a light; the master's rule (SS 31) says no firearm.
			{"sku": "PARTS-SS-LIGHT", "upc": "", "department": "31", "ffl_required": 1,
			 "short_description": "Streamlight TLR-1 HL Weapon Light", "product_class": "Accessory",
			 "cost": 90.0, "map": 120.0},
			# Unflagged in a long-gun CATID: an air rifle.
			{"sku": "PARTS-SS-AIRGUN", "upc": "", "department": "40", "ffl_required": 0,
			 "short_description": "Crosman Pumpmaster Air Rifle .177",
			 "product_class": "Long Gun", "cost": 60.0, "map": 80.0},
		], distributor="ss")

	def tearDown(self):
		catalog_stub.uninstall()

	def _item(self, code):
		return frappe.db.get_value("Item", code,
			["item_group", "is_firearm", "has_serial_no", "firearm_type"], as_dict=True)

	def test_rsr_dealer_only_barrel_is_a_part(self):
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "PARTS-RSR-BBL", "item_code": "PARTS-RSR-BBL"})
		it = self._item("PARTS-RSR-BBL")
		self.assertEqual(it.item_group, "Barrels")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)
		self.assertFalse(it.firearm_type)

	def test_rsr_part_the_title_does_not_place_never_lands_in_rifle(self):
		# Dept 41 defaults to the Rifle group; a dust cover must not become a gun.
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "PARTS-RSR-COVER", "item_code": "PARTS-RSR-COVER"})
		it = self._item("PARTS-RSR-COVER")
		self.assertEqual(it.item_group, "Misc Accessory")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)

	def test_order_lane_agrees_with_promotion(self):
		# The lane (FDS vs direct) and the Item must give one answer.
		from ffl_integrations.distributor import api
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-LPK", {"item_code": "PARTS-SS-LPK"})
		provider = api.get_provider("ss")
		line = api.enrich_line(provider, {"sku": "PARTS-SS-LPK", "item_code": "PARTS-SS-LPK", "qty": 1})
		self.assertFalse(line["legal_firearm"])
		self.assertFalse(line["is_firearm"])
		provider.assert_classification([line])
		rsr_line = api.enrich_line(api.get_provider("rsr"), {"sku": "PARTS-RSR-BBL", "qty": 1})
		self.assertTrue(rsr_line["ffl_required"])
		self.assertFalse(rsr_line["legal_firearm"])

	def test_rsr_firearm_upper_stays_a_firearm(self):
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "PARTS-RSR-UPR-GUN", "item_code": "PARTS-RSR-UPR-GUN"})
		it = self._item("PARTS-RSR-UPR-GUN")
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)
		self.assertEqual(it.firearm_type, "Receiver")

	def test_ss_lower_parts_kit_is_a_part(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-LPK", {"item_code": "PARTS-SS-LPK"})
		it = self._item("PARTS-SS-LPK")
		self.assertEqual(it.item_group, "Lower Parts & Kits")
		self.assertFalse(it.is_firearm)

	def test_ss_complete_upper_is_a_part(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-UPPER", {"item_code": "PARTS-SS-UPPER"})
		it = self._item("PARTS-SS-UPPER")
		self.assertEqual(it.item_group, "Uppers")
		self.assertFalse(it.is_firearm)

	def test_ss_flagged_row_outside_firearm_catids_is_other_firearm(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-FLAGGED", {"item_code": "PARTS-SS-FLAGGED"})
		it = self._item("PARTS-SS-FLAGGED")
		self.assertEqual(it.item_group, "Other Firearm")
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)

	def test_ss_flagged_row_the_master_says_is_no_firearm_is_not_one(self):
		# Rule: SS 31 lights are not firearms (Data Service master legal).
		from ffl_integrations.sports_south.promote import promote_to_item
		self.stub.set_master("ss:PARTS-SS-LIGHT", "accessories-weapon-lights", is_firearm=False)
		promote_to_item("PARTS-SS-LIGHT", {"item_code": "PARTS-SS-LIGHT"})
		it = self._item("PARTS-SS-LIGHT")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)
		self.assertFalse(it.firearm_type)
		self.assertNotIn(it.item_group, ("Other Firearm", "Pistol", "Rifle"))

	def test_ss_unflagged_row_in_a_firearm_catid_is_an_accessory(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-AIRGUN", {"item_code": "PARTS-SS-AIRGUN"})
		it = self._item("PARTS-SS-AIRGUN")
		self.assertEqual(it.item_group, "Misc Accessory")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)

	def test_item_form_lookup_files_a_barrel_in_parts(self):
		from ffl_integrations.rsr.promote import lookup_for_item_form
		out = lookup_for_item_form("PARTS-RSR-BBL")
		self.assertTrue(out["found"])
		self.assertEqual(out["defaults"]["item_group"], "Barrels")
		self.assertFalse(out["defaults"]["has_serial_no"])
		self.assertFalse(out["defaults"]["firearm_type"])
		self.assertFalse(out["rsr"]["is_firearm"])

	def test_ss_serialized_receiver_stays_a_firearm(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("PARTS-SS-RECEIVER", {"item_code": "PARTS-SS-RECEIVER"})
		it = self._item("PARTS-SS-RECEIVER")
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)


class PartsItemGroups(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups
		item_groups.ensure()

	def test_parts_tree(self):
		from ffl_core.firearm import ALL_FIREARM_GROUPS, PARTS_LEAVES
		self.assertTrue(frappe.db.get_value("Item Group", "Parts", "is_group"))
		for leaf in PARTS_LEAVES:
			self.assertEqual(frappe.db.get_value("Item Group", leaf, "parent_item_group"), "Parts", leaf)
			self.assertNotIn(leaf, ALL_FIREARM_GROUPS)
		self.assertNotIn("Parts", ALL_FIREARM_GROUPS)
		self.assertEqual(frappe.db.get_value("Item Group", "Optic Mounts", "parent_item_group"), "Accessory")

	def test_parts_count_as_accessories_in_inventory_receipts(self):
		# Handguards and stocks moved out of Accessory: the report's basis must not.
		from ffl_core.ffl_core.report.inventory_receipts.inventory_receipts import _TOP_GROUP_CATEGORY
		self.assertEqual(_TOP_GROUP_CATEGORY["Parts"], _TOP_GROUP_CATEGORY["Accessory"])


class PromoteLipseys(FrappeTestCase):
	"""Lipsey's runs the shared catalog promoter with its own firearm-type map and
	caliber gate (firearms only)."""

	CODES = ("PROMO-LIP-PISTOL", "PROMO-LIP-AMMO")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()

	def setUp(self):
		for code in self.CODES:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		catalog_stub.install()
		catalog_stub.seed_catalog([
			{"sku": "PROMO-LIP-PISTOL", "upc": "", "department": "Semi-Auto Pistol",
			 "ffl_required": 1, "product_class": "Handgun",
			 "short_description": "Glock 19 Gen5 9mm 15rd", "cost": 450.0, "map": 549.0,
			 "msrp": 649.0},
			{"sku": "PROMO-LIP-AMMO", "upc": "", "department": "Accessory-Ammunition",
			 "ffl_required": 0, "product_class": "Ammunition",
			 "short_description": "Federal 9mm 115gr FMJ 50rd", "cost": 15.0, "map": 20.0},
		], distributor="lipseys")

	def tearDown(self):
		catalog_stub.uninstall()

	def test_pistol_is_a_serialized_pistol_at_map(self):
		from ffl_integrations.lipseys.promote import promote_to_item
		out = promote_to_item("PROMO-LIP-PISTOL", {"item_code": "PROMO-LIP-PISTOL"})
		self.assertEqual(out["item_code"], "PROMO-LIP-PISTOL")
		it = frappe.db.get_value("Item", "PROMO-LIP-PISTOL",
			["is_firearm", "has_serial_no", "firearm_type", "standard_rate", "dealer_price",
			 "serial_no_series"], as_dict=True)
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)
		self.assertEqual(it.firearm_type, "Pistol")
		self.assertEqual(float(it.standard_rate), 549.0)
		self.assertEqual(float(it.dealer_price), 450.0)
		self.assertEqual(it.serial_no_series, "PROMO-LIP-PISTOL-.####")

	def test_ammo_keeps_a_blank_caliber_and_no_firearm_fields(self):
		# Lipsey's reads a caliber from the description for firearms only.
		from ffl_integrations.lipseys.promote import promote_to_item
		promote_to_item("PROMO-LIP-AMMO", {"item_code": "PROMO-LIP-AMMO"})
		it = frappe.db.get_value("Item", "PROMO-LIP-AMMO",
			["is_firearm", "has_serial_no", "firearm_type", "caliber"], as_dict=True)
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)
		self.assertFalse(it.firearm_type)
		self.assertFalse(it.caliber)

	def test_unknown_item_names_the_house(self):
		from ffl_integrations.lipseys.promote import promote_to_item
		with self.assertRaisesRegex(frappe.ValidationError, "Lipsey's item PROMO-LIP-NONE not found"):
			promote_to_item("PROMO-LIP-NONE")
