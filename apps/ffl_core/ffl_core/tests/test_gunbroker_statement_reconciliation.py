"""Month-end reconciliation against the marketplace's own statements.

GunBroker bills in two documents, not one: a sales-tax statement it files under
its own permit, and a fee bill it auto-debits around the 20th. Neither arrives
through the API — there is no per-order fee endpoint — so the only way to check
that what we accrued is what we are being charged is to read the statement the
operator downloads.

Two things this has to get right.

**Match per order and per item, never per month.** An order sold on 30 January
and shipped on 2 February sits in January on GunBroker's statement and February
in our ledger. Compare monthly totals and every such order shows up as a
difference; compare order by order and it shows up as what it is — a timing
gap, with both sides agreeing on the amount.

**The file format is not sampled.** Nobody has yet exported a real statement
(that is still open), so the parser reads headers by meaning rather than
by position, tolerates the byte-order mark Excel writes, and accepts a pasted
table as readily as a file. The fixtures below are CONSTRUCTED from the expected
statement field list, not taken from a real download — which is exactly why the parser is
written to bend rather than to assume.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.report.gunbroker_statement_reconciliation import (
	gunbroker_statement_reconciliation as sr)


# A constructed fee statement — NOT a sample of a real GunBroker export.
# Field names follow the expected statement field list; the reconciler is built
# so that a real export with different spelling still parses.
FEE_CSV = """OrderID,ItemID,Description,Amount,Date
1001,55501,Final Value Fee,52.00,2027-01-02
1001,55501,FFL Document Fee,5.00,2027-01-02
1002,55502,Final Value Fee,40.00,2027-01-02
"""

TAX_CSV = """Order ID,Sales Tax,Transaction Date
1001,89.99,2027-01-02
1002,0.00,2027-01-02
"""


def _pos(order="1001", item="55501", amount=57.0, date="2027-01-02",
         reference="ACC-SINV-2027-00001", accrual="posted"):
	return {"order_id": order, "item_id": item, "amount": amount,
		"posting_date": date, "reference": reference, "accrual": accrual}


def _by_status(rows, status):
	return [r for r in rows if r.get("status") == status]


# ------------------------------------------------------------------ parsing

class ReadingAStatementNobodyHasSeenYet(FrappeTestCase):
	"""The export's exact shape is unverified. So headers are read by meaning,
	and everything that commonly breaks a spreadsheet round-trip is handled."""

	def test_a_plain_comma_file_parses(self):
		rows = sr._parse_statement(FEE_CSV)
		self.assertEqual(len(rows), 3)
		self.assertEqual(rows[0]["order_id"], "1001")
		self.assertEqual(rows[0]["item_id"], "55501")
		self.assertAlmostEqual(rows[0]["amount"], 52.0)

	def test_headers_are_matched_by_meaning_not_spelling(self):
		# "Order ID" / "OrderID" / "Order #" are the same column.
		for header in ("Order ID", "OrderID", "Order #", "order_number"):
			rows = sr._parse_statement(f"{header},Amount\n1001,10.00\n")
			self.assertEqual(rows[0]["order_id"], "1001", header)

	def test_a_pasted_tab_separated_table_parses(self):
		rows = sr._parse_statement("OrderID\tAmount\n1001\t52.00\n")
		self.assertEqual(rows[0]["order_id"], "1001")
		self.assertAlmostEqual(rows[0]["amount"], 52.0)

	def test_the_excel_byte_order_mark_does_not_swallow_the_first_header(self):
		# A BOM turns "OrderID" into "﻿OrderID", the order column goes
		# unrecognised, and every row reconciles as missing. Silent and total.
		rows = sr._parse_statement("﻿OrderID,Amount\n1001,52.00\n")
		self.assertEqual(rows[0]["order_id"], "1001")

	def test_header_normalisation_is_what_defuses_the_byte_order_mark(self):
		# Pinned at the level that actually does the work. The test above passes
		# through _norm; if _norm ever became a blacklist ("strip spaces and
		# underscores") the BOM would come back and that test would be the only
		# thing between it and a month of phantom missing rows.
		self.assertEqual(sr._norm("﻿OrderID"), "orderid")
		self.assertEqual(sr._norm("Order #"), "order")

	def test_currency_dressing_is_stripped(self):
		self.assertAlmostEqual(sr._parse_money("$1,245.99"), 1245.99)
		self.assertAlmostEqual(sr._parse_money(" 52.00 "), 52.0)

	def test_a_credit_in_parentheses_is_negative(self):
		# Accounting notation for a credit. Read as positive it flips the sign
		# of a fee reversal and the difference reads as double.
		self.assertAlmostEqual(sr._parse_money("(52.00)"), -52.0)
		self.assertAlmostEqual(sr._parse_money("-52.00"), -52.0)

	def test_junk_is_zero_rather_than_an_exception(self):
		# A totals row or a footnote at the bottom of the export must not take
		# the whole month's reconciliation down.
		self.assertAlmostEqual(sr._parse_money("n/a"), 0.0)
		self.assertAlmostEqual(sr._parse_money(None), 0.0)

	def test_a_file_with_no_order_column_is_refused(self):
		# Better to say "this is not a statement" than to report every order
		# missing, which looks like a catastrophic accrual failure.
		with self.assertRaises(Exception) as caught:
			sr._parse_statement("Foo,Bar\n1,2\n")
		self.assertIn("order", str(caught.exception).lower())

	def test_an_empty_paste_is_refused(self):
		with self.assertRaises(Exception):
			sr._parse_statement("   ")

	def test_rows_without_an_order_number_are_skipped(self):
		# The trailing "Total" line every export has.
		rows = sr._parse_statement("OrderID,Amount\n1001,52.00\nTotal,52.00\n,\n")
		self.assertEqual([r["order_id"] for r in rows], ["1001", "Total"])


# ------------------------------------------------------------- reconciliation

class MatchingIsPerOrderAndPerItem(FrappeTestCase):
	def test_two_statement_lines_for_one_item_are_added_before_comparing(self):
		# GunBroker bills the value fee and the document fee as separate lines;
		# we accrue their sum. Comparing line by line would report both as
		# differences on every single order.
		rows = sr._reconcile(sr._parse_statement(FEE_CSV), [_pos(amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		matched = _by_status(rows, "matched")
		self.assertTrue(any(r["order_id"] == "1001" for r in matched))

	def test_a_difference_in_amount_is_reported_with_both_figures(self):
		rows = sr._reconcile(sr._parse_statement(FEE_CSV), [_pos(amount=53.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		row = _by_status(rows, "amount")[0]
		self.assertAlmostEqual(row["statement_amount"], 57.0)
		self.assertAlmostEqual(row["pos_amount"], 53.0)
		self.assertAlmostEqual(row["difference"], 4.0)

	def test_a_penny_of_rounding_is_not_a_difference(self):
		rows = sr._reconcile([{"order_id": "1", "item_id": "", "amount": 57.004,
			"date": None, "description": ""}],
			[_pos(order="1", item="", amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "amount"), [])

	def test_on_the_statement_and_not_in_our_books(self):
		# The dangerous direction: GunBroker is charging for something we never
		# accrued, so the fee expense is understated and the bank still pays.
		rows = sr._reconcile(sr._parse_statement(FEE_CSV), [_pos(amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		missing = _by_status(rows, "missing_in_pos")
		self.assertEqual([r["order_id"] for r in missing], ["1002"])

	def test_in_our_books_and_not_on_the_statement(self):
		rows = sr._reconcile([], [_pos(order="1003", amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual([r["order_id"] for r in _by_status(rows, "missing_on_statement")],
			["1003"])

	def test_an_item_the_order_does_not_have_is_reported_against_that_item(self):
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "99999", "amount": 10.0,
			  "date": None, "description": "Final Value Fee"}],
			[_pos(order="1001", item="55501", amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		row = _by_status(rows, "missing_in_pos")[0]
		self.assertEqual(row["item_id"], "99999")

	def test_a_statement_line_with_no_item_matches_the_order_total(self):
		# The tax statement is per order, not per item.
		rows = sr._reconcile(sr._parse_statement(TAX_CSV),
			[_pos(order="1001", item="", amount=89.99),
			 _pos(order="1002", item="", amount=0.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "amount"), [])
		self.assertEqual(len(_by_status(rows, "matched")), 2)

	def test_an_order_billed_per_item_still_matches_an_order_level_line(self):
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "", "amount": 97.0,
			  "date": None, "description": "Fees"}],
			[_pos(order="1001", item="55501", amount=57.0),
			 _pos(order="1001", item="55502", amount=40.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(len(_by_status(rows, "matched")), 1)


class TimingIsNotADifference(FrappeTestCase):
	"""An order sold on 30 January and shipped 2 February is on
	January's statement and in February's ledger. Both sides are right."""

	def test_an_order_posted_after_the_statement_period_is_a_timing_gap(self):
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "", "amount": 57.0,
			  "date": "2027-01-30", "description": "Final Value Fee"}],
			[_pos(order="1001", item="", amount=57.0, date="2027-02-02")],
			from_date="2027-01-01", to_date="2027-01-31")
		row = _by_status(rows, "timing")[0]
		self.assertAlmostEqual(row["difference"], 0.0)

	def test_a_timing_gap_is_not_reported_as_an_amount_difference(self):
		# The whole point: an operator chasing "amount" rows must not be handed
		# a month of orders where nothing is actually wrong.
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "", "amount": 57.0,
			  "date": "2027-01-30", "description": ""}],
			[_pos(order="1001", item="", amount=57.0, date="2027-02-02")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "amount"), [])

	def test_a_wrong_amount_out_of_period_is_still_an_amount_difference(self):
		# Timing must not become a place for real differences to hide.
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "", "amount": 57.0,
			  "date": "2027-01-30", "description": ""}],
			[_pos(order="1001", item="", amount=40.0, date="2027-02-02")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(len(_by_status(rows, "amount")), 1)
		self.assertEqual(_by_status(rows, "timing"), [])

	def test_an_order_posted_inside_the_period_is_plainly_matched(self):
		rows = sr._reconcile(
			[{"order_id": "1001", "item_id": "", "amount": 57.0,
			  "date": "2027-01-05", "description": ""}],
			[_pos(order="1001", item="", amount=57.0, date="2027-01-06")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(len(_by_status(rows, "matched")), 1)


class TimingSurvivesTheFetch(FrappeTestCase):
	"""The tests above hand `_reconcile` an out-of-period POS row. The fetch used
	to keep only invoices posted inside the period, so through `execute()` no
	such row ever arrived: a January-statement order invoiced on 2 February was
	reported "On statement, not in POS" and the Timing card was always 0."""

	def setUp(self):
		if not sr._gb_installed():
			self.skipTest("GunBroker Order is not installed on this site")
		tag = frappe.generate_hash(length=6)
		self.straddling = self._order(f"GBSR-{tag}-1", "2027-02-02", 89.99)
		self.next_month = self._order(f"GBSR-{tag}-2", "2027-02-03", 12.00)

	def _order(self, gb_order_id, posting_date, tax):
		si = frappe.new_doc("Sales Invoice")
		si.update({"name": f"SI-{gb_order_id}", "docstatus": 1,
			"posting_date": posting_date})
		si.db_insert()
		order = frappe.new_doc("GunBroker Order")
		order.update({"name": f"ORD-{gb_order_id}", "gb_order_id": gb_order_id,
			"order_status": "Transferred", "sales_invoice": si.name,
			"sales_tax_total": tax, "compliance_fee": 0})
		order.db_insert()
		return gb_order_id

	def _run(self):
		_columns, rows, *_rest = sr.execute({
			"statement_type": sr.TAX_STATEMENT,
			"from_date": "2027-01-01", "to_date": "2027-01-31",
			"statement_text": f"Order ID,Sales Tax\n{self.straddling},89.99\n"})
		return rows

	def test_a_statement_order_invoiced_next_month_is_a_timing_gap(self):
		mine = [r for r in self._run() if r["order_id"] == self.straddling]
		self.assertEqual([r["status"] for r in mine], ["timing"])
		self.assertEqual(str(mine[0]["posting_date"]), "2027-02-02")

	def test_next_months_orders_the_statement_does_not_list_stay_out(self):
		rows = self._run()
		self.assertEqual([r for r in rows if r["order_id"] == self.next_month], [])


class TheOperatorSeesTheThreeCounts(FrappeTestCase):
	def test_each_class_is_counted_on_its_own_card(self):
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(order="1001", amount=53.0), _pos(order="1009", amount=10.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		cards = {c["label"]: c["value"] for c in sr._summary(rows)}
		self.assertEqual(cards["Amount differs"], 1)
		self.assertEqual(cards["Missing in POS"], 1)
		self.assertEqual(cards["Missing on statement"], 1)

	def test_a_clean_reconciliation_reads_as_clean(self):
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(order="1001", amount=57.0),
			 _pos(order="1002", item="55502", amount=40.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		cards = {c["label"]: c["value"] for c in sr._summary(rows)}
		self.assertEqual(cards["Amount differs"], "✓ Clean")
		self.assertEqual(cards["Missing in POS"], "✓ Clean")

	def test_the_statement_and_pos_totals_are_both_on_the_strip(self):
		# The first check anyone does is "does the bill total what we think".
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(order="1001", amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		cards = {c["label"]: c["value"] for c in sr._summary(rows)}
		self.assertAlmostEqual(cards["Statement total"], 97.0)
		self.assertAlmostEqual(cards["POS total"], 57.0)


class AnIdThatWentThroughExcel(FrappeTestCase):
	"""The parser normalises HEADERS carefully and then
	compares the key CELLS raw. A spreadsheet renders a numeric ID column as
	`8888888.0` and drops leading zeros, and when it does, every row lands in
	`missing_in_pos` AND `missing_on_statement` at once — the same "whole month
	is missing" misreading that refusing a headerless file exists to prevent."""

	def test_a_trailing_decimal_zero_is_the_same_id(self):
		self.assertEqual(sr._key("8888888.0", ""), sr._key("8888888", ""))

	def test_it_matches_across_the_two_sides(self):
		rows = sr._reconcile(
			[{"order_id": "1001.0", "item_id": "55501.0", "amount": 57.0,
			  "date": None, "description": ""}],
			[_pos(order="1001", item="55501", amount=57.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(len(_by_status(rows, "matched")), 1)

	def test_a_genuinely_different_id_still_differs(self):
		# The normalisation must not collapse ids that are actually distinct.
		self.assertNotEqual(sr._key("1001", ""), sr._key("10010", ""))

	def test_a_non_numeric_id_is_left_alone(self):
		# Order references are not guaranteed numeric; do not get clever.
		self.assertEqual(sr._key("GB-1001", ""), ("GB-1001", ""))

	def test_a_decimal_that_is_not_a_rounding_artefact_is_left_alone(self):
		self.assertEqual(sr._key("1001.5", ""), ("1001.5", ""))


class TheAccrualBehindEachPosRow(FrappeTestCase):
	"""What is reconciled is the per-item figure the order carries,
	which is a CALCULATION. The statement is really being checked against the
	accrual on the ledger, and those two come apart exactly when it matters:
	the accrual journal failed to post, or was reversed on a cancellation and
	never re-raised. Both show as a clean Match today."""

	def test_a_posted_accrual_is_reported_as_such(self):
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(amount=57.0, accrual="posted")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "matched")[0]["accrual"], "posted")

	def test_a_missing_accrual_is_visible_on_a_matching_row(self):
		# The amounts agree — they are both derived from the same order — and
		# nothing is on the ledger. That is the case worth seeing.
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(amount=57.0, accrual="none")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "matched")[0]["accrual"], "none")

	def test_a_reversed_accrual_is_visible(self):
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(amount=57.0, accrual="reversed")],
			from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "matched")[0]["accrual"], "reversed")

	def test_the_column_exists(self):
		self.assertIn("accrual", [c["fieldname"] for c in sr._columns()])

	def test_a_row_with_no_pos_side_has_no_accrual_state(self):
		rows = sr._reconcile(
			[{"order_id": "9999", "item_id": "", "amount": 10.0, "date": None,
			  "description": ""}], [], from_date="2027-01-01", to_date="2027-01-31")
		self.assertEqual(_by_status(rows, "missing_in_pos")[0]["accrual"], "")


