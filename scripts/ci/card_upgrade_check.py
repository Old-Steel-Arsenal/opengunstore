#!/usr/bin/env python3
"""Upgrade check for the card-charge storage rename (``ffl_integrations.patches.rename_payroc_storage_to_card``).

    cd frappe-bench/sites && ../env/bin/python <repo>/scripts/ci/card_upgrade_check.py seed   <site>
    bench --site <site> migrate
    cd frappe-bench/sites && ../env/bin/python <repo>/scripts/ci/card_upgrade_check.py verify <site>

``seed`` runs on the site BEFORE the migrate, while it still has the old shape (POS Invoice
``payroc_*`` columns, ``Payroc Charge`` / ``Payroc Terminal Payment`` tables), and writes raw rows
straight into those tables — no app code, so it works whatever release built the site: a charged
multi-card sale, a return with a partial-refund lock and booked money, a legacy stamp-only sale,
a counter terminal payment, a deleted draft in the recycle bin, and a Property Setter on a renamed
field. It records what it wrote in ``card_upgrade_seed.json`` beside the site. On a site that
already has the new shape there is nothing to rename: it says so and writes no file.

``verify`` runs AFTER the migrate and checks every seeded value under the new names (scalars,
charge rows loaded through the ORM — which proves ``parentfield`` moved —, terminal payments, the
recycle-bin JSON, the Property Setter), that ``processor`` was backfilled, and that no old table,
column or Custom Field is left. Then it deletes what ``seed`` wrote. No file: nothing to verify.

lazy: self-retiring — once main carries the new shape every upgrade run seeds nothing; delete
this script and its two workflow steps one release after the rename ships.
"""
from __future__ import annotations

import json
import os
import sys

import frappe

MARKER = "card_upgrade_seed.json"
SALE, RETURN, LEGACY, DRAFT = "CARD-UPG-SALE", "CARD-UPG-RET", "CARD-UPG-LEGACY", "CARD-UPG-DRAFT"
TP = "card-upg-tp-1"
DELETED = "card-upg-deleted"
PS = "POS Invoice-payroc_status-bold"

SALE_SCALARS = {"status": "CHARGED", "charged_amount": 150.0, "unique_reference": "MULTI",
	"instruction_id": None, "attempt": 2, "response": '{"resultCode": "A"}', "manual_settlement": 0}
RETURN_SCALARS = {"refund_reference": "PARTIAL:RF-1", "refunded_amount": 40.0,
	"oob_refund_reference": "PORTAL-77", "status": "REFUNDED", "manual_settlement": 1,
	"manual_settlement_reason": "virtual terminal, receipt in drawer"}
LEGACY_SCALARS = {"status": "VOIDED", "charged_amount": 0.0, "unique_reference": "LEGACYREF1",
	"refund_reference": None, "attempt": 1}
ROWS = [
	{"name": "card-upg-row-1", "idx": 1, "amount": 100.0, "refunded_amount": 40.0, "status": "CHARGED",
		"unique_reference": "REF-A", "instruction_id": "INS-1", "attempt": 1, "card_summary": "Visa •1111",
		"response": '{"a": 1}'},
	{"name": "card-upg-row-2", "idx": 2, "amount": 50.0, "refunded_amount": 0.0, "status": "CHARGED",
		"unique_reference": "REF-B", "instruction_id": "INS-2", "attempt": 2, "card_summary": "MC •2222",
		"response": '{"b": 2}'},
]
TP_ROW = {"sales_invoice": "CARD-UPG-SI", "amount": 75.5, "status": "CHARGED", "unique_reference": "REF-TP",
	"approval_code": "A1B2", "card_summary": "Amex •3333", "payment_entry": "CARD-UPG-PE",
	"instruction_id": "INS-TP", "response": '{"tp": 1}'}


def _connect(site: str) -> None:
	frappe.init(site=site, sites_path=".")
	frappe.connect()


def _insert(table: str, row: dict) -> None:
	cols = ", ".join(f"`{c}`" for c in row)
	frappe.db.sql(f"insert into `{table}` ({cols}) values ({', '.join(['%s'] * len(row))})",
		tuple(row.values()))


def _base(name: str, **extra) -> dict:
	now = frappe.utils.now()
	return {"name": name, "creation": now, "modified": now, "owner": "Administrator",
		"modified_by": "Administrator", "docstatus": 1, **extra}


