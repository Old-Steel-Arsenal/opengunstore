"""Counter pre-orders, arrival to hand-over: what comes in is put by for the oldest order waiting for
it, the pickup ticket bills the goods on the day they go out and takes off what was paid, and nothing
before that books to sales, tax, cost or stock.

Site tests on the bootstrap company ("FFL Test"), with the fixtures of ``test_preorder``: an 8.25%
store, and an 8.75% + 11% excise one. Goods arrive by Material Receipt; the arrival job
(``preorder.allocate_arrival``, queued after the receipt commits) is run directly. Register tickets
are checked out as the register would and merged into their Sales Invoice, whose GL Entries are read.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from ffl_core import deposits, preorder
from ffl_core.setup import preorder as setup
from ffl_core.setup.preorder import AVAILABLE_FIELD, PREORDER_ITEM, RESERVED_FIELD, SERIAL_FIELD
from ffl_core.tests.test_preorder import ABBR, CARD, COMPANY, _Base, _checkout, _customer, _gl, _merge

SHELF = f"Stores - {ABBR}"


def _receipt(rows: list[dict]) -> str:
	se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": COMPANY,
		"items": rows})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _gun_in(item: str, serial: str, cost: float = 700.0) -> str:
	from ffl_core.firearm import serialized_receipt_row

	return _receipt([serialized_receipt_row(item_code=item, qty=1, warehouse=SHELF, basic_rate=cost, serial=serial)])


def _units_in(item: str, qty: float, cost: float = 10.0) -> str:
	return _receipt([{"item_code": item, "qty": qty, "t_warehouse": SHELF, "basic_rate": cost}])


def _serial() -> str:
	return f"TPOH-{frappe.generate_hash(length=8).upper()}"


def _cover(ticket: str, serial: str) -> None:
	"""What FastBound's 4473 webhook leaves on a finished sale: a Retail Sale disposition on the
	ticket and the ticket's 4473 / NICS fields (as test_counter_to_store._cover)."""
	from frappe.utils import now_datetime

	pos = frappe.db.get_value("POS Invoice", ticket, ["company", "customer"], as_dict=True)
	disp = frappe.get_doc({"doctype": "FFL Disposition", "disposition_date": now_datetime(),
		"disposition_type": "Retail Sale", "company": pos.company, "customer": pos.customer,
		"item": frappe.db.get_value("Serial No", serial, "item_code"),
		"serial": serial, "linked_pos_invoice": ticket, "form_4473_status": "Completed", "nics_status": "Proceed",
		"fastbound_id": f"FB-TEST-PO-{serial}"})
	disp.flags.ignore_permissions = True
	disp.insert()
	disp.submit()
	frappe.db.set_value("POS Invoice", ticket, {"form_4473_status": "Completed", "nics_status": "Proceed"},
		update_modified=False)


def _line(so: str) -> frappe._dict:
	return frappe.db.get_value("Sales Order Item", {"parent": so}, ["name", SERIAL_FIELD, RESERVED_FIELD], as_dict=True)


def _accounts_by_type(gl: dict) -> dict[str, float]:
	"""A voucher's GL folded by what the account is: income, tax, stock, expense, else the account."""
	meta = {r.name: r for r in frappe.get_all("Account", filters={"name": ["in", list(gl)]},
		fields=["name", "root_type", "account_type"])}
	out: dict[str, float] = {}
	for account, v in gl.items():
		m = meta[account]
		key = ("stock" if m.account_type == "Stock" else "tax" if m.account_type == "Tax"
			else "income" if m.root_type == "Income" else "expense" if m.root_type == "Expense" else account)
		out[key] = flt(out.get(key, 0) + v, 2)
	return out


class _Handover(_Base):
	"""Each test gets a gun and a counted item of its own: what one test leaves on the shelf or waiting
	is never the next one's."""

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		fb = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in pre-order tests"))
		fb.start()
		self.addCleanup(fb.stop)
		token = frappe.generate_hash(length=6).upper()
		self.gun, self.acc = f"_T-POH-self.gun-{token}", f"_T-POH-self.acc-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": self.gun, "item_name": f"Test Hand-over Pistol {token}",
			"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1}
			).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Item", "item_code": self.acc, "item_name": f"Test Hand-over Optic {token}",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)

	def _preorder(self, item=None, **kwargs) -> str:
		return super()._preorder(item=item or self.gun, **kwargs)

	def _arrive_gun(self, serial: str | None = None) -> str:
		serial = serial or _serial()
		preorder.allocate_arrival("Stock Entry", _gun_in(self.gun, serial))
		return serial

	def _hand_over(self, so: str, *, mode: str = CARD, serial: str | None = None):
		"""Open pickup, the 4473 done, checked out for what is left: the submitted ticket."""
		ticket = preorder.open_pickup(so)["pos_invoice"]
		if serial:
			_cover(ticket, serial)
		return _checkout(ticket, mode=mode)


