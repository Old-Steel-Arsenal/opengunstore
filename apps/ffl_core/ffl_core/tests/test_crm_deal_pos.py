"""A CRM Deal and the POS (ffl_core.crm.customer, ffl_core.crm.orders): the customer link, the card,
Open order, and the write-back each accounting ruling asks for. Real documents through the real
create_order, update_order and submit paths; FastBound is off. Every test runs inside the savepoint
CRMTestCase rolls back."""
from __future__ import annotations

import json
import random
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from ffl_core.api import crm as api
from ffl_core.api import manual_order
from ffl_core.crm import customer as deal_customer
from ffl_core.crm import orders, pos_link_on, setup
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
from ffl_core.tests.crm_support import AMMO, CLERK, MANAGER, PER_GUN, CRMTestCase, product_of, set_item_price, user
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin

SETTINGS = "Lead Mail Settings"
OWNER = "crm-deal-owner@example.com"
STOCK = "crm-deal-stock@example.com"
GUN = "BO-GUN"
FEE = "_T-CRM-XFER-FEE"
ACC = "_T-CRM-DEAL-ACC"


def _token() -> str:
	return frappe.generate_hash(length=10)


def _switch(on: int) -> None:
	frappe.db.set_single_value(SETTINGS, "crm_pos_link", on)


def _contact(email: str | None = None, phone: str | None = None, first_name: str | None = None) -> str:
	doc = frappe.new_doc("Contact")
	doc.first_name = first_name or "Buyer " + _token()
	if email:
		doc.append("email_ids", {"email_id": email, "is_primary": 1})
	if phone:
		doc.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
	doc.insert(ignore_permissions=True)
	return doc.name


def _deal(contact: str | None = None, *, owner: str | None = OWNER, products=(), status: str = "Qualification"):
	contact = contact or _contact(f"{_token()}@example.com")
	doc = frappe.new_doc("CRM Deal")
	doc.status = status
	doc.deal_owner = owner
	doc.append("contacts", {"contact": contact, "is_primary": 1})
	for row in products:
		doc.append("products", row)
	doc.insert(ignore_permissions=True)
	return doc


def _tasks(deal: str) -> list:
	return frappe.get_all("CRM Task", filters={"reference_doctype": "CRM Deal", "reference_docname": deal},
		fields=["name", "title", "assigned_to", "status"])


def _customer(name: str | None = None, **fields) -> str:
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": name or "Cust " + _token(),
		"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)
	if fields:
		frappe.db.set_value("Customer", doc.name, fields, update_modified=False)
	return doc.name


