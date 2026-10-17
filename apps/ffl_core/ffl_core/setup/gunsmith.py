"""Idempotent setup for gunsmith work orders (rules in ``ffl_core.gunsmith``).

* **GUNSMITHING** — the default labor item (Service group, non-stock), named in
  FFL Settings › Gunsmithing. The store may point the setting at its own item; the
  labor item's own Item Tax Template decides whether labor is taxed, because that is
  a state question (repair labor is taxable in some states, separately stated labor
  is exempt in others).
* **GUNSMITH-DEPOSIT** — the deposit line. Non-stock, booked to the company's
  Customer Deposits liability (Item Default income account), with the company's
  0% "Non-Taxable Service" template attached on the item itself, so it stays
  untaxed whatever the Service group's default becomes. A locked line
  (``setup/locked_lines``): its amount comes from the work order, never a price.
* **Row / header links** — ``ffl_work_order`` on POS Invoice, POS Invoice Item and
  Sales Invoice Item (copied on return and consolidation), what the deposit gate
  and the sales report read.
* **Gunsmithing Authorization** — a Per document Agreement Template the customer
  signs for each work order, named in FFL Settings. Seeded once; the store edits
  the text.

Lifecycle like the other setup modules: lazy at first use + after_migrate, never
after_install (items and accounts need masters a fresh site has not got yet), and
the migrate path never raises.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.setup.customer_deposits import deposit_account
from ffl_core.setup.tax import SERVICE_GROUP
from ffl_core.utils import commit

LABOR_ITEM = "GUNSMITHING"
DEPOSIT_ITEM = "GUNSMITH-DEPOSIT"
AGREEMENT_TEMPLATE = "Gunsmithing Authorization"
LINK_FIELD = "ffl_work_order"

_LINK = {"fieldname": LINK_FIELD, "fieldtype": "Link", "options": "Gunsmith Work Order",
	"label": "Gunsmith Work Order", "read_only": 1, "no_copy": 0}
FIELDS = {
	"POS Invoice": [{**_LINK, "insert_after": "customer_name", "in_standard_filter": 1}],
	"POS Invoice Item": [{**_LINK, "insert_after": "item_name"}],
	"Sales Invoice Item": [{**_LINK, "insert_after": "item_name"}],
}

_AGREEMENT_BODY = """<p>I, {{ customer_name }}, leave the firearm below with {{ store_name }} for gunsmithing
under work order {{ work_order }}.</p>
<p><b>Firearm:</b> {{ manufacturer }} {{ model }}, {{ caliber }}, serial {{ serial_no }} ({{ firearm_type }}).<br>
<b>Accessories left with it:</b> {{ accessories }}<br>
<b>Condition at intake:</b> {{ condition }}</p>
<p><b>Work requested:</b> {{ work_requested }}</p>
<p><b>Estimated cost:</b> {{ estimated_cost }} &middot; <b>Deposit:</b> {{ deposit }} &middot;
<b>Estimated completion:</b> {{ estimated_completion }}</p>
<p>I authorize the work above. I understand the estimate is not a fixed price; the store will contact me
before going materially over it. I own this firearm and may lawfully possess it. I will show photo ID to
pick it up.</p>"""


def ensure() -> dict:
	"""Create the fields, items, settings defaults and agreement template. Idempotent; never raises."""
	out = {"items": 0, "defaults": 0}
	try:
		create_custom_fields({dt: [{**f, "module": "FFL Core"} for f in rows] for dt, rows in FIELDS.items()},
			ignore_validate=True)
		out["items"] += _ensure_item(LABOR_ITEM, "Gunsmithing Labor",
			"Gunsmith labor on a work order. Whether it is taxed follows this item's Item Tax Template.")
		out["items"] += _ensure_item(DEPOSIT_ITEM, "Gunsmithing Deposit",
			"Deposit held for a customer against a gunsmith work order (a liability, not a sale). Locked to its "
			"work order; added only from the work order.")
		out["defaults"] = _ensure_deposit_defaults()
		_ensure_agreement_template()
		_ensure_settings()
		from ffl_core.setup import locked_lines
		locked_lines.ensure()
		commit()
	except Exception:
		frappe.log_error(title="gunsmith setup failed", message=frappe.get_traceback())
		out["error"] = 1
	return out


def labor_item() -> str:
	return frappe.db.get_single_value("FFL Settings", "gunsmith_labor_item") or LABOR_ITEM


def agreement_template() -> str | None:
	return frappe.db.get_single_value("FFL Settings", "gunsmith_agreement_template") or None


def _ensure_item(code: str, name: str, description: str) -> int:
	if frappe.db.exists("Item", code):
		return 0
	if not frappe.db.exists("Item Group", SERVICE_GROUP):
		from ffl_core.setup.item_groups import ensure as ensure_item_groups
		ensure_item_groups()
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": name, "item_group": SERVICE_GROUP,
		"stock_uom": "Nos", "is_stock_item": 0, "has_serial_no": 0, "is_sales_item": 1,
		"is_purchase_item": 0, "is_firearm": 0, "description": description,
	}).insert(ignore_permissions=True)
	return 1


def _ensure_deposit_defaults() -> int:
	"""Per company: income account = Customer Deposits, and the 0% template on the item itself.
	Rows written directly, like setup/trade_in_credit (Item.save would fill one company's default
	warehouse on every row and then refuse the others)."""
	from ffl_core.setup import customer_deposits
	from ffl_core.setup.tax import _exempt_templates_by_company

	if not frappe.db.exists("Item", DEPOSIT_ITEM):
		return 0
	changed = 0
	rows = {r.company: r for r in frappe.get_all("Item Default",
		filters={"parent": DEPOSIT_ITEM, "parenttype": "Item"}, fields=["name", "company", "income_account", "idx"])}
	last = max((r.idx for r in rows.values()), default=0)
	for company in frappe.get_all("Company", pluck="name"):
		account = deposit_account(company)
		if not account:
			customer_deposits._ensure_account(company)
			account = deposit_account(company)
		row = rows.get(company)
		if not account or (row and row.income_account == account):
			continue
		if row:
			frappe.db.set_value("Item Default", row.name, "income_account", account, update_modified=False)
		else:
			last += 1
			frappe.get_doc({"doctype": "Item Default", "name": frappe.generate_hash(length=10), "parent": DEPOSIT_ITEM,
				"parenttype": "Item", "parentfield": "item_defaults", "idx": last, "company": company,
				"income_account": account}).db_insert()
		changed += 1
	have = set(frappe.get_all("Item Tax", filters={"parent": DEPOSIT_ITEM, "parenttype": "Item"},
		pluck="item_tax_template"))
	idx = len(have)
	for template in _exempt_templates_by_company().values():
		if template in have:
			continue
		idx += 1
		frappe.get_doc({"doctype": "Item Tax", "name": frappe.generate_hash(length=10), "parent": DEPOSIT_ITEM,
			"parenttype": "Item", "parentfield": "taxes", "idx": idx, "item_tax_template": template}).db_insert()
		changed += 1
	if changed:
		frappe.clear_document_cache("Item", DEPOSIT_ITEM)
	return changed


def _ensure_agreement_template() -> None:
	if frappe.db.exists("Agreement Template", AGREEMENT_TEMPLATE):
		return
	frappe.get_doc({
		"doctype": "Agreement Template", "title": AGREEMENT_TEMPLATE, "active": 1, "version": 1,
		"requirement": "Optional", "scope": "Per document", "show_on_counter_panel": 0,
		"minors_need_guardian": 0, "valid_days": 0, "body": _AGREEMENT_BODY,
		"description": "Signed by the customer when a gun is left for gunsmithing.",
	}).insert(ignore_permissions=True)


def _ensure_settings() -> None:
	updates = {}
	if not frappe.db.get_single_value("FFL Settings", "gunsmith_labor_item") and frappe.db.exists("Item", LABOR_ITEM):
		updates["gunsmith_labor_item"] = LABOR_ITEM
	if not frappe.db.get_single_value("FFL Settings", "gunsmith_agreement_template") \
			and frappe.db.exists("Agreement Template", AGREEMENT_TEMPLATE):
		updates["gunsmith_agreement_template"] = AGREEMENT_TEMPLATE
	for field, value in updates.items():
		frappe.db.set_single_value("FFL Settings", field, value)
