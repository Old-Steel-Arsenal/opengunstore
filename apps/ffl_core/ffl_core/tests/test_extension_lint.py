"""ffl_core.extensions.lint — every rule has a fixture that fails it, and the
example extension passes (including wheels/dependencies, and one parse per file /
one walk of the tree).

The fixture is a scratch copy of deploy/prod/tests/ext_hello, mutated per
test; nothing here needs a site (the lint is pure Python) but the module runs
under bench run-tests like the rest of the suite.
"""
from __future__ import annotations

import builtins
import collections
import contextlib
import importlib.metadata
import io
import json
import os
import runpy
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ffl_core import gate_audit
from ffl_core.extensions import lint as L

REPO = Path(__file__).resolve().parents[4]           # apps/ffl_core/ffl_core/tests → repo root
EXT_HELLO = REPO / "deploy" / "prod" / "tests" / "ext_hello"
UPGRADE = runpy.run_path(str(REPO / "deploy" / "prod" / "tests" / "make_ext_hello_upgrade.py"))   # build_wheel, build_upgrade


def _rules(report: L.Report, level: str = "error") -> set[str]:
	return {f.rule for f in report.findings if f.level == level}


class Fixture(unittest.TestCase):
	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp(prefix="lint-"))
		self.addCleanup(shutil.rmtree, self.tmp, True)
		self.root = self.tmp / "ext_hello"
		shutil.copytree(EXT_HELLO, self.root, ignore=shutil.ignore_patterns("__pycache__"))
		self.pkg = self.root / "ext_hello"

	def hooks(self) -> str:
		return (self.pkg / "hooks.py").read_text()

	def write_hooks(self, text: str):
		(self.pkg / "hooks.py").write_text(text)

	def lint(self, **kw) -> L.Report:
		return L.lint(self.root, **kw)


class RealExtensionsPass(Fixture):
	def test_ext_hello_is_clean(self):
		r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])
		self.assertEqual((r.app_id, r.version), ("ext_hello", "0.1.0"))
		self.assertIn("pos_ui", r.permissions)


class Structure(Fixture):
	def test_forbidden_files_and_core_dirs(self):
		(self.root / ".env").write_text("SECRET=1")
		(self.root / "ffl_core").mkdir()
		(self.pkg / "dump.sql.gz").write_bytes(b"x")
		(self.pkg / "libhello.so.1").write_bytes(b"\0")               # a versioned shared library is one too
		r = self.lint()
		msgs = " | ".join(f.message for f in r.errors)
		self.assertIn(".env", msgs); self.assertIn("core app files", msgs); self.assertIn(".sql.gz", msgs)
		self.assertIn("ext_hello/libhello.so.1: .so.1 files must not be in the package", msgs)

	def test_pycache_is_only_an_error_in_a_package(self):
		(self.pkg / "__pycache__").mkdir()
		(self.pkg / "__pycache__" / "hooks.cpython-314.pyc").write_bytes(b"\0")
		self.assertTrue(self.lint().ok)
		self.assertIn("structure", _rules(self.lint(packaged=True)))

	def test_missing_module_package_is_an_error(self):
		shutil.rmtree(self.pkg / "ext_hello")                      # the Module dir frappe imports as ext_hello.ext_hello
		msgs = [f.message for f in self.lint().errors]
		self.assertTrue(any("module package" in m and "ext_hello/ext_hello/__init__.py" in m for m in msgs), msgs)

	def test_compile_error_names_file_and_line(self):
		(self.pkg / "api" / "greet.py").write_text("def (\n")
		r = self.lint()
		f = [x for x in r.errors if x.rule == "compile"][0]
		self.assertEqual((f.path, f.line), ("ext_hello/api/greet.py", 1))