class DealPOSCase(BuildOrderMixin, CRMTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup.trade_in_credit import ensure

		ensure()
		user(OWNER, "Sales User")
		user(STOCK, "Stock Manager")
		cls._item(FEE, firearm=False)
		cls._item(ACC, firearm=False)
		cls.pos = frappe.db.get_value("POS Profile", {"company": cls.company, "disabled": 0}, "name")

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in these tests"))
		p.start()
		self.addCleanup(p.stop)
		_switch(1)

	def _invoice(self, deal: str | None, lines, *, shipping: float = 0, customer: str | None = None, submit=True):
		"""A Desk-shaped Sales Invoice carrying *deal*: lines are (item, rate) or a full row dict."""
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer or self._indiv("_T CRM Deal Buyer")
		si.ffl_destination_ffl = DEST_FFL
		si.ffl_buyer_type = "Individual"
		si.ffl_crm_deal = deal
		si.update_stock = 0
		si.ffl_shipping_amount = shipping
		for line in lines:
			row = line if isinstance(line, dict) else {"item_code": line[0], "qty": 1, "rate": line[1]}
			si.append("items", row)
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		if submit:
			si.submit()
		return si

	def _sale(self, deal: str, rate: float = 100, **kwargs):
		return self._invoice(deal, [(ACC, rate)], **kwargs)

	def _order(self, deal: str | None, rate: float = 250, **over):
		payload = {"buyer_type": "individual", "customer": self._indiv("_T CRM Deal Buyer"), "pos_profile": self.pos,
			"tax_exempt": 1, "pickup_in_store": 1, "payment_received": 0,
			"items": [{"item_code": ACC, "qty": 1, "rate": rate}]}
		if deal:
			payload["crm_deal"] = deal
		payload.update(over)
		return manual_order.create_order(payload)

	def _trade_in(self, cost: float) -> str:
		from ffl_core.acquisition import record_firearm_acquisition

		return record_firearm_acquisition(item_code=GUN, serial="CRM-TI-" + _token().upper(), source_type="Trade-in",
			source_name="Testy Seller", cost=cost, company=self.company, source_id_type="Driver License",
			source_id_number="DL-1", source_first_name="Testy", source_last_name="Seller",
			source_address="123 Main St, Springfield TX 75024", submit_now=True,
			pending_intake_payload=json.dumps({"manufacturer": "Glock", "model_name": "19", "caliber": "9mm"}))


class TestWriteBack(DealPOSCase):
	def test_the_worked_example_and_its_full_cancel(self):
		"""Gun 1300, transfer fee 30, trade-in 800, shipping 40: 1330. Tax, shipping and the allowance
		take no part. A full cancel: 0, still Won, a task for the owner."""
		deal = _deal().name
		serial = self._stocked_serial("CRMWE" + _token().upper())
		si = self._invoice(deal, [
			{"item_code": GUN, "qty": 1, "rate": 1300, "warehouse": self.warehouse, "serial_no": serial,
				"use_serial_batch_fields": 1},
			(FEE, 30),
			{"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -800, "ffl_trade_in_acquisition": self._trade_in(800)},
		], shipping=40)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(flt(si.base_net_total), 530, "the invoice's own net is after the allowance")
		self.assertEqual(flt(si.ffl_shipping_amount), 40)
		row = frappe.db.get_value("CRM Deal", deal, ["deal_value", "status", "closed_date"], as_dict=True)
		self.assertEqual(flt(row.deal_value), 1330)
		self.assertEqual(row.status, "Won")
		self.assertEqual(str(row.closed_date), nowdate())
		self.assertEqual(_tasks(deal), [], "a sale makes no task")

		si.reload()
		si.cancel()
		row = frappe.db.get_value("CRM Deal", deal, ["deal_value", "status"], as_dict=True)
		self.assertEqual(flt(row.deal_value), 0)
		self.assertEqual(row.status, "Won", "a cancel never reopens the deal")
		tasks = _tasks(deal)
		self.assertEqual(len(tasks), 1)
		self.assertEqual(tasks[0].assigned_to, OWNER)
		self.assertIn(si.name, tasks[0].title)

	def test_the_status_change_is_crms_own(self):
		deal = _deal().name
		self._sale(deal)
		log = frappe.get_doc("CRM Deal", deal).status_change_log
		self.assertEqual(log[-2].to, "Won", "CRM's validate logged the change")
		self.assertEqual(log[-1].get("from"), "Won")

	def test_several_invoices_sum(self):
		deal = _deal().name
		self._sale(deal, 100)
		self._sale(deal, 250)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 350)

	def test_tax_is_not_in_the_value(self):
		deal = _deal().name
		si = self._order(deal, tax_exempt=0)
		net = frappe.db.get_value("Sales Invoice", si["sales_invoice"], ["base_net_total", "grand_total"], as_dict=True)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), flt(net.base_net_total))

	def test_a_credit_note_inherits_the_deal_and_counts_negative(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		deal = _deal().name
		si = self._sale(deal, 400)
		ret = make_sales_return(si.name)
		self.assertEqual(ret.ffl_crm_deal, deal, "ffl_crm_deal is not no_copy")
		ret.items[0].qty = -1
		ret.insert(ignore_permissions=True)
		ret.submit()
		row = frappe.db.get_value("CRM Deal", deal, ["deal_value", "status"], as_dict=True)
		self.assertEqual(flt(row.deal_value), 0)
		self.assertEqual(row.status, "Won")
		tasks = _tasks(deal)
		self.assertEqual(len(tasks), 1)
		self.assertIn(ret.name, tasks[0].title)

	def test_a_credit_note_does_not_win_a_deal(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		deal = _deal().name
		si = self._sale(deal, 400)
		frappe.db.set_value("CRM Deal", deal, "status", "Negotiation")
		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		ret.submit()
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "status"), "Negotiation")

	def test_an_amended_invoice_stays_on_the_deal(self):
		deal = _deal().name
		si = self._sale(deal, 400)
		si.reload()
		si.cancel()
		amended = frappe.copy_doc(si)
		amended.amended_from = si.name
		self.assertEqual(amended.ffl_crm_deal, deal)

	def test_edit_order_rebuilds_with_no_task(self):
		deal = _deal().name
		created = self._order(deal, rate=300)
		self.assertEqual(frappe.db.get_value("Sales Invoice", created["sales_invoice"], "ffl_crm_deal"), deal)
		self.assertEqual(manual_order.get_order_for_edit(created["sales_invoice"])["payload"]["crm_deal"], deal)
		# The page's payload is not what keeps the deal: update_order takes it from the invoice.
		edited = manual_order.update_order(created["sales_invoice"], {"buyer_type": "individual",
			"customer": self._indiv("_T CRM Deal Buyer"), "pos_profile": self.pos, "tax_exempt": 1, "pickup_in_store": 1,
			"items": [{"item_code": ACC, "qty": 1, "rate": 450}]})
		self.assertNotEqual(edited["sales_invoice"], created["sales_invoice"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", edited["sales_invoice"], "ffl_crm_deal"), deal)
		row = frappe.db.get_value("CRM Deal", deal, ["deal_value", "status"], as_dict=True)
		self.assertEqual(flt(row.deal_value), 450)
		self.assertEqual(row.status, "Won")
		self.assertEqual(_tasks(deal), [], "an edit's cancel makes no task")

	def test_cancel_order_makes_a_task(self):
		deal = _deal().name
		created = self._order(deal, rate=300)
		with patch("frappe.db.commit"):
			manual_order.cancel_order(created["sales_invoice"], "changed their mind")
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 0)
		self.assertEqual(len(_tasks(deal)), 1)

	def test_a_stock_manager_cancelling_still_leaves_the_owner_a_task(self):
		deal = _deal().name
		created = self._order(deal, rate=300)
		frappe.set_user(STOCK)
		try:
			with patch("frappe.db.commit"):
				manual_order.cancel_order(created["sales_invoice"], "changed their mind")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual([t.assigned_to for t in _tasks(deal)], [OWNER])
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 0)

	def test_a_deal_without_an_owner_gets_an_unassigned_task(self):
		deal = _deal(owner=None).name
		si = self._sale(deal)
		si.reload()
		si.cancel()
		self.assertEqual([t.assigned_to for t in _tasks(deal)], [None])

	def test_validate_pins_the_value_while_invoices_stand(self):
		deal = _deal().name
		si = self._sale(deal, 400)
		doc = frappe.get_doc("CRM Deal", deal)
		doc.deal_value = 9999
		doc.save(ignore_permissions=True)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 400)
		si.reload()
		si.cancel()
		doc = frappe.get_doc("CRM Deal", deal)
		doc.deal_value = 75
		doc.save(ignore_permissions=True)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 75, "no submitted invoice: staff's value stands")

	def test_a_consolidated_invoice_is_not_counted(self):
		deal = _deal().name
		si = self._sale(deal, 400)
		frappe.db.set_value("Sales Invoice", si.name, "is_consolidated", 1)
		self.assertEqual(orders.deal_sales(deal), (0.0, False))

	def test_the_write_back_does_not_reprice_the_quote(self):
		"""An open deal's line with a sold gun would make price_lines throw: a cancel still writes back."""
		serial = self._stocked_serial("CRMRP" + _token().upper())
		frappe.db.set_value("Item", GUN, "crm_product_code", None)
		from ffl_core.crm.item import upsert_product

		upsert_product(frappe.get_doc("Item", GUN))
		deal = _deal(products=[{"product_code": product_of(GUN), "qty": 1, "ffl_serial_no": serial}]).name
		si = self._sale(deal, 400)
		frappe.db.set_value("CRM Deal", deal, "status", "Negotiation")
		frappe.db.set_value("Serial No", serial, "status", "Delivered")
		si.reload()
		si.cancel()
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 0)
		self.assertEqual(len(_tasks(deal)), 1)

	def test_a_failed_write_back_does_not_block_the_invoice(self):
		deal = _deal().name
		before = frappe.db.count("Error Log", {"method": ["like", "[CRM] Deal write-back failed%"]})
		with patch.object(orders, "_task_for_owner", side_effect=frappe.ValidationError("boom")), \
				patch.object(orders, "deal_sales", side_effect=frappe.ValidationError("no sum")):
			si = self._sale(deal, 400)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "status"), "Qualification")
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", "[CRM] Deal write-back failed%"]}), before + 1)

	def test_a_failure_part_way_leaves_no_half_written_deal(self):
		deal = _deal().name
		si = self._sale(deal, 400)
		messages = len(frappe.local.message_log)
		def refuse(*_args, **_kwargs):
			frappe.throw("Refused")
		with patch.object(orders, "_task_for_owner", side_effect=refuse):
			si.reload()
			si.cancel()
		self.assertEqual(si.docstatus, 2)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 400, "rolled back with the task")
		self.assertEqual(len(frappe.local.message_log), messages, "the invoice's user is not shown a failure that is not theirs")

	def test_a_deleted_deal_does_not_stop_anything(self):
		self.assertEqual(frappe.db.get_value("Sales Invoice",
			self._order("CRM-DEAL-NOT-THERE")["sales_invoice"], "ffl_crm_deal"), None)
		si = self._sale("CRM-DEAL-NOT-THERE")
		self.assertEqual(si.docstatus, 1)

	def test_a_write_back_never_commits(self):
		deal = _deal().name
		si = self._sale(deal)
		commit = frappe.db.commit
		with patch("frappe.db.commit", side_effect=commit) as committed:
			orders.on_submit(si)
			orders.on_cancel(si)
		committed.assert_not_called()

	def test_pos_invoice_takes_no_part(self):
		self.assertFalse(frappe.get_meta("POS Invoice").has_field(orders.DEAL_FIELD), "merging would copy it")
		self.assertTrue(frappe.get_meta("Sales Invoice").has_field(orders.DEAL_FIELD))
		self.assertFalse(frappe.get_meta("Sales Invoice").get_field(orders.DEAL_FIELD).no_copy)


