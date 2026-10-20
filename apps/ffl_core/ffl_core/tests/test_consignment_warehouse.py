"""The default-stock-warehouse fallback must never hand back a
per-dealer outbound-consignment warehouse.

Three call sites picked "the company's first non-group
Warehouse" with no exclusion and no deterministic ordering. Once a
consignment warehouse exists, an unluckily-ordered query could return IT as
"the" default sale/receiving warehouse for a normal counter transaction —
silently routing stock into (or out of) a warehouse that must hold only
consigned-out guns. This module pins the fix (ffl_core/warehouses.py) at
both layers: the new shared resolver, and each of the three call sites that
now delegate to it.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

DEALER = "_Test Consignment Out Warehouse Dealer"


class ConsignmentWarehouseFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.stores = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")
		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
				"is_ffl_dealer": 1,
			}).insert(ignore_permissions=True)

	def _blank_pos_profile_warehouse(self) -> None:
		"""Force the POS-Profile-first branch to miss, so
		firearm_intake._default_warehouse / importer._default_warehouse
		actually exercise their fallback (the thing under test) instead of
		short-circuiting on the profile's own configured warehouse. Runs
		inside this test's own transaction/savepoint — invisible to any
		other test."""
		pos = frappe.db.get_value("POS Profile", {"company": self.company}, "name")
		frappe.db.set_value("POS Profile", pos, "warehouse", "", update_modified=False)


class DefaultStockWarehouseExcludesConsignment(ConsignmentWarehouseFixtures):
	def test_ensure_consignment_warehouse_is_idempotent_and_flagged(self):
		from ffl_core.warehouses import ensure_consignment_warehouse, is_consignment_warehouse

		wh1 = ensure_consignment_warehouse(DEALER, self.company)
		wh2 = ensure_consignment_warehouse(DEALER, self.company)
		self.assertEqual(wh1, wh2, "re-running must return the SAME warehouse, not create a second one")
		self.assertTrue(is_consignment_warehouse(wh1))
		self.assertFalse(is_consignment_warehouse(self.stores))
		self.assertEqual(
			frappe.db.get_value("Warehouse", wh1, "consignment_dealer"), DEALER)

	def test_consignment_warehouse_gets_stock_at_consignee_account(self):
		from ffl_core.warehouses import stock_at_consignee_account
		from ffl_core.warehouses import ensure_consignment_warehouse

		wh = ensure_consignment_warehouse(DEALER, self.company)
		self.assertEqual(
			frappe.db.get_value("Warehouse", wh, "account"),
			stock_at_consignee_account(self.company))

	def test_default_stock_warehouse_never_returns_a_consignment_warehouse(self):
		"""The real invariant, true on ANY site
		regardless of how many other warehouses (or test suites' leftover
		fixture warehouses) happen to sort earlier by creation date. `==
		Stores` bakes an environmental assumption into the assertion (this
		exact assumption is what made 4 of this file's tests flaky on the
		shared persistent dev DB before the fix); this does not."""
		from ffl_core.warehouses import default_stock_warehouse, ensure_consignment_warehouse, \
			is_consignment_warehouse

		ensure_consignment_warehouse(DEALER, self.company)
		result = default_stock_warehouse(self.company)
		self.assertIsNotNone(result)
		self.assertFalse(is_consignment_warehouse(result))
		self.assertEqual(frappe.db.get_value("Warehouse", result, "disabled"), 0)

	def test_default_stock_warehouse_skips_a_disabled_older_warehouse(self):
		"""A warehouse older than Stores (e.g. a long-retired
		one) that later got disabled must never be handed back: ERPNext hard-
		rejects a transaction into a disabled warehouse, so this used to mean
		"counter can't open a sale" until someone noticed and fixed the data
		by hand."""
		from ffl_core.warehouses import default_stock_warehouse

		stale = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "_Test M6 Stale WH"}, "name")
		if not stale:
			stale = frappe.get_doc({
				"doctype": "Warehouse", "warehouse_name": "_Test M6 Stale WH",
				"company": self.company,
			}).insert(ignore_permissions=True).name
		frappe.db.set_value("Warehouse", stale, "disabled", 1, update_modified=False)
		try:
			result = default_stock_warehouse(self.company)
			self.assertNotEqual(result, stale)
			self.assertEqual(result, self.stores)
		finally:
			frappe.db.set_value("Warehouse", stale, "disabled", 0, update_modified=False)

	def test_default_stock_warehouse_skips_a_transit_warehouse(self):
		"""The `warehouse_type != "Transit"` exclusion — ERPNext's own
		"Goods In Transit" default warehouse is exactly this shape (non-group,
		not disabled, not a consignment warehouse) and must never be handed
		back as the counter's default."""
		from ffl_core.warehouses import default_stock_warehouse

		transit = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Goods In Transit"}, "name")
		self.assertTrue(transit, "ERPNext's default Goods In Transit warehouse must exist")
		result = default_stock_warehouse(self.company)
		self.assertNotEqual(result, transit)

	def test_store_warehouse_still_returns_stores(self):
		"""manual_order._store_warehouse has NO POS Profile fallback of its
		own — it IS the fallback (the highest-risk call site)."""
		from ffl_core.api.manual_order import _store_warehouse
		from ffl_core.warehouses import ensure_consignment_warehouse

		ensure_consignment_warehouse(DEALER, self.company)
		self.assertEqual(_store_warehouse(self.company), self.stores)

	def test_firearm_intake_default_warehouse_still_returns_stores(self):
		from ffl_core.api.firearm_intake import _default_warehouse
		from ffl_core.warehouses import ensure_consignment_warehouse

		ensure_consignment_warehouse(DEALER, self.company)
		self._blank_pos_profile_warehouse()
		self.assertEqual(_default_warehouse(self.company), self.stores)

	def test_fastbound_importer_default_warehouse_still_returns_stores(self):
		from ffl_integrations.fastbound.importer import _default_warehouse
		from ffl_core.warehouses import ensure_consignment_warehouse

		ensure_consignment_warehouse(DEALER, self.company)
		self._blank_pos_profile_warehouse()
		self.assertEqual(_default_warehouse(self.company), self.stores)