class OnlyPostedInvoicesAreOnThePosSide(FrappeTestCase):
	"""The per-item fee is written when the invoice posts. An order
	still waiting for payment or an FFL therefore carries 0.00, and comparing
	it against a real statement line reads as "GunBroker is billing us 57 and
	we accrued nothing" — an alarm, on an order that has simply not shipped
	yet. On the tax statement it is worse: the tax figure IS populated on an
	unpaid order, so it shows up as a red "in POS, not on statement" row."""

	def _fee_query(self):
		module = ("ffl_core.ffl_core.report.gunbroker_statement_reconciliation"
			".gunbroker_statement_reconciliation")
		with patch(f"{module}._gb_installed", return_value=True), \
				patch("frappe.db.sql", return_value=[]) as sql:
			sr._fetch_pos_fees(frappe._dict({"from_date": "2027-01-01",
				"to_date": "2027-01-31"}))
		return " ".join(sql.call_args[0][0].split())

	def _tax_query(self):
		module = ("ffl_core.ffl_core.report.gunbroker_statement_reconciliation"
			".gunbroker_statement_reconciliation")
		with patch(f"{module}._gb_installed", return_value=True), \
				patch("frappe.db.sql", return_value=[]) as sql:
			sr._fetch_pos_tax(frappe._dict({"from_date": "2027-01-01",
				"to_date": "2027-01-31"}))
		return " ".join(sql.call_args[0][0].split())

	def test_the_fee_side_joins_only_posted_invoices(self):
		query = self._fee_query()
		self.assertIn("JOIN `tabSales Invoice` si", query)
		self.assertNotIn("LEFT JOIN `tabSales Invoice` si", query)
		self.assertIn("si.docstatus = 1", query)

	def test_the_tax_side_joins_only_posted_invoices(self):
		query = self._tax_query()
		self.assertIn("JOIN `tabSales Invoice` si", query)
		self.assertNotIn("LEFT JOIN `tabSales Invoice` si", query)
		self.assertIn("si.docstatus = 1", query)

	def test_the_fee_side_reports_the_ledger_state_of_the_accrual(self):
		query = self._fee_query()
		self.assertIn("reversal_of", query)
		self.assertIn("fee_accrual_je", query)


