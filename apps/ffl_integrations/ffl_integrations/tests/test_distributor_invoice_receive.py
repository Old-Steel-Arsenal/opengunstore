"""Outside-POS distributor invoices: where the goods went (destination), the Receive
Goods "Incoming shipments" listing, and receiving a store-bound parcel from its
invoice (``receive_bridge.stage_invoice`` / ``receive_invoice_lines``).

Destination rules are pinned against the live shapes probed 2026-09-29: SS
``orders.asmx GetShipTo`` returns the shop's address for a to-store order and an empty
``<NewDataSet />`` for a drop-ship one. The receive tests run on a real site with
FastBound disabled (push no-ops), like ``TestReceiveFromDO``."""
from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.cache_manager import clear_defaults_cache
from frappe.utils import add_days, nowdate

from ffl_integrations.distributor import invoices, lanes
from ffl_integrations.distributor import receive_bridge as rb
from ffl_integrations.distributor.base import DistributorProvider
from ffl_integrations.distributor.rsr_provider import normalize_shipment
from ffl_integrations.distributor.ss_provider import (
	SSProvider, normalize_invoice, ship_to_verdict,
)
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError
from ffl_integrations.sports_south.api_client import SSOrdersClient
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests.test_distributor_integration import _Base
from ffl_integrations.tests.test_distributor_invoices import RSR_SHIPMENT, _FakeProvider, _inv

STORE = {"name": "_Test Store A", "line1": "100 Main Street", "line2": "STE 117",
	"city": "Springfield", "state": "TX", "zip": "70001"}
# GetShipTo, live-verified 2026-09-29: a to-store order (padded values) / a drop-ship one.
SHIP_TO_STORE_XML = ("<NewDataSet><Table><WEBNAM>_Test Store A</WEBNAM><WEBATN>Receiving</WEBATN>"
	"<WEBAD1>100 Main Street </WEBAD1><WEBAD2>STE 117 </WEBAD2><WEBCTY>Springfield </WEBCTY>"
	"<WEBSTA>TX</WEBSTA><WEBZIP>70001</WEBZIP></Table></NewDataSet>")


class TestGetShipTo(frappe.tests.utils.FrappeTestCase):
	def _client(self, xml):
		client = mock.Mock()
		client._post.return_value = ("string", xml)
		return SSOrdersClient.get_ship_to(client, "12428127"), client

	def test_parses_the_shop_address_and_sends_a_blank_customer_order_number(self):
		rec, client = self._client(SHIP_TO_STORE_XML)
		self.assertEqual((rec["WEBAD1"], rec["WEBZIP"]), ("100 Main Street", "70001"))
		# Omitting CustomerOrderNumber is HTTP 500 on the live service.
		self.assertEqual(client._post.call_args.args[2],
			{"OrderNumber": "12428127", "CustomerOrderNumber": ""})

	def test_empty_dataset_is_an_empty_record(self):
		self.assertEqual(self._client("<NewDataSet />")[0], {})


class TestShipToVerdict(frappe.tests.utils.FrappeTestCase):
	def test_zip_and_street_line_one_must_both_match(self):
		verdict = lambda rec, store=STORE: ship_to_verdict(rec, store)  # noqa: E731
		same = {"WEBAD1": "100 Main Street ", "WEBZIP": "70001"}
		self.assertEqual(verdict(same), lanes.DEST_STORE)
		# Another spelling of the same street, ZIP+4.
		self.assertEqual(verdict({"WEBAD1": "100 MAIN ST.", "WEBZIP": "70001-1234"}),
			lanes.DEST_STORE)
		# A clearly different place: another ZIP, or another state.
		self.assertEqual(verdict({"WEBAD1": "100 Main Street", "WEBZIP": "90210"}),
			lanes.DEST_DROPSHIP)
		self.assertEqual(verdict({"WEBAD1": "100 Main Street", "WEBSTA": "CA"}), lanes.DEST_DROPSHIP)
		# The SAME ZIP but a street we cannot match (a suite suffix, a different street)
		# may well be the shop: Unknown, never Drop-ship and never Store.
		self.assertEqual(verdict({"WEBAD1": "100 Main St #117", "WEBZIP": "70001"}),
			lanes.DEST_UNKNOWN)
		self.assertEqual(verdict({"WEBAD1": "12 Elm St", "WEBZIP": "70001"}), lanes.DEST_UNKNOWN)
		# Nothing usable: Unknown, on either side.
		self.assertEqual(verdict({}), lanes.DEST_UNKNOWN)
		self.assertEqual(verdict(same, {"line1": "", "zip": ""}), lanes.DEST_UNKNOWN)


