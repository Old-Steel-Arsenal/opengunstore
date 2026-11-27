"""Draft reply on a CRM Lead or Deal: what an extension needs to write a reply, and where it goes.

The core writes no reply itself. An extension registers two hooks (docs/extensions/hooks.md):
``gunshop_crm_reply_draft_ready`` (is drafting on: the Draft reply button shows) and
``gunshop_crm_reply_draft`` (write the text). This module picks the message being answered, works out
the envelope a reply to it has, gathers the context the provider gets, and turns the provider's plain
text into the HTML the composer takes. Nothing is written to the database and nothing is sent: the
employee sends from CRM's own composer, or from the fallback dialog through the same endpoint CRM's
composer calls (``crm.setup`` holds the page script). A draft written when the mail arrived
(``crm.arrival_draft``) is given instead of a new one while it still holds.

Which message: the employee opened CRM's composer with Reply on a mail, and the composer quotes it.
The page sends the quote's text; the received mail whose text starts the same way is the one being
answered (``native_ok``: the draft goes into that composer, whose To / Subject / From CRM set for that
mail). Without a quote, or one that matches nothing the customer sent, it is the record's newest
customer message: a received mail, or a website inquiry note (which may come with no mail at all).
For a Deal the timeline includes its Lead, as CRM's own does, when the user may read the Lead.

What the provider is given: the message, at most five others of the conversation, the customer's
first name and state, what the customer's memory holds (``crm.memory``: the record's summary and its rows,
blanked by ``crm.sensitive.redact``), and product facts with the register's price and what is in stock now.
Never an internal note, a serial number, an ID, an address beyond the state, a phone number or an email
address.
"""
from __future__ import annotations

import re
from html import escape

import frappe
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.utils import cint, flt, get_datetime, parse_addr, split_emails

from ffl_core.crm import TITLE, gunbroker, installed
from ffl_core.crm.deal import SERIAL_FIELD
from ffl_core.crm.deals import DEAL
from ffl_core.crm.inquiries import EVENT_FIELD
from ffl_core.crm.mail import LEAD
from ffl_core.crm.item import PRODUCT
from ffl_core.crm import memory, sell_requests, wants

READY_HOOK = "gunshop_crm_reply_draft_ready"
DRAFT_HOOK = "gunshop_crm_reply_draft"
DOCTYPES = (LEAD, DEAL)
NOTE = "FCRM Note"

QUOTE_LIMIT = 1000      # what the page may send of the quote
QUOTE_MATCH = 200       # how much of it (letters and digits) must start the mail
LATEST_LIMIT = 4000
THREAD_COUNT, THREAD_EACH, THREAD_TOTAL = 5, 1500, 8000
FACT_LIMIT, UNIT_LIMIT = 12, 5
# lazy: the memory's wants put at most 36 matches in budget order (each priced one by one); upgrade path: a
# batch price read if a store's matches ever run that long.
BUDGET_CANDIDATES = 3 * FACT_LIMIT
STORE_NAME_LIMIT, FIRST_NAME_LIMIT = 140, 40

NO_MAIL = "No mail from the customer to reply to."
FAILED = "Couldn't write a draft. Reply as usual."

_BEGIN = re.compile(r"-+\s*BEGIN MESSAGE FROM USER\s*-+", re.I)
_END = re.compile(r"-+\s*END MESSAGE FROM USER\s*-+", re.I)
_WROTE = re.compile(r"^\s*On .{0,300}wrote:\s*$", re.M | re.S)
_PARAGRAPHS = re.compile(r"\n\s*\n")


# ── the record and its messages ──

def record(doctype: str, name: str, ptype: str):
	"""The Lead or Deal the button is on, after the permission check."""
	if doctype not in DOCTYPES:
		frappe.throw(_("Only a lead or a deal has replies."))
	if not isinstance(name, str) or not frappe.db.exists(doctype, name):
		frappe.throw(_("This record no longer exists."), frappe.DoesNotExistError)
	frappe.has_permission(doctype, ptype, doc=name, throw=True)
	return frappe.get_doc(doctype, name)


