# syntax=docker/dockerfile:1
# Production image for OpenGunstore: frappe + erpnext + our 3 apps
# (ffl_core, ffl_integrations, ffl_woo_sync).
#
# Built in CI (.github/workflows/release-image.yml) and pushed to GHCR; the
# server only pulls it. Mirrors frappe_docker's layered build, but our
# custom apps live inside this monorepo (not separate repos), so we COPY them
# from the build context instead of `bench get-app <url>`.
#
# IMPORTANT: build context MUST be the repo root (so apps/ and deploy/prod/ are
# reachable). The stack runs linux/amd64 only (its compose files pin it), so on an
# ARM machine (Apple Silicon, ARM servers) build for that platform:
#   docker build --platform linux/amd64 -f deploy/prod/Dockerfile -t <registry>/<image>:<tag> .

ARG FRAPPE_BRANCH=version-16

# ---- builder: full bench with node/yarn to compile assets ----
FROM frappe/build:${FRAPPE_BRANCH} AS builder

ARG FRAPPE_BRANCH=version-16
ARG FRAPPE_PATH=https://github.com/frappe/frappe
ARG ERPNEXT_REMOTE=https://github.com/frappe/erpnext
# Pinned app refs: frozen to fixed release tags so every build is reproducible and
# deploys can't silently pull a newer version-16 with upstream regressions (e.g.
# the v16.24.0 print.js `letter_head_for` bug that broke printing). Bump these
# deliberately, validating in CI/dev first. FRAPPE_BRANCH above stays a branch —
# it only tags the frappe/build + frappe/base toolchain images, which aren't
# published per-patch-version.
ARG FRAPPE_REF=v16.52.0
ARG ERPNEXT_REF=v16.51.1
# Change to force a fresh clone of erpnext even if layers are cached.
ARG CACHE_BUST=""

USER frappe

RUN : "${CACHE_BUST}" && \
  bench init \
    --frappe-branch=${FRAPPE_REF} \
    --frappe-path=${FRAPPE_PATH} \
    --no-procfile \
    --no-backups \
    --skip-redis-config-generation \
    --verbose \
    /home/frappe/frappe-bench && \
  cd /home/frappe/frappe-bench && \
  bench get-app --branch=${ERPNEXT_REF} erpnext ${ERPNEXT_REMOTE}

# Our three apps, straight from this monorepo's build context.
COPY --chown=frappe:frappe apps/ffl_core         /home/frappe/frappe-bench/apps/ffl_core
COPY --chown=frappe:frappe apps/ffl_integrations /home/frappe/frappe-bench/apps/ffl_integrations
COPY --chown=frappe:frappe apps/ffl_woo_sync     /home/frappe/frappe-bench/apps/ffl_woo_sync

RUN cd /home/frappe/frappe-bench && \
  ./env/bin/pip install --no-cache-dir \
    -e apps/ffl_core -e apps/ffl_integrations -e apps/ffl_woo_sync && \
  { [ -z "$(tail -c1 sites/apps.txt)" ] || echo >> sites/apps.txt; } && \
  printf 'ffl_core\nffl_integrations\nffl_woo_sync\n' >> sites/apps.txt && \
  bench build && \
  echo "{}" > sites/common_site_config.json && \
  find apps -mindepth 1 -path "*/.git" -prune -exec rm -rf {} +

# ---- backend: lean runtime (also serves frontend/websocket/workers) ----
FROM frappe/base:${FRAPPE_BRANCH} AS backend

USER frappe

COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench

# Extension layer mount point (compose.extensions.yaml). Created here, owned by
# frappe, so a fresh named volume is initialised writable for uid 1000.
RUN mkdir -p apps-ext

# Move built assets to image-layer storage; the entrypoint re-links them into
# the (volume-mounted) sites/ dir on container start.
RUN cp -r sites/assets assets && rm -rf sites/assets

VOLUME [ \
  "/home/frappe/frappe-bench/sites", \
  "/home/frappe/frappe-bench/logs" \
]

USER root
COPY deploy/prod/main-entrypoint.sh /usr/local/bin/entrypoint.sh
COPY deploy/prod/ext-replay.sh /usr/local/bin/ext-replay.sh
COPY deploy/prod/gunicorn.conf.py /etc/gunicorn.conf.py
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/ext-replay.sh

USER frappe
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# gunicorn.conf.py: trust the frontend nginx's X-Forwarded-Proto (and only that
# header) so the request scheme reads https — Frappe builds the OAuth issuer and
# endpoints from it. Details in the file.
CMD [ \
  "/home/frappe/frappe-bench/env/bin/gunicorn", \
  "--config=/etc/gunicorn.conf.py", \
  "--chdir=/home/frappe/frappe-bench/sites", \
  "--bind=0.0.0.0:8000", \
  "--threads=4", \
  "--workers=2", \
  "--worker-class=gthread", \
  "--worker-tmp-dir=/dev/shm", \
  "--timeout=120", \
  "--preload", \
  "frappe.app:application" \
]
