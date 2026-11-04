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


def _account(email, first="", last="", billing=None, role="customer"):
	return {"id": abs(hash(email)) % 10**6, "email": email, "first_name": first, "last_name": last,
		"billing": billing or {}, "role": role}


def _store(orders=(), accounts=()):
	wc = MagicMock()
	rows = {"orders": list(orders), "customers": list(accounts)}
	wc.get.side_effect = lambda path, params=None: rows[path] if params.get("page") == 1 else []
	return wc


def _retail(name, email):
	doc = new_retail_customer(name, email=email)
	doc.flags.ignore_permissions = True
	return doc.insert()


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

	def test_every_account_role_but_staff(self):
		store = _store(accounts=[_account("sub@shop.test", "Sub", "Scriber", role="subscriber"),
			_account("boss@shop.test", "Shop", "Boss", role="administrator"),
			_account("mgr@shop.test", "Shop", "Manager", role="shop_manager")])
		out = self._run(store)
		self.assertEqual(out["created"], 1)
		self.assertTrue(self._customer("sub@shop.test"))
		self.assertIsNone(self._customer("boss@shop.test"))
		# The customers list is asked for every role.
		customers_call = [c for c in store.get.call_args_list if c.args[0] == "customers"][0]
		self.assertEqual(customers_call.kwargs["params"]["role"], "all")

	def test_an_address_on_a_customers_contact_is_known(self):
		customer = _retail("Contact Only", None)
		frappe.get_doc({"doctype": "Contact", "first_name": "Contact", "email_ids": [{"email_id": "contact.only@shop.test",
			"is_primary": 1}], "links": [{"link_doctype": "Customer", "link_name": customer.name}]}).insert(
			ignore_permissions=True)
		out = self._run(_store(accounts=[_account("contact.only@shop.test", "Web", "Account")]))
		self.assertEqual((out["created"], out["existing"]), (0, 1))

	def test_a_dealers_alias_does_not_keep_out_a_retail_buyer(self):
		dealer = _retail("_T Import Dealer", "dealer@shop.test")
		frappe.db.set_value("Customer", dealer.name, "is_ffl_dealer", 1)
		dealer.reload()
		dealer.append("customer_aliases", {"alias_type": "Email", "value": "merged.buyer@shop.test"})
		dealer.flags.ignore_permissions = True
		dealer.save()
		# The address really is the dealer's alias (what the order sync would see).
		from ffl_core.customer_profile import find_customer_by_email

		hit = find_customer_by_email("merged.buyer@shop.test")
		self.assertEqual((hit.name, hit.get("via_alias")), (dealer.name, 1))
		out = self._run(_store(orders=[_order("merged.buyer@shop.test", "Merged", "Buyer")]))
		self.assertEqual(out["created"], 1)
		self.assertEqual(self._customer("merged.buyer@shop.test").customer_name, "Merged Buyer")

	def test_a_phone_the_pos_rejects_is_dropped_not_the_person(self):
		out = self._run(_store(orders=[_order("badphone@shop.test", "Bad", "Phone", phone="call me maybe!")]))
		self.assertEqual((out["created"], out["phone_dropped"], out["errors"]), (1, 1, []))
		self.assertTrue(self._customer("badphone@shop.test"))

	def test_one_failing_row_leaves_the_others(self):
		real = customer_import.new_retail_customer

		def flaky(name, **kw):
			if kw.get("email") == "broken@shop.test":
				raise frappe.ValidationError("broken row")
			return real(name, **kw)

		with patch.object(customer_import, "new_retail_customer", side_effect=flaky):
			out = self._run(_store(accounts=[_account("broken@shop.test", "Bro", "Ken"),
				_account("fine@shop.test", "Fi", "Ne")]))
		self.assertEqual(out["created"], 1)
		self.assertEqual([e["email"] for e in out["errors"]], ["broken@shop.test"])
		self.assertTrue(self._customer("fine@shop.test"))
		self.assertIsNone(self._customer("broken@shop.test"))

	def test_only_an_explicit_no_runs_for_real(self):
		for value in ("true", "1", "yes", None, 1, True):
			out = self._run(_store(accounts=[_account("maybe@shop.test", "May", "Be")]), dry_run=value)
			self.assertTrue(out["dry_run"], value)
		self.assertIsNone(self._customer("maybe@shop.test"))

	def test_a_non_list_answer_stops_the_import(self):
		# From either list: a broken orders endpoint must not pass for a store with no buyers.
		for broken in ("orders", "customers"):
			store = _store(accounts=[_account("kept.out@shop.test", "Kept", "Out")])
			ok = store.get.side_effect
			store.get.side_effect = lambda path, params=None, b=broken, ok=ok: (
				{"code": "rest_forbidden"} if path == b else ok(path, params))
			with self.assertRaises(frappe.ValidationError, msg=broken):
				self._run(store)
		self.assertIsNone(self._customer("kept.out@shop.test"))

