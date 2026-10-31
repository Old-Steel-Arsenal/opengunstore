"""Unit tests for scripts/ci (run in release-image.yml's api-docs-freshness job).

python3 -m unittest discover -s scripts/ci -p 'test_*.py'
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import all_checks_pass  # noqa: E402


class AllChecksPass(unittest.TestCase):
	def verdict(self, tests_required=False, **results):
		base = {"api-docs-freshness": "success", "build": "success", "release-version": "success",
			"tests": "success", "upgrade": "success"}
		base.update({k.replace("_", "-"): v for k, v in results.items()})
		return all_checks_pass.problems({k: {"result": v} for k, v in base.items()}, tests_required)

	def test_all_green_passes(self):
		self.assertEqual(self.verdict(), [])

	def test_a_failed_or_cancelled_gate_fails(self):
		self.assertEqual(self.verdict(tests="failure"), ["tests: failure"])
		self.assertEqual(self.verdict(build="cancelled"), ["build: cancelled"])

	def test_skipped_tests_pass_on_a_develop_pr_but_not_on_the_release_pr(self):
		self.assertEqual(self.verdict(tests="skipped", upgrade="skipped"), [])
		self.assertEqual(len(self.verdict(tests_required=True, tests="skipped", upgrade="skipped")), 2)

	def test_a_draft_pull_request_always_fails(self):
		results = {job: {"result": "skipped"} for job in ("api-docs-freshness", "build", "release-version", "tests", "upgrade")}
		self.assertEqual(len(all_checks_pass.problems(results, False, draft=True)), 1)

	def test_the_version_check_is_skippable_off_the_release_pr_only(self):
		self.assertEqual(self.verdict(release_version="skipped"), [])
		self.assertEqual(self.verdict(tests_required=True, release_version="skipped"),
			["release-version: skipped, but this run must execute it"])
		self.assertEqual(self.verdict(tests_required=True, release_version="failure"), ["release-version: failure"])

	def test_build_and_api_docs_may_never_be_skipped(self):
		self.assertEqual(len(self.verdict(build="skipped", api_docs_freshness="skipped")), 2)


REPO_APPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "apps")
OUR_APPS = ["ffl_core", "ffl_integrations", "ffl_woo_sync"]


def rerun_classes(apps_dir, apps):
	"""Test classes unittest would run a second time: its loader collects every
	TestCase in a module's namespace, so a test class imported into another test
	module, or subclassed (even in its own module), re-runs all its test_ methods
	there. Shared fixtures belong in a base class with no test_ methods."""
	import ast
	import parallel_tests

	classes, names = {}, {}  # "mod.Class" -> (base refs, own tests?); mod -> {name: dotted}
	for mod in parallel_tests.discover(apps_dir, apps):
		path = os.path.join(apps_dir, mod.split(".")[0], *mod.split(".")) + ".py"
		with open(path, encoding="utf-8") as f:
			tree = ast.parse(f.read())
		bound = names[mod] = {}
		for node in tree.body:
			if isinstance(node, ast.ImportFrom) and node.module and not node.level:
				bound.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
			elif isinstance(node, ast.Import):
				bound.update({a.asname or a.name: a.name for a in node.names})
		for node in tree.body:
			if isinstance(node, ast.ClassDef):
				own = any(isinstance(f, ast.FunctionDef) and f.name.startswith("test")
					for f in node.body)
				classes[f"{mod}.{node.name}"] = ([ast.unparse(b) for b in node.bases], own)

	def resolve(mod, ref):
		head, _, rest = ref.partition(".")
		if head in names[mod]:
			return names[mod][head] + ("." + rest if rest else "")
		return f"{mod}.{ref}"

	def has_tests(cls, seen=()):
		if cls not in classes or cls in seen:
			return False
		bases, own = classes[cls]
		mod = cls.rsplit(".", 1)[0]
		return own or any(has_tests(resolve(mod, b), seen + (cls,)) for b in bases)

	found = []
	for mod in names:
		found += [f"{mod} imports {full}" for full in names[mod].values() if has_tests(full)]
		for cls, (bases, _own) in classes.items():
			if cls.rsplit(".", 1)[0] == mod:
				found += [f"{cls} subclasses {resolve(mod, b)}" for b in bases
					if has_tests(resolve(mod, b))]
	return sorted(found)


class NoRerunTestClasses(unittest.TestCase):
	maxDiff = None

	def test_no_test_class_is_collected_twice(self):
		self.assertEqual(rerun_classes(REPO_APPS, OUR_APPS), [],
			"move the shared setUpClass/helpers into a base class without test_ methods "
			"and import/subclass that instead (see test_counter_to_store.CounterToStoreFixtures)")

	def test_detects_import_subclass_and_module_alias(self):
		import tempfile
		files = {
			"test_a.py": "class Fix:\n\tpass\nclass A(Fix):\n\tdef test_x(self): pass\n"
				"class A2(A):\n\tpass\n",
			"test_b.py": "from app.tests.test_a import A, Fix\nclass B(Fix):\n\tdef test_y(self): pass\n",
			"test_c.py": "from app.tests import test_a as ta\nclass C(ta.A):\n\tpass\n"
				"class D(ta.Fix):\n\tpass\n",
		}
		with tempfile.TemporaryDirectory() as root:
			os.makedirs(os.path.join(root, "app", "app", "tests"))
			for name, body in files.items():
				with open(os.path.join(root, "app", "app", "tests", name), "w") as f:
					f.write(body)
			self.assertEqual(rerun_classes(root, ["app"]), [
				"app.tests.test_a.A2 subclasses app.tests.test_a.A",
				"app.tests.test_b imports app.tests.test_a.A",
				"app.tests.test_c.C subclasses app.tests.test_a.A",
			])


if __name__ == "__main__":
	unittest.main()


class FrappeRefsAreOneSource(unittest.TestCase):
	"""deploy/prod/frappe-refs.env is what production runs and what CI's reused
	benches are rebuilt to; the Dockerfile's ARG defaults (a bare `docker build`)
	must not drift from it."""

	def test_the_dockerfile_defaults_match_the_pinned_refs(self):
		root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
		pinned = dict(line.strip().split("=", 1) for line in
			open(os.path.join(root, "deploy/prod/frappe-refs.env"))
			if line.strip() and not line.startswith("#"))
		dockerfile = open(os.path.join(root, "deploy/prod/Dockerfile")).read()
		for key in ("FRAPPE_REF", "ERPNEXT_REF", "HRMS_REF"):
			self.assertIn(f"ARG {key}={pinned[key]}\n", dockerfile, key)


class ReleasePlan(unittest.TestCase):
	def setUp(self):
		import release_plan
		self.rp = release_plan

	def test_version_follows_the_bump_grammar(self):
		self.assertEqual(self.rp.read_version('x = 1\n__version__ = "1.12.0"\n'), "1.12.0")
		self.assertEqual(self.rp.read_version('__version__ = "1.12.0-rc.1"\n'), "1.12.0-rc.1")
		for bad in ('__version__ = "1.12"\n', '__version__ = "x"\n', "nothing here\n"):
			with self.assertRaises(SystemExit):
				self.rp.read_version(bad)

	def test_tagged_commit_prefers_the_peeled_line(self):
		light = "aaa\trefs/tags/v1.12.0\n"
		annotated = "ttt\trefs/tags/v1.12.0\nccc\trefs/tags/v1.12.0^{}\n"
		self.assertEqual(self.rp.tagged_commit(light, "v1.12.0"), "aaa")
		self.assertEqual(self.rp.tagged_commit(annotated, "v1.12.0"), "ccc")
		self.assertEqual(self.rp.tagged_commit("", "v1.12.0"), "")

	def test_plan(self):
		self.assertEqual(self.rp.plan(2, "", "head"), "new")
		self.assertEqual(self.rp.plan(0, "head", "head"), "resume")
		self.assertEqual(self.rp.plan(0, "other", "head"), "taken")

	def test_a_failed_ls_remote_never_reads_as_untagged(self):
		for rc in (1, 128):
			with self.assertRaises(SystemExit):
				self.rp.plan(rc, "", "head")

	def test_check_rc2_is_fine_rc0_fails_anything_else_raises(self):
		self.assertEqual(self.rp.check_problem(2, "1.12.0", ""), "")
		self.assertIn("already released", self.rp.check_problem(0, "1.12.0", ""))
		for rc in (1, 128):
			with self.assertRaises(SystemExit):
				self.rp.check_problem(rc, "1.12.0", "")

	def test_check_needs_a_version_above_every_release(self):
		tags = "a\trefs/tags/v1.11.0\nb\trefs/tags/v1.12.0\nc\trefs/tags/v1.12.0^{}\nd\trefs/tags/v-old\ne\trefs/tags/vx.y.z\n"
		self.assertEqual(self.rp.highest_release(tags), "1.12.0")
		self.assertIn("not greater", self.rp.check_problem(2, "1.11.5", tags))                # lower
		self.assertIn("not greater", self.rp.check_problem(2, "1.12.0", tags))                # equal
		self.assertIn("not greater", self.rp.check_problem(2, "1.12.0-rc.1", tags))           # pre-release after its final
		self.assertEqual(self.rp.check_problem(2, "1.12.1", tags), "")                        # higher
		self.assertEqual(self.rp.check_problem(2, "1.13.0-rc.1", tags), "")                   # pre-release of a higher base
		self.assertEqual(self.rp.check_problem(2, "0.0.1", ""), "")                           # first release

	def test_version_order(self):
		order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0-alpha.beta",
			"1.0.0-beta", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2.0.0-rc.1", "2.0.0"]
		keys = [self.rp.version_key(v) for v in order]
		self.assertEqual(keys, sorted(keys))
		self.assertEqual(len(set(keys)), len(keys))

	def test_latest_follows_only_the_newest_release(self):
		tags = "a\trefs/tags/v1.11.0\nb\trefs/tags/v1.12.0\n"
		self.assertTrue(self.rp.newest("1.12.0", tags))    # a resume of the newest
		self.assertTrue(self.rp.newest("1.12.1", tags))
		self.assertFalse(self.rp.newest("1.11.0", tags))   # a re-run of an older release
		self.assertTrue(self.rp.newest("1.0.0", ""))

	def test_a_repo_with_no_tags_releases_its_first_version(self):
		# The public repository starts at 0.1.0 with no tags at all (git ls-remote --exit-code → 2):
		# the release PR's --check passes, the release job tags it (new), and `latest` follows it.
		import contextlib
		import io
		import tempfile
		from unittest import mock

		def no_tags(cmd, **kw):
			if cmd[:2] == ["git", "ls-remote"]:
				return mock.Mock(returncode=2, stdout="", stderr="")
			if cmd[:4] == ["docker", "buildx", "imagetools", "inspect"]:
				return mock.Mock(returncode=1, stdout="", stderr=f"ERROR: {cmd[-1]}: not found")
			raise AssertionError(cmd)

		with tempfile.TemporaryDirectory() as d:
			out = os.path.join(d, "out")
			env = {"GITHUB_OUTPUT": out, "GITHUB_SHA": "c0", "IMG": "ghcr.io/x/y", "ACTION": "new"}
			with mock.patch.object(self.rp, "read_version", return_value="0.1.0"), \
					mock.patch.object(self.rp.subprocess, "run", side_effect=no_tags), \
					mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
				self.assertEqual(self.rp.main(["--check"]), 0)
				self.assertEqual(self.rp.main([]), 0)
				self.assertEqual(self.rp.main(["image"]), 0)
			with open(out) as fh:
				written = fh.read().split()
			self.assertEqual(written,
				["version=0.1.0", "tag=v0.1.0", "action=new", "image=create", "latest=true"])
		# Once v0.1.0 is tagged, the usual rule holds: only a higher version releases.
		tags = "a\trefs/tags/v0.1.0\n"
		self.assertIn("not greater", self.rp.check_problem(2, "0.0.9", tags))
		self.assertIn("not greater", self.rp.check_problem(2, "0.1.0-rc.1", tags))
		self.assertEqual(self.rp.check_problem(2, "0.1.1", tags), "")

	def test_image_plan(self):
		gone = "ERROR: ghcr.io/x/y:v1.12.0: not found"
		self.assertEqual(self.rp.image_plan("new", 1, gone), "create")
		self.assertEqual(self.rp.image_plan("new", 1, "manifest unknown"), "create")
		with self.assertRaises(SystemExit):               # an image nobody released: never moved
			self.rp.image_plan("new", 0, "")
		self.assertEqual(self.rp.image_plan("resume", 0, ""), "keep")    # digest is not compared
		self.assertEqual(self.rp.image_plan("resume", 1, gone), "create")

	def test_an_inspect_failure_other_than_not_found_is_an_error(self):
		for stderr in ("ERROR: unauthorized: authentication required", "ERROR: 500 Internal Server Error",
				"ERROR: toomanyrequests: rate limit", "dial tcp: lookup ghcr.io: no such host", ""):
			for action in ("new", "resume"):
				with self.assertRaises(SystemExit, msg=(action, stderr)):
					self.rp.image_plan(action, 1, stderr)
		with self.assertRaises(SystemExit):               # the docker CLI itself missing is not "absent"
			self.rp.image_plan("new", 127, "sh: 1: docker: not found")
		with self.assertRaises(SystemExit):
			self.rp.image_plan("taken", 0, "")

	def test_build_tag(self):
		sha = "0123456789abcdef"
		self.assertEqual(self.rp.build_tag("push", "main", sha, ""), "main-0123456")
		self.assertEqual(self.rp.build_tag("push", "develop", sha, ""), "develop-0123456")
		self.assertEqual(self.rp.build_tag("workflow_dispatch", "main", sha, "hotfix-try.1"), "hotfix-try.1")
		for bad in ("v1.8.2", "v1", "vault", "latest", "main-abc1234", "develop-abc1234", "", "a b", "x;y"):
			with self.assertRaises(SystemExit, msg=bad):
				self.rp.build_tag("workflow_dispatch", "main", sha, bad)


class Dispatch(unittest.TestCase):
	"""scripts/ci/dispatch.sh against a `gh` shim that records its arguments."""

	def run_script(self, *args, **env):
		import subprocess
		import tempfile
		with tempfile.TemporaryDirectory() as tmp:
			gh = os.path.join(tmp, "gh")
			with open(gh, "w") as fh:
				fh.write('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_LOG"\n')
			os.chmod(gh, 0o755)
			log = os.path.join(tmp, "log")
			e = {"PATH": f"{tmp}:{os.environ['PATH']}", "GH_LOG": log, **env}
			p = subprocess.run([os.path.join(os.path.dirname(os.path.abspath(__file__)), "dispatch.sh"), *args],
				env=e, capture_output=True, text=True)
			if not os.path.exists(log):
				return p, None
			with open(log) as fh:
				return p, fh.read().split()

	def test_sends_the_event_and_the_payload(self):
		p, sent = self.run_script("pos-develop", "tag=develop-abc1234", "sha=" + "f" * 40, DEPLOY_REPO="o/r", GH_TOKEN="t")
		self.assertEqual(p.returncode, 0, p.stderr)
		self.assertEqual(sent, ["api", "repos/o/r/dispatches", "-f", "event_type=pos-develop",
			"-f", "client_payload[tag]=develop-abc1234", "-f", "client_payload[sha]=" + "f" * 40])

	def test_a_release_carries_only_the_tag(self):
		p, sent = self.run_script("pos-release", "tag=v1.12.0", DEPLOY_REPO="o/r", GH_TOKEN="t")
		self.assertEqual(p.returncode, 0, p.stderr)
		self.assertEqual(sent[-2:], ["-f", "client_payload[tag]=v1.12.0"])

	def test_refuses_without_repo_token_event_or_key_value(self):
		for args, env in ((("pos-release", "tag=v1"), {"GH_TOKEN": "t"}),
				(("pos-release", "tag=v1"), {"DEPLOY_REPO": "o/r"}),
				((), {"DEPLOY_REPO": "o/r", "GH_TOKEN": "t"}),
				(("pos-release", "tag"), {"DEPLOY_REPO": "o/r", "GH_TOKEN": "t"})):
			p, sent = self.run_script(*args, **env)
			self.assertNotEqual(p.returncode, 0, (args, env))
			self.assertIsNone(sent, "gh must not run")

	def test_the_token_error_names_the_environment_secret(self):
		p, _ = self.run_script("pos-release", "tag=v1", DEPLOY_REPO="o/r")
		self.assertIn("environment secret", p.stderr)
