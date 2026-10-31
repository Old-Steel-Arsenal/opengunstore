"""A GunBroker release undoes only the delist the GunBroker exit made.

`serial_channel_return` used to push with `relist=True` unconditionally, which
clears `{site}_delisted` — so a gun the owner had hidden from the website before
GunBroker took it came back online when GunBroker let it go. The exit now marks
`{site}_channel_delisted` when it is the one hiding the gun; the return relists
only those.

Real Serial No rows; only `frappe.enqueue` is captured (the jobs would call
WooCommerce).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import channel

ITEM = "TEST-CHAN-RETURN-ITEM"
SERIAL = "TEST-CHAN-RETURN-SN"


class AGunBrokerReleaseRelistsOnlyItsOwnDelist(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.sql("DELETE FROM `tabSerial No` WHERE name=%s", (SERIAL,))
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Rifle", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1,
			}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Serial No", "serial_no": SERIAL, "item_code": ITEM,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", SERIAL, "woo_product_id", "9911")
		self.addCleanup(frappe.db.rollback)

	def _exit_then_return(self):
		with patch("frappe.enqueue") as enq:
			channel.serial_channel_exit(SERIAL, channel="gunbroker", reason="sold")
			# The delist job that the exit queued, run for real minus the HTTP.
			frappe.db.set_value("Serial No", SERIAL, "woo_delisted", 1)
			enq.reset_mock()
			channel.serial_channel_return(SERIAL, channel="gunbroker", reason="released")
		pushes = [c.kwargs for c in enq.call_args_list
			if c.args[0] == "ffl_woo_sync.woocommerce.tasks.push_serial_task"]
		self.assertEqual(len(pushes), 1)
		return pushes[0]

	def test_a_gun_the_gunbroker_exit_hid_is_relisted(self):
		push = self._exit_then_return()
		self.assertTrue(push["relist"])
		self.assertEqual(
			frappe.db.get_value("Serial No", SERIAL, "woo_channel_delisted"), 1)

	def test_a_gun_the_owner_hid_beforehand_stays_hidden(self):
		frappe.db.set_value("Serial No", SERIAL, "woo_delisted", 1)
		push = self._exit_then_return()
		self.assertFalse(push["relist"])
		self.assertFalse(
			frappe.db.get_value("Serial No", SERIAL, "woo_channel_delisted"))

	def test_a_re_polled_order_keeps_the_mark(self):
		"""The second exit finds the gun already delisted — by its own first exit."""
		with patch("frappe.enqueue"):
			channel.serial_channel_exit(SERIAL, channel="gunbroker", reason="reserved")
			frappe.db.set_value("Serial No", SERIAL, "woo_delisted", 1)
			channel.serial_channel_exit(SERIAL, channel="gunbroker", reason="sold")
		self.assertEqual(
			frappe.db.get_value("Serial No", SERIAL, "woo_channel_delisted"), 1)
