"""Counter pre-orders: the order (a submitted Sales Order, no invoice), its payments at the register
(a locked PREORDER-DEPOSIT line booked to Pre-order Deposits), cancel-and-refund, the sub-ledger
report and the general ledger behind it.

Site tests against the real doctypes on the bootstrap company ("FFL Test"). Each money class makes
its own sales-tax template the company default and syncs the 0% template from it (``setup.tax``),
the way a store's migrate does: 8.25% sales tax for a Texas store, 8.75% sales tax + 11% excise for
a California one. Register invoices are built by the pre-order's own builders, checked out as the
register would and merged into their Sales Invoice (``pos_consolidate``), whose GL Entries are read.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from ffl_core import deposits, preorder
from ffl_core.setup import preorder as setup
from ffl_core.setup.preorder import ACCOUNT_TITLE, AVAILABLE_FIELD, PREORDER_ITEM
from ffl_core.tests.test_customer_profile import _hooks, _uniq

COMPANY = "FFL Test"
ABBR = "FT"
GUN = "_T-PO-GUN"
ACC = "_T-PO-ACC"
SVC = "_T-PO-SVC"
CARD = "Credit Card"


def _account(name: str, parent_name: str = "Duties and Taxes", account_type: str = "Tax") -> str:
	acct = f"{name} - {ABBR}"
	if not frappe.db.exists("Account", acct):
		parent = (frappe.db.get_value("Account", {"company": COMPANY, "account_name": parent_name, "is_group": 1})
			or frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Liability", "is_group": 1}, "name",
				order_by="lft"))
		frappe.get_doc({"doctype": "Account", "account_name": name, "company": COMPANY, "parent_account": parent,
			"account_type": account_type, "is_group": 0}).insert(ignore_permissions=True)
	return acct


def _default_template(title: str, rows: list[tuple[str, float]]) -> str:
	"""A sales-tax template made the company's default, and the 0% template synced from it (setup.tax)."""
	name = f"{title} - {ABBR}"
	if not frappe.db.exists("Sales Taxes and Charges Template", name):
		frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": title, "company": COMPANY,
			"taxes": [{"charge_type": "On Net Total", "account_head": a, "rate": r, "description": a}
				for a, r in rows]}).insert(ignore_permissions=True)
	frappe.db.sql("UPDATE `tabSales Taxes and Charges Template` SET is_default = 0 WHERE company = %s", COMPANY)
	frappe.db.set_value("Sales Taxes and Charges Template", name, "is_default", 1)
	from ffl_core.setup import tax
	tax.ensure()
	return name


def _profile(name: str, template: str) -> str:
	bank = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Bank", "is_group": 0})
	for mode, account in (("Cash", f"Cash - {ABBR}"), (CARD, bank)):
		mop = frappe.get_doc("Mode of Payment", mode)
		if not any(r.company == COMPANY for r in mop.accounts):
			mop.append("accounts", {"company": COMPANY, "default_account": account})
			mop.save(ignore_permissions=True)
	if not frappe.db.exists("POS Profile", name):
		frappe.get_doc({
			"doctype": "POS Profile", "name": name, "company": COMPANY, "warehouse": f"Stores - {ABBR}",
			"currency": "USD", "customer_group": "All Customer Groups", "territory": "All Territories",
			"selling_price_list": "Standard Selling", "taxes_and_charges": template,
			"write_off_account": frappe.db.get_value("Account", {"company": COMPANY, "account_name": ("like", "Write Off%")}),
			"write_off_cost_center": f"Main - {ABBR}",
			"income_account": f"Sales - {ABBR}",
			"expense_account": frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Expense Account", "is_group": 0}),
			"payments": [{"mode_of_payment": "Cash", "default": 1}, {"mode_of_payment": CARD}],
		}).insert(ignore_permissions=True)
	frappe.db.set_value("POS Profile", name, "taxes_and_charges", template)
	return name


