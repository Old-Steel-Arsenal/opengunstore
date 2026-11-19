"""Customer memory on CRM: what a customer has told the store, kept for the next reply, and a one-line
summary of each lead and deal.

The core writes no memory by itself. An extension registers two hooks (docs/extensions/hooks.md):
``gunshop_crm_memory_ready`` (is memory on) and ``gunshop_crm_memory_update`` (read the new messages,
answer what to add, change and remove). This module decides when to ask, builds what the provider is
given, and checks and writes the answer. Without a provider nothing here runs and the fields stay empty.

Storage: one CRM Customer Memory per person, found by its Customer, else its Contact, else the CRM Lead
it began on (:func:`memory_for`). Two found for the same person are merged into one on the next write,
and when a lead becomes a deal, contacts or customers are merged (:func:`deal_inserted`,
:func:`contact_renamed`, :func:`customer_renamed`). Each lead and deal keeps its own summary
(``ffl_summary``) and its own watermark of the customer messages already read (``ffl_memory_seen``).

When: a customer's mail filed on a lead or deal (``crm.mail.route``), a website inquiry note
(``crm.inquiries``) and a deal turning Won mark the record due (:func:`mark_due`, one UPDATE that never
fails the mail or the save). Every five minutes :func:`sweep` queues the records that have been quiet
for three minutes; several mails in a row are one call. :func:`run` reads the messages past the
watermark, blanks what must not leave (``crm.sensitive``), asks the providers, and writes the answer
under one lock. A due that moved while it ran is left for the next round, so no mail is lost.

Rows staff wrote or changed are locked: the provider may neither change nor remove them.

On the page: the Lead and Deal side panels' Memory card (:func:`card`), each customer row linking the
message it came from (:func:`source`), and Edit / Update / Clear memory (:func:`save`, :func:`update_now`,
:func:`clear`); the endpoints in ``api.crm`` check the caller's rights on the record. A draft reply is told
the memory too (:func:`kept_rows`, :func:`draft_context`).
"""
from __future__ import annotations

import copy
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.core.utils import html_to_plain_text
from frappe.utils import add_to_date, cint, escape_html, flt, fmt_money, get_datetime, getdate, now_datetime, nowdate
from frappe.utils.synchronization import filelock

from ffl_core.crm import TITLE, installed, sensitive
from ffl_core.crm.deals import DEAL, STATUS, is_closed
from ffl_core.crm.mail import LEAD, OTHER_MAIL, is_automated
from ffl_core.utils import commit

MEMORY = "CRM Customer Memory"
READY_HOOK = "gunshop_crm_memory_ready"
UPDATE_HOOK = "gunshop_crm_memory_update"
RUN_JOB = "ffl_core.crm.memory.run"
NOTE = "FCRM Note"
# On CRM Lead and CRM Deal (crm.setup.CUSTOM_FIELDS). CRM copies a lead's onto the deal it becomes.
SUMMARY = "ffl_summary"
DUE = "ffl_memory_due"     # when the record was last marked for an update; cleared once read
SEEN = "ffl_memory_seen"   # the creation of the newest customer message already read
WON = "ffl_memory_won"     # CRM Deal only: Won since the last update, tell the provider what was bought
FAILING = "ffl_memory_failing"   # when the current batch first got no answer; cleared once it is read or skipped
DOCTYPES = (LEAD, DEAL)
WANTED_FLAG = "ffl_crm_memory_wanted"   # frappe.local.flags: the ready answer, once per request or job
BACKFILLED = "ffl_crm_memory_backfilled_on"

QUIET = timedelta(minutes=3)       # a record is read once it has had no new message for this long
# lazy: 10 records per five-minute sweep (a 100-record backfill takes ~50 minutes); upgrade path: a larger
# batch, or a sweep that runs until the queue is empty, if stores ever need it faster.
BATCH = 10
RETRY = timedelta(hours=1)         # no answer, or the answer could not be written: tried again after this
GIVE_UP = timedelta(hours=6)       # still failing this long after the first failure: the batch is skipped
# lazy: 5 messages per update, oldest unread first; a longer backlog is read 5 a round, one sweep apart;
# upgrade path: a larger batch once the provider's context allows it.
NEW_COUNT, NEW_BODY, SUBJECT_LIMIT = 5, 2000, 500
THREAD_COUNT, THREAD_BODY = 4, 1000
PRODUCT_COUNT = 10
LIMITS = {"want": 6, "budget": 1, "state": 1, "trade_in": 3, "preference": 4, "question": 5}
SINGLE = ("budget", "state")
TOTAL = 20
TEXT_LIMIT, KEYWORDS_LIMIT, SUMMARY_LIMIT = 160, 80, 300
# lazy: one lock for the whole site serializes every memory write (a run's write, a merge); upgrade path: a
# lock per person, once many runs wait on it.
LOCK = "ffl_crm_memory"
_SAVEPOINT = "ffl_crm_memory"
# The Lead and Deal pages' Memory card (crm.setup puts the field on both, the section in their side panels).
CARD_FIELD = "ffl_memory_card"
SECTION = "ffl_memory_section"
LABELS = {"want": "Wants", "budget": "Budget", "state": "State", "trade_in": "Trade-in", "preference": "Prefers",
	"question": "Open"}
LINES = ("want", "trade_in", "preference", "question")   # Edit memory: one row a line
COMMUNICATION = "Communication"
SOURCE_TEXT = 2000
UPDATE_FAILED = "Couldn't update memory."


def _log():
	return frappe.logger("ffl_crm_memory")


def table_ready() -> bool:
	"""Whether this site has the memory table and fields (False for a moment on the deploy that brings them)."""
	return installed() and frappe.db.table_exists(MEMORY) and frappe.db.has_column(DEAL, DUE) \
		and frappe.db.has_column(LEAD, DUE)


# ── providers ──

def memory_wanted() -> bool:
	"""Whether any provider has memory on (no network call). Asked once per request or job (a mail pull
	files many mails): a failing provider is logged once, not once per mail."""
	if not installed():
		return False
	hooks = tuple(frappe.get_hooks(READY_HOOK))
	if not hooks:
		return False
	cached = frappe.local.flags.get(WANTED_FLAG) or {}
	if hooks in cached:
		return cached[hooks]
	answer = False
	for hook in hooks:
		try:
			if frappe.get_attr(hook)():
				answer = True
				break
		except Exception:
			frappe.log_error(title=f"{READY_HOOK} provider failed: {hook}")
	frappe.local.flags[WANTED_FLAG] = {**cached, hooks: answer}
	return answer


