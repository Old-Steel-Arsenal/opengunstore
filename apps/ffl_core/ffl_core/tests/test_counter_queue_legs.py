"""Pending Order queue, per-leg behaviour. Three things:

1. A queue row carries a ``legs`` payload (local undelivered + each DO's status);
2. **The leave-the-queue rule is per leg**: an order with a non-terminal DO no
   longer leaves the queue just because it is "paid in full + nothing local
   pending" (fix: a pure drop-ship order dropped out of the queue once paid
   while its DO sat in Draft with nobody confirming it);
3. ``release_order_legs``: once paid in full, confirm every Draft DO in one go,
   each leg isolated (one failing leg does not drag the others; idempotent,
   safe to retry).

Reuses the fixture chain of test_counter_mixed_order.MixedOrderFixtures."""
from __future__ import annotations

from unittest import mock

import frappe

from ffl_core.api import manual_order
from ffl_core.tests.test_counter_mixed_order import (
	LOCAL_ACC, MixedOrderFixtures, p4b)


def _queue_row(si_name):
	for row in manual_order.list_pending_dispositions():
		if row.get("sales_invoice") == si_name:
			return row
	return None


class QueueLegsTest(MixedOrderFixtures):
	"""Inherits the fixtures and _mixed_counter (without the MixedOrderOrchestration test cases, so they are not re-run)."""

	def test_queue_row_carries_legs(self):
		out = self._mixed_counter()
		row = _queue_row(out["sales_invoice"])
		self.assertIsNotNone(row)
		legs = row["legs"]
		self.assertEqual(legs["local"]["undelivered"], 1)
		self.assertEqual(len(legs["distributor_orders"]), 1)
		self.assertEqual(legs["distributor_orders"][0]["status"], "Draft")
		# is_pos paid-in-full order -> outstanding 0 -> the Draft DO can be released
		self.assertTrue(legs["releasable"])

	def test_unsettled_do_keeps_pure_dropship_order_in_queue(self):
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Ann Individual"),
			"pos_profile": self._pos_profile(),
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": "Cash",
			"contact_email": "legs@example.com", "ship_to": dict(self._SHIP_TO),
			"items": [{"item_code": p4b.ACC_ITEM, "qty": 1, "rate": 40,
				"serials": [], "source": "dropship"}],
		})
		row = _queue_row(out["sales_invoice"])
		self.assertIsNotNone(row)   # before the fix: paid + nothing local pending -> left the queue, nobody managed the DO
		self.assertEqual(row["legs"]["local"]["undelivered"], 0)

	def test_release_order_legs_confirms_all_draft_dos(self):
		out = self._mixed_counter()
		si_name = out["sales_invoice"]
		result = manual_order.release_order_legs(si_name)
		self.assertEqual(len(result["released"]), 1)
		self.assertEqual(result["failed"], [])
		do_name = result["released"][0]
		self.assertEqual(
			frappe.db.get_value("Distributor Order", do_name, "status"), "Queued")

	def test_release_refuses_unpaid(self):
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Ann Individual"),
			"pos_profile": self._pos_profile(),
			"dispose_now": 0, "payment_received": 0, "mode_of_payment": "Cash",
			"contact_email": "legs2@example.com", "ship_to": dict(self._SHIP_TO),
			"items": [{"item_code": p4b.ACC_ITEM, "qty": 1, "rate": 40,
				"serials": [], "source": "dropship"}],
		})
		# The rule is the leg's (confirm_order): a drop-ship leg refuses while the
		# invoice owes — reported per leg, the leg stays Draft.
		result = manual_order.release_order_legs(out["sales_invoice"])
		self.assertEqual(result["released"], [])
		self.assertEqual(len(result["failed"]), 1)
		self.assertIn("not paid in full", result["failed"][0]["error"])
		self.assertEqual(frappe.db.get_value(
			"Distributor Order", result["failed"][0]["name"], "status"), "Draft")
		row = _queue_row(out["sales_invoice"])
		self.assertFalse(row["legs"]["releasable"])
		self.assertIn("Unpaid", manual_order.blocked_reason(row))

	def test_release_partial_failure_isolates_legs(self):
		out = self._mixed_counter()
		si_name = out["sales_invoice"]
		from ffl_integrations.distributor import api as dist_api
		real_confirm = dist_api._confirm_order
		calls = []

		def flaky_confirm(do_name):
			calls.append(do_name)
			if len(calls) == 1:
				raise RuntimeError("worker hiccup")
			return real_confirm(do_name)

		with mock.patch.object(dist_api, "_confirm_order", side_effect=flaky_confirm):
			result = manual_order.release_order_legs(si_name)
		self.assertEqual(len(result["failed"]), 1)
		self.assertEqual(result["released"], [])
		# idempotent retry: the failed leg is still Draft; releasing again succeeds
		result2 = manual_order.release_order_legs(si_name)
		self.assertEqual(len(result2["released"]), 1)
		self.assertEqual(result2["failed"], [])