class Arrival(_Handover):

	def test_ac4_the_oldest_order_gets_the_first_gun_and_the_next_one_the_second(self):
		first = self._preorder(available=5)
		second = self._preorder(customer=_customer(), available=4)
		one = self._arrive_gun()
		self.assertEqual(_line(first)[SERIAL_FIELD], one)
		self.assertFalse(_line(second)[SERIAL_FIELD])
		rows = {r["sales_order"]: r for r in preorder.list_preorders() if r["sales_order"] in (first, second)}
		self.assertEqual((rows[first]["stage"], rows[second]["stage"]), ("ready_pickup", "waiting"))
		two = self._arrive_gun()
		self.assertEqual((_line(first)[SERIAL_FIELD], _line(second)[SERIAL_FIELD]), (one, two))
		# A third gun is ordinary stock: nobody is waiting.
		three = self._arrive_gun()
		self.assertEqual(preorder.local_stock(self.gun, COMPANY), 1)
		self.assertNotIn(three, (one, two))

	def test_a_second_run_of_the_same_arrival_changes_nothing(self):
		so = self._preorder()
		se = _gun_in(self.gun, _serial())
		got = preorder.allocate_arrival("Stock Entry", se)
		self.assertEqual(len(got[self.gun]), 1)
		self.assertEqual(preorder.allocate_arrival("Stock Entry", se), {self.gun: []})
		self.assertTrue(_line(so)[SERIAL_FIELD])

	def test_ac10_counted_goods_are_put_by_and_come_off_the_shelf(self):
		so = self._preorder(item=self.acc, qty=2, rate=50, available=5)
		preorder.allocate_arrival("Stock Entry", _units_in(self.acc, 3))
		self.assertEqual(flt(_line(so)[RESERVED_FIELD]), 2)
		self.assertEqual(preorder.local_stock(self.acc, COMPANY), 1, "the shelf less the two put by")
		self.assertEqual(preorder.preorder_options(self.acc)["in_stock"], 1)
		from ffl_core.reservations import committed_units
		self.assertEqual(committed_units(self.acc).get(preorder.CHANNEL), 2, "every channel's count takes them off")
		from ffl_woo_sync.woocommerce.mapping import local_stock_for_item
		self.assertEqual(local_stock_for_item(self.acc), int(frappe.db.get_value("Bin", {"item_code": self.acc,
			"warehouse": SHELF}, "actual_qty")) - 2, "the website's count")
		# The register may sell the one left, not one of the two.
		sale = self._ticket(_customer(), {"item_code": self.acc, "qty": 2, "rate": 50})
		with self.assertRaisesRegex(frappe.ValidationError, "put by for pre-orders"):
			preorder.refuse_reserved_units(sale)
		preorder.refuse_reserved_units(self._ticket(_customer(), {"item_code": self.acc, "qty": 1, "rate": 50}))
		self.assertEqual(preorder.list_preorders(customer=self.cust)[0]["stage"], "ready_pickup")
		# The pickup sells the two put by: its own units do not stop it.
		ticket = preorder.open_pickup(so)["pos_invoice"]
		pos = _checkout(ticket)
		self.assertEqual([(r.item_code, flt(r.qty)) for r in pos.items if r.item_code != PREORDER_ITEM], [(self.acc, 2)])
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Completed")
		self.assertEqual(committed_units(self.acc).get(preorder.CHANNEL), 0)

	def test_partly_arrived_goods_wait(self):
		so = self._preorder(item=self.acc, qty=3, rate=50, available=5)
		preorder.allocate_arrival("Stock Entry", _units_in(self.acc, 2))
		self.assertEqual(flt(_line(so)[RESERVED_FIELD]), 2)
		self.assertEqual(preorder.list_preorders(customer=self.cust)[0]["stage"], "waiting")
		with self.assertRaisesRegex(frappe.ValidationError, "Not here yet"):
			preorder.open_pickup(so)
		preorder.allocate_arrival("Stock Entry", _units_in(self.acc, 5))
		self.assertEqual(flt(_line(so)[RESERVED_FIELD]), 3)

	def test_a_receipt_queues_the_arrival_after_it_commits(self):
		self._preorder()
		with patch.object(frappe, "enqueue") as enqueue:
			se = _gun_in(self.gun, _serial())
		calls = [c for c in enqueue.call_args_list if c.args and c.args[0] == preorder.ALLOCATE_JOB]
		self.assertEqual(len(calls), 1, enqueue.call_args_list)
		self.assertEqual((calls[0].kwargs["doctype"], calls[0].kwargs["name"], calls[0].kwargs["enqueue_after_commit"]),
			("Stock Entry", se, True))
		with patch.object(frappe, "enqueue") as enqueue:
			_units_in(self.acc, 1)   # nobody waits for it
		self.assertFalse([c for c in enqueue.call_args_list if c.args and c.args[0] == preorder.ALLOCATE_JOB])

	def _ticket(self, customer, *rows):
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": customer, "items": list(rows)})
		inv.set_missing_values()
		return inv


