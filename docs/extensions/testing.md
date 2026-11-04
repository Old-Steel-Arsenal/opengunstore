# Testing and CI

## What to test

`tests/` with `FrappeTestCase`, for every endpoint and hook:

- **Switched off:** every endpoint raises `frappe.PermissionError`, every hook does nothing. Flip the row with `frappe.db.set_value("POS Extension", APP, "enabled", 0)` and `ffl_core.extensions.clear_enabled_cache()`; call `ffl_core.extensions.registry.sync()` in `setUpClass` so the row exists.
- **No role:** a user holding none of the endpoint's roles is refused.
- **Repeat call:** a second call to a data-changing endpoint does not write twice.
- **Install / uninstall:** `after_install` and `before_uninstall` are idempotent and leave no orphan Custom Field or Role.
- **Isolation:** no network, no real credentials, no dependence on one store's data. Do not commit inside a test ([Testing the core](../testing.md#rules)).
- **JavaScript:** `tests/js/*.test.js` with `node --test`; registry `condition` functions are plain functions.
- **Budget:** hooks under 50 ms at p95 (excluding enqueue); `condition` functions synchronous and O(1).
- **Compatibility:** run the suite against the lower bound of `requires_core` and the current core.

`deploy/prod/tests/ext_hello/ext_hello/tests/test_greet.py` has the first three.

## `make ext-test`

From the core checkout, with the dev stack up:

```bash
make ext-test ID=<id> DIR=<extension folder>
```

It unpacks a copy of the folder and checks it before it replaces `frappe-bench/apps-ext/<id>` (a refused folder changes nothing), wires it the way a store's replay does (an `apps/<id>` symlink, a `.pth`, a line in `sites/apps.txt`), installs it on `dev.localhost` (or runs `bench migrate` when it is installed already, so changed DocTypes, fields, fixtures and patches land), then runs `scripts/ci/ext_test.sh`:

1. `bench --site <site> run-tests --app <id>` (a run that ran no test fails)
2. `python -m ffl_core.extensions.lint <root>`
3. `scripts/generate_api_docs.py --app <root> --check`

All three run even after a failure. The last line is `ext-test <id>: tests … · lint … · docs …`.

| Exit | Meaning |
|---|---|
| 0 | All three passed. |
| 1 | Wiring, install or migrate failed; or the tests, the lint, or the API docs failed: an endpoint without its docstring, `Args` or `Returns`, a parity failure, or a `Permissions` section whose role list contradicts a gate built from your own roles (`CONFORMANCE FAIL` / `PARITY FAIL` in the log). |
| 2 | The folder was refused before anything ran: not an extension id or a core app's id, no `<id>/hooks.py`, a `wheels/` folder (bundled wheels are not installed by this script yet), or an `apps/<id>` the script did not make. |
| 3 | Only the docs drifted from this core (`docs STALE`): a stale `docs/api/`, or a hand-written `Permissions` role list that differs from what a gate resolved through the core derives (`PERMISSIONS DRIFT`). Regenerate with `python3 scripts/generate_api_docs.py --app <folder>` and drop hand-written role lists. |

Other commands:

| Command | Does |
|---|---|
| `make ext-lint DIR=<dir or zip>` | The lint alone, in the dev container; `DIR` is relative to `/workspace/frappe-bench` (e.g. `apps-ext/<id>`). |
| `python3 scripts/generate_api_docs.py --app <folder> [--check]` | Writes the extension's `docs/api/` (`openapi.json` + `reference/`), access lines derived from the gates. `--check`: 1 on a defect, 3 when only stale. |
| `<bench>/env/bin/python -m ffl_core.extensions.lint <dir or zip>` | The lint anywhere `frappe` and `ffl_core` import (the module imports the `ffl_core.extensions` package, which imports `frappe`). Exit 0 / 1. |

### Removing it from the dev site

`make ext-test` leaves the extension installed. To take it off:

1. `bench --site dev.localhost uninstall-app <id>`, or restore the backup you took before the first run (`bench --site dev.localhost restore <file>`).
2. Delete `frappe-bench/apps/<id>` (the link), `frappe-bench/apps-ext/<id>` (the copy), `env/lib/python*/site-packages/<id>.pth`, and the `<id>` line of `sites/apps.txt`.
3. Only then run `make test` again.

## CI in your own repository

