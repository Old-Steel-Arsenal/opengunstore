# Test discipline — `make test` must be same-site repeatable

## What the targets do

- `make test` — the JS unit tests (`make test-js`, the `node --test` command CI runs), then the
  three apps in parallel with `scripts/ci/parallel_tests.py`, the same splitter CI uses: the
  prepared dev site is cloned `WORKERS` times (default 6, `make test WORKERS=8`; a 7.7 GB Docker
  VM holds 6, each worker is a full bench process), modules are balanced by their last measured
  seconds (`frappe-bench/logs/test-timings.json`, kept between runs; the first run after a
  fresh checkout balances by test count and is slower), and a module that fails is re-run once
  alone — passing then is reported as `FLAKY` and does not fail the run. The clone sites are
  removed afterwards. Every worker's log is printed, then the `TOTAL` line (see below).
- `make test-serial` — the old behaviour: one app after another on the dev site, no JS. Use it
  to follow one failure in order, or when a parallel-only failure needs ruling out.
- Both run the `allow_tests` check and the `test_bootstrap` step first, on the dev site, so
  the clones start from identical masters. Both need `make dev-site-create` once.

The suite must pass **twice back-to-back on the same site, both all-green** (CI and local alike).
`make test` cannot show this: most modules run on clone sites rebuilt from the base site and dropped
after every run, so a module that commits inside the test transaction stays green there. Check
same-site repeatability with `make test-serial` twice (before a PR that adds or reworks tests).

## The trap: commits inside the test transaction poison the next run

