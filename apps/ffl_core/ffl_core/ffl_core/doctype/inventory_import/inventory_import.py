"""Inventory Import: the Settings page that brings a new store's non-firearm
stock in from a CSV (see ffl_core.inventory_import)."""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core import inventory_import as imp

ROLE = "System Manager"


class InventoryImport(Document):
	def onload(self):
		if not self.warehouse:
			from ffl_core.api.item_admin import _default_warehouse
			self.set_onload("default_warehouse", _default_warehouse())


def _settings():
	doc = frappe.get_single("Inventory Import")
	if not doc.csv_file:
		frappe.throw(_("Attach the CSV file first."))
	warehouse = doc.warehouse
	if not warehouse:
		from ffl_core.api.item_admin import _default_warehouse
		warehouse = _default_warehouse()
	if not warehouse:
		frappe.throw(_("Pick the warehouse the stock goes into."))
	content = frappe.get_doc("File", {"file_url": doc.csv_file}).get_content()
	if isinstance(content, bytes):
		content = content.decode("utf-8-sig", errors="replace")
	return doc, imp.parse(content), warehouse


def _public(planned: list[dict]) -> list[dict]:
	keep = ("line", "upc", "name", "outcome", "item_code", "message", "qty", "cost")
	return [{k: r.get(k) for k in keep} for r in planned]


@frappe.whitelist()
def preview() -> dict:
	"""What importing the attached CSV would do, row by row. Writes nothing.

	Returns:
	    {summary: {counts, units, value}, rows: [{line, upc, name, outcome,
	    item_code, message, qty, cost}], warehouse}.
	"""
	frappe.only_for(ROLE)
	# lazy: runs in the request (one catalog lookup per unmatched UPC); move to a
	# job like start_import if previews of large files time out.
	doc, rows, warehouse = _settings()
	imp.check_posting(warehouse, doc.posting_date)
	planned = imp.plan(rows, warehouse)
	return {"summary": imp.summarize(planned), "rows": _public(planned), "warehouse": warehouse}


@frappe.whitelist()
def start_import() -> dict:
	"""Import the attached CSV in the background. The result is saved on the
	page and announced with the ``inventory_import_done`` event.

	Returns:
	    {queued: True}.
	"""
	frappe.only_for(ROLE)
	doc, _rows, warehouse = _settings()
	imp.check_posting(warehouse, doc.posting_date)
	frappe.enqueue("ffl_core.ffl_core.doctype.inventory_import.inventory_import.run_import",
		queue="long", timeout=3600, user=frappe.session.user,
		job_id="inventory_import", deduplicate=True)
	return {"queued": True}


def run_import(user: str | None = None) -> None:
	doc, rows, warehouse = _settings()
	try:
		planned = imp.plan(rows, warehouse)
		result = imp.run(planned, warehouse=warehouse, posting_date=doc.posting_date,
			source=doc.csv_file)
		payload = {"summary": result["summary"], "rows": _public(result["rows"]),
			"reconciliations": result["reconciliations"], "warehouse": warehouse}
	except Exception as exc:  # noqa: BLE001 — report it on the page, not only in the log
		frappe.db.rollback()
		frappe.log_error(title="Inventory Import failed", message=frappe.get_traceback())
		payload = {"error": frappe.utils.strip_html(str(exc))}
	payload["finished_at"] = str(frappe.utils.now_datetime())
	frappe.db.set_single_value("Inventory Import", "last_run", json.dumps(payload, default=str))
	frappe.db.commit()
	frappe.publish_realtime("inventory_import_done", payload, user=user)