def _ask(ctx: dict) -> dict | None:
	"""The first provider's answer (a refusal included); None when none answers."""
	for hook in frappe.get_hooks(UPDATE_HOOK):
		try:
			answer = frappe.get_attr(hook)(copy.deepcopy(ctx))
		except Exception:
			frappe.log_error(title=f"{UPDATE_HOOK} provider failed: {hook}")
			continue
		if isinstance(answer, dict):
			return answer
	return None


def refresh_layout() -> bool:
	"""Called by a provider when its ready answer changes, and at every migrate. The Memory section of the
	Lead and Deal side panels is there while a provider has memory on or any memory or summary is kept (a
	store that switched it off still sees and edits what it has), and gone otherwise. The section's field is
	hidden until the page script finds the record has a card to show. Returns whether a layout changed."""
	frappe.local.flags.pop(WANTED_FLAG, None)   # the provider's answer just changed
	if not table_ready():
		return False
	on = memory_wanted() or _any_kept()
	changed = False
	for doctype in DOCTYPES:
		changed = _place_section(f"{doctype}-Side Panel", on) or changed
	return changed


def _any_kept() -> bool:
	return bool(frappe.db.exists(f"{MEMORY} Item", {"parenttype": MEMORY})
		or any(frappe.db.has_column(dt, SUMMARY) and frappe.db.exists(dt, {SUMMARY: ["is", "set"]}) for dt in DOCTYPES))


def _place_section(layout_name: str, on: bool) -> bool:
	"""The Memory section in one side panel, below the contacts and the POS's own cards (crm.setup)."""
	from ffl_core.crm.setup import CARD_SECTION, SELL_SECTION

	if not frappe.db.exists("CRM Fields Layout", layout_name):
		return False
	layout = frappe.get_doc("CRM Fields Layout", layout_name)
	sections = json.loads(layout.layout or "[]")
	present = any(s.get("name") == SECTION for s in sections)
	if on == present:
		return False
	if on:
		above = ("contacts_section", CARD_SECTION, SELL_SECTION)
		at = 0
		while at < len(sections) and sections[at].get("name") in above:
			at += 1
		sections.insert(at, {"name": SECTION, "label": "Memory", "opened": True,
			"columns": [{"name": "ffl_memory_column", "fields": [CARD_FIELD]}]})
	else:
		sections = [s for s in sections if s.get("name") != SECTION]
	layout.layout = json.dumps(sections)
	layout.save(ignore_permissions=True)   # the CRM page layout, written by migrate or the provider's settings
	return True


# ── marking due ──

def _deal_of_lead(lead: str) -> str | None:
	return frappe.db.get_value(DEAL, {"lead": lead}, "name")


def mark_due(doctype: str, name: str, won: bool = False) -> dict | None:
	"""Mark a lead or deal for a memory update: one UPDATE, inside the caller's transaction (a mail pull,
	an invoice), so it never raises; a failure is rolled back to its own savepoint and logged. A lead that
	became a deal marks the deal, whose timeline holds the lead's messages. Returns the values written."""
	if doctype not in DOCTYPES or not name:
		return None
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if not table_ready() or not memory_wanted():
			return None
		if doctype == LEAD:
			lead = frappe.db.get_value(LEAD, name, ["status", "converted"], as_dict=True)
			if not lead or lead.status == OTHER_MAIL:
				return None
			deal = lead.converted and _deal_of_lead(name)
			if deal:
				doctype, name = DEAL, deal
		values = {DUE: now_datetime()}
		if won and doctype == DEAL:
			values[WON] = 1
		frappe.db.set_value(doctype, name, values, update_modified=False)
		return values
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Memory update not marked: {doctype} {name}", message=frappe.get_traceback())
		return None


def mail_routed(doc, target) -> None:
	"""``crm.mail.route``: a customer's received mail just went on *target* (doctype, name)."""
	if not target or target[0] not in DOCTYPES:
		return
	if doc.get("sent_or_received") != "Received" or doc.get("communication_type") != "Communication":
		return
	from ffl_core.crm.reply_draft import _address

	if is_automated(_address(doc.get("sender"))):
		return
	mark_due(*target)


def deal_updated(doc, method=None) -> None:
	"""CRM Deal on_update: a deal that just turned Won tells the next update what was bought."""
	if not installed() or not doc.has_value_changed("status"):
		return
	if frappe.db.get_value(STATUS, doc.status, "type") != "Won":
		return
	values = mark_due(DEAL, doc.name, won=True)
	if values:
		doc.update(values)   # a later save of this same object must not write the old values back


# ── the sweep and the run ──

def sweep() -> list[tuple[str, str]]:
	"""Every five minutes: queue a run for each record that has been quiet for three minutes, oldest due
	first, ten at most. Returns what was queued."""
	if not table_ready() or not memory_wanted():
		return []
	cutoff = now_datetime() - QUIET
	rows = []
	for doctype in DOCTYPES:
		rows += [(get_datetime(due), doctype, name) for name, due in frappe.get_all(doctype,
			filters={DUE: ["<=", cutoff]}, fields=["name", DUE], order_by=f"{DUE} asc", limit=BATCH, as_list=True)]
	rows.sort()
	queued = []
	for _due, doctype, name in rows[:BATCH]:
		frappe.enqueue(RUN_JOB, queue="long", timeout=300, enqueue_after_commit=True,
			job_id=f"ffl_crm_memory:{doctype}:{name}", deduplicate=True, doctype=doctype, name=name)
		queued.append((doctype, name))
	return queued


def _clear(doctype: str, name: str, due0, won: bool = False, more: bool = False) -> None:
	"""Clear the due mark, only if it is still the one read when the run began: a message that came in
	meanwhile moved it, and stays for the next round. *more*: unread messages are left (a backlog), so the
	record is due again at the next sweep. The failure mark goes too."""
	if more:
		frappe.db.set_value(doctype, name, DUE, now_datetime() - QUIET, update_modified=False)
	elif due0 is not None:
		frappe.db.sql(f"UPDATE `tab{doctype}` SET `{DUE}` = NULL WHERE name = %s AND `{DUE}` = %s", (name, due0))
	values = {FAILING: None} if frappe.db.has_column(doctype, FAILING) else {}
	if won and doctype == DEAL:
		values[WON] = 0
	if values:
		frappe.db.set_value(doctype, name, values, update_modified=False)


