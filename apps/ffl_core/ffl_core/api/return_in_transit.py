"""Return in transit: a shipped firearm the carrier brought back.

The gun never reached the receiving dealer, so the bound book must read as if it
never left: the FFL Disposition is cancelled, and its own on_cancel hook in
ffl_integrations deletes the FastBound disposition and undisposes the item.

The stock side does NOT cancel the disposition's Material Issue (what a plain
Desk cancel does, disposition_stock.restore_stock_on_cancel). Cancelling it would
rewrite the shipping month after its books may have been closed and handed over,
and ERPNext refuses it outright once that period is frozen. Instead the gun comes
back on a Material Receipt dated today that mirrors the issue: the same item,
serial and warehouse, the same value, the same offset (difference) account and
cost center, and remarks that name the disposition and the issue. The month-end
COGS figure nets the Stock Adjustment lines whose remarks carry "FFL Disposition",
so the receipt cancels the issue's cost in the month the gun came back. The
receipt is stamped on the disposition (return_stock_entry), which is how
restore_stock_on_cancel knows the stock is already back.

The sale side, per what the disposition shipped:

* nothing (a Desk transfer, a return to a supplier): the gun is free stock again;
* a counter order (Sales Invoice, update_stock=0): the invoice is untouched, its
  line still names the gun, so the order is back on the Pending Order queue with
  the gun held for it; staff Dispose again to re-ship, or Cancel Order to refund.
  Its shipping stamp is cleared (shipstation_order_id / shipstation_status back to
  not requested) so Ship or Mark Shipped works again; a ShipStation order it was
  linked to is only unlinked, named in a comment on the invoice, and a new push
  creates a new one;
* a sale that moved the stock itself (Sales Invoice with Update Stock, Delivery
  Note): refused; bringing that gun back is a return of the sale (credit note);
* a web or GunBroker order: refused; reopening a Transferred channel order whose
  revenue is booked is not built.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.firearm import serialized_receipt_row
from ffl_core.roles import RETURN_IN_TRANSIT_ROLES
from ffl_core.serials import serials_for_line

# What a carrier can bring back: a gun shipped to another licensee. Destruction and
# 4473 sales never travel.
SHIPPED_TYPES = frozenset({"FFL Transfer", "Dealer Transfer", "Return"})

# Channel orders whose sale side this action cannot put back (see the module docstring).
_CHANNEL_LINKS = (
	("woo_online_order", "web order"),
	("gunbroker_order", "GunBroker order"),
)


@frappe.whitelist(methods=["POST"])
def return_in_transit(disposition: str) -> dict:
	"""Undo a shipped transfer the carrier returned: cancel the disposition, receive the gun back.

	The FFL Disposition is cancelled (its on_cancel deletes the FastBound record and
	undisposes the item) and a Material Receipt dated today brings the gun back into
	the warehouse it left, at the value and offset account of the disposition's
	Material Issue, with remarks naming both. The issue itself stays submitted.

	Args:
	    disposition: Submitted FFL Disposition (FFL Transfer, Dealer Transfer or
	        Return) whose own Material Issue took the gun out of stock.

	Returns:
	    {"ok": True, "disposition", "serial", "stock_entry" (the receipt),
	    "issue" (the original Material Issue), "fastbound": "undispose_queued" |
	    "not_pushed" | "disabled", "sale": {"doctype", "name"} | None, "next_step": str | None}.
	    next_step says what the linked sale still needs (a counter order is back on
	    the Pending Order queue).

	Raises:
	    frappe.ValidationError: Not a submitted shipped transfer; the gun left on the
	        sale's own invoice or delivery note (book a credit note instead); a web or
	        GunBroker order; the gun is already back in stock; or the bound book has a
	        later entry for the serial.

	Permissions:
	    Managers only.
	"""
	frappe.only_for(RETURN_IN_TRANSIT_ROLES)
	# Row lock: a double-click must not post two receipts.
	if not frappe.db.get_value("FFL Disposition", disposition, "name", for_update=True):
		frappe.throw(_("FFL Disposition {0} not found.").format(disposition))
	doc = frappe.get_doc("FFL Disposition", disposition)
	issue = _check_returnable(doc)

	receipt = _receive_back(doc, issue)
	frappe.db.set_value("FFL Disposition", doc.name, "return_stock_entry", receipt,
		update_modified=False)
	doc.reload()
	doc.flags.ignore_permissions = True
	doc.cancel()
	esc = frappe.utils.escape_html
	doc.add_comment("Comment", _("Returned in transit by {0}: disposition cancelled, {1} "
		"received back on {2} (reverses {3}).").format(
		esc(frappe.session.user), esc(doc.serial), esc(receipt), esc(issue.name)))

	sale, next_step = _sale_side(doc)
	return {
		"ok": True,
		"disposition": doc.name,
		"serial": doc.serial,
		"stock_entry": receipt,
		"issue": issue.name,
		"fastbound": _fastbound_outcome(doc),
		"sale": sale,
		"next_step": next_step,
	}


def _check_returnable(doc):
	"""Every refusal, before anything is written. Returns the Material Issue."""
	if doc.docstatus != 1:
		frappe.throw(_("{0} is not a submitted disposition.").format(doc.name))
	if doc.disposition_type not in SHIPPED_TYPES:
		frappe.throw(_("{0} is a {1} disposition. Return in transit is for a gun shipped "
			"to another licensee (FFL Transfer, Dealer Transfer or Return).").format(
			doc.name, doc.disposition_type), title=_("Not a Shipment"))
	for field, label in _CHANNEL_LINKS:
		link = (doc.get(field) or "").strip()
		if link:
			frappe.throw(_("{0} shipped {1} {2}. Return in transit cannot reopen a web or "
				"GunBroker order yet: the order and its revenue would still claim the sale. "
				"Leave the disposition as it is and set the gun aside for the office.").format(
				doc.name, label, link), title=_("Channel Order"))

	se_name = (doc.get("stock_entry") or "").strip()
	if not se_name:
		frappe.throw(_no_issue_message(doc), title=_("Return the Sale Instead"))
	issue = frappe.get_doc("Stock Entry", se_name)
	if issue.docstatus != 1 or issue.stock_entry_type != "Material Issue":
		frappe.throw(_("{0}'s stock entry {1} is not a submitted Material Issue, so there "
			"is nothing to reverse.").format(doc.name, se_name))

	later = _later_book_entry(doc)
	if later:
		frappe.throw(_("{0} {1} records {2} after this disposition. The bound book has "
			"moved on, so this shipment cannot be undone: a gun the dealer received and "
			"sent back is a new acquisition from that dealer.").format(
			_(later[0]), later[1], doc.serial), title=_("Later Bound-Book Entry"))

	sn = frappe.db.get_value("Serial No", doc.serial, ["status", "warehouse"], as_dict=True)
	if sn and sn.status == "Active" and sn.warehouse:
		frappe.throw(_("{0} is already back in stock ({1}). If a manual receipt brought it "
			"back, cancel that receipt first, then run Return in transit again.").format(
			doc.serial, sn.warehouse), title=_("Already in Stock"))
	return issue


def _no_issue_message(doc) -> str:
	dn = (doc.get("delivery_note") or "").strip()
	if dn:
		return _("{0} left stock on Delivery Note {1}, not on a Material Issue of this "
			"disposition. Book the return on {1} (a Delivery Note return) to bring the gun "
			"back, then cancel this disposition.").format(doc.serial, dn)
	si = (doc.get("sales_invoice") or "").strip()
	if si:
		return _("{0} left stock on Sales Invoice {1}, not on a Material Issue of this "
			"disposition. Bringing it back is a return of that sale: book a credit note "
			"(Update Stock) on {1}, then cancel this disposition.").format(doc.serial, si)
	return _("{0} did not take {1} out of stock itself, so there is no stock issue to "
		"reverse.").format(doc.name, doc.serial)


def _later_book_entry(doc) -> tuple[str, str] | None:
	"""A submitted acquisition or disposition of the same gun booked after this one."""
	for doctype in ("FFL Acquisition", "FFL Disposition"):
		name = frappe.db.get_value(doctype, {
			"serial": doc.serial, "docstatus": 1, "name": ("!=", doc.name),
			"creation": (">", doc.creation),
		}, "name")
		if name:
			return doctype, name
	return None


def _receive_back(doc, issue) -> str:
	"""Material Receipt mirroring the issue's row for this serial; returns its name."""
	rows = [r for r in issue.items if doc.serial in serials_for_line(r)]
	if len(rows) != 1 or not rows[0].s_warehouse:
		frappe.throw(_("{0} has no single row issuing {1} to reverse.").format(
			issue.name, doc.serial))
	row = rows[0]
	value = -flt(frappe.db.get_value("Stock Ledger Entry", {
		"voucher_type": "Stock Entry", "voucher_no": issue.name,
		"voucher_detail_no": row.name, "is_cancelled": 0,
	}, "stock_value_difference"))
	receipt_row = serialized_receipt_row(item_code=doc.item, qty=1,
		warehouse=row.s_warehouse, basic_rate=value, serial=doc.serial)
	receipt_row.update({
		"expense_account": row.expense_account,
		"cost_center": row.cost_center,
		# A custody gun is held at zero cost by design (issue_serial issues it so).
		"allow_zero_valuation_rate": 1 if not value else 0,
	})
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": issue.company,
		"remarks": _("Return in transit: reverses FFL Disposition {0} ({1}), Material "
			"Issue {2}.").format(doc.name, doc.disposition_type, issue.name),
		"items": [receipt_row],
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _fastbound_outcome(doc) -> str:
	"""What the disposition's on_cancel did on FastBound: the same switch its delete hook
	(ffl_integrations.fastbound.tasks.cancel_disposition_on_cancel) checks."""
	try:
		enabled = frappe.get_attr("ffl_integrations.integration_gate.fastbound_enabled")()
	except (ImportError, AttributeError):
		enabled = False  # ffl_integrations not installed: there is no FastBound
	if not enabled:
		return "disabled"
	return "undispose_queued" if (doc.fastbound_id or "").strip() else "not_pushed"


