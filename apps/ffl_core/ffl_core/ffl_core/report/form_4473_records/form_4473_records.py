"""Form 4473 Records — every completed 4473 on file, newest first, with its PDF.

Reads the Form 4473 Log (ffl_core doctype), the durable record the
form4473.completed webhook and the paper / manager-override path write: a 4473
stays listed after its draft is deleted (Clear Abandoned Order, a Denied
buyer's draft) or re-run with Redo 4473, and whatever the NICS outcome.
FastBound's API cannot list completed 4473s, which is why the POS keeps its
own. Order Status says what became of the order since. The PDF is served by
ffl_integrations.fastbound.pos_4473.view_4473_pdf (see the .js formatter).

An extension that checks 4473s says where each record's check stands through the
``gunshop_4473_record_checks`` hook; the Check column shows only when one answers.
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import getdate

_ORDER_STATUS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
RECORD_CHECKS_HOOK = "gunshop_4473_record_checks"
_INDICATORS = ("green", "orange", "red", "gray")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	rows = _rows(filters)
	return _columns(checked=any(row["check"] for row in rows)), rows


def _columns(checked: bool = False):
	return [
		{"label": _("Completed On"), "fieldname": "completed_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Record"), "fieldname": "log", "fieldtype": "Link",
			"options": "Form 4473 Log", "width": 120},
		{"label": _("Invoice"), "fieldname": "pos_invoice", "fieldtype": "Data", "width": 170},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link",
			"options": "Customer", "width": 170},
		{"label": _("Firearms"), "fieldname": "serials", "fieldtype": "Data", "width": 180},
		{"label": _("NICS"), "fieldname": "nics_status", "fieldtype": "Data", "width": 90},
		{"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 130},
		{"label": _("Order Status"), "fieldname": "order_status", "fieldtype": "Data", "width": 110},
		{"label": _("4473 PDF"), "fieldname": "pdf", "fieldtype": "Data", "width": 90},
		{"label": _("PDF Copy"), "fieldname": "pdf_status", "fieldtype": "Data", "width": 90},
		{"label": _("Check"), "fieldname": "check", "fieldtype": "Data", "width": 130, "hidden": 0 if checked else 1},
		{"label": _("Has PDF"), "fieldname": "has_pdf", "fieldtype": "Check", "hidden": 1},
		{"label": _("Check Indicator"), "fieldname": "check_indicator", "fieldtype": "Data", "hidden": 1},
		{"label": _("Check Route"), "fieldname": "check_route", "fieldtype": "Data", "hidden": 1},
	]


def _rows(filters):
	conditions = []
	if filters.get("from_date"):
		conditions.append(["completed_on", ">=", getdate(filters.from_date)])
	if filters.get("to_date"):
		conditions.append(["completed_on", "<", getdate(filters.to_date) + timedelta(days=1)])
	for field in ("customer", "nics_status", "source"):
		if filters.get(field):
			conditions.append([field, "=", filters[field]])
	if (filters.get("serial") or "").strip():
		conditions.append(["serials", "like", f"%{filters.serial.strip()}%"])

	# get_list, not raw SQL: the user's permissions (and User Permissions) apply.
	logs = frappe.get_list("Form 4473 Log", filters=conditions, limit_page_length=0,
		fields=["name", "completed_on", "pos_invoice", "customer", "serials", "nics_status",
			"source", "form4473_id", "fastbound_disposition_id", "pdf_file", "pdf_status"],
		order_by="completed_on desc, name desc")
	invoices = {i for log in logs for i in _invoices(log)}
	orders = {o.name: o for o in frappe.get_all("POS Invoice",
		filters={"name": ("in", list(invoices))},
		fields=["name", "docstatus", "fastbound_disposition_id"])} if invoices else {}

	checks = _record_checks(logs)
	out = []
	for log in logs:
		has_pdf = 1 if (log.form4473_id or log.pdf_file) else 0
		check = checks.get(log.name) or {}
		out.append({
			"completed_on": log.completed_on,
			"log": log.name,
			"pos_invoice": log.pos_invoice,
			"customer": log.customer,
			"serials": log.serials,
			"nics_status": log.nics_status,
			"source": log.source,
			"order_status": _order_status(log, orders),
			"pdf": _("View PDF") if has_pdf else "",
			"pdf_status": log.pdf_status,
			"has_pdf": has_pdf,
			"check": check.get("label", ""),
			"check_indicator": check.get("indicator", ""),
			"check_route": check.get("route", ""),
		})
	return out


def _record_checks(logs) -> dict:
	"""``{log name: {"label", "indicator", "route"}}`` from ``gunshop_4473_record_checks``
	providers; the first to answer for a record wins. A provider that fails is logged and
	answers nothing, and an answer is cut down to what the column can safely show: a short
	label, a known indicator colour, a route inside the desk."""
	asked = [{"name": log.name, "pos_invoices": _invoices(log), "source": log.source} for log in logs]
	out: dict = {}
	for hook in frappe.get_hooks(RECORD_CHECKS_HOOK) if asked else []:
		try:
			answers = frappe.get_attr(hook)(asked) or {}
		except Exception:
			frappe.log_error(title=f"{RECORD_CHECKS_HOOK} provider failed: {hook}")
			continue
		for name, answer in answers.items():
			label = str((answer or {}).get("label") or "").strip()[:40]
			if name in out or not label:
				continue
			route = str(answer.get("route") or "")
			out[name] = {"label": label,
				"indicator": answer.get("indicator") if answer.get("indicator") in _INDICATORS else "gray",
				"route": route if route.startswith(("/app/", "/desk/")) else ""}
	return out


def _invoices(log) -> list[str]:
	return [i for i in (log.pos_invoice or "").split(", ") if i]


def _order_status(log, orders: dict) -> str:
	"""What became of the order this 4473 ran on — its latest invoice."""
	invoices = _invoices(log)
	if not invoices:
		return "No POS order"
	order = orders.get(invoices[-1])
	if order is None:
		return "Deleted"
	# Redo 4473 blanks the invoice's disposition id and the next 4473 sets a new
	# one: either way it no longer points at this 4473.
	if log.fastbound_disposition_id and order.fastbound_disposition_id != log.fastbound_disposition_id:
		return "Redone"
	return _ORDER_STATUS.get(order.docstatus, "")
