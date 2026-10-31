"""Pending Transfer Pickups report — inbound customer-transfer guns awaiting
pickup.

A transfer-in received for a customer used to be indistinguishable from store
stock: no gun→customer link, no arrival notice, no aging. The fix is a
`transfer_for_customer` link on FFL Acquisition (plumbed from Receive Goods)
plus this report: every transfer gun still in the safe, who it belongs to, and
how many days it has been sitting.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.acquisition import record_firearm_acquisition


class PendingTransferPickups(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		# Fresh CI sites have no Customer at all — create ours explicitly.
		cls.customer = "_Test Pickup Customer"
		if not frappe.db.exists("Customer", cls.customer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cls.customer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "PTP-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "PTP-GUN", "item_name": "PTP-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)

	def _serial(self, name: str, status: str = "Active") -> str:
		if not frappe.db.exists("Serial No", name):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": name,
				"item_code": "PTP-GUN", "company": self.company,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", name, "status", status,
			update_modified=False)
		return name

	def _acq(self, serial: str, *, for_customer: str | None = None) -> str:
		return record_firearm_acquisition(
			item_code="PTP-GUN",
			serial=serial,
			source_type="FFL Dealer",
			source_name="Shipping Dealer LLC",
			source_ffl="1-23-456-78-9X-12345",
			cost=0,
			company=self.company,
			transfer_for_customer=for_customer,
		)

	def _run(self, filters=None):
		from ffl_core.ffl_core.report.pending_transfer_pickups.pending_transfer_pickups import (
			execute,
		)
		_, rows = execute(filters or {})
		return rows

	def test_acquisition_records_transfer_customer(self):
		serial = self._serial("PTP-S1")
		acq = self._acq(serial, for_customer=self.customer)
		self.assertEqual(
			frappe.db.get_value("FFL Acquisition", acq, "transfer_for_customer"),
			self.customer,
		)

	def test_active_transfer_gun_is_listed_with_aging(self):
		serial = self._serial("PTP-S2")
		self._acq(serial, for_customer=self.customer)
		rows = self._run()
		mine = [r for r in rows if r["serial"] == serial]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0]["customer"], self.customer)
		self.assertEqual(mine[0]["days_in_store"], 0)

	def test_picked_up_gun_not_listed(self):
		serial = self._serial("PTP-S3", status="Delivered")
		self._acq(serial, for_customer=self.customer)
		rows = self._run()
		self.assertFalse([r for r in rows if r["serial"] == serial])

	def test_plain_stock_acquisition_not_listed(self):
		serial = self._serial("PTP-S4")
		self._acq(serial)  # no transfer_for_customer → store stock
		rows = self._run()
		self.assertFalse([r for r in rows if r["serial"] == serial])

	def test_old_transfer_not_listed_after_serial_is_received_again(self):
		# Customer picked the gun up long ago; the store has since received the
		# same serial as its own stock. Only the newer acquisition is current.
		serial = self._serial("PTP-S5")
		old = self._acq(serial, for_customer=self.customer)
		frappe.db.set_value("FFL Acquisition", old, "acquisition_date",
			frappe.utils.add_days(frappe.utils.nowdate(), -300), update_modified=False)
		again = frappe.copy_doc(frappe.get_doc("FFL Acquisition", old))
		again.update({"name": f"{old}-AGAIN", "docstatus": 1,
			"transfer_for_customer": None,
			"acquisition_date": frappe.utils.nowdate()})
		again.db_insert()
		rows = self._run()
		self.assertFalse([r for r in rows if r["serial"] == serial])

	def test_newer_transfer_for_another_customer_is_the_one_listed(self):
		serial = self._serial("PTP-S6")
		old = self._acq(serial, for_customer=self.customer)
		frappe.db.set_value("FFL Acquisition", old, "acquisition_date",
			frappe.utils.add_days(frappe.utils.nowdate(), -300), update_modified=False)
		again = frappe.copy_doc(frappe.get_doc("FFL Acquisition", old))
		again.update({"name": f"{old}-AGAIN", "docstatus": 1,
			"acquisition_date": frappe.utils.nowdate()})
		again.db_insert()
		mine = [r for r in self._run() if r["serial"] == serial]
		self.assertEqual([r["acquisition"] for r in mine], [again.name])


if __name__ == "__main__":
	unittest.main()
