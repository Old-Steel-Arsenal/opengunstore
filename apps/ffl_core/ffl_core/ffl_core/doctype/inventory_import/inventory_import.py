"""Inventory Import: the Settings page that brings a new store's non-firearm
stock in from a CSV (see ffl_core.inventory_import)."""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core import inventory_import as imp
from ffl_core.utils import commit

ROLE = "System Manager"
PREVIEW, IMPORT = "preview", "import"


class InventoryImport(Document):
	def onload(self):
		if not self.warehouse:
			from ffl_core.api.item_admin import _default_warehouse
			self.set_onload("default_warehouse", _default_warehouse())


def _decode(content) -> str:
	"""Excel saves "CSV" as Windows-1252 unless told UTF-8; read either."""
	if not isinstance(content, bytes):
		return content
	try:
		return content.decode("utf-8-sig")
	except UnicodeDecodeError:
		return content.decode("cp1252", errors="replace")


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
	return doc, warehouse


def _public(planned: list[dict]) -> list[dict]:
	keep = ("line", "upc", "name", "outcome", "item_code", "message", "qty", "cost")
	return [{k: r.get(k) for k in keep} for r in planned]


@frappe.whitelist()
def start(mode: str = PREVIEW) -> dict:
	"""Preview or import the attached CSV in the background: one catalog lookup
	per unknown UPC outlasts a web request on a new store's file. The result is
	saved on the page (``last_run``) and announced with ``inventory_import_done``.

	Args:
	    mode: "preview" reads every row and writes nothing; "import" creates the
	        missing items and posts the opening stock.

	Returns:
	    {queued: True}.
	"""
	frappe.only_for(ROLE)
	if mode not in (PREVIEW, IMPORT):
		frappe.throw(_("Unknown mode {0}.").format(mode))
	doc, warehouse = _settings()
	imp.check_posting(warehouse, doc.posting_date)
	frappe.enqueue("ffl_core.ffl_core.doctype.inventory_import.inventory_import.run_job",
		queue="long", timeout=3600, user=frappe.session.user, mode=mode,
		job_id="inventory_import", deduplicate=True)
	return {"queued": True}


def run_job(mode: str = PREVIEW, user: str | None = None) -> None:
	payload: dict = {"mode": mode}
	try:
		doc, warehouse = _settings()
		payload.update(csv_file=doc.csv_file, warehouse=warehouse, posting_date=str(doc.posting_date))
		content = frappe.get_doc("File", {"file_url": doc.csv_file}).get_content()
		planned = imp.plan(imp.parse(_decode(content)), warehouse)
		if mode == IMPORT:
			result = imp.run(planned, warehouse=warehouse, posting_date=doc.posting_date,
				source=doc.csv_file)
			planned = result["rows"]
			payload["reconciliations"] = result["reconciliations"]
		payload.update(summary=imp.summarize(planned), rows=_public(planned))
	except Exception as exc:  # noqa: BLE001 — report it on the page, not only in the log
		frappe.db.rollback()
		frappe.log_error(title="Inventory Import failed", message=frappe.get_traceback())
		payload["error"] = frappe.utils.strip_html(str(exc))
	payload["finished_at"] = str(frappe.utils.now_datetime())
	frappe.db.set_single_value("Inventory Import", "last_run", json.dumps(payload, default=str))
	commit()
	frappe.publish_realtime("inventory_import_done", payload, user=user)