def _timeline(doc) -> list[tuple[str, str]]:
	"""The records whose messages CRM shows on this page: a deal's lead too, when the user may read it."""
	out = [(doc.doctype, doc.name)]
	if doc.doctype == DEAL and doc.get("lead") and frappe.db.exists(LEAD, doc.lead) \
			and frappe.has_permission(LEAD, "read", doc=doc.lead):
		out.append((LEAD, doc.lead))
	return out


def _messages(doc) -> list[frappe._dict]:
	"""Every email (both ways) and website inquiry note on the timeline, newest first. Each has ``kind``
	("in", "out" or "note"), ``at``, ``creation``, ``subject`` and ``html``."""
	out = []
	for doctype, name in _timeline(doc):
		mailed = set()   # inquiries that are a mail too (inquiries._file_mail): their note would say it twice
		for c in frappe.get_all("Communication", filters={"reference_doctype": doctype, "reference_name": name,
				"communication_type": "Communication", "communication_medium": "Email"},
				fields=["name", "sender", "recipients", "cc", "bcc", "subject", "content", "communication_date",
					"sent_or_received", "email_account", "creation", "message_id"]):
			mailed.add(c.message_id)
			out.append(frappe._dict(c, kind="in" if c.sent_or_received == "Received" else "out",
				at=get_datetime(c.communication_date), html=c.content or ""))
		if frappe.db.has_column(NOTE, EVENT_FIELD):
			for n in frappe.get_all(NOTE, filters={"reference_doctype": doctype, "reference_docname": name,
					EVENT_FIELD: ["is", "set"]}, fields=["name", "title", "content", "creation", EVENT_FIELD]):
				if n.get(EVENT_FIELD) in mailed:
					continue
				out.append(frappe._dict(name=n.name, kind="note", at=get_datetime(n.creation), creation=n.creation, subject=n.title or "",
					html=n.content or ""))
	out.sort(key=lambda m: (m.at, m.name), reverse=True)
	return out


def _letters(text: str) -> str:
	"""Text as compared with a quote: letters and digits only, lower case. CRM's composer quotes a
	mail's HTML, and its text comes back without the spacing and markup the stored copy has."""
	return "".join(c for c in (text or "").lower() if c.isalnum())


def _quoted(messages: list, quote_text: str):
	"""The mail whose text the quote starts with (the newest of several), or None."""
	head = _letters(quote_text[:QUOTE_LIMIT])[:QUOTE_MATCH]
	if not head:
		return None
	# lazy: a 200-character prefix of the quote picks the mail; upgrade path: read in_reply_to from the
	# composer if CRM ever exposes it.
	return next((m for m in messages if m.kind in ("in", "out") and _letters(html_to_plain_text(m.html)).startswith(head)),
		None)


def target(doc, quote_text: str = "") -> tuple[frappe._dict, bool]:
	"""(the message being answered, whether the draft can go into CRM's composer)."""
	messages = _messages(doc)
	if quote_text:
		hit = _quoted(messages, quote_text)
		if hit and hit.kind == "in":
			return hit, True
	latest = next((m for m in messages if m.kind in ("in", "note")), None)
	if not latest:
		frappe.throw(_(NO_MAIL), title=_("Nothing to reply to"))
	# No quote: CRM's composer was opened from the top, with the record's email and its own subject,
	# which is the reply an inquiry note gets; a mail needs its own To and subject.
	native = not quote_text and latest.kind == "note" and bool(doc.get("email"))
	return latest, native


# ── the envelope ──

def _address(value: str | None) -> str:
	return (parse_addr(value or "")[1] or "").strip().lower()


def _reply_addresses(mail) -> list[str]:
	"""Every address on the mail in CRM's order (sender, To, CC, BCC). A mail stored without a To
	(GunBroker's, before ``crm.mail.fill_recipients``) counts its Email Account's address as its To."""
	recipients = mail.recipients or (mail.email_account and frappe.db.get_value("Email Account", mail.email_account,
		"email_id")) or ""
	out = []
	for value in (mail.sender, recipients, mail.cc, mail.bcc):
		for part in split_emails(value or ""):
			address = _address(part)
			if address and address not in out:
				out.append(address)
	return out