class TestSwitch(DealPOSCase):
	def test_off_by_default(self):
		meta = frappe.get_meta(SETTINGS).get_field("crm_pos_link")
		self.assertEqual((meta.fieldtype, meta.default, meta.description), ("Check", "0", None))

	def test_off_nothing_writes_back_or_answers(self):
		_switch(0)
		self.assertFalse(pos_link_on())
		deal = _deal().name
		si = self._sale(deal, 400)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "status"), "Qualification")
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 0)
		si.reload()
		si.cancel()
		self.assertEqual(_tasks(deal), [])
		for call in (api.deal_customer_summary, api.ensure_deal_customer, api.open_deal_order):
			with self.assertRaises(frappe.ValidationError):
				call(deal)

	def test_off_a_deal_keeps_its_own_value(self):
		_switch(0)
		deal = _deal().name
		self._sale(deal, 400)
		doc = frappe.get_doc("CRM Deal", deal)
		doc.deal_value = 75
		doc.save(ignore_permissions=True)
		self.assertEqual(flt(frappe.db.get_value("CRM Deal", deal, "deal_value")), 75)

	def _section(self) -> list:
		sections = json.loads(frappe.db.get_value("CRM Fields Layout", setup.SIDE_PANEL, "layout"))
		return [s for s in sections if s.get("name") == setup.CARD_SECTION]

	def test_the_button_and_card_follow_the_switch(self):
		_switch(0)
		setup.apply_pos_link()
		setup.apply_pos_link()
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, ["dt", "view", "enabled"]), ("CRM Deal", "Form", 0))
		self.assertEqual(self._section(), [])
		_switch(1)
		setup.apply_pos_link()
		setup.apply_pos_link()
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, "enabled"), 1)
		self.assertEqual(self._section()[0]["columns"][0]["fields"], [deal_customer.CARD_FIELD])
		self.assertEqual(len(self._section()), 1)
		sections = json.loads(frappe.db.get_value("CRM Fields Layout", setup.SIDE_PANEL, "layout"))
		self.assertEqual(sections[0]["name"], "contacts_section")
		self.assertEqual(sections[1]["name"], setup.CARD_SECTION)
		script = frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, "script")
		for needle in ("class CRMDeal", "setFieldHtml", deal_customer.CARD_FIELD, "Open order",
				"ffl_core.api.crm.open_deal_order", "ffl_core.api.crm.ensure_deal_customer",
				"ffl_core.api.crm.deal_customer_summary"):
			self.assertIn(needle, script)

	def test_saving_the_settings_applies_it(self):
		_switch(0)
		setup.apply_pos_link()
		doc = frappe.get_doc(SETTINGS)
		doc.crm_pos_link = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.DEAL_SCRIPT, "enabled"), 1)
		self.assertEqual(len(self._section()), 1)


