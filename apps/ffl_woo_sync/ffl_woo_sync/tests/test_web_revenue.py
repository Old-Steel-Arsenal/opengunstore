"""Tests for woocommerce/revenue.py — the web-order Sales Invoice booking.

dispose_web_order books the bound-book exits; revenue.py mints the missing
accounting document (a submitted SI mirroring the Woo amounts, update_stock=0,
collected into Woo Clearing). Covered here: the pure line builder, the
idempotent/resumable entry point, the dispose-hook wiring (a revenue failure
must never break a dispose), and the interstate-tax engine's hands-off rule.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, get_datetime, now_datetime


def _line(item_code="GLOCK-19", qty=1, line_total=639.0, woo_line_id="7",
		unit_price=639.0):
	return frappe._dict({
		"item_code": item_code, "qty": qty, "line_total": line_total,
		"unit_price": unit_price, "woo_line_id": woo_line_id,
	})


def _srow(item_code="GLOCK-19", confirmed_serial="SN-1", woo_line_id="7",
		unit_index=0):
	return frappe._dict({
		"item_code": item_code, "confirmed_serial": confirmed_serial,
		"woo_line_id": woo_line_id, "unit_index": unit_index,
	})


def _order(name="WOO-ORD-00001", status="Transferred", order_total=639.0,
		total_tax=0.0, shipping_total=0.0, lines=None, serials=None):
	o = MagicMock()
	o.name = name
	o.order_status = status
	o.woo_order_id = "931"
	o.site = "retail"
	o.customer_name = "John Smith"
	o.customer_email = "john.smith@example.com"  # plan-A per-buyer customer
	o.customer_phone = "555-0100"
	o.ffl_business_name = "ACME Guns"
	o.ffl_license = "1-99-888-01-2D-09999"
	o.atf_ffl_record = "1-99-888-01-2D-09999"
	o.order_total = order_total
	o.total_tax = total_tax
	o.shipping_total = shipping_total
	o.discount_total = 0.0
	o.lines = lines if lines is not None else [_line()]
	o.serials = serials if serials is not None else [_srow()]
	o.refunds = []  # WooCommerce partial refunds (D1); empty on a clean order
	o.transferred_at = None
	o.get = lambda key, default=None: getattr(o, key, default)
	return o


class _StubSI:
	"""Records what revenue's builder does to the Sales Invoice."""

	def __init__(self, grand_total_after_insert=639.0):
		self._gt = grand_total_after_insert
		self.items = []
		self.taxes = []
		self.flags = frappe._dict()
		self.name = "ACC-SINV-TEST-1"
		self.grand_total = 0.0
		self.outstanding_amount = 0.0
		self.inserted = False
		self.submitted = False

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))

	def insert(self):
		self.inserted = True
		self.grand_total = self._gt  # "ERPNext computed the totals"

	def submit(self):
		self.submitted = True

	def reload(self):
		pass


def _create(order, stub, existing=None):
	"""Run create_sales_invoice_for_web_order with all externals patched."""
	from ffl_woo_sync.woocommerce import revenue

	def _db_get_value(doctype, *a, **k):
		return existing if doctype == "Sales Invoice" else None

	with patch.object(revenue, "_default_company", return_value="Test Store A LLC"), \
		 patch.object(revenue, "_resolve_web_customer", return_value="Web Store Customer"), \
		 patch.object(revenue, "_sales_tax_account", return_value="Sales Tax Payable - TSA"), \
		 patch.object(revenue, "_ensure_paid") as paid, \
		 patch.object(revenue, "deliver_web_stock_lines", return_value=[]), \
		 patch("frappe.db.get_value", side_effect=_db_get_value), \
		 patch("frappe.new_doc", return_value=stub), \
		 patch("frappe.log_error") as log_error:
		result = revenue.create_sales_invoice_for_web_order(order)
	return result, paid, log_error


class TestBuildInvoiceItems(FrappeTestCase):

	def test_rate_is_post_discount_line_total_over_qty(self):
		from ffl_woo_sync.woocommerce.revenue import build_invoice_items
		order = _order(lines=[_line(qty=2, line_total=1200.0)], serials=[])
		rows = build_invoice_items(order)
		self.assertEqual(rows[0]["rate"], 600.0)
		self.assertEqual(rows[0]["qty"], 2)
		self.assertNotIn("serial_no", rows[0])

	def test_firearm_line_carries_joined_serials_accessory_does_not(self):
		from ffl_woo_sync.woocommerce.revenue import build_invoice_items
		order = _order(
			lines=[_line(woo_line_id="7", qty=2, line_total=1278.0),
				_line(item_code="AMMO-9MM", woo_line_id="8", line_total=29.99)],
			serials=[_srow(woo_line_id="7", confirmed_serial="SN-1"),
				_srow(woo_line_id="7", confirmed_serial="SN-2", unit_index=1)])
		rows = build_invoice_items(order)
		self.assertEqual(rows[0]["serial_no"], "SN-1\nSN-2")
		self.assertNotIn("serial_no", rows[1])  # accessory: price only


