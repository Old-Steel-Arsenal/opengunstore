"""`distributor.hub` — the server-side contract of the Distributors settings hub page.

Shared dev site discipline: build our own HUB-DC-* rows and clean them up with
addClassCleanup; any case that touches existing real rows (full reorder
permutations, the priority backfill patch) first snapshots every priority on the
site and restores them row by row at the end (update_modified=False, so the
modified order is not disturbed). Catalog reads go through catalog_stub
(remote_catalog forbids real HTTP in the test environment).
"""
from __future__ import annotations

import types
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_integrations.catalog_local import sync as catalog_sync
from ffl_integrations.distributor import hub, rsr_provider
from ffl_integrations.distributor.api import _default_distributor_name
from ffl_integrations.distributor.base import DistributorProvider
from ffl_integrations.patches import backfill_distributor_priority
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests.site_fixtures import snapshot_single_values

DIST_A = "HUB-DC-A"
DIST_B = "HUB-DC-B"
SUM_RSR = "HUB-DC-SR"
SUM_SS = "HUB-DC-SS"
_ALL_TEST_CODES = (DIST_A, DIST_B, "HUB-DC-C1", "HUB-DC-C2", "HUB-DC-N", SUM_RSR, SUM_SS)


class _BareProvider(DistributorProvider):
	"""Only the abstract methods: every optional one keeps the base default."""

	code = "bare"

	def capabilities(self):
		return set()

	def search(self, q, limit=10):
		return []

	def catalog_row(self, sku):
		return None

	def check_availability(self, lines):
		return []

	def plan_fulfillment(self, lines, ship_context):
		return []

	def place_order(self, do_name):
		return {}

	def get_order_status(self, do_name):
		return {}


def _mk(code, **overrides):
	doc = frappe.get_doc({
		"doctype": "Distributor", "code": code, "distributor_name": f"{code} DC",
		"provider": "rsr", "enabled": 0, "dropship_enabled": 0,
		"stocking_enabled": 0, "poll_enabled": 0, **overrides,
	})
	doc.insert(ignore_permissions=True)
	return doc


def _cleanup_rows():
	for code in _ALL_TEST_CODES:
		if frappe.db.exists("Distributor", code):
			frappe.delete_doc("Distributor", code, ignore_permissions=True, force=True)
	frappe.db.commit()


def _snapshot_priorities():
	"""{name: priority} for every Distributor on the site RIGHT NOW."""
	return {r.name: r.priority for r in frappe.get_all(
		"Distributor", fields=["name", "priority"])}


def _restore_priorities(snap):
	for name, prio in snap.items():
		if frappe.db.exists("Distributor", name):
			frappe.db.set_value(
				"Distributor", name, "priority", prio, update_modified=False)


