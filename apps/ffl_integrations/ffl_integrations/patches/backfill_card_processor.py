"""Name the card processor on every card charge recorded before charges carried one.

``rename_payroc_storage_to_card`` (pre_model_sync) renamed the storage; the model sync then
added ``processor`` to Card Charge and Card Terminal Payment. Every row that existed before was
taken by Payroc, the only processor there was, so blanks become ``payroc``. A row that already
names its processor is never touched, so a rerun changes nothing.
"""
from __future__ import annotations

import frappe

LEGACY_PROCESSOR = "payroc"


def execute() -> None:
	for doctype in ("Card Charge", "Card Terminal Payment"):
		if frappe.db.has_column(doctype, "processor"):
			frappe.db.sql(f"""update `tab{doctype}` set processor = %s
				where ifnull(processor, '') = ''""", (LEGACY_PROCESSOR,))
