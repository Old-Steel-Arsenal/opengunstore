"""GunBroker member questions in the team mailbox (ffl_core.crm.gunbroker, ffl_core.crm.mail): one lead per
conversation, the gun it is about, and the other mail from that address left where it went. Mail is pulled
through Frappe's own receive path (see test_crm_team_mail)."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from ffl_core.crm import gunbroker, mail
from ffl_core.crm.deal import add_product_row
from ffl_core.tests.crm_support import PER_GUN, product_of
from ffl_core.tests.test_crm_deal import _gun
from ffl_core.tests.test_crm_other_mail import ASKED, HERE, _answer, _lead
from ffl_core.tests.test_crm_team_mail import (ADDRESS, INFO, SALES, STAFF_A, TeamMailCase, _assignees,
	_customer_contact, _pull, _raw as _team_raw, _settings, _stored)
from ffl_core.tests.test_extension_hooks import hooks

GB = gunbroker.SENDER
_uid = [200]


def _body(question: str, trailer: str) -> str:
	return f"""-- BEGIN MESSAGE FROM USER ---
{question}
-- END MESSAGE FROM USER ---

{trailer}
"""


def _trailer(listing: str = "1219067857", sku: str = "", sep: str = ": ") -> str:
	lines = [f"Item Title{sep}Winchester Model 70", f"Item Number{sep}{listing}"]
	if sku:
		lines.append(f"SKU{sep}{sku}")
	return "\n".join(lines)


def _raw(msgid: str | None = "H1gweZbRzl", body: str | None = None, name: str = "jsmith42", re: str = "",
		subject: str | None = None) -> bytes:
	_uid[0] += 1
	msg = EmailMessage()
	msg["From"] = f"{name} <{GB}>" if name else GB
	msg["To"] = SALES
	tail = f"  (Do not remove MsgId: {msgid})" if msgid else ""
	msg["Subject"] = subject or f"{re}Question regarding GunBroker.com  Item #1219067857{tail}"
	msg["Date"] = format_datetime(now_datetime().astimezone())
	msg["Message-ID"] = f"<ffl-gb-{frappe.generate_hash(length=12)}@example.org>"
	msg.set_content(body or _body("Would you take $500?", _trailer()))
	return msg.as_bytes()


def _file(raw: bytes) -> tuple[str, str]:
	_pull(INFO, (raw, _uid[0]))
	[kept] = _stored(raw)
	return kept.reference_doctype, kept.reference_name


def _listed(serial: str, listing: str) -> str:
	"""A body about *listing*, which is a gun of the store."""
	frappe.db.set_value("Serial No", _gun(serial, 899), "gb_item_id", listing)
	return _body("Hi", _trailer(listing=listing))


def _rows(doctype: str, name: str) -> list[tuple]:
	return [(r.product_code, r.qty, r.get("ffl_serial_no")) for r in frappe.get_doc(doctype, name).products]


class TestGunBrokerConversation(TeamMailCase):
	def setUp(self):
		super().setUp()
		mail_settings = {"category_rules": [{"domain": "gunbroker.com", "category": "GunBroker"}]}
		from ffl_core.tests.test_crm_team_mail import _settings

		_settings(**mail_settings)

	def test_a_new_conversation_is_a_lead_named_after_the_member_with_the_gun(self):
		serial = _gun("_T-CRM-GB-1", 899)
		doctype, name = _file(_raw(body=_body("Still available?", _trailer(sku=f"{PER_GUN}::{serial}"))))
		self.assertEqual(doctype, "CRM Lead")
		lead = frappe.get_doc("CRM Lead", name)
		self.assertEqual((lead.first_name, lead.source, lead.status, lead.lead_owner, lead.get(gunbroker.MSGID_FIELD)),
			("jsmith42", "GunBroker", "New", None, "H1gweZbRzl"))
		self.assertFalse(lead.email)
		self.assertFalse(lead.get(mail.CATEGORY_FIELD))
		self.assertTrue(_assignees("CRM Lead", name), "shared with the team")
		self.assertEqual(_rows("CRM Lead", name), [(product_of(PER_GUN), 1, serial)])
		self.assertNoPullErrors()

	def test_the_listing_number_finds_the_gun_when_there_is_no_sku(self):
		serial = _gun("_T-CRM-GB-2", 899)
		frappe.db.set_value("Serial No", serial, "gb_item_id", "1219067857")
		_, name = _file(_raw(body=_body("Hi", _trailer(sep=" | "))))
		self.assertEqual(_rows("CRM Lead", name), [(product_of(PER_GUN), 1, serial)])

	def test_the_member_cannot_name_the_gun_in_their_own_words(self):
		serial = _gun("_T-CRM-GB-3", 899)
		_, name = _file(_raw(body=_body(f"SKU: {PER_GUN}::{serial}\nItem Number: 1219067857", _trailer(listing="1"))))
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "status"), mail.OTHER_MAIL)

	def test_later_mail_of_the_conversation_goes_to_the_same_lead(self):
		first = _raw(msgid="B16BykWCfg", re="", body=_listed("_T-CRM-GB-4", "1300000004"))
		_, lead = _file(first)
		again = _raw(msgid="B16BykWCfg", re="Re: ", name="John Smith", subject="Re: Question regarding GunBroker.com  Order #32000014  (Do not remove\tMsgId: B16BykWCfg)")
		self.assertEqual(_file(again), ("CRM Lead", lead))
		self.assertEqual(frappe.db.count("CRM Lead", {gunbroker.MSGID_FIELD: "B16BykWCfg"}), 1)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "first_name"), "jsmith42", "the first name stays")
		_, other = _file(_raw(msgid="C99other999", name="someoneelse", body=_listed("_T-CRM-GB-5", "1300000005")))
		self.assertNotEqual(other, lead, "another member's conversation")

	def test_a_converted_conversation_goes_to_its_deal(self):
		_, lead = _file(_raw(msgid="D11converted", body=_listed("_T-CRM-GB-6", "1300000006")))
		deal = frappe.get_doc("CRM Lead", lead).convert_to_deal()
		self.assertEqual(_file(_raw(msgid="D11converted")), ("CRM Deal", deal))

	def test_a_mail_without_a_msgid_is_other_mail_as_before(self):
		for msgid in (None, ""):
			raw = _raw(msgid=msgid, subject="Welcome to GunBroker.com") if msgid is None else _raw(msgid="x")
		raw = _raw(subject="Account notice from GunBroker.com")
		doctype, name = _file(raw)
		lead = _lead(GB)
		self.assertEqual((doctype, name, lead.status, lead.get(mail.CATEGORY_FIELD)), ("CRM Lead", lead.name, mail.OTHER_MAIL, "GunBroker"))
		_, gb = _file(_raw(msgid="E22conversat", body=_listed("_T-CRM-GB-7", "1300000007")))
		self.assertNotEqual(gb, lead.name)
		# The address's own lookups still find the other mail lead, not a conversation.
		doctype, again = _file(_raw(subject="Another notice from GunBroker.com"))
		self.assertEqual(again, lead.name)

	def test_a_listing_that_is_not_ours_is_other_mail(self):
		"""The store buys on GunBroker too: a seller's answer about their listing is not a lead."""
		_, name = _file(_raw(msgid="F22notours", body=_body("Hi", _trailer(listing="999", sku="NO-SUCH::SN"))))
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "status"), mail.OTHER_MAIL)
		self.assertNoPullErrors()
		_, again = _file(_raw(msgid="F33noanswer", body="no markers at all"))
		self.assertEqual(again, name)
		self.assertFalse(frappe.db.exists("CRM Lead", {gunbroker.MSGID_FIELD: ["in", ["F22notours", "F33noanswer"]]}))

	def test_a_failing_listing_lookup_is_logged_and_the_mail_is_other_mail(self):
		errors = frappe.db.count("Error Log")
		with patch("ffl_integrations.gunbroker.webhooks.resolve_listing", side_effect=RuntimeError("boom")):
			_, name = _file(_raw(msgid="G44lookupfail"))
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "status"), mail.OTHER_MAIL)
		self.assertGreater(frappe.db.count("Error Log"), errors)

	def test_the_facts_of_a_subject_and_body_in_both_layouts(self):
		subject = "Re: Question regarding GunBroker.com  Item #1219067857  (Do not remove\n MsgId: H1gweZbRzl)"
		for sep in (": ", " | "):
			facts = gunbroker.conversation(GB, subject, "<pre>" + _body("q", _trailer(sku="AB-1::SN1", sep=sep)).replace("\n", "<br>") + "</pre>")
			self.assertEqual(facts, {"msgid": "H1gweZbRzl", "listing": "1219067857", "sku": "AB-1::SN1"}, sep)
		self.assertIsNone(gunbroker.conversation("someone@example.org", subject, ""))
		self.assertIsNone(gunbroker.conversation(GB, "Welcome", ""))