class TestSSInvoiceDestination(frappe.tests.utils.FrappeTestCase):
	def _dest(self, order_ref, answers):
		client = mock.Mock()
		client.get_ship_to.side_effect = lambda ref: (
			answers[ref] if not isinstance(answers[ref], Exception) else (_ for _ in ()).throw(answers[ref]))
		provider = SSProvider(client=client)
		with mock.patch.object(provider, "_store_address", return_value=STORE), \
				mock.patch("frappe.log_error"):
			return provider.invoice_destination({"invoice_number": "I1", "order_ref": order_ref})[0]

	def _note(self, order_ref, answers):
		client = mock.Mock()
		client.get_ship_to.side_effect = lambda ref: (
			answers[ref] if not isinstance(answers[ref], Exception) else (_ for _ in ()).throw(answers[ref]))
		provider = SSProvider(client=client)
		with mock.patch.object(provider, "_store_address", return_value=STORE), \
				mock.patch("frappe.log_error"):
			return provider.invoice_destination({"invoice_number": "I1", "order_ref": order_ref})

	def test_rules(self):
		store = {"WEBAD1": "100 Main Street ", "WEBZIP": "70001"}
		elsewhere = {"WEBAD1": "1 Main St", "WEBZIP": "10001"}
		self.assertEqual(self._dest("1", {"1": store}), lanes.DEST_STORE)
		self.assertEqual(self._dest("1", {"1": elsewhere}), lanes.DEST_DROPSHIP)
		# SS answers nothing for a consumer drop-ship (live-verified) → Unknown.
		self.assertEqual(self._dest("1", {"1": {}}), lanes.DEST_UNKNOWN)
		# Several orders on one invoice: Store only when ALL are; mixed = Unknown.
		self.assertEqual(self._dest("1,2", {"1": store, "2": store}), lanes.DEST_STORE)
		self.assertEqual(self._dest("1,2", {"1": store, "2": elsewhere}), lanes.DEST_UNKNOWN)
		self.assertEqual(self._dest("1,2", {"1": elsewhere, "2": elsewhere}), lanes.DEST_DROPSHIP)
		# A failed call is never Store.
		self.assertEqual(self._dest("1,2", {"1": store, "2": RuntimeError("500")}), lanes.DEST_UNKNOWN)
		self.assertEqual(self._dest("", {}), lanes.DEST_UNKNOWN)

	def test_only_a_failed_call_is_worth_asking_again(self):
		store = {"WEBAD1": "100 Main Street ", "WEBZIP": "70001"}
		# A confirmed-empty ship-to (a drop-ship), no order number, a mixed invoice: settled.
		for ref, answers in (("1", {"1": {}}), ("", {}), ("1,2", {"1": store, "2": {"WEBZIP": "10001"}})):
			self.assertEqual(self._note(ref, answers), (lanes.DEST_UNKNOWN, lanes.NOTE_NO_ANSWER), ref)
		self.assertEqual(self._note("1", {"1": RuntimeError("500")}),
			(lanes.DEST_UNKNOWN, lanes.NOTE_FAILED))
		self.assertEqual(self._note("1", {"1": store}), (lanes.DEST_STORE, ""))

	def test_the_override_is_compared_the_way_the_company_address_is_written(self):
		settings = frappe._dict(store_ship_line1="100 Main Street ", store_ship_zip=" 70001",
			store_ship_state="Texas", store_ship_phone="", store_ship_name="", store_ship_attn="",
			store_ship_line2="", store_ship_city="")
		provider = SSProvider(client=mock.Mock())
		with mock.patch("frappe.get_cached_doc", return_value=settings):
			store = provider._store_address()
		self.assertEqual((store["state"], store["zip"], store["line1"]), ("TX", "70001", "100 Main Street"))
		self.assertEqual(ship_to_verdict({"WEBAD1": "100 Main Street", "WEBZIP": "70001",
			"WEBSTA": "TX"}, store), lanes.DEST_STORE)
		# Another state written in full on the record is still another state.
		self.assertEqual(ship_to_verdict({"WEBAD1": "100 Main Street", "WEBSTA": "California"}, store),
			lanes.DEST_DROPSHIP)

	def test_a_partly_filled_override_falls_back_to_the_company_address(self):
		settings = frappe._dict(store_ship_line1="", store_ship_zip="", store_ship_state="",
			store_ship_phone="555-0100", store_ship_name="", store_ship_attn="", store_ship_line2="",
			store_ship_city="")
		provider = SSProvider(client=mock.Mock())
		company = {"name": "Co", "line1": "100 Main Street", "zip": "70001", "state": "TX"}
		with mock.patch("frappe.get_cached_doc", return_value=settings), \
				mock.patch("ffl_integrations.distributor.ss_provider.company_ship_to", return_value=company):
			store = provider._store_address()
		self.assertEqual((store["line1"], store["zip"], store["state"]), ("100 Main Street", "70001", "TX"))


class TestSSSerials(frappe.tests.utils.FrappeTestCase):
	def test_serials_are_handed_to_lines_by_item_number(self):
		header = {"S1INV": "9", "S1DATE": "2026-09-16", "INVGS": "10"}
		detail = [
			{"SINUM": "111", "IDESC": "GUN", "SQTY": "2", "SPRC": "300", "SGROS": "600", "ORDNO": "5"},
			{"SINUM": "222", "IDESC": "MAG", "SQTY": "1", "SPRC": "10", "SGROS": "10", "ORDNO": "5"}]
		serials = [{"ITEMNO": "111", "SERIAL": "A1 "}, {"ITEMNO": "111", "SERIAL": "A2"},
			{"ITEMNO": "999", "SERIAL": "X"}]
		inv = normalize_invoice(header, detail, [], serials)
		self.assertEqual([l["serials"] for l in inv["lines"]], ["A1,A2", ""])
		self.assertEqual(normalize_invoice(header, detail, [])["lines"][0]["serials"], "")
		# A credit line (negative quantity) takes none, and leaves the pool to the others.
		credit = [{"SINUM": "111", "IDESC": "RETURN", "SQTY": "-1", "SPRC": "300", "SGROS": "-300",
			"ORDNO": "5"}, *detail]
		got = normalize_invoice(header, credit, [], serials)["lines"]
		self.assertEqual([l["serials"] for l in got], ["", "A1,A2", ""])


class TestAccountDestination(frappe.tests.utils.FrappeTestCase):
	def test_default_follows_the_account(self):
		class P(DistributorProvider):
			code = "x"
		guess = lambda acct: P.invoice_destination(None, {"account": acct})  # noqa: E731
		self.assertEqual(guess("dealer"), (lanes.DEST_STORE, ""))
		self.assertEqual(guess("fulfillment"), (lanes.DEST_DROPSHIP, ""))
		self.assertEqual(guess(""), (lanes.DEST_UNKNOWN, lanes.NOTE_NO_ANSWER))  # settled: not re-asked
		self.assertEqual(lanes.destination_of_order_type("Stocking"), lanes.DEST_STORE)
		self.assertEqual(lanes.destination_of_order_type("Procure To Store"), lanes.DEST_STORE)
		self.assertEqual(lanes.destination_of_order_type("Dropship Firearm"), lanes.DEST_DROPSHIP)
		self.assertEqual(lanes.destination_of_order_type("Nope"), lanes.DEST_UNKNOWN)


class _InvoiceBase(_Base):
	def setUp(self):
		super().setUp()
		frappe.db.delete("Distributor Invoice", {"distributor": "TEST-DC"})
		frappe.db.delete("Distributor Order", {"distributor": "TEST-DC"})
		# The patch stamps the day receiving shipped on the site; tests start without it.
		self._was = frappe.db.get_default(invoices.RECEIVE_FROM_KEY)
		frappe.db.set_default(invoices.RECEIVE_FROM_KEY, "")
		self.addCleanup(self._restore_receive_from)
		p = mock.patch.object(FastBoundClient, "from_settings",
			side_effect=FastBoundConfigError("disabled in distributor tests"))
		p.start()
		self.addCleanup(p.stop)

	def _restore_receive_from(self):
		# The default is cached; put back what the site had, cache and all.
		frappe.db.set_default(invoices.RECEIVE_FROM_KEY, self._was or "")
		clear_defaults_cache("__default")

	@property
	def _warehouse(self):
		company = frappe.new_doc("Purchase Receipt").company \
			or frappe.db.get_single_value("Global Defaults", "default_company")
		from ffl_core.warehouses import default_stock_warehouse
		return default_stock_warehouse(company)

	def _invoice(self, no="I-1", lines=None, **fields):
		values = {"doctype": "Distributor Invoice", "distributor": "TEST-DC", "invoice_number": no,
			"invoice_date": nowdate(), "account": "dealer", "outside_pos": 1,
			"destination": "Store", "tracking_numbers": "TRK1", "total": 100,
			"lines": lines or [
				{"sku": "DC-ACC", "description": "ammo", "qty": 3, "unit_cost": 2.5, "amount": 7.5}],
			**fields}
		return frappe.get_doc(values).insert(ignore_permissions=True)


