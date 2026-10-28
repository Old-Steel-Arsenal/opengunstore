# Catalog API — contract v1.1

The POS never reads a distributor's feed itself. It reads a **catalog
service** over HTTPS, and this document is the contract that service must
honour. A hosted Data Service implements it, and so does the POS's built-in local catalog
(`docs/data-service.md`); anyone can stand up their own — for another distributor, a scraper, or a
CSV — and the POS will use it without a code change once it passes the
conformance runner below.

Machine-readable shapes: [`openapi.json`](./openapi.json) (carved from the
reference service; the schemas there are normative). This page carries the
semantics the schemas cannot.

## Scope

Read-only. The contract covers **what the catalog says**: products, offers
(one distributor's line on a product), stock, prices, ship restrictions and
feed freshness. **Ordering is not part of it** — placing, polling and
cancelling orders stays a per-distributor Python provider inside the POS
(`ffl_integrations.distributor.base.DistributorProvider`), because ordering
APIs differ far more than catalogs do.

## Identifiers

| Term | Meaning |
|---|---|
| `distributor` (code) | Short lowercase code for one house, e.g. `rsr`, `ss`. Stable; it keys credentials, cursors and the POS's Distributor row (`Distributor.provider`). |
| `sku` | The house's own item number, unique **within** a distributor. The order line identity on that house. |
| `upc` | 12–14 digit GTIN as a string, or `null` when the house has none. The cross-house identity: products are grouped by UPC; the POS names Items by UPC. Leading zeros must be preserved. |

## Endpoints (all under `/v1/catalog`, scope `catalog:read`)

| Endpoint | Purpose | POS reader |
|---|---|---|
| `POST /search` | Typeahead. Returns **products** (`ProductHit`, one per UPC) with every house's `offers[]` and a `best_offer`. `distributor` scopes to one house; omitted = every house. | counter search, MCP |
| `POST /rows` | Up to 200 SKUs of one house → `OfferFull` rows. Unknown SKUs are simply absent (never an error). | quote, planner prefetch, listing sync |
| `GET /item/{upc}` | One product with all its offers. `404` for an unknown UPC. Weak `ETag` + `Cache-Control` welcome, not required. | counter ensure / matrix by UPC, MCP |
| `GET /restrictions/{distributor}/{sku}` | Ship restrictions for one SKU as raw entries; `entries: []` when none (a **200**, never a 404). | quote red line, web-order panel |
| `GET /health?distributor=` | Feed heartbeats, catalog version, row counts, `contract_version`. | watchdog, hub, catalog service page |
| `GET /products` | One product per key (the UPC, or `<house>:<sku>` for an offer with no UPC), with every house's offers. Filters: `q`, `brand`, `product_class`, `department` as `<house>:<department>`, `distributor`, `in_stock`, `min_cost` / `max_cost`, `ffl_required`. Sort by key with an opaque `cursor`, or by brand / cost / qty with `offset`. `count` on a first page. | Drop-ship Center, catalog Items |
| `POST /products/batch` | Products for a list of keys (≤200). Unknown keys are absent; a renamed key is reported under `keys`. | legal answer, listing sync |
| `GET /products/changes` | Product keys changed after an opaque `since` cursor, oldest first, with `cursor` and `more`. A local catalog's cursor reads `rsr:12;ss:5`, one number per house; treat it as opaque. | category follower, listing sync |
| `GET /products/facets` | Brands, classes and departments with product counts. | Drop-ship Center filters |
| `GET /categories` | The service's category tree as nodes, with a change cursor. An empty tree is valid and means stores keep their own Item Groups. | category mirror |
| `GET /ops/status`, `GET /ops/history`, `GET /ops/flags` | Feed freshness per feed; recent runs with the error class only; data-quality counts. Scope `catalog:ops`. | Catalog Service page |

`GET /offers` (a paged scan of one house) and `GET /changes` (SKUs changed since a
cursor) were part of this contract until drop-ship v2 R5b: the POS no longer
reads them. A house on its own service is synced by the oldest-first scan and
the stale/error safety net; the site-wide service's houses are synced from its
cross-house product change feed. A service may keep serving them.

### Field semantics that the schemas cannot say

- **`cost` may be unknown.** `0` (or absent) means *unknown*, never free. Sorting rules treat unknown cost as worst; a UI must render it as "cost unknown", not `$0.00`.
- **`qty`** is the house's on-hand at last sync — never a reservation. `0` = out of stock.
- **`product_class`** (`OfferFull`, optional) is the offer's class — one of `Handgun`, `Long Gun`, `Other Firearm`, `NFA`, `Gun Parts`, `Ammunition` and `Accessory` — from the house's own department code. The POS keeps no class table: markup and listing rules that name a class, and the website shipping class, read it. Absent or `''` = no class (a class rule does not match the row).
- **`case_cost` and `units_per_case`** (`OfferFull`, optional) are the house's full-case price **per unit** (not the case total) and the case size, for a SKU sold by the case (RSR's Direct Connect get-items `DealerCasePrice` / `UnitsPerCase`). The POS uses them only for a line shipped to the store: full cases at `case_cost`, the rest at `cost`, and only when `0 < case_cost < cost`. Absent, null or 0 = not sold by the case.
- **`category` and `legal` may be null** (not classified). The POS then reads the house's `ffl_required` flag and the offer's `product_class`; only `legal.is_firearm` is read from `legal`.
- **`map` / `msrp`** `0` = none published. `map_policy` (`OfferFull`) is the manufacturer's policy mark when the house ships one (`M` enforceable MAP, `U` suggested, `''` none); render `''` as "no policy", never as MAP $0.
- **`ffl_required`** is the house's firearm flag and is authoritative for order splitting. The POS cross-checks it against its own Item and stops for a human on disagreement.
- **`dropship_blocked` / `vendor_approval_required`** are the house's per-SKU flags (the house will not drop-ship / needs brand approval). The POS folds its own brand policy on top; a service must not invent these — report the feed's fact or `false`.
- **`discontinued`** = the house dropped the product; **`status_flag == "Deleted"`** = the row is gone from the feed. Both stay in `/rows` and `/item` (archive reads) but are excluded from `/search` unless `include_removed` (Deleted).
- **`best_offer`** (search, item) = the offer the merge rule would buy from now: in stock first, then lowest **known** cost, ties to the house with lower `content_priority` (reference: rsr), then sku. `null` only when no offer qualifies. It must be one of `offers[]`.
- **Product-level content** (`manufacturer`, `model`, `description`, `image_url`) comes from the house the service designates (`content_source`); a service with one house returns that house's content.

