# Pinned versions

> The upgrade-safety policy requires every dependency to be pinned to a specific version. This file records the versions currently used in production / staging / dev; update it on every upgrade.
>
> This repository starts from a cleaned snapshot; the product version restarts at 0.1.0 (see [Version line](#version-line)).

## Version line

- **0.1.0 is the first release of this repository and continues the private 1.x line**, whose last release was 1.15.1: same features, same data. A store on 1.15.1 moves to 0.1.0 as an ordinary upgrade; a patch resets the What's New marker so the 0.1.0 note shows once. After 0.1.0, plain semver: the release job tags a version only when it is above every tag in this repository.
- **The 1.x tags (v1.0.0-beta.1 … v1.15.1) live in a private archive repository**, not here. An emergency fix for a store still on 1.15.1 is v1.15.2, made in the archive.
- **Deploy routing** (in the operator's deploy repository): a tag `v1.*` comes from the archive, any other tag from this repository. First-party extensions declare `requires_core` `>=0.1.0,<2` for the transition, which holds on both lines; once every store runs 0.x their ranges narrow to `<1`.
- **Container images:** both lines push to the same image package, and `latest` follows whichever line released last. Deploys pin a version tag, never `latest`.
- **Before this repository tags v1.0.0, the archive route must be retired.** From v1.6.0 on the archive's image tags are in the same package, so a public v1.x tag would collide with them (the release job refuses to move an existing version tag), and the deploy route would send any `v1.*` tag to the archive. The 1.0 release PR retires both.

## Core stack

| Component | Version | Source | Upgrade cadence |
|---|---|---|---|
| ERPNext | `v16.51.1` (`deploy/prod/frappe-refs.env` is the single source: the image build and the CI bench reuse it; the Dockerfile ARG defaults are pinned in line by the `scripts/ci` tests) | https://github.com/frappe/erpnext | Rolls with v16 stable; the daily `ci.yml` runs a regression against the version-16 head |
| Frappe Framework | `v16.52.0` (`FRAPPE_REF`, same as above) | https://github.com/frappe/frappe | Same as above |
| ~~POS Awesome~~ | **Removed** | Upstream stopped updating in 2024-02; no longer used | - |
| **POS UI** | ERPNext built-in `/app/point-of-sale` | Moves with ERPNext | No extra upgrade risk |
| frappe_docker | main (upgrade by re-cloning) | https://github.com/frappe/frappe_docker | Follows main |
| MariaDB | 10.6 | docker image | Follows the ERPNext recommendation |
| Redis | 6 | docker image | Follows the ERPNext recommendation |
| Python (in the container) | 3.14 | Shipped with frappe_docker (frappe v16 `requires-python >=3.14`) | - |
| Node (in the container) | 24 | Shipped with frappe_docker (frappe v16 `engines.node >=24`) | - |

## Local dev bench

`make dev-bench-init` initializes from the **head of the `version-16` branch** (`FRAPPE_BRANCH`, default `version-16`), which is usually newer than the tags production runs; afterwards frappe/erpnext inside the bench are plain git checkouts and do not follow upstream automatically. The versions production runs are pinned in `deploy/prod/frappe-refs.env` (`FRAPPE_REF` and `ERPNEXT_REF`, which can differ).

- `make dev-upgrade` moves the bench to the **latest `version-16` head** (fetch, checkout, pull, reinstall the venv, migrate; it works the same way inside a worktree). It does **not** align the bench with the versions pinned in `deploy/prod/frappe-refs.env`.
- `make ci-upgrade-test` = `dev-upgrade` + `test`, so it **permanently** upgrades and migrates the dev bench and its site to the version-16 head; it does not restore the previous versions afterwards. Use a throwaway worktree bench (`make dev-bench-clone`) for it, and `make dev-down-volumes` plus deleting the worktree to discard it. CI runs it against a throwaway bench.

## Custom app compatibility

| App | Compatible ERPNext | Compatible Frappe |
|---|---|---|
| ffl_core | v16 | v16 |
| ffl_integrations | v16 | v16 |
| ffl_woo_sync | v16 | v16 |

## External API versions

| API | Version | Purpose |
|---|---|---|
| WooCommerce REST | v3 | Product / order sync |
| FastBound | TBD | A&D / 4473 / NICS |
| Payroc | TBD | Card payments |
| ATF EZ Check | TBD | FFL verification |

## Upgrade log

| Date | Component | From -> To | Decided by | Notes |
|---|---|---|---|---|
| 2026-05-04 | Project initialization | - -> v15 stable | - | Starting point |
| 2026-05-04 | POS UI choice | POS Awesome -> ERPNext built-in POS | - | Upstream stopped updating + the upgrade-safety policy |
| 2026-05-04 | Upgrade to v16 stable | v15 -> v16 | - | Owner decision: follow the latest release |
| 2026-09-08 | Frappe + ERPNext | v16.24.0 / v16.25.0 -> v16.33.1 / v16.34.2 | Owner decision | Local `make ci-upgrade-test` (which now upgrades Frappe as well) 6689/0; the 9 cases that were red in the daily regression were ffl_woo_sync tests whose global get_value stub intercepted new framework-internal queries; the stub now answers only application doctypes |
| 2026-10-06 | Frappe + ERPNext | v16.33.1 / v16.34.2 -> v16.50.0 / v16.50.0 | Owner decision | On the old develop, local `make test` 7256/0/1 (two passes); on develop with v16.50 unchanged there were 13 errors, 12 of them the POS Profile select-or-read gate added in 16.50 (`get_stock_availability` is called in-process when a POS Invoice is submitted), which Sales User cannot pass. Fix: `pos_compat.ensure_cashier_pos_profile_select` adds only `select` (Custom DocPerm, granted once per role, without widening owner-only rules) to the roles in `COUNTER_ROLES` that lack it, and `check_pos_profile_perm_drift` reports differences between the frozen rows and ERPNext's native rows (with persistent de-duplication). Frappe 16.50 moved navigation into `Sidebar` records: ffl_core / ffl_integrations now ship the Gun Shop / Settings Sidebars with the app, and extension modules write Sidebars through `mirror_gun_shop_sidebar`. CI: `bench init --python` now receives setup-python's absolute path (uv had been picking a managed CPython 3.12); upgrade-from-base now builds the old site in a throwaway bench with the base refs first, then switches to the new refs. Final local result 7280/0/0 (the migrated site twice, plus one site created fresh by the CI procedure) |
| 2026-10-17 | Frappe + ERPNext | v16.50.0 / v16.50.0 -> v16.52.0 / v16.51.1 | Owner decision | A worktree bench built on v16.50.0, switched to the new tags and migrated cleanly. The first `make test` on it had 1 failure and 154 errors, all from one cause: ERPNext 16.51 moved Default Warehouse from Stock Settings to Company (patch `move_warehouse_defaults_to_company` carries the value over), and `warehouses.default_stock_warehouse` read the old field. It now reads `Company.default_warehouse`. Two test-isolation problems the nightly run was also hitting were fixed: `test_check_money_order` now clears a Check/Money Order row another fixture pointed at Cash, and `tests/pos_opening.todays_pos_opening` opens a shift on the profile it is asked for. The `NegativeTradeInTicket` override still matches `is_negative_grand_total_allowed` in accounts_controller. Upstream's POS Closing change-amount fix does not touch the cash drawer, which computes its own expected cash. Final local result 8839/0/0 plus JS 464/0 |
