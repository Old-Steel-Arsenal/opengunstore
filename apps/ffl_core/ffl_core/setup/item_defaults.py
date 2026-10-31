"""Per-company Item Default rows written directly, for the items setup points at fixed accounts."""
from __future__ import annotations

import frappe


def set_item_default_accounts(item_code: str, values_for) -> int:
	"""Per company, set the Item Default fields ``values_for(company)`` returns (None or {} skips the
	company). Returns how many companies changed. The caller clears the Item's document cache.

	Written row by row, not through Item.save: saving fills a new row's default warehouse from the site
	default, which belongs to one company only, and the Item then refuses the row of any other company."""
	rows = {r.company: r for r in frappe.get_all(
		"Item Default", filters={"parent": item_code, "parenttype": "Item"}, fields=["*"])}
	last = max((r.idx for r in rows.values()), default=0)
	changed = 0
	for company in frappe.get_all("Company", pluck="name"):
		values = values_for(company)
		row = rows.get(company)
		if not values or (row and all(row.get(k) == v for k, v in values.items())):
			continue
		if row:
			frappe.db.set_value("Item Default", row.name, values, update_modified=False)
		else:
			last += 1
			frappe.get_doc({
				"doctype": "Item Default", "name": frappe.generate_hash(length=10), "parent": item_code,
				"parenttype": "Item", "parentfield": "item_defaults", "idx": last, "company": company, **values,
			}).db_insert()
		changed += 1
	return changed
