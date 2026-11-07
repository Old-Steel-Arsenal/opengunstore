"""A customer's web orders on their open CRM deal: a note when an order is placed, shipped or picked up,
cancelled, or refunded, so whoever talks to them sees it. Behind Lead Mail Settings' deal to POS switch.

Only a note: the deal's value and status follow POS invoices alone (``crm.orders``). A customer with no
open deal gets no note and no deal. Woo redelivers webhooks, and an order passes some of these points
more than once, so a note's title is its key. Never fails the order: a failure is rolled back and logged.

A web order whose payment failed is the one exception (:func:`payment_failed`): the sale is still
there to win back, so it gets a deal when the customer has none, a note with the order and the link
where they can pay it, and one follow-up task per deal (more failures add to it). The task closes when
a later web order of the customer is paid, or an invoice is made from the deal (:func:`payment_recovered`,
:func:`close_payment_failed`). No email is sent to the customer.

lazy: a failure webhook that arrives after the customer's paid order leaves its task open (webhooks
are delivered in order per order, not across orders); upgrade path: compare the orders' paid times.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.form.assign_to import _add as assign
from frappe.desk.form.assign_to import close_all_assignments
from frappe.utils import escape_html, fmt_money, now_datetime

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.crm.deals import DEAL, _statuses, open_deal_for
from ffl_core.crm.mail import _customer_contact, _split_name, contacts_with_email, share_with_team

NOTE = "FCRM Note"
TASK = "CRM Task"
ORDER = "Woo Online Order"
PLACED, TRANSFERRED, CANCELLED, REFUNDED = "placed", "transferred", "cancelled", "refunded"
_TITLES = {
	PLACED: "Web order {0} placed",
	TRANSFERRED: "Web order {0} shipped or picked up",
	CANCELLED: "Web order {0} cancelled",
	REFUNDED: "Web order {0} refunded",
}
_FAILED_NOTE = "Web order {0} payment failed"
_TASK_TITLE = "Payment failed on web order {0} — contact customer"
_TASK_LIKE = "Payment failed on web order %"   # one open task per deal is told apart by its title
_TASK_OPEN = ["Backlog", "Todo", "In Progress"]


def _open_deal(email: str) -> str | None:
	"""The newest open deal this address's contact is on. Never makes one."""
	contacts = contacts_with_email(email)
	deals = contacts and frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": ["in", list(contacts)]},
		pluck="parent")
	if not deals:
		return None
	found = frappe.get_all(DEAL, filters={"name": ["in", deals], "status": ["not in", _statuses(closed=True) or [""]]},
		pluck="name", order_by="modified desc", limit=1)
	return found[0] if found else None


def note_web_order(order: str, event: str, key: str = "", amount: float | None = None) -> None:
	"""Add the note for *event* on the order's customer's open deal. *key* tells repeats of an event apart
	(a second refund); *amount* is what the note reports, the order's total if not given."""
	if not installed() or not pos_link_on():
		return
	frappe.db.savepoint("ffl_crm_web_order")
	try:
		row = frappe.db.get_value(ORDER, order, ["customer_email", "woo_order_id", "order_total", "currency"], as_dict=True)
		email = ((row and row.customer_email) or "").strip().lower()
		deal = email and _open_deal(email)
		if not deal:
			return
		title = _(_TITLES[event]).format(row.woo_order_id)
		if key:
			title = f"{title} ({key})"
		if frappe.db.exists(NOTE, {"reference_doctype": DEAL, "reference_docname": deal, "title": title}):
			return
		total = fmt_money(row.order_total if amount is None else amount, currency=row.currency)
		frappe.get_doc({"doctype": NOTE, "title": title, "content": f"<p>{frappe.utils.escape_html(total)}</p>",
			"reference_doctype": DEAL, "reference_docname": deal}).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_web_order")
		frappe.log_error(title=f"[{TITLE}] Web order note not added: {order}", message=frappe.get_traceback())


# Payment failed -------------------------------------------------------------------------------------

def _contact_of(email: str, full_name: str, phone: str) -> str:
	"""The Contact to put a new deal on: the one of a POS customer, else any that holds the address,
	else a new one (a buyer who never became a customer)."""
	contact = _customer_contact(email)
	if contact:
		return contact
	found = contacts_with_email(email)
	if found:
		return sorted(found)[0]
	first, last = _split_name(full_name)
	doc = {"doctype": "Contact", "first_name": first or email.split("@", 1)[0], "last_name": last,
		"email_ids": [{"email_id": email, "is_primary": 1}]}
	if phone:
		doc["phone_nos"] = [{"phone": phone, "is_primary_phone": 1}]
	# The webhook is a guest request: the system makes the contact, as CRM's own mail intake does.
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def _failure_html(order_id: str, total: str, items: list[str], pay_link: str, method: str, phone: str) -> str:
	lines = [f"<b>{escape_html(_('Web order {0}').format(order_id))}</b>: {escape_html(total)}"]
	if items:
		lines.append(escape_html(", ".join(items)))
	if method:
		lines.append(escape_html(_("Paid with: {0}").format(method)))
	if phone:
		lines.append(escape_html(_("Phone: {0}").format(phone)))
	if pay_link.startswith(("http://", "https://")):
		lines.append(f'<a href="{escape_html(pay_link)}">{escape_html(_("Link for the customer to pay this order"))}</a>')
	return "".join(f"<p>{line}</p>" for line in lines)


