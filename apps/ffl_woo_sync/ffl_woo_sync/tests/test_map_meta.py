"""MAP price control meta — ``_osa_map_price`` / ``_osa_map_mode`` on retail pushes.

The retail storefront plugin (MapPrice) hides a price below the Item's MAP by
the brand's mode. The POS owns both keys: they ride every retail push (empty when
there is no MAP, so taking a MAP off reaches the store), never a non-retail store's
 and lean pushes keep them; reconcile heals a lost
re-push. The mode comes from the ``map_mode_for_item`` hook (ffl_integrations' brand table, exercised for real in
its test_dropship_pricing); a site without a registrant is "MAP is minimum price".
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import mapping, sync, tasks
from ffl_woo_sync.woocommerce.doc_events import on_item_update

_MOD = "ffl_woo_sync.tests.test_map_meta"
_REAL_GET_HOOKS = frappe.get_hooks


def email_mode(item_code, brand):
	return "Email for price" if brand == "Glock" else None


def _hooks(*paths, hook="map_mode_for_item"):
	def fake(name=None, *args, **kwargs):
		if name == hook:
			return [f"{_MOD}.{p}" for p in paths]
		return _REAL_GET_HOOKS(name, *args, **kwargs)
	return patch.object(frappe, "get_hooks", side_effect=fake)


def _meta(payload):
	return {r["key"]: r["value"] for r in payload.get("meta_data") or []}


class RetailPayload(FrappeTestCase):
	def _payload(self, **item):
		base = {"meta_data": [{"key": "_osa_map_price", "value": "stale"}, {"key": "_x", "value": "1"}]}
		return mapping._with_map_meta(base, frappe._dict(item))

	def test_a_map_and_the_brands_mode_ride_the_retail_push(self):
		with _hooks("email_mode"):
			meta = _meta(self._payload(map_price=599, brand="Glock"))
		self.assertEqual((meta["_osa_map_price"], meta["_osa_map_mode"], meta["_x"]),
			("599.00", "Email for price", "1"))

	def test_no_registrant_or_no_answer_is_map_is_minimum_price(self):
		with _hooks():
			self.assertEqual(_meta(self._payload(map_price=10, brand="Glock"))["_osa_map_mode"],
				"MAP is minimum price")
		with _hooks("email_mode"):
			self.assertEqual(_meta(self._payload(map_price=10, manufacturer="Ruger"))["_osa_map_mode"],
				"MAP is minimum price")

	def test_no_map_clears_both_keys(self):
		with _hooks("email_mode"):
			meta = _meta(self._payload(map_price=0, brand="Glock"))
		self.assertEqual((meta["_osa_map_price"], meta["_osa_map_mode"]), ("", ""))

	def test_a_used_gun_carries_no_new_product_map(self):
		with _hooks("email_mode"):
			meta = _meta(self._payload(map_price=599, brand="Glock", is_used=1))
		self.assertEqual((meta["_osa_map_price"], meta["_osa_map_mode"]), ("", ""))

	def test_a_lean_push_keeps_them(self):
		with _hooks("email_mode"):
			lean = sync._pos_owned_payload(self._payload(map_price=599, brand="Glock"))
		self.assertEqual(set(_meta(lean)), set(mapping.MAP_META_KEYS))

	def test_both_product_shapes_carry_them(self):
		"""By source: the item-level and the per-serial builder."""
		for fn in (mapping.item_to_product_payload, mapping.serial_to_product_payload):
			tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
			calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
			self.assertIn("_with_map_meta", calls, fn.__name__)


def _item(changed, *, woo_product_id="", firearm=True, uniform=0, new=False):
	doc = MagicMock()
	doc.name = "ITEM-MAP"
	doc.woo_product_id = woo_product_id
	doc.has_value_changed.side_effect = lambda f: f in changed
	doc.get_doc_before_save.return_value = None if new else MagicMock()
	doc.get.side_effect = lambda f, *a: {"uniform_sell_price": uniform}.get(f)
	return doc, patch("ffl_woo_sync.woocommerce.doc_events._is_firearm_row",
		return_value=firearm)


class ItemSaves(FrappeTestCase):
	def _run(self, doc, firearm_patch):
		with firearm_patch, patch("frappe.enqueue") as enqueue:
			on_item_update(doc)
		return [(c.args[0], c.kwargs) for c in enqueue.call_args_list]

	def test_a_listed_items_map_or_brand_edit_pushes_it(self):
		for field in ("map_price", "brand"):
			calls = self._run(*_item((field,), woo_product_id="WC-1"))
			self.assertEqual([c[0] for c in calls], ["ffl_woo_sync.woocommerce.tasks.push_item_task"])

	def test_a_per_serial_firearms_map_edit_pushes_its_listed_guns(self):
		for field in ("map_price", "manufacturer", "is_used"):
			with patch.object(frappe.db, "exists", return_value=True):
				calls = self._run(*_item((field,)))
			self.assertEqual(calls[0][0], "ffl_woo_sync.woocommerce.tasks.push_map_task")
			self.assertEqual(calls[0][1]["item_codes"], ["ITEM-MAP"])
		with patch.object(frappe.db, "exists", return_value=False):
			self.assertEqual(self._run(*_item(("map_price",))), [], "no gun of it is listed")

	def test_nothing_for_an_unlisted_non_serial_item_or_a_new_one(self):
		self.assertEqual(self._run(*_item(("map_price",), firearm=False)), [])
		self.assertEqual(self._run(*_item(("map_price",), uniform=1)), [])
		self.assertEqual(self._run(*_item(("map_price",), new=True)), [])


class BrandRepush(FrappeTestCase):
	def test_the_hook_queues_batches(self):
		with patch("frappe.enqueue") as enqueue:
			tasks.map_meta_changed([f"I{n:03}" for n in range(250)] + ["I000"])
		batches = [c.kwargs["item_codes"] for c in enqueue.call_args_list]
		self.assertEqual([len(b) for b in batches], [100, 100, 50])

	def test_listed_items_and_active_listed_guns_are_pushed(self):
		def get_all(doctype, **kw):
			if doctype == "Item":
				return [frappe._dict(name="I-1", is_firearm=0, item_group="", uniform_sell_price=0),
					frappe._dict(name="PER-SERIAL", is_firearm=1, item_group="", uniform_sell_price=0)]
			return ["SN-1"]
		with patch.object(tasks.frappe, "get_all", side_effect=get_all) as ga, \
				patch.object(tasks, "push_items_task") as items, \
				patch.object(tasks, "push_serial_task") as serials:
			tasks.push_map_task(["I-1", "I-2"])
		self.assertEqual(items.call_args.args, (["I-1"],), "a per-serial Item's product is each gun's")
		self.assertEqual(ga.call_args_list[0].kwargs["filters"]["woo_listed"], 1)
		self.assertIn("MAP", items.call_args.kwargs["remedy"])
		serials.assert_called_once_with("SN-1")
		serial_filters = ga.call_args_list[1].kwargs["filters"]
		self.assertEqual((serial_filters["status"], serial_filters["woo_product_id"]),
			("Active", ["is", "set"]))


class ReconcileHealsIt(FrappeTestCase):
	"""A lost MAP / brand-mode re-push (store down, a deduplicated job) is healed by
	the reconcile sweep comparing the store's meta with mapping.map_meta."""

	def test_drift_is_any_difference_and_absent_reads_as_empty(self):
		item = frappe._dict(name="I-1", map_price=599, brand="Glock")
		wc = lambda **m: {"meta_data": [{"key": k, "value": v} for k, v in m.items()]}
		with _hooks("email_mode"):
			self.assertFalse(tasks._map_meta_drifted(
				wc(_osa_map_price="599.00", _osa_map_mode="Email for price"), item))
			self.assertTrue(tasks._map_meta_drifted(
				wc(_osa_map_price="599.00", _osa_map_mode="Show in cart"), item))
			self.assertTrue(tasks._map_meta_drifted(wc(), item))
			self.assertFalse(tasks._map_meta_drifted(wc(), frappe._dict(name="I-2", map_price=0)))

	def test_both_passes_compare_it(self):
		source = inspect.getsource(tasks._run_reconcile)
		self.assertEqual(source.count("_map_meta_drifted("), 2)
