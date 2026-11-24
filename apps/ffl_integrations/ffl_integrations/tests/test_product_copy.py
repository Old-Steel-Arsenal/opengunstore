"""The catalog service's optimized copy onto the Item, field by field.

A product can be optimized in part — SEO fields written, the title still the
distributor's. Each Item field is written only from its own optimized value: the
name only when the served title differs from the distributor's own (``original``),
the description likewise, the short description and SEO fields when set.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import catalog, items

CODE = "_TEST-COPY-ITEM"
RAW_TITLE = "MAG PMAG 30 AR/M4 GEN M3 BLK"
RAW_BODY = "<p>Feed text.</p>"
FIELDS = ["item_name", "description", "woo_short_description", "woo_seo_title",
	"woo_seo_description", "woo_focus_keyword"]


def _served(**over) -> dict:
	"""A catalog product as ``catalog._product`` hands it over."""
	raw = {"key": "k", "description": RAW_TITLE, "long_description": RAW_BODY,
		"content_optimized": True, "original": {"description": RAW_TITLE,
		"long_description": RAW_BODY}}
	raw.update(over)
	return catalog._product(raw, [])


class TestApplyProductCopy(FrappeTestCase):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Item", CODE):
			frappe.get_doc({"doctype": "Item", "item_code": CODE, "item_name": "Shop name",
				"item_group": "All Item Groups", "is_stock_item": 1, "stock_uom": "Nos",
				"description": "<p>Shop text.</p>"}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", CODE, {"item_name": "Shop name",
			"description": "<p>Shop text.</p>", "woo_short_description": "",
			"woo_seo_title": "", "woo_seo_description": "", "woo_focus_keyword": ""})

	def _apply(self, product):
		with patch.object(catalog, "product", return_value=product):
			items._apply_product_copy(CODE)
		return frappe.db.get_value("Item", CODE, FIELDS, as_dict=True)

	def test_catalog_keeps_the_seo_fields_and_original(self):
		p = _served(short_description="<p>S</p>", meta_description="M", seo_title="T",
			focus_keyword="K")
		self.assertEqual((p["short_description"], p["meta_description"], p["seo_title"],
			p["focus_keyword"], p["original"]["description"]),
			("<p>S</p>", "M", "T", "K", RAW_TITLE))
		self.assertEqual((catalog._product({"key": "k"}, [])["seo_title"],
			catalog._product({"key": "k"}, [])["original"]), ("", {}))

	def test_seo_fields_without_an_optimized_title_leave_the_name_alone(self):
		"""The served title is the distributor's own: the Item keeps its name and
		description, and takes only the fields that were optimized."""
		row = self._apply(_served(seo_title="Magpul PMAG 30 Gen M3 Magazine",
			meta_description="A 30-round AR magazine.", focus_keyword="magpul pmag 30"))
		self.assertEqual((row.item_name, row.description), ("Shop name", "<p>Shop text.</p>"))
		self.assertEqual((row.woo_seo_title, row.woo_seo_description, row.woo_focus_keyword,
			row.woo_short_description or ""),
			("Magpul PMAG 30 Gen M3 Magazine", "A 30-round AR magazine.", "magpul pmag 30", ""))

	def test_an_optimized_title_and_body_are_written(self):
		row = self._apply(_served(description="Magpul PMAG 30 AR/M4 Gen M3 Magazine",
			long_description="<h3>Features</h3><p>Thirty.</p>",
			short_description="<p>Thirty rounds.</p>"))
		self.assertEqual((row.item_name, row.description, row.woo_short_description),
			("Magpul PMAG 30 AR/M4 Gen M3 Magazine",
			"<p><strong>Features</strong></p><p>Thirty.</p>", "<p>Thirty rounds.</p>"))
		self.assertEqual(row.woo_seo_title or "", "", "an empty SEO field is not written")

	def test_an_optimized_body_alone_keeps_the_name(self):
		row = self._apply(_served(long_description="<p>Better.</p>"))
		self.assertEqual((row.item_name, row.description), ("Shop name", "<p>Better.</p>"))

	def test_empty_values_never_blank_the_item(self):
		frappe.db.set_value("Item", CODE, "woo_seo_title", "Hand-written")
		row = self._apply(_served(description="", long_description="",
			original={}, seo_title=""))
		self.assertEqual((row.item_name, row.description, row.woo_seo_title),
			("Shop name", "<p>Shop text.</p>", "Hand-written"))

	def test_unoptimized_product_changes_nothing(self):
		row = self._apply(_served(content_optimized=False, seo_title="T",
			description="Other title"))
		self.assertEqual((row.item_name, row.woo_seo_title or ""), ("Shop name", ""))

	def test_unchanged_copy_writes_nothing(self):
		product = _served(seo_title="T")
		self._apply(product)
		with patch.object(catalog, "product", return_value=product), \
			patch("frappe.db.set_value") as set_value:
			items._apply_product_copy(CODE)
		set_value.assert_not_called()
