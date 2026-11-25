"""Customer/Supplier ↔ ATF FFL Record linkage.

The ATF directory record (name == FFL number) is the source of truth for a
dealer party's license facts. `atf_ffl_record` is the formal Link; on every
party save the license fields are synced FROM the record, so a typo in the
free-text ffl_number can no longer desync the Search-ATF fill, the shipping
gate and the disposition destination. Parties created before the link existed
heal automatically: a bare ffl_number that matches a directory record
backfills the link on the next save.

The California CFD number is the one field the record does not get from ATF:
staff type it by hand, on the record or on any party linked to it. It lives on
the record (one value per FFL) and every linked party carries a copy.
"""
from __future__ import annotations

import re

import frappe
from frappe import _


def sync_party_atf_link(doc, method: str | None = None) -> None:
	"""Customer + Supplier validate hook."""
	if not int(doc.get("is_ffl_dealer") or 0):
		return
	record = (doc.get("atf_ffl_record") or "").strip()
	if not record:
		# Old data heals: bare FFL number that exists in the directory →
		# adopt it as the formal link (then sync from it below).
		number = (doc.get("ffl_number") or "").strip()
		if not number or not frappe.db.exists("ATF FFL Record", number):
			return
		record = number
		doc.atf_ffl_record = record

	rec = frappe.db.get_value(
		"ATF FFL Record", record,
		["ffl_number", "license_type", "licensee_name", "licensee_dba",
			"street", "street2", "city", "state", "zip", "expiry_date", "cfd_number"],
		as_dict=True,
	)
	if not rec:
		frappe.throw(_("ATF FFL Record {0} not found.").format(record))

	# The directory is authoritative for everything it knows.
	doc.ffl_number = rec.ffl_number
	if rec.licensee_name:
		doc.licensee_name = rec.licensee_name
	address = ", ".join(p for p in (
		rec.street, rec.street2, rec.city, " ".join(p for p in (rec.state, rec.zip) if p),
	) if p)
	if address:
		doc.licensee_address = address
	if rec.expiry_date:
		doc.ffl_expiry_date = rec.expiry_date
	code = _license_code(rec.license_type)
	if code:
		doc.ffl_license_type = code

	# CFD: a hand edit on the party is written to the record (and its other
	# parties); otherwise the party takes the record's value.
	cfd = (doc.get("cfd_number") or "").strip() or None
	edited = bool(cfd) if doc.is_new() else doc.has_value_changed("cfd_number")
	if edited and cfd != (rec.cfd_number or None):
		push_cfd_number(record, cfd, skip=(doc.doctype, doc.name))
	doc.cfd_number = cfd if edited else rec.cfd_number


def push_cfd_number(record: str, cfd: str | None, skip: tuple | None = None) -> None:
	"""Store `cfd` on the ATF FFL Record and on every party linked to it."""
	frappe.db.set_value("ATF FFL Record", record, "cfd_number", cfd, update_modified=False)
	for party_type in ("Customer", "Supplier"):
		for name in frappe.get_all(party_type, {"atf_ffl_record": record}, pluck="name"):
			if (party_type, name) != skip:
				frappe.db.set_value(party_type, name, "cfd_number", cfd, update_modified=False)


def _license_code(license_type: str | None) -> str | None:
	"""'01 - Dealer in Firearms' → '01' (the party field is a 2-digit Select)."""
	m = re.match(r"\s*(\d{2})", license_type or "")
	return m.group(1) if m else None