class TestMailWithoutTo(TeamMailCase):
	"""GunBroker's member mail reaches the team mailbox with no To the POS keeps; CRM's Reply needs one."""

	def setUp(self):
		super().setUp()
		from ffl_core.tests.test_crm_team_mail import _settings

		_settings(category_rules=[{"domain": "gunbroker.com", "category": "GunBroker"}])

	def _no_to(self, raw: bytes) -> bytes:
		from email import message_from_bytes
		from email.policy import default

		msg = message_from_bytes(raw, policy=default)
		del msg["To"]
		return msg.as_bytes()

	def test_a_received_mail_without_to_gets_its_mailbox_address(self):
		from ffl_core.tests.test_crm_team_mail import ADDRESS

		raw = self._no_to(_raw(msgid="NoToMsg01"))
		doctype, name = _file(raw)
		self.assertEqual(doctype, "CRM Lead", "filed as a GunBroker conversation, as with a To")
		[kept] = _stored(raw)
		self.assertEqual(frappe.db.get_value("Communication", kept.name, "recipients"), ADDRESS[INFO])
		# A mail with a To keeps it.
		raw = _raw(msgid="WithToMsg01")
		_file(raw)
		self.assertEqual(frappe.db.get_value("Communication", _stored(raw)[0].name, "recipients"), SALES)

	def test_the_patch_fills_old_mail_once(self):
		from ffl_core.patches import fill_received_mail_recipients
		from ffl_core.tests.test_crm_team_mail import ADDRESS

		raw = _raw(msgid="OldMsg01")
		_file(raw)
		received = _stored(raw)[0].name
		sent = frappe.get_doc({"doctype": "Communication", "communication_medium": "Email", "sent_or_received": "Sent",
			"subject": "Out", "email_account": INFO, "recipients": "x@example.org"}).insert(ignore_permissions=True).name
		frappe.db.set_value("Communication", received, "recipients", None, update_modified=False)
		frappe.db.set_value("Communication", sent, "recipients", "", update_modified=False)
		modified = frappe.db.get_value("Communication", received, "modified")
		fill_received_mail_recipients.execute()
		self.assertEqual(frappe.db.get_value("Communication", received, ["recipients", "modified"]),
			(ADDRESS[INFO], modified))
		self.assertEqual(frappe.db.get_value("Communication", sent, "recipients"), "", "sent mail is left alone")
		frappe.db.set_value("Communication", received, "recipients", SALES, update_modified=False)
		fill_received_mail_recipients.execute()
		self.assertEqual(frappe.db.get_value("Communication", received, "recipients"), SALES, "a mail with recipients stays")


