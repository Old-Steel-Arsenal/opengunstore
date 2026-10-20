"""Check/Money Order — a paper tender taken at the counter and deposited later.

It is a bank tender like Zelle and ACH, never drawer cash: the mode posts to the
same bank account they use, so the cash drawer's expected count never moves.
Its check or money order number is required wherever money is recorded with it
(Record Payment, Create Order, refunds, the POS tile), because that number is
the only thing tying the deposit back to the sale.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.compliance.payment_mode import require_check_money_order_number
from ffl_core.setup.payments import CHECK_MONEY_ORDER, ensure_manual_modes_of_payment
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


class CheckMoneyOrderMode(BuildOrderMixin, FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.cash_drawer import ensure_accounts
		# A real bank account for the company, as every store has (store_setup
		# makes it); the bare test site otherwise has none.
		cls.bank = ensure_accounts(cls.company)["bank"]
		# ensure_manual_modes_of_payment honours an existing asset row, so a row another module's fixture
		# pointed at Cash (test_pos_close fills every unmapped mode with Cash) would stick. Start clean.
		frappe.db.delete("Mode of Payment Account", {"parent": CHECK_MONEY_ORDER, "company": cls.company})
		ensure_manual_modes_of_payment()

	def _account(self, mode):
		return frappe.db.get_value("Mode of Payment Account",
			{"parent": mode, "company": self.company}, "default_account")

	def test_mode_is_a_bank_tender_on_the_bank_account(self):
		from ffl_core.cash_drawer import cash_account
		mop = frappe.db.get_value("Mode of Payment", CHECK_MONEY_ORDER,
			["type", "enabled"], as_dict=True)
		self.assertEqual(mop.type, "Bank")
		self.assertTrue(mop.enabled)
		self.assertEqual(self._account(CHECK_MONEY_ORDER), self.bank)
		self.assertNotEqual(self._account(CHECK_MONEY_ORDER), cash_account(self.company))

	def test_never_falls_back_to_the_drawer_cash_account(self):
		"""Zelle/ACH fall back to Cash on a site with no bank; a check must not —
		it would count toward the drawer it never sits in."""
		frappe.db.delete("Mode of Payment Account",
			{"parent": CHECK_MONEY_ORDER, "company": self.company})
		# ensure_manual_modes_of_payment commits, so put the row back for the rest.
		self.addCleanup(ensure_manual_modes_of_payment)
		with patch("ffl_core.cash_drawer.resolve_bank", return_value=None):
			ensure_manual_modes_of_payment()
		self.assertIsNone(self._account(CHECK_MONEY_ORDER))

	def test_on_every_enabled_pos_profile(self):
		for name in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
			modes = {r.mode_of_payment for r in frappe.get_doc("POS Profile", name).payments}
			self.assertIn(CHECK_MONEY_ORDER, modes, name)

	def _profiles(self):
		names = frappe.get_all("POS Profile",
			filters={"disabled": 0, "company": self.company}, pluck="name")
		self.assertTrue(names)
		return names

	def _tiles(self, profile):
		return {r.mode_of_payment for r in frappe.get_doc("POS Profile", profile).payments}

	def _drop_tiles(self, *modes):
		# ensure_manual_modes_of_payment commits, so put the rows and tiles back after.
		self.addCleanup(ensure_manual_modes_of_payment)
		for name in self._profiles():
			profile = frappe.get_doc("POS Profile", name)
			profile.set("payments", [r for r in profile.payments
				if r.mode_of_payment not in modes])
			profile.flags.ignore_permissions = True
			profile.save()

	def test_unmapped_mode_gets_no_tile(self):
		"""A fresh site migrates before store_setup makes the bank: the mode is
		left unmapped, and a tile for it would fail at submit. ERPNext refuses to
		save a profile carrying it, which took the mapped modes' tiles down with
		it — so Zelle is dropped too and must come back."""
		self._drop_tiles(CHECK_MONEY_ORDER, "Zelle")
		frappe.db.delete("Mode of Payment Account",
			{"parent": CHECK_MONEY_ORDER, "company": self.company})
		with patch("ffl_core.cash_drawer.resolve_bank", return_value=None):
			ensure_manual_modes_of_payment()
		for name in self._profiles():
			tiles = self._tiles(name)
			self.assertNotIn(CHECK_MONEY_ORDER, tiles, name)
			self.assertIn("Zelle", tiles, name)  # the mapped modes are still wired

	def test_mapped_mode_gets_its_tile(self):
		"""The migrate that maps the account adds the tile."""
		self._drop_tiles(CHECK_MONEY_ORDER)
		ensure_manual_modes_of_payment()
		self.assertEqual(self._account(CHECK_MONEY_ORDER), self.bank)
		for name in self._profiles():
			self.assertIn(CHECK_MONEY_ORDER, self._tiles(name), name)

	# ------------------------------------------------------------ Record Payment

	def test_record_payment_requires_the_number(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.record_payment("SINV-DOES-NOT-EXIST", CHECK_MONEY_ORDER)
		self.assertIn("check or money order number", str(ctx.exception))

	def test_record_payment_books_to_the_bank_not_the_drawer(self):
		from ffl_core.cash_drawer import cash_account
		si = self._ship(self._stocked_serial("CMO-RP-1"), update_stock=0)
		out = manual_order.record_payment(si.name, CHECK_MONEY_ORDER,
			transaction_number="CHK 1042")
		self.assertTrue(out["paid"])
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(pe.mode_of_payment, CHECK_MONEY_ORDER)
		self.assertEqual(pe.reference_no, "CHK 1042")
		self.assertEqual(pe.paid_to, self.bank)
		accounts = set(frappe.get_all("GL Entry", filters={"voucher_type": "Payment Entry",
			"voucher_no": pe.name, "is_cancelled": 0}, pluck="account"))
		self.assertIn(self.bank, accounts)
		self.assertNotIn(cash_account(self.company), accounts)

	# -------------------------------------------------------------- Create Order

	def _order(self, serial, **payment):
		return manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv(f"CMO Buyer {serial}"),
			"destination_ffl": DEST_FFL, "dispose_now": 0, "tax_exempt": 1,
			"payment_received": 1, "mode_of_payment": CHECK_MONEY_ORDER, **payment,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [self._stocked_serial(serial)]}],
		})

	def test_create_order_requires_the_number(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._order("CMO-CO-1")
		self.assertIn("check or money order number", str(ctx.exception))

	def test_create_order_records_the_number_on_the_tender(self):
		out = self._order("CMO-CO-2", transaction_number="MO 55120934")
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.payments[0].mode_of_payment, CHECK_MONEY_ORDER)
		self.assertEqual(si.payments[0].reference_no, "MO 55120934")

	def test_create_order_deposit_books_a_payment_entry(self):
		out = self._order("CMO-CO-3", paid_amount=100, transaction_number="CHK 77")
		pe = frappe.get_all("Payment Entry Reference", filters={
			"reference_name": out["sales_invoice"], "docstatus": 1}, pluck="parent")
		self.assertEqual(len(pe), 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe[0], "mode_of_payment"),
			CHECK_MONEY_ORDER)

	# ------------------------------------------------------------------- refunds

	def test_cancel_refund_requires_the_number(self):
		si = self._ship(self._stocked_serial("CMO-CX-1"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=100)
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.cancel_order(si.name, "test", refund_mode=CHECK_MONEY_ORDER)
		self.assertIn("check or money order number", str(ctx.exception))

	def test_cancel_refund_by_check_leaves_the_bank(self):
		si = self._ship(self._stocked_serial("CMO-CX-2"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=100)
		with patch.object(manual_order, "_try_void_shipment", return_value=(True, "")):
			out = manual_order.cancel_order(si.name, "test",
				refund_mode=CHECK_MONEY_ORDER, refund_reference="CHK 2001")
		pe = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(pe.payment_type, "Pay")
		self.assertEqual(pe.mode_of_payment, CHECK_MONEY_ORDER)
		self.assertEqual(pe.paid_from, self.bank)
		self.assertAlmostEqual(pe.paid_amount, 100, places=2)


class CheckMoneyOrderInvoiceGate(FrappeTestCase):
	"""POS Invoice / Sales Invoice validate: the tile's row needs its number."""

	def _doc(self, amount, reference=""):
		return frappe._dict({"docstatus": 1, "is_consolidated": 0, "payments": [
			frappe._dict({"mode_of_payment": CHECK_MONEY_ORDER, "amount": amount,
				"reference_no": reference})]})

	def test_positive_row_without_number_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_check_money_order_number(self._doc(250))
		self.assertIn("check or money order number", str(ctx.exception))

	def test_row_with_number_passes(self):
		require_check_money_order_number(self._doc(250, "CHK 3310"))

	def test_return_row_is_not_gated(self):
		require_check_money_order_number(self._doc(-250))

	def test_wired_on_both_invoice_types(self):
		hooks = frappe.get_hooks("doc_events")
		path = "ffl_core.compliance.payment_mode.require_check_money_order_number"
		for doctype in ("POS Invoice", "Sales Invoice"):
			validate = hooks.get(doctype, {}).get("validate") or []
			self.assertIn(path, validate, doctype)