class HandOver(_Handover):

	def test_ac1_deposit_then_the_balance_at_hand_over(self):
		"""$200 cash, the gun arrives, $882.50 by card at pickup: the ticket books the sale (Dr Pre-order
		Deposits 200 + card 882.50, Cr Sales 1,000 + sales tax 82.50) and the gun's cost; nothing before it
		touched sales, tax, cost or stock. Net Sales of the ticket is the gun's 1,000."""
		from ffl_core.ffl_core.report.sales_report.sales_report import net_sales_of

		so = self._preorder()
		pay = _checkout(self._payment(so, 200))
		paid_si = _merge(pay.name)
		self.assertEqual(_gl(paid_si), {f"Cash - {ABBR}": 200, self.deposits: -200})
		serial = self._arrive_gun()
		self.assertFalse(frappe.get_all("GL Entry", filters={"voucher_no": so}), "putting a gun by books nothing")
		ticket = frappe.get_doc("POS Invoice", preorder.open_pickup(so)["pos_invoice"])
		self.assertEqual(ticket.ffl_preorder, so)
		goods = [(r.item_code, r.serial_no, flt(r.rate)) for r in ticket.items if r.item_code != PREORDER_ITEM]
		self.assertEqual(goods, [(self.gun, serial, 1000)])
		self.assertEqual([flt(r.amount) for r in ticket.items if r.item_code == PREORDER_ITEM], [-200])
		self.assertEqual((flt(ticket.total_taxes_and_charges), flt(ticket.grand_total)), (82.5, 882.5))
		self.assertEqual(preorder.open_pickup(so), {"pos_invoice": ticket.name, "already": True})
		_cover(ticket.name, serial)
		pos = _checkout(ticket.name, mode=CARD)
		self.assertEqual(str(pos.posting_date), nowdate())
		self.assertEqual(frappe.db.get_value("Sales Order", so, "status"), "Completed")
		self.assertEqual(preorder.held(so), 0)
		self.assertEqual(net_sales_of("POS Invoice", {"name": pos.name}), {pos.name: 1000})
		si = _merge(pos.name)
		card = frappe.db.get_value("Mode of Payment Account", {"parent": CARD, "company": COMPANY}, "default_account")
		folded = _accounts_by_type(_gl(si))
		self.assertEqual({k: folded.get(k) for k in (self.deposits, card, "income", "tax")},
			{self.deposits: 200, card: 882.5, "income": -1000, "tax": -82.5})
		self.assertEqual((folded.get("stock"), folded.get("expense")), (-700, 700), "the gun leaves stock at its cost")
		self.assertEqual(net_sales_of("Sales Invoice", {"name": si}), {si: 1000})
		self.assertFalse(preorder.list_preorders(customer=self.cust), "nothing left to do")

	def test_ac2_paid_in_full_pays_nothing_at_hand_over(self):
		so = self._preorder()
		_merge(_checkout(self._payment(so, 1082.5), mode=CARD).name)
		serial = self._arrive_gun()
		pos = self._hand_over(so, serial=serial, mode="Cash")
		self.assertEqual((flt(pos.grand_total), flt(pos.paid_amount)), (0, 0))
		folded = _accounts_by_type(_gl(_merge(pos.name)))
		self.assertEqual({k: folded.get(k) for k in (self.deposits, "income", "tax", f"Cash - {ABBR}")},
			{self.deposits: 1082.5, "income": -1000, "tax": -82.5, f"Cash - {ABBR}": None})

	def test_ac3_the_hand_over_books_on_its_own_day(self):
		"""Ordered and paid last month, handed over today: the sale and its tax are today's, and until
		then the order's money sat on Pre-order Deposits only."""
		so = self._preorder()
		frappe.db.set_value("Sales Order", so, "transaction_date", add_days(nowdate(), -40))
		pay_si = _merge(_checkout(self._payment(so, 500)).name)
		self.assertEqual(set(_gl(pay_si)), {f"Cash - {ABBR}", self.deposits})
		serial = self._arrive_gun()
		pos = self._hand_over(so, serial=serial)
		si = _merge(pos.name)
		self.assertEqual((str(pos.posting_date), str(frappe.db.get_value("Sales Invoice", si, "posting_date"))),
			(nowdate(), nowdate()))
		dates = set(frappe.get_all("GL Entry", filters={"voucher_no": si, "is_cancelled": 0}, pluck="posting_date"))
		self.assertEqual({str(d) for d in dates}, {nowdate()})

	def test_ac7_no_4473_no_hand_over_and_the_money_stays(self):
		so = self._preorder()
		_checkout(self._payment(so, 300))
		self._arrive_gun()
		ticket = preorder.open_pickup(so)["pos_invoice"]
		with self.assertRaisesRegex(frappe.ValidationError, "4473"):
			_checkout(ticket)
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 0)
		self.assertEqual(preorder.held(so), 300)
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Completed")

	def test_ac9_the_sub_ledger_after_hand_over(self):
		from ffl_core.ffl_core.report.pre_order_deposits import pre_order_deposits as report

		so = self._preorder()
		pay = _checkout(self._payment(so, 200))
		serial = self._arrive_gun()
		before = report.gl_balance(COMPANY, nowdate())
		pay_si = _merge(pay.name)
		self.assertEqual(report.gl_balance(COMPANY, nowdate()), flt(before + 200, 2))
		pos = self._hand_over(so, serial=serial)
		pos_si = _merge(pos.name)
		rows = {r["sales_order"]: r for r in report.build_rows(COMPANY, nowdate())}
		self.assertEqual({k: rows[so][k] for k in ("collected", "refunded", "applied", "balance", "status", "aging")},
			{"collected": 200, "refunded": 0, "applied": 200, "balance": 0, "status": "Completed", "aging": ""})
		# The order's own entries on the account net to its balance, and the account is back where it was.
		self.assertEqual(flt(-_gl(pay_si)[self.deposits] - _gl(pos_si)[self.deposits], 2), rows[so]["balance"])
		self.assertEqual(report.gl_balance(COMPANY, nowdate()), before)

	def test_a_payment_taken_off_the_hand_over_cannot_be_cancelled(self):
		so = self._preorder()
		pay = _checkout(self._payment(so, 200))
		serial = self._arrive_gun()
		self._hand_over(so, serial=serial)
		pay.reload()
		pay.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "taken off its pickup ticket"):
			pay.cancel()
		with self.assertRaisesRegex(frappe.ValidationError, "already been handed over"):
			preorder.cancel_preorder(so, "Changed mind")

	def test_the_hand_over_carries_the_whole_order(self):
		"""Two lines on one pre-order: the pickup ticket goes out with both or not at all, and a ticket
		with none of its goods keeps nothing it could take off."""
		frappe.db.set_value("Item", self.acc, AVAILABLE_FIELD, 3)
		frappe.db.set_value("Item", self.gun, AVAILABLE_FIELD, 3)
		so = preorder.create_preorder({"customer": self.cust, "items": [{"item_code": self.gun, "qty": 1, "rate": 1000},
			{"item_code": self.acc, "qty": 1, "rate": 50}]})["sales_order"]
		_checkout(self._payment(so, 100))
		serial = self._arrive_gun()
		self.assertEqual(preorder.list_preorders(customer=self.cust)[0]["stage"], "waiting", "the optic is not here")
		preorder.allocate_arrival("Stock Entry", _units_in(self.acc, 1))
		ticket = frappe.get_doc("POS Invoice", preorder.open_pickup(so)["pos_invoice"])
		ticket.items = [r for r in ticket.items if r.item_code != self.acc]
		ticket.save(ignore_permissions=True)
		_cover(ticket.name, serial)
		with self.assertRaisesRegex(frappe.ValidationError, "Hand everything"):
			_checkout(ticket.name)
		ticket.reload()
		ticket.items = [r for r in ticket.items if r.item_code == PREORDER_ITEM]
		with self.assertRaisesRegex(frappe.ValidationError, "comes off its pickup ticket"):
			ticket.save(ignore_permissions=True)
		self.assertEqual(preorder.held(so), 100)
		self.assertNotEqual(frappe.db.get_value("Sales Order", so, "status"), "Completed")

	def test_a_payment_or_pickup_open_blocks_the_other(self):
		so = self._preorder()
		self._arrive_gun()
		self._payment(so, 100)
		with self.assertRaisesRegex(frappe.ValidationError, "still open"):
			preorder.open_pickup(so)


