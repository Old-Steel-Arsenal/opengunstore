"""Tests for the dealer-entered serial sync (webhook.py order.updated path).

When a dealer types the firearm's serial on the WooCommerce Edit Order screen,
the storefront plugin saves it to the order-item meta `firearm_serials` and nudges WC to
fire its "Order updated" webhook. The POS reads that serial and applies it to
the matching Woo Online Order line — overriding any serial the webhook
auto-reserved at order time.

Covers:
- _extract_line_serials: read `firearm_serials` from line-item meta_data
- _apply_dealer_serial: validate + reserve the dealer serial, release the old
- _handle_order_updated: route an existing order's update to serial application
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

_INTAKE = patch("ffl_woo_sync.woocommerce.webhook.intake_customer", return_value=None)


def setUpModule():
	# The buyer's POS customer at intake reads and writes Customers for real, which these doubles
	# cannot answer; it is tested on the real database in test_web_order_customer.
	_INTAKE.start()


def tearDownModule():
	_INTAKE.stop()


# Newer Frappe (v16.30+) answers meta / permission questions through
# frappe.db.get_value too, so a blanket ``patch("frappe.db.get_value",
# return_value=…)`` poisons the framework ("'Meta' object has no attribute
# 'istable'", "DocType Serial No not found"). These stubs answer OUR doctypes
# and hand everything the framework asks about back to the real function.
_REAL_GET_VALUE = frappe.db.get_value
_FRAMEWORK_DOCTYPES = frozenset({
	"DocType", "DocField", "DocPerm", "Custom DocPerm", "Custom Field", "Property Setter",
	"Module Def", "User", "Role", "Has Role", "Singles", "System Settings", "Language",
	"Print Format", "Client Script", "Server Script", "Installed Application",
	"Deleted Document", "Page", "Report", "Workflow", "Notification",
})


def _gv_stub(value):
	"""frappe.db.get_value side_effect: ``value`` for application doctypes,
	the real answer for framework ones."""
	def _gv(doctype, *args, **kwargs):
		if doctype in _FRAMEWORK_DOCTYPES:
			return _REAL_GET_VALUE(doctype, *args, **kwargs)
		return value
	return _gv


# ---------------------------------------------------------------------------
# _extract_line_serials
# ---------------------------------------------------------------------------

# Reads the drop-ship layer makes on a site that has it (its listing, and the
# extension switch every osa_dropship entry point checks): answered "not drop-ship".
_DROPSHIP_READS = ("Distributor Listing", "OSA Dropship Listing", "POS Extension")

class TestExtractLineSerials(FrappeTestCase):
	"""Reads dealer-entered serials keyed by WC line id from line-item meta."""

	def test_array_value_extracted(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [
			{"key": "firearm_serials", "value": ["SN-1", "SN-2"]}]}]
		self.assertEqual(_extract_line_serials(line_items), {"7": ["SN-1", "SN-2"]})

	def test_json_string_value_extracted(self):
		"""WC may serialise the array meta value as a JSON string."""
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [
			{"key": "firearm_serials", "value": '["SN-1"]'}]}]
		self.assertEqual(_extract_line_serials(line_items), {"7": ["SN-1"]})

	def test_single_string_value_extracted(self):
		"""A bare string value is treated as a single serial."""
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [
			{"key": "firearm_serials", "value": "SN-1"}]}]
		self.assertEqual(_extract_line_serials(line_items), {"7": ["SN-1"]})

	def test_line_without_serials_key_skipped(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [{"key": "_other", "value": "x"}]}]
		self.assertEqual(_extract_line_serials(line_items), {})

	def test_blank_serials_filtered_out(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [
			{"key": "firearm_serials", "value": ["SN-1", "", "  "]}]}]
		self.assertEqual(_extract_line_serials(line_items), {"7": ["SN-1"]})

	def test_all_blank_line_omitted(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [{"id": 7, "meta_data": [
			{"key": "firearm_serials", "value": ["", "  "]}]}]
		self.assertEqual(_extract_line_serials(line_items), {})

	def test_multiple_lines(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_line_serials
		line_items = [
			{"id": 7, "meta_data": [{"key": "firearm_serials", "value": ["SN-1"]}]},
			{"id": 9, "meta_data": [{"key": "firearm_serials", "value": ["SN-2"]}]},
		]
		self.assertEqual(_extract_line_serials(line_items), {"7": ["SN-1"], "9": ["SN-2"]})


# ---------------------------------------------------------------------------
# _apply_dealer_serial
# ---------------------------------------------------------------------------

def _line(item_code="GLOCK-19", serial="", woo_line_id="7", reservation_error=""):
	return frappe._dict({
		"item_code": item_code, "serial": serial,
		"woo_line_id": woo_line_id, "reservation_error": reservation_error,
	})


def _srow(item_code="GLOCK-19", confirmed_serial="", stock_hold_serial="",
		woo_line_id="7", unit_index=0, reservation_error=""):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"stock_hold_serial": stock_hold_serial, "woo_line_id": woo_line_id,
		"unit_index": unit_index, "reservation_error": reservation_error,
	})


def _order(name="WOO-ORD-00001"):
	o = MagicMock()
	o.name = name
	return o


class TestApplyDealerSerial(FrappeTestCase):
	def test_valid_serial_confirmed_and_reserved(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(stock_hold_serial="SN-HOLD")
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 0, "woo_order": ""})), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._release_serial") as mock_rel, \
			 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"):
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		self.assertEqual(row.confirmed_serial, "SN-NEW")
		self.assertEqual(row.stock_hold_serial, "")
		self.assertEqual(row.reservation_error, "")
		mock_res.assert_called_once_with("SN-NEW", "WOO-ORD-00001")
		mock_rel.assert_called_once_with("SN-HOLD", site="retail")  # placeholder released

	def test_confirm_equal_to_placeholder_does_not_release_it(self):
		"""Dealer names the very gun the placeholder held → promote, do NOT release."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(stock_hold_serial="SN-HOLD")
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 1, "woo_order": "WOO-ORD-00001"})), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._release_serial") as mock_rel, \
			 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"):
			changed = _apply_dealer_serial(_order(), row, "SN-HOLD")
		self.assertTrue(changed)
		self.assertEqual(row.confirmed_serial, "SN-HOLD")
		self.assertEqual(row.stock_hold_serial, "")
		mock_res.assert_called_once_with("SN-HOLD", "WOO-ORD-00001")
		mock_rel.assert_not_called()

	def test_unknown_serial_sets_reservation_error(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", side_effect=_gv_stub(None)), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note"), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "SN-GHOST")
		self.assertTrue(changed)
		self.assertIn("SN-GHOST", row.reservation_error)
		self.assertEqual(row.confirmed_serial, "")
		mock_res.assert_not_called()

	def test_wrong_item_sets_reservation_error(self):
		"""'OTHER' declares no `sells_through_item`, so a mismatch stays a refusal
		— the cross-Item re-bucket only fires for a pairing an Item states."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(item_code="GLOCK-19")
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "OTHER", "status": "Active",
				"woo_reserved": 0, "woo_order": ""})), \
			 patch("ffl_woo_sync.woocommerce.webhook._sells_through", return_value=""), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note"), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		self.assertIn("SN-NEW", row.reservation_error)
		mock_res.assert_not_called()

	def test_inactive_serial_sets_reservation_error(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Delivered",
				"woo_reserved": 0, "woo_order": ""})), \
			 patch("ffl_woo_sync.woocommerce.webhook._suggest_instock_serial",
				 return_value=None), \
			 patch("ffl_woo_sync.woocommerce.webhook._queue_wc_order_note"), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		mock_res.assert_not_called()

	def test_override_releases_previous_confirmed(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(confirmed_serial="SN-OLD")
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 0, "woo_order": ""})), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._release_serial") as mock_rel, \
			 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"):
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		self.assertEqual(row.confirmed_serial, "SN-NEW")
		mock_rel.assert_called_once_with("SN-OLD", site="retail")
		mock_res.assert_called_once_with("SN-NEW", "WOO-ORD-00001")

	def test_serial_reserved_by_other_order_sets_error(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 1, "woo_order": "WOO-ORD-OTHER"})), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertTrue(changed)
		self.assertIn("WOO-ORD-OTHER", row.reservation_error)
		self.assertEqual(row.confirmed_serial, "")
		mock_res.assert_not_called()

	def test_idempotent_when_same_serial_confirmed(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow(confirmed_serial="SN-NEW")
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 1, "woo_order": "WOO-ORD-00001"})), \
			 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as mock_res:
			changed = _apply_dealer_serial(_order(), row, "SN-NEW")
		self.assertFalse(changed)
		mock_res.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_order_updated
# ---------------------------------------------------------------------------

class TestHandleOrderUpdated(FrappeTestCase):
	"""An order.updated for an EXISTING Woo Online Order applies new serials."""

	def test_no_serials_in_body_is_noop(self):
		"""Update with no firearm_serials and no firearm lines awaiting → no doc load."""
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 99, "status": "processing", "line_items": []}
		with patch("frappe.db.get_value", side_effect=_gv_stub(None)) as mock_gv, \
			 patch("frappe.get_doc") as mock_get_doc:
			_handle_order_updated(body)
		mock_get_doc.assert_not_called()

	def test_applies_serials_by_unit_index_to_rows_and_saves(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 99, "status": "processing", "line_items": [
			{"id": 7, "sku": "GLOCK-19", "meta_data": [
				{"key": "firearm_serials", "value": ["SN-1", "SN-2"]}]}]}
		order = MagicMock()
		order.name = "WOO-ORD-00099"
		order.order_status = "Pending Fulfillment"
		order.lines = []
		order.serials = [
			_srow(woo_line_id="7", unit_index=0),
			_srow(woo_line_id="7", unit_index=1),
		]
		with patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-00099")), \
			 patch("frappe.get_doc", return_value=order), \
			 patch("ffl_woo_sync.woocommerce.webhook._apply_dealer_serial",
				   return_value=True) as mock_apply:
			_handle_order_updated(body)
		calls = mock_apply.call_args_list
		self.assertEqual(len(calls), 2)
		by_index = {c.args[1].unit_index: c.args[2] for c in calls}
		self.assertEqual(by_index, {0: "SN-1", 1: "SN-2"})
		order.save.assert_called_once()

	def test_disposed_order_syncs_status_but_does_not_reapply_serial(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 99, "status": "completed", "line_items": [
			{"id": 7, "meta_data": [{"key": "firearm_serials", "value": ["SN-NEW"]}]}]}
		order = MagicMock()
		order.name = "WOO-ORD-00099"
		order.order_status = "Transferred"
		order.woo_status = "processing"
		order.lines = []
		order.serials = [_srow(woo_line_id="7", unit_index=0, confirmed_serial="SN-OLD")]
		with patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-00099")), \
			 patch("frappe.get_doc", return_value=order), \
			 patch("ffl_woo_sync.woocommerce.webhook._apply_dealer_serial") as mock_apply:
			_handle_order_updated(body)
		mock_apply.assert_not_called()
		self.assertEqual(order.woo_status, "completed")

	def test_skips_rest_and_apply_when_no_serial_rows(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 99, "status": "processing", "line_items": [
			{"id": 7, "sku": "AMMO", "meta_data": []}]}
		order = MagicMock()
		order.name = "WOO-ORD-00099"
		order.order_status = "Pending Fulfillment"
		order.lines = []
		order.serials = []
		with patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-00099")), \
			 patch("frappe.get_doc", return_value=order), \
			 patch("ffl_woo_sync.woocommerce.webhook._fetch_order_serials_and_status") as mock_fetch, \
			 patch("ffl_woo_sync.woocommerce.webhook._apply_dealer_serial") as mock_apply:
			_handle_order_updated(body)
		mock_fetch.assert_not_called()
		mock_apply.assert_not_called()

	def test_unknown_order_is_noop(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 12345, "status": "processing", "line_items": [
			{"id": 7, "meta_data": [{"key": "firearm_serials", "value": ["SN-X"]}]}]}
		with patch("frappe.db.get_value", side_effect=_gv_stub(None)), \
			 patch("frappe.get_doc") as mock_get_doc:
			_handle_order_updated(body)
		mock_get_doc.assert_not_called()

	def test_rest_fallback_when_body_lacks_serials(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 99, "status": "processing", "line_items": [
			{"id": 7, "sku": "GLOCK-19", "meta_data": []}]}
		order = MagicMock()
		order.name = "WOO-ORD-00099"
		# The refetch reads the order's OWN woo_order_id (the helper is reused by
		# the retry cron, which has no webhook body to read an id from). Same value
		# either way in production — the order was just looked up BY this id.
		order.woo_order_id = "99"
		order.order_status = "Pending Fulfillment"
		order.lines = []
		order.serials = [_srow(woo_line_id="7", unit_index=0)]
		with patch("ffl_woo_sync.woocommerce.webhook._fetch_order_serials_and_status",
				   return_value=({"7": ["SN-R"]}, "processing")) as mock_fetch, \
			 patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-00099")), \
			 patch("frappe.get_doc", return_value=order), \
			 patch("ffl_woo_sync.woocommerce.webhook._apply_dealer_serial", return_value=True) as mock_apply:
			_handle_order_updated(body)
		mock_fetch.assert_called_once_with("99")
		self.assertEqual(mock_apply.call_args.args[2], "SN-R")


# ---------------------------------------------------------------------------
# _fetch_order_serials_and_status (REST fallback)
# ---------------------------------------------------------------------------

class TestFetchOrderLineSerials(FrappeTestCase):
	"""REST fallback reads firearm_serials from the order's line items."""

	def tearDown(self):
		super().tearDown()
		frappe.flags.woo_sync_site = None

	def test_extracts_from_rest_order(self):
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_serials_and_status
		client_mock = MagicMock()
		client_mock.get_order.return_value = {"line_items": [
			{"id": 7, "meta_data": [{"key": "firearm_serials", "value": ["SN-R"]}]}]}
		client_mock.get_order.return_value["status"] = "processing"
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls:
			MockCls.from_settings.return_value = client_mock
			serials, status = _fetch_order_serials_and_status("42")
		self.assertEqual(serials, {"7": ["SN-R"]})
		# The live status rides along on the same response the serials came from.
		self.assertEqual(status, "processing")

	def test_rest_error_returns_empty(self):
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_serials_and_status
		from ffl_woo_sync.woocommerce.client import WooCommerceError
		client_mock = MagicMock()
		client_mock.get_order.side_effect = WooCommerceError("HTTP 503")
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls, \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error"):
			MockCls.from_settings.return_value = client_mock
			serials, status = _fetch_order_serials_and_status("42")
		self.assertEqual(serials, {})
		# Empty status means UNKNOWN, never "terminal" — a flaky store must not
		# read as a wave of cancellations to the retry sweep.
		self.assertEqual(status, "")

	# ---- Finding 1: serial REST fallback uses the originating site's client --

	def test_no_flag_defaults_to_retail(self):
		"""No woo_sync_site flag → _fetch_order_serials_and_status defaults to 'retail'."""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_serials_and_status
		client_mock = MagicMock()
		client_mock.get_order.return_value = {"line_items": []}
		frappe.flags.woo_sync_site = None
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls:
			MockCls.from_settings.return_value = client_mock
			_fetch_order_serials_and_status("99")
		MockCls.from_settings.assert_called_once_with("retail")

	def test_retail_flag_selects_retail_client(self):
		"""RETAIL order: _fetch_order_serials_and_status calls from_settings('retail').

		Explicit retail flag must use the retail store, not default.
		"""
		from ffl_woo_sync.woocommerce.webhook import _fetch_order_serials_and_status
		client_mock = MagicMock()
		client_mock.get_order.return_value = {"line_items": []}
		frappe.flags.woo_sync_site = "retail"
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient") as MockCls:
			MockCls.from_settings.return_value = client_mock
			_fetch_order_serials_and_status("42")
		MockCls.from_settings.assert_called_once_with("retail")