class Identity(Fixture):
	def test_version_mismatch_and_missing_upper_bound(self):
		self.write_hooks(self.hooks().replace('"version": "0.1.0"', '"version": "0.2.0"').replace('">=0.1.0,<2"', '">=0.1.0"'))
		msgs = [f.message for f in self.lint().errors]
		self.assertTrue(any("__version__" in m for m in msgs), msgs)
		self.assertTrue(any("upper bound" in m for m in msgs), msgs)

	def test_bad_id_and_title_and_modules(self):
		bad = self.tmp / "hello"
		shutil.move(self.root, bad); shutil.move(bad / "ext_hello", bad / "hello")
		(bad / "pyproject.toml").write_text((bad / "pyproject.toml").read_text().replace("ext_hello", "hello"))
		r = L.lint(bad)
		self.assertIn("identity", _rules(r))
		self.assertTrue(any("<vendor>_<name>" in f.message for f in r.errors))

	def test_unknown_permission_and_first_party_reserved(self):
		self.write_hooks(self.hooks().replace('"permissions": ["doc_events:Customer", "pos_ui"]',
			'"permissions": ["doc_events:Customer", "pos_ui", "first_party", "root_access"]').replace('"publisher_id": "osa"', '"publisher_id": "acme"'))
		r = self.lint()
		self.assertIn("first_party", _rules(r))
		self.assertTrue(any("root_access" in f.message for f in r.errors))


class Hooks(Fixture):
	def test_hook_off_the_whitelist(self):
		self.write_hooks(self.hooks() + '\noverride_whitelisted_methods = {"frappe.client.get": "ext_hello.api.greet.ping"}\n')
		r = self.lint()
		self.assertTrue(any(f.rule == "hooks" and "override_whitelisted_methods" in f.message for f in r.errors))

	def test_sidebar_items_hook_is_on_the_whitelist(self):
		self.write_hooks(self.hooks() + '\ngunshop_sidebar_items = [{"type": "Section Break", "label": "Hello"}]\n')
		r = self.lint()
		self.assertFalse(any(f.rule == "hooks" and "gunshop_sidebar_items" in f.message for f in r.findings))

	def test_catalog_change_cursors_hook_is_on_the_whitelist(self):
		self.write_hooks(self.hooks() + '\ngunshop_catalog_change_cursors = ["Hello Settings.sync_cursor"]\n')
		r = self.lint()
		self.assertFalse(any(f.rule == "hooks" and "gunshop_catalog_change_cursors" in f.message
			for f in r.findings))

	def test_auth_hooks_is_first_party_only(self):
		self.write_hooks(self.hooks() + '\nauth_hooks = ["ext_hello.api.greet.ping"]\n')
		r = self.lint()
		self.assertTrue(any(f.rule == "hooks" and "auth_hooks" in f.message and "first-party" in f.message for f in r.errors))
		self.write_hooks(self.hooks().replace('"permissions": [', '"permissions": ["first_party", '))
		r = self.lint()
		self.assertFalse([f for f in r.errors if f.rule == "hooks"], [f.message for f in r.errors])

	def test_first_party_needs_a_core_known_id(self):
		self.write_hooks(self.hooks().replace('"permissions": [', '"permissions": ["first_party", '))
		msgs = [f.message for f in self.lint().errors if f.rule == "first_party"]
		self.assertTrue(any("'ext_hello' is not a first-party extension" in m for m in msgs), msgs)

	def test_portal_paths_and_foreign_has_permission(self):
		self.write_hooks(self.hooks() + '\nwebsite_redirects = [{"source": "/old", "target": "/hello"}]\nhas_permission = {"Sales Invoice": "ext_hello.api.greet.ping"}\n')
		msgs = [f.message for f in self.lint().errors if f.rule == "hooks"]
		self.assertTrue(any("/ext/ext_hello/" in m for m in msgs), msgs)
		self.assertTrue(any("Sales Invoice" in m for m in msgs), msgs)


