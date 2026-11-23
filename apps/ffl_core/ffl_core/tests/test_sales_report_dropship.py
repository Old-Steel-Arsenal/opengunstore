"""The drop-ship cost branch in Sales Report.

`_line_cost` can only derive a cost two ways today, and a drop-ship line defeats
both:

* the firearm branch sums ``Serial No.purchase_rate`` — an FDS gun has no local
  Serial No at all, so the lookup finds nothing;
* the fallback uses the Item's Bin moving average — a SKU that has never been in
  our warehouse has none.

Either way the line reports **cost 0 → a phantom 100 % margin**, and the FDS gun
additionally raises a false "missing cost" flag. Worse, a SKU we ALSO stock
locally would price a drop-shipped unit at our own moving average — a cost that
belongs to different goods.

The real cost is the distributor order line's ``unit_cost`` (RSR's acknowledged
cost), which is also exactly what the cost Purchase Invoice books — so the
report's margin and the GL's COGS come from one number.

Branch placement matters as much as the arithmetic: it must sit BEFORE the
firearm branch, for the same reason the consignment branch does — otherwise the
firearm branch wins first with cost 0.
"""
from __future__ import annotations

import unittest

import frappe

from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.ffl_core.report.sales_report.sales_report import _build_rows, _line_cost


def _line(item="RSR-GLK19", qty=1, net=699.0, serial=None, dropship="RSR", to_store=None):
	return frappe._dict({
		"item_code": item, "qty": qty, "base_net_amount": net, "serial_no": serial,
		"consignment_commission_pct": None, "dropship_distributor": dropship,
		"ffl_to_store_distributor": to_store,
		"parent": "SI-1",
	})


def _hdr(name="SI-1", net=699.0, total=699.0):
	return {"name": name, "voucher_type": "Sales Invoice", "channel": "Web",
		"posting_date": "2026-07-23", "customer": "C1", "base_net_total": net,
		"grand_total": total, "is_return": 0, "woo_online_order": "WOO-ORD-1"}


