"""A reply drafted when the customer's mail arrives (ffl_core.crm.arrival_draft): marking, the sweep and
the run, and when Draft reply gives the stored draft instead of asking the provider.

Real documents on the test site; the only stand-in is the provider, functions of this module registered
as the reply-draft hooks (``drafting``), which count the calls. Every test runs inside the savepoint
CRMTestCase rolls back."""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from ffl_core.api import crm as api
from ffl_core.crm import arrival_draft as ad
from ffl_core.crm import inquiries, mail, memory, reply_draft, setup
from ffl_core.crm.sell_requests import SELL
from ffl_core.tests import test_crm_reply_draft as rd
from ffl_core.tests.crm_support import CLERK, product_of
from ffl_core.tests.test_crm_deal_pos import OWNER, _contact, _token
from ffl_core.tests.test_crm_mail import TEAM, _inbox, _receive, _settings
from ffl_core.tests.test_extension_hooks import hooks

HERE = "ffl_core.tests.test_crm_arrival_draft"
CALLS: list = []
ANSWERS: list = []
READY = [True]
MEMORY_ON = [False]
BODY = "Hi Sam,\n\nThe Glock is in."


def ready():
	return READY[0]


def memory_ready():
	return MEMORY_ON[0]


def provider(context):
	CALLS.append(context)
	answer = ANSWERS.pop(0) if ANSWERS else {"body": BODY, "handoff": None, "refs": []}
	if isinstance(answer, Exception):
		raise answer
	return answer(context) if callable(answer) else answer


@contextmanager
def drafting(*answers, ready_answer=True, memory_on=False):
	"""The provider answers *answers* in turn, then ``BODY``. Customer memory is off unless *memory_on*."""
	CALLS.clear()
	ANSWERS[:] = list(answers)
	READY[0] = ready_answer
	MEMORY_ON[0] = memory_on
	frappe.local.flags.pop(memory.WANTED_FLAG, None)
	with hooks(reply_draft.READY_HOOK, [f"{HERE}.ready"]), hooks(reply_draft.DRAFT_HOOK, [f"{HERE}.provider"]), \
			hooks(memory.READY_HOOK, [f"{HERE}.memory_ready"]), hooks(memory.UPDATE_HOOK, []):
		yield
	frappe.local.flags.pop(memory.WANTED_FLAG, None)


def _on(value: int = 1) -> None:
	frappe.db.set_single_value(ad.SETTINGS, ad.SWITCH, value)


def _set(doc, **values):
	frappe.db.set_value(doc.doctype, doc.name, values, update_modified=False)


def _due(doc):
	return frappe.db.get_value(doc.doctype, doc.name, ad.DUE)


def _stored(doc) -> frappe._dict:
	return frappe.db.get_value(doc.doctype, doc.name, [ad.HTML, ad.HANDOFF, ad.FOR, ad.AT, ad.PRINT], as_dict=True)


def _ahead(doc, *answers) -> str:
	"""Mark *doc* as a customer message does and run the queued job; returns the outcome."""
	with drafting(*answers):
		ad.mark_due(doc.doctype, doc.name)
		return ad.run(doc.doctype, doc.name)["outcome"]


def _glock_deal(**fields):
	"""A deal for the Glock, two guns of it in stock, and the customer's mail asking about it."""
	rd._gun(rd.GLOCK, "_T-AD-SN-1", 499)
	rd._gun(rd.GLOCK, "_T-AD-SN-2", 549)
	deal = rd._deal(products=[{"product_code": product_of(rd.GLOCK), "qty": 1}], **fields)
	comm = rd._mail(deal, "<p>Is the Glock 19 still there?</p>", at=rd._ago(10))
	return deal, comm


def _click(doc, **kw) -> dict:
	"""Draft reply, with the provider counting: CALLS is empty when the stored draft was given."""
	with drafting({"body": "Written now.", "handoff": None}):
		return api.draft_reply(doc.doctype, doc.name, **kw)


class ArrivalCase(rd.ReplyCase):
	def setUp(self):
		super().setUp()
		_on(1)


