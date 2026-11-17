"""Insert price on a CRM Lead or Deal: one line, "{product} — ${price}", at the cursor of CRM's composer.

No AI. The price is the register's today (``pricing.counter_price_for``), worked out when the line is
inserted, never taken from the list the employee picked from. The candidates come most relevant first:
the guns on the record's product lines, the other products on them, other guns of those models for sale,
then what the record's open wants fit in stock (``crm.reply_draft``, the want list's own rule). A serial
number never goes into the text; the list shows its last four characters to tell two guns apart. Cost is
never returned.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

from ffl_core import pricing
from ffl_core.crm import reply_draft

OPTION_LIMIT = 15
OTHER_UNITS = 3
NO_PRICE = "No price for this item."


def _money(amount: float) -> str:
	return "$" + fmt_money(amount, precision=2)


def _item(item_code: str):
	return frappe.db.get_value("Item", item_code, ["name", "item_name", "is_antique", "has_serial_no",
		"uniform_sell_price", "disabled"], as_dict=True)


def _per_gun(item) -> bool:
	return bool(item.has_serial_no and not item.uniform_sell_price)


def _price(item_code: str, serial_no: str | None) -> float:
	try:
		return flt(pricing.counter_price_for(item_code, serial_no))
	except frappe.ValidationError:
		return 0.0


def options(doctype: str, name: str) -> dict:
	"""{"options": [{"item_code", "serial_no", "label"}], "default": 0}, most relevant first."""
	doc = reply_draft.record(doctype, name, "read")
	out: list[dict] = []
	seen: set[tuple] = set()

	def add(item, serial: str | None, price: float) -> None:
		key = (item.name, serial)
		if key in seen or len(out) >= OPTION_LIMIT or price <= 0 or item.is_antique or item.disabled:
			return
		seen.add(key)
		money = _money(price)
		if serial:
			label = f"{item.item_name or item.name} · {serial[-4:]} — {money}"
		elif _per_gun(item):
			label = f"{item.item_name or item.name} — " + _("from {0}").format(money)
		else:
			label = f"{item.item_name or item.name} — {money}"
		out.append({"item_code": item.name, "serial_no": serial, "label": label})

	lines = reply_draft.record_lines(doc)
	items = {code: _item(code) for code, _serial in lines}
	for code, serial in lines:   # 1. the guns picked on the lines
		if serial and items[code]:
			add(items[code], serial, _price(code, serial))
	for code, serial in lines:   # 2. the other products on the lines
		if not serial and items[code]:
			add(items[code], None, _price(code, None))
	for code in dict.fromkeys(code for code, _serial in lines):   # 3. other guns of those models
		item = items[code]
		if item and _per_gun(item) and not item.is_antique:
			for unit in reply_draft.units(code, OTHER_UNITS):
				add(item, unit["serial"], flt(unit["sell_price"]))
	for code in reply_draft.matching_items(reply_draft.record_wants(doc)):   # 4. what the wants fit
		item = _item(code)
		if item:
			add(item, None, _price(code, None))
	return {"options": out, "default": 0}


def text(doctype: str, name: str, item_code: str, serial_no: str | None = None) -> dict:
	"""{"text": "{product} — ${price}"} at the register's price now."""
	reply_draft.record(doctype, name, "read")
	item = isinstance(item_code, str) and _item(item_code)
	if not item:
		frappe.throw(_("This item no longer exists."), frappe.DoesNotExistError)
	serial_no = serial_no or None
	price = flt(pricing.counter_price_for(item.name, serial_no))   # refuses a gun of another model or sold
	if price <= 0:
		frappe.throw(_(NO_PRICE), title=_("No price"))
	money = _money(price)
	if _per_gun(item) and not serial_no:
		money = _("from {0}").format(money)
	return {"text": f"{item.item_name or item.name} — {money}"}
