"""A counter login holding only Sales User (+ Stock User), or Sales Manager + Sales User, can run the register end to end.

ERPNext v16 gives Sales User read on POS Invoice and nothing on POS Opening Entry, POS Closing Entry or Sales
Invoice, and Sales Manager no write on POS Invoice: every real login used to carry Accounts User as well, so
nobody noticed. pos_compat.REGISTER_GRANTS adds the minimum. These tests sign in as such users, with no
Administrator in the session and no ignore_permissions, and walk the register's own calls: open the shift, read
what the page reads, ring a cash sale, print the receipt, return a sale, close the shift (the consolidation job
runs as the closing user) -- then prove the books stay closed to them.

Not about the grant mechanics (POS Profile / bundle rows have their own tests); the rows made are checked in
CounterRegisterGrantRows. Nothing commits: ERPNext's merge of a closing is neutralised by ShiftFixtures.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, nowdate, nowtime

from ffl_core.setup import pos_compat
from ffl_core.tests.test_cash_drawer import _user
from ffl_core.tests.test_cash_drawer_pos import ShiftFixtures
from ffl_core.tests.test_pos_profile_cashier_perm import _drop_global_defaults, _isolate

ITEM = "CRP-AMMO"
DOCTYPES = tuple(sorted({d for d, *_ in pos_compat.CASHIER_GRANTS + pos_compat.ROLE_GRANTS + pos_compat.REGISTER_GRANTS}))


class CounterRegisterLoop(ShiftFixtures):
	KIND = "store_a"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The "Counter Sales" role profile is Sales User + Stock User; a manager is Sales Manager + Sales User. Neither holds an Accounts role.
		cls.users = {
			"Sales User + Stock User": _user("crp-clerk@example.com", "Sales User", "Stock User"),
			"Sales Manager + Sales User": _user("crp-manager@example.com", "Sales Manager", "Sales User"),
		}
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM, "item_group": "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 5}).insert(ignore_permissions=True)
		cls.stock_up()

	@classmethod
	def stock_up(cls):
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": cls.company,
			"items": [{"item_code": ITEM, "qty": 50, "basic_rate": 5, "t_warehouse": frappe.db.get_value(
				"Warehouse", {"company": cls.company, "warehouse_name": "Stores"})}]})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

	def setUp(self):
		super().setUp()
		# From the stock rules, as on a site that never ran the grants: they are made inside the test's savepoint.
		for doctype in DOCTYPES:
			frappe.db.delete("Custom DocPerm", {"parent": doctype})  # back to the stock rules, until the rollback
			self.addCleanup(frappe.clear_cache, doctype=doctype)
			frappe.clear_cache(doctype=doctype)
		for key in (pos_compat._GRANTED_KEY, pos_compat._ROLE_GRANTS_KEY, pos_compat._REGISTER_GRANTS_KEY):
			frappe.db.set_global(key, "")
		self.addCleanup(_drop_global_defaults)
		pos_compat.ensure_cashier_pos_profile_select()
		pos_compat.ensure_role_grants()
		# One shift per profile: shifts left Open by an earlier run or module are Closed (status only, rolled back).
		frappe.db.sql("UPDATE `tabPOS Opening Entry` SET status = 'Closed' WHERE status = 'Open'")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	# --- the register's own calls ---------------------------------------------------------------

	def _open(self, user: str) -> str:
		"""What the Create POS Opening Entry dialog does, signed in as ``user``."""
		from erpnext.selling.page.point_of_sale.point_of_sale import create_opening_voucher

		from ffl_core.api.pos_session import check_opening_entry

		frappe.set_user(user)
		self.assertEqual(check_opening_entry(user), [])
		with self.subTest(read="the dialog reads the profile's payment methods"):
			self.assertEqual(frappe.client.get("POS Profile", self.profile)["name"], self.profile)
		opening = create_opening_voucher(self.profile, self.company,
			[{"mode_of_payment": "Cash", "opening_amount": 0}])
		return opening["name"]

	def _page_loads(self):
		"""The reads POS page makes while it loads (pos_controller.js / pos_item_selector.js)."""
		from erpnext.selling.page.point_of_sale.point_of_sale import get_items, get_parent_item_group, get_pos_profile_data

		with self.subTest(read="Stock Settings"):
			frappe.client.get_value("Stock Settings", fieldname="allow_negative_stock")
		with self.subTest(read="POS Settings type"):
			self.assertEqual(frappe.client.get_single_value("POS Settings", "invoice_type"), "POS Invoice")
		with self.subTest(read="POS Settings"):
			frappe.client.get("POS Settings")
		get_pos_profile_data(self.profile)
		get_parent_item_group(self.profile)
		# the item grid waits for the profile's price list
		with self.subTest(read="POS Profile price list"):
			price_list = frappe.client.get_value("POS Profile", fieldname="selling_price_list", filters=self.profile)
			self.assertEqual(price_list["selling_price_list"], "Standard Selling")
		items = get_items(0, 40, "Standard Selling", get_parent_item_group(self.profile), self.profile, ITEM)
		self.assertIn(ITEM, [i["item_code"] for i in items["items"]])

	def _sell(self, user: str, qty: int = 2, rate: int = 10) -> str:
		"""Checkout: a draft saved, then submitted with cash (frm.savesubmit), as ``user`` -- no ignore_permissions."""
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import get_stock_availability

		frappe.set_user(user)
		warehouse = frappe.db.get_value("POS Profile", self.profile, "warehouse")
		get_stock_availability(ITEM, warehouse)
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = self.profile
		inv.customer = "_Test CD Customer"
		inv.append("items", {"item_code": ITEM, "qty": qty, "rate": rate, "price_list_rate": rate})
		inv.set_missing_values()
		inv.items[0].rate = inv.items[0].price_list_rate = rate
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": "Cash", "amount": qty * rate})
		inv.insert()
		inv.submit()
		return inv.name

	def _recent_orders(self):
		from erpnext.selling.page.point_of_sale.point_of_sale import get_past_order_list

		return get_past_order_list("", "Paid")

	def _print(self, name: str):
		from frappe.www.printview import get_html_and_style

		self.assertIn(name, get_html_and_style(doc="POS Invoice", name=name)["html"])

	def _return(self, user: str, original: str) -> str:
		"""The Recent Orders > Return button, then Checkout."""
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return
		from erpnext.controllers.sales_and_purchase_return import is_invoice_returnable

		frappe.set_user(user)
		self.assertTrue(is_invoice_returnable("POS Invoice", original))
		ret = frappe.get_doc(make_sales_return(original))
		ret.pos_profile = self.profile
		ret.set("payments", [])
		ret.append("payments", {"mode_of_payment": "Cash", "amount": ret.grand_total})
		ret.insert()
		ret.submit()
		return ret.name

	def _close(self, user: str, opening: str) -> str:
		"""Close the POS: the closing form built from the shift, saved and submitted (its job consolidates)."""
		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

		frappe.set_user(user)
		closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", opening))
		closing.user = user
		closing.posting_date, closing.posting_time = nowdate(), nowtime()
		for row in closing.payment_reconciliation:
			row.closing_amount = row.expected_amount
			row.difference = 0
		closing.insert()
		closing.submit()
		return closing.name

	# --- the loop ---------------------------------------------------------------------------------

	def test_the_register_runs_end_to_end(self):
		for label, user in self.users.items():
			with self.subTest(role=label):
				savepoint = f"crp_{user.split('@')[0].replace('-', '_')}"
				frappe.db.savepoint(savepoint)
				self.addCleanup(frappe.set_user, "Administrator")
				try:
					self._run_loop(user)
				finally:
					frappe.set_user("Administrator")
					frappe.db.rollback(save_point=savepoint)
					frappe.db.sql("UPDATE `tabPOS Opening Entry` SET status = 'Closed' WHERE status = 'Open'")

	def _run_loop(self, user: str):
		opening = self._open(user)
		self._page_loads()
		sale, kept = self._sell(user), self._sell(user, qty=1)
		self.assertEqual(frappe.db.get_value("POS Invoice", sale, ["docstatus", "owner", "status"], as_dict=True),
			{"docstatus": 1, "owner": user, "status": "Paid"})
		self._print(sale)
		self.assertIn(sale, [o["name"] for o in self._recent_orders()])
		ret = self._return(user, sale)
		self.assertEqual(frappe.db.get_value("POS Invoice", ret, ["docstatus", "is_return"], as_dict=True),
			{"docstatus": 1, "is_return": 1})

		# Close the POS opens the closing form, and the shift's entry is read on the way
		frappe.set_user(user)
		self.assertTrue(frappe.has_permission("POS Opening Entry", "read", doc=opening))
		self.assertTrue(frappe.has_permission("POS Closing Entry", "read"))
		frappe.set_user("Administrator")
		closing = self._close(user, opening)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("POS Closing Entry", closing, ["docstatus", "status"]), (1, "Submitted"))
		self.assertEqual(frappe.db.get_value("POS Opening Entry", opening, "status"), "Closed")
		# consolidation worked under the clerk's session: the day's sale, its return and the kept sale are merged
		for name in (sale, ret, kept):
			self.assertEqual(frappe.db.get_value("POS Invoice", name, "status"), "Consolidated", name)
		invoice = frappe.db.get_value("POS Invoice", kept, "consolidated_invoice")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)

	def test_a_firearm_style_immediate_merge_runs_as_the_counter_user(self):
		"""consolidate_firearm_invoice_on_submit enqueues this as the cashier: it must merge one invoice."""
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now

		for label, user in self.users.items():
			with self.subTest(role=label):
				frappe.db.savepoint("crp_merge")
				try:
					self._open(user)
					sale = self._sell(user)
					out = consolidate_pos_invoice_now(sale)
					self.assertTrue(out["consolidated"])
					frappe.set_user("Administrator")
					self.assertEqual(frappe.db.get_value("POS Invoice", sale, "status"), "Consolidated")
				finally:
					frappe.set_user("Administrator")
					frappe.db.rollback(save_point="crp_merge")
					frappe.db.sql("UPDATE `tabPOS Opening Entry` SET status = 'Closed' WHERE status = 'Open'")

	def test_a_draft_can_be_deleted_but_a_sale_cannot_be_cancelled_or_deleted(self):
		for label, user in self.users.items():
			with self.subTest(role=label):
				frappe.db.savepoint("crp_draft")
				try:
					self._open(user)
					draft = frappe.new_doc("POS Invoice")
					draft.pos_profile, draft.customer = self.profile, "_Test CD Customer"
					draft.append("items", {"item_code": ITEM, "qty": 1, "rate": 10, "price_list_rate": 10})
					draft.set_missing_values()
					draft.insert()
					frappe.delete_doc("POS Invoice", draft.name)  # the register's Delete Order on a draft
					self.assertFalse(frappe.db.exists("POS Invoice", draft.name))
					sale = self._sell(user)
					with self.assertRaises(frappe.PermissionError):
						frappe.get_doc("POS Invoice", sale).cancel()
					with self.assertRaises(frappe.ValidationError):  # Frappe never deletes a submitted document
						frappe.delete_doc("POS Invoice", sale)
				finally:
					frappe.set_user("Administrator")
					frappe.db.rollback(save_point="crp_draft")
					frappe.db.sql("UPDATE `tabPOS Opening Entry` SET status = 'Closed' WHERE status = 'Open'")

	def test_only_a_manager_deletes_a_cancelled_invoice(self):
		"""The delete grant is for drafts: Frappe stops a submitted invoice, guard_cancelled_delete a cancelled one."""
		clerk, manager = self.users["Sales User + Stock User"], self.users["Sales Manager + Sales User"]
		self._open(clerk)
		sales = [self._sell(clerk) for _ in range(3)]
		frappe.set_user("Administrator")
		for name in sales:
			frappe.get_doc("POS Invoice", name).cancel()
		frappe.set_user(clerk)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("POS Invoice", sales[0])
		self.assertTrue(frappe.db.exists("POS Invoice", sales[0]))
		frappe.set_user(manager)
		frappe.delete_doc("POS Invoice", sales[1])
		self.assertFalse(frappe.db.exists("POS Invoice", sales[1]))
		frappe.set_user("Administrator")
		frappe.delete_doc("POS Invoice", sales[2])
		self.assertFalse(frappe.db.exists("POS Invoice", sales[2]))

	def test_the_books_stay_closed(self):
		"""The counter roles gain the register's doctypes and nothing near the ledger."""
		closed = (
			("Journal Entry", "create"), ("Journal Entry", "read"), ("Payment Entry", "create"), ("Payment Entry", "read"),
			("GL Entry", "read"), ("Account", "write"),
			("POS Invoice", "cancel"), ("POS Invoice", "amend"),
			("Sales Invoice", "cancel"), ("Sales Invoice", "delete"), ("Sales Invoice", "amend"),
			("POS Opening Entry", "cancel"), ("POS Opening Entry", "delete"),
			("POS Closing Entry", "cancel"), ("POS Closing Entry", "delete"),
			("POS Profile", "write"), ("POS Profile", "create"),
		)
		for label, user in self.users.items():
			frappe.set_user(user)
			for doctype, ptype in closed:
				if "Sales Manager" in label and (doctype, ptype) in (
						("POS Opening Entry", "cancel"), ("POS Closing Entry", "cancel")):
					continue  # ERPNext's own rows for Sales Manager, untouched
				self.assertFalse(frappe.has_permission(doctype, ptype), f"{label}: {ptype} {doctype} must stay closed")
			frappe.set_user("Administrator")
		# the clerk cannot cancel a closing or an opening (ERPNext's rows give that to managers only)
		frappe.set_user(self.users["Sales User + Stock User"])
		self.assertFalse(frappe.has_permission("POS Opening Entry", "cancel"))
		self.assertFalse(frappe.has_permission("POS Closing Entry", "cancel"))
		frappe.set_user("Administrator")

	def test_a_clerk_cannot_make_a_journal_entry_or_cancel_a_posted_invoice(self):
		clerk = self.users["Sales User + Stock User"]
		self._open(clerk)
		sale = self._sell(clerk)
		frappe.set_user(clerk)
		je = frappe.get_doc({"doctype": "Journal Entry", "company": self.company, "posting_date": nowdate(),
			"voucher_type": "Journal Entry", "accounts": [
				{"account": self.cash, "debit_in_account_currency": 5},
				{"account": self.sales, "credit_in_account_currency": 5}]})
		with self.assertRaises(frappe.PermissionError):
			je.insert()
		pe = frappe.get_doc({"doctype": "Payment Entry", "payment_type": "Receive", "company": self.company,
			"party_type": "Customer", "party": "_Test CD Customer", "paid_to": self.cash, "paid_amount": 5,
			"received_amount": 5})
		with self.assertRaises(frappe.PermissionError):
			pe.insert()
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("POS Invoice", sale).cancel()
		frappe.set_user("Administrator")
		# once merged, the Sales Invoice is read-only to the clerk too
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now
		frappe.set_user(clerk)
		consolidate_pos_invoice_now(sale)
		frappe.set_user("Administrator")
		merged = frappe.db.get_value("POS Invoice", sale, "consolidated_invoice")
		frappe.set_user(clerk)
		self.assertTrue(frappe.has_permission("Sales Invoice", "read", doc=merged))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Sales Invoice", merged).cancel()


