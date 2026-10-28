# Extension developer specification

For developers. An extension is a Frappe app that follows this specification. Anyone can write one and any store can install it from the Extensions page — there is no marketplace gate. The one condition is that the package passes the built-in checks (the "lint"). The lint and the Extensions page enforce most of the **MUST** rules below mechanically. The rest are checked in review (§17). This version applies to POS core `ffl_core` 0.1 and later.

Keywords follow RFC 2119: MUST, MUST NOT, SHOULD, MAY. Items marked *Planned* are not available yet.

## 0. Principles

1. **Extension points only.** An extension talks to the core through the hooks, the registry and the public functions listed in [Core hooks and the registry](hooks.md). Everything else in the core is internal and may change in any release.
2. **Never modify the core.** An extension MUST NOT change any file, DocType definition or core field property of `frappe`, `erpnext`, `ffl_core`, `ffl_integrations` or `ffl_woo_sync`. A package that ships core files is refused.
3. **Off means off; uninstall leaves nothing behind.** Switched off, an extension behaves as if it were not there: hooks return, UI disappears, tasks do not run, data stays. Uninstalled, it leaves no orphan custom field, role, shortcut or scheduled job.
4. **The counter comes first.** Text shown to staff is plain English. Settings pages follow the core standard. Cashiers never see code words.
5. **Compliance cannot be bypassed.** The bound book, Form 4473, serial custody, FastBound and payments are read-only for extensions or reached only through the core's public API.

## 1. Identity and naming

| Object | Rule | Example |
|---|---|---|
| Extension id (= app directory = Python package) | `<vendor>_<name>`, lowercase snake_case, vendor 2–12 characters | `acme_layaway` |
| Vendor | Yours to choose for a sideloaded package; an id MUST NOT collide with an extension already on the server (the page refuses) | `acme` |
| Title (`app_title`) | Human readable, at most 30 characters | `Acme Layaway` |
| Module (`modules.txt`) | Exactly one line, equal to the title; the module package `<id>/<scrubbed title>/__init__.py` MUST exist | `Acme Layaway` |
| Own DocTypes | MUST start with the first word of the title | `Acme Layaway Plan` |
| Custom fields on core DocTypes | fieldname MUST start with `<vendor>_` | `acme_layaway_plan` |
| Roles | MUST contain the title prefix | `Acme Layaway Manager` |
| Settings Single | `<Title> Settings`, one per extension | `Acme Layaway Settings` |
| Server endpoints | Inside the `<id>/api/` package; dotted path `<id>.api.<module>.<fn>` | `acme_layaway.api.plan.start` |
| JavaScript namespace | Only under `gunstore.ext.<id>`; MUST NOT pollute globals | `gunstore.ext.acme_layaway` |
| CSS classes and ids | Prefixed `<vendor>-<name>-`; MUST NOT override core or ERPNext selectors | `.acme-layaway-card` |
| Scheduled task names, Error Log titles | Prefixed `[<Title>]` | `[Acme Layaway] nightly sweep` |
| Database tables | Only through your own DocTypes; MUST NOT create or alter tables with SQL | — |

## 2. The manifest

The single source of truth is the `gunshop_extension` dict in `hooks.py`. It MUST be a plain dict literal (the lint and the registry read it without importing your code).

