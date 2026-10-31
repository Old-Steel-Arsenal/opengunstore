"""The catalog master's category tree in the POS (milestone M3b): the tree mirror,
Item Group links, filing a promoted product, and planning a product's move."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import catalog_master as cm
from ffl_integrations.data_service.client import DataServiceUnavailable
from ffl_integrations.tests import catalog_stub


class _TreeCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()

	# Groups the mirror creates; the class rolls back only at its end.
	CREATED = ("Bipods & Rests", "Gas System Parts", "Range Gear", "Receiver", "Antique")

	def setUp(self):
		frappe.db.delete("Catalog Category")
		for group in self.CREATED:
			if frappe.db.exists("Item Group", group):
				frappe.delete_doc("Item Group", group, ignore_permissions=True, force=True)
		frappe.db.set_single_value(cm.SETTINGS, {"category_mirror": cm.MODE_APPLY,
			"category_tree_cursor": "", "category_products_cursor": "", "category_products_baselined": 0,
			"category_mirror_report": "", "category_mirror_issues": 0})
		self.stub = catalog_stub.install()
		self.stub.seed_tree(catalog_stub.TREE_A)
		# No store in these tests: the Woo half has its own suite.
		p = mock.patch("ffl_woo_sync.woocommerce.categories.configured_stores", return_value=[])
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self):
		catalog_stub.uninstall()

	def _row(self, slug):
		return frappe.db.get_value(cm.DOCTYPE, slug, ["node_name", "parent_slug", "path", "item_group",
			"group_parent", "active", "level"], as_dict=True)


class TreeMirror(_TreeCase):
	def test_full_read_mirrors_every_node_with_its_path(self):
		report = cm.run_mirror()
		self.assertEqual(report["tree"]["read"], "full")
		self.assertEqual(frappe.db.count(cm.DOCTYPE), len(catalog_stub.TREE_A))
		row = self._row("firearms-suppressors")
		self.assertEqual(row.parent_slug, "firearms-nfa")
		self.assertEqual(row.path, "Firearms › NFA › Suppressors")
		self.assertEqual(row.level, 3)
		self.assertEqual(frappe.db.get_single_value(cm.SETTINGS, "category_tree_cursor"),
			str(self.stub.tree_seq))

	def test_agreed_and_same_named_groups_are_linked(self):
		cm.run_mirror()
		self.assertEqual(self._row("firearms-pistols").item_group, "Pistol")
		self.assertEqual(self._row("optics").item_group, "Optic")
		self.assertEqual(self._row("accessories-other").item_group, "Misc Accessory")
		self.assertEqual(self._row("parts-handguards").item_group, "Handguards & Rails")  # by name
		self.assertEqual(self._row("firearms-nfa").item_group, "NFA")                     # by name
		self.assertEqual(self._row("firearms-suppressors").item_group, "Suppressor")

	def test_a_node_under_a_leaf_group_files_as_that_leaf(self):
		cm.run_mirror()
		self.assertFalse(self._row("optics-red-dots").item_group)
		self.assertEqual(cm.group_for("optics-red-dots"), "Optic")
		self.assertEqual(cm.group_for("parts-barrels-rifle"), "Barrels")

	def test_apply_creates_a_group_under_the_parents_group(self):
		self.assertFalse(frappe.db.exists("Item Group", "Bipods & Rests"))
		report = cm.run_mirror()
		self.assertEqual(self._row("accessories-bipods-rests").item_group, "Bipods & Rests")
		self.assertEqual(frappe.db.get_value("Item Group", "Bipods & Rests", "parent_item_group"), "Accessory")
		self.assertIn("create group", {a["action"] for a in report["groups"]["actions"]})

	def test_report_only_links_existing_groups_and_creates_nothing(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		report = cm.run_mirror()
		self.assertFalse(frappe.db.exists("Item Group", "Gas System Parts"))
		self.assertFalse(self._row("parts-gas-system").item_group)
		self.assertIn({"category": "parts-gas-system", "action": "create group"},
			[{"category": a["category"], "action": a["action"]} for a in report["groups"]["actions"]])
		self.assertEqual(self._row("firearms-rifles").item_group, "Rifle")

	def test_a_firearm_node_whose_agreed_group_is_missing_is_reported_not_created(self):
		report = cm.run_mirror()   # no Receiver group on this site
		self.assertFalse(frappe.db.exists("Item Group", "Receivers & Frames"))
		self.assertFalse(self._row("firearms-receivers-frames").item_group)
		self.assertIn("firearms-receivers-frames", {i["category"] for i in report["groups"]["issues"]})
		self.assertGreater(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_issues"), 0)

	def test_receivers_and_antiques_link_to_the_owners_existing_groups(self):
		# Receiver (under Firearm) and root Antique, as they exist
		# on the stores' POS — linked, never created, never moved.
		frappe.get_doc({"doctype": "Item Group", "item_group_name": "Receiver",
			"parent_item_group": "Firearm", "is_group": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Item Group", "item_group_name": "Antique",
			"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		report = cm.run_mirror()
		self.assertEqual(self._row("firearms-receivers-frames").item_group, "Receiver")
		self.assertEqual(self._row("firearms-muzzleloaders-antiques").item_group, "Antique")
		self.assertEqual(frappe.db.get_value("Item Group", "Antique", "parent_item_group"), "All Item Groups")
		self.assertEqual(frappe.db.get_value("Item Group", "Receiver", "parent_item_group"), "Firearm")
		self.assertNotIn("firearms-receivers-frames", {i["category"] for i in report["groups"]["issues"]})
		from ffl_core.firearm import ALL_FIREARM_GROUPS
		self.assertIn("Receiver", ALL_FIREARM_GROUPS)         # r1 M3: one firearm-group definition
		self.assertTrue(cm.is_firearm_group("Receiver"))      # under Firearm: never moved
		# A catalog receiver files there, a firearm; a muzzleloader in Antique, not one.
		self.assertEqual(cm.group_for_product("firearms-receivers-frames", is_firearm=True), "Receiver")
		self.assertIsNone(cm.group_for_product("firearms-receivers-frames", is_firearm=False))
		self.assertEqual(cm.group_for_product("firearms-muzzleloaders-antiques", is_firearm=False), "Antique")
		# Antique's node moving later is reported, not applied (its new parent is Firearm).
		self.stub.edit_node("firearms-muzzleloaders-antiques", parent="firearms-nfa")
		report = cm.run_mirror()
		self.assertEqual(frappe.db.get_value("Item Group", "Antique", "parent_item_group"), "All Item Groups")
		self.assertIn("firearms-muzzleloaders-antiques", {i["category"] for i in report["groups"]["issues"]})

	def test_the_tree_feed_pages_until_done(self):
		cm.run_mirror()
		self.stub.categories_page_size = 2
		for slug in ("parts-barrels", "parts-triggers", "optics", "magazines", "accessories-other"):
			self.stub.edit_node(slug, name=f"{slug} renamed")
		report = cm.run_mirror()
		self.assertEqual(report["tree"]["read"], "changes")
		for slug in ("parts-barrels", "accessories-other"):
			self.assertEqual(self._row(slug).node_name, f"{slug} renamed")
		self.assertEqual(frappe.db.get_single_value(cm.SETTINGS, "category_tree_cursor"),
			str(self.stub.tree_seq))

	def test_the_first_run_baselines_the_product_feed_in_any_mode(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		report = cm.run_mirror()
		self.assertIn("product_feed_baseline", report)
		self.assertTrue(frappe.db.get_single_value(cm.SETTINGS, "category_products_baselined"))

	def test_off_runs_nothing(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_OFF)
		self.assertEqual(cm.run_mirror(), {"mode": cm.MODE_OFF})
		self.assertEqual(frappe.db.count(cm.DOCTYPE), 0)

	def test_a_rename_follows_by_the_feed_and_recomputes_child_paths(self):
		cm.run_mirror()
		self.stub.edit_node("firearms-nfa", name="NFA Items")
		report = cm.run_mirror()
		self.assertEqual(report["tree"]["read"], "changes")
		self.assertEqual(self._row("firearms-nfa").node_name, "NFA Items")
		self.assertEqual(self._row("firearms-suppressors").path, "Firearms › NFA Items › Suppressors")
		self.assertEqual(self._row("firearms-nfa").item_group, "NFA")  # the group is never renamed

	def test_a_moved_node_re_parents_its_group(self):
		cm.run_mirror()
		self.stub.seed_tree([("accessories-range", "Range Gear", "accessories", None)])
		cm.run_mirror()
		self.assertEqual(frappe.db.get_value("Item Group", "Range Gear", "parent_item_group"), "Accessory")
		self.stub.edit_node("accessories-range", parent="parts")
		cm.run_mirror()
		self.assertEqual(frappe.db.get_value("Item Group", "Range Gear", "parent_item_group"), "Parts")
		self.assertEqual(self._row("accessories-range").group_parent, "parts")

	def test_a_moved_firearm_node_never_moves_its_group(self):
		cm.run_mirror()
		self.stub.edit_node("firearms-shotguns", parent="firearms-nfa")
		report = cm.run_mirror()
		self.assertEqual(frappe.db.get_value("Item Group", "Shotgun", "parent_item_group"), "Firearm")
		self.assertIn("firearms-shotguns", {i["category"] for i in report["groups"]["issues"]})

	def test_the_seed_does_not_undo_a_mirror_move(self):
		from ffl_core.setup import item_groups
		cm.run_mirror()
		self.stub.edit_node("optics-mounts-rings", parent="parts")
		cm.run_mirror()
		self.assertEqual(frappe.db.get_value("Item Group", "Optic Mounts", "parent_item_group"), "Parts")
		item_groups.ensure()
		self.assertEqual(frappe.db.get_value("Item Group", "Optic Mounts", "parent_item_group"), "Parts")

	def test_service_unreachable_is_reported_and_changes_nothing(self):
		self.stub.categories_error = DataServiceUnavailable("down")
		report = cm.run_mirror()
		self.assertIn("down", report["error"])
		self.assertEqual(frappe.db.count(cm.DOCTYPE), 0)
		self.assertEqual(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_issues"), 1)

	def test_the_error_log_is_written_once_per_new_set_of_issues(self):
		with mock.patch.object(cm.frappe, "log_error") as log:
			cm.run_mirror()
			cm.run_mirror()
		self.assertEqual(log.call_count, 1)

	def test_run_button_queues_the_mirror(self):
		out = cm.run_category_mirror(full=1)
		self.assertTrue(out["queued"])
		self.assertEqual(frappe.db.count(cm.DOCTYPE), len(catalog_stub.TREE_A))


class FilingProducts(_TreeCase):
	"""Promotion files a catalog product under the master's category."""

	def setUp(self):
		super().setUp()
		cm.run_mirror()
		for code in ("M3B-BBL", "M3B-RIFLE", "M3B-LIGHT", "M3B-FALLBACK", "M3B-SS-DOT", "M3B-BP"):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "M3B-BBL", "upc": "700000000016", "department_number": "32",
			 "short_description": "FAXON 16IN 5.56 GOV 1:8", "ffl_required": 1,
			 "product_class": "Gun Parts", "dealer_price": 120.0, "map_price": 170.0},
			{"rsr_stock_number": "M3B-RIFLE", "upc": "700000000023", "department_number": "05",
			 "short_description": "BERGARA B14 RIDGE 6.5CM", "ffl_required": 1,
			 "dealer_price": 700.0, "map_price": 900.0},
			# Black powder: RSR ships it to the door (unflagged) — no firearm under the
			# law, so no 4473 type and no serial (the old department table filed it
			# in Other Firearm).
			{"rsr_stock_number": "M3B-BP", "upc": "", "department_number": "07",
			 "short_description": "TC PRO HUNTER 50CAL MUZZLELOADER", "ffl_required": 0,
			 "dealer_price": 500.0, "map_price": 650.0},
			{"rsr_stock_number": "M3B-FALLBACK", "upc": "700000000030", "department_number": "13",
			 "short_description": "MAGPUL PMAG 30RD GEN M3 5.56", "ffl_required": 0,
			 "dealer_price": 10.0, "map_price": 15.0},
		], distributor="rsr")
		catalog_stub.seed_catalog([
			{"sku": "M3B-SS-DOT", "upc": "700000000047", "department": "39", "ffl_required": 0,
			 "short_description": "Holosun 507C X2 Red Dot", "cost": 200.0, "map": 300.0},
		], distributor="ss")
		self.stub.set_master("700000000016", "parts-barrels-rifle")
		self.stub.set_master("700000000023", "firearms-rifles", is_firearm=True)
		self.stub.set_master("700000000047", "optics-red-dots")

	def _item(self, code):
		return frappe.db.get_value("Item", code, ["item_group", "is_firearm", "has_serial_no",
			"catalog_category", "firearm_type"], as_dict=True)

	def test_a_catalog_barrel_lands_in_barrels_not_a_firearm(self):
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "M3B-BBL", "item_code": "M3B-BBL"})
		it = self._item("M3B-BBL")
		self.assertEqual(it.item_group, "Barrels")
		self.assertEqual(it.catalog_category, "parts-barrels-rifle")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)

	def test_a_catalog_rifle_lands_in_rifles_a_serialized_firearm(self):
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "M3B-RIFLE", "item_code": "M3B-RIFLE"})
		it = self._item("M3B-RIFLE")
		self.assertEqual(it.item_group, "Rifle")
		self.assertEqual(it.catalog_category, "firearms-rifles")
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)
		self.assertEqual(it.firearm_type, "Rifle")

	def test_an_unflagged_black_powder_rifle_is_no_firearm(self):
		from ffl_integrations.rsr.promote import lookup_for_item_form, promote_to_item
		promote_to_item({"rsr_stock_number": "M3B-BP", "item_code": "M3B-BP"})
		it = self._item("M3B-BP")
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)
		self.assertFalse(it.firearm_type)
		self.assertNotIn(it.item_group, ("Other Firearm", "Rifle"))
		out = lookup_for_item_form("M3B-BP")
		self.assertEqual((out["defaults"]["has_serial_no"], out["defaults"]["firearm_type"]), (0, None))

	def test_sports_south_files_the_same_way(self):
		from ffl_integrations.sports_south.promote import promote_to_item
		promote_to_item("M3B-SS-DOT", {"item_code": "M3B-SS-DOT"})
		it = self._item("M3B-SS-DOT")
		self.assertEqual(it.item_group, "Optic")
		self.assertEqual(it.catalog_category, "optics-red-dots")

	def test_a_scanned_non_rsr_row_promotes_through_its_own_house(self):
		# Receive Goods' scan-first dialog sends the offer's Distributor with the SKU;
		# a Sports South SKU must not be looked up in the RSR catalog.
		from ffl_integrations.rsr.promote import promote_to_item
		if frappe.db.exists("Distributor", "M3B-SS"):
			frappe.delete_doc("Distributor", "M3B-SS", ignore_permissions=True, force=True)
		frappe.get_doc({"doctype": "Distributor", "code": "M3B-SS", "distributor_name": "M3B SS",
			"provider": "ss", "enabled": 0, "dropship_enabled": 0, "stocking_enabled": 0,
			"poll_enabled": 0}).insert(ignore_permissions=True)
		out = promote_to_item(frappe.as_json({"rsr_stock_number": "M3B-SS-DOT",
			"item_code": "M3B-SS-DOT", "distributor": "M3B-SS", "uniform_sell_price": 1}))
		self.assertTrue(out["found"])
		self.assertEqual(self._item("M3B-SS-DOT").item_group, "Optic")

	def test_no_master_category_falls_back_to_the_title(self):
		from ffl_integrations.rsr.promote import promote_to_item
		promote_to_item({"rsr_stock_number": "M3B-FALLBACK", "item_code": "M3B-FALLBACK"})
		it = self._item("M3B-FALLBACK")
		self.assertEqual(it.item_group, "Magazine")
		self.assertFalse(it.catalog_category)

	def test_master_unreachable_still_promotes_by_the_fallback(self):
		from ffl_integrations.rsr.promote import promote_to_item
		with mock.patch("ffl_integrations.distributor.catalog.batch",
				side_effect=DataServiceUnavailable("down")), \
				mock.patch("ffl_integrations.distributor.catalog.legal_of",
				side_effect=DataServiceUnavailable("down")):
			promote_to_item({"rsr_stock_number": "M3B-BBL", "item_code": "M3B-BBL"})
		it = self._item("M3B-BBL")
		self.assertEqual(it.item_group, "Barrels")   # the title says barrel
		self.assertFalse(it.catalog_category)
		self.assertFalse(it.is_firearm)

	def test_a_non_firearm_never_files_in_a_firearm_group(self):
		self.stub.set_master("700000000016", "firearms-rifles")  # a mis-filed barrel
		filed = cm.classify(self.stub.rows("rsr", ["M3B-BBL"])["M3B-BBL"], "rsr")
		self.assertFalse(filed.is_firearm)
		self.assertEqual(filed.item_group, "Barrels")        # the fallback, not Rifle
		self.assertEqual(filed.category, "firearms-rifles")

	def test_a_category_that_files_as_a_group_node_uses_the_fallback(self):
		self.stub.set_master("700000000023", "firearms-receivers-frames", is_firearm=True)
		filed = cm.classify(self.stub.rows("rsr", ["M3B-RIFLE"])["M3B-RIFLE"], "rsr")
		self.assertTrue(filed.is_firearm)
		self.assertEqual(filed.item_group, "Rifle")   # class Long Gun → Rifle, not the Firearm node

	def test_item_form_lookup_carries_the_category(self):
		from ffl_integrations.rsr.promote import lookup_for_item_form
		out = lookup_for_item_form("M3B-BBL")
		self.assertEqual(out["defaults"]["item_group"], "Barrels")
		self.assertEqual(out["defaults"]["catalog_category"], "parts-barrels-rifle")
		self.assertEqual(out["defaults"]["is_firearm"], 0)
		self.assertEqual(out["rsr"]["category"], "parts-barrels-rifle")


