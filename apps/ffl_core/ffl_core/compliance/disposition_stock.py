"""Stock-side companion to FFL Disposition.

FFL Disposition is the bound-book 'D' record — submitting one says "this firearm
left our FFL". The bound book (FastBound) sync is wired separately via
ffl_integrations.fastbound.tasks. This module covers the ERPNext-stock side:
when a non-Retail-Sale disposition submits, we Material-Issue the firearm out
of inventory so ERPNext, the Serial No state, the Item form, and the POS
picker all immediately reflect that the gun is gone.

Why this didn't auto-happen before:
  - FFL Disposition.on_submit only enqueued the FastBound push; nothing
    touched ERPNext stock.
  - Operators had to run "Sync Bound Book" (inventory_sync.py) to reconcile —
    a periodic recovery sweep, not a per-disposition write.
  - Result: a firearm could be in FastBound's bound book as disposed AND still
    Active in ERPNext, visible to POS and to other disposition pickers.

Retail Sale path is intentionally skipped — its stock is decremented by the
POS Invoice / Sales Invoice submit (ERPNext's standard sale flow), and the
disposition is created by the FastBound 4473 webhook AFTER stock already
moved. Issuing again would double-decrement.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import issue_serial


# Disposition types that 'move stock out' on submit. Anything not in this set
# (Retail Sale, anything FastBound-webhook-imported) gets its stock movement
# from elsewhere (POS / Sales Invoice).
_STOCK_ISSUING_TYPES = frozenset({
	"FFL Transfer", "Dealer Transfer", "Return",
	"Scrap", "Destroyed",
	"Gunsmithing", "NFA",
})


def issue_stock_on_submit(doc, method: str | None = None) -> None:
	"""FFL Disposition on_submit — create a Material Issue Stock Entry to take
	the firearm out of ERPNext inventory.

	Idempotent: skipped when this disposition already has a stock_entry link,
	when the serial isn't on hand (no warehouse / non-Active status), or when
	the disposition is a Retail Sale (POS handles stock).
	"""
	if not _should_issue_stock(doc):
		return
	serial = (doc.serial or "").strip()
	if not serial:
		return  # validate() should have caught this; defence in depth.

	sn = frappe.db.get_value(
		"Serial No", serial,
		["item_code", "warehouse", "status"],
		as_dict=True,
	)
	if not sn:
		# A submitted disposition for a serial we don't have a Serial No row
		# for — webhook-imported Retail Sale that bypassed our intake, or
		# a manually-entered serial pointing at a missing record. Skip; the
		# upstream validator (_require_serial_belongs_to_item) catches typos.
		return
	if sn.status != "Active" or not sn.warehouse:
		# Already out of stock (sold, transferred elsewhere, etc.). Nothing to do.
		return

	se_name = issue_serial(
		sn.item_code, serial, sn.warehouse,
		remarks=f"Auto-issued by FFL Disposition {doc.name} ({doc.disposition_type}).")
	frappe.db.set_value(
		"FFL Disposition", doc.name, "stock_entry", se_name,
		update_modified=False,
	)


def restore_stock_on_cancel(doc, method: str | None = None) -> None:
	"""FFL Disposition on_cancel — cancel the Material Issue we created so the
	firearm returns to ERPNext inventory. Symmetric with issue_stock_on_submit;
	does nothing if no stock_entry was linked (Retail Sale path, or a
	disposition that submitted before this hook existed), or when a return
	receipt already brought the gun back (return_stock_entry)."""
	se_name = (doc.stock_entry or "").strip() if hasattr(doc, "stock_entry") else ""
	if not se_name:
		return
	if (doc.get("return_stock_entry") or "").strip():
		# Return in transit (api/return_in_transit) already brought the gun back on its
		# own receipt; the issue stays in the month it was posted.
		return
	if not frappe.db.exists("Stock Entry", se_name):
		return
	docstatus = frappe.db.get_value("Stock Entry", se_name, "docstatus")
	if docstatus != 1:
		# Already cancelled (docstatus 2) or still draft (0) — nothing to undo.
		return
	se = frappe.get_doc("Stock Entry", se_name)
	se.flags.ignore_permissions = True
	se.cancel()
	frappe.db.set_value(
		"FFL Disposition", doc.name, "stock_entry", "",
		update_modified=False,
	)


def _should_issue_stock(doc) -> bool:
	"""Per-doc gate: only auto-issue for types whose stock movement isn't
	handled elsewhere, and only when no Stock Entry is already linked."""
	# An extension that moved this gun's stock itself says so here (hooks
	# gunshop_disposition_stock_handled, docs/extensions/hooks.md) — an
	# outbound consignment, say, whose Material Transfer into the dealer's
	# warehouse ran BEFORE the disposition submits: auto-issuing on top would
	# Material-Issue the gun straight out of that warehouse and the asset
	# would vanish from the books. Fail closed: a provider that cannot answer
	# is treated as "handled", because a second stock move is the worse error.
	for hook in frappe.get_hooks("gunshop_disposition_stock_handled"):
		try:
			handled = bool(frappe.get_attr(hook)(doc))
		except Exception:
			frappe.log_error(title=f"gunshop_disposition_stock_handled failed: {hook}")
			return False
		if handled:
			return False
	if doc.disposition_type not in _STOCK_ISSUING_TYPES:
		return False
	if hasattr(doc, "stock_entry") and (doc.stock_entry or "").strip():
		return False
	return True
