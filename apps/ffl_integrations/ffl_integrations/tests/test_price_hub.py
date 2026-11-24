"""Price Hub reads: each enabled distributor's cost history, and the store's own
record of the product."""
from __future__ import annotations

import frappe

from ffl_integrations.distributor import price_hub
from ffl_integrations.tests.test_distributor_integration import _Base

UPC = "000000777001"   # _Base: catalog SKU DC-UPC-ACC, and a local Item named by it


def _series(house, costs):
	points = [{"at": f"2026-01-{day:02d}T00:00:00+00:00", "cost": cost}
		for day, cost in enumerate(costs, start=1)]
	return {"distributor": house, "sku": "DC-UPC-ACC", "segments": [{"points": points}],
		"last_cost": costs[-1], "low_cost": min(costs), "high_cost": max(costs)}


class PriceHubDetail(_Base):
	def setUp(self):
		self.stub.trends = {UPC: {"window_start": "2026-01-01T00:00:00+00:00",
			"server_time": "2026-01-31T00:00:00+00:00",
			"series": [_series("rsr", [10, 9]), _series("not-enabled-here", [1])]}}

	def test_only_enabled_distributors_have_a_series(self):
		trend = price_hub.detail(upc=UPC)["trend"]
		self.assertEqual([s["distributor"] for s in trend["series"]], ["TEST-DC"])
		self.assertEqual(trend["series"][0]["segments"][0][1]["cost"], 9)
		self.assertEqual(trend["series"][0]["low_cost"], 9)

	def test_a_product_without_a_upc_is_read_by_its_house_and_sku(self):
		self.stub.trends = {("rsr", "DC-NO-ITEM"): {"series": [_series("rsr", [3])]}}
		out = price_hub.detail(sku="DC-NO-ITEM", distributor="TEST-DC")
		self.assertEqual(out["trend"]["series"][0]["last_cost"], 3)
		self.assertIsNone(out["store"])

	def test_no_history_or_a_failed_read_leaves_the_rest(self):
		self.stub.trends = {}
		self.assertIsNone(price_hub.detail(upc=UPC)["trend"])
		self.stub.cost_trend = lambda **_kw: 1 / 0
		self.addCleanup(lambda: self.stub.__dict__.pop("cost_trend"))
		out = price_hub.detail(upc=UPC)
		self.assertIsNone(out["trend"])
		self.assertEqual(out["store"]["item_code"], UPC)

	def test_store_record_lists_what_was_paid_newest_first(self):
		before = price_hub.detail(upc=UPC)["store"]
		for rate, day in ((7.5, "2026-01-05"), (6.25, "2026-01-20")):
			frappe.get_doc({"doctype": "Purchase Receipt", "supplier": "DC-TEST-SUPPLIER",
				"posting_date": day, "set_posting_time": 1, "items": [{"item_code": UPC, "qty": 2,
					"rate": rate, "warehouse": self._warehouse}]}).insert().submit()
		store = price_hub.detail(upc=UPC)["store"]
		self.assertEqual(store["on_hand"], before["on_hand"] + 4)
		self.assertEqual([r["rate"] for r in store["receipts"][:2]], [6.25, 7.5])
		self.assertEqual(store["receipts"][0]["supplier"], "DC-TEST-SUPPLIER")

	def test_on_order_counts_what_is_still_to_come_in(self):
		before = price_hub.detail(upc=UPC)["store"]["on_order"]
		line = {"sku": "DC-UPC-ACC", "item_code": UPC}
		self._do([{**line, "wish_qty": 5}])                                   # a draft: asked for
		acked = self._do([{**line, "wish_qty": 4}], status="Placed")          # the house sends 3, 1 is in
		frappe.db.set_value("Distributor Order Line", acked.lines[0].name,
			{"ack_qty": 3, "received_qty": 1}, update_modified=False)
		killed = self._do([{**line, "wish_qty": 9}], status="Placed")
		frappe.db.set_value("Distributor Order Line", killed.lines[0].name, "line_status", "Killed",
			update_modified=False)
		self._do([{**line, "wish_qty": 7}], status="Cancelled")
		self.assertEqual(price_hub.detail(upc=UPC)["store"]["on_order"], before + 5 + 2)

	def test_sold_is_stock_out_on_sales_less_returns_inside_the_window(self):
		before = price_hub.detail(upc=UPC)["store"]
		for qty, days_ago, voucher in ((-3, 10, "POS Invoice"), (1, 5, "POS Invoice"),
				(-2, 60, "Sales Invoice"), (-8, 200, "Sales Invoice"), (-4, 3, "Stock Entry")):
			frappe.get_doc({"doctype": "Stock Ledger Entry", "item_code": UPC, "warehouse": self._warehouse,
				"posting_date": frappe.utils.add_days(frappe.utils.nowdate(), -days_ago),
				"actual_qty": qty, "voucher_type": voucher, "voucher_no": "PH-TEST",
				"company": frappe.db.get_single_value("Global Defaults", "default_company"),
			}).db_insert()
		store = price_hub.detail(upc=UPC)["store"]
		self.assertEqual(store["sold_30"], before["sold_30"] + 2)
		self.assertEqual(store["sold_90"], before["sold_90"] + 4)

	def test_nothing_named_reads_nothing(self):
		self.assertEqual(price_hub.detail(), {"trend": None, "store": None})

	def test_reading_costs_takes_an_ordering_role(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				price_hub.detail(upc=UPC)
		finally:
			frappe.set_user("Administrator")

	def test_a_series_says_whether_its_last_sample_is_current(self):
		self.stub.trends[UPC]["series"][0]["state"] = "stale"
		self.assertEqual(price_hub.detail(upc=UPC)["trend"]["series"][0]["state"], "stale")

	def test_signals_merge_the_enabled_houses_strongest_first(self):
		row = lambda sku, score: {"sku": sku, "upc": "1", "name": sku, "current_value": 5,  # noqa: E731
			"reference_value": 6, "amount": 1, "score": score}
		self.stub.signal_lists = {"rsr": {"cost_drop": [row("A", 6), row("B", 12)],
			"stockout": [row("C", 4), row("D", 1.5)]}, "not-enabled-here": {"cost_drop": [row("Z", 99)]}}
		out = price_hub.signals()
		self.assertEqual([r["sku"] for r in out["cost_drop"]], ["B", "A"])
		self.assertEqual([r["sku"] for r in out["stockout"]], ["D", "C"])
		self.assertEqual(out["cost_drop"][0]["distributor"], "TEST-DC")
		self.assertEqual(out["fast_depletion"], [])

	def test_signals_survive_a_house_that_cannot_be_read(self):
		self.stub.signals = lambda *_a: 1 / 0
		self.addCleanup(lambda: self.stub.__dict__.pop("signals"))
		self.assertEqual(price_hub.signals(), {"cost_drop": [], "stockout": [], "fast_depletion": []})

	def test_search_offers_carry_the_case_price(self):
		from ffl_integrations.distributor import api as dist_api

		offer = dist_api.search("DC-UPC-ACC")[0]["offers"][0]
		self.assertIn("case_cost", offer)
		self.assertIn("units_per_case", offer)

	@property
	def _warehouse(self):
		from ffl_core.warehouses import default_stock_warehouse

		return default_stock_warehouse(frappe.db.get_single_value("Global Defaults", "default_company"))
