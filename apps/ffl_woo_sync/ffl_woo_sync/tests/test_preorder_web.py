"""Website pre-orders, paid in full at checkout: the listing, the intake, the money held in
Pre-order Deposits until the order ships, the shared arrival queue with the counter, the
cancellation, and the sub-ledger report.

Site tests on the bootstrap company ("FFL Test") with the counter pre-order fixtures
(``ffl_core.tests.test_preorder`` / ``test_preorder_handover``: an 8.25% store, a register, goods
received by Material Receipt). Orders come in through the real webhook handler
(``webhook._handle_order_upsert``); WooCommerce's HTTP side is the only double (the order status
write and, for the reconcile sweep, the product read). Queued jobs do not run in a test
transaction, so each test runs the pre-order job (``preorder.sync_order``) and the arrival job
itself; GL Entries are read as booked.
"""
from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt, getdate, nowdate

from ffl_core import preorder as counter
from ffl_core.setup.preorder import ACCOUNT_TITLE, AVAILABLE_FIELD, ETA_FIELD
from ffl_core.tests.test_preorder import ABBR, COMPANY, _customer
from ffl_core.tests.test_preorder_handover import SHELF, _Handover, _gun_in, _serial, _units_in
from ffl_woo_sync.woocommerce import mapping, preorder, revenue, webhook

CLIENT = "ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings"
FFL_META = [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}]
PAID_ON = "2026-11-02T10:15:00"


def _gl(**filters) -> dict[str, float]:
	"""{account: debit − credit} of the live GL Entries matching *filters*."""
	out: dict[str, float] = {}
	for r in frappe.get_all("GL Entry", filters={"is_cancelled": 0, **filters},
			fields=["account", "debit", "credit"]):
		out[r.account] = flt(out.get(r.account, 0) + flt(r.debit) - flt(r.credit), 2)
	return {a: v for a, v in out.items() if v}


def _entries(order: str) -> list[str]:
	return frappe.get_all("Journal Entry", filters={"woo_online_order": order, "docstatus": 1}, pluck="name",
		order_by="creation")


def _order_gl(order: str) -> dict[str, float]:
	"""The GL of every journal entry the order's money made."""
	names = _entries(order)
	return _gl(voucher_no=["in", names]) if names else {}


class _Web(_Handover):
	"""A uniform-price gun and a counted item of the test's own, both priced, with the counter's
	register and tax; a WooCommerce order body builder and the handler run as the webhook does."""

	def setUp(self):
		super().setUp()
		token = frappe.generate_hash(length=6).upper()
		self.web_gun = f"_T-POW-GUN-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": self.web_gun, "item_name": f"Test Web Pre-order Pistol {token}",
			"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			"uniform_sell_price": 1, "standard_rate": 1000}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", self.acc, "standard_rate", 1000)
		self.deposits = f"{ACCOUNT_TITLE} - {ABBR}"
		self.clearing = revenue._ensure_clearing_account(COMPANY)
		self.client = MagicMock()
		p = patch(CLIENT, return_value=self.client)
		p.start()
		self.addCleanup(p.stop)

	def _slots(self, item, n, eta=None):
		frappe.db.set_value("Item", item, {AVAILABLE_FIELD: n, ETA_FIELD: eta})

	def _body(self, lines, *, woo_id=None, status="processing", total=None, total_tax=0.0, shipping=0.0,
			refunds=None, email=None):
		"""A WooCommerce order: lines are (item_code, qty, line_total, line_tax)."""
		items = [{"id": 100 + i, "sku": code, "quantity": qty, "price": flt(lt) / qty, "subtotal": str(lt),
			"total": str(lt), "total_tax": str(tax), "name": code} for i, (code, qty, lt, tax) in enumerate(lines)]
		total = total if total is not None else flt(sum(l[2] for l in lines) + total_tax + shipping, 2)
		body = {"id": woo_id or random.randint(10**8, 10**9), "status": status, "currency": "USD",
			"total": str(total), "total_tax": str(total_tax), "shipping_total": str(shipping), "discount_total": "0",
			"date_paid": PAID_ON if status in ("processing", "completed") else None,
			"billing": {"first_name": "Web", "last_name": "Buyer", "email": email or f"po-{frappe.scrub(self.cust)}@example.com"},
			"line_items": items, "meta_data": FFL_META}
		if refunds:
			body["refunds"] = refunds
		return body

	def _send(self, body) -> str:
		"""The webhook's order.created / order.updated, then the order's name."""
		with webhook.woo_site("retail"), patch("frappe.enqueue") as enqueue:
			webhook._handle_order_upsert(body)
		self.enqueued = enqueue.call_args_list
		return frappe.db.get_value("Woo Online Order", {"woo_order_id": str(body["id"])}, "name")

	def _sync(self, order: str) -> dict:
		with patch("frappe.enqueue") as enqueue:
			out = preorder.sync_order(order)
		self.enqueued = enqueue.call_args_list
		return out

	def _queued(self, method: str) -> list:
		return [c for c in self.enqueued if c.args and c.args[0] == method]

	def _arrive(self, item, serial=None, qty=None):
		"""Goods in by Material Receipt, then the arrival job the receipt queues."""
		with patch("frappe.enqueue"):
			if qty:
				return counter.allocate_arrival("Stock Entry", _units_in(item, qty))
			return counter.allocate_arrival("Stock Entry", _gun_in(item, serial or _serial()))

	def _row(self, order):
		return frappe.get_doc("Woo Online Order", order)


