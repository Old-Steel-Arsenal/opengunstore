"""Whose shop is this — name, logo, address and receipt footer, per Company.

ONE source of truth for the branding that reaches a customer or a partner
dealer, so a second store's site prints its own identity instead of the first
store's. The two receipt print formats, the two Email Templates and the
consignment dealer portal all call `company_brand()`; none of them reaches for
Company fields on its own (that is how one store's name got hardcoded into
five places to begin with).

Exposed to Jinja as the global `company_brand` through hooks.py's `jinja` hook,
so Print Formats and Email Templates can call it with no import.

Every value degrades to "" instead of raising: a fresh site has no logo, no
Address row and no footer note, and a receipt still has to print.
"""
from __future__ import annotations

import frappe
from frappe.utils import get_url

from ffl_core.utils import single_raw

_FIELDS = ("company_name", "company_logo", "phone_no")


def company_brand(company: str | None) -> dict:
	"""Branding for `company`: name / logo_url / address_line / footer_note.

	Args:
		company: Company docname, e.g. ``doc.company``. Blank or unknown is fine
			and yields empty strings throughout.

	Returns:
		dict with ``name`` (Company.company_name), ``logo_url``, ``address_line``
		(the company's own address on one line) and ``footer_note``
		(FFL Settings.receipt_footer_note, site-wide).

		``logo_url`` is absolute because the PDF renderer cannot resolve a
		site-relative path. It must also point at a PUBLIC file: a logo attached
		as private (``/private/files/…``) needs a session the PDF renderer does
		not have, and prints as a broken image rather than nothing.

	Deliberately no ``email`` key. A contact address is never one thing: the
	dealer portal wants whoever runs consignment
	(the consignment extension's contact_email setting), and putting Company.email here
	would offer every print format a plausible-looking address to print — which
	is how a consignment alias ends up on a customer's sales receipt.
	"""
	company = (company or "").strip()
	row = frappe.db.get_value("Company", company, _FIELDS, as_dict=True) if company else None
	row = row or {}
	logo = row.get("company_logo")
	return {
		"name": row.get("company_name") or company,
		"logo_url": get_url(logo) if logo else "",
		"address_line": _address_line(company),
		# Store phone for receipts / letter heads that want a contact line.
		# Company.phone_no is set by store_setup from store.env; blank -> "".
		"phone": row.get("phone_no") or "",
		# Site-wide, not per-Company: each store runs its own site, and the note
		# is a shop policy line rather than a legal-entity attribute.
		# `single_raw` and not `get_single_value` — the codebase's settings reader
		# (see its docstring): direct SQL, so it neither goes stale behind the
		# singles cache nor needs the DocType meta loaded.
		"footer_note": single_raw("FFL Settings", "receipt_footer_note") or "",
	}


def company_address(company: str) -> str | None:
	"""The Address row a receipt for `company` prints — deterministically.

	NOT ``frappe.contacts…get_default_address``: that filters only on the
	Dynamic Link and ``disabled=0`` and orders by ``is_primary_address DESC``
	with no tiebreaker, so a Company carrying two addresses prints whichever one
	the database hands back first, and it can differ between two runs. A
	receipt header cannot be arbitrary.

	So: the company's OWN addresses only (``is_your_company_address``, which
	ERPNext stamps on every Address linked to a Company —
	erpnext/accounts/custom/address.py::update_company_address), preferring the
	primary one, then oldest-created, then name. The last key is what makes it
	total.

	One place answers "does this company already print an address", the same
	way for every caller.
	"""
	if not company:
		return None
	rows = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Company"],
			["Dynamic Link", "link_name", "=", company],
			["is_your_company_address", "=", 1],
			["disabled", "=", 0],
		],
		pluck="name",
		order_by="is_primary_address desc, creation asc, name asc",
		limit=1,
	)
	return rows[0] if rows else None


def _address_line(company: str) -> str:
	"""The company's own address on ONE line, e.g. "12 Main St 40, Springfield, TX 70001".

	Only the join is ours: ``get_address_display`` renders the multi-line Address
	Template and a receipt header has one line. Country is deliberately dropped —
	a store prints its own domestic address.
	"""
	name = company_address(company)
	if not name:
		return ""
	a = frappe.db.get_value(
		"Address", name,
		["address_line1", "address_line2", "city", "state", "pincode"], as_dict=True) or {}
	street = ", ".join(p for p in (a.get("address_line1"), a.get("address_line2")) if p)
	region = " ".join(p for p in (a.get("state"), a.get("pincode")) if p)
	return ", ".join(p for p in (street, a.get("city"), region) if p)


def extend_bootinfo(bootinfo) -> None:
	"""``frappe.boot.app_logo_dark``: Navbar Settings' Dark Mode Logo (custom field), or "".

	The desk swaps it in for the Application Logo under the dark theme (ffl_core.bundle.js);
	Frappe has one logo field and no dark variant.
	"""
	bootinfo.app_logo_dark = frappe.db.get_single_value("Navbar Settings", "app_logo_dark") or ""
