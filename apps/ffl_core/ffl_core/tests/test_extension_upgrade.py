"""In-place upgrade and install from a public git repository
on test_extension_sideload's scratch bench: temp sites/ + apps-ext/, a
fake replay, the real registry. No network: DNS answers and the clone's remote
are injected (``sideload._resolve``, ``sideload.GIT_CONFIG``).
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import frappe.utils.backups
from frappe.utils import add_to_date, now_datetime

from ffl_core.api import extensions as api
from ffl_core.extensions import DOCTYPE, jobs, registry, sideload
from ffl_core.tests.test_extension_sideload import APP, EXT_HELLO, ScratchBench, _renamed_zip

URL = "https://github.com/acme/ext_uptest"
BUILD_WHEEL = runpy.run_path(str(EXT_HELLO.parent / "make_ext_hello_upgrade.py"))["build_wheel"]
GUIDE = EXT_HELLO.parents[3] / "docs" / "extensions-runbook.md"   # the operator runbook: the recipes under test
SELF_HOSTING = GUIDE.with_name("self-hosting.md")   # what the messages point at (docs/self-hosting.md#…); its Extensions section links the runbook
# The guide's decision fields: the records the table has no row for, pinned here and applied as written by guide_row
LEGACY_RULE = ("If the entry's `upgrade` or the intent has `reused_rollback_point`, or the volume has `.previous/.<id>.replaced`: "
	"they were left by a removed \"roll forward after a failure\" feature (an earlier test build). "
	"The table below has no row for them -> stop and ask a developer.")
# the Extensions section's decision rows (intent, state, migrate) → the RollbackDecision test that reaches the row on a real path and rolls back by it
ROWS = {
	("none", "done", "any"): "test_a_successful_upgrade_rolls_back_only_on_purpose",
	("none", "staged / failed", "succeeded"): "test_a_deploy_that_migrates_a_staged_upgrade_is_finished_not_rolled_back",
	("none", "staged / failed", "started"): "test_a_migrate_that_fails_is_restored_even_after_a_core_deploy",
	("none", "staged / failed", "not_started"): "test_an_agent_that_fails_before_the_migrate_puts_only_the_code_back",
	("present", "—", "started"): "test_an_interrupted_upgrade_a_migrate_ran_is_restored_to_from",
	("present", "—", "not_started"): "test_an_interrupted_upgrade_nothing_migrated_puts_from_back",
}


class AskADeveloper(AssertionError):
	"""the guide's Extensions section cannot decide from the fields: stop and ask a developer."""


def _rollback_section() -> str:
	text = GUIDE.read_text(encoding="utf-8")
	return text[text.index("### Roll back an upgrade"):text.index("### Troubleshooting a git install")]


# the Extensions section's rollback recipe, the steps Installed.follow_recipe runs, each found by its command
RECIPE_STEPS = {
	"check": "env/bin/python -c 'import $ID; print($ID.__version__)'",     # the tree's version, against Installed Applications
	"replay": "$UP restart backend",                                        # its entrypoint replays the tree: wheels/ → .deps/<id>
	"migrate": "$UP exec -T backend bench --site $SITE migrate",
	"services": "$UP restart websocket queue-short queue-long scheduler",   # the scheduler and the queues come back
}


def _recipe() -> str:
	"""The rollback recipe itself: its numbered steps, up to the unrecorded success."""
	section = _rollback_section()
	return section[section.index("# 1) "):section.index("### The unrecorded success")]


def _recipe_steps() -> list[str]:
	"""RECIPE_STEPS in the order the guide's recipe runs them."""
	recipe = _recipe()
	assert all(command in recipe for command in RECIPE_STEPS.values()), "the recipe lost a step's command"
	return sorted(RECIPE_STEPS, key=lambda step: recipe.index(RECIPE_STEPS[step]))


def _gated_block() -> str:
	"""Steps 5–9 as an operator pastes them: from the comment on their gates to the end of the recipe's code block."""
	recipe = _recipe()
	start = recipe.index("# The gates for steps 5-9 are in the commands")
	return recipe[start:recipe.index("```", start)]


def _success_block() -> str:
	"""The unrecorded success's code block, as pasted."""
	section = _rollback_section()
	success = section[section.index("### The unrecorded success"):section.index("### An interrupted upgrade")]
	start = success.index("```bash\n") + len("```bash\n")
	return success[start:success.index("```", start)]


def _interrupted_tree_block() -> str:
	""""An interrupted upgrade" step 2's code block — the trees put back — as pasted."""
	section = _rollback_section()
	interrupted = section[section.index("### An interrupted upgrade"):]
	start = interrupted.index("```bash\n") + len("```bash\n")
	return interrupted[start:interrupted.index("```", start)]


# deploy/prod/up.sh for "An interrupted upgrade" step 2's block: `exec -T backend bash -c <script>` runs the script right here, on $B
UP_EXEC_STUB = """#!/usr/bin/env bash
[ "$1 $2 $3 $4 $5" = "exec -T backend bash -c" ] && exec bash -c "$6"
echo "up.sh $*: the block makes no such call"; exit 2
"""


def paste_on_bench(text: str, bench: Path) -> tuple[str, str]:
	"""guide lines run by bash as pasted (run_guide) with B = ``bench`` and an up.sh whose `exec -T backend bash -c` runs
	on it, env/bin/python being the Python running this test. Returns (the token $rb the lines left, what they printed)."""
	python = bench / "env" / "bin" / "python"
	if not python.exists():
		python.parent.mkdir(parents=True)
		python.symlink_to(sys.executable)
	with tempfile.TemporaryDirectory(prefix="runbook-") as tmp:
		up = Path(tmp) / "up.sh"
		up.write_text(UP_EXEC_STUB)
		up.chmod(0o755)
		env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "UP": str(up), "SITE": "pos.example.com", "ID": APP, "WHO": "tester", "B": str(bench)}
		out = subprocess.run(["bash", "-c", text + '\nprintf "\\nrb=%s\\n" "$rb"\n'], env=env, capture_output=True, text=True, timeout=60)
		printed, _, token = out.stdout.rpartition("\nrb=")
		return token.strip(), printed + out.stderr


# deploy/prod/up.sh and systemctl as the guide's pasted lines meet them: every call is a line of $LEDGER; the tree's version
# read answers $CODE_VERSION, Installed Applications $DB_VERSION, a migrate exits $MIGRATE_EXIT, finish_upgrade(<id>, 1, '')
# prints $FINISH_OUTPUT (bench execute's own print of the dict); everything else does nothing and exits 0.
UP_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LEDGER"
case "$*" in
	*"print($ID.__version__)"*) [ -z "$CODE_VERSION" ] || printf '%s\\n' "$CODE_VERSION" ;;
	*"'Installed Application'"*) [ -z "$DB_VERSION" ] || printf '%s\\n' "$DB_VERSION" ;;
	"exec -T backend bench --site $SITE migrate") exit "$MIGRATE_EXIT" ;;
	*"sideload.finish_upgrade --args ['$ID', 1, '']") printf '%s\\n' "$FINISH_OUTPUT" ;;
esac
exit 0
"""
SYSTEMCTL_STUB = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "$LEDGER"
"""
# an up.sh / systemctl call → what it is, in the recipe's words
GUIDE_CALLS = (
	("check", f"env/bin/python -c 'import {APP}; print({APP}.__version__)'"),
	("installed_applications", "'Installed Application'"),
	("replay", "restart backend"),
	("wait", "exec -T backend test -f /home/frappe/frappe-bench/.ext-replay.done"),
	("migrate", "exec -T backend bench --site pos.example.com migrate"),
	("services", "restart websocket queue-short queue-long scheduler"),
	("frontend", "restart frontend"),
	("sync", "ffl_core.extensions.registry.sync"),
	("job_state", "'POS Extension'"),
	("logs", "logs --tail 40 backend"),
	("start", "start scheduler queue-short queue-long"),
	("finish", f"sideload.finish_upgrade --args ['{APP}', 1, '']"),
	("record_failure", f"sideload.finish_upgrade --args ['{APP}', 0, "),
	("timer", "systemctl --user start ext-agent.timer"),
)


def run_guide(text: str, *, code_version: str = "", db_version: str = "", migrate_exit: int = 0, finish_output: str = "") -> tuple[list[str], str]:
	"""guide lines run by bash the way a shell with UP / SITE / ID / WHO / B set runs them pasted (bracketed paste: every
	line reaches bash; lines a docker exec swallows are what run_guide's callers leave out), against UP_STUB and a
	systemctl stub. Returns (the calls in order, named by GUIDE_CALLS; what the lines printed)."""
	with tempfile.TemporaryDirectory(prefix="runbook-") as tmp:
		stubs, ledger = Path(tmp), Path(tmp) / "ledger"
		for name, body in (("up.sh", UP_STUB), ("systemctl", SYSTEMCTL_STUB)):
			(stubs / name).write_text(body)
			(stubs / name).chmod(0o755)
		ledger.write_text("")
		env = {"PATH": f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}", "LEDGER": str(ledger), "UP": str(stubs / "up.sh"),
			"SITE": "pos.example.com", "ID": APP, "WHO": "tester", "B": "/home/frappe/frappe-bench", "CODE_VERSION": code_version,
			"DB_VERSION": db_version, "MIGRATE_EXIT": str(migrate_exit), "FINISH_OUTPUT": finish_output}
		out = subprocess.run(["bash", "-c", text], env=env, capture_output=True, text=True, timeout=60)
		calls = []
		for line in ledger.read_text().splitlines():
			names = [name for name, fragment in GUIDE_CALLS if fragment in line]
			assert len(names) == 1, f"a call the recipe does not make, or an ambiguous one: {line!r} → {names}"
			calls.append(names[0])
		return calls, out.stdout + out.stderr


def _guide_tables() -> tuple[dict, list]:
	"""the Extensions section's two tables, read from the guide itself: {(field, "entry" | "intent"): its value when the
	field is missing} and the decision rows (intent, state, migrate, restore, code) — every cell a tuple of its
	" / " alternatives, backticks stripped."""
	defaults, rows = {}, []
	for line in GUIDE.read_text(encoding="utf-8").splitlines():
		if not line.startswith("|"):
			continue
		cells = [c.strip() for c in line.strip().strip("|").split("|")]
		clean = [tuple(part.strip().strip("`") for part in c.split(" / ")) for c in cells]
		if len(cells) == 4 and cells[1] in ("entry `upgrade`", "intent"):
			defaults[(clean[0][0], "intent" if cells[1] == "intent" else "entry")] = clean[3][0]
		elif len(cells) == 5 and cells[0] in ("none", "present"):
			rows.append(tuple(clean))
	return defaults, rows


def guide_row(entry: dict, intent: dict | None = None, *, replaced: bool = False) -> tuple[str, ...]:
	"""the Extensions section's decision row for the fields alone — (intent, state, migrate, restore, code), each cell " / "-joined.
	``entry`` is the manifest entry, ``intent`` the intent file if there is one, ``replaced``: apps-ext/.previous/.<id>.replaced
	exists (LEGACY_RULE)."""
	assert LEGACY_RULE in GUIDE.read_text(encoding="utf-8"), "the guide's Extensions section no longer states LEGACY_RULE"
	if replaced or any(isinstance(record, dict) and "reused_rollback_point" in record for record in (intent, entry.get("upgrade"))):
		raise AskADeveloper((entry, intent, replaced))
	defaults, rows = _guide_tables()
	if intent is None:
		up = entry["upgrade"]
		key = ("none", up["state"], str(up.get("migrate", defaults[("migrate", "entry")])))
	else:
		key = ("present", "", str(intent.get("migrate", defaults[("migrate", "intent")])))
	for row in rows:
		if row[0][0] == key[0] and all(cell[0] in ("any", "—") or value in cell for cell, value in zip(row[1:3], key[1:])):
			return tuple(" / ".join(cell) for cell in row)
	raise AssertionError(f"the guide's Extensions section has no rollback row for {key}")


def guide_decision(entry: dict, intent: dict | None = None, *, replaced: bool = False) -> tuple[bool, str]:
	"""the Extensions section's rollback decision on the fields alone: (restore the backup?, the code goes back to
	"previous" | "from" | "unchanged")."""
	row = guide_row(entry, intent, replaced=replaced)
	return row[3] == "do", row[4]


def _fake_backup(**kw):
	"""frappe's new_backup minus mysqldump: the dump lands where it was asked to — or, asked
	nowhere, where frappe would put it (private/backups, which frappe prunes; not written)."""
	path = kw.get("backup_path_db") or os.path.join(frappe.utils.backups.get_backup_path(), "20260913_010203-test_site-database.sql.gz")
	if kw.get("backup_path_db"):
		Path(path).parent.mkdir(parents=True, exist_ok=True)
		Path(path).write_bytes(b"-- dump")
	return SimpleNamespace(backup_path_db=path)


def _bench_execute_args(text: str) -> list:
	"""How ``bench execute --args`` turns its text into arguments (frappe/commands/utils.py
	execute()): Python eval, and on NameError the whole text as the only argument."""
	try:
		return eval(text)                                           # noqa: S307 — what bench does
	except NameError:
		return [text]


def _digest(*paths: Path) -> str:
	"""sha256 over every file (relative path + bytes) and symlink (target) under ``paths``."""
	h = hashlib.sha256()
	for base in paths:
		h.update(f"<{base.name}>".encode())
		if base.is_symlink() or base.is_file():
			h.update(os.readlink(base).encode() if base.is_symlink() else base.read_bytes())
			continue
		for p in sorted(base.rglob("*")) if base.exists() else ():
			h.update(str(p.relative_to(base)).encode())
			if p.is_symlink():
				h.update(os.readlink(p).encode())
			elif p.is_file():
				h.update(p.read_bytes())
	return h.hexdigest()


