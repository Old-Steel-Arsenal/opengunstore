"""ffl_integrations.patches.rename_payroc_storage_to_card on a site in the old shape (DocTypes
``Payroc Charge`` / ``Payroc Terminal Payment``, POS Invoice ``payroc_*`` fields): every seeded
money fact — the invoice scalars, the charge rows, a counter terminal payment, a deleted draft in
the recycle bin — comes through under the new names; a Property Setter follows its field; a stored
place naming an old name is listed, not edited; a second run changes nothing; and
``backfill_card_processor`` names Payroc on every row.

The site is put into the old shape by renaming the live tables and columns back, so rows other
tests left are carried through both renames too. Its DDL (RENAME TABLE, ALTER TABLE) commits, and
everything pending with it: these commits are load-bearing (docs/testing.md rule 2). Every record
the test makes has an exact name and is deleted by name; the cleanup runs the patch again
(idempotent — it puts the new shape back if a test stopped half way) while ``in_patch`` is still
set, then commits.
"""
from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.model.utils.rename_field import update_property_setters
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.patches import backfill_card_processor as backfill
from ffl_integrations.patches import rename_payroc_storage_to_card as patch

INVOICE = patch.INVOICE
SALE, RETURN, LEGACY, DRAFT = "_T-CARD-SALE", "_T-CARD-RET", "_T-CARD-LEGACY", "_T-CARD-DRAFT"
TP, DELETED, SCRIPT = "_t-card-tp", "_t-card-deleted", "_T-CARD script"
SALE_SCALARS = {"status": "CHARGED", "charged_amount": 150.0, "unique_reference": "MULTI", "attempt": 2,
	"response": '{"resultCode": "A"}'}
RETURN_SCALARS = {"refund_reference": "PARTIAL:RF-1", "refunded_amount": 40.0,
	"oob_refund_reference": "PORTAL-77", "status": "REFUNDED", "manual_settlement": 1,
	"manual_settlement_reason": "virtual terminal"}
LEGACY_SCALARS = {"status": "VOIDED", "charged_amount": 0.0, "unique_reference": "LEGACYREF1",
	"instruction_id": "INS-OLD", "attempt": 1}
ROWS = (
	{"name": "_t-card-row-1", "idx": 1, "amount": 100.0, "refunded_amount": 40.0, "status": "CHARGED",
		"unique_reference": "REF-A", "instruction_id": "INS-1", "attempt": 1, "card_summary": "Visa 1111"},
	{"name": "_t-card-row-2", "idx": 2, "amount": 50.0, "refunded_amount": 0.0, "status": "CHARGED",
		"unique_reference": "REF-B", "instruction_id": "INS-2", "attempt": 2, "card_summary": "MC 2222"},
)
TP_ROW = {"sales_invoice": "_T-CARD-SI", "amount": 75.5, "status": "CHARGED", "unique_reference": "REF-TP",
	"approval_code": "A1B2", "card_summary": "Amex 3333", "instruction_id": "INS-TP"}
LOG_TITLE = patch.LOG_TITLE


def _insert(table: str, row: dict) -> None:
	now = frappe.utils.now()
	row = {"creation": now, "modified": now, "owner": "Administrator", "modified_by": "Administrator", **row}
	frappe.db.sql(f"insert into `{table}` ({', '.join(f'`{c}`' for c in row)}) "
		f"values ({', '.join(['%s'] * len(row))})", tuple(row.values()))