class Listing(_Web):

	def test_ac1_the_slots_are_for_sale_while_nothing_is_on_the_shelf(self):
		self._slots(self.web_gun, 3, "2026-12-01")
		self.assertEqual(mapping.stock_for_item(self.web_gun), 0)
		self.assertEqual(mapping.preorder_stock(self.web_gun, 0),
			(3, {"_osa_preorder": "1", "_osa_preorder_eta": "2026-12-01"}))
		self.assertEqual(mapping.stock_payload(self.web_gun), {"stock_quantity": 3, "manage_stock": True,
			"meta_data": [{"key": "_osa_preorder", "value": "1"}, {"key": "_osa_preorder_eta", "value": "2026-12-01"}]})
		full = mapping.item_to_product_payload(self.web_gun)
		meta = {m["key"]: m["value"] for m in full["meta_data"]}
		self.assertEqual((full["stock_quantity"], full["manage_stock"]), (3, True))
		self.assertEqual((meta["_osa_preorder"], meta["_osa_preorder_eta"]), ("1", "2026-12-01"))
		self.assertNotIn("backorders", full)
		# The slots taken, the product is out of stock and no longer on pre-order.
		order = self._send(self._body([(self.web_gun, 3, 3000, 0)]))
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 0)
		self.assertEqual(mapping.stock_payload(self.web_gun), {"stock_quantity": 0, "manage_stock": True,
			"meta_data": [{"key": "_osa_preorder", "value": ""}, {"key": "_osa_preorder_eta", "value": ""}]})
		self.assertTrue(order)

	def test_no_eta_sends_an_empty_date(self):
		self._slots(self.acc, 2)
		self.assertEqual(mapping.preorder_stock(self.acc, mapping.stock_for_item(self.acc)),
			(2, {"_osa_preorder": "1", "_osa_preorder_eta": ""}))

	def test_ac2_stock_on_the_shelf_is_sold_as_stock(self):
		self._slots(self.web_gun, 3, "2026-12-01")
		self._arrive(self.web_gun)   # nobody waits: ordinary stock
		self.assertEqual(mapping.stock_for_item(self.web_gun), 1)
		self.assertEqual(mapping.preorder_stock(self.web_gun, 1), (1, mapping.PREORDER_OFF))
		self.assertEqual(mapping.stock_payload(self.web_gun)["meta_data"],
			[{"key": "_osa_preorder", "value": ""}, {"key": "_osa_preorder_eta", "value": ""}])
		order = self._row(self._send(self._body([(self.web_gun, 1, 1000, 0)])))
		self.assertEqual([l.lane for l in order.lines], ["local"])
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 3, "no slot taken")
		self.assertFalse(_entries(order.name))

	def test_an_item_pre_orders_never_touched_keeps_its_plain_stock_push(self):
		self.assertEqual(mapping.preorder_stock(self.acc, 4), (4, None))
		self.assertEqual(mapping.preorder_stock(self.acc, 4, always=True), (4, mapping.PREORDER_OFF))

	def test_a_drop_shipped_item_is_never_on_pre_order(self):
		self._slots(self.acc, 3)
		with patch.object(mapping, "dropship_source", return_value={"distributor": "RSR"}):
			self.assertEqual(mapping.preorder_stock(self.acc, 0), (0, mapping.PREORDER_OFF))
			self.assertFalse(preorder.is_preorder_line(self.acc))

	def test_ac6_reconcile_leaves_the_slots_and_the_meta_alone(self):
		from ffl_woo_sync.woocommerce.tasks import _run_reconcile

		self._slots(self.acc, 3, "2026-12-01")
		listed = frappe._dict(name=self.acc, woo_product_id="WC-PRE-1", is_firearm=0, uniform_sell_price=0,
			standard_rate=1000.0)
		on_store = {"stock_quantity": 3, "regular_price": "1000.00", "meta_data": [
			{"key": "_osa_preorder", "value": "1"}, {"key": "_osa_preorder_eta", "value": "2026-12-01"}]}
		for product, pushed in ((on_store, False), ({**on_store, "stock_quantity": 0}, True),
				({**on_store, "meta_data": []}, True)):
			self.client.reset_mock()
			self.client.get_attribute_ids.return_value = {}
			self.client.get_products.return_value = {}
			self.client.get_product.return_value = product
			with patch("frappe.get_all", side_effect=[[listed], [], [], []]), \
					patch("ffl_woo_sync.woocommerce.sync.push_item") as push, patch("frappe.db.set_value"):
				_run_reconcile()
			self.assertEqual(push.called, pushed, product)
		# The lean push it makes carries the slots and the meta (both on the allowlist).
		from ffl_woo_sync.woocommerce.sync import _pos_owned_payload

		lean = _pos_owned_payload(mapping.item_to_product_payload(self.acc))
		self.assertEqual(lean["stock_quantity"], 3)
		self.assertEqual({m["key"]: m["value"] for m in lean["meta_data"]}.get("_osa_preorder"), "1")


