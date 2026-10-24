"""Peace-officer excise exemption — the excise tax comes off, sales tax stays.

California's firearm and ammunition excise tax (11%, R&TC §36011) does not apply
to a sale to an active or retired peace officer or the agency employing one
(§36021(a)); sales tax still does. FFL Settings names the excise account; a sale
flagged ffl_le_excise_exempt drops every taxes row on that account, and only
with the buyer and a CDTFA-230-FET certificate on the sale.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.compliance import le_excise_exempt as le

ITEM = "_TEST-LE-PISTOL"
TYPED_CERT = "/private/files/230-fet-test.pdf"  # a path no File carries
OFFICER = "Active Peace Officer"


def _certificate(customer: str) -> str:
	"""A CDTFA-230-FET file uploaded on this customer's record (as the page and the
	register do); its URL."""
	return frappe.get_doc({"doctype": "File", "file_name": "230-fet-test.txt", "content": f"CDTFA-230-FET for {customer}".encode(),
		"is_private": 1, "attached_to_doctype": "Customer", "attached_to_name": customer}
		).insert(ignore_permissions=True).file_url


def _company() -> str:
	return frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")


def _tax_account(company: str, name: str) -> str:
	abbr = frappe.get_cached_value("Company", company, "abbr")
	acct = f"{name} - {abbr}"
	if not frappe.db.exists("Account", acct):
		parent = (
			frappe.db.get_value("Account", {"company": company, "account_name": "Duties and Taxes"}, "name")
			or frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 1},
				"name", order_by="lft"))
		frappe.get_doc({"doctype": "Account", "account_name": name, "company": company,
			"parent_account": parent, "account_type": "Tax", "is_group": 0}).insert(ignore_permissions=True)
	return acct


class LeExciseFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = _company()
		cls.sales_tax = _tax_account(cls.company, "_Test LE Sales Tax")
		cls.excise = _tax_account(cls.company, "_Test LE Excise")
		title = "_Test LE Sales Tax + Excise"
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls.template = f"{title} - {abbr}"
		if not frappe.db.exists("Sales Taxes and Charges Template", cls.template):
			frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": title,
				"company": cls.company, "is_default": 0, "taxes": [
					{"charge_type": "On Net Total", "account_head": cls.sales_tax,
						"rate": 8.75, "description": "Sales Tax 8.75%"},
					{"charge_type": "On Net Total", "account_head": cls.excise,
						"rate": 11, "description": "Excise 11%"},
				]}).insert(ignore_permissions=True)
		# A group of its own: no Item Tax Template overrides the template's rates.
		group = "_Test LE Group"
		if not frappe.db.exists("Item Group", group):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": group,
				"parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM, "item_group": group,
				"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", ITEM, "item_group", group, update_modified=False)
		cls.customer = frappe.db.get_value("Customer", {"customer_name": "_Test LE Officer"}, "name")
		if not cls.customer:
			cls.customer = frappe.get_doc({"doctype": "Customer", "customer_name": "_Test LE Officer",
				"customer_type": "Individual"}).insert(ignore_permissions=True).name
		cls.cert = _certificate(cls.customer)

	def setUp(self):
		self._was = frappe.db.get_single_value("FFL Settings", le.SETTING)
		frappe.db.set_single_value("FFL Settings", le.SETTING, self.excise)
		# The test Company's address is in Texas; these tests are a California store.
		self._ca = patch.object(le, "in_exempt_state", return_value=True)
		self._ca.start()

	def tearDown(self):
		self._ca.stop()
		frappe.db.set_single_value("FFL Settings", le.SETTING, self._was)

	def _si(self, *, extra_taxes=(), **fields):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		si = frappe.get_doc({"doctype": "Sales Invoice", "company": self.company,
			"customer": self.customer, "taxes_and_charges": self.template,
			"items": [{"item_code": ITEM, "qty": 1, "rate": 500}], **fields})
		si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", self.template))
		for row in extra_taxes:
			si.append("taxes", row)
		si.insert(ignore_permissions=True)
		return si

	def _exempt(self, **over):
		return {le.FLAG: 1, le.BUYER: OFFICER, le.CERT: self.cert, **over}


class ExemptSale(LeExciseFixtures):
	def test_exempt_sale_drops_the_excise_and_keeps_sales_tax(self):
		si = self._si(**self._exempt())
		self.assertEqual([t.account_head for t in si.taxes], [self.sales_tax])
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 43.75, places=2)
		self.assertAlmostEqual(flt(si.grand_total), 543.75, places=2)

	def test_a_normal_sale_still_pays_both(self):
		si = self._si()
		self.assertAlmostEqual(flt(si.total_taxes_and_charges), 98.75, places=2)
		self.assertAlmostEqual(flt(si.grand_total), 598.75, places=2)

	def test_an_excise_freight_row_comes_off_too(self):
		"""Freight to an FFL carries the excise in California: whatever row is booked
		to the excise account goes, not only the template's."""
		si = self._si(extra_taxes=[{"charge_type": "Actual", "account_head": self.excise,
			"tax_amount": 4.95, "description": "Shipping excise"}], **self._exempt())
		self.assertEqual([t.account_head for t in si.taxes], [self.sales_tax])

	def test_turning_it_off_again_brings_the_excise_back(self):
		si = self._si(**self._exempt())
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		si.set(le.FLAG, 0)
		si.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", self.template))
		si.save(ignore_permissions=True)
		self.assertAlmostEqual(flt(si.grand_total), 598.75, places=2)


