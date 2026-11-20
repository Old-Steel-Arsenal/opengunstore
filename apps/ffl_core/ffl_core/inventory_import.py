"""One-time import of a new store's non-firearm stock from a CSV (Settings →
Inventory Import).

Each row names one product and how many are on the shelf. A row is matched to an
existing Item by UPC (or by item code when it has none), else promoted from a
distributor catalog that lists the UPC, else created from the row itself. The
quantities then go in as opening stock (Stock Reconciliation, purpose Opening
Stock) at the row's unit cost.

Firearms never come in here: the bound book is their source (FastBound → Import
Bound Book, or Receive Goods), so a firearm row is refused with that pointer.
An Item with any stock history is skipped: opening stock is only for an Item the
POS has never moved, so it cannot land before (and re-cost) a later transaction.

The opening value is debited to stock and credited to the company's Temporary
Opening account, dated the cut-over date the store picks (the day its old
system's counts are as of); the bookkeeper clears Temporary Opening once the
total agrees with the old system.

``plan`` reads only, so the preview and the import report the same outcome for
every row; ``run`` acts on the plan.
"""
from __future__ import annotations

import csv
import io
import re

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from ffl_core.firearm import ALL_FIREARM_GROUPS
from ffl_core.utils import commit

DEFAULT_GROUP = "Misc Accessory"

# Template header → row key. Matching ignores case and spacing.
COLUMNS = {
	"upc": "upc",
	"name": "name",
	"brand": "brand",
	"part number": "mpn",
	"category": "category",
	"quantity": "qty",
	"unit cost": "cost",
	"price": "price",
}
TEMPLATE_HEADER = ["UPC", "Name", "Brand", "Part Number", "Category", "Quantity", "Unit Cost", "Price"]

# Row outcomes.
MATCHED, CATALOG, NEW, SKIPPED, ERROR = "matched", "catalog", "new", "skipped", "error"

# lazy: one Stock Reconciliation per this many rows; a shop's accessory list is a few
# thousand rows at most. Raise it, or enqueue per chunk, if a bigger file stalls.
CHUNK = 500


def parse(content: str) -> list[dict]:
	"""CSV text → row dicts keyed by COLUMNS values, each with its 1-based file
	line in ``line``. Unknown columns are ignored; a file without Quantity or
	Unit Cost is refused outright."""
	content = (content or "").lstrip("﻿")
	reader = csv.reader(io.StringIO(content))
	try:
		header = next(reader)
	except StopIteration:
		frappe.throw(_("The file is empty."))
	keys = [COLUMNS.get(re.sub(r"\s+", " ", (h or "").strip().lower())) for h in header]
	missing = [label for label in ("Quantity", "Unit Cost") if COLUMNS[label.lower()] not in keys]
	if missing:
		frappe.throw(_("The file has no {0} column. Start from the template.").format(
			_(" or ").join(missing)))
	rows = []
	for line, values in enumerate(reader, start=2):
		if not any((v or "").strip() for v in values):
			continue
		row = {"line": line}
		for key, value in zip(keys, values):
			if key:
				row[key] = (value or "").strip()
		rows.append(row)
	return rows


def normalize_upc(raw: str) -> tuple[str, str | None]:
	"""(upc, problem). Digits only; a UPC-A that lost its leading zero (a
	spreadsheet read it as a number) gets it back."""
	raw = (raw or "").strip()
	if not raw:
		return "", None
	if re.fullmatch(r"[\d.]+[eE]\+?\d+", raw):
		return "", _("UPC {0} was turned into a number by a spreadsheet. Format the UPC column as text and export again.").format(raw)
	digits = re.sub(r"\D", "", raw)
	if not 8 <= len(digits) <= 14:
		return "", _("UPC {0} is not 8 to 14 digits.").format(raw)
	if len(digits) == 11:
		digits = "0" + digits
	return digits, None


def _number(value, label: str, *, required: bool) -> tuple[float | None, str | None]:
	value = (value or "").replace("$", "").replace(",", "").strip()
	if not value:
		return None, (_("{0} is missing.").format(label) if required else None)
	try:
		return float(value), None
	except ValueError:
		return None, _("{0} '{1}' is not a number.").format(label, value)


def _existing_item(upc: str, code: str) -> str | None:
	if upc:
		hit = frappe.db.get_value("Item Barcode", {"barcode": upc}, "parent")
		if hit:
			return hit
	for candidate in (upc, code):
		if candidate and frappe.db.exists("Item", candidate):
			return candidate
	return None


def _catalog_offer(upc: str) -> dict | None:
	"""The first enabled house's row for this UPC, tagged ``is_firearm``; None
	when no catalog is set up or none lists it."""
	from ffl_integrations.data_service import CATALOG, is_off
	from ffl_integrations.distributor import offers
	from ffl_integrations.distributor.util import legal_firearm

	if not upc or is_off(CATALOG):
		return None
	rows = offers.offers_for(upc=upc)
	if not rows:
		return None
	row = rows[0]
	return {**row, "is_firearm": legal_firearm(row)}


