"""The Memory card on CRM Lead and Deal pages, its Edit / Update / Clear memory, and what a draft reply is told
of the customer's memory (ffl_core.crm.memory, ffl_core.api.crm.memory_*, ffl_core.crm.reply_draft).

Real documents on the test site; the only stand-ins are the providers, functions of the memory and
draft-reply test modules registered as hooks. Every test runs inside the savepoint CRMTestCase rolls back."""
from __future__ import annotations

import json

import frappe
from frappe.utils import getdate

from ffl_core.api import crm as api
from ffl_core.crm import memory, setup
from ffl_core.tests import test_crm_reply_draft as rd
from ffl_core.tests.crm_support import CLERK, make_item, user
from ffl_core.tests.test_crm_deal_pos import OWNER
from ffl_core.tests.test_crm_memory import (ASKED, MemoryCase, _deal, _lead, _mem, _msg, _rows, _set,
	_staff_memory, no_provider, remembering)

OUTSIDER = "ffl-memory-outsider@example.com"


def _owned_deal(owner: str = OWNER):
	deal = _deal()
	frappe.db.set_value("CRM Deal", deal.name, "deal_owner", owner, update_modified=False)
	return frappe.get_doc("CRM Deal", deal.name)


def _share(deal, who: str, write: int = 0) -> None:
	frappe.share.add_docshare("CRM Deal", deal.name, who, read=1, write=write, flags={"ignore_share_permission": 1})


def _values(**over) -> str:
	values = {"want": "", "budget": None, "state": "", "trade_in": "", "preference": "", "question": "",
		"auto_update": 1}
	values.update(over)
	return json.dumps(values)