def _reset_shipping(si: str) -> None:
	"""The gun is back, so the order is not shipped: clear the stamp shipstation_pushed()
	reads, or Mark Shipped answers "already" and the Ship push skips. ShipStation is not
	called; a linked order there is only unlinked and named on the invoice."""
	from ffl_core.api.manual_order import shipstation_pushed

	if not frappe.db.has_column("Sales Invoice", "shipstation_status"):
		return
	cur = frappe.db.get_value("Sales Invoice", si,
		["shipstation_order_id", "shipstation_status"], as_dict=True, for_update=True)
	if not cur or not shipstation_pushed(cur.shipstation_order_id, cur.shipstation_status):
		return
	frappe.db.set_value("Sales Invoice", si,
		{"shipstation_order_id": "", "shipstation_status": "Not Requested"},
		update_modified=False)
	esc = frappe.utils.escape_html
	old = (cur.shipstation_order_id or "").strip()
	frappe.get_doc("Sales Invoice", si).add_comment("Comment", _(
		"Firearm returned in transit: shipping reset (was {0}{1}). Ship again for a new "
		"shipment.").format(esc(cur.shipstation_status or ""),
		_(", ShipStation shipment {0}").format(esc(old)) if old else ""))


def _sale_side(doc) -> tuple[dict | None, str | None]:
	si = (doc.get("sales_invoice") or "").strip()
	if not si:
		return None, None
	_reset_shipping(si)
	return {"doctype": "Sales Invoice", "name": si}, _(
		"{0} is back on the Pending Order queue with {1}: Dispose to ship it again, or "
		"Cancel Order to refund the buyer.").format(si, doc.serial)
