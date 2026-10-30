"""setup/item_layout.ensure — a field_order Property Setter on a core form we
lay out must not survive a migrate, or the custom_field fixtures' insert_after
is ignored for every field the setter saw (2026-09-02 prod: Item Channels tab
rendered empty). Plus the section each moved field must land in."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import item_layout


class ItemFieldOrderSetterIsDropped(FrappeTestCase):
	def test_a_saved_field_order_setter_is_removed_and_ensure_is_idempotent(self):
		if not frappe.db.exists("Property Setter", {"doc_type": "Item", "property": "field_order"}):
			frappe.get_doc({
				"doctype": "Property Setter", "doctype_or_field": "DocType",
				"doc_type": "Item", "property": "field_order", "property_type": "Data",
				"value": frappe.as_json(["item_code", "item_name"]),
			}).insert(ignore_permissions=True)
		item_layout.ensure()
		self.assertFalse(frappe.db.exists("Property Setter", {"doc_type": "Item", "property": "field_order"}))
		item_layout.ensure()  # nothing left: must not raise
		self.assertIn("ffl_core.setup.item_layout.ensure", frappe.get_hooks("after_migrate"))

	def test_every_laid_out_doctype_is_covered(self):
		if not frappe.db.exists("Property Setter", {"doc_type": "Serial No", "property": "field_order"}):
			frappe.get_doc({
				"doctype": "Property Setter", "doctype_or_field": "DocType",
				"doc_type": "Serial No", "property": "field_order", "property_type": "Data",
				"value": frappe.as_json(["serial_no", "item_code"]),
			}).insert(ignore_permissions=True)
		item_layout.ensure()
		self.assertFalse(frappe.db.exists("Property Setter",
			{"doc_type": ("in", item_layout.DOCTYPES), "property": "field_order"}))


def _sections(doctype):
	"""fieldname -> the Section Break it renders under ("" before the first)."""
	frappe.clear_cache(doctype=doctype)
	section, out = "", {}
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in ("Section Break", "Tab Break"):
			section = df.fieldname
		out[df.fieldname] = section
	return out


class CoreFormSections(FrappeTestCase):
	"""A custom Section Break anchored on a core field walks forward to the next
	field of the anchor's own type (meta.sort_fields), so it can land mid-column
	and pull core fields into our section. Pin where the fields end up."""

	def test_top_sections_keep_their_core_fields(self):
		cust = _sections("Customer")
		for f in ("customer_group", "territory", "ffl_first_name", "gender"):
			self.assertEqual(cust[f], cust["customer_name"], f)
		self.assertEqual(cust["is_ffl_dealer"], "section_ffl")
		supp = _sections("Supplier")
		self.assertEqual(supp["country"], supp["supplier_name"])
		# The ATF picker fills the license fields, so it sits right under Is FFL Dealer.
		for dt in ("Customer", "Supplier"):
			order = [df.fieldname for df in frappe.get_meta(dt).fields]
			self.assertEqual(order.index("atf_ffl_record"), order.index("is_ffl_dealer") + 1, dt)

	def test_item_gunbroker_status_is_its_own_section(self):
		item = _sections("Item")
		for f in ("gb_category_id", "gb_featured", "gb_view_counter"):
			self.assertEqual(item[f], "item_section_gunbroker", f)
		for f in ("gb_item_id", "gb_delisted", "gb_price_pushed"):
			self.assertEqual(item[f], "item_section_gunbroker_status", f)

	def test_serial_no_groups(self):
		sn = _sections("Serial No")
		for f in ("status", "item_name", "description", "warehouse", "purchase_rate"):
			self.assertEqual(sn[f], "details", f)
		for f in ("sell_price", "dealer_sell_price", "sale_sell_price", "sale_to"):
			self.assertEqual(sn[f], "sn_section_pricing", f)
		for f in ("condition_grade", "importer", "year_made"):
			self.assertEqual(sn[f], "ffl_firearm_section", f)

	def test_transaction_sections(self):
		pr = _sections("Purchase Receipt")
		for f in ("ffl_source", "ffl_acquisition_source", "ffl_seller_id_document", "ffl_transfer_for_customer"):
			self.assertEqual(pr[f], "ffl_section_acquisition", f)
		self.assertEqual(pr["posting_date"], pr["supplier"])
		si = _sections("Sales Invoice")
		self.assertEqual(si["shipping_address"], "shipping_address_section")
		self.assertEqual(si["dispatch_address_name"], "shipping_address_section")
		if "card_charges" in (pos := _sections("POS Invoice")):
			for f in ("card_refunded_amount", "card_manual_settlement", "card_charges"):
				self.assertEqual(pos[f], "section_card", f)
