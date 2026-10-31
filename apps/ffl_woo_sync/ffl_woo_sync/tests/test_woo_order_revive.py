"""A Woo order cancelled here on a 'failed' / 'trash' status is reopened when
WooCommerce moves it back to a placed status (the shopper retried payment on the
same order, or staff restored it from the trash).

Before: _handle_order_cancelled released the guns and wrote Cancelled, and
nothing ever wrote the status back — the paid order never reached the Pending
Order queue and its gun stayed for sale at the counter.

Real Woo Online Order, real Serial Nos, real reservation writes; only the
outbound pushes and the Error Log are silenced.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.tests.test_woo_dealer_serial_precedence import (
	ITEM,
	_ensure_items,
	_make_order,
	_reservation_of,
)

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"


class TestReopenRevivedOrder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_items()

	def setUp(self):
		super().setUp()
		stack = ExitStack()
		for target in ("_enqueue_serial_or_item_push", "_push_item_stock_everywhere",
				"_announce_commitments", "_log_error"):
			stack.enter_context(patch(f"{WEBHOOK}.{target}"))
		self.addCleanup(stack.close)

	def _serial(self, *, reserved_for=""):
		name = f"QAR-{frappe.generate_hash(length=8).upper()}"
		doc = frappe.get_doc({"doctype": "Serial No", "serial_no": name, "item_code": ITEM})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.set_value("Serial No", name, {
			"status": "Active", "woo_reserved": 1 if reserved_for else 0,
			"woo_order": reserved_for}, update_modified=False)
		return name

	def _cancelled(self, serials):
		order = _make_order(serials=serials, order_status="Cancelled")
		frappe.db.set_value("Woo Online Order", order.name, "woo_status", "failed")
		order.reload()
		return order

	def test_a_retried_payment_reopens_the_order_and_re_reserves_its_gun(self):
		from ffl_woo_sync.woocommerce.webhook import _reopen_if_revived
		gun = self._serial()
		order = self._cancelled([{"confirmed_serial": gun}])
		self.assertTrue(_reopen_if_revived(order, {"status": "processing"}))
		self.assertEqual(order.order_status, "Pending Fulfillment")
		self.assertEqual(_reservation_of(gun), {"woo_reserved": 1, "woo_order": order.name})
		self.assertFalse(order.serials[0].reservation_error)

	def test_a_placeholder_gone_elsewhere_becomes_a_claim(self):
		from ffl_woo_sync.woocommerce.webhook import _reopen_if_revived
		other = _make_order(serials=[{}])
		gun = self._serial(reserved_for=other.name)
		order = self._cancelled([{"stock_hold_serial": gun}])
		self.assertTrue(_reopen_if_revived(order, {"status": "processing"}))
		self.assertEqual(order.serials[0].stock_hold_serial, "")
		self.assertEqual(_reservation_of(gun)["woo_order"], other.name)

	def test_a_confirmed_gun_gone_elsewhere_is_reported_on_the_row(self):
		from ffl_woo_sync.woocommerce.webhook import _reopen_if_revived
		other = _make_order(serials=[{}])
		gun = self._serial(reserved_for=other.name)
		order = self._cancelled([{"confirmed_serial": gun}])
		self.assertTrue(_reopen_if_revived(order, {"status": "processing"}))
		self.assertIn(gun, order.serials[0].reservation_error)
		self.assertEqual(_reservation_of(gun)["woo_order"], other.name)

	def test_a_still_dead_status_leaves_it_cancelled(self):
		from ffl_woo_sync.woocommerce.webhook import _reopen_if_revived
		gun = self._serial()
		order = self._cancelled([{"confirmed_serial": gun}])
		for status in ("failed", "cancelled", "refunded", "checkout-draft"):
			self.assertFalse(_reopen_if_revived(order, {"status": status}))
		self.assertEqual(order.order_status, "Cancelled")
		self.assertEqual(_reservation_of(gun)["woo_reserved"], 0)

	def test_a_live_order_is_left_alone(self):
		from ffl_woo_sync.woocommerce.webhook import _reopen_if_revived
		order = _make_order(serials=[{}])
		self.assertFalse(_reopen_if_revived(order, {"status": "processing"}))
