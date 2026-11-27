"""Customer memory on CRM (ffl_core.crm.memory): when a record is marked, what the provider is given,
what of its answer is written, and how memories of one person become one.

Real documents on the test site; the only stand-in is the provider, functions of this module registered
as the two hooks (``remembering``). Every test runs inside the savepoint CRMTestCase rolls back."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime, nowdate

from ffl_core.crm import inquiries, mail, memory, orders
from ffl_core.crm.gunbroker import MSGID_FIELD, SENDER as GB_SENDER
from ffl_core.tests.crm_support import AMMO, CRMTestCase, product_of
from ffl_core.tests.test_crm_deal_pos import _contact, _customer, _token
from ffl_core.tests.test_crm_mail import TEAM, _inbox, _receive, _settings
from ffl_core.tests.test_extension_hooks import hooks

HERE = "ffl_core.tests.test_crm_memory"
READY = [True]
ASKED: list = []
ANSWERS: list = []
MEMORY = memory.MEMORY


def ready():
	return READY[0]


def provider(context):
	ASKED.append(context)
	answer = ANSWERS.pop(0) if ANSWERS else None
	if isinstance(answer, Exception):
		raise answer
	return answer(context) if callable(answer) else answer


@contextmanager
def remembering(*answers, ready_answer=True):
	ASKED.clear()
	ANSWERS[:] = list(answers)
	READY[0] = ready_answer
	with hooks(memory.READY_HOOK, [f"{HERE}.ready"]), hooks(memory.UPDATE_HOOK, [f"{HERE}.provider"]):
		yield


@contextmanager
def no_provider():
	with hooks(memory.READY_HOOK, []), hooks(memory.UPDATE_HOOK, []):
		yield


def _ago(minutes: float):
	return add_to_date(now_datetime(), minutes=-minutes)


def _lead(**fields):
	return frappe.get_doc({"doctype": "CRM Lead", "first_name": "Sam", "last_name": "Buyer",
		"email": f"{_token()}@buyer.test", **fields}).insert(ignore_permissions=True)


def _deal(contact: str | None = None, **fields):
	doc = frappe.new_doc("CRM Deal")
	doc.status = "New"
	doc.append("contacts", {"contact": contact or _contact(f"{_token()}@buyer.test", first_name="Sam"), "is_primary": 1})
	doc.update(fields)
	doc.insert(ignore_permissions=True)
	return doc


def _msg(doc, body: str, minutes_ago: float = 10, *, sender: str = "sam@buyer.test", received: bool = True,
		subject: str = "Question") -> str:
	"""A mail already on *doc* (no mailbox: the mail rules leave it alone), dated *minutes_ago*."""
	name = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
		"communication_medium": "Email", "sent_or_received": "Received" if received else "Sent", "sender": sender,
		"recipients": TEAM, "subject": subject, "content": body, "reference_doctype": doc.doctype,
		"reference_name": doc.name}).insert(ignore_permissions=True).name
	at = _ago(minutes_ago)
	frappe.db.set_value("Communication", name, {"communication_date": at, "creation": at, "sender": sender},
		update_modified=False)
	return name


def _due(doc):
	return frappe.db.get_value(doc.doctype, doc.name, memory.DUE)


def _set(doc, **values):
	frappe.db.set_value(doc.doctype, doc.name, values, update_modified=False)


def _mem(doc):
	return memory.memory_for(frappe.get_doc(doc.doctype, doc.name))


def _rows(doc) -> list[tuple]:
	found = _mem(doc)
	return [(r.kind, r.text, r.said, int(r.locked)) for r in found.items] if found else []


def _run(doc, *answers, manual=False):
	with remembering(*answers):
		return memory.run(doc.doctype, doc.name, manual=manual)["outcome"]


def _staff_memory(doc, *rows, auto_update=1):
	"""A memory for *doc*'s person with rows staff wrote (locked) or AI wrote (unlocked)."""
	with frappe.utils.synchronization.filelock(memory.LOCK):
		found = memory.memory_for(frappe.get_doc(doc.doctype, doc.name), write=True)
	found.auto_update = auto_update
	for row in rows:
		found.append("items", {"said": "Staff" if row.get("locked") else "Customer", "noted_on": getdate(), **row})
	found.flags.ignore_permissions = True
	found.save()
	return found


class MemoryCase(CRMTestCase):
	def setUp(self):
		super().setUp()
		_inbox()
		_settings(blocked_sender_domains="spam.test", own_domains="store.test",
			team_addresses=[{"address": TEAM, "address_type": "Group"}], primary_team_address="")
		frappe.db.delete("Error Log", {"method": ["like", "%memory%"]})
		# No mail-screening extension, whatever this site has installed: mail from someone new is filed at once.
		self.enterContext(hooks(mail.SCREEN_HOOK, []))

	def tearDown(self):
		super().tearDown()
		frappe.clear_document_cache(mail.SETTINGS, mail.SETTINGS)


