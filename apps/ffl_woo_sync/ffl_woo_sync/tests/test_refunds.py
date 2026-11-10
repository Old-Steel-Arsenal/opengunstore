"""Tests for woocommerce/refunds.py — WooCommerce partial-refund sync.

Woo keeps ``order.total`` at the GROSS amount forever; a partial refund shows up
only as a row in the order's ``refunds`` array (``{id, total<0}``). These tests
pin:

- parsing + idempotent recording of Woo refunds on the Woo Online Order
  (deduped by Woo refund id — a re-delivered webhook must add nothing);
- the net target (order_total − Σ refunds) and the discount that books it
  (never a phantom −1 firearm line);
- the reconcile decision tree: no invoice yet → defer; SI already at net (a
  hand-corrected order) → no-op; SI still at gross → amend;
- the cancel+amend sequence and its COMPLIANCE guard (refuse to cancel any
  invoice that links an FFL Disposition — the bound book must never move);
- the terminal-status alert (cancel/trash/full-refund after invoicing must
  never be silent).
"""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from ffl_woo_sync.woocommerce import refunds


# --------------------------------------------------------------------- doubles


class _StubOrder:
	"""A Woo Online Order stand-in with a real ``refunds`` child list."""

	def __init__(self, name="WOO-ORD-00007", order_total=603.50, woo_order_id="9002",
			woo_status="completed", refunds=None, total_tax=0.0, shipping_total=0.0):
		self.name = name
		self.order_total = order_total
		self.total_tax = total_tax
		self.shipping_total = shipping_total
		self.woo_order_id = woo_order_id
		self.woo_status = woo_status
		self.customer_name = "John Smith"
		self.refunds = list(refunds) if refunds else []
		self.flags = frappe._dict()
		self.saved = False

	def append(self, table, row):
		d = frappe._dict(row)
		getattr(self, table).append(d)
		return d

	def save(self):
		self.saved = True

	def reload(self):
		pass


class _StubSI:
	"""A Sales Invoice stand-in that records the amend lifecycle."""

	def __init__(self, name="ACC-SINV-49", grand_total=603.50, insert_grand_total=None,
			woo_online_order="WOO-ORD-00007", update_stock=0, posting_date="2026-06-23"):
		self.name = name
		self.grand_total = grand_total
		self._insert_gt = insert_grand_total if insert_grand_total is not None else grand_total
		self.woo_online_order = woo_online_order
		self.update_stock = update_stock
		self.posting_date = posting_date
		self.company = "Test Store A LLC"
		self.flags = frappe._dict()
		self.cancelled = False
		self.inserted = False
		self.submitted = False
		self.amended_from = None
		self.apply_discount_on = None
		self.discount_amount = None

	def cancel(self):
		self.cancelled = True

	def insert(self):
		self.inserted = True
		self.grand_total = self._insert_gt  # "ERPNext computed the totals"

	def submit(self):
		self.submitted = True

	def reload(self):
		pass


@contextlib.contextmanager
def woo_refunds_http(refunds_json=None, fail=False, status=200, config_error=False):
	"""The HTTP boundary, and only it: a real WooCommerceClient (dummy config, no
	Settings needed) whose session answers ``GET orders/<id>/refunds`` with
	``refunds_json`` (a list = page 1, or a callable(page)), with HTTP ``status``,
	or fails like a dead store (``fail`` = connection error, "timeout" = read
	timeout). ``config_error``: the settings themselves are unusable. Yields the
	(method, url, page) calls."""
	import requests

	from ffl_woo_sync.woocommerce.client import WooCommerceConfig, WooCommerceConfigError

	cfg = WooCommerceConfig(store_url="https://woo.test", consumer_key="ck",
		consumer_secret="cs", timeout=5)
	calls = []

	def request(self, method, url, **kwargs):
		page = (kwargs.get("params") or {}).get("page", 1)
		calls.append((method, url, page))
		if fail == "timeout":
			raise requests.ReadTimeout("store too slow")
		if fail:
			raise requests.ConnectionError("store unreachable")
		resp = MagicMock(status_code=status, content=b"[]", text="woo says no")
		body = refunds_json(page) if callable(refunds_json) else (
			list(refunds_json or []) if page == 1 else [])
		resp.json.return_value = body
		return resp

	settings = ({"side_effect": WooCommerceConfigError("Consumer Key is missing.")}
		if config_error else {"return_value": cfg})
	with patch.object(WooCommerceConfig, "from_settings", **settings), \
		 patch("requests.Session.request", request):
		yield calls


# Woo refund objects (REST orders/<id>/refunds), A10 figures: shipping refunded
# as cost 41.57 + tax 3.43; a goods line refunded with its tax; a custom amount.
SHIPPING_REFUND = {"id": 7001, "amount": "45.00", "line_items": [], "fee_lines": [],
	"shipping_lines": [{"id": 1, "total": "-41.57", "total_tax": "-3.43"}]}
GOODS_REFUND = {"id": 7002, "amount": "108.25", "shipping_lines": [],
	"line_items": [{"id": 2, "quantity": -1, "total": "-100.00", "total_tax": "-8.25"}]}
CUSTOM_REFUND = {"id": 7003, "amount": "20.00", "line_items": [], "shipping_lines": []}


# ------------------------------------------------------------- parse + record


class TestParseRefunds(FrappeTestCase):

	def test_parses_id_amount_reason_and_stores_amount_positive(self):
		rows = refunds._woo_refund_rows(
			{"refunds": [{"id": 9003, "total": "-9.99", "reason": "price adj"}]})
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["woo_refund_id"], "9003")
		self.assertEqual(rows[0]["refund_amount"], 9.99)  # Woo sends -9.99; stored +9.99
		self.assertEqual(rows[0]["reason"], "price adj")

	def test_skips_rows_without_an_id(self):
		self.assertEqual(refunds._woo_refund_rows({"refunds": [{"total": "-9.99"}]}), [])

	def test_skips_zero_total_rows(self):
		self.assertEqual(refunds._woo_refund_rows({"refunds": [{"id": 5, "total": "0.00"}]}), [])

	def test_missing_or_empty_refunds_returns_empty(self):
		self.assertEqual(refunds._woo_refund_rows({}), [])
		self.assertEqual(refunds._woo_refund_rows({"refunds": None}), [])
		self.assertEqual(refunds._woo_refund_rows({"refunds": []}), [])


class TestRecordRefunds(FrappeTestCase):

	def test_recording_marks_pending_and_never_calls_woo(self):
		"""The webhook request only records — the breakdown read is the
		long-queue reconcile's job."""
		order = _StubOrder(refunds=[])
		with woo_refunds_http([SHIPPING_REFUND]) as calls:
			self.assertTrue(refunds.record_order_refunds(
				order, {"refunds": [{"id": 7001, "total": "-45.00"}]}))
		self.assertEqual(calls, [])
		self.assertEqual(order.refunds[0].breakdown_status, "Pending")
		self.assertTrue(refunds.has_pending_breakdown(order))

	def test_adds_new_row_and_reports_changed(self):
		order = _StubOrder(refunds=[])
		changed = refunds.record_order_refunds(
			order, {"refunds": [{"id": 9003, "total": "-9.99"}]})
		self.assertTrue(changed)
		self.assertEqual(len(order.refunds), 1)
		self.assertEqual(order.refunds[0].woo_refund_id, "9003")
		self.assertEqual(order.refunds[0].refund_amount, 9.99)

	def test_dedupes_an_already_recorded_refund_id(self):
		order = _StubOrder(refunds=[frappe._dict(woo_refund_id="9003", refund_amount=9.99)])
		changed = refunds.record_order_refunds(
			order, {"refunds": [{"id": 9003, "total": "-9.99"}]})
		self.assertFalse(changed)
		self.assertEqual(len(order.refunds), 1)

	def test_adds_only_the_new_refund_when_partially_overlapping(self):
		order = _StubOrder(refunds=[frappe._dict(woo_refund_id="9003", refund_amount=9.99)])
		changed = refunds.record_order_refunds(order, {"refunds": [
			{"id": 9003, "total": "-9.99"}, {"id": 2222, "total": "-5.00"}]})
		self.assertTrue(changed)
		self.assertEqual({r.woo_refund_id for r in order.refunds}, {"9003", "2222"})