class FallbackGroups(_TreeCase):
	def test_class_and_title(self):
		cases = [
			({"product_class": "Handgun"}, True, "GLOCK 19", "Pistol"),
			({"product_class": "Long Gun"}, True, "REM 870", "Rifle"),
			({"product_class": "NFA"}, True, "SILENCERCO OMEGA 36M SUPPRESSOR", "Suppressor"),
			({"product_class": "NFA"}, True, "SBR 10.5 5.56", "NFA"),
			({"product_class": "Other Firearm"}, True, "FRAME", "Other Firearm"),
			({"product_class": "Ammunition"}, False, "FED 9MM 115GR FMJ 50RD", "Pistol Ammo"),
			({"product_class": "Ammunition"}, False, "MYSTERY BOX", "Ammunition"),
			({"product_class": "Gun Parts"}, False, "BCM BCG 5.56", "Bolt Carrier Groups & Charging Handles"),
			({"product_class": "Accessory"}, False, "STREAMLIGHT TLR-1 LIGHT", "Misc Accessory"),
			({"product_class": "Accessory"}, False, "MAGPUL MS1 SLING", "Slings & Mounts"),
			({"product_class": "Accessory"}, False, "GEISSELE TRIGGER SSA-E", "Triggers"),
		]
		for row, firearm, title, want in cases:
			self.assertEqual(cm.fallback_group(row, is_firearm=firearm, title=title), want, title)


