# Field notes: FFL Settings, Item and Serial No

The forms carry one-sentence descriptions. The longer reasoning that used to live in each field's description is kept here, verbatim, grouped by form.


## FFL Settings

### Our FFL Licence Number

This store's own FFL. A web order that names it as the receiving FFL is an in-store pickup, not a shipment: the buyer collects at the counter and completes a 4473 here, so POS refuses to book a transfer to ourselves. Leave blank and nothing changes.

### Alert Recipients

Comma- or newline-separated emails that receive operational alerts (sync failures). Empty → all System Managers.

### Receive Sources

One per line. Drives the Source dropdown on Receive Goods, the FFL Acquisition form, and the Purchase Receipt form. Edits take effect on the next form refresh — no app restart needed.

### Receipt Footer Note

Store policy printed under every receipt, one line per line. Empty prints nothing — no default, because a returns policy is each store's own call. Plain text only: it is placed into the receipt HTML unescaped, so &lt; and &amp; would be read as markup.

### Paper 4473 Mode

Check when 4473s and NICS are handled OUTSIDE this system (paper form, background check by phone or the FBI portal) — the normal setup once FastBound is switched off. At the counter the 'FastBound 4473' button is replaced by 'Record Paper 4473': confirming it books this order's bound-book Disposition for every gun on the ticket, stamped with who ticked it and when, and the sale can then be completed. The ATF record-keeping duty is unchanged — the paper 4473 must be filled in and filed exactly as before; this only tells POS not to wait for FastBound. Takes effect on the next POS page load — reload any till that is already open, or it keeps acting on the old setting.

### Transfer Fee Item

Service item charged for a customer transfer-in 4473. Non-firearm, non-stock — auto-created and seeded here on migrate. (No field default: a Link default to an app-created Item breaks init_singles on a fresh site.)

### Default Transfer Fee

Per-firearm transfer fee auto-added to a Transfer ticket in POS. Editable per sale.

### Require Manager Approval for Price Reductions

Enforced in validate of POS Invoice, Sales Invoice and Sales Order (`ffl_core/price_approval.py`); the screens' dialog only saves a round trip. What asks is a line whose rate is below its floor: the list price (the serial's own price with its sale for a firearm line with one serial, else the Item Price of the document's price list), lowered by a Pricing Rule ERPNext applied on this save, and never above the rate the same line was saved at (or, on an invoice line made from a Sales Order line, that line's rate). So a person's reduction in THIS save asks, not one the system set or one already approved; builders that price below list on purpose mark the document (`price_approval.mark_system_priced`) instead of being listed. A Sales Order line has no serial, so a per-gun firearm is judged on the invoice that carries its serial. Lines a distributor fills are not judged (their counter price comes from the distributor's quote). The approval token is single-use, 15 minutes, for the user who asked; the PIN check and lockouts are the staff switch's. Off on stores that predate it (patch `seed_price_approval_off`); on for a new install.

### Consignment Out

Sending THIS store's own guns to a partner FFL's rack to sell on our behalf. Uncheck and the Consignment Out / Dealer Orders shortcuts leave the Gun Shop workspace, those pages and Dealer Onboarding stop opening, the partner dealer portal at /consignment closes, and every consignment endpoint refuses. Nothing is deleted: existing Consignment Out records, the Consignment Dealer role and the Stock at Consignee accounts stay exactly as they are, so ticking this again restores the whole subsystem. Unrelated to a customer leaving their OWN gun here to sell (Serial No 'Is Consignment'), which is unaffected. The two Gun Shop shortcut cards belong to this switch, not to the workspace: while it is ticked, every migrate puts them back, so deleting a card by hand does not stick — untick this instead. Changing this switch through the API or an MCP tool writes the value but skips the form's on_update, so the cards and the Desk buttons only catch up on the next settings save or migrate.

### Consignment Contact Email

Where a partner dealer is told to write when the portal cannot mark a gun sold or confirm receipt — their only self-service way out of a failure. Empty drops the offer from those messages entirely. Not Company.email: that is the store's public address, and this one belongs to whoever runs consignment.

### Store Customer ID Numbers