class TestDestinationAtSync(_InvoiceBase):
	def _sync(self, rows, provider=None):
		fake = provider or _FakeProvider(rows)
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()

	def _dest(self, no):
		return frappe.db.get_value("Distributor Invoice",
			{"distributor": "TEST-DC", "invoice_number": no}, ["destination", "received"])

	def test_rsr_account_decides_an_unmatched_invoice(self):
		self._sync([_inv("D1", account="dealer"), _inv("F1", account="fulfillment"), _inv("N1")])
		self.assertEqual(self._dest("D1"), ("Store", 0))
		self.assertEqual(self._dest("F1"), ("Drop-ship", 0))
		self.assertEqual(self._dest("N1"), ("Unknown", 0))

	def test_matched_invoice_follows_its_orders_lane_not_the_account(self):
		stocking = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}]).name
		drop = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}]).name
		frappe.db.set_value("Distributor Order", drop, "order_type", "Dropship Accessory",
			update_modified=False)
		self._sync([_inv("M1", account="fulfillment", po_number=stocking),
			_inv("M2", account="dealer", po_number=drop)])
		self.assertEqual(self._dest("M1")[0], "Store")
		self.assertEqual(self._dest("M2")[0], "Drop-ship")

	def test_rematch_replaces_the_destination_with_the_orders(self):
		self._sync([_inv("R1")])  # no account → Unknown, outside POS
		late = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}]).name
		frappe.db.set_value("Distributor Order Line", {"parent": late}, "invoices", "R1",
			update_modified=False)
		self._sync([])
		self.assertEqual(frappe.db.get_value("Distributor Invoice",
			{"distributor": "TEST-DC", "invoice_number": "R1"},
			["destination", "outside_pos", "distributor_order"]), ("Store", 0, late))

	def test_a_failing_provider_answer_is_unknown_never_store(self):
		fake = _FakeProvider([_inv("E1", account="dealer")])
		fake.invoice_destination = mock.Mock(side_effect=RuntimeError("boom"))
		with mock.patch("frappe.log_error"):
			self._sync([], provider=fake)
		self.assertEqual(self._dest("E1"), ("Unknown", 0))

	def test_an_invoice_dated_before_receiving_shipped_is_born_received(self):
		frappe.db.set_default(invoices.RECEIVE_FROM_KEY, nowdate())
		self._sync([_inv("OLD1", account="dealer", invoice_date=add_days(nowdate(), -2)),
			# On the day itself too: a same-day parcel re-mirrored after the upgrade.
			_inv("SAME1", account="dealer"),
			_inv("NEW1", account="dealer", invoice_date=add_days(nowdate(), 1))])
		self.assertEqual(self._dest("OLD1"), ("Store", 1))
		self.assertEqual(self._dest("SAME1"), ("Store", 1))
		self.assertEqual(self._dest("NEW1"), ("Store", 0))

	def _do(self, lines):
		return frappe.get_doc({"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer", "lines": lines}).insert(
			ignore_permissions=True)


class TestPatch(_InvoiceBase):
	def test_existing_invoices_become_received_with_a_best_effort_destination(self):
		from ffl_integrations.patches.mark_existing_distributor_invoices_received import execute

		matched_do = frappe.get_doc({"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Dropship Accessory", "account": "fulfillment",
			"lines": [{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}]}).insert(
			ignore_permissions=True).name
		a = self._invoice("P-DEALER", destination="", account="dealer")
		b = self._invoice("P-FULFIL", destination="", account="fulfillment")
		c = self._invoice("P-NONE", destination="", account="")
		d = self._invoice("P-DO", destination="", account="dealer", outside_pos=0,
			distributor_order=matched_do)
		listed = lambda: {o.get("invoice_name") for g in rb.list_incoming()["groups"]  # noqa: E731
			for o in g["orders"]}
		e = self._invoice("P-STORE")  # already a Store parcel: listed until the patch
		self.assertIn(e.name, listed())
		execute()
		self.assertFalse(listed() & {a.name, b.name, c.name, d.name, e.name})
		got = lambda inv: frappe.db.get_value("Distributor Invoice", inv.name,  # noqa: E731
			["destination", "received"])
		self.assertEqual([got(x) for x in (a, b, c, d)],
			[("Store", 1), ("Drop-ship", 1), ("Unknown", 1), ("Drop-ship", 1)])
		self.assertEqual(frappe.db.get_default(invoices.RECEIVE_FROM_KEY), nowdate())


class TestIncomingInvoices(_InvoiceBase):
	def _ids(self):
		return [o["invoice_name"] for g in rb.list_incoming()["groups"] for o in g["orders"]
			if o.get("invoice_name") and o["distributor"] == "TEST-DC"]

	def test_only_unreceived_store_invoices_with_tracking_and_no_order_are_listed(self):
		ok = self._invoice("L-OK")
		self._invoice("L-RECEIVED", received=1)
		self._invoice("L-DROP", destination="Drop-ship")
		self._invoice("L-UNKNOWN", destination="Unknown")
		self._invoice("L-NOTRACK", tracking_numbers="")
		self._invoice("L-POS", outside_pos=0)
		done = self._invoice("L-DONE", lines=[{"sku": "DC-ACC", "qty": 2, "unit_cost": 1,
			"amount": 2, "received_qty": 2}])
		old = self._invoice("L-OLD")
		frappe.db.set_value("Distributor Invoice", old.name, "creation",
			add_days(nowdate(), -(invoices.LOOKBACK_DAYS + 3)), update_modified=False)
		self.assertEqual(self._ids(), [ok.name])
		self.assertNotIn(done.name, self._ids())

	def test_invoice_sits_in_the_restock_group_shaped_like_an_order(self):
		inv = self._invoice("L-SHAPE", tracking_numbers="A1,B2")
		groups = rb.list_incoming()["groups"]
		g = next(g for g in groups if any(o.get("invoice_name") == inv.name for o in g["orders"]))
		self.assertEqual((g["label"], g["source_name"]), ("Restock", ""))
		o = next(o for o in g["orders"] if o.get("invoice_name") == inv.name)
		self.assertEqual((o["do_name"], o["outside_pos"], o["distributor"], o["supplier"],
			o["tracking_numbers"], o["expected"], o["received"], o["outstanding"]),
			("", 1, "TEST-DC", "DC-TEST-SUPPLIER", ["A1", "B2"], 3, 0, 3))

	def test_order_entries_are_untouched_by_the_invoice_keys(self):
		do = self._do_with_units()
		orders = [o for g in rb.list_incoming()["groups"] for o in g["orders"]
			if o["do_name"] == do.name]
		self.assertEqual(len(orders), 1)
		self.assertNotIn("invoice_name", orders[0])

	def _do_with_units(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name,
			{"invoiced_qty": 2, "tracking_numbers": "T9"}, update_modified=False)
		return do