# GunBroker's own notices --------------------------------------------------------------------------

BUYER, SELLER = "gb-buyer@example.org", "gb-seller@example.org"
SOLD = "Congratulations! Your item has sold Winchester Model 70 (1219067857)"
WON = "Congratulations! You won this item Colt Python (1300000099) on GunBroker.com!"
TRANSFERS = ("New Inbound FFL Transfer: GunBroker Order 32000001", "FFL Transfer Documents Ready: GunBroker Order 32000002",
	"FFL Transfer Complete: GunBroker Order 32000003")


def _notice(subject: str, reply_to: str, name: str = "GunBroker.com", to: str | None = None) -> bytes:
	"""A notice as GunBroker sends it: from GunBroker, with the other party in Reply-To."""
	_uid[0] += 1
	msg = EmailMessage()
	msg["From"] = f"{name} <noreply@gunbroker.com>"
	msg["Reply-To"] = reply_to
	msg["To"] = to or SALES
	msg["Subject"] = subject
	msg["Date"] = format_datetime(now_datetime().astimezone())
	msg["Message-ID"] = f"<ffl-gbn-{frappe.generate_hash(length=12)}@example.org>"
	msg.set_content("Item sold. Contact the other party to arrange payment and shipping.")
	return msg.as_bytes()


def _person(sender: str, subject: str = "Payment sent", name: str = "Pat Person") -> bytes:
	_uid[0] += 1
	msg = EmailMessage()
	msg["From"] = f"{name} <{sender}>"
	msg["To"] = SALES
	msg["Subject"] = subject
	msg["Date"] = format_datetime(now_datetime().astimezone())
	msg["Message-ID"] = f"<ffl-gbp-{frappe.generate_hash(length=12)}@example.org>"
	msg.set_content("Sent the money today, please ship to my FFL.")
	return msg.as_bytes()


