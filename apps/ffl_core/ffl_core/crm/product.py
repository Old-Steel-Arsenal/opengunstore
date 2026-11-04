"""CRM Product → Item: what a CRM user may change on a product that comes from an Item.

Products come from Items (``crm.item``); CRM's own Product → Item push is off
(``sync_products`` 0), so these hooks are the only way an edit made in CRM reaches the POS:

* a price change: a price approver only (``ffl_core.roles.PRICE_APPROVER_ROLES``), never for a
  read-only class (``pricing.price_readonly_reason``), written through
  ``pricing.set_regular_price`` and read back as the register's price;
* switching a product off: a price approver who may also edit Items;
* name, description, photo: anyone who may edit Items (the Item save checks it as well);
* the product code, renaming and deleting a linked product: never (the Item is the master).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core import pricing
from ffl_core.crm.item import DETAIL_FIELDS, PRODUCT, SYNC_FLAG
from ffl_core.roles import PRICE_APPROVER_ROLES


def _ours(doc) -> bool:
	return bool(doc.flags.get(SYNC_FLAG))


def _in_cascade() -> bool:
	"""CRM's own Item delete / rename cascade is moving this product: let it."""
	from crm.integrations.erpnext.utils import in_cascade

	return in_cascade()


def _is_approver() -> bool:
	return bool(set(frappe.get_roles()) & set(PRICE_APPROVER_ROLES))


def _differs(field: str, new, old) -> bool:
	if field == "disabled":
		return bool(new) != bool(old)
	return (new or None) != (old or None)


def _changes(doc, before) -> tuple[dict, bool]:
	"""(Item detail fields changed, whether the price changed) against the stored product."""
	details = {item_field: doc.get(field) for field, item_field in DETAIL_FIELDS.items()
		if _differs(field, doc.get(field), before.get(field))}
	price = flt(doc.get("standard_rate"), 2) != flt(before.get("standard_rate"), 2)
	return details, price


def before_insert(doc, method=None):
	"""Products are made from Items only."""
	if _ours(doc) or _in_cascade():
		return
	frappe.throw(_("Products come from the POS: add the item there and it appears here with its price."),
		title=_("Add the item in the POS"))


def validate(doc, method=None):
	if doc.is_new() or _ours(doc):
		return
	before = doc.get_doc_before_save()
	item = before and before.get("erpnext_item_code")
	if not item:
		return
	if doc.get("product_code") != before.get("product_code") or doc.get("erpnext_item_code") != item:
		frappe.throw(_("This product is a POS item; its code cannot change here."), title=_("Can't change the code"))
	details, price = _changes(doc, before)
	if price:
		if not _is_approver():
			frappe.throw(_("Only a manager can change a price."), frappe.PermissionError, title=_("Ask a manager"))
		reason = pricing.price_readonly_reason(item)
		if reason:
			frappe.throw(reason, title=_("Price can't be changed here"))
		if flt(doc.get("standard_rate")) <= 0:
			frappe.throw(_("Enter a price above zero."), title=_("Price can't be changed here"))
	if "disabled" in details and not _is_approver():
		frappe.throw(_("Only a manager can switch a product on or off."), frappe.PermissionError,
			title=_("Ask a manager"))
	if details and not frappe.has_permission("Item", "write", doc=item):
		frappe.throw(_("Changing a product's name, description, photo or on/off here changes the POS item, which you are not allowed to edit. Ask someone who edits items."),
			frappe.PermissionError, title=_("Not allowed"))


def on_update(doc, method=None):
	"""Push what a person changed to the Item: details by an Item save (CRM's own Item hooks
	skip it), the price by ``pricing.set_regular_price``; then show the register's price."""
	if _ours(doc):
		return
	before = doc.get_doc_before_save()
	item = before and before.get("erpnext_item_code")
	if not item or not frappe.db.exists("Item", item):
		return
	details, price = _changes(doc, before)
	if not (details or price):
		return
	if details:
		item_doc = frappe.get_doc("Item", item)
		item_doc.update(details)
		item_doc.flags.ignore_crm_sync = True
		item_doc.save()
	if price:
		pricing.set_regular_price(item, flt(doc.get("standard_rate")))
	# Always, even when the price reads back unchanged: the Item save above re-syncs this product
	# (db.set_value moves its modified), so the form must get the record's latest timestamp back or
	# its next save is refused. db_set moves modified on the record and on this doc alike, and an
	# older copy elsewhere is still refused instead of re-sending its price.
	doc.db_set("standard_rate", pricing.counter_list_price(item))


def before_rename(doc, method=None, olddn=None, newdn=None, merge=False):
	if doc.get("erpnext_item_code") and not _in_cascade():
		frappe.throw(_("This product is a POS item. Rename the item in the POS instead."), title=_("Rename in the POS"))


def on_trash(doc, method=None):
	if doc.get("erpnext_item_code") and not _in_cascade():
		frappe.throw(_("This product is a POS item. Disable or delete the item in the POS instead."),
			title=_("Delete in the POS"))
