"""Cash drawer: the daily count, the entries that take cash out, the weekly report.

Every behaviour is run twice, on two stand-in companies that look like the two stores:

* Store A-like: ``Operating Bank`` already exists and the company has no default bank account;
* Store B-like: no bank account at all.

Accounts are resolved from each company's abbr, so a hard-coded ``- TSA`` shows up here as a failure.
The acceptance assertions A1-A10 are named in the test docstrings. Nothing here commits: the whole
class is one transaction (ERPNext's own merge of a POS closing commits only outside tests).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.api import cash_drawer as api

SPECS = {"store_a": ("_Test Cash Store A Co", "TSA"), "store_b": ("_Test Cash Store B Co", "TSB")}
DEFAULTS = ("Print and Stationery", "Office Maintenance Expenses", "Freight and Forwarding Charges",
	"Postal Expenses", "Meals", "Entertainment Expenses", "Miscellaneous Expenses")


def _user(email, *roles):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)
	return email


def _account(company, abbr, name, parent, account_type=""):
	full = f"{name} - {abbr}"
	if not frappe.db.exists("Account", full):
		frappe.get_doc({
			"doctype": "Account", "account_name": name, "company": company, "is_group": 0,
			"parent_account": f"{parent} - {abbr}", "account_type": account_type,
		}).insert(ignore_permissions=True)
	return full


class CashFixtures(FrappeTestCase):
	KIND = "store_a"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.name, cls.abbr = SPECS[cls.KIND]
		if not frappe.db.exists("Company", cls.name):
			frappe.get_doc({
				"doctype": "Company", "company_name": cls.name, "abbr": cls.abbr,
				"default_currency": "USD", "country": "United States",
			}).insert(ignore_permissions=True)
		cls.company = cls.name
		cls.cash = f"Cash - {cls.abbr}"
		cls.sales = f"Sales - {cls.abbr}"
		if cls.KIND == "store_a":
			# What prod had before this feature: the bank account is there, the company does not point at it.
			_account(cls.company, cls.abbr, "Operating Bank", "Bank Accounts", "Bank")
			frappe.db.set_value("Company", cls.company, "default_bank_account", None)
		cls.stp = _account(cls.company, cls.abbr, "Sales Tax Payable", "Duties and Taxes", "Tax")
		cls.clerk = _user(f"cd-clerk-{cls.KIND}@example.com", "Sales User")
		cls.manager = _user(f"cd-manager-{cls.KIND}@example.com", "Sales Manager")
		cls.nobody = _user(f"cd-nobody-{cls.KIND}@example.com")
		if not frappe.db.exists("Customer", "_Test CD Customer"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test CD Customer",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		# The class is one transaction, rolled back at the end: give every test its own savepoint so the
		# books of one test are not the opening balance of the next.
		frappe.db.savepoint("cash_drawer_case")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="cash_drawer_case")
		super().tearDown()

	# --- helpers ------------------------------------------------------------------------------

	def _je(self, lines, remark="TEST"):
		return cd._post_je(self.company, lines, remark)

	def _sale(self, amount):
		"""Cash taken in a sale (stand-in for a merged Sales Invoice: Dr Cash / Cr Sales)."""
		return self._je([(self.cash, amount, 0), (self.sales, 0, amount)], "TEST cash sale")

	def _expected(self):
		return cd.drawer_expected(self.company).expected

	def _gl_cash(self):
		return flt(frappe.db.sql(
			"SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE company = %s AND account = %s AND is_cancelled = 0",
			(self.company, self.cash))[0][0], 2)

	def _lines(self, je):
		out = {}
		for r in frappe.db.sql(
				"SELECT account, debit, credit FROM `tabGL Entry` WHERE voucher_no = %s AND is_cancelled = 0", je, as_dict=True):
			d, c = out.get(r.account, (0, 0))
			out[r.account] = (flt(d + r.debit, 2), flt(c + r.credit, 2))
		return out

	def _jes(self, prefix):
		return frappe.get_all("Journal Entry", filters={
			"company": self.company, "docstatus": 1, "user_remark": ["like", f"{prefix}%"]}, pluck="name")

	def _receipt(self):
		"""A private receipt photo uploaded by whoever is the session user right now."""
		f = frappe.get_doc({"doctype": "File", "file_name": f"receipt-{frappe.generate_hash(length=6)}.txt",
			"content": frappe.generate_hash(length=24), "is_private": 1}).insert(ignore_permissions=True)
		return f.file_url

	def _next_day(self):
		"""Time passes: everything this company has done so far moves back one day."""
		frappe.db.sql(
			"UPDATE `tabCash Drawer Close` SET active_key = IF(status = 'Closed', CONCAT(company, '|', DATE_SUB(business_date, INTERVAL 1 DAY)), NULL), "
			"business_date = DATE_SUB(business_date, INTERVAL 1 DAY) WHERE company = %s ORDER BY business_date ASC", self.company)
		frappe.db.sql("UPDATE `tabGL Entry` SET posting_date = DATE_SUB(posting_date, INTERVAL 1 DAY) WHERE company = %s", self.company)
		for table in ("Journal Entry", "Payment Entry"):
			frappe.db.sql(f"UPDATE `tab{table}` SET posting_date = DATE_SUB(posting_date, INTERVAL 1 DAY) WHERE company = %s", self.company)

	def _protected(self):
		"""Rows and net of the accounts a cash count must never touch."""
		names = [f"{n} - {self.abbr}" for n in ("Sales", "Sales Tax Payable", "Debtors", "Cost of Goods Sold", "Stock In Hand")]
		rows = frappe.db.sql(
			"SELECT account, COUNT(*), IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE company = %s AND account IN %s GROUP BY account",
			(self.company, tuple(names)))
		return {r[0]: (r[1], flt(r[2], 2)) for r in rows}

	def _close(self, counted, **kw):
		frappe.set_user(self.clerk)
		try:
			return api.close_day(self.company, counted, **kw)
		finally:
			frappe.set_user("Administrator")

	def _report(self, days=1):
		from ffl_core.ffl_core.report.cash_drawer_weekly.cash_drawer_weekly import execute
		return execute({"company": self.company, "from_date": add_days(nowdate(), -days), "to_date": nowdate()})

	def _first_count(self, books=1000, counted=120):
		self._sale(books)
		return self._close(counted)


class Cases:
	# ------------------------------------------------------------------ accounts (A6, A7, A7b)

	def test_accounts_are_made_from_the_company_abbr(self):
		"""A6/A7: the three accounts exist in the right place on both stores, the company points at its bank."""
		made = cd.ensure_accounts(self.company)
		a = self.abbr
		self.assertEqual({k: made[k] for k in ("over_short", "bank")}, {
			"over_short": f"Cash Over/Short - {a}", "bank": f"Operating Bank - {a}"})
		over_short = frappe.db.get_value("Account", made["over_short"], ["root_type", "parent_account", "is_group", "account_type"], as_dict=True)
		self.assertEqual((over_short.root_type, over_short.parent_account, over_short.is_group, over_short.account_type or ""),
			("Expense", f"Indirect Expenses - {a}", 0, ""))
		bank = frappe.db.get_value("Account", made["bank"], ["account_type", "parent_account", "is_group"], as_dict=True)
		self.assertEqual((bank.account_type, bank.parent_account, bank.is_group), ("Bank", f"Bank Accounts - {a}", 0))
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), made["bank"])
		self.assertEqual(cd.ensure_accounts(self.company), made)  # idempotent

	def test_setup_ensure_covers_every_company(self):
		from ffl_core.patches.ensure_cash_drawer_accounts import execute
		execute()
		self.assertTrue(frappe.db.exists("Account", f"Cash Over/Short - {self.abbr}"))
		self.assertTrue(frappe.db.exists("Custom Field", "POS Closing Entry-ffl_cash_counted"))

	def test_the_default_expense_accounts_resolve(self):
		cd.ensure_accounts(self.company)  # makes Meals; the rest come with the chart of accounts
		self.assertEqual(frappe.db.get_value("Account", f"Meals - {self.abbr}", "parent_account"),
			f"Indirect Expenses - {self.abbr}")
		self.assertEqual(cd.expense_account_list(self.company), [f"{n} - {self.abbr}" for n in DEFAULTS])

	# ------------------------------------------------------------------ deposit (A6)

	def test_deposit_is_bank_against_cash(self):
		self._sale(500)
		before = self._expected()
		frappe.set_user(self.clerk)
		r = api.record_deposit(self.company, 200, "SLIP-9")
		frappe.set_user("Administrator")
		je = frappe.get_doc("Journal Entry", r["journal_entry"])
		self.assertEqual(self._lines(je.name), {f"Operating Bank - {self.abbr}": (200, 0), self.cash: (0, 200)})
		self.assertEqual(str(je.posting_date), nowdate())
		self.assertTrue(je.user_remark.startswith("CASH-DEPOSIT SLIP-9"))
		self.assertEqual(self._expected(), before - 200)
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), f"Operating Bank - {self.abbr}")

	def test_deposit_cannot_go_to_a_processor_account(self):
		cd.ensure_accounts(self.company)
		for name in ("Payroc Clearing", "Woo Clearing", "GunBroker Clearing"):
			account = _account(self.company, self.abbr, name, "Bank Accounts", "Bank")
			with self.assertRaises(frappe.ValidationError):
				cd.record_deposit(self.company, 10, None, account)
		with self.assertRaises(frappe.ValidationError):
			cd.record_deposit(self.company, 10, None, self.cash)

	def test_deposit_needs_a_positive_amount(self):
		for bad in (0, -5, "abc", None, float("nan")):
			with self.assertRaises(frappe.ValidationError):
				cd.record_deposit(self.company, bad)

	# ------------------------------------------------------------------ cash from the bank

	def test_cash_from_the_bank_is_cash_against_bank(self):
		self._sale(100)
		before = self._expected()
		frappe.set_user(self.clerk)
		r = api.record_from_bank(self.company, 300, "W-7")
		frappe.set_user("Administrator")
		self.assertEqual(self._lines(r["journal_entry"]), {self.cash: (300, 0), f"Operating Bank - {self.abbr}": (0, 300)})
		self.assertTrue(frappe.db.get_value("Journal Entry", r["journal_entry"], "user_remark").startswith("CASH-FROMBANK W-7"))
		self.assertEqual(self._expected(), before + 300)
		for bad in (0, -5, "abc", None):
			with self.assertRaises(frappe.ValidationError):
				cd.record_from_bank(self.company, bad)

	def test_cash_from_the_bank_never_comes_from_a_processor_or_cash(self):
		cd.ensure_accounts(self.company)
		clearing = _account(self.company, self.abbr, "Payroc Clearing", "Bank Accounts", "Bank")
		for account in (clearing, self.cash):
			doc = frappe.new_doc("Cash Drawer Entry")
			doc.update({"entry_type": "From Bank", "company": self.company, "amount": 10, "credit_account": account})
			with self.assertRaises(frappe.ValidationError):
				doc.insert(ignore_permissions=True)

	def test_owner_draws_are_gone(self):
		self.assertFalse(hasattr(api, "record_owner_draw"))
		doc = frappe.new_doc("Cash Drawer Entry")
		doc.update({"entry_type": "Owner Draw", "company": self.company, "amount": 10})
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_an_old_owner_draw_still_reads_as_one(self):
		self._sale(100)
		cd.ensure_accounts(self.company)
		je = cd._post_je(self.company, [(f"Cash Over/Short - {self.abbr}", 40, 0), (self.cash, 0, 40)], "CASH-OWNER Owner weekly")
		log = api.get_log(self.company, nowdate(), nowdate())
		self.assertEqual([(l["kind"], l["voucher_no"]) for l in log["lines"] if l["amount"] == -40], [("owner", je)])
		columns, _rows = self._report()
		self.assertIn("owner", [c["fieldname"] for c in columns])

	# ------------------------------------------------------------------ the log

	def test_the_log_lists_every_movement_with_a_running_balance(self):
		self._first_count()          # books 1000 -> counted 120 (cutoff)
		self._next_day()
		self._sale(100)
		frappe.set_user(self.clerk)
		cd.record_deposit(self.company, 30, "S1")
		cd.record_from_bank(self.company, 50, "W1")
		log = api.get_log(self.company, add_days(nowdate(), -5), nowdate())
		frappe.set_user("Administrator")
		self.assertEqual(log["opening"], 0)
		self.assertEqual([l["kind"] for l in log["lines"]][-3:], ["unclassified", "deposit", "from_bank"])
		self.assertEqual([l["balance"] for l in log["lines"]][-3:], [220, 190, 240])
		self.assertEqual(log["closing"], self._expected())
		self.assertEqual((log["cash_in"], log["cash_out"]), (1150, 30))  # the cutoff is neither in nor out
		self.assertEqual(log["lines"][-1]["by"], self.clerk)
		self.assertEqual(log["lines"][-1]["detail"], "W1")
		# a later window starts from the balance before it
		later = api.get_log(self.company, nowdate(), nowdate())
		self.assertEqual(later["opening"], 120)
		self.assertEqual(later["closing"], log["closing"])
		with self.assertRaises(frappe.ValidationError):
			api.get_log(self.company, nowdate(), add_days(nowdate(), -1))
		accountant = _user("cd-accountant@example.com", "Accounts User")
		frappe.set_user(accountant)
		self.assertEqual(api.get_log(self.company, nowdate(), nowdate())["closing"], log["closing"])
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ small expenses (A7b)

	def test_expense_needs_a_memo_and_the_receipt_is_optional(self):
		cd.ensure_accounts(self.company)  # makes Meals
		acct = f"Meals - {self.abbr}"
		r = cd.record_expense(self.company, 10, acct, "lunch", "")
		self.assertIn(acct, self._lines(r.journal_entry))
		self.assertIsNone(frappe.db.get_value("Cash Drawer Entry", r.entry, "receipt"))
		self.assertTrue(cd.record_expense(self.company, 10, acct, "lunch again").entry)  # no receipt twice is fine
		with self.assertRaises(frappe.ValidationError):  # a receipt given must still be a real, fresh upload
			cd.record_expense(self.company, 10, acct, "paper", "/private/files/not-uploaded.png")
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, acct, "  ", self._receipt())

	def test_one_receipt_cannot_back_two_expenses(self):
		acct = f"Print and Stationery - {self.abbr}"
		receipt = self._receipt()
		made = cd.record_expense(self.company, 10, acct, "paper", receipt)
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, acct, "paper again", receipt)
		cd.void_entry(made.entry)  # undone, but the photo stays with that entry: a new purchase needs its own
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, acct, "paper", receipt)
		self.assertTrue(cd.record_expense(self.company, 10, acct, "paper", self._receipt()).entry)

	def test_expense_is_capped_and_the_cap_is_a_setting(self):
		acct = f"Print and Stationery - {self.abbr}"
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 200.01, acct, "paper", self._receipt())
		self.assertTrue(cd.record_expense(self.company, 200, acct, "paper", self._receipt()).entry)
		frappe.db.set_single_value("Cash Drawer Settings", "expense_cap", 50)
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 60, acct, "paper", self._receipt())

	def test_expense_account_must_be_on_the_list_and_not_forbidden(self):
		cd.ensure_accounts(self.company)
		for name in ("Cost of Goods Sold", "Stock Adjustment", "Tax Expense", "Salary", "Write Off", "Round Off",
				"Sales Tax Payable", "Telephone Expenses", "Cash Over/Short"):
			with self.assertRaises(frappe.ValidationError, msg=name):
				cd.record_expense(self.company, 10, f"{name} - {self.abbr}", "x", self._receipt())

	def test_a_forbidden_account_cannot_be_put_on_the_list(self):
		settings = frappe.get_doc("Cash Drawer Settings")
		settings.append("expense_accounts", {"account": f"Cost of Goods Sold - {self.abbr}"})
		with self.assertRaises(frappe.ValidationError):
			settings.save()

	def test_the_shops_own_list_replaces_the_defaults(self):
		settings = frappe.get_doc("Cash Drawer Settings")
		settings.append("expense_accounts", {"account": f"Telephone Expenses - {self.abbr}"})
		settings.save()
		self.assertEqual(cd.expense_account_list(self.company), [f"Telephone Expenses - {self.abbr}"])
		self.assertTrue(cd.record_expense(self.company, 10, f"Telephone Expenses - {self.abbr}", "SIM", self._receipt()).entry)
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, f"Print and Stationery - {self.abbr}", "paper", self._receipt())

	def test_expense_posts_the_gross_to_one_expense_account(self):
		"""A7b: tax paid on a purchase is part of the expense; Sales Tax Payable and Tax Expense stay untouched."""
		tax_expense = _account(self.company, self.abbr, "Tax Expense", "Indirect Expenses")
		self._sale(300)
		before = self._expected()
		frappe.set_user(self.clerk)
		receipt = self._receipt()
		r = api.record_expense(self.company, 43.30, f"Print and Stationery - {self.abbr}", "Printer paper", receipt)
		frappe.set_user("Administrator")
		self.assertEqual(self._lines(r["journal_entry"]), {f"Print and Stationery - {self.abbr}": (43.3, 0), self.cash: (0, 43.3)})
		self.assertEqual(frappe.db.count("GL Entry", {"account": ["in", [self.stp, tax_expense]]}), 0)
		self.assertTrue(frappe.db.get_value("Journal Entry", r["journal_entry"], "user_remark").startswith("CASH-EXPENSE"))
		self.assertEqual(self._expected(), flt(before - 43.30, 2))
		self.assertEqual(frappe.db.get_value("File", {"file_url": receipt}, ["attached_to_doctype", "attached_to_name", "is_private"]),
			("Cash Drawer Entry", r["entry"], 1))
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", r["entry"], "receipt"), receipt)

	def test_every_default_account_takes_an_expense(self):
		for n in DEFAULTS:
			r = cd.record_expense(self.company, 10, f"{n} - {self.abbr}", n, self._receipt())
			self.assertIn(f"{n} - {self.abbr}", self._lines(r.journal_entry))


	# ------------------------------------------------------------------ receipt files

	def test_a_receipt_must_be_the_callers_own_fresh_unattached_upload(self):
		acct = f"Print and Stationery - {self.abbr}"
		mine = self._receipt()
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.ValidationError):  # Administrator's file, not the clerk's
			cd.record_expense(self.company, 10, acct, "paper", mine)
		frappe.set_user("Administrator")
		stale = self._receipt()
		frappe.db.set_value("File", {"file_url": stale}, "creation", add_days(nowdate(), -3), update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, acct, "paper", stale)
		attached = self._receipt()
		frappe.db.set_value("File", {"file_url": attached}, {"attached_to_doctype": "Customer", "attached_to_name": "_Test CD Customer"})
		with self.assertRaises(frappe.ValidationError):
			cd.record_expense(self.company, 10, acct, "paper", attached)
		self.assertTrue(cd.record_expense(self.company, 10, acct, "paper", mine).entry)

	def test_only_the_one_file_is_attached_and_it_ends_up_private(self):
		acct = f"Print and Stationery - {self.abbr}"
		f = frappe.get_doc({"doctype": "File", "file_name": f"pub-{frappe.generate_hash(length=6)}.txt",
			"content": frappe.generate_hash(length=24), "is_private": 0}).insert(ignore_permissions=True)
		bystander = frappe.get_doc({"doctype": "File", "file_name": "same.txt", "file_url": f.file_url,
			"attached_to_doctype": "Customer", "attached_to_name": "_Test CD Customer"}).insert(ignore_permissions=True)
		made = cd.record_expense(self.company, 10, acct, "paper", f.file_url)
		entry = frappe.get_doc("Cash Drawer Entry", made.entry)
		mine = frappe.get_doc("File", f.name)
		self.assertEqual((mine.is_private, mine.attached_to_name), (1, made.entry))
		self.assertTrue(entry.receipt.startswith("/private/files/"))
		self.assertEqual(entry.receipt, mine.file_url)
		self.assertEqual(frappe.db.get_value("File", bystander.name, "attached_to_doctype"), "Customer")

	# ------------------------------------------------------------------ guards

	def test_a_posted_entry_cannot_be_edited_in_any_field(self):
		made = cd.record_expense(self.company, 10, f"Postal Expenses - {self.abbr}", "first memo", self._receipt())
		for field, value in (("memo", "changed memo"), ("reference", "x"), ("posting_date", add_days(nowdate(), -2)),
				("company", "FFL Test"), ("entry_type", "Deposit"), ("journal_entry", "x"), ("amount", 11), ("serial", "x"),
				("receipt", "/private/files/x.png"), ("acquisition", "x"), ("debit_account", self.cash)):
			doc = frappe.get_doc("Cash Drawer Entry", made.entry)
			doc.set(field, value)
			with self.assertRaises(frappe.ValidationError, msg=field):
				doc.save()
		doc = frappe.get_doc("Cash Drawer Entry", made.entry)
		doc.save()  # untouched: fine

	def test_the_database_allows_one_live_close_per_company_and_day(self):
		first = self._first_count()
		clash = frappe.get_doc({"doctype": "Cash Drawer Close", "company": self.company, "business_date": nowdate(),
			"status": "Closed", "is_counted": 1, "counted": 1, "expected": 1})
		clash.flags.ignore_permissions = True
		with self.assertRaises(frappe.UniqueValidationError):
			clash.insert()
		cd.undo_close(first["close"])  # an undone close frees the day
		again = self._close(120)
		self.assertTrue(again["close"])
		self.assertEqual(frappe.db.count("Cash Drawer Close", {"company": self.company, "business_date": nowdate()}), 2)

	def test_the_limits_in_settings_have_minimums(self):
		for field, bad in (("variance_threshold", 0), ("variance_threshold", -1), ("expense_cap", 0.5), ("expense_cap", -5)):
			settings = frappe.get_doc("Cash Drawer Settings")
			settings.update({"variance_threshold": 20, "expense_cap": 200, field: bad})
			with self.assertRaises(frappe.ValidationError, msg=f"{field}={bad}"):
				settings.save()
		settings = frappe.get_doc("Cash Drawer Settings")
		settings.update({"variance_threshold": 0.01, "expense_cap": 1})
		settings.save()

	# ------------------------------------------------------------------ the bank account

	def test_an_existing_bank_account_is_used_not_a_second_one(self):
		bank = _account(self.company, self.abbr, "Main Checking", "Bank Accounts", "Bank")
		frappe.db.set_value("Company", self.company, "default_bank_account", bank)
		made = cd.ensure_accounts(self.company)
		self.assertEqual(made["bank"], bank)
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), bank)
		r = cd.record_deposit(self.company, 5)
		self.assertIn(bank, self._lines(r.journal_entry))

	def test_a_clearing_account_is_never_the_bank_side(self):
		clearing = _account(self.company, self.abbr, "Payroc Card Clearing", "Bank Accounts", "Bank")
		frappe.db.set_value("Company", self.company, "default_bank_account", clearing)
		made = cd.ensure_accounts(self.company)
		self.assertEqual(made["bank"], f"Operating Bank - {self.abbr}")
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), clearing)  # left as found
		lines = self._lines(cd.record_deposit(self.company, 5).journal_entry)
		self.assertIn(f"Operating Bank - {self.abbr}", lines)
		self.assertNotIn(clearing, lines)


	# ------------------------------------------------------------------ one bank, read-only page

	def _zelle_maps_to(self, account):
		mop = frappe.get_doc("Mode of Payment", "Zelle")
		mop.accounts = [r for r in mop.accounts if r.company != self.company]
		mop.append("accounts", {"company": self.company, "default_account": account})
		mop.save(ignore_permissions=True)

	def test_the_bank_is_chosen_in_a_fixed_order(self):
		abbr = self.abbr
		frappe.db.set_value("Company", self.company, "default_bank_account", None)
		frappe.db.delete("Account", {"name": f"Operating Bank - {abbr}"})
		self.assertIsNone(cd.resolve_bank(self.company))
		operating = _account(self.company, abbr, "Operating Bank", "Bank Accounts", "Bank")
		self.assertEqual(cd.resolve_bank(self.company), operating)  # an existing Operating Bank
		mapped = _account(self.company, abbr, "Main Checking", "Bank Accounts", "Bank")
		self._zelle_maps_to(mapped)
		self.assertEqual(cd.resolve_bank(self.company), mapped)  # the account Zelle settles to wins over it
		own = _account(self.company, abbr, "Woodforest Checking", "Bank Accounts", "Bank")
		frappe.db.set_value("Company", self.company, "default_bank_account", own)
		self.assertEqual(cd.resolve_bank(self.company), own)  # a usable default wins over everything

	def test_the_prod_shape_resolves_to_operating_bank(self):
		"""Store A: Operating Bank exists and Zelle settles to it, the company's default bank account is empty."""
		operating = _account(self.company, self.abbr, "Operating Bank", "Bank Accounts", "Bank")
		self._zelle_maps_to(operating)
		frappe.db.set_value("Company", self.company, "default_bank_account", None)
		made = cd.ensure_accounts(self.company, create_bank=False)
		self.assertEqual(made["bank"], operating)
		self.assertEqual(frappe.db.get_value("Company", self.company, "default_bank_account"), operating)

	def test_a_migrate_on_a_new_site_leaves_the_bank_to_store_setup(self):
		frappe.db.set_value("Company", self.company, "default_bank_account", None)
		frappe.db.delete("Account", {"name": f"Operating Bank - {self.abbr}"})
		made = cd.ensure_accounts(self.company, create_bank=False)
		self.assertIsNone(made["bank"])
		self.assertFalse(frappe.db.exists("Account", f"Operating Bank - {self.abbr}"))
		self.assertFalse(frappe.db.get_value("Company", self.company, "default_bank_account"))
		self.assertEqual(cd.ensure_accounts(self.company)["bank"], f"Operating Bank - {self.abbr}")  # the first cash entry makes it

	def test_the_page_load_changes_nothing(self):
		frappe.db.set_value("Company", self.company, "default_bank_account", None)
		frappe.db.delete("Account", {"name": f"Operating Bank - {self.abbr}"})
		before = frappe.db.count("Account", {"company": self.company})
		frappe.set_user(self.clerk)
		page = api.get_today(self.company)
		frappe.set_user("Administrator")
		self.assertIsNone(page["bank_account"])
		self.assertEqual(frappe.db.count("Account", {"company": self.company}), before)
		self.assertFalse(frappe.db.get_value("Company", self.company, "default_bank_account"))

	def test_clearing_accounts_are_matched_by_word(self):
		abbr = self.abbr
		for name in ("Woodforest Checking", "Payroll Checking", "Woodlands Savings"):
			account = _account(self.company, abbr, name, "Bank Accounts", "Bank")
			self.assertIsNone(cd._bank_problem(account, self.company), name)
		for name in ("WooCommerce Clearing", "Woo Clearing", "Payroc Card Clearing", "GunBroker Clearing", "Card Clearing"):
			account = _account(self.company, abbr, name, "Bank Accounts", "Bank")
			self.assertIsNotNone(cd._bank_problem(account, self.company), name)

	# ------------------------------------------------------------------ the page: what it lists

	def test_the_page_lists_today_and_says_how_much_is_older(self):
		self._sale(700)
		frappe.db.sql("UPDATE `tabGL Entry` SET creation = DATE_SUB(creation, INTERVAL 3 DAY) WHERE company = %s", self.company)
		self._sale(40)
		frappe.set_user(self.manager)
		page = api.get_today(self.company)
		frappe.set_user("Administrator")
		self.assertEqual([l["amount"] for l in page["lines"]], [40.0])
		self.assertEqual((page["older_lines"], page["older_net"], page["other"], page["expected"]), (1, 700.0, 0.0, 740.0))

	def test_the_page_totals_cash_in_and_out_like_the_log(self):
		"""get_today's cash_in / cash_out (the Cash Drawer page and the Overview show them) use get_log's rule."""
		self._sale(500)
		frappe.set_user(self.clerk)
		api.record_deposit(self.company, 200, "SLIP-IO")
		frappe.set_user(self.manager)
		page = api.get_today(self.company)
		log = api.get_log(self.company, nowdate(), nowdate())
		frappe.set_user("Administrator")
		self.assertEqual((page["cash_in"], page["cash_out"]), (500, 200))
		self.assertEqual((page["cash_in"], page["cash_out"]), (log["cash_in"], log["cash_out"]))
		# a count difference and the first count are neither in nor out
		lines = [{"kind": "sale_pos", "amount": 100}, {"kind": "payment_in", "amount": 250.5},
			{"kind": "deposit", "amount": -100}, {"kind": "expense", "amount": -43.3},
			{"kind": "variance", "amount": -8.91}, {"kind": "cutoff", "amount": -880}, {"kind": "cutoff_trueup", "amount": -300}]
		self.assertEqual(api._in_out(lines), (350.5, 143.3))
		self.assertEqual(api._in_out([]), (0, 0))

	def test_a_register_return_that_has_not_merged_is_a_refund(self):
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"CD-RET-{frappe.generate_hash(length=6)}", "company": self.company, "docstatus": 1,
			"posting_date": nowdate(), "posting_time": "10:00:00", "is_return": 1, "customer": "_Test CD Customer"})
		inv.db_insert()
		pay = frappe.new_doc("Sales Invoice Payment")
		pay.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice", "parentfield": "payments",
			"idx": 1, "docstatus": 1, "mode_of_payment": "Cash", "account": self.cash, "amount": -50})
		pay.db_insert()
		frappe.set_user(self.manager)
		page = api.get_today(self.company)
		frappe.set_user("Administrator")
		self.assertEqual([(l["kind"], l["amount"]) for l in page["lines"]], [("refund", -50.0)])
		self.assertEqual(page["expected"], -50.0)

	# ------------------------------------------------------------------ the count (A3, A8)

	def test_the_first_count_sets_the_books_once(self):
		"""A8: one CASH-CUTOFF, amount = ledger balance - counted, against the bank; never a loss; not again."""
		r = self._first_count(books=1000, counted=120)
		self.assertTrue(r["first_count"])
		self.assertIsNone(r["variance_je"])
		self.assertEqual(self._lines(r["cutoff_je"]), {f"Operating Bank - {self.abbr}": (880, 0), self.cash: (0, 880)})
		self.assertTrue(frappe.db.get_value("Journal Entry", r["cutoff_je"], "user_remark").startswith("CASH-CUTOFF"))
		self.assertEqual(self._gl_cash(), 120)
		self.assertEqual(frappe.db.count("GL Entry", {"account": f"Cash Over/Short - {self.abbr}"}), 0)
		close = frappe.get_doc("Cash Drawer Close", r["close"])
		self.assertEqual((close.counted, close.expected, close.variance, close.is_cutoff, close.carry_forward),
			(120, 1000, -880, 1, 120))
		self._next_day()
		self._sale(100)
		second = self._close(220)
		self.assertFalse(second["first_count"])
		self.assertIsNone(second["cutoff_je"])
		self.assertEqual(len(self._jes("CASH-CUTOFF")), 1)

	def test_a_first_count_above_the_books_runs_the_other_way(self):
		self._sale(50)
		r = self._close(70)
		self.assertEqual(self._lines(r["cutoff_je"]), {self.cash: (20, 0), f"Operating Bank - {self.abbr}": (0, 20)})

	def test_a_short_count_is_booked_and_carried_forward(self):
		"""A3: exactly one CASH-VARIANCE, Over/Short against Cash, GL = counted = tomorrow's opening."""
		first = self._first_count()
		self._next_day()
		self._sale(300)
		self.assertEqual(self._expected(), 420)
		r = self._close(411.09)  # 8.91 short, under the reason limit
		self.assertEqual(r["variance"], -8.91)
		self.assertEqual(self._lines(r["variance_je"]), {f"Cash Over/Short - {self.abbr}": (8.91, 0), self.cash: (0, 8.91)})
		self.assertTrue(frappe.db.get_value("Journal Entry", r["variance_je"], "user_remark").startswith("CASH-VARIANCE"))
		self.assertEqual(len(self._jes("CASH-VARIANCE")), 1)
		self.assertEqual(self._gl_cash(), 411.09)
		close = frappe.get_doc("Cash Drawer Close", r["close"])
		self.assertEqual((close.carry_forward, close.opening_amount, close.variance_je), (411.09, 120, r["variance_je"]))
		self.assertEqual(frappe.get_doc("Cash Drawer Close", first["close"]).carry_forward, 120)

	def test_an_over_count_and_an_exact_count(self):
		self._first_count()
		self._next_day()
		self._sale(80)
		over = self._close(205)  # expected 200, 5 over
		self.assertEqual(self._lines(over["variance_je"]), {self.cash: (5, 0), f"Cash Over/Short - {self.abbr}": (0, 5)})
		self._next_day()
		self._sale(10)
		exact = self._close(215)
		self.assertIsNone(exact["variance_je"])
		self.assertEqual(len(self._jes("CASH-VARIANCE")), 1)

	def test_a_big_difference_needs_a_reason_but_is_never_blocked(self):
		self._first_count()
		self._next_day()
		self._sale(100)  # expected 220
		with self.assertRaises(frappe.ValidationError):
			self._close(200)  # exactly 20 short
		just_under = self._close(200.01)
		self.assertEqual(just_under["variance"], -19.99)  # under the limit: no reason asked
		self._next_day()
		self._sale(50)  # expected 250.01 -> 270.01 with the next sale below
		self.assertEqual(self._expected(), 250.01)
		r = self._close(100, reason="drawer short after the gun show")
		self.assertEqual(r["variance"], -150.01)
		self.assertTrue(r["variance_je"])
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", r["close"], "reason"), "drawer short after the gun show")

	def test_the_first_count_needs_no_reason(self):
		self._sale(5000)
		self.assertTrue(self._close(0)["cutoff_je"])  # a count of zero is a count

	def test_the_screen_figure_must_still_be_true(self):
		self._first_count()
		self._next_day()
		self._sale(100)
		with self.assertRaises(frappe.ValidationError):
			self._close(220, expected_seen=219)
		self.assertTrue(self._close(220, expected_seen=220)["close"])

	def test_a_day_is_counted_once_and_a_manager_can_undo_it(self):
		first = self._first_count()
		with self.assertRaises(frappe.ValidationError):
			self._close(130)
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.PermissionError):
			api.undo_close(first["close"])
		frappe.set_user(self.manager)
		api.undo_close(first["close"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Journal Entry", first["cutoff_je"], "docstatus"), 2)
		self.assertEqual(self._gl_cash(), 1000)
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", first["close"], "status"), "Undone")
		again = self._close(130)
		self.assertTrue(again["first_count"])  # the cut-off is still owed
		self.assertEqual(len(self._jes("CASH-CUTOFF")), 1)  # the undone one is cancelled; one is live

	def test_only_the_latest_count_can_be_undone(self):
		first = self._first_count()
		self._next_day()
		self._sale(10)
		self._close(130)
		with self.assertRaises(frappe.ValidationError):
			cd.undo_close(first["close"])

	def test_a_night_nobody_counted_is_absorbed_by_the_next_count(self):
		"""A4/A1: the uncounted day books nothing and carries the ledger balance; the next count covers it."""
		self._first_count()
		self._next_day()
		self._sale(1080)  # books 1200
		night = cd.ensure_uncounted_close(self.company)
		row = frappe.get_doc("Cash Drawer Close", night)
		self.assertEqual((row.is_counted, row.counted, row.expected, row.carry_forward, row.opening_amount), (0, 0, 1200, 1200, 120))
		self.assertFalse(row.variance_je)
		self.assertIsNone(cd.ensure_uncounted_close(self.company))  # once a day
		self._next_day()
		self._sale(300)  # books 1500
		r = self._close(1150, reason="counted late")
		self.assertEqual(r["variance"], -350)
		self.assertEqual((r["days_covered"], str(r["covers_from"])), (2, add_days(nowdate(), -1)))
		close = frappe.get_doc("Cash Drawer Close", r["close"])
		self.assertEqual(close.opening_amount, 1200)  # the uncounted day's ledger balance
		self.assertEqual(len(self._jes("CASH-VARIANCE")), 1)

	def test_a_manual_count_replaces_a_nightly_uncounted_close_of_the_same_day(self):
		self._first_count()
		self._next_day()
		self._sale(10)
		night = cd.ensure_uncounted_close(self.company)
		r = self._close(130)
		self.assertEqual(r["close"], night)
		self.assertEqual(frappe.db.count("Cash Drawer Close", {"company": self.company, "business_date": nowdate(), "status": "Closed"}), 1)

	# ------------------------------------------------------------------ history before the first count (H1-H7)

	def _bank(self):
		return f"Operating Bank - {self.abbr}"

	def _old_payout(self, amount, days_ago=10):
		"""A cash payout booked for a day long before the first count (part of the books the first count saw)."""
		return cd._post_je(self.company, [(self.sales, amount, 0), (self.cash, 0, amount)], "TEST old cash payout",
			add_days(nowdate(), -days_ago))

	def _history(self):
		return cd.history_change(self.company, self.cash)

	def _page(self):
		frappe.set_user(self.manager)
		try:
			return api.get_today(self.company)
		finally:
			frappe.set_user("Administrator")

	def _history_corrected(self):
		"""First count of 120 on books of 1000, which carried an old 300 cash payout; the next day the accountant finds
		it was paid by Zelle and cancels it. The books go up 300, the drawer does not."""
		paid = self._old_payout(300)
		self._first_count(books=1300, counted=120)
		self._next_day()
		self.assertEqual(self._history(), 0)
		frappe.get_doc("Journal Entry", paid).cancel()
		return paid

	def test_a_history_correction_stays_out_of_the_drawer_and_the_next_count_books_it(self):
		"""B1/B2: expected leaves the 300 out (no Other line); the next count books one TRUE-UP dated the cut-off day,
		against the bank, then the books equal the count and nothing went to Over/Short."""
		paid = self._history_corrected()
		self.assertEqual(self._gl_cash(), 420)
		exp = cd.drawer_expected(self.company)
		self.assertEqual((exp.history, exp.expected), (300, 120))
		page = self._page()
		self.assertEqual((page["history"], page["other"], page["lines"], page["expected"]), (300, 0, [], 120))
		self._sale(50)
		r = self._close(170)
		self.assertEqual((r["variance"], r["variance_je"]), (0, None))
		self.assertEqual(self._lines(r["trueup_je"]), {self._bank(): (300, 0), self.cash: (0, 300)})
		je = frappe.db.get_value("Journal Entry", r["trueup_je"], ["posting_date", "user_remark"], as_dict=True)
		self.assertEqual(str(je.posting_date), add_days(nowdate(), -1))  # the cut-off day
		self.assertTrue(je.user_remark.startswith("CASH-CUTOFF TRUE-UP"))
		self.assertIn(f"{paid} +300.00", je.user_remark)
		self.assertEqual((self._gl_cash(), self._history()), (170, 0))
		self.assertEqual(frappe.db.count("GL Entry", {"account": f"Cash Over/Short - {self.abbr}"}), 0)
		self.assertEqual(frappe.db.get_value("Cash Drawer Close", r["close"], "trueup_je"), r["trueup_je"])
		self.assertEqual(frappe.db.get_value("Journal Entry", r["trueup_je"], "ffl_cash_drawer_source"), r["close"])
		self.assertEqual(len(self._jes("CASH-CUTOFF")), 2)  # the first count's entry and one true-up
		# the log names it and keeps it out of cash in / out
		frappe.set_user(self.manager)
		log = api.get_log(self.company, add_days(nowdate(), -1), nowdate())
		frappe.set_user("Administrator")
		line = next(l for l in log["lines"] if l["voucher_no"] == r["trueup_je"])
		self.assertEqual((line["kind"], line["label"]), ("cutoff_trueup", "Books corrected for days before the first count"))
		self.assertEqual((log["cash_in"], log["cash_out"]), (1350, 0))  # the two sales; the first count and true-up are neither
		# the weekly report: its own column and note, the count differences apart, every check at 0.00
		_cols, rows = self._report(days=2)
		self.assertTrue(any(r["trueup_je"] in str(n.get("note")) for n in rows if n.get("row_kind") == "note"))
		cut_day = next(x for x in rows if x.get("day") == add_days(nowdate(), -1))
		self.assertEqual((cut_day["first_count"], cut_day["count_adjust"]), (-1180, 0))
		self.assertIn(r["trueup_je"], cut_day["note"])
		self.assertEqual([x["closing_gl"] for x in rows if x.get("day") == "Difference"], [0.0, 0.0])

	def test_a_line_added_for_before_the_cut_off_runs_the_other_way(self):
		"""B3 + the report's foot: a forgotten old payout added after the first count lowers the books, not the drawer."""
		self._first_count()
		self._next_day()
		self._close(120)
		cd._post_je(self.company, [(self.sales, 40, 0), (self.cash, 0, 40)], "TEST forgotten payout", add_days(nowdate(), -5))
		self.assertEqual((self._history(), self._expected()), (-40, 120))
		self.assertEqual((self._page()["other"], self._page()["lines"]), (0, []))
		_cols, rows = self._report(days=1)
		self.assertTrue(any(x.get("day") == "History changed after the first count, not yet booked" and x["closing_gl"] == -40
			for x in rows))
		self.assertEqual([x["closing_gl"] for x in rows if x.get("day") == "Difference"], [0.0, 0.0])
		self._next_day()
		r = self._close(120)
		self.assertEqual(self._lines(r["trueup_je"]), {self.cash: (40, 0), self._bank(): (0, 40)})
		self.assertEqual((r["variance_je"], self._gl_cash(), self._history()), (None, 120, 0))

	def test_a_correction_after_the_cut_off_is_still_a_count_difference(self):
		"""B4/H5: a cash sale that was not cash, after the first count: short at one count, over at the next, no true-up."""
		self._first_count()
		self._next_day()
		sale = self._sale(100)
		short = self._close(120, reason="the sale was paid by card")
		self.assertEqual(short["variance"], -100)
		self._next_day()
		frappe.get_doc("Journal Entry", sale).cancel()
		self.assertEqual(self._history(), 0)
		over = self._close(120, reason="the card sale was fixed")
		self.assertEqual((over["variance"], over["trueup_je"]), (100, None))
		net = frappe.db.sql("SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE account = %s AND is_cancelled = 0",
			f"Cash Over/Short - {self.abbr}")[0][0]
		self.assertEqual(flt(net, 2), 0)

	def test_a_night_nobody_counted_keeps_the_correction_for_the_next_count(self):
		"""B7: the uncounted close carries the drawer (the books less the correction) and books nothing."""
		self._history_corrected()
		night = frappe.get_doc("Cash Drawer Close", cd.ensure_uncounted_close(self.company))
		self.assertEqual((night.expected, night.carry_forward, night.trueup_je), (120, 120, None))
		self._next_day()
		self.assertEqual(self._history(), 300)
		r = self._close(120)
		self.assertEqual((self._lines(r["trueup_je"])[self.cash], r["variance"]), ((0, 300), 0))

	def test_undoing_a_count_takes_its_true_up_back(self):
		"""B8."""
		self._history_corrected()
		r = self._close(120)
		frappe.set_user(self.manager)
		api.undo_close(r["close"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Journal Entry", r["trueup_je"], "docstatus"), 2)
		self.assertEqual(self._history(), 300)
		again = self._close(120)
		self.assertTrue(again["trueup_je"])
		self.assertEqual(self._history(), 0)

	def test_a_frozen_cut_off_day_moves_the_true_up_to_the_count_day(self):
		"""B11: the count is never blocked."""
		self._history_corrected()
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", add_days(nowdate(), -1))
		r = self._close(120)
		je = frappe.db.get_value("Journal Entry", r["trueup_je"], ["posting_date", "user_remark"], as_dict=True)
		self.assertEqual(str(je.posting_date), nowdate())
		self.assertIn("cut-off date is frozen", je.user_remark)
		self.assertFalse(any("frozen" in str(m) for m in frappe.message_log))  # an answer here, not a message for the counter

	def test_register_tickets_open_at_the_first_count_are_part_of_history(self):
		"""B6: a ticket the first count saw as pending is history whether or not it has merged since."""
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"CD-PEND-{frappe.generate_hash(length=6)}", "company": self.company, "docstatus": 1,
			"posting_date": nowdate(), "posting_time": "10:00:00", "customer": "_Test CD Customer"})
		inv.db_insert()
		pay = frappe.new_doc("Sales Invoice Payment")
		pay.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice", "parentfield": "payments",
			"idx": 1, "docstatus": 1, "mode_of_payment": "Cash", "account": self.cash, "amount": 50})
		pay.db_insert()
		self._first_count(books=1000, counted=1050)
		self.assertEqual(self._history(), 0)

	# ------------------------------------------------------------------ undo an entry

	def test_undoing_an_entry_puts_the_cash_back_until_a_count_relies_on_it(self):
		self._sale(500)
		dep = cd.record_deposit(self.company, 100)
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.PermissionError):
			api.undo_entry(dep.entry)
		frappe.set_user(self.manager)
		api.undo_entry(dep.entry)
		frappe.set_user("Administrator")
		self.assertEqual(self._expected(), 500)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", dep.entry, "status"), "Voided")
		with self.assertRaises(frappe.ValidationError):
			cd.void_entry(dep.entry)
		kept = cd.record_deposit(self.company, 50)
		self._close(450)
		with self.assertRaises(frappe.ValidationError):
			cd.void_entry(kept.entry)

	def test_an_entry_cannot_be_edited_or_deleted(self):
		dep = cd.record_deposit(self.company, 100)
		doc = frappe.get_doc("Cash Drawer Entry", dep.entry)
		doc.amount = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	# ------------------------------------------------------------------ gates

	def test_endpoints_are_gated(self):
		frappe.set_user(self.nobody)
		for fn, args in ((api.get_today, ()), (api.preview_close, (self.company, 1)), (api.close_day, (self.company, 1)),
				(api.record_deposit, (self.company, 1)), (api.record_from_bank, (self.company, 1)), (api.get_log, (self.company,)),
				(api.record_expense, (self.company, 1, "a", "m", "r")), (api.undo_entry, ("x",)), (api.undo_close, ("x",)),
				(api.record_payout, ("x", "Cash"))):
			with self.assertRaises(frappe.PermissionError, msg=fn.__name__):
				fn(*args)
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.PermissionError):
			api.undo_entry("x")

	def test_the_page_data(self):
		self._first_count()
		self._next_day()
		self._sale(100)
		cd.record_deposit(self.company, 30, "S1")
		frappe.set_user(self.manager)
		data = api.get_today(self.company)
		frappe.set_user("Administrator")
		self.assertEqual((data["expected"], data["opening"], data["other"]), (190, 120, 0))
		kinds = [(l["kind"], l["amount"]) for l in data["lines"]]
		self.assertEqual(sorted(kinds), [("deposit", -30.0), ("unclassified", 100.0)])
		self.assertTrue(any(l["can_undo"] for l in data["lines"] if l["kind"] == "deposit"))
		self.assertEqual([a["name"] for a in data["expense_accounts"]], [f"{n} - {self.abbr}" for n in DEFAULTS])
		self.assertTrue(data["can_undo_close"])
		self.assertEqual(data["bank_account"], f"Operating Bank - {self.abbr}")
		frappe.set_user(self.clerk)
		self.assertFalse(any(l["can_undo"] for l in api.get_today(self.company)["lines"]))
		self.assertFalse(api.get_today(self.company)["is_manager"])

	def test_cash_flows_never_touch_sales_tax_debtors_cogs_or_stock(self):
		"""A10: the whole flow leaves those accounts exactly as they were."""
		self._first_count()
		snapshot = self._protected()
		self._next_day()
		cd.record_deposit(self.company, 20)
		cd.record_from_bank(self.company, 10)
		cd.record_expense(self.company, 5, f"Postal Expenses - {self.abbr}", "stamps", self._receipt())
		r = self._close(50, reason="short")
		cd.undo_close(r["close"])
		self.assertEqual(self._protected(), snapshot)
		for je in self._jes("CASH-"):
			for account in self._lines(je):
				self.assertNotIn(account.split(" - ")[0], ("Sales", "Sales Tax Payable", "Debtors", "Cost of Goods Sold", "Stock In Hand"))

	# ------------------------------------------------------------------ the weekly report (A9)

	def _receive_payment(self, amount):
		pe = frappe.get_doc({
			"doctype": "Payment Entry", "payment_type": "Receive", "company": self.company,
			"posting_date": nowdate(), "party_type": "Customer", "party": "_Test CD Customer",
			"paid_from": f"Debtors - {self.abbr}", "paid_to": self.cash, "paid_amount": amount, "received_amount": amount,
			"source_exchange_rate": 1, "target_exchange_rate": 1,
		})
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()
		return pe.name

	def test_the_weekly_report_adds_up_and_has_every_column(self):
		self._first_count(books=1000, counted=120)
		self._next_day()
		pe = self._receive_payment(400)
		dep = cd.record_deposit(self.company, 100, "SLIP-7")
		cd.record_from_bank(self.company, 50, "W-3")
		cd.record_expense(self.company, 25, f"Postal Expenses - {self.abbr}", "stamps", self._receipt())
		self._sale(7)  # a hand-posted line the drawer screens did not make
		manual = frappe.db.get_value("GL Entry", {"company": self.company, "account": self.cash, "debit": 7, "is_cancelled": 0}, "voucher_no")
		expected = self._expected()
		self._close(expected - 30, reason="short at close")
		columns, rows = self._report()
		fields = [c["fieldname"] for c in columns]
		for needed in ("opening", "cash_pos", "cash_counter", "cash_payments", "receipt_docs", "refunds", "payouts", "payout_detail",
				"deposits", "deposit_detail", "from_bank", "from_bank_detail", "expenses", "expense_detail", "closing_gl", "expected",
				"counted", "variance", "variance_je", "counted_flag", "counted_by"):
			self.assertIn(needed, fields)
		self.assertFalse(any("8300" in str(c["label"]) for c in columns))  # no Form 8300 flag
		self.assertNotIn("owner", fields)  # owner draws are gone: the column is only for old ones
		# the first line is the cut-off entry, for the accountant
		self.assertEqual(rows[0]["row_kind"], "note")
		self.assertIn("CASH-CUTOFF", rows[0]["note"])
		today = next(r for r in rows if r.get("day") == nowdate())
		self.assertEqual((today["cash_payments"], today["deposits"], today["from_bank"], today["expenses"], today["other"]),
			(400, -100, 50, -25, 7))
		self.assertEqual(today["receipt_docs"], pe)
		self.assertEqual(today["deposit_detail"], "SLIP-7")
		self.assertEqual(today["from_bank_detail"], "W-3")
		self.assertIn("stamps", today["expense_detail"])
		self.assertEqual(today["count_adjust"], -30)
		self.assertEqual(today["flagged"], 1)  # 30 off, a reason was written
		# the foot: daily lines = the Cash account; the books end at the count; the stray line is listed
		check = {r["day"]: r for r in rows if r.get("row_kind") == "check"}
		differences = [r for r in rows if r.get("day") == "Difference"]
		self.assertEqual([r["closing_gl"] for r in differences], [0.0, 0.0])
		self.assertTrue(any(manual in str(r.get("note")) for r in rows))
		self.assertEqual(check["Total"]["from_bank"], 50)
		self.assertTrue(dep.entry)

	def test_a_clean_week_lists_no_unclassified_lines(self):
		self._first_count()
		self._next_day()
		self._receive_payment(60)
		cd.record_deposit(self.company, 10)
		self._close(self._expected() - 5)
		_cols, rows = self._report(days=0)
		self.assertTrue(any(r.get("day") == "Unclassified ledger lines" and r.get("note") == "None." for r in rows))
		self.assertEqual([r["closing_gl"] for r in rows if r.get("day") == "Difference"], [0.0, 0.0])
		self.assertFalse(any(r.get("flagged") for r in rows))  # 5 off needs no reason: not flagged

	# ------------------------------------------------------------------ refactor: the drawer's books, one door

	def _desk_cancel(self, name):
		je = frappe.get_doc("Journal Entry", name)
		je.flags.ignore_permissions = True
		je.cancel()

	def test_a_drawer_entrys_journal_entry_is_cancelled_only_from_the_page(self):
		"""Refactor (an old seller payment switched from Cash to Zelle in Desk): cancelling a cash
		entry's journal entry anywhere but the Cash Drawer page is refused -- the entry would stay Posted with no books
		behind it. Undo on the page still works."""
		made = cd.record_deposit(self.company, 50, "SLIP-R1")
		with self.assertRaises(frappe.ValidationError):
			self._desk_cancel(made.journal_entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", made.journal_entry, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", made.entry, "status"), "Posted")
		cd.void_entry(made.entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", made.journal_entry, "docstatus"), 2)

	def test_a_counts_journal_entry_is_cancelled_only_by_undoing_the_count(self):
		self._sale(100)
		first = self._close(80)
		self.assertTrue(first["cutoff_je"])
		with self.assertRaises(frappe.ValidationError):
			self._desk_cancel(first["cutoff_je"])
		cd.undo_close(first["close"])
		self.assertEqual(frappe.db.get_value("Journal Entry", first["cutoff_je"], "docstatus"), 2)

	def test_a_leftover_journal_entry_of_an_undone_entry_can_be_cancelled(self):
		"""Review: an entry already marked undone whose journal entry somehow stayed submitted is a leftover -- the page
		cannot undo it again, so Desk may clean it up."""
		made = cd.record_deposit(self.company, 50, "SLIP-LEFT")
		frappe.db.set_value("Cash Drawer Entry", made.entry, "status", "Voided", update_modified=False)
		self._desk_cancel(made.journal_entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", made.journal_entry, "docstatus"), 2)

	def test_the_refusal_names_the_right_way_to_undo(self):
		"""Review: a seller payment is a System Manager's on the page; anything else is Undo on the page."""
		made = cd.record_deposit(self.company, 50, "SLIP-MSG")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._desk_cancel(made.journal_entry)
		self.assertIn("Undo it on the Cash Drawer page", str(ctx.exception))
		frappe.db.set_value("Cash Drawer Entry", made.entry, "entry_type", "Payout", update_modified=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._desk_cancel(made.journal_entry)
		self.assertIn("System Manager", str(ctx.exception))

	def test_the_patch_stamps_the_drawers_existing_journal_entries(self):
		from ffl_core.patches.stamp_cash_drawer_journal_entries import execute

		made = cd.record_deposit(self.company, 50, "SLIP-OLD")
		self._sale(100)
		first = self._close(80)
		for je in (made.journal_entry, first["cutoff_je"]):
			frappe.db.set_value("Journal Entry", je, "ffl_cash_drawer_source", None, update_modified=False)
		execute()
		self.assertEqual(frappe.db.get_value("Journal Entry", made.journal_entry, "ffl_cash_drawer_source"), made.entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", first["cutoff_je"], "ffl_cash_drawer_source"), first["close"])
		with self.assertRaises(frappe.ValidationError):
			self._desk_cancel(made.journal_entry)

	def test_a_stray_stamp_locks_nothing(self):
		"""Review r2: the stamp counts only when the drawer record points back to this journal entry."""
		made = cd.record_deposit(self.company, 50, "SLIP-REAL")
		accounts = cd.ensure_accounts(self.company)
		stray = cd._post_je(self.company, [(self.cash, 10, 0), (accounts["over_short"], 0, 10)], "Not the drawer's",
			source=made.entry)
		self._desk_cancel(stray)
		self.assertEqual(frappe.db.get_value("Journal Entry", stray, "docstatus"), 2)

	def test_an_older_counts_journal_entry_says_only_the_latest_count_can_be_undone(self):
		self._sale(100)
		first = self._close(80)
		self._next_day()
		self._sale(50)
		second = self._close(120)  # 10 short: a variance
		self.assertEqual(frappe.db.get_value("Journal Entry", second["variance_je"], "ffl_cash_drawer_source"), second["close"])
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._desk_cancel(first["cutoff_je"])
		self.assertIn("only the latest count", str(ctx.exception))
		frappe.db.set_value("Cash Drawer Close", first["close"], "status", "Undone", update_modified=False)
		self._desk_cancel(first["cutoff_je"])  # a leftover of a count already taken back
		self.assertEqual(frappe.db.get_value("Journal Entry", first["cutoff_je"], "docstatus"), 2)

	def test_reversing_a_drawer_journal_entry_in_desk_is_refused(self):
		"""Review r2: Desk's Reverse Journal Entry would undo it without cancelling it."""
		from erpnext.accounts.doctype.journal_entry.journal_entry import make_reverse_journal_entry

		made = cd.record_deposit(self.company, 50, "SLIP-REV")
		reverse = make_reverse_journal_entry(made.journal_entry)
		reverse.posting_date = nowdate()
		reverse.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			reverse.insert()
		self.assertIn("Cash Drawer page", str(ctx.exception))

	def test_amending_a_drawer_journal_entry_the_page_cancelled_is_refused(self):
		"""Review r3: the stamp is not copied, so an Amend would put an undone entry's books back unwatched."""
		made = cd.record_deposit(self.company, 50, "SLIP-AMD")
		cd.void_entry(made.entry)
		amended = frappe.copy_doc(frappe.get_doc("Journal Entry", made.journal_entry))
		amended.amended_from = made.journal_entry
		amended.docstatus = 0
		for row in amended.accounts:
			row.docstatus = 0
		amended.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			amended.insert()
		self.assertIn("Record it again on the Cash Drawer page", str(ctx.exception))

	def test_a_counted_entrys_refusal_does_not_send_you_to_undo_it(self):
		"""Review r3: once a count relied on the entry, the refusal says to undo the count, not the entry."""
		self._sale(100)
		made = cd.record_deposit(self.company, 50, "SLIP-CNT")
		self._close(cd.drawer_expected(self.company).expected)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._desk_cancel(made.journal_entry)
		self.assertIn("undo that count", str(ctx.exception))
		self.assertNotIn("Undo it on the Cash Drawer page", str(ctx.exception))

	def test_any_other_journal_entry_cancels_as_before(self):
		accounts = cd.ensure_accounts(self.company)
		je = cd._post_je(self.company, [(self.cash, 10, 0), (accounts["over_short"], 0, 10)], "Not the drawer's")
		self._desk_cancel(je)
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 2)



def _store_tests(name: str, kind: str, base, cases):
	"""A concrete TestCase for one store: ``cases`` (a plain class of test methods and helpers) copied onto ``base``.
	Built here, not by subclassing ``cases``, so unittest collects each test once per store (see
	scripts/ci/test_ci_scripts.py NoRerunTestClasses)."""
	body = {k: v for k, v in vars(cases).items() if not k.startswith("__")}
	return type(name, (base,), {**body, "KIND": kind})


StoreACash = _store_tests("StoreACash", "store_a", CashFixtures, Cases)
StoreBCash = _store_tests("StoreBCash", "store_b", CashFixtures, Cases)
