"""Tests for the Sales Report (revenue + profit across all sales channels).

The row/cost logic lives in the pure `_build_rows` helper so it can be tested
exhaustively without seeding fully-submitted POS/Sales Invoices (which would drag
in the 4473/NICS gate, consolidation, and stock posting). A thin execute() test
covers the SQL wiring + date validation.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe.utils import flt

from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.ffl_core.report.sales_report.sales_report import (
	_build_rows, _resolve_valuation, execute)
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM


def _hdr(name, voucher_type="POS Invoice", channel="POS", date="2026-06-15",
         cust="C1", net=100.0, total=108.0, is_return=0, is_ffl_transfer=0,
         po_no=None, woo_online_order=None):
	# is_ffl_transfer (B), po_no + woo_online_order (C) are real header fields
	# _fetch_headers now carries; the pure _build_rows tests supply them directly.
	return {"name": name, "voucher_type": voucher_type, "channel": channel,
		"posting_date": date, "customer": cust,
		"base_net_total": net, "grand_total": total, "is_return": is_return,
		"is_ffl_transfer": is_ffl_transfer, "po_no": po_no,
		"woo_online_order": woo_online_order}


def _line(item, qty=1, net=100.0, serial=None, consignment_commission_pct=None):
	# frappe._dict (not a plain dict): effective_commission_pct() reads the
	# line's own consignment_commission_pct via getattr, which a plain dict
	# would silently miss (dicts don't expose keys as attributes).
	return frappe._dict({"item_code": item, "qty": qty, "base_net_amount": net,
		"serial_no": serial, "consignment_commission_pct": consignment_commission_pct})


def _data(rows):
	return [r for r in rows if not r.get("is_total")]


def _total(rows):
	return [r for r in rows if r.get("is_total")][0]


class SalesReportLogic(FrappeTestCase):
	def test_non_firearm_cost_uses_valuation_rate(self):
		rows = _build_rows([_hdr("P1", net=100.0, total=108.0)],
			{"P1": [_line("AMMO", qty=2, net=100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}, {}, "")
		r = _data(rows)[0]
		self.assertEqual(r["category"], "Ammunition")   # the item's real Item Group
		self.assertEqual(r["cost"], 60.0)        # 2 * 30
		self.assertEqual(r["net_sales"], 100.0)
		self.assertEqual(r["profit"], 40.0)
		self.assertAlmostEqual(r["margin"], 40.0)
		self.assertFalse(r.get("missing_cost"))

	def test_firearm_cost_uses_serial_purchase_rate(self):
		rows = _build_rows([_hdr("P2", net=500.0)],
			{"P2": [_line("GUN", qty=1, net=500.0, serial="SN1")]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"}}, {"SN1": 350.0}, "")
		r = _data(rows)[0]
		self.assertEqual(r["category"], "Handgun")      # the item's real Item Group
		self.assertEqual(r["cost"], 350.0)
		self.assertEqual(r["profit"], 150.0)
		self.assertFalse(r.get("missing_cost"))

	def test_firearm_missing_purchase_rate_flags_row(self):
		rows = _build_rows([_hdr("P3")],
			{"P3": [_line("GUN", qty=1, net=500.0, serial="SN-NONE")]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"}}, {}, "")
		r = _data(rows)[0]
		self.assertEqual(r["cost"], 0.0)
		self.assertTrue(r["missing_cost"])

	def test_mixed_group_invoice_is_mixed_and_sums_costs(self):
		# An invoice spanning two Item Groups shows category "Mixed".
		rows = _build_rows([_hdr("P4", net=600.0)],
			{"P4": [_line("GUN", 1, 500.0, "SN2"), _line("AMMO", 2, 100.0)]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"},
			 "AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}},
			{"SN2": 350.0}, "")
		r = _data(rows)[0]
		self.assertEqual(r["category"], "Mixed")
		self.assertEqual(r["cost"], 410.0)       # 350 + 2*30
		self.assertEqual(r["qty"], 3)

	def test_product_type_filter(self):
		# The Firearms/Non-Firearms filter keys on the item's is_firearm flag
		# (independent of the Item-Group Category column).
		headers = [_hdr("PF1"), _hdr("PF2", date="2026-06-16")]
		lines = {"PF1": [_line("GUN", 1, 500.0, "SNx")], "PF2": [_line("AMMO", 1, 50.0)]}
		meta = {"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"},
			"AMMO": {"is_firearm": 0, "valuation_rate": 20.0, "item_group": "Ammunition"}}
		sc = {"SNx": 300.0}
		fa = _data(_build_rows(headers, lines, meta, sc, "Firearms"))
		self.assertEqual({r["invoice"] for r in fa}, {"PF1"})
		non = _data(_build_rows(headers, lines, meta, sc, "Non-Firearms"))
		self.assertEqual({r["invoice"] for r in non}, {"PF2"})

	def test_totals_row(self):
		rows = _build_rows(
			[_hdr("T1", net=100.0, total=100.0),
			 _hdr("T2", date="2026-06-16", net=200.0, total=200.0)],
			{"T1": [_line("AMMO", 1, 100.0)], "T2": [_line("AMMO", 1, 200.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 50.0}}, {}, "")
		t = _total(rows)
		self.assertEqual(t["net_sales"], 300.0)
		self.assertEqual(t["cost"], 100.0)       # 50 + 50
		self.assertEqual(t["profit"], 200.0)
		self.assertAlmostEqual(t["margin"], 200.0 / 300.0 * 100)

	def test_return_nets_down_totals(self):
		rows = _build_rows(
			[_hdr("S1", net=100.0, total=100.0),
			 _hdr("R1", date="2026-06-16", net=-100.0, total=-100.0, is_return=1)],
			{"S1": [_line("AMMO", 1, 100.0)], "R1": [_line("AMMO", -1, -100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 40.0}}, {}, "")
		t = _total(rows)
		self.assertEqual(t["net_sales"], 0.0)    # 100 + (-100)
		self.assertEqual(t["cost"], 0.0)         # 40 + (-40), signed qty
		self.assertEqual(t["profit"], 0.0)

	def test_trade_in_reports_gross_net_sales_and_goods_cost(self):
		# A TRADE-IN-CREDIT line is non-cash consideration for a gun taken INTO
		# inventory (its allowance == that gun's acquisition cost) — neither
		# negative revenue nor a cost of THIS sale.
		# The row reports the sale GROSS: Net Sales = the gun's full price and
		# Cost = the gun's own COGS; the allowance appears on neither side, and
		# is realized as the traded-in gun's COGS when IT later sells. Real
		# repro: prod a past invoice — an M1 Garand sold at 1330.89 net
		# (serial cost 1128.59) with an 869 trade-in shows 1330.89 / 1128.59 /
		# +202.30 — not the old net-method 461.89 / 259.59, and never a
		# -666.70 phantom loss.
		rows = _build_rows(
			[_hdr("TI1", net=461.89, total=500.0)],
			{"TI1": [
				_line("M1-30", qty=1, net=1330.89, serial="SN-GARAND"),
				_line(TRADE_IN_CREDIT_ITEM, qty=1, net=-869.0),
			]},
			{"M1-30": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			 TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0.0,
				"item_group": "Misc Accessory"}},
			{"SN-GARAND": 1128.59}, "")
		r = _data(rows)[0]
		self.assertAlmostEqual(r["net_sales"], 1330.89)  # gross of the credit
		self.assertAlmostEqual(r["cost"], 1128.59)       # the gun's own COGS, no wash
		self.assertAlmostEqual(r["profit"], 202.30)      # unchanged by the gross-up
		self.assertAlmostEqual(r["margin"], 202.30 / 1330.89 * 100)
		# The Total column stays grand_total — what was actually charged after
		# the credit — so this is the one row shape where net_sales > total. The
		# data-only keys below let the FE mark the cell and name the allowance
		# (the exact figure is NOT derivable from the 13 columns alone: the gap
		# is allowance − tax, and tax is not an Order-view column).
		self.assertAlmostEqual(r["total"], 500.0)
		self.assertTrue(r["is_trade_in"])
		self.assertAlmostEqual(r["trade_in_net"], -869.0)
		self.assertEqual(r["qty"], 2)
		self.assertFalse(r.get("missing_cost"))
		# The Total row totals the same gross figures.
		total = rows[-1]
		self.assertTrue(total.get("is_total"))
		self.assertAlmostEqual(total["net_sales"], 1330.89)
		self.assertAlmostEqual(total["cost"], 1128.59)
		self.assertAlmostEqual(total["margin"], 202.30 / 1330.89 * 100)

	def test_trade_in_reversed_on_return_mirrors_gross(self):
		# A return mirrors the credit as a POSITIVE row (compliance exempts
		# returns); the gross-up must mirror the sign too, so the reversal nets
		# to an exact mirror of the original sale rather than a phantom gain.
		rows = _build_rows(
			[_hdr("TR1", net=-461.89, total=-500.0, is_return=1)],
			{"TR1": [
				_line("M1-30", qty=-1, net=-1330.89, serial="SN-GARAND"),
				_line(TRADE_IN_CREDIT_ITEM, qty=-1, net=869.0),
			]},
			{"M1-30": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			 TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0.0,
				"item_group": "Misc Accessory"}},
			{"SN-GARAND": 1128.59}, "")
		r = _data(rows)[0]
		self.assertAlmostEqual(r["net_sales"], -1330.89)  # -461.89 net - 869 add-back
		self.assertAlmostEqual(r["cost"], -1128.59)       # signed qty reverses the COGS
		self.assertAlmostEqual(r["profit"], -202.30)      # exact reversal of the sale
		self.assertTrue(r["is_trade_in"])                 # mirrored credit still marks
		self.assertAlmostEqual(r["trade_in_net"], 869.0)

	def test_consignment_cost_is_net_minus_commission(self):
		rows = _build_rows(
			[_hdr("CN1", net=1000.0, total=1082.5)],
			{"CN1": [_line("GUN", qty=1, net=1000.0, serial="CS1")]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}},
			{"CS1": 0.0}, "",
			consignment={"CS1": {"is_consignment": 1, "commission_pct": 20.0}})
		r = _data(rows)[0]
		self.assertAlmostEqual(r["cost"], 800.0)    # 1000 * (1 - 0.20)
		self.assertAlmostEqual(r["profit"], 200.0)  # commission
		self.assertFalse(r.get("missing_cost"))

	def test_consignment_line_pct_overrides_serial_default(self):
		# The line's own frozen consignment_commission_pct (15) wins over the
		# Serial No's default (20) — mirrors ffl_core.consignment.effective_commission_pct's
		# priority rule.
		rows = _build_rows(
			[_hdr("CN2", net=1000.0, total=1082.5)],
			{"CN2": [_line("GUN", qty=1, net=1000.0, serial="CS2",
				consignment_commission_pct=15)]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}},
			{"CS2": 0.0}, "",
			consignment={"CS2": {"is_consignment": 1, "commission_pct": 20.0}})
		r = _data(rows)[0]
		self.assertAlmostEqual(r["cost"], 850.0)    # 1000 * (1 - 0.15)
		self.assertAlmostEqual(r["profit"], 150.0)  # commission
		self.assertFalse(r.get("missing_cost"))

	def test_consignment_line_zero_pct_falls_back_to_serial(self):
		# A consignment gun sold BEFORE this feature (or a retro-flagged one) has
		# a frozen line % of 0 — the field default, not a real 0% override.
		# ERPNext locks that % on the submitted invoice, so the report must fall
		# back to the Serial No's rate; otherwise cost=net/profit 0 while the
		# (corrected) GL carries the commission. Real repro: M1 D Day, sold 2600
		# net, retro-set to 7% consignment → cost 2418, profit 182 (not 2600/0).
		rows = _build_rows(
			[_hdr("MC1", net=2600.0, total=2814.5)],
			{"MC1": [_line("M1", qty=1, net=2600.0, serial="MS1",
				consignment_commission_pct=0)]},
			{"M1": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}},
			{"MS1": 2600.0}, "",
			consignment={"MS1": {"is_consignment": 1, "commission_pct": 7.0}})
		r = _data(rows)[0]
		self.assertAlmostEqual(r["cost"], 2418.0)    # 2600 * (1 - 0.07), NOT 2600
		self.assertAlmostEqual(r["profit"], 182.0)   # commission, NOT 0
		self.assertFalse(r.get("missing_cost"))

	# ------------------------------------------------------------ B: FFL transfer
	def test_ffl_transfer_net_zero_firearm_not_flagged_missing_cost(self):
		# B: an FFL transfer (is_ffl_transfer header) processes a customer's OWN
		# gun — the firearm line is net 0 and the store never bought it, so a 0
		# purchase_rate is EXPECTED, NOT a missing-cost data gap. Profit = the
		# transfer fee line. Real false-alarm repro: a past invoice.
		rows = _build_rows(
			[_hdr("XF1", net=25.0, total=27.06, is_ffl_transfer=1)],
			{"XF1": [
				_line("XFER-GUN", qty=1, net=0.0, serial="CUST-OWN-SN"),
				_line("XFER-FEE", qty=1, net=25.0)]},
			{"XFER-GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"},
			 "XFER-FEE": {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Services"}},
			{}, "")   # empty serial_cost → the transfer gun's rate is 0
		r = _data(rows)[0]
		self.assertFalse(r.get("missing_cost"))   # net-0 transfer gun NOT flagged
		self.assertEqual(r["cost"], 0.0)
		self.assertEqual(r["profit"], 25.0)        # = the fee, no phantom missing

	def test_non_transfer_net_zero_firearm_still_flags_missing_cost(self):
		# Guard: the exemption is keyed on is_ffl_transfer — a net-0 firearm line
		# on an ORDINARY invoice (no transfer flag) with no purchase_rate must
		# STILL flag, so the exemption can't be triggered by net 0 alone.
		rows = _build_rows(
			[_hdr("NX1", net=0.0, total=0.0, is_ffl_transfer=0)],
			{"NX1": [_line("GUN", qty=1, net=0.0, serial="NOCOST-SN")]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"}},
			{}, "")
		self.assertTrue(_data(rows)[0]["missing_cost"])

	# --------------------------------------- C: order_ref / serials / …
	def test_order_row_carries_order_ref_and_serials(self):
		# C: the Order row exposes order_ref (Woo storefront #) + serials
		# (invoice serials "; "-joined), so two same-price same-item real orders
		# are distinguishable. Real repro: a past invoice.
		rows = _build_rows(
			[_hdr("W120", voucher_type="Sales Invoice", net=578.99, total=626.76,
				po_no="2134", woo_online_order="WOO-ORD-01122")],
			{"W120": [_line("CZ75", qty=1, net=578.99, serial="F7550")]},
			{"CZ75": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}},
			{"F7550": 446.80}, "")
		r = _data(rows)[0]
		self.assertEqual(r.get("order_ref"), "2134")   # bare value; FE renders "#2134"
		self.assertEqual(r.get("serials"), "F7550")

	def test_order_ref_falls_back_to_woo_online_order(self):
		# order_ref = po_no or woo_online_order or "": a web SI with no po_no still
		# shows the WOO-ORD value rather than blank.
		rows = _build_rows(
			[_hdr("W2", voucher_type="Sales Invoice", net=100.0, total=108.0,
				po_no=None, woo_online_order="WOO-ORD-9")],
			{"W2": [_line("AMMO", qty=1, net=100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}, {}, "")
		self.assertEqual(_data(rows)[0].get("order_ref"), "WOO-ORD-9")

	def test_order_row_multi_serial_semicolon_joined(self):
		# Serials across all of the invoice's lines are aggregated, "; "-joined.
		rows = _build_rows(
			[_hdr("M1", net=1000.0, total=1082.5)],
			{"M1": [
				_line("GUN-A", qty=1, net=500.0, serial="SNA"),
				_line("GUN-B", qty=1, net=500.0, serial="SNB")]},
			{"GUN-A": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			 "GUN-B": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}},
			{"SNA": 300.0, "SNB": 300.0}, "")
		self.assertEqual(_data(rows)[0].get("serials"), "SNA; SNB")

	def test_order_row_pos_sale_has_blank_order_ref(self):
		# A POS counter sale has no Woo order: order_ref is "" (FE renders "—");
		# serials is "" when the line carries none.
		rows = _build_rows(
			[_hdr("P1", net=100.0, total=108.0)],
			{"P1": [_line("AMMO", qty=2, net=100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}, {}, "")
		r = _data(rows)[0]
		self.assertEqual(r.get("order_ref"), "")
		self.assertEqual(r.get("serials"), "")

	def test_order_row_channel_is_three_way(self):
		# C: Order view channel now uses _channel() → POS/Web/Manual
		# (was the literal "Order" for every direct SI), matching the Product view.
		headers = [
			_hdr("PC", voucher_type="POS Invoice", channel="POS"),
			_hdr("WC", voucher_type="Sales Invoice", channel="Order", woo_online_order="WOO-1"),
			_hdr("BC", voucher_type="Sales Invoice", channel="Order", woo_online_order=None)]
		lines = {"PC": [_line("A", 1, 100)], "WC": [_line("A", 1, 100)], "BC": [_line("A", 1, 100)]}
		meta = {"A": {"is_firearm": 0, "valuation_rate": 10.0, "item_group": "Misc"}}
		ch = {r["invoice"]: r["channel"] for r in _data(_build_rows(headers, lines, meta, {}, ""))}
		self.assertEqual(ch["PC"], "POS")
		self.assertEqual(ch["WC"], "Web")
		self.assertEqual(ch["BC"], "Manual")

	def test_order_row_category_detail_lists_groups(self):
		# C: category_detail = sorted item groups ", "-joined — the Mixed
		# tooltip source. A single-group invoice fills it with that one group too.
		rows = _build_rows(
			[_hdr("MX", net=600.0), _hdr("SG", date="2026-06-16", net=100.0)],
			{"MX": [_line("GUN", 1, 500.0, "SN2"), _line("AMMO", 2, 100.0)],
			 "SG": [_line("AMMO", 1, 100.0)]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"},
			 "AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}},
			{"SN2": 350.0}, "")
		by = {r["invoice"]: r for r in _data(rows)}
		self.assertEqual(by["MX"]["category"], "Mixed")
		self.assertEqual(by["MX"].get("category_detail"), "Ammunition, Handgun")   # sorted
		self.assertEqual(by["SG"].get("category_detail"), "Ammunition")

	def test_order_row_is_consignment_flag(self):
		# C: Order row carries is_consignment so the ▲ profit marker
		# can render in the Order view (any consigned line flags the row).
		rows = _build_rows(
			[_hdr("CN", net=1000.0, total=1082.5), _hdr("PL", date="2026-06-16", net=500.0)],
			{"CN": [_line("GUN", 1, 1000.0, "CS1")], "PL": [_line("GUN2", 1, 500.0, "PS1")]},
			{"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"},
			 "GUN2": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}},
			{"CS1": 0.0, "PS1": 350.0}, "",
			consignment={"CS1": {"is_consignment": 1, "commission_pct": 20.0}})
		by = {r["invoice"]: r for r in _data(rows)}
		self.assertTrue(by["CN"].get("is_consignment"))
		self.assertFalse(by["PL"].get("is_consignment"))


def _bin(qty, stock_value, rate):
	return {"actual_qty": qty, "stock_value": stock_value, "valuation_rate": rate}


class SalesReportPickupTicket(FrappeTestCase):
	"""A counter pickup's 4473 ticket (api/counter_pickup) is a $0 POS Invoice
	that hands a gun over; the sale is the counter order's own line. The header
	fetch must leave the ticket out, or the day reads one gun sold twice — once
	at its price, once free."""

	def _pos_filters(self, has_column):
		from unittest.mock import patch
		from ffl_core.ffl_core.report.sales_report import sales_report as sr
		seen = {}

		def get_all(doctype, filters=None, fields=None, **kw):
			seen[doctype] = filters
			return []
		with patch.object(sr.frappe, "get_all", side_effect=get_all), \
				patch.object(sr, "_has_column", lambda dt, f: has_column if f == "ffl_counter_order" else False):
			sr._fetch_headers(frappe._dict(from_date="2026-09-01", to_date="2026-09-30"))
		return seen["POS Invoice"]

	def test_the_ticket_is_left_out_of_the_pos_headers(self):
		self.assertEqual(self._pos_filters(True).get("ffl_counter_order"), ("is", "not set"))

	def test_a_site_without_the_column_is_unaffected(self):
		self.assertNotIn("ffl_counter_order", self._pos_filters(False))


class SalesReportValuationSource(FrappeTestCase):
	"""_resolve_valuation is the fix for RSR accessories: the Item-master
	valuation_rate is 0 (stock is received into the Bin at real cost, but the
	master field is never synced), so a non-firearm line was costed at 0 and
	showed a phantom 100% margin. Cost must come from the Bin's moving average,
	falling back to the master rate only when there's no usable Bin figure."""

	def test_in_stock_uses_bin_weighted_average_over_zero_master(self):
		# HUX1574 repro: master rate 0, Bin holds the real $97.20 cost.
		self.assertAlmostEqual(
			_resolve_valuation([_bin(4, 388.8, 97.2)], master_rate=0.0), 97.2)

	def test_sold_out_uses_persisted_bin_rate(self):
		# Qty 0 (all sold) can't be weight-averaged, but the Bin retains the last
		# moving-average rate — use it rather than the 0 master field.
		self.assertAlmostEqual(
			_resolve_valuation([_bin(0, 0.0, 97.2)], master_rate=0.0), 97.2)

	def test_weighted_average_across_warehouses(self):
		# 2 @ 30 + 3 @ 40 → 180/5 = 36, not a naive mean of the rates.
		self.assertAlmostEqual(
			_resolve_valuation([_bin(2, 60.0, 30.0), _bin(3, 120.0, 40.0)],
				master_rate=0.0), 36.0)

	def test_no_bin_falls_back_to_master_rate(self):
		# Item never stocked (no Bin) — the master rate is all we have.
		self.assertAlmostEqual(_resolve_valuation([], master_rate=12.5), 12.5)

	def test_all_zero_bin_falls_back_to_master_rate(self):
		self.assertAlmostEqual(
			_resolve_valuation([_bin(0, 0.0, 0.0)], master_rate=15.0), 15.0)


