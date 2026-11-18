"""Tests for scripts/ci/run_guard.py: the pure verdict logic, real git in a temporary repo, and
the whole guard around a fake test command (each way a run can lie, once).

python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_guard as rg  # noqa: E402

KNOWN = {"demo.tests.test_alpha", "demo.tests.test_beta"}


def ok(name):
	return f"    \x1b[32m ✔ \x1b[0m {name}\n"


class ScanOutput(unittest.TestCase):
	def scan(self, *lines):
		scan = rg.LogScan(KNOWN)
		for line in lines:
			scan.feed(line)
		return scan

	def test_counts_per_module_from_class_headers_and_strips_colour(self):
		scan = self.scan("demo.tests.test_alpha.One\n", ok("test_a"), ok("test_b"),
			"demo.tests.test_alpha.Two\n", "    \x1b[31m ✖ \x1b[0m test_c\n", "    \x1b[37m = \x1b[0m test_d\n",
			"demo.tests.test_beta.Only\n", ok("test_e"), "TOTAL ran=5 failures=1 errors=0\n")
		self.assertEqual(dict(scan.executed), {"demo.tests.test_alpha": 3, "demo.tests.test_beta": 1})
		self.assertEqual(dict(scan.skipped), {"demo.tests.test_alpha": 1})
		self.assertEqual(scan.total, (5, 1, 0))

	def test_nested_class_header_still_names_its_module(self):
		self.assertEqual(rg.module_of("demo.tests.test_alpha.Outer.Inner", KNOWN), "demo.tests.test_alpha")
		self.assertEqual(rg.module_of("elsewhere.helpers.Base", KNOWN), "elsewhere.helpers")

	def test_the_rerun_of_a_failed_module_is_not_counted_twice(self):
		scan = self.scan("::group::worker1 on dev.localhost\n", "demo.tests.test_alpha.One\n", ok("test_a"), "::endgroup::\n",
			"::group::rerun1-1 on dev.localhost\n", "demo.tests.test_alpha.One\n", ok("test_a"), "::endgroup::\n")
		self.assertEqual(scan.executed["demo.tests.test_alpha"], 1)

	def test_lines_before_any_header_and_noise_count_for_nothing(self):
		scan = self.scan("    ✔  test_orphan\n", "Running 3 tests for demo\n", "OK\n")
		self.assertEqual(sum(scan.executed.values()), 0)


class Verdict(unittest.TestCase):
	def scan(self, executed, total, skipped=None):
		scan = rg.LogScan(KNOWN)
		scan.executed.update(executed)
		scan.skipped.update(skipped or {})
		scan.total = total
		return scan

	def test_a_changed_file_with_no_executed_test_is_named(self):
		scan = self.scan({"demo.tests.test_alpha": 4}, (4, 0, 0))
		problems = rg.judge(scan, {"demo.tests.test_alpha": 4, "demo.tests.test_beta": 2}, "origin/develop", 0)
		self.assertEqual(len(problems), 1)
		self.assertIn("demo.tests.test_beta", problems[0])
		self.assertNotIn("test_alpha", problems[0])

	def test_skipped_only_is_still_not_executed_and_says_so(self):
		scan = self.scan({}, (1, 0, 0), skipped={"demo.tests.test_beta": 2})
		self.assertIn("2 skipped", rg.judge(scan, {"demo.tests.test_beta": 2}, "origin/develop", 0)[0])

	def test_a_file_that_defines_no_tests_is_not_expected_to_run(self):
		self.assertEqual(rg.judge(self.scan({"demo.tests.test_alpha": 1}, (1, 0, 0)), {"demo.tests.test_beta": 0}, "b", 0), [])

	def test_zero_tests_fails_even_when_nothing_changed(self):
		self.assertIn("no test ran", rg.judge(self.scan({}, (0, 0, 0)), {}, "b", 0)[0])

	def test_missing_total_fails_a_clean_exit_but_not_one_that_already_failed(self):
		self.assertIn("never printed its TOTAL", rg.judge(self.scan({}, None), {}, "b", 0)[0])
		self.assertEqual(rg.judge(self.scan({}, None), {}, "b", 2), [])

	def test_head_moved_and_files_changed(self):
		a = {"head": "a" * 40, "files": {"x.py": "1"}}
		self.assertIn("HEAD moved", rg.compare_snapshots(a, {"head": "b" * 40, "files": {"x.py": "1"}})[0])
		self.assertIn("x.py", rg.compare_snapshots(a, {"head": "a" * 40, "files": {"x.py": "2"}})[0])
		self.assertIn("y.py", rg.compare_snapshots(a, {"head": "a" * 40, "files": {"x.py": "1", "y.py": "3"}})[0])
		self.assertEqual(rg.compare_snapshots(a, dict(a)), [])

	def test_summary_line_carries_head_project_total_and_per_file_counts(self):
		scan = self.scan({"demo.tests.test_alpha": 4}, (9, 0, 0))
		line = rg.summary("PASS", "abcdef0123456789", "dev-x", scan, {"demo.tests.test_alpha": 4})
		self.assertEqual(line, "run-guard: PASS head=abcdef012 project=dev-x ran=9 "
			"changed-test-files=1 (demo.tests.test_alpha=4)")


def git(repo, *args):
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root):
	"""A repo with branch `base` (the stand-in for origin/develop) and a demo app with two test modules."""
	repo = Path(root)
	git(repo, "init", "-q", "-b", "base")
	git(repo, "config", "user.email", "t@example.com")
	git(repo, "config", "user.name", "t")
	tests = repo / "apps" / "demo" / "demo" / "tests"
	tests.mkdir(parents=True)
	(tests / "test_alpha.py").write_text("def test_a(): pass\n")
	(repo / ".gitignore").write_text("frappe-bench/\n")
	git(repo, "add", "-A")
	git(repo, "commit", "-q", "-m", "base")
	git(repo, "checkout", "-q", "-b", "work")
	return repo


class RealGit(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.repo = make_repo(self.tmp.name)
		self.tests = self.repo / "apps" / "demo" / "demo" / "tests"

	def test_changed_files_cover_committed_staged_and_untracked_but_not_deleted(self):
		(self.tests / "test_beta.py").write_text("def test_b(): pass\n")
		git(self.repo, "add", "-A")
		git(self.repo, "commit", "-q", "-m", "beta")
		(self.tests / "test_alpha.py").write_text("def test_a(): pass\ndef test_a2(): pass\n")
		(self.tests / "test_gamma.py").write_text("def test_g(): pass\n")
		(self.tests / "helpers.py").write_text("x = 1\n")
		self.assertEqual(rg.changed_files(self.repo, "base"), [
			"apps/demo/demo/tests/helpers.py", "apps/demo/demo/tests/test_alpha.py",
			"apps/demo/demo/tests/test_beta.py", "apps/demo/demo/tests/test_gamma.py"])
		found = rg.changed_test_modules(rg.changed_files(self.repo, "base"), rg.parallel_tests.discover(self.repo / "apps", ["demo"]))
		self.assertEqual(found, {"demo.tests.test_alpha": 2, "demo.tests.test_beta": 1, "demo.tests.test_gamma": 1})

	def test_a_missing_base_ref_says_how_to_get_it(self):
		with self.assertRaisesRegex(rg.GuardError, "git fetch origin develop.*BASE_REF"):
			rg.changed_files(self.repo, "origin/develop")

	def test_snapshot_sees_edits_staging_new_files_and_head_moves(self):
		first = rg.snapshot(self.repo)
		self.assertEqual(first["files"], {})
		(self.tests / "test_alpha.py").write_text("def test_a(): pass\n# edit\n")
		edited = rg.snapshot(self.repo)
		self.assertEqual(list(edited["files"]), ["apps/demo/demo/tests/test_alpha.py"])
		git(self.repo, "add", "-A")
		self.assertEqual(rg.snapshot(self.repo), edited)  # staging is not a change
		(self.tests / "test_new.py").write_text("x = 1\n")
		self.assertIn("apps/demo/demo/tests/test_new.py", rg.snapshot(self.repo)["files"])
		git(self.repo, "commit", "-q", "-am", "x")
		self.assertNotEqual(rg.snapshot(self.repo)["head"], first["head"])


class WholeGuard(unittest.TestCase):
	"""The guard around a fake test command; the probe is a plain `cat` of the nonce file, which
	is what the container's probe does when the container mounts this checkout."""

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.repo = make_repo(self.tmp.name)
		(self.repo / "frappe-bench" / "logs").mkdir(parents=True)
		self.good_probe = f"cat {self.repo}/{rg.NONCE_FILE}"
		(self.repo / "apps" / "demo" / "demo" / "tests" / "test_beta.py").write_text("def test_b(): pass\n")  # untracked = changed

	def run_guard(self, command, probe=None):
		out, err = io.StringIO(), io.StringIO()
		argv = ["--project", "dev-demo", "--container", "no-such-container", "--probe", probe or self.good_probe,
			"--apps", "demo", "--base", "base", "--repo", str(self.repo), "--", *command]
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = rg.main(argv)
		return code, out.getvalue(), err.getvalue()

	def fake_run(self, *, beta=True, total=None, extra=""):
		"""A shell command printing what the real runner prints."""
		lines = ["demo.tests.test_alpha.One", "    ✔  test_a"]
		if beta:
			lines += ["demo.tests.test_beta.Two", "    ✔  test_b"]
		lines.append(total or f"TOTAL ran={1 + beta} failures=0 errors=0")
		script = "".join(f"printf '%s\\n' '{line}'; " for line in lines) + extra
		return ["sh", "-c", script]

	def test_a_clean_run_passes_and_ends_with_the_evidence_line(self):
		code, out, err = self.run_guard(self.fake_run())
		self.assertEqual((code, err), (0, ""))
		self.assertEqual(out.splitlines()[-1], re.sub(r"head=\w+", f"head={git(self.repo, 'rev-parse', 'HEAD')[:9]}",
			"run-guard: PASS head=X project=dev-demo ran=2 changed-test-files=1 (demo.tests.test_beta=1)"))
		self.assertFalse((self.repo / rg.NONCE_FILE).exists())

	def test_a_new_test_file_that_never_ran_fails_the_run(self):
		code, _, err = self.run_guard(self.fake_run(beta=False))
		self.assertEqual(code, 1)
		self.assertIn("changed test file(s) not executed: demo.tests.test_beta", err)
		self.assertIn("run-guard: FAIL", err.splitlines()[-1])

	def test_zero_tests_fails_the_run(self):
		code, _, err = self.run_guard(["sh", "-c", "echo 'TOTAL ran=0 failures=0 errors=0'"])
		self.assertEqual(code, 1)
		self.assertIn("no test ran", err)

	def test_head_moving_mid_run_fails_the_run(self):
		code, _, err = self.run_guard(self.fake_run(extra=f"git -C {self.repo} commit -q --allow-empty -m moved"))
		self.assertEqual(code, 1)
		self.assertIn("HEAD moved during the run", err)

	def test_a_file_edited_mid_run_fails_the_run(self):
		code, _, err = self.run_guard(self.fake_run(extra=f"echo '# late edit' >> {self.repo}/apps/demo/demo/tests/test_alpha.py"))
		self.assertEqual(code, 1)
		self.assertIn("test_alpha.py", err)

	def test_a_stack_mounting_another_tree_stops_the_run_before_any_test(self):
		marker = self.repo / "ran-anyway"
		code, _, err = self.run_guard(["touch", str(marker)], probe="echo some-other-trees-marker")
		self.assertEqual(code, 2)
		self.assertIn("does not mount this checkout", err)
		self.assertIn("make dev-up", err)
		self.assertFalse(marker.exists())

	def test_a_failing_test_command_keeps_its_exit_code(self):
		code, _, err = self.run_guard(self.fake_run(extra="exit 3"))
		self.assertEqual(code, 3)
		self.assertIn("exited 3", err)


class MakefileWiring(unittest.TestCase):
	def test_both_test_targets_run_through_the_guard(self):
		text = (Path(__file__).resolve().parents[2] / "Makefile").read_text()
		for target in ("test", "test-serial"):
			recipe = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", text, re.M).group(1)
			self.assertTrue(recipe.lstrip().startswith("$(RUN_GUARD) $(COMPOSE) exec"), target)
		self.assertIn("scripts/ci/run_guard.py", text)


if __name__ == "__main__":
	unittest.main()
