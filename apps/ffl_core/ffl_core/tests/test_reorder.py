from unittest import TestCase
from unittest.mock import patch

import frappe

from ffl_core import reorder


class TestReorder(TestCase):
	def test_setup_uses_pricing_model_not_serial_tracking(self):
		item = frappe._dict(is_stock_item=1, item_group="Pistol", has_serial_no=1, uniform_sell_price=0)
		self.assertFalse(reorder._eligible(item))
		item.uniform_sell_price = 1
		self.assertTrue(reorder._eligible(item))

	def test_manual_and_per_serial_only_requests_do_not_alert(self):
		request = frappe._dict(
			doctype="Material Request", name="demo-request",
			items=[frappe._dict(item_code="demo-item")],
		)
		per_serial = frappe._dict(is_firearm=1, uniform_sell_price=0)
		with patch.object(reorder, "notify_staff") as notify:
			reorder.notify_managers(request)
			notify.assert_not_called()
			request.auto_created_via_reorder = 1
			with patch.object(frappe, "get_all", return_value=[per_serial]):
				reorder.notify_managers(request)
			notify.assert_not_called()

	def test_uniform_pricing_request_links_to_request_with_permission_check(self):
		request = frappe._dict(
			doctype="Material Request", name="demo-request", auto_created_via_reorder=1,
			items=[frappe._dict(item_code="demo-item")],
		)
		uniform = frappe._dict(is_firearm=1, uniform_sell_price=1)
		with (
			patch.object(frappe, "get_all", return_value=[uniform]),
			patch.object(reorder, "_", side_effect=str),
			patch.object(reorder, "notify_staff") as notify,
		):
			reorder.notify_managers(request)
		self.assertEqual(notify.call_args.args[:2], ("Material Request", request.name))
		self.assertEqual(set(notify.call_args.args[3]), {"Stock Manager", "Purchase Manager"})
		self.assertTrue(notify.call_args.kwargs["require_read"])
