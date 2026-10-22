"""`payments.hub` — the Payments settings-hub page's server contract.

Mirrors test_distributor_hub in spirit: overview shape, mutation rules,
probe shapes. Settings flips ride SettingsSandbox so a run leaves the site's
switches exactly as it found them; the probe tests patch the one gateway
call (the Payroc adapter's `list_terminals`), never the network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.payments import hub

PAYROC_SETTINGS = "Payroc Settings"


class PaymentHubOverview(SettingsSandbox, FrappeTestCase):
	def test_overview_row_shape(self):
		out = hub.overview()
		rows = {r["code"]: r for r in out["processors"]}
		self.assertIn("payroc", rows)
		p = rows["payroc"]
		self.assertEqual(p["label"], "Payroc")
		self.assertEqual(p["kind"], "Credit Card")
		self.assertEqual(p["settings_doctype"], PAYROC_SETTINGS)
		self.assertEqual(p["mode_of_payment"], "Credit Card")
		self.assertIn(p["enabled"], (0, 1))
		self.assertIsInstance(p["ready"], bool)
		self.assertEqual([h["label"] for h in p["health"]],
			["Token refreshed", "Last charge"])
		self.assertTrue(p["can_write"])  # Administrator
		self.assertTrue(out["can_write"])

	def test_overview_reads_the_switch_not_the_form_cache(self):
		"""The card's enabled state is the same raw-row read the integration
		gate uses — flip the row, the hub answers the new truth immediately,
		and ready collapses with it (a switched-off processor is never
		"ready" no matter what credentials it holds)."""
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		row = hub.overview()["processors"][0]
		self.assertEqual(row["enabled"], 0)
		self.assertFalse(row["ready"])
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self.assertEqual(hub.overview()["processors"][0]["enabled"], 1)

	def test_endpoints_are_system_manager_only(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			hub.overview()
		with self.assertRaises(frappe.PermissionError):
			hub.toggle("payroc", 0)
		with self.assertRaises(frappe.PermissionError):
			hub.test_connection("payroc")


class PaymentHubToggle(SettingsSandbox, FrappeTestCase):
	def test_unknown_processor_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			hub.toggle("stripe", 1)
		with self.assertRaises(frappe.ValidationError):
			hub.test_connection("stripe")

	def test_disable_persists_through_the_document(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		out = hub.toggle("payroc", 0)
		self.assertEqual(out, {"code": "payroc", "enabled": 0})
		self.assertEqual(
			frappe.db.get_single_value(PAYROC_SETTINGS, "enabled"), 0)

	def test_enable_routes_through_the_real_save(self):
		"""The contract worth pinning is the ROUTE, not the outcome: enabling
		goes through doc.save(), where the settings form's own rule (
		credentials mandatory while enabled) fires server-side. Whether this
		site holds an API key is site state, so the save itself is mocked —
		the rule's existence is the settings doctype's test surface, not
		ours; what must never regress here is someone "optimising" this into
		a db_set that walks straight past that rule."""
		doc = MagicMock()
		with patch.object(hub.frappe, "get_doc", return_value=doc) as get_doc:
			out = hub.toggle("payroc", "1")
			get_doc.assert_called_once_with(PAYROC_SETTINGS)
			self.assertEqual(doc.enabled, 1)
			doc.save.assert_called_once_with()
		# MagicMock's attribute survives int(); the endpoint echoes it back.
		self.assertEqual(out["code"], "payroc")


class PaymentHubProbe(SettingsSandbox, FrappeTestCase):
	def test_switched_off_probes_nothing(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		with patch("ffl_integrations.payroc.adapter.PayrocProcessor.list_terminals") as disco:
			checks = hub.test_connection("payroc")["checks"]
			disco.assert_not_called()
		self.assertEqual(len(checks), 1)
		self.assertIsNone(checks[0]["ok"])
		self.assertIn("switched off", checks[0]["detail"])

	def test_on_but_unconfigured_names_the_credentials(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		with patch("ffl_integrations.payroc.adapter.PayrocProcessor.ready", return_value=False), patch("ffl_integrations.payroc.adapter.PayrocProcessor.list_terminals") as disco:
			checks = hub.test_connection("payroc")["checks"]
			disco.assert_not_called()
		self.assertTrue(checks[0]["ok"])  # switch
		self.assertFalse(checks[1]["ok"])  # credentials
		self.assertIn("API Key", checks[1]["detail"])

	def test_gateway_error_is_a_failed_check_not_a_crash(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		with patch("ffl_integrations.payroc.adapter.PayrocProcessor.ready", return_value=True), patch("ffl_integrations.payroc.adapter.PayrocProcessor.list_terminals",
				return_value={"error": "401 nope"}):
			checks = hub.test_connection("payroc")["checks"]
		self.assertFalse(checks[1]["ok"])
		self.assertIn("401 nope", checks[1]["detail"])

	def test_gateway_success_counts_terminals(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		with patch("ffl_integrations.payroc.adapter.PayrocProcessor.ready", return_value=True), patch("ffl_integrations.payroc.adapter.PayrocProcessor.list_terminals",
				return_value={"allowedTerminals": [{"id": 1}, {"id": 2}]}):
			checks = hub.test_connection("payroc")["checks"]
		gateway = checks[1]
		self.assertTrue(gateway["ok"])
		self.assertIn("2 terminal(s)", gateway["detail"])
		self.assertEqual(checks[2]["label"], "Default terminal")