def _failed(doctype: str, name: str, due0, batch: "Batch", refused: bool = False) -> dict:
	"""No usable answer for *batch* in the background: tried again in an hour, counted from the first
	failure; once that is :data:`GIVE_UP` old, or the provider refused, the batch is skipped (watermark
	advanced past it, one log line without content) so it is never asked about, and paid for, without end."""
	first = frappe.db.get_value(doctype, name, FAILING)
	now = now_datetime()
	if refused or (first and get_datetime(first) <= now - GIVE_UP):
		_advance(doctype, name, batch.seen)
		_clear(doctype, name, due0, won=batch.bought, more=batch.more)
		_log().info(f"memory update skipped: {doctype} {name}, {len(batch.sources)} message(s)"
			+ (", won" if batch.bought else "") + (" (refused)" if refused else ""))
		commit()
		return {"outcome": "skipped"}
	if not first:
		frappe.db.set_value(doctype, name, FAILING, now, update_modified=False)
	frappe.db.sql(f"UPDATE `tab{doctype}` SET `{DUE}` = %s WHERE name = %s AND `{DUE}` <=> %s",
		(now + RETRY, name, due0))
	commit()
	return {"outcome": "retry"}


def _advance(doctype: str, name: str, seen) -> None:
	if not seen:
		return
	current = frappe.db.get_value(doctype, name, SEEN)
	if not current or get_datetime(seen) > get_datetime(current):
		frappe.db.set_value(doctype, name, SEEN, seen, update_modified=False)


def _snapshot(memory) -> tuple | None:
	return (memory.name, str(memory.modified)) if memory else None


def run(doctype: str, name: str, manual: bool = False) -> dict:
	"""Update the memory from a record's new customer messages (the queued job; *manual*: the employee's
	**Update memory**, which reads the latest messages again when nothing is unread, and raises a failed
	write instead of retrying). Returns
	``{"outcome": str, "refused": str | None}``; *outcome* is one of gone, off, moved, paused, nothing,
	no_answer, retry, skipped, discarded, updated."""
	if doctype not in DOCTYPES or not table_ready() or not frappe.db.exists(doctype, name):
		return {"outcome": "gone"}
	due0 = frappe.db.get_value(doctype, name, DUE)
	if not memory_wanted():
		_clear(doctype, name, due0)
		return {"outcome": "off"}
	doc = frappe.get_doc(doctype, name)
	if doctype == LEAD and doc.get("converted"):
		deal = _deal_of_lead(name)
		if deal:   # the deal's timeline holds this lead's messages
			if due0 and not frappe.db.get_value(DEAL, deal, DUE):
				frappe.db.set_value(DEAL, deal, DUE, due0, update_modified=False)
			_clear(doctype, name, due0)
			return run(DEAL, deal, manual) if manual else {"outcome": "moved"}
	memory = memory_for(doc)
	before = _snapshot(memory)
	if memory and not cint(memory.auto_update):
		# Off for this person: what came meanwhile is never read later either.
		_advance(doctype, name, max((m.creation for m in _customer_messages(doc)), default=None))
		_clear(doctype, name, due0, won=True)
		return {"outcome": "paused"}
	batch = batch_for(doc, memory, "manual" if manual else None, everything=manual)
	if batch is None:
		_clear(doctype, name, due0, won=True)
		return {"outcome": "nothing"}
	commit()   # only reads so far; a provider's spending reservation commits on its own
	answer = _ask(batch.context)
	refused = answer.get("refused") if isinstance(answer, dict) else None
	if refused or answer is None:
		if manual:
			return {"outcome": "no_answer", "refused": str(refused) if refused else None}
		return _failed(doctype, name, due0, batch, refused=bool(refused))
	commit()   # a fresh snapshot: an edit made while the provider worked is visible
	frappe.db.savepoint("ffl_crm_memory_write")
	try:
		with filelock(LOCK, timeout=60):
			doc = frappe.get_doc(doctype, name)
			if _snapshot(memory_for(doc)) != before:
				# Staff (or another record's run) changed this memory meanwhile: read again next round.
				return {"outcome": "discarded"}
			memory = memory_for(doc, write=True)
			counts = apply(doc, memory, batch, answer)
			_advance(doctype, name, batch.seen)
			_clear(doctype, name, due0, won=batch.bought, more=batch.more)
			commit()
	except Exception:
		# The answer is paid for but could not be written (a lock, a bad row, the database): never rerun
		# every five minutes; a retry like any other failure, skipped after GIVE_UP.
		frappe.db.rollback(save_point="ffl_crm_memory_write")
		frappe.log_error(title=f"[{TITLE}] Customer memory not written: {doctype} {name}", message=frappe.get_traceback())
		if manual:
			raise
		return _failed(doctype, name, due0, batch)
	if counts.get("dropped"):
		_log().info(f"memory update: {doctype} {name}, {counts['dropped']} entr(ies) dropped")
	return {"outcome": "updated"}


# ── what the provider is given ──

class Batch:
	"""One update: the ``context`` the provider gets, and what its refs stand for (never sent)."""

	def __init__(self):
		self.context: dict = {}
		self.sources: dict[str, frappe._dict] = {}   # E1 → the message
		self.rows: dict[str, str] = {}               # M1 → the memory row's name
		self.seen = None                             # the newest creation among the new messages
		self.more = False                            # unread messages left for the next round
		self.bought = False


def _customer_messages(doc) -> list:
	"""The record's customer messages, newest first: received mail (not from a machine) and website
	inquiry notes, each with ``creation``."""
	from ffl_core.crm.reply_draft import _address, _messages

	out = []
	for m in _messages(doc):
		if m.kind == "in" and not is_automated(_address(m.sender)):
			out.append(m)
		elif m.kind == "note":
			out.append(m)
	return out


