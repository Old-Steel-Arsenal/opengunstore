"""Carrying a tracking number back to GunBroker, one step at a time.

The write-back is three calls and each one can fail on its own, so what the next
round sends is decided by which timestamps are already written. Getting that
wrong has two shapes and both are silent:

* re-sending everything every round — harmless to GunBroker, but it hides which
  step is actually failing behind a wall of repeated calls;
* selecting orders on one condition — "no tracking timestamp", say — which
  strands the order whose flag went through and whose tracking did not, because
  it has a timestamp and no longer matches.

The other thing asserted here is the carrier. GunBroker knows three couriers.
An unmapped one still gets its tracking number uploaded, with the ``Carrier``
key OMITTED — not zero, not empty, not a guess — because a wrong code makes
GunBroker render a tracking link that goes nowhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import shipping
from ffl_integrations.gunbroker.client import GunBrokerError
from ffl_integrations.tests import gunbroker_fixtures as fx


class _ShippingCase(FrappeTestCase):
	def setUp(self):
		self.written: dict = {}
		self.alerts: list[str] = []

		def _set_value(doctype, name, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.written.setdefault(name, {}).update(updates)

		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch_object(shipping, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"]))

	def _patch(self, target, *args, **kw):
		patcher = patch(target, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _order(self, **fields):
		defaults = {"name": "GB-ORD-00001", "gb_order_id": "998877",
			"order_status": "Transferred", "sales_invoice": "ACC-SINV-1",
			"shipping_flag_pushed_at": None, "tracking_pushed_at": None,
			"gb_ship_writeback_pending": 0, "tracking_number": "",
			"ship_carrier": ""}
		defaults.update(fields)
		return fx.FakeDoc(**defaults)

	def _push(self, order, *, label=("1Z999", "ups", ""), remote=None):
		client = MagicMock()
		client.get_order.return_value = remote if remote is not None else \
			fx.order_detail(tracking="1Z999", carrier=2, status=5)
		self._patch("frappe.get_doc", return_value=order)
		self._patch_object(shipping, "_fetch_label", return_value=label)
		return shipping.push_tracking(order.name, client=client), client


# ---------------------------------------------------------------------------
# Which step goes out next
# ---------------------------------------------------------------------------

class TestStepsOwed(_ShippingCase):
	def test_a_fresh_shipment_owes_both_writes(self):
		self.assertEqual(shipping.steps_owed(self._order()), ["flag", "shipping"])

	def test_a_flagged_order_owes_only_the_tracking_upload(self):
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00")
		self.assertEqual(shipping.steps_owed(order), ["shipping"])

	def test_a_completed_write_back_owes_nothing(self):
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00",
			tracking_pushed_at="2026-08-27 09:00:05")
		self.assertEqual(shipping.steps_owed(order), [])

	def test_a_read_back_that_just_disagreed_re_reads_rather_than_re_sending(self):
		# Both writes landed; only the confirmation did not agree. Re-sending
		# the upload immediately would hide which of the two is actually wrong.
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00",
			tracking_pushed_at=frappe.utils.now_datetime(),
			gb_ship_writeback_pending=1)
		self.assertEqual(shipping.steps_owed(order), [])

	def test_a_read_back_stuck_for_three_rounds_re_sends_the_upload(self):
		# By then the likeliest explanation is that GunBroker took the 200 and
		# dropped the value. The upload is idempotent, so re-sending is safe.
		stale = frappe.utils.now_datetime() - timedelta(minutes=31)
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00",
			tracking_pushed_at=stale, gb_ship_writeback_pending=1)
		self.assertEqual(shipping.steps_owed(order), ["shipping"])


class TestSelection(_ShippingCase):
	ROWS = [
		frappe._dict({"name": "A", "sales_invoice": "SI-A",
			"shipping_flag_pushed_at": None, "tracking_pushed_at": None,
			"gb_ship_writeback_pending": 0}),
		frappe._dict({"name": "B", "sales_invoice": "SI-B",
			"shipping_flag_pushed_at": "x", "tracking_pushed_at": None,
			"gb_ship_writeback_pending": 0}),
		frappe._dict({"name": "C", "sales_invoice": "SI-C",
			"shipping_flag_pushed_at": "x", "tracking_pushed_at": "y",
			"gb_ship_writeback_pending": 1}),
		frappe._dict({"name": "D", "sales_invoice": "SI-D",
			"shipping_flag_pushed_at": "x", "tracking_pushed_at": "y",
			"gb_ship_writeback_pending": 0}),
	]

	def test_every_unfinished_shape_is_selected(self):
		# Each of these strands an order if the query only looks for one of
		# them: nothing sent, the flag sent but not the tracking, and both sent
		# with the read-back disagreeing.
		with patch("frappe.get_all", return_value=self.ROWS), \
				patch.object(shipping, "_invoices_with_a_shipment",
					return_value={"SI-A", "SI-B", "SI-C", "SI-D"}):
			self.assertEqual(shipping.pending_writeback_orders(), ["A", "B", "C"])

	def test_an_order_with_no_shipment_yet_is_not_selected(self):
		# Transferred says the bound-book exit is booked, not that the parcel
		# has left. Selecting one of these sends OrderShipped — "on its way",
		# no tracking — while the gun is still on the packing bench, and moves
		# the GunBroker order out of Pending Shipment on the strength of it.
		with patch("frappe.get_all", return_value=self.ROWS), \
				patch.object(shipping, "_invoices_with_a_shipment",
					return_value={"SI-C"}):
			self.assertEqual(shipping.pending_writeback_orders(), ["C"])

	def test_the_shipment_lookup_asks_for_the_shipstation_id(self):
		captured = {}

		def _get_all(doctype, **kw):
			captured[doctype] = kw
			return []

		with patch("frappe.get_all", side_effect=_get_all):
			shipping._invoices_with_a_shipment(["SI-A"])
		self.assertEqual(
			captured["Sales Invoice"]["filters"]["shipstation_order_id"],
			("is", "set"))


# ---------------------------------------------------------------------------
# The three calls
# ---------------------------------------------------------------------------

class TestWriteBack(_ShippingCase):
	def test_the_flag_goes_first_and_the_tracking_second(self):
		# GunBroker support was explicit that uploading tracking does NOT mark
		# an order shipped. Flag first because of the middle state each ordering
		# leaves: "shipped, tracking to follow" is visible to the buyer and
		# names its own retry; "tracking on an unshipped order" is neither.
		order = self._order()
		result, client = self._push(order)
		self.assertEqual(result["steps"], ["flag", "shipping", "readback"])
		client.set_order_flags.assert_called_once_with("998877", OrderShipped=True)
		client.set_order_shipping.assert_called_once()

	def test_a_mapped_carrier_is_sent(self):
		order = self._order()
		_result, client = self._push(order, label=("1Z999", "ups", ""))
		self.assertEqual(client.set_order_shipping.call_args[0][2], 2)

	def test_shipstations_usps_reseller_maps_to_usps(self):
		order = self._order()
		_result, client = self._push(order, label=("9400111", "stamps_com", ""),
			remote=fx.order_detail(tracking="9400111", carrier=3, status=5))
		self.assertEqual(client.set_order_shipping.call_args[0][2], 3)

	def test_an_unmapped_carrier_still_uploads_the_tracking_number(self):
		# GunBroker knows three couriers. A DHL shipment is not a reason to
		# leave the buyer with no tracking number at all.
		order = self._order()
		result, client = self._push(order,
			label=("DHL123", "dhl_express", "carrier not mapped: dhl_express"),
			remote=fx.order_detail(tracking="DHL123", carrier=0, status=5))
		self.assertIsNone(client.set_order_shipping.call_args[0][2])
		self.assertTrue(result["ok"])
		self.assertIn("carrier not mapped: dhl_express", result["warnings"])
		self.assertEqual(self.alerts, [])

	def test_the_read_back_reads_gunbrokers_key_pair_enums(self):
		# GET /Orders returns shipCarrier and status as {"code": "label"}, not
		# bare integers. Parsed as ints both read 0, and every mapped-carrier
		# shipment would fail its read-back for ever.
		order = self._order()
		result, _client = self._push(order, label=("1Z999", "ups", ""),
			remote=fx.order_detail(tracking="1Z999", carrier={"2": "UPS"},
				status={"5": "Order Complete"}))
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_status"], 5)

	def test_the_read_back_is_what_decides_success(self):
		# A 200 from a PUT is not evidence: GunBroker accepts and silently
		# ignores fields it does not want.
		order = self._order()
		result, _client = self._push(order,
			remote=fx.order_detail(tracking="", carrier=0, status=4))
		self.assertFalse(result["ok"])
		self.assertEqual(self.written["GB-ORD-00001"]["gb_ship_writeback_pending"], 1)
		self.assertIn("gb_ship_writeback_998877", self.alerts)

	def test_a_matching_read_back_clears_the_pending_flag(self):
		order = self._order(gb_ship_writeback_pending=1)
		result, _client = self._push(order)
		self.assertTrue(result["ok"])
		self.assertEqual(self.written["GB-ORD-00001"]["gb_ship_writeback_pending"], 0)

	def test_the_carrier_is_only_asserted_when_one_was_sent(self):
		# An order we deliberately sent no Carrier for reads back 0/Unknown;
		# asserting on that would fail every DHL shipment for doing exactly what
		# it was told.
		order = self._order()
		result, _client = self._push(order,
			label=("DHL123", "dhl_express", ""),
			remote=fx.order_detail(tracking="DHL123", carrier=0, status=5))
		self.assertTrue(result["ok"])

	def test_the_status_gunbroker_reports_is_recorded_not_asserted_on(self):
		# The expectation is that 4 Pending Shipment becomes 5 Order Complete. Their
		# status machine is theirs and has been seen to lag, so a write-back
		# GunBroker confirmed storing is not failed over it — that would put the
		# order back in the retry set and mail the store about a buyer who can
		# already see their tracking. The reading is kept where a human can see
		# it instead.
		order = self._order()
		result, _client = self._push(order,
			remote=fx.order_detail(tracking="1Z999", carrier=2, status=4))
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_status"], 4)
		self.assertTrue(any("status 4" in w for w in result["warnings"]),
			result["warnings"])
		self.assertIn("status 4", self.written["GB-ORD-00001"]["sync_error"])

	def test_the_expected_status_adds_no_noise(self):
		order = self._order()
		result, _client = self._push(order)
		self.assertEqual(result["gb_status"], shipping.COMPLETE_STATUS)
		self.assertEqual(result["warnings"], [])
		self.assertEqual(self.written["GB-ORD-00001"]["sync_error"], "")

	def test_a_resend_leaves_a_note_on_the_order(self):
		# After the read-back has disagreed for three rounds the
		# upload goes out again; without the note the order looks identical to
		# one on its first attempt, so nobody can tell whether re-sending has
		# already been tried and failed.
		stale = frappe.utils.now_datetime() - timedelta(minutes=31)
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00",
			tracking_pushed_at=stale, gb_ship_writeback_pending=1,
			tracking_number="1Z999", ship_carrier="UPS")
		result, _client = self._push(order)
		self.assertTrue(result["ok"])
		self.assertIn(shipping.RESENT_NOTE, result["warnings"])
		self.assertIn(shipping.RESENT_NOTE,
			self.written["GB-ORD-00001"]["sync_error"])

	def test_a_first_upload_is_not_reported_as_a_resend(self):
		order = self._order()
		result, _client = self._push(order)
		self.assertNotIn(shipping.RESENT_NOTE, result["warnings"])

	def test_a_resend_that_fails_says_so_alongside_the_error(self):
		stale = frappe.utils.now_datetime() - timedelta(minutes=31)
		order = self._order(shipping_flag_pushed_at="2026-08-27 09:00:00",
			tracking_pushed_at=stale, gb_ship_writeback_pending=1,
			tracking_number="1Z999", ship_carrier="UPS")
		client = MagicMock()
		client.set_order_shipping.side_effect = GunBrokerError("403 forbidden")
		self._patch("frappe.get_doc", return_value=order)
		self._patch_object(shipping, "_fetch_label", return_value=("1Z999", "ups", ""))
		shipping.push_tracking(order.name, client=client)
		recorded = self.written["GB-ORD-00001"]["sync_error"]
		self.assertIn("403 forbidden", recorded)
		self.assertIn(shipping.RESENT_NOTE, recorded)

	def test_no_label_means_not_even_the_flag_goes_out(self):
		# The flag used to be sent anyway, on the reasoning that "the parcel HAS
		# left" — which is only true under the selection query that requires a
		# shipment. push_tracking_now reaches this function directly, so the
		# call site checks for itself. Nothing may claim a shipment before the
		# store has bought the label for it.
		order = self._order()
		client = MagicMock()
		self._patch("frappe.get_doc", return_value=order)
		self._patch_object(shipping, "_fetch_label", return_value=None)
		result = shipping.push_tracking(order.name, client=client)
		self.assertFalse(result["ok"])
		self.assertEqual(result["steps"], [])
		client.set_order_flags.assert_not_called()
		client.set_order_shipping.assert_not_called()
		self.assertNotIn("GB-ORD-00001", self.written)

	def test_a_failed_upload_is_recorded_rather_than_raised(self):
		# A throw would roll the transaction back and take the pending flag and
		# the reason with it — the operator would see the error once and the
		# order would forget it.
		order = self._order()
		client = MagicMock()
		client.set_order_shipping.side_effect = GunBrokerError("403 forbidden")
		self._patch("frappe.get_doc", return_value=order)
		self._patch_object(shipping, "_fetch_label", return_value=("1Z999", "ups", ""))
		result = shipping.push_tracking(order.name, client=client)
		self.assertFalse(result["ok"])
		self.assertEqual(self.written["GB-ORD-00001"]["gb_ship_writeback_pending"], 1)
		self.assertNotIn("tracking_pushed_at", self.written["GB-ORD-00001"])

	def test_nothing_outstanding_makes_no_calls(self):
		order = self._order(shipping_flag_pushed_at="x", tracking_pushed_at="y")
		client = MagicMock()
		self._patch("frappe.get_doc", return_value=order)
		result = shipping.push_tracking(order.name, client=client)
		self.assertEqual(result["steps"], [])
		client.set_order_flags.assert_not_called()


class TestPollGate(_ShippingCase):
	def test_the_cron_entry_never_raises(self):
		self._patch_object(shipping, "gunbroker_enabled",
			side_effect=RuntimeError("settings table is missing"))
		self._patch_object(shipping, "safe_log_error", side_effect=lambda *a: None)
		shipping.poll_gb_tracking()  # must not raise

	def test_nothing_happens_while_the_integration_is_off(self):
		self._patch_object(shipping, "gunbroker_enabled", return_value=False)
		enqueue = self._patch("frappe.enqueue")
		pending = self._patch_object(shipping, "pending_writeback_orders")
		shipping.poll_gb_tracking()
		enqueue.assert_not_called()
		pending.assert_not_called()

	def test_the_tick_only_enqueues_and_does_no_outbound_work(self):
		# A Cron scheduled job runs on the default queue's 300-second budget,
		# and a round that gets cut off part way has no resume point. The
		# dedupe is what stops a slow round having a second one stacked behind
		# it, each PUTting tracking for the same order.
		self._patch_object(shipping, "gunbroker_enabled", return_value=True)
		self._patch("ffl_core.utils.single_flag", return_value=True)
		enqueue = self._patch("frappe.enqueue")
		pending = self._patch_object(shipping, "pending_writeback_orders")
		summary = shipping.poll_gb_tracking()
		self.assertTrue(summary["enqueued"])
		pending.assert_not_called()
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(
			args[0], "ffl_integrations.gunbroker.shipping._run_tracking_writeback")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], shipping.TRACKING_JOB_ID)

	def test_one_orders_failure_does_not_stop_the_round(self):
		self._patch_object(shipping, "pending_writeback_orders",
			return_value=["A", "B"])
		seen = []

		def _push(name):
			seen.append(name)
			if name == "A":
				raise RuntimeError("gunbroker is down")
			return {"ok": True}

		self._patch_object(shipping, "push_tracking", side_effect=_push)
		self._patch_object(shipping, "safe_log_error", side_effect=lambda *a: None)
		self._patch_object(shipping, "safe_commit", side_effect=lambda: None)
		summary = shipping._run_tracking_writeback()
		self.assertEqual(seen, ["A", "B"])
		self.assertEqual(summary["failed"], 1)
		self.assertEqual(summary["pushed"], 1)
