# Build an extension

An extension is a separate Frappe app that adds behavior to one store without a new core release. A store manager installs it from **Settings → Extensions** (a zip or a public Git tag), switches it on and off there, and the core's lint checks it before anything runs. Anyone can publish one; there is no marketplace gate.

The contract is these pages, the docstrings of `ffl_core.extensions`, and the lint (`make ext-lint`). `deploy/prod/tests/ext_hello` is a complete, passing example; `make ext-new` generates the same skeleton for your id.

| Page | For |
|---|---|
| [Quick start](quickstart.md) | A working extension, checked and packaged, in five steps. |
| [Manifest and layout](manifest.md) | Every `gunshop_extension` field and where each file goes. |
| [Lifecycle](lifecycle.md) | Discovery, the effective on/off state, install, upgrade, uninstall. |
| [Hooks and registry](hooks.md) | The SDK, every `gunshop_*` hook, the JavaScript registry. |
| [Rules](rules.md) | What an extension may and must not do, marked lint or review. |
| [Testing and CI](testing.md) | `make ext-test`, its exit codes, a GitHub Actions workflow. |
| [Release checklist](release.md) | Version, release note, tag, zip. |
| [Common lint errors](lint-errors.md) | Each lint message and its fix. |

Server-side operation (replay, the agent, rollback) is in the [Extensions runbook](../extensions-runbook.md).
