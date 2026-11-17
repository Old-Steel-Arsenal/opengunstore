#!/bin/bash
# Graceful reload of the backend's web server after an extension install or upgrade
# (the extension agent runs it in the backend container: deploy/prod/ext-agent.sh).
#
#   ext-reload.sh [timeout seconds, default 90]
#
# PID 1 of the backend container is the gunicorn master (entrypoint.sh execs the CMD).
# SIGHUP makes it fork a new set of workers and retire the old ones gracefully: they stop
# accepting, finish the requests in flight (graceful_timeout, 30 s) and exit. The listening
# socket is the master's, so no request is refused meanwhile, and the container — its IP,
# which the frontend nginx resolved at its start — stays. The master runs --preload; the
# new workers' sys.path and imports are made current in gunicorn.conf.py's post_fork.
#
# Run AFTER ext-replay.sh has wired this container (symlink, .pth, assets.json): a worker
# imports the extension from what replay left. Exit 0 once every old worker has exited and
# as many new ones run; anything else (PID 1 is not gunicorn, no workers, the timeout)
# exits 1 with a message — the agent then falls back to restarting the container.
set -euo pipefail
TIMEOUT="${1:-90}"

log() { echo "ext-reload: $*"; }
die() { echo "ext-reload: ERROR: $*" >&2; exit 1; }

grep -qa gunicorn /proc/1/cmdline 2>/dev/null || die "PID 1 is not gunicorn: $(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null)"
grep -q 'def post_fork' /etc/gunicorn.conf.py 2>/dev/null || die "/etc/gunicorn.conf.py has no post_fork: this image cannot reload in place"

workers() {   # the master's children, one pid per line (no procps in the image: /proc)
	local p
	for p in /proc/[0-9]*; do
		[ "$(awk '{print $4}' "$p/stat" 2>/dev/null)" = 1 ] && echo "${p#/proc/}"
	done
	return 0
}
alive() { local p; for p in $1; do [ -d "/proc/$p" ] && [ "$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)" = 1 ] && echo "$p"; done; return 0; }

old="$(workers)"
n="$(wc -w <<<"$old")"
[ "$n" -gt 0 ] || die "gunicorn has no workers"
kill -HUP 1
log "HUP sent; waiting for $n old worker(s) to finish their requests"
t0=$SECONDS
while :; do
	left="$(alive "$old")"
	new=0
	for p in $(workers); do grep -qxF "$p" <<<"$old" || new=$((new + 1)); done
	if [ -z "$left" ] && [ "$new" -ge "$n" ]; then
		log "reloaded: $new new worker(s) ($((SECONDS - t0)) s)"
		exit 0
	fi
	[ $((SECONDS - t0)) -lt "$TIMEOUT" ] || die "reload did not finish in $TIMEOUT s (old workers left: $(echo $left), new: $new)"
	sleep 0.5
done