class TestReceiveInvoice(_InvoiceBase):
	def _gun_invoice(self, no="G-1", serials="SN-INV-1"):
		return self._invoice(no, lines=[
			{"sku": "DC-GUN", "description": "UPC 000", "qty": 1, "unit_cost": 400, "amount": 400,
				"serials": serials}])

	def _receive(self, inv, items):
		return rb.receive_invoice_lines(inv.name, json.dumps(items), self._warehouse)

	def test_accessory_receive_books_a_receipt_and_completes_in_two_batches(self):
		inv = self._invoice("A-1")
		line = inv.lines[0].name
		res = self._receive(inv, [{"line_name": line, "qty": 2}])
		self.assertTrue(res["ok"])
		self.assertFalse(res["received"])
		pr = frappe.get_doc("Purchase Receipt", res["name"])
		self.assertEqual((pr.supplier, pr.items[0].item_code, pr.items[0].qty, float(pr.items[0].rate)),
			("DC-TEST-SUPPLIER", "DC-ACC", 2, 2.5))
		self.assertIn("A-1", pr.remarks or "")
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", line, "received_qty"), 2)
		self.assertEqual(frappe.db.get_value("Distributor Invoice", inv.name, "received"), 0)
		# Still listed, 1 left.
		listed = [o for g in rb.list_incoming()["groups"] for o in g["orders"]
			if o.get("invoice_name") == inv.name]
		self.assertEqual(listed[0]["outstanding"], 1)
		# More than is left is refused, and books nothing.
		with self.assertRaises(frappe.ValidationError):
			self._receive(inv, [{"line_name": line, "qty": 2}])
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", line, "received_qty"), 2)
		res2 = self._receive(inv, [{"line_name": line, "qty": 1}])
		self.assertTrue(res2["received"])
		row = frappe.db.get_value("Distributor Invoice", inv.name, ["received", "received_receipts"])
		self.assertEqual(row[0], 1)
		self.assertIn(res["name"], row[1])
		self.assertIn(res2["name"], row[1])
		self.assertNotIn(inv.name, [o.get("invoice_name") for g in rb.list_incoming()["groups"]
			for o in g["orders"]])

	def test_a_received_invoice_cannot_be_received_again(self):
		inv = self._invoice("A-2", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2}])
		line = inv.lines[0].name
		self._receive(inv, [{"line_name": line, "qty": 1}])
		before = frappe.db.count("Purchase Receipt")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._receive(inv, [{"line_name": line, "qty": 1}])
		self.assertIn("already", str(ctx.exception))
		self.assertEqual(frappe.db.count("Purchase Receipt"), before)

	def test_the_same_line_twice_in_one_call_cannot_exceed_the_invoice(self):
		inv = self._invoice("A-3", lines=[{"sku": "DC-ACC", "qty": 2, "unit_cost": 2, "amount": 4}])
		line = inv.lines[0].name
		with self.assertRaises(frappe.ValidationError):
			self._receive(inv, [{"line_name": line, "qty": 2}, {"line_name": line, "qty": 1}])
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", line, "received_qty"), 0)

	def test_firearm_needs_one_serial_per_unit_then_enters_the_book(self):
		inv = self._gun_invoice()
		line = inv.lines[0].name
		for bad in ([], ["A", "B"]):
			with self.assertRaises(frappe.ValidationError):
				self._receive(inv, [{"line_name": line, "qty": 1, "serials": bad}])
		self.assertFalse(frappe.db.exists("Serial No", "SN-INV-1"))
		res = self._receive(inv, [{"line_name": line, "qty": 1, "serials": ["SN-INV-1"]}])
		self.assertTrue(res["received"])
		self.assertEqual(float(frappe.db.get_value("Serial No", "SN-INV-1", "purchase_rate")), 400.0)
		acq = frappe.get_all("FFL Acquisition", filters={"purchase_receipt": res["name"], "docstatus": 1},
			fields=["serial", "distributor_order"])
		self.assertEqual([a.serial for a in acq], ["SN-INV-1"])
		self.assertFalse(acq[0].distributor_order)  # no order behind it

	def test_stage_lays_out_lines_serials_and_cost_and_remembers_the_item(self):
		inv = self._invoice("S-1", lines=[
			{"sku": "DC-GUN", "qty": 2, "unit_cost": 400, "amount": 800, "serials": "S1,S2"},
			{"sku": "DC-ACC", "qty": 5, "unit_cost": 2, "amount": 10}])
		got = rb.stage_invoice(inv.name)
		by_sku = {l["sku"]: l for l in got["lines"]}
		self.assertEqual((by_sku["DC-GUN"]["item_code"], by_sku["DC-GUN"]["is_firearm"],
			by_sku["DC-GUN"]["outstanding"], by_sku["DC-GUN"]["serials"], by_sku["DC-GUN"]["unit_cost"]),
			("DC-GUN", True, 2, ["S1", "S2"], 400.0))
		self.assertEqual((by_sku["DC-ACC"]["item_code"], by_sku["DC-ACC"]["is_firearm"],
			by_sku["DC-ACC"]["serials"]), ("DC-ACC", False, []))
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", by_sku["DC-ACC"]["line_name"],
			"item_code"), "DC-ACC")
		# One gun booked → the hint serials move on to the next unit.
		self._receive(inv, [{"line_name": by_sku["DC-GUN"]["line_name"], "qty": 1, "serials": ["S1"]}])
		again = {l["sku"]: l for l in rb.stage_invoice(inv.name)["lines"]}
		self.assertEqual((again["DC-GUN"]["outstanding"], again["DC-GUN"]["serials"]), (1, ["S2"]))

	def test_a_line_the_catalog_cannot_resolve_is_reported_not_guessed(self):
		inv = self._invoice("S-2", lines=[{"sku": "NOT-IN-CATALOG", "qty": 1, "unit_cost": 1, "amount": 1},
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 1, "amount": 1}])
		got = rb.stage_invoice(inv.name)
		self.assertEqual([u["sku"] for u in got["unmatched"]], ["NOT-IN-CATALOG"])
		self.assertTrue(got["unmatched"][0]["reason"])
		self.assertEqual([l["sku"] for l in got["lines"]], ["DC-ACC"])
		with self.assertRaises(frappe.ValidationError):
			self._receive(inv, [{"line_name": inv.lines[0].name, "qty": 1}])

	def test_a_new_sku_is_promoted_to_an_item_without_stamping_dropship(self):
		catalog_stub.seed_catalog([{"rsr_stock_number": "DC-FRESH", "upc": "036000291452",
			"ffl_required": 0, "department_number": "13", "dealer_price": 3, "map_price": 4,
			"msrp": 5, "quantity": 9, "product_description": "Fresh thing"}])
		inv = self._invoice("S-3", lines=[{"sku": "DC-FRESH", "qty": 1, "unit_cost": 3, "amount": 3}])
		got = rb.stage_invoice(inv.name)
		code = got["lines"][0]["item_code"]
		self.assertEqual(code, "036000291452")
		self.assertTrue(frappe.db.exists("Item", code))
		self.assertFalse(frappe.db.get_value("Item", code, "dropship_distributor"))
		# By the UPC on the line when the SKU is not the house's.
		inv2 = self._invoice("S-4", lines=[{"sku": "SOME-OTHER", "description": "anything at all",
			"upc": "036000291452", "qty": 1, "unit_cost": 3, "amount": 3}])
		self.assertEqual(rb.stage_invoice(inv2.name)["lines"][0]["item_code"], code)

	def test_only_a_receivable_store_parcel_can_be_staged_or_received(self):
		item = [{"qty": 1}]
		cases = {
			"drop": self._invoice("X-1", destination="Drop-ship"),
			"unknown": self._invoice("X-2", destination="Unknown"),
			"got": self._invoice("X-3", received=1),
			"pos": self._invoice("X-4", outside_pos=0),
		}
		for label, inv in cases.items():
			with self.subTest(label):
				with self.assertRaises(frappe.ValidationError):
					rb.stage_invoice(inv.name)
				with self.assertRaises(frappe.ValidationError):
					rb.receive_invoice_lines(inv.name, [{**item[0], "line_name": inv.lines[0].name}])
				self.assertEqual(frappe.db.get_value("Distributor Invoice Line", inv.lines[0].name,
					"received_qty"), 0)

	def test_an_order_that_claims_the_invoice_since_the_last_sync_blocks_it(self):
		inv = self._invoice("X-5")
		do = frappe.get_doc({"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer",
			"lines": [{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 3}]}).insert(
			ignore_permissions=True)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoices", "X-5",
			update_modified=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			rb.receive_invoice_lines(inv.name, [{"line_name": inv.lines[0].name, "qty": 1}])
		self.assertIn(do.name, str(ctx.exception))

	def test_a_receive_that_fails_books_nothing_on_the_invoice(self):
		inv = self._invoice("X-6", lines=[{"sku": "DC-ACC", "qty": 2, "unit_cost": 0, "amount": 0}])
		# Unit cost 0 and no "it was free" answer: create_receive refuses at the door.
		with self.assertRaises(frappe.ValidationError):
			self._receive(inv, [{"line_name": inv.lines[0].name, "qty": 1}])
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", inv.lines[0].name, "received_qty"), 0)
		self.assertEqual(frappe.db.get_value("Distributor Invoice", inv.name, "received"), 0)

	def test_a_free_line_no_item_matches_does_not_hold_the_invoice_open(self):
		inv = self._invoice("F-1", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2},
			{"sku": "NOT-IN-CATALOG", "qty": 3, "unit_cost": 0, "amount": 0}])
		res = self._receive(inv, [{"line_name": inv.lines[0].name, "qty": 1}])
		self.assertTrue(res["received"])
		self.assertEqual(frappe.db.get_value("Distributor Invoice", inv.name, "received"), 1)
		self.assertNotIn(inv.name, [o.get("invoice_name") for g in rb.list_incoming()["groups"]
			for o in g["orders"]])

	def test_a_free_line_that_matches_an_item_still_waits_to_be_received(self):
		inv = self._invoice("F-2", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2},
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 0, "amount": 0}])
		res = self._receive(inv, [{"line_name": inv.lines[0].name, "qty": 1}])
		self.assertFalse(res["received"])
		self.assertEqual(frappe.db.get_value("Distributor Invoice", inv.name, "received"), 0)

	def test_staging_closes_an_invoice_with_only_unmatched_free_lines_left(self):
		inv = self._invoice("F-3", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2,
			"received_qty": 1}, {"sku": "NOT-IN-CATALOG", "qty": 3, "unit_cost": 0, "amount": 0}])
		got = rb.stage_invoice(inv.name)
		self.assertEqual((got["lines"], got["received"]), ([], True))
		self.assertEqual(frappe.db.get_value("Distributor Invoice", inv.name, "received"), 1)
		# A paid line nobody can match is not a freebie: the invoice stays open.
		paid = self._invoice("F-4", lines=[{"sku": "NOT-IN-CATALOG", "qty": 1, "unit_cost": 5, "amount": 5}])
		self.assertFalse(rb.stage_invoice(paid.name)["received"])
		self.assertEqual(frappe.db.get_value("Distributor Invoice", paid.name, "received"), 0)

	def test_staff_only(self):
		inv = self._invoice("X-7")
		frappe.set_user("Guest")
		try:
			for call in (lambda: rb.stage_invoice(inv.name),
					lambda: rb.receive_invoice_lines(inv.name, [])):
				with self.assertRaises(frappe.PermissionError):
					call()
		finally:
			frappe.set_user("Administrator")

	def test_a_late_match_to_an_order_after_a_receive_alerts_the_office(self):
		inv = self._invoice("X-8")
		self._receive(inv, [{"line_name": inv.lines[0].name, "qty": 1}])
		late = frappe.get_doc({"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer",
			"lines": [{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 3}]}).insert(
			ignore_permissions=True).name
		frappe.db.set_value("Distributor Order Line", {"parent": late}, "invoices", "X-8",
			update_modified=False)
		with mock.patch("ffl_integrations.notifications.notify_admins") as alert, \
				mock.patch.object(invoices, "get_providers", return_value={"rsr": _FakeProvider([])}):
			invoices.sync_invoices()
		alert.assert_called_once()
		self.assertIn(late, alert.call_args.args[1])