class Permissions(Fixture):
	def test_undeclared_doc_event_scheduler_network_guest(self):
		self.write_hooks(self.hooks().replace('"Customer": {', '"Sales Invoice": {"on_submit": "ext_hello.hooks_impl.customer.after_insert"}, "Customer": {')
			+ '\nscheduler_events = {"daily": ["ext_hello.hooks_impl.customer.after_insert"]}\n')
		(self.pkg / "api" / "ping.py").write_text(
			'import frappe\n\n@frappe.whitelist(allow_guest=True)\ndef ping():\n\t"""Ping."""\n\treturn frappe.make_get_request("https://api.acme-corp.io/v1")\n')
		r = self.lint()
		msgs = " | ".join(f.message for f in r.errors if f.rule == "permissions")
		for needle in ("doc_events:Sales Invoice", "scheduler", "network:api.acme-corp.io", "public_endpoint"):
			self.assertIn(needle, msgs)

	def test_compliance_read_derived_unless_first_party(self):
		(self.pkg / "api" / "peek.py").write_text(
			'import frappe\n\n@frappe.whitelist()\ndef peek():\n\t"""Peek."""\n\tfrappe.only_for("System Manager")\n\treturn frappe.get_all("FFL Disposition", limit=1)\n')
		self.assertTrue(any("compliance_read" in f.message for f in self.lint().errors))
		self.write_hooks(self.hooks().replace('"pos_ui"]', '"pos_ui", "first_party"]'))
		self.assertFalse(any("compliance_read" in f.message for f in self.lint().errors))


class Security(Fixture):
	def test_forbidden_imports_calls_and_strings(self):
		(self.pkg / "api" / "shell.py").write_text(
			'import subprocess\nimport os\nimport frappe\n\n@frappe.whitelist()\ndef run():\n\t"""Run."""\n\tfrappe.only_for("System Manager")\n'
			'\tos.system("ls")\n\teval("1")\n\tfrappe.make_get_request("https://a.example.com", verify=False)\n\treturn open("sites/x/site_config.json").read()\n')
		msgs = [f.message for f in self.lint().errors if f.rule == "security"]
		for needle in ("import subprocess", "os.system", "eval()", "verify=False", "site_config.json"):
			self.assertTrue(any(needle in m for m in msgs), (needle, msgs))


class Gates(Fixture):
	def test_ungated_endpoint_is_an_error_and_missing_docstring_a_warning(self):
		(self.pkg / "api" / "open.py").write_text('import frappe\n\n@frappe.whitelist()\ndef open_door():\n\treturn 1\n')
		r = self.lint()
		self.assertTrue(any(f.rule == "gates" and "open_door" in f.message for f in r.errors))
		self.assertTrue(any(f.rule == "gates" and "docstring" in f.message for f in r.findings if f.level == "warn"))

	def test_the_gate_rule_is_gate_audit_on_the_parsed_trees(self):
		"""One implementation — lint hands its trees to gate_audit.find_ungated_endpoints."""
		(self.pkg / "api" / "open.py").write_text('import frappe\n\n@frappe.whitelist()\ndef open_door():\n\treturn 1\n')
		self.assertIs(L.find_ungated_endpoints, gate_audit.find_ungated_endpoints)
		with mock.patch.object(L, "find_ungated_endpoints", wraps=gate_audit.find_ungated_endpoints) as audit:
			r = self.lint()
		audit.assert_called_once()
		self.assertEqual((audit.call_args.args, set(audit.call_args.kwargs["trees"])), (("ext_hello", self.pkg), set(self.pkg.rglob("*.py"))))
		self.assertEqual([f.message for f in r.errors if f.rule == "gates"],
			["endpoint is not gated: ext_hello.api.open.open_door (first stmt: 'return 1') (first statement must be require_enabled/only_for)"])

	def test_a_package_checked_out_under_a_tests_directory_is_still_audited(self):
		"""gate_audit skips test modules by their path inside the app, not by every folder above it."""
		moved = self.tmp / "tests" / "ext_hello"
		shutil.move(self.root, moved)
		(moved / "ext_hello" / "api" / "open.py").write_text('import frappe\n\n@frappe.whitelist()\ndef open_door():\n\t"""Open."""\n\treturn 1\n')
		self.assertTrue(any("open_door" in f.message for f in L.lint(moved).errors if f.rule == "gates"))
		self.assertEqual([o.split(" ")[0] for o in gate_audit.find_ungated_endpoints("ext_hello", moved / "ext_hello")], ["ext_hello.api.open.open_door"])