class TestCreateSalesInvoice(FrappeTestCase):

	def test_happy_path_mirrors_woo_and_collects(self):
		order = _order(order_total=745.84, total_tax=56.84,
			lines=[_line(line_total=689.0)])
		stub = _StubSI(grand_total_after_insert=745.84)
		result, paid, _log = _create(order, stub)
		self.assertEqual(result["status"], "created")
		self.assertTrue(stub.submitted)
		paid.assert_called_once()
		# The honest mapping: accounting-only + destination FFL + Shipped stamp.
		self.assertEqual(stub.update_stock, 0)
		self.assertEqual(stub.shipstation_status, "Shipped")
		self.assertEqual(stub.woo_online_order, "WOO-ORD-00001")
		self.assertEqual(stub.ffl_destination_ffl, "1-99-888-01-2D-09999")
		# Tax mirrored verbatim as ONE Actual row (never recomputed).
		self.assertEqual(len(stub.taxes), 1)
		self.assertEqual(stub.taxes[0].charge_type, "Actual")
		self.assertEqual(stub.taxes[0].tax_amount, 56.84)
		self.assertEqual(stub.taxes[0].account_head, "Sales Tax Payable - TSA")

	def test_zero_tax_order_appends_no_tax_row(self):
		order = _order(order_total=639.0, total_tax=0.0)
		stub = _StubSI(grand_total_after_insert=639.0)
		result, _paid, _log = _create(order, stub)
		self.assertEqual(result["status"], "created")
		self.assertEqual(stub.taxes, [])

	def test_refunded_order_is_born_at_net_via_discount(self):
		# Grossed 603.50 with a 9.99 partial refund already recorded → the SI is
		# built with a 9.99 Net-Total discount (no phantom -1 gun) and the total
		# check runs against the 593.51 net, so it submits cleanly.
		order = _order(order_total=603.50, lines=[_line(line_total=603.50)])
		order.refunds = [frappe._dict(woo_refund_id="9003", refund_amount=9.99)]
		stub = _StubSI(grand_total_after_insert=593.51)
		result, _paid, _log = _create(order, stub)
		self.assertEqual(result["status"], "created")
		self.assertEqual(stub.apply_discount_on, "Net Total")
		self.assertAlmostEqual(stub.discount_amount, 9.99, places=2)

	def test_total_mismatch_leaves_draft_and_alerts(self):
		order = _order(order_total=639.0)
		stub = _StubSI(grand_total_after_insert=600.0)  # > $0.02 drift
		with patch("ffl_core.alerts.alert_once") as alert:
			result, paid, _log = _create(order, stub)
		self.assertEqual(result["status"], "draft_total_mismatch")
		self.assertFalse(stub.submitted)
		paid.assert_not_called()
		# alert_once, not a bare log: the draft stays a backstop candidate, so the
		# same mismatch would otherwise write an Error Log every two hours.
		alert.assert_called_once()
		self.assertEqual(alert.call_args.args[:2], ("Sales Invoice", stub.name))

	def test_submitted_existing_short_circuits_to_payment_topup(self):
		from ffl_woo_sync.woocommerce import revenue
		order = _order()
		si = MagicMock()
		with patch.object(revenue, "_default_company", return_value="Test Store A LLC"), \
			 patch.object(revenue, "_ensure_paid") as paid, \
			 patch.object(revenue, "deliver_web_stock_lines", return_value=[]), \
			 patch("frappe.db.get_value",
				return_value=frappe._dict(name="ACC-SINV-1", docstatus=1)), \
			 patch("frappe.get_doc", return_value=si), \
			 patch("frappe.new_doc") as new_doc:
			result = revenue.create_sales_invoice_for_web_order(order)
		self.assertEqual(result["status"], "exists")
		new_doc.assert_not_called()  # resumable, not duplicated
		paid.assert_called_once_with(si, order, posting_date=si.posting_date)

	def test_refuses_untransferred_order(self):
		from ffl_woo_sync.woocommerce.revenue import create_sales_invoice_for_web_order
		with self.assertRaises(frappe.ValidationError):
			create_sales_invoice_for_web_order(_order(status="Pending Fulfillment"))

	def test_a_submitted_pickup_ticket_is_the_booking(self):
		"""A firearm pickup is booked on its POS Invoice; the Sales Invoice only
		appears when the ticket consolidates. While that merge is stuck, the
		backstop must not mint a second revenue SI (and clearing payment) that
		the consolidated invoice would then double."""
		from ffl_woo_sync.woocommerce import revenue
		order = _order()

		def _db_get_value(doctype, filters=None, *a, **k):
			if doctype == "POS Invoice":
				self.assertEqual(filters, {"woo_online_order": order.name,
					"docstatus": 1, "is_return": 0})
				return "ACC-PSINV-TEST-1"
			return None

		with patch.object(revenue, "_default_company", return_value="Test Store A LLC"), \
			 patch.object(revenue, "_ensure_paid") as paid, \
			 patch("frappe.db.get_value", side_effect=_db_get_value), \
			 patch("frappe.new_doc") as new_doc:
			result = revenue.create_sales_invoice_for_web_order(order)
		self.assertEqual(result["status"], "counter_sale")
		self.assertEqual(result["pos_invoice"], "ACC-PSINV-TEST-1")
		new_doc.assert_not_called()
		paid.assert_not_called()


class TestSalesTaxAccount(FrappeTestCase):
	"""The Woo-collected tax goes to the template's FIRST row. Child rows share
	their parent's `creation`, so an unordered get_value returned whichever row
	the database liked — on a CA store, possibly the CFET excise row."""

	def test_first_row_by_idx_wins_over_a_later_created_row(self):
		from ffl_core.setup import test_bootstrap
		from ffl_woo_sync.woocommerce import revenue

		test_bootstrap.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		parent = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Liability", "is_group": 1},
			"name", order_by="lft")
		heads = []
		for account_name in ("WRT Sales Tax Payable", "WRT Excise Tax Payable"):
			name = frappe.db.get_value(
				"Account", {"company": company, "account_name": account_name}, "name")
			if not name:
				acct = frappe.get_doc({
					"doctype": "Account", "account_name": account_name,
					"company": company, "parent_account": parent, "account_type": "Tax",
				})
				acct.insert(ignore_permissions=True)
				name = acct.name
			heads.append(name)
		template = frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template",
			"title": f"WRT Two Row {frappe.generate_hash(length=6)}",
			"company": company, "is_default": 0,
			"taxes": [
				{"charge_type": "On Net Total", "account_head": heads[0],
					"rate": 8.25, "description": "Sales tax"},
				{"charge_type": "On Net Total", "account_head": heads[1],
					"rate": 0, "description": "Excise"},
			],
		}).insert(ignore_permissions=True)
		# Make the idx-2 row the "newest": the default `creation desc` order
		# would pick it.
		frappe.db.set_value("Sales Taxes and Charges", template.taxes[1].name,
			"creation", frappe.utils.add_to_date(template.creation, seconds=5),
			update_modified=False)

		with patch("ffl_core.compliance.interstate_tax._default_template",
				return_value=template.name):
			self.assertEqual(revenue._sales_tax_account(company), heads[0])


