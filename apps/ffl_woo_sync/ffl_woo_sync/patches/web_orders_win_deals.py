"""Let the paid web orders already on file win the deal or the lead they closed (``crm.web_orders``),
oldest order first, with the deal to POS switch on.

Each paid order that is not cancelled or refunded wins its customer's open deal, or converts and wins
their newest open lead, made before the order (:func:`ffl_core.crm.web_orders.web_order_paid`). Here a
lead staff set to a Won status by hand, never converted and with no deal, counts too: they closed it
because the system did not. Nobody is told.

Safe to run again: an order wins once, and a won deal or a converted lead is not open any more.
"""
from __future__ import annotations

import frappe

ORDER = "Woo Online Order"


def execute():
	from ffl_core.crm import installed, pos_link_on

	if not installed() or not pos_link_on():
		return
	from ffl_core.crm.setup import ensure_custom_fields
	from ffl_core.crm.web_orders import web_order_paid
	from ffl_woo_sync.woocommerce.fulfillment import _PAID_STATUSES

	ensure_custom_fields()   # after_migrate's setup runs after the patches
	# lazy: every paid order on file, one savepoint each (the patch runs once); upgrade path: a job per batch.
	for order in frappe.get_all(ORDER, filters={"order_status": ["!=", "Cancelled"], "woo_status": ["in", sorted(_PAID_STATUSES)],
			"customer_email": ["is", "set"]}, pluck="name", order_by="creation asc"):
		web_order_paid(order, hand_closed=True)
