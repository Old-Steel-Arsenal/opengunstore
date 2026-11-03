# Common lint errors and fixes

The lint prints one line per finding, then a summary, and exits 1 when there is any `ERROR`:

```
ERROR security      ext_hello/api/greet.py:1: import subprocess is not allowed
FAIL ext_hello 0.1.0 — 1 error(s), 0 warning(s)
```

Columns: level, rule, `path:line`, message. `WARN` findings are shown on the review card and do not block an upload. Messages below use `<…>` for the parts that vary.

## structure

| Message | Fix |
|---|---|
| `pyproject.toml is missing at the app root` | Put `pyproject.toml` next to the `<id>/` package; zip from that folder. |
| `no <id>/hooks.py package found next to pyproject.toml` | Exactly one folder beside `pyproject.toml` may hold both `hooks.py` and `__init__.py`. |
| `README.md is missing` (warn) | Add the three-section README ([Rules](rules.md#readme)). |
| `the package contains core app files (<app>); an extension never ships core code` | Remove the `frappe` / `erpnext` / `ffl_*` folder (or `apps/<core app>`). |
| `<path> must not be in the package` | Remove `.git`, `.env*`, `node_modules`, `setup.py` or `__pycache__`. Package with `git archive`. |
| `<path>: wheels are only allowed directly in wheels/ next to pyproject.toml` | Move the `.whl` into `wheels/` at the root. |
| `<path>: <suffix> files must not be in the package` | Remove `.pem`, `.key`, `.sql`, `.sql.gz`, `.so`, `.pyc`, `.env` files. |
| `<path> is a symlink` | Replace the link with the file. |
| `module package <id>/<module>/__init__.py is missing (frappe imports it for the module '<title>')` | Create the module package: the title lowercased, spaces as `_`, with an empty `__init__.py`. |

## compile

| Message | Fix |
|---|---|
| `syntax error: <msg>` | Fix the file. Nothing else is checked until every module compiles. |
| `not UTF-8` | Save the file as UTF-8. |

## identity and manifest

| Message | Fix |
|---|---|
| `id '<id>' must be <vendor>_<name>, lowercase, vendor 2–12 characters` | Rename the package (and every place the id appears) to match `^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]*$`. |
| `pyproject name '<x>' != app id '<id>'` | Set `[project].name` to the id. |
| `hooks.app_name '<x>' != package '<id>'` | Set `app_name` to the folder name. |
| `hooks.app_title missing or longer than 30 characters` | Shorten `app_title`. |
| `required_apps must be a list containing "ffl_core"` | `required_apps = ["ffl_core"]` (plus any you depend on). |
| `hooks.py must define gunshop_extension as a dict literal` | No function calls, variables or comprehensions inside the manifest. |
| `manifest id '<x>' != app id '<id>'` / `manifest title != app_title` | Make them equal. |
| `__init__.py must contain __version__ = "x.y.z"` | Double quotes, at the start of a line. |
| `manifest version '<x>' != __version__ '<y>'` | Bump both together. |
| `requires_core is required (PEP 440 range against ffl_core)` | Add it: `">=0.4.0,<1"`. |
| `requires_core '<x>' has no upper bound (e.g. ">=0.1.0,<1")` | Add `,<1` on the 0.x line. |
| `permissions must be a list` / `unknown permission '<x>'` | Use only the values in [Permissions](manifest.md#permissions); DocType names letters, digits and spaces; hosts lowercase. |
| `modules.txt must hold exactly one line equal to the title '<title>'` | One line, the exact `app_title`. |
| `fixtures/custom_field.json is not valid JSON` | Fix the file (re-export it). |

## first_party

| Message | Fix |
|---|---|
| `first_party is reserved for publisher_id "osa"` / `'<id>' is not a first-party extension this core knows` | Remove `first_party` from `permissions`; it is for the maintainers' listed extensions only. |

## hooks

| Message | Fix |
|---|---|
| `hook '<name>' is not on the whitelist` | Remove it, or use an allowed hook ([Rules](rules.md#allowed-frappe-hooks), [Hooks](hooks.md)). `gunshop_extension_updates` lands here: it is core-integration only. A module-level helper variable in `hooks.py` triggers it too: prefix it with `_`. |
| `hook '<name>' is reserved for first-party extensions` | `override_whitelisted_methods`, `auth_hooks` and the drop-ship hooks are not available to third parties. |
| `<hook>: <key> '<path>' must start with /ext/<id>/` | Move the route under `/ext/<id>/`. |
| `add_to_apps_screen entries must carry a has_permission that returns False` | Add `"has_permission": "<id>.….hide"` returning `False`. |
| `<hook> may only cover the extension's own DocTypes, not '<DocType>'` | Drop the core DocType from `has_permission` / `permission_query_conditions`. |

## permissions

| Message | Fix |
|---|---|
| `uses <permission> (<where>) but does not declare it` | Add the permission to the manifest. `<where>` names the hook, fixture or `file:line` that needs it. A `network:<host>` for a URL that is only an example: use an `example.` host. |
| `app_include_js needs pos_ui or desk_ui declared` | Declare `pos_ui` (sale screen) or `desk_ui` (Desk pages, form buttons). |

## security

| Message | Fix |
|---|---|
| `import <module> is not allowed` / `from <module> import … is not allowed` | No `subprocess` or `pickle`. |
| `<call>() is not allowed` | No `eval`, `exec`, `os.system`, `os.popen`, `frappe.utils.execute_in_shell`, `pickle.loads`. |
| `verify=False disables TLS checks` | Remove it; fix the certificate instead. |
| `string mentions '<s>' — extensions never touch site config or pip` | No `site_config.json`, `common_site_config` or `pip install` in strings. |

Test modules (`tests/`, `test_*.py`) are not scanned for security or permissions.

## gates

| Message | Fix |
|---|---|
| `endpoint is not gated: <dotted> (first stmt: …) (first statement must be require_enabled/only_for)` | Make `require_enabled(APP, roles=…)` the first statement after the docstring. Without `roles=` it does not count. |
| `endpoint <name> has no docstring (docs/api reads it)` (warn) | Add a summary, `Args:` and `Returns:` (the API-docs check fails without them). |

## release_notes

| Message | Fix |
|---|---|
| `no release note for <version> at <id>/change_log/v<major>/v<x_y_z>.md` (warn) | Add the note ([Release checklist](release.md#release-note)). |

## naming

| Message | Fix |
|---|---|
| `DocType '<name>' must start with the title prefix '<word>'` | Rename the DocType to start with the title's first word. |
| `custom field '<fieldname>' on <DocType> must start with <vendor>_` | Prefix the fieldname with your vendor. |

## deps

| Message | Fix |
|---|---|
| `pyproject.toml is not valid TOML: …` / `[project] must be a table` / `[project].dependencies must be a list of requirement strings` | Fix `pyproject.toml`. |
| `<wheel>: only pure-Python wheels (py3-none-any / py2.py3-none-any) can be bundled, not '<tag>'` | Use a pure-Python wheel or drop the dependency. |
| `<wheel> is not a valid wheel (…)` | Rebuild it with `pip wheel`. |
| `<wheel>: <name> is provided by core <version>, do not bundle` | Remove the wheel; use the core's copy. |
| `<wheel> installs a top-level '<name>': the core, this extension or another extension on this server already owns that name` | Drop the wheel or vendor it under your own package. |
| `<wheel>: <member> must not be in the package (<suffix> files)` | The wheel contains compiled code, keys or dumps: find a pure one. |
| `the package and its wheels unpack to more than 200 MB` | Trim the package. |
| `<where>: <spec> is not satisfied (<found>) — bundle a pure-Python wheel in wheels/ or use the core's version` | Bundle a matching wheel, or relax the requirement to the core's version. |
| `<where>: '<spec>' is not a valid requirement (…)` / `its marker cannot be evaluated (…)` | Fix the requirement string. |

## package (zips only)

| Message | Fix |
|---|---|
| `<reason> (encrypted, corrupt or unsafe archive)` | Larger than 50 MB, not a zip, over 20,000 entries, unpacks to more than 200 MB, an absolute or `..` path, a symlink, or the root is not the app root. Re-package with `git archive --format=zip HEAD`. |