class PlanningItems(_TreeCase):
	def setUp(self):
		super().setUp()
		cm.run_mirror()

	def _item(self, **kw):
		return frappe._dict({"name": "X", "item_name": "X", "upc": "", "item_group": "Misc Accessory",
			"catalog_category": None, "is_firearm": 0, "has_serial_no": 0, **kw})

	def _product(self, slug, firearm=False):
		return {"category": {"slug": slug}, "legal": {"is_firearm": firearm, "nfa_type": "none",
			"is_antique": False}}

	def test_a_part_moves_to_its_category_and_group(self):
		change = cm.plan_item(self._item(), self._product("parts-barrels"))
		self.assertEqual(change["item_group"], "Barrels")
		self.assertEqual(change["catalog_category"], "parts-barrels")
		self.assertEqual((change["old_group"], change["new_group"]), ("Misc Accessory", "Barrels"))

	def test_a_serialized_item_is_never_touched(self):
		self.assertIsNone(cm.plan_item(self._item(has_serial_no=1, item_group="Pistol"),
			self._product("firearms-rifles", True)))

	def test_a_firearm_is_not_regrouped_and_a_legal_difference_is_only_reported(self):
		change = cm.plan_item(self._item(is_firearm=1, item_group="Other Firearm"),
			self._product("parts-barrels", False))
		self.assertNotIn("item_group", change)
		self.assertEqual(change["catalog_category"], "parts-barrels")
		self.assertEqual(len(change["issues"]), 1)

	def test_nothing_to_do(self):
		self.assertIsNone(cm.plan_item(self._item(item_group="Barrels", catalog_category="parts-barrels"),
			self._product("parts-barrels")))
		self.assertIsNone(cm.plan_item(self._item(), {"category": None, "legal": None}))
		self.assertIsNone(cm.plan_item(self._item(), self._product("not-mirrored")))

	def test_the_items_key(self):
		self.assertEqual(cm.item_key({"name": "A", "upc": "012345678905"}), "012345678905")
		self.assertEqual(cm.item_key({"name": "012345678905"}), "012345678905")
		self.assertIsNone(cm.item_key({"name": "NO-KEY-HERE"}))


