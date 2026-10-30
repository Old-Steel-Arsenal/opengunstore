"""pos_pricing_summary: the single data source for the POS card price range,
the add-to-cart rate seed, the picker auto-pick decision and the checkout
gate. Uniform mode collapses the range to standard_rate; un-priced serials
(0 → None) count in n but never in min/max; non-firearm / unknown codes come
back with n=0 so the frontend can cache the negative.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.firearm import pos_pricing_summary


def _ensure_item(code: str, **overrides):
	if frappe.db.exists("Item", code):
		return
	doc = {
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": "All Item Groups", "stock_uom": "Nos",
		"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
	}
	doc.update(overrides)
	frappe.get_doc(doc).insert(ignore_permissions=True)


def _ensure_serial(serial: str, item_code: str, sell_price: float):
	frappe.delete_doc_if_exists("Serial No", serial)
	frappe.get_doc({
		"doctype": "Serial No", "serial_no": serial, "item_code": item_code,
	}).insert(ignore_permissions=True)
	frappe.db.set_value(
		"Serial No", serial,
		{"status": "Active", "sell_price": sell_price}, update_modified=False,
	)


class PosPricingSummary(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		_ensure_item("PPS-NU-GUN", uniform_sell_price=0)
		_ensure_serial("PPS-NU-A", "PPS-NU-GUN", 500)
		_ensure_serial("PPS-NU-B", "PPS-NU-GUN", 900)
		_ensure_serial("PPS-NU-C", "PPS-NU-GUN", 0)        # un-priced
		_ensure_item("PPS-U-GUN", uniform_sell_price=1, standard_rate=750)
		_ensure_serial("PPS-U-A", "PPS-U-GUN", 500)        # tag prices ignored
		_ensure_serial("PPS-U-B", "PPS-U-GUN", 900)
		_ensure_item("PPS-NU-BARE", uniform_sell_price=0)
		_ensure_serial("PPS-BARE-A", "PPS-NU-BARE", 0)     # all un-priced

	def test_non_uniform_range_excludes_unpriced(self):
		s = pos_pricing_summary(["PPS-NU-GUN"])["PPS-NU-GUN"]
		self.assertTrue(s["is_firearm"])
		self.assertEqual(s["uniform"], 0)
		self.assertEqual(s["n"], 3)              # un-priced still counts in n
		self.assertEqual(s["min"], 500)
		self.assertEqual(s["max"], 900)

	def test_all_unpriced_min_max_none(self):
		s = pos_pricing_summary(["PPS-NU-BARE"])["PPS-NU-BARE"]
		self.assertEqual(s["n"], 1)
		self.assertIsNone(s["min"])
		self.assertIsNone(s["max"])

	def test_uniform_collapses_to_standard_rate(self):
		s = pos_pricing_summary(["PPS-U-GUN"])["PPS-U-GUN"]
		self.assertEqual(s["uniform"], 1)
		self.assertEqual(s["min"], 750)
		self.assertEqual(s["max"], 750)

	def test_unknown_code_is_negative_cacheable(self):
		s = pos_pricing_summary(["PPS-NO-SUCH-ITEM"])["PPS-NO-SUCH-ITEM"]
		self.assertFalse(s["is_firearm"])
		self.assertEqual(s["n"], 0)

	def test_accepts_json_string_arg(self):
		out = pos_pricing_summary('["PPS-U-GUN"]')
		self.assertIn("PPS-U-GUN", out)

	def test_accepts_bare_code_string_arg(self):
		# A bare (non-JSON) code, as an MCP agent may pass it, is one code.
		self.assertEqual(pos_pricing_summary("PPS-U-GUN"), pos_pricing_summary(["PPS-U-GUN"]))