def _items() -> None:
	if not frappe.db.exists("Item", GUN):
		frappe.get_doc({"doctype": "Item", "item_code": GUN, "item_name": "Test Pre-order Pistol", "item_group": "Pistol",
			"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			"serial_no_series": "TPOG-.####"}).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", SVC):
		frappe.get_doc({"doctype": "Item", "item_code": SVC, "item_name": "Test Cleaning Kit",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", ACC):
		frappe.get_doc({"doctype": "Item", "item_code": ACC, "item_name": "Test Pre-order Optic",
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)


def _customer() -> str:
	return frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("Pre-order"), "customer_type": "Individual",
		"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True).name


def _checkout(name: str, mode: str = "Cash", amount: float | None = None):
	"""Check a register invoice out the way the register does: one tender for what it comes to (a
	return keeps the split its builder made)."""
	inv = frappe.get_doc("POS Invoice", name)
	if not inv.is_return:
		total = flt(inv.rounded_total) or flt(inv.grand_total)
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": mode, "amount": total if amount is None else amount, "default": 1})
	for p in inv.payments:
		if p.mode_of_payment == CARD and flt(p.amount) and not p.reference_no:
			p.reference_no = "TEST-APPROVAL"   # a standalone terminal's approval code
	inv.flags.ignore_permissions = True
	inv.save()
	inv.submit()
	return inv


def _merge(name: str) -> str:
	from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now

	out = consolidate_pos_invoice_now(name)
	assert out.get("sales_invoice"), out
	return out["sales_invoice"]


def _gl(voucher: str) -> dict[str, float]:
	"""{account: debit − credit} of a voucher's live GL Entries."""
	out: dict[str, float] = {}
	for r in frappe.get_all("GL Entry", filters={"voucher_no": voucher, "is_cancelled": 0},
			fields=["account", "debit", "credit"]):
		out[r.account] = flt(out.get(r.account, 0) + flt(r.debit) - flt(r.credit), 2)
	return {a: v for a, v in out.items() if v}


def _gl_on_sales_tax_cost_or_stock(voucher: str) -> list[str]:
	"""Accounts of a voucher's GL that a payment must never touch: income, expense (COGS), stock, tax."""
	accounts = list(_gl(voucher))
	if not accounts:
		return []
	meta = {r.name: r for r in frappe.get_all("Account", filters={"name": ["in", accounts]},
		fields=["name", "root_type", "account_type"])}
	return [a for a in accounts if meta[a].root_type in ("Income", "Expense")
		or meta[a].account_type in ("Stock", "Tax", "Cost of Goods Sold", "Stock Adjustment")]


class _Base(FrappeTestCase):
	TEMPLATE_TITLE = "_Test PO TX 8.25"
	PROFILE = "_Test PO Register"

	@classmethod
	def tax_rows(cls) -> list[tuple[str, float]]:
		return [(_account("_Test PO Sales Tax"), 8.25)]

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup.ensure()   # first: on a fresh site it adds columns, and DDL commits (docs/testing.md rule 1)
		frappe.defaults.set_user_default("Company", COMPANY)
		cls.template = _default_template(cls.TEMPLATE_TITLE, cls.tax_rows())
		setup.ensure()   # the payment item's 0% template, synced to the new default
		cls.profile = _profile(cls.PROFILE, cls.template)
		_items()
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(COMPANY, cls.profile)
		cls.deposits = f"{ACCOUNT_TITLE} - {ABBR}"

	def setUp(self):
		frappe.set_user("Administrator")
		# ERPNext's merge commits inside its submit; keep each test's rows in the class transaction.
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		self.cust = _customer()

	def _preorder(self, item=GUN, qty=1, rate=1000.0, available=3, customer=None) -> str:
		frappe.db.set_value("Item", item, AVAILABLE_FIELD, available)
		return preorder.create_preorder({"customer": customer or self.cust,
			"items": [{"item_code": item, "qty": qty, "rate": rate}]})["sales_order"]

	def _payment(self, so: str, amount: float) -> str:
		return preorder.open_payment(so, amount)["pos_invoice"]


class Setup(FrappeTestCase):

	def test_hooks(self):
		hooks = _hooks()
		events = hooks["doc_events"]
		for dt in ("POS Invoice", "Sales Invoice"):
			self.assertIn("ffl_core.preorder.guard_payment_lines", events[dt]["validate"], dt)
		validate = events["POS Invoice"]["validate"]
		self.assertLess(validate.index("ffl_core.preorder.guard_payment_lines"),
			validate.index("ffl_core.compliance.locked_lines.assert_locked_lines"))
		self.assertIn("ffl_core.preorder.before_invoice_cancel", events["POS Invoice"]["before_cancel"])
		self.assertEqual(events["Sales Order"]["before_cancel"], "ffl_core.preorder.before_order_cancel")
		self.assertIn("ffl_core.preorder.on_order_cancel", events["Sales Order"]["on_cancel"])
		after = hooks["after_migrate"]
		self.assertLess(after.index("ffl_core.setup.gunsmith.ensure"), after.index("ffl_core.setup.preorder.ensure"))

	def test_account_item_and_fields(self):
		setup.ensure()
		account = setup.preorder_account(COMPANY)
		self.assertEqual(account, f"{ACCOUNT_TITLE} - {ABBR}")
		acct = frappe.db.get_value("Account", account, ["root_type", "parent_account", "is_group"], as_dict=True)
		self.assertEqual((acct.root_type, acct.is_group), ("Liability", 0))
		self.assertTrue(acct.parent_account.startswith("Current Liabilit"), acct.parent_account)
		self.assertNotEqual(account, deposits.account(deposits.GUNSMITH, COMPANY), "its own account, not Customer Deposits")
		item = frappe.db.get_value("Item", PREORDER_ITEM, ["item_group", "is_stock_item", "is_firearm", "has_serial_no"],
			as_dict=True)
		self.assertEqual((item.is_stock_item, item.is_firearm, item.has_serial_no), (0, 0, 0))
		firearm_groups = {"Firearm", "Pistol", "Revolver", "Rifle", "Shotgun", "Other Firearm", "Receiver", "Handgun"}
		group, parents = item.item_group, []
		while group:
			parents.append(group)
			group = frappe.db.get_value("Item Group", group, "parent_item_group")
		self.assertFalse(firearm_groups & set(parents), parents)
		self.assertEqual(frappe.db.get_value("Item Default", {"parent": PREORDER_ITEM, "company": COMPANY},
			"income_account"), account)
		self.assertTrue(frappe.db.exists("Item Tax", {"parent": PREORDER_ITEM,
			"item_tax_template": f"Non-Taxable Service - {ABBR}"}), "the payment carries its own 0% template")
		from ffl_core.setup.locked_lines import TOMBSTONE_DATE, locked_items
		self.assertIn(PREORDER_ITEM, locked_items())
		self.assertTrue(frappe.db.exists("Item Price", {"item_code": PREORDER_ITEM, "price_list": "Standard Selling",
			"valid_from": TOMBSTONE_DATE}))
		for dt, field in (("Item", AVAILABLE_FIELD), ("Item", setup.ETA_FIELD), ("Sales Order", setup.FLAG_FIELD),
				("POS Invoice", setup.LINK_FIELD), ("POS Invoice Item", setup.LINK_FIELD),
				("Sales Invoice Item", setup.LINK_FIELD)):
			self.assertTrue(frappe.db.has_column(dt, field), f"{dt}.{field}")
		self.assertTrue(frappe.db.has_index("tabPOS Invoice Item", "ffl_preorder_index"),
			"a payment's locking read covers its own order's lines")
		self.assertEqual(frappe.db.get_value("Custom Field", "Sales Order-ffl_is_preorder", "no_copy"), 1,
			"an amended order took no pre-order slots")

	def test_off_until_hand_over_is_built(self):
		"""Nothing offers a pre-order while every count is 0, and the count is not on the Item form yet."""
		for field in (AVAILABLE_FIELD, setup.ETA_FIELD):
			self.assertEqual(frappe.db.get_value("Custom Field", f"Item-{field}", "hidden"), 1, field)
		self.assertFalse(frappe.db.exists("Item", {AVAILABLE_FIELD: [">", 0], "name": ["not like", "\\_T-PO-%"]}))

	def test_setup_is_idempotent(self):
		setup.ensure()
		self.assertEqual(setup.ensure(), {"accounts": 0, "items": 0, "defaults": 0})


class Money(_Base):

	def test_ac1_cash_deposit_books_to_preorder_deposits_only(self):
		"""$200 cash: Dr Cash 200 / Cr Pre-order Deposits 200, and nothing on sales, tax, cost or stock."""
		so = self._preorder()
		self.assertEqual(frappe.db.get_value("Sales Order", so, ["docstatus", "per_billed"]), (1, 0))
		self.assertFalse(frappe.get_all("Sales Invoice Item", filters={"sales_order": so}), "no invoice for the order")
		inv = _checkout(self._payment(so, 200))
		self.assertEqual(flt(inv.total_taxes_and_charges), 0)
		self.assertEqual(flt(inv.grand_total), 200)
		self.assertEqual(inv.ffl_preorder, so, "the guard links the header")
		self.assertEqual(inv.items[0].income_account, self.deposits)
		si = _merge(inv.name)
		self.assertEqual(_gl(si), {f"Cash - {ABBR}": 200, self.deposits: -200})
		self.assertEqual(_gl_on_sales_tax_cost_or_stock(si), [])
		self.assertFalse(frappe.get_all("GL Entry", filters={"voucher_no": so}))
		self.assertEqual(preorder.held(so), 200)

	def test_ac2_full_payment_by_card_is_held_not_sold(self):
		"""$1,082.50 (1,000 + 8.25%) by card: Dr card account / Cr Pre-order Deposits; Net Sales 0."""
		from ffl_core.ffl_core.report.sales_report.sales_report import net_sales_of

		so = self._preorder()
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so, "grand_total")), 1082.5)
		inv = _checkout(self._payment(so, 1082.5), mode=CARD)
		si = _merge(inv.name)
		card_account = frappe.db.get_value("Mode of Payment Account", {"parent": CARD, "company": COMPANY},
			"default_account")
		self.assertEqual(_gl(si), {card_account: 1082.5, self.deposits: -1082.5})
		self.assertEqual(_gl_on_sales_tax_cost_or_stock(si), [])
		self.assertEqual(net_sales_of("POS Invoice", {"name": inv.name}), {inv.name: 0})
		self.assertEqual(net_sales_of("Sales Invoice", {"name": si}), {si: 0})

	def test_ac3_payments_add_up_and_stop_at_the_total(self):
		so = self._preorder()
		_checkout(self._payment(so, 200))
		_checkout(self._payment(so, 882.5), mode=CARD)
		self.assertEqual(preorder.held(so), 1082.5)
		with self.assertRaisesRegex(frappe.ValidationError, "left to pay"):
			preorder.open_payment(so, 0.01)
		# A draft opened while room was left is refused at checkout once the room is gone.
		so2 = self._preorder(available=5)
		first = self._payment(so2, 1000)
		later = deposits.new_invoice(deposits.PREORDER, frappe.get_doc("Sales Order", so2),
			[deposits.line(deposits.PREORDER, frappe.get_doc("Sales Order", so2), 500, "x")])
		later.insert()
		_checkout(first)
		with self.assertRaisesRegex(frappe.ValidationError, "left to pay"):
			_checkout(later.name)
		self.assertEqual(preorder.held(so2), 1000)

	def test_one_open_payment_draft_per_order(self):
		so = self._preorder()
		first = preorder.open_payment(so, 100)
		again = preorder.open_payment(so, 300)
		self.assertEqual((again["pos_invoice"], again["already"], again["amount"]), (first["pos_invoice"], True, 100))
		for bad in (0, -5):
			with self.subTest(amount=bad), self.assertRaises(frappe.ValidationError):
				preorder.open_payment(self._preorder(), bad)

	def test_ac4_cancel_refunds_every_payment_and_gives_the_slots_back(self):
		"""$200 cash then $882.50 card, cancelled: each comes back the way it was paid, the deposit account
		goes back to 0, the order is cancelled and the item takes pre-orders again."""
		so = self._preorder(available=3)
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 2)
		cash = _checkout(self._payment(so, 200))
		card = _checkout(self._payment(so, 882.5), mode=CARD)
		with self.assertRaisesRegex(frappe.ValidationError, "Cancel & refund"):
			doc = frappe.get_doc("Sales Order", so)
			doc.flags.ignore_permissions = True
			doc.cancel()   # on its own, from the form
		out = preorder.cancel_preorder(so, "Maker delayed it a year")
		self.assertTrue(out["cancelled"])
		self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 3)
		self.assertEqual(sorted(r["amount"] for r in out["refunds"]), [200, 882.5])
		self.assertEqual(preorder.refund_preorder(so)["refunds"], out["refunds"], "the open returns, not new ones")
		returns = [_checkout(r["pos_invoice"]) for r in out["refunds"]]
		self.assertEqual(preorder.held(so), 0)
		card_account = frappe.db.get_value("Mode of Payment Account", {"parent": CARD, "company": COMPANY},
			"default_account")
		paid = [_merge(i.name) for i in (cash, card)]
		credit_notes = [_merge(r.name) for r in returns]
		total: dict[str, float] = {}
		for si in paid + credit_notes:
			self.assertEqual(_gl_on_sales_tax_cost_or_stock(si), [], si)
			for a, v in _gl(si).items():
				total[a] = flt(total.get(a, 0) + v, 2)
		self.assertEqual({a: v for a, v in total.items() if v}, {})
		self.assertEqual(sorted((_gl(si) for si in credit_notes), key=lambda g: g[self.deposits]),
			[{self.deposits: 200, f"Cash - {ABBR}": -200}, {self.deposits: 882.5, card_account: -882.5}])
		self.assertFalse(preorder.list_preorders(customer=self.cust), "nothing left to do")

	def test_a_refunded_payment_stays(self):
		so = self._preorder()
		pay = _checkout(self._payment(so, 100))
		ret = preorder.cancel_preorder(so, "No")["refunds"][0]["pos_invoice"]
		_checkout(ret)
		pay.reload()
		pay.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "already been refunded"):
			pay.cancel()

	def test_cancel_refused_while_a_payment_is_open(self):
		so = self._preorder()
		self._payment(so, 100)
		with self.assertRaisesRegex(frappe.ValidationError, "still open"):
			preorder.cancel_preorder(so, "No")
		with self.assertRaises(frappe.ValidationError):
			preorder.cancel_preorder(so, "  ")

	def test_a_cancelled_order_takes_no_payment(self):
		so = self._preorder()
		preorder.cancel_preorder(so, "No")
		with self.assertRaisesRegex(frappe.ValidationError, "cancelled"):
			preorder.open_payment(so, 10)

	def test_ac7_the_payment_line_comes_only_from_its_preorder(self):
		so = self._preorder()
		line = deposits.line(deposits.PREORDER, frappe.get_doc("Sales Order", so), 100, "x")

		def ticket(customer, *rows, **header):
			inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
				"customer": customer, "items": list(rows), **header})
			inv.set_missing_values()
			inv.flags.ignore_permissions = True
			return inv

		with self.subTest("from the item list"), self.assertRaisesRegex(frappe.ValidationError, "comes from its pre-order"):
			ticket(self.cust, {"item_code": PREORDER_ITEM, "qty": 1, "rate": 100}).insert()
		with self.subTest("another customer"), self.assertRaisesRegex(frappe.ValidationError, "another customer"):
			ticket(_customer(), dict(line)).insert()
		with self.subTest("two lines"), self.assertRaisesRegex(frappe.ValidationError, "One pre-order payment"):
			ticket(self.cust, dict(line), dict(line)).insert()
		with self.subTest("no lock"), self.assertRaises(frappe.ValidationError):
			ticket(self.cust, {**line, "ffl_locked_rate": 0}).insert()
		with self.subTest("two units"), self.assertRaises(frappe.ValidationError):
			ticket(self.cust, {**line, "qty": 2}).insert()
		with self.subTest("a release, before hand-over exists"), self.assertRaises(frappe.ValidationError):
			ticket(self.cust, {**line, "rate": -100, "ffl_locked_rate": -100}).insert()
		with self.subTest("invoice discount"), self.assertRaisesRegex(frappe.ValidationError, "invoice discount"):
			ticket(self.cust, dict(line), discount_amount=10, apply_discount_on="Grand Total").insert()
		with self.subTest("by hand on a Sales Invoice"), self.assertRaisesRegex(frappe.ValidationError, "at the register"):
			si = frappe.new_doc("Sales Invoice")
			si.company, si.customer = COMPANY, self.cust
			si.append("items", dict(line))
			preorder.guard_payment_lines(si)
		with self.subTest("a work order's link is not a pre-order"), self.assertRaises(frappe.ValidationError):
			ticket(self.cust, {**line, "ffl_preorder": None, "ffl_work_order": "GWO-NOPE"}).insert()

	def test_ac7_the_amount_is_locked(self):
		"""Typed, priced or re-stamped, the line keeps the amount it was made for."""
		inv = frappe.get_doc("POS Invoice", self._payment(self._preorder(), 60))
		inv.items[0].rate = 5
		inv.items[0].price_list_rate = 999
		inv.items[0].discount_percentage = 50
		inv.save(ignore_permissions=True)
		self.assertEqual((flt(inv.items[0].rate), flt(inv.items[0].price_list_rate)), (60, 0))
		self.assertEqual(inv.items[0].income_account, self.deposits)
		self.assertFalse(frappe.get_all("Item Price", filters={"item_code": PREORDER_ITEM, "valid_upto": [">", "2000-01-01"]}),
			"no live price for the payment item")

	def test_a_preorder_takes_only_whats_left_and_never_a_stocked_item(self):
		self._preorder(available=1)
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 0)
		with self.assertRaisesRegex(frappe.ValidationError, "can still be pre-ordered"):
			preorder.create_preorder({"customer": self.cust, "items": [{"item_code": GUN, "qty": 1, "rate": 1000}]})
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 0)
		with self.assertRaisesRegex(frappe.ValidationError, "one unit per line"):
			self._preorder(qty=2, available=5)
		with self.assertRaises(frappe.ValidationError):
			self._preorder(item=PREORDER_ITEM, available=5)
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
		make_stock_entry(item_code=ACC, target=f"Stores - {ABBR}", qty=2, basic_rate=10, company=COMPANY)
		self.assertEqual(preorder.local_stock(ACC, COMPANY), 2)
		self.assertFalse(preorder.preorder_options(ACC)["can_preorder"])
		with self.assertRaisesRegex(frappe.ValidationError, "in stock"):
			self._preorder(item=ACC, qty=1, rate=50, available=5)
		self.assertEqual(frappe.db.get_value("Item", ACC, AVAILABLE_FIELD), 5, "a refused order takes nothing")
		frappe.db.set_value("Item", GUN, AVAILABLE_FIELD, 2)
		opts = preorder.preorder_options(GUN)
		self.assertEqual((opts["available"], opts["in_stock"], opts["can_preorder"]), (2, 0, True))

	def test_ac9_a_preorder_gun_needs_no_serial_and_the_counter_still_refuses_one_not_in_stock(self):
		so = self._preorder()
		doc = frappe.get_doc("Sales Order", so)
		self.assertEqual((doc.docstatus, doc.ffl_is_preorder, [r.item_code for r in doc.items]), (1, 1, [GUN]))
		self.assertFalse(frappe.db.exists("Serial No", {"item_code": GUN}))
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": self.profile,
			"customer": self.cust, "items": [{"item_code": GUN, "qty": 1, "rate": 1000}]})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			inv.insert()
			_checkout(inv.name)
		from ffl_core.api.manual_order import _validate_items
		with self.assertRaisesRegex(frappe.ValidationError, "exactly one serial"):
			_validate_items([{"item_code": GUN, "qty": 1, "rate": 1000}], customer=self.cust)

	def test_the_queue(self):
		so = self._preorder()
		_checkout(self._payment(so, 300))
		draft = self._payment(so, 100)
		row = next(r for r in preorder.list_preorders(customer=self.cust) if r["sales_order"] == so)
		self.assertEqual((row["source"], row["stage"], row["held"], row["outstanding_amount"], row["paid"]),
			("preorder", "waiting", 300, 782.5, False))
		self.assertEqual(row["drafts"]["payment"], draft)
		self.assertFalse(row["overdue"])
		frappe.delete_doc("POS Invoice", draft, ignore_permissions=True)
		preorder.cancel_preorder(so, "No")
		row = next(r for r in preorder.list_preorders(customer=self.cust) if r["sales_order"] == so)
		self.assertEqual((row["stage"], row["cancelled"], len(row["drafts"]["refunds"])), ("blocked", True, 1))
		_checkout(row["drafts"]["refunds"][0])
		self.assertFalse([r for r in preorder.list_preorders(customer=self.cust) if r["sales_order"] == so])

	def test_a_cancelled_order_without_money_gives_back_from_the_form(self):
		so = self._preorder(available=2)
		doc = frappe.get_doc("Sales Order", so)
		doc.flags.ignore_permissions = True
		doc.cancel()
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 2)


