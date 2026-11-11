"""A customer's want list (CRM Want, ffl_core.crm.wants): stock that fits gives the want's owner a CRM task.

Real documents through the real stock paths (Receive Goods, the Trade-in dialog paid now and as credit
at checkout). The only stand-in is the job queue: ``frappe.enqueue`` records what the stock document's hook
queued for after the commit, and the test then runs that job itself, as the worker would. FastBound is off.
Every test runs inside the savepoint CRMTestCase rolls back."""
from __future__ import annotations

import json
import random
from unittest.mock import patch

import frappe

from ffl_core.api import crm as api
from ffl_core.api import trade_in
from ffl_core.api.receive_goods import create_receive
from ffl_core.crm import wants
from ffl_core.tests import test_trade_in_credit as credit_tests
from ffl_core.tests.crm_support import AMMO, CLERK, CRMTestCase, user
from ffl_core.tests.test_crm_deal_pos import OWNER, _contact, _deal, _token
from ffl_core.tests.test_receive_goods import ReceiveGoodsFixtures, _ensure_item


class WantCase(CRMTestCase):
	def setUp(self):
		super().setUp()
		user(OWNER, "Sales User")

	def _want(self, **fields):
		return frappe.get_doc({"doctype": "CRM Want", "contact": _contact(), "want_owner": OWNER, **fields}
			).insert(ignore_permissions=True)

	def _arrive(self, fn):
		"""Run *fn* (a stock document being submitted), then the jobs its hook queued. Returns its result and the jobs."""
		queued = []

		def fake(method=None, **kw):
			queued.append((method, kw))

		with patch("frappe.enqueue", side_effect=fake):
			out = fn()
		jobs = [kw for method, kw in queued if method == wants.JOB]
		for kw in jobs:
			wants.match_arrival(kw["doctype"], kw["name"])
		return out, jobs

	def _task_of(self, want):
		want.reload()
		return frappe.get_doc("CRM Task", want.arrival_task)

	def _assert_alert(self, want, serial):
		want.reload()
		self.assertEqual(want.status, "Matched")
		self.assertEqual(want.matched_serial, serial)
		self.assertTrue(want.matched_item and want.matched_on)
		task = self._task_of(want)
		self.assertIn(serial, task.title)
		self.assertTrue(frappe.db.exists("ToDo", {"reference_type": "CRM Task", "reference_name": task.name,
			"allocated_to": OWNER, "status": "Open"}))
		return task


class TestKeywordRules(WantCase):
	def test_words_ignore_case_and_punctuation(self):
		self.assertEqual(wants.keyword_tokens("Mosin 91/30"), ["mosin", "9130"])
		self.assertEqual(wants.keyword_tokens("P-228"), ["p228"])
		self.assertEqual(wants.keyword_tokens(" - / "), [])

	def test_a_gun_is_found_by_its_words_whatever_the_punctuation(self):
		arrival = wants.Arrival(item_code="x", item_name="x", serial=None, text=wants._text(
			"<p>Mosin-Nagant M91/30 rifle</p>", "SIG P-228"))
		for found in ("Mosin 91/30", "p228", "mosin nagant", "SIG-P228", "SIG P 228"):
			self.assertTrue(wants._fits(frappe._dict(item=None, keywords=found), arrival), found)
		for missed in ("Mosin 44", "P229", "mosin sig p229", "-"):
			self.assertFalse(wants._fits(frappe._dict(item=None, keywords=missed), arrival), missed)

	def test_a_purchase_return_is_not_an_arrival(self):
		self._want(keywords="p228")
		with patch("frappe.enqueue") as enqueue:
			wants.on_stock_receipt(frappe._dict(doctype="Purchase Receipt", name="PR-x", is_return=1))
			enqueue.assert_not_called()
			wants.on_stock_receipt(frappe._dict(doctype="Purchase Receipt", name="PR-x", is_return=0))
			enqueue.assert_called_once()

	def test_a_want_needs_keywords_or_an_item(self):
		for fields in ({}, {"keywords": " / "}):
			with self.assertRaises(frappe.ValidationError):
				self._want(**fields)
		self.assertTrue(self._want(keywords="P228").customer is None)
		self._want(item=AMMO)


