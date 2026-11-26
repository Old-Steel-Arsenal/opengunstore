"""Wants from what the customer said (``crm.wants.from_memory``, called by ``crm.memory``).

Real documents on the test site; the only stand-in is the memory provider (as in test_crm_memory) and the job
queue for the stock arrival (as in test_crm_wants). Every test runs inside the savepoint CRMTestCase rolls
back."""
from __future__ import annotations

import frappe
from frappe.utils import getdate

from ffl_core.crm import memory, wants
from ffl_core.tests import test_crm_memory as M
from ffl_core.tests import test_crm_wants as W
from ffl_core.tests.crm_support import MANAGER, make_item, user
from ffl_core.tests.test_crm_deal_pos import OWNER, _contact, _token
from ffl_core.tests.test_receive_goods import ReceiveGoodsFixtures

SETTINGS = "Lead Mail Settings"
WORD = "zqx7"   # a word no other item on the test site has


def _switch(on: int = 1) -> None:
	frappe.db.set_single_value(SETTINGS, wants.SWITCH, on)


def _in_stock(name: str) -> str:
	code = f"_T-WFM-{_token()}"
	make_item(code, item_name=name, is_stock_item=1, has_serial_no=1, is_firearm=1)
	sn = frappe.get_doc({"doctype": "Serial No", "serial_no": f"WFM-{_token()}", "item_code": code, "sell_price": 500})
	sn.insert(ignore_permissions=True)
	frappe.db.set_value("Serial No", sn.name, "status", "Active", update_modified=False)
	return code


def _want_row(text: str = f"{WORD} rifle", keywords: str | None = None) -> dict:
	return {"kind": "want", "text": text, "keywords": keywords or text, "source": "E1"}


def _wants_of(deal) -> list:
	return frappe.get_all(wants.WANT, filters={"deal": deal.name}, order_by="creation asc",
		fields=["name", "status", "keywords", "budget", "notes", "contact", "want_owner", "memory_row", "source_doctype",
			"source_name", "arrival_task"])


class FromMemoryCase(M.MemoryCase):
	def setUp(self):
		super().setUp()
		user(OWNER, "Sales User")
		_switch()
		self.clock = 60   # minutes ago of the next message

	def _say(self, doc, *answers):
		"""A customer message on *doc*, read by a memory run that answers *answers* (one run, one answer)."""
		self.clock -= 10
		comm = M._msg(doc, "Do you have a rifle?", minutes_ago=self.clock)
		self.assertEqual(M._run(doc, *answers), "updated")
		return comm

	def _deal(self, **fields):
		return M._deal(**{"deal_owner": OWNER, **fields})

	def _row(self, doc, kind="want"):
		return next(r for r in M._mem(doc).items if r.kind == kind)


class TestMakingAWant(FromMemoryCase):
	def test_a_want_the_customer_said_is_made_when_nothing_fits(self):
		deal = self._deal()
		comm = self._say(deal, {"add": [_want_row(), {"kind": "budget", "amount": 700, "source": "E1"}]})
		[want] = _wants_of(deal)
		self.assertEqual((want.status, want.keywords, want.budget, want.notes), ("Open", f"{WORD} rifle", 700, f"{WORD} rifle"))
		self.assertEqual((want.contact, want.want_owner), (deal.contacts[0].contact, OWNER))
		self.assertEqual((want.source_doctype, want.source_name), ("Communication", comm))
		self.assertEqual(want.memory_row, self._row(deal).name)

	def test_nothing_is_made_when_stock_fits(self):
		_in_stock(f"{WORD.title()} Rifle M1")
		deal = self._deal()
		self._say(deal, {"add": [_want_row(keywords=WORD)]})
		self.assertEqual(_wants_of(deal), [])

	def test_nothing_is_made_with_the_switch_off(self):
		_switch(0)
		deal = self._deal()
		self._say(deal, {"add": [_want_row()]})
		self.assertEqual(_wants_of(deal), [])
		self.assertEqual(len(M._mem(deal).items), 1, "the memory is written as ever")

	def test_a_staff_row_is_not_a_want(self):
		deal = self._deal()
		M._staff_memory(deal, {"kind": "want", "text": f"{WORD} rifle", "keywords": WORD, "locked": 1})
		self._say(deal, {"add": [_want_row(keywords=WORD)]})
		self.assertEqual(_wants_of(deal), [])

	def test_a_row_said_again_gets_one_want_only(self):
		deal = self._deal()
		self._say(deal, {"add": [_want_row()]})
		self._say(deal, {"add": [_want_row()]})
		self.assertEqual(len(_wants_of(deal)), 1)

	def test_a_budget_said_later_reaches_the_want(self):
		deal = self._deal()
		self._say(deal, {"add": [_want_row()]})
		self.assertFalse(_wants_of(deal)[0].budget)
		self._say(deal, {"add": [{"kind": "budget", "amount": 650, "source": "E1"}]})
		self.assertEqual(_wants_of(deal)[0].budget, 650)

	def test_a_failure_making_the_want_never_fails_the_memory_write(self):
		from unittest.mock import patch

		deal = self._deal()
		with patch.object(wants, "_write", side_effect=RuntimeError("boom")):
			self._say(deal, {"add": [_want_row()], "summary": "Wants a rifle."})
		self.assertEqual(_wants_of(deal), [])
		self.assertEqual([r.text for r in M._mem(deal).items], [f"{WORD} rifle"])
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, memory.SUMMARY), "Wants a rifle.")
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", "%Wants from memory failed%"]}))


