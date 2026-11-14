"""A web order's POS customer from the moment it is recorded (``Woo Online Order.customer``).

Real documents on the real database: the webhook's own intake (``_handle_order_created``), the
invoice-time resolver, the Deal page's customer card, the deal auto-link and the upgrade patch. Only
the job queue is replaced (to see what is queued, and to run it by hand). Every test runs inside the
savepoint CRMTestCase rolls back.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.crm import customer as deal_customer
from ffl_core.tests.crm_support import CRMTestCase
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin
from ffl_woo_sync.patches import backfill_web_order_customer as patch_module
from ffl_woo_sync.woocommerce import revenue, webhook

ORDER = "Woo Online Order"
SETTINGS = "Lead Mail Settings"
ACC = "BO-ACC"
FFL_META = [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}]


def _token() -> str:
	return frappe.generate_hash(length=10)


def _email() -> str:
	return f"web-{_token()}@example.com"


def _body(email: str, woo_id: str | None = None, **billing) -> dict:
	return {"id": woo_id or str(800000 + int(_token()[:5], 16)), "status": "processing",
		"billing": {"first_name": "Pat", "last_name": "Sample", "email": email, "phone": "555-0100", **billing},
		"line_items": [], "meta_data": FFL_META}


def _customer(email: str | None = None, **fields) -> str:
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": "Cust " + _token(),
		"customer_group": "Individual", "territory": "All Territories", "email_id": email or ""})
	doc.update(fields)
	doc.insert(ignore_permissions=True)
	return doc.name


def _alias(customer: str, email: str) -> None:
	doc = frappe.get_doc("Customer", customer)
	doc.append("customer_aliases", {"alias_type": "Email", "value": email})
	doc.save(ignore_permissions=True)


def _contact(email: str) -> str:
	doc = frappe.new_doc("Contact")
	doc.first_name = "Buyer " + _token()
	doc.append("email_ids", {"email_id": email, "is_primary": 1})
	return doc.insert(ignore_permissions=True).name


def _deal(email: str) -> str:
	doc = frappe.new_doc("CRM Deal")
	doc.status = "New"
	doc.append("contacts", {"contact": _contact(email), "is_primary": 1})
	return doc.insert(ignore_permissions=True).name


def _switch(on: int) -> None:
	frappe.db.set_single_value(SETTINGS, "crm_pos_link", on)


class _Queue:
	"""frappe.enqueue, recorded instead of sent."""

	def __init__(self):
		self.calls = []

	def __call__(self, method, **kwargs):
		self.calls.append((method, kwargs))

	def methods(self) -> list[str]:
		return [m for m, _k in self.calls]

	def run(self, method: str) -> None:
		for m, kwargs in list(self.calls):
			if m == method:
				frappe.get_attr(m)(**{k: v for k, v in kwargs.items() if k not in ("queue", "enqueue_after_commit")})


class WebOrderCustomerCase(BuildOrderMixin, CRMTestCase):
	def setUp(self):
		super().setUp()
		_switch(1)
		self.queue = _Queue()
		p = patch("frappe.enqueue", self.queue)
		p.start()
		self.addCleanup(p.stop)

	def _intake(self, email: str, **billing) -> frappe._dict:
		body = _body(email, **billing)
		webhook._handle_order_created(body)
		name = frappe.db.get_value(ORDER, {"woo_order_id": body["id"], "site": "retail"})
		self.assertTrue(name, "the order was recorded")
		return frappe.get_doc(ORDER, name)

	def _invoice(self, order: str, customer: str, *, submit: bool = True):
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer
		si.ffl_destination_ffl = DEST_FFL
		si.ffl_buyer_type = "Individual"
		si.woo_online_order = order
		si.update_stock = 0
		si.append("items", {"item_code": ACC, "qty": 1, "rate": 40})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		if submit:
			si.submit()
		return si


class IntakeGivesTheOrderItsCustomer(WebOrderCustomerCase):
	def test_a_new_buyer_gets_a_retail_customer(self):
		email = _email()
		order = self._intake(email)
		self.assertTrue(order.customer)
		row = frappe.db.get_value("Customer", order.customer,
			["email_id", "customer_name", "is_ffl_dealer", "is_tax_exempt", "customer_type"], as_dict=True)
		self.assertEqual(row.email_id, email)
		self.assertEqual(row.customer_name, "Pat Sample")
		self.assertEqual((row.is_ffl_dealer, row.is_tax_exempt, row.customer_type), (0, 0, "Individual"))

	def test_a_repeat_buyer_keeps_one_customer(self):
		email = _email()
		existing = _customer(email)
		first = self._intake(email.upper())   # the email lookup ignores case
		second = self._intake(email)
		self.assertEqual((first.customer, second.customer), (existing, existing))
		self.assertEqual(frappe.db.count("Customer", {"email_id": email}), 1)

	def test_a_merged_address_finds_its_customer(self):
		email = _email()
		owner = _customer(_email())
		_alias(owner, email)
		self.assertEqual(self._intake(email).customer, owner)

	def test_an_address_merged_into_a_dealer_is_not_the_dealer(self):
		email = _email()
		dealer = _customer(_email(), is_ffl_dealer=1)
		_alias(dealer, email)
		order = self._intake(email)
		self.assertNotEqual(order.customer, dealer)
		self.assertEqual(frappe.db.get_value("Customer", order.customer, "is_ffl_dealer"), 0)

	def test_an_order_with_no_email_gets_no_customer(self):
		before = frappe.db.count("Customer")
		order = self._intake("")
		self.assertFalse(order.customer)
		self.assertEqual(frappe.db.count("Customer"), before, "not even the shared web customer")
		self.assertNotIn(deal_customer.AUTO_LINK_JOB, self.queue.methods())

	def test_a_failing_resolver_never_stops_the_order(self):
		email = _email()
		partial = "Partial " + _token()

		def half_made(*args, **kwargs):
			frappe.get_doc({"doctype": "Customer", "customer_name": partial, "customer_group": "Individual",
				"territory": "All Territories"}).insert(ignore_permissions=True)
			raise RuntimeError("customer service down")

		with patch.object(revenue, "new_retail_customer", side_effect=half_made):
			order = self._intake(email)
		self.assertFalse(order.customer)
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": partial}), "the half-made write is undone")
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", f"Web order Woo #{order.woo_order_id}%"]}))
		# The invoice still finds the buyer.
		self.assertEqual(revenue._resolve_web_customer(order), frappe.db.get_value(ORDER, order.name, "customer"))

	def test_the_webhook_guest_can_make_the_customer(self):
		frappe.set_user("Guest")
		try:
			order = self._intake(_email())
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(order.customer)

	def test_no_email_and_no_push_to_the_shop(self):
		frappe.db.set_single_value("Marketing Settings", {"enabled": 1, "auto_subscribe_customers": 1})
		mails = frappe.db.count("Email Queue")
		with patch("ffl_woo_sync.woocommerce.customer_events.deliver") as push:
			order = self._intake(_email())
		self.assertTrue(order.customer)
		self.assertEqual(frappe.db.count("Email Queue"), mails)
		push.assert_not_called()
		self.assertFalse([m for m in self.queue.methods() if "customer_events" in m or "sendmail" in m])


class TheInvoiceUsesIt(WebOrderCustomerCase):
	def test_the_stored_customer_is_the_one_the_resolver_would_pick(self):
		email = _email()
		order = self._intake(email)
		from ffl_core.customer_profile import find_customer_by_email

		self.assertEqual(revenue._resolve_web_customer(order), order.customer)
		self.assertEqual(find_customer_by_email(email).name, order.customer)   # the old code's pick

	def test_the_stored_customer_does_not_decide(self):
		email = _email()
		order = self._intake(email)
		mine = order.customer
		other = _customer(_email())
		frappe.db.set_value(ORDER, order.name, "customer", other)
		order.reload()
		self.assertEqual(revenue._resolve_web_customer(order), mine)   # by the email, as before
		self.assertEqual(frappe.db.get_value(ORDER, order.name, "customer"), mine)   # written back

	def test_a_buyer_merged_into_a_dealer_is_not_booked_on_the_dealer(self):
		from ffl_core.customer_profile import merge_customer

		email = _email()
		order = self._intake(email)
		dealer = _customer(_email(), is_ffl_dealer=1)
		merge_customer(order.customer, dealer)   # writes the email alias on the dealer
		order.reload()
		self.assertEqual(order.customer, dealer, "the link followed the merge")
		booked = revenue._resolve_web_customer(order)
		self.assertNotEqual(booked, dealer)
		self.assertEqual(frappe.db.get_value("Customer", booked, "is_ffl_dealer"), 0)
		self.assertEqual(frappe.db.get_value("Customer", booked, "email_id"), email)
		self.assertEqual(frappe.db.get_value(ORDER, order.name, "customer"), booked)

	def test_a_customer_gone_since_is_found_again_and_written_back(self):
		email = _email()
		order = self._intake(email)
		frappe.delete_doc("Customer", order.customer, force=True, ignore_permissions=True)
		order.reload()
		found = revenue._resolve_web_customer(order)
		self.assertTrue(frappe.db.exists("Customer", found))
		self.assertEqual(frappe.db.get_value("Customer", found, "email_id"), email)
		self.assertEqual(frappe.db.get_value(ORDER, order.name, "customer"), found)

	def test_no_email_books_the_shared_customer_without_writing_it_back(self):
		order = self._intake("")
		shared = revenue._resolve_web_customer(order)
		self.assertEqual(frappe.db.get_value("Customer", shared, "customer_name"), revenue.WEB_CUSTOMER)
		self.assertFalse(frappe.db.get_value(ORDER, order.name, "customer"))

	def test_a_corrected_email_drops_the_stored_customer(self):
		order = self._intake(_email())
		new_email = _email()
		order.customer_email = new_email
		order.flags.ignore_permissions = True
		order.save()
		self.assertFalse(order.customer)
		found = revenue._resolve_web_customer(order)
		self.assertEqual(frappe.db.get_value("Customer", found, "email_id"), new_email)


class TheDealCardShowsIt(WebOrderCustomerCase):
	def _card(self, customer: str) -> dict:
		deal = frappe.get_doc("CRM Deal", _deal(_email()))
		frappe.db.set_value("CRM Deal", deal.name, "erpnext_customer", customer)
		deal.reload()
		return deal_customer.customer_summary(deal)

	def test_an_order_awaiting_fulfilment_is_listed(self):
		order = self._intake(_email())
		frappe.db.set_value(ORDER, order.name, {"woo_status": "on-hold", "order_total": 129.5})
		card = self._card(order.customer)
		self.assertEqual([r["name"] for r in card["web_orders"]], [order.name])
		self.assertEqual(card["web_orders"][0]["status"], "On hold")
		self.assertIn(order.name, card["html"])
		self.assertIn("Web order, On hold", card["html"])
		self.assertNotIn("No orders yet.", card["html"])

	def test_newer_invoiced_orders_do_not_hide_an_older_pending_one(self):
		email = _email()
		pending = self._intake(email)
		for _i in range(deal_customer.RECENT_ORDERS):
			order = self._intake(email)
			self._invoice(order.name, order.customer)
		card = self._card(pending.customer)
		self.assertEqual([r["name"] for r in card["web_orders"]], [pending.name])

	def test_an_invoiced_or_finished_order_is_not_listed_twice(self):
		invoiced = self._intake(_email())
		self._invoice(invoiced.name, invoiced.customer)
		cancelled = self._intake(invoiced.customer_email)
		frappe.db.set_value(ORDER, cancelled.name, "order_status", "Cancelled")
		card = self._card(invoiced.customer)
		self.assertEqual(card["web_orders"], [])
		self.assertEqual([r["doctype"] for r in card["orders"]], ["Sales Invoice"])   # shown as the sale


class DealsLinkThemselves(WebOrderCustomerCase):
	def test_a_web_order_links_the_deal_already_on_file(self):
		email = _email()
		deal = _deal(email)
		order = self._intake(email)
		self.assertIn((deal_customer.AUTO_LINK_JOB, {"queue": "short", "enqueue_after_commit": True,
			"deal": None, "email": email}), self.queue.calls)
		self.queue.run(deal_customer.AUTO_LINK_JOB)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"), order.customer)

	def test_a_new_deal_links_to_the_customer_its_order_made(self):
		email = _email()
		order = self._intake(email)
		self.queue.calls.clear()
		deal = _deal(email)
		self.assertIn((deal_customer.AUTO_LINK_JOB, {"queue": "short", "enqueue_after_commit": True,
			"deal": deal, "email": None}), self.queue.calls)
		self.queue.run(deal_customer.AUTO_LINK_JOB)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"), order.customer)

	def test_two_customers_with_the_email_are_left_to_the_employee(self):
		email = _email()
		_customer(email)
		_customer(email)
		deal = _deal(email)
		self.assertEqual(deal_customer.auto_link_deals(deal=deal), [])
		self.assertFalse(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"))

	def test_a_name_or_phone_match_never_links(self):
		deal = _deal(_email())
		contact = frappe.db.get_value("CRM Contacts", {"parent": deal}, "contact")
		full_name = frappe.db.get_value("Contact", contact, "full_name")
		_customer(_email(), customer_name=full_name, mobile_no="555-0100")
		self.assertEqual(deal_customer.auto_link_deals(deal=deal), [])

	def test_a_dealer_alias_never_links(self):
		email = _email()
		dealer = _customer(_email(), is_ffl_dealer=1)
		_alias(dealer, email)
		deal = _deal(email)
		self.assertEqual(deal_customer.auto_link_deals(deal=deal), [])

	def test_a_contact_already_on_another_customer_is_left_alone(self):
		email = _email()
		mine = _customer(email)
		deal = _deal(email)
		contact = frappe.db.get_value("CRM Contacts", {"parent": deal}, "contact")
		other = _customer(_email())
		doc = frappe.get_doc("Contact", contact)
		doc.append("links", {"link_doctype": "Customer", "link_name": other})
		doc.save(ignore_permissions=True)
		self.assertEqual(deal_customer.auto_link_deals(deal=deal), [])
		self.assertNotEqual(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"), mine)

	def test_switch_off_links_nothing_and_queues_nothing(self):
		_switch(0)
		email = _email()
		customer = _customer(email)
		deal = _deal(email)
		order = self._intake(email)
		self.assertEqual(order.customer, customer, "the order still gets its customer")
		self.assertNotIn(deal_customer.AUTO_LINK_JOB, self.queue.methods())
		self.assertEqual(deal_customer.auto_link_deals(deal=deal), [])
		self.assertFalse(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"))


class ThePatch(WebOrderCustomerCase):
	def _bare_order(self, email: str, status: str = "Pending Fulfillment") -> str:
		"""An order as the code before this field left it: no customer."""
		order = self._intake(email)
		frappe.db.set_value(ORDER, order.name, {"customer": None, "order_status": status})
		return order.name

	def test_backfill_is_idempotent(self):
		paid_email, pending_email, cancelled_email = _email(), _email(), _email()
		billed = _customer(_email())   # the invoice's customer, whatever the order's email says
		invoiced = self._bare_order(paid_email, "Transferred")
		self._invoice(invoiced, billed)
		pending = self._bare_order(pending_email)
		cancelled = self._bare_order(cancelled_email, "Cancelled")
		frappe.db.delete("Customer", {"email_id": ["in", [pending_email, cancelled_email]]})
		no_email = self._bare_order("")
		shared = revenue._ensure_web_customer()
		self._invoice(no_email, shared, submit=False)
		deal = _deal(pending_email)

		patch_module.execute()
		first = {n: frappe.db.get_value(ORDER, n, "customer") for n in (invoiced, pending, cancelled, no_email)}
		customers = frappe.db.count("Customer")
		patch_module.execute()
		second = {n: frappe.db.get_value(ORDER, n, "customer") for n in (invoiced, pending, cancelled, no_email)}

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("Customer"), customers, "a second run makes no customer")
		self.assertEqual(first[invoiced], billed)
		self.assertEqual(frappe.db.get_value("Customer", first[pending], "email_id"), pending_email)
		self.assertFalse(first[cancelled], "nothing is made for a cancelled order")
		self.assertFalse(first[no_email], "the shared web customer is nobody's")
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "erpnext_customer"), first[pending])
