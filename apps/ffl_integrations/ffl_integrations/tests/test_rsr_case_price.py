"""RSR full-case pricing for lines shipped to the store.

RSR's Direct Connect get-items carries a per-UNIT price for a full case
(``DealerCasePrice``) and the case size (``UnitsPerCase``). A ship-to-store line
(stocking / procure-to-store) of qty Q buys its full cases at that price and the
rest at the unit cost; a drop-ship line always pays the unit cost. The figure is an
estimate: the Cost RSR acknowledges on placement replaces it.

Covered: the shared helper's math and hint, the catalog row contract (keys may be
absent), the Create Order planner and the Draft Distributor Order using it for the
store lanes only, the acknowledged Cost still winning, and the local catalog's
``case_price`` lane (parse, page, write, skip when unconfigured, defer in the
maintenance window, refuse a partial read). Only RSR's HTTP and the catalog stub
are faked; the frappe side is real.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.backend import LocalCatalog
from ffl_integrations.catalog_local.drivers import rsr
from ffl_integrations.distributor import api as dist_api
from ffl_integrations.distributor import tasks
from ffl_integrations.distributor.remote_catalog_mixin import remote_normalised
from ffl_integrations.distributor.util import case_note, case_pricing
from ffl_integrations.rsr.api_client import RSRClient, RSRConfig
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests.rsr_synth import catalog_text, sku
from ffl_integrations.tests.test_catalog_local_rsr import TCODE, _age_runs, _runs
from ffl_integrations.tests.test_catalog_local_rsr import _Base as _RsrLaneBase


def upc_a(body11: str) -> str:
	"""A UPC-A with its check digit (ERPNext validates barcodes)."""
	digits = [int(c) for c in body11]
	check = (10 - (sum(digits[0::2]) * 3 + sum(digits[1::2])) % 10) % 10
	return body11 + str(check)


# ---------------------------------------------------------------- the helper

class TestCasePricingMath(unittest.TestCase):
	U, C, N = 7.99, 7.83, 50

	def price(self, q, u=U, c=C, n=N):
		return case_pricing(q, u, c, n)

	def test_short_of_a_case_pays_the_unit_cost_and_says_how_many_fill_one(self):
		p = self.price(30)
		self.assertEqual((p["cases"], p["to_fill"], p["line_cost"], p["unit_cost"]),
			(0, 20, 239.70, 7.99))
		self.assertEqual(case_note(p), "Case of 50 at $7.83 each — 20 more fill a case")

	def test_exactly_one_case(self):
		p = self.price(50)
		self.assertEqual((p["cases"], p["to_fill"], p["line_cost"], p["unit_cost"]),
			(1, 0, 391.50, 7.83))
		self.assertEqual(case_note(p), "1 case at $7.83 each")

	def test_two_cases_and_a_remainder(self):
		p = self.price(120)   # 100 at 7.83 + 20 at 7.99
		self.assertEqual((p["cases"], p["to_fill"], p["line_cost"]), (2, 30, 942.80))
		self.assertEqual(p["unit_cost"], 7.86)   # 942.80 / 120 = 7.8567, to the cent
		self.assertEqual(case_note(p), "2 cases at $7.83 each — 30 more fill another")
		self.assertEqual(case_note(self.price(100)), "2 cases at $7.83 each")

	def test_a_case_price_not_below_the_unit_cost_is_ignored(self):
		self.assertIsNone(self.price(100, c=7.99))
		self.assertIsNone(self.price(100, c=8.50))

	def test_missing_or_empty_case_data_is_no_case_price(self):
		for c, n in ((0, 50), (None, 50), (7.83, 0), (7.83, None), (None, None), (-1, 50)):
			self.assertIsNone(self.price(100, c=c, n=n), (c, n))
		self.assertIsNone(self.price(0))
		self.assertIsNone(self.price(10, u=0))
		self.assertEqual(case_note(None), "")

	def test_catalog_strings_are_read(self):
		self.assertEqual(case_pricing("50", "7.99", "7.83", "50")["unit_cost"], 7.83)


class TestCatalogRowContract(unittest.TestCase):
	BASE = {"sku": "S1", "upc": None, "manufacturer": "", "model": "", "department": "18",
		"cost": 7.99, "map": 0, "msrp": 0, "qty": 3, "ffl_required": False,
		"dropship_blocked": False, "vendor_approval_required": False, "image_url": "",
		"discontinued": False, "status_flag": ""}

	def test_a_service_without_the_keys_reads_as_no_case_price(self):
		row = remote_normalised(dict(self.BASE))
		self.assertEqual((row["case_cost"], row["units_per_case"]), (0.0, 0))

	def test_null_and_present_values(self):
		row = remote_normalised(dict(self.BASE, case_cost=None, units_per_case=None))
		self.assertEqual((row["case_cost"], row["units_per_case"]), (0.0, 0))
		row = remote_normalised(dict(self.BASE, case_cost="7.83", units_per_case="50"))
		self.assertEqual((row["case_cost"], row["units_per_case"]), (7.83, 50))


class TestPreselectUsesTheModesCost(unittest.TestCase):
	def test_a_house_that_ships_to_the_store_is_ranked_by_its_case_priced_cost(self):
		row = {"local": {"feasible": False}, "channels": [
			{"distributor": "A", "feasible": True, "mode": "direct", "cost": 7.90,
				"store_cost": 7.90},
			{"distributor": "B", "feasible": True, "mode": "to_store", "cost": 7.99,
				"store_cost": 7.83},
		]}
		self.assertEqual(dist_api._matrix_preselect(row), "B")
		row["channels"][1]["mode"] = "direct"   # drop-ship pays the unit cost
		self.assertEqual(dist_api._matrix_preselect(row), "A")


# ---------------------------------------------------------------- planner + Draft DO

DIST = "CASE-DC"
CASE_SKU = "CASE-AMMO-1"
PLAIN_SKU = "CASE-PLAIN-1"
CASE_UPC = upc_a("09999100001")


class TestStoreLinesUseTheCasePrice(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap

		test_bootstrap.run()
		if not frappe.db.exists("Distributor", DIST):
			frappe.get_doc({"doctype": "Distributor", "code": DIST,
				"distributor_name": "Case Price DC", "provider": "rsr", "enabled": 1,
				"dropship_enabled": 1, "stocking_enabled": 1, "poll_enabled": 0,
				"override_dropship_defaults": 1}).insert(ignore_permissions=True)
		group = "Ammunition" if frappe.db.exists("Item Group", "Ammunition") else "All Item Groups"
		for code in (CASE_SKU, PLAIN_SKU):
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
					"item_group": group, "stock_uom": "Nos", "is_stock_item": 1}).insert(
					ignore_permissions=True)

	def setUp(self):
		catalog_stub.install()
		self.addCleanup(catalog_stub.uninstall)
		catalog_stub.seed_catalog([
			{"rsr_stock_number": CASE_SKU, "upc": CASE_UPC, "dealer_price": 7.99,
				"quantity": 500, "department_number": "18", "short_description": "CASE AMMO",
				"case_cost": 7.83, "units_per_case": 50},
			{"rsr_stock_number": PLAIN_SKU, "dealer_price": 12.0, "quantity": 500,
				"department_number": "18", "short_description": "PLAIN AMMO"},
		])

	def _cell(self, sku_, qty):
		m = dist_api.fulfillment_matrix([{"sku": sku_, "qty": qty}])
		return next(c for c in m["rows"][0]["channels"] if c["distributor"] == DIST)

	def test_planner_cell_carries_the_store_cost_and_the_hint(self):
		cell = self._cell(CASE_SKU, 120)
		self.assertEqual(cell["cost"], 7.99)          # drop-ship pays the unit cost
		self.assertEqual(cell["store_cost"], 7.86)
		self.assertEqual(cell["case_note"], "2 cases at $7.83 each — 30 more fill another")
		cell = self._cell(CASE_SKU, 30)
		self.assertEqual(cell["store_cost"], 7.99)
		self.assertEqual(cell["case_note"], "Case of 50 at $7.83 each — 20 more fill a case")
		plain = self._cell(PLAIN_SKU, 120)
		self.assertEqual((plain["store_cost"], plain.get("case_note")), (12.0, None))

	def _order(self, mode, qty=120):
		created = dist_api.create_orders(DIST, json.dumps([
			{"sku": CASE_SKU, "item_code": CASE_SKU, "qty": qty},
			{"sku": PLAIN_SKU, "item_code": PLAIN_SKU, "qty": qty}]), json.dumps({"mode": mode}))
		self.assertEqual(len(created), 1)
		return frappe.get_doc("Distributor Order", created[0]["name"])

	def test_stocking_and_procure_to_store_drafts_use_the_case_price(self):
		for mode, order_type in (("stocking", "Stocking"), ("to_store", "Procure To Store")):
			do = self._order(mode)
			self.assertEqual(do.order_type, order_type)
			lines = {ln.sku: ln for ln in do.lines}
			self.assertEqual(lines[CASE_SKU].unit_cost, 7.86, mode)
			self.assertEqual(lines[CASE_SKU].case_note,
				"2 cases at $7.83 each — 30 more fill another")
			self.assertEqual((lines[PLAIN_SKU].unit_cost, lines[PLAIN_SKU].case_note or ""),
				(12.0, ""))
			self.assertAlmostEqual(do.total_cost, 7.86 * 120 + 12.0 * 120, places=2)

	def test_a_drop_ship_draft_pays_the_unit_cost(self):
		dist = frappe.get_doc("Distributor", DIST)
		provider = dist_api.get_provider(dist.provider)
		enriched = dist_api.enrich_line(provider, {"sku": CASE_SKU, "item_code": CASE_SKU,
			"qty": 120})
		self.assertEqual((enriched["case_cost"], enriched["units_per_case"]), (7.83, 50))
		plan = {"account": "fulfillment", "order_type": "Dropship Accessory",
			"fill_or_kill": False, "ship_to": {"line1": "1 Test St", "city": "Testville",
				"state": "TX", "zip": "75001"},
			"lines": [{"sku": CASE_SKU, "item_code": CASE_SKU, "qty": 120, "ship_method": ""}]}
		do = dist_api.create_do_from_plan(dist, plan, {CASE_SKU: enriched})
		self.assertEqual((do.lines[0].unit_cost, do.lines[0].case_note or ""), (7.99, ""))

	def test_the_acknowledged_cost_replaces_the_estimate(self):
		do = self._order("stocking")
		frappe.db.set_value("Distributor Order", do.name, "status", "Queued",
			update_modified=False)

		class Acked:
			def place_order(self, name):
				return {"web_ref": "W-CASE", "hold_status": "", "raw": {}, "ack_lines": [
					{"sku": CASE_SKU, "ack_qty": 120, "unit_cost": 7.95, "item_status": "OK"},
					# no Cost in the ack: the estimate stands
					{"sku": PLAIN_SKU, "ack_qty": 120, "unit_cost": 0, "item_status": "OK"}]}

		with mock.patch.object(tasks, "_provider_for", return_value=Acked()):
			tasks.place_order_job(do.name)
		do.reload()
		lines = {ln.sku: ln for ln in do.lines}
		self.assertEqual(do.status, "Placed")
		self.assertEqual(lines[CASE_SKU].unit_cost, 7.95)
		self.assertEqual(lines[PLAIN_SKU].unit_cost, 12.0)


# ---------------------------------------------------------------- the local lane

def _item(n, case="", per_case="0", dealer="9.99"):
	return {"SKU": sku(n), "UnitOfMeasure": "BX", "ItemsPerUnit": "20", "ItemsPerCase": "1000",
		"UnitsPerCase": per_case, "DealerPrice": dealer, "DealerCasePrice": case}


class _Session:
	"""Answers get-items like RSR: TotalFound for the whole book, at most ``cap``
	items a page. Everything else is a test failure."""

	def __init__(self, items, cap=None, fail_at=None, total=None):
		self.items, self.cap, self.fail_at, self.total = list(items), cap, fail_at, total
		self.bodies = []

	def request(self, method, url, json=None, timeout=None):
		assert url.endswith("/get-items"), url
		self.bodies.append(dict(json))
		off, limit = int(json["Offset"]), int(json["Limit"])
		resp = mock.MagicMock(status_code=200, content=b"x")
		if self.fail_at is not None and off >= self.fail_at:
			resp.json.return_value = {"StatusCode": "99", "StatusMssg": "Failed to login"}
			return resp
		page = self.items[off:off + min(limit, self.cap or limit)]
		resp.json.return_value = {"StatusCode": "00", "RowCount": str(len(page)),
			"TotalFound": str(len(self.items) if self.total is None else self.total), "Items": page}
		return resp


def _client(session, hour_et=12):
	config = RSRConfig(api_base_url="https://rsr.invalid", pos_indicator="I",
		dealer_account="D1", dealer_password="pw", fulfillment_account="",
		fulfillment_password="", api_enabled=True)
	client = RSRClient(config, session=session)
	client._now_et = lambda: datetime(2026, 10, 28, hour_et, 30, tzinfo=ZoneInfo("America/New_York"))
	return client


class TestCasePriceParse(unittest.TestCase):
	def test_an_item_reads_per_unit_price_and_case_size(self):
		self.assertEqual(rsr.case_price_row(_item(1, "7.83", "50", "7.99")), (sku(1), 7.83, 50))
		self.assertEqual(rsr.case_price_row(_item(2, "$1,234.50", "4")), (sku(2), 1234.5, 4))
		self.assertEqual(rsr.case_price_row(_item(3, "", "0")), (sku(3), 0.0, 0))
		self.assertEqual(rsr.case_price_row(_item(4, "n/a", "lots")), (sku(4), 0.0, 0))
		self.assertEqual(rsr.case_price_row({"SKU": sku(5)}), (sku(5), 0.0, 0))

	def test_pages_by_what_each_page_returned(self):
		session = _Session([_item(n, "1.00", "2") for n in range(1, 6)], cap=2)
		rows = rsr.fetch_case_prices(_client(session))
		self.assertEqual([r[0] for r in rows], [sku(n) for n in range(1, 6)])
		self.assertEqual([b["Offset"] for b in session.bodies], [0, 2, 4])
		self.assertTrue(all(b["Username"] == "D1" for b in session.bodies))   # dealer account

	def test_a_short_or_empty_book_is_refused(self):
		with self.assertRaises(sync.FeedFormatError):
			rsr.fetch_case_prices(_client(_Session([_item(1)], total=3)))
		with self.assertRaises(sync.FeedFormatError):
			rsr.fetch_case_prices(_client(_Session([])))
		with self.assertRaises(sync.FeedUnavailable):
			rsr.fetch_case_prices(_client(_Session([_item(n) for n in range(1, 6)], cap=2,
				fail_at=2)))


class TestApiClientChoice(unittest.TestCase):
	def _config(self, **over):
		base = dict(api_base_url="https://rsr.invalid", pos_indicator="I", dealer_account="D1",
			dealer_password="pw", fulfillment_account="", fulfillment_password="",
			api_enabled=True)
		return RSRConfig(**{**base, **over})

	def test_the_shipped_driver_reads_rsr_settings(self):
		driver = rsr.RsrDriver(code=TCODE)
		for over, want in (({}, True), ({"api_enabled": False}, False),
				({"dealer_password": ""}, False), ({"dealer_account": ""}, False)):
			with mock.patch.object(RSRConfig, "from_settings", return_value=self._config(**over)):
				self.assertEqual(isinstance(driver._api_client(), RSRClient), want, over)

	def test_a_driver_over_a_fake_feed_never_reaches_the_live_api(self):
		with mock.patch.object(RSRConfig, "from_settings") as settings:
			self.assertIsNone(rsr.RsrDriver(code=TCODE, transport=object())._api_client())
		settings.assert_not_called()


def _case(s):
	cost, per_case = frappe.db.sql("""SELECT case_cost, units_per_case FROM dc_offer
		WHERE distributor = %s AND sku = %s""", (TCODE, s))[0]
	return (float(cost), int(per_case))


class TestCasePriceLane(_RsrLaneBase):
	SOURCE = f"{TCODE}_case_price"

	def setUp(self):
		super().setUp()
		self.session = _Session([
			_item(1, "7.83", "50", "7.99"),      # a case item
			_item(2, "", "0"),                    # not sold by the case
			_item(3, "6.00", "0"),                # a price but no case size
			_item(99, "1.00", "10"),              # a SKU this catalog does not carry
		])
		self.client = _client(self.session)
		self.driver = sync.register(rsr.RsrDriver(code=TCODE, transport=self.transport,
			items_client=self.client))
		sync.run_lane(TCODE, "catalog")

	def test_writes_only_the_case_columns_and_moves_no_change_seq(self):
		seqs = dict(frappe.db.sql("SELECT sku, change_seq FROM dc_offer WHERE distributor = %s",
			(TCODE,)))
		out = sync.run_lane(TCODE, "case_price")
		self.assertEqual(out, {"updated": 1, "total": 4})
		self.assertEqual(_case(sku(1)), (7.83, 50))
		self.assertEqual((_case(sku(2)), _case(sku(3))), ((0, 0), (0, 0)))
		self.assertEqual(dict(frappe.db.sql("SELECT sku, change_seq FROM dc_offer "
			"WHERE distributor = %s", (TCODE,))), seqs)
		self.assertEqual(_runs()[-1], (self.SOURCE, "success", ""))
		row = LocalCatalog().rows(TCODE, [sku(1), sku(2)])
		self.assertEqual((row[sku(1)]["case_cost"], row[sku(1)]["units_per_case"]), (7.83, 50))
		self.assertIsInstance(row[sku(1)]["case_cost"], float)
		self.assertEqual((row[sku(2)]["case_cost"], row[sku(2)]["units_per_case"]), (0.0, 0))
		# the catalog lane's reload leaves them alone
		self.transport.files["catalog"] = catalog_text(range(1, 13))
		sync.run_lane(TCODE, "catalog", force=True)
		self.assertEqual(_case(sku(1)), (7.83, 50))

	def test_daily_gate_and_a_snapshot_that_drops_a_case_price(self):
		sync.run_lane(TCODE, "case_price")
		self.session.bodies.clear()
		self.assertEqual(sync.run_lane(TCODE, "case_price"), sync.NOT_DUE)
		self.assertEqual(self.session.bodies, [])
		_age_runs(self.SOURCE, 25)
		self.session.items[0] = _item(1, "", "0")
		self.assertEqual(sync.run_lane(TCODE, "case_price"), {"updated": 1, "total": 4})
		self.assertEqual(_case(sku(1)), (0, 0))

	def test_skipped_without_a_run_when_the_api_is_not_set_up(self):
		sync.register(rsr.RsrDriver(code=TCODE, transport=self.transport))
		self.assertEqual(sync.run_lane(TCODE, "case_price", skip_schedule=True), sync.NOT_DUE)
		self.assertNotIn(self.SOURCE, [r[0] for r in _runs()])

	def test_deferred_in_the_maintenance_window(self):
		self.client._now_et = lambda: datetime(2026, 10, 28, 4, 10,
			tzinfo=ZoneInfo("America/New_York"))
		self.assertEqual(sync.run_lane(TCODE, "case_price", skip_schedule=True), sync.NOT_DUE)
		self.assertEqual(self.session.bodies, [])
		self.assertNotIn(self.SOURCE, [r[0] for r in _runs()])

	def test_a_partial_read_fails_the_lane_and_keeps_the_last_prices(self):
		sync.run_lane(TCODE, "case_price")
		self.session.items[0] = _item(1, "", "0")
		self.session.total = 10   # RSR says 10, sends 4
		self.assertIsNone(sync.run_lane(TCODE, "case_price", skip_schedule=True))
		self.assertEqual(_runs()[-1], (self.SOURCE, "failed", "FeedFormatError"))
		self.assertEqual(_case(sku(1)), (7.83, 50))

	def test_a_lock_skip_records_nothing(self):
		other = self._other_connection()
		self.assertEqual(other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(TCODE),))[0][0], 1)
		try:
			out = sync.run_lane(TCODE, "case_price")
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(TCODE),))
		self.assertEqual(out, store.LOCK_SKIPPED)
		self.assertNotIn(self.SOURCE, [r[0] for r in _runs()])
