#!/usr/bin/env python3
"""What release-image.yml's `release` job does with this main commit.

The version is ffl_core's `__version__` (scripts/bump_version.py's grammar).
Its tag `v<version>` on origin decides:
  new    — not tagged: tag this commit, tag the image, tell the deploy repo.
  resume — tagged at THIS commit: a re-run after a later step failed; the tag
           stays, the image tag and the dispatch are redone (both idempotent).
  taken  — tagged at another commit: this merge kept a released version and
           releases nothing (bump the version to ship it).
Any other `git ls-remote` failure stops the job: never guess that a tag is absent.

The image half (`image`) decides from `docker buildx imagetools inspect` whether the release
job creates the image tag: only an explicit "not found" means absent, any other failure stops.
`latest` is the newest release of THIS repository, so only the release job moves it, never below
this repository's own tags. The private archive line (v1.*) shares the image package, so its
hotfix can move `latest` across lines (docs/versions.md): deploys pin a version, never `latest`.

  release_plan.py           → writes version/tag/action to $GITHUB_OUTPUT
  release_plan.py image     → writes image=create|keep and latest=true|false (env IMG, ACTION)
  release_plan.py build-tag → the image tag the build job publishes (env EVENT, REF_NAME, SHA, DISPATCH_TAG,
                              TREE: a pull request's image is candidate-<tree>)
  release_plan.py reuse     → for a push to main: is there a tested-<tree> image for this commit's tree?
                              writes digest=<sha256:…> or digest= (env IMG, TREE)
  release_plan.py --check   → for a PR into main: fails if the version is already tagged
                              or is not greater than the highest release tag
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from bump_version import SEMVER  # noqa: E402  one grammar for bump and release

INIT = ROOT / "apps" / "ffl_core" / "ffl_core" / "__init__.py"


def read_version(text: str) -> str:
	m = re.search(r'^__version__ = "([^"]*)"', text, re.M)
	version = m.group(1) if m else ""
	if not SEMVER.match(version):
		raise SystemExit(f"::error::ffl_core __version__ is not x.y.z[-pre]: {version!r}")
	return version


def tagged_commit(ls_remote_out: str, tag: str) -> str:
	"""The commit `tag` names in `git ls-remote` output (an annotated tag's peeled line wins)."""
	refs: dict[str, str] = {}
	for line in ls_remote_out.splitlines():
		sha, _, ref = line.partition("\t")
		refs[ref] = sha
	return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}", "")


def plan(rc: int, tag_sha: str, head_sha: str) -> str:
	if rc == 2:
		return "new"
	if rc != 0:
		raise SystemExit(f"::error::git ls-remote failed (exit {rc}) — not guessing whether the tag exists")
	return "resume" if tag_sha == head_sha else "taken"


def version_key(version: str) -> tuple:
	"""Sort key: (major, minor, patch, final=1 / pre=0, pre parts), numeric parts as ints.
	A numeric pre-release part sorts below an alphanumeric one (semver 2.0)."""
	m = SEMVER.match(version)
	if not m:
		raise SystemExit(f"::error::not a release version: {version!r}")
	major, minor, patch, pre = m.groups()
	parts = () if pre is None else tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre[1:].split("."))
	return (int(major), int(minor), int(patch), 0 if pre else 1, parts)


def highest_release(ls_remote_out: str) -> str:
	"""The highest `v<version>` tag in `git ls-remote --tags` output ("" when there is none)."""
	versions = []
	for line in ls_remote_out.splitlines():
		ref = line.partition("\t")[2]
		name = ref.removeprefix("refs/tags/v")
		if ref.startswith("refs/tags/v") and SEMVER.match(name):
			versions.append(name)
	return max(versions, key=version_key, default="")


def check_problem(rc: int, version: str, tags_out: str) -> str:
	"""Why a PR into main may not carry `version` ("" = fine). rc = ls-remote of its own tag (--exit-code)."""
	if rc == 0:
		return f"v{version} is already released"
	if rc != 2:
		raise SystemExit(f"::error::git ls-remote failed (exit {rc}) — not guessing whether the tag exists")
	top = highest_release(tags_out)
	if top and version_key(version) <= version_key(top):
		return f"v{version} is not greater than the highest release v{top}"
	return ""


def newest(version: str, tags_out: str) -> bool:
	"""Is `version` at least the highest release tag (so `latest` may follow it)?"""
	top = highest_release(tags_out)
	return not top or version_key(version) >= version_key(top)


# "not found" / "manifest unknown" is how a registry says the tag is absent; anything else
# (auth, 5xx, rate limit, no network) says nothing about the tag.
ABSENT = re.compile(r"manifest unknown|:\s*not found\b", re.I)


def image_plan(action: str, rc: int, stderr: str) -> str:
	"""create | keep, from the inspect of the version's image tag."""
	if rc == 0:
		exists = True
	elif rc == 1 and ABSENT.search(stderr):
		exists = False
	else:
		raise SystemExit(f"::error::cannot tell whether the image tag exists (inspect exit {rc}): {stderr.strip()[-300:]}")
	if action == "new":
		if exists:
			raise SystemExit("::error::the image tag already exists but the version is not released — an out-of-band image; not moving it")
		return "create"
	if action == "resume":
		return "keep" if exists else "create"  # no digest compare: a re-run rebuilds main-<sha> with a new digest
	raise SystemExit(f"::error::no image to plan for action {action!r}")


