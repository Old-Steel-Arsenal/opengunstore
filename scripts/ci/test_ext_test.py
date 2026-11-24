"""Unit tests for scripts/ci/ext_test.sh — the gate `make ext-test` and the core CI run for each extension.

Shims stand in for bench and for the bench's Python, so what is tested is the script's own judgement:
what it refuses before touching the bench, install on a first run and migrate on a re-run, that all
three steps run after a failure, and what the exit status says. The id rule and the core app names
come from the real ffl_core/extensions/lint.py. `make ext-test` runs for real with a shim for
$(COMPOSE): its copy is checked (--check) before it replaces apps-ext/<id>.

python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""
import ast
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ext_test.sh"
LINT = HERE.parent.parent / "apps" / "ffl_core" / "ffl_core" / "extensions" / "lint.py"
EXTENSIONS = LINT.parent / "__init__.py"
MAKEFILE = HERE.parent.parent / "Makefile"

BENCH_SHIM = """#!/bin/sh
echo "bench $*" >> "$SHIM_LOG"
case "$*" in
	*"execute frappe.get_installed_apps"*) echo "$SHIM_INSTALLED"; exit "$SHIM_INSTALLED_EXIT" ;;
	*"execute frappe.db.get_value"*) [ -n "$SHIM_ROW" ] && echo "$SHIM_ROW"; exit "$SHIM_ROW_EXIT" ;;
	*" migrate") exit "$SHIM_MIGRATE_EXIT" ;;
	*install-app*) exit "$SHIM_INSTALL_EXIT" ;;
	*run-tests*) printf '%b' "$SHIM_TESTS_OUT"; exit "$SHIM_TESTS_EXIT" ;;
esac
"""
# -c sysconfig: the purelib the .pth goes to · -c …lint…: the real id rule, through a stub package
# holding lint.py's ID_RE and CORE_APPS · -c …effective…: the real switch rule, the stub package holding
# ffl_core.extensions.effective · -m …lint / generate_api_docs.py: steps 2 and 3
PYTHON_SHIM = """#!/bin/sh
case "$1 $2" in
	"-c "*sysconfig*) echo "$SHIM_PURELIB" ;;
	"-c "*ffl_core.extensions.lint*|"-c "*"ffl_core.extensions import effective"*) PYTHONPATH="$SHIM_STUBS" exec python3 "$@" ;;
	"-m ffl_core.extensions.lint") echo "lint $3" >> "$SHIM_LOG"; exit "$SHIM_LINT_EXIT" ;;
	*generate_api_docs.py*) echo "docs $*" >> "$SHIM_LOG"; exit "$SHIM_DOCS_EXIT" ;;
	*) echo "unexpected: python $*" >&2; exit 99 ;;
esac
"""
# The POS Extension row as `bench execute frappe.db.get_value … as_dict` prints it.
ON = '{"enabled": 1, "installed_on_site": 1, "compatible": 1, "incompatible_reason": null}'
TWO_CATEGORIES = "Ran 2 tests in 0.1s\\n\\nOK\\nRan 3 tests in 0.2s\\n\\nOK\\n"
# $(COMPOSE) for `make ext-test`: `exec -T frappe bash -c` (unpack, check, replace) runs here against the test's
# bench and this checkout's scripts/; `exec -T frappe bash -lc` (install and test on the site) is only logged.
COMPOSE_SHIM = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as log:
	log.write("compose " + " ".join(args[:5]) + "\\n")
if args[:5] == ["exec", "-T", "frappe", "bash", "-c"]:
	script = args[5].replace("/workspace/frappe-bench", os.environ["SHIM_BENCH"])
	os.execvp("bash", ["bash", "-c", script.replace("/workspace/scripts", os.environ["SHIM_SCRIPTS"])])
"""


def executable(path: Path, text: str):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)
	path.chmod(0o755)


def tree_hash(root: Path) -> str:
	"""Every path under ``root`` with its file bytes or link target."""
	h = hashlib.sha256()
	for p in sorted(root.rglob("*")):
		what = "L" + os.readlink(p) if p.is_symlink() else "F" + hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "D"
		h.update(f"{p.relative_to(root)}\0{what}\n".encode())
	return h.hexdigest()


