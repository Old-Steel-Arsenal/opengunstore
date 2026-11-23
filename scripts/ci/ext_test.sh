#!/usr/bin/env bash
# One extension through the checks the core CI runs for each first-party extension
# (the `extension_list` input of _app-tests.yml, run by the maintainers' deployment pipeline).
# `make ext-test` runs this
# same script inside the dev stack, so a local run and CI print the same steps.
#
#   scripts/ci/ext_test.sh <bench dir> <site> <extension id> <extension app root>
#   scripts/ci/ext_test.sh --check <bench dir> <extension id> <package dir> <extension app root>
#       (every refusal below for the package in <package dir>, which a run will find at <app root>;
#        touches nothing — `make ext-test` runs it on its unpacked copy before that replaces apps-ext/<id>)
#
# Refused (exit 2) before the bench is touched: an id that is not an extension id or is a
# core app (both as ffl_core.extensions.lint defines them, read through the bench's Python);
# a package without <id>/hooks.py; a package with wheels/ — bundled dependencies are installed
# by a store's ext-replay.sh, not here (yet); an apps/<id> this script did not make — a
# real directory, or a link to another folder that sites/apps.txt lists.
#
# Wires the extension into the bench the way deploy/prod/ext-replay.sh does — an
# apps/<id> symlink and a .pth, no package metadata — and
# installs it on <site>. Installed already (a re-run of `make ext-test`), it migrates
# the site instead: install-app would return at once, and the tests would run against
# the DocTypes, fields, fixtures and patches of the previous run. Then:
#   [1/3] bench --site <site> run-tests --app <id>     (a run that ran no test fails; not run, and failed, when
#         the extension is off on the site — ffl_core.extensions.effective, e.g. an unmet requires_core)
#   [2/3] python -m ffl_core.extensions.lint <root>
#   [3/3] scripts/generate_api_docs.py --app <root> --check   (endpoints documented; docs/api committed and fresh)
# All three run even after a failure. Exit 1 when the wiring, the tests, the lint or the API docs failed
# (parity, an endpoint without its docstring, Args or Returns, or Permissions prose contradicting a gate
# made of the extension's own roles: the generator's exit 1); 3 when the docs' only problem is one a core
# change alone makes — a stale docs/api, or Permissions prose naming other roles than a gate resolved
# through the core (the generator's exit 3: summary "docs STALE", the core CI only warns); 0 when all passed.
set -uo pipefail

if [ "${1:-}" = --check ] && [ $# -eq 5 ]; then
	mode=check
	BENCH=$(cd "$2" && pwd) || exit 2
	ID=$3
elif [ $# -eq 4 ]; then
	mode=run
	BENCH=$(cd "$1" && pwd) || exit 2
	SITE=$2
	ID=$3
else
	echo "usage: $0 <bench dir> <site> <extension id> <extension app root>" >&2
	echo "       $0 --check <bench dir> <extension id> <package dir> <extension app root>" >&2
	exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)
PY="$BENCH/env/bin/python"

verdict=$(cd "$BENCH" && "$PY" -c 'import sys
from ffl_core.extensions.lint import CORE_APPS, ID_RE
print("core" if sys.argv[1] in CORE_APPS else "ok" if ID_RE.fullmatch(sys.argv[1]) else "not")' "$ID") \
	|| { echo "ext-test: $PY cannot import ffl_core.extensions.lint (the id rule)" >&2; exit 2; }
case "$verdict" in
	ok) ;;
	core) echo "ext-test: '$ID' is a core app, not an extension" >&2; exit 2 ;;
	*) echo "ext-test: '$ID' is not an extension id (<vendor>_<name>)" >&2; exit 2 ;;
esac

if [ "$mode" = check ]; then
	PKG=$(cd "$4" && pwd) || exit 2
	ROOT=$5                                              # where the run will find the package (it may not exist yet)
else
	ROOT=$(cd "$4" && pwd) || exit 2
	PKG=$ROOT
fi
apps_txt="$BENCH/sites/apps.txt"
if [ ! -f "$PKG/$ID/hooks.py" ]; then
	echo "ext-test: $PKG/$ID/hooks.py not found — pass the extension's app root (pyproject.toml next to $ID/)" >&2
	exit 2
fi
if [ -e "$PKG/wheels" ]; then
	echo "ext-test: $PKG has wheels/ — make ext-test and the core CI do not install bundled wheels yet (a store's ext-replay.sh does), so an extension that ships them cannot be tested this way" >&2
	exit 2
fi
if [ -e "$BENCH/apps/$ID" ] && [ ! -L "$BENCH/apps/$ID" ]; then
	echo "ext-test: $BENCH/apps/$ID is a real directory, not a link this script made — leaving it alone" >&2
	exit 2
fi
if [ -L "$BENCH/apps/$ID" ] && [ "$(readlink "$BENCH/apps/$ID")" != "$ROOT" ] && grep -qxF "$ID" "$apps_txt"; then
	echo "ext-test: $BENCH/apps/$ID links to $(readlink "$BENCH/apps/$ID"), an app sites/apps.txt lists — not a link this script made; leaving it alone" >&2
	exit 2
