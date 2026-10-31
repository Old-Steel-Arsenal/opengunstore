"""Dealer shipments auto-book Dealer Transfer dispositions.

Selling a gun to another FFL skips the 4473 (lawful — dealer-to-dealer), but
the bound book still needs a Disposition the moment stock leaves. Historically
that was a separate manual Desk step nobody enforced ("sold but never
de-booked" — the classic ATF audit finding). Now:

  - validate (DN / SI): shipping firearms to an is_ffl_dealer customer
	requires their FFL to be Verified AND unexpired (stricter than the
	purchase side — we're handing the gun over);
  - on_submit (DN, or SI with update_stock=1): one submitted Dealer Transfer
	FFL Disposition per serial, destination_ffl resolved to the ATF FFL
	Record; the existing FastBound push pipeline takes it from there;
  - no double stock issue: the controller already moved the serial to
	Delivered, so disposition_stock's status gate skips its own Material
	Issue (pinned by test);
  - on_cancel mirrors: dispositions cancelled with the shipping document.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

DEALER = "_Test Dealer FFL Shipping"
DEALER_FFL = "1-99-888-01-2D-00001"


def _ns_ship(customer: str, *rows: dict, doctype: str = "Sales Invoice",
		docstatus: int = 1, update_stock: int = 1) -> SimpleNamespace:
	items = []
	for i, row in enumerate(rows):
		base = {"idx": i + 1, "item_code": "DLT-GUN", "serial_no": ""}
		base.update(row)
		items.append(SimpleNamespace(**base))
	return SimpleNamespace(doctype=doctype, name=f"NS-{doctype[:2]}-1",
		docstatus=docstatus, customer=customer, update_stock=update_stock,
		posting_date=frappe.utils.nowdate(), items=items)


class DealerTransferFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Item", "DLT-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "DLT-GUN", "item_name": "DLT-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("ATF FFL Record", DEALER_FFL):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": DEALER_FFL,
				"licensee_name": "Test Shipping Dealer LLC",
			}).insert(ignore_permissions=True)
		cls._set_dealer(ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), 365))

	@classmethod
	def _set_dealer(cls, **values) -> None:
		base = {"is_ffl_dealer": 1, "ffl_number": DEALER_FFL}
		base.update(values)
		frappe.db.set_value("Customer", DEALER, base, update_modified=False)

	def _stocked_serial(self, serial: str) -> str:
		"""Put one DLT-GUN with this serial into the test warehouse."""
		from ffl_core.firearm import serialized_receipt_row
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			return serial
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code="DLT-GUN", qty=1,
				warehouse=frappe.db.get_value("Warehouse",
					{"company": self.company, "is_group": 0}, "name"),
				basic_rate=500, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return serial


class ShipGate(DealerTransferFixtures):
	def _validate(self, doc):
		from ffl_core.compliance.dealer_transfer import require_shippable_ffl_dealer
		require_shippable_ffl_dealer(doc)

	def test_unverified_dealer_blocked(self):
		self._set_dealer(ez_check_status="Pending")
		with self.assertRaises(frappe.ValidationError):
			self._validate(_ns_ship(DEALER, {}))

	def test_expired_ffl_blocked(self):
		self._set_dealer(ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), -1))
		with self.assertRaises(frappe.ValidationError):
			self._validate(_ns_ship(DEALER, {}))

	def test_verified_unexpired_passes(self):
		self._set_dealer(ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), 365))
		self._validate(_ns_ship(DEALER, {}))  # no raise

	def test_non_dealer_customer_skipped(self):
		"""Retail customers go through the 4473 gate instead."""
		if not frappe.db.exists("Customer", "_Test Retail Buyer DLT"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Retail Buyer DLT",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		self._validate(_ns_ship("_Test Retail Buyer DLT", {}))  # no raise

	def test_non_firearm_rows_skipped(self):
		self._set_dealer(ez_check_status="Pending")
		if not frappe.db.exists("Item", "DLT-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "DLT-ACC", "item_name": "DLT-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		self._validate(_ns_ship(DEALER, {"item_code": "DLT-ACC"}))  # no raise
		self._set_dealer(ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), 365))

	def test_draft_not_blocked(self):
		self._set_dealer(ez_check_status="Pending")
		self._validate(_ns_ship(DEALER, {}, docstatus=0))  # no raise
		self._set_dealer(ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), 365))


class AutoUpdateStock(DealerTransferFixtures):
	"""A dealer firearm invoice IS the shipping document unless a Delivery
	Note is in play — Update Stock gets enabled automatically on creation,
	so staff can't accidentally submit a billing-only invoice that never
	moves the gun or books the disposition. Unchecking after creation is
	respected (the DN path stays available)."""

	def _new_si(self, customer: str, item_code: str = "DLT-GUN") -> "frappe.Document":
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer
		si.append("items", {"item_code": item_code, "qty": 1, "rate": 900,
			"use_serial_batch_fields": 1})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		return si

	def test_dealer_firearm_invoice_auto_enables_update_stock(self):
		si = self._new_si(DEALER)
		self.assertEqual(si.update_stock, 1)

	def test_dn_billed_invoice_left_alone(self):
		"""Rows referencing a Delivery Note = stock already shipped (unit-level:
		a real DN needs stocked serials; the branch only reads the row field)."""
		from types import SimpleNamespace
		from ffl_core.compliance.dealer_transfer import (
			auto_enable_update_stock_for_dealer_shipment,
		)
		doc = SimpleNamespace(
			doctype="Sales Invoice", is_return=0, is_consolidated=0,
			update_stock=0, customer=DEALER,
			items=[frappe._dict(idx=1, item_code="DLT-GUN",
				delivery_note="DN-0001")],
		)
		auto_enable_update_stock_for_dealer_shipment(doc)
		self.assertEqual(doc.update_stock, 0)

	def test_non_dealer_invoice_left_alone(self):
		if not frappe.db.exists("Customer", "_Test Retail Buyer DLT"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Retail Buyer DLT",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		si = self._new_si("_Test Retail Buyer DLT")
		self.assertEqual(si.update_stock, 0)

	def test_accessory_only_invoice_left_alone(self):
		if not frappe.db.exists("Item", "DLT-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "DLT-ACC", "item_name": "DLT-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		si = self._new_si(DEALER, item_code="DLT-ACC")
		self.assertEqual(si.update_stock, 0)


class SerialsForLine(FrappeTestCase):
	def test_newline_string_path(self):
		from ffl_core.serials import serials_for_line
		row = frappe._dict({"serial_no": "A1\nA2\n  \nA3"})
		self.assertEqual(serials_for_line(row), ["A1", "A2", "A3"])

	def test_empty_row(self):
		from ffl_core.serials import serials_for_line
		self.assertEqual(serials_for_line(frappe._dict({})), [])


class DealerShipmentBooks(DealerTransferFixtures):
	def _ship(self, serial: str) -> "frappe.Document":
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = DEALER
		si.update_stock = 1
		si.append("items", {
			"item_code": "DLT-GUN", "qty": 1, "rate": 900,
			"warehouse": frappe.db.get_value("Warehouse",
				{"company": self.company, "is_group": 0}, "name"),
			"serial_no": serial, "use_serial_batch_fields": 1,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def _disposition(self, si_name: str):
		return frappe.db.get_value(
			"FFL Disposition",
			{"sales_invoice": si_name, "docstatus": 1},
			["name", "disposition_type", "destination_ffl", "customer",
				"serial", "stock_entry"],
			as_dict=True,
		)

	def test_shipping_si_books_dealer_transfer(self):
		serial = self._stocked_serial("DLT-S1")
		si = self._ship(serial)
		disp = self._disposition(si.name)
		self.assertTrue(disp)
		self.assertEqual(disp.disposition_type, "Dealer Transfer")
		self.assertEqual(disp.destination_ffl, DEALER_FFL)
		self.assertEqual(disp.customer, DEALER)
		self.assertEqual(disp.serial, serial)
		# Double-issue pin: the SI moved the stock; the disposition must NOT
		# have issued its own Stock Entry (serial was already Delivered).
		self.assertFalse(disp.stock_entry)
		self.assertEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Delivered")

	def test_billing_only_si_books_nothing(self):
		"""Update Stock now defaults ON for dealer firearm invoices, so the
		billing-only (DN-ships-later) path means deliberately unchecking it
		AFTER creation — which must stick, and must not book a disposition."""
		serial = self._stocked_serial("DLT-S2")
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = DEALER
		si.append("items", {"item_code": "DLT-GUN", "qty": 1, "rate": 900,
			"serial_no": serial, "use_serial_batch_fields": 1})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		self.assertEqual(si.update_stock, 1)  # auto-enabled at creation
		si.update_stock = 0  # staff opts into the DN path
		si.save()
		si.submit()
		self.assertEqual(si.update_stock, 0)  # the uncheck stuck
		self.assertFalse(self._disposition(si.name))

	def test_rebooking_is_idempotent(self):
		from ffl_core.compliance.dealer_transfer import create_dealer_dispositions
		serial = self._stocked_serial("DLT-S3")
		si = self._ship(serial)
		create_dealer_dispositions(si)  # re-run the hook
		count = frappe.db.count("FFL Disposition",
			{"sales_invoice": si.name, "docstatus": ("<", 2)})
		self.assertEqual(count, 1)

	def test_book_disposition_is_idempotent_per_link(self):
		"""The shared helper the web and GunBroker paths also book through:
		a second call for the same {serial, link} returns None, books nothing."""
		from ffl_core.compliance.dealer_transfer import book_disposition, serial_company
		serial = self._stocked_serial("DLT-S5")
		self.assertEqual(serial_company(serial), self.company)
		si = self._ship(serial)
		disp = self._disposition(si.name)
		again = book_disposition(
			serial=serial, item="DLT-GUN", link_field="sales_invoice",
			link_name=si.name, destination=disp.destination_ffl,
			disposition_type="Dealer Transfer", company=self.company, notes="retry")
		self.assertIsNone(again)
		self.assertEqual(frappe.db.count("FFL Disposition",
			{"sales_invoice": si.name, "docstatus": ("<", 2)}), 1)

	def test_cancel_si_cancels_disposition(self):
		serial = self._stocked_serial("DLT-S4")
		si = self._ship(serial)
		disp = self._disposition(si.name)
		si.reload()
		si.cancel()
		self.assertEqual(
			frappe.db.get_value("FFL Disposition", disp.name, "docstatus"), 2)


if __name__ == "__main__":
	unittest.main()