# ---------------------------------------------------------------------------
# order.created: 'web' tag
# ---------------------------------------------------------------------------

class TestOrderCreationWebTag(FrappeTestCase):
	"""A newly-created Woo Online Order is tagged 'web' so the Pending Order
	page can flag online orders apart from counter orders."""

	def test_web_tag_added_on_creation(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_created
		doc = MagicMock()
		doc.name = "WOO-ORD-00001"
		doc.lines = []
		doc.append.side_effect = lambda table, row: doc.lines.append(row)
		body = {
			"id": 77, "status": "processing",
			"billing": {"first_name": "A", "last_name": "B", "email": "a@b.c"},
			"line_items": [],
			"meta_data": [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}],
		}
		with patch("frappe.db.exists", return_value=None), \
			 patch("frappe.new_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_gv_stub({})), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("frappe.enqueue"):
			_handle_order_created(body)
		# Tag written via _user_tags db.set_value (NOT doc.add_tag, which does a
		# permission check that the Guest webhook user fails).
		tag_calls = [c for c in mock_sv.call_args_list
			if c[0][0] == "Woo Online Order" and c[0][2] == "_user_tags"]
		self.assertEqual(len(tag_calls), 1)
		self.assertEqual(tag_calls[0][0][3], ",web")
		doc.add_tag.assert_not_called()


# ---------------------------------------------------------------------------
# WC status + pricing sync
# ---------------------------------------------------------------------------

class TestExtractPricing(FrappeTestCase):
	def test_extracts_order_totals(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_pricing
		p = _extract_pricing({"currency": "USD", "total": "1234.56", "total_tax": "99.50",
			"shipping_total": "25.00", "discount_total": "10.00"})
		self.assertEqual(p["currency"], "USD")
		self.assertEqual(p["order_total"], 1234.56)
		self.assertEqual(p["total_tax"], 99.50)
		self.assertEqual(p["shipping_total"], 25.00)
		self.assertEqual(p["discount_total"], 10.00)

	def test_missing_fields_default_zero(self):
		from ffl_woo_sync.woocommerce.webhook import _extract_pricing
		p = _extract_pricing({})
		self.assertEqual(p["order_total"], 0.0)
		self.assertEqual(p["currency"], "")


class TestOrderCreatedStatusPricing(FrappeTestCase):
	def test_captures_status_pricing_and_line_pricing(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_created
		doc = MagicMock()
		doc.name = "WOO-ORD-1"
		doc.lines = []
		doc.append.side_effect = lambda t, r: doc.lines.append(r)
		body = {
			"id": 50, "status": "processing", "currency": "USD", "total": "914.00",
			"total_tax": "15.00", "shipping_total": "25.00", "discount_total": "0",
			"billing": {"first_name": "P", "last_name": "Q", "email": "p@q.c"},
			"line_items": [{"id": 1, "sku": "AMMO", "quantity": 1, "price": "874.00",
				"subtotal": "874.00", "total": "874.00", "total_tax": "15.00"}],
			"meta_data": [{"key": "_shipping_fflno", "value": "1-57-x"}],
		}
		with patch("frappe.db.exists", return_value=None), \
			 patch("frappe.new_doc", return_value=doc), \
			 patch("frappe.db.get_value", side_effect=_gv_stub({"is_firearm": 0, "uniform_sell_price": 0})), \
			 patch("frappe.db.set_value"), patch("frappe.enqueue"):
			_handle_order_created(body)
		self.assertEqual(doc.woo_status, "processing")
		self.assertEqual(doc.currency, "USD")
		self.assertEqual(doc.order_total, 914.00)
		self.assertEqual(doc.total_tax, 15.00)
		self.assertEqual(doc.shipping_total, 25.00)
		self.assertEqual(doc.lines[0]["subtotal"], 874.00)
		self.assertEqual(doc.lines[0]["line_total"], 874.00)
		self.assertEqual(doc.lines[0]["line_tax"], 15.00)


class TestOrderUpdatedStatusSync(FrappeTestCase):
	def test_status_change_syncs_to_existing_order(self):
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 60, "status": "completed", "total": "914.00", "line_items": []}
		order = MagicMock()
		order.name = "WOO-ORD-60"
		order.order_status = "Pending Fulfillment"
		order.woo_status = "processing"
		order.lines = []
		order.serials = []
		with patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-60")), \
			 patch("frappe.get_doc", return_value=order):
			_handle_order_updated(body)
		self.assertEqual(order.woo_status, "completed")
		self.assertEqual(order.order_total, 914.00)
		order.save.assert_called_once()

	def test_line_pricing_synced_on_update(self):
		"""order.updated backfills per-line subtotal/total/tax (matched by woo_line_id)."""
		from ffl_woo_sync.woocommerce.webhook import _handle_order_updated
		body = {"id": 70, "status": "processing", "line_items": [
			{"id": 7, "price": "899.00", "subtotal": "899.00", "total": "899.00",
			 "total_tax": "74.17"}]}
		line = _line(item_code="GLOCK-19", serial="SN-1", woo_line_id="7")
		line.subtotal = 0
		line.line_total = 0
		line.line_tax = 0
		line.unit_price = 0
		order = MagicMock()
		order.name = "WOO-ORD-70"
		order.order_status = "Pending Fulfillment"
		order.woo_status = "processing"
		order.lines = [line]
		order.serials = []
		with patch("frappe.db.get_value", side_effect=_gv_stub("WOO-ORD-70")), \
			 patch("frappe.get_doc", return_value=order):
			_handle_order_updated(body)
		self.assertEqual(line.line_total, 899.00)
		self.assertEqual(line.line_tax, 74.17)
		self.assertEqual(line.unit_price, 899.00)
		order.save.assert_called_once()


# ---------------------------------------------------------------------------
# No cross-store fan-out: the retail store is the only one (1.11.0)
# ---------------------------------------------------------------------------

class TestNoCrossStoreFanOut(FrappeTestCase):
	"""Reserving a serial on the retail store used to fan a delist out to the
	second (dealer) store. That store is gone: the order's own push is the only
	outbound job, and no delist is aimed at any other store."""

	def _run_created(self, body, site, other_site_pid="WC-PID"):
		"""Call _handle_order_created with woo_sync_site flag set to *site*,
		patch away all side-effectful helpers, and return the frappe.enqueue
		mock so the caller can inspect what was enqueued.

		*other_site_pid* is the blanket answer to every product-id read — a
		non-empty string means "listed everywhere it is asked about".
		"""
		from ffl_woo_sync.woocommerce.webhook import _handle_order_created
		doc = MagicMock()
		doc.name = "WOO-ORD-00099"
		doc.lines = []
		doc.append.side_effect = lambda t, r: doc.lines.append(r)

		def _gv_except_dropship(doctype=None, name=None, field=None, *a, **k):
			"""`other_site_pid` is a non-empty STRING, and `_fulfillment_lane`
			reads `Item.dropship_distributor` — which is also a string column — so a
			blanket answer would make this ordinary local gun read as drop-ship
			sourced and skip the reservation. Answer that
			one field (and the listing probe behind it) as "not drop-ship"; every
			other read keeps the blanket answer these assertions rely on."""
			if field == "dropship_distributor" or doctype in _DROPSHIP_READS:
				return None
			return other_site_pid

		frappe.flags.woo_sync_site = site
		try:
			with patch("frappe.db.exists", return_value=None), \
				 patch("frappe.new_doc", return_value=doc), \
				 patch("frappe.db.set_value"), \
				 patch("frappe.db.get_value", side_effect=_gv_except_dropship), \
				 patch("ffl_woo_sync.woocommerce.webhook._reserve_serial"), \
				 patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push"), \
				 patch("ffl_woo_sync.woocommerce.webhook._serial_availability",
					   return_value=("", "")), \
				 patch("ffl_woo_sync.woocommerce.webhook.is_regulated_firearm_item",
					   return_value=True), \
				 patch("ffl_woo_sync.woocommerce.webhook.is_firearm_item",
					   return_value=True), \
				 patch("frappe.enqueue") as mock_enqueue:
				_handle_order_created(body)
		finally:
			frappe.flags.woo_sync_site = None
		return mock_enqueue

	@staticmethod
	def _body(order_id, serial):
		"""Minimal order body with one per-gun SKU line so the serial ends up
		in to_reserve (the per-gun SKU path; qty=1 triggers the SKU fallback)."""
		return {
			"id": order_id, "status": "processing",
			"billing": {"first_name": "T", "last_name": "E", "email": "t@example.com"},
			"line_items": [{"id": 7, "sku": f"GLOCK-19::{serial}",
							"quantity": 1, "price": "500.00",
							"subtotal": "500.00", "total": "500.00", "total_tax": "0"}],
			"meta_data": [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}],
		}

	def _delist_calls(self, mock_enqueue):
		return [
			c for c in mock_enqueue.call_args_list
			if c.args and c.args[0] == "ffl_woo_sync.woocommerce.tasks.delist_serial_task"
		]

	def test_a_retail_order_enqueues_no_delist_anywhere(self):
		"""Retail-site order reserving S1 → no delist_serial_task, and nothing
		aimed at a store other than retail."""
		mock_enqueue = self._run_created(self._body(102, "S1"), "retail")
		self.assertEqual(self._delist_calls(mock_enqueue), [])
		self.assertFalse([c for c in mock_enqueue.call_args_list
			if c.kwargs.get("site") not in (None, "retail")])

# ---------------------------------------------------------------------------
# _enqueue_serial_or_item_push is site-aware
# ---------------------------------------------------------------------------

class TestEnqueueSerialOrItemPushSiteAware(FrappeTestCase):
	"""The reservation-driven outbound push targets the originating store: it
	reads the serial's / Item's product_id for *site* and threads site= into the
	task."""

	def test_retail_default_pushes_serial_task_retail(self):
		"""Regression: default site is retail and reads woo_product_id."""
		from ffl_woo_sync.woocommerce.webhook import _enqueue_serial_or_item_push
		with patch("frappe.db.get_value", side_effect=_gv_stub({
				"woo_product_id": "R-1", "item_code": "GLOCK-19"})), \
			 patch("frappe.enqueue") as mock_enq:
			_enqueue_serial_or_item_push("S1")
		mock_enq.assert_called_once()
		self.assertEqual(mock_enq.call_args.args[0],
			"ffl_woo_sync.woocommerce.tasks.push_serial_task")
		self.assertEqual(mock_enq.call_args.kwargs["site"], "retail")