RESERVED = ("v", "latest", "main-", "develop-", "candidate-", "tested-")
TREE = re.compile(r"[0-9a-f]{40}")


def build_tag(event: str, ref_name: str, sha: str, dispatch_tag: str, tree: str = "") -> str:
	"""The one tag a build publishes: develop-<sha7> / main-<sha7> / a manual dispatch's own tag.

	A pull request's image is candidate-<tree>, by the git tree of the merge it built: only a
	release PR (into main, from this repository) pushes it, after the smoke; the
	stamp-tested job copies it to tested-<tree> once every gate of that run passed, and a push
	to main whose commit has that same tree reuses it (`reuse`) instead of building again."""
	if event == "workflow_dispatch":
		if not re.fullmatch(r"[A-Za-z0-9._/-]+", dispatch_tag):
			raise SystemExit(f"::error::not a valid image tag: {dispatch_tag!r}")
		if dispatch_tag.startswith(RESERVED):
			raise SystemExit(f"::error::{dispatch_tag!r} is reserved (v*, latest, main-*, develop-*, candidate-*, tested-*): version tags come only from the release job")
		return dispatch_tag
	if event == "pull_request":
		if not TREE.fullmatch(tree):
			raise SystemExit(f"::error::not a git tree id: {tree!r}")
		return f"candidate-{tree}"
	return f"{'develop' if ref_name == 'develop' else 'main'}-{sha[:7]}"


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def reuse_plan(rc: int, stdout: str, stderr: str) -> tuple[str, str]:
	"""(digest, note) from `imagetools inspect <img>:tested-<tree> --format '{{json .Manifest.Digest}}'`.

	digest "" = build and test as usual. Unlike image_plan, nothing here stops the run: any doubt
	(an error, an answer that is not one digest) only costs the full build and tests."""
	if rc == 0:
		found = DIGEST.findall(stdout)
		if len(found) == 1:
			return found[0], "a run that passed every gate built this tree: reusing its image"
		return "", f"::warning::unexpected inspect answer, building as usual: {stdout.strip()[-200:]!r}"
	if rc == 1 and ABSENT.search(stderr):
		return "", "no tested image for this tree (a hotfix, or the release PR was not up to date): building as usual"
	return "", f"::warning::cannot tell whether a tested image exists (inspect exit {rc}), building as usual: {stderr.strip()[-200:]}"


def ls_remote(*refs: str) -> tuple[int, str]:
	p = subprocess.run(["git", "ls-remote", "--exit-code", "--tags", "origin", *refs], capture_output=True, text=True, cwd=ROOT)
	return p.returncode, p.stdout


def release_tags() -> str:
	rc, out = ls_remote("refs/tags/v*")
	if rc not in (0, 2):  # 2 = no release tag yet
		raise SystemExit(f"::error::git ls-remote failed (exit {rc}) — not guessing which releases exist")
	return out


def output(**kv: str) -> None:
	with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
		fh.writelines(f"{k}={v}\n" for k, v in kv.items())


def main(argv: list[str]) -> int:
	if argv[:1] == ["build-tag"]:
		env = os.environ
		tag = build_tag(env["EVENT"], env["REF_NAME"], env["SHA"], env.get("DISPATCH_TAG", ""), env.get("TREE", ""))
		print(f"image tag: {tag}")
		output(tag=tag)
		return 0
	if argv[:1] == ["reuse"]:
		tree = os.environ["TREE"]
		if not TREE.fullmatch(tree):
			raise SystemExit(f"::error::not a git tree id: {tree!r}")
		ref = f"{os.environ['IMG']}:tested-{tree}"
		p = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest.Digest}}"],
			capture_output=True, text=True)
		digest, note = reuse_plan(p.returncode, p.stdout, p.stderr)
		print(f"{ref}: {note}")
		output(digest=digest)
		return 0
	version = read_version(INIT.read_text())
	tag = f"v{version}"
	if argv[:1] == ["image"]:
		ref = f"{os.environ['IMG']}:{tag}"
		p = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref], capture_output=True, text=True)
		image = image_plan(os.environ["ACTION"], p.returncode, p.stderr)
		latest = newest(version, release_tags())
		print(f"{ref}: {image}; latest {'follows' if latest else 'stays (a higher release exists)'}")
		output(image=image, latest=str(latest).lower())
		return 0
	rc, out = ls_remote(f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
	if "--check" in argv:
		problem = check_problem(rc, version, release_tags())
		if problem:
			print(f"::error::{problem} — bump the version (make bump-version) before merging into main")
			return 1
		print(f"{tag} is new and the highest release: merging releases it")
		return 0
	action = plan(rc, tagged_commit(out, tag), os.environ["GITHUB_SHA"])
	if action == "taken":
		print(f"::warning::{tag} is already released at another commit — this merge ships nowhere until the version is bumped")
	print(f"{tag}: {action}")
	output(version=version, tag=tag, action=action)
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