def _comm(sender: str, subject: str, name: str, lead: str | None = None) -> str:
	"""A received mail as it was filed before notices were told apart (no account: no rules run on it)."""
	return frappe.get_doc({"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email",
		"sent_or_received": "Received", "sender": sender, "sender_full_name": name, "subject": subject, "content": "x",
		"reference_doctype": "CRM Lead" if lead else None, "reference_name": lead}).insert(ignore_permissions=True).name


def _legacy_notice_lead(email: str, subject: str) -> str:
	"""An Other mail lead a notice made, as the AI screening filed it before this change."""
	lead = mail._new_lead(email, None, "Notification")
	_comm(email, subject, "GunBroker.com", lead)
	return lead


def _leads(email: str) -> list[dict]:
	return frappe.get_all("CRM Lead", filters={"email": email}, fields=["name", "status", mail.CATEGORY_FIELD])


class TestGunBrokerNotices(TeamMailCase):
	def setUp(self):
		super().setUp()
		_answer(None)

	def _screened(self, raw: bytes, answer: str | None = None) -> dict:
		"""Pull *raw* with the AI screening installed (answering *answer*), run its job; the stored mail."""
		_answer(answer)
		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue"):
			_pull(INFO, (raw, _uid[0]))
			[kept] = _stored(raw)
			if not kept.reference_name:
				mail.screen_new_sender(kept.name)
		return _stored(raw)[0]

	def assertKeptUnderNoOne(self, raw: bytes, kind: str):
		[kept] = _stored(raw)
		self.assertFalse(kept.reference_doctype, "not filed under the Reply-To address")
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Communication", "reference_name": kept.name,
			"comment_type": "Info", "content": ["like", f"{mail.GB_NOTICE} ({kind})%"]}))

	def test_sold_won_and_transfer_notices_are_kept_under_no_one(self):
		cases = [(SOLD, BUYER, "sold"), (WON, SELLER, "won")] + [(s, "gb-ffl@example.org", "transfer") for s in TRANSFERS]
		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue") as enqueue:
			for subject, reply_to, kind in cases:
				raw = _notice(subject, reply_to)
				_pull(INFO, (raw, _uid[0]))
				self.assertKeptUnderNoOne(raw, kind)
				self.assertFalse(_leads(reply_to), subject)
			enqueue.assert_not_called()
		self.assertFalse(ASKED, "no screening asked")
		self.assertNoPullErrors()

	def test_gunbrokers_name_over_someone_elses_address_is_a_notice(self):
		raw = _notice("Your payment was received", BUYER, name="GunBroker.com Support")
		_pull(INFO, (raw, _uid[0]))
		self.assertKeptUnderNoOne(raw, "notice")
		self.assertFalse(_leads(BUYER))

	def test_a_notice_to_a_staff_mailbox_is_left_out(self):
		raw = _notice(SOLD, BUYER, to=ADDRESS[STAFF_A])
		_pull(STAFF_A, (raw, _uid[0]))
		self.assertFalse(_stored(raw))
		self.assertNoPullErrors()

	def test_a_notice_does_not_go_on_a_customers_deal(self):
		_customer_contact(BUYER)
		raw = _notice(SOLD, BUYER)
		_pull(INFO, (raw, _uid[0]))
		self.assertKeptUnderNoOne(raw, "sold")

	def test_the_buyers_own_mail_after_a_sold_notice_is_judged_as_anyones(self):
		_pull(INFO, (_notice(SOLD, BUYER), _uid[0]))
		kept = self._screened(_person(BUYER), answer=None)
		[lead] = _leads(BUYER)
		self.assertEqual((kept.reference_doctype, kept.reference_name), ("CRM Lead", lead.name))
		self.assertEqual((lead.status, lead.get(mail.CATEGORY_FIELD)), ("New", None))
		self.assertTrue(_assignees("CRM Lead", lead.name), "shared with the team, as any new lead")
		# The AI screening still has its say: a buyer's "not a customer" verdict is other mail of its category.
		_pull(INFO, (_notice(SOLD, "gb-buyer2@example.org"), _uid[0]))
		self._screened(_person("gb-buyer2@example.org"), answer="notification")
		self.assertEqual(_leads("gb-buyer2@example.org")[0].status, mail.OTHER_MAIL)

	def test_a_buyer_answering_our_reply_to_the_notice_is_a_person(self):
		self.assertIsNone(gunbroker.notice(BUYER, "Pat Person", "Re: " + SOLD))
		self.assertIsNone(gunbroker.notice(BUYER, "GunBroker.com", "Question regarding GunBroker.com Item #1 (Do not remove MsgId: H1gweZbRzl)"))
		self.assertIsNone(gunbroker.notice("noreply@gunbroker.com", "GunBroker.com", SOLD), "GunBroker's own address")
		self.assertEqual(gunbroker.notice(BUYER, None, "congratulations!  your item has\nsold X"), "sold")

	def test_the_sellers_mail_after_a_won_notice_is_vendor_mail_not_a_lead(self):
		_pull(INFO, (_notice(WON, SELLER), _uid[0]))
		kept = self._screened(_person(SELLER, "Tracking number"), answer="customer")
		[lead] = _leads(SELLER)
		self.assertEqual((kept.reference_name, lead.status, lead.get(mail.CATEGORY_FIELD)),
			(lead.name, mail.OTHER_MAIL, gunbroker.SELLER_CATEGORY))
		self.assertFalse(ASKED, "a seller is known without asking")
		self.assertFalse(_assignees("CRM Lead", lead.name), "other mail is not shared")

	def test_a_lead_a_notice_made_earlier_does_not_keep_the_buyers_mail(self):
		lead = _legacy_notice_lead(BUYER, SOLD)
		raw = _person(BUYER)
		_answer("customer")
		with hooks(mail.SCREEN_HOOK, [f"{HERE}.provider"]), patch("frappe.enqueue"):
			_pull(INFO, (raw, _uid[0]))
			self.assertFalse(_stored(raw)[0].reference_name, "waits for the screening, as someone new's mail")
			mail.screen_new_sender(_stored(raw)[0].name)
		self.assertEqual(_stored(raw)[0].reference_name, lead, "the same lead, not a second one")
		self.assertEqual([(lead, "New", None)], [(r.name, r.status, r.get(mail.CATEGORY_FIELD)) for r in _leads(BUYER)])
		self.assertTrue(_assignees("CRM Lead", lead))
		# From now on it is the buyer's own lead: their next mail goes straight there.
		again = _person(BUYER, "Shipped?")
		_pull(INFO, (again, _uid[0]))
		self.assertEqual(_stored(again)[0].reference_name, lead)

	def test_a_lead_a_won_notice_made_earlier_becomes_vendor_mail(self):
		lead = _legacy_notice_lead(SELLER, WON)
		raw = _person(SELLER, "Invoice")
		_pull(INFO, (raw, _uid[0]))
		self.assertEqual(_stored(raw)[0].reference_name, lead)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, mail.CATEGORY_FIELD), gunbroker.SELLER_CATEGORY)

	def test_an_answer_in_a_thread_on_such_a_lead_makes_it_a_lead(self):
		lead = _legacy_notice_lead(BUYER, SOLD)
		sent = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
			"communication_medium": "Email", "sent_or_received": "Sent", "subject": "Re: " + SOLD,
			"sender": SALES, "recipients": BUYER, "reference_doctype": "CRM Lead", "reference_name": lead,
			"message_id": f"ffl-gbt-{frappe.generate_hash(length=8)}@example.com"}).insert(ignore_permissions=True)
		reply = _team_raw(BUYER, SALES, in_reply_to=sent.message_id)
		_pull(INFO, (reply, _uid[0]))
		self.assertEqual(_stored(reply)[0].reference_name, lead)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "status"), "New")

	def test_a_lead_with_a_persons_mail_keeps_its_sender(self):
		"""Other mail that is not only notices is left as the store filed it."""
		lead = _legacy_notice_lead(BUYER, SOLD)
		_comm(BUYER, "Hello", "Pat Person", lead)
		raw = _person(BUYER)
		_pull(INFO, (raw, _uid[0]))
		self.assertEqual(_stored(raw)[0].reference_name, lead)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "status"), mail.OTHER_MAIL)