class TestAddWantButton(WantCase):
	def test_a_clerk_adds_a_want_to_the_deals_contact(self):
		contact = _contact()
		deal = _deal(contact, owner=CLERK).name
		frappe.set_user(CLERK)
		name = api.add_want(deal, keywords="P228", budget=600, notes="Wants nickel")
		frappe.set_user("Administrator")
		want = frappe.get_doc("CRM Want", name)
		self.assertEqual((want.contact, want.deal, want.want_owner, want.status), (contact, deal, CLERK, "Open"))
		self.assertEqual(want.budget, 600)

	def test_nothing_to_look_for_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.add_want(_deal().name)


class ReceiveGoodsWants(WantCase, ReceiveGoodsFixtures):
	"""Receive Goods: the purchase receipt path (A1-A6)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import custody

		custody.ensure()

	def _gun_in(self, name, serial=None, code=None, transfer=False):
		"""Receive one gun called *name* through Receive Goods. Returns (result, jobs, serial, item code)."""
		code = code or f"_T-WANT-{_token()}"
		serial = serial or f"WANT-{_token()}"
		_ensure_item(code, "Rifle", 1)
		frappe.db.set_value("Item", code, "item_name", name)
		extra = {"is_ffl_transfer": 1, "acquisition_type": "FFL Transfer"} if transfer else {}
		payload = json.loads(self._payload(**extra, items=[{
			"item_code": code, "qty": 1, "rate": 10 if transfer else 500, "serials": [serial], "sell_price": 0,
			"manufacturer": "Glock", "model_name": "Classic", "firearm_type": "Pistol", "caliber": "9mm"}]))
		res, jobs = self._arrive(lambda: create_receive(payload))
		return res, jobs, serial, code

	def test_a_keyword_finds_a_received_gun_by_its_name(self):
		want = self._want(keywords="p228")
		res, jobs, serial, code = self._gun_in("SIG P-228")
		self.assertEqual(len(jobs), 1)
		task = self._assert_alert(want, serial)
		self.assertEqual(want.matched_item, code)
		self.assertIn("p228", task.title)
		self.assertIn(serial, task.description)

	def test_a_want_with_an_item_fits_that_item_only(self):
		_, _, _, code = self._gun_in("SIG P-228")
		want = self._want(item=code)
		self._gun_in("Ruger Mark IV")
		want.reload()
		self.assertEqual((want.status, want.arrival_task), ("Open", None))
		_, _, serial, _ = self._gun_in("SIG P-228", code=code)
		self._assert_alert(want, serial)

	def test_every_keyword_must_be_there(self):
		want = self._want(keywords="p228 nickel")
		self._gun_in("SIG P-228")
		want.reload()
		self.assertEqual(want.status, "Open")
		_, _, serial, _ = self._gun_in("SIG P-228 Nickel")
		self._assert_alert(want, serial)

	def test_a_second_arrival_is_added_to_the_open_task(self):
		want = self._want(keywords="p228", budget=650)
		_, _, first, _ = self._gun_in("SIG P-228")
		task = self._assert_alert(want, first)
		self.assertIn("650", task.description)
		_, _, second, _ = self._gun_in("SIG P228 Elite")
		after = self._task_of(want)
		self.assertEqual(after.name, task.name)
		self.assertIn(first, after.description)
		self.assertIn(second, after.description)
		self.assertEqual(want.matched_serial, second)
		self.assertEqual(frappe.db.count("CRM Task", {"title": ["like", "Arrival:%"], "name": task.name}), 1)
		# A finished task is not reopened: the next arrival is a new one.
		frappe.db.set_value("CRM Task", task.name, "status", "Done")
		_, _, third, _ = self._gun_in("SIG P-228")
		self.assertNotEqual(self._task_of(want).name, task.name)
		self.assertIn(third, self._task_of(want).description)

	def test_the_task_sits_on_an_open_deal_and_nowhere_when_the_deal_is_closed(self):
		contact = _contact()
		open_deal, won = _deal(contact).name, _deal(status="Won").name
		on_deal = self._want(keywords="p228", contact=contact, deal=open_deal)
		closed = self._want(keywords="p228", deal=won)
		_, _, serial, _ = self._gun_in("SIG P-228")
		self.assertEqual(self._task_of(on_deal).reference_docname, open_deal)
		self.assertFalse(self._task_of(closed).reference_docname)
		self._assert_alert(closed, serial)

	def test_a_customers_transfer_gun_never_alerts_and_a_closed_want_is_left_alone(self):
		want = self._want(keywords="p228")
		_, jobs, serial, _ = self._gun_in("SIG P-228", transfer=True)
		self.assertEqual(len(jobs), 1, "the job runs; the custody check is what stops the alert")
		self.assertEqual(frappe.db.get_value("Serial No", serial, "is_transfer_gun"), 1)
		want.reload()
		self.assertEqual((want.status, want.arrival_task), ("Open", None))
		want.status = "Closed"
		want.save()
		_, jobs, _, _ = self._gun_in("SIG P-228")
		want.reload()
		self.assertEqual((jobs, want.status, want.arrival_task), ([], "Closed", None))

	def test_a_failed_match_never_fails_the_receipt(self):
		want = self._want(keywords="p228")
		before = frappe.db.count("Error Log", {"method": ["like", "[CRM] Want alert failed%"]})
		with patch.object(wants, "_alert", side_effect=frappe.ValidationError("boom")):
			res, jobs, serial, _ = self._gun_in("SIG P-228")
		self.assertEqual(len(jobs), 1)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", res["name"], "docstatus"), 1)
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", "[CRM] Want alert failed%"]}), before + 1)
		want.reload()
		self.assertEqual((want.status, want.arrival_task), ("Open", None))

	def test_a_queue_that_is_down_never_fails_the_receipt(self):
		self._want(keywords="p228")
		before = frappe.db.count("Error Log", {"method": ["like", "[CRM] Want match not queued%"]})
		payload = json.loads(self._payload(items=[{
			"item_code": "RG-FIREARM", "qty": 1, "rate": 500, "serials": [f"WANT-{_token()}"], "sell_price": 0,
			"manufacturer": "Glock", "model_name": "Classic", "firearm_type": "Pistol", "caliber": "9mm"}]))
		with patch("frappe.enqueue", side_effect=ConnectionError("redis down")):
			res = create_receive(payload)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", res["name"], "docstatus"), 1)
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", "[CRM] Want match not queued%"]}), before + 1)


class TradeInWants(WantCase):
	"""The Trade-in dialog: paid now (a purchase receipt from the seller) and as checkout credit (a Material Receipt
	when the sale is rung) (A7). The gun is found by a model name no other test uses."""

	_build_credit_invoice = credit_tests.TradeInCreditFlow._build_credit_invoice

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		from ffl_core.setup.trade_in_credit import ensure

		dev_site.run()
		ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.pos_profile = "FFL Test"
		credit_tests.TradeInCreditFlow._ensure_open_pos_entry.__func__(cls)
		credit_tests.TradeInCreditFlow._ensure_customer("_Test Trade-in Seller")

	def setUp(self):
		super().setUp()
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in want tests"))
		p.start()
		self.addCleanup(p.stop)
		self.model = f"Zq{random.randint(10**5, 10**6)}"
		self.serial = f"TIW-{_token()}"

	def _payload(self, **over):
		return {"serial_no": self.serial, "manufacturer": "Glock", "model_name": self.model, "caliber": "9mm",
			"firearm_type": "Pistol", "condition_grade": "Good", "id_type": "Driver License", "id_number": f"DL-{self.serial}",
			"source_name": "_Test Trade-in Seller", "source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024", "acquisition_cost": 300, "retail_price": 500,
			"payout_method": "Cash", **over}

	def test_a_trade_in_paid_now_alerts(self):
		want = self._want(keywords=self.model.upper())
		res, jobs = self._arrive(lambda: trade_in.create_trade_in_intake(self._payload()))
		self.assertTrue(res["purchase_receipt"])
		self.assertEqual([j["doctype"] for j in jobs], ["Purchase Receipt"])
		self._assert_alert(want, self.serial)

	def test_a_trade_in_taken_as_credit_alerts_when_the_sale_is_rung(self):
		want = self._want(keywords=self.model)
		res, jobs = self._arrive(lambda: trade_in.create_trade_in_intake(self._payload(apply_credit=1)))
		self.assertEqual(jobs, [], "nothing is in stock until the checkout submits")

		def checkout():
			inv = self._build_credit_invoice(res["acquisition"])
			inv.payments = []
			inv.append("payments", {"mode_of_payment": "Cash", "amount": inv.grand_total})
			inv.save()
			inv.submit()

		_, jobs = self._arrive(checkout)
		self.assertEqual([j["doctype"] for j in jobs], ["Stock Entry"])
		self._assert_alert(want, self.serial)