class TestOrderClaimsTheParcel(_InvoiceBase):
	"""RSR invoices carry no PO, so a POS-placed store order's invoice stays "outside POS"
	until the status poll writes its number on the order line. Meanwhile it must not be
	offered as a second parcel."""

	def _open_do(self, status="Placed", **line):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2}], status=status)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name,
			{"invoiced_qty": 2, "tracking_numbers": "T9", **line}, update_modified=False)
		return do

	def _listed(self, inv):
		return inv.name in [o.get("invoice_name") for g in rb.list_incoming()["groups"]
			for o in g["orders"]]

	def test_an_invoice_sharing_an_item_with_an_open_order_is_not_a_second_parcel(self):
		inv = self._invoice("O-1")
		do = self._open_do()
		self.assertFalse(self._listed(inv))
		line = inv.lines[0].name
		for call in (lambda: rb.stage_invoice(inv.name),
				lambda: rb.receive_invoice_lines(inv.name, [{"line_name": line, "qty": 1}])):
			with self.assertRaises(frappe.ValidationError) as ctx:
				call()
			self.assertIn(do.name, str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line", line, "received_qty"), 0)
		# The Invoices page says so instead of "To receive".
		row = next(r for r in invoices.list_invoices(distributor="TEST-DC") if r["name"] == inv.name)
		self.assertEqual(row["likely_order"], do.name)

	def test_it_is_offered_once_the_order_is_received_or_cancelled(self):
		inv = self._invoice("O-2")
		do = self._open_do()
		self.assertFalse(self._listed(inv))
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "received_qty", 2,
			update_modified=False)
		self.assertTrue(self._listed(inv))
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "received_qty", 0,
			update_modified=False)
		self.assertFalse(self._listed(inv))
		frappe.db.set_value("Distributor Order", do.name, "status", "Cancelled", update_modified=False)
		self.assertTrue(self._listed(inv))
		self.assertEqual(rb.stage_invoice(inv.name)["lines"][0]["item_code"], "DC-ACC")
		row = next(r for r in invoices.list_invoices(distributor="TEST-DC") if r["name"] == inv.name)
		self.assertEqual(row["likely_order"], "")

	def test_another_house_or_another_item_does_not_claim_it(self):
		inv = self._invoice("O-3", lines=[{"sku": "DC-GUN", "qty": 1, "unit_cost": 5, "amount": 5}])
		self._open_do()  # DC-ACC only
		self.assertTrue(self._listed(inv))