class Installed(ScratchBench):
	"""ext_uptest 0.1.0 staged from an upload and finished: row Idle, switch on. ``db`` is the site's database as the guide
	the upgrade guide has to reason about it: the extension version it was last migrated to ("<v> half-way" while a migrate of <v> stopped
	part-way), the version Installed Applications records (``app_version``: install-app and every migrate that got through
	write the code they imported), the documents written, the core's migrations — and every backup keeps a copy
	(``snapshots``). ``deps`` is what apps-ext/.deps/<id> holds: the wheels/ of the tree the last replay ran on."""

	def setUp(self):
		super().setUp()
		self.v1_sha = self.upload()
		sideload.stage(self.v1_sha, APP, "Administrator")
		sideload.mark_done(APP, True, "installed")
		frappe.db.set_value(DOCTYPE, APP, {"job_state": "Idle", "job_message": ""}, update_modified=False)
		self.db, self.image_core, self.snapshots = {"version": "0.1.0", "app_version": "0.1.0", "docs": [], "core": "core 1"}, "core 1", {}
		p = patch("frappe.utils.backups.new_backup", side_effect=self.backup)
		self.new_backup = p.start()
		self.addCleanup(p.stop)
		self.ext = self.tmp / "apps-ext"
		self.backups = self.tmp / "sites" / ".ext-backups" / frappe.local.site / APP
		self.deps = self.wheels(self.ext / APP)
		replay = sideload.run_replay
		p = patch.object(sideload, "run_replay", side_effect=lambda: (replay(), self.replayed())[0])
		p.start()
		self.addCleanup(p.stop)

	@staticmethod
	def wheels(tree: Path) -> str | None:
		"""The tree's wheels/ (a digest), None without one."""
		return _digest(tree / "wheels") if (tree / "wheels").is_dir() else None

	def replayed(self) -> None:
		"""A replay ran on the volume's tree: ext-replay.sh installs its wheels/ into .deps/<id>, or removes .deps/<id>."""
		self.deps = self.wheels(self.ext / APP)

	def backup(self, **kw):
		"""_fake_backup, keeping a copy of ``db`` under the backup's path."""
		out = _fake_backup(**kw)
		self.snapshots[os.path.abspath(out.backup_path_db)] = copy.deepcopy(self.db)
		return out

	def write_doc(self) -> None:
		"""A document written now, named after the last backup taken."""
		self.db["docs"].append(f"written after backup {len(self.snapshots)}")

	def upload_version(self, version: str, *, wheel: bool = False) -> str:
		"""ext_uptest at ``version``; ``wheel``: with wheels/ext_uptest_dep-1.0.0, a library its code imports."""
		arc = self.tmp / f"src-{version}{'-wheel' if wheel else ''}" / f"{APP}.zip"
		if not arc.exists():
			_renamed_zip(arc.parent, version=version)
			if wheel:
				whl = BUILD_WHEEL(arc.parent, f"{APP}_dep", "1.0.0", {f"{APP}_dep/__init__.py": 'GREETING = "hello"\n'})
				with zipfile.ZipFile(arc, "a") as z:
					z.write(whl, f"wheels/{whl.name}")
		return sideload.save_upload(arc.read_bytes())

	def entry(self) -> dict:
		return registry.manifest_file()[APP]

	def row(self):
		return frappe.db.get_value(DOCTYPE, APP, ["job_state", "job_message", "version", "enabled"], as_dict=True)

	def upgrade_to(self, version: str, *, ok: bool = True, message: str = "", wheel: bool = False) -> None:
		"""Both segments of an upgrade, the agent's calls and its migrate included; a document is written after the backup."""
		sideload.stage_upgrade(self.upload_version(version, wheel=wheel), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate(version, completes=ok)
		self.assertEqual(sideload.finish_upgrade(APP, 1 if ok else 0, message)["ok"], ok)

	def decision(self) -> tuple[bool, str]:
		"""guide_decision on what is on the scratch bench now."""
		path = sideload.intent_path(APP)
		return guide_decision(self.entry(), json.loads(path.read_text()) if path.exists() else None,
			replaced=(self.ext / ".previous" / f".{APP}.replaced").exists())

	def migrate(self, version: str, *, completes: bool = True) -> None:
		"""A ``bench migrate`` running ``version`` of the extension, as the core's hooks see it — before_migrate; then,
		when patches and schema sync got through, frappe's Installed Applications row and the core's after_migrate —
		and as ``db`` keeps it."""
		with patch.object(registry, "version_of", side_effect=lambda app, manifest: version if app == APP else ""), \
				patch.dict(frappe.flags, {"in_migrate": True}):
			sideload.note_migrate_started()
			if completes:
				self.installed_application(version)
				sideload.note_migrate_succeeded()
				registry.reset_stale_jobs()
		if completes:
			self.db.update(version=version, app_version=version, core=self.image_core)
		elif version != self.db["version"]:
			self.db["version"] = f"{version} half-way"

	def installed_application(self, version: str) -> None:
		"""frappe's Installed Applications row for the extension, as migrate writes it before after_migrate."""
		name = frappe.db.get_value("Installed Application", {"parent": "Installed Applications", "app_name": APP})
		if name:
			frappe.db.set_value("Installed Application", name, "app_version", version, update_modified=False)
			return
		frappe.get_doc({"doctype": "Installed Application", "parent": "Installed Applications", "parenttype": "Installed Applications",
			"parentfield": "installed_applications", "app_name": APP, "app_version": version, "git_branch": "UNVERSIONED"}).db_insert()
		self.addCleanup(frappe.db.delete, "Installed Application", {"parent": "Installed Applications", "app_name": APP})

	def stale(self) -> None:
		"""30 minutes on: the row's job is older than the sweep's timeout."""
		frappe.db.set_value(DOCTYPE, APP, "job_started_at", add_to_date(now_datetime(), minutes=-45), update_modified=False)

	def later(self, minutes: int):
		"""A clock ahead by ``minutes``: backups are named to the second."""
		return patch("frappe.utils.now_datetime", return_value=now_datetime() + datetime.timedelta(minutes=minutes))

	def restore_v1(self):
		"""Put 0.1.0 back the way docs/self-hosting.md#roll-back-an-upgrade does (tree, entry, row)."""
		shutil.rmtree(self.ext / APP)
		(self.ext / ".previous" / APP).rename(self.ext / APP)
		entry = self.entry()
		sideload.add_manifest_entry(APP, {**entry["previous"], "installed_at": entry["installed_at"], "installed_by": entry["installed_by"]})
		shutil.rmtree(self.backups, ignore_errors=True)
		frappe.db.set_value(DOCTYPE, APP, {"job_state": "Idle", "version": "0.1.0"}, update_modified=False)

	@staticmethod
	def tree_version(tree: Path) -> str | None:
		init = tree / APP / "__init__.py"
		m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text()) if init.exists() else None
		return m.group(1) if m else None

	def roll_back(self, row: tuple[str, str, str]) -> None:
		"""the guide's Extensions section on the scratch bench as it is now: the fields select ``row``, and the recipe
		runs for it (follow_recipe). Then the code is the version the database was last migrated to, the documents written
		after the backup are there exactly when nothing was restored, and the core's migrations are the image's."""
		path = sideload.intent_path(APP)
		intent = json.loads(path.read_text()) if path.exists() else None
		entry = self.entry()
		decided = guide_row(entry, intent, replaced=(self.ext / ".previous" / f".{APP}.replaced").exists())
		self.assertEqual(decided[:3], row, "the path reached another row of the table")
		since = f"written after backup {list(self.snapshots).index((intent or entry)['backup']) + 1}"
		self.assertIn(since, self.db["docs"], "the path writes a document after its backup")
		self.follow_recipe(decided, entry, intent)
		self.assertEqual(self.db["core"], self.image_core, "the core's migrations are the image's")
		self.assertEqual(since in self.db["docs"], decided[3] == "skip", "documents written after the backup: kept exactly when nothing was restored")

	def follow_recipe(self, decided: tuple[str, ...], entry: dict, intent: dict | None) -> None:
		"""the Extensions section's recipe for the decision row ``decided`` (intent, state, migrate, restore, code) on ``db``: step 2
		restores the backup when the row says so, steps 3–4 put the row's tree back; then steps 5–9 run as written, pasted
		whole (run_guide): the check reads that tree's version and Installed Applications, and only the calls
		the gates let through happen, in the order they ran — a check that differs stops the recipe (AskADeveloper: another
		row); the backend restart replays the tree, its wheels/ into .deps; the migrate imports the tree's code — code with
		wheels/ cannot be imported without them, and frappe's remove_orphan_doctypes would delete its DocTypes — and rewrites
		Installed Applications to it; the scheduler, the queues and the timer start on a database the code matches.
		"unchanged" (the unrecorded success) restores and swaps nothing and runs its own block: migrate, finish, then the services."""
		if decided[3] == "do":
			self.db = copy.deepcopy(self.snapshots[(intent or entry)["backup"]])
		if decided[4] == "from":
			self.put_from_back()
		version, trees = {
			"previous": ((entry.get("previous") or {}).get("version"), [self.ext / ".previous" / APP]),
			"from": ((intent or {}).get("from"), [self.ext / APP]),
			"unchanged": (entry.get("version"), [self.ext / APP]),
		}[decided[4]]
		tree = next((tree for tree in trees if self.tree_version(tree) == version), None)
		self.assertIsNotNone(tree, "the tree the code goes back to is on the volume")
		if decided[4] == "unchanged":                                   # its migrate, run again on the tree in place
			calls, out = run_guide(_success_block(), finish_output='{"ok": true}')
			self.assertEqual(calls, ["migrate", "finish", "start", "timer"], out)
			self.db["core"] = self.image_core
		else:
			calls, out = run_guide(_gated_block(), code_version=version, db_version=self.db["app_version"])
			if "replay" not in calls:
				self.assertEqual(calls, ["check", "installed_applications"], out)
				self.assertIn("STOP 5)", out)
				raise AskADeveloper(f"the code is {version}, Installed Applications says {self.db['app_version']}: another row")
			migrated = False
			for call in calls:
				if call == "replay":
					self.deps = self.wheels(tree)
				if call == "migrate":
					if self.wheels(tree) and self.deps != self.wheels(tree):
						self.fail(f"the migrate imports {version} before a replay put its wheels/ back: remove_orphan_doctypes deletes its DocTypes")
					self.db.update(app_version=version, core=self.image_core)
					migrated = True
				if call in ("services", "timer"):
					self.assertTrue(migrated, f"{call} before the migrate")
			self.assertEqual(calls[-1], "timer", f"the check passed, yet the recipe stopped before releasing the pipeline: {calls}\n{out}")
		self.assertEqual(self.db["version"], version, "the code is the version the database was last migrated to")


	def put_from_back(self) -> None:
		""""An interrupted upgrade" step 2's block on the scratch bench, pasted twice: from is in apps-ext/<id> after the
		first paste, which says done and leaves the token; the second paste changes no tree and moves nothing."""
		token, out = paste_on_bench(_interrupted_tree_block(), self.tmp)
		self.assertEqual((token, self.tree_version(self.ext / APP)), ("2", json.loads(sideload.intent_path(APP).read_text())["from"]), out)
		self.assertRegex(out, r"(?m)^2\) done: ")
		trees = _digest(self.ext / APP, self.ext / ".previous", self.ext / ".failed")
		token, again = paste_on_bench(_interrupted_tree_block(), self.tmp)
		self.assertEqual((token, _digest(self.ext / APP, self.ext / ".previous", self.ext / ".failed")), ("2", trees), again)
		self.assertNotIn(" -> ", again, "pasted again, nothing moves")


class UpgradeVerdict(Installed):
	def test_a_newer_version_is_an_allowed_upgrade_with_permission_diff_and_notes(self):
		frappe.db.set_value(DOCTYPE, APP, "permissions", json.dumps(["pos_ui", "files"]), update_modified=False)
		sha = sideload.save_upload(self._with_notes("0.2.0", "0.1.5"))
		up = sideload.inspect(sha)["upgrade"]
		self.assertEqual((up["from"], up["to"], up["allowed"], up["reason"]), ("0.1.0", "0.2.0", True, ""))
		self.assertEqual((up["permissions_added"], up["permissions_removed"]), (["doc_events:Customer"], ["files"]))
		notes = up["release_notes"]
		self.assertLess(notes.index("Version 0.2.0"), notes.index("Version 0.1.5"), "newest first")
		self.assertNotIn("Version 0.1.0", notes, "(from, to]: the installed version's own note is not news")

	def _with_notes(self, version: str, *more: str) -> bytes:
		arc = _renamed_zip(self.tmp / f"src-{version}", version=version)
		with zipfile.ZipFile(arc, "a") as z:
			for v in more:
				z.writestr(f"{APP}/change_log/v0/v{v.replace('.', '_')}.md", f"### Version {v}\n\n- in between\n")
		return arc.read_bytes()

	def test_a_bundled_wheel_may_not_install_over_an_extension_on_this_server(self):
		"""The lint that inspects an upload knows the ids in extensions.json."""
		arc = _renamed_zip(self.tmp / "other", app="ext_other", title="Ext Other")
		(self.tmp / "whl").mkdir()
		whl = BUILD_WHEEL(self.tmp / "whl", "ext_other_dep", "1.0.0", {"ext_other_dep/__init__.py": "", f"{APP}/__init__.py": "X = 1\n"})
		with zipfile.ZipFile(arc, "a") as z:
			z.write(whl, f"wheels/{whl.name}")
		out = sideload.inspect(sideload.save_upload(arc.read_bytes()))
		self.assertFalse(out["ok"])
		self.assertEqual([f["message"] for f in out["findings"] if f["rule"] == "deps"],
			[f"wheels/{whl.name} installs a top-level {APP!r}: the core, this extension or another extension on this server already owns that name"])

	def test_nothing_on_the_bench_means_no_upgrade(self):
		frappe.delete_doc(DOCTYPE, APP, force=True, ignore_permissions=True)
		sideload.remove_manifest_entry(APP)
		shutil.rmtree(self.ext / APP)
		self.assertIsNone(sideload.inspect(self.upload_version("0.2.0"))["upgrade"])

	def test_same_version_downgrade_baked_and_test_source_are_refused_everywhere(self):
		"""Inspect says no with a reason, and install_upload refuses even when called directly."""
		def refused(sha: str, fragment: str):
			up = sideload.inspect(sha)["upgrade"]
			self.assertFalse(up["allowed"], up)
			self.assertIn(fragment, up["reason"])
			with patch.object(frappe, "enqueue") as enqueue, self.assertRaises(frappe.ValidationError) as cm:
				api.install_upload(sha, APP)
			enqueue.assert_not_called()
			self.assertIn(fragment, str(cm.exception))

		refused(self.upload_version("0.1.0"), "already installed")
		refused(self.upload_version("0.0.9"), "older")
		v2 = self.upload_version("0.2.0")
		sideload.add_manifest_entry(APP, {**self.entry(), "source": "test"})
		refused(v2, "test")
		sideload.add_manifest_entry(APP, {**self.entry(), "source": "upload"})
		(self.tmp / "apps" / APP).mkdir()                            # a real directory in apps/ = baked into the image
		refused(v2, "built into")

	def test_install_upload_routes_an_allowed_upgrade_to_stage_upgrade(self):
		sha = self.upload_version("0.2.0")
		sideload.inspect(sha)
		with patch.object(frappe, "enqueue") as enqueue:
			out = api.install_upload(sha, APP)
		self.assertEqual((out["extension"], out["mode"]), (APP, "upgrade"))
		self.assertEqual(enqueue.call_args.args[0], "ffl_core.extensions.sideload.stage_upgrade")
		self.assertEqual(enqueue.call_args.kwargs["sha256"], sha)

	def test_a_fresh_id_is_still_an_install(self):
		arc = _renamed_zip(self.tmp / "other", app="ext_other", title="Ext Other")
		sha = sideload.save_upload(arc.read_bytes())
		self.assertIsNone(sideload.inspect(sha)["upgrade"])
		with patch.object(frappe, "enqueue") as enqueue:
			out = api.install_upload(sha, "ext_other")
		self.assertEqual(out["mode"], "install")
		self.assertEqual(enqueue.call_args.args[0], "ffl_core.extensions.sideload.stage")


