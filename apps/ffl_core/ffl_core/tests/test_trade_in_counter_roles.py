"""The POS Trade-in is a counter job: Sales User takes trade-ins, for
credit and as a pay-now buy, as the stock roles always could (roles.TRADE_IN_ROLES). A Sales Manager without
the Sales User role is refused.

The gate on create_trade_in_intake is the only door. Both branches go on through
gate-free helpers (firearm_intake.create_intake for the credit draft,
receive_goods._create_receive for the pay-now receipt), and the writes behind them (FFL Acquisition, Item,
Stock Entry / Purchase Receipt, the seller payout Cash Drawer Entry + Journal Entry) must run for a user
holding ONLY Sales User. These tests run as such a user, with no Administrator in the session, so a DocPerm
or a stray gate the counter lacks fails here, not at the register. A counter user files the gun under no
Item (one is made for it) or an existing used firearm Item, never a new or any other Item
(trade_in._check_counter_item).
"""
from __future__ import annotations

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import trade_in
from ffl_core.roles import TRADE_IN_ROLES

SALES_USER = "tradein-sales-user@example.com"
SALES_MANAGER = "tradein-sales-manager@example.com"  # Sales Manager only: refused
SALES_BOTH = "tradein-sales-both@example.com"        # Sales Manager + Sales User: a counter user
STOCK_USER = "tradein-stock-user@example.com"      # Stock User: counts stock, never takes a gun in
STOCK_MANAGER = "tradein-stock-manager@example.com"
ACCOUNTS_USER = "tradein-accounts-user@example.com"  # reads the books, not the counter
COUNTER_USERS = {"Sales User": SALES_USER, "Sales Manager + Sales User": SALES_BOTH}
REFUSAL = "Taking a trade-in needs the Sales User role."


def _ensure_user(email: str, *roles: str) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": roles[0], "send_welcome_email": 0,
			"user_type": "System User",
		}).insert(ignore_permissions=True)
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)


def _item(item_code: str, **fields) -> str:
	"""An Item made as Administrator, for the counter's item_code checks."""
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc({"doctype": "Item", "item_code": item_code, "item_name": item_code, "stock_uom": "Nos",
			"item_group": "All Item Groups", "is_stock_item": 1, **fields}).insert(ignore_permissions=True)
	return item_code