class TheEntryRefusesWhatItCannotReconcile(FrappeTestCase):
	"""Every one of these is what an operator hits on the
	first run, and none of them had a test."""

	def _run(self, **filters):
		return sr.execute(filters)

	def test_an_unknown_statement_type_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._run(statement_type="Invoice", from_date="2027-01-01",
				to_date="2027-01-31")
		self.assertIn("Invoice", str(caught.exception))

	def test_a_missing_period_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._run(statement_type="Fee Statement")
		self.assertIn("timing", str(caught.exception).lower())

	def test_a_backwards_period_is_refused(self):
		with self.assertRaises(Exception):
			self._run(statement_type="Fee Statement", from_date="2027-01-31",
				to_date="2027-01-01")

	def test_no_file_and_no_paste_is_refused_by_name(self):
		with self.assertRaises(Exception) as caught:
			self._run(statement_type="Fee Statement", from_date="2027-01-01",
				to_date="2027-01-31")
		self.assertIn("paste", str(caught.exception).lower())


class TheCardsComeFromOnePlace(FrappeTestCase):
	""""✓ Clean" and its green/red rule were written twice —
	once in the Sales Report, once here — and two copies of a convention drift
	into two conventions."""

	def test_the_summary_uses_the_shared_builders(self):
		from ffl_core import report_cards

		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(order="1001", amount=57.0),
			 _pos(order="1002", item="55502", amount=40.0)],
			from_date="2027-01-01", to_date="2027-01-31")
		clean = next(c for c in sr._summary(rows)
			if c["label"] == "Amount differs")
		self.assertEqual(clean, report_cards.flag_card(0, "Amount differs"))

	def test_the_clean_wording_has_exactly_one_source(self):
		from ffl_core import report_cards
		from ffl_core.ffl_core.report.sales_report import sales_report

		self.assertIs(sales_report._flag_card, report_cards.flag_card)
		self.assertIs(sales_report._card, report_cards.card)


