"""pdf_archive — private copies of signed 4473 PDFs on their Form 4473 Log row."""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_integrations.fastbound import pdf_archive
from ffl_integrations.fastbound.client import FastBoundConfigError, FastBoundError

_CLIENT = "ffl_integrations.fastbound.pdf_archive.FastBoundClient.from_settings"
_ENABLED = "ffl_integrations.fastbound.pdf_archive.fastbound_enabled"


def _pdf() -> bytes:
	"""A real one-page PDF: File validates a .pdf's content on insert."""
	from pypdf import PdfWriter
	writer, buf = PdfWriter(), io.BytesIO()
	writer.add_blank_page(width=72, height=72)
	writer.write(buf)
	return buf.getvalue()


PDF = _pdf()  # once: a writer stamps a fresh document id per call


def _pdf_with_script() -> bytes:
	"""A PDF that opens with a script, as FastBound's fillable 4473 does."""
	from pypdf import PdfWriter
	writer, buf = PdfWriter(), io.BytesIO()
	writer.add_blank_page(width=72, height=72)
	writer.add_js("this.calculateNow();")
	writer.write(buf)
	return buf.getvalue()


class Store4473Pdf(FrappeTestCase):
	def setUp(self):
		frappe.db.delete("Form 4473 Log", {"form4473_id": ("like", "ARCH-%")})
		self.log = record_4473(form4473_id="ARCH-1", pos_invoices=["ACC-PSINV-A-1"])

	def _store(self, client=None, *, enabled=True, log=None):
		with patch(_ENABLED, return_value=enabled), patch(_CLIENT, return_value=client or MagicMock()):
			return pdf_archive.store_4473_pdf(log or self.log)

	def _row(self, log=None):
		return frappe.db.get_value("Form 4473 Log", log or self.log,
			["pdf_status", "pdf_file", "pdf_attempts", "pdf_error"], as_dict=True)

	def test_stores_a_private_copy_attached_to_the_row(self):
		client = MagicMock()
		client.download_4473_pdf.return_value = PDF
		self.assertEqual(self._store(client), "Stored")
		client.download_4473_pdf.assert_called_once_with("ARCH-1")
		row = self._row()
		f = frappe.get_doc("File", {"file_url": row.pdf_file})
		self.assertEqual((f.is_private, f.attached_to_doctype, f.attached_to_name),
			(1, "Form 4473 Log", self.log))
		self.assertEqual(f.file_name, "4473-ACC-PSINV-A-1.pdf")
		self.assertEqual(f.get_content(encodings=[]), PDF)

	def test_a_4473_that_carries_form_scripts_is_stored_as_it_came(self):
		from frappe.utils.pdf import pdf_contains_js
		scripted = _pdf_with_script()
		self.assertTrue(pdf_contains_js(scripted))      # what File refuses in an upload
		client = MagicMock()
		client.download_4473_pdf.return_value = scripted
		self.assertEqual(self._store(client), "Stored")
		f = frappe.get_doc("File", {"file_url": self._row().pdf_file})
		self.assertEqual(f.get_content(encodings=[]), scripted)

	def test_a_body_that_is_not_a_pdf_counts_as_a_failed_attempt(self):
		client = MagicMock()
		client.download_4473_pdf.return_value = b"<html>maintenance</html>"
		self.assertEqual(self._store(client), "Pending")
		row = self._row()
		self.assertEqual((row.pdf_attempts, row.pdf_file), (1, None))

	def test_a_404_is_retried_and_only_the_last_one_ends_not_found(self):
		"""Right after form4473.completed FastBound may not have the PDF yet."""
		client = MagicMock()
		client.download_4473_pdf.side_effect = FastBoundError("GET → 404", status_code=404)
		for _ in range(pdf_archive.MAX_ATTEMPTS - 1):
			self.assertEqual(self._store(client), "Pending")
		self.assertEqual(self._store(client), "Not Found")

	def test_a_refused_request_is_not_this_4473s_fault_and_is_not_counted(self):
		"""A rotated key / blank audit user must not burn the whole backlog to Failed."""
		for status in (400, 401, 403, 429):
			client = MagicMock()
			client.download_4473_pdf.side_effect = FastBoundError(f"GET → {status}", status_code=status)
			self.assertEqual(self._store(client), "Pending")
		row = self._row()
		self.assertEqual(row.pdf_attempts, 0)
		self.assertIn("429", row.pdf_error)

	def test_force_stores_a_row_that_had_given_up(self):
		frappe.db.set_value("Form 4473 Log", self.log, "pdf_status", "Failed")
		client = MagicMock()
		client.download_4473_pdf.return_value = PDF
		self.assertEqual(self._store(client), "Failed")
		client.download_4473_pdf.assert_not_called()
		with patch(_ENABLED, return_value=True), patch(_CLIENT, return_value=client):
			self.assertEqual(pdf_archive.store_4473_pdf(self.log, force=True), "Stored")

	def test_failures_are_counted_and_the_fifth_gives_up(self):
		client = MagicMock()
		client.download_4473_pdf.side_effect = FastBoundError("GET → 500", status_code=500)
		for _ in range(pdf_archive.MAX_ATTEMPTS - 1):
			self.assertEqual(self._store(client), "Pending")
		self.assertEqual(self._store(client), "Failed")
		row = self._row()
		self.assertEqual(row.pdf_attempts, pdf_archive.MAX_ATTEMPTS)
		self.assertIn("500", row.pdf_error)

	def test_disabled_or_unconfigured_fastbound_leaves_it_pending(self):
		client = MagicMock()
		self.assertEqual(self._store(client, enabled=False), "Pending")
		client.download_4473_pdf.assert_not_called()
		with patch(_ENABLED, return_value=True), \
				patch(_CLIENT, side_effect=FastBoundConfigError("no key")):
			self.assertEqual(pdf_archive.store_4473_pdf(self.log), "Pending")
		self.assertEqual(self._row().pdf_attempts, 0)

	def test_paper_rows_are_never_fetched(self):
		paper = record_4473(pos_invoices=["ACC-PSINV-A-2"], source="Paper / override")
		client = MagicMock()
		self.assertEqual(self._store(client, log=paper), "No PDF")
		client.download_4473_pdf.assert_not_called()


