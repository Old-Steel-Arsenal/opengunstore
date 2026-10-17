"""Gunsmith work orders: status, the intake authorization (a Per document agreement),
the deposit and pick-up invoices at the register, the bound book, and the sales report.

Site tests against the real doctypes on the bootstrap company ("FFL Test", 6% sales
tax). Register invoices are built by the work order's own builders and submitted as
the register would, with a cash payment.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core import agreements, gunsmith
from ffl_core.custody import is_custody_serial
from ffl_core.setup import gunsmith as setup
from ffl_core.setup.customer_deposits import deposit_account
from ffl_core.setup.gunsmith import DEPOSIT_ITEM, LABOR_ITEM
from ffl_core.tests.test_agreements import _png
from ffl_core.tests.test_customer_profile import _hooks, _uniq, _user

COMPANY = "FFL Test"
PROFILE = "FFL Test Gunsmith"
TAX = "US ST 6% - FT"


def _customer(**fields):
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": _uniq("Owner"), "customer_group": "Individual",
		"territory": "All Territories", "ffl_first_name": "Pat", "ffl_last_name": "Owner", **fields})
	doc.insert(ignore_permissions=True)
	addr = frappe.get_doc({"doctype": "Address", "address_title": doc.name, "address_type": "Billing",
		"address_line1": "12 Main St", "city": "Springfield", "state": "TX", "pincode": "75024", "country": "United States",
		"links": [{"link_doctype": "Customer", "link_name": doc.name}]})
	addr.insert(ignore_permissions=True)
	frappe.db.set_value("Customer", doc.name, "customer_primary_address", addr.name)
	return frappe.get_doc("Customer", doc.name)


def _job(customer, **fields):
	doc = frappe.get_doc({"doctype": gunsmith.DOCTYPE, "customer": customer.name, "company": COMPANY,
		"manufacturer": "Colt", "model": "1911", "caliber": ".45 ACP", "firearm_type": "Pistol",
		"serial_number": "GW" + frappe.generate_hash(length=8).upper(), "work_requested": "Replace the extractor.",
		**fields})
	doc.insert(ignore_permissions=True)
	return doc


def _ready(job, lines=()):
	job.reload()
	job.work_performed = "Fitted a new extractor, function tested."
	for item_code, rate in lines:
		job.append("lines", {"item_code": item_code, "qty": 1, "rate": rate})
	job.save(ignore_permissions=True)
	gunsmith.mark_ready(job.name)
	job.reload()
	return job


def _payment(job, amount, *goods, header=False):
	"""A register sale paying *amount* towards *job*: its locked line (gunsmith.deposit_line) in a
	cart, with any other goods, the header link left to the deposit guard as at the register."""
	line = gunsmith.deposit_line(job.name, amount)["deposit_line"]
	inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": PROFILE, "customer": job.customer,
		"items": [line, *goods], **({"ffl_work_order": job.name} if header else {})})
	inv.set_missing_values()
	inv.flags.ignore_permissions = True
	inv.insert()
	return inv.name


def _pay(name, amount=None):
	"""Submit a register invoice the way the register does: cash for what it comes to."""
	inv = frappe.get_doc("POS Invoice", name)
	total = flt(inv.rounded_total) or flt(inv.grand_total)
	inv.set("payments", [])
	inv.append("payments", {"mode_of_payment": "Cash", "amount": total if amount is None else amount, "default": 1})
	inv.flags.ignore_permissions = True
	inv.save()
	inv.submit()
	return inv


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup.ensure()
		frappe.defaults.set_user_default("Company", COMPANY)
		if not frappe.db.exists("Item", "_Test GW Part"):
			frappe.get_doc({"doctype": "Item", "item_code": "_Test GW Part", "item_name": "Extractor",
				"item_group": "Parts", "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
		cls._profile()
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(COMPANY, PROFILE)

	@classmethod
	def _profile(cls):
		if frappe.db.exists("POS Profile", PROFILE):
			return
		mop = frappe.get_doc("Mode of Payment", "Cash")
		if not any(r.company == COMPANY for r in mop.accounts):
			mop.append("accounts", {"company": COMPANY, "default_account": "Cash - FT"})
			mop.save(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "POS Profile", "name": PROFILE, "company": COMPANY, "warehouse": "Stores - FT",
			"currency": "USD", "customer_group": "All Customer Groups", "territory": "All Territories",
			"selling_price_list": "Standard Selling", "taxes_and_charges": TAX,
			"write_off_account": frappe.db.get_value("Account", {"company": COMPANY, "account_name": ("like", "Write Off%")}),
			"write_off_cost_center": "Main - FT",
			"income_account": frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Sales", "is_group": 0}),
			"expense_account": frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Expense Account", "is_group": 0}),
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
		}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		# ERPNext commits inside some submit paths; keep each test's rows in its own transaction.
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		self.cust = _customer()


class Wiring(FrappeTestCase):

	def test_hooks(self):
		hooks = _hooks()
		events = hooks["doc_events"]
		self.assertIn("ffl_core.gunsmith.guard_deposit_lines", events["POS Invoice"]["validate"])
		self.assertIn("ffl_core.gunsmith.guard_deposit_lines", events["Sales Invoice"]["validate"])
		self.assertIn("ffl_core.gunsmith.on_invoice_submit", events["POS Invoice"]["on_submit"])
		self.assertIn("ffl_core.gunsmith.on_invoice_cancel", events["POS Invoice"]["on_cancel"])
		self.assertIn("ffl_core.gunsmith.before_invoice_cancel", events["POS Invoice"]["before_cancel"])
		self.assertIn("ffl_core.gunsmith.link_intake_agreement", str(events["Customer Agreement"]))
		self.assertIn("ffl_core.setup.gunsmith.ensure", hooks["after_migrate"])
		# Before the deposit guard reads the locked amount, ERPNext's own validate has run.
		validate = events["POS Invoice"]["validate"]
		self.assertLess(validate.index("ffl_core.gunsmith.guard_deposit_lines"),
			validate.index("ffl_core.compliance.locked_lines.assert_locked_lines"))

	def test_setup_items_and_settings(self):
		setup.ensure()
		self.assertEqual(frappe.db.get_value("Item Default", {"parent": DEPOSIT_ITEM, "company": COMPANY},
			"income_account"), deposit_account(COMPANY))
		self.assertTrue(frappe.db.exists("Item Tax", {"parent": DEPOSIT_ITEM, "item_tax_template": "Non-Taxable Service - FT"}),
			"the deposit carries its own 0% template, not the group's")
		self.assertEqual(frappe.db.get_value("Item", LABOR_ITEM, "is_stock_item"), 0)
		self.assertEqual(setup.labor_item(), LABOR_ITEM)
		self.assertEqual(frappe.db.get_value("Agreement Template", setup.agreement_template(), "scope"), "Per document")
		from ffl_core.setup.locked_lines import locked_items
		self.assertIn(DEPOSIT_ITEM, locked_items())
		self.assertTrue(frappe.db.exists("Email Template", "Gunsmith Work Ready"))
		for dt in ("POS Invoice", "POS Invoice Item", "Sales Invoice Item"):
			self.assertTrue(frappe.db.has_column(dt, "ffl_work_order"), dt)


class Status(_Base):

	def test_new_job_is_received_with_the_customers_contact(self):
		job = _job(_customer(mobile_no="555-0100"))
		self.assertEqual(job.status, "Received")
		self.assertEqual(job.phone, "555-0100")
		self.assertIn("12 Main St", job.address)
		self.assertTrue(job.name.startswith("GWO-"))

	def test_status_moves_only_through_the_actions(self):
		job = _job(self.cust)
		job.status = "Ready"
		with self.assertRaises(frappe.ValidationError):
			job.save(ignore_permissions=True)
		job.reload()
		self.assertEqual(gunsmith.start_work(job.name)["status"], "In Progress")
		with self.assertRaises(frappe.ValidationError):
			gunsmith.mark_ready(job.name)   # no work performed yet
		job = _ready(job)
		self.assertEqual(job.status, "Ready")
		self.assertTrue(job.completed_on)
		self.assertEqual(gunsmith.start_work(job.name)["status"], "In Progress", "Ready can be reopened")

	def test_charges_total_the_final_cost(self):
		job = _ready(_job(self.cust), [(LABOR_ITEM, 150), ("_Test GW Part", 40)])
		self.assertEqual(flt(job.final_cost), 190)

	def test_notify(self):
		job = _ready(_job(self.cust))
		with self.assertRaises(frappe.ValidationError):
			gunsmith.notify_customer(job.name, "Email")   # no email address
		self.assertEqual(gunsmith.notify_customer(job.name, "Text")["notified_via"], "Text")
		job.reload()
		self.assertEqual((job.notified_via, job.notified_by), ("Text", "Administrator"))
		frappe.db.set_value(gunsmith.DOCTYPE, job.name, "email", "owner@example.com")
		with patch("frappe.sendmail") as sent:
			self.assertEqual(gunsmith.notify_customer(job.name, "Email")["email"], "owner@example.com")
		self.assertEqual(sent.call_args.kwargs["recipients"], ["owner@example.com"])
		self.assertIn("1911", sent.call_args.kwargs["subject"])
		with self.assertRaises(frappe.ValidationError):
			gunsmith.notify_customer(job.name, "Carrier pigeon")

	def test_a_closed_job_is_kept_as_it_is(self):
		job = _job(self.cust)
		gunsmith.cancel_work_order(job.name, "Customer changed their mind")
		job.reload()
		self.assertEqual(job.status, "Cancelled")
		job.work_requested = "Something else"
		with self.assertRaises(frappe.ValidationError):
			job.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			gunsmith.start_work(job.name)


class Authorization(_Base):

	def _request(self, job, deliver="qr"):
		return agreements.request_agreement(job.customer, setup.agreement_template(), deliver, 1,
			reference_doctype=gunsmith.DOCTYPE, reference_name=job.name)

	def test_signed_for_the_job_it_names(self):
		job = _job(self.cust, deposit_amount=50, estimated_cost=200)
		other = _job(self.cust)
		r = self._request(job)
		copy = frappe.get_doc("Customer Agreement", r["name"])
		self.assertEqual((copy.reference_doctype, copy.reference_name), (gunsmith.DOCTYPE, job.name))
		self.assertIn(job.name, copy.body_snapshot)
		self.assertIn(job.serial_number, copy.body_snapshot)
		self.assertIn("200.00", copy.body_snapshot)
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "intake_agreement"), copy.name)
		r2 = self._request(other)
		self.assertNotEqual(r2["name"], r["name"], "each job its own copy")
		token = r["sign_url"].split("#t=", 1)[1]
		frappe.set_user("Guest")
		try:
			agreements.submit_signature(token, signature=_png(), agreed=1, signer_name="Pat Owner")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(gunsmith.get_state(job.name)["authorization"]["state"], "signed")
		self.assertEqual(gunsmith.get_state(other.name)["authorization"]["state"], "pending")
		self.assertIsNone(frappe.db.get_value("Customer Agreement", r["name"], "expires_on"))

	def test_needs_its_record_and_the_records_customer(self):
		job = _job(self.cust)
		with self.assertRaises(frappe.ValidationError):
			agreements.request_agreement(self.cust.name, setup.agreement_template(), "qr", 1)
		with self.assertRaises(frappe.ValidationError):
			agreements.request_agreement(_customer().name, setup.agreement_template(), "qr", 1,
				reference_doctype=gunsmith.DOCTYPE, reference_name=job.name)

	def test_never_asked_for_at_a_sale_or_on_the_counter_panel(self):
		frappe.db.set_value("Agreement Template", setup.agreement_template(),
			{"requirement": "Required for every sale", "show_on_counter_panel": 1})
		si = frappe.new_doc("Sales Invoice")
		si.customer = self.cust.name
		si.append("items", {"item_code": "_Test GW Part", "qty": 1, "rate": 10})
		self.assertNotIn(setup.agreement_template(), [n["template"] for n in agreements.sale_requirements(si)])
		self.assertNotIn(setup.agreement_template(), [r["template"] for r in agreements.counter_panel_rows(self.cust.name)])
		self.assertNotIn(setup.agreement_template(), [r["template"] for r in
			agreements.agreements_for_sale(self.cust.name, [{"item_code": "_Test GW Part"}])["rows"]])

	def test_a_per_customer_copy_is_not_satisfied_by_a_jobs_copy(self):
		tpl = frappe.get_doc({"doctype": "Agreement Template", "title": _uniq("Waiver"), "scope": "Per document",
			"body": "<p>{{ work_order }}</p>"}).insert(ignore_permissions=True)
		job = _job(self.cust)
		agreements.request_agreement(self.cust.name, tpl.name, "none", 1, reference_doctype=gunsmith.DOCTYPE,
			reference_name=job.name)
		self.assertEqual(agreements.agreement_status(self.cust.name, tpl.name)["state"], "missing")


class Money(_Base):

	def test_deposit_then_pickup(self):
		"""$100 paid towards the job, held as a liability, untaxed; pick-up bills $150 labor + $40 part
		(6% on the part only) less the $100."""
		job = _job(self.cust)
		dep = _pay(_payment(job, 100))
		self.assertEqual(flt(dep.grand_total), 100)
		self.assertEqual(flt(dep.total_taxes_and_charges), 0)
		# A POS Invoice books through its consolidated Sales Invoice, which copies each row's account.
		self.assertEqual(dep.items[0].income_account, deposit_account(COMPANY))
		self.assertEqual(dep.ffl_work_order, job.name, "the guard links the header")
		self.assertEqual(gunsmith.deposit_held(job.name), 100)
		self.assertEqual(flt(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "deposit_held")), 100)
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "deposit_invoice"), dep.name)

		job = _ready(job, [(LABOR_ITEM, 150), ("_Test GW Part", 40)])
		pick = frappe.get_doc("POS Invoice", gunsmith.open_pickup(job.name)["pos_invoice"])
		self.assertEqual([(r.item_code, flt(r.amount)) for r in pick.items],
			[(LABOR_ITEM, 150), ("_Test GW Part", 40), (DEPOSIT_ITEM, -100)])
		self.assertEqual(flt(pick.total_taxes_and_charges), 2.4)
		self.assertEqual(flt(pick.grand_total), 92.4)
		pick = _pay(pick.name)
		self.assertEqual([r.income_account for r in pick.items if r.item_code == DEPOSIT_ITEM], [deposit_account(COMPANY)])
		job.reload()
		self.assertEqual((job.status, job.pickup_invoice), ("Picked Up", pick.name))
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_several_payments_any_amount_while_open(self):
		"""$50 at intake's sale (with other goods, no header link), $30 later: $80 held, all released at
		pick-up; a closed job takes no more."""
		job = _job(self.cust)
		first = _pay(_payment(job, 50, {"item_code": "_Test GW Part", "qty": 1, "rate": 20}))
		self.assertEqual([(r.item_code, flt(r.amount)) for r in first.items], [(DEPOSIT_ITEM, 50), ("_Test GW Part", 20)])
		self.assertEqual(flt(first.grand_total), 71.2, "6% on the part only")
		gunsmith.start_work(job.name)
		second = _pay(_payment(job, 30))
		self.assertEqual(gunsmith.deposit_held(job.name), 80)
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, ["status", "deposit_invoice"]),
			("In Progress", second.name), "a payment never picks the job up")
		job = _ready(job, [(LABOR_ITEM, 100)])
		_pay(_payment(job, 10))   # Ready still takes payments
		pick = frappe.get_doc("POS Invoice", gunsmith.open_pickup(job.name)["pos_invoice"])
		self.assertEqual([(r.item_code, flt(r.amount)) for r in pick.items], [(LABOR_ITEM, 100), (DEPOSIT_ITEM, -90)])
		_pay(pick.name)
		self.assertEqual(gunsmith.deposit_held(job.name), 0)
		second.reload()
		second.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "already come off"):
			second.cancel()   # its money came off the pick-up
		with self.assertRaises(frappe.ValidationError):
			gunsmith.deposit_line(job.name, 10)
		for bad in (0, -5):
			with self.subTest(amount=bad), self.assertRaises(frappe.ValidationError):
				gunsmith.deposit_line(_job(self.cust).name, bad)

	def test_a_payment_line_stays_at_its_own_amount(self):
		"""The lock: typed, priced or re-stamped, the line keeps the amount it was made for."""
		job = _job(self.cust)
		inv = frappe.get_doc("POS Invoice", _payment(job, 60))
		inv.items[0].rate = 5
		inv.items[0].price_list_rate = 999
		inv.save(ignore_permissions=True)
		self.assertEqual(flt(inv.items[0].rate), 60)
		self.assertEqual(flt(inv.items[0].price_list_rate), 0)
		self.assertEqual(inv.items[0].income_account, deposit_account(COMPANY))
		inv.items[0].ffl_locked_rate = 0
		with self.assertRaises(frappe.ValidationError):
			inv.save(ignore_permissions=True)   # a line with no lock is not from its work order

	def test_a_deposit_bigger_than_the_bill_is_refunded(self):
		job = _job(self.cust)
		_pay(_payment(job, 300))
		job = _ready(job, [(LABOR_ITEM, 120)])
		pick = frappe.get_doc("POS Invoice", gunsmith.open_pickup(job.name)["pos_invoice"])
		self.assertEqual(flt(pick.grand_total), 0)
		_pay(pick.name, 0)
		self.assertEqual(gunsmith.deposit_held(job.name), 180)
		ret = frappe.get_doc("POS Invoice", gunsmith.refund_deposit(job.name)["pos_invoice"])
		self.assertTrue(ret.is_return)
		self.assertEqual(flt(ret.grand_total), -180)
		_pay(ret.name)
		self.assertEqual(gunsmith.deposit_held(job.name), 0)
		with self.assertRaises(frappe.ValidationError):
			gunsmith.refund_deposit(job.name)

	def test_a_used_deposit_invoice_cannot_be_cancelled(self):
		job = _job(self.cust)
		dep = _pay(_payment(job, 100))
		job = _ready(job, [(LABOR_ITEM, 150)])
		pick = _pay(gunsmith.open_pickup(job.name)["pos_invoice"])
		dep.reload()
		dep.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			dep.cancel()
		self.assertEqual(gunsmith.deposit_held(job.name), 0)
		# Once the pick-up is cancelled the deposit is unused again, and its invoice may go.
		pick.reload()
		pick.flags.ignore_permissions = True
		pick.cancel()
		dep.reload()
		dep.flags.ignore_permissions = True
		dep.cancel()
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_cancel_refused_while_a_pickup_is_open(self):
		job = _ready(_job(self.cust), [(LABOR_ITEM, 80)])
		gunsmith.open_pickup(job.name)
		with self.assertRaises(frappe.ValidationError):
			gunsmith.cancel_work_order(job.name, "Customer changed their mind")

	def test_cancelled_job_refunds_the_whole_deposit(self):
		job = _job(self.cust)
		_pay(_payment(job, 75))
		with self.assertRaises(frappe.ValidationError):
			gunsmith.refund_deposit(job.name)   # still open: it comes off the bill
		gunsmith.cancel_work_order(job.name, "Parts unavailable")
		_pay(gunsmith.refund_deposit(job.name)["pos_invoice"])
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_nothing_to_charge_and_nothing_held_picks_up_at_once(self):
		job = _ready(_job(self.cust))
		self.assertEqual(gunsmith.open_pickup(job.name), {"picked_up": True, "refund_due": 0})
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "status"), "Picked Up")

	def test_no_charges_with_a_deposit_hands_back_then_refunds(self):
		job = _job(self.cust)
		_pay(_payment(job, 40))
		job = _ready(job)
		self.assertEqual(gunsmith.open_pickup(job.name), {"picked_up": True, "refund_due": 40})
		_pay(gunsmith.refund_deposit(job.name)["pos_invoice"])
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_a_reopened_job_does_not_release_its_deposit(self):
		job = _job(self.cust)
		_pay(_payment(job, 100))
		job = _ready(job, [(LABOR_ITEM, 150)])
		pick = gunsmith.open_pickup(job.name)["pos_invoice"]
		with self.assertRaises(frappe.ValidationError):
			gunsmith.start_work(job.name)   # the pick-up draft is open
		frappe.db.set_value(gunsmith.DOCTYPE, job.name, "status", "In Progress")
		with self.assertRaises(frappe.ValidationError):
			_pay(pick)
		self.assertEqual(gunsmith.deposit_held(job.name), 100)

	def test_a_parked_payment_is_not_linked_on_the_job(self):
		job = _job(self.cust)
		inv = _payment(job, 100)
		self.assertFalse(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "deposit_invoice"))
		self.assertEqual(gunsmith.get_state(job.name)["drafts"]["deposit"], inv)
		job = _ready(job, [(LABOR_ITEM, 10)])
		with self.assertRaises(frappe.ValidationError):
			gunsmith.complete_work(job.name, "Done.", "[]")   # the parked payment first
		frappe.delete_doc("POS Invoice", inv, ignore_permissions=True)
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_a_refund_goes_back_payment_by_payment(self):
		"""$100 then $50 held on a cancelled job: the first return pays back the $50 (newest), the
		next the $100 — a return never exceeds the payment it goes against."""
		job = _job(self.cust)
		_pay(_payment(job, 100))
		_pay(_payment(job, 50))
		gunsmith.cancel_work_order(job.name, "Parts unavailable")
		first = gunsmith.refund_deposit(job.name)
		self.assertEqual(first["amount"], 50)
		_pay(first["pos_invoice"])
		self.assertEqual(gunsmith.deposit_held(job.name), 100)
		second = gunsmith.refund_deposit(job.name)
		self.assertEqual(second["amount"], 100)
		ret = _pay(second["pos_invoice"])
		self.assertEqual(flt(ret.grand_total), -100)
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_deposit_line_only_from_its_work_order(self):
		job = _job(self.cust)

		def ticket(customer, **row):
			inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": PROFILE,
				"customer": customer, "items": [{"item_code": DEPOSIT_ITEM, "qty": 1, "rate": 100, **row}]})
			inv.set_missing_values()
			inv.flags.ignore_permissions = True
			return inv

		with self.assertRaises(frappe.ValidationError):
			ticket(self.cust.name).insert()
		with self.assertRaises(frappe.ValidationError):
			ticket(_customer().name, ffl_work_order=job.name, ffl_locked_rate=100).insert()
		with self.assertRaises(frappe.ValidationError):
			ticket(self.cust.name, ffl_work_order=job.name, ffl_locked_rate=100, rate=100,
				qty=2).insert()
		with self.assertRaises(frappe.ValidationError):
			ticket(self.cust.name, ffl_work_order=job.name).insert()   # no lock: not made by its work order
		gunsmith.cancel_work_order(job.name, "No")
		with self.assertRaises(frappe.ValidationError):
			ticket(self.cust.name, ffl_work_order=job.name, ffl_locked_rate=100).insert()   # closed
		si = frappe.new_doc("Sales Invoice")
		si.company = COMPANY
		si.customer = self.cust.name
		si.append("items", {"item_code": DEPOSIT_ITEM, "qty": 1, "rate": 100, "ffl_work_order": job.name})
		with self.assertRaises(frappe.ValidationError):
			gunsmith.guard_deposit_lines(si)


class Register(_Base):
	"""The POS register: the Work Order button's intake, Recent Orders › Work Orders, and
	Complete (confirm the charges, then the pick-up invoice the cart loads)."""

	def _intake(self, **fields):
		return gunsmith.create_work_order(frappe.as_json({"customer": self.cust.name, "manufacturer": "Ruger",
			"model": "10/22", "serial_number": "GR" + frappe.generate_hash(length=8).upper(),
			"work_requested": "Trigger job.", **fields}))

	def test_intake_creates_the_job(self):
		out = self._intake(estimated_cost=120, accessories="Sling", status="Picked Up", deposit_held=500)
		self.assertIsNone(out["deposit_line"])
		job = frappe.get_doc(gunsmith.DOCTYPE, out["name"])
		self.assertEqual((job.status, job.customer, flt(job.estimated_cost), job.accessories),
			("Received", self.cust.name, 120, "Sling"), "only intake fields are taken from the dialog")
		self.assertEqual(flt(job.deposit_held), 0)
		self.assertFalse(out["purchase_receipt"])
		with self.assertRaises(frappe.ValidationError):
			gunsmith.create_work_order(frappe.as_json({"customer": self.cust.name, "manufacturer": "Ruger"}))

	def test_pay_now_at_intake(self):
		"""Pay now: the register gets the locked line for the cart; the line books as a payment
		towards the job, which records the amount (its authorization prints it) once that cart is paid."""
		out = self._intake(pay_now=75)
		line = out["deposit_line"]
		self.assertEqual((line["item_code"], flt(line["rate"]), flt(line["ffl_locked_rate"]), line["ffl_work_order"]),
			(DEPOSIT_ITEM, 75, 75, out["name"]))
		job = frappe.get_doc(gunsmith.DOCTYPE, out["name"])
		self.assertEqual(flt(job.deposit_amount), 0, "nothing is paid until the cart checks out")
		inv = frappe.get_doc({"doctype": "POS Invoice", "company": COMPANY, "pos_profile": PROFILE,
			"customer": self.cust.name, "items": [line]})
		inv.set_missing_values()
		inv.flags.ignore_permissions = True
		inv.insert()
		_pay(inv.name)
		self.assertEqual(gunsmith.deposit_held(job.name), 75)
		job.reload()
		self.assertEqual(flt(job.deposit_amount), 75)
		self.assertIn("75.00", job.agreement_context()["deposit"])
		with self.assertRaises(frappe.ValidationError):
			self._intake(pay_now=-1)

	def test_intake_logs_the_gun_when_asked(self):
		with self.assertRaises(frappe.ValidationError):
			self._intake(log_in_bound_book=1, caliber=".22 LR", firearm_type="Rifle")   # no ID
		out = self._intake(log_in_bound_book=1, caliber=".22 LR", firearm_type="Rifle", id_type="Driver License",
			id_number="D5550001")
		self.assertTrue(out["purchase_receipt"])
		self.assertEqual(frappe.db.get_value("Purchase Receipt", out["purchase_receipt"], "is_ffl_transfer"), 1)

	def test_intake_is_a_counter_job(self):
		frappe.set_user(_user("gunsmith-buyer@example.com", "Purchase User"))
		with self.assertRaises(frappe.PermissionError):
			self._intake()
		with self.assertRaises(frappe.PermissionError):
			gunsmith.register_work_orders()
		with self.assertRaises(frappe.PermissionError):
			gunsmith.complete_work("GWO-none", "x")

	def test_the_button_shows_for_the_counter_roles(self):
		"""The POS Work Order button (ffl_core.bundle.js) mirrors roles.COUNTER_ROLES, the gate on
		create_work_order."""
		import re

		from ffl_core.roles import COUNTER_ROLES
		from ffl_core.tests.front_end_invariants import code_only, read

		js = code_only(read("ffl_core", "public", "js", "ffl_core.bundle.js"))
		[listed] = re.findall(r"const WORK_ORDER_ROLES = \[([^\]]*)\];", js)
		self.assertEqual(tuple(re.findall(r"'([^']+)'", listed)), COUNTER_ROLES)
		self.assertIn("if (frappe.user.has_role(WORK_ORDER_ROLES)) ensureBtn(WORK_ORDER_BTN_ID,", js)

	def test_the_list_holds_open_jobs_ready_first(self):
		received = _job(self.cust)
		ready = _ready(_job(self.cust), [(LABOR_ITEM, 75)])
		closed = _job(self.cust)
		gunsmith.cancel_work_order(closed.name, "No")
		out = gunsmith.register_work_orders(search_term=self.cust.customer_name)
		names = [r["name"] for r in out["orders"]]
		self.assertEqual(out["labor_item"], LABOR_ITEM)
		self.assertNotIn(closed.name, names)
		self.assertLess(names.index(ready.name), names.index(received.name))
		row = next(r for r in out["orders"] if r["name"] == ready.name)
		self.assertEqual([(l["item_code"], flt(l["rate"])) for l in row["lines"]], [(LABOR_ITEM, 75)])
		self.assertEqual(row["drafts"], {"deposit": None, "pickup": None, "refund": None})
		self.assertFalse(row["logged"])
		self.assertEqual([r["name"] for r in gunsmith.register_work_orders(name=received.name)["orders"]],
			[received.name])
		self.assertFalse(gunsmith.register_work_orders(search_term="zzz-no-such-job")["orders"])

	def test_complete_keeps_the_charges_marks_ready_and_bills_at_checkout(self):
		"""In Progress → Complete with $150 labor + $40 part: the job keeps them and is Ready;
		the pick-up draft carries them (plus a cart's other goods) and its checkout picks it up."""
		job = _job(self.cust, estimated_cost=100)
		gunsmith.start_work(job.name)
		out = gunsmith.complete_work(job.name, "Fitted a new extractor.", frappe.as_json([
			{"item_code": LABOR_ITEM, "description": "Extractor fitting", "qty": 1, "rate": 150},
			{"item_code": "_Test GW Part", "qty": 1, "rate": 40}, {"item_code": "", "rate": 5}]))
		job.reload()
		self.assertEqual((job.status, str(job.completed_on)), ("Ready", frappe.utils.nowdate()))
		self.assertEqual(job.work_performed, "Fitted a new extractor.")
		self.assertEqual([(r.item_code, flt(r.rate)) for r in job.lines], [(LABOR_ITEM, 150), ("_Test GW Part", 40)])
		inv = frappe.get_doc("POS Invoice", out["pos_invoice"])
		self.assertEqual((inv.docstatus, inv.customer, inv.ffl_work_order), (0, self.cust.name, job.name))
		self.assertEqual([(r.item_code, flt(r.amount)) for r in inv.items], [(LABOR_ITEM, 150), ("_Test GW Part", 40)])
		self.assertEqual(inv.items[0].description, "Extractor fitting")
		self.assertEqual(flt(inv.grand_total), 192.4)
		with self.assertRaises(frappe.ValidationError):
			gunsmith.complete_work(job.name, "Again", "[]")   # the pick-up is open in the cart
		self.assertEqual(gunsmith.register_work_orders(name=job.name)["orders"][0]["drafts"]["pickup"], inv.name)
		# The cashier sells something else on the same ticket, then checks out normally.
		inv.append("items", {"item_code": "_Test GW Part", "qty": 2, "rate": 10})
		inv.flags.ignore_permissions = True
		inv.save()
		inv = _pay(inv.name)
		job.reload()
		self.assertEqual((job.status, job.pickup_invoice), ("Picked Up", inv.name))
		self.assertEqual(gunsmith.register_work_orders(name=job.name)["orders"], [])

	def test_complete_releases_a_deposit_held(self):
		job = _job(self.cust)
		_pay(_payment(job, 100))
		inv = frappe.get_doc("POS Invoice", gunsmith.complete_work(job.name, "Done.",
			frappe.as_json([{"item_code": LABOR_ITEM, "qty": 1, "rate": 150}]))["pos_invoice"])
		self.assertEqual([(r.item_code, flt(r.amount)) for r in inv.items], [(LABOR_ITEM, 150), (DEPOSIT_ITEM, -100)])
		_pay(inv.name)
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "status"), "Picked Up")
		self.assertEqual(gunsmith.deposit_held(job.name), 0)

	def test_a_bill_cut_below_the_release_is_refused(self):
		"""Complete sized the release to the bill; cutting the charges in the cart afterwards
		would ring a negative pick-up: ERPNext refuses a negative non-return ticket."""
		job = _job(self.cust)
		_pay(_payment(job, 100))
		inv = frappe.get_doc("POS Invoice", gunsmith.complete_work(job.name, "Done.",
			frappe.as_json([{"item_code": LABOR_ITEM, "qty": 1, "rate": 150}]))["pos_invoice"])
		inv.items[0].rate = inv.items[0].price_list_rate = 60
		with self.assertRaises(frappe.ValidationError):
			inv.save()

	def test_a_closed_job_holding_money_is_listed_for_its_refund(self):
		job = _job(self.cust)
		_pay(_payment(job, 40))
		gunsmith.cancel_work_order(job.name, "Customer took it back")
		rows = gunsmith.register_work_orders(name=job.name)["orders"]
		self.assertEqual([(r["name"], r["status"], flt(r["deposit_held"]), r["authorization"]) for r in rows],
			[(job.name, "Cancelled", 40, None)])
		self.assertEqual(gunsmith.register_work_orders(name=_job(self.cust).name)["orders"][0]["status"], "Received")

	def test_a_refund_goes_back_the_way_it_was_paid(self):
		"""Paid by card, refunded on the card row (the register's checkout refunds the card), not on
		whatever row came first."""
		mop = "GW Test Card"
		if not frappe.db.exists("Mode of Payment", mop):
			frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mop, "type": "Bank",
				"accounts": [{"company": COMPANY, "default_account": "Cash - FT"}]}).insert(ignore_permissions=True)
		job = _job(self.cust)
		inv = frappe.get_doc("POS Invoice", _payment(job, 30))
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": "Cash", "amount": 0, "default": 1})
		inv.append("payments", {"mode_of_payment": mop, "amount": 30})
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		gunsmith.cancel_work_order(job.name, "Parts unavailable")
		ret = frappe.get_doc("POS Invoice", gunsmith.refund_deposit(job.name)["pos_invoice"])
		self.assertEqual({p.mode_of_payment: flt(p.amount) for p in ret.payments if flt(p.amount)}, {mop: -30})

	def test_complete_with_nothing_to_charge_picks_up_at_once(self):
		job = _job(self.cust)
		self.assertEqual(gunsmith.complete_work(job.name, "Inspected, nothing needed.", "[]"),
			{"picked_up": True, "refund_due": 0})
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "status"), "Picked Up")

	def test_complete_refuses_what_is_not_a_charge(self):
		job = _job(self.cust)
		for bad in ([{"item_code": DEPOSIT_ITEM, "rate": 10}], [{"item_code": "No Such Item X", "rate": 10}],
				[{"item_code": LABOR_ITEM, "rate": -5}]):
			with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError):
				gunsmith.complete_work(job.name, "Done.", frappe.as_json(bad))
		with self.assertRaises(frappe.ValidationError):
			gunsmith.complete_work(job.name, "  ", "[]")
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "status"), "Received")
		gunsmith.cancel_work_order(job.name, "No")
		with self.assertRaises(frappe.ValidationError):
			gunsmith.complete_work(job.name, "Done.", "[]")

	def test_a_logged_job_completed_at_the_register_books_the_disposition(self):
		job = _job(self.cust, log_in_bound_book=1, id_type="Driver License", id_number="D7650001")
		out = gunsmith.complete_work(job.name, "Cleaned.", frappe.as_json([{"item_code": LABOR_ITEM, "rate": 40}]))
		_pay(out["pos_invoice"])
		job.reload()
		self.assertEqual(job.status, "Picked Up")
		self.assertEqual(frappe.db.get_value("FFL Disposition", job.disposition, ["docstatus", "disposition_type"]),
			(1, "Gunsmithing"))