class Review(_Base):
	"""Fixes from review: a returned item beside the payment, a partly returned payment, the Item's
	modified on give-back, and a second amount while a payment is open."""

	def _return_goods(self, source: str, *, submit: bool):
		"""The register's Return of the goods on a payment ticket (not its payment line)."""
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		ret = make_sales_return(source)
		ret.items = [r for r in ret.items if r.item_code != PREORDER_ITEM]
		ret.flags.ignore_permissions = True
		ret.set_missing_values()
		ret.calculate_taxes_and_totals()
		for p in ret.payments:
			p.amount = p.base_amount = 0
		ret.payments[0].amount = ret.payments[0].base_amount = flt(ret.rounded_total) or flt(ret.grand_total)
		ret.paid_amount = ret.base_paid_amount = ret.payments[0].amount
		ret.insert()
		if submit:
			ret.submit()
		return ret

	def test_f2_goods_returned_from_the_payment_ticket_leave_the_payment_refundable(self):
		so = self._preorder()
		inv = frappe.get_doc("POS Invoice", self._payment(so, 200))
		inv.append("items", {"item_code": SVC, "qty": 1, "rate": 40})
		inv.set_missing_values()
		inv.save(ignore_permissions=True)
		pay = _checkout(inv.name)
		self._return_goods(pay.name, submit=True)
		out = preorder.cancel_preorder(so, "No")
		self.assertEqual([r["amount"] for r in out["refunds"]], [200])
		ret = frappe.get_doc("POS Invoice", out["refunds"][0]["pos_invoice"])
		self.assertEqual([(r.item_code, flt(r.amount)) for r in ret.items], [(PREORDER_ITEM, -200)])
		_checkout(ret.name)
		self.assertEqual(preorder.held(so), 0)
		self.assertFalse(deposits.refundable_payments(deposits.PREORDER, so))

	def test_f2_a_partly_returned_payment_line_is_not_offered_again(self):
		"""A payment line ERPNext has already returned once cannot be returned again (it counts returned
		qty per line): what it still holds is named, not silently dropped."""
		so = self._preorder()
		pay = _checkout(self._payment(so, 100))
		part = deposits.refund_return(deposits.PREORDER, so, pay.name, 40)
		_checkout(part.name)
		self.assertEqual(preorder.held(so), 60)
		self.assertEqual(deposits.refundable_payments(deposits.PREORDER, so), [])
		self.assertEqual(preorder.cancel_preorder(so, "No")["refunds"], [])
		with self.assertRaisesRegex(frappe.ValidationError, "returned only once"):
			preorder.refund_preorder(so)
		self.assertEqual(preorder.held(so), 60)

	def test_f5_giving_slots_back_moves_the_items_modified(self):
		"""A form opened before an order takes or gives back slots is refused on save, both ways."""
		frappe.db.set_value("Item", GUN, AVAILABLE_FIELD, 3)
		opened = frappe.get_doc("Item", GUN)
		so = self._preorder(available=3)
		opened.description = "edited"
		with self.assertRaises(frappe.TimestampMismatchError):
			opened.save(ignore_permissions=True)
		opened = frappe.get_doc("Item", GUN)
		before = opened.modified
		preorder.cancel_preorder(so, "No")
		self.assertEqual(frappe.db.get_value("Item", GUN, AVAILABLE_FIELD), 3)
		self.assertGreater(str(frappe.db.get_value("Item", GUN, "modified")), str(before))
		opened.description = "edited"
		with self.assertRaises(frappe.TimestampMismatchError):
			opened.save(ignore_permissions=True)

	def test_f6_a_second_amount_names_the_payment_already_open(self):
		so = self._preorder()
		first = preorder.open_payment(so, 100)
		self.assertIsNone(first["notice"])
		same = preorder.open_payment(so, 100)
		self.assertEqual((same["pos_invoice"], same["already"], same["notice"]), (first["pos_invoice"], True, None))
		other = preorder.open_payment(so, 300)
		self.assertEqual((other["pos_invoice"], other["amount"]), (first["pos_invoice"], 100))
		self.assertIn("100", other["notice"])
		self.assertIn(so, other["notice"])


