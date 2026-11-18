"""The counter's refusal of a gun a GunBroker order holds (``ffl_core.reservations.
refuse_channel_reserved_serials``) never stands in that order's own way: its disposition takes the gun
out of stock and its revenue invoice books. A real GunBroker Order and real stock on the pre-order
hand-over fixtures; the bound-book write is the one Dispose makes (``fulfillment._book_dispositions``)."""
from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from ffl_core.compliance.dealer_transfer import book_disposition
from ffl_core.tests.test_preorder import COMPANY, _customer
from ffl_core.tests.test_preorder_handover import SHELF, _gun_in, _Handover, _serial

FFL = "1-99-888-01-2D-09999"


class OwnOrder(_Handover):

	def _line(self, gun: str) -> dict:
		return {"item_code": self.gun, "qty": 1, "rate": 900, "serial_no": gun, "use_serial_batch_fields": 1,
			"warehouse": SHELF}

	def test_the_counter_is_refused_and_the_order_still_goes_out(self):
		if not frappe.db.exists("ATF FFL Record", FFL):
			frappe.get_doc({"doctype": "ATF FFL Record", "ffl_number": FFL, "licensee_name": "Test Hold Gate Dealer LLC",
				"street": "1 Test Way", "city": "Austin", "state": "TX", "zip": "78701",
				"expiry_date": add_days(nowdate(), 365)}).insert(ignore_permissions=True)
		gun = _serial()
		_gun_in(self.gun, gun)
		order = frappe.get_doc({"doctype": "GunBroker Order", "gb_order_id": frappe.generate_hash(length=9),
			"order_status": "Pending Fulfillment"}).insert(ignore_permissions=True).name
		frappe.db.set_value("Serial No", gun, {"gb_reserved": 1, "gb_order": order}, update_modified=False)

		ticket = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": _customer(), "items": [self._line(gun)]})
		ticket.set_missing_values()
		ticket.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, f"{gun} is reserved by gunbroker order {order}"):
			ticket.insert()

		# The order's own revenue invoice, while the gun is still held for it.
		invoice = frappe.new_doc("Sales Invoice")
		invoice.update({"company": COMPANY, "customer": _customer(), "update_stock": 0, "gunbroker_order": order})
		invoice.append("items", self._line(gun))
		invoice.set_missing_values()
		invoice.flags.ignore_permissions = True
		invoice.insert()
		self.assertTrue(invoice.name)

		# Dispose's bound-book write: the gun leaves stock on the order's disposition.
		name = book_disposition(serial=gun, item=self.gun, link_field="gunbroker_order", link_name=order,
			destination=FFL, disposition_type="FFL Transfer", company=COMPANY, notes="GunBroker order")
		self.assertEqual(frappe.db.get_value("FFL Disposition", name, "docstatus"), 1)
		self.assertNotEqual(frappe.db.get_value("Serial No", gun, "status"), "Active", "the gun left stock")
