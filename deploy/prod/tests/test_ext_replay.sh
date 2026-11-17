#!/usr/bin/env bash
# Extension-layer smoke test against a built image — no DB, no site, a few minutes.
#
#   deploy/prod/tests/test_ext_replay.sh ghcr.io/old-steel-arsenal/gunstore-pos:<tag>
#
# Runs in CI (release-image.yml, build job) on the image just built, and locally
# against any pulled tag. It always tests THIS checkout's ext-replay.sh and
# entrypoint (bind-mounted over the image's copies), so it also works on an image
# that predates the extension layer. Drives the fixture deploy/prod/tests/ext_hello
# through a throw-away docker volume and asserts the spec's AC3–AC7:
#   no manifest → no-op · declared → linked / importable / built · second start → cached ·
#   parallel starts serialise · version bump → rebuilt · missing dir or bad manifest → fails loudly
# and run 5a's: the 0.2.0 upgrade package (make_ext_hello_upgrade.py) with a bundled wheel →
#   deps installed offline / importable / after every core app on sys.path / cached / reinstalled
#   on change / healed after a stop between the two renames / only regular *.whl files handed to pip /
#   removed with the extension · rollback to .previous · dangling apps.txt links dropped, baked lines kept.
# Sentinels are echoed by the shell after the command they vouch for, never printed from
# python source (a traceback would echo the source line), and exit codes match whole lines.
set -euo pipefail
export COPYFILE_DISABLE=1          # macOS tar: no ._* AppleDouble files in the volume

IMAGE="${1:?usage: test_ext_replay.sh <image>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$PROD/../.." && pwd)"
BENCH=/home/frappe/frappe-bench
VOL="ext-smoke-$$"
# The image is linux/amd64 only; on an arm64 host docker needs to be told (Rosetta / qemu).
# EXT_TEST_PLATFORM= (empty) turns the flag off on an amd64 host.
PLATFORM="${EXT_TEST_PLATFORM---platform linux/amd64}"
# EXT_TEST_BAKED=1: test the scripts baked into the image instead of bind-mounting this
# checkout's copies over them (CI does that — the image IS this commit).
BAKED="${EXT_TEST_BAKED:-}"
TMP="$(mktemp -d)"

pass=0; fail=0
ok()   { pass=$((pass + 1)); echo "  ✔ $*"; }
bad()  { fail=$((fail + 1)); echo "  ✘ $*"; }
step() { echo; echo "== $*"; }

# in_image < script : run a bash script inside the image with the volume mounted. Every script
# starts the way a container does — in the bench, with the baked assets linked into sites/
# (what entrypoint.sh does before it calls the replay).
in_image() {
	local mounts=(-v "$VOL:$BENCH/apps-ext")
	if [ -z "$BAKED" ]; then
		mounts+=(-v "$PROD/ext-replay.sh:/usr/local/bin/ext-replay.sh:ro"
		         -v "$PROD/main-entrypoint.sh:/usr/local/bin/entrypoint.sh:ro"
		         -v "$PROD/ext-reload.sh:/usr/local/bin/ext-reload.sh:ro"
		         -v "$PROD/gunicorn.conf.py:/etc/gunicorn.conf.py:ro"
		         -v "$REPO/apps/ffl_core/ffl_core/extensions/lint.py:$BENCH/apps/ffl_core/ffl_core/extensions/lint.py:ro")
	fi
	# NET="--network none": the step must work with no network at all
	{ echo "cd $BENCH; ln -sfn $BENCH/assets sites/assets"; cat; } |
		docker run --rm -i $PLATFORM ${NET:-} "${mounts[@]}" --entrypoint bash "$IMAGE" -s 2>&1
}
# into_volume <dir> <parent-in-apps-ext> <shell before extract> : tar a host directory into the volume
into_volume() {
	tar -C "$(dirname "$1")" -c "$(basename "$1")" |
		docker run --rm -i $PLATFORM -v "$VOL:$BENCH/apps-ext" --entrypoint bash "$IMAGE" -c "mkdir -p $BENCH/apps-ext/$2 && cd $BENCH/apps-ext/$2 && $3 tar -x"
}
manifest() { printf '{"schema": 1, "extensions": {%s}}' "$1"; }

docker volume create "$VOL" >/dev/null
trap 'docker volume rm -f "$VOL" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
# An image without the extension layer has no apps-ext dir, so the mount point is
# created root-owned; make it writable for uid 1000, as the new image's own directory
# (and therefore a fresh production volume) already is.
[ -z "$BAKED" ] || echo "testing the scripts baked into $IMAGE (EXT_TEST_BAKED)"
docker run --rm $PLATFORM -u root -v "$VOL:$BENCH/apps-ext" --entrypoint bash "$IMAGE" \
	-c "chown 1000:1000 $BENCH/apps-ext"

sha256() { python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"; }
if [ -z "$BAKED" ]; then
	step "the ext-replay.sh under test is this checkout's copy (bind-mounted over the image's)"
	out="$(in_image <<<'sha256sum /usr/local/bin/ext-replay.sh')" || true
	[ "${out%% *}" = "$(sha256 "$PROD/ext-replay.sh")" ] && ok "sha256 in the container = $PROD/ext-replay.sh" || bad "container runs another script: $out"
fi

step "AC3 no manifest → idle no-op"
out="$(in_image <<EOF
cp sites/apps.txt /tmp/before; cp sites/assets/assets.json /tmp/assets.before
entrypoint.sh true && echo REPLAY_EXIT=\$?
test -f $BENCH/.ext-replay.done && echo IDLE_DONE_MARKER
cmp -s /tmp/before sites/apps.txt && echo APPS_TXT_UNCHANGED
cmp -s /tmp/assets.before sites/assets/assets.json && echo ASSETS_UNCHANGED
EOF
)" || true
grep -q 'extension layer idle' <<<"$out" && ok "reports idle" || bad "no idle line: $out"
grep -qx 'REPLAY_EXIT=0' <<<"$out" && ok "exit 0" || bad "non-zero exit"
grep -q 'IDLE_DONE_MARKER' <<<"$out" && ok "done marker on the idle path" || bad "no done marker without a manifest"
grep -q 'APPS_TXT_UNCHANGED' <<<"$out" && grep -q 'ASSETS_UNCHANGED' <<<"$out" && ok "apps.txt / assets.json untouched" || bad "files changed without a manifest"

