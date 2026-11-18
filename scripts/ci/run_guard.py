"""Make a test run that cannot be trusted fail, instead of leaving a human to notice.

`make test` and `make test-serial` run their command through this guard (host side, around the
`docker compose exec`):

    python3 scripts/ci/run_guard.py --project dev-foo --container gunstore-foo-frappe \\
        --probe '<prints the nonce file as the container sees it>' --apps "ffl_core ..." \\
        --base origin/develop -- <test command>

Before the run (any failure here stops it before a single test starts):
  * the base ref resolves, so "changed test files" can be worked out;
  * the container mounts THIS checkout: the guard writes a nonce under frappe-bench/logs and the
    probe must read the same nonce at /workspace/frappe-bench/logs inside the container.

After the run, the run fails (exit 1, whatever the tests said) when:
  * HEAD, or any file that differs from HEAD (tracked changes, staged or not, and new untracked
    files), is not the same as at the start: the result describes another tree;
  * no test ran, or the run never printed its TOTAL line;
  * a test file added or modified against the merge-base with the base ref has no executed test.

The last line is a one-line summary (HEAD, compose project, tests run, executed count per changed
test file): the evidence a reviewer reads. Tests are counted from the runner's own output (the
`pkg.module.Class` header and one result line per test, the same in both targets); the rerun of a
failed module in `make test` is not counted twice.

Not covered, on purpose: JS tests (`make test-js`), a new test method inside an already-collected
file, and test files outside apps/<app>/<app>/ (scripts/ci has its own CI job).
"""
import argparse
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import parallel_tests  # noqa: E402  (its discover() is the module walk Frappe and CI use)

NONCE_FILE = "frappe-bench/logs/.run-guard"
NONCE_IN_CONTAINER = "/workspace/" + NONCE_FILE
MAX_NAMED = 12  # names listed in one message or summary before "+N more"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULT = re.compile(r"^\s+(✔|✖|=)\s+test")  # pass / fail+error / skip, frappe.testing.result.TestResult
CLASS_HEADER = re.compile(r"^[A-Za-z_]\w*(\.\w+)+$")
TOTAL = re.compile(r"^TOTAL ran=(\d+) failures=(\d+) errors=(\d+)")


class GuardError(Exception):
	"""A problem found before the tests start; the message says what to do."""


# --- reading the runner's output (pure, unit-tested) ---------------------------

def module_of(header, known):
	"""The test module a `pkg.module.Class` (or `pkg.module.Outer.Inner`) header belongs to."""
	parts = header.split(".")
	for i in range(len(parts) - 1, 0, -1):
		if ".".join(parts[:i]) in known:
			return ".".join(parts[:i])
	return ".".join(parts[:-1])


class LogScan:
	"""Counts executed tests per module from the runner's output, line by line."""

	def __init__(self, known):
		self.known = known
		self.executed = Counter()  # passed or failed, per module
		self.skipped = Counter()
		self.total = None  # (ran, failures, errors) from the TOTAL line
		self._module = None
		self._rerun = False

	def feed(self, raw):
		line = ANSI.sub("", raw).rstrip("\r\n")
		if line.startswith("::group::"):
			self._rerun = line.startswith("::group::rerun")  # make test's second look at a failed module
		elif line.startswith("::endgroup::"):
			self._rerun = False
		elif TOTAL.match(line):
			self.total = tuple(int(n) for n in TOTAL.match(line).groups())
		elif CLASS_HEADER.match(line):
			self._module = module_of(line, self.known)
		elif self._module and not self._rerun:
			found = RESULT.match(line)
			if found:
				(self.skipped if found.group(1) == "=" else self.executed)[self._module] += 1


# --- git (real git, unit-tested against a temporary repo) -------------------------

def git(repo, *args, stdin=None):
	done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, input=stdin)
	if done.returncode:
		raise GuardError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
	return done.stdout


def _paths(output):
	return {p for p in output.split("\0") if p}


