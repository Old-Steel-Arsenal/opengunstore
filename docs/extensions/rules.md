# Rules

Every rule an extension follows. **lint** = `ffl_core.extensions.lint` refuses the package (or warns, where marked); the Extensions page runs it on every upload. **review** = the lint does not check it; a reviewer or the store does. The lint is a floor built on string and AST heuristics, not a review.

If a feature needs something listed under "don't" for compliance, payments or taxes, it belongs in the core: open an issue.

## Identity and naming

| Do | Don't | Checked |
|---|---|---|
| Id `<vendor>_<name>`: `^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]*$`; equal to `app_name`, the package folder, `[project].name` and the manifest `id` | Reuse an id already on the server | lint (id on the server: upload) |
| `app_title` at most 30 characters, equal to the manifest `title`; `modules.txt` exactly one line equal to it; the module package `<id>/<scrubbed title>/__init__.py` exists | | lint |
| Own DocTypes start with the first word of the title (`Acme Layaway Plan`) | | lint |
| Custom fields on core DocTypes: fieldname starts with `<vendor>_` (Section, Column and Tab Breaks excepted) | | lint |
| Roles contain the title (`Acme Layaway Manager`); one Settings Single named `<Title> Settings` | | review |
| Endpoints in `<id>/api/`, path `<id>.api.<module>.<fn>`; handlers in `hooks_impl/` | | review |
| JavaScript only under `gunstore.ext.<id>`; CSS classes prefixed `<vendor>-<name>-` | Pollute globals; override core or ERPNext selectors, `:root`, `.btn`, `.form-control`, `.point-of-sale-app` | review |
| Scheduled task names and Error Log titles prefixed `[<Title>]` | | review |

## Allowed Frappe hooks

