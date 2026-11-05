"""Verdict for the all-checks-pass job in release-image.yml.

Reads NEEDS (the workflow's toJSON(needs)), TESTS_REQUIRED, DRAFT and REUSED ("true"/"false").
A draft pull request always fails: nothing ran, and a pass here would sit on
the commit as a green required check once the PR is marked ready.
A gate that failed or was cancelled fails the verdict. A skipped gate passes
only where skipping is legitimate: the test jobs on runs that do not need them
(plain develop PRs, develop pushes), and release-version (release PRs into main only).
api-docs-freshness and build always run, except on a push to main that REUSED the image a
release PR built and fully tested from the same tree (reuse-image): build and tests are then
skipped on purpose. reuse-image and stamp-tested run on one kind of event each, so their skip
is never a problem; their failure is.
"""
import json
import os
import sys

ALWAYS_RUN = {"api-docs-freshness", "build"}
OPTIONAL = {"reuse-image", "stamp-tested"}
REUSED_SKIP = {"build", "tests"}


def problems(needs, tests_required, draft=False, reused=False):
	if draft:
		return ["draft pull request: the checks run once it is marked ready for review"]
	bad = []
	for job, info in sorted(needs.items()):
		result = info.get("result")
		if result in ("failure", "cancelled"):
			bad.append(f"{job}: {result}")
		elif result == "skipped" and (job in OPTIONAL or (reused and job in REUSED_SKIP)):
			continue
		elif result == "skipped" and (job in ALWAYS_RUN or tests_required):
			bad.append(f"{job}: skipped, but this run must execute it")
		elif result not in ("success", "skipped"):
			bad.append(f"{job}: unexpected result {result!r}")
	return bad


def main():
	needs = json.loads(os.environ["NEEDS"])
	bad = problems(needs, os.environ.get("TESTS_REQUIRED") == "true", os.environ.get("DRAFT") == "true",
		os.environ.get("REUSED") == "true")
	print("gates:", {job: info.get("result") for job, info in sorted(needs.items())})
	for line in bad:
		print(f"::error::{line}")
	return 1 if bad else 0


if __name__ == "__main__":
	sys.exit(main())
