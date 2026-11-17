"""Tests for woocommerce/fulfillment.py — the web-order fulfillment queue.

list_pending_web_orders surfaces paid Woo Online Orders on the POS Pending
Order page; dispose_web_order books one bound-book FFL Disposition per firearm
serial row (stock-out + FastBound via the disposition's own on_submit hooks)
without creating a Sales Invoice (decision: disposition-only — Woo already
collected).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _srow(item_code="GLOCK-19", confirmed_serial="", woo_line_id="7", unit_index=0):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"woo_line_id": woo_line_id, "unit_index": unit_index,
		"stock_hold_serial": "", "reservation_error": "",
	})


def _woo_order(name="WOO-ORD-00001", status="Pending Fulfillment", atf_ffl_record="",
		woo_order_id="101", serials=None, woo_status="completed"):
	o = MagicMock()
	o.name = name
	o.order_status = status
	o.woo_status = woo_status  # live WC/payment status; paid by default
	o.atf_ffl_record = atf_ffl_record
	o.woo_order_id = woo_order_id
	o.ffl_license = "1-99-888-01-2D-09999"
	o.ffl_business_name = "ACME Guns"
	o.ffl_expiration = "2027-04-30"
	o.customer_name = "John Smith"
	o.site = "retail"
	o.serials = serials if serials is not None else []
	return o


class TestListPendingWebOrders(FrappeTestCase):

	def test_awaiting_serial_row(self):
		"""A firearm unit with no confirmed serial → awaiting_serial, not dispose-ready."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-1", "woo_status": "completed", "woo_order_id": "101", "customer_name": "John",
			"atf_ffl_record": "", "ffl_license": "1-57-x", "ffl_business_name": "ACME",
			"ffl_expiration": "2027-04-30", "creation": "2026-06-01 10:00:00"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-06-19"):
			rows = list_pending_web_orders()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["source"], "woo")
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertFalse(rows[0]["needs_disposition"])
		self.assertTrue(rows[0]["paid"])
		self.assertEqual(rows[0]["woo_online_order"], "WOO-ORD-1")

	def test_ready_row_with_pending_serial(self):
		"""A firearm unit with a confirmed serial + valid ATF record → dispose-ready."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-1", "woo_status": "processing", "woo_order_id": "101", "customer_name": "John",
			"atf_ffl_record": "1-57-x", "ffl_license": "1-57-x", "ffl_business_name": "ACME",
			"ffl_expiration": "2027-04-30", "creation": "2026-06-01 10:00:00"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="SN-1")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", return_value="2099-12-31"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item",
				   return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-06-19"):
			rows = list_pending_web_orders()
		self.assertEqual(rows[0]["pending_serials"],
			[{"item_code": "GLOCK-19", "serial": "SN-1"}])
		self.assertTrue(rows[0]["needs_disposition"])
		self.assertFalse(rows[0]["awaiting_serial"])
		self.assertFalse(rows[0]["ffl_expired"])

	def test_row_carries_the_orders_site(self):
		"""Each row carries its order's own site."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-D1", "site": "retail", "woo_status": "completed", "woo_order_id": "201",
			"customer_name": "Dealer LLC", "atf_ffl_record": "", "ffl_license": "1-57-x",
			"ffl_business_name": "Dealer LLC", "ffl_expiration": "2027-04-30",
			"creation": "2026-06-01 10:00:00"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="SN-D")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", return_value="2099-12-31"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item",
				   return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-06-19"):
			rows = list_pending_web_orders()
		self.assertEqual(rows[0]["site"], "retail")

	def test_atf_expiration_code_does_not_pollute_message_log(self):
		"""Regression: a flat ffl_expiration that's an ATF expiration *code* (e.g.
		"9C", not a date) must yield an unverified row WITHOUT surfacing a
		"9C is not a valid date string." popup. getdate() reports invalid dates via
		frappe.throw, which appends to message_log even when the raise is caught — so
		the message rides back in _server_messages (HTTP 200) and the Pending Order
		page pops it up. The row must be unverified and the message log untouched."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-9C", "woo_status": "completed", "woo_order_id": "1295", "customer_name": "David",
			"atf_ffl_record": "", "ffl_license": "5-99-888-01-00-11762",
			"ffl_business_name": "ALAMO", "ffl_expiration": "9C",
			"creation": "2026-06-23 17:49:10"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="U484472")]
		msgs_before = len(frappe.message_log)
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-06-23"):
			rows = list_pending_web_orders()
		self.assertEqual(len(rows), 1)
		self.assertIsNone(rows[0]["ffl_expiry_date"])
		self.assertNotIn("ffl_unverified", rows[0], "the unresolved state is not a row key")
		self.assertFalse(rows[0]["ffl_expired"])
		self.assertEqual(
			len(frappe.message_log), msgs_before,
			"ATF expiration code must not append a date-parse error to message_log")

	def test_excludes_unpaid_pending_woo_status(self):
		"""The bug: a WooCommerce order in 'pending' (payment not received) reserves
		its serial but must NOT enter the POS Pending Order queue — a gun cannot ship
		for an unpaid order. Repro: WC status pending."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-UP", "woo_status": "pending", "woo_order_id": "1782",
			"customer_name": "Sam Testbuyer", "atf_ffl_record": "1-57-x", "ffl_license": "1-57-x",
			"ffl_business_name": "ACME", "ffl_expiration": "2027-04-30",
			"creation": "2026-07-01 14:40:11"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="SN-UP")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", return_value="2099-12-31"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item",
				   return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-01"):
			rows = list_pending_web_orders()
		self.assertEqual(rows, [], "an unpaid (WC pending) order must not appear in the queue")

	def test_excludes_cancelled_woo_status(self):
		"""A WooCommerce-cancelled order must auto-drop from the Pending Order queue.
		The cancel webhook already flips order_status to 'Cancelled' (the DB filter
		then excludes it); this asserts the paid-status guard as the second line of
		defense — 'cancelled' is not a shippable status."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-CX", "woo_status": "cancelled", "woo_order_id": "1790",
			"customer_name": "Jane", "atf_ffl_record": "1-57-x", "ffl_license": "1-57-x",
			"ffl_business_name": "ACME", "ffl_expiration": "2027-04-30",
			"creation": "2026-07-01 09:00:00"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="SN-CX")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", return_value="2099-12-31"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item",
				   return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-01"):
			rows = list_pending_web_orders()
		self.assertEqual(rows, [], "a cancelled order must not appear in the queue")

	def test_includes_processing_woo_status(self):
		"""'processing' (payment received, awaiting fulfillment) IS shippable and
		must appear in the queue — mirror of the exclude tests."""
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-PR", "woo_status": "processing", "woo_order_id": "1791",
			"customer_name": "Ann", "atf_ffl_record": "1-57-x", "ffl_license": "1-57-x",
			"ffl_business_name": "ACME", "ffl_expiration": "2027-04-30",
			"creation": "2026-07-01 11:00:00"})]
		doc = MagicMock()
		doc.serials = [_srow(confirmed_serial="SN-PR")]
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("frappe.db.get_value", return_value="2099-12-31"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item",
				   return_value=False), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-07-01"):
			rows = list_pending_web_orders()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["woo_online_order"], "WOO-ORD-PR")


class TestPendingWebOrderDistributorLegs(FrappeTestCase):
	"""The card lists the order's distributor orders (with the house's web
	reference) and, for a to_store unit, does not ask WooCommerce for a serial —
	it arrives with the distributor receive."""

	def _rows(self, serials, dos, antique=False, lines=(), dropship_status=""):
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"dropship_status": dropship_status,
			"name": "WOO-ORD-TS", "woo_status": "awaiting-stock", "woo_order_id": "15353",
			"customer_name": "Test Buyer", "atf_ffl_record": "", "ffl_license": "9-99-999-01-9B-00001",
			"ffl_business_name": "Test Dealer", "ffl_expiration": "2027-04-30",
			"creation": "2026-10-09 22:46:48"})]
		doc = MagicMock()
		doc.serials = serials
		doc.lines = [frappe._dict(ln) for ln in lines]

		def get_all(doctype, *args, **kwargs):
			return [frappe._dict(d) for d in dos] if doctype == "Distributor Order" else orders

		with patch("frappe.only_for"), \
			 patch("frappe.get_all", side_effect=get_all), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.is_antique_item", return_value=antique), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.nowdate", return_value="2026-10-10"):
			return list_pending_web_orders()

	def test_the_row_carries_its_distributor_orders_with_the_web_ref(self):
		row = dict(_srow(confirmed_serial=""), fulfillment="to_store")
		rows = self._rows([frappe._dict(row)], [{
			"source_name": "WOO-ORD-TS", "name": "DO-2610-0002", "distributor": "RSR",
			"status": "Placed", "order_type": "Procure To Store",
			"receive_status": "Not Received", "web_ref": "3203100"}])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["distributor_orders"], [{
			"name": "DO-2610-0002", "distributor": "RSR", "status": "Placed",
			"order_type": "Procure To Store", "receive_status": "Not Received",
			"web_ref": "3203100"}])
		self.assertTrue(rows[0]["to_store"])
		self.assertFalse(rows[0]["arrived"])
		# Awaiting, but the serial is the distributor's to bring — not Woo's.
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["awaiting_serial_lanes"], ["to_store"])

	def test_a_local_unit_still_awaits_woocommerce(self):
		rows = self._rows([_srow(confirmed_serial="")], [])
		self.assertEqual(rows[0]["awaiting_serial_lanes"], ["local"])
		self.assertEqual(rows[0]["distributor_orders"], [])

	def test_an_fds_unit_is_not_asked_of_woocommerce(self):
		# Its serial is the distributor's ship-notice echo.
		rows = self._rows([frappe._dict(_srow(confirmed_serial=""), fulfillment="fds")], [])
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["awaiting_serial_lanes"], ["fds"])

	def test_a_local_antique_awaits_woocommerce(self):
		rows = self._rows([_srow(confirmed_serial="")], [], antique=True)
		self.assertTrue(rows[0]["awaiting_serial"])
		self.assertEqual(rows[0]["awaiting_serial_lanes"], ["local"])

	def test_a_cancelled_leg_shows_but_does_not_hold_arrived(self):
		# It never arrives; its replacement did.
		row = dict(_srow(confirmed_serial=""), fulfillment="to_store")
		rows = self._rows([frappe._dict(row)], [
			{"source_name": "WOO-ORD-TS", "name": "DO-1", "distributor": "RSR", "status": "Cancelled",
				"order_type": "Procure To Store", "receive_status": "Not Received"},
			{"source_name": "WOO-ORD-TS", "name": "DO-2", "distributor": "RSR", "status": "Complete",
				"order_type": "Procure To Store", "receive_status": "Received"}])
		self.assertEqual([d["name"] for d in rows[0]["distributor_orders"]], ["DO-1", "DO-2"])
		self.assertTrue(rows[0]["arrived"])

	def test_a_to_store_order_whose_only_leg_was_cancelled_stays_queued(self):
		# An accessory parcel is still owed until it is re-ordered — no serial
		# row and no live leg must not drop it out of every staff surface.
		rows = self._rows([], [
			{"source_name": "WOO-ORD-TS", "name": "DO-1", "distributor": "RSR", "status": "Cancelled",
				"order_type": "Procure To Store", "receive_status": "Not Received"}],
			lines=[{"item_code": "ACC-1", "lane": "to_store"}])
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["to_store"])
		self.assertFalse(rows[0]["arrived"])

	_DROPSHIP_LEG = {"source_name": "WOO-ORD-TS", "name": "DO-1", "distributor": "RSR",
		"order_type": "Dropship Accessory", "receive_status": "Not Received"}

	def _dropship_rows(self, status, dropship_status="Ordered", lane="direct"):
		# A pure accessory order: no serial row, no to_store line.
		dos = [dict(self._DROPSHIP_LEG, status=status)] if status else []
		with patch("ffl_woo_sync.woocommerce.fulfillment._cannot_prove_actionless",
				return_value=""):
			return self._rows([], dos, lines=[{"item_code": "ACC-1", "lane": lane}],
				dropship_status=dropship_status)

	def test_a_drop_ship_order_with_an_unplaced_leg_stays_queued(self):
		# Nobody has sent the distributor order yet — placing it is the action owed.
		rows = self._dropship_rows("Draft")
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["to_place"])
		self.assertEqual(rows[0]["stage"], "to_place")

	def test_a_drop_ship_order_leaves_the_queue_once_its_leg_is_placed(self):
		# From here the distributor ships it; POS has nothing left to do.
		self.assertEqual(self._dropship_rows("Placed"), [])

	def test_a_drop_ship_order_nobody_has_sourced_stays_queued(self):
		# Never routed, or its only distributor order failed or was cancelled.
		for dropship_status, do_status in (("Pending Route", ""), ("Error", "Cancelled")):
			rows = self._dropship_rows(do_status, dropship_status=dropship_status)
			self.assertEqual([r["stage"] for r in rows], ["to_place"], dropship_status)

	def test_a_store_shipped_order_is_not_waiting_on_a_distributor(self):
		# Every web order starts at Pending Route; only a distributor line makes
		# that work owed.
		self.assertEqual(
			self._dropship_rows("", dropship_status="Pending Route", lane="local"), [])

	def test_the_lane_and_status_words_are_the_routers(self):
		from ffl_integrations.distributor import router
		from ffl_woo_sync.woocommerce import fulfillment
		self.assertEqual(fulfillment._DISTRIBUTOR_LANES, router.DROPSHIP_LANES)
		self.assertEqual(fulfillment._UNSOURCED_STATUSES,
			{router.DS_PENDING_ROUTE, router.DS_ERROR})

	def test_a_placed_leg_does_not_mark_a_firearm_order_to_place(self):
		row = dict(_srow(confirmed_serial="SN-1"))
		rows = self._rows([frappe._dict(row)], [dict(self._DROPSHIP_LEG, status="Placed")])
		self.assertFalse(rows[0]["to_place"])
		self.assertNotEqual(rows[0]["stage"], "to_place")


class TestDisposeWebOrder(FrappeTestCase):

	def setUp(self):
		super().setUp()
		# These tests exercise the DISPOSITION mechanics; the revenue Sales
		# Invoice dispose now also books is covered by test_web_revenue — stub
		# it out so its real builder doesn't run against these mock orders.
		patcher = patch(
			"ffl_woo_sync.woocommerce.fulfillment.create_sales_invoice_for_web_order",
			return_value={"sales_invoice": None, "status": "stubbed"})
		patcher.start()
		self.addCleanup(patcher.stop)
		# The Transferred stamp reads the module clock; with frappe.get_doc
		# stubbed to mock orders, the real clock's system-settings lookup would
		# cache a mock. Pin it here for every case (explicit per-test stubs nest).
		for target in (
			"ffl_woo_sync.woocommerce.fulfillment.now_datetime",
			# mark_transferred stamps from the doctype module.
			"ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order."
			"now_datetime",
		):
			clock = patch(target, return_value="2026-06-19 12:00:00")
			clock.start()
			self.addCleanup(clock.stop)

	def _patch_company(self):
		def _gv(doctype, name, field=None, *a, **k):
			if doctype == "Serial No":
				# product_id lookups belong to the post-dispose delist branch; return
				# None there so these disposition-creation tests don't fan out a delist.
				return None if field and "product_id" in field else "WH-Main"
			if doctype == "Warehouse":
				return "Test Store A LLC"
			return None
		return patch("frappe.db.get_value", side_effect=_gv)

	def test_delist_disposed_branches_per_serial_vs_item_level(self):
		"""Dispose-time delist: a per-serial firearm delists its own product; an
		item-level (uniform-price) firearm re-pushes its parent Item's stock; an
		unlisted serial enqueues nothing (no guaranteed-failing delist_serial)."""
		from ffl_woo_sync.woocommerce import fulfillment

		def _gv(doctype, name, field=None, *a, **k):
			if doctype == "Serial No":
				return "P-100" if name == "S-PER" else None  # only S-PER is per-serial listed
			if doctype == "Item":
				return "P-200" if name == "ITEM-LEVEL" else None  # only this Item is listed
			return None

		calls = []
		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.enqueue",
				   side_effect=lambda path, **k: calls.append(
					   (path.split(".")[-1], k.get("serial_no"), k.get("item_code")))):
			fulfillment._delist_disposed(
				[("ANY", "S-PER"), ("ITEM-LEVEL", "S-ITEM"), ("UNLISTED", "S-NONE")], "retail")
		self.assertIn(("delist_serial_task", "S-PER", None), calls)
		self.assertIn(("push_stock_task", None, "ITEM-LEVEL"), calls)
		self.assertEqual(len(calls), 2)  # S-NONE (neither listed) enqueues nothing

	def test_creates_disposition_per_firearm_serial(self):
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(serials=[_srow(confirmed_serial="SN-1")])
		disp = MagicMock()
		disp.name = "DISP-2026-00001"
		created_payload = {}

		def _get_doc(arg, *a, **k):
			if isinstance(arg, dict):
				created_payload.update(arg)
				return disp
			return order

		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", side_effect=_get_doc), \
			 patch("frappe.db.exists", return_value=None), \
			 self._patch_company(), \
			 patch("frappe.db.set_value") as mock_sv, \
			 patch("ffl_woo_sync.woocommerce.fulfillment.now_datetime",
				   return_value="2026-06-19 12:00:00"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl",
				   return_value="1-99-888-01-2D-09999"):
			result = dispose_web_order("WOO-ORD-00001")

		self.assertTrue(result["ok"])
		self.assertEqual(result["disposed_count"], 1)
		disp.insert.assert_called_once()
		disp.submit.assert_called_once()
		self.assertEqual(created_payload["doctype"], "FFL Disposition")
		self.assertEqual(created_payload["disposition_type"], "FFL Transfer")
		self.assertEqual(created_payload["serial"], "SN-1")
		self.assertEqual(created_payload["destination_ffl"], "1-99-888-01-2D-09999")
		self.assertEqual(created_payload["woo_online_order"], "WOO-ORD-00001")
		self.assertEqual(created_payload["company"], "Test Store A LLC")
		st = [c for c in mock_sv.call_args_list
			if c[0][0] == "Woo Online Order" and isinstance(c[0][2], dict)
			and "order_status" in c[0][2]]
		self.assertTrue(any(c[0][2]["order_status"] == "Transferred" for c in st))
		self.assertTrue(all(c[0][2].get("transferred_at") for c in st))  # revenue date stamped

	def test_creates_one_disposition_per_unit_multi_gun(self):
		"""qty=2 uniform firearm → two serial rows → two dispositions."""
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(serials=[
			_srow(confirmed_serial="SN-1", unit_index=0),
			_srow(confirmed_serial="SN-2", unit_index=1),
		])
		disp = MagicMock()
		serials_booked = []

		def _get_doc(arg, *a, **k):
			if isinstance(arg, dict):
				serials_booked.append(arg["serial"])
				return disp
			return order

		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", side_effect=_get_doc), \
			 patch("frappe.db.exists", return_value=None), \
			 self._patch_company(), \
			 patch("frappe.db.set_value"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.now_datetime",
				   return_value="2026-06-19 12:00:00"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl",
				   return_value="1-99-888-01-2D-09999"):
			result = dispose_web_order("WOO-ORD-00001")
		self.assertEqual(result["disposed_count"], 2)
		self.assertEqual(sorted(serials_booked), ["SN-1", "SN-2"])

	def test_idempotent_skips_existing_disposition(self):
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(serials=[_srow(confirmed_serial="SN-1")])
		disp = MagicMock()

		def _get_doc(arg, *a, **k):
			return disp if isinstance(arg, dict) else order

		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", side_effect=_get_doc), \
			 patch("frappe.db.exists", return_value="DISP-2026-00001"), \
			 self._patch_company(), \
			 patch("frappe.db.set_value"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl",
				   return_value="1-99-888-01-2D-09999"):
			result = dispose_web_order("WOO-ORD-00001")

		self.assertEqual(result["disposed_count"], 0)
		disp.insert.assert_not_called()

	def test_throws_when_awaiting_serial(self):
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(serials=[_srow(confirmed_serial="")])
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order):
			with self.assertRaises(frappe.ValidationError):
				dispose_web_order("WOO-ORD-00001")

	def test_throws_when_any_unit_awaiting(self):
		"""Mixed (one serial entered, one still awaiting) must not partially dispose."""
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(serials=[
			_srow(confirmed_serial="SN-1", woo_line_id="7", unit_index=0),
			_srow(confirmed_serial="", woo_line_id="7", unit_index=1),
		])
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order):
			with self.assertRaises(frappe.ValidationError):
				dispose_web_order("WOO-ORD-00001")

	def test_throws_when_not_pending(self):
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(status="Transferred", serials=[_srow(confirmed_serial="SN-1")])
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order):
			with self.assertRaises(frappe.ValidationError):
				dispose_web_order("WOO-ORD-00001")

	def test_throws_when_unpaid(self):
		"""Defense in depth: even called directly, dispose must refuse an unpaid
		(WC pending) order — a firearm cannot ship for an order not yet paid."""
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		order = _woo_order(woo_status="pending", serials=[_srow(confirmed_serial="SN-1")])
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order):
			with self.assertRaises(frappe.ValidationError):
				dispose_web_order("WOO-ORD-00001")
