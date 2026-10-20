"""The POS shift the test fixtures sell in: an Open POS Opening Entry that started today.

ERPNext refuses a POS Invoice whose profile's newest Open entry started on another day ("POS Opening Entry - … is
outdated") and a new opening while the profile or the cashier has one Open. The fixtures used to reuse any Open entry,
so on a dev site kept from a run the day before every module that sells at the counter errored.
"""
from __future__ import annotations

import frappe
from frappe.utils import get_date_str, now_datetime, nowdate, today


def close_stale_pos_openings() -> None:
	"""Mark Closed every Open POS Opening Entry that started before today: a test site's shift left open by an earlier
	run, which blocks both the day's sales and a new opening. Status only — no POS Closing Entry, nothing counted."""
	for row in frappe.get_all("POS Opening Entry", filters={"status": "Open", "docstatus": 1}, fields=["name", "period_start_date"]):
		if get_date_str(row.period_start_date) != today():
			frappe.db.set_value("POS Opening Entry", row.name, "status", "Closed", update_modified=False)


def todays_pos_opening(company: str, pos_profile: str = "FFL Test") -> str:
	"""The session user's Open POS Opening Entry on ``pos_profile``, started today: reused, or opened.

	An Open entry of the user's on another profile is marked Closed first: ERPNext allows a cashier one Open entry,
	and a module selling on its own profile (the gunsmith's) otherwise ran after one that opened ``FFL Test`` with
	no shift for its profile at all."""
	close_stale_pos_openings()
	open_entry = {"user": frappe.session.user, "status": "Open", "docstatus": 1}
	name = frappe.db.get_value("POS Opening Entry", {**open_entry, "pos_profile": pos_profile})
	if name:
		return name
	for other in frappe.get_all("POS Opening Entry", filters=open_entry, pluck="name"):
		frappe.db.set_value("POS Opening Entry", other, "status", "Closed", update_modified=False)
	opening = frappe.get_doc({
		"doctype": "POS Opening Entry",
		"company": company,
		"pos_profile": pos_profile,
		"user": frappe.session.user,
		"period_start_date": now_datetime(),
		"posting_date": nowdate(),
		"balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
	})
	opening.flags.ignore_permissions = True
	opening.insert()
	opening.submit()
	return opening.name
