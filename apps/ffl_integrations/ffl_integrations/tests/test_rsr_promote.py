"""Promote-from-RSR tests."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.rsr.promote import (
	promote_to_item,
	backfill_from_rsr,
	lookup_for_item_form,
	migrate_legacy_item_groups,
	_ammo_subcategory,
	_extract_caliber,
)
from ffl_integrations.tests import catalog_stub


class RSRPromote(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		from ffl_integrations.setup import rsr_seed
		rsr_seed.run()
		cls.stock = "PROMOTE-TEST-1"
		cls.stock_msrp_only = "PROMOTE-TEST-MSRP"
		cls.stock_used = "PROMOTE-TEST-USED"

	def setUp(self):
		# Wipe any Item from a prior test method: FrappeTestCase rolls the whole
		# CLASS back once at tearDownClass (docs/testing.md), so Items created by
		# an earlier test in this run persist until then unless swept here.
		for code in ("PROMOTE-TEST-1", "OVERRIDE-TEST-1", "PROMOTE-TEST-MSRP",
				"PROMOTE-TEST-USED", "PROMOTE-USED-HG", "PROMOTE-AMMO-9MM",
				"764503037085", "NOUPC1", "CUSTOM"):
			if frappe.db.exists("Item", code):
				try:
					frappe.delete_doc("Item", code, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

		# Remote catalog stub — replaces inserting real "RSR Catalog Item" docs.
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": self.stock, "upc": "012345678905",
			 "manufacturer": "TEST MFG", "model": "T-1", "mfg_number": "MFG-PART-123",
			 "short_description": "Test Pistol", "department_number": "01",
			 "msrp": 599.99, "map_price": 549.99, "dealer_price": 399.99,
			 "image_url": "https://img.rsrgroup.com/pimages/PROMOTE-TEST-1_1.jpg",
			 "ffl_required": 1},
			# MSRP-only (MAP=0) — verifies fallback to MSRP for selling price
			{"rsr_stock_number": self.stock_msrp_only, "upc": "012345678912",
			 "manufacturer": "TEST MFG", "model": "T-2", "short_description": "Test Ammo",
			 "department_number": "18", "msrp": 14.99, "map_price": 0,
			 "dealer_price": 9.99, "ffl_required": 0},
			# A USED firearm department (02 = Used Handguns). Every firearm — used
			# included — must be serial-tracked.
			{"rsr_stock_number": self.stock_used, "upc": "012345678929",
			 "manufacturer": "TEST MFG", "model": "T-USED",
			 "short_description": "Used Test Handgun", "department_number": "02",
			 "msrp": 399.99, "map_price": 0, "dealer_price": 250.0, "ffl_required": 1},
		])

	def tearDown(self):
		catalog_stub.uninstall()

	# Note: all "legacy" tests below pin `"item_code": <stock>` explicitly so
	# their item_code stays the mnemonic RSR stock number (exercising the
	# "explicit payload item_code always wins" branch) — that keeps every
	# pre-existing assertion (item_group / mpn / gallery / dealer price / …)
	# valid without rewriting them around the new UPC-keyed default. The new
	# default (item_code = UPC when no override, else stock number for
	# orphans) is covered on its own by the three tests right below.

	def test_promote_keys_item_by_upc(self):
		"""Default (no item_code override) + a real UPC → item_code = UPC. Also
		asserts last_rate (dealer_price) to exercise the barcode-fallback
		`_rsr_for_item` path end to end: since item_code is now the UPC, not
		the sku, `_item_response`'s dealer-price lookup misses the direct
		sku match and must resolve via the Item Barcode → remote_catalog.
		item_by_upc round trip — a bare item_code assertion wouldn't prove
		that branch actually returns real data."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "GLK19", "upc": "764503037085",
			"department_number": "01", "ffl_required": 1, "msrp": 599.0,
			"dealer_price": 399.0, "short_description": "GLOCK 19 GEN5",
			"manufacturer": "GLOCK"}])
		out = promote_to_item({"rsr_stock_number": "GLK19"})
		self.assertEqual(out["item_code"], "764503037085")
		self.assertEqual(out["last_rate"], 399.0)

	def test_promote_default_path_is_idempotent(self):
		"""Re-promoting with NO item_code override (the UPC-keyed default path,
		as opposed to test_promote_is_idempotent's explicit-override path) must
		return the same item_code both times — not a second Item, not a crash
		on re-appending the now-duplicate UPC barcode."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "GLK19", "upc": "764503037085",
			"department_number": "01", "ffl_required": 1, "msrp": 599.0,
			"short_description": "GLOCK 19 GEN5", "manufacturer": "GLOCK"}])
		first = promote_to_item({"rsr_stock_number": "GLK19"})
		second = promote_to_item({"rsr_stock_number": "GLK19"})
		self.assertEqual(first["item_code"], "764503037085")
		self.assertEqual(second["item_code"], "764503037085")

	def test_promote_orphan_falls_back_to_stock_number(self):
		"""No UPC on the catalog row → item_code falls back to the stock #."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "NOUPC1", "upc": "",
			"department_number": "10", "short_description": "MAG"}])
		out = promote_to_item({"rsr_stock_number": "NOUPC1"})
		self.assertEqual(out["item_code"], "NOUPC1")

	def test_promote_explicit_item_code_still_wins(self):
		"""A payload item_code always wins, even when the row has a UPC."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "GLK17", "upc": "764503026966",
			"department_number": "01", "ffl_required": 1, "msrp": 549.0,
			"short_description": "GLOCK 17 GEN5", "manufacturer": "GLOCK"}])
		out = promote_to_item({"rsr_stock_number": "GLK17", "item_code": "CUSTOM"})
		self.assertEqual(out["item_code"], "CUSTOM")

	def test_promote_finds_the_item_already_carrying_the_upc(self):
		"""An Item named otherwise (an older stock number) that carries the row's
		UPC is that product: re-promoting returns it rather than failing on the
		duplicate barcode."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "GLK17", "upc": "764503026966",
			"department_number": "01", "ffl_required": 1, "msrp": 549.0,
			"short_description": "GLOCK 17 GEN5", "manufacturer": "GLOCK"}])
		promote_to_item({"rsr_stock_number": "GLK17", "item_code": "CUSTOM"})
		out = promote_to_item({"rsr_stock_number": "GLK17"})
		self.assertEqual(out["item_code"], "CUSTOM")
		self.assertFalse(frappe.db.exists("Item", "764503026966"))

	def test_promote_creates_item_with_mapped_group(self):
		r = promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		self.assertTrue(r["found"])
		self.assertEqual(r["item_code"], "PROMOTE-TEST-1")
		# Department 01 → Handguns → "Pistol" leaf group (Handgun split, 2026-07-21)
		self.assertEqual(r["item_group"], "Pistol")
		# Has serial no defaulted on for handguns (dept 01)
		self.assertTrue(r["has_serial_no"])

	def test_promote_used_firearm_dept_is_serialized(self):
		# Used firearm departments (02/03) must still default to serial-tracked —
		# the store tracks EVERY gun by Serial No.
		r = promote_to_item({"rsr_stock_number": self.stock_used, "item_code": self.stock_used})
		self.assertTrue(r["found"])
		self.assertTrue(r["has_serial_no"], "used firearm must be serial-tracked")
		self.assertTrue(frappe.db.get_value("Item", "PROMOTE-TEST-USED", "is_used"))

	def test_promote_writes_upc_to_item_barcode(self):
		promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		bc = frappe.db.get_value(
			"Item Barcode", {"parent": "PROMOTE-TEST-1", "barcode": "012345678905"}, "name")
		self.assertTrue(bc)

	def test_promote_writes_mpn_and_upc_to_item(self):
		"""Promote must persist the manufacturer part number (RSR mfg_number) to
		Item.mpn and the UPC to Item.upc, so the FastBound push sends SKU/UPC/MPN."""
		promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		item = frappe.db.get_value("Item", "PROMOTE-TEST-1", ["upc", "mpn"], as_dict=True)
		self.assertEqual(item.upc, "012345678905")
		self.assertEqual(item.mpn, "MFG-PART-123")

	def test_promote_is_idempotent(self):
		first = promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		second = promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		self.assertEqual(first["item_code"], second["item_code"])

	def test_promote_with_overrides(self):
		# item_group override is honoured; a has_serial_no=0 override on a
		# firearm group is NOT — every firearm is serial-tracked and the Item
		# hook forces the flag (the 2026-09-16 NFA suppressor was created
		# exactly this way from the promote dialog and was unsellable).
		r = promote_to_item({
			"rsr_stock_number": self.stock,
			"item_code": "OVERRIDE-TEST-1",
			"item_group": "Revolver",
			"has_serial_no": 0,
		})
		self.assertEqual(r["item_code"], "OVERRIDE-TEST-1")
		self.assertEqual(r["item_group"], "Revolver")
		self.assertTrue(r["has_serial_no"])

	def test_selling_price_prefers_map_when_set(self):
		"""Item.standard_rate (selling-side default) should = MAP when MAP > 0."""
		promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		standard_rate = frappe.db.get_value("Item", "PROMOTE-TEST-1", "standard_rate")
		# MAP=549.99 (preferred over MSRP=599.99)
		self.assertEqual(float(standard_rate or 0), 549.99)

	def test_selling_price_falls_back_to_msrp_when_map_is_zero(self):
		"""When MAP=0, Item.standard_rate falls back to MSRP."""
		promote_to_item({"rsr_stock_number": self.stock_msrp_only, "item_code": self.stock_msrp_only})
		standard_rate = frappe.db.get_value("Item", "PROMOTE-TEST-MSRP", "standard_rate")
		self.assertEqual(float(standard_rate or 0), 14.99)

	def test_promote_response_last_rate_is_dealer_price(self):
		"""last_rate (cost prefill for receive line) must be RSR dealer_price, NOT MSRP/MAP."""
		r = promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		# dealer_price=399.99 — never the selling price (MAP 549.99 / MSRP 599.99)
		self.assertEqual(r["last_rate"], 399.99)

	def test_lookup_item_returns_dealer_price_for_rsr_linked_item(self):
		"""ffl_core.api.receive_goods.lookup_item should return RSR dealer_price
		as the cost prefill when the Item has no purchase history yet."""
		from ffl_core.api.receive_goods import lookup_item
		promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		# Sanity: standard_rate is the selling default, not the cost
		standard_rate = frappe.db.get_value("Item", "PROMOTE-TEST-1", "standard_rate")
		self.assertEqual(float(standard_rate or 0), 549.99)
		# Lookup by item_code: cost prefill should be dealer_price, NOT standard_rate
		r = lookup_item("PROMOTE-TEST-1")
		self.assertTrue(r["found"])
		self.assertEqual(r["last_rate"], 399.99)
		# Lookup by UPC: same answer (joins through Item Barcode)
		r2 = lookup_item("012345678905")
		self.assertTrue(r2["found"])
		self.assertEqual(r2["last_rate"], 399.99)

	def test_promote_writes_firearm_details_and_image_gallery(self):
		"""Promote must populate manufacturer/model_name/firearm_type/dealer_price
		custom fields and seed image_gallery with one primary row."""
		promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		item = frappe.get_doc("Item", "PROMOTE-TEST-1")
		# Brand auto-created; manufacturer custom field = same Brand name
		self.assertEqual(item.brand, "TEST MFG")
		self.assertEqual(item.manufacturer, "TEST MFG")
		self.assertEqual(item.model_name, "T-1")
		# Dept 01 → ATF Form 4473 type "Pistol" (default; user can re-flag to Revolver)
		self.assertEqual(item.firearm_type, "Pistol")
		# dealer_price (custom field) ≠ standard_rate (selling)
		self.assertEqual(float(item.dealer_price or 0), 399.99)
		self.assertEqual(item.listing_status, "Draft")
		# Image gallery seeded with RSR _1/_2/_3 variants; only _1 is primary.
		# The CDN 404s missing variants and the Item form's onerror hides them,
		# so seeding speculatively is cheap and avoids HEAD-checking on import.
		gallery_by_idx = {row.sort_order: row for row in item.image_gallery}
		self.assertEqual(sorted(gallery_by_idx), [1, 2, 3])
		self.assertTrue(gallery_by_idx[1].image.endswith("_1.jpg"))
		self.assertTrue(gallery_by_idx[2].image.endswith("_2.jpg"))
		self.assertTrue(gallery_by_idx[3].image.endswith("_3.jpg"))
		self.assertEqual(gallery_by_idx[1].is_primary, 1)
		self.assertEqual(gallery_by_idx[2].is_primary, 0)

	def test_promote_non_firearm_dept_leaves_firearm_type_blank(self):
		"""Dept 18 (Ammunition) is not a firearm — firearm_type stays empty."""
		promote_to_item({"rsr_stock_number": self.stock_msrp_only, "item_code": self.stock_msrp_only})
		item = frappe.get_doc("Item", "PROMOTE-TEST-MSRP")
		self.assertFalse(item.firearm_type)
		# But cost/selling prices still flow through correctly
		self.assertEqual(float(item.dealer_price or 0), 9.99)
		self.assertEqual(float(item.standard_rate or 0), 14.99)

	def test_backfill_fills_empty_fields_only(self):
		"""backfill_from_rsr should fill empty fields but NOT overwrite user edits."""
		# Manually create an Item that's RSR-linked by item_code but missing details
		stock = self.stock
		if frappe.db.exists("Item", stock):
			frappe.delete_doc("Item", stock, ignore_permissions=True)
		# Insert minimal Item — most firearm fields empty, but model_name USER-SET
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": stock,
			"item_name": "User Edited Name",
			"item_group": "Pistol",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"model_name": "USER-EDIT-MODEL",  # already set — backfill must NOT touch this
		})
		doc.flags.ignore_permissions = True
		doc.insert()

		stats = backfill_from_rsr([stock])
		self.assertEqual(stats["updated"], 1)
		item = frappe.get_doc("Item", stock)
		# Empty fields filled in
		self.assertEqual(item.brand, "TEST MFG")
		self.assertEqual(item.manufacturer, "TEST MFG")
		self.assertEqual(item.firearm_type, "Pistol")
		self.assertEqual(float(item.dealer_price or 0), 399.99)
		# User-set field preserved
		self.assertEqual(item.model_name, "USER-EDIT-MODEL")

	def test_lookup_for_item_form_by_stock_number(self):
		"""lookup_for_item_form should return defaults dict + rsr metadata."""
		r = lookup_for_item_form(self.stock)
		self.assertTrue(r["found"])
		# UPC-first default, matching promote_to_item's own naming (Task 4): the
		# form's prefill and the real promote insert must never disagree on what
		# an Item will be named for the exact same catalog row.
		self.assertEqual(r["defaults"]["item_code"], "012345678905")
		self.assertEqual(r["defaults"]["manufacturer"], "TEST MFG")
		self.assertEqual(r["defaults"]["firearm_type"], "Pistol")
		# Selling default = MAP (RSR-enforced floor) since MAP > 0 here
		self.assertEqual(r["defaults"]["standard_rate"], 549.99)
		# Cost = dealer_price (custom field on Item)
		self.assertEqual(r["defaults"]["dealer_price"], 399.99)
		# UPC available for client to push into barcodes child table
		self.assertEqual(r["defaults"]["upc"], "012345678905")
		self.assertEqual(r["rsr"]["is_firearm"], True)

	def test_lookup_for_item_form_by_upc(self):
		"""UPC lookup should return the same defaults as stock-number lookup."""
		r = lookup_for_item_form("012345678905")
		self.assertTrue(r["found"])
		self.assertEqual(r["rsr"]["rsr_stock_number"], self.stock)
		self.assertEqual(r["defaults"]["item_code"], "012345678905")

	def test_lookup_for_item_form_orphan_falls_back_to_stock_number(self):
		"""No UPC on the row → defaults.item_code falls back to the stock number,
		matching promote_to_item's own orphan fallback (Task 4)."""
		catalog_stub.seed_catalog([{"rsr_stock_number": "LOOKUP-ORPHAN-1", "upc": "",
			"department_number": "10", "short_description": "MAG"}])
		r = lookup_for_item_form("LOOKUP-ORPHAN-1")
		self.assertTrue(r["found"])
		self.assertEqual(r["defaults"]["item_code"], "LOOKUP-ORPHAN-1")

	def test_lookup_for_item_form_miss(self):
		r = lookup_for_item_form("DOES-NOT-EXIST-IN-CATALOG")
		self.assertFalse(r["found"])

	def test_lookup_for_item_form_used_firearm_prefills_serial_tracked(self):
		"""The Item-form prefill follows promote's rule: EVERY firearm is
		serial-tracked, used departments (02/03) included."""
		r = lookup_for_item_form(self.stock_used)
		self.assertTrue(r["found"])
		self.assertEqual(r["defaults"]["has_serial_no"], 1)

	def test_seed_distributor_row_skips_when_provider_row_exists_under_another_name(self):
		"""The shared Distributor seed (RSR / SS / Lipsey's) is create-once by name
		OR provider: a house row the operator re-created under another code must not
		get a second disabled twin on the next migrate."""
		from ffl_integrations.distributor.policy import ensure_distributor_row

		# setUp commits, so these rows would outlive the class rollback: sweep them
		# before (a crashed earlier run) and after.
		def sweep():
			for code in ("SEED-TEST-RENAMED", "SEED-TEST", "SEED-TEST-2"):
				if frappe.db.exists("Distributor", code):
					frappe.delete_doc("Distributor", code, ignore_permissions=True, force=True)
			frappe.db.commit()
		sweep()
		self.addCleanup(sweep)
		provider = "seed-test-house"
		frappe.get_doc({"doctype": "Distributor", "code": "SEED-TEST-RENAMED",
			"distributor_name": "Seed Test House", "provider": provider, "enabled": 0,
			"stocking_enabled": 1, "dropship_enabled": 1, "poll_enabled": 0,
			"safety_buffer": 3, "display_cap": 5}).insert(ignore_permissions=True)
		self.assertFalse(ensure_distributor_row("SEED-TEST", "Seed Test, Inc.", provider))
		self.assertFalse(frappe.db.exists("Distributor", "SEED-TEST"))

		self.assertTrue(ensure_distributor_row("SEED-TEST-2", "Seed Test 2, Inc.",
			"seed-test-house-2", poll=1, priority=7))
		row = frappe.db.get_value("Distributor", "SEED-TEST-2",
			["enabled", "poll_enabled", "priority", "safety_buffer", "display_cap"], as_dict=True)
		self.assertEqual((row.enabled, row.poll_enabled, row.priority, row.safety_buffer,
			row.display_cap), (0, 1, 7, 3, 5))
		self.assertFalse(ensure_distributor_row("SEED-TEST-2", "Seed Test 2, Inc.",
			"seed-test-house-2"))

	def test_promote_handgun_lands_in_pistol_group_not_legacy(self):
		"""RSR dept 01 (Handguns) → Item.item_group must be the "Pistol" leaf
		(Handgun split into Pistol/Revolver, 2026-07-21), not legacy "New Firearm"."""
		r = promote_to_item({"rsr_stock_number": self.stock, "item_code": self.stock})
		self.assertEqual(r["item_group"], "Pistol")
		# is_used=0 because dept 01 is not a Used dept (02/03)
		self.assertEqual(int(frappe.db.get_value("Item", "PROMOTE-TEST-1", "is_used")), 0)

	def test_rsr_seed_handgun_depts_target_pistol_leaf(self):
		"""Category recurrence-source #2: dept 01/02 default_item_group must be the
		"Pistol" leaf, not the dead "Handgun" — else RSR promote re-creates the
		empty Woo "Handgun" category after cleanup (sync._apply_category)."""
		for dept in ("01", "02"):
			with self.subTest(dept=dept):
				self.assertEqual(
					frappe.db.get_value("RSR Department", dept, "default_item_group"),
					"Pistol",
				)

	def test_promote_used_dept_marks_is_used(self):
		"""RSR dept 02 (Used Handguns) → is_used=1 and item_group='Pistol'."""
		stock = "PROMOTE-USED-HG"
		if frappe.db.exists("Item", stock):
			frappe.delete_doc("Item", stock, ignore_permissions=True)
		catalog_stub.seed_catalog([{
			"rsr_stock_number": stock, "upc": "012345678929",
			"manufacturer": "TEST USED", "model": "USED-1",
			"short_description": "Used Handgun Test", "department_number": "02",
			"msrp": 400.0, "dealer_price": 300.0, "ffl_required": 1,
		}])
		r = promote_to_item({"rsr_stock_number": stock, "item_code": stock})
		self.assertEqual(r["item_group"], "Pistol")
		self.assertEqual(int(frappe.db.get_value("Item", stock, "is_used")), 1)
		# Cleanup
		frappe.delete_doc("Item", stock, ignore_permissions=True)

	def test_promote_ammo_routes_to_subcategory(self):
		"""Dept 18 with Pistol caliber description → Pistol Ammo leaf group."""
		stock = "PROMOTE-AMMO-9MM"
		if frappe.db.exists("Item", stock):
			frappe.delete_doc("Item", stock, ignore_permissions=True)
		catalog_stub.seed_catalog([{
			"rsr_stock_number": stock, "upc": "012345678936",
			"manufacturer": "WINCHESTER", "model": "USA",
			"short_description": "WIN USA 9MM 115GR FMJ 50/BX", "department_number": "18",
			"msrp": 14.99, "dealer_price": 9.99, "ffl_required": 0,
		}])
		r = promote_to_item({"rsr_stock_number": stock, "item_code": stock})
		self.assertEqual(r["item_group"], "Pistol Ammo")
		# Cleanup
		frappe.delete_doc("Item", stock, ignore_permissions=True)

	def test_ammo_classifier_unit(self):
		"""Direct unit test of the ammo regex classifier — covers the four
		buckets plus edge cases."""
		def cls(desc, dept="18", model=""):
			return _ammo_subcategory({
				"department_number": dept, "model": model,
				"short_description": desc, "long_description": "",
			})

		# Pistol
		self.assertEqual(cls("WIN USA 9MM 115GR FMJ 50/BX"), "Pistol Ammo")
		self.assertEqual(cls("SPR GOLD DOT 32ACP 60GR HP"), "Pistol Ammo")
		self.assertEqual(cls("FED PRM 454CAS 250GR"), "Pistol Ammo")
		self.assertEqual(cls("PMC BRNZ 38SPL 132GR FMJ"), "Pistol Ammo")
		# Rifle
		self.assertEqual(cls("FED 223REM 55GR FMJ 500/1000"), "Rifle Ammo")
		self.assertEqual(cls("MAGTECH M193 556NATO 55GR FMJ"), "Rifle Ammo")
		self.assertEqual(cls("HRNDY SF 22 ARC 70GR CX"), "Rifle Ammo")
		self.assertEqual(cls("REM CORE-LOKT 6.5PRC 140GR"), "Rifle Ammo")
		# Shotgun
		self.assertEqual(cls("REM HYPSNC STL 12GA 3\" #4"), "Shotgun Ammo")
		self.assertEqual(cls("WIN SPRX 410 2.5\" 000BK"), "Shotgun Ammo")
		# Rimfire
		self.assertEqual(cls("AGUILA 22 LR HV 40GR"), "Rimfire Ammo")
		self.assertEqual(cls("CCI MINI MAG 22LR 40GR CPRN"), "Rimfire Ammo")
		self.assertEqual(cls("HORNADY 17 HMR 17GR V-MAX"), "Rimfire Ammo")
		# Non-dept-18 returns None even with caliber-like text
		self.assertIsNone(cls("9MM Glock", dept="01"))
		# Empty / unmatched
		self.assertIsNone(cls("Mystery Cartridge"))

	def test_extract_caliber_for_firearms(self):
		"""Caliber extraction must pull a real caliber from RSR firearm
		descriptions and must NOT return generic category words ("RIFLE",
		"PISTOL") that the ammo regex includes for sub-category routing."""
		# Pistols
		self.assertEqual(_extract_caliber('RUGER SEC-9 9MM 4" CBLT KIN BL 15RD'), "9MM")
		self.assertEqual(_extract_caliber('GLOCK 19 GEN5 9mm Luger 4.02"'), "9MM")
		# Shotgun
		self.assertEqual(_extract_caliber('Remington 870 12GA pump 28"'), "12GA")
		self.assertEqual(_extract_caliber('Henry Lever 410 20" 5RD'), "410")
		# Rimfire
		self.assertEqual(_extract_caliber('Ruger 10/22 Carbine 22 LR'), "22 LR")
		# Rifle calibers
		self.assertEqual(_extract_caliber('Springfield M1A 308 WIN 22" Black'), "308 WIN")
		self.assertEqual(_extract_caliber('AM DEF UIC10A 308WIN 16"'), "308WIN")
		# Generic words must NOT be returned (these come from the regex's
		# original ammo-routing role).
		self.assertNotIn(_extract_caliber('AM DEF ADM4 RIFLE MLOK 16" BLK 5.56'),
			{"RIFLE", "PISTOL", "HANDGUN"})
		# Empty / no caliber hint
		self.assertIsNone(_extract_caliber("STRIPPED LOWER BLK"))
		self.assertIsNone(_extract_caliber(""))
		self.assertIsNone(_extract_caliber(None))


