"""A gun held for a CRM deal on the website (ffl_core.crm.holds → gunshop_serial_hold_changed): the
listing reconcile keeps its page at stock 0 while held, as for a parked POS sale, and puts it back on
sale when the hold ends; the two-hourly reconcile reads the hold as stock 0. Real holds through the
real endpoints; only the calls to WooCommerce are stubbed."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

from ffl_core.api import crm as api
from ffl_core.crm import holds
from ffl_core.tests.crm_support import CLERK
from ffl_core.tests.deal_hold_support import DealHoldCase


class TestDealHoldListing(DealHoldCase):
	def _listed_hold(self):
		serial = self._gun()
		frappe.db.set_value("Serial No", serial, "woo_product_id", "88" + serial[-6:], update_modified=False)
		deal = self._deal(serial)
		return serial, self._held(deal, serial)

	def test_the_hook_is_registered_and_queues_the_listing_reconcile(self):
		self.assertIn("ffl_woo_sync.woocommerce.doc_events.on_serial_hold_changed",
			frappe.get_hooks(holds.HOOK_CHANGED))
		with patch("frappe.enqueue") as enqueue:
			serial, _name = self._listed_hold()
		calls = [c for c in enqueue.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0]) and c.kwargs.get("serial_no") == serial]
		self.assertEqual(len(calls), 1)
		self.assertTrue(calls[0].kwargs.get("enqueue_after_commit"))

	def test_held_keeps_the_page_at_stock_zero_then_comes_back(self):
		from ffl_woo_sync.woocommerce import tasks

		serial, name = self._listed_hold()
		with patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
				patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
				patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as archive:
			tasks.sync_serial_listing_task(serial, site="retail")
		push.assert_called_once_with(serial, site="retail", pos_fields_only=True)
		delist.assert_not_called()
		archive.assert_not_called()

		frappe.set_user(CLERK)
		try:
			api.release_hold(name)
		finally:
			frappe.set_user("Administrator")
		with patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
				patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist:
			tasks.sync_serial_listing_task(serial, site="retail")
		push.assert_called_once_with(serial, site="retail", _relist=True, pos_fields_only=True)
		delist.assert_not_called()

	def test_the_push_publishes_stock_zero_for_a_held_gun(self):
		from ffl_woo_sync.woocommerce import mapping

		serial, _name = self._listed_hold()
		payload = mapping.serial_to_product_payload(serial)
		self.assertEqual(int(payload.get("stock_quantity") or 0), 0, payload)

	def test_reconcile_reads_a_hold_as_stock_zero(self):
		"""A push that failed left the store at stock 1: the sweep must see that as drift and push."""
		from ffl_woo_sync.woocommerce import tasks

		serial, _name = self._listed_hold()
		pid = frappe.db.get_value("Serial No", serial, "woo_product_id")
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		live = {"id": pid, "status": "publish", "catalog_visibility": "visible", "stock_quantity": 1,
			"regular_price": "700.00", "sale_price": "", "meta_data": []}
		client.get_products.return_value = {str(pid): live}
		client.get_product.return_value = live
		real_get_all = frappe.get_all

		def only_this_gun(doctype, *args, **kwargs):
			rows = real_get_all(doctype, *args, **kwargs)
			if doctype == "Serial No" and (kwargs.get("filters") or {}).get("status") == "Active":
				return [r for r in rows if r.name == serial]
			if doctype in ("Item", "Serial No") and "filters" in kwargs and "woo_product_id" in str(kwargs["filters"]):
				return [r for r in rows if r.get("name") == serial]
			return rows

		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings", return_value=client), \
				patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=700), \
				patch("ffl_woo_sync.woocommerce.tasks._map_meta_drifted", return_value=False), \
				patch("ffl_woo_sync.woocommerce.tasks._wc_sale_drifted", return_value=False), \
				patch("frappe.get_all", side_effect=only_this_gun), \
				patch("ffl_woo_sync.woocommerce.sync.push_item"), \
				patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
				patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
				patch("frappe.db.set_value"):
			tasks._run_reconcile()
		pushed = [c for c in push.call_args_list if c.args and c.args[0] == serial]
		self.assertEqual(len(pushed), 1, push.call_args_list)
		self.assertTrue(pushed[0].kwargs.get("pos_fields_only"))
		delist.assert_not_called()
