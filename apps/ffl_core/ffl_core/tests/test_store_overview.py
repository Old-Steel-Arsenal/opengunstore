"""The Gun Shop Overview page (page store-overview, ffl_core.api.store_overview) and the landing.

What is pinned here:

* each block is its source's own numbers: the sales tiles are the Sales Report's Net Sales and row
  count for the day, "Not logged" is the set reconcile_failed_pushes retries (plus the ones it
  leaves to a person), less the last 15 minutes;
* each block answers to its own permission, and one that fails costs only itself;
* All records groups by what the store has switched on, and keeps only what the user may open;
* every way into the Gun Shop lands on the Overview, and the mirrored extension sidebars follow
  the new first row after a migrate.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, getdate, now_datetime, nowdate

from ffl_core.api import gun_shop_cards
from ffl_core.api import store_overview as so
from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.setup import gunshop_sidebar as gs
from ffl_core.tests.test_extensions import ExtensionFixture
from ffl_core.tests.test_gun_shop_nav import FAKE_ROWS, _drop_site_layer, _fake_sidebar_hook, _rendered, _switches, _user

SALES_USER = "overview-sales@example.com"
STOCK_USER = "overview-stock@example.com"
BLOCKS = ("sales", "open_4473", "pickups", "stock", "drawer", "ad", "arriving", "records")


def _integrations() -> bool:
	return "ffl_integrations" in frappe.get_installed_apps()


@contextmanager
def _as(user):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


class OverviewCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		gun_shop_cards.clear_cache()
		self.addCleanup(gun_shop_cards.clear_cache)
		frappe.set_user("Administrator")


# ------------------------------------------------------------------ sales

def _fake_sales(headers):
	"""The Sales Report reading ``headers`` instead of the database (lines: one $net line each)."""
	def fetch(filters, extended=False):
		lo, hi = getdate(filters.from_date), getdate(filters.to_date)
		return [dict(h) for h in headers if lo <= getdate(h["posting_date"]) <= hi]

	def lines(hs, extended=False):
		return {h["name"]: [frappe._dict({"item_code": "OVERVIEW-NO-ITEM", "qty": 1, "base_net_amount": h["base_net_total"],
			"serial_no": None})] for h in hs}

	return [patch.object(sr, "_fetch_headers", side_effect=fetch), patch.object(sr, "_fetch_lines", side_effect=lines)]


def _hdr(name, day, net, voucher_type="POS Invoice"):
	return {"name": name, "posting_date": day, "customer": "C", "company": "X", "base_net_total": net, "grand_total": net,
		"rounded_total": net, "is_return": 1 if net < 0 else 0, "voucher_type": voucher_type,
		"channel": "POS" if voucher_type == "POS Invoice" else "Order", "woo_online_order": None, "gunbroker_order": None,
		"po_no": None, "is_ffl_transfer": None}


class TestSales(OverviewCase):
	def test_today_is_the_sales_reports_net_sales_and_row_count_for_today(self):
		today = getdate(nowdate())
		headers = [
			_hdr("OV-T1", today, 120.0), _hdr("OV-T2", today, 80.5, "Sales Invoice"), _hdr("OV-T3", today, -20.0),
			_hdr("OV-Y1", add_days(today, -1), 999.0), _hdr("OV-W1", add_days(today, -7), 150.0),
			_hdr("OV-OLD", add_days(today, -8), 5000.0),
		]
		patches = _fake_sales(headers)
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		days = so.get_overview()["sales"]["days"]
		report = sr.execute({"from_date": today, "to_date": today, "view": "Order"})[1]
		body = [r for r in report if not r.get("is_total")]
		total = next(r for r in report if r.get("is_total"))
		self.assertEqual(len(days), 8)
		self.assertEqual(days[-1]["date"], str(today))
		# Sales today = the report's Net Sales, the return included; Transactions / Avg. ticket count the
		# sales only: the report's rows less its returns, and those sales' Net Sales.
		self.assertEqual(days[-1]["net_sales"], total["net_sales"])           # 120 + 80.50 - 20
		self.assertEqual(days[-1]["transactions"], len([r for r in body if not r["is_return"]]))
		self.assertEqual((days[-1]["net_sales"], days[-1]["transactions"], days[-1]["ticket_net"]), (180.5, 2, 200.5))
		self.assertEqual(days[0], {"date": str(add_days(today, -7)), "net_sales": 150.0, "transactions": 1, "ticket_net": 150.0})
		self.assertEqual(days[-2]["net_sales"], 999.0)
		self.assertNotIn(5000.0, [d["net_sales"] for d in days])             # eight days, no more

	def test_one_report_run_feeds_the_tiles_and_the_bars(self):
		with patch.object(so, "sales_report", wraps=so.sales_report) as run:
			so.get_overview()
			so.get_overview()                                                   # the second read is the cached one
		self.assertEqual(run.call_count, 1)
		filters = run.call_args.args[0]
		self.assertEqual((filters["view"], str(filters["to_date"])), ("Order", nowdate()))
		self.assertEqual(str(filters["from_date"]), str(add_days(getdate(nowdate()), -7)))


# ------------------------------------------------------------------ permissions and failures

class TestPermissionsAndFailures(OverviewCase):
	def setUp(self):
		super().setUp()
		_user(SALES_USER, "Sales User")
		_user(STOCK_USER, "Stock User")

	def test_the_counter_sees_sales_and_the_drawer_but_not_which_records_failed(self):
		with _as(SALES_USER):
			d = so.get_overview()
			pos_reports = frappe.has_permission("POS Invoice", "report")
		self.assertIsNotNone(d["sales"])
		self.assertNotIn("error", d["sales"])
		self.assertIsNotNone(d["drawer"])
		self.assertNotIn("error", d["drawer"])
		# The 4473 reports point at POS Invoice; pos_compat gives Sales User report on it (after_migrate).
		self.assertTrue(pos_reports)
		self.assertIsNotNone(d["open_4473"])
		self.assertNotIn("error", d["open_4473"])
		self.assertIsNone(d["stock"])                                           # Firearms In Stock does not
		self.assertIsNotNone(d["pickups"])                                      # FFL Acquisition read + report (r1)
		self.assertNotIn("error", d["pickups"])
		self.assertTrue(d["links"]["pickups"])
		if d["ad"] and not d["ad"].get("off"):
			self.assertFalse(d["ad"]["can_review"])                             # still no FFL Disposition read
			self.assertNotIn("records", d["ad"])

	def test_the_counter_can_run_the_transfer_pickups_report_but_not_change_an_acquisition(self):
		from frappe.desk.query_report import run

		with _as(SALES_USER):
			self.assertIn("result", run("Pending Transfer Pickups"))
			self.assertTrue(frappe.has_permission("FFL Acquisition", "read"))
			for ptype in ("write", "create", "submit", "delete"):
				self.assertFalse(frappe.has_permission("FFL Acquisition", ptype), ptype)

	def test_a_report_role_without_report_permission_on_its_doctype_sees_no_block_and_a_zero_card(self):
		real = frappe.has_permission

		def no_pos_reports(doctype=None, ptype="read", *args, **kwargs):
			if doctype == "POS Invoice" and ptype == "report":
				return False
			return real(doctype, ptype, *args, **kwargs)

		with _as(SALES_USER), patch.object(frappe, "has_permission", side_effect=no_pos_reports):
			self.assertTrue(frappe.get_cached_doc("Report", so.REPORT_4473).is_permitted())   # the report's roles let them in
			d = so.get_overview()
			card = gun_shop_cards.get_count({"card": "pending_4473"})
		self.assertIsNone(d["open_4473"])
		self.assertEqual(card["value"], 0)

	def test_stock_user_sees_no_sales_and_no_drawer(self):
		with _as(STOCK_USER):
			d = so.get_overview()
		self.assertIsNone(d["sales"])
		self.assertIsNone(d["drawer"])
		self.assertIsNotNone(d["stock"])
		self.assertIsNotNone(d["ad"])
		if not d["ad"].get("off"):
			self.assertTrue(d["ad"]["can_review"])

	def test_a_user_without_an_overview_role_is_refused(self):
		_user("overview-nobody@example.com", "Purchase User")
		with _as("overview-nobody@example.com"), self.assertRaises(frappe.PermissionError):
			so.get_overview()

	def test_a_block_that_throws_leaves_no_message_for_the_browser(self):
		def thrower():
			frappe.throw("Pickup report is broken")

		before = list(frappe.local.message_log or [])
		with patch.object(so, "_pickups", side_effect=thrower):
			d = so.get_overview()
		self.assertEqual(set(d["pickups"]), {"error"})
		self.assertEqual(list(frappe.local.message_log or []), before)
		self.assertIn("days", d["sales"])

	def test_a_failed_block_is_not_run_again_for_a_minute(self):
		errors = frappe.db.count("Error Log")
		with patch.object(so, "_stock", side_effect=RuntimeError("report broke")) as stock:
			first = so.get_overview()
			second = so.get_overview()
		self.assertEqual(stock.call_count, 1)
		self.assertEqual(first["stock"], second["stock"])
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		gun_shop_cards.clear_cache()                                            # the minute is up
		self.assertNotIn("error", so.get_overview()["stock"])

	def test_all_records_only_when_asked(self):
		self.assertIn("records", so.get_overview())
		self.assertNotIn("records", so.get_overview(with_records=0))

	def test_one_failing_block_costs_only_itself(self):
		errors = frappe.db.count("Error Log")
		with patch.object(so, "_pickups", side_effect=RuntimeError("report broke")):
			d = so.get_overview()
		self.assertEqual(set(d["pickups"]), {"error"})
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)
		for block in BLOCKS:
			if block != "pickups" and d[block] is not None and not isinstance(d[block], list):
				self.assertNotIn("error", d[block], block)
		self.assertIsInstance(d["records"], list)
		self.assertIn("days", d["sales"])

	def test_the_page_lets_in_every_role_that_lands_on_it(self):
		page = frappe.get_doc("Page", "store-overview")
		roles = {r.role for r in page.roles}
		self.assertTrue(set(frappe.get_hooks("role_home_page")) <= roles)
		from ffl_core.roles import OVERVIEW_ROLES

		self.assertEqual(roles, set(OVERVIEW_ROLES))


# ------------------------------------------------------------------ A&D

class TestNotLogged(OverviewCase):
	def setUp(self):
		super().setUp()
		if not _integrations():
			self.skipTest("ffl_integrations not installed")
		from ffl_core.setup import test_bootstrap

		test_bootstrap.run()
		self.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		enqueue = patch.object(frappe, "enqueue")
		enqueue.start()
		self.addCleanup(enqueue.stop)

	def _acq(self, tag, *, minutes_ago, error=""):
		from ffl_core.acquisition import record_firearm_acquisition

		code = f"OV-AD-{tag}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Rifle",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 0, "firearm_serial": f"OV-SN-{tag}"}).insert(
				ignore_permissions=True)
		name = record_firearm_acquisition(item_code=code, serial=f"OV-SN-{tag}", source_type="FFL Dealer",
			source_name="ACME", source_ffl="1-12-345-67-8K-99999", cost=100, company=self.company)
		frappe.db.set_value("FFL Acquisition", name, {"fastbound_sync_error": error,
			"modified": add_to_date(now_datetime(), minutes=-minutes_ago)}, update_modified=False)
		return name

	def _overview(self):
		gun_shop_cards.clear_cache()
		with patch("ffl_integrations.integration_gate.fastbound_enabled", return_value=True):
			return so.get_overview()["ad"]

	def test_not_logged_is_the_daily_retry_set_older_than_15_minutes_split_by_who_fixes_it(self):
		from ffl_integrations.fastbound import tasks

		old = self._acq("OLD", minutes_ago=60, error="HTTP 503 from FastBound")
		stuck = self._acq("STUCK", minutes_ago=60, error=tasks._TERMINAL_PREFIX + "already disposed")
		fresh = self._acq("FRESH", minutes_ago=2)

		ad = self._overview()
		listed = {r["name"]: r["terminal"] for r in ad["records"]}
		self.assertEqual(listed.get(old), False)
		self.assertEqual(listed.get(stuck), True)
		self.assertNotIn(fresh, listed)

		# The retry's own selection, by the task itself.
		frappe.enqueue.reset_mock()
		with patch.object(tasks, "fastbound_enabled", return_value=True):
			tasks.reconcile_failed_pushes()
		retried = {c.kwargs.get("acquisition_name") or c.kwargs.get("disposition_name") for c in frappe.enqueue.call_args_list}
		self.assertIn(old, retried)
		self.assertIn(fresh, retried)                                           # the retry does not age-filter
		self.assertNotIn(stuck, retried)                                        # it leaves terminal ones to a person

		cutoff = add_to_date(now_datetime(), minutes=-so.NOT_LOGGED_AFTER_MINUTES)
		every = tasks.unsynced_records()
		self.assertEqual({r["name"] for r in every if not r["terminal"]}, retried)
		aged = [r for r in every if r["modified"] <= cutoff]
		self.assertEqual(ad["retrying"], sum(1 for r in aged if not r["terminal"]))
		self.assertEqual(ad["needs_person"], sum(1 for r in aged if r["terminal"]))
		self.assertEqual(len(ad["records"]), min(len(aged), so.REVIEW_ROWS))

	def test_a_synced_record_is_not_counted(self):
		name = self._acq("SYNCED", minutes_ago=60)
		frappe.db.set_value("FFL Acquisition", name, {"fastbound_id": "fb-1", "fastbound_synced_at": now_datetime()},
			update_modified=False)
		ad = self._overview()
		self.assertNotIn(name, {r["name"] for r in ad["records"]})
		self.assertTrue(ad["synced_at"])
		self.assertGreaterEqual(ad["acquired"], 1)                              # booked today, synced or not

	def test_fastbound_off_is_all_the_block_says(self):
		with patch("ffl_integrations.integration_gate.fastbound_enabled", return_value=False):
			self.assertEqual(so.get_overview()["ad"], {"off": True})


# ------------------------------------------------------------------ all records

class TestRecords(ExtensionFixture):
	def setUp(self):
		super().setUp()
		gun_shop_cards.clear_cache()

	def _titles(self, off=(), rows=()):
		with _switches(off), _fake_sidebar_hook(list(rows)):
			return so.records()

	def test_core_groups_online_while_a_web_channel_is_on_and_setup_last(self):
		groups = self._titles()
		titles = [g["title"] for g in groups]
		self.assertEqual(titles[:5], ["Sales", "Compliance", "Purchasing", "Inventory", "Customers"])
		self.assertEqual(titles[5], "Online")
		self.assertEqual(titles[-1], "Setup")
		links = {i["link_to"] for g in groups for i in g["items"]}
		self.assertNotIn("FastBound Import", links)
		if frappe.db.exists("DocType", "ShipStation Settings"):
			self.assertIn("ShipStation Settings", {i["link_to"] for i in groups[-1]["items"]})

	def test_a_store_without_web_channels_has_no_online_group_and_no_shipstation(self):
		groups = self._titles(off={"Woo Online Order", "GunBroker Order"})
		self.assertNotIn("Online", [g["title"] for g in groups])
		links = {i["link_to"] for g in groups for i in g["items"]}
		self.assertFalse(links & {"Woo Online Order", "GunBroker Order", "ShipStation Settings"})
		self.assertEqual(self._titles(off={"GunBroker Order"})[5]["items"][0]["link_to"], "Woo Online Order")

	def test_an_extension_adds_its_own_group_and_online_can_sit_beside_it(self):
		groups = self._titles(rows=FAKE_ROWS)
		titles = [g["title"] for g in groups]
		self.assertIn("Online", titles)
		self.assertEqual(titles[-2:], ["Fake Things", "Setup"])
		self.assertEqual(groups[-2]["items"], [{"label": "Fake Page", "link_type": "Page", "link_to": "permission-manager"}])
		no_web = [g["title"] for g in self._titles(off={"Woo Online Order", "GunBroker Order"}, rows=FAKE_ROWS)]
		self.assertNotIn("Online", no_web)
		self.assertIn("Fake Things", no_web)

	def test_a_switched_off_integration_takes_its_link_with_it(self):
		links = {i["link_to"] for g in self._titles(off={"Card Terminal Payment"}) for i in g["items"]}
		self.assertNotIn("Card Terminal Payment", links)

	def test_only_what_the_user_may_open(self):
		_user(SALES_USER, "Sales User")
		with _as(SALES_USER):
			links = {i["link_to"] for g in self._titles() for i in g["items"]}
		self.assertIn("Customer", links)
		self.assertIn("FFL Acquisition", links)                                 # read + report since r1
		self.assertNotIn("FFL Disposition", links)                              # Sales User cannot read it
		self.assertNotIn("Firearms In Stock", links)                            # nor open that report


# ------------------------------------------------------------------ landing and sidebar

class TestLanding(FrappeTestCase):
	LANDING = "desk/gun-shop/store-overview"

	def test_every_way_in_lands_on_the_overview(self):
		self.assertEqual(frappe.get_hooks("home_page")[-1], self.LANDING)
		self.assertTrue(all(v[-1] == self.LANDING for v in frappe.get_hooks("role_home_page").values()))
		tile = next(t for t in frappe.get_hooks("add_to_apps_screen") if t["name"] == "ffl_core")
		self.assertEqual(tile["route"], "/" + self.LANDING)
		redirect = next(r for r in frappe.get_hooks("website_redirects") if r["source"] == "/gun-shop")
		self.assertEqual(redirect["target"], "/" + self.LANDING)

	def test_the_desk_reroutes_the_workspace_to_the_overview(self):
		path = frappe.get_app_path("ffl_core", "public", "js", "ffl_core.bundle.js")
		with open(path) as f:
			bundle = f.read()
		self.assertIn("frappe.re_route['gun-shop'] = LANDING", bundle)
		self.assertIn(f"const LANDING = '/{self.LANDING}'", bundle)
		self.assertIn("route[0] === 'Workspaces' && route[1] === 'Gun Shop'", bundle)   # any shell's spelling

	def test_the_settings_sidebar_leads_back_through_the_workspace(self):
		"""Its Gun Shop row stays a Workspace link, spelled /desk/settings/gun-shop inside that shell: a
		URL row always opens a new tab (16.50 sidebar_item.html) and a Page row would draw the Overview
		under the Settings sidebar. The bundle's route hook (above) turns that spelling into the Overview."""
		first = frappe.get_doc("Sidebar", "Settings").items[0]
		self.assertEqual((first.label, first.link_type, first.link_to), ("Gun Shop", "Workspace", "Gun Shop"))