class TestRefundBreakdown(FrappeTestCase):
	"""Each refund's tax and shipping come
	from WooCommerce's own refund object, read by the reconcile job, checked,
	and only then allowed to move tax / shipping."""

	def _reconcile(self, order, woo=None, **http):
		"""_reconcile with no invoice yet: exercises the breakdown read only.
		Child-row writes are captured (stub rows are not in the database)."""
		with woo_refunds_http(woo, **http) as calls, \
			 patch("frappe.db.get_value"), \
			 patch("frappe.db.set_value") as set_value, \
			 patch.object(refunds, "_existing_web_si", return_value=None), \
			 patch("ffl_core.alerts.alert_once") as alert:
			result = refunds._reconcile(order)
		self.set_value, self.alert = set_value, alert
		return result, calls, alert

	def _pending(self, refund_id="7001", amount=45.0, **order_kw):
		order = _StubOrder(order_total=758.36, total_tax=57.80, shipping_total=41.57, **order_kw)
		order.refunds.append(frappe._dict(name=f"row-{refund_id}", woo_refund_id=refund_id,
			refund_amount=amount, breakdown_status="Pending",
			recorded_on=frappe.utils.now_datetime()))
		return order

	def test_breakdown_of_shipping_goods_custom_and_fee_refunds(self):
		self.assertEqual(refunds._refund_breakdown(SHIPPING_REFUND), (3.43, 41.57))
		self.assertEqual(refunds._refund_breakdown(GOODS_REFUND), (8.25, 0.0))
		self.assertEqual(refunds._refund_breakdown(CUSTOM_REFUND), (0.0, 0.0))
		self.assertEqual(refunds._refund_breakdown(
			{"fee_lines": [{"total": "-10", "total_tax": "-0.83"}]}), (0.83, 0.0))

	def test_reconcile_reads_the_pending_breakdown_onto_the_child_row_only(self):
		order = self._pending()
		result, calls, alert = self._reconcile(order, [SHIPPING_REFUND, GOODS_REFUND])
		self.assertEqual(result["status"], "no_invoice")
		self.assertEqual(calls, [("GET", "https://woo.test/wp-json/wc/v3/orders/9002/refunds", 1)])
		row = order.refunds[0]
		self.assertEqual((row.breakdown_status, row.tax_refunded, row.shipping_refunded),
			("Fetched", 3.43, 41.57))
		# Written on the child row, the parent order is never saved
		self.set_value.assert_called_once_with("Woo Online Order Refund", "row-7001",
			{"breakdown_status": "Fetched", "tax_refunded": 3.43, "shipping_refunded": 41.57,
				"returned_lines": "[]"}, update_modified=False)
		self.assertFalse(order.saved)
		self.assertEqual(refunds.refund_split(order), (3.43, 41.57))
		alert.assert_not_called()

	def test_errors_are_classified(self):
		"""404 = gone (terminal Not in Woo); 401/403/config = persistent
		(Pending, one config alert); timeout / connection / 5xx = transient
		(Pending, one outage alert)."""
		cases = [
			(dict(status=404), "Not in Woo", "booked as a goods discount"),
			(dict(status=401), "Pending", "refused reading refunds"),
			(dict(status=403), "Pending", "refused reading refunds"),
			(dict(config_error=True), "Pending", "refused reading refunds"),
			(dict(fail="timeout"), "Pending", "refund breakdown not read"),
			(dict(fail=True), "Pending", "refund breakdown not read"),
			(dict(status=503), "Pending", "refund breakdown not read"),
		]
		for http, status, title in cases:
			with self.subTest(**{k: str(v) for k, v in http.items()}):
				order = self._pending()
				self._reconcile(order, **http)
				self.assertEqual(order.refunds[0].breakdown_status, status)
				self.assertIsNone(refunds.refund_split(order))  # → all a goods discount
				alert = self.alert.call_args
				self.assertIn(title, alert.kwargs["title"])
				self.assertTrue(alert.kwargs["dedupe_on_title"])

	def test_refund_woo_does_not_list_is_settled_not_re_read(self):
		"""A successful read without the id is terminal — alerted once, booked
		the old way, never fetched again."""
		order = self._pending()
		_result, _calls, alert = self._reconcile(order, [GOODS_REFUND])
		self.assertEqual(order.refunds[0].breakdown_status, "Not in Woo")
		self.assertIsNone(refunds.refund_split(order))
		alert.assert_called_once()
		self.assertIn("refund 7001", alert.call_args.kwargs["title"])
		_result, calls, _alert = self._reconcile(order, [SHIPPING_REFUND])
		self.assertEqual(calls, [])

	def test_a_breakdown_that_does_not_add_up_is_rejected_with_zero_amounts(self):
		"""Bounds; a Rejected row carries no amounts — the raw ones
		are in the alert only."""
		cases = {
			"exceed the refund": ({"id": 7001, "shipping_lines": [
				{"total": "-41.57", "total_tax": "-3.43"}]}, 40.0),
			"order's tax": ({"id": 7001, "line_items": [
				{"total": "-100", "total_tax": "-60.00"}]}, 160.0),
			"order's shipping": ({"id": 7001, "shipping_lines": [
				{"total": "-50.00", "total_tax": "0"}]}, 50.0),
		}
		for why, (woo_refund, amount) in cases.items():
			with self.subTest(why=why):
				order = self._pending(amount=amount)
				_r, _c, alert = self._reconcile(order, [woo_refund])
				row = order.refunds[0]
				self.assertEqual((row.breakdown_status, row.tax_refunded, row.shipping_refunded),
					("Rejected", 0.0, 0.0))
				self.assertIsNone(refunds.refund_split(order))
				message = alert.call_args.kwargs["message"]
				self.assertIn(why, message)
				tax, ship = refunds._refund_breakdown(woo_refund)
				self.assertIn(f"Woo reported tax {tax:.2f}, shipping {ship:.2f}", message)
		# Cumulative: a second refund's tax on top of an accepted first one.
		order = self._pending(refund_id="7002", amount=60.0)
		order.refunds.insert(0, frappe._dict(woo_refund_id="7001", refund_amount=100.0,
			breakdown_status="Fetched", tax_refunded=55.0, shipping_refunded=0))
		_r, _c, alert = self._reconcile(order, [{"id": 7002, "line_items": [
			{"total": "-55", "total_tax": "-5.00"}]}])
		self.assertEqual(order.refunds[1].breakdown_status, "Rejected")
		self.assertEqual(refunds.refund_split(order), (55.0, 0.0))

	def test_manual_reconcile_never_says_already_net_while_a_breakdown_is_unread(self):
		"""The invoice is at the net, but a refund's split is unknown."""
		order = self._pending()
		si = frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=713.36,
			discount_amount=45.0, ffl_shipping_amount=41.57, company="Test Store A LLC")
		with woo_refunds_http(fail=True), \
			 patch("frappe.db.get_value"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch.object(refunds, "_existing_web_si", return_value=si), \
			 patch.object(refunds, "_amend_si_to_net") as amend, \
			 patch("ffl_core.alerts.alert_once"):
			result = refunds.reconcile_web_order_refund(order.name)
		self.assertEqual(result["status"], "pending_breakdown")
		amend.assert_not_called()

	def test_split_targets_leave_tax_in_the_discount_without_a_woo_tax_row(self):
		"""No Woo tax row on the invoice → the refunded tax stays a
		discount (the old booking); a split with no tax never looks up
		the sales-tax account."""
		order = _StubOrder(order_total=758.36, total_tax=57.80, shipping_total=41.57,
			refunds=[frappe._dict(refund_amount=45.0, breakdown_status="Fetched",
				tax_refunded=3.43, shipping_refunded=41.57)])
		with patch("ffl_woo_sync.woocommerce.revenue._sales_tax_account",
				return_value="Sales Tax Payable - TSA"):
			rows, new_tax, new_ship, goods = refunds._refund_split_targets(
				frappe._dict(company="Test Store A LLC", taxes=[]), order)
		self.assertEqual((rows, new_tax, new_ship, goods), ([], None, 0.0, 3.43))
		order.refunds[0].update(tax_refunded=0.0, refund_amount=41.57)
		with patch("ffl_woo_sync.woocommerce.revenue._sales_tax_account",
				side_effect=AssertionError("no tax moves → no lookup")):
			self.assertEqual(refunds._refund_split_targets(
				frappe._dict(company="Test Store A LLC", taxes=[frappe._dict()]), order),
				([], None, 0.0, 0.0))

	def test_listing_reads_every_page(self):
		"""More than one page of refunds."""
		from ffl_woo_sync.woocommerce.client import WooCommerceClient
		pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}, {"id": 101}, {"id": 102}]}
		with woo_refunds_http(lambda page: pages.get(page, [])) as calls:
			listed = WooCommerceClient.from_settings("retail").list_order_refunds(9002)
		self.assertEqual(len(listed), 103)
		self.assertEqual([c[2] for c in calls], [1, 2])

	def test_listing_stops_on_a_repeated_page_and_at_the_page_cap(self):
		"""A server that ignores ``page`` (same 100 every time) ends after
		the first repeat; an endless one ends at max_pages."""
		from ffl_woo_sync.woocommerce.client import WooCommerceClient
		same = [{"id": i} for i in range(100)]
		with woo_refunds_http(lambda page: same) as calls:
			listed = WooCommerceClient.from_settings("retail").list_order_refunds(9002)
		self.assertEqual((len(listed), [c[2] for c in calls]), (100, [1, 2]))
		endless = lambda page: [{"id": page * 1000 + i} for i in range(100)]
		with woo_refunds_http(endless) as calls:
			listed = WooCommerceClient.from_settings("retail").list_order_refunds(
				9002, max_pages=3)
		self.assertEqual((len(listed), len(calls)), (300, 3))


