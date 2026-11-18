"""The counter's refusal of a gun a web order holds (``ffl_core.reservations.
refuse_channel_reserved_serials``) never stands in that order's own way: Dispose ships it and the
in-store pickup ticket hands it over. Real orders through the webhook handler on the web pre-order
fixtures (``test_preorder_web._Web``), the gun reserved for the order as it arrives."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from ffl_core.tests.test_preorder import COMPANY, _customer
from ffl_core.tests.test_preorder_handover import SHELF, _cover, _serial
from ffl_woo_sync.tests.test_preorder_web import _Web
from ffl_woo_sync.woocommerce import pickup
from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order

FFL = "1-99-888-01-2D-09999"


class OwnOrder(_Web):

	def _order_holding_a_gun(self) -> tuple[str, str]:
		self._slots(self.web_gun, 3)
		order = self._send(self._body([(self.web_gun, 1, 1000, 0)]))
		self._sync(order)
		gun = _serial()
		self._arrive(self.web_gun, gun)
		self._sync(order)
		self.assertEqual(frappe.db.get_value("Serial No", gun, ["woo_reserved", "woo_order"]), (1, order))
		return order, gun

	def _counter_ticket(self, gun: str):
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": _customer(), "items": [{"item_code": self.web_gun, "qty": 1, "rate": 1000, "serial_no": gun,
				"use_serial_batch_fields": 1, "warehouse": SHELF}]})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		return inv

	def test_the_counter_is_refused_and_the_order_still_ships(self):
		if not frappe.db.exists("ATF FFL Record", FFL):
			frappe.get_doc({"doctype": "ATF FFL Record", "ffl_number": FFL, "licensee_name": "Test Hold Gate Dealer LLC",
				"street": "1 Test Way", "city": "Austin", "state": "TX", "zip": "78701",
				"expiry_date": add_days(nowdate(), 365)}).insert(ignore_permissions=True)
		order, gun = self._order_holding_a_gun()
		with self.assertRaisesRegex(frappe.ValidationError, f"{gun} is reserved by web order {order}"):
			self._counter_ticket(gun).insert()
		frappe.db.set_value("Woo Online Order", order, "atf_ffl_record", FFL)
		with patch("frappe.enqueue"):
			out = dispose_web_order(order)
		self.assertEqual((out["disposed_count"], out["invoice_status"]), (1, "created"), out)
		self.assertNotEqual(frappe.db.get_value("Serial No", gun, "status"), "Active", "the gun left stock")
		invoice = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual((invoice.docstatus, invoice.woo_online_order), (1, order))

	def test_a_delivery_note_made_from_the_orders_invoice_passes(self):
		"""The web stock leg (``revenue.deliver_web_stock_lines``) delivers against the order's invoice."""
		from ffl_core.reservations import refuse_channel_reserved_serials

		order, gun = self._order_holding_a_gun()
		invoice = frappe.get_doc({"doctype": "Sales Invoice", "company": COMPANY, "customer": _customer(),
			"update_stock": 0, "woo_online_order": order,
			"items": [{"item_code": self.acc, "qty": 1, "rate": 10, "warehouse": SHELF}]})
		invoice.flags.ignore_permissions = True
		invoice.insert()
		note = frappe.new_doc("Delivery Note")
		note.update({"company": COMPANY, "customer": invoice.customer})
		row = note.append("items", {"item_code": self.web_gun, "qty": 1, "serial_no": gun, "warehouse": SHELF,
			"against_sales_invoice": invoice.name})
		refuse_channel_reserved_serials(note)
		row.against_sales_invoice = None
		with self.assertRaisesRegex(frappe.ValidationError, f"reserved by web order {order}"):
			refuse_channel_reserved_serials(note)

	def test_the_orders_own_pickup_ticket_hands_the_gun_over(self):
		order, gun = self._order_holding_a_gun()
		with patch.object(pickup, "is_own_license", return_value=True), patch("frappe.enqueue"):
			name = pickup._pickup(frappe.get_doc("Woo Online Order", order))["pos_invoice"]
			ticket = frappe.get_doc("POS Invoice", name)
			self.assertEqual((ticket.woo_online_order, ticket.items[0].serial_no), (order, gun))
			self.assertEqual(frappe.db.get_value("Serial No", gun, "woo_reserved"), 1, "held while the ticket is open")
			_cover(name, gun)
			ticket.reload()
			ticket.flags.ignore_permissions = True
			ticket.submit()
		self.assertEqual(ticket.docstatus, 1)
		self.assertEqual(frappe.db.get_value("Woo Online Order", order, "order_status"), "Transferred")
		self.assertEqual(frappe.db.get_value("Serial No", gun, ["woo_reserved", "woo_order"]), (0, ""))