def _bought(deal) -> dict | None:
	"""What a won deal sold, without prices or serial numbers: its invoices' lines, else the web order
	that won it, else its own product lines."""
	from ffl_core.crm.orders import DEAL_FIELD
	from ffl_core.crm.web_orders import ORDER, WON_BY

	lines: dict[str, float] = {}

	def add(name, qty):
		if name:
			lines[name] = lines.get(name, 0) + flt(qty)

	invoices = frappe.db.has_column("Sales Invoice", DEAL_FIELD) and frappe.get_all("Sales Invoice",
		filters={DEAL_FIELD: deal.name, "docstatus": 1, "is_return": 0}, pluck="name")
	if invoices:
		for r in frappe.get_all("Sales Invoice Item", filters={"parent": ["in", invoices], "parenttype": "Sales Invoice"},
				fields=["item_name", "item_code", "qty"], order_by="idx asc"):
			add(r.item_name or r.item_code, r.qty)
	elif deal.get(WON_BY) and frappe.db.exists(ORDER, deal.get(WON_BY)):
		for r in frappe.get_all("Woo Online Order Line", filters={"parent": deal.get(WON_BY), "parenttype": ORDER},
				fields=["item_code", "qty"], order_by="idx asc"):
			add(frappe.db.get_value("Item", r.item_code, "item_name") or r.item_code, r.qty)
	else:
		for r in deal.get("products") or []:
			add(r.product_name or r.product_code, r.qty)
	if not lines:
		return None
	return {"ref": "O1", "lines": [{"name": n, "qty": q} for n, q in list(lines.items())[:PRODUCT_COUNT]]}


def _closed(doc) -> str | None:
	if doc.doctype == DEAL:
		kind = frappe.db.get_value(STATUS, doc.status, "type")
	else:
		kind = frappe.db.get_value("CRM Lead Status", doc.status, "type")
	return {"Won": "won", "Lost": "lost"}.get(kind)


def _record(doc) -> dict:
	from ffl_core.crm import sell_requests
	from ffl_core.crm.reply_draft import record_wants

	wanted = []
	for w in record_wants(doc):
		text = w.keywords or (w.item and frappe.db.get_value("Item", w.item, "item_name")) or ""
		if text and text not in wanted:
			wanted.append(text)
	products = []
	for r in doc.get("products") or []:
		text = r.product_name or r.product_code
		if text and text not in products:
			products.append(text)
	return {"type": "deal" if doc.doctype == DEAL else "lead", "stage": doc.get("status") or "",
		"closed": _closed(doc), "sell_request": bool(cint(doc.get(sell_requests.SELL))),
		"products": products[:PRODUCT_COUNT], "wants": wanted[:PRODUCT_COUNT]}


def _clip(text: str | None, limit: int) -> str:
	return sensitive.redact((text or "").strip())[:limit]


def batch_for(doc, memory=None, trigger: str | None = None, everything: bool = False) -> Batch | None:
	"""What the provider is asked about this record: the oldest five customer messages past its watermark
	(oldest first; the rest are left for the next round), and what a won deal bought. With nothing unread,
	*everything* (a manual update) reads the latest five again. None when there is nothing to ask. Every
	message is blanked by ``crm.sensitive.redact``, and a locked row that breaks a rule written since is
	not sent. Also the contract an extension's tests build its context from.

	lazy: the watermark is a creation time, so a mail filed on the record later than newer ones were read
	(an old mail made a lead by hand) is never read; upgrade path: keep the names of the messages read."""
	from ffl_core.crm.reply_draft import _body, _store_name

	messages = sorted(_customer_messages(doc), key=lambda m: (get_datetime(m.creation), m.name))
	seen = doc.get(SEEN)
	unread = [m for m in messages if not seen or get_datetime(m.creation) > get_datetime(seen)]
	new = unread[:NEW_COUNT] if unread else (messages[-NEW_COUNT:] if everything else [])
	bought = _bought(doc) if doc.doctype == DEAL and cint(doc.get(WON)) else None
	if not new and not bought:
		return None
	batch = Batch()
	batch.bought = bool(cint(doc.get(WON))) if doc.doctype == DEAL else False
	batch.more = len(unread) > NEW_COUNT
	if new:
		batch.seen = max(m.creation for m in new)
	from ffl_core.crm.reply_draft import _messages

	timeline = _messages(doc)
	thread = []
	if new:
		first = new[0]
		older = [m for m in timeline if (m.at, m.name) < (first.at, first.name)]
		for m in older[:THREAD_COUNT]:
			body = _clip(_body(m), THREAD_BODY)
			if body:
				thread.append({"direction": "out" if m.kind == "out" else "in", "at": str(m.at), "body": body})
	entries = []
	for i, m in enumerate(new, 1):
		ref = f"E{i}"
		batch.sources[ref] = m
		entries.append({"ref": ref, "kind": "mail" if m.kind == "in" else "inquiry", "at": str(m.at),
			"subject": _clip(m.subject, SUBJECT_LIMIT), "body": _clip(_body(m), NEW_BODY)})
	rows = []
	for i, row in enumerate(_sendable(memory), 1):
		ref = f"M{i}"
		batch.rows[ref] = row.name
		rows.append({"ref": ref, "kind": row.kind, "text": row.text or "", "keywords": row.keywords or "",
			"amount": flt(row.amount) or None, "said": (row.said or "Customer").lower(),
			"on": str(row.noted_on) if row.noted_on else None, "locked": bool(cint(row.locked))})
	batch.context = {
		"trigger": trigger or ("mail" if new else "won"),
		"store_name": _store_name(),
		"record": _record(doc),
		"summary": doc.get(SUMMARY) or "",
		"memory": rows,
		"new": entries,
		"thread": thread,
		"bought": bought,
	}
	return batch


def _sendable(memory) -> list:
	"""The rows a provider may see: a locked row that breaks a rule written since it was kept is not."""
	return [r for r in ((memory.get("items") if memory else None) or [])
		if not any(sensitive.has_sensitive(v) or sensitive.has_price_or_stock(v) for v in (r.text, r.keywords))]


# ── one memory per person ──