class MasterLegal(_TreeCase):
	"""The master's legal.is_firearm decides — the lane, the classification check and
	promotion agree; the house flag is only the fallback."""

	CODES = ("M3B-TIGHT", "M3B-OLIGHT", "M3B-RCVR")

	def setUp(self):
		super().setUp()
		cm.run_mirror()
		for code in self.CODES:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		catalog_stub.seed_catalog([
			# No house flags it; the owner tightened it to a firearm in the workbench.
			{"rsr_stock_number": "M3B-TIGHT", "upc": "700000000085", "department_number": "34",
			 "short_description": "80 LOWER RECEIVER", "ffl_required": 0,
			 "product_class": "Gun Parts", "dealer_price": 60.0, "map_price": 90.0},
		], distributor="rsr")
		catalog_stub.seed_catalog([
			# SS flags a light (ITYPE); the master's rule says no firearm.
			{"sku": "M3B-OLIGHT", "upc": "700000000092", "department": "31", "ffl_required": 1,
			 "short_description": "Olight Baldr Pro R", "product_class": "Accessory",
			 "cost": 100.0, "map": 140.0},
			{"sku": "M3B-RCVR", "upc": "700000000108", "department": "63", "ffl_required": 1,
			 "short_description": "Aero M4E1 Stripped Lower", "product_class": "Other Firearm",
			 "cost": 90.0, "map": 120.0},
		], distributor="ss")
		self.stub.set_master("700000000085", "firearms-receivers-frames", is_firearm=True)
		self.stub.set_master("700000000092", "accessories-weapon-lights", is_firearm=False)

	def _row(self, house, sku):
		return self.stub.rows(house, [sku])[sku]

	def test_master_firearm_on_an_unflagged_row_is_a_firearm_everywhere(self):
		from ffl_integrations.distributor import api, lanes
		from ffl_integrations.distributor.base import PlanningError
		from ffl_integrations.distributor.util import legal_firearm
		from ffl_integrations.rsr.promote import promote_to_item

		self.assertTrue(legal_firearm(self._row("rsr", "M3B-TIGHT"), "rsr"))
		provider = api.get_provider("rsr")
		line = api.enrich_line(provider, {"sku": "M3B-TIGHT", "qty": 1})
		self.assertTrue(line["legal_firearm"])
		with self.assertRaises(PlanningError):
			provider.assert_lane_fits(lanes.lane(lanes.DIRECT), [line])   # never to the door
		promote_to_item({"rsr_stock_number": "M3B-TIGHT", "item_code": "M3B-TIGHT",
			"has_serial_no": 0})                                           # the dialog's untick
		it = frappe.db.get_value("Item", "M3B-TIGHT", ["is_firearm", "has_serial_no"], as_dict=True)
		self.assertTrue(it.is_firearm)
		self.assertTrue(it.has_serial_no)
		line = api.enrich_line(provider, {"sku": "M3B-TIGHT", "item_code": "M3B-TIGHT", "qty": 1})
		provider.assert_classification([line])                           # classification agrees

	def test_master_non_firearm_on_an_ss_flagged_row_is_not_one(self):
		from ffl_integrations.distributor.util import legal_firearm
		from ffl_integrations.sports_south.promote import promote_to_item

		self.assertFalse(legal_firearm(self._row("ss", "M3B-OLIGHT"), "ss"))
		promote_to_item("M3B-OLIGHT", {"item_code": "M3B-OLIGHT"})
		it = frappe.db.get_value("Item", "M3B-OLIGHT", ["is_firearm", "has_serial_no"], as_dict=True)
		self.assertFalse(it.is_firearm)
		self.assertFalse(it.has_serial_no)

	def test_the_legal_read_is_not_a_product_read(self):
		# Extensions forbid catalog.batch on their pricing paths; the law must still answer.
		from ffl_integrations.distributor import remote_catalog
		from ffl_integrations.distributor.util import legal_firearm

		remote_catalog._memo().clear()
		with mock.patch("ffl_integrations.distributor.catalog.batch",
				side_effect=AssertionError("product read")):
			self.assertFalse(legal_firearm(self._row("ss", "M3B-OLIGHT"), "ss"))  # master, not the SS flag

	def test_service_down_falls_back_to_the_house_flag(self):
		from ffl_integrations.distributor import remote_catalog
		from ffl_integrations.distributor.util import legal_firearm

		remote_catalog._memo().clear()
		with mock.patch("ffl_integrations.distributor.catalog.legal_of",
				side_effect=DataServiceUnavailable("down")):
			self.assertTrue(legal_firearm(self._row("ss", "M3B-OLIGHT"), "ss"))    # SS flag
			self.assertFalse(legal_firearm(self._row("rsr", "M3B-TIGHT"), "rsr"))  # unflagged

	def test_a_receiver_group_item_is_a_serialized_firearm(self):
		# Receiver is a firearm group — the before_save hook enforces it.
		if not frappe.db.exists("Item Group", "Receiver"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Receiver",
				"parent_item_group": "Firearm", "is_group": 0}).insert(ignore_permissions=True)
		doc = frappe.get_doc({"doctype": "Item", "item_code": "M3B-RCVR", "item_name": "LOWER",
			"item_group": "Receiver", "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 0}).insert(ignore_permissions=True)
		self.assertTrue(doc.is_firearm)
		self.assertTrue(doc.has_serial_no)

	def test_a_firearm_never_files_in_a_non_firearm_group(self):
		self.stub.set_master("700000000108", "parts-uppers", is_firearm=True)
		filed = cm.classify(self._row("ss", "M3B-RCVR"), "ss")
		self.assertTrue(filed.is_firearm)
		self.assertEqual(filed.item_group, "Other Firearm")   # the fallback, not Uppers