class CardCase(MemoryCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		user(OWNER, "Sales User")
		user(OUTSIDER, "Sales User")


class TestCard(CardCase):
	def test_shown_only_while_memory_is_on_or_something_is_kept(self):
		lead = _lead()
		with no_provider():
			card = api.memory_card("CRM Lead", lead.name)
			self.assertEqual((card["shown"], card["ready"], card["has_content"]), (False, False, False))
		with remembering():
			card = api.memory_card("CRM Lead", lead.name)
			self.assertEqual((card["shown"], card["ready"], card["can_edit"]), (True, True, True))
			self.assertIn("Nothing yet.", card["html"])
		_set(lead, **{memory.SUMMARY: "Wants a Garand."})
		with no_provider():
			card = api.memory_card("CRM Lead", lead.name)
			self.assertEqual((card["shown"], card["ready"], card["has_content"]), (True, False, True))
			self.assertIn("Wants a Garand.", card["html"])

	def test_rows_escaped_said_links_and_values(self):
		lead = _lead()
		_msg(lead, "Looking for a Garand", minutes_ago=30)
		with remembering(lambda c: {"add": [
				{"kind": "want", "text": "M1 Garand", "keywords": "Garand", "source": "E1"},
				{"kind": "budget", "text": "", "amount": 1500, "source": "E1"},
				{"kind": "state", "text": "Texas", "source": "E1"}], "summary": "Wants a Garand."}):
			api.memory_update_now("CRM Lead", lead.name)
		_staff_memory(lead, {"kind": "preference", "text": "Text after 5 pm", "locked": 1})
		_set(lead, **{memory.SUMMARY: "<img src=x onerror=alert(1)>"})   # set straight in the database
		card = api.memory_card("CRM Lead", lead.name)
		html = card["html"]
		self.assertNotIn("<img", html)
		self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
		raw = memory.card_html(lead, "", [frappe._dict(name="r1", kind="want", text="<script>x</script>", said="Staff")])
		self.assertIn("&lt;script&gt;x&lt;/script&gt;", raw)
		self.assertIn("1,500.00", html)
		row = next(r for r in _mem(lead).items if r.kind == "want")
		self.assertIn(f'data-ffl-memory-source="{row.name}"', html)
		self.assertEqual(html.count("data-ffl-memory-source"), 3, "the three customer rows, not the staff one")
		day = getdate()
		self.assertIn(f"said {day:%b} {day.day}", html)
		self.assertIn("Text after 5 pm</div>", html, "a staff row has no 'said'")
		self.assertEqual(card["values"]["want"], "M1 Garand")
		self.assertEqual((card["values"]["budget"], card["values"]["state"], card["values"]["auto_update"]),
			(1500, "TX", 1))
		source = api.memory_source("CRM Lead", lead.name, row.name)
		self.assertEqual((source["subject"], source["text"]), ("Question", "Looking for a Garand"))


class TestPermissions(CardCase):
	def test_read_to_see_write_to_change(self):
		deal = _owned_deal()
		_msg(deal, "Garand?", minutes_ago=30)
		_staff_memory(deal, {"kind": "want", "text": "Garand", "keywords": "Garand", "locked": 1})
		changes = (lambda: api.memory_save("CRM Deal", deal.name, _values(want="Mosin")),
			lambda: api.memory_clear("CRM Deal", deal.name),
			lambda: api.memory_update_now("CRM Deal", deal.name))
		with remembering({"add": []}):
			frappe.set_user(OUTSIDER)   # a Sales User the deal is not theirs or shared with
			for call in (lambda: api.memory_card("CRM Deal", deal.name), *changes):
				with self.assertRaises(frappe.PermissionError):
					call()
			frappe.set_user("Administrator")
			_share(deal, CLERK)
			frappe.set_user(CLERK)
			card = api.memory_card("CRM Deal", deal.name)
			self.assertEqual((card["shown"], card["can_edit"]), (True, False))
			for call in changes:
				with self.assertRaises(frappe.PermissionError):
					call()
			frappe.set_user("Administrator")
			user("ffl-memory-stock@example.com", "Stock User")
			frappe.set_user("ffl-memory-stock@example.com")
			with self.assertRaises(frappe.PermissionError):
				api.memory_card("CRM Deal", deal.name)
		frappe.set_user("Administrator")
		self.assertEqual(_rows(deal), [("want", "Garand", "Staff", 1)])
		self.assertFalse(ASKED)

	def test_a_source_only_for_its_own_row_on_a_readable_record(self):
		lead = _lead()
		frappe.db.set_value("CRM Lead", lead.name, "lead_owner", OWNER, update_modified=False)
		_msg(lead, "Garand please", minutes_ago=30)
		with remembering({"add": [{"kind": "want", "text": "Garand", "keywords": "Garand", "source": "E1"}]}):
			memory.run("CRM Lead", lead.name, manual=True)
		row = _mem(lead).items[0].name
		other = _lead()
		_staff_memory(other, {"kind": "want", "text": "Mosin", "keywords": "Mosin", "locked": 1})
		with self.assertRaises(frappe.DoesNotExistError):
			api.memory_source("CRM Lead", other.name, row)
		# The deal the lead became is shared with the clerk; the lead, where the mail is, is not.
		deal = _owned_deal()
		frappe.db.set_value("CRM Deal", deal.name, "lead", lead.name, update_modified=False)
		frappe.db.set_value("CRM Lead", lead.name, "converted", 1, update_modified=False)
		with frappe.utils.synchronization.filelock(memory.LOCK):
			memory.memory_for(frappe.get_doc("CRM Deal", deal.name), write=True)
		_share(deal, CLERK)
		frappe.set_user(CLERK)
		card = api.memory_card("CRM Deal", deal.name)
		self.assertNotIn("Garand", card["html"], "a row from a mail the clerk may not read is not shown")
		with self.assertRaises((frappe.DoesNotExistError, frappe.PermissionError)):
			api.memory_source("CRM Deal", deal.name, row)


class TestEdit(CardCase):
	def test_kept_lines_keep_their_row_new_and_changed_are_staffs(self):
		lead = _lead()
		_msg(lead, "Garand and a Mosin, budget 1500", minutes_ago=30)
		with remembering({"add": [{"kind": "want", "text": "Garand", "keywords": "Garand", "source": "E1"},
				{"kind": "want", "text": "Mosin", "keywords": "Mosin", "source": "E1"},
				{"kind": "budget", "amount": 1500, "source": "E1"}]}):
			memory.run("CRM Lead", lead.name, manual=True)
		before = {r.text: r.name for r in _mem(lead).items}
		with remembering():
			card = api.memory_save("CRM Lead", lead.name, _values(want="garand\nSKS", budget=1200, state="texas",
				question="Can you ship to CA?", auto_update=0))
		rows = {r.text: r for r in _mem(lead).items}
		self.assertEqual(rows["Garand"].name, before["Garand"], "the same line keeps its row")
		self.assertEqual((rows["Garand"].said, int(rows["Garand"].locked)), ("Customer", 0))
		self.assertNotIn("Mosin", rows)
		self.assertEqual((rows["SKS"].said, int(rows["SKS"].locked), rows["SKS"].keywords, rows["SKS"].source_name),
			("Staff", 1, "SKS", None))
		budget = next(r for r in _mem(lead).items if r.kind == "budget")
		self.assertEqual((budget.amount, budget.said, int(budget.locked)), (1200, "Staff", 1))
		self.assertEqual(rows["TX"].said, "Staff")
		self.assertEqual(rows["Can you ship to CA?"].kind, "question", "staff may keep any question")
		self.assertEqual(_mem(lead).auto_update, 0)
		self.assertEqual(card["values"]["auto_update"], 0)

	def test_the_ai_cannot_change_what_staff_wrote(self):
		lead = _lead()
		with remembering():
			api.memory_save("CRM Lead", lead.name, _values(budget=1200))
		_msg(lead, "My budget is 2000 now", minutes_ago=30)
		with remembering(lambda c: {"update": [{"ref": c["memory"][0]["ref"], "amount": 2000, "source": "E1"}],
				"add": [{"kind": "budget", "amount": 2000, "source": "E1"}]}):
			api.memory_update_now("CRM Lead", lead.name)
		self.assertEqual([(r.kind, r.amount, r.said) for r in _mem(lead).items], [("budget", 1200, "Staff")])

	def test_refused_values_save_nothing(self):
		lead = _lead()
		with remembering():
			api.memory_save("CRM Lead", lead.name, _values(want="Garand"))
			for bad in (dict(preference="DL TX12345678"), dict(want="Garand\nGlock is $499"),
					dict(trade_in="SKS, in stock"), dict(question="I was convicted of a felony, can I buy?"),
					dict(state="Narnia"), dict(budget=-5), dict(want="\n".join(f"Gun {i}" for i in range(7)))):
				with self.assertRaises(frappe.ValidationError):
					api.memory_save("CRM Lead", lead.name, _values(**{"want": "Garand\nMosin", **bad}))
		self.assertEqual(_rows(lead), [("want", "Garand", "Staff", 1)])

	def test_clear_keeps_the_watermark_and_the_switch(self):
		lead = _lead()
		_msg(lead, "Garand", minutes_ago=30)
		with remembering({"add": [{"kind": "want", "text": "Garand", "keywords": "Garand", "source": "E1"}],
				"summary": "Wants a Garand."}):
			memory.run("CRM Lead", lead.name, manual=True)
		_mem(lead).db_set("auto_update", 0)
		seen = frappe.db.get_value("CRM Lead", lead.name, memory.SEEN)
		with no_provider():
			card = api.memory_clear("CRM Lead", lead.name)
		self.assertEqual((card["shown"], card["has_content"]), (False, False))
		self.assertEqual(_rows(lead), [])
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, memory.SUMMARY))
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN), seen)
		self.assertEqual(_mem(lead).auto_update, 0)