class TestDisposeHookWiring(FrappeTestCase):

	def _dispose(self, revenue_patch):
		from ffl_woo_sync.tests.test_web_fulfillment import (
			_srow as fsrow,
			_woo_order,
		)
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order

		order = _woo_order(serials=[fsrow(confirmed_serial="SN-1")])
		disp = MagicMock()

		def _get_doc(arg, *a, **k):
			return disp if isinstance(arg, dict) else order

		def _gv(doctype, name, field=None, *a, **k):
			if doctype == "Serial No":
				return None if field and "product_id" in field else "WH-Main"
			if doctype == "Warehouse":
				return "Test Store A LLC"
			return None

		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", side_effect=_get_doc), \
			 patch("frappe.db.exists", return_value=None), \
			 patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.log_error") as log_error, \
			 patch("ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order."
				"woo_online_order.now_datetime", return_value="2026-06-19 12:00:00"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.now_datetime",
				return_value="2026-07-06 12:00:00"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment._resolve_and_validate_ffl",
				return_value="1-57-x"), \
			 patch("ffl_woo_sync.woocommerce.fulfillment.create_sales_invoice_for_web_order",
				**revenue_patch) as rev:
			result = dispose_web_order("WOO-ORD-00001")
		return result, order, rev, log_error

	def test_dispose_books_revenue_and_reports_invoice(self):
		result, order, rev, _log = self._dispose(
			{"return_value": {"sales_invoice": "ACC-SINV-9", "status": "created"}})
		rev.assert_called_once_with(order)
		# The in-memory doc was refreshed for the hook (db.set_value bypasses it).
		self.assertEqual(order.order_status, "Transferred")
		self.assertEqual(order.atf_ffl_record, "1-57-x")
		self.assertTrue(result["ok"])
		self.assertEqual(result["sales_invoice"], "ACC-SINV-9")
		self.assertEqual(result["invoice_status"], "created")

	def test_revenue_failure_never_breaks_the_dispose(self):
		result, _order_, _rev, log_error = self._dispose(
			{"side_effect": Exception("boom")})
		self.assertTrue(result["ok"])  # dispositions stand; accounting retries later
		self.assertIsNone(result["sales_invoice"])
		self.assertEqual(result["invoice_status"], "error")
		log_error.assert_called_once()


class TestInterstateTaxSkip(FrappeTestCase):

	def test_web_si_short_circuits_the_exemption_engine(self):
		from ffl_core.compliance import interstate_tax
		doc = frappe._dict(is_return=0, is_consolidated=0,
			woo_online_order="WOO-ORD-00001", company="Test Store A LLC",
			taxes_and_charges="Texas Sales Tax 8.25% (Springfield)")
		with patch.object(interstate_tax, "_oos_template") as oos:
			interstate_tax.apply_interstate_tax(doc)
		oos.assert_not_called()  # hands off: tax already mirrors Woo's charge
		self.assertEqual(doc.taxes_and_charges, "Texas Sales Tax 8.25% (Springfield)")


# --------------------------------------------------------------- stock leg

def _si_row(name, item_code, qty=1.0, warehouse="Stores - TSA", dropship=0):
	return frappe._dict({
		"name": name, "item_code": item_code, "qty": qty,
		"warehouse": warehouse, "delivered_by_supplier": dropship,
	})


class _Si(SimpleNamespace):
	"""A Sales Invoice stand-in with a real child table — NOT frappe._dict, whose
	``.items`` is dict.items and shadows the rows."""

	def get(self, key, default=None):
		return getattr(self, key, default)


def _si(rows, name="ACC-SINV-TEST-7", update_stock=0):
	return _Si(name=name, items=rows, update_stock=update_stock,
		posting_date="2026-08-22", posting_time="10:02:41",
		woo_online_order="WOO-ORD-01782", po_no="5122")


class _StubDN:
	"""What make_delivery_note hands back: mapped rows, then insert/submit."""

	def __init__(self, rows, name="MAT-DN-TEST-1", fail_on_submit=False):
		self.items = [frappe._dict(r) for r in rows]
		self.taxes = [frappe._dict(account_head="Shipping Income - TSA", tax_amount=45)]
		self.name = name
		self.flags = frappe._dict()
		self.inserted = self.submitted = False
		self.comments = []
		self._fail = fail_on_submit

	def set(self, field, value):
		setattr(self, field, value)

	def insert(self):
		self.inserted = True

	def submit(self):
		if self._fail:
			raise frappe.ValidationError("Stock shortfall in Stores - TSA")
		self.submitted = True

	def add_comment(self, kind, text):
		self.comments.append((kind, text))


class TestWebStockRows(FrappeTestCase):
	"""Which invoice rows owe a Delivery Note: ours, from our shelf, not a gun."""

	def _rows(self, si, *, stock=None, firearm=None, unplaced=None):
		from ffl_woo_sync.woocommerce import revenue
		stock = stock or {}
		firearm = firearm or set()
		with patch("frappe.get_cached_value",
				side_effect=lambda dt, name, field: stock.get(name, 1)), \
			 patch("ffl_core.firearm.is_firearm_item",
				side_effect=lambda code: code in firearm):
			return [r.name for r in revenue._web_stock_rows(si, unplaced)]

	def test_accessory_from_our_warehouse_is_deliverable(self):
		si = _si([_si_row("r1", "IRAY-RH25")])
		self.assertEqual(self._rows(si), ["r1"])

	def test_firearm_row_is_left_to_the_disposition(self):
		si = _si([_si_row("r1", "GLOCK-19"), _si_row("r2", "IRAY-RH25")])
		self.assertEqual(self._rows(si, firearm={"GLOCK-19"}), ["r2"])

	def test_dropship_row_never_touched_our_shelf(self):
		si = _si([_si_row("r1", "RSR-OPTIC", warehouse="", dropship=1)])
		self.assertEqual(self._rows(si), [])

	def test_non_stock_item_and_zero_qty_are_skipped(self):
		si = _si([_si_row("r1", "GIFT-WRAP"), _si_row("r2", "IRAY-RH25", qty=0)])
		self.assertEqual(self._rows(si, stock={"GIFT-WRAP": 0}), [])

	def test_a_stock_row_without_a_warehouse_is_reported_not_dropped(self):
		si = _si([_si_row("r1", "IRAY-RH25", warehouse="")])
		unplaced = []
		self.assertEqual(self._rows(si, unplaced=unplaced), [])
		self.assertEqual([r.name for r in unplaced], ["r1"])


