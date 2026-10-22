"""Draft order status for the POS Recent Orders panel.

ERPNext's past-order list (point_of_sale.get_past_order_list) carries only the
name, customer, total and date of each order. The counter also needs to see, on
every parked draft, how much money is on it and whether its 4473 is done —
without opening each one. This endpoint returns those facts for a page of drafts
in three local queries; the browser turns them into labels
(public/js/pos_order_status.js, through pos_tender_guard.js's form4473State and
draftPaymentSummary, which the cart badge shares). Nothing here calls Payroc or
FastBound.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import cint, flt

from ffl_core.firearm import ALL_FIREARM_GROUPS, firearm_item_sql

MAX_INVOICES = 100


@frappe.whitelist(methods=["GET"])
def draft_order_status(invoices: list[str] | str) -> dict:
	"""Payment and 4473 facts for a page of draft POS Invoices.

	Args:
	    invoices: POS Invoice names — a list, a JSON-encoded list, or one name.
	        Duplicates are ignored; at most 100 names are read.

	Returns:
	    {name: {"due", "currency", "paid_amount", "charged", "card_status",
	    "held", "paid_online", "payments": [{"mode_of_payment", "amount"}],
	    "counter_order": {"name", "total", "outstanding"} | None,
	    "has_firearm", "form_4473_status", "nics_status"}} for each name that is a draft,
	    non-return POS Invoice the caller can read. Any other name is left out.
	    counter_order is the money side of a counter pickup's 4473 ticket — the
	    Sales Invoice it hands over for (the ticket itself carries $0).
	"""
	# A read, so gate on POS Invoice READ rather than a role list: the POS page is
	# also open to Accounts roles, and frappe.only_for would put a "Not permitted"
	# dialog in front of them on every Recent Orders refresh.
	frappe.has_permission("POS Invoice", "read", throw=True)
	names = _names(invoices)
	if not names:
		return {}
	# The web-order link is ffl_woo_sync's Custom Field; a site without that app
	# (or mid-migrate) has no such column, and one unknown column would take the
	# whole panel's status with it. Guarded the way sales_report guards its
	# channel columns; paid_online then reads False.
	has_web_link = frappe.db.has_column("POS Invoice", "woo_online_order")
	has_counter_link = frappe.db.has_column("POS Invoice", "ffl_counter_order")
	rows = frappe.get_list(
		"POS Invoice",
		filters={"name": ("in", names), "docstatus": 0, "is_return": 0},
		fields=["name", "grand_total", "rounded_total", "paid_amount", "currency",
			"form_4473_status", "nics_status", "card_status", "card_charged_amount",
			"ffl_payment_held"] + (["woo_online_order"] if has_web_link else [])
			+ (["ffl_counter_order"] if has_counter_link else []),
		limit=MAX_INVOICES,
	)
	if not rows:
		return {}
	found = [r.name for r in rows]
	# A counter pickup ticket's money is on its order: read the Sales Invoice's
	# total and balance so the chip can say Paid / Deposit / Unpaid for it.
	counter: dict[str, dict] = {}
	links = {r.get("ffl_counter_order") for r in rows if r.get("ffl_counter_order")}
	if links:
		for si in frappe.get_all("Sales Invoice", filters={"name": ("in", list(links))},
				fields=["name", "grand_total", "rounded_total", "outstanding_amount"]):
			counter[si.name] = {"name": si.name,
				"total": flt(si.rounded_total) or flt(si.grand_total),
				"outstanding": flt(si.outstanding_amount)}
	payments: dict[str, list[dict]] = {}
	for p in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parenttype": "POS Invoice", "parent": ("in", found), "amount": ("!=", 0)},
			fields=["parent", "mode_of_payment", "amount"],
			order_by="idx asc"):
		payments.setdefault(p.parent, []).append(
			{"mode_of_payment": p.mode_of_payment, "amount": flt(p.amount)})
	with_firearm = _drafts_with_regulated_firearm(found)
	return {
		r.name: {
			"due": flt(r.rounded_total) or flt(r.grand_total),
			"currency": r.currency,
			"paid_amount": flt(r.paid_amount),
			"charged": flt(r.card_charged_amount),
			"card_status": r.card_status or "",
			"held": cint(r.ffl_payment_held),
			# A web order's pickup ticket (ffl_woo_sync.woocommerce.pickup): the
			# buyer already paid on WooCommerce and the ticket carries that tender
			# as its payment row, so the counter must read it as paid — not as a
			# typed, unconfirmed amount.
			"paid_online": bool((r.get("woo_online_order") or "").strip()),
			"counter_order": counter.get(r.get("ffl_counter_order")),
			"payments": payments.get(r.name, []),
			"has_firearm": r.name in with_firearm,
			"form_4473_status": r.form_4473_status or "N/A",
			"nics_status": r.nics_status or "N/A",
		}
		for r in rows
	}


def _names(invoices) -> list[str]:
	"""The request's names: JSON list, list, or a bare name; de-duplicated, capped."""
	if isinstance(invoices, str):
		try:
			invoices = json.loads(invoices)
		except ValueError:
			invoices = [invoices]
	if isinstance(invoices, str):
		invoices = [invoices]
	if not isinstance(invoices, (list, tuple)):
		return []
	return list(dict.fromkeys(n for n in invoices if isinstance(n, str) and n))[:MAX_INVOICES]


def _drafts_with_regulated_firearm(names: list[str]) -> set[str]:
	"""The names among `names` carrying at least one regulated firearm line — the
	same predicate as ffl_core.firearm.firearm_items_in (antiques need no 4473) —
	in one query."""
	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT pii.parent
		FROM `tabPOS Invoice Item` pii
		JOIN `tabItem` it ON it.name = pii.item_code
		WHERE pii.parenttype = 'POS Invoice'
		  AND pii.parent IN %(names)s
		  AND {firearm_item_sql("it")}
		  AND COALESCE(it.is_antique, 0) = 0
		""",
		{"names": tuple(names), "firearm_groups": tuple(ALL_FIREARM_GROUPS)},
	)
	return {r[0] for r in rows}