class StageUpgrade(Installed):
	def test_segment_one_then_the_agent_segment_upgrades_in_place(self):
		"""As far as the job and the agent's two calls go."""
		old_tree, old_entry = _digest(self.ext / APP), self.entry()
		fields = ["version", "installed_at", "installed_by", "permissions", "manifest", "source", "enabled"]
		row_before = frappe.db.get_value(DOCTYPE, APP, fields, as_dict=True)
		sha = self.upload_version("0.2.0")
		sideload.inspect(sha)
		with patch.object(registry, "sync_app", wraps=registry.sync_app) as sync:
			sideload.stage_upgrade(sha, APP, "Administrator")
		sync.assert_not_called()          # this worker has the old module imported: a sync here was a second History entry

		backup = self.entry()["backup"]
		self.new_backup.assert_called_once_with(ignore_files=True, force=True, backup_path_db=backup)
		self.assertEqual(Path(backup).parent, self.backups, "K1: sites/.ext-backups/<site>/<id>/")
		self.assertRegex(Path(backup).name, r"^\d{8}-\d{6}-database\.sql\.gz$")
		self.assertFalse(Path(backup).is_relative_to(frappe.utils.backups.get_backup_path()), "frappe prunes private/backups within a day")
		self.assertFalse(Path(backup).is_relative_to(self.tmp / "sites" / frappe.local.site / "private"))
		self.assertEqual(_digest(self.ext / ".previous" / APP), old_tree, "the old tree is kept whole")
		self.assertIn('"0.2.0"', (self.ext / APP / APP / "__init__.py").read_text())
		entry = self.entry()
		self.assertEqual((entry["version"], entry["source"], entry["sha256"]), ("0.2.0", "upload", sha))
		self.assertEqual(entry["previous"], {"version": "0.1.0", "source": "upload", "sha256": old_entry["sha256"]})
		self.assertEqual(entry["upgrade"], {"from": "0.1.0", "to": "0.2.0", "state": "staged", "migrate": "not_started"})
		req = sideload.pending_request()
		self.assertEqual((req["action"], req["app"]), ("upgrade", APP))
		row = self.row()
		self.assertEqual(row.job_state, "Staged")
		self.assertIn("ext_uptest 0.2.0 is on the server", row.job_message)
		self.assertTrue(row.job_message.endswith(f"Backup: {backup}"), row.job_message)
		self.assertEqual(frappe.db.get_value(DOCTYPE, APP, fields, as_dict=True), row_before, "segment 1 writes only job_state/job_message")
		self.assertFalse((self.tmp / "uploads" / f"{sha}.zip").exists(), "the upload is consumed")
		self.assertEqual([p.name for p in (self.ext / ".previous").iterdir()], [APP], "no intent or leftovers next to .previous/<id>")

		self.assertEqual(sideload.begin_upgrade(APP), {"ok": True})
		self.assertEqual((self.row().job_state, self.entry()["upgrade"]["migrate"]), ("Upgrading", "started"))
		self.assertTrue(self.row().job_message.endswith(f"Backup: {backup}"), "K3: a card stuck in Upgrading can name its backup too")
		self.migrate("0.2.0")
		with patch.object(registry, "sync_app", wraps=registry.sync_app) as sync:
			self.assertEqual(sideload.finish_upgrade(APP, True, ""), {"ok": True})
		sync.assert_called_once_with(APP)  # the row's one write (one History entry; tests save without versions)
		row = self.row()
		self.assertEqual((row.job_state, row.version, row.enabled), ("Idle", "0.2.0", 1))
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("done", "succeeded"))
		done = sideload.last_done()
		self.assertEqual((done["app"], done["ok"], done["message"]), (APP, True, "upgraded 0.1.0 → 0.2.0"))
		self.assertIsNone(sideload.pending_request())
		self.assertEqual(list(self.backups.iterdir()), [Path(backup)])

	def test_a_failed_replay_puts_the_bench_back_exactly(self):
		"""The new package's replay fails → old tree, old entry, wiring and the older .previous as they were."""
		older = self.ext / ".previous" / APP
		older.mkdir(parents=True)
		(older / "marker").write_text("0.0.1")
		link = self.tmp / "apps" / APP
		link.symlink_to(self.ext / APP)
		pth = self.tmp / f"{APP}.pth"
		pth.write_text(f"{link}\n")
		before = (_digest(self.ext / APP, older, link, pth), json.dumps(self.entry(), sort_keys=True))
		sha = self.upload_version("0.2.0")
		with patch.object(sideload, "run_replay", side_effect=[RuntimeError("ext-replay failed:\nbuilding…\next-replay: ERROR: boom"), ""]) as replay, \
				patch.object(frappe.db, "rollback"):
			with self.assertRaises(RuntimeError):
				sideload.stage_upgrade(sha, APP, "Administrator")
		self.assertEqual(replay.call_count, 2, "the restored tree is replayed again")
		self.assertEqual((_digest(self.ext / APP, older, link, pth), json.dumps(self.entry(), sort_keys=True)), before)
		row = self.row()
		self.assertEqual((row.job_state, row.version), ("Failed", "0.1.0"))
		self.assertEqual(row.job_message, "Upgrade failed: ext-replay failed: … ext-replay: ERROR: boom. The previous version is still in place.",
			"one line (the card shows the first): the log tail's first and last lines")
		self.assertIsNone(sideload.pending_request())
		self.assertEqual(sorted(p.name for p in self.ext.iterdir()), [".previous", APP], "no staging leftovers on the volume")
		self.assertEqual([p.name for p in (self.ext / ".previous").iterdir()], [APP], "the intent went with the undo")
		self.assertEqual(list(self.backups.iterdir()), [], "nothing changed, so this attempt's backup is no rollback point")

	def test_a_failure_after_the_restart_request_withdraws_it(self):
		sha = self.upload_version("0.2.0")
		real_set_state = jobs.set_state

		def fail_on_staged(app, state, message=""):
			if state == "Staged" and "Waiting" in message:
				raise RuntimeError("db went away")
			return real_set_state(app, state, message)

		before = _digest(self.ext / APP)
		with patch.object(sideload, "run_replay", return_value=""), patch.object(jobs, "set_state", side_effect=fail_on_staged), \
				patch.object(frappe.db, "rollback"):
			with self.assertRaises(RuntimeError):
				sideload.stage_upgrade(sha, APP, "Administrator")
		self.assertIsNone(sideload.pending_request(), "the agent must not upgrade what was just undone")
		self.assertEqual(_digest(self.ext / APP), before)

	def test_the_backup_is_taken_before_the_row_changes(self):
		"""The backup is the rollback point: restored, the row must read Idle, not Staged."""
		self.new_backup.side_effect = lambda **kw: (self.assertEqual(self.row().job_state, "Idle"), _fake_backup(**kw))[1]
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.assertEqual(self.row().job_state, "Staged")

	def test_a_failed_backup_touches_nothing(self):
		before = _digest(self.ext)
		self.new_backup.side_effect = RuntimeError("mysqldump: disk full")
		with patch.object(sideload, "run_replay") as replay, patch.object(frappe.db, "rollback"):
			with self.assertRaises(RuntimeError):
				sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		replay.assert_not_called()
		self.assertEqual(_digest(self.ext), before)
		self.assertIn("disk full", self.row().job_message)

	def test_migrate_failure_is_failed_with_the_backup_and_the_old_tree_kept(self):
		sha = self.upload_version("0.2.0")
		sideload.stage_upgrade(sha, APP, "Administrator")
		sideload.begin_upgrade(APP)
		out = sideload.finish_upgrade(APP, False, 'Traceback …\nValidationError: "Ext Uptest Plan" cannot be renamed')
		self.assertFalse(out["ok"])
		row, backup = self.row(), self.entry()["backup"]
		self.assertEqual(row.job_state, "Failed")
		self.assertTrue(row.job_message.startswith("Upgrade failed while updating the database: Traceback"), row.job_message)
		self.assertIn('"Ext Uptest Plan" cannot be renamed', row.job_message.splitlines()[0], "one line: the card shows the first")
		self.assertIn(f"Backup: {backup} — docs/self-hosting.md#roll-back-an-upgrade.", row.job_message)
		self.assertTrue(Path(backup).is_file())
		self.assertTrue((self.ext / ".previous" / APP / APP / "hooks.py").exists())
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "started"))
		done = sideload.last_done()
		self.assertEqual((done["app"], done["ok"]), (APP, False))
		self.assertIn(backup, done["message"])
		self.assertIsNone(sideload.pending_request())
		self.assertEqual(self.decision(), (True, "previous"), "the guide: a migrate that started and failed is restored")

	def test_the_agent_failing_before_the_migrate_says_so(self):
		"""K4: the agent's own failure (restart, replay wait, begin_upgrade) arrives as ok=0 while the row is still Staged."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		out = sideload.finish_upgrade(*_bench_execute_args(json.dumps([APP, 0, "backend never finished its extension replay (600 s)"])))
		self.assertFalse(out["ok"])
		row = self.row()
		self.assertEqual(row.job_state, "Failed")
		self.assertEqual(row.job_message, "Upgrade failed before the database was updated: backend never finished its extension replay (600 s). "
			f"Backup: {self.entry()['backup']} — docs/self-hosting.md#roll-back-an-upgrade.")
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "not_started"))
		self.assertIsNone(sideload.pending_request())
		self.assertEqual(sideload.last_done()["message"], row.job_message)
		self.assertEqual(self.decision(), (False, "previous"), "nothing migrated: the code only")

	def test_the_message_is_made_storable_then_cut_to_its_first_300_characters(self):
		"""K5: bench execute evals json.dumps' \\ud83d\\ude00 into two surrogate halves; a lone half cannot be
		stored at all. The agent puts the error line first, so the cut keeps it."""
		message = "ValidationError: \U0001F600 boom\n" + "File x, line 1\n" * 40
		expected = " ".join(message.split())[:300]
		for version, literal in (("0.2.0", json.dumps([APP, 0, message])), ("0.2.1", json.dumps([APP, 0, message], ensure_ascii=False))):
			sideload.stage_upgrade(self.upload_version(version), APP, "Administrator")
			sideload.begin_upgrade(APP)
			self.assertFalse(sideload.finish_upgrade(*_bench_execute_args(literal))["ok"], literal[:40])
			self.assertIn(f"Upgrade failed while updating the database: {expected}. Backup:", self.row().job_message)
			self.restore_v1()
		sideload.finish_upgrade(APP, 0, "lone \ud83d half")
		self.assertIn("lone � half", self.row().job_message)

	def test_finish_upgrade_takes_ok_as_1_or_0(self):
		"""The agent sends ``["<id>", 1|0, "<msg>"]`` — valid Python, so bench
		execute hands over three arguments, and ``ok`` is read as a number, not ``is True``."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		failed = _bench_execute_args(json.dumps([APP, 0, 'migrate: "Plan" cannot be renamed\nTraceback …']))
		self.assertEqual(len(failed), 3, "1/0 evals cleanly: no one-string fallback")
		self.assertFalse(sideload.finish_upgrade(*failed)["ok"])
		self.assertEqual((self.row().job_state, sideload.last_done()["ok"]), ("Failed", False))
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Upgrading", update_modified=False)
		self.installed_application("0.2.0")                         # the migrate the agent ran again
		self.assertTrue(sideload.finish_upgrade(*_bench_execute_args(json.dumps([APP, 1, ""])))["ok"])
		self.assertEqual((self.row().job_state, self.row().version, sideload.last_done()["ok"]), ("Idle", "0.2.0", True))

	def test_finish_upgrade_survives_bench_execute_argument_parsing(self):
		"""JSON true/false (not the contract, but json.dumps's default for a bool) → NameError
		in bench's eval → the whole text becomes the one argument; still understood."""
		bench_execute_args = _bench_execute_args
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		message = 'ValidationError: "Plan" cannot be renamed\nTraceback …'
		out = sideload.finish_upgrade(*bench_execute_args(json.dumps([APP, False, message])))
		self.assertFalse(out["ok"])
		self.assertIn('"Plan" cannot be renamed Traceback', self.row().job_message)
		self.installed_application("0.2.0")
		for ok in (json.dumps([APP, True, ""]), json.dumps([APP, 1, ""])):
			frappe.db.set_value(DOCTYPE, APP, "job_state", "Upgrading", update_modified=False)
			self.assertTrue(sideload.finish_upgrade(*bench_execute_args(ok))["ok"], ok)
			self.assertEqual(self.row().job_state, "Idle")
		self.assertFalse(sideload.finish_upgrade(APP, "false", "x")["ok"])