step "load fixture ext_hello into the volume"
tar -C "$HERE" -c ext_hello | docker run --rm -i $PLATFORM -v "$VOL:$BENCH/apps-ext" --entrypoint bash "$IMAGE" -c "tar -C $BENCH/apps-ext -x"
ok "copied"

step "AC4 declared → symlink, .pth, apps.txt once, bundle built, assets.json key, importable · AC5 second run in the same container is cached and keeps one line"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.0", "source": "test"}')' > sites/extensions.json
ext-replay.sh && echo REPLAY_EXIT=\$?
[ -L apps/ext_hello ] && echo SYMLINK_OK
purelib=\$(env/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
[ "\$(cat \$purelib/ext_hello.pth)" = "$BENCH/apps/ext_hello" ] && echo PTH_OK
echo APPS_TXT_LINES=\$(grep -cx ext_hello sites/apps.txt)
ls sites/assets/ext_hello/dist/js/ext_hello.bundle.*.js >/dev/null 2>&1 && echo BUNDLE_OK
env/bin/python -c 'import json; a=json.load(open("sites/assets/assets.json")); v=a["ext_hello.bundle.js"]; assert v.startswith("/assets/ext_hello/dist/js/ext_hello.bundle."), v' && echo ASSETS_JSON_OK
v=\$(env/bin/python -c 'import ext_hello; print(ext_hello.__version__)') && echo "IMPORT_OK \$v"
# same container, nothing touched in between: still exactly one line
ext-replay.sh >/dev/null 2>&1 && echo TWICE_EXIT=\$?; echo APPS_TXT_LINES_TWICE=\$(grep -cx ext_hello sites/apps.txt)
# the configurator rewrites apps.txt from \`ls apps\` on every up; then a plain restart replays
ls -1 apps | grep -v ext_hello > sites/apps.txt
start=\$SECONDS; ext-replay.sh > /tmp/second.log 2>&1 && echo SECOND_EXIT=\$?; echo SECOND_SECONDS=\$((SECONDS-start))
grep -q ' cached ' /tmp/second.log && echo SECOND_CACHED
echo APPS_TXT_LINES2=\$(grep -cx ext_hello sites/apps.txt)
EOF
)" || true
for want in REPLAY_EXIT=0 SYMLINK_OK PTH_OK APPS_TXT_LINES=1 BUNDLE_OK ASSETS_JSON_OK "IMPORT_OK 0.1.0" TWICE_EXIT=0 APPS_TXT_LINES_TWICE=1 SECOND_EXIT=0 SECOND_CACHED APPS_TXT_LINES2=1; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want"
done
secs="$(sed -n 's/^SECOND_SECONDS=//p' <<<"$out")"
[ -n "$secs" ] && [ "$secs" -le 15 ] && ok "cached replay ${secs}s ≤ 15s" || bad "cached replay took ${secs:-?}s"
[ "$fail" -eq 0 ] || { echo "--- container output ---"; echo "$out"; }

step "AC5 fresh container, same volume → cached path (no compile) still registers the bundle"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.0"}')' > sites/extensions.json
ext-replay.sh 2>&1 | grep -E 'cached|built'
grep -q '"ext_hello.bundle.js"' sites/assets/assets.json && echo FRESH_ASSETS_OK
ls sites/assets/ext_hello/dist/js/ext_hello.bundle.*.js >/dev/null && echo FRESH_BUNDLE_OK
EOF
)" || true
grep -q ' cached ' <<<"$out" && ok "cached" || bad "not cached: $out"
grep -q FRESH_ASSETS_OK <<<"$out" && grep -q FRESH_BUNDLE_OK <<<"$out" && ok "assets registered from the volume" || bad "assets missing in a fresh container"

step "AC5 the lock is real: with apps-ext/.lock held elsewhere a replay waits (and says so); released, it finishes"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.0"}')' > sites/extensions.json
: > $BENCH/.ext-replay.done      # a stale marker from a previous run must be re-armed
exec 8>apps-ext/.lock; flock -w 60 8 || echo LOCK_NOT_TAKEN
( timeout 600 ext-replay.sh > /tmp/locked.log 2>&1; echo DONE >> /tmp/locked.log ) &   # wait below is bounded by this
sleep 6
grep -q DONE /tmp/locked.log && echo FINISHED_WHILE_LOCKED || echo BLOCKED_WHILE_LOCKED
grep -q 'waiting for another replay' /tmp/locked.log && echo WAIT_LOGGED
# container-local wiring must not wait for the lock (a migrate started meanwhile needs it)
[ -L apps/ext_hello ] && env/bin/python -c 'import ext_hello' && echo WIRED_WHILE_LOCKED
[ -f $BENCH/.ext-replay.done ] || echo NO_DONE_MARKER_YET
flock -u 8; wait
grep -q DONE /tmp/locked.log && echo FINISHED_AFTER_UNLOCK
echo PAR_LINES=\$(grep -cx ext_hello sites/apps.txt)
EOF
)" || true
for want in BLOCKED_WHILE_LOCKED WAIT_LOGGED WIRED_WHILE_LOCKED NO_DONE_MARKER_YET FINISHED_AFTER_UNLOCK PAR_LINES=1; do
	grep -qx "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done