class TestStockLegState(FrappeTestCase):
	"""What the DN table already says about the rows: issued qty vs rows a human
	has touched (draft / cancelled / return), which are never re-issued."""

	def _state(self, dn_rows, returns=()):
		from ffl_woo_sync.woocommerce import revenue

		def _get_all(doctype, **kw):
			if doctype == "Delivery Note Item":
				return [frappe._dict(r) for r in dn_rows]
			if doctype == "Delivery Note":
				return list(returns)
			raise AssertionError(doctype)

		with patch("frappe.get_all", side_effect=_get_all):
			return revenue._stock_leg_state(["r1", "r2"])

	def test_submitted_rows_add_up_per_invoice_row(self):
		delivered, blocked = self._state([
			{"si_detail": "r1", "qty": 1, "docstatus": 1, "parent": "DN-1"},
			{"si_detail": "r1", "qty": 2, "docstatus": 1, "parent": "DN-2"},
		])
		self.assertEqual(delivered, {"r1": 3.0})
		self.assertEqual(blocked, set())

	def test_draft_and_cancelled_rows_block_the_row(self):
		delivered, blocked = self._state([
			{"si_detail": "r1", "qty": 1, "docstatus": 0, "parent": "DN-1"},
			{"si_detail": "r2", "qty": 1, "docstatus": 2, "parent": "DN-2"},
		])
		self.assertEqual(delivered, {})
		self.assertEqual(blocked, {"r1", "r2"})

	def test_a_return_dn_blocks_and_never_counts_as_delivery(self):
		delivered, blocked = self._state([
			{"si_detail": "r1", "qty": 2, "docstatus": 1, "parent": "DN-1"},
			{"si_detail": "r1", "qty": -2, "docstatus": 1, "parent": "DN-RET"},
		], returns=["DN-RET"])
		self.assertEqual(delivered, {"r1": 2.0})
		self.assertEqual(blocked, {"r1"})

	def test_no_rows_no_queries(self):
		from ffl_woo_sync.woocommerce import revenue
		with patch("frappe.get_all") as get_all:
			self.assertEqual(revenue._stock_leg_state([]), ({}, set()))
		get_all.assert_not_called()


class TestDeliverWebStockLines(FrappeTestCase):

	def _run(self, si, dn_rows, *, delivered=None, blocked=None, fail=False,
			deliverable=None, unplaced=None):
		from ffl_woo_sync.woocommerce import revenue
		deliverable = deliverable if deliverable is not None else list(si.items)
		dn = _StubDN(dn_rows, fail_on_submit=fail)

		def _rows(si_, unplaced_list=None):
			if unplaced_list is not None and unplaced:
				unplaced_list.extend(unplaced)
			return deliverable

		with patch.object(revenue, "_web_stock_rows", side_effect=_rows), \
			 patch.object(revenue, "_stock_leg_state",
				return_value=(delivered or {}, blocked or set())), \
			 patch("erpnext.accounts.doctype.sales_invoice.sales_invoice.make_delivery_note",
				return_value=dn) as mdn, \
			 patch.object(revenue, "_alert_web_stock_gap") as alert, \
			 patch("frappe.db.get_value") as lock, \
			 patch("frappe.db.savepoint") as savepoint, \
			 patch("frappe.db.rollback") as rollback, \
			 patch("frappe.get_traceback", return_value="tb"):
			result = revenue.deliver_web_stock_lines(si)
		return frappe._dict(result=result, dn=dn, mdn=mdn, alert=alert, lock=lock,
			savepoint=savepoint, rollback=rollback)

	def test_issues_one_dn_for_the_accessory_rows_on_the_invoice_date(self):
		si = _si([_si_row("r1", "IRAY-RH25", qty=1), _si_row("r2", "GLOCK-19")])
		dn_rows = [{"si_detail": "r1", "qty": 1.0, "item_code": "IRAY-RH25", "idx": 2},
			{"si_detail": "r2", "qty": 1.0, "item_code": "GLOCK-19", "idx": 1}]
		m = self._run(si, dn_rows, deliverable=[si.items[0]])
		self.assertEqual(m.result, ["MAT-DN-TEST-1"])
		m.mdn.assert_called_once_with("ACC-SINV-TEST-7")
		m.lock.assert_called_once_with("Sales Invoice", "ACC-SINV-TEST-7", "name", for_update=True)
		self.assertEqual([d.si_detail for d in m.dn.items], ["r1"])  # gun row dropped
		self.assertEqual([d.idx for d in m.dn.items], [1])           # renumbered
		self.assertEqual(m.dn.taxes, [])                             # freight stays on the SI
		self.assertEqual(m.dn.posting_date, "2026-08-22")
		self.assertEqual(m.dn.posting_time, "10:02:41")
		self.assertEqual(m.dn.set_posting_time, 1)
		self.assertTrue(m.dn.flags.ignore_permissions)
		self.assertTrue(m.dn.inserted and m.dn.submitted)
		self.assertEqual(len(m.dn.comments), 1)
		self.assertIn("ACC-SINV-TEST-7", m.dn.comments[0][1])
		m.alert.assert_not_called()

	def test_already_delivered_rows_make_it_a_noop(self):
		si = _si([_si_row("r1", "IRAY-RH25", qty=1)])
		m = self._run(si, [], delivered={"r1": 1.0})
		self.assertEqual(m.result, [])
		m.mdn.assert_not_called()

	def test_partial_delivery_issues_only_the_remainder(self):
		si = _si([_si_row("r1", "AMMO-9MM", qty=5)])
		dn_rows = [{"si_detail": "r1", "qty": 5.0, "item_code": "AMMO-9MM", "idx": 1}]
		m = self._run(si, dn_rows, delivered={"r1": 3.0})
		self.assertEqual(m.result, ["MAT-DN-TEST-1"])
		self.assertEqual(m.dn.items[0].qty, 2.0)

	def test_a_row_a_human_touched_is_alerted_and_skipped(self):
		si = _si([_si_row("r1", "IRAY-RH25", qty=1), _si_row("r2", "AMMO-9MM", qty=2)])
		dn_rows = [{"si_detail": "r1", "qty": 1.0, "item_code": "IRAY-RH25", "idx": 1},
			{"si_detail": "r2", "qty": 2.0, "item_code": "AMMO-9MM", "idx": 2}]
		m = self._run(si, dn_rows, blocked={"r1"})
		self.assertEqual([d.si_detail for d in m.dn.items], ["r2"])
		m.alert.assert_called_once()
		self.assertIn("IRAY-RH25", m.alert.call_args[0][1])

	def test_both_reasons_ride_one_alert(self):
		"""alert_once dedupes on the invoice's open ToDo, so a second call in the
		same run would be swallowed and its reason lost."""
		si = _si([_si_row("r1", "IRAY-RH25", qty=1), _si_row("r2", "AMMO-9MM", qty=2)])
		m = self._run(si, [], blocked={"r1"}, deliverable=[si.items[0], si.items[1]],
			unplaced=[_si_row("r3", "SLING", warehouse="")])
		m.alert.assert_called_once()
		body = m.alert.call_args[0][1]
		self.assertIn("SLING", body)        # no warehouse
		self.assertIn("IRAY-RH25", body)    # a human's document holds the row

	def test_a_blocked_row_that_owes_nothing_is_not_reported(self):
		"""A Delivery Note cancelled and re-issued by hand leaves the row blocked
		but fully delivered — nothing to report and nothing to do."""
		si = _si([_si_row("r1", "IRAY-RH25", qty=1)])
		m = self._run(si, [], blocked={"r1"}, delivered={"r1": 1.0})
		self.assertEqual(m.result, [])
		m.alert.assert_not_called()
		m.mdn.assert_not_called()

	def test_a_stock_row_without_a_warehouse_is_alerted(self):
		si = _si([_si_row("r1", "IRAY-RH25", warehouse="")])
		m = self._run(si, [], deliverable=[], unplaced=[si.items[0]])
		self.assertEqual(m.result, [])
		m.alert.assert_called_once()
		self.assertIn("no warehouse", m.alert.call_args[0][1])

	def test_a_stock_moving_invoice_is_refused(self):
		si = _si([_si_row("r1", "IRAY-RH25")], update_stock=1)
		m = self._run(si, [])
		self.assertEqual(m.result, [])
		m.lock.assert_not_called()
		m.mdn.assert_not_called()

	def test_a_failing_dn_is_rolled_back_alerted_and_never_raises(self):
		si = _si([_si_row("r1", "IRAY-RH25", qty=1)])
		dn_rows = [{"si_detail": "r1", "qty": 1.0, "item_code": "IRAY-RH25", "idx": 1}]
		m = self._run(si, dn_rows, fail=True)
		self.assertEqual(m.result, [])
		m.savepoint.assert_called_once_with("deliver_web_stock_lines")
		m.rollback.assert_called_once_with(save_point="deliver_web_stock_lines")
		m.alert.assert_called_once()
		self.assertIs(m.alert.call_args[0][0], si)

	def test_even_a_crash_in_the_failure_path_cannot_escape(self):
		from ffl_woo_sync.woocommerce import revenue
		si = _si([_si_row("r1", "IRAY-RH25")])
		with patch.object(revenue, "_deliver_web_stock_lines",
				side_effect=RuntimeError("SAVEPOINT does not exist")), \
			 patch("frappe.log_error") as log:
			self.assertEqual(revenue.deliver_web_stock_lines(si), [])
		self.assertIn("crashed", log.call_args.kwargs["title"])