class SalesReport(FrappeTestCase):
	"""A deposit is never a sale: intake $100 → Net 0; pick-up $150 + $40 − $100 → Net 190."""

	def test_order_view_leaves_deposits_out(self):
		from ffl_core.ffl_core.report.sales_report.sales_report import _build_rows

		headers = [
			{"name": "GWD", "posting_date": "2026-10-17", "base_net_total": 100, "grand_total": 100,
				"voucher_type": "POS Invoice", "customer": "X"},
			{"name": "GWP", "posting_date": "2026-10-18", "base_net_total": 90, "grand_total": 92.4,
				"voucher_type": "POS Invoice", "customer": "X"},
		]
		lines = {
			"GWD": [{"item_code": DEPOSIT_ITEM, "qty": 1, "base_net_amount": 100}],
			"GWP": [{"item_code": LABOR_ITEM, "qty": 1, "base_net_amount": 150},
				{"item_code": "_Test GW Part", "qty": 1, "base_net_amount": 40},
				{"item_code": DEPOSIT_ITEM, "qty": 1, "base_net_amount": -100}],
		}
		rows = {r.get("invoice"): r for r in _build_rows(headers, lines, {}, {}, "All")}
		self.assertEqual(flt(rows["GWD"]["net_sales"]), 0)
		self.assertEqual(flt(rows["GWD"]["cost"]), 0)
		self.assertEqual(flt(rows["GWP"]["net_sales"]), 190)


