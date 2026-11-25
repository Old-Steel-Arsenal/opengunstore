"""A reply drafted when the customer's mail arrives, ready before anyone clicks Draft reply.

With Lead Mail Settings › ``crm_draft_on_arrival`` on and a provider drafting (``crm.reply_draft.ready``),
a customer's mail filed on a lead or deal (``crm.mail.route``) and a website inquiry (``crm.inquiries``)
mark the record due (:func:`mark_due`, one UPDATE that never fails the mail). Every five minutes
:func:`sweep` queues the records quiet for as long as the customer memory waits (``crm.memory.QUIET``),
ten at most, and only once the memory has read them; :func:`run` writes the draft through the same
provider hook as the button and stores it on the record (:data:`STORED`). A refusal, a deferral, no
answer or a draft with something ``crm.sensitive`` blanks stores nothing, and is not asked again
until the next customer message. Other mail, closed records, sell requests and a latest message from
a machine get no draft.

Use (``crm.reply_draft.draft``): the stored draft is given instead of asking the provider only when it
answers the message being replied to, and everything it was written from is the same now, for the
person asking: :func:`fingerprint` covers the record, the whole context the provider was given (the
message, the thread, the customer, the memory) and the product facts, the ones the provider found
through ``find_stock`` included, recomputed with today's price and stock. The sweep runs as
Administrator; a user's own context leaves out a deal's lead they may not read and memory rows from
records they may not read, so it differs and that user gets a live draft. Envelope, From and signature
are always the user's, worked out when they click. A newer customer message or a mail the store
sends drops the stored draft (:func:`mark_due`, :func:`mail_sent`).

The fields are on permission level 1, which no role has: the stored text is read only here, never
through the record's own reads.
"""
from __future__ import annotations

import hashlib
import json

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import DEAL
from ffl_core.crm.mail import LEAD, OTHER_MAIL, is_automated
from ffl_core.utils import commit, single_flag

SETTINGS = "Lead Mail Settings"
SWITCH = "crm_draft_on_arrival"
RUN_JOB = "ffl_core.crm.arrival_draft.run"
DOCTYPES = (LEAD, DEAL)
# On CRM Lead and CRM Deal (crm.setup.CUSTOM_FIELDS).
DUE = "ffl_draft_due"            # when a customer message last marked the record; cleared once tried
HTML = "ffl_draft_html"          # the stored draft, as the composer takes it
HANDOFF = "ffl_draft_handoff"    # what the provider said needs a person
FOR = "ffl_draft_for"            # the message it answers: "in:<Communication>" or "note:<FCRM Note>"
AT = "ffl_draft_at"              # when it was written
PRINT = "ffl_draft_fingerprint"  # {"key": fingerprint, "items": the Items its facts were looked up for}
STORED = (HTML, HANDOFF, FOR, AT, PRINT)
_SAVEPOINT = "ffl_crm_arrival_draft"


def _log():
	return frappe.logger("ffl_crm_arrival_draft")


def table_ready() -> bool:
	return installed() and frappe.db.has_column(LEAD, DUE) and frappe.db.has_column(DEAL, DUE)


def switch_on() -> bool:
	"""Lead Mail Settings › Draft replies as mail arrives (read from the Singles table, as ``pos_link_on``)."""
	return installed() and single_flag(SETTINGS, SWITCH, default=0)


# ── marking due, and dropping ──

def _deal_of_lead(lead: str) -> str | None:
	return frappe.db.get_value(DEAL, {"lead": lead}, "name")


