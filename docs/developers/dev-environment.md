# Set up a dev environment

## Prerequisites

Docker (with Compose v2), git, GNU make and a host `python3`. Every entry point is a Makefile target (`make help` lists them). Do not run `docker compose` yourself: the targets pick the Compose project for this checkout, and a bare `docker compose` in a git worktree attaches to the main checkout's containers.

## First run

Run these once, in order, from the repository root:

| Step | Target | Result |
|---|---|---|
| 1 | `make dev-up` | MariaDB, two Redis containers and a `frappe/bench` container are running. |
| 2 | `make dev-bench-init` | `frappe-bench/` holds Frappe and ERPNext on the `version-16` branch head (`FRAPPE_BRANCH`). |
| 3 | `make dev-site-create` | Site `dev.localhost` with ERPNext, HRMS and Frappe CRM; `allow_tests` set. Administrator password `admin`. |
| 4 | `make dev-install-our-apps` | `ffl_core`, `ffl_integrations`, `ffl_woo_sync` linked into the bench and installed on the site. |
| 5 | `make dev-serve` | `bench start` in the foreground. |

Open <http://dev.localhost:8000> and sign in as `Administrator` / `admin`. A new site opens on ERPNext's setup wizard; `make test` completes the setup programmatically (`ffl_core.setup.test_bootstrap.run`) if you only need the test masters.

The dev bench follows the `version-16` branch head, not the tags production pins in `deploy/prod/frappe-refs.env` ([Pinned versions](../versions.md#local-dev-bench)).

## Day to day

| Target | Does |
|---|---|
| `make dev-up` / `make dev-down` | Start / stop the containers (volumes kept). |
| `make dev-logs` / `make dev-shell` | Tail the bench container / open a shell in it. |
| `make dev-names` | Show this checkout's Compose project, container names and ports. |
| `make dev-down-volumes` | Stop and wipe the database volume. |
| `make dev-site-drop CONFIRM=yes` | Drop the dev site and its database. |
| `make dev-reset` | Wipe everything, including `frappe-bench/`. |
| `make test` | JS unit tests, then all three apps in parallel (`WORKERS=6`). |
| `make test-serial` | The same suite one app after another on one site. |
| `make ci-upgrade-test` | Move the bench to the latest `version-16` head and re-run the suite (permanent for that bench). |
| `make api-docs` / `make api-docs-check` | Regenerate `docs/api/` / fail when it is stale (the CI gate; no bench needed). |

Test rules and traps: [Testing the core](../testing.md). Several checkouts side by side: see the worktree section of [CONTRIBUTING.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/develop/CONTRIBUTING.md#working-in-a-git-worktree).

## Rules the checks enforce

- A whitelisted endpoint has a docstring with a one-line summary, `Args:` and `Returns:`. The [API reference](../api/index.html) is generated from it; the permission line is derived from the code, so do not write role lists in a docstring.
- Commit through `ffl_core.utils.commit`, not `frappe.db.commit()`: it does nothing inside a test, so the test's rollback still works.
- Add the test before you change compliance logic (Form 4473, the bound book, validation hooks).
- Never change Frappe or ERPNext core.
