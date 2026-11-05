"""Run our apps' test modules across several sites of one bench at once.

Used by .github/workflows/_app-tests.yml when `workers` > 1 or `modules` is set:

    python3 parallel_tests.py --bench frappe-bench --apps "ffl_core ffl_integrations ffl_woo_sync" \\
        --workers 8 --base-site ci.localhost --db-root-password root --timings timings.json \\
        [--modules "ffl_core.tests.test_build_order ffl_core.tests.test_edit_order"]

1. Discover every test module (the same walk as frappe.testing.discovery), or
   only the ones named by --modules; a name that does not exist is an error.
2. Split the modules into N groups of about equal time, using last run's
   per-module seconds from --timings, or test-method counts for new modules.
3. Clone the prepared base site N-1 times: the database is copied inside
   MariaDB and the site config copied with the clone's credentials, so every
   worker starts from identical data.
4. Run one parallel_worker.py per site at once; print each worker's log as a group.
5. Re-run each failed module once, alone, in a fresh process. A module that
   passes on the re-run is FLAKY: reported, not fatal. Failing again fails the job.
6. Fail when a worker crashed or nothing ran at all.
7. Save the measured per-module seconds to --timings for the next split.

`parallel_tests.py plan` splits the modules across several CI jobs (shards) the same way,
once, before any of them starts (the `plan` job of _app-tests.yml): each shard is then
handed its own list through --modules and splits it again across its workers. Planning
once keeps the shards disjoint and complete whatever timings each runner would read.
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {"node_modules", "locals", "public", "__pycache__"}
RERUN_LIMIT = 5  # more failed modules than this is a real break, not flakiness
CLONE_PREFIX = "ci-w"


# --- pure planning (unit-tested) ---------------------------------------------

def discover(apps_dir, apps):
	"""{module: test-method count} for every test module, like frappe.testing.discovery."""
	found = {}
	for app in apps:
		root = Path(apps_dir) / app / app
		for path, folders, files in os.walk(root):
			folders[:] = sorted(f for f in folders if not f.startswith(".") and f not in SKIP_DIRS)
			if os.path.join("doctype", "doctype", "boilerplate") in path:
				continue
			for name in sorted(files):
				if name.startswith("test_") and name.endswith(".py") and name != "test_runner.py":
					file = Path(path) / name
					module = ".".join(file.relative_to(root.parent).with_suffix("").parts)
					found[module] = file.read_text(errors="replace").count("def test_")
	return found


def select(found, wanted):
	"""Keep only the modules named in `wanted` (space- or comma-separated)."""
	names = [n for n in wanted.replace(",", " ").split() if n]
	missing = sorted(set(names) - set(found))
	if missing:
		raise SystemExit(f"--modules names modules that do not exist: {', '.join(missing)}")
	return {n: found[n] for n in names} if names else dict(found)


def weights(modules, timings):
	"""Seconds per module: measured where known, else test count × the average seconds per test."""
	known = {m: timings[m] for m in modules if m in timings}
	per_test = (sum(known.values()) / max(1, sum(modules[m] for m in known))) if known else 0.2
	return {m: known.get(m, max(1, count) * per_test) for m, count in modules.items()}


def partition(module_weights, workers):
	"""Greedy balance: heaviest module first onto the lightest group; each group keeps sorted order."""
	groups = [[] for _ in range(workers)]
	loads = [0.0] * workers
	for module, weight in sorted(module_weights.items(), key=lambda kv: (-kv[1], kv[0])):
		i = loads.index(min(loads))
		groups[i].append(module)
		loads[i] += weight
	return [sorted(g) for g in groups if g], loads


def shard_legs(legs, groups):
	"""The job matrix legs with the core leg replaced by one leg per group of modules.

	legs: [{"suite": "core"}, {"suite": "extensions"}, ...] as the extensions gate decides;
	every other leg is kept once, as it is. A shard never gets an empty list: an empty
	--modules would mean every module."""
	if not groups or not all(groups):
		raise SystemExit("cannot shard: a shard would get no modules")
	out = []
	for leg in legs:
		if leg.get("suite") != "core":
			out.append(leg)
			continue
		out += [dict(leg, shard=k, shards=len(groups), modules=" ".join(g)) for k, g in enumerate(groups, 1)]
	return out


def plan_main(argv):
	parser = argparse.ArgumentParser(prog="parallel_tests.py plan", description="Split the test modules across CI jobs")
	parser.add_argument("--apps-dir", required=True, help="the checkout's apps/ (the modules are discovered there)")
	parser.add_argument("--apps", required=True)
	parser.add_argument("--shards", type=int, required=True)
	parser.add_argument("--modules", default="")
	parser.add_argument("--timings", default="")
	parser.add_argument("--legs", default='[{"suite": "core"}]', help="JSON list of matrix legs (the extensions gate's)")
	args = parser.parse_args(argv)
	modules = select(discover(args.apps_dir, args.apps.split()), args.modules)
	groups, loads = partition(weights(modules, read_json(args.timings, {}) if args.timings else {}), max(1, args.shards))
	legs = shard_legs(json.loads(args.legs), groups)
	print(f"{len(modules)} test modules in {len(groups)} shards; planned seconds per shard: "
		f"{', '.join(f'{x:.0f}' for x in loads if x)}")
	for leg in legs:
		print(f"  {leg.get('suite')} {leg.get('shard', '')}: {len(leg.get('modules', '').split())} modules")
	line = f"legs={json.dumps(legs, separators=(',', ':'))}\n"
	if os.environ.get("GITHUB_OUTPUT"):
		with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
			fh.write(line)
	else:
		sys.stdout.write(line)
	return 0


def verdict(first, reruns):
	"""(ok, flaky, broken, notes) from worker results and {module: rerun result}."""
	notes, broken, flaky = [], [], []
	crashed = [r for r in first if r.get("crash")]
	for r in crashed:
		notes.append(f"worker on {r['site']} crashed before its tests ran: {r['crash']}")
	if not crashed and sum(r.get("tests_run", 0) for r in first) == 0:
		notes.append("no tests ran at all")
	failed = sorted({m for r in first for m in r.get("failed_modules", [])})
	if "?" in failed:
		notes.append("a failure could not be tied to a module")
	if len(failed) > RERUN_LIMIT:
		broken = failed
		notes.append(f"{len(failed)} modules failed; more than {RERUN_LIMIT} is not flakiness, so nothing was re-run")
	else:
		for module in failed:
			again = reruns.get(module)
			if again and not again.get("crash") and again.get("tests_run", 0) > 0 and not again.get("failed_modules"):
				flaky.append(module)
			else:
				broken.append(module)
	ok = not crashed and not broken and not any(n.startswith("no tests") for n in notes)
	return ok, flaky, broken, notes


def totals(first, flaky):
	"""(ran, failures, errors) over the first pass. A module that failed and then passed
	alone (flaky) is not counted as failing; a crashed worker counts as one error."""
	ran = sum(r.get("tests_run", 0) for r in first)
	failures = errors = 0
	for r in first:
		failures += sum(n for m, n in r.get("failure_counts", {}).items() if m not in flaky)
		errors += sum(n for m, n in r.get("error_counts", {}).items() if m not in flaky)
		errors += 1 if r.get("crash") else 0
	return ran, failures, errors


# --- bench / database I/O -----------------------------------------------------

def read_json(path, default):
	try:
		return json.loads(Path(path).read_text())
	except (OSError, ValueError):
		return default


def db_command(args, tool, *extra):
	# MYSQL_PWD instead of -p keeps the password off the process list and out of warnings
	binary = shutil.which(tool) or shutil.which({"mariadb": "mysql", "mariadb-dump": "mysqldump"}[tool])
	return [binary, "-h", str(args.db_host), "-P", str(args.db_port), "-uroot", *extra]


def db_env(args):
	return dict(os.environ, MYSQL_PWD=args.db_root_password)


def sql(args, statement):
	subprocess.run(db_command(args, "mariadb", "-e", statement), check=True, env=db_env(args))


def clone_sites(args, sites_dir, count):
	"""Create count clones of the base site; returns [(site, database)]."""
	base_conf = read_json(sites_dir / args.base_site / "site_config.json", None)
	if not base_conf:
		raise SystemExit(f"base site {args.base_site} has no site_config.json")
	for stale in sites_dir.glob(f"{CLONE_PREFIX}*.localhost"):
		shutil.rmtree(stale, ignore_errors=True)
	if count == 0:
		return []
	dump = Path(tempfile.mkdtemp(prefix="ci-clone-")) / "base.sql"
	with open(dump, "wb") as fh:
		subprocess.run(db_command(args, "mariadb-dump", "--single-transaction", "--quick", "--routines",
			"--triggers", base_conf["db_name"]), stdout=fh, check=True, env=db_env(args))
	def make(k):
		site = f"{CLONE_PREFIX}{k}.localhost"
		db = f"{base_conf['db_name']}_w{k}"[:64]
		password = os.urandom(12).hex()
		sql(args, f"DROP DATABASE IF EXISTS `{db}`; CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
			f" CREATE OR REPLACE USER '{db}'@'%' IDENTIFIED BY '{password}'; GRANT ALL PRIVILEGES ON `{db}`.* TO '{db}'@'%';")
		with open(dump, "rb") as fh:
			subprocess.run(db_command(args, "mariadb", db), stdin=fh, check=True, env=db_env(args))
		conf = dict(base_conf, db_name=db, db_password=password)
		if "db_user" in base_conf:
			conf["db_user"] = db
		for sub in ("private/files", "private/backups", "public/files", "logs", "locks"):
			(sites_dir / site / sub).mkdir(parents=True, exist_ok=True)
		(sites_dir / site / "site_config.json").write_text(json.dumps(conf, indent=1))
		return site, db

	with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
		clones = list(pool.map(make, range(2, count + 2)))
	shutil.rmtree(dump.parent, ignore_errors=True)
	return clones


def start_worker(args, sites_dir, site, modules, tag):
	out = Path(args.work_dir) / f"{tag}.json"
	log = open(Path(args.work_dir) / f"{tag}.log", "w")
	proc = subprocess.Popen(
		[str(Path(args.bench) / "env" / "bin" / "python"), str(HERE / "parallel_worker.py"),
			"--site", site, "--modules", ",".join(modules), "--result", str(out)],
		cwd=sites_dir, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, CI="true"))
	return proc, out, log


def run_batch(args, sites_dir, assignments, tag):
	"""assignments: [(site, [modules])] run at once; returns results in the same order."""
	started = [(start_worker(args, sites_dir, site, mods, f"{tag}{i + 1}"), site) for i, (site, mods) in enumerate(assignments)]
	results = []
	for i, ((proc, out, log), site) in enumerate(started):
		proc.wait()
		log.close()
		print(f"::group::{tag}{i + 1} on {site}")
		print((Path(args.work_dir) / f"{tag}{i + 1}.log").read_text(errors="replace"))
		print("::endgroup::")
		results.append(read_json(out, {"site": site, "crash": f"worker exited {proc.returncode} without a result"}))
	return results


def main():
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--bench", required=True)
	parser.add_argument("--apps", required=True)
	parser.add_argument("--workers", type=int, required=True)
	parser.add_argument("--base-site", default="ci.localhost")
	parser.add_argument("--db-root-password", default="root")
	parser.add_argument("--timings", required=True)
	parser.add_argument("--work-dir", default=None)
	parser.add_argument("--modules", default="", help="only these dotted module names (default: every module of --apps)")
	args = parser.parse_args()

	bench = Path(args.bench).resolve()
	args.bench = str(bench)  # workers are started from <bench>/sites, so no relative paths
	sites_dir = bench / "sites"
	common = read_json(sites_dir / "common_site_config.json", {})
	args.db_host, args.db_port = common.get("db_host", "127.0.0.1"), common.get("db_port", 3306)
	args.work_dir = args.work_dir or tempfile.mkdtemp(prefix="ci-parallel-")
	Path(args.work_dir).mkdir(parents=True, exist_ok=True)

	discovered = discover(bench / "apps", args.apps.split())
	modules = select(discovered, args.modules)
	timings = read_json(args.timings, {})
	groups, loads = partition(weights(modules, timings), max(1, args.workers))
	print(f"{len(modules)} test modules, {sum(modules.values())} test methods, {len(groups)} workers; "
		f"planned seconds per worker: {', '.join(f'{x:.0f}' for x in loads if x)}")

	began = time.monotonic()
	clones = clone_sites(args, sites_dir, len(groups) - 1)
	sites = [args.base_site] + [site for site, _ in clones]
	print(f"cloned {len(clones)} sites in {time.monotonic() - began:.0f}s")
	try:
		first = run_batch(args, sites_dir, list(zip(sites, groups)), "worker")
		failed = sorted({m for r in first for m in r.get("failed_modules", []) if m != "?"})
		reruns = {}
		if failed and len(failed) <= RERUN_LIMIT and not any(r.get("crash") for r in first):
			print(f"re-running {len(failed)} failed module(s) once, each alone: {', '.join(failed)}")
			for i, module in enumerate(failed):
				reruns[module] = run_batch(args, sites_dir, [(sites[0], [module])], f"rerun{i + 1}-")[0]
	finally:
		for site, db in clones:  # CI's database container is thrown away anyway; this is for local runs
			shutil.rmtree(sites_dir / site, ignore_errors=True)
			subprocess.run(db_command(args, "mariadb", "-e", f"DROP DATABASE IF EXISTS `{db}`; DROP USER IF EXISTS '{db}'@'%';"),
				env=db_env(args), check=False)

	measured = dict(timings)
	for r in first:
		measured.update(r.get("module_seconds", {}))
	target = Path(args.timings)
	target.parent.mkdir(parents=True, exist_ok=True)
	scratch = target.with_suffix(f".{os.getpid()}.tmp")  # several runners share the file: write whole, then swap
	# Keep every discovered module's history: a --modules run must not erase the rest.
	scratch.write_text(json.dumps({m: s for m, s in measured.items() if m in discovered}, indent=1, sort_keys=True))
	os.replace(scratch, target)

	ok, flaky, broken, notes = verdict(first, reruns)
	total = sum(r.get("tests_run", 0) for r in first)
	walls = ", ".join(f"{r.get('wall_seconds', 0):.0f}s" for r in first)
	print(f"ran {total} tests in {time.monotonic() - began:.0f}s (worker wall: {walls})")
	for note in notes:
		print(f"::error::{note}")
	for module in flaky:
		print(f"::warning::FLAKY: {module} failed, then passed when re-run alone")
	for module in broken:
		print(f"::error::FAILED: {module}")
	# Same shape as `make test-serial`'s last line, so one grep reads either run.
	print("TOTAL ran={} failures={} errors={}".format(*totals(first, flaky)))
	return 0 if ok else 1

if __name__ == "__main__":
	sys.exit(plan_main(sys.argv[2:]) if sys.argv[1:2] == ["plan"] else main())