def mark_due(doctype: str, name: str) -> dict | None:
	"""A customer message landed on a lead or deal: its stored draft goes, and with the switch on and a
	provider drafting, the record is due for a new one. One UPDATE inside the caller's transaction: a
	failure is rolled back to its own savepoint and logged, never raised. A lead that became a deal marks
	the deal. Returns the values written."""
	if doctype not in DOCTYPES or not name:
		return None
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if not table_ready():
			return None
		if doctype == LEAD:
			lead = frappe.db.get_value(LEAD, name, ["status", "converted"], as_dict=True)
			if not lead:
				return None
			deal = lead.converted and _deal_of_lead(name)
			if deal:
				doctype, name = DEAL, deal
			elif lead.status == OTHER_MAIL:
				return None
		from ffl_core.crm import reply_draft

		values = dict.fromkeys(STORED)
		if switch_on() and reply_draft.ready():
			values[DUE] = now_datetime()
		elif not frappe.db.get_value(doctype, name, HTML):
			return None   # nothing to drop, nothing to mark
		frappe.db.set_value(doctype, name, values, update_modified=False)
		return values
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Draft not marked: {doctype} {name}", message=frappe.get_traceback())
		return None


def mail_routed(doc, target) -> None:
	"""``crm.mail.route``: a received mail just went on *target* (doctype, name); a customer's marks it."""
	if not target or target[0] not in DOCTYPES:
		return
	if doc.get("sent_or_received") != "Received" or doc.get("communication_type") != "Communication":
		return
	from ffl_core.crm.reply_draft import _address

	if is_automated(_address(doc.get("sender"))):
		return
	mark_due(*target)


def _drop(doctype: str, name: str) -> None:
	found = frappe.db.get_value(doctype, name, [HTML, DUE])
	if found and any(found):
		frappe.db.set_value(doctype, name, {**dict.fromkeys(STORED), DUE: None}, update_modified=False)


def mail_sent(doc, method=None) -> None:
	"""Communication after_insert: a mail the store sent on a lead or deal answers the customer, so its
	stored draft and any pending one go (a converted lead's deal's too). Never fails the send."""
	if doc.get("communication_type") != "Communication" or doc.get("sent_or_received") != "Sent":
		return
	doctype, name = doc.get("reference_doctype"), doc.get("reference_name")
	if doctype not in DOCTYPES or not name:
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if not table_ready():
			return
		_drop(doctype, name)
		deal = doctype == LEAD and _deal_of_lead(name)
		if deal:
			_drop(DEAL, deal)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Draft not dropped: {doctype} {name}", message=frappe.get_traceback())


def deal_inserted(doc, method=None) -> None:
	"""CRM Deal after_insert: CRM copies a lead's fields onto the deal it becomes, the stored draft too. A
	draft is the lead's (another record, other people, another context): it is not kept on the deal; the
	deal is marked for its own instead. A pending mark carries over as it is."""
	if not doc.get(HTML) or not table_ready():
		return
	frappe.db.set_value(DEAL, doc.name, dict.fromkeys(STORED), update_modified=False)
	doc.update(dict.fromkeys(STORED))
	values = mark_due(DEAL, doc.name)
	if values:
		doc.update(values)


# ── the sweep and the run ──

def sweep() -> list[tuple[str, str]]:
	"""Every five minutes: queue a draft for each record quiet for :data:`crm.memory.QUIET` since it was
	marked, oldest first, ten at most. A record the customer memory still has to read waits for it (the
	draft is told the memory), unless the memory is off or is waiting itself (a retry, a deferral).
	Returns what was queued."""
	from ffl_core.crm import memory, reply_draft

	if not table_ready() or not switch_on() or not reply_draft.ready():
		return []
	now = now_datetime()
	cutoff = now - memory.QUIET
	wait_for_memory = memory.table_ready() and memory.memory_wanted()
	rows = []
	for doctype in DOCTYPES:
		rows += [(get_datetime(due), doctype, name) for name, due in frappe.get_all(doctype,
			filters=[[DUE, "is", "set"], [DUE, "<=", cutoff]],   # "<=" alone matches an empty one (IFNULL)
			or_filters=[[memory.DUE, "is", "not set"], [memory.DUE, ">", now]] if wait_for_memory else None,
			fields=["name", DUE], order_by=f"{DUE} asc", limit=memory.BATCH, as_list=True)]
	rows.sort()
	queued = []
	for _due, doctype, name in rows[:memory.BATCH]:
		frappe.enqueue(RUN_JOB, queue="long", timeout=300, enqueue_after_commit=True,
			job_id=f"ffl_crm_arrival_draft:{doctype}:{name}", deduplicate=True, doctype=doctype, name=name)
		queued.append((doctype, name))
	return queued


