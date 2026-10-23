# Self-hosting OpenGunstore

How to run the OpenGunstore stack (Frappe/ERPNext with the `ffl_core`, `ffl_integrations` and `ffl_woo_sync` apps) on one Linux server: install, back up and restore, upgrade, and manage extensions, including rolling an extension upgrade back.

The stack runs in containers: the image is built by CI and pushed to a container registry, and the server only pulls and runs it. The database (MariaDB), Redis, the scheduler and the workers all run in containers on the same machine. The stack does **not** ship its own reverse proxy: the frontend is published on `127.0.0.1:8080` and a proxy you run on the host (the Caddy example below) terminates HTTPS and forwards to it.

> Conventions: `pos.example.com` is your site's public host name, `<your-server>` is your server, commands run from the repository root, and the server user is `ubuntu`. Store-specific operations (which machines, DNS, deploy pipelines) are not covered here; they belong in the operator's own deploy repository.

Related: [README](https://github.com/Old-Steel-Arsenal/opengunstore#readme) (what the project is; the Quick start there is for local development, not for a server) · [Deploying with an AI assistant](deploy-with-ai.md) (this guide as a checklist an AI coding agent can drive) · [Data Service](data-service.md) · [field notes for Settings](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/docs/field-notes.md).

Contents: [Before you start](#before-you-start) · [Docker](#docker) · [Get the code](#get-the-code) · [Configure `.env`](#configure-env) · [Start the stack and create the site](#start-the-stack-and-create-the-site) · [Reverse proxy](#reverse-proxy) · [Set up your store](#set-up-your-store) · [Verify a first sale](#verify-a-first-sale) · [Selling firearms: FastBound or paper 4473](#selling-firearms-fastbound-or-paper-4473) · [Integration credentials](#integration-credentials) · [FastBound webhook](#fastbound-webhook) · [Verify the integrations](#verify-the-integrations) · [Backups and restore](#backups-and-restore) · [Upgrades](#upgrades) · [Moving data from a dev machine](#moving-data-from-a-dev-machine) · [Extensions](#extensions) · [Remote MCP connector (optional)](#remote-mcp-connector-optional)

---

## Before you start

- A Linux server (Ubuntu is what this guide was written on) with at least 8 GB of RAM and a **static IP**. The ERPNext stack uses roughly 1.5-2.5 GB; check you have headroom for anything else on the box.
- **An x86-64 machine (`linux/amd64`).** The image is built for amd64 only and the compose files pin that platform, so an ARM host (Apple Silicon, ARM cloud servers) cannot run it natively. The helper scripts (`auto_deploy.sh` and the others) use GNU tools and are written for Linux, not macOS.
- Firewall: open **22 / 80 / 443**. Ports 80 and 443 belong to the host's reverse proxy.
- DNS: an **A record** for `pos.example.com` pointing at the server's static IP. `dig +short pos.example.com` should return that IP.
- **A container image to run**, one of two ways (see [Configure `.env`](#configure-env) for the settings that go with each):
  - *A published image.* The repository's release workflow builds and pushes one to a registry; set `CUSTOM_IMAGE` to it. `IMAGE` in `.github/workflows/release-image.yml` names the package CI pushes to; a fork changes it. If that registry is private, run `docker login <registry>` on the server first, with a token that can read packages; without it `up.sh pull` fails with "pull access denied".
  - *An image you build yourself*, from the repository root (about 10 minutes on a fast machine; the build is not for a small VPS):
    ```bash
    docker build --platform linux/amd64 -f deploy/prod/Dockerfile -t <image>:<tag> .
    ```
    `--platform linux/amd64` is required on an ARM machine and harmless on x86-64. If you build on the server, keep the image there and set `PULL_POLICY=never` (below). If you build elsewhere, `docker push` it to a registry and use it as a published image.
- **Caddy**, or another reverse proxy, installed on the server. The stack does not include one. For Ubuntu and Debian, follow [caddyserver.com/docs/install](https://caddyserver.com/docs/install); the Caddy section below assumes the package install (a `caddy` systemd service reading `/etc/caddy/Caddyfile`). Any other proxy works if it terminates HTTPS and forwards to `127.0.0.1:8080` with the original `Host` header.
- **Your store's facts** for [Set up your store](#set-up-your-store): company name and abbreviation, address, FFL number, sales-tax rate, owner e-mail. Have them before you start; the setup refuses to guess.

The release process tags product versions `vX.Y.Z` (the same image also carries a `main-<sha7>` tag). Deploy a release tag, not a branch build.

## Docker

```bash
docker version && docker compose version
# Only if Docker is missing: curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker ubuntu   (log in again to take effect)
```

## Get the code

```bash
cd ~ && git clone https://github.com/<owner>/opengunstore.git gunstore-pos
cd gunstore-pos
# frappe_docker is upstream and .gitignored; the runtime compose files need it, so clone it separately:
git clone --depth 1 https://github.com/frappe/frappe_docker.git deploy/frappe_docker
```
Clone into the home directory of the server user: the extension agent's systemd unit (`deploy/prod/ext-agent.service`) assumes `~/gunstore-pos`. For another path, change the `%h/gunstore-pos` entries in the unit (`WorkingDirectory=` and `Environment=UP=` / `ExecStart=`) to it before installing the unit (see "Install the agent" under [Extensions](#extensions)).

## Configure `.env`

```bash
cp deploy/prod/.env.example deploy/prod/.env
# Edit deploy/prod/.env:
#   CUSTOM_IMAGE=<registry>/<image>      # the image repository the server pulls from (or the name you gave `docker build -t`, without the tag)
#   CUSTOM_TAG=vX.Y.Z                    # first start: a product release tag, or the tag you built; afterwards every deploy rewrites it to the tag it rolled out (auto_deploy.sh)
#   PULL_POLICY=never                    # ONLY for an image built on this machine; leave `always` for a published image
#   DB_PASSWORD=<strong random>          # e.g. openssl rand -hex 18
#   FRAPPE_SITE_NAME_HEADER=pos.example.com
#   HTTP_PUBLISH_PORT=127.0.0.1:8080     # already the default; the frontend binds to localhost only and the proxy forwards to it
```
`deploy/prod/.env` is in `.gitignore` and must **never be committed**.

**A second stack on the same Docker host** (a demo, say) needs its own Compose project name, or it would reuse the first stack's containers and volumes. `up.sh` takes the name from the **shell environment**, not from `.env` (default `gunstore`), so `export COMPOSE_PROJECT_NAME=<name>` before every `up.sh` call, and before `prod_bootstrap.sh`, `store_setup.sh` and `auto_deploy.sh`, which call it; a unit or cron job that runs them must set it too. Pick the name before the first start: containers and volumes are named after it, so changing it later starts a new, empty stack. `HTTP_PUBLISH_PORT` must also differ.

`PULL_POLICY` tells Docker Compose when to pull the image. `always` (the template's value) pulls from the registry on every `up`, which is right for a published image. `never` uses the image already on this machine and never contacts a registry, which is what a locally built image needs: with `always`, Docker looks for your local-only name in a registry and fails with "pull access denied". With `never`, skip `./deploy/prod/up.sh pull` (or let it report "Skipped").

`.env.example` ships `MCP_ENABLED=0`: the MCP server image its default points at is private, so a fresh install must not try to pull it on every deploy. Leave it at 0 unless you build and publish your own MCP image ([Remote MCP connector](#remote-mcp-connector-optional)).

## Start the stack and create the site

```bash
chmod +x deploy/prod/up.sh scripts/prod_bootstrap.sh scripts/store_setup.sh
./deploy/prod/up.sh pull      # skip this for an image built here (PULL_POLICY=never)
./deploy/prod/up.sh up -d
./deploy/prod/up.sh ps      # wait for db (healthy) and frontend mapped to 127.0.0.1:8080

ADMIN_PASSWORD='<Desk administrator password>' ./scripts/prod_bootstrap.sh
```
The script creates the site `pos.example.com`, installs erpnext + ffl_core + ffl_integrations + ffl_woo_sync, runs `set-config host_name https://<site name>`, migrates, and enables the scheduler. It is safe to run again: an existing site is kept and the apps are installed only where missing. A site created here is empty: it has no company, tax rules or POS Profile yet. [Set up your store](#set-up-your-store) adds them.

`host_name` is not optional. `frappe.utils.get_url()` builds absolute URLs from it; without it Frappe falls back to `http://<site name>`, the proxy redirects that to https, wkhtmltopdf silently drops images (receipt PDFs lose the logo) and `/printview` shows broken images as mixed content. `auto_deploy.sh` re-asserts it on every deploy; after creating a site by hand or changing the domain, set it yourself.

Local self-check:
```bash
curl -H "Host: pos.example.com" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/login   # expect 200 (use your HTTP_PUBLISH_PORT if you changed it)
```

## Reverse proxy

> Changing and reloading a shared `/etc/caddy/Caddyfile` is a change to shared infrastructure; do it yourself on the server.

Caddy must already be installed (see [Before you start](#before-you-start)), and the A record and the firewall ports 80 and 443 must already be in place: Caddy gets its Let's Encrypt certificate by reaching the server on those ports under the site's name, and fails (and retries) until they are.

`deploy/prod/Caddyfile.example` is a generic vhost for the host `pos.example.com`. Put your own host name in `POS_HOST` and the `sed` below substitutes it while appending (`caddy validate` runs under `sudo` and does not see your shell's environment, so the file carries a literal host rather than a variable). Append it to the Caddyfile, validate, then reload (other site blocks are untouched):
```bash
sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%s)   # back up
POS_HOST=pos.example.com   # your site's public host name
POS_UPSTREAM=127.0.0.1:8080   # must equal HTTP_PUBLISH_PORT in deploy/prod/.env
sed -e "s/pos\.example\.com/$POS_HOST/g" -e "s/127\.0\.0\.1:8080/$POS_UPSTREAM/g" deploy/prod/Caddyfile.example | sudo tee -a /etc/caddy/Caddyfile >/dev/null
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```
The vhost redirects the **exact path `/` to `/login`** and forwards everything else to `127.0.0.1:8080` (the `HTTP_PUBLISH_PORT` default; change the `reverse_proxy` line if you changed that); Caddy obtains a Let's Encrypt certificate within a few seconds. The redirect is scoped to GET on purpose: Frappe's legacy form-post endpoint lives at `/` (report Excel/CSV export posts there), and redirecting a POST would silently break every report export.

Verify:
```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://pos.example.com/login   # 200
curl -sS -o /dev/null -w "%{http_code} -> %{redirect_url}\n" https://pos.example.com/   # 302 -> /login
```
Then open `https://pos.example.com/app` and sign in as `Administrator` with the password you gave the bootstrap script.

## Set up your store

A site made by `prod_bootstrap.sh` is empty: no company, no chart of accounts, no tax rules, no POS Profile. Two steps fill it in: ERPNext's setup wizard (once, in the browser), then the store configuration (a script, repeatable). Do them in this order.

### 1. The ERPNext setup wizard

Open `https://pos.example.com/app` and sign in as `Administrator`; the wizard appears on first login. Screens and labels vary a little between ERPNext versions; these are the answers that matter:

| Question | Answer |
|---|---|
| Language, country, time zone | English, **United States**, your time zone |
| Currency | **USD** |
| Your name, e-mail, password | The store owner. This becomes a login; its e-mail is `OWNER_EMAIL` below. |
| Company name | Your store's name. Write it down exactly: it must match `COMPANY_NAME` below. |
| Company abbreviation | A short code (2-4 letters). It is the suffix on every account and warehouse (`Stores - AB`) and must match `COMPANY_ABBR` below. |
| Chart of accounts | **Standard**. "Standard with Numbers" also works. Pick one and keep it: it is hard to change once the company has transactions. |
| Fiscal year | A calendar year (1 January to 31 December), unless your accountant says otherwise. |

Finish the wizard. Do not add sample data.

### 2. The store configuration

Copy the template and fill it in with your store's real facts. Every variable has a one-line comment, and the ones marked REQUIRED must be filled in; the values in the template are made up.

```bash
cp deploy/prod/store.env.example deploy/prod/store.env
nano deploy/prod/store.env                      # fill it in; the file is gitignored and may hold an SMTP password
./scripts/store_setup.sh deploy/prod/store.env
```

What goes in it, in short: `COMPANY_NAME` and `COMPANY_ABBR` exactly as in the wizard, `OWNER_EMAIL`, the store's address and `FFL_NUMBER`, and the sales-tax rate (`SALES_TAX_RATE`, a percentage; ask your accountant) with two names for the tax templates. Optional: phone, website, logo, receipt footer, California excise tax (`CFET_RATE`), outgoing e-mail, and `FORM_4473_MODE` ([next section but one](#selling-firearms-fastbound-or-paper-4473)).

Writing it without an editor (a script or an assistant): create it from the user's answers with a heredoc, `chmod 600` it, and leave out the keys you have no answer for; an absent optional key is the same as a blank one, and the required ones must be there. Only the keys in the template are read; anything else in the file is reported and skipped.

The script runs `ffl_core.setup.store_setup` inside the backend container. It passes the values by name, so they never appear in a command line or on screen, and it exits non-zero unless the run printed `STORE_SETUP COMMITTED`. That line is printed last, once everything is written. If the run fails earlier, part of the setup may already be written (some steps commit on their own); every step is safe to repeat, so fix the cause and run it again. A good run ends with:

```
STORE_SETUP COMMITTED
==> VERIFY (...)
VERIFY {"Pistol": {"template": "...", "net": 1000.0, "sales_tax": 82.5, ...}, "Service": {... "sales_tax": 0.0 ...}, ...}
```

Read the `VERIFY` line: it prices a $1000 pistol, $100 of ammunition, a $500 optic, a $70 service and a $50 accessory with your tax rules. Your rate should show on firearms, ammunition, optics and accessories, and zero on the service. If the numbers are wrong, the rate or the tax names in `store.env` are wrong; edit and run it again.

What it sets up: the sales-tax, card-clearing and bank accounts; the cash drawer's accounts (Cash Over/Short, Meals); the tax template and the item tax template on the Firearm and Ammunition groups (ERPNext's generic "US ST" seed is removed); payment methods mapped to accounts; a Walk-in Customer; the store's letter head, address and receipt branding; the POS Profile (named after your abbreviation); Stock Settings "Activate Serial / Batch No for Item", which a firearm Item needs (written both in Stock Settings and as the global default the Desk reads, and repaired if only one was set; it is the only Stock Settings value a new site lacks, because the other serial and batch settings the POS relies on are ERPNext's own defaults); and, if you set `FORM_4473_MODE`, the 4473 switches. No `bench migrate` is needed afterwards. (An image older than this guide's `store_setup` does not create the cash drawer accounts: if *Cash Over/Short* is missing under Accounting > Chart of Accounts, run `./deploy/prod/up.sh exec backend bench --site pos.example.com migrate` once.)

Re-running is safe and is how you change a fact later: the file is the source of truth, and what you edited by hand in Desk for the things it manages (letter head, tax rows, POS Profile tax template and print format) is put back. Things it creates once and then leaves alone: the company address, the walk-in customer, POS Profile payment methods and users, the e-mail account, and accounts.

**Staff.** In Desk, add a User for each person at the counter (User list > New). Give cashiers the role **Sales User** (rings sales and runs 4473s); add **Sales Manager** for someone who also handles the cash drawer and overrides; add **Stock Manager** for whoever receives inventory. Then open the POS Profile (named after your abbreviation) and add each cashier under *Applicable for Users*, or they cannot open the register.

## Verify a first sale

Before opening the store, prove the basics work: open a shift, sell something non-firearm for cash, close the shift. It needs no FastBound, distributor or WooCommerce account. Run it once the store configuration is done; the same steps are the smoke test to repeat after an [upgrade](#upgrades).

**In Desk**
1. *Stock > Item > New*: Item Code `TEST-ITEM`, Item Group **Accessory**, "Maintain Stock" ticked, Standard Selling Rate 20. Save.
2. *Stock > Stock Entry > New*, type **Material Receipt**: item `TEST-ITEM`, quantity 5, target warehouse `Stores - <abbr>`, basic rate 10. Submit.
3. Open **Point of Sale** (type it in the search bar). Choose your POS Profile and open the shift (opening cash 0 is fine; Submit).
4. Click `TEST-ITEM`, **Checkout**, payment **Cash**, enter the full amount, **Complete Order**. The total is 20 plus your sales tax (8.25 % gives 21.65).
5. In the POS menu choose **Close the POS**, accept the counted cash (it equals the expected cash), and submit.
6. *Accounting > Sales Invoice*: one invoice for `TEST-ITEM`, status **Paid**. The merge from POS Invoice to Sales Invoice happens when the shift closes.

**From the command line** (does the same steps with real documents and checks the ledger):
```bash
./deploy/prod/up.sh exec -T backend bench --site pos.example.com execute ffl_core.setup.first_sale_check.run
```
Expect `FIRST_SALE_CHECK shift opened ...`, `... cash sale ... net 20.00 + tax ...`, `... shift closed ...`, `... ledger ... paid, debit X = credit X`, and last `FIRST_SALE_CHECK PASS`. Any other ending is a failure; the message names the step. It posts to the ledger and keeps what it made: a test item (`FIRST-SALE-CHECK`) once, and **every run adds one more $20 sale** (a POS Invoice, its merged Sales Invoice and a closed shift). It therefore **refuses to run on a site that already has other sales**: use the Desk steps on a store that is open. With more than one enabled POS Profile it refuses and lists them; pass the one to use (`--kwargs "{'pos_profile': '<name>'}"`).

If a run fails after the shift has been closed, the test documents stay (ERPNext's merge commits as it goes); an earlier failure normally leaves nothing. Remove them the same way. To remove test sales before opening, find them in Desk (*POS Invoice* list, filter Item = `FIRST-SALE-CHECK`), cancel the shift's *POS Closing Entry* (that also cancels the merged Sales Invoice), then cancel the POS Invoice. Do this only before the store opens; on a ledger with real sales, ask your accountant.

Also check serial tracking is on, which a firearm Item needs:
```bash
./deploy/prod/up.sh exec -T backend bench --site pos.example.com execute frappe.db.get_single_value --args "['Stock Settings','enable_serial_and_batch_no_for_item']"   # expect 1
```

## Selling firearms: FastBound or paper 4473

A firearm cannot leave the store until its 4473 and background check are recorded. That gate is on in every mode and nothing here lowers it. The only choice is **where the 4473 lives**:

| | FastBound (the default) | Paper 4473 Mode |
|---|---|---|
| 4473 and NICS | Done in FastBound; its webhook tells the POS | Done on paper, NICS by phone or the FBI portal, outside this system |
| At the counter | **FastBound 4473** button, then wait for the form to complete | **Record Paper 4473** button, after the paper form is done and the check has cleared |
| Bound book (A&D) | Recorded in FastBound, and here | Kept here only; the disposition is stamped with who confirmed the paper form, and when |
| Settings | FastBound Settings > Enable FastBound ticked, with credentials ([Integration credentials](#integration-credentials)); FFL Settings > Paper 4473 Mode off | FFL Settings > Paper 4473 Mode ticked; FastBound Settings > Enable FastBound off |

A new site is in the FastBound column with no credentials, so **a firearm sale is blocked until you do one of the two**: the submit error says so ("If this store keeps PAPER 4473s instead, switch on FFL Settings > Paper 4473 Mode"). Nothing is switched off silently.

To choose, either put `FORM_4473_MODE=paper` (or `fastbound`) in `store.env` and re-run `./scripts/store_setup.sh`, or change the two checkboxes in Desk. Leave `FORM_4473_MODE` blank to change nothing. The change applies on the next POS page load, so reload every open till. You can switch back and forth; the 4473s already recorded stay as they are.

Paper mode does not change what the law requires. The paper 4473 must still be completed and filed, and the background check cleared, before the gun is handed over; this software cannot verify either, which is why it records who attested. Whether your store may use paper forms is for you and your compliance adviser to decide.

## Integration credentials

Enter these in Desk. They live in Settings documents in the database (and travel with its backups); they are never committed:
1. **FastBound Settings**: account_num, api_key, webhook_secret, default_audit_user. Skip this if you use [paper 4473s](#selling-firearms-fastbound-or-paper-4473).
2. **Distributor settings** (RSR Settings, Sports South Settings): account credentials and ordering options. The distributor catalog comes from the Data Service or the optional local catalog; see [data-service.md](data-service.md).
3. **WooCommerce settings** (ffl_woo_sync): store URL + consumer key/secret.
4. **Data Service Settings** (optional): endpoint, client id and secret, if you use a Data Service; see [data-service.md](data-service.md).

## FastBound webhook

In the FastBound back office, point the webhook at:
```
https://pos.example.com/api/method/ffl_integrations.fastbound.webhook.fastbound_event
```
The secret must equal `webhook_secret` in FastBound Settings (above).
> If a distributor requires its FTPS traffic to come from an allow-listed IP, give it the server's static IP.

## Verify the integrations

These need real accounts. The account-free check is [Verify a first sale](#verify-a-first-sale).

- Sell a firearm in the POS and walk the FastBound 4473 flow (hold, complete, webhook unlock), or, in paper mode, the **Record Paper 4473** flow.
- Distributor catalog or stock sync from the distributor's settings page (manual sync button).
- WooCommerce push and the `Firearms In Stock` report (Gun Shop workspace).
- Scheduled jobs: `./deploy/prod/up.sh logs -f scheduler`.

## Backups and restore

Back up the whole machine on a schedule the hosting provider offers (a daily full-instance snapshot, with a retention you choose). A machine snapshot covers the OS, Docker, MariaDB, the sites volume (which holds `encryption_key`) and all local configuration. It is crash-consistent; MariaDB/InnoDB recovers itself on restart. Decide deliberately whether snapshots live in the same account and region as the instance: that survives mistakes, bad migrations, disk loss and an accidentally deleted instance, but not the loss of the whole account. Take a manual snapshot before risky work.

**Application-level backup (every 6 h, database)**
- The ofelia container in `compose.backup-cron.yaml` runs `bench --site all backup` on `BACKUP_CRONSTRING` (default `@every 6h`) and writes into the sites volume. Use it for a **fast database-level rollback** (quicker than building a machine from a snapshot).
- List the backups that exist:
  ```bash
  ./deploy/prod/up.sh exec backend ls -lh sites/pos.example.com/private/backups/
  ```

### Restore A: whole machine / disaster recovery (from a snapshot)
1. In your provider's console, create a new instance from a snapshot (same plan or larger).
2. **Move the static IP to the new machine** (detach it from the old instance, attach it to the new one). DNS does not change. Without a static IP, change the A record for `pos.example.com` to the new instance's IP.
3. Check that the new instance's **firewall allows 22 / 80 / 443** (an instance created from a snapshot may reset to the defaults).
4. Containers restart with Docker (the image layers are in the snapshot, so nothing is pulled again); the host's reverse proxy is restored with it. Verify:
   ```bash
   ./deploy/prod/up.sh ps
   curl -sS -o /dev/null -w "%{http_code}\n" https://pos.example.com/login   # 200
   ```

### Restore B: accidental deletion / roll back one database (from a local bench backup)
```bash
./deploy/prod/up.sh exec backend ls -lh sites/pos.example.com/private/backups/   # pick a point in time
# Overwrites the whole current database with that point. Stop and check first.
# bench asks for the MariaDB root password (DB_PASSWORD in .env); feed it on stdin so the command works
# in a script and the password is never echoed, held in a shell variable, or put on a command line:
grep -E '^DB_PASSWORD=' deploy/prod/.env | cut -d= -f2- | \
  ./deploy/prod/up.sh exec -T backend bench --site pos.example.com \
    restore /home/frappe/frappe-bench/sites/pos.example.com/private/backups/<db.sql.gz>
./deploy/prod/up.sh exec -T backend bench --site pos.example.com migrate
```
`-T` (no terminal) is what lets bench read the password from the pipe. bench prints a harmless "Password input may be echoed" warning; the password is not shown.
> A bench backup is an application-consistent logical export, a cleaner complement to the crash-consistent machine snapshot.

## Upgrades

Follows `docs/versions.md`.

```bash
# 1) A PR changes deploy/prod/frappe-refs.env (the single source for the image and the CI bench) + docs/versions.md
# 2) Merging the release PR into main builds the image; once tests and build pass, a new
#    ffl_core version gets the tag v<x.y.z> (a version that is already tagged gets none).
#    This repo deploys nothing: upgrade the store yourself, inside a maintenance window:
CUSTOM_TAG=v<x.y.z> ./deploy/prod/up.sh pull     # a tag with no image stops here, .env untouched
./deploy/prod/auto_deploy.sh v<x.y.z>            # rewrites CUSTOM_TAG, starts, migrates, flushes caches
# 3) Smoke test (POS open shift -> take cash -> close shift): see "Verify a first sale"; on a store that is open use the Desk steps
```
`auto_deploy.sh` runs on Linux (it uses GNU `sed -i`). `auto_deploy.sh <tag>` is the script a deploy pipeline calls: it pulls, starts the stack, waits for the extension replay, flushes the asset cache, restarts the app containers, migrates and asserts the extension switches. It ends by removing the images of the POS and MCP stacks that no container uses, except the ones the stacks ran before this deploy, so the disk does not fill up with old releases and rolling back one release needs no download. Whatever deploys your store should call it (or do the same steps) rather than only `up -d`.

### Post-upgrade checklist

Where `ffl_*` meets ERPNext internals.

We pin the `version-16` branch, and every image rebuild pulls the latest version-16. The table below lists the **only** points where the custom apps depend on private ERPNext internals; after a minor upgrade, walk through it. CI's test job already runs against `version-16` (covering the Python side), but **CI does not cover the POS front-end internals, so smoke-test by hand**.

| Coupling point | File | Check |
|---|---|---|
| `make_closing_entry_from_opening` | `ffl_core/api/pos_close.py` | In `bench --site <site> console`, `from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening` works |
| `cur_pos` / `load_new_invoice_on_pos` / `make_new_invoice` | `ffl_core/public/js/ffl_core.bundle.js` (hold order) | In the POS, click FastBound 4473: the order is put on hold and a new one opens automatically |
| `serial_no_control.set_value` / `events.form_updated(current_item,"serial_no",...)` / `$component` / `current_item` | `ffl_core.bundle.js` (serial number dropdown) | The serial dropdown in the Item Details panel lists, selects and writes back |
| `is_created_using_pos` compatibility patch | `ffl_core/setup/pos_compat.py` | Confirm ERPNext still does **not** add the field natively (grep `pos_invoice.json` finds nothing); if it does, remove this patch to avoid a duplicate field |

**POS smoke test (run it on dev after an upgrade, and only then upgrade main):**
1. Add a firearm: the serial dropdown lists in-stock serials and lets you pick one.
2. Click FastBound 4473: the order goes on hold and the cashier opens a new one normally.
3. Recent Orders resumes the held order: the submit gate works (the 4473 / NICS checks block).
4. Add an ordinary item: sales tax is added automatically at your jurisdiction's rate.

If any step misbehaves, check against the table which internal interface changed on `version-16`.

## Moving data from a dev machine

Optional.
```bash
# dev machine: make dev-shell -> bench --site dev.localhost backup --with-files
# copy the files into the container (up.sh cp ... backend:/tmp/), then, with the MariaDB root password on stdin as in Restore B:
grep -E '^DB_PASSWORD=' deploy/prod/.env | cut -d= -f2- | \
  ./deploy/prod/up.sh exec -T backend bench --site pos.example.com \
    restore /tmp/<db.sql.gz> --with-public-files /tmp/<public.tar> --with-private-files /tmp/<private.tar>
./deploy/prod/up.sh exec -T backend bench --site pos.example.com migrate
```

## Extensions

Images are immutable and extensions are not baked into them. An extension's code lives on the **`apps-ext` volume** and is declared in **`sites/extensions.json`**; on every container start the entrypoint runs `ext-replay.sh` (`deploy/prod/ext-replay.sh`, baked into `/usr/local/bin`), which re-wires the declared extensions into the bench. Containers can be recreated, the image swapped or a service restarted and everything is restored automatically, with no manual step. No manifest means one log line and no change.

| Thing | Where | Written by |
|---|---|---|
| Extension source | volume `apps-ext` -> `/home/frappe/frappe-bench/apps-ext/<id>/` (the app root, with `pyproject.toml` and `<id>/hooks.py`) | the install action (the agent; by hand when there is none) |
| Manifest | `sites/extensions.json` (sites volume): `{"schema":1,"extensions":{"<id>":{"version":"0.1.0","source":"upload","sha256":"...","installed_at":"...","installed_by":"..."}}}` | same; replay only reads it |
| Wiring (re-done on every start) | `apps/<id>` symlink · `env/.../site-packages/<id>.pth` · `zz-ext-deps-<id>.pth` (-> `apps-ext/.deps/<id>`) · one line in `sites/apps.txt` · `sites/assets/<id>` | `ext-replay.sh` |
| Dependencies | `wheels/*.whl` in the package root (pure Python, plain files only) -> the volume's `apps-ext/.deps/<id>`; stamp `.deps/<id>/.ext-deps-stamp` = hash of the `wheels/` contents | `pip install --no-index --no-deps --target` only when the stamp changes, otherwise the log says `deps cached` |
| Upgrade rollback point | database backup `sites/.ext-backups/<site>/<id>/<YYYYmmdd-HHMMSS>-database.sql.gz` (system time zone, one per id) · previous tree `apps-ext/.previous/<id>` · the stage-1 temporary tree `.previous/.<id>.older` (the version before that; deleted when stage 1 succeeds) · tree-swap intent `apps-ext/.previous/<id>.intent.json` | panel upgrade stage 1 (`sideload.stage_upgrade`) writes them; panel uninstall clears them with `sideload.forget_rollback_point(<id>)` (a manual uninstall must delete them by hand); rollback recipe below |
| Build output | `apps-ext/<id>/<id>/public/dist/` (on the volume with the source) + stamp `apps-ext/.built/<id>` = `manifest version\|source content hash\|frappe version\|ffl_core version` | rebuilt only when the stamp changes (any source change, a new core) or `dist` lacks a bundle; otherwise `bench build --using-cached` (about 3 s) just refills `assets.json` |

**Rules**
- If replay fails (broken manifest; the manifest lists an id but the volume has no directory / no `__init__.py` / no `__version__` for it; `bench build` reports an esbuild error), the container **does not start** (restart loop; see `ERROR: ...` in `up.sh logs backend` under `ext-replay`). A site with an extension installed and no code returns 500 on every request anyway, so loud is better. **Recovery = put the files back.** Do not just delete the manifest entry: the site still has the extension installed, and the restart loop would become a 500 loop. To remove it for real, use the uninstall recipe below (`uninstall-app` first).
- frappe_docker's configurator rewrites `sites/apps.txt` to `ls apps` on every `up`; replay then reconciles it: ids in the manifest are added back, and only lines "we left ourselves" are removed (`apps/<x>` is a symlink into `apps-ext`, does not exist at all, or is a dangling symlink); other lines are left alone.
- Extensions have **no package metadata** (they are installed with `.pth` files and no dist-info): extension code must not call `importlib.metadata.version()` on itself and must not expect `pip list` to show it; the version comes only from `<id>.__version__` and the manifest.
- An extension's extra Python dependencies: **pure-Python wheels only**, in `wheels/*.whl` at the package root and listed in `[project].dependencies` of `pyproject.toml`. The lint rejects: anything not tagged `py3-none-any` / `py2.py3-none-any`; a `.whl` outside `wheels/`; a `*.whl` that is not a plain file (a directory called `x.whl`, say); wheels containing compiled artifacts (`.so` and the like) or whose top-level package name is a core app, the extension itself or an installed extension; dependencies (including `Requires-Dist` in a wheel's METADATA) satisfied by neither a bundled wheel nor the core environment (the core environment contains nothing under `apps-ext`); a wheel the core already ships ("provided by core x.y, do not bundle"); a package whose unpacked size, wheels included, exceeds the unpack limit. Replay hands pip only the **plain files** of `wheels/`, under the lock and before the build, and installs them into `apps-ext/.deps/<id>`: the new tree is installed to `.deps/<id>.tmp`, then swapped in with a **two-step rename** (not atomic; an interruption leaves a `.tmp` / `.old`, which the next replay cleans up and reinstalls, so it heals itself). In every container `site-packages/zz-ext-deps-<id>.pth` points there. `site.py` reads `.pth` files in file-name order and `zz-ext-deps-` sorts after the image's `erpnext.pth` / `ffl_core.pth` / `frappe.pth` and the other core apps' `.pth`, so the dependency directory sits on `sys.path` after site-packages and **after every core app**: it can only add packages the core lacks and cannot shadow the core. (An earlier name, `<id>-deps.pth`, sorted before `ffl_core.pth`; replay deletes it when it finds it.) If two extensions each bundle a wheel with the same name, the first `.pth` file name wins; do not package it that way.
- **The whole pipeline is serial.** While any extension row is Installing / Staged / Upgrading / Uninstalling, or `sites/.ext-request.json` is still waiting for the agent, the panel refuses uploading, installing, upgrading and uninstalling (the dialog is titled Busy and its text starts with `The store is busy:`), and a queued stage-1 job checks again when it starts. Why: the restart-request file has one slot and the agent restarts every worker at once, so a second job running in parallel would be killed half-way. `request_restart` refuses outright if another app's request is present; `mark_done` deletes only its own app's request.
- Nothing goes online at run time: the only pip is the `--no-index` install of bundled wheels above; `bench build` uses the node/yarn in the image.
- **Replay done marker** `/home/frappe/frappe-bench/.ext-replay.done` (container-local; deleted at every start and written when replay finishes): wait for it before any `exec ... bench`. A container being up does not mean the extensions are wired (this is what `wait_for_replay` in `auto_deploy.sh` does). If it has not appeared after 10 minutes, replay is stuck: read `logs backend`.

### Panel install

Store owners install themselves, with no SSH. Settings > Extensions > **Upload** a zip -> server-side `ffl_core.extensions.lint` (the mechanical checks: package structure / naming / hook allow-list / declared permissions covering the ones actually used / banned APIs / endpoint gates / py_compile; an error refuses the install, a warning is only shown) -> review card (yellow warning when unsigned, permission chips, high-risk ones red) -> type the id to confirm -> a two-stage pipeline:
1. **Stage 1** (long-queue job `ffl_core.extensions.sideload.stage`): unpack to `apps-ext/<id>` -> atomically write the manifest entry (`source: upload, sha256, installed_by`) -> run `ext-replay.sh` in this container to prove it wires and builds -> write `sites/.ext-request.json` `{action:"install", app, site}` -> the card shows **Staged**. If any step fails, the manifest and `apps-ext/<id>` go back to how they were, the card shows Failed (or the Upload page pops up the reason) and Error Log has an entry.
2. **Stage 2** (the host agent `deploy/prod/ext-agent.sh`, a systemd timer every 20 s): sees the request -> `up.sh restart websocket queue-short queue-long scheduler` -> `restart backend` -> waits for the backend marker -> `restart frontend` (nginx must re-resolve the backend IP and link the extension's static assets; every container replays and wires on start) -> `bench --site <site> execute ffl_core.extensions.sideload.finish_install --args '["<id>"]'` (= install-app + registry sync) -> writes `sites/.ext-request.done` and deletes the request. **Why restart first and install after**: once `install_app` runs, the site counts the app as installed, and any container that is not wired yet (the backend web container above all) raises ModuleNotFoundError on its next request, which takes the whole site down with a 500.
   - A store that runs the optional local catalog (Data Service Settings > Catalog source = Local, `COMPOSE_PROFILES=catalog` in `.env`) has one more container, `queue-catalog`. The agent and `auto_deploy.sh` add it to the restart batch only while it is running (`up.sh ps --services --status running`). The hand-copied stop / start / restart lines below do the same through `$optional`; a store on the Data Service has no such container and `$optional` is empty.
   - No agent installed (timer not enabled) = the card stays Staged, turns Failed after 30 minutes and tells you to check the timer. The code is already on the volume: once the timer is installed and the containers have **been restarted**, press Install to continue (before the restart Install is refused, because this container cannot import the extension yet).

### Install the agent

Once per store machine; a user-level service.

```bash
cd ~/gunstore-pos && mkdir -p ~/.config/systemd/user
cp deploy/prod/ext-agent.service deploy/prod/ext-agent.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ext-agent.timer
loginctl enable-linger $USER          # keep running when nobody is logged in
systemctl --user list-timers | grep ext-agent ; journalctl --user -u ext-agent -n 20   # check it is running
```
A second stack on the same host (a demo or staging stack, say) needs its own copy of the unit: change both `%h/gunstore-pos` paths in the unit to the other checkout's directory, and add `Environment=COMPOSE_PROJECT_NAME=<project>` (`up.sh` uses it to choose the stack).
Troubleshooting: `journalctl --user -u ext-agent -f`; `cat sites/.ext-request.json` (present = waiting for the agent), `cat sites/.ext-request.done` (the previous result); run `deploy/prod/ext-agent.sh` once by hand. The agent's idle probe, every 20 s, uses only `docker ps` (compose labels `com.docker.compose.project=${COMPOSE_PROJECT_NAME:-gunstore}` + `com.docker.compose.service=backend`) and a `docker exec` to read the request, and goes through `up.sh` only when there is work; if it finds no backend container it exits silently (the stack is down, or the unit's `COMPOSE_PROJECT_NAME` is wrong). The request's `action` is only `install` or `upgrade`; anything else is refused and written to done. The agent judges success only by the exit code of `bench execute`: an exception thrown inside the function is shown by bench as `name '...' is not defined` (exit 1). If done shows that sentence, the real cause is in the site's Error Log / `up.sh logs backend`. **An id that is already on the bench goes through the upgrade** (below).

### Panel upgrade

A new version of the same id. Upload the new zip (or a git address) -> the same lint and review card -> after confirming, two stages:
1. **Stage 1** (long queue, `sideload.stage_upgrade`): busy check (the whole site is serial, see above; if it is busy at the start, or only found out after the backup was made, this row is left as it was and the backup just made is deleted, the reason goes to Error Log and the panel dialog explains; if this id's previous upgrade was never finished (see "After a failed upgrade" below) it is handled the same way, with no backup taken) -> database backup of this site with `new_backup(backup_path_db=...)` to **`sites/.ext-backups/<site>/<id>/<YYYYmmdd-HHMMSS>-database.sql.gz`** (system time zone; **the database only**; the card message says `Backup: <absolute path>`, followed only by a space or the end of the line) -> the card shows Staged "Putting <id> <to> in place. Backup: <path>" -> write the intent `apps-ext/.previous/<id>.intent.json` (`{from, to, backup, started_at}`; after the row is Staged and before the first rename) -> whatever was in `.previous/<id>` (the version before the previous one) moves to `.previous/.<id>.older`, and `apps-ext/<id>` moves to `.previous/<id>` -> copy the new tree -> replace the manifest entry as a whole, with `"previous": {version, source, sha256|url+ref+commit}`, `"backup": "<path>"` and `"upgrade": {"from", "to", "state": "staged", "migrate": "not_started"}` -> delete the intent right after the manifest is written -> replay in this container (dependency install included) -> request `{action:"upgrade"}` -> Staged "<id> <to> is on the server. Waiting for the store to restart; ... Backup: <path>" -> delete `.older`. If any stage-1 step fails: the old tree and the old entry are restored, replay runs once more, and the card shows Failed "Upgrade failed: <reason>. The previous version is still in place." If the id carries an older intent: "Upgrade failed: <reason>. An earlier upgrade of <id> stopped half-way. Backup: ... - ...". If the restore itself fails: "... Putting the previous version back failed too - see Error Log. Backup: <path> - docs/self-hosting.md#roll-back-an-upgrade." and the intent file stays (this id cannot be upgraded again until it is sorted out); follow "An interrupted upgrade" below. A message with no backup to cite reads "No database backup was recorded - docs/self-hosting.md#extensions, ..." and carries no `Backup:`.
   - **Why the backup is not under `private/backups`**: before every backup frappe deletes temporary backups older than 23 hours, and its hourly maintenance keeps only `backup_limit` sets (default 3), so a rollback point there would be deleted within a day. `sites/.ext-backups/` is outside both cleanups. Each id keeps one: the current rollback point. It is deleted only when the next upgrade's stage 1 has put the new tree in place (when stage 1 fails and restores cleanly, what is deleted is the backup it just made), and the panel uninstall removes it together with `.previous/<id>`. Copy it elsewhere if you want to keep it longer.
   - **After a failed upgrade**: an entry whose `upgrade.state` is `failed` (a `staged` one counts too when the row is not busy and there is no intent: that is an outcome nobody recorded) refuses every version: the review card says "Cannot upgrade", and so do an Install request and a queued stage-1 job, with the same reason. With `migrate` = `succeeded`: "The upgrade of <id> to <to> updated the database but did not finish. Ask your developer to finish it (docs/self-hosting.md#roll-back-an-upgrade) before uploading again."; otherwise: "The upgrade of <id> to <to> did not finish. Ask your developer to roll it back (docs/self-hosting.md#roll-back-an-upgrade) before uploading again." First roll back as below under "Roll back an upgrade" (`succeeded` is finished as "The unrecorded success"); once the entry is back to having no `upgrade` (after a rollback) or `done` (after finishing), upload again. So each upgrade's rollback point is the backup it made itself and the tree it replaced. A busy row or a leftover intent is refused as before.
2. **Stage 2** (the agent): restart in the same order as an install -> wait for the backend marker -> `execute sideload.begin_upgrade --args '["<id>"]'` (first records the entry's `upgrade.migrate` as `started`; with no record, or if the manifest cannot be written, it errors and the agent does not run migrate; then it sets the card to Upgrading "Updating the database for <id> <to>. ... Backup: <path>") -> `bench --site <site> migrate` -> `execute sideload.finish_upgrade --args '["<id>", 1|0, "<message>"]'`. The arguments are generated on the host with `json.dumps(..., ensure_ascii=False)`: `bench execute` evals them, so ok is passed as 1/0, not true/false, and non-BMP characters must not be escaped as `\ud83d...` (after eval they would be lone surrogates and crash the database write). The message is **the last non-empty line** of the migrate output first (the exception line, which the card shows) plus up to 4 lines before it; the backend then cuts it to 300 characters. On success (1): first record `migrate: succeeded`, then sync the row, record `state: done`, Idle "upgraded a -> b"; if any later step fails, Failed "Upgrade failed after the database was updated (nothing to roll back; finishing it was not recorded): ...", `state: failed`, with `migrate` left at `succeeded`. On migrate failure (0): `state: failed`; if `migrate` was already `succeeded` it goes back to `started` (the core's after_migrate recorded it before the other apps' hooks, so that migrate did not really succeed); Failed "Upgrade failed while updating the database: ... Backup: <path> - docs/self-hosting.md#roll-back-an-upgrade", and the agent exits 1. When one of the agent's **own steps** fails (restart, waiting for replay times out, `begin_upgrade` cannot run), then as long as the backend can still run `bench execute`, it calls `finish_upgrade ["<id>", 0, "<reason>"]` the same way: `migrate` is still `not_started`, and the card shows Failed "Upgrade failed before the database was updated: <reason>. Backup: <path> - ..."; only if even that cannot run does the agent write done itself, and the panel shows the reason from done on the still-busy card ("Did not finish"). The card's before / while / after wording is decided by `migrate`, not by the row state. If `finish_upgrade` or the 30-minute sweep cannot write the manifest when recording `upgrade` (disk full), the card still shows Failed and Error Log gets an entry, and the entry stays as it was; afterwards every Refresh / `registry.sync` records a `staged` entry whose row is not busy and has no intent as `failed`.
   **Every migrate is recorded.** The core's `before_migrate` (`sideload.note_migrate_started`), before migrate touches the database, records an entry that is `staged` / `failed`, has `migrate` = `not_started` and whose `to` is exactly the version this process imports (or whose version cannot be read) as `started`; for one with an intent, if the imported version is not the intent's `from`, it adds `"migrate": "started"` to the intent; if it cannot write, migrate fails outright (the database has not been touched). The core's `after_migrate` `registry.sync`: frappe has just rewritten `Installed Applications.app_version` from the code that ran (after patches and schema sync, before after_migrate), so a record equal to `to` is recorded as `succeeded` (with no such row it looks at the imported version; a write failure only goes to Error Log and leaves `started`, the conservative side of the decision table). That is how a deploy's unconditional migrate (`auto_deploy.sh`) gets recorded, including the one after stage 1 was killed by a deploy, the agent never came, and a failure record sat for days. Only a migrate counts for `Installed Applications`: `install-app` / `uninstall-app` also rewrite it from whatever code is imported at the time, and that does not mean it migrated.
   Window: the restart (10-30 s) plus the migrate make the POS unavailable, so pick a time when the store is closed. During that time the panel's 3-second polling shows no dialog for 502 / 503 / 504 or a refused connection and carries on refreshing when the store is back.

### Roll back an upgrade

A manual recipe (there is no button yet; a Failed card's message cites this heading): the migrate failed, the agent did not finish, or a successful upgrade needs to go back to the old version. A panel refuses further uploads of an id whose upgrade did not finish, so sort it out here first. **The decision reads only these fields**: the manifest entry's `upgrade` and the intent file. Card wording, row state, `sites/.ext-request.done`, message prefixes and the tree's version number play no part.

**Stop the pipeline first, then read the fields** (do this in every case, including the ones that do not restore or roll back: the agent takes a request every 20 s, and a stage-1 job may still be running):
```bash
UP=./deploy/prod/up.sh; SITE=<site>; ID=<ext_id>; WHO=<your login name>; B=/home/frappe/frappe-bench
optional=$("$UP" ps --services --status running | grep -x queue-catalog || true)   # the opt-in local-catalog worker, if it runs: stop it and bring it back with the others (the scripts do the same)
systemctl --user stop ext-agent.timer                  # host: stop taking requests
systemctl --user is-active ext-agent.service           # must not be "activating" (= the agent is running: wait for it to finish, then check again)
journalctl --user -u ext-agent -n 5 --no-pager         # the last run has ended
$UP stop scheduler queue-short queue-long $optional    # scheduler and queues: a running stage-1 job ends with them, which is why the fields are read afterwards
```
The last step of the sort-out is `systemctl --user start ext-agent.timer` (recipe step 9; the scheduler and queues are brought up by step 8, after the migrate succeeds).

**The decision fields** (recipe step 1 prints the entry; the intent is `apps-ext/.previous/<id>.intent.json`):

| Field | Where | Values | If the field is missing |
|---|---|---|---|
| `state` | entry `upgrade` | `staged` / `failed` / `done` | — |
| `migrate` | entry `upgrade` | `not_started` / `started` / `succeeded` | `started` |
| `migrate` | intent | `started` | `not_started` |

- `migrate`: `not_started` = this upgrade's new code has not been run by any migrate, the database is untouched; `started` = it ran and did not confirm finishing (the database may be half-changed); `succeeded` = it finished. Stage 1 writes `not_started`; the agent's `begin_upgrade` (which changes only `not_started`) and the core's `before_migrate` write `started` before migrate touches the database; the core's `after_migrate` and a successful `finish_upgrade` write `succeeded` (`finish_upgrade` first checks that the migrate ran exactly `to`), and a failed `finish_upgrade` turns `succeeded` back into `started`. An entry with the field missing is a record from before the field existed and is read as `started`; before the restore, note in your sort-out record the version the database has (printed by step 1): equal to `to` means that migrate at least ran the patches and the schema. That does not change the decision: part of the patches may have been committed.
- `state` = `staged`: still `staged` after the pipeline was stopped = nobody recorded the outcome (the agent or the job died half-way and the record was not written); treat it like `failed`.
- If the entry's `upgrade` or the intent has `reused_rollback_point`, or the volume has `.previous/.<id>.replaced`: they were left by a removed "roll forward after a failure" feature (an earlier test build). The table below has no row for them -> stop and ask a developer.

| intent | `state` | `migrate` | restore (step 2) | code goes back to |
|---|---|---|---|---|
| none | `done` | any | do | `previous` |
| none | `staged` / `failed` | `succeeded` | skip | unchanged |
| none | `staged` / `failed` | `started` | do | `previous` |
| none | `staged` / `failed` | `not_started` | skip | `previous` |
| present | — | `started` | do | `from` |
| present | — | `not_started` | skip | `from` |

For the "present" rows `migrate` is read from the intent, and `—` = the entry is not read: follow "An interrupted upgrade" below. `previous` = the entry's `previous` (the version this upgrade replaced, the tree is `apps-ext/.previous/<id>` on the volume; `backup` was made just before replacing it, when the database was at that version); `from` = the intent's `from` (the version the database was at before this upgrade).
- `done`: the upgrade completed and there is nothing to sort out (if the card is Failed, it is a job that stopped earlier: check the version and set the card back to Idle, see after the recipe); follow this row **only to go back to the old version after a successful upgrade**.
- `succeeded` with "skip / unchanged" is not a rollback: finish up as in "The unrecorded success" below.
- The other rows without an intent follow the recipe below; "skip" for restore skips step 2 (so nothing written since the backup is lost).

Prerequisites:
- the pipeline is stopped (above);
- the manifest entry has `previous` and the volume has `apps-ext/.previous/<id>`; for a row that restores, the `backup` file exists (`sites/.ext-backups/<site>/<id>/...-database.sql.gz`, the absolute path after `Backup:` in the card message);
- there is no intent (if there is, read "An interrupted upgrade" first);
- the restore puts the **whole database** back to the moment of the backup: documents written after it are lost; **other extensions** installed / uninstalled / upgraded after it also go back in the database; so if you touched other extensions after the failure, deal with them before rolling back. If **core was deployed** after the backup, the core patches / schema the new image had already committed are also rolled back: the migrate in step 7 puts them back.

Order: stop the pipeline -> read the fields -> database -> code -> manifest -> check the version -> restart backend and wait for replay -> migrate -> start the remaining services -> release the pipeline (why the check comes before the migrate and the migrate after replay: see steps 5 and 7). The two gates in steps 5-9 are written into the commands, so pasting the whole block cannot get past them (see the comment above step 5).
```bash
# (UP / SITE / ID / WHO / B / optional were set above when the pipeline was stopped)
# 1) (pipeline stopped) the entry: `upgrade` holds the decision fields, backup = the rollback point's database backup, previous = the version to go back to; is there an intent; the version the database records
$UP exec -T backend bash -c "cd $B && env/bin/python -c 'import json,sys; print(json.dumps(json.load(open(\"sites/extensions.json\"))[\"extensions\"][sys.argv[1]], indent=2))' $ID; ls apps-ext/.previous/$ID.intent.json 2>/dev/null"
$UP exec -T backend bench --site $SITE execute frappe.db.get_value --args "['Installed Application', {'app_name': '$ID'}, 'app_version']"
# 2) restore (only for the rows that say "do"): the database goes back to the moment of the backup (the scheduler and queues are stopped, so the new code's scheduled tasks / background jobs do not run against the restored old database; the backend is still serving, so do not use the POS until step 8 is done). Run it interactively (no -T): at the super user prompt press Enter (= root) and paste DB_PASSWORD from deploy/prod/.env, which is not echoed and appears in no process's command line
$UP exec backend bench --site $SITE restore <Backup path>
# 3) put the code back: first delete this id's request if the agent has not taken it; the failed new tree (if there is one) moves to .failed/<id> (delete it after investigating), and .previous/<id> goes back in place
$UP exec -T backend bash -c "cd $B && { ! grep -qs '\"app\": \"$ID\"' sites/.ext-request.json || rm -f sites/.ext-request.json; }"
$UP exec -T backend bash -c "cd $B/apps-ext && test -d .previous/$ID && rm -rf .failed/$ID && mkdir -p .failed && { [ ! -e $ID ] || mv $ID .failed/$ID; } && mv .previous/$ID $ID"
# 4) the manifest entry = previous, with installed_at / installed_by added (written atomically with python; backup, upgrade and the other keys disappear with it)
$UP exec -T backend bash -c "cd $B && env/bin/python - $ID '$WHO' <<'PY'
import datetime, json, os, sys
p = 'sites/extensions.json'; app, who = sys.argv[1], sys.argv[2]; d = json.load(open(p)); e = d['extensions'][app]
prev = e.get('previous') or sys.exit('no previous in the entry: nothing to roll back to')
d['extensions'][app] = {**prev, 'installed_at': datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'), 'installed_by': who}
tmp = p + '.tmp'; json.dump(d, open(tmp, 'w'), indent=2); os.replace(tmp, p)
print(json.dumps(d['extensions'][app]))
PY
"
# The gates for steps 5-9 are in the commands: the [ "$rb" = N ] at the start of each line accepts only the token written when the previous step succeeded, and takes it before it runs (step 5's line clears it first). So when the versions differ in step 5, none of steps 6-9 runs; when the migrate in step 7 exits non-zero, steps 8 and 9 do not run; and once a line has not run, no later line runs. Pasting the whole block is the same: when the terminal has no bracketed paste, lines bash has not read yet are swallowed by the running $UP exec (docker compose exec forwards standard input); a swallowed line is only not executed, the lines after it cannot find the token, and pasting the unexecuted line again carries on. No exit is used (it would close an interactive terminal)
# 5) check (before any migrate, which is when the check means something): the version of the tree on the volume (a freshly started python process imports the tree step 3 put back) and the version Installed Applications records must be equal. Without an intent both should be previous; with one, both should be from. If they differ (or cannot be read) the wrong row was chosen -> STOP is printed, stop and find a developer, the pipeline stays stopped. It comes before the migrate because migrate rewrites Installed Applications from the code it actually imports; after it the two versions are always equal
rb=; code=$($UP exec -T backend bash -c "cd $B && env/bin/python -c 'import $ID; print($ID.__version__)'"); db=$($UP exec -T backend bench --site $SITE execute frappe.db.get_value --args "['Installed Application', {'app_name': '$ID'}, 'app_version']"); echo "5) tree on the volume $code · Installed Applications $db"; [ -n "$code" ] && [ "$code" = "$db" ] && rb=5 || echo "STOP 5) the versions differ or cannot be read = the wrong row was chosen: steps 6-9 do not run, the pipeline stays stopped, find a developer"
# 6) restart backend and wait for it to finish replay: replay wires the tree that was put back and installs its wheels/ into .deps/<id> (with no wheels/ it deletes the dependencies the new version brought). The scheduler and queues stay stopped; do not use the POS until step 8 is done
[ "$rb" = 5 ] && rb= && $UP restart backend && until $UP exec -T backend test -f $B/.ext-replay.done; do sleep 5; done && rb=6
# 7) migrate (every row runs it; it is a no-op when database and code already agree): if core was deployed after the backup, the restore rolled back the core patches / schema the new image had committed, and this puts them back from the current image's core plus the code that was put back. It comes after replay: when previous has wheels/ the dependencies must be installed first, otherwise frappe's remove_orphan_doctypes swallows the ImportError and deletes those DocType records and the Table-type Custom Fields that point at them (the exit code may still be 0). Non-zero = STOP is printed, find a developer, the scheduler / queues stay stopped (steps 8 and 9 do not run)
[ "$rb" = 6 ] && rb= && { $UP exec -T backend bench --site $SITE migrate && rb=7 || echo "STOP 7) migrate exited non-zero: the scheduler / queues and the agent timer stay stopped, find a developer"; }
# 8) start the remaining services (restart also starts stopped services; every container replays the tree that was put back; frontend re-resolves backend) -> sync the registry from the old code -> check the card state and the replay log
[ "$rb" = 7 ] && rb= && $UP restart websocket queue-short queue-long scheduler $optional && $UP restart frontend && rb=8
[ "$rb" = 8 ] && $UP exec -T backend bench --site $SITE execute ffl_core.extensions.registry.sync
[ "$rb" = 8 ] && $UP exec -T backend bench --site $SITE execute frappe.db.get_value --args "['POS Extension', '$ID', 'job_state']"
[ "$rb" = 8 ] && $UP logs --tail 40 backend | grep ext-replay
# 9) release the pipeline
[ "$rb" = 8 ] && rb= && systemctl --user start ext-agent.timer && rb=9
[ "$rb" = 9 ] && echo "9) steps 5-9 done, the pipeline is released" || echo "STOP steps 5-9 did not finish (stopped at the first STOP above or at an error), the pipeline stays stopped"
```
The replay log of step 6 should contain `<id> <old version> [...] built`; if the old version has `wheels/` it also says `<id> deps installed`, and if not `<id> has no wheels/ - removed its deps` (the dependencies the new version brought go with it). The step-2 restore brings the row back to its state **at the moment of the backup**: normally Idle; if the row was already hanging on an earlier failure (Failed) at backup time, it is still that old failure after the restore; without a restore the card is still this upgrade's Failed. After steps 5-9 have finished (`rb` = 9: the check passed and migrate exited 0), clear it with: `[ "$rb" = 9 ] && $UP exec -T backend bench --site $SITE execute ffl_core.extensions.jobs.set_state --args "['$ID', 'Idle']"`. The backup holds only the database: after a restore, documents written between the upgrade and the rollback are lost, so upgrade outside business hours and roll back quickly if it fails. The backup file stays in `sites/.ext-backups/<site>/<id>/` (deleted by the next successful upgrade or an uninstall); copy it elsewhere to keep it. The "roll back an upgrade (code layer)" case of `make ext-smoke` covers steps 3-4 and the code layer of step 6 (put back + replay + dependency cleanup); steps 2, 5 and 7 have no database in the smoke test, so verify them on a dev site.

### The unrecorded success

(decision table `staged` / `failed` + `succeeded`): the database has been migrated to `to`, only the outcome was not written (`finish_upgrade` could not run or could not write the manifest, a deploy's migrate ran in the agent's place, the agent died after the migrate). No restore and no tree swap: finish the upgrade (pipeline stopped):
```bash
# The gates are written like the recipe above: the [ "$rb" = ... ] at the start of each line accepts only the token the previous line left when it succeeded, and takes it first
rb=; $UP exec -T backend bench --site $SITE migrate && rb=migrated || rb=migrate-failed   # running it again is harmless and also completes a finish that was interrupted
# exit code 0 -> record success (sync the row, state done, card Idle, done file); only when it answers {"ok": true} start the scheduler / queues and release the pipeline; any other answer ("ok": false = the migrate ran something other than to, the card names both versions) -> STOP is printed, do not release the pipeline, stop and find a developer
[ "$rb" = migrated ] && rb= && case $($UP exec -T backend bench --site $SITE execute ffl_core.extensions.sideload.finish_upgrade --args "['$ID', 1, '']" | tee /dev/stderr) in '{"ok": true}') rb=finished ;; *) echo "STOP finish_upgrade did not answer {\"ok\": true}: the scheduler / queues and the agent timer stay stopped, find a developer" ;; esac
[ "$rb" = finished ] && rb= && $UP start scheduler queue-short queue-long $optional && systemctl --user start ext-agent.timer
# non-zero -> record the failure (migrate goes back to started) and leave the pipeline stopped: read the fields again = a `started` row, roll back with the recipe above (its steps 8 and 9 start the services and release the pipeline)
[ "$rb" = migrate-failed ] && rb= && $UP exec -T backend bench --site $SITE execute ffl_core.extensions.sideload.finish_upgrade --args "['$ID', 0, 'migrate failed again when finishing by hand']"
```

### An interrupted upgrade

(stage 1's job was killed hard: a worker restart / OOM / a deploy; or stage 1 could not even restore) - the sign: `apps-ext/.previous/<id>.intent.json` is still there (it is written after the row is Staged and before the first rename, and deleted right after the manifest is written; rewritten when a restore fails; and when a migrate has run the new code afterwards, the core's `before_migrate` adds `"migrate": "started"` to it). While the card is Staged the whole site's pipeline refuses; after a full 30 minutes a Refresh turns it into Failed and the pipeline is open again (this id is still refused for uploads), so sort it out early. **Do not restart containers yet**: a restart makes every container wire the tree on the volume, which may be half swapped. First stop as in "Stop the pipeline first, then read the fields" above, then follow the two "present" rows of the decision table:
1. Look at the intent (`from` / `to` / `backup` / `migrate`) and at the trees on the volume: `$UP exec -T backend bash -c "cd $B/apps-ext && cat .previous/$ID.intent.json; grep -H '^__version__' $ID/$ID/__init__.py .previous/$ID/$ID/__init__.py .previous/.$ID.*/$ID/__init__.py"`. The version numbers are only for recognizing which tree is which version; they do not take part in the decision.
2. If `migrate` = `started`, first restore the intent's `backup` (recipe step 2, interactively; this job made it just now, when the database was at `from`); with `not_started`, do not restore. Then put only `from` back in place by pasting the block below (paste it whole; pasting it again is harmless; gated like steps 5-9, and when it finishes it leaves the token `rb=2`, which step 3 accepts). If `apps-ext/<id>` is already `from` it does not move. If not (it is `to`, cannot be read, or is missing), `.previous/<id>` must be `from` (if not, STOP is printed and no tree moves), `apps-ext/<id>` (if present) moves to `.failed/<id>` (anything already there is deleted first) and `.previous/<id>` moves back to `apps-ext/<id>`; the earlier generation this job moved aside, `.previous/.<id>.older`, if present moves back to `.previous/<id>` (if `.previous/<id>` is still occupied = STOP). Every tree moved prints a line `2) ... -> ...`, and at the end it prints `2) done:` with the versions of the three places; a printed STOP = stop and find a developer, the pipeline stays stopped. If the manifest entry's `version` is `from`, leave the entry alone; if not, do recipe step 4 (entry = `previous`).
```bash
rb=; $UP exec -T backend bash -c "cd $B && env/bin/python - $ID <<'PY'
import json, re, shutil, sys
from pathlib import Path
app = sys.argv[1]; ext = Path('apps-ext'); tree, failed = ext / app, ext / '.failed' / app
prev, older, intent = ext / '.previous' / app, ext / '.previous' / ('.' + app + '.older'), ext / '.previous' / (app + '.intent.json')
if not intent.is_file():
    sys.exit('STOP 2) no %s: no interrupted upgrade to put back (step 3 ran already?), nothing moved' % intent)
frm = json.loads(intent.read_text())['from']
def version(t):
    init = t / app / '__init__.py'
    m = re.search(r'^__version__[ \t]*=[ \t]*\x22([^\x22]+)\x22', init.read_text(), re.M) if init.is_file() else None
    return m.group(1) if m else None
def move(src, dst):
    print('2) %s (%s) -> %s' % (src, version(src), dst))
    src.rename(dst)
if version(tree) != frm:
    if version(prev) != frm:
        sys.exit('STOP 2) from %s is neither %s (%s) nor %s (%s): nothing moved' % (frm, tree, version(tree), prev, version(prev)))
    if tree.exists():
        shutil.rmtree(failed, ignore_errors=True)
        failed.parent.mkdir(exist_ok=True)
        move(tree, failed)
    move(prev, tree)
if older.exists():
    if prev.exists():
        sys.exit('STOP 2) %s is there and so is %s: %s not moved' % (prev, older, older))
    move(older, prev)
print('2) done: %s %s | %s %s | %s %s' % (tree, version(tree), prev, version(prev), failed, version(failed)))
PY
" && rb=2 || echo "STOP 2) did not finish (no done line): the trees stay as printed above - stop and find a developer, the pipeline stays stopped"
```
3. Delete the intent and **this id's** unsent request (only if step 2 left the token): `[ "$rb" = 2 ] && rb= && $UP exec -T backend bash -c "cd $B && rm -f apps-ext/.previous/$ID.intent.json && { ! grep -qs '\"app\": \"$ID\"' sites/.ext-request.json || rm -f sites/.ext-request.json; }" || echo "STOP 3) nothing deleted: step 2 left no token (it has no done line, or this is not the same terminal) or the deletion failed - find a developer"` -> recipe step 5 check (both version numbers should be the intent's `from`) -> step 6 restart backend and wait for replay -> step 7 migrate -> step 8 start the services and check -> step 9 release the pipeline (steps 5-9 exactly as in the recipe, the gates are in the commands) -> set the card back to Idle (as above, `[ "$rb" = 9 ] && ... jobs.set_state`). The intent's `backup` is the backup this job just made; once the manifest does not reference it you may delete it.

### Troubleshooting a git install

Only public https repositories `https://<host>/<owner>/<repo>[.git]` plus a branch or tag name (`^[A-Za-z0-9._/-]{1,100}$`, not starting with `-`; a commit sha does not work, because `--branch` accepts only a branch / tag). A user name and password, ssh / git protocols, and hosts that resolve to internal / loopback / link-local addresses are all refused. The clone is `git clone --depth 1 --single-branch --no-tags` with `GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=/bin/false`: a **private repository fails at once** (it tells you to download a zip and Upload); a missing ref = git reports `Remote branch ... not found`; the sync takes at most 60 s, and exceeding the unpack limit = refused (use Upload for a big repository); if the image has no git (`$UP exec -T backend git --version`) the feature is unavailable. On success `.git` is removed and a zip is made, after which it is exactly like Upload; the manifest has `source: "git"` + `url / ref / commit` (so does `.report.json`), and upgrades go through the same panel upgrade.

### Troubleshooting dependencies

`up.sh logs backend | grep -E 'deps|ERROR'`: `<id> deps installed [stamp] (Ns)` (only when the wheels changed) / `<id> deps cached` / `ERROR: '<id>': installing its wheels/ failed: ...` (the container does not start: the package is broken; fix the package and reinstall, or roll back as above). To find where a dependency comes from: `$UP exec -T backend env/bin/python -c 'import <dep>; print(<dep>.__file__)'`; it should be under `apps-ext/.deps/<id>/` (under `site-packages` = the core ships it). To force a reinstall: `rm -rf apps-ext/.deps/<id>` and restart (replay reinstalls it, still `--no-index`). An uninstall (id removed from the manifest) or a rollback to a version without `wheels/` makes the next replay delete `.deps/<id>` and `zz-ext-deps-<id>.pth`; `.deps/<id>.tmp` / `.old` are an interrupted install (stopped between the two renames) and the next replay cleans them up and reinstalls. `'<id>': skipping wheels/<x>.whl - not a regular file` = the package contains a directory or symlink named `.whl`; the lint should have refused it and replay does not hand it to pip. Lint errors: a non-pure-Python tag -> only `py3-none-any` wheels can be built; `provided by core x.y, do not bundle` -> delete that wheel and use the core's (relax the version constraint if it does not fit); `not satisfied` -> add a wheel to `wheels/` or change the constraint; `Requires-Dist` likewise.

### Post-deploy switch assertion

`auto_deploy.sh` runs `ffl_core.extensions.registry.switch_snapshot / assert_switches` once before and once after migrate; if migrate flipped a switch, it is filled back, an Error Log entry is written and the deploy log gets a `!!` line.

### Replay preflight

Before wiring, `ext-replay.sh` runs `python -m compileall` on every declared extension; a syntax error = the container does not start and the log says `ext-replay: ERROR: '<id>' does not compile: <file>` (fix the file or reinstall the package, then restart). An esbuild error while compiling a bundle is the same: the container does not start, `ERROR: '<id>': bench build reported errors (exit N)`, and no built stamp is written (a production build makes esbuild kill itself with a non-zero exit; a `developer_mode` build only prints `There were some problems during build` and still exits 0; replay recognizes both). The version line `__version__ = "x.y.z"`: spaces or tabs on either side of `=` are fine, the same rule as the lint; single quotes are still refused.

### Running the lint alone

`make ext-lint DIR=<app directory or zip>` (inside the dev stack) or `python -m ffl_core.extensions.lint <dir|zip>` (any environment with ffl_core installed; stdlib only).

### Manual install

The fallback recipe when there is no agent; every step is idempotent.
```bash
UP=./deploy/prod/up.sh; SITE=<site>; ID=<ext_id>
optional=$("$UP" ps --services --status running | grep -x queue-catalog || true)   # the opt-in local-catalog worker, if it runs (the scripts do the same)
# 1) source onto the volume (the zip holds ONE top-level folder named <ext_id>, with pyproject.toml and <ext_id>/hooks.py inside it; a zip without that folder spills its files into apps-ext/)
$UP cp <ext_id>-<ver>.zip backend:/tmp/ext.zip
$UP exec -T backend bash -c "cd /home/frappe/frappe-bench && rm -rf apps-ext/$ID && env/bin/python -m zipfile -e /tmp/ext.zip apps-ext/ && test -f apps-ext/$ID/$ID/hooks.py"   # there is no unzip in the image
# 2) write the manifest (merge with python; do not edit the JSON by hand)
$UP exec -T backend bash -c "cd /home/frappe/frappe-bench && env/bin/python - <<'PY'
import json, os, datetime
p='sites/extensions.json'; d=json.load(open(p)) if os.path.exists(p) else {'schema':1,'extensions':{}}
d['extensions']['$ID']={'version':'<ver>','source':'upload','installed_at':datetime.datetime.now(datetime.UTC).isoformat(),'installed_by':'<who>'}
tmp=p+'.tmp'; json.dump(d,open(tmp,'w'),indent=2); os.replace(tmp,p)   # atomic write: a crash half-way cannot leave every container unable to start
PY"
# 3) replay (every service that runs the entrypoint) -> wait for backend to finish replay (the marker) -> install on the site -> smoke test
$UP restart backend websocket queue-short queue-long scheduler $optional frontend
until $UP exec -T backend test -f /home/frappe/frappe-bench/.ext-replay.done; do sleep 5; done   # do not get ahead of the entrypoint
$UP exec -T backend bench --site $SITE install-app $ID
$UP exec -T backend bench --site $SITE execute ffl_core.extensions.registry.sync   # fallback: install-app already syncs through after_app_install; no row = switched off (a panel Refresh works too)
$UP exec -T backend bench --site $SITE list-apps
```

### Manual uninstall

The order must not be reversed: `bench --site $SITE uninstall-app $ID` (on every site that has it) -> delete the id from the manifest (the same python as above, `del d['extensions']['$ID']`) -> `rm -rf apps-ext/$ID apps-ext/.built/$ID apps-ext/.previous/$ID apps-ext/.previous/.$ID.older apps-ext/.previous/.$ID.replaced apps-ext/.previous/$ID.intent.json sites/.ext-backups/<site>/$ID` (the panel uninstall clears these, and the previous / backup / upgrade in the entry, through `sideload.forget_rollback_point(<id>)`; a manual CLI uninstall **does not go through it**, so delete them by hand here; the entry goes with the deletion of the whole entry; copy the backup away first if you want to keep it) -> the same `restart` (the reconciliation removes the apps.txt line, the symlink, the .pth files and `.deps/<id>`). **To roll back this whole mechanism**: revert the PR that introduced it and redeploy; the volume can stay, it does no harm.

### Troubleshooting the replay

`up.sh logs backend | grep ext-replay` shows the state at a glance (`idle` / `built` / `cached` / `deps ...` / `ERROR`). `bench build` imports **every** app in `sites/apps.txt`, so one stale line that cannot be imported makes a store with extensions fail at every start. The reconciliation deletes the lines we left ourselves, the lines whose `apps/<x>` does not exist at all, and lines whose symlink **dangles** (it deletes the symlink too); a manual line that points at a real directory but cannot be imported is left alone, delete it by hand. The configurator rewrites the file from `ls apps` on every `up`, so one `up -d` usually cleans it.

### Verification

`make ext-smoke IMAGE=<image>` (= `deploy/prod/tests/test_ext_replay.sh` + `test_ext_agent.sh`; the CI build job runs it against the image it has just built, and the image is pushed only when it is green. Locally it can run against any registry tag and needs no database. It tests this checkout's `ext-replay.sh` (bind-mounted; the first case checks its sha256), while CI uses `EXT_TEST_BAKED=1` to test the copy baked into the image). The upgrade fixture: `python3 deploy/prod/tests/make_ext_hello_upgrade.py <out.zip>` generates ext_hello 0.2.0 on the spot (version / `change_log` / the idempotent patch `ext_hello.patches.v0_2.remember_greeting` / a pure-Python wheel `ext_hello_dep 1.0.0` built on the spot in `wheels/`), with 0 lint errors; after the upgrade's migrate has run the patch, `bench --site <site> execute frappe.db.get_default --args '["ext_hello_greeting"]'` prints `Hello from ext_hello_dep 1.0.0`. To check that an extension's static bundle is served: `curl -s https://pos.example.com/assets/<id>/dist/js/<id>.bundle.<hash>.js -o /dev/null -w '%{http_code}'`.

## Remote MCP connector (optional)

A remote MCP server lets AI assistants (Claude, ChatGPT and other MCP clients) work with the POS through its connector API (`ffl_core/api/connector.py`). It is **optional**. Its image is not built from this repository: build it from the public [opengunstore-mcp](https://github.com/Old-Steel-Arsenal/opengunstore-mcp) repository (or use the published image once available) and set `MCP_IMAGE=<registry>/<image>` in `deploy/prod/.env`; `auto_deploy.sh` passes it on to `deploy/prod/mcp/compose.yaml`, which falls back to its built-in default image when `MCP_IMAGE` is unset. `auto_deploy.sh` only warns when the servers are not healthy; it never fails the POS deploy.

- `deploy/prod/mcp/version.env` pins the MCP image tag that matches this POS version (`MCP_TAG=sha-<7>`); `deploy/prod/mcp/compose.yaml` defines two services (read-only on 8781 / full on 8782, host networking, bound to 127.0.0.1 only).
- Two optional keys in `deploy/prod/.env` steer this step: `MCP_TAG=<tag>` runs that image tag instead of the one in `version.env`, and `MCP_ENABLED=0` skips the whole MCP step (no `mcp/.env`, existing containers are left as they are). `.env.example` sets `MCP_ENABLED=0`, so a new install never pulls an MCP image; to run the connector, remove that line (or set it to anything but 0) and set `MCP_IMAGE`. With the key unset, the pinned tag is deployed.
- After the POS deploy + migrate, `auto_deploy.sh` writes `deploy/prod/mcp/.env` (site URL, the local frontend port, MCP_TAG, MCP_IMAGE when you set one, and two switches read from the site: the GunBroker write tools follow GunBroker Settings, the Range board tools follow whether the Range extension is on) and runs `up -d`, then waits for both containers to be healthy. A failure does not fail the POS deploy but prints a `::warning::` in the log.
- **Upgrading the MCP server**: build and test a new image in the MCP server's own repository, then open a PR here changing `version.env`; it ships with the next POS release. Rolling the POS back also puts the MCP server back to the matching version.
- The switches are in the POS's **MCP Settings** (Read-only / Full separately, tied to OAuth Settings); every call is recorded in the **Connector Audit Log**.
- Caddy: the site block for `pos.example.com` needs the four routes in `deploy/prod/caddy-connector.caddy` (the shared Caddyfile is edited by hand).
- To look: `docker compose -f ~/gunstore-pos/deploy/prod/mcp/compose.yaml ps`.
