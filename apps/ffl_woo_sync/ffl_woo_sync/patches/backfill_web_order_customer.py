"""Give the web orders already on file their POS customer (``Woo Online Order.customer``), then link
the CRM deals that one customer's email names.

1. An order with a Sales Invoice or a register ticket takes that document's customer, in one
   UPDATE per source; the shared aggregate web customer is nobody's and is skipped.
2. An order still awaiting fulfilment, with an email and no invoice, takes the buyer the invoice
   would book (``revenue.intake_customer``: found by email, or made).
3. With the deal to POS switch on, every deal with no customer is linked by the same rule as a new
   one (``crm.customer.auto_link_deals``).

Cancelled and refunded orders only take a customer from an invoice; nothing is made for them.
Safe to run again: steps 1 and 2 skip an order that has a customer, and the resolver finds the
customer a first run made.
"""
from __future__ import annotations

import frappe

from ffl_core.customer_profile import WEB_STORE_CUSTOMER

ORDER = "Woo Online Order"
_ENDED = {"cancelled", "refunded", "failed", "trash"}


def execute():
	if not frappe.db.has_column(ORDER, "customer"):
		return
	from_invoices()
	resolve_pending()
	from ffl_core.crm.customer import auto_link_deals

	auto_link_deals()


def from_invoices() -> None:
	aggregate = frappe.db.get_value("Customer", {"customer_name": WEB_STORE_CUSTOMER}) or ""
	# Submitted documents first; a draft (a total left for review, a pickup ticket not rung up) last.
	for doctype, docstatus in (("Sales Invoice", 1), ("POS Invoice", 1), ("Sales Invoice", 0), ("POS Invoice", 0)):
		frappe.db.sql(f"""
			UPDATE `tab{ORDER}` o
			JOIN `tab{doctype}` d ON d.woo_online_order = o.name AND d.docstatus = %(docstatus)s
			JOIN `tabCustomer` c ON c.name = d.customer
			SET o.customer = d.customer
			WHERE IFNULL(o.customer, '') = '' AND d.customer != %(aggregate)s""",
			{"docstatus": docstatus, "aggregate": aggregate})


def resolve_pending() -> None:
	from ffl_woo_sync.woocommerce.revenue import intake_customer

	rows = frappe.get_all(ORDER, filters={"order_status": "Pending Fulfillment", "customer": ["is", "not set"],
		"customer_email": ["is", "set"]}, fields=["name", "woo_status"], order_by="creation asc")
	for row in rows:
		name = row.name
		if (row.woo_status or "").lower() in _ENDED:
			continue
		if frappe.db.exists("Sales Invoice", {"woo_online_order": name, "docstatus": ["<", 2]}) \
				or frappe.db.exists("POS Invoice", {"woo_online_order": name, "docstatus": ["<", 2]}):
			continue   # its invoice books the buyer
		customer = intake_customer(frappe.get_doc(ORDER, name))   # undone and logged on failure
		if customer:
			frappe.db.set_value(ORDER, name, "customer", customer, update_modified=False)
