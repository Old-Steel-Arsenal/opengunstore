"""P2 ship to store at the counter.

A counter line can be ordered INTO the shop from a distributor: the invoice books
the sale now (a deferred, serial-less line pinned with the distributor), the hook
builds a Procure To Store Distributor Order, and receiving that order hangs the
arrived serial on the line — which is the counter hold — so the gun is Disposed
from the Pending Order queue like any local one.

Rules (unittest, stubs answer only the thing under test) first; the site chain
(create_order → DO → receive → hold → queue) after.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.utils import flt
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.compliance import firearm as compliance_firearm


def _exists_known(doctype, name=None, *a, **k):
	if doctype == "Item":
		return name in {"TS-ACC", "TS-GUN"}
	return None


_GUNS = {"TS-GUN"}


class ToStoreLineRules(unittest.TestCase):
	def setUp(self):
		self.block = None
		for p in [
			mock.patch.object(manual_order.frappe.db, "exists", _exists_known),
			mock.patch.object(manual_order, "is_firearm_item", lambda c: c in _GUNS),
			mock.patch.object(manual_order, "is_regulated_firearm_item", lambda c: c in _GUNS),
			mock.patch.object(manual_order, "_to_store_availability_block",
				lambda item_code, distributor, sku: self.block),
		]:
			p.start()
			self.addCleanup(p.stop)
		# restricted_states read for the row — the only get_value this path makes.
		p = mock.patch.object(manual_order.frappe.db, "get_value",
			lambda dt, name, field, *a, **k: "CA" if (dt, field) == ("Item", "restricted_states") else None)
		p.start()
		self.addCleanup(p.stop)

	def _line(self, **kw):
		return {"item_code": "TS-ACC", "qty": 2, "rate": 10, "source": "to_store",
			"distributor": "RSR", "sku": "SKU-1", **kw}

	def test_to_store_row_carries_distributor_sku_and_no_serial(self):
		rows, _s, has_reg = manual_order._validate_items([self._line()])
		self.assertEqual(rows[0]["source"], "to_store")
		self.assertEqual(rows[0]["to_store_distributor"], "RSR")
		self.assertEqual(rows[0]["sku"], "SKU-1")
		self.assertEqual(rows[0]["serials"], [])
		self.assertEqual(rows[0]["restricted_states"], "CA")
		self.assertFalse(has_reg)

	def test_to_store_firearm_is_regulated_and_needs_no_serial(self):
		rows, _s, has_reg = manual_order._validate_items(
			[self._line(item_code="TS-GUN", qty=1)])
		self.assertTrue(has_reg)
		self.assertEqual(rows[0]["serials"], [])

	def test_to_store_firearm_is_one_unit_per_line(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order._validate_items([self._line(item_code="TS-GUN", qty=2)])

	def test_no_distributor_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order._validate_items([self._line(distributor="")])

	def test_unavailable_at_the_distributor_is_refused(self):
		self.block = "TS-ACC is out of stock at RSR."
		with self.assertRaises(frappe.ValidationError):
			manual_order._validate_items([self._line()])

	def test_a_blank_item_on_a_to_store_line_is_refused_not_dropped(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order._validate_items([self._line(item_code="")])

	def test_business_buyer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order._assert_dropship_buyer(
				[{"item_code": "TS-ACC", "source": "to_store"}], is_business=True)

	def test_ordered_leg_defers_the_order(self):
		rows = [{"item_code": "TS-ACC", "source": "to_store"}]
		self.assertTrue(manual_order._order_has_ordered_leg(rows))
		self.assertFalse(manual_order._order_has_dropship(rows))
		self.assertEqual(manual_order._counter_update_stock(
			dispose_now=True, has_dropship=manual_order._order_has_ordered_leg(rows)), 0)

	def test_context_tags_to_store_lines_with_the_planned_house(self):
		with mock.patch.object(manual_order, "_counter_ffl_block", lambda ffl: None):
			ctx = manual_order._counter_distributor_context(
				payload={}, destination_ffl=None, is_business=False,
				rows=[
					{"item_code": "LOCAL", "qty": 1, "rate": 5, "serials": []},
					{"item_code": "TS-GUN", "qty": 1, "rate": 500, "source": "to_store",
						"to_store_distributor": "Sports South", "sku": "SS-9"},
				])
		self.assertEqual(len(ctx["lines"]), 1)
		line = ctx["lines"][0]
		self.assertEqual(line["lane"], "to_store")
		self.assertEqual(line["distributor"], "Sports South")
		self.assertEqual(line["sku"], "SS-9")
		self.assertTrue(line["is_firearm"])


class PickupOrderRules(unittest.TestCase):
	"""A counter pickup has no destination, so three shapes are refused — and a
	regulated gun is allowed only as a ship-to-store line (api/counter_pickup)."""

	def setUp(self):
		p = mock.patch.object(manual_order, "is_regulated_firearm_item", lambda c: c in _GUNS)
		p.start()
		self.addCleanup(p.stop)

	def test_a_firearm_from_stock_is_not_a_counter_pickup(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order._assert_pickup_order([{"item_code": "TS-GUN"}], {}, True)
		self.assertIn("TS-GUN", str(ctx.exception))

	def test_a_firearm_ordered_to_the_store_may_be_picked_up(self):
		manual_order._assert_pickup_order(
			[{"item_code": "TS-GUN", "source": "to_store"}, {"item_code": "TS-ACC"}], {}, True)

	def test_a_destination_ffl_is_not_a_counter_pickup(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order._assert_pickup_order([], {"destination_ffl": "1-99-888"}, False)

	def test_a_dropship_line_is_refused_and_named(self):
		rows = [{"item_code": "TS-ACC", "source": "dropship"}]
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order._assert_pickup_order(rows, {}, False)
		self.assertIn("TS-ACC", str(ctx.exception))

	def test_local_and_to_store_lines_are_fine(self):
		manual_order._assert_pickup_order(
			[{"item_code": "TS-ACC", "source": "to_store"}, {"item_code": "L"}], {}, False)


class AwaitingProcurementPredicate(unittest.TestCase):
	"""The narrow exemption that lets a serial-less ship-to-store gun submit."""

	def setUp(self):
		p = mock.patch.object(compliance_firearm, "is_firearm_item", lambda c: c in _GUNS)
		p.start()
		self.addCleanup(p.stop)

	def _doc(self, **kw):
		# SimpleNamespace, not frappe._dict: a dict's own .items() would shadow the
		# invoice's `items` table.
		return SimpleNamespace(**{"doctype": "Sales Invoice", "update_stock": 0, **kw})

	def _row(self, **kw):
		return frappe._dict({"item_code": "TS-GUN", "ffl_to_store_distributor": "RSR",
			"delivered_by_supplier": 0, "serial_no": "", **kw})

	def test_awaiting_gun_line(self):
		self.assertTrue(compliance_firearm.is_awaiting_procurement_row(self._doc(), self._row()))

	def test_each_condition_is_load_bearing(self):
		f = compliance_firearm.is_awaiting_procurement_row
		self.assertFalse(f(self._doc(update_stock=1), self._row()))
		self.assertFalse(f(self._doc(doctype="Delivery Note"), self._row()))
		self.assertFalse(f(self._doc(), self._row(ffl_to_store_distributor="")))
		self.assertFalse(f(self._doc(), self._row(delivered_by_supplier=1)))
		self.assertFalse(f(self._doc(), self._row(serial_no="SN-1")))
		self.assertFalse(f(self._doc(), self._row(item_code="TS-ACC")))

	def test_submit_guard_lets_only_the_awaiting_line_through(self):
		doc = self._doc(docstatus=1, items=[self._row()])
		compliance_firearm.require_serial_for_firearm(doc)  # no raise
		doc.items.append(self._row(ffl_to_store_distributor="", idx=2))
		with self.assertRaises(frappe.ValidationError):
			compliance_firearm.require_serial_for_firearm(doc)


# ---------------------------------------------------------------- integration

from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin  # noqa: E402
from ffl_core.tests import test_counter_dropship_integration as p4b  # noqa: E402
from ffl_integrations.tests import catalog_stub  # noqa: E402

DISTRIBUTOR = "TS-P2-DC"
SUPPLIER = "RSR GROUP INC"
GUN = "TS-P2-GLK19"        # a brand the house won't drop-ship — the P2 customer
ACC = "TS-P2-HOLO"


def _ensure(doctype, name, payload):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	doc = frappe.get_doc({"doctype": doctype, **payload})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


class CounterToStoreFixtures(BuildOrderMixin, FrappeTestCase):
	"""Masters + helpers for ship-to-store orders, WITHOUT test methods. Other
	modules subclass this, never CounterToStoreIntegration: unittest collects
	inherited (and imported) test_ methods, so they would all re-run there."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		p4b.CounterDropshipIntegration.company = cls.company
		p4b.CounterDropshipIntegration._ensure_default_tax_template()
		p4b.CounterDropshipIntegration._ensure_pos_profile_default_tax()
		p4b.CounterDropshipIntegration._ensure_oos_template()
		cls.stub = catalog_stub.install()
		cls._fixture()

	@classmethod
	def tearDownClass(cls):
		catalog_stub.uninstall()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError
		p = mock.patch.object(FastBoundClient, "from_settings",
			side_effect=FastBoundConfigError("disabled in counter to-store tests"))
		p.start()
		self.addCleanup(p.stop)

	@classmethod
	def _fixture(cls):
		from ffl_integrations.tests.site_fixtures import ensure_ffl_supplier
		ensure_ffl_supplier(SUPPLIER, ffl_number="9-99-999-99-9X-99961")
		_ensure("Brand", "Glock", {"brand": "Glock"})
		_ensure("Caliber", "9mm", {"caliber_name": "9mm"})
		_ensure("Distributor", DISTRIBUTOR, {
			"code": DISTRIBUTOR, "distributor_name": "P2 To Store DC",
			"provider": "rsr", "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
			"supplier": SUPPLIER, "safety_buffer": 0, "display_cap": 0})
		# _ensure keeps a leftover row as it is — and one left switched off (seen on a
		# worktree dev site) fails every order here with "not enabled". In-transaction.
		frappe.db.set_value("Distributor", DISTRIBUTOR, {"enabled": 1, "dropship_enabled": 1},
			update_modified=False)
		for code, is_firearm, dept in ((GUN, 1, "01"), (ACC, 0, "10")):
			if not frappe.db.exists("Item", code):
				doc = frappe.get_doc({
					"doctype": "Item", "item_code": code, "item_name": code,
					"item_group": ("Pistol" if is_firearm else "Accessory")
						if frappe.db.exists("Item Group", "Pistol" if is_firearm else "Accessory")
						else "All Item Groups",
					"is_firearm": is_firearm, "is_stock_item": 1,
					"has_serial_no": 1 if is_firearm else 0,
					"uniform_sell_price": 1 if is_firearm else 0, "stock_uom": "Nos",
					"manufacturer": "Glock" if is_firearm else None,
					"model_name": code if is_firearm else None,
					"firearm_type": "Pistol" if is_firearm else None,
					"caliber": "9mm" if is_firearm else None})
				doc.flags.ignore_permissions = True
				doc.insert()
			# The house's catalog carries it, blocked for drop-ship: ship to store is
			# the only way — exactly the P2 case.
			catalog_stub.seed_catalog([{
				"rsr_stock_number": code, "dealer_price": 430.0, "quantity": 9,
				"ffl_required": is_firearm, "department_number": dept, "model": code,
				"dropship_blocked": 1}])

	# ------------------------------------------------------------- helpers

	def _pos_profile(self) -> str:
		return frappe.db.get_value("POS Profile", {"company": self.company, "disabled": 0}, "name")

	def _counter(self, items, *, ffl=None, ship_to=None, buyer="Tess ToStore"):
		payload = {
			"buyer_type": "individual", "customer": self._indiv(buyer),
			"pos_profile": self._pos_profile(), "dispose_now": 1,
			"payment_received": 1, "mode_of_payment": "Cash",
			"contact_email": "ts@example.com", "items": items,
		}
		if ffl:
			payload["destination_ffl"] = ffl
		if ship_to:
			payload["ship_to"] = ship_to
		return manual_order.create_order(payload)

	def _line(self, code, **kw):
		return {"item_code": code, "qty": 1, "rate": 600, "serials": [],
			"source": "to_store", "distributor": DISTRIBUTOR, "sku": code, **kw}

	def _only_do(self, si_name):
		names = frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": si_name}, pluck="name")
		self.assertEqual(len(names), 1)
		return frappe.get_doc("Distributor Order", names[0])

	def _ship_to_store(self, do, serial=None):
		"""The distributor shipped the order to us: invoiced (+ serial echoed)."""
		for line in do.lines:
			frappe.db.set_value("Distributor Order Line", line.name,
				{"ack_qty": line.wish_qty, "invoiced_qty": line.wish_qty,
					"serials": serial or "", "line_status": "Shipped"}, update_modified=False)
		frappe.db.set_value("Distributor Order", do.name, "status", "Complete",
			update_modified=False)
		return frappe.get_doc("Distributor Order", do.name)

	def _receive(self, do):
		from ffl_core.warehouses import default_stock_warehouse
		from ffl_integrations.distributor import receive_bridge as rb

		payload = rb.build_receive_payload(do.name)
		payload["warehouse"] = default_stock_warehouse(self.company)
		return rb.receive_from_do(do.name, json.dumps(payload))

	def _queue_row(self, si_name):
		return next((r for r in manual_order.list_pending_dispositions()
			if r.get("sales_invoice") == si_name), None)


