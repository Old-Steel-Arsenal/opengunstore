"""Tests for the WooCommerce refund wiring in webhook.py (D1).

- ``order.updated`` on a completed order carrying a NEW refund records it and
  enqueues the SI reconcile (after commit); a re-delivered same-refund webhook
  enqueues nothing (idempotent);
- a terminal status (cancelled / trash / fully refunded) that lands on an order
  whose sale was already booked raises an alert — never silent, never
  auto-reversed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _updated_order():
	o = MagicMock()
	o.name = "WOO-ORD-00007"
	o.woo_order_id = "9002"
	o.order_status = "Transferred"
	o.woo_status = "completed"
	o.lines = []
	o.serials = []
	o.refunds = []
	return o


class TestOrderUpdatedRefundSync(FrappeTestCase):

	def _run(self, order, body, woo_refunds=()):
		# Woo's HTTP is the only thing faked for the breakdown read.
		from ffl_woo_sync.tests.test_refunds import woo_refunds_http
		# now_datetime() (used by record_order_refunds for recorded_on) would load
		# System Settings through the patched get_doc and try to cache a MagicMock —
		# pin it to a constant to keep the double self-contained.
		with patch("frappe.db.get_value", return_value="WOO-ORD-00007"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch("ffl_woo_sync.woocommerce.refunds.now_datetime",
				return_value="2026-07-12 12:00:00"), \
			 patch("frappe.enqueue") as enq, \
			 woo_refunds_http(list(woo_refunds)) as calls:
			from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
			_handle_order_updated(body)
		self.woo_calls = calls  # the webhook must not have called Woo at all
		return enq

	def test_new_refund_recorded_and_reconcile_enqueued(self):
		body = {"id": 9002, "status": "completed", "line_items": [],
			"refunds": [{"id": 9003, "total": "-9.99"}]}
		order = _updated_order()
		enq = self._run(order, body)
		reconcile = [c for c in enq.call_args_list if "_reconcile" in c.args[0]]
		self.assertEqual(len(reconcile), 1)
		self.assertEqual(reconcile[0].kwargs.get("order"), "WOO-ORD-00007")
		self.assertTrue(reconcile[0].kwargs.get("enqueue_after_commit"))
		# r2 #6: one job per order (redeliveries while it waits add nothing)
		self.assertEqual(reconcile[0].kwargs.get("job_id"), "refund-reconcile-WOO-ORD-00007")
		self.assertTrue(reconcile[0].kwargs.get("deduplicate"))
		order.save.assert_called_once()

	def test_a_new_refund_is_noted_with_its_own_amount(self):
		body = {"id": 9002, "status": "completed", "line_items": [],
			"refunds": [{"id": 9003, "total": "-9.99"}]}
		order = _updated_order()
		order.append = lambda table, row: order.refunds.append(frappe._dict(row))
		with patch("ffl_woo_sync.woocommerce.webhook.note_web_order") as note:
			self._run(order, body)
		note.assert_called_once_with("WOO-ORD-00007", "refunded", key="9003", amount=9.99)

	def test_redelivered_same_refund_enqueues_nothing(self):
		body = {"id": 9002, "status": "completed", "line_items": [],
			"refunds": [{"id": 9003, "total": "-9.99"}]}
		order = _updated_order()
		order.refunds = [frappe._dict(woo_refund_id="9003", refund_amount=9.99)]
		enq = self._run(order, body)
		reconcile = [c for c in enq.call_args_list if "_reconcile" in c.args[0]]
		self.assertEqual(reconcile, [])

	def test_new_refund_is_recorded_pending_without_calling_woo(self):
		"""r1 #1: the breakdown read (REST, up to 30s × retries) never runs in the
		webhook request — the row is Pending and the long-queue job reads it."""
		from ffl_woo_sync.tests.test_refunds import SHIPPING_REFUND
		body = {"id": 9002, "status": "completed", "line_items": [],
			"refunds": [{"id": 7001, "total": "-45.00"}]}
		order = _updated_order()
		order.append = lambda table, row: order.refunds.append(frappe._dict(row)) \
			or order.refunds[-1]
		enq = self._run(order, body, woo_refunds=[SHIPPING_REFUND])
		self.assertEqual(self.woo_calls, [])
		self.assertEqual(order.refunds[0].breakdown_status, "Pending")
		self.assertEqual(len([c for c in enq.call_args_list if "_reconcile" in c.args[0]]), 1)

	def test_a_redelivery_with_a_pending_breakdown_re_enqueues_the_reconcile(self):
		"""The refund was recorded while its breakdown could not be read; the next
		order.updated hands it to the reconcile job again."""
		body = {"id": 9002, "status": "completed", "line_items": [],
			"refunds": [{"id": 7001, "total": "-45.00"}]}
		order = _updated_order()
		order.refunds = [frappe._dict(woo_refund_id="7001", refund_amount=45.0,
			breakdown_status="Pending")]
		enq = self._run(order, body)
		self.assertEqual(self.woo_calls, [])
		self.assertEqual(len(order.refunds), 1)  # nothing new recorded
		self.assertEqual(len([c for c in enq.call_args_list if "_reconcile" in c.args[0]]), 1)

	def test_order_without_refunds_enqueues_no_reconcile(self):
		body = {"id": 9002, "status": "completed", "line_items": []}
		order = _updated_order()
		enq = self._run(order, body)
		reconcile = [c for c in enq.call_args_list if "_reconcile" in c.args[0]]
		self.assertEqual(reconcile, [])


class TestOrderCancelledAlert(FrappeTestCase):

	def _run(self, invoice_name):
		def _gv(doctype, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00007", "site": "retail"}
			if doctype == "Sales Invoice":
				return invoice_name
			return None

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.log_error") as log:
			from ffl_woo_sync.woocommerce.webhook import _handle_order_cancelled
			_handle_order_cancelled({"id": 9002, "status": "cancelled"})
		return log

	def test_only_a_real_cancel_or_refund_is_noted(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_cancelled

		def _gv(doctype, *a, **k):
			return {"name": "WOO-ORD-00007", "site": "retail"} if doctype == "Woo Online Order" else None

		for status, event in (("cancelled", "cancelled"), ("refunded", "refunded"), ("failed", None), ("trash", None)):
			with patch("frappe.db.get_value", side_effect=_gv), patch("frappe.db.set_value"), \
					patch("frappe.get_all", return_value=[]), \
					patch("ffl_woo_sync.woocommerce.webhook.note_web_order") as note:
				_handle_order_cancelled({"id": 9002, "status": status})
			if event:
				note.assert_called_once_with("WOO-ORD-00007", event)
			else:
				note.assert_not_called()

	def test_terminal_status_on_invoiced_order_alerts(self):
		log = self._run(invoice_name="ACC-SINV-49")
		log.assert_called_once()

	def test_terminal_status_without_invoice_is_silent(self):
		log = self._run(invoice_name=None)
		log.assert_not_called()
