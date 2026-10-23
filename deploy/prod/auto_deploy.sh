#!/usr/bin/env bash
# Roll out a released image tag on the server. Whatever deploys the store calls it
# over SSH (a deploy pipeline of your own, or a person); this repo runs no deploy job.
# Pull the image first (`CUSTOM_TAG=<tag> ./deploy/prod/up.sh pull`) so a tag with no
# image stops before .env changes:
#   ./deploy/prod/auto_deploy.sh <image-tag>
# Only the CUSTOM_TAG line of .env is touched; secrets in .env are left alone.
set -euo pipefail

TAG="${1:?usage: auto_deploy.sh <image-tag>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.env"
UP="$HERE/up.sh"

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE on server"; exit 1; }
SITE="$(grep '^FRAPPE_SITE_NAME_HEADER=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
: "${SITE:?FRAPPE_SITE_NAME_HEADER not found in .env}"

echo "==> rolling out $TAG to $SITE"
# The images of this host's stacks (the POS and its services, the MCP servers) as compose resolves
# them from .env and mcp/.env right now.
stack_images() {
	"$UP" config --images
	env -u COMPOSE_PROJECT_NAME docker compose -f "$HERE/mcp/compose.yaml" --env-file "$HERE/mcp/.env" config --images 2>/dev/null || true
}
# What the stacks run before this deploy: prune_images (at the end) keeps it, so rolling back to the
# release this one replaces needs no download.
KEEP_IMAGES="$(stack_images 2>/dev/null || true)"
sed -i "s|^CUSTOM_TAG=.*|CUSTOM_TAG=${TAG}|" "$ENV_FILE"

"$UP" pull
"$UP" up -d

echo "==> waiting for db healthy"
for _ in $(seq 1 24); do
	"$UP" ps 2>/dev/null | grep -q "(healthy)" && break
	sleep 5
done
# The entrypoint replays the extension layer (docs/self-hosting.md#extensions) before it hands over
# to the service; a bench command exec'd into the container must not overtake it.
# Worst case ≈ N extensions × ~40 s compile in the one container that holds the
# lock, plus the others' cached passes — 10 minutes covers a handful.
wait_for_replay() {
	for _ in $(seq 1 120); do
		"$UP" exec -T backend test -f /home/frappe/frappe-bench/.ext-replay.done 2>/dev/null && return 0
		sleep 5
	done
	echo "!! backend never finished its extension replay (10 min). Last log lines:"
	"$UP" logs --tail=50 backend || true
	return 1
}
echo "==> waiting for the extension replay in backend"
wait_for_replay

# A new image can have new asset hashes (bench build isn't deterministic), and
# the shared `assets_json` key is cached in redis-cache. `up -d` doesn't recreate
# redis-cache, and a `restart` reloads its RDB snapshot — so the stale key
# survives and the app serves old asset URLs that 404 against the new image's
# files (broken CSS/JS). `bench clear-cache` doesn't purge that shared key, and a
# restart isn't enough — FLUSH it, then bounce the app so it re-reads assets.json.
echo "==> refreshing asset cache (flush redis-cache + bounce app + frontend)"
"$UP" exec -T redis-cache redis-cli FLUSHALL
sleep 2
# queue-catalog (local catalog) is an opt-in compose profile: restarted only when
# it runs, since restarting a service whose profile is off fails.
optional="$("$UP" ps --services --status running 2>/dev/null | grep -x queue-catalog || true)"
"$UP" restart backend websocket queue-short queue-long scheduler $optional
# restart re-runs the entrypoint (and its replay): wait again before frontend
# resolves the backend and before install-app / migrate exec into it.
echo "==> waiting for the extension replay after restart"
wait_for_replay
# The frontend nginx resolves the backend's container IP at its own startup.
# We just restarted backend (new IP), so the frontend must also restart or it
# 502s on a stale upstream IP. Restart it AFTER the backend has its new IP.
"$UP" restart frontend
sleep 4

# Ensure newly-added apps are installed on the EXISTING site before migrate.
# auto_deploy historically only migrated, but an app freshly baked into the image
# (e.g. ffl_woo_sync) still needs a one-time install-app on the live site for its
# doctypes/custom-fields/hooks to load. Idempotent: skipped when already installed.
for app in ffl_woo_sync; do
	if ! "$UP" exec -T backend bench --site "$SITE" list-apps 2>/dev/null | grep -qw "$app"; then
		echo "==> installing new app on site: $app"
		"$UP" exec -T backend bench --site "$SITE" install-app "$app"
	fi
done

# Site config guard, re-asserted on every deploy.
# host_name is what frappe.utils.get_url() builds absolute URLs from — receipt
# PDFs (wkhtmltopdf fetches the logo by URL), email headers, print view. Unset,
# frappe falls back to http://<site>; Caddy 308s that to https, wkhtmltopdf drops
# the image, and the browser print view blocks it as mixed content. Every receipt PDF a
# production site mailed without the logo had exactly this cause. The site name
# IS the public host.
"$UP" exec -T backend bench --site "$SITE" set-config host_name "https://$SITE" >/dev/null

# Extension switches must survive a migrate (run-3 qa-r2 saw one flip it could
# not explain): snapshot before, assert (and restore + log) after.
switches="$("$UP" exec -T backend bench --site "$SITE" execute ffl_core.extensions.registry.switch_snapshot 2>/dev/null | tail -n 1 || echo '{}')"
"$UP" exec -T backend bench --site "$SITE" migrate
"$UP" exec -T backend bench --site "$SITE" execute ffl_core.extensions.registry.assert_switches --args "[$(printf '%s' "$switches" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip() or "{}"))')]" 2>/dev/null | tail -n 1 | grep -q '"restored": \[\]' || echo "!! extension switches changed by migrate and were restored — see Error Log on $SITE"
"$UP" exec -T backend bench --site "$SITE" clear-cache
echo "==> auto-deployed $TAG to $SITE"