class SalesReportExecute(FrappeTestCase):
	def test_execute_returns_columns_and_list(self):
		cols, data, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30"})
		self.assertTrue(any(c["fieldname"] == "profit" for c in cols))
		self.assertIsInstance(data, list)

	def test_execute_validates_date_order(self):
		with self.assertRaises(frappe.ValidationError):
			execute({"from_date": "2026-06-30", "to_date": "2026-06-01"})

	def test_execute_returns_native_five_tuple(self):
		# execute() returns Frappe's native
		# (columns, data, message, chart, report_summary). chart=None (deferred);
		# report_summary is the period summary-card list.
		res = execute({"from_date": "2026-06-01", "to_date": "2026-06-30"})
		self.assertEqual(len(res), 5)
		columns, data, message, chart, report_summary = res
		self.assertIsInstance(columns, list)
		self.assertIsInstance(data, list)
		self.assertIsNone(chart)                       # chart is deferred (backlog)
		self.assertIsInstance(report_summary, list)    # the summary cards


# =====================================================================
# Product-level view (tax report)
# =====================================================================

# Prod accounts use a store abbreviation suffix such as "- TSA"; dev company is "- FT". The report classifies by the
# account's type (Tax / Income root), NOT by these literal strings — but the
# pure builders take a pre-computed {account_head: class} map, so the tests use
# readable prod-style names.
SALES_TAX_ACC = "Sales Tax Payable - TSA"
SHIP_ACC = "Shipping Income - TSA"
ACCT_CLASS = {SALES_TAX_ACC: "sales_tax", SHIP_ACC: "shipping"}