class TheAccrualStatesAgreeAcrossTheLanguageBoundary(FrappeTestCase):
	"""The SQL names four accrual states; the grid's formatter decides which of
	them are red. Two files, no compiler between them.

	Both drifts are silent. Add a state server-side and the formatter renders it
	plain — a row that should stop the reader goes by unmarked. Rename one and
	the rule stops matching anything, with the column still full of correct
	text. Neither raises, and the page looks fine in both cases."""

	def _states(self):
		import re
		from pathlib import Path

		import ffl_core
		source = (Path(ffl_core.__file__).resolve().parent / "ffl_core" / "report"
			/ "gunbroker_statement_reconciliation"
			/ "gunbroker_statement_reconciliation.js").read_text(encoding="utf-8")
		block = re.search(r"GBSR_ACCRUAL_ALARM = new Set\(\[(.*?)\]\)", source, re.S)
		self.assertIsNotNone(block, "the alarm set is not a literal any more")
		return set(re.findall(r'"([^"]+)"', block.group(1)))

	def test_the_server_emits_exactly_the_four_states_the_grid_knows(self):
		emitted = set(re.findall(r"THEN '([a-z]+)'", sr._ACCRUAL_STATE))
		emitted.add("posted")   # the ELSE arm
		self.assertEqual(emitted, {"none", "cancelled", "reversed", "posted"})

	def test_every_state_the_grid_alarms_on_is_one_the_server_can_emit(self):
		emitted = set(re.findall(r"THEN '([a-z]+)'", sr._ACCRUAL_STATE)) | {"posted"}
		self.assertEqual(self._states() - emitted, set(),
			"the grid alarms on a state nothing produces")

	def test_the_only_state_left_unmarked_is_the_good_one(self):
		emitted = set(re.findall(r"THEN '([a-z]+)'", sr._ACCRUAL_STATE)) | {"posted"}
		self.assertEqual(emitted - self._states(), {"posted"},
			"a state the server emits renders unmarked in the grid")


class TheColumnSet(FrappeTestCase):
	DATA_ONLY = {"status"}


	def test_every_key_a_row_writes_has_a_column(self):
		declared = {c["fieldname"] for c in sr._columns()} | self.DATA_ONLY
		rows = sr._reconcile(sr._parse_statement(FEE_CSV),
			[_pos(order="1001", amount=53.0), _pos(order="1009", amount=10.0,
				date="2027-02-05")],
			from_date="2027-01-01", to_date="2027-01-31")
		written = set()
		for row in rows:
			written |= set(row)
		self.assertEqual(written - declared, set())

	def test_the_preamble_says_the_format_is_not_yet_sampled(self):
		# Nobody has exported a real statement. An operator whose file parses
		# oddly needs to know that before they conclude the accruals are wrong.
		text = sr._preamble("Fee Statement")
		self.assertIn("not yet been sampled", text)

	def test_the_preamble_names_the_statement_being_reconciled(self):
		self.assertIn("Fee Statement", sr._preamble("Fee Statement"))
		self.assertIn("Sales Tax Statement", sr._preamble("Sales Tax Statement"))
