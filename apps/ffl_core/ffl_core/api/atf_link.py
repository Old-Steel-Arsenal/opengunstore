"""Build a dealer Customer/Supplier straight from an ATF FFL Record.

The natural B2B onboarding path: find the dealer in the directory (or pull
them in live via "+ Lookup by FFL #"), then one click creates the party with
is_ffl_dealer set and the formal atf_ffl_record link — the validate sync
(ffl_core.atf_link) fills the license fields from the record. Idempotent:
a party already carrying this FFL number is returned instead of duplicated.
"""
from __future__ import annotations

import frappe
from frappe import _

_PARTY_NAME_FIELD = {"Customer": "customer_name", "Supplier": "supplier_name"}


@frappe.whitelist()
def create_party_from_atf(record: str, party_type: str) -> dict:
	"""Create a dealer Customer/Supplier from an ATF FFL Record (idempotent).

	Sets is_ffl_dealer and the atf_ffl_record link; the party's validate
	hook then fills the license fields from the record. If a party already
	carries the record's FFL number it is returned as-is (the link is
	healed if missing) — never duplicated or renamed.

	Args:
	    record: ATF FFL Record name (the directory row to build from).
	    party_type: "Customer" or "Supplier".

	Returns:
	    {"name": <party name>, "created": bool} — created=False when an
	    existing party with this FFL number was reused.

	Permissions:
	    Sales User / Purchase User / Stock Manager / System Manager.
	"""
	frappe.only_for(("Sales User", "Purchase User", "Stock Manager", "System Manager"))
	return _create_party_from_atf(record, party_type)


def _create_party_from_atf(record: str, party_type: str, display_name: str | None = None) -> dict:
	"""The gate-free body of create_party_from_atf. Split out so
	dealer_onboarding.onboard_dealer can reuse it under ITS OWN, DIFFERENT role
	gate (_CONSIGNMENT_ROLES includes Sales Manager, which the public wrapper's
	only_for does NOT) — calling the whitelisted wrapper would re-run that
	mismatched only_for against the same session and reject a legitimate Sales
	Manager. `display_name`, when given, overrides the auto-picked party name
	(the onboarding page's editable Store name); it is ignored when an existing
	party is returned (idempotent — we never rename a party that's already on
	file)."""
	if party_type not in _PARTY_NAME_FIELD:
		frappe.throw(_("party_type must be Customer or Supplier."))
	rec = frappe.db.get_value(
		"ATF FFL Record", record,
		["ffl_number", "licensee_name", "licensee_dba", "cfd_number"], as_dict=True)
	if not rec:
		frappe.throw(_("ATF FFL Record {0} not found.").format(record))

	existing = frappe.db.get_value(
		party_type, {"ffl_number": rec.ffl_number}, "name")
	if existing:
		# Heal the link on the way out, but don't duplicate the party.
		if not frappe.db.get_value(party_type, existing, "atf_ffl_record"):
			frappe.db.set_value(party_type, existing,
				{"atf_ffl_record": record, "cfd_number": rec.cfd_number},
				update_modified=False)
		return {"name": existing, "created": False}

	display = (display_name or "").strip() or rec.licensee_dba or rec.licensee_name or rec.ffl_number
	doc = frappe.new_doc(party_type)
	doc.set(_PARTY_NAME_FIELD[party_type], display)
	doc.is_ffl_dealer = 1
	doc.atf_ffl_record = record
	if party_type == "Customer":
		# Explicit: a licensed business, whatever the site's default Customer Type is.
		doc.customer_type = "Company"
		doc.customer_group = _customer_group()
		doc.territory = frappe.db.get_single_value(
			"Selling Settings", "territory") or _first_leaf("Territory")
	else:
		doc.supplier_group = _first_leaf("Supplier Group")
	doc.flags.ignore_permissions = True
	doc.insert()  # validate hook syncs the license fields from the record
	return {"name": doc.name, "created": True}


def _customer_group() -> str | None:
	return frappe.db.get_single_value("Selling Settings", "customer_group") \
		or _first_leaf("Customer Group")


def _first_leaf(doctype: str) -> str | None:
	return frappe.db.get_value(doctype, {"is_group": 0}, "name")


# Public, GATE-FREE name for first-party extensions,
# which run it behind their own role gate — osa_consignment's dealer onboarding
# needs `display_name` and admits Sales Manager, which the endpoint's only_for
# does not.
#
# ⚠ NOT `create_party_from_atf`. Binding it to that name replaced the
# whitelisted endpoint above with this un-whitelisted function, and every
# browser call died with "Function ffl_core.api.atf_link._create_party_from_atf
# is not whitelisted" — the Create Order page could not resolve a Business
# (FFL) buyer, and the ATF FFL Record form's Create Customer/Supplier button
# was dead, from 2026-09-11 (dba43a58) until 2026-09-18. The module attribute
# is what Frappe resolves a /api/method/ path to; a later assignment silently
# wins over the decorator. `find_shadowed_endpoints` now fails the build on
# any repeat, in any app.
build_party_from_atf = _create_party_from_atf