class TestAlertWebStockGap(FrappeTestCase):
	"""Routed through ffl_core.alerts.alert_once — deduped on the open ToDo
	BEFORE logging (one Error Log per stuck invoice, not three)."""

	def test_uses_alert_once_on_the_invoice(self):
		from ffl_woo_sync.woocommerce import revenue
		si = _si([])
		with patch("ffl_core.alerts.alert_once") as once:
			revenue._alert_web_stock_gap(si, "shelf disagrees")
		once.assert_called_once()
		self.assertEqual(once.call_args.args[:2], ("Sales Invoice", "ACC-SINV-TEST-7"))
		self.assertEqual(once.call_args.kwargs["message"], "shelf disagrees")

	def test_an_alert_failure_is_logged_not_raised(self):
		from ffl_woo_sync.woocommerce import revenue
		with patch("ffl_core.alerts.alert_once", side_effect=RuntimeError("smtp")), \
			 patch("frappe.log_error") as log, \
			 patch("frappe.get_traceback", return_value="tb"):
			revenue._alert_web_stock_gap(_si([]), "x")
		self.assertIn("alert failed", log.call_args.kwargs["title"])


class TestStockLegCancelBlockers(FrappeTestCase):
	"""Ask BEFORE you touch anything: the refund reconcile cancels the payment
	first, so a refusal discovered mid-sequence loses the refund entirely."""

	def _blockers(self, rows, docs, returns=()):
		from ffl_woo_sync.woocommerce import revenue

		def _get_all(doctype, **kw):
			if doctype == "Delivery Note Item":
				filters = kw.get("filters") or {}
				if "parent" in filters:      # the mixed-invoice probe
					return ["ACC-SINV-OTHER"] if filters["parent"] in docs.get(
						"_mixed", ()) else ["ACC-SINV-TEST-7"]
				return [frappe._dict(r) for r in rows]
			if doctype == "Delivery Note":
				return list(returns)
			raise AssertionError(doctype)

		with patch("frappe.get_all", side_effect=_get_all), \
			 patch("frappe.db.get_value", side_effect=lambda dt, n, f, **k: docs.get(n)):
			return revenue.stock_leg_cancel_blockers("ACC-SINV-TEST-7")

	def test_a_plain_submitted_dn_blocks_nothing(self):
		out = self._blockers([{"parent": "DN-1", "docstatus": 1}],
			{"DN-1": frappe._dict(is_return=0, docstatus=1)})
		self.assertEqual(out, [])

	def test_a_draft_dn_blocks(self):
		out = self._blockers([{"parent": "DN-D", "docstatus": 0}],
			{"DN-D": frappe._dict(is_return=0, docstatus=0)})
		self.assertEqual(len(out), 1)
		self.assertIn("DRAFT", out[0])

	def test_a_return_dn_blocks(self):
		out = self._blockers([{"parent": "DN-R", "docstatus": 1}],
			{"DN-R": frappe._dict(is_return=1, docstatus=1)})
		self.assertEqual(len(out), 1)
		self.assertIn("return", out[0])

	def test_a_submitted_return_against_our_dn_blocks(self):
		out = self._blockers([{"parent": "DN-1", "docstatus": 1}],
			{"DN-1": frappe._dict(is_return=0, docstatus=1)}, returns=["DN-RET"])
		self.assertEqual(len(out), 1)
		self.assertIn("DN-RET", out[0])

	def test_a_dn_that_mixes_another_invoice_blocks(self):
		out = self._blockers([{"parent": "DN-MIX", "docstatus": 1}],
			{"DN-MIX": frappe._dict(is_return=0, docstatus=1), "_mixed": ("DN-MIX",)})
		self.assertEqual(len(out), 1)
		self.assertIn("another invoice", out[0])

	def test_a_real_dn_with_a_null_or_blank_unlinked_row_blocks(self):
		"""Exercise the database predicate, not the mocked query seam: an unlinked
		row is still somebody else's stock and must make the DN mixed."""
		from ffl_woo_sync.woocommerce import revenue

		for idx, unlinked in enumerate((None, ""), start=1):
			si_name = f"ACC-SINV-{self._testMethodName}-{idx}"[:140]
			dn_name = f"DN-{self._testMethodName}-{idx}"[:140]
			frappe.db.delete("Delivery Note Item", {"parent": dn_name})
			frappe.db.delete("Delivery Note", {"name": dn_name})
			now = now_datetime()
			frappe.get_doc({
				"doctype": "Delivery Note", "name": dn_name, "docstatus": 1,
				"customer": "_Test Customer", "posting_date": now.date(),
				"creation": now, "modified": now, "owner": "Administrator",
				"modified_by": "Administrator",
			}).db_insert()
			for row_idx, against in enumerate((si_name, unlinked), start=1):
				frappe.get_doc({
					"doctype": "Delivery Note Item",
					"name": f"{dn_name}-row-{row_idx}"[:140], "parent": dn_name,
					"parenttype": "Delivery Note", "parentfield": "items",
					"idx": row_idx, "docstatus": 1, "item_code": "NULL-SAFE-ITEM",
					"qty": 1, "against_sales_invoice": against,
					"creation": now, "modified": now, "owner": "Administrator",
					"modified_by": "Administrator",
				}).db_insert()
			out = revenue.stock_leg_cancel_blockers(si_name)
			self.assertEqual(len(out), 1)
			self.assertIn("another invoice", out[0])


