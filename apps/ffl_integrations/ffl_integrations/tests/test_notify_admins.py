"""Tests for the operational-alert email helper."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.notifications import notify_admins, _THROTTLE_PREFIX, _safe_log_error


class TestNotifyAdmins(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.cache().delete_value(_THROTTLE_PREFIX + "unit_key")
		frappe.db.set_single_value("FFL Settings", "alert_recipients", "ops@example.com")

	@patch("frappe.sendmail")
	def test_sends_to_configured_recipients(self, mock_sendmail):
		queued = notify_admins("Sync down", "details", dedup_key="unit_key")
		self.assertTrue(queued)
		mock_sendmail.assert_called_once()
		self.assertIn("ops@example.com", mock_sendmail.call_args.kwargs["recipients"])

	@patch("frappe.sendmail")
	def test_throttles_repeat_within_window(self, mock_sendmail):
		notify_admins("Sync down", "details", dedup_key="unit_key")
		second = notify_admins("Sync down", "details", dedup_key="unit_key")
		self.assertFalse(second)
		mock_sendmail.assert_called_once()

	@patch("frappe.sendmail")
	def test_no_recipients_no_send(self, mock_sendmail):
		frappe.db.set_single_value("FFL Settings", "alert_recipients", "")
		with patch("ffl_core.alerts.get_system_managers", return_value=[]):
			queued = notify_admins("X", "Y", dedup_key="unit_key")
		self.assertFalse(queued)
		mock_sendmail.assert_not_called()

	@patch("frappe.logger")
	@patch("frappe.log_error", side_effect=Exception("log boom"))
	@patch("frappe.sendmail", side_effect=Exception("smtp boom"))
	def test_never_raises_even_if_logging_fails(self, _sm, _le, _logger):
		# Inner work raises AND the fallback log_error raises — must still return
		# False, never propagate (callers are scheduler/worker except-paths).
		result = notify_admins("X", "Y", dedup_key="unit_key")
		self.assertFalse(result)
