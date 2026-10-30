"""Structured individual name on the Customer — First / Middle / Last / Suffix.

ERPNext's Customer carries only a single free-text ``customer_name``; its
standard ``first_name`` / ``last_name`` are HIDDEN, Read-Only mirrors fetched
from the primary Contact, so an FFL's bound-book identity had nowhere to be
entered on the customer itself — the "Add new customer" form and quick-entry
showed only Customer Name.

This adds four editable, quick-entry name parts at the top of the right
column of the top section, the same structured identity the POS Trade-in /
Receive Goods forms capture and that ``ffl_core.api.trade_in.seller_defaults`` reads to prefill
them. They are CUSTOM fields (``ffl_first_name`` / ``middle_name`` /
``ffl_last_name`` / ``name_suffix``): Frappe honours ``insert_after`` for
custom fields but NOT for standard ones, so the standard first_name/last_name
mirrors are left hidden and untouched, and seller_defaults coalesces our
fields, then those mirrors, then a display-name split. A dedicated Section
Break was avoided on purpose — anchored near the top it swallows the following
customer_group, and anchored lower it collides with the FFL-Dealer section
chain; a plain field run above Customer Group is unambiguous and self-labelled.

``customer_name`` (the docname — this site's ``cust_master_name`` is "Customer
Name") is kept in sync with the parts server-side by
``ffl_core.customer_name.sync_individual_name`` (before_insert runs before
autoname, so a parts-only insert gets its docname composed; validate re-splits
the parts when the display name is corrected). The quick-entry dialog
(public/js/customer_quick_entry.js) composes it live and read-only; the full
form's customer_form.js only fills it when blank. ``_SUFFIX_OPTIONS`` below is
the single source of the suffix spellings — the Python map and the JS parsers
derive from / are pinned to it by test_customer_name.

A new customer defaults to Customer Type = Individual (a Property Setter on
``Customer.customer_type.default``, seeded once by ``ensure``): a retail gun
counter almost always means a person, and ERPNext's own default of Company hid
the name fields. Every server path that must create a Company (an FFL dealer /
business) says so explicitly — ``api/atf_link._create_party_from_atf`` — so the
default cannot turn a dealer into an Individual.

Idempotent; wired into hooks.after_install / after_migrate. Never raises."""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Show the name parts only for individuals — a Company/Partnership customer
# keeps the single Customer Name field.
_INDIVIDUAL = "eval:doc.customer_type=='Individual'"

_SUFFIX_OPTIONS = "\nJr\nSr\nII\nIII\nIV\nV\nVI"


def ensure() -> dict:
	"""Create the individual name parts. Idempotent; never raises."""
	try:
		create_custom_fields({
			"Customer": [
				{
					"fieldname": "ffl_first_name",
					"fieldtype": "Data",
					"label": "First Name",
					# Top of the right column, above Customer Group: under Gender
					# they made the left column nine deep beside a lone Customer Group.
					"insert_after": "column_break0",
					"depends_on": _INDIVIDUAL,
					"allow_in_quick_entry": 1,
					"module": "FFL Core",
				},
				{
					"fieldname": "middle_name",
					"fieldtype": "Data",
					"label": "Middle Name",
					"insert_after": "ffl_first_name",
					"depends_on": _INDIVIDUAL,
					"allow_in_quick_entry": 1,
					"module": "FFL Core",
				},
				{
					"fieldname": "ffl_last_name",
					"fieldtype": "Data",
					"label": "Last Name",
					"insert_after": "middle_name",
					"depends_on": _INDIVIDUAL,
					"allow_in_quick_entry": 1,
					"module": "FFL Core",
				},
				{
					"fieldname": "name_suffix",
					"fieldtype": "Select",
					"label": "Suffix",
					"options": _SUFFIX_OPTIONS,
					"insert_after": "ffl_last_name",
					"depends_on": _INDIVIDUAL,
					"allow_in_quick_entry": 1,
					"module": "FFL Core",
				},
			]
		}, ignore_validate=True)
		_seed_default_customer_type()
		frappe.clear_cache(doctype="Customer")
		return {"ok": 1}
	except Exception:
		frappe.log_error(
			title="customer_identity.ensure failed",
			message=frappe.get_traceback(),
		)
		return {"ok": 0}


def _seed_default_customer_type() -> None:
	"""Default Customer Type = Individual, unless the site already has a say.

	Seeded once and then left alone: a Property Setter someone made in Customize
	Form (even back to Company) is the store's decision and survives migrates.
	"""
	if frappe.db.exists("Property Setter", {"doc_type": "Customer",
			"field_name": "customer_type", "property": "default"}):
		return
	frappe.make_property_setter({"doctype": "Customer", "fieldname": "customer_type",
		"property": "default", "value": "Individual", "property_type": "Text"})