class TestCancelWebStockLines(FrappeTestCase):

	def _run(self, parents, docs):
		from ffl_woo_sync.woocommerce import revenue
		with patch("frappe.get_all", return_value=parents), \
			 patch("frappe.get_doc", side_effect=lambda dt, n: docs[n]):
			return revenue.cancel_web_stock_lines(_si([]))

	def _dn(self, is_return=0):
		dn = MagicMock()
		dn.is_return = is_return
		dn.flags = frappe._dict()
		return dn

	def test_cancels_each_submitted_dn_of_the_invoice_once(self):
		dn = self._dn()
		out = self._run(["DN-1", "DN-1"], {"DN-1": dn})
		self.assertEqual(out, ["DN-1"])
		dn.cancel.assert_called_once()
		self.assertTrue(dn.flags.ignore_permissions)

	def test_a_return_dn_is_left_alone(self):
		dn = self._dn(is_return=1)
		self.assertEqual(self._run(["DN-RET"], {"DN-RET": dn}), [])
		dn.cancel.assert_not_called()

	def test_never_throws_it_runs_mid_transaction(self):
		"""The gate is stock_leg_cancel_blockers; a throw here would roll back a
		refund whose Payment Entry the caller already cancelled."""
		import inspect
		from ffl_woo_sync.woocommerce import revenue
		self.assertNotIn("frappe.throw", inspect.getsource(revenue.cancel_web_stock_lines))


class TestWebCompletionJob(FrappeTestCase):
	"""One place decides antique-retire vs revenue booking; the webhook enqueues
	it, pickup and the backstop run it inline."""

	def test_accessory_or_disposed_order_books_revenue(self):
		from ffl_woo_sync.woocommerce import revenue
		with patch("ffl_core.firearm.is_antique_item", return_value=False):
			self.assertEqual(revenue.web_completion_job(_order(serials=[])),
				revenue.REVENUE_COMPLETION_JOB)

	def test_antique_order_retires_first(self):
		from ffl_woo_sync.woocommerce import revenue
		order = _order(serials=[_srow(item_code="MOSIN-1891")])
		with patch("ffl_core.firearm.is_antique_item", side_effect=lambda c: c == "MOSIN-1891"):
			self.assertEqual(revenue.web_completion_job(order), revenue.ANTIQUE_COMPLETION_JOB)

	def test_book_inline_resolves_the_job_by_dotted_path(self):
		from ffl_woo_sync.woocommerce import revenue
		order = _order(serials=[])
		with patch("ffl_core.firearm.is_antique_item", return_value=False), \
			 patch.object(revenue, "create_sales_invoice_for_web_order",
				return_value={"status": "created"}) as target:
			self.assertEqual(revenue.book_web_order_completion(order), {"status": "created"})
		target.assert_called_once_with(order)


class TestInvoicePostingDate(FrappeTestCase):
	"""Revenue is dated when the order left (transferred_at), not when the job
	happened to run — a late backstop must not move a sale into today."""

	def _build(self, transferred_at):
		from ffl_woo_sync.woocommerce import revenue
		order = _order(order_total=639.0)
		order.transferred_at = transferred_at
		stub = _StubSI()
		with patch.object(revenue, "_resolve_web_customer", return_value="Web Store Customer"), \
			 patch.object(revenue, "_sales_tax_account", return_value="Sales Tax Payable - TSA"), \
			 patch("frappe.new_doc", return_value=stub):
			revenue._build_invoice(order, "Test Store A LLC")
		return stub

	def test_dated_on_the_transferred_moment(self):
		from frappe.utils import getdate
		stub = self._build("2026-08-22 11:10:34")
		self.assertEqual(getdate(stub.posting_date), getdate("2026-08-22"))
		self.assertEqual(str(stub.posting_time), "11:10:34")
		self.assertEqual(stub.set_posting_time, 1)
		self.assertIn("order Transferred 2026-08-22 11:10:34", stub.remarks)

	def test_falls_back_to_today_without_a_stamp_and_says_so(self):
		from frappe.utils import nowdate
		stub = self._build(None)
		self.assertEqual(stub.posting_date, nowdate())
		# The provenance line matters most exactly here — an invoice dated later
		# than the shipment, with no stamp to explain it.
		self.assertIn("no Transferred timestamp", stub.remarks)