class SubLedger(_Base):
	"""AC6: three pre-orders — part paid, paid in full, cancelled and refunded — and the report's balance
	equals the GL balance of Pre-order Deposits to the cent, once the register's invoices are merged."""

	def test_ac6_report_equals_the_general_ledger(self):
		from ffl_core.ffl_core.report.pre_order_deposits import pre_order_deposits as report

		part = self._preorder()
		full = self._preorder(customer=_customer())
		gone = self._preorder(customer=_customer())
		invoices = [_checkout(self._payment(part, 200)), _checkout(self._payment(full, 1000)),
			_checkout(self._payment(full, 82.5), mode=CARD), _checkout(self._payment(gone, 300))]
		returns = [_checkout(r["pos_invoice"]) for r in preorder.cancel_preorder(gone, "No")["refunds"]]
		columns, rows, _msg, _chart, summary = report.execute({"company": COMPANY, "as_of_date": nowdate()})
		by = {r["sales_order"]: r for r in rows}
		self.assertEqual({k: by[part][k] for k in ("collected", "refunded", "balance", "status", "aging", "channel")},
			{"collected": 200, "refunded": 0, "balance": 200, "status": "Open", "aging": "0–30", "channel": "Counter"})
		self.assertEqual((by[full]["collected"], by[full]["balance"]), (1082.5, 1082.5))
		self.assertEqual((by[gone]["collected"], by[gone]["refunded"], by[gone]["balance"], by[gone]["status"]),
			(300, 300, 0, "Cancelled"))
		held = flt(sum(r["balance"] for r in rows), 2)
		self.assertEqual(held, 1282.5)
		self.assertEqual(report.gl_balance(COMPANY, nowdate()), 0, "nothing merged yet")
		for inv in invoices + returns:
			_merge(inv.name)
		self.assertEqual(report.gl_balance(COMPANY, nowdate()), held)
		_c, rows, _m, _ch, summary = report.execute({"company": COMPANY, "as_of_date": nowdate()})
		self.assertEqual([s["value"] for s in summary], [1282.5, 1282.5, 0])
		# As of yesterday nothing was held.
		self.assertEqual(report.build_rows(COMPANY, add_days(nowdate(), -1)), [])

	def test_aging_buckets(self):
		from ffl_core.ffl_core.report.pre_order_deposits.pre_order_deposits import aging_bucket

		self.assertEqual([aging_bucket(d) for d in (0, 30, 31, 90, 91, 180, 181, 365, 366)],
			["0–30", "0–30", "31–90", "31–90", "91–180", "91–180", "181–365", "181–365", ">365"])