class TestTotals(FrappeTestCase):

	def test_total_refunded_sums_recorded_rows(self):
		order = _StubOrder(refunds=[
			frappe._dict(refund_amount=9.99), frappe._dict(refund_amount=5.0)])
		self.assertEqual(refunds.total_refunded(order), 14.99)

	def test_total_refunded_zero_without_rows(self):
		self.assertEqual(refunds.total_refunded(_StubOrder(refunds=[])), 0.0)

	def test_target_net_is_order_total_minus_refunds(self):
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		self.assertAlmostEqual(refunds.target_net(order), 593.51, places=2)


class TestApplyDiscount(FrappeTestCase):

	def test_sets_net_total_discount_when_refunds_exist(self):
		si = frappe._dict()
		order = _StubOrder(refunds=[frappe._dict(refund_amount=9.99)])
		refunds.apply_refund_discount(si, order)
		self.assertEqual(si.apply_discount_on, "Net Total")
		self.assertAlmostEqual(si.discount_amount, 9.99, places=2)

	def test_noop_when_no_refunds(self):
		si = frappe._dict()
		refunds.apply_refund_discount(si, _StubOrder(refunds=[]))
		self.assertNotIn("discount_amount", si)


# ---------------------------------------------------------------- reconcile


class TestReconcile(FrappeTestCase):

	def _run(self, existing_si, order):
		# The stock-leg probe is a real query and has its own tests (_held below);
		# here the decision tree is what's under test.
		with patch("frappe.db.get_value") as gv, \
			 patch.object(refunds, "_existing_web_si", return_value=existing_si), \
			 patch.object(refunds, "_amend_si_to_net", return_value="ACC-SINV-49-1") as amend, \
			 patch("ffl_woo_sync.woocommerce.revenue.stock_leg_cancel_blockers",
				return_value=[]), \
			 patch("frappe.log_error"):
			result = refunds._reconcile(order)
		return result, amend, gv

	def test_no_refund_is_a_noop(self):
		order = _StubOrder(refunds=[])
		result, amend, _gv = self._run(
			frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=603.50), order)
		self.assertEqual(result["status"], "no_refund")
		amend.assert_not_called()

	def test_no_submitted_invoice_defers(self):
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		result, amend, _gv = self._run(None, order)
		self.assertEqual(result["status"], "no_invoice")
		amend.assert_not_called()

	def test_draft_invoice_defers(self):
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		si = frappe._dict(name="ACC-SINV-49", docstatus=0, grand_total=603.50)
		result, amend, _gv = self._run(si, order)
		self.assertEqual(result["status"], "no_invoice")
		amend.assert_not_called()

	def test_already_net_invoice_is_left_alone_protecting_manual_fix(self):
		# The SI is already 593.51 (a hand-fixed order). Recording
		# the refund must NOT re-amend and double-deduct.
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		si = frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=593.51)
		result, amend, _gv = self._run(si, order)
		self.assertEqual(result["status"], "already_net")
		amend.assert_not_called()

	def test_gross_invoice_is_amended_to_net(self):
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		si = frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=603.50)
		result, amend, _gv = self._run(si, order)
		self.assertEqual(result["status"], "adjusted")
		amend.assert_called_once()
		name_arg, order_arg, target_arg = amend.call_args.args
		self.assertEqual(name_arg, "ACC-SINV-49")
		self.assertAlmostEqual(target_arg, 593.51, places=2)

	def _held(self, *, blockers=(), grand_total=603.50, discount_amount=0,
			refund_amounts=(9.99,)):
		"""Reconcile an order whose amend must be refused BEFORE anything moves."""
		order = _StubOrder(order_total=603.50, refunds=[
			frappe._dict(refund_amount=amount) for amount in refund_amounts])
		existing = frappe._dict(name="ACC-SINV-49", docstatus=1,
			grand_total=grand_total, discount_amount=discount_amount)
		with patch("frappe.db.get_value"), \
			 patch.object(refunds, "_existing_web_si", return_value=existing), \
			 patch.object(refunds, "_amend_si_to_net") as amend, \
			 patch("ffl_woo_sync.woocommerce.revenue.stock_leg_cancel_blockers",
				return_value=list(blockers)), \
			 patch("ffl_core.alerts.alert_once") as alert:
			result = refunds._reconcile(order)
		return result, amend, alert

	def test_held_when_a_person_holds_the_stock_leg(self):
		"""_amend_si_to_net cancels the collection Payment Entry first, so a refusal
		found halfway would roll the refund back and record nothing."""
		result, amend, alert = self._held(
			blockers=["Delivery Note DN-R is a sales return of this invoice's stock"])
		self.assertEqual(result["status"], "held")
		amend.assert_not_called()
		alert.assert_called_once()
		self.assertIn("DN-R", alert.call_args.kwargs["message"])

	def test_held_when_the_invoice_would_not_reach_the_woo_net(self):
		"""The amend is a Net-Total discount of the refunds, so the amended total is
		arithmetically knowable up front — check it while nothing is half-undone."""
		result, amend, alert = self._held(grand_total=700.00)
		self.assertEqual(result["status"], "held")
		amend.assert_not_called()
		self.assertIn("would come to", alert.call_args.kwargs["message"])

	def test_a_clean_order_is_not_held(self):
		result, amend, alert = self._held()
		self.assertEqual(result["status"], "adjusted")
		amend.assert_called_once()
		alert.assert_not_called()

	def test_a_second_partial_refund_replaces_the_existing_discount(self):
		"""The invoice already carries the first refund as an absolute discount;
		the second reconcile must replace it with the cumulative amount, not deduct
		the cumulative refunds from the already-net grand total."""
		result, amend, alert = self._held(
			grand_total=593.51, discount_amount=9.99,
			refund_amounts=(9.99, 5.00))
		self.assertEqual(result["status"], "adjusted")
		self.assertAlmostEqual(amend.call_args.args[2], 588.51, places=2)
		alert.assert_not_called()

	def test_takes_a_row_lock_before_deciding(self):
		# minor-3: two concurrent order.updated must not race a cancel against an
		# already-cancelled SI — serialise on the order row up front.
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		si = frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=603.50)
		_result, _amend, gv = self._run(si, order)
		lock_calls = [c for c in gv.call_args_list
			if c.args and c.args[0] == "Woo Online Order" and c.kwargs.get("for_update")]
		self.assertEqual(len(lock_calls), 1)

	def test_full_refund_alerts_instead_of_amending_to_zero(self):
		# Line refunds that zero out the order while woo_status is still
		# "completed" → don't post a $0 SI; alert for a human instead.
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=603.50)])
		si = frappe._dict(name="ACC-SINV-49", docstatus=1, grand_total=603.50)
		result, amend, _gv = self._run(si, order)
		self.assertEqual(result["status"], "full_refund_alert")
		amend.assert_not_called()


