"""Test/CI bootstrap — runs the Frappe setup wizard programmatically.

Frappe's stock `before_tests` hook short-circuits when more than one app is
installed (it only auto-runs the wizard for vanilla Frappe sites). With
ERPNext + our three custom apps installed, the wizard never fires, so
ERPNext's after-setup hooks never seed Fiscal Year, Stock Entry Type,
Warehouse Type, Mode of Payment defaults, chart-of-accounts, default
Customer Group / Territory — and every test that touches Stock Entry or
Sales Invoice errors out on a missing master.

Invoked from `.github/workflows/ci.yml` and locally via
`bench execute ffl_core.setup.test_bootstrap.run`.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit
from ffl_core.setup.dev_site import (
	COMPANY,
	COMPANY_ABBR,
	COUNTRY,
	CURRENCY,
	_firearm_item_groups,
	_stock_settings,
	company_address,
)


GUN_SHOP_TILE = "Gun Shop"


def run() -> dict:
	"""Idempotent. Returns a small status dict."""
	# frappe/locale.py:get_locale_value reads frappe.local.lang and raises
	# UnboundLocalError if it's None. setup_complete and friends hit that
	# path early; pre-seed before invoking them.
	if not getattr(frappe.local, "lang", None):
		frappe.local.lang = "en"

	if not frappe.is_setup_complete():
		_run_setup_wizard()

	_firearm_item_groups()
	_stock_settings()
	_ensure_fiscal_years()
	company_address()
	_desktop_tile()
	_run_after_migrate_hooks()
	_fastbound_test_settings()
	_fastbound_reconcile_test_masters()
	commit()

	return {
		"setup_complete": bool(frappe.is_setup_complete()),
		"company": COMPANY,
		"firearm_groups": True,
		"desktop_tile": True,
	}


def run_template_stage() -> dict:
	"""Template half of the CI bootstrap — everything that needs only
	frappe+erpnext (setup wizard, stock settings, firearm item groups), run
	once on the erpnext-only site that gets dumped as the self-hosted CI site
	template. `run()` finishes the rest (desktop tile, after_migrate ensures)
	after our apps are installed on the restored copy — on an erpnext-only
	site those would either fail (Workspace links to our doctypes) or no-op
	(get_hooks only sees installed apps). Idempotent."""
	if not getattr(frappe.local, "lang", None):
		frappe.local.lang = "en"
	if not frappe.is_setup_complete():
		_run_setup_wizard()
	_firearm_item_groups()
	_stock_settings()
	commit()
	return {"template_stage": "complete"}


def _run_after_migrate_hooks() -> None:
	"""A fresh CI site never runs `bench migrate`, so the after_migrate
	ensures (consignment/payments/tax/… custom fields and masters) that prod
	gets on every deploy would not exist here — leaving it to filesystem-
	dependent test order whether some test module happened to create them
	lazily first (it didn't on the self-hosted runner: Unknown column
	'is_consignment'). Run them explicitly so the site starts in the same
	state a freshly migrated prod site is in.

	ffl_core ONLY: its ensures are all documented idempotent + never-raise.
	ffl_integrations' are not CI-safe (payroc's ensure wires the stock
	"Credit Card" MoP into POS Profiles, whose save then fails validation
	because that MoP has no account on a CI site) — and CI never ran them
	before, so nothing in the suites depends on them."""
	for method in frappe.get_hooks("after_migrate", app_name="ffl_core") or []:
		frappe.get_attr(method)()


def _fastbound_test_settings() -> None:
	"""FastBound Settings' account_num/api_key are mandatory whenever the
	integration is enabled (mandatory_depends_on), and have no default. A fresh
	site IS enabled — the switch defaults on — and has neither credential, so any
	test touching FastBound Settings (get_single, .save(), or
	ffl_integrations.fastbound.importer's
	FastBoundClient.from_settings) fails on MandatoryError before it even
	reaches the mocked HTTP client. ffl_integrations' own tests never send a
	real request (FastBoundClient is always mocked in the test suite), so
	these are inert placeholders, not real credentials — mirrors the guard
	test_fastbound_importer.py's own setUpClass already had for
	account_num/default_audit_user (that guard alone was never enough: it
	never touched api_key, so it only worked by accident on a site that
	already had one set from an earlier manual run).

	Idempotent: does nothing once real (or previously-seeded) values exist.
	No-op if ffl_integrations isn't installed on this site.

	Gated to test/dev contexts only. This module
	is reachable via `bench execute ffl_core.setup.test_bootstrap.run`
	outside CI too — without this guard, a "real account_num set, api_key
	not yet set" half-configured site (mid key-rotation, say) would get a
	fake key silently written on top of a genuinely prod-bound Settings
	record. FastBound Settings itself lives in ffl_integrations, not this
	app (ffl_core) — read/written here by doctype-name string only, so this
	module stays usable even when ffl_integrations isn't installed (the
	frappe.db.exists("DocType", ...) check above already assumes that)."""
	if not (frappe.flags.in_test or frappe.conf.developer_mode):
		return
	if not frappe.db.exists("DocType", "FastBound Settings"):
		return
	s = frappe.get_single("FastBound Settings")
	if s.account_num and s.get_password("api_key", raise_exception=False):
		return
	s.account_num = s.account_num or "TEST-ACCOUNT"
	if not s.get_password("api_key", raise_exception=False):
		s.api_key = "TEST-API-KEY-PLACEHOLDER"
	s.default_audit_user = s.default_audit_user or "test@gunshop.local"
	s.flags.ignore_permissions = True
	s.save(ignore_permissions=True)


def _fastbound_reconcile_test_masters() -> None:
	"""Brand "Colt" and Caliber "45ACP" are hardcoded sample values the
	ffl_integrations reconcile fixtures (test_fastbound_reconcile.py) build
	Serial No rows against. Serial No.manufacturer is a custom Link field
	LABELLED "Manufacturer" but pointing at ERPNext's *Brand* doctype (ffl_core
	custom_field "Serial No-manufacturer", options=Brand — the "Could not find
	Manufacturer: Colt" LinkValidationError uses the field label, not the link
	target), and Serial No.caliber a Link to ffl_core's own Caliber doctype.
	Neither master exists on a fresh site (nothing in the normal intake/import
	flow requires these TWO specific values in particular; production callers
	auto-vivify whatever manufacturer/caliber they actually see, e.g.
	ffl_core.firearm.ensure_brand / ensure_caliber). Seeding them here means
	the CI bench-execute bootstrap doesn't silently depend on a long-lived dev
	site having accumulated a Brand "Colt" from earlier real intake.

	The reconcile tests ALSO self-vivify these in-transaction (see
	test_fastbound_reconcile._ensure_link_masters), so this cross-process seed is
	belt-and-suspenders — but keep it correct: seeding a *Manufacturer* "Colt"
	(the previous bug) never satisfied the Brand link and left the fresh-site
	suite red under `|| true`.

	Idempotent. No-op if ERPNext (for Brand) / ffl_core (for Caliber) isn't
	installed on this site."""
	if frappe.db.exists("DocType", "Brand") and not frappe.db.exists("Brand", "Colt"):
		frappe.get_doc({"doctype": "Brand", "brand": "Colt"}).insert(ignore_permissions=True)
	if frappe.db.exists("DocType", "Caliber") and not frappe.db.exists("Caliber", "45ACP"):
		frappe.get_doc({
			"doctype": "Caliber", "caliber_name": "45ACP", "category": "Pistol",
		}).insert(ignore_permissions=True)


def _ensure_fiscal_years() -> None:
	"""ERPNext dates every ledger-affecting submit (Stock Entry, Sales
	Invoice, Journal Entry, Payment Entry, ...) against a Fiscal Year — with
	none on file, EVERY such submit throws FiscalYearError, not just a
	consignment-specific one (test_dealer_transfer.py and other pre-existing
	suites hit this identically; confirmed live on the shared dev site
	2026-07-10, zero Fiscal Year rows present). _run_setup_wizard (above)
	only creates the single year covering the moment the site was FIRST
	stood up (its fy_start_date/fy_end_date are hardcoded to "2026"), is
	gated behind `not frappe.is_setup_complete()`, and never runs again — a
	long-lived dev site outlives that one year as real time moves on, and
	nothing else ever seeds the next one.

	Unconditional (not gated behind is_setup_complete — a site whose wizard
	ran long ago still needs this backfilled) and idempotent: seeds a wide,
	company-unrestricted span (leaving Fiscal Year.companies empty applies a
	fiscal year to every company — erpnext.accounts.utils._get_fiscal_years)
	so this doesn't recur every time a session crosses a year boundary,
	skipping any year that already exists."""
	from frappe.utils import getdate

	current_year = getdate(frappe.utils.nowdate()).year
	for year in range(current_year - 2, current_year + 10):
		name = str(year)
		if frappe.db.exists("Fiscal Year", name):
			continue
		frappe.get_doc({
			"doctype": "Fiscal Year",
			"year": name,
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
		}).insert(ignore_permissions=True)


def _desktop_tile() -> None:
	"""Idempotent: pin Gun Shop's Workspace.parent_page (raw DB writes can bypass the field).

	The desk navigation itself is the Sidebar ffl_core ships (setup/sidebar.py); Frappe 16.50
	no longer reads the Workspace Sidebar / Desktop Icon pair this used to seed as a fallback.
	"""
	if frappe.db.exists("Workspace", GUN_SHOP_TILE):
		current = frappe.db.get_value("Workspace", GUN_SHOP_TILE, "parent_page") or ""
		if current != GUN_SHOP_TILE:
			frappe.db.set_value(
				"Workspace", GUN_SHOP_TILE, "parent_page", GUN_SHOP_TILE, update_modified=False
			)

	from frappe.desk.doctype.desktop_icon.desktop_icon import clear_desktop_icons_cache
	clear_desktop_icons_cache()


def _run_setup_wizard() -> None:
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	setup_complete({
		"language": "English",
		"country": COUNTRY,
		"timezone": "America/New_York",
		"currency": CURRENCY,
		"full_name": "Test Admin",
		"email": "test@gunshop.local",
		"password": "test",
		"company_name": COMPANY,
		"company_abbr": COMPANY_ABBR,
		"fy_start_date": "2026-01-01",
		"fy_end_date": "2026-12-31",
		"chart_of_accounts": "Standard",
		"domains": ["Retail"],
	})
