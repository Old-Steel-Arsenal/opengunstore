"""Items made in options on the Item form (ffl_core.item_options) reach the store through the
1.1.0 template → variable product sync: the created set pushes as one variable product, a value
added later becomes new variations, a row unticked comes off sale, and grouping clears a simple
listing. Site tests; the WooCommerce client is the only double (``_Web``), and queued jobs are
caught at ``frappe.enqueue`` and run by the test.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import frappe

from ffl_core import item_options as io
from ffl_woo_sync.tests.test_preorder_web import _Web
from ffl_woo_sync.woocommerce import sync, variants


class OptionsWeb(_Web):

	def setUp(self):
		super().setUp()
		t = frappe.generate_hash(length=6).upper()
		self.color, self.size = f"_T Color {t}", f"_T Size {t}"
		self.code = f"_T-WOPT-{t}"
		io.create_item_with_options(
			{"doctype": "Item", "item_code": self.code, "item_name": f"Test Sling {t}", "item_group": "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1, "description": "A sling."},
			[{"name": self.color, "values": ["Black", "Coyote"]}, {"name": self.size, "values": ["S"]}],
			[{"values": {self.color: c, self.size: "S"}, "sku": f"{self.code}-{c[:3].upper()}", "price": p}
				for c, p in (("Black", 30), ("Coyote", 32))])
		self.black, self.coyote = f"{self.code}-BLA", f"{self.code}-COY"
		self.P = random.randint(10**6, 10**7) * 100
		ids = iter(range(self.P + 1, self.P + 99))
		self.client.get_attribute_ids.return_value = {}
		self.client.create_product.return_value = {"id": self.P, "status": "publish"}
		self.client.create_variation.side_effect = lambda parent, payload: {"id": next(ids), "sku": payload.get("sku")}
		self.client.update_variation.side_effect = lambda parent, vid, payload: {"id": int(vid)}
		self.client.update_product.side_effect = lambda pid, payload: {"id": int(pid)}

	def _enqueued(self, fn):
		with patch("frappe.enqueue") as enqueue:
			fn()
		return [(c.args[0], c.kwargs) for c in enqueue.call_args_list]

	def _list(self):
		sync.push_item(self.code, _relist=True)
		self.client.reset_mock(return_value=False, side_effect=False)

	def test_the_created_set_is_one_variable_product(self):
		sync.push_item(self.code, _relist=True)
		parent = self.client.create_product.call_args.args[0]
		self.assertEqual((parent["type"], parent["sku"], parent["description"]), ("variable", self.code, "A sling."))
		attrs = {a["name"]: a["options"] for a in parent["attributes"]}
		self.assertEqual(attrs[self.color], ["Black", "Coyote"])
		made = {c.args[1]["sku"]: c.args[1] for c in self.client.create_variation.call_args_list}
		self.assertEqual(set(made), {self.black, self.coyote})
		self.assertEqual(made[self.coyote]["regular_price"], "32.00")
		self.assertEqual(frappe.db.get_value("Item", self.black, "woo_listed"), 1)

	def test_a_value_added_later_becomes_variations_of_the_listed_product(self):
		self._list()
		options = [{"name": self.color, "values": ["Black", "Coyote", "Ranger Green"]}, {"name": self.size, "values": ["S"]}]
		new = f"{self.code}-RG"
		jobs = self._enqueued(lambda: io.update_option_rows(self.code, [
			{"values": {self.color: "Ranger Green", self.size: "S"}, "sku": new, "price": 33}], options))
		self.assertIn(("ffl_woo_sync.woocommerce.tasks.push_new_variations_task", {"queue": "long",
			"enqueue_after_commit": True, "job_id": f"woo-new-variations::{self.code}", "deduplicate": True,
			"template": self.code}), jobs)

		out = sync.push_new_variations(self.code)
		# The parent's options first (the rest of it untouched), then the one new variation.
		self.client.update_product.assert_called_once()
		pid, payload = self.client.update_product.call_args.args
		self.assertEqual((pid, list(payload)), (str(self.P), ["attributes"]))
		self.assertEqual({a["name"]: a["options"] for a in payload["attributes"]}[self.color],
			["Black", "Coyote", "Ranger Green"])
		self.assertEqual([c.args[1]["sku"] for c in self.client.create_variation.call_args_list], [new])
		self.assertEqual(list(out["variations"]), [new])
		self.client.update_variation.assert_not_called()

	def test_a_template_not_on_the_store_queues_nothing(self):
		options = [{"name": self.color, "values": ["Black", "Coyote", "Tan"]}, {"name": self.size, "values": ["S"]}]
		jobs = self._enqueued(lambda: io.update_option_rows(self.code, [
			{"values": {self.color: "Tan", self.size: "S"}, "sku": f"{self.code}-TAN", "price": 31}], options))
		self.assertNotIn("ffl_woo_sync.woocommerce.tasks.push_new_variations_task", [n for n, _k in jobs])

	def test_an_unticked_row_comes_off_sale_and_back(self):
		self._list()
		vid = frappe.db.get_value("Item", self.coyote, "woo_product_id")
		jobs = self._enqueued(lambda: io.update_option_rows(self.code, [{"item_code": self.coyote, "include": 0}]))
		self.assertIn(("ffl_woo_sync.woocommerce.tasks.delist_item_task", {"queue": "long",
			"enqueue_after_commit": True, "item_code": self.coyote}), jobs)
		self.assertNotIn("ffl_woo_sync.woocommerce.tasks.push_item_task", [n for n, _k in jobs])
		sync.delist_item(self.coyote)
		self.client.update_variation.assert_called_once_with(str(self.P), vid, dict(sync.OFF_SALE_PAYLOAD))
		self.client.reset_mock(return_value=False, side_effect=False)

		jobs = self._enqueued(lambda: io.update_option_rows(self.code, [{"item_code": self.coyote, "include": 1}]))
		self.assertIn("ffl_woo_sync.woocommerce.tasks.push_item_task", [n for n, _k in jobs])
		sync.push_item(self.coyote, pos_fields_only=True)
		parent, again, payload = self.client.update_variation.call_args.args
		self.assertEqual((parent, again, payload["status"]), (str(self.P), vid, "publish"))

	def test_a_price_edit_is_a_lean_variation_push(self):
		self._list()
		jobs = self._enqueued(lambda: io.update_option_rows(self.code, [{"item_code": self.black, "price": 35}]))
		self.assertIn(("ffl_woo_sync.woocommerce.tasks.push_item_task", {"queue": "long",
			"enqueue_after_commit": True, "item_code": self.black}), jobs)
		sync.push_item(self.black, pos_fields_only=True)
		self.assertEqual(self.client.update_variation.call_args.args[2]["regular_price"], "35.00")

	def test_grouping_an_item_with_its_own_product_clears_that_link(self):
		self._list()
		plain = f"_T-WPLAIN-{frappe.generate_hash(length=6).upper()}"
		frappe.get_doc({"doctype": "Item", "item_code": plain, "item_name": plain, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 1, "standard_rate": 29}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", plain, {"woo_product_id": "4242", "woo_listed": 1})
		self.assertEqual(variants.simple_listings(self.code, [plain]), {plain: "4242"})
		# A size the item shows no row for yet: added to the option, its row left unticked.
		io.update_option_rows(self.code, [{"values": {self.color: "Black", self.size: "M"}, "sku": f"{plain}-X",
			"price": 1, "include": 0}], [{"name": self.color, "values": ["Black", "Coyote"]},
			{"name": self.size, "values": ["S", "M"]}])
		self.assertFalse(frappe.db.exists("Item", f"{plain}-X"))
		with patch("frappe.enqueue"):
			out = io.group_items(self.code, {plain: {self.color: "Black", self.size: "M"}})
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(frappe.db.get_value("Item", plain, ["woo_product_id", "woo_listed", "variant_of"]),
			(None, 0, self.code))
		sync.push_new_variations(self.code)
		self.assertEqual([c.args[1]["sku"] for c in self.client.create_variation.call_args_list], [plain])