grep -qx LOCK_NOT_TAKEN <<<"$out" && bad "the test could not take apps-ext/.lock itself" || true

step "AC6 version bump on the volume → rebuilt"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
sed -i 's/0.1.0/0.1.1/' apps-ext/ext_hello/ext_hello/__init__.py
ext-replay.sh 2>&1 | grep -E 'cached|built'
cat apps-ext/.built/ext_hello
EOF
)" || true
grep -q ' built ' <<<"$out" && ok "rebuilt" || bad "not rebuilt: $out"
grep -q '^0.1.1|' <<<"$out" && ok "stamp updated" || bad "stamp not updated: $out"

step "AC6 source edited in place, versions untouched → rebuilt and the new code is what dist holds"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
echo 'window.ext_hello_marker = "NEWCODE_7f3a";' >> apps-ext/ext_hello/ext_hello/public/js/ext_hello.bundle.js
ext-replay.sh 2>&1 | grep -E 'cached|built'
grep -l NEWCODE_7f3a apps-ext/ext_hello/ext_hello/public/dist/js/ext_hello.bundle.*.js >/dev/null && echo NEW_CODE_IN_DIST
EOF
)" || true
grep -q ' built ' <<<"$out" && ok "rebuilt on a source change" || bad "not rebuilt: $out"
grep -q NEW_CODE_IN_DIST <<<"$out" && ok "dist holds the new code" || bad "dist is stale"

step "AC7 __version__ not in the canonical double-quoted form → non-zero WITH a message (never a silent empty stamp)"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
cp apps-ext/ext_hello/ext_hello/__init__.py /tmp/init.bak
printf "__version__ = '0.2.0'\\n" > apps-ext/ext_hello/ext_hello/__init__.py
ext-replay.sh; echo SQ_EXIT=\$?
cp /tmp/init.bak apps-ext/ext_hello/ext_hello/__init__.py
# pass 1 compiled the single-quoted file; restored in the same second with the same size, its pyc
# would still look current (mtime granularity) and later imports would see '0.2.0'
rm -rf apps-ext/ext_hello/ext_hello/__pycache__
EOF
)" || true
grep -qx 'SQ_EXIT=1' <<<"$out" && ok "exit 1" || bad "exit was not 1: $out"
grep -q 'ERROR.*no __version__' <<<"$out" && ok "names the missing __version__ line" || bad "no message: $out"

