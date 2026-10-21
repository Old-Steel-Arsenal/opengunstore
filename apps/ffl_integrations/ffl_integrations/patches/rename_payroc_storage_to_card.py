"""Rename the card-charge storage to processor-neutral names: DocType ``Payroc Charge`` →
``Card Charge``, ``Payroc Terminal Payment`` → ``Card Terminal Payment``, and the POS Invoice
fields ``payroc_*`` → ``card_*`` (``section_payroc`` → ``section_card``, ``column_payroc_a`` →
``column_card_a``, the table ``payroc_charges`` → ``card_charges``). Every money fact — the
charge rows, the invoice's scalars, the counter's terminal payments — is kept; only names change.

A **pre_model_sync** patch, for the same reason as ``ffl_core.patches.rename_catalog_category``
(whose shape this follows): ``bench migrate`` syncs the DocType JSON between the two patch
passes, which would create empty ``Card Charge`` / ``Card Terminal Payment`` tables beside the
old ones, and ``sync_fixtures`` runs later still and would add empty ``card_*`` columns next to
the populated ``payroc_*`` ones. So, before both:

1. ``frappe.rename_doc("DocType", …)`` for both DocTypes: the table and everything that names
   the DocType (Table / Link options in DocField, Custom Field and Property Setter, Custom
   DocPerm, Link-to-DocType values such as Version / Comment / File / ToDo, dynamic links).
   Saved list views of the old DocType move to the new name.
2. Each POS Invoice column is renamed in place (``ALTER TABLE … CHANGE COLUMN``), so the values
   never pass through Python. Its Custom Field record takes the new name and fieldname (its own
   ``depends_on`` expressions too); Property Setters are re-pointed; Custom Fields placed after
   an old field follow it; Report Builder reports, report-view columns and user settings that
   name an old value field follow it (frappe's ``rename_field`` helpers). Should a site already
   have both columns, blanks in the new one are filled from the old, and the old column is
   dropped only when every value equals the new column's; otherwise it stays and an Error Log
   gives the count.
3. The charge rows' ``parentfield`` follows the table field (frappe loads child rows by it).
4. Deleted Document rows of a POS Invoice or a Payroc Terminal Payment get their stored JSON in
   the new names, so a later restore (the POS recycle bin, frappe's own) carries its money facts.
5. Nothing else is edited. The common stored places that still name an old name (``PLACES`` of
   the catalog rename: saved filters, reports, scripts, print formats, notifications, user
   settings) are listed for a person in one Error Log. Version rows keep the old field names in
   their history on purpose.

The ``processor`` column the two DocTypes gain comes with the model sync right after this patch;
``backfill_card_processor`` (post_model_sync) fills it with ``payroc`` on every existing row.

Idempotent: a site with no old DocType, no old column and no old Custom Field (a fresh install, or
one already renamed) is left alone. A site with BOTH DocTypes of a pair stops with an error for a
person to look at, rather than guessing which table holds the money.
"""
from __future__ import annotations

import json
import re

import frappe
from frappe.model.utils.rename_field import (
	update_property_setters,
	update_reports,
	update_user_settings,
	update_users_report_view_settings,
)
from frappe.model.utils.user_settings import clear_user_settings_cache, sync_user_settings

DOCTYPES = (("Payroc Charge", "Card Charge"), ("Payroc Terminal Payment", "Card Terminal Payment"))
INVOICE = "POS Invoice"
TABLE_FIELD = ("payroc_charges", "card_charges")
# (old, new, has a column with values)
FIELDS = (
	("section_payroc", "section_card", False),
	("payroc_unique_reference", "card_unique_reference", True),
	("payroc_instruction_id", "card_instruction_id", True),
	("payroc_refund_reference", "card_refund_reference", True),
	("payroc_oob_refund_reference", "card_oob_refund_reference", True),
	("payroc_attempt", "card_attempt", True),
	("payroc_status", "card_status", True),
	("payroc_charged_amount", "card_charged_amount", True),
	("column_payroc_a", "column_card_a", False),
	("payroc_response", "card_response", True),
	("payroc_refunded_amount", "card_refunded_amount", True),
	("payroc_manual_settlement", "card_manual_settlement", True),
	("payroc_manual_settlement_reason", "card_manual_settlement_reason", True),
	(*TABLE_FIELD, False),
)
NEW_NAME = {old: new for old, new, _v in FIELDS}
OLD_FIELD = re.compile(r"\b(" + "|".join(sorted(NEW_NAME, key=len, reverse=True)) + r")\b")
MENTION = re.compile(OLD_FIELD.pattern + "|" + "|".join(old for old, _new in DOCTYPES), re.IGNORECASE)
NUMERIC = ("int", "bigint", "decimal", "tinyint", "smallint", "float", "double")
# Shipped by our own fixtures and rewritten by the fixture sync that follows the migrate: not a
# place a person has to look at.
SHIPPED = {("Print Format", "POS Receipt")}
LOG_TITLE = "rename_payroc_storage_to_card: still names an old name"