class TaxAtHandOver(_Handover):
	"""AC6: the tax is the hand-over day's. A dearer rate is paid at the pickup; a cheaper one leaves
	money held, refunded after."""

	def _rate(self, rate: float) -> None:
		row = frappe.db.get_value("Sales Taxes and Charges", {"parent": self.template}, "name")
		was = frappe.db.get_value("Sales Taxes and Charges", row, "rate")
		frappe.db.set_value("Sales Taxes and Charges", row, "rate", rate)
		self.addCleanup(frappe.db.set_value, "Sales Taxes and Charges", row, "rate", was)

	def test_ac6_a_dearer_rate_is_paid_at_the_pickup(self):
		so = self._preorder()
		_checkout(self._payment(so, 1082.5), mode=CARD)
		serial = self._arrive_gun()
		self._rate(9)
		ticket = frappe.get_doc("POS Invoice", preorder.open_pickup(so)["pos_invoice"])
		self.assertEqual((flt(ticket.total_taxes_and_charges), flt(ticket.grand_total)), (90, 7.5))
		_cover(ticket.name, serial)
		pos = _checkout(ticket.name)
		folded = _accounts_by_type(_gl(_merge(pos.name)))
		self.assertEqual({k: folded.get(k) for k in (self.deposits, "income", "tax", f"Cash - {ABBR}")},
			{self.deposits: 1082.5, "income": -1000, "tax": -90, f"Cash - {ABBR}": 7.5})
		self.assertEqual(preorder.held(so), 0)

	def test_ac6_a_cheaper_rate_leaves_money_to_refund(self):
		"""Paid in full at 8.25%, handed over at 7%: the ticket comes to $0 and $12.50 stays held, which
		the queue offers back and the register refunds the way it was paid."""
		so = self._preorder()
		pay = _checkout(self._payment(so, 1082.5), mode=CARD)
		serial = self._arrive_gun()
		self._rate(7)
		with self.assertRaisesRegex(frappe.ValidationError, "Cancel the pre-order"):
			preorder.refund_preorder(so)
		pos = self._hand_over(so, serial=serial)
		self.assertEqual((flt(pos.total_taxes_and_charges), flt(pos.grand_total)), (70, 0))
		self.assertEqual(preorder.held(so), 12.5)
		row = next(r for r in preorder.list_preorders(customer=self.cust) if r["sales_order"] == so)
		self.assertEqual((row["stage"], row["handed_over"]), ("blocked", True))
		self.assertIn("12.50", row["blocked_reason"])
		refunds = preorder.refund_preorder(so)["refunds"]
		self.assertEqual([r["amount"] for r in refunds], [12.5])
		ret = _checkout(refunds[0]["pos_invoice"])
		self.assertEqual([p.mode_of_payment for p in ret.payments if flt(p.amount)], [CARD], "back the way it was paid")
		self.assertEqual(preorder.held(so), 0)
		self.assertFalse(preorder.list_preorders(customer=self.cust))
		card = frappe.db.get_value("Mode of Payment Account", {"parent": CARD, "company": COMPANY}, "default_account")
		_merge(pay.name)
		_merge(pos.name)
		self.assertEqual(_gl(_merge(ret.name)), {self.deposits: 12.5, card: -12.5})


