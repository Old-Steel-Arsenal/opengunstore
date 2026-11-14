"""Marketing Request: someone waiting to hear about a product (back in stock) or a model (model alert).

A request's target is the first of its identifying fields that is set: for a back-in-stock request
the website product id, the SKU, the product URL, the product name; for a model alert the model key,
then its label. One person keeps one Open request per target.
"""
from __future__ import annotations

import frappe
from frappe.utils import get_datetime, now_datetime

from ffl_core.marketing import REQUEST

BACK_IN_STOCK, MODEL_ALERT = "Back in stock", "Model alert"
OPEN, NOTIFIED, CLOSED = "Open", "Notified", "Closed"

_TARGET_FIELDS = {
	BACK_IN_STOCK: ("web_product_id", "sku", "url", "label"),
	MODEL_ALERT: ("model_key", "label"),
}


def target(kind: str, fields: dict) -> tuple[str, str] | None:
	"""(field, value) that names what a request of *kind* waits for, or None when nothing does."""
	for field in _TARGET_FIELDS[kind]:
		if fields.get(field):
			return field, fields[field]
	return None


def item_for(sku: str = "", web_product_id: str = "") -> str | None:
	"""The POS Item a website product is: the SKU's item code (``item_code`` or ``item_code::serial``),
	else the Item or the Serial No listed as that website product."""
	code = (sku or "").partition("::")[0].strip()
	if code and frappe.db.exists("Item", code):
		return code
	if web_product_id:
		return (frappe.db.get_value("Item", {"woo_product_id": web_product_id}, "name")
			or frappe.db.get_value("Serial No", {"woo_product_id": web_product_id}, "item_code"))
	return None


def existing(contact: str, kind: str, fields: dict, *, status: str = OPEN, requested_at=None) -> str | None:
	"""The request this one would repeat: an Open one for the same target (when *status* is Open), or
	one for the same target made at the same time (an import run again)."""
	key = target(kind, fields)
	if not key:
		return None
	base = {"contact": contact, "kind": kind, key[0]: key[1]}
	if status == OPEN:
		name = frappe.db.get_value(REQUEST, {**base, "status": OPEN}, "name")
		if name:
			return name
	if requested_at:
		return frappe.db.get_value(REQUEST, {**base, "requested_at": get_datetime(requested_at)}, "name")
	return None


def add(contact: str, kind: str, *, sku: str = "", web_product_id: str = "", model_key: str = "", label: str = "",
		url: str = "", item: str | None = None, status: str = OPEN, requested_at=None, notified_at=None) -> tuple[str, bool]:
	"""(name, created): a request of *contact*'s, or the one it repeats (:func:`existing`)."""
	fields = {"sku": sku, "web_product_id": web_product_id, "model_key": model_key, "label": label, "url": url}
	when = requested_at or now_datetime()
	name = existing(contact, kind, fields, status=status, requested_at=when)
	if name:
		return name, False
	doc = frappe.get_doc({"doctype": REQUEST, "contact": contact, "kind": kind,
		"item": item or item_for(sku, web_product_id), "status": status, "requested_at": when,
		"notified_at": notified_at, **{k: v or None for k, v in fields.items()}})
	return doc.insert(ignore_permissions=True).name, True
