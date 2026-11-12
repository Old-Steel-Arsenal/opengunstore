"""pos_ticket.open_ticket: a draft register ticket on the open shift, built by the
shared counter-draft helpers, with the caller's lines checked at the boundary.

Site tests on the bootstrap company ("FFL Test") and the gunsmith fixtures' profile.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core.api import pos_ticket
from ffl_core.tests import test_gunsmith_work_order as gw

PART = "_Test GW Part"


class OpenTicket(gw._Base):

	def _open(self, items, **kw):
		return pos_ticket.open_ticket(self.cust.name, frappe.as_json(items), company=gw.COMPANY, **kw)

	def test_opens_a_draft_on_the_open_shift(self):
		out = self._open([{"item_code": PART, "qty": 2, "rate": 25}])
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		self.assertEqual(inv.docstatus, 0)
		self.assertEqual(inv.customer, self.cust.name)
		self.assertEqual(inv.pos_profile, gw.PROFILE, "rung on the shift that is open")
		self.assertEqual([(r.item_code, flt(r.qty), flt(r.rate)) for r in inv.items], [(PART, 2, 25)])
		self.assertEqual(out["grand_total"], flt(inv.rounded_total) or flt(inv.grand_total))
		self.assertEqual(flt(inv.grand_total), 53, "the profile's 6% tax applies")
		self.assertIn("Cash", out["payment_modes"], "the profile's payment rows are on the ticket")
		self.assertEqual(inv.update_stock, frappe.db.get_value("POS Profile", gw.PROFILE, "update_stock"))

	def test_a_list_payload_and_default_qty(self):
		out = pos_ticket.open_ticket(self.cust.name, [{"item_code": PART, "rate": 10}], company=gw.COMPANY)
		self.assertEqual(flt(frappe.db.get_value("POS Invoice Item", {"parent": out["pos_invoice"]}, "qty")), 1)

	def test_bad_lines_are_refused(self):
		for bad in ([], [{"qty": 1}], [{"item_code": "_No Such Item"}], [{"item_code": PART, "qty": 0}],
				[{"item_code": PART, "rate": -1}], [{"item_code": PART, "income_account": "x"}],
				[{"item_code": PART, "serial_no": "_NO-SUCH-SERIAL"}], ["x"]):
			with self.subTest(items=bad), self.assertRaises(frappe.ValidationError):
				self._open(bad)

	def test_unknown_customer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			pos_ticket.open_ticket("_No Such Customer", [{"item_code": PART}], company=gw.COMPANY)

	def test_no_open_shift_is_refused(self):
		open_entries = frappe.get_all("POS Opening Entry",
			filters={"status": "Open", "docstatus": 1, "company": gw.COMPANY}, pluck="name")
		for name in open_entries:
			frappe.db.set_value("POS Opening Entry", name, "status", "Closed", update_modified=False)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "No POS shift is open"):
				self._open([{"item_code": PART}])
		finally:
			for name in open_entries:
				frappe.db.set_value("POS Opening Entry", name, "status", "Open", update_modified=False)

	def test_counter_roles_only(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			self._open([{"item_code": PART}])


# Priced items for the discount tests: code -> (list price, Item.max_discount).
_PRICED = {"_Test Ticket Disc 20": (20, 0), "_Test Ticket Disc 1999": (19.99, 0),
	"_Test Ticket Disc 1234": (1234.56, 0), "_Test Ticket Disc Cap": (20, 10)}


class OpenTicketDiscount(gw._Base):
	"""A caller's rate under the list price is the line's discount off it, and the rate it saves."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for code, (price, cap) in _PRICED.items():
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Parts",
					"stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
			frappe.db.set_value("Item", code, "max_discount", cap)
			name = frappe.db.get_value("Item Price", {"item_code": code, "price_list": "Standard Selling"})
			if name:
				frappe.db.set_value("Item Price", name, "price_list_rate", price)
			else:
				frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": "Standard Selling",
					"selling": 1, "price_list_rate": price}).insert(ignore_permissions=True)
		frappe.clear_cache(doctype="Item")

	def _ticket(self, items):
		out = pos_ticket.open_ticket(self.cust.name, frappe.as_json(items), company=gw.COMPANY)
		return out, frappe.get_doc("POS Invoice", out["pos_invoice"])

	def _row(self, row):
		return (row.item_code, flt(row.price_list_rate), flt(row.rate), flt(row.discount_percentage),
			flt(row.discount_amount))

	def test_rate_below_list_shows_its_discount(self):
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 20", "rate": 17}])
		self.assertEqual(self._row(inv.items[0]), ("_Test Ticket Disc 20", 20, 17, 15, 3))

	def test_mixed_lines(self):
		out, inv = self._ticket([
			{"item_code": "_Test Ticket Disc 20", "qty": 2, "rate": 15},     # 25% off
			{"item_code": "_Test Ticket Disc 1999", "rate": 16.99},          # 15% off $19.99, to the cent
			{"item_code": "_Test Ticket Disc 20"},                           # list price
			{"item_code": "_Test Ticket Disc 20", "rate": 20},               # list price, typed
		])
		self.assertEqual([self._row(r) for r in inv.items], [
			("_Test Ticket Disc 20", 20, 15, 25, 5),
			("_Test Ticket Disc 1999", 19.99, 16.99, 15.01, 3),
			("_Test Ticket Disc 20", 20, 20, 0, 0),
			("_Test Ticket Disc 20", 20, 20, 0, 0),
		])
		# Totals are the rates': 30 + 16.99 + 20 + 20 = 86.99, plus the profile's 6% (5.22).
		self.assertEqual(flt(inv.net_total), 86.99)
		self.assertEqual(flt(inv.total_taxes_and_charges), 5.22)
		self.assertEqual(flt(inv.grand_total), 92.21)
		self.assertEqual(out["grand_total"], flt(inv.rounded_total) or flt(inv.grand_total))

	def test_rounding_edge_keeps_the_rate(self):
		# 15% off $19.99 is $16.9915: the caller's cent rate stays, the shown % is the one that gives it.
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 1999", "rate": 16.99},
			{"item_code": "_Test Ticket Disc 1999", "rate": 0.01}])
		self.assertEqual(self._row(inv.items[0]), ("_Test Ticket Disc 1999", 19.99, 16.99, 15.01, 3))
		self.assertEqual(self._row(inv.items[1]), ("_Test Ticket Disc 1999", 19.99, 0.01, 99.95, 19.98))

	def test_zero_rate_is_all_off(self):
		# ERPNext prices a line with no rate from its list price: a caller's $0 is 100% off, not $20.
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 20", "rate": 0}])
		self.assertEqual(self._row(inv.items[0]), ("_Test Ticket Disc 20", 20, 0, 100, 20))

	def test_percentage_that_cannot_give_the_rate_back(self):
		# 1000 off 1234.56 is 18.9995%: at two places 19% would save 999.99, so the rate wins and the
		# discount is the amount alone (what ERPNext saves for a typed rate).
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 1234", "rate": 1000}])
		self.assertEqual(self._row(inv.items[0]), ("_Test Ticket Disc 1234", 1234.56, 1000, 0, 234.56))

	def test_over_the_items_max_discount_still_opens(self):
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc Cap", "rate": 15}])
		self.assertEqual(self._row(inv.items[0]), ("_Test Ticket Disc Cap", 20, 15, 0, 5))

	def test_rate_above_list(self):
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 20", "rate": 25}])
		row = inv.items[0]
		self.assertEqual(self._row(row), ("_Test Ticket Disc 20", 20, 25, 0, 0))
		self.assertEqual((row.margin_type, flt(row.margin_rate_or_amount)), ("Amount", 5))

	def test_no_list_price(self):
		_out, inv = self._ticket([{"item_code": PART, "rate": 12.34}])
		self.assertEqual((flt(inv.items[0].price_list_rate), flt(inv.items[0].rate),
			flt(inv.items[0].discount_percentage), flt(inv.items[0].discount_amount)), (0, 12.34, 0, 0))

	def test_saving_again_keeps_rates(self):
		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 20", "rate": 17},
			{"item_code": "_Test Ticket Disc 1999", "rate": 16.99},
			{"item_code": "_Test Ticket Disc 1234", "rate": 1000}])
		first = [self._row(r) for r in inv.items]
		for _ in range(2):
			inv = frappe.get_doc("POS Invoice", inv.name)
			inv.save()
			self.assertEqual([self._row(r) for r in frappe.get_doc("POS Invoice", inv.name).items], first)

	def test_price_approval_reads_the_same(self):
		"""What the register's submit would ask: nothing for the rates the ticket was saved at; below
		list (the approval record's test) every discounted line, at the caller's rate."""
		from ffl_core import price_approval

		_out, inv = self._ticket([{"item_code": "_Test Ticket Disc 20", "rate": 17},
			{"item_code": "_Test Ticket Disc 1999", "rate": 16.99},
			{"item_code": "_Test Ticket Disc 20"}])
		stored = frappe.get_doc("POS Invoice", inv.name)
		self.assertEqual(price_approval.reductions(inv, stored)["lines"], [])
		self.assertEqual([(l["item_code"], l["list_rate"], l["rate"])
			for l in price_approval.reductions(inv, against_list=True)["lines"]],
			[("_Test Ticket Disc 20", 20, 17), ("_Test Ticket Disc 1999", 19.99, 16.99)])
		# A new ticket judged as unsaved (the register's own first save) asks for the same two lines.
		self.assertEqual([l["item_code"] for l in price_approval.reductions(inv)["lines"]],
			["_Test Ticket Disc 20", "_Test Ticket Disc 1999"])