def _phdr(name, voucher_type="Sales Invoice", date="2026-06-15", cust="C1",
          net=100.0, total=108.0, woo_online_order=None, exempt=0, is_return=0,
          is_ffl_transfer=0, po_no=None):
	channel = "POS" if voucher_type == "POS Invoice" else "Order"
	return {"name": name, "voucher_type": voucher_type, "channel": channel,
		"posting_date": date, "customer": cust, "base_net_total": net,
		"grand_total": total, "is_return": is_return,
		"woo_online_order": woo_online_order, "exempt_from_sales_tax": exempt,
		"is_ffl_transfer": is_ffl_transfer, "po_no": po_no}


def _pline(name, item, qty=1, net=100.0, serial=None, item_name=None,
           net_rate=None, price_list_rate=None, discount_amount=0.0,
           consignment_commission_pct=None):
	# The item row's own `name` is the key IWTD.item_row points at.
	return frappe._dict({"name": name, "item_code": item, "qty": qty,
		"base_net_amount": net, "net_amount": net,
		"base_net_rate": net_rate if net_rate is not None else net,
		"price_list_rate": price_list_rate, "discount_amount": discount_amount,
		"serial_no": serial, "item_name": item_name or item,
		"consignment_commission_pct": consignment_commission_pct})


def _iwt(item_row, tax_row, amount, taxable=0.0):
	return frappe._dict({"item_row": item_row, "tax_row": tax_row,
		"amount": amount, "taxable_amount": taxable})


def _taxrow(name, account_head, base_tax_amount, after=None, charge_type=None,
            description=None):
	# base_tax_amount is a pre-discount denormalized field; the report reads the
	# post-discount `base_tax_amount_after_discount_amount`. `after` defaults to
	# the same value (no grand-total discount) so existing fixtures are unaffected.
	# charge_type/description default to None (legacy fixtures don't care) — the
	# freight-tax subclassification keys on both, so its fixtures set them.
	return frappe._dict({"name": name, "account_head": account_head,
		"base_tax_amount": base_tax_amount,
		"base_tax_amount_after_discount_amount": base_tax_amount if after is None else after,
		"charge_type": charge_type, "description": description})


class SalesReportAccountClassification(FrappeTestCase):
	"""Positive classification against the company's OWN canonical accounts:
	shipping is exactly the company shipping-income account, sales tax is exactly
	the company sales-tax template's account_heads; everything else fails closed.
	NOT by account TYPE — which would fold a Tax-type FET into sales tax, or an
	Income-type handling fee into shipping (fail-open)."""

	SHIP = "Shipping Income - TSA"
	TAXSET = {"Sales Tax Payable - TSA"}

	def test_shipping_account_matches(self):
		self.assertEqual(sr._classify_account(self.SHIP, self.SHIP, self.TAXSET), "shipping")

	def test_sales_tax_account_matches(self):
		self.assertEqual(
			sr._classify_account("Sales Tax Payable - TSA", self.SHIP, self.TAXSET), "sales_tax")

	def test_income_non_shipping_charge_is_unclassified(self):
		# An Income-root charge that is NOT the shipping account (handling /
		# restocking / gift-wrap income) must fail closed, not be swept to shipping.
		self.assertEqual(
			sr._classify_account("Restocking Fee Income - TSA", self.SHIP, self.TAXSET), "unclassified")

	def test_tax_non_sales_tax_account_is_unclassified(self):
		# A Tax-type account that is NOT a sales-tax account (e.g. federal excise
		# tax on NFA items) must NOT pollute collected sales tax.
		self.assertEqual(
			sr._classify_account("Federal Excise Tax - TSA", self.SHIP, self.TAXSET), "unclassified")

	def test_unknown_account_is_unclassified(self):
		self.assertEqual(
			sr._classify_account("Misc Payable - TSA", self.SHIP, self.TAXSET), "unclassified")


class SalesReportTaxBreakdown(FrappeTestCase):
	"""Row-level tax comes from the item_wise_tax_details child table
	(item_row→tax_row→account_head), never re-prorated by net share."""

	def test_per_item_sales_tax_attribution_a(self):
		taxes = [_taxrow("t1", SALES_TAX_ACC, 115.50)]
		iwt = [_iwt("r_sks", "t1", 74.25), _iwt("r_sw", "t1", 41.25)]
		b = sr._tax_breakdown(taxes, iwt, ACCT_CLASS)
		self.assertAlmostEqual(b["line_tax"]["r_sks"], 74.25)
		self.assertAlmostEqual(b["line_tax"]["r_sw"], 41.25)
		self.assertAlmostEqual(b["sales_tax_total"], 115.50)
		self.assertAlmostEqual(b["shipping_total"], 0.0)
		self.assertEqual(b["unclassified"], [])

	def test_shipping_separated_from_sales_tax_b(self):
		# 00060: Total Taxes = 45 is ALL shipping (Shipping Income), sales tax 0.
		taxes = [_taxrow("t1", SHIP_ACC, 45.00)]
		iwt = [_iwt("r_carcano", "t1", 45.00)]
		b = sr._tax_breakdown(taxes, iwt, ACCT_CLASS)
		self.assertAlmostEqual(b["shipping_total"], 45.00)
		self.assertAlmostEqual(b["sales_tax_total"], 0.0)
		# shipping IWTD is NOT attributed to a product line's sales tax.
		self.assertNotIn("r_carcano", b["line_tax"])

	def test_trade_in_negative_line_tax_c(self):
		# 00023: the trade-in credit row carries negative sales tax (old net method).
		taxes = [_taxrow("t1", SALES_TAX_ACC, 38.11)]
		iwt = [_iwt("r_m1", "t1", 109.80), _iwt("r_ti", "t1", -71.69)]
		b = sr._tax_breakdown(taxes, iwt, ACCT_CLASS)
		self.assertAlmostEqual(b["line_tax"]["r_m1"], 109.80)
		self.assertAlmostEqual(b["line_tax"]["r_ti"], -71.69)
		self.assertAlmostEqual(b["sales_tax_total"], 38.11)

	def test_unknown_charge_is_unclassified_not_tax_f1(self):
		taxes = [_taxrow("t1", SALES_TAX_ACC, 10.0), _taxrow("t2", "Excise Duty - TSA", 5.0)]
		iwt = [_iwt("r1", "t1", 10.0), _iwt("r1", "t2", 5.0)]
		acct = {SALES_TAX_ACC: "sales_tax"}  # Excise unmapped → unclassified
		b = sr._tax_breakdown(taxes, iwt, acct)
		self.assertAlmostEqual(b["sales_tax_total"], 10.0)
		self.assertAlmostEqual(b["line_tax"]["r1"], 10.0)  # only the sales-tax IWTD
		self.assertEqual(len(b["unclassified"]), 1)
		self.assertEqual(b["unclassified"][0]["account_head"], "Excise Duty - TSA")
		self.assertAlmostEqual(b["unclassified"][0]["amount"], 5.0)