class CounterToStoreIntegration(CounterToStoreFixtures):

	# ------------------------------------------------------------- the claims

	def test_to_store_firearm_books_the_sale_and_a_procure_order(self):
		# Another customer's arrived gun of the same model sits on the shelf — the
		# new line must not be handed it by ERPNext's FIFO serial pick.
		self._receive(self._ship_to_store(self._only_do(
			self._counter([self._line(GUN)], ffl=DEST_FFL)["sales_invoice"]), serial="TS-P2-SHELF"))
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(int(si.update_stock), 0)
		row = si.items[0]
		self.assertFalse(row.serial_no, "no gun yet — the serial comes on arrival")
		self.assertEqual(row.ffl_to_store_distributor, DISTRIBUTOR)
		self.assertFalse(int(row.delivered_by_supplier or 0))
		self.assertFalse(row.get("dropship_distributor"),
			"never a drop-ship row — the Sales Report would cost it from a DO")
		do = self._only_do(si.name)
		self.assertEqual(do.order_type, "Procure To Store")
		self.assertEqual(do.account, "dealer")
		self.assertEqual(do.status, "Draft")
		self.assertFalse(do.ship_address_line1)
		self.assertEqual([l.fulfillment for l in do.lines], ["to_store"])

	def test_unpaid_pickup_order_may_place_its_ship_to_store_leg(self):
		"""The goods come to our shelf, so a deposit (or nothing) is enough to order
		them; the hand-over still waits for the balance (counter_pickup)."""
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Deposit Dan"),
			"pos_profile": self._pos_profile(), "dispose_now": 0,
			"payment_received": 0, "mode_of_payment": "Cash",
			"contact_email": "dan@example.com", "pickup_in_store": 1,
			"items": [self._line(GUN)],
		})
		si_name = out["sales_invoice"]
		self.assertGreater(out["outstanding"], 0)
		row = self._queue_row(si_name)
		self.assertTrue(row["legs"]["releasable"])
		self.assertEqual(manual_order.blocked_reason(row), "")
		result = manual_order.release_order_legs(si_name)
		self.assertEqual(result["failed"], [])
		self.assertEqual(len(result["released"]), 1)
		self.assertEqual(frappe.db.get_value(
			"Distributor Order", result["released"][0], "status"), "Queued")

	def test_awaiting_gun_keeps_the_order_on_the_queue_and_blocks_shipping(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		row = self._queue_row(si_name)
		self.assertIsNotNone(row)
		self.assertEqual(row["legs"]["awaiting_arrival"], 1)
		self.assertEqual(row["pending_serials"], [])
		self.assertFalse(row["can_mark_shipped"])
		# Even once the distributor reports the order Complete, it has not ARRIVED.
		self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-Q")
		self.assertIsNotNone(self._queue_row(si_name))

	def test_receiving_hangs_the_serial_as_a_counter_hold(self):
		from ffl_core.reservations import counter_holds

		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		do = self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-1")
		self._receive(do)

		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertEqual(si.items[0].serial_no, "TS-P2-SN-1")
		self.assertEqual(counter_holds(GUN).get("TS-P2-SN-1", {}).get("ref"), si_name,
			"the arrived gun is held for this order")
		row = self._queue_row(si_name)
		self.assertEqual(row["legs"]["awaiting_arrival"], 0)
		self.assertEqual([p["serial"] for p in row["pending_serials"]], ["TS-P2-SN-1"],
			"ready to Dispose from the queue")
		self.assertTrue(any("TS-P2-SN-1" in (c.content or "") for c in frappe.get_all(
			"Comment", filters={"reference_doctype": "Sales Invoice",
				"reference_name": si_name}, fields=["content"])))

	def test_arrived_gun_disposes_through_the_normal_chain(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-D"))
		self.assertTrue(frappe.db.exists("FFL Acquisition", {"serial": "TS-P2-SN-D", "docstatus": 1}),
			"a ship-to-store gun enters OUR bound book on arrival")
		res = manual_order.dispose_order(si_name)
		self.assertEqual(res["disposed_count"], 1)
		self.assertTrue(frappe.db.exists("FFL Disposition",
			{"serial": "TS-P2-SN-D", "sales_invoice": si_name, "docstatus": 1}))

	def test_hang_is_idempotent_and_never_moves_a_serial(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		do = self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-2")
		self._receive(do)
		again = manual_order.assign_procured_serials(si_name, {GUN: ["TS-P2-SN-OTHER"]})
		self.assertEqual(again["assigned"], [])
		self.assertEqual(frappe.db.get_value("Sales Invoice Item",
			{"parent": si_name}, "serial_no"), "TS-P2-SN-2")

	def test_accessory_waits_for_arrival_before_shipping(self):
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=2, rate=40)], ship_to=ship_to)
		si_name = out["sales_invoice"]
		si = frappe.get_doc("Sales Invoice", si_name)
		block = manual_order.mark_shipped_block(si, [], set())
		self.assertIn("waiting for ship-to-store goods", block or "")
		do = self._receive(self._ship_to_store(self._only_do(si_name)))
		self.assertTrue(do)
		self.assertIsNone(manual_order.mark_shipped_block(
			frappe.get_doc("Sales Invoice", si_name), [], set()))

	def test_sales_report_costs_an_unarrived_accessory_from_the_order_line(self):
		"""A past invoice (prod, 2026-09-16): ship-to-store accessories
		read cost 0 — never stocked, so no Bin average — and a phantom 100 % margin.
		The Distributor Order's acknowledged cost is what these goods cost."""
		from ffl_core.ffl_core.report.sales_report import sales_report

		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=5, rate=13.99)], ship_to=ship_to)
		si_name = out["sales_invoice"]
		do = self._only_do(si_name)
		for line in do.lines:
			line.db_set("ack_qty", 5)
			line.db_set("unit_cost", 13.03)
		rows = sales_report.execute({
			"from_date": frappe.utils.nowdate(), "to_date": frappe.utils.nowdate(),
			"company": self.company, "view": "Product"})[1]
		line = next(r for r in rows if r.get("row_kind") == "product"
			and r.get("invoice") == si_name and r.get("item_code") == ACC)
		self.assertAlmostEqual(line["cost"], 5 * 13.03, places=2)
		self.assertFalse(line["missing_cost"])

	def test_pickup_order_books_with_no_address_and_only_to_store(self):
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Pick Up Pete"),
			"pos_profile": self._pos_profile(), "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash",
			"contact_email": "pick@example.com", "pickup_in_store": 1,
			# an address typed before the toggle went on must not book
			"ship_to": {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX",
				"pincode": "75024"},
			"items": [self._line(ACC, qty=1, rate=40)],
		})
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(si.shipping_address_name, "a pickup books no shipping address")
		self.assertEqual(self._only_do(si.name).order_type, "Procure To Store")
		self.assertEqual(
			manual_order.get_order_for_edit(si.name)["payload"]["pickup_in_store"], 1)

	def test_pickup_charges_no_freight_even_if_one_was_typed(self):
		"""The page clears the box when Pickup goes on, but a figure typed after that
		would ride the payload through — and nothing is being shipped."""
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Pick Up Pete"),
			"pos_profile": self._pos_profile(), "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash",
			"pickup_in_store": 1, "shipping_amount": 15,
			"items": [self._line(ACC, qty=1, rate=40)],
		})
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(flt(si.ffl_shipping_amount), 0.0)
		self.assertEqual(
			manual_order.get_order_for_edit(si.name)["payload"]["shipping_amount"], 0.0)

	def test_pickup_refuses_a_dropship_line(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.create_order({
				"buyer_type": "individual", "customer": self._indiv("Pick Up Pete"),
				"pos_profile": self._pos_profile(), "payment_received": 1,
				"mode_of_payment": "Cash", "pickup_in_store": 1,
				"items": [{"item_code": ACC, "qty": 1, "rate": 40, "serials": [],
					"source": "dropship"}]})

	def test_pickup_refuses_a_firearm_with_a_destination_ffl(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.create_order({
				"buyer_type": "individual", "customer": self._indiv("Pick Up Pete"),
				"pos_profile": self._pos_profile(), "payment_received": 1,
				"mode_of_payment": "Cash", "pickup_in_store": 1,
				"destination_ffl": DEST_FFL,
				"items": [self._line(GUN)]})

	def test_pickup_refuses_a_firearm_from_stock(self):
		"""A gun on the shelf that the customer takes home today is a register
		sale with a 4473 — not a counter order."""
		# A free shelf gun: one that arrived for another customer's order is held
		# by that order, and the submit gate refuses it before the pickup rule.
		self._stocked_serial("TS-P2-SHELF-PU", GUN)
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.create_order({
				"buyer_type": "individual", "customer": self._indiv("Pick Up Pete"),
				"pos_profile": self._pos_profile(), "payment_received": 1,
				"mode_of_payment": "Cash", "pickup_in_store": 1,
				"items": [{"item_code": GUN, "qty": 1, "rate": 600, "serials": ["TS-P2-SHELF-PU"]}]})
		self.assertIn("ordered to the store", str(ctx.exception))

	# ------------------------------------------- pickup of a gun ordered to the store

	def _pickup(self, items, *, paid=True, buyer="Pick Up Pete"):
		return manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv(buyer),
			"pos_profile": self._pos_profile(), "dispose_now": 0,
			"payment_received": 1 if paid else 0, "mode_of_payment": "Cash",
			"contact_email": "pick@example.com", "pickup_in_store": 1, "items": items})

	def _open_register(self):
		from ffl_core.tests.pos_opening import todays_pos_opening
		return todays_pos_opening(self.company, self._pos_profile())

	def _close_register(self):
		for name in frappe.get_all("POS Opening Entry",
				filters={"status": "Open", "docstatus": 1, "company": self.company}, pluck="name"):
			frappe.db.set_value("POS Opening Entry", name, "status", "Closed", update_modified=False)

	def _ticket_for(self, si_name):
		from ffl_core.api.counter_pickup import open_pickup_drafts_for_invoices
		return open_pickup_drafts_for_invoices([si_name]).get(si_name)

	def _cover(self, ticket, serial):
		"""What FastBound's 4473 webhook leaves behind on a finished sale: a
		submitted Retail Sale disposition on the ticket, and the ticket's own
		4473 / NICS fields set (webhook._ensure_disposition_coverage)."""
		from frappe.utils import now_datetime
		pos = frappe.db.get_value("POS Invoice", ticket, ["company", "customer"], as_dict=True)
		disp = frappe.get_doc({
			"doctype": "FFL Disposition", "disposition_date": now_datetime(),
			"disposition_type": "Retail Sale", "company": pos.company,
			"customer": pos.customer, "item": GUN, "serial": serial,
			"linked_pos_invoice": ticket, "form_4473_status": "Completed",
			"nics_status": "Proceed", "fastbound_id": f"FB-TEST-PU-{serial}"})
		disp.flags.ignore_permissions = True
		disp.insert()
		disp.submit()
		frappe.db.set_value("POS Invoice", ticket,
			{"form_4473_status": "Completed", "nics_status": "Proceed"}, update_modified=False)

	def test_unpaid_pickup_ticket_opens_but_the_gun_waits_for_the_balance(self):
		"""The 4473 may run while the customer still owes; the hand-over may not.
		Recent Orders reads the ORDER's money for the ticket (deposit / due)."""
		from ffl_core.api.pos_order_status import draft_order_status

		self._open_register()
		out = self._pickup([self._line(GUN)], paid=False)
		si_name = out["sales_invoice"]
		total = flt(out["outstanding"])
		self.assertGreater(total, 0)
		manual_order.record_payment(si_name, "Cash", amount=100)
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-OWE"))
		ticket = self._ticket_for(si_name)
		self.assertTrue(ticket, "a deposit order still gets its 4473 ticket on arrival")
		facts = draft_order_status([ticket])[ticket]
		self.assertEqual(facts["counter_order"],
			{"name": si_name, "total": total, "outstanding": total - 100})

		self._cover(ticket, "TS-P2-SN-OWE")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as cm:
			pos.submit()
		self.assertIn("still owes", str(cm.exception))
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 0)

		# the register collects the rest on the order, then hands over
		manual_order.record_payment(si_name, "Cash")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.submit()
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 1)
		self.assertEqual(draft_order_status([ticket]), {}, "submitted — no longer a draft chip")

	def test_pickup_gun_ordered_to_the_store_hands_over_on_a_pos_ticket(self):
		from ffl_core.api import counter_pickup
		from ffl_core.reservations import counter_holds, si_firearm_serial_state

		self._open_register()
		out = self._pickup([self._line(GUN)])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.docstatus, 1, "submits with no destination FFL: nothing moves yet")
		self.assertEqual(int(si.ffl_pickup_in_store), 1)
		self.assertFalse(si.ffl_destination_ffl)
		self.assertEqual(int(si.update_stock), 0)
		self.assertEqual(self._only_do(si.name).order_type, "Procure To Store")
		row = self._queue_row(si.name)
		self.assertTrue(row["store_pickup"])
		self.assertIsNone(row["pickup_invoice"])
		self.assertEqual(row["stage"], "to_place", "the procure order is still to be placed")
		with self.assertRaises(frappe.ValidationError):
			manual_order.dispose_order(si.name)
		with self.assertRaises(frappe.ValidationError):
			counter_pickup.open_pickup_invoice(si.name)

		# arrival: the receive hangs the serial AND opens the 4473 ticket
		self._receive(self._ship_to_store(self._only_do(si.name), serial="TS-P2-SN-PU"))
		ticket = self._ticket_for(si.name)
		self.assertTrue(ticket, "a register is open, so the ticket opens on arrival")
		pos = frappe.get_doc("POS Invoice", ticket)
		self.assertEqual(pos.docstatus, 0)
		self.assertEqual(pos.ffl_counter_order, si.name)
		self.assertEqual(pos.customer, si.customer)
		self.assertEqual([(r.item_code, r.serial_no, flt(r.rate)) for r in pos.items],
			[(GUN, "TS-P2-SN-PU", 0.0)], "the money is on the order — the ticket carries none")
		self.assertEqual(flt(pos.grand_total), 0.0)
		self.assertEqual([flt(p.amount) for p in pos.payments], [0.0])
		self.assertEqual(int(pos.update_stock), 1, "Complete Order takes the gun out of stock")
		self.assertEqual(counter_holds(GUN).get("TS-P2-SN-PU", {}).get("ref"), si.name,
			"held for this order although it has no destination FFL")
		row = self._queue_row(si.name)
		self.assertEqual(row["stage"], "ready_pickup")
		self.assertEqual(row["pickup_invoice"], ticket)
		self.assertFalse(row["needs_disposition"], "no Dispose — the buyer collects here")
		self.assertEqual(counter_pickup.open_pickup_invoice(si.name),
			{"ok": True, "pos_invoice": ticket, "already": True}, "one ticket per order")
		# an open ticket no longer blocks a reprice: the edit rebuilds the gun as a
		# serial-less ship-to-store line and carries ticket + serial over
		self.assertEqual(manual_order.get_order_for_edit(si.name)["payload"]["items"][0]["source"],
			"to_store")

		# the 4473 clears on the ticket and the cashier completes the order
		self._cover(ticket, "TS-P2-SN-PU")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.submit()
		si.reload()
		pending, disposed, _rows = si_firearm_serial_state(si)
		self.assertEqual(pending, [])
		self.assertEqual(disposed, {"TS-P2-SN-PU"}, "the order reads the ticket's 4473")
		self.assertEqual(si.shipstation_status, "Shipped", "nothing else owed — cleared")
		self.assertIsNone(self._queue_row(si.name), "the order has left the queue")
		self.assertNotIn("TS-P2-SN-PU", counter_holds(GUN))
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, reason="test", refund_mode="Cash")

	def test_a_ticket_that_would_charge_leaves_nothing_behind(self):
		"""Review: the $0 assertion runs after the insert and the arrival path
		swallows the raise (the receive is committed). Without the savepoint a
		charging draft stayed on the order — linked, offered as "Open counter
		order", blocking Edit / Cancel."""
		from ffl_core.api import counter_pickup

		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		# Force a charging ticket: stand in for a pricing pass the pin lost to.
		def _charge(doc):
			for row in doc.items:
				row.rate = 1
		with mock.patch.object(counter_pickup, "_pin_zero", _charge):
			self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU4"))
		self.assertIsNone(self._ticket_for(si_name), "no charging draft may survive")
		self.assertFalse(frappe.get_all("POS Invoice",
			filters={"ffl_counter_order": si_name}, pluck="name"))
		row = self._queue_row(si_name)
		self.assertEqual(row["stage"], "ready_pickup", "the order is still ready — the button retries")
		res = counter_pickup.open_pickup_invoice(si_name)
		self.assertFalse(res["already"], "with the pin back the ticket opens at $0")

	def _reprice_like_the_register(self, pos, price=670):
		"""What a later save from the POS page did to a live ticket: the price list
		re-applied to the gun, the card tile filled with the total, and a stale
		change amount ERPNext's validate_change_amount never resets."""
		for row in pos.items:
			row.price_list_rate = price
			row.rate = price
			row.amount = price
		pay = pos.payments[0]
		pay.amount = pay.base_amount = price
		pos.paid_amount = pos.base_paid_amount = price
		pos.change_amount = pos.base_change_amount = price
		pos.grand_total = pos.rounded_total = price

	def _assert_free(self, name):
		pos = frappe.get_doc("POS Invoice", name)
		self.assertEqual([flt(r.rate) for r in pos.items], [0.0])
		self.assertEqual([flt(r.price_list_rate) for r in pos.items], [0.0])
		self.assertEqual([flt(r.amount) for r in pos.items], [0.0])
		self.assertTrue(all(flt(p.amount) == 0 and flt(p.base_amount) == 0 for p in pos.payments),
			[(p.mode_of_payment, p.amount) for p in pos.payments])
		self.assertEqual((flt(pos.grand_total), flt(pos.rounded_total), flt(pos.paid_amount),
			flt(pos.change_amount), flt(pos.base_change_amount), flt(pos.write_off_amount)),
			(0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
		return pos

	def test_a_repriced_ticket_saves_back_to_zero_and_still_hands_over(self):
		"""Incident: a POS-page save re-rated a live ticket to the gun's list price
		with a matching card tender, and Complete Order asked to charge the
		customer a second time. Every save of a ticket comes back at $0 — paid 0,
		change 0 — and the $0 ticket still submits against its settled order."""
		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU7"))
		ticket = self._ticket_for(si_name)

		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		self._reprice_like_the_register(pos)
		pos.save()
		self._assert_free(ticket)

		# Complete Order on a register still holding the re-rated doc: the submit
		# itself pins it, and both counter-order hooks still pass.
		self._cover(ticket, "TS-P2-SN-PU7")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		self._reprice_like_the_register(pos)
		pos.submit()
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 1)
		self._assert_free(ticket)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si_name, "shipstation_status"), "Shipped",
			"close_counter_order_on_pos_submit cleared the order")
		self.assertIsNone(self._queue_row(si_name))

	def test_a_ticket_refuses_an_item_the_order_did_not_sell(self):
		"""The Sales Report skips tickets: anything rung on one would vanish from
		reporting, and the $0 pin would give it away."""
		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU8"))
		ticket = self._ticket_for(si_name)
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.append("items", {"item_code": ACC, "qty": 1, "rate": 40,
			"warehouse": pos.items[0].warehouse})
		with self.assertRaises(frappe.ValidationError) as cm:
			pos.save()
		self.assertIn("ring other items as a separate sale", str(cm.exception))
		self.assertEqual(len(frappe.get_doc("POS Invoice", ticket).items), 1)

	def test_a_ticket_refuses_more_of_the_ordered_gun_than_the_order_sold(self):
		"""Review r1: the check compared item codes only, so a second serial of the
		same model, or qty 2 of the sold line, passed and was pinned to $0."""
		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU9"))
		ticket = self._ticket_for(si_name)

		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.append("items", {"item_code": GUN, "qty": 1, "serial_no": "TS-P2-SN-NOT-SOLD",
			"use_serial_batch_fields": 1, "warehouse": pos.items[0].warehouse})
		with self.assertRaises(frappe.ValidationError) as cm:
			pos.save()
		self.assertIn("TS-P2-SN-NOT-SOLD", str(cm.exception))
		self.assertIn("is not on order", str(cm.exception))

		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.items[0].qty = 2
		with self.assertRaises(frappe.ValidationError) as cm:
			pos.save()
		self.assertIn("more than order", str(cm.exception))

		saved = frappe.get_doc("POS Invoice", ticket)
		self.assertEqual([(r.item_code, flt(r.qty), r.serial_no) for r in saved.items],
			[(GUN, 1.0, "TS-P2-SN-PU9")], "the ticket kept only what the order sold")

	def test_ticket_books_on_the_day_it_is_rung_not_the_day_the_gun_arrived(self):
		"""Review: opened at arrival with set_posting_time, the ticket froze on
		the arrival day and a later submit back-dated the stock issue and the
		consolidated invoice — a different day from the FastBound 4473."""
		from frappe.utils import add_days, getdate, nowdate

		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU5"))
		ticket = self._ticket_for(si_name)
		pos = frappe.get_doc("POS Invoice", ticket)
		self.assertEqual(int(pos.set_posting_time or 0), 0)
		# The gun arrived "three days ago": pretend the draft has sat since.
		frappe.db.set_value("POS Invoice", ticket, "posting_date", add_days(nowdate(), -3),
			update_modified=False)
		self._cover(ticket, "TS-P2-SN-PU5")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.submit()
		self.assertEqual(getdate(pos.posting_date), getdate(nowdate()))

	def test_cancelling_the_ticket_puts_the_order_back_on_the_queue(self):
		"""Review: the submit hook stamped the order Shipped with no cancel
		counterpart — the gun came back to stock unheld, the order never
		reappeared, and the picker could sell the customer's gun to a walk-in."""
		from ffl_core.reservations import counter_holds, si_firearm_serial_state

		self._open_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU6"))
		ticket = self._ticket_for(si_name)
		self._cover(ticket, "TS-P2-SN-PU6")
		pos = frappe.get_doc("POS Invoice", ticket)
		pos.flags.ignore_permissions = True
		pos.submit()
		self.assertIsNone(self._queue_row(si_name))
		# Frappe refuses to cancel a document another submitted document links:
		# the 4473 disposition goes first (what manager_redo_4473 does), then the
		# ticket — the shape of any real ticket reversal.
		for name in frappe.get_all("FFL Disposition",
				filters={"linked_pos_invoice": ticket, "docstatus": 1}, pluck="name"):
			disp = frappe.get_doc("FFL Disposition", name)
			disp.flags.ignore_permissions = True
			disp.cancel()
		pos.reload()
		pos.cancel()
		si = frappe.get_doc("Sales Invoice", si_name)
		self.assertNotEqual(si.shipstation_status, "Shipped")
		pending, _disposed, _rows = si_firearm_serial_state(si)
		self.assertEqual([p["serial"] for p in pending], ["TS-P2-SN-PU6"], "the gun is owed again")
		self.assertEqual(counter_holds(GUN).get("TS-P2-SN-PU6", {}).get("ref"), si_name,
			"and held for this customer again")
		row = self._queue_row(si_name)
		self.assertIsNotNone(row, "back on the queue")
		self.assertIsNone(row["pickup_invoice"], "the cancelled ticket is not an open draft")

	def test_pickup_ticket_waits_for_a_register_and_the_queue_button_opens_it(self):
		from ffl_core.api import counter_pickup

		self._close_register()
		out = self._pickup([self._line(GUN)])
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-PU2"))
		self.assertIsNone(self._ticket_for(si_name), "no register open — nothing to ring under")
		row = self._queue_row(si_name)
		self.assertEqual(row["stage"], "ready_pickup")
		self.assertIsNone(row["pickup_block"], "ready: the button opens it")
		with self.assertRaises(frappe.ValidationError):
			counter_pickup.open_pickup_invoice(si_name)
		self._open_register()
		res = counter_pickup.open_pickup_invoice(si_name)
		self.assertFalse(res["already"])
		self.assertEqual(self._ticket_for(si_name), res["pos_invoice"])

	def test_business_buyer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.create_order({
				"buyer_type": "business", "customer": self._indiv("TS Biz"),
				"pos_profile": self._pos_profile(), "payment_received": 1,
				"mode_of_payment": "Cash",
				"items": [self._line(ACC, rate=40)]})

	def test_edit_locks_the_line_a_distributor_is_already_shipping(self):
		"""Review #1 revisited: the edit is allowed once the order has left the queue,
		but that line is LOCKED — its order moves to the rebuilt invoice rather than
		being ordered again (test_edit_order_placed_do covers the whole round trip)."""
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		do = self._ship_to_store(self._only_do(out["sales_invoice"]), serial="TS-P2-SN-E")
		item = manual_order.get_order_for_edit(out["sales_invoice"])["payload"]["items"][0]
		self.assertEqual(item["ordered_do"], do.name)

	def test_ship_is_refused_until_to_store_goods_arrive(self):
		"""Review #3: the ShipStation push shares the arrival gate with Mark Shipped."""
		from ffl_integrations.shipstation import api as ss_api

		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=1, rate=40)], ship_to=ship_to)
		row = self._queue_row(out["sales_invoice"])
		self.assertTrue(row["legs"]["awaiting_goods"])
		with mock.patch.object(ss_api, "_enabled", lambda: True):
			with self.assertRaises(frappe.ValidationError) as ctx:
				ss_api.push_shipment(out["sales_invoice"])
		self.assertIn("waiting for ship-to-store goods", str(ctx.exception))

	def test_hang_skips_a_serial_that_is_not_on_the_shelf_or_another_house(self):
		"""Review #4: only an in-stock Serial No of that item, for the house that
		ordered it, is ever held."""
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		self.assertEqual(manual_order.assign_procured_serials(
			si_name, {GUN: ["NO-SUCH-SERIAL"]})["assigned"], [])
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-P2-SN-H"))
		# the receive hung it; a fresh order of the same model from ANOTHER house
		# must not take a serial booked against this house's order
		other = self._counter([self._line(GUN)], ffl=DEST_FFL)["sales_invoice"]
		self.assertEqual(manual_order.assign_procured_serials(
			other, {GUN: ["TS-P2-SN-H"]}, distributor="SOME-OTHER-DC")["assigned"], [])

	def test_edit_payload_keeps_an_unarrived_line_to_store(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		items = manual_order.get_order_for_edit(out["sales_invoice"])["payload"]["items"]
		self.assertEqual(items[0]["source"], "to_store")
		self.assertEqual(items[0]["distributor"], DISTRIBUTOR)
		self.assertEqual(items[0]["sku"], GUN, "the SKU the house was ordered under")

	def test_a_house_with_ship_to_store_switched_off_is_refused(self):
		"""The switch is the store's decision about a house, so the counter must not
		be able to ring it — even with a payload that names the line ship to store."""
		frappe.db.set_value("Distributor", DISTRIBUTOR, "stocking_enabled", 0,
			update_modified=False)
		self.addCleanup(frappe.db.set_value, "Distributor", DISTRIBUTOR,
			"stocking_enabled", 1, update_modified=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._counter([self._line(GUN)], ffl=DEST_FFL)
		self.assertIn("ship to store is switched off", str(ctx.exception))

	def test_an_amend_rebuilds_the_procure_order(self):
		"""Cancel withdraws the Draft procure order and the amended invoice submits
		without create_order — the on_submit guard must rebuild it, or the customer
		has paid for goods nobody orders."""
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		for code, kw in ((ACC, {"ship_to": ship_to}), (GUN, {"ffl": DEST_FFL})):
			with self.subTest(item=code):
				si = frappe.get_doc("Sales Invoice",
					self._counter([self._line(code, rate=40)], **kw)["sales_invoice"])
				si.cancel()
				amended = frappe.copy_doc(si)
				amended.amended_from = si.name
				amended.docstatus = 0
				amended.flags.ignore_permissions = True
				amended.insert()
				amended.submit()
				do = self._only_do(amended.name)
				self.assertEqual(do.order_type, "Procure To Store")
				self.assertEqual(do.status, "Draft")   # never auto-placed
				self.assertEqual([(l.item_code, l.fulfillment) for l in do.lines],
					[(code, "to_store")])