class TestCreateInvoiceStockLeg(FrappeTestCase):
	"""The stock leg rides every path that yields a submitted invoice, the
	collection is pinned to the invoice date, and the order row is locked."""

	def test_fresh_invoice_reports_its_delivery_notes(self):
		from ffl_woo_sync.woocommerce import revenue
		order = _order(order_total=639.0)
		stub = _StubSI(grand_total_after_insert=639.0)
		calls = []

		def _db_get_value(doctype, *a, **k):
			calls.append((doctype, a, k))
			return None

		with patch.object(revenue, "_default_company", return_value="Test Store A LLC"), \
			 patch.object(revenue, "_resolve_web_customer", return_value="Web Store Customer"), \
			 patch.object(revenue, "_sales_tax_account", return_value="Sales Tax Payable - TSA"), \
			 patch.object(revenue, "_ensure_paid") as paid, \
			 patch.object(revenue, "deliver_web_stock_lines", return_value=["MAT-DN-9"]) as leg, \
			 patch("frappe.db.get_value", side_effect=_db_get_value), \
			 patch("frappe.new_doc", return_value=stub):
			result = revenue.create_sales_invoice_for_web_order(order)
		self.assertEqual(result["status"], "created")
		self.assertEqual(result["delivery_notes"], ["MAT-DN-9"])
		leg.assert_called_once_with(stub)
		paid.assert_called_once_with(stub, order, posting_date=stub.posting_date)
		self.assertIn(("Woo Online Order", ("WOO-ORD-00001", "name"), {"for_update": True}), calls)

	def test_existing_submitted_invoice_still_gets_its_stock_leg(self):
		from ffl_woo_sync.woocommerce import revenue
		order = _order()
		existing = frappe._dict(name="ACC-SINV-9", docstatus=1)
		si = MagicMock(name="ACC-SINV-9")

		def _db_get_value(doctype, *a, **k):
			return existing if doctype == "Sales Invoice" else None

		with patch.object(revenue, "_default_company", return_value="Test Store A LLC"), \
			 patch.object(revenue, "_ensure_paid"), \
			 patch.object(revenue, "deliver_web_stock_lines", return_value=[]) as leg, \
			 patch("frappe.db.get_value", side_effect=_db_get_value), \
			 patch("frappe.get_doc", return_value=si):
			result = revenue.create_sales_invoice_for_web_order(order)
		self.assertEqual(result["status"], "exists")
		leg.assert_called_once_with(si)


class TestGuestElevation(FrappeTestCase):
	"""The revenue job enqueued by the Guest webhook must not run as Guest."""

	def setUp(self):
		self._prev = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._prev)

	def _booking_user(self):
		from ffl_woo_sync.woocommerce import revenue
		seen = {}

		def _company():
			seen["user"] = frappe.session.user
			raise frappe.ValidationError("stop here")

		with patch.object(revenue, "_default_company", side_effect=_company), \
			 patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				revenue.create_sales_invoice_for_web_order(_order())
		return seen["user"]

	def test_guest_context_runs_the_booking_as_administrator(self):
		frappe.set_user("Guest")
		self.assertEqual(self._booking_user(), "Administrator")
		self.assertEqual(frappe.session.user, "Guest")  # restored after the throw

	def test_staff_context_keeps_its_own_identity(self):
		frappe.set_user("Administrator")
		self.assertEqual(self._booking_user(), "Administrator")


# ------------------------------------------------ real documents (integration)