class Intake(_Web):

	def test_ac3_a_paid_pre_order_is_held_in_pre_order_deposits(self):
		self._slots(self.web_gun, 3, "2026-12-01")
		order = self._send(self._body([(self.web_gun, 1, 1000, 82.50)], total=1112.50, total_tax=84.79,
			shipping=27.71))
		doc = self._row(order)
		self.assertEqual([(l.lane, l.preorder_slots, l.reservation_error or "") for l in doc.lines],
			[("preorder", 1, "")])
		self.assertEqual([(s.fulfillment, s.confirmed_serial or "", s.stock_hold_serial or "", s.reservation_error or "")
			for s in doc.serials], [("preorder", "", "", "")], "a unit claimed, no gun named, no no-stock error")
		self.assertFalse(frappe.db.exists("Serial No", {"woo_order": order}))
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 2)
		self.assertEqual(getdate(doc.paid_at), getdate(PAID_ON))
		self.assertTrue(self._queued(preorder.SYNC_JOB))
		out = self._sync(order)
		self.assertEqual(out["status"], "received")
		je = _entries(order)
		self.assertEqual(len(je), 1)
		self.assertEqual(getdate(frappe.db.get_value("Journal Entry", je[0], "posting_date")), getdate(PAID_ON),
			"dated the day it was paid")
		self.assertEqual(_order_gl(order), {self.clearing: 1112.50, self.deposits: -1112.50})
		self.assertFalse(frappe.db.exists("Sales Invoice", {"woo_online_order": order}), "nothing sold yet")
		self.client.update_order.assert_called_once_with(doc.woo_order_id, {"status": "awaiting-stock"})
		self.assertEqual(frappe.db.get_value("Woo Online Order", order, "woo_status"), "awaiting-stock")
		# Run again: nothing more.
		self.client.reset_mock()
		self.assertEqual(self._sync(order)["status"], "in_sync")
		self.client.update_order.assert_not_called()
		self.assertEqual(len(_entries(order)), 1)

	def test_an_unpaid_order_takes_its_slot_and_holds_no_money(self):
		self._slots(self.acc, 3)
		order = self._send(self._body([(self.acc, 1, 1000, 0)], status="pending"))
		self.assertEqual(frappe.db.get_value("Item", self.acc, AVAILABLE_FIELD), 2)
		self.assertEqual(self._sync(order)["status"], "in_sync")
		self.assertFalse(_entries(order))
		self.client.update_order.assert_not_called()

	def test_too_few_slots_is_recorded_on_the_line_never_raised(self):
		self._slots(self.acc, 1)
		order = self._row(self._send(self._body([(self.acc, 2, 2000, 0)])))
		self.assertEqual(order.lines[0].lane, "preorder")
		self.assertEqual(order.lines[0].preorder_slots, 0)
		self.assertIn("Pre-order oversold", order.lines[0].reservation_error)
		self.assertEqual(frappe.db.get_value("Item", self.acc, AVAILABLE_FIELD), 1, "nothing taken")

	def test_the_queue_shows_it_awaiting_arrival(self):
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders

		self._slots(self.acc, 3)
		order = self._send(self._body([(self.acc, 1, 1000, 0)]))
		row = next(r for r in list_pending_web_orders() if r["woo_online_order"] == order)
		self.assertEqual((row["preorder"], row["awaiting_preorder"], row["stage"]), (True, True, "waiting"))
		with self.assertRaisesRegex(frappe.ValidationError, "pre-order whose goods have not all arrived"):
			from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
			dispose_web_order(order)

	def test_mixed_order_holds_everything_and_waits_for_the_pre_ordered_part(self):
		self._slots(self.web_gun, 3)
		self._units_on_shelf(self.acc, 2)
		order = self._send(self._body([(self.web_gun, 1, 1000, 0), (self.acc, 1, 50, 0)]))
		doc = self._row(order)
		self.assertEqual({l.item_code: l.lane for l in doc.lines}, {self.web_gun: "preorder", self.acc: "local"})
		self._sync(order)
		self.assertEqual(_order_gl(order), {self.clearing: 1050.0, self.deposits: -1050.0},
			"the whole order is held until it ships")
		self.assertFalse(preorder.ready(doc))

	def _units_on_shelf(self, item, qty):
		_units_in(item, qty)


