"""The Gun Shop Overview page (page ``store-overview``): every block it draws, from one call.

Each block is worked out on its own, from the list or report it summarises, so a number on the page
and the rows behind it cannot disagree:

* the sales tiles are the Sales Report's own rows (Order view), the same Net Sales;
* the 4473, pickup and stock blocks are the rows of their reports;
* the cash drawer is what the Cash Drawer page shows (``api.cash_drawer.get_today``);
* "Not logged" is the set the daily FastBound retry works on (``fastbound.tasks.unsynced_records``);
* "Arriving" is what Receive Goods lists as incoming (``receive_bridge.rank_candidates``).

Two rules hold for every block:

* **its own permission** -- a block this user may not see is ``None`` and the page leaves it out;
  the report blocks use the report's own role check (``gun_shop_cards._may_open``);
* **its own failure** -- a block that raises is ``{"error": ...}`` and an Error Log; the others
  still load (the Pending Order page treats its three sources the same way).

Counts and report rows are shared by every user for a minute (``gun_shop_cards._cached``);
the integration app is asked only when it is installed (ffl_core never imports it at load).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.desk_views import DeskViews
from frappe.desk.utils import is_item_allowed
from frappe.utils import add_days, add_to_date, cint, flt, getdate, now_datetime, nowdate

from ffl_core.api.gun_shop_cards import CACHE_PREFIX, CACHE_SECONDS, _cached, _may_open
from ffl_core.ffl_core.report.firearms_in_stock.firearms_in_stock import execute as firearms_report
from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import execute as pending_4473_report
from ffl_core.ffl_core.report.pending_transfer_pickups.pending_transfer_pickups import (
	execute as transfer_pickups_report,
)
from ffl_core.ffl_core.report.sales_report.sales_report import execute as sales_report
from ffl_core.roles import CASH_LOG_ROLES, COUNT_ROLES, COUNTER_ROLES, OVERVIEW_ROLES

PICKUP_ROWS = 4
ARRIVING_ROWS = 4
OLD_PICKUP_DAYS = 21               # the page turns a pickup amber past this (sovDaysClass)
NOT_LOGGED_AFTER_MINUTES = 15      # a push normally lands within seconds; younger ones are in flight
REVIEW_ROWS = 50                   # lazy: the Review dialog lists the first 50, upgrade = page it
PURCHASE_ROLES = ("Purchase User", "Purchase Manager")
SALES_DAYS = 8                     # today, the six days before it (the bars) and last week's same day

REPORT_4473 = "Pending 4473 Orders"
REPORT_PICKUPS = "Pending Transfer Pickups"
REPORT_STOCK = "Firearms In Stock"
REPORT_RECORDS_4473 = "Form 4473 Records"


def _roles() -> set[str]:
	return set(frappe.get_roles())


FAILED_KEY = CACHE_PREFIX + "overview:failed:"


def _failed_key(name: str, per_user: bool) -> str:
	return FAILED_KEY + name + (f":{frappe.session.user}" if per_user else "")


def _block(name: str, allowed, build, per_user: bool = False):
	"""One block: ``None`` when this user may not see it (``allowed()`` is false), ``{"error": ...}``
	when the permission check or the block fails.

	A failure stays quiet: whatever the block queued for the browser (a ``frappe.throw`` writes its
	message before it raises) is taken back, so the page gets no error pop-up. It is remembered for
	CACHE_SECONDS: until then the block answers the error without running again, so a broken block
	costs one run and one Error Log a minute, not one per view. A block worked out for the store
	(its rows are shared) is remembered for the store; one worked out for this user (``per_user``:
	their company's drawer, the links they may open) only for them."""
	failed = {"error": _("This could not be loaded. The other blocks are up to date.")}
	key = _failed_key(name, per_user)
	if frappe.cache.get_value(key, expires=True):
		return failed
	queued = len(frappe.local.message_log or [])
	try:
		if not allowed():
			return None
		return build()
	except Exception:
		del frappe.local.message_log[queued:]
		frappe.log_error(title=f"Overview: {name} block failed")
		frappe.cache.set_value(key, 1, expires_in_sec=CACHE_SECONDS)
		return failed


# ------------------------------------------------------------------ sales

def _sales() -> dict:
	"""Per day for today and the seven days before it, from ONE Sales Report run (Order view):
	``net_sales`` is the report's own Net Sales (returns included); ``transactions`` counts the sales,
	not the returns, and ``ticket_net`` is their Net Sales, the base of the average ticket."""
	today = getdate(nowdate())
	start = add_days(today, -(SALES_DAYS - 1))

	def build():
		rows = sales_report({"from_date": start, "to_date": today, "view": "Order"})[1]
		days = {str(add_days(start, i)): {"net_sales": 0.0, "transactions": 0, "ticket_net": 0.0} for i in range(SALES_DAYS)}
		for r in rows:
			if r.get("is_total"):
				continue
			day = days.get(str(getdate(r.get("posting_date"))))
			if day is not None:
				day["net_sales"] = flt(day["net_sales"] + flt(r.get("net_sales")), 2)
				if not r.get("is_return"):
					day["transactions"] += 1
					day["ticket_net"] = flt(day["ticket_net"] + flt(r.get("net_sales")), 2)
		return [{"date": d, **v} for d, v in days.items()]

	# lazy: the report costs every line of eight days to show three numbers and seven bars; one run a
	# minute for the whole store. Upgrade path = a net-only path through the report (no costing).
	return {"days": _cached(f"overview:sales:{today}", build), "report": _may_open(["query-report", "Sales Report"])}


# ------------------------------------------------------------------ report blocks

def _names(doctype: str, field: str, names) -> dict:
	"""{name: <field>} for the given names of ``doctype`` (item names, customer names), one query."""
	wanted = sorted({n for n in names if n})
	if not wanted:
		return {}
	return dict(frappe.get_all(doctype, filters={"name": ["in", wanted]}, fields=["name", field], as_list=True))


def _open_4473() -> dict:
	"""Every row of Pending 4473 Orders (the page filters them by NICS state and shows five), with
	the customer's name, each gun's name and when the order was started."""
	def build():
		rows = pending_4473_report({})[1]
		guns = {r["pos_invoice"]: [s.strip() for s in (r.get("serials") or "").split(",") if s.strip()] for r in rows}
		item_of = _names("Serial No", "item_code", (s for serials in guns.values() for s in serials))
		names = _names("Item", "item_name", item_of.values())
		customers = _names("Customer", "customer_name", (r.get("customer") for r in rows))
		started = _names("POS Invoice", "creation", guns)
		return [{
			"invoice": r["pos_invoice"],
			"customer_name": customers.get(r.get("customer")) or r.get("customer") or "",
			"guns": [{"serial": s, "item_name": names.get(item_of.get(s)) or item_of.get(s) or ""} for s in guns[r["pos_invoice"]]],
			"nics_status": r.get("nics_status") or "",
			"form_4473_status": r.get("form_4473_status") or "",
			"started": str(started.get(r["pos_invoice"]) or ""),
		} for r in rows]

	return {
		"rows": _cached("overview:open_4473", build),
		"records": _may_open(["query-report", REPORT_RECORDS_4473]),
		# The row buttons open the order; only for someone who may read it.
		"can_open": bool(frappe.has_permission("POS Invoice", "read")),
	}


def _pickups() -> dict:
	"""Pending Transfer Pickups, the first four: the report lists them oldest acquisition first,
	which is longest wait first."""
	def build():
		rows = transfer_pickups_report({})[1]
		top = rows[:PICKUP_ROWS]
		names = _names("Item", "item_name", (r.get("item_code") for r in top))
		customers = _names("Customer", "customer_name", (r.get("customer") for r in top))
		return {"total": len(rows), "rows": [{
			"customer": customers.get(r.get("customer")) or r.get("customer") or "",
			"item_name": names.get(r.get("item_code")) or r.get("item_code") or "",
			"serial": r.get("serial") or "",
			"source_name": r.get("source_name") or "",
			"days_in_store": int(r.get("days_in_store") or 0),
		} for r in top]}

	return _cached("overview:pickups", build)


def _stock() -> dict:
	"""Firearms In Stock's row count, and how many of those guns are consigned or a customer's
	transfer (the Serial No flags the Sales Report reads the same way).

	# lazy: the whole report (every in-stock gun, with its acquisition and FastBound link) plus one IN
	# query over all of their serials, once a minute for the store, to show three numbers. Upgrade
	# path = COUNT / SUM with the report's own conditions (status Active, firearm Item).
	"""
	def build():
		serials = [r["serial_no"] for r in firearms_report({})[1]]
		flags = frappe.get_all("Serial No", filters={"name": ["in", serials]},
			fields=["is_consignment", "is_transfer_gun"]) if serials else []
		return {
			"total": len(serials),
			"consignment": sum(1 for f in flags if f.is_consignment),
			"transfers": sum(1 for f in flags if f.is_transfer_gun),
		}

	return _cached("overview:stock", build)


# ------------------------------------------------------------------ cash drawer

def _drawer() -> dict:
	"""What the Cash Drawer page shows, trimmed to what the block draws, with get_today's cash in / out.

	# lazy: not cached -- money a cashier just moved should show on the next refresh -- so every open
	# Overview runs get_today once a minute. Upgrade path = cache it per company for a few seconds,
	# or have the cash-drawer writes clear a cached copy.
	"""
	from ffl_core.api.cash_drawer import get_today

	d = get_today()
	return {
		"opening": flt(d.get("opening"), 2),
		"expected": flt(d.get("expected"), 2),
		"cash_in": d["cash_in"],
		"cash_out": d["cash_out"],
		"open_shifts": d.get("open_shifts") or 0,
		"counted": bool(d.get("closed_today")),
	}


# ------------------------------------------------------------------ A&D

def _ad_key() -> str:
	return f"overview:ad:{getdate(nowdate())}"


def _ad() -> dict:
	"""Today's acquisitions and dispositions, and what the bound book does not have yet."""
	if "ffl_integrations" not in frappe.get_installed_apps():
		return {"off": True}
	from ffl_integrations.integration_gate import fastbound_enabled

	if not fastbound_enabled():
		return {"off": True}

	def build():
		from ffl_integrations.fastbound.tasks import unsynced_records

		today = getdate(nowdate())
		tomorrow = add_days(today, 1)

		def booked_today(doctype, field):
			return frappe.db.count(doctype, [["docstatus", "=", 1], [field, ">=", today], [field, "<", tomorrow]])

		def last_synced(doctype):
			return (frappe.get_all(doctype, filters={"fastbound_synced_at": ["is", "set"]}, order_by="fastbound_synced_at desc",
				limit=1, pluck="fastbound_synced_at") or [None])[0]

		# Submitted at least this long ago; ``modified`` is the submit (the push stamps without touching it).
		cutoff = add_to_date(now_datetime(), minutes=-NOT_LOGGED_AFTER_MINUTES)
		stale = [r for r in unsynced_records() if r["modified"] <= cutoff]
		synced = [s for s in (last_synced("FFL Acquisition"), last_synced("FFL Disposition")) if s]
		return {
			"off": False,
			"acquired": booked_today("FFL Acquisition", "acquisition_date"),
			"disposed": booked_today("FFL Disposition", "disposition_date"),
			"retrying": sum(1 for r in stale if not r["terminal"]),
			"needs_person": sum(1 for r in stale if r["terminal"]),
			"synced_at": str(max(synced)) if synced else "",
			"records": [{k: r[k] for k in ("doctype", "name", "error", "terminal")}
				for r in sorted(stale, key=lambda r: (not r["terminal"], str(r["modified"])))[:REVIEW_ROWS]],
		}

	data = dict(_cached(_ad_key(), build))
	# Only someone who may open both lists sees which records they are (the Review button).
	data["can_review"] = all(frappe.has_permission(dt, "read") for dt in ("FFL Acquisition", "FFL Disposition"))
	if not data["can_review"]:
		data.pop("records", None)
	return data


# ------------------------------------------------------------------ arriving

def _arriving() -> dict:
	"""Distributor orders with units still to arrive, the order Receive Goods lists them in."""
	from ffl_integrations.distributor.receive_bridge import _incoming_line_rows

	return _cached("overview:arriving", lambda: fold_arrivals(_incoming_line_rows()))


def fold_arrivals(rows: list) -> dict:
	"""Open distributor order lines (``_incoming_line_rows``) as one entry per order, in the order
	Receive Goods ranks them. Every figure is the receive bridge's own line rule
	(``expected_qty`` / ``outstanding_qty``): what came in counts booked and counter-substituted
	units alike, so expected - received = the units still to come."""
	from ffl_integrations.distributor.receive_bridge import expected_qty, outstanding_qty, rank_candidates

	orders: dict[str, dict] = {}
	for r in rank_candidates(rows):
		o = orders.setdefault(r["do_name"], {"name": r["do_name"], "distributor": r["distributor"],
			"status": r["status"], "outstanding": 0, "received": 0, "expected": 0})
		o["outstanding"] += int(r["outstanding"])
	for r in rows:                 # received / expected cover every line of the order, not just the open ones
		o = orders.get(r["do_name"])
		if o:
			expected = expected_qty(r)
			o["expected"] += expected
			o["received"] += expected - outstanding_qty(r)
	return {"total": len(orders), "orders": list(orders.values())[:ARRIVING_ROWS]}


def _arriving_allowed(roles: set[str]) -> bool:
	if "ffl_integrations" not in frappe.get_installed_apps():
		return False
	from ffl_integrations.distributor.receive_bridge import RECEIVE_ROLES

	return bool(roles & set(COUNTER_ROLES + RECEIVE_ROLES + PURCHASE_ROLES))


# ------------------------------------------------------------------ all records

# (group, [(label, link_type, link_to)]) -- the core groups every store has. The Online group and
# one group per extension are added in records(); Setup always comes last.
CORE_RECORDS = [
	("Sales", [
		("POS invoices", "DocType", "POS Invoice"),
		("Sales invoices", "DocType", "Sales Invoice"),
		("Sales orders", "DocType", "Sales Order"),
		("Gunsmith work orders", "DocType", "Gunsmith Work Order"),
		("Sales report", "Report", "Sales Report"),
		("Cash drawer weekly", "Report", "Cash Drawer Weekly"),
		("Card payments", "DocType", "Card Terminal Payment"),
		("POS opening entries", "DocType", "POS Opening Entry"),
		("POS closing entries", "DocType", "POS Closing Entry"),
	]),
	("Compliance", [
		("4473 records", "Report", "Form 4473 Records"),
		("Pending 4473 orders", "Report", "Pending 4473 Orders"),
		("Firearms in stock", "Report", "Firearms In Stock"),
		("Pending transfer pickups", "Report", "Pending Transfer Pickups"),
		("FFL acquisitions", "DocType", "FFL Acquisition"),
		("FFL dispositions", "DocType", "FFL Disposition"),
		("ATF FFL records", "DocType", "ATF FFL Record"),
	]),
	("Purchasing", [
		("Distributor orders", "Page", "distributor-restock"),
		("Purchase orders", "DocType", "Purchase Order"),
		("Purchase receipts", "DocType", "Purchase Receipt"),
		("Inventory receipts", "Report", "Inventory Receipts"),
		("Suppliers", "DocType", "Supplier"),
	]),
	("Inventory", [
		("Items", "DocType", "Item"),
		("Item groups", "DocType", "Item Group"),
		("Serial numbers", "DocType", "Serial No"),
		("Stock entries", "DocType", "Stock Entry"),
		("Stock balance", "Report", "Stock Balance"),
		("Stock ledger", "Report", "Stock Ledger"),
		("Inventory count", "Page", "stocktake"),
		("Storage locations", "Page", "storage-map"),
	]),
	("Customers", [
		("Customers", "DocType", "Customer"),
		("Customer groups", "DocType", "Customer Group"),
		("Agreements", "DocType", "Customer Agreement"),
		("Agreement templates", "DocType", "Agreement Template"),
		("Signing tablets", "DocType", "Signing Terminal"),
	]),
]
ONLINE_RECORDS = ("Online", [
	("Web orders", "DocType", "Woo Online Order"),
	("GunBroker orders", "DocType", "GunBroker Order"),
])
SETUP_RECORDS = ("Setup", [
	("POS profiles", "DocType", "POS Profile"),
	("Modes of payment", "DocType", "Mode of Payment"),
	("Calibers", "DocType", "Caliber"),
	("Brands", "DocType", "Brand"),
	("Warehouses", "DocType", "Warehouse"),
	("FFL settings", "DocType", "FFL Settings"),
	("FastBound settings", "DocType", "FastBound Settings"),
])
SHIPSTATION_RECORD = ("ShipStation settings", "DocType", "ShipStation Settings")
ONLINE_LINKS = {link_to for _label, _type, link_to in ONLINE_RECORDS[1]}


def records() -> list[dict]:
	"""All records: the core groups, Online while a web channel is on, one group per extension that
	is on (its ``gunshop_sidebar_items`` links, under its section's title), Setup last. Each link is
	kept only if this user may open it, by the rule the sidebar uses (``is_item_allowed``), and an
	integration that is switched off takes its link with it."""
	from ffl_core.setup.gunshop_sidebar import extension_rows, integrations_off

	off = integrations_off()
	online = bool(ONLINE_LINKS - off)
	groups = [*CORE_RECORDS]
	if online:
		groups.append(ONLINE_RECORDS)
	title, links = None, []
	for row in extension_rows():
		if row.get("type") == "Section Break":
			if title and links:
				groups.append((title, links))
			title, links = row.get("label"), []
		elif row.get("type") == "Link" and row.get("link_type") in ROUTABLE and (row.get("link_to") or row.get("url")):
			links.append((row.get("label") or row.get("link_to") or row["url"], row["link_type"], row.get("link_to") or row["url"]))
	if title and links:
		groups.append((title, links))
	groups.append((SETUP_RECORDS[0], SETUP_RECORDS[1] + ([SHIPSTATION_RECORD] if online else [])))

	ctx = DeskViews()
	admin = frappe.session.user == "Administrator"     # is_item_allowed answers True for anything, there only

	def allowed(lt, to):
		if lt == "URL":
			return True
		if lt == "Report":                            # the report blocks' own rule (ref doctype report permission)
			return _may_open(["query-report", to])
		return (not admin or frappe.db.exists(lt, to)) and is_item_allowed(to, lt, ctx)

	out = []
	for title, links in groups:
		items = [_record(label, lt, to) for label, lt, to in links if to not in off and allowed(lt, to)]
		if items:
			out.append({"title": _(title), "items": items})
	return out


# The link types the page can route (frappe.utils.generate_route, plus a URL as it is); a row of any
# other type is left out.
ROUTABLE = ("DocType", "Report", "Page", "Dashboard", "Workspace", "URL")


def _record(label: str, link_type: str, link_to: str) -> dict:
	"""One All records link, with what generate_route needs to spell its route."""
	item = {"label": _(label), "link_type": link_type, "link_to": link_to}
	if link_type == "Report":
		report_type, ref = frappe.get_cached_value("Report", link_to, ["report_type", "ref_doctype"])
		item.update(is_query_report=report_type in ("Query Report", "Script Report"), ref_doctype=ref)
	return item


# ------------------------------------------------------------------ the endpoint

@frappe.whitelist()
def get_overview(with_records=1, fresh_ad=0) -> dict:
	"""Everything the Gun Shop Overview page shows, one block per key.

	A block this user may not see is null (the page leaves it out); a block that failed is
	``{"error": text}`` and an Error Log, and the others still load. Counts and report rows are
	shared by every user for up to a minute.

	Args:
	    with_records: 0 leaves out All records (the page asks for it once, not on every refresh).
	    fresh_ad: 1 recounts the A&D block now instead of reading the minute-old shared count (the
	        block's refresh button).

	Returns:
	    {today, now, sales: {days: [{date, net_sales, transactions, ticket_net}] (8 days, oldest
	    first, today last; transactions and ticket_net leave returns out), report}, open_4473: {rows: [{invoice, customer_name, guns: [{serial, item_name}],
	    nics_status, form_4473_status, started}], records, can_open}, pickups: {total, rows:
	    [{customer, item_name, serial, source_name, days_in_store}]}, stock: {total, consignment,
	    transfers}, drawer: {opening, expected, cash_in, cash_out, open_shifts, counted},
	    ad: {off} or {off, acquired, disposed, retrying, needs_person, synced_at, can_review,
	    records?: [{doctype, name, error, terminal}]}, arriving: {total, orders: [{name,
	    distributor, status, outstanding, received, expected}]}, records (only with
	    with_records): [{title, items: [{label, link_type, link_to, is_query_report?, ref_doctype?}]}],
	    links: {pos, new_order,
	    receive, add_customer, cash_drawer, pickups}}.
	"""
	frappe.only_for(OVERVIEW_ROLES)
	roles = _roles()

	def report(name):
		return lambda: _may_open(["query-report", name])

	def has(role_set):
		return lambda: bool(roles & set(role_set))

	ad_allowed = has(COUNTER_ROLES + COUNT_ROLES)
	if cint(fresh_ad) and ad_allowed():
		frappe.cache.delete_value(CACHE_PREFIX + _ad_key())
		frappe.cache.delete_value(_failed_key("ad", False))
	out = {
		"today": nowdate(),
		"now": str(now_datetime()),
		"sales": _block("sales", has(CASH_LOG_ROLES), _sales),
		"open_4473": _block("open_4473", report(REPORT_4473), _open_4473),
		"pickups": _block("pickups", report(REPORT_PICKUPS), _pickups),
		"stock": _block("stock", report(REPORT_STOCK), _stock),
		"drawer": _block("drawer", has(COUNTER_ROLES), _drawer, per_user=True),
		"ad": _block("ad", ad_allowed, _ad),
		"arriving": _block("arriving", lambda: _arriving_allowed(roles), _arriving),
		"links": _links(),
	}
	if cint(with_records):
		out["records"] = _block("records", lambda: True, records, per_user=True)
	return out


def _links() -> dict:
	"""Which of the page's buttons this user may follow; a failed check here hides them, nothing else."""
	try:
		return {
			"pos": _may_open(["point-of-sale"]),
			"new_order": _may_open(["create-order"]),
			"receive": _may_open(["receive-goods"]),
			"add_customer": bool(frappe.has_permission("Customer", "create")),
			"cash_drawer": _may_open(["cash-drawer"]),
			"pickups": _may_open(["query-report", REPORT_PICKUPS]),
		}
	except Exception:
		frappe.log_error(title="Overview: page buttons failed")
		return {}
