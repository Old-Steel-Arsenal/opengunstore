"""One-shot — give every Each firearm sold copy its Firearms row.

Copies made before one copy could cover several guns name their gun only in
``serial_no``; the sale gate now reads the ``firearms`` table
(``agreements.agreement_status``). Idempotent: a copy that has rows is skipped.
"""
from __future__ import annotations

import frappe


def execute() -> None:
	have = set(frappe.get_all("Customer Agreement Firearm", filters={"parenttype": "Customer Agreement"},
		pluck="parent"))
	for r in frappe.get_all("Customer Agreement", filters={"serial_no": ("is", "set")},
			fields=["name", "serial_no", "item_code"]):
		if r.name in have:
			continue
		frappe.get_doc({"doctype": "Customer Agreement Firearm", "parent": r.name, "parenttype": "Customer Agreement",
			"parentfield": "firearms", "idx": 1, "serial_no": r.serial_no, "item_code": r.item_code}).db_insert()