class TestGalleryVariantsSkipDeadCdnUrls(FrappeTestCase):
	"""Woo sideloads every image server-side on product create — one 404
	variant 400s the whole push (first hit: 2026-08-01 dev E2E). Promote must
	only seed gallery rows the CDN actually serves."""

	def setUp(self):
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([{
			"rsr_stock_number": "IMGSKU1", "upc": "111111111117",
			"department_number": "14", "short_description": "IMG TEST HOLSTER",
			"manufacturer": "1791 Gunleather", "map_price": 48.99,
			"image_url": "https://img.rsrgroup.com/pimages/IMGSKU1_1.jpg",
			"image_url_hires": "https://img.rsrgroup.com/highres-pimages/IMGSKU1_1_HR.jpg",
		}])

	def tearDown(self):
		catalog_stub.uninstall()
		frappe.db.rollback()

	def test_dead_variant_rows_are_not_seeded(self):
		from unittest.mock import patch
		with patch("ffl_integrations.rsr.promote._cdn_url_exists",
				side_effect=lambda u: not u.endswith("_3.jpg")):
			out = promote_to_item({"rsr_stock_number": "IMGSKU1"})
		doc = frappe.get_doc("Item", out["item_code"])
		urls = [r.image for r in (doc.get("image_gallery") or [])]
		self.assertTrue(urls, "live variants should still be seeded")
		self.assertTrue(all(not u.endswith("_3.jpg") for u in urls))
		self.assertEqual(doc.image, "https://img.rsrgroup.com/pimages/IMGSKU1_1.jpg")
		self.assertEqual(out["item_code"], "111111111117")

	def test_dead_main_image_leaves_item_image_unset(self):
		from unittest.mock import patch
		with patch("ffl_integrations.rsr.promote._cdn_url_exists", return_value=False):
			out = promote_to_item({"rsr_stock_number": "IMGSKU1"})
		doc = frappe.get_doc("Item", out["item_code"])
		self.assertFalse(doc.image)
		self.assertFalse(doc.get("image_gallery"))
