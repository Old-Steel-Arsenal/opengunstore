"""A POS sale submits only fully paid, even though the store's POS Profiles allow
partial payment (a $400 cash tender on a $481.71 ticket once went through)."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.payment_mode import require_full_payment
from ffl_core.tests.test_cash_drawer_pos import ShiftFixtures


def _ticket(**kw):
	doc = {"is_return": 0, "grand_total": 481.71, "rounded_total": 481.71,
		"paid_amount": 481.71, "outstanding_amount": 0}
	doc.update(kw)
	return frappe._dict(doc)


class RequireFullPayment(FrappeTestCase):
	def test_underpaid_submit_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_full_payment(_ticket(paid_amount=400, outstanding_amount=81.71))
		self.assertIn("81.71", str(ctx.exception))
		self.assertIn("Manual Card Settlement", str(ctx.exception))
		self.assertIn("Recent Orders", str(ctx.exception))  # how to take the rest later

	def test_paid_in_full_passes(self):
		require_full_payment(_ticket())
		require_full_payment(_ticket(outstanding_amount=0.01))  # rounding noise

	def test_write_off_covers_the_rest(self):
		require_full_payment(_ticket(paid_amount=481.31, outstanding_amount=0.40, write_off_amount=0.40))
		with self.assertRaises(frappe.ValidationError):
			require_full_payment(_ticket(paid_amount=481.31, outstanding_amount=0.40, write_off_amount=0.20))

	def test_reads_the_outstanding_erpnext_sets_at_submit(self):
		"""POS Invoice.set_outstanding_amount (ERPNext before_submit) is what the gate reads."""
		inv = frappe.new_doc("POS Invoice")
		inv.update({"grand_total": 481.71, "rounded_total": 481.71, "paid_amount": 400})
		inv.set_outstanding_amount()
		with self.assertRaises(frappe.ValidationError):
			require_full_payment(inv)
		inv.paid_amount = 481.71
		inv.set_outstanding_amount()
		require_full_payment(inv)

	def test_return_and_non_positive_tickets_pass(self):
		require_full_payment(_ticket(is_return=1, paid_amount=0))                   # return
		require_full_payment(_ticket(grand_total=0, rounded_total=0, paid_amount=0))  # pickup ticket
		require_full_payment(_ticket(grand_total=-300, rounded_total=-300, paid_amount=0))  # store pays out

	def test_wired_on_pos_invoice_before_submit(self):
		hooks = frappe.get_hooks("doc_events")["POS Invoice"]["before_submit"]
		self.assertIn("ffl_core.compliance.payment_mode.require_full_payment", hooks)


class SubmitAtTheRegister(ShiftFixtures):
	"""A real submit, on a profile that allows partial payment as the stores' do: ERPNext's own check steps aside,
	so only this gate stands between an underpaid ticket and the books."""
	KIND = "store_a"

	def setUp(self):
		super().setUp()
		frappe.db.set_value("POS Profile", self.profile, "allow_partial_payment", 1)
		self._open_shift()

	def test_an_underpaid_ticket_does_not_submit(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._pos_sale(rate=100, tendered=60)
		self.assertIn("not paid yet", str(ctx.exception))

	def test_a_paid_ticket_submits(self):
		self.assertEqual(frappe.db.get_value("POS Invoice", self._pos_sale(rate=100, tendered=100), "docstatus"), 1)