class TestPermissions(DealPOSCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_a_sales_user_on_their_own_deal(self):
		deal = _deal(owner=CLERK).name
		frappe.set_user(CLERK)
		self.assertIn("html", api.deal_customer_summary(deal))
		self.assertIsNone(api.ensure_deal_customer(deal)["customer"])
		self.assertTrue(api.ensure_deal_customer(deal, create=1)["customer"])
		self.assertTrue(api.open_deal_order(deal)["draft"])

	def test_a_sales_user_on_someone_elses_deal(self):
		deal = _deal(owner=MANAGER).name
		frappe.set_user(CLERK)
		for call in (api.deal_customer_summary, api.ensure_deal_customer, api.open_deal_order):
			with self.assertRaises(frappe.PermissionError):
				call(deal)

	def test_a_sales_manager_sees_any_deal(self):
		deal = _deal(owner=CLERK).name
		frappe.set_user(MANAGER)
		self.assertIn("html", api.deal_customer_summary(deal))

	def test_a_role_outside_crm_is_refused(self):
		deal = _deal().name
		frappe.set_user(STOCK)
		for call in (api.deal_customer_summary, api.ensure_deal_customer, api.open_deal_order):
			with self.assertRaises(frappe.PermissionError):
				call(deal)


class TestCustomerLink(DealPOSCase):
	def test_matches_by_the_customers_email_and_phone(self):
		email, phone = f"{_token()}@example.com", "512555" + str(random.randint(1000, 9999))
		by_email = _customer(email_id=email)
		by_phone = _customer(mobile_no=phone)
		deal = _deal(_contact(email, phone))
		out = deal_customer.ensure_customer(deal)
		self.assertIsNone(out["customer"])
		matched = {m["name"]: m["matched"] for m in out["matches"]}
		self.assertEqual(matched[by_email], ["email"])
		self.assertEqual(matched[by_phone], ["phone"])
		self.assertIsNone(frappe.db.get_value("CRM Deal", deal.name, "erpnext_customer"), "nothing linked until confirmed")

	def test_a_contacts_email_matches_too(self):
		email = f"{_token()}@example.com"
		customer = _customer()
		frappe.get_doc({"doctype": "Contact", "first_name": "Other " + _token(),
			"email_ids": [{"email_id": email, "is_primary": 1}],
			"links": [{"link_doctype": "Customer", "link_name": customer}]}).insert(ignore_permissions=True)
		deal = _deal(_contact(email))
		self.assertEqual([(m["name"], m["matched"]) for m in deal_customer.ensure_customer(deal)["matches"]],
			[(customer, ["contact email"])])

	def test_the_order_resolvers_still_ignore_contact_emails(self):
		from ffl_core.customer_profile import find_customer_by_email

		email = f"{_token()}@example.com"
		customer = _customer()
		frappe.get_doc({"doctype": "Contact", "first_name": "Other " + _token(),
			"email_ids": [{"email_id": email, "is_primary": 1}],
			"links": [{"link_doctype": "Customer", "link_name": customer}]}).insert(ignore_permissions=True)
		self.assertIsNone(find_customer_by_email(email))

	def test_confirming_writes_the_deal_and_the_contact(self):
		customer, contact = _customer(), _contact(f"{_token()}@example.com")
		deal = _deal(contact)
		self.assertEqual(deal_customer.ensure_customer(deal, customer=customer), {"customer": customer})
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, "erpnext_customer"), customer)
		self.assertTrue(frappe.db.exists("Dynamic Link", {"parenttype": "Contact", "parent": contact,
			"link_doctype": "Customer", "link_name": customer}))
		# Again: no second row, and the deal now just answers.
		deal_customer.ensure_customer(deal, customer=customer)
		self.assertEqual(frappe.db.count("Dynamic Link", {"parenttype": "Contact", "parent": contact,
			"link_doctype": "Customer", "link_name": customer}), 1)
		self.assertEqual(deal_customer.ensure_customer(frappe.get_doc("CRM Deal", deal.name)), {"customer": customer})

	def test_a_contact_already_linked_needs_no_confirming(self):
		customer = _customer()
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Known " + _token(),
			"links": [{"link_doctype": "Customer", "link_name": customer}]}).insert(ignore_permissions=True).name
		self.assertEqual(deal_customer.ensure_customer(_deal(contact)), {"customer": customer})

	def test_create_makes_one_customer_on_the_deals_contact(self):
		email = f"{_token()}@example.com"
		contact = _contact(email, first_name="Morgan " + _token())
		deal = _deal(contact)
		before = frappe.db.count("Contact")
		customer = deal_customer.ensure_customer(deal, create=True)["customer"]
		self.assertEqual(frappe.db.get_value("Customer", customer,
			["customer_name", "email_id", "customer_primary_contact"]),
			(frappe.db.get_value("Contact", contact, "full_name"), email, contact))
		self.assertEqual(frappe.db.count("Contact"), before, "no second Contact for the same person")
		self.assertTrue(frappe.db.exists("Dynamic Link", {"parenttype": "Contact", "parent": contact,
			"link_doctype": "Customer", "link_name": customer}))
		# A double click: the deal has its customer now.
		count = frappe.db.count("Customer")
		self.assertEqual(deal_customer.ensure_customer(frappe.get_doc("CRM Deal", deal.name), create=True)["customer"], customer)
		self.assertEqual(frappe.db.count("Customer"), count)

	def test_create_without_an_email_is_refused(self):
		deal = _deal(_contact(phone="5125550199"))
		with self.assertRaises(frappe.ValidationError):
			deal_customer.ensure_customer(deal, create=True)

	def test_a_customer_gone_since_reads_as_none(self):
		deal = _deal()
		frappe.db.set_value("CRM Deal", deal.name, "erpnext_customer", "No Such Customer")
		deal.reload()
		self.assertIsNone(deal_customer.ensure_customer(deal)["customer"])

	def test_an_unknown_customer_is_refused(self):
		with self.assertRaises(frappe.DoesNotExistError):
			deal_customer.ensure_customer(_deal(), customer="No Such Customer")


