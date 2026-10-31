"""Booking a GunBroker sale — the arithmetic and the refusals.

Three of these tests exist because getting them wrong is invisible until an
accountant or a tax authority finds it months later:

* the final value fee is progressive and is billed PER GUN. Running one order
  total through the tiers charges the cheap upper bands once instead of once per
  gun — $88 where GunBroker bills $96 on a two-rifle order. It reconciles to the
  penny on every single-gun order, which is most of them, so nothing would look
  wrong for a long time.
* two of the tax rows are money collected for somebody else. Posting them where
  the store's own sales tax goes would overstate the Texas return by every
  marketplace sale.
* the receipt has to post on the invoice's date. These orders are paid before
  they are invoiced — always — and POS refuses a receipt dated before the
  invoice it settles, so the "obvious" choice fails on every order and leaves
  the cash off the ledger with the gun already gone.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import revenue
from ffl_integrations.tests import gunbroker_fixtures as fx


def _tier(upper, rate):
	return frappe._dict({"upper_bound": upper, "rate": rate})


# GunBroker's published firearm tiers: 6% to $400, 4% to $15,000, 3% to
# $20,000, 2% above.
TIERS = [_tier(400, 6), _tier(15000, 4), _tier(20000, 3), _tier(0, 2)]


class _RevenueCase(FrappeTestCase):
	def setUp(self):
		self.settings = fx.settings_stub(fee_tiers=TIERS, ffl_doc_fee=5.00)
		# Warm the System Settings client cache before anything is patched:
		# nowdate() loads it through frappe.client_cache, whose miss path calls
		# frappe.get_doc — a test's return_value=order patch would hand it a
		# FakeDoc to pickle into redis under the real key. Only visible on a
		# cold cache (a module run on its own); the full suite warmed it first.
		frappe.get_system_settings("time_zone")
		self.written: dict = {}
		self.alerts: list[str] = []
		self.logs: list[str] = []

		def _set_value(doctype, name, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.written.setdefault((doctype, name), {}).update(updates)

		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch("frappe.get_single", return_value=self.settings)
		self._patch("frappe.get_cached_value", return_value="TSA")
		self.alert_bodies: list[str] = []

		def _notify(subject, message, **kw):
			self.alerts.append(kw["dedup_key"])
			self.alert_bodies.append(message)

		self._patch_object(revenue, "notify_admins", side_effect=_notify)
		self._patch_object(revenue, "safe_log_error",
			side_effect=lambda title, msg: self.logs.append(title))

	def _patch(self, target, *args, **kw):
		patcher = patch(target, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _order(self, **fields):
		defaults = {"name": "GB-ORD-00001", "gb_order_id": "998877",
			"order_status": "Transferred"}
		defaults.update(fields)
		return fx.FakeDoc(**defaults)

	def _order_with_lines(self, detail=None, **fields):
		from ffl_integrations.gunbroker import orders as gb_orders

		order = self._order(**fields)
		detail = detail or fx.order_detail()
		with patch("frappe.db.exists", return_value=True):
			gb_orders.apply_order_fields(order, detail)
			gb_orders.apply_order_lines(order, detail)
		order.order_status = "Transferred"
		for line in order.lines:
			order.append("serials", {"item_code": line.item_code,
				"gb_item_id": line.gb_item_id,
				"confirmed_serial": line.serial_number})
		return order


# ---------------------------------------------------------------------------
# The seller fee
# ---------------------------------------------------------------------------

class TestFinalValueFee(FrappeTestCase):
	def test_a_price_inside_the_first_band_pays_one_rate(self):
		self.assertEqual(revenue.final_value_fee(300, TIERS), 18.00)

	def test_a_price_spanning_two_bands_pays_each_band_its_own_rate(self):
		# 400 at 6% + 600 at 4%.
		self.assertEqual(revenue.final_value_fee(1000, TIERS), 48.00)

	def test_the_open_ended_top_band_applies_above_the_last_bound(self):
		# 400@6 + 14,600@4 + 5,000@3 + 5,000@2 = 24 + 584 + 150 + 100.
		self.assertEqual(revenue.final_value_fee(25000, TIERS), 858.00)

	def test_two_guns_are_billed_per_gun_and_not_per_order(self):
		# THE case. Per gun: 2 x 48 = 96. Per order (wrong): 400@6 + 1,600@4
		# = 88. The difference is systematic and always in GunBroker's favour,
		# and it only shows up on multi-gun orders — so a single-gun store sees
		# a perfect reconciliation for months.
		per_gun = revenue.final_value_fee(1000, TIERS) * 2
		per_order = revenue.final_value_fee(2000, TIERS)
		self.assertEqual(per_gun, 96.00)
		self.assertEqual(per_order, 88.00)

	def test_each_gun_rounds_on_its_own(self):
		# GunBroker bills per line, so the rounding has to happen per line too
		# or the statement disagrees by a cent or two per gun.
		self.assertEqual(revenue.final_value_fee(333.33, TIERS), 20.00)

	def test_no_tiers_configured_charges_nothing_rather_than_guessing(self):
		self.assertEqual(revenue.final_value_fee(1000, []), 0.0)


class TestLineFees(_RevenueCase):
	def test_each_line_stores_its_own_fee(self):
		order = self._order_with_lines(fx.order_detail(item_price=1000.00))
		fvf, doc_fee = revenue._line_fees(order)
		self.assertEqual(fvf, 48.00)
		self.assertEqual(doc_fee, 5.00)
		self.assertEqual(order.lines[0].fvf_amount, 48.00)
		self.assertEqual(order.lines[0].ffl_doc_fee_amount, 5.00)

	def test_two_guns_accrue_ninety_six(self):
		detail = fx.order_detail(item_price=1000.00, extra_items=[
			fx.order_item(16087231, sku="COLT::SN-002", serial="SN-002", price=1000.00)])
		order = self._order_with_lines(detail)
		fvf, doc_fee = revenue._line_fees(order)
		self.assertEqual(fvf, 96.00)
		self.assertEqual(doc_fee, 10.00)

	def test_the_document_fee_is_charged_per_gun_like_the_final_value_fee(self):
		# GunBroker bills the FFL document fee per firearm, and a line of
		# two is two firearms. It cannot happen on a serialised listing today,
		# which is exactly why it would go unnoticed the first time a
		# non-serialised one is sold — the FVF beside it was already per unit.
		order = self._order_with_lines()
		order.lines[0].qty = 2
		order.lines[0].fvf_amount = 0
		order.lines[0].ffl_doc_fee_amount = 0
		_fvf, doc_fee = revenue._line_fees(order)
		self.assertEqual(doc_fee, 10.00)

	def test_a_line_that_needs_no_licence_pays_no_document_fee(self):
		detail = fx.order_detail(item_price=1000.00, extra_items=[
			fx.order_item(16087231, sku="AMMO", serial="", price=40.00,
				ffl_required=False)])
		order = self._order_with_lines(detail)
		_fvf, doc_fee = revenue._line_fees(order)
		self.assertEqual(doc_fee, 5.00)

	def test_an_already_accrued_line_is_not_re_priced(self):
		# A part refund does not reduce what GunBroker bills, so recomputing
		# would quietly re-price a liability that has already been journalled.
		order = self._order_with_lines(fx.order_detail(item_price=1000.00))
		revenue._line_fees(order)
		order.lines[0].unit_price = 500.00
		fvf, _doc = revenue._line_fees(order)
		self.assertEqual(fvf, 48.00)


class TestFeeAccrual(_RevenueCase):
	"""The journal itself, not just the link back to it.

	Everything that can be wrong about an accrual is inside the document —
	which account is debited, which is credited, for how much, on what date.
	Asserting only that the order came back with a Journal Entry name on it
	passes just as happily with the two accounts the other way round.
	"""

	def _accrue(self, si, **kw):
		order = self._order(fee_accrual_je=None, **kw)
		captured = {}

		def _get_doc(spec):
			captured.update(spec)
			return fx.FakeDoc("Journal Entry", name="ACC-JV-1")

		with patch("frappe.get_doc", side_effect=_get_doc), \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue, "_cost_center", return_value="Main - TSA"):
			revenue._accrue_fees(order, si)
		return order, captured

	def _si(self, **kw):
		defaults = {"name": "ACC-SINV-1", "company": "TSA",
			"posting_date": "2026-08-26", "gb_final_value_fee": 48.0,
			"gb_ffl_doc_fee": 5.0}
		defaults.update(kw)
		return fx.FakeDoc("Sales Invoice", **defaults)

	def test_the_expense_is_debited_and_the_liability_credited(self):
		order, spec = self._accrue(self._si())
		self.assertEqual(order.fee_accrual_je, "ACC-JV-1")
		self.assertEqual(spec["voucher_type"], "Journal Entry")
		self.assertEqual(spec["accounts"][0], {
			"account": "Marketplace Fees - GunBroker - TSA",
			"debit_in_account_currency": 53.0,
			"cost_center": "Main - TSA"})
		self.assertEqual(spec["accounts"][1], {
			"account": "Accrued GunBroker Fees - TSA",
			"credit_in_account_currency": 53.0,
			"cost_center": "Main - TSA"})

	def test_the_journal_takes_the_invoices_date(self):
		_order, spec = self._accrue(self._si(posting_date="2026-07-31"))
		self.assertEqual(spec["posting_date"], "2026-07-31")

	def test_a_frozen_month_moves_the_journal_to_today_rather_than_failing(self):
		# Without the escape hatch a retry after the month closes cannot
		# build the journal at all, and si_no_fee_je then reports the order
		# every single day with nothing anybody can do about it.
		si = self._si(posting_date="2026-07-31")
		captured = {}

		def _get_doc(spec):
			captured.update(spec)
			return fx.FakeDoc("Journal Entry", name="ACC-JV-1")

		with patch("frappe.get_doc", side_effect=_get_doc), \
				patch("frappe.db.get_value", return_value="2026-07-31"), \
				patch.object(revenue, "_cost_center", return_value="Main - TSA"):
			revenue._accrue_fees(self._order(fee_accrual_je=None), si)
		self.assertEqual(captured["posting_date"], frappe.utils.nowdate())

	def test_an_amendments_re_accrual_is_dated_today_not_the_original_month(self):
		# The re-accrual pairs with the reversal that went out with the
		# cancel, and that reversal is dated today. Desk copies posting_date on
		# an amend, so leaving this on the invoice's date puts the fee back
		# into the month the reversal just took it out of: that month shows the
		# fee twice, this month shows it once negative, the running balance is
		# right and neither monthly figure agrees with GunBroker's statement.
		_order, spec = self._accrue(
			self._si(posting_date="2026-07-31", amended_from="ACC-SINV-0"))
		# Today, whenever today is — not the date the offline stub happens to
		# freeze. The point is that it is NOT the invoice's month.
		self.assertEqual(spec["posting_date"], frappe.utils.nowdate())
		self.assertNotEqual(spec["posting_date"], "2026-07-31")

	def test_a_first_submission_still_takes_the_invoices_date(self):
		_order, spec = self._accrue(self._si(posting_date="2026-07-31"))
		self.assertEqual(spec["posting_date"], "2026-07-31")

	def test_two_guns_accrue_the_sum_of_both_fees(self):
		_order, spec = self._accrue(
			self._si(gb_final_value_fee=96.0, gb_ffl_doc_fee=10.0))
		self.assertEqual(spec["accounts"][0]["debit_in_account_currency"], 106.0)
		self.assertEqual(spec["accounts"][1]["credit_in_account_currency"], 106.0)

	def test_a_retry_does_not_accrue_the_fee_twice(self):
		# GunBroker bills once. The invoice booking is retried freely.
		order = self._order(fee_accrual_je="ACC-JV-1")
		si = fx.FakeDoc("Sales Invoice", gb_final_value_fee=48.0, gb_ffl_doc_fee=5.0)
		with patch("frappe.get_doc") as get_doc:
			revenue._accrue_fees(order, si)
		get_doc.assert_not_called()

	def test_a_failed_accrual_leaves_the_sale_booked(self):
		# The sale is on the ledger; the accrual is a separate, retryable fact,
		# and the daily audit reports an invoice with none against it.
		order = self._order(fee_accrual_je=None)
		si = self._si()
		with patch("frappe.get_doc", side_effect=RuntimeError("no such account")), \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue, "_cost_center", return_value="Main - TSA"):
			revenue._accrue_fees(order, si)
		self.assertIsNone(order.fee_accrual_je)
		self.assertTrue(self.logs)


class TestReversingTheAccrual(_RevenueCase):
	"""Cancelling the invoice has to take the accrual with it.

	The accrual is its own Journal Entry and nothing cancels it on its own, so
	`Dr Marketplace Fees / Cr Accrued GunBroker Fees` would stay on the ledger
	against a sale that no longer exists — channel profit low for ever, and a
	monthly GunBroker statement that cannot be reconciled.
	"""

	def _si(self, **kw):
		defaults = {"name": "ACC-SINV-1", "gunbroker_order": "GB-ORD-00001"}
		defaults.update(kw)
		return fx.FakeDoc("Sales Invoice", **defaults)

	def test_the_journal_is_reversed_and_the_link_cleared(self):
		reverse = fx.FakeDoc("Journal Entry", name="ACC-JV-2")

		def _get_value(doctype, name, field=None, *a, **kw):
			if doctype == revenue.ORDER_DOCTYPE:
				return "ACC-JV-1"
			return 1  # the accrual is submitted

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch("erpnext.accounts.doctype.journal_entry.journal_entry."
					"make_reverse_journal_entry", return_value=reverse) as make:
			revenue.reverse_fee_accrual_on_cancel(self._si())
		make.assert_called_once_with("ACC-JV-1")
		self.assertEqual(reverse["docstatus"], 1)
		self.assertIsNone(self.written[(revenue.ORDER_DOCTYPE, "GB-ORD-00001")]
			["fee_accrual_je"])

	def test_an_invoice_that_is_not_a_gunbroker_one_is_left_alone(self):
		with patch("frappe.db.get_value") as get_value:
			revenue.reverse_fee_accrual_on_cancel(
				fx.FakeDoc("Sales Invoice", name="ACC-SINV-9"))
		get_value.assert_not_called()

	def test_an_order_with_no_accrual_is_left_alone(self):
		with patch("frappe.db.get_value", return_value=None), \
				patch("erpnext.accounts.doctype.journal_entry.journal_entry."
					"make_reverse_journal_entry") as make:
			revenue.reverse_fee_accrual_on_cancel(self._si())
		make.assert_not_called()

	def test_an_already_cancelled_journal_needs_no_reversal(self):
		def _get_value(doctype, name, field=None, *a, **kw):
			return "ACC-JV-1" if doctype == revenue.ORDER_DOCTYPE else 2

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch("erpnext.accounts.doctype.journal_entry.journal_entry."
					"make_reverse_journal_entry") as make:
			revenue.reverse_fee_accrual_on_cancel(self._si())
		make.assert_not_called()
		self.assertIsNone(self.written[(revenue.ORDER_DOCTYPE, "GB-ORD-00001")]
			["fee_accrual_je"])

	def test_a_reversal_that_fails_never_blocks_the_cancellation(self):
		# A cancel that throws leaves the store unable to cancel an invoice at
		# all, which is worse than an accrual somebody unwinds by hand — and the
		# daily audit reports the mismatch either way.
		def _get_value(doctype, name, field=None, *a, **kw):
			return "ACC-JV-1" if doctype == revenue.ORDER_DOCTYPE else 1

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch("erpnext.accounts.doctype.journal_entry.journal_entry."
					"make_reverse_journal_entry",
					side_effect=RuntimeError("period closed")):
			revenue.reverse_fee_accrual_on_cancel(self._si())  # must not raise
		self.assertIn("gb_accrual_unreversed_GB-ORD-00001", self.alerts)
		self.assertNotIn((revenue.ORDER_DOCTYPE, "GB-ORD-00001"), self.written)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class TestUnhandledAmounts(_RevenueCase):
	def test_a_clean_order_posts(self):
		self.assertEqual(revenue.unhandled_amounts(self._order_with_lines()), "")

	def test_tax_without_a_service_fee_stops_the_invoice(self):
		# The two are owed to different parties and salesTaxTotal is their sum.
		# Assuming the documented 1% would misfile real money the first time
		# GunBroker changed the rate — silently, and for every order.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.0, compliance_fee=0))
		reason = revenue.unhandled_amounts(order)
		self.assertIn("complianceFee", reason)

	def test_a_no_tax_state_still_carries_the_buyers_service_fee(self):
		# Oregon, Montana, New Hampshire, Delaware, Alaska — or a tax-exempt
		# buyer. The state takes nothing; GunBroker still charges the buyer its
		# 1%, and that 1% rides inside salesTaxTotal. So the honest shape of a
		# no-tax order is salesTaxTotal == complianceFee, not both zero, and
		# what comes out is the MSF row on its own.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=11.00, compliance_fee=11.00))
		self.assertEqual(revenue.unhandled_amounts(order), "")
		rows = revenue._tax_rows(order, "TSA")
		self.assertEqual([r["account_head"] for r in rows],
			["GunBroker Marketplace Fee Payable - TSA"])
		self.assertEqual(rows[0]["tax_amount"], 11.00)

	def test_both_figures_zero_is_a_missing_fee_like_any_other(self):
		# A Currency column cannot hold "GunBroker did not send this
		# key", so zero has to mean absent — and it means absent whatever
		# salesTaxTotal says, because the service fee is charged on every
		# purchase. Letting this through was also what opened the door to the
		# empty-taxes-table injection and to the negative row below.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=0, compliance_fee=0))
		self.assertIn("complianceFee", revenue.unhandled_amounts(order))

	def test_the_refusal_warns_against_submitting_the_draft_it_produces(self):
		# Since _tax_rows stopped throwing, the Draft this case
		# leaves behind is exactly the shape the comment above warns about:
		# MSF row present, MPF row missing. Nothing stops an operator
		# submitting it from the Desk, so the words have to.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=5.00, compliance_fee=11.00))
		reason = revenue.unhandled_amounts(order)
		self.assertIn("Do NOT submit", reason)
		self.assertIn("marketplace sales tax", reason)

	def test_a_fee_larger_than_the_tax_figure_stops_the_invoice(self):
		# If salesTaxTotal turns out NOT to contain the service fee (research
		# item #2, never verified on a real order), a no-tax buyer produces
		# {MPF −11.00, MSF +11.00}. The drift guard would hold that in Draft,
		# but the obvious repair — delete the negative row, submit — books a fee
		# with no tax behind it.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=5.00, compliance_fee=11.00))
		self.assertIn("negative", revenue.unhandled_amounts(order))

	def test_the_row_builder_leaves_the_negative_row_off_rather_than_throwing(self):
		# It used to throw, which took the Draft away with it: the only
		# automatic caller is the dispose step, whose blanket handler turns an
		# exception into one line in the Error Log — on an order whose guns
		# have already gone. unhandled_amounts refuses this before anything is
		# built; what has to survive is the Draft somebody can look at.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=5.00, compliance_fee=11.00))
		heads = [r["account_head"] for r in revenue._tax_rows(order, "TSA")]
		self.assertEqual(heads, ["GunBroker Marketplace Fee Payable - TSA"])

	def test_per_line_tax_above_the_marketplace_tax_stops_the_invoice(self):
		# The same question from the other side, for free. Freight tax can
		# only make the order-level figure bigger, so the per-line sum can never
		# exceed what is left after the service fee comes out. The first real
		# order settles research item #2 either way.
		order = self._order_with_lines(fx.order_detail(
			sales_tax_total=50.00, compliance_fee=11.00))
		order.lines[0].line_tax = 45.00
		self.assertIn("per-line sales tax", revenue.unhandled_amounts(order))

	def test_the_ordinary_order_passes_that_check(self):
		order = self._order_with_lines()
		self.assertEqual(revenue.unhandled_amounts(order), "")

	def test_an_amount_this_phase_has_never_seen_stops_the_invoice(self):
		order = self._order_with_lines(fx.order_detail(couponValue=25.00))
		reason = revenue.unhandled_amounts(order)
		self.assertIn("couponValue", reason)
		self.assertIn("25.00", reason)

	def test_every_unhandled_amount_is_named(self):
		order = self._order_with_lines(
			fx.order_detail(miscFee=3.00, lostCashDiscount=7.00))
		reason = revenue.unhandled_amounts(order)
		self.assertIn("miscFee", reason)
		self.assertIn("lostCashDiscount", reason)


# ---------------------------------------------------------------------------
# The invoice
# ---------------------------------------------------------------------------

class TestTaxRows(_RevenueCase):
	def test_the_two_marketplace_liabilities_are_split_apart(self):
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.99, compliance_fee=11.00))
		rows = revenue._tax_rows(order, "TSA")
		self.assertEqual(len(rows), 2)
		amounts = {r["account_head"]: r["tax_amount"] for r in rows}
		self.assertEqual(amounts["GunBroker MPF Sales Tax Payable - TSA"], 89.99)
		self.assertEqual(amounts["GunBroker Marketplace Fee Payable - TSA"], 11.00)

	def test_neither_row_touches_the_stores_own_sales_tax_account(self):
		# The Texas return is built from that account. A marketplace sale
		# belongs in Total Sales and NOT in Taxable Sales.
		order = self._order_with_lines()
		heads = [r["account_head"] for r in revenue._tax_rows(order, "TSA")]
		self.assertTrue(all(h.startswith("GunBroker ") for h in heads), heads)

	def test_both_figures_zero_yields_no_rows_and_is_refused_upstream(self):
		# A zero row on a ledger is a question somebody has to answer later —
		# and this shape never reaches the builder in production anyway:
		# complianceFee of zero means GunBroker did not send it, and
		# unhandled_amounts stops the order before anything is built. (A real
		# no-tax state is salesTaxTotal == complianceFee; see TestUnhandledAmounts.)
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=0, compliance_fee=0))
		self.assertEqual(revenue._tax_rows(order, "TSA"), [])
		self.assertIn("complianceFee", revenue.unhandled_amounts(order))

	def test_every_row_carries_the_order_number_for_the_reconciliation(self):
		order = self._order_with_lines()
		for row in revenue._tax_rows(order, "TSA"):
			self.assertIn("998877", row["description"])

	def test_every_row_carries_a_cost_centre(self):
		# Without one the invoice saves happily and then fails at submit with a
		# cryptic GL error.
		order = self._order_with_lines()
		with patch("frappe.db.get_value", return_value="Main - TSA"):
			rows = revenue._tax_rows(order, "TSA")
		self.assertTrue(all(r["cost_center"] for r in rows))


class TestTheTaxRowWhitelist(_RevenueCase):
	"""ERPNext fills an empty taxes table in, and we would not notice.

	`set_taxes_and_charges()` runs inside the controller's validate. It finds no
	rows — the freight row is appended by ffl_core's hook AFTER the controller —
	and with `add_taxes_from_item_tax_template` on, which it is in production,
	it appends one per Item Tax Template on the lines. The three ffl_core hooks
	that step aside for a channel-mirrored invoice only skip; none of them
	deletes a row. What comes out is the store's own Sales Tax Payable on a
	marketplace sale, which is the one account the Texas return is built from
	and the one account GunBroker already filed the tax for.
	"""

	def _si(self, rows):
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", grand_total=1245.99)
		si["taxes"] = rows
		si["docstatus"] = 0
		return si

	def test_the_three_heads_this_module_writes_are_accepted(self):
		si = self._si([
			{"account_head": "GunBroker MPF Sales Tax Payable - TSA",
				"tax_amount": 89.99},
			{"account_head": "GunBroker Marketplace Fee Payable - TSA",
				"tax_amount": 11.00},
			{"account_head": "Shipping Income - TSA", "tax_amount": 45.00},
		])
		with patch("frappe.get_cached_value", return_value="TSA"):
			self.assertEqual(revenue._unexpected_tax_rows(si, "TSA"), "")

	def test_the_stores_own_sales_tax_row_stops_the_invoice(self):
		si = self._si([
			{"account_head": "GunBroker MPF Sales Tax Payable - TSA",
				"tax_amount": 89.99},
			{"account_head": "Sales Tax Payable - TSA", "tax_amount": 82.50},
		])
		with patch("frappe.get_cached_value", return_value="TSA"):
			reason = revenue._unexpected_tax_rows(si, "TSA")
		self.assertIn("Sales Tax Payable - TSA", reason)
		self.assertIn("82.50", reason)

	def test_an_injected_row_holds_the_whole_booking_in_draft(self):
		# The end to end of it: nothing is submitted, the order is linked to
		# the Draft so a retry can find it, and somebody is told why.
		order = self._order_with_lines()
		si = self._si([{"account_head": "Sales Tax Payable - TSA",
			"tax_amount": 82.50}])
		with patch("frappe.new_doc", return_value=si), \
				patch("frappe.get_doc", return_value=si), \
				patch("frappe.db.get_value", return_value=None), \
				patch("frappe.get_cached_value", return_value="TSA"), \
				patch("frappe.log_error"), \
				patch.object(revenue, "resolve_customer", return_value="CUST-1"), \
				patch.object(revenue, "_cost_center", return_value="Main - TSA"), \
				patch.object(revenue, "_apply_derived"), \
				patch.object(revenue, "_default_company", return_value="TSA"):
			out = revenue.create_sales_invoice_for_gb_order(order)
		self.assertEqual(out["status"], "draft_unhandled_amounts")
		self.assertEqual(si["docstatus"], 0)
		self.assertIn("gb_invoice_draft_998877", self.alerts)


class TestTheRealEntryPoint(_RevenueCase):
	"""Through create_sales_invoice_for_gb_order, not into its parts.

	Both tests passed while the behaviour they described was unreachable:
	one called `unhandled_amounts` directly, the other `_tax_rows` directly,
	and the defect was the ORDER the entry point called them in. The invoice
	was being built before the arithmetic was judged, so the row builder threw
	first — and the only automatic caller is the dispose step, whose blanket
	handler turns that into one line in the Error Log on an order whose guns
	have already left. Draft, alert and a document somebody can open, all gone.
	"""

	def setUp(self):
		super().setUp()
		self.si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-00001")
		self.si["docstatus"] = 0
		self.si["grand_total"] = 1245.99
		self._patch("frappe.new_doc", return_value=self.si)
		self._patch("frappe.get_doc", return_value=self.si)
		self._patch("frappe.log_error")
		self._patch("frappe.get_cached_value", return_value="TSA")
		self._patch_object(revenue, "resolve_customer", return_value="CUST-1")
		self._patch_object(revenue, "_cost_center", return_value="Main - TSA")
		self._patch_object(revenue, "_default_company", return_value="TSA")
		self._patch_object(revenue, "_accrue_fees")
		self._patch_object(revenue, "_ensure_paid")

	def _book(self, order):
		with patch("frappe.db.get_value", return_value=None):
			return revenue.create_sales_invoice_for_gb_order(order)

	def test_a_fee_larger_than_the_tax_leaves_a_draft_and_says_so(self):
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=5.00, compliance_fee=11.00))
		out = self._book(order)
		self.assertEqual(out["status"], "draft_unhandled_amounts")
		self.assertEqual(self.si["docstatus"], 0)
		self.assertIn("gb_invoice_draft_998877", self.alerts)
		self.assertIn("below complianceFee", self.logged_reason())

	def test_the_reason_is_written_onto_the_invoice_itself(self):
		# The Error Log and the email both name it, but the person who
		# eventually opens this document is looking at the document — and a
		# Draft held for a missing service fee looks exactly like an ordinary
		# no-tax sale from there.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.99, compliance_fee=0))
		self._book(order)
		self.assertTrue(self.si.comments)
		self.assertIn("complianceFee", self.si.comments[0])

	def test_a_missing_fee_leaves_a_draft_and_says_so(self):
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.99, compliance_fee=0))
		out = self._book(order)
		self.assertEqual(out["status"], "draft_unhandled_amounts")
		self.assertIn("gb_invoice_draft_998877", self.alerts)

	def test_the_negative_row_never_reaches_the_draft(self):
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=5.00, compliance_fee=11.00))
		self._book(order)
		for row in (self.si.get("taxes") or []):
			self.assertGreaterEqual(row["tax_amount"], 0, row)

	def test_the_drift_exit_goes_through_the_one_shared_draft_exit(self):
		# Two copies of log + link + comment + alert, same title,
		# same dedup key, same window, differing only in wording, is one
		# window-change away from drifting apart.
		order = self._order_with_lines()
		self.si["grand_total"] = 999.00
		with patch.object(revenue, "_leave_in_draft",
				return_value={"sales_invoice": "x", "status": "y"}) as leave:
			self._book(order)
		leave.assert_called_once()
		self.assertEqual(leave.call_args.kwargs["status"], "draft_total_mismatch")

	def test_a_total_that_does_not_agree_alerts_like_the_other_draft_exit(self):
		# Both Draft exits leave the same thing behind — a document nobody
		# has posted and a sale that is not on the ledger — so both have to
		# reach a person the same way. This one only wrote to the Error Log.
		order = self._order_with_lines()
		self.si["grand_total"] = 999.00
		out = self._book(order)
		self.assertEqual(out["status"], "draft_total_mismatch")
		self.assertEqual(self.si["docstatus"], 0)
		self.assertIn("gb_invoice_draft_998877", self.alerts)

	def test_a_negative_row_that_got_past_everything_stops_at_the_door(self):
		# The check had a direct test and no wiring test: deleting
		# the call from the entry point left all 83 green. It is defence in
		# depth today (unhandled_amounts refuses first, _tax_rows no longer
		# emits the row), and the lesson was precisely that an unreachable
		# defence with a green direct test is worth nothing.
		order = self._order_with_lines()
		negative = [{"charge_type": "Actual",
			"account_head": "GunBroker MPF Sales Tax Payable - TSA",
			"description": "x", "tax_amount": -6.00, "cost_center": "Main - TSA"}]
		with patch.object(revenue, "_tax_rows", return_value=negative), \
				self.assertRaises(Exception) as caught:
			self._book(order)
		self.assertIn("negative", str(caught.exception).lower())
		self.assertEqual(self.si["docstatus"], 0)

	def test_a_clean_order_still_posts(self):
		order = self._order_with_lines()
		out = self._book(order)
		self.assertEqual(out["status"], "created")
		self.assertEqual(self.si["docstatus"], 1)
		self.assertEqual(self.alerts, [])

	def logged_reason(self) -> str:
		return " ".join(str(a) for a in self.alert_bodies)


class TestAnAmendmentIsNotADraftToRefresh(_RevenueCase):
	"""Re-deriving one puts back the figure somebody deliberately changed.

	An amendment Draft exists BECAUSE a human altered something — a part refund
	agreed with the buyer, a line taken off. `_refresh_draft` rebuilds items,
	taxes and remarks from the GunBroker order, and GunBroker's own totalPrice
	does not move on a partial refund, so the drift guard agrees with the
	restored figure and it submits at the price nobody agreed to. The daily
	audit was walking operators straight into it ("Run Create Invoice").
	"""

	def test_an_amendment_draft_is_refused_rather_than_re_derived(self):
		order = self._order_with_lines()
		existing = frappe._dict({"name": "ACC-SINV-00001-1", "docstatus": 0,
			"amended_from": "ACC-SINV-00001"})
		with patch("frappe.db.get_value", return_value=existing) as get_value, \
				patch.object(revenue, "_apply_derived") as derive, \
				patch.object(revenue, "_default_company", return_value="TSA"), \
				self.assertRaises(Exception) as caught:
			revenue.create_sales_invoice_for_gb_order(order)
		self.assertIn("amendment", str(caught.exception).lower())
		derive.assert_not_called()
		# The guard reads a column the entry point has to ASK for. Take
		# "amended_from" out of that list and the guard reads None for ever,
		# silently, while a hand-built dict in a test goes on carrying it —
		# which is how this test passed against the defect it exists for.
		lookups = [c for c in get_value.call_args_list if c.args and c.args[0] == "Sales Invoice"]
		self.assertTrue(lookups, "the entry point never asked for the invoice")
		self.assertIn("amended_from", lookups[0].args[2])

	def test_the_lookup_asks_the_database_for_the_column_the_guard_reads(self):
		# The same contract said once more without the throw in the way, so a
		# reader can see what it is.
		order = self._order_with_lines()
		with patch("frappe.db.get_value", return_value=None) as get_value, \
				patch.object(revenue, "_default_company", return_value="TSA"), \
				patch.object(revenue, "_build_invoice",
					side_effect=RuntimeError("far enough")):
			with self.assertRaises(RuntimeError):
				revenue.create_sales_invoice_for_gb_order(order)
		doctype, filters, fields = get_value.call_args[0][:3]
		self.assertEqual(doctype, "Sales Invoice")
		self.assertEqual(filters["gunbroker_order"], order.name)
		self.assertEqual(fields, ["name", "docstatus", "amended_from"])

	def test_an_ordinary_draft_is_still_refreshed(self):
		order = self._order_with_lines()
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-00001", grand_total=1245.99)
		si["docstatus"] = 0
		existing = frappe._dict({"name": si.name, "docstatus": 0,
			"amended_from": None})
		with patch("frappe.db.get_value", return_value=existing), \
				patch("frappe.get_doc", return_value=si), \
				patch("frappe.get_cached_value", return_value="TSA"), \
				patch.object(revenue, "_cost_center", return_value="Main - TSA"), \
				patch.object(revenue, "_default_company", return_value="TSA"), \
				patch.object(revenue, "_accrue_fees"), \
				patch.object(revenue, "_ensure_paid"):
			out = revenue.create_sales_invoice_for_gb_order(order)
		self.assertEqual(out["status"], "created")


class TestAnAmendmentThatLostItsLink(_RevenueCase):
	"""It is invisible to the lookup, and the audit sends people here.

	`existing` is found by `gunbroker_order`. An amendment built through the
	REST API or the MCP tools can arrive without it — Desk copies the field,
	those do not necessarily — and then the order still points at the cancelled
	original, the daily audit says "run Create Invoice", and that raises a
	SECOND live invoice for one sale: gross receipts twice, a second accrual
	(the cancel cleared the first link), both dated today.
	"""

	def _order_pointing_at_a_cancelled_invoice(self):
		return self._order_with_lines(sales_invoice="ACC-SINV-00001")

	def test_an_unlinked_amendment_stops_the_second_invoice(self):
		def _get_value(doctype, filters=None, fields=None, *a, **kw):
			if isinstance(filters, dict) and "amended_from" in filters:
				return "ACC-SINV-00001-1"
			if isinstance(filters, dict) and "gunbroker_order" in filters:
				return None
			return 2  # the original is cancelled

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch.object(revenue, "_default_company", return_value="TSA"), \
				patch.object(revenue, "_build_invoice") as build, \
				self.assertRaises(Exception) as caught:
			revenue.create_sales_invoice_for_gb_order(
				self._order_pointing_at_a_cancelled_invoice())
		message = str(caught.exception)
		self.assertIn("ACC-SINV-00001-1", message)
		self.assertIn("SECOND invoice", message)
		build.assert_not_called()

	def test_a_cancellation_with_no_amendment_behind_it_is_rebuilt(self):
		# The legitimate way back from a cancellation made in error.
		def _get_value(doctype, filters=None, fields=None, *a, **kw):
			if isinstance(filters, dict) and "amended_from" in filters:
				return None
			if isinstance(filters, dict) and "gunbroker_order" in filters:
				return None
			return 2

		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch.object(revenue, "_default_company", return_value="TSA"), \
				patch.object(revenue, "_build_invoice",
					side_effect=RuntimeError("got past the guard")):
			with self.assertRaises(RuntimeError) as caught:
				revenue.create_sales_invoice_for_gb_order(
					self._order_pointing_at_a_cancelled_invoice())
		self.assertIn("got past the guard", str(caught.exception))


class TestRebookingAnAmendment(_RevenueCase):
	"""Submitting the amendment puts the fee and the receipt back.

	The cancel that preceded it reversed the accrual and unlinked the receipt,
	and nothing restored either — the order stayed pointing at the cancelled
	original and the only way through was for somebody to know to press Create
	Invoice.
	"""

	def _si(self, **kw):
		defaults = {"name": "ACC-SINV-00001-1", "gunbroker_order": "GB-ORD-00001",
			"amended_from": "ACC-SINV-00001"}
		defaults.update(kw)
		return fx.FakeDoc("Sales Invoice", **defaults)

	def test_an_amendment_re_accrues_the_fee_and_replays_the_receipt(self):
		order = self._order()
		with patch("frappe.get_doc", return_value=order), \
				patch.object(revenue, "_accrue_fees") as accrue, \
				patch.object(revenue, "_ensure_paid") as paid:
			revenue.rebook_on_amended_submit(self._si())
		accrue.assert_called_once()
		paid.assert_called_once()
		self.assertEqual(
			self.written[(revenue.ORDER_DOCTYPE, "GB-ORD-00001")]["sales_invoice"],
			"ACC-SINV-00001-1")

	def test_a_first_submission_is_left_to_whoever_raised_it(self):
		with patch.object(revenue, "_accrue_fees") as accrue, \
				patch.object(revenue, "_ensure_paid") as paid:
			revenue.rebook_on_amended_submit(self._si(amended_from=None))
		accrue.assert_not_called()
		paid.assert_not_called()

	def test_an_invoice_that_is_not_a_gunbroker_one_is_left_alone(self):
		with patch.object(revenue, "_accrue_fees") as accrue:
			revenue.rebook_on_amended_submit(
				fx.FakeDoc("Sales Invoice", name="ACC-SINV-9",
					amended_from="ACC-SINV-8"))
		accrue.assert_not_called()

	def test_a_failure_here_never_blocks_the_submit(self):
		with patch("frappe.get_doc", side_effect=RuntimeError("gone")):
			revenue.rebook_on_amended_submit(self._si())  # must not raise
		self.assertTrue(self.logs)

	def test_a_failure_is_announced_and_rolled_back(self):
		# The person who pressed Submit sees nothing — throwing here would fail
		# their submit — so somebody has to be told another way. And the
		# rollback matters because this runs inside their transaction: a
		# journal that inserted and then failed to submit would ride out on the
		# enclosing commit as a Draft nobody asked for.
		with patch("frappe.get_doc", side_effect=RuntimeError("gone")), \
				patch("frappe.db.savepoint") as savepoint, \
				patch("frappe.db.rollback") as rollback:
			revenue.rebook_on_amended_submit(self._si())
		savepoint.assert_called_once_with("gb_rebook_amendment")
		self.assertEqual(rollback.call_args.kwargs["save_point"],
			"gb_rebook_amendment")
		self.assertIn("gb_rebook_failed_GB-ORD-00001", self.alerts)

	def test_a_success_rolls_nothing_back(self):
		order = self._order()
		with patch("frappe.get_doc", return_value=order), \
				patch("frappe.db.savepoint"), \
				patch("frappe.db.rollback") as rollback, \
				patch.object(revenue, "_accrue_fees"), \
				patch.object(revenue, "_ensure_paid"):
			revenue.rebook_on_amended_submit(self._si())
		rollback.assert_not_called()
		self.assertEqual(self.alerts, [])


class TestTheDeskCannotSubmitANegativeLiability(_RevenueCase):
	"""The Draft this case produces is submittable by hand.

	Held for salesTaxTotal below complianceFee, it reaches the operator with
	the marketplace sales-tax row missing and the service-fee row present:
	an ordinary no-tax sale with one odd line, and the obvious thing to do
	with it is press Submit. The booking path's own check sits immediately
	before ITS submit and the Desk never goes through that.
	"""

	def _si(self, rows, **kw):
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", **kw)
		si["taxes"] = rows
		return si

	def test_a_negative_row_is_refused_on_validate(self):
		si = self._si([{"account_head": "GunBroker MPF Sales Tax Payable - TSA",
			"tax_amount": -6.00}], gunbroker_order="GB-ORD-00001")
		with self.assertRaises(Exception) as caught:
			revenue.refuse_a_negative_liability_on_validate(si)
		self.assertIn("negative", str(caught.exception).lower())

	def test_an_ordinary_gunbroker_invoice_passes(self):
		si = self._si([{"account_head": "GunBroker Marketplace Fee Payable - TSA",
			"tax_amount": 11.00}], gunbroker_order="GB-ORD-00001")
		revenue.refuse_a_negative_liability_on_validate(si)  # no raise

	def test_an_invoice_that_is_not_a_gunbroker_one_is_not_this_hooks_business(self):
		# This runs on the validate of every Sales Invoice in the system. A
		# counter sale with a negative adjustment row is somebody else's rule.
		si = self._si([{"account_head": "Sales Tax Payable - TSA",
			"tax_amount": -6.00}])
		revenue.refuse_a_negative_liability_on_validate(si)  # no raise


class TestADirectOrdersInvoiceCarriesTheBuyersAddress(_RevenueCase):
	"""An all-antique order (ships_direct, written by Dispose) has no dealer:
	the invoice must carry the buyer's own Shipping Address — the one thing
	ShipStation's no-FFL fallback reads — and say so in its remarks."""

	def _direct_order(self, **fields):
		detail = fx.order_detail(ffl_number="", ffl_received=False,
			shipToName="Dave Collector", shipToAddress1="12 Elm St",
			shipToCity="Dallas", shipToState="TX", shipToPostalCode="75201")
		return self._order_with_lines(detail, ships_direct=1, **fields)

	def _derive(self, order, si=None):
		si = si or fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			customer="CUST-1")
		with patch.object(revenue, "_invoice_items", return_value=[]), \
				patch.object(revenue, "_tax_rows", return_value=[]), \
				patch.object(revenue, "_line_fees", return_value=(0.0, 0.0)), \
				patch.object(revenue, "_ensure_ship_address",
					return_value="ADDR-1") as ensure:
			revenue._apply_derived(si, order, "TSA")
		return si, ensure

	def test_a_direct_order_gets_a_shipping_address_built_from_its_ship_to(self):
		si, ensure = self._derive(self._direct_order())
		self.assertEqual(si.shipping_address_name, "ADDR-1")
		customer, recipient, ship_to = ensure.call_args.args
		self.assertEqual(customer, "CUST-1")
		self.assertEqual(recipient, "Dave Collector")
		self.assertEqual(ship_to["address_line1"], "12 Elm St")
		self.assertEqual(ship_to["city"], "Dallas")
		self.assertEqual(ship_to["state"], "TX")
		self.assertEqual(ship_to["pincode"], "75201")
		self.assertIsNone(si.ffl_destination_ffl)
		self.assertIn("direct to the buyer", si.remarks)
		self.assertNotIn("Ships to FFL", si.remarks)

	def test_a_dealer_order_gets_no_buyer_address_and_names_the_dealer(self):
		si, ensure = self._derive(self._order_with_lines())
		ensure.assert_not_called()
		self.assertIsNone(si.shipping_address_name)
		self.assertIn("Ships to FFL: 1-99-888-01-2A-34567", si.remarks)

	def test_a_recovered_draft_keeps_the_address_it_already_has(self):
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			customer="CUST-1", shipping_address_name="ADDR-OLD")
		si, ensure = self._derive(self._direct_order(), si=si)
		ensure.assert_not_called()
		self.assertEqual(si.shipping_address_name, "ADDR-OLD")

	def test_a_recovered_draft_that_never_got_an_address_gets_one_now(self):
		# _ensure_ship_address is best-effort and can return None at build
		# time; the Draft recovery must be able to repair that, or the invoice
		# can never be shipped.
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			customer="CUST-1", shipping_address_name=None)
		si, ensure = self._derive(self._direct_order(), si=si)
		ensure.assert_called_once()
		self.assertEqual(si.shipping_address_name, "ADDR-1")