FIREARM_META = {"RSR-GLK19": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}}
ACC_META = {"RSR-HOLO": {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Optics"}}


# ================================================================ the branch

class TestDropshipCostBranch(unittest.TestCase):

	def test_fds_gun_costs_the_acknowledged_distributor_cost(self):
		cost, missing, _ = _line_cost(
			_line(), FIREARM_META, {}, dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, 430.0)
		self.assertFalse(missing)

	def test_cost_is_the_ack_total_not_the_sold_quantity(self):
		"""The map carries the DO's acknowledged total, so the branch returns it
		directly (sign only follows qty) — it does NOT re-multiply by the SI qty."""
		cost, _, _ = _line_cost(
			_line(qty=3, net=897.0, item="RSR-HOLO", dropship="RSR"),
			ACC_META, {}, dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 297.0})
		self.assertEqual(cost, 297.0)

	def test_margin_is_no_longer_a_phantom_hundred_percent(self):
		"""The defect in one assertion."""
		cost, _, _ = _line_cost(
			_line(net=699.0), FIREARM_META, {},
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertGreater(cost, 0)
		self.assertNotEqual(cost, 699.0)

	def test_branch_beats_the_firearm_branch(self):
		"""A drop-ship gun that somehow carries a serial must STILL be costed from
		the distributor order — the same precedence the consignment branch needs."""
		cost, missing, _ = _line_cost(
			_line(serial="RSRSERIAL1"), FIREARM_META, {"RSRSERIAL1": 0.0},
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, 430.0)
		self.assertFalse(missing, "must not inherit the firearm branch's missing flag")

	def test_branch_beats_the_bin_valuation_branch(self):
		"""A SKU we also stock locally: the drop-shipped unit never passed through
		our warehouse, so our moving average is the wrong goods' cost."""
		meta = {"RSR-HOLO": {"is_firearm": 0, "valuation_rate": 250.0, "item_group": "Optics"}}
		cost, _, _ = _line_cost(
			_line(item="RSR-HOLO", net=299.0), meta, {},
			dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 99.0})
		self.assertEqual(cost, 99.0)

	def test_missing_acknowledged_cost_is_a_real_data_gap(self):
		"""Flagged — but as a genuine gap (RSR has not acked yet), not because a
		local serial lookup failed."""
		cost, missing, _ = _line_cost(_line(), FIREARM_META, {}, dropship_cost={})
		self.assertEqual(cost, 0.0)
		self.assertTrue(missing)

	def test_a_return_reverses_the_cost(self):
		cost, _, _ = _line_cost(
			_line(qty=-1, net=-699.0), FIREARM_META, {},
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, -430.0)

	def test_non_dropship_lines_are_untouched(self):
		"""REGRESSION: the branch keys off the LINE's dropship_distributor, so an
		ordinary gun keeps the serial branch even when a map is supplied."""
		line = _line(item="GLOCK-19", serial="SN-1", dropship=None)
		meta = {"GLOCK-19": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}}
		cost, missing, _ = _line_cost(line, meta, {"SN-1": 400.0},
			dropship_cost={("SI-1", "GLOCK-19", "RSR"): 999.0})
		self.assertEqual(cost, 400.0)
		self.assertFalse(missing)

	def test_c2_to_store_gun_keeps_the_firearm_branch(self):
		"""A procure-to-store gun IS ours, with a
		real Serial No and a real purchase_rate. Its SI line carries no
		dropship_distributor, so the drop-ship branch must not claim it."""
		line = _line(item="RSR-GLK19", serial="OURS-9", dropship=None)
		cost, missing, _ = _line_cost(line, FIREARM_META, {"OURS-9": 415.0},
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, 415.0)
		self.assertFalse(missing)

	# ---------------------------------------------- ship to store

	def test_to_store_accessory_on_the_way_costs_the_order_line(self):
		"""A past invoice: ship-to-store accessories never stocked before
		read a Bin average of 0 and reported a phantom 100 % margin."""
		cost, missing, _ = _line_cost(
			_line(item="RSR-HOLO", net=299.0, dropship=None, to_store="RSR"),
			ACC_META, {}, dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 99.0})
		self.assertEqual(cost, 99.0)
		self.assertFalse(missing)

	def test_to_store_accessory_ignores_the_bin_average_even_once_stocked(self):
		"""The Bin average mixes every other lot of the SKU; the order line is what
		THESE goods cost."""
		meta = {"RSR-HOLO": {"is_firearm": 0, "valuation_rate": 250.0, "item_group": "Optics"}}
		cost, _, _ = _line_cost(
			_line(item="RSR-HOLO", net=299.0, dropship=None, to_store="RSR"),
			meta, {}, dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 99.0})
		self.assertEqual(cost, 99.0)

	def test_to_store_gun_on_the_way_costs_the_order_line(self):
		"""No serial until it arrives — the firearm branch would find nothing."""
		cost, missing, _ = _line_cost(
			_line(dropship=None, to_store="RSR"), FIREARM_META, {},
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, 430.0)
		self.assertFalse(missing)

	def test_to_store_gun_that_arrived_keeps_the_firearm_branch(self):
		"""Once hung, the serial's purchase_rate (the Purchase Receipt's
		figure) is the cost — the order line is not consulted."""
		cost, missing, _ = _line_cost(
			_line(serial="OURS-9", dropship=None, to_store="RSR"), FIREARM_META,
			{"OURS-9": 415.0}, dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(cost, 415.0)
		self.assertFalse(missing)

	def test_to_store_without_an_ack_is_a_real_data_gap(self):
		cost, missing, _ = _line_cost(
			_line(item="RSR-HOLO", dropship=None, to_store="RSR"), ACC_META, {},
			dropship_cost={})
		self.assertEqual(cost, 0.0)
		self.assertTrue(missing)

	def test_same_sku_from_two_houses_is_costed_per_house(self):
		"""One invoice, one SKU, a drop-ship leg at RSR and a ship-to-store leg at
		Sports South: each line reads its own order, never the sum of both."""
		costs = {("SI-1", "RSR-HOLO", "RSR"): 99.0, ("SI-1", "RSR-HOLO", "Sports South"): 90.0}
		self.assertEqual(_line_cost(_line(item="RSR-HOLO", dropship="RSR"),
			ACC_META, {}, dropship_cost=costs)[0], 99.0)
		self.assertEqual(_line_cost(_line(item="RSR-HOLO", dropship=None, to_store="Sports South"),
			ACC_META, {}, dropship_cost=costs)[0], 90.0)

	def test_absent_map_does_not_break_existing_callers(self):
		"""The parameter is keyword-only with a default — every existing call site
		keeps working unchanged."""
		line = _line(item="GLOCK-19", serial="SN-1", dropship=None)
		meta = {"GLOCK-19": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}}
		self.assertEqual(_line_cost(line, meta, {"SN-1": 400.0})[0], 400.0)


# =========================================== reconciliation with the PI

