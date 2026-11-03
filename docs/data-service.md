# Data Service

The Data Service is an optional HTTP service the POS reads for shared data: the cross-distributor product catalog, offers, the national FFL directory and the extension update feed. The POS works without it.

- **Connection:** Data Service Settings (endpoint, client id, secret). Test Connection records the contract version the service reports. Credentials come from whoever runs the service.
- **Contract:** [catalog-api/README.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/develop/docs/catalog-api/README.md) (semantics, authentication, conformance runner) and [catalog-api/openapi.json](https://github.com/Old-Steel-Arsenal/opengunstore/blob/develop/docs/catalog-api/openapi.json) (shapes). Any service that passes the conformance runner can be used; the hosted service and the local catalog implement the same contract.
- **Client:** `ffl_integrations/data_service/client.py`. Every caller goes through `require()` or the catalog selector (a test enforces it).

## Without it

The counter, receiving, inventory, serials, storage, the cash drawer and reports; the bound book and 4473 through FastBound; local ATF FFL records and live FFL checks through the ATF Form Scraper provider; WooCommerce, GunBroker, Payroc, ShipStation; distributor accounts as ordering providers; extensions by upload or Git.

## Capabilities

One function decides what the POS can do: `ffl_integrations.data_service.capabilities()`. The desk reads it as `frappe.boot.ffl_capabilities`; pages that refresh call `ffl_integrations.data_service.get_capabilities`. It is cached per request and dropped when Data Service Settings, FFL Settings or a Distributor is saved, or an extension is switched. A capability whose settings cannot be read is `unknown`: not off, so no notice, no refusal, no clean-up, and scheduled jobs skip that tick. An unknown capability name raises `ValueError`.

| Capability | Values | On when | Off |
|---|---|---|---|
| `catalog` | `data_service`, `local`, `off` | Catalog source is **Local**; or the connection is filled in; or a distributor reads its own complete catalog service. Judged per read: a read for one house goes to the first Distributor row (by priority, disabled rows included) that names an endpoint, else Data Service Settings; a read across houses needs Data Service Settings | Catalog searches show "Distributor catalog is not set up · Set it up"; endpoints that need a catalog row answer 417; the hub shows "Catalog: off" per card |
| `category_tree` | `data_service`, `off` | Connected and the source is not Local | The category mirror does not run; **Run now** answers 417 |
| `ffl_national_search` | `data_service`, `not_chosen`, `off` | `data_service`: connected and FFL Settings › live check provider is **Data Service**; `not_chosen`: connected, another provider | FFL searches use this store's ATF FFL Records only ("National FFL search is not set up"); EZ Check follows FFL Settings regardless |
| `extension_updates` | `data_service`, `off` | Connected | No update cards; the Extensions footer says "Update service not configured" |
| `ai` | `connected`, `off` | An installed extension answers `gunshop_ai_connected` ([Hooks](extensions/hooks.md#gunshop_ai_connected)) | — |

Rules every surface follows:

- **Screens** use the shared notice (`ffl.capabilities.notice`, `ffl_core/public/js/capability_notice.js`) and check the capability before sending a request the server would refuse. A 417 `CapabilityOff` shows the same notice; `silent: true` shows nothing.
- **Endpoints** raise `CapabilityOff` (a `ValidationError`, HTTP 417), never a 500.
- **Scheduled jobs** return without writing when off, and clear their own stale state once on a confirmed off.
- **Error Log:** nothing per tick for an off capability; one entry when something set up stops working.

## Local catalog

An experimental alternative to the hosted catalog for a new self-hosted store: the POS downloads each distributor's feeds with the store's own credentials (RSR over FTPS, Sports South's inventory web service, Lipsey's dealer API) into local tables and serves the same catalog reads. FFL search and the update feed still need the Data Service.

1. `COMPOSE_PROFILES=catalog` in `deploy/prod/.env`, then `./deploy/prod/up.sh up -d` (starts the `queue-catalog` worker).
2. **Data Service Settings › Catalog source** = **Local** (new installs only).
3. The feed logins: **RSR Settings › Account › Catalog feed**, **Sports South Settings › Account**, **Lipseys Settings › Account**, and an enabled Distributor row per house (`rsr`, `ss`, `lipseys`).
4. **Distributor hub › Test Connection** shows a catalog-feed row per house; **Sync now** (System Manager) queues every lane of a house without skipping any guard.

Budget 2 GB of RAM and 2 GB of disk on top of the stack. Classification copies the Data Service's rules, with two deliberate deviations, both stricter: FFL-flagged receivers in RSR's upper and conversion-kit departments are firearms, and every black-powder / muzzleloader row is FFL-required (no antique list). Moving from Local to a Data Service is supported (fill in the connection first; every registered change-feed cursor is cleared); the other way is refused once the site holds a cursor. Lanes, schedules and guards are in `ffl_integrations/catalog_local/` (`sync.py`, `drivers/`).