class TestInvoiceItems(_RevenueCase):
	def test_each_gun_is_a_line_at_the_price_gunbroker_charged(self):
		order = self._order_with_lines(fx.order_detail(item_price=1100.00))
		items = revenue._invoice_items(order)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["rate"], 1100.00)
		self.assertEqual(items[0]["qty"], 1)

	def test_the_serial_goes_on_the_line(self):
		# Not decoration: the report prices a firearm's cost from the serial's
		# own purchase rate, and the consignment and pay-on-sale settlements
		# both resolve which gun was sold by reading it off the line. Without it
		# the sale reports inflated profit and a supplier is never paid.
		order = self._order_with_lines()
		self.assertEqual(revenue._invoice_items(order)[0]["serial_no"], "SN-001")

	def test_a_line_with_no_matched_gun_carries_no_serial(self):
		order = self._order_with_lines()
		order.set("serials", [])
		self.assertNotIn("serial_no", revenue._invoice_items(order)[0])


class TestCustomerResolution(_RevenueCase):
	def test_a_repeat_buyer_is_matched_on_their_gunbroker_account(self):
		# The numeric id, because a username can be changed and an email can be
		# shared.
		order = self._order(buyer_user_id="55501")
		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.db.get_value", return_value="CUST-0007"):
			self.assertEqual(revenue.resolve_customer(order), "CUST-0007")

	def test_an_email_match_is_the_second_key(self):
		order = self._order(buyer_user_id="", customer_email="dave@example.com")
		values = [frappe._dict({"name": "CUST-0009", "is_ffl_dealer": 0})]
		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.db.get_value", side_effect=lambda *a, **k: values.pop(0)):
			self.assertEqual(revenue.resolve_customer(order), "CUST-0009")

	def test_a_dealer_record_is_never_reused_for_a_retail_purchase(self):
		# A dealer carries wholesale pricing and tax treatment. A marketplace
		# buyer who happens to share the email must not inherit it.
		order = self._order(buyer_user_id="", customer_email="shop@example.com",
			customer_name="Dave")
		created = fx.FakeDoc("Customer", name="CUST-NEW")
		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"name": "CUST-DEALER", "is_ffl_dealer": 1})), \
				patch("frappe.db.get_single_value", return_value="All Territories"), \
				patch("ffl_core.api.manual_order._individual_group",
					return_value="Individual"), \
				patch("frappe.get_doc", return_value=created):
			self.assertEqual(revenue.resolve_customer(order), "CUST-NEW")

	def test_a_new_buyer_is_created_non_exempt_and_not_a_dealer(self):
		order = self._order(buyer_user_id="55501", customer_email="new@example.com",
			customer_name="New Buyer")
		captured = {}

		def _get_doc(spec):
			captured.update(spec)
			return fx.FakeDoc("Customer", name="CUST-NEW")

		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue, "find_customer_by_gb_id", return_value=None), \
				patch.object(revenue, "find_customer_by_email", return_value=None), \
				patch("frappe.db.get_single_value", return_value="All Territories"), \
				patch("ffl_core.api.manual_order._individual_group",
					return_value="Individual"), \
				patch("frappe.get_doc", side_effect=_get_doc):
			revenue.resolve_customer(order)
		self.assertEqual(captured["is_ffl_dealer"], 0)
		self.assertEqual(captured["is_tax_exempt"], 0)


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

