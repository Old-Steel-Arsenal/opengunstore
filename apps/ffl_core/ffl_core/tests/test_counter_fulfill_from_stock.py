"""Fulfill from stock: a counter ship-to-store line whose
goods have not arrived is served from the shelf instead. The Distributor Order
stays placed (neither house has a cancel API); its line's ``substituted_qty``
retires the units, so the queue stops waiting and the parcel, when it lands,
is plain restock — the Receive Goods scan no longer offers it to this order."""
from __future__ import annotations

import frappe

from ffl_core.api import manual_order
from ffl_core.tests.test_counter_to_store import (ACC, DEST_FFL, DISTRIBUTOR, GUN, SUPPLIER,
	CounterToStoreFixtures)


class FulfillFromStock(CounterToStoreFixtures):

	def _shelf_gun(self, serial):
		"""A gun of the same model already in stock, unheld — booked through the
		plain receive path (a Stocking purchase), never via a counter order."""
		from ffl_core.api.receive_goods import create_receive
		from ffl_core.warehouses import default_stock_warehouse
		create_receive({
			"supplier": SUPPLIER, "source": "RSR", "acquisition_type": "Purchase",
			"warehouse": default_stock_warehouse(self.company),
			"items": [{"item_code": GUN, "qty": 1, "rate": 400, "serials": [serial],
				"manufacturer": "Glock", "model_name": GUN, "firearm_type": "Pistol",
				"caliber": "9mm"}],
		})
		return serial

	def test_gun_line_takes_the_shelf_serial_and_stops_waiting(self):
		shelf = self._shelf_gun("TS-FFS-SHELF")
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		row = si.items[0]
		res = manual_order.fulfill_from_stock(si.name, row.name, serial_no=shelf)
		self.assertEqual(res["serial_no"], shelf)
		self.assertEqual(res["substituted_qty"], 1)
		row = frappe.get_doc("Sales Invoice Item", row.name)
		self.assertEqual(row.serial_no, shelf)
		self.assertFalse(row.ffl_to_store_distributor)
		do = self._only_do(si.name)
		self.assertEqual(do.lines[0].substituted_qty, 1)
		self.assertIsNone(manual_order.ship_arrival_block(frappe.get_doc("Sales Invoice", si.name)))
		q = self._queue_row(si.name)
		self.assertEqual(q["legs"]["awaiting_arrival"], 0)
		self.assertEqual(q["legs"]["substitutable"], [])
		self.assertEqual([p["serial"] for p in q["pending_serials"]], [shelf])

	def test_holding_a_shelf_gun_tells_the_sales_channels(self):
		"""The serial is written onto an invoice submitted long ago, so no
		on_submit fires — the channels must still hear the gun is now promised,
		or it stays buyable online."""
		from unittest import mock
		shelf = self._shelf_gun("TS-FFS-SHELF3")
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		seen = []
		real_get_hooks, real_get_attr = frappe.get_hooks, frappe.get_attr
		probe = "test.ffl_counter_hold_changed.registrant"

		def _record(doc):
			seen.append((doc.name, [r.serial_no for r in doc.items if r.serial_no]))

		def _hooks(name=None, *a, **kw):
			return [probe] if name == "ffl_counter_hold_changed" else real_get_hooks(name, *a, **kw)

		def _attr(path, *a, **kw):
			return _record if path == probe else real_get_attr(path, *a, **kw)

		with mock.patch.object(manual_order.frappe, "get_hooks", _hooks), \
				mock.patch.object(manual_order.frappe, "get_attr", _attr):
			manual_order.fulfill_from_stock(si.name, si.items[0].name, serial_no=shelf)
		self.assertEqual(seen, [(si.name, [shelf])])

	def test_both_channels_listen_for_a_counter_hold_change(self):
		hooks = frappe.get_hooks("ffl_counter_hold_changed")
		self.assertIn("ffl_woo_sync.woocommerce.doc_events.on_invoice_change", hooks)
		self.assertIn("ffl_integrations.gunbroker.doc_events.on_invoice_change", hooks)

	def test_a_held_or_missing_serial_is_refused(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		with self.assertRaises(frappe.ValidationError):
			manual_order.fulfill_from_stock(si.name, si.items[0].name, serial_no="NOT-A-GUN")

	def test_the_queue_offers_the_waiting_line(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		q = self._queue_row(out["sales_invoice"])
		subs = q["legs"]["substitutable"]
		self.assertEqual(len(subs), 1)
		self.assertEqual(subs[0]["item_code"], GUN)
		self.assertTrue(subs[0]["is_firearm"])
		self.assertEqual(subs[0]["do_name"], self._only_do(out["sales_invoice"]).name)

	def test_the_queue_counts_what_the_shelf_could_serve(self):
		"""``available`` is what the queue's Fulfill from local stock button is gated
		on — the unheld in-stock count of the gun's twins. Asserted RELATIVE to
		what the shelf already holds: receives commit (docs/testing.md in_test
		semantics), so an earlier test in the same run can legitimately leave an
		unheld twin on the shelf, and "zero while empty" only held for whichever
		test ran first (CI 2026-09-16: 1 != 0 on a fresh site)."""
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		before = self._queue_row(out["sales_invoice"])["legs"]["substitutable"][0]["available"]
		self._shelf_gun("TS-FFS-COUNT")
		self.assertEqual(self._queue_row(out["sales_invoice"])["legs"]["substitutable"][0]
			["available"], before + 1)
		# A held twin is not available: hang the new one on another order.
		out2 = self._counter([self._line(GUN)], ffl=DEST_FFL)
		manual_order.fulfill_from_stock(out2["sales_invoice"],
			frappe.get_doc("Sales Invoice", out2["sales_invoice"]).items[0].name,
			serial_no="TS-FFS-COUNT")
		self.assertEqual(self._queue_row(out["sales_invoice"])["legs"]["substitutable"][0]
			["available"], before)

	def test_two_lines_of_one_item_share_the_shelf_instead_of_each_claiming_it(self):
		"""Both rows reading the full shelf count would offer Fulfill from local
		stock twice over three units: the second click is refused by the server.
		Units are allotted in row order, so the second row reads what is left."""
		from ffl_core.api.receive_goods import create_receive
		from ffl_core.warehouses import default_stock_warehouse
		create_receive({
			"supplier": SUPPLIER, "source": "RSR", "acquisition_type": "Purchase",
			"warehouse": default_stock_warehouse(self.company),
			"items": [{"item_code": ACC, "qty": 3, "rate": 20, "serials": []}],
		})
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=2, rate=40),
			self._line(ACC, qty=2, rate=40)], ship_to=ship_to)
		subs = self._queue_row(out["sales_invoice"])["legs"]["substitutable"]
		self.assertEqual(len(subs), 2)
		self.assertGreaterEqual(subs[0]["available"], 3)
		self.assertEqual(subs[1]["available"], subs[0]["available"] - 2)

	def test_only_the_queue_pays_for_the_shelf_count(self):
		"""``available`` is attached by list_pending_dispositions, not by
		substitutable_rows: counting one gun's unheld serials walks every invoice
		that ever carried it, and fulfill_from_stock / resource_line, which also
		call substitutable_rows, never read the count."""
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertNotIn("available", manual_order.substitutable_rows(si)[0])

	def test_accessory_line_is_substituted_by_quantity(self):
		from ffl_core.api.receive_goods import create_receive
		from ffl_core.warehouses import default_stock_warehouse
		create_receive({
			"supplier": SUPPLIER, "source": "RSR", "acquisition_type": "Purchase",
			"warehouse": default_stock_warehouse(self.company),
			"items": [{"item_code": ACC, "qty": 5, "rate": 20, "serials": []}],
		})
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		out = self._counter([self._line(ACC, qty=2, rate=40)], ship_to=ship_to)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		subs = self._queue_row(si.name)["legs"]["substitutable"]
		self.assertEqual(subs[0]["available"], 5)
		res = manual_order.fulfill_from_stock(si.name, si.items[0].name)
		self.assertEqual(res["serial_no"], "")
		self.assertEqual(self._only_do(si.name).lines[0].substituted_qty, 2)
		self.assertIsNone(manual_order.ship_arrival_block(frappe.get_doc("Sales Invoice", si.name)))

	def test_a_substituted_leg_stops_holding_the_order_once_the_house_completes(self):
		"""The parcel of a fully substituted line books as plain restock, never
		against the DO, so its receive_status never reads Received — the queue must
		judge the leg by what it still owes, or the shipped order never leaves."""
		from ffl_core.api.receive_goods import create_receive
		from ffl_core.warehouses import default_stock_warehouse
		create_receive({
			"supplier": SUPPLIER, "source": "RSR", "acquisition_type": "Purchase",
			"warehouse": default_stock_warehouse(self.company),
			"items": [{"item_code": ACC, "qty": 2, "rate": 20, "serials": []}],
		})
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		si_name = self._counter([self._line(ACC, qty=2, rate=40)], ship_to=ship_to)["sales_invoice"]
		si = frappe.get_doc("Sales Invoice", si_name)
		manual_order.fulfill_from_stock(si_name, si.items[0].name)
		self._ship_to_store(self._only_do(si_name))   # the house ships it anyway: Complete
		manual_order.mark_shipped_manually(si_name)
		self.assertIsNone(self._queue_row(si_name),
			"a Complete leg that owes nothing must not keep the order on the queue")

	def test_a_received_line_cannot_be_substituted(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si_name = out["sales_invoice"]
		self._receive(self._ship_to_store(self._only_do(si_name), serial="TS-FFS-ARR"))
		si = frappe.get_doc("Sales Invoice", si_name)
		with self.assertRaises(frappe.ValidationError):
			manual_order.fulfill_from_stock(si.name, si.items[0].name, serial_no="TS-FFS-ARR")

	def test_the_parcel_is_no_longer_a_candidate_once_substituted(self):
		from ffl_integrations.distributor import receive_bridge as rb
		shelf = self._shelf_gun("TS-FFS-SHELF2")
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		do = self._only_do(si.name)
		frappe.db.set_value("Distributor Order", do.name, "status", "Placed", update_modified=False)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "ack_qty", 1,
			update_modified=False)
		self.assertIn(do.name, [c["do_name"] for c in rb.match_incoming(GUN)["candidates"]])
		manual_order.fulfill_from_stock(si.name, si.items[0].name, serial_no=shelf)
		self.assertNotIn(do.name, [c["do_name"] for c in rb.match_incoming(GUN)["candidates"]])


