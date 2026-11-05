# Contributing — Branch & Deploy Workflow

## Branches

- **`main`** — release branch. Merging to `main` triggers the maintainers'
  deployment pipeline (GitHub Actions). Protected: PR required, CI
  (`all-checks-pass`) must pass, no force-push / no deletion.
- **`develop`** — integration branch. Code here is tested on the **local dev
  environment** (`dev.localhost:8000`, `deploy/dev/docker-compose.yml`; see
  the README quick start).
- **`feature/*`**, **`fix/*`** — work branches; cut from `develop`, PR back into `develop`.

## Normal flow

1. Branch off `develop`: `git checkout -b fix/xyz develop`
2. Build + test locally on `dev.localhost:8000` (`make test` — this is where
   the full suite runs for a develop PR).
3. PR your branch → **`develop`**; merge when CI is green. The develop gate is
   light (api-docs + image build, ~3 min): add the **`ci:full`** label to run
   the full test suite on a PR, **`ci:upgrade`** to also run the upgrade
   regression (a PR that adds a patch), and keep a PR a **draft** to run nothing.
   A new push cancels the PR's in-flight run.
4. **Release prep** (on `develop`, before the release PR): write the release
   note `apps/ffl_core/ffl_core/change_log/v<major>/v<x>_<y>_<z>.md`, then
   `make bump-version VERSION=x.y.z` (sets `__version__` in all three apps,
   regenerates `docs/api/` because `openapi.json` carries the version; refuses
   to run without the note) and commit all of it. A beta is `x.y.z-beta.N`
   with its own note `v<x>_<y>_<z>-beta_<N>.md`. The note is what every user sees in
   the login popup and under the sidebar-header menu → What's New in POS, so write it for the
   people at the counter:
   - heading `### Version x.y.z — Month D, YYYY`; sections `#### New`,
     `#### Improved`, `#### Fixed` (drop empty ones);
   - one bullet per change: **bold feature name** + one sentence on what you
     can now do / what is different;
   - plain English only — no PR numbers, doctype names, field names or code.
5. When `develop` is verified, PR **`develop` → `main`**. That release PR runs
   the full gate (build + tests + the upgrade smoke from main) on the merged tree. Merging to
   `main` triggers the maintainers' deployment, so only maintainers merge release PRs.
   Merged while its last run is green and `main` has not moved since, `main` ends up with
   exactly the tree that run tested, so the push to `main` reuses that run's image instead of
   building and testing it again (minutes instead of ~20); otherwise it builds and tests as usual.
6. Nothing to tag by hand: once `main`'s tests and build pass (or it reused the release PR's
   tested image), the `release` job tags the merge commit `v<x.y.z>` (the version from step 4)
   and adds that tag to `main`'s image. A merge whose version is already tagged releases nothing — a hotfix
   bumps the patch version (`make bump-version`) to ship.

## Working in a git worktree

To work on several branches at once, give each its own worktree and its own stack:

```bash
git worktree add ../opengunstore-fix-xyz -b fix/xyz develop
cd ../opengunstore-fix-xyz
make dev-up && make dev-bench-clone   # clones the main checkout's frappe-bench, then creates the site and installs the apps
make dev-names                        # this checkout's Compose project (dev-<dir>) and containers
make test
make dev-down-volumes                 # when done: containers and the database volume go
```

- `make dev-bench-clone` copies the main checkout's `frappe-bench/` (seconds on macOS with APFS copy-on-write, a full copy elsewhere), so the main checkout needs `make dev-bench-init` first.
- A worktree stack publishes no ports. `make dev-up PORT_OFFSET=10` then `make dev-serve` serves it at `http://dev.localhost:8010`.
- Never run `docker compose` directly in a worktree: it attaches to the main checkout's project and recreates its containers on your tree. Check `make dev-names` before trusting a test run.

## Hotfix flow (emergencies only)

For production-down / urgent fixes that can't wait for the full `develop` cycle:

1. Branch off `main`: `git checkout -b hotfix/xyz main`
2. Bump the patch version on the branch (step 4 above), then PR `hotfix/xyz` → `main`
   (releases `v<x.y.z>`; deployment then follows the operator's process).
3. Back-merge `main` → `develop` so the fix isn't lost. If both changed the version,
   keep the higher one (normally develop's): the next release must carry a version
   that is not tagged yet, or its release PR fails.

## Notes

- **Local dev ≠ production.** Migration / fixture / custom-field changes should be
  tested against a **restored copy of real production data** before promoting —
  local test data won't surface data-shape or migration issues. See
  `docs/self-hosting.md`.
- Developer docs (architecture, dev stack, extensions, testing) are under `docs/developers/`,
  `docs/extensions/` and `docs/testing.md`.
- Integration / infra config (FastBound, RSR, Caddy) isn't exercised by local dev —
  review those changes carefully.
- **Secrets are never committed** — they live in Desk Settings (DB) or a git-ignored
  `.env`.
