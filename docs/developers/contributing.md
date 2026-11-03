# Contributing and releases

The full rules are in [CONTRIBUTING.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/develop/CONTRIBUTING.md). In short:

| Branch | Role |
|---|---|
| `develop` | Default and integration branch. Cut `feature/*` / `fix/*` from it; pull requests go back into it. |
| `main` | Release branch, protected (pull request + CI). Only release pull requests from `develop`, and emergency hotfixes, land here. |

- A pull request into `develop` runs a light gate: API docs and the image build. Label `ci:full` runs the whole suite, `ci:upgrade` the upgrade regression (for a pull request that adds a patch). A draft runs nothing. Run `make test` locally before you open one.
- After changing an endpoint or a docstring, run `make api-docs` and commit `docs/api/`.
- Release prep, on `develop`: write `apps/ffl_core/ffl_core/change_log/v<major>/v<x>_<y>_<z>.md` in plain English for the counter, then `make bump-version VERSION=x.y.z` (it refuses without the note and regenerates `docs/api/`).
- Merging the release pull request into `main` tags `v<x.y.z>` automatically once tests and build pass. Never tag by hand.
- Security problems: report privately per [SECURITY.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/develop/SECURITY.md), not in a public issue.