# ------------------------------------------------------ amend sequence + guard


class TestAmendSequence(FrappeTestCase):

	def _run(self, si, amended, order, *, receive_pes, disposition_linked=False):
		"""Run _amend_si_to_net with all externals patched; return the mocks."""
		pe = MagicMock()

		def _get_doc(doctype, name=None, *a, **k):
			if doctype == "Sales Invoice":
				return si
			if doctype == "Payment Entry":
				return pe
			raise AssertionError(doctype)

		m = frappe._dict(pe=pe)
		with contextlib.ExitStack() as stack:
			stack.enter_context(patch("frappe.get_doc", side_effect=_get_doc))
			stack.enter_context(patch("frappe.copy_doc", return_value=amended))
			stack.enter_context(patch("frappe.db.exists",
				return_value=("DISP-1" if disposition_linked else None)))
			stack.enter_context(patch("ffl_core.api.manual_order._receive_payment_entries",
				return_value=receive_pes))
			m.paid = stack.enter_context(patch.object(refunds, "_ensure_paid"))
			m.cancel_dn = stack.enter_context(patch(
				"ffl_woo_sync.woocommerce.revenue.cancel_web_stock_lines", return_value=["DN-1"]))
			m.deliver = stack.enter_context(patch(
				"ffl_woo_sync.woocommerce.revenue.deliver_web_stock_lines", return_value=["DN-2"]))
			m.reassert = stack.enter_context(
				patch.object(refunds, "_reassert_web_listing_reserved"))
			m.log = stack.enter_context(patch("frappe.log_error"))
			m.alert = stack.enter_context(patch("ffl_core.alerts.alert_once"))
			m.result = refunds._amend_si_to_net("ACC-SINV-49", order, 593.51)
		return m

	def test_cancels_payment_then_invoice_then_amends_and_repays(self):
		si = _StubSI(name="ACC-SINV-49", grand_total=603.50, posting_date="2026-06-23")
		amended = _StubSI(name="ACC-SINV-49-1", grand_total=0.0, insert_grand_total=593.51,
			posting_date="2026-06-23")
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		m = self._run(si, amended, order, receive_pes=[frappe._dict(name="PE-1")])
		self.assertTrue(m.pe.cancel.called)          # payment reversed first
		self.assertTrue(si.cancelled)                # then the gross invoice
		self.assertEqual(amended.amended_from, "ACC-SINV-49")
		self.assertEqual(amended.discount_amount, 9.99)   # net via discount, no fake qty
		self.assertTrue(amended.submitted)
		# minor-1: re-collection PE booked on the SI's OWN (preserved) posting date.
		m.paid.assert_called_once_with(amended, order, posting_date="2026-06-23")
		m.reassert.assert_not_called()               # success self-heals via the real submit
		self.assertEqual(m.result, "ACC-SINV-49-1")
		# Churn-suppression flag restored (no leak).
		self.assertIsNone(frappe.flags.get("woo_sync_inbound"))

	def test_stock_leg_is_cancelled_before_the_invoice_and_reissued_after(self):
		"""An accessory web SI carries a submitted Delivery Note pointing back at
		it (revenue.deliver_web_stock_lines); Frappe refuses to cancel an SI a
		submitted DN links, so the DN goes first — and comes back on the amended
		invoice, on its preserved date, so COGS never leaves the sale's month."""
		events = []
		si = _StubSI(name="ACC-SINV-49", grand_total=603.50, posting_date="2026-06-23")
		amended = _StubSI(name="ACC-SINV-49-1", grand_total=0.0, insert_grand_total=593.51,
			posting_date="2026-06-23")
		orig_cancel, orig_submit = si.cancel, amended.submit
		si.cancel = lambda: (events.append("si.cancel"), orig_cancel())[1]
		amended.submit = lambda: (events.append("amended.submit"), orig_submit())[1]
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		pe = MagicMock()

		def _get_doc(doctype, name=None, *a, **k):
			return si if doctype == "Sales Invoice" else pe

		with patch("frappe.get_doc", side_effect=_get_doc), \
			 patch("frappe.copy_doc", return_value=amended), \
			 patch("frappe.db.exists", return_value=None), \
			 patch("ffl_core.api.manual_order._receive_payment_entries", return_value=[]), \
			 patch.object(refunds, "_ensure_paid"), \
			 patch("ffl_woo_sync.woocommerce.revenue.cancel_web_stock_lines",
				side_effect=lambda s_: events.append("dn.cancel") or ["DN-1"]) as cancel_dn, \
			 patch("ffl_woo_sync.woocommerce.revenue.deliver_web_stock_lines",
				side_effect=lambda s_: events.append("dn.reissue") or ["DN-2"]) as deliver:
			refunds._amend_si_to_net("ACC-SINV-49", order, 593.51)
		self.assertEqual(events, ["dn.cancel", "si.cancel", "amended.submit", "dn.reissue"])
		self.assertIs(cancel_dn.call_args.args[0], si)
		self.assertIs(deliver.call_args.args[0], amended)

	def test_amend_drift_restores_listing_status_and_leaves_draft(self):
		si = _StubSI(name="ACC-SINV-49", grand_total=603.50)
		# Insert lands far from the 593.51 target → never submit a wrong number.
		amended = _StubSI(name="ACC-SINV-49-1", grand_total=0.0, insert_grand_total=580.00)
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		m = self._run(si, amended, order, receive_pes=[])
		self.assertFalse(amended.submitted)          # left as a draft for a human
		m.paid.assert_not_called()
		# The cancel reset firearms to "Listed"; drift path must re-Reserve.
		m.reassert.assert_called_once_with(amended)
		# The drift alert must SAY the stock leg was reversed: the goods shipped,
		# the Bin has them back, and Woo is oversold until the draft is submitted.
		m.alert.assert_called_once()
		self.assertIn("DN-1", m.alert.call_args.kwargs["message"])
		self.assertIn("oversold", m.alert.call_args.kwargs["message"])

	def test_refuses_to_cancel_an_invoice_linked_to_a_disposition(self):
		# The compliance backstop: a web SI never links a disposition (they link by
		# woo_online_order), but if one ever did, we must NOT cancel it — cancelling
		# would reverse the bound book / FastBound entry.
		si = _StubSI(name="ACC-SINV-49", grand_total=603.50)
		amended = _StubSI(name="ACC-SINV-49-1")
		order = _StubOrder(order_total=603.50, refunds=[frappe._dict(refund_amount=9.99)])
		with self.assertRaises(frappe.ValidationError):
			self._run(si, amended, order, receive_pes=[], disposition_linked=True)
		self.assertFalse(si.cancelled)               # aborted before any cancel
		self.assertIsNone(frappe.flags.get("woo_sync_inbound"))  # no flag leak on abort


