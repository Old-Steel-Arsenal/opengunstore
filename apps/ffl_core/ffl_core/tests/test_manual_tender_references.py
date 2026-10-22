"""Manually-settled tender references — the shared PAN guard and the
record_payment / create_order reference gates for card and ACH.

The invoice-row gates (Zelle + standalone-terminal card) are tested from
ffl_integrations (test_integration_switches.CardReferenceGate); this file
covers the ffl_core half: the PAN shape test itself, and the Payment-Entry
legs (record_payment) that neither invoice gate ever sees.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.compliance.payment_mode import looks_like_pan, require_zelle_transaction_no
from ffl_core.tests.settings_sandbox import SettingsSandbox

PAYROC_SETTINGS = "Payroc Settings"


class LooksLikePan(FrappeTestCase):
	def test_real_card_shapes_are_caught(self):
		for value in ("4111111111111111",           # classic Visa test PAN
				"4111 1111 1111 1111",               # spaced
				"4111-1111-1111-1111",               # dashed
				"5500005555555559",                  # 16-digit Luhn-valid
				"340000000000009",                   # 15-digit Amex shape
				"6011000990139424"):                 # Discover shape
			self.assertTrue(looks_like_pan(value), value)

	def test_ordinary_references_pass(self):
		for value in ("APPR 004512",                 # terminal approval code
				"AUTH-903471",                       # alnum reference
				"12345678",                          # short digits (< 13)
				"99999999999999999999",              # 20 digits (> 19)
				"4111111111111112",                  # 16 digits, Luhn-INVALID
				"", "  "):
			self.assertFalse(looks_like_pan(value), value)


class ZelleGateRefusesPan(FrappeTestCase):
	def test_a_pan_typed_as_a_zelle_reference_is_refused(self):
		"""The PAN guard lives in the SHARED gate, so every tender that stores a
		reference gets it — a card number in reference_no is cardholder data in
		the GL no matter which mode it arrived under."""
		doc = frappe._dict({
			"docstatus": 1, "is_consolidated": 0,
			"payments": [frappe._dict({"mode_of_payment": "Zelle",
				"amount": 50.0, "reference_no": "4111 1111 1111 1111"})],
		})
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_zelle_transaction_no(doc)
		self.assertIn("card number", str(ctx.exception).lower())


class RecordPaymentReferenceGates(SettingsSandbox, FrappeTestCase):
	"""All three run BEFORE the invoice is even loaded — asserted by pointing
	them at an invoice that does not exist: the gate must throw first."""

	def test_ach_requires_a_reference(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.record_payment("SINV-DOES-NOT-EXIST", "ACH")
		self.assertIn("ACH", str(ctx.exception))

	def test_card_requires_approval_code_when_standalone(self):
		self.set_single(PAYROC_SETTINGS, "enabled", 0)
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.record_payment("SINV-DOES-NOT-EXIST", "Credit Card")
		self.assertIn("approval code", str(ctx.exception).lower())

	def test_virtual_terminal_card_requires_approval_code_while_terminal_is_live(self):
		"""With the A80 armed the terminal books its own reference, so a card typed
		in here was run on the Virtual Terminal — its code is the only link to the
		Payroc batch."""
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		with patch("ffl_integrations.integration_gate.payroc_ready", return_value=True):
			with self.assertRaises(frappe.ValidationError) as ctx:
				manual_order.record_payment("SINV-DOES-NOT-EXIST", "Credit Card")
			self.assertIn("virtual terminal", str(ctx.exception).lower())
			# With the code it passes the gate, then fails on the missing invoice —
			# proving the gate let it through rather than the call being unreachable.
			with self.assertRaises(frappe.DoesNotExistError):
				manual_order.record_payment("SINV-DOES-NOT-EXIST", "Credit Card",
					transaction_number="APPR 004512")

	def test_card_gate_is_inert_while_payroc_is_switched_on_but_not_set_up(self):
		"""Switch on, no credentials / device: cards are plain standalone tender
		and the store's own reference switch decides — no Virtual Terminal code."""
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 0)
		with patch("ffl_integrations.integration_gate.payroc_ready", return_value=False):
			with self.assertRaises(frappe.DoesNotExistError):
				manual_order.record_payment("SINV-DOES-NOT-EXIST", "Credit Card")

	def test_card_gate_fails_open_when_payroc_config_is_unreadable(self):
		"""A Payroc password that no longer decrypts breaks the terminal, not the
		counter's ability to record money it genuinely collected. (Unreadable counts
		as not configured, so the store's own reference switch decides — off here.)"""
		self.set_single(PAYROC_SETTINGS, "enabled", 1)
		self.set_single(PAYROC_SETTINGS, "require_card_reference", 0)
		with patch("ffl_integrations.integration_gate.payroc_ready",
				side_effect=frappe.ValidationError("cannot decrypt")):
			with self.assertRaises(frappe.DoesNotExistError):
				manual_order.record_payment("SINV-DOES-NOT-EXIST", "Credit Card")

	def test_a_pan_shaped_reference_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.record_payment("SINV-DOES-NOT-EXIST", "Zelle",
				transaction_number="4111111111111111")
		self.assertIn("card number", str(ctx.exception).lower())
