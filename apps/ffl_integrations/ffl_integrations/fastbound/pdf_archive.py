"""Private copies of signed 4473 PDFs, attached to their Form 4473 Log row.

FastBound keeps every signed 4473, but a POS that can only show one live from
FastBound loses its whole history the day FastBound is switched off or the
account changes. So each FastBound 4473 on the log gets its PDF copied in once:
right after the form4473.completed webhook commits (webhook._record_4473_log)
and, for anything that missed that — the rows the backfill created, a
FastBound outage — by the hourly sweep.

The bytes are the buyer's 4473 (name, date of birth, address): they only ever
go into a private File, never into a log line or an Error Log.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError, FastBoundError
from ffl_integrations.integration_gate import fastbound_enabled

# A row that failed this many times stops being retried (status Failed).
MAX_ATTEMPTS = 5
# lazy: fixed rows per hourly sweep (~50 FastBound GETs/hour, so a 122-row backfill
# takes ~3 hours); raise it or add a second cron if a backlog ever outruns it.
SWEEP_BATCH = 50


def store_4473_pdf(log_name: str, force: bool = False) -> str:
	"""Copy one 4473's signed PDF into a private File on its log row.

	A row with a FastBound form id and no copy is fetched while Pending — or in
	any state with ``force`` (the viewer just fetched it live, so it exists).
	FastBound unconfigured or refusing the request itself (400 / 401 / 403 /
	429: a key, audit-user or rate problem, not this 4473) leaves the row
	Pending without counting; any other failure counts an attempt, and the
	MAX_ATTEMPTS-th ends it Failed — Not Found when FastBound kept answering 404.
	Returns the row's pdf_status afterwards.
	"""
	if not fastbound_enabled():
		return frappe.db.get_value("Form 4473 Log", log_name, "pdf_status") or ""
	log = frappe.get_doc("Form 4473 Log", log_name)
	if log.pdf_status == "Stored" or not log.form4473_id:
		return log.pdf_status
	if log.pdf_status != "Pending" and not force:
		return log.pdf_status
	try:
		pdf = FastBoundClient.from_settings().download_4473_pdf(log.form4473_id)
	except FastBoundConfigError:
		return log.pdf_status
	except FastBoundError as exc:
		if exc.status_code in (400, 401, 403, 429):
			return _set(log, pdf_error=str(exc)[:500])
		return _failed_attempt(log, exc, final="Not Found" if exc.status_code == 404 else "Failed")
	except Exception as exc:  # noqa: BLE001 — network: timeout / connection refused
		return _failed_attempt(log, exc)

	invoice = (log.pos_invoice or "").split(", ")[0]
	file_doc = frappe.new_doc("File")
	file_doc.update({
		"file_name": f"4473-{invoice or log.form4473_id}.pdf",
		"attached_to_doctype": "Form 4473 Log",
		"attached_to_name": log.name,
		"attached_to_field": "pdf_file",
		"is_private": 1,
		"content": pdf,
	})
	file_doc.flags.ignore_permissions = True
	# FastBound's 4473 carries form scripts, which File refuses in an upload. This one is
	# no upload: it comes from FastBound over the store's own key and is kept as signed.
	file_doc.check_content = lambda: _require_pdf(pdf)
	try:
		file_doc.insert()  # File validates a .pdf's content: a truncated body fails here
	except Exception as exc:  # noqa: BLE001 — counted like any failed fetch
		return _failed_attempt(log, exc)
	return _set(log, pdf_file=file_doc.file_url, pdf_status="Stored", pdf_error="")


def sweep_pending_4473_pdfs() -> None:
	"""Hourly: copy the PDFs still Pending, newest first, SWEEP_BATCH per run.
	Each row commits on its own, so one bad row never costs the others."""
	if not fastbound_enabled():
		return
	names = frappe.get_all("Form 4473 Log",
		filters={"pdf_status": "Pending", "form4473_id": ("is", "set")},
		pluck="name", order_by="completed_on desc", limit=SWEEP_BATCH)
	for name in names:
		try:
			store_4473_pdf(name)
		except Exception:  # noqa: BLE001 — e.g. the File insert; next run retries
			frappe.log_error(title=f"4473 PDF copy failed for {name}", message=frappe.get_traceback())
		commit()


def _require_pdf(content: bytes) -> None:
	"""Raise unless the bytes open as a PDF (a truncated body, an HTML error page)."""
	import io

	from pypdf import PdfReader

	PdfReader(io.BytesIO(content))


def _failed_attempt(log, exc: Exception, final: str = "Failed") -> str:
	attempts = (log.pdf_attempts or 0) + 1
	return _set(log, pdf_attempts=attempts, pdf_error=str(exc)[:500],
		pdf_status=final if attempts >= MAX_ATTEMPTS else "Pending")


def _set(log, **values) -> str:
	frappe.db.set_value("Form 4473 Log", log.name, values, update_modified=False)
	return values.get("pdf_status", log.pdf_status)