def sender_for(user: str, addresses: list[str]) -> str:
	"""The From CRM's composer would pick: the first of *addresses* among the user's mailboxes that send,
	else the first such mailbox; empty when the user's only one is their login address (Frappe's
	``email.make`` then sends from the default account)."""
	mine = [(r.email_id or "").strip().lower() for r in frappe.get_all("User Email",
		filters={"parenttype": "User", "parent": user, "enable_outgoing": 1}, fields=["email_id"], order_by="idx asc")]
	mine = [m for m in mine if m]
	if not mine or (len(mine) == 1 and mine[0] == (user or "").lower()):
		return ""
	return next((a for a in addresses if a in mine), mine[0])


def _subject(doc) -> str:
	"""CRM's composer's own subject for a record."""
	prefix = doc.get("lead_name") or doc.get("organization") or ""
	return f"{prefix} (#{doc.name})"


def envelope(doc, message) -> dict:
	"""{"to", "subject", "sender", "in_reply_to", "channel"} of a reply to *message*, as CRM's Reply sets them."""
	user = frappe.session.user
	if message.kind == "note":
		if not doc.get("email"):
			frappe.throw(_(NO_MAIL), title=_("Nothing to reply to"))
		return {"to": doc.email, "subject": _subject(doc), "sender": sender_for(user, []), "in_reply_to": None,
			"channel": "email"}
	subject = message.subject or ""
	sender = _address(message.sender)
	return {"to": sender, "subject": subject if subject.startswith("Re:") else f"Re: {subject}",
		"sender": sender_for(user, _reply_addresses(message)), "in_reply_to": message.name,
		"channel": "gunbroker" if sender == gunbroker.SENDER else "email"}


# ── the context ──

def _clip(text: str, limit: int) -> str:
	return (text or "").strip()[:limit]


def _body(message) -> str:
	"""A message's own words as plain text: a GunBroker member's question between its markers; any other
	mail without the quoted mail under it."""
	if message.kind == "in" and _address(message.sender) == gunbroker.SENDER:
		text = html_to_plain_text(message.html)
		begin, end = _BEGIN.search(text), _END.search(text)
		if begin and end and end.start() > begin.end():
			return text[begin.end():end.start()].strip()
		return text.strip()
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(message.html or "", "html.parser")
	for quote in soup.find_all("blockquote"):
		quote.decompose()
	text = html_to_plain_text(str(soup))
	wrote = _WROTE.search(text)
	return (text[:wrote.start()] if wrote else text).strip()


def _thread(messages: list, latest) -> list[dict]:
	out, total = [], 0
	for m in messages:
		if (m.kind, m.name) == (latest.kind, latest.name):
			continue
		body = _clip(_body(m), THREAD_EACH)
		if total + len(body) > THREAD_TOTAL:
			body = body[:THREAD_TOTAL - total]
		if not body:
			continue
		out.append({"direction": "out" if m.kind == "out" else "in", "at": str(m.at), "body": body})
		total += len(body)
		if len(out) == THREAD_COUNT or total >= THREAD_TOTAL:
			break
	return out


def _customer(doc) -> dict:
	from ffl_core.compliance.interstate_tax import normalize_us_state

	name = (doc.get("first_name") or doc.get("lead_name") or "").strip()
	customer = doc.get("erpnext_customer") if doc.doctype == DEAL else None
	state = ""
	if customer:
		from frappe.contacts.doctype.address.address import get_default_address

		address = get_default_address("Customer", customer)
		state = (address and normalize_us_state(frappe.db.get_value("Address", address, "state"))) or ""
	return {"first_name": name.split()[0][:FIRST_NAME_LIMIT] if name else "", "state": state,
		"is_customer": bool(customer)}


def _store_name() -> str:
	return _clip(frappe.db.get_single_value("Global Defaults", "default_company") or "", STORE_NAME_LIMIT)


# ── product facts ──