class TestMirrorAfterTheFirstRowChanged(ExtensionFixture):
	def setUp(self):
		super().setUp()
		_drop_site_layer()
		self.addCleanup(frappe.clear_cache, doctype="Custom Sidebar")
		frappe.db.savepoint("ov_mirror")
		self.addCleanup(frappe.db.rollback, save_point="ov_mirror")
		self.addCleanup(frappe.clear_cache, doctype="Sidebar")

	def test_a_mirror_built_on_the_old_first_row_is_rewritten_by_migrate_and_found_again(self):
		from ffl_core.setup.sidebar import gun_shop_items, item_dicts, mirror_gun_shop_sidebar

		module = "FFL Woo Sync"
		# The mirror an extension wrote before this change: the old shell, Gun Shop workspace first.
		old_first = {"type": "Link", "label": "Gun Shop", "link_type": "Workspace", "link_to": "Gun Shop", "icon": "retail"}
		doc = frappe.new_doc("Sidebar")
		doc.update({"module": module, "title": "Old Mirror", "standard": 0, "header_icon": "retail"})
		for row in [old_first] + gun_shop_items()[1:] + FAKE_ROWS:
			doc.append("items", row)
		doc.flags.ignore_links = True
		previous = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		self.addCleanup(setattr, frappe.flags, "in_migrate", previous)
		doc.save(ignore_permissions=True)

		self.assertNotIn(module, gs.mirror_shells(gun_shop_items(), "FFL Core"))   # not recognised any more

		# The extension's migrate hook writes its mirror again, from the shipped shell.
		with _fake_sidebar_hook([]):
			self.assertTrue(mirror_gun_shop_sidebar("Old Mirror", module=module, extra_items=FAKE_ROWS))
		shells = gs.mirror_shells(gun_shop_items(), "FFL Core")
		self.assertIn(module, shells)
		self.assertEqual(shells[module][0]["link_to"], "store-overview")

		with _fake_sidebar_hook(FAKE_ROWS), _switches({"GunBroker Order"}):
			gs.sync_site_layer()
		mirror = _rendered(item_dicts(frappe.get_doc("Sidebar", "Old Mirror")), module=module)
		self.assertEqual(mirror, _rendered())
		self.assertEqual(mirror[0], "Dashboard")
		self.assertEqual(mirror[-3:], ["Fake Things", "Fake Page", "Settings"])


