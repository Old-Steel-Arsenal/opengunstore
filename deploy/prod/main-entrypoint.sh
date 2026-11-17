#!/bin/bash
# Links the image-baked assets into the (volume-mounted) sites/ dir on start.
# Vendored from frappe_docker (resources/core/main-entrypoint.sh) so the image
# build doesn't depend on the gitignored deploy/frappe_docker/ checkout.
set -e

ASSETS_PATH="/home/frappe/frappe-bench/sites/assets"
BAKED_PATH="/home/frappe/frappe-bench/assets"

# sites/ is shared by every container of the stack and several start at once (the extension
# agent restarts the workers together): the frappe_docker original, rm -rf then ln -s, raced —
# a container that lost found the link already there, `ln` failed, set -e stopped it and docker
# restarted it (an unexplained extra restart, and a moment without sites/assets for nginx).
# The link is the same path in every container (each resolves it to its own image's assets),
# so it is only written when it is not that link, and then atomically (rename over it).
if [ "$(readlink "$ASSETS_PATH" 2>/dev/null)" != "$BAKED_PATH" ]; then
	echo "Linking fresh assets to volume..."
	mkdir -p "$(dirname "$ASSETS_PATH")"
	[ -L "$ASSETS_PATH" ] || rm -rf "$ASSETS_PATH"       # a real directory (a bench's own) cannot be renamed over
	tmp="$ASSETS_PATH.tmp.$$"
	ln -sfn "$BAKED_PATH" "$tmp"
	mv -Tf "$tmp" "$ASSETS_PATH"
fi

# Extension layer: re-link the apps declared in sites/extensions.json (no-op
# without that file). A failed replay stops the container on purpose — see the
# script header.
/usr/local/bin/ext-replay.sh

exec "$@"
