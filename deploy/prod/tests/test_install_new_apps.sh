#!/usr/bin/env bash
# auto_deploy.sh's install_new_apps against a fake compose wrapper — no docker, no ssh.
#   deploy/prod/tests/test_install_new_apps.sh
# An existing site gets each app the image newly carries (Frappe CRM first) installed once, before
# the migrate; a site that has them is left alone; a failed install stops the deploy.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../auto_deploy.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  ✔ $*"; }
bad() { fail=$((fail + 1)); echo "  ✘ $*"; }

{ grep '^NEW_APPS=' "$SCRIPT"; sed -n '/^install_new_apps() {$/,/^}$/p' "$SCRIPT"; } > "$T/fn.sh"
grep -q '^install_new_apps() {$' "$T/fn.sh" || { echo "install_new_apps not found in auto_deploy.sh"; exit 1; }

# fake up.sh: `list-apps` prints $FAKE_APPS (one per line, as bench does); every call is logged;
# install-app of $FAKE_FAIL exits 1.
cat > "$T/up.sh" <<'SH'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
	*list-apps*) printf '%s\n' $FAKE_APPS ;;
	*"install-app $FAKE_FAIL"*) exit 1 ;;
esac
SH
chmod +x "$T/up.sh"

# run <apps on the site>: install_new_apps on a site with those apps; its exit status in $T/rc
run() {
	: > "$T/log"
	rc=0
	FAKE_APPS="$1" FAKE_FAIL="${FAKE_FAIL:-none}" FAKE_LOG="$T/log" UP="$T/up.sh" SITE=pos.example.com \
		bash -c 'set -euo pipefail; source "$0"; install_new_apps' "$T/fn.sh" > "$T/out" 2>&1 || rc=$?
}
installs() { grep -o 'install-app [a-z_]*' "$T/log" | sed 's/install-app //' | tr '\n' ' ' | sed 's/ $//'; }

echo "== a site from before Frappe CRM: crm is installed, first"
run "frappe erpnext ffl_core ffl_integrations ffl_woo_sync"
[ "$(installs)" = "crm" ] && ok "installs crm only" || bad "installed: '$(installs)' — $(cat "$T/out")"
[ $rc -eq 0 ] && ok "exit 0" || bad "exit $rc"

echo "== a site with nothing new: no install"
run "frappe erpnext crm ffl_core ffl_integrations ffl_woo_sync"
[ -z "$(installs)" ] && ok "nothing installed" || bad "installed: '$(installs)'"

echo "== a site missing both: crm before ffl_woo_sync"
run "frappe erpnext ffl_core ffl_integrations"
[ "$(installs)" = "crm ffl_woo_sync" ] && ok "crm, then ffl_woo_sync" || bad "installed: '$(installs)'"

echo "== the app list is read once per deploy"
[ "$(grep -c list-apps "$T/log")" -eq 1 ] && ok "one list-apps" || bad "$(grep -c list-apps "$T/log") list-apps calls"

echo "== a failed crm install stops the deploy"
FAKE_FAIL=crm run "frappe erpnext ffl_core"
[ $rc -ne 0 ] && ok "non-zero exit" || bad "exit 0 after a failed install"
! grep -q 'install-app ffl_woo_sync' "$T/log" && ok "stopped at crm" || bad "went on: $(installs)"

echo "== auto_deploy.sh installs before it migrates"
inst="$(grep -n '^install_new_apps$' "$SCRIPT" | cut -d: -f1)"
mig="$(grep -n 'bench --site "\$SITE" migrate$' "$SCRIPT" | head -1 | cut -d: -f1)"
[ -n "$inst" ] && [ -n "$mig" ] && [ "$inst" -lt "$mig" ] && ok "install_new_apps (line $inst) before migrate (line $mig)" \
	|| bad "install_new_apps at '${inst:-none}', migrate at '${mig:-none}'"

echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
