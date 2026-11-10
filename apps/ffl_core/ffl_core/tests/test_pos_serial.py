"""get_pos_reserved_serial_nos override: a consolidated POS ticket no longer
reserves its serial (a gun sold at the register and later bought back can be
rung up again); an open ticket still does, net of open return tickets.

Rows are written with db_insert (no controller validation): the query only
reads POS Invoice / POS Invoice Item columns, and the class rollback drops them.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.pos_serial import get_pos_reserved_serial_nos

ITEM = "_TEST-POS-SERIAL-ITEM"
WH = "_Test POS Serial WH"


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

	def test_hook_wires_override(self):
		from ffl_core import hooks
		self.assertEqual(
			hooks.override_whitelisted_methods[
				"erpnext.stock.doctype.serial_no.serial_no.get_pos_reserved_serial_nos"
			],
			"ffl_core.api.pos_serial.get_pos_reserved_serial_nos",
		)
