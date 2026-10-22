"""draft_order_status — the facts behind the Recent Orders draft chips.

The labels themselves are mapped in the browser (pos_tender_guard.js, Node
tests); what is pinned here is the server's half: the right drafts, the right
raw facts, the firearm predicate the counter uses, and the gate.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.pos_order_status import MAX_INVOICES, draft_order_status
from ffl_core.tests.test_pending_4473_orders import Pending4473Fixtures

ANTIQUE = "POS-ST-ANTIQUE"


class DraftOrderStatus(Pending4473Fixtures, FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("Item", ANTIQUE):
			frappe.get_doc({
				"doctype": "Item", "item_code": ANTIQUE, "item_name": ANTIQUE,
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
				"serial_no_series": "POS-ST-ANTIQUE-.####",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", ANTIQUE, "is_antique", 1, update_modified=False)

	def _stamp(self, name: str, **values) -> None:
		# Straight to the row: a doc.save() is reverted by the money and 4473
		# field protection hooks (protect_money_scalars, protect_webhook_4473_fields).
		frappe.db.set_value("POS Invoice", name, values, update_modified=False)

	def _payment(self, name: str, mode: str, amount: float) -> None:
		row = frappe.get_doc({
			"doctype": "Sales Invoice Payment", "parent": name, "parenttype": "POS Invoice",
			"parentfield": "payments", "mode_of_payment": mode, "amount": amount,
			"idx": 90 + len(frappe.get_all("Sales Invoice Payment", filters={"parent": name})),
		})
		row.db_insert()

	def test_a_prefilled_cash_row_is_reported_as_a_row_not_as_money(self):
		name = self._draft_invoice("P4473-GUN", serial="POS-ST-S1")
		frappe.db.delete("Sales Invoice Payment", {"parent": name})
		self._payment(name, "Cash", 600)
		facts = draft_order_status([name])[name]
		self.assertEqual(facts["due"], 600)
		self.assertEqual(facts["charged"], 0)
		self.assertEqual(facts["held"], 0)
		self.assertEqual(facts["payments"], [{"mode_of_payment": "Cash", "amount": 600}])

	def test_card_capture_and_hold_stamps_come_through(self):
		name = self._draft_invoice("P4473-GUN", serial="POS-ST-S2")
		self._stamp(name, card_charged_amount=100, card_status="PARTIAL",
			ffl_payment_held=1, paid_amount=600)
		facts = draft_order_status([name])[name]
		self.assertEqual((facts["charged"], facts["card_status"], facts["held"], facts["paid_amount"]),
			(100, "PARTIAL", 1, 600))

	def test_a_web_order_pickup_ticket_is_flagged_paid_online(self):
		# ffl_woo_sync.woocommerce.pickup opens the ticket linked to its Woo
		# Online Order with the Woo tender as its payment row; the browser turns
		# `paid_online` into a confirmed Paid chip instead of "entered".
		ticket = self._draft_invoice("P4473-GUN", serial="POS-ST-S4")
		self._stamp(ticket, woo_online_order="WOO-ORD-TEST-PAID-ONLINE")
		plain = self._draft_invoice("P4473-ACC")
		facts = draft_order_status([ticket, plain])
		self.assertTrue(facts[ticket]["paid_online"])
		self.assertFalse(facts[plain]["paid_online"])

	def test_without_the_web_sync_column_paid_online_is_false_and_nothing_breaks(self):
		# ffl_woo_sync owns POS Invoice.woo_online_order; a site without it must
		# still get every other fact for the panel.
		from unittest.mock import patch
		name = self._draft_invoice("P4473-ACC")
		real = frappe.db.has_column
		with patch("frappe.db.has_column",
				side_effect=lambda dt, f: False if f == "woo_online_order" else real(dt, f)):
			facts = draft_order_status([name])[name]
		self.assertFalse(facts["paid_online"])
		self.assertEqual(facts["due"], 600)

	def test_rounded_total_is_what_is_due(self):
		name = self._draft_invoice("P4473-ACC")
		self._stamp(name, rounded_total=601)
		self.assertEqual(draft_order_status([name])[name]["due"], 601)

	def test_zero_amount_payment_rows_are_left_out(self):
		name = self._draft_invoice("P4473-ACC")
		frappe.db.delete("Sales Invoice Payment", {"parent": name})
		self._payment(name, "Cash", 0)
		self._payment(name, "Zelle", 25)
		self.assertEqual(draft_order_status([name])[name]["payments"],
			[{"mode_of_payment": "Zelle", "amount": 25}])

	def test_4473_fields_travel_with_the_firearm_flag(self):
		gun = self._draft_invoice("P4473-GUN", serial="POS-ST-S3", form_4473="Completed", nics="Delayed")
		accessory = self._draft_invoice("P4473-ACC")
		antique = self._draft_invoice(ANTIQUE, serial="POS-ST-A1")
		facts = draft_order_status([gun, accessory, antique])
		self.assertEqual((facts[gun]["has_firearm"], facts[gun]["form_4473_status"], facts[gun]["nics_status"]),
			(True, "Completed", "Delayed"))
		self.assertFalse(facts[accessory]["has_firearm"])
		self.assertFalse(facts[antique]["has_firearm"], "an antique needs no 4473")
		self.assertEqual(facts[accessory]["form_4473_status"], "N/A")

	def test_a_firearm_known_only_by_its_item_group_still_counts(self):
		name = self._draft_invoice("P4473-GUN", serial="POS-ST-S4")
		was = frappe.db.get_value("Item", "P4473-GUN", "is_firearm")
		frappe.db.set_value("Item", "P4473-GUN", "is_firearm", 0, update_modified=False)
		self.addCleanup(frappe.db.set_value, "Item", "P4473-GUN", "is_firearm", was, update_modified=False)
		self.assertTrue(draft_order_status([name])[name]["has_firearm"])

	def test_only_draft_sales_are_answered(self):
		draft = self._draft_invoice("P4473-ACC")
		submitted = self._draft_invoice("P4473-ACC")
		returned = self._draft_invoice("P4473-ACC")
		self._stamp(submitted, docstatus=1)
		self._stamp(returned, is_return=1)
		facts = draft_order_status([draft, submitted, returned, "ACC-PSINV-NOPE"])
		self.assertEqual(set(facts), {draft})

	def test_arguments_json_bare_duplicates_and_cap(self):
		name = self._draft_invoice("P4473-ACC")
		self.assertIn(name, draft_order_status(frappe.as_json([name, name])))
		self.assertIn(name, draft_order_status(name))
		self.assertEqual(draft_order_status([]), {})
		self.assertEqual(draft_order_status(frappe.as_json({"not": "a list"})), {})
		padding = [f"ACC-PSINV-PAD-{i}" for i in range(MAX_INVOICES)]
		self.assertIn(name, draft_order_status([name, *padding]))
		self.assertNotIn(name, draft_order_status([*padding, name]), "only the first 100 names are read")