class TestKeepingItInStep(FromMemoryCase):
	def _made(self):
		deal = self._deal()
		self._say(deal, {"add": [_want_row()]})
		return deal, frappe.get_doc(wants.WANT, _wants_of(deal)[0].name)

	def test_a_changed_row_changes_the_open_want(self):
		deal, want = self._made()
		self._say(deal, {"update": [{"ref": "M1", "text": f"{WORD} rifle in nickel", "keywords": f"{WORD} nickel", "source": "E1"}]})
		[now] = _wants_of(deal)
		self.assertEqual((now.name, now.keywords, now.notes, now.status),
			(want.name, f"{WORD} nickel", f"{WORD} rifle in nickel", "Open"))

	def test_a_removed_row_closes_the_open_want(self):
		deal, want = self._made()
		self._say(deal, {"remove": ["M1"]})
		self.assertEqual(M._mem(deal).items, [])
		self.assertEqual(_wants_of(deal)[0].status, "Closed")

	def test_a_row_trimmed_away_closes_its_want(self):
		deal, want = self._made()
		many = [{"kind": "want", "text": f"{WORD}{i} thing", "keywords": f"{WORD}{i}", "source": "E1"} for i in range(wants_limit())]
		self._say(deal, {"add": many})
		made = {w.keywords: w.status for w in _wants_of(deal)}
		self.assertEqual(made[f"{WORD} rifle"], "Closed", "the oldest row went to make room")
		self.assertEqual(sorted(s for s in made.values()), ["Closed"] + ["Open"] * wants_limit())

	def test_a_want_staff_edited_is_left_alone(self):
		deal, want = self._made()
		want.reload()
		want.keywords = f"{WORD} walnut"
		want.save()
		self._say(deal, {"update": [{"ref": "M1", "text": f"{WORD} rifle", "keywords": f"{WORD} nickel", "source": "E1"}]})
		self.assertEqual(_wants_of(deal)[0].keywords, f"{WORD} walnut")
		self._say(deal, {"remove": ["M1"]})
		self.assertEqual(_wants_of(deal)[0].status, "Open")

	def test_a_closed_want_is_not_made_again_or_reopened(self):
		deal, want = self._made()
		want.status = "Closed"
		want.save()
		self._say(deal, {"update": [{"ref": "M1", "text": f"{WORD} rifle", "keywords": f"{WORD} nickel", "source": "E1"}]})
		[now] = _wants_of(deal)
		self.assertEqual((now.status, now.keywords), ("Closed", f"{WORD} rifle"))

	def test_a_matched_want_is_left_alone(self):
		deal, want = self._made()
		frappe.db.set_value(wants.WANT, want.name, "status", "Matched", update_modified=False)
		self._say(deal, {"update": [{"ref": "M1", "text": f"{WORD} rifle", "keywords": f"{WORD} nickel", "source": "E1"}]})
		self.assertEqual((_wants_of(deal)[0].status, _wants_of(deal)[0].keywords), ("Matched", f"{WORD} rifle"))
		self._say(deal, {"remove": ["M1"]})
		self.assertEqual(_wants_of(deal)[0].status, "Matched")

	def test_merged_memories_keep_one_want_per_row(self):
		deal, want = self._made()
		contact = deal.contacts[0].contact
		other = _contact(f"{_token()}@buyer.test")
		frappe.get_doc({"doctype": memory.MEMORY, "contact": other}).insert(ignore_permissions=True)
		frappe.rename_doc("Contact", contact, other, merge=True, force=True)
		found = frappe.get_doc(memory.MEMORY, {"contact": other})
		row = next(r for r in found.items if r.kind == "want")
		self.assertEqual(frappe.db.get_value(wants.WANT, want.name, "memory_row"), row.name)
		self._say(frappe.get_doc("CRM Deal", deal.name), {"remove": ["M1"]})
		self.assertEqual(_wants_of(deal)[0].status, "Closed")


