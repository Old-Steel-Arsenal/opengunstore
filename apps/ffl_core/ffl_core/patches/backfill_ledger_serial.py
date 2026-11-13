"""Serial No.ledger_serial (and the A&D records' copy) for every gun already on file.

Every existing gun is named by its bound-book serial, so its ledger serial IS its
name. Fills blanks only, so re-running changes nothing and a value set since (an
imported no-serial gun) is never overwritten. Rolling back is not reading the field:
ffl_core.serials.ledger_serial falls back to the name.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	# Fixtures sync AFTER post_model_sync patches, so on the migrate that brings the
	# field its column does not exist yet. Ensure it (idempotent; the fixture sync later
	# in the same migrate sets the full field properties). Same as backfill_serial_firearm_type.
	create_custom_fields(
		{"Serial No": [
			{"fieldname": "ledger_serial", "label": "Ledger Serial", "fieldtype": "Data",
				"insert_after": "serial_no", "read_only": 1, "search_index": 1, "module": "FFL Core"},
		]},
		ignore_validate=True,
	)
	frappe.db.sql("""
		UPDATE `tabSerial No` SET ledger_serial = name
		WHERE IFNULL(ledger_serial, '') = ''""")
	# The A&D copies: the Serial No's ledger serial, else the record's own serial.
	for doctype in ("FFL Acquisition", "FFL Disposition"):
		frappe.db.sql(f"""
			UPDATE `tab{doctype}` d
			LEFT JOIN `tabSerial No` sn ON sn.name = d.serial
			SET d.ledger_serial = COALESCE(NULLIF(sn.ledger_serial, ''), d.serial)
			WHERE IFNULL(d.ledger_serial, '') = '' AND IFNULL(d.serial, '') != ''""")
