# Data Service

The **Data Service** is an optional, hosted service that the POS reads for shared data: the cross-distributor product catalog, offers, a national FFL directory, and an extension update feed. The POS is fully usable without it. This page says what works without it, what needs it, and how to run a **local catalog** instead of the hosted one.

The connection lives in **Data Service Settings** (endpoint, client id, secret; Test Connection records the contract version the service reports). Credentials are issued by whoever operates the service you use. The service speaks the published [catalog contract](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/docs/catalog-api/README.md), so any service that passes the conformance runner described there can be used.

## What works without it

With Data Service Settings left blank, the POS runs as a normal store system:

- the counter (Create Order, Point of sale), receiving, inventory, serial numbers, storage locations, the cash drawer and reports;
- the A&D book and 4473 through FastBound, local ATF FFL records, and live FFL verification through the **ATF Form Scraper** provider (the default in FFL Settings);
- WooCommerce sync of items you create yourself, GunBroker, Payroc card payments, ShipStation;
- the distributor accounts (RSR, Sports South) as ordering providers: credentials, ordering switches and freight settings are on their own settings pages;
- extensions installed by upload or git address, and extensions that are already on the server.

## What needs it: capabilities

What the POS can do is decided in one place, `ffl_integrations.data_service.capabilities()`, from the settings below. Every screen, endpoint and scheduled job follows it; nothing guesses on its own. The desk reads it as `frappe.boot.ffl_capabilities`, and pages that refresh call `ffl_integrations.data_service.get_capabilities`. The answer is cached for the request and dropped when Data Service Settings, FFL Settings or a Distributor is saved, or an extension is switched on or off (the desk's cached boot is dropped after the save commits). A capability whose settings cannot be read (a database hiccup, including the catalog source itself) is `unknown`: not off, so no notice, no refusal and no clean-up; the scheduled jobs skip that tick. Asking for a capability name that does not exist is a programming error (`ValueError`), never "off".

| Capability | Values | On when | When it is off |
|---|---|---|---|
| `catalog` | `data_service`, `local`, `off` | Catalog source is **Local** (`local`), or the Data Service connection is filled in, or a distributor you buy from reads its own complete catalog service (endpoint, client id, secret) (`data_service`). Each catalog read is also judged on its own (`capability("catalog", house=...)`): a read for one distributor goes where the client sends it (the first Distributor row for that distributor, by priority and including disabled rows, that names an endpoint; else Data Service Settings), and a read across distributors (the counter's unnamed search, product batches, the change feed) needs Data Service Settings itself. A read that would fail is off, quietly, before anything is sent | Counter item search, Receive Goods, Distributor Orders › Search Catalog, the Distributor hub and the Item form's catalog lookup show the notice **"Distributor catalog is not set up · Set it up"** (it links to Data Service Settings, where Catalog source offers Local). The local search (Items and serials in the POS) still runs; only the distributor part shows the notice. Endpoints that need a catalog row (a quote for a named house, the drop-ship item ensure, promoting a catalog item, the Item form's catalog lookup) answer HTTP 417 with that message. The hub shows "Catalog: off" on each card whose reads are off, not an error. The feed watchdog stays silent on a site with no enabled drop-ship distributor; an enabled drop-ship distributor that has a feed (one that reads the catalog) but no catalog is stale by definition: the admins are alerted and its zero-stock setting applies, as for a feed that aged out. A drop-ship distributor ordered by phone (no feed) is skipped, catalog or not. |
| `category_tree` | `data_service`, `off` | The Data Service connection is filled in and the catalog source is not Local (only the Data Service ships a tree) | The category mirror does not run. On its first tick after the tree goes off it clears its own stale report and issue counter once, then writes nothing. **Run now** on the Categories tab answers 417. Stores keep their own Item Groups. |
| `ffl_national_search` | `data_service`, `not_chosen`, `off` | `data_service`: the connection is filled in and FFL Settings › live check provider is **Data Service**. `not_chosen`: connected, but another provider is chosen | `off` (no connection): the ATF FFL search (Customer and Supplier forms) and the counter's business-buyer FFL field search only this store's ATF FFL Records and say "National FFL search is not set up" (connect the Data Service, then choose it in FFL Settings). `not_chosen` searches local records as before, with no notice. Verifying an FFL (EZ Check) is not part of this: it follows the provider chosen in FFL Settings. |
| `extension_updates` | `data_service`, `off` | The Data Service connection is filled in | No update cards and no error; the Extensions panel footer says **"Update service not configured"**. Uploads and git installs work as always. |
| `ai` | `connected`, `off` | An installed AI extension answers the `gunshop_ai_connected` hook ([hooks](extensions/hooks.md)) | The extension has nothing to talk to; the core never imports it. |

The product change feed (re-filing catalog Items from `/products/changes`, Categories mode **Apply**) needs only `catalog`, so it runs on the Data Service and on a Local catalog. With a Local catalog the products carry no master category or legal answer: the follower compares the feed's own firearm answer (the distributor's FFL flag, then the class) with the Item and reports a difference for the owner, exactly as it reports a master legal change — once per conclusion: a price or quantity change does not report it again. A conclusion counts as reported only once its Error Log entry and that page of the feed are committed; a failure before then reports it again. Only Items that are not serialized and not filed under a master category (or that have a difference on record) are checked, with one batched read of their offers a page; the reported conclusions are one cache value, read once a tick and written at most once a page. It never changes `is_firearm` or a firearm group by itself.

**The rules every surface follows**

- **Screens** use one shared notice (`ffl.capabilities.notice`, `ffl_core/public/js/capability_notice.js`) instead of an empty "No matches.", and check the capability before sending a request the server would refuse. A 417 `CapabilityOff` from any endpoint shows the same notice (other messages in the answer still show, from either response shape, and the caller's error callback still runs); a call made with `silent: true` shows nothing.
- **Endpoints** that cannot work without a capability raise `CapabilityOff` (a ValidationError, HTTP 417) with that capability's message, never a 500. The catalog selector (`remote_catalog._backend`) raises the same exception quietly, so with the catalog off no call reaches `data_service/client.py`; a test checks that every caller of the client goes through `require()` or the selector.
- **Scheduled jobs** check the capability first and return without writing when it is off. Each job clears its own stale state once, on a confirmed off only (the category mirror: its report and issue counter).
- **Error Log**: nothing is logged on every tick for an off capability. A log is written once when something that is set up stops working, as before.

The ATF Form Scraper, Manual Only and Data Service choices in FFL Settings are independent of the catalog source below.

## Local catalog

A store that does not use the hosted service can run its own distributor catalog. The POS then downloads each distributor's feeds with the store's own credentials and serves the same catalog reads from its database, so quoting, ordering, drop-ship and listing sync work on top of it. No call for catalog data reaches the Data Service in this mode; FFL search and the update feed still use it if you also fill in the connection.

Local mode is **experimental**: it is meant for a new self-hosted store, and live stores use the Data Service. Its feed drivers cover RSR, Sports South and Lipsey's, and the limits below (strict legal answers, no antique list, new installs only) are part of what makes it experimental. A store that depends on its catalog should stay on a Data Service until Local mode has run in production.

**Set-up**
1. In `deploy/prod/.env` set `COMPOSE_PROFILES=catalog` and run `./deploy/prod/up.sh up -d`. This starts the `queue-catalog` worker (the dedicated `catalog` queue). Without it the Distributor hub warns that the catalog worker is not running instead of silently not syncing. The upgrade scripts and the extension agent restart the worker with the others only while it is running; the recipes in the [self-hosting guide](self-hosting.md) do the same.
2. In **Data Service Settings › Catalog source**, choose **Local** (new installs only; a site can later move from Local to a Data Service, see Limits).
3. In **RSR Settings › Account › Catalog feed** (shown only in Local mode): FTP host (default `ftp.rsrgroup.com`), port (default `2222`), **Key dealer** (tick it when RSR set the account up as a key dealer: the files are read from `keydealer/`), FTP user name and password (RSR issues them with the account), and **Skip TLS certificate check** (leave it off; ticking it exposes the login to anyone in the network path).
4. In **Sports South Settings › Account**: account number, API user name (blank means the account number), API password and API base URL (the default is right for most stores). This is the same login as ordering; the catalog reads Sports South's inventory web service with it.
5. In **Lipseys Settings › Account**: the dealer login (email and password), the customer number (optional; when set, the login must answer for that account) and the API base URL (the default is right). This is the same login as ordering; the catalog reads Lipsey's catalog and pricing feeds with it. Lipsey's only answers from an address on its allowlist: ask Lipsey's to add the store's server address.
6. Each house needs an enabled Distributor row (provider `rsr`, `ss` or `lipseys`). The scheduler syncs a house only when that row is enabled and its feed login is filled in (RSR: the FTP user name; Sports South: the account number; Lipsey's: the dealer email).
7. **Distributor hub › Test Connection** adds a "Catalog feed (FTPS)" row for RSR, a "Catalog feed (inventory.asmx)" row for Sports South and a "Catalog feed (dealer login)" row for Lipsey's.

**What syncs, and when**

| House | Lane | Schedule | Notes |
|---|---|---|---|
| RSR | catalog | every 2 hours, heavy | reads the deleted list first |
| RSR | deleted | with the catalog | |
| RSR | quantity | every 15 minutes | |
| RSR | restrictions | every 2 hours, heavy | an unchanged file is skipped |
| RSR | case_price | daily | full-case price per unit and case size from Direct Connect get-items (dealer account); skipped while RSR Settings has the API off or no dealer login, and in RSR's 3–5am ET maintenance window |
| Sports South | quantity | every 15 minutes | incremental from the server's own time; when the buffer is idle, the full on-hand list at most every 30 minutes |
| Sports South | snapshot | weekly full rebuild, heavy | a row missing from it is marked discontinued; a free-disk check of about 900 MiB before each large download |
| Sports South | catalog | daily changes with a one-day overlap, after the first snapshot | |
| Sports South | text | daily long descriptions, heavy | |
| Lipsey's | catalog | about every 4 hours, heavy | the full item list (about 48 MB of JSON), after a free-disk check; a live row missing from it is marked discontinued; stock, cost and allocation are left to the quantity lane |
| Lipsey's | quantity | about every hour, after the first catalog | stock, cost and allocation for every item; a live item the file omits gets 0 |

Lipsey's lanes are timed from their last attempt, not their last success: a failing lane waits twice as long after each failure in a row (the catalog up to 24 hours, the quantity lane up to 8 hours), and a lane killed mid-run counts as a failure. When Lipsey's refuses the login, both lanes stop dialing until **Lipseys Settings** is saved again (the save is the retry). Sync now still waits 30 minutes (catalog) or 15 minutes (quantity) after the last attempt.

**Distributor hub**

Each card shows the local catalog's offer counts and, per lane, its last success, rows and a failed last run. **Sync now** (System Manager only) queues every lane of that house on the catalog queue now. It skips the schedule only, never a guard: a short, truncated or shrunk file is refused exactly as on a scheduled run (snapshot floor of 40,000 rows, more than 2% of rows vanishing, the catalog's half-size rule, the restrictions half-size rule). Sports South's weekly snapshot runs only when due.

Lipsey's guards: a catalog under 12,900 rows, or with more than 1% of its rows unreadable (no item number, no type, or a missing FFL / NFA flag), is refused before any row changes. When more than 2% of the live rows vanish from a catalog, the file still loads but no row is retired and the run is recorded as failed until a forced run applies the diff. A pricing file whose item numbers match the catalog for fewer than 90% of its rows, or that leaves out more than 10% of the live rows, is refused.

An administrator who has checked that a smaller restrictions file is real loads it from bench:

```
bench --site <site> execute ffl_integrations.catalog_local.sync.run_lane --kwargs "{'code': 'rsr', 'lane': 'restrictions', 'force': True}"
```

The same call with `{'code': 'ss', 'lane': 'snapshot', 'force': True}` rebuilds the Sports South snapshot now, and with `{'code': 'lipseys', 'lane': 'catalog', 'force': True}` loads a Lipsey's catalog and retires the rows that vanished from it after an administrator has checked that the drop is real (a forced run does not lift a refused login: save Lipseys Settings for that).

**Box minimum:** 2 GB of RAM for the catalog worker and the database on top of the rest of the stack, and 2 GB of free disk for the downloaded feeds and the catalog tables.

**Classification: the Data Service's rules, copied**

**RSR.** Requires FFL = the FFL column of RSR's department table (01, 02, 03, 05, 06, 41, 42, 43); a blank or unlisted department is FFL-required (stricter than the Data Service). Class: department 06 is NFA, and so is an FFL row whose title names an SBR, SBS, AOW, suppressor or silencer, short-barreled rifle or shotgun, or any other weapon or machine gun (not "suppressor ready" or suppressor-height sights, threaded, uppers, barrels, flash suppressors); 01/02 Handgun; 03/05 Long Gun; 18 Ammunition; 41/42/43 Gun Parts; unflagged 32/34 Gun Parts; other FFL rows Other Firearm; the rest Accessory.

**Sports South.** Requires FFL = Sports South's own item type (1 or 2 = a firearm). Class, the class the Data Service serves with its owner rules included: category 31 (lights) is Accessory whatever its flag or title; a flagged category 93 row whose title says pistol or pistols (the whole word) and names no NFA item is Handgun; any other category 93 row with the flag is NFA; any other FFL row with an NFA title is NFA; 26/64/96 Handgun; 40/48/57/58/59/94 Long Gun; 65 Other Firearm; 63/66/67 with the flag Other Firearm; 8/63/66/67/93 without it Gun Parts; other FFL rows Other Firearm; ammunition categories (4, 5, 6, 49, 69, 70, 71, 72, 90, 91) Ammunition; the rest Accessory.

**Lipsey's.** Requires FFL = Lipsey's own `fflRequired` or `sotRequired` flag, and every Muzzleloader and Revolver row (see below). The department is Lipsey's `type`, ignoring case and spacing; an NFA row (`sotRequired`) filed outside the four NFA types is filed under "NFA - Other". Class, from the Data Service's reviewed type map: an FFL row in an NFA type, or with an NFA title, is NFA; other FFL rows follow the type's firearm node: Semi-Auto Pistol, Specialty Handgun and Revolver Handgun; Rifle, Shotgun and Combo Long Gun; Other Firearm, Muzzleloader, the receiver types (Lowers, Frames, Receiver Sets, Actions, Uppers) and any other type Other Firearm. Rows without the flag: the parts types (barrels, bolts, triggers, stocks, muzzle devices, slides and the like) Gun Parts, Accessory-Ammunition Ammunition, everything else Accessory. A type missing from the map is Other Firearm with the flag and Accessory without it. Images link Lipsey's own image URL; nothing is downloaded.

**Owner rules.** Local mode applies the Data Service's two Sports South owner rules to the class, so a row's class is the one the Data Service serves: category 31 (lights) holds no guns, so a flagged light is an Accessory, its product's legal answer is "not a firearm", and it still ships only to a dealer (its flag stands); a flagged pistol-titled row filed in category 93 is a pistol (Handgun), not an NFA item, unless its title also names an NFA item. The Data Service also files products in a curated category tree that is not published. Where that tree would name a different firearm class (a rifle filed in a parts category), Local mode keeps the department's class; the legal answer (firearm or not) is the same.

**Deliberate deviations (both stricter than the Data Service)**

1. In RSR's upper and conversion-kit departments (41/42/43) an FFL-flagged row whose title names a receiver, lower, frame or barreled action is a firearm here (Other Firearm, or NFA when the title also names an NFA item). The Data Service keeps every row of those departments a part (its owner rule says they hold no guns). Selling a part as a firearm costs a 4473, and selling a firearm as a part is unlawful, so Local mode keeps the strict answer.
2. Every Sports South black-powder gun in category 13, and every Lipsey's Muzzleloader and Revolver row, is FFL-required here, and no product is marked antique. The Data Service releases a confirmed antique or black-powder gun from a list it keeps; Local mode has no such list, so such a gun ships to a dealer with a 4473 instead of to the buyer.

**Limits of the first version**
- **Data Service → Local: new installs only.** A site that already syncs from the Data Service is not switched over: the form refuses that change once the site holds a product-change cursor, because the two sources number product changes differently.
- **Local → Data Service: supported.** Fill in the endpoint, client ID and secret first (the form refuses the switch without them, so the catalog never goes off), then choose **Data Service** and save. Everything the local catalog held is a copy of the distributors' feeds, so nothing is lost. On save every registered product-change cursor is cleared: the category follower takes a fresh baseline on the service, and a drop-ship listing sync reads the service's feed from its start, so each listing is re-synced once from the new source. The local `dc_*` tables are left in place and no longer read; the catalog worker can be stopped (drop `catalog` from `COMPOSE_PROFILES`). Save the switch while no background job is running (pause the scheduler with `bench --site <site> disable-scheduler`, let running jobs finish, switch, then `enable-scheduler`): a drop-ship sync that started on the local catalog would otherwise write its old position back after the reset. The form probes the service on save and keeps the site on its local catalog if the service does not answer.
- **No category tree.** The mirror stays silent and stores use their own Item Groups; with Categories mode Apply the product change feed still reports FFL-flag changes on catalog Items.
- Not included: Sports South's per-item text backfill, a confirmed-antique list for black-powder guns, national FFL search, retailer MAP and the update feed.

## The catalog contract

The catalog is read through a small published contract: [catalog-api/README.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/docs/catalog-api/README.md) (semantics, authentication, conformance runner) and [catalog-api/openapi.json](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/docs/catalog-api/openapi.json) (shapes). A third-party catalog service, the local catalog and the hosted service all implement it.