```python
# <id>/hooks.py
app_name = "acme_layaway"
app_title = "Acme Layaway"
app_publisher = "Acme Software LLC"
app_description = "Let a customer pay for a gun over time and pick it up when it is paid off."
app_license = "proprietary"          # or an SPDX id such as "MIT"
required_apps = ["ffl_core"]         # required; list other extensions you depend on here too

gunshop_extension = {
    "id": "acme_layaway",            # MUST equal app_name
    "title": "Acme Layaway",         # MUST equal app_title
    "publisher": "Acme Software LLC",
    "publisher_id": "acme",
    "version": "1.2.0",              # MUST equal <id>/__init__.py __version__
    "requires_core": ">=0.1.0,<1",   # PEP 440 range against ffl_core; MUST have an upper bound
    "requires_extensions": {},       # optional: {"acme_base": ">=1.0"}
    "permissions": [                 # §5.3 — MUST cover everything the code actually does
        "doc_events:Sales Invoice",
        "custom_fields:Customer",
        "pos_ui",
        "scheduler",
        "network:api.acme.example",
    ],
    "license_required": False,
    "homepage": "https://acme.example/layaway",
    "support": "support@acme.example",
    "release_notes": "change_log",
    "disable_notice": "acme_layaway.hooks_impl.notice.disable_notice",   # optional: one sentence shown before Turn off
}
```

`<id>/__init__.py` holds only `__version__ = "1.2.0"`.

## 3. Package layout

```
acme_layaway/                      # repository root = app root
├── acme_layaway/
│   ├── __init__.py                # __version__
│   ├── hooks.py                   # manifest + hooks
│   ├── modules.txt                # one line: Acme Layaway
│   ├── patches.txt
│   ├── acme_layaway/              # the module package (DocType / Page / Report / Workspace)
│   │   └── doctype/acme_layaway_plan/…
│   ├── api/                       # every whitelisted endpoint
│   ├── hooks_impl/                # doc_events / scheduler handlers (not in api/)
│   ├── setup/                     # idempotent after_install / before_uninstall
│   ├── fixtures/                  # Custom Field / Role / Workspace, with filters
│   ├── public/js/<id>.bundle.js   # registers with gunstore.extensions only
│   ├── public/css/                # prefixed selectors
│   ├── change_log/v1/v1_2_0.md    # plain-language release note (§13)
│   ├── translations/
│   └── tests/                     # FrappeTestCase + tests/js/*.test.js
├── README.md                      # §16
├── pyproject.toml                 # name == id; pure-Python dependencies only
├── wheels/                        # optional: your dependencies as pure-Python .whl files (§12)
└── LICENSE
```

MUST NOT be present: `setup.py` build steps, C extensions, `node_modules`, `.so` files, wheels that are not pure Python or that sit outside `wheels/`, `.git`, `.env*`, keys or certificates, SQL dumps, `__pycache__`. The upload zip's root MUST be the app root (or a single top-level folder containing it, as GitHub downloads are).

## 4. Lifecycle

