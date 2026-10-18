"""Warehouse resolution — the counter's "default stock warehouse" fallback,
made outbound-consignment-safe.

Three call sites (ffl_core.api.manual_order._store_warehouse,
ffl_core.api.firearm_intake._default_warehouse,
ffl_integrations.fastbound.importer._default_warehouse) each pick "the
company's first non-group Warehouse" as a fallback when no POS Profile is
configured for the current user/company. None of them excluded anything or
ordered the query, so once a per-dealer outbound-consignment warehouse
exists (ensure_consignment_warehouse), an unluckily-ordered result could
hand one of THOSE back as "the" default sale/receiving warehouse — silently
routing a normal counter sale's stock deduction, or a normal intake receipt,
into a warehouse that must hold only consigned-out guns (
`manual_order._store_warehouse` is the highest-risk of the three: it IS the
fallback after the POS Profile's own warehouse).

default_stock_warehouse() is the single, safe replacement all three call
sites now delegate to: it never returns a consignment warehouse, and it
orders deterministically (creation asc) instead of leaving the pick to
whatever order MySQL happens to return rows in without an ORDER BY.
"""
from __future__ import annotations

import frappe


def default_stock_warehouse(company: str) -> str | None:
	"""The company's default sellable/receivable warehouse — never a
	per-dealer outbound-consignment warehouse, never disabled, never a
	Transit-type holding warehouse (ERPNext's own "Goods In Transit" default
	is exactly this shape).

	Resolution order (the earlier "oldest matching
	warehouse wins" heuristic could return a long-retired, later-disabled
	warehouse on any site old enough to have one, and gave no way to pin a
	deliberate answer):

	  1. Company.default_warehouse (Company › Warehouse Defaults; ERPNext 16.51
		 moved it there from Stock Settings), if it still passes the safety
		 filters below — the explicit, operator-set answer, when one exists.
	  2. This company's own POS Profile.warehouse — the same source
		 ffl_integrations.fastbound.importer._default_warehouse and
		 ffl_core.api.firearm_intake._default_warehouse already prefer before
		 ever falling back to this function.
	  3. The naming convention every company in this system is bootstrapped
		 with: a warehouse literally named "Stores" (ERPNext's own default
		 warehouse set, created automatically with every new Company).
	  4. Last resort: the OLDEST matching warehouse (creation asc) —
		 deterministic, but NOT guaranteed correct on a site whose oldest
		 surviving warehouse isn't Stores, so this branch leaves a
		 frappe.log_error trail instead of silently guessing forever.
	"""
	# NOT `"warehouse_type": ("!=", "Transit")` in the filters below:
	# warehouse_type is a nullable Link, and this Frappe version's query
	# builder does NOT wrap nullable columns in ifnull() for a bare "!="
	# (nor "not in") comparison — an ordinary warehouse with warehouse_type
	# left blank (the common case; "Stores" itself is one) evaluates
	# `NULL <> 'Transit'` to SQL NULL, which is not TRUE, so the row is
	# silently excluded (confirmed empirically against this pinned Frappe
	# version — see docs/versions.md). Resolving the Transit set up front as
	# a plain Python list and checking membership in Python sidesteps the
	# nullable column entirely instead of trying to express the exclusion in
	# SQL.
	transit_warehouses = set(frappe.get_all(
		"Warehouse", filters={"company": company, "warehouse_type": "Transit"}, pluck="name"))

	# Customer-custody warehouses are excluded for the same reason consignment
	# ones are, and the cost of missing it runs both ways: custody holds property
	# the store does NOT own, so handing it back as "the" default receiving
	# warehouse would (a) route an ordinary purchase into a location every
	# sellable-stock query deliberately ignores — real inventory, bought and
	# paid for, invisible to the storefront and the POS pickers — and (b) mix
	# owned stock into the one warehouse whose whole job is to contain only
	# things that aren't ours. Resolved as a Python set for the same nullable-
	# column reason spelled out above.
	ineligible = transit_warehouses | custody_warehouse_names()

	def _eligible(name: str | None) -> bool:
		if not name or name in ineligible:
			return False
		return bool(frappe.db.exists("Warehouse", {
			"name": name, "company": company, "is_group": 0,
			"consignment_dealer": ("is", "not set"), "disabled": 0,
		}))

	configured = frappe.db.get_value("Company", company, "default_warehouse")
	if _eligible(configured):
		return configured

	# disabled:0 + an explicit order_by. A
	# company with several POS Profiles (one retired) otherwise let the table's
	# physical row order pick the answer — and a disabled profile is exactly
	# the one that must not.
	pos_wh = frappe.db.get_value(
		"POS Profile", {"company": company, "disabled": 0}, "warehouse",
		order_by="creation asc")
	if _eligible(pos_wh):
		return pos_wh

	conventional = frappe.db.get_value(
		"Warehouse", {
			"company": company, "is_group": 0, "warehouse_name": "Stores",
			"consignment_dealer": ("is", "not set"), "disabled": 0,
		}, "name")
	if _eligible(conventional):
		return conventional

	fallback_filters = {
		"company": company, "is_group": 0,
		"consignment_dealer": ("is", "not set"), "disabled": 0,
	}
	if ineligible:
		fallback_filters["name"] = ("not in", list(ineligible))
	fallback = frappe.db.get_value(
		"Warehouse", fallback_filters, "name", order_by="creation asc")
	if fallback:
		_log_heuristic_fallback(company, fallback)
	return fallback