fi
[ "$mode" = check ] && exit 0

echo "==> wire $ID into $BENCH ($ROOT)"
ln -sfn "$ROOT" "$BENCH/apps/$ID" || exit 1
purelib=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') || exit 1
printf '%s\n' "$BENCH/apps/$ID" > "$purelib/$ID.pth" || exit 1
if ! grep -qxF "$ID" "$apps_txt"; then
	[ -n "$(tail -c1 "$apps_txt")" ] && echo >> "$apps_txt"
	echo "$ID" >> "$apps_txt"
fi
# apps.txt just changed, but the site's cached module map (app → modules, in redis) may predate it — any
# bench command since the last clear-cache built it without $ID. Frappe before ~16.33 installs with that map
# as it is: install-app syncs NONE of $ID's DocTypes / Pages (no "Updating DocTypes for $ID"), and its
# tests then fail on missing tables and pages. Seen on a site fresh from bootstrap (CI's parallel extension
# job on a self-hosted runner, frappe 16.26); after the core suites the map had been cleared by them, which hid it.
(cd "$BENCH" && bench --site "$SITE" clear-cache) || { echo "ext-test: clear-cache failed" >&2; exit 1; }
installed=$(cd "$BENCH" && bench --site "$SITE" execute frappe.get_installed_apps) \
	|| { echo "ext-test: cannot read the apps installed on $SITE" >&2; exit 1; }
if grep -qF "\"$ID\"" <<<"$installed"; then
	echo "==> $ID is installed on $SITE already: bench --site $SITE migrate (brings in its changed DocTypes, fields, fixtures, patches)"
	(cd "$BENCH" && bench --site "$SITE" migrate) || { echo "ext-test: migrate failed" >&2; exit 1; }
else
	(cd "$BENCH" && bench --site "$SITE" install-app "$ID") || { echo "ext-test: install-app $ID failed" >&2; exit 1; }
fi

# Off on the site — most often a requires_core this core's version does not satisfy, e.g. an extension's main
# that already needs the next core release — every endpoint refuses "<title> is turned off for this store" and
# the tests fail by the dozen without naming why. Name it once and skip them.
row=$(cd "$BENCH" && bench --site "$SITE" execute frappe.db.get_value --kwargs \
	"{'doctype': 'POS Extension', 'filters': '$ID', 'fieldname': ['enabled', 'installed_on_site', 'compatible', 'incompatible_reason'], 'as_dict': 1}") \
	|| { echo "ext-test: cannot read the POS Extension row of $ID on $SITE" >&2; exit 1; }
off=$(printf '%s\n' "$row" | tail -n1 | (cd "$BENCH" && "$PY" -c 'import json, sys
from ffl_core.extensions import effective
text = sys.stdin.read().strip()
row = json.loads(text) if text.startswith("{") else None
if not row:
	print("it has no POS Extension row (ffl_core.extensions.registry.sync never described it)")
elif not effective(row):
	print(row.get("incompatible_reason") if not row.get("compatible") else
		"it is not installed on the site" if not row.get("installed_on_site") else "its switch is off")')) \
	|| { echo "ext-test: cannot read the POS Extension row of $ID on $SITE" >&2; exit 1; }

log=$(mktemp)
trap 'rm -f "$log"' EXIT

if [ -n "$off" ]; then
	echo "ext-test: $ID is off on $SITE — $off; its tests would only see \"turned off for this store\", so they are not run" >&2
	tests="FAIL (not run: $off)"
else
	echo "==> [1/3] tests: bench --site $SITE run-tests --app $ID"
	(cd "$BENCH" && bench --site "$SITE" run-tests --app "$ID") 2>&1 | tee "$log"
	status=${PIPESTATUS[0]}
	ran=0                                                # one "Ran N tests" line per test category frappe runs
	while read -r n; do ran=$((ran + n)); done < <(sed -nE 's/^Ran ([0-9]+) tests? in .*/\1/p' "$log")
	if [ "$status" -eq 0 ] && [ "$ran" -gt 0 ]; then tests="OK ($ran ran)"; else tests="FAIL (exit $status, $ran ran)"; fi
fi

echo "==> [2/3] lint: python -m ffl_core.extensions.lint $ROOT"
if (cd "$BENCH" && "$PY" -m ffl_core.extensions.lint "$ROOT"); then lint=OK; else lint=FAIL; fi

echo "==> [3/3] API docs: generate_api_docs.py --app $ROOT --check"
"$PY" "$HERE/../generate_api_docs.py" --app "$ROOT" --check
case $? in 0) docs=OK ;; 3) docs=STALE ;; *) docs=FAIL ;; esac

echo "ext-test $ID: tests $tests · lint $lint · docs $docs"
case "$tests $lint $docs" in *FAIL*) exit 1 ;; esac
[ "$docs" = OK ] || exit 3