class ExtTest(unittest.TestCase):
	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		tmp = Path(self._tmp.name).resolve()
		self.bench, self.purelib, self.log = tmp / "bench", tmp / "purelib", tmp / "shim.log"
		executable(tmp / "bin" / "bench", BENCH_SHIM)
		executable(self.bench / "env" / "bin" / "python", PYTHON_SHIM)
		(self.bench / "apps").mkdir()
		(self.bench / "sites").mkdir()
		(self.bench / "sites" / "apps.txt").write_text("frappe\nerpnext\nffl_core\n")
		self.purelib.mkdir()
		lint = LINT.read_text()
		names = {"ID_RE", "CORE_APPS"}
		stub = tmp / "stubs" / "ffl_core" / "extensions"
		stub.mkdir(parents=True)
		(stub.parent / "__init__.py").write_text("")
		ext = EXTENSIONS.read_text()
		(stub / "__init__.py").write_text("".join(ast.get_source_segment(ext, n) + "\n"
			for n in ast.parse(ext).body if isinstance(n, ast.FunctionDef) and n.name == "effective"))
		(stub / "lint.py").write_text("import re\n" + "".join(ast.get_source_segment(lint, n) + "\n"
			for n in ast.parse(lint).body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in names))
		self.root = tmp / "acme-hello"
		(self.root / "acme_hello").mkdir(parents=True)
		(self.root / "acme_hello" / "hooks.py").write_text('app_name = "acme_hello"\n')
		self.env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", SHIM_LOG=str(self.log),
			SHIM_PURELIB=str(self.purelib), SHIM_STUBS=str(tmp / "stubs"))

	def tearDown(self):
		self._tmp.cleanup()

	def run_script(self, *args, installed='["frappe", "erpnext"]', tests_out=TWO_CATEGORIES, installed_exit=0,
			migrate_exit=0, install_exit=0, tests_exit=0, lint_exit=0, docs_exit=0, row=ON, row_exit=0):
		self.log.write_text("")
		env = dict(self.env, SHIM_INSTALLED=installed, SHIM_TESTS_OUT=tests_out, SHIM_TESTS_EXIT=str(tests_exit),
			SHIM_ROW=row, SHIM_ROW_EXIT=str(row_exit),
			SHIM_INSTALLED_EXIT=str(installed_exit), SHIM_MIGRATE_EXIT=str(migrate_exit),
			SHIM_INSTALL_EXIT=str(install_exit), SHIM_LINT_EXIT=str(lint_exit), SHIM_DOCS_EXIT=str(docs_exit))
		args = args or (str(self.bench), "dev.localhost", "acme_hello", str(self.root))
		return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)

	def steps(self):
		"""What the shims were asked to do, in order (reading the installed apps and the registry row left out)."""
		calls = [line.split() for line in self.log.read_text().splitlines()]
		return [c[3] if c[0] == "bench" else c[0] for c in calls
			if "frappe.get_installed_apps" not in c and "frappe.db.get_value" not in c]

	def apps_txt(self):
		return (self.bench / "sites" / "apps.txt").read_text().split()

	def test_a_new_extension_is_wired_installed_and_passes_all_three_steps(self):
		r = self.run_script()
		self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
		self.assertIn("ext-test acme_hello: tests OK (5 ran) · lint OK · docs OK", r.stdout)
		self.assertEqual(self.steps(), ["clear-cache", "install-app", "run-tests", "lint", "docs"])
		self.assertEqual(os.readlink(self.bench / "apps" / "acme_hello"), str(self.root))
		self.assertEqual((self.purelib / "acme_hello.pth").read_text(), f"{self.bench}/apps/acme_hello\n")
		self.assertEqual(self.apps_txt(), ["frappe", "erpnext", "ffl_core", "acme_hello"])

	def test_a_rerun_migrates_the_site_instead_of_a_no_op_install(self):
		self.assertEqual(self.run_script().returncode, 0)
		r = self.run_script(installed='["frappe", "erpnext", "acme_hello"]')
		self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
		self.assertEqual(self.steps(), ["clear-cache", "migrate", "run-tests", "lint", "docs"])
		self.assertEqual(self.apps_txt().count("acme_hello"), 1)

	def test_a_wiring_step_that_fails_ends_the_run_with_1(self):
		# clearing the module map, reading the installed apps, migrate (a re-run), install-app (a first run):
		# nothing runs after it
		for name, kwargs, message, steps in (
			("installed apps", {"installed_exit": 1}, "cannot read the apps installed on dev.localhost", ["clear-cache"]),
			("migrate", {"installed": '["frappe", "acme_hello"]', "migrate_exit": 1}, "migrate failed", ["clear-cache", "migrate"]),
			("install-app", {"install_exit": 1}, "install-app acme_hello failed", ["clear-cache", "install-app"]),
		):
			r = self.run_script(**kwargs)
			self.assertEqual((r.returncode, self.steps()), (1, steps), (name, r.stdout, r.stderr))
			self.assertIn(message, r.stderr, name)
			self.assertNotIn("ext-test acme_hello:", r.stdout, name)

	def test_a_run_that_ran_no_test_fails(self):
		r = self.run_script(tests_out="Testing is disabled for the site!\\n")
		self.assertEqual(r.returncode, 1)
		self.assertIn("tests FAIL (exit 0, 0 ran) · lint OK · docs OK", r.stdout)

	def test_every_step_runs_and_the_status_says_what_failed(self):
		# generate_api_docs --app --check: 3 = only stale files or Permissions drift from the core, 1 = parity/conformance
		# ext_test.sh: 1 = the tests, the lint or a docs defect failed · 3 = only that docs drift (the core CI warns on it)
		for (tests_exit, lint_exit, docs_exit), (status, docs) in {
			(1, 0, 0): (1, "OK"), (0, 1, 0): (1, "OK"), (0, 0, 3): (3, "STALE"), (0, 0, 1): (1, "FAIL"),
			(1, 0, 3): (1, "STALE"), (0, 1, 3): (1, "STALE"), (0, 0, 2): (1, "FAIL"), (1, 1, 1): (1, "FAIL"),
		}.items():
			r = self.run_script(tests_exit=tests_exit, lint_exit=lint_exit, docs_exit=docs_exit)
			self.assertEqual((r.returncode, self.steps()[-3:]), (status, ["run-tests", "lint", "docs"]),
				(tests_exit, lint_exit, docs_exit, r.stdout, r.stderr))
			self.assertIn(f"· docs {docs}\n", r.stdout, (tests_exit, lint_exit, docs_exit))

	def test_an_extension_that_is_off_on_the_site_fails_naming_why_and_its_tests_do_not_run(self):
		# off, every endpoint refuses "<title> is turned off for this store": its tests would fail by the dozen
		# without saying why (an extension's main needing a core release this checkout is not on yet)
		too_new = '{"enabled": 0, "installed_on_site": 1, "compatible": 0, "incompatible_reason": "needs core >=1.3.3,<2, this store runs 1.3.2"}'
		for name, row, reason in (
			("incompatible", too_new, "needs core >=1.3.3,<2, this store runs 1.3.2"),
			("switched off", '{"enabled": 0, "installed_on_site": 1, "compatible": 1, "incompatible_reason": null}', "its switch is off"),
			("not installed", '{"enabled": 1, "installed_on_site": 0, "compatible": 1, "incompatible_reason": null}', "it is not installed on the site"),
			("no row", "", "it has no POS Extension row"),
		):
			r = self.run_script(row=row)
			self.assertEqual((r.returncode, self.steps()), (1, ["clear-cache", "install-app", "lint", "docs"]),
				(name, r.stdout, r.stderr))
			self.assertIn(f"ext-test acme_hello: tests FAIL (not run: {reason}", r.stdout, name)
			self.assertIn(f"acme_hello is off on dev.localhost — {reason}", r.stderr, name)

	def test_a_registry_row_that_cannot_be_read_ends_the_run_with_1(self):
		r = self.run_script(row="", row_exit=1)
		self.assertEqual((r.returncode, self.steps()), (1, ["clear-cache", "install-app"]), r.stdout + r.stderr)
		self.assertIn("cannot read the POS Extension row of acme_hello", r.stderr)
		self.assertNotIn("ext-test acme_hello:", r.stdout)

	def test_refusals_touch_nothing(self):
		other = self.root.parent / "other"
		(other / "acme_hello").mkdir(parents=True)
		wheels = self.root.parent / "with-wheels"
		(wheels / "acme_hello").mkdir(parents=True)
		(wheels / "acme_hello" / "hooks.py").write_text("")
		(wheels / "wheels").mkdir()
		(wheels / "wheels" / "dep-1.0-py3-none-any.whl").write_text("")
		cases = {
			"../../apps/ffl_core": ("../../apps/ffl_core", self.root, "not an extension id"),
			"capitals": ("Acme_hello", self.root, "not an extension id"),
			"trailing newline": ("acme_hello\n", self.root, "not an extension id"),
			"core app": ("ffl_core", self.root, "core app"),
			"no hooks.py": ("acme_hello", other, "hooks.py not found"),
			"wheels/": ("acme_hello", wheels, "wheels/"),
		}
		for name, (app_id, root, message) in cases.items():
			r = self.run_script(str(self.bench), "dev.localhost", app_id, str(root))
			self.assertEqual((r.returncode, self.steps()), (2, []), (name, r.stdout, r.stderr))
			self.assertIn(message, r.stderr, name)
		self.assertEqual(sorted(os.listdir(self.bench / "apps")), [])
		self.assertEqual(sorted(os.listdir(self.purelib)), [])
		self.assertEqual(self.apps_txt(), ["frappe", "erpnext", "ffl_core"])

	def test_an_apps_entry_this_script_did_not_make_is_left_alone(self):
		link = self.bench / "apps" / "acme_hello"
		link.mkdir()                                                        # a real app directory
		r = self.run_script()
		self.assertEqual((r.returncode, self.steps()), (2, []), r.stderr)
		self.assertIn("real directory", r.stderr)
		link.rmdir()
		link.symlink_to(self.root.parent / "someone-else")                   # another app's link, in apps.txt
		with open(self.bench / "sites" / "apps.txt", "a") as f:
			f.write("acme_hello\n")
		r = self.run_script()
		self.assertEqual((r.returncode, self.steps()), (2, []), r.stderr)
		self.assertEqual(os.readlink(link), str(self.root.parent / "someone-else"))
		(self.bench / "sites" / "apps.txt").write_text("frappe\nerpnext\nffl_core\n")
		r = self.run_script()                                              # the same link, no longer listed: a leftover
		self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
		self.assertEqual(os.readlink(link), str(self.root))

	def test_check_refuses_what_a_run_refuses_and_touches_nothing(self):
		"""`make ext-test` replaced apps-ext/<id> with a folder the run then refused (wheels/), and left a copy of a
		refused id behind. --check runs every refusal of a run on the unpacked copy, before anything is replaced."""
		installed = self.bench / "apps-ext" / "acme_hello"                     # the tree an earlier run installed
		(installed / "acme_hello").mkdir(parents=True)
		(installed / "acme_hello" / "hooks.py").write_text("# installed by an earlier run\n")
		other = self.root.parent / "other"
		(other / "acme_hello").mkdir(parents=True)
		wheels = self.root.parent / "with-wheels"
		(wheels / "acme_hello").mkdir(parents=True)
		(wheels / "acme_hello" / "hooks.py").write_text("")
		(wheels / "wheels").mkdir()
		(wheels / "wheels" / "dep-1.0-py3-none-any.whl").write_text("")
		link = self.bench / "apps" / "acme_hello"

		def state():
			tree = sorted((str(p.relative_to(self.bench)), p.read_text() if p.is_file() else os.readlink(p) if p.is_symlink() else "")
				for p in self.bench.rglob("*") if "env" not in p.parts)
			return tree, sorted(os.listdir(self.purelib))

		def check(app_id="acme_hello", package=self.root):
			return self.run_script("--check", str(self.bench), app_id, str(package), str(installed))

		before = state()
		for name, (app_id, package, message) in {
			"../../apps/ffl_core": ("../../apps/ffl_core", self.root, "not an extension id"),
			"core app": ("ffl_core", self.root, "core app"),
			"no hooks.py": ("acme_hello", other, "hooks.py not found"),
			"wheels/": ("acme_hello", wheels, "wheels/"),
		}.items():
			r = check(app_id, package)
			self.assertEqual((r.returncode, self.steps()), (2, []), (name, r.stdout, r.stderr))
			self.assertIn(message, r.stderr, name)
			self.assertEqual(state(), before, name)
		link.mkdir()                                                            # a real app directory
		r = check()
		self.assertEqual((r.returncode, r.stderr.count("real directory")), (2, 1), r.stderr)
		link.rmdir()
		link.symlink_to(self.root.parent / "someone-else")                      # another app's link, in apps.txt
		(self.bench / "sites" / "apps.txt").write_text("frappe\nerpnext\nffl_core\nacme_hello\n")
		before = state()
		r = check()
		self.assertEqual((r.returncode, self.steps(), state()), (2, [], before), r.stderr)
		link.unlink()
		link.symlink_to(installed)                                              # the link a run made to the root the copy replaces
		before = state()
		r = check()
		self.assertEqual((r.returncode, self.steps(), state()), (0, [], before), r.stdout + r.stderr)

	def test_make_ext_test_leaves_the_folder_and_apps_ext_alone_when_the_check_refuses(self):
		"""The real recipe, $(COMPOSE) shimmed — a refused folder changes no byte of the bench
		(apps-ext/<id> from an earlier run, no copy left behind) or of the folder; an accepted one replaces apps-ext/<id>."""
		compose = self.bench.parent / "bin" / "compose"
		executable(compose, COMPOSE_SHIM)
		installed = self.bench / "apps-ext" / "acme_hello"                     # the tree an earlier run installed
		(installed / "acme_hello").mkdir(parents=True)
		(installed / "acme_hello" / "hooks.py").write_text("# installed by an earlier run\n")
		wheels = self.root.parent / "with-wheels"
		(wheels / "acme_hello").mkdir(parents=True)
		(wheels / "acme_hello" / "hooks.py").write_text('app_name = "acme_hello"\n')
		(wheels / "wheels").mkdir()
		(wheels / "wheels" / "dep-1.0-py3-none-any.whl").write_text("")
		env = {k: v for k, v in self.env.items() if not k.startswith("MAKE")}
		env.update(SHIM_BENCH=str(self.bench), SHIM_SCRIPTS=str(HERE.parent), SHIM_INSTALLED='["frappe"]')

		def make(folder):
			self.log.write_text("")
			return subprocess.run(["make", "--no-print-directory", "-C", str(MAKEFILE.parent), "ext-test", "ID=acme_hello",
				f"DIR={folder}", f"COMPOSE={compose}"], env=env, capture_output=True, text=True)

		bench, folder = tree_hash(self.bench), tree_hash(wheels)
		r = make(wheels)
		self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
		self.assertIn("has wheels/", r.stderr)
		self.assertEqual((tree_hash(self.bench), tree_hash(wheels)), (bench, folder), r.stdout + r.stderr)
		self.assertEqual(self.log.read_text().splitlines(), ["compose exec -T frappe bash -c"])   # no install, no tests

		r = make(self.root)
		self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
		self.assertEqual(tree_hash(installed), tree_hash(self.root))
		self.assertEqual(self.log.read_text().splitlines(), ["compose exec -T frappe bash -c", "compose exec -T frappe bash -lc"])
		self.assertEqual([p.name for p in self.bench.iterdir() if p.name.startswith(".ext-test.")], [])


if __name__ == "__main__":
	unittest.main()