class DistributorHub(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		catalog_stub.install()
		cls.addClassCleanup(_cleanup_rows)
		_mk(DIST_A)
		_mk(DIST_B)

	# ------------------------------------------------------------- overview
	def test_overview_row_shape_and_provider_registry(self):
		out = hub.overview()
		rows = {r["name"]: r for r in out["distributors"]}
		self.assertIn(DIST_A, rows)
		a = rows[DIST_A]
		self.assertTrue(a["provider_registered"])
		self.assertEqual(a["settings_doctype"], "RSR Settings")
		self.assertEqual(a["orders"], {"draft": 0, "in_flight": 0, "attention": 0})
		# The markup column is the drop-ship layer's (osa_dropship): there exactly
		# when the layer is installed.
		self.assertEqual("markup_rules" in a, out["dropship_layer"])
		# stub catalog is healthy: both heartbeat keys exist and carry no error
		self.assertNotIn("error", a["health"])
		self.assertIn("feed_heartbeat", a["health"])
		codes = {p["code"]: p for p in out["providers"]}
		self.assertIn("rsr", codes)
		self.assertEqual(codes["rsr"]["settings_doctype"], "RSR Settings")
		self.assertIn(DIST_A, codes["rsr"]["used_by"])
		self.assertTrue(out["can_write"])  # Administrator

	def test_overview_orders_by_priority(self):
		snap = _snapshot_priorities()
		self.addCleanup(_restore_priorities, snap)
		frappe.db.set_value("Distributor", DIST_A, "priority", -1, update_modified=False)
		frappe.db.set_value("Distributor", DIST_B, "priority", -2, update_modified=False)
		names = [r["name"] for r in hub.overview()["distributors"]]
		self.assertEqual(names[:2], [DIST_B, DIST_A])

	def test_unregistered_provider_degrades_not_crashes(self):
		_mk("HUB-DC-N", provider="no-such-provider")
		try:
			out = hub.overview()
			row = next(r for r in out["distributors"] if r["name"] == "HUB-DC-N")
			self.assertFalse(row["provider_registered"])
			self.assertIn("error", row["health"])
			self.assertEqual(row["settings_doctype"], "")
		finally:
			frappe.delete_doc(
				"Distributor", "HUB-DC-N", ignore_permissions=True, force=True)

	# ------------------------------------------------------- priority plumbing
	def test_before_insert_appends_to_tail(self):
		max_prio = max((r.priority or 0 for r in frappe.get_all(
			"Distributor", fields=["priority"])), default=0)
		doc = _mk("HUB-DC-C1")
		try:
			self.assertEqual(doc.priority, max_prio + 1)
		finally:
			frappe.delete_doc(
				"Distributor", "HUB-DC-C1", ignore_permissions=True, force=True)

	def test_backfill_patch_stamps_modified_order_and_skips_set_rows(self):
		snap = _snapshot_priorities()
		self.addCleanup(_restore_priorities, snap)
		c1 = _mk("HUB-DC-C1")
		c2 = _mk("HUB-DC-C2")
		self.addCleanup(lambda: [frappe.delete_doc(
			"Distributor", n, ignore_permissions=True, force=True)
			for n in ("HUB-DC-C1", "HUB-DC-C2")])
		# simulate the pre-patch world: two rows with no explicit priority, modified order c1 < c2
		frappe.db.set_value("Distributor", c1.name, "priority", 0, update_modified=False)
		frappe.db.set_value("Distributor", c2.name, "priority", 0, update_modified=False)
		frappe.db.set_value("Distributor", c1.name, "modified", "2020-01-01 00:00:01",
			update_modified=False)
		frappe.db.set_value("Distributor", c2.name, "modified", "2020-01-01 00:00:02",
			update_modified=False)
		set_before = {n: p for n, p in _snapshot_priorities().items() if p}

		backfill_distributor_priority.execute()

		p1 = frappe.db.get_value("Distributor", c1.name, "priority")
		p2 = frappe.db.get_value("Distributor", c2.name, "priority")
		self.assertTrue(0 < p1 < p2, f"expected modified-order stamping, got {p1}/{p2}")
		for name, prio in set_before.items():
			self.assertEqual(
				frappe.db.get_value("Distributor", name, "priority"), prio,
				f"{name}: already-set priority must survive the patch")
		# idempotent: a second run changes nothing
		backfill_distributor_priority.execute()
		self.assertEqual(frappe.db.get_value("Distributor", c1.name, "priority"), p1)

	def test_default_house_follows_priority(self):
		snap = _snapshot_priorities()
		self.addCleanup(_restore_priorities, snap)
		self.addCleanup(lambda: [frappe.db.set_value(
			"Distributor", n, "enabled", 0) for n in (DIST_A, DIST_B)])
		for name, prio in ((DIST_A, -1), (DIST_B, -2)):
			frappe.db.set_value("Distributor", name, "enabled", 1)
			frappe.db.set_value("Distributor", name, "priority", prio,
				update_modified=False)
		self.assertEqual(_default_distributor_name(), DIST_B)
		frappe.db.set_value("Distributor", DIST_A, "priority", -3,
			update_modified=False)
		self.assertEqual(_default_distributor_name(), DIST_A)

	# ------------------------------------------------------------ mutations
	def test_toggle_flag_flips_and_rejects_unknown_flag(self):
		out = hub.toggle_flag(DIST_A, "dropship_enabled", 1)
		self.assertEqual(out, {"name": DIST_A, "flag": "dropship_enabled", "value": 1})
		self.assertEqual(
			frappe.db.get_value("Distributor", DIST_A, "dropship_enabled"), 1)
		hub.toggle_flag(DIST_A, "dropship_enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			hub.toggle_flag(DIST_A, "default_markup_pct", 1)

	def test_reorder_full_permutation_and_partial_rejection(self):
		snap = _snapshot_priorities()
		self.addCleanup(_restore_priorities, snap)
		names = [r.name for r in frappe.get_all(
			"Distributor", order_by="priority asc, modified asc")]
		ours_first = [DIST_B, DIST_A] + [n for n in names if n not in (DIST_A, DIST_B)]
		hub.reorder(ours_first)
		self.assertEqual(
			frappe.db.get_value("Distributor", DIST_B, "priority"), 1)
		self.assertEqual(
			frappe.db.get_value("Distributor", DIST_A, "priority"), 2)
		with self.assertRaises(frappe.ValidationError):
			hub.reorder(ours_first[:-1])
		# the JSON-string form (HTTP marshalling) is accepted too
		hub.reorder(frappe.as_json(ours_first))

	# ------------------------------------------------------ connection probe
	def test_connection_probe_unknown_house_throws(self):
		with self.assertRaises(frappe.ValidationError):
			hub.test_connection("NO-SUCH-HOUSE")

	def test_connection_probe_rsr_direct_connect_disabled(self):
		self.addCleanup(snapshot_single_values("RSR Settings", ["api_enabled"]))
		s = frappe.get_single("RSR Settings")
		s.api_enabled = 0
		s.save(ignore_permissions=True)
		out = hub.test_connection(DIST_A)
		checks = out["checks"]
		self.assertGreaterEqual(len(checks), 2)
		self.assertTrue(checks[0]["label"].startswith("Catalog service"))
		self.assertTrue(checks[0]["ok"])  # stub catalog is healthy
		self.assertIsNone(checks[1]["ok"])  # Direct Connect disabled -> not applicable rather than a failure
		self.assertIn("disabled", checks[1]["detail"])

	def test_base_provider_default_probe_is_not_applicable(self):
		out = _BareProvider().connection_test()
		self.assertIsNone(out["checks"][0]["ok"])
		self.assertIn("bare", out["checks"][0]["detail"])


class HubCardSummary(SettingsSandbox, FrappeTestCase):
	"""The card's read-only line: Ordering (the row's Enabled flag and the
	provider's Allow live orders switch) and Ship to store (the provider's
	freight rule), each opening that tab on the provider's settings page."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		catalog_stub.install()
		cls.addClassCleanup(_cleanup_rows)
		_mk(SUM_RSR)
		_mk(SUM_SS, provider="ss")

	def setUp(self):
		# set_single's raw DELETE (restoring "no row") does not clear the document
		# cache the providers read through; registered first, so it runs last.
		for doctype in ("RSR Settings", "Sports South Settings"):
			self.addCleanup(frappe.clear_document_cache, doctype, doctype)
		self.addCleanup(lambda: [frappe.db.set_value("Distributor", n, "enabled", 0,
			update_modified=False) for n in (SUM_RSR, SUM_SS)])

	def _summary(self, name, providers=None):
		"""{label: (value, tab)} for one card; `providers` replaces the registry."""
		if providers is None:
			out = hub.overview()
		else:
			with mock.patch.object(hub, "get_providers", return_value=providers):
				out = hub.overview()
		row = next(r for r in out["distributors"] if r["name"] == name)
		return {item["label"]: (item["value"], item["tab"]) for item in row["summary"]}

	def test_ordering_is_live_only_when_the_row_and_the_master_switch_are_on(self):
		for enabled, switch, expected in (
				(1, 1, "live"),
				(1, 0, "off (Allow live orders is off)"),
				(0, 1, "off (disabled)"),
				(0, 0, "off (disabled, Allow live orders is off)")):
			with self.subTest(enabled=enabled, switch=switch):
				frappe.db.set_value("Distributor", SUM_RSR, "enabled", enabled, update_modified=False)
				self.set_single("RSR Settings", "api_enabled", switch)
				self.assertEqual(self._summary(SUM_RSR)["Ordering"], (expected, "tab_account"))

	def test_ss_ordering_reads_its_own_switch(self):
		frappe.db.set_value("Distributor", SUM_SS, "enabled", 1, update_modified=False)
		self.set_single("Sports South Settings", "ordering_enabled", 1)
		self.assertEqual(self._summary(SUM_SS)["Ordering"], ("live", "tab_account"))
		self.set_single("Sports South Settings", "ordering_enabled", 0)
		self.assertEqual(self._summary(SUM_SS)["Ordering"][0], "off (Allow live orders is off)")

	def test_rsr_ship_to_store_says_what_the_estimate_charges(self):
		self.set_single("RSR Settings", "rate_ground", 10)
		for free_switch, threshold, expected in (
				(1, 750, "free"),
				(0, 750, "free over $750.00"),
				# A stated 0 threshold ships every store order free (setting_value).
				(0, 0, "free")):
			with self.subTest(free_switch=free_switch, threshold=threshold):
				self.set_single("RSR Settings", "store_free_freight", free_switch)
				self.set_single("RSR Settings", "free_freight_threshold", threshold)
				self.assertEqual(self._summary(SUM_RSR)["Ship to store"], (expected, "tab_freight"))
				# The card never contradicts the planner: an order AT the threshold
				# pays freight exactly when the card says "free over".
				order = [{"unit_cost": threshold or 1, "qty": 1, "ship_method": "Grnd"}]
				charged = rsr_provider.estimate_freight(
					order, "to_store", frappe.get_cached_doc("RSR Settings")) > 0
				self.assertEqual(charged, expected.startswith("free over"))

	def test_ss_ship_to_store_states_both_thresholds(self):
		for qty, amount, other, expected in (
				(3, 750, 500, "handguns free from 3 or $750.00; everything else free from $500.00"),
				(0, 750, 500, "handguns free; everything else free from $500.00"),
				(3, 750, 0, "handguns free from 3 or $750.00; everything else free")):
			with self.subTest(qty=qty, amount=amount, other=other):
				self.set_single("Sports South Settings", "store_free_handgun_qty", qty)
				self.set_single("Sports South Settings", "store_free_handgun_amount", amount)
				self.set_single("Sports South Settings", "store_free_other_amount", other)
				self.assertEqual(self._summary(SUM_SS)["Ship to store"], (expected, "tab_freight"))

	def test_a_provider_without_a_summary_renders_no_line(self):
		inherited = _BareProvider()
		inherited.code = "rsr"
		duck = types.SimpleNamespace(code="rsr", settings_doctype="RSR Settings")
		for provider in (inherited, duck):
			with self.subTest(type(provider).__name__):
				self.assertEqual(self._summary(SUM_RSR, {"rsr": provider}), {})


class HubLocalCatalog(FrappeTestCase):
	LOCAL_RSR = "HUB-LOCAL-RSR"
	PM_USER = "hub.local.pm@example.com"

	def setUp(self):
		super().setUp()
		if frappe.db.exists("Distributor", self.LOCAL_RSR):
			frappe.delete_doc("Distributor", self.LOCAL_RSR, ignore_permissions=True, force=True)
		self.dist = _mk(self.LOCAL_RSR, enabled=1)

		def cleanup():
			if frappe.db.exists("Distributor", self.LOCAL_RSR):
				frappe.delete_doc("Distributor", self.LOCAL_RSR, ignore_permissions=True, force=True)
			frappe.db.commit()

		self.addCleanup(cleanup)

	def test_overview_shows_lane_health_only_for_a_local_catalog(self):
		with mock.patch("ffl_integrations.catalog_local.sync.is_local", return_value=False):
			out = hub.overview()
		for row in out["distributors"]:
			self.assertIsNone(row["catalog_lanes"])
		self.assertFalse(out["can_sync_now"])
		with mock.patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				mock.patch.object(catalog_sync, "worker_running", return_value=True):
			out = hub.overview()
		row = next(r for r in out["distributors"] if r["name"] == self.LOCAL_RSR)
		self.assertEqual([ln["lane"] for ln in row["catalog_lanes"]["lanes"]],
			["catalog", "deleted", "quantity", "restrictions", "case_price"])
		self.assertTrue(out["can_sync_now"])

	def test_sync_now_is_system_manager_only_and_local_only(self):
		if not frappe.db.exists("User", self.PM_USER):
			frappe.get_doc({"doctype": "User", "email": self.PM_USER, "first_name": "Hub",
				"send_welcome_email": 0, "roles": [{"role": "Purchase Manager"}]}).insert(
				ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("User", self.PM_USER, ignore_permissions=True,
			force=True))
		frappe.set_user(self.PM_USER)
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			hub.sync_now(self.LOCAL_RSR)
		frappe.set_user("Administrator")
		with mock.patch("ffl_integrations.catalog_local.is_local", return_value=False), \
				self.assertRaises(frappe.ValidationError):
			hub.sync_now(self.LOCAL_RSR)
		with mock.patch("ffl_integrations.catalog_local.is_local", return_value=True), \
				mock.patch.object(catalog_sync, "sync_now",
					return_value={"enqueued": [], "deduplicated": [], "worker_running": True}) as queue:
			out = hub.sync_now(self.LOCAL_RSR)
		queue.assert_called_once_with("rsr")
		self.assertTrue(out["worker_running"])