class TestReportTiesToPurchaseInvoice(unittest.TestCase):
	"""The report's drop-ship cost total must equal the
	PI's COGS leg to the cent, so margin and the GL cannot tell two stories."""

	def test_totals_match_the_pi_cogs_leg_to_the_cent(self):
		from ffl_integrations.distributor import accounting

		do = frappe._dict({
			"name": "DO-2607-00001", "order_type": "Dropship Firearm",
			"freight_cost": 25.0,
			"lines": [
				frappe._dict({"sku": "RSR-GLK19", "item_code": "RSR-GLK19", "ack_qty": 1,
					"unit_cost": 430.07, "fulfillment": "fds", "description": "G19"}),
				frappe._dict({"sku": "RSR-HOLO", "item_code": "RSR-HOLO", "ack_qty": 2,
					"unit_cost": 99.33, "fulfillment": "direct", "description": "Holo"}),
			],
		})
		payload = accounting.build_pi_payload(do, supplier="RSR GROUP INC",
			company="C", abbr="TSA", bill_no=None)
		pi_cogs = round(sum(
			i["qty"] * i["rate"] for i in payload["items"]
			if i["expense_account"] == "Cost of Goods Sold - TSA"), 2)

		# The report map carries the SAME acknowledged totals the PI books
		# (ack_qty × unit_cost): GLK 1×430.07, HOLO 2×99.33 — this is what
		# _fetch_dropship_costs produces.
		dropship_cost = {("SI-1", "RSR-GLK19", "RSR"): 1 * 430.07, ("SI-1", "RSR-HOLO", "RSR"): 2 * 99.33}
		meta = dict(FIREARM_META)
		meta["RSR-HOLO"] = {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Optics"}
		report_cost = round(sum(
			_line_cost(l, meta, {}, dropship_cost=dropship_cost)[0]
			for l in (_line(item="RSR-GLK19", qty=1),
				_line(item="RSR-HOLO", qty=2, net=298.0))), 2)

		self.assertEqual(report_cost, pi_cogs)
		self.assertEqual(report_cost, 628.73)

	def test_tie_out_holds_on_a_short_ship(self):
		"""The case that exposed it: ack < sold. Both the PI and the report use the
		ack quantity, so they still tie."""
		from ffl_integrations.distributor import accounting

		do = frappe._dict({
			"name": "DO-1", "order_type": "Dropship Accessory", "freight_cost": 0.0,
			"lines": [frappe._dict({"sku": "RSR-HOLO", "item_code": "RSR-HOLO",
				"ack_qty": 2, "unit_cost": 99.0, "fulfillment": "direct",
				"description": "Holo"})]})
		payload = accounting.build_pi_payload(do, supplier="S", company="C",
			abbr="TSA", bill_no=None)
		pi_cogs = round(sum(i["qty"] * i["rate"] for i in payload["items"]
			if i["expense_account"] == "Cost of Goods Sold - TSA"), 2)
		self.assertEqual(pi_cogs, 198.0)

		# The customer's SI line sold 3, but only 2 were acked/shipped.
		report_cost = _line_cost(
			_line(item="RSR-HOLO", qty=3, net=897.0), ACC_META, {},
			dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 198.0})[0]
		self.assertEqual(report_cost, pi_cogs, "report and PI tie on ack, not sold qty")

	def test_freight_is_not_in_the_line_margin(self):
		"""Freight is order-level and cannot be split per SKU, so it stays off the
		product COGS both sides — otherwise the two totals would never tie."""
		from ffl_integrations.distributor import accounting

		do = frappe._dict({"name": "DO-1", "order_type": "Dropship Accessory",
			"freight_cost": 25.0,
			"lines": [frappe._dict({"sku": "A", "item_code": "A", "ack_qty": 1,
				"unit_cost": 100.0, "fulfillment": "direct", "description": "A"})]})
		payload = accounting.build_pi_payload(do, supplier="S", company="C",
			abbr="TSA", bill_no=None)
		cogs = [i for i in payload["items"]
			if i["expense_account"] == "Cost of Goods Sold - TSA"]
		self.assertEqual(sum(i["qty"] * i["rate"] for i in cogs), 100.0)


# ================================================= wiring through _build_rows

