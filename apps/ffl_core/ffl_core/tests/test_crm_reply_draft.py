"""Draft reply and Insert price on a CRM Lead or Deal (ffl_core.crm.reply_draft, ffl_core.crm.price_insert).

Real documents on the test site; the only stand-in is the provider, a function of this module registered
as the two hooks (``hooks``), which records the context it was given. Every test runs inside the savepoint
CRMTestCase rolls back."""
from __future__ import annotations

import json
from contextlib import contextmanager

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from ffl_core import pricing
from ffl_core.api import crm as api
from ffl_core.crm import gunbroker, price_insert, reply_draft, setup, wants
from ffl_core.crm.inquiries import EVENT_FIELD
from ffl_core.crm.sell_requests import SELL
from ffl_core.tests.crm_support import CLERK, CRMTestCase, make_item, product_of, user
from ffl_core.tests.test_crm_deal_pos import OWNER, _contact, _customer, _switch, _token
from ffl_core.tests.test_extension_hooks import hooks

HERE = "ffl_core.tests.test_crm_reply_draft"
ASKED: list = []
ANSWER: list = []
READY: list = [True]

GLOCK = "_T-RD-GLOCK19"      # priced gun by gun
MOSIN = "_T-RD-MOSIN"        # uniform price
OLD = "_T-RD-ANTIQUE"
UNPRICED = "_T-RD-UNPRICED"
SLING = "_T-RD-SLING"        # a counted item


def ready():
	return READY[0]


def provider(context):
	ASKED.append(context)
	answer = ANSWER[0] if ANSWER else None
	if isinstance(answer, Exception):
		raise answer
	return answer


@contextmanager
def drafting(answer=None, ready_answer=True):
	ASKED.clear()
	ANSWER[:] = [answer if answer is not None else {"body": "Hi Sam,\n\nYes, it is in.\nCome by.", "handoff": None,
		"refs": []}]
	READY[0] = ready_answer
	with hooks(reply_draft.READY_HOOK, [f"{HERE}.ready"]), hooks(reply_draft.DRAFT_HOOK, [f"{HERE}.provider"]):
		yield


def _gun(code: str, serial: str, price: float, status: str = "Active") -> str:
	sn = frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": code, "sell_price": price})
	sn.insert(ignore_permissions=True)
	frappe.db.set_value("Serial No", sn.name, "status", status, update_modified=False)
	return sn.name


def _deal(contact: str | None = None, owner: str = CLERK, products=(), **fields):
	contact = contact or _contact(f"{_token()}@example.com", first_name="Sam")
	doc = frappe.new_doc("CRM Deal")
	doc.status = frappe.get_all("CRM Deal Status", filters={"type": ["not in", ["Won", "Lost"]]}, pluck="name",
		order_by="position asc", limit=1)[0]
	doc.deal_owner = owner
	doc.append("contacts", {"contact": contact, "is_primary": 1})
	for row in products:
		doc.append("products", row)
	doc.update(fields)
	doc.insert(ignore_permissions=True)
	return doc


def _lead(email: str | None = None, **fields):
	return frappe.get_doc({"doctype": "CRM Lead", "first_name": "Sam", "last_name": "Buyer",
		"email": email or f"{_token()}@example.com", "lead_owner": CLERK, **fields}).insert(ignore_permissions=True)


def _mail(doc, content: str, *, sender: str = "sam@example.com", received: bool = True, subject: str = "Glock 19?",
		at=None, recipients: str = "sales@example.com", **fields) -> str:
	name = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication",
		"communication_medium": "Email", "sent_or_received": "Received" if received else "Sent", "sender": sender,
		"recipients": recipients, "subject": subject, "content": content, "reference_doctype": doc.doctype,
		"reference_name": doc.name, **fields}).insert(ignore_permissions=True).name
	frappe.db.set_value("Communication", name, {"communication_date": at or now_datetime(), "sender": sender,
		"recipients": recipients, "sent_or_received": "Received" if received else "Sent"}, update_modified=False)
	return name


def _note(doc, content: str, at=None, event: str | None = None) -> str:
	name = frappe.get_doc({"doctype": "FCRM Note", "title": "Website price request", "content": content,
		"reference_doctype": doc.doctype, "reference_docname": doc.name,
		**({EVENT_FIELD: event} if event else {})}).insert(ignore_permissions=True).name
	frappe.db.set_value("FCRM Note", name, "creation", at or now_datetime(), update_modified=False)
	return name