class Holds(_Handover):

	def test_ac5_a_put_by_gun_is_not_the_counters_nor_the_websites(self):
		from ffl_core.reservations import counter_holds, serial_shelf_state

		so = self._preorder()
		serial = self._arrive_gun()
		hold = counter_holds(self.gun)[serial]
		self.assertEqual((hold["kind"], hold["ref"]), ("preorder", so))
		self.assertFalse(serial_shelf_state(serial)["sellable"], "the rule every channel reads")
		self.assertEqual(preorder.local_stock(self.gun, COMPANY), 0)
		other = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": _customer(), "items": [{"item_code": self.gun, "qty": 1, "rate": 900, "serial_no": serial,
				"use_serial_batch_fields": 1, "warehouse": SHELF}]})
		other.set_missing_values()
		other.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "held for pre-order"):
			other.insert()
		si = frappe.new_doc("Sales Invoice")
		si.update({"company": COMPANY, "customer": _customer(), "update_stock": 1})
		si.append("items", {"item_code": self.gun, "qty": 1, "rate": 900, "serial_no": serial, "warehouse": SHELF})
		with self.assertRaisesRegex(frappe.ValidationError, "held for pre-order"):
			preorder.refuse_held_serials(si)
		# Its own pickup ticket carries it, and the register's picker offers it there.
		ticket = preorder.open_pickup(so)["pos_invoice"]
		from ffl_core.firearm import _pos_picker_excluded_serials
		self.assertNotIn(serial, _pos_picker_excluded_serials([self.gun], current_invoice=ticket))
		self.assertIn(serial, _pos_picker_excluded_serials([self.gun]))

	def test_staff_change_the_gun(self):
		from ffl_core.reservations import serial_shelf_state

		so = self._preorder(available=5)
		other = self._preorder(customer=_customer(), available=4)
		one, two, three = self._arrive_gun(), self._arrive_gun(), self._arrive_gun()
		self.assertEqual((_line(so)[SERIAL_FIELD], _line(other)[SERIAL_FIELD]), (one, two))
		lines = preorder.serial_choices(so)["lines"]
		self.assertEqual((lines[0]["serial"], lines[0]["choices"]), (one, [one, three]), "not the other order's")
		with self.assertRaisesRegex(frappe.ValidationError, "not a free"):
			preorder.assign_serial(so, lines[0]["row"], two)
		self.assertEqual(preorder.assign_serial(so, lines[0]["row"], three), {"serial": three, "was": one})
		self.assertEqual(_line(so)[SERIAL_FIELD], three)
		self.assertTrue(serial_shelf_state(one)["sellable"], "the one it had is back on the shelf")
		preorder.open_pickup(so)
		with self.assertRaisesRegex(frappe.ValidationError, "open at the register"):
			preorder.assign_serial(so, lines[0]["row"], one)

	def test_cancel_after_arrival_puts_the_gun_back_without_an_entry(self):
		from ffl_core.reservations import serial_shelf_state

		so = self._preorder(available=2)
		_checkout(self._payment(so, 200))
		serial = self._arrive_gun()
		ledger = frappe.db.count("Stock Ledger Entry", {"item_code": self.gun, "is_cancelled": 0})
		out = preorder.cancel_preorder(so, "Found one elsewhere")
		self.assertEqual([r["amount"] for r in out["refunds"]], [200])
		self.assertFalse(_line(so)[SERIAL_FIELD])
		self.assertTrue(serial_shelf_state(serial)["sellable"])
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.gun, "is_cancelled": 0}), ledger)
		self.assertFalse(frappe.get_all("GL Entry", filters={"voucher_no": so}))
		self.assertEqual(frappe.db.get_value("Item", self.gun, AVAILABLE_FIELD), 2)

	def test_cancel_refused_while_the_pickup_is_open(self):
		so = self._preorder()
		self._arrive_gun()
		preorder.open_pickup(so)
		with self.assertRaisesRegex(frappe.ValidationError, "open at the register"):
			preorder.cancel_preorder(so, "No")


