"""Receive-time Item creation from the catalog: a scanned
barcode no Item carries is looked up across the enabled houses by UPC."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.front_end_invariants import block, code_only, read
from ffl_integrations.distributor import api as dist_api
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests.test_distributor_integration import _Base

UPC = "0123456789012"


class CatalogByUpc(_Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		catalog_stub.seed_catalog([{
			"rsr_stock_number": "DC-NEW", "upc": UPC, "ffl_required": 0, "department_number": "13",
			"dealer_price": 7.5, "map_price": 9, "msrp": 12, "quantity": 4,
			"manufacturer": "TestArms", "model": "Widget", "short_description": "Test widget"}])

	def test_a_listed_upc_comes_back_tagged_with_its_house(self):
		rows = dist_api.catalog_by_upc(UPC)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["distributor"], "TEST-DC")
		self.assertEqual(rows[0]["sku"], "DC-NEW")
		self.assertEqual(rows[0]["upc"], UPC)
		self.assertEqual(rows[0]["cost"], 7.5)

	def test_the_row_carries_the_product_title(self):
		# Without it the promote dialog defaults the Item name to the manufacturer.
		self.assertEqual(dist_api.catalog_by_upc(UPC)[0]["item_name"], "Test widget")

	def test_an_unlisted_or_blank_upc_is_empty(self):
		self.assertEqual(dist_api.catalog_by_upc("9999999999999"), [])
		self.assertEqual(dist_api.catalog_by_upc(""), [])


class PickerFallsBackToTheCatalog(FrappeTestCase):
	def test_unknown_upc_scan_asks_the_catalog_then_opens_promote(self):
		src = code_only(read("ffl_core", "public", "js", "ffl_core.bundle.js"))
		scan = block(src, "_scan() {", "\t")
		self.assertIn("ffl_integrations.distributor.api.catalog_by_upc", scan)
		self.assertLess(scan.index("catalog_by_upc"), scan.index("_openPromote"))

	def test_promote_dialog_defaults_the_item_code_to_the_upc(self):
		# Same identity chain as the server promoters: UPC first, SKU for orphans.
		src = code_only(read("ffl_core", "public", "js", "ffl_core.bundle.js"))
		promote = block(src, "_openPromote(row) {", "\t")
		self.assertIn("fieldname: 'item_code', fieldtype: 'Data', reqd: 1, default: row.upc || row.sku", promote)
