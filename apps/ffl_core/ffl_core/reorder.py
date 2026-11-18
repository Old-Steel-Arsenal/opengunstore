"""Counter setup and Desk alerts for ERPNext automatic replenishment."""

from math import isfinite

import frappe
from frappe import _
from frappe.permissions import has_permission
from frappe.utils import cstr

from ffl_core.alerts import notify_staff
from ffl_core.api._helpers import default_warehouse
from ffl_core.firearm import is_per_serial_priced
from ffl_core.warehouses import custody_warehouse_names


MANAGER_ROLES = ("Stock Manager", "Purchase Manager")


def _eligible(item):
	return item.is_stock_item and not item.disabled and not is_per_serial_priced(item)


def _warehouse_filters():
	filters = [["disabled", "=", 0], ["is_group", "=", 0], ["consignment_dealer", "is", "not set"]]
	excluded = custody_warehouse_names() | set(frappe.get_all(
		"Warehouse", filters={"warehouse_type": "Transit"}, pluck="name"
	))
	if excluded:
		filters.append(["name", "not in", sorted(excluded)])
	return filters


def load_setup(item, method=None):
	if not _eligible(item) or not item.has_permission("write"):
		return
	if not (has_permission("Warehouse", "read", print_logs=False)
		or has_permission("Warehouse", "select", print_logs=False)):
		return
	warehouses = frappe.get_list(
		"Warehouse", filters=_warehouse_filters(), pluck="name", order_by="name", limit_page_length=0
	)
	default = default_warehouse()
	if default not in warehouses:
		default = warehouses[0] if len(warehouses) == 1 else None
	item.set_onload("reorder_setup", {"warehouses": warehouses, "default_warehouse": default})


@frappe.whitelist(methods=["POST"])
def save_rule(item_code: str, warehouse: str, level: float, quantity: float, modified: str) -> None:
	"""Save or remove a purchase reorder rule for an item and warehouse.

	Args:
		item_code: Saved item to configure.
		warehouse: Receiving warehouse.
		level: Nonnegative projected stock threshold, in stock units.
		quantity: Reorder quantity in stock units, or zero to remove the rule.
		modified: Item modification timestamp from the form.

	Returns:
		None.
	"""
	item = frappe.get_doc("Item", item_code, for_update=True)
	item.check_permission("write")
	if cstr(item.modified) != modified:
		frappe.throw(_("This item was modified. Reload it before changing the reorder settings."), frappe.TimestampMismatchError)
	if not _eligible(item):
		frappe.throw(_("Automatic reorder is unavailable for this item."))
	if not all(isfinite(value) and value >= 0 for value in (level, quantity)):
		frappe.throw(_("Reorder level and quantity must be finite, nonnegative numbers."))
	if not frappe.get_list(
		"Warehouse", filters=[*_warehouse_filters(), ["name", "=", warehouse]],
		pluck="name", limit_page_length=1
	):
		frappe.throw(_("Select a warehouse you can use for stock replenishment."), frappe.PermissionError)

	rule = next(
		(row for row in item.reorder_levels if row.warehouse == warehouse and row.material_request_type == "Purchase"),
		None,
	)
	if not quantity:
		if not rule:
			return
		item.remove(rule)
	else:
		if not rule:
			rule = item.append("reorder_levels", {
				"warehouse": warehouse, "material_request_type": "Purchase",
			})
		rule.warehouse_reorder_level = level
		rule.warehouse_reorder_qty = quantity
	item.save()


def notify_managers(request, method=None):
	if not request.get("auto_created_via_reorder"):
		return
	items = frappe.get_all("Item", filters={"name": ("in", [row.item_code for row in request.get("items")])},
		fields=["is_firearm", "item_group", "uniform_sell_price"])
	if any(not is_per_serial_priced(item) for item in items):
		notify_staff(
			request.doctype, request.name, _("Automatic replenishment request is ready for review"),
			MANAGER_ROLES, match={"docstatus": 1, "auto_created_via_reorder": 1}, require_read=True,
		)