def execute():
	changed = False
	for old, new in DOCTYPES:
		changed = rename_doctype(old, new) or changed
	changed = rename_invoice_fields() or changed
	changed = rewrite_deleted_documents() or changed
	if changed:
		report_mentions()
	frappe.clear_cache()


def rename_doctype(old: str, new: str) -> bool:
	if not frappe.db.exists("DocType", old):
		return False
	if frappe.db.exists("DocType", new):
		frappe.throw(f"Both {old!r} and {new!r} exist on this site; nothing was renamed. Compare the "
			f"two tables, keep the one with the card charges, then run the migrate again.")
	frappe.rename_doc("DocType", old, new, force=True)
	# rename_doc moves other doctypes' list settings, not the renamed DocType's own (see
	# rename_catalog_category): cached views to the table first, old-name keys dropped after.
	sync_user_settings()
	frappe.db.sql("""update `__UserSettings` s set s.doctype = %(new)s where s.doctype = %(old)s
		and s.user not in (select user from (select user from `__UserSettings` where doctype = %(new)s) t)""",
		{"old": old, "new": new})
	clear_user_settings_cache(old)
	return True


def rename_invoice_fields() -> bool:
	changed = False
	for old, new, has_value in FIELDS:
		changed = _rename_field(old, new, has_value) or changed
	child = DOCTYPES[0][1]
	if frappe.db.table_exists(child) and frappe.db.sql(f"""select 1 from `tab{child}`
			where parentfield = %s and parenttype = %s limit 1""", (TABLE_FIELD[0], INVOICE)):
		frappe.db.sql(f"""update `tab{child}` set parentfield = %s
			where parentfield = %s and parenttype = %s""", (TABLE_FIELD[1], TABLE_FIELD[0], INVOICE))
		changed = True
	if changed:
		frappe.clear_cache(doctype=INVOICE)
	return changed


def _rename_field(old: str, new: str, has_value: bool) -> bool:
	old_cf, new_cf = f"{INVOICE}-{old}", f"{INVOICE}-{new}"
	had_column = has_value and frappe.db.has_column(INVOICE, old)
	if had_column:
		if frappe.db.has_column(INVOICE, new):
			_merge_columns(old, new)
		else:
			frappe.db.rename_column(INVOICE, old, new)
	if frappe.db.exists("Custom Field", old_cf):
		if frappe.db.exists("Custom Field", new_cf):
			frappe.db.delete("Custom Field", {"name": old_cf})
		else:
			frappe.db.sql("update `tabCustom Field` set name = %s, fieldname = %s where name = %s",
				(new_cf, new, old_cf))
	elif not had_column:
		return False                          # nothing of the old field on this site
	_rename_own_expressions(new_cf)
	_rename_field_property_setters(old, new)
	update_property_setters(INVOICE, old, new)       # what is left: Custom Field insert_after
	if has_value:
		update_reports(INVOICE, old, new)
		update_users_report_view_settings(INVOICE, old, new)
		update_user_settings(INVOICE, old, new)
	return True


def _rename_own_expressions(custom_field: str) -> None:
	"""The renamed field's own ``depends_on`` family names its siblings by their old names."""
	row = frappe.db.get_value("Custom Field", custom_field,
		["depends_on", "mandatory_depends_on", "read_only_depends_on"], as_dict=True)
	for field, value in (row or {}).items():
		if value and OLD_FIELD.search(value):
			frappe.db.set_value("Custom Field", custom_field, field,
				OLD_FIELD.sub(lambda m: NEW_NAME[m.group(1)], value), update_modified=False)


def _merge_columns(old: str, new: str) -> None:
	"""Both columns (only if a fixture sync ran before this patch): fill the new one where it is
	blank; drop the old one only when it holds nothing the new one does not."""
	table = f"tab{INVOICE}"
	kind = frappe.db.sql("""select data_type from information_schema.columns
		where table_schema = database() and table_name = %s and column_name = %s""", (table, old))[0][0]
	blank = "0" if kind.lower() in NUMERIC else "''"
	frappe.db.sql(f"""update `{table}` set `{new}` = `{old}`
		where ifnull(`{new}`, {blank}) = {blank} and ifnull(`{old}`, {blank}) != {blank}""")
	differ = frappe.db.sql(f"""select count(*) from `{table}`
		where ifnull(`{old}`, {blank}) != ifnull(`{new}`, {blank})""")[0][0]
	if differ:
		frappe.log_error(title=f"rename_payroc_storage_to_card: {INVOICE}.{old} kept",
			message=f"{differ} {INVOICE} row(s) have a value in {old} that differs from {new}. Both "
				f"columns stay; compare them, then drop {old} by hand.")
		print(f"rename_payroc_storage_to_card: kept {old}, {differ} row(s) differ from {new}")
	else:
		frappe.db.sql_ddl(f"alter table `{table}` drop column `{old}`")


