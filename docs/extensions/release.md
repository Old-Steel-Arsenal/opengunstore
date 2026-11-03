# Release checklist

1. **Version, in two places, equal.** `__version__ = "x.y.z"` in `<id>/__init__.py` and `"version"` in the manifest (lint). Strictly higher than the version stores have (an equal or lower one is refused as an upgrade). Use `x.y.z`, or `x.y.z-beta.N` for a beta.
2. **Release note** (below) for exactly that version.
3. **`requires_core`** still true: on the 0.x line `>=<lowest 0.x you tested>,<1`. Run the tests against that lowest core and the current one.
4. **Permissions** cover any new behaviour; the review card shows the manager what was added and removed.
5. **Patches** in `patches.txt` are idempotent and lose no data.
6. **API docs** regenerated and committed: `python3 scripts/generate_api_docs.py --app <folder>`.
7. **`make ext-test`** (or your CI) exits 0.
8. **Commit, tag, package.**

    ```bash
    git commit -am "Acme Layaway 1.2.0"
    git tag v1.2.0                                    # v<version>
    git push origin main v1.2.0
    git archive --format=zip -o acme_layaway-1.2.0.zip v1.2.0
    ```

## Release note

`<id>/change_log/v<major>/v<version with dots as underscores>.md`: `change_log/v1/v1_2_0.md`; a beta is `v1_2_0-beta_1.md`. The lint warns when the current version has none. Frappe reads the folder itself; the manifest's `release_notes` key does not move it.

```markdown
### Version 1.2.0 — November 3, 2026

#### New
- **Layaway reminders**: the customer gets an email a week before a payment is due.

#### Fixed
- **Plan totals**: a plan with a trade-in shows the right balance.
```

`#### New` / `#### Improved` / `#### Fixed`, empty sections dropped; each item a bold feature name and one plain sentence for the counter, no code words. The upgrade card shows every note between the installed and the new version; the POS shows an effective extension's notes at login and under What's New in POS.

## How a store gets the new version

| Channel | What the manager does |
|---|---|
| Zip | **Settings → Extensions → Upload**, choose `acme_layaway-1.2.0.zip`. |
| Git | Same tab: `https://github.com/<you>/acme_layaway` and the tag `v1.2.0`. The repository must be public, over https, with `pyproject.toml` at the top. The clone is shallow at that ref; the card shows the address, the tag and the commit. A tag is better than a branch: the same tag always gives the same package. |
| Update source | **Update to {version}** on the card, checked daily and on Refresh. Only for extensions whose `publisher_id` matches the source's (the bundled Data Service source serves the maintainers' `osa` extensions only), and only for extensions installed by upload, Git or the source itself. The core downloads the package, refuses it when its sha256 differs from what the source announced or its `requires_core` excludes this core, and then treats it as an upload. |

Every channel goes through the same lint, review card and upgrade: backup, code swap, restart, `bench migrate`. The POS restarts, so upgrade outside business hours.