def wants_limit() -> int:
	return memory.LIMITS["want"]


class TestALead(FromMemoryCase):
	def test_a_lead_has_no_want_until_it_becomes_a_deal(self):
		lead = M._lead(lead_owner=OWNER)
		self._say(lead, {"add": [_want_row()]})
		self.assertEqual(frappe.db.count(wants.WANT, {"keywords": f"{WORD} rifle"}), 0)
		deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		[want] = _wants_of(deal)
		self.assertEqual((want.status, want.keywords, want.contact, want.want_owner),
			("Open", f"{WORD} rifle", deal.contacts[0].contact, deal.deal_owner))
		self.assertEqual(want.source_doctype, "Communication")
		self.assertEqual(want.memory_row, M._mem(deal).items[0].name)

	def test_a_lead_with_no_owner_makes_a_want_with_no_owner(self):
		lead = M._lead()
		self._say(lead, {"add": [_want_row()]})
		deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		[want] = _wants_of(deal)
		self.assertFalse(want.want_owner)

	def test_what_fits_stock_when_the_lead_becomes_a_deal_makes_no_want(self):
		_in_stock(f"{WORD.title()} Rifle M1")
		lead = M._lead()
		self._say(lead, {"add": [_want_row(keywords=WORD)]})
		deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		self.assertEqual(_wants_of(deal), [])

	def test_a_converted_leads_wants_are_made_with_the_switch_on_only(self):
		lead = M._lead()
		self._say(lead, {"add": [_want_row()]})
		_switch(0)
		deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		self.assertEqual(_wants_of(deal), [])


class TestAnArrival(FromMemoryCase, ReceiveGoodsFixtures):
	"""Receive Goods, the purchase receipt path, as in test_crm_wants."""

	_arrive = W.WantCase._arrive
	_gun_in = W.ReceiveGoodsWants._gun_in

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import custody

		custody.ensure()

	def _todos(self, task: str) -> set:
		return set(frappe.get_all("ToDo", filters={"reference_type": "CRM Task", "reference_name": task, "status": "Open"},
			pluck="allocated_to"))

	def test_a_fitting_arrival_gives_the_deals_owner_a_task(self):
		deal = self._deal()
		self._say(deal, {"add": [_want_row(keywords=WORD)]})
		_, jobs, serial, _ = self._gun_in(f"{WORD.title()} Rifle")
		self.assertEqual(len(jobs), 1)
		want = frappe.get_doc(wants.WANT, _wants_of(deal)[0].name)
		self.assertEqual((want.status, want.matched_serial), ("Matched", serial))
		task = frappe.get_doc("CRM Task", want.arrival_task)
		self.assertEqual((task.reference_docname, self._todos(task.name)), (deal.name, {OWNER}))

	def test_a_want_nobody_owns_goes_to_whoever_owns_the_deal_by_then(self):
		deal = self._deal(deal_owner=None)
		self._say(deal, {"add": [_want_row(keywords=WORD)]})
		self.assertFalse(_wants_of(deal)[0].want_owner)
		frappe.db.set_value("CRM Deal", deal.name, "deal_owner", OWNER, update_modified=False)
		self._gun_in(f"{WORD.title()} Rifle")
		want = frappe.get_doc(wants.WANT, _wants_of(deal)[0].name)
		self.assertEqual(self._todos(want.arrival_task), {OWNER})

	def test_a_want_nobody_owns_goes_to_the_sales_managers(self):
		deal = self._deal(deal_owner=None)
		self._say(deal, {"add": [_want_row(keywords=WORD)]})
		self._gun_in(f"{WORD.title()} Rifle")
		want = frappe.get_doc(wants.WANT, _wants_of(deal)[0].name)
		todos = self._todos(want.arrival_task)
		self.assertEqual(want.status, "Matched")
		self.assertFalse(want.want_owner, "the want stays unowned; only the task is assigned")
		self.assertIn(MANAGER, todos)
		self.assertEqual(todos, set(wants._managers()))
		self.assertNotIn("Administrator", todos)
		self.assertTrue(todos)
