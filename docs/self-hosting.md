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

Extensions are not baked into the image. Their code lives on the `apps-ext` volume and is declared in `sites/extensions.json`; at every container start the entrypoint runs `deploy/prod/ext-replay.sh`, which wires each declared extension back into the bench. Recreating containers or swapping the image keeps them.

- **Install, upgrade, uninstall:** from Settings → Extensions, no SSH ([Lifecycle](extensions/lifecycle.md)). An upload is staged by a background job; the **extension agent** on the host then restarts the stack and finishes the install. Install the agent once per server:

    ```bash
    cd ~/gunstore-pos && mkdir -p ~/.config/systemd/user
    cp deploy/prod/ext-agent.service deploy/prod/ext-agent.timer ~/.config/systemd/user/
    systemctl --user daemon-reload && systemctl --user enable --now ext-agent.timer
    loginctl enable-linger $USER
    ```

    Without it an upload stays Staged and turns Failed after 30 minutes.
- **One job at a time.** While any extension is installing, staged, upgrading or uninstalling, the page refuses new work.
- **Upgrades** take a database backup (`sites/.ext-backups/<site>/<id>/`) and keep the previous tree (`apps-ext/.previous/<id>`). A failed upgrade blocks further uploads of that id until it is rolled back by hand.
- **Replay failures stop the container** (a restart loop, `ERROR` lines under `ext-replay` in `up.sh logs backend`). Recovery is to put the files back, never to delete the manifest entry alone.
- **Deploys:** `auto_deploy.sh` waits for replay, migrates and asserts that no extension switch changed.

Everything else (the files on the volume, the agent's stages, manual install and uninstall, troubleshooting, the smoke test) is in the [Extensions runbook](extensions-runbook.md).

### Roll back an upgrade

The rollback decision table and recipes (including an unrecorded success and an interrupted upgrade) are in the runbook: [Roll back an upgrade](extensions-runbook.md#roll-back-an-upgrade).

## Remote MCP connector (optional)

A remote MCP server lets AI assistants (Claude, ChatGPT and other MCP clients) work with the POS through its connector API (`ffl_core/api/connector.py`). It is **optional**. Its image is not built from this repository: build it from the public [opengunstore-mcp](https://github.com/Old-Steel-Arsenal/opengunstore-mcp) repository (or use the published image once available) and set `MCP_IMAGE=<registry>/<image>` in `deploy/prod/.env`; `auto_deploy.sh` passes it on to `deploy/prod/mcp/compose.yaml`, which falls back to its built-in default image when `MCP_IMAGE` is unset. `auto_deploy.sh` only warns when the servers are not healthy; it never fails the POS deploy.

- `deploy/prod/mcp/version.env` pins the MCP image tag that matches this POS version (`MCP_TAG=sha-<7>`); `deploy/prod/mcp/compose.yaml` defines two services (read-only on 8781 / full on 8782, host networking, bound to 127.0.0.1 only).
- Two optional keys in `deploy/prod/.env` steer this step: `MCP_TAG=<tag>` runs that image tag instead of the one in `version.env`, and `MCP_ENABLED=0` skips the whole MCP step (no `mcp/.env`, existing containers are left as they are). `.env.example` sets `MCP_ENABLED=0`, so a new install never pulls an MCP image; to run the connector, remove that line (or set it to anything but 0) and set `MCP_IMAGE`. With the key unset, the pinned tag is deployed.
- After the POS deploy + migrate, `auto_deploy.sh` writes `deploy/prod/mcp/.env` (site URL, the local frontend port, MCP_TAG, MCP_IMAGE when you set one, and two switches read from the site: the GunBroker write tools follow GunBroker Settings, the Range board tools follow whether the Range extension is on) and runs `up -d`, then waits for both containers to be healthy. A failure does not fail the POS deploy but prints a `::warning::` in the log.
- **Upgrading the MCP server**: build and test a new image in the MCP server's own repository, then open a PR here changing `version.env`; it ships with the next POS release. Rolling the POS back also puts the MCP server back to the matching version.
- The switches are in the POS's **MCP Settings** (Read-only / Full separately, tied to OAuth Settings); every call is recorded in the **Connector Audit Log**.
- Caddy: the site block for `pos.example.com` needs the four routes in `deploy/prod/caddy-connector.caddy` (the shared Caddyfile is edited by hand).
- To look: `docker compose -f ~/gunstore-pos/deploy/prod/mcp/compose.yaml ps`.
