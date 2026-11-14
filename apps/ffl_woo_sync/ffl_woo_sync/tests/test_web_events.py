"""The website's events endpoint (client_api.web_events): POST only, the stock-role gate, and that the
website's API user (a Stock Manager with no marketing rights) can file events given as JSON. What each
event does: ffl_core.tests.test_marketing_events."""
from __future__ import annotations

import json

import frappe

from ffl_core.marketing import CONTACT
from ffl_core.tests.crm_support import user
from ffl_core.tests.marketing_support import MarketingTestCase, address
from ffl_woo_sync.woocommerce import client_api

SITE_USER = "web-events-site@example.com"
CLERK = "web-events-clerk@example.com"
MANAGER = "web-events-manager@example.com"


class TestWebEventsEndpoint(MarketingTestCase):
	def _call(self, as_user: str, events):
		frappe.set_user(as_user)
		try:
			return client_api.web_events(events=events)
		finally:
			frappe.set_user("Administrator")

	def _batch(self, email: str) -> list[dict]:
		return [{"id": "evt-" + frappe.generate_hash(length=8), "type": "contact.subscribed",
			"occurred_at": "2026-11-13T18:04:05+00:00", "data": {"email": email, "tags": ["new-arrivals"]}},
			{"id": "evt-" + frappe.generate_hash(length=8), "type": "alert.requested",
			"occurred_at": "2026-11-13T18:04:05+00:00", "data": {"email": email, "kind": "model", "model_key": "sks"}}]

	def test_post_only_and_whitelisted(self):
		self.assertIn(client_api.web_events, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[client_api.web_events], ["POST"])

	def test_the_website_user_files_events_from_json(self):
		user(SITE_USER, "Stock Manager")
		email = address("web")
		batch = self._batch(email)
		out = self._call(SITE_USER, json.dumps(batch))
		self.assertEqual(out, {"accepted": [e["id"] for e in batch], "rejected": []})
		self.assertEqual(frappe.db.get_value(CONTACT, email, "status"), "Subscribed")
		self.assertTrue(frappe.db.exists("Marketing Request", {"contact": email, "model_key": "sks"}))

	def test_anyone_without_a_stock_role_is_refused(self):
		user(CLERK, "Sales User")
		user(MANAGER, "Sales Manager")
		email = address("web")
		for who in (CLERK, MANAGER):
			with self.assertRaises(frappe.PermissionError):
				self._call(who, self._batch(email))
		self.assertFalse(frappe.db.exists(CONTACT, email))