def record_lines(doc) -> list[tuple[str, str | None]]:
	"""(item code, gun or None) of each product line of the record that is a POS item, in line order."""
	rows = doc.get("products") or []
	codes = {r.product_code for r in rows if r.product_code}
	items = dict(frappe.get_all(PRODUCT, filters={"name": ["in", list(codes)]}, fields=["name", "erpnext_item_code"],
		as_list=True)) if codes else {}
	out = []
	for r in rows:
		item = items.get(r.product_code)
		if item and (item, r.get(SERIAL_FIELD) or None) not in out:
			out.append((item, r.get(SERIAL_FIELD) or None))
	return out


def record_wants(doc) -> list:
	"""The open wants of the deal or of its contacts."""
	if doc.doctype != DEAL:
		return []
	contacts = [r.contact for r in doc.get("contacts") or [] if r.contact]
	filters = {"status": ["in", wants.ACTIVE]}
	found = frappe.get_all(wants.WANT, filters={**filters, "deal": doc.name}, fields=["name", "item", "keywords"],
		order_by="creation asc")
	if contacts:
		seen = {w.name for w in found}
		found += [w for w in frappe.get_all(wants.WANT, filters={**filters, "contact": ["in", contacts]},
			fields=["name", "item", "keywords"], order_by="creation asc") if w.name not in seen]
	return found


def _company() -> str:
	return frappe.db.get_single_value("Global Defaults", "default_company") or ""


def in_stock_items() -> list[wants.Arrival]:
	"""Every Item for sale on our own shelf now, with the text a want's keywords are looked up in.

	lazy: every in-stock Item is read and matched in Python, as the want list is; fine for a store's
	stock in the low thousands. Upgrade path: a keyword index (the same as ``crm.wants``)."""
	from ffl_core import storage

	codes = set(frappe.get_all("Serial No", filters={"status": "Active"}, pluck="item_code", distinct=True))
	company = _company()
	if company:
		codes |= {c for c, q in storage.stock_qty(company).items() if q > 0}
	if not codes:
		return []
	return [wants.Arrival(item_code=i.name, item_name=i.item_name or i.name, serial=None,
			text=wants._text(i.item_name, i.manufacturer, i.model_name, i.caliber, i.description))
		for i in frappe.get_all("Item", filters={"name": ["in", list(codes)], "disabled": 0, "is_sales_item": 1},
			fields=["name", "item_name", "manufacturer", "model_name", "caliber", "description"], order_by="name asc")]


def matching_items(found_wants, stock: list | None = None) -> list[str]:
	"""Item codes in stock that fit any of *found_wants* (each with ``item`` / ``keywords``), by the want
	list's own rule (``crm.wants._fits``), in the order of the wants."""
	stock = in_stock_items() if stock is None else stock
	out = []
	for want in found_wants:
		for arrival in stock:
			if arrival.item_code not in out and wants._fits(want, arrival):
				out.append(arrival.item_code)
	return out


def units(item_code: str, limit: int) -> list[dict]:
	"""The guns of a serialized Item for sale now ({"serial", "sell_price"}), cheapest first, priced only:
	not held, not out on consignment, not a customer's gun (``firearm.available_serials_with_prices``)."""
	from ffl_core.firearm import available_serials_with_prices

	rows = available_serials_with_prices([item_code], exclude_unavailable=True).get(item_code) or []
	return [r for r in rows if flt(r.get("sell_price")) > 0][:limit]


def available_count(item) -> int:
	if item.has_serial_no:
		from ffl_core.firearm import available_serials_with_prices

		return len(available_serials_with_prices([item.name], exclude_unavailable=True).get(item.name) or [])
	from ffl_core import storage

	company = _company()
	return max(0, int(flt(storage.stock_qty(company, [item.name]).get(item.name)))) if company else 0


_FACT_ITEM_FIELDS = ["name", "item_name", "manufacturer", "brand", "model_name", "caliber", "is_used", "is_antique",
	"has_serial_no", "is_stock_item", "uniform_sell_price", "sale_price", "sale_from", "sale_to", "disabled"]


