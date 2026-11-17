#!/bin/bash
# Extension layer replay — runs on every container start (called by entrypoint.sh).
#
# Extension apps live OUTSIDE the image, on the apps-ext volume
# (deploy/prod/compose.extensions.yaml), and are declared in sites/extensions.json:
#
#   {"schema": 1, "extensions": {"<id>": {"version": "0.1.0", "source": "upload",
#                                          "sha256": "…", "installed_at": "…", "installed_by": "…"}}}
#
# This script makes the running container match that manifest, idempotently:
#   apps/<id>                        -> symlink to apps-ext/<id> (bench, esbuild and frappe find apps here)
#   env/…/site-packages/<id>.pth     one line, the app dir — the same .pth the baked apps' editable
#                                    installs left behind, so `import <id>` works with no pip and no
#                                    network. (No dist-info: an extension must not rely on
#                                    importlib.metadata / pip list for itself.)
#   env/…/site-packages/zz-ext-deps-<id>.pth  one line, apps-ext/.deps/<id>: the extension's pure-Python
#                                    dependencies. It ships them as wheels/*.whl next to pyproject.toml
#                                    (the lint checks tags, contents and requirements); the regular files
#                                    among them are installed there with `pip --no-index --no-deps
#                                    --target`, once per wheels/ content (stamp inside the tree), and the
#                                    new tree replaces the old one by two renames — a replay stopped in
#                                    between leaves .tmp / .old, which the next one clears and reinstalls.
#                                    site.py reads .pth files in name order and "zz-ext-deps-" sorts after
#                                    the baked apps' erpnext.pth / ffl_core.pth / frappe.pth …, so the deps
#                                    come after every core app on sys.path. The line is skipped while the
#                                    directory does not exist: no wheels, no cost. Never the network.
#   sites/apps.txt                   baked apps + declared extensions. frappe_docker's configurator
#                                    rewrites the file from `ls apps` on every `up`, and frappe's
#                                    install_app / bench build require the id in it — so declared ids
#                                    are appended and leftovers of removed extensions (a line whose
#                                    apps/<x> is our own symlink into apps-ext, does not exist at all,
#                                    or is a dangling link) are dropped. Anything else is left alone.
#   apps-ext/<id>/<id>/public/dist   esbuild output, lives with the app on the volume. Compiled when the
#                                    stamp (manifest version | source hash | frappe | ffl_core versions,
#                                    kept in apps-ext/.built/<id>) changes or dist lacks the bundles the
#                                    sources declare; otherwise `bench build --using-cached` only
#                                    re-registers the files in this container's assets.json (≈3 s) and
#                                    clears the redis assets_json key.
#
# Ordering: the container-local wiring (symlink, .pth) is done before the shared
# lock, so `bench` commands exec'd into a freshly started container can import
# the extension while another container still compiles assets. /tmp/ext-replay.done
# marks the end of the whole replay for scripts that need the assets too — it is
# $BENCH/.ext-replay.done, container-local, re-armed on every start.
#
# Contract: any error exits non-zero WITH a message, and the container does NOT start. A site
# with the extension installed cannot run without its code anyway (hooks import fails on every
# request); failing loudly beats a half-dead POS. Recovery: restore the files, or uninstall the
# extension from every site first and then remove it from the manifest (docs/self-hosting.md#extensions) — deleting
# the manifest entry alone turns a restart loop into a 500 loop. Without a manifest this is a
# one-line no-op.
set -euo pipefail

BENCH="${BENCH_PATH:-/home/frappe/frappe-bench}"
# "This container's replay finished" — container-local (rw layer, owned by the
# bench user, never a volume), removed first thing so a restart re-arms it; the
# deploy scripts and docs/self-hosting.md#extensions wait on it before exec'ing bench.
DONE="$BENCH/.ext-replay.done"
rm -f "$DONE"
MANIFEST="$BENCH/sites/extensions.json"
EXT_DIR="$BENCH/apps-ext"
DEPS="$EXT_DIR/.deps"
APPS_TXT="$BENCH/sites/apps.txt"
PY="$BENCH/env/bin/python"
ID_RE='^[a-z][a-z0-9_]{1,63}$'

log() { echo "ext-replay: $*"; }
die() { echo "ext-replay: ERROR: $*" >&2; exit 1; }

