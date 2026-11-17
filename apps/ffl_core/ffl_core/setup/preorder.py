"""Idempotent setup for counter pre-orders (rules in ``ffl_core.preorder``).

* **Item** — ``ffl_preorder_available`` (how many more the store will take on pre-order; 0 = none)
  and ``ffl_preorder_eta``. Staff keep them; a pre-order takes from the count, a cancellation
  gives back.
* **Sales Order** — ``ffl_is_preorder``: a submitted order taken before the goods are here. Not
  copied to an amendment (an amended order took no pre-order slots).
* **Row / header links** — ``ffl_preorder`` (the Sales Order's name) on POS Invoice, POS Invoice
  Item and Sales Invoice Item (copied on return and consolidation), what the payment guard, the held
  sum and the reports read. A Data field, not a Link: a cancelled pre-order's payments are returned
  after the order is cancelled, and a Link to a cancelled document refuses that return (and its
  merge into a Sales Invoice); the payment guard checks the name instead.
* **Pre-order Deposits** — a current LIABILITY account per company. Its own account, not Customer
  Deposits: other reports read that one by name.
* **PREORDER-DEPOSIT** — the payment line. Non-stock, Service group (never a firearm group),
  booked to Pre-order Deposits, with the company's 0% "Non-Taxable Service" template on the item
  itself (it zeroes every account of the company's default sales-tax template, excise included).
  A locked line (``setup/locked_lines``): its amount comes from the pre-order, never a price.

Lifecycle like setup/gunsmith: lazy at first use + after_migrate, never after_install, and the
migrate path never raises.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.utils import commit

PREORDER_ITEM = "PREORDER-DEPOSIT"
ACCOUNT_TITLE = "Pre-order Deposits"
LINK_FIELD = "ffl_preorder"
FLAG_FIELD = "ffl_is_preorder"
AVAILABLE_FIELD = "ffl_preorder_available"
ETA_FIELD = "ffl_preorder_eta"

_LINK = {"fieldname": LINK_FIELD, "fieldtype": "Data", "label": "Pre-order", "read_only": 1, "no_copy": 0}
# Hidden until hand-over of an arrived pre-order is built: while every Item's count is 0 nothing can
# be pre-ordered, so the counter offers no Pre-order line and the feature stays off.
_ITEM_HIDDEN = 1
FIELDS = {
	"Item": [
		{"fieldname": AVAILABLE_FIELD, "fieldtype": "Int", "label": "Pre-order Available", "default": "0",
			"non_negative": 1, "hidden": _ITEM_HIDDEN, "insert_after": "sells_through_item"},
		{"fieldname": ETA_FIELD, "fieldtype": "Date", "label": "Pre-order ETA", "hidden": _ITEM_HIDDEN,
			"insert_after": AVAILABLE_FIELD},
	],
	"Sales Order": [
		{"fieldname": FLAG_FIELD, "fieldtype": "Check", "label": "Pre-order", "read_only": 1, "no_copy": 1,
			"in_standard_filter": 1, "insert_after": "order_type"},
	],
	"POS Invoice": [{**_LINK, "insert_after": "ffl_work_order", "in_standard_filter": 1}],
	"POS Invoice Item": [{**_LINK, "insert_after": "ffl_work_order"}],
	"Sales Invoice Item": [{**_LINK, "insert_after": "ffl_work_order"}],
}


def preorder_account(company: str) -> str | None:
	"""The company's Pre-order Deposits liability account, if created."""
	from ffl_core.setup.customer_deposits import deposit_account
	return deposit_account(company, ACCOUNT_TITLE)


def ensure() -> dict:
	"""Fields, accounts, the payment item and its defaults, the price tombstones. Idempotent; never raises."""
	out = {"accounts": 0, "items": 0, "defaults": 0}
	try:
		from ffl_core.setup import customer_deposits, gunsmith, locked_lines

		gunsmith.ensure()   # the ffl_work_order fields the links above sit after, and the Service group's 0% template
		create_custom_fields({dt: [{**f, "module": "FFL Core"} for f in rows] for dt, rows in FIELDS.items()},
			ignore_validate=True)
		for company in frappe.get_all("Company", pluck="name"):
			out["accounts"] += int(bool(customer_deposits._ensure_account(company, ACCOUNT_TITLE)))
		out["items"] += gunsmith._ensure_item(PREORDER_ITEM, "Pre-order Payment",
			"Money paid towards a pre-order, held for the customer until the goods are handed over (a liability, "
			"not a sale). Locked to its pre-order; added only from the pre-order.")
		out["defaults"] = gunsmith._ensure_deposit_defaults(PREORDER_ITEM, ACCOUNT_TITLE)
		locked_lines.ensure()
		commit()
	except Exception:
		frappe.log_error(title="preorder setup failed", message=frappe.get_traceback())
		out["error"] = 1
	return out