class NamingAndNotes(Fixture):
	def test_release_note_missing_is_a_warning(self):
		shutil.rmtree(self.pkg / "change_log")
		r = self.lint()
		self.assertTrue(r.ok)
		self.assertIn("release_notes", _rules(r, "warn"))

	def test_doctype_and_custom_field_prefixes(self):
		d = self.pkg / "ext_hello" / "doctype" / "layaway_plan"
		d.mkdir(parents=True)
		(d / "layaway_plan.json").write_text(json.dumps({"name": "Layaway Plan", "doctype": "DocType"}))
		(self.pkg / "fixtures").mkdir()
		(self.pkg / "fixtures" / "custom_field.json").write_text(json.dumps([{"dt": "Customer", "fieldname": "plan_count", "fieldtype": "Int"}]))
		self.write_hooks(self.hooks().replace('"pos_ui"]', '"pos_ui", "custom_fields:Customer"]'))
		msgs = [f.message for f in self.lint().errors if f.rule == "naming"]
		self.assertTrue(any("Layaway Plan" in m for m in msgs), msgs)
		self.assertTrue(any("plan_count" in m and "ext_" in m for m in msgs), msgs)


class Archives(Fixture):
	def _zip(self, arc: Path, *, prefix: str = "", extra: list[tuple[str, bytes]] = ()):
		with zipfile.ZipFile(arc, "w") as z:
			for p in sorted(self.root.rglob("*")):
				if p.is_file():
					z.write(p, prefix + str(p.relative_to(self.root)))
			for name, data in extra:
				z.writestr(name, data)
		return arc

	def test_zip_at_root_and_under_one_folder_both_lint(self):
		self.assertTrue(L.lint_path(self._zip(self.tmp / "a.zip")).ok)
		self.assertTrue(L.lint_path(self._zip(self.tmp / "b.zip", prefix="ext_hello-main/")).ok)

	def test_traversal_symlink_and_non_zip_are_refused(self):
		bad = self._zip(self.tmp / "c.zip", extra=[("../evil.py", b"x")])
		self.assertTrue(any("unsafe path" in f.message for f in L.lint_path(bad).errors))
		with zipfile.ZipFile(self.tmp / "d.zip", "w") as z:
			info = zipfile.ZipInfo("ext_hello/link")
			info.external_attr = (0o120777 << 16)
			z.writestr(info, "/etc/passwd")
			z.writestr("pyproject.toml", "[project]\nname='ext_hello'")
		self.assertTrue(any("symlink" in f.message for f in L.lint_path(self.tmp / "d.zip").errors))
		(self.tmp / "e.zip").write_bytes(b"not a zip")
		self.assertTrue(any("not a zip" in f.message for f in L.lint_path(self.tmp / "e.zip").errors))

	def test_zip_bomb_by_size_is_refused(self):
		old = L.MAX_UNPACKED_BYTES
		L.MAX_UNPACKED_BYTES = 10
		try:
			self.assertTrue(any("unpacks to more" in f.message for f in L.lint_path(self._zip(self.tmp / "f.zip")).errors))
		finally:
			L.MAX_UNPACKED_BYTES = old

	def test_cli_reports_and_exits_nonzero_on_errors(self):
		(self.pkg / "api" / "greet.py").write_text("def (\n")
		import contextlib
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			code = L.main([str(self.root)])
		self.assertEqual(code, 1)
		self.assertIn("FAIL ext_hello", buf.getvalue())


