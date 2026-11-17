"""Sideload: a zip uploaded on the Extensions page becomes an installed
extension without anyone touching the server.

Two halves, because ``install_app`` must never run before every container
can import the code (the moment a site lists an app as installed, a web
worker that cannot import it answers 500 to everything):

1. ``stage`` (long queue, this worker): lint again, move the code onto the
   apps-ext volume, add it to sites/extensions.json, run ext-replay.sh here
   to prove it wires and builds, then leave a request file for the agent.
   Any failure puts the volume and the manifest back exactly as they were.
2. The host agent (deploy/prod/ext-agent.sh, a systemd timer) sees the
   request, makes every process load the code — workers restarted (each
   replays the manifest on start), the backend replayed in place and its
   gunicorn reloaded gracefully, so the POS keeps serving — then runs
   ``finish_install`` in the backend: the same install-on-this-site job the
   page already offers.

An upload whose id is already on the bench is an *upgrade*
when it came from upload or git and its version is newer: ``stage_upgrade``
backs the site up, keeps the old tree in apps-ext/.previous/<id> and swaps the
new one in; the agent reloads, then ``begin_upgrade`` → ``bench migrate`` →
``finish_upgrade``. A public https git repository becomes an upload
through ``inspect_git`` and follows the same path.

The entry's ``upgrade`` record says how far an upgrade got (
docs/self-hosting.md#extensions decides a rollback on these fields alone): ``state`` staged → done |
failed, and ``migrate`` not_started → started → succeeded — "started" is written
before any migrate runs the new code (begin_upgrade for the agent's, the core's
before_migrate hook for any other, e.g. a deploy's), "succeeded" once one has
(the core's after_migrate, finish_upgrade). Nothing is uploaded over an upgrade that did
not finish: it is rolled back, or its unrecorded success finished, by hand first
(unfinished_upgrade).

The rollback point = the entry's ``previous`` + ``backup`` +
apps-ext/.previous/<id>; the backup is sites/.ext-backups/<site>/<id>/, out of
reach of frappe's backup pruning. It moves on when the next upgrade is staged
(the older one is deleted then) and goes when the extension is uninstalled. One
thing runs on the pipeline at a time (``pipeline_busy``): the restart request is
a single file and the agent reloads every process.

Uploads live under the site's private files until staged; the manifest and
the request/done files live in sites/ (the shared volume), the code in
apps-ext/ (the other shared volume) — nothing container-local survives here.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path

import frappe
from frappe import _

from ffl_core.extensions import DOCTYPE, EXTENSIONS_DOC, ROLLBACK_DOC
from ffl_core.extensions.lint import MAX_UNPACKED_BYTES, MAX_ZIP_BYTES, Report, ZipRejected, extract_zip, lint
from ffl_core.extensions import registry
from ffl_core.extensions.updates import SOURCE_TYPE, STORED_SOURCE_TYPE, normalize_source

REQUEST_FILE = ".ext-request.json"
DONE_FILE = ".ext-request.done"
BACKUPS_DIR = ".ext-backups"                  # sites/.ext-backups/<site>/<id>/<YYYYmmdd-HHMMSS>-database.sql.gz
ENTRY_UPGRADE_KEYS = ("previous", "backup", "upgrade")
REPLAY_TIMEOUT = 900
ZIP_ERRORS = (ZipRejected, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error, EOFError)
# Sources an upload may replace in place (the update service: extensions/updates.py). Compare a
# stored source through updates.normalize_source.
UPGRADE_SOURCES = ("upload", "git", SOURCE_TYPE)
MIGRATE_STEPS = ("not_started", "started", "succeeded")   # an upgrade record's ``migrate``
MAX_NOTES_CHARS = 20_000


# --------------------------------------------------------------------------- uploads
def upload_dir() -> Path:
	d = Path(frappe.get_site_path("private", "ext-uploads"))
	d.mkdir(parents=True, exist_ok=True)
	return d


def save_upload(data: bytes) -> str:
	"""Store the bytes under their sha256 (the size cap is the caller's, api.inspect_upload); returns the hex digest."""
	sha = hashlib.sha256(data).hexdigest()
	(upload_dir() / f"{sha}.zip").write_bytes(data)
	for stale in (f"{sha}.failed", f"{sha}.report.json"):
		(upload_dir() / stale).unlink(missing_ok=True)      # a re-upload starts clean
	return sha


def _ref(sha256: str, suffix: str) -> Path:
	"""A file next to an upload, by validated sha256 — never a caller-shaped path."""
	if not (isinstance(sha256, str) and len(sha256) == 64 and all(c in "0123456789abcdef" for c in sha256)):
		frappe.throw(_("Not a valid upload reference."))
	return upload_dir() / f"{sha256}{suffix}"


def upload_path(sha256: str) -> Path:
	p = _ref(sha256, ".zip")
	if not p.exists():
		frappe.throw(_("That upload is gone — choose the file again."), title=_("Upload expired"))
	return p


def inspect(sha256: str, source: dict | None = None) -> dict:
	"""Unpack to a temp dir, lint, and say how the id relates to this bench/site
	(``upgrade``: what installing it over the same id would mean). ``source`` is
	where the bytes came from when that was not a browser upload (inspect_git)."""
	zip_path = upload_path(sha256)
	with tempfile.TemporaryDirectory(prefix="ext-inspect-") as tmp:
		try:
			root = extract_zip(zip_path, Path(tmp))
		except ZIP_ERRORS as e:
			zip_path.unlink(missing_ok=True)
			frappe.throw(_("{0} (encrypted, corrupt or unsafe archive)").format(str(e)[:200] or type(e).__name__), title=_("Cannot use this file"))
		report = lint(root, packaged=True, known_ids=tuple(registry.manifest_file()))
		out = report.as_dict()
		out["upgrade"] = _upgrade(report.app_id, report.version, report.permissions, root / report.app_id)
	out["sha256"] = sha256
	out["size"] = zip_path.stat().st_size
	out.update(_placement(report.app_id))
	if source:
		out["source"] = source
	# content-addressed, so the report cannot go stale: install_upload reads it back
	(upload_dir() / f"{sha256}.report.json").write_text(json.dumps(out))
	return out


def _stored_report(sha256: str) -> dict | None:
	try:
		return json.loads(_ref(sha256, ".report.json").read_text())
	except (OSError, ValueError):
		return None


def inspected(sha256: str) -> dict:
	"""The report ``inspect`` wrote for this upload, re-inspecting only if it is missing."""
	out = _stored_report(sha256)
	if out is None:
		return inspect(sha256)
	# placement and the upgrade verdict can change between clicks; the notes cannot
	notes = (out.get("upgrade") or {}).get("release_notes", "")
	out.update(_placement(out.get("app_id") or ""))
	out["upgrade"] = _upgrade(out.get("app_id") or "", out.get("version") or "", out.get("permissions") or [])
	if out["upgrade"]:
		out["upgrade"]["release_notes"] = notes
	return out


def _placement(app_id: str) -> dict:
	"""Where an id already is: on this bench (any source), installed on this site."""
	if not app_id:
		return {"on_bench": False, "installed_on_site": False, "baked": False}
	baked = (registry.bench_root() / "apps" / app_id).exists() and not (registry.bench_root() / "apps" / app_id).is_symlink()
	return {
		"on_bench": baked or app_id in registry.manifest_file() or registry.ext_dir(app_id).exists(),
		"installed_on_site": app_id in registry.installed_apps(),
		"baked": baked,
	}


def _upgrade(app_id: str, version: str, permissions: list[str], pkg: Path | None = None) -> dict | None:
	"""None when ``app_id`` is not on this bench; else what replacing it with
	``version`` means and whether that is allowed: the id came
	from upload or git, its last upgrade finished (unfinished_upgrade), the new version
	is strictly newer, nothing holds the row, no earlier upgrade is half-way. ``from``
	is the version of the tree on the volume (the manifest entry), the one that moves.
	``pkg`` (the unpacked package) supplies the release notes."""
	from packaging.version import InvalidVersion, Version

	from ffl_core.extensions.jobs import BUSY_STATES

	place = _placement(app_id)
	if not place["on_bench"]:
		return None
	entry = registry.manifest_file().get(app_id) or {}
	row = frappe.db.get_value(DOCTYPE, app_id, ["version", "permissions", "job_state"], as_dict=True) or {}
	busy, interrupted = row.get("job_state") in BUSY_STATES, intent_path(app_id).exists()
	unfinished = unfinished_upgrade(entry, settled=not busy and not interrupted)
	old = str(entry.get("version") or "") or row.get("version") or ""
	try:
		old_permissions = set(json.loads(row.get("permissions") or "[]"))
	except ValueError:
		old_permissions = set()
	source = "baked" if place["baked"] else normalize_source(str(entry.get("source") or "unknown"))
	try:
		new, current = Version(version), Version(old)
	except InvalidVersion:
		new = current = None
	if source == "baked":
		reason = _("{0} is built into this POS image; it is updated with the POS itself.").format(app_id)
	elif source not in UPGRADE_SOURCES:
		reason = _("{0} was not installed from an upload, git or the update service (source: {1}); upgrade it the way it was installed ({2}).").format(app_id, source, EXTENSIONS_DOC)
	elif new is None:
		reason = _("Cannot compare version {0} with the installed {1}.").format(version or "?", old or "?")
	elif unfinished and "reused_rollback_point" in unfinished:
		reason = _("The last upgrade of {0} was recorded by a test build that rolled forward over a failed upgrade. Ask your "
			"developer to check it ({1}) before uploading again.").format(app_id, ROLLBACK_DOC)
	elif unfinished and migrate_of(unfinished) == "succeeded":
		reason = _("The upgrade of {0} to {1} updated the database but did not finish. Ask your developer to finish it "
			"({2}) before uploading again.").format(app_id, unfinished.get("to") or old, ROLLBACK_DOC)
	elif unfinished:
		reason = _("The upgrade of {0} to {1} did not finish. Ask your developer to roll it back ({2}) "
			"before uploading again.").format(app_id, unfinished.get("to") or old, ROLLBACK_DOC)
	elif new == current:
		reason = _("{0} {1} is already installed.").format(app_id, old)
	elif new < current:
		reason = _("{0} is older than the installed {1}; going back is a restore, not an upload ({2}).").format(version, old, ROLLBACK_DOC)
	elif busy:
		reason = _("{0} is busy ({1}). Try again when that finishes.").format(app_id, row.get("job_state"))
	elif interrupted:
		reason = _("An earlier upgrade of {0} stopped half-way (apps-ext/.previous/{1} is still there). Ask your developer to "
			"finish putting the previous version back ({2}) before uploading again.").format(app_id, intent_path(app_id).name, ROLLBACK_DOC)
	else:
		reason = ""
	new_permissions = set(permissions or [])
	return {
		"from": old, "to": version, "allowed": not reason, "reason": reason,
		"permissions_added": sorted(new_permissions - old_permissions),
		"permissions_removed": sorted(old_permissions - new_permissions),
		"release_notes": _release_notes(pkg, old, version) if pkg else "",
	}


def _release_notes(pkg: Path, after: str, upto: str) -> str:
	"""The package's change_log notes (lint's layout: change_log/v<major>/v<x_y_z>.md)
	for versions in (after, upto], newest first, as one markdown text."""
	from packaging.version import InvalidVersion, Version

	notes = []
	for f in (pkg / "change_log").glob("v*/v*.md"):
		try:
			v = Version(f.stem[1:].replace("_", "."))
			if Version(after) < v <= Version(upto):
				notes.append((v, f.read_text(encoding="utf-8", errors="replace").strip()))
		except InvalidVersion:
			continue
	return "\n\n".join(text for _v, text in sorted(notes, reverse=True))[:MAX_NOTES_CHARS]


def _source_entry(sha256: str) -> dict:
	"""What extensions.json records about where an upload came from: the git
	address inspect_git wrote into its report, the update service's version
	(api.extensions.fetch_update), else the upload's sha256."""
	src = (_stored_report(sha256) or {}).get("source") or {}
	if src.get("type") == "git":
		return {"source": "git", "url": src.get("url"), "ref": src.get("ref"), "commit": src.get("commit")}
	if normalize_source(src.get("type")) == SOURCE_TYPE:
		return {"source": STORED_SOURCE_TYPE, "version": src.get("version"), "sha256": sha256}
	return {"source": "upload", "sha256": sha256}


def _now_iso() -> str:
	return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _unpack_checked(zip_path: Path, dest: Path, app_id: str) -> tuple[Path, Report]:
	"""Unpack and lint again at staging time: anything but a clean package with the confirmed id is refused."""
	root = extract_zip(zip_path, dest)
	report = lint(root, packaged=True, known_ids=tuple(registry.manifest_file()))
	if not report.ok or report.app_id != app_id:
		raise RuntimeError(_("The package no longer passes the checks ({0} errors).").format(len(report.errors)))
	return root, report


def _entry(sha256: str, report: Report, user: str, **more) -> dict:
	"""The extensions.json entry for a staged package; an upgrade adds previous / backup / upgrade."""
	return {"version": report.version, **_source_entry(sha256), "installed_at": _now_iso(), "installed_by": user, **more}


# --------------------------------------------------------------------------- rollback point
def backup_dir(app_id: str) -> Path:
	"""Where an upgrade's database backup lives: the sites volume, outside private/backups
	(frappe deletes files there after 23 h and keeps three sets)."""
	return registry.sites_dir() / BACKUPS_DIR / frappe.local.site / app_id


def intent_path(app_id: str) -> Path:
	"""{from, to, backup, started_at}, written before stage_upgrade moves a tree and removed once the manifest
	entry says the same — left behind, it is what a killed job was doing (a migrate that ran new code meanwhile
	adds ``"migrate": "started"``, note_migrate_started)."""
	return registry.ext_dir(".previous") / f"{app_id}.intent.json"


def _backup_note(backup: str | None) -> str:
	return _("Backup: {0}").format(backup) if backup else _("No database backup was recorded")


def rollback_hint(app_id: str, backup: str | None = None) -> str:
	"""Where the rollback point's backup is and which recipe puts the old version back (by hand).
	Every message carrying a backup says ``Backup: <absolute path>`` (K3)."""
	if backup is None:
		try:
			backup = json.loads(intent_path(app_id).read_text()).get("backup")
		except (OSError, ValueError, AttributeError):
			backup = (registry.manifest_file().get(app_id) or {}).get("backup")
	return _("{0} — {1}.").format(_backup_note(backup), ROLLBACK_DOC)


def set_upgrade(app_id: str, **fields) -> bool:
	"""Write ``fields`` into the entry's ``upgrade`` record (``state`` staged → done | failed, ``migrate``
	not_started → started → succeeded). False when there is no record: nothing written."""
	data = _manifest_raw()
	record = (data["extensions"].get(app_id) or {}).get("upgrade")
	if not isinstance(record, dict):
		return False
	if any(record.get(k) != v for k, v in fields.items()):
		record.update(fields)
		_write_manifest(data)
	return True


def migrate_of(record) -> str:
	"""How far an upgrade record's new code got through a migrate. An older record has no
	``migrate``: "started", the way docs/self-hosting.md#extensions reads it (a restore, not trust in something unknown)."""
	value = record.get("migrate") if isinstance(record, dict) else None
	return value if value in MIGRATE_STEPS else "started"


def unfinished_upgrade(entry: dict, *, settled: bool = True) -> dict | None:
	"""The entry's upgrade record if that upgrade did not finish, else None: nothing may be uploaded for the id until it
	is rolled back — or its unrecorded success finished — by hand (docs/self-hosting.md#extensions). ``failed``;
	``staged`` when ``settled`` (no job holds the row and no intent is left: an outcome nobody could record); or any
	record carrying ``reused_rollback_point``, which only the removed roll-forward over a failed upgrade wrote."""
	record = entry.get("upgrade")
	if not isinstance(record, dict):
		return None
	state = record.get("state")
	return record if state == "failed" or (settled and state == "staged") or "reused_rollback_point" in record else None


def migrate_note(record) -> str:
	"""What an upgrade record's ``migrate`` says about the database, for a card."""
	return {
		"not_started": _("The database was not updated."),
		"started": _("The database update may be half done."),
		"succeeded": _("The database was updated; only finishing the upgrade was not recorded."),
	}[migrate_of(record)]


def upgrade_ask(record) -> str:
	"""The way on for an unfinished upgrade, in the words the upload's refusal uses (``_upgrade``): a migrate that got
	through is finished by hand (docs/self-hosting.md#extensions 'no record of the success') — a restore would lose what was written since
	the backup — any other is rolled back."""
	return "Ask your developer to finish it." if migrate_of(record) == "succeeded" else "Ask your developer to roll it back."


def _unfinished(record) -> bool:
	return isinstance(record, dict) and record.get("state") in ("staged", "failed")


def note_migrate_started() -> None:
	"""hooks before_migrate (core): a ``bench migrate`` is about to run the code on this bench — the agent's
	(begin_upgrade wrote this already) or any other, such as a deploy's unconditional one. On record before the
	database is touched, so docs/self-hosting.md#extensions never reads "not_started" over a database a migrate has changed: an
	unfinished upgrade (``staged`` / ``failed``) still "not_started" whose ``to`` is the code this process imports
	moves to "started"; an interrupted upgrade's intent gets ``"migrate": "started"`` unless that code is its
	``from``. A version that cannot be read counts as the new code. A write that fails stops the migrate."""
	data = _manifest_raw()
	moved = [app_id for app_id, entry in data["extensions"].items()
		if isinstance(entry, dict) and _unfinished(entry.get("upgrade")) and entry["upgrade"].get("migrate") == "not_started"
		and registry.version_of(app_id, {}) in ("", entry["upgrade"].get("to"))]
	for app_id in moved:
		data["extensions"][app_id]["upgrade"]["migrate"] = "started"
	if moved:
		_write_manifest(data)
	for path in sorted(registry.ext_dir(".previous").glob("*.intent.json")):
		try:
			intent = json.loads(path.read_text())
		except (OSError, ValueError):
			continue
		if isinstance(intent, dict) and intent.get("migrate") != "started" and registry.version_of(path.name[: -len(".intent.json")], {}) != intent.get("from"):
			tmp = path.with_name(path.name + ".tmp")
			tmp.write_text(json.dumps({**intent, "migrate": "started"}, indent=2))
			os.replace(tmp, path)


def note_migrate_succeeded() -> None:
	"""registry.sync inside a migrate (the core's after_migrate): patches and schema sync are done, and frappe has just
	rewritten Installed Applications from the code this migrate ran. An unfinished upgrade whose ``to`` is the version
	recorded there (the imported one if none is) moves to "succeeded". Only inside a migrate: install-app and
	uninstall-app rewrite Installed Applications too, from whatever code their process imports. A failed write stays
	in Error Log: the record keeps "started", which docs/self-hosting.md#extensions restores — the safe side."""
	data = _manifest_raw()
	moved = [app_id for app_id, entry in data["extensions"].items()
		if isinstance(entry, dict) and _unfinished(entry.get("upgrade")) and entry["upgrade"].get("migrate") != "succeeded"
		and _migrated_version(app_id) == entry["upgrade"].get("to")]
	for app_id in moved:
		data["extensions"][app_id]["upgrade"]["migrate"] = "succeeded"
	if moved:
		try:
			_write_manifest(data)
		except OSError:
			frappe.log_error(title=f"extensions {', '.join(moved)}: recording the migrate in extensions.json failed")


def _migrated_version(app_id: str) -> str:
	"""The version frappe's Installed Applications holds for ``app_id``, else the imported code's."""
	version = frappe.db.get_value("Installed Application", {"parent": "Installed Applications", "app_name": app_id}, "app_version")
	return str(version or "") or registry.version_of(app_id, {})


def forget_rollback_point(app_id: str) -> None:
	"""Uninstalled from the site, the extension's upgrade rollback point means nothing: its backups,
	apps-ext/.previous/<id> (with a test build's .<id>.replaced) and the entry's previous /
	backup / upgrade go. An interrupted upgrade's leftovers (the intent file) are left for the self-hosting guide."""
	# lazy: one site per bench (an upgrade backs up and migrates one site); several sites: keep .previous until none has the id
	if intent_path(app_id).exists():
		return
	shutil.rmtree(backup_dir(app_id), ignore_errors=True)
	for name in (app_id, f".{app_id}.older", f".{app_id}.replaced"):
		shutil.rmtree(registry.ext_dir(".previous") / name, ignore_errors=True)
	data = _manifest_raw()
	entry = data["extensions"].get(app_id)
	if entry and set(ENTRY_UPGRADE_KEYS) & entry.keys():
		data["extensions"][app_id] = {k: v for k, v in entry.items() if k not in ENTRY_UPGRADE_KEYS}
		_write_manifest(data)


# --------------------------------------------------------------------------- manifest
def _manifest_path() -> Path:
	return registry.sites_dir() / "extensions.json"


def _write_manifest(data: dict) -> None:
	p = _manifest_path()
	tmp = p.with_name(p.name + ".tmp")
	tmp.write_text(json.dumps(data, indent=2))
	os.replace(tmp, p)                       # atomic: a half-written manifest would stop every container


def _manifest_raw() -> dict:
	p = _manifest_path()
	try:
		data = json.loads(p.read_text())
	except (OSError, ValueError):
		data = {}
	if not isinstance(data, dict) or data.get("schema") != 1:
		data = {"schema": 1, "extensions": {}}
	if not isinstance(data.get("extensions"), dict):
		data["extensions"] = {}                   # null or not an object: empty, as registry.manifest_file reads it
	return data


def add_manifest_entry(app_id: str, entry: dict) -> None:
	data = _manifest_raw()
	data["extensions"][app_id] = entry
	_write_manifest(data)


def remove_manifest_entry(app_id: str) -> None:
	data = _manifest_raw()
	if app_id in data["extensions"]:
		del data["extensions"][app_id]
		_write_manifest(data)


# --------------------------------------------------------------------------- agent request
def _request_path() -> Path:
	return registry.sites_dir() / REQUEST_FILE


def _done_path() -> Path:
	return registry.sites_dir() / DONE_FILE


def request_restart(action: str, app_id: str, user: str | None = None) -> dict:
	"""Leave the agent its request. The file is one slot for the whole bench and the agent restarts
	every container, so another extension's request is never overwritten."""
	# lazy: check-then-replace — the stage jobs share the one long-queue worker; several workers: create with O_EXCL
	other = pending_request()
	if other and other.get("app") != app_id:
		raise RuntimeError(_("The store is busy: it is waiting to restart for {0}.").format(other.get("app")))
	req = {
		"action": action, "app": app_id, "site": frappe.local.site,
		"requested_at": _now_iso(),
		"requested_by": user or frappe.session.user,
	}
	tmp = _request_path().with_suffix(".tmp")
	tmp.write_text(json.dumps(req, indent=2))
	os.replace(tmp, _request_path())
	return req


def pending_request() -> dict | None:
	try:
		return json.loads(_request_path().read_text())
	except (OSError, ValueError):
		return None


def last_done() -> dict | None:
	try:
		return json.loads(_done_path().read_text())
	except (OSError, ValueError):
		return None


def mark_done(app_id: str, ok: bool, message: str = "") -> None:
	_done_path().write_text(json.dumps({
		"app": app_id, "ok": bool(ok), "message": message[:500],
		"finished_at": _now_iso(),
	}, indent=2))
	_withdraw_request(app_id)


def _withdraw_request(app_id: str) -> None:
	"""Remove the restart request if it is ``app_id``'s (or unreadable) — never another extension's."""
	if (pending_request() or {}).get("app") in (None, app_id):
		_request_path().unlink(missing_ok=True)


def pipeline_busy() -> str:
	"""Why nothing new may start on the extension pipeline, or "": a pending restart request or any
	busy row holds everything — installs, upgrades and uninstalls run one at a time."""
	from ffl_core.extensions.jobs import BUSY_STATES

	if req := pending_request():
		return _("The store is busy: it is waiting to restart for {0} ({1}). Try again when that has finished.").format(
			req.get("app"), req.get("action"))
	if row := frappe.db.get_value(DOCTYPE, {"job_state": ["in", BUSY_STATES]}, ["name", "job_state"], as_dict=True):
		return _("The store is busy: {0} is {1}. Extensions are installed, upgraded and removed one at a time; "
			"try again when that has finished.").format(row.name, row.job_state)
	return ""


# --------------------------------------------------------------------------- replay
def replay_script() -> Path:
	"""ext-replay.sh: baked into the image, or (dev bench) this checkout's copy."""
	for c in ("/usr/local/bin/ext-replay.sh", registry.bench_root().parent / "deploy" / "prod" / "ext-replay.sh"):
		if Path(c).is_file():
			return Path(c)
	frappe.throw(_("ext-replay.sh was not found on this server; the extension layer is not installed here ({0}).").format(EXTENSIONS_DOC))


def run_replay() -> str:
	"""Wire this container to the manifest (symlink, .pth, apps.txt, build). Raises with the log on failure."""
	script = replay_script()
	env = dict(os.environ, BENCH_PATH=str(registry.bench_root()))
	proc = subprocess.run(["bash", str(script)], cwd=str(registry.bench_root()), env=env,
		capture_output=True, text=True, timeout=REPLAY_TIMEOUT)
	if proc.returncode != 0:
		tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
		raise RuntimeError("ext-replay failed:\n" + "\n".join(tail))
	return proc.stdout


# --------------------------------------------------------------------------- stage (job)
def stage(sha256: str, app_id: str, user: str) -> None:
	"""Long-queue job: code onto the volume, manifest, self-replay, request the restart."""
	from ffl_core.extensions.jobs import set_state
	from ffl_core.extensions.registry import sync_app
	from ffl_core.utils import commit

	zip_path = upload_path(sha256)
	target = registry.ext_dir(app_id)
	had_entry = app_id in registry.manifest_file()
	moved = False
	try:
		if busy := pipeline_busy():
			raise RuntimeError(busy)
		registry.ext_dir().mkdir(parents=True, exist_ok=True)      # a dev bench has no apps-ext until the first sideload
		with tempfile.TemporaryDirectory(prefix="ext-stage-", dir=str(registry.bench_root())) as tmp:
			root, report = _unpack_checked(zip_path, Path(tmp), app_id)
			if _placement(app_id)["on_bench"]:
				raise RuntimeError(_("{0} is already on this server; upload it again to see whether it can be upgraded.").format(app_id))
			moved = True                               # before the copy: a half-copied tree must be removed too
			shutil.copytree(root, target, symlinks=False)
		add_manifest_entry(app_id, _entry(sha256, report, user))
		run_replay()                                   # this worker proves the package wires and builds
		frappe.clear_cache()
		sync_app(app_id)                               # the row: read off the file, not installed yet
		if not frappe.db.exists(DOCTYPE, app_id):
			raise RuntimeError("the registry did not pick the extension up after the replay")
		request_restart("install", app_id, user)
		set_state(app_id, "Staged",
			_("Code is on the server. Waiting for every part of the POS to load it — usually under a minute."))
		zip_path.unlink(missing_ok=True)
		commit()
	except Exception as e:
		frappe.db.rollback()
		if not had_entry:
			remove_manifest_entry(app_id)
		if moved:
			shutil.rmtree(target, ignore_errors=True)
			_unwire(app_id)
			_withdraw_request(app_id)                  # a late failure: the agent must not install what was just removed
		frappe.log_error(title=f"extension {app_id}: staging the upload failed")
		if frappe.db.exists(DOCTYPE, app_id):
			set_state(app_id, "Failed", _("Install failed: {0}").format(str(e)[:300] or type(e).__name__))
		else:
			_remember_failure(sha256, app_id, str(e)[:300] or type(e).__name__)
		raise


def _unwire(app_id: str) -> None:
	"""Undo what a partial replay did in this container (the next full replay tidies the rest)."""
	link = registry.bench_root() / "apps" / app_id
	if link.is_symlink():
		link.unlink()
	try:
		import sysconfig
		Path(sysconfig.get_paths()["purelib"], f"{app_id}.pth").unlink(missing_ok=True)
	except Exception:
		pass


def _remember_failure(sha256: str, app_id: str, message: str) -> None:
	"""No registry row to carry the message, or one this job may not write (refused while the store was busy):
	keep it next to the upload for the page (api.upload_failure)."""
	from ffl_core.utils import commit

	try:
		(upload_dir() / f"{sha256}.failed").write_text(json.dumps({"app": app_id, "message": message}))
		commit()
	except Exception:
		pass


def failure_for(sha256: str) -> dict | None:
	try:
		return json.loads(_ref(sha256, ".failed").read_text())
	except (OSError, ValueError):
		return None


# --------------------------------------------------------------------------- upgrade (job)
def stage_upgrade(sha256: str, app_id: str, user: str) -> None:
	"""Long-queue job, segment 1 of an in-place upgrade: back
	the site up, keep the old tree in apps-ext/.previous/<id>, put the new one in
	its place, rewrite the manifest entry, replay, request the restart. Any
	failure puts the old tree, the old entry and this container's wiring back
	(replaying them once more) before the row says Failed.

	Only over a finished upgrade, or none: the verdict refuses an unfinished one
	(unfinished_upgrade), and that refusal — like a busy store — leaves the row and
	the backup its message names alone. The row is not synced here — this worker
	still has the old code imported; finish_upgrade writes it once."""
	from frappe.utils import now_datetime
	from frappe.utils.backups import new_backup

	from ffl_core.extensions.jobs import BUSY_STATES, describe_error, set_state
	from ffl_core.utils import commit

	state = frappe.db.get_value(DOCTYPE, app_id, "job_state")
	if state in BUSY_STATES:
		raise RuntimeError(_("{0} is busy ({1}).").format(app_id, state))      # that job owns the row: touch nothing
	zip_path = upload_path(sha256)
	target = registry.ext_dir(app_id)
	previous = registry.ext_dir(".previous") / app_id
	older = previous.with_name(f".{app_id}.older")           # the last upgrade's previous, dropped once this one is staged
	intent = intent_path(app_id)
	old_entry = registry.manifest_file().get(app_id)
	interrupted = intent.exists()                            # an earlier upgrade's: the verdict refuses, its message must stay true
	unfinished = unfinished_upgrade(old_entry or {}, settled=not interrupted)
	undo: list = []                                          # run in reverse on failure
	claimed = swapped = False                                # the row is ours once written; the volume changed once swapped
	plan: dict = {}
	backup = taken = None                                    # the rollback point's backup; ``taken`` when this job made it
	busy = ""                                                # why pipeline_busy refused this job
	try:
		if busy := pipeline_busy():
			raise RuntimeError(busy)
		registry.ext_dir().mkdir(parents=True, exist_ok=True)
		with tempfile.TemporaryDirectory(prefix=".upgrade-", dir=str(registry.ext_dir())) as tmp:
			# unpacked on the apps-ext volume itself, so the swap below is two renames
			(Path(tmp) / "pkg").mkdir()
			root, report = _unpack_checked(zip_path, Path(tmp) / "pkg", app_id)
			verdict = _upgrade(app_id, report.version, report.permissions)
			if not (verdict and verdict["allowed"]):
				raise RuntimeError(verdict["reason"] if verdict else _("{0} is not on this server any more; install it instead.").format(app_id))
			# The backup is the rollback point, so it is taken before this job writes anything —
			# restored, the row reads exactly as it did before the upgrade (Idle, not Staged).
			name = f"{now_datetime():%Y%m%d-%H%M%S}-database.sql.gz"
			backup = taken = os.path.abspath(new_backup(ignore_files=True, force=True, backup_path_db=str(backup_dir(app_id) / name)).backup_path_db)
			if busy := pipeline_busy():
				raise RuntimeError(_("{0} (it started while the database was backed up)").format(busy))
			set_state(app_id, "Staged", _("Putting {0} {1} in place. {2}").format(app_id, report.version, _backup_note(backup)))
			claimed = True
			plan = {"from": verdict["from"], "to": report.version, "backup": backup, "started_at": _now_iso()}
			intent.parent.mkdir(exist_ok=True)
			intent.write_text(json.dumps(plan, indent=2))
			undo.append(lambda: intent.unlink(missing_ok=True))
			swapped = True
			if target.exists():
				if previous.exists():
					shutil.rmtree(older, ignore_errors=True)
					previous.rename(older)
					undo.append(lambda: older.rename(previous))
				target.rename(previous)
				undo.append(lambda: previous.rename(target))
			undo.append(lambda: shutil.rmtree(target, ignore_errors=True))
			root.rename(target)
		rest = old_entry or {}
		add_manifest_entry(app_id, _entry(sha256, report, user,
			previous={"version": verdict["from"], **{k: v for k, v in rest.items() if k in ("source", "sha256", "url", "ref", "commit")}},
			backup=backup, upgrade={"from": verdict["from"], "to": report.version, "state": "staged", "migrate": "not_started"}))
		undo.append(lambda: add_manifest_entry(app_id, old_entry) if old_entry else remove_manifest_entry(app_id))
		intent.unlink()                                # the entry now says everything the intent did
		run_replay()                                   # this worker proves the new package wires and builds
		request_restart("upgrade", app_id, user)
		undo.append(lambda: _withdraw_request(app_id))
		set_state(app_id, "Staged", _("{0} {1} is on the server. Waiting for the POS to load it; it keeps working while the database "
			"is updated. {2}").format(app_id, report.version, _backup_note(backup)))
		zip_path.unlink(missing_ok=True)
		commit()
	except Exception as e:
		frappe.db.rollback()
		restored = True
		for step in reversed(undo):
			try:
				step()
			except Exception:
				restored = False
				frappe.log_error(title=f"extension {app_id}: putting the previous version back failed")
		if swapped:
			try:
				run_replay()                               # the old tree, wired and built again
			except Exception:
				restored = False
				frappe.log_error(title=f"extension {app_id}: replay after a failed upgrade")
		if not restored:
			try:
				intent.write_text(json.dumps(plan, indent=2))  # the volume is not known to be whole: no upgrade of this id until the self-hosting guide's recipe
			except OSError:
				frappe.log_error(title=f"extension {app_id}: keeping the upgrade intent failed")
		elif taken:
			Path(taken).unlink(missing_ok=True)            # rolled back cleanly: not a rollback point for anything
		frappe.log_error(title=f"extension {app_id}: staging the upgrade failed")
		if not claimed and (busy or unfinished):
			# the store was busy, or this id's last upgrade is still to be rolled back: its row (and the backup it names) stays
			_remember_failure(sha256, app_id, describe_error(e))
			raise
		if not claimed and frappe.db.get_value(DOCTYPE, app_id, "job_state") in BUSY_STATES:
			raise                                          # another job holds the row: its state is not ours to overwrite
		lines = [line.strip() for line in str(e).splitlines() if line.strip()]    # the replay's log tail: its first and last line
		reason = " … ".join(dict.fromkeys((lines[0], lines[-1])))[:300] if len(lines) > 1 else describe_error(e)
		if not restored:
			tail = _("Putting the previous version back failed too — see Error Log. {0}").format(rollback_hint(app_id, backup))
		elif interrupted:
			tail = _("An earlier upgrade of {0} stopped half-way. {1}").format(app_id, rollback_hint(app_id))
		else:
			tail = _("The previous version is still in place.")
		set_state(app_id, "Failed", _("Upgrade failed: {0}. {1}").format(reason, tail))
		raise
	shutil.rmtree(older, ignore_errors=True)
	for stale in backup_dir(app_id).glob("*"):                # one backup per id: the rollback point's
		if str(stale) != backup:
			stale.unlink(missing_ok=True)


# --------------------------------------------------------------------------- finish (agent)
def finish_install(app_id: str) -> dict:
	"""Run by the agent after the restart: ``bench --site X execute
	ffl_core.extensions.sideload.finish_install --args '["<id>"]'``.
	The same site-level install the page's Install button starts."""
	from ffl_core.extensions.jobs import install_on_site

	try:
		install_on_site(app_id)
		mark_done(app_id, True, "installed")
		return {"ok": True}
	except Exception as e:
		mark_done(app_id, False, str(e)[:300] or type(e).__name__)
		return {"ok": False, "error": str(e)[:300]}


def begin_upgrade(app_id: str) -> dict:
	"""Run by the agent after the restart, right before ``bench --site X migrate``:
	``bench --site X execute ffl_core.extensions.sideload.begin_upgrade --args '["<id>"]'``.
	The entry's ``upgrade.migrate`` is "started" before the card says Upgrading (and the stale
	sweep can time it out) — written over "not_started" only: a migrate already on record (a
	deploy's, before_migrate / after_migrate) stays as it was. No record, or extensions.json not
	writable, raises — the agent migrates only after this returned."""
	from ffl_core.extensions.jobs import set_state

	record = (registry.manifest_file().get(app_id) or {}).get("upgrade")
	if not isinstance(record, dict):
		raise RuntimeError(f"{app_id} has no upgrade record in extensions.json")
	if record.get("migrate") == "not_started":
		set_upgrade(app_id, migrate="started")
	entry = registry.manifest_file().get(app_id) or {}
	set_state(app_id, "Upgrading", _("Updating the database for {0} {1}. {2}").format(
		app_id, entry.get("version") or "", _backup_note(entry.get("backup"))))
	return {"ok": True}


def finish_upgrade(app_id: str, ok=None, message: str = "") -> dict:
	"""Run by the agent after the migrate, with its outcome: ``bench --site X execute
	ffl_core.extensions.sideload.finish_upgrade --args '["<id>", 1|0, "<message>"]'`` —
	with 0 too when the agent could not get as far as the migrate (K4). docs/self-hosting.md#extensions
	runs it by hand as well, to finish an upgrade whose migrate succeeded.

	1: the migrate must have run the record's ``to`` — the version Installed Applications holds
	(_migrated_version: frappe rewrites it from the code a migrate runs). Another version means
	that migrate was not this upgrade's: ``state`` failed, the row Failed naming both versions,
	and the restart request — maybe that other upgrade's — left where it is. Then ``migrate``
	"succeeded" is recorded first, the row, ``state`` done, Idle; a step after the first
	failing leaves ``state`` failed with ``migrate`` "succeeded".
	0: ``state`` failed, and a "succeeded" goes back to "started" — the core's
	after_migrate records it before later apps' hooks run, so a migrate that failed after
	it did not succeed. The card's text reads those fields, not the row.

	``ok`` is 1 or 0: ``bench execute`` evals ``--args`` as
	Python, where json.dumps's ``true``/``false`` do not exist — frappe then passes
	the whole text as the only argument, which is decoded here as a belt. That eval
	turns an escaped non-BMP character into two surrogate halves, and a lone half
	cannot be stored: the message is made encodable first, then cut (K5)."""
	from frappe.utils import cint

	from ffl_core.extensions.jobs import describe_error, set_state

	if ok is None and str(app_id).lstrip().startswith("["):
		app_id, ok, message = (json.loads(app_id) + [None, ""])[:3]
	message = str(message or "").encode("utf-16", "surrogatepass").decode("utf-16", "replace")
	entry = registry.manifest_file().get(app_id) or {}
	to = str((entry.get("upgrade") or {}).get("to") or "")
	fields, withdraw = {"state": "failed"}, True
	if cint(ok) and (ran := _migrated_version(app_id)) != to:
		message, withdraw = _("the migrate ran {0} {1}, not {2}").format(app_id, ran or "?", to or "?"), False
	elif cint(ok):
		try:
			set_upgrade(app_id, migrate="succeeded")      # first: whatever fails below, the database update is on record
			frappe.clear_cache()
			registry.sync_app(app_id)
			set_upgrade(app_id, state="done")
			set_state(app_id, "Idle")
			mark_done(app_id, True, f"upgraded {(entry.get('upgrade') or {}).get('from') or '?'} → {entry.get('version') or '?'}")
			return {"ok": True}
		except Exception as e:
			frappe.db.rollback()
			frappe.log_error(title=f"extension {app_id}: finishing the upgrade failed")
			message = describe_error(e)
	elif migrate_of(entry.get("upgrade")) == "succeeded":
		fields["migrate"] = "started"
	try:
		set_upgrade(app_id, **fields)
	except Exception:                                  # the row still says Failed; registry.reset_stale_jobs records the entry later
		frappe.log_error(title=f"extension {app_id}: recording the failed upgrade in extensions.json failed")
	detail = " ".join(message.split())[:300] or _("no details")       # one line, the agent's error line first
	text = {
		"not_started": _("Upgrade failed before the database was updated: {0}. {1}"),
		"started": _("Upgrade failed while updating the database: {0}. {1}"),
		"succeeded": _("Upgrade failed after the database was updated (nothing to roll back; finishing it was not recorded): {0}. Ask your developer to finish it. {1}"),
	}[migrate_of((registry.manifest_file().get(app_id) or {}).get("upgrade"))].format(detail, rollback_hint(app_id))
	set_state(app_id, "Failed", text)
	if withdraw:
		mark_done(app_id, False, text)
	return {"ok": False, "error": text}


# --------------------------------------------------------------------------- git
GIT_TIMEOUT = 60
# https only, no redirects (a public host may not bounce the clone to a private one),
# symlinks checked out as plain files (a link to /etc/... never gets zipped).
GIT_CONFIG = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
	"-c", "http.followRedirects=false", "-c", "core.symlinks=false")
