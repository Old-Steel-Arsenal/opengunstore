"""SKUs over GunBroker's 50-character limit.

GunBroker refuses ``POST /Items`` when the SKU is longer than 50 characters, and
the SKU is the key every reader uses to find the gun again (order matching, the
listing sweep, webhooks). The short forms must therefore be minted only when the
full form is too long — a listing already up keeps its SKU — and every reader must
resolve them back to the same gun or Item.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import mapping, orders, webhooks
from ffl_integrations.tests import gunbroker_fixtures as fx

# The real case: 51 characters as ``item_code::serial``.
LONG_CODE = "DAVIDE PEDERSOLI S.TRAPDOOR CARBINE .45-70"
SERIAL = "TD04739"


class TestSerialSku(FrappeTestCase):
	def test_a_sku_within_the_limit_is_unchanged(self):
		self.assertEqual(mapping.listing_sku("COLT-PYTHON", "SN-001"), "COLT-PYTHON::SN-001")
		code = "X" * (mapping.SKU_MAX_LEN - len("::SN-001"))
		self.assertEqual(mapping.listing_sku(code, "SN-001"), f"{code}::SN-001")

	def test_an_over_long_sku_drops_the_item_code(self):
		self.assertEqual(len(f"{LONG_CODE}::{SERIAL}"), 51)
		self.assertEqual(mapping.listing_sku(LONG_CODE, SERIAL), f"::{SERIAL}")

	def test_the_short_form_reads_back_to_the_gun_and_its_item(self):
		with patch("frappe.db.get_value", return_value=LONG_CODE) as get:
			self.assertEqual(orders.split_sku(f"::{SERIAL}"), (LONG_CODE, SERIAL))
		get.assert_called_once_with("Serial No", SERIAL, "item_code")

	def test_an_unknown_serial_gives_no_item_rather_than_a_guess(self):
		with patch("frappe.db.get_value", return_value=None):
			self.assertEqual(orders.split_sku("::NOPE"), ("", "NOPE"))


class TestItemSku(FrappeTestCase):
	CODE = "SOME DISTRIBUTOR AMMO 9MM LUGER 115GR FMJ 50RD BOX - LONG NAME"

	def _group(self) -> str:
		return frappe.db.get_value("Item Group", {"is_group": 0}, "name")

	def _make_item(self, code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code[:140],
			"item_group": self._group(), "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 0, "is_firearm": 0}).insert(ignore_permissions=True)

	def test_a_code_within_the_limit_is_unchanged(self):
		self.assertEqual(mapping.item_listing_sku(" AMMO-9 "), "AMMO-9")

	def test_an_over_long_code_becomes_prefix_and_digest(self):
		sku = mapping.item_listing_sku(self.CODE)
		self.assertEqual(len(sku), mapping.SKU_MAX_LEN)
		self.assertTrue(self.CODE.startswith(sku.split("~")[0]))
		self.assertEqual(sku, mapping.item_listing_sku(self.CODE), "must be stable")

	def test_the_short_form_reads_back_to_the_one_item(self):
		self._make_item(self.CODE)
		# A sibling sharing the prefix (and a LIKE wildcard in it) must not match.
		self._make_item(self.CODE[:45] + "_ OTHER VARIANT OF THE SAME PRODUCT LINE")
		sku = mapping.item_listing_sku(self.CODE)
		self.assertEqual(orders.split_sku(sku), (self.CODE, None))
		self.assertTrue(orders.is_item_level_sku(sku))
		self.assertEqual(webhooks.resolve_listing(sku, ""), ("item", self.CODE))

	def test_a_digest_matching_no_item_is_left_as_it_came(self):
		sku = mapping.item_listing_sku(self.CODE)  # Item never created
		self.assertEqual(orders.split_sku(sku), (sku, None))
		self.assertFalse(orders.is_item_level_sku(sku))


class TestLongSkuOrderIngestion(FrappeTestCase):
	"""A sale on a short-SKU listing lands on the right gun."""

	def _ingest(self):
		order = fx.FakeDoc(name="GB-ORD-00001", gb_order_id="998877",
			order_status="Pending Fulfillment")
		exists = {("Item", LONG_CODE), ("Serial No", SERIAL)}
		with patch("frappe.db.exists", side_effect=lambda dt, name=None: (dt, name) in exists), \
				patch("frappe.db.get_value", return_value=LONG_CODE):
			orders.apply_order_lines(order, fx.order_detail(
				sku=mapping.listing_sku(LONG_CODE, SERIAL), serial=""))
		return order, exists

	def test_the_line_gets_its_item(self):
		order = self._ingest()[0]
		line = order.lines[0]
		self.assertEqual(line.sku, f"::{SERIAL}")
		self.assertEqual(line.item_code, LONG_CODE)

	def test_the_line_matches_its_gun_by_the_sku(self):
		order, exists = self._ingest()
		# serialNumber is blank so only the SKU can name the gun.
		with patch("frappe.db.exists", side_effect=lambda dt, name=None: (dt, name) in exists), \
				patch("frappe.db.get_value", return_value=LONG_CODE):
			serial, why = orders._match_serial(order.lines[0], {})
			self.assertEqual((serial, why), (SERIAL, ""))
			self.assertFalse(orders.is_item_level_line(order.lines[0]))
			self.assertEqual(webhooks.resolve_listing(order.lines[0].sku, ""),
				("serial", SERIAL))
