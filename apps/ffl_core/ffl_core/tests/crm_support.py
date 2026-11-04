"""Test support for ffl_core.crm (Frappe CRM on the POS): the CRM settings the setup writes, fixture
Items of each shape the register prices differently, and users. Each test runs inside a savepoint
rolled back after it, so a price or a lead one test makes is never the next one's starting point."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.item_hooks import _default_selling_price_list

AMMO = "_T-CRM-AMMO"
UNIFORM = "_T-CRM-UNIFORM"
PER_GUN = "_T-CRM-PER-GUN"
MANAGER = "crm-manager@example.com"
CLERK = "crm-clerk@example.com"
SAVEPOINT = "ffl_crm_test"


def item_price(code: str):
	return frappe.db.get_value("Item Price",
		{"item_code": code, "price_list": _default_selling_price_list(), "selling": 1}, "price_list_rate")


def set_item_price(code: str, rate: float) -> None:
	pl = _default_selling_price_list()
	name = frappe.db.get_value("Item Price", {"item_code": code, "price_list": pl, "selling": 1})
	if name:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
	else:
		frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": pl, "selling": 1,
			"price_list_rate": rate}).insert(ignore_permissions=True)


def make_item(code: str, **fields):
	if frappe.db.exists("Item", code):
		return frappe.get_doc("Item", code)
	return frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
		"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1, **fields}).insert(ignore_permissions=True)


def user(email: str, *roles) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0}).insert(ignore_permissions=True)
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)
	return email


def product_of(item_code: str) -> str | None:
	return frappe.db.get_value("CRM Product", {"erpnext_item_code": item_code}, "name")


class CRMTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from ffl_core.crm import setup

		setup.ensure_custom_fields()
		setup.ensure_crm_settings()
		make_item(AMMO, standard_rate=22.49, woo_product_id="990101")
		set_item_price(AMMO, 24.99)
		make_item(UNIFORM, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1, standard_rate=589)
		make_item(PER_GUN, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=0, standard_rate=0)
		set_item_price(PER_GUN, 829)   # stray: a per-gun model is never priced from it
		# A manager who may also edit Items, and a counter clerk; CRM's own DocPerms give both
		# write on CRM Product and CRM Deal.
		user(MANAGER, "Sales Manager", "Item Manager")
		user(CLERK, "Sales User")

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()