def _keys(doc) -> dict:
	"""{"customer", "contact", "lead"} of the person a lead or deal is about."""
	from ffl_core.crm.customer import _primary_contact, linked_customer

	if doc.doctype == LEAD:
		deal = doc.get("converted") and _deal_of_lead(doc.name)
		if not deal:
			return {"customer": None, "contact": None, "lead": doc.name}
		doc = frappe.get_doc(DEAL, deal)
	return {"customer": linked_customer(doc), "contact": _primary_contact(doc), "lead": doc.get("lead") or None}


def _found(keys: dict) -> list[str]:
	out = []
	for key in ("customer", "contact", "lead"):
		if keys.get(key):
			for name in frappe.get_all(MEMORY, filters={key: keys[key]}, pluck="name", order_by="creation asc"):
				if name not in out:
					out.append(name)
	return out


def memory_for(doc, write: bool = False, create: bool = True):
	"""The person's CRM Customer Memory, or None. Read: the first found by Customer, Contact, Lead.
	*write* (under :data:`LOCK`): every one found is merged into the first, the keys it lacks are filled
	in, and one is made when none exists (unless not *create*)."""
	keys = _keys(doc)
	found = _found(keys)
	if not write:
		return frappe.get_doc(MEMORY, found[0]) if found else None
	if not found:
		if not create:
			return None
		memory = frappe.get_doc({"doctype": MEMORY, **keys})
		memory.flags.ignore_permissions = True
		return memory.insert()
	memory = frappe.get_doc(MEMORY, found[0])
	for name in found[1:]:
		_merge(frappe.get_doc(MEMORY, name), memory)
	if any(keys[k] and not memory.get(k) for k in keys):
		for k, v in keys.items():
			if v and not memory.get(k):
				memory.set(k, v)
		_save(memory)
	return memory


def _norm(text: str | None) -> str:
	return " ".join((text or "").lower().split())


def _newer(a, b) -> bool:
	"""Row *a* was said after row *b*."""
	return getdate(a.noted_on or "1900-01-01") > getdate(b.noted_on or "1900-01-01")


_COPIED = ("kind", "text", "keywords", "amount", "said", "source_doctype", "source_name", "noted_on", "locked")


def _copy_row(source, target) -> None:
	for f in _COPIED:
		target.set(f, source.get(f))


def _merge(source, target) -> None:
	"""*source* into *target* (no lock taken here): rows united, the same thing said twice kept once
	(the later), a single-valued kind settled for the locked row, else the later one; ``auto_update``
	off if either is off; keys filled in. *source* is deleted."""
	for row in source.get("items") or []:
		same = next((r for r in target.items if r.kind == row.kind and _norm(r.text) == _norm(row.text)), None)
		if same:   # said twice: one row, the later source, locked if either was
			locked = max(cint(same.locked), cint(row.locked))
			if _newer(row, same):
				_copy_row(row, same)
			same.locked = locked
			continue
		other = row.kind in SINGLE and next((r for r in target.items if r.kind == row.kind), None)
		if other:   # one budget, one state: the locked one, else the later one
			if (cint(row.locked), getdate(row.noted_on or "1900-01-01")) > \
					(cint(other.locked), getdate(other.noted_on or "1900-01-01")):
				_copy_row(row, other)
			continue
		target.append("items", {f: row.get(f) for f in _COPIED})
	target.auto_update = 1 if cint(target.auto_update) and cint(source.auto_update) else 0
	for key in ("customer", "contact", "lead"):
		if source.get(key) and not target.get(key):
			target.set(key, source.get(key))
	_trim(target)
	_save(target)
	frappe.delete_doc(MEMORY, source.name, ignore_permissions=True, force=True)


def _trim(memory) -> list:
	"""Each kind to its limit and all to :data:`TOTAL`, the oldest unlocked rows going first. Returns
	the rows taken out."""
	gone = []

	def drop(rows: list) -> bool:
		unlocked = sorted((r for r in rows if not cint(r.locked)), key=lambda r: (getdate(r.noted_on or "1900-01-01"), r.idx))
		if not unlocked:
			return False
		memory.remove(unlocked[0])
		gone.append(unlocked[0])
		return True

	for kind, limit in LIMITS.items():
		while len([r for r in memory.items if r.kind == kind]) > limit:
			if not drop([r for r in memory.items if r.kind == kind]):
				break
	while len(memory.items) > TOTAL and drop(memory.items):
		pass
	for i, row in enumerate(memory.items, 1):
		row.idx = i
	return gone


def _save(memory) -> None:
	memory.flags.ignore_permissions = True
	memory.flags.ffl_written_by_core = True   # validate drops a bad unlocked row instead of refusing
	memory.save()


def _guarded(what: str, fn, *args) -> None:
	"""A merge hook: never fails the save, rename or delete it rides on."""
	if not table_ready():
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		with filelock(LOCK, timeout=10):
			fn(*args)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Customer memory: {what} failed", message=frappe.get_traceback())


def deal_inserted(doc, method=None) -> None:
	"""CRM Deal after_insert: a deal converted from a lead takes the lead's memory, with the deal's
	contact and customer as keys; one the contact already had is merged in."""
	if doc.get("lead"):
		_guarded(f"lead {doc.lead} to deal {doc.name}", lambda: memory_for(doc, write=True, create=False))


def _merge_key(key: str, value: str) -> None:
	names = frappe.get_all(MEMORY, filters={key: value}, pluck="name", order_by="creation asc")
	if len(names) > 1:
		target = frappe.get_doc(MEMORY, names[0])
		for name in names[1:]:
			_merge(frappe.get_doc(MEMORY, name), target)


def contact_renamed(doc, method=None, old: str | None = None, new: str | None = None, merge: bool = False) -> None:
	"""Contact after_rename: two contacts merged into one leave one memory."""
	if merge and new:
		_guarded(f"contact merge into {new}", _merge_key, "contact", new)


def customer_renamed(doc, method=None, old: str | None = None, new: str | None = None, merge: bool = False) -> None:
	"""Customer after_rename (the POS's Merge customer ends here): one memory for the customer kept."""
	if merge and new:
		_guarded(f"customer merge into {new}", _merge_key, "customer", new)


def _drop_key(key: str, value: str) -> None:
	for name in frappe.get_all(MEMORY, filters={key: value}, pluck="name"):
		keys = frappe.db.get_value(MEMORY, name, ["customer", "contact", "lead"], as_dict=True)
		keys[key] = None
		if any(keys.values()):
			frappe.db.set_value(MEMORY, name, key, None, update_modified=False)
		else:
			frappe.delete_doc(MEMORY, name, ignore_permissions=True, force=True)