class TestBannedAndFlagged(DealPOSCase):
	def _banned(self, **fields) -> str:
		return _customer(customer_status="Banned", status_reason="Threats <b>loud</b>", **fields)

	def test_a_banned_match_is_shown(self):
		email = f"{_token()}@example.com"
		customer = self._banned(email_id=email)
		out = deal_customer.ensure_customer(_deal(_contact(email)))
		self.assertEqual(out["matches"][0]["name"], customer)
		self.assertTrue(out["matches"][0]["banned"])
		self.assertIn("Banned", out["html"])
		self.assertIn("Threats &lt;b&gt;loud&lt;/b&gt;", out["html"])

	def test_the_card_shows_a_ban_and_alerts(self):
		customer = self._banned()
		doc = frappe.get_doc("Customer", customer)
		doc.append("customer_alerts", {"message": "Call first <b>now</b>", "active": 1})
		doc.flags.ignore_permissions = True
		doc.save()
		deal = _deal()
		deal_customer.ensure_customer(deal, customer=customer)
		out = api.deal_customer_summary(deal.name)
		self.assertTrue(out["flags"]["banned"])
		self.assertEqual(out["flags"]["status"], "Banned")
		self.assertIn("Banned", out["html"])
		self.assertIn("Call first &lt;b&gt;now&lt;/b&gt;", out["html"])

	def test_a_banned_customer_cannot_be_sold_to_through_the_deal(self):
		customer = self._banned()
		deal = _deal()
		deal_customer.ensure_customer(deal, customer=customer)
		draft = api.open_deal_order(deal.name)["draft"]
		payload = manual_order.get_order_draft(draft)["payload"]
		self.assertEqual(payload["customer"], customer)
		payload["items"] = [{"item_code": ACC, "qty": 1, "rate": 10}]
		payload.update(pickup_in_store=1, tax_exempt=1, pos_profile=self.pos)
		with self.assertRaisesRegex(frappe.ValidationError, "Banned"):
			manual_order.create_order(payload)
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, "status"), "Qualification")