class TestWebStockLegIntegration(FrappeTestCase):
	"""The stock leg against REAL documents — the one thing the mocks above
	cannot prove: ERPNext's mapper fills si_detail/against_sales_invoice, the DN
	submits, the Bin and the ledger move by exactly the invoiced qty, a second
	run is a no-op, a cancel of the invoice is possible once the DN is cancelled
	first, and the amended invoice gets its own DN with the Bin netting to the
	same place."""

	ITEM = "WSL-OPTIC"
	CUSTOMER = "_Test Web Stock Leg Buyer"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		from ffl_core.warehouses import default_stock_warehouse
		test_bootstrap.run()
		item_groups.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.warehouse = default_stock_warehouse(cls.company)
		if not frappe.db.exists("Item", cls.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM, "item_name": cls.ITEM,
				"item_group": "Accessory", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 0, "is_firearm": 0,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", cls.CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cls.CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

	def _receive(self, qty):
		# Received BEFORE the invoice date: the DN posts on the SI's date, and
		# stock must exist there (a backfilled DN older than its receipt fails
		# with NegativeStockError and is alerted — by design).
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company, "set_posting_time": 1,
			"posting_date": "2026-08-01", "posting_time": "09:00:00",
			"items": [{"item_code": self.ITEM, "qty": qty,
				"t_warehouse": self.warehouse, "basic_rate": 95}],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

	def _bin(self) -> float:
		return flt(frappe.db.get_value(
			"Bin", {"item_code": self.ITEM, "warehouse": self.warehouse}, "actual_qty"))

	def _order(self, tag):
		order = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": f"9{tag}",
			"order_status": "Transferred", "site": "retail",
			"customer_name": self.CUSTOMER, "order_total": 100.0,
		})
		order.flags.ignore_permissions = True
		order.insert()
		return order

	def _invoice(self, order, qty=2):
		si = frappe.get_doc({
			"doctype": "Sales Invoice", "company": self.company,
			"customer": self.CUSTOMER, "update_stock": 0,
			"woo_online_order": order.name, "po_no": order.woo_order_id,
			"set_posting_time": 1, "posting_date": "2026-08-22",
			"items": [{"item_code": self.ITEM, "qty": qty, "rate": 50,
				"warehouse": self.warehouse}],
		})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def _leg(self, si, expect=1):
		from ffl_woo_sync.woocommerce.revenue import deliver_web_stock_lines
		real_log = frappe.log_error
		captured = []

		def _log(*a, **k):
			captured.append(k.get("message") or (a[1] if len(a) > 1 else ""))
			return real_log(*a, **k)

		# Nobody to assign a ToDo to on the test site (and no mail): keep the
		# alert path to the Error Log — and surface it when the leg unexpectedly
		# fails, so the assertion says why.
		with patch("ffl_core.alerts.alert_assignees", return_value=[]), \
			 patch("frappe.log_error", side_effect=_log):
			created = deliver_web_stock_lines(si)
		if expect is not None:
			self.assertEqual(len(created), expect, "\n".join(str(c)[-1500:] for c in captured))
		return created

	def test_issues_moves_stock_and_is_idempotent(self):
		self._receive(3)
		before = self._bin()
		si = self._invoice(self._order("01"), qty=2)
		created = self._leg(si)
		dn = frappe.get_doc("Delivery Note", created[0])
		self.assertEqual(dn.docstatus, 1)
		self.assertEqual(dn.items[0].against_sales_invoice, si.name)
		self.assertEqual(dn.items[0].si_detail, si.items[0].name)
		self.assertEqual(dn.items[0].idx, 1)
		self.assertEqual(str(dn.posting_date), "2026-08-22")
		self.assertEqual(dn.taxes, [])
		self.assertEqual(self._bin(), before - 2)
		sle = frappe.get_all("Stock Ledger Entry",
			filters={"voucher_no": dn.name, "is_cancelled": 0}, fields=["actual_qty"])
		self.assertEqual([flt(r.actual_qty) for r in sle], [-2.0])
		# Second run: nothing owed.
		self.assertEqual(self._leg(si, expect=0), [])
		self.assertEqual(self._bin(), before - 2)

	def test_no_stock_leaves_the_invoice_standing_and_alerts(self):
		from erpnext.stock import stock_ledger
		si = self._invoice(self._order("02"), qty=50)   # far more than the shelf holds
		with patch.object(stock_ledger, "is_negative_stock_allowed", return_value=False), \
			 patch("frappe.log_error") as log:
			created = self._leg(si, expect=0)
		self.assertEqual(created, [])
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)
		self.assertEqual(frappe.get_all("Delivery Note Item",
			filters={"against_sales_invoice": si.name, "docstatus": 1}), [])
		self.assertTrue(any("Web stock delivery failed" in c.kwargs.get("title", "")
			for c in log.call_args_list))

	def test_cancel_and_amend_keep_the_bin_where_it_was(self):
		from ffl_woo_sync.woocommerce.revenue import cancel_web_stock_lines
		self._receive(2)
		before = self._bin()
		si = self._invoice(self._order("03"), qty=1)
		self._leg(si)
		self.assertEqual(self._bin(), before - 1)
		# The refund path's sequence: DN first, then the invoice cancels cleanly.
		cancelled = cancel_web_stock_lines(si)
		self.assertEqual(len(cancelled), 1)
		self.assertEqual(self._bin(), before)
		si.reload()
		si.flags.ignore_permissions = True
		si.cancel()   # would raise LinkExistsError with the DN still submitted
		amended = frappe.copy_doc(si)
		amended.docstatus = 0
		amended.amended_from = si.name
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()
		# The cancelled DN blocks its OLD rows from ever being re-issued …
		from ffl_woo_sync.woocommerce.revenue import _stock_leg_state
		_d, blocked = _stock_leg_state([si.items[0].name])
		self.assertEqual(blocked, {si.items[0].name})
		# … while the amended rows are fresh and get their own leg.
		self._leg(amended)
		self.assertEqual(self._bin(), before - 1)
		self.assertEqual(self._leg(amended, expect=0), [])


class TestTransferredAtStamp(FrappeTestCase):
	"""WooOnlineOrder.validate stamps transferred_at once when the status becomes
	Transferred, and clears it when the order is reopened."""

	def _order(self, tag, status):
		order = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": f"8{tag}",
			"order_status": status, "site": "retail", "customer_name": "Stamp Test",
		})
		order.flags.ignore_permissions = True
		order.insert()
		return order

	def test_stamped_on_transfer_and_kept_on_later_saves(self):
		order = self._order("01", "Pending Fulfillment")
		self.assertFalse(order.transferred_at)
		order.order_status = "Transferred"
		order.save(ignore_permissions=True)
		first = order.transferred_at
		self.assertTrue(first)
		order.customer_phone = "555-0100"
		order.save(ignore_permissions=True)
		self.assertEqual(order.transferred_at, first)

	def test_a_pre_existing_transferred_order_is_never_back_stamped(self):
		"""Every order that was Transferred before this field existed carries NULL.
		An unrelated later save — the order.updated webhook backfilling a serial or
		recording a refund — must not invent "now" as the moment it shipped: that
		fake stamp would become a booked invoice's posting date and its remarks
		provenance line."""
		order = self._order("03", "Transferred")
		frappe.db.set_value("Woo Online Order", order.name, "transferred_at", None,
			update_modified=False)   # what `bench migrate` leaves behind
		order.reload()
		order.customer_phone = "555-0199"
		order.save(ignore_permissions=True)
		self.assertIsNone(order.transferred_at)

	def test_mark_transferred_writes_status_and_stamp_together(self):
		from ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order import (
			mark_transferred, unmark_transferred)
		order = self._order("04", "Pending Fulfillment")
		stamp = mark_transferred(order.name)
		row = frappe.db.get_value("Woo Online Order", order.name,
			["order_status", "transferred_at"], as_dict=True)
		self.assertEqual(row.order_status, "Transferred")
		self.assertEqual(get_datetime(row.transferred_at), get_datetime(stamp))
		unmark_transferred(order.name, status="Pending Fulfillment")
		row = frappe.db.get_value("Woo Online Order", order.name,
			["order_status", "transferred_at"], as_dict=True)
		self.assertEqual(row.order_status, "Pending Fulfillment")
		self.assertIsNone(row.transferred_at)

	def test_cleared_when_reopened(self):
		order = self._order("02", "Transferred")
		self.assertTrue(order.transferred_at)
		order.order_status = "Pending Fulfillment"
		order.save(ignore_permissions=True)
		self.assertFalse(order.transferred_at)