class TestADRefresh(OverviewCase):
	def test_the_refresh_button_recounts_instead_of_reading_the_shared_count(self):
		if not _integrations():
			self.skipTest("ffl_integrations not installed")
		from ffl_integrations.fastbound import tasks

		with patch("ffl_integrations.integration_gate.fastbound_enabled", return_value=True), \
				patch.object(tasks, "unsynced_records", wraps=tasks.unsynced_records) as count:
			so.get_overview()
			so.get_overview()
			self.assertEqual(count.call_count, 1)                               # the minute-old count
			so.get_overview(fresh_ad=1)
			self.assertEqual(count.call_count, 2)


def _line(do, idx, *, ack=0, inv=0, rec=0, sub=0, status="", placed="2026-10-01 09:00:00"):
	return {"do_name": do, "idx": idx, "distributor": "RSR", "status": "Partially Shipped", "placed_at": placed,
		"ack_qty": ack, "invoiced_qty": inv, "received_qty": rec, "substituted_qty": sub, "line_status": status}


class TestArrivals(FrappeTestCase):
	def test_received_of_expected_uses_the_receive_bridges_line_rule(self):
		if not _integrations():
			self.skipTest("ffl_integrations not installed")
		got = so.fold_arrivals([
			_line("DO-1", 1, ack=3, status="Killed"),                          # will never come
			_line("DO-1", 2, ack=4, rec=2),
			_line("DO-2", 1, ack=5, inv=5, rec=1, sub=2, placed="2026-10-02 09:00:00"),   # a counter substitution counts as in
			_line("DO-3", 1, ack=2, rec=2),                                    # nothing left: not arriving
		])
		self.assertEqual(got["total"], 2)
		one, two = got["orders"]
		self.assertEqual((one["name"], one["received"], one["expected"], one["outstanding"]), ("DO-1", 2, 4, 2))
		self.assertEqual((two["name"], two["received"], two["expected"], two["outstanding"]), ("DO-2", 3, 5, 2))
		for o in got["orders"]:
			self.assertEqual(o["expected"] - o["received"], o["outstanding"])