def snapshot(repo):
	"""HEAD plus a content hash of everything that differs from it, so an edit, a staged change,
	a new untracked file or a moved HEAD all show up."""
	head = git(repo, "rev-parse", "HEAD").strip()
	paths = _paths(git(repo, "diff", "HEAD", "--name-only", "-z")) | _paths(
		git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
	files = {p: "deleted" for p in paths if not os.path.lexists(os.path.join(repo, p))}
	present = sorted(paths - set(files))
	if present:
		hashes = git(repo, "hash-object", "--stdin-paths", stdin="\n".join(present) + "\n").split()
		files.update(zip(present, hashes))
	return {"head": head, "files": files}


def changed_files(repo, base_ref):
	"""Files added, modified or renamed against the merge-base with base_ref, working tree included."""
	try:
		base = git(repo, "merge-base", "HEAD", base_ref).strip()
	except GuardError as err:
		raise GuardError(f"cannot work out which test files changed: {err} Fetch it (git fetch origin develop) "
			f"or name another base: make test BASE_REF=<ref>.")
	return sorted(_paths(git(repo, "diff", "--name-only", "--diff-filter=AMR", "-z", base)) | _paths(
		git(repo, "ls-files", "--others", "--exclude-standard", "-z")))


def changed_test_modules(paths, discovered):
	"""{module: static test count} for the changed files that are test modules of the checked apps."""
	by_path = {m.replace(".", "/") + ".py": m for m in discovered}
	found = {}
	for path in paths:
		parts = path.split("/")
		if len(parts) > 3 and parts[0] == "apps":
			module = by_path.get("/".join(parts[2:]))
			if module:
				found[module] = discovered[module]
	return found


# --- the verdict (pure, unit-tested) ------------------------------------------------

def _named(names):
	names = sorted(names)
	return ", ".join(names[:MAX_NAMED]) + (f" (+{len(names) - MAX_NAMED} more)" if len(names) > MAX_NAMED else "")


def compare_snapshots(begin, end):
	if begin["head"] != end["head"]:
		return [f"HEAD moved during the run ({begin['head'][:9]} -> {end['head'][:9]}), so the result describes "
			"neither tree. Do not commit, pull, rebase or switch branches in this checkout while tests run "
			"(work in another worktree), then run again."]
	b, e = begin["files"], end["files"]
	changed = {p for p in set(b) | set(e) if b.get(p) != e.get(p)}
	if changed:
		return [f"{len(changed)} file(s) changed during the run ({_named(changed)}), so the result mixes two "
			"states of the tree. Stop editing this checkout while tests run (or find out what writes into "
			"tracked files), then run again."]
	return []


def judge(scan, expected, base_ref, code):
	"""Problems with the run itself. expected: {module: static test count} of the changed test modules."""
	if scan.total is None:
		return [] if code else ["the run never printed its TOTAL line, so it did not finish; read the output above."]
	problems = []
	if scan.total[0] == 0:
		problems.append("no test ran (TOTAL ran=0), yet Frappe exits 0 when it collects nothing. Check that the "
			"site has allow_tests and the three apps installed (make dev-site-create, make dev-install-our-apps).")
	silent = sorted(m for m, n in expected.items() if n and not scan.executed[m])
	if silent:
		skipped = [f"{m} ({scan.skipped[m]} skipped)" for m in silent if scan.skipped[m]]
		problems.append(f"changed test file(s) not executed: {_named(silent)}. They differ from {base_ref} yet the "
			"run executed none of their tests" + (f"; skipped: {_named(skipped)}" if skipped else "") +
			". Likely not collected (a name not starting with test_, an import error, a skip) or the stack runs "
			"another tree. Run that module alone (make dev-shell, then bench --site dev.localhost run-tests "
			"--module <module>) and read what it prints.")
	return problems


def summary(verdict, head, project, scan, expected):
	ran = scan.total[0] if scan.total else "?"
	listed = [f"{m}={scan.executed[m]}" for m in sorted(expected)]
	more = f", +{len(listed) - MAX_NAMED} more" if len(listed) > MAX_NAMED else ""
	changed = f"{len(listed)} ({', '.join(listed[:MAX_NAMED])}{more})" if listed else "0"
	return f"run-guard: {verdict} head={head[:9]} project={project} ran={ran} changed-test-files={changed}"


# --- the run -----------------------------------------------------------------------

def mount_source(container):
	"""Best effort, for the message only: what the container mounts at /workspace."""
	done = subprocess.run(["docker", "inspect", "-f",
		'{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}', container],
		capture_output=True, text=True)
	return done.stdout.strip() or "unknown (container missing?)"


def check_mount(repo, args):
	nonce = uuid.uuid4().hex
	path = Path(repo) / NONCE_FILE
	if not path.parent.is_dir():
		raise GuardError(f"{path.parent} does not exist: this checkout has no bench. Run make dev-up && make "
			"dev-bench-clone (a worktree) or make dev-bench-init (the main checkout).")
	path.write_text(nonce)
	try:
		seen = ""
		for attempt in range(3):  # a bind mount can lag a moment behind a host write
			probe = subprocess.run(args.probe, shell=True, capture_output=True, text=True)
			seen = probe.stdout.strip()
			if seen == nonce:
				return
			time.sleep(1)
		raise GuardError(f"the container for compose project '{args.project}' does not mount this checkout: the "
			f"marker the guard wrote to {path} is {'not the one' if seen else 'not'} visible at "
			f"{NONCE_IN_CONTAINER}. {args.container} mounts {mount_source(args.container)}, this checkout is "
			f"{repo}, so the tests would run another tree's code. Run make dev-up from this checkout to recreate "
			"the containers onto it (a new worktree also needs make dev-bench-clone); never bare docker compose. "
			f"make dev-names shows which project this checkout uses.{'' if seen else ' If the stack is simply down, start it with make dev-up.'}")
	finally:
		path.unlink(missing_ok=True)


def run_command(command, scan):
	"""Run the test command, echoing its output as it comes. Returns its exit code (None if interrupted)."""
	with subprocess.Popen(["env", *command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:  # env: leading VAR=value words
		try:
			for raw in proc.stdout:
				text = raw.decode("utf-8", "replace")
				sys.stdout.write(text)
				sys.stdout.flush()
				scan.feed(text)
			return proc.wait()
		except KeyboardInterrupt:
			proc.wait()
			return None


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--project", required=True)
	parser.add_argument("--container", required=True)
	parser.add_argument("--probe", required=True, help="shell command printing NONCE_IN_CONTAINER as the container sees it")
	parser.add_argument("--apps", required=True)
	parser.add_argument("--base", default="origin/develop")
	parser.add_argument("--repo", default=None, help="the checkout (default: the one this script is in)")
	parser.add_argument("command", nargs=argparse.REMAINDER)
	args = parser.parse_args(argv)
	command = args.command[1:] if args.command[:1] == ["--"] else args.command
	if not command:
		parser.error("no test command after --")
	repo = Path(args.repo or HERE.parents[1]).resolve()

	try:
		discovered = parallel_tests.discover(repo / "apps", args.apps.split())
		expected = changed_test_modules(changed_files(repo, args.base), discovered)
		check_mount(repo, args)
		begin = snapshot(repo)
	except GuardError as err:
		print(f"run-guard: REFUSED before the run: {err}", file=sys.stderr)
		return 2

	scan = LogScan(set(discovered))
	code = run_command(command, scan)
	if code is None:
		print("run-guard: interrupted; the run is not judged", file=sys.stderr)
		return 130
	try:
		end = snapshot(repo)
	except GuardError as err:
		print(f"run-guard: cannot read git after the run: {err}", file=sys.stderr)
		return 2
	problems = compare_snapshots(begin, end) + judge(scan, expected, args.base, code)
	for problem in problems:
		print(f"run-guard: ERROR: {problem}", file=sys.stderr)
	if code:
		print(f"run-guard: the test command itself exited {code}", file=sys.stderr)
	verdict = "FAIL" if problems or code else "PASS"
	print(summary(verdict, begin["head"], args.project, scan, expected), file=sys.stderr if verdict == "FAIL" else sys.stdout)
	return code or (1 if problems else 0)


if __name__ == "__main__":
	sys.exit(main())
