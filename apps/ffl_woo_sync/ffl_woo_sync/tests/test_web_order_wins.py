"""A paid web order wins the CRM deal or lead it closes (``ffl_core.crm.web_orders``).

Real documents on the real database, through the webhook's own handler (``_handle_order_upsert``) and
Frappe CRM's own lead conversion. Only the job queue is replaced: the jobs it records are run by hand,
as the worker would after the commit. Every test runs inside the savepoint CRMTestCase rolls back.
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import add_to_date, now_datetime

from ffl_core.crm import web_orders
from ffl_core.crm.mail import LEAD, OTHER_MAIL, SHARED_NOTE, share_with_team
from ffl_core.crm.orders import DEAL_FIELD
from ffl_core.tests.crm_support import CLERK
from ffl_woo_sync.patches import web_orders_win_deals
from ffl_woo_sync.tests.test_web_order_customer import WebOrderCustomerCase, _contact, _customer, _email, _token
from ffl_woo_sync.woocommerce import webhook

DEAL = "CRM Deal"
ORDER = "Woo Online Order"
TOTAL = 250.0
FFL_META = [{"key": "_shipping_fflno", "value": "1-99-888-01-2D-09999"}]


def _status(type_: str, doctype: str = "CRM Deal Status") -> str:
	found = frappe.get_all(doctype, filters={"type": type_}, pluck="name", order_by="position asc", limit=1)
	if found:
		return found[0]
	field = "deal_status" if doctype == "CRM Deal Status" else "lead_status"
	return frappe.get_doc({"doctype": doctype, field: f"T {type_}", "type": type_}).insert(ignore_permissions=True).name


def _open_lead_status() -> str:
	return frappe.get_all("CRM Lead Status", filters={"type": ["not in", ["Won", "Lost"]], "name": ["!=", OTHER_MAIL]},
		pluck="name", order_by="position asc", limit=1)[0]


def _lead(email: str, status: str | None = None, owner: str | None = CLERK) -> str:
	doc = frappe.get_doc({"doctype": LEAD, "first_name": "Lead " + _token(), "email": email,
		"status": status or _open_lead_status(), "lead_owner": owner})
	if frappe.db.get_value("CRM Lead Status", doc.status, "type") == "Lost":
		doc.lost_reason, doc.lost_notes = "Other", "Bought elsewhere"
	return doc.insert(ignore_permissions=True).name


def _deal(email: str, status: str = "New") -> str:
	doc = frappe.new_doc(DEAL)
	doc.status = status
	doc.append("contacts", {"contact": _contact(email), "is_primary": 1})
	return doc.insert(ignore_permissions=True).name


def _body(email: str, woo_id: str, status: str = "processing", **extra) -> dict:
	return {"id": woo_id, "status": status, "total": f"{TOTAL:.2f}", "currency": "USD",
		"billing": {"first_name": "Pat", "last_name": "Sample", "email": email, "phone": "555-0100"},
		"line_items": [], "meta_data": FFL_META, **extra}


def _woo_id() -> str:
	return str(700000 + int(_token()[:5], 16))


class WinCase(WebOrderCustomerCase):
	def setUp(self):
		super().setUp()
		self.won = _status("Won")

	def _jobs(self, method: str) -> None:
		"""Run the recorded *method* jobs, as the worker does after the commit, and forget them."""
		calls, self.queue.calls = self.queue.calls, []
		for m, kwargs in calls:
			if m == method:
				frappe.get_attr(m)(order=kwargs["order"])
			else:
				self.queue.calls.append((m, kwargs))

	def _upsert(self, email: str, woo_id: str, status: str = "processing", **extra) -> str:
		webhook._handle_order_upsert(_body(email, woo_id, status, **extra))
		self._jobs(web_orders.WIN_JOB)
		self._jobs(web_orders.UNDO_JOB)
		return frappe.db.get_value(ORDER, {"woo_order_id": woo_id, "site": "retail"})

	def _pay(self, email: str) -> tuple[str, str]:
		woo_id = _woo_id()
		return self._upsert(email, woo_id), woo_id

	def _notes(self, deal: str) -> list[str]:
		return frappe.get_all(web_orders.NOTE, filters={"reference_doctype": DEAL, "reference_docname": deal},
			pluck="title", order_by="creation asc")

	def assertWon(self, deal: str, order: str) -> None:
		row = frappe.db.get_value(DEAL, deal, ["status", "deal_value", web_orders.WON_BY, "closed_date"], as_dict=True)
		self.assertEqual(row.status, self.won)
		self.assertEqual(row.deal_value, TOTAL)
		self.assertEqual(row[web_orders.WON_BY], order)
		self.assertTrue(row.closed_date)


class ALeadIsConvertedAndWon(WinCase):
	def test_the_lead_becomes_a_won_deal_with_its_owner_and_the_customer(self):
		email = _email()
		lead = _lead(email)
		share_with_team(LEAD, lead)
		notices = frappe.db.count("Notification Log", {"for_user": CLERK})
		order, woo_id = self._pay(email)

		deal = frappe.db.get_value(DEAL, {"lead": lead})
		self.assertTrue(deal, "the lead was converted by CRM")
		self.assertEqual(frappe.db.get_value(LEAD, lead, "converted"), 1)
		self.assertWon(deal, order)
		doc = frappe.get_doc(DEAL, deal)
		self.assertEqual(doc.deal_owner, CLERK)
		customer = frappe.db.get_value(ORDER, order, "customer")
		self.assertTrue(customer)
		self.assertEqual(doc.erpnext_customer, customer)
		self.assertIn(f"Web order {woo_id} placed", self._notes(deal))
		# CRM's own log: open, then Won, then the Won interval still running.
		self.assertEqual([r.get("to") for r in doc.status_change_log][-2:], [self.won, ""])
		# A shared lead stays shared on its deal; nobody was told.
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": DEAL, "reference_name": deal, "description": SHARED_NOTE}))
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": CLERK}), notices)
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": DEAL, "reference_name": deal, "ffl_ack_required": 1}))
		self.assertFalse(frappe.db.exists("CRM Task", {"reference_doctype": DEAL, "reference_docname": deal}))

	def test_redelivery_converts_once(self):
		email = _email()
		lead = _lead(email)
		order, woo_id = self._pay(email)
		self._upsert(email, woo_id)
		self._upsert(email, woo_id, status="completed")
		web_orders.web_order_paid(order)
		self.assertEqual(frappe.db.count(DEAL, {"lead": lead}), 1)

	def test_the_newest_of_several_leads_only(self):
		email = _email()
		old = _lead(email)
		new = _lead(email)
		frappe.db.set_value(LEAD, old, "creation", add_to_date(now_datetime(), hours=-2), update_modified=False)
		self._pay(email)
		self.assertTrue(frappe.db.exists(DEAL, {"lead": new}))
		self.assertFalse(frappe.db.exists(DEAL, {"lead": old}))
		self.assertEqual(frappe.db.get_value(LEAD, old, "converted"), 0)

	def test_a_lost_lead_or_other_mail_is_untouched(self):
		for status in (_status("Lost", "CRM Lead Status"), OTHER_MAIL):
			with self.subTest(status=status):
				email = _email()
				lead = _lead(email, status=status)
				self._pay(email)
				self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))
				self.assertEqual(frappe.db.get_value(LEAD, lead, ["status", "converted"]), (status, 0))

	def test_a_lead_made_after_the_order_is_untouched(self):
		email = _email()
		order, woo_id = self._pay(email)
		lead = _lead(email)
		self._upsert(email, woo_id, status="completed")
		self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))

	def test_an_unpaid_order_wins_nothing_until_it_is_paid(self):
		email = _email()
		lead = _lead(email)
		woo_id = _woo_id()
		self._upsert(email, woo_id, status="on-hold")
		self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))
		order = self._upsert(email, woo_id, status="processing")
		self.assertWon(frappe.db.get_value(DEAL, {"lead": lead}), order)


class AnOpenDealIsWon(WinCase):
	def test_the_open_deal_is_won_with_the_note(self):
		email = _email()
		deal = _deal(email)
		order, woo_id = self._pay(email)
		self.assertWon(deal, order)
		self.assertEqual(self._notes(deal), [f"Web order {woo_id} placed"])

	def test_an_open_deal_comes_before_a_lead(self):
		email = _email()
		lead = _lead(email)
		deal = _deal(email)
		order, _woo = self._pay(email)
		self.assertWon(deal, order)
		self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))

	def test_a_deal_with_an_invoice_gets_the_note_only(self):
		email = _email()
		deal = _deal(email)
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = _customer(email)
		si.set(DEAL_FIELD, deal)
		si.append("items", {"item_code": "BO-ACC", "qty": 1, "rate": 40})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()   # a draft is an order opened from the deal
		_order, woo_id = self._pay(email)
		self.assertNotEqual(frappe.db.get_value(DEAL, deal, "status"), self.won)
		self.assertEqual(self._notes(deal), [f"Web order {woo_id} placed"])

	def test_a_deal_with_an_open_order_draft_gets_the_note_only(self):
		from ffl_core.api.manual_order import save_order_draft

		email = _email()
		deal = _deal(email)
		save_order_draft({"customer": _customer(email), "items": [], "crm_deal": deal}, title=f"Deal {deal}")
		self._pay(email)
		self.assertNotEqual(frappe.db.get_value(DEAL, deal, "status"), self.won)
		self.assertFalse(frappe.db.get_value(DEAL, deal, web_orders.WON_BY))

	def test_a_deal_holding_a_gun_gets_the_note_only(self):
		email = _email()
		deal = _deal(email)
		serial = self._stocked_serial("BO-GUN")
		frappe.get_doc({"doctype": "CRM Deal Hold", "serial_no": serial, "item_code": "BO-GUN", "deal": deal,
			"status": "Active", "active_serial": serial, "held_by": "Administrator", "held_at": now_datetime(),
			"expires_at": now_datetime() + timedelta(hours=48)}).db_insert()
		_order, woo_id = self._pay(email)
		self.assertNotEqual(frappe.db.get_value(DEAL, deal, "status"), self.won)
		self.assertEqual(self._notes(deal), [f"Web order {woo_id} placed"])

	def test_a_won_deal_and_a_second_order_are_left_alone(self):
		email = _email()
		deal = _deal(email)
		first, _w = self._pay(email)
		second, _w2 = self._pay(email)
		self.assertWon(deal, first)
		self.assertEqual(frappe.db.count(DEAL, {web_orders.WON_BY: second}), 0)

	def test_switch_off_does_nothing(self):
		from ffl_woo_sync.tests.test_web_order_customer import _switch

		_switch(0)
		email = _email()
		deal = _deal(email)
		lead = _lead(email)
		order, _w = self._pay(email)
		self.assertNotIn(web_orders.WIN_JOB, self.queue.methods())
		self.assertIsNone(web_orders.web_order_paid(order))
		self.assertNotEqual(frappe.db.get_value(DEAL, deal, "status"), self.won)
		self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))


class TheValueStays(WinCase):
	def test_the_value_survives_a_save(self):
		email = _email()
		deal = _deal(email)
		order, _w = self._pay(email)
		doc = frappe.get_doc(DEAL, deal)
		doc.deal_value = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(DEAL, deal, "deal_value"), TOTAL)

	def test_the_orders_own_invoice_on_the_deal_is_not_counted_twice(self):
		email = _email()
		deal = _deal(email)
		order, _w = self._pay(email)
		si = self._invoice(order, frappe.db.get_value(ORDER, order, "customer"), submit=False)
		si.set(DEAL_FIELD, deal)
		si.save(ignore_permissions=True)
		si.submit()
		self.assertEqual(frappe.db.get_value(DEAL, deal, "deal_value"), TOTAL)


class CancelledOrRefunded(WinCase):
	def _won_deal(self):
		email = _email()
		deal = _deal(email, status="Talking" if frappe.db.exists("CRM Deal Status", "Talking") else "New")
		order, woo_id = self._pay(email)
		self.assertWon(deal, order)
		return email, deal, order, woo_id

	def test_cancel_and_full_refund_reopen_and_clear(self):
		for status in ("cancelled", "refunded"):
			with self.subTest(status=status):
				email, deal, order, woo_id = self._won_deal()
				before = frappe.get_doc(DEAL, deal).status_change_log[-2].get("from")
				self._upsert(email, woo_id, status=status)
				row = frappe.db.get_value(DEAL, deal, ["status", "deal_value", web_orders.WON_BY, "closed_date"], as_dict=True)
				self.assertEqual(row.status, before)
				self.assertFalse(row.deal_value)
				self.assertFalse(row[web_orders.WON_BY])
				self.assertFalse(row.closed_date)
				self.assertIn(f"Web order {woo_id} {status}", self._notes(deal))

	def test_a_converted_lead_reopens_to_the_status_before_won(self):
		email = _email()
		lead = _lead(email)
		_order, woo_id = self._pay(email)
		deal = frappe.db.get_value(DEAL, {"lead": lead})
		self._upsert(email, woo_id, status="cancelled")
		status = frappe.db.get_value(DEAL, deal, "status")
		self.assertNotEqual(status, self.won)
		self.assertEqual(frappe.db.get_value("CRM Deal Status", status, "type"), "Open")

	def test_a_partial_refund_leaves_the_deal_won(self):
		email, deal, order, woo_id = self._won_deal()
		self._upsert(email, woo_id, refunds=[{"id": 91, "total": "-20.00"}])
		self.assertWon(deal, order)
		self.assertIn(f"Web order {woo_id} refunded (91)", self._notes(deal))


class ThePatch(WinCase):
	def test_the_patch_wins_old_orders_once_including_a_hand_closed_lead(self):
		won_lead = _status("Won", "CRM Lead Status")
		email, other = _email(), _email()
		deal = _deal(email)
		lead = _lead(other, status=won_lead)
		order = self._record(email)
		order2 = self._record(other)
		web_orders_win_deals.execute()
		web_orders_win_deals.execute()
		self.assertWon(deal, order)
		converted = frappe.get_all(DEAL, filters={"lead": lead}, pluck="name")
		self.assertEqual(len(converted), 1)
		self.assertWon(converted[0], order2)

	def test_live_orders_never_take_a_hand_closed_lead(self):
		email = _email()
		lead = _lead(email, status=_status("Won", "CRM Lead Status"))
		self._pay(email)
		self.assertFalse(frappe.db.exists(DEAL, {"lead": lead}))

	def _record(self, email: str) -> str:
		"""An order on file from before this release: recorded with the switch off, so nothing ran."""
		from ffl_woo_sync.tests.test_web_order_customer import _switch

		_switch(0)
		order, _w = self._pay(email)
		_switch(1)
		return order
