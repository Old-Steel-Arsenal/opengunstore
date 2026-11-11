# Testing the core

## Targets

- `make test`: the JS unit tests (`make test-js`), then the three apps in parallel with `scripts/ci/parallel_tests.py`, the splitter CI uses. The dev site is cloned `WORKERS` times (default 6; a 7.7 GB Docker VM holds 6), modules are balanced by their last measured time (`frappe-bench/logs/test-timings.json`), and a failed module is re-run once alone: passing then is reported `FLAKY` and does not fail the run. Clone sites are dropped afterwards.
- `make test-serial`: one app after another on the dev site, no JS. Use it to follow one failure in order.
- Both first check `allow_tests` and run `ffl_core.setup.test_bootstrap.run` on the dev site, and both end with one `TOTAL ran=… failures=… errors=…` line (the whole run is in `frappe-bench/logs/make-test.log`). Trust that line: each app prints two `Ran N` blocks and the real count is their sum.

## The repeatability rule

The suite must pass **twice back-to-back on the same site**. `make test` cannot show this (its clone sites are rebuilt every run), so run `make test-serial` twice before a pull request that adds or reworks tests, and compare the TOTALs. A second pass with fewer tests has a `setUpClass` dying.

Why: `IntegrationTestCase` (what `FrappeTestCase` resolves to in Frappe 16) rolls a class back once, at `tearDownClass`, and `frappe.db.rollback()` only rewinds to the last commit. Any commit during a test class persists every row before it, and the next run collides (`SerialNoDuplicate`, "already on file", `NegativeStockError`).

## A green run that means nothing

- No `allow_tests` on the site: Frappe runs zero tests and exits 0. `make test` refuses such a site; `make dev-site-create` sets the key.
- A site you thought was fresh: `dev-site-create` never recreates one. Fresh means `make dev-site-drop CONFIRM=yes` first.
- Counting the last block on screen instead of the `TOTAL` line.
- Running it once.

## Rules

1. **No real commit inside a test.** Our code commits through `ffl_core.utils.commit()`, a no-op under `frappe.flags.in_test`. Patch `frappe.db.commit` for third-party code the test reaches. Bulk APIs take a `commit_each` flag; tests pass `0`.
2. **Test files do not commit either**, including in `setUp` / `setUpClass`. Use unique names and let the class rollback clean up. The few intentional commits carry a comment saying why; read it before removing one.
3. **Shared masters come from `test_bootstrap`**, seeded idempotently and out of band (Company, chart of accounts, warehouses, POS Profile, custom fields, FastBound test settings, the reconcile masters). `setUpClass` assumes them. `Serial No.manufacturer` is a Link to **Brand** although its label says Manufacturer.
4. **Never stub a Frappe primitive with a bare `return_value`.** `get_hooks`, `get_value`, `get_all` and friends are called by Frappe itself under your code; answer only the doctype / field / hook under test and pass everything else through. Avoid positional `side_effect` lists, blanket truthy strings and last-call-wins captures.
5. **If a test passes because something fails, say so** in the test. A stub that crashes the code under test can hide a real write.
6. **One `addCleanup(p.stop)` per patcher**, never one cleanup stopping a group: cleanups unwind LIFO, which is the only safe order when patches overlap (two modules that `import frappe` share one object).
7. **Guard what a stub cannot see**, and prove each guard by re-introducing the defect: the doctype column contract (`ffl_core/tests/test_counter_doctype_contracts.py`), Singles reads against the real table (`ffl_core/tests/test_single_reads.py`), the doctype three-file contract (`test_doctype_three_file_contract.py`).
8. **Mock every document fetch** the code under test uses (`get_doc`, `get_cached_doc`, `get_last_doc`), or let `get_value("DocType", …)` through: otherwise the bench loads the real controller, the module lookup answers `None`, and Frappe looks for the DocType in Core.
9. **A permission gate is verified only through the real call path** (HTTP with real credentials). A 403 can mean "no permission" or "record does not exist" (Frappe disguises the second); check which before widening any permission. After the gate, report a missing record explicitly.
10. **A new DocType is three files**: JSON, `__init__.py` and the controller (child tables too). Missing one passes every unit test and fails at `bench migrate`.
11. **Seed on both `after_install` and `after_migrate`.** A fresh `new-site` + `install-app` runs no migrate. Assert both hooks by reading `hooks.py`, not `frappe.get_hooks()`.
12. **Dev fixtures:** `bench console` from stdin ends a function at a blank line (run a file with `exec(compile(...))`); commit seed scripts in stages; generate UPCs with a valid check digit; `frappe.get_all` without `limit` returns 20 rows.

## Upgrade watch list

Check these on every `make ci-upgrade-test`; each relies on Frappe or ERPNext internals and fails silently.

| Point | Relies on | Tell |
|---|---|---|
| `ffl_core/workspace_shortcuts.py::_save_workspace` sets `frappe.flags.in_patch` around `ws.save()` | `Workspace.on_update` → `disable_saving_as_public` skipping only export / rename work; since Frappe 16.50 also what lets a standard workspace be saved outside developer mode | `gun_shop.json` comes back modified, or its `modified` bumps, after an FFL Settings save |
| `ffl_core/tests/test_dealer_onboarding.py::_forget_resolved_outgoing_account` writes `frappe.local.outgoing_email_account` | the private memo of `cache_email_account` | the two Mode-A tests fail on the second same-site run |
| `ffl_core/setup/pos_compat.py`: `ensure_cashier_pos_profile_select`, `ensure_role_grants`, `REGISTER_GRANTS` write Custom DocPerm rows | Custom DocPerm replaces a doctype's shipped DocPerm rows wholesale, freezing them | `check_perm_drift` (after_migrate) raises an Error Log + ToDo per difference; still `git diff` the permissions of POS Profile, POS Invoice, POS Opening / Closing Entry, Sales Invoice and Item Price, and run `test_counter_register_perms` |
| `ffl_core/api/pos_serial.py` overrides `erpnext.stock.doctype.serial_no.serial_no.get_pos_reserved_serial_nos` (a copy of the upstream query plus a "not consolidated" filter) | `pos_controller.js::check_serial_no_availablilty` calling that exact method path, and the upstream query staying as copied | `test_pos_serial` fails, or a gun sold at the register and bought back is refused with "has already been transacted into another POS Invoice"; diff the upstream function against the copy |
| Sidebars: ffl_core / ffl_integrations ship `Sidebar` records; `mirror_gun_shop_sidebar` writes extension modules' | Frappe 16.50's `Sidebar` (the old `Workspace Sidebar` is an archive) | when a bump touches `frappe/desk/doctype/sidebar`, check navigation in a browser on a fresh and a migrated site (serve each on its own port) |
