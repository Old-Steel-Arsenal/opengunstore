"""POS recycle bin — list and restore deleted POS Invoice drafts.

Frappe logs every deletion in `Deleted Document` (the doc's JSON + who / when),
but its own restore (`deleted_document.restore`) is System Manager only and
re-numbers the invoice from the naming series — useless to the clerk who just
hit "Delete Order" on the wrong draft (2026-09-08:
a $3,084 sale carrying a $712.25 card deposit). This module is the cashier-
facing version wired into the POS "Recent Orders" list (public/js/
pos_recycle_bin.js): whoever may delete a POS Invoice may put it back, and
the original number is kept whenever it is still free.

Only DRAFT deletions are offered: a cancelled invoice that someone deleted
from Desk must not silently come back as a sellable draft.
"""
from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.model.naming import parse_naming_series
from frappe.utils import add_days, cint, flt, now_datetime

DOCTYPE = "POS Invoice"
RETENTION_DAYS = 30
LIST_LIMIT = 100        # lazy: 100 cards max, upgrade path = push search into SQL + paging
_SCAN_LIMIT = 500       # lazy: rows scanned per list call before the draft/search filter


def _check_perm() -> None:
	"""Restore is the mirror of delete: the same people, no more, no less."""
	if not frappe.has_permission("POS Invoice", "delete"):
		frappe.throw(_("You need delete permission on POS Invoice to use the recycle bin."),
			frappe.PermissionError)
	if not frappe.has_permission("POS Invoice", "create"):
		frappe.throw(_("You need create permission on POS Invoice to use the recycle bin."),
			frappe.PermissionError)


def _payload(row) -> dict | None:
	"""The logged JSON, or None when it is not a restorable POS Invoice draft
	(wrong doctype / cancelled / submitted / unparsable)."""
	try:
		data = json.loads(row.data or "{}")
	except ValueError:
		return None
	if data.get("doctype") != DOCTYPE or cint(data.get("docstatus")) != 0:
		return None
	return data


def _summary(row) -> dict | None:
	"""Card-sized summary of one Deleted Document row; None when not restorable."""
	data = _payload(row)
	if data is None:
		return None
	return {
		"deleted_document": row.name,
		"name": row.deleted_name,
		"customer_name": data.get("customer_name") or data.get("customer") or "",
		"grand_total": flt(data.get("grand_total")),
		"currency": data.get("currency") or "",
		"posting_date": data.get("posting_date"),
		"posting_time": data.get("posting_time"),
		"is_return": cint(data.get("is_return")),
		"deleted_by": row.owner,
		"deleted_at": row.creation,
	}


def _ensure_series_counter(doc) -> None:
	"""Deleting the LAST document of a naming series rewinds its counter
	(frappe.model.naming.revert_series_if_last), so a draft restored under its
	original number would be re-issued to the next new invoice — a primary-key
	collision at the register. Move the counter back up to the restored number
	when it fell below it. Prefix/count slicing mirrors revert_series_if_last
	branch for branch (trailing hash / infix hash / no dot) so the two can
	never disagree about which tabSeries row a name belongs to; a series row
	that does not exist is left alone (the name was not issued by it)."""
	key = doc.get("naming_series")
	name = doc.name
	if not key or not name:
		return
	if ".#" in key:
		prefix, hashes = key.rsplit(".", 1)
		if "#" not in hashes:
			hash = re.search("#+", key)
			if not hash:
				return
			name = name.replace(hashes, "")
			prefix = prefix.replace(hash.group(), "")
	else:
		prefix = key
	if "." in prefix:
		boundary = len(parse_naming_series(prefix.split("."), doc=doc))
		count = cint(name[boundary:])
		prefix = name[:boundary]
	else:
		count = cint(name.replace(prefix, ""))
	if not count:
		return
	series = frappe.qb.DocType("Series")
	current = (frappe.qb.from_(series).where(series.name == prefix)
		.for_update().select("current")).run()
	if current and cint(current[0][0]) < count:
		frappe.qb.update(series).set(series.current, count).where(series.name == prefix).run()