class Dependencies(Fixture):
	"""Pure-Python wheels in wheels/: each rule has a package that fails it."""

	def wheel(self, name="ext_hello_dep", version="1.0.0", *, requires=(), tag="py3-none-any", dest=None, files=None):
		dest = dest or self.root / "wheels"
		dest.mkdir(parents=True, exist_ok=True)
		return UPGRADE["build_wheel"](dest, name, version, files or {f"{name}/__init__.py": "X = 1\n"}, requires=requires, tag=tag)

	def depends_on(self, *reqs):
		py = self.root / "pyproject.toml"
		py.write_text(py.read_text().replace("dependencies = []", f"dependencies = {json.dumps(list(reqs))}"))

	def deps_errors(self) -> list[str]:
		return [f.message for f in self.lint().errors if f.rule == "deps"]

	def test_wheel_and_core_satisfy_the_dependencies(self):
		self.wheel(requires=("packaging>=20", 'ext-hello-nowhere; extra == "test"', 'ext-hello-nowhere; python_version < "3"'))
		self.depends_on("ext_hello_dep>=1,<2", "packaging>=20")
		r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])

	def test_platform_wheel_is_refused(self):
		self.wheel(tag="cp314-cp314-manylinux_2_17_x86_64")
		msgs = self.deps_errors()
		self.assertTrue(any("pure-Python" in m and "cp314-cp314-manylinux_2_17_x86_64" in m for m in msgs), msgs)

	def test_py2_py3_none_any_is_pure(self):
		self.wheel(tag="py2.py3-none-any")
		self.depends_on("ext_hello_dep")
		r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])

	def test_whl_outside_wheels_is_refused(self):
		self.wheel(dest=self.pkg / "vendor")
		self.wheel(dest=self.root / "wheels" / "nested")
		msgs = [f.message for f in self.lint().errors if f.rule == "structure"]
		self.assertTrue(any(m.startswith("ext_hello/vendor/ext_hello_dep-1.0.0-py3-none-any.whl") and "wheels/" in m for m in msgs), msgs)
		self.assertTrue(any(m.startswith("wheels/nested/") for m in msgs), msgs)

	def test_unsatisfied_dependency(self):
		self.depends_on("ext-hello-nowhere>=1")
		msgs = self.deps_errors()
		self.assertTrue(any("ext-hello-nowhere>=1" in m and "not satisfied" in m for m in msgs), msgs)

	def test_core_version_outside_the_range(self):
		core = importlib.metadata.version("packaging")
		self.depends_on("packaging<1")
		msgs = self.deps_errors()
		self.assertTrue(any("packaging<1" in m and f"core has {core}" in m for m in msgs), msgs)

	def test_bundling_what_the_core_provides_is_refused(self):
		core = importlib.metadata.version("packaging")
		self.wheel("packaging", "99.0")
		msgs = self.deps_errors()
		self.assertTrue(any(f"provided by core {core}, do not bundle" in m for m in msgs), msgs)

	def test_wheel_requires_dist_must_be_satisfied(self):
		self.wheel(requires=("ext-hello-nowhere>=2",))
		self.depends_on("ext_hello_dep")
		msgs = self.deps_errors()
		self.assertTrue(any("Requires-Dist" in m and "ext-hello-nowhere>=2" in m for m in msgs), msgs)

	def test_broken_wheel_is_refused(self):
		(self.root / "wheels").mkdir()
		(self.root / "wheels" / "ext_hello_dep-1.0.0-py3-none-any.whl").write_bytes(b"not a zip")
		self.assertTrue(any("not a valid wheel" in m for m in self.deps_errors()))

	def test_wheel_contents_count_toward_the_unpack_limit(self):
		self.wheel(files={"ext_hello_dep/__init__.py": "X = 1\n", "ext_hello_dep/data.txt": "x" * 200_000})
		self.depends_on("ext_hello_dep")
		on_disk = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
		with mock.patch.object(L, "MAX_UNPACKED_BYTES", on_disk + 100_000):     # the files fit; unpacked wheels do not
			self.assertTrue(any("unpack to more than" in m for m in self.deps_errors()))
		self.assertEqual(self.deps_errors(), [])

	def test_another_extensions_deps_are_not_the_core(self):
		"""apps-ext/.deps/<id> (and apps/<id> → apps-ext) are on sys.path through the
		replay's .pth files; a dist-info there belongs to that extension, not to the core."""
		deps = self.dist_info(self.tmp / "apps-ext" / ".deps" / "ext_other", "ext_hello_dep", "1.0.0")
		tree = self.dist_info(self.tmp / "apps-ext" / "ext_other", "ext_hello_dep2", "1.0.0")
		(self.tmp / "apps").mkdir()
		(self.tmp / "apps" / "ext_other").symlink_to(self.tmp / "apps-ext" / "ext_other")
		with mock.patch.object(sys, "path", [*sys.path, deps, str(self.tmp / "apps" / "ext_other")]):
			self.wheel()
			self.wheel("ext_hello_dep2")
			self.depends_on("ext_hello_dep", "ext_hello_dep2")
			self.assertEqual(self.deps_errors(), [], "bundling what only another extension has is fine")
			shutil.rmtree(self.root / "wheels")
			msgs = self.deps_errors()
		for dist in ("ext_hello_dep ", "ext_hello_dep2 "):
			self.assertTrue(any(m.startswith(f"pyproject.toml: {dist}is not satisfied (not in wheels/ or the core)") for m in msgs), (dist, msgs, tree))

	def test_a_dist_info_elsewhere_on_sys_path_is_the_core(self):
		site = self.dist_info(self.tmp / "site-packages", "ext_hello_dep", "1.0.0")
		with mock.patch.object(sys, "path", [*sys.path, site]):
			self.wheel()
			msgs = self.deps_errors()
		self.assertTrue(any("provided by core 1.0.0, do not bundle" in m for m in msgs), msgs)

	def dist_info(self, base: Path, name: str, version: str) -> str:
		d = base / f"{name}-{version}.dist-info"
		d.mkdir(parents=True)
		(d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
		return str(base)

	def test_wheel_contents_may_not_shadow_the_core_this_or_another_extension(self):
		"""What a wheel installs at the top of .deps/<id> sits on every process's sys.path."""
		self.wheel(files={"ext_hello_dep/__init__.py": "", "frappe/__init__.py": "", "ffl_core.py": "", "ext_hello/__init__.py": "",
			"ext_other/api.py": "", "ext_hello_dep-1.0.0.data/purelib/erpnext/__init__.py": ""})
		self.depends_on("ext_hello_dep")
		msgs = [f.message for f in self.lint(known_ids=("ext_other",)).errors if f.rule == "deps"]
		for top in ("frappe", "ffl_core", "erpnext", "ext_hello", "ext_other"):
			self.assertTrue(any(f"installs a top-level {top!r}" in m for m in msgs), (top, msgs))
		self.assertFalse(any("'ext_hello_dep'" in m for m in msgs), msgs)
		self.assertEqual(len(msgs), 5, msgs)

	def test_a_wheel_may_carry_data_files_but_no_compiled_code_keys_or_dumps(self):
		"""A pure-Python wheel with its own CA bundle (.pem) is fine. A versioned
		shared library is compiled code too, and keys, database dumps and .env stay refused inside a wheel."""
		self.wheel(files={"ext_hello_dep/__init__.py": "", "ext_hello_dep/certs/cacert.pem": "-----BEGIN CERTIFICATE-----\n"})
		self.depends_on("ext_hello_dep")
		r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])
		compiled = ("_speedups.so", "_speedups.cpython-312-x86_64-linux-gnu.so", "libhello.so.1", "libhello.so.1.2", "_speedups.pyd", "libhello.dylib",
			"hello.dll", "hello.exe", "mod.pyc", "certs/server.key", "fixtures/dump.sql", "fixtures/dump.sql.gz", "settings/.env")
		for whl in (self.root / "wheels").iterdir():
			whl.unlink()
		self.wheel(files={"ext_hello_dep/__init__.py": "", "ext_hello_dep/certs/cacert.pem": "x", **{f"ext_hello_dep/{n}": "\0" for n in compiled}})
		msgs = self.deps_errors()
		for name in compiled:
			self.assertTrue(any(f"ext_hello_dep/{name} must not be in the package" in m for m in msgs), (name, msgs))
		self.assertEqual(len(msgs), len(compiled), msgs)

	def test_a_whl_that_is_not_a_regular_file_is_refused(self):
		"""pip would build a directory named x.whl as a source project."""
		(self.root / "wheels" / "ext_hello_dep-1.0.0-py3-none-any.whl").mkdir(parents=True)
		(self.root / "wheels" / "ext_hello_dep-1.0.0-py3-none-any.whl" / "setup.py.txt").write_text("")
		msgs = [f.message for f in self.lint().errors if f.rule == "structure"]
		self.assertTrue(any(m.startswith("wheels/ext_hello_dep-1.0.0-py3-none-any.whl is not a regular file") for m in msgs), msgs)

	def test_project_that_is_not_a_table_is_an_error_not_a_crash(self):
		"""A malformed pyproject [project] entry is reported as a lint error."""
		(self.root / "pyproject.toml").write_text('project = "ext_hello"\n')
		self.assertTrue(any("[project] must be a table" in m for m in self.deps_errors()))

	def test_a_marker_that_cannot_be_evaluated_is_an_error_not_a_crash(self):
		self.wheel(requires=('ext-hello-nowhere; python_version ~= "xyz"',))
		self.depends_on("ext_hello_dep")
		msgs = self.deps_errors()
		self.assertTrue(any("Requires-Dist" in m and "marker cannot be evaluated" in m for m in msgs), msgs)

	def test_an_invalid_requirement_is_refused(self):
		"""A requirement string that does not parse is reported as a lint error."""
		self.depends_on("not a requirement!!")
		self.assertTrue(any("'not a requirement!!' is not a valid requirement" in m for m in self.deps_errors()))

	def test_an_oversized_metadata_is_never_inflated(self):
		(self.root / "wheels").mkdir()
		with zipfile.ZipFile(self.root / "wheels" / "ext_hello_dep-1.0.0-py3-none-any.whl", "w", zipfile.ZIP_DEFLATED) as z:
			z.writestr("ext_hello_dep-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: ext_hello_dep\nVersion: 1.0.0\n" + "#" * (1024 * 1024))
		self.depends_on("ext_hello_dep")
		with mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("inflated a METADATA over 1 MB")):
			msgs = self.deps_errors()
		self.assertTrue(any("not a valid wheel" in m and "under 1 MB" in m for m in msgs), msgs)

	def test_an_encrypted_wheel_is_refused(self):
		whl = self.wheel()
		data = bytearray(whl.read_bytes())
		for signature, flags in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):     # local and central headers: bit 0 = encrypted
			at = data.find(signature)
			while at != -1:
				data[at + flags] |= 0x1
				at = data.find(signature, at + 4)
		whl.write_bytes(bytes(data))
		self.depends_on("ext_hello_dep")
		msgs = self.deps_errors()
		self.assertTrue(any("not a valid wheel" in m and "encrypted" in m for m in msgs), msgs)

	def test_upgrade_fixture_is_clean(self):
		zip_path = UPGRADE["build_upgrade"](self.tmp / "ext_hello-0.2.0.zip")
		r = L.lint_path(zip_path)
		self.assertEqual((r.errors, r.version), ([], "0.2.0"))
		with zipfile.ZipFile(zip_path) as z:
			names = z.namelist()
		for want in ("wheels/ext_hello_dep-1.0.0-py3-none-any.whl", "ext_hello/change_log/v0/v0_2_0.md", "ext_hello/patches/v0_2/remember_greeting.py"):
			self.assertIn(want, names)


