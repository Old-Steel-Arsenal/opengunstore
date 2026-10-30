"""POS Trade-in API tests.

Verifies create_trade_in_intake forwards to firearm_intake and supports filing
an individual trade-in under a shared model SKU while preserving the legal
individual-ID acquisition.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import trade_in


class TradeIn(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)
		if not frappe.db.exists("Customer", "_Test Trade-in Seller"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Trade-in Seller",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are not about FastBound.
		# A site where another module saved credentials (they persist) would otherwise reach the live client.
		from unittest.mock import patch

		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in trade-in tests"))
		p.start()
		self.addCleanup(p.stop)

	def _payload(self, suffix: str, **over) -> dict:
		base = {
			"serial_no": f"TI-{suffix}",
			"manufacturer": "Glock",
			"model_name": "19",
			"caliber": "9mm",
			"firearm_type": "Pistol",
			"condition_grade": "Good",
			"id_type": "Driver License",
			"id_number": f"DL-{suffix}",
			"source_name": "_Test Trade-in Seller",
			"source_first_name": "Testy",
			"source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 300,
			"retail_price": 500,
			"payout_method": "Cash",  # a pure buy says how the seller was paid (ignored with apply_credit)
		}
		base.update(over)
		return base

	def test_trade_in_files_under_shared_sku(self):
		suffix = self._testMethodName
		sku = f"GLOCK19-USED-{suffix}"
		res = trade_in.create_trade_in_intake(self._payload(suffix, item_code=sku))
		self.assertTrue(res["ok"])
		self.assertEqual(res["item_code"], sku)
		# Gun filed under the shared SKU, with its own per-serial data.
		self.assertEqual(frappe.db.get_value("Serial No", f"TI-{suffix}", "item_code"), sku)
		self.assertEqual(frappe.db.get_value("Serial No", f"TI-{suffix}", "condition_grade"), "Good")
		# Compliance: the individual ID still lands on the acquisition.
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.source_type, "Trade-in")
		self.assertEqual(acq.source_id_number, f"DL-{suffix}")

	def test_trade_in_requires_seller_address_and_structured_name(self):
		"""ATF 478.125(e): an acquisition from a non-licensee must record the
		seller's name AND address, and the gun's type + caliber are bound-book
		columns — the POS dialog can no longer submit without them (the old
		path pushed 'Unknown - please edit in FastBound' address placeholders
		and 'Unknown'/'Other Firearm' item placeholders into the bound book)."""
		suffix = self._testMethodName
		for missing in ("source_address", "source_first_name", "source_last_name",
				"caliber", "firearm_type"):
			payload = self._payload(f"{suffix}-{missing}")
			payload[missing] = ""
			with self.assertRaises(frappe.ValidationError, msg=f"{missing} should be required"):
				trade_in.create_trade_in_intake(payload)

	def test_trade_in_seller_identity_lands_on_acquisition(self):
		"""source_customer link + structured name (incl. suffix) + address must
		reach the FFL Acquisition so the FastBound contact carries the real
		seller, not a name-split guess with a placeholder address."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(
			suffix,
			source_middle_name="Q",
			source_suffix="IV",
		))
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.source_customer, "_Test Trade-in Seller")
		self.assertEqual(acq.source_first_name, "Testy")
		self.assertEqual(acq.source_middle_name, "Q")
		self.assertEqual(acq.source_last_name, "Seller")
		self.assertEqual(acq.source_suffix, "IV")
		self.assertEqual(acq.source_address, "123 Main St, Springfield TX 75024")

	def test_trade_in_accepts_split_address_parts(self):
		"""The POS dialog now collects street / city / state / ZIP separately;
		the API composes the canonical single line ("street, City ST zip") the
		FastBound push parses deterministically. State is upper-cased."""
		suffix = self._testMethodName
		payload = self._payload(suffix)
		payload.pop("source_address")
		payload.update({
			"source_address_line1": "77 Split St",
			"source_city": "Springfield",
			"source_state": "tx",
			"source_zip": "75024",
		})
		res = trade_in.create_trade_in_intake(payload)
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.source_address, "77 Split St, Springfield TX 75024")

	def test_split_address_requires_every_part_and_sane_values(self):
		suffix = self._testMethodName
		base_parts = {
			"source_address_line1": "77 Split St", "source_city": "Springfield",
			"source_state": "TX", "source_zip": "75024",
		}
		cases = (
			("source_address_line1", ""), ("source_city", ""),
			("source_state", ""), ("source_zip", ""),
			("source_state", "Texas"),  # must be the 2-letter code
			("source_state", "ZZ"),     # of a real state (interstate_tax.US_STATES)
			("source_zip", "75O24"),    # must be a real 5/9-digit ZIP
		)
		for i, (field, bad) in enumerate(cases):
			payload = self._payload(f"{suffix}-{i}")
			payload.pop("source_address")
			payload.update(base_parts)
			payload[field] = bad
			with self.assertRaises(frappe.ValidationError,
					msg=f"{field}={bad!r} should be rejected"):
				trade_in.create_trade_in_intake(payload)

	def test_seller_defaults_returns_structured_address(self):
		"""The dialog prefill maps the Customer's primary Address straight into
		the split fields — no string parsing anywhere."""
		cust = "_Test Prefill Seller"
		if not frappe.db.exists("Customer", cust):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cust,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		addr = frappe.get_doc({
			"doctype": "Address", "address_title": cust, "address_type": "Billing",
			"address_line1": "9 Prefill Ave", "city": "Springfield", "state": "TX",
			"pincode": "75093", "country": "United States",
			"links": [{"link_doctype": "Customer", "link_name": cust}],
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust, "customer_primary_address", addr.name)

		d = trade_in.seller_defaults(cust)
		self.assertEqual(d["source_address_line1"], "9 Prefill Ave")
		self.assertEqual(d["source_city"], "Springfield")
		self.assertEqual(d["source_state"], "TX")
		self.assertEqual(d["source_zip"], "75093")
		self.assertEqual(d["source_address"], "9 Prefill Ave, Springfield TX 75093")

	def test_seller_defaults_prefers_customer_structured_identity(self):
		"""The Customer form now carries editable First/Middle/Last/Suffix name
		parts (setup/customer_identity) — when set, they beat the display-name
		split in the dialog prefill."""
		cust = "_Test Structured Seller"
		if not frappe.db.exists("Customer", cust):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cust,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust, {
			"ffl_first_name": "Marcus", "middle_name": "Quincy",
			"ffl_last_name": "Holloway", "name_suffix": "Jr",
		}, update_modified=False)

		d = trade_in.seller_defaults(cust)
		# The editable name parts win over the "_Test Structured Seller" split.
		self.assertEqual(d["source_first_name"], "Marcus")
		self.assertEqual(d["source_middle_name"], "Quincy")
		self.assertEqual(d["source_last_name"], "Holloway")
		self.assertEqual(d["source_suffix"], "Jr")

	def test_seller_defaults_falls_back_to_name_split(self):
		"""With no structured parts, the display-name split still drives the
		prefill (unchanged behaviour for legacy customers)."""
		cust = "_Test Split Fallback IV Seller"
		if not frappe.db.exists("Customer", cust):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cust,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		d = trade_in.seller_defaults(cust)
		self.assertEqual(d["source_first_name"], "_Test")
		self.assertEqual(d["source_last_name"], "Seller")
		self.assertEqual(d["source_suffix"], "IV")  # mid-name suffix pulled out

	def test_trade_in_importer_stamped_on_serial(self):
		"""Form parity with Receive Goods: an imported gun's importer marking is
		a per-unit bound-book fact — it must land on the Serial No (the FastBound
		push prefers the per-serial value)."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(
			suffix, manufacturer="Norinco", model_name="1911A1",
			importer="Interarms",
		))
		self.assertTrue(res["ok"])
		self.assertEqual(
			frappe.db.get_value("Serial No", f"TI-{suffix}", "importer"),
			"Interarms",
		)

	def test_trade_in_without_sku_auto_mints(self):
		"""Back-compat: no item_code → legacy one-Item-per-gun (UF-) code."""
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix))
		self.assertTrue(res["ok"])
		self.assertTrue(res["item_code"].startswith("UF-"))

	def test_trade_in_stamps_purchase_rate_on_serial(self):
		# The acquisition cost MUST land on Serial No.purchase_rate — the Sales
		# Report reads that field for firearm COGS. ERPNext does NOT auto-fill it
		# from the Material Receipt's basic_rate (the same gap compliance/
		# purchase.py works around for Purchase Receipts), so intake must stamp
		# it explicitly. Without it the traded-in gun resells at 0 cost / 100%
		# margin and its acquisition cost is expensed nowhere.
		suffix = self._testMethodName
		res = trade_in.create_trade_in_intake(self._payload(suffix, acquisition_cost=742))
		self.assertTrue(res["ok"])
		self.assertEqual(
			float(frappe.db.get_value("Serial No", f"TI-{suffix}", "purchase_rate") or 0),
			742.0,
		)


if __name__ == "__main__":
	unittest.main()
