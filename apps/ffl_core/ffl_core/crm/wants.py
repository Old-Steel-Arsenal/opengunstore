"""A customer's want list (CRM Want) and the alert when something that fits arrives.

Purchase Receipt and Stock Entry (Material Receipt) on_submit queue :func:`match_arrival` after the
stock document commits. It lists what came in (each item, and each gun by serial number) and sets it
against every Open or Matched want. A want fits an arrival when its item is the arrival's item, or
every keyword is found in the arrival's name, brand, model, caliber and description, compared
without case, spaces or punctuation ("91/30" = "9130", "P-228" = "P228" = "P 228"). A fit gives the want's owner a
CRM Task (one open arrival task per want: a further fit is added to it), on the want's deal while
that is open, and moves the want to Matched. No email goes to the customer and no deal or lead changes.

A customer's gun in for transfer is not the store's to sell and never alerts (``custody``).

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
from frappe.utils import escape_html, fmt_money, get_url_to_form, now_datetime, strip_html

from ffl_core.crm import TITLE, installed
from ffl_core.crm.deals import DEAL, is_closed, new_task
from ffl_core.custody import is_custody_serial
from ffl_core.serials import serials_for_line
from ffl_core.utils import commit

WANT = "CRM Want"
TASK = "CRM Task"
ACTIVE = ["Open", "Matched"]
_TASK_OPEN = ("Backlog", "Todo", "In Progress")
JOB = "ffl_core.crm.wants.match_arrival"
_SAVEPOINT = "ffl_crm_want"


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
		want.arrival_task = new_task(_title(want, found[0]), _intro(want) + lines, want.want_owner,
			_open_deal(want.deal), priority="High", due_date=now_datetime())
	last = found[-1]
	want.status = "Matched"
	want.matched_item, want.matched_serial, want.matched_on = last.item_code, last.serial, now_datetime()
	want.flags.ignore_permissions = True   # the system's write: the owner may be a clerk with no write right
	want.save()


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
