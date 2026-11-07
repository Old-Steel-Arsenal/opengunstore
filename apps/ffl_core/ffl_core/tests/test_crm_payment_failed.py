"""A web order whose payment failed (ffl_core.crm.web_orders, the Woo webhook): a CRM follow-up so the
sale can be won back, closed when the customer pays. Real webhook handler, real CRM documents; only the
Woo REST fallback the handler tries for an order's FFL is stood in for."""
from __future__ import annotations

import random
from unittest.mock import patch

import frappe

from ffl_core.crm import web_orders
from ffl_core.tests.crm_support import user
from ffl_core.tests.test_crm_deal_pos import OWNER, DealPOSCase, _deal, _switch
from ffl_woo_sync.woocommerce import webhook

EMAIL = "pay-failed-buyer@example.org"
STORE = "https://shop.example.test"


def _id() -> str:
	return str(random.randint(10**8, 10**9))


def _body(order_id: str, status: str, email: str = EMAIL, **over) -> dict:
	body = {"id": order_id, "status": status, "total": "125.50", "currency": "USD", "order_key": "wc_order_k3y",
		"date_paid": None, "created_via": "checkout", "payment_method_title": "Credit card", "line_items": [{"name": "Range Bag", "quantity": 2}],
		"billing": {"first_name": "Pat", "last_name": "Buyer", "email": email, "phone": "555-0100"}}
	body.update(over)
	return body


def _tasks(deal: str | None = None) -> list:
	filters = {"title": ["like", web_orders._TASK_LIKE]}
	if deal:
		filters.update(reference_doctype="CRM Deal", reference_docname=deal)
	return frappe.get_all(web_orders.TASK, filters=filters, fields=["name", "title", "status", "description", "reference_docname"])


def _deals_of(email: str) -> list[str]:
	contacts = web_orders.contacts_with_email(email)
	return frappe.get_all("CRM Contacts", filters={"parenttype": "CRM Deal", "contact": ["in", list(contacts) or [""]]}, pluck="parent")


def _notes(deal: str) -> list[str]:
	return frappe.get_all(web_orders.NOTE, filters={"reference_doctype": "CRM Deal", "reference_docname": deal},
		pluck="title", order_by="creation asc")