class Arrival(_Web):

	def test_ac7_counter_and_web_pre_orders_share_one_queue_oldest_first(self):
		self._slots(self.web_gun, 5)
		first = counter.create_preorder({"customer": self.cust,
			"items": [{"item_code": self.web_gun, "qty": 1, "rate": 1000}]})["sales_order"]
		web = self._send(self._body([(self.web_gun, 1, 1000, 0)]))
		self._sync(web)
		self.assertEqual(frappe.db.get_value("Woo Online Order", web, "woo_status"), "awaiting-stock")
		last = counter.create_preorder({"customer": _customer(),
			"items": [{"item_code": self.web_gun, "qty": 1, "rate": 1000}]})["sales_order"]
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 2, "one count for both channels")
		one = _serial()
		self._arrive(self.web_gun, one)
		self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": first}, "ffl_preorder_serial"), one)
		self.assertFalse(self._row(web).serials[0].confirmed_serial)
		two = _serial()
		self._arrive(self.web_gun, two)
		self.assertEqual(self._row(web).serials[0].confirmed_serial, two, "the web order placed second")
		self.assertEqual(frappe.db.get_value("Serial No", two, ["woo_reserved", "woo_order"]), (1, web))
		self.assertFalse(frappe.db.get_value("Sales Order Item", {"parent": last}, "ffl_preorder_serial"))
		self._arrive(self.web_gun)
		self.assertTrue(frappe.db.get_value("Sales Order Item", {"parent": last}, "ffl_preorder_serial"))
		# Everything is here: Woo goes back to processing and it ships as any web order.
		self.assertTrue(preorder.ready(self._row(web)))
		self.client.reset_mock()
		self._sync(web)
		self.client.update_order.assert_called_with(self._row(web).woo_order_id, {"status": "processing"})
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		row = next(r for r in list_pending_web_orders() if r["woo_online_order"] == web)
		self.assertEqual((row["awaiting_preorder"], row["needs_disposition"]), (False, True))

	def test_a_web_order_placed_first_gets_the_first_gun(self):
		self._slots(self.web_gun, 5)
		web = self._send(self._body([(self.web_gun, 1, 1000, 0)]))
		so = counter.create_preorder({"customer": self.cust,
			"items": [{"item_code": self.web_gun, "qty": 1, "rate": 1000}]})["sales_order"]
		one = _serial()
		self._arrive(self.web_gun, one)
		self.assertEqual(self._row(web).serials[0].confirmed_serial, one)
		self.assertFalse(frappe.db.get_value("Sales Order Item", {"parent": so}, "ffl_preorder_serial"))

	def test_an_unpaid_web_order_waits_behind_paid_ones(self):
		self._slots(self.web_gun, 5)
		unpaid = self._send(self._body([(self.web_gun, 1, 1000, 0)], status="on-hold"))
		paid = self._send(self._body([(self.web_gun, 1, 1000, 0)]))
		self._arrive(self.web_gun)
		self.assertFalse(self._row(unpaid).serials[0].confirmed_serial)
		self.assertTrue(self._row(paid).serials[0].confirmed_serial)

	def test_guns_owed_to_in_stock_web_orders_are_not_put_by(self):
		"""Two guns on the shelf, both sold online as unnamed units (claims reserve no serial): a
		pre-order paid after them gets neither — only the next gun that comes in."""
		_gun_in(self.web_gun, _serial())
		_gun_in(self.web_gun, _serial())
		self._slots(self.web_gun, 3)
		for _ in range(2):
			claim = self._row(self._send(self._body([(self.web_gun, 1, 1000, 0)])))
			self.assertEqual((claim.lines[0].lane, claim.serials[0].stock_hold_serial or ""), ("local", ""))
		self.assertEqual(mapping.stock_for_item(self.web_gun), 0)
		pre = self._send(self._body([(self.web_gun, 1, 1000, 0)]))
		self.assertEqual(self._row(pre).lines[0].lane, "preorder")
		out = self._sync(pre)
		self.assertEqual(self._queued(counter.ALLOCATE_ITEMS_JOB)[0].kwargs["item_codes"], [self.web_gun],
			"a first payment looks at the shelf")
		with patch("frappe.enqueue"):
			self.assertEqual(counter.allocate_items([self.web_gun], COMPANY), {self.web_gun: []})
		self.assertEqual(out["status"], "received")
		self._arrive(self.web_gun)
		self.assertTrue(self._row(pre).serials[0].confirmed_serial, "one of three put by: the units are alike")
		self.assertEqual(mapping.free_units(self.web_gun), 2, "two left for the two claims")
		self.assertEqual(mapping.stock_for_item(self.web_gun), 0)

	def test_counted_goods_are_put_by_and_come_off_every_count(self):
		self._slots(self.acc, 5)
		web = self._send(self._body([(self.acc, 2, 2000, 0)]))
		self._arrive(self.acc, qty=3)
		self.assertEqual(flt(self._row(web).lines[0].preorder_reserved), 2)
		self.assertEqual(mapping.local_stock_for_item(self.acc), 1, "the storefront's count less the two put by")
		self.assertEqual(mapping.pending_web_line_qty(self.acc), 2)
		self.assertTrue(preorder.ready(self._row(web)))

	def test_a_receipt_queues_the_arrival_for_a_web_order(self):
		self._slots(self.acc, 5)
		self._send(self._body([(self.acc, 1, 1000, 0)]))
		with patch.object(frappe, "enqueue") as enqueue:
			se = _units_in(self.acc, 1)
		calls = [c for c in enqueue.call_args_list if c.args and c.args[0] == counter.ALLOCATE_JOB]
		self.assertEqual([c.kwargs["name"] for c in calls], [se])