`frappe.tests.IntegrationTestCase` (what `FrappeTestCase` resolves to, frappe 16.x) rolls the whole
class back **once, at `tearDownClass`**, and `frappe.db.rollback()` rewinds only to the **last
commit**. So any `frappe.db.commit()` executed while a test class runs moves that rollback floor
forward and **permanently persists every row created before it**. The next run then collides:
`SerialNoDuplicate`, `ValidationError: … already on file`, or a serial that persisted while its stock
ledger rolled back → `NegativeStockError`. (Frappe's own `_commit_watcher` warns "Transaction
committed during tests" when `SHOW_TRANSACTION_COMMIT_WARNINGS` is on.)

## When `make test` is lying to you

Four ways a run looks green and means nothing. The Makefile now closes the first
three; the fourth is on you.

1. **No `allow_tests` on the site.** `frappe/commands/testing.py` prints "Testing is
   disabled for the site!", returns, and **exits 0** — zero tests, a green tick.
   `make test` now asserts the key before anything else and exits 1 with the fix;
   `make dev-site-create` sets it on both the create and the already-exists path.
2. **A site you only thought was fresh.** `dev-site-create` never recreates an
   existing site — it now says so loudly. Truly fresh means `make dev-site-drop CONFIRM=yes` first (it refuses without the flag — in the main tree it would be the main dev DB).
3. **`| tail` eating the score.** Each app prints TWO `Ran N` lines (one per category
   group), six in a full run, and the real figure is their **sum**. `make test` writes
   the whole run to `frappe-bench/logs/make-test.log` and ends with one
   `TOTAL ran=… failures=… errors=…` line. Trust that, not the last block on screen.
   Both targets (`make test`, `make test-serial`) print it and exit non-zero on any failure.
4. **Running it once.** Same-site repeatability is the point of this document: run
   `make test-serial` **twice** and compare the TOTALs. A second pass that runs FEWER tests than the
   first has a `setUpClass` dying, which shows up as a smaller failure count, not a
   bigger one.

## Upgrade watch list (check these on `make ci-upgrade-test`)

* `ffl_core/workspace_shortcuts.py::_save_workspace` sets the GLOBAL
  `frappe.flags.in_patch` around `ws.save()` so a settings-driven workspace edit is
  not exported back into `gun_shop.json`. It rests on an implicit frappe contract:
  the flag is read by `workspace.py::disable_saving_as_public`, whose only caller is
  `Workspace.on_update`, and everything that early return skips is export / rename
  cleanup — `Workspace.clear_cache` is NOT behind it. Should an upgrade move cache or
  sidebar work into `on_update` after that guard, our flip silently starts skipping it
  too. **The failure is silent**; the tells are the shipped `gun_shop.json` coming back
  modified after an FFL Settings save, or its `modified` timestamp being bumped.
* `ffl_core/tests/test_dealer_onboarding.py::_forget_resolved_outgoing_account` writes
  `frappe.local.outgoing_email_account`, the private memo of frappe's
  `cache_email_account` decorator (`frappe/email/doctype/email_account/email_account.py`).
  Name and dict shape are both internal. If an upgrade renames either, disabling the
  Email Account in the DB stops reaching `find_outgoing` again and the two Mode-A
  tests go back to asserting against a stale account on the second same-site run.

* `ffl_core/setup/pos_compat.py::ensure_cashier_pos_profile_select` (ERPNext 16.50 bump)
  gives every `roles.COUNTER_ROLES` role that lacks it `select` on POS Profile through
  **Custom DocPerm**, once per role (recorded in DefaultValue `ffl_pos_profile_select_granted`,
  so a rule an administrator deletes stays deleted). A doctype's Custom DocPerm rows replace
  its shipped DocPerm rows wholesale: the first one written froze POS Profile's 16.50 rows on
  the site, so later ERPNext rule changes stop reaching it. `ensure_role_grants` does the same
  to the `ROLE_GRANTS` doctypes it writes (Serial and Batch Bundle for the trade-in; POS
  Invoice / FFL Acquisition where needed) and to `REGISTER_GRANTS` (what a login holding
  only Sales User or only Sales Manager needs to run the register end to end; recorded
  separately in DefaultValue `ffl_register_grants_done`; `test_counter_register_perms` walks
  the whole loop as such users). `check_perm_drift` (after_migrate) compares, for
  the doctypes in `_DRIFT_DOCTYPES`, the two tables as multisets
  (our own grants left out) and raises one Error Log +
  System Manager ToDo per distinct difference ("<DocType> permissions differ from ERPNext
  (<digest>)"; digests kept in DefaultValue `ffl_pos_profile_perm_drift_seen`, so an Error
  Log purge does not re-alert; a newer difference closes the older ToDo). It does not
  re-sync, because nothing tells an upstream change from a rule someone set on purpose.
  On each bump still `git diff <old> <new>` those doctypes' JSON (pos_profile.json first)
  and check whether ERPNext ships its own mirror patch (16.50 did:
  `mirror_select_perms_to_custom_docperm`). Error Log is MyISAM: a test that makes the
  alert fire leaves its log behind unless it deletes it (the perm tests do).
* Frappe 16.50 made `Workspace.validate` refuse content edits to a *standard*
  workspace outside developer mode ("saved as a customization"), and Gun Shop /
  Settings migrate in as `standard=1`. `_save_workspace` (first item above) gets
  past it only because its `in_patch` flip satisfies `disable_saving_as_public()`:
  that flag is now load-bearing for the save itself, not just for the export.
* Frappe 16.50 also turned `Workspace Sidebar` into an archive nothing renders (the desk
  reads `Sidebar`; `convert_sidebars` converted existing sites once), and a module without
  a `Sidebar` gets one computed from its contents (doctype lists, no way back to Gun Shop).
  ffl_core and ffl_integrations now ship `<module>/sidebar/gun_shop` and `.../settings`
  (exported in developer mode, imported on install/migrate, force-re-imported by
  `setup/sidebar.py` when a converted copy is in force), and `mirror_gun_shop_sidebar`
  writes an extension module's `Sidebar` (install/migrate only: frappe refuses Sidebar
  writes elsewhere). When a bump touches `frappe/desk/doctype/sidebar`, re-check the
  nav entry points in a browser on a fresh site AND a migrated one (`bench serve` follows
  `default_site` whatever the Host header says: serve each site on its own port).