step "AC6 dist emptied but stamp unchanged → rebuilt (the cached path never registers nothing)"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
rm -rf apps-ext/ext_hello/ext_hello/public/dist/*
ext-replay.sh 2>&1 | grep -E 'cached|built'
grep -q '"ext_hello.bundle.js"' sites/assets/assets.json && echo KEY_PRESENT
EOF
)" || true
grep -q ' built ' <<<"$out" && ok "rebuilt" || bad "not rebuilt: $out"
grep -q KEY_PRESENT <<<"$out" && ok "assets.json key present" || bad "key missing"

step "AC7 manifest names an extension whose files are missing → non-zero, names it"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}, "ext_gone": {"version": "1.0.0"}')' > sites/extensions.json
ext-replay.sh; echo REPLAY_EXIT=\$?
EOF
)" || true
grep -qx 'REPLAY_EXIT=1' <<<"$out" && ok "exit 1" || bad "did not fail: $out"
grep -q "ext_gone" <<<"$out" && ok "names ext_gone" || bad "message does not name the extension"

step "AC9 (run 4) a syntax error anywhere in the package → non-zero, names the file, nothing wired"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
cp apps-ext/ext_hello/ext_hello/api/greet.py /tmp/greet.bak
printf 'def (\\n' >> apps-ext/ext_hello/ext_hello/api/greet.py
rm -f apps/ext_hello
ext-replay.sh; echo COMPILE_EXIT=\$?
test -L apps/ext_hello && echo LINKED_ANYWAY
cp /tmp/greet.bak apps-ext/ext_hello/ext_hello/api/greet.py
ext-replay.sh >/dev/null && echo FIXED_REPLAY_OK
EOF
)" || true
grep -qx 'COMPILE_EXIT=1' <<<"$out" && ok "bad syntax → exit 1" || bad "bad syntax did not fail: $out"
grep -q "does not compile" <<<"$out" && grep -q "greet.py" <<<"$out" && ok "message names the file" || bad "no file named: $out"
grep -q 'LINKED_ANYWAY' <<<"$out" && bad "wired a package that does not compile" || ok "not wired"
grep -q 'FIXED_REPLAY_OK' <<<"$out" && ok "fixed file → replay succeeds again" || bad "replay still failing after the fix: $out"

step "run 5a: a bundle esbuild cannot compile → non-zero naming the extension, no built stamp, no done marker — in a production build (esbuild kills itself) and a developer_mode build (bench build exits 0, only prints the error); fixed → built"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
cp apps-ext/ext_hello/ext_hello/public/js/ext_hello.bundle.js /tmp/bundle.bak
printf 'const = ;\n' >> apps-ext/ext_hello/ext_hello/public/js/ext_hello.bundle.js
ext-replay.sh > /tmp/badjs.log 2>&1; echo BADJS_EXIT=\$?
grep "ERROR: 'ext_hello'" /tmp/badjs.log
[ -e apps-ext/.built/ext_hello ] || echo NO_BUILT_STAMP
env/bin/python -c 'import json, os; p = "sites/common_site_config.json"; c = json.load(open(p)) if os.path.exists(p) else {}; c["developer_mode"] = 1; json.dump(c, open(p, "w"))'
ext-replay.sh > /tmp/badjs-dev.log 2>&1; echo BADJS_DEV_EXIT=\$?
grep -q 'There were some problems during build' /tmp/badjs-dev.log && echo DEV_BUILD_PRINTED_THE_ERROR
grep "ERROR: 'ext_hello'" /tmp/badjs-dev.log
[ -e apps-ext/.built/ext_hello ] || echo NO_BUILT_STAMP_DEV
test -f $BENCH/.ext-replay.done || echo BADJS_NO_DONE_MARKER
cp /tmp/bundle.bak apps-ext/ext_hello/ext_hello/public/js/ext_hello.bundle.js
ext-replay.sh > /tmp/fixedjs.log 2>&1; echo FIXEDJS_EXIT=\$?
grep -q '^ext-replay: ext_hello 0.1.1 .* built ' /tmp/fixedjs.log && echo FIXEDJS_BUILT
EOF
)" || true
for want in BADJS_EXIT=1 NO_BUILT_STAMP BADJS_DEV_EXIT=1 DEV_BUILD_PRINTED_THE_ERROR NO_BUILT_STAMP_DEV BADJS_NO_DONE_MARKER FIXEDJS_EXIT=0 FIXEDJS_BUILT; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done
[ "$(grep -c "ERROR: 'ext_hello': bench build reported errors" <<<"$out")" = 2 ] && ok "both builds: message names the extension and the build" || bad "messages: $out"

step "run 5a: lint and replay read __version__ by the same rule — a tab-separated line passes both"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
cp apps-ext/ext_hello/ext_hello/__init__.py /tmp/init.bak
printf '__version__\t=\t"0.1.1"\n' > apps-ext/ext_hello/ext_hello/__init__.py
v=\$(env/bin/python -c 'import sys; from pathlib import Path; from ffl_core.extensions.lint import _read_version; print(_read_version(Path(sys.argv[1])))' apps-ext/ext_hello/ext_hello/__init__.py) && echo "LINT_VERSION \$v"
ext-replay.sh > /tmp/tab.log 2>&1; echo TAB_EXIT=\$?
grep 'ERROR' /tmp/tab.log
grep -q '^ext-replay: ext_hello 0.1.1 ' /tmp/tab.log && echo "REPLAY_VERSION 0.1.1"
cp /tmp/init.bak apps-ext/ext_hello/ext_hello/__init__.py
rm -rf apps-ext/ext_hello/ext_hello/__pycache__        # same size, maybe the same second: no stale pyc for later steps
EOF
)" || true
for want in "LINT_VERSION 0.1.1" TAB_EXIT=0 "REPLAY_VERSION 0.1.1"; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "AC7 hooks.py present but __init__.py missing → non-zero WITH a message naming the file"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
mv apps-ext/ext_hello/ext_hello/__init__.py /tmp/init.bak
ext-replay.sh; echo NOINIT_EXIT=\$?
mv /tmp/init.bak apps-ext/ext_hello/ext_hello/__init__.py
EOF
)" || true
grep -qx 'NOINIT_EXIT=1' <<<"$out" && ok "exit 1" || bad "exit was not 1: $out"
grep -q 'ERROR.*__init__.py is missing' <<<"$out" && ok "names __init__.py" || bad "no message: $out"

step "AC7 bad id / malformed JSON → non-zero; the entrypoint stops the container"
out="$(in_image <<EOF
echo '{"schema": 1, "extensions": {"../x": {}}}' > sites/extensions.json
ext-replay.sh; echo BADID_EXIT=\$?
echo 'not json' > sites/extensions.json
ext-replay.sh; echo BADJSON_EXIT=\$?
entrypoint.sh true; echo ENTRY_EXIT=\$?
EOF
)" || true
grep -qx 'BADID_EXIT=1' <<<"$out" && ok "bad id rejected" || bad "bad id accepted: $out"
grep -qx 'BADJSON_EXIT=1' <<<"$out" && ok "bad JSON rejected" || bad "bad JSON accepted"
grep -qx 'ENTRY_EXIT=1' <<<"$out" && ok "entrypoint propagates the failure" || bad "entrypoint ignored the replay failure"

step "AC4 entrypoint path: valid manifest → the container starts with the extension linked"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
entrypoint.sh bash -c 'echo ENTRY_OK; ls sites/assets/ext_hello/dist/js/ext_hello.bundle.*.js >/dev/null && echo ENTRY_BUNDLE_OK; env/bin/python -c "import ext_hello" && echo ENTRY_IMPORT_OK; test -f $BENCH/.ext-replay.done && echo ENTRY_DONE_MARKER'
EOF
)" || true
for want in ENTRY_OK ENTRY_BUNDLE_OK ENTRY_IMPORT_OK ENTRY_DONE_MARKER; do
	grep -qx "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "AC7 (run 5a) frappe's __version__ unreadable → non-zero naming the file, no done marker (both halves of the CORE stamp stop the start)"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
sed -i 's/^__version__/# __version__/' apps/frappe/frappe/__init__.py
ext-replay.sh; echo CORE_EXIT=\$?
test -f $BENCH/.ext-replay.done && echo CORE_DONE_MARKER
EOF
)" || true
grep -qx 'CORE_EXIT=1' <<<"$out" && ok "exit 1" || bad "exit was not 1: $out"
grep -q 'ERROR: no __version__ = "x.y.z" line in apps/frappe/frappe/__init__.py' <<<"$out" && ok "names frappe's __init__.py" || bad "no message: $out"
grep -qx CORE_DONE_MARKER <<<"$out" && bad "done marker written after a failed replay" || ok "no done marker"

step "reconcile (run 5a): a dangling apps/<x> link's line is dropped, every baked line survives, a listed declared id stays exactly once"
out="$(in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
cp sites/apps.txt /tmp/baked                          # the lines baked into the image, before any replay
echo ext_hello >> sites/apps.txt                      # already listed: a replay must not append it again
ln -s /nonexistent/ext_dangling apps/ext_dangling; echo ext_dangling >> sites/apps.txt
ext-replay.sh > /tmp/rec.log 2>&1; echo RECON_EXIT=\$?
grep -q "dropping stale 'ext_dangling'" /tmp/rec.log && echo DANGLING_LOGGED
echo DANGLING_LINES=\$(grep -cx ext_dangling sites/apps.txt)
[ -e apps/ext_dangling ] || [ -L apps/ext_dangling ] || echo DANGLING_LINK_GONE
echo HELLO_LINES=\$(grep -cx ext_hello sites/apps.txt)
missing=0; while read -r a; do grep -qx "\$a" sites/apps.txt || { echo "BAKED_MISSING \$a"; missing=1; }; done < /tmp/baked
[ -s /tmp/baked ] && [ \$missing = 0 ] && echo BAKED_LINES_KEPT
EOF
)" || true
for want in RECON_EXIT=0 DANGLING_LOGGED DANGLING_LINES=0 DANGLING_LINK_GONE HELLO_LINES=1 BAKED_LINES_KEPT; do
	grep -qx "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "run 5a: every pip install in the replay under test is --no-index"
script="$PROD/ext-replay.sh"; [ -z "$BAKED" ] || { docker run --rm $PLATFORM --entrypoint cat "$IMAGE" /usr/local/bin/ext-replay.sh > "$TMP/baked-replay.sh"; script="$TMP/baked-replay.sh"; }
pips="$(grep -E '^[^#]*-m pip install' "$script" || true)"
[ -n "$pips" ] && ! grep -v -- '--no-index' <<<"$pips" >/dev/null && ok "$(wc -l <<<"$pips" | tr -d ' ') pip install line(s), all --no-index" || bad "pip lines: ${pips:-none}"

# The 0.2.0 upgrade package (bundled wheel ext_hello_dep 1.0.0) and a 1.0.1 wheel, built here with zipfile.
python3 "$HERE/make_ext_hello_upgrade.py" "$TMP/ext_hello-0.2.0.zip" >/dev/null
python3 -m zipfile -e "$TMP/ext_hello-0.2.0.zip" "$TMP/upgrade/ext_hello"
mkdir -p "$TMP/wheels"
python3 -c 'import runpy, sys; runpy.run_path(sys.argv[1])["build_wheel"](sys.argv[2], "ext_hello_dep", "1.0.1", {"ext_hello_dep/__init__.py": "__version__ = \"1.0.1\"\n"})' "$HERE/make_ext_hello_upgrade.py" "$TMP/wheels"
# what stage_upgrade does on the volume: the installed tree to .previous/<id>, the new tree in its place
into_volume "$TMP/upgrade/ext_hello" . "rm -rf .previous/ext_hello && mkdir -p .previous && mv ext_hello .previous/ext_hello &&"

step "AC10 upgrade package with wheels/ → pip --no-index into apps-ext/.deps/ext_hello, importable, after every core app on sys.path — with no network at all"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
[ "\$(tail -n +2 /proc/net/route | wc -l)" = 0 ] && echo NO_NETWORK     # no route at all: --network none took
purelib=\$(env/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$BENCH/apps-ext/.deps/ext_hello" > \$purelib/ext_hello-deps.pth    # the line under its first name (sorted before ffl_core.pth)
ext-replay.sh > /tmp/deps.log 2>&1; echo DEPS_EXIT=\$?
grep -E 'deps|ERROR' /tmp/deps.log
grep -q '^ext-replay: ext_hello deps installed' /tmp/deps.log && echo DEPS_INSTALLED
[ "\$(cat \$purelib/zz-ext-deps-ext_hello.pth)" = "$BENCH/apps-ext/.deps/ext_hello" ] && echo DEPS_PTH_OK
[ -e \$purelib/ext_hello-deps.pth ] || echo FIRST_NAME_PTH_GONE
test -f apps-ext/.deps/ext_hello/ext_hello_dep/__init__.py && echo DEPS_TREE_OK
env/bin/python -c 'import ext_hello_dep' && echo DEP_IMPORT_OK
env/bin/python -c 'import sys; p = [x.rstrip("/") for x in sys.path]; deps = p.index("$BENCH/apps-ext/.deps/ext_hello"); core = [p.index("$BENCH/apps/" + a) for a in ("frappe", "erpnext", "ffl_core", "ffl_integrations", "ffl_woo_sync")]; assert deps > max(core), (deps, core, p)' && echo DEPS_AFTER_CORE
v=\$(env/bin/python -c 'import ext_hello; print(ext_hello.__version__)') && echo "EXT_VERSION \$v"
EOF
)" || true
for want in NO_NETWORK DEPS_EXIT=0 DEPS_INSTALLED DEPS_PTH_OK FIRST_NAME_PTH_GONE DEPS_TREE_OK DEP_IMPORT_OK DEPS_AFTER_CORE "EXT_VERSION 0.2.0"; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done
grep '^ext-replay: .*deps' <<<"$out" | sed 's/^/    /' || true

step "AC10/AC12 fresh container (container layer gone), wheels unchanged → deps cached, pip not run, still importable"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
before=\$(stat -c '%i %Y' apps-ext/.deps/ext_hello apps-ext/.deps/ext_hello/.ext-deps-stamp | tr '\n' ' ')
env/bin/python -c 'import ext_hello_dep' 2>/dev/null || echo NOT_IMPORTABLE_BEFORE_REPLAY
ext-replay.sh > /tmp/cached.log 2>&1; echo CACHED_EXIT=\$?
grep -E 'deps|cached|built' /tmp/cached.log
grep -q '^ext-replay: ext_hello deps cached' /tmp/cached.log && echo DEPS_CACHED
grep -q 'deps installed' /tmp/cached.log || echo NO_PIP_RUN
[ "\$before" = "\$(stat -c '%i %Y' apps-ext/.deps/ext_hello apps-ext/.deps/ext_hello/.ext-deps-stamp | tr '\n' ' ')" ] && echo SAME_TREE
env/bin/python -c 'import ext_hello_dep' && echo FRESH_IMPORT_OK
EOF
)" || true
for want in NOT_IMPORTABLE_BEFORE_REPLAY CACHED_EXIT=0 DEPS_CACHED NO_PIP_RUN SAME_TREE FRESH_IMPORT_OK; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done
grep '^ext-replay: ' <<<"$out" | sed 's/^/    /' || true

step "AC12 wheels changed (1.0.0 → 1.0.1) → reinstalled, the new tree replaces the old one; an interrupted install's leftovers go"
into_volume "$TMP/wheels" ext_hello "rm -rf wheels &&"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
mkdir -p apps-ext/.deps/ext_hello.tmp && touch apps-ext/.deps/ext_hello.tmp/half-written
ext-replay.sh > /tmp/changed.log 2>&1; echo CHANGED_EXIT=\$?
grep -q '^ext-replay: ext_hello deps installed' /tmp/changed.log && echo DEPS_REINSTALLED
v=\$(env/bin/python -c 'import ext_hello_dep; print(ext_hello_dep.__version__)') && echo "DEP_VERSION \$v"
[ -e apps-ext/.deps/ext_hello.tmp ] || [ -e apps-ext/.deps/ext_hello.old ] || echo NO_LEFTOVERS
EOF
)" || true
for want in CHANGED_EXIT=0 DEPS_REINSTALLED "DEP_VERSION 1.0.1" NO_LEFTOVERS; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "run 5a r1: a *.whl that is a directory (the lint refuses it) is never handed to pip — pip would build it as a project"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
mkdir -p apps-ext/ext_hello/wheels/sneaky-1.0-py3-none-any.whl
printf '[project]\\nname = "sneaky"\\nversion = "1.0"\\n' > apps-ext/ext_hello/wheels/sneaky-1.0-py3-none-any.whl/pyproject.toml
ext-replay.sh > /tmp/dirwhl.log 2>&1; echo DIRWHL_EXIT=\$?
grep -E 'ERROR|skipping' /tmp/dirwhl.log
grep -q "^ext-replay: 'ext_hello': skipping wheels/sneaky-1.0-py3-none-any.whl — not a regular file" /tmp/dirwhl.log && echo DIRWHL_SKIPPED
grep -q '^ext-replay: ext_hello deps installed' /tmp/dirwhl.log && echo DIRWHL_REAL_WHEELS_INSTALLED
v=\$(env/bin/python -c 'import ext_hello_dep; print(ext_hello_dep.__version__)') && echo "DIRWHL_DEP \$v"
rm -rf apps-ext/ext_hello/wheels/sneaky-1.0-py3-none-any.whl
ext-replay.sh >/dev/null 2>&1 && echo DIRWHL_CLEANED      # back to the plain wheels/ stamp for the next steps
EOF
)" || true
for want in DIRWHL_EXIT=0 DIRWHL_SKIPPED DIRWHL_REAL_WHEELS_INSTALLED "DIRWHL_DEP 1.0.1" DIRWHL_CLEANED; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "run 5a r1: a replay stopped between the two renames (no .deps/ext_hello; .tmp and .old left) → the next one heals it with a fresh tree, never the leftover"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
mv apps-ext/.deps/ext_hello apps-ext/.deps/ext_hello.tmp          # the new tree, stamp inside, not renamed into place yet
touch apps-ext/.deps/ext_hello.tmp/leftover-marker                # pip --target into a leftover skips what exists: it must never be promoted
cp -r apps-ext/.deps/ext_hello.tmp apps-ext/.deps/ext_hello.old   # the old tree, already renamed away
[ -e apps-ext/.deps/ext_hello ] || echo HEAL_NO_TARGET_BEFORE
ext-replay.sh > /tmp/heal.log 2>&1; echo HEAL_EXIT=\$?
grep -q '^ext-replay: ext_hello deps installed' /tmp/heal.log && echo HEAL_REINSTALLED
env/bin/python -c 'import ext_hello_dep' && echo HEAL_IMPORT_OK
[ -e apps-ext/.deps/ext_hello.tmp ] || [ -e apps-ext/.deps/ext_hello.old ] || echo HEAL_NO_LEFTOVERS
[ -e apps-ext/.deps/ext_hello/leftover-marker ] || echo HEAL_FRESH_TREE
EOF
)" || true
for want in HEAL_NO_TARGET_BEFORE HEAL_EXIT=0 HEAL_REINSTALLED HEAL_IMPORT_OK HEAL_NO_LEFTOVERS HEAL_FRESH_TREE; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "run 5a r1: deps a regex would call declared ('ext.hello' vs ext_hello) are stale — dir and .pth dropped, ext_hello's own kept"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
purelib=\$(env/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
mkdir -p apps-ext/.deps/ext.hello && touch apps-ext/.deps/ext.hello/stale.py
echo "$BENCH/apps-ext/.deps/ext.hello" > \$purelib/zz-ext-deps-ext.hello.pth
ext-replay.sh > /tmp/dot.log 2>&1; echo DOT_EXIT=\$?
grep -q "dropping stale deps of 'ext.hello'" /tmp/dot.log && echo DOT_LOGGED
[ -e apps-ext/.deps/ext.hello ] || echo DOT_DEPS_GONE
[ -e \$purelib/zz-ext-deps-ext.hello.pth ] || echo DOT_PTH_GONE
test -f apps-ext/.deps/ext_hello/ext_hello_dep/__init__.py && [ -f \$purelib/zz-ext-deps-ext_hello.pth ] && echo OWN_DEPS_KEPT
EOF
)" || true
for want in DOT_EXIT=0 DOT_LOGGED DOT_DEPS_GONE DOT_PTH_GONE OWN_DEPS_KEPT; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "a wheel pip cannot install → non-zero naming the extension, the installed deps untouched; fixed → cached again"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
cp -r apps-ext/ext_hello/wheels /tmp/wheels.bak
printf 'not a wheel' > apps-ext/ext_hello/wheels/broken-1.0-py3-none-any.whl
ext-replay.sh > /tmp/broken.log 2>&1; echo PIPFAIL_EXIT=\$?
cat /tmp/broken.log | grep ERROR
test -f $BENCH/.ext-replay.done || echo PIPFAIL_NO_DONE_MARKER
v=\$(env/bin/python -c 'import ext_hello_dep; print(ext_hello_dep.__version__)') && echo "DEP_STILL \$v"
rm -rf apps-ext/ext_hello/wheels && cp -r /tmp/wheels.bak apps-ext/ext_hello/wheels
ext-replay.sh > /tmp/fixed.log 2>&1; echo FIXED_EXIT=\$?
grep -q '^ext-replay: ext_hello deps cached' /tmp/fixed.log && echo FIXED_CACHED
EOF
)" || true
for want in PIPFAIL_EXIT=1 PIPFAIL_NO_DONE_MARKER "DEP_STILL 1.0.1" FIXED_EXIT=0 FIXED_CACHED; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done
grep -q "ERROR: 'ext_hello': installing its wheels/ failed" <<<"$out" && ok "message names the extension" || bad "no message: $out"

step "AC12 extension removed from the manifest → its .deps dir and deps .pth are gone; foreign *-deps.pth / zz-ext-deps-*.pth lines survive"
out="$(NET="--network none" in_image <<EOF
purelib=\$(env/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
ext-replay.sh >/dev/null 2>&1 && test -f \$purelib/zz-ext-deps-ext_hello.pth && echo DECLARED_PTH_WRITTEN
echo '/opt/elsewhere' > \$purelib/vendor-deps.pth
echo '/opt/elsewhere' > \$purelib/zz-ext-deps-vendor.pth
echo '{"schema": 1, "extensions": {}}' > sites/extensions.json
ext-replay.sh > /tmp/removed.log 2>&1; echo REMOVED_EXIT=\$?
grep -q "dropping stale deps of 'ext_hello'" /tmp/removed.log && echo REMOVAL_LOGGED
[ -e apps-ext/.deps/ext_hello ] || echo DEPS_DIR_GONE
[ -e \$purelib/zz-ext-deps-ext_hello.pth ] || echo DEPS_PTH_GONE
[ -e \$purelib/vendor-deps.pth ] && [ -e \$purelib/zz-ext-deps-vendor.pth ] && echo FOREIGN_PTH_KEPT
env/bin/python -c 'import ext_hello_dep' 2>/dev/null || echo DEP_NOT_IMPORTABLE
EOF
)" || true
for want in DECLARED_PTH_WRITTEN REMOVED_EXIT=0 REMOVAL_LOGGED DEPS_DIR_GONE DEPS_PTH_GONE FOREIGN_PTH_KEPT DEP_NOT_IMPORTABLE; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "docs/self-hosting.md#extensions roll back an upgrade (code layer): .previous/ext_hello swapped back, entry back to 0.1.1 → replay → 0.1.1 wired, its deps gone"
out="$(NET="--network none" in_image <<EOF
echo '$(manifest '"ext_hello": {"version": "0.2.0"}')' > sites/extensions.json
ext-replay.sh >/dev/null 2>&1 && test -d apps-ext/.deps/ext_hello && echo UPGRADED_AGAIN
cd apps-ext && rm -rf ext_hello && mv .previous/ext_hello ext_hello && cd $BENCH
echo '$(manifest '"ext_hello": {"version": "0.1.1"}')' > sites/extensions.json
ext-replay.sh > /tmp/rollback.log 2>&1; echo ROLLBACK_EXIT=\$?
grep -q '^ext-replay: ext_hello has no wheels/ — removed its deps' /tmp/rollback.log && echo ROLLBACK_DEPS_REMOVED
grep -q '^ext-replay: ext_hello 0.1.1 .* built ' /tmp/rollback.log && echo ROLLBACK_REBUILT
v=\$(env/bin/python -c 'import ext_hello; print(ext_hello.__version__)') && echo "ROLLBACK_VERSION \$v"
env/bin/python -c 'import ext_hello_dep' 2>/dev/null || echo ROLLBACK_DEP_NOT_IMPORTABLE
EOF
)" || true
for want in UPGRADED_AGAIN ROLLBACK_EXIT=0 ROLLBACK_DEPS_REMOVED ROLLBACK_REBUILT "ROLLBACK_VERSION 0.1.1" ROLLBACK_DEP_NOT_IMPORTABLE; do
	grep -qx -- "$want" <<<"$out" && ok "$want" || bad "missing $want: $out"
done

step "reconcile: extension removed from the manifest → dropped from apps.txt, symlink and .pth gone"
out="$(in_image <<EOF
echo ext_hello >> sites/apps.txt; ln -s $BENCH/apps-ext/ext_hello apps/ext_hello
echo '{"schema": 1, "extensions": {}}' > sites/extensions.json
ext-replay.sh 2>&1 | grep -i stale
echo STALE_LINES=\$(grep -cx ext_hello sites/apps.txt); [ -e apps/ext_hello ] || echo STALE_LINK_GONE
EOF
)" || true
grep -q STALE_LINES=0 <<<"$out" && grep -q STALE_LINK_GONE <<<"$out" && ok "reconciled" || bad "stale entry survived: $out"

step "in-place reload (ext-agent's hot path): a --preload gunicorn that imported 0.1.0 in its master serves 0.2.0 and its bundled wheel after ext-replay.sh + ext-reload.sh, without a failed request; a container whose PID 1 is not gunicorn is refused"
out="$(in_image <<<'ext-reload.sh; echo RELOAD_EXIT=$?')" || true
grep -qx 'RELOAD_EXIT=1' <<<"$out" && grep -q 'PID 1 is not gunicorn' <<<"$out" && ok "not gunicorn → exit 1 with a message" || bad "refusal: $out"
python3 "$HERE/make_ext_hello_upgrade.py" "$TMP/up.zip" >/dev/null
mkdir -p "$TMP/up/ext_hello" "$TMP/v1" && (cd "$TMP/up/ext_hello" && python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(".")' "$TMP/up.zip")
cp -R "$HERE/ext_hello" "$TMP/v1/"
docker run --rm $PLATFORM -v "$VOL:$BENCH/apps-ext" --entrypoint bash "$IMAGE" -c "rm -rf $BENCH/apps-ext/ext_hello $BENCH/apps-ext/.deps/ext_hello $BENCH/apps-ext/.built/ext_hello"
into_volume "$TMP/v1/ext_hello" "" ""
mkdir -p "$TMP/probe"
cat > "$TMP/probe/probe.py" <<'PY'
import importlib
try:
	import ext_hello          # the master imports the extension at preload: the worst case for a fork
except ImportError:
	pass


def app(environ, start_response):
	out = []
	for name in ("ext_hello", "ext_hello_dep"):
		try:
			out.append(f"{name}={importlib.import_module(name).__version__}")
		except ImportError:
			out.append(f"{name}=missing")
	start_response("200 OK", [("Content-Type", "text/plain")])
	return [" ".join(out).encode()]
PY
manifest '"ext_hello": {"version": "0.1.0"}' > "$TMP/extensions.json"
mounts=(-v "$VOL:$BENCH/apps-ext" -v "$TMP/probe:/tmp/probe:ro" -v "$TMP/extensions.json:$BENCH/sites/extensions.json")
[ -n "$BAKED" ] || mounts+=(-v "$PROD/ext-replay.sh:/usr/local/bin/ext-replay.sh:ro" -v "$PROD/main-entrypoint.sh:/usr/local/bin/entrypoint.sh:ro"
	-v "$PROD/ext-reload.sh:/usr/local/bin/ext-reload.sh:ro" -v "$PROD/gunicorn.conf.py:/etc/gunicorn.conf.py:ro")
GC="ext-smoke-gunicorn-$$"
docker run -d --name "$GC" $PLATFORM "${mounts[@]}" "$IMAGE" \
	$BENCH/env/bin/gunicorn --config=/etc/gunicorn.conf.py --chdir=/tmp/probe --bind=127.0.0.1:8000 --workers=2 --threads=2 --worker-class=gthread --preload probe:app >/dev/null
trap 'docker rm -f "$GC" >/dev/null 2>&1 || true; docker volume rm -f "$VOL" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
get() { docker exec "$GC" curl -s --max-time 5 http://127.0.0.1:8000/ || true; }
for _ in $(seq 1 60); do [ -n "$(get)" ] && break; sleep 2; done
[ "$(get)" = "ext_hello=0.1.0 ext_hello_dep=missing" ] && ok "started: 0.1.0, no deps" || bad "start: $(get) $(docker logs --tail 20 "$GC" 2>&1)"
docker run --rm $PLATFORM -v "$VOL:$BENCH/apps-ext" --entrypoint bash "$IMAGE" -c "rm -rf $BENCH/apps-ext/ext_hello"
into_volume "$TMP/up/ext_hello" "" ""
manifest '"ext_hello": {"version": "0.2.0"}' > "$TMP/extensions.json"            # in place: the bind mount follows the inode
docker exec "$GC" ext-replay.sh >/dev/null 2>&1 && ok "ext-replay.sh in the running container" || bad "replay failed: $(docker exec "$GC" ext-replay.sh 2>&1 | tail -3)"
[ "$(get)" = "ext_hello=0.1.0 ext_hello_dep=missing" ] && ok "replay alone changes nothing the workers run (why the reload is needed)" || bad "before reload: $(get)"
docker exec -d "$GC" sh -c 'n=0; f=0; while [ $n -lt 60 ]; do curl -s -f --max-time 5 -o /dev/null http://127.0.0.1:8000/ || f=$((f+1)); n=$((n+1)); sleep 0.1; done; echo "$n $f" > /tmp/load.out'
sleep 1
out="$(docker exec "$GC" ext-reload.sh 2>&1; echo "RELOAD_EXIT=$?")"
grep -qx 'RELOAD_EXIT=0' <<<"$out" && ok "ext-reload.sh: $(grep reloaded <<<"$out")" || bad "reload: $out"
[ "$(get)" = "ext_hello=0.2.0 ext_hello_dep=1.0.0" ] && ok "after the reload: 0.2.0 and its wheel, in a worker forked from the 0.1.0 master" || bad "after reload: $(get)"
for _ in $(seq 1 30); do docker exec "$GC" test -f /tmp/load.out && break; sleep 1; done
load="$(docker exec "$GC" cat /tmp/load.out 2>/dev/null || echo '? ?')"
[ "${load#* }" = 0 ] && ok "requests during the reload: ${load% *} sent, none failed" || bad "requests during the reload (sent failed): $load"
docker rm -f "$GC" >/dev/null 2>&1 || true

echo
echo "ext-replay smoke: $pass passed, $fail failed ($IMAGE)"
[ "$fail" -eq 0 ]
