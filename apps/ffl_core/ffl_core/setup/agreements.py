"""Agreement fields on Item and Customer (rules in ``ffl_core.agreements``).

* **Item** — ``ffl_required_agreement`` (Link Agreement Template): the buyer must
  have signed it before a counter sale of this Item is submitted
  (``agreements.refuse_missing_required``). Next to the Firearm flags on the
  first tab.
* **Item Group** — the same field, alone in the form's right column: every Item in the group and its sub-groups needs it,
  unless the Item names its own; a used Item naming none takes FFL Settings › Used Item
  Agreement (``agreements.required_for_item``).
* **Customer** — an "Agreements" section on the Profile tab, after the merged
  aliases: one HTML slot the form script fills with the customer's copies and the
  Request agreement button.

Custom fields only, so it is safe at after_install. Idempotent; never raises.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDS = {
	"Item": [
		{"fieldname": "ffl_required_agreement", "fieldtype": "Link", "options": "Agreement Template",
			"label": "Agreement Required to Buy", "insert_after": "is_antique",
			"description": "The customer must sign this before a counter sale; empty uses the Item Group's."},
	],
	"Item Group": [
		{"fieldname": "ffl_required_agreement", "fieldtype": "Link", "options": "Agreement Template",
			"label": "Agreement Required to Buy", "insert_after": "column_break_5",
			"description": "Items in this group need it signed before a counter sale, unless the Item names its own."},
	],
	"Customer": [
		{"fieldname": "section_customer_agreements", "fieldtype": "Section Break", "label": "Agreements",
			"insert_after": "customer_aliases"},
		{"fieldname": "customer_agreements_html", "fieldtype": "HTML", "label": "Agreements",
			"insert_after": "section_customer_agreements"},
	],
}


def ensure() -> dict:
	"""Create the agreement fields. Idempotent; never raises."""
	try:
		create_custom_fields({dt: [{**f, "module": "FFL Core"} for f in rows] for dt, rows in FIELDS.items()},
			ignore_validate=True)
		for dt in FIELDS:
			frappe.clear_cache(doctype=dt)
		# Templates saved before Required / Signed existed keep what they meant then.
		if frappe.db.table_exists("Agreement Template") and frappe.db.has_column("Agreement Template", "requirement"):
			frappe.db.sql("""update `tabAgreement Template` set requirement = 'Required by item'
				where ifnull(requirement, '') = ''""")
			frappe.db.sql("""update `tabAgreement Template` set scope = 'Per customer'
				where ifnull(scope, '') = ''""")
		return {"ok": 1}
	except Exception:
		frappe.log_error(title="agreements.ensure failed", message=frappe.get_traceback())
		return {"ok": 0}
