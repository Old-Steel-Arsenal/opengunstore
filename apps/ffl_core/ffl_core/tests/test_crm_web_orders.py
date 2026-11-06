"""A web order on its customer's open CRM deal: a note per event, once, and never a new deal."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.crm import web_orders
from ffl_core.tests.crm_support import CRMTestCase

EMAIL = "web-order-buyer@example.org"


def _contact() -> str:
	return frappe.get_doc({"doctype": "Contact", "first_name": "Web", "email_ids": [{"email_id": EMAIL, "is_primary": 1}]}
		).insert(ignore_permissions=True).name


def _deal(contact: str, status: str = "New") -> str:
	return frappe.get_doc({"doctype": "CRM Deal", "status": status, "contacts": [{"contact": contact, "is_primary": 1}]}
		).insert(ignore_permissions=True).name


def _order() -> str:
	doc = frappe.new_doc(web_orders.ORDER)
	doc.woo_order_id = 987654
	doc.customer_email = EMAIL
	doc.order_total = 125.5
	doc.currency = "USD"
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.db_insert()
	return doc.name


def _notes(deal: str) -> list[str]:
	return frappe.get_all(web_orders.NOTE, filters={"reference_doctype": "CRM Deal", "reference_docname": deal},
		pluck="title", order_by="creation asc")


class TestWebOrderNotes(CRMTestCase):
	def setUp(self):
		super().setUp()
		self.patch = patch.object(web_orders, "pos_link_on", return_value=True)
		self.patch.start()
		self.addCleanup(self.patch.stop)
		self.order = _order()

	def test_each_event_leaves_one_note(self):
		deal = _deal(_contact())
		for event in (web_orders.PLACED, web_orders.PLACED, web_orders.TRANSFERRED, web_orders.CANCELLED):
			web_orders.note_web_order(self.order, event)
		self.assertEqual(_notes(deal), ["Web order 987654 placed", "Web order 987654 shipped or picked up",
			"Web order 987654 cancelled"])

	def test_two_refunds_are_two_notes_with_their_amounts(self):
		deal = _deal(_contact())
		web_orders.note_web_order(self.order, web_orders.REFUNDED, key="11", amount=10)
		web_orders.note_web_order(self.order, web_orders.REFUNDED, key="12", amount=20)
		web_orders.note_web_order(self.order, web_orders.REFUNDED, key="12", amount=20)
		self.assertEqual(len(_notes(deal)), 2)
		self.assertIn("20", frappe.db.get_value(web_orders.NOTE, {"reference_docname": deal, "title": ["like", "%(12)"]}, "content"))

	def test_no_open_deal_means_no_note_and_no_deal(self):
		_contact()
		deals = frappe.db.count("CRM Deal")
		web_orders.note_web_order(self.order, web_orders.PLACED)
		self.assertEqual(frappe.db.count("CRM Deal"), deals)
		self.assertFalse(frappe.db.exists(web_orders.NOTE, {"title": "Web order 987654 placed"}))

	def test_the_switch_off_means_no_note(self):
		deal = _deal(_contact())
		with patch.object(web_orders, "pos_link_on", return_value=False):
			web_orders.note_web_order(self.order, web_orders.PLACED)
		self.assertFalse(_notes(deal))

	def test_a_failure_never_reaches_the_order(self):
		_deal(_contact())
		with patch.object(web_orders, "_open_deal", side_effect=RuntimeError("no")):
			web_orders.note_web_order(self.order, web_orders.PLACED)

	def test_marking_an_order_transferred_leaves_the_note(self):
		from ffl_woo_sync.ffl_woo_sync.doctype.woo_online_order.woo_online_order import mark_transferred

		deal = _deal(_contact())
		mark_transferred(self.order)
		self.assertEqual(_notes(deal), ["Web order 987654 shipped or picked up"])