def _fallback_log_cache_key(company: str) -> str:
	return f"ffl_core:default_stock_warehouse_fallback:{company}"


_FALLBACK_LOG_TTL_SECONDS = 3600


def _log_heuristic_fallback(company: str, fallback: str) -> None:
	"""default_stock_warehouse sits on the
	counter's hot path (every consignment return, every intake, every build
	order). Reaching the heuristic branch is a standing misconfiguration a
	human has to fix, not a transient failure — so it must be visible, but
	writing an Error Log on EVERY call turns one under-configured company
	into thousands of identical rows a day and buries the logs that matter.
	Once an hour per company says exactly the same thing to the operator.

	Deliberately keyed per company: two companies misconfigured for different
	reasons must each get their own row, and fixing one must not silence the
	other."""
	cache_key = _fallback_log_cache_key(company)
	if frappe.cache().get_value(cache_key):
		return
	frappe.cache().set_value(cache_key, 1, expires_in_sec=_FALLBACK_LOG_TTL_SECONDS)
	frappe.log_error(
		title="default_stock_warehouse: heuristic fallback used",
		message=(
			f"No Company.default_warehouse / POS Profile warehouse / "
			f'a warehouse named "Stores" resolved for company {company} — '
			f"falling back to the oldest non-group, non-consignment, "
			f"non-disabled, non-Transit warehouse ({fallback}). Configure "
			"the Company's Default Warehouse or a POS Profile warehouse "
			"for this company to make this deterministic. (This log is "
			"throttled to once an hour per company.)"
		),
	)


def is_consignment_warehouse(warehouse: str) -> bool:
	"""True when `warehouse` is a per-dealer outbound-consignment holding
	location (Warehouse.consignment_dealer is set). One query per call — for a
	LOOP over many warehouses, take consignment_warehouse_names() once instead."""
	if not warehouse:
		return False
	return bool(frappe.db.get_value("Warehouse", warehouse, "consignment_dealer"))


def consignment_warehouse_names() -> set[str]:
	"""Every per-dealer outbound-consignment holding warehouse, in ONE query —
	the set form of is_consignment_warehouse, for callers that would otherwise
	ask row by row (the serial picker asked once
	per candidate serial, up to 200 extra queries per render).

	`("is", "set")` rather than `!= ""`: consignment_dealer is a nullable Link,
	and this Frappe version does not wrap nullable columns in ifnull() for a
	bare comparison — the same trap documented at length in
	default_stock_warehouse above. Not company-scoped, matching
	is_consignment_warehouse's own semantics: a serial sitting in ANY company's
	consignment warehouse is out on consignment."""
	return set(frappe.get_all(
		"Warehouse", filters={"consignment_dealer": ("is", "set")}, pluck="name"))


