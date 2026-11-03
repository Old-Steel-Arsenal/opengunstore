# Quick start

One command creates a working extension: a handler that honours the store's switch, two gated endpoints, a button on the sale screen, a release note, its tests and its API reference. A second command checks it.

**Before you start:** an OpenGunstore checkout with the dev stack running and the dev site created ([Set up a dev environment](../developers/dev-environment.md)), and a host `python3` (the scaffolder uses only the standard library). Run every command from the root of the core checkout unless a step says otherwise.

## 1. Create

```bash
make ext-new ID=acme_hello TITLE="Acme Hello" PUBLISHER="Acme Software LLC"
```

- `ID`: `<vendor>_<name>`, lowercase letters, digits and underscores, vendor 2–12 characters starting with a letter. It is the folder, the Python package and the extension id.
- `TITLE`: what staff read. Letters, digits, spaces and hyphens, at most 30 characters, starting with a letter.
- Output goes to `../acme_hello`; `OUT=<folder>` puts it elsewhere. A non-empty folder is never overwritten.

It prints `created acme_hello in ../acme_hello (<n> files)` and writes:

```
acme_hello/
├── acme_hello/
│   ├── __init__.py                     __version__ = "0.1.0"
│   ├── hooks.py                        the manifest and the hooks
│   ├── modules.txt                     one line: Acme Hello
│   ├── patches.txt
│   ├── acme_hello/__init__.py          the module package
│   ├── api/greet.py                    two gated, documented endpoints: greet (POST), ping (GET)
│   ├── hooks_impl/customer.py          a Customer after_insert handler in @extension_hook
│   ├── public/js/acme_hello.bundle.js  the Say hi button, registered with the POS
│   ├── change_log/v0/v0_1_0.md         the release note staff read
│   └── tests/test_greet.py             switched off, no role, repeat call
├── docs/api/                           the API reference, generated from the code
├── README.md                           For the counter / For the manager / For developers
├── pyproject.toml
├── LICENSE
└── .gitignore
```

The lint finds nothing in it and its tests pass. `deploy/prod/tests/ext_hello` is the same output for the id `ext_hello`.

## 2. Check

```bash
make ext-test ID=acme_hello DIR=../acme_hello
```

It copies the folder into the dev stack, installs it on the dev site (or migrates the site when it is installed already), then runs its tests, the lint and the API-docs check, and ends with:

```
ext-test acme_hello: tests OK (5 ran) · lint OK · docs OK
```

Exit codes and what each failure means: [Testing and CI](testing.md#make-ext-test).

!!! warning "Back up the dev site before the first run"
    `make ext-test` leaves the extension installed on `dev.localhost`. If your extension's tests commit records,
    those records stay, and the Extensions page refuses to uninstall an extension whose DocTypes hold records.
    Take `bench --site dev.localhost backup` first; restoring it is the reliable way back.
    The full removal steps are in [Testing and CI](testing.md#removing-it-from-the-dev-site).

## 3. Make it yours

- In `hooks.py`, fill in `homepage` and `support`, and declare a permission for everything the code does ([Manifest](manifest.md#permissions)). The lint names what is missing.
- Every endpoint opens with `require_enabled(APP, roles=…)` and has a docstring with `Args:` and `Returns:`. Every `doc_events` and `scheduler_events` handler is wrapped in `@extension_hook(APP)` ([Hooks and registry](hooks.md#the-python-sdk)).
- For every endpoint and hook you add, keep the three kinds of test: switched off, no role, repeat call writes once.
- After changing an endpoint or a DocType, regenerate and commit the API reference:

    ```bash
    python3 scripts/generate_api_docs.py --app ../acme_hello
    ```

- Run `make ext-test ID=acme_hello DIR=../acme_hello` again.

## 4. Package

```bash
cd ../acme_hello
git init && git add -A && git commit -m "Acme Hello 0.1.0"
git archive --format=zip -o acme_hello-0.1.0.zip HEAD
```

The zip root is the app root (`pyproject.toml` next to `acme_hello/`). `git archive` leaves out what `.gitignore` ignores. A public repository needs no zip: the manager can fetch `https://github.com/<you>/acme_hello` at tag `v0.1.0` instead.

## 5. Install

On **Settings → Extensions → Upload** the manager chooses the zip (or enters the Git address and tag), reads the review card and its permission chips, and types `acme_hello` to confirm. The code is staged, the POS restarts once, and the extension is installed and switched on ([Lifecycle](lifecycle.md)).

For the next version, follow the [Release checklist](release.md).