def _firearm_refusal() -> str:
	return _("Firearm. Bring guns in from the bound book (FastBound → Import Bound Book) or Receive Goods.")


def plan(rows: list[dict], warehouse: str) -> list[dict]:
	"""Each row with ``outcome`` (matched / catalog / new / skipped / error),
	``item_code`` and ``message``. Reads only."""
	firearm_groups = set(ALL_FIREARM_GROUPS)
	seen: dict[str, list[dict]] = {}
	out = []
	for row in rows:
		r = dict(row)
		out.append(r)

		def fail(msg, outcome=ERROR):
			r["outcome"], r["message"] = outcome, msg

		upc, problem = normalize_upc(row.get("upc"))
		if problem:
			fail(problem)
			continue
		r["upc"] = upc
		qty, problem = _number(row.get("qty"), _("Quantity"), required=True)
		if problem:
			fail(problem)
			continue
		cost, problem = _number(row.get("cost"), _("Unit Cost"), required=True)
		if problem:
			fail(problem)
			continue
		price, problem = _number(row.get("price"), _("Price"), required=False)
		if problem:
			fail(problem)
			continue
		if qty <= 0:
			fail(_("Quantity is {0}: nothing to bring in.").format(row.get("qty")), SKIPPED)
			continue
		if qty != int(qty):
			fail(_("Quantity {0} is not a whole number.").format(row.get("qty")))
			continue
		if cost <= 0:
			fail(_("Unit Cost must be more than zero."))
			continue
		r.update(qty=qty, cost=cost, price=price)

		code = upc or (row.get("mpn") or row.get("name") or "").strip()[:140]
		if not code:
			fail(_("A row needs a UPC, a Part Number or a Name."))
			continue
		item_code = _existing_item(upc, code)
		if item_code:
			item = frappe.db.get_value("Item", item_code,
				["is_firearm", "item_group", "has_serial_no", "is_stock_item"], as_dict=True)
			if item.is_firearm or item.item_group in firearm_groups:
				fail(_firearm_refusal())
				continue
			if item.has_serial_no or not item.is_stock_item:
				fail(_("{0} is tracked by serial number or does not keep stock.").format(item_code))
				continue
			if frappe.db.exists("Stock Ledger Entry", {"item_code": item_code, "is_cancelled": 0}):
				r["item_code"] = item_code
				fail(_("{0} already has stock history in the POS, so it is left out. Ask your bookkeeper how to add it.")
					.format(item_code), SKIPPED)
				continue
			r.update(outcome=MATCHED, item_code=item_code, message=_("Existing item {0}").format(item_code))
		else:
			offer = _catalog_offer(upc)
			if offer and offer["is_firearm"]:
				fail(_firearm_refusal())
				continue
			if offer:
				r.update(outcome=CATALOG, item_code=code, offer=offer,
					message=_("New item from {0}").format(offer.get("distributor")))
			else:
				group = (row.get("category") or "").strip() or DEFAULT_GROUP
				if group in firearm_groups:
					fail(_firearm_refusal())
					continue
				if not frappe.db.exists("Item Group", {"name": group, "is_group": 0}):
					fail(_("Category '{0}' is not one of the store's categories.").format(group))
					continue
				if not (row.get("name") or "").strip():
					fail(_("Name is missing, and no distributor lists this UPC."))
					continue
				r.update(outcome=NEW, item_code=code, item_group=group, message=_("New item"))
		if price and cost >= price:
			r["message"] += " · " + _("Unit Cost is not below Price; check the two columns.")
		seen.setdefault(r["item_code"], []).append(r)

	for group in seen.values():
		if len(group) > 1:
			lines = ", ".join(str(g["line"]) for g in group)
			for g in group:
				g["outcome"], g["message"] = ERROR, _("Lines {0} are the same item. Combine them into one line.").format(lines)
	return out


def summarize(planned: list[dict]) -> dict:
	counts = {k: 0 for k in (MATCHED, CATALOG, NEW, SKIPPED, ERROR)}
	units = value = 0.0
	for r in planned:
		counts[r["outcome"]] += 1
		if r["outcome"] in (MATCHED, CATALOG, NEW):
			units += r["qty"]
			value += flt(r["qty"] * r["cost"], 2)
	return {"counts": counts, "units": units, "value": flt(value, 2)}