class Sweep4473Pdfs(FrappeTestCase):
	def setUp(self):
		frappe.db.delete("Form 4473 Log", {"form4473_id": ("like", "ARCH-%")})

	def test_sweep_takes_only_pending_rows_and_at_most_a_batch(self):
		for i in range(3):
			record_4473(form4473_id=f"ARCH-S{i}")
		done = record_4473(form4473_id="ARCH-DONE")
		frappe.db.set_value("Form 4473 Log", done, "pdf_status", "Stored")
		seen = []
		with patch(_ENABLED, return_value=True), patch.object(pdf_archive, "SWEEP_BATCH", 2), \
				patch.object(pdf_archive, "store_4473_pdf", side_effect=seen.append):
			pdf_archive.sweep_pending_4473_pdfs()
		self.assertEqual(len(seen), 2)
		self.assertNotIn(done, seen)
		self.assertEqual({frappe.db.get_value("Form 4473 Log", n, "pdf_status") for n in seen},
			{"Pending"})

	def test_sweep_is_a_no_op_when_fastbound_is_off(self):
		record_4473(form4473_id="ARCH-OFF")
		with patch(_ENABLED, return_value=False), \
				patch.object(pdf_archive, "store_4473_pdf") as store:
			pdf_archive.sweep_pending_4473_pdfs()
		store.assert_not_called()


class RetryRefusedAsUnsafe(FrappeTestCase):
	def test_only_the_rows_file_refused_go_back_to_pending(self):
		from ffl_integrations.patches import retry_4473_pdfs_refused_as_unsafe as patch_
		frappe.db.delete("Form 4473 Log", {"form4473_id": ("like", "ARCH-%")})
		refused = record_4473(form4473_id="ARCH-R", pos_invoices=["ACC-PSINV-A-7"])
		gone = record_4473(form4473_id="ARCH-G", pos_invoices=["ACC-PSINV-A-8"])
		frappe.db.set_value("Form 4473 Log", refused, {"pdf_status": "Failed", "pdf_attempts": 5,
			"pdf_error": "This PDF cannot be uploaded as it contains unsafe content."})
		frappe.db.set_value("Form 4473 Log", gone, {"pdf_status": "Failed", "pdf_attempts": 5, "pdf_error": "timeout"})
		patch_.execute()
		self.assertEqual(frappe.db.get_value("Form 4473 Log", refused, ["pdf_status", "pdf_attempts", "pdf_error"]), ("Pending", 0, ""))
		self.assertEqual(frappe.db.get_value("Form 4473 Log", gone, "pdf_status"), "Failed")