class TestUpdateNow(CardCase):
	def test_each_outcome_tells_the_employee(self):
		lead = _lead()
		with no_provider(), self.assertRaisesRegex(frappe.ValidationError, "Customer memory is off."):
			api.memory_update_now("CRM Lead", lead.name)
		with remembering({"add": []}), self.assertRaisesRegex(frappe.ValidationError, "Nothing from the customer"):
			api.memory_update_now("CRM Lead", lead.name)
		_msg(lead, "Garand?", minutes_ago=30)
		with remembering({"refused": "Today's AI limit is used up."}), \
				self.assertRaisesRegex(frappe.ValidationError, "Today's AI limit is used up."):
			api.memory_update_now("CRM Lead", lead.name)
		with remembering(None), self.assertRaisesRegex(frappe.ValidationError, memory.UPDATE_FAILED):
			api.memory_update_now("CRM Lead", lead.name)
		with remembering(RuntimeError("provider down")), self.assertRaisesRegex(frappe.ValidationError,
				memory.UPDATE_FAILED):
			api.memory_update_now("CRM Lead", lead.name)
		with remembering({"add": [{"kind": "want", "text": "Garand", "keywords": "Garand", "source": "E1"}],
				"summary": "Asked about a Garand."}):
			card = api.memory_update_now("CRM Lead", lead.name)
		self.assertIn("Asked about a Garand.", card["html"])
		self.assertEqual(ASKED[-1]["trigger"], "manual")
		_mem(lead).db_set("auto_update", 0)
		with remembering({"add": []}), self.assertRaisesRegex(frappe.ValidationError, "Update with AI is off"):
			api.memory_update_now("CRM Lead", lead.name)