class TestMarking(MemoryCase):
	def test_a_customer_mail_marks_its_lead_and_deal(self):
		with remembering():
			comm = _receive("New Buyer <new-buyer@buyer.test>", "New Buyer", recipients=TEAM)
			lead = frappe.get_doc("CRM Lead", comm.reload().reference_name)
			self.assertTrue(_due(lead))
			customer_mail = _receive("known@buyer.test")   # no lead, no customer: direct mail is skipped
			self.assertFalse(customer_mail.reload().reference_name)
			# A reply in a thread already on the lead ("keep").
			_set(lead, **{memory.DUE: None})
			_receive("new-buyer@buyer.test", recipients=TEAM, reference_doctype="CRM Lead", reference_name=lead.name)
			self.assertTrue(_due(lead))

	def test_what_never_marks(self):
		with remembering():
			other = _receive("noreply@shop.test", recipients=TEAM)   # automated: other mail
			lead = frappe.get_doc("CRM Lead", other.reload().reference_name)
			self.assertEqual(lead.status, mail.OTHER_MAIL)
			self.assertFalse(_due(lead))
			real = _lead()
			# A machine answering in a thread on a real lead, and our own sent mail on it.
			_receive("no-reply@notices.test", recipients=TEAM, reference_doctype="CRM Lead", reference_name=real.name)
			_receive("staff@store.test", sent_or_received="Sent", reference_doctype="CRM Lead", reference_name=real.name)
			self.assertFalse(_due(real))
			# A blocked sender's direct mail is skipped altogether.
			_receive("x@spam.test")
		with no_provider():   # no extension: nothing is marked
			comm = _receive("Other Buyer <other-buyer@buyer.test>", recipients=TEAM)
			self.assertFalse(_due(frappe.get_doc("CRM Lead", comm.reload().reference_name)))

	def test_a_gunbroker_conversation_marks_its_lead(self):
		lead = _lead(email="", **{MSGID_FIELD: "abc12345"})
		with remembering():
			comm = _receive(GB_SENDER, "GunBroker member", recipients=TEAM, subject="Question MsgId: abc12345",
				content="--- BEGIN MESSAGE FROM USER ---\nIs the Garand still there?\n--- END MESSAGE FROM USER ---")
		self.assertEqual(comm.reload().reference_name, lead.name)
		self.assertTrue(_due(lead))

	def test_a_website_inquiry_marks_once(self):
		args = dict(kind="ask", event_id=f"evt-{_token()}", email=f"{_token()}@buyer.test", message="Garand?")
		with remembering():
			out = inquiries.file_inquiry(**args)
			doc = frappe.get_doc(out["doctype"], out["name"])
			self.assertTrue(_due(doc))
			_set(doc, **{memory.DUE: None})
			inquiries.file_inquiry(**args)   # the same event again
			self.assertFalse(_due(doc))

	def test_a_failure_never_stops_the_mail(self):
		with remembering(), patch.object(memory, "now_datetime", side_effect=RuntimeError("boom")):
			comm = _receive("Broken Buyer <broken@buyer.test>", recipients=TEAM)
		self.assertEqual(comm.reload().reference_doctype, "CRM Lead", "the mail is filed as without memory")
		self.assertFalse(_due(frappe.get_doc("CRM Lead", comm.reference_name)))
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", "%Memory update not marked%"]}))

	def test_a_converted_lead_marks_its_deal(self):
		lead = _lead()
		deal = frappe.get_doc("CRM Deal", lead.convert_to_deal())
		_set(deal, **{memory.DUE: None})
		with remembering():
			memory.mark_due("CRM Lead", lead.name)
		self.assertTrue(_due(deal))


