"""Customer/Supplier ↔ ATF FFL Record linkage.

Dealer parties used to carry a free-text ffl_number whose only tie to the ATF
directory (ATF FFL Record, name == FFL number) was string equality — one typo
and the Search-ATF fill, the shipping gate and the disposition destination all
silently diverge. Now:

  - `atf_ffl_record` Link on Customer + Supplier is the formal association;
	on validate the party's FFL fields are synced FROM the directory record
	(ffl_number forced equal — the directory is the source of truth);
  - a party with a bare ffl_number that matches a directory record gets the
	link backfilled automatically (old data heals on next save);
  - create_party_from_atf builds a dealer Customer/Supplier straight from a
	directory record (idempotent — ffl_number is UNIQUE per party type, so a
	party already carrying the number is returned instead of duplicated);
  - dealer shipments resolve destination_ffl from the link first.

Each test uses its own directory record: ffl_number is unique on Customer and
Supplier, so two test parties can never share one.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase


class AtfLinkFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)

	def _rec(self, suffix: str) -> str:
		"""A directory record private to this test (unique FFL number)."""
		number = f"1-99-888-01-2D-{suffix:0>5}"
		if not frappe.db.exists("ATF FFL Record", number):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": number,
				"licensee_name": f"Linked Dealer {suffix} LLC",
				"license_type": "01 - Dealer in Firearms",
				"street": "42 Range Rd", "city": "Tulsa", "state": "OK",
				"zip": "74101",
				"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 400),
			}).insert(ignore_permissions=True)
		return number

	def _customer(self, name: str, **fields) -> "frappe.Document":
		if frappe.db.exists("Customer", name):
			frappe.db.set_value("Customer", name, {
				"atf_ffl_record": None, "ffl_number": None,
				"licensee_name": None, "ffl_expiry_date": None, "cfd_number": None,
			}, update_modified=False)
			doc = frappe.get_doc("Customer", name)
		else:
			doc = frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "Commercial", "territory": "All Territories",
			})
			doc.flags.ignore_permissions = True
			doc.insert()
		doc.update({"is_ffl_dealer": 1, **fields})
		doc.flags.ignore_permissions = True
		doc.save()
		return doc


class LinkSync(AtfLinkFixtures):
	def test_link_set_syncs_fields_on_save(self):
		rec = self._rec("70001")
		c = self._customer("_Test ATF Link A", atf_ffl_record=rec)
		self.assertEqual(c.ffl_number, rec)
		self.assertEqual(c.licensee_name, "Linked Dealer 70001 LLC")
		self.assertIn("Tulsa", c.licensee_address or "")
		self.assertEqual(str(c.ffl_expiry_date),
			str(frappe.utils.add_days(frappe.utils.nowdate(), 400)))
		self.assertEqual(c.ffl_license_type, "01")

	def test_mismatched_ffl_number_corrected(self):
		rec = self._rec("70002")
		c = self._customer("_Test ATF Link B", atf_ffl_record=rec,
			ffl_number="9-99-999-99-9X-99999")
		self.assertEqual(c.ffl_number, rec)

	def test_bare_ffl_number_backfills_link(self):
		rec = self._rec("70003")
		c = self._customer("_Test ATF Link C", ffl_number=rec)
		self.assertEqual(c.atf_ffl_record, rec)
		self.assertEqual(c.licensee_name, "Linked Dealer 70003 LLC")

	def test_unknown_ffl_number_left_alone(self):
		c = self._customer("_Test ATF Link D", ffl_number="1-11-111-11-1A-11111")
		self.assertFalse(c.atf_ffl_record)
		self.assertEqual(c.ffl_number, "1-11-111-11-1A-11111")

	def test_non_dealer_untouched(self):
		rec = self._rec("70004")
		c = self._customer("_Test ATF Link E", is_ffl_dealer=0, ffl_number=rec)
		self.assertFalse(c.atf_ffl_record)


class CfdNumber(AtfLinkFixtures):
	"""CA CFD number: typed by hand, one value per FFL, mirrored on linked parties."""

	def _ca_rec(self, suffix: str) -> str:
		number = self._rec(suffix)
		frappe.db.set_value("ATF FFL Record", number, {"state": "CA", "cfd_number": None})
		return number

	def test_party_edit_writes_record(self):
		rec = self._ca_rec("70101")
		c = self._customer("_Test CFD A", atf_ffl_record=rec, cfd_number=" CFD-111 ")
		self.assertEqual(c.cfd_number, "CFD-111")
		self.assertEqual(frappe.db.get_value("ATF FFL Record", rec, "cfd_number"), "CFD-111")
		# A later save that doesn't touch the CFD keeps it.
		c.reload()
		c.flags.ignore_permissions = True
		c.save()
		self.assertEqual(frappe.db.get_value("ATF FFL Record", rec, "cfd_number"), "CFD-111")
		# Clearing it on the party clears the record.
		c.cfd_number = ""
		c.save()
		self.assertIsNone(frappe.db.get_value("ATF FFL Record", rec, "cfd_number"))

	def test_new_party_takes_record_value(self):
		rec = self._ca_rec("70102")
		frappe.db.set_value("ATF FFL Record", rec, "cfd_number", "CFD-222")
		c = self._customer("_Test CFD B", atf_ffl_record=rec)
		self.assertEqual(c.cfd_number, "CFD-222")

	def test_record_edit_reaches_linked_parties(self):
		rec = self._ca_rec("70103")
		c = self._customer("_Test CFD C", atf_ffl_record=rec)
		r = frappe.get_doc("ATF FFL Record", rec)
		r.cfd_number = "CFD-333"
		r.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Customer", c.name, "cfd_number"), "CFD-333")

	def test_record_edit_reaches_linked_supplier(self):
		rec = self._ca_rec("70104")
		name = "_Test CFD Supplier"
		if frappe.db.exists("Supplier", name):
			s = frappe.get_doc("Supplier", name)
		else:
			s = frappe.get_doc({"doctype": "Supplier", "supplier_name": name})
			s.flags.ignore_permissions = True
			s.insert()
		s.update({"is_ffl_dealer": 1, "atf_ffl_record": rec, "ffl_number": "", "cfd_number": None})
		s.flags.ignore_permissions = True
		s.save()
		r = frappe.get_doc("ATF FFL Record", rec)
		r.cfd_number = "CFD-444"
		r.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Supplier", name, "cfd_number"), "CFD-444")

	def test_heal_link_copies_cfd(self):
		from ffl_core.api.atf_link import _create_party_from_atf
		rec = self._ca_rec("70105")
		frappe.db.set_value("ATF FFL Record", rec, "cfd_number", "CFD-555")
		c = self._customer("_Test CFD Heal", is_ffl_dealer=0, ffl_number=rec)
		out = _create_party_from_atf(rec, "Customer")
		self.assertEqual(out["name"], c.name)
		self.assertEqual(frappe.db.get_value("Customer", c.name, "cfd_number"), "CFD-555")


class CreateFromRecord(AtfLinkFixtures):
	def test_create_customer_from_atf(self):
		from ffl_core.api.atf_link import create_party_from_atf
		rec = self._rec("70005")
		out = create_party_from_atf(rec, "Customer")
		c = frappe.get_doc("Customer", out["name"])
		self.assertEqual(c.is_ffl_dealer, 1)
		self.assertEqual(c.atf_ffl_record, rec)
		self.assertEqual(c.ffl_number, rec)
		self.assertEqual(c.customer_name, "Linked Dealer 70005 LLC")
		# A licensed business is a Company whatever the site's default Customer Type
		# is (default = Individual): the dealer must not become a person.
		self.assertEqual(c.customer_type, "Company")
		# Idempotent: second call returns the same party, creates nothing new.
		again = create_party_from_atf(rec, "Customer")
		self.assertEqual(again["name"], out["name"])
		self.assertFalse(again.get("created"))

	def test_create_supplier_from_atf(self):
		from ffl_core.api.atf_link import create_party_from_atf
		rec = self._rec("70006")
		out = create_party_from_atf(rec, "Supplier")
		s = frappe.get_doc("Supplier", out["name"])
		self.assertEqual(s.is_ffl_dealer, 1)
		self.assertEqual(s.atf_ffl_record, rec)
		self.assertEqual(s.ffl_number, rec)
		again = create_party_from_atf(rec, "Supplier")
		self.assertEqual(again["name"], out["name"])


class SupplierSync(AtfLinkFixtures):
	def test_supplier_link_syncs_too(self):
		rec = self._rec("70007")
		name = "_Test ATF Link Supplier"
		if frappe.db.exists("Supplier", name):
			frappe.db.set_value("Supplier", name,
				{"atf_ffl_record": None, "ffl_number": None},
				update_modified=False)
			s = frappe.get_doc("Supplier", name)
		else:
			s = frappe.get_doc({"doctype": "Supplier", "supplier_name": name})
			s.flags.ignore_permissions = True
			s.insert()
		s.update({"is_ffl_dealer": 1, "atf_ffl_record": rec, "ffl_number": ""})
		s.flags.ignore_permissions = True
		s.save()
		self.assertEqual(s.ffl_number, rec)
		self.assertEqual(s.licensee_name, "Linked Dealer 70007 LLC")


class DispositionUsesLink(AtfLinkFixtures):
	def test_destination_prefers_linked_record(self):
		"""Even with a drifted ffl_number string (forced via direct DB write,
		bypassing the validate sync), the shipment books against the LINKED
		directory record."""
		from ffl_core.firearm import serialized_receipt_row
		rec = self._rec("70008")
		self._customer("_Test ATF Link F", atf_ffl_record=rec,
			ez_check_status="Verified",
			ffl_expiry_date=frappe.utils.add_days(frappe.utils.nowdate(), 365))
		frappe.db.set_value("Customer", "_Test ATF Link F",
			"ffl_number", "9-88-888-88-8X-88888", update_modified=False)
		if not frappe.db.exists("Item", "ATL-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "ATL-GUN", "item_name": "ATL-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		warehouse = frappe.db.get_value("Warehouse",
			{"company": self.company, "is_group": 0}, "name")
		if frappe.db.get_value("Serial No", "ATL-S1", "status") != "Active":
			se = frappe.get_doc({
				"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [serialized_receipt_row(item_code="ATL-GUN", qty=1,
					warehouse=warehouse, basic_rate=500, serial="ATL-S1")],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = "_Test ATF Link F"
		si.update_stock = 1
		si.append("items", {"item_code": "ATL-GUN", "qty": 1, "rate": 900,
			"warehouse": warehouse, "serial_no": "ATL-S1",
			"use_serial_batch_fields": 1})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		dest = frappe.db.get_value("FFL Disposition",
			{"sales_invoice": si.name, "docstatus": ("<", 2)},
			"destination_ffl")
		self.assertEqual(dest, rec)


if __name__ == "__main__":
	unittest.main()