def is_consigned_out(*, consigned_out, warehouse: str | None,
		names: set[str] | None = None) -> bool:
	"""Is this firearm out on consignment at a partner dealer?

	TWO independent keys, either alone disqualifying — the same shape (and the
	same reason) as ffl_core.custody's ownership rule:

	  * ``Serial No.consigned_out`` — the flag ship_consignment_out stamps as
	    its FIRST write (step 6a), before the Material Transfer and long before
	    the disposition is pushed to FastBound;
	  * the gun's location in a warehouse from consignment_warehouse_names().

	A flag and a location drift apart — a hand-run stock entry moves a gun
	without touching flags, a backfill sets flags without moving stock — and
	between those two writes there is a window where only the flag is set. Any
	caller that decides "may I take this gun out of stock?" has to consult both;
	the flag half is also what makes the answer race-free against FastBound's
	webhook echo (incident CONO-2026-01544).

	Pass `names` (from consignment_warehouse_names()) when looping, or leave it
	None for a single check. Written once here so a third consumer inherits the
	rule instead of re-deriving it — firearm._pos_picker_excluded_serials rule 3
	is the older in-place copy of the same predicate.
	"""
	if consigned_out:
		return True
	if not warehouse:
		return False
	if names is None:
		return is_consignment_warehouse(warehouse)
	return warehouse in names


def is_custody_warehouse(warehouse: str) -> bool:
	"""True when `warehouse` holds customer property the store possesses but
	does not own (transfer-in guns awaiting pickup; later repair/layaway).
	One query per call — for a LOOP, take custody_warehouse_names() once."""
	if not warehouse:
		return False
	return bool(frappe.db.get_value("Warehouse", warehouse, "is_custody_warehouse"))


def custody_warehouse_names() -> set[str]:
	"""Every customer-custody warehouse, in ONE query — the set form of
	is_custody_warehouse, for callers that would otherwise ask row by row
	(the sellable-stock count asks per warehouse; the POS pickers per serial).

	Not company-scoped, matching is_custody_warehouse and its consignment
	sibling: a gun sitting in ANY company's custody warehouse is not ours to
	sell. Tolerates the column being absent — this is read by
	default_stock_warehouse, which runs on the counter's hot path and must not
	start throwing on a site that has not migrated yet."""
	if not frappe.db.has_column("Warehouse", "is_custody_warehouse"):
		return set()
	# Raw SQL rather than frappe.get_all, for two reasons that point the same way
	# (1) get_all drags in the permission + meta machinery, which on
	# a COLD meta cache calls frappe.db.get_value — and several long-standing
	# suites mock that wholesale, so this lookup came back holding their fixture
	# rows and poisoned Frappe's own Meta object. (2) This sits on the counter's
	# hot path (every default-warehouse resolution, every POS picker render, the
	# Create Order scan gate); a one-column indexed read is the cheapest honest
	# answer, and it is why this set is deliberately NOT memoised — see n-3.
	rows = frappe.db.sql(
		"SELECT name FROM `tabWarehouse` WHERE COALESCE(is_custody_warehouse, 0) = 1")
	return {r[0] for r in rows}