class TestReceiptPostingDate(_RevenueCase):
	def test_the_receipt_takes_the_invoices_date(self):
		# Pinning it to the day the cheque arrived fails on EVERY GunBroker
		# order — they are paid before they are invoiced — and leaves the sale
		# in receivables with the cash already in the bank.
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			posting_date="2026-09-02")
		with patch("frappe.db.get_value", return_value=None):
			self.assertEqual(revenue._receipt_posting_date(si), "2026-09-02")

	def test_a_frozen_month_falls_back_to_today_and_says_so(self):
		# Refusing to post at all would leave the cash off the ledger entirely.
		# A frozen date far in the past: the fallback is "today", and a literal
		# recent date turned this into a time bomb on 2026-09-30 itself.
		si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			posting_date="2000-01-31")
		with patch("frappe.db.get_value", return_value="2000-01-31"), \
				patch("frappe.msgprint") as msgprint:
			out = revenue._receipt_posting_date(si)
		self.assertNotEqual(out, "2000-01-31")
		msgprint.assert_called_once()


class TestEnsurePaid(_RevenueCase):
	def _invoice(self, outstanding=1245.99):
		return fx.FakeDoc("Sales Invoice", name="ACC-SINV-1", company="TSA",
			customer="CUST-1", posting_date="2026-09-02",
			outstanding_amount=outstanding)

	def test_nothing_recorded_means_no_payment_entry_is_invented(self):
		# Not from the order total, and not from GunBroker's own flag. A
		# receipt nobody took is a lie that balances; the invoice stays
		# outstanding and the daily audit keeps saying so.
		order = self._order(payment_mode_used="", payment_received=1,
			payment_amount=0, order_total=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry") as book:
			revenue._ensure_paid(self._invoice(), order)
		book.assert_not_called()

	def test_the_receipt_carries_the_cheque_number(self):
		# It is the only thing tying this deposit to a line on the bank
		# statement, and the entry's posting date is the invoice's by design —
		# so the reference is what a reconciliation matches on.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1245.99, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
					return_value="ACC-PAY-1") as book, \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue, "_ensure_modes"):
			revenue._ensure_paid(self._invoice(), order)
		self.assertEqual(book.call_args.kwargs["transaction_number"], "9001")

	def test_a_recorded_cheque_books_a_receipt_on_the_invoices_date(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1245.99, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
				return_value="ACC-PAY-1") as book, \
				patch("frappe.db.get_value", return_value=None):
			revenue._ensure_paid(self._invoice(), order)
		_args, kwargs = book.call_args
		self.assertEqual(kwargs["posting_date"], "2026-09-02")
		self.assertEqual(book.call_args[0][2], 1245.99)
		self.assertEqual(order.payment_entry, "ACC-PAY-1")

	def test_the_deposit_date_survives_in_the_entrys_remarks(self):
		# It is the only place it can live: the posting date is the invoice's by
		# design, so without this a bank reconciliation has nothing to match on.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1245.99, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
				return_value="ACC-PAY-1") as book, \
				patch("frappe.db.get_value", return_value=None):
			revenue._ensure_paid(self._invoice(), order)
		remarks = book.call_args[1]["remarks"]
		self.assertIn("2026-08-28", remarks)
		self.assertIn("9001", remarks)
		self.assertIn("998877", remarks)

	def test_a_short_payment_books_only_what_arrived(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1000.00, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
				return_value="ACC-PAY-1") as book, \
				patch("frappe.db.get_value", return_value=None):
			revenue._ensure_paid(self._invoice(), order)
		self.assertEqual(book.call_args[0][2], 1000.00)

	def test_an_overpayment_is_held_as_customer_credit_on_the_same_date(self):
		# The bank took the whole cheque. Recording only the part that fitted
		# the invoice leaves the reconciliation permanently out by the rest, and
		# letting the remainder default to today splits one deposit across two
		# months.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1295.99, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		captured = {}

		def _get_doc(spec):
			captured.update(spec)
			return fx.FakeDoc("Payment Entry", name="ACC-PAY-2")

		with patch("ffl_core.api.manual_order._book_payment_entry",
				return_value="ACC-PAY-1"), \
				patch("ffl_core.api.manual_order._mode_of_payment_account",
					return_value="Operating Bank - TSA"), \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue, "_ensure_modes"), \
				patch("erpnext.accounts.party.get_party_account",
					return_value="Debtors - TSA"), \
				patch("frappe.get_doc", side_effect=_get_doc):
			revenue._ensure_paid(self._invoice(), order)
		# Two dependencies patched like the sibling tests patch them, because
		# both go to the database behind this test's get_value=None: _ensure_modes
		# would try to CREATE the Modes of Payment through the patched get_doc,
		# and get_party_account loads the Account meta — which, on a cold meta
		# cache (a module run on its own), reads DocType through db.get_value
		# and dies with "DocType Account not found", swallowed by
		# _book_overpayment's log-and-continue. The full suite had warmed it.
		self.assertEqual(captured["payment_type"], "Receive")
		self.assertEqual(captured["paid_from"], "Debtors - TSA",
			"the credit sits on the customer's receivable account")
		self.assertEqual(captured["paid_amount"], 50.00)
		self.assertEqual(captured["posting_date"], "2026-09-02")
		self.assertEqual(captured["reference_no"], "9001")
		self.assertEqual(captured["paid_to"], "Operating Bank - TSA")
		self.assertIn("gb_overpaid_998877", self.alerts)

	def test_a_second_run_does_not_book_a_second_receipt(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_entry="ACC-PAY-1", payment_amount=1245.99,
			gb_overpayment_pe="ACC-PAY-2")
		with patch("ffl_core.api.manual_order._book_payment_entry") as book, \
				patch("frappe.db.get_value", return_value=1):
			revenue._ensure_paid(self._invoice(), order)
		book.assert_not_called()

	def test_a_cancelled_receipt_does_not_count_as_one(self):
		# The cheque bounced, the entry was
		# cancelled, the money was chased and arrived. The booking has to run
		# again — and a link still pointing at the cancelled document made
		# create_gb_invoice_now return having done nothing at all.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_entry="ACC-PAY-1", payment_amount=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
					return_value="ACC-PAY-9") as book, \
				patch("frappe.db.get_value",
					side_effect=lambda dt, *a, **kw: 2 if dt == "Payment Entry"
						else None), \
				patch.object(revenue, "_ensure_modes"):
			revenue._ensure_paid(self._invoice(), order)
		book.assert_called_once()

	def test_the_modes_of_payment_are_re_pointed_before_the_receipt(self):
		# The seed that creates GunBroker Check runs on install and on
		# migrate and nowhere else, so on the ordinary go-live order — migrate
		# an empty site, fill the deposit account in afterwards — the mode has
		# no account and _book_payment_entry throws with the gun already gone.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry",
					return_value="ACC-PAY-9"), \
				patch("frappe.db.get_value", return_value=None), \
				patch.object(revenue.gb_setup, "ensure_modes_of_payment") as ensure:
			revenue._ensure_paid(self._invoice(), order)
		ensure.assert_called_once_with("TSA")

	def test_an_overpayment_that_never_posted_is_picked_up_next_time(self):
		# The second entry is best-effort and logs rather than raises; once
		# payment_entry was set nothing ever came back for it, and the bank
		# reconciliation stayed out by the difference for good.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_entry="ACC-PAY-1", payment_amount=1300.00)

		def _get_value(doctype, name, field=None, *a, **kw):
			if field == "docstatus":
				return 1 if name == "ACC-PAY-1" else 0
			if field == "paid_amount":
				return 1245.99
			return None

		invoice = self._invoice()
		invoice["outstanding_amount"] = 0
		with patch("frappe.db.get_value", side_effect=_get_value), \
				patch.object(revenue, "_book_overpayment") as book:
			revenue._ensure_paid(invoice, order)
		book.assert_called_once()
		self.assertEqual(book.call_args[0][2], 54.01)

	def test_an_invoice_still_owed_money_is_not_an_overpayment(self):
		# A short payment somebody "corrected" by editing payment_amount
		# would otherwise book the difference as unallocated customer credit
		# while the invoice went on showing it outstanding: the bank
		# reconciles and the receivable is nonsense. That one is si_outstanding's.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_entry="ACC-PAY-1", payment_amount=1245.99)
		invoice = self._invoice()
		invoice["outstanding_amount"] = 245.99
		with patch("frappe.db.get_value", return_value=1), \
				patch.object(revenue, "_book_overpayment") as book:
			revenue._ensure_paid(invoice, order)
		book.assert_not_called()

	def test_booking_an_overpayment_never_commits(self):
		# This became reachable from a Sales Invoice on_submit handler.
		# Committing part way through somebody else's submit breaks its
		# atomicity: a later handler on the same document that throws would
		# leave the invoice, the journal and both receipts persisted while the
		# operator is shown an error. The request boundary commits anyway.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1295.99, payment_reference_no="9001",
			payment_received_on="2026-08-28", order_total=1245.99)
		with patch("frappe.db.get_value", return_value=None), \
				patch("ffl_core.api.manual_order._mode_of_payment_account",
					return_value="Operating Bank - TSA"), \
				patch("erpnext.accounts.party.get_party_account",
					return_value="Debtors - TSA"), \
				patch("frappe.get_doc",
					return_value=fx.FakeDoc("Payment Entry", name="ACC-PAY-2")), \
				patch("ffl_core.utils.commit") as commit, \
				patch("frappe.db.commit") as db_commit:
			revenue._book_overpayment(self._invoice(), order, 50.00, "2026-09-02")
		commit.assert_not_called()
		db_commit.assert_not_called()

	def test_a_live_overpayment_entry_stops_a_second_one(self):
		# The re-booking path (the cheque bounced,
		# the allocated entry was cancelled, the money arrived) reaches
		# _book_overpayment with the OVERPAYMENT entry possibly still live. An
		# operator who cancelled one of the two would get a second unallocated
		# receipt for the same fifty dollars.
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1295.99, payment_reference_no="9001",
			gb_overpayment_pe="ACC-PAY-2", order_total=1245.99)
		# Everything the builder needs is in place, so the ONLY thing that can
		# stop it is the guard.
		with patch("frappe.db.get_value", return_value=1), \
				patch("ffl_core.api.manual_order._mode_of_payment_account",
					return_value="Operating Bank - TSA"), \
				patch("erpnext.accounts.party.get_party_account",
					return_value="Debtors - TSA"), \
				patch("frappe.get_doc") as get_doc:
			revenue._book_overpayment(self._invoice(), order, 50.00, "2026-09-02")
		get_doc.assert_not_called()
		self.assertEqual(self.alerts, [])

	def test_a_fully_settled_invoice_is_left_alone(self):
		order = self._order(payment_mode_used="GunBroker Check",
			payment_amount=1245.99)
		with patch("ffl_core.api.manual_order._book_payment_entry") as book:
			revenue._ensure_paid(self._invoice(outstanding=0), order)
		book.assert_not_called()