class TestWon(MemoryCase):
	def _won(self) -> str:
		return frappe.get_all("CRM Deal Status", filters={"type": "Won"}, pluck="name", limit=1)[0]

	def test_every_way_to_win_marks_the_deal(self):
		with remembering():
			by_hand = _deal()
			by_hand.status = self._won()
			by_hand.save(ignore_permissions=True)
			self.assertEqual(frappe.db.get_value("CRM Deal", by_hand.name, memory.WON), 1)
			self.assertTrue(_due(by_hand))
			by_invoice = frappe.get_doc("CRM Deal", _deal().name)   # what an invoice made from the deal does
			orders._mark_won(by_invoice)
			orders._save(by_invoice)
			self.assertEqual(frappe.db.get_value("CRM Deal", by_invoice.name, memory.WON), 1)
			# Reopened and won again: marked again.
			_set(by_hand, **{memory.WON: 0, memory.DUE: None})
			doc = frappe.get_doc("CRM Deal", by_hand.name)
			doc.status = "Talking"
			doc.save(ignore_permissions=True)
			self.assertFalse(frappe.db.get_value("CRM Deal", by_hand.name, memory.WON))
			doc.status = self._won()
			doc.save(ignore_permissions=True)
			self.assertEqual(frappe.db.get_value("CRM Deal", by_hand.name, memory.WON), 1)

	def test_a_paid_web_order_marks_the_deal(self):
		from ffl_core.crm import web_orders

		order = frappe.new_doc(web_orders.ORDER)
		order.woo_order_id = 876543
		order.customer_email = "web@buyer.test"
		order.order_total = 50
		order.currency = "USD"
		order.flags.ignore_mandatory = True
		order.flags.ignore_validate = True
		order.db_insert()
		deal = frappe.get_doc("CRM Deal", _deal().name)
		with remembering(), patch.object(web_orders, "pos_link_on", return_value=True):
			web_orders._win(deal, frappe.get_doc(web_orders.ORDER, order.name))
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, memory.WON), 1)

	def test_the_update_is_told_what_was_bought(self):
		deal = _deal(products=[{"product_code": product_of(AMMO), "qty": 2}])
		with remembering():
			deal.status = self._won()
			deal.save(ignore_permissions=True)
		_set(deal, **{memory.DUE: _ago(4)})
		self.assertEqual(_run(deal, {"remove": [], "summary": "Bought ammo."}), "updated")
		self.assertEqual(ASKED[0]["trigger"], "won")
		self.assertEqual(ASKED[0]["bought"]["lines"], [{"name": frappe.db.get_value("Item", AMMO, "item_name"), "qty": 2}])
		self.assertNotIn("rate", str(ASKED[0]["bought"]))
		self.assertFalse(frappe.db.get_value("CRM Deal", deal.name, memory.WON))
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, memory.SUMMARY), "Bought ammo.")


