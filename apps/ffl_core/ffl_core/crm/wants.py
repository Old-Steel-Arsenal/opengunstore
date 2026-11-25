"""A customer's want list (CRM Want) and the alert when something that fits arrives.

Purchase Receipt and Stock Entry (Material Receipt) on_submit queue :func:`match_arrival` after the
stock document commits. It lists what came in (each item, and each gun by serial number) and sets it
against every Open or Matched want. A want fits an arrival when its item is the arrival's item, or
every keyword is found in the arrival's name, brand, model, caliber and description, compared
without case, spaces or punctuation ("91/30" = "9130", "P-228" = "P228" = "P 228"). A fit gives the want's owner a
CRM Task (one open arrival task per want: a further fit is added to it), on the want's deal while
that is open, and moves the want to Matched. No email goes to the customer and no deal or lead changes.

A customer's gun in for transfer is not the store's to sell and never alerts (``custody``).

From what the customer said (Lead Mail Settings › crm_wants_from_memory): a ``want`` row the customer's
mail put into customer memory (``crm.memory.apply``) becomes a CRM Want when nothing in stock fits it
(:func:`from_memory`). One want per memory row, found by ``memory_row``: while the want is Open a changed
row changes it, and a row the provider removed closes it. A want staff edited or closed, or one that
matched, is left alone. A lead has no Contact to hold a want, so its rows become wants when it becomes a
deal (``memory.deal_inserted``). A want from memory has the record's owner, or none: whoever owns the deal
when the arrival comes gets the task, else the Sales Managers (:func:`_assignees`).

Nothing here can fail the stock document: the hook only queues, the job runs after the commit, and a
failure on one want is rolled back, logged, and leaves the others alone. The state is in the database
(the want and its task), not in Redis, so a deploy that clears the cache loses nothing.

lazy: every Open or Matched want is read and tried against every arrival, and a keyword is a plain
substring of the arrival's text with its spaces and punctuation removed (so "p22" fits "SIG P228", and a
short word can fit across two words); fine for a want list in the hundreds. Upgrade path: a keyword
index, whole-word matching.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, get_datetime, get_url_to_form, now_datetime, strip_html

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import DEAL, is_closed, new_task
from ffl_core.custody import is_custody_serial
from ffl_core.serials import serials_for_line
from ffl_core.utils import commit, single_flag

WANT = "CRM Want"
TASK = "CRM Task"
ACTIVE = ["Open", "Matched"]
_TASK_OPEN = ("Backlog", "Todo", "In Progress")
JOB = "ffl_core.crm.wants.match_arrival"
_SAVEPOINT = "ffl_crm_want"
SETTINGS = "Lead Mail Settings"
SWITCH = "crm_wants_from_memory"
_MEMORY_SAVEPOINT = "ffl_crm_want_memory"


def _word(text: str) -> str:
	return "".join(c for c in text.lower() if c.isalnum())


def keyword_tokens(keywords: str | None) -> list[str]:
	"""The words of a want's keywords, each without case or punctuation: "Mosin 91/30" is
	["mosin", "9130"]. A word with nothing but punctuation is dropped."""
	return [w for w in map(_word, (keywords or "").split()) if w]


def _text(*parts) -> str:
	"""Free text as what keywords are looked up in: tags out, then as :func:`_word`."""
	return _word(strip_html(" ".join(str(p) for p in parts if p)))


class Arrival(frappe._dict):
	"""One thing that came in: its ``item_code``, ``item_name``, ``serial`` (None for a counted item)
	and the ``text`` a keyword is looked up in."""


def _arrivals(doc) -> list[Arrival]:
	items = {}
	arrivals = []
	for row in doc.get("items") or []:
		if not row.item_code:
			continue
		if row.item_code not in items:
			items[row.item_code] = frappe.db.get_value("Item", row.item_code,
				["item_name", "manufacturer", "model_name", "caliber"], as_dict=True) or frappe._dict()
		item = items[row.item_code]
		for serial in serials_for_line(row) or [None]:
			# A customer's gun in for transfer is not for sale: no alert.
			if serial and is_custody_serial(serial):
				continue
			gun = (serial and frappe.db.get_value("Serial No", serial,
				["item_name", "description", "manufacturer", "model_name", "caliber"], as_dict=True)) or frappe._dict()
			arrivals.append(Arrival(item_code=row.item_code, item_name=gun.item_name or item.item_name or row.item_code,
				serial=serial, text=_text(item.item_name, item.manufacturer, item.model_name, item.caliber,
					gun.item_name, gun.description, gun.manufacturer, gun.model_name, gun.caliber)))
	return arrivals


def _fits(want, arrival: Arrival) -> bool:
	if want.item and want.item == arrival.item_code:
		return True
	tokens = keyword_tokens(want.keywords)
	return bool(tokens) and all(t in arrival.text for t in tokens)


def on_stock_receipt(doc, method=None):
	"""Purchase Receipt and Stock Entry on_submit: queue the match for after the commit. Never raises."""
	try:
		if doc.doctype == "Stock Entry" and doc.stock_entry_type != "Material Receipt":
			return
		if doc.get("is_return"):
			return   # goods going back to the supplier, not arriving
		# Most days nobody wants anything: no job for a stock document then.
		if not installed() or not frappe.db.exists(WANT, {"status": ["in", ACTIVE]}):
			return
		frappe.enqueue(JOB, queue="short", enqueue_after_commit=True, doctype=doc.doctype, name=doc.name)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Want match not queued: {doc.doctype} {doc.name}", message=frappe.get_traceback())


def match_arrival(doctype: str, name: str) -> None:
	"""The job: set what *doctype* *name* took in against the want list. Safe to run twice (the
	second run adds the same lines to the task again, nothing more)."""
	try:
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus != 1:
			return   # cancelled since
		arrivals = _arrivals(doc)
		wants = arrivals and frappe.get_all(WANT, filters={"status": ["in", ACTIVE]}, order_by="name asc",
			fields=["name", "item", "keywords"])
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Want match failed: {doctype} {name}", message=frappe.get_traceback())
		return
	for want in wants or []:
		found = [a for a in arrivals if _fits(want, a)]
		if not found:
			continue
		frappe.db.savepoint(_SAVEPOINT)
		try:
			_alert(want.name, found)
			commit()
		except Exception:
			frappe.db.rollback(save_point=_SAVEPOINT)
			frappe.log_error(title=f"[{TITLE}] Want alert failed: {want.name} for {doctype} {name}",
				message=frappe.get_traceback())


def _alert(name: str, found: list[Arrival]) -> None:
	# Locking reads: another receipt's job may be adding to the same task right now, and a plain read
	# would show the snapshot from before it committed.
	want = frappe.get_doc(WANT, name, for_update=True)
	if want.status not in ACTIVE:
		return   # closed while this job waited
	lines = "".join(f"<p>{escape_html(_line(a))}</p>" for a in found)
	task = want.arrival_task and frappe.db.get_value(TASK, want.arrival_task, ["name", "status", "description"],
		as_dict=True, for_update=True)
	if task and task.status in _TASK_OPEN:
		frappe.db.set_value(TASK, task.name, "description", (task.description or "") + lines)
	else:
		deal = _open_deal(want.deal)
		want.arrival_task = new_task(_title(want, found[0]), _intro(want) + lines, _assignees(want, deal), deal,
			priority="High", due_date=now_datetime())
	last = found[-1]
	want.status = "Matched"
	want.matched_item, want.matched_serial, want.matched_on = last.item_code, last.serial, now_datetime()
	want.flags.ignore_permissions = True   # the system's write: the owner may be a clerk with no write right
	want.save()


def _assignees(want, deal: str | None) -> str | list[str] | None:
	"""Who gets the arrival task: the want's owner, else the open deal's owner (a want from memory has
	none until someone owns the deal: the first mail a staff member sends on it), else every Sales Manager
	(the people who can open a record nobody owns)."""
	owner = want.want_owner or (deal and frappe.db.get_value(DEAL, deal, "deal_owner"))
	return owner or _managers() or None


def _managers() -> list[str]:
	users = set(frappe.get_all("Has Role", filters={"parenttype": "User", "role": "Sales Manager"}, pluck="parent"))
	users -= {"Administrator", "Guest"}
	return frappe.get_all("User", filters={"name": ["in", sorted(users)], "enabled": 1, "user_type": "System User"},
		pluck="name", order_by="name asc") if users else []


def _open_deal(deal: str | None) -> str | None:
	status = deal and frappe.db.get_value(DEAL, deal, "status")
	return deal if deal and not is_closed(status) else None


def _line(arrival: Arrival) -> str:
	return _("Arrived: {0} ({1})").format(arrival.item_name, arrival.serial) if arrival.serial \
		else _("Arrived: {0}").format(arrival.item_name)


def _wanted(want) -> str:
	return want.keywords or frappe.db.get_value("Item", want.item, "item_name") or want.item


def _who(want) -> str:
	return (want.customer and frappe.db.get_value("Customer", want.customer, "customer_name")) \
		or frappe.db.get_value("Contact", want.contact, "full_name") or want.contact


def _title(want, arrival: Arrival) -> str:
	what = f"{arrival.item_name} ({arrival.serial})" if arrival.serial else arrival.item_name
	return _("Arrival: {0}, {1} wants {2}").format(what, _who(want), _wanted(want))


def _intro(want) -> str:
	"""The task's first lines: who wants what, and where to open the want."""
	parts = [_("{0} wants {1}").format(_who(want), _wanted(want))]
	if want.budget:
		parts.append(_("Budget: {0}").format(fmt_money(want.budget)))
	if want.notes:
		parts.append(want.notes)
	html = "".join(f"<p>{escape_html(p)}</p>" for p in parts)
	return html + f'<p><a href="{escape_html(get_url_to_form(WANT, want.name))}">{escape_html(want.name)}</a></p>'


