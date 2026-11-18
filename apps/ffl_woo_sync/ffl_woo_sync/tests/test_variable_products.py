"""Item templates and variants → a WooCommerce variable product and its variations.

Site tests: real Item Attributes, a template Item and variant Items made by ERPNext's own
``create_variant``; the WooCommerce client is the only double. The variant's
``woo_product_id`` holds its VARIATION id and every write to it goes under the parent
(the template's ``woo_product_id``) — never ``products/{variation id}``.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import frappe
from erpnext.controllers.item_variant import create_variant

from ffl_core.setup.preorder import AVAILABLE_FIELD, ETA_FIELD
from ffl_woo_sync.tests.test_preorder_web import _Web
from ffl_woo_sync.woocommerce import dangling, mapping, sync, tasks, variants
from ffl_woo_sync.woocommerce.client import WooCommerceError

def _meta(payload) -> dict:
	return {m["key"]: m["value"] for m in payload.get("meta_data") or []}


class _Variable(_Web):
	"""A template with two attributes (Fits × Selector) and three variants, priced."""

	def setUp(self):
		super().setUp()
		token = frappe.generate_hash(length=5).upper()
		self.fits, self.sel = f"_T Fits {token}", f"_T Selector {token}"
		for name, values in ((self.fits, ["SP5", "SP5K", "PTR"]), (self.sel, ["Navy", "SEF"])):
			frappe.get_doc({"doctype": "Item Attribute", "attribute_name": name,
				"item_attribute_values": [{"attribute_value": v, "abbr": v} for v in values]},
			).insert(ignore_permissions=True)
		self.template = f"_T-VAR-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": self.template, "item_name": f"Test Lower {token}",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1, "has_variants": 1,
			"description": "A lower.", "attributes": [{"attribute": self.fits}, {"attribute": self.sel}]},
		).insert(ignore_permissions=True)
		self.v = {}
		for fits, sel, price in (("SP5", "Navy", 450), ("PTR", "SEF", 475), ("SP5", "SEF", 460)):
			doc = create_variant(self.template, {self.fits: fits, self.sel: sel})
			doc.insert(ignore_permissions=True)
			frappe.db.set_value("Item", doc.name, "standard_rate", price)
			self.v[(fits, sel)] = doc.name
		# Ids of this test's own: a class rolls back only at its end, so the earlier tests'
		# templates are still listed on the test site.
		self.P = random.randint(10**6, 10**7) * 100
		self.next_id = iter(range(self.P + 1, self.P + 99))
		self.client.get_attribute_ids.return_value = {}
		self.client.create_product.return_value = {"id": self.P, "status": "publish"}
		self.client.create_variation.side_effect = lambda parent, payload: {
			"id": next(self.next_id), "sku": payload.get("sku")}
		self.client.update_variation.side_effect = lambda parent, vid, payload: {"id": int(vid)}
		self.client.update_product.side_effect = lambda pid, payload: {"id": int(pid)}

	def _pid(self, item):
		return frappe.db.get_value("Item", item, "woo_product_id")

	def _push_all(self):
		return sync.push_item(self.template, _relist=True)

	def _variation_payloads(self) -> dict:
		return {c.args[1]["sku"]: c.args[1] for c in self.client.create_variation.call_args_list}


class Push(_Variable):

	def test_the_template_is_one_variable_product_and_each_variant_a_variation(self):
		result = self._push_all()

		parent = self.client.create_product.call_args.args[0]
		self.assertEqual(parent["type"], "variable")
		self.assertEqual(parent["sku"], self.template)
		self.assertEqual(parent["description"], "A lower.")
		self.assertFalse(parent["manage_stock"])
		for key in ("regular_price", "sale_price", "stock_quantity"):
			self.assertNotIn(key, parent)
		attrs = {a["name"]: a for a in parent["attributes"]}
		# Options: the values the variants use, in the Item Attribute's order (no SP5K).
		self.assertEqual(attrs[self.fits]["options"], ["SP5", "PTR"])
		self.assertEqual(attrs[self.sel]["options"], ["Navy", "SEF"])
		self.assertTrue(attrs[self.fits]["variation"] and attrs[self.sel]["variation"])

		self.assertEqual({c.args[0] for c in self.client.create_variation.call_args_list}, {str(self.P)})
		payloads = self._variation_payloads()
		self.assertEqual(set(payloads), set(self.v.values()))
		navy = payloads[self.v[("SP5", "Navy")]]
		self.assertEqual(navy["regular_price"], "450.00")
		self.assertEqual(navy["status"], "publish")
		self.assertEqual(sorted((a["name"], a["option"]) for a in navy["attributes"]),
			sorted([(self.fits, "SP5"), (self.sel, "Navy")]))
		for key in ("name", "description", "categories", "images", "type"):
			self.assertNotIn(key, navy)

		self.assertEqual(self._pid(self.template), str(self.P))
		ids = {code: self._pid(code) for code in self.v.values()}
		self.assertEqual(sorted(ids.values()), [str(self.P + i) for i in (1, 2, 3)])
		self.assertEqual(result["variations"], ids)
		for code in [self.template, *self.v.values()]:
			self.assertEqual(frappe.db.get_value("Item", code, "woo_listed"), 1)
		# Nothing went to products/{variation id}.
		self.client.update_product.assert_not_called()

	def test_a_variation_sells_its_preorder_slots(self):
		code = self.v[("PTR", "SEF")]
		frappe.db.set_value("Item", code, {AVAILABLE_FIELD: 10, ETA_FIELD: "2026-12-31"})
		self._push_all()
		payload = self._variation_payloads()[code]
		self.assertEqual((payload["stock_quantity"], payload["manage_stock"]), (10, True))
		self.assertEqual(_meta(payload)["_osa_preorder"], "1")
		self.assertEqual(_meta(payload)["_osa_preorder_eta"], "2026-12-31")
		# The parent shows pre-order while a variation sells slots (its earliest ETA).
		parent = self.client.create_product.call_args.args[0]
		self.assertEqual((_meta(parent)["_osa_preorder"], _meta(parent)["_osa_preorder_eta"]),
			("1", "2026-12-31"))

	def test_a_second_push_updates_the_same_variations(self):
		self._push_all()
		self.client.reset_mock()
		self._push_all()
		self.client.create_product.assert_not_called()
		self.client.create_variation.assert_not_called()
		self.client.update_product.assert_called_once()
		self.assertEqual(self.client.update_product.call_args.args[0], str(self.P))
		self.assertEqual(sorted(c.args[1] for c in self.client.update_variation.call_args_list),
			[str(self.P + i) for i in (1, 2, 3)])

	def test_a_variant_pushed_before_its_template_brings_the_template(self):
		code = self.v[("SP5", "Navy")]
		self.assertEqual(sync.push_item(code, pos_fields_only=True)["skipped"], sync.NO_PARENT)
		self.client.create_product.assert_not_called()
		sync.push_item(code)
		self.client.create_product.assert_called_once()
		self.assertEqual(self.client.create_variation.call_count, 3)


class Changes(_Variable):

	def setUp(self):
		super().setUp()
		self._push_all()
		self.client.reset_mock()
		self.code = self.v[("SP5", "Navy")]
		self.vid = self._pid(self.code)

	def test_stock_goes_to_that_variation_only(self):
		frappe.db.set_value("Item", self.code, {AVAILABLE_FIELD: 4})
		sync.push_stock(self.code)
		self.client.update_variation.assert_called_once()
		parent, vid, payload = self.client.update_variation.call_args.args
		self.assertEqual((parent, vid, payload["stock_quantity"]), (str(self.P), self.vid, 4))
		# Pre-order touches this variant, so only the parent's pre-order meta follows.
		self.client.update_product.assert_called_once_with(str(self.P), {"meta_data": [
			{"key": "_osa_preorder", "value": "1"}, {"key": "_osa_preorder_eta", "value": ""}]})
		self.assertEqual(sync.push_stock(self.template)["skipped"], "template")

	def test_the_stock_batch_sends_a_variant_under_its_parent(self):
		results = sync.push_stock_batch([self.code])
		self.client.batch_update_products.assert_not_called()
		self.assertEqual(self.client.update_variation.call_args.args[:2], (str(self.P), self.vid))
		self.assertNotIsInstance(results[self.code], Exception)

	def test_a_price_change_is_a_lean_variation_update(self):
		frappe.db.set_value("Item", self.code, "standard_rate", 499)
		sync.push_item(self.code, pos_fields_only=True)
		parent, vid, payload = self.client.update_variation.call_args.args
		self.assertEqual((parent, vid), (str(self.P), self.vid))
		self.assertEqual(payload["regular_price"], "499.00")
		for key in ("attributes", "image", "sku"):
			self.assertNotIn(key, payload)
		self.client.update_product.assert_not_called()

	def test_an_unpriced_variant_is_taken_off_sale_under_its_parent(self):
		frappe.db.set_value("Item", self.code, "standard_rate", 0)
		self.assertEqual(sync.push_item(self.code, pos_fields_only=True)["skipped"], sync.UNPRICED)
		self.client.update_variation.assert_called_once_with(
			str(self.P), self.vid, dict(sync.OFF_SALE_PAYLOAD))
		self.assertEqual(frappe.db.get_value("Item", self.code, "woo_listed"), 0)

	def test_delisting_the_template_keeps_its_variants_off(self):
		sync.delist_item(self.template, _deliberate=True)
		self.client.update_product.assert_called_once_with(str(self.P), dict(sync.OFF_SALE_PAYLOAD))
		self.assertEqual(sync.push_item(self.code, pos_fields_only=True)["skipped"], "delisted")
		self.client.update_variation.assert_not_called()
		sync.push_item(self.template, _relist=True)
		self.assertEqual(frappe.db.get_value("Item", self.template, "woo_delisted"), 0)
		self.assertEqual(self.client.update_variation.call_count, 3)

	def test_a_variant_delists_and_relists_alone(self):
		sync.delist_item(self.code, _deliberate=True)
		self.client.update_variation.assert_called_once_with(
			str(self.P), self.vid, dict(sync.OFF_SALE_PAYLOAD))
		self.client.reset_mock()
		# The template's push leaves a variant delisted on its own alone ...
		result = sync.push_item(self.template, _relist=True)
		self.assertEqual(result["variations"][self.code], "delisted")
		self.assertNotIn(self.vid, [c.args[1] for c in self.client.update_variation.call_args_list])
		# ... its own push brings it back.
		sync.push_item(self.code, _relist=True)
		self.assertEqual(frappe.db.get_value("Item", self.code, "woo_delisted"), 0)
		self.assertEqual(self.client.update_variation.call_args.args[1], self.vid)


class Reconcile(_Variable):

	def setUp(self):
		super().setUp()
		self._push_all()
		self.client.reset_mock()
		self.client.get_attribute_ids.return_value = {}
		self.client.update_variation.side_effect = lambda parent, vid, payload: {"id": int(vid)}
		self.client.update_product.side_effect = lambda pid, payload: {"id": int(pid)}
		self.variations = {}
		for code in self.v.values():
			payload = mapping.variant_to_variation_payload(code)
			vid = self._pid(code)
			self.variations[vid] = {"id": int(vid), "status": "publish",
				"stock_quantity": payload["stock_quantity"], "regular_price": payload["regular_price"],
				"sale_price": "", "date_on_sale_from": None, "date_on_sale_to": None,
				"meta_data": payload["meta_data"]}
		self.parent = {"id": self.P, "status": "publish", "type": "variable"}
		self.client.get_products.side_effect = lambda ids, **kw: (
			{str(self.P): dict(self.parent)} if str(self.P) in [str(i) for i in ids] else {})
		self.client.list_variations.side_effect = lambda parent: {
			k: dict(v) for k, v in self.variations.items()} if str(parent) == str(self.P) else {}
		self.client.get_variation.side_effect = lambda parent, vid: dict(self.variations[str(vid)])
		self.client.get_product.side_effect = lambda pid: dict(self.parent) if str(pid) == str(self.P) else {
			"id": int(pid), "status": "draft"}

	def _run(self):
		with patch("frappe.enqueue"):
			tasks._run_reconcile("retail")

	def test_in_step_nothing_is_pushed_and_the_template_is_not_price_guarded(self):
		self._run()
		self.client.update_product.assert_not_called()
		self.client.update_variation.assert_not_called()
		self.assertEqual(frappe.db.get_value("Item", self.template, "woo_listed"), 1)
		self.client.list_variations.assert_called_once_with(str(self.P))
		self.assertEqual(frappe.db.get_value("Item", self.template, "woo_delisted"), 0)

	def test_a_drifted_variation_is_healed_under_its_parent(self):
		code = self.v[("PTR", "SEF")]
		self.variations[self._pid(code)]["stock_quantity"] = 7
		self._run()
		self.client.update_variation.assert_called_once()
		parent, vid, payload = self.client.update_variation.call_args.args
		self.assertEqual((parent, vid, payload["stock_quantity"]), (str(self.P), self._pid(code), 0))
		self.assertNotIn("attributes", payload)

	def test_a_variation_disabled_in_wp_admin_is_marked_delisted(self):
		code = self.v[("SP5", "SEF")]
		self.variations[self._pid(code)]["status"] = "private"
		self._run()
		self.assertEqual(frappe.db.get_value("Item", code, ["woo_delisted", "woo_listed"]), (1, 0))
		self.client.update_variation.assert_not_called()

	def test_a_parent_drafted_in_wp_admin_delists_the_template_and_skips_its_variations(self):
		self.parent["status"] = "draft"
		self.variations[self._pid(self.v[("PTR", "SEF")])]["stock_quantity"] = 7
		self._run()
		self.assertEqual(frappe.db.get_value("Item", self.template, ["woo_delisted", "woo_listed"]), (1, 0))
		self.assertNotIn(str(self.P), [str(c.args[0]) for c in self.client.list_variations.call_args_list])
		self.client.update_variation.assert_not_called()

	def test_the_dangling_audit_asks_for_a_variation_under_its_parent(self):
		gone = self._pid(self.v[("SP5", "Navy")])

		def get_variation(parent, vid):
			if str(parent) == str(self.P) and str(vid) == gone:
				raise WooCommerceError("not found", status_code=404)
			return {"id": int(vid)}

		self.client.get_variation.side_effect = get_variation
		found = []
		dangling._audit_site("retail", 50, False, found)
		mine = [d for d in found if d["name"] in self.v.values() or d["name"] == self.template]
		self.assertEqual([(d["name"], d["woo_id"]) for d in mine], [(self.v[("SP5", "Navy")], gone)])
		self.assertNotIn(gone, [str(c.args[0]) for c in self.client.get_product.call_args_list])


class Orders(_Variable):

	def test_a_variation_line_imports_on_the_preorder_lane_and_takes_a_slot(self):
		self._push_all()
		code = self.v[("PTR", "SEF")]
		frappe.db.set_value("Item", code, {AVAILABLE_FIELD: 10, ETA_FIELD: "2026-12-31"})
		body = self._body([(code, 2, 950, 0)])
		body["line_items"][0].update(product_id=self.P, variation_id=int(self._pid(code)),
			name="Test Lower - PTR, SEF")
		order = self._row(self._send(body))
		self.assertEqual([(l.item_code, l.lane, l.preorder_slots) for l in order.lines],
			[(code, "preorder", 2)])
		self.assertEqual(frappe.db.get_value("Item", code, AVAILABLE_FIELD), 8)
		# The slots left reach that variation, under its parent.
		self.client.reset_mock()
		self.client.update_variation.side_effect = lambda parent, vid, payload: {"id": int(vid)}
		sync.push_stock(code)
		parent, vid, payload = self.client.update_variation.call_args.args
		self.assertEqual((parent, vid, payload["stock_quantity"]), (str(self.P), self._pid(code), 8))


class Migration(_Variable):
	"""Existing simple Items made variants, then linked to a variable product built by hand."""

	def setUp(self):
		super().setUp()
		token = frappe.generate_hash(length=5).upper()
		self.plain = [f"_T-PLAIN-{token}-{i}" for i in range(2)]
		for code in self.plain:
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1,
				"standard_rate": 400}).insert(ignore_permissions=True)
			frappe.db.set_value("Item", code, {"woo_product_id": "77", "woo_delisted": 1})

	def test_make_variants_checks_everything_before_writing(self):
		bad = {self.plain[0]: {self.fits: "SP5K", self.sel: "Navy"},
			self.plain[1]: {self.fits: "SP5K", self.sel: "Nope"}}
		out = variants.make_variants(self.template, bad, dry_run=0)
		self.assertFalse(out["ok"])
		self.assertTrue(any("'Nope'" in e for e in out["errors"]))
		self.assertFalse(frappe.db.get_value("Item", self.plain[0], "variant_of"))
		taken = {self.plain[0]: {self.fits: "SP5", self.sel: "Navy"}}
		self.assertTrue(any("same attributes" in e for e in
			variants.make_variants(self.template, taken)["errors"]))

	def test_make_variants_then_adopt_the_hand_built_product(self):
		wanted = {self.plain[0]: {self.fits: "SP5K", self.sel: "Navy"},
			self.plain[1]: {self.fits: "SP5K", self.sel: "SEF"}}
		self.assertTrue(variants.make_variants(self.template, wanted)["ok"])
		self.assertFalse(frappe.db.get_value("Item", self.plain[0], "variant_of"), "dry run wrote")
		made = variants.make_variants(self.template, wanted, dry_run=0)
		self.assertTrue(made["ok"])
		# Their simple-product ids are not variation ids: cleared, and reported.
		self.assertEqual(made["cleared"], {self.plain[0]: "77", self.plain[1]: "77"})
		self.assertEqual(frappe.db.get_value("Item", self.plain[0], ["woo_product_id", "woo_listed"]),
			(None, 0))
		self.assertEqual(mapping.variant_kind(self.plain[0]), (mapping.VARIANT, self.template))
		doc = frappe.get_doc("Item", self.plain[1])
		self.assertEqual({(a.attribute, a.attribute_value) for a in doc.attributes},
			{(self.fits, "SP5K"), (self.sel, "SEF")})
		doc.save(ignore_permissions=True)  # ERPNext's own variant validation accepts it

		self.client.get_product.side_effect = None
		self.client.get_product.return_value = {"id": 20964, "type": "variable", "status": "publish",
			"name": "Hand built"}
		self.client.list_variations.return_value = {
			"801": {"id": 801, "sku": self.plain[0]}, "802": {"id": 802, "sku": self.plain[1]},
			"803": {"id": 803, "sku": "SOMETHING-ELSE"}}
		dry = variants.adopt_variable_product(self.template, 20964)
		self.assertEqual(dry["matched"], {self.plain[0]: "801", self.plain[1]: "802"})
		self.assertEqual(dry["woo_only"], ["SOMETHING-ELSE"])
		self.assertEqual(sorted(dry["pos_only"]), sorted(self.v.values()))
		self.assertEqual(dry["replaced"], {})
		self.assertEqual(self._pid(self.template), None)

		variants.adopt_variable_product(self.template, 20964, dry_run=0)
		self.assertEqual(self._pid(self.template), "20964")
		self.assertEqual(frappe.db.get_value("Item", self.plain[0], ["woo_product_id", "woo_delisted",
			"woo_listed"]), ("801", 0, 1))
		# The next push updates the adopted product and variations; the three
		# variants the store had no variation for are created under it.
		sync.push_item(self.template, _relist=True)
		self.client.create_product.assert_not_called()
		self.assertEqual(self.client.update_product.call_args.args[0], "20964")
		self.assertEqual({c.args[1] for c in self.client.update_variation.call_args_list}, {"801", "802"})
		self.assertEqual({c.args[0] for c in self.client.create_variation.call_args_list}, {"20964"})
		self.assertEqual(self.client.create_variation.call_count, 3)

	def test_adopt_refuses_a_simple_product(self):
		self.client.get_product.side_effect = None
		self.client.get_product.return_value = {"id": 5, "type": "simple"}
		with self.assertRaisesRegex(frappe.ValidationError, "not a variable product"):
			variants.adopt_variable_product(self.template, 5)


class ReviewFixes(_Variable):

	def test_the_parent_preorder_meta_follows_the_variants(self):
		a, b = self.v[("SP5", "Navy")], self.v[("PTR", "SEF")]
		frappe.db.set_value("Item", a, {AVAILABLE_FIELD: 3, ETA_FIELD: "2027-02-01"})
		frappe.db.set_value("Item", b, {AVAILABLE_FIELD: 2, ETA_FIELD: "2027-01-15"})
		self.assertEqual(mapping.template_preorder_meta(self.template),
			{"_osa_preorder": "1", "_osa_preorder_eta": "2027-01-15"})
		self._push_all()
		self.client.reset_mock()
		self.client.update_variation.side_effect = lambda parent, vid, payload: {"id": int(vid)}
		# Every slot gone: the variation's stock push turns the parent's notice off.
		frappe.db.set_value("Item", a, AVAILABLE_FIELD, 0)
		frappe.db.set_value("Item", b, AVAILABLE_FIELD, 0)
		sync.push_stock(b)
		self.client.update_product.assert_called_once_with(str(self.P), {"meta_data": [
			{"key": "_osa_preorder", "value": ""}, {"key": "_osa_preorder_eta", "value": ""}]})
		# A lean template push carries the derived meta, never the template's own blanks.
		frappe.db.set_value("Item", b, AVAILABLE_FIELD, 4)
		self.client.reset_mock()
		sync.push_item(self.template, pos_fields_only=True)
		lean = self.client.update_product.call_args.args[1]
		self.assertEqual(_meta(lean)["_osa_preorder"], "1")
		self.client.update_variation.assert_not_called()

	def test_reconcile_heals_a_parent_whose_preorder_meta_drifted(self):
		self._push_all()
		frappe.db.set_value("Item", self.v[("PTR", "SEF")], {AVAILABLE_FIELD: 5})
		self.client.reset_mock()
		self.client.get_attribute_ids.return_value = {}
		self.client.get_products.side_effect = lambda ids, **kw: {str(self.P): {
			"id": self.P, "status": "publish", "meta_data": []}} if str(self.P) in map(str, ids) else {}
		self.client.get_product.side_effect = lambda pid: {"id": int(pid), "status": "draft"}
		self.client.list_variations.side_effect = lambda parent: {}
		self.client.get_variation.side_effect = WooCommerceError("gone", status_code=404)
		self.client.update_product.side_effect = lambda pid, payload: {"id": int(pid)}
		with patch("frappe.enqueue"):
			tasks._run_reconcile("retail")
		mine = [c for c in self.client.update_product.call_args_list if c.args[0] == str(self.P)]
		self.assertEqual(len(mine), 1)
		self.assertEqual(_meta(mine[0].args[1])["_osa_preorder"], "1")

	def test_a_dead_variation_image_is_dropped_and_the_push_retried(self):
		code = self.v[("SP5", "Navy")]
		url = "https://cdn.example.com/dead-lower.jpg"
		frappe.db.set_value("Item", code, "image", url)
		created = {}

		def create_variation(parent, payload):
			if (payload.get("image") or {}).get("src") == url:
				raise WooCommerceError(f"Error getting remote image {url}. Error: 404", status_code=400)
			created[payload["sku"]] = payload
			return {"id": next(self.next_id)}

		self.client.create_variation.side_effect = create_variation
		result = self._push_all()
		self.assertNotIn("image", created[code])
		self.assertTrue(self._pid(code))
		self.assertFalse(frappe.db.get_value("Item", code, "image"))
		self.assertEqual(result["variation_errors"], {})

	def test_a_failing_variation_is_that_variants_error_not_the_templates(self):
		bad = self.v[("PTR", "SEF")]

		def create_variation(parent, payload):
			if payload["sku"] == bad:
				raise WooCommerceError("refused", status_code=400)
			return {"id": next(self.next_id)}

		self.client.create_variation.side_effect = create_variation
		result = self._push_all()
		self.assertEqual(frappe.db.get_value("Item", self.template, "woo_sync_error"), "")
		self.assertEqual(self._pid(self.template), str(self.P))
		self.assertEqual(list(result["variation_errors"]), [bad])
		self.assertIn("refused", frappe.db.get_value("Item", bad, "woo_sync_error"))

	def test_a_deleted_variation_reaches_the_button_as_dangling(self):
		from ffl_woo_sync.woocommerce.client_api import push_item_now

		self._push_all()
		gone = self.v[("SP5", "Navy")]

		def update_variation(parent, vid, payload):
			if vid == self._pid(gone):
				raise WooCommerceError("no such variation", status_code=404)
			return {"id": int(vid)}

		self.client.update_variation.side_effect = update_variation
		out = push_item_now(self.template)
		self.assertEqual((out["ok"], out["dangling_variations"], out["variation_errors"]),
			(True, [gone], {}))

	def test_make_variants_ignores_gaps_among_existing_variants(self):
		# Two existing variants missing a value would share the tuple (x, "").
		for code in list(self.v.values())[:2]:
			frappe.db.delete("Item Variant Attribute", {"parent": code, "attribute": self.sel})
		token = frappe.generate_hash(length=5).upper()
		new = f"_T-PLAIN-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": new, "item_name": new,
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1}).insert(
			ignore_permissions=True)
		out = variants.make_variants(self.template, {new: {self.fits: "SP5K", self.sel: "Navy"}})
		self.assertEqual(out["errors"], [])
		# A requested Item repeating a complete existing combination is still refused.
		clash = variants.make_variants(self.template, {new: {self.fits: "SP5", self.sel: "SEF"}})
		self.assertEqual(clash["errors"], [f"{new}: same attributes as {self.v[('SP5', 'SEF')]}"])