## Rules

1. **No real commit inside the test transaction.**
   - Our code commits through **`ffl_core.utils.commit()`** — a no-op when `frappe.flags.in_test` is
     set, a real commit under `bench execute` / `bench migrate` (bootstrap and prod unchanged).
   - Third-party / ERPNext commits reached by code-under-test: neutralize per-test with
     `patch("frappe.db.commit")`.
   - Batch/bulk APIs (e.g. the FastBound importer) take an explicit `commit_each` flag so the caller
     owns the transaction boundary; tests pass `commit_each=0`.

2. **Test files must not commit either.** A `frappe.db.commit()` in `setUp`/`setUpClass` (a common
   "purge last run's leftovers" pattern) commits the *previous* test's pending rows — a hidden
   self-poison. Use unique per-test names and let the class rollback clean up; don't persist cleanup.
   Exception: a few commits are *load-bearing and intentional* — e.g. `test_edit_order.py:366`, where a
   fixture must survive an in-test rollback the code-under-test performs. Those must carry a comment
   saying so; **read the comment before deleting a commit from a test file.**

3. **Shared masters are seeded once by `test_bootstrap`, out of band.** Company / chart of accounts /
   warehouses / POS Profile / custom fields / FastBound TEST settings / the reconcile masters
   (`Brand "Colt"`, `Caliber "45ACP"` — note `Serial No.manufacturer` is a Link to **Brand**; its
   field *label* just reads "Manufacturer") are created idempotently by
   `bench execute ffl_core.setup.test_bootstrap.run`. `make test` runs this once before the run-tests
   loop. Most `setUpClass` **assume** they exist and must not create-then-commit them; the reconcile
   suite additionally self-vivifies its own Brand/Caliber **in-transaction (no commit)**, so it never
   depends on cross-process bootstrap visibility.

## Stub discipline (added after the RSR P3a/P4b rounds — each rule cost a red run)

4. **Never stub a frappe primitive with a bare `return_value`.** Answer only the
   doctype / field / hook under test and let everything else through. These primitives
   are called by *core itself* underneath the code you are testing, so a blanket stub
   reaches places you never intended:

   | Symptom | Actual cause |
   |---|---|
   | `TypeError: 'NoneType' object is not iterable` in `frappe.sqlite_search` | a `get_hooks` wrapper that re-declared `default=None`, destroying frappe's sentinel so every UNREGISTERED hook returned None instead of `[]` |
   | `DoesNotExistError: DocType Error Log not found` | unconditional `get_value` answering core's `get_meta("Error Log")` inside `frappe.log_error` |
   | "RuntimeError never caught" (it *was* caught) | unconditional `get_all` making the except handler itself raise |
   | local-stock assertions collapsing to 0 | unconditional `get_value` feeding a truthy row to a newly-registered `distributor_stock_source` probe |

   The three shapes that recur: **positional scripts** (`side_effect=[a, b, c]` answers
   by call ORDER, so one extra read shifts every later answer), **blanket truthy
   strings** (`return_value="X"` also satisfies an unrelated string column), and
   **last-call-wins captures** (`captured = fields` records whichever call ran last).

5. **A stub that CRASHES the code under test can make a test pass for the wrong
   reason.** `test_releases_reserved_serial_and_cancels_order` asserted "exactly one
   write to Woo Online Order" and was green for months — because its blanket
   `get_all` handed Serial No rows to a *Distributor Order* query, the hook died on
   the missing key, and the webhook's `except` swallowed it, so a second write that
   production ALWAYS made never ran. Making the hook more tolerant "broke" the test.
   If a test's expectation depends on something failing, say so explicitly, or it will
   be read as a regression later.