class BoundBook(_Base):

	def test_logged_at_intake_into_custody_and_out_at_pickup(self):
		frappe.clear_messages()
		job = _job(self.cust, log_in_bound_book=1, id_type="Driver License", id_number="D1234567")
		job.reload()
		self.assertTrue(job.purchase_receipt)
		self.assertFalse([m for m in frappe.local.message_log if "No accounting entries" in str(m)],
			"custody at zero cost is by design, not a missing account")
		pr = frappe.get_doc("Purchase Receipt", job.purchase_receipt)
		self.assertEqual((pr.ffl_acquisition_type, pr.is_ffl_transfer), ("Gunsmithing", 1))
		sn = frappe.db.get_value("Serial No", job.serial_number, ["status", "purchase_rate", "warehouse"], as_dict=True)
		self.assertEqual((sn.status, flt(sn.purchase_rate)), ("Active", 0))
		self.assertTrue(is_custody_serial(job.serial_number), "never for sale")
		acq = frappe.get_doc("FFL Acquisition", {"purchase_receipt": pr.name})
		self.assertEqual((acq.source_type, acq.source_id_number), ("Individual", "D1234567"))

		job.serial_number = "CHANGED"
		with self.assertRaises(frappe.ValidationError):
			job.save(ignore_permissions=True)
		job.reload()
		job.log_in_bound_book = 0
		with self.assertRaises(frappe.ValidationError):
			job.save(ignore_permissions=True)
		job.reload()
		with self.assertRaises(frappe.ValidationError):
			gunsmith.cancel_work_order(job.name, "no")

		_ready(job)
		self.assertTrue(gunsmith.open_pickup(job.name)["picked_up"])
		job.reload()
		disp = frappe.get_doc("FFL Disposition", job.disposition)
		self.assertEqual((disp.docstatus, disp.disposition_type, disp.customer), (1, "Gunsmithing", self.cust.name))
		self.assertNotEqual(frappe.db.get_value("Serial No", job.serial_number, "status"), "Active")

	def test_the_job_keeps_no_id_number_when_ids_are_not_stored(self):
		with patch("ffl_core.feature_gate.customer_id_numbers_enabled", return_value=False):
			job = _job(self.cust, log_in_bound_book=1, id_type="Driver License", id_number="D2468101")
		job.reload()
		self.assertTrue(job.purchase_receipt)
		self.assertFalse(job.id_number)
		acq = frappe.get_doc("FFL Acquisition", {"purchase_receipt": job.purchase_receipt})
		self.assertEqual(acq.source_id_number, "D2468101")

	def test_unlogged_job_and_switch_off_keep_no_id_number(self):
		with patch("ffl_core.feature_gate.customer_id_numbers_enabled", return_value=False):
			job = _job(self.cust, id_type="Driver License", id_number="D1357911")
		self.assertFalse(frappe.db.get_value(gunsmith.DOCTYPE, job.name, "id_number"))
		with patch("ffl_core.feature_gate.customer_id_numbers_enabled", return_value=True):
			kept = _job(self.cust, id_type="Driver License", id_number="D1122334")
		self.assertEqual(frappe.db.get_value(gunsmith.DOCTYPE, kept.name, "id_number"), "D1122334")
		from ffl_core.customer_profile import erase_stored_ids
		erase_stored_ids()
		self.assertFalse(frappe.db.get_value(gunsmith.DOCTYPE, kept.name, "id_number"))

	def test_logged_later(self):
		job = _job(self.cust)
		self.assertFalse(job.purchase_receipt)
		with self.assertRaises(frappe.ValidationError):
			gunsmith.log_in_bound_book(job.name)   # no ID yet
		out = gunsmith.log_in_bound_book(job.name, "State ID", "S-99")
		self.assertTrue(out["purchase_receipt"])
		self.assertEqual(gunsmith.log_in_bound_book(job.name)["purchase_receipt"], out["purchase_receipt"])

	def test_a_logged_pickup_invoice_books_the_disposition(self):
		job = _job(self.cust, log_in_bound_book=1, id_type="Driver License", id_number="D7654321")
		job = _ready(job, [(LABOR_ITEM, 60)])
		pick = _pay(gunsmith.open_pickup(job.name)["pos_invoice"])
		job.reload()
		self.assertEqual(job.status, "Picked Up")
		self.assertEqual(frappe.db.get_value("FFL Disposition", job.disposition, "docstatus"), 1)
		pick.reload()
		pick.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			pick.cancel()