class TroubleAndResource(CounterToStoreFixtures):
	"""A killed / short / stale-hold leg shows as trouble; Re-source
	orders the line again elsewhere and retires the old line; an order every
	line of which is dead or covered no longer blocks Edit."""

	def test_killed_line_shows_as_trouble(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		do = self._only_do(out["sales_invoice"])
		frappe.db.set_value("Distributor Order", do.name, {"status": "Placed", "web_ref": "77"},
			update_modified=False)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "line_status", "Killed",
			update_modified=False)
		q = self._queue_row(out["sales_invoice"])
		t = q["legs"]["trouble"]
		self.assertEqual(t[0]["kind"], "killed")
		self.assertEqual(t[0]["do_name"], do.name)
		self.assertEqual(t[0]["web_ref"], "77")

	def test_stale_hold_shows_as_trouble_after_the_house_limit(self):
		from frappe.utils import add_to_date, now_datetime
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		do = self._only_do(out["sales_invoice"])
		frappe.db.set_value("Distributor Order", do.name, {
			"status": "Hold", "hold_status": "Prepayment hold",
			"placed_at": add_to_date(now_datetime(), days=-10)}, update_modified=False)
		q = self._queue_row(out["sales_invoice"])
		self.assertEqual([t["kind"] for t in q["legs"]["trouble"]], ["hold"])
		self.assertIn("Prepayment hold", q["legs"]["trouble"][0]["detail"])

	def test_resource_makes_a_new_draft_order_and_retires_the_old_line(self):
		from ffl_core.tests.test_counter_to_store import _ensure
		_ensure("Distributor", "TS-P2-DC2", {
			"code": "TS-P2-DC2", "distributor_name": "P2 Second House",
			"provider": "rsr", "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
			"supplier": SUPPLIER, "safety_buffer": 0, "display_cap": 0})
		frappe.db.set_value("Distributor", "TS-P2-DC2", {"enabled": 1, "stocking_enabled": 1},
			update_modified=False)
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		old = self._only_do(si.name)
		frappe.db.set_value("Distributor Order", old.name, "status", "Hold", update_modified=False)
		res = manual_order.resource_line(si.name, si.items[0].name, "TS-P2-DC2")
		self.assertNotEqual(res["do_name"], old.name)
		self.assertEqual(res["retired"], old.name)
		new = frappe.get_doc("Distributor Order", res["do_name"])
		self.assertEqual(new.status, "Draft")
		self.assertEqual(new.distributor, "TS-P2-DC2")
		self.assertEqual(new.order_type, "Procure To Store")
		self.assertEqual((new.source_doctype, new.source_name), ("Sales Invoice", si.name))
		self.assertEqual(frappe.db.get_value("Distributor Order Line", old.lines[0].name,
			"substituted_qty"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice Item", si.items[0].name,
			"ffl_to_store_distributor"), "TS-P2-DC2")
		self.assertIn(new.name, manual_order._unreceived_to_store_orders(si.name))
		self.assertNotIn(old.name, manual_order._unreceived_to_store_orders(si.name))

	def test_resource_of_a_short_shipped_line_orders_only_what_is_still_owed(self):
		"""Two of three units already arrived from the old house: the new house is
		asked for the one still missing, and the old line is retired by one — not
		the full line quantity bought again."""
		from ffl_core.tests.test_counter_to_store import _ensure
		_ensure("Distributor", "TS-P2-DC2", {
			"code": "TS-P2-DC2", "distributor_name": "P2 Second House",
			"provider": "rsr", "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
			"supplier": SUPPLIER, "safety_buffer": 0, "display_cap": 0})
		frappe.db.set_value("Distributor", "TS-P2-DC2", {"enabled": 1, "stocking_enabled": 1},
			update_modified=False)
		ship_to = {"address_line1": "5 Elm St", "city": "Springfield", "state": "TX", "pincode": "75024"}
		si_name = self._counter([self._line(ACC, qty=3, rate=40)], ship_to=ship_to)["sales_invoice"]
		si = frappe.get_doc("Sales Invoice", si_name)
		old = self._only_do(si_name)
		frappe.db.set_value("Distributor Order Line", old.lines[0].name,
			{"received_qty": 2, "invoiced_qty": 2, "line_status": "Shipped"},
			update_modified=False)
		res = manual_order.resource_line(si_name, si.items[0].name, "TS-P2-DC2")
		new = frappe.get_doc("Distributor Order", res["do_name"])
		self.assertEqual(sum(int(l.wish_qty or 0) for l in new.lines), 1)
		self.assertEqual(frappe.db.get_value("Distributor Order Line", old.lines[0].name,
			"substituted_qty"), 1)

	def test_resourcing_to_the_same_house_is_refused(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		with self.assertRaises(frappe.ValidationError):
			manual_order.resource_line(si.name, si.items[0].name, DISTRIBUTOR)

	def test_an_order_whose_lines_are_all_retired_no_longer_blocks_edit(self):
		out = self._counter([self._line(GUN)], ffl=DEST_FFL)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		old = self._only_do(si.name)
		frappe.db.set_value("Distributor Order", old.name, "status", "Placed", update_modified=False)
		self.assertEqual(manual_order._committed_distributor_orders(si.name), [old.name])
		frappe.db.set_value("Distributor Order Line", old.lines[0].name, "line_status", "Killed",
			update_modified=False)
		self.assertEqual(manual_order._committed_distributor_orders(si.name), [])
