"""Extension SDK + registry (core 1.1): the store switch is honoured everywhere.

A fake extension ``ext_fake`` is injected through ``registry.candidates`` /
``manifest_of`` (nothing has to be installed on the bench), so the sync
rules, the switch cache, the gate, the hook decorator, boot and the page
endpoints are all pinned without a real app.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import extensions as sdk
from ffl_core.api import extensions as api
from ffl_core.extensions import jobs, registry
from ffl_core.gate_audit import DEFAULT_GATE_PATTERNS

FAKE = "ext_fake"
MANIFEST = {
	"id": FAKE, "title": "Fake Ext", "publisher": "Test Co", "publisher_id": "testco",
	"version": "0.3.0", "requires_core": ">=0.1.0b1,<2",
	"permissions": ["pos_ui", "network:api.example.com"], "license_required": False,
}


class ExtensionFixture(FrappeTestCase):
	"""Sync with ext_fake on the bench (and, unless said otherwise, installed here)."""
	installed_here = True
	manifest = MANIFEST

	def setUp(self):
		super().setUp()
		real_bench = registry.bench_apps
		real_installed = registry.installed_apps
		self._patches = [
			patch.object(registry, "bench_apps", side_effect=lambda: real_bench() + [FAKE]),
			patch.object(registry, "manifest_of",
				side_effect=lambda app: dict(self.manifest) if app == FAKE else _orig_manifest_of(app)),
			patch.object(registry, "installed_apps",
				side_effect=lambda: real_installed() | ({FAKE} if self.installed_here else set())),
		]
		for p in self._patches:
			p.start()
		frappe.set_user("Administrator")
		registry.sync()

	def tearDown(self):
		for p in self._patches:
			p.stop()
		if frappe.db.exists(sdk.DOCTYPE, FAKE):
			frappe.delete_doc(sdk.DOCTYPE, FAKE, ignore_permissions=True, force=True)
		sdk.clear_enabled_cache()
		super().tearDown()


_orig_manifest_of = registry.manifest_of


class TestCompat(FrappeTestCase):
	def test_ranges(self):
		self.assertEqual(registry.check_compat(None, "0.2.0-beta.1"), (True, ""))
		self.assertTrue(registry.check_compat(">=0.2.0,<1", "0.2.0-beta.1")[0])
		self.assertTrue(registry.check_compat(">=0.1.0b1,<1", "0.2.0-beta.1")[0])
		ok, reason = registry.check_compat(">=0.3", "0.2.0-beta.1")
		self.assertFalse(ok)
		self.assertIn("needs core >=0.3", reason)
		ok, reason = registry.check_compat("not a range", "0.2.0-beta.1")
		self.assertFalse(ok)
		self.assertIn("not a valid version range", reason)

	def test_the_transition_range_spans_both_version_lines(self):
		# The version line restarted at 0.1.0 after the private 1.15.1: first-party extensions ship
		# ">=0.1.0,<2" so they run on both, and a store rolled back to 1.15.1 keeps them.
		for core in ("1.15.1", "0.1.0", "0.2.0-beta.1"):
			self.assertTrue(registry.check_compat(">=0.1.0,<2", core)[0], core)
		self.assertFalse(registry.check_compat(">=1.15.1,<2", "0.1.0")[0])   # an old range stops at the restart

	def test_gate_audit_recognises_require_enabled_only_with_roles(self):
		from ffl_core.gate_audit import _is_gate
		self.assertIn("require_enabled(", DEFAULT_GATE_PATTERNS)
		self.assertTrue(_is_gate('require_enabled("x", roles=COUNTER_ROLES)', DEFAULT_GATE_PATTERNS))
		self.assertFalse(_is_gate('require_enabled("x")', DEFAULT_GATE_PATTERNS))
		self.assertTrue(_is_gate('frappe.only_for("System Manager")', DEFAULT_GATE_PATTERNS))

	def test_core_apps_are_never_extensions(self):
		self.assertFalse(set(registry.candidates()) & registry.CORE_APPS)

	def test_app_install_and_uninstall_resync_the_registry(self):
		"""A CLI `bench install-app <ext>` must show up installed without the
		manual sync (self-hosting guide): both frappe app hooks point at on_app_change,
		which syncs and swallows (logs) a failure so the install itself lives."""
		target = "ffl_core.extensions.registry.on_app_change"
		self.assertIn(target, frappe.get_hooks("after_app_install"))
		self.assertIn(target, frappe.get_hooks("after_app_uninstall"))
		with patch.object(registry, "sync", return_value={}) as sync:
			registry.on_app_change("some_app")
		sync.assert_called_once_with()
		with patch.object(registry, "sync", side_effect=RuntimeError("boom")), \
				patch.object(frappe, "log_error") as log:
			registry.on_app_change("some_app")            # must not raise
		self.assertIn("some_app", log.call_args.kwargs["title"])


class TestSyncInstalled(ExtensionFixture):
	def test_row_created_installed_enabled(self):
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.title, doc.version, doc.publisher_id), ("Fake Ext", "0.3.0", "testco"))
		self.assertEqual((doc.installed_on_site, doc.compatible, doc.enabled), (1, 1, 1))
		self.assertEqual(doc.source, "unknown")          # no sites/extensions.json entry → not claimed as baked
		self.assertIn("network:api.example.com", doc.permissions)
		self.assertTrue(sdk.is_enabled(FAKE))

	def test_second_sync_is_unchanged_and_keeps_the_switch(self):
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		doc.enabled = 0
		doc.save()
		summary = registry.sync()
		self.assertIn(FAKE, summary["unchanged"])
		self.assertEqual(frappe.db.get_value(sdk.DOCTYPE, FAKE, "enabled"), 0)

	def test_manifest_file_metadata_is_stable_across_syncs(self):
		"""docs/self-hosting.md#extensions writes installed_at as ISO 8601 with a zone; the row stores a
		naive local datetime — the second sync must still say unchanged."""
		entry = {"source": "upload", "installed_at": "2026-09-11T22:20:18.132211+00:00", "installed_by": "admin-user"}
		with patch.object(registry, "manifest_file", return_value={FAKE: entry}):
			first = registry.sync()
			second = registry.sync()
		self.assertIn(FAKE, first["updated"])
		self.assertIn(FAKE, second["unchanged"])
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.source, doc.installed_by), ("upload", "admin-user"))
		self.assertTrue(doc.installed_at)

	def test_installed_app_missing_from_the_bench_list_keeps_its_row(self):
		"""A bench whose apps.txt momentarily omits an installed
		app must not cost the store its switch (the row would be rebuilt from
		defaults). Gone from the bench AND uninstalled is the only deletion."""
		frappe.db.set_value(sdk.DOCTYPE, FAKE, "enabled", 0)
		with patch.object(registry, "bench_apps", return_value=[]):
			summary = registry.sync()
		self.assertNotIn(FAKE, summary["removed"])
		self.assertTrue(frappe.db.exists(sdk.DOCTYPE, FAKE))
		self.assertEqual(frappe.db.get_value(sdk.DOCTYPE, FAKE, "enabled"), 0)   # the store's choice survives
		self.installed_here = False
		with patch.object(registry, "bench_apps", return_value=[]):
			self.assertIn(FAKE, registry.sync()["removed"])                       # gone from both → deleted

	def test_unreadable_manifest_keeps_the_row_and_the_switch_but_is_not_effective(self):
		"""Still on the bench but hooks unreadable: never deleted, never effective —
		and the store's switch is not written."""
		self.assertTrue(sdk.is_enabled(FAKE))
		with patch.object(registry, "manifest_of", side_effect=lambda app: None if app == FAKE else _orig_manifest_of(app)):
			summary = registry.sync()
			self.assertIn(FAKE, registry.sync()["unchanged"], "a second pass has nothing to write")
		self.assertIn(FAKE, summary["updated"])
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.compatible, doc.enabled), (0, 1))
		self.assertIn("could not be read", doc.incompatible_reason)
		self.assertFalse(sdk.is_enabled(FAKE))

	def test_no_row_is_off_and_the_read_path_never_writes(self):
		"""A row deleted underneath a running site (or never written after a CLI
		install) means OFF until registry.sync runs — is_enabled never inserts."""
		self.assertTrue(sdk.is_enabled(FAKE))
		frappe.delete_doc(sdk.DOCTYPE, FAKE, ignore_permissions=True, force=True)
		with patch.object(registry, "sync_app", side_effect=AssertionError("read path wrote")):
			self.assertFalse(sdk.is_enabled(FAKE))
		self.assertFalse(frappe.db.exists(sdk.DOCTYPE, FAKE))
		registry.sync()                                   # the one writer brings it back
		self.assertTrue(sdk.is_enabled(FAKE))

	def test_stale_job_is_reset_by_sync(self):
		from frappe.utils import add_to_date, now_datetime
		frappe.db.set_value(sdk.DOCTYPE, FAKE, {"job_state": "Installing",
			"job_started_at": add_to_date(now_datetime(), minutes=-45)}, update_modified=False)
		summary = registry.sync()
		self.assertIn(FAKE, summary["reset"])
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual(doc.job_state, "Failed")
		self.assertIn("did not finish", doc.job_message)
		frappe.db.set_value(sdk.DOCTYPE, FAKE, {"job_state": "Installing",
			"job_started_at": now_datetime()}, update_modified=False)
		self.assertNotIn(FAKE, registry.sync()["reset"])    # a fresh job is left alone

	def test_switch_off_is_immediate_everywhere(self):
		self.assertTrue(sdk.is_enabled(FAKE))
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		doc.enabled = 0
		doc.save()
		self.assertFalse(sdk.is_enabled(FAKE))
		with self.assertRaises(frappe.PermissionError):
			sdk.require_enabled(FAKE)
		boot = frappe._dict()
		sdk.extend_bootinfo(boot)
		self.assertEqual(boot.gunshop_extensions[FAKE]["enabled"], False)
		self.assertEqual(boot.gunshop_extensions[FAKE]["version"], "0.3.0")

	def test_require_enabled_roles(self):
		sdk.require_enabled(FAKE)                                     # on, no role demand
		sdk.require_enabled(FAKE, roles=("System Manager",))          # Administrator has it
		with self.assertRaises(frappe.PermissionError):
			sdk.require_enabled(FAKE, roles=("No Such Role",))

	def test_extension_hook_decorator(self):
		calls = []

		@sdk.extension_hook(FAKE)
		def handler(doc, method):
			calls.append(method)
			return "ran"

		self.assertEqual(handler.__gunshop_extension__, FAKE)
		self.assertEqual(handler(None, "on_submit"), "ran")
		frappe.db.set_value(sdk.DOCTYPE, FAKE, "enabled", 0)   # a bypass write: the platform cache still invalidates
		self.assertIsNone(handler(None, "on_submit"))
		self.assertEqual(calls, ["on_submit"])

	def test_endpoints(self):
		payload = api.list_extensions()
		row = next(r for r in payload["extensions"] if r["name"] == FAKE)
		self.assertEqual(row["high_risk"], ["network:api.example.com"])
		self.assertEqual(row["manifest"]["publisher_id"], "testco")
		self.assertEqual(payload["core_version"], registry.core_version())
		self.assertIs(row["effective"], True)
		self.assertEqual(payload["busy_states"], list(jobs.BUSY_STATES))
		self.assertEqual(api.disable_notice(FAKE), "")                # none declared
		frappe.db.set_value(sdk.DOCTYPE, FAKE, "manifest", '{"disable_notice": "ffl_core.extensions.registry.core_version"}')
		self.assertEqual(api.disable_notice(FAKE), "")                # outside the extension's own package → ignored
		frappe.db.set_value(sdk.DOCTYPE, FAKE, "manifest", '{"disable_notice": "ext_fake.notices.x"}')
		self.assertEqual(api.disable_notice(FAKE), "")                # not importable → logged, empty
		row = api.set_enabled(FAKE, 0)
		self.assertEqual(row["enabled"], 0)
		self.assertFalse(sdk.is_enabled(FAKE))
		with self.assertRaises(frappe.ValidationError):
			api.uninstall_from_site(FAKE, "wrong id")
		with self.assertRaises(frappe.ValidationError):
			api.install_on_site(FAKE)                                 # already installed
		refreshed = api.refresh()
		self.assertIn("sync", refreshed)

	def test_endpoints_need_system_manager(self):
		frappe.set_user("Guest")
		try:
			for call in (api.list_extensions, api.refresh, lambda: api.set_enabled(FAKE, 1),
					lambda: api.install_on_site(FAKE), lambda: api.uninstall_from_site(FAKE, FAKE),
					lambda: api.disable_notice(FAKE), lambda: api.install_upload("0" * 64, FAKE),
					lambda: api.inspect_git("https://github.com/acme/ext_fake", "main"),
					lambda: api.upload_failure("0" * 64)):
				with self.assertRaises(frappe.PermissionError):
					call()
		finally:
			frappe.set_user("Administrator")