class RenamePayrocStorage(FrappeTestCase):
	def setUp(self):
		self._logs: list[str] = []
		in_patch = frappe.flags.in_patch
		frappe.flags.in_patch = True          # what the patch runner sets: keeps rename_doc off the app files
		self.addCleanup(setattr, frappe.flags, "in_patch", in_patch)   # registered first, so it runs last
		log_error = frappe.log_error

		def record(*args, **kwargs):
			doc = log_error(*args, **kwargs)
			self._logs.append(doc.name)
			return doc

		log = mock.patch.object(frappe, "log_error", side_effect=record)
		log.start()
		self.addCleanup(log.stop)
		self.addCleanup(self._put_back)

	def _put_back(self):
		frappe.flags.in_patch = True
		patch.execute()
		for doctype, names in ((INVOICE, [SALE, RETURN, LEGACY]), ("Card Charge", [r["name"] for r in ROWS]),
				("Card Terminal Payment", [TP]), ("Deleted Document", [DELETED]), ("Client Script", [SCRIPT]),
				("Property Setter", [f"{INVOICE}-card_status-bold", f"{INVOICE}-payroc_status-bold"]),
				("Error Log", self._logs or [""])):
			frappe.db.delete(doctype, {"name": ("in", names)})
		frappe.db.commit()                    # load-bearing: the test's DDL already committed what it made
		frappe.clear_cache(doctype=INVOICE)

	def _to_old_shape(self):
		"""What a site that has not run the patch looks like."""
		for old, new in patch.DOCTYPES:
			frappe.rename_doc("DocType", new, old, force=True)
		for old, new, has_value in patch.FIELDS:
			if has_value:
				frappe.db.rename_column(INVOICE, new, old)
			frappe.db.sql("update `tabCustom Field` set name = %s, fieldname = %s where name = %s",
				(f"{INVOICE}-{old}", old, f"{INVOICE}-{new}"))
			update_property_setters(INVOICE, new, old)
		frappe.db.sql("update `tabPayroc Charge` set parentfield = %s where parentfield = %s",
			patch.TABLE_FIELD)              # (old, new): every existing row back to the old name
		frappe.clear_cache(doctype=INVOICE)

	def _seed(self):
		old = lambda scalars: {f"payroc_{k}": v for k, v in scalars.items()}       # noqa: E731
		_insert(f"tab{INVOICE}", {"name": SALE, "docstatus": 1, **old(SALE_SCALARS)})
		_insert(f"tab{INVOICE}", {"name": RETURN, "docstatus": 0, "is_return": 1, "return_against": SALE,
			**old(RETURN_SCALARS)})
		_insert(f"tab{INVOICE}", {"name": LEGACY, "docstatus": 1, **old(LEGACY_SCALARS)})
		for row in ROWS:
			_insert("tabPayroc Charge", {**row, "docstatus": 1, "parent": SALE, "parenttype": INVOICE,
				"parentfield": "payroc_charges"})
		_insert("tabPayroc Terminal Payment", {"name": TP, "docstatus": 0, **TP_ROW})
		draft = {"doctype": INVOICE, "name": DRAFT, "docstatus": 0, "payroc_status": "CHARGED",
			"payroc_charged_amount": 20.0, "payroc_charges": [{"doctype": "Payroc Charge", "name": "_t-del-row",
				"parentfield": "payroc_charges", "amount": 20.0, "status": "CHARGED"}]}
		_insert("tabDeleted Document", {"name": DELETED, "deleted_name": DRAFT, "deleted_doctype": INVOICE,
			"data": json.dumps(draft)})
		frappe.make_property_setter({"doctype": INVOICE, "fieldname": "payroc_status", "property": "bold",
			"value": "1", "property_type": "Check"}, validate_fields_for_doctype=False)
		_insert("tabClient Script", {"name": SCRIPT, "dt": INVOICE, "view": "Form", "enabled": 0,
			"script": "frm.toggle_display('payroc_status', 1);"})

	def _scalars(self, name, scalars):
		got = frappe.db.get_value(INVOICE, name, [f"card_{k}" for k in scalars], as_dict=True)
		return {k: (float(got[f"card_{k}"]) if isinstance(v, float) else got[f"card_{k}"])
			for k, v in scalars.items()}

	def test_every_money_fact_survives_and_a_second_run_changes_nothing(self):
		self._to_old_shape()
		self._seed()
		self.assertTrue(frappe.db.table_exists("Payroc Charge") and not frappe.db.table_exists("Card Charge"))

		patch.execute()
		patch.execute()                       # idempotent
		backfill.execute()
		backfill.execute()

		for old, new in patch.DOCTYPES:
			self.assertFalse(frappe.db.exists("DocType", old))
			self.assertFalse(frappe.db.table_exists(old))
			self.assertTrue(frappe.db.table_exists(new))
		self.assertEqual([c for c in frappe.db.get_table_columns(INVOICE) if "payroc" in c], [])
		self.assertEqual(frappe.get_all("Custom Field", filters={"dt": INVOICE, "fieldname": ("like", "%payroc%")},
			pluck="name"), [])
		self.assertEqual(frappe.db.get_value("Custom Field", f"{INVOICE}-card_charges", "options"), "Card Charge")
		self.assertEqual(frappe.db.get_value("Custom Field", f"{INVOICE}-card_unique_reference", "insert_after"),
			"section_card")

		self.assertEqual(self._scalars(SALE, SALE_SCALARS), SALE_SCALARS)
		self.assertEqual(self._scalars(RETURN, RETURN_SCALARS), RETURN_SCALARS)
		self.assertEqual(self._scalars(LEGACY, LEGACY_SCALARS), LEGACY_SCALARS)
		# Loaded through the ORM: the rows only come back when parentfield moved with the field.
		rows = frappe.get_doc(INVOICE, SALE).get("card_charges")
		self.assertEqual(sorted((r.name, r.amount, r.refunded_amount, r.status, r.unique_reference,
			r.instruction_id, r.attempt, r.card_summary, r.processor) for r in rows),
			sorted((r["name"], r["amount"], r["refunded_amount"], r["status"], r["unique_reference"],
				r["instruction_id"], r["attempt"], r["card_summary"], "payroc") for r in ROWS))
		tp = frappe.db.get_value("Card Terminal Payment", TP, [*TP_ROW, "processor"], as_dict=True)
		self.assertEqual({k: (float(v) if k == "amount" else v) for k, v in tp.items()},
			{**TP_ROW, "processor": "payroc"})

		deleted = json.loads(frappe.db.get_value("Deleted Document", DELETED, "data"))
		self.assertEqual(sorted(k for k in deleted if "payroc" in k or k.startswith("card_")),
			["card_charged_amount", "card_charges", "card_status"])
		self.assertEqual((deleted["card_charges"][0]["doctype"], deleted["card_charges"][0]["parentfield"]),
			("Card Charge", "card_charges"))
		self.assertEqual(frappe.get_all("Property Setter", filters={"doc_type": INVOICE, "property": "bold",
			"field_name": ("in", ["payroc_status", "card_status"])}, pluck="name"), [f"{INVOICE}-card_status-bold"])

		# The script naming an old field is left as it was and listed once (the second run renamed
		# nothing, so it listed nothing).
		self.assertEqual(frappe.db.get_value("Client Script", SCRIPT, "script"),
			"frm.toggle_display('payroc_status', 1);")
		logs = frappe.get_all("Error Log", filters={"name": ("in", self._logs or [""]), "method": LOG_TITLE},
			pluck="error")
		self.assertEqual(len(logs), 1, logs)
		self.assertIn(f"Client Script {SCRIPT}: script", logs[0])

	def test_a_new_shape_site_is_left_alone(self):
		before = frappe.get_all("Custom Field", filters={"dt": INVOICE, "fieldname": ("like", "card_%")},
			fields=["name", "fieldname", "insert_after", "options"], order_by="name")
		patch.execute()
		self.assertEqual(frappe.get_all("Custom Field", filters={"dt": INVOICE, "fieldname": ("like", "card_%")},
			fields=["name", "fieldname", "insert_after", "options"], order_by="name"), before)
		self.assertEqual(self._logs, [])

	def test_a_site_with_both_doctypes_stops_for_a_person(self):
		frappe.db.sql("insert into `tabDocType` (name, module, custom) values ('Payroc Charge', 'FFL Integrations', 1)")
		try:
			with self.assertRaises(frappe.ValidationError):
				patch.rename_doctype("Payroc Charge", "Card Charge")
		finally:
			frappe.db.delete("DocType", {"name": "Payroc Charge"})