class TestGunBrokerMemberAsksAgain(TeamMailCase):
	def setUp(self):
		super().setUp()
		_settings(category_rules=[{"domain": "gunbroker.com", "category": "GunBroker"}])

	def test_three_questions_of_one_member_are_one_lead(self):
		first = _listed("_T-CRM-GBR-1", "1400000001")
		bodies = [first, _listed("_T-CRM-GBR-2", "1400000002"), first]
		places = {_file(_raw(msgid=f"R{i}repeatask", name="dupmember", body=body)) for i, body in enumerate(bodies)}
		[(doctype, lead)] = places
		self.assertEqual(doctype, "CRM Lead")
		self.assertEqual(frappe.db.count("CRM Lead", {"lead_name": "dupmember"}), 1)
		self.assertEqual(len(_rows("CRM Lead", lead)), 2, "each gun once")
		# A later mail of the second conversation finds it by its MsgId; another member gets their own.
		self.assertEqual(_file(_raw(msgid="R1repeatask", re="Re: ", name="Somebody Else")), ("CRM Lead", lead))
		_, other = _file(_raw(msgid="R9another", name="othermember", body=_listed("_T-CRM-GBR-3", "1400000003")))
		self.assertNotEqual(other, lead)
		self.assertNoPullErrors()

	def test_a_closed_conversation_is_not_joined(self):
		_, lead = _file(_raw(msgid="S1closed", name="closedmember", body=_listed("_T-CRM-GBR-4", "1400000004")))
		frappe.db.set_value("CRM Lead", lead, {"status": "Junk", "lost_reason": None}, update_modified=False)
		_, again = _file(_raw(msgid="S2closed", name="closedmember", body=_body("Hi", _trailer(listing="1400000004"))))
		self.assertNotEqual(again, lead)

	def test_a_converted_conversation_takes_the_question_on_its_deal(self):
		_, lead = _file(_raw(msgid="T1deal", name="dealmember", body=_listed("_T-CRM-GBR-5", "1400000005")))
		deal = frappe.get_doc("CRM Lead", lead).convert_to_deal()
		self.assertEqual(_file(_raw(msgid="T2deal", name="dealmember", body=_listed("_T-CRM-GBR-6", "1400000006"))),
			("CRM Deal", deal))