def ensure_custody_warehouse(company: str) -> str:
	"""Idempotent. Returns the customer-custody Warehouse for `company`,
	creating it if missing.

	Naming: "Customer Custody" (ERPNext auto-appends " - {company abbr}" on
	insert, matching every other Warehouse in this system). Non-group, parented
	under the company's Warehouse root group. Looked up BY FLAG, not by name,
	so a site that renamed the warehouse gets its existing one back instead of
	silently growing a second custody location.

	Deliberately does NOT set `account`: stock here is always valued at 0 (the
	receive forces rate 0), so there is no balance to segregate — and
	Warehouse.account is a one-way door once Stock Ledger Entries exist (see
	ensure_consignment_warehouse). Should custody ever need its own account,
	that is a fresh warehouse, not a repoint of this one."""
	existing = frappe.db.get_value(
		"Warehouse", {"is_custody_warehouse": 1, "company": company}, "name")
	if existing:
		return existing

	from ffl_core.setup.custody import CUSTODY_WAREHOUSE_NAME

	parent = frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")},
		"name",
		order_by="lft",
	)
	doc = frappe.get_doc({
		"doctype": "Warehouse",
		"warehouse_name": CUSTODY_WAREHOUSE_NAME,
		"company": company,
		"parent_warehouse": parent,
		"is_group": 0,
		"is_custody_warehouse": 1,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_consignment_warehouse(dealer: str, company: str) -> str:
	"""Idempotent. Returns the per-dealer outbound-consignment Warehouse for
	`dealer` (a Customer docname), creating it if missing.

	Naming: "Consignment Out - {dealer}" (ERPNext auto-appends " - {company
	abbr}" on insert, matching every other Warehouse in this system).
	Non-group, parented under the company's Warehouse root group ("All
	Warehouses - {abbr}"). `account` is set ONCE at creation to the
	company's "Stock at Consignee" asset account
	(stock_at_consignee_account below) —
	this is a deliberate ONE-WAY DOOR: Warehouse.account can't be
	repointed after the warehouse has Stock Ledger Entries without a full
	repost, so this function never touches `account` on a warehouse that
	already exists (the `if existing: return` guard above keeps it that
	way — it does not re-assert or update anything on an existing row).
	"""
	existing = frappe.db.get_value(
		"Warehouse", {"consignment_dealer": dealer, "company": company}, "name")
	if existing:
		return existing

	# order_by pins this deterministic (matches
	# ensure_stock_at_consignee_account's own root-group lookup) — a company normally has exactly one un-parented
	# root group ("All Warehouses - {abbr}"), so this rarely matters in
	# practice, but an unordered query has no guaranteed result if that
	# ever isn't true.
	parent = frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")},
		"name",
		order_by="lft",
	)
	doc = frappe.get_doc({
		"doctype": "Warehouse",
		"warehouse_name": f"Consignment Out - {dealer}",
		"company": company,
		"parent_warehouse": parent,
		"is_group": 0,
		"consignment_dealer": dealer,
		"account": stock_at_consignee_account(company),
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


# --- Stock at Consignee: the asset account behind every consignment warehouse
STOCK_AT_CONSIGNEE_TITLE = "Stock at Consignee"


def stock_at_consignee_account(company: str) -> str | None:
	"""The company's Stock at Consignee current-asset account, if created."""
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_name": STOCK_AT_CONSIGNEE_TITLE, "is_group": 0},
		"name",
	)


def ensure_stock_at_consignee_account(company: str) -> bool:
	"""Idempotent. The per-dealer consignment warehouse's ``account`` points
	here, so the balance sheet carries "guns physically at a partner FFL" as
	its own line instead of blending into Stock In Hand (a Material Transfer
	into that warehouse is Dr this / Cr Stock In Hand — net zero P&L).
	Parented under "Stock Assets" when present, then "Current Assets", then
	any Asset group. Returns True when the account was created; False on a
	chartless company (mid-setup — retried lazily by the next caller)."""
	if stock_at_consignee_account(company):
		return False
	parent = (
		frappe.db.get_value(
			"Account",
			{"company": company, "root_type": "Asset", "is_group": 1,
				"account_name": "Stock Assets"},
		)
		or frappe.db.get_value(
			"Account",
			{"company": company, "root_type": "Asset", "is_group": 1,
				"account_name": "Current Assets"},
		)
		or frappe.db.get_value(
			"Account",
			{"company": company, "root_type": "Asset", "is_group": 1},
			order_by="lft",
		)
	)
	if not parent:
		return False
	frappe.get_doc({
		"doctype": "Account",
		"account_name": STOCK_AT_CONSIGNEE_TITLE,
		"parent_account": parent,
		"company": company,
		"root_type": "Asset",
		"is_group": 0,
		"account_type": "Stock",
	}).insert(ignore_permissions=True)
	return True


def ensure_stock_at_consignee_accounts() -> int:
	"""after_migrate: one account per company. Never raises (a thrown hook
	aborts the whole migrate); failures are logged and retried next time."""
	created = 0
	try:
		for company in frappe.get_all("Company", pluck="name"):
			if ensure_stock_at_consignee_account(company):
				created += 1
	except Exception:
		frappe.log_error(title="ensure_stock_at_consignee_accounts failed", message=frappe.get_traceback())
	return created
