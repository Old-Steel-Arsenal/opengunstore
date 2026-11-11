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
either: a failure is rolled back to before it and goes in the Error Log, and a short job queued for
after the invoice commits (:func:`recalculate_deal`) sums the deal again and makes it Won when an
invoice stands, if the failed write-back was a sale's. That retry makes no task for the owner: a return or a cancel whose write-back failed
leaves the Error Log entry as the only trace. If the retry fails too it only logs.

lazy: two invoices of one deal submitted in the same second: the second waits on the deal's row
and then sums from its own snapshot, or fails to the Error Log under InnoDB snapshot isolation.
The deal's next save sums again; upgrade path: a locking read of the invoices.
lazy: with CRM's forecasting on, a deal without an expected value and closure date cannot go Won
here (CRM's validate refuses it, Error Log); upgrade path: fill both on the write-back.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.crm.deal import WRITE_BACK_FLAG
from ffl_core.crm.deals import DEAL, STATUS, new_task
from ffl_core.crm.web_orders import close_payment_failed
from ffl_core.utils import commit

DEAL_FIELD = "ffl_crm_deal"
# update_order's mark on the invoice it cancels and rebuilds (api/manual_order.py).
EDIT_FLAG = "ffl_edit_carry_forward"
_SAVEPOINT = "ffl_crm_write_back"
_RETRY_SAVEPOINT = "ffl_crm_recalculate"


def deal_for_order(deal, customer: str, keep: bool = False) -> str | None:
	"""The deal an order's payload names, when it still exists and is the buyer's: a deal stays on
	the invoice only while its ``erpnext_customer`` is the order's customer, so an employee who
	changed the buyer on Create Order leaves the deal as it was. ``keep`` skips that check (Edit
	Order rebuilds on the old invoice's deal). A deal deleted while its draft waited is no reason to
	refuse the sale."""
	deal = str(deal or "").strip()
	if not deal or not installed() or not frappe.db.exists(DEAL, deal):
		return None
	if not keep and frappe.db.get_value(DEAL, deal, "erpnext_customer") != customer:
		return None
	return deal


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
		_queue_recalculation(deal, may_win=not cancelled and not doc.get("is_return"))


def _queue_recalculation(deal: str, may_win: bool) -> None:
	"""One recalculation per deal, after the invoice's transaction commits. A queue that is down
	costs the retry, never the invoice."""
	try:
		frappe.enqueue("ffl_core.crm.orders.recalculate_deal", queue="short", enqueue_after_commit=True,
			job_id=f"ffl_crm_recalculate:{deal}", deduplicate=True, deal=deal, may_win=may_win)
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Deal recalculation not queued: {deal}", message=frappe.get_traceback())


def recalculate_deal(deal: str, may_win: bool = True) -> None:
	"""The retry job after a failed write-back, in its own transaction: the deal's value from its
	submitted invoices, and, with ``may_win`` (the failure was a sale's, not a return's or a cancel's,
	which leave the status alone), Won when one that is not a credit note stands. Safe to run any
	number of times. It makes no task for the owner."""
	frappe.db.savepoint(_RETRY_SAVEPOINT)
	try:
		if pos_link_on() and frappe.db.exists(DEAL, deal):
			doc = frappe.get_doc(DEAL, deal, for_update=True)
			doc.deal_value = deal_sales(deal)[0]
			if may_win and frappe.db.exists("Sales Invoice", {DEAL_FIELD: deal, "docstatus": 1, "is_return": 0, "is_consolidated": 0}):
				_mark_won(doc)
			_save(doc)
		commit()
	except Exception:
		frappe.db.rollback(save_point=_RETRY_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Deal recalculation failed: {deal}", message=frappe.get_traceback())


def _mark_won(deal) -> None:
	if frappe.db.get_value(STATUS, deal.status, "type") != "Won":
		# CRM's validate logs the status change and sets closed_date.
		deal.status = frappe.db.get_value(STATUS, {"type": "Won"}, "name", order_by="position asc")


def _save(deal) -> None:
	deal.flags[WRITE_BACK_FLAG] = True
	# CRM's DocType, written for whoever submitted the invoice, who need not hold CRM rights.
	deal.flags.ignore_permissions = True
	deal.save()


def _apply(doc, name: str, cancelled: bool) -> None:
	if not frappe.db.exists(DEAL, name):
		return   # deleted since: the invoice keeps the name, there is nothing to write to
	deal = frappe.get_doc(DEAL, name, for_update=True)
	is_return = bool(doc.get("is_return"))
	deal.deal_value = deal_sales(name)[0]
	if not cancelled and not is_return:
		_mark_won(deal)
	_save(deal)
	if not cancelled and not is_return:
		# The sale a failed web payment was a chance of is made.
		close_payment_failed([name], _("Recovered by invoice {0}").format(doc.name))
	if is_return or (cancelled and not doc.flags.get(EDIT_FLAG)):
		_task_for_owner(deal, doc, cancelled)


def _task_for_owner(deal, doc, cancelled: bool) -> None:
	if cancelled:
		title = _("{0} was cancelled: check deal {1}").format(doc.name, deal.name)
	else:
		title = _("Return {0}: check deal {1}").format(doc.name, deal.name)
	new_task(title, _("The deal's value is now {0}. Its status did not change.").format(
		fmt_money(deal.deal_value, currency=deal.get("currency"))), deal.deal_owner, deal.name, priority="High")
