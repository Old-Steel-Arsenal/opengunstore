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

	def test_nothing_named_reads_nothing(self):
		self.assertEqual(price_hub.detail(), {"trend": None, "store": None})

	def test_reading_costs_takes_an_ordering_role(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				price_hub.detail(upc=UPC)
		finally:
			frappe.set_user("Administrator")

	@property
	def _warehouse(self):
		from ffl_core.warehouses import default_stock_warehouse

		return default_stock_warehouse(frappe.db.get_single_value("Global Defaults", "default_company"))
