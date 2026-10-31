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