def _add_to_task(deal: str, order_id: str, html: str) -> None:
	"""The deal's open payment-failed task gets this order added; a deal with none gets one."""
	task = frappe.db.get_value(TASK, {"reference_doctype": DEAL, "reference_docname": deal,
		"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}, ["name", "description"], as_dict=True)
	if task:
		frappe.db.set_value(TASK, task.name, "description", (task.description or "") + html)
		return
	title = _(_TASK_TITLE).format(order_id)
	task = frappe.get_doc({"doctype": TASK, "title": title, "description": html, "priority": "High", "status": "Todo",
		"due_date": now_datetime(), "reference_doctype": DEAL, "reference_docname": deal}).insert(ignore_permissions=True)
	# Not by assigned_to on the insert: CRM's after_insert checks the session user's rights on the task.
	# A deal nobody owns is the team's (shared on creation): its task is readable by every Sales User.
	owner = frappe.db.get_value(DEAL, deal, "deal_owner")
	if owner:
		assign({"assign_to": [owner], "doctype": TASK, "name": task.name, "description": title}, ignore_permissions=True)


def payment_failed(order_id: str, email: str, full_name: str = "", phone: str = "", total: float = 0,
		currency: str | None = None, items: list[str] | None = None, pay_link: str = "", method: str = "") -> None:
	"""A web order's payment failed (the webhook decides what counts): note it on the customer's open deal,
	making one when they have none, and leave the team one task to contact them. *items* are lines of text,
	*pay_link* where the customer can pay this order. Once per order: Woo redelivers."""
	email = (email or "").strip().lower()
	if not email or not installed() or not pos_link_on():
		return
	title = _(_FAILED_NOTE).format(order_id)
	if frappe.db.exists(NOTE, {"reference_doctype": DEAL, "title": title}):
		return
	frappe.db.savepoint("ffl_crm_payment_failed")
	try:
		deal = _open_deal(email)
		if not deal:
			deal = open_deal_for(_contact_of(email, full_name, phone))
			share_with_team(DEAL, deal)
		html = _failure_html(str(order_id), fmt_money(total, currency=currency), items or [], pay_link, method, phone)
		frappe.get_doc({"doctype": NOTE, "title": title, "content": html,
			"reference_doctype": DEAL, "reference_docname": deal}).insert(ignore_permissions=True)
		_add_to_task(deal, str(order_id), html)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_payment_failed")
		frappe.log_error(title=f"[{TITLE}] Payment failure not followed up: web order {order_id}", message=frappe.get_traceback())


def close_payment_failed(deals: list[str], recovered_by: str) -> None:
	"""The open payment-failed tasks on these deals are done: the customer paid. Each deal gets a note
	saying *recovered_by*. Never fails the caller: a failure is rolled back and logged."""
	if not deals or not installed() or not pos_link_on():
		return
	frappe.db.savepoint("ffl_crm_payment_recovered")
	try:
		for task in frappe.get_all(TASK, filters={"reference_doctype": DEAL, "reference_docname": ["in", deals],
				"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}, fields=["name", "reference_docname"]):
			doc = frappe.get_doc(TASK, task.name)
			doc.status = "Done"
			doc.save(ignore_permissions=True)
			close_all_assignments(TASK, task.name, ignore_permissions=True)
			frappe.get_doc({"doctype": NOTE, "title": recovered_by, "content": f"<p>{escape_html(_('The payment-failed task is done.'))}</p>",
				"reference_doctype": DEAL, "reference_docname": task.reference_docname}).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="ffl_crm_payment_recovered")
		frappe.log_error(title=f"[{TITLE}] Payment-failed task not closed: {recovered_by}", message=frappe.get_traceback())


def payment_recovered(order_id: str, email: str) -> None:
	"""A web order of this customer is paid: close their payment-failed tasks, on any of their deals
	(a deal may have been closed since). Never fails the order: a failure is logged."""
	email = (email or "").strip().lower()
	if not email or not installed() or not pos_link_on():
		return
	try:
		if not frappe.db.exists(TASK, {"status": ["in", _TASK_OPEN], "title": ["like", _TASK_LIKE]}):
			return   # every paid order comes by here: most days there is nothing to close
		contacts = contacts_with_email(email)
		if contacts:
			deals = frappe.get_all("CRM Contacts", filters={"parenttype": DEAL, "contact": ["in", list(contacts)]}, pluck="parent")
			close_payment_failed(deals, _("Recovered by web order {0}").format(order_id))
	except Exception:
		frappe.log_error(title=f"[{TITLE}] Payment-failed task not closed: web order {order_id}", message=frappe.get_traceback())
