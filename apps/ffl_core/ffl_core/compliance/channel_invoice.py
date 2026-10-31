"""Is this Sales Invoice the accounting mirror of a sale made somewhere else?

Some invoices are not counter sales. A web order and a GunBroker order are
fulfilled on their own record — the guns leave under dispositions booked before
the invoice exists — and the invoice is raised afterwards purely so the money
reaches the ledger. Every rule that assumes "an invoice is a sale being rung up
here" has to step aside for them, and there are eight such rules:

* the interstate-tax engine, the customer-exemption engine and the freight-tax
  hook — the channel already charged tax at checkout and there is no shipping
  Address here to judge;
* the sale-receipt mailer — the buyer already got the channel's own receipt;
* the counter-order marker behind Edit and Cancel Order — those reverse a GL
  entry without touching the bound book, which for a shipped gun is a hole;
* the Pending Order queue and the counter-hold registry — fulfilment lives on
  the channel's record, so these invoices would sit in the queue for ever
  showing buttons that do the wrong thing;
* the Sales Report's channel split;
* the Item form's Pending badge.

Before this module each of those spelled the rule out as ``woo_online_order``,
which is why adding a second channel meant finding all eight. Now there is one
predicate and the next channel is one line.

Deliberately field-name-only: no import of ffl_woo_sync or ffl_integrations,
because ffl_core must run with neither installed and this is called from the
hot path of every invoice submit.
"""
from __future__ import annotations

import frappe

# (fieldname, doctype-that-owns-it) for every channel whose sales are mirrored
# into a Sales Invoice. Order is only cosmetic.
CHANNEL_INVOICE_FIELDS: tuple[str, ...] = ("woo_online_order", "gunbroker_order")


def is_channel_mirrored_invoice(doc) -> bool:
	"""True when this document is a sales-channel mirror, not a counter sale.

	Args:
	    doc: A Sales Invoice (or POS Invoice) document, or anything with the
	        same attributes — read with ``getattr`` so a plain dict-like row
	        works and a doctype without the fields simply answers False.

	Returns:
	    Whether any channel claims this invoice.
	"""
	return any(getattr(doc, field, None) for field in CHANNEL_INVOICE_FIELDS)


# Extensions declare Sales Invoice Link fieldnames here (hooks
# gunshop_pending_queue_exclusion_fields, docs/extensions/hooks.md) for
# invoices whose fulfilment lives on the extension's own record — an outbound
# consignment settlement, say. Those are kept out of the Pending Order queue
# and the counter-hold registry exactly like a channel mirror, and out of the
# counter-order marker behind Edit / Cancel Order. They are NOT channel
# mirrors: the tax, receipt and Sales Report rules above still apply to them.
EXTENSION_EXCLUSION_HOOK = "gunshop_pending_queue_exclusion_fields"


def extension_exclusion_fields() -> tuple[str, ...]:
	return tuple(str(f) for f in (frappe.get_hooks(EXTENSION_EXCLUSION_HOOK) or []))


def is_fulfilled_elsewhere(doc) -> bool:
	"""True when this invoice's fulfilment does not live on the invoice: a
	channel mirror, or an invoice an extension claims through its declared
	Link field. The counter-order marker and the queue/hold exclusions share
	this one predicate."""
	return is_channel_mirrored_invoice(doc) \
		or any(getattr(doc, field, None) for field in extension_exclusion_fields())


def pending_queue_exclusions(doctype: str = "Sales Invoice") -> dict:
	"""Filters that keep channel-mirrored invoices out of the counter queue.

	Only for the channels whose column actually exists on this site. The guard
	is not defensive habit: both call sites build raw ``get_all`` filters, and a
	filter naming a column that has not been created yet does not degrade — it
	raises, taking down the Pending Order page and the counter-hold registry on
	any site that has ffl_core but not the integration app, or that is
	mid-migrate.

	Returns:
	    ``{fieldname: ("is", "not set")}`` for the columns present.
	"""
	return {
		field: ("is", "not set")
		for field in CHANNEL_INVOICE_FIELDS + extension_exclusion_fields()
		if frappe.db.has_column(doctype, field)
	}


def pending_si_base_filter() -> dict:
	"""Submitted, non-consolidated, non-return Sales Invoices whose fulfilment
	lives on the invoice: the shared base filter of the Pending Order queue
	(manual_order.list_pending_dispositions) and the counter-hold registry
	(reservations.counter_holds)."""
	return {"docstatus": 1, "is_consolidated": 0, "is_return": 0,
		**pending_queue_exclusions()}
