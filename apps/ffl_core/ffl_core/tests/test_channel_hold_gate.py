"""A gun a sales channel holds (a web order, a GunBroker order or auction bid) is not the counter's to
sell: ``reservations.refuse_channel_reserved_serials`` on POS Invoice / Sales Invoice / Delivery Note.
Real register tickets on the pre-order hand-over fixtures (a register, an open shift, a gun of the
test's own, FastBound off); the channels' own fulfilment is proved in their apps' tests."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import reservations
from ffl_core.tests.test_preorder import COMPANY, _checkout, _customer
from ffl_core.tests.test_preorder_handover import SHELF, _cover, _gun_in, _Handover, _serial

GATE = "ffl_core.reservations.refuse_channel_reserved_serials"


def hold(serial: str, **fields) -> None:
	"""What a channel leaves on the gun it holds (each channel writes its own flag and reference)."""
	frappe.db.set_value("Serial No", serial, fields, update_modified=False)


class CounterSale(_Handover):

	def _gun(self) -> str:
		serial = _serial()
		_gun_in(self.gun, serial)
		return serial

	def _ticket(self, serial: str):
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": _customer(), "items": [{"item_code": self.gun, "qty": 1, "rate": 900, "serial_no": serial,
				"use_serial_batch_fields": 1, "warehouse": SHELF}]})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		return inv

	def _sell(self, serial: str):
		"""Rung up, the 4473 done, checked out: the submitted ticket."""
		ticket = self._ticket(serial).insert().name
		_cover(ticket, serial)
		return _checkout(ticket)

	def test_a_free_gun_sells(self):
		self.assertEqual(self._sell(self._gun()).docstatus, 1)

	def test_a_gun_a_web_order_holds_is_refused_and_the_order_named(self):
		serial = self._gun()
		hold(serial, woo_reserved=1, woo_order="WEB-ORDER-A")
		with self.assertRaisesRegex(frappe.ValidationError, f"{serial} is reserved by web order WEB-ORDER-A"):
			self._sell(serial)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active", "still on the shelf")

	def test_a_gun_sold_on_gunbroker_after_the_ticket_was_parked_is_refused_at_checkout(self):
		serial = self._gun()
		ticket = self._ticket(serial).insert().name
		_cover(ticket, serial)
		hold(serial, gb_reserved=1, gb_order="GB-ORDER-A")
		with self.assertRaisesRegex(frappe.ValidationError, "reserved by gunbroker order GB-ORDER-A"):
			_checkout(ticket)

	def test_a_gun_with_bids_in_an_auction_is_refused(self):
		serial = self._gun()
		hold(serial, gb_bid_locked=1, gb_item_id="1234567")
		with self.assertRaisesRegex(frappe.ValidationError, "has bids in a GunBroker auction"):
			self._ticket(serial).insert()

	def test_a_stock_moving_invoice_and_a_delivery_note_are_refused_too(self):
		serial = self._gun()
		hold(serial, woo_reserved=1, woo_order="WEB-ORDER-A")
		for doctype in ("Sales Invoice", "Delivery Note"):
			doc = frappe.new_doc(doctype)
			doc.update({"company": COMPANY, "customer": _customer()})
			doc.append("items", {"item_code": self.gun, "qty": 1, "rate": 900, "serial_no": serial, "warehouse": SHELF})
			with self.assertRaisesRegex(frappe.ValidationError, "reserved by web order WEB-ORDER-A", msg=doctype):
				reservations.refuse_channel_reserved_serials(doc)

	def test_what_passes(self):
		"""A return, a merged invoice and a channel's own invoice (the web pickup ticket and the web /
		GunBroker revenue invoice) are not counter sales; nor is a gun whose hold was let go."""
		serial = self._gun()
		hold(serial, woo_reserved=1, woo_order="WEB-ORDER-A")
		ticket = self._ticket(serial)
		for flag in ({"is_return": 1}, {"is_consolidated": 1}, {"woo_online_order": "WEB-ORDER-A"},
				{"gunbroker_order": "GB-ORDER-A"}):
			probe = frappe.copy_doc(ticket)
			probe.update(flag)
			reservations.refuse_channel_reserved_serials(probe)
		hold(serial, woo_reserved=0, woo_order="")
		reservations.refuse_channel_reserved_serials(ticket)


class Hooks(FrappeTestCase):

	def test_wired_after_the_serials_are_canonical(self):
		events = frappe.get_hooks("doc_events")
		for doctype in ("POS Invoice", "Sales Invoice", "Delivery Note"):
			validate = events[doctype]["validate"]
			self.assertIn(GATE, validate, doctype)
			self.assertLess(validate.index("ffl_core.compliance.firearm.canonicalize_row_serials"),
				validate.index(GATE), doctype)