def lead_trashed(doc, method=None) -> None:
	"""CRM Lead on_trash: its memory loses that key, and goes when it has no other."""
	_guarded(f"lead {doc.name} deleted", _drop_key, "lead", doc.name)


def contact_trashed(doc, method=None) -> None:
	_guarded(f"contact {doc.name} deleted", _drop_key, "contact", doc.name)


def customer_trashed(doc, method=None) -> None:
	_guarded(f"customer {doc.name} deleted", _drop_key, "customer", doc.name)


# ── writing the answer ──

def _clean(kind: str, text, keywords, amount) -> tuple | None:
	"""A row's (text, keywords, amount) as stored, or None when it must not be (§ ``crm.sensitive``)."""
	from ffl_core.compliance.interstate_tax import normalize_us_state
	from ffl_core.crm.wants import keyword_tokens

	if kind not in LIMITS:
		return None
	text = " ".join(str(text or "").split())[:TEXT_LIMIT]
	keywords = " ".join(str(keywords or "").split())[:KEYWORDS_LIMIT] if kind == "want" else ""
	if kind == "budget":
		try:
			amount = float(amount)
		except (TypeError, ValueError):
			return None
		if not 0 < amount <= sensitive.MAX_AMOUNT:
			return None
	else:
		amount = None
	if kind == "state":
		text = normalize_us_state(text) or ""
	if kind != "budget" and not text:
		return None
	if kind == "want" and not keyword_tokens(keywords):
		return None
	if sensitive.row_problem(kind, text, keywords):
		return None
	return text, keywords, amount


def apply(doc, memory, batch: Batch, answer: dict) -> dict:
	"""Write a provider's *answer* for *batch* onto *memory* and the record *doc*'s summary. Every entry
	that breaks a rule is dropped on its own: a ref that is not a row or is a locked one, a source that
	is not one of the batch's messages, an unknown kind, a state that is not one, a budget without an
	amount, a want without keywords, ID numbers, eligibility details, prices or stock. Returns counts."""
	counts = {"added": 0, "updated": 0, "removed": 0, "dropped": 0}
	rows = {ref: next((r for r in memory.items if r.name == n), None) for ref, n in batch.rows.items()}
	answer = answer if isinstance(answer, dict) else {}

	def listed(key):
		value = answer.get(key)
		return value if isinstance(value, list) else []

	for ref in listed("remove"):
		row = rows.get(ref) if isinstance(ref, str) else None
		if row is None or cint(row.locked) or row not in memory.items:
			counts["dropped"] += 1
			continue
		memory.remove(row)
		counts["removed"] += 1

	def source_of(entry):
		m = batch.sources.get(entry.get("source"))
		if not m:
			return None
		return {"said": "Customer", "source_doctype": "Communication" if m.kind == "in" else NOTE,
			"source_name": m.name, "noted_on": getdate(m.at), "locked": 0}

	for entry in listed("update"):
		row = rows.get(entry.get("ref")) if isinstance(entry, dict) else None
		source = row is not None and source_of(entry)
		if not source or cint(row.locked) or row not in memory.items:
			counts["dropped"] += 1
			continue
		cleaned = _clean(row.kind, entry.get("text") if entry.get("text") is not None else row.text,
			entry.get("keywords") if entry.get("keywords") is not None else row.keywords,
			entry.get("amount") if entry.get("amount") is not None else row.amount)
		if not cleaned:
			counts["dropped"] += 1
			continue
		row.update({"text": cleaned[0], "keywords": cleaned[1], "amount": cleaned[2], **source})
		counts["updated"] += 1

	for entry in listed("add"):
		source = isinstance(entry, dict) and source_of(entry)
		kind = entry.get("kind") if isinstance(entry, dict) else None
		cleaned = source and _clean(kind, entry.get("text"), entry.get("keywords"), entry.get("amount"))
		if not cleaned:
			counts["dropped"] += 1
			continue
		text, keywords, amount = cleaned
		same = next((r for r in memory.items if r.kind == kind and _norm(r.text) == _norm(text)
			and (kind != "budget" or flt(r.amount) == flt(amount))), None)
		if same:
			if not cint(same.locked):
				same.update(source)
			counts["updated"] += 1
			continue
		if kind in SINGLE:
			current = [r for r in memory.items if r.kind == kind]
			if any(cint(r.locked) for r in current):
				counts["dropped"] += 1
				continue
			for r in current:
				memory.remove(r)
		row = memory.append("items", {"kind": kind, "text": text, "keywords": keywords, "amount": amount, **source})
		counts["added"] += 1
		if row in _trim(memory):
			counts["added"] -= 1
			counts["dropped"] += 1
	_trim(memory)
	_save(memory)
	counts["dropped"] += cint(memory.flags.get("ffl_rows_dropped"))

	summary = answer.get("summary")
	if isinstance(summary, str):
		summary = " ".join(summary.split())[:SUMMARY_LIMIT]
		if sensitive.has_sensitive(summary) or sensitive.has_price_or_stock(summary):
			counts["dropped"] += 1
		elif frappe.db.has_column(doc.doctype, SUMMARY):
			frappe.db.set_value(doc.doctype, doc.name, SUMMARY, summary, update_modified=False)
	return counts


# ── the card, staff edits and the draft (api.crm.memory_*, crm.reply_draft) ──
# The caller's rights on the lead or deal are checked by the endpoint (crm.reply_draft.record); the memory
# itself has no DocPerm for Sales Users and is read and written here without one.

_MUTED = "color:#6b7280"


def _said_on(day) -> str:
	day = getdate(day)
	text = f"{day:%b} {day.day}"
	return text if day.year == getdate().year else f"{text}, {day.year}"


def _source_record(row) -> tuple[str, str] | None:
	"""The lead or deal the message a row came from is on, or None (no source, or it is gone)."""
	if not row.source_name:
		return None
	if row.source_doctype == COMMUNICATION:
		ref = frappe.db.get_value(COMMUNICATION, row.source_name, ["reference_doctype", "reference_name"])
	elif row.source_doctype == NOTE:
		ref = frappe.db.get_value(NOTE, row.source_name, ["reference_doctype", "reference_docname"])
	else:
		return None
	if not ref or ref[0] not in DOCTYPES or not ref[1] or not frappe.db.exists(ref[0], ref[1]):
		return None
	return ref