Off by default, and off is how every store ran before the switch existed: no ID number and no date of birth on the Customer (they live on the 4473). Ticked, the Customer form's Profile tab shows Date of Birth and the ID Number column of the ID Documents table (unticked, that column is hidden, so stored numbers are not displayed or typed), and scanning a driver license in the new-customer dialog fills Date of Birth, Driver License # and License Expiry (the licence becomes a Driver License row). Unticked, every Customer save — form, quick entry, REST, import, merge — clears Date of Birth and every ID Number on the server; ID type, issuer, dates and the attached copy stay. Unticking (saved from on to off) also erases every Date of Birth and ID Number already stored, in one step when the settings are saved, and leaves a Comment on FFL Settings with the counts; the form asks first ("Turning this off erases N stored ID number(s) and date(s) of birth. Attached ID copies, change history and deleted records are kept. Continue?"), and the question is part of the save itself, so Ctrl+S gets it too. Attached ID copies, Version history and deleted records (Deleted Document) are kept: an old number can still be read in a customer's change history, or in a deleted customer's saved copy, until those are cleared on their own. Changing the switch through the REST API or an MCP tool (System Manager only) erases the same way but asks nothing, because the question lives in the form. The attached copy of an ID is always a private file, whichever way this is set; a file already attached to another record (a product photo picked from the library) is refused instead, so upload the ID image directly. The server rule applies from the next save; the form fields and the scan follow on the next page load — reload an open till.

## FFL Settings (removed)

### ATF CSV URL

URL admins rotate when atf.gov reorganises.

### Last CSV Refresh

(no description)

### Provider API Key

Used only by paid providers (2A Commerce). Stored encrypted.


## Item

### Firearm Serial

Legacy — kept for historical lookups only. Canonical firearm serial(s) live in ERPNext's Serial No records now (the Used Firearm one-of-one path was unified — every firearm is has_serial_no=1).

### Firearm

Check if this Item represents a firearm — any bound-book type, NFA classes included (the Firearm Type field carries the vocabulary). Drives FFL compliance, 4473 gating, FastBound sync, and the Serial Numbers tab. Auto-filled from the Item Group when the group is a firearm group.

### Antique

Antique firearm (pre-1899 / GCA-exempt). Tracked as a serialized gun (still requires a serial, shows on Firearms In Stock) but NOT entered in the FastBound bound book and NO 4473/NICS at sale. Taxed as ordinary goods.

### UPC

12-digit UPC barcode (one per item). Kept in sync with the standard Item Barcode table so POS / Receive Goods scanning still resolves the item.

### Importer

Reference only — never used as a default anywhere (2026-08-20). Each gun's actual importer lives on its Serial No; this field neither prefills receives nor reaches FastBound.


## Serial No

### Service Needed