class Busy(Installed):
	def test_staged_or_upgrading_refuses_upload_switch_and_uninstall(self):
		sha = self.upload_version("0.2.0")
		frappe.db.set_value(DOCTYPE, APP, "installed_on_site", 1, update_modified=False)
		for state in ("Staged", "Upgrading"):
			frappe.db.set_value(DOCTYPE, APP, "job_state", state, update_modified=False)
			up = sideload.inspect(sha)["upgrade"]
			self.assertFalse(up["allowed"])
			self.assertIn(state, up["reason"])
			with patch.object(frappe, "enqueue") as enqueue:
				for call in (lambda: api.install_upload(sha, APP), lambda: api.set_enabled(APP, 0),
						lambda: api.uninstall_from_site(APP, APP)):
					with self.assertRaises(frappe.ValidationError) as cm:
						call()
					self.assertIn("busy", str(cm.exception))
			enqueue.assert_not_called()
			with self.assertRaises(RuntimeError):
				sideload.stage_upgrade(sha, APP, "Administrator")         # the job checks again
			self.assertEqual(self.row().job_state, state, "a refused job leaves the owner's state alone")

	def test_a_row_taken_by_another_job_during_the_backup_is_left_to_that_job(self):
		def uninstall_starts(**kw):
			frappe.db.set_value(DOCTYPE, APP, "job_state", "Uninstalling", update_modified=False)
			return _fake_backup(**kw)

		self.new_backup.side_effect = uninstall_starts
		before = _digest(self.ext)
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.assertEqual(self.row().job_state, "Uninstalling")
		self.assertEqual(_digest(self.ext), before)
		self.assertEqual(list(self.backups.iterdir()), [], "the backup this job took is removed with it")

	def test_stale_upgrading_row_fails_with_the_rollback_hint_and_consumes_its_request(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.begin_upgrade(APP)
		frappe.db.set_value(DOCTYPE, APP, "job_started_at", add_to_date(now_datetime(), minutes=-45), update_modified=False)
		self.assertIn(APP, registry.reset_stale_jobs())
		row = self.row()
		self.assertEqual(row.job_state, "Failed")
		self.assertIn(f"Backup: {self.entry()['backup']} — docs/self-hosting.md", row.job_message)
		self.assertEqual(self.entry()["upgrade"]["state"], "failed", "recorded: rolled back before the id is uploaded again")
		self.assertIsNone(sideload.pending_request())
		self.assertFalse(sideload.last_done()["ok"])

	def test_stale_staged_upgrade_says_upgrade_not_install(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		frappe.db.set_value(DOCTYPE, APP, "job_started_at", add_to_date(now_datetime(), minutes=-45), update_modified=False)
		registry.reset_stale_jobs()
		msg = self.row().job_message
		self.assertIn("upgrade never finished", msg)
		self.assertNotIn("ext-agent", msg, "after the sweep only a rollback is left — no timer to check")
		self.assertIn(f"The database was not updated. Ask your developer to roll it back. Backup: {self.entry()['backup']} — docs/self-hosting.md#roll-back-an-upgrade.", msg)
		self.assertNotIn("finish it", msg)
		self.assertEqual(self.entry()["upgrade"]["state"], "failed")


class ManifestUnwritable(Installed):
	"""extensions.json cannot be written when an upgrade's outcome is recorded (a full disk).
	The row used to say Failed while the entry stayed staged — refused by every later upgrade, and out of the
	sweep's reach (it only looks at busy rows)."""

	def unwritable(self):
		return patch.object(sideload, "_write_manifest", side_effect=OSError(28, "No space left on device"))

	def staged_and_migrating(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.begin_upgrade(APP)

	def assert_the_sweep_records_it_and_the_next_upload_waits_for_the_rollback(self):
		self.assertEqual(self.entry()["upgrade"]["state"], "staged", "the write failed")
		registry.reset_stale_jobs()
		self.assertEqual(self.entry()["upgrade"]["state"], "failed")
		up = sideload.inspect(self.upload_version("0.3.0"))["upgrade"]
		self.assertFalse(up["allowed"], up)
		self.assertIn("did not finish. Ask your developer to roll it back", up["reason"])

	def test_a_failed_migrate_still_fails_the_row(self):
		self.staged_and_migrating()
		with self.unwritable():
			out = sideload.finish_upgrade(APP, 0, "ValidationError: boom")
		self.assertFalse(out["ok"])
		row = self.row()
		self.assertEqual(row.job_state, "Failed")
		self.assertTrue(row.job_message.startswith("Upgrade failed while updating the database: ValidationError: boom."), row.job_message)
		self.assertFalse(sideload.last_done()["ok"])
		self.assert_the_sweep_records_it_and_the_next_upload_waits_for_the_rollback()

	def test_a_migrate_whose_success_cannot_be_recorded_is_failed_not_left_upgrading(self):
		self.staged_and_migrating()
		self.installed_application("0.2.0")
		with self.unwritable(), patch.object(frappe.db, "rollback"):
			self.assertFalse(sideload.finish_upgrade(APP, 1, "")["ok"])
		row = self.row()
		self.assertEqual(row.job_state, "Failed")
		self.assertIn("No space left on device", row.job_message)
		self.assert_the_sweep_records_it_and_the_next_upload_waits_for_the_rollback()

	def test_a_stale_row_is_failed_even_when_its_entry_cannot_be_written(self):
		self.staged_and_migrating()
		frappe.db.set_value(DOCTYPE, APP, "job_started_at", add_to_date(now_datetime(), minutes=-45), update_modified=False)
		with self.unwritable():
			self.assertEqual(registry.reset_stale_jobs(), [APP])
		self.assertEqual(self.row().job_state, "Failed")
		self.assert_the_sweep_records_it_and_the_next_upload_waits_for_the_rollback()

	def test_the_sweep_leaves_a_staged_entry_to_a_busy_row_or_to_an_interrupted_upgrade(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		registry.reset_stale_jobs()
		self.assertEqual(self.entry()["upgrade"]["state"], "staged", "Staged and not stale: the agent is still to come")
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Failed", update_modified=False)
		sideload.intent_path(APP).write_text("{}")
		registry.reset_stale_jobs()
		self.assertEqual(self.entry()["upgrade"]["state"], "staged", "an intent is the guide's to read")


class Pipeline(Installed):
	"""The restart request is one file for the bench and the agent restarts every
	container, so nothing starts while anything else is under way."""

	def other_busy(self, state="Staged"):
		doc = frappe.get_doc({"doctype": DOCTYPE, "extension_id": "ext_other", "title": "Ext Other", "job_state": state,
			"enabled": 1, "compatible": 1, "installed_on_site": 1}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True))
		return doc

	def test_nothing_starts_while_another_extension_is_busy_or_the_store_waits_to_restart(self):
		frappe.db.set_value(DOCTYPE, APP, "installed_on_site", 1, update_modified=False)
		sha = self.upload_version("0.2.0")
		sideload.inspect(sha)
		other = self.other_busy()
		for holder, fragment in ((lambda: None, "ext_other is Staged"),
				(lambda: (frappe.db.set_value(DOCTYPE, other.name, "job_state", "Failed", update_modified=False),
					sideload.request_restart("install", "ext_other")), "waiting to restart for ext_other (install)")):
			holder()
			with patch.object(frappe, "enqueue") as enqueue, patch.object(api, "_importable_here", return_value=True):
				for call in (lambda: api.install_upload(sha, APP), lambda: api.uninstall_from_site(APP, APP),
						lambda: (frappe.db.set_value(DOCTYPE, APP, "installed_on_site", 0, update_modified=False), api.install_on_site(APP))):
					with self.assertRaises(frappe.ValidationError) as cm:
						call()
					self.assertIn(fragment, str(cm.exception))
					frappe.db.set_value(DOCTYPE, APP, "installed_on_site", 1, update_modified=False)
			enqueue.assert_not_called()
			before = (_digest(self.ext), json.dumps(registry.manifest_file(), sort_keys=True))
			with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
				sideload.stage_upgrade(sha, APP, "Administrator")          # a job that was queued before: refused when it starts
			self.assertEqual((_digest(self.ext), json.dumps(registry.manifest_file(), sort_keys=True)), before)
			self.assertEqual(self.new_backup.call_count, 0)
			self.assertEqual((self.row().job_state, self.row().job_message), ("Idle", ""), "the store was busy, not this id: its row is left alone")
			self.assertIn(fragment, sideload.failure_for(sha)["message"], "the page's staging watch shows why (upload_failure)")

	def test_a_job_refused_while_the_store_is_busy_keeps_the_last_failure_and_its_backup_on_the_row(self):
		"""The refusal used to overwrite the row with "Upgrade failed: The store is busy…" —
		over the message naming the failed upgrade's backup."""
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		failed = self.row().job_message
		self.assertIn(f"Backup: {self.entry()['backup']}", failed)
		sha = self.upload_version("0.3.0")
		self.other_busy()
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(sha, APP, "Administrator")
		self.assertEqual((self.row().job_state, self.row().job_message), ("Failed", failed))
		self.assertIn("ext_other is Staged", sideload.failure_for(sha)["message"])

	def test_a_store_that_turns_busy_during_the_backup_leaves_the_row_and_drops_the_backup(self):
		def another_install_starts(**kw):
			self.other_busy("Installing")
			return _fake_backup(**kw)

		self.new_backup.side_effect = another_install_starts
		sha = self.upload_version("0.2.0")
		before = _digest(self.ext)
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(sha, APP, "Administrator")
		self.assertEqual((self.row().job_state, self.row().job_message), ("Idle", ""))
		self.assertEqual(_digest(self.ext), before)
		self.assertEqual(list(self.backups.iterdir()), [], "not a rollback point for anything")
		self.assertIn("ext_other is Installing", sideload.failure_for(sha)["message"])
		self.assertIn("it started while the database was backed up", sideload.failure_for(sha)["message"])

	def test_a_staging_install_is_refused_too(self):
		sideload.request_restart("upgrade", "ext_other")
		arc = _renamed_zip(self.tmp / "fresh", app="ext_fresh", title="Ext Fresh")
		sha = sideload.save_upload(arc.read_bytes())
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage(sha, "ext_fresh", "Administrator")
		self.assertFalse((self.ext / "ext_fresh").exists())
		self.assertIn("waiting to restart for ext_other", sideload.failure_for(sha)["message"])
		self.assertEqual(sideload.pending_request()["app"], "ext_other")

	def test_a_request_is_never_overwritten_or_consumed_by_another_extension(self):
		sideload.request_restart("upgrade", "ext_other")
		with self.assertRaises(RuntimeError) as cm:
			sideload.request_restart("install", APP)
		self.assertIn("ext_other", str(cm.exception))
		sideload.mark_done(APP, False, "a late finish for another id")
		self.assertEqual(sideload.pending_request()["app"], "ext_other", "mark_done only removes its own request")
		sideload.mark_done("ext_other", True, "installed")
		self.assertIsNone(sideload.pending_request())

	def test_a_late_install_failure_withdraws_its_own_request(self):
		arc = _renamed_zip(self.tmp / "fresh", app="ext_fresh", title="Ext Fresh")
		sha = sideload.save_upload(arc.read_bytes())
		real_set_state = jobs.set_state
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, "ext_fresh", force=True, ignore_permissions=True) if frappe.db.exists(DOCTYPE, "ext_fresh") else None)

		def fail_on_staged(app, state, message=""):
			if state == "Staged":
				raise RuntimeError("db went away")
			return real_set_state(app, state, message)

		with patch.object(jobs, "set_state", side_effect=fail_on_staged), patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage(sha, "ext_fresh", "Administrator")
		self.assertIsNone(sideload.pending_request(), "the agent must not restart the store for code that was just removed")

	def test_an_interrupted_segment_one_leaves_its_intent_for_the_runbook(self):
		"""K2: a job killed between moving the trees and writing the entry (no except clause runs)."""
		with patch.object(sideload, "add_manifest_entry", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		intent = json.loads((self.ext / ".previous" / f"{APP}.intent.json").read_text())
		backup = str(next(self.backups.iterdir()))
		self.assertEqual({k: v for k, v in intent.items() if k != "started_at"}, {"from": "0.1.0", "to": "0.2.0", "backup": backup})
		datetime.datetime.fromisoformat(intent["started_at"])
		self.assertNotIn("upgrade", self.entry(), "the entry was never written")
		frappe.db.set_value(DOCTYPE, APP, "job_started_at", add_to_date(now_datetime(), minutes=-45), update_modified=False)
		registry.reset_stale_jobs()
		msg = self.row().job_message
		self.assertIn("interrupted while the new code was being put in place", msg)
		self.assertIn(f"Backup: {backup} — docs/self-hosting.md#roll-back-an-upgrade.", msg, "not the install message, and the backup is not lost")
		up = sideload.inspect(self.upload_version("0.3.0"))["upgrade"]
		self.assertFalse(up["allowed"])
		self.assertIn("stopped half-way", up["reason"])
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(self.upload_version("0.3.0"), APP, "Administrator")     # a job queued before: its card still names the backup
		self.assertTrue(self.row().job_message.endswith(f"An earlier upgrade of {APP} stopped half-way. Backup: {backup} — docs/self-hosting.md#roll-back-an-upgrade."),
			self.row().job_message)


class RollbackPoint(Installed):
	"""The way back is the version before the upgrade and the backup taken right before it — it moves on
	when the next upgrade is staged, and goes with an uninstall."""

	def test_the_next_upgrade_moves_the_rollback_point_on(self):
		self.upgrade_to("0.2.0")
		first, v2_tree = self.entry()["backup"], _digest(self.ext / APP)
		with self.later(5):
			sideload.stage_upgrade(self.upload_version("0.3.0"), APP, "Administrator")
		entry = self.entry()
		self.assertNotEqual(entry["backup"], first)
		self.assertEqual(list(self.backups.iterdir()), [Path(entry["backup"])], "one backup per id: the rollback point's")
		self.assertEqual(entry["previous"]["version"], "0.2.0")
		self.assertEqual(_digest(self.ext / ".previous" / APP), v2_tree)
		self.assertEqual(sorted(p.name for p in (self.ext / ".previous").iterdir()), [APP])

	def test_a_double_failure_says_where_the_backup_is_and_blocks_the_next_upgrade(self):
		"""The new code could not be taken back out — that is the case that needs the rollback."""
		with patch.object(sideload, "run_replay", side_effect=[RuntimeError("ext-replay failed:\nERROR: boom"), RuntimeError("still broken")]), \
				patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		backup = str(next(self.backups.iterdir()))
		msg = self.row().job_message
		self.assertTrue(msg.startswith("Upgrade failed: "), msg)
		self.assertIn(f"Putting the previous version back failed too — see Error Log. Backup: {backup} — docs/self-hosting.md#roll-back-an-upgrade.", msg)
		self.assertNotIn("still in place", msg)
		self.assertTrue((self.ext / ".previous" / f"{APP}.intent.json").exists())
		self.assertIn("stopped half-way", sideload.inspect(self.upload_version("0.3.0"))["upgrade"]["reason"])

	def test_uninstalling_forgets_the_rollback_point(self):
		"""…and an older test build's .previous/.<id>.replaced tree: left behind, LEGACY_RULE
		would send the next rollback of a reinstalled id to a developer."""
		self.upgrade_to("0.2.0")
		replaced = self.ext / ".previous" / f".{APP}.replaced"
		shutil.copytree(self.ext / ".previous" / APP, replaced)
		with patch("frappe.installer.remove_app"), patch.object(registry, "installed_apps", return_value=set()):
			jobs.uninstall_from_site(APP)
		self.assertFalse(self.backups.exists())
		self.assertFalse((self.ext / ".previous" / APP).exists())
		self.assertFalse(replaced.exists())
		self.assertFalse(set(sideload.ENTRY_UPGRADE_KEYS) & self.entry().keys(), self.entry())
		self.assertEqual(self.entry()["version"], "0.2.0")


class UnfinishedUpgrade(Installed):
	"""Nothing rolls forward over an upgrade that did not finish. Whatever its migrate
	got to, every upload of the id is refused — by the review card's verdict, by install_upload, and by a stage_upgrade job
	queued before — with what to do first; the job moves nothing and leaves the row, and the backup it names, alone."""

	def assert_refused(self, fragment: str, versions: tuple[str, ...] = ("0.2.0", "0.3.0")) -> None:
		for version in versions:                                    # by default the upgrade that did not finish again, and a newer version
			sha = self.upload_version(version)
			up = sideload.inspect(sha)["upgrade"]
			self.assertFalse(up["allowed"], up)
			self.assertIn(fragment, up["reason"])
			self.assertIn("(docs/self-hosting.md#roll-back-an-upgrade) before uploading again.", up["reason"])
			self.assertNotIn("rollback_point", up)
			with patch.object(frappe, "enqueue") as enqueue, self.assertRaises(frappe.ValidationError) as cm:
				api.install_upload(sha, APP)
			enqueue.assert_not_called()
			self.assertIn(fragment, str(cm.exception))
		state = lambda: (_digest(self.ext), json.dumps(registry.manifest_file(), sort_keys=True), sorted(self.backups.iterdir()), self.row(), sideload.pending_request())  # noqa: E731
		before, backups_taken = state(), self.new_backup.call_count
		with patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(sha, APP, "Administrator")         # queued before: refused when it starts
		self.assertEqual(state(), before, "no tree moved; the entry, the backup, the row (and the backup it names) as they were")
		self.assertEqual(self.new_backup.call_count, backups_taken, "no backup of a database the unfinished upgrade may have changed")
		self.assertIn(fragment, sideload.failure_for(sha)["message"], "the page's staging watch shows why (upload_failure)")

	def test_failed_before_the_migrate(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.finish_upgrade(APP, 0, "restart of backend failed")
		self.assertEqual(self.entry()["upgrade"]["migrate"], "not_started")
		self.assert_refused("The upgrade of ext_uptest to 0.2.0 did not finish. Ask your developer to roll it back")

	def test_failed_while_migrating(self):
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started")
		self.assert_refused("did not finish. Ask your developer to roll it back")

	def test_failed_after_the_migrate_succeeded(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		with patch.object(registry, "sync_app", side_effect=RuntimeError("db went away")), patch.object(frappe.db, "rollback"):
			self.assertFalse(sideload.finish_upgrade(APP, 1, "")["ok"])
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "succeeded"))
		self.assert_refused("The upgrade of ext_uptest to 0.2.0 updated the database but did not finish. Ask your developer to finish it")

	def test_a_staged_record_nobody_could_finish(self):
		"""The outcome could not be written (the entry stayed staged) under a row that is not busy, with no intent."""
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		sideload.set_upgrade(APP, state="staged")
		self.assert_refused("did not finish. Ask your developer to roll it back")
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Upgrading", update_modified=False)
		self.assertIn("busy", sideload.inspect(self.upload_version("0.3.0"))["upgrade"]["reason"], "a busy row still owns it")

	def test_a_record_from_a_build_that_rolled_forward(self):
		"""reused_rollback_point, true or false, whatever the state: the guide sends it to a developer."""
		self.upgrade_to("0.2.0")
		for reused in (True, False):
			sideload.add_manifest_entry(APP, {**self.entry(), "upgrade": {**self.entry()["upgrade"], "reused_rollback_point": reused}})
			self.assert_refused("The last upgrade of ext_uptest was recorded by a test build that rolled forward over a failed upgrade. Ask your developer to check it")

	def test_chain_is_refused_at_every_step(self):
		"""① 0.2.0's migrate fails after a patch committed → ② 0.3.0 rolls forward on that backup →
		③ 0.3.0 fails before its migrate → ④ 0.4.0 took a new backup of the half-migrated database and deleted the only
		clean one. The chain now stops at ②; the state ③ left on a store that ran the build rolling forward is refused
		as well, and the clean backup stays the only one."""
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")        # ① failed / started, backup B1
		clean = self.entry()["backup"]
		self.assert_refused("The upgrade of ext_uptest to 0.2.0 did not finish. Ask your developer to roll it back", versions=("0.3.0", "0.4.0"))
		entry = self.entry()                                                         # ③ as that build wrote it: 0.3.0 on B1, failed before its migrate
		sideload.add_manifest_entry(APP, {**entry, "version": "0.3.0",
			"upgrade": {"from": "0.2.0", "to": "0.3.0", "state": "failed", "migrate": "not_started", "reused_rollback_point": True}})
		self.assert_refused("was recorded by a test build that rolled forward over a failed upgrade", versions=("0.4.0", "0.5.0"))
		self.assertEqual(sorted(str(p) for p in self.backups.iterdir()), [clean], "B1 is still the only backup")
		self.assertEqual(self.new_backup.call_count, 1, "no backup was taken after the one before 0.2.0")
		with self.assertRaises(AskADeveloper):
			self.decision()

	def test_rolled_back_or_finished_the_id_may_be_uploaded_again(self):
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		self.restore_v1()                                             # the guide: the entry is `previous` again, with no upgrade record
		self.assertTrue(sideload.inspect(self.upload_version("0.2.0"))["upgrade"]["allowed"])
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		with patch.object(registry, "sync_app", side_effect=RuntimeError("db went away")), patch.object(frappe.db, "rollback"):
			self.assertFalse(sideload.finish_upgrade(APP, 1, "")["ok"])
		self.assertFalse(sideload.inspect(self.upload_version("0.3.0"))["upgrade"]["allowed"])
		self.assertTrue(sideload.finish_upgrade(APP, 1, "")["ok"], "the guide 'the unrecorded success': finished")
		up = sideload.inspect(self.upload_version("0.3.0"))["upgrade"]
		self.assertEqual((up["allowed"], up["from"]), (True, "0.2.0"))


class MigrateRecord(Installed):
	"""The entry's upgrade.migrate is written by whatever runs the new code, before and after."""

	def test_begin_upgrade_records_the_migrate_before_the_card_says_upgrading(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		seen = []
		real_set_state = jobs.set_state
		with patch.object(jobs, "set_state", side_effect=lambda app, state, message="": (seen.append(self.entry()["upgrade"]["migrate"]), real_set_state(app, state, message))):
			sideload.begin_upgrade(APP)
		self.assertEqual(seen, ["started"])
		sideload.finish_upgrade(APP, 0, "x")
		sideload.add_manifest_entry(APP, {k: v for k, v in self.entry().items() if k != "upgrade"})
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Staged", update_modified=False)
		with self.assertRaises(RuntimeError):
			sideload.begin_upgrade(APP)                              # nothing to record: the agent must not migrate
		self.assertEqual(self.row().job_state, "Staged")

	def test_begin_upgrade_that_cannot_record_raises_before_the_row_moves(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		with patch.object(sideload, "_write_manifest", side_effect=OSError(28, "No space left on device")), self.assertRaises(OSError):
			sideload.begin_upgrade(APP)
		self.assertEqual((self.row().job_state, self.entry()["upgrade"]["migrate"]), ("Staged", "not_started"))

	def test_begin_upgrade_leaves_a_migrate_already_on_record(self):
		"""A deploy's migrate recorded succeeded (or started) before the agent came."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.migrate("0.2.0")
		self.assertEqual(self.entry()["upgrade"]["migrate"], "succeeded")
		sideload.begin_upgrade(APP)
		self.assertEqual((self.entry()["upgrade"]["migrate"], self.row().job_state), ("succeeded", "Upgrading"))
		sideload.set_upgrade(APP, migrate="started")
		with patch.object(sideload, "_write_manifest", side_effect=AssertionError("nothing to write")):
			sideload.begin_upgrade(APP)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started")

	def test_any_migrate_of_the_new_code_is_on_record_before_it_touches_the_database(self):
		"""The core's before_migrate: a deploy migrates unconditionally, with or without the agent."""
		self.assertIn("ffl_core.extensions.sideload.note_migrate_started", frappe.get_hooks("before_migrate"))
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.migrate("0.1.0", completes=False)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "not_started", "the old code migrating changes nothing of 0.2.0's")
		self.migrate("", completes=False)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started", "a version that cannot be read counts as the new code")
		sideload.set_upgrade(APP, migrate="not_started")
		self.migrate("0.2.0", completes=False)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started")
		sideload.set_upgrade(APP, state="done", migrate="succeeded")
		self.migrate("0.2.0", completes=False)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "succeeded", "a finished upgrade is left alone")

	def test_a_migrate_that_cannot_record_the_start_fails_before_the_database_changes(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		with patch.object(sideload, "_write_manifest", side_effect=OSError(28, "No space left on device")), \
				patch.object(registry, "version_of", return_value="0.2.0"), self.assertRaises(OSError):
			sideload.note_migrate_started()

	def test_a_manifest_whose_extensions_is_not_an_object_stops_no_migrate(self):
		"""{"schema": 1, "extensions": null} — ext-replay.sh and registry.manifest_file read it as empty —
		made the core's before_migrate raise, which aborts every migrate."""
		for value in (None, [], "x"):
			(self.tmp / "sites" / "extensions.json").write_text(json.dumps({"schema": 1, "extensions": value}))
			with patch.dict(frappe.flags, {"in_migrate": True}):
				sideload.note_migrate_started()
				sideload.note_migrate_succeeded()
			self.assertEqual(sideload._manifest_raw()["extensions"], {}, value)

	def test_after_migrate_records_success_from_installed_applications(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		sideload.begin_upgrade(APP)
		self.installed_application("0.1.0")
		with patch.object(registry, "version_of", return_value="0.2.0"):
			sideload.note_migrate_succeeded()
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started", "Installed Applications still holds 0.1.0: not this migrate's")
		self.installed_application("0.2.0")
		sideload.note_migrate_succeeded()
		self.assertEqual(self.entry()["upgrade"]["migrate"], "succeeded")
		sideload.set_upgrade(APP, migrate="started")
		frappe.db.delete("Installed Application", {"parent": "Installed Applications", "app_name": APP})
		with patch.object(registry, "version_of", return_value="0.2.0"):
			self.assertEqual(sideload._migrated_version(APP), "0.2.0", "no row: the imported code's version")

	def test_sync_records_it_only_inside_a_migrate_and_before_the_sweep_reads_it(self):
		calls = []
		with patch.object(registry, "bench_apps", return_value=[]), patch.object(registry, "installed_apps", return_value={APP}), \
				patch.object(sideload, "note_migrate_succeeded", side_effect=lambda: calls.append("note")), \
				patch.object(registry, "reset_stale_jobs", side_effect=lambda: calls.append("sweep") or []):
			registry.sync()
			self.assertEqual(calls, ["sweep"], "a Refresh or an install-app is no migrate")
			calls.clear()
			with patch.dict(frappe.flags, {"in_migrate": True}):
				registry.sync()
		self.assertEqual(calls, ["note", "sweep"])

	def test_finish_upgrade_records_the_migrate_first_so_a_later_failure_rolls_nothing_back(self):
		"""The migrate succeeded but the rest of finish_upgrade failed — the old table restored it."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		with patch.object(registry, "sync_app", side_effect=RuntimeError("db went away")), patch.object(frappe.db, "rollback"):
			self.assertFalse(sideload.finish_upgrade(APP, 1, "")["ok"])
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "succeeded"))
		self.assertEqual(self.row().job_message, "Upgrade failed after the database was updated (nothing to roll back; finishing it was not recorded): "
			f"db went away. Ask your developer to finish it. Backup: {self.entry()['backup']} — docs/self-hosting.md#roll-back-an-upgrade.")
		self.roll_back(("none", "staged / failed", "succeeded"))
		self.assertTrue(sideload.finish_upgrade(APP, 1, "")["ok"], "the guide 'no record of the success': run it again")
		self.assertEqual((self.row().job_state, self.row().version, self.entry()["upgrade"]["state"]), ("Idle", "0.2.0", "done"))

	def test_finish_upgrade_records_success_only_for_the_version_the_migrate_ran(self):
		"""The migrate that exited 0 ran another version than the record's ``to`` (Installed Applications
		holds it) — not this upgrade's: not done, not succeeded, and the request, which may be another upgrade's, stays."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.1.0")                                        # the code this migrate ran was still 0.1.0
		request = sideload.pending_request()
		out = sideload.finish_upgrade(APP, 1, "")
		self.assertFalse(out["ok"])
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "started"))
		row = self.row()
		self.assertEqual((row.job_state, row.version), ("Failed", "0.1.0"))
		self.assertEqual(row.job_message, "Upgrade failed while updating the database: the migrate ran ext_uptest 0.1.0, not 0.2.0. "
			f"Backup: {self.entry()['backup']} — docs/self-hosting.md#roll-back-an-upgrade.")
		self.assertEqual(out["error"], row.job_message)
		self.assertEqual(sideload.pending_request(), request, "the request is not taken")
		self.assertEqual(sideload.last_done()["message"], "installed", "nor answered")
		self.roll_back(("none", "staged / failed", "started"))

	def test_a_migrate_longer_than_the_sweep_that_then_fails_still_says_while_updating(self):
		"""The sweep turned the Upgrading row Failed, so the old text — by row state — called the
		migrate's failure "before the database was updated" (no restore). The record says it started."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.stale()
		registry.reset_stale_jobs()
		self.assertEqual(self.row().job_state, "Failed")
		self.migrate("0.2.0", completes=False)
		sideload.finish_upgrade(APP, 0, "ValidationError: boom")
		self.assertTrue(self.row().job_message.startswith("Upgrade failed while updating the database: ValidationError: boom."), self.row().job_message)
		self.roll_back(("none", "staged / failed", "started"))

	def test_a_migrate_that_failed_after_the_core_recorded_success_is_taken_back(self):
		"""The core's after_migrate runs before later apps' hooks: one of those failing fails the migrate."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		sideload.finish_upgrade(APP, 0, "ext_uptest.setup.after_migrate: boom")
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "started"))
		self.assertTrue(self.row().job_message.startswith("Upgrade failed while updating the database: ext_uptest.setup.after_migrate: boom."))
		self.roll_back(("none", "staged / failed", "started"))