class TestSweepAndRun(MemoryCase):
	def test_quiet_records_only_and_one_call_for_two_mails(self):
		lead = _lead()
		first = _msg(lead, "Looking for a Garand.", minutes_ago=2)
		second = _msg(lead, "Also have a Mosin to trade.", minutes_ago=1)
		with remembering():
			memory.mark_due("CRM Lead", lead.name)
			memory.mark_due("CRM Lead", lead.name)
			self.assertNotIn(("CRM Lead", lead.name), memory.sweep(), "a mail in the last three minutes: wait")
			_set(lead, **{memory.DUE: _ago(4)})
			self.assertIn(("CRM Lead", lead.name), memory.sweep())
		self.assertEqual(_run(lead, {"add": []}), "updated")
		self.assertEqual(len(ASKED), 1)
		self.assertEqual([(e["ref"], e["body"]) for e in ASKED[0]["new"]],
			[("E1", "Looking for a Garand."), ("E2", "Also have a Mosin to trade.")])
		self.assertFalse(_due(lead))
		self.assertEqual(get_datetime(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN)),
			get_datetime(frappe.db.get_value("Communication", second, "creation")))
		self.assertTrue(first)

	def test_a_website_inquiry_note_is_read_once(self):
		event = f"evt-{_token()}"
		out = inquiries.file_inquiry(kind="ask", event_id=event, email=f"{_token()}@buyer.test",
			message="Do you have a Garand?")
		# An inquiry filed before inquiries were filed as mail too: only its note is on the record.
		frappe.db.delete("Communication", {"message_id": event})
		doc = frappe.get_doc(out["doctype"], out["name"])
		notes = [m for m in memory._customer_messages(doc) if m.kind == "note"]
		self.assertTrue(notes, "the inquiry is on the record as a note, not a mail")
		self.assertEqual(_run(doc, {"add": []}, manual=True), "updated")
		self.assertEqual(get_datetime(frappe.db.get_value(doc.doctype, doc.name, memory.SEEN)),
			get_datetime(notes[0].creation))
		self.assertIsNone(memory.batch_for(frappe.get_doc(doc.doctype, doc.name)), "read once, not again")

	def test_a_record_never_marked_is_not_swept(self):
		_msg(_lead(), "Looking for a Garand.", minutes_ago=30)
		with remembering():
			swept = memory.sweep()
		self.assertTrue(all(frappe.db.get_value(doctype, name, memory.DUE) for doctype, name in swept), swept)

	def test_a_mail_during_the_run_is_not_lost(self):
		lead = _lead()
		_msg(lead, "Looking for a Garand.", minutes_ago=10)
		_set(lead, **{memory.DUE: _ago(5)})

		def meanwhile(context):
			_msg(lead, "Budget is flexible.", minutes_ago=0)   # a newer creation than the first
			_set(lead, **{memory.DUE: now_datetime()})          # as the mail's route marks it
			return {"add": [{"kind": "want", "text": "M1 Garand", "keywords": "Garand", "source": "E1"}]}

		self.assertEqual(_run(lead, meanwhile), "updated")
		self.assertTrue(_due(lead), "the newer mark stays for the next round")
		self.assertEqual(_run(lead, {"add": []}), "updated")
		self.assertEqual([e["body"] for e in ASKED[0]["new"]], ["Budget is flexible."])
		self.assertEqual(ASKED[0]["thread"][0]["body"], "Looking for a Garand.")
		self.assertEqual(ASKED[0]["memory"][0]["text"], "M1 Garand")

	def test_the_watermark_crosses_into_the_deal(self):
		lead = _lead()
		_msg(lead, "Looking for a Garand.", minutes_ago=30)
		self.assertEqual(_run(lead, {"add": []}), "updated")
		deal = frappe.get_doc("CRM Deal", frappe.get_doc("CRM Lead", lead.name).convert_to_deal())
		self.assertTrue(deal.get(memory.SEEN), "CRM copies the watermark onto the deal")
		self.assertEqual(_run(deal), "nothing", "the lead's mail is not read again")
		self.assertFalse(ASKED)
		_msg(deal, "Is it still there?", minutes_ago=1)
		self.assertEqual(_run(deal, {"add": []}), "updated")
		self.assertEqual([e["body"] for e in ASKED[0]["new"]], ["Is it still there?"])
		# A due left on the converted lead goes to the deal.
		_set(lead, **{memory.DUE: _ago(5)})
		_set(deal, **{memory.DUE: None})
		self.assertEqual(_run(lead), "moved")
		self.assertTrue(_due(deal))
		self.assertFalse(_due(lead))

	def test_machine_mail_and_our_own_are_not_new_messages(self):
		deal = _deal()
		_msg(deal, "We have one coming Friday.", minutes_ago=5, sender="staff@store.test", received=False)
		_msg(deal, "Your order shipped", minutes_ago=4, sender="noreply@shop.test")
		self.assertEqual(_run(deal), "nothing")
		self.assertFalse(ASKED)

	def _failing(self, doc):
		return frappe.db.get_value(doc.doctype, doc.name, memory.FAILING)

	def test_no_answer_retries_then_skips_six_hours_after_the_first_failure(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, None), "retry")
		self.assertGreater(get_datetime(_due(lead)), add_to_date(now_datetime(), minutes=55))
		first = self._failing(lead)
		self.assertTrue(first)
		self.assertEqual(_run(lead, RuntimeError("down")), "retry")
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", f"%{memory.UPDATE_HOOK} provider failed%"]}))
		self.assertEqual(get_datetime(self._failing(lead)), get_datetime(first), "counted from the first failure")
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))
		_set(lead, **{memory.FAILING: _ago(7 * 60), memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, None), "skipped")
		self.assertFalse(_due(lead))
		self.assertFalse(self._failing(lead))
		self.assertTrue(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))
		self.assertFalse(_mem(lead))

	def test_an_old_mail_gets_its_retries(self):
		# A backfilled mail weeks old: one short outage must not skip it.
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=60 * 24 * 20)
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, None), "retry")
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))
		self.assertEqual(_run(lead, {"add": []}), "updated")
		self.assertFalse(self._failing(lead), "a success clears the failure")

	def test_a_refusal_is_not_retried(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, {"refused": "Not this one."}), "skipped")
		self.assertFalse(_due(lead))
		self.assertTrue(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))

	def test_a_deferral_waits_without_starting_the_failure_clock(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		at = add_to_date(now_datetime(), hours=9)
		self.assertEqual(_run(lead, {"deferred": str(at)}), "deferred")
		self.assertEqual(get_datetime(_due(lead)), get_datetime(str(at)))
		self.assertFalse(self._failing(lead), "a deferral is not a failure")
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))
		# No time, a time gone by, or nonsense: the next site midnight; never further than two days.
		for value in (None, str(_ago(60)), "soon"):
			_set(lead, **{memory.DUE: _ago(5)})
			self.assertEqual(_run(lead, {"deferred": value}), "deferred")
			self.assertEqual(get_datetime(_due(lead)), get_datetime(add_days(nowdate(), 1)))
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, {"deferred": str(add_to_date(now_datetime(), days=30))}), "deferred")
		self.assertLessEqual(get_datetime(_due(lead)), add_to_date(now_datetime(), days=2, minutes=1))

	def test_a_deferral_after_a_failure_pauses_the_clock(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5), memory.FAILING: _ago(5 * 60)})
		self.assertEqual(_run(lead, {"deferred": str(add_to_date(now_datetime(), hours=10))}), "deferred")
		# Five hours of failing before the pause are still five hours once it is over.
		self.assertGreater(get_datetime(self._failing(lead)), add_to_date(now_datetime(), hours=4, minutes=59))
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, None), "retry")

	def test_a_manual_update_is_not_deferred(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		self.assertEqual(_run(lead, {"deferred": None}, manual=True), "no_answer")
		self.assertFalse(_due(lead))

	def test_a_win_with_no_answer_gives_up_too(self):
		deal = _deal(products=[{"product_code": product_of(AMMO), "qty": 1}])
		_set(deal, **{memory.WON: 1, memory.DUE: _ago(5)})
		self.assertEqual(_run(deal, None), "retry")
		self.assertTrue(self._failing(deal))
		_set(deal, **{memory.FAILING: _ago(7 * 60), memory.DUE: _ago(5)})
		self.assertEqual(_run(deal, None), "skipped")
		self.assertFalse(_due(deal))
		self.assertFalse(frappe.db.get_value("CRM Deal", deal.name, memory.WON))

	def test_a_failed_write_after_the_answer_is_a_retry(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		with patch.object(memory, "apply", side_effect=RuntimeError("write failed")):
			self.assertEqual(_run(lead, {"add": []}), "retry")
		self.assertTrue(frappe.db.exists("Error Log", {"method": ["like", "%Customer memory not written%"]}))
		self.assertGreater(get_datetime(_due(lead)), add_to_date(now_datetime(), minutes=55))
		self.assertTrue(self._failing(lead))
		self.assertFalse(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))
		_set(lead, **{memory.FAILING: _ago(7 * 60), memory.DUE: _ago(5)})
		with patch.object(memory, "apply", side_effect=RuntimeError("write failed")):
			self.assertEqual(_run(lead, {"add": []}), "skipped", "never rerun (and paid for) without end")
		self.assertFalse(_due(lead))

	def test_a_backlog_is_read_five_at_a_time_oldest_first(self):
		lead = _lead()
		for i in range(7):
			_msg(lead, f"Mail {i}", minutes_ago=60 - i)
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, {"add": []}), "updated")
		self.assertEqual([e["body"] for e in ASKED[0]["new"]], [f"Mail {i}" for i in range(5)])
		self.assertTrue(_due(lead), "the rest wait for the next round")
		self.assertEqual(_run(lead, {"add": []}), "updated")
		self.assertEqual([e["body"] for e in ASKED[0]["new"]], ["Mail 5", "Mail 6"])
		self.assertFalse(_due(lead))

	def test_a_locked_row_that_breaks_a_newer_rule_does_not_block_updates(self):
		lead = _lead()
		found = _staff_memory(lead, {"kind": "want", "text": "Garand", "keywords": "Garand", "locked": 1})
		# Written before the rule existed: straight to the row.
		frappe.db.set_value(found.items[0].doctype, found.items[0].name, "text", "Garand, DL TX12345678",
			update_modified=False)
		_msg(lead, "Also a Mosin.", minutes_ago=10)
		self.assertEqual(_run(lead, {"add": [{"kind": "trade_in", "text": "Mosin 91/30", "source": "E1"}]}), "updated")
		self.assertNotIn("TX12345678", frappe.as_json(ASKED[0]), "never sent to the provider")
		self.assertEqual(sorted(r[1] for r in _rows(lead)), ["Garand, DL TX12345678", "Mosin 91/30"],
			"kept as staff wrote it")

	def test_ready_is_asked_once_per_request(self):
		calls = []

		def counting():
			calls.append(1)
			raise RuntimeError("settings unreadable")

		lead = _lead()
		frappe.local.flags.pop(memory.WANTED_FLAG, None)
		with patch(f"{HERE}.ready", side_effect=counting), remembering():
			for _i in range(3):
				memory.mark_due("CRM Lead", lead.name)
		self.assertEqual(len(calls), 1)
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", f"%{memory.READY_HOOK} provider failed%"]}), 1)
		frappe.local.flags.pop(memory.WANTED_FLAG, None)

	def test_a_manual_update_reads_the_latest_and_reports_a_refusal(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		self.assertEqual(_run(lead, {"add": []}), "updated")
		with remembering({"refused": "Today's AI limit is used up."}):
			result = memory.run("CRM Lead", lead.name, manual=True)
		self.assertEqual(result, {"outcome": "no_answer", "refused": "Today's AI limit is used up."})
		self.assertEqual(ASKED[0]["trigger"], "manual")
		self.assertEqual([e["body"] for e in ASKED[0]["new"]], ["Garand?"], "read again whatever the watermark")

	def test_update_with_ai_off_skips_the_provider(self):
		lead = _lead()
		_staff_memory(lead, auto_update=0)
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		self.assertEqual(_run(lead, {"add": []}), "paused")
		self.assertFalse(ASKED)
		self.assertFalse(_due(lead))
		self.assertTrue(frappe.db.get_value("CRM Lead", lead.name, memory.SEEN))

	def test_an_edit_while_the_provider_works_discards_the_answer(self):
		lead = _lead()
		_staff_memory(lead, {"kind": "want", "text": "Garand", "keywords": "Garand"})
		_msg(lead, "Also a Mosin.", minutes_ago=10)
		due0 = _ago(5)
		_set(lead, **{memory.DUE: due0})

		def staff_edits(context):
			found = _mem(lead)
			found.auto_update = 1
			found.items[0].text = "Garand, shooting grade"
			found.items[0].locked = 1
			found.flags.ignore_permissions = True
			found.save()
			return {"remove": [context["memory"][0]["ref"]]}

		self.assertEqual(_run(lead, staff_edits), "discarded")
		self.assertEqual(get_datetime(_due(lead)), get_datetime(due0), "kept for the next round")
		self.assertEqual(_rows(lead), [("want", "Garand, shooting grade", "Customer", 1)])
		self.assertEqual(_run(lead, {"add": [{"kind": "trade_in", "text": "Mosin 91/30", "source": "E1"}]}), "updated")
		self.assertIn(("trade_in", "Mosin 91/30", "Customer", 0), _rows(lead))

	def test_without_a_provider_nothing_runs(self):
		lead = _lead()
		_msg(lead, "Garand?", minutes_ago=30)
		_set(lead, **{memory.DUE: _ago(5)})
		with no_provider():
			self.assertEqual(memory.sweep(), [])
			self.assertEqual(memory.run("CRM Lead", lead.name)["outcome"], "off")
			memory.mark_due("CRM Deal", _deal().name, won=True)
		self.assertFalse(_due(lead))
		self.assertFalse(_mem(lead))


class TestFilters(MemoryCase):
	def test_numbers_and_eligibility_never_reach_the_provider(self):
		deal = _deal()
		_msg(deal, "Looking for a Garand. My DL is TX12345678, DOB 1/2/1980, SSN 123-45-6789. "
			"Card 4111 1111 1111 1111. NTN 100ABC123 came back. I was convicted of something years ago.",
			minutes_ago=5, subject="SSN 987-65-4321")
		_msg(deal, "Earlier: my passport P12345678 is attached.", minutes_ago=60)
		_set(deal, **{memory.SEEN: _ago(30)})   # the passport mail is old: thread only
		self.assertEqual(_run(deal, {"add": []}), "updated")
		sent = frappe.as_json(ASKED[0])
		for value in ("TX12345678", "1/2/1980", "123-45-6789", "4111", "100ABC123", "convicted", "987-65-4321",
				"P12345678"):
			self.assertNotIn(value, sent)
		self.assertIn("[removed]", ASKED[0]["new"][0]["body"])
		self.assertIn("Looking for a Garand.", ASKED[0]["new"][0]["body"])
		self.assertNotIn("sam@buyer.test", sent)

	def test_bad_rows_and_a_bad_summary_are_not_written(self):
		lead = _lead()
		_msg(lead, "Hi", minutes_ago=5)
		_set(lead, **{memory.SUMMARY: "Wants a Garand."})
		self.assertEqual(_run(lead, {"add": [
			{"kind": "want", "text": "Garand, DL TX12345678", "keywords": "Garand", "source": "E1"},
			{"kind": "want", "text": "Glock 19 in stock", "keywords": "Glock 19", "source": "E1"},
			{"kind": "preference", "text": "Pays $500 cash", "source": "E1"},
			{"kind": "question", "text": "Can you ship to CA?", "source": "E1"},
			{"kind": "question", "text": "Does it come with a sling?", "source": "E1"},
		], "summary": "Glock 19 is $499."}), "updated")
		self.assertEqual(_rows(lead), [("question", "Does it come with a sling?", "Customer", 0)])
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, memory.SUMMARY), "Wants a Garand.")

	def test_a_staff_save_with_an_id_number_is_refused(self):
		found = _staff_memory(_lead())
		found.append("items", {"kind": "preference", "text": "DL TX12345678", "said": "Staff", "locked": 1})
		with self.assertRaises(frappe.ValidationError):
			found.save()
		found.reload()
		found.append("items", {"kind": "want", "text": "Garand", "keywords": "Garand", "said": "Staff", "locked": 1})
		found.append("items", {"kind": "budget", "amount": 1200, "said": "Staff", "locked": 1})
		found.save()