class TestWorkspaceLinks(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.savepoint("ov_ws_links")
		self.addCleanup(frappe.db.rollback, save_point="ov_ws_links")
		self.addCleanup(frappe.clear_cache, doctype="Workspace")

	def _add_missing_card(self):
		from ffl_core.workspace_shortcuts import ensure_number_card

		return ensure_number_card("Overview Test No Such Card", anchor="To Receive", block_id="nc_ov_missing")

	def test_a_broken_link_is_still_refused_at_run_time(self):
		with self.assertRaises(frappe.LinkValidationError):
			self._add_missing_card()

	def test_during_an_app_install_a_link_to_something_not_synced_yet_is_let_through(self):
		previous = frappe.flags.in_install
		frappe.flags.in_install = "ffl_core"
		self.addCleanup(setattr, frappe.flags, "in_install", previous)
		self.assertTrue(self._add_missing_card())


class TestSalesUserPosInvoiceReport(FrappeTestCase):
	"""pos_compat.ensure_role_grants."""

	def setUp(self):
		super().setUp()
		_user(SALES_USER, "Sales User")
		frappe.set_user("Administrator")
		frappe.db.savepoint("ov_pos_report")
		self.addCleanup(frappe.clear_cache, doctype="POS Invoice")
		self.addCleanup(frappe.db.rollback, save_point="ov_pos_report")
		gun_shop_cards.clear_cache()
		self.addCleanup(gun_shop_cards.clear_cache)

	def _rows(self):
		from ffl_core.setup.pos_compat import _PERM_FLAGS
		from frappe.utils import cint

		table = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": "POS Invoice"}) else "DocPerm"
		return sorted((r.role, cint(r.permlevel), cint(r.if_owner), tuple(cint(r.get(f)) for f in _PERM_FLAGS))
			for r in frappe.get_all(table, filters={"parent": "POS Invoice"}, fields=["*"]))

	def _from_scratch(self):
		"""The site as ERPNext ships it: no Custom DocPerm on POS Invoice, the grant not yet made."""
		from ffl_core.setup import pos_compat

		for doctype in ("POS Invoice", "FFL Acquisition"):
			frappe.db.delete("Custom DocPerm", {"parent": doctype})
			frappe.clear_cache(doctype=doctype)
		frappe.db.set_global(pos_compat._ROLE_GRANTS_KEY, "[]")

	def test_the_counter_gets_report_and_both_4473_reports_and_the_block(self):
		from ffl_core.setup.pos_compat import ensure_role_grants

		self._from_scratch()
		with _as(SALES_USER):
			self.assertFalse(frappe.has_permission("POS Invoice", "report"))
		ensure_role_grants()
		with _as(SALES_USER):
			self.assertTrue(gun_shop_cards._may_open(["query-report", "Pending 4473 Orders"]))
			self.assertTrue(gun_shop_cards._may_open(["query-report", "Form 4473 Records"]))
			self.assertIsNotNone(so.get_overview()["open_4473"])
			self.assertFalse(frappe.has_permission("POS Invoice", "write"))

	def test_twice_is_once_and_no_other_role_changes(self):
		from ffl_core.setup.pos_compat import ensure_role_grants

		self._from_scratch()
		stock = self._rows()
		ensure_role_grants()
		once = self._rows()
		ensure_role_grants()
		self.assertEqual(self._rows(), once)
		self.assertEqual(frappe.db.count("Custom DocPerm", {"parent": "POS Invoice", "role": "Sales User"}), 1)
		self.assertEqual([r for r in once if r[0] != "Sales User"], [r for r in stock if r[0] != "Sales User"])
		[(_, _, _, flags)] = [r for r in once if r[0] == "Sales User"]
		[(_, _, _, before)] = [r for r in stock if r[0] == "Sales User"]
		from ffl_core.setup.pos_compat import _PERM_FLAGS
		changed = {f for f, a, b in zip(_PERM_FLAGS, before, flags) if a != b}
		self.assertEqual(changed, {"report"})

	def test_both_install_and_migrate_run_it(self):
		for hook in ("after_install", "after_migrate"):
			self.assertIn("ffl_core.setup.pos_compat.ensure_role_grants", frappe.get_hooks(hook))

	def test_a_removed_sales_user_row_on_a_customised_site_stays_removed(self):
		from frappe.permissions import setup_custom_perms
		from ffl_core.setup.pos_compat import ensure_role_grants

		self._from_scratch()
		setup_custom_perms("POS Invoice")                                       # the site's rules are custom...
		frappe.db.delete("Custom DocPerm", {"parent": "POS Invoice", "role": "Sales User"})   # ...and Sales User removed
		ensure_role_grants()
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "POS Invoice", "role": "Sales User"}))

	def test_ffl_acquisition_writes_nothing_where_its_json_rules_apply(self):
		from ffl_core.setup.pos_compat import ensure_role_grants

		self._from_scratch()
		ensure_role_grants()
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": "FFL Acquisition"}))
		with _as(SALES_USER):
			self.assertTrue(frappe.has_permission("FFL Acquisition", "report"))

	def test_ffl_acquisition_is_added_to_rules_customised_before_the_grant(self):
		from frappe.permissions import setup_custom_perms
		from ffl_core.setup.pos_compat import ensure_role_grants

		self._from_scratch()
		setup_custom_perms("FFL Acquisition")
		# a copy made before the JSON gave the counter its row
		frappe.db.delete("Custom DocPerm", {"parent": "FFL Acquisition", "role": ["in", ["Sales User", "Sales Manager"]]})
		ensure_role_grants()
		for role in ("Sales User", "Sales Manager"):
			row = frappe.db.get_value("Custom DocPerm", {"parent": "FFL Acquisition", "role": role},
				["read", "report", "write", "create", "submit", "delete"], as_dict=True)
			self.assertEqual((row.read, row.report, row.write, row.create, row.submit, row.delete), (1, 1, 0, 0, 0, 0), role)