class TradeInCounterRoles(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		from ffl_core.setup.trade_in_credit import ensure
		ensure()
		# The grants a site gets at install / migrate (pos_compat.ROLE_GRANTS); idempotent, a no-op once made.
		from ffl_core.setup.pos_compat import ensure_role_grants
		ensure_role_grants()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		if not frappe.db.exists("Customer", "_Test Trade-in Seller"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Trade-in Seller",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		_ensure_user(SALES_USER, "Sales User")
		_ensure_user(SALES_MANAGER, "Sales Manager")
		_ensure_user(SALES_BOTH, "Sales Manager", "Sales User")
		_ensure_user(STOCK_USER, "Stock User")
		_ensure_user(STOCK_MANAGER, "Stock Manager")
		_ensure_user(ACCOUNTS_USER, "Accounts User")

	def setUp(self) -> None:
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; this is not about FastBound.
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in counter-role tests"))
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def _payload(self, suffix: str, **over) -> dict:
		base = {
			"serial_no": f"TCR-{suffix}",
			"manufacturer": "Glock", "model_name": "19", "caliber": "9mm", "firearm_type": "Pistol",
			"condition_grade": "Good", "id_type": "Driver License", "id_number": f"DL-{suffix}",
			"source_name": "_Test Trade-in Seller", "source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 300,
		}
		base.update(over)
		return base

	def _assert_priced_without_item_price(self, item_code: str, price: float) -> None:
		"""A new per-gun Item keeps the retail price as its reference standard_rate and carries no Item Price
		(item_hooks.sync_standard_selling_price: the serial sets the price)."""
		self.assertEqual(float(frappe.db.get_value("Item", item_code, "standard_rate")), price)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": item_code}))

	def test_the_door_is_sales_user_and_the_stock_roles(self):
		self.assertEqual(set(TRADE_IN_ROLES), {"Sales User", "Stock Manager", "System Manager"})

	def test_the_pos_button_shows_for_the_same_roles(self):
		"""The POS Trade-in button (ffl_core.bundle.js) mirrors roles.TRADE_IN_ROLES and checks it before adding
		the button: a list that drifts either hides it from someone allowed or offers it to someone refused."""
		from ffl_core.tests.front_end_invariants import code_only, read

		js = code_only(read("ffl_core", "public", "js", "ffl_core.bundle.js"))
		[listed] = re.findall(r"const TRADE_IN_ROLES = \[([^\]]*)\];", js)
		self.assertEqual(tuple(re.findall(r"'([^']+)'", listed)), TRADE_IN_ROLES)
		self.assertIn("if (frappe.user.has_role(TRADE_IN_ROLES)) ensureBtn(BUTTON_ID,", js)

	def test_counter_roles_take_a_trade_in_for_credit(self):
		for role, email in COUNTER_USERS.items():
			with self.subTest(role=role):
				suffix = f"credit-{email.split('@')[0]}"
				frappe.set_user(email)
				res = trade_in.create_trade_in_intake(self._payload(suffix, apply_credit=1, retail_price=550))
				frappe.set_user("Administrator")
				self.assertTrue(res["ok"])
				self.assertEqual(float(res["credit"]["rate"]), -300.0)
				self._assert_priced_without_item_price(res["item_code"], 550)
				acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
				self.assertEqual(acq.docstatus, 0, "a credit trade-in stays a draft until the sale submits")
				self.assertEqual(acq.source_type, "Trade-in")
				self.assertEqual(acq.owner, email)

	def test_counter_role_refreshes_its_pending_draft(self):
		"""_refresh_pending_draft: the cashier re-opens the dialog for the same serial with a new cost."""
		frappe.set_user(SALES_USER)
		first = trade_in.create_trade_in_intake(self._payload("refresh", apply_credit=1))
		second = trade_in.create_trade_in_intake(self._payload("refresh", apply_credit=1, acquisition_cost=350))
		frappe.set_user("Administrator")
		self.assertEqual(first["acquisition"], second["acquisition"])
		self.assertEqual(float(second["credit"]["rate"]), -350.0)
		self.assertEqual(float(frappe.db.get_value("FFL Acquisition", first["acquisition"], "cost")), 350.0)

	def test_counter_role_completes_the_booking_at_checkout(self):
		"""The credit draft books when the sale submits, under the cashier's session (complete_intake)."""
		from ffl_core.api.firearm_intake import complete_intake

		frappe.set_user(SALES_USER)
		res = trade_in.create_trade_in_intake(self._payload("complete", apply_credit=1, retail_price=500))
		out = complete_intake(res["acquisition"])
		frappe.set_user("Administrator")
		self.assertTrue(out["booked"])
		self.assertEqual(frappe.db.get_value("FFL Acquisition", res["acquisition"], "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Serial No", "TCR-complete", "item_code"), res["item_code"])

	def test_counter_roles_buy_a_gun_and_pay_the_seller_now(self):
		for role, email in COUNTER_USERS.items():
			with self.subTest(role=role):
				suffix = f"paynow-{email.split('@')[0]}"
				frappe.set_user(email)
				res = trade_in.create_trade_in_intake(self._payload(suffix, payout_method="Cash", retail_price=650))
				frappe.set_user("Administrator")
				self.assertTrue(res["ok"])
				self._assert_priced_without_item_price(res["item_code"], 650)
				# Received on a submitted Purchase Receipt, booked to the bound book, in stock.
				self.assertEqual(frappe.db.get_value("Purchase Receipt", res["purchase_receipt"], "docstatus"), 1)
				acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
				self.assertEqual((acq.docstatus, acq.source_type), (1, "Trade-in"))
				self.assertTrue(frappe.db.exists("Serial No", f"TCR-{suffix}"))
				# The seller's money: one posted Cash Drawer Entry and its submitted Journal Entry.
				paid = res["payout"]
				self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid["entry"], "status"), "Posted")
				self.assertEqual(float(frappe.db.get_value("Cash Drawer Entry", paid["entry"], "amount")), 300.0)
				self.assertEqual(frappe.db.get_value("Journal Entry", paid["journal_entry"], "docstatus"), 1)

	def test_a_role_outside_the_trade_in_set_is_refused_on_both_branches(self):
		for email in (SALES_MANAGER, STOCK_USER, ACCOUNTS_USER):
			for branch in ({"apply_credit": 1}, {"payout_method": "Cash"}):
				with self.subTest(user=email, branch=branch):
					serial = f"refused-{email.split('@')[0]}-{'credit' if branch.get('apply_credit') else 'paynow'}"
					frappe.set_user(email)
					with self.assertRaises(frappe.PermissionError) as refused:
						trade_in.create_trade_in_intake(self._payload(serial, **branch))
					frappe.set_user("Administrator")
					self.assertIn(REFUSAL, str(refused.exception))
					self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": f"TCR-{serial}"}),
						"a refused call must not leave an acquisition behind")

	def test_stock_manager_takes_a_priced_trade_in(self):
		"""Stock Manager has no Item Price create in stock ERPNext either: a priced trade-in failed for it when the
		new Item's insert made the price."""
		frappe.set_user(STOCK_MANAGER)
		res = trade_in.create_trade_in_intake(self._payload("stockmgr", apply_credit=1, retail_price=725))
		frappe.set_user("Administrator")
		self.assertTrue(res["ok"])
		self._assert_priced_without_item_price(res["item_code"], 725)

	def test_the_counter_has_what_the_receipt_needs_and_no_more(self):
		"""The rules in force on this site (granted at install / migrate): the bundle (Stock User's rights short of
		cancel and delete); Item read and Account select are stock ERPNext's -- no price, no item edit, no books."""
		for email in COUNTER_USERS.values():
			frappe.set_user(email)
			for doctype, ptype in (("Serial and Batch Bundle", "read"), ("Serial and Batch Bundle", "write"),
					("Serial and Batch Bundle", "create"), ("Serial and Batch Bundle", "submit"), ("Item", "read"),
					("Account", "select"),
					# the dialog's Link fields (Customer, Caliber, Storage Location)
					("Customer", "select"), ("Caliber", "select"), ("Storage Location", "select")):
				self.assertTrue(frappe.has_permission(doctype, ptype), f"{email}: {ptype} {doctype}")
			for doctype, ptype in (("Serial and Batch Bundle", "cancel"), ("Serial and Batch Bundle", "delete"),
					("Item", "write"), ("Item", "create"), ("Item Price", "create")):
				self.assertFalse(frappe.has_permission(doctype, ptype), f"{email}: {ptype} {doctype} must stay closed")
			frappe.set_user("Administrator")
		frappe.set_user(SALES_MANAGER)  # given nothing for the trade-in
		self.assertFalse(frappe.has_permission("Serial and Batch Bundle", "write"))

	def test_a_counter_user_files_the_gun_under_an_existing_used_firearm_item(self):
		"""The picker's pick: an existing per-gun priced, serialized firearm Item."""
		model = _item("TCR-USED-MODEL", is_firearm=1, has_serial_no=1, is_used=1, item_group="Pistol")
		frappe.set_user(SALES_USER)
		res = trade_in.create_trade_in_intake(self._payload("used-model", apply_credit=1, item_code=model))
		frappe.set_user("Administrator")
		self.assertEqual(res["item_code"], model)

	def test_a_counter_user_cannot_make_or_misfile_an_item(self):
		"""A new item_code (a model SKU the trade-in would make), a uniform-priced catalog SKU, a non-firearm and a
		disabled Item are all refused before anything is written."""
		cases = {
			"missing": "TCR-NO-SUCH-ITEM",
			"uniform": _item("TCR-UNIFORM-SKU", is_firearm=1, has_serial_no=1, uniform_sell_price=1, item_group="Pistol"),
			"not-a-firearm": _item("TCR-ACCESSORY", is_firearm=0, item_group="All Item Groups"),
			"disabled": _item("TCR-DISABLED-MODEL", is_firearm=1, has_serial_no=1, item_group="Pistol", disabled=1),
		}
		items_before = frappe.db.count("Item")
		for case, item_code in cases.items():
			for branch in ({"apply_credit": 1}, {"payout_method": "Cash"}):
				with self.subTest(case=case, branch=branch):
					suffix = f"misfile-{case}-{'credit' if branch.get('apply_credit') else 'paynow'}"
					frappe.set_user(SALES_USER)
					with self.assertRaises(frappe.PermissionError) as refused:
						trade_in.create_trade_in_intake(self._payload(suffix, item_code=item_code, **branch))
					frappe.set_user("Administrator")
					self.assertIn(item_code, str(refused.exception))
					self.assertFalse(frappe.db.exists("FFL Acquisition", {"serial": f"TCR-{suffix}"}))
					self.assertFalse(frappe.db.exists("Serial No", f"TCR-{suffix}"))
		self.assertFalse(frappe.db.exists("Item", "TCR-NO-SUCH-ITEM"))
		self.assertEqual(frappe.db.count("Item"), items_before, "no Item made for a refused trade-in")

	def test_a_stock_role_still_makes_a_model_sku(self):
		"""Stock roles keep today's behaviour: a new item_code becomes a shared used-model SKU."""
		frappe.set_user(STOCK_MANAGER)
		res = trade_in.create_trade_in_intake(self._payload("stock-model", apply_credit=1, item_code="TCR-NEW-MODEL USED"))
		frappe.set_user("Administrator")
		self.assertEqual(res["item_code"], "TCR-NEW-MODEL USED")
		item = frappe.db.get_value("Item", "TCR-NEW-MODEL USED", ["is_firearm", "has_serial_no", "is_used"], as_dict=True)
		self.assertEqual((item.is_firearm, item.has_serial_no, item.is_used), (1, 1, 1))

	def test_receive_goods_keeps_its_gate_and_intake_is_no_endpoint(self):
		"""The widened door must not widen the Receive Goods page: create_receive stays a Stock Manager endpoint.
		firearm_intake.create_intake has no gate of its own because nothing outside reaches it."""
		from ffl_core.api import firearm_intake
		from ffl_core.api.receive_goods import create_receive

		self.assertNotIn(firearm_intake.create_intake, frappe.whitelisted)
		frappe.set_user(SALES_USER)
		with self.assertRaises(frappe.PermissionError):
			create_receive({})