class ShipAndCancel(_Web):

	def _ready_accessory_order(self):
		self._slots(self.acc, 3)
		body = self._body([(self.acc, 1, 1000, 82.50)], total=1112.50, total_tax=84.79, shipping=27.71)
		order = self._send(body)
		self._sync(order)
		self._arrive(self.acc, qty=1)
		self._sync(order)
		return order, body

	def test_ac4_shipping_pays_the_invoice_from_pre_order_deposits(self):
		from ffl_core.setup.shipping_charge import ensure_shipping_account

		order, body = self._ready_accessory_order()
		self.assertEqual(self.client.update_order.call_args.args[1], {"status": "processing"})
		before = {(e.account, flt(e.debit - e.credit, 2)) for e in frappe.get_all("GL Entry",
			filters={"account": self.clearing, "is_cancelled": 0, "voucher_no": ["in", _entries(order)]},
			fields=["account", "debit", "credit"])}
		self.assertEqual(before, {(self.clearing, 1112.50)})
		# ShipStation shipped it: Woo completes the order, which auto-clears and books the sale.
		self._send({**body, "status": "completed"})
		self.assertEqual(frappe.db.get_value("Woo Online Order", order, "order_status"), "Transferred")
		with patch("frappe.enqueue"):
			out = revenue.create_sales_invoice_for_web_order(order)
		self.assertEqual(out["status"], "created")
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(flt(si.outstanding_amount), 0)
		self.assertFalse(frappe.db.exists("Payment Entry Reference", {"reference_name": si.name, "docstatus": 1}),
			"no Woo Clearing collection")
		applied = [j for j in _entries(order) if frappe.db.get_value("Journal Entry", j, "woo_preorder_entry") == "Applied"]
		self.assertEqual(len(applied), 1)
		self.assertEqual(getdate(frappe.db.get_value("Journal Entry", applied[0], "posting_date")), si.posting_date)
		gl = _gl(voucher_no=["in", [si.name, applied[0]]])
		self.assertEqual(gl.pop(self.deposits), 1112.50)
		self.assertEqual(gl.pop(ensure_shipping_account(COMPANY)), -27.71)
		self.assertEqual(gl.pop(revenue._sales_tax_account(COMPANY)), -84.79)
		self.assertEqual(gl.pop(si.items[0].income_account), -1000.0)
		self.assertEqual(gl, {}, "receivable nets to zero; nothing on Woo Clearing")
		self.assertEqual(_order_gl(order).get(self.deposits, 0), 0, "nothing left held")
		self.assertEqual(_gl(voucher_no=["in", _entries(order)]).get(self.clearing), 1112.50, "Woo Clearing once")
		# A second run of the booking changes nothing.
		with patch("frappe.enqueue"):
			self.assertEqual(revenue.create_sales_invoice_for_web_order(order)["status"], "exists")
		self.assertEqual(len(_entries(order)), 2)

	def test_ac5_cancel_pays_everything_back_and_gives_the_slot_back(self):
		self._slots(self.web_gun, 3)
		body = self._body([(self.web_gun, 1, 1000, 82.50)], total=1112.50, total_tax=84.79, shipping=27.71)
		order = self._send(body)
		self._sync(order)
		gun = _serial()
		self._arrive(self.web_gun, gun)
		self.assertEqual(frappe.db.get_value("Serial No", gun, "woo_reserved"), 1)
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 2)
		self._send({**body, "status": "cancelled"})
		self.assertEqual(frappe.db.get_value("Item", self.web_gun, AVAILABLE_FIELD), 3, "slot back")
		self.assertEqual(frappe.db.get_value("Serial No", gun, ["woo_reserved", "woo_order"]), (0, ""), "gun let go")
		self.assertFalse(self._row(order).serials[0].confirmed_serial)
		self.assertEqual(self._queued(counter.ALLOCATE_ITEMS_JOB)[0].kwargs["item_codes"], [self.web_gun])
		self.assertEqual(self._sync(order)["status"], "refunded")
		self.assertEqual(_order_gl(order), {}, "Pre-order Deposits and Woo Clearing back to zero")
		kinds = [frappe.db.get_value("Journal Entry", j, ["woo_preorder_entry", "posting_date"]) for j in _entries(order)]
		self.assertEqual([k for k, _d in kinds], ["Receipt", "Refund"])
		self.assertEqual(getdate(kinds[1][1]), getdate(nowdate()), "refunded on the day it is booked")
		self.assertEqual(mapping.stock_for_item(self.web_gun), 1, "the gun is ordinary stock again")

	def test_a_cancel_after_it_shipped_gives_nothing_back(self):
		self._slots(self.acc, 3)
		body = self._body([(self.acc, 1, 1000, 0)])
		order = self._send(body)
		frappe.db.set_value("Woo Online Order", order, "order_status", "Transferred")
		self._send({**body, "status": "cancelled"})
		self.assertEqual(frappe.db.get_value("Item", self.acc, AVAILABLE_FIELD), 2)
		self.assertEqual(frappe.db.get_value("Woo Online Order Line", {"parent": order}, "preorder_slots"), 1)

	def test_a_refund_before_shipping_pays_that_much_back(self):
		self._slots(self.acc, 3)
		body = self._body([(self.acc, 1, 1000, 0)], shipping=30)
		order = self._send(body)
		self._sync(order)
		self._send({**body, "refunds": [{"id": 9001, "total": "-30.00", "reason": "shipping"}]})
		self.assertEqual(self._sync(order)["status"], "refunded")
		self.assertEqual(_order_gl(order), {self.clearing: 1000.0, self.deposits: -1000.0})

	def test_report_rows_match_the_ledger(self):
		from ffl_core.ffl_core.report.pre_order_deposits import pre_order_deposits as report

		def gap():
			return {s["label"]: s["value"] for s in report.execute({"company": COMPANY})[4]}["Not posted yet"]

		start = gap()
		self._slots(self.acc, 3)
		open_order = self._send(self._body([(self.acc, 1, 400, 0)]))
		self._sync(open_order)
		body = self._body([(self.acc, 1, 250, 0)])
		gone = self._send(body)
		self._sync(gone)
		self._send({**body, "status": "cancelled"})
		self._sync(gone)
		rows = {r["sales_order"]: r for r in report.channel_rows(COMPANY, nowdate())}
		self.assertEqual({k: rows[open_order][k] for k in ("ref_doctype", "channel", "collected", "refunded",
			"balance", "status", "aging")}, {"ref_doctype": "Woo Online Order", "channel": "Web", "collected": 400,
			"refunded": 0, "balance": 400, "status": "Open", "aging": "0–30"})
		self.assertEqual({k: rows[gone][k] for k in ("collected", "refunded", "balance", "status")},
			{"collected": 250, "refunded": 250, "balance": 0, "status": "Cancelled"})
		self.assertEqual(gap(), start, "the web rows add exactly what the ledger holds")