Flagged when the firearm was received with 'Service Needed' ticked on the Receive Goods line. Cleared by the gunsmith after service (click the pill on the Item form's Serial Numbers tab, or uncheck on the Serial No form).

### Sell Price

Per-gun selling price. Set at receive time on the Receive Goods page; editable later on the Item form's Serial Numbers tab. POS picks the lowest-priced in-stock unit by default.

### Dealer Sell Price

Per-gun dealer price. Set at receive on the Receive Goods page; editable on the Item form's Serial Numbers tab.


## Item

### Sells Through Listing

Per-gun-priced pool: its serials may ALSO be sold through this uniform-price listing Item. A web order for that listing then accepts a serial from here, and POS re-buckets the gun into the listing itself. Leave blank on an ordinary Item.


## Serial No

### Manufacturer

Per-unit manufacturer. Old / surplus / imported guns under one model SKU can each have a different maker (different arsenals). Blank = use the Item's Manufacturer. This serial's value is what FastBound's bound book records.

### Model

Per-unit model. This gun's true model as recorded in FastBound; the sync keeps it aligned to the bound book. Blank = use the Item's Model.

### Caliber

Per-unit caliber. This gun's true caliber as recorded in FastBound; the sync keeps it aligned to the bound book. Blank = use the Item's Caliber.

### Importer

Per-unit importer marking (imported firearms). Same model SKU can carry different importer markings per import batch. Blank = no importer on the report or the bound-book push (nothing falls back to the Item); imported guns must be stated per gun. This serial's value is what FastBound records.

### Firearm Type

Per-unit firearm type (ATF 4473 category). This gun's type as recorded in FastBound; the sync keeps it aligned to the bound book. Blank = use the Item's Firearm Type.

### Year Made

Per-unit year of manufacture. Two guns of one model can be decades apart, so this belongs to THIS gun; the Item's Year Made is only the model-level default. On an antique it is the provenance behind the GCA exemption - optional, and never a condition of receiving.

### Consigned Out

Set while this firearm is sitting at a partner FFL on outbound consignment (ship_consignment_out sets it; the settlement job / return_consignment_lines / cancel_consignment_line all clear it). The exclusion key for all four ffl_woo_sync listing touchpoints — a consigned-out gun must never look sellable on the Woo store while it is physically at the dealer.


## Item

### Sale Price

Promotional price - must be below the Standard Selling Rate. Applies at the POS register and syncs to the web store's Sale price. Leave 0 for no sale.


## Serial No

### Sale Price

Promotional price for this gun - must be below its Sell Price. Ignored when the parent Item is uniform-priced (set the sale on the Item instead).


## Item

### State Restrictions

Comma-separated 2-letter state codes this item may not ship to (from the distributor feed). Pushed to the store as the _osa_restricted_states meta and enforced at checkout.


## Serial No

### GunBroker Item ID

The listing this gun is on GunBroker RIGHT NOW. Non-empty means it is live there. A cache, not an identity: GunBroker mints a new item number every time it relists (every 30 days on a fixed-price listing), so anything looking a listing up matches on the SKU instead and treats this as the current value.

### GunBroker Delisted

This listing was ended. No automated path ever re-lists a gun carrying this flag — a person does, by pressing Send to GunBroker, which is also the only thing that clears it.

### GunBroker End Pending

The listing needs to end and has not been confirmed ended. While this is set the listing may still be BUYABLE on GunBroker for a gun that is no longer available — which is why it raises an alert rather than sitting here quietly.

### Reserved via GunBroker

Sold on GunBroker and awaiting fulfilment. The GunBroker counterpart of Reserved via Woo; both are read through one predicate so no channel can sell a gun another channel has taken.

### GunBroker Push Pending

A listing was posted to GunBroker and the result was never confirmed. Written and committed BEFORE the request goes out, on purpose: GunBroker enforces no uniqueness on SKU or serial, so if this record vanished with a failed request the next click would post a second live listing for the same physical gun.

### GunBroker Push Attempted At

When that unconfirmed attempt was made. The grace period runs from here — an attempt is not written off as 'nothing was created' until it has had time to appear in GunBroker's own listing index.

### GunBroker Price Dirty

The sell price changed and GunBroker has not been told yet. A flag rather than a queued value: the job reads the price when it runs, so two edits in quick succession publish the second one rather than whichever job wins.

### GunBroker Sold (unconfirmed)

GunBroker stopped serving this listing while showing the same SKU as sold, and no order has come through yet. Fail-closed: the gun cannot be listed, sold at the counter or picked for a web order until either the order arrives or somebody checks GunBroker and clears it.

### GunBroker Missing Count

Consecutive sweeps in which GunBroker answered that this item does not exist. Reset to zero the moment it is found again, or a new listing is created.

### GunBroker First Missing At

When the listing first went missing. A listing is only written off once it has been missing for several sweeps AND for long enough — measured from here rather than counted in sweeps, because sweeps do not run on a fixed clock. Cleared alongside the strike count.


## Item

### GunBroker Category ID

Overrides the GunBroker category this model lists under, ahead of the firearm-type map in GunBroker Settings. Set it for a model that deserves a more specific category than its type implies — the map deliberately sends rifles and shotguns to the generic leaves rather than guessing an action type this system does not record.

### GunBroker Item ID

The item-level listing this model is on GunBroker RIGHT NOW — one listing standing for a quantity of interchangeable units, as opposed to the per-gun listings that live on Serial No. A cache, not an identity: GunBroker mints a new item number on every relist, so lookups match on the SKU (the bare item code) and treat this as the current value.

### Previous GunBroker Item IDs

Every item number this model's listing has had, newest last. Non-empty is also what tells a restock it may re-list itself: a model that has been on GunBroker before goes back automatically, one that never has does not.

### GunBroker Quantity Pushed

The quantity GunBroker was last told. Compared against stock on every ledger movement so an unchanged count costs no API call — this fires on every sale of every listed item, and most of them do not change it.

### GunBroker Delisted

Somebody ended this listing by hand. A restock will NOT put it back — a machine never undoes a person's decision; the person presses the button again. Selling out clears nothing and leaves this at 0, which is what makes automatic re-listing safe.

### GunBroker Push Pending

A listing attempt went out and its outcome is unknown. Blocks a second attempt until the recovery scan settles it — GunBroker enforces no uniqueness, so a hopeful retry is how one model ends up with two live listings and sells twice.


## Item (descriptions removed 2026-09-02, form reorganised)

### Used (pre-owned / trade-in)

Mark used / pre-owned / trade-in inventory.

### Quantity

Stock-on-hand summed across all warehouses. Auto-maintained by Stock Ledger Entry hooks.

### Firearm Serial

Legacy; firearm serials now live on Serial No records.

### Firearm

This Item is a firearm (any bound-book type). Drives 4473 gating, FastBound sync and the Serial Numbers tab.

### UPC

12-digit UPC, kept in sync with the Item Barcode table for scanning.

### MPN

Manufacturer part number (RSR mfg_number). Sent to FastBound as the item MPN; populated from the RSR catalog on promote.

### Importer

Reference only; each gun's actual importer lives on its Serial No.

### Sale Price

Promotional price, below the Standard Selling Rate; syncs to the web store. 0 = no sale.

### Sale From

First day of the sale (empty = starts immediately).

### Sale To

Last day of the sale, inclusive (empty = runs until cleared).

### Drop-ship Distributor

When set, stock/procurement for this Item is sourced from this distributor (drop-ship / procure-to-store). Blank = local stock.

### GunBroker Shipping

Buyer-paid shipping for this model's listing. Empty: the accessory / ammo default in GunBroker Settings.

### GunBroker Item ID

Current GunBroker quantity listing for this model. A cache; lookups match on SKU.

### Previous GunBroker Item IDs

Every item number this listing has had, newest last. Non-empty lets a restock re-list automatically.

### GunBroker Sync Error

Why the last push was refused, verbatim from GunBroker where it answered.

### GunBroker Quantity Pushed

Quantity last sent to GunBroker; an unchanged count skips the API call.

### GunBroker Delisted

Ended by hand. A restock will not re-list until Send to GunBroker is pressed again.

### GunBroker End Pending

The listing was asked to end and the end could not be confirmed. Until it is, the model can still be bought on GunBroker.

### GunBroker Push Pending

A listing attempt's outcome is unknown; blocks another attempt until the recovery scan settles it.

### GunBroker Push Attempted At

When that unconfirmed attempt was made. The sweep leaves it alone for the grace period before concluding nothing was created.

### GunBroker Price Dirty

The price changed locally and GunBroker has not confirmed the new one. The sweep retries until it reads back.


## POS Invoice (descriptions cleaned 2026-09-03)

These fields carried their implementation notes as on-screen help text, where a cashier taking a payment read them. The forms now carry one sentence each; the reasoning is kept here verbatim.

### Card Unique Reference

Set by ffl_integrations.payments.api.charge_pos_invoice on a successful charge.

### Card Attempt

Counter for idempotency-key construction. Increments on each charge attempt.

### Card Refunded Amount

THIS return's card money already pushed back at the gateway: written only by ffl_integrations.payments.api money-fact writes; feeds the orphan-refund gate. NOTE: frappe's amend RETAINS no_copy values (see ffl_core test_consignment_out_reinvoice) — an amended successor starts at 0 because restore_charge_rows zeroes money facts on every NEW doc, not because of the field property; no_copy still covers get_mapped_doc copies (returns).

### Card Charges

One row per device payment instruction — a sale split across several cards has one row per card. The single-value card_* fields above are aggregates of these rows; written only by ffl_integrations.payments.api. This Table field deliberately lives in ffl_integrations (not with the sibling card_* fields in ffl_core): its options reference this app's Card Charge doctype, and ffl_core installs first on a fresh site.

### NICS Cleared Notified

Set once the customer has been emailed that their delayed NICS check cleared. Internal idempotency flag.

### Woo Online Order

Set when this counter invoice is the in-store pickup of a web (WooCommerce) order: the buyer chose this store as the receiving FFL and collects here under a retail 4473. It is what makes a second Pickup click return this draft instead of opening another, what retires the web order when this invoice is submitted, and what puts it back in the queue if the invoice is cancelled. Deliberately NOT no_copy: an amended invoice that lost this link would be invisible to all three.


## Card Charge (description cleaned 2026-09-03)

### Status

REFUNDED is reserved for display flows; refunds are ledgered via refunded_amount and do not flip a CHARGED row's status.


## Data Service Settings

The site-wide connection to the shared data service (FFL verification via EZ Check + every distributor catalog). One connection, read by `ffl_integrations/data_service/client.py` only; a Distributor row's *Catalog service* tab overrides it for that house. Formerly three fields under FFL Settings → FFL Verification (moved by `patches.move_data_service_to_own_settings`).

### Catalog source

`Data Service` (default) or `Local` (experimental: partial feed drivers, new installs only; live stores use the Data Service). Read by `remote_catalog._backend()`, the one choke point every catalog read passes (rows, search, item, health, restrictions, products*, categories, ops): Local serves them from this site's `dc_*` tables (`ffl_integrations/catalog_local`) and never calls the data service client for catalog; FFL search and extension updates keep using the connection below. Local tables are filled by the `catalog` queue's worker from each distributor's own feeds (`catalog_local.sync.tick`, `*/15`); the hub warns when that worker is missing. Data Service → Local is for fresh installs only: the two sources number product changes differently, so the form refuses that change once any registered product change cursor holds a value, and names it. Local → Data Service is allowed once the connection (endpoint, client id, secret) is filled in; saving it clears every registered cursor (`reset_change_cursors`) and the category follower's baseline flag, so each follower starts over on the service. Cursors are registered as `DocType.field` under the `gunshop_catalog_change_cursors` hook: ffl_integrations registers the category follower's; the drop-ship extension registers its listing sync / sourcing cursors in its own hooks. The `queue-catalog` worker is an opt-in compose profile: a Local store sets `COMPOSE_PROFILES=catalog` in `deploy/prod/.env`.

### Endpoint

Base URL of the service, e.g. https://catalog.example.com. Any service that passes the Catalog API conformance runner (docs/catalog-api) works here.

### Client ID / Secret

Issued by the operator of the data service. The secret is a per-client HMAC key, stored encrypted.

### Catalog API contract / Checked at / Last error

Written by Test Connection: the `contract_version` the service reported, when, and the last transport error (blank when the last test passed).

## RSR Settings (removed 2026-09-08)

The FTP connection, path, automation and sync-status fields (ftp_host, ftp_port, key_dealer, ftp_username, ftp_password, ftp_dir_default, ftp_dir_keydealer, the five *_filename fields, auto_sync_enabled, the three *_sync_frequency selects, last_catalog_sync_at/count, last_quantity_sync_at/count, last_restrictions_sync_at/count, restrictions_batch, last_sync_error) drove the in-POS RSR feed ingest that moved to the Data Service. Nothing read them any more; `rsr/loader.py`, `rsr/tasks.py` and the "Sync … Now" buttons went with them. Feed health is on the Catalog Service page and the Distributors hub.

### Catalog feed (local catalog) — ftp_host, ftp_port, key_dealer, ftp_username, ftp_password (back 2026-10-10)
Five of those names return for the local catalog (Data Service Settings › Catalog source = Local): the FTPS login `catalog_local/drivers/rsr.py` downloads RSR's inventory, quantity, deleted and ship-restriction files with. The section shows only on a Local site (`__onload.catalog_local`); a Data Service site never reads them, and a value saved before the 2026-09-08 removal (Singles keep it) is just as unused there. Blank host/port mean ftp.rsrgroup.com:2222 (explicit FTPS, passive). A key dealer reads every file from `keydealer/` and the catalog as `rsrinventory-keydlr-new.txt`; everyone else reads `ftpdownloads/`. Test Connection adds a "Catalog feed (FTPS)" row on a Local site: log in and list the folder, no download.

The server's TLS certificate is checked (chain and hostname). *Skip TLS certificate check* (`ftps_skip_verify`, off by default) turns that off for a feed server whose certificate fails it; every download then logs a warning, because the login is sent without proof of who receives it.

**Requires FFL and product class (Local mode).** They copy the Data Service's own RSR rules. RSR's inventory file has no FFL column, so the flag is the FFL column of RSR's department table, the one already shipped as `setup/rsr_seed.DEPARTMENTS` and seeded by the Data Service in the same form: Handguns (01), Used Handguns (02), Used Long Guns (03), Sporting Long Guns (05), SOTS (06) and the three upper/conversion-kit departments (41, 42, 43) are FFL-required; every other listed department, Black Powder Firearms (07) included, is not. A department number RSR adds later, or a row with no department, is FFL-required (stricter than the Data Service, which treats it as not FFL). The product class is the RSR branch of the Data Service's class rule (`catalog_local/drivers/rsr_feed.product_class`): the department decides (06 NFA; 01/02 Handgun; 03/05 Long Gun; 18 Ammunition; 41/42/43 Gun Parts; 32/34 Gun Parts; anything else FFL-required Other Firearm, otherwise Accessory), and the title only marks an FFL-required row NFA when it names an SBR, SBS, suppressor or silencer (not a "suppressor ready" gun, a threaded or barrel item, or an upper). An upper (41/42/43) is therefore FFL-required Gun Parts: it ships only to a dealer, never to a buyer's door. The department name is stored for facets. The inventory file writes every column, quantity included, on each 2-hourly run, and the 15-minute quantity lane moves the quantity in between; an Allocated, Deleted or discontinued offer always reads 0.

### POS indicator
The POS code RSR assigned to this integration; sent as `POS` on every Direct Connect call.

### Order email
Required by RSR on place-order (StatusCode 88 without it) — the shop's address, never the consumer's or the transfer dealer's.

### API base URL
Production is www.rsrgroup.com; RSR's testing environment (test.rsrgroup.com) is what the dev site points at.


## Sports South Settings (descriptions cut 2026-09-15)

### 2-Day Air ship code
ShipVIA when the order contains a handgun (handguns ship air only). N = Next-Day Air.

### Accessory / ammunition orders ship Ground Economy
On: non-firearm orders send an empty ShipVIA (Ground Economy, $7.95). Discount carriers — no package claims, traces, address changes or intercepts.

### Ship-to address override (store_ship_*)
Formerly "Store ship-to (stocking and procure-to-store orders)", the only source of a store order's destination. Since 2026-09-15 a blank block means the company's own address (`ffl_core.store_identity.company_address`, the Address its receipts print). Any one filled field makes the whole block the destination, never mixed with the company address, which keeps a site that filled it before the change shipping exactly where it did.


## Distributor

### Priority
Lower = higher priority: the planner matrix column order and the default-house pick when nothing names a house. Managed from the Distributors hub arrows.

### Default markup % / Rounding
The global cost-plus fallback price when no markup rule (per department / brand / cost range) matches; a matched rule carries its own rounding.

### Safety buffer / Display cap
Published quantity = min(live − buffer, cap). The buffer absorbs feed staleness between syncs; the cap keeps a 4,000-unit distributor line from reading as unlimited on the storefront.

### Zero stock when the feed is stale
Watchdog interlock: when this house's quantity heartbeat ages past the threshold, its items publish 0 rather than a stale number the buyer can order against.

### Poll offset (removed)
Never read by the scheduler.

### Catalog service (Endpoint / Client ID / Secret)
Per-house override of the site-wide Catalog API service on FFL Settings (docs/catalog-api). Reads scoped to this house (rows, offers, changes, health, restrictions, scoped search) go to this service; unscoped search and `/item/{upc}` always use the site-wide one. All three or none.