if [ ! -f "$MANIFEST" ]; then
	log "no $MANIFEST — extension layer idle"
	: > "$DONE"
	exit 0
fi
[ -d "$EXT_DIR" ] || die "$EXT_DIR missing — the image ships it; is BENCH_PATH right?"
[ -w "$EXT_DIR" ] || die "$EXT_DIR is not writable by $(id -un) (uid $(id -u)) — chown the volume to 1000:1000"
[ -f "$APPS_TXT" ] || die "$APPS_TXT missing"

# Manifest -> "id<TAB>version" per extension, sorted. Malformed JSON or a bad id aborts
# the start: a wrong manifest gets fixed, not skipped.
parsed="$("$PY" - "$MANIFEST" <<'PY'
import json, re, sys
try:
	with open(sys.argv[1]) as f:
		data = json.load(f)
except ValueError as e:
	sys.exit(f"extensions.json is not valid JSON: {e}")
if not isinstance(data, dict) or data.get("schema") != 1:
	sys.exit("extensions.json: expected {\"schema\": 1, \"extensions\": {...}}")
exts = data.get("extensions") or {}
if not isinstance(exts, dict):
	sys.exit("extensions.json: 'extensions' must be an object keyed by extension id")
for ext_id in sorted(exts):
	if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", ext_id):
		sys.exit(f"extensions.json: invalid extension id {ext_id!r} (lowercase snake_case, 2-64 chars)")
	entry = exts[ext_id] if isinstance(exts[ext_id], dict) else {}
	version = str(entry.get("version") or "")
	if "\t" in version or "\n" in version:
		sys.exit(f"extensions.json: bad version for {ext_id!r}")
	print(f"{ext_id}\t{version}")
PY
)" || die "invalid $MANIFEST"

ids=""
declare -A MANIFEST_VERSION=()
while IFS=$'\t' read -r id version; do
	[ -n "$id" ] || continue
	ids="${ids:+$ids$'\n'}$id"
	MANIFEST_VERSION[$id]="$version"
done <<<"$parsed"
declared() { grep -qxF -- "$1" <<<"$ids"; }        # a fixed string: names come off the filesystem too

cd "$BENCH"
PURELIB="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
mkdir -p "$EXT_DIR/.built"

# __version__ = "x.y.z" from an app's __init__.py; anything else is an error, not an empty string.
# The same rule as ffl_core.extensions.lint._read_version (^__version__\s*=\s*"…"): a package the
# lint passes must replay.
py_version() {
	[ -f "$1" ] || die "$1 is missing"
	local v
	v="$(grep -m1 -E '^__version__[[:space:]]*=[[:space:]]*"[^"]+"' "$1" | sed -E 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')" || true
	[ -n "$v" ] || die "no __version__ = \"x.y.z\" line in $1"
	echo "$v"
}
# Content hash of an extension's sources (build output and caches excluded): any change to the
# code on the volume — not only a version bump — makes the next start rebuild the assets.
src_hash() {
	"$PY" - "$1" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
h = hashlib.sha256()
for d, dirs, files in os.walk(root):
	dirs[:] = sorted(x for x in dirs
		if x not in ("__pycache__", "node_modules", ".git")
		and not (x == "dist" and os.path.basename(d) == "public"))
	for f in sorted(files):
		if f.endswith((".pyc", ".pyo")):
			continue
		p = os.path.join(d, f)
		h.update(os.path.relpath(p, root).encode()); h.update(b"\0")
		with open(p, "rb") as fh:
			for chunk in iter(lambda: fh.read(1 << 16), b""):
				h.update(chunk)
		h.update(b"\0")
print(h.hexdigest()[:16])
PY
}
# The cached path is only valid when dist holds what the sources would build.
has_bundle_sources() { local d; for d in js css scss; do [ -d "$1/$d" ] && find "$1/$d" -maxdepth 1 -name '*.bundle.*' -print -quit | grep -q . && return 0; done; return 1; }
has_built_bundles()  { [ -d "$1" ] && find "$1" -name '*.bundle.*' -print -quit | grep -q .; }
dist_matches_sources() { if has_bundle_sources "$1"; then has_built_bundles "$1/dist"; else [ -d "$1/dist" ]; fi; }
# bench build <args>, judged by its output as well as its status: on an esbuild error a production
# build kills itself (non-zero) but a developer_mode build only prints the line below and exits 0 —
# a stamp written after that would make a broken bundle look built.
bench_build() {   # bench_build <id> [--using-cached]
	local out rc=0
	out="$(bench build --app "$@" 2>&1)" || rc=$?
	printf '%s\n' "$out"
	if [ "$rc" != 0 ] || grep -q 'There were some problems during build' <<<"$out"; then
		die "'$1': bench build reported errors (exit $rc) — fix the bundle on the volume (or reinstall the package) and restart"
	fi
}

