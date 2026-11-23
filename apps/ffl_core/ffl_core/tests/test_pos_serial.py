"""get_pos_reserved_serial_nos override: a consolidated POS ticket no longer
reserves its serial (a gun sold at the register and later bought back can be
rung up again); an open ticket still does, net of open return tickets.

Rows are written with db_insert (no controller validation): the query only
reads POS Invoice / POS Invoice Item columns, and the class rollback drops them.
The warehouse is a real one: the endpoint checks select on it, as upstream does.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.pos_serial import get_pos_reserved_serial_nos
from ffl_core.tests.crm_support import user

ITEM = "_TEST-POS-SERIAL-ITEM"
WH = None  # a leaf warehouse of the test company, set in setUpClass


def _ticket(serial, *, consolidated="", is_return=0):
	name = frappe.generate_hash(length=12)
	frappe.get_doc({
		"doctype": "POS Invoice", "name": name, "docstatus": 1,
		"is_return": is_return, "consolidated_invoice": consolidated,
	}).db_insert()
	frappe.get_doc({
		"doctype": "POS Invoice Item", "name": frappe.generate_hash(length=12),
		"parent": name, "parenttype": "POS Invoice", "parentfield": "items",
		"docstatus": 1, "item_code": ITEM, "warehouse": WH, "serial_no": serial,
	}).db_insert()


class PosReservedSerials(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		global WH
		WH = frappe.get_all("Warehouse", filters={"is_group": 0}, pluck="name", order_by="name", limit=1)[0]

	def tearDown(self):
		frappe.set_user("Administrator")

	def reserved(self):
		return get_pos_reserved_serial_nos({"item_code": ITEM, "warehouse": WH})

	def test_open_ticket_reserves(self):
		_ticket("PRS-OPEN")
		self.assertIn("PRS-OPEN", self.reserved())

	def test_consolidated_ticket_does_not_reserve(self):
		# The regression: sold, consolidated, bought back -> must be sellable.
		_ticket("PRS-SOLD", consolidated="SINV-TEST-1")
		self.assertNotIn("PRS-SOLD", self.reserved())

	def test_open_return_releases_open_sale(self):
		_ticket("PRS-RET")
		_ticket("PRS-RET", is_return=1)
		self.assertNotIn("PRS-RET", self.reserved())

	def test_json_filters_accepted(self):
		_ticket("PRS-JSON")
		out = get_pos_reserved_serial_nos(frappe.as_json({"item_code": ITEM, "warehouse": WH}))
		self.assertIn("PRS-JSON", out)

	def test_pos_page_call_resolves_to_override(self):
		# The exact path pos_controller.js calls, resolved the way frappe.handler
		# does: the last registered override must be ours, and it must behave as ours.
		upstream = "erpnext.stock.doctype.serial_no.serial_no.get_pos_reserved_serial_nos"
		resolved = frappe.override_whitelisted_method(upstream)
		self.assertEqual(resolved, "ffl_core.api.pos_serial.get_pos_reserved_serial_nos")
		_ticket("PRS-DISPATCH", consolidated="SINV-TEST-2")
		fn = frappe.get_attr(resolved)
		self.assertNotIn("PRS-DISPATCH", fn({"item_code": ITEM, "warehouse": WH}))
		pos_js = frappe.get_app_path("erpnext", "selling", "page", "point_of_sale", "pos_controller.js")
		with open(pos_js) as f:
			self.assertIn(upstream, f.read())

	def test_cashier_passes_the_gates(self):
		_ticket("PRS-CLERK")
		frappe.set_user(user("prs-clerk@example.com", "Sales User"))
		self.assertIn("PRS-CLERK", self.reserved())

	def test_needs_pos_invoice_read(self):
		frappe.set_user(user("prs-norole@example.com"))
		with self.assertRaises(frappe.PermissionError):
			self.reserved()

	def test_needs_an_existing_warehouse(self):
		frappe.set_user(user("prs-clerk@example.com", "Sales User"))
		with self.assertRaises(frappe.PermissionError):
			get_pos_reserved_serial_nos({"item_code": ITEM, "warehouse": "_No Such Warehouse"})
