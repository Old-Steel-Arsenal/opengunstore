"""Idempotent setup for customer-custody intake: the custody warehouse's flag,
the transfer gun's flag, and the receipt-level "this is a transfer" declaration.

A customer's gun in our safe is not inventory.  We possess it; we do not own
it.  Until 2026-07-20 the system had no way to say that, so a transfer was
received like a purchase — into the sellable warehouse, at a made-up cost — and
the incident followed: a customer's gun counted as sellable stock, $10 of someone
else's property became inventory asset and phantom COGS, and the same $10 hung
in SRBNB against a bill that would never arrive (reversed by hand later).

Three fields, three independent jobs:

  - ``Warehouse.is_custody_warehouse`` — marks the place customer property
    lives.  Deliberately generic: a repair drop-off and a layaway gun
    have the same legal shape as a transfer, so the WAREHOUSE says only "not
    ours".  Every exclusion downstream keys off this flag, never off the
    warehouse's name, so renaming it can never quietly re-open the store's
    inventory to customer property.
  - ``Serial No.is_transfer_gun`` — marks the individual gun as a customer's
    transfer.  Transfer-specific on purpose, leaving room for a sibling
    ``is_repair_gun`` / layaway flag to be added the same way later without
    re-cutting this one's meaning.
  - ``Purchase Receipt.is_ffl_transfer`` — the receipt-level declaration the
    Receive Goods checkbox writes.  Named to match the POS Invoice field of the
    same name and meaning, so the two ends of a transfer read alike:
    ``Purchase Receipt.is_ffl_transfer`` (the gun arrives) ↔
    ``POS Invoice.is_ffl_transfer`` (the gun goes home).

Registration route: ``create_custom_fields`` at after_migrate (module=None →
NOT captured by the ffl_core Custom Field fixture filter, NOT committed to
fixtures/custom_field.json) — the same route setup/consignment.py and
setup/pay_on_sale.py take.  Chosen over the fixture route so that a patch which
must READ one of these columns can call ``ensure()`` itself first: fixtures sync
AFTER post_model_sync patches, so a fixture-only field does not exist yet on the
migrate that introduces it.  Lazy + never raises — a throwing after_migrate hook
aborts the whole ``bench migrate``; failures are logged and retried next call.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.utils import commit

# ERPNext appends " - {company abbr}" on insert, matching every other warehouse
# in this system. "Customer Custody" over the provisional "Transfer
# Custody": the warehouse is the generic not-ours location; the
# transfer-specific meaning lives on the serial's own flag.
CUSTODY_WAREHOUSE_NAME = "Customer Custody"

_WAREHOUSE_FIELDS = {
	"Warehouse": [
		{
			# Anchored on a CORE Warehouse field (always present) rather than a
			# sibling custom field, so ordering never depends on which setup
			# module ran first (setup/pay_on_sale.py's rule).
			"fieldname": "is_custody_warehouse",
			"fieldtype": "Check",
			"label": "Customer Custody Warehouse",
			"insert_after": "company",
			"description": "Stock here is customer property: never sold or listed on the website.",
			# System state: a hand-flipped flag either strands real inventory or
			# re-exposes customer property to the storefront.
			"read_only": 1,
			"in_standard_filter": 1,
		}
	]
}

_SERIAL_FIELDS = {
	"Serial No": [
		{
			"fieldname": "is_transfer_gun",
			"fieldtype": "Check",
			"label": "Customer Transfer Gun",
			"insert_after": "warehouse",
			"description": "A customer's gun on an FFL transfer: never listed or sold, and costed at $0.",
			# Read-only for the same reason consigned_out is: clearing it by hand
			# is exactly how a customer's gun would reach the storefront.
			"read_only": 1,
			"in_standard_filter": 1,
		}
	]
}

_PURCHASE_RECEIPT_FIELDS = {
	"Purchase Receipt": [
		{
			"fieldname": "is_ffl_transfer",
			"fieldtype": "Check",
			"label": "FFL Transfer (customer's gun)",
			# Anchored on a CORE Purchase Receipt field, per this module's own rule
			# above: "ffl_transfer_for_customer" is a fixtures-owned custom field, so
			# anchoring there would make ordering depend on whether the fixture sync
			# or this ensure() ran first.
			"insert_after": "supplier",
			"description": "Receives a customer's gun into custody at $0, not as store stock.",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
		}
	]
}


def ensure() -> dict:
	"""Create the custody custom fields. Idempotent; never raises (runs in
	after_migrate — failures are logged, the next call retries).

	Deliberately does NOT create the custody warehouse: warehouse creation
	needs a company, and a fresh install runs hooks before ERPNext's setup
	wizard seeds one. The warehouse is created on demand instead, by
	``ffl_core.warehouses.ensure_custody_warehouse`` at the first transfer
	receive — the same lazy pattern setup/transfer_fee.py uses for its item."""
	try:
		create_custom_fields(
			{**_WAREHOUSE_FIELDS, **_SERIAL_FIELDS, **_PURCHASE_RECEIPT_FIELDS},
			ignore_validate=True,
		)
		commit()
		return {"ok": 1}
	except Exception:
		frappe.log_error(title="custody.ensure failed", message=frappe.get_traceback())
		return {"error": 1}
