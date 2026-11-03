# Manifest and layout reference

## The manifest

The manifest is the `gunshop_extension` dict in `<id>/hooks.py`. It must be a plain dict literal: the lint and the registry read it with `ast.literal_eval`, without importing your code. The registry snapshots it into the extension's `POS Extension` row.

```python
# acme_layaway/hooks.py
app_name = "acme_layaway"
app_title = "Acme Layaway"
app_publisher = "Acme Software LLC"
app_description = "Let a customer pay for a gun over time and pick it up when it is paid off."
app_license = "MIT"
required_apps = ["ffl_core"]

gunshop_extension = {
    "id": "acme_layaway",
    "title": "Acme Layaway",
    "publisher": "Acme Software LLC",
    "publisher_id": "acme",
    "version": "1.2.0",
    "requires_core": ">=0.4.0,<1",
    "requires_extensions": {},
    "permissions": ["doc_events:Sales Invoice", "custom_fields:Customer", "pos_ui", "scheduler",
                    "network:api.acme.example"],
    "license_required": False,
    "homepage": "https://acme.example/layaway",
    "support": "support@acme.example",
    "release_notes": "change_log",
    "disable_notice": "acme_layaway.hooks_impl.notice.disable_notice",
}
```

| Field | Required | Type | Rule / effect | Checked by |
|---|---|---|---|---|
| `id` | yes | str | Equals `app_name`, the package folder and `[project].name` in `pyproject.toml`. `<vendor>_<name>`: regex `^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]*$`. Must not collide with an extension already on the server. | lint; upload |
| `title` | yes | str | Equals `app_title`, at most 30 characters. `modules.txt` holds exactly this one line. | lint |
| `version` | yes | str | Equals `__version__ = "…"` in `<id>/__init__.py`. Write `x.y.z` or `x.y.z-beta.N` (PEP 440 readable; the release-note file name follows it). An upgrade must be strictly higher. | lint; upload |
| `requires_core` | yes | str | PEP 440 range against `ffl_core`'s version, with an upper bound (`<`, `<=`, `==`, `~=` or `===`). On the 0.x line write `>=<lowest 0.x you tested>,<1`. A beta core is compared by its base release (`0.7.0-beta.1` satisfies `>=0.7.0`). Outside the range the extension is paused, not removed. | lint (bound); registry (range) |
| `permissions` | yes | list[str] | What the code does, shown as chips on the review card ([below](#permissions)). Must cover everything the lint derives from the code. | lint |
| `publisher` | no | str | Shown on the card. | — |
| `publisher_id` | no | str | Your vendor (`make ext-new` writes the id's vendor part). `osa` is the maintainers' (required for `first_party`). The update source offers updates only for extensions whose `publisher_id` matches it. | lint (`first_party`) |
| `requires_extensions` | no | dict `{id: specifier}` | Recorded in the manifest snapshot; the core does not check it today. Put hard dependencies in `required_apps` too, which Frappe enforces at install. | — |
| `license_required` | no | bool | `True` shows a **Paid** pill on the card. Nothing is enforced; paid licensing is not built. | — |
| `homepage`, `support` | no | str | Links shown on the card. | — |
| `release_notes` | no | str | Informational. Notes are always read from `<id>/change_log/` ([Release checklist](release.md#release-note)). | — |
| `disable_notice` | no | str | Dotted path inside your package to a zero-argument function returning one sentence. The page shows it in a confirmation before **Turn off**. A path outside `<id>.` is ignored and logged. | page |

Also required in `hooks.py`: `app_name`, `app_title`, and `required_apps` as a list containing `"ffl_core"` (lint). Other Frappe metadata keys (`app_publisher`, `app_description`, `app_license`, `app_email`, `app_version`, `app_icon`, `app_color`, `app_logo_url`, `app_home`) are allowed. `app_description` is shown on the card.

### Permissions

| Value | Means | Derived by the lint from | Card |
|---|---|---|---|
| `doc_events:<DocType>` | Reacts when that DocType is saved | each key of `doc_events` | listed |
| `custom_fields:<DocType>` | Adds fields to that core DocType | each `dt` in `fixtures/custom_field.json` | listed |
| `pos_ui` / `desk_ui` | Buttons on the sale screen / Desk pages and form buttons | `app_include_js` needs one of them | listed |
| `scheduler` | Runs scheduled tasks | a non-empty `scheduler_events` | listed |
| `network:<host>` | Sends data to that host, one entry per host | every `https://<host>` string literal in non-test Python outside `hooks.py` and docstrings (local, `example.` and `test.` hosts ignored) | **red** |
| `portal` | Website or portal routes | `website_route_rules`, `portal_menu_items`, `website_redirects`, a `www/` folder | listed |
| `files` | Reads and writes `File` | — (declare it yourself) | listed |
| `compliance_read` | Reads the bound book | `frappe.get_doc` / `get_all` / `get_list` / `frappe.db.*` with a literal `"FFL Acquisition"` / `"FFL Disposition"` first argument | **red** |
| `public_endpoint` | Has `allow_guest=True` endpoints | `@frappe.whitelist(allow_guest=True)` | **red** |
| `woo_product_meta` | Adds values to website listings | — (declare it with `gunshop_woo_product_meta`) | listed |
| `first_party` | Maintainers only ([Rules](rules.md#first-party)) | — | **red** |

Anything else fails the lint as an unknown permission.

## Package layout

```
acme_layaway/                      # repository root = app root
├── acme_layaway/
│   ├── __init__.py                # __version__ = "1.2.0"
│   ├── hooks.py                   # manifest + hooks
│   ├── modules.txt                # one line: Acme Layaway
│   ├── patches.txt
│   ├── acme_layaway/              # the module package (Frappe imports it): DocType / Page / Report / Workspace
│   ├── api/                       # every whitelisted endpoint
│   ├── hooks_impl/                # doc_events / scheduler handlers
│   ├── setup/                     # idempotent after_install / before_uninstall
│   ├── fixtures/                  # Custom Field / Property Setter / Role / Workspace, with filters
│   ├── public/js/<id>.bundle.js   # registers with gunstore.extensions only
│   ├── public/css/                # prefixed selectors
│   ├── change_log/v1/v1_2_0.md    # release note
│   └── tests/                     # FrappeTestCase; tests/js/*.test.js
├── docs/api/                      # generated API reference, committed
├── README.md                      # For the counter / For the manager / For developers
├── pyproject.toml                 # [project].name == id; pure-Python dependencies only
├── wheels/                        # optional: dependencies as pure-Python wheels
└── LICENSE
```

The module package's folder is the title lowercased with spaces (and anything outside `[a-z0-9_]`) turned into `_`.

## Where it lives on a server

Extensions are not baked into the image.

- Code: the `apps-ext` volume, `frappe-bench/apps-ext/<id>/` (the app root).
- Declaration: `sites/extensions.json` (`{"schema": 1, "extensions": {"<id>": {"version", "source", "sha256", "installed_at", "installed_by"}}}`).
- Wiring, redone at every container start by `deploy/prod/ext-replay.sh`: an `apps/<id>` symlink, a `<id>.pth` in site-packages, a line in `sites/apps.txt`, and bundled wheels in `apps-ext/.deps/<id>`.
- No package metadata: never call `importlib.metadata.version()` on yourself; read `<id>.__version__`.
- Operator detail: [Extensions runbook](../extensions-runbook.md).