class California(_Base):
	"""AC10: 8.75% sales tax + 11% excise. The order's total carries both; a payment carries neither."""
	TEMPLATE_TITLE = "_Test PO CA"
	PROFILE = "_Test PO CA Register"

	@classmethod
	def tax_rows(cls):
		from ffl_core.setup.tax import EXCISE_ACCOUNT_NAME
		return [(_account("_Test PO CA Sales Tax"), 8.75), (_account(EXCISE_ACCOUNT_NAME), 11)]

	def test_ac10_a_payment_carries_no_sales_tax_and_no_excise(self):
		so = self._preorder()
		self.assertEqual(flt(frappe.db.get_value("Sales Order", so, "grand_total")), 1197.5)
		inv = _checkout(self._payment(so, 200))
		self.assertEqual(flt(inv.total_taxes_and_charges), 0)
		self.assertEqual(sorted(t.account_head for t in inv.taxes), sorted(a for a, _r in self.tax_rows()))
		self.assertTrue(all(not flt(t.tax_amount) for t in inv.taxes))
		si = _merge(inv.name)
		self.assertEqual(_gl(si), {f"Cash - {ABBR}": 200, self.deposits: -200})
		# Paid in full: the whole total with both taxes is held, none of it booked as tax.
		_checkout(self._payment(so, 997.5), mode=CARD)
		self.assertEqual(preorder.held(so), 1197.5)

	def test_a_template_the_zero_template_does_not_cover_is_refused(self):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		other = _account("_Test PO Uncovered Tax")
		title = "_Test PO Uncovered"
		name = frappe.db.get_value("Sales Taxes and Charges Template", {"title": title, "company": COMPANY}) \
			or frappe.get_doc({"doctype": "Sales Taxes and Charges Template", "title": title, "company": COMPANY,
				"taxes": [{"charge_type": "On Net Total", "account_head": other, "rate": 5, "description": "x"}]}
			).insert(ignore_permissions=True).name
		inv = frappe.get_doc("POS Invoice", self._payment(self._preorder(), 100))
		inv.taxes_and_charges = name
		inv.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", name))
		with self.assertRaisesRegex(frappe.ValidationError, "would be taxed"):
			inv.save(ignore_permissions=True)


