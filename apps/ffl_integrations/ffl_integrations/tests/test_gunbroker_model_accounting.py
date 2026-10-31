"""What a model-level GunBroker sale posts: the CPA's acceptance checks A1-A7.

The ruling (specific identification, seller fees per unit, price as sold, nothing
booked for a unit that was never delivered) is only true if the posted documents
say so, so these run the real Dispose: real dispositions out of a real stock
ledger, a real Sales Invoice and the real fee journal. Only GunBroker's network
is faked.

The tolerance and the expected numbers are the ruling's own:

* seed fee tiers 6% to $400 / 4% to $15,000 and an FFL document fee of $5.00;
* $499 x 2: $27.96 per unit, so $55.92 (billing the whole line at once would
  give $47.92 and under-accrue); $10.00 of document fees; $65.92 in one journal;
* the invoice rate is GunBroker's ``itemPrice`` (a promotion that has not reached
  GunBroker yet is billed at the price GunBroker charged), with the serials as
  finally assigned.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt, nowdate

from ffl_core import cash_drawer
from ffl_integrations.gunbroker import fulfillment, revenue
from ffl_integrations.gunbroker import setup as gb_setup
from ffl_integrations.tests.gb_model_fixtures import MODEL, TAX, ModelUnitCase

KEY0, KEY1 = "16087299:0", "16087299:1"
FFL = "1-99-888-01-2A-34567"


class _BookedCase(ModelUnitCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The cash drawer's bank account, made if the company has none: a fresh site has no
		# leaf bank account, and finding one made by an earlier test made this order-dependent.
		bank = cash_drawer.bank_account(cls.company)
		frappe.db.set_single_value("GunBroker Settings", "check_deposit_account", bank)
		gb_setup.ensure(cls.company)
		# The ruling's numbers: the shipped tiers and the default document fee.
		frappe.db.set_single_value("GunBroker Settings", "ffl_doc_fee", 5.0)
		if not frappe.db.exists("ATF FFL Record", FFL):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": FFL,
				"licensee_name": "Far End Guns", "street": "900 Dealer Row",
				"city": "Austin", "state": "TX", "zip": "78701",
				"expiry_date": frappe.utils.add_days(nowdate(), 365),
			}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		self._start(patch.object(fulfillment, "notify_admins"))
		self._start(patch.object(revenue, "notify_admins"))
		self._start(patch.object(fulfillment, "notify_channel_commitments_changed"))
		self._start(patch("ffl_core.compliance.cr_transfer.commit"))

	# -------------------------------------------------------------- helpers

	def paid_order(self, *, qty=2, price=499.0, **kw):
		"""An ingested model-level order whose payment is recorded, ready to assign."""
		total = price * qty + 40.0 + TAX
		order = self.ingest(self.detail(qty=qty, price=price, **kw))
		frappe.db.set_value("GunBroker Order", order.name, {
			"order_total": total, "payment_mode_used": "GunBroker Check",
			"payment_received_on": nowdate(), "payment_amount": total,
			"payment_reference_no": "9001",
		})
		order.reload()
		return order

	def assign(self, order, key, serial):
		return fulfillment.assign_gb_unit_serial(order.name, key, serial)

	def dispose(self, order):
		return fulfillment.dispose_gb_order(order.name)

	def invoice(self, order):
		name = frappe.db.get_value("Sales Invoice",
			{"gunbroker_order": order.name, "docstatus": ("<", 2)}, "name")
		return frappe.get_doc("Sales Invoice", name) if name else None

	def dispositions(self, order):
		return frappe.get_all("FFL Disposition",
			filters={"gunbroker_order": order.name, "docstatus": 1}, pluck="serial")


class TwoUnitsAtFourNinetyNine(_BookedCase):
	"""A1, A2, and the dispositions behind R15."""

	def setUp(self):
		super().setUp()
		self.a = self.stock("GBA-A", rate=210)
		self.b = self.stock("GBA-B", rate=260)
		self.c = self.stock("GBA-C", rate=230)
		self.order = self.paid_order()

	def test_a1_fees_are_per_unit_and_the_journal_is_the_sum(self):
		self.assign(self.order, KEY0, self.a)
		self.assign(self.order, KEY1, self.b)
		self.dispose(self.order)
		si = self.invoice(self.order)
		self.assertEqual(flt(si.gb_final_value_fee), 55.92)   # 2 x (24.00 + 3.96)
		self.assertEqual(flt(si.gb_ffl_doc_fee), 10.00)       # 2 x 5.00
		order = frappe.get_doc("GunBroker Order", self.order.name)
		self.assertEqual([(flt(l.fvf_amount), flt(l.ffl_doc_fee_amount)) for l in order.lines],
			[(55.92, 10.00)])
		je = frappe.get_doc("Journal Entry", order.fee_accrual_je)
		sides = {a.account: (flt(a.debit), flt(a.credit)) for a in je.accounts}
		self.assertEqual(sides, {
			gb_setup.account_name(gb_setup.FEES_EXPENSE_ACCOUNT, self.company): (65.92, 0.0),
			gb_setup.account_name(gb_setup.ACCRUED_FEES_ACCOUNT, self.company): (0.0, 65.92)})

	def test_a1_assigning_and_re_polling_never_recomputes_or_zeroes_the_fees(self):
		self.assign(self.order, KEY0, self.a)
		self.assign(self.order, KEY1, self.b)
		self.dispose(self.order)
		before = [(flt(l.fvf_amount), flt(l.ffl_doc_fee_amount))
			for l in frappe.get_doc("GunBroker Order", self.order.name).lines]
		# GunBroker touches the order again (it is now shipped, still re-polled).
		detail = self.detail(qty=2, order_id=self.order.gb_order_id)
		self.ingest(detail)
		after = [(flt(l.fvf_amount), flt(l.ffl_doc_fee_amount))
			for l in frappe.get_doc("GunBroker Order", self.order.name).lines]
		self.assertEqual(after, before)
		self.assertEqual(before, [(55.92, 10.00)])

	def test_assignment_alone_writes_no_fee(self):
		self.assign(self.order, KEY0, self.a)
		line = frappe.get_doc("GunBroker Order", self.order.name).lines[0]
		self.assertEqual((flt(line.fvf_amount), flt(line.ffl_doc_fee_amount)), (0.0, 0.0))

	def test_a2_the_invoice_is_one_row_of_two_at_gunbrokers_price_with_the_final_serials(self):
		self.assign(self.order, KEY0, self.a)
		self.assign(self.order, KEY1, self.b)
		self.assign(self.order, KEY0, self.c)   # a change of mind: A -> C
		result = self.dispose(self.order)
		si = self.invoice(self.order)
		self.assertEqual(len(si.items), 1)
		row = si.items[0]
		self.assertEqual((row.item_code, flt(row.qty), flt(row.rate)), (MODEL, 2.0, 499.0))
		self.assertEqual(sorted(row.serial_no.split("\n")), sorted([self.c, self.b]))
		self.assertNotIn(self.a, row.serial_no)
		self.assertEqual(flt(si.discount_amount), 0.0)
		self.assertLessEqual(abs(flt(si.grand_total) - 1128.0), 0.02)
		self.assertEqual(result["invoice_status"], "created")
		self.assertEqual(si.docstatus, 1)

	def test_a2_a_price_that_is_really_the_line_total_stops_in_draft(self):
		"""If `itemPrice` ever turns out to be the whole line's price, the invoice
		cannot agree with what the buyer was charged and must not post."""
		order = self.paid_order(price=499.0)
		frappe.db.set_value("GunBroker Order Line", order.lines[0].name, "unit_price", 998.0)
		self.assign(order, KEY0, self.a)
		self.assign(order, KEY1, self.b)
		result = self.dispose(order)
		self.assertEqual(result["invoice_status"], "draft_total_mismatch")
		self.assertEqual(self.invoice(order).docstatus, 0)

	def test_r15_two_units_book_two_dispositions_one_per_gun(self):
		self.assign(self.order, KEY0, self.a)
		self.assign(self.order, KEY1, self.b)
		result = self.dispose(self.order)
		self.assertEqual(result["disposed_count"], 2)
		self.assertEqual(sorted(self.dispositions(self.order)), sorted([self.a, self.b]))
		self.assertNotEqual(frappe.db.get_value("Serial No", self.a, "status"), "Active")
		self.assertEqual(frappe.db.get_value("GunBroker Order", self.order.name,
			"order_status"), "Transferred")
		self.assertEqual(self.held_by(self.a), (False, ""))

	def test_a3_cost_follows_the_gun_that_left_not_the_first_suggestion(self):
		"""A (210) was assigned and swapped for C (230): A never moves, the two
		dispositions issue C and B, and the Sales Report prices the line at the
		serials' purchase rates, 230 + 260."""
		from ffl_core.ffl_core.report.sales_report.sales_report import (
			_fetch_serial_facts,
			_line_cost,
		)

		self.assign(self.order, KEY0, self.a)
		self.assign(self.order, KEY1, self.b)
		self.assign(self.order, KEY0, self.c)
		self.dispose(self.order)
		moved = {r.serial_no for r in frappe.get_all("Stock Entry Detail",
			filters={"serial_no": ("like", "GBA-%"), "s_warehouse": ("is", "set")},
			fields=["serial_no"])}
		self.assertTrue(any(self.b in (s or "") for s in moved))
		self.assertFalse(any(self.a in (s or "") for s in moved))
		self.assertEqual(frappe.db.get_value("Serial No", self.a, "status"), "Active")
		issued = frappe.db.sql(
			"""SELECT COALESCE(SUM(ABS(stock_value_difference)), 0) FROM `tabStock Ledger Entry`
			WHERE voucher_type = 'Stock Entry' AND is_cancelled = 0 AND actual_qty < 0
			  AND item_code = %s AND voucher_no IN (
			    SELECT stock_entry FROM `tabFFL Disposition` WHERE gunbroker_order = %s)""",
			(MODEL, self.order.name))[0][0]
		self.assertEqual(flt(issued), 490.0)
		si = self.invoice(self.order)
		line = {"item_code": MODEL, "qty": 2, "serial_no": si.items[0].serial_no}
		cost, _missing = _line_cost(line, {MODEL: {"is_firearm": 1}},
			_fetch_serial_facts({si.name: [line]})[0])
		self.assertEqual(flt(cost), 490.0)

	def test_a6_a_promotion_price_is_the_rate_and_the_fee_follows_it(self):
		order = self.paid_order(price=449.0)
		self.assign(order, KEY0, self.a)
		self.assign(order, KEY1, self.b)
		self.dispose(order)
		si = self.invoice(order)
		self.assertEqual((flt(si.items[0].qty), flt(si.items[0].rate), flt(si.items[0].amount)),
			(2.0, 449.0, 898.0))
		self.assertEqual(flt(si.gb_final_value_fee), 51.92)   # 2 x (24.00 + 1.96)
		self.assertEqual(flt(si.gb_ffl_doc_fee), 10.00)
		self.assertEqual(flt(si.discount_amount), 0.0)  # no order-level discount line