The workflow below builds a bench on the Frappe, ERPNext, HRMS and Frappe CRM tags an OpenGunstore release pins (`deploy/prod/frappe-refs.env`), installs the three core apps on a fresh site the way the core's CI does (from 0.6.4 the core requires Frappe CRM, so a workflow without the `crm` lines fails to create the site), and runs the same `scripts/ci/ext_test.sh` on your repository. It needs no secrets: the core repository is public. Set `EXT_ID` and `CORE_REF`.

```yaml
# .github/workflows/opengunstore.yml
name: OpenGunstore

on:
  push:
    branches: [main]
  pull_request:

jobs:
  ext-test:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      EXT_ID: acme_hello          # your extension id (the folder next to pyproject.toml)
      CORE_REF: v0.6.2            # the OpenGunstore release to test against
    services:
      mariadb:
        image: mariadb:11.8
        env:
          MYSQL_ROOT_PASSWORD: root
        ports: ["3306:3306"]
        options: >-
          --health-cmd="healthcheck.sh --connect --innodb_initialized"
          --health-interval=5s
          --health-timeout=5s
          --health-retries=30
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
    steps:
      - uses: actions/checkout@v4
        with:
          path: ext
      - uses: actions/checkout@v4
        with:
          repository: Old-Steel-Arsenal/opengunstore
          ref: ${{ env.CORE_REF }}
          path: core
      - uses: actions/setup-python@v5
        id: py
        with:
          python-version: "3.14"
      - uses: actions/setup-node@v4
        with:
          node-version: "24"

      - name: Install bench
        run: |
          pip install frappe-bench
          npm install -g yarn

      - name: Build the bench
        env:
          PYTHON_BIN: ${{ steps.py.outputs.python-path }}
          FRAPPE_REDIS_CACHE: redis://127.0.0.1:6379
          FRAPPE_REDIS_QUEUE: redis://127.0.0.1:6379
        run: |
          set -euo pipefail
          . core/deploy/prod/frappe-refs.env            # FRAPPE_REF, ERPNEXT_REF, HRMS_REF, CRM_REF
          bench init --skip-redis-config-generation --no-backups \
            --python "$PYTHON_BIN" --frappe-branch "$FRAPPE_REF" frappe-bench
          cd frappe-bench
          bench get-app --branch "$ERPNEXT_REF" erpnext https://github.com/frappe/erpnext.git
          bench get-app --branch "$HRMS_REF" hrms https://github.com/frappe/hrms.git
          bench get-app --skip-assets --branch "$CRM_REF" crm https://github.com/frappe/crm.git
          bench set-config -g db_host 127.0.0.1
          bench set-config -g -p db_port 3306
          for key in redis_cache redis_queue redis_socketio; do
            bench set-config -g "$key" redis://127.0.0.1:6379
          done
          for app in ffl_core ffl_integrations ffl_woo_sync; do
            ln -sfn "$GITHUB_WORKSPACE/core/apps/$app" "apps/$app"
            ./env/bin/pip install -q -e "apps/$app"
          done
          printf '%s\n' frappe erpnext hrms crm ffl_core ffl_integrations ffl_woo_sync > sites/apps.txt

      - name: Create the test site
        working-directory: frappe-bench
        run: |
          set -euo pipefail
          bench new-site ci.localhost --mariadb-root-password root --admin-password admin \
            --no-mariadb-socket --install-app erpnext --install-app hrms --install-app crm \
            --install-app ffl_core --install-app ffl_integrations --install-app ffl_woo_sync
          bench --site ci.localhost set-config allow_tests true
          bench --site ci.localhost execute ffl_core.setup.test_bootstrap.run

      # Exit 0 = all passed; 1 = install/migrate, tests, lint or an API-docs defect failed;
      # 2 = the folder was refused before anything ran; 3 = only docs/api is stale.
      - name: Install, test, lint, check the API docs
        run: core/scripts/ci/ext_test.sh frappe-bench ci.localhost "$EXT_ID" ext
```

Notes:

- `ext_test.sh` refuses an extension with `wheels/` (exit 2), as `make ext-test` does.
- Testing against a newer `CORE_REF` can return 3 with nothing wrong in your code: the core's generator or role names changed. Regenerate `docs/api/` with that core and commit it.
- To cover the lower bound of `requires_core`, add a `strategy.matrix` over two `CORE_REF` values.

## First-party extensions

The maintainers run the same `ext_test.sh` on their own extensions against each core release, from their deployment pipeline, through this repository's reusable `.github/workflows/_app-tests.yml` (`only_extensions`, `extension_list`; the inputs are documented in the file). This repository's own CI runs no extension tests.