class LastSlotRace(FrappeTestCase):
	"""AC5: two counters take the last pre-order of an item at the same moment; one order is made.

	Real sessions in two threads, so the fixtures are committed (and deleted in the finally below).
	The second session reads the count before the first commits (its REPEATABLE READ snapshot says 1)
	and then waits on the row lock: only a locking read after the lock sees the 0."""

	def test_ac5_only_one_order_gets_the_last_unit(self):
		setup.ensure()
		token = frappe.generate_hash(length=6).upper()
		item = f"_T-PO-RACE-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 1, AVAILABLE_FIELD: 1}).insert(ignore_permissions=True)
		customers = [frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test PO Race {token} {i}",
			"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories"}
			).insert(ignore_permissions=True).name for i in range(2)]
		frappe.db.commit()   # the other sessions must see the item and the customers (deleted in the finally below)
		site, results = frappe.local.site, []
		barrier = threading.Barrier(2)
		real = preorder.cint

		def slow_cint(value=None, *args, **kwargs):
			# take_slots reads the count and casts it here: both sessions sit between the read and the write
			# at once, unless the row lock serializes them.
			if isinstance(value, (int, float)) and threading.current_thread() is not threading.main_thread():
				time.sleep(0.3)
			return real(value, *args, **kwargs)

		def order(customer):
			frappe.init(site=site, sites_path=frappe.local.sites_path if hasattr(frappe.local, "sites_path") else ".")
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				frappe.db.get_value("Item", item, AVAILABLE_FIELD)   # this session's snapshot: 1 left
				barrier.wait(timeout=30)
				out = preorder.create_preorder({"customer": customer, "items": [{"item_code": item, "qty": 1, "rate": 10}]})
				frappe.db.commit()
				results.append(out)
			except Exception as e:
				results.append({"error": type(e).__name__ + ": " + str(e)})
				frappe.db.rollback()
			finally:
				frappe.destroy()

		try:
			with patch.object(preorder, "cint", side_effect=slow_cint):
				threads = [threading.Thread(target=order, args=(c,)) for c in customers]
				for t in threads:
					t.start()
				for t in threads:
					t.join(timeout=120)
			frappe.db.rollback()
			self.assertEqual(len(results), 2, results)
			made = [r for r in results if r.get("sales_order")]
			refused = [r for r in results if r.get("error")]
			self.assertEqual(len(made), 1, results)
			self.assertEqual(len(refused), 1, results)
			self.assertIn("just pre-ordered at another counter", refused[0]["error"])
			self.assertEqual(frappe.db.get_value("Item", item, AVAILABLE_FIELD), 0)
		finally:
			frappe.db.rollback()
			for so in frappe.get_all("Sales Order", filters={"customer": ["in", customers]}, pluck="name"):
				doc = frappe.get_doc("Sales Order", so)
				if doc.docstatus == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
				frappe.delete_doc("Sales Order", so, force=True, ignore_permissions=True)
			for c in customers:
				frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
			frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
			frappe.db.commit()