def _create_item(r: dict) -> str:
	if r["outcome"] == CATALOG:
		from ffl_integrations.distributor.api import get_provider
		from ffl_integrations.rsr.promote import _promote_to_item

		offer = r["offer"]
		opts = {"item_code": r["item_code"], "has_serial_no": 0}
		if r.get("price"):
			opts["standard_rate"] = r["price"]
		provider = get_provider(offer["distributor"])
		if provider.code == "rsr":
			made = _promote_to_item({**opts, "rsr_stock_number": offer["sku"]})
		else:
			made = provider.promote_to_item(offer["sku"], opts)
		item_code = made["item_code"]
	else:
		brand = (r.get("brand") or "").strip()
		if brand and not frappe.db.exists("Brand", brand):
			frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": r["item_code"],
			"item_name": r["name"][:140],
			"item_group": r["item_group"],
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"brand": brand or None,
			"mpn": (r.get("mpn") or "").strip() or None,
			"standard_rate": r.get("price") or None,
		})
		if r.get("upc"):
			doc.append("barcodes", {"barcode": r["upc"], "barcode_type": "UPC"})
		doc.insert(ignore_permissions=True)
		item_code = doc.name
	frappe.db.set_value("Item", item_code, "dealer_price", r["cost"])
	return item_code


def check_posting(warehouse: str, posting_date) -> tuple[str, str]:
	"""(company, Temporary Opening account) once the cut-over date and the chart
	of accounts allow opening stock; refuses before anything is written."""
	from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import get_difference_account

	if not posting_date:
		frappe.throw(_("Pick the cut-over date: the day your old system's counts are as of."))
	posting_date = getdate(posting_date)
	if posting_date > getdate(nowdate()):
		frappe.throw(_("The cut-over date cannot be in the future."))
	company = frappe.db.get_value("Warehouse", warehouse, "company")
	frozen = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
	if frozen and posting_date <= getdate(frozen):
		frappe.throw(_("The books are frozen through {0}. Pick a later cut-over date.").format(frozen))
	closed = frappe.get_all("Period Closing Voucher", filters={"company": company, "docstatus": 1},
		pluck="period_end_date", order_by="period_end_date desc", limit=1)
	closed = closed[0] if closed else None
	if closed and posting_date <= getdate(closed):
		frappe.throw(_("The books are closed through {0}. Pick a later cut-over date.").format(closed))
	account = get_difference_account("Opening Stock", company)
	if not account:
		frappe.throw(_("{0} has no Temporary Opening account. Add one to the chart of accounts first.").format(company))
	return company, account


def run(planned: list[dict], *, warehouse: str, posting_date, source: str | None = None) -> dict:
	"""Create the missing Items, then post the opening stock. ``source`` is the
	CSV's File URL, attached to the first reconciliation. Returns
	``{summary, rows, reconciliations}``."""
	company, account = check_posting(warehouse, posting_date)
	for r in planned:
		if r["outcome"] in (CATALOG, NEW):
			frappe.db.savepoint("inventory_import_row")
			try:
				r["item_code"] = _create_item(r)
				commit()
			except Exception as exc:  # noqa: BLE001 — one bad row must not stop the file
				frappe.db.rollback(save_point="inventory_import_row")
				r["outcome"], r["message"] = ERROR, _("Could not create the item: {0}").format(
					frappe.utils.strip_html(str(exc)))
	stock_rows = [r for r in planned if r["outcome"] in (MATCHED, CATALOG, NEW)]
	names = []
	for start in range(0, len(stock_rows), CHUNK):
		chunk = stock_rows[start:start + CHUNK]
		sr = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": company,
			"purpose": "Opening Stock",
			"posting_date": getdate(posting_date),
			"posting_time": "00:00:00",
			"set_posting_time": 1,
			"expense_account": account,
			"items": [{
				"item_code": r["item_code"],
				"warehouse": warehouse,
				"qty": r["qty"],
				"valuation_rate": r["cost"],
			} for r in chunk],
		})
		sr.flags.ignore_permissions = True
		sr.insert()
		sr.submit()
		_note(sr.name, source, chunk, posting_date, first=not names)
		commit()
		names.append(sr.name)
		for r in chunk:
			r["reconciliation"] = sr.name
	return {"summary": summarize(planned), "rows": planned, "reconciliations": names}


def _note(name: str, source: str | None, chunk: list[dict], posting_date, *, first: bool) -> None:
	"""The audit trail: where the opening figures came from."""
	value = flt(sum(flt(r["qty"] * r["cost"], 2) for r in chunk), 2)
	file_name = frappe.db.get_value("File", {"file_url": source}, "file_name") if source else None
	frappe.get_doc({
		"doctype": "Comment", "comment_type": "Comment",
		"reference_doctype": "Stock Reconciliation", "reference_name": name,
		"content": _("Opening stock from {0}: {1} rows, {2} at cost, as of {3}.").format(
			file_name or _("Inventory Import"), len(chunk), frappe.utils.fmt_money(value),
			frappe.utils.formatdate(posting_date)),
	}).insert(ignore_permissions=True)
	if first and source:
		frappe.get_doc({"doctype": "File", "file_url": source, "file_name": file_name,
			"attached_to_doctype": "Stock Reconciliation", "attached_to_name": name,
			"is_private": 1}).insert(ignore_permissions=True)