def _readable(row) -> bool:
	"""Whether the caller may open the message *row* came from: they may read the record it is on (a
	deal's lead may be one they were not shared)."""
	ref = _source_record(row)
	return bool(ref) and bool(frappe.has_permission(ref[0], "read", doc=ref[1]))


def _said_html(doc, row) -> str:
	"""`` · said Nov 12`` after a row the customer said, a link to that message when the caller may read it."""
	if row.said != "Customer" or not row.noted_on:
		return ""
	label = escape_html(_("said {0}").format(_said_on(row.noted_on)))
	if not _readable(row):
		return f' · <span style="white-space:nowrap">{label}</span>'
	data = " ".join(f'data-ffl-memory-{k}="{escape_html(v)}"' for k, v in (("source", row.name),
		("doctype", doc.doctype), ("name", doc.name)))
	return f' · <a href="#" {data} style="color:inherit;text-decoration:underline;white-space:nowrap">{label}</a>'


def card_html(doc, summary: str, rows: list) -> str:
	"""The Memory card: the record's summary, then one line per row under its kind's label. Every value
	escaped (CRM renders it as HTML)."""
	parts = []
	if summary:
		parts.append(f'<div style="margin-bottom:8px">{escape_html(summary)}</div>')
	lines = []
	for kind, label in LABELS.items():
		for i, row in enumerate(r for r in rows if r.kind == kind):
			text = fmt_money(row.amount, currency=frappe.db.get_default("currency")) if kind == "budget" else row.text
			head = escape_html(_(label)) if i == 0 else ""
			lines.append(f'<div style="{_MUTED}">{head}</div><div>{escape_html(text or "")}{_said_html(doc, row)}</div>')
	if lines:
		parts.append('<div style="display:grid;grid-template-columns:auto 1fr;column-gap:12px;row-gap:4px">'
			+ "".join(lines) + "</div>")
	if not parts:
		parts.append(f'<div style="{_MUTED}">{escape_html(_("Nothing yet."))}</div>')
	return "".join(parts)


def card(doc) -> dict:
	"""What the Lead or Deal page shows: ``{"html", "shown", "ready", "can_edit", "has_content", "values"}``.
	*shown*: memory is on, or this record already has a memory or a summary (switched off, what is kept can
	still be read, edited and cleared). *values*: what Edit memory starts from."""
	ready = table_ready() and memory_wanted()
	found = memory_for(doc) if table_ready() else None
	rows = list(found.items) if found else []
	summary = doc.get(SUMMARY) or ""
	content = bool(rows or summary)
	budget = next((r for r in rows if r.kind == "budget"), None)
	state = next((r for r in rows if r.kind == "state"), None)
	values = {kind: "\n".join(r.text or "" for r in rows if r.kind == kind) for kind in LINES}
	values.update(budget=(flt(budget.amount) or None) if budget else None, state=state.text if state else "",
		auto_update=cint(found.auto_update) if found else 1)
	return {"html": card_html(doc, summary, rows), "shown": bool(ready or content), "ready": bool(ready),
		"can_edit": bool(frappe.has_permission(doc.doctype, "write", doc=doc.name)), "has_content": content,
		"values": values}


def source(doc, row_name: str) -> dict:
	"""The message a row of *doc*'s memory came from: ``{"subject", "at", "text"}`` (plain text, 2000
	characters at most). Refused for a row that is not this memory's, and for a message on a record the
	caller may not read."""
	found = memory_for(doc) if table_ready() else None
	row = found and next((r for r in found.items if r.name == row_name), None)
	ref = row and _source_record(row)
	if not ref:
		frappe.throw(_("That message is no longer on file."), frappe.DoesNotExistError)
	frappe.has_permission(ref[0], "read", doc=ref[1], throw=True)
	if row.source_doctype == COMMUNICATION:
		m = frappe.db.get_value(COMMUNICATION, row.source_name, ["subject", "content", "communication_date"], as_dict=True)
		subject, html, at = m.subject, m.content, m.communication_date
	else:
		m = frappe.db.get_value(NOTE, row.source_name, ["title", "content", "creation"], as_dict=True)
		subject, html, at = m.title, m.content, m.creation
	return {"subject": subject or "", "at": frappe.utils.format_datetime(at) if at else "",
		"text": html_to_plain_text(html or "").strip()[:SOURCE_TEXT]}


def _staff_rows(values: dict) -> list[tuple]:
	"""Edit memory's fields as (kind, text, keywords, amount), in the card's order; a bad value refused."""
	from ffl_core.compliance.interstate_tax import normalize_us_state

	rows = []
	for kind in LABELS:
		if kind == "budget":
			amount = flt(values.get("budget"))
			if amount:
				if not 0 < amount <= sensitive.MAX_AMOUNT:
					frappe.throw(_("Enter a budget above 0."), title=_("Not saved"))
				rows.append(("budget", "", "", amount))
		elif kind == "state":
			raw = " ".join(str(values.get("state") or "").split())
			if raw:
				code = normalize_us_state(raw)
				if not code:
					frappe.throw(_("Enter a US state."), title=_("Not saved"))
				rows.append(("state", code, "", None))
		else:
			seen = []
			for line in str(values.get(kind) or "").splitlines():
				text = " ".join(line.split())[:TEXT_LIMIT]
				if text and _norm(text) not in seen:
					seen.append(_norm(text))
					rows.append((kind, text, text[:KEYWORDS_LIMIT] if kind == "want" else "", None))
			if len(seen) > LIMITS[kind]:
				frappe.throw(_("At most {0} lines in {1}.").format(LIMITS[kind], _(LABELS[kind])), title=_("Not saved"))
	return rows


def _same(row, kind: str, text: str, amount) -> bool:
	if row.kind != kind:
		return False
	if kind == "budget":
		return flt(row.amount) == flt(amount)
	return _norm(row.text) == _norm(text)


