"""A deal's sales: what a Sales Invoice made from a CRM Deal writes back to that deal.

The value is the Sales Report's Net Sales (``net_sales_of``) summed over the deal's submitted
invoices, credit notes negative: a trade-in allowance does not reduce it, a transfer fee is in it,
sales tax, excise tax and shipping are not. It goes in ``deal_value``, which CRM's Won board reads;
``net_total`` stays the quote. While the deal has submitted invoices its validate pins the value.

The first invoice that is not a credit note makes the deal Won: invoiced, not paid or handed over.
A credit note or a cancelled invoice recalculates the value and gives the owner a task; the status
does not move. Edit Order cancels and rebuilds an order in one transaction, so its cancel makes no
task. Only Sales Invoice takes part: POS Invoice has no deal field, and a consolidated invoice never
carries one.

A write-back runs inside the invoice's transaction and never commits. It never stops an invoice
either: a failure is rolled back to before it and goes in the Error Log.

lazy: two invoices of one deal submitted in the same second: the second waits on the deal's row
and then sums from its own snapshot, or fails to the Error Log under InnoDB snapshot isolation.
The deal's next save sums again; upgrade path: a locking read of the invoices.
lazy: with CRM's forecasting on, a deal without an expected value and closure date cannot go Won
here (CRM's validate refuses it, Error Log); upgrade path: fill both on the write-back.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.form.assign_to import _add as assign
from frappe.utils import flt, fmt_money

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.crm.deal import WRITE_BACK_FLAG
from ffl_core.crm.deals import DEAL, STATUS

DEAL_FIELD = "ffl_crm_deal"
# update_order's mark on the invoice it cancels and rebuilds (api/manual_order.py).
EDIT_FLAG = "ffl_edit_carry_forward"
_SAVEPOINT = "ffl_crm_write_back"


def deal_for_order(deal) -> str | None:
	"""The deal an order's payload names, when it still exists. A deal deleted while its draft
	waited is no reason to refuse the sale."""
	deal = str(deal or "").strip()
	return deal if deal and installed() and frappe.db.exists(DEAL, deal) else None


def deal_sales(deal: str) -> tuple[float, bool]:
	"""(the deal's value, whether it has submitted invoices)."""
	from ffl_core.ffl_core.report.sales_report.sales_report import net_sales_of

	nets = net_sales_of("Sales Invoice", {DEAL_FIELD: deal, "docstatus": 1, "is_consolidated": 0})
	return flt(sum(nets.values()), 2), bool(nets)


def on_submit(doc, method=None):
	"""Sales Invoice on_submit."""
	_write_back(doc, cancelled=False)


def on_cancel(doc, method=None):
	"""Sales Invoice on_cancel."""
	_write_back(doc, cancelled=True)


def pin_deal_value(doc, method=None):
	"""CRM Deal validate: while invoices stand behind it, the value is theirs."""
	if doc.is_new() or doc.flags.get(WRITE_BACK_FLAG) or not pos_link_on():
		return
	value, invoiced = deal_sales(doc.name)
	if invoiced:
		doc.deal_value = value


def _write_back(doc, cancelled: bool) -> None:
	deal = doc.get(DEAL_FIELD)
	if not deal or doc.get("is_consolidated") or not pos_link_on():
		return
	messages = len(frappe.local.message_log)
	frappe.db.savepoint(_SAVEPOINT)
	try:
		_apply(doc, deal, cancelled)
	except Exception:
		# Whatever it said to the user goes with it: the invoice is fine.
		frappe.db.rollback(save_point=_SAVEPOINT)
		del frappe.local.message_log[messages:]
		frappe.log_error(title=f"[{TITLE}] Deal write-back failed: {doc.name}", message=frappe.get_traceback())


def _apply(doc, name: str, cancelled: bool) -> None:
	if not frappe.db.exists(DEAL, name):
		return   # deleted since: the invoice keeps the name, there is nothing to write to
	deal = frappe.get_doc(DEAL, name, for_update=True)
	is_return = bool(doc.get("is_return"))
	deal.deal_value = deal_sales(name)[0]
	if not cancelled and not is_return and frappe.db.get_value(STATUS, deal.status, "type") != "Won":
		# CRM's validate logs the status change and sets closed_date.
		deal.status = frappe.db.get_value(STATUS, {"type": "Won"}, "name", order_by="position asc")
	deal.flags[WRITE_BACK_FLAG] = True
	# CRM's DocType, written for whoever submitted the invoice, who need not hold CRM rights.
	deal.flags.ignore_permissions = True
	deal.save()
	if is_return or (cancelled and not doc.flags.get(EDIT_FLAG)):
		_task_for_owner(deal, doc, cancelled)


def _task_for_owner(deal, doc, cancelled: bool) -> None:
	if cancelled:
		title = _("{0} was cancelled: check deal {1}").format(doc.name, deal.name)
	else:
		title = _("Return {0}: check deal {1}").format(doc.name, deal.name)
	task = frappe.get_doc({
		"doctype": "CRM Task",
		"title": title,
		"description": _("The deal's value is now {0}. Its status did not change.").format(
			fmt_money(deal.deal_value, currency=deal.get("currency"))),
		"priority": "High",
		"status": "Todo",
		"reference_doctype": DEAL,
		"reference_docname": deal.name,
	}).insert(ignore_permissions=True)
	# Not by assigned_to on the insert: CRM's after_insert checks the session user's rights on the
	# task, and a Stock Manager cancelling an order has none.
	if deal.deal_owner:
		assign({"assign_to": [deal.deal_owner], "doctype": "CRM Task", "name": task.name, "description": title},
			ignore_permissions=True)
