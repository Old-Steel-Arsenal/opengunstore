#!/usr/bin/env bash
# One-time: create the production site and install all apps.
# Run AFTER `deploy/prod/up.sh up -d` and the db + backend are healthy.
#
#   ADMIN_PASSWORD='strong-desk-admin-pw' ./scripts/prod_bootstrap.sh
#
# SITE defaults to FRAPPE_SITE_NAME_HEADER from deploy/prod/.env.
# The site is empty afterwards (no company, tax rules or POS Profile): the setup wizard and
# scripts/store_setup.sh fill that in. Integration credentials (FastBound / RSR / Woo) are NOT
# set here either — enter them in the Desk (their Settings doctypes live in the DB, not git).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UP="$HERE/../deploy/prod/up.sh"
ENV_FILE="$HERE/../deploy/prod/.env"

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE"; exit 1; }
# Read specific keys WITHOUT sourcing — .env holds docker-compose values like
# SITES_RULE=Host(`...`) that are valid for compose but not for shell `source`.
env_val() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

SITE="${SITE:-$(env_val FRAPPE_SITE_NAME_HEADER)}"
DB_PASSWORD="$(env_val DB_PASSWORD)"
: "${SITE:?FRAPPE_SITE_NAME_HEADER not found in .env}"
: "${DB_PASSWORD:?DB_PASSWORD not found in .env}"
: "${ADMIN_PASSWORD:?set ADMIN_PASSWORD env (the Desk Administrator password)}"

if "$UP" exec -T backend bench --site "$SITE" doctor >/dev/null 2>&1; then
  echo "Site $SITE already exists — skipping new-site."
else
  echo "Creating site $SITE ..."
  "$UP" exec -T backend bench new-site "$SITE" \
    --no-mariadb-socket \
    --mariadb-root-password "$DB_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --install-app erpnext
fi

# crm (Frappe CRM) first: ffl_core requires it (installing ffl_core would pull it in as well).
for app in crm ffl_core ffl_integrations ffl_woo_sync; do
  echo "Installing $app ..."
  "$UP" exec -T backend bench --site "$SITE" install-app "$app"
done

# The site name is the public host on every store; get_url() builds every
# absolute URL (receipt PDF logo, email headers, print view) from host_name and
# falls back to http://<site> without it — see docs/self-hosting.md#start-the-stack-and-create-the-site.
"$UP" exec -T backend bench --site "$SITE" set-config host_name "https://$SITE"

echo "Running migrate + enabling scheduler ..."
"$UP" exec -T backend bench --site "$SITE" migrate
"$UP" exec -T backend bench --site "$SITE" enable-scheduler

cat <<EOF

Bootstrap complete for $SITE.

Next (see docs/self-hosting.md, "Set up your store"):
  1. Sign in at https://$SITE/app as Administrator and finish the ERPNext setup wizard
     (country United States, currency USD, chart of accounts "Standard").
  2. Copy deploy/prod/store.env.example to deploy/prod/store.env, fill it in, then run
     ./scripts/store_setup.sh deploy/prod/store.env
  3. Prove a first sale works (docs/self-hosting.md, "Verify a first sale").

Then, in the Desk (NOT in git), as you need them:
  - FastBound Settings   -> account_num, api_key, webhook_secret, default_audit_user
                            (no FastBound? use Paper 4473 Mode instead: docs/self-hosting.md)
  - RSR Settings         -> FTP host/user/password + sync schedule
  - WooCommerce settings (ffl_woo_sync) -> store URL + consumer key/secret
  - Re-register the FastBound webhook to:
    https://$SITE/api/method/ffl_integrations.fastbound.webhook.fastbound_event
EOF