def fact(item_code: str, ref: str) -> dict | None:
	"""One product as the provider sees it, or None when it is not one to quote: an antique, an item that is
	not stocked, one with no price."""
	from ffl_core import pricing
	from ffl_core.firearm import _sale_active

	item = frappe.db.get_value("Item", item_code, _FACT_ITEM_FIELDS, as_dict=True)
	if not item or item.disabled or item.is_antique or not item.is_stock_item:
		return None
	per_gun = bool(item.has_serial_no and not item.uniform_sell_price)
	guns = units(item_code, UNIT_LIMIT) if per_gun else []
	price = flt(pricing.counter_price_for(item_code))
	if price <= 0:
		return None
	sale = _sale_active(item.sale_price, item.sale_from, item.sale_to) and flt(item.sale_price) == price \
		and not per_gun
	return {"ref": ref, "name": item.item_name or item_code, "brand": item.manufacturer or item.brand or "",
		"model": item.model_name or "", "caliber": item.caliber or "", "condition": "used" if item.is_used else "new",
		"price": price, "sale_price": price if sale else None, "sale_ends": str(item.sale_to) if sale and item.sale_to else None,
		"available": available_count(item),
		"units": [{"ref": f"{ref}.{i}", "price": flt(g["sell_price"])} for i, g in enumerate(guns, 1)]}


class Facts:
	"""The facts gathered for one draft, numbered F1, F2 … in the order they were added, one per Item."""

	def __init__(self):
		self.items: list[str] = []
		self.facts: list[dict] = []
		self.kept: list[str] = []   # the item code of each fact, in step with ``facts``

	def add(self, item_code: str) -> bool:
		if item_code in self.items or len(self.facts) >= FACT_LIMIT:
			return False
		self.items.append(item_code)
		found = fact(item_code, f"F{len(self.facts) + 1}")
		if found:
			self.facts.append(found)
			self.kept.append(item_code)
		return bool(found)

	def find_stock(self, asked) -> list[dict]:
		"""What the provider's ``find_stock`` returns: the facts of in-stock Items that fit *asked*
		([{"keywords", "budget"}]), added to the draft's facts."""
		if not isinstance(asked, list):
			return []
		found = [frappe._dict(item=None, keywords=str(w.get("keywords") or "")[:80]) for w in asked[:5]
			if isinstance(w, dict) and w.get("keywords")]
		before = len(self.facts)
		for code in matching_items(found):
			self.add(code)
		return [dict(f) for f in self.facts[before:]]


def by_budget(codes: list[str], budget: float) -> list[str]:
	"""*codes* with what the register prices at or under the budget (10% over at most) first, each group
	nearest the budget first; the order is kept between equals and for an item with no price (last)."""
	from ffl_core import pricing

	def key(code):
		price = flt(pricing.counter_price_for(code))
		return (True, float("inf")) if price <= 0 else (price > budget * 1.1, abs(price - budget))
	return sorted(codes, key=key)


def gather_facts(doc, remembered: list | None = None) -> Facts:
	"""The record's product lines, then what its open wants fit in stock, then what the wants in the
	customer's memory (*remembered*, ``crm.memory.kept_rows``) fit, nearest the remembered budget first.

	lazy: only the memory's wants are put in budget order; keywords are matched as substrings and 12 facts at
	most are kept; upgrade path: a keyword index once a store's stock is large (as ``crm.wants``)."""
	facts = Facts()
	for item, _serial in record_lines(doc):
		facts.add(item)
	found = record_wants(doc)
	asked = [frappe._dict(item=None, keywords=r.keywords) for r in remembered or []
		if r.kind == "want" and wants.keyword_tokens(r.keywords)]
	if not found and not asked:
		return facts
	stock = in_stock_items()
	for code in matching_items(found, stock):
		facts.add(code)
	# Enough candidates to fill the facts, priced only when ordered by the budget.
	codes = matching_items(asked, stock)[:BUDGET_CANDIDATES] if asked else []
	budget = next((flt(r.amount) for r in remembered or [] if r.kind == "budget" and flt(r.amount) > 0), 0)
	for code in by_budget(codes, budget) if budget else codes:
		facts.add(code)
	return facts


def context(doc, latest, messages: list, facts: Facts, channel: str, memory: dict | None = None) -> dict:
	"""What ``gunshop_crm_reply_draft`` is given (docs/extensions/hooks.md)."""
	return {
		"channel": channel,
		"store_name": _store_name(),
		"latest": {"subject": _clip(latest.subject, 500), "body": _clip(_body(latest), LATEST_LIMIT), "at": str(latest.at)},
		"thread": _thread(messages, latest),
		"customer": _customer(doc),
		"memory": memory or {"summary": "", "items": []},
		"facts": [dict(f) for f in facts.facts],
		"find_stock": facts.find_stock,
	}