class TestMarking(ArrivalCase):
	def setUp(self):
		super().setUp()
		_inbox()
		_settings(blocked_sender_domains="spam.test", own_domains="store.test",
			team_addresses=[{"address": TEAM, "address_type": "Group"}], primary_team_address="")
		self.enterContext(hooks(mail.SCREEN_HOOK, []))

	def tearDown(self):
		super().tearDown()
		frappe.clear_document_cache(mail.SETTINGS, mail.SETTINGS)

	def test_a_customer_mail_and_a_website_inquiry_mark_the_record(self):
		with drafting():
			comm = _receive("New Buyer <new-buyer@buyer.test>", "New Buyer", recipients=TEAM)
			lead = frappe.get_doc("CRM Lead", comm.reload().reference_name)
			self.assertTrue(_due(lead))
			out = inquiries.file_inquiry(kind="ask", event_id=f"evt-{_token()}", email=f"{_token()}@buyer.test",
				message="Garand?")
			self.assertTrue(_due(frappe.get_doc(out["doctype"], out["name"])))
		self.assertFalse(CALLS, "marking asks nothing")

	def test_what_never_marks(self):
		with drafting():
			other = _receive("noreply@shop.test", recipients=TEAM)   # automated: other mail
			self.assertEqual(frappe.db.get_value("CRM Lead", other.reload().reference_name, "status"), mail.OTHER_MAIL)
			self.assertFalse(frappe.db.get_value("CRM Lead", other.reference_name, ad.DUE))
			real = rd._lead()
			_receive("no-reply@notices.test", recipients=TEAM, reference_doctype="CRM Lead", reference_name=real.name)
			self.assertFalse(_due(real), "a machine's mail is not the customer's")
		with drafting(ready_answer=False):
			comm = _receive("Other Buyer <other-buyer@buyer.test>", recipients=TEAM)
			self.assertFalse(frappe.db.get_value("CRM Lead", comm.reload().reference_name, ad.DUE), "no provider drafting")
		_on(0)
		with drafting():
			comm = _receive("Third Buyer <third-buyer@buyer.test>", recipients=TEAM)
			self.assertFalse(frappe.db.get_value("CRM Lead", comm.reload().reference_name, ad.DUE), "switch off")

	def test_a_failure_never_stops_the_mail(self):
		with drafting(), patch.object(ad, "now_datetime", side_effect=RuntimeError("boom")):
			comm = _receive("Broken Buyer <broken@buyer.test>", recipients=TEAM)
		self.assertEqual(comm.reload().reference_doctype, "CRM Lead")
		self.assertFalse(frappe.db.get_value("CRM Lead", comm.reference_name, ad.DUE))
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", "%Draft not marked%"]}))


class TestSweep(ArrivalCase):
	def test_quiet_records_after_the_memory_ten_at_most(self):
		lead = rd._lead()
		rd._mail(lead, "<p>Glock?</p>")
		with drafting():
			ad.mark_due("CRM Lead", lead.name)
			self.assertNotIn(("CRM Lead", lead.name), ad.sweep(), "a mail in the last three minutes: wait")
			_set(lead, **{ad.DUE: rd._ago(4)})
			self.assertIn(("CRM Lead", lead.name), ad.sweep())
		with drafting(memory_on=True):
			_set(lead, **{memory.DUE: rd._ago(4)})
			self.assertNotIn(("CRM Lead", lead.name), ad.sweep(), "the memory reads it first")
			_set(lead, **{memory.DUE: add_to_date(now_datetime(), hours=1)})
			self.assertIn(("CRM Lead", lead.name), ad.sweep(), "a memory retry an hour out does not hold it up")
			_set(lead, **{memory.DUE: None})
			self.assertIn(("CRM Lead", lead.name), ad.sweep())
		with drafting():
			_set(lead, **{memory.DUE: rd._ago(4)})
			self.assertIn(("CRM Lead", lead.name), ad.sweep(), "memory off: its old mark is not waited for")
			for i in range(11):
				_set(rd._lead(), **{ad.DUE: rd._ago(10 + i)})
			swept = ad.sweep()
			self.assertEqual(len(swept), 10)
			self.assertTrue(all(_due(frappe._dict(doctype=d, name=n)) for d, n in swept), "never one with no mark")
		with drafting(ready_answer=False):
			self.assertEqual(ad.sweep(), [])
		_on(0)
		with drafting():
			self.assertEqual(ad.sweep(), [])


