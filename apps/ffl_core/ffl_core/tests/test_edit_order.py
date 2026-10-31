"""Pending Order × Edit Order — get_order_for_edit reconstructs the
create_order payload from a submitted counter invoice; update_order rebuilds
the order atomically with payments carried over; rename_customer fixes name
spellings system-wide. No external systems are involved anywhere (editing is
gated to undisposed, never-pushed orders), so nothing is mocked."""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


class EditOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose — the fixture CLASS
	carries commit-heavy test methods unittest would re-run per subclass."""


class TestGetOrderForEdit(EditOrderFixtures):
	def test_round_trips_payload(self):
		serial = self._stocked_serial("EO-G1")
		si = self._ship(serial, update_stock=0, buyer_type="Business")
		out = manual_order.get_order_for_edit(si.name)
		p = out["payload"]
		self.assertEqual(p["buyer_type"], "business")
		self.assertEqual(p["customer"], si.customer)
		self.assertEqual(p["destination_ffl"], DEST_FFL)
		self.assertEqual(p["biz_ffl"], DEST_FFL)
		self.assertEqual(len(p["items"]), 1)
		row = p["items"][0]
		self.assertEqual(row["item_code"], "BO-GUN")
		self.assertEqual(row["serials"], ["EO-G1"])
		self.assertTrue(row["is_firearm"])
		self.assertAlmostEqual(row["rate"], 900, places=2)
		self.assertEqual(p["payment_received"], 0)
		self.assertEqual(p["dispose_now"], 0)
		ctx = out["edit_ctx"]
		self.assertEqual(ctx["sales_invoice"], si.name)
		self.assertAlmostEqual(ctx["grand_total"], flt(si.grand_total), places=2)
		self.assertAlmostEqual(ctx["paid_total"], 0, places=2)

	def test_individual_payload_has_no_biz_ffl(self):
		si = self._ship(self._stocked_serial("EO-G2"), update_stock=0)
		p = manual_order.get_order_for_edit(si.name)["payload"]
		self.assertEqual(p["buyer_type"], "individual")
		self.assertNotIn("biz_ffl", p)

	def test_paid_ctx_carries_amount_and_mode(self):
		si = self._ship(self._stocked_serial("EO-G3"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=150)
		ctx = manual_order.get_order_for_edit(si.name)["edit_ctx"]
		self.assertAlmostEqual(ctx["paid_total"], 150, places=2)
		self.assertEqual(ctx["last_payment_mode"], "Cash")


class TestEditGuards(EditOrderFixtures):
	def test_refuses_disposed(self):
		si = self._ship(self._stocked_serial("EO-GD1"), update_stock=0,
			buyer_type="Business")
		manual_order.dispose_order(si.name)
		with self.assertRaises(frappe.ValidationError):
			manual_order.get_order_for_edit(si.name)

	def test_refuses_woo_and_missing_ffl(self):
		si = self._ship(self._stocked_serial("EO-GD2"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name,
			"woo_online_order", "WOO-FAKE-9", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.get_order_for_edit(si.name)
		si2 = self._ship(self._stocked_serial("EO-GD3"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si2.name,
			"ffl_destination_ffl", "", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.get_order_for_edit(si2.name)

	def test_refuses_shipstation_pushed(self):
		si = self._ship(self._stocked_serial("EO-GD4"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_order_id", "se-777", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.get_order_for_edit(si.name)

	def test_requires_roles(self):
		si = self._ship(self._stocked_serial("EO-GD5"), update_stock=0,
			buyer_type="Business")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				manual_order.get_order_for_edit(si.name)
		finally:
			frappe.set_user("Administrator")


class TestUpdateOrder(EditOrderFixtures):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# Test-module execution order is filesystem-dependent — don't assume
		# test_build_order's ManualModes has already created the Zelle Mode of
		# Payment (on the self-hosted runner it hadn't: DoesNotExistError).
		# Create it ourselves via the same idempotent prod setup path.
		from ffl_core.setup.payments import ensure_manual_modes_of_payment
		ensure_manual_modes_of_payment()
		# A fresh CI site has no Zelle Mode of Payment Account for FFL Test —
		# the exact gap that broke cancel-order's CI. Seed it so the
		# Zelle carry test runs everywhere, not just on the lived-in dev DB.
		if not frappe.db.exists("Mode of Payment Account",
				{"parent": "Zelle", "company": cls.company}):
			cash = frappe.db.get_value("Company", cls.company, "default_cash_account")
			mop = frappe.get_doc("Mode of Payment", "Zelle")
			mop.append("accounts", {"company": cls.company, "default_account": cash})
			mop.flags.ignore_permissions = True
			mop.save()

	def _edit(self, si, mutate=None, **kw):
		out = manual_order.get_order_for_edit(si.name)
		payload = out["payload"]
		if mutate:
			mutate(payload)
		return manual_order.update_order(si.name, payload, **kw)

	def test_unpaid_edit_rebuilds_and_cancels_old(self):
		serial = self._stocked_serial("EO-U1")
		si = self._ship(serial, update_stock=0, buyer_type="Business")
		out = self._edit(si, mutate=lambda p: p.__setitem__(
			"shipping_amount", 25))
		self.assertTrue(out["ok"])
		self.assertEqual(out["replaced"], si.name)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 2)
		new_si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(new_si.docstatus, 1)
		self.assertAlmostEqual(
			flt(getattr(new_si, "ffl_shipping_amount", 0)), 25, places=2)
		# The serial moved to the new invoice and stayed in stock.
		self.assertEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Active")
		self.assertIsNone(out["refund_entry"])
		self.assertAlmostEqual(out["carried_amount"], 0, places=2)

	def test_edit_keeps_the_orders_shipping_tax_basis(self):
		"""The rebuild keeps the freight tax
		basis the order was sold under, whatever FFL Settings says now — and the
		Edit prefill tells the page which one it is."""
		from ffl_core.tests.settings_sandbox import single_value
		for basis, switch in ((0, 1), (1, 0)):
			with self.subTest(basis=basis):
				si = self._ship(self._stocked_serial(f"EO-STI{basis}"), update_stock=0,
					buyer_type="Business")
				frappe.db.set_value("Sales Invoice", si.name,
					"ffl_shipping_tax_inclusive", basis, update_modified=False)
				self.assertEqual(manual_order.get_order_for_edit(si.name)
					["edit_ctx"]["shipping_tax_inclusive"], basis)
				with single_value("FFL Settings", "shipping_price_includes_tax", switch):
					out = self._edit(si, mutate=lambda p: p.__setitem__("shipping_amount", 45))
				self.assertEqual(frappe.db.get_value("Sales Invoice", out["sales_invoice"],
					"ffl_shipping_tax_inclusive"), basis)

	def test_edit_changes_ffl(self):
		from ffl_core.tests.test_build_order import DEALER_FFL
		si = self._ship(self._stocked_serial("EO-U2"), update_stock=0,
			buyer_type="Business")
		out = self._edit(si, mutate=lambda p: p.__setitem__(
			"destination_ffl", DEALER_FFL))
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "ffl_destination_ffl"),
			DEALER_FFL)

	def test_existing_staff_note_round_trips(self):
		"""A staff Order Note on the original invoice survives an edit: the Edit
		prefill (get_order_for_edit) reads ffl_staff_note into the payload and the
		rebuild re-writes it — the note travels with the order, not the old PK."""
		si = self._ship(self._stocked_serial("EO-N1"), update_stock=0,
			buyer_type="Business")
		note = "Hold until Friday — customer adding a suppressor."
		frappe.db.set_value("Sales Invoice", si.name, "ffl_staff_note", note,
			update_modified=False)
		out = self._edit(si)  # no mutation — pure carry
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "ffl_staff_note"), note)

	def test_edit_changes_staff_note(self):
		"""Editing the Order Note lands the new text on the rebuilt invoice — the
		field is editable during an edit, not frozen to its original value."""
		si = self._ship(self._stocked_serial("EO-N2"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name, "ffl_staff_note",
			"Old note.", update_modified=False)
		out = self._edit(si, mutate=lambda p: p.__setitem__(
			"notes", "New note — ship after 4473 clears."))
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "ffl_staff_note"),
			"New note — ship after 4473 clears.")

	def test_pe_paid_edit_carries_payment(self):
		# A dedicated, tax-exempt customer — NOT the shared INDIV — so the
		# raw _ship() fixture's untaxed total matches what create_order's
		# tax_exempt fallback (Customer.is_tax_exempt) recomputes on rebuild.
		# A real order is always built via create_order in the first place,
		# so its persisted exemption flag already agrees with its own total;
		# _ship() bypasses that persistence, so this test must set it up
		# itself (same technique test_inline_paid_edit_rebooks_tender uses).
		customer = self._indiv("EO PE Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		si = self._ship(self._stocked_serial("EO-P1"), update_stock=0,
			customer=customer)
		manual_order.record_payment(si.name, "Cash")  # pays in full
		si.reload()
		paid = flt(si.grand_total)
		pe = frappe.get_all("Payment Entry Reference",
			filters={"reference_name": si.name, "docstatus": 1},
			pluck="parent")[0]
		out = self._edit(si)
		# The ORIGINAL PE survives (real money) and now points at the new SI.
		self.assertEqual(
			frappe.db.get_value("Payment Entry", pe, "docstatus"), 1)
		self.assertTrue(frappe.db.exists("Payment Entry Reference",
			{"parent": pe, "reference_name": out["sales_invoice"],
				"docstatus": 1}))
		self.assertAlmostEqual(out["carried_amount"], paid, places=2)
		self.assertAlmostEqual(out["outstanding"], 0, places=2)

	def test_deposit_edit_total_up_leaves_balance(self):
		si = self._ship(self._stocked_serial("EO-P2"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=200)

		def add_accessory(p):
			p["items"].append({"item_code": "BO-ACC", "qty": 1, "rate": 50,
				"serials": []})
		out = self._edit(si, mutate=add_accessory)
		self.assertAlmostEqual(out["carried_amount"], 200, places=2)
		self.assertIsNone(out["refund_entry"])
		self.assertAlmostEqual(
			out["outstanding"], flt(out["grand_total"]) - 200, places=2)

	def test_overpay_refunds_delta(self):
		si = self._ship(self._stocked_serial("EO-P3"), update_stock=0)
		manual_order.record_payment(si.name, "Cash")
		si.reload()
		paid = flt(si.grand_total)

		def cheaper(p):
			p["items"][0]["rate"] = 500
		out = self._edit(si, mutate=cheaper, refund_mode="Cash")
		self.assertTrue(out["refund_entry"])
		self.assertAlmostEqual(
			out["refunded_amount"], paid - flt(out["grand_total"]), places=2)
		self.assertAlmostEqual(out["outstanding"], 0, places=2)
		pe = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(pe.payment_type, "Pay")

	def test_overpay_requires_refund_mode(self):
		si = self._ship(self._stocked_serial("EO-P4"), update_stock=0)
		manual_order.record_payment(si.name, "Cash")

		def cheaper(p):
			p["items"][0]["rate"] = 500
		with self.assertRaises(frappe.ValidationError):
			self._edit(si, mutate=cheaper)

	def test_zelle_deposit_reallocates_with_reference(self):
		si = self._ship(self._stocked_serial("EO-P5"), update_stock=0)
		manual_order.record_payment(si.name, "Zelle", amount=100,
			transaction_number="ZTX-EDIT-1")
		out = self._edit(si)
		pe = frappe.get_all("Payment Entry Reference",
			filters={"reference_name": out["sales_invoice"], "docstatus": 1},
			pluck="parent")[0]
		row = frappe.db.get_value("Payment Entry", pe,
			["mode_of_payment", "reference_no", "docstatus"], as_dict=True)
		self.assertEqual(row.docstatus, 1)
		self.assertEqual(row.mode_of_payment, "Zelle")
		self.assertEqual(row.reference_no, "ZTX-EDIT-1")

	def test_customer_swap_with_payment_refunds_original(self):
		si = self._ship(self._stocked_serial("EO-C1"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=300)
		old_customer = si.customer
		new_customer = self._indiv("EO Swap Buyer")

		def swap(p):
			p["customer"] = new_customer
		out = self._edit(si, mutate=swap, refund_mode="Cash")
		self.assertAlmostEqual(out["carried_amount"], 0, places=2)
		self.assertAlmostEqual(out["refunded_amount"], 300, places=2)
		refund = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(refund.party, old_customer)
		new_si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(new_si.customer, new_customer)
		self.assertAlmostEqual(
			flt(new_si.outstanding_amount), flt(new_si.grand_total), places=2)

	def test_customer_swap_unpaid_needs_no_refund_mode(self):
		si = self._ship(self._stocked_serial("EO-C2"), update_stock=0)
		new_customer = self._indiv("EO Swap Buyer 2")

		def swap(p):
			p["customer"] = new_customer
		out = self._edit(si, mutate=swap)
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "customer"), new_customer)

	def test_inline_paid_edit_rebooks_tender(self):
		"""A full inline (is_pos) payment reverses with the old invoice's
		cancel — the edit must re-book the same cash against the new invoice."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial("EO-I1")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("EO Inline Buyer"),
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		paid = flt(si.grand_total) - flt(si.outstanding_amount)
		self.assertGreater(paid, 0)
		out = self._edit(si)
		self.assertAlmostEqual(out["carried_amount"], paid, places=2)
		self.assertAlmostEqual(out["outstanding"], 0, places=2)
		# The re-booked tender is a real Receive PE on the new invoice.
		pes = frappe.get_all("Payment Entry Reference",
			filters={"reference_name": out["sales_invoice"], "docstatus": 1},
			pluck="parent")
		self.assertEqual(len(pes), 1)

	def test_inline_paid_with_change_rebooks_only_the_net(self):
		"""$700 cash handed over on a $600 order books $100 change. The cancel
		reverses the NET $600 the drawer kept, so the edit must re-book $600 —
		re-booking the gross $700 leaves cash $100 above the drawer and a
		phantom $100 advance on the customer."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial("EO-IC1")
		customer = self._indiv("EO Inline Change Buyer")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"paid_amount": 700,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertAlmostEqual(flt(si.change_amount), 100, places=2)
		net = sum(g.net for g in manual_order._inline_tender_gl(si))
		self.assertAlmostEqual(net, flt(si.grand_total), places=2)

		out = self._edit(si)
		self.assertAlmostEqual(out["outstanding"], 0, places=2)
		self.assertFalse(out["refund_entry"])
		pes = frappe.get_all("Payment Entry", filters={
			"party_type": "Customer", "party": customer,
			"payment_type": "Receive", "docstatus": 1, "reference_no": si.name,
		}, fields=["name", "paid_amount", "unallocated_amount"])
		self.assertEqual(len(pes), 1)
		self.assertAlmostEqual(flt(pes[0].paid_amount), flt(si.grand_total), places=2)
		self.assertAlmostEqual(flt(pes[0].unallocated_amount), 0, places=2)

	def test_update_refuses_disposed(self):
		si = self._ship(self._stocked_serial("EO-X1"), update_stock=0,
			buyer_type="Business")
		payload = manual_order.get_order_for_edit(si.name)["payload"]
		manual_order.dispose_order(si.name)
		with self.assertRaises(frappe.ValidationError):
			manual_order.update_order(si.name, payload)

	def test_overpay_then_reedit_succeeds(self):
		"""Regression: editing an order a SECOND time after the first edit
		refunded an overpay used to throw "Payment Entry has been modified
		after you pulled it" — _allocate_advances compared a stale pre-cancel
		snapshot against erpnext's live PE.unallocated_amount."""
		customer = self._indiv("EO Reedit Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		si = self._ship(self._stocked_serial("EO-RE1"), update_stock=0,
			customer=customer)
		manual_order.record_payment(si.name, "Cash")  # pays in full
		si.reload()
		paid = flt(si.grand_total)

		def cheaper(p):
			p["items"][0]["rate"] = 500
		out1 = self._edit(si, mutate=cheaper, refund_mode="Cash")
		self.assertTrue(out1["refund_entry"])
		net_credit = paid - flt(out1["refunded_amount"])

		si2 = frappe.get_doc("Sales Invoice", out1["sales_invoice"])

		def restore(p):
			p["items"][0]["rate"] = 900
		out2 = self._edit(si2, mutate=restore)  # must NOT throw
		self.assertTrue(out2["ok"])
		self.assertAlmostEqual(out2["carried_amount"], net_credit, places=2)
		self.assertAlmostEqual(
			out2["outstanding"], flt(out2["grand_total"]) - net_credit, places=2)

	def test_failed_edit_rolls_back(self):
		"""update_order makes NO frappe.db.commit() — a throw mid-edit (here,
		an overpay with no refund_mode, which only surfaces AFTER the old SI
		is already cancelled and the new one already built) must leave the
		ORIGINAL order fully intact once the caller rolls back, exactly like
		a real request handler does on an unhandled throw."""
		customer = self._indiv("EO Rollback Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		serial = self._stocked_serial("EO-RB1")
		si = self._ship(serial, update_stock=0, customer=customer)
		manual_order.record_payment(si.name, "Cash")  # pays in full
		frappe.db.commit()  # the fixture must survive the rollback below
		# Snapshot right before the risky edit, not "assert count == 1" —
		# this test commits real residue every run (by design, so the
		# rollback below has something honest to undo), so on the
		# persistent dev DB a rerun already has an earlier run's invoice
		# for this same dedicated customer. The invariant under test is
		# "the failed edit added nothing", which a before/after diff
		# captures correctly regardless of what earlier runs left behind.
		before = set(frappe.get_all("Sales Invoice",
			filters={"customer": customer, "docstatus": 1}, pluck="name"))

		def cheaper(p):
			p["items"][0]["rate"] = 500
		with self.assertRaises(frappe.ValidationError):
			self._edit(si, mutate=cheaper)  # overpay, no refund_mode
		frappe.db.rollback()

		self.assertEqual(
			frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)
		self.assertTrue(frappe.db.exists("Payment Entry Reference",
			{"reference_name": si.name, "docstatus": 1}))
		after = set(frappe.get_all("Sales Invoice",
			filters={"customer": customer, "docstatus": 1}, pluck="name"))
		self.assertEqual(after, before)

	def test_customer_swap_with_inline_refunds_original(self):
		"""Customer swap on an inline (is_pos)-paid order: the reversed tender
		must re-book as a pure advance on the ORIGINAL customer (not the new
		one) before the full refund nets it out — otherwise the cash GL would
		fall below what's physically in the drawer."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial("EO-CI1")
		old_customer = self._indiv("EO CSwap Inline Old")
		new_customer = self._indiv("EO CSwap Inline New")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": old_customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		paid = flt(si.grand_total) - flt(si.outstanding_amount)

		def swap(p):
			p["customer"] = new_customer
		out = self._edit(si, mutate=swap, refund_mode="Cash")
		self.assertTrue(out["refund_entry"])
		self.assertAlmostEqual(out["refunded_amount"], paid, places=2)
		refund = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(refund.party, old_customer)
		self.assertAlmostEqual(refund.paid_amount, paid, places=2)

		# The re-booked tender: a Receive PE, party = ORIGINAL customer, with
		# no Sales Invoice references — a pure advance the refund above nets.
		# Scoped by reference_no = the ORIGINAL SI's own (freshly auto-named,
		# so rerun-unique) name — _rebook_inline_tender always stamps that as
		# the Cash tender's reference_no — not just by customer: create_order's
		# submit chain commits for real (FastBound/receipt hooks), so a
		# customer-only filter accumulates residue from earlier runs on this
		# persistent dev DB.
		rebooked = frappe.get_all("Payment Entry", filters={
			"party_type": "Customer", "party": old_customer,
			"payment_type": "Receive", "docstatus": 1, "reference_no": si.name,
		}, pluck="name")
		self.assertEqual(len(rebooked), 1)
		self.assertFalse(frappe.db.exists("Payment Entry Reference",
			{"parent": rebooked[0], "reference_doctype": "Sales Invoice"}))

		new_si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(new_si.customer, new_customer)
		self.assertAlmostEqual(
			flt(new_si.outstanding_amount), flt(new_si.grand_total), places=2)

	def test_inline_overpay_refunds_delta(self):
		"""An inline tender re-books at its FULL original amount even when the
		edit shrinks the order below it — the allocation caps at the new
		invoice's outstanding, the remainder rides as an advance on the new
		Receive PE, and the overpay refund nets it out."""
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial("EO-IO1")
		customer = self._indiv("EO Inline Overpay Buyer")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		paid = flt(si.grand_total) - flt(si.outstanding_amount)

		def cheaper(p):
			p["items"][0]["rate"] = 200
		out = self._edit(si, mutate=cheaper, refund_mode="Cash")
		self.assertTrue(out["refund_entry"])
		self.assertAlmostEqual(
			out["refunded_amount"], paid - flt(out["grand_total"]), places=2)
		self.assertAlmostEqual(out["outstanding"], 0, places=2)

		# Scoped by reference_no = si.name too — see the identical note in
		# test_customer_swap_with_inline_refunds_original.
		pes = frappe.get_all("Payment Entry", filters={
			"party_type": "Customer", "party": customer,
			"payment_type": "Receive", "docstatus": 1, "reference_no": si.name,
		}, pluck="name")
		self.assertEqual(len(pes), 1)
		pe = frappe.get_doc("Payment Entry", pes[0])
		self.assertAlmostEqual(pe.paid_amount, paid, places=2)
		allocated_sum = sum(flt(r.allocated_amount) for r in pe.references)
		self.assertLess(allocated_sum, pe.paid_amount)

	def test_ffl_state_change_retaxes_and_surfaces_delta(self):
		"""Editing the destination FFL to an out-of-state one re-taxes the
		rebuild through the SAME interstate-tax engine create_order always
		runs (apply_interstate_tax, keyed on the shipping address) — the
		total can legitimately drop, and that delta must surface as a refund
		like any other total-decrease edit."""
		az_ffl = "1-99-999-01-9Z-88888"
		if not frappe.db.exists("ATF FFL Record", az_ffl):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": az_ffl,
				"licensee_name": "Test FFL 88888",
				"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
			}).insert(ignore_permissions=True)
		frappe.db.set_value("ATF FFL Record", az_ffl, {
			"street": "200 Desert Rd", "city": "Phoenix", "state": "AZ",
			"zip": "85001",
			"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
		}, update_modified=False)

		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial("EO-TX1")
		# NOT tax-exempt — origin (DEST_FFL, TX, in-state) is taxed exactly
		# as the rebuild will be, so the ONLY thing that can move the total
		# is the destination-state swap below, not a stale exemption flag.
		customer = self._indiv("EO FFL State Buyer")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash",
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		old_grand = flt(si.grand_total)

		def move(p):
			p["destination_ffl"] = az_ffl
		out = self._edit(si, mutate=move, refund_mode="Cash")
		self.assertLess(flt(out["grand_total"]), old_grand)
		self.assertAlmostEqual(
			out["refunded_amount"], old_grand - flt(out["grand_total"]), places=2)

	def test_edit_does_not_requeue_receipt(self):
		"""An edit rebuilds the invoice, but its on_submit must NOT re-enqueue the
		sale receipt — staff re-send deliberately from the SI. Asserted at the
		enqueue boundary (send_sale_receipt enqueues in-process during submit), so
		it's deterministic without an after-commit worker run."""
		from unittest.mock import patch
		customer = self._indiv("EO Receipt Buyer")
		manual_order.set_customer_tax_exempt(customer, 1)
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial(f"EO-RCPT-{frappe.generate_hash(length=8)}")
		receipt_enqueues = []

		def spy(method=None, *a, **kw):
			if method == "ffl_core.receipts.send_sale_receipt_task":
				receipt_enqueues.append(kw.get("name"))
			return None  # swallow: no background job fires during the test

		payload = {
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 0, "mode_of_payment": "Cash", "tax_exempt": 1,
			"contact_email": "eo-receipt@example.com",
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		}
		with patch.object(frappe, "enqueue", side_effect=spy):
			created = manual_order.create_order(payload)
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		# Baseline: a normal build DOES queue the receipt — proves the spy fires
		# and the edit assertion below is a real discriminator, not vacuous.
		self.assertIn(si.name, receipt_enqueues)

		receipt_enqueues.clear()
		with patch.object(frappe, "enqueue", side_effect=spy):
			out = self._edit(si)
		self.assertTrue(out["ok"])
		# The rebuilt invoice queued NO receipt.
		self.assertEqual(receipt_enqueues, [])

	def test_customer_swap_blanks_stale_email(self):
		"""A buyer swap must not carry the ORIGINAL buyer's email onto the rebuilt
		invoice (else a deliberate re-send would address the wrong person). The
		stale carry is blanked server-side; the swap refund still books."""
		old_email = "eo-stale-old@example.com"
		customer = self._indiv("EO Stale Email Old")
		manual_order.set_customer_tax_exempt(customer, 1)
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial(f"EO-STALE-{frappe.generate_hash(length=8)}")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 0, "mode_of_payment": "Cash", "tax_exempt": 1,
			"contact_email": old_email,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertEqual((si.contact_email or ""), old_email)
		manual_order.record_payment(si.name, "Cash", amount=300)
		new_customer = self._indiv("EO Stale Email New")
		manual_order.set_customer_tax_exempt(new_customer, 1)

		def swap(p):
			p["customer"] = new_customer
			# Prefill carried the OLD buyer's email — the stale carry under test.
			self.assertEqual((p.get("contact_email") or ""), old_email)
		out = self._edit(si, mutate=swap, refund_mode="Cash")
		# Regression guard: the swap refund still books.
		self.assertTrue(out["refund_entry"])
		self.assertAlmostEqual(out["refunded_amount"], 300, places=2)
		# The stale email did NOT ride onto the rebuilt invoice.
		new_email = (frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "contact_email") or "")
		self.assertNotEqual(new_email.lower(), old_email.lower())


