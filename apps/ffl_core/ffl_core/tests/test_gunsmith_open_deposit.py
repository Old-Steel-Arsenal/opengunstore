"""gunsmith.open_deposit: a payment towards an open job parked as its own draft
deposit invoice, one per job, booked when the register (or an agent) submits it."""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core import gunsmith
from ffl_core.setup.customer_deposits import deposit_account
from ffl_core.setup.gunsmith import DEPOSIT_ITEM, LABOR_ITEM
from ffl_core.tests import test_gunsmith_work_order as gw


class OpenDeposit(gw._Base):

	def test_parks_one_draft_and_books_on_submit(self):
		job = gw._job(self.cust)
		out = gunsmith.open_deposit(job.name, 60)
		self.assertEqual((out["already"], out["amount"]), (False, 60))
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		self.assertEqual(inv.docstatus, 0)
		self.assertEqual(inv.ffl_work_order, job.name)
		self.assertEqual([(r.item_code, flt(r.amount)) for r in inv.items], [(DEPOSIT_ITEM, 60)])
		self.assertEqual(flt(inv.grand_total), 60, "untaxed")
		self.assertEqual(inv.items[0].income_account, deposit_account(gw.COMPANY))
		self.assertEqual(gunsmith.get_state(job.name)["drafts"]["deposit"], inv.name)

		again = gunsmith.open_deposit(job.name, 25)
		self.assertEqual(again, {"pos_invoice": inv.name, "already": True, "amount": 60},
			"the open draft is returned at its own amount, never a second one")

		self.assertEqual(gunsmith.deposit_held(job.name), 0, "nothing is held until it is paid")
		gw._pay(inv.name)
		self.assertEqual(gunsmith.deposit_held(job.name), 60)

	def test_bad_amount_and_closed_job_are_refused(self):
		job = gw._job(self.cust)
		for bad in (0, -5):
			with self.subTest(amount=bad), self.assertRaises(frappe.ValidationError):
				gunsmith.open_deposit(job.name, bad)
		gunsmith.cancel_work_order(job.name, "Parts unavailable")
		with self.assertRaises(frappe.ValidationError):
			gunsmith.open_deposit(job.name, 10)

	def test_refused_while_a_pickup_is_open(self):
		job = gw._ready(gw._job(self.cust), [(LABOR_ITEM, 80)])
		gunsmith.open_pickup(job.name)
		with self.assertRaisesRegex(frappe.ValidationError, "pick-up invoice"):
			gunsmith.open_deposit(job.name, 20)

	def test_counter_roles_only(self):
		job = gw._job(self.cust)
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			gunsmith.open_deposit(job.name, 10)