def seed(site: str) -> None:
	_connect(site)
	if not frappe.db.has_column("POS Invoice", "payroc_status"):
		print("::notice::card_upgrade_check: the base site already has the card_* shape - nothing to seed")
		return
	old = lambda scalars: {f"payroc_{k}": v for k, v in scalars.items()}       # noqa: E731
	_insert("tabPOS Invoice", _base(SALE, **old(SALE_SCALARS)))
	_insert("tabPOS Invoice", _base(RETURN, is_return=1, return_against=SALE, **old(RETURN_SCALARS)))
	_insert("tabPOS Invoice", _base(LEGACY, **old(LEGACY_SCALARS)))
	for row in ROWS:
		_insert("tabPayroc Charge", {**_base(row["name"], docstatus=1), "parent": SALE,
			"parenttype": "POS Invoice", "parentfield": "payroc_charges",
			**{k: v for k, v in row.items() if k != "name"}})
	_insert("tabPayroc Terminal Payment", _base(TP, docstatus=0, **TP_ROW))
	draft = {"doctype": "POS Invoice", "name": DRAFT, "docstatus": 0, "payroc_status": "CHARGED",
		"payroc_charged_amount": 20.0, "payroc_unique_reference": "REF-DEL",
		"payroc_charges": [{"doctype": "Payroc Charge", "name": "card-upg-del-row", "parent": DRAFT,
			"parenttype": "POS Invoice", "parentfield": "payroc_charges", "idx": 1, "amount": 20.0,
			"status": "CHARGED", "unique_reference": "REF-DEL"}]}
	_insert("tabDeleted Document", _base(DELETED, docstatus=0, deleted_name=DRAFT,
		deleted_doctype="POS Invoice", restored=0, data=json.dumps(draft)))
	if not frappe.db.exists("Property Setter", PS):
		_insert("tabProperty Setter", _base(PS, docstatus=0, doctype_or_field="DocField",
			doc_type="POS Invoice", field_name="payroc_status", property="bold", property_type="Check",
			value="1"))
	frappe.db.commit()
	with open(MARKER, "w") as fh:
		json.dump({"site": site}, fh)
	print(f"card_upgrade_check: seeded {SALE}, {RETURN}, {LEGACY}, {len(ROWS)} charge rows, {TP}, {DELETED}")


def verify(site: str) -> None:
	if not os.path.exists(MARKER):
		print("::notice::card_upgrade_check: nothing was seeded - nothing to verify")
		return
	_connect(site)
	problems: list[str] = []

	def expect(what: str, got, want) -> None:
		if got != want:
			problems.append(f"{what}: got {got!r}, want {want!r}")

	for name, scalars in ((SALE, SALE_SCALARS), (RETURN, RETURN_SCALARS), (LEGACY, LEGACY_SCALARS)):
		got = frappe.db.get_value("POS Invoice", name, [f"card_{k}" for k in scalars], as_dict=True) or {}
		for k, v in scalars.items():
			value = got.get(f"card_{k}")
			expect(f"{name}.card_{k}", float(value) if isinstance(v, float) and value is not None else value, v)
	doc = frappe.get_doc("POS Invoice", SALE)
	loaded = sorted(((r.name, r.amount, r.refunded_amount, r.status, r.unique_reference, r.instruction_id,
		r.attempt, r.card_summary, r.response, r.processor) for r in doc.get("card_charges")))
	expect(f"{SALE}.card_charges", loaded, sorted((r["name"], r["amount"], r["refunded_amount"], r["status"],
		r["unique_reference"], r["instruction_id"], r["attempt"], r["card_summary"], r["response"], "payroc")
		for r in ROWS))
	tp = frappe.db.get_value("Card Terminal Payment", TP, [*TP_ROW, "processor"], as_dict=True) or {}
	expect(TP, {k: (float(v) if k == "amount" else v) for k, v in tp.items()}, {**TP_ROW, "processor": "payroc"})
	deleted = json.loads(frappe.db.get_value("Deleted Document", DELETED, "data") or "{}")
	expect(f"{DELETED} keys", sorted(k for k in deleted if "payroc" in k or k.startswith("card_")),
		["card_charged_amount", "card_charges", "card_status", "card_unique_reference"])
	expect(f"{DELETED} row", {k: deleted.get("card_charges", [{}])[0].get(k) for k in ("doctype", "parentfield")},
		{"doctype": "Card Charge", "parentfield": "card_charges"})
	expect("Property Setter", frappe.db.get_value("Property Setter", {"doc_type": "POS Invoice",
		"property": "bold", "field_name": ("in", ["payroc_status", "card_status"])}, ["name", "field_name"]),
		("POS Invoice-card_status-bold", "card_status"))
	for table in ("Payroc Charge", "Payroc Terminal Payment"):
		expect(f"table {table}", frappe.db.table_exists(table), False)
		expect(f"DocType {table}", bool(frappe.db.exists("DocType", table)), False)
	expect("payroc_* columns", [c for c in frappe.db.get_table_columns("POS Invoice") if "payroc" in c], [])
	expect("payroc_* Custom Fields", frappe.get_all("Custom Field",
		filters={"dt": "POS Invoice", "fieldname": ("like", "%payroc%")}, pluck="name"), [])

	for doctype, names in (("POS Invoice", [SALE, RETURN, LEGACY]), ("Card Charge", [r["name"] for r in ROWS]),
			("Card Terminal Payment", [TP]), ("Deleted Document", [DELETED]),
			("Property Setter", ["POS Invoice-card_status-bold"])):
		frappe.db.delete(doctype, {"name": ("in", names)})
	frappe.db.commit()
	os.remove(MARKER)
	if problems:
		print("card_upgrade_check: FAILED\n  " + "\n  ".join(problems))
		sys.exit(1)
	print("card_upgrade_check: every seeded card money fact came through the rename")


if __name__ == "__main__":
	if len(sys.argv) != 3 or sys.argv[1] not in ("seed", "verify"):
		sys.exit(__doc__)
	{"seed": seed, "verify": verify}[sys.argv[1]](sys.argv[2])
