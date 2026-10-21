"""The card endpoints' old paths, kept for API clients.

Card money moved to the processor-neutral ``ffl_integrations.payments.api`` (Payroc is now one
adapter behind it, ``payroc.adapter``). Every endpoint that used to live here keeps its path and
its arguments, runs the same role gate, and hands the call to its new home. Nothing else lives
here: the POS, the counter pages and the doc_events call the new paths.

lazy: alias kept for API clients, drop in a later major.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_integrations.payments import api
from ffl_integrations.payments.api import _check_manager, _check_perm, _check_refund_perm


@frappe.whitelist(methods=["POST"])
def charge_pos_invoice(invoice_name: str, terminal: str | None = None,
		amount=None, description: str | None = None) -> dict:
	"""Old path of ffl_integrations.payments.api.charge_pos_invoice (start a card charge).

	Args:
		invoice_name: POS Invoice being tendered.
		terminal: Optional terminal id; defaults to the configured one.
		amount: Optional charge amount; defaults to the card-tender target.
		description: Optional statement descriptor.

	Returns:
		The new endpoint's answer: {ok, instruction_id, status, amount} or {"skip": True}.
	"""
	_check_perm()
	return api.charge_pos_invoice(invoice_name, terminal=terminal, amount=amount,
		description=description)


@frappe.whitelist(methods=["POST"])
def poll_pos_charge(invoice_name: str, instruction_id: str, expected_amount=None) -> dict:
	"""Old path of ffl_integrations.payments.api.poll_pos_charge (poll a card charge once).

	Args:
		invoice_name: POS Invoice being tendered.
		instruction_id: The charge handle returned by charge_pos_invoice.
		expected_amount: Optional approval-amount check.

	Returns:
		The new endpoint's answer.
	"""
	_check_perm()
	return api.poll_pos_charge(invoice_name, instruction_id, expected_amount=expected_amount)


@frappe.whitelist(methods=["POST"])
def cancel_pos_charge(invoice_name: str, instruction_id: str) -> dict:
	"""Old path of ffl_integrations.payments.api.cancel_pos_charge (cancel an in-flight charge).

	Args:
		invoice_name: POS Invoice being tendered.
		instruction_id: The charge handle to cancel.

	Returns:
		The new endpoint's answer.
	"""
	_check_perm()
	return api.cancel_pos_charge(invoice_name, instruction_id)


@frappe.whitelist(methods=["POST"])
def charge_sales_invoice(sales_invoice: str, amount=None, terminal: str | None = None) -> dict:
	"""Old path of ffl_integrations.payments.api.charge_sales_invoice (card charge on a counter order).

	Args:
		sales_invoice: Submitted Sales Invoice with an outstanding balance.
		amount: Charge amount, at most the outstanding balance (blank = the whole balance).
		terminal: Optional terminal id; defaults to the configured one.

	Returns:
		The new endpoint's answer.
	"""
	_check_perm()
	return api.charge_sales_invoice(sales_invoice, amount=amount, terminal=terminal)


@frappe.whitelist()
def poll_sales_invoice_charge(sales_invoice: str, instruction_id: str) -> dict:
	"""Old path of ffl_integrations.payments.api.poll_sales_invoice_charge.

	Args:
		sales_invoice: The Sales Invoice being charged.
		instruction_id: The charge handle returned by charge_sales_invoice.

	Returns:
		The new endpoint's answer.
	"""
	_check_perm()
	return api.poll_sales_invoice_charge(sales_invoice, instruction_id)


@frappe.whitelist(methods=["POST"])
def cancel_sales_invoice_charge(sales_invoice: str, instruction_id: str) -> dict:
	"""Old path of ffl_integrations.payments.api.cancel_sales_invoice_charge.

	Args:
		sales_invoice: The Sales Invoice being charged.
		instruction_id: The charge handle to cancel.

	Returns:
		The new endpoint's answer.
	"""
	_check_perm()
	return api.cancel_sales_invoice_charge(sales_invoice, instruction_id)


@frappe.whitelist()
def discover_terminals() -> dict:
	"""Old path of ffl_integrations.payments.api.list_terminals (connectivity smoke test).

	Returns:
		The new endpoint's answer: the processor's terminal list, {"error": ...} or {"skip": True}.
	"""
	_check_manager(_("Terminal discovery requires manager role."))
	return api.list_terminals()


@frappe.whitelist(methods=["POST"])
def void_pos_invoice(invoice_name: str, reason: str | None = None,
		unique_reference: str | None = None) -> dict:
	"""Old path of ffl_integrations.payments.api.void_pos_invoice (reverse an unsettled charge).

	Args:
		invoice_name: POS Invoice carrying the charge.
		reason: Optional audit reason.
		unique_reference: The specific charge row to void (required when several live captures exist).

	Returns:
		The new endpoint's answer.
	"""
	_check_refund_perm()
	return api.void_pos_invoice(invoice_name, reason=reason, unique_reference=unique_reference)


@frappe.whitelist(methods=["POST"])
def refund_pos_invoice(return_invoice_name: str, reason: str | None = None) -> dict:
	"""Old path of ffl_integrations.payments.api.refund_pos_invoice (refund a POS return's card portion).

	Args:
		return_invoice_name: The POS RETURN invoice.
		reason: Optional audit reason.

	Returns:
		The new endpoint's answer.
	"""
	_check_refund_perm()
	return api.refund_pos_invoice(return_invoice_name, reason=reason)


@frappe.whitelist(methods=["POST"])
def unlock_refund_lock(return_invoice_name: str, reason: str) -> dict:
	"""Old path of ffl_integrations.payments.api.unlock_refund_lock (manager unlock of a refund lock).

	Args:
		return_invoice_name: The locked POS RETURN invoice.
		reason: Required attestation — what the manager verified in the processor's portal.

	Returns:
		The new endpoint's answer.
	"""
	_check_manager(_("Unlocking a refund requires manager permission (Sales Manager)."))
	return api.unlock_refund_lock(return_invoice_name, reason)


@frappe.whitelist(methods=["POST"])
def confirm_out_of_band_refund(return_invoice_name: str, portal_reference: str,
		reason: str) -> dict:
	"""Old path of ffl_integrations.payments.api.confirm_out_of_band_refund.

	Args:
		return_invoice_name: The POS RETURN invoice being confirmed.
		portal_reference: The refund's reference from the processor's portal.
		reason: Why the refund went out of band (audited, mandatory).

	Returns:
		The new endpoint's answer.
	"""
	_check_manager(_("Confirming a portal refund requires manager role."))
	return api.confirm_out_of_band_refund(return_invoice_name, portal_reference, reason)