def _clear(doctype: str, name: str, due0) -> None:
	"""The mark goes, only if it is still the one read when the run began: a message that came meanwhile
	moved it, and is drafted at the next round."""
	frappe.db.sql(f"UPDATE `tab{doctype}` SET `{DUE}` = NULL WHERE name = %s AND `{DUE}` = %s", (name, due0))


def _no_draft(doc) -> str | None:
	"""Why a record gets no draft ahead: other mail, closed, a sell request. None when it may."""
	from ffl_core.crm import memory, sell_requests

	if doc.doctype == LEAD and doc.get("status") == OTHER_MAIL:
		return "other mail"
	if memory._closed(doc):
		return "closed"
	if doc.doctype == DEAL and cint(doc.get(sell_requests.SELL)):
		return "sell request"
	return None


def run(doctype: str, name: str) -> dict:
	"""Draft the reply to a due record's newest customer message and store it (the queued job). Returns
	``{"outcome": str}``: gone, nothing, off, moved, skipped, no_answer, failed, sensitive, superseded or
	stored."""
	from ffl_core.crm import reply_draft

	if doctype not in DOCTYPES or not table_ready() or not frappe.db.exists(doctype, name):
		return {"outcome": "gone"}
	due0 = frappe.db.get_value(doctype, name, DUE)
	if not due0:
		return {"outcome": "nothing"}
	if not switch_on() or not reply_draft.ready():
		_clear(doctype, name, due0)
		return {"outcome": "off"}
	doc = frappe.get_doc(doctype, name)
	if doctype == LEAD and doc.get("converted"):
		deal = _deal_of_lead(name)
		if deal:   # the deal's timeline holds this lead's messages
			if not frappe.db.get_value(DEAL, deal, DUE):
				frappe.db.set_value(DEAL, deal, DUE, due0, update_modified=False)
			_clear(doctype, name, due0)
			return {"outcome": "moved"}
	why = _no_draft(doc)
	try:
		prepared = None if why else reply_draft.prepare(doc)
		latest = prepared and prepared.latest
		if latest and latest.kind == "in" and is_automated(reply_draft._address(latest.sender)):
			why = "automated sender"
		if why:
			_clear(doctype, name, due0)
			_log().info(f"no draft ahead: {doctype} {name}, {why}")
			return {"outcome": "skipped"}
		base = _canonical(prepared.context)   # before the provider sees it: it may change what it was given
		commit()   # only reads so far; the provider may take a while
		answer = reply_draft._ask(prepared.context)
	except frappe.ValidationError:
		# Refused, deferred, no answer, or nothing to answer: kept for the button, not asked again here.
		frappe.clear_messages()
		_clear(doctype, name, due0)
		commit()
		return {"outcome": "no_answer"}
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Draft not written ahead: {doctype} {name}", message=frappe.get_traceback())
		_clear(doctype, name, due0)
		commit()
		return {"outcome": "failed"}
	return _store(doc, due0, prepared, base, answer)