6. **Never stop a group of patchers in one cleanup.** Register each with
   `self.addCleanup(p.stop)` — unittest unwinds cleanups **LIFO**, which is the only
   correct order when patches overlap. Two patchers can target the same attribute
   without looking like it: modules that both `import frappe` share ONE module object,
   so `patch.object(a.frappe, "get_doc")` and `patch.object(b.frappe, "get_doc")` are
   the same target. Stop them FIFO and the second patcher restores the FIRST one's
   **stub** as the "original" — a process-wide leak. That cost 87 cascade errors
   across three apps, none of them near the culprit, because the victim is always a
   LATER module. A "pure" test is process-global under `bench run-tests`: it must
   restore on EVERY path.

7. **Guard what the stubbed layer structurally cannot see.** A stubbed `frappe.db`
   never validates a column name, never runs a site test, and never notices a leaked
   patch. Cheap guards that catch those classes locally instead of on the bench:
   - **doctype column contract** — AST-read the field list a function passes to
     `get_value("<DocType>", …)` and check it against the doctype JSON. (`license` vs
     the real `ffl_number` on ATF FFL Record was a bench-only `OperationalError 1054`.)
   - **site-test helper contract** — import each site-only module and assert every
     `self._helper` it references resolves on the class. (`_pos_profile` lived in
     another module's fixture class: 10 identical bench errors, zero local signal.)
   - **patch-leak detector** — fingerprint the frappe primitives before the suite and
     report any not restored afterwards. Compare `__func__`, not identity: `getattr`
     on an instance rebuilds bound methods, so a naive `is` check flags every
     `frappe.db.*` as leaked.

   Prove each guard by **re-introducing the real defect** and watching it fire. A guard
   that has never failed is a claim, not a check.

   **Where each one lives** (a rule you can't locate isn't executable):
   - the **doctype column contract** is in the repo and runs under `make test` —
     `ffl_core/tests/test_counter_doctype_contracts.py` (currently ATF FFL Record plus
     the three doctypes the endpoint reads). Extend it when new code reads a new
     doctype's field list.
   - it covers **doctype** tables only, and the gap is not theoretical: hand-written
     SQL against a FRAMEWORK table has no doctype JSON to check against. An
     `ORDER BY name` on `tabSingles` — which has three columns and no key — passed
     230 offline tests and three reviews, then took out `bench migrate` and 715
     tests on the first real site (2026-08-26). For those, the guard has to be a site test:
     `ffl_core/tests/test_single_reads.py` drives the `ffl_core.utils` Singles
     readers against the real table and pins its schema. Same pattern if we ever
     hand-write SQL against another framework table.
   - the **site-test helper contract** and the **patch-leak detector** live in the
     role's stubbed harness (`p5-harness/harness.py` in this run's directory,
     `_check_site_test_contracts` / `_report_patch_leaks`) — and they belong there:
     both are assertions *about the stubbed environment itself*, so they have nothing
     to check under `make test`, where frappe is real and site tests actually run.
     Carry them forward when porting the harness to the next slice.

## Task #10 — RESOLVED (fresh-site reconcile "Could not find Manufacturer: Colt")

Root cause (fixed 2026-07-11): the 15 `test_fastbound_reconcile` failures insert `Serial No` rows with
`manufacturer="Colt"`. That custom field is **labelled "Manufacturer" but is a Link to the `Brand`
doctype** (`custom_field "Serial No-manufacturer"`, `options=Brand`) — so the insert needs
**Brand "Colt"**, which no setup step seeded. Frappe's `LinkValidationError` uses the field *label*, so
it read "Could not find **Manufacturer**: Colt" and every prior investigation (this doc included)
chased a *Manufacturer*-doctype / transaction-visibility ghost. The masters were in fact committed and
visible — verified on a fresh `iso.localhost`: `Manufacturer "Colt"` **present**, `Brand "Colt"`
**ABSENT**, `Caliber "45ACP"` **present** — the bootstrap simply seeded the **wrong doctype**. Warm dev
sites passed only because real intake had accumulated a `Brand "Colt"`.