# ── providers ──

def ready() -> bool:
	"""Whether any provider has drafting on (no network call)."""
	if not installed():
		return False
	for hook in frappe.get_hooks(READY_HOOK):
		try:
			if frappe.get_attr(hook)():
				return True
		except Exception:
			frappe.log_error(title=f"{READY_HOOK} provider failed: {hook}")
	return False


def _ask(ctx: dict) -> dict:
	"""The first provider's answer that has a body or a refusal; a refusal is raised as it was given."""
	for hook in frappe.get_hooks(DRAFT_HOOK):
		try:
			answer = frappe.get_attr(hook)(ctx)
		except Exception:
			frappe.log_error(title=f"{DRAFT_HOOK} provider failed: {hook}")
			continue
		if not isinstance(answer, dict):
			continue
		if answer.get("refused"):
			frappe.throw(str(answer["refused"]), title=_("No draft"))
		if isinstance(answer.get("body"), str) and answer["body"].strip():
			return answer
	frappe.throw(_(FAILED), title=_("No draft"))


def to_html(text: str) -> str:
	"""Plain text as paragraphs: escaped, a blank line between paragraphs, a line break within one."""
	paragraphs = [p.strip() for p in _PARAGRAPHS.split((text or "").replace("\r\n", "\n")) if p.strip()]
	return "".join("<p>" + escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def signature_html() -> str:
	"""The user's signature as CRM's composer adds it (``crm.api.get_user_signature``)."""
	try:
		from crm.api import get_user_signature

		return (get_user_signature() or "").replace("\n", "<br>")
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Signature not read for a draft reply")
		return ""


def prepare(doc, quote_text: str = "") -> frappe._dict:
	"""What a draft of the reply on *doc* is written from, as the current user would have it: ``latest``
	(the message answered), ``native`` (the draft can go into CRM's composer), ``envelope``, ``context``
	(what the provider is given) and ``facts``."""
	latest, native = target(doc, quote_text)
	env = envelope(doc, latest)
	remembered = memory.kept_rows(doc)
	facts = gather_facts(doc, remembered)
	ctx = context(doc, latest, _messages(doc), facts, env["channel"], memory.draft_context(doc, remembered))
	return frappe._dict(latest=latest, native=native, envelope=env, context=ctx, facts=facts)


def draft(doctype: str, name: str, quote_text: str = "") -> dict:
	"""The Draft reply button: a draft of the reply, and where it goes. The one written when the mail
	arrived (``crm.arrival_draft``) when it still holds for this user, else the provider's now."""
	from ffl_core.crm import arrival_draft

	doc = record(doctype, name, "write")
	if not ready():
		frappe.throw(_(FAILED), title=_("No draft"))
	prepared = prepare(doc, str(quote_text or "")[:QUOTE_LIMIT])
	written = arrival_draft.stored(doc, prepared)
	if not written:
		answer = _ask(prepared.context)
		handoff = answer.get("handoff")
		written = {"content_html": to_html(answer["body"]), "handoff": str(handoff) if handoff else None}
	return {"native_ok": 1 if prepared.native else 0, **prepared.envelope, **written,
		"signature_html": signature_html()}


def buttons(doctype: str, name: str) -> dict:
	"""{"ready", "insert_price", "draft_ready"}: which of Draft reply and Insert price the page shows, and
	whether Draft reply has a draft waiting (``crm.arrival_draft.ready_for``)."""
	from ffl_core.crm import arrival_draft, pos_link_on

	doc = record(doctype, name, "read")
	if doctype == DEAL and cint(doc.get(sell_requests.SELL)):
		return {"ready": False, "insert_price": False, "draft_ready": False}   # a gun offered to the store: nothing to sell
	shown = ready()
	return {"ready": shown, "insert_price": pos_link_on(), "draft_ready": shown and arrival_draft.ready_for(doc)}