class TestLayout(CardCase):
	def _sections(self, doctype):
		layout = frappe.db.get_value("CRM Fields Layout", f"{doctype}-Side Panel", "layout")
		return [s.get("name") for s in json.loads(layout or "[]")]

	def test_the_section_follows_the_switch_and_what_is_kept(self):
		for doctype in memory.DOCTYPES:
			self.assertTrue(frappe.get_meta(doctype).get_field(memory.CARD_FIELD).hidden)
		frappe.db.delete(f"{memory.MEMORY} Item")
		for doctype in memory.DOCTYPES:
			frappe.db.sql(f"UPDATE `tab{doctype}` SET `{memory.SUMMARY}` = NULL")
		with no_provider():
			memory.refresh_layout()
			for doctype in memory.DOCTYPES:
				self.assertNotIn(memory.SECTION, self._sections(doctype))
		with remembering():
			self.assertTrue(memory.refresh_layout())
			self.assertFalse(memory.refresh_layout(), "already there")
		deal = self._sections("CRM Deal")
		for above in ("contacts_section", setup.CARD_SECTION, setup.SELL_SECTION):
			if above in deal:
				self.assertLess(deal.index(above), deal.index(memory.SECTION))
		self.assertIn(memory.SECTION, self._sections("CRM Lead"))
		_set(_lead(), **{memory.SUMMARY: "Wants a Garand."})
		with no_provider():
			memory.refresh_layout()
			self.assertIn(memory.SECTION, self._sections("CRM Lead"), "kept while something is kept")

	def test_the_page_scripts_carry_the_card(self):
		setup.ensure_reply_scripts()
		for name in (setup.LEAD_SCRIPT, setup.DEAL_REPLY_SCRIPT):
			script = frappe.db.get_value("CRM Form Script", name, "script")
			for needle in ("ffl_core.api.crm.memory_card", "Edit memory", "Update memory", "Clear memory",
					f'setFieldProperty("{memory.CARD_FIELD}", "hidden"', "data-ffl-memory-source"):
				self.assertIn(needle, script)