class NoBillingOutsideTheHandOver(_Handover):
	"""A pre-order made into a Sales Invoice or Delivery Note on the Desk would book the sale outside its
	hand-over and leave its money held for good: refused, saying where it is handed over."""

	def test_the_desk_cannot_bill_or_deliver_a_preorder(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice

		so = self._preorder(item=self.acc, qty=1, rate=50, available=3)
		for make in (make_sales_invoice, make_delivery_note):
			with self.subTest(make.__name__):
				doc = make(so)
				doc.flags.ignore_permissions = True
				with self.assertRaisesRegex(frappe.ValidationError, "Open pickup"):
					doc.insert()


class California(_Handover):
	"""AC8: 8.75% sales tax + 11% excise. Taken at the payment, neither; at the hand-over, both — unless
	the register ticks the peace-officer exemption, which takes the excise off the ticket and what is
	held off the rest."""
	TEMPLATE_TITLE = "_Test PO CA"
	PROFILE = "_Test PO CA Register"

	@classmethod
	def tax_rows(cls):
		from ffl_core.setup.tax import EXCISE_ACCOUNT_NAME
		from ffl_core.tests.test_preorder import _account
		return [(_account("_Test PO CA Sales Tax"), 8.75), (_account(EXCISE_ACCOUNT_NAME), 11)]

	def test_ac8_the_hand_over_charges_sales_tax_and_excise(self):
		so = self._preorder()
		pay = _checkout(self._payment(so, 200))
		self.assertEqual(_gl(_merge(pay.name)), {f"Cash - {ABBR}": 200, self.deposits: -200})
		serial = self._arrive_gun()
		pos = self._hand_over(so, serial=serial)
		self.assertEqual(flt(pos.grand_total), 997.5)
		gl = _gl(_merge(pos.name))
		sales_tax, excise = (a for a, _r in self.tax_rows())
		self.assertEqual({a: gl.get(a) for a in (sales_tax, excise, self.deposits)},
			{sales_tax: -87.5, excise: -110, self.deposits: 200})

	def test_the_peace_officer_exemption_ticked_at_the_register(self):
		from ffl_core.compliance import le_excise_exempt as le

		sales_tax, excise = (a for a, _r in self.tax_rows())
		was = frappe.db.get_single_value("FFL Settings", le.SETTING)
		frappe.db.set_single_value("FFL Settings", le.SETTING, excise)
		self.addCleanup(frappe.db.set_single_value, "FFL Settings", le.SETTING, was)
		ca = patch.object(le, "in_exempt_state", return_value=True)
		ca.start()
		self.addCleanup(ca.stop)
		so = self._preorder()
		_checkout(self._payment(so, 1197.5), mode=CARD)
		serial = self._arrive_gun()
		ticket = frappe.get_doc("POS Invoice", preorder.open_pickup(so)["pos_invoice"])
		cert = frappe.get_doc({"doctype": "File", "file_name": "230-fet.txt", "content": b"CDTFA-230-FET",
			"is_private": 1, "attached_to_doctype": "Customer", "attached_to_name": self.cust}).insert(
				ignore_permissions=True).file_url
		ticket.update({le.FLAG: 1, le.BUYER: "Active Peace Officer", le.CERT: cert})
		ticket.save(ignore_permissions=True)
		self.assertEqual([t.account_head for t in ticket.taxes if flt(t.tax_amount)], [sales_tax])
		self.assertEqual((flt(ticket.total_taxes_and_charges), flt(ticket.grand_total)), (87.5, 0))
		_cover(ticket.name, serial)
		pos = _checkout(ticket.name)
		self.assertEqual(preorder.held(so), 110, "the excise paid comes back")
		gl = _gl(_merge(pos.name))
		self.assertEqual({a: gl.get(a) for a in (sales_tax, excise, self.deposits)},
			{sales_tax: -87.5, excise: None, self.deposits: 1087.5})


class Hooks(FrappeTestCase):

	def test_wired(self):
		from ffl_core.tests.test_customer_profile import _hooks

		hooks = _hooks()
		ev = hooks["doc_events"]
		for dt in ("Purchase Receipt", "Stock Entry"):
			self.assertIn("ffl_core.preorder.on_stock_receipt", ev[dt]["on_submit"], dt)
		for dt in ("POS Invoice", "Sales Invoice", "Delivery Note"):
			for fn in ("refuse_held_serials", "refuse_billing_outside_handover"):
				self.assertIn(f"ffl_core.preorder.{fn}", ev[dt]["validate"], dt)
		pos = ev["POS Invoice"]
		self.assertEqual(pos["before_validate"][-1], "ffl_core.preorder.fit_release")
		before_submit = pos["before_submit"]
		self.assertLess(before_submit.index("ffl_core.preorder.refuse_reserved_units"),
			before_submit.index("ffl_core.compliance.web_hold.refuse_held_units"))
		self.assertIn("ffl_core.preorder.require_whole_order", before_submit)
		self.assertIn("ffl_core.preorder.refuse_reserved_units", ev["Sales Invoice"]["before_submit"])
		self.assertIn("ffl_core.preorder.on_invoice_submit", pos["on_submit"])
		self.assertIn("ffl_core.preorder.on_invoice_cancel", pos["on_cancel"])
		from ffl_core import hooks as module
		self.assertIn("ffl_core.preorder.channel_committed_units", module.channel_committed_units)

	def test_the_item_fields_are_on_the_form(self):
		setup.ensure()
		for field in (AVAILABLE_FIELD, setup.ETA_FIELD):
			self.assertEqual(frappe.db.get_value("Custom Field", f"Item-{field}", "hidden"), 0, field)
		for field in (SERIAL_FIELD, RESERVED_FIELD):
			self.assertEqual(frappe.db.get_value("Custom Field", f"Sales Order Item-{field}",
				["read_only", "no_copy", "allow_on_submit"]), (1, 1, 1), field)


class ArrivalRace(FrappeTestCase):
	"""Two receipts of the same item land at once, each run by its own job: under the Item's lock the
	second waits for the first and then fills the next order, never the one just filled.

	Real sessions in two threads, so the fixtures are committed (and removed in the finally below). The
	allocator's read of who waits is slowed so both jobs would sit between read and write together if
	the lock did not keep them apart."""

	def test_two_arrivals_fill_two_orders(self):
		setup.ensure()
		token = frappe.generate_hash(length=6).upper()
		item = f"_T-PO-ARR-{token}"
		company = COMPANY
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 1, AVAILABLE_FIELD: 2}).insert(ignore_permissions=True)
		customers = [frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test PO Arr {token} {i}",
			"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories"}
			).insert(ignore_permissions=True).name for i in range(2)]
		orders, receipts = [], []
		for c in customers:
			# The order as create_preorder leaves it, without needing a register set up on this site.
			so = frappe.get_doc({"doctype": "Sales Order", "company": company, "customer": c, setup.FLAG_FIELD: 1,
				"delivery_date": add_days(nowdate(), 7), "set_warehouse": SHELF,
				"items": [{"item_code": item, "qty": 1, "rate": 10}]})
			so.flags.ignore_permissions = True
			so.insert()
			so.submit()
			orders.append(so.name)
		receipts = [_units_in(item, 1) for _ in range(2)]
		frappe.db.commit()   # the job sessions must see them (removed in the finally below)
		site, results = frappe.local.site, []
		barrier = threading.Barrier(2)
		real = preorder._demands

		def slow_demands(item_code):
			found = real(item_code)
			if threading.current_thread() is not threading.main_thread():
				time.sleep(0.5)
			return found

		def job(receipt):
			frappe.init(site=site, sites_path=frappe.local.sites_path if hasattr(frappe.local, "sites_path") else ".")
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				barrier.wait(timeout=30)
				results.append(preorder.allocate_arrival("Stock Entry", receipt))
				frappe.db.commit()
			except Exception as e:
				results.append({"error": type(e).__name__ + ": " + str(e)})
				frappe.db.rollback()
			finally:
				frappe.destroy()

		try:
			with patch.object(preorder, "_demands", side_effect=slow_demands):
				threads = [threading.Thread(target=job, args=(r,)) for r in receipts]
				for t in threads:
					t.start()
				for t in threads:
					t.join(timeout=120)
			frappe.db.rollback()
			self.assertEqual(len(results), 2, results)
			self.assertFalse([r for r in results if "error" in r], results)
			got = {o: flt(frappe.db.get_value("Sales Order Item", {"parent": o}, RESERVED_FIELD)) for o in orders}
			self.assertEqual(got, {orders[0]: 1, orders[1]: 1}, results)
		finally:
			frappe.db.rollback()
			for se in receipts:
				doc = frappe.get_doc("Stock Entry", se)
				if doc.docstatus == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
				frappe.delete_doc("Stock Entry", se, force=True, ignore_permissions=True)
			for so in orders:
				doc = frappe.get_doc("Sales Order", so)
				if doc.docstatus == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
				frappe.delete_doc("Sales Order", so, force=True, ignore_permissions=True)
			for c in customers:
				frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
			for sle in frappe.get_all("Stock Ledger Entry", filters={"item_code": item}, pluck="name"):
				frappe.delete_doc("Stock Ledger Entry", sle, force=True, ignore_permissions=True)
			frappe.db.delete("Bin", {"item_code": item})
			frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
			frappe.db.commit()