class SinglePass(Fixture):
	"""Every module is parsed once and the tree walked once (was 5 parses, 5 walks)."""

	def test_every_module_is_parsed_once(self):
		sources = collections.Counter(p.read_text() for p in self.root.rglob("*.py"))   # empty __init__.py files share a key
		parsed = collections.Counter()
		real = builtins.compile

		def counting(source, *a, **kw):
			if isinstance(source, str):                 # a parse — ast.parse lands here too; compiling a tree does not
				parsed[source] += 1
			return real(source, *a, **kw)

		with mock.patch("builtins.compile", counting):
			r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])
		self.assertEqual(collections.Counter({s: n for s, n in parsed.items() if s in sources}), sources)

	def test_the_tree_is_walked_once(self):
		walks = []
		ours = {Path(L.__file__).resolve(), Path(gate_audit.__file__).resolve()}

		def counting(name, real):
			def wrapper(*a, **kw):
				if Path(sys._getframe(1).f_code.co_filename).resolve() in ours:
					walks.append(f"{name}({a[0] if a else ''})")
				return real(*a, **kw)
			return wrapper

		with contextlib.ExitStack() as stack:
			for owner, names in ((Path, ("rglob", "glob", "iterdir", "walk")), (os, ("walk", "scandir", "listdir"))):
				for name in names:
					stack.enter_context(mock.patch.object(owner, name, counting(name, getattr(owner, name))))
			r = self.lint()
		self.assertTrue(r.ok, [f.message for f in r.errors])
		self.assertEqual(len(walks), 1, walks)