Fix: `test_bootstrap._fastbound_reconcile_test_masters` seeds `Brand "Colt"` (not `Manufacturer`);
`test_fastbound_reconcile._serial` self-vivifies Brand/Caliber in-transaction. `_app-tests.yml` now
runs **`ffl_integrations` with no `|| true`** (all three apps are real gates). Verified: fresh
`iso.localhost`, `make test` green **twice** (incl. the first, wizard-firing run). Two downstream
failures seen on `main` (`test_payroc`, `test_fastbound_pos_4473` Destination-FFL) were collateral of
the 15 aborting inserts and cleared once those were fixed.

## 8. A gate counts as verified only when it was exercised through the real call path (2026-07-23, P5b integration)

The `Distributor Console` role gate was once judged "not working": a proxy user calling the
endpoint from outside the site with an API key got a **403**, so the conclusion was "the role did
not attach / the seed was rolled back / the role name is wrong". All three were wrong. The truth:

- the role **was attached** (visible by querying the `Has Role` table), and `only_for` **passed**;
- the 403 came from the `frappe.get_doc` after it, and **the order number did not exist**;
- when the caller has no permission on a doctype, Frappe's `handle_does_not_exist_error`
  **deliberately disguises `DoesNotExistError` as `PermissionError`**, so it does not leak whether
  a record exists.

**Cost**: we nearly "fixed" the misdiagnosis by granting that role read permission on the doctype.
That would have let a key **stored inside WordPress** enumerate orders and customers directly,
widening the blast radius for a bug that did not exist.

### Rules

1. **Verify a gate through the real call path**: HTTP from outside the site with real credentials.
   `frappe.set_user()` + `only_for()` only proves "the role matches"; it does not prove that a
   request made with this key can run the whole chain.
2. **"The role record exists" is not "it takes effect through the API once granted".** A stub
   harness cannot send HTTP, so it **structurally** cannot verify this; all it can verify is that
   the record is created and that the seed is registered on `after_migrate`. The difference must be
   covered by a site test or a manual re-check, and the report must say which one was done.
3. **When diagnosing a 403, first tell its two meanings apart**: "you have no permission" and
   "this record does not exist" look like the same 403 in Frappe. The test is whether the traceback
   lands in `only_for` or in the read after it; if it lands in the read, first check **whether the
   record exists**, and only then suspect permissions.
4. **Once past the gate, stop hiding existence**: the caller is already authorized, so continuing to
   hide it buys no security and only makes a mistyped order number look like "your role is broken".
   The endpoint should check existence explicitly **after** the gate and report a clear error. The
   order must not be reversed, or it becomes an enumeration oracle.
5. **Do not "fix" a permission failure you have not reproduced by widening permissions.** Reproduce
   first, then locate, and only then change; "give it more permissions" is usually the most
   expensive fix.

### Four pitfalls in dev-site fixtures (same batch)

- When `bench console` reads code from stdin, **a blank line inside a function body ends the
  block**: the script is cut off in the middle and the second half runs as top-level statements, so
  "the log says it was seeded, but the database has nothing". Deleting the blank lines is worse
  (the `def` never terminates). The reliable fix: **write the script into the container and run the
  whole file with `exec(compile(...))`**.
- Seed scripts should **commit in stages**. With a single commit at the end, any exception midway
  rolls back everything before it.
- ERPNext **validates barcodes**: a made-up UPC raises `InvalidBarcode`; generate it with a proper
  UPC-A check digit.
- `frappe.get_all` **silently returns only 20 rows when no limit is given**; a catalog-wide job
  that hits this "succeeds" while seeing only 20 rows.

### A new doctype is a "trio"; missing one only blows up at migrate (2026-07-24)