class TestApply(MemoryCase):
	def _lead_with(self, *rows, mails=("Hi",)):
		lead = _lead()
		if rows:
			_staff_memory(lead, *rows)
		for i, body in enumerate(mails):
			_msg(lead, body, minutes_ago=10 - i)
		return lead

	def test_locked_rows_stay_and_unknown_refs_and_sources_are_ignored(self):
		lead = self._lead_with({"kind": "want", "text": "Garand", "keywords": "Garand", "locked": 1},
			{"kind": "want", "text": "Mosin", "keywords": "Mosin"})
		self.assertEqual(_run(lead, lambda c: {
			"remove": [c["memory"][0]["ref"], "M9"],
			"update": [{"ref": c["memory"][0]["ref"], "text": "Garand CMP", "source": "E1"},
				{"ref": c["memory"][1]["ref"], "text": "Mosin 91/30", "keywords": "Mosin 91/30", "source": "M1"}],
			"add": [{"kind": "want", "text": "SKS", "keywords": "SKS", "source": "Comm-123"}]}), "updated")
		self.assertEqual(_rows(lead), [("want", "Garand", "Staff", 1), ("want", "Mosin", "Customer", 0)])

	def test_an_ai_row_links_its_message(self):
		lead = self._lead_with({"kind": "want", "text": "Mosin", "keywords": "Mosin"}, mails=("Old", "Garand please"))
		comm = frappe.get_all("Communication", filters={"reference_name": lead.name}, pluck="name",
			order_by="creation desc", limit=1)[0]
		_run(lead, lambda c: {"update": [{"ref": c["memory"][0]["ref"], "text": "Mosin 91/30", "keywords": None,
			"source": "E2"}], "add": [{"kind": "want", "text": "M1 Garand", "keywords": "Garand", "source": "E2"}]})
		rows = {r.text: r for r in _mem(lead).items}
		self.assertEqual(rows["Mosin 91/30"].keywords, "Mosin", "a null field keeps the row's value")
		for row in rows.values():
			self.assertEqual((row.said, row.source_doctype, row.source_name, int(row.locked)),
				("Customer", "Communication", comm, 0))

	def test_kinds_states_budgets_and_text(self):
		lead = self._lead_with()
		_run(lead, {"add": [
			{"kind": "state", "text": "texas", "source": "E1"},
			{"kind": "state", "text": "Narnia", "source": "E1"},
			{"kind": "budget", "text": "", "amount": 1500, "source": "E1"},
			{"kind": "budget", "text": "", "amount": 0, "source": "E1"},
			{"kind": "want", "text": "Something", "keywords": "!!", "source": "E1"},
			{"kind": "favorite", "text": "x", "source": "E1"},
			{"kind": "preference", "text": "x" * 300, "source": "E1"},
		]})
		rows = {r.kind: r for r in _mem(lead).items}
		self.assertEqual(sorted(rows), ["budget", "preference", "state"])
		self.assertEqual(rows["state"].text, "TX")
		self.assertEqual(rows["budget"].amount, 1500)
		self.assertEqual(len(rows["preference"].text), memory.TEXT_LIMIT)

	def test_one_budget_and_a_locked_one_wins(self):
		lead = self._lead_with({"kind": "budget", "amount": 1000}, mails=("a", "b"))
		_run(lead, {"add": [{"kind": "budget", "amount": 1500, "source": "E1"}]})
		self.assertEqual([r.amount for r in _mem(lead).items], [1500])
		locked = self._lead_with({"kind": "budget", "amount": 1200, "locked": 1})
		_run(locked, {"add": [{"kind": "budget", "amount": 2000, "source": "E1"}]})
		self.assertEqual([(r.amount, int(r.locked)) for r in _mem(locked).items], [(1200, 1)])

	def test_the_same_thing_twice_is_one_row(self):
		lead = self._lead_with({"kind": "want", "text": "Glock 19", "keywords": "Glock 19"})
		_run(lead, {"add": [{"kind": "want", "text": "glock  19", "keywords": "Glock 19", "source": "E1"}]})
		rows = _mem(lead).items
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].source_doctype, "Communication", "the source and date refreshed")

	def test_limits_push_out_the_oldest_unlocked(self):
		rows = [{"kind": "want", "text": f"Gun {i}", "keywords": f"Gun{i}", "noted_on": add_to_date(getdate(), days=-10 + i)}
			for i in range(5)] + [{"kind": "want", "text": "Kept", "keywords": "Kept", "locked": 1,
			"noted_on": add_to_date(getdate(), days=-30)}]
		lead = self._lead_with(*rows)
		_run(lead, {"add": [{"kind": "want", "text": "New gun", "keywords": "New", "source": "E1"}]})
		texts = [r.text for r in _mem(lead).items]
		self.assertEqual(len(texts), memory.LIMITS["want"])
		self.assertNotIn("Gun 0", texts)
		self.assertIn("Kept", texts)
		self.assertIn("New gun", texts)