class _RealWebOrder:
	"""Real web-order fixtures: a Transferred order whose gross SI is submitted
	and paid (or, submit=False, just the order)."""

	def _web_item(self) -> str:
		# Pin a deterministic NON-firearm item group. A bare
		# get_value("Item Group", {"is_group": 0}) is order-undefined and can land
		# on a firearm leaf (e.g. "Long Gun") — auto_set_is_firearm then stamps
		# is_firearm=1 and the SI validate demands a serial on the line.
		# Create our own group so the fixture never depends on a site's leaf order.
		group = "QA Refund Accessory"
		if not frappe.db.exists("Item Group", group):
			frappe.get_doc({
				"doctype": "Item Group", "item_group_name": group,
				"parent_item_group": "All Item Groups", "is_group": 0,
			}).insert(ignore_permissions=True)
		name = "QA-REFUND-ITEM"
		if not frappe.db.exists("Item", name):
			doc = frappe.get_doc({
				"doctype": "Item", "item_code": name, "item_name": name,
				"item_group": group, "stock_uom": "Nos",
				"is_stock_item": 0, "is_sales_item": 1, "is_firearm": 0,
			})
			doc.flags.ignore_permissions = True
			doc.insert()
		return name

	def _paid_gross_web_order(self, order_total=625.0, posting_date=None,
			total_tax=0.0, shipping_total=0.0, submit=True):
		"""A real Transferred web order whose gross SI is submitted AND paid — the
		refund is recorded AFTER, so reconcile must amend gross → net.

		posting_date back-dates the sale (None → today, _build_invoice's own
		default). The web item is non-stock and the invoice is update_stock=0, so
		an older date moves no stock ledger — only the GL, which is the point."""
		from ffl_woo_sync.woocommerce import revenue

		item = self._web_item()
		line_total = flt(order_total - total_tax - shipping_total, 2)
		order = frappe.get_doc({
			"doctype": "Woo Online Order",
			"woo_order_id": f"RT-{frappe.generate_hash(length=8)}",
			"order_status": "Transferred", "woo_status": "completed", "site": "retail",
			"customer_name": "QA Buyer", "customer_email": "qa-buyer@example.com",
			"customer_phone": "555-0100", "currency": "USD",
			"order_total": order_total, "total_tax": total_tax,
			"shipping_total": shipping_total, "discount_total": 0.0,
			"lines": [{"item_code": item, "qty": 1, "line_total": line_total,
				"unit_price": line_total, "woo_line_id": "1"}],
		})
		order.flags.ignore_permissions = True
		order.insert()
		if not submit:
			return order
		si = revenue._build_invoice(order, revenue._default_company())  # gross (no refund yet)
		if posting_date:
			# _build_invoice already set set_posting_time=1, so the date sticks
			# (and copy_doc carries both onto the amended invoice).
			si.posting_date = posting_date
		si.submit()
		revenue._ensure_paid(si, order)  # real Receive PE → Woo Clearing (the collection)
		return order


class TestReconcileRealAmend(_RealWebOrder, FrappeTestCase):
	"""Execution-level regression: amending a paid web SI.

	With a REAL submitted web SI + its collection Payment Entry, cancelling the PE
	bumps the SI's ``modified`` timestamp (outstanding_amount changes), so a later
	``si.cancel()`` on the stale in-memory doc raises ``TimestampMismatchError``.
	The unit suite mocked ``copy_doc``/``get_doc``/``cancel`` and could not see this
	— this drives the whole reconcile with real documents so the reload fix stays
	pinned. No mocks on the cancel/PE interaction; no commits (class rollback).
	"""

	def setUp(self):
		# The earlier tests below record refunds Woo has no breakdown for.
		self.enterContext(woo_refunds_http([]))

	def test_reconcile_amends_paid_order_to_net_without_timestamp_error(self):
		from ffl_woo_sync.woocommerce import refunds

		# The sale is 35 days old — the real shape of this path (the refund
		# arrives, and is reconciled, in a LATER month than the sale). It also
		# makes "the sale's date" and "today" two different answers, which is what
		# the re-collect date assertions at the bottom need to mean anything.
		sale_date = add_days(nowdate(), -35)
		order = self._paid_gross_web_order(order_total=625.0, posting_date=sale_date)
		# Refund lands AFTER the sale was booked → gross SI must be amended to net.
		refunds.record_order_refunds(order, {"refunds": [{"id": "5001", "total": "-9.99"}]})
		order.save(ignore_permissions=True)

		# Frappe's copy_doc keeps docstatus when frappe.in_test is True
		# (document.py: `if not frappe.in_test: fields_to_clear.append("docstatus")`),
		# so amending a cancelled SI under FrappeTestCase would raise
		# DocstatusTransitionError (0→2). Production runs the reconcile in a background
		# job (frappe.in_test False) where copy_doc clears docstatus — a real-order
		# E2E run confirms the whole path. Neutralise
		# ONLY that harness quirk here by patching the module-level frappe.in_test —
		# NOT frappe.flags.in_test, which stays True so ffl_core.utils.commit() remains
		# a no-op (no real commit, no cross-run poisoning). Without the
		# si.reload() in _amend_si_to_net this raises TimestampMismatchError instead.
		with patch.object(frappe, "in_test", False):
			result = refunds._reconcile(order)

		self.assertEqual(result["status"], "adjusted")
		amended = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertEqual(amended.docstatus, 1)
		self.assertAlmostEqual(flt(amended.grand_total), 615.01, places=2)  # 625 − 9.99
		self.assertEqual([flt(i.qty) for i in amended.items], [1.0])  # no phantom -1 line
		self.assertEqual(amended.apply_discount_on, "Net Total")
		# Regression: the order carries a billing email, so it booked to a
		# REAL per-buyer Customer (not the aggregate) — and the cancel+amend must
		# carry that customer through onto the amended SI unchanged.
		buyer = frappe.db.get_value("Customer", {"email_id": "qa-buyer@example.com"}, "name")
		self.assertTrue(buyer)
		self.assertEqual(amended.customer, buyer)
		self.assertNotEqual(amended.customer, "Web Store Customer")

		# The re-collect keeps the sale's month — BOTH dates on the voucher.
		# _amend_si_to_net pins the Receive PE's posting_date to the amended SI's
		# preserved date, and ffl_core._book_payment_entry dates the bank
		# reference with the entry rather than with the clock: ERPNext's
		# set_remarks stamps "Transaction reference no X dated <reference_date>"
		# into the voucher, so a receipt booked into the sale's month must not
		# also claim it was referenced today. This is the only place in the repo
		# that path is asserted — revert either half and this block reds.
		self.assertEqual(getdate(amended.posting_date), getdate(sale_date))
		self.assertNotEqual(getdate(sale_date), getdate(nowdate()))  # fixture is discriminating
		pe_name = frappe.db.get_value("Payment Entry Reference",
			{"reference_name": amended.name, "docstatus": 1}, "parent")
		self.assertTrue(pe_name, "the amended net invoice must be re-collected")
		pe = frappe.get_doc("Payment Entry", pe_name)
		self.assertEqual(getdate(pe.posting_date), getdate(sale_date))
		self.assertEqual(getdate(pe.reference_date), getdate(sale_date))
		# The deposit's reference is the WOO ORDER identity — the string
		# _ensure_paid has always passed and _book_payment_entry used to DROP on
		# this Bank-type branch (writing the invoice name instead). A later change made
		# every manually-settled tender keep its caller-supplied reference: the
		# invoice name already sits on the PE's references table, an accountant
		# reconciling the Woo payout needs the order id, and the accounting acceptance
		# assertion is "non-empty AND not an invoice name". The previous pin on
		# the drop did its job — this change had to red that line first.
		self.assertEqual(pe.reference_no, f"Woo #{order.woo_order_id}")
		self.assertNotEqual(pe.reference_no, amended.name)

		# Re-run is idempotent: already at net → no second deduction. No copy_doc on
		# this path, so it needs no harness patch.
		self.assertEqual(refunds._reconcile(order)["status"], "already_net")

	def test_a_second_real_partial_refund_replaces_the_first_discount(self):
		"""End-to-end regression: the live invoice is already net of the
		first refund when the second webhook arrives."""
		from ffl_woo_sync.woocommerce import refunds

		order = self._paid_gross_web_order(order_total=625.0)
		refunds.record_order_refunds(
			order, {"refunds": [{"id": "R-1", "total": "-9.99"}]})
		order.save(ignore_permissions=True)
		with patch.object(frappe, "in_test", False):
			first = refunds._reconcile(order)
		self.assertEqual(first["status"], "adjusted")

		refunds.record_order_refunds(
			order, {"refunds": [{"id": "R-2", "total": "-5.00"}]})
		order.save(ignore_permissions=True)
		with patch.object(frappe, "in_test", False):
			second = refunds._reconcile(order)

		self.assertEqual(second["status"], "adjusted")
		amended = frappe.get_doc("Sales Invoice", second["sales_invoice"])
		self.assertEqual(amended.docstatus, 1)
		self.assertAlmostEqual(flt(amended.grand_total), 610.01, places=2)
		self.assertAlmostEqual(flt(amended.discount_amount), 14.99, places=2)