class RollbackDecision(Installed):
	"""the Extensions section's decision table, read from the guide, against what each path leaves in the fields; rolled back by
	that table (Installed.roll_back), code and database end on the same version with the documents the table keeps."""

	def test_the_table_is_the_one_these_scenarios_cover(self):
		defaults, rows = _guide_tables()
		self.assertEqual(defaults, {("state", "entry"): "—", ("migrate", "entry"): "started", ("migrate", "intent"): "not_started"})
		self.assertEqual([tuple(" / ".join(cell) for cell in row) for row in rows], [
			("none", "done", "any", "do", "previous"),
			("none", "staged / failed", "succeeded", "skip", "unchanged"),
			("none", "staged / failed", "started", "do", "previous"),
			("none", "staged / failed", "not_started", "skip", "previous"),
			("present", "—", "started", "do", "from"),
			("present", "—", "not_started", "skip", "from"),
		])
		self.assertEqual([tuple(" / ".join(cell) for cell in row[:3]) for row in rows], list(ROWS), "every row has a path below")
		for name in ROWS.values():
			self.assertTrue(callable(getattr(self, name, None)), name)

	def test_a_successful_upgrade_rolls_back_only_on_purpose(self):
		self.upgrade_to("0.2.0")
		self.roll_back(("none", "done", "any"))

	def test_a_deploy_that_migrates_a_staged_upgrade_is_finished_not_rolled_back(self):
		"""A deploy kills segment 1 after the entry and before the request, restarts every container on
		the new tree and migrates it; 30 minutes on the sweep used to say "the database was not updated" → no restore,
		0.1.0 back over a database 0.2.0 had migrated."""
		with patch.object(sideload, "request_restart", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		self.assertEqual((self.entry()["upgrade"]["migrate"], sideload.pending_request(), sideload.intent_path(APP).exists()), ("not_started", None, False))
		self.migrate("0.2.0")
		self.assertEqual(self.entry()["upgrade"]["migrate"], "succeeded")
		self.stale()
		registry.reset_stale_jobs()
		self.assertEqual(self.entry()["upgrade"]["state"], "failed")
		msg = self.row().job_message
		self.assertIn("The database was updated; only finishing the upgrade was not recorded. Ask your developer to finish it. "
			f"Backup: {self.entry()['backup']} — docs/self-hosting.md#roll-back-an-upgrade.", msg)
		self.assertNotIn("roll it back", msg, "review-pipeline r1 minor: a restore would lose the documents written since the backup")
		self.roll_back(("none", "staged / failed", "succeeded"))

	def test_an_agent_that_died_after_the_migrate_is_finished_not_rolled_back(self):
		"""The agent's migrate succeeded (after_migrate recorded it) but finish_upgrade never ran."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		self.stale()
		registry.reset_stale_jobs()
		self.assertEqual((self.entry()["upgrade"]["state"], self.entry()["upgrade"]["migrate"]), ("failed", "succeeded"))
		self.roll_back(("none", "staged / failed", "succeeded"))

	def test_a_migrate_that_fails_is_restored_even_after_a_core_deploy(self):
		"""The upgrade failed before the agent migrated; days later a deploy ships a new image, its core
		patches commit, then the extension's patch fails (before_migrate put that on record). The restore takes the core's
		patches back with the extension's; the recipe's migrate before the restart runs them again on the image's core."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.finish_upgrade(APP, 0, "restart of backend failed")
		self.assertEqual(self.decision(), (False, "previous"))
		self.image_core = self.db["core"] = "core 2"                  # the deploy's image, and its core patches committed
		self.migrate("0.2.0", completes=False)
		self.assertEqual(self.entry()["upgrade"]["migrate"], "started")
		self.roll_back(("none", "staged / failed", "started"))

	def test_an_agent_migrate_that_fails_is_restored(self):
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		self.roll_back(("none", "staged / failed", "started"))

	def test_the_30_minute_sweep_of_an_upgrading_row_is_restored(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0", completes=False)                         # killed, or still running
		self.stale()
		registry.reset_stale_jobs()
		self.assertIn("The upgrade did not finish within 30 minutes. The database update may be half done. Ask your developer to roll it back. ",
			self.row().job_message)
		self.roll_back(("none", "staged / failed", "started"))

	def test_the_30_minute_sweep_of_an_upgrading_row_whose_migrate_got_through_says_finish_it(self):
		"""The agent's migrate succeeded, finish_upgrade never ran — the card says what the table says
		(no record of the success: finish it), not a restore that would lose what was written since the backup."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		self.stale()
		registry.reset_stale_jobs()
		msg = self.row().job_message
		self.assertIn("The upgrade did not finish within 30 minutes. The database was updated; only finishing the upgrade was not recorded. "
			"Ask your developer to finish it. ", msg)
		self.assertNotIn("roll it back", msg)
		self.assertEqual(self.decision(), (False, "unchanged"), "the guide: no record of the success — finish it")

	def test_a_record_written_before_run_5b_is_restored(self):
		"""No ``migrate`` in the record: the guide's Extensions section reads "started" (a restore, not trust in something unknown)."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.finish_upgrade(APP, 0, "restart of backend failed")
		entry = self.entry()
		sideload.add_manifest_entry(APP, {**entry, "upgrade": {k: v for k, v in entry["upgrade"].items() if k in ("from", "to", "state")}})
		self.roll_back(("none", "staged / failed", "started"))

	def test_an_agent_that_fails_before_the_migrate_puts_only_the_code_back(self):
		"""K4: the agent's own failure (restart, replay wait, begin_upgrade) before any migrate."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.finish_upgrade(APP, 0, "backend never finished its extension replay (600 s)")
		self.roll_back(("none", "staged / failed", "not_started"))

	def test_the_30_minute_sweep_of_a_staged_row_puts_only_the_code_back(self):
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		self.stale()
		registry.reset_stale_jobs()
		self.assertIn("The database was not updated.", self.row().job_message)
		self.roll_back(("none", "staged / failed", "not_started"))

	def test_an_interrupted_upgrade_a_migrate_ran_is_restored_to_from(self):
		with patch.object(sideload, "add_manifest_entry", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		path = sideload.intent_path(APP)
		self.migrate("0.1.0", completes=False)
		self.assertNotIn("migrate", json.loads(path.read_text()), "from migrating is every deploy's no-op")
		self.migrate("0.2.0", completes=False)
		self.assertEqual(json.loads(path.read_text())["migrate"], "started")
		self.roll_back(("present", "—", "started"))

	def test_an_interrupted_upgrade_nothing_migrated_puts_from_back(self):
		"""The 2nd upgrade's replay fails, the undo puts 0.2.0 back and its replay fails too.
		.previous/<id> holds 0.1.0 (≠ from); the intent says what it was: nothing migrated, from = 0.2.0."""
		self.upgrade_to("0.2.0")
		good, v2_tree, v1_tree = self.entry(), _digest(self.ext / APP), _digest(self.ext / ".previous" / APP)
		with self.later(5), patch.object(sideload, "run_replay", side_effect=[RuntimeError("ext-replay failed:\nERROR: boom"), RuntimeError("still broken")]), \
				patch.object(frappe.db, "rollback"), self.assertRaises(RuntimeError):
			sideload.stage_upgrade(self.upload_version("0.3.0"), APP, "Administrator")
		self.write_doc()
		intent = json.loads(sideload.intent_path(APP).read_text())
		self.assertEqual({k: v for k, v in intent.items() if k not in ("backup", "started_at")}, {"from": "0.2.0", "to": "0.3.0"}, "no migrate has run since")
		self.assertEqual((_digest(self.ext / APP), _digest(self.ext / ".previous" / APP)), (v2_tree, v1_tree), "from (0.2.0) is in place: nothing to swap")
		self.assertEqual(self.entry(), good)
		self.assertNotEqual(intent["backup"], good["backup"], "this attempt's backup: the entry never used it")
		self.roll_back(("present", "—", "not_started"))

	def test_an_upgrade_killed_after_the_copy_puts_from_and_the_older_generation_back(self):
		"""0.2.0 finished, then segment 1 of 0.3.0 is killed after it copied the new tree —
		apps-ext/<id> is 0.3.0, .previous/<id> 0.2.0 (from), .previous/.<id>.older 0.1.0. Step 2's block (put_from_back)
		puts from and the older generation back and 0.3.0 into .failed; pasted again it changes nothing."""
		self.upgrade_to("0.2.0")
		with self.later(5), patch.object(sideload, "add_manifest_entry", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
			sideload.stage_upgrade(self.upload_version("0.3.0"), APP, "Administrator")
		self.write_doc()
		self.assertEqual([self.tree_version(t) for t in (self.ext / APP, self.ext / ".previous" / APP, self.ext / ".previous" / f".{APP}.older")],
			["0.3.0", "0.2.0", "0.1.0"])
		self.roll_back(("present", "—", "not_started"))
		self.assertEqual([self.tree_version(t) for t in (self.ext / APP, self.ext / ".previous" / APP, self.ext / ".failed" / APP)],
			["0.2.0", "0.1.0", "0.3.0"])
		self.assertFalse((self.ext / ".previous" / f".{APP}.older").exists())

	def test_a_previous_version_with_wheels_migrates_only_after_the_backend_replay_put_them_back(self):
		"""0.2.0 bundles a wheel its code imports, 0.3.0 bundles none — its replay removed
		.deps/<id>. Rolled back to 0.2.0, a migrate before backend's replay imports 0.2.0 without the wheel: frappe's
		remove_orphan_doctypes swallows the ImportError and deletes those DocTypes (and the Table Custom Fields on them), or
		a hooks.py import fails the migrate while the recipe said not to restart. The recipe replays first, then migrates."""
		self.upgrade_to("0.2.0", wheel=True)
		self.assertIsNotNone(self.deps, "0.2.0's replay installed its wheels/")
		with self.later(5):
			self.upgrade_to("0.3.0", ok=False, message="ValidationError: boom")
		self.assertIsNone(self.deps, "0.3.0 has no wheels/: its replay removed 0.2.0's")
		self.roll_back(("none", "staged / failed", "started"))

	def test_the_version_check_stops_a_wrong_row_before_the_migrate_rewrites_installed_applications(self):
		"""A migrate rewrites Installed Applications to the code it imports, so a check
		after it always agrees. 0.2.0's migrate went through and only finishing it was not recorded; read as "nothing
		migrated", the recipe would put 0.1.0 back over a database 0.2.0 migrated — the check before the migrate stops it."""
		sideload.stage_upgrade(self.upload_version("0.2.0"), APP, "Administrator")
		self.write_doc()
		sideload.begin_upgrade(APP)
		self.migrate("0.2.0")
		entry = self.entry()
		self.assertEqual(guide_decision(entry), (False, "unchanged"), "the right row: the unrecorded success")
		wrong = next(tuple(" / ".join(cell) for cell in row) for row in _guide_tables()[1] if row[0] == ("none",) and row[2] == ("not_started",))
		with self.assertRaises(AskADeveloper):
			self.follow_recipe(wrong, entry, None)

	def test_records_the_removed_roll_forward_left_go_to_a_developer(self):
		"""LEGACY_RULE: reused_rollback_point in the entry's upgrade or in the intent (any value), or a .replaced tree."""
		self.upgrade_to("0.2.0", ok=False, message="ValidationError: boom")
		entry = self.entry()
		for reused in (True, False):
			with self.assertRaises(AskADeveloper):
				guide_decision({**entry, "upgrade": {**entry["upgrade"], "reused_rollback_point": reused}})
		intent = {"from": "0.2.0", "to": "0.3.0", "backup": entry["backup"], "started_at": "x"}
		for kwargs in ({"intent": {**intent, "reused_rollback_point": False}}, {"intent": intent, "replaced": True}):
			with self.assertRaises(AskADeveloper):
				guide_decision(entry, **kwargs)
		self.assertEqual(guide_decision(entry, intent), (False, "from"), "an intent as it is written now")

	def test_the_runbook_stops_the_pipeline_checks_replays_then_migrates_and_removes_only_this_ids_request(self):
		"""Every cell first
		stops the agent's timer, checks it is not running and stops the scheduler and queues — then reads the fields; the
		recipe checks the tree's version against Installed Applications before anything migrates, restarts backend and
		waits for its replay, migrates, and only then starts the other services and the timer, each saying why; its
		variables are set before their first use; the unrecorded success starts nothing when its migrate fails; the other
		paragraphs point at the steps by their new numbers, the interrupted upgrade's check against `from`; a request is
		removed only when it is this id's; the removed roll-forward is left to LEGACY_RULE."""
		section = _rollback_section()
		stop = section.index("systemctl --user stop ext-agent.timer")
		self.assertLess(section.index("UP=./deploy/prod/up.sh; SITE=<site>; ID=<ext_id>"), section.index("$UP"), "UP= before its first use")
		self.assertLess(stop, section.index("systemctl --user is-active ext-agent.service"))
		self.assertLess(section.index("$UP stop scheduler queue-short queue-long"), section.index("| intent | `state` |"))
		self.assertLess(stop, section.index("| Field | Where |"))
		self.assertEqual(_recipe_steps(), ["check", "replay", "migrate", "services"], "check < restart and replay < migrate < services")
		recipe = _recipe()
		starts = [recipe.index(f"# {n}) ") for n in range(1, 10)]
		self.assertEqual(starts, sorted(starts))
		step = {n: recipe[start:(starts + [len(recipe)])[n]] for n, start in enumerate(starts, 1)}
		self.assertIn(RECIPE_STEPS["check"], step[5])
		self.assertIn("frappe.db.get_value --args \"['Installed Application', {'app_name': '$ID'}, 'app_version']\"", step[5])
		self.assertIn("migrate rewrites Installed Applications from the code it actually imports", step[5], "why the check comes before the migrate")
		self.assertIn('[ "$rb" = 5 ] && rb= && $UP restart backend && until $UP exec -T backend test -f $B/.ext-replay.done; do sleep 5; done && rb=6', step[6])
		self.assertIn(RECIPE_STEPS["migrate"], step[7])
		self.assertIn("remove_orphan_doctypes", step[7], "why the migrate waits for the replay")
		self.assertIn("if core was deployed after the backup", step[7])
		self.assertIn("the scheduler / queues stay stopped", step[7], "a migrate that fails starts nothing")
		for command in (RECIPE_STEPS["services"], "$UP restart frontend", "ffl_core.extensions.registry.sync", "'POS Extension', '$ID', 'job_state'"):
			self.assertIn(command, step[8])
		self.assertIn("systemctl --user start ext-agent.timer", step[9])
		self.assertIn("(recipe step 9; the scheduler and queues are brought up by step 8, after the migrate succeeds)", section)
		self.assertIn("the migrate in step 7 puts them back", section)
		success = section[section.index("### The unrecorded success"):section.index("### An interrupted upgrade")]
		zero, nonzero = success.index("# exit code 0"), success.index("# non-zero")
		self.assertTrue(zero < success.index("$UP start scheduler queue-short queue-long") < nonzero, "services start in the 0 branch only")
		self.assertIn("roll back with the recipe above (its steps 8 and 9 start the services and release the pipeline)", success)
		interrupted = section[section.index("### An interrupted upgrade"):]
		self.assertIn("recipe step 5 check (both version numbers should be the intent's `from`) -> step 6 restart backend and wait for replay -> step 7 migrate -> step 8 start the services and check", interrupted)
		self.assertIn("-> step 9 release the pipeline (steps 5-9 exactly as in the recipe, the gates are in the commands) -> set the card back to Idle (as above, `[ \"$rb\" = 9 ] && ... jobs.set_state`)", interrupted)
		self.assertEqual(section.count("systemctl --user start ext-agent.timer"), 3, "the recipe, the unrecorded success and the paragraph before")
		removals = [line for line in section.splitlines() if "rm -f" in line and ".ext-request.json" in line]
		self.assertEqual(len(removals), 2, removals)
		for line in removals:
			self.assertIn("{ ! grep -qs '\\\"app\\\": \\\"$ID\\\"' sites/.ext-request.json || rm -f sites/.ext-request.json; }", line)
		text = GUIDE.read_text(encoding="utf-8")
		uninstall = text[text.index("### Manual uninstall"):text.index("### Troubleshooting the replay")]
		self.assertIn(" apps-ext/.previous/.$ID.replaced ", uninstall, "the manual uninstall removes what forget_rollback_point does")
		for word, besides in (("reused_rollback_point", 0), (".replaced", uninstall.count(".replaced"))):
			self.assertEqual(text.count(word), 0 if besides is None else text.count(LEGACY_RULE) + besides, word)


class GuideGates(unittest.TestCase):
	"""The Extensions section's two gates are in the commands, not in their comments. Steps 5–9
	and the unrecorded success run as pasted (run_guide): nothing after a gate that did not pass runs — and a pasted line
	that never runs (docker compose exec forwards the terminal, so it swallows what bash has not read yet) lets nothing after
	it run either, whichever line it is."""

	FULL = ["check", "installed_applications", "replay", "wait", "migrate", "services", "frontend", "sync", "job_state", "logs", "timer"]

	@staticmethod
	def commands(block: str) -> list[str]:
		return [line for line in block.splitlines() if line.strip() and not line.startswith("#")]

	def test_every_command_after_a_gate_is_gated_and_nothing_exits(self):
		for block in (_gated_block(), _success_block()):
			first, *rest = self.commands(block)
			self.assertTrue(first.startswith("rb=; "), first)                  # the gate's token is cleared on the gate's own line
			for line in rest:
				self.assertTrue(line.startswith('[ "$rb" = '), f"a command no gate guards: {line}")
			for line in (first, *rest):
				self.assertIsNone(re.search(r"(^|[;&|{]\s*)exit\b", line), f"exit closes an interactive shell: {line}")

	def test_versions_that_differ_run_nothing_after_the_check(self):
		for code, db in (("0.1.0", "0.2.0"), ("0.2.0", "0.2.0 half-way"), ("", ""), ("0.1.0", "")):
			calls, out = run_guide(_gated_block(), code_version=code, db_version=db)
			self.assertEqual(calls, ["check", "installed_applications"], (code, db, out))
			self.assertIn("STOP 5)", out)
			self.assertNotIn("9) steps 5-9 done", out)

	def test_a_migrate_that_exits_non_zero_starts_no_service_and_leaves_the_timer_stopped(self):
		calls, out = run_guide(_gated_block(), code_version="0.1.0", db_version="0.1.0", migrate_exit=1)
		self.assertEqual(calls, ["check", "installed_applications", "replay", "wait", "migrate"], out)
		self.assertIn("STOP 7)", out)
		self.assertNotIn("9) steps 5-9 done", out)

	def test_both_gates_passed_the_recipe_runs_to_the_timer_in_order(self):
		calls, out = run_guide(_gated_block(), code_version="0.1.0", db_version="0.1.0")
		self.assertEqual(calls, self.FULL, out)
		self.assertIn("9) steps 5-9 done", out)
		self.assertNotIn("STOP", out)

	def test_a_pasted_line_that_never_runs_lets_nothing_after_it_run(self):
		lines = _gated_block().splitlines(keepends=True)
		variants = [(f"line {i + 1} swallowed", lines[:i] + lines[i + 1:]) for i in range(len(lines))]
		variants += [(f"lines 1–{i} swallowed", lines[i:]) for i in range(1, len(lines))]
		for name, kept in variants:
			for migrate_exit in (0, 1):
				calls, out = run_guide("".join(kept), code_version="0.1.0", db_version="0.1.0", migrate_exit=migrate_exit)
				ran_before = lambda a, b: a in calls and calls.index(a) < calls.index(b)  # noqa: E731
				with self.subTest(name, migrate_exit=migrate_exit):
					if "replay" in calls:
						self.assertTrue(ran_before("check", "replay") and ran_before("installed_applications", "replay"), calls)
					if "migrate" in calls:
						self.assertTrue(ran_before("replay", "migrate") and ran_before("wait", "migrate"), calls)
					for later in ("services", "frontend", "sync", "job_state", "logs", "timer"):
						if later in calls:
							self.assertTrue(ran_before("migrate", later) and migrate_exit == 0, calls)
					if "timer" in calls:
						self.assertTrue(ran_before("services", "timer") and ran_before("frontend", "timer"), calls)
					if "9) steps 5-9 done" in out:                    # the last line never says done over a pipeline it did not release
						self.assertIn("timer", calls, out)

	def test_a_token_is_used_once_so_a_paste_again_after_a_stop_begins_at_the_check(self):
		# STOP 7), then (a restore, say) the lines are pasted again and the check's line is the one swallowed: the token the
		# failed step took is gone, so its migrate does not run again on a check made before
		block = _gated_block()
		again = "".join(line for line in block.splitlines(keepends=True) if not line.startswith("rb=; "))
		calls, out = run_guide(block + again, code_version="0.1.0", db_version="0.1.0", migrate_exit=1)
		self.assertEqual(calls, ["check", "installed_applications", "replay", "wait", "migrate"], out)

	def test_the_unrecorded_success_starts_services_only_after_finish_upgrade_says_ok(self):
		for migrate_exit, finish, expected, stop in (
			(0, '{"ok": true}', ["migrate", "finish", "start", "timer"], False),
			(0, '{"ok": false, "error": "the migrate ran ext_uptest 0.1.0, not 0.2.0"}', ["migrate", "finish"], True),
			(0, "", ["migrate", "finish"], True),
			(1, '{"ok": true}', ["migrate", "record_failure"], False),
		):
			calls, out = run_guide(_success_block(), migrate_exit=migrate_exit, finish_output=finish)
			self.assertEqual(calls, expected, (migrate_exit, finish, out))
			self.assertEqual("STOP finish_upgrade" in out, stop, out)
		lines = _success_block().splitlines(keepends=True)
		for i in range(len(lines)):
			for migrate_exit in (0, 1):
				calls, out = run_guide("".join(lines[:i] + lines[i + 1:]), migrate_exit=migrate_exit, finish_output='{"ok": true}')
				with self.subTest(f"line {i + 1} swallowed", migrate_exit=migrate_exit):
					if "start" in calls or "timer" in calls:
						self.assertTrue(migrate_exit == 0 and calls[:2] == ["migrate", "finish"], calls)
					if "record_failure" in calls:
						self.assertEqual((migrate_exit, calls), (1, ["migrate", "record_failure"]))


class GuideOptionalWorker(unittest.TestCase):
	"""Every copy-paste recipe that stops, starts or restarts the worker services includes the opt-in local-catalog worker the
	way auto_deploy.sh and ext-agent.sh do: `optional` is set (before the services are touched) from the running services,
	and every line naming the workers carries `$optional`."""

	DEFINITION = 'optional=$("$UP" ps --services --status running | grep -x queue-catalog || true)'

	def test_every_worker_line_carries_optional_and_it_is_defined_first_in_each_recipe(self):
		text = GUIDE.read_text(encoding="utf-8")
		recipes = {
			"rollback": text[text.index("### Roll back an upgrade"):text.index("### Troubleshooting a git install")],
			"manual install": text[text.index("### Manual install"):text.index("### Manual uninstall")],
		}
		for name, recipe in recipes.items():
			with self.subTest(name):
				self.assertIn(self.DEFINITION, recipe)
				defined = recipe.index(self.DEFINITION)
				lines = [m for m in re.finditer(r"\$UP (?:stop|start|restart) [^\n]*queue-short[^\n]*", recipe)]
				self.assertTrue(lines, "the recipe no longer touches the workers")
				for m in lines:
					self.assertIn("$optional", m.group(0), m.group(0))
					self.assertLess(defined, m.start(), "optional is used before it is set")


class GuideAnchors(unittest.TestCase):
	"""Every pointer into the self-hosting guide or the extensions runbook names a heading that exists: the server's
	two constants, the panel's two, the anchors the deploy files and scripts use, and each page's own contents links. Headings are the ones GitHub
	renders: outside fenced code blocks (a `# comment` in a recipe is not one), with its duplicate-slug rule."""

	ROOT = GUIDE.parent.parent
	# files that point into the guide with docs/self-hosting.md#<anchor> or docs/extensions-runbook.md#<anchor>
	POINTER_SCAN = ("deploy", "scripts", "README.md", "CONTRIBUTING.md", "docs",
		"apps/ffl_core/ffl_core/extensions", "apps/ffl_core/ffl_core/ffl_core/page/extensions")

	@staticmethod
	def slug(heading: str) -> str:
		return re.sub(r"\s", "-", re.sub(r"[^\w \-]", "", heading.lower()).strip())

	@classmethod
	def anchors(cls, text: str) -> set:
		seen, found, fence = {}, set(), None
		for line in text.splitlines():
			m = re.match(r"\s*(`{3,}|~{3,})", line)
			if m:
				fence = m.group(1)[0] if fence is None else (None if m.group(1)[0] == fence else fence)
				continue
			h = None if fence else re.match(r"#{1,6} +(.+?)\s*#*$", line)
			if h:
				base = cls.slug(h.group(1))
				n = seen.get(base, 0)
				seen[base] = n + 1
				found.add(base if n == 0 else f"{base}-{n}")
		return found

	def test_anchor_helper_follows_github(self):
		text = "# A\n## Same\n```\n# not a heading\n```\n## Same\n### Configure `.env`\n## Remote MCP connector (optional)\n"
		self.assertEqual(self.anchors(text), {"a", "same", "same-1", "configure-env", "remote-mcp-connector-optional"})

	def test_the_constants_and_the_panel_constants_are_headings_of_the_guide(self):
		from ffl_core.extensions import EXTENSIONS_DOC, ROLLBACK_DOC
		headings = self.anchors(SELF_HOSTING.read_text(encoding="utf-8"))
		panel = (self.ROOT / "apps/ffl_core/ffl_core/ffl_core/page/extensions/extensions.js").read_text(encoding="utf-8")
		for doc in (EXTENSIONS_DOC, ROLLBACK_DOC):
			path, _, anchor = doc.partition("#")
			self.assertEqual(path, "docs/self-hosting.md")
			self.assertIn(anchor, headings, doc)
			self.assertIn(f"'{doc}'", panel, "the panel keeps the same pointer")

	def test_every_pointer_in_the_repo_and_the_contents_links_hit_a_heading(self):
		pages = {"self-hosting.md": SELF_HOSTING, "extensions-runbook.md": GUIDE}
		headings = {name: self.anchors(path.read_text(encoding="utf-8")) for name, path in pages.items()}
		used = {name: {} for name in pages}
		for entry in self.POINTER_SCAN:
			base = self.ROOT / entry
			for f in ([base] if base.is_file() else sorted(base.rglob("*"))):
				if not f.is_file() or "node_modules" in f.parts:
					continue
				try:
					text = f.read_text(encoding="utf-8")
				except (UnicodeDecodeError, OSError):
					continue
				for name, a in re.findall(r"(self-hosting\.md|extensions-runbook\.md)#([\w-]+)", text):
					used[name].setdefault(a, set()).add(str(f.relative_to(self.ROOT)))
		for name, path in pages.items():
			page = re.sub(r"(?ms)^(`{3,}|~{3,}).*?^\1", "", path.read_text(encoding="utf-8"))
			for a in re.findall(r"\]\(#([\w-]+)\)", page):
				used[name].setdefault(a, set()).add(f"docs/{name}")
		self.assertTrue({"reverse-proxy", "start-the-stack-and-create-the-site", "remote-mcp-connector-optional"} <= set(used["self-hosting.md"]),
			"the deploy files point at the guide by anchor")
		self.assertIn("roll-back-an-upgrade", used["extensions-runbook.md"], "the guide's Extensions section links the runbook's recipe")
		missing = {f"{name}#{a}": sorted(w) for name in pages for a, w in used[name].items() if a not in headings[name]}
		self.assertEqual(missing, {})


class GuideInterruptedTrees(unittest.TestCase):
	""""An interrupted upgrade" step 2: the trees are put back by a block pasted whole — whatever point
	segment 1 was killed at (stage_upgrade moves .previous/<id> to .previous/.<id>.older, apps-ext/<id> to .previous/<id>, then
	copies the new tree in), or where an earlier paste stopped — and a second paste changes nothing; a volume the block cannot
	read stops it with no tree moved and no token for step 3. The intent's from is 0.2.0 throughout."""

	OLDER, PREV, FAILED = f".previous/.{APP}.older", f".previous/{APP}", f".failed/{APP}"
	# name, the trees before (path under apps-ext → the version its __init__.py says; "" = none readable),
	# the trees after (path → (version, the path the tree was at before))
	PUT_BACK = (
		("killed after the copy, with an older generation", {APP: "0.3.0", PREV: "0.2.0", OLDER: "0.1.0"},
			{APP: ("0.2.0", PREV), PREV: ("0.1.0", OLDER), FAILED: ("0.3.0", APP)}),
		("killed after the copy, no older generation", {APP: "0.3.0", PREV: "0.2.0"}, {APP: ("0.2.0", PREV), FAILED: ("0.3.0", APP)}),
		("killed before the copy", {PREV: "0.2.0", OLDER: "0.1.0"}, {APP: ("0.2.0", PREV), PREV: ("0.1.0", OLDER)}),
		("killed after moving the older generation aside", {APP: "0.2.0", OLDER: "0.1.0"}, {APP: ("0.2.0", APP), PREV: ("0.1.0", OLDER)}),
		("killed before any move, or put back but not replayed", {APP: "0.2.0", PREV: "0.1.0"}, {APP: ("0.2.0", APP), PREV: ("0.1.0", PREV)}),
		("a half-copied new tree, and a .failed/<id> from an earlier incident", {APP: "", PREV: "0.2.0", OLDER: "0.1.0", FAILED: "0.0.9"},
			{APP: ("0.2.0", PREV), PREV: ("0.1.0", OLDER), FAILED: ("", APP)}),
		("an earlier paste stopped after moving apps-ext/<id> aside", {PREV: "0.2.0", OLDER: "0.1.0", FAILED: "0.3.0"},
			{APP: ("0.2.0", PREV), PREV: ("0.1.0", OLDER), FAILED: ("0.3.0", FAILED)}),
	)
	STOPS = (
		("from is in neither apps-ext/<id> nor .previous/<id>", {APP: "0.3.0", PREV: "0.1.0"}, True),
		("an older generation while .previous/<id> is taken", {APP: "0.2.0", PREV: "0.1.0", OLDER: "0.0.9"}, True),
		("no intent: not an interrupted upgrade, or step 3 ran", {APP: "0.3.0", PREV: "0.2.0"}, False),
	)

	def bench(self, trees: dict, intent: bool = True) -> Path:
		bench = Path(tempfile.mkdtemp(prefix="interrupted-"))
		self.addCleanup(shutil.rmtree, bench, True)
		(bench / "apps-ext" / ".previous").mkdir(parents=True)
		if intent:
			(bench / "apps-ext" / ".previous" / f"{APP}.intent.json").write_text(json.dumps({"from": "0.2.0", "to": "0.3.0", "backup": "/b.sql.gz"}))
		for rel, version in trees.items():
			init = bench / "apps-ext" / rel / APP / "__init__.py"
			init.parent.mkdir(parents=True)
			init.write_text(f'__version__ = "{version}"\n' if version else "# half copied\n")
			(init.parents[1] / "tree").write_text(rel)                     # the tree's own name, wherever it moves
		return bench

	def trees(self, bench: Path) -> dict:
		return {rel: (Installed.tree_version(bench / "apps-ext" / rel) or "", (bench / "apps-ext" / rel / "tree").read_text())
			for rel in (APP, self.PREV, self.OLDER, self.FAILED) if (bench / "apps-ext" / rel).exists()}

	def test_step_2_holds_the_block_gated_like_steps_5_to_9_and_step_3_takes_its_token(self):
		block = _interrupted_tree_block()
		section = _rollback_section()
		interrupted = section[section.index("### An interrupted upgrade"):]
		self.assertTrue(interrupted.index("2. If `migrate` = `started`") < interrupted.index(block) < interrupted.index("3. Delete the intent"), "the block is step 2's")
		lines = [line for line in block.splitlines() if line.strip()]
		self.assertTrue(lines[0].startswith("rb=; $UP exec -T backend bash -c \"cd $B && env/bin/python - $ID <<'PY'"), lines[0])
		self.assertTrue(lines[-1].startswith('" && rb=2 || echo "STOP 2)'), lines[-1])
		for condition in ("if version(tree) != frm:", "if version(prev) != frm:", "if tree.exists():", "if older.exists():", "if prev.exists():"):
			self.assertIn(condition, block)
		for line in lines:
			self.assertIsNone(re.search(r"(^|[;&|{]\s*)exit\b", line), f"exit closes an interactive shell: {line}")
		self.assertIn('3. Delete the intent and **this id\'s** unsent request (only if step 2 left the token): `[ "$rb" = 2 ] && rb= && $UP exec -T backend bash -c', interrupted)

	def test_pasted_once_from_is_in_place_and_pasted_again_nothing_moves(self):
		for name, before, after in self.PUT_BACK:
			with self.subTest(name):
				bench = self.bench(before)
				token, out = paste_on_bench(_interrupted_tree_block(), bench)
				self.assertEqual((token, self.trees(bench)), ("2", after), out)
				self.assertRegex(out, rf"(?m)^2\) done: apps-ext/{APP} 0\.2\.0 \| ")
				token, again = paste_on_bench(_interrupted_tree_block(), bench)
				self.assertEqual((token, self.trees(bench)), ("2", after), again)
				self.assertNotIn(" -> ", again, "pasted again, nothing moves")

	def test_a_volume_the_block_cannot_read_stops_with_no_tree_moved_and_no_token(self):
		for name, before, intent in self.STOPS:
			with self.subTest(name):
				bench = self.bench(before, intent=intent)
				trees = self.trees(bench)
				for paste in (1, 2):
					token, out = paste_on_bench(_interrupted_tree_block(), bench)
					self.assertEqual((token, self.trees(bench)), ("", trees), (paste, out))
					self.assertIn("STOP 2)", out)
					self.assertNotIn(" -> ", out)
					self.assertNotRegex(out, r"(?m)^2\) done: ")
		# step 3 after a stop: no token, so it removes nothing
		bench = self.bench(self.STOPS[0][1])
		step3 = re.search(r"3\. Delete the intent.*?`(\[ \"\$rb\" = 2 \][^`]+)`", _rollback_section(), re.S).group(1)
		token, out = paste_on_bench(_interrupted_tree_block() + step3 + "\n", bench)
		self.assertTrue((bench / "apps-ext" / ".previous" / f"{APP}.intent.json").exists(), out)
		self.assertIn("STOP 3)", out)


# --------------------------------------------------------------------------- git
class Git(ScratchBench):
	def setUp(self):
		super().setUp()
		self.real_resolve = sideload._resolve
		p = patch.object(sideload, "_resolve", return_value=["140.82.112.3"])      # a public answer; tests never ask DNS
		self.resolve = p.start()
		self.addCleanup(p.stop)

	def repo(self, version: str = "0.1.0", tag: str = "v0.1.0", *, extension: bool = True) -> tuple[Path, str]:
		"""A local repository with the renamed fixture (or just a README) committed and tagged."""
		src = self.tmp / f"git-{version}" / APP
		if extension:
			_renamed_zip(src.parent, version=version)
		else:
			src.mkdir(parents=True)
			(src / "README.md").write_text("hello\n")

		def git(*args):
			return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
				cwd=src, check=True, capture_output=True, text=True).stdout.strip()

		git("init", "-q", "-b", "main")
		git("add", "-A")
		git("commit", "-qm", version)
		git("tag", tag)
		return src, git("rev-parse", "HEAD")

	def remote(self, src: Path):
		"""URL → the local repository: the one test injection point into the real clone."""
		return patch.object(sideload, "GIT_CONFIG", (*sideload.GIT_CONFIG, "-c", "protocol.file.allow=always",
			"-c", f"url.file://{src}.insteadOf={URL}", "-c", f"url.file://{src}.insteadOf={URL}.git"))

	def refused(self, url: str, ref: str = "main", fragment: str = ""):
		with patch.object(sideload.subprocess, "run", side_effect=AssertionError("reached git")), \
				self.assertRaises(frappe.ValidationError) as cm:
			sideload.inspect_git(url, ref)
		if fragment:
			self.assertIn(fragment, str(cm.exception))
		return cm.exception


class GitInstall(Git):
	def test_inspect_git_clones_zips_inspects_and_records_the_source(self):
		src, commit = self.repo()
		with self.remote(src):
			out = api.inspect_git(URL + ".git", "v0.1.0")
		self.assertTrue(out["ok"], out["findings"])
		self.assertEqual((out["app_id"], out["version"], out["upgrade"]), (APP, "0.1.0", None))
		self.assertEqual(out["source"], {"type": "git", "url": URL + ".git", "ref": "v0.1.0", "commit": commit})
		self.assertIn("high_risk", out)
		self.assertEqual(json.loads((self.tmp / "uploads" / f"{out['sha256']}.report.json").read_text())["source"], out["source"])
		names = zipfile.ZipFile(self.tmp / "uploads" / f"{out['sha256']}.zip").namelist()
		self.assertFalse([n for n in names if n.split("/")[0] == ".git"], "the clone's .git never reaches the package")

		sideload.stage(out["sha256"], APP, "Administrator")
		entry = registry.manifest_file()[APP]
		self.assertEqual({k: entry.get(k) for k in ("source", "url", "ref", "commit", "sha256")},
			{"source": "git", "url": URL + ".git", "ref": "v0.1.0", "commit": commit, "sha256": None})

	def test_the_same_commit_gives_the_same_upload(self):
		src, _ = self.repo()
		with self.remote(src):
			self.assertEqual(sideload.inspect_git(URL, "main")["sha256"], sideload.inspect_git(URL, "v0.1.0")["sha256"])

	def test_an_upgrade_from_git_records_where_the_previous_version_came_from(self):
		v1, c1 = self.repo()
		with self.remote(v1):
			sideload.stage(sideload.inspect_git(URL, "v0.1.0")["sha256"], APP, "Administrator")
		sideload.mark_done(APP, True, "installed")
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Idle", update_modified=False)
		v2, c2 = self.repo("0.2.0", "v0.2.0")
		with self.remote(v2), patch("frappe.utils.backups.new_backup", side_effect=_fake_backup):
			out = sideload.inspect_git(URL, "v0.2.0")
			self.assertTrue(out["upgrade"]["allowed"], out["upgrade"])
			sideload.stage_upgrade(out["sha256"], APP, "Administrator")
		entry = registry.manifest_file()[APP]
		self.assertEqual((entry["source"], entry["ref"], entry["commit"]), ("git", "v0.2.0", c2))
		self.assertEqual(entry["previous"], {"version": "0.1.0", "source": "git", "url": URL, "ref": "v0.1.0", "commit": c1})

	def test_a_repository_that_is_not_an_extension_says_so(self):
		src, _ = self.repo(extension=False)
		with self.remote(src), self.assertRaises(frappe.ValidationError) as cm:
			sideload.inspect_git(URL, "main")
		self.assertIn("not a POS extension", str(cm.exception))


class GitRefusals(Git):
	"""Each refusal class, before or instead of anything reaching the network."""

	def test_only_https_addresses(self):
		for url in ("http://github.com/acme/ext_uptest", "ssh://git@github.com/acme/ext_uptest.git",
				"git@github.com:acme/ext_uptest.git", "file:///srv/git/ext_uptest", "ext::sh -c touch% /tmp/pwned"):
			self.refused(url, fragment="https")

	def test_credentials_in_the_address(self):
		for url in ("https://user:ghp_secret@github.com/acme/ext_uptest", "https://ghp_secret@github.com/acme/ext_uptest"):
			exc = self.refused(url, fragment="public")
			self.assertNotIn("ghp_secret", str(exc), "the token is never echoed back")

	def test_not_a_repository_address(self):
		for url in ("https://github.com/acme", "https://github.com/acme/ext/tree/main", "https://github.com:8443/acme/ext",
				"https://github.com/acme/ext?x=1", "https://github.com/../..", "https://[::1]/acme/ext"):
			self.refused(url, fragment="https://github.com/<owner>/<repo>")

	def test_private_loopback_link_local_reserved_and_multicast_hosts(self):
		for addr in ("10.0.0.8", "172.16.3.4", "192.168.1.10", "127.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
				"224.0.0.251", "240.0.0.1", "::1", "fe80::1", "fc00::5", "::ffff:127.0.0.1"):
			self.resolve.return_value = ["140.82.112.3", addr]            # one bad answer among good ones is enough
			self.refused(URL, fragment="public")

	def test_literal_private_address_without_dns(self):
		with patch.object(sideload, "_resolve", wraps=self.real_resolve):
			self.refused("https://127.0.0.1/acme/ext_uptest", fragment="public")
			self.refused("https://169.254.169.254/latest/meta-data", fragment="public")

	def test_bad_refs(self):
		for ref in ("", "-upload-pack=touch /tmp/x", "--help", "main branch", "a" * 101, "v1;rm", "ma\nin"):
			self.refused(URL, ref, fragment="branch or tag")

	def test_timeout_is_sixty_seconds(self):
		with patch.object(sideload.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 60)) as run, \
				self.assertRaises(frappe.ValidationError) as cm:
			sideload.inspect_git(URL, "main")
		self.assertEqual(run.call_args.kwargs["timeout"], 60)
		self.assertIn("60 seconds", str(cm.exception))

	def test_a_private_repository_fails_at_once_and_points_to_the_zip(self):
		failed = subprocess.CompletedProcess([], 128, "", "Cloning into '/tmp/x'...\nfatal: could not read Username for 'https://github.com': terminal prompts disabled\n")
		with patch.object(sideload.subprocess, "run", return_value=failed) as run, self.assertRaises(frappe.ValidationError) as cm:
			sideload.inspect_git(URL, "main")
		self.assertIn("zip", str(cm.exception))
		self.assertIn("could not read Username", str(cm.exception))
		cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
		self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_ASKPASS"]), ("0", "/bin/false"))
		self.assertEqual((env["GIT_CONFIG_NOSYSTEM"], env["GIT_CONFIG_GLOBAL"]), ("1", os.devnull), "no system or user git config applies")
		self.assertEqual(cmd[cmd.index("clone"):], ["clone", "--depth", "1", "--single-branch", "--no-tags", "--branch", "main", "--", URL, cmd[-1]])
		settings = [cmd[i + 1] for i, arg in enumerate(cmd[:cmd.index("clone")]) if arg == "-c"]
		self.assertEqual(settings, ["protocol.allow=never", "protocol.https.allow=always", "http.followRedirects=false", "core.symlinks=false",
			"http.curloptResolve=github.com:443:140.82.112.3"])
		self.assertEqual(run.call_count, 1)

	def test_git_connects_only_to_the_addresses_that_were_checked(self):
		"""The host is resolved once, every answer vetted, and exactly those are pinned for
		git (curl's resolve list) — a second, rebinding answer never reaches the clone."""
		self.resolve.side_effect = [["2606:50c0:8000::153", "::ffff:140.82.112.4", "140.82.112.4"], ["127.0.0.1"]]
		failed = subprocess.CompletedProcess([], 128, "", "fatal: repository 'https://github.com/acme/ext_uptest/' not found\n")
		with patch.object(sideload.subprocess, "run", return_value=failed) as run, self.assertRaises(frappe.ValidationError):
			sideload.inspect_git(URL, "main")
		self.assertEqual(self.resolve.call_count, 1, "the pin is the checked answer, not a fresh lookup")
		cmd = run.call_args.args[0]
		self.assertIn("http.curloptResolve=github.com:443:[2606:50c0:8000::153],140.82.112.4", cmd[:cmd.index("clone")])

	def test_symlinks_in_a_repository_never_reach_the_package(self):
		"""core.symlinks=false checks a link out as a small file holding its target; _zip_tree skips real links as a belt."""
		src, _commit = self.repo()
		(src / "passwd").symlink_to("/etc/passwd")
		subprocess.run(["git", "add", "passwd"], cwd=src, check=True)
		subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "link"], cwd=src, check=True)
		with self.remote(src):
			sha = sideload.inspect_git(URL, "main")["sha256"]
		with zipfile.ZipFile(self.tmp / "uploads" / f"{sha}.zip") as z:
			self.assertEqual(z.read("passwd"), b"/etc/passwd", "the link's target path as text, never the file it points to")
		tree = self.tmp / "tree"
		(tree / "pkg").mkdir(parents=True)
		(tree / "pkg" / "a.txt").write_text("a")
		(tree / "pkg" / "hosts").symlink_to("/etc/hosts")
		(tree / "linked-dir").symlink_to(tree / "pkg")
		self.assertEqual(zipfile.ZipFile(io.BytesIO(sideload._zip_tree(tree))).namelist(), ["pkg/a.txt"])

	def test_over_the_unpacked_limit(self):
		src, _ = self.repo()
		with self.remote(src), patch.object(sideload, "MAX_UNPACKED_BYTES", 1000), self.assertRaises(frappe.ValidationError) as cm:
			sideload.inspect_git(URL, "main")
		self.assertIn("larger than", str(cm.exception))
		self.assertFalse(list((self.tmp / "uploads").iterdir()), "nothing was kept")

	def test_inspect_git_needs_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				api.inspect_git(URL, "main")
		finally:
			frappe.set_user("Administrator")

