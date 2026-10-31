# apps/ffl_core/ffl_core/tests/test_only_active_serial.py
"""The A&D auto-fill rule shared by FFL Acquisition and FFL Disposition:
a blank serial is stamped only when the Item has exactly one Active serial."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.serials import only_active_serial


class OnlyActiveSerial(FrappeTestCase):
	def _item(self) -> str:
		code = f"OAS-{frappe.generate_hash(length=8)}"
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
				"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Used Firearm", "is_firearm": 1, "is_stock_item": 1,
			"has_serial_no": 1, "firearm_type": "Rifle"}).insert(ignore_permissions=True)
		return code

	def _serial(self, code: str, status: str = "Active") -> str:
		name = f"{code}-{frappe.generate_hash(length=6)}"
		frappe.get_doc({"doctype": "Serial No", "serial_no": name,
			"item_code": code}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", name, "status", status, update_modified=False)
		return name

	def test_one_active_serial_is_returned(self):
		code = self._item()
		only = self._serial(code)
		self._serial(code, status="Delivered")
		self.assertEqual(only_active_serial(code), only)

	def test_none_or_two_active_serials_return_none(self):
		code = self._item()
		self.assertIsNone(only_active_serial(code))
		self._serial(code)
		self._serial(code)
		self.assertIsNone(only_active_serial(code))
		self.assertIsNone(only_active_serial(None))

	def test_acquisition_fills_a_blank_serial_but_never_overwrites(self):
		code = self._item()
		only = self._serial(code)
		acq = frappe.new_doc("FFL Acquisition")
		acq.item = code
		acq.source_type = "Manufacturer"  # no source-ID / FFL checks to satisfy
		acq.validate()
		self.assertEqual(acq.serial, only)

		acq = frappe.new_doc("FFL Acquisition")
		acq.item = code
		acq.source_type = "Manufacturer"  # no source-ID / FFL checks to satisfy
		acq.serial = "SOMETHING-ELSE"
		acq.validate()
		self.assertEqual(acq.serial, "SOMETHING-ELSE")
