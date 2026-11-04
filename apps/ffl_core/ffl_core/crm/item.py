"""Item → CRM Product: every Item is a CRM Product, priced as the register charges it.

CRM's own Item hooks (``crm.integrations.erpnext.item``) would price a product from the Item
Price of the selling list, a per-gun model included; they skip a save whose
``doc.flags.ignore_crm_sync`` is set, which :func:`skip_crm_sync` sets on every Item save. CRM's
Item ``on_trash`` and rename cascade stay theirs: deleting or renaming an Item deletes or renames
its product, the right direction.

Prices also change where no Item hook runs (Receive Goods, the daily sale refresh, the drop-ship
sync, all ``db.set_value``): :func:`refresh_product_prices` brings the products back daily.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core import pricing
from ffl_core.crm import TITLE, installed

PRODUCT = "CRM Product"
# Our own writes to CRM Product: crm.product lets them through and pushes nothing back.
SYNC_FLAG = "ffl_crm_sync"
DETAIL_FIELDS = {"product_name": "item_name", "description": "description", "image": "image", "disabled": "disabled"}
_ITEM_FIELDS = ["name", "item_name", "description", "image", "disabled", "crm_product_code"]
_SAVEPOINT = "ffl_crm_item_sync"
BATCH = 500


def skip_crm_sync(doc, method=None):
	"""Item before_insert / before_save: CRM's own Item → Product hooks skip this save."""
	doc.flags.ignore_crm_sync = True


def sync_product(doc, method=None):
	"""Item on_update (which runs on insert too): upsert its CRM Product. Never breaks the Item save: a
	failure is rolled back to before it and logged."""
	if not installed():
		return
	frappe.db.savepoint(_SAVEPOINT)
	try:
		upsert_product(doc)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.log_error(title=f"[{TITLE}] Item to CRM product failed: {doc.name}", message=frappe.get_traceback())


def _values(item) -> dict:
	return {
		"product_name": item.get("item_name") or item.get("name"),
		"description": item.get("description"),
		"image": item.get("image"),
		"disabled": 1 if item.get("disabled") else 0,
		"standard_rate": pricing.counter_list_price(item.get("name")),
	}


def _linked_product(item) -> str | None:
	name = frappe.db.get_value(PRODUCT, {"erpnext_item_code": item.get("name")}, "name")
	if name:
		return name
	# Made in CRM before this sync, or linked from this side only: adopt it when it is free.
	for candidate in (item.get("crm_product_code"), item.get("name")):
		if candidate and frappe.db.exists(PRODUCT, candidate) \
				and frappe.db.get_value(PRODUCT, candidate, "erpnext_item_code") in (None, "", item.get("name")):
			return candidate
	return None


def upsert_product(item) -> str:
	"""Create or update *item*'s CRM Product (``item``: an Item document or a row carrying
	``_ITEM_FIELDS``) and link both sides. Writes only what differs. Returns the product name."""
	values = _values(item)
	name = _linked_product(item)
	if name:
		current = frappe.db.get_value(PRODUCT, name, list(values), as_dict=True) or {}
		changed = {k: v for k, v in values.items() if not _same(k, v, current.get(k))}
		if changed:
			frappe.db.set_value(PRODUCT, name, changed)
	else:
		product = frappe.get_doc({"doctype": PRODUCT, "product_code": item.get("name"), **values})
		product.flags[SYNC_FLAG] = True
		product.flags.ignore_erpnext_sync = True   # CRM's own controller: not a CRM-side creation
		# CRM Product is CRM's DocType; the sync runs as whoever saved the Item (a stock clerk
		# receiving goods), who need not hold CRM rights. CRM's own Item hook inserts the same way.
		product.insert(ignore_permissions=True)
		name = product.name
	if frappe.db.get_value(PRODUCT, name, "erpnext_item_code") != item.get("name"):
		frappe.db.set_value(PRODUCT, name, "erpnext_item_code", item.get("name"), update_modified=False)
	if frappe.db.get_value("Item", item.get("name"), "crm_product_code") != name:
		frappe.db.set_value("Item", item.get("name"), "crm_product_code", name, update_modified=False)
	return name


def _same(field: str, new, old) -> bool:
	if field in ("standard_rate", "disabled"):
		return flt(new, 2) == flt(old, 2)
	return (new or None) == (old or None)


def backfill_products() -> dict | None:
	"""Long queue, once per site (``setup.enqueue_backfill``): every Item gets its CRM Product.
	Idempotent; an Item that fails is logged and the rest go on."""
	from ffl_core.crm.setup import BACKFILL_FLAG

	if not installed():
		return None
	done = failed = 0
	names = frappe.get_all("Item", pluck="name", order_by="name", limit_page_length=0)
	for start in range(0, len(names), BATCH):
		for row in frappe.get_all("Item", filters={"name": ["in", names[start:start + BATCH]]}, fields=_ITEM_FIELDS):
			frappe.db.savepoint(_SAVEPOINT)
			try:
				upsert_product(row)
				done += 1
			except Exception:
				frappe.db.rollback(save_point=_SAVEPOINT)
				failed += 1
				frappe.log_error(title=f"[{TITLE}] Backfill failed: {row.name}", message=frappe.get_traceback())
	frappe.db.set_global(BACKFILL_FLAG, "1")
	return {"products": done, "failed": failed}


def refresh_product_prices() -> dict | None:
	"""Daily: every linked CRM Product's price back to :func:`pricing.counter_list_price`, only
	where it differs."""
	if not installed():
		return None
	changed = 0
	rows = frappe.get_all(PRODUCT, filters={"erpnext_item_code": ["is", "set"]},
		fields=["name", "erpnext_item_code", "standard_rate"], order_by="name", limit_page_length=0)
	# lazy: one price lookup per product (~2,000 in seconds); batch the Item Price reads if a store
	# grows past tens of thousands of items.
	for row in rows:
		try:
			price = pricing.counter_list_price(row.erpnext_item_code)
		except Exception:
			frappe.log_error(title=f"[{TITLE}] Price refresh failed: {row.name}", message=frappe.get_traceback())
			continue
		if not _same("standard_rate", price, row.standard_rate):
			# modified moves, so a CRM form opened before this refresh is refused on save
			# instead of sending its old price back as a manager's change.
			frappe.db.set_value(PRODUCT, row.name, "standard_rate", price)
			changed += 1
	return {"checked": len(rows), "changed": changed}