class CounterRoleGrantRows(FrappeTestCase):
	"""pos_compat.ensure_role_grants on a site that never ran it: the exact rows it makes for the trade-in.
	Inside a savepoint its cleanup rolls back."""

	DOCTYPES = ("Serial and Batch Bundle", "Item", "Account")

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		_ensure_user(SALES_MANAGER, "Sales Manager")

	def setUp(self):
		super().setUp()
		from ffl_core.setup import pos_compat
		from ffl_core.tests.test_pos_profile_cashier_perm import _isolate

		_isolate(self, self.DOCTYPES, (pos_compat._ROLE_GRANTS_KEY, pos_compat._REGISTER_GRANTS_KEY))
		self.stock = {d: self._rows("DocPerm", d) for d in self.DOCTYPES}
		pos_compat.ensure_role_grants()

	@staticmethod
	def _rows(table: str, doctype: str) -> dict:
		from ffl_core.setup.pos_compat import _PERM_FLAGS, perm_rows

		return {row[:3]: {f for f, v in zip(_PERM_FLAGS, row[3]) if v} for row in perm_rows(table, doctype)}

	def test_the_bundle_gains_exactly_the_sales_user_grant(self):
		site = self._rows("Custom DocPerm", "Serial and Batch Bundle")
		# ERPNext's select-only row, with the grant added
		self.assertEqual(site.pop(("Sales User", 0, 0)), {"select", "read", "write", "create", "submit"})
		stock = dict(self.stock["Serial and Batch Bundle"])
		stock.pop(("Sales User", 0, 0))
		self.assertEqual(site, stock, "every other role's rule (Sales Manager's select-only row too) came across unchanged")

	def test_item_and_account_keep_sales_users_stock_rules(self):
		"""The trade-in grants nothing on Item or Account. The only rows made there are Sales Manager's, for the
		register (pos_compat.REGISTER_GRANTS); Sales User's are ERPNext's."""
		for doctype in ("Item", "Account"):
			with self.subTest(doctype=doctype):
				site = {k: v for k, v in self._rows("Custom DocPerm", doctype).items() if k[0] != "Sales Manager"}
				stock = {k: v for k, v in self.stock[doctype].items() if k[0] != "Sales Manager"}
				self.assertEqual(site, stock)

	def test_a_sales_manager_only_user_gets_nothing_from_the_trade_in(self):
		frappe.set_user(SALES_MANAGER)
		self.assertFalse(frappe.has_permission("Serial and Batch Bundle", "write"))
		self.assertFalse(frappe.has_permission("Account", "read"))

	def test_a_bundle_row_an_admin_removed_is_left_alone_and_reported(self):
		"""add_missing is False: a Custom DocPerm copy of the bundle without a Sales User row
		stays without one, and check_perm_drift reports the missing shipped row once."""
		from frappe.permissions import setup_custom_perms
		from ffl_core.setup import pos_compat

		frappe.db.set_global(pos_compat._DRIFT_SEEN_KEY, "")
		frappe.db.delete("Custom DocPerm", {"parent": "Serial and Batch Bundle"})
		setup_custom_perms("Serial and Batch Bundle")
		frappe.db.delete("Custom DocPerm", {"parent": "Serial and Batch Bundle", "role": "Sales User"})
		frappe.db.set_global(pos_compat._ROLE_GRANTS_KEY, "")
		pos_compat.ensure_role_grants()
		self.assertNotIn(("Sales User", 0, 0), self._rows("Custom DocPerm", "Serial and Batch Bundle"))
		self.assertIn(("Sales Manager", 0, 0), self._rows("Custom DocPerm", "Serial and Batch Bundle"))
		method = "Serial and Batch Bundle permissions differ from ERPNext (%"
		before = len(frappe.get_all("Error Log", filters={"method": ("like", method)}))
		pos_compat.check_perm_drift()
		pos_compat.check_perm_drift()  # the same difference: no second alert
		logs = frappe.get_all("Error Log", filters={"method": ("like", method)}, fields=["error"],
			order_by="creation desc")
		self.assertEqual(len(logs), before + 1)
		self.assertIn("ERPNext ships Sales User, level 0: select; this site lacks it", logs[0].error)