def _rename_field_property_setters(old: str, new: str) -> None:
	for ps in frappe.get_all("Property Setter", filters={"doc_type": INVOICE, "field_name": old},
			fields=["name", "property"]):
		twin = frappe.db.get_value("Property Setter",
			{"doc_type": INVOICE, "field_name": new, "property": ps.property})
		if twin:
			frappe.db.delete("Property Setter", {"name": ps.name})
			continue
		new_name = f"{INVOICE}-{new}-{ps.property}"
		if ps.name != f"{INVOICE}-{old}-{ps.property}" or frappe.db.exists("Property Setter", new_name):
			new_name = ps.name                # a name frappe did not give it: keep it
		frappe.db.sql("update `tabProperty Setter` set name = %s, field_name = %s where name = %s",
			(new_name, new, ps.name))


def rewrite_deleted_documents() -> bool:
	"""Stored JSON of deleted POS Invoices / terminal payments, in the new names."""
	changed = False
	doctypes = [INVOICE, *[d for pair in DOCTYPES[1:] for d in pair]]
	for name, deleted_doctype, data in frappe.db.sql("""select name, deleted_doctype, data
			from `tabDeleted Document` where deleted_doctype in %s""", (doctypes,)):
		try:
			doc = json.loads(data or "{}")
		except ValueError:
			continue
		new_doctype = dict(DOCTYPES).get(deleted_doctype, deleted_doctype)
		renamed = _rename_keys(doc)
		if renamed != doc or new_doctype != deleted_doctype:
			frappe.db.set_value("Deleted Document", name, {"deleted_doctype": new_doctype,
				"data": json.dumps(renamed, indent=1, default=str)}, update_modified=False)
			changed = True
	return changed


def _rename_keys(doc: dict) -> dict:
	out = {}
	for key, value in doc.items():
		if key == "doctype" and value in dict(DOCTYPES):
			value = dict(DOCTYPES)[value]
		elif key == "parentfield" and value == TABLE_FIELD[0]:
			value = TABLE_FIELD[1]
		if isinstance(value, list):
			value = [_rename_keys(v) if isinstance(v, dict) else v for v in value]
		out[NEW_NAME.get(key, key)] = value
	return out


def report_mentions() -> list[str]:
	"""The common stored places that still name an old field or DocType, in one Error Log for a
	person (the catalog rename's list of places)."""
	from ffl_core.patches.rename_catalog_category import ONLY_WHERE, PLACES

	found = []
	like_args = {f"k{i}": f"%{term}%" for i, term in enumerate(["payroc_", *[o for o, _n in DOCTYPES]])}
	for doctype, fields, parent in PLACES:
		if not frappe.db.table_exists(doctype):
			continue
		fields = [f for f in fields if frappe.db.has_column(doctype, f)]
		if not fields:
			continue
		shown = ["name", parent] if parent else ["name"]
		like = " or ".join(f"`{f}` like %({k})s" for f in fields for k in like_args)
		only = f" and {ONLY_WHERE[doctype]}" if doctype in ONLY_WHERE else ""
		rows = frappe.db.sql(f"select {', '.join(f'`{c}`' for c in shown + fields)} from `tab{doctype}` "
			f"where ({like}){only}", like_args, as_dict=True)
		for row in rows:
			if (doctype, row.name) in SHIPPED:
				continue
			hits = [f for f in fields if row[f] and MENTION.search(row[f])]
			if hits:
				where = f" (in {row[parent]})" if parent else ""
				found.append(f"{doctype} {row.name}{where}: {', '.join(hits)}")

	if "__UserSettings" in frappe.db.get_tables(cached=False):
		sync_user_settings()
		for user, doctype, data in frappe.db.sql("""select user, doctype, data from `__UserSettings`
				where doctype in %(old)s or data like %(k0)s""",
				{"old": [o for o, _n in DOCTYPES], **like_args}):
			hits = [k for k, v in (("doctype", doctype), ("data", data)) if v and MENTION.search(v)]
			if hits:
				found.append(f"__UserSettings {user} / {doctype}: {', '.join(hits)}")

	print(f"rename_payroc_storage_to_card: {len(found)} stored place(s) still name an old name"
		+ (" (listed in the Error Log)" if found else ""))
	if found:
		frappe.log_error(title=LOG_TITLE,
			message="The card-charge storage now has processor-neutral names (Payroc Charge -> Card "
				"Charge, Payroc Terminal Payment -> Card Terminal Payment, POS Invoice payroc_* -> "
				"card_*). Nothing below was edited; check each by hand:\n" + "\n".join(found))
	return found