class TestTrackingRefresh(_InvoiceBase):
	def _sync(self, fake):
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()

	def test_a_parcel_that_ships_after_its_invoice_gets_its_tracking(self):
		late = self._invoice("T-LATE", tracking_numbers="")
		drop = self._invoice("T-DROP", tracking_numbers="", destination="Drop-ship")
		got = self._invoice("T-GOT", tracking_numbers="KEEP")
		received = self._invoice("T-RCVD", tracking_numbers="", received=1)
		fake = _FakeProvider([])
		fake.invoice_tracking = mock.Mock(return_value=["Z1", "Z2"])
		self._sync(fake)
		val = lambda inv: frappe.db.get_value("Distributor Invoice", inv.name, "tracking_numbers")  # noqa: E731
		self.assertEqual((val(late), val(drop), val(got), val(received)), ("Z1,Z2", "", "KEEP", ""))
		self.assertEqual([c.args[0]["invoice_number"] for c in fake.invoice_tracking.call_args_list],
			["T-LATE"])
		# ...and it is now receivable.
		self.assertIn(late.name, [o.get("invoice_name") for g in rb.list_incoming()["groups"]
			for o in g["orders"]])

	def test_a_failing_house_or_invoice_is_skipped_and_an_answerless_one_stays_empty(self):
		a = self._invoice("T-A", tracking_numbers="")
		b = self._invoice("T-B", tracking_numbers="")
		fake = _FakeProvider([])
		fake.invoice_tracking = mock.Mock(side_effect=lambda inv: (
			(_ for _ in ()).throw(RuntimeError("boom")) if inv["invoice_number"] == "T-A" else []))
		with mock.patch("frappe.log_error") as log:
			self._sync(fake)
		self.assertEqual(log.call_count, 1)
		self.assertFalse(frappe.db.get_value("Distributor Invoice", a.name, "tracking_numbers"))
		self.assertFalse(frappe.db.get_value("Distributor Invoice", b.name, "tracking_numbers"))
		self.assertEqual(fake.invoice_tracking.call_count, 2)

	def test_an_invoice_inserted_this_run_is_not_asked_again(self):
		fake = _FakeProvider([_inv("T-NEW", account="dealer", tracking_numbers=[])])
		fake.invoice_tracking = mock.Mock(return_value=["Q"])
		self._sync(fake)
		fake.invoice_tracking.assert_not_called()

	def test_the_refresh_is_bounded(self):
		for i in range(3):
			self._invoice(f"T-B{i}", tracking_numbers="")
		fake = _FakeProvider([])
		fake.invoice_tracking = mock.Mock(return_value=[])
		with mock.patch.object(invoices, "TRACKING_REFRESH_LIMIT", 2):
			self._sync(fake)
		self.assertEqual(fake.invoice_tracking.call_count, 2)


class TestInvoiceReceiveReview(_InvoiceBase):
	def _gun(self, no, serials):
		return self._invoice(no, lines=[{"sku": "DC-GUN", "qty": 2, "unit_cost": 400, "amount": 800,
			"serials": serials}])

	def test_a_serial_already_booked_is_not_offered_again_whatever_the_order(self):
		inv = self._gun("V-1", "V1-S1,V1-S2")
		line = inv.lines[0].name
		# The receiver read S2 off the first gun out of the box.
		rb.receive_invoice_lines(inv.name, [{"line_name": line, "qty": 1, "serials": ["V1-S2"]}],
			self._warehouse)
		again = rb.stage_invoice(inv.name)["lines"][0]
		self.assertEqual((again["outstanding"], again["serials"]), (1, ["V1-S1"]))

	def test_the_order_and_invoice_paths_pass_the_receivers_answers_to_create_receive(self):
		it = {"sell_price": 12.5, "is_antique": 1, "year_made": 1890, "importer": "X"}
		got = rb._payload_item(it, "DC-GUN", rb._item_meta("DC-GUN"), 1, ["S"], 400)
		self.assertEqual((got["sell_price"], got["is_antique"], got["year_made"]), (12.5, 1, 1890))
		with mock.patch("ffl_core.api.receive_goods.create_receive",
				return_value={"ok": True, "name": "PR-X", "total": 0}) as cr:
			inv = self._invoice("V-2", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2}])
			rb.receive_invoice_lines(inv.name, [{"line_name": inv.lines[0].name, "qty": 1,
				"sell_price": 3, "is_antique": 0, "year_made": 0}])
		sent = cr.call_args.args[0]["items"][0]
		self.assertEqual((sent["sell_price"], sent["is_antique"], sent["year_made"]), (3, 0, 0))

	def test_an_item_the_catalog_lacks_falls_back_to_the_one_the_shop_has(self):
		# DC-ACC / DC-GUN exist as Items; the house catalog knows neither of these spellings.
		inv = self._invoice("V-3", lines=[
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 1, "amount": 1},
			{"sku": "NOT-A-HOUSE-SKU", "upc": "DC-GUN", "qty": 1, "unit_cost": 1, "amount": 1}])
		with mock.patch("ffl_integrations.distributor.offers.catalog_row", return_value=None):
			got = rb.stage_invoice(inv.name)
		self.assertEqual([(l["sku"], l["item_code"]) for l in got["lines"]],
			[("DC-ACC", "DC-ACC"), ("NOT-A-HOUSE-SKU", "DC-GUN")])
		self.assertEqual(got["unmatched"], [])

	def test_one_failing_line_is_reported_and_the_rest_still_stage_with_no_lock_held(self):
		inv = self._invoice("V-4", lines=[
			{"sku": "BOOM", "qty": 1, "unit_cost": 1, "amount": 1},
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 1, "amount": 1}])
		locked = mock.Mock(wraps=rb._locked_invoice)
		real = rb._resolve_line_item

		def resolve(provider, line):
			self.assertEqual(locked.call_count, 0)  # nothing is locked while the catalog is asked
			if line.sku == "BOOM":
				raise RuntimeError("catalog down")
			return real(provider, line)

		with mock.patch.object(rb, "_locked_invoice", locked), \
				mock.patch.object(rb, "_resolve_line_item", resolve), mock.patch("frappe.log_error"):
			got = rb.stage_invoice(inv.name)
		self.assertEqual(locked.call_count, 1)
		self.assertEqual([l["sku"] for l in got["lines"]], ["DC-ACC"])
		self.assertEqual(got["unmatched"][0]["sku"], "BOOM")
		self.assertIn("RuntimeError", got["unmatched"][0]["reason"])

	def test_receive_revalidates_under_the_lock_after_resolving(self):
		inv = self._invoice("V-5", lines=[{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2}])
		real = rb._resolve_lines

		def resolve_then_lose_the_race(*a, **kw):
			out = real(*a, **kw)
			frappe.db.set_value("Distributor Invoice", inv.name, "received", 1, update_modified=False)
			return out

		with mock.patch.object(rb, "_resolve_lines", resolve_then_lose_the_race):
			with self.assertRaises(frappe.ValidationError) as ctx:
				rb.receive_invoice_lines(inv.name, [{"line_name": inv.lines[0].name, "qty": 1}])
		self.assertIn("already", str(ctx.exception))

	def test_the_upc_is_its_own_field(self):
		line = normalize_shipment("4022784237", RSR_SHIPMENT, "dealer")["lines"][0]
		self.assertEqual(line["upc"], "815835015408")
		self.assertEqual(normalize_invoice({"S1INV": "1"}, [{"SINUM": "5", "SQTY": "1", "SGROS": "1",
			"IDESC": "X", "ORDNO": "3"}], [])["lines"][0].get("upc", ""), "")
		# The sync stores it.
		fake = _FakeProvider([_inv("U-1", account="dealer", lines=[
			{"sku": "S", "description": "words", "upc": "036000291452", "qty": 1, "unit_cost": 1, "amount": 1}])])
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()
		self.assertEqual(frappe.db.get_value("Distributor Invoice Line",
			{"parent": "TEST-DC-U-1"}, "upc"), "036000291452")

	def test_the_patch_fills_upc_from_the_old_descriptions(self):
		from ffl_integrations.patches.mark_existing_distributor_invoices_received import execute

		inv = self._invoice("U-2", lines=[
			{"sku": "A", "description": "UPC 815835015408", "qty": 1, "unit_cost": 1, "amount": 1},
			{"sku": "B", "description": "MAGPUL PMAG", "qty": 1, "unit_cost": 1, "amount": 1}])
		execute()
		self.assertEqual([l.upc for l in frappe.get_doc("Distributor Invoice", inv.name).lines],
			["815835015408", None])

	def test_order_with_invoice_matches_the_exact_number_of_the_same_house(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoices", "123456, 777",
			update_modified=False)
		self.assertEqual(invoices.order_with_invoice("TEST-DC", "777"), do.name)
		self.assertEqual(invoices.order_with_invoice("TEST-DC", "123456"), do.name)
		self.assertIsNone(invoices.order_with_invoice("TEST-DC", "12345"))  # inside 123456
		self.assertIsNone(invoices.order_with_invoice("TEST-DC", ""))
		self.assertIsNone(invoices.order_with_invoice("OTHER-HOUSE", "777"))


