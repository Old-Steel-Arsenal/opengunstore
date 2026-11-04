"""Item → CRM Product: created and kept at the register's price, linked both ways, CRM's own
Item hooks skipped, and an Item save that never fails because of CRM."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.crm import item as item_sync
from ffl_core.crm import setup
from ffl_core.tests.crm_support import AMMO, PER_GUN, UNIFORM, CRMTestCase, make_item, product_of, set_item_price


class TestItemSync(CRMTestCase):
	def test_a_new_item_becomes_a_linked_product(self):
		doc = make_item("_T-CRM-NEW", item_name="Range bag", description="<p>Big bag</p>", standard_rate=40)
		name = product_of(doc.name)
		self.assertEqual(name, doc.name)
		product = frappe.get_doc("CRM Product", name)
		self.assertEqual((product.product_code, product.product_name, product.description, product.disabled,
			product.standard_rate), (doc.name, "Range bag", "<p>Big bag</p>", 0, 40))
		self.assertEqual(frappe.db.get_value("Item", doc.name, "crm_product_code"), name)
		self.assertTrue(doc.flags.ignore_crm_sync, "CRM's own Item hooks skip the save")

	def test_an_item_edit_reaches_the_product(self):
		doc = frappe.get_doc("Item", UNIFORM)
		doc.item_name = "Model 19 Gen 5"
		doc.disabled = 1
		doc.save()
		self.assertEqual(frappe.db.get_value("CRM Product", product_of(UNIFORM), ["product_name", "disabled"]),
			("Model 19 Gen 5", 1))

	def test_a_per_gun_model_shows_no_item_level_price(self):
		self.assertEqual(frappe.db.get_value("CRM Product", product_of(PER_GUN), "standard_rate"), 0)

	def test_the_daily_refresh_takes_prices_changed_without_an_item_save(self):
		set_item_price(AMMO, 26.49)   # as Receive Goods writes it: no Item hook runs
		out = item_sync.refresh_product_prices()
		self.assertGreaterEqual(out["changed"], 1)
		self.assertEqual(frappe.db.get_value("CRM Product", product_of(AMMO), "standard_rate"), 26.49)
		self.assertEqual(item_sync.refresh_product_prices()["changed"], 0)

	def test_a_form_opened_before_the_refresh_cannot_send_its_old_price_back(self):
		stale = frappe.get_doc("CRM Product", product_of(AMMO))   # a manager's form, opened yesterday
		set_item_price(AMMO, 26.49)
		item_sync.refresh_product_prices()
		stale.description = "Edited the next morning"
		with self.assertRaises(frappe.TimestampMismatchError):
			stale.save()
		self.assertEqual(frappe.db.get_value("Item", AMMO, "standard_rate"), 22.49)

	def test_the_item_saves_when_the_crm_sync_fails(self):
		title = f"[CRM] Item to CRM product failed: {UNIFORM}"
		logged = frappe.db.count("Error Log", {"method": title})
		doc = frappe.get_doc("Item", UNIFORM)
		doc.item_name = "Renamed while CRM is broken"
		# A fault injected into the sync itself (the test passes because the sync fails). The
		# Error Log it writes outlives the test's rollback, as every Error Log does.
		with patch.object(item_sync, "_values", side_effect=RuntimeError("CRM down")):
			doc.save()
		self.assertEqual(frappe.db.get_value("Item", UNIFORM, "item_name"), "Renamed while CRM is broken")
		self.assertNotEqual(frappe.db.get_value("CRM Product", product_of(UNIFORM), "product_name"),
			"Renamed while CRM is broken")
		self.assertEqual(frappe.db.count("Error Log", {"method": title}), logged + 1)

	def test_deleting_an_item_deletes_its_product(self):
		doc = make_item("_T-CRM-GONE", standard_rate=5)
		self.assertTrue(product_of(doc.name))
		frappe.delete_doc("Item", doc.name)
		self.assertFalse(frappe.db.exists("CRM Product", doc.name))

	def test_backfill_makes_the_missing_products_once(self):
		with patch("ffl_core.crm.item.installed", return_value=False):   # an Item from before CRM: no product
			doc = make_item("_T-CRM-OLD", item_name="Old item")
		self.assertFalse(product_of(doc.name))
		first = item_sync.backfill_products()
		self.assertEqual(first["failed"], 0)
		self.assertEqual(product_of("_T-CRM-OLD"), "_T-CRM-OLD")
		self.assertTrue(frappe.db.get_global(setup.BACKFILL_FLAG))
		count = frappe.db.count("CRM Product")
		item_sync.backfill_products()
		self.assertEqual(frappe.db.count("CRM Product"), count)


class TestItemSyncWithoutCRM(CRMTestCase):
	"""The first deploy of the release that brings CRM runs this code before `install-app crm`."""

	def test_nothing_touches_crm_before_it_is_installed(self):
		with patch("ffl_core.crm.item.installed", return_value=False):
			doc = make_item("_T-CRM-EARLY", standard_rate=9)
			self.assertIsNone(item_sync.refresh_product_prices())
			self.assertIsNone(item_sync.backfill_products())
		self.assertFalse(product_of(doc.name))
		with patch("ffl_core.crm.setup.installed", return_value=False):
			self.assertEqual(setup.ensure(), {"skipped": "crm not installed"})
