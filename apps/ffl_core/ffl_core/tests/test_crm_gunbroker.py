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
from ffl_core.tests.crm_support import PER_GUN, product_of
from ffl_core.tests.test_crm_deal import _gun
from ffl_core.tests.test_crm_other_mail import _lead
from ffl_core.tests.test_crm_team_mail import INFO, SALES, TeamMailCase, _assignees, _pull, _stored

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
		self.assertEqual(_rows("CRM Lead", name), [])

	def test_later_mail_of_the_conversation_goes_to_the_same_lead(self):
		first = _raw(msgid="B16BykWCfg", re="")
		_, lead = _file(first)
		again = _raw(msgid="B16BykWCfg", re="Re: ", name="John Smith", subject="Re: Question regarding GunBroker.com  Order #32000014  (Do not remove\tMsgId: B16BykWCfg)")
		self.assertEqual(_file(again), ("CRM Lead", lead))
		self.assertEqual(frappe.db.count("CRM Lead", {gunbroker.MSGID_FIELD: "B16BykWCfg"}), 1)
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "first_name"), "jsmith42", "the first name stays")
		_, other = _file(_raw(msgid="C99other999"))
		self.assertNotEqual(other, lead)

	def test_a_converted_conversation_goes_to_its_deal(self):
		_, lead = _file(_raw(msgid="D11converted"))
		deal = frappe.get_doc("CRM Lead", lead).convert_to_deal()
		self.assertEqual(_file(_raw(msgid="D11converted")), ("CRM Deal", deal))

	def test_a_mail_without_a_msgid_is_other_mail_as_before(self):
		for msgid in (None, ""):
			raw = _raw(msgid=msgid, subject="Welcome to GunBroker.com") if msgid is None else _raw(msgid="x")
		raw = _raw(subject="Account notice from GunBroker.com")
		doctype, name = _file(raw)
		lead = _lead(GB)
		self.assertEqual((doctype, name, lead.status, lead.get(mail.CATEGORY_FIELD)), ("CRM Lead", lead.name, mail.OTHER_MAIL, "GunBroker"))
		_, gb = _file(_raw(msgid="E22conversat"))
		self.assertNotEqual(gb, lead.name)
		# The address's own lookups still find the other mail lead, not a conversation.
		doctype, again = _file(_raw(subject="Another notice from GunBroker.com"))
		self.assertEqual(again, lead.name)

	def test_a_listing_that_cannot_be_told_still_makes_the_lead(self):
		_, name = _file(_raw(body=_body("Hi", _trailer(listing="999", sku="NO-SUCH::SN"))))
		self.assertEqual(_rows("CRM Lead", name), [])
		self.assertNoPullErrors()
		_, name = _file(_raw(msgid="F33noanswer", body="no markers at all"))
		self.assertEqual(_rows("CRM Lead", name), [])

	def test_a_failing_listing_lookup_still_makes_the_lead(self):
		errors = frappe.db.count("Error Log")
		with patch("ffl_integrations.gunbroker.webhooks.resolve_listing", side_effect=RuntimeError("boom")):
			_, name = _file(_raw(msgid="G44lookupfail"))
		self.assertEqual(_rows("CRM Lead", name), [])
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)

	def test_the_facts_of_a_subject_and_body_in_both_layouts(self):
		subject = "Re: Question regarding GunBroker.com  Item #1219067857  (Do not remove\n MsgId: H1gweZbRzl)"
		for sep in (": ", " | "):
			facts = gunbroker.conversation(GB, subject, "<pre>" + _body("q", _trailer(sku="AB-1::SN1", sep=sep)).replace("\n", "<br>") + "</pre>")
			self.assertEqual(facts, {"msgid": "H1gweZbRzl", "listing": "1219067857", "sku": "AB-1::SN1"}, sep)
		self.assertIsNone(gunbroker.conversation("someone@example.org", subject, ""))
		self.assertIsNone(gunbroker.conversation(GB, "Welcome", ""))