def _store(doc, due0, prepared, base: str, answer: dict) -> dict:
	from ffl_core.crm import reply_draft, sensitive

	handoff = str(answer["handoff"]) if answer.get("handoff") else None
	if sensitive.has_sensitive(answer["body"]) or sensitive.has_sensitive(handoff):
		_clear(doc.doctype, doc.name, due0)
		_log().info(f"draft ahead not stored: {doc.doctype} {doc.name}, sensitive text")
		commit()
		return {"outcome": "sensitive"}
	at = now_datetime()
	print_ = json.dumps({"key": fingerprint(doc, base, prepared.facts), "items": prepared.facts.items})
	# Only if the mark is still the one this run read: a newer customer message or a mail the store sent
	# meanwhile moved or cleared it, and this draft answers what is no longer the newest.
	frappe.db.sql(f"""UPDATE `tab{doc.doctype}` SET `{HTML}` = %s, `{HANDOFF}` = %s, `{FOR}` = %s, `{AT}` = %s,
		`{PRINT}` = %s, `{DUE}` = NULL WHERE name = %s AND `{DUE}` = %s""",
		(reply_draft.to_html(answer["body"]), handoff, message_key(prepared.latest), at, print_, doc.name, due0))
	stored = get_datetime(frappe.db.get_value(doc.doctype, doc.name, AT) or "1900-01-01") == at
	commit()
	return {"outcome": "stored" if stored else "superseded"}


# ── the fingerprint, and use ──

def message_key(message) -> str:
	return f"{message.kind}:{message.name}"


def _canonical(ctx: dict) -> str:
	"""The context as text, without the facts (fingerprinted with the ones the provider found too) and the
	``find_stock`` callable."""
	return json.dumps({k: v for k, v in ctx.items() if k not in ("facts", "find_stock")}, sort_keys=True, default=str)


def fingerprint(doc, base: str, facts) -> str:
	"""The record, the context (:func:`_canonical`) and every product fact with its price and stock."""
	raw = json.dumps([doc.doctype, doc.name, base, facts.facts], sort_keys=True, default=str)
	return hashlib.sha256(raw.encode()).hexdigest()


def stored(doc, prepared) -> dict | None:
	"""``{"content_html", "handoff"}`` of *doc*'s stored draft when it may stand in for a live one for the
	current user: it answers *prepared*'s message (``crm.reply_draft.prepare``, built for this user) and its
	fingerprint is the one of what this user's live draft would be written from now. Else None.

	lazy: the facts the provider found are looked up again on every use (12 Items at most); upgrade path:
	none needed while a draft has at most 12 facts."""
	if not table_ready() or not switch_on():
		return None
	row = frappe.db.get_value(doc.doctype, doc.name, [HTML, HANDOFF, FOR, PRINT], as_dict=True)
	if not row or not row.get(HTML) or row.get(FOR) != message_key(prepared.latest):
		return None
	try:
		saved = json.loads(row.get(PRINT) or "")
		items = [str(i) for i in saved["items"]]
		key = str(saved["key"])
	except (ValueError, TypeError, KeyError):
		return None
	from ffl_core.crm.reply_draft import Facts

	again = Facts()   # the user's own facts, then the ones the provider found, priced now
	again.items, again.facts = list(prepared.facts.items), list(prepared.facts.facts)
	for code in items:
		again.add(code)
	if fingerprint(doc, _canonical(prepared.context), again) != key:
		return None
	return {"content_html": row[HTML], "handoff": row.get(HANDOFF) or None}


def ready_for(doc) -> bool:
	"""Whether the Draft reply button reads Draft ready for the current user: a stored draft that
	:func:`stored` would give them for the record's newest customer message.

	lazy: the check builds the same context as a live draft (the stock is read when the record has wants)
	on each page load of a record with a stored draft; upgrade path: cache it per record until a price or
	stock change, if a page load is ever slow."""
	if not table_ready() or not switch_on() or not frappe.db.get_value(doc.doctype, doc.name, HTML):
		return False
	if not frappe.has_permission(doc.doctype, "write", doc=doc.name):
		return False
	from ffl_core.crm import reply_draft

	try:
		return bool(stored(doc, reply_draft.prepare(doc)))
	except frappe.ValidationError:
		frappe.clear_messages()
		return False