class TestRefundTaxSplitReal(_RealWebOrder, FrappeTestCase):
	"""End to end with real documents (only Woo's HTTP is faked): the tax
	and shipping a refund gave back leave the Woo tax row and the Shipping row —
	and Sales Tax Payable — instead of sitting under a goods discount. A10: TX
	order goods + shipping 41.57 / tax 3.43 (inclusive), total 758.36."""

	A10 = dict(order_total=758.36, total_tax=57.80, shipping_total=41.57)

	def _record(self, order, refund_id, total):
		"""The webhook's part: record the refund (Pending), no Woo call."""
		refunds.record_order_refunds(order, {"refunds": [{"id": refund_id, "total": total}]})
		order.save(ignore_permissions=True)

	def _reconcile(self, order, woo=None, fail=False):
		"""The long-queue job's part: read the breakdown, then amend."""
		with woo_refunds_http(woo, fail=fail), patch.object(frappe, "in_test", False):
			result = refunds._reconcile(order.name)
		order.reload()
		return result

	def _shape(self, si):
		ship = frappe.db.get_value("Company", si.company, "abbr")
		return {
			"woo_tax": flt(sum(flt(t.tax_amount) for t in refunds._woo_tax_rows(si)), 2),
			"shipping": flt(sum(flt(t.tax_amount) for t in si.taxes
				if t.account_head == f"Shipping Income - {ship}"), 2),
			"discount": flt(si.discount_amount, 2),
			"grand": flt(si.grand_total, 2),
		}

	def _invoice(self, result):
		return frappe.get_doc("Sales Invoice", result["sales_invoice"])

	def _gl(self, voucher, account):
		rows = frappe.get_all("GL Entry", filters={"voucher_no": voucher,
			"account": account, "is_cancelled": 0}, fields=["credit", "debit"])
		return flt(sum(flt(r.credit) - flt(r.debit) for r in rows), 2)

	def test_a10_shipping_refund_amends_tax_and_shipping_out(self):
		from ffl_woo_sync.woocommerce.revenue import _sales_tax_account
		order = self._paid_gross_web_order(**self.A10)
		gross = frappe.get_doc("Sales Invoice", {"woo_online_order": order.name, "docstatus": 1})
		account = _sales_tax_account(gross.company)
		self.assertEqual(self._gl(gross.name, account), 57.80)
		self._record(order, 7001, "-45.00")
		result = self._reconcile(order, [SHIPPING_REFUND])
		self.assertEqual(result["status"], "adjusted")
		amended = self._invoice(result)
		self.assertEqual(self._shape(amended),
			{"woo_tax": 54.37, "shipping": 0.0, "discount": 0.0, "grand": 713.36})
		self.assertEqual(flt(amended.ffl_shipping_amount), 0.0)
		# Sales Tax Payable: the gross invoice's 57.80 is reversed by its cancel,
		# the amended one books 54.37 — 3.43 less owed to the state.
		self.assertEqual(self._gl(gross.name, account), 0.0)
		self.assertEqual(self._gl(amended.name, account), 54.37)
		self.assertEqual(self._reconcile(order)["status"], "already_net")

	def test_a10_born_net_when_the_refund_precedes_the_booking(self):
		from ffl_woo_sync.woocommerce import revenue
		order = self._paid_gross_web_order(submit=False, **self.A10)
		self._record(order, 7001, "-45.00")
		self.assertEqual(self._reconcile(order, [SHIPPING_REFUND])["status"], "no_invoice")
		si = revenue._build_invoice(order, revenue._default_company())
		self.assertEqual(self._shape(si),
			{"woo_tax": 54.37, "shipping": 0.0, "discount": 0.0, "grand": 713.36})
		self.assertAlmostEqual(flt(si.grand_total), refunds.target_net(order), places=2)

	def test_goods_refund_with_its_tax(self):
		order = self._paid_gross_web_order(**self.A10)
		self._record(order, 7002, "-108.25")
		amended = self._invoice(self._reconcile(order, [GOODS_REFUND]))
		self.assertEqual(self._shape(amended),
			{"woo_tax": 49.55, "shipping": 41.57, "discount": 100.0, "grand": 650.11})
		self.assertEqual([flt(i.qty) for i in amended.items], [1.0])  # no phantom line

	def test_custom_amount_refund_is_a_goods_discount(self):
		order = self._paid_gross_web_order(**self.A10)
		self._record(order, 7003, "-20.00")
		amended = self._invoice(self._reconcile(order, [CUSTOM_REFUND]))
		self.assertEqual(self._shape(amended),
			{"woo_tax": 57.80, "shipping": 41.57, "discount": 20.0, "grand": 738.36})

	def test_failed_read_books_the_old_way_alerts_once_then_the_retry_corrects_it(self):
		order = self._paid_gross_web_order(**self.A10)
		self._record(order, 7001, "-45.00")
		title = f"Web order {order.name}: refund breakdown not read"
		# Error Logs outlive the test transaction and order names repeat across
		# runs — count only what this test adds.
		logs_before = frappe.db.count("Error Log", {"method": title})
		first = self._invoice(self._reconcile(order, fail=True))
		self.assertEqual(self._shape(first),  # today's behaviour: all a discount
			{"woo_tax": 57.80, "shipping": 41.57, "discount": 45.0, "grand": 713.36})
		# Still unread: the manual path must not call it in sync, and a second
		# failed read during the same outage logs nothing new.
		self.assertEqual(self._reconcile(order, fail=True)["status"], "pending_breakdown")
		self.assertEqual(frappe.db.count("Error Log", {"method": title}) - logs_before, 1)
		second = self._reconcile(order, [SHIPPING_REFUND])
		self.assertEqual(second["status"], "adjusted")
		self.assertEqual(self._shape(self._invoice(second)),
			{"woo_tax": 54.37, "shipping": 0.0, "discount": 0.0, "grand": 713.36})

	def test_second_refund_amends_from_an_already_split_invoice(self):
		order = self._paid_gross_web_order(**self.A10)
		self._record(order, 7001, "-45.00")
		self.assertEqual(self._reconcile(order, [SHIPPING_REFUND])["status"], "adjusted")
		self._record(order, 7002, "-108.25")
		second = self._reconcile(order, [SHIPPING_REFUND, GOODS_REFUND])
		self.assertEqual(second["status"], "adjusted")
		self.assertEqual(self._shape(self._invoice(second)),
			{"woo_tax": 46.12, "shipping": 0.0, "discount": 100.0, "grand": 605.11})

	def test_zero_tax_order_moves_only_the_shipping(self):
		"""An out-of-state order (no Woo tax row) with a Shipping row and a
		read shipping refund never touches the sales-tax account."""
		from ffl_woo_sync.woocommerce import revenue
		order = self._paid_gross_web_order(order_total=700.57, total_tax=0.0, shipping_total=41.57)
		self._record(order, 7004, "-41.57")
		with patch.object(revenue, "_sales_tax_account",
				side_effect=AssertionError("zero-tax order looked up the tax account")):
			result = self._reconcile(order, [{"id": 7004, "line_items": [],
				"shipping_lines": [{"total": "-41.57", "total_tax": "0.00"}]}])
		self.assertEqual(result["status"], "adjusted")
		amended = self._invoice(result)
		self.assertEqual(flt(amended.ffl_shipping_amount), 0.0)
		self.assertEqual(flt(amended.discount_amount), 0.0)
		self.assertEqual(flt(amended.grand_total), 659.0)

	def test_a_draft_built_before_the_breakdown_is_re_split_when_resumed(self):
		"""The completion job's draft/resume path re-applies the current
		refund booking before it submits."""
		from ffl_woo_sync.woocommerce import revenue
		order = self._paid_gross_web_order(submit=False, **self.A10)
		self._record(order, 7001, "-45.00")
		draft = revenue._build_invoice(order, revenue._default_company())  # Pending → all discount
		self.assertEqual(flt(draft.discount_amount), 45.0)
		self.assertEqual(self._reconcile(order, [SHIPPING_REFUND])["status"], "no_invoice")
		with patch.object(revenue, "deliver_web_stock_lines", return_value=[]):
			result = revenue.create_sales_invoice_for_web_order(order.name)
		self.assertEqual(result["status"], "created")
		self.assertEqual(result["sales_invoice"], draft.name)
		self.assertEqual(self._shape(frappe.get_doc("Sales Invoice", draft.name)),
			{"woo_tax": 54.37, "shipping": 0.0, "discount": 0.0, "grand": 713.36})

	def test_the_completion_job_books_what_is_on_the_order_under_its_lock(self):
		"""The job was handed an order read BEFORE a refund was saved; it
		re-reads it under the lock, so the refund is in what is booked."""
		from ffl_woo_sync.woocommerce import revenue
		order = self._paid_gross_web_order(submit=False, **self.A10)
		stale = frappe.get_doc("Woo Online Order", order.name)
		self._record(order, 7001, "-45.00")
		self._reconcile(order, [SHIPPING_REFUND])
		self.assertEqual(stale.refunds, [])
		with patch.object(revenue, "deliver_web_stock_lines", return_value=[]):
			result = revenue.create_sales_invoice_for_web_order(stale)
		self.assertEqual(result["status"], "created")
		self.assertEqual(flt(frappe.db.get_value(
			"Sales Invoice", result["sales_invoice"], "grand_total")), 713.36)

	def test_backstop_sweeps_stale_pending_breakdowns_only(self):
		"""The 2-hourly backstop re-runs the reconcile for a refund left
		Pending longer than PENDING_REFUND_MINUTES — not for a fresh one, which
		is still the webhook job's."""
		from frappe.utils import add_to_date, now_datetime

		from ffl_woo_sync.woocommerce import order_audit
		old = self._paid_gross_web_order(**self.A10)
		self._record(old, 7001, "-45.00")
		frappe.db.set_value("Woo Online Order Refund", old.refunds[0].name, "recorded_on",
			add_to_date(now_datetime(), hours=-1), update_modified=False)
		fresh = self._paid_gross_web_order(**self.A10)
		self._record(fresh, 7001, "-45.00")
		# An order is "read" only once nothing is Pending — a failed read
		# lists it as still pending (booked the old way meanwhile).
		with woo_refunds_http(fail=True), patch.object(frappe, "in_test", False):
			read, pending = order_audit._sweep_pending_refund_breakdowns()
		self.assertNotIn(old.name, read)
		self.assertIn(old.name, pending)
		with woo_refunds_http([SHIPPING_REFUND]), patch.object(frappe, "in_test", False):
			read, pending = order_audit._sweep_pending_refund_breakdowns()
		self.assertIn(old.name, read)
		self.assertNotIn(old.name, pending)
		self.assertNotIn(fresh.name, read + pending)
		amended = frappe.get_doc("Sales Invoice", {"woo_online_order": old.name, "docstatus": 1})
		self.assertEqual(self._shape(amended),
			{"woo_tax": 54.37, "shipping": 0.0, "discount": 0.0, "grand": 713.36})
		fresh.reload()
		self.assertEqual(fresh.refunds[0].breakdown_status, "Pending")


