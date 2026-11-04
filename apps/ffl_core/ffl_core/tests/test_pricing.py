"""ffl_core.pricing: the register's price for code outside the sale screen, and a price approver's
regular-price change.

Site tests on real Items and Item Prices, one of each shape the counter prices differently: an
ammo box whose Item Price is not its standard_rate, a uniform-priced gun model, the same on sale,
a gun priced one by one with a stray Item Price, a drop-ship listing and the transfer fee. Each
test runs inside a savepoint rolled back after it, so a price one test sets is never the next
one's starting point.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core import pricing
from ffl_core.api.receive_goods import _set_item_selling_price
from ffl_core.item_hooks import _default_selling_price_list
from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM
from ffl_core.tests.test_customer_profile import _user

AMMO = "_T-PRICING-AMMO"
UNIFORM = "_T-PRICING-UNIFORM"
ON_SALE = "_T-PRICING-ON-SALE"
PER_GUN = "_T-PRICING-PER-GUN"
DROPSHIP = "_T-PRICING-DROPSHIP"
MANAGER = "pricing-manager@example.com"
CLERK = "pricing-clerk@example.com"
SAVEPOINT = "ffl_pricing_test"


def _item(code: str, **fields) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1, **fields}).insert(ignore_permissions=True)
	return code


def _item_price(code: str):
	return frappe.db.get_value("Item Price",
		{"item_code": code, "price_list": _default_selling_price_list(), "selling": 1}, "price_list_rate")


def _set_item_price(code: str, rate: float) -> None:
	name = frappe.db.get_value("Item Price", {"item_code": code, "price_list": _default_selling_price_list(), "selling": 1})
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
	else:
		frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": _default_selling_price_list(),
			"selling": 1, "price_list_rate": rate}).insert(ignore_permissions=True)


def _number(shown) -> float:
	return flt(re.sub(r"[^0-9.\-]", "", str(shown)))


def _woo_pushes(enqueue) -> list[str]:
	return [c.kwargs.get("item_code") for c in enqueue.call_args_list
		if c.args and c.args[0] == "ffl_woo_sync.woocommerce.tasks.push_item_task"]


class TestPricing(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_item(AMMO, standard_rate=22.49, woo_product_id="990001")
		_set_item_price(AMMO, 24.99)   # the curated card price, not the standard rate
		_item(UNIFORM, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1, standard_rate=589)
		_item(ON_SALE, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1, standard_rate=500,
			sale_price=450)
		_item(PER_GUN, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=0, standard_rate=0)
		_set_item_price(PER_GUN, 829)  # stray: a per-gun model must never be priced from it
		_item(DROPSHIP, standard_rate=120)
		# The Link would want a Distributor record; the reason only reads the stamp.
		frappe.db.set_value("Item", DROPSHIP, "dropship_distributor", "_T Pricing House", update_modified=False)
		from ffl_core.setup.transfer_fee import ensure

		ensure()
		_user(MANAGER, "Sales Manager")
		_user(CLERK, "Sales User")

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()

	# --- what the register charges -------------------------------------------------------
	def test_the_list_price_is_the_item_price_not_the_standard_rate(self):
		self.assertEqual(pricing.counter_list_price(AMMO), 24.99)
		self.assertEqual(pricing.counter_price_for(AMMO), 24.99)

	def test_a_uniform_model_on_sale_lists_at_the_sale_price(self):
		self.assertEqual(pricing.counter_list_price(UNIFORM), 589)
		self.assertEqual(pricing.counter_list_price(ON_SALE), 450)

	def test_a_per_gun_model_has_no_item_level_price(self):
		self.assertEqual(_item_price(PER_GUN), 829)
		self.assertEqual(pricing.counter_list_price(PER_GUN), 0.0)
		# No gun of the model in stock: nothing to quote from.
		self.assertEqual(pricing.counter_price_for(PER_GUN), 0.0)

	def test_a_per_gun_line_is_priced_from_its_own_gun(self):
		serial = frappe.get_doc({"doctype": "Serial No", "serial_no": "_T-PRICING-SN-1", "item_code": PER_GUN,
			"sell_price": 799}).insert(ignore_permissions=True)
		self.assertEqual(pricing.counter_price_for(PER_GUN, serial.name), 799)
		# A serial that is not one of this model's guns is refused, never priced as one.
		with self.assertRaises(frappe.ValidationError):
			pricing.counter_price_for(PER_GUN, "_T-NO-SUCH-SN")

	def test_unknown_item(self):
		self.assertEqual(pricing.counter_list_price("_T-PRICING-NONE"), 0.0)
		self.assertIsNotNone(pricing.price_readonly_reason("_T-PRICING-NONE"))

	# --- read-only classes --------------------------------------------------------------
	def test_read_only_classes(self):
		for code in (ON_SALE, PER_GUN, DROPSHIP, TRANSFER_FEE_ITEM):
			reason = pricing.price_readonly_reason(code)
			self.assertTrue(reason, code)
			for code_word in ("standard_rate", "Item Price", "dropship", "uniform_sell_price", "FFL Settings"):
				self.assertNotIn(code_word, reason)
		self.assertIsNone(pricing.price_readonly_reason(AMMO))
		self.assertIsNone(pricing.price_readonly_reason(UNIFORM))

	def test_the_configured_transfer_fee_item_is_read_only(self):
		frappe.db.set_single_value("FFL Settings", "transfer_fee_item", AMMO)
		self.assertIsNotNone(pricing.price_readonly_reason(AMMO))

	def test_set_regular_price_refuses_every_read_only_class(self):
		frappe.set_user(MANAGER)
		for code in (ON_SALE, PER_GUN, DROPSHIP, TRANSFER_FEE_ITEM):
			with self.assertRaises(frappe.ValidationError, msg=code):
				pricing.set_regular_price(code, 99)
		self.assertEqual(frappe.db.get_value("Item", ON_SALE, "standard_rate"), 500)

	# --- a manager's change ---------------------------------------------------------------
	def test_a_manager_sets_ammo_price_on_both_records_with_a_trail_and_a_website_push(self):
		frappe.set_user(MANAGER)
		with patch("frappe.enqueue") as enqueue:
			pricing.set_regular_price(AMMO, 23.99)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Item", AMMO, "standard_rate"), 23.99)
		self.assertEqual(_item_price(AMMO), 23.99)
		self.assertEqual(pricing.counter_list_price(AMMO), 23.99)
		self.assertEqual(_woo_pushes(enqueue), [AMMO])
		versions = frappe.get_all("Version", filters={"ref_doctype": "Item", "docname": AMMO}, pluck="data")
		# Version stores the values as displayed ("$ 22.49").
		changed = [c for v in versions for c in json.loads(v).get("changed", []) if c[0] == "standard_rate"]
		self.assertEqual([[_number(c[1]), _number(c[2])] for c in changed], [[22.49, 23.99]])
		self.assertTrue(frappe.get_all("Version", filters={"ref_doctype": "Item", "docname": AMMO, "owner": MANAGER}))

	def test_a_uniform_model_takes_the_new_price_everywhere(self):
		frappe.set_user(MANAGER)
		pricing.set_regular_price(UNIFORM, 579)
		self.assertEqual(frappe.db.get_value("Item", UNIFORM, "standard_rate"), 579)
		self.assertEqual(_item_price(UNIFORM), 579)

	def test_the_same_price_again_saves_nothing(self):
		frappe.set_user(MANAGER)
		pricing.set_regular_price(UNIFORM, 589)
		self.assertFalse(frappe.get_all("Version", filters={"ref_doctype": "Item", "docname": UNIFORM, "owner": MANAGER}))
		self.assertEqual(_item_price(UNIFORM), 589)

	def test_the_price_must_be_above_zero(self):
		frappe.set_user(MANAGER)
		for price in (0, -5):
			with self.assertRaises(frappe.ValidationError):
				pricing.set_regular_price(AMMO, price)
		self.assertEqual(_item_price(AMMO), 24.99)

	def test_a_clerk_cannot_change_a_price(self):
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			pricing.set_regular_price(AMMO, 19.99)
		frappe.set_user("Administrator")
		self.assertEqual(_item_price(AMMO), 24.99)

	# --- Receive Goods keeps its own behaviour on the shared helper ------------------------
	def test_receive_goods_price_write_is_unchanged(self):
		_set_item_selling_price(ON_SALE, 520)
		self.assertEqual(frappe.db.get_value("Item", ON_SALE, "standard_rate"), 520)
		self.assertEqual(_item_price(ON_SALE), 450)   # the running sale still undercuts it
		_set_item_selling_price(AMMO, 30)
		self.assertEqual((frappe.db.get_value("Item", AMMO, "standard_rate"), _item_price(AMMO)), (30, 30))
		_set_item_selling_price(AMMO, 0)
		self.assertEqual((frappe.db.get_value("Item", AMMO, "standard_rate"), _item_price(AMMO)), (0, 30))
		code = _item("_T-PRICING-FRESH")
		_set_item_selling_price(code, 15)
		self.assertEqual(_item_price(code), 15)
