"""Pre-order Deposits: the sub-ledger behind the Pre-order Deposits liability account.

One row per pre-order with money on it up to the date: what was collected, refunded and applied to
its bill, the balance still held, and how long it has been held (from the first payment). Read
from the submitted register invoices' PREORDER-DEPOSIT lines (``ffl_core.deposits``), the same
lines every other pre-order figure reads.

A register invoice books to the general ledger when it is merged into a Sales Invoice (the
nightly close, or at once for a firearm sale), so the summary sets the balance next to the
account's GL balance on the same date: a difference is money taken at the register and not
merged yet.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, nowdate

from ffl_core.setup.preorder import LINK_FIELD, PREORDER_ITEM, preorder_account

CHANNEL_COUNTER = "Counter"
# (upper bound in days, label); the last bucket has no bound.
AGING = ((30, "0–30"), (90, "31–90"), (180, "91–180"), (365, "181–365"), (None, ">365"))


def execute(filters=None):
	filters = _validate(filters)
	rows = build_rows(filters.company, filters.as_of_date)
	return _columns(), rows, None, None, _summary(rows, filters)


def _validate(filters) -> frappe._dict:
	filters = frappe._dict(filters or {})
	company = filters.get("company") or frappe.defaults.get_user_default("Company") \
		or frappe.db.get_single_value("Global Defaults", "default_company")
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Choose a company."))
	if not frappe.has_permission("Company", "read", doc=company):
		frappe.throw(_("You cannot read the books of {0}.").format(company), frappe.PermissionError)
	filters.company = company
	filters.as_of_date = getdate(filters.get("as_of_date") or nowdate())
	return filters


def aging_bucket(days: int) -> str:
	for bound, label in AGING:
		if bound is None or days <= bound:
			return label
	return AGING[-1][1]


def build_rows(company: str, as_of) -> list[dict]:
	"""One row per pre-order with a PREORDER-DEPOSIT line on a submitted register invoice dated on or
	before *as_of*: collected (payments), refunded (returns), applied (released on a bill), balance."""
	if not frappe.db.has_column("POS Invoice Item", LINK_FIELD):
		return []
	lines = frappe.db.sql(f"""
		SELECT i.`{LINK_FIELD}` AS sales_order, p.posting_date, p.is_return, i.amount
		FROM `tabPOS Invoice Item` i INNER JOIN `tabPOS Invoice` p ON p.name = i.parent
		WHERE p.docstatus = 1 AND p.company = %s AND p.posting_date <= %s AND i.item_code = %s
			AND IFNULL(i.`{LINK_FIELD}`, '') != ''
	""", (company, as_of, PREORDER_ITEM), as_dict=True)
	by_order: dict[str, frappe._dict] = {}
	for line in lines:
		o = by_order.setdefault(line.sales_order, frappe._dict(collected=0.0, refunded=0.0, applied=0.0, first=None))
		amount = flt(line.amount)
		if cint(line.is_return) and amount > 0:
			o.applied -= amount   # a hand-over ticket returned: what it took off is held again
		elif cint(line.is_return):
			o.refunded -= amount
		elif amount >= 0:
			o.collected += amount
			o.first = min(o.first, line.posting_date) if o.first else line.posting_date
		else:
			o.applied -= amount
	if not by_order:
		return []
	names = list(by_order)
	orders = {o.name: o for o in frappe.get_all("Sales Order", filters={"name": ["in", names]},
		fields=["name", "customer", "customer_name", "docstatus", "status"])}
	items: dict[str, list[str]] = {}
	for r in frappe.get_all("Sales Order Item", filters={"parent": ["in", names]},
			fields=["parent", "item_name", "item_code", "qty"], order_by="idx"):
		items.setdefault(r.parent, []).append(f"{flt(r.qty):g} × {r.item_name or r.item_code}")
	out = []
	for name in sorted(names, key=lambda n: (str(by_order[n].first or ""), n)):
		o, so = by_order[name], orders.get(name) or frappe._dict()
		balance = flt(o.collected - o.refunded - o.applied, 2)
		status = _("Cancelled") if cint(so.docstatus) == 2 else (_(so.status) if so.status in ("Completed", "Closed")
			else _("Open"))
		out.append({
			"sales_order": name, "channel": _(CHANNEL_COUNTER), "customer": so.customer,
			"customer_name": so.customer_name, "items": ", ".join(items.get(name, [])),
			"first_payment": o.first, "collected": flt(o.collected, 2), "refunded": flt(o.refunded, 2),
			"applied": flt(o.applied, 2), "balance": balance, "status": status,
			"aging": aging_bucket(date_diff(as_of, o.first)) if balance > 0.005 and o.first else "",
		})
	return out


def gl_balance(company: str, as_of) -> float:
	"""The Pre-order Deposits account's balance (credit positive) on *as_of*."""
	account = preorder_account(company)
	if not account:
		return 0.0
	return flt(frappe.db.sql("""SELECT COALESCE(SUM(credit - debit), 0) FROM `tabGL Entry`
		WHERE account = %s AND is_cancelled = 0 AND posting_date <= %s""", (account, as_of))[0][0], 2)


def _summary(rows, filters) -> list[dict]:
	held = flt(sum(r["balance"] for r in rows), 2)
	gl = gl_balance(filters.company, filters.as_of_date)
	diff = flt(held - gl, 2)
	return [
		{"label": _("Held"), "value": held, "datatype": "Currency", "indicator": "Blue"},
		{"label": _("General ledger"), "value": gl, "datatype": "Currency", "indicator": "Blue"},
		{"label": _("Not posted yet"), "value": diff, "datatype": "Currency",
			"indicator": "Green" if abs(diff) < 0.005 else "Orange"},
	]


def _columns() -> list[dict]:
	return [
		{"fieldname": "sales_order", "label": _("Pre-order"), "fieldtype": "Link", "options": "Sales Order", "width": 140},
		{"fieldname": "channel", "label": _("Channel"), "fieldtype": "Data", "width": 90},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 120},
		{"fieldname": "customer_name", "label": _("Customer Name"), "fieldtype": "Data", "width": 160},
		{"fieldname": "items", "label": _("Items"), "fieldtype": "Data", "width": 220},
		{"fieldname": "first_payment", "label": _("First Payment"), "fieldtype": "Date", "width": 110},
		{"fieldname": "collected", "label": _("Collected"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "refunded", "label": _("Refunded"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "applied", "label": _("Applied"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "balance", "label": _("Balance"), "fieldtype": "Currency", "width": 110},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
		{"fieldname": "aging", "label": _("Age (days)"), "fieldtype": "Data", "width": 90},
	]