class TestRun(ArrivalCase):
	def test_the_run_stores_the_draft(self):
		deal, comm = _glock_deal()
		self.assertEqual(_ahead(deal, {"body": BODY, "handoff": "Asks for a trade-in value"}), "stored")
		self.assertEqual(len(CALLS), 1)
		self.assertEqual(CALLS[0]["latest"]["body"], "Is the Glock 19 still there?")
		row = _stored(deal)
		self.assertEqual((row[ad.HTML], row[ad.HANDOFF], row[ad.FOR]),
			(reply_draft.to_html(BODY), "Asks for a trade-in value", f"in:{comm}"))
		self.assertTrue(row[ad.AT])
		self.assertEqual(json.loads(row[ad.PRINT])["items"], [rd.GLOCK])
		self.assertFalse(_due(deal))

	def test_draft_reply_gives_the_stored_draft_without_asking(self):
		deal, comm = _glock_deal()
		self.assertEqual(_ahead(deal, {"body": BODY, "handoff": "Asks for a trade-in value"}), "stored")
		out = _click(deal)
		self.assertEqual(CALLS, [])
		self.assertEqual((out["content_html"], out["handoff"], out["in_reply_to"], out["to"]),
			(reply_draft.to_html(BODY), "Asks for a trade-in value", comm, "sam@example.com"))
		# The deal's owner gets it too, with their own From and signature, worked out now.
		frappe.set_user(CLERK)
		out = _click(deal, quote_text="Is the Glock 19 still there?", composer=1)
		self.assertEqual((CALLS, out["native_ok"], out["content_html"]), ([], 1, reply_draft.to_html(BODY)))
		frappe.set_user("Administrator")
		# Replying to an older mail is another message: drafted now.
		older = rd._mail(deal, "<p>First question</p>", at=rd._ago(60))
		out = _click(deal, quote_text="First question", composer=1)
		self.assertEqual((len(CALLS), out["in_reply_to"], out["content_html"]), (1, older, "<p>Written now.</p>"))

	def test_a_changed_price_or_stock_drafts_now(self):
		changes = {
			"a gun's price": lambda: frappe.db.set_value("Serial No", "_T-AD-SN-1", "sell_price", 479),
			"a gun sold": lambda: frappe.db.set_value("Serial No", "_T-AD-SN-2", "status", "Delivered"),
			"a gun came in": lambda: rd._gun(rd.GLOCK, "_T-AD-SN-3", 519),
		}
		for what, change in changes.items():
			with self.subTest(what):
				frappe.db.savepoint("ffl_ad_change")
				deal, _comm = _glock_deal()
				self.assertEqual(_ahead(deal), "stored")
				_click(deal)
				self.assertEqual(CALLS, [], "unchanged: the stored draft")
				change()
				out = _click(deal)
				self.assertEqual((len(CALLS), out["content_html"]), (1, "<p>Written now.</p>"))
				frappe.db.rollback(save_point="ffl_ad_change")

	def test_the_facts_the_provider_found_are_checked_too(self):
		rd._gun(rd.MOSIN, "_T-AD-SN-M1", 0)
		deal, _comm = _glock_deal()

		def found(ctx):
			ctx["find_stock"]([{"keywords": "mosin"}])
			return {"body": BODY, "handoff": None}

		self.assertEqual(_ahead(deal, found), "stored")
		self.assertIn(rd.MOSIN, json.loads(_stored(deal)[ad.PRINT])["items"])
		_click(deal)
		self.assertEqual(CALLS, [])
		frappe.db.set_value("Item", rd.MOSIN, {"sale_price": 249, "sale_from": add_days(nowdate(), -1),
			"sale_to": add_days(nowdate(), 5)}, update_modified=False)
		_click(deal)
		self.assertEqual(len(CALLS), 1, "the Mosin it found went on sale")

	def test_a_newer_message_or_a_sent_mail_drops_it(self):
		deal, _comm = _glock_deal()
		self.assertEqual(_ahead(deal), "stored")
		with drafting():
			ad.mark_due("CRM Deal", deal.name)   # what crm.mail.route does with the customer's next mail
		self.assertFalse(_stored(deal)[ad.HTML])
		self.assertTrue(_due(deal))
		self.assertEqual(_ahead(deal), "stored")
		rd._mail(deal, "<p>Yes, it is here.</p>", received=False)
		self.assertFalse(_stored(deal)[ad.HTML])
		self.assertFalse(_due(deal))
		# A lead's sent mail drops its deal's too.
		lead = rd._lead()
		deal = rd._deal(lead=lead.name)
		rd._mail(deal, "<p>Glock?</p>", at=rd._ago(10))
		frappe.db.set_value("CRM Lead", lead.name, "converted", 1, update_modified=False)
		self.assertEqual(_ahead(deal), "stored")
		rd._mail(lead, "<p>Sent from the lead</p>", received=False)
		self.assertFalse(_stored(deal)[ad.HTML])

	def test_a_message_during_the_run_is_not_overwritten(self):
		deal, _comm = _glock_deal()

		def meanwhile(ctx):
			rd._mail(deal, "<p>Also, a Mosin?</p>")
			ad.mark_due("CRM Deal", deal.name)   # as the mail's route marks it
			return {"body": BODY, "handoff": None}

		self.assertEqual(_ahead(deal, meanwhile), "superseded")
		self.assertFalse(_stored(deal)[ad.HTML])
		self.assertTrue(_due(deal), "the newer mark stays for the next round")

	def test_switch_off_does_nothing(self):
		deal, _comm = _glock_deal()
		self.assertEqual(_ahead(deal), "stored")
		_on(0)
		_click(deal)
		self.assertEqual(len(CALLS), 1, "a stored draft is not used with the switch off")
		with drafting():
			self.assertFalse(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"])
			_set(deal, **{ad.DUE: rd._ago(10)})
			self.assertEqual(ad.run("CRM Deal", deal.name)["outcome"], "off")
		self.assertFalse(CALLS)
		self.assertFalse(_due(deal))

	def test_no_answer_stores_nothing_and_is_not_asked_again(self):
		answers = {"failure": RuntimeError("boom"), "refusal": {"refused": "Today's AI limit is used up."},
			"deferral": {"deferred": None}, "no answer": None, "empty": {"body": "  "}}
		deal, _comm = _glock_deal()
		for what, answer in answers.items():
			with self.subTest(what):
				self.assertEqual(_ahead(deal, answer), "no_answer")
				self.assertEqual(len(CALLS), 1)
				self.assertFalse(_stored(deal)[ad.HTML])
				self.assertFalse(_due(deal), "tried again only after the next customer message")
				with drafting():
					self.assertNotIn(("CRM Deal", deal.name), ad.sweep())
		self.assertFalse(any("AI limit" in str(m) for m in frappe.local.message_log), "a background refusal shows nothing")

	def test_a_draft_with_an_id_number_is_not_stored(self):
		deal, _comm = _glock_deal()
		self.assertEqual(_ahead(deal, {"body": "Your SSN 123-45-6789 is on file.", "handoff": None}), "sensitive")
		self.assertFalse(_stored(deal)[ad.HTML])
		self.assertFalse(_due(deal))

	def test_no_draft_ahead_for_other_mail_closed_sell_requests_or_a_machine(self):
		other = rd._lead()
		rd._mail(other, "<p>Newsletter</p>")
		_set(other, status=mail.OTHER_MAIL)
		lost = frappe.get_all("CRM Deal Status", filters={"type": "Lost"}, pluck="name", limit=1)[0]
		closed, _c = _glock_deal()
		_set(closed, status=lost)
		sell = rd._deal(**{SELL: 1})
		rd._mail(sell, "<p>I have a gun to sell</p>")
		machine = rd._deal()
		rd._mail(machine, "<p>Your order shipped</p>", sender="noreply@shop.test")
		for doc in (other, closed, sell, machine):
			with self.subTest(doc.name):
				_set(doc, **{ad.DUE: rd._ago(10)})
				with drafting():
					self.assertEqual(ad.run(doc.doctype, doc.name)["outcome"], "skipped")
				self.assertFalse(CALLS)
				self.assertFalse(_due(doc))

	def test_a_lead_becoming_a_deal(self):
		lead = rd._lead()
		rd._mail(lead, "<p>Glock?</p>", at=rd._ago(10))
		self.assertEqual(_ahead(lead), "stored")
		with drafting():
			deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		self.assertFalse(_stored(deal)[ad.HTML], "the lead's draft is not the deal's")
		self.assertTrue(_due(deal), "the deal gets its own")
		with drafting():
			_set(lead, **{ad.DUE: rd._ago(10)})
			self.assertEqual(ad.run("CRM Lead", lead.name)["outcome"], "moved")
			self.assertEqual(ad.run("CRM Deal", deal.name)["outcome"], "stored")


class TestWhoGetsIt(ArrivalCase):
	def test_a_user_who_cannot_read_the_deals_lead_gets_a_draft_of_their_own(self):
		lead = rd._lead(lead_owner=OWNER)   # not the clerk's: they may not read it
		deal = rd._deal(lead=lead.name)     # the clerk's
		rd._mail(deal, "<p>On the deal, older</p>", at=rd._ago(30))
		newest = rd._mail(lead, "<p>On the lead, newest</p>", at=rd._ago(5))
		self.assertEqual(_ahead(deal, {"body": "Written from the lead.", "handoff": None}), "stored")
		self.assertEqual(CALLS[0]["latest"]["body"], "On the lead, newest")
		self.assertEqual(_stored(deal)[ad.FOR], f"in:{newest}")
		frappe.set_user(CLERK)
		with drafting():
			self.assertEqual(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"], False)
		out = _click(deal)
		self.assertEqual(len(CALLS), 1)
		self.assertEqual(CALLS[0]["latest"]["body"], "On the deal, older")
		self.assertEqual(out["content_html"], "<p>Written now.</p>")
		frappe.set_user("Administrator")
		self.assertEqual(_click(deal)["content_html"], "<p>Written from the lead.</p>")
		self.assertEqual(CALLS, [], "whoever may read the lead gets the stored one")

	def test_memory_from_a_record_the_user_cannot_read_is_not_handed_over(self):
		from ffl_core.tests.test_crm_memory import remembering

		contact = _contact(f"{_token()}@buyer.test", first_name="Sam")
		mine = rd._deal(contact)                 # the clerk's
		theirs = rd._deal(contact, owner=OWNER)   # the same person, another employee's
		rd._mail(theirs, "<p>Private: looking for a Garand</p>", at=rd._ago(30))
		with remembering({"add": [{"kind": "want", "text": "Private Garand", "keywords": "Garand", "source": "E1"}]}):
			self.assertEqual(memory.run("CRM Deal", theirs.name, manual=True)["outcome"], "updated")
		rd._mail(mine, "<p>Any news?</p>", at=rd._ago(5))
		self.assertEqual(_ahead(mine), "stored")
		self.assertEqual([i["text"] for i in CALLS[0]["memory"]["items"]], ["Private Garand"])
		frappe.set_user(CLERK)
		with drafting():
			self.assertFalse(api.reply_draft_ready("CRM Deal", mine.name)["draft_ready"])
		_click(mine)
		self.assertEqual(len(CALLS), 1)
		self.assertEqual(CALLS[0]["memory"]["items"], [])

	def test_read_only_users_get_neither_the_draft_nor_its_text(self):
		deal, _comm = _glock_deal(owner=OWNER)
		self.assertEqual(_ahead(deal), "stored")
		self.assertTrue(frappe.client.get("CRM Deal", deal.name).get(ad.HTML), "Administrator reads every level")
		frappe.share.add_docshare("CRM Deal", deal.name, CLERK, read=1, write=0, flags={"ignore_share_permission": 1})
		frappe.set_user(CLERK)
		with drafting():
			self.assertFalse(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"])
			with self.assertRaises(frappe.PermissionError):
				api.draft_reply("CRM Deal", deal.name)
		read = frappe.client.get("CRM Deal", deal.name)
		self.assertTrue(read.get("name"))
		for field in ad.STORED:
			self.assertIsNone(read.get(field), field)
		try:
			listed = frappe.get_list("CRM Deal", filters={"name": deal.name}, fields=["name", ad.HTML])
		except frappe.PermissionError:
			listed = []
		self.assertFalse(listed and listed[0].get(ad.HTML))


class TestButtonLabel(ArrivalCase):
	def test_draft_ready_while_a_stored_draft_holds(self):
		deal, _comm = _glock_deal()
		with drafting():
			shown = api.reply_draft_ready("CRM Deal", deal.name)
			self.assertEqual((shown["ready"], shown["draft_ready"]), (True, False))
		self.assertEqual(_ahead(deal), "stored")
		with drafting():
			self.assertTrue(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"])
			frappe.db.set_value("Serial No", "_T-AD-SN-1", "sell_price", 479)
			self.assertFalse(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"])
		with drafting(ready_answer=False):
			self.assertEqual(api.reply_draft_ready("CRM Deal", deal.name)["draft_ready"], False)
		self.assertFalse(CALLS, "the label asks no provider")

	def test_the_page_script_reads_draft_ready(self):
		for doctype in ("CRM Lead", "CRM Deal"):
			self.assertIn('shown.draft_ready ? __("Draft ready") : __("Draft reply")', setup.reply_script_js(doctype))
