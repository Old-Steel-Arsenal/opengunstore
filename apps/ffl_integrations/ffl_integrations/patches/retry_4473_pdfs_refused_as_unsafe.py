"""4473 copies that File refused for carrying form scripts go back to Pending.

Frappe 16.56 began refusing PDFs with scripts, FastBound's 4473 among them, so every copy
since that upgrade ended Failed. pdf_archive now stores them; the hourly sweep fetches these.
"""
import frappe


def execute():
	frappe.db.sql("""
		UPDATE `tabForm 4473 Log` SET pdf_status = 'Pending', pdf_attempts = 0, pdf_error = ''
		WHERE pdf_status = 'Failed' AND pdf_error LIKE %s AND IFNULL(pdf_file, '') = ''
	""", "%unsafe content%")