class TestOrderViewWiring(unittest.TestCase):

	def test_invoice_row_cost_uses_the_dropship_map(self):
		rows = _build_rows(
			[_hdr()], {"SI-1": [_line()]}, FIREARM_META, {}, "",
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		body = [r for r in rows if r.get("invoice")]
		self.assertEqual(body[0]["cost"], 430.0)
		self.assertFalse(body[0]["missing_cost"])

	def test_row_shape_is_unchanged(self):
		"""The golden-snapshot contract: costing a line differently must not add
		keys to the Order-view row."""
		plain = _build_rows([_hdr()], {"SI-1": [_line(dropship=None)]}, FIREARM_META, {}, "")
		withmap = _build_rows(
			[_hdr()], {"SI-1": [_line()]}, FIREARM_META, {}, "",
			dropship_cost={("SI-1", "RSR-GLK19", "RSR"): 430.0})
		self.assertEqual(set(plain[0].keys()), set(withmap[0].keys()))


# ============================================================ the prefetch

class TestDropshipCostPrefetch(unittest.TestCase):
	"""`_fetch_dropship_costs` must be ONE bulk pass (the house pattern), keyed so
	a line can find its distributor order through either link:

	* web  — ``SI.woo_online_order`` ↔ ``DO.source_name`` (Woo Online Order);
	* counter — ``DO.source_name`` == the Sales Invoice itself.
	"""

	def test_no_dropship_lines_means_no_queries(self):
		calls = []
		orig = frappe.get_all
		frappe.get_all = lambda *a, **k: (calls.append(a), [])[1]
		try:
			out = sr._fetch_dropship_costs([_hdr()], {"SI-1": [_line(dropship=None)]})
		finally:
			frappe.get_all = orig
		self.assertEqual(out, {})
		self.assertEqual(calls, [], "must not query when nothing is drop-ship")

	def test_prefetch_costs_by_ack_quantity_not_unit_rate(self):
		"""The map value is the DO line's ACKNOWLEDGED total (ack_qty × unit_cost),
		so a short-ship (ack < ordered) ties the report to the PI, which also books
		ack_qty."""
		hdr = _hdr()
		calls = {"n": 0}

		def _fake_get_all(dt, filters=None, fields=None, **k):
			calls["n"] += 1
			if dt == "Distributor Order":
				return [{"name": "DO-1", "source_doctype": "Sales Invoice",
					"source_name": "SI-1", "distributor": "RSR"}]
			if dt == "Distributor Order Line":
				return [{"parent": "DO-1", "item_code": "RSR-HOLO", "sku": "RSR-HOLO",
					"unit_cost": 99.0, "ack_qty": 2}]
			return []

		orig = frappe.get_all
		frappe.get_all = _fake_get_all
		try:
			out = sr._fetch_dropship_costs([hdr],
				{"SI-1": [_line(item="RSR-HOLO", qty=3, net=897.0)]})
		finally:
			frappe.get_all = orig
		self.assertEqual(out[("SI-1", "RSR-HOLO", "RSR")], 198.0, "2 acked × 99, not 3 sold × 99")

	def test_short_ship_line_cost_is_the_ack_total(self):
		"""The SI sold 3 but RSR acked 2 → cost is 2×99, matching the PI."""
		cost, missing, _ = _line_cost(
			_line(item="RSR-HOLO", qty=3, net=897.0), ACC_META, {},
			dropship_cost={("SI-1", "RSR-HOLO", "RSR"): 198.0})
		self.assertEqual(cost, 198.0)
		self.assertFalse(missing)

	def test_a_to_store_only_invoice_is_prefetched_too(self):
		"""The prefetch used to skip an invoice with no drop-ship line — which is
		exactly a counter ship-to-store order."""
		def _fake_get_all(dt, filters=None, fields=None, **k):
			if dt == "Distributor Order":
				return [{"name": "DO-2", "source_doctype": "Sales Invoice",
					"source_name": "SI-1", "distributor": "Sports South"}]
			if dt == "Distributor Order Line":
				return [{"parent": "DO-2", "item_code": "RSR-HOLO", "sku": "HOLO",
					"unit_cost": 13.03, "ack_qty": 5}]
			return []

		orig = frappe.get_all
		frappe.get_all = _fake_get_all
		try:
			out = sr._fetch_dropship_costs([_hdr()],
				{"SI-1": [_line(item="RSR-HOLO", qty=5, dropship=None, to_store="Sports South")]})
		finally:
			frappe.get_all = orig
		self.assertEqual(set(out), {("SI-1", "RSR-HOLO", "Sports South")})
		self.assertAlmostEqual(out[("SI-1", "RSR-HOLO", "Sports South")], 65.15, places=2)

	def test_source_keys_cover_both_links(self):
		"""The mapping production consumes: each Dynamic-Link shape → its invoice."""
		mapping = sr._dropship_source_keys([_hdr()])
		self.assertEqual(mapping[("Sales Invoice", "SI-1")], "SI-1")
		self.assertEqual(mapping[("Woo Online Order", "WOO-ORD-1")], "SI-1")

	def test_counter_invoice_has_only_its_own_key(self):
		h = _hdr()
		h["woo_online_order"] = None
		mapping = sr._dropship_source_keys([h])
		self.assertEqual(set(mapping), {("Sales Invoice", "SI-1")})
		self.assertEqual(mapping[("Sales Invoice", "SI-1")], "SI-1")


if __name__ == "__main__":
	unittest.main()
