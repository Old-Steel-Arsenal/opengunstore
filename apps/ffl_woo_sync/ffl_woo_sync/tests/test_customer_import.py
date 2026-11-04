"""Tests for woocommerce/customer_import.py — online buyers and store accounts become Customers,
once per email address, never touching a Customer the POS already has. The store is faked (the
external API); the Customer lookup and creation run for real."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.customer_profile import new_retail_customer
from ffl_woo_sync.woocommerce import customer_import

SAVEPOINT = "test_customer_import"


def _order(email, first="Pat", last="Buyer", phone="", status="completed"):
	return {"id": abs(hash(email + status)) % 10**6, "status": status,
		"billing": {"email": email, "first_name": first, "last_name": last, "phone": phone}}


def _account(email, first="", last="", billing=None):
	return {"id": abs(hash(email)) % 10**6, "email": email, "first_name": first, "last_name": last,
		"billing": billing or {}}


def _store(orders=(), accounts=()):
	wc = MagicMock()
	wc.list_orders.side_effect = lambda **kw: list(orders) if kw.get("page") == 1 else []
	wc.get.side_effect = lambda path, params=None: list(accounts) if params.get("page") == 1 else []
	return wc


class TestCustomerImport(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()

	def _run(self, store, dry_run=0):
		with patch.object(customer_import.WooCommerceClient, "from_settings", return_value=store):
			return customer_import.import_customers(dry_run=dry_run)

	@staticmethod
	def _customer(email):
		return frappe.db.get_value("Customer", {"email_id": email}, ["customer_name", "mobile_no"], as_dict=True)

	def test_buyers_and_accounts_become_customers_once(self):
		store = _store(
			orders=[_order("old.buyer@shop.test", "Old", "Buyer", "5550101"),
				_order("OLD.BUYER@shop.test", "Older", "Name")],
			accounts=[_account("old.buyer@shop.test", "Account", "Name"),
				_account("browser@shop.test", "Window", "Shopper")])
		out = self._run(store)
		self.assertEqual((out["emails"], out["created"], out["existing"], out["errors"]), (2, 2, 0, []))
		# The newest order's billing name and phone win over the account's.
		buyer = self._customer("old.buyer@shop.test")
		self.assertEqual((buyer.customer_name, buyer.mobile_no), ("Old Buyer", "5550101"))
		self.assertEqual(self._customer("browser@shop.test").customer_name, "Window Shopper")
		# A second run finds them all.
		again = self._run(store)
		self.assertEqual((again["created"], again["existing"]), (0, 2))

	def test_an_existing_customer_is_left_as_it_is(self):
		doc = new_retail_customer("Counter Regular", email="Regular@Shop.test")
		doc.flags.ignore_permissions = True
		doc.insert()
		out = self._run(_store(orders=[_order("regular@shop.test", "Web", "Name")]))
		self.assertEqual((out["created"], out["existing"]), (0, 1))
		self.assertEqual(frappe.db.get_value("Customer", doc.name, "customer_name"), "Counter Regular")
		self.assertEqual(frappe.db.count("Customer", {"email_id": "regular@shop.test"}), 1)

	def test_abandoned_and_unpaid_orders_are_not_buyers(self):
		store = _store(orders=[_order(f"{s}@shop.test", status=s)
			for s in ("checkout-draft", "pending", "failed", "cancelled", "trash")])
		out = self._run(store)
		self.assertEqual((out["emails"], out["created"]), (0, 0))

	def test_dry_run_creates_nothing(self):
		out = self._run(_store(accounts=[_account("dry@shop.test", "Dry", "Run")]), dry_run=1)
		self.assertEqual((out["dry_run"], out["created"]), (True, 1))
		self.assertEqual(out["sample"], [{"email": "dry@shop.test", "name": "Dry Run"}])
		self.assertIsNone(self._customer("dry@shop.test"))

	def test_no_name_uses_the_address_and_junk_is_skipped(self):
		out = self._run(_store(accounts=[_account("noname@shop.test"), _account("not-an-email")]))
		self.assertEqual((out["created"], out["invalid"]), (1, 1))
		self.assertEqual(self._customer("noname@shop.test").customer_name, "noname@shop.test")

	def test_only_a_system_manager_may_run_it(self):
		user = "_t-import-clerk@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "Clerk", "send_welcome_email": 0,
				"roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			self._run(_store())