# Two assignments: `X="$(a)|$(b)"` takes the status of the last substitution only, so a failing
# frappe half would print its ERROR and carry on with a half-empty stamp.
core_frappe="$(py_version apps/frappe/frappe/__init__.py)"
core_ffl="$(py_version apps/ffl_core/ffl_core/__init__.py)"
CORE="$core_frappe|$core_ffl"

put_line() {   # put_line <file> <line>: the file holds exactly that line, replaced atomically when it did not
	[ "$(cat "$1" 2>/dev/null)" = "$2" ] && return 0
	printf '%s\n' "$2" > "$1.tmp" && mv -f "$1.tmp" "$1"
}

# --- pass 1: every declared extension gets its code path ---------------------------
# Container-local (apps/<id> symlink, this env's .pth) and cheap, so it runs BEFORE
# the shared lock: a `docker compose exec … bench migrate` fired the moment the
# container is up must already find the module even while another container is
# still compiling assets under the lock (a deploy script hit exactly that race).
for id in $ids; do
	app="$EXT_DIR/$id"
	[ -f "$app/$id/hooks.py" ] || die "'$id' is in extensions.json but $app/$id/hooks.py is missing — restore the files or uninstall it (docs/self-hosting.md#extensions)"
	[ -f "$app/$id/__init__.py" ] || die "'$id': $app/$id/__init__.py is missing (every extension declares __version__ there)"
	if [ -e "apps/$id" ] && [ ! -L "apps/$id" ]; then
		die "'$id' collides with an app baked into the image"
	fi
	# A package that does not even compile would take the whole site down on the
	# first request (frappe imports every installed app's hooks). Refuse to wire it
	# and say which file — loud, before anything shared is touched.
	if ! compile_out="$("$PY" -m compileall -q "$app/$id" 2>&1)"; then
		die "'$id' does not compile: $(echo "$compile_out" | grep -m1 -E 'File|Error' | sed 's/^ *//') — fix the file on the volume (or reinstall the package) and restart"
	fi
	# Written only when different: a replay exec'd into a running container (the agent's in-place
	# install / upgrade) must not leave a moment without the link or with an empty .pth.
	[ "$(readlink "apps/$id" 2>/dev/null)" = "$app" ] || ln -sfn "$app" "apps/$id"
	put_line "$PURELIB/$id.pth" "$BENCH/apps/$id"
	put_line "$PURELIB/zz-ext-deps-$id.pth" "$DEPS/$id"
done

# One container at a time from here: sites/apps.txt and the compiled assets are shared.
exec 9>"$EXT_DIR/.lock"
if ! flock -n 9; then
	log "waiting for another replay to finish (apps-ext/.lock)"
	flock 9
fi

# --- reconcile sites/apps.txt (see the header): keep order, append missing ids -------
tmp="$(mktemp "$BENCH/sites/.apps.txt.XXXXXX")"   # same filesystem → the final mv is atomic
while IFS= read -r line || [ -n "$line" ]; do
	line="${line%$'\r'}"
	[ -n "$line" ] || continue
	if declared "$line"; then
		echo "$line" >> "$tmp"
	elif [[ "$line" =~ $ID_RE ]] && { [ ! -e "apps/$line" ] || { [ -L "apps/$line" ] && [[ "$(readlink "apps/$line")" == "$EXT_DIR/"* ]]; }; }; then
		# ! -e: no apps/<x> at all, or a dangling link — bench build would fail to import it
		log "dropping stale '$line' from sites/apps.txt (not declared in extensions.json)"
		[ -L "apps/$line" ] && rm -f "apps/$line"
		rm -f "$PURELIB/$line.pth"
	else
		echo "$line" >> "$tmp"          # baked app, or something this script did not create
	fi
done < "$APPS_TXT"
for id in $ids; do
	grep -qxF -- "$id" "$tmp" || echo "$id" >> "$tmp"