class TestDraft(CardCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for code, name, price in (("_T-MEM-GARAND-A", "M1 Garand Service Grade", 900),
				("_T-MEM-GARAND-B", "M1 Garand Field Grade", 1450), ("_T-MEM-GARAND-C", "M1 Garand Correct Grade", 2100)):
			make_item(code, item_name=name, is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1,
				standard_rate=price)

	def setUp(self):
		super().setUp()
		for code in ("_T-MEM-GARAND-A", "_T-MEM-GARAND-B", "_T-MEM-GARAND-C"):
			rd._gun(code, f"_T-MEM-SN-{code[-1]}-{frappe.generate_hash(length=6)}", 0)

	def _draft(self, doc):
		with rd.drafting():
			api.draft_reply(doc.doctype, doc.name)
		return rd.ASKED[0]

	def test_the_memory_is_in_the_draft_without_sources_and_blanked(self):
		deal = rd._deal()
		comm = rd._mail(deal, "<p>Looking for a Garand. My DOB 1/2/1980.</p>")
		_staff_memory(deal, {"kind": "want", "text": "Garand", "keywords": "Garand", "locked": 1},
			{"kind": "preference", "text": "Text after 5 pm", "said": "Customer", "source_doctype": "Communication",
				"source_name": comm})
		# A staff row kept before a rule that now refuses it: never sent.
		frappe.get_doc({"doctype": f"{memory.MEMORY} Item", "parent": _mem(deal).name, "parenttype": memory.MEMORY,
			"parentfield": "items", "idx": 9, "kind": "question", "text": "SSN 123-45-6789 ok?", "said": "Staff",
			"locked": 1}).db_insert()
		_set(deal, **{memory.SUMMARY: "Wants a Garand. DOB 1/2/1980."})
		ctx = self._draft(deal)
		self.assertEqual(ctx["memory"]["summary"], "Wants a Garand. DOB [removed].")
		self.assertEqual([(i["kind"], i["text"], i["said"]) for i in ctx["memory"]["items"]],
			[("want", "Garand", "staff"), ("preference", "Text after 5 pm", "customer")])
		self.assertEqual(set(ctx["memory"]["items"][0]), {"kind", "text", "amount", "said", "on"})
		flat = json.dumps(ctx["memory"])
		for secret in (comm, "123-45-6789", "1/2/1980", "keywords", "locked"):
			self.assertNotIn(secret, flat)
		self.assertIn("1/2/1980", ctx["latest"]["body"], "the draft's own messages are sent as before")

	def test_no_memory_is_an_empty_one(self):
		deal = rd._deal()
		rd._mail(deal, "<p>Hi</p>")
		self.assertEqual(self._draft(deal)["memory"], {"summary": "", "items": []})

	def test_remembered_wants_bring_stock_nearest_the_budget_first(self):
		deal = rd._deal()
		rd._mail(deal, "<p>Any Garands?</p>")
		_staff_memory(deal, {"kind": "want", "text": "M1 Garand", "keywords": "Garand", "locked": 1})
		ours = ("M1 Garand Correct Grade", "M1 Garand Field Grade", "M1 Garand Service Grade")
		names = [f["name"] for f in self._draft(deal)["facts"] if f["name"] in ours]
		self.assertEqual(sorted(names), list(ours))
		with remembering():
			api.memory_save("CRM Deal", deal.name, _values(want="M1 Garand", budget=1500))
		names = [f["name"] for f in self._draft(deal)["facts"] if f["name"] in ours]
		self.assertEqual(names, ["M1 Garand Field Grade", "M1 Garand Service Grade", "M1 Garand Correct Grade"])

	def test_a_lead_draft_reads_its_memory(self):
		lead = rd._lead()
		rd._mail(lead, "<p>Garand?</p>")
		with remembering():
			api.memory_save("CRM Lead", lead.name, _values(preference="Spanish"))
		self.assertEqual([i["text"] for i in self._draft(lead)["memory"]["items"]], ["Spanish"])



# ── review round 1 ──

def _person_deal(contact: str, owner: str):
	"""A deal about *contact*, owned by *owner*."""
	deal = _deal(contact)
	frappe.db.set_value("CRM Deal", deal.name, "deal_owner", owner, update_modified=False)
	return frappe.get_doc("CRM Deal", deal.name)


def _converted(lead, deal) -> None:
	frappe.db.set_value("CRM Lead", lead.name, "converted", 1, update_modified=False)
	frappe.db.set_value("CRM Deal", deal.name, "lead", lead.name, update_modified=False)


class TestConvertedLead(CardCase):
	def test_a_converted_lead_is_checked_on_its_deal(self):
		lead = _lead(lead_owner=CLERK)
		deal = _owned_deal(OWNER)   # not the clerk's, not shared
		_converted(lead, deal)
		_msg(deal, "Garand?", minutes_ago=30)
		with remembering({"add": []}):
			frappe.set_user(CLERK)
			for call in (lambda: api.memory_card("CRM Lead", lead.name),
					lambda: api.memory_save("CRM Lead", lead.name, _values(want="Garand")),
					lambda: api.memory_clear("CRM Lead", lead.name),
					lambda: api.memory_update_now("CRM Lead", lead.name)):
				with self.assertRaises(frappe.PermissionError):
					call()
			frappe.set_user("Administrator")
			_share(deal, CLERK)   # read only
			frappe.set_user(CLERK)
			self.assertFalse(api.memory_card("CRM Lead", lead.name)["can_edit"])
			with self.assertRaises(frappe.PermissionError):
				api.memory_save("CRM Lead", lead.name, _values(want="Garand"))
		frappe.set_user("Administrator")
		self.assertEqual(_rows(deal), [])
		self.assertFalse(ASKED)

	def test_a_converted_lead_shows_its_deals_summary(self):
		lead = _lead()
		deal = _owned_deal("Administrator")
		_converted(lead, deal)
		_set(lead, **{memory.SUMMARY: "Old lead summary."})
		_set(deal, **{memory.SUMMARY: "Deal summary."})
		with remembering():
			html = api.memory_card("CRM Lead", lead.name)["html"]
		self.assertIn("Deal summary.", html)
		self.assertNotIn("Old lead summary.", html)


class TestHiddenSources(CardCase):
	"""One person, two deals: the clerk's own, and another employee's they may not read."""

	def _setup(self):
		contact = rd._contact(f"{frappe.generate_hash(length=8)}@buyer.test")
		mine = _person_deal(contact, CLERK)
		theirs = _person_deal(contact, OWNER)
		_msg(theirs, "Private: looking for a Garand", minutes_ago=30)
		with remembering({"add": [{"kind": "want", "text": "Private Garand", "keywords": "Garand", "source": "E1"}]}):
			self.assertEqual(memory.run("CRM Deal", theirs.name, manual=True)["outcome"], "updated")
		_staff_memory(mine, {"kind": "preference", "text": "Text after 5 pm", "locked": 1})
		return mine, theirs

	def test_rows_from_a_record_the_caller_cannot_read_are_not_shown_or_sent(self):
		mine, _theirs = self._setup()
		rd._mail(mine, "<p>Any news?</p>")
		frappe.set_user(CLERK)
		with remembering():
			card = api.memory_card("CRM Deal", mine.name)
		self.assertNotIn("Private Garand", card["html"])
		self.assertIn("Text after 5 pm", card["html"])
		self.assertEqual(card["values"]["want"], "")
		with rd.drafting():
			api.draft_reply("CRM Deal", mine.name)
		self.assertEqual([i["text"] for i in rd.ASKED[0]["memory"]["items"]], ["Text after 5 pm"])

	def test_a_save_keeps_the_rows_the_caller_cannot_see(self):
		mine, _theirs = self._setup()
		frappe.set_user(CLERK)
		with remembering():
			card = api.memory_card("CRM Deal", mine.name)
			api.memory_save("CRM Deal", mine.name, json.dumps({**card["values"], "want": "SKS"}))
		frappe.set_user("Administrator")
		self.assertEqual(sorted(r[1] for r in _rows(mine)), ["Private Garand", "SKS", "Text after 5 pm"])

	def test_clear_clears_the_summaries_the_caller_may_change(self):
		mine, theirs = self._setup()
		shared = _person_deal(frappe.db.get_value("CRM Contacts", {"parent": mine.name}, "contact"), OWNER)
		_share(shared, CLERK, write=1)
		for doc in (mine, theirs, shared):
			_set(doc, **{memory.SUMMARY: f"Summary of {doc.name}"})
		frappe.set_user(CLERK)
		card = api.memory_clear("CRM Deal", mine.name)
		frappe.set_user("Administrator")
		self.assertEqual((card["summaries_cleared"], card["summaries_kept"]), (2, 1))
		self.assertFalse(frappe.db.get_value("CRM Deal", mine.name, memory.SUMMARY))
		self.assertFalse(frappe.db.get_value("CRM Deal", shared.name, memory.SUMMARY))
		self.assertTrue(frappe.db.get_value("CRM Deal", theirs.name, memory.SUMMARY))
		self.assertEqual([r[1] for r in _rows(mine)], ["Private Garand"], "a row the clerk cannot see stays")


class TestMigrateLayout(CardCase):
	def _without_section(self, doctype):
		name = f"{doctype}-Side Panel"
		sections = [s for s in json.loads(frappe.db.get_value("CRM Fields Layout", name, "layout") or "[]")
			if s.get("name") != memory.SECTION]
		frappe.db.set_value("CRM Fields Layout", name, "layout", json.dumps(sections))

	def _has_section(self, doctype):
		layout = frappe.db.get_value("CRM Fields Layout", f"{doctype}-Side Panel", "layout") or ""
		return memory.SECTION in layout

	def test_migrate_leaves_a_removed_section_removed(self):
		frappe.db.set_default(memory.PLACED, None)
		with remembering():
			for doctype in memory.DOCTYPES:
				self._without_section(doctype)
			memory.refresh_layout(on_migrate=True)
			self.assertTrue(self._has_section("CRM Lead"), "placed the first time")
			for doctype in memory.DOCTYPES:
				self._without_section(doctype)
			memory.refresh_layout(on_migrate=True)
			self.assertFalse(self._has_section("CRM Lead"), "a manager removed it: migrate leaves it")
			self.assertFalse(self._has_section("CRM Deal"))
			memory.refresh_layout()   # the provider's switch changed
			self.assertTrue(self._has_section("CRM Lead"))


class TestNamedRefusal(CardCase):
	def test_an_old_row_that_breaks_a_rule_is_named(self):
		lead = _lead()
		_staff_memory(lead, {"kind": "want", "text": "Garand", "keywords": "Garand", "locked": 1})
		frappe.get_doc({"doctype": f"{memory.MEMORY} Item", "parent": _mem(lead).name, "parenttype": memory.MEMORY,
			"parentfield": "items", "idx": 5, "kind": "preference", "text": "DL TX12345678 on file", "said": "Staff",
			"locked": 1}).db_insert()
		with remembering():
			values = api.memory_card("CRM Lead", lead.name)["values"]
			with self.assertRaisesRegex(frappe.ValidationError, "Prefers"):
				api.memory_save("CRM Lead", lead.name, json.dumps({**values, "want": "Garand\nSKS"}))
			api.memory_save("CRM Lead", lead.name, json.dumps({**values, "want": "Garand\nSKS", "preference": ""}))
		self.assertEqual([r[1] for r in _rows(lead)], ["Garand", "SKS"])