class TestPaymentFailed(DealPOSCase):
	def setUp(self):
		super().setUp()
		p = patch.object(webhook, "_fetch_order_ffl", return_value=webhook._extract_ffl([]))
		p.start()
		self.addCleanup(p.stop)
		frappe.db.set_single_value("WooCommerce Settings", "store_url", STORE)
		user(OWNER, "Sales User")
		self.order = _id()

	def _send(self, body: dict) -> None:
		with webhook.woo_site("retail"):
			webhook._handle_order_upsert(body)

	def test_a_failed_order_gets_a_deal_a_note_and_one_task(self):
		self._send(_body(self.order, "failed"))
		deals = _deals_of(EMAIL)
		self.assertEqual(len(deals), 1, "a customer with no deal gets one")
		deal = deals[0]
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "status"), "New")
		self.assertEqual(_notes(deal), [f"Web order {self.order} payment failed"])
		note = frappe.db.get_value(web_orders.NOTE, {"reference_docname": deal}, "content")
		for part in ("125.5", "2 x Range Bag", "Credit card", "555-0100",
				f"{STORE}/checkout/order-pay/{self.order}/?pay_for_order=true&amp;key=wc_order_k3y"):
			self.assertIn(part, note)
		(task,) = _tasks(deal)
		self.assertEqual(task.title, f"Payment failed on web order {self.order} — contact customer")
		self.assertEqual(task.status, "Todo")
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "CRM Deal", "reference_name": deal, "status": "Closed"}),
			"a deal nobody owns is the team's")

	def test_an_open_deal_is_used_and_its_owner_gets_the_task(self):
		contact = web_orders._contact_of(EMAIL, "Pat Buyer", "")
		deal = _deal(contact, owner=OWNER).name
		self._send(_body(self.order, "failed"))
		self.assertEqual(_deals_of(EMAIL), [deal])
		(task,) = _tasks(deal)
		self.assertEqual(frappe.db.get_value("ToDo", {"reference_type": web_orders.TASK, "reference_name": task.name,
			"status": "Open"}, "allocated_to"), OWNER)

	def test_an_unpaid_cancel_is_a_failure_a_paid_one_is_not(self):
		self._send(_body(self.order, "cancelled"))
		self.assertEqual(len(_tasks()), 1)
		paid = _id()
		self._send(_body(paid, "cancelled", email="paid-cancel@example.org", date_paid="2026-11-01T10:00:00"))
		self.assertFalse(_deals_of("paid-cancel@example.org"), "the shop cancelling a paid order is no payment failure")
		self.assertEqual(len(_tasks()), 1)

	def test_a_cancel_after_the_order_was_processing_is_not_a_failure(self):
		"""The paid time may be missing from the payload: POS knows the order was processing."""
		self._send(_body(self.order, "processing", email="was-paid@example.org"))
		self._send(_body(self.order, "cancelled", email="was-paid@example.org"))
		self.assertFalse(_deals_of("was-paid@example.org"))

	def test_an_order_that_is_not_dead_leaves_nothing(self):
		for status in ("pending", "on-hold", "checkout-draft"):
			self._send(_body(_id(), status, email="alive@example.org"))
		self.assertFalse(_deals_of("alive@example.org"))

	def test_a_redelivered_webhook_changes_nothing(self):
		self._send(_body(self.order, "failed"))
		self._send(_body(self.order, "failed"))
		self._send(_body(self.order, "cancelled"))   # the same order, cancelled after it failed
		(deal,) = _deals_of(EMAIL)
		self.assertEqual(len(_notes(deal)), 1)
		self.assertEqual(len(_tasks()), 1)

	def test_a_retry_that_fails_again_adds_to_the_one_task(self):
		second = _id()
		self._send(_body(self.order, "failed"))
		self._send(_body(second, "failed", total="130.00"))
		(deal,) = _deals_of(EMAIL)
		self.assertEqual(len(_notes(deal)), 2)
		(task,) = _tasks(deal)
		self.assertIn(self.order, task.title)
		self.assertIn(second, task.description)
		self.assertIn("130", task.description)

	def test_a_later_paid_order_closes_the_task(self):
		self._send(_body(self.order, "failed"))
		(deal,) = _deals_of(EMAIL)
		(task,) = _tasks(deal)
		paid = _id()
		self._send(_body(paid, "processing", date_paid="2026-11-01T10:00:00"))
		self.assertEqual(frappe.db.get_value(web_orders.TASK, task.name, "status"), "Done")
		self.assertIn(f"Recovered by web order {paid}", _notes(deal))
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": web_orders.TASK, "reference_name": task.name, "status": "Open"}))
		self._send(_body(paid, "completed"))
		self.assertEqual(_notes(deal).count(f"Recovered by web order {paid}"), 1)
		# A new failure after that (a newer order) is a new task.
		self._send(_body(str(int(paid) + 1), "failed"))
		self.assertEqual(sorted(t.status for t in _tasks(deal)), ["Done", "Todo"])

	def test_a_failed_order_that_is_paid_after_all_closes_its_own_task(self):
		self._send(_body(self.order, "pending"))
		self._send(_body(self.order, "failed"))
		(task,) = _tasks()
		self.assertEqual(task.status, "Todo")
		self._send(_body(self.order, "processing", date_paid="2026-11-01T10:00:00"))   # the reopen path
		self.assertEqual(frappe.db.get_value("Woo Online Order", {"woo_order_id": self.order}, "order_status"), "Pending Fulfillment")
		self.assertEqual(frappe.db.get_value(web_orders.TASK, task.name, "status"), "Done")

	def test_an_abandoned_order_cancelled_after_a_paid_retry_is_no_failure(self):
		# Card declined on the first order (it stays pending); the shopper pays a new one; Woo cancels the
		# abandoned first order an hour later. They already bought: no task.
		first = self.order
		self._send(_body(first, "pending"))
		self._send(_body(str(int(first) + 1), "processing", date_paid="2026-11-01T10:00:00"))
		self._send(_body(first, "cancelled"))
		self.assertEqual(_tasks(), [])
		self.assertFalse(frappe.db.exists(web_orders.NOTE, {"title": f"Web order {first} payment failed"}))

	def test_an_order_staff_made_in_woo_is_no_shopper_failure(self):
		self._send(_body(self.order, "cancelled", created_via="admin"))
		self._send(_body(_id(), "failed", created_via="rest-api"))
		self.assertEqual(_tasks(), [])

	def test_an_awaiting_stock_payment_closes_the_task(self):
		self._send(_body(self.order, "failed"))
		self._send(_body(str(int(self.order) + 1), "awaiting-stock", date_paid="2026-11-01T10:00:00"))
		self.assertEqual([t.status for t in _tasks()], ["Done"])

	def test_another_customers_payment_does_not_close_it(self):
		self._send(_body(self.order, "failed"))
		self._send(_body(_id(), "processing", email="someone-else@example.org"))
		self.assertEqual([t.status for t in _tasks()], ["Todo"])

	def test_an_invoice_made_from_the_deal_closes_the_task(self):
		self._send(_body(self.order, "failed"))
		(deal,) = _deals_of(EMAIL)
		self._sale(deal)
		self.assertEqual([t.status for t in _tasks(deal)], ["Done"])
		self.assertTrue(any(n.startswith("Recovered by invoice") for n in _notes(deal)))

	def test_the_switch_off_does_nothing(self):
		_switch(0)
		self._send(_body(self.order, "failed"))
		self.assertFalse(_deals_of(EMAIL))
		self.assertFalse(_tasks())
		task_before = frappe.db.count(web_orders.TASK)
		_switch(1)
		self._send(_body(_id(), "failed", email="late@example.org"))
		_switch(0)
		self._send(_body(_id(), "processing", email="late@example.org"))
		self.assertEqual(frappe.db.count(web_orders.TASK), task_before + 1, "a paid order closes nothing while the switch is off")
		self.assertEqual([t.status for t in _tasks()], ["Todo"])

	def test_a_failure_never_reaches_the_webhook(self):
		with patch.object(web_orders, "_open_deal", side_effect=RuntimeError("no")):
			self._send(_body(self.order, "failed"))
		self.assertFalse(_tasks())
		self.assertFalse(_deals_of(EMAIL), "rolled back to before it")

	def test_no_email_means_nothing_to_follow_up(self):
		self._send(_body(self.order, "failed", email=""))
		self.assertFalse(_tasks())


class TestPayLink(DealPOSCase):
	def test_woos_own_payment_url_wins_else_it_is_built_from_the_store_url(self):
		frappe.db.set_single_value("WooCommerce Settings", "store_url", STORE + "/")
		self.assertEqual(webhook._pay_link({"id": 7, "order_key": "k", "payment_url": "https://x.test/pay"}), "https://x.test/pay")
		self.assertEqual(webhook._pay_link({"id": 7, "order_key": "k"}), f"{STORE}/checkout/order-pay/7/?pay_for_order=true&key=k")
		self.assertEqual(webhook._pay_link({"id": 7}), "")
