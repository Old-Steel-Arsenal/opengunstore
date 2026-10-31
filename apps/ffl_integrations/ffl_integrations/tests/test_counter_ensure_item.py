"""Counter drop-ship item-ensure seam (`distributor.api.ensure_dropship_item`).

The unified Create Order search lets staff pick ANY catalog SKU as a drop-ship
line, but `_validate_items` (rightly) refuses a line whose Item does not exist
locally with `dropship_distributor` set. This seam closes that gap: one-stock-source guard →
idempotent promote → stamp — WITHOUT a Distributor Listing row (a counter sale
is not a web listing; Flow B's `create_listing` keeps that half).

Integration-style against the catalog stub + a real Distributor doc, mirroring
test_distributor_dual_source_guard.py's harness.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import api as dist_api
from ffl_integrations.distributor import dropship_hooks
from ffl_integrations.tests import catalog_stub

DIST = "CU-DC"
OTHER_DIST = "CU-DC-OTHER"

# Checksum-valid UPC-A codes — promote appends them as Item barcodes and
# ERPNext's validate_barcode rejects an invalid check digit outright.
ACC_SKU, ACC_UPC = "CU-ACC-1", "860000777711"
GUN_SKU, GUN_UPC = "CU-GUN-1", "860000777728"
ORPHAN_SKU = "CU-ORPHAN-1"  # no UPC → item_code falls back to the stock number
LEGACY_ITEM = "CU-LEGACY-1"  # named by an older stock number, carries ACC_UPC as a barcode


def _plain_group() -> str:
	"""A non-firearm Item Group that exists on any site — same fallback chain
	promote_to_item uses."""
	return "Products" if frappe.db.exists("Item Group", "Products") else "All Item Groups"


def _ensure_distributor(code=DIST):
	if frappe.db.exists("Distributor", code):
		return
	frappe.get_doc({
		"doctype": "Distributor", "code": code, "distributor_name": f"{code} Test DC",
		"provider": "rsr", "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
		"poll_enabled": 0, "safety_buffer": 3, "display_cap": 5,
		"default_markup_pct": 35, "zero_stock_when_feed_stale": 0,
		"override_dropship_defaults": 1,
	}).insert(ignore_permissions=True)


def _remove_test_rows():
	"""An ENABLED Distributor row is live routing config on the shared dev site:
	leaving CU-DC* behind made distributor.api's default-house resolution pick a
	test row over the real one (2026-08-01, same run as the wiring incident).
	Items go first — a stamped dropship_distributor link blocks the delete."""
	for code in (ACC_UPC, ACC_SKU, GUN_UPC, GUN_SKU, ORPHAN_SKU, LEGACY_ITEM):
		if frappe.db.exists("Item", code):
			frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
	for dist in (DIST, OTHER_DIST):
		if frappe.db.exists("Distributor", dist):
			frappe.delete_doc("Distributor", dist, ignore_permissions=True, force=True)
	frappe.db.commit()


class CounterEnsureItemTest(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		_ensure_distributor(DIST)
		_ensure_distributor(OTHER_DIST)
		cls.addClassCleanup(_remove_test_rows)

	def setUp(self):
		for code in (ACC_UPC, ACC_SKU, GUN_UPC, GUN_SKU, ORPHAN_SKU, LEGACY_ITEM):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		frappe.db.commit()
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": ACC_SKU, "upc": ACC_UPC, "department_number": "20",
				"short_description": "COUNTER TEST MAG", "manufacturer": "MAGCO",
				"dealer_price": 10.0, "map_price": 19.99, "quantity": 25},
			{"rsr_stock_number": GUN_SKU, "upc": GUN_UPC, "department_number": "01",
				"ffl_required": 1, "short_description": "COUNTER TEST PISTOL",
				"manufacturer": "GUNCO", "dealer_price": 400.0, "quantity": 4},
			{"rsr_stock_number": ORPHAN_SKU, "upc": "", "department_number": "20",
				"short_description": "COUNTER TEST ORPHAN", "quantity": 3},
		])

	def tearDown(self):
		catalog_stub.uninstall()

	# ------------------------------------------------------------- happy path

	def test_creates_upc_keyed_item_stamped_for_dropship(self):
		out = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(out["item_code"], ACC_UPC)  # identity chain: UPC first
		self.assertFalse(out["is_firearm"])
		self.assertEqual(out["claimed_by"], DIST)
		self.assertEqual(
			frappe.db.get_value("Item", ACC_UPC, "dropship_distributor"), DIST)
		# A counter ensure is NOT a web listing — Flow B owns those.
		self.assertIsNone(dropship_hooks.listing_for(ACC_UPC, DIST))

	def test_firearm_promotes_serialized_and_uniform(self):
		out = dist_api.ensure_dropship_item(GUN_SKU, DIST)
		self.assertEqual(out["item_code"], GUN_UPC)
		got = frappe.db.get_value("Item", GUN_UPC,
			["has_serial_no", "uniform_sell_price", "dropship_distributor"], as_dict=True)
		self.assertTrue(got.has_serial_no)          # dept 01 → serial-tracked
		self.assertTrue(got.uniform_sell_price)     # #4: no local unit to price
		self.assertEqual(got.dropship_distributor, DIST)

	def test_ship_to_store_promotes_without_claiming_the_item(self):
		"""Ordering goods into the shop buys stock; it says nothing about who
		drop-ships the Item on the web, so the stamp stays empty."""
		out = dist_api.ensure_dropship_item(ACC_SKU, DIST, claim=0)
		self.assertEqual(out["item_code"], ACC_UPC)
		self.assertEqual(out["claimed_by"], "")
		self.assertFalse(frappe.db.get_value("Item", ACC_UPC, "dropship_distributor"))
		# and a later drop-ship pick still claims it
		again = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(again["claimed_by"], DIST)
		self.assertEqual(
			frappe.db.get_value("Item", ACC_UPC, "dropship_distributor"), DIST)

	def test_orphan_sku_keeps_stock_number_identity(self):
		out = dist_api.ensure_dropship_item(ORPHAN_SKU, DIST)
		self.assertEqual(out["item_code"], ORPHAN_SKU)

	def test_idempotent_reensure_returns_same_item(self):
		first = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		again = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(first["item_code"], again["item_code"])
		self.assertEqual(frappe.db.count("Item", {"item_code": ACC_UPC}), 1)

	def test_reuses_existing_unstamped_item_and_fills_stamp(self):
		frappe.get_doc({
			"doctype": "Item", "item_code": ACC_UPC, "item_name": "Pre-existing",
			"item_group": _plain_group(), "stock_uom": "Nos", "is_stock_item": 1,
		}).insert(ignore_permissions=True)
		out = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(out["item_code"], ACC_UPC)
		self.assertEqual(
			frappe.db.get_value("Item", ACC_UPC, "dropship_distributor"), DIST)

	# ------------------------------------------------------------------ guards

	def test_locally_stocked_serialized_item_is_refused(self):
		# one-stock-source: the same guard create_listing runs.
		frappe.get_doc({
			"doctype": "Item", "item_code": GUN_UPC, "item_name": "Local gun",
			"item_group": "Rifle", "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 1, "serial_no_series": f"{GUN_UPC}-.####",
			"uniform_sell_price": 0,
		}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			dist_api.ensure_dropship_item(GUN_SKU, DIST)

	def test_item_claimed_by_another_distributor_is_used_as_is(self):
		"""distributor-settings-layout E1: the counter orders from the house picked in
		the Fulfillment plan (the line carries it), so an Item another house already
		sources is handed back untouched — never refused, never re-homed. The claim
		drives the web listing, web-order routing and stock publishing."""
		frappe.get_doc({
			"doctype": "Item", "item_code": ACC_UPC, "item_name": "Foreign-sourced",
			"item_group": _plain_group(), "stock_uom": "Nos", "is_stock_item": 1,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", ACC_UPC, "dropship_distributor", OTHER_DIST,
			update_modified=False)
		out = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(out["item_code"], ACC_UPC)
		self.assertEqual(out["claimed_by"], OTHER_DIST)
		self.assertFalse(out["is_firearm"])
		self.assertEqual(
			frappe.db.get_value("Item", ACC_UPC, "dropship_distributor"), OTHER_DIST,
			"the other house's claim must survive")
		self.assertFalse(frappe.db.exists("Item", ACC_SKU),
			"no second Item promoted under the stock number")

	def test_barcode_owner_claimed_by_another_distributor_is_not_rehomed(self):
		"""No Item is named by the UPC, but an older one carries it as a barcode and
		another house drop-ships it. The promoter resolves the row to that Item, so
		the ownership check must too — its stamp must survive."""
		frappe.get_doc({
			"doctype": "Item", "item_code": LEGACY_ITEM, "item_name": "Legacy-named",
			"item_group": _plain_group(), "stock_uom": "Nos", "is_stock_item": 1,
			"barcodes": [{"barcode": ACC_UPC}],
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", LEGACY_ITEM, "dropship_distributor", OTHER_DIST,
			update_modified=False)
		out = dist_api.ensure_dropship_item(ACC_SKU, DIST)
		self.assertEqual(out["item_code"], LEGACY_ITEM)
		self.assertEqual(out["claimed_by"], OTHER_DIST)
		self.assertEqual(
			frappe.db.get_value("Item", LEGACY_ITEM, "dropship_distributor"), OTHER_DIST,
			"the other house's claim must survive")
		self.assertFalse(frappe.db.exists("Item", ACC_UPC))

	def test_unknown_sku_throws(self):
		with self.assertRaises(frappe.ValidationError):
			dist_api.ensure_dropship_item("CU-NOPE-404", DIST)

	# ------------------------------------------------------- search provenance

	def test_search_rows_carry_the_distributor_tag(self):
		rows = dist_api.search("COUNTER TEST", limit=10, distributor=DIST)
		self.assertTrue(rows)
		self.assertTrue(all(r.get("distributor") == DIST for r in rows))

	# --------------------------------------------------- quote after an ensure

	def test_quote_by_item_code_resolves_ensured_item_via_upc(self):
		"""The submit-path eligibility (`manual_order._dropship_eligibility`) re-quotes by
		item_code. An ensured Item has NO listing row and a UPC item_code the
		sku-keyed catalog can't find — the UPC fallback must resolve it, or every
		counter drop-ship of a never-web-listed SKU dies at submit as fake OOS."""
		dist_api.ensure_dropship_item(ACC_SKU, DIST)
		q = dist_api.quote(item_code=ACC_UPC, distributor=DIST)
		self.assertTrue(q["found"])
		self.assertEqual(q["sku"], ACC_SKU)   # re-keyed to the DISTRIBUTOR sku
		self.assertEqual(q["qty"], 25)

	def test_quote_by_item_code_still_finds_orphans_directly(self):
		dist_api.ensure_dropship_item(ORPHAN_SKU, DIST)
		q = dist_api.quote(item_code=ORPHAN_SKU, distributor=DIST)
		self.assertTrue(q["found"])
		self.assertEqual(q["sku"], ORPHAN_SKU)