Any other top-level name in `hooks.py` fails the lint, except Frappe metadata keys, names starting with `_`, and the [named hooks](hooks.md#named-hooks).

| Hook | Constraint | Checked |
|---|---|---|
| `doc_events` | Handlers wrapped in `@extension_hook` | review |
| `scheduler_events` | Own functions, `@extension_hook`, idempotent, under 5 minutes | review |
| `fixtures` | Custom Field, Property Setter (own DocTypes only), Role, Workspace, seed data of own DocTypes; always with `filters` selecting only your records | review |
| `doctype_js`, `doctype_list_js`, `doctype_tree_js`, `doctype_calendar_js` | Own DocTypes freely; core DocTypes add-only (never hide, disable or relabel core fields and buttons) | review |
| `app_include_js` / `app_include_css` | One bundle each; JS only calls `gunstore.extensions.register(...)`; `app_include_js` needs `pos_ui` or `desk_ui` | lint (permission); review (rest) |
| `after_install`, `before_uninstall`, `after_migrate` | Own setup, idempotent; never touch core records | review |
| `boot_session`, `extend_bootinfo` | Only `bootinfo["ext"][<id>]` | review |
| `permission_query_conditions`, `has_permission` | Own DocTypes only | lint |
| `website_route_rules` (`from_route`), `portal_menu_items` (`route`), `website_redirects` (`target`) | Start with `/ext/<id>/`; declare `portal`; a redirect `source` is a legacy path named in the README | lint (prefix, permission); review (source) |
| `standard_help_items`, `standard_navbar_items` | At most one each, label contains the title | review |
| `add_to_apps_screen` | Every entry has a `has_permission` | lint |
| | …and that function returns `False` | review |
| `jinja`, `jenv` | Method names prefixed `<vendor>_` | review |
| First-party hooks (`override_whitelisted_methods`, `auth_hooks`, the drop-ship hooks) | Only with `first_party` ([list](hooks.md#first-party-only)) | lint |

## On / off

| Do | Don't | Checked |
|---|---|---|
| `require_enabled(APP, roles=…)` (or `frappe.only_for(...)`) as the first executable statement of every module-level whitelisted endpoint (docstring and imports skipped) | `require_enabled(APP)` without `roles=`; gate an endpoint on `is_enabled` | lint |
| Wrap every `doc_events` and `scheduler_events` handler in `@extension_hook` | | review |
| Render UI only through the registry | Check the switch yourself and insert DOM | review |
| While off: own DocTypes readable, not writable (a `has_permission` returning `False` for write/create/delete/submit/cancel/amend, `True` otherwise); custom field values stay; core flows untouched | | review |
| A `doc_events` handler that only keeps data consistent when a core document *you created* is cancelled or amended may run while off; say so in a comment | | review |
| Switched off, nothing of the extension is visible on the sale screen, forms or menus | | review |

## Data and model

| Do | Don't | Checked |
|---|---|---|
| Own DocTypes in your module, `autoname` from your own series | Change core naming series | review |
| Custom fields defined once in `fixtures/custom_field.json`, fed to `create_custom_fields` in `after_install` too (it runs before `sync_fixtures`); in your own tab or section after the core's last field | Make them `reqd` or `unique` (first-party exception: an idempotency key) | review |
| Property Setters on your own DocTypes | Property Setters on core fields of core DocTypes | review |
| SQL parameterized or `frappe.qb`; tables only through your own DocTypes | `UPDATE` / `DELETE` core tables; create or alter tables with SQL; read `__Auth`, password columns, another app's Password fields | review |
| Long work through `frappe.enqueue(queue="long")` | `frappe.db.commit()` in a hook | review |
| Attachments through `File` with `is_private=1` | Delete core records; copy driver's licenses or 4473s into your tables | review |
| `ignore_permissions=True` only on your own DocTypes, with a comment saying why | Read another extension's settings or tables | review |

## Compliance red lines

| Object | May | Must not | Checked |
|---|---|---|---|
| `FFL Acquisition` / `FFL Disposition` (bound book) | Read (declare `compliance_read`); `doc_events` `after_insert` / `on_update` / `on_submit` / `on_cancel` for read-only purposes (declare `doc_events:<DocType>`) | Create, modify, cancel, delete; change a field; block submission in `validate` / `before_*` | lint (both permissions are derived); review (which events, what they do) |
| Form 4473 (`Pending 4473`, `start_4473`, manager override) | Read status | Start, skip, override, modify | review |
| `Serial No` compliance and custody fields (`is_consignment`, `dealer_transfer`, custody, FastBound id, status) | Read; write your own custom fields | Write core fields; set a serial sold or delivered directly | review |
| FastBound, ATF eZ Check, Payroc, ShipStation, WooCommerce credentials and clients | Use the core's public API | Read credentials; build your own client; log secrets (a card processor brings its own gateway client behind `CardProcessor`, never another processor's) | review |
| Payments (invoice payments, card charge rows) | Read; add your own rows through the core API; as a card processor, answer the core's gateway calls | Change core payment rows, tax tables or totals; book money outside the POS flow (a card processor never writes a charge row, a terminal payment or a Payment Entry) | review |
| Taxes | Read | Change templates, rates or the tax base | review |
| Customer PII (license number, date of birth, address) | Read what you need, masked in the UI | Send it to another host unless declared with `network:` and explained in the README; write it to logs | review |

### First party

`first_party` is accepted only from `publisher_id` `"osa"` **and** an id on the list shipped with the core (`FIRST_PARTY_IDS` in `ffl_core/extensions/lint.py`): `osa_ai`, `osa_ca_compliant`, `osa_consignment`, `osa_dropship`, `osa_range` (lint). No third-party id can be added without a core release. First-party extensions may also call a documented set of core functions others may not (dealer-transfer dispositions, custody flags, the consignment warehouses, settlement invoicing, the ShipStation client) and register the first-party hooks; reading the bound book does not make them derive `compliance_read`. Every other rule applies unchanged.

## Endpoints

| Do | Don't | Checked |
|---|---|---|
| `@frappe.whitelist(methods=["POST"])` (`["GET"]` for reads) | | review |
| Docstring with a one-line summary, `Args:` and `Returns:` | | lint (warn: no docstring); API-docs check (`Args` / `Returns`) |
| `allow_guest=True` only declared as `public_endpoint`, rate-limited and input-whitelisted | | lint (permission); review (rest) |
| Type-annotate and validate inputs | Build SQL, shell commands or paths from user input | review |
| Return JSON-serializable values; raise `frappe.throw(_("…"))` | Return `{"error": …}` as a success | review |
| Work over about 2 s to `frappe.enqueue(queue="long")`; an idempotency key on data-changing endpoints | Poll an external service inside a request | review |
| | Override core endpoints (`override_whitelisted_methods` is first-party only) | lint |

## Front end and text

| Do | Don't | Checked |
|---|---|---|
| Register once with `gunstore.extensions.register(id, {...})`; treat `ctx` as read-only | Query the POS DOM and insert nodes; override `erpnext.PointOfSale.*` | review |
| Form scripts add buttons and field logic | Remove or disable core buttons; set core fields hidden or read-only | review |
| Remove event listeners when a page is hidden | | review |
| Settings: one Single, tabs such as Connection / Options / Advanced, one sentence per field, credentials as `Password` fields read with `get_password` | Red banners; long explanations on the form (they go in the README) | review |
| Every user-facing string through `__()` / `_()`; errors say what to do next | DocType names, fieldnames or code words in text staff see | review |
| The core's CSS variables (dark mode) | | review |

## Background tasks

| Do | Don't | Checked |
|---|---|---|
| Incremental work (a cursor or `modified > last_run`) | Full-table scans in the every-minute slots | review |
| `frappe.log_error(title="[<Title>] …")` | Log credentials, full request bodies or PII | review |
| External calls with a timeout (≤ 30 s) and a retry cap | Network calls synchronously inside a hook | review |

## Security and packaging

| Do | Don't | Checked |
|---|---|---|
| | `import subprocess` / `pickle`; call `eval`, `exec`, `os.system`, `os.popen`, `frappe.utils.execute_in_shell`, `pickle.loads` | lint (non-test modules; by name) |
| | `verify=False` | lint |
| | A string mentioning `site_config.json`, `common_site_config` or `pip install` | lint |
| Escape HTML (`frappe.utils.escape_html`, Jinja autoescape), and user input before `frappe.msgprint` | | review |
| Outbound requests only to hosts declared with `network:` | | lint (literal `https://` hosts); review (computed hosts) |
| `pyproject.toml` at the app root, `[project].dependencies` met by the core or by pure-Python wheels (`py3-none-any`, `py2.py3-none-any`) directly in `wheels/` | Bundle a package the core already has; a wheel with compiled files, keys or dumps; a wheel whose top-level names clash with the core, your id or another extension | lint |
| Every `.py` compiles; README.md present | | lint (README: warn) |
| | Ship core app folders, `.git`, `.env*`, `node_modules`, `setup.py`, `__pycache__`, `.pyc`, `.pem`, `.key`, `.sql`, `.so`, symlinks | lint (in a zip; a checkout skips `.git`, `__pycache__`, `.pyc`) |
| Zip at most 50 MB, unpacking to at most 200 MB (wheels included), at most 20,000 entries; root = the app root or one top folder | Absolute paths, `..`, symlinks | upload |

## Versions and compatibility

| Do | Checked |
|---|---|
| `__version__` equals the manifest `version` | lint |
| `requires_core` with an upper bound; on 0.x: `>=<lowest 0.x you support>,<1` | lint (bound) |
| A release note at `change_log/v<major>/v<x_y_z>.md` for the current version | lint (warn) |
| An upgrade carries a strictly higher version; breaking your own data format or public contract is a major bump | upload; review |
| `patches.txt` idempotent; never drop a column or change a field type in a way that loses data | review |
| Read `<id>.__version__`, never `importlib.metadata.version()` (an extension has no package metadata) | review |

## README

Three sections. **For the counter:** two or three sentences on what staff can do. **For the manager:** installation, each setting, roles needed, external accounts, every `network:` host and what is sent to it. **For developers:** hooks and core functions used, the data model in one sentence, the commands that run the tests, the lint and the API-docs generator. Hand-written API overviews go in `docs/api/README.md`; the generator leaves it alone.

## Review checklist

A reviewer checks, beyond the lint: the feature belongs in an extension, not the core; no red line is touched; every `network:` host is explained in the README; off means invisible; uninstall leaves no orphan Custom Field, Role, Workspace shortcut or Scheduled Job Type; the settings page follows the standard; staff text has no code words; scheduled tasks are idempotent, short and incremental; tests cover off / no-role / idempotency and never reach the network; release notes are plain language; the package has no secrets.