class TestSummary(DealPOSCase):
	def test_before_a_customer_it_shows_the_matches(self):
		email = f"{_token()}@example.com"
		customer = _customer(email_id=email)
		out = api.deal_customer_summary(_deal(_contact(email)).name)
		self.assertIsNone(out["customer"])
		self.assertEqual(out["matches"][0]["name"], customer)
		self.assertIn(customer, out["html"])

	def test_orders_spend_and_open_orders(self):
		customer = self._indiv("_T CRM Summary " + _token())
		deal = _deal()
		deal_customer.ensure_customer(deal, customer=customer)
		open_order = self._order(deal.name, rate=300, customer=customer)["sales_invoice"]
		self._order(None, rate=50, customer=customer)
		out = api.deal_customer_summary(deal.name)
		self.assertEqual(out["lifetime_spend"], 350)
		self.assertEqual(len(out["orders"]), 2)
		self.assertIn(open_order, {o["name"] for o in out["orders"]})
		self.assertIn(open_order, {o["sales_invoice"] for o in out["open_orders"]})
		self.assertIn(open_order, out["html"])
		self.assertIn(f"/app/sales-invoice/{open_order}", out["html"])

	def test_lifetime_spend_is_gross_of_trade_in_and_net_of_returns(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		customer = self._indiv("_T CRM Summary " + _token())
		si = self._invoice(None, [(ACC, 200)], customer=customer)
		self._invoice(None, [(ACC, 500)], customer=customer)
		ret = make_sales_return(si.name)
		ret.insert(ignore_permissions=True)
		ret.submit()
		self.assertEqual(deal_customer._lifetime_spend(customer), 500)

	def test_ffl_status(self):
		from ffl_core.tests.test_build_order import DEALER, DEALER_FFL

		self.assertFalse(deal_customer._ffl_status(_customer())["is_dealer"])
		out = deal_customer._ffl_status(DEALER)
		self.assertTrue(out["is_dealer"] and out["on_file"])
		self.assertFalse(out["expired"])
		frappe.db.set_value("ATF FFL Record", DEALER_FFL, "expiry_date", "2020-01-01")
		self.assertTrue(deal_customer._ffl_status(DEALER)["expired"])
		frappe.db.set_value("Customer", DEALER, "ffl_number", "")
		self.assertFalse(deal_customer._ffl_status(DEALER)["on_file"])

	def test_no_id_fields_in_the_summary(self):
		customer = _customer(date_of_birth="1980-02-03")
		doc = frappe.get_doc("Customer", customer)
		doc.append("customer_ids", {"id_type": "Driver License", "expiry_date": nowdate()})
		doc.flags.ignore_permissions = True
		doc.save()
		row = frappe.get_doc("Customer", customer).customer_ids[0]
		frappe.db.set_value(row.doctype, row.name, "id_number", "DL-SECRET-99")
		frappe.db.set_value("Customer", customer, "date_of_birth", "1980-02-03")
		deal = _deal()
		deal_customer.ensure_customer(deal, customer=customer)
		self.assertIsNotNone(frappe.db.get_value(row.doctype, row.name, "id_number"), "the fixture holds an ID number")
		out = api.deal_customer_summary(deal.name)
		dump = frappe.as_json(out).lower()
		for secret in ("dl-secret-99", "1980-02-03", "driver license", "date_of_birth", "id_number", "id_type",
				"4473", "nics", '"ids"', "agreements"):
			self.assertNotIn(secret, dump)
		self.assertEqual(set(out["flags"]), {"status", "status_reason", "banned", "override_until", "alerts", "tags"})

	def test_the_customer_name_is_escaped(self):
		customer = _customer(customer_name="Pat <img src=x onerror=alert(1)>")   # a name the Customer form would refuse
		deal = _deal()
		deal_customer.ensure_customer(deal, customer=customer)
		html = api.deal_customer_summary(deal.name)["html"]
		self.assertNotIn("<img", html)
		self.assertIn("&lt;img", html)


class TestOpenOrder(DealPOSCase):
	def _gun_product(self):
		from ffl_core.crm.item import upsert_product

		upsert_product(frappe.get_doc("Item", GUN))
		return product_of(GUN)

	def _linked_deal(self, products=(), customer=None):
		deal = _deal(products=products)
		deal_customer.ensure_customer(deal, customer=customer or _customer())
		return frappe.get_doc("CRM Deal", deal.name)

	def test_needs_a_customer(self):
		with self.assertRaises(frappe.ValidationError):
			api.open_deal_order(_deal().name)

	def test_the_draft_carries_the_customer_lines_and_deal_at_the_pages_prices(self):
		serial = self._stocked_serial("CRMOO" + _token().upper())
		frappe.db.set_value("Item", AMMO, "standard_rate", 22.49)
		deal = self._linked_deal(products=[
			{"product_code": product_of(AMMO), "qty": 3, "discount_percentage": 50},
			{"product_code": self._gun_product(), "qty": 1, "ffl_serial_no": serial},
		])
		out = api.open_deal_order(deal.name)
		self.assertEqual(out["route"], f"/app/create-order/draft/{out['draft']}")
		payload = manual_order.get_order_draft(out["draft"])["payload"]
		self.assertEqual(payload["customer"], deal.erpnext_customer)
		self.assertEqual(payload["crm_deal"], deal.name)
		self.assertEqual(payload["buyer_type"], "individual")
		ammo, gun = payload["items"]
		self.assertEqual((ammo["item_code"], ammo["qty"], ammo["rate"], ammo["serials"]), (AMMO, 3, 22.49, []))
		self.assertEqual((gun["item_code"], gun["serials"], gun["is_firearm"]), (GUN, [serial], True))
		data, _reason = manual_order._serial_status(serial, probe=False)
		self.assertEqual(gun["rate"], flt(data["sell_price"]))

	def test_a_gun_with_no_serial_waits_for_a_pick(self):
		deal = self._linked_deal(products=[{"product_code": product_of(PER_GUN), "qty": 1}])
		payload = manual_order.get_order_draft(api.open_deal_order(deal.name)["draft"])["payload"]
		self.assertEqual(payload["items"], [])
		self.assertEqual([i["item_code"] for i in payload["unpicked_firearms"]], [PER_GUN])

	def test_a_gun_that_sold_is_left_for_the_page_to_drop(self):
		serial = self._stocked_serial("CRMSOLD" + _token().upper())
		deal = self._linked_deal(products=[{"product_code": self._gun_product(), "qty": 1, "ffl_serial_no": serial}])
		frappe.db.set_value("Serial No", serial, "status", "Delivered")
		payload = manual_order.get_order_draft(api.open_deal_order(deal.name)["draft"])["payload"]
		self.assertEqual(payload["items"][0]["serials"], [serial])
		self.assertFalse(manual_order.check_serials_available([serial])[serial]["ok"])

	def test_a_dealer_opens_as_a_business_order(self):
		from ffl_core.tests.test_build_order import DEALER, DEALER_FFL

		deal = self._linked_deal(customer=DEALER)
		payload = manual_order.get_order_draft(api.open_deal_order(deal.name)["draft"])["payload"]
		self.assertEqual((payload["buyer_type"], payload["biz_ffl"]), ("business", DEALER_FFL))

	def test_a_second_click_refreshes_its_own_draft(self):
		deal = self._linked_deal(products=[{"product_code": product_of(AMMO), "qty": 1}])
		first = api.open_deal_order(deal.name)["draft"]
		self.assertEqual(api.open_deal_order(deal.name)["draft"], first)

	def test_a_draft_saved_from_the_page_is_left_alone(self):
		deal = self._linked_deal(products=[{"product_code": product_of(AMMO), "qty": 1}])
		first = api.open_deal_order(deal.name)["draft"]
		edited = {"customer": deal.erpnext_customer, "crm_deal": deal.name, "items": [{"item_code": AMMO, "qty": 9, "rate": 1}]}
		manual_order.save_order_draft(json.dumps(edited), name=first)   # the page's Save Draft, no title
		second = api.open_deal_order(deal.name)["draft"]
		self.assertNotEqual(second, first)
		self.assertEqual(manual_order.get_order_draft(first)["payload"]["items"][0]["qty"], 9)

	def test_the_order_from_the_draft_carries_the_deal_and_uses_the_draft(self):
		deal = self._linked_deal(customer=self._indiv("_T CRM Deal Buyer"), products=[{"product_code": product_of(AMMO), "qty": 1}])
		draft = api.open_deal_order(deal.name)["draft"]
		payload = manual_order.get_order_draft(draft)["payload"]
		payload.update(draft=draft, pickup_in_store=1, tax_exempt=1, pos_profile=self.pos,
			items=[{"item_code": ACC, "qty": 1, "rate": 125}])
		created = manual_order.create_order(payload)
		self.assertFalse(frappe.db.exists("Create Order Draft", draft))
		self.assertEqual(frappe.db.get_value("Sales Invoice", created["sales_invoice"], "ffl_crm_deal"), deal.name)
		row = frappe.db.get_value("CRM Deal", deal.name, ["status", "deal_value"], as_dict=True)
		self.assertEqual((row.status, flt(row.deal_value)), ("Won", 125))

	def test_a_line_that_is_not_a_pos_item_is_reported(self):
		code = "_T-CRM-NOT-POS-" + _token()
		product = frappe.new_doc("CRM Product")
		product.update({"product_code": code, "product_name": code})
		product.flags.ffl_crm_sync = True
		product.flags.ignore_erpnext_sync = True
		product.insert(ignore_permissions=True)
		deal = self._linked_deal(products=[{"product_code": product.name, "product_name": code, "qty": 1, "rate": 5}])
		self.assertEqual(api.open_deal_order(deal.name)["skipped"], [code])


class TestPageWiring(FrappeTestCase):
	"""The Create Order page carries the deal through its one payload path and opens a draft by address."""

	def test_the_page(self):
		path = frappe.get_app_path("ffl_core", "ffl_core", "page", "create_order", "create_order.js")
		with open(path) as handle:
			js = handle.read()
		self.assertIn("payload.crm_deal = this._crmDeal", js)
		self.assertIn("this._crmDeal = p.crm_deal || null", js)
		self.assertIn("this._crmDeal = null", js)
		self.assertIn("r[1] === 'draft'", js)
		self.assertIn("p.unpicked_firearms", js)
