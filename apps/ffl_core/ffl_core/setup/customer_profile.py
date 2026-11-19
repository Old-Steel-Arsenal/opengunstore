"""Customer profile fields: status, alerts, ID documents, date of birth, family.

One "Profile" tab on the Customer form, right after the Details tab, plus the
Identification section on the Details tab itself:

* **Status** — ``customer_status`` (Active / Banned, default Active) and
  ``status_reason`` (shown and required for Banned; enforced
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
* **Identification** (Details tab, above FFL Dealer) — ``date_of_birth`` and
  ``customer_ids`` → child doctype Customer ID Document. The ID number and the
  date of birth are kept only when FFL Settings → Store Customer ID Numbers is
  on (``feature_gate.customer_id_numbers_enabled``); otherwise every save blanks them. The two
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

Retired statuses (``RETIRED``): Inactive and Prospect were labels that stopped
nothing, so both become Active and the old status and reason go on the
customer's timeline. Disabled is NOT ticked for a former Inactive: the Woo and
CRM order paths still match a disabled customer by email, and ERPNext refuses a
sale to one, so staff tick it by hand. Leads belong in the CRM. Same raw-SQL
rule as above.

ERPNext's Settings-tab controls a one-company store never uses (``IDLE_CONTROLS``:
Company Restrictions, the two "Allow sales invoice creation without…" boxes,
Is Frozen, Block Customer) get a ``depends_on`` Property Setter so each shows
only on a customer where it is already set and can still be cleared there.
Disabled stays: it is the off switch. Seeded once per field; a store's own
``depends_on`` setter for that field is left alone.

Idempotent; wired into hooks.after_install and after_migrate. Never raises.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import escape_html

STATUSES = ("Active", "Banned")
RETIRED = ("Inactive", "Prospect")
# ERPNext Customer field → shown only while it (or the section's switch) is set.
IDLE_CONTROLS = {
	"company_restrictions_section": "restrict_to_companies",
	"so_required": "so_required",
	"dn_required": "dn_required",
	"is_frozen": "is_frozen",
	"block_customer_section": "on_hold",
}
RELATIONSHIPS = ("Spouse", "Child", "Parent", "Sibling", "Guest", "Other")
# Shown/required by the form; the server rule lives in customer_profile.
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
		"depends_on": _BANNED, "mandatory_depends_on": _BANNED,
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
	# On the Details tab, between the name and contact fields and FFL Dealer (the
	# fixtures anchor section_ffl on customer_ids): a Section Break anchored on the
	# first section's last core field closes that section.
	{"fieldname": "section_customer_ids", "fieldtype": "Section Break", "label": "Identification",
		"insert_after": "image"},
	{"fieldname": "date_of_birth", "fieldtype": "Date", "label": "Date of Birth",
		"no_copy": 1, "depends_on": IDS_ON, "insert_after": "section_customer_ids"},
	{"fieldname": "customer_ids", "fieldtype": "Table", "options": "Customer ID Document",
		"label": "ID Documents", "no_copy": 1, "insert_after": "date_of_birth"},
	{"fieldname": "section_customer_family", "fieldtype": "Section Break", "label": "Family",
		"insert_after": "customer_alerts"},
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
		_retire_statuses()
		_hide_idle_controls()
		return {"ok": 1}
	except Exception:
		frappe.log_error(
			title="customer_profile.ensure failed",
			message=frappe.get_traceback(),
		)
		return {"ok": 0}


def _retire_statuses() -> None:
	for row in frappe.get_all("Customer", filters={"customer_status": ("in", RETIRED)},
			fields=["name", "customer_status", "status_reason"]):
		note = _("Customer Status {0} was retired: now Active.").format(row.customer_status)
		if (row.status_reason or "").strip():
			note += " " + _("Reason was: {0}").format(escape_html(row.status_reason))
		frappe.db.set_value("Customer", row.name, {"customer_status": "Active", "status_reason": None},
			update_modified=False)
		frappe.get_doc("Customer", row.name).add_comment("Comment", note)


def _hide_idle_controls() -> None:
	meta = frappe.get_meta("Customer")
	for field, switch in IDLE_CONTROLS.items():
		if not meta.has_field(field) or frappe.db.exists("Property Setter", {
				"doc_type": "Customer", "field_name": field, "property": "depends_on"}):
			continue
		# make_property_setter clears the Customer cache itself.
		frappe.make_property_setter({"doctype": "Customer", "fieldname": field,
			"property": "depends_on", "value": f"eval:doc.{switch}"},
			validate_fields_for_doctype=False)