class TestRepairGunBrokerMail(TeamMailCase):
	def _state(self) -> list:
		leads = frappe.get_all("CRM Lead", fields=["name", "status", mail.CATEGORY_FIELD, "modified"],
			order_by="name")
		comms = frappe.get_all("Communication", filters={"reference_doctype": "CRM Lead"},
			fields=["name", "reference_name"], order_by="name")
		return [leads, comms, frappe.db.count("Comment")]

	def _setup(self) -> dict:
		buyer = _legacy_notice_lead(BUYER, SOLD)
		_comm(BUYER, "Payment sent", "Pat Person", buyer)
		seller = _legacy_notice_lead(SELLER, WON)
		_comm(SELLER, "Tracking number", "Sam Seller", seller)
		only_notice = _legacy_notice_lead("gb-quiet@example.org", SOLD)
		first = mail._new_lead("", "repeatmember", source="GunBroker", **{gunbroker.MSGID_FIELD: "U1first"})
		second = mail._new_lead("", "repeatmember", source="GunBroker", **{gunbroker.MSGID_FIELD: "U2second"})
		question = _comm(GB, "Question (Do not remove MsgId: U2second)", "repeatmember", second)
		serial = _gun("_T-CRM-GBR-7", 899)
		add_product_row("CRM Lead", second, frappe.db.get_value("Serial No", serial, "item_code"), serial)
		return {"buyer": buyer, "seller": seller, "quiet": only_notice, "first": first, "second": second,
			"question": question, "serial": serial}

	def test_a_dry_run_lists_and_changes_nothing(self):
		made = self._setup()
		before = self._state()
		for dry_run in (1, "1", True, None):
			report = mail.repair_gunbroker_mail(dry_run) if dry_run is not None else mail.repair_gunbroker_mail()
			self.assertTrue(report["dry_run"])
		self.assertEqual(self._state(), before)
		mine = set(made.values())
		self.assertEqual({(p["lead"], p["action"]) for p in report["pinned"] if p["lead"] in mine},
			{(made["buyer"], "screen"), (made["seller"], gunbroker.SELLER_CATEGORY)})
		self.assertEqual([(d["keep"], d["merge"]) for d in report["duplicates"] if d["keep"] in mine],
			[(made["first"], [made["second"]])])

	def test_a_run_files_the_pinned_leads_and_merges_the_repeats(self):
		made = self._setup()
		report = mail.repair_gunbroker_mail(dry_run=0)
		self.assertFalse(report["dry_run"])
		self.assertFalse(report["failed"])
		self.assertEqual(frappe.db.get_value("CRM Lead", made["buyer"], ["status", mail.CATEGORY_FIELD]), ("New", None))
		self.assertEqual(frappe.db.get_value("CRM Lead", made["seller"], ["status", mail.CATEGORY_FIELD]),
			(mail.OTHER_MAIL, gunbroker.SELLER_CATEGORY))
		self.assertEqual(frappe.db.get_value("CRM Lead", made["quiet"], "status"), mail.OTHER_MAIL, "no person's mail yet")
		self.assertFalse(frappe.db.exists("CRM Lead", made["second"]))
		self.assertEqual(frappe.db.get_value("Communication", made["question"], "reference_name"), made["first"])
		self.assertEqual([r[2] for r in _rows("CRM Lead", made["first"])], [made["serial"]])
		# The merged conversation's later mail finds the lead it went into.
		self.assertEqual(mail._gunbroker_place("U2second"), ("CRM Lead", made["first"]))
		again = mail.repair_gunbroker_mail(dry_run=0)
		mine = set(made.values())
		self.assertFalse([p for p in again["pinned"] if p["lead"] in mine], "run twice: nothing left")
		self.assertFalse([d for d in again["duplicates"] if d["keep"] in mine])
