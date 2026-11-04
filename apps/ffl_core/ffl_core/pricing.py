"""What the register charges, for code that prices outside the sale screen (a quote, a CRM).

Part of the stable surface an extension may call (docs/extensions/hooks.md). Nothing here has a
pricing rule of its own: every number comes from the counter's rules, so a quote and the register
never disagree.

* :func:`counter_list_price` — an Item's list price today, the one
  ``price_approval.list_rate`` gives a line without a serial (the selling Item Price of the
  default price list, or a uniform-priced gun model's price). A gun priced one by one has no
  Item-level price: 0.0.
* :func:`counter_price_for` — a quote line's price: a gun priced one by one from its own serial
  (``firearm.serial_price_now``), or the cheapest one in stock while no serial is picked
  (``firearm.lowest_counter_price``); anything else :func:`counter_list_price`.
* :func:`price_readonly_reason` — why the regular price cannot be changed outside the POS (a
  per-gun model, a sale running today, a drop-ship listing, the transfer fee), or None.
* :func:`set_regular_price` — a price approver changes the regular price: ``Item.standard_rate``
  through an Item save (validate, the website / GunBroker pushes and the Version trail all run)
  and the selling Item Price with it (:func:`sync_selling_item_price`, the rule Receive Goods
  uses). ``item_hooks.sync_standard_selling_price`` never overwrites an existing Item Price on an
  Item that is not uniform-priced, so the save alone would leave the register on the old price.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from ffl_core.roles import PRICE_APPROVER_ROLES

_ITEM_FIELDS = ("has_serial_no", "uniform_sell_price", "stock_uom", "sale_price", "sale_from", "sale_to")


def _item(item_code: str | None):
	if not item_code:
		return None
	return frappe.db.get_value("Item", item_code, list(_ITEM_FIELDS), as_dict=True)


def _per_gun(item) -> bool:
	"""Priced serial by serial: the Item carries no price of its own (the rule
	``item_hooks.sync_standard_selling_price`` keeps its Item Price off by)."""
	return bool(item.get("has_serial_no") and not item.get("uniform_sell_price"))


def counter_list_price(item_code: str) -> float:
	"""The Item-level list price the register charges today.

	Args:
	    item_code: The Item.

	Returns:
	    ``price_approval.list_rate`` for a one-unit line without a serial on the default selling
	    price list; 0.0 for a gun priced one by one, an unknown Item, or one without a price.
	"""
	item = _item(item_code)
	if not item or _per_gun(item):
		return 0.0
	from ffl_core.item_hooks import _default_selling_price_list
	from ffl_core.price_approval import list_rate

	price_list = _default_selling_price_list()
	if not price_list:
		return 0.0
	doc = frappe._dict(selling_price_list=price_list, posting_date=nowdate())
	row = frappe._dict(item_code=item_code, qty=1, uom=item.stock_uom, stock_uom=item.stock_uom, conversion_factor=1)
	return flt(list_rate(doc, row)[0])


def counter_price_for(item_code: str, serial_no: str | None = None) -> float:
	"""A quote line's price, as the register would charge it today.

	Args:
	    item_code: The Item.
	    serial_no: The gun picked for the line, if any. Must be a serial of *item_code*.

	Returns:
	    For a gun priced one by one: that gun's price, or the cheapest priced one in stock while
	    no gun is picked (0.0 when none is). For anything else: :func:`counter_list_price`.
	"""
	item = _item(item_code)
	if not item or not _per_gun(item):
		return counter_list_price(item_code)
	from ffl_core.firearm import lowest_counter_price, serial_price_now

	if not serial_no:
		return flt(lowest_counter_price(item_code))
	if frappe.db.get_value("Serial No", serial_no, "item_code") != item_code:
		frappe.throw(_("Serial number {0} is not a gun of {1}. Pick one of its own serial numbers.").format(
			frappe.bold(serial_no), frappe.bold(item_code)), title=_("Wrong gun"))
	return flt(serial_price_now(serial_no))


def _transfer_fee_item() -> str:
	from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM

	return frappe.db.get_single_value("FFL Settings", "transfer_fee_item") or TRANSFER_FEE_ITEM


def price_readonly_reason(item_code: str) -> str | None:
	"""Why the regular price of *item_code* cannot be changed outside the POS, or None.

	Args:
	    item_code: The Item.

	Returns:
	    One sentence for staff saying where the price is set instead, or None when
	    :func:`set_regular_price` may change it.
	"""
	item = _item(item_code)
	if not item:
		return _("This item is no longer in the POS.")
	if item_code == _transfer_fee_item():
		return _("This is the transfer fee. Its amount is set in the POS settings, not on the item.")
	if _per_gun(item):
		return _("Each gun of this model has its own price. Change the price on the gun in the POS.")
	if frappe.get_meta("Item").has_field("dropship_distributor") \
			and frappe.db.get_value("Item", item_code, "dropship_distributor"):
		return _("A distributor ships this item, and its price follows the drop-ship pricing rules. Change those instead.")
	from ffl_core.firearm import sale_active

	if sale_active(item.sale_price, item.sale_from, item.sale_to):
		return _("This item is on sale today. Change its price after the sale ends, or change the sale in the POS.")
	return None


def set_regular_price(item_code: str, price: float) -> None:
	"""Change an Item's regular price, as a price approver.

	Saves the Item (its validate, the website / GunBroker pushes and the Version trail run) and
	makes the selling Item Price the register charges match.

	Args:
	    item_code: The Item.
	    price: The new regular price, above 0.

	Returns:
	    None. Raises ``frappe.PermissionError`` for a user who is not a price approver, and
	    ``frappe.ValidationError`` when :func:`price_readonly_reason` answers or the price is not
	    above 0.
	"""
	frappe.only_for(PRICE_APPROVER_ROLES)
	reason = price_readonly_reason(item_code)
	if reason:
		frappe.throw(reason, title=_("Price can't be changed here"))
	doc = frappe.get_doc("Item", item_code)
	price = flt(price, doc.precision("standard_rate"))
	if price <= 0:
		frappe.throw(_("Enter a price above zero."), title=_("Price can't be changed here"))
	if flt(doc.standard_rate, doc.precision("standard_rate")) != price:
		doc.standard_rate = price
		# The gate is the approver role above, not the Item DocPerms (a Sales Manager has no
		# write on Item); the save still runs every Item hook. The Version is the price's audit
		# trail, so it is asked for even where Frappe skips versions by default (tests).
		doc.flags.ignore_permissions = True
		doc.save(ignore_version=False)
	sync_selling_item_price(item_code, price)


def sync_selling_item_price(item_code: str, price: float) -> None:
	"""Make the default list's selling Item Price what the register charges for *price* today:
	on a uniform-priced Item an active sale undercuts it (the serial picker's overlay), so writing
	the raw regular price mid-sale would show full price until the daily re-sync. Creates the Item
	Price when missing; *price* 0 (un-priced by convention) never creates or zeroes one. Targeted
	writes, no Item save. Shared with Receive Goods (``receive_goods._set_item_selling_price``)."""
	if price <= 0:
		return
	from ffl_core.item_hooks import _default_selling_price_list

	price_list = _default_selling_price_list()
	if not price_list:
		return
	charge = price
	meta = frappe.db.get_value(
		"Item", item_code, ["uniform_sell_price", "sale_price", "sale_from", "sale_to"], as_dict=True)
	if meta and meta.uniform_sell_price:
		from ffl_core.firearm import apply_sale

		charge = float(apply_sale(price, meta.sale_price, meta.sale_from, meta.sale_to) or price)
	existing = frappe.db.get_value(
		"Item Price", {"item_code": item_code, "price_list": price_list, "selling": 1}, "name")
	if existing:
		frappe.db.set_value("Item Price", existing, "price_list_rate", charge)
	else:
		frappe.get_doc({
			"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
			"selling": 1, "price_list_rate": charge,
		}).insert(ignore_permissions=True)