class AUnitThatWasNeverDelivered(_BookedCase):
	"""A7 and the oversold case (ruling item 7): two bought, one gun exists."""

	def setUp(self):
		super().setUp()
		self.a = self.stock("GBA-X", rate=210)
		self.order = self.paid_order()

	def test_a7_a_unit_with_no_gun_and_no_refund_mark_books_nothing(self):
		self.assign(self.order, KEY0, self.a)
		before = (frappe.db.count("Sales Invoice"), frappe.db.count("Journal Entry"),
			frappe.db.count("FFL Disposition"))
		with self.assertRaises(frappe.ValidationError):
			self.dispose(self.order)
		self.assertEqual((frappe.db.count("Sales Invoice"), frappe.db.count("Journal Entry"),
			frappe.db.count("FFL Disposition")), before)
		order = frappe.get_doc("GunBroker Order", self.order.name)
		self.assertEqual((order.order_status, order.fee_accrual_je or ""),
			("Pending Fulfillment", ""))
		# The gun is still held, nothing has left.
		self.assertEqual(self.held_by(self.a), (True, order.name))

	def test_the_refunded_unit_is_left_off_the_invoice_but_the_fees_stay_whole(self):
		self.assign(self.order, KEY0, self.a)
		fulfillment.mark_gb_unit_refunded(self.order.name, KEY1)
		result = self.dispose(self.order)
		# One disposition, for the one gun that exists.
		self.assertEqual(self.dispositions(self.order), [self.a])
		si = self.invoice(self.order)
		self.assertEqual((len(si.items), flt(si.items[0].qty), flt(si.items[0].rate),
			si.items[0].serial_no), (1, 1.0, 499.0, self.a))
		# GunBroker does not lower its fee for a refund: the whole line is accrued.
		self.assertEqual((flt(si.gb_final_value_fee), flt(si.gb_ffl_doc_fee)), (55.92, 10.00))
		# The invoice no longer agrees with what the buyer was charged, so a person
		# checks the refund and the tax rows before it posts.
		self.assertEqual(result["invoice_status"], "draft_total_mismatch")
		self.assertEqual(si.docstatus, 0)

	def test_when_gunbrokers_total_already_reflects_the_refund_the_invoice_posts(self):
		self.assign(self.order, KEY0, self.a)
		fulfillment.mark_gb_unit_refunded(self.order.name, KEY1)
		# What the buyer ends up charged once GunBroker has refunded one unit.
		frappe.db.set_value("GunBroker Order", self.order.name, "order_total",
			499.0 + 40.0 + 90.0)
		result = self.dispose(self.order)
		self.assertEqual(result["invoice_status"], "created")
		self.assertEqual(self.invoice(self.order).docstatus, 1)


