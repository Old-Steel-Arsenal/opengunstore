"""A customer's web orders on their open CRM deal: a note when an order is placed, shipped or picked up,
cancelled, or refunded, so whoever talks to them sees it. Behind Lead Mail Settings' deal to POS switch.

Only a note: the deal's value and status follow POS invoices alone (``crm.orders``). A customer with no
open deal gets no note and no deal. Woo redelivers webhooks, and an order passes some of these points
more than once, so a note's title is its key. Never fails the order: a failure is rolled back and logged.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import fmt_money

from ffl_core.crm import TITLE, installed, pos_link_on
from ffl_core.crm.deals import DEAL, _statuses
from ffl_core.crm.mail import contacts_with_email

NOTE = "FCRM Note"
ORDER = "Woo Online Order"
PLACED, TRANSFERRED, CANCELLED, REFUNDED = "placed", "transferred", "cancelled", "refunded"
_TITLES = {
	PLACED: "Web order {0} placed",
	TRANSFERRED: "Web order {0} shipped or picked up",
	CANCELLED: "Web order {0} cancelled",
	REFUNDED: "Web order {0} refunded",
}


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