def _line_refund(refund_id, line_id, qty, total):
	"""A WooCommerce refund of order line ``line_id`` (``quantity`` units, ``total``
	money): Woo names the refunded line in the ``_refunded_item_id`` meta."""
	return {"id": refund_id, "amount": f"{total:.2f}", "shipping_lines": [], "fee_lines": [],
		"line_items": [{"id": 900 + int(line_id), "quantity": -qty, "total": f"{-total:.2f}",
			"total_tax": "0.00", "meta_data": [{"key": "_refunded_item_id", "value": str(line_id)}]}]}


class TestLineRefundReal(_RealWebOrder, FrappeTestCase):
	"""A refund of a WHOLE order line is goods that never stayed with the buyer: the
	line leaves the invoice (with its serial, so the Sales Report stops costing it)
	instead of hiding under a goods discount. Order: line 1 $495 + line 2 $599."""

	def _two_line_order(self):
		from ffl_woo_sync.woocommerce import revenue

		item = self._web_item()
		order = frappe.get_doc({
			"doctype": "Woo Online Order",
			"woo_order_id": f"RT-{frappe.generate_hash(length=8)}",
			"order_status": "Transferred", "woo_status": "completed", "site": "retail",
			"customer_name": "QA Buyer", "customer_email": "qa-buyer@example.com",
			"customer_phone": "555-0100", "currency": "USD",
			"order_total": 1094.0, "total_tax": 0.0, "shipping_total": 0.0, "discount_total": 0.0,
			"lines": [
				{"item_code": item, "qty": 1, "line_total": 495.0, "unit_price": 495.0, "woo_line_id": "1"},
				{"item_code": item, "qty": 1, "line_total": 599.0, "unit_price": 599.0, "woo_line_id": "2"},
			],
		})
		order.flags.ignore_permissions = True
		order.insert()
		si = revenue._build_invoice(order, revenue._default_company())
		si.submit()
		revenue._ensure_paid(si, order)
		return order

	def _record(self, order, refund_id, total):
		refunds.record_order_refunds(order, {"refunds": [{"id": refund_id, "total": total}]})
		order.save(ignore_permissions=True)

	def _reconcile(self, order, woo):
		with woo_refunds_http(woo), patch.object(frappe, "in_test", False):
			result = refunds._reconcile(order.name)
		order.reload()
		return result

	def _rows(self, result):
		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		return [flt(i.rate) for i in si.items], flt(si.discount_amount), flt(si.grand_total)

	def test_a_whole_line_refund_takes_the_line_off_the_invoice(self):
		order = self._two_line_order()
		self._record(order, 7101, "-599.00")
		result = self._reconcile(order, [_line_refund(7101, 2, 1, 599.0)])
		self.assertEqual(result["status"], "adjusted")
		self.assertEqual(self._rows(result), ([495.0], 0.0, 495.0))
		self.assertEqual(self._reconcile(order, [])["status"], "already_net")

	def test_a_refund_read_before_lines_were_read_is_re_read_and_corrected(self):
		"""An invoice already netted by a discount (its refund row Fetched before the
		returned line was recorded) is amended again once, the line coming off."""
		order = self._two_line_order()
		self._record(order, 7102, "-599.00")
		with patch.object(refunds, "_returned_lines", return_value=None):
			first = self._reconcile(order, [_line_refund(7102, 2, 1, 599.0)])
		self.assertEqual(self._rows(first), ([495.0, 599.0], 599.0, 495.0))
		frappe.db.set_value("Woo Online Order Refund", order.refunds[0].name,
			"returned_lines", None, update_modified=False)
		result = self._reconcile(order, [_line_refund(7102, 2, 1, 599.0)])
		self.assertEqual(result["status"], "adjusted")
		self.assertEqual(self._rows(result), ([495.0], 0.0, 495.0))

	def test_a_line_refunded_for_less_than_it_cost_is_held_for_a_person(self):
		"""Quantity back but less money than the line: a restocking fee or a mistyped
		quantity — code cannot tell, so nothing is amended."""
		order = self._two_line_order()
		self._record(order, 7103, "-100.00")
		result = self._reconcile(order, [_line_refund(7103, 2, 1, 100.0)])
		self.assertEqual(result["status"], "held")
		self.assertIn("less than its 599.00", " ".join(result["reasons"]))
		self.assertEqual(self._rows(result), ([495.0, 599.0], 0.0, 1094.0))

	def test_a_price_allowance_on_a_line_stays_a_discount(self):
		order = self._two_line_order()
		self._record(order, 7104, "-30.00")
		result = self._reconcile(order, [_line_refund(7104, 2, 0, 30.0)])
		self.assertEqual(result["status"], "adjusted")
		self.assertEqual(self._rows(result), ([495.0, 599.0], 30.0, 1064.0))

	def test_a_gun_already_transferred_on_the_order_is_held(self):
		"""Shipped and came back is a return, not a cancelled line: held, not removed.
		A gun never dispositioned on the order is removed."""
		order = frappe._dict(name="WOO-ORD-T", lines=[
			frappe._dict(woo_line_id="1", item_code="GUN-B", qty=1, line_total=495.0),
			frappe._dict(woo_line_id="2", item_code="GUN-A", qty=1, line_total=599.0)],
			serials=[frappe._dict(woo_line_id="1", confirmed_serial="SN-B"),
				frappe._dict(woo_line_id="2", confirmed_serial="SN-A")],
			refunds=[frappe._dict(woo_refund_id="7105", breakdown_status="Fetched",
				returned_lines='[{"line_id": "2", "qty": 1, "total": 599.0, "refund": "7105"}]')])
		items = [{"item_code": "GUN-B", "qty": 1, "rate": 495.0, "serial_no": "SN-B"},
			{"item_code": "GUN-A", "qty": 1, "rate": 599.0, "serial_no": "SN-A"}]
		real_exists = frappe.db.exists

		def plan(disposed):
			with patch.object(frappe.db, "exists", side_effect=lambda dt, *a, **k: disposed
					if dt == "FFL Disposition" else real_exists(dt, *a, **k)):
				return refunds._line_plan(items, order)

		remove, value, _absent, held = plan(True)
		self.assertEqual((remove, value), ([], 0.0))
		self.assertIn("SN-A was transferred on this order", held[0])
		self.assertEqual(plan(False), ([items[1]], 599.0, 0.0, []))