class SalesReportFreightTax227(FrappeTestCase):
	"""The POS freight-tax rows compliance.shipping_charge appends
	are Actual rows on the sales-tax account with the fixed "Shipping Tax"
	description prefix and NO item_wise_tax_details (dont_recompute_tax). The
	report must (a) count them into Sales Tax so the card ties to the GL account
	aggregation, and (b) exempt exactly them from the IWTD-vs-charged mismatch
	probe — a tampered goods-tax row or a manually keyed Actual tax row must
	still flag."""

	# Real figures: goods 664.00 → tax 54.78, shipping 45.00 → freight tax 3.71.
	def _p5_taxes(self, goods_charged=54.78):
		from ffl_core.compliance.shipping_charge import SHIPPING_TAX_DESCRIPTION
		return [
			_taxrow("t1", SALES_TAX_ACC, goods_charged, charge_type="On Net Total"),
			_taxrow("t2", SHIP_ACC, 45.00, charge_type="Actual", description="Shipping"),
			_taxrow("t3", SALES_TAX_ACC, 3.71, charge_type="Actual",
				description=f"{SHIPPING_TAX_DESCRIPTION} (8.25%)"),
		]

	# The freight row deliberately has NO IWTD; the shipping row's allocation is
	# ignored anyway (order-level).
	def _p5_iwt(self):
		return [_iwt("r_g", "t1", 54.78), _iwt("r_g", "t2", 45.00)]

	def test_freight_tax_joins_sales_tax_and_skips_mismatch(self):
		b = sr._tax_breakdown(self._p5_taxes(), self._p5_iwt(), ACCT_CLASS)
		self.assertAlmostEqual(b["freight_tax_total"], 3.71)
		# Card basis = goods IWTD + freight tax == GL account aggregation (58.49).
		self.assertAlmostEqual(b["sales_tax_total"], 58.49)
		self.assertAlmostEqual(b["shipping_total"], 45.00)
		# line_tax stays goods-only (freight tax is order-level, like shipping).
		self.assertAlmostEqual(b["line_tax"]["r_g"], 54.78)
		self.assertFalse(b["tax_mismatch"])
		self.assertEqual(b["unclassified"], [])

	def test_tampered_goods_tax_still_flags(self):
		# Goods row charged 60.00 while Σ IWTD is 54.78: the freight exemption
		# must not mask a genuine goods-tax divergence.
		b = sr._tax_breakdown(self._p5_taxes(goods_charged=60.00), self._p5_iwt(), ACCT_CLASS)
		self.assertTrue(b["tax_mismatch"])

	def test_manual_actual_tax_row_without_prefix_still_flags(self):
		# A manually keyed Actual tax on the sales-tax account (no IWTD, no
		# "Shipping Tax" prefix) is the original genuine anomaly the probe exists
		# for — blanket-Actual exemption would be a hole.
		taxes = [
			_taxrow("t1", SALES_TAX_ACC, 54.78, charge_type="On Net Total"),
			_taxrow("t2", SALES_TAX_ACC, 5.00, charge_type="Actual",
				description="Manually keyed tax"),
		]
		b = sr._tax_breakdown(taxes, [_iwt("r_g", "t1", 54.78)], ACCT_CLASS)
		self.assertTrue(b["tax_mismatch"])
		self.assertAlmostEqual(b["freight_tax_total"], 0.0)

	def test_woo_mirror_actual_row_with_iwtd_stays_clean(self):
		# Web-order SIs mirror the Woo-collected tax as an Actual row WITH an
		# item-wise allocation and no "Shipping Tax" prefix — unchanged by the freight-tax rows.
		taxes = [_taxrow("t1", SALES_TAX_ACC, 9.08, charge_type="Actual",
			description="Sales Tax (WooCommerce)")]
		b = sr._tax_breakdown(taxes, [_iwt("r_w", "t1", 9.08)], ACCT_CLASS)
		self.assertFalse(b["tax_mismatch"])
		self.assertAlmostEqual(b["sales_tax_total"], 9.08)
		self.assertAlmostEqual(b["freight_tax_total"], 0.0)

	def test_pre_225_invoice_unchanged(self):
		# Legacy shape (a past invoice): goods tax + shipping, no freight-tax
		# row — breakdown identical to before the freight-tax rows existed.
		taxes = [
			_taxrow("t1", SALES_TAX_ACC, 54.78, charge_type="On Net Total"),
			_taxrow("t2", SHIP_ACC, 45.00, charge_type="Actual", description="Shipping"),
		]
		b = sr._tax_breakdown(taxes, self._p5_iwt(), ACCT_CLASS)
		self.assertAlmostEqual(b["sales_tax_total"], 54.78)
		self.assertAlmostEqual(b["freight_tax_total"], 0.0)
		self.assertFalse(b["tax_mismatch"])

	def _p5_rows(self, detail=False):
		headers = [_phdr("FT1", net=664.0, total=767.49)]
		lines = {"FT1": [_pline("r_g", "SKS", qty=1, net=664.0)]}
		meta = {"SKS": {"is_firearm": 0, "valuation_rate": 400.0, "item_group": "Rifle"}}
		return sr._build_product_rows(headers, lines, meta, {}, {},
			{"FT1": self._p5_iwt()}, {"FT1": self._p5_taxes()}, ACCT_CLASS, "", detail)

	def test_shipping_row_carries_freight_tax_and_card_ties_to_gl(self):
		rows = self._p5_rows()
		ship = next(r for r in rows if r.get("row_kind") == "shipping")
		self.assertAlmostEqual(ship["line_tax"], 3.71)
		total = _total(rows)
		# Total (== the Sales Tax card) ties to the GL account aggregation, and
		# the flat view's Line Tax column sums to the same figure (54.78 + 3.71).
		self.assertAlmostEqual(total["sales_tax"], 58.49)
		self.assertAlmostEqual(total["line_tax"], 58.49)
		self.assertAlmostEqual(
			sum(flt(r.get("line_tax")) for r in _data(rows)), 58.49)
		self.assertFalse(any(r.get("tax_mismatch") for r in rows))
		# Per-order reconciliation: net + shipping + sales tax == grand_total.
		self.assertAlmostEqual(
			total["net_amount"] + total["shipping"] + total["sales_tax"], 767.49)
		card = next(c for c in sr._report_summary("Product", rows)
			if c.get("label") == "Sales Tax")
		self.assertAlmostEqual(card["value"], 58.49)
		flags = next(c for c in sr._report_summary("Product", rows)
			if c.get("label") == "Data Flags")
		self.assertEqual(flags["indicator"], "Green")

	def test_order_detail_parent_row_includes_freight_tax(self):
		rows = self._p5_rows(detail=True)
		order = next(r for r in rows if r.get("row_kind") == "order")
		self.assertAlmostEqual(order["sales_tax"], 58.49)
		self.assertFalse(order["tax_mismatch"])

	def test_a11_tax_inclusive_shipping_reports_the_net_and_its_tax(self):
		"""Tax-inclusive shipping (order: 899 goods, S=45 including its
		tax): Shipping 41.57, its Line Tax 3.43, Sales Tax card 77.60, no
		mismatch — and the month-end check 77.60 / 8.25% vs 899 + 41.57 holds."""
		from ffl_core.compliance.shipping_charge import SHIPPING_TAX_DESCRIPTION
		taxes = [
			_taxrow("t1", SALES_TAX_ACC, 74.17, charge_type="On Net Total"),
			_taxrow("t2", SHIP_ACC, 41.57, charge_type="Actual", description="Shipping"),
			_taxrow("t3", SALES_TAX_ACC, 3.43, charge_type="Actual",
				description=f"{SHIPPING_TAX_DESCRIPTION} (8.25%)"),
		]
		iwt = [_iwt("r_g", "t1", 74.17), _iwt("r_g", "t2", 41.57)]
		rows = sr._build_product_rows([_phdr("A11", net=899.0, total=1018.17)],
			{"A11": [_pline("r_g", "SKS", qty=1, net=899.0)]},
			{"SKS": {"is_firearm": 0, "valuation_rate": 400.0, "item_group": "Rifle"}},
			{}, {}, {"A11": iwt}, {"A11": taxes}, ACCT_CLASS, "", False)
		ship = next(r for r in rows if r.get("row_kind") == "shipping")
		self.assertAlmostEqual(ship["shipping"], 41.57)
		self.assertAlmostEqual(ship["line_tax"], 3.43)
		self.assertFalse(any(r.get("tax_mismatch") for r in rows))
		card = next(c for c in sr._report_summary("Product", rows)
			if c.get("label") == "Sales Tax")
		self.assertAlmostEqual(card["value"], 77.60)
		total = _total(rows)
		self.assertAlmostEqual(
			total["net_amount"] + total["shipping"] + total["sales_tax"], 1018.17)
		self.assertLessEqual(abs(77.60 / 0.0825 - (899 + 41.57)), 0.06)