# Remote MCP servers ride the POS release (deploy/prod/mcp): the pinned build in
# version.env, pointed at this site. After the POS is up and migrated, because
# they verify tokens against it. A failure here must not fail the POS deploy —
# the POS is already live — but it must be loud: a ::warning:: in the Actions log.
#
# Two optional keys in deploy/prod/.env (unset = the behaviour above, unchanged):
#   MCP_ENABLED=0  skip this step entirely (no MCP servers on this host)
#   MCP_TAG=...    run this image tag instead of the one pinned in mcp/version.env
# The MCP stack is its own Compose project, named after its directory (`mcp`). A COMPOSE_PROJECT_NAME
# exported for the POS stack (a second stack on this host, see docs/self-hosting.md) must not rename it,
# so every call below runs without that variable. Production sets none, so nothing changes there.
deploy_mcp() {
	local dir="$HERE/mcp" publish base gb enabled tag
	enabled="$(grep '^MCP_ENABLED=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
	[ "$enabled" != "0" ] || { echo "==> MCP servers: skipped (MCP_ENABLED=0)"; return 0; }
	[ -f "$dir/compose.yaml" ] && [ -f "$dir/version.env" ] || return 0
	tag="$(grep '^MCP_TAG=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
	[ -n "$tag" ] || tag="$(grep '^MCP_TAG=' "$dir/version.env" | head -1 | cut -d= -f2-)"
	publish="$(grep '^HTTP_PUBLISH_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
	publish="${publish:-127.0.0.1:8080}"
	base="https://$SITE"
	# GunBroker writes on the full connector follow the store's GunBroker switch;
	# anything but a clean 1 (query failed, switch off) leaves them off.
	gb="$("$UP" exec -T backend bench --site "$SITE" execute frappe.db.get_single_value \
		--args "['GunBroker Settings', 'enabled']" 2>/dev/null | tail -n 1 | tr -d '\r ')"
	[ "$gb" = "1" ] || gb=0
	# The Range tools follow the Range extension (osa_range): on only where it is installed and
	# switched on. bench execute prints `true` for True and nothing for False or a missing app.
	range="$("$UP" exec -T backend bench --site "$SITE" execute ffl_core.extensions.is_enabled \
		--args "['osa_range']" 2>/dev/null | tail -n 1 | tr -d '\r ')"
	[ "$range" = "true" ] && range=1 || range=0
	{
		echo "# written by auto_deploy.sh on every deploy — edit version.env / the script instead"
		echo "FRAPPE_BASE_URL=$base"
		echo "FRAPPE_INTERNAL_URL=http://${publish}"
		echo "MCP_GUNBROKER_ACTIONS=$gb"
		echo "MCP_RANGE=$range"
		echo "MCP_TAG=$tag"
		# the image to run: MCP_IMAGE from deploy/prod/.env when the store builds its own
		grep -h '^MCP_IMAGE=' "$ENV_FILE" || true
	} > "$dir/.env"
	echo "==> MCP servers: $(grep '^MCP_TAG=' "$dir/.env")"
	env -u COMPOSE_PROJECT_NAME docker compose -f "$dir/compose.yaml" --env-file "$dir/.env" pull -q &&
		env -u COMPOSE_PROJECT_NAME docker compose -f "$dir/compose.yaml" --env-file "$dir/.env" up -d || return 1
	for _ in $(seq 1 24); do
		[ "$(env -u COMPOSE_PROJECT_NAME docker compose -f "$dir/compose.yaml" --env-file "$dir/.env" ps --format '{{.Health}}' | grep -c healthy)" -ge 2 ] && { echo "==> MCP servers healthy"; return 0; }
		sleep 5
	done
	env -u COMPOSE_PROJECT_NAME docker compose -f "$dir/compose.yaml" --env-file "$dir/.env" ps || true
	return 1
}
deploy_mcp || echo "::warning title=MCP servers not healthy on $SITE::The POS deployed, but the remote MCP servers did not come up healthy — check 'docker compose -f ~/gunstore-pos/deploy/prod/mcp/compose.yaml ps' on the host."

# Every release pulls a new ~4 GB image and nothing removed the old ones, so a store's disk filled
# up after a few dozen releases and the next pull failed. After a deploy that went through, remove
# the images of this host's stacks that no container uses, except the ones the stacks ran before
# this deploy. Other images on the host are not touched. An image whose tag moved to a newer pull
# is listed as <repo>:<none>, which is not a reference: it goes by its ID.
prune_images() {
	local used old
	used="$(docker ps -aq | xargs -r docker inspect --format '{{.Image}}')"
	old="$(
		stack_images | sed 's/:[^:/]*$//' | sort -u | while read -r repo; do
			docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' "$repo"
		done | while read -r ref id; do
			grep -qxF "$ref" <<<"$KEEP_IMAGES" || grep -qxF "$id" <<<"$used" && continue
			[ "${ref##*:}" = "<none>" ] && echo "$id" || echo "$ref"
		done
	)"
	[ -n "$old" ] || { echo "==> no old images to remove"; return 0; }
	echo "==> removing $(wc -l <<<"$old" | tr -d ' ') old image(s)"
	xargs docker rmi <<<"$old" >/dev/null
}
prune_images || echo "::warning title=Old images not removed on $SITE::The deploy went through, but removing old images failed — check 'df -h /' and 'docker images' on the host."