class TestSyncNotInstalledHere(ExtensionFixture):
	installed_here = False

	def test_row_available_but_not_effective(self):
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.installed_on_site, doc.enabled), (0, 1))     # the row default: on once installed
		self.assertFalse(sdk.is_enabled(FAKE))
		boot = frappe._dict()
		sdk.extend_bootinfo(boot)
		self.assertNotIn(FAKE, boot.gunshop_extensions)
		with self.assertRaises(frappe.ValidationError):
			api.set_enabled(FAKE, 1)
		doc.enabled = 0
		doc.save()                                                          # the store may say off
		doc.enabled = 1
		with self.assertRaises(frappe.ValidationError):                    # but not on, while not installed here
			doc.save()

	def test_install_transition_turns_it_on(self):
		self.installed_here = True
		registry.sync()
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.installed_on_site, doc.enabled), (1, 1))


class TestSyncIncompatible(ExtensionFixture):
	manifest = {**MANIFEST, "requires_core": ">=9.0"}

	def test_incompatible_is_off_and_kept(self):
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.compatible, doc.enabled, doc.installed_on_site), (0, 0, 1))
		self.assertIn("needs core >=9.0", doc.incompatible_reason)
		self.assertFalse(sdk.is_enabled(FAKE))
		doc.enabled = 1
		with self.assertRaises(frappe.ValidationError):
			doc.save()


