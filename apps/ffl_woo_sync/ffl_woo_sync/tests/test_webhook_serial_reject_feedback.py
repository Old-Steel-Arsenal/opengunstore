"""Serial-rejection feedback loop (webhook.py order.updated path).

When a dealer-entered serial is rejected (not in stock / wrong item / held by
another order), the rejection must not die silently in the Error Log:

- _suggest_instock_serial: offer the in-stock serial whose normalized form or
  digits match the entered one (the U283116-vs-B283116 misread class of typo).
- _apply_dealer_serial: embed the suggestion in reservation_error, post a
  WooCommerce order note so the person who typed the serial sees the rejection,
  and stay idempotent — the same rejection re-delivered by the next webhook
  must not flip `changed` nor re-post the note.
- _post_wc_order_note: best-effort — a WC API failure logs, never raises.
- list_pending_web_orders: expose the rejection as row["serial_errors"] so the
  Pending Order page can show WHY a row is stuck "awaiting serial".
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double


def _srow(item_code="GLOCK-19", confirmed_serial="", stock_hold_serial="",
		woo_line_id="7", unit_index=0, reservation_error=""):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"stock_hold_serial": stock_hold_serial, "woo_line_id": woo_line_id,
		"unit_index": unit_index, "reservation_error": reservation_error,
	})


def _order(name="WOO-ORD-00001", woo_order_id="1893"):
	o = MagicMock()
	o.name = name
	o.woo_order_id = woo_order_id
	return o


def _sn(name, woo_reserved=0, woo_order=""):
	return frappe._dict({"name": name, "woo_reserved": woo_reserved, "woo_order": woo_order})


def setUpModule():
	# These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
	# the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
	from ffl_core.tests.channel_query_doubles import warm_channel_meta

	warm_channel_meta()


# ---------------------------------------------------------------------------
# _suggest_instock_serial
# ---------------------------------------------------------------------------

class TestSuggestInstockSerial(FrappeTestCase):
	def test_unique_digit_match_suggested(self):
		"""U283116 typed, B283116 in stock — digits match uniquely → suggest it."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([_sn("B283116"), _sn("B275219")])):
			self.assertEqual(
				_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"),
				"B283116")

	def test_normalized_alnum_match_beats_digit_match(self):
		"""'b-283116' normalizes to B283116 exactly → suggested even when another
		serial shares the digits (exact-normalized match is unambiguous)."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([_sn("B283116"), _sn("U283116")])):
			self.assertEqual(
				_suggest_instock_serial("b-283116", "P228-A", "WOO-ORD-00001"),
				"B283116")

	def test_ambiguous_digit_match_returns_none(self):
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([_sn("B283116"), _sn("C283116")])):
			self.assertIsNone(_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"))

	def test_no_match_returns_none(self):
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([_sn("B111111")])):
			self.assertIsNone(_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"))

	def test_candidate_reserved_by_other_order_excluded(self):
		"""A digit-matching serial held by ANOTHER web order must not be suggested."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([
				_sn("B283116", woo_reserved=1, woo_order="WOO-ORD-OTHER")])):
			self.assertIsNone(_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"))

	def test_candidate_held_by_this_order_included(self):
		"""The order's own stock-hold placeholder IS a valid suggestion."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([
				_sn("B283116", woo_reserved=1, woo_order="WOO-ORD-00001")])):
			self.assertEqual(
				_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"),
				"B283116")

	def test_a_customer_or_consigned_out_gun_is_never_suggested(self):
		"""The dealer path refuses those guns, so a hint pointing at one steers a
		typo onto a customer's transfer gun or a gun at a partner FFL."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		double = get_all_double([
			frappe._dict(name="B283116", warehouse="Customer Custody - FT")])
		with patch("frappe.get_all", side_effect=double) as ga, \
			 patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names",
				 return_value={"Customer Custody - FT"}):
			self.assertIsNone(_suggest_instock_serial("U283116", "P228-A", "WOO-ORD-00001"))
		filters = ga.call_args_list[0].kwargs["filters"]
		self.assertEqual(filters["is_transfer_gun"], 0)
		self.assertEqual(filters["consigned_out"], 0)

	def test_entered_serial_without_digits_never_digit_matches(self):
		"""No digits in the entered serial → digit tier must not match everything."""
		from ffl_woo_sync.woocommerce.webhook import _suggest_instock_serial
		with patch("frappe.get_all", side_effect=get_all_double([_sn("NODIGITS")])):
			self.assertIsNone(_suggest_instock_serial("????", "P228-A", "WOO-ORD-00001"))