class TestDestinationRetry(_InvoiceBase):
	def _sync(self, fake):
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()

	def _row(self, inv):
		return frappe.db.get_value("Distributor Invoice", inv.name, ["destination", "destination_note"])

	def test_a_failed_lookup_is_asked_again_and_a_settled_one_is_not(self):
		failed = self._invoice("R-FAIL", destination="Unknown", destination_note="Lookup failed")
		settled = self._invoice("R-SETTLED", destination="Unknown", destination_note="No answer on file")
		fake = _FakeProvider([])
		fake.invoice_destination = mock.Mock(return_value=("Store", ""))
		self._sync(fake)
		self.assertEqual(self._row(failed), ("Store", ""))
		self.assertEqual(self._row(settled), ("Unknown", "No answer on file"))
		self.assertEqual([c.args[0]["invoice_number"] for c in fake.invoice_destination.call_args_list],
			["R-FAIL"])

	def test_it_stays_unknown_and_never_store_while_the_lookup_keeps_failing(self):
		inv = self._invoice("R-STILL", destination="Unknown", destination_note="Lookup failed")
		fake = _FakeProvider([])
		fake.invoice_destination = mock.Mock(side_effect=RuntimeError("500"))
		with mock.patch("frappe.log_error"):
			self._sync(fake)
		self.assertEqual(self._row(inv), ("Unknown", "Lookup failed"))
		with self.assertRaises(frappe.ValidationError):
			rb.stage_invoice(inv.name)

	def test_a_house_whose_list_failed_this_run_is_left_alone(self):
		inv = self._invoice("R-DOWN", destination="Unknown", destination_note="Lookup failed")
		fake = _FakeProvider([])
		fake.list_invoices = mock.Mock(side_effect=RuntimeError("house down"))
		fake.invoice_destination = mock.Mock(return_value=("Store", ""))
		fake.invoice_tracking = mock.Mock(return_value=["T"])
		with mock.patch("frappe.log_error"):
			self._sync(fake)
		fake.invoice_destination.assert_not_called()
		fake.invoice_tracking.assert_not_called()
		self.assertEqual(self._row(inv), ("Unknown", "Lookup failed"))

	def test_the_tracking_budget_goes_to_store_invoices_first_and_skips_confirmed_drop_ships(self):
		store = self._invoice("R-T-STORE", tracking_numbers="")
		maybe = self._invoice("R-T-MAYBE", tracking_numbers="", destination="Unknown",
			destination_note="Lookup failed")
		empty = self._invoice("R-T-EMPTY", tracking_numbers="", destination="Unknown",
			destination_note="No answer on file")
		fake = _FakeProvider([])
		fake.invoice_tracking = mock.Mock(return_value=[])
		fake.invoice_destination = mock.Mock(return_value=("Unknown", "Lookup failed"))
		with mock.patch.object(invoices, "TRACKING_REFRESH_LIMIT", 1):
			self._sync(fake)
		asked = [c.args[0]["invoice_number"] for c in fake.invoice_tracking.call_args_list]
		self.assertEqual(asked, ["R-T-STORE"])
		with mock.patch.object(invoices, "TRACKING_REFRESH_LIMIT", 5):
			fake.invoice_tracking.reset_mock()
			self._sync(fake)
		asked = [c.args[0]["invoice_number"] for c in fake.invoice_tracking.call_args_list]
		self.assertEqual(sorted(asked), ["R-T-MAYBE", "R-T-STORE"])  # never the confirmed-empty one
		self.assertNotIn(empty.name, asked)


