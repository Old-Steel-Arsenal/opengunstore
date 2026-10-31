"""Units a web order is waiting on are not the counter's to sell (compliance.web_hold)."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import web_hold

ITEM = "WEB-HOLD-ACC"


def _invoice(qty, **kw):
	doc = frappe._dict(doctype="POS Invoice", is_return=0, is_consolidated=0,
		woo_online_order="", update_stock=1,
		items=[frappe._dict(item_code=ITEM, qty=qty, stock_qty=qty)], **kw)
	doc.add_comment = mock.Mock()
	return doc


def _bootstrap():
	from ffl_core.setup import test_bootstrap

	test_bootstrap.run()
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": "Misc Accessory",
			"stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)


class WebHold(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()

	def setUp(self):
		for patcher in (mock.patch.object(web_hold, "_free_on_shelf", return_value=1),
				mock.patch.object(web_hold, "_held_by", return_value=["6120"]),
				mock.patch.object(frappe, "get_roles", return_value=["Sales User"])):
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_the_free_shelf_sells(self):
		web_hold.refuse_held_units(_invoice(1))

	def test_a_held_unit_is_refused_with_the_order_named(self):
		with self.assertRaisesRegex(frappe.ValidationError, "#6120"):
			web_hold.refuse_held_units(_invoice(2))

	def test_a_manager_rings_it_and_the_invoice_says_so(self):
		doc = _invoice(2)
		with mock.patch.object(frappe, "get_roles", return_value=["Sales Manager"]):
			web_hold.refuse_held_units(doc)
		self.assertIn("#6120", doc.add_comment.call_args[0][1])

	def test_not_a_counter_sale(self):
		for kw in ({"is_return": 1}, {"is_consolidated": 1}, {"woo_online_order": "WOO-1"}):
			web_hold.refuse_held_units(frappe._dict(_invoice(5), **kw))
		web_hold.refuse_held_units(frappe._dict(_invoice(5), doctype="Sales Invoice", update_stock=0))

	def test_no_webshop_holds_nothing(self):
		with mock.patch.object(web_hold, "_free_on_shelf", return_value=None):
			web_hold.refuse_held_units(_invoice(9))


def _storefront_shelf(item_code):
	"""The webshop's shelf for ITEM: Bin 3 less 2 held for a web order."""
	return {"qty": 1}


class UnmergedCounterSales(FrappeTestCase):
	"""Today's counter sales leave the Bin only at the nightly close; the gate counts them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()

	def setUp(self):
		frappe.db.delete("POS Invoice Item", {"item_code": ITEM})
		frappe.db.delete("Packed Item", {"item_code": ITEM, "parenttype": "POS Invoice"})
		get_hooks = frappe.get_hooks
		hook = f"{__name__}._storefront_shelf"
		for patcher in (
				mock.patch.object(frappe, "get_hooks", side_effect=lambda name=None, *a, **kw:
					[hook] if name == web_hold.STORE_STOCK_HOOK else get_hooks(name, *a, **kw)),
				mock.patch.object(web_hold, "_held_by", return_value=["6120"]),
				mock.patch.object(frappe, "get_roles", return_value=["Sales User"])):
			patcher.start()
			self.addCleanup(patcher.stop)

	def _pos_sale(self, qty, **fields):
		"""A submitted POS Invoice line not merged yet, written straight to the tables
		(as test_inventory_count does): a real sale needs a shift and payments."""
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"INVC-POS-{frappe.generate_hash(length=8)}", "docstatus": 1, **fields})
		inv.db_insert()
		line = frappe.new_doc("POS Invoice Item")
		line.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice",
			"parentfield": "items", "idx": 1, "docstatus": 1, "item_code": ITEM, "qty": qty, "stock_qty": qty})
		line.db_insert()

	def test_a_second_counter_sale_cannot_take_the_held_unit(self):
		web_hold.refuse_held_units(_invoice(1))
		self._pos_sale(1)
		with self.assertRaisesRegex(frappe.ValidationError, "#6120"):
			web_hold.refuse_held_units(_invoice(1))

	def test_merged_and_web_pickup_tickets_do_not_count_a_return_puts_its_unit_back(self):
		self._pos_sale(1, consolidated_invoice="SINV-X")
		self._pos_sale(1, woo_online_order="WOO-1")
		self._pos_sale(1)
		self._pos_sale(-1, is_return=1)
		web_hold.refuse_held_units(_invoice(1))

	def test_a_bundle_component_sold_at_the_counter_counts(self):
		"""A bundle's component leaves the Bin at the close too (Packed Item)."""
		web_hold.refuse_held_units(_invoice(1))
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"INVC-POS-{frappe.generate_hash(length=8)}", "docstatus": 1})
		inv.db_insert()
		packed = frappe.new_doc("Packed Item")
		packed.update({"name": frappe.generate_hash(length=10), "parent": inv.name,
			"parenttype": "POS Invoice", "parentfield": "packed_items", "idx": 1, "docstatus": 1,
			"parent_item": "WEB-HOLD-KIT", "item_code": ITEM, "qty": 1})
		packed.db_insert()
		with self.assertRaisesRegex(frappe.ValidationError, "#6120"):
			web_hold.refuse_held_units(_invoice(1))