class AssignmentDoesNotMoveTheCashBasisDates(_BookedCase):
	"""A5. A paid order whose guns are not yet named is cash in the bank with
	nothing on the ledger, and stays so until Dispose: assigning on another day
	moves neither the invoice nor the receipt."""

	def test_a5_the_order_is_listed_until_the_period_its_receipt_posts_in(self):
		from ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments import (
			gunbroker_cash_basis_adjustments as cb,
		)

		today = nowdate()
		paid_on = frappe.utils.add_days(today, -5)
		end_before = frappe.utils.add_days(today, -3)   # paid, not yet on the ledger
		end_after = frappe.utils.add_days(today, 1)     # after the dispose
		a, b = self.stock("GBA-Y1"), self.stock("GBA-Y2")
		order = self.paid_order()
		frappe.db.set_value("GunBroker Order", order.name, "payment_received_on", paid_on)

		def row(period_end):
			return next(r for r in cb._fetch_orders(period_end) if r["name"] == order.name)

		self.assertTrue(cb._still_in_hand(row(end_before), [], end_before))
		# Unassigned: Dispose is refused, so no invoice or receipt can exist.
		with self.assertRaises(frappe.ValidationError):
			self.dispose(order)
		self.assertTrue(cb._still_in_hand(row(end_after), [], end_after))
		# Naming the guns is not a ledger event.
		self.assign(order, KEY0, a)
		self.assign(order, KEY1, b)
		fresh = frappe.get_doc("GunBroker Order", order.name)
		self.assertEqual((fresh.sales_invoice or "", fresh.payment_entry or ""), ("", ""))
		self.assertTrue(cb._still_in_hand(row(end_after), [], end_after))
		# Dispose books the invoice and the receipt on the day it happens.
		self.dispose(order)
		fresh.reload()
		si_date = str(frappe.db.get_value("Sales Invoice", fresh.sales_invoice, "posting_date"))
		pe_date = str(frappe.db.get_value("Payment Entry", fresh.payment_entry, "posting_date"))
		self.assertEqual((si_date, pe_date), (today, today))
		# As-of, not as-now: still item 3 on the earlier period end, gone from the later.
		self.assertTrue(cb._still_in_hand(row(end_before), [], end_before))
		self.assertFalse(cb._still_in_hand(row(end_after), [], end_after))
