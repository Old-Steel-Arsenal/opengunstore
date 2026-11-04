"""CRM Product → Item: only a manager changes a price or switches a product off, read-only classes
stay read-only, the Item is the master for code / rename / delete, and a description edit never
touches the price."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core import pricing
from ffl_core.tests.crm_support import AMMO, CLERK, MANAGER, PER_GUN, UNIFORM, CRMTestCase, item_price, product_of, user


def _edit(item_code: str, as_user: str = "Administrator", **values):
	frappe.set_user(as_user)
	try:
		doc = frappe.get_doc("CRM Product", product_of(item_code))
		doc.update(values)
		doc.save()
		return doc
	finally:
		frappe.set_user("Administrator")


class TestProductEdits(CRMTestCase):
	def test_products_are_not_made_in_crm(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "CRM Product", "product_code": "_T-CRM-HANDMADE", "standard_rate": 5}).insert()

	def test_a_description_edit_leaves_the_price_alone(self):
		before = (frappe.db.get_value("Item", AMMO, "standard_rate"), item_price(AMMO))
		with patch.object(pricing, "set_regular_price", wraps=pricing.set_regular_price) as price_push:
			_edit(AMMO, description="<p>50 rounds, brass case</p>")
		price_push.assert_not_called()
		self.assertEqual(frappe.db.get_value("Item", AMMO, "description"), "<p>50 rounds, brass case</p>")
		self.assertEqual((frappe.db.get_value("Item", AMMO, "standard_rate"), item_price(AMMO)), before)
		self.assertFalse(frappe.get_all("Version", filters={"ref_doctype": "Item", "docname": AMMO,
			"data": ["like", "%standard_rate%"]}))

	def test_a_manager_changes_the_price_on_the_item_and_the_register(self):
		doc = _edit(AMMO, as_user=MANAGER, standard_rate=23.99)
		self.assertEqual(frappe.db.get_value("Item", AMMO, "standard_rate"), 23.99)
		self.assertEqual(item_price(AMMO), 23.99)
		self.assertEqual(frappe.db.get_value("CRM Product", doc.name, "standard_rate"), 23.99)
		self.assertTrue(frappe.get_all("Version", filters={"ref_doctype": "Item", "docname": AMMO, "owner": MANAGER}))

	def test_a_name_and_price_edit_leaves_the_form_saveable(self):
		doc = _edit(AMMO, as_user=MANAGER, product_name="_T Ammo renamed", standard_rate=24.49)
		self.assertEqual(str(doc.modified), str(frappe.db.get_value("CRM Product", doc.name, "modified")))
		frappe.set_user(MANAGER)
		try:
			doc.description = "<p>again</p>"
			doc.save()   # the same form, saved again: no TimestampMismatchError
		finally:
			frappe.set_user("Administrator")

	def test_a_manager_without_item_rights_still_changes_a_price(self):
		user("crm-pricer@example.com", "Sales Manager")
		_edit(UNIFORM, as_user="crm-pricer@example.com", standard_rate=579)
		self.assertEqual((frappe.db.get_value("Item", UNIFORM, "standard_rate"), item_price(UNIFORM)), (579, 579))

	def test_a_clerk_cannot_change_a_price_or_switch_a_product_off(self):
		with self.assertRaises(frappe.PermissionError):
			_edit(AMMO, as_user=CLERK, standard_rate=1)
		with self.assertRaises(frappe.PermissionError):
			_edit(AMMO, as_user=CLERK, disabled=1)
		self.assertEqual((item_price(AMMO), frappe.db.get_value("Item", AMMO, "disabled")), (24.99, 0))

	def test_a_clerk_who_cannot_edit_items_cannot_edit_their_text(self):
		with self.assertRaises(frappe.PermissionError):
			_edit(AMMO, as_user=CLERK, description="cheap")
		self.assertNotEqual(frappe.db.get_value("Item", AMMO, "description"), "cheap")

	def test_a_read_only_price_is_refused_with_the_reason(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			_edit(PER_GUN, as_user=MANAGER, standard_rate=799)
		self.assertIn(pricing.price_readonly_reason(PER_GUN), str(ctx.exception))

	def test_a_manager_switches_a_product_off(self):
		_edit(UNIFORM, as_user=MANAGER, disabled=1)
		self.assertEqual(frappe.db.get_value("Item", UNIFORM, "disabled"), 1)

	def test_the_code_rename_and_delete_belong_to_the_item(self):
		with self.assertRaises(frappe.ValidationError):
			_edit(AMMO, product_code="_T-CRM-OTHER")
		with self.assertRaises(frappe.ValidationError):
			frappe.rename_doc("CRM Product", product_of(AMMO), "_T-CRM-AMMO-2")
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("CRM Product", product_of(AMMO))
		self.assertTrue(frappe.db.exists("CRM Product", AMMO))
