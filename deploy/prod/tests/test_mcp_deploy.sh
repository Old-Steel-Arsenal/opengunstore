#!/usr/bin/env bash
# auto_deploy.sh's deploy_mcp against a fake compose wrapper and a fake docker — no docker, no ssh.
#   deploy/prod/tests/test_mcp_deploy.sh
# The function is cut out of the script and run in a temp deploy dir: what it writes to
# mcp/.env and which docker calls it makes are what the store's MCP_TAG / MCP_ENABLED /
# MCP_IMAGE keys in deploy/prod/.env must change (and, when unset, must not).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../auto_deploy.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "  ✔ $*"; }
bad() { fail=$((fail + 1)); echo "  ✘ $*"; }

sed -n '/^deploy_mcp() {$/,/^}$/p' "$SCRIPT" > "$T/fn.sh"
[ -s "$T/fn.sh" ] || { echo "deploy_mcp not found in auto_deploy.sh"; exit 1; }

cat > "$T/up.sh" <<'SH'
#!/usr/bin/env bash
case "$*" in
	*get_single_value*) echo 1 ;;
	*"extensions.is_enabled"*) [ -n "${FAKE_RANGE:-}" ] && echo "$FAKE_RANGE" ;;
esac
SH
chmod +x "$T/up.sh"
# fake docker: log the call; `ps --format` reports both containers healthy
cat > "$T/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $* [COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME-unset}]" >> "$FAKE_LOG"
case "$*" in *"ps --format"*) printf 'healthy\nhealthy\n' ;; esac
SH
chmod +x "$T/docker"

# run <extra .env lines...>: a fresh deploy dir with mcp/ pinned at sha-pinned0
run() {
	rm -rf "$T/d"; mkdir -p "$T/d/mcp"
	: > "$T/log"
	printf 'FRAPPE_SITE_NAME_HEADER=pos.example.com\n%s\n' "$*" > "$T/d/.env"
	echo 'x: y' > "$T/d/mcp/compose.yaml"
	echo 'MCP_TAG=sha-pinned0' > "$T/d/mcp/version.env"
	PATH="$T:$PATH" FAKE_LOG="$T/log" HERE="$T/d" ENV_FILE="$T/d/.env" UP="$T/up.sh" SITE=pos.example.com \
		bash -c 'source "$0"; deploy_mcp' "$T/fn.sh" > "$T/out" 2>&1 || echo "(deploy_mcp exit $?)" >> "$T/out"
}
env_has() { grep -qx "$1" "$T/d/mcp/.env"; }

echo "== unset: today's behaviour (version.env tag, no image line)"
run ''
env_has 'MCP_TAG=sha-pinned0' && ok "MCP_TAG from version.env" || bad "$(cat "$T/d/mcp/.env" 2>&1)"
! grep -q '^MCP_IMAGE=' "$T/d/mcp/.env" && ok "no MCP_IMAGE line" || bad "MCP_IMAGE written"
grep -q 'up -d' "$T/log" && ok "compose up -d ran" || bad "no up -d: $(cat "$T/out")"

echo "== MCP_TAG in deploy/prod/.env overrides version.env"
run 'MCP_TAG=sha-override'
env_has 'MCP_TAG=sha-override' && ! grep -q sha-pinned0 "$T/d/mcp/.env" && ok "override wins" || bad "$(cat "$T/d/mcp/.env" 2>&1)"

echo "== MCP_IMAGE is passed on"
run 'MCP_IMAGE=registry.example/mcp'
env_has 'MCP_IMAGE=registry.example/mcp' && ok "MCP_IMAGE copied" || bad "$(cat "$T/d/mcp/.env" 2>&1)"

echo "== MCP_ENABLED=0 skips the step entirely"
run 'MCP_ENABLED=0'
[ ! -e "$T/d/mcp/.env" ] && [ ! -s "$T/log" ] && grep -q 'skipped (MCP_ENABLED=0)' "$T/out" && ok "no mcp/.env, no docker call, says so" || bad "env: $(ls "$T/d/mcp"); log: $(cat "$T/log"); out: $(cat "$T/out")"

echo "== any other MCP_ENABLED value still deploys"
run 'MCP_ENABLED=1'
env_has 'MCP_TAG=sha-pinned0' && grep -q 'up -d' "$T/log" && ok "MCP_ENABLED=1 deploys" || bad "$(cat "$T/out")"

echo "== an exported COMPOSE_PROJECT_NAME does not reach the MCP compose calls"
COMPOSE_PROJECT_NAME=demo run ''
[ -s "$T/log" ] && ! grep -v 'COMPOSE_PROJECT_NAME=unset' "$T/log" | grep -q . && ok "every docker call ran without it" || bad "log: $(cat "$T/log")"
unset COMPOSE_PROJECT_NAME
echo "== and with none exported the calls are unchanged"
run ''
grep -q 'docker compose -f .*up -d \[COMPOSE_PROJECT_NAME=unset\]' "$T/log" && ok "same calls as before" || bad "log: $(cat "$T/log")"

echo "== the Range tools follow the Range extension"
run ''
env_has 'MCP_RANGE=0' && ok "no Range extension: MCP_RANGE=0" || bad "$(cat "$T/d/mcp/.env" 2>&1)"
FAKE_RANGE=true run ''
env_has 'MCP_RANGE=1' && ok "Range on: MCP_RANGE=1" || bad "$(cat "$T/d/mcp/.env" 2>&1)"
FAKE_RANGE=garbage run ''
env_has 'MCP_RANGE=0' && ok "anything but true: MCP_RANGE=0" || bad "$(cat "$T/d/mcp/.env" 2>&1)"
unset FAKE_RANGE

echo; echo "mcp-deploy: $pass passed, $fail failed"
[ "$fail" = 0 ]