class TestOnePersonOneMemory(MemoryCase):
	def test_a_converted_lead_takes_its_memory_to_the_deal(self):
		lead = _lead()
		_staff_memory(lead, {"kind": "want", "text": "Garand", "keywords": "Garand"})
		deal = frappe.get_doc("CRM Deal", lead.convert_to_deal())
		found = _mem(deal)
		self.assertEqual(found.lead, lead.name)
		self.assertEqual(found.contact, deal.contacts[0].contact)
		self.assertEqual(frappe.db.count(MEMORY, {"lead": lead.name}), 1)

	def test_a_contacts_memory_is_merged_in(self):
		contact = _contact(f"{_token()}@buyer.test", first_name="Pat")
		earlier = _deal(contact)
		_staff_memory(earlier, {"kind": "budget", "amount": 900, "locked": 1},
			{"kind": "want", "text": "Garand", "keywords": "Garand", "noted_on": add_to_date(getdate(), days=-5)},
			auto_update=0)
		lead = _lead()
		_staff_memory(lead, {"kind": "budget", "amount": 1500}, {"kind": "want", "text": "garand", "keywords": "Garand"},
			{"kind": "state", "text": "TX"})
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		deal = frappe.get_doc("CRM Deal", convert_to_deal(lead=lead.name, existing_contact=contact))
		self.assertEqual(frappe.db.count(MEMORY, {"contact": contact}), 1)
		found = _mem(deal)
		self.assertEqual(sorted((r.kind, r.text or "", r.amount or 0) for r in found.items),
			[("budget", "", 900), ("state", "TX", 0), ("want", "garand", 0)])
		self.assertEqual(found.auto_update, 0, "off if either was off")
		self.assertEqual((found.contact, found.lead), (contact, lead.name))

	def test_merged_contacts_and_customers_leave_one(self):
		a, b = _contact(f"{_token()}@buyer.test"), _contact(f"{_token()}@buyer.test")
		_staff_memory(_deal(a), {"kind": "want", "text": "Garand", "keywords": "Garand"})
		_staff_memory(_deal(b), {"kind": "want", "text": "Mosin", "keywords": "Mosin"})
		frappe.rename_doc("Contact", a, b, merge=True, force=True)
		self.assertEqual(frappe.db.count(MEMORY, {"contact": b}), 1)
		self.assertEqual(sorted(r.text for r in frappe.get_doc(MEMORY, {"contact": b}).items), ["Garand", "Mosin"])

		from ffl_core.customer_profile import merge_customer

		keep, gone = _customer(), _customer()
		for customer, text in ((keep, "SKS"), (gone, "AK")):
			frappe.get_doc({"doctype": MEMORY, "customer": customer, "items": [{"kind": "want", "text": text,
				"keywords": text, "said": "Customer", "noted_on": getdate()}]}).insert(ignore_permissions=True)
		merge_customer(gone, keep)
		self.assertEqual(frappe.db.count(MEMORY, {"customer": keep}), 1)
		self.assertEqual(sorted(r.text for r in frappe.get_doc(MEMORY, {"customer": keep}).items), ["AK", "SKS"])

	def test_deleting_a_key_record(self):
		alone = _lead()
		_staff_memory(alone, {"kind": "want", "text": "Garand", "keywords": "Garand"})
		frappe.delete_doc("CRM Lead", alone.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(MEMORY, {"lead": alone.name}))
		contact = _contact(f"{_token()}@buyer.test")
		found = frappe.get_doc({"doctype": MEMORY, "contact": contact, "lead": _lead().name}).insert(ignore_permissions=True)
		frappe.delete_doc("CRM Lead", found.lead, ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(MEMORY, found.name, ["contact", "lead"]), (contact, None))
		customer = _customer()
		frappe.get_doc({"doctype": MEMORY, "customer": customer}).insert(ignore_permissions=True)
		frappe.delete_doc("Customer", customer, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(MEMORY, {"customer": customer}))

	def test_the_write_path_merges_and_fills_keys(self):
		contact = _contact(f"{_token()}@buyer.test")
		deal = _deal(contact)
		by_contact = frappe.get_doc({"doctype": MEMORY, "contact": contact}).insert(ignore_permissions=True)
		customer = _customer()
		by_customer = frappe.get_doc({"doctype": MEMORY, "customer": customer}).insert(ignore_permissions=True)
		_set(deal, erpnext_customer=customer)
		deal = frappe.get_doc("CRM Deal", deal.name)
		self.assertEqual(memory.memory_for(deal).name, by_customer.name, "read: by customer first")
		self.assertEqual(frappe.db.count(MEMORY, {"name": ["in", [by_contact.name, by_customer.name]]}), 2,
			"reading never merges")
		found = memory.memory_for(deal, write=True)
		self.assertEqual((found.name, found.customer, found.contact), (by_customer.name, customer, contact))
		self.assertFalse(frappe.db.exists(MEMORY, by_contact.name))


class TestBackfill(MemoryCase):
	def test_recent_open_records_are_marked_once(self):
		frappe.db.set_default(memory.BACKFILLED, "")
		fresh, old, read = _lead(), _lead(), _lead()
		_msg(fresh, "Garand?", minutes_ago=60)
		_msg(old, "Garand?", minutes_ago=60 * 24 * 40)
		_msg(read, "Garand?", minutes_ago=60)
		_set(read, **{memory.SEEN: _ago(30)})
		self.assertGreaterEqual(memory.queue_recent(), 1)
		self.assertTrue(_due(fresh))
		self.assertFalse(_due(old))
		self.assertFalse(_due(read))
		self.assertEqual(memory.queue_recent(), 0, "once per site")