class TestTheClearingAccount(FrappeTestCase):
	"""Build one only where Settings does not already name one.

	A store that has pointed GunBroker card money at its existing Payroc
	clearing account has said where it wants that money to land. Creating a
	second, empty, near-identically named Bank account under the same parent
	gives the month-end reconciliation two places to look, one of them always
	zero.
	"""

	def test_a_configured_account_is_used_rather_than_a_new_one(self):
		from ffl_integrations.gunbroker import setup as gb_setup

		with patch("frappe.db.get_single_value",
					return_value="Payroc Card Clearing - TSA"), \
				patch("frappe.get_doc") as get_doc:
			out = gb_setup.ensure_clearing_account("TSA")
		self.assertEqual(out, "Payroc Card Clearing - TSA")
		get_doc.assert_not_called()

	def test_with_nothing_configured_the_account_is_built(self):
		from ffl_integrations.gunbroker import setup as gb_setup

		with patch("frappe.db.get_single_value", return_value=""), \
				patch("frappe.get_cached_value", return_value="TSA"), \
				patch("frappe.db.exists", side_effect=lambda dt, name=None, **k:
					name == "Bank Accounts - TSA"), \
				patch("frappe.db.get_value", return_value=None), \
				patch("frappe.get_doc") as get_doc:
			gb_setup.ensure_clearing_account("TSA")
		get_doc.assert_called_once()