class SalesReport(FrappeTestCase):
	"""A pre-order payment is never a sale: like a gunsmith deposit, it comes off Net Sales."""

	def test_order_view_leaves_preorder_payments_out(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _build_product_rows, _build_rows

		headers = [{"name": "POP", "posting_date": "2026-11-17", "base_net_total": 1082.5, "grand_total": 1082.5,
			"voucher_type": "POS Invoice", "customer": "X"}]
		lines = {"POP": [{"item_code": PREORDER_ITEM, "qty": 1, "base_net_amount": 1082.5}]}
		rows = {r.get("invoice"): r for r in _build_rows(headers, lines, {}, {}, "All")}
		self.assertEqual((flt(rows["POP"]["net_sales"]), flt(rows["POP"]["cost"])), (0, 0))
		detail = _build_product_rows(headers, lines, {}, {}, {}, {}, {}, {}, "", True)
		self.assertFalse(any(r.get("item_code") == PREORDER_ITEM for r in detail))
		self.assertIn(PREORDER_ITEM, deposits.ITEMS)


class PaymentLockRace(FrappeTestCase):
	"""F4: a payment checked out while another session changed its pre-order after this one began
	reading: MariaDB refuses the locking read (1020). The cashier is told to try again, not shown a
	database error. Committed fixtures (deleted in the finally below): the other session must see them."""

	def test_f4_a_lock_conflict_at_checkout_says_try_again(self):
		setup.ensure()
		token = frappe.generate_hash(length=6).upper()
		item = f"_T-PO-LOCK-{token}"
		frappe.get_doc({"doctype": "Item", "item_code": item, "item_name": item, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, AVAILABLE_FIELD: 1}).insert(ignore_permissions=True)
		customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"_Test PO Lock {token}",
			"customer_type": "Individual", "customer_group": "Individual", "territory": "All Territories"}
			).insert(ignore_permissions=True).name
		so = preorder.create_preorder({"customer": customer, "items": [{"item_code": item, "qty": 1, "rate": 10}]})[
			"sales_order"]
		frappe.db.commit()   # the other session must see the order (deleted in the finally below)
		site = frappe.local.site
		try:
			with patch.object(frappe.db, "commit"):
				frappe.db.get_value("Sales Order", so, "modified")   # this transaction's snapshot starts here

				def touch():
					frappe.init(site=site, sites_path=frappe.local.sites_path if hasattr(frappe.local, "sites_path") else ".")
					frappe.connect()
					try:
						frappe.db.sql("UPDATE `tabSales Order` SET modified = NOW(6) WHERE name = %s", so)
						frappe.db.commit()
					finally:
						frappe.destroy()

				t = threading.Thread(target=touch)
				t.start()
				t.join(timeout=60)
				from ffl_core.tests.pos_opening import todays_pos_opening
				company = frappe.db.get_value("Sales Order", so, "company")
				profile = frappe.db.get_value("POS Profile", {"company": company, "disabled": 0}, "name")
				todays_pos_opening(company, profile)
				pay = preorder.open_payment(so, 10)["pos_invoice"]
				with self.assertRaisesRegex(frappe.ValidationError, "Try again"):
					_checkout(pay)
		finally:
			frappe.db.rollback()
			doc = frappe.get_doc("Sales Order", so)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc("Sales Order", so, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", customer, force=True, ignore_permissions=True)
			frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
			frappe.db.commit()
