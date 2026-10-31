"""Pulling GunBroker sales in — and the four ways that goes wrong quietly.

The expensive failures on this path are all failures that look like success:

* a watermark that advances past an order nobody managed to write, so the sale
  is never seen again and the gun stays on the shelf;
* a first run that reaches back into the account's history and reserves guns
  currently in stock against orders from years ago;
* an order re-polled every fifteen minutes deciding its OWN reservation belongs
  to somebody else, and emailing the store about it hourly for ever;
* a payment method or a tax figure read under the wrong spelling, which becomes
  a zero rather than an error.

So the assertions here are mostly about state AFTER the function returned, and
about what did NOT happen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import orders
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerConfig,
	GunBrokerError,
	utc_stamp,
)
from ffl_integrations.tests import gunbroker_fixtures as fx


def _config() -> GunBrokerConfig:
	return GunBrokerConfig(
		username="seller", password="secret", dev_key="sandbox-key",
		base_url="https://api.sandbox.gunbroker.com/v1", sandbox=True, timeout=5)


class _OrderCase(FrappeTestCase):
	def setUp(self):
		self.settings = fx.settings_stub(last_orders_synced_at=None,
			orders_since_override=None)
		self.singles: dict = {}
		self.serial_rows: dict[str, dict] = {}
		self.alerts: list[str] = []
		self.exits: list[tuple] = []
		self.returns: list[str] = []

		def _set_single(_doctype, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.singles.update(updates)

		def _set_value(doctype, name, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			if doctype == "Serial No":
				self.serial_rows.setdefault(name, {}).update(updates)

		self._patch("frappe.db.set_single_value", side_effect=_set_single)
		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch_object(orders, "gunbroker_enabled", return_value=True)
		self._patch_object(orders, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"]))
		self._patch_object(orders, "fire_serial_channel_exit",
			side_effect=lambda serial, **kw: self.exits.append((serial, kw.get("reason"))))
		self._patch_object(orders, "fire_serial_channel_return",
			side_effect=lambda serial, **kw: self.returns.append(serial))
		self._patch_object(orders, "safe_commit", side_effect=lambda: None)

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _client(self, script):
		session = fx.RecordingSession(script)
		client = GunBrokerClient(_config(), session=session)
		client._store_token("tok")
		return client, session


# ---------------------------------------------------------------------------
# Reading GunBroker's shapes
# ---------------------------------------------------------------------------

class TestPaymentMethodDecoding(FrappeTestCase):
	def test_a_bitmask_becomes_the_methods_it_names(self):
		self.assertEqual(orders.decode_payment_methods(8192 | 32768),
			["Certified Check", "Money Order"])

	def test_the_single_key_object_form_is_read_as_a_mask(self):
		# Every other GunBroker enumeration reads back as {"code": "Label"};
		# assuming an integer here silently yields "no payment methods".
		self.assertEqual(orders.decode_payment_methods({"8192": "Certified Check"}),
			["Certified Check"])

	def test_an_undocumented_bit_is_reported_not_dropped(self):
		# 1024 is one of the three codes GunBroker's table skips. A payment
		# method we cannot name is exactly the thing somebody needs to see.
		out = orders.decode_payment_methods(8 | 1024)
		self.assertIn("Visa/MasterCard", out)
		self.assertIn("Unknown (1024)", out)

	def test_nothing_offered_is_an_empty_list_not_a_crash(self):
		self.assertEqual(orders.decode_payment_methods(0), [])
		self.assertEqual(orders.decode_payment_methods(None), [])


class TestSkuSplitting(FrappeTestCase):
	def test_the_two_part_sku_gives_item_and_serial(self):
		self.assertEqual(orders.split_sku("COLT-PYTHON::SN-001"),
			("COLT-PYTHON", "SN-001"))

	def test_a_plain_sku_names_no_serial(self):
		self.assertEqual(orders.split_sku("COLT-PYTHON"), ("COLT-PYTHON", None))

	def test_a_trailing_separator_is_not_a_serial_called_empty_string(self):
		self.assertEqual(orders.split_sku("COLT-PYTHON::"), ("COLT-PYTHON", None))


class TestPaymentSource(FrappeTestCase):
	def _order(self, **fields):
		return fx.FakeDoc(**fields)

	def test_what_we_recorded_beats_any_inference(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_log_transaction_id="txn-1", payment_received=1)
		self.assertEqual(
			orders.classify_payment_source(order, ["Visa/MasterCard"]),
			orders.SOURCE_CHECK)

	def test_a_gateway_trail_with_a_card_method_is_immediate_checkout(self):
		order = self._order(payment_log_transaction_id="txn-1")
		self.assertEqual(
			orders.classify_payment_source(order, ["Visa/MasterCard"]),
			orders.SOURCE_GATEWAY_CHECKOUT)

	def test_their_flag_with_no_trail_and_nothing_recorded_is_flagged_as_theirs(self):
		# This is the state to notice, not to normalise: this phase has no
		# gateway, so money GunBroker says arrived that we never recorded is
		# either their button being pressed early or something wrong.
		order = self._order(payment_received=1)
		self.assertEqual(orders.classify_payment_source(order, ["Certified Check"]),
			orders.SOURCE_GATEWAY_GB_SET)

	def test_a_fresh_unpaid_order_is_unknown(self):
		self.assertEqual(orders.classify_payment_source(self._order(), []),
			orders.SOURCE_UNKNOWN)


class TestCancellationBeforeInvoicing(_OrderCase):
	"""Putting the gun back is only half the job when money was banked.

	Nothing is on the ledger — no invoice, no receipt, no refund — while the
	order itself records that a cheque was deposited. Nobody was told, and the
	only thing that would eventually notice describes it as "paid and not
	shipped", which is a different problem with a different answer.
	"""

	def _cancelled(self, **fields):
		order = fx.FakeDoc(name="GB-ORD-00001", gb_order_id="998877",
			gb_status=orders.CANCELLED_STATUS, gb_status_label="Cancelled",
			order_status="Pending Fulfillment", **fields)
		order.append("serials", {"confirmed_serial": "SN-001"})
		return order

	def test_an_unpaid_cancellation_is_announced_plainly(self):
		sent = []
		self._patch_object(orders, "notify_admins",
			side_effect=lambda s, m, **kw: sent.append((kw["dedup_key"], m)))
		with patch("frappe.db.get_value", return_value=None):
			orders._detect_unwound(self._cancelled())
		self.assertEqual([key for key, _m in sent], ["gb_cancel_998877"])
		self.assertIn("back on sale on the other channels", sent[0][1])

	def test_a_cancellation_after_the_cheque_was_banked_names_the_refund(self):
		order = self._cancelled(payment_mode_used="GunBroker Check",
			payment_amount=1245.99, payment_reference_no="4471",
			payment_received_on="2026-08-20")
		sent = []
		self._patch_object(orders, "notify_admins",
			side_effect=lambda s, m, **kw: sent.append((s, m)))
		with patch("frappe.db.get_value", return_value=None):
			orders._detect_unwound(order)
		subject, body = sent[0]
		self.assertIn("refund due", subject)
		self.assertIn("4471", body)
		self.assertIn("unreferenced Receive entry reversed by a Pay entry", body)
		self.assertIn("following your GunBroker refund procedure", body)
		# The marker carries the real GunBroker order id, which is what the
		# cash-basis report matches on, never the POS record name or a placeholder.
		self.assertIn('Custom Remarks first, then write "GunBroker Order #998877"', body)
		# Both entries carry the marker, as the field help says.
		self.assertIn("Both Payment Entries (the unreferenced Receive and the Pay)", body)
		self.assertNotIn("<", body)
		self.assertNotIn("sixth case", body)


class TestTheGatewayAlert(_OrderCase):
	"""Card money on an account with no card gateway is somebody's mistake.

	The gun does not move either way — the dispose gate reads what POS
	recorded, and a GB-set order has nothing recorded — so this is not a hold.
	It is the only thing that tells anybody it happened: the daily audit reads
	what POS recorded too, so without the alert these orders are invisible in
	both places at once.
	"""

	def _apply(self, detail):
		order = fx.FakeDoc(name="GB-ORD-00001", gb_order_id="998877")
		with patch("frappe.db.exists", return_value=True):
			orders.apply_order_fields(order, detail)
		return order

	def test_gunbroker_setting_the_flag_itself_is_alerted_on(self):
		order = self._apply(fx.order_detail(payment_received=True,
			paymentReceivedDateUTC=None))
		self.assertEqual(order.payment_source, orders.SOURCE_GATEWAY_GB_SET)
		self.assertEqual(self.alerts, ["gb_gateway_payment_unexpected"])

	def test_a_card_trail_is_alerted_on_too(self):
		# Stricter than the payment-method table: this phase connected no
		# gateway at all, so ANY reading that says one was used is an anomaly.
		order = self._apply(fx.order_detail(
			paymentMethods={"8": "Visa / MasterCard"},
			paymentLogTransactionID="txn-1"))
		self.assertEqual(order.payment_source, orders.SOURCE_GATEWAY_CHECKOUT)
		self.assertEqual(self.alerts, ["gb_gateway_payment_unexpected"])

	def test_an_ordinary_cheque_order_is_not_alerted_on(self):
		order = self._apply(fx.order_detail())
		self.assertEqual(order.payment_source, orders.SOURCE_UNKNOWN)
		self.assertEqual(self.alerts, [])


class TestFieldMapping(_OrderCase):
	def test_the_detail_maps_onto_the_order(self):
		order = fx.FakeDoc()
		orders.apply_order_fields(order, fx.order_detail())
		self.assertEqual(order.gb_status, 4)
		self.assertEqual(order.gb_status_label, "Pending Shipment")
		self.assertEqual(order.buyer_user_id, "55501")
		self.assertEqual(order.customer_email, "dave@example.com")
		self.assertEqual(order.ship_to_state, "TX")
		self.assertEqual(order.order_total, 1245.99)
		self.assertEqual(order.compliance_fee, 11.00)
		self.assertEqual(order.payment_methods, "Certified Check")
		self.assertEqual(order.order_status, "Pending Fulfillment")

	def test_the_status_enum_is_read_from_its_key_pair_shape(self):
		# GET /Orders returns the status as {"3": "Pending Payment"}, not a bare
		# integer. Reading it as an int gave 0 on every real order, which blinds
		# the layaway and cancellation checks that key off gb_status.
		order = fx.FakeDoc()
		orders.apply_order_fields(order, fx.order_detail(status={"3": "Pending Payment"}))
		self.assertEqual(order.gb_status, 3)
		self.assertEqual(order.gb_status_label, orders.GB_STATUS_LABELS[3])

	def test_a_long_effl_url_saves(self):
		# A MasterFFL transfer-proof link runs past 140 characters. As a Data
		# field it failed every save, so the order froze at its first poll with
		# no licence number on it.
		url = "https://documents.masterffl.com/ffl-transfer-proofs/" + "a" * 200 + ".pdf"
		doc = frappe.get_doc({"doctype": "GunBroker Order", "gb_order_id": "EFFL-LEN-1",
			"effl_url": url, "seller_packet_url": url})
		doc.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("GunBroker Order", doc.name, "effl_url"), url)

	def test_a_corrected_licence_number_is_not_overwritten_by_the_poll(self):
		# Staff can correct a licence number in the dispose dialog. GunBroker is
		# not going to change its mind about what the buyer typed, so a poll
		# that re-applied it would send the gun back to the wrong dealer — and
		# it re-polls every fifteen minutes.
		order = fx.FakeDoc(ffl_number="1-99-888-01-2A-99999")
		orders.apply_order_fields(order, fx.order_detail())
		self.assertEqual(order.ffl_number, "1-99-888-01-2A-99999")

	def test_a_blank_licence_number_is_filled_in(self):
		order = fx.FakeDoc()
		orders.apply_order_fields(order, fx.order_detail())
		self.assertEqual(order.ffl_number, "1-99-888-01-2A-34567")

	def test_utc_timestamps_are_read_from_the_utc_twin(self):
		order = fx.FakeDoc()
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=timezone.utc):
			orders.apply_order_fields(order, fx.order_detail())
		self.assertIsNotNone(order.order_date)
		self.assertEqual(order.order_date.hour, 13)

	def test_every_gunbroker_timestamp_lands_in_site_time_and_naive(self):
		# LD-20. A Frappe Datetime column holds a naive value in site time and
		# get_valid_dict stringifies whatever it is handed, so an aware UTC
		# value stores the UTC wall clock: five or six hours ahead of Texas on
		# the fields the Desk shows, the ones the Pending Order page groups its
		# cards by, and the ones the cash-basis schedule compares against a
		# local period end. 13:05Z is 08:05 in Chicago.
		order = fx.FakeDoc()
		detail = fx.order_detail(payment_received=True, tracking="")
		detail["cancelDate"] = "2026-08-26T13:05:00Z"
		detail["refundDate"] = "2026-08-26T13:05:00Z"
		detail["returnedDate"] = "2026-08-26T13:05:00Z"
		detail["paymentLogDateTime"] = "2026-08-26T13:05:00Z"
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=ZoneInfo("America/Chicago")):
			orders.apply_order_fields(order, detail)
		stamped = ["order_date", "gb_last_modified", "ffl_received_at",
			"payment_received_at", "payment_log_date", "cancel_date",
			"refund_date", "returned_date"]
		for field in stamped:
			value = order.get(field)
			self.assertIsNotNone(value, field)
			self.assertIsNone(value.tzinfo, f"{field} kept its tzinfo")
		self.assertEqual(order.order_date.hour, 8)
		self.assertEqual(order.cancel_date.hour, 8)

	def test_the_cpa_algebra_case_lands_on_the_previous_day(self):
		# The reason the conversion has to happen on
		# the way in: a cancellation at 23:30 on New Year's Eve is the NEXT
		# year in UTC. The cash-basis schedule and the year-end journal both
		# work from local dates, so a column holding the UTC wall clock moves
		# this order into the wrong year.
		order = fx.FakeDoc()
		detail = fx.order_detail()
		detail["cancelDate"] = "2027-01-01T05:30:00Z"
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=ZoneInfo("America/Chicago")):
			orders.apply_order_fields(order, detail)
		self.assertEqual(order.cancel_date.strftime("%Y-%m-%d %H:%M:%S"),
			"2026-12-31 23:30:00")
		self.assertIsNone(order.cancel_date.tzinfo)

	def test_the_watermark_the_poll_advances_to_stays_in_utc(self):
		# The other half of LD-20, and the reason this is not a blanket rule:
		# the watermark is machine state handed straight to a query filter that
		# demands UTC. Converting it would move the floor by the whole offset.
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=ZoneInfo("America/Chicago")):
			modified = orders._row_modified(fx.order_detail())
		self.assertIsNotNone(modified.tzinfo)
		self.assertEqual(modified.astimezone(timezone.utc).hour, 14)

	def test_lines_carry_price_serial_and_the_ffl_requirement(self):
		order = fx.FakeDoc()
		with patch("frappe.db.exists", return_value=True):
			orders.apply_order_lines(order, fx.order_detail())
		self.assertEqual(len(order.lines), 1)
		line = order.lines[0]
		self.assertEqual(line.sku, "COLT-PYTHON::SN-001")
		self.assertEqual(line.item_code, "COLT-PYTHON")
		self.assertEqual(line.unit_price, 1100.00)
		self.assertEqual(line.serial_number, "SN-001")
		self.assertEqual(line.is_ffl_required, 1)
		self.assertEqual(order.item_subtotal, 1100.00)

	def test_an_already_accrued_fee_survives_the_next_poll(self):
		# The fee was computed from the price at the time of sale and GunBroker
		# does not reduce it when a seller discounts later. Recomputing on every
		# poll would re-price a liability that has already been journalled.
		order = fx.FakeDoc()
		with patch("frappe.db.exists", return_value=True):
			orders.apply_order_lines(order, fx.order_detail())
			order.lines[0].fvf_amount = 48.0
			order.lines[0].ffl_doc_fee_amount = 5.0
			orders.apply_order_lines(order, fx.order_detail())
		self.assertEqual(order.lines[0].fvf_amount, 48.0)
		self.assertEqual(order.lines[0].ffl_doc_fee_amount, 5.0)

	def test_an_unknown_item_code_leaves_the_link_empty_rather_than_guessing(self):
		order = fx.FakeDoc()
		with patch("frappe.db.exists", return_value=False):
			orders.apply_order_lines(order, fx.order_detail())
		self.assertFalse(order.lines[0].item_code)
		self.assertEqual(order.lines[0].sku, "COLT-PYTHON::SN-001")


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------

class TestQueryFloor(_OrderCase):
	def test_a_new_site_starts_now_and_not_a_month_ago(self):
		floor = orders._query_floor(self.settings)
		now = datetime.now(timezone.utc)
		# Fifteen minutes of overlap, and nothing more. A default of "thirty
		# days" would pull the account's history in and match those orders'
		# serial numbers against guns on the shelf today.
		self.assertLess(abs((now - floor).total_seconds() - 15 * 60), 120)

	def test_the_first_run_persists_where_it_started(self):
		orders._query_floor(self.settings)
		self.assertIn("last_orders_synced_at", self.singles)

	def test_an_override_earlier_than_the_watermark_pulls_the_floor_back(self):
		# Midnight as the operator typed it — in the STORE's clock, which is the
		# only reading a Desk date control could mean. In January, Chicago is
		# UTC−6, so it is 06:00 UTC that goes to GunBroker, less the fifteen
		# minutes of overlap every query carries.
		#
		# The zone is patched rather than inherited. The offline harness falls
		# back to UTC when it cannot resolve one, so an assertion written
		# against the ambient setting silently asserts "no conversion happens"
		# — which is how this test passed offline and failed on a real site.
		settings = fx.settings_stub(
			last_orders_synced_at="2026-08-26 12:00:00",
			orders_since_override="2026-01-10 00:00:00")
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=ZoneInfo("America/Chicago")):
			floor = orders._query_floor(settings)
		self.assertEqual(floor, datetime(2026, 1, 10, 5, 45, tzinfo=timezone.utc))

	def test_a_later_override_does_not_push_the_floor_forward(self):
		# Forward would SKIP orders, which is the failure this whole watermark
		# design exists to avoid; the field is for reaching back, only.
		settings = fx.settings_stub(
			last_orders_synced_at="2026-03-01 00:00:00",
			orders_since_override="2026-08-26 12:00:00")
		floor = orders._query_floor(settings)
		self.assertEqual(floor, datetime(2026, 2, 28, 23, 45, tzinfo=timezone.utc))

	def test_the_watermark_is_read_as_utc_and_the_override_as_store_time(self):
		# Reading either in the other's zone shifts the floor by the whole
		# offset — hours of orders skipped, or hours re-fetched.
		stored = orders._load_utc("2026-08-26 12:00:00")
		self.assertEqual(stored.tzinfo, timezone.utc)
		self.assertEqual(stored.hour, 12)
		with patch.object(orders, "_load_site", wraps=orders._load_site):
			typed = orders._load_site("2026-08-26 12:00:00")
		self.assertIsNotNone(typed.tzinfo)

	def test_the_override_is_consumed_once_and_then_cleared(self):
		# Left in place it is not a back-fill but a permanent floor: every tick
		# from then on re-reads everything since that moment — up to fifty
		# pages, and a GET /Orders/{id} per row on them — until somebody
		# notices. The watermark is moved back FIRST, so a run that dies half
		# way through resumes from the floor instead of the back-fill
		# disappearing with the field.
		settings = fx.settings_stub(
			last_orders_synced_at="2026-08-26 12:00:00",
			orders_since_override="2026-01-10 00:00:00")
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=ZoneInfo("America/Chicago")):
			orders._query_floor(settings)
		# Stored in UTC, which is what the watermark column holds — the
		# operator typed midnight in the store's clock and January in Chicago
		# is UTC−6.
		self.assertEqual(self.singles["last_orders_synced_at"],
			"2026-01-10 06:00:00")
		self.assertIsNone(self.singles["orders_since_override"])
		self.assertIn("gb_orders_backfill", self.alerts)

	def test_an_ordinary_poll_does_not_touch_the_override(self):
		settings = fx.settings_stub(last_orders_synced_at="2026-08-26 12:00:00",
			orders_since_override=None)
		orders._query_floor(settings)
		self.assertNotIn("orders_since_override", self.singles)
		self.assertEqual(self.alerts, [])

	def test_the_stored_watermark_round_trips(self):
		moment = datetime(2026, 8, 26, 18, 30, tzinfo=timezone.utc)
		orders._set_watermark(moment, count=3)
		self.assertEqual(self.singles["last_orders_synced_at"], "2026-08-26 18:30:00")
		self.assertEqual(orders._load_utc(self.singles["last_orders_synced_at"]), moment)


class TestOrdersSinceFormat(FrappeTestCase):
	def test_the_incremental_filter_is_iso_utc_with_a_z(self):
		# GunBroker validates this strictly and 400s anything else, which would
		# take the whole poll down rather than one order.
		stamp = utc_stamp(datetime(2026, 8, 26, 14, 5, tzinfo=timezone.utc))
		self.assertEqual(stamp, "2026-08-26T14:05:00Z")

	def test_a_naive_value_is_converted_and_not_relabelled(self):
		with patch("ffl_integrations.gunbroker.client._site_tz",
				return_value=timezone(timedelta(hours=-5))):
			stamp = utc_stamp("2026-08-26 09:05:00")
		self.assertEqual(stamp, "2026-08-26T14:05:00Z")


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

class TestWatermarkAdvance(_OrderCase):
	def _run(self, rows, outcomes):
		"""Drive run_order_sync over `rows` with a scripted per-order outcome."""
		calls = {"i": 0}

		def _ingest(_client, row):
			index = calls["i"]
			calls["i"] += 1
			outcome = outcomes[index]
			if outcome == "boom":
				raise GunBrokerError("nope")
			return "GB-ORD-0000" + str(index)

		self._patch_object(orders, "_collect_rows", return_value=rows)
		self._patch_object(orders, "GunBrokerClient", MagicMock())
		self._patch_object(orders, "_ingest_order", side_effect=_ingest)
		self._patch("frappe.get_single", return_value=self.settings)
		self._patch("frappe.db.savepoint")
		self._patch("frappe.db.rollback")
		self._patch("frappe.db.get_value", return_value=None)
		return orders.run_order_sync()

	def _row(self, order_id, minute):
		return fx.orders_sold_row(order_id,
			modified=f"2026-08-26T14:{minute:02d}:00Z")

	def test_an_all_clear_batch_advances_to_the_last_change(self):
		rows = [self._row("1", 1), self._row("2", 2), self._row("3", 3)]
		summary = self._run(rows, ["ok", "ok", "ok"])
		self.assertEqual(summary["ingested"], 3)
		self.assertEqual(self.singles["last_orders_synced_at"], "2026-08-26 14:03:00")

	def test_a_failure_stops_the_watermark_at_the_order_before_it(self):
		# The later orders still go in — one bad order must not block a day of
		# sales — but the mark cannot pass the failure, or it is never retried.
		rows = [self._row("1", 1), self._row("2", 2), self._row("3", 3)]
		summary = self._run(rows, ["ok", "boom", "ok"])
		self.assertEqual(summary["ingested"], 2)
		self.assertEqual(summary["failed"], 1)
		self.assertEqual(self.singles["last_orders_synced_at"], "2026-08-26 14:01:00")

	def test_a_first_order_failure_leaves_the_watermark_alone(self):
		rows = [self._row("1", 1), self._row("2", 2)]
		self._run(rows, ["boom", "ok"])
		# _query_floor persisted the starting point on a fresh site; what must
		# NOT have happened is an advance past the failure.
		self.assertNotEqual(self.singles.get("last_orders_synced_at"),
			"2026-08-26 14:02:00")

	def test_rows_are_processed_oldest_change_first(self):
		# The watermark rule is only meaningful in modification order, and no
		# sort parameter is sent because none has ever been observed working.
		rows = [self._row("3", 3), self._row("1", 1), self._row("2", 2)]
		client, session = self._client([
			(("GET", "/OrdersSold"), fx.make_response(fx.page(rows))),
		])
		ordered = orders._collect_rows(client, datetime.now(timezone.utc), self.settings)
		self.assertEqual([orders._order_id(r) for r in ordered], ["1", "2", "3"])
		params = session.calls[0]["params"]
		self.assertNotIn("Sort", params)
		self.assertTrue(str(params["OrdersModifiedSinceDate"]).endswith("Z"))

	def test_the_same_order_twice_in_one_batch_is_read_once(self):
		rows = [self._row("7", 1), self._row("7", 2)]
		client, _session = self._client([
			(("GET", "/OrdersSold"), fx.make_response(fx.page(rows))),
		])
		ordered = orders._collect_rows(client, datetime.now(timezone.utc), self.settings)
		self.assertEqual(len(ordered), 1)

	def test_a_batch_that_could_not_be_read_does_not_move_the_watermark(self):
		# Not reading is not the same as reading nothing. Advancing here would
		# skip every order that changed during the outage.
		self._patch("frappe.get_single", return_value=self.settings)
		self._patch_object(orders, "GunBrokerClient", MagicMock())
		self._patch_object(orders, "_collect_rows",
			side_effect=GunBrokerError("gunbroker is down"))
		self.singles.clear()
		orders.run_order_sync()
		self.assertIn("gb_order_sync", self.alerts)
		self.assertIn("last_sync_error", self.singles)


class TestPersistentFailure(_OrderCase):
	def test_an_order_that_keeps_failing_stops_blocking_and_says_so(self):
		self._patch("frappe.db.get_value", side_effect=lambda dt, *a, **kw: (
			"GB-ORD-00009" if isinstance(a[0], dict) else 4))
		self._patch_object(orders, "safe_commit", side_effect=lambda: None)
		outcome = orders._record_order_failure("998877", GunBrokerError("nope"))
		self.assertEqual(outcome, "abandoned")
		self.assertIn("gb_order_stuck_998877", self.alerts)

	def test_an_order_that_has_only_just_started_failing_still_blocks(self):
		self._patch("frappe.db.get_value", side_effect=lambda dt, *a, **kw: (
			"GB-ORD-00009" if isinstance(a[0], dict) else 1))
		self._patch_object(orders, "safe_commit", side_effect=lambda: None)
		self.assertEqual(
			orders._record_order_failure("998877", GunBrokerError("nope")), "failed")
		self.assertEqual(self.alerts, [])

	def test_an_order_that_could_not_even_be_created_blocks(self):
		self._patch("frappe.db.get_value", return_value=None)
		self.assertEqual(
			orders._record_order_failure("998877", GunBrokerError("nope")), "failed")


# ---------------------------------------------------------------------------
# Matching a line to a gun, and holding it
# ---------------------------------------------------------------------------

class TestSerialMatching(_OrderCase):
	def _line(self, **fields):
		return fx.FakeChildRow({"gb_item_id": "16087230", "sku": "", "serial_number": "",
			**fields})

	def test_the_sku_is_tried_first(self):
		with patch("frappe.db.exists", side_effect=lambda dt, name: name == "SN-001"):
			serial, why = orders._match_serial(
				self._line(sku="COLT::SN-001", serial_number="SN-OTHER"), {})
		self.assertEqual(serial, "SN-001")
		self.assertEqual(why, "")

	def test_the_reported_serial_is_the_fallback(self):
		# A listing made by hand on the GunBroker website carries no SKU of
		# ours; the serial number field is all there is.
		with patch("frappe.db.exists", side_effect=lambda dt, name: name == "SN-002"):
			serial, _why = orders._match_serial(
				self._line(sku="", serial_number="SN-002"), {})
		self.assertEqual(serial, "SN-002")

	def test_neither_matching_is_reported_not_guessed(self):
		with patch("frappe.db.exists", return_value=False):
			serial, why = orders._match_serial(
				self._line(sku="COLT::SN-404", serial_number="SN-404"), {})
		self.assertIsNone(serial)
		self.assertIn("16087230", why)
		self.assertIn("SN-404", why)

	def test_two_lines_cannot_claim_the_same_gun(self):
		with patch("frappe.db.exists", return_value=True):
			serial, why = orders._match_serial(
				self._line(sku="COLT::SN-001"), {"SN-001": "16087111"})
		self.assertIsNone(serial)
		self.assertIn("already claimed", why)


class TestHolding(_OrderCase):
	def _order(self):
		return fx.FakeDoc(name="GB-ORD-00001")

	def test_a_free_gun_is_held_for_this_order(self):
		self._patch("frappe.db.get_value",
			return_value=frappe._dict({"name": "SN-001", "gb_reserved": 0, "gb_order": ""}))
		self._patch_object(orders, "serial_shelf_state",
			return_value={"sellable": True, "reason": None, "detail": {}})
		error, newly = orders._hold_serial(self._order(), "SN-001")
		self.assertEqual(error, "")
		self.assertTrue(newly)
		self.assertEqual(self.serial_rows["SN-001"],
			{"gb_reserved": 1, "gb_order": "GB-ORD-00001"})

	def test_this_order_re_polled_does_not_find_itself_in_conflict(self):
		# GunBroker re-modifies an order on payment, on FFL receipt and on
		# shipment, so it comes back through here repeatedly. Without this the
		# store gets an email every hour saying a gun it sold on GunBroker is
		# unavailable because GunBroker has it.
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"name": "SN-001", "gb_reserved": 1, "gb_order": "GB-ORD-00001"}))
		shelf = self._patch_object(orders, "serial_shelf_state")
		error, newly = orders._hold_serial(self._order(), "SN-001")
		self.assertEqual(error, "")
		self.assertFalse(newly)
		shelf.assert_not_called()

	def test_a_gun_another_channel_holds_is_reported_with_the_channel(self):
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"name": "SN-001", "gb_reserved": 0, "gb_order": ""}))
		self._patch_object(orders, "serial_shelf_state", return_value={
			"sellable": False, "reason": "channel_reserved",
			"detail": {"channel": "woo", "ref": "WOO-ORD-0007"}})
		error, newly = orders._hold_serial(self._order(), "SN-001")
		self.assertIn("woo", error)
		self.assertIn("WOO-ORD-0007", error)
		self.assertFalse(newly)
		self.assertNotIn("SN-001", self.serial_rows)

	def _flagged(self, history="16087230"):
		self._patch("frappe.db.get_value", return_value=frappe._dict({
			"name": "SN-001", "gb_reserved": 0, "gb_order": "", "gb_sold_unknown": 1,
			"gb_item_id": "", "gb_item_history": history,
			"gb_sync_error": "may already be sold on GunBroker: x"}))
		return self._patch_object(orders, "_note_doubt_resolved")

	def test_the_order_for_the_sold_listing_lifts_the_doubt_flag(self):
		# The sweep ran between the sale and this poll: it saw the listing sold
		# with no order in POS and flagged the gun. This order is that sale, so
		# it must be held — not reported as "GunBroker has a buyer for it".
		note = self._flagged(history="16087111,16087230")
		self._patch_object(orders, "serial_shelf_state",
			side_effect=lambda s: {"sellable": self.serial_rows.get(s, {}).get(
				"gb_sold_unknown") == 0, "reason": "sold_unknown", "detail": {}})
		error, newly = orders._hold_serial(self._order(), "SN-001", "16087230")
		self.assertEqual(error, "")
		self.assertTrue(newly)
		self.assertEqual(self.serial_rows["SN-001"], {"gb_sold_unknown": 0,
			"gb_sync_error": "", "gb_reserved": 1, "gb_order": "GB-ORD-00001"})
		note.assert_called_once()

	def test_a_different_listing_does_not_lift_the_doubt_flag(self):
		# Some other sale of this serial (a hand-made listing, a stale relist) is
		# not evidence the flagged listing's order arrived — keep failing closed.
		note = self._flagged()
		self._patch_object(orders, "serial_shelf_state", return_value={
			"sellable": False, "reason": "sold_unknown", "detail": {}})
		error, newly = orders._hold_serial(self._order(), "SN-001", "16089999")
		self.assertIn("sold_unknown", error)
		self.assertFalse(newly)
		self.assertNotIn("SN-001", self.serial_rows)
		note.assert_not_called()

	def test_the_flag_comes_back_when_another_channel_still_has_the_gun(self):
		# Lifting the doubt must not leave the gun unguarded: if a web order
		# holds it too, this order does not get it, and when that web order is
		# cancelled the gun would otherwise go back on the shelf with a paid
		# GunBroker buyer waiting.
		note = self._flagged()
		self._patch_object(orders, "serial_shelf_state", return_value={
			"sellable": False, "reason": "channel_reserved",
			"detail": {"channel": "woo", "ref": "WOO-ORD-0007"}})
		error, newly = orders._hold_serial(self._order(), "SN-001", "16087230")
		self.assertIn("WOO-ORD-0007", error)
		self.assertFalse(newly)
		self.assertEqual(self.serial_rows["SN-001"], {"gb_sold_unknown": 1,
			"gb_sync_error": "may already be sold on GunBroker: x"})
		note.assert_not_called()

	def test_a_vanished_serial_is_reported_rather_than_reserved(self):
		self._patch("frappe.db.get_value", return_value=None)
		error, newly = orders._hold_serial(self._order(), "SN-GONE")
		self.assertIn("no longer exists", error)
		self.assertFalse(newly)

	def test_the_gun_is_read_under_a_row_lock(self):
		# THE anti-double-sale line, and the one nothing else here can stand in
		# for. Between "is this gun free?" and "then it is mine" nobody else
		# gets to answer the same question — a counter sale, the Woo poll and a
		# second GunBroker worker all reach the same row. Drop the keyword and
		# every other test in this file still passes: the reservation is
		# written, the shelf state is read, the alert is not sent. Only the
		# window between the two reads changes, and it only shows up as two
		# buyers for one gun.
		lock = self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"name": "SN-001", "gb_reserved": 0, "gb_order": ""}))
		self._patch_object(orders, "serial_shelf_state",
			return_value={"sellable": True, "reason": None, "detail": {}})
		orders._hold_serial(self._order(), "SN-001")
		_args, kwargs = lock.call_args_list[0]
		self.assertTrue(kwargs.get("for_update"),
			"the reservation re-check read Serial No without FOR UPDATE")

	def test_the_lock_is_taken_before_the_shelf_is_read(self):
		# The lock has to come first or it locks nothing that matters: reading
		# the shelf state outside it is the same race with an extra step.
		order_of_calls = []
		self._patch("frappe.db.get_value", side_effect=lambda *a, **kw: (
			order_of_calls.append("lock" if kw.get("for_update") else "read")
			or frappe._dict({"name": "SN-001", "gb_reserved": 0, "gb_order": ""})))
		self._patch_object(orders, "serial_shelf_state", side_effect=lambda s: (
			order_of_calls.append("shelf")
			or {"sellable": True, "reason": None, "detail": {}}))
		orders._hold_serial(self._order(), "SN-001")
		self.assertEqual(order_of_calls[:2], ["lock", "shelf"])


class TestReservation(_OrderCase):
	def _order(self):
		order = fx.FakeDoc(name="GB-ORD-00001", gb_order_id="998877",
			order_status="Pending Fulfillment")
		with patch("frappe.db.exists", return_value=True):
			orders.apply_order_lines(order, fx.order_detail())
		return order

	def test_a_matched_line_is_reserved_and_the_storefronts_are_told(self):
		self._patch("frappe.db.exists", return_value=True)
		self._patch_object(orders, "_hold_serial", return_value=("", True))
		order = self._order()
		orders.reserve_order_serials(order)
		self.assertEqual(len(order.serials), 1)
		self.assertEqual(order.serials[0].confirmed_serial, "SN-001")
		self.assertEqual(self.exits, [("SN-001", "sold")])

	def test_re_polling_the_same_order_does_not_re_announce_the_exit(self):
		# Announcing again would queue a redundant delist against both
		# storefronts on every poll for the whole life of the order.
		self._patch("frappe.db.exists", return_value=True)
		self._patch_object(orders, "_hold_serial", return_value=("", False))
		order = self._order()
		orders.reserve_order_serials(order)
		self.assertEqual(self.exits, [])
		self.assertEqual(self.alerts, [])

	def test_a_conflict_still_records_the_order(self):
		# Losing a sale is worse than recording one that needs a human:
		# GunBroker has already taken the buyer's money.
		self._patch("frappe.db.exists", return_value=True)
		self._patch_object(orders, "_hold_serial",
			return_value=("Held by woo WOO-ORD-7", False))
		order = self._order()
		orders.reserve_order_serials(order)
		self.assertEqual(order.lines[0].reservation_error, "Held by woo WOO-ORD-7")
		self.assertIn("gb_conflict_998877", self.alerts)
		self.assertGreaterEqual(order.saves, 1)

	def test_a_gun_whose_sku_named_an_unknown_item_still_gets_one(self):
		# A listing made by hand on GunBroker can carry any SKU it likes. The
		# disposition needs an Item and the Serial No knows which one it is;
		# without this fallback the whole dispose fails validation half way
		# through, after some guns have already been booked.
		self._patch("frappe.db.exists", return_value=True)
		self._patch_object(orders, "_hold_serial", return_value=("", True))
		self._patch("frappe.db.get_value", return_value="COLT-PYTHON")
		order = self._order()
		order.lines[0].item_code = None
		orders.reserve_order_serials(order)
		self.assertEqual(order.serials[0].item_code, "COLT-PYTHON")

	def test_an_unmatched_line_records_the_order_and_alerts(self):
		self._patch("frappe.db.exists", return_value=False)
		order = self._order()
		orders.reserve_order_serials(order)
		self.assertEqual(order.serials, [])
		self.assertTrue(order.lines[0].reservation_error)
		self.assertIn("gb_unmatched_998877", self.alerts)


# ---------------------------------------------------------------------------
# Reacting to what came back
# ---------------------------------------------------------------------------

class TestAcknowledge(_OrderCase):
	def test_a_new_order_is_acknowledged_once(self):
		client = MagicMock()
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", acknowledged_at=None)
		with patch("ffl_core.utils.single_flag", return_value=True):
			orders._acknowledge(client, order)
		client.acknowledge_order.assert_called_once_with("998877")
		self.assertIsNotNone(order.acknowledged_at)

	def test_an_already_acknowledged_order_is_left_alone(self):
		client = MagicMock()
		order = fx.FakeDoc(gb_order_id="998877", acknowledged_at="2026-08-26 10:00:00")
		orders._acknowledge(client, order)
		client.acknowledge_order.assert_not_called()

	def test_a_refusal_switches_the_behaviour_off_instead_of_retrying_for_ever(self):
		# A 403 is a configuration fact, not a transient one. Retrying every
		# fifteen minutes would produce an alert a day and never a fix.
		client = MagicMock()
		client.acknowledge_order.side_effect = GunBrokerError("no", status_code=403)
		order = fx.FakeDoc(gb_order_id="998877", acknowledged_at=None)
		with patch("ffl_core.utils.single_flag", return_value=True):
			orders._acknowledge(client, order)
		self.assertEqual(self.singles.get("auto_acknowledge"), 0)
		self.assertIn("gb_ack_unauthorized", self.alerts)


class TestLayaway(_OrderCase):
	def test_a_layaway_order_is_flagged_for_a_human(self):
		# Instalments are a customer deposit, not a sale. This store does not
		# offer layaway, so one on the account is either an old listing's terms
		# or a mistake — and either way it must not be disposed.
		order = fx.FakeDoc(gb_order_id="998877", gb_status=10)
		orders._warn_on_layaway(order)
		self.assertIn("gb_layaway_998877", self.alerts)

	def test_an_ordinary_order_is_not(self):
		orders._warn_on_layaway(fx.FakeDoc(gb_order_id="998877", gb_status=4))
		self.assertEqual(self.alerts, [])


class TestUnwoundOrders(_OrderCase):
	def test_a_cancellation_before_invoicing_releases_the_gun(self):
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=6,
			order_status="Pending Fulfillment", sales_invoice=None)
		order.append("serials", {"confirmed_serial": "SN-001"})
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"gb_reserved": 1, "gb_order": "GB-ORD-1"}))
		orders._detect_unwound(order)
		self.assertEqual(self.serial_rows["SN-001"], {"gb_reserved": 0, "gb_order": ""})
		self.assertEqual(order.order_status, "Cancelled")
		# Back on the shelf, so the storefronts that took it down are told to
		# put it back up.
		self.assertEqual(self.returns, ["SN-001"])

	def test_a_cancellation_ends_the_sold_listing_on_the_serial(self):
		# GunBroker does not un-sell an item when it cancels the order. Left on
		# the serial, the sweep reads that listing as sold with no order holding
		# the gun and raises "may have sold on GunBroker" for a gun on the shelf.
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=6,
			order_status="Pending Fulfillment", sales_invoice=None)
		order.append("lines", {"gb_item_id": "555111"})
		order.append("serials", {"confirmed_serial": "SN-001"})
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"gb_reserved": 1, "gb_order": "GB-ORD-1", "gb_item_id": "555111",
			 "gb_item_history": "444000"}))
		orders._detect_unwound(order)
		row = self.serial_rows["SN-001"]
		self.assertEqual(row["gb_reserved"], 0)
		self.assertEqual(row["gb_item_id"], "")
		self.assertEqual(row["gb_delisted"], 1)
		self.assertIn("555111", row["gb_item_history"])
		self.assertIn("444000", row["gb_item_history"])

	def test_a_listing_that_is_not_this_orders_stays_on_the_serial(self):
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=6,
			order_status="Pending Fulfillment", sales_invoice=None)
		order.append("lines", {"gb_item_id": "555111"})
		order.append("serials", {"confirmed_serial": "SN-001"})
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"gb_reserved": 1, "gb_order": "GB-ORD-1", "gb_item_id": "777999"}))
		orders._detect_unwound(order)
		self.assertEqual(self.serial_rows["SN-001"], {"gb_reserved": 0, "gb_order": ""})

	def test_another_channels_hold_is_never_released_by_ours(self):
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=6,
			order_status="Pending Fulfillment", sales_invoice=None)
		order.append("serials", {"confirmed_serial": "SN-001"})
		self._patch("frappe.db.get_value", return_value=frappe._dict(
			{"gb_reserved": 1, "gb_order": "GB-ORD-OTHER"}))
		orders._detect_unwound(order)
		self.assertNotIn("SN-001", self.serial_rows)
		self.assertEqual(self.returns, [])

	def test_a_refund_after_invoicing_alerts_and_reverses_nothing(self):
		# GunBroker recomputes its own tax statement when a refund is raised
		# THERE. A POS-side reversal would leave us paying tax on a sale that
		# no longer exists.
		order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=13,
			gb_status_label="Refund Order", order_status="Transferred",
			sales_invoice="ACC-SINV-0001")
		orders._detect_unwound(order)
		self.assertIn("gb_refund_998877", self.alerts)
		self.assertEqual(self.serial_rows, {})

	def test_the_unwound_alerts_carry_the_remarks_rule_with_the_real_order_id(self):
		# Invoiced and shipped-not-invoiced alike: the factual sentence, the
		# pointer to the store's own procedure, and the one rule the cash-basis
		# report needs, with the GunBroker order id it matches on. Nothing in the
		# text may look like markup (it goes into email and the notification list).
		for invoice in ("ACC-SINV-0001", None):
			order = fx.FakeDoc(name="GB-ORD-1", gb_order_id="998877", gb_status=13,
				gb_status_label="Refund Order", order_status="Transferred",
				sales_invoice=invoice)
			sent = []
			self._patch_object(orders, "notify_admins",
				side_effect=lambda s, m, **kw: sent.append((s, m)))
			orders._detect_unwound(order)
			_subject, body = sent[0]
			self.assertIn("Nothing has been reversed", body)
			self.assertIn("Book the refund following your GunBroker refund procedure.", body)
			self.assertIn('Custom Remarks first, then write "GunBroker Order #998877"', body)
			self.assertNotIn("<", body)
			self.assertNotIn("accounting manual", body)

	def test_an_ordinary_status_change_does_nothing(self):
		orders._detect_unwound(fx.FakeDoc(gb_order_id="998877", gb_status=4))
		self.assertEqual(self.alerts, [])


class TestSyncGate(_OrderCase):
	def test_the_cron_entry_never_raises(self):
		# A scheduler tick that throws takes every other integration's job with
		# it.
		self._patch_object(orders, "gunbroker_enabled",
			side_effect=RuntimeError("settings table is missing"))
		orders.sync_orders()  # must not raise

	def test_nothing_is_enqueued_while_the_integration_is_off(self):
		self._patch_object(orders, "gunbroker_enabled", return_value=False)
		enqueue = self._patch("frappe.enqueue")
		orders.sync_orders()
		enqueue.assert_not_called()