# ── wants from what the customer said (crm.memory) ──

def from_memory_on() -> bool:
	return installed() and single_flag(SETTINGS, SWITCH, default=0)


def from_memory(doc, memory, rows: list | None = None, gone=()) -> None:
	"""Keep the wants made from *memory*'s rows in step with it, after a write to it (never raises: a
	failure is rolled back to its own savepoint and logged, the memory write stands).

	* *gone*: names of rows that left the memory; an Open want made from one is closed.
	* every Open want made from a row still there takes the row's keywords and text, and the memory's budget.
	* *rows*: the rows the customer just said or said again, for *doc*, a CRM Deal: each without a want
	  gets one when nothing in stock fits it. None (a lead just became *doc*): the rows said on that lead.

	A lead (*doc* is a CRM Lead) makes none: a want needs a Contact. A want staff edited, closed or that
	matched is left as it is."""
	if not memory or not from_memory_on() or not frappe.db.has_column(WANT, "memory_row"):
		return
	frappe.db.savepoint(_MEMORY_SAVEPOINT)
	try:
		_from_memory(doc, memory, rows, tuple(gone))
	except Exception:
		frappe.db.rollback(save_point=_MEMORY_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Wants from memory failed: {doc.doctype} {doc.name}", message=frappe.get_traceback())


def _ours(want) -> bool:
	"""An Open want nobody has touched since memory wrote it."""
	return want.status == "Open" and bool(want.memory_synced) and get_datetime(want.modified) == get_datetime(want.memory_synced)


def _write(want) -> None:
	want.flags.ignore_permissions = True
	want.flags.ffl_owner_at_arrival = True
	if want.name:
		want.save()
	else:
		want.insert()
	frappe.db.set_value(WANT, want.name, "memory_synced", want.modified, update_modified=False)


def _from_memory(doc, memory, rows, gone) -> None:
	from ffl_core.crm.customer import _primary_contact
	from ffl_core.crm.mail import LEAD
	from ffl_core.crm.memory import _source_record
	from ffl_core.crm.reply_draft import in_stock_items

	kept = {r.name: r for r in memory.items if r.name}
	budget = next((flt(r.amount) for r in memory.items if r.kind == "budget" and flt(r.amount) > 0), None)
	if gone:
		for name in frappe.get_all(WANT, filters={"memory_row": ["in", list(gone)], "status": "Open"}, pluck="name"):
			want = frappe.get_doc(WANT, name, for_update=True)
			if _ours(want):
				want.status = "Closed"
				want.flags.ignore_permissions = True
				want.save()
	linked = {}
	for name in frappe.get_all(WANT, filters={"memory_row": ["in", list(kept)]}, pluck="name") if kept else []:
		want = frappe.get_doc(WANT, name, for_update=True)
		linked[want.memory_row] = want
		row = kept[want.memory_row]
		if not _ours(want) or row.said != "Customer" or cint(row.locked):
			continue
		values = {"keywords": row.keywords, "notes": row.text, "budget": budget}
		if any((want.get(k) or None) != (v or None) for k, v in values.items()):
			want.update(values)
			_write(want)
	if doc.doctype != DEAL:
		return
	contact = _primary_contact(doc)
	if not contact:
		return
	if rows is None:
		here = {(LEAD, doc.get("lead")), (DEAL, doc.name)}
		rows = [r for r in memory.items if _source_record(r) in here]
	stock = None
	for row in rows:
		if row.kind != "want" or row.said != "Customer" or cint(row.locked) or not row.name or row.name in linked \
				or row.name not in kept:
			continue
		stock = in_stock_items() if stock is None else stock
		if any(_fits(frappe._dict(item=None, keywords=row.keywords), a) for a in stock):
			continue
		_write(frappe.get_doc({"doctype": WANT, "contact": contact, "deal": doc.name, "keywords": row.keywords,
			"customer": doc.get("erpnext_customer") or None, "notes": row.text, "budget": budget,
			"want_owner": doc.get("deal_owner") or None, "source_doctype": row.source_doctype,
			"source_name": row.source_name, "memory_row": row.name}))


def carry(source, target) -> None:
	"""Memory *source* is being merged into *target* (``memory._merge``): a want made from one of
	*source*'s rows follows the same thing said in *target*, so the row keeps its one want. Never raises."""
	if not from_memory_on() or not frappe.db.has_column(WANT, "memory_row"):
		return
	from ffl_core.crm.memory import _norm

	frappe.db.savepoint(_MEMORY_SAVEPOINT)
	try:
		for row in source.items:
			want = row.kind == "want" and frappe.db.get_value(WANT, {"memory_row": row.name}, "name")
			twin = want and next((t for t in target.items if t.kind == "want" and t.name and _norm(t.text) == _norm(row.text)), None)
			if twin and not frappe.db.exists(WANT, {"memory_row": twin.name}):
				frappe.db.set_value(WANT, want, "memory_row", twin.name, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point=_MEMORY_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Wants not carried to merged memory: {target.name}", message=frappe.get_traceback())