class CounterRegisterGrantRows(FrappeTestCase):
	"""pos_compat.ensure_role_grants on a site that never ran it: the exact rows REGISTER_GRANTS makes."""

	DOCTYPES = tuple(sorted({d for d, *_ in pos_compat.REGISTER_GRANTS}))

	def setUp(self):
		super().setUp()
		_isolate(self, self.DOCTYPES, (pos_compat._ROLE_GRANTS_KEY, pos_compat._REGISTER_GRANTS_KEY))
		self.stock = {d: self._rows("DocPerm", d) for d in self.DOCTYPES}
		pos_compat.ensure_role_grants()

	@staticmethod
	def _rows(table: str, doctype: str) -> dict:
		return {row[:3]: {f for f, v in zip(pos_compat._PERM_FLAGS, row[3]) if v}
			for row in pos_compat.perm_rows(table, doctype)}

	def test_each_doctype_gains_exactly_its_grants(self):
		for doctype in self.DOCTYPES:
			with self.subTest(doctype=doctype):
				site, expected = self._rows("Custom DocPerm", doctype), {k: set(v) for k, v in self.stock[doctype].items()}
				for d, role, ptypes, _add_missing in pos_compat.ROLE_GRANTS + pos_compat.REGISTER_GRANTS:
					if d == doctype:
						expected.setdefault((role, 0, 0), set()).update(ptypes)
				self.assertEqual(site, expected)

	def test_sales_user_gets_no_sales_invoice_cancel_or_delete(self):
		self.assertEqual(self._rows("Custom DocPerm", "Sales Invoice")[("Sales User", 0, 0)],
			{"read", "write", "create", "submit"})

	def test_running_again_changes_nothing(self):
		before = {d: self._rows("Custom DocPerm", d) for d in self.DOCTYPES}
		pos_compat.ensure_role_grants()
		pos_compat.ensure_role_grants()
		self.assertEqual({d: self._rows("Custom DocPerm", d) for d in self.DOCTYPES}, before)

	def test_a_row_an_administrator_removes_stays_removed(self):
		frappe.db.delete("Custom DocPerm", {"parent": "POS Opening Entry", "role": "Sales User"})
		frappe.db.set_global(pos_compat._REGISTER_GRANTS_KEY, frappe.db.get_global(pos_compat._REGISTER_GRANTS_KEY))
		pos_compat.ensure_role_grants()
		self.assertNotIn(("Sales User", 0, 0), self._rows("Custom DocPerm", "POS Opening Entry"))

	def test_an_owner_only_rule_is_not_widened(self):
		"""A role the store restricted to its own documents is left as it is."""
		from frappe.permissions import setup_custom_perms

		frappe.db.delete("Custom DocPerm", {"parent": "POS Invoice"})
		setup_custom_perms("POS Invoice")
		frappe.db.set_value("Custom DocPerm", {"parent": "POS Invoice", "role": "Sales User", "permlevel": 0},
			"if_owner", 1)
		frappe.db.set_global(pos_compat._REGISTER_GRANTS_KEY, "")
		pos_compat.ensure_role_grants()
		row = frappe.db.get_value("Custom DocPerm", {"parent": "POS Invoice", "role": "Sales User", "permlevel": 0},
			["if_owner", "write", "create", "submit"], as_dict=True)
		self.assertEqual((row.if_owner, row.write, row.create, row.submit), (1, 0, 0, 0))

	def test_the_drift_check_covers_the_register_doctypes(self):
		for doctype in self.DOCTYPES:
			self.assertIn(doctype, pos_compat._DRIFT_DOCTYPES)
		# our own rows are not drift
		method = "% permissions differ from ERPNext (%"
		before = len(frappe.get_all("Error Log", filters={"method": ("like", method)}))
		pos_compat.check_perm_drift()
		logs = frappe.get_all("Error Log", filters={"method": ("like", method)}, fields=["error"])
		self.assertEqual(len(logs), before, [x.error for x in logs])