### Cursors and freshness

- `/health`: `last_quantity_sync_at` is **the feed heartbeat** — the POS watchdog zeroes published stock for a house whose heartbeat is older than its threshold and stops mass-delisting when `offers_active` is 0 (a service that answers 0 active rows during an outage triggers the brake, which is the intended fail-safe). `last_catalog_sync_at` is the content heartbeat. `catalog_version` is any string that changes when catalog content changes (cache key material). Timestamps are ISO-8601 **with offset** (UTC recommended) or `null` when never synced.

## Authentication

Every request carries three headers, computed per request:

```
x-client-id : <client id issued by the service>
x-timestamp : <Unix seconds, integer>
x-signature : hex( HMAC-SHA256( secret, client_id + timestamp + METHOD + path + body ) )
```

- `METHOD` is upper-case (`GET` / `POST`); `path` is the URL path only (no query string), percent-encoded exactly as sent; `body` is the raw request bytes (empty for GET).
- The service rejects a timestamp outside its skew window (reference: ±300 s) with **401**, a bad signature with **401**, a client lacking the `catalog:read` scope with **403**, and answers **429** when a shared budget is exhausted. The POS treats 403 as "not entitled" (a provisioning fact), 429 as "budget", anything else non-200 as "service down".
- Secret rotation: a service should accept the previous secret for a grace window so clients can roll without downtime.

Reference client: `ffl_integrations/data_service/client.py::signed_headers`.

## Versioning

- Path prefix `/v1`. Additive changes (new optional fields, new query parameters, new endpoints) keep `/v1`; new endpoints raise the minor of `contract_version`.
- Breaking changes (removed/renamed fields, changed semantics) go to `/v2` and bump `contract_version`; a service may serve both.
- `contract_version` on `/health` is the minor the service implements: `"1.1"` for this document; `"1.0"` is a service without the products, categories and ops endpoints. Minor versions only add; a consumer accepts any `1.x` and refuses a major it does not know.

## Conformance

The POS ships a runner that exercises every endpoint against a live service
and reports each check:

```
bench --site <site> execute ffl_integrations.catalog_contract.run \
  --kwargs '{"base_url": "https://catalog.example.com", "client_id": "…", "secret": "…", "distributor": "rsr", "sku": "<one the service carries>"}'
```

It reads the named SKU from `/rows` (and its UPC from `/item`), then checks
shapes, types, scoping, limits, the unknown-SKU/UPC behaviours and the health
fields. Every check must pass before pointing a store at the service. The same
runner runs in the POS test suite against the in-memory reference
(`ffl_integrations/tests/catalog_stub.py`), so the contract and the reference
cannot drift apart unnoticed. The runner checks the 1.0 endpoints; the 1.1
shapes are in openapi.json, and the POS's local catalog is held to the
reference stub's shapes for every method by its own test suite.

## Pointing the POS at a service

Data Service Settings → *Endpoint / Client ID / Secret* is the site-wide
catalog service (Test Connection there records the service's contract version). A Distributor row may name its own service (Distributor →
*Catalog service* tab): every read scoped to that house then goes there,
while cross-house reads (unscoped search, `/item/{upc}`) still use the
site-wide service — so a second service adds a house without touching the
first. A half-filled Catalog service tab fails closed (no silent fallback).

On the code side a house is a `DistributorProvider` that mixes in
`RemoteCatalogMixin` (`code` = the distributor code the service knows); the
mixin supplies every catalog read, and the provider adds only ordering.
Freight is the provider's too: `estimate_order_freight` prices one order
(a house with flat per-order rates returns `freight.flat_rate(...)` over its
Settings), and every caller (the counter's Fulfillment planner, drop-ship
sourcing) asks through `ffl_integrations.distributor.freight.estimate_orders`.
