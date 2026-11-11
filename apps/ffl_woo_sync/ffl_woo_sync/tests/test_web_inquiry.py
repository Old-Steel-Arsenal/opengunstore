"""The website's inquiry endpoint (client_api.web_inquiry): the role gate, POST only, JSON arguments, and
that a Stock Manager (the website's API user) can file with no CRM rights of their own. The filing rules
are tested in ffl_core.tests.test_crm_inquiries."""
from __future__ import annotations

import json

import frappe

from ffl_core.tests.crm_support import CLERK, CRMTestCase, user
from ffl_woo_sync.woocommerce import client_api

SITE_USER = "web-inquiry-site@example.com"


class TestWebInquiryEndpoint(CRMTestCase):
	def _call(self, as_user: str, **over):
		args = {"kind": "sell_gun", "event_id": "evt-endpoint-1", "email": "endpoint-buyer@example.org",
			"full_name": "Pat Buyer", "message": "Selling a rifle", "details": json.dumps({"Make": "Colt", "Model": "Python"}),
			"photos": json.dumps(["https://cdn.example.org/a.jpg"])}
		args.update(over)
		frappe.set_user(as_user)
		try:
			return client_api.web_inquiry(**args)
		finally:
			frappe.set_user("Administrator")

	def test_post_only_and_whitelisted(self):
		self.assertIn(client_api.web_inquiry, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[client_api.web_inquiry], ["POST"])

	def test_the_website_user_files_an_inquiry_from_json_arguments(self):
		user(SITE_USER, "Stock Manager")
		out = self._call(SITE_USER)
		self.assertEqual((out["filed"], out["doctype"], out["created"]), (True, "CRM Lead", True))
		note = frappe.db.get_value("FCRM Note", {"reference_docname": out["name"]}, "content")
		self.assertIn("<b>Make</b>: Colt", note)
		self.assertIn("https://cdn.example.org/a.jpg", note)
		self.assertEqual(self._call(SITE_USER)["created"], False, "a repeat of the event files nothing")
		self.assertEqual(frappe.db.count("CRM Lead", {"email": "endpoint-buyer@example.org"}), 1)

	def test_anyone_without_a_stock_role_is_refused(self):
		user(CLERK, "Sales User")
		with self.assertRaises(frappe.PermissionError):
			self._call(CLERK, event_id="evt-endpoint-2")
		self.assertFalse(frappe.db.exists("CRM Lead", {"email": "endpoint-buyer@example.org"}))

	def test_a_refused_kind_is_a_validation_error(self):
		user(SITE_USER, "Stock Manager")
		with self.assertRaises(frappe.ValidationError):
			self._call(SITE_USER, kind="complaint")