def _ago(minutes: int):
	return add_to_date(now_datetime(), minutes=-minutes)


def _user_email(user_name: str, address: str, idx: int, outgoing: int = 1) -> None:
	account = f"_T RD {address}"
	if not frappe.db.exists("Email Account", account):
		frappe.get_doc({"doctype": "Email Account", "email_account_name": account, "email_id": address,
			"enable_incoming": 0, "enable_outgoing": 0, "awaiting_password": 1}).insert(ignore_permissions=True)
	frappe.get_doc({"doctype": "User Email", "parent": user_name, "parenttype": "User", "parentfield": "user_emails",
		"idx": idx, "email_account": account, "email_id": address, "enable_outgoing": outgoing}).db_insert()


def _hold(deal: str, serial: str, code: str) -> None:
	frappe.get_doc({"doctype": "CRM Deal Hold", "serial_no": serial, "item_code": code, "deal": deal,
		"status": "Active", "held_by": "Administrator", "expires_at": add_days(now_datetime(), 1)}).db_insert()


class ReplyCase(CRMTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for brand in ("Glock", "Mosin"):
			if not frappe.db.exists("Brand", brand):
				frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
		make_item(GLOCK, item_name="Glock 19 Gen5 9mm", is_stock_item=1, has_serial_no=1, is_firearm=1,
			uniform_sell_price=0, standard_rate=0, manufacturer="Glock", model_name="19 Gen5")
		make_item(MOSIN, item_name="Mosin Nagant 91/30", is_stock_item=1, has_serial_no=1, is_firearm=1,
			uniform_sell_price=1, standard_rate=299, manufacturer="Mosin", is_used=1)
		make_item(OLD, item_name="Antique Colt revolver", is_stock_item=1, has_serial_no=1, is_firearm=1,
			uniform_sell_price=0, standard_rate=0)
		frappe.db.set_value("Item", OLD, "is_antique", 1, update_modified=False)
		make_item(UNPRICED, item_name="Unpriced Glock 17", is_stock_item=1, has_serial_no=1, is_firearm=1,
			uniform_sell_price=0, standard_rate=0)
		make_item(SLING, item_name="Glock sling", is_stock_item=1)
		user(OWNER, "Sales User")

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Item", MOSIN, {"sale_price": 0, "sale_from": None, "sale_to": None}, update_modified=False)


class TestProviders(ReplyCase):
	def test_no_provider_no_button_and_no_draft(self):
		deal = _deal()
		_mail(deal, "<p>Do you have a Glock 19?</p>")
		with hooks(reply_draft.READY_HOOK, []), hooks(reply_draft.DRAFT_HOOK, []):
			self.assertFalse(api.reply_draft_ready("CRM Deal", deal.name)["ready"])
			with self.assertRaisesRegex(frappe.ValidationError, "Couldn't write a draft"):
				api.draft_reply("CRM Deal", deal.name)
		with drafting(ready_answer=False):
			self.assertFalse(api.reply_draft_ready("CRM Deal", deal.name)["ready"])
		with drafting():
			self.assertTrue(api.reply_draft_ready("CRM Deal", deal.name)["ready"])

	def test_refused_is_shown_as_given_and_failure_is_logged(self):
		deal = _deal()
		_mail(deal, "<p>Do you have a Glock 19?</p>")
		with drafting({"refused": "Today's AI limit is used up. Reply as usual."}):
			with self.assertRaisesRegex(frappe.ValidationError, "Today's AI limit is used up"):
				api.draft_reply("CRM Deal", deal.name)
		before = frappe.db.count("Error Log", {"method": ["like", f"{reply_draft.DRAFT_HOOK}%"]})
		with drafting(RuntimeError("boom")):
			with self.assertRaisesRegex(frappe.ValidationError, "Couldn't write a draft. Reply as usual."):
				api.draft_reply("CRM Deal", deal.name)
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", f"{reply_draft.DRAFT_HOOK}%"]}), before + 1)
		ANSWER[:] = []
		with hooks(reply_draft.READY_HOOK, [f"{HERE}.ready"]), hooks(reply_draft.DRAFT_HOOK, [f"{HERE}.provider"]):
			READY[0] = True
			with self.assertRaisesRegex(frappe.ValidationError, "Couldn't write a draft"):
				api.draft_reply("CRM Deal", deal.name)

	def test_body_is_escaped_into_paragraphs(self):
		self.assertEqual(reply_draft.to_html("Hi <b>Sam</b>,\n\nLine one\nline two\n\n\n"),
			"<p>Hi &lt;b&gt;Sam&lt;/b&gt;,</p><p>Line one<br>line two</p>")


class TestEnvelope(ReplyCase):
	def test_an_email_reply(self):
		deal = _deal()
		comm = _mail(deal, "<p>Do you have a Glock 19?</p>", sender="Sam Buyer <Sam@Example.com>",
			recipients="sales@example.com, other@example.com")
		with drafting():
			out = api.draft_reply("CRM Deal", deal.name)
		self.assertEqual((out["to"], out["subject"], out["in_reply_to"], out["channel"]),
			("sam@example.com", "Re: Glock 19?", comm, "email"))
		self.assertEqual(out["content_html"], "<p>Hi Sam,</p><p>Yes, it is in.<br>Come by.</p>")

	def test_a_subject_already_re_stays(self):
		deal = _deal()
		_mail(deal, "<p>Still there?</p>", subject="Re: Glock 19?")
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", deal.name)["subject"], "Re: Glock 19?")

	def test_a_gunbroker_reply_keeps_its_msgid(self):
		lead = _lead()
		subject = "Question regarding GunBroker.com Item #1219067857 (Do not remove MsgId: H1gweZbRzl)"
		_mail(lead, "-- BEGIN MESSAGE FROM USER ---\nWould you take $500?\n-- END MESSAGE FROM USER ---\n\nItem Number: 1",
			sender=f"jsmith42 <{gunbroker.SENDER}>", subject=subject)
		with drafting():
			out = api.draft_reply("CRM Lead", lead.name)
		self.assertEqual((out["to"], out["subject"], out["channel"]), (gunbroker.SENDER, "Re: " + subject, "gunbroker"))
		self.assertEqual(ASKED[0]["latest"]["body"], "Would you take $500?")
		self.assertEqual(ASKED[0]["channel"], "gunbroker")

	def test_from_is_the_mailbox_the_mail_came_to(self):
		_user_email(CLERK, "ffl-rd-mine@example.com", 1)
		_user_email(CLERK, "ffl-rd-sales@example.com", 2)
		_user_email(CLERK, "ffl-rd-noout@example.com", 3, outgoing=0)
		deal = _deal()
		_mail(deal, "<p>Glock 19?</p>", recipients="ffl-rd-sales@example.com")
		frappe.set_user(CLERK)
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", deal.name)["sender"], "ffl-rd-sales@example.com")
		frappe.set_user("Administrator")
		other = _deal()
		_mail(other, "<p>Glock 19?</p>", recipients="ffl-rd-noout@example.com")
		frappe.set_user(CLERK)
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", other.name)["sender"], "ffl-rd-mine@example.com", "the first that sends")

	def test_a_gunbroker_mail_stored_without_a_to_matches_its_mailbox(self):
		from ffl_core.tests.test_crm_team_mail import ADDRESS, INFO, _account

		_account(INFO)
		_user_email(CLERK, "ffl-rd-mine@example.com", 1)
		_user_email(CLERK, ADDRESS[INFO], 2)
		lead = _lead()
		comm = _mail(lead, "-- BEGIN MESSAGE FROM USER ---\nHi\n-- END MESSAGE FROM USER ---", sender=gunbroker.SENDER,
			subject="Q (Do not remove MsgId: H1gweZbRzl)", email_account=INFO)
		frappe.db.set_value("Communication", comm, "recipients", None, update_modified=False)
		frappe.set_user(CLERK)
		with drafting():
			self.assertEqual(api.draft_reply("CRM Lead", lead.name)["sender"], ADDRESS[INFO])

	def test_one_mailbox_that_is_the_login_leaves_from_empty(self):
		_user_email(CLERK, CLERK, 1)
		deal = _deal()
		_mail(deal, "<p>Glock 19?</p>")
		frappe.set_user(CLERK)
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", deal.name)["sender"], "")


class TestLatestMessage(ReplyCase):
	def test_a_deal_and_its_lead_mixed_newest_wins(self):
		lead = _lead()
		deal = _deal(lead=lead.name)
		_mail(deal, "<p>On the deal, older</p>", at=_ago(30))
		newest = _mail(lead, "<p>On the lead, newest</p>", at=_ago(5))
		_mail(deal, "<p>Our answer</p>", received=False, at=_ago(1))
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", deal.name)["in_reply_to"], newest)
		self.assertEqual(ASKED[0]["latest"]["body"], "On the lead, newest")
		self.assertEqual([t["direction"] for t in ASKED[0]["thread"]], ["out", "in"])

	def test_an_inquiry_newer_than_the_mail(self):
		deal = _deal(_contact("sam@example.com", first_name="Sam"), lead_name="Sam Buyer")
		_mail(deal, "<p>Older mail</p>", at=_ago(30))
		_note(deal, "<p>What is your best price?</p>", at=_ago(2), event=_token())
		with drafting():
			out = api.draft_reply("CRM Deal", deal.name)
		self.assertEqual((out["to"], out["subject"], out["in_reply_to"], out["native_ok"]),
			("sam@example.com", f"Sam Buyer (#{deal.name})", None, 1))

	def test_nothing_from_the_customer(self):
		deal = _deal()
		_mail(deal, "<p>Our mail</p>", received=False)
		_note(deal, "<p>Internal: call him</p>")
		with drafting(), self.assertRaisesRegex(frappe.ValidationError, "No mail from the customer to reply to."):
			api.draft_reply("CRM Deal", deal.name)


class TestContext(ReplyCase):
	def test_what_the_provider_is_given(self):
		customer = _customer()
		address = frappe.get_doc({"doctype": "Address", "address_title": "Home", "address_type": "Billing",
			"address_line1": "1 Main St", "city": "Dallas", "state": "Texas", "country": "United States",
			"pincode": "75001", "is_primary_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": customer}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", customer, "customer_primary_address", address.name, update_modified=False)
		serial = _gun(GLOCK, "_T-RD-SN-CTX", 549)
		deal = _deal(products=[{"product_code": product_of(GLOCK), "qty": 1, "ffl_serial_no": serial}],
			first_name="Samuel Lee", erpnext_customer=customer)
		long_text = "word " * 2000
		_mail(deal, f"<p>Is the Glock in?</p><p>On Mon, Sam wrote:</p><blockquote>{long_text}</blockquote>",
			at=_ago(1))
		_mail(deal, f"<p>{long_text}</p>", received=False, at=_ago(10))
		_note(deal, "<p>INTERNAL-SECRET</p>")
		with drafting():
			api.draft_reply("CRM Deal", deal.name)
		ctx = dict(ASKED[0])
		self.assertTrue(callable(ctx.pop("find_stock")))
		self.assertEqual(ctx["latest"]["body"], "Is the Glock in?")
		self.assertEqual(ctx["customer"], {"first_name": "Samuel", "state": "TX", "is_customer": True})
		self.assertEqual(len(ctx["thread"][0]["body"]), reply_draft.THREAD_EACH)
		flat = json.dumps(ctx, default=str)
		for secret in ("INTERNAL-SECRET", serial, "1 Main St", "example.com", "75001"):
			self.assertNotIn(secret, flat)


class TestFacts(ReplyCase):
	def test_per_gun_prices_holds_and_exclusions(self):
		cheap = _gun(GLOCK, "_T-RD-SN-1", 499)
		_gun(GLOCK, "_T-RD-SN-2", 549)
		held = _gun(GLOCK, "_T-RD-SN-3", 450)
		_gun(OLD, "_T-RD-SN-OLD", 900)
		_gun(UNPRICED, "_T-RD-SN-UNP", 0)
		deal = _deal(products=[{"product_code": product_of(c), "qty": 1} for c in (GLOCK, OLD, UNPRICED)])
		_hold(deal.name, held, GLOCK)
		_mail(deal, "<p>Glock?</p>")
		with drafting():
			api.draft_reply("CRM Deal", deal.name)
		[glock] = ASKED[0]["facts"]
		self.assertEqual((glock["ref"], glock["name"], glock["brand"].lower(), glock["model"], glock["condition"]),
			("F1", "Glock 19 Gen5 9mm", "glock", "19 Gen5", "new"))
		self.assertEqual(glock["available"], 2, "the held gun is not for sale")
		self.assertEqual([u["price"] for u in glock["units"]], [499, 549])
		self.assertEqual(glock["units"][0]["price"], pricing.counter_price_for(GLOCK, cheap))
		self.assertEqual(glock["price"], pricing.counter_price_for(GLOCK))

	def test_a_sale_only_while_it_runs(self):
		_gun(MOSIN, "_T-RD-SN-M1", 0)
		deal = _deal(products=[{"product_code": product_of(MOSIN), "qty": 1}])
		_mail(deal, "<p>Mosin?</p>")
		frappe.db.set_value("Item", MOSIN, {"sale_price": 249, "sale_from": add_days(nowdate(), -10),
			"sale_to": add_days(nowdate(), -1)}, update_modified=False)
		with drafting():
			api.draft_reply("CRM Deal", deal.name)
		self.assertEqual((ASKED[0]["facts"][0]["price"], ASKED[0]["facts"][0]["sale_price"]), (299, None))
		self.assertEqual(ASKED[0]["facts"][0]["condition"], "used")
		frappe.db.set_value("Item", MOSIN, "sale_to", add_days(nowdate(), 3), update_modified=False)
		with drafting():
			api.draft_reply("CRM Deal", deal.name)
		self.assertEqual((ASKED[0]["facts"][0]["price"], ASKED[0]["facts"][0]["sale_price"]), (249, 249))

	def test_at_most_twelve(self):
		facts = reply_draft.Facts()
		codes = []
		for i in range(14):
			code = f"_T-RD-MANY-{i}"
			make_item(code, item_name=f"Many {i}", is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1,
				standard_rate=100 + i)
			_gun(code, f"_T-RD-SN-MANY-{i}", 0)
			codes.append(code)
		for code in codes:
			facts.add(code)
		self.assertEqual(len(facts.facts), reply_draft.FACT_LIMIT)

	def test_wants_and_find_stock_use_the_want_rule(self):
		_gun(MOSIN, "_T-RD-SN-M2", 0)
		_gun(GLOCK, "_T-RD-SN-G9", 610)
		contact = _contact(f"{_token()}@example.com")
		deal = _deal(contact)
		frappe.get_doc({"doctype": wants.WANT, "contact": contact, "deal": deal.name, "want_owner": CLERK,
			"keywords": "Mosin 91/30"}).insert(ignore_permissions=True)
		_mail(deal, "<p>Any Glock 19 Gen 5?</p>")
		with drafting():
			api.draft_reply("CRM Deal", deal.name)
		self.assertEqual([f["name"] for f in ASKED[0]["facts"]], ["Mosin Nagant 91/30"])
		found = ASKED[0]["find_stock"]([{"keywords": "glock 19gen5", "budget": 700}, {"keywords": "P-228"}])
		self.assertEqual([(f["ref"], f["name"]) for f in found], [("F2", "Glock 19 Gen5 9mm")])
		self.assertEqual(ASKED[0]["find_stock"]("nonsense"), [])
		# The same samples as the want list's own rule.
		arrival = wants.Arrival(item_code="x", item_name="x", serial=None, text=wants._text("Mosin-Nagant M91/30", "SIG P-228"))
		stock = [arrival]
		for keywords in ("Mosin 91/30", "p228", "SIG P 228"):
			self.assertEqual(reply_draft.matching_items([frappe._dict(item=None, keywords=keywords)], stock), ["x"])
		for keywords in ("Mosin 44", "P229"):
			self.assertEqual(reply_draft.matching_items([frappe._dict(item=None, keywords=keywords)], stock), [])


class TestPermissions(ReplyCase):
	def test_write_is_needed_to_draft_and_a_crm_role_for_anything(self):
		deal = _deal(owner=OWNER)
		_mail(deal, "<p>Glock?</p>")
		frappe.share.add_docshare("CRM Deal", deal.name, CLERK, read=1, write=0, flags={"ignore_share_permission": 1})
		frappe.set_user(CLERK)
		with drafting():
			self.assertIn("ready", api.reply_draft_ready("CRM Deal", deal.name))
			with self.assertRaises(frappe.PermissionError):
				api.draft_reply("CRM Deal", deal.name)
		frappe.set_user("Administrator")
		outsider = user("ffl-rd-outsider@example.com", "Stock User")
		frappe.set_user(outsider)
		with drafting():
			for call in (lambda: api.reply_draft_ready("CRM Deal", deal.name), lambda: api.draft_reply("CRM Deal", deal.name),
					lambda: api.price_options("CRM Deal", deal.name)):
				with self.assertRaises(frappe.PermissionError):
					call()

	def test_only_leads_and_deals(self):
		with self.assertRaises(frappe.ValidationError):
			api.draft_reply("Customer", "x")


class TestButtons(ReplyCase):
	def test_scripts_install_and_update(self):
		setup.ensure_reply_scripts()
		for name, doctype in ((setup.LEAD_SCRIPT, "CRM Lead"), (setup.DEAL_REPLY_SCRIPT, "CRM Deal")):
			row = frappe.db.get_value("CRM Form Script", name, ["dt", "enabled", "script"], as_dict=True)
			self.assertEqual((row.dt, row.enabled), (doctype, 1))
			for needle in ("Draft reply", "Insert price", "fflComposer", f"class {doctype.replace(' ', '')} "):
				self.assertIn(needle, row.script)
		frappe.db.set_value("CRM Form Script", setup.LEAD_SCRIPT, {"script": "old", "enabled": 0})
		setup.ensure_reply_scripts()
		self.assertEqual(frappe.db.get_value("CRM Form Script", setup.LEAD_SCRIPT, ["script", "enabled"]),
			(setup.reply_script_js("CRM Lead"), 1))

	def test_which_buttons(self):
		deal = _deal()
		with drafting():
			_switch(0)
			self.assertEqual(api.reply_draft_ready("CRM Deal", deal.name), {"ready": True, "insert_price": False})
			_switch(1)
			self.assertEqual(api.reply_draft_ready("CRM Deal", deal.name), {"ready": True, "insert_price": True})
			sell = _deal(**{SELL: 1})
			self.assertEqual(api.reply_draft_ready("CRM Deal", sell.name), {"ready": False, "insert_price": False})


class TestQuoteMatch(ReplyCase):
	def test_the_quoted_older_mail_is_the_one_answered(self):
		deal = _deal()
		older = _mail(deal, "<div>Do you still have the <b>Glock&nbsp;19</b>?<br>Thanks, Sam</div>", at=_ago(60))
		_mail(deal, "<p>Also, any Mosins?</p>", at=_ago(5))
		quote = "Do you still have the Glock 19?Thanks, Sam"   # TipTap's textContent: no spacing between blocks
		with drafting():
			out = api.draft_reply("CRM Deal", deal.name, quote_text=quote, composer=1)
		self.assertEqual((out["native_ok"], out["in_reply_to"]), (1, older))
		self.assertIn("Glock", ASKED[0]["latest"]["body"])
		self.assertNotIn("<p class=\"signature\">", out["content_html"])

	def test_a_quote_of_our_own_mail_or_of_nothing(self):
		deal = _deal()
		latest = _mail(deal, "<p>Customer asks again</p>", at=_ago(1))
		_mail(deal, "<p>Our offer stands</p>", received=False, at=_ago(10))
		for quote in ("Our offer stands", "Something else entirely"):
			with drafting():
				out = api.draft_reply("CRM Deal", deal.name, quote_text=quote, composer=1)
			self.assertEqual((out["native_ok"], out["in_reply_to"]), (0, latest), quote)

	def test_no_quote(self):
		deal = _deal()
		_mail(deal, "<p>A mail</p>")
		with drafting():
			self.assertEqual(api.draft_reply("CRM Deal", deal.name, composer=1)["native_ok"], 0)
		lead = _lead()
		_note(lead, "<p>Price?</p>", event=_token())
		with drafting():
			self.assertEqual(api.draft_reply("CRM Lead", lead.name, composer=1)["native_ok"], 1)

	def test_the_signature_comes_apart(self):
		frappe.db.set_value("User", "Administrator", "email_signature", "Pat\nThe store", update_modified=False)
		deal = _deal()
		_mail(deal, "<p>A mail</p>")
		with drafting():
			out = api.draft_reply("CRM Deal", deal.name)
		self.assertIn("Pat<br>The store", out["signature_html"])
		self.assertNotIn("Pat", out["content_html"])


class TestInsertPrice(ReplyCase):
	def test_order_limit_and_exclusions(self):
		_switch(1)
		picked = _gun(GLOCK, "_T-RD-SN-P1A1", 549)
		_gun(GLOCK, "_T-RD-SN-P2B2", 499)
		_gun(GLOCK, "_T-RD-SN-P3C3", 599)
		_gun(MOSIN, "_T-RD-SN-PM", 0)
		_gun(OLD, "_T-RD-SN-POLD", 900)
		contact = _contact(f"{_token()}@example.com")
		deal = _deal(contact, products=[
			{"product_code": product_of(GLOCK), "qty": 1, "ffl_serial_no": picked},
			{"product_code": product_of(GLOCK), "qty": 1},
			{"product_code": product_of(OLD), "qty": 1},
			{"product_code": product_of(UNPRICED), "qty": 1}])
		frappe.get_doc({"doctype": wants.WANT, "contact": contact, "deal": deal.name, "want_owner": CLERK,
			"keywords": "mosin"}).insert(ignore_permissions=True)
		out = api.price_options("CRM Deal", deal.name)
		self.assertEqual(out["default"], 0)
		self.assertEqual([(o["item_code"], o["serial_no"]) for o in out["options"]], [
			(GLOCK, picked), (GLOCK, None), (GLOCK, "_T-RD-SN-P2B2"), (GLOCK, "_T-RD-SN-P3C3"), (MOSIN, None)])
		self.assertEqual([o["label"] for o in out["options"]][:3], [
			"Glock 19 Gen5 9mm · P1A1 — $549.00", "Glock 19 Gen5 9mm — from $499.00", "Glock 19 Gen5 9mm · P2B2 — $499.00"])
		flat = json.dumps(out)
		self.assertNotIn("purchase_rate", flat)
		self.assertNotIn("_T-RD-SN-P1A1\"", flat.replace(f'"serial_no": "{picked}"', ""))

	def test_at_most_fifteen(self):
		rows = []
		for i in range(17):
			code = f"_T-RD-IP-{i}"
			make_item(code, item_name=f"IP {i}", is_stock_item=1, has_serial_no=1, is_firearm=1, uniform_sell_price=1,
				standard_rate=10 + i)
			_gun(code, f"_T-RD-SN-IP-{i}", 0)
			rows.append({"product_code": product_of(code), "qty": 1})
		deal = _deal(products=rows)
		self.assertEqual(len(api.price_options("CRM Deal", deal.name)["options"]), price_insert.OPTION_LIMIT)

	def test_the_text_is_the_register_price_now(self):
		serial = _gun(GLOCK, "_T-RD-SN-T1", 549)
		_gun(MOSIN, "_T-RD-SN-TM", 0)
		other = _gun(MOSIN, "_T-RD-SN-TM2", 0)
		deal = _deal()
		self.assertEqual(api.price_text("CRM Deal", deal.name, GLOCK, serial)["text"], "Glock 19 Gen5 9mm — $549.00")
		frappe.db.set_value("Serial No", serial, "sell_price", 529, update_modified=False)
		self.assertEqual(api.price_text("CRM Deal", deal.name, GLOCK, serial)["text"], "Glock 19 Gen5 9mm — $529.00")
		self.assertEqual(api.price_text("CRM Deal", deal.name, GLOCK)["text"], "Glock 19 Gen5 9mm — from $529.00")
		self.assertEqual(api.price_text("CRM Deal", deal.name, MOSIN)["text"],
			f"Mosin Nagant 91/30 — ${pricing.counter_price_for(MOSIN):.2f}")
		with self.assertRaisesRegex(frappe.ValidationError, "is not a gun of"):
			api.price_text("CRM Deal", deal.name, GLOCK, other)
		with self.assertRaisesRegex(frappe.ValidationError, "No price for this item."):
			api.price_text("CRM Deal", deal.name, UNPRICED)

	def test_read_is_needed(self):
		deal = _deal(owner=OWNER)
		frappe.set_user(CLERK)
		for call in (lambda: api.price_options("CRM Deal", deal.name), lambda: api.price_text("CRM Deal", deal.name, GLOCK)):
			with self.assertRaises(frappe.PermissionError):
				call()