class TestReviewR2(_InvoiceBase):
	def _legacy_gun(self, code, has_serial_no):
		# A firearm the flag missed: legacy Item Group only.
		from ffl_integrations.tests.test_distributor_integration import _ensure_item
		_ensure_item(code, item_group="Rifle", has_serial_no=has_serial_no, is_firearm=1,
			manufacturer="TestArms", model_name="L1", firearm_type="Rifle", caliber="5.56")
		frappe.db.set_value("Item", code, "is_firearm", 0)
		return code

	def test_the_firearm_test_is_the_one_create_receive_applies(self):
		code = self._legacy_gun("DC-LEGACY", 1)
		self.assertTrue(rb._item_meta(code)["is_firearm"])
		inv = self._invoice("W-1", lines=[{"sku": "DC-LEGACY", "item_code": code, "qty": 1,
			"unit_cost": 300, "amount": 300, "serials": "LEG-1"}])
		line = rb.stage_invoice(inv.name)["lines"][0]
		self.assertEqual((line["is_firearm"], line["has_serial_no"], line["serials"], line["manufacturer"],
			line["firearm_type"]), (True, True, ["LEG-1"], "TestArms", "Rifle"))
		with self.assertRaises(frappe.ValidationError):  # one serial per unit
			rb.receive_invoice_lines(inv.name, [{"line_name": inv.lines[0].name, "qty": 1}])
		res = rb.receive_invoice_lines(inv.name,
			[{"line_name": inv.lines[0].name, "qty": 1, "serials": ["LEG-1"]}], self._warehouse)
		self.assertTrue(res["received"])
		self.assertTrue(frappe.db.exists("Serial No", "LEG-1"))
		self.assertTrue(frappe.db.exists("FFL Acquisition", {"purchase_receipt": res["name"], "serial": "LEG-1"}))

	def test_a_firearm_without_serial_tracking_is_still_a_serial_per_unit(self):
		code = self._legacy_gun("DC-LEGACY2", 0)
		inv = self._invoice("W-2", lines=[{"sku": "DC-LEGACY2", "item_code": code, "qty": 1,
			"unit_cost": 300, "amount": 300, "serials": "LEG-2"}])
		line = rb.stage_invoice(inv.name)["lines"][0]
		self.assertEqual((line["is_firearm"], line["has_serial_no"], line["serials"]), (True, True, ["LEG-2"]))

	def test_the_dashboard_lists_a_legacy_group_gun_as_a_firearm_too(self):
		code = self._legacy_gun("DC-LEGACY3", 1)
		do = self._do([{"sku": "DC-LEGACY3", "item_code": code, "wish_qty": 1}], status="Placed")
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "invoiced_qty", 1,
			update_modified=False)
		lines = [ln for g in rb.list_incoming()["groups"] for o in g["orders"]
			if o["do_name"] == do.name for ln in o["lines"]]
		self.assertTrue(lines[0]["is_firearm"])

	def test_a_swallowed_throw_leaves_no_red_dialog_behind(self):
		inv = self._invoice("W-3", lines=[
			{"sku": "BOOM", "qty": 1, "unit_cost": 1, "amount": 1},
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 1, "amount": 1}])
		real = rb._resolve_line_item

		def resolve(provider, line):
			if line.sku == "BOOM":
				frappe.msgprint("earlier message, not ours")  # noqa: not swallowed below
				raise_in_promote()
			return real(provider, line)

		def raise_in_promote():
			frappe.throw("Item BOOM cannot be promoted")

		frappe.local.message_log = []
		with mock.patch.object(rb, "_resolve_line_item", resolve), mock.patch("frappe.log_error"):
			got = rb.stage_invoice(inv.name)
		texts = " ".join(str(m) for m in frappe.local.message_log)
		self.assertNotIn("cannot be promoted", texts)
		self.assertNotIn("earlier message", texts)  # added during that line's attempt
		self.assertEqual([u["sku"] for u in got["unmatched"]], ["BOOM"])
		self.assertEqual([l["sku"] for l in got["lines"]], ["DC-ACC"])
		# What was queued BEFORE the attempt is kept.
		frappe.local.message_log = [{"message": "keep me"}]
		with mock.patch.object(rb, "_resolve_line_item", resolve), mock.patch("frappe.log_error"):
			rb.stage_invoice(self._invoice("W-3B", lines=[{"sku": "BOOM", "qty": 1, "unit_cost": 1,
				"amount": 1}]).name)
		self.assertIn("keep me", " ".join(str(m) for m in frappe.local.message_log))

	def test_the_open_order_index_is_built_once_per_request(self):
		inv = self._invoice("W-4")
		with mock.patch.object(rb, "open_order_index", wraps=rb.open_order_index) as idx:
			rb.stage_invoice(inv.name)
		self.assertEqual(idx.call_count, 1)
		with mock.patch.object(rb, "open_order_index", wraps=rb.open_order_index) as idx:
			rb.receive_invoice_lines(inv.name, [{"line_name": inv.lines[0].name, "qty": 1}],
				self._warehouse)
		self.assertEqual(idx.call_count, 1)
		with mock.patch.object(rb, "open_order_index", wraps=rb.open_order_index) as idx:
			rb.list_incoming()
		self.assertEqual(idx.call_count, 1)


class TestLikelyOrderScope(_InvoiceBase):
	def _open_do(self, distributor="TEST-DC", sku="DC-ACC"):
		do = frappe.get_doc({"doctype": "Distributor Order", "distributor": distributor,
			"order_type": "Stocking", "account": "dealer",
			"lines": [{"sku": sku, "item_code": sku, "wish_qty": 2}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Distributor Order", do.name, "status", "Placed", update_modified=False)
		frappe.db.set_value("Distributor Order Line", do.lines[0].name,
			{"invoiced_qty": 2, "tracking_numbers": "T9"}, update_modified=False)
		return do

	def _listed(self, inv):
		return inv.name in [o.get("invoice_name") for g in rb.list_incoming()["groups"]
			for o in g["orders"]]

	def test_a_house_that_puts_our_po_on_its_invoices_is_never_blocked(self):
		if not frappe.db.exists("Distributor", "TEST-SS"):
			frappe.get_doc({"doctype": "Distributor", "code": "TEST-SS", "distributor_name": "Test SS",
				"provider": "ss", "enabled": 0}).insert(ignore_permissions=True)
		frappe.db.delete("Distributor Order", {"distributor": "TEST-SS"})
		frappe.db.delete("Distributor Invoice", {"distributor": "TEST-SS"})
		self._open_do("TEST-SS")
		ss_inv = self._invoice("S-1", distributor="TEST-SS")
		self.assertTrue(self._listed(ss_inv))  # unmatched at sync = really outside the POS
		# The same shape on the RSR-style house (no PO on its invoices) IS blocked.
		self._open_do("TEST-DC")
		rsr_inv = self._invoice("S-2")
		self.assertFalse(self._listed(rsr_inv))
		self.assertTrue(SSProvider.invoices_carry_po)
		self.assertFalse(DistributorProvider.invoices_carry_po)

	def test_credit_and_fully_received_lines_claim_nothing(self):
		self._open_do("TEST-DC", "DC-ACC")
		inv = self._invoice("C-1", lines=[
			{"sku": "DC-ACC", "qty": -1, "unit_cost": 2, "amount": -2},  # a credit line
			{"sku": "DC-ACC", "qty": 1, "unit_cost": 2, "amount": 2, "received_qty": 1},
			{"sku": "DC-GUN", "qty": 1, "unit_cost": 5, "amount": 5}])
		self.assertTrue(self._listed(inv))
		row = next(r for r in invoices.list_invoices(distributor="TEST-DC") if r["name"] == inv.name)
		self.assertEqual(row["likely_order"], "")