class TestEditCarryPayment(EditOrderFixtures):
	"""Buyer changed on a paid order and staff chose "Carry payment to new buyer":
	each receipt moves to the new buyer on its own day (the Receive PE is cancelled
	and amended — the card charge row follows it), the rebuilt invoice is settled,
	and nothing is refunded except a real overpayment, the way it was paid."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# A fresh CI site has no card account for FFL Test (see TestUpdateOrder's Zelle note).
		from ffl_core.setup.payments import ensure_manual_modes_of_payment
		ensure_manual_modes_of_payment()
		if not frappe.db.exists("Mode of Payment Account",
				{"parent": "Credit Card", "company": cls.company}):
			mop = frappe.get_doc("Mode of Payment", "Credit Card")
			mop.append("accounts", {"company": cls.company, "default_account":
				frappe.db.get_value("Company", cls.company, "default_cash_account")})
			mop.flags.ignore_permissions = True
			mop.save()

	def _swap(self, si, new_customer, rate=None, **kw):
		payload = manual_order.get_order_for_edit(si.name)["payload"]
		payload["customer"] = new_customer
		if rate is not None:
			payload["items"][0]["rate"] = rate
		return manual_order.update_order(si.name, payload, carry_payment=1, **kw)

	def _paid_order(self, tag, mode="Cash", amount: float | None = 300, reference=None):
		si = self._ship(self._stocked_serial(f"EO-CY-{tag}-{frappe.generate_hash(length=6)}"),
			update_stock=0)
		pe = manual_order.record_payment(si.name, mode, amount=amount,
			transaction_number=reference)["payment_entry"]
		si.reload()
		return si, pe

	def _new_buyer(self, name):
		customer = self._indiv(name)
		manual_order.set_customer_tax_exempt(customer, 1)
		return customer

	def test_carry_amends_the_receipt_to_the_new_buyer(self):
		si, pe = self._paid_order("PE")
		original = frappe.db.get_value("Payment Entry", pe,
			["posting_date", "paid_to", "mode_of_payment"], as_dict=True)
		new_customer = self._new_buyer("EO Carry New")
		out = self._swap(si, new_customer)

		self.assertIsNone(out["refund_entry"])
		self.assertAlmostEqual(out["carried_amount"], 300, places=2)
		self.assertAlmostEqual(out["outstanding"], flt(out["grand_total"]) - 300, places=2)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
		amended = frappe.get_doc("Payment Entry", {"amended_from": pe, "docstatus": 1})
		self.assertEqual(amended.party, new_customer)
		# Same day, amount, tender and account — only the party moved.
		self.assertEqual(amended.posting_date, original.posting_date)
		self.assertEqual(amended.paid_to, original.paid_to)
		self.assertEqual(amended.mode_of_payment, original.mode_of_payment)
		self.assertAlmostEqual(amended.paid_amount, 300, places=2)
		self.assertEqual([(r.reference_name, flt(r.allocated_amount)) for r in amended.references],
			[(out["sales_invoice"], 300.0)])
		# Who really paid stays on the receipt.
		self.assertIn(si.customer, amended.remarks)
		self.assertIn(pe, amended.remarks)
		# No refund was booked to anyone.
		self.assertFalse(frappe.db.exists("Payment Entry", {"payment_type": "Pay", "docstatus": 1,
			"remarks": ("like", f"Refund for cancelled {si.name}.%")}))

	def test_carry_moves_the_card_charge_with_its_receipt(self):
		if not frappe.db.exists("DocType", "Card Terminal Payment"):
			self.skipTest("card payments app not installed")
		si, pe = self._paid_order("CARD", mode="Credit Card", reference="AUTH-CARRY-1")
		card = frappe.get_doc({"doctype": "Card Terminal Payment", "sales_invoice": si.name,
			"payment_entry": pe, "amount": 300, "status": "CHARGED", "processor": "payroc",
			"unique_reference": f"EO-CARRY-{frappe.generate_hash(length=8)}"})
		card.insert(ignore_permissions=True)
		out = self._swap(si, self._new_buyer("EO Carry Card New"))

		amended = frappe.db.get_value("Payment Entry", {"amended_from": pe, "docstatus": 1}, "name")
		row = frappe.db.get_value("Card Terminal Payment", card.name,
			["payment_entry", "sales_invoice", "status"], as_dict=True)
		self.assertEqual((row.payment_entry, row.sales_invoice, row.status),
			(amended, out["sales_invoice"], "CHARGED"))
		# The daily Payroc reconcile still expects the capture held: no Cancel
		# Order-style refund sits on either invoice.
		refund_of = frappe.get_attr("ffl_integrations.payroc.reconcile._cancel_order_refund")
		self.assertEqual((refund_of(si.name), refund_of(out["sales_invoice"])), (0, 0))

	def test_carry_is_refused_when_the_card_was_refunded(self):
		if not frappe.db.exists("DocType", "Card Terminal Payment"):
			self.skipTest("card payments app not installed")
		si, pe = self._paid_order("CARDR", mode="Credit Card", reference="AUTH-CARRY-2")
		frappe.get_doc({"doctype": "Card Terminal Payment", "sales_invoice": si.name,
			"payment_entry": pe, "amount": 300, "refunded_amount": 100, "status": "CHARGED",
			"processor": "payroc",
			"unique_reference": f"EO-CARRY-{frappe.generate_hash(length=8)}"}).insert(ignore_permissions=True)
		self.assertTrue(manual_order.get_order_for_edit(si.name)["edit_ctx"]["carry_block"])
		with self.assertRaises(frappe.ValidationError):
			self._swap(si, self._new_buyer("EO Carry Refused New"))
		# Refused before any change.
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 1)

	def test_carry_is_refused_once_the_receipt_is_bank_matched(self):
		si, pe = self._paid_order("BANK")
		self.assertIsNone(manual_order.get_order_for_edit(si.name)["edit_ctx"]["carry_block"])
		frappe.db.set_value("Payment Entry", pe, "clearance_date", frappe.utils.nowdate(),
			update_modified=False)
		self.assertTrue(manual_order.get_order_for_edit(si.name)["edit_ctx"]["carry_block"])
		with self.assertRaises(frappe.ValidationError):
			self._swap(si, self._new_buyer("EO Carry Bank New"))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)

	def test_carry_overpay_refunds_the_original_payer_the_way_it_was_paid(self):
		si, _pe = self._paid_order("OVER", amount=None)  # pays in full
		paid = flt(si.grand_total)
		new_customer = self._new_buyer("EO Carry Over New")
		# Another tender than the one the payer used is refused — after the
		# rebuild, so the request's rollback (a savepoint here) undoes the edit.
		frappe.db.savepoint("carry_wrong_tender")
		with self.assertRaises(frappe.ValidationError):
			self._swap(si, new_customer, rate=500, refund_mode="Zelle",
				refund_reference="ZTX-CARRY-1")
		frappe.db.rollback(save_point="carry_wrong_tender")

		out = self._swap(frappe.get_doc("Sales Invoice", si.name), new_customer, rate=500,
			refund_mode="Cash")
		self.assertAlmostEqual(out["outstanding"], 0, places=2)
		self.assertAlmostEqual(out["refunded_amount"], paid - flt(out["grand_total"]), places=2)
		refund = frappe.get_doc("Payment Entry", out["refund_entry"])
		# The credit sits with the new buyer after the carry; the remark names who
		# the money actually goes back to.
		self.assertEqual(refund.party, new_customer)
		self.assertIn("original payer", refund.remarks)
		self.assertIn(si.customer, refund.remarks)

	def test_carry_rebooks_inline_tender_to_the_new_buyer(self):
		pos = frappe.db.get_value("POS Profile", {"company": self.company, "disabled": 0}, "name")
		serial = self._stocked_serial(f"EO-CYI-{frappe.generate_hash(length=6)}")
		old_customer = self._new_buyer("EO Carry Inline Old")
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": old_customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600, "serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		new_customer = self._new_buyer("EO Carry Inline New")
		out = self._swap(si, new_customer)

		self.assertIsNone(out["refund_entry"])
		self.assertAlmostEqual(out["outstanding"], 0, places=2)
		# Booked straight to the new buyer on the sale's own day — never an advance
		# on the old buyer.
		self.assertFalse(frappe.db.exists("Payment Entry", {"party": old_customer,
			"docstatus": 1, "reference_no": si.name}))
		pe = frappe.get_doc("Payment Entry", {"party": new_customer, "docstatus": 1,
			"payment_type": "Receive", "reference_no": si.name})
		self.assertEqual(pe.posting_date, si.posting_date)
		self.assertIn("inline counter tender", pe.remarks)
		self.assertIn(old_customer, pe.remarks)

	def test_a_carried_order_edits_again_like_any_other(self):
		si, _pe = self._paid_order("RE")
		out1 = self._swap(si, self._new_buyer("EO Carry Reedit New"))
		out2 = manual_order.update_order(out1["sales_invoice"],
			manual_order.get_order_for_edit(out1["sales_invoice"])["payload"])
		self.assertIsNone(out2["refund_entry"])
		self.assertAlmostEqual(out2["carried_amount"], 300, places=2)


class TestEditSerialRevalidation(EditOrderFixtures):
	def test_serial_check_ignores_own_invoice(self):
		"""The Edit prefill re-validates an order's OWN serial lines through the
		draft-resume checker; a serial held only by the invoice being edited must
		stay available to it (ignore_invoice), or the gun is silently dropped from
		the rebuilt order. Covers find_serial_for_order (scan-to-add) too.

		Serial is unique per run: _ship commits (FastBound/receipt hooks), so on
		the persistent dev DB a fixed name would accumulate holders across runs and
		the held_map ref the ignore_invoice check pivots on would go ambiguous."""
		serial = self._stocked_serial(f"EO-OWN-{frappe.generate_hash(length=8)}")
		si = self._ship(serial, update_stock=0, buyer_type="Business")
		as_json = f'["{serial}"]'
		# Held by its own submitted order → unavailable in the plain draft check.
		self.assertFalse(
			manual_order.check_serials_available(as_json)[serial]["ok"])
		# ...but the edit flow exempts the order's own hold.
		self.assertTrue(
			manual_order.check_serials_available(
				as_json, ignore_invoice=si.name)[serial]["ok"])
		# The client sends a JSON string; a raw list must behave identically.
		self.assertTrue(
			manual_order.check_serials_available(
				[serial], ignore_invoice=si.name)[serial]["ok"])
		# Scan-to-add (throwing path) honors it too — staff may delete then re-scan
		# the same gun mid-edit.
		self.assertEqual(
			manual_order.find_serial_for_order(
				serial, ignore_invoice=si.name)["serial"],
			serial)
		# ...and a plain scan still throws (held by the order).
		with self.assertRaises(frappe.ValidationError):
			manual_order.find_serial_for_order(serial)


class TestRenameCustomer(EditOrderFixtures):
	def test_renames_doc_and_refreshes_snapshots(self):
		# Clean up any residue from a prior run on the persistent dev DB.
		for name in ("EO Mispeled Person", "EO Correct Person"):
			try:
				if frappe.db.exists("Customer", name):
					frappe.delete_doc("Customer", name, force=1)
			except:
				pass
		cust = self._indiv("EO Mispeled Person")
		si = self._ship(self._stocked_serial("EO-R1"), update_stock=0,
			customer=cust)
		out = manual_order.rename_customer(cust, "EO Correct Person")
		self.assertEqual(out["name"], "EO Correct Person")
		self.assertFalse(frappe.db.exists("Customer", "EO Mispeled Person"))
		self.assertEqual(frappe.db.get_value(
			"Customer", out["name"], "customer_name"), "EO Correct Person")
		# rename_doc migrated the link; the display snapshot refreshed too.
		row = frappe.db.get_value("Sales Invoice", si.name,
			["customer", "customer_name"], as_dict=True)
		self.assertEqual(row.customer, "EO Correct Person")
		self.assertEqual(row.customer_name, "EO Correct Person")

	def test_series_named_customer_updates_name_only(self):
		# When the docname no longer mirrors customer_name (an ERPNext series- or
		# autoname-created customer), rename_doc is SKIPPED: only the field and the
		# open-document snapshots move, the docname (and every link) stays put.
		# Clean up any residue from a prior run on the persistent dev DB.
		for name in ("EO Series Person", "EO Series Final"):
			try:
				if frappe.db.exists("Customer", name):
					frappe.delete_doc("Customer", name, force=1)
			except:
				pass
		cust = self._indiv("EO Series Person")
		# Drift the display name off the docname, as a series-named customer is.
		frappe.db.set_value("Customer", cust, "customer_name",
			"EO Series Person Typo", update_modified=False)
		si = self._ship(self._stocked_serial("EO-R2"), update_stock=0,
			customer=cust)
		out = manual_order.rename_customer(cust, "EO Series Final")
		# docname untouched (no rename), no doc minted under the new name...
		self.assertEqual(out["name"], "EO Series Person")
		self.assertTrue(frappe.db.exists("Customer", "EO Series Person"))
		self.assertFalse(frappe.db.exists("Customer", "EO Series Final"))
		# ...but the field and the open SI snapshot both carry the corrected name.
		self.assertEqual(frappe.db.get_value(
			"Customer", "EO Series Person", "customer_name"), "EO Series Final")
		row = frappe.db.get_value("Sales Invoice", si.name,
			["customer", "customer_name"], as_dict=True)
		self.assertEqual(row.customer, "EO Series Person")
		self.assertEqual(row.customer_name, "EO Series Final")

	def test_requires_name(self):
		# Clean up any residue from a prior run on the persistent dev DB.
		try:
			if frappe.db.exists("Customer", "EO Rename Guard"):
				frappe.delete_doc("Customer", "EO Rename Guard", force=1)
		except:
			pass
		cust = self._indiv("EO Rename Guard")
		with self.assertRaises(frappe.ValidationError):
			manual_order.rename_customer(cust, "   ")

	def test_requires_roles(self):
		# Clean up any residue from a prior run on the persistent dev DB.
		try:
			if frappe.db.exists("Customer", "EO Rename Role"):
				frappe.delete_doc("Customer", "EO Rename Role", force=1)
		except:
			pass
		cust = self._indiv("EO Rename Role")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				manual_order.rename_customer(cust, "X")
		finally:
			frappe.set_user("Administrator")


if __name__ == "__main__":
	unittest.main()