class DefaultStockWarehouseFixtures(FrappeTestCase):
	"""Fixtures only, deliberately test-free: subclassing a TestCase that
	already carries tests silently re-runs every one of them under the
	subclass's name."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()

	def _fresh_company(self, suffix: str) -> str:
		name = f"_Test M6 Fresh Co {suffix}"
		if not frappe.db.exists("Company", {"company_name": name}):
			frappe.get_doc({
				"doctype": "Company", "company_name": name, "abbr": f"M6{suffix}",
				"default_currency": "USD", "country": "United States",
			}).insert(ignore_permissions=True)
		return frappe.db.get_value("Company", {"company_name": name}, "name")

	def _older_eligible_warehouse(self, company: str, label: str) -> str:
		"""An otherwise-perfectly-eligible (non-group, not disabled, not a
		consignment warehouse, not Transit) warehouse FORCED older than
		"Stores" by backdating `creation` — Company.insert() auto-creates
		Stores/WIP/Finished Goods/Goods In Transit as part of the SAME
		transaction, so anything this helper creates afterwards is naturally
		YOUNGER than Stores unless explicitly backdated. Used ONLY by the
		naming-convention test below, to prove Stores wins there because of
		its NAME, not because it happens to be the oldest."""
		name = self._plain_eligible_warehouse(company, label)
		stores_creation = frappe.db.get_value(
			"Warehouse", {"company": company, "warehouse_name": "Stores"}, "creation")
		frappe.db.set_value(
			"Warehouse", name, "creation",
			frappe.utils.add_to_date(stores_creation, seconds=-60), update_modified=False)
		return name

	def _plain_eligible_warehouse(self, company: str, label: str) -> str:
		"""An otherwise-perfectly-eligible warehouse with ITS NATURAL (i.e.
		YOUNGER than Stores) creation time — deliberately NOT backdated. Used
		by the Stock-Settings/POS-Profile priority tests: those tiers must
		win regardless of age, so leaving this one younger than Stores means
		the OLD "oldest wins" heuristic would return Stores (not this
		warehouse) and correctly fail the assertion — backdating it here
		would let the old heuristic "accidentally" pick the right answer for
		the wrong reason (creation order, not reading Stock Settings/POS
		Profile) and silently defeat the RED half of these tests."""
		root = frappe.db.get_value(
			"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")},
			"name")
		return frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": label,
			"company": company, "parent_warehouse": root,
		}).insert(ignore_permissions=True).name

	def _clear_fallback_log_throttle(self, company: str) -> None:
		"""The heuristic-fallback Error Log is throttled to once an hour
		per company, and the cache backing that lives in Redis — i.e. it
		survives the test transaction rollback and the whole test RUN. Any
		test that asserts on the log must clear the key first, or it passes
		once and then fails for an hour."""
		from ffl_core.warehouses import _fallback_log_cache_key

		frappe.cache().delete_value(_fallback_log_cache_key(company))

	def _ensure_pos_profile(self, company: str, warehouse: str, name: str | None = None) -> str:
		"""Mirrors setup/dev_site.py's own _pos_profile() — v16's POS Profile
		validate wants a payment method + write-off account/cost center +
		income/expense accounts, none of which auto-populate."""
		name = name or company
		if frappe.db.exists("POS Profile", name):
			frappe.db.set_value("POS Profile", name, "warehouse", warehouse, update_modified=False)
			return name
		if not frappe.db.exists("Mode of Payment", "Cash"):
			frappe.get_doc({
				"doctype": "Mode of Payment", "mode_of_payment": "Cash",
				"type": "Cash", "enabled": 1,
			}).insert(ignore_permissions=True)
		mop = frappe.get_doc("Mode of Payment", "Cash")
		if not any(row.company == company for row in mop.accounts):
			cash_account = frappe.db.get_value(
				"Account", {"company": company, "account_name": ("like", "Cash%"), "is_group": 0})
			if cash_account:
				mop.append("accounts", {"company": company, "default_account": cash_account})
				mop.save(ignore_permissions=True)
		write_off_account = frappe.db.get_value("Company", company, "write_off_account") \
			or frappe.db.get_value(
				"Account", {"company": company, "account_name": ("like", "Write Off%"), "is_group": 0})
		cost_center = frappe.db.get_value("Company", company, "cost_center") \
			or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0})
		frappe.get_doc({
			"doctype": "POS Profile",
			"name": name,
			"company": company,
			"warehouse": warehouse,
			"currency": "USD",
			"selling_price_list": "Standard Selling",
			"write_off_account": write_off_account,
			"write_off_cost_center": cost_center,
			"income_account": frappe.db.get_value(
				"Account", {"company": company, "account_type": "Income Account", "is_group": 0}),
			"expense_account": frappe.db.get_value(
				"Account", {"company": company, "account_type": "Expense Account", "is_group": 0}),
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
		}).insert(ignore_permissions=True)
		return name


class DefaultStockWarehouseResolutionOrder(DefaultStockWarehouseFixtures):
	"""The `== Stores` assertion belongs here, on a
	freshly-created, single-purpose Company this test class alone controls,
	NOT on the shared `self.company` every other test file in this suite
	also writes warehouses into. Proves the actual resolution order (Stock
	Settings.default_warehouse -> this company's POS Profile.warehouse ->
	the "Stores" naming convention -> oldest-match fallback + log_error),
	not just its accidental agreement with a polluted site's creation
	order."""

	def test_resolves_to_stores_by_naming_convention_on_a_fresh_company(self):
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("A")
		self._older_eligible_warehouse(company, "Older Than Stores A")
		stores = frappe.db.get_value(
			"Warehouse", {"company": company, "warehouse_name": "Stores"}, "name")
		self.assertTrue(stores, "ERPNext must have auto-created a Stores warehouse for a new Company")
		self.assertEqual(default_stock_warehouse(company), stores)

	def test_company_default_warehouse_wins_over_naming_convention(self):
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("B")
		configured = self._plain_eligible_warehouse(company, "Configured Default B")
		prev = frappe.db.get_value("Company", company, "default_warehouse")
		frappe.db.set_value("Company", company, "default_warehouse", configured)
		try:
			self.assertEqual(default_stock_warehouse(company), configured)
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)

	def test_pos_profile_warehouse_wins_over_naming_convention_when_company_default_unset(self):
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("C")
		configured = self._plain_eligible_warehouse(company, "POS Profile Default C")
		prev = frappe.db.get_value("Company", company, "default_warehouse")
		frappe.db.set_value("Company", company, "default_warehouse", None)
		try:
			self._ensure_pos_profile(company, configured)
			self.assertEqual(default_stock_warehouse(company), configured)
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)

	def test_falls_back_to_oldest_match_and_logs_when_nothing_else_resolves(self):
		"""No Company default warehouse, no POS Profile warehouse, and Stores
		itself renamed away — the last-resort heuristic still returns SOME
		safe (non-consignment/non-disabled/non-Transit) warehouse rather than
		None, and leaves a log_error trail so an operator notices the site is
		under-configured."""
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("D")
		stores = frappe.db.get_value(
			"Warehouse", {"company": company, "warehouse_name": "Stores"}, "name")
		frappe.db.set_value("Warehouse", stores, "warehouse_name", "Not Stores Anymore")
		prev = frappe.db.get_value("Company", company, "default_warehouse")
		frappe.db.set_value("Company", company, "default_warehouse", None)
		self._clear_fallback_log_throttle(company)
		error_count_before = frappe.db.count("Error Log")
		try:
			result = default_stock_warehouse(company)
			self.assertIsNotNone(result)
			self.assertEqual(
				frappe.db.get_value("Warehouse", result, "company"), company)
			self.assertGreater(
				frappe.db.count("Error Log"), error_count_before,
				"the heuristic fallback must log_error so it doesn't fail silently forever")
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)
			frappe.db.set_value("Warehouse", stores, "warehouse_name", "Stores")


class DefaultStockWarehouseHotPathHygiene(DefaultStockWarehouseFixtures):
	"""default_stock_warehouse runs on the
	counter's hot path, and two details of it were left to chance: WHICH POS
	Profile answers when a company has more than one, and how loudly the
	last-resort branch complains."""

	def _unconfigure(self, company: str):
		prev = frappe.db.get_value("Company", company, "default_warehouse")
		frappe.db.set_value("Company", company, "default_warehouse", None)
		return prev

	def test_a_disabled_pos_profile_never_answers(self):
		"""A retired POS Profile still carries the warehouse it used to ship
		from. Table order decided whether that answer won."""
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("E")
		retired_wh = self._plain_eligible_warehouse(company, "Retired POS Warehouse E")
		stores = frappe.db.get_value(
			"Warehouse", {"company": company, "warehouse_name": "Stores"}, "name")
		prev = self._unconfigure(company)
		try:
			profile = self._ensure_pos_profile(company, retired_wh)
			frappe.db.set_value("POS Profile", profile, "disabled", 1, update_modified=False)
			# Tier 2 must decline, so tier 3 (the "Stores" naming convention) answers.
			self.assertEqual(default_stock_warehouse(company), stores)
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)

	def test_the_oldest_enabled_pos_profile_wins_deterministically(self):
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("H")
		first_wh = self._plain_eligible_warehouse(company, "First POS Warehouse H")
		second_wh = self._plain_eligible_warehouse(company, "Second POS Warehouse H")
		prev = self._unconfigure(company)
		try:
			older = self._ensure_pos_profile(company, first_wh, name=f"{company} Older")
			self._ensure_pos_profile(company, second_wh, name=f"{company} Newer")
			# Force an unambiguous creation order regardless of insert timing.
			frappe.db.set_value(
				"POS Profile", older, "creation",
				frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-30),
				update_modified=False)
			self.assertEqual(default_stock_warehouse(company), first_wh)
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)

	def test_the_fallback_error_log_is_throttled_per_company(self):
		"""Reaching the heuristic branch is a standing misconfiguration, not a
		transient failure: it must be visible once, not once per counter
		lookup. And silencing one company must not silence another."""
		from ffl_core.warehouses import default_stock_warehouse

		company = self._fresh_company("F")
		other = self._fresh_company("G")
		renamed = []
		prev = frappe.db.get_value("Company", company, "default_warehouse")
		frappe.db.set_value("Company", company, "default_warehouse", None)
		try:
			for co in (company, other):
				stores = frappe.db.get_value(
					"Warehouse", {"company": co, "warehouse_name": "Stores"}, "name")
				frappe.db.set_value("Warehouse", stores, "warehouse_name", "Not Stores Anymore")
				# A new Company gets Stores as its Default Warehouse (ERPNext 16.51); renaming
				# keeps the docname, so clear it or tier 1 would still answer.
				frappe.db.set_value("Company", co, "default_warehouse", None)
				renamed.append(stores)
				self._clear_fallback_log_throttle(co)

			before = frappe.db.count("Error Log")
			self.assertIsNotNone(default_stock_warehouse(company))
			after_first = frappe.db.count("Error Log")
			self.assertEqual(after_first, before + 1, "the first fallback must log")

			# The counter hits this path over and over; the log must not.
			for _ in range(3):
				default_stock_warehouse(company)
			self.assertEqual(
				frappe.db.count("Error Log"), after_first,
				"repeat fallbacks for the same company must be throttled")

			# ... but a DIFFERENT misconfigured company still gets its own row.
			self.assertIsNotNone(default_stock_warehouse(other))
			self.assertEqual(
				frappe.db.count("Error Log"), after_first + 1,
				"the throttle is per company, not global")
		finally:
			frappe.db.set_value("Company", company, "default_warehouse", prev)
			for stores in renamed:
				frappe.db.set_value("Warehouse", stores, "warehouse_name", "Stores")