@frappe.whitelist()
def list_deleted_invoices(search_term: str | None = None, days: int | None = None):
	"""Deleted POS Invoice drafts that can still be restored, newest first.

	Backs the "Deleted" filter of the POS Recent Orders list. Reads the
	Deleted Document log directly (that doctype is System Manager only — the
	gate here is the caller's POS Invoice permission instead).

	Args:
	    search_term: Optional substring matched case-insensitively against
	        the original invoice number and the customer name.
	    days: Look-back window in days (default 30, max 365).

	Returns:
	    List of dicts: deleted_document (log row id to pass to
	    restore_deleted_invoice), name (original invoice number),
	    customer_name, grand_total, currency, posting_date, posting_time,
	    is_return, deleted_by, deleted_at.
	"""
	_check_perm()
	days = min(max(cint(days) or RETENTION_DAYS, 1), 365)
	needle = (search_term or "").strip().lower()
	filters = {"deleted_doctype": DOCTYPE, "restored": 0, "creation": [">=", _since(days)]}
	kwargs = {}
	if needle:
		# The invoice number lives in its own column; the customer name only
		# inside the JSON payload — both pushed down as LIKE so a match is
		# never cut off by the scan limit (re-checked in Python below).
		like = f"%{needle}%"
		kwargs["or_filters"] = [["deleted_name", "like", like], ["data", "like", like]]
	rows = frappe.get_all("Deleted Document", filters=filters,
		fields=["name", "deleted_name", "data", "owner", "creation"],
		order_by="creation desc", limit=_SCAN_LIMIT, **kwargs)
	out = []
	for r in rows:
		s = _summary(r)
		if not s:
			continue
		if needle and needle not in s["name"].lower() and needle not in s["customer_name"].lower():
			continue
		out.append(s)
		if len(out) >= LIST_LIMIT:
			break
	return out


def _since(days: int):
	return add_days(now_datetime(), -days)


@frappe.whitelist()
def restore_deleted_invoice(name: str):
	"""Put a deleted POS Invoice draft back, keeping its original number when free.

	Re-inserts the logged JSON with `flags.from_restore` (so the card charge
	protector keeps the invoice's own card rows — see
	ffl_integrations.payments.api.restore_charge_rows), retains the original
	owner / timestamps like frappe's own restore, and marks the log row
	restored. Validation runs as on any insert: a serial sold in the meantime
	makes the restore fail loudly instead of creating a double sale.

	Args:
	    name: Deleted Document row id (from list_deleted_invoices).

	Returns:
	    Dict with name (the restored invoice's number), original_name, and
	    renamed (True when the original number was already taken and the
	    naming series assigned a new one).
	"""
	_check_perm()
	# Row lock first: two clerks restoring the same log row (double click, two
	# registers) must serialise here, or the second one re-inserts a copy —
	# under a new number — carrying the same captured card row.
	locked = frappe.db.get_value("Deleted Document", name,
		["restored", "new_name", "deleted_name", "deleted_doctype", "creation", "data"],
		as_dict=True, for_update=True)
	if not locked:
		frappe.throw(_("Deleted Document {0} not found.").format(name), frappe.DoesNotExistError)
	if locked.deleted_doctype != DOCTYPE:
		frappe.throw(_("{0} is not a deleted {1}.").format(name, DOCTYPE))
	if locked.restored:
		frappe.throw(_("{0} was already restored as {1}.").format(locked.deleted_name, locked.new_name),
			frappe.DocumentAlreadyRestored)
	if frappe.utils.get_datetime(locked.creation) < _since(RETENTION_DAYS):
		frappe.throw(_("{0} was deleted more than {1} days ago; ask an administrator to restore it.")
			.format(locked.deleted_name, RETENTION_DAYS))
	data = _payload(locked)
	if data is None:
		frappe.throw(_("Only deleted POS Invoice drafts can be restored from the POS; {0} is not one.")
			.format(locked.deleted_name))

	doc = frappe.get_doc(data)
	original = locked.deleted_name
	if not frappe.has_permission(DOCTYPE, "read", doc=doc):
		frappe.throw(_("You do not have permission to restore this document."), frappe.PermissionError)
	doc.flags.from_restore = True
	keep_number = not frappe.db.exists(DOCTYPE, original)
	doc.insert(set_name=original if keep_number else None)
	if keep_number:
		_ensure_series_counter(doc)

	frappe.db.set_value(DOCTYPE, doc.name, {
		"owner": data.get("owner") or doc.owner,
		"creation": data.get("creation") or doc.creation,
		"modified": data.get("modified") or doc.modified,
		"modified_by": data.get("modified_by") or doc.modified_by,
	}, update_modified=False)
	doc.add_comment("Edit", _("restored {0} as {1} from the POS recycle bin").format(original, doc.name))

	frappe.db.set_value("Deleted Document", name, {"new_name": doc.name, "restored": 1},
		update_modified=False)
	return {"name": doc.name, "original_name": original, "renamed": doc.name != original}