def save(doc, values) -> dict:
	"""Edit memory: a line kept as it was keeps its row (who said it, its source, its lock); a new or
	changed line is staff's (locked: the provider may not change or remove it); a line taken out goes.
	Refused with ID numbers, eligibility details, prices or stock in any row. Returns the new card."""
	values = frappe.parse_json(values) if isinstance(values, str) else values
	if not isinstance(values, dict):
		frappe.throw(_("Nothing to save."), title=_("Not saved"))
	wanted = _staff_rows(values)
	auto = cint(values["auto_update"]) if values.get("auto_update") is not None else None
	with filelock(LOCK, timeout=10):
		memory = memory_for(doc, write=True, create=bool(wanted) or auto == 0)
		if memory:
			left = list(memory.items)
			ordered = []
			for kind, text, keywords, amount in wanted:
				same = next((r for r in left if _same(r, kind, text, amount)), None)
				if same:
					left.remove(same)
					ordered.append(same)
				else:
					ordered.append({"kind": kind, "text": text, "keywords": keywords, "amount": amount, "said": "Staff",
						"source_doctype": None, "source_name": None, "noted_on": nowdate(), "locked": 1})
			memory.set("items", [])
			for i, row in enumerate(ordered, 1):
				memory.append("items", row).idx = i
			if auto is not None:
				memory.auto_update = auto
			memory.flags.ignore_permissions = True
			memory.save()   # validate refuses a row with ID numbers, eligibility details, prices or stock
	return card(frappe.get_doc(doc.doctype, doc.name))


def clear(doc) -> dict:
	"""Clear memory: every row and this record's summary go. The watermark stays (old mail is not read
	again) and so does Update with AI. Returns the new card."""
	with filelock(LOCK, timeout=10):
		memory = memory_for(doc, write=True, create=False)
		if memory and memory.items:
			memory.set("items", [])
			memory.flags.ignore_permissions = True
			memory.save()
		if frappe.db.has_column(doc.doctype, SUMMARY):
			frappe.db.set_value(doc.doctype, doc.name, SUMMARY, None, update_modified=False)
	return card(frappe.get_doc(doc.doctype, doc.name))


_NOT_UPDATED = {
	"off": "Customer memory is off.",
	"paused": "Update with AI is off for this customer.",
	"nothing": "Nothing from the customer to read yet.",
	"discarded": "The memory changed while it was updating. Try again.",
}


def update_now(doc) -> dict:
	"""Update memory: read the record's latest customer messages now, without waiting for the sweep. A
	provider's refusal is raised as it was given. Returns the new card."""
	if not (table_ready() and memory_wanted()):
		frappe.throw(_(_NOT_UPDATED["off"]), title=_("Not updated"))
	try:
		outcome = run(doc.doctype, doc.name, manual=True)
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.throw(_(UPDATE_FAILED), title=_("Not updated"))   # run logged it
	if outcome["outcome"] == "no_answer":
		frappe.throw(outcome.get("refused") or _(UPDATE_FAILED), title=_("Not updated"))
	if outcome["outcome"] in _NOT_UPDATED:
		frappe.throw(_(_NOT_UPDATED[outcome["outcome"]]), title=_("Not updated"))
	return card(frappe.get_doc(doc.doctype, doc.name))


def kept_rows(doc) -> list:
	"""The rows of *doc*'s person's memory a provider may see (none while the table is not there yet)."""
	return _sendable(memory_for(doc)) if table_ready() else []


def draft_context(doc, rows: list) -> dict:
	"""``memory`` of a draft reply's context: the record's summary and *rows*, through
	``crm.sensitive.redact`` as a memory update's messages are, without sources, keywords or locks."""
	return {"summary": sensitive.redact(doc.get(SUMMARY) or ""),
		"items": [{"kind": r.kind, "text": sensitive.redact(r.text or ""), "amount": flt(r.amount) or None,
			"said": (r.said or "Customer").lower(), "on": str(r.noted_on) if r.noted_on else None} for r in rows]}


# ── the backfill ──

def queue_recent(days: int = 30, limit: int = 100) -> int:
	"""Mark due the open leads and deals with a customer message in the last *days* days that were never
	read, newest message first, *limit* at most: the sweep then works through them ten at a time. Once
	per site (the date is kept in the site default :data:`BACKFILLED`). Returns how many were marked."""
	if not table_ready() or frappe.db.get_default(BACKFILLED):
		return 0
	from ffl_core.crm.inquiries import EVENT_FIELD

	since = add_to_date(now_datetime(), days=-cint(days))
	latest: dict[tuple, object] = {}
	for r in frappe.get_all("Communication", filters={"reference_doctype": ["in", list(DOCTYPES)],
			"communication_type": "Communication", "sent_or_received": "Received", "communication_date": [">=", since]},
			fields=["reference_doctype", "reference_name", {"MAX": "communication_date", "as": "at"}],
			group_by="reference_doctype, reference_name"):
		latest[(r.reference_doctype, r.reference_name)] = r.at
	if frappe.db.has_column(NOTE, EVENT_FIELD):
		for r in frappe.get_all(NOTE, filters={"reference_doctype": ["in", list(DOCTYPES)], EVENT_FIELD: ["is", "set"],
				"creation": [">=", since]}, fields=["reference_doctype", "reference_docname", {"MAX": "creation", "as": "at"}],
				group_by="reference_doctype, reference_docname"):
			key = (r.reference_doctype, r.reference_docname)
			latest[key] = max(get_datetime(r.at), get_datetime(latest.get(key) or r.at))
	marked = 0
	due = now_datetime() - QUIET
	for (doctype, name), _at in sorted(latest.items(), key=lambda kv: get_datetime(kv[1]), reverse=True):
		if marked >= cint(limit):
			break
		record = frappe.db.get_value(doctype, name, ["status", SEEN] + (["converted"] if doctype == LEAD else []),
			as_dict=True)
		if not record or record.get(SEEN):
			continue
		if doctype == LEAD and (record.converted or record.status == OTHER_MAIL
				or frappe.db.get_value("CRM Lead Status", record.status, "type") == "Lost"):
			continue
		if doctype == DEAL and is_closed(record.status):
			continue
		frappe.db.set_value(doctype, name, DUE, due, update_modified=False)
		marked += 1
	frappe.db.set_default(BACKFILLED, nowdate())
	return marked
