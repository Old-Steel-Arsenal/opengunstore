"""Shared serial-number extraction for transaction lines.

ERPNext stores a line's serials as either a newline-separated string in
row.serial_no (use_serial_batch_fields=1) or a Serial and Batch Bundle
reference (v16 default). Every consumer of per-line serials — acquisitions on
Purchase Receipt, dealer-transfer dispositions on SI/DN — must handle both,
so the dual-path lives here once.
"""
from __future__ import annotations

import frappe


def only_active_serial(item_code: str | None) -> str | None:
	"""The Item's Serial No when it has exactly ONE Active serial, else None.

	The A&D auto-fill rule shared by FFL Acquisition and FFL Disposition: a
	blank serial on a single-gun Item is stamped server-side too (defence in
	depth for REST/script callers that bypass the form's auto-pick)."""
	if not item_code:
		return None
	active = frappe.get_all(
		"Serial No",
		filters={"item_code": item_code, "status": "Active"},
		pluck="name", limit=2,
	)
	return active[0] if len(active) == 1 else None


def serials_for_line(row) -> list[str]:
	# Legacy path: newline-separated string in row.serial_no
	if row.get("serial_no"):
		return [s.strip() for s in (row.serial_no or "").splitlines() if s.strip()]

	# v16 Bundle path
	if row.get("serial_and_batch_bundle"):
		bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
		return [e.serial_no for e in bundle.entries if e.serial_no]

	return []


def serials_for_lines(rows) -> list[list[str]]:
	"""``serials_for_line`` for many rows, with ONE query for all their bundles —
	a report over years of v16 lines must not get_doc each bundle. Same answer
	per row, in input order; a bundle that no longer exists reads as no serials."""
	bundles = {r.get("serial_and_batch_bundle") for r in rows
		if not r.get("serial_no") and r.get("serial_and_batch_bundle")}
	by_bundle: dict[str, list[str]] = {}
	if bundles:
		for parent, serial in frappe.db.sql("""
				select parent, serial_no from `tabSerial and Batch Entry`
				where parent in %(bundles)s and ifnull(serial_no, '') != ''
				order by parent, idx""", {"bundles": tuple(bundles)}):
			by_bundle.setdefault(parent, []).append(serial)
	return [serials_for_line(r) if r.get("serial_no")
		else by_bundle.get(r.get("serial_and_batch_bundle") or "", []) for r in rows]


def receipts_of(serial: str) -> list:
	"""The gun's standing receipts — submitted Purchase Receipts that took it IN,
	not purchase returns nor a receipt's rejected guns — latest first, each with
	the row (voucher_detail_no) and its posting_datetime. Read off the Serial and
	Batch Bundles, which every submitted receipt row carries whatever its serial
	storage."""
	return frappe.db.sql("""
		select b.voucher_no, b.voucher_detail_no, b.posting_datetime
		from `tabSerial and Batch Bundle` b
		join `tabSerial and Batch Entry` e on e.parent = b.name
		where e.serial_no = %s and b.voucher_type = 'Purchase Receipt'
			and b.type_of_transaction = 'Inward' and b.is_rejected = 0
			and b.docstatus = 1 and b.is_cancelled = 0
		order by b.posting_datetime desc, b.creation desc""", serial, as_dict=True)


def receipt_serials(receipt: str) -> dict[str, str]:
	"""Every serial a receipt took in (its rejected guns aside) → its item code,
	the receipt standing or cancelled: a cancel marks the row's bundle cancelled
	and unlinks it from the row (StockController.delete_auto_created_batches),
	but the bundle keeps its voucher and its entries."""
	return dict(frappe.db.sql("""
		select e.serial_no, b.item_code
		from `tabSerial and Batch Bundle` b
		join `tabSerial and Batch Entry` e on e.parent = b.name
		where b.voucher_type = 'Purchase Receipt' and b.voucher_no = %s
			and b.type_of_transaction = 'Inward' and b.is_rejected = 0
			and ifnull(e.serial_no, '') != ''""", receipt))
