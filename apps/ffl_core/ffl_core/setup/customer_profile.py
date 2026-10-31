"""Customer profile fields: status, alerts, ID documents, date of birth, family.

One "Profile" tab on the Customer form, right after the Details tab:

* **Status** — ``customer_status`` (Active / Inactive / Prospect / Banned,
  default Active) and ``status_reason`` (required for Banned; enforced
  server-side by ``ffl_core.customer_profile.validate_customer``). The two
  read-only ``ban_override_*`` fields are written only by the counter's
  manager override.
* **Alerts** — ``customer_alerts`` → child doctype Customer Alert.

Every personal-record field the form's Duplicate would otherwise carry is
``no_copy``: alerts, ID documents (numbers and attached copies), date of birth,
and the family link (``primary_customer`` / ``relationship``) — a duplicate is a
new person, so it must not inherit someone's IDs or alerts, nor silently become
a family member; "Add family member" is the way to put one in a family. ``ensure``
re-applies the definitions on every migrate, so a site that already has the
fields picks the flag up.
* **Identification** — ``date_of_birth`` and ``customer_ids`` → child doctype
  Customer ID Document. The ID number and the date of birth are kept only when
  FFL Settings → Store Customer ID Numbers is on (``feature_gate.
  customer_id_numbers_enabled``); otherwise every save blanks them. The two
  inputs are hidden by ``depends_on`` on the same boot flag — convenience only,
  the server rule is what holds.
* **Family** — ``primary_customer`` / ``relationship`` (one level only, see
  customer_profile) and an HTML slot the form script fills with the members.
* **Merged emails and GunBroker IDs** — ``customer_aliases`` → child doctype
  Customer Alias. Read-only, hidden while empty; ``merge_customer`` is the only
  writer. The Woo and GunBroker order resolvers read it so a merged-away
  customer's address or GB id still finds the customer that was kept.

Anchored on ``loyalty_program_tier`` (the last field of ERPNext's first tab):
frappe 16's ``Meta.sort_fields`` places a custom Tab Break at the END of its
anchor's tab, and places a field a ``field_order`` Property Setter has never
seen by its ``insert_after`` (meta.py ``sort_fields``) — so a Customize Form
save made before this shipped does not swallow the tab (pinned by
test_customer_profile).

Also brings every existing customer to ``customer_status = "Active"`` (rows the
column was added under, or written by a path that skipped defaults). Raw SQL
on purpose: no ``modified`` bump, no validate, nothing else on the row moves.

Idempotent; wired into hooks.after_install and after_migrate. Never raises.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

STATUSES = ("Active", "Inactive", "Prospect", "Banned")
RELATIONSHIPS = ("Spouse", "Child", "Parent", "Sibling", "Guest", "Other")
# Shown/required by the form; the server rule lives in customer_profile.
_NOT_ACTIVE = "eval:doc.customer_status && doc.customer_status!='Active'"
_BANNED = "eval:doc.customer_status=='Banned'"
HAS_ALIASES = "eval:(doc.customer_aliases||[]).length"
# Boot flag from ffl_integrations.boot (FFL Settings → Store Customer ID Numbers).
IDS_ON = "eval:frappe.boot.ffl_store_customer_id_numbers"

FIELDS = [
	{"fieldname": "customer_profile_tab", "fieldtype": "Tab Break", "label": "Profile",
		"insert_after": "loyalty_program_tier"},
	{"fieldname": "section_customer_status", "fieldtype": "Section Break", "label": "Status",
		"insert_after": "customer_profile_tab"},
	{"fieldname": "customer_status", "fieldtype": "Select", "label": "Customer Status",
		"options": "\n".join(STATUSES), "default": "Active", "in_standard_filter": 1,
		"insert_after": "section_customer_status"},
	{"fieldname": "status_reason", "fieldtype": "Small Text", "label": "Status Reason",
		"depends_on": _NOT_ACTIVE, "mandatory_depends_on": _BANNED,
		"insert_after": "customer_status"},
	# Its two fields show only for Banned; hiding the column with them lets Customer Status
	# take the section's width instead of sitting next to an empty column.
	{"fieldname": "column_customer_status", "fieldtype": "Column Break", "depends_on": _BANNED,
		"insert_after": "status_reason"},
	{"fieldname": "ban_override_until", "fieldtype": "Datetime", "label": "Ban Override Until",
		"read_only": 1, "no_copy": 1, "depends_on": _BANNED,
		"insert_after": "column_customer_status"},
	{"fieldname": "ban_override_by", "fieldtype": "Link", "options": "User",
		"label": "Ban Override By", "read_only": 1, "no_copy": 1, "depends_on": _BANNED,
		"insert_after": "ban_override_until"},
	{"fieldname": "section_customer_alerts", "fieldtype": "Section Break", "label": "Alerts",
		"insert_after": "ban_override_by"},
	{"fieldname": "customer_alerts", "fieldtype": "Table", "options": "Customer Alert",
		"label": "Alerts", "no_copy": 1, "insert_after": "section_customer_alerts"},
	{"fieldname": "section_customer_ids", "fieldtype": "Section Break", "label": "Identification",
		"insert_after": "customer_alerts"},
	{"fieldname": "date_of_birth", "fieldtype": "Date", "label": "Date of Birth",
		"no_copy": 1, "depends_on": IDS_ON, "insert_after": "section_customer_ids"},
	{"fieldname": "customer_ids", "fieldtype": "Table", "options": "Customer ID Document",
		"label": "ID Documents", "no_copy": 1, "insert_after": "date_of_birth"},
	{"fieldname": "section_customer_family", "fieldtype": "Section Break", "label": "Family",
		"insert_after": "customer_ids"},
	{"fieldname": "primary_customer", "fieldtype": "Link", "options": "Customer",
		"label": "Family Of", "search_index": 1, "no_copy": 1,
		"insert_after": "section_customer_family"},
	{"fieldname": "relationship", "fieldtype": "Select", "label": "Relationship",
		"options": "\n" + "\n".join(RELATIONSHIPS), "depends_on": "eval:doc.primary_customer",
		"no_copy": 1, "insert_after": "primary_customer"},
	{"fieldname": "column_customer_family", "fieldtype": "Column Break",
		"insert_after": "relationship"},
	{"fieldname": "family_members_html", "fieldtype": "HTML", "label": "Family Members",
		"insert_after": "column_customer_family"},
	# Written only by customer_profile.merge_customer; hidden until a merge has put
	# something there. no_copy: a duplicated customer must not claim the original's keys.
	{"fieldname": "section_customer_aliases", "fieldtype": "Section Break",
		"label": "Merged Emails and GunBroker IDs", "depends_on": HAS_ALIASES,
		"insert_after": "family_members_html"},
	{"fieldname": "customer_aliases", "fieldtype": "Table", "options": "Customer Alias",
		"label": "Merged Emails and GunBroker IDs", "read_only": 1, "no_copy": 1,
		"depends_on": HAS_ALIASES, "insert_after": "section_customer_aliases"},
]


def ensure() -> dict:
	"""Create the profile fields and default existing customers to Active.

	Idempotent; never raises.
	"""
	try:
		create_custom_fields(
			{"Customer": [{**f, "module": "FFL Core"} for f in FIELDS]}, ignore_validate=True)
		frappe.clear_cache(doctype="Customer")
		frappe.db.sql(
			"""update `tabCustomer` set customer_status = 'Active'
			where customer_status is null or customer_status = ''""")
		return {"ok": 1}
	except Exception:
		frappe.log_error(
			title="customer_profile.ensure failed",
			message=frappe.get_traceback(),
		)
		return {"ok": 0}
