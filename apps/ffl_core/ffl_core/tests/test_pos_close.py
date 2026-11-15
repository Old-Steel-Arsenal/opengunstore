"""POS auto-close cron tests."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from ffl_core.api.pos_close import auto_close_open_pos


class POSAutoClose(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")
		cls._ensure_pos_pre_reqs()

	@classmethod
	def _ensure_pos_pre_reqs(cls):
		"""POS Opening / Closing Entry validate requires every Mode of
		Payment to have a default account on the active company. The test
		bootstrap doesn't seed those, so configure them here."""
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		if not company:
			return
		cash_account = frappe.db.get_value(
			"Account",
			{"company": company, "account_name": "Cash", "is_group": 0},
			"name",
		) or frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Cash", "is_group": 0},
			"name",
		)
		if not cash_account:
			return
		for mop in frappe.get_all("Mode of Payment", filters={"enabled": 1}, pluck="name"):
			doc = frappe.get_doc("Mode of Payment", mop)
			if not any(a.company == company for a in (doc.accounts or [])):
				doc.append("accounts", {"company": company, "default_account": cash_account})
				doc.flags.ignore_permissions = True
				doc.save()
		# A POS Profile to bind the opening entry to.
		if not frappe.db.exists("POS Profile", {"company": company}):
			warehouse = frappe.db.get_value(
				"Warehouse",
				{"company": company, "is_group": 0, "disabled": 0},
				"name",
			)
			pp = frappe.get_doc({
				"doctype": "POS Profile",
				"name": f"FFL Test POS",
				"company": company,
				"warehouse": warehouse,
				"currency": "USD",
				"customer_group": "All Customer Groups",
				"territory": "All Territories",
				"selling_price_list": frappe.db.get_value("Price List", {"selling": 1}, "name"),
				# A Profit and Loss account: ERPNext 16.55 refuses any other.
				"write_off_account": frappe.db.get_value("Account", {"company": company, "is_group": 0,
					"account_name": ("like", "Write Off%")}, "name"),
				"write_off_cost_center": frappe.db.get_value(
					"Cost Center", {"company": company, "is_group": 0}, "name"),
				"payments": [{"mode_of_payment": "Cash", "default": 1}],
			})
			pp.flags.ignore_permissions = True
			pp.insert()
		frappe.db.commit()

	def setUp(self):
		# Wipe ALL non-closed POS Opening Entries (and their dependents)
		# from prior runs. ERPNext blocks new openings while another is
		# open for the same POS profile + user.
		opens = frappe.get_all(
			"POS Opening Entry",
			filters=[["status", "!=", "Closed"]],
			pluck="name",
		)
		for name in opens:
			try:
				doc = frappe.get_doc("POS Opening Entry", name)
				if doc.docstatus == 1:
					doc.flags.ignore_permissions = True
					doc.cancel()
				frappe.delete_doc("POS Opening Entry", name, force=1, ignore_permissions=True)
			except Exception:
				pass
		# Tidy any orphan draft Closing Entries too.
		for name in frappe.get_all(
			"POS Closing Entry",
			filters={"docstatus": 0},
			pluck="name",
		):
			try:
				frappe.delete_doc("POS Closing Entry", name, force=1, ignore_permissions=True)
			except Exception:
				pass
		# Belt-and-suspenders direct DELETE for any remaining open openings
		# (rolled-back submitted state from a flaky prior run can survive
		# the high-level cleanup if the doctype's on_trash refuses).
		frappe.db.sql(
			"DELETE FROM `tabPOS Opening Entry` WHERE status != 'Closed'"
		)
		# Mark any unconsolidated submitted POS Invoices as Consolidated so
		# get_all_unconsolidated_invoices() doesn't sweep dev-DB leftovers
		# into our test's closing entry.
		frappe.db.sql(
			"UPDATE `tabPOS Invoice` SET status = 'Consolidated', "
			"consolidated_invoice = COALESCE(consolidated_invoice, name) "
			"WHERE docstatus = 1 AND (consolidated_invoice IS NULL OR consolidated_invoice = '')"
		)
		frappe.db.commit()

	def _make_opening(self, *, age_hours: float) -> str:
		"""Create a submitted POS Opening Entry that's `age_hours` old.

		Uses a draft insert + direct docstatus UPDATE because ERPNext's
		validate() calls check_open_pos_exists() on every save, including
		the submit→save chain — by which point the just-inserted row
		exists in DB and triggers a false 'already open' error. We
		short-circuit by submitting via SQL once the row is already
		written.
		"""
		profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
		if not profile:
			self.skipTest("No enabled POS Profile in this site")
		company = frappe.db.get_value("POS Profile", profile, "company")
		now = now_datetime()
		start = add_to_date(now, hours=-age_hours)
		mop = frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name") or "Cash"
		opening = frappe.get_doc({
			"doctype": "POS Opening Entry",
			"posting_date": start.date(),
			"period_start_date": start,
			"user": "Administrator",
			"pos_profile": profile,
			"company": company,
			"status": "Open",
			"balance_details": [{"mode_of_payment": mop, "opening_amount": 100}],
		})
		opening.flags.ignore_permissions = True
		opening.insert()
		# Submit via direct UPDATE (bypasses the redundant validate).
		frappe.db.set_value(
			"POS Opening Entry", opening.name,
			{"docstatus": 1, "period_start_date": start},
			update_modified=False,
		)
		return opening.name

	def test_closes_old_opening_with_zero_variance(self):
		opening_name = self._make_opening(age_hours=24)
		out = auto_close_open_pos()
		self.assertEqual(len(out["closed"]), 1)
		self.assertEqual(out["closed"][0]["opening"], opening_name)
		# Closing was submitted (docstatus=1)
		closing_name = out["closed"][0]["closing"]
		self.assertEqual(frappe.db.get_value("POS Closing Entry", closing_name, "docstatus"), 1)
		# Opening is now flagged Closed
		self.assertEqual(frappe.db.get_value("POS Opening Entry", opening_name, "status"), "Closed")
		# Zero variance: closing_amount == expected_amount on every payment row
		closing = frappe.get_doc("POS Closing Entry", closing_name)
		for p in closing.payment_reconciliation:
			self.assertEqual(float(p.closing_amount), float(p.expected_amount))
			self.assertEqual(float(p.difference or 0), 0.0)

	def test_skips_recent_opening(self):
		"""An opening younger than MIN_AGE_HOURS (created in the last hour) must
		NOT be auto-closed — the next nightly run catches it instead."""
		opening_name = self._make_opening(age_hours=0.5)  # 30 min < 1h guard
		out = auto_close_open_pos()
		self.assertEqual(out["closed"], [])
		# Still Open
		self.assertEqual(frappe.db.get_value("POS Opening Entry", opening_name, "status"), "Open")

	def test_idempotent(self):
		"""Running twice on the same set must not double-close."""
		self._make_opening(age_hours=24)
		first = auto_close_open_pos()
		self.assertEqual(len(first["closed"]), 1)
		second = auto_close_open_pos()
		self.assertEqual(second["closed"], [])
		self.assertEqual(second["total_open_at_start"], 0)

	def test_min_age_override(self):
		"""Caller-supplied min_age_hours wins — e.g. an admin force-close that
		closes a shift younger than the nightly guard would normally skip."""
		opening_name = self._make_opening(age_hours=0.5)  # younger than the 1h default
		out = auto_close_open_pos(min_age_hours=0)  # force-close: no age floor
		self.assertEqual(len(out["closed"]), 1)
		self.assertEqual(out["closed"][0]["opening"], opening_name)