class TestRefundReadConcurrency(_RealWebOrder, FrappeTestCase):
	"""The reconcile job against webhooks running at the same time. Each
	test interleaves the two deterministically: the "concurrent" step runs
	INSIDE the job's Woo read (the fake HTTP boundary), i.e. after the job
	snapshotted the order and before it takes the row lock."""

	A10 = TestRefundTaxSplitReal.A10

	def _record(self, order, refund_id, total):
		refunds.record_order_refunds(order, {"refunds": [{"id": refund_id, "total": total}]})
		order.save(ignore_permissions=True)

	def test_a_refund_recorded_during_the_read_stays_pending(self):
		"""The listing was taken before refund 7002 existed — it must not
		be settled "Not in Woo" against it."""
		order = self._paid_gross_web_order(submit=False, **self.A10)
		self._record(order, 7001, "-45.00")

		def woo(page):
			webhook = frappe.get_doc("Woo Online Order", order.name)
			self._record(webhook, 7002, "-108.25")  # lands mid-read
			return [SHIPPING_REFUND]  # the listing Woo gave BEFORE 7002
		with woo_refunds_http(woo):
			refunds._reconcile(order.name)
		status = {r.woo_refund_id: r.breakdown_status
			for r in frappe.get_doc("Woo Online Order", order.name).refunds}
		self.assertEqual(status, {"7001": "Fetched", "7002": "Pending"})

	def test_a_webhook_loaded_order_still_saves_after_the_job(self):
		"""The job writes the child rows only — the parent's ``modified`` is
		untouched, so the webhook's own save goes through (no TimestampMismatch)
		and does not write its stale "Pending" back over the breakdown."""
		order = self._paid_gross_web_order(submit=False, **self.A10)
		self._record(order, 7001, "-45.00")
		webhook = frappe.get_doc("Woo Online Order", order.name)  # loaded first
		modified = frappe.db.get_value("Woo Online Order", order.name, "modified")
		with woo_refunds_http([SHIPPING_REFUND]):
			refunds._reconcile(order.name)
		self.assertEqual(frappe.db.get_value("Woo Online Order", order.name, "modified"), modified)
		self._record(webhook, 7002, "-108.25")  # would raise on a bumped `modified`
		rows = {r.woo_refund_id: (r.breakdown_status, flt(r.tax_refunded))
			for r in frappe.get_doc("Woo Online Order", order.name).refunds}
		self.assertEqual(rows, {"7001": ("Fetched", 3.43), "7002": ("Pending", 0.0)})

	def test_the_job_always_re_reads_the_order_under_its_lock(self):
		"""The job was handed an order object from before the refund, with
		nothing Pending on it; it must still book the refund."""
		order = self._paid_gross_web_order(**self.A10)
		stale = frappe.get_doc("Woo Online Order", order.name)
		self._record(order, 7003, "-20.00")
		with woo_refunds_http([CUSTOM_REFUND]), patch.object(frappe, "in_test", False):
			result = refunds._reconcile(stale)
		self.assertEqual(result["status"], "adjusted")
		self.assertEqual(flt(frappe.db.get_value(
			"Sales Invoice", result["sales_invoice"], "grand_total")), 738.36)

	def test_terminal_outcomes_log_even_with_an_open_todo_on_the_order(self):
		"""An unrelated open ToDo (an earlier outage alert, a staff note)
		must not swallow "Not in Woo" / "Rejected" or the sweep's failure."""
		from ffl_woo_sync.woocommerce import order_audit
		order = self._paid_gross_web_order(submit=False, **self.A10)
		frappe.get_doc({"doctype": "ToDo", "description": "unrelated", "status": "Open",
			"reference_type": "Woo Online Order", "reference_name": order.name,
			"allocated_to": "Administrator"}).insert(ignore_permissions=True)
		self._record(order, 7001, "-45.00")
		self._record(order, 7002, "-10.00")

		def logs(title):
			return frappe.db.count("Error Log", {"method": title})
		gone = f"Web order {order.name}: refund 7001 booked as a goods discount"
		bad = f"Web order {order.name}: refund 7002 booked as a goods discount"
		failed = f"Web refund reconcile failed: {order.name}"
		# Same-site repeatability: the order's name comes back on the next run (its
		# naming series rolls back with the class) while Error Log (MyISAM) does not,
		# and these alerts dedupe on title — so clear what a previous run left.
		frappe.db.delete("Error Log", {"method": ("in", [gone, bad, failed])})
		before = logs(gone), logs(bad)
		with woo_refunds_http([{"id": 7002, "line_items": [
				{"total": "-10", "total_tax": "-9.00"}], "shipping_lines": [
				{"total": "-5", "total_tax": "0"}]}]):  # 14 > the 10 refunded
			refunds._reconcile(order.name)
		self.assertEqual((logs(gone) - before[0], logs(bad) - before[1]), (1, 1))

		self._record(order, 7004, "-5.00")
		frappe.db.set_value("Woo Online Order Refund",
			{"parent": order.name, "woo_refund_id": "7004"}, "recorded_on",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1),
			update_modified=False)
		n = logs(failed)
		with patch.object(refunds, "_reconcile", side_effect=RuntimeError("boom")), \
			 patch("frappe.db.rollback"):
			order_audit._sweep_pending_refund_breakdowns()
		self.assertEqual(logs(failed) - n, 1)

	def test_a_hand_edited_draft_is_left_alone_on_resume(self):
		"""Staff edited the dangling draft (modified != creation) — the
		completion job verifies it as it stands, it does not re-split it."""
		from ffl_woo_sync.woocommerce import revenue
		order = self._paid_gross_web_order(submit=False, **self.A10)
		self._record(order, 7001, "-45.00")
		draft = revenue._build_invoice(order, revenue._default_company())  # all discount
		draft.remarks += " Checked by staff."
		draft.save(ignore_permissions=True)
		with woo_refunds_http([SHIPPING_REFUND]):
			self.assertEqual(refunds._reconcile(order.name)["status"], "no_invoice")
		with patch.object(revenue, "deliver_web_stock_lines", return_value=[]):
			result = revenue.create_sales_invoice_for_web_order(order.name)
		self.assertEqual(result["sales_invoice"], draft.name)
		si = frappe.get_doc("Sales Invoice", draft.name)
		self.assertEqual((flt(si.discount_amount), flt(si.ffl_shipping_amount),
			flt(si.grand_total)), (45.0, 41.57, 713.36))
		self.assertIn("Checked by staff.", si.remarks)


class TestAlertIfInvoiced(FrappeTestCase):

	def test_logs_error_when_a_submitted_si_exists(self):
		order = _StubOrder(woo_status="cancelled")
		with patch("frappe.db.get_value", return_value="ACC-SINV-49"), \
			 patch("frappe.log_error") as log:
			flagged = refunds.alert_if_invoiced(order)
		self.assertTrue(flagged)
		log.assert_called_once()

	def test_silent_return_false_when_no_invoice(self):
		order = _StubOrder(woo_status="cancelled")
		with patch("frappe.db.get_value", return_value=None), \
			 patch("frappe.log_error") as log:
			flagged = refunds.alert_if_invoiced(order)
		self.assertFalse(flagged)
		log.assert_not_called()