| Stage | Triggered by | The extension must |
|---|---|---|
| Install (`install_app`) | Extensions page → the store's agent | `after_install` is idempotent: create own master data, sync fixtures. MUST NOT touch core records. |
| On / off | The switch on the Extensions page (the manager's intent) | What counts is the effective state: switched on, installed on this site and compatible (§6). Not effective: every `@extension_hook` handler returns; `require_enabled` endpoints raise PermissionError; registry items are not rendered; scheduled tasks are skipped. Data stays. |
| Upgrade | Extensions page: upload, or fetch from a public Git repository, a strictly higher version of the same id. An extension from a configured update source is also offered as **Update to {version}** on its card (§15). The store backs up the database, swaps the code, restarts and runs `bench migrate`. After a failed upgrade the store rolls back first, then upgrades again. | `patches.txt` is idempotent; never drop columns or change field types in a way that loses data; add a new change_log file (the manager reads every note between the two versions before confirming). The switch and your data are kept. |
| Uninstall (`remove_app`) | Extensions page: backup, then typed-id confirmation | `before_uninstall` MUST remove every Custom Field, Property Setter, Role, workspace shortcut, Scheduled Job Type and navbar item it created. Frappe drops your own DocType tables. |
| Core upgrade | Operations | Outside `requires_core`, the page marks the extension "Needs core …" and pauses it: never uninstalled, the manager's switch kept, running again by itself once compatible. |

## 5. Extension points

### 5.1 Allowed Frappe hooks (whitelist — anything else fails the lint)

| Hook | Scope | Constraint |
|---|---|---|
| `doc_events` | Any DocType | Handlers MUST be decorated with `@extension_hook`. On the compliance DocTypes (§8) only `after_insert` / `on_update` / `on_submit` / `on_cancel` for read-only purposes; MUST NOT change their fields or block them in `validate` / `before_*`. |
| `scheduler_events` | Your own functions | Declare the `scheduler` permission; see §11. |
| `fixtures` | Custom Field, Property Setter (own DocTypes only), Role, Workspace, seed data of own DocTypes | MUST carry `filters` that select only your own records. |
| `doctype_js` / `doctype_list_js` / `doctype_tree_js` / `doctype_calendar_js` | Own DocTypes freely; core DocTypes add-only | MUST NOT hide, disable or relabel core fields and buttons. |
| `app_include_js` / `app_include_css` | One bundle each | JS only calls `gunstore.extensions.register(...)`; CSS is prefix-scoped. Needs `pos_ui` or `desk_ui`. |
| `after_install` / `before_uninstall` / `after_migrate` | Own setup functions | Idempotent; §4. |
| `boot_session` / `extend_bootinfo` | Only `bootinfo["ext"][<id>]` | MUST NOT touch other keys. |
| `permission_query_conditions` / `has_permission` | Own DocTypes only | — |
| `website_route_rules` / `portal_menu_items` | Routes MUST start with `/ext/<id>/` | Needs `portal`. |
| `website_redirects` | `source` = a legacy path named in the README; `target` MUST be under `/ext/<id>/` | Needs `portal`. |
| `standard_help_items` / `standard_navbar_items` | At most one each, label contains the title | — |
| `add_to_apps_screen` | Only with a `has_permission` that returns False (keeps the extension out of the app switcher) | Pages join the POS shell through `gunshop_sidebar_items` and `mirror_gun_shop_sidebar`. |
| `jinja` / `jenv` | Method names prefixed `<vendor>_` | — |
| Core named hooks (`gunshop_*`) | Per their contracts | [Core hooks and the registry](hooks.md). |
| First-party hooks (`override_whitelisted_methods`, `auth_hooks`, the drop-ship source and price hooks) | First-party extensions only (§8.1) | [Core hooks — First-party only](hooks.md#first-party-only). Any other extension declaring one fails the lint. |

### 5.2 Core named hooks

See [Core hooks and the registry](hooks.md).

### 5.3 Permissions (manifest `permissions`)

| Value | Meaning | On the card |
|---|---|---|
| `doc_events:<DocType>` | Reacts when that DocType is saved | listed |
| `custom_fields:<DocType>` | Adds fields to that core DocType | listed |
| `pos_ui` / `desk_ui` | Adds buttons to the sale screen / pages and form buttons | listed |
| `scheduler` | Runs scheduled tasks | listed |
| `network:<host>` | Sends data to that host (one entry per host) | **red** |
| `portal` | Provides website or portal routes | listed |
| `files` | Reads and writes File | listed |
| `compliance_read` | Reads bound-book records (§8) | **red** |
| `public_endpoint` | Has endpoints with `allow_guest=True` | **red** |
| `woo_product_meta` | Adds its own values to website listings through `gunshop_woo_product_meta` | listed |
| `first_party` | Reserved for the maintainers' own extensions (§8.1) | **red** |

The lint derives what the code actually uses — `doc_events` keys, `scheduler_events`, `fixtures/custom_field.json`, portal hooks and `www/`, `allow_guest`, `https://<host>` literals in Python, reads of the compliance DocTypes — and every derived permission MUST be declared.

## 6. The on/off contract

The core provides `ffl_core.extensions`: `extension_hook`, `require_enabled`, `is_enabled`. Rules:

- `require_enabled(app, roles=...)` MUST be the first executable statement of every whitelisted endpoint (`frappe.only_for(...)` is also accepted by the gate audit).
- `is_enabled` is for templates and branches; it MUST NOT be used to bypass `require_enabled`.
- `enabled` on the registry row is the manager's intent. Only the manager's switch (and a fresh install) writes it. What runs is the effective state: `enabled` and installed on this site and compatible with the core. `is_enabled`, `require_enabled`, `@extension_hook` and the registry all follow the effective state. An incompatible extension the manager left on shows **Paused** on the Extensions page.
- `gunshop_extension_toggled(app, effective)` fires exactly once per change of the effective state while the extension is installed: a switch flip, or a compatibility change after a core update. Install and uninstall do not fire it.
- JavaScript renders only from the registry; an extension MUST NOT check the switch itself and insert DOM.
- The core's scheduler wrapper skips `@extension_hook` functions while off.
- While off: your own DocTypes stay readable but not writable (use a `has_permission` hook that returns False for write/create/delete/submit/cancel/amend and True otherwise — a falsy return is a denial); custom field values on historical documents stay; core flows are untouched.
- **Integrity exception:** a `doc_events` handler that only keeps data consistent when a core document *you created* is cancelled or amended (for example, putting a consignment line back when its settlement invoice is cancelled) MAY run while off. Say so in a comment next to the hook.

## 7. Data and model

- Own DocTypes belong to your module; `autoname` uses your own series (`ACME-LAY-.YYYY.-.#####`); MUST NOT change core naming series.
- Custom fields on core DocTypes: prefixed, MUST NOT be `reqd`, MUST NOT be `unique` (first-party exception: a field used as an idempotency key), SHOULD sit in your own tab or section after the core's last field. Define them once in `fixtures/custom_field.json`; at install time feed the same JSON to `create_custom_fields` (`after_install` runs before `sync_fixtures`).
- Property Setters MUST NOT touch core fields of core DocTypes.
- Raw SQL: parameterized or `frappe.qb`; MUST NOT `UPDATE` / `DELETE` core tables; MUST NOT read `__Auth`, password columns or another app's Password fields.
- Hooks MUST NOT call `frappe.db.commit()`; long work goes through `frappe.enqueue`.
- MUST NOT delete core records. Attachments go through `File` with `is_private=1`; customer identity documents (driver's license, 4473) MUST NOT be copied into your tables.

## 8. Compliance red lines

Breaking any of these is refused at review and painted red on the card.

| Object | An extension may | An extension MUST NOT |
|---|---|---|
| `FFL Acquisition` / `FFL Disposition` (bound book) | Read; mirror or notify after submit | Create, modify, cancel, delete; change any field; block submission |
| Form 4473 (`Pending 4473`, `start_4473`, manager override) | Read status | Start, skip, override, modify |
| `Serial No` compliance and custody fields (`is_consignment`, `dealer_transfer`, custody, FastBound id, status) | Read; write your **own** custom fields | Write core fields; set a serial sold or delivered directly |
| FastBound / ATF EZ Check / Payroc / ShipStation / WooCommerce credentials and clients | Use the core's public API | Read credentials; build your own client; log secrets (a card processor extension brings its own gateway client behind `CardProcessor` — never another processor's) |
| Payments (`Sales Invoice` / `POS Invoice` payments, card charge rows) | Read; add your own rows through the core API; as a card processor, answer the core's gateway calls | Change core payment rows, tax tables or totals; book money outside the POS flow (a card processor never writes a charge row, a terminal payment or a Payment Entry — the core does) |
| Taxes | Read | Change templates, rates or the tax base |
| Customer PII (license number, date of birth, address) | Read what you need, masked in the UI | Export to another host (unless declared with `network:` and explained in the README); write to logs |

If a feature needs one of the MUST NOT capabilities, it belongs in the core: open an issue instead of writing an extension.

### 8.1 First party

Extensions published by the project maintainers may declare `permissions: ["first_party"]` and call a documented set of core functions that other extensions may not (dealer-transfer dispositions, custody flags, the consignment warehouses, settlement invoicing, the ShipStation client). They may also register the first-party-only hooks ([Core hooks](hooks.md#first-party-only)).

The manifest's claim is not enough on its own while packages are unsigned. The lint accepts `first_party` only from the maintainers' publisher id **and** an extension id on a list shipped with the core (`FIRST_PARTY_IDS` in `ffl_core/extensions/lint.py`). Any other package declaring it fails the lint, and no third-party extension can be added to the list without a core release. Everything else in this specification applies to first-party extensions unchanged.

## 9. Server endpoints

1. Only inside `<id>/api/`; `@frappe.whitelist(methods=["POST"])` (`["GET"]` for reads).
2. The first executable statement MUST be the gate: `require_enabled(app, roles=...)`.
3. `allow_guest=True` MUST be declared as `public_endpoint`, rate-limited and input-whitelisted.
4. The docstring MUST have a one-line summary, `Args:` and `Returns:` (the core's API-docs format). The core's `scripts/generate_api_docs.py --app <folder>` reads them to write the extension's `docs/api/`: an OpenAPI file and one reference page per module, with each endpoint's access line worked out from its `require_enabled(…, roles=…)` gate.
5. Type-annotate inputs and validate them; never build SQL, shell commands or paths from user input.
6. Return JSON-serializable values; raise `frappe.throw(_("…"))` for errors — never return `{"error": …}` as a success.
7. Work longer than about 2 seconds goes to `frappe.enqueue(queue="long")`; never poll an external service inside a request.
8. Data-changing endpoints SHOULD accept an idempotency key.
9. MUST NOT override core endpoints. `override_whitelisted_methods` is refused for every extension except a first-party one (§8.1), which uses it only to keep an endpoint that moved out of the core into the extension answering at its old path.

## 10. Front end

- Register once with `gunstore.extensions.register(id, {...})` (see [the registry](hooks.md#the-javascript-registry)). The core renders, orders, hides while off and de-duplicates. MUST NOT query the POS DOM and insert nodes; MUST NOT override `erpnext.PointOfSale.*`.
- `ctx` is a read-only snapshot; change documents through the core's public actions or your own endpoints.
- Form scripts may add buttons and field logic; MUST NOT remove or disable core buttons or set core fields hidden or read-only.
- Remove event listeners when a page is hidden.
- Settings: one Single named `<Title> Settings`, tabs such as Connection / Options / Advanced, one sentence per field, credentials as `Password` fields read with `get_password`, no red banners, long explanations in the README.
- Every user-facing string goes through `__()` / `_()`. No DocType names, fieldnames or code words in text shown to staff; errors say what to do next.
- Styles use prefixed classes only; use the core's CSS variables for dark mode; never override `:root`, `.btn`, `.form-control` or `.point-of-sale-app`.

## 11. Background tasks

- Each `scheduler_events` function MUST be `@extension_hook`, idempotent and under 5 minutes; heavy work is enqueued on the long queue.
- No full-table scans in the every-minute slots; work incrementally from a cursor or `modified > last_run`.
- Failures go to `frappe.log_error(title="[<Title>] …")` without credentials, full request bodies or customer PII.
- External calls have a timeout (≤ 30 s) and a retry cap; never call the network synchronously inside a hook.

## 12. Security (checked by the lint)

- MUST NOT use `subprocess`, `os.system`, `os.popen`, `eval`, `exec`, `pickle`, `frappe.utils.execute_in_shell`, or read or write `site_config.json` / `common_site_config.json`, or run `pip`.
- `ignore_permissions=True` only on your own DocTypes, with a comment explaining why.
- Escape HTML (`frappe.utils.escape_html`, Jinja autoescape); escape user input before `frappe.msgprint`.
- Outbound requests only to hosts declared with `network:`; TLS verification stays on (`verify=False` is refused).
- Dependencies: every entry of `[project].dependencies` in `pyproject.toml` MUST be satisfied by a wheel you ship in `wheels/` or by a package the core already has; the same holds for each bundled wheel's own `Requires-Dist`. Wheels MUST be pure Python (`py3-none-any` or `py2.py3-none-any`): no compiled files inside (`.so`, `.pyd`, `.dylib`, `.dll`, `.exe`, `.pyc`), while data files such as a CA bundle's `.pem` are fine — the package itself still MUST NOT carry keys or certificates (§3). Wheels MUST NOT bundle a package the core already provides (the lint says "provided by core x.y, do not bundle"). Wheels count toward the unpacked size limit. The store installs them without network access; your code still never runs `pip`.
- MUST NOT read another extension's settings or tables.

## 13. Versions and compatibility

- `__version__` equals the manifest `version`, strictly `x.y.z[-beta.N]`. Breaking your own data format or public contract is a major bump.
- `requires_core` MUST have an upper bound. Outside it, the store pauses the extension until it is compatible again; the manager's switch is not changed.
- An upgrade MUST carry a strictly higher version (PEP 440 order). The same version or an older one is refused; going back to an older version is an operator's restore ([Roll back an upgrade](../self-hosting.md#roll-back-an-upgrade)).
- An extension has no package metadata on the store (it is wired with a `.pth`, not pip): do not call `importlib.metadata.version()` on yourself; read `<id>.__version__`.
- Release notes: `change_log/v<major>/v<version with dots as underscores>.md` (`v1/v1_2_0.md`; a beta is `v1_2_0-beta_1.md`). First line `### Version x.y.z — Month D, YYYY`, then `#### New / Improved / Fixed`, each item a bold feature name and one sentence for the counter. The POS shows an enabled extension's notes at login and under the sidebar-header menu (the store name at the top of the sidebar) → What's New in POS.

### 13.1 The stable surface

Extensions may depend on: the hooks in §5.1, the core named hooks, `ffl_core.extensions.*`, `ffl_core.roles.*`, `ffl_core.workspace_shortcuts.ensure_shortcut` / `remove_shortcut`, `ffl_core.setup.sidebar.mirror_gun_shop_sidebar`, `ffl_core.utils.single_flag`, the core endpoints documented in the POS API reference, `ffl_integrations.distributor.base.DistributorProvider`, `ffl_integrations.distributor.freight.estimate_orders` (distributor freight estimates), and `ffl_integrations.payments.base` (`CardProcessor`, `CardResult`, `CardProcessorError`, for a card processor registered through `gunshop_card_processors`). Everything else is internal.

## 14. Quality gates

| Gate | Requirement |
|---|---|
| Lint (required for every upload) | Package layout; identity and manifest; hook whitelist; permissions cover use; forbidden APIs; endpoint gates; docstrings (warning); release note present (warning); DocType and custom-field prefixes; every `.py` compiles; the module package exists. Run it yourself: `python -m ffl_core.extensions.lint <dir or zip>` in any environment with `ffl_core` importable, or `make ext-lint DIR=…` in the core's dev stack. |
| Unit and integration tests | `tests/` with `FrappeTestCase`. Cover: every hook does nothing while off; every endpoint returns 403 without the role and while off; a repeated call to a data-changing endpoint does not write twice; `after_install` and `before_uninstall` are idempotent and leave no orphan Custom Field or Role. |
| API reference | `docs/api/` is generated with `python3 scripts/generate_api_docs.py --app <folder>` from the core repository and committed. With `--check` it fails on an undocumented endpoint or a stale file. |
| All at once | In the core repository's dev stack, `make ext-test ID=<id> DIR=<folder>` installs the extension on the development site (a re-run migrates the site, so DocType and field changes land) and runs its tests, the lint and the API-reference check. It does not install `wheels/` yet: an extension that ships them is refused. First-party extensions go through the same three steps in the core's CI ([Extensions](README.md#first-party-extensions-in-ci)). |
| JavaScript | `tests/js/*.test.js` with `node --test`; registry `condition` functions are unit-testable. |
| Compatibility | Run the suite at the lower bound of `requires_core` and at the current core. |
| Isolation | Tests never reach the network or real credentials and never depend on one store's data. |
| Performance | Hooks under 50 ms at p95 (excluding enqueue); registry `condition` functions synchronous and O(1). |

## 15. Packaging and installation

- Package: `<id>-<version>.zip`, root = app root, at most 50 MB, unpacking to at most 200 MB, no symlinks, no absolute or `..` paths. `git archive --format=zip HEAD` from the repository root produces a valid one.
- Installation from the Extensions page (**Upload** tab): the store lints the package, shows the review card, and after the manager types the id, stages the code, restarts the POS once and installs the extension. Details for operators are in the [Extensions section of the self-hosting guide](../self-hosting.md#extensions).
- The **Upload** tab also fetches a package from a public Git repository: `https://<host>/<owner>/<repo>` (optionally ending in `.git`) plus a tag or branch. Prefer tags. Addresses with credentials, non-https addresses, hosts that resolve to private, loopback or link-local addresses, and private repositories are refused; package a private repository as a zip. The fetched tree has `.git` removed and goes through the same lint and review card, which shows the address, the tag and the commit.
- The same id with a strictly higher version is upgraded in place: backup, code swap, restart, `bench migrate`. If the migration fails, the card shows the backup path and the operator rolls back by hand ([Roll back an upgrade](../self-hosting.md#roll-back-an-upgrade)); until then every upload of that id is refused, so a failed upgrade is rolled back first and upgraded again, never upgraded over. The same or a lower version, and extensions shipped in the POS image, are refused.
- **Update source.** A site can register a source of newer versions through the `gunshop_extension_updates` hook ([Core hooks](hooks.md)). The core's integrations app registers the [Data Service](../data-service.md) as one, and it serves only the publisher it names. The Extensions page checks the source daily and on Refresh. A newer version that fits the store's core shows **Update to {version}** on the card of an extension installed from an upload, a Git fetch or the source itself (never on one shipped in the POS image). The download is checked against the hash the source announced, then goes through the same lint, review and upgrade path as an upload. Other publishers distribute by file or Git as above.
- Marketplace distribution, signing and paid licenses are *Planned*.

## 16. README

Three sections: **For the counter** (two or three sentences on what staff can do), **For the manager** (installation, each settings field, roles needed, external accounts, every `network:` host and what is sent to it), **For developers** (hooks and core functions used, the data model in one sentence, the commands that run the tests, the lint and the API-reference generator).

`docs/api/` SHOULD be the generated reference, committed with the code. Write any overview by hand on a separate page such as `docs/api/README.md`; the generator does not touch it.

## 17. Review checklist (for a human reviewer, beyond the lint)

1. The feature belongs in an extension, not in the core.
2. No §8 red line is touched.
3. Every `network:` host is explained in the README.
4. Switched off, nothing of the extension remains visible on the sale screen, forms or menus.
5. Uninstalled, no orphan Custom Field, Role, Workspace shortcut or Scheduled Job Type.
6. The settings page follows the standard.
7. Staff-facing text has no code words; errors are actionable.
8. Every endpoint is gated and documented; no undeclared `allow_guest`.
9. Scheduled tasks are idempotent, short and incremental.
10. Tests cover off / no-role / idempotency and never reach the network.
11. Release notes are plain language; the version matches in all three places.
12. Dependencies do not conflict; the package contains no `.git` or secrets.

## 18. Starting point

`make ext-new ID=<id> TITLE="…" PUBLISHER="…"` in the core repository creates an extension that already meets this specification: the manifest, a `doc_events` handler wrapped in `@extension_hook`, two gated and documented endpoints, a registry button, a release note, the three kinds of tests from §14 and a generated `docs/api/`. The lint finds nothing in it and its tests pass. The [Quick start](quickstart.md) walks through it. The repository's `deploy/prod/tests/ext_hello` is a generated copy of that skeleton; read it as a worked example.