class SalesReportProductRows(FrappeTestCase):
	def _rows(self, headers, lines, meta, sc, iwt, taxes, acct=None,
	          consignment=None, detail=False, product_type=""):
		return sr._build_product_rows(headers, lines, meta, sc, consignment or {},
			iwt, taxes, acct or ACCT_CLASS, product_type, detail)

	def _recon(self, rows, invoice, grand_total):
		inv = [r for r in rows if r.get("invoice") == invoice]
		net = sum(flt(r.get("net_amount")) for r in inv if r.get("row_kind") == "product")
		ship = sum(flt(r.get("shipping")) for r in inv if r.get("row_kind") == "shipping")
		tax = sum(flt(r.get("line_tax")) for r in inv if r.get("row_kind") == "product")
		uncl = sum(flt(r.get("unclassified")) for r in inv if r.get("row_kind") == "unclassified")
		self.assertAlmostEqual(net + ship + tax + uncl, grand_total, places=2)

	def test_counter_double_gun_a1(self):
		h = _phdr("P6", voucher_type="POS Invoice", net=1400.0, total=1515.50)
		lines = {"P6": [
			_pline("r_sks", "SKS-762X39", 1, 900.0, serial="SKSSN", item_name="SKS 7.62x39"),
			_pline("r_sw", "SW-1000", 1, 500.0, serial="SWSN", item_name="S&W Model 1000")]}
		meta = {"SKS-762X39": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			"SW-1000": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Shotgun"}}
		sc = {"SKSSN": 600.0, "SWSN": 300.0}
		taxes = {"P6": [_taxrow("t1", SALES_TAX_ACC, 115.50)]}
		iwt = {"P6": [_iwt("r_sks", "t1", 74.25), _iwt("r_sw", "t1", 41.25)]}
		rows = self._rows([h], lines, meta, sc, iwt, taxes)
		prod = [r for r in rows if r.get("row_kind") == "product"]
		self.assertEqual(len(prod), 2)
		sks = next(r for r in prod if r["item_code"] == "SKS-762X39")
		self.assertAlmostEqual(sks["net_amount"], 900.0)
		self.assertAlmostEqual(sks["line_tax"], 74.25)
		self.assertAlmostEqual(sks["cost"], 600.0)
		self.assertEqual(sks["channel"], "POS")
		self.assertTrue(sks["is_firearm"])
		self.assertFalse([r for r in rows if r.get("row_kind") == "shipping"])
		self._recon(rows, "P6", 1515.50)

	def test_web_order_shipping_b(self):
		h = _phdr("S60", net=219.0, total=264.0, woo_online_order="WC-60")
		lines = {"S60": [_pline("r_c", "CARCANO", 1, 219.0, serial="CARSN", item_name="Carcano M91")]}
		meta = {"CARCANO": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}}
		sc = {"CARSN": 150.0}
		taxes = {"S60": [_taxrow("t1", SHIP_ACC, 45.0)]}
		iwt = {"S60": [_iwt("r_c", "t1", 45.0)]}
		rows = self._rows([h], lines, meta, sc, iwt, taxes)
		prod = [r for r in rows if r.get("row_kind") == "product"]
		self.assertAlmostEqual(prod[0]["net_amount"], 219.0)
		self.assertAlmostEqual(prod[0]["line_tax"], 0.0)  # shipping is NOT this line's tax
		self.assertEqual(prod[0]["channel"], "Web")
		ship = [r for r in rows if r.get("row_kind") == "shipping"]
		self.assertEqual(len(ship), 1)
		self.assertAlmostEqual(ship[0]["shipping"], 45.0)
		# 45 never appears as a sales-tax figure anywhere.
		self.assertFalse(any(flt(r.get("line_tax")) == 45.0 for r in rows))
		self._recon(rows, "S60", 264.0)

	def test_trade_in_split_c(self):
		h = _phdr("P23", voucher_type="POS Invoice", net=461.89, total=500.0)
		lines = {"P23": [
			_pline("r_m1", "M1-30", 1, 1330.89, serial="GARSN", item_name="M1 Garand"),
			_pline("r_ti", TRADE_IN_CREDIT_ITEM, 1, -869.0, item_name="Trade-in credit")]}
		meta = {"M1-30": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Misc Accessory"}}
		sc = {"GARSN": 1128.59}
		taxes = {"P23": [_taxrow("t1", SALES_TAX_ACC, 38.11)]}
		iwt = {"P23": [_iwt("r_m1", "t1", 109.80), _iwt("r_ti", "t1", -71.69)]}
		rows = self._rows([h], lines, meta, sc, iwt, taxes)
		m1 = next(r for r in rows if r.get("item_code") == "M1-30")
		ti = next(r for r in rows if r.get("item_code") == TRADE_IN_CREDIT_ITEM)
		self.assertAlmostEqual(m1["net_amount"], 1330.89)  # full gun price separable
		self.assertAlmostEqual(m1["line_tax"], 109.80)
		self.assertAlmostEqual(m1["cost"], 1128.59)
		self.assertAlmostEqual(m1["profit"], 202.30)
		self.assertTrue(ti["is_trade_in"])
		self.assertAlmostEqual(ti["net_amount"], -869.0)
		self.assertAlmostEqual(ti["line_tax"], -71.69)  # negative tax shown as-is
		self.assertAlmostEqual(ti["cost"], 0.0)  # an inventory purchase — no cost, no wash
		self.assertAlmostEqual(ti["profit"], 0.0, places=2)  # no P&L on the swap
		# per-row gross_merch / trade_in_net split: the accountant reads gross_merch
		# as merchandise revenue without having to subtract the trade-in row.
		self.assertAlmostEqual(m1["gross_merch"], 1330.89)
		self.assertAlmostEqual(m1["trade_in_net"], 0.0)
		self.assertAlmostEqual(ti["gross_merch"], 0.0)
		self.assertAlmostEqual(ti["trade_in_net"], -869.0)
		self._recon(rows, "P23", 500.0)

	def test_trade_in_detail_parent_mirrors_gross_order_view(self):
		# The Order Detail PARENT row carries the same gross-of-trade-in basis as
		# the Order view: net_sales == gross_merch (the credit is an inventory
		# purchase, not negative revenue), cost is the sold gun's own COGS, and
		# profit is unchanged by the gross-up. Total stays the charged amount.
		h = _phdr("P23", voucher_type="POS Invoice", net=461.89, total=500.0)
		lines = {"P23": [
			_pline("r_m1", "M1-30", 1, 1330.89, serial="GARSN", item_name="M1 Garand"),
			_pline("r_ti", TRADE_IN_CREDIT_ITEM, 1, -869.0, item_name="Trade-in credit")]}
		meta = {"M1-30": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Misc Accessory"}}
		taxes = {"P23": [_taxrow("t1", SALES_TAX_ACC, 38.11)]}
		iwt = {"P23": [_iwt("r_m1", "t1", 109.80), _iwt("r_ti", "t1", -71.69)]}
		rows = self._rows([h], lines, meta, {"GARSN": 1128.59}, iwt, taxes, detail=True)
		order = next(r for r in rows if r.get("row_kind") == "order")
		self.assertAlmostEqual(order["net_sales"], 1330.89)
		self.assertAlmostEqual(order["gross_merch"], 1330.89)
		self.assertAlmostEqual(order["trade_in_net"], -869.0)
		self.assertTrue(order["is_trade_in"])  # FE marks the gross Net Sales cell
		self.assertAlmostEqual(order["cost"], 1128.59)
		self.assertAlmostEqual(order["profit"], 202.30)
		self.assertAlmostEqual(order["total"], 500.0)
		# The Totals row carries net_sales GROSS (what the Order Detail grid's
		# visible column shows — sums the parents, agrees with the card) while
		# net_amount stays net-of-trade-in for the tax worksheet.
		total = next(r for r in rows if r.get("is_total"))
		self.assertAlmostEqual(total["net_sales"], 1330.89)
		self.assertAlmostEqual(total["net_amount"], 461.89)

	def test_amended_partial_refund_d(self):
		h = _phdr("S81", net=598.99, total=598.99, woo_online_order="WC-81")
		lines = {"S81": [_pline("r_cz", "CZ75", 1, 598.99, serial="CZSN", item_name="CZ 75")]}
		meta = {"CZ75": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}}
		rows = self._rows([h], lines, meta, {"CZSN": 400.0}, {"S81": []}, {"S81": []})
		prod = [r for r in rows if r.get("row_kind") == "product"]
		self.assertEqual(len(prod), 1)  # D2: the -9.99 refund is not a separate row
		self.assertAlmostEqual(prod[0]["net_amount"], 598.99)  # D1: net after refund
		self.assertAlmostEqual(prod[0]["line_tax"], 0.0)
		self.assertFalse([r for r in rows if r.get("row_kind") == "shipping"])
		self._recon(rows, "S81", 598.99)

	def test_unclassified_charge_row_f1(self):
		h = _phdr("SX", net=100.0, total=120.0)
		lines = {"SX": [_pline("r_x", "X", 1, 100.0)]}
		meta = {"X": {"is_firearm": 0, "valuation_rate": 40.0, "item_group": "Misc Accessory"}}
		taxes = {"SX": [_taxrow("t1", SALES_TAX_ACC, 8.0), _taxrow("t2", "Excise - TSA", 12.0)]}
		iwt = {"SX": [_iwt("r_x", "t1", 8.0), _iwt("r_x", "t2", 12.0)]}
		acct = {SALES_TAX_ACC: "sales_tax"}
		rows = self._rows([h], lines, meta, {}, iwt, taxes, acct=acct)
		prod = next(r for r in rows if r.get("row_kind") == "product")
		self.assertAlmostEqual(prod["line_tax"], 8.0)  # excise excluded from sales tax
		uncl = [r for r in rows if r.get("row_kind") == "unclassified"]
		self.assertEqual(len(uncl), 1)
		self.assertAlmostEqual(uncl[0]["unclassified"], 12.0)
		# the account is surfaced in the rendered Category column (the Item Name
		# column was removed) so the fail-closed charge stays identifiable in CSV.
		self.assertIn("Excise", uncl[0]["category"])
		self._recon(rows, "SX", 120.0)

	def test_missing_cost_flag_f2(self):
		h = _phdr("PM", voucher_type="POS Invoice", net=500.0, total=541.25)
		lines = {"PM": [_pline("r_g", "GUN", 1, 500.0, serial="NOCOST")]}
		meta = {"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"}}
		taxes = {"PM": [_taxrow("t1", SALES_TAX_ACC, 41.25)]}
		iwt = {"PM": [_iwt("r_g", "t1", 41.25)]}
		rows = self._rows([h], lines, meta, {}, iwt, taxes)  # empty serial_cost → rate 0
		g = next(r for r in rows if r.get("row_kind") == "product")
		self.assertTrue(g["missing_cost"])
		self.assertEqual(g["cost"], 0.0)

	def test_ffl_transfer_exempts_net_zero_gun_per_row(self):
		# B (per-row proof): on an is_ffl_transfer invoice the net-0 transfer gun
		# (customer's own, no purchase_rate) is NOT flagged missing_cost, but a
		# real-sale gun (net>0, no cost) on the SAME invoice STILL flags. The
		# product view carries per-line missing_cost, so this pins the exemption
		# to the net-0 row rather than the whole invoice.
		h = _phdr("PX", voucher_type="POS Invoice", net=500.0, total=541.25, is_ffl_transfer=1)
		lines = {"PX": [
			_pline("r_x", "XFER-GUN", 1, 0.0, serial="CUSTGUN"),
			_pline("r_s", "SALE-GUN", 1, 500.0, serial="SOLDGUN")]}
		meta = {"XFER-GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Handgun"},
			"SALE-GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}}
		taxes = {"PX": [_taxrow("t1", SALES_TAX_ACC, 41.25)]}
		iwt = {"PX": [_iwt("r_s", "t1", 41.25)]}
		rows = self._rows([h], lines, meta, {}, iwt, taxes)  # empty serial_cost
		xfer = next(r for r in rows if r.get("item_code") == "XFER-GUN")
		sale = next(r for r in rows if r.get("item_code") == "SALE-GUN")
		self.assertFalse(xfer["missing_cost"])   # net-0 transfer gun exempt
		self.assertTrue(sale["missing_cost"])     # real-sale gun still flagged

	def test_channel_three_way_o6(self):
		pos = _phdr("PC", voucher_type="POS Invoice", net=100, total=100)
		web = _phdr("WC", net=100, total=100, woo_online_order="WC-1")
		b2b = _phdr("BC", net=100, total=100, woo_online_order=None)
		lines = {"PC": [_pline("r1", "A", 1, 100)], "WC": [_pline("r2", "A", 1, 100)],
			"BC": [_pline("r3", "A", 1, 100)]}
		meta = {"A": {"is_firearm": 0, "valuation_rate": 10.0, "item_group": "Misc"}}
		rows = self._rows([pos, web, b2b], lines, meta, {}, {}, {"PC": [], "WC": [], "BC": []})
		ch = {r["invoice"]: r["channel"] for r in rows if r.get("row_kind") == "product"}
		self.assertEqual(ch["PC"], "POS")
		self.assertEqual(ch["WC"], "Web")
		self.assertEqual(ch["BC"], "Manual")

	def test_consignment_flag_and_cost(self):
		h = _phdr("CN", voucher_type="POS Invoice", net=1000.0, total=1082.5)
		lines = {"CN": [_pline("r_c", "GUN", 1, 1000.0, serial="CS1")]}
		meta = {"GUN": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"}}
		taxes = {"CN": [_taxrow("t1", SALES_TAX_ACC, 82.5)]}
		iwt = {"CN": [_iwt("r_c", "t1", 82.5)]}
		rows = self._rows([h], lines, meta, {"CS1": 0.0}, iwt, taxes,
			consignment={"CS1": {"is_consignment": 1, "commission_pct": 20.0}})
		r = next(x for x in rows if x.get("row_kind") == "product")
		self.assertTrue(r["is_consignment"])
		self.assertAlmostEqual(r["cost"], 800.0)  # 1000*(1-0.20)
		self.assertAlmostEqual(r["profit"], 200.0)

	def test_detail_view_parent_and_indented_children_h(self):
		h = _phdr("P6", voucher_type="POS Invoice", net=1400.0, total=1515.50)
		lines = {"P6": [
			_pline("r_sks", "SKS", 1, 900.0, serial="SKSSN"),
			_pline("r_sw", "SW", 1, 500.0, serial="SWSN")]}
		meta = {"SKS": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"},
			"SW": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Shotgun"}}
		sc = {"SKSSN": 600.0, "SWSN": 300.0}
		taxes = {"P6": [_taxrow("t1", SALES_TAX_ACC, 115.50)]}
		iwt = {"P6": [_iwt("r_sks", "t1", 74.25), _iwt("r_sw", "t1", 41.25)]}
		rows = self._rows([h], lines, meta, sc, iwt, taxes, detail=True)
		parent = [r for r in rows if r.get("row_kind") == "order"]
		self.assertEqual(len(parent), 1)
		self.assertEqual(parent[0]["indent"], 0)
		self.assertAlmostEqual(parent[0]["net_sales"], 1400.0)  # mirrors Order view net
		self.assertAlmostEqual(parent[0]["total"], 1515.50)
		self.assertAlmostEqual(parent[0]["sales_tax"], 115.50)
		children = [r for r in rows if r.get("row_kind") == "product"]
		self.assertEqual(len(children), 2)
		self.assertTrue(all(c["indent"] == 1 for c in children))

	def test_detail_order_row_carries_order_ref(self):
		# The Order Detail PARENT (order) row carries order_ref — same
		# expression as the Order view — for the FE pass-through (Web shows the Woo
		# #, POS shows ""). Added to the parent order row ONLY: the flat product
		# child rows are unchanged (not added there).
		web = _phdr("SW", net=578.99, total=626.76, po_no="2134", woo_online_order="WOO-ORD-01122")
		pos = _phdr("PC", voucher_type="POS Invoice", date="2026-06-16", net=100.0, total=108.0)
		lines = {"SW": [_pline("r_w", "CZ75", 1, 578.99, serial="F7550")],
			"PC": [_pline("r_p", "AMMO", 1, 100.0)]}
		meta = {"CZ75": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"},
			"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}
		rows = self._rows([web, pos], lines, meta, {"F7550": 446.80},
			{"SW": [], "PC": []}, {"SW": [], "PC": []}, detail=True)
		orders = {r["invoice"]: r for r in rows if r.get("row_kind") == "order"}
		self.assertEqual(orders["SW"].get("order_ref"), "2134")   # Web → Woo storefront #
		self.assertEqual(orders["PC"].get("order_ref"), "")        # POS → blank (FE "—")
		# the exemption stays scoped: flat product CHILD rows do NOT gain order_ref.
		self.assertNotIn("order_ref", next(r for r in rows if r.get("row_kind") == "product"))

	def test_totals_row_independent_components_g2(self):
		h1 = _phdr("P6", voucher_type="POS Invoice", net=1400.0, total=1515.50)
		h2 = _phdr("P23", voucher_type="POS Invoice", date="2026-06-16", net=461.89, total=500.0)
		h3 = _phdr("S60", date="2026-06-17", net=219.0, total=264.0, woo_online_order="w")
		lines = {
			"P6": [_pline("r_sks", "SKS", 1, 900.0, serial="SKSSN"),
				_pline("r_sw", "SW", 1, 500.0, serial="SWSN")],
			"P23": [_pline("r_m1", "M1-30", 1, 1330.89, serial="GARSN"),
				_pline("r_ti", TRADE_IN_CREDIT_ITEM, 1, -869.0)],
			"S60": [_pline("r_c", "CARCANO", 1, 219.0, serial="CARSN")]}
		meta = {"SKS": {"is_firearm": 1, "valuation_rate": 0, "item_group": "Rifle"},
			"SW": {"is_firearm": 1, "valuation_rate": 0, "item_group": "Shotgun"},
			"M1-30": {"is_firearm": 1, "valuation_rate": 0, "item_group": "Rifle"},
			TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0, "item_group": "Misc Accessory"},
			"CARCANO": {"is_firearm": 1, "valuation_rate": 0, "item_group": "Rifle"}}
		sc = {"SKSSN": 600.0, "SWSN": 300.0, "GARSN": 1128.59, "CARSN": 150.0}
		taxes = {"P6": [_taxrow("t1", SALES_TAX_ACC, 115.50)],
			"P23": [_taxrow("t2", SALES_TAX_ACC, 38.11)],
			"S60": [_taxrow("t3", SHIP_ACC, 45.0)]}
		iwt = {"P6": [_iwt("r_sks", "t1", 74.25), _iwt("r_sw", "t1", 41.25)],
			"P23": [_iwt("r_m1", "t2", 109.80), _iwt("r_ti", "t2", -71.69)],
			"S60": [_iwt("r_c", "t3", 45.0)]}
		rows = self._rows([h1, h2, h3], lines, meta, sc, iwt, taxes)
		total = next(r for r in rows if r.get("is_total"))
		self.assertAlmostEqual(total["net_amount"], 1400.0 + 461.89 + 219.0)  # all product net (net-of-trade-in)
		self.assertAlmostEqual(total["gross_merch"], 2949.89)  # full-price merchandise = gross receipts base
		self.assertAlmostEqual(total["net_sales"], 2949.89)  # visible Net Sales total == gross
		self.assertAlmostEqual(total["trade_in_net"], -869.0)  # separable trade-in allowance
		self.assertAlmostEqual(total["shipping"], 45.0)
		# period sales tax totals under line_tax (flat view's tax column) and
		# sales_tax (detail view's) — both carry the same figure.
		self.assertAlmostEqual(total["line_tax"], 115.50 + 38.11)
		self.assertAlmostEqual(total["sales_tax"], 115.50 + 38.11)
		# goods COGS only — the trade-in credit line prices at 0, no wash.
		self.assertAlmostEqual(total["cost"], 900.0 + 1128.59 + 150.0)  # 2178.59
		# profit is measured on gross merchandise (2949.89 − 2178.59 = 771.30),
		# numerically equal to the old net-method figure (2080.89 − 1309.59).
		self.assertAlmostEqual(total["profit"], 771.30)
		# Each profit component must be a RENDERED column, not just a data key
		# (a data-only key never reaches the report/CSV — that was a false green).
		# The flat view's tax column is line_tax (sales_tax is the detail column).
		fields = {c["fieldname"] for c in sr._product_columns(detail=False)}
		for f in ("gross_merch", "trade_in_net", "shipping", "line_tax", "cost"):
			self.assertIn(f, fields)

	def test_discounted_order_sales_tax_is_iwt_golden(self):
		# 6th golden — a past invoice (POS 10% grand-total discount). The
		# taxes row's base_tax_amount is stale (57.67 = 699 * 8.25%, pre-discount),
		# but the real collected tax is 51.90 (post-discount, = IWTD = grand−net).
		# Sales tax everywhere (line, order, Totals) must be the IWTD 51.90; the
		# stale 57.67 must not surface, and this is NOT flagged (IWTD == charged).
		h = _phdr("P29", voucher_type="POS Invoice", net=629.10, total=681.00)
		lines = {"P29": [_pline("r29", "GUN29", 1, 629.10, serial="S29", item_name="Discounted Gun")]}
		meta = {"GUN29": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}}
		taxes = {"P29": [_taxrow("t1", SALES_TAX_ACC, 57.67, after=51.90)]}
		iwt = {"P29": [_iwt("r29", "t1", 51.90)]}
		rows = self._rows([h], lines, meta, {"S29": 400.0}, iwt, taxes)
		prod = next(r for r in rows if r.get("row_kind") == "product")
		self.assertAlmostEqual(prod["line_tax"], 51.90)
		self.assertFalse(prod["tax_mismatch"])
		self._recon(rows, "P29", 681.00)  # 629.10 + 0 + 51.90 = 681.00
		total = next(r for r in rows if r.get("is_total"))
		self.assertAlmostEqual(total["sales_tax"], 51.90)
		self.assertAlmostEqual(total["line_tax"], 51.90)  # Totals ≡ Line Tax column
		self.assertFalse(total.get("tax_mismatch"))

	def test_tax_mismatch_flag_when_iwt_diverges_from_charged(self):
		# A genuine anomaly: the charged (post-discount) sales tax is 50.00 but the
		# per-item IWTD only sums to 45.00 (e.g. a manually-entered tax with
		# incomplete item_wise detail). The report shows ΣIWTD and raises the flag.
		h = _phdr("PM", voucher_type="POS Invoice", net=600.0, total=650.0)
		lines = {"PM": [_pline("rA", "G", 1, 600.0, serial="S")]}
		meta = {"G": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}}
		taxes = {"PM": [_taxrow("t1", SALES_TAX_ACC, 57.0, after=50.0)]}  # charged 50
		iwt = {"PM": [_iwt("rA", "t1", 45.0)]}  # IWTD 45 ≠ charged 50
		rows = self._rows([h], lines, meta, {"S": 400.0}, iwt, taxes)
		prod = next(r for r in rows if r.get("row_kind") == "product")
		self.assertTrue(prod["tax_mismatch"])
		total = next(r for r in rows if r.get("is_total"))
		self.assertTrue(total["tax_mismatch"])
		self.assertAlmostEqual(total["sales_tax"], 45.0)  # ΣIWTD, not 57 (base) nor 50 (charged)

	def test_flat_columns_include_tax_audit_flags(self):
		# Audit flags: the CSV tax worksheet must carry the fail-closed /
		# trade-in / missing-cost / tax-mismatch signals as REAL columns — the .js
		# formatter's ⚠/tint is stripped on CSV export, so a column is the only
		# durable carrier.
		fields = {c["fieldname"] for c in sr._product_columns(detail=False)}
		for f in ("gross_merch", "trade_in_net", "unclassified", "missing_cost",
				"is_trade_in", "is_consignment", "tax_mismatch"):
			self.assertIn(f, fields)

	def test_detail_columns_include_unclassified(self):
		# An unclassified charge's amount must be visible in the tree,
		# not just its label.
		fields = {c["fieldname"] for c in sr._product_columns(detail=True)}
		self.assertIn("unclassified", fields)

	def test_item_column_is_link_and_no_item_name_column(self):
		# item_code links to its Item; the redundant Item
		# Name column is removed from both product views.
		for detail in (False, True):
			cols = {c["fieldname"]: c for c in sr._product_columns(detail)}
			self.assertEqual(cols["item_code"]["fieldtype"], "Link")
			self.assertEqual(cols["item_code"]["options"], "Item")
			self.assertNotIn("item_name", cols)

	def test_product_rows_carry_voucher_type_for_link(self):
		# The invoice column is a Dynamic Link keyed on voucher_type, so
		# every non-total row must carry its document's doctype for the link to
		# resolve. Product / shipping rows inherit it from the header.
		h = _phdr("P6", voucher_type="POS Invoice", net=1400.0, total=1515.50)
		lines = {"P6": [_pline("r_sks", "SKS", 1, 900.0, serial="SKSSN")]}
		meta = {"SKS": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Rifle"}}
		taxes = {"P6": [_taxrow("t1", SALES_TAX_ACC, 74.25)]}
		iwt = {"P6": [_iwt("r_sks", "t1", 74.25)]}
		rows = self._rows([h], lines, meta, {"SKSSN": 600.0}, iwt, taxes)
		self.assertEqual(next(r for r in rows if r.get("row_kind") == "product")["voucher_type"], "POS Invoice")
		# a web Sales Invoice row carries "Sales Invoice"; the Totals row is NOT a
		# document, so it must carry no voucher_type (Dynamic Link renders it as text).
		h2 = _phdr("S60", net=219.0, total=264.0, woo_online_order="w")
		taxes2 = {"S60": [_taxrow("t2", SHIP_ACC, 45.0)]}
		iwt2 = {"S60": [_iwt("r_c", "t2", 45.0)]}
		rows2 = self._rows([h2], {"S60": [_pline("r_c", "C", 1, 219.0)]},
			{"C": {"is_firearm": 0, "valuation_rate": 10.0, "item_group": "Misc"}}, {}, iwt2, taxes2)
		self.assertEqual(next(r for r in rows2 if r.get("row_kind") == "product")["voucher_type"], "Sales Invoice")
		self.assertEqual(next(r for r in rows2 if r.get("row_kind") == "shipping")["voucher_type"], "Sales Invoice")
		self.assertFalse(next(r for r in rows2 if r.get("is_total")).get("voucher_type"))


_ORDER_VIEW_COLUMNS = ["posting_date", "invoice", "order_ref", "channel", "customer",
	"serials", "category", "qty", "net_sales", "cost", "profit", "margin",
	# The marketplace's cut and the profit left after it, between
	# Margin and Total. Default-hidden on screen, always in the CSV.
	"channel_fees", "profit_after_fees",
	# Negative-ticket settlement: default-hidden, always in the CSV.
	"trade_in_applied", "trade_in_payout",
	"total"]


class SalesReportBackCompat(FrappeTestCase):
	"""Order view COLUMN CONTRACT: 15 columns in a fixed order
	— adds Order Ref + Serial (to tell look-alike rows apart), then Channel Fees + Profit
	after Fees, Total moved to the end (FE default-hides it). execute() returns
	Frappe's native 5-tuple. The number COLUMNS (Net Sales/Cost/Profit/Margin)
	and per-row numbers stay unchanged (the redesign is visual/interaction only) —
	Cost in particular still excludes the channel fee, which is the whole point
	of giving the fee its own column."""

	def test_default_view_is_order_with_the_standard_columns(self):
		cols, _data_, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30"})
		self.assertEqual([c["fieldname"] for c in cols], _ORDER_VIEW_COLUMNS)

	def test_explicit_order_view_same_columns(self):
		cols, _data_, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30", "view": "Order"})
		self.assertEqual([c["fieldname"] for c in cols], _ORDER_VIEW_COLUMNS)

	def test_order_view_rows_carry_no_product_view_keys(self):
		rows = _build_rows([_hdr("P1", net=100.0, total=108.0)],
			{"P1": [_line("AMMO", qty=2, net=100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}, {}, "")
		r = _data(rows)[0]
		self.assertEqual(r["net_sales"], 100.0)
		self.assertEqual(r["cost"], 60.0)
		self.assertEqual(r["profit"], 40.0)
		self.assertNotIn("row_kind", r)
		self.assertNotIn("line_tax", r)

	def test_execute_product_view_smoke(self):
		cols, data, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30", "view": "Product"})
		self.assertTrue(any(c["fieldname"] == "line_tax" for c in cols))
		self.assertIsInstance(data, list)

	def test_execute_detail_view_smoke(self):
		cols, data, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30", "view": "Order Detail"})
		self.assertTrue(any(c["fieldname"] == "line_tax" for c in cols))
		self.assertIsInstance(data, list)

	def test_order_view_row_golden_snapshot(self):
		# Pin the FULL Order-view data-row dict so any drift is caught in CI.
		# The row carries order_ref / serials
		# / category_detail / is_consignment (new data keys) and channel via _channel()
		# (POS here → "POS", unchanged). Numbers (net/cost/profit/margin) unchanged.
		rows = _build_rows(
			[_hdr("P1", net=100.0, total=108.0)],
			{"P1": [_line("AMMO", qty=2, net=100.0)]},
			{"AMMO": {"is_firearm": 0, "valuation_rate": 30.0, "item_group": "Ammunition"}}, {}, "")
		self.assertEqual(_data(rows)[0], {
			"posting_date": "2026-06-15", "invoice": "P1", "voucher_type": "POS Invoice",
			"channel": "POS", "customer": "C1", "category": "Ammunition", "qty": 2.0,
			"net_sales": 100.0, "total": 108.0, "cost": 60.0, "profit": 40.0,
			"margin": 40.0, "missing_cost": False,
			# Data-only: a gun costed from its Serial No over a placeholder ledger value.
			"placeholder_cost": False,
			# Data-only review flags of a gun line (_line_marks); False on a sale with no gun.
			"cost_pending_delivery": False, "unledgered_cost": False, "double_sale": False,
			"order_ref": "", "serials": "", "category_detail": "Ammunition",
			"is_consignment": False,
			# Data-only trade-in keys (no column) so the
			# FE can mark gross trade-in rows; 0/False on an ordinary sale.
			"is_trade_in": False, "trade_in_net": 0.0,
			# Negative-ticket settlement: real columns; 0 on a normal sale.
			"trade_in_applied": 0.0, "trade_in_payout": 0.0,
			"is_return": False,
			# 0 / == profit on every non-marketplace sale, which
			# is what makes the pair safe to read on a counter row.
			"channel_fees": 0.0, "profit_after_fees": 40.0,
		})

	def test_invoice_column_is_clickable_dynamic_link_all_views(self):
		# The invoice column links to its document in all three views — a Dynamic
		# Link keyed on the row's voucher_type.
		for filt in ({}, {"view": "Product"}, {"view": "Order Detail"}):
			cols, _d, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30", **filt})
			inv = next(c for c in cols if c["fieldname"] == "invoice")
			self.assertEqual(inv["fieldtype"], "Dynamic Link")
			self.assertEqual(inv["options"], "voucher_type")

	def test_execute_channel_filter_wired_in_all_views(self):
		# batch-4: the 3-way channel filter is applied in every view without error.
		for view in ("Order", "Order Detail", "Product"):
			for ch in ("POS", "Web", "Manual"):
				_cols, data, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30",
					"view": view, "channel": ch})
				self.assertIsInstance(data, list)


class SalesReportChannelFilter(FrappeTestCase):
	"""The channel filter matches the 3-way channel (POS / Web / Manual)
	uniformly for every view; empty = all; the retired 2-way "Order" value stays
	a backward-compatible alias for all direct Sales Invoices (Web + Manual)."""

	HS = [
		{"voucher_type": "POS Invoice", "woo_online_order": None, "name": "P"},
		{"voucher_type": "Sales Invoice", "woo_online_order": "WC-1", "name": "W"},
		{"voucher_type": "Sales Invoice", "woo_online_order": None, "name": "B"},
	]

	def _names(self, channel):
		return [h["name"] for h in sr._apply_channel_filter(self.HS, channel)]

	def test_pos_matches_pos_invoice(self):
		self.assertEqual(self._names("POS"), ["P"])

	def test_web_matches_si_with_woo_order(self):
		self.assertEqual(self._names("Web"), ["W"])

	def test_manual_matches_si_without_woo_order(self):
		self.assertEqual(self._names("Manual"), ["B"])

	def test_the_old_name_b2b_manual_is_still_accepted(self):
		"""Renamed 2026-08-30. A saved filter or an MCP caller sending the old
		value must get Manual's rows, not an empty report."""
		self.assertEqual(self._names("B2B-Manual"), ["B"])

	def test_empty_channel_returns_all(self):
		self.assertEqual(self._names(""), ["P", "W", "B"])
		self.assertEqual(self._names(None), ["P", "W", "B"])

	def test_legacy_order_alias_is_all_sales_invoices(self):
		self.assertEqual(self._names("Order"), ["W", "B"])


class SalesReportColumnContract(FrappeTestCase):
	"""Column picker: hiding is CLIENT-SIDE only (localStorage + datatable
	col.hidden). The server's get_columns (_product_columns) must ALWAYS return the
	full contract column set, so the Product-view CSV tax worksheet is complete regardless
	of what a user hides on screen. These guard against anyone
	adding server-side column filtering that would silently break the CSV contract."""

	# The contract fieldnames the Product-view CSV tax worksheet must always carry.
	CONTRACT = {
		"posting_date", "invoice", "channel", "customer", "row_kind", "item_code",
		"category", "serial_no", "qty", "unit_price", "net_amount", "gross_merch",
		"trade_in_net", "line_tax", "shipping", "unclassified", "cost", "profit",
		"missing_cost", "is_trade_in", "is_consignment", "exempt_from_sales_tax",
		"tax_mismatch",
	}

	def test_product_columns_always_full_contract(self):
		flat = {c["fieldname"] for c in sr._product_columns(detail=False)}
		missing = self.CONTRACT - flat
		self.assertEqual(missing, set(), f"CSV contract columns missing server-side: {missing}")

	def test_execute_product_view_returns_full_columns(self):
		# End-to-end: execute() — which the CSV export re-runs server-side — never
		# filters columns; the client picker only toggles col.hidden on screen.
		cols, _data, *_ = execute({"from_date": "2026-06-01", "to_date": "2026-06-30", "view": "Product"})
		self.assertTrue(self.CONTRACT.issubset({c["fieldname"] for c in cols}))


# =====================================================================
# report_summary period cards + message helper/empty-state
# =====================================================================

def _order_total(**over):
	base = {"is_total": True, "net_sales": 300.0, "cost": 100.0, "profit": 200.0, "margin": 66.6667}
	base.update(over)
	return base


def _product_total(**over):
	# gross_merch deliberately ≠ net_amount (a 200 trade-in allowance) so the
	# summary tests pin WHICH figure each card reads: Net Sales must be the
	# gross_merch 1200, never the netted 1000.
	base = {"is_total": True, "net_amount": 1000.0, "gross_merch": 1200.0, "cost": 700.0,
		"profit": 500.0, "sales_tax": 82.5, "line_tax": 82.5, "shipping": 45.0, "unclassified": 0.0}
	base.update(over)
	return base


class SalesReportSummary(FrappeTestCase):
	"""_report_summary(view, data) assembles the Frappe report_summary cards from
	the existing Total row + one flag scan — NO new accounting basis. Order = 5 cards;
	Order Detail / Product = 7 (adds Sales Tax + Shipping)."""

	def test_order_view_five_cards_labels_and_values(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		data = [{"missing_cost": False}, _order_total()]
		cards = _report_summary("Order", data)
		self.assertEqual([c["label"] for c in cards],
			["Net Sales", "Cost", "Goods Profit", "Margin %", "Cost Flags"])
		self.assertEqual(cards[0]["value"], 300.0)
		self.assertEqual(cards[0]["indicator"], "Blue")
		self.assertEqual(cards[2]["value"], 200.0)      # Goods Profit == Total profit
		self.assertEqual(cards[2]["indicator"], "Green")
		self.assertAlmostEqual(cards[3]["value"], 66.6667)  # Margin == Total margin

	def test_order_cost_flags_clean_when_zero(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		cards = _report_summary("Order", [{"missing_cost": False}, _order_total()])
		self.assertEqual(cards[4]["label"], "Cost Flags")
		self.assertEqual(cards[4]["value"], "✓ Clean")
		self.assertEqual(cards[4]["indicator"], "Green")

	def test_order_cost_flags_counts_missing_rows(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		data = [{"missing_cost": True}, {"missing_cost": False}, {"missing_cost": True},
			_order_total()]
		flags = _report_summary("Order", data)[4]
		self.assertEqual(flags["value"], 2)
		self.assertEqual(flags["indicator"], "Red")

	def test_loss_period_profit_and_margin_indicators_red(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		cards = _report_summary("Order", [_order_total(profit=-50.0, margin=-10.0)])
		self.assertEqual(cards[2]["indicator"], "Red")   # Goods Profit < 0
		self.assertEqual(cards[3]["indicator"], "Red")   # Margin < 0

	def test_product_view_seven_cards_with_tax_shipping(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		data = [
			{"row_kind": "product", "invoice": "S1", "missing_cost": False, "tax_mismatch": False},
			_product_total()]
		cards = _report_summary("Product", data)
		self.assertEqual([c["label"] for c in cards],
			["Net Sales", "Cost", "Goods Profit", "Margin %", "Sales Tax", "Shipping", "Data Flags"])
		self.assertEqual(cards[0]["value"], 1200.0)       # gross_merch, NOT net_amount
		self.assertAlmostEqual(cards[3]["value"], 500.0 / 1200.0 * 100)  # profit/gross
		self.assertEqual(cards[4]["value"], 82.5)         # Sales Tax
		self.assertEqual(cards[5]["value"], 45.0)         # Shipping

	def test_detail_view_also_seven_cards(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		cards = _report_summary("Order Detail", [_product_total()])
		self.assertEqual(len(cards), 7)

	def test_product_data_flags_counts_missing_mismatch_unclassified(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		data = [
			{"row_kind": "product", "invoice": "S1", "missing_cost": True, "tax_mismatch": False},
			{"row_kind": "product", "invoice": "S2", "missing_cost": False, "tax_mismatch": True},
			{"row_kind": "unclassified", "invoice": "S2"},
			_product_total()]
		flags = _report_summary("Product", data)[6]
		self.assertEqual(flags["label"], "Data Flags")
		self.assertEqual(flags["value"], 3)   # 1 missing + 1 mismatch order + 1 unclassified
		self.assertEqual(flags["indicator"], "Red")

	def test_tax_mismatch_counted_once_per_order(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		# Two product rows of the SAME order both carry tax_mismatch → 1 order, not 2.
		data = [
			{"row_kind": "product", "invoice": "S1", "missing_cost": False, "tax_mismatch": True},
			{"row_kind": "product", "invoice": "S1", "missing_cost": False, "tax_mismatch": True},
			_product_total()]
		self.assertEqual(_report_summary("Product", data)[6]["value"], 1)

	def test_empty_period_zero_value_cards(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_summary
		cards = _report_summary("Order", [])
		self.assertEqual(cards[0]["value"], 0.0)          # Net Sales $0
		self.assertEqual(cards[4]["value"], "✓ Clean")    # 0 flags → clean, not broken


class SalesReportMessage(FrappeTestCase):
	"""_report_message(view, data, filters): the above-datatable helper when
	there are rows, or the friendly empty-state when the period is empty."""

	def test_order_view_helper_when_rows_present(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_message
		msg = _report_message("Order", [{"invoice": "X"}], {"from_date": "a", "to_date": "b"})
		self.assertIn("One row per sale", msg)

	def test_product_view_helper_mentions_csv(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_message
		msg = _report_message("Product", [{"invoice": "X"}], {"from_date": "a", "to_date": "b"})
		self.assertIn("CSV", msg)

	def test_empty_period_friendly_message(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _report_message
		msg = _report_message("Order", [], {"from_date": "2026-07-01", "to_date": "2026-07-15"})
		self.assertIn("No sales", msg)
		self.assertIn("2026-07-01", msg)          # names the date range
		self.assertIn("2026-07-15", msg)


# =====================================================================
# Negative trade-in ticket: trade-ins exceed the
# goods, the store pays the customer the difference on the same ticket.
# =====================================================================

GUN_META = {"GLOCK": {"is_firearm": 1, "valuation_rate": 0.0, "item_group": "Pistol"},
	"SVC": {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Services"},
	TRADE_IN_CREDIT_ITEM: {"is_firearm": 0, "valuation_rate": 0.0, "item_group": "Misc Accessory"}}
CREDITS = (450.0, 1200.0, 300.0, 2750.0, 750.0)   # the example: 5,450 in all
EXEMPT_TEMPLATE = "EXEMPT-TEMPLATE"   # stands in for a 0% Item Tax Template (the real one: test_sales_report_negative_ticket)


def _negative_ticket(name="P-NEG", gun=565.0, credits=CREDITS, extra=(), total=None, net_total=None,
		is_return=0):
	"""The example ticket as the DB holds it AFTER the tax floor: tax 0, so
	net_total == grand_total == goods - credits."""
	credit = sum(credits)
	goods = gun + sum(n for _i, n, _r in extra)
	net_total = goods - credit if net_total is None else net_total
	h = _phdr(name, voucher_type="POS Invoice", net=net_total,
		total=net_total if total is None else total, is_return=is_return)
	lines = [_pline(f"{name}-g", "GLOCK", 1, gun, serial="SN-G")]
	lines += [_pline(f"{name}-x{i}", item, 1, n, item_name=item, ) for i, (item, n, _r) in enumerate(extra)]
	for i, (_item, _n, rate) in enumerate(extra):
		lines[1 + i]["item_tax_template"] = rate
	lines += [_pline(f"{name}-c{i}", TRADE_IN_CREDIT_ITEM, 1, -c) for i, c in enumerate(credits)]
	return h, {name: lines}


class SalesReportNegativeTradeIn(FrappeTestCase):
	def _order(self, h, lines):
		rows = _build_rows([h], lines, GUN_META, {"SN-G": 350.0}, "")
		return _data(rows)[0], _total(rows)

	def _detail(self, h, lines, taxes, iwt):
		rows = sr._build_product_rows([h], lines, GUN_META, {"SN-G": 350.0}, {}, iwt, taxes,
			ACCT_CLASS, "", True)
		return next(r for r in rows if r.get("row_kind") == "order"), _total(rows)

	def test_order_view_example_ticket(self):
		h, lines = _negative_ticket()
		r, total = self._order(h, lines)
		self.assertAlmostEqual(r["net_sales"], 565.0)          # the goods, not -4,885
		self.assertAlmostEqual(r["total"], -4885.0)            # the money actually settled
		self.assertAlmostEqual(r["trade_in_net"], -5450.0)     # existing sign: credit lines' net
		self.assertAlmostEqual(r["trade_in_applied"], 565.0)   # min(5,450 credit, 565 taxable goods)
		self.assertAlmostEqual(r["trade_in_payout"], 4885.0)   # -grand_total
		# the identity the CPA reads off one row: goods + credits + tax(0) == total
		self.assertAlmostEqual(r["net_sales"] + r["trade_in_net"], r["total"])
		self.assertAlmostEqual(r["cost"], 350.0)               # the gun's own COGS; credit lines cost 0
		self.assertAlmostEqual(r["profit"], 215.0)
		self.assertTrue(r["is_trade_in"])
		self.assertAlmostEqual(total["trade_in_applied"], 565.0)
		self.assertAlmostEqual(total["trade_in_payout"], 4885.0)
		self.assertAlmostEqual(total["total"], -4885.0)

	def test_detail_parent_row_example_ticket_floored_tax_is_clean(self):
		# After the tax floor the Sales Tax row is 0 and so is every item-wise allocation.
		h, lines = _negative_ticket()
		taxes = {"P-NEG": [_taxrow("t1", SALES_TAX_ACC, 0.0)]}
		names = [l["name"] for l in lines["P-NEG"]]
		iwt = {"P-NEG": [_iwt(n, "t1", 0.0) for n in names]}
		parent, total = self._detail(h, lines, taxes, iwt)
		self.assertAlmostEqual(parent["net_sales"], 565.0)
		self.assertAlmostEqual(parent["sales_tax"], 0.0)
		self.assertAlmostEqual(parent["total"], -4885.0)
		self.assertAlmostEqual(parent["trade_in_net"], -5450.0)
		self.assertAlmostEqual(parent["trade_in_applied"], 565.0)
		self.assertAlmostEqual(parent["trade_in_payout"], 4885.0)
		self.assertFalse(parent["tax_mismatch"])
		self.assertFalse(total["tax_mismatch"])
		self.assertAlmostEqual(total["trade_in_payout"], 4885.0)
		self.assertAlmostEqual(total["trade_in_applied"], 565.0)

	def test_detail_floored_ticket_with_no_item_wise_rows_is_clean_too(self):
		h, lines = _negative_ticket()
		parent, _t = self._detail(h, lines, {"P-NEG": [_taxrow("t1", SALES_TAX_ACC, 0.0)]}, {"P-NEG": []})
		self.assertFalse(parent["tax_mismatch"])
		self.assertAlmostEqual(parent["sales_tax"], 0.0)

	def test_ordinary_trade_in_is_applied_in_full_with_no_payout(self):
		# 565 - 300 -> tax 21.86, total 286.86: the whole allowance came off the tax base.
		h, lines = _negative_ticket("P-REG", credits=(300.0,), net_total=265.0, total=286.86)
		r, _t = self._order(h, lines)
		self.assertAlmostEqual(r["trade_in_applied"], 300.0)
		self.assertAlmostEqual(r["trade_in_payout"], 0.0)
		self.assertAlmostEqual(r["total"], 286.86)

	def test_exempt_service_is_not_taxable_goods(self):
		# 565 taxable gun + 50 exempt service - 600 of credit: tax base 565 -> 0,
		# customer pays 15, nothing paid out. Applied is capped by the TAXABLE goods.
		h, lines = _negative_ticket("P-SVC", credits=(600.0,), extra=[("SVC", 50.0, EXEMPT_TEMPLATE)],
			total=15.0)
		from unittest.mock import patch
		with patch.object(sr, "_line_taxable", side_effect=lambda l, c, a: l.get("item_tax_template") != EXEMPT_TEMPLATE):
			r, _t = self._order(h, lines)
		self.assertAlmostEqual(r["trade_in_applied"], 565.0)
		self.assertAlmostEqual(r["trade_in_payout"], 0.0)
		self.assertAlmostEqual(r["total"], 15.0)
		self.assertAlmostEqual(r["net_sales"], 615.0)

	def test_a_return_never_reports_a_payout_or_applied_allowance(self):
		# The return of the example ticket: grand_total is negative too, but that is a refund.
		h = _hdr("R-NEG", net=-565.0, total=-565.0, is_return=1)
		lines = {"R-NEG": [_line("GLOCK", -1, -565.0, serial="SN-G")]}
		r, _t = self._order(h, lines)
		self.assertAlmostEqual(r["trade_in_payout"], 0.0)
		self.assertAlmostEqual(r["trade_in_applied"], 0.0)
		self.assertAlmostEqual(r["total"], -565.0)

	def test_negative_total_without_trade_in_credit_is_not_a_payout(self):
		# A negative line other than a credit past the goods (the server refuses it; if one ever lands, it is
		# not a trade-in payout).
		h = _hdr("P-NEG-SVC", net=-100.0, total=-100.0)
		lines = {"P-NEG-SVC": [_line("GLOCK", 1, 500.0, serial="SN-G"), _line("SVC", 1, -600.0)]}
		r, _t = self._order(h, lines)
		self.assertAlmostEqual(r["trade_in_payout"], 0.0)
		self.assertAlmostEqual(r["trade_in_applied"], 0.0)

	def test_totals_row_sums_across_tickets(self):
		h1, l1 = _negative_ticket("P-A")
		h2, l2 = _negative_ticket("P-B", credits=(100.0,), net_total=465.0, total=502.19)
		h2["posting_date"] = "2026-06-16"
		rows = _build_rows([h1, h2], {**l1, **l2}, GUN_META, {"SN-G": 350.0}, "")
		t = _total(rows)
		self.assertAlmostEqual(t["trade_in_applied"], 565.0 + 100.0)
		self.assertAlmostEqual(t["trade_in_payout"], 4885.0)
		self.assertAlmostEqual(t["total"], -4885.0 + 502.19)

	def test_line_taxable_without_a_template_is_taxable_whatever_the_stored_rate(self):
		# the real template lookups: test_sales_report_negative_ticket (a ticket through the tax floor)
		acct = ["Sales Tax Payable - TSA"]
		self.assertTrue(sr._line_taxable({}, "Co", acct))
		self.assertTrue(sr._line_taxable({"item_tax_template": None}, "Co", acct))
		# the stored rate is never read: the tax floor rewrites it to 0 on every row of the ticket it floors
		self.assertTrue(sr._line_taxable({"item_tax_rate": '{"Sales Tax Payable - TSA": 0}'}, "Co", acct))
		# a ticket with no sales-tax row has nothing to be exempt from
		self.assertTrue(sr._line_taxable({"item_tax_template": "ANY"}, "Co", []))

	def test_payout_is_the_rounded_total_when_there_is_one_like_the_server(self):
		h, lines = _negative_ticket()
		h["rounded_total"] = -4885.0
		h["grand_total"] = -4885.4
		r, _t = self._order(h, lines)
		self.assertAlmostEqual(r["trade_in_payout"], 4885.0)
		h2, l2 = _negative_ticket("P-N2")
		h2["rounded_total"] = 0     # rounding off: the grand total
		h2["grand_total"] = -4885.4
		r2, _t = self._order(h2, l2)
		self.assertAlmostEqual(r2["trade_in_payout"], 4885.4)

	def test_columns_carry_the_pair_before_total_in_order_and_detail_views(self):
		order = [c["fieldname"] for c in sr._columns()]
		self.assertEqual(order[-3:], ["trade_in_applied", "trade_in_payout", "total"])
		detail = [c["fieldname"] for c in sr._product_columns(detail=True)]
		self.assertEqual(detail[-3:], ["trade_in_applied", "trade_in_payout", "total"])
		# the flat tax worksheet is line-level: an order-level payout has no row to sit on
		flat = {c["fieldname"] for c in sr._product_columns(detail=False)}
		self.assertNotIn("trade_in_payout", flat)

	def test_fetch_lines_reads_the_item_tax_template_column_on_both_invoice_item_doctypes(self):
		# A stubbed frappe.get_all would never notice a column that does not exist; ask the real meta.
		for dt in ("POS Invoice Item", "Sales Invoice Item"):
			self.assertTrue(frappe.get_meta(dt).has_field("item_tax_template"), dt)
		# and the real query runs against both (empty date window, so no data dependence)
		headers = [{"name": "NOPE-1", "voucher_type": "POS Invoice", "posting_date": "2026-01-01"},
			{"name": "NOPE-2", "voucher_type": "Sales Invoice", "posting_date": "2026-01-01"}]
		self.assertEqual(sr._fetch_lines(headers), {})