A doctype directory must contain **JSON + `__init__.py` + controller `<name>.py`** together. On
`bench migrate`, Frappe runs `import <name>.<name>` for **every** doctype, so a doctype that has
only the JSON and no controller **passes every unit test** and then dies hard at migrate with
`No module named ...`. The child table `Distributor Listing Cost Range` did exactly this and
blocked a gate at migrate.

- A child table (`istable:1`) needs a controller too; its content is the five-line
  `class X(Document): pass`.
- The stub harness **does not run migrate**, so it structurally cannot see the missing file. A
  **filesystem guard** covers it instead (see `test_doctype_three_file_contract.py`: it scans every
  doctype directory and goes red when a controller or `__init__` is missing). It was verified
  against a real defect (remove the controller: red; put it back: green).
- Warning: do not casually add a heuristic such as "the class name must be the PascalCase of the
  directory name". The casing of existing doctypes such as `FastBoundSettings` and
  `RSRCatalogItem` does not match naive PascalCase, yet they all migrate fine; that heuristic would
  only cause false positives. The criterion that actually works is "the controller module exists
  and can be imported".

### A stub that misses get_cached_doc / get_doc lets the bench resolve the controller back to Core (2026-07-24)

`api.quote` goes through `frappe.get_cached_doc("Distributor")`, and
`carry_forward_amended_orders` goes through `frappe.get_doc("Distributor Order").add_comment`. If a
test mocks only `get_doc` and misses `get_cached_doc` (or some path never mocks the doc fetch at
all), the bench **really loads the controller**; and the same test's `get_value` mock often answers
`get_value("DocType", X, "module")` with `None` as well, so Frappe concludes that the doctype
belongs to **Core** and tries to import `frappe.core.doctype.<x>` -> `ModuleNotFoundError`.

- **It only shows on a real bench**: the stub harness's `get_doc` / `get_cached_doc` never load a
  controller, so a missed mock like this is green in the harness and blows up in `make test` / CI.
  Two earlier tests, one on `get_cached_doc` and one on `add_comment`, both behaved this way, until a gate ran
  them alone on a bench.
- **Fix**: whenever the code under test fetches the **document object** of a doctype (get_doc /
  get_cached_doc / get_last_doc ...), the test must mock **all** of those entry points to a fake,
  not just one of them; or make the `get_value` mock let `dt == "DocType"` through with a real
  value instead of answering `None` for everything.
- **Double-patch race**: when the same attribute is patched once by `_run` and once by a case, the
  later registration wins. A case that needs to see the side effect should **pass its fake into**
  `_run` instead of patching it again itself (see the audit-comment in test_g1).

### after_migrate seeding does not cover a fresh install; hang both hooks (2026-07-24)

`ensure_console_role` shipped hooked only on `after_migrate`. Existing dev/prod sites go through
migrate, so they got the role, but **a fresh `bench new-site` + `install-app` does not run
migrate**, so the role was never seeded. A store installed fresh therefore lacked the
`Distributor Console` role, and any "assign this role to a user" raised `LinkValidationError`. This
was a real deployment-surface defect planted earlier and forced out by the fresh-site CI (the
role site test).

- **Upgrade path != install path**: verifying "seeding succeeded" on dev.localhost (an existing
  site) only covers migrate, not a fresh install. To prove the deployment surface is complete you
  must run the fresh-site path (which is what CI does).
- **Fix at the root**: hang the idempotent seed function on **both `after_install` and
  `after_migrate`**. The repo's existing `rsr_seed.run` / `ensure_mode_of_payment` are already
  hooked both ways; when copying that pattern, do not copy only half of it (this one originally
  missed `after_install`).
- **Have the registration test assert that both hooks are present**, and **read the hooks.py
  source** rather than `frappe.get_hooks()`: the stub harness's `get_hooks` does not read the real
  file, so trusting it can go falsely green when the file is actually wrong (exactly the kind of
  hole this closes).