GIT_URL_RE = re.compile(r"https://([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
GIT_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def inspect_git(url: str, ref: str) -> dict:
	"""Clone a public https repository at a branch or tag (shallow, no credentials,
	≤ GIT_TIMEOUT s), pack the tree as an upload and inspect it; the report
	carries ``source`` = {"type": "git", "url", "ref", "commit"}."""
	url, ref = (url or "").strip().rstrip("/"), (ref or "").strip()
	pin = _check_git_address(url, ref)
	with tempfile.TemporaryDirectory(prefix="ext-git-") as tmp:
		tree = Path(tmp) / "repo"
		commit = _clone(url, ref, tree, pin)
		shutil.rmtree(tree / ".git", ignore_errors=True)
		if not (tree / "pyproject.toml").is_file():
			frappe.throw(_("{0}@{1} is not a POS extension: there is no pyproject.toml at the top of the repository.").format(url, ref),
				title=_("Not an extension"))
		# lazy: measured after the clone — a huge repository can use disk for up to GIT_TIMEOUT s; upgrade: watch the dir while cloning
		if sum(p.stat().st_size for p in tree.rglob("*") if p.is_file()) > MAX_UNPACKED_BYTES:
			frappe.throw(_("The repository is larger than {0} MB.").format(MAX_UNPACKED_BYTES // (1024 * 1024)), title=_("Too large"))
		data = _zip_tree(tree)
	if len(data) > MAX_ZIP_BYTES:
		frappe.throw(_("The repository is larger than {0} MB zipped.").format(MAX_ZIP_BYTES // (1024 * 1024)), title=_("Too large"))
	return inspect(save_upload(data), source={"type": "git", "url": url, "ref": ref, "commit": commit})


def _check_git_address(url: str, ref: str) -> str:
	"""A public https repository address and a plain branch/tag name, or a user error.
	Refused before git runs: other protocols, credentials, anything but host/owner/repo,
	and hosts resolving to private, loopback, link-local, reserved or multicast addresses.
	Returns the vetted answer as curl's resolve entry ``host:443:addr[,addr]``: git is pinned
	to exactly those, so the name cannot be re-pointed inward before it connects (DNS rebinding)."""
	if not url.lower().startswith("https://"):
		frappe.throw(_("Only https:// repository addresses can be installed (not http, ssh or file)."), title=_("Not an https address"))
	if "@" in url.split("/")[2]:
		frappe.throw(_("Take the user name or token out of the address: only public repositories can be installed from git."),
			title=_("Credentials in the address"))
	m = GIT_URL_RE.fullmatch(url)
	if not m or {m.group(2), m.group(3)} & {".", ".."}:
		frappe.throw(_("The address must look like https://github.com/<owner>/<repo>."), title=_("Not a repository address"))
	if not GIT_REF_RE.fullmatch(ref) or ref.startswith("-"):
		frappe.throw(_("{0} is not a branch or tag name.").format(repr(ref)[:60]), title=_("Not a branch or tag"))
	vetted = []
	for address in _resolve(m.group(1)):
		ip = ipaddress.ip_address(address)
		ip = getattr(ip, "ipv4_mapped", None) or ip
		if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
			frappe.throw(_("{0} is not a public server; only public repositories can be installed from git.").format(m.group(1)),
				title=_("Not a public server"))
		vetted.append(f"[{ip}]" if ip.version == 6 else str(ip))
	return f"{m.group(1)}:443:{','.join(dict.fromkeys(vetted))}"


def _resolve(host: str) -> list[str]:
	"""Every address ``host`` resolves to (one seam: tests answer instead of DNS)."""
	try:
		return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
	except (OSError, UnicodeError):
		frappe.throw(_("Cannot find the server {0}.").format(host), title=_("Unknown server"))


def _clone(url: str, ref: str, dest: Path, pin: str) -> str:
	"""``git clone --depth 1`` of ``ref`` into ``dest``, connecting only to ``pin`` (the addresses
	_check_git_address vetted); returns the commit. Never prompts: a private repository fails at once."""
	env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
	cmd = ["git", *GIT_CONFIG, "-c", f"http.curloptResolve={pin}", "clone", "--depth", "1", "--single-branch", "--no-tags", "--branch", ref, "--", url, str(dest)]
	try:
		proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
		if proc.returncode == 0:
			return subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], env=env, capture_output=True, text=True,
				timeout=GIT_TIMEOUT, check=True).stdout.strip()
	except subprocess.TimeoutExpired:
		frappe.throw(_("Cloning took longer than {0} seconds. Download the repository as a zip and upload it instead.").format(GIT_TIMEOUT),
			title=_("Too slow"))
	except (OSError, subprocess.CalledProcessError) as e:
		frappe.throw(_("git could not run on this server ({0}). Download the repository as a zip and upload it instead.").format(str(e)[:200]))
	last = next((line for line in reversed(proc.stderr.strip().splitlines()) if line.strip()), "") or f"exit {proc.returncode}"
	frappe.throw(_("Could not clone {0} at {1}: {2}. Only public repositories can be installed from git; "
		"for a private one, download the zip and upload it.").format(url, ref, last[:300]), title=_("Clone failed"))


def _zip_tree(root: Path) -> bytes:
	"""The checked-out tree as the zip an upload would be: sorted entries and fixed
	timestamps, so the same commit is the same sha256."""
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
		for p in sorted(root.rglob("*")):
			if p.is_file() and not p.is_symlink():
				z.writestr(zipfile.ZipInfo(p.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0)), p.read_bytes(), zipfile.ZIP_DEFLATED)
	return buf.getvalue()
