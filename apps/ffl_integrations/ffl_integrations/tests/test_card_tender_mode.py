"""payments.api.card_tender_mode: whether a Credit Card tender must go through the
terminal — the same go-live gate (processor on, configured, with a device) every
charge path reads."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.payments import api


class CardTenderMode(FrappeTestCase):

	def setUp(self):
		frappe.set_user("Administrator")

	def test_answers_the_device_gate(self):
		self.assertEqual(api.card_tender_mode(), {"integrated": api._integration_ready(require_device=True)})
		with patch.object(api, "_integration_ready", return_value=True) as ready:
			self.assertEqual(api.card_tender_mode(), {"integrated": True})
		ready.assert_called_once_with(require_device=True)
		with patch.object(api, "_integration_ready", return_value=False):
			self.assertEqual(api.card_tender_mode(), {"integrated": False})

	def test_charge_roles_only(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			api.card_tender_mode()