done
if cmp -s "$tmp" "$APPS_TXT"; then rm -f "$tmp"; else mv -f "$tmp" "$APPS_TXT"; fi   # never a half-written apps.txt

# --- deps: wheels/*.whl → apps-ext/.deps/<id> (see the header). Before pass 2: `bench build`
#     imports every app, and an app may import its dependencies at module level. ------------------
mkdir -p "$DEPS"
for d in "$DEPS"/*; do
	[ -e "$d" ] || continue
	name="${d##*/}"
	case "$name" in
		*.tmp|*.old) rm -rf "$d" ;;                  # an install interrupted mid-way; we hold the lock
		*) declared "$name" || { log "dropping stale deps of '$name' (not declared in extensions.json)"; rm -rf "$d"; } ;;
	esac
done
for pth in "$PURELIB"/zz-ext-deps-*.pth; do         # this container's lines for removed extensions
	[ -e "$pth" ] || continue
	name="${pth##*/zz-ext-deps-}"; name="${name%.pth}"
	if ! declared "$name" && [ "$(cat "$pth")" = "$DEPS/$name" ]; then rm -f "$pth"; fi
done
for pth in "$PURELIB"/*-deps.pth; do                 # our line under its first name, which sorted before the core apps
	[ -e "$pth" ] || continue
	name="$(basename "$pth" -deps.pth)"
	if [ "$(cat "$pth")" = "$DEPS/$name" ]; then rm -f "$pth"; fi
done
for id in $ids; do
	t0=$SECONDS
	target="$DEPS/$id"
	wheels=()
	for w in "$EXT_DIR/$id"/wheels/*.whl; do
		if [ -f "$w" ] && [ ! -L "$w" ]; then
			wheels+=("$w")
		elif [ -e "$w" ] || [ -L "$w" ]; then
			log "'$id': skipping wheels/${w##*/} — not a regular file"
		fi
	done
	if [ "${#wheels[@]}" = 0 ]; then
		if [ -e "$target" ]; then rm -rf "$target"; log "$id has no wheels/ — removed its deps"; fi
		continue
	fi
	stamp="$(src_hash "$EXT_DIR/$id/wheels")"
	if [ "$(cat "$target/.ext-deps-stamp" 2>/dev/null)" = "$stamp" ]; then
		log "$id deps cached"
		continue
	fi
	pip_out="$("$PY" -m pip install --no-index --no-deps --disable-pip-version-check --no-cache-dir --quiet --target "$target.tmp" "${wheels[@]}" 2>&1)" \
		|| die "'$id': installing its wheels/ failed: $(tail -n 3 <<<"$pip_out" | tr '\n' ' ')— reinstall a package that passes the lint (docs/self-hosting.md#extensions)"
	printf '%s\n' "$stamp" > "$target.tmp/.ext-deps-stamp"
	# Two renames, not one atomic swap: a replay stopped in between leaves no $target — the next one
	# clears .tmp / .old above and installs again.
	if [ -e "$target" ]; then mv "$target" "$target.old"; fi
	mv "$target.tmp" "$target"
	rm -rf "$target.old"
	log "$id deps installed [$stamp] ($((SECONDS - t0))s)"
done

# --- pass 2: assets. Needs pass 1 complete for ALL ids first: `bench build` imports every
#     app in apps.txt (make_asset_dirs) before compiling any of them. --------------------
for id in $ids; do
	t0=$SECONDS
	app="$EXT_DIR/$id"
	ext_version="$(py_version "$app/$id/__init__.py")"
	stamp="${MANIFEST_VERSION[$id]}|$(src_hash "$app")|$CORE"
	dist="$app/$id/public/dist"
	if [ "$(cat "$EXT_DIR/.built/$id" 2>/dev/null)" = "$stamp" ] && dist_matches_sources "$app/$id/public"; then
		bench_build "$id" --using-cached
		log "$id $ext_version [$stamp] cached ($((SECONDS - t0))s)"
	else
		rm -rf "$dist" "$EXT_DIR/.built/$id"
		bench_build "$id"
		mkdir -p "$dist"                             # an extension without JS still counts as built
		printf '%s\n' "$stamp" > "$EXT_DIR/.built/$id"
		log "$id $ext_version [$stamp] built ($((SECONDS - t0))s)"
	fi
done
log "done — $(wc -w <<<"$ids" | tr -d ' ') extension(s) linked"
: > "$DONE"
