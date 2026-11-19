"""Handing a custody gun back to its owner — the ONE exit for every firearm
this feature takes in, and until now the only path with no test at all.

``start_transfer_for_pos_invoice`` appends each gun row and lets
``set_missing_values()`` fill the warehouse, which yields the POS Profile's
warehouse — it never asked where the serial actually is. That was invisible
before 2026-07-20 because transfer guns sat in the sellable warehouse, so the
POS default happened to be right. Custody moved them to ``Customer Custody``,
and a POS Invoice line pointing at the wrong warehouse does not fail quietly:
this repo already carries the judgement, in manual_order.py's own note —

    "Ship each firearm from the warehouse its serial is actually in, not the
     POS profile default — otherwise update_stock 404s on the gun."

Create Order pins the row warehouse for exactly that reason. The POS transfer
flow did not. The failure would land at the counter, after the 4473 was already
started, with the customer standing there to collect their own property.

The 4473 kickoff itself is stubbed: this is about the invoice the function
builds, not about FastBound.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch


from ffl_core.setup import custody
from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM
from ffl_core.warehouses import default_stock_warehouse, ensure_custody_warehouse

CUSTOMER = "TCH Transfer Owner"


class ACustodyGunIsIssuedFromTheWarehouseItIsActuallyIn(FrappeTestCase):
	ITEM = "TCH-CUSTODY-GLOCK"
	CUSTODY_SERIAL = "TCH-CUSTODY-SN-1"
	SELLABLE_SERIAL = "TCH-SELLABLE-SN-1"

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		custody.ensure()
		from ffl_core.setup.transfer_fee import ensure as ensure_fee
		ensure_fee()
		# The transfer POS invoice pays by card; ERPNext needs the "Credit Card"
		# MoP's company account, which a full serial run inherited from an earlier module.
		from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account
		ensure_credit_card_mop_account()

		cls.company = (frappe.new_doc("POS Invoice").company
					   or frappe.db.get_single_value("Global Defaults", "default_company"))
		cls.sellable = default_stock_warehouse(cls.company)
		cls.custody_warehouse = ensure_custody_warehouse(cls.company)

		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": CUSTOMER,
				"customer_type": "Individual",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", cls.ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": cls.ITEM, "item_name": cls.ITEM,
				"item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1, "serial_no_series": f"{cls.ITEM}-.####",
			}).insert(ignore_permissions=True)

	def _ensure_pos_opening(self) -> None:
		"""The session user's Open shift, started today, on the test company's POS Profile: the transfer
		builds its invoice on the profile of the cashier's own Open shift. Made here rather than taken from
		the site: test_pos_close.py deletes every non-Closed opening entry, and a shift another module
		left Open for this user on another profile made ERPNext refuse a second one ('Cashier is
		currently assigned to another POS') — todays_pos_opening closes that one first."""
		from ffl_core.tests.pos_opening import ensure_pos_profile, todays_pos_opening
		todays_pos_opening(self.company, ensure_pos_profile(self.company))

	def setUp(self) -> None:
		super().setUp()
		self._ensure_pos_opening()
		for serial, warehouse, flag in (
				(self.CUSTODY_SERIAL, self.custody_warehouse, 1),
				(self.SELLABLE_SERIAL, self.sellable, 0)):
			if not frappe.db.exists("Serial No", serial):
				frappe.get_doc({
					"doctype": "Serial No", "serial_no": serial,
					"item_code": self.ITEM, "company": self.company,
				}).insert(ignore_permissions=True)
			frappe.db.set_value("Serial No", serial, {
				"warehouse": warehouse, "is_transfer_gun": flag, "status": "Active",
			}, update_modified=False)

	def _start(self, serial):
		"""Drive the real function; capture the invoice it built. The 4473
		kickoff is stubbed — FastBound is not what this is testing."""
		from ffl_integrations.fastbound import pos_4473

		captured = {}

		def _fake_kickoff(invoice_name, serial_map):
			captured["invoice"] = invoice_name
			return {"ok": True}

		with patch.object(pos_4473, "start_4473_for_pos_invoice", _fake_kickoff):
			pos_4473.start_transfer_for_pos_invoice(
				serials=[serial], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer=CUSTOMER, pos_profile="")
		return frappe.get_doc("POS Invoice", captured["invoice"])

	def _gun_row(self, doc, serial):
		return next(r for r in doc.items if (r.serial_no or "").strip() == serial)

	def test_the_gun_row_is_issued_from_the_custody_warehouse(self):
		"""The regression guard: the row must name the warehouse the gun
		is actually in, not the POS profile's."""
		doc = self._start(self.CUSTODY_SERIAL)
		self.assertEqual(
			self._gun_row(doc, self.CUSTODY_SERIAL).warehouse, self.custody_warehouse)

	def test_the_gun_row_is_not_left_on_the_pos_profile_default(self):
		"""Stated separately because it is the actual failure: a row pointing at
		the sellable warehouse has no stock to issue, and update_stock 404s on a
		gun the store is physically holding."""
		doc = self._start(self.CUSTODY_SERIAL)
		self.assertNotEqual(
			self._gun_row(doc, self.CUSTODY_SERIAL).warehouse, self.sellable)

	def test_an_ordinary_gun_still_issues_from_its_own_warehouse(self):
		"""The control — the pin follows the serial, it is not a custody
		special case. A transfer of a gun sitting in normal stock (the shape
		every transfer had before this run) must still work."""
		doc = self._start(self.SELLABLE_SERIAL)
		self.assertEqual(
			self._gun_row(doc, self.SELLABLE_SERIAL).warehouse, self.sellable)

	def test_the_gun_is_still_handed_over_at_zero_and_only_the_fee_is_charged(self):
		"""The delivery half, at the invoice level: the customer already owns
		the gun, so the only thing with a price on this ticket is the service."""
		doc = self._start(self.CUSTODY_SERIAL)
		self.assertEqual(float(self._gun_row(doc, self.CUSTODY_SERIAL).rate), 0.0)
		fee_rows = [r for r in doc.items if r.item_code == TRANSFER_FEE_ITEM]
		self.assertEqual(len(fee_rows), 1)
		self.assertEqual(float(fee_rows[0].rate), 25.0)
		self.assertEqual(
			float(sum(r.amount for r in doc.items)), 25.0)

	def test_the_invoice_is_marked_as_a_customer_transfer(self):
		doc = self._start(self.CUSTODY_SERIAL)
		self.assertTrue(doc.is_ffl_transfer)