class TestR2Guards(OverviewCase):
	def setUp(self):
		super().setUp()
		_user(SALES_USER, "Sales User")
		_user(STOCK_USER, "Stock User")
		_user("overview-books@example.com", "Accounts User")

	def test_one_users_failed_drawer_does_not_blank_anothers(self):
		real = so._drawer

		def drawer_for_b_only():
			if frappe.session.user == SALES_USER:
				raise RuntimeError("no company for this user")
			return real()

		with patch.object(so, "_drawer", side_effect=drawer_for_b_only):
			with _as(SALES_USER):
				self.assertEqual(set(so.get_overview()["drawer"]), {"error"})
			d = so.get_overview()                                                # Administrator
		self.assertNotIn("error", d["drawer"])

	def test_a_failing_permission_check_or_button_check_costs_only_itself(self):
		with patch.object(so, "_arriving_allowed", side_effect=RuntimeError("roles broke")):
			d = so.get_overview()
		self.assertEqual(set(d["arriving"]), {"error"})
		self.assertIn("days", d["sales"])
		with patch.object(frappe, "has_permission", side_effect=RuntimeError("perm broke")):
			links = so._links()
		self.assertEqual(links, {})

	def test_all_records_reports_follow_the_report_rule(self):
		with _as(STOCK_USER):
			links = {i["link_to"] for g in so.records() for i in g["items"]}
			pos_reports = frappe.has_permission("POS Invoice", "report")
		self.assertFalse(pos_reports)
		self.assertNotIn("Pending 4473 Orders", links)                          # the report lists Stock User; POS Invoice does not
		self.assertIn("Firearms In Stock", links)

	def test_a_disabled_report_opens_for_nobody(self):
		frappe.db.savepoint("ov_disabled")
		self.addCleanup(frappe.db.rollback, save_point="ov_disabled")
		self.addCleanup(frappe.clear_document_cache, "Report", so.REPORT_STOCK)
		self.assertTrue(gun_shop_cards._may_open(["query-report", so.REPORT_STOCK]))
		frappe.db.set_value("Report", so.REPORT_STOCK, "disabled", 1)
		frappe.clear_document_cache("Report", so.REPORT_STOCK)
		self.assertFalse(gun_shop_cards._may_open(["query-report", so.REPORT_STOCK]))

	def test_only_someone_who_sees_the_ad_block_can_force_a_recount(self):
		key = gun_shop_cards.CACHE_PREFIX + so._ad_key()
		stale = {"off": False, "acquired": -1}
		frappe.cache.set_value(key, stale, expires_in_sec=60)
		with _as("overview-books@example.com"):                                  # Accounts User: no A&D block
			self.assertIsNone(so.get_overview(fresh_ad=1)["ad"])
		self.assertEqual(frappe.cache.get_value(key, expires=True), stale)
		with _as(STOCK_USER):
			so.get_overview(fresh_ad=1)
		self.assertNotEqual(frappe.cache.get_value(key, expires=True), stale)