class ExemptionPaperwork(LeExciseFixtures):
	def test_no_certificate_no_exemption(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._si(**self._exempt(**{le.CERT: ""}))
		self.assertIn("230-FET", str(caught.exception))

	def test_a_typed_certificate_is_not_one(self):
		with self.assertRaises(frappe.ValidationError):
			self._si(**self._exempt(**{le.CERT: TYPED_CERT}))

	def test_another_buyers_certificate_is_not_this_ones(self):
		"""The buyer was changed after the upload: the certificate left on the sale is
		on someone else's record."""
		other = frappe.db.get_value("Customer", {"customer_name": "_Test LE Other Buyer"}) or frappe.get_doc(
			{"doctype": "Customer", "customer_name": "_Test LE Other Buyer", "customer_type": "Individual"}
			).insert(ignore_permissions=True).name
		with self.assertRaises(frappe.ValidationError):
			self._si(**self._exempt(**{le.CERT: _certificate(other)}))

	def test_who_the_buyer_is_is_required(self):
		with self.assertRaises(frappe.ValidationError):
			self._si(**self._exempt(**{le.BUYER: ""}))

	def test_a_store_without_the_setting_refuses_the_flag(self):
		"""No excise account named = nothing to exempt from (a Texas store): the flag
		is refused rather than saved as a promise the invoice does not keep."""
		frappe.db.set_single_value("FFL Settings", le.SETTING, None)
		with self.assertRaises(frappe.ValidationError):
			self._si(**self._exempt())


class CaliforniaOnly(LeExciseFixtures):
	"""The exemption is California law: a store elsewhere (the test Company is in
	Texas) is never offered it, whatever FFL Settings holds."""

	def setUp(self):
		super().setUp()
		self._ca.stop()  # the real home state: Texas

	def tearDown(self):
		self._ca.start()  # LeExciseFixtures.tearDown stops it
		super().tearDown()

	def test_a_texas_store_refuses_the_flag_and_offers_nothing(self):
		self.assertIsNone(le.exempt_account(self.company))
		with self.assertRaises(frappe.ValidationError) as caught:
			self._si(**self._exempt())
		self.assertIn("California", str(caught.exception))
		from ffl_core.api import manual_order

		self.assertFalse(manual_order.quote_config(self.company)["le_exempt_offered"])

	def test_settings_refuse_the_account_outside_california(self):
		settings = frappe.get_single("FFL Settings")
		settings.set(le.SETTING, self.excise)
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_only_a_california_address_qualifies(self):
		from ffl_core.compliance import interstate_tax

		for answer, expected in ((("CA", ""), True), (("TX", ""), False), ((None, "no address"), False)):
			with patch.object(interstate_tax, "_read_home_state", return_value=answer):
				self.assertEqual(le.in_exempt_state(self.company), expected, answer)


class HookUnit(unittest.TestCase):
	"""The hook on an in-memory document: returns, channel mirrors and POS Invoice."""

	def _doc(self, doctype="POS Invoice", **fields):
		doc = frappe.new_doc(doctype)
		doc.company = "Any Co"
		doc.update({le.FLAG: 1, le.BUYER: OFFICER, le.CERT: TYPED_CERT, **fields})
		doc.append("taxes", {"charge_type": "On Net Total", "account_head": "Sales Tax - X", "rate": 8.75})
		doc.append("taxes", {"charge_type": "On Net Total", "account_head": "Excise - X", "rate": 11})
		return doc

	def _run(self, doc, hook=le.apply_le_excise_exemption):
		with patch.object(le, "exempt_account", return_value="Excise - X"), \
				patch.object(le, "_certificate_on_file", return_value=True), \
				patch.object(type(doc), "calculate_taxes_and_totals") as calc:
			hook(doc)
		return calc

	def test_pos_invoice_drops_the_excise_before_erpnext_totals(self):
		doc = self._doc()
		calc = self._run(doc, le.apply_le_excise_exemption_before)
		self.assertEqual([t.account_head for t in doc.taxes], ["Sales Tax - X"])
		calc.assert_not_called()  # ERPNext's own validate totals it next

	def test_a_return_keeps_what_it_copied(self):
		doc = self._doc(is_return=1)
		self._run(doc)
		self.assertEqual(len(doc.taxes), 2)

	def test_a_channel_mirror_is_left_alone(self):
		doc = self._doc("Sales Invoice")
		with patch.object(le, "is_channel_mirrored_invoice", return_value=True):
			self._run(doc)
		self.assertEqual(len(doc.taxes), 2)

	def test_unflagged_is_a_no_op(self):
		doc = self._doc(**{le.FLAG: 0})
		calc = self._run(doc)
		self.assertEqual(len(doc.taxes), 2)
		calc.assert_not_called()


from ffl_core.tests.test_build_order import DEST_FFL  # noqa: E402
from ffl_core.tests.test_manual_order_tax_exempt import TaxExemptOrderFixtures  # noqa: E402


class CreateOrderExemption(TaxExemptOrderFixtures):
	"""Create Order carries the exemption onto its Sales Order and Sales Invoice,
	through the paid-in-full (is_pos) pass that re-derives taxes, and Edit keeps it.
	The default template's only row stands in for the excise row here."""

	def setUp(self):
		super().setUp()
		self._was = frappe.db.get_single_value("FFL Settings", le.SETTING)
		acct = frappe.db.get_value("Sales Taxes and Charges", {"parent": self.default_template}, "account_head")
		frappe.db.set_single_value("FFL Settings", le.SETTING, acct)
		self._ca = patch.object(le, "in_exempt_state", return_value=True)
		self._ca.start()
		self.buyer = self._indiv("LE Officer Buyer")
		self.cert = _certificate(self.buyer)

	def tearDown(self):
		self._ca.stop()
		frappe.db.set_single_value("FFL Settings", le.SETTING, self._was)
		super().tearDown()

	def _order(self, serial_tag, **le_fields):
		serial = self._stocked_serial(serial_tag)
		return manual_order.create_order({
			"buyer_type": "individual", "customer": self.buyer,
			"pos_profile": self._pos_profile(), "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 1000, "serials": [serial]}],
			**le_fields,
		})

	def test_the_exemption_rides_onto_the_order_and_survives_the_paid_pass(self):
		out = self._order("LE-CO-1", le_excise_exempt=1, le_exempt_buyer=OFFICER,
			le_exempt_certificate=self.cert)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertEqual(len(si.taxes), 0)
		self.assertAlmostEqual(flt(si.grand_total), 1000, places=2)
		so = frappe.get_doc("Sales Order", out["sales_order"])
		for doc in (so, si):
			self.assertEqual((doc.get(le.FLAG), doc.get(le.BUYER), doc.get(le.CERT)), (1, OFFICER, self.cert))
		payload = manual_order.get_order_for_edit(si.name)["payload"]
		self.assertEqual((payload["le_excise_exempt"], payload["le_exempt_certificate"]), (1, self.cert))

	def test_no_certificate_no_order(self):
		with self.assertRaises(frappe.ValidationError):
			self._order("LE-CO-2", le_excise_exempt=1, le_exempt_buyer=OFFICER)


from ffl_core.api import manual_order  # noqa: E402