class TestJobs(ExtensionFixture):
	"""Install / uninstall jobs with the installer and the backup mocked out."""

	def test_install_success_flips_the_row(self):
		from ffl_core.extensions import jobs
		frappe.db.set_value(sdk.DOCTYPE, FAKE, {"installed_on_site": 0, "enabled": 0}, update_modified=False)
		with patch("frappe.installer.install_app") as install, patch.object(frappe, "clear_cache"):
			jobs.install_on_site(FAKE)
		install.assert_called_once_with(FAKE)
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.job_state, doc.installed_on_site, doc.enabled), ("Idle", 1, 1))

	def test_install_failure_records_why_and_rolls_back(self):
		from ffl_core.extensions import jobs

		def boom(app):
			frappe.throw("required app 'nope' is not installed")   # str(e) == "" for frappe.throw

		with patch("frappe.installer.install_app", side_effect=boom), \
				patch.object(frappe.db, "rollback") as rollback:
			with self.assertRaises(frappe.ValidationError):
				jobs.install_on_site(FAKE)
		rollback.assert_called()
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual(doc.job_state, "Failed")
		self.assertIn("required app 'nope'", doc.job_message)     # the frappe.throw text, not ""
		self.assertTrue(doc.job_started_at)
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"extension {FAKE}: Install failed"}))  # Error Log stores the title in `method`  # the card says "see Error Log"

	def test_uninstall_backs_up_then_removes(self):
		from ffl_core.extensions import jobs
		order = []
		with patch("frappe.utils.backups.new_backup", side_effect=lambda **k: order.append("backup")), \
				patch("frappe.installer.remove_app", side_effect=lambda app, **k: order.append(("remove", app))):
			self.installed_here = False                          # sync_app after removal sees it gone
			jobs.uninstall_from_site(FAKE)
		self.assertEqual(order, ["backup", ("remove", FAKE)])
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual((doc.job_state, doc.installed_on_site, doc.enabled), ("Idle", 0, 1))   # the switch is the store's
		self.assertFalse(sdk.is_enabled(FAKE))

	def test_uninstall_failure_leaves_row_honest(self):
		from ffl_core.extensions import jobs
		with patch("frappe.utils.backups.new_backup", side_effect=RuntimeError("disk full")), \
				patch.object(frappe.db, "rollback"):
			with self.assertRaises(RuntimeError):
				jobs.uninstall_from_site(FAKE)
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual(doc.job_state, "Failed")
		self.assertIn("disk full", doc.job_message)
		self.assertEqual(doc.installed_on_site, 1)               # re-synced: still installed

	def test_records_held_counts_real_tables_and_marks_child_tables(self):
		rows = api.records_held("ffl_core")
		by_name = {r["name"]: r for r in rows}
		self.assertIn("POS Extension", by_name)
		self.assertFalse(by_name["POS Extension"]["child"])
		self.assertGreaterEqual(by_name["POS Extension"]["rows"], 1)        # the fixture row
		self.assertTrue(all(isinstance(r["rows"], int) for r in rows))
		self.assertEqual(api.records_held("no_such_app_xyz"), [])

	def test_uninstall_is_refused_while_records_exist(self):
		"""Tables with rows are history a backup does not bring back by itself:
		the endpoint refuses (the page shows the same reason) — turn it off instead."""
		held = [{"name": "Fake Plan", "rows": 3, "child": False}, {"name": "Fake Plan Item", "rows": 7, "child": True}]
		with patch.object(api, "records_held", return_value=held), patch.object(frappe, "enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError) as cm:
				api.uninstall_from_site(FAKE, confirm_id=FAKE)
			enqueue.assert_not_called()
			preview = api.uninstall_preview(FAKE)
		self.assertIn("holds 3 records", str(cm.exception))      # documents, not child rows
		self.assertEqual((preview["records"], preview["blocked"]), (3, True))
		self.assertEqual(frappe.get_doc(sdk.DOCTYPE, FAKE).job_state, "Idle")
		with patch.object(api, "records_held", return_value=[{"name": "Fake Plan", "rows": 0, "child": False}]):
			self.assertFalse(api.uninstall_preview(FAKE)["blocked"])

	def test_enqueue_failure_does_not_leave_busy(self):
		frappe.db.set_value(sdk.DOCTYPE, FAKE, {"installed_on_site": 0, "enabled": 0}, update_modified=False)
		with patch.object(frappe, "enqueue", side_effect=RuntimeError("redis down")), patch.object(api, "_importable_here", return_value=True):
			with self.assertRaises(RuntimeError):
				api.install_on_site(FAKE)
		doc = frappe.get_doc(sdk.DOCTYPE, FAKE)
		self.assertEqual(doc.job_state, "Failed")
		self.assertIn("Could not start", doc.job_message)


class TestEffective(ExtensionFixture):
	"""``enabled`` is only the store's intent; what counts is
	``effective`` = enabled ∧ installed here ∧ compatible."""

	def _row(self):
		return next(r for r in api.list_extensions()["extensions"] if r["name"] == FAKE)

	def test_a_compatibility_round_trip_never_writes_the_switch(self):
		snapshot = registry.switch_snapshot()
		self.assertEqual((self._row()["enabled"], self._row()["effective"]), (1, True))
		self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
		registry.sync()
		self.assertEqual((self._row()["enabled"], self._row()["effective"]), (1, False))
		self.assertFalse(sdk.is_enabled(FAKE))
		self.assertEqual(registry.assert_switches(json.dumps(snapshot))["restored"], [])
		self.manifest = MANIFEST
		registry.sync()
		self.assertEqual((self._row()["enabled"], self._row()["effective"]), (1, True))
		self.assertTrue(sdk.is_enabled(FAKE))

	def test_the_store_cannot_turn_on_what_is_incompatible(self):
		self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
		registry.sync()
		api.set_enabled(FAKE, 0)                                            # off is always allowed
		with self.assertRaises(frappe.ValidationError):
			api.set_enabled(FAKE, 1)
		self.assertEqual(frappe.db.get_value(sdk.DOCTYPE, FAKE, "enabled"), 0)

	def test_busy_states_have_one_definition(self):
		"""Every guard reads jobs.BUSY_STATES; no module spells job states out again."""
		self.assertIn("Upgrading", jobs.BUSY_STATES)
		self.assertEqual(jobs.STATES, ("Idle", *jobs.BUSY_STATES, "Failed"))
		pkg = Path(jobs.__file__).resolve().parents[1]
		for path in sorted({*pkg.joinpath("extensions").glob("*.py"), pkg / "api" / "extensions.py"} - {Path(jobs.__file__).resolve()}):
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
					named = {e.value for e in node.elts if isinstance(e, ast.Constant)} & set(jobs.STATES)
					self.assertLess(len(named), 2, f"{path.name}:{node.lineno} lists job states {sorted(named)} — use jobs.BUSY_STATES")

	def test_job_state_options_are_jobs_states(self):
		spec = json.loads((Path(api.__file__).resolve().parents[1] / "ffl_core" / "doctype" / "pos_extension" / "pos_extension.json").read_text())
		field = next(f for f in spec["fields"] if f["fieldname"] == "job_state")
		self.assertEqual(tuple(field["options"].split("\n")), jobs.STATES)
		self.assertEqual(tuple(frappe.get_meta(sdk.DOCTYPE).get_field("job_state").options.split("\n")), jobs.STATES, "site not migrated")

	def test_every_busy_state_refuses_the_switch_and_uninstall(self):
		for state in jobs.BUSY_STATES:
			frappe.db.set_value(sdk.DOCTYPE, FAKE, "job_state", state, update_modified=False)
			for call in (lambda: api.set_enabled(FAKE, 0), lambda: api.uninstall_from_site(FAKE, FAKE)):
				with self.assertRaises(frappe.ValidationError) as cm:
					call()
				self.assertIn("busy", str(cm.exception))
		self.assertGreaterEqual(api.list_extensions()["busy"], 1)