class TestBookingGate(_RevenueCase):
	def test_an_undisposed_order_cannot_be_booked(self):
		order = self._order(order_status="Pending Fulfillment")
		with self.assertRaises(Exception):
			revenue.create_sales_invoice_for_gb_order(order)


# ---------------------------------------------------------------------------
# Coming back to a Draft
# ---------------------------------------------------------------------------

class TestDraftRecovery(_RevenueCase):
	"""What happens on the retry, which is where a Draft is always submitted.

	A Draft only exists because something about the order could not be posted,
	and the commonest of those reasons fixes itself: GunBroker had not sent
	complianceFee yet. Submitting the Draft as it was built keeps the split it
	was built with — the whole of salesTaxTotal on the MPF sales-tax row — and
	the grand total is identical either way, so the drift guard cannot see it.
	The buyer's service fee would then be filed with the state.
	"""

	def setUp(self):
		super().setUp()
		self.si = fx.FakeDoc("Sales Invoice", name="ACC-SINV-00001")
		self.si["docstatus"] = 0
		self.si["grand_total"] = 1245.99
		self._patch("frappe.new_doc", return_value=self.si)
		self._patch("frappe.get_doc", return_value=self.si)
		self._patch("frappe.log_error")
		self._patch_object(revenue, "resolve_customer", return_value="CUST-GB-01")
		self._patch_object(revenue, "_cost_center", return_value="Main - TSA")
		self._patch_object(revenue, "_accrue_fees")
		self._patch_object(revenue, "_ensure_paid")

	def _heads(self):
		return {row["account_head"]: row["tax_amount"]
			for row in (self.si.get("taxes") or [])}

	def test_a_fee_that_arrives_late_moves_off_the_states_liability(self):
		# Round one: no complianceFee, so the split cannot be made and the whole
		# tax figure lands on the MPF row of a Draft nobody submits.
		blocked = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.99, compliance_fee=0))
		with patch("frappe.db.get_value", return_value=None):
			first = revenue.create_sales_invoice_for_gb_order(blocked)
		self.assertEqual(first["status"], "draft_unhandled_amounts")
		self.assertEqual(self.si["docstatus"], 0)
		self.assertEqual(self._heads(),
			{"GunBroker MPF Sales Tax Payable - TSA": 100.99})

		# Round two: GunBroker sent the fee, so the SAME Draft is picked up and
		# submitted. It must be re-derived first.
		fixed = self._order_with_lines(
			fx.order_detail(sales_tax_total=100.99, compliance_fee=11.00),
			sales_invoice=self.si.name)
		existing = frappe._dict({"name": self.si.name, "docstatus": 0})
		with patch("frappe.db.get_value", return_value=existing):
			second = revenue.create_sales_invoice_for_gb_order(fixed)
		self.assertEqual(second["status"], "created")
		self.assertEqual(self.si["docstatus"], 1)
		self.assertEqual(self._heads(), {
			"GunBroker MPF Sales Tax Payable - TSA": 89.99,
			"GunBroker Marketplace Fee Payable - TSA": 11.00})

	def test_the_lines_are_replaced_rather_than_appended_to(self):
		# Re-deriving twice must not leave the invoice billing the gun twice.
		order = self._order_with_lines()
		existing = frappe._dict({"name": self.si.name, "docstatus": 0})
		revenue._refresh_draft(order, existing, "TSA")
		revenue._refresh_draft(order, existing, "TSA")
		self.assertEqual(len(self.si.get("items")), 1)
		self.assertEqual(len(self.si.get("taxes")), 2)

	def test_a_rebuilt_draft_never_carries_a_tax_template(self):
		# ERPNext fills taxes_and_charges in from the customer or the company
		# when it is blank, and the no-tax variant leaves the table empty for it
		# to fill — with the store's own sales-tax row, the one account a
		# marketplace sale must never touch.
		order = self._order_with_lines(
			fx.order_detail(sales_tax_total=0, compliance_fee=0))
		existing = frappe._dict({"name": self.si.name, "docstatus": 0})
		revenue._refresh_draft(order, existing, "TSA")
		self.assertEqual(self.si["taxes"], [])
		self.assertEqual(self.si["taxes_and_charges"], "")

	def test_the_drafts_posting_date_is_not_moved_by_the_retry(self):
		# A Draft raised last month stays in last month: re-dating it would
		# move the revenue into a period nobody asked for.
		self.si["posting_date"] = "2026-07-31"
		order = self._order_with_lines()
		existing = frappe._dict({"name": self.si.name, "docstatus": 0})
		revenue._refresh_draft(order, existing, "TSA")
		self.assertEqual(self.si["posting_date"], "2026-07-31")