# ---------------------------------------------------------------------------
# _apply_dealer_serial — rejection embeds suggestion + posts WC note, idempotent
# ---------------------------------------------------------------------------

class TestApplyDealerSerialRejectFeedback(FrappeTestCase):
	def test_unknown_serial_error_includes_suggestion_and_posts_note(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="P228-A")
		with patch("frappe.db.get_value", return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value="B283116"), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note") as mock_note, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "U283116")
		self.assertTrue(changed)
		self.assertIn("U283116", row.reservation_error)
		self.assertIn("B283116", row.reservation_error)
		mock_note.assert_called_once()
		note_text = mock_note.call_args[0][1]
		self.assertIn("U283116", note_text)
		self.assertIn("B283116", note_text)
		self.assertEqual(mock_note.call_args[0][0], "1893")

	def test_same_rejection_is_idempotent_no_second_note(self):
		"""The next order.updated re-delivers the same bad serial: no change, no
		second note, no needless order.save()."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="P228-A")
		with patch("frappe.db.get_value", return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note") as mock_note, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			first = _apply_dealer_serial(_order(), row, "U283116")
			second = _apply_dealer_serial(_order(), row, "U283116")
		self.assertTrue(first)
		self.assertFalse(second)
		self.assertEqual(mock_note.call_count, 1)

	def test_different_rejection_posts_again(self):
		"""Dealer fixes the serial to ANOTHER bad value → new error, new note."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="P228-A")
		with patch("frappe.db.get_value", return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note") as mock_note, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			first = _apply_dealer_serial(_order(), row, "U283116")
			second = _apply_dealer_serial(_order(), row, "U999999")
		self.assertTrue(first)
		self.assertTrue(second)
		self.assertEqual(mock_note.call_count, 2)
		self.assertIn("U999999", row.reservation_error)

	def _not_ours(self, **flags):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="P228-A")
		locked = {"item_code": "P228-A", "status": "Active", "woo_reserved": 0,
			"woo_order": "", "warehouse": "Stores - FT", "is_transfer_gun": 0,
			"consigned_out": 0, **flags}
		with patch("frappe.db.get_value", return_value=locked), \
			 patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names",
				 return_value={"Customer Custody - FT"}), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as reserve, \
			 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note"), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "B275219")
		return changed, row, reserve

	def test_a_customers_transfer_gun_is_never_pinned(self):
		"""Transfer guns share the web listing's Item and are Active and never
		woo_reserved: before this, staff typing one pinned it to the web order
		and dispose shipped the customer's gun to the buyer."""
		changed, row, reserve = self._not_ours(is_transfer_gun=1)
		self.assertTrue(changed)
		self.assertIn("transfer", row.reservation_error)
		self.assertFalse(row.confirmed_serial)
		reserve.assert_not_called()

	def test_a_gun_in_the_custody_warehouse_is_never_pinned(self):
		changed, row, reserve = self._not_ours(warehouse="Customer Custody - FT")
		self.assertTrue(changed)
		self.assertIn("transfer", row.reservation_error)
		reserve.assert_not_called()

	def test_a_consigned_out_gun_is_never_pinned(self):
		changed, row, reserve = self._not_ours(consigned_out=1)
		self.assertTrue(changed)
		self.assertIn("consigned out", row.reservation_error)
		reserve.assert_not_called()

	def test_an_ordinary_shelf_gun_is_still_pinned(self):
		changed, row, reserve = self._not_ours()
		self.assertTrue(changed)
		self.assertEqual(row.confirmed_serial, "B275219")
		reserve.assert_called_once()

	def test_reserved_by_other_order_rejection_idempotent_and_noted(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="P228-A")
		with patch("frappe.db.get_value", return_value={
				"item_code": "P228-A", "status": "Active",
				"woo_reserved": 1, "woo_order": "WOO-ORD-OTHER"}), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note") as mock_note, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			first = _apply_dealer_serial(_order(), row, "B275219")
			second = _apply_dealer_serial(_order(), row, "B275219")
		self.assertTrue(first)
		self.assertFalse(second)
		self.assertEqual(mock_note.call_count, 1)
		self.assertIn("WOO-ORD-OTHER", mock_note.call_args[0][1])

	def test_valid_serial_posts_no_note(self):
		"""The feedback loop is for rejections only — a clean apply stays silent."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="GLOCK-19", stock_hold_serial="SN-HOLD")
		with patch("frappe.db.get_value", return_value={
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 0, "woo_order": ""}), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial"), \
			 patch("ffl_woo_sync.woocommerce.webhook._release_serial"), \
			 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note") as mock_note:
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		mock_note.assert_not_called()


# ---------------------------------------------------------------------------
# _post_wc_order_note — best-effort, never raises
# ---------------------------------------------------------------------------

class TestPostWcOrderNote(FrappeTestCase):
	def test_posts_via_client(self):
		from ffl_woo_sync.woocommerce import webhook
		client = MagicMock()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client) as mock_from:
			webhook._post_wc_order_note("1893", "POS: serial rejected", "retail")
		mock_from.assert_called_once_with("retail")
		client.create_order_note.assert_called_once_with("1893", "POS: serial rejected")

	def test_client_failure_logs_and_swallows(self):
		from ffl_woo_sync.woocommerce import webhook
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				side_effect=RuntimeError("boom")), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error") as mock_log:
			webhook._post_wc_order_note("1893", "note", "retail")  # must not raise
		mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# WooCommerceClient.create_order_note
# ---------------------------------------------------------------------------

class TestClientCreateOrderNote(FrappeTestCase):
	def test_posts_to_order_notes_endpoint(self):
		from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceConfig
		cfg = WooCommerceConfig(
			store_url="https://shop.example.com", consumer_key="ck",
			consumer_secret="cs", timeout=5)
		client = WooCommerceClient(cfg, session=MagicMock())
		with patch.object(client, "post", return_value={"id": 1}) as mock_post:
			client.create_order_note("1893", "hello")
		mock_post.assert_called_once_with(
			"orders/1893/notes", json={"note": "hello", "customer_note": False})


# ---------------------------------------------------------------------------
# list_pending_web_orders — expose serial_errors
# ---------------------------------------------------------------------------

class TestPendingQueueSerialErrors(FrappeTestCase):
	def _orders(self):
		return [frappe._dict({
			"name": "WOO-ORD-1", "woo_status": "completed", "woo_order_id": "1893",
			"customer_name": "Noah", "atf_ffl_record": "", "ffl_license": "3-37-x",
			"ffl_business_name": "BERGER", "ffl_expiration": "2027-04-30",
			"creation": "2026-07-08 01:28:34"})]

	def test_reservation_error_surfaces_as_serial_errors(self):
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		doc = MagicMock()
		doc.serials = [_srow(item_code="P228-A",
			reservation_error="Dealer-entered serial 'U283116' is not a valid in-stock serial")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", side_effect=get_all_double(self._orders())), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-08"):
			rows = list_pending_web_orders()
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["serial_errors"], [{
			"item_code": "P228-A",
			"error": "Dealer-entered serial 'U283116' is not a valid in-stock serial"}])

	def test_no_error_yields_empty_serial_errors(self):
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		doc = MagicMock()
		doc.serials = [_srow(item_code="P228-A")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", side_effect=get_all_double(self._orders())), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-08"):
			rows = list_pending_web_orders()
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["serial_errors"], [])
