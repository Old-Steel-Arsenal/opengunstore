# Core hooks and the registry

For developers. This page lists everything the POS core offers an extension to plug into: the Python SDK, the named hooks the core reads on an extension's behalf, and the JavaScript registry that places an extension's buttons in the POS. Anything not on this page is internal to the core and can change in any release. The [developer specification](developer-spec.md) says which of these an extension may use, and the lint refuses any hook in an extension's `hooks.py` that is not listed there or here.

## The Python SDK — `ffl_core.extensions`

```python
from ffl_core.extensions import extension_hook, require_enabled, is_enabled
from ffl_core.roles import COUNTER_ROLES

@extension_hook("acme_layaway")                 # switched off: returns None, does nothing
def on_sales_invoice_submit(doc, method):
    ...

@frappe.whitelist(methods=["POST"])
def start_plan(customer: str, item: str):
    """Start a layaway plan.

    Args:
        customer: Customer name.
        item: Item code.

    Returns:
        dict: {"plan": name}
    """
    require_enabled("acme_layaway", roles=COUNTER_ROLES)   # must be the first statement
    ...
```

| Function | Behavior |
|---|---|
| `extension_hook(app)` | Decorator for `doc_events` and `scheduler_events` handlers. When the store has switched the extension off, the handler returns `None` without running. |
| `require_enabled(app, *, roles=None)` | Raises `frappe.PermissionError` when the extension is off or the user has none of `roles`. The built-in gate audit requires it (or `frappe.only_for`) to be the first executable statement of every whitelisted endpoint. Always pass `roles`: the audit counts only the `roles=` form as a gate. |
| `is_enabled(app)` | For templates and branches. Never use it to bypass `require_enabled`. |
| `title_of(app)` | The extension's title as the registry knows it. |

Roles: reuse `ffl_core.roles.COUNTER_ROLES`, `STOCK_ROLES` and `SYSTEM_ROLES`, or your own role.

## Named hooks the core reads (`gunshop_*`)

The named hooks `ffl_core` reads on behalf of extensions. Declare them in the extension's `hooks.py` like any Frappe hook;
values are dotted paths to functions unless the table says otherwise. Every
provider is called inside `try/except`: a failure is written to Error Log and
the last column says what core assumes in that case.

| hook | value | called with → returns | when a provider fails |
|---|---|---|---|
| `gunshop_counter_holds` | dotted path | `(item_code) → [{serial, source, ref, kind, tooltip, url}]` — serials of that Item an extension is holding for something not yet disposed (a draft outbound consignment, a layaway…). Merged into `ffl_core.reservations.counter_holds` AFTER the counter's own sources, so a serial already on a counter order keeps the counter's reason. Enforced everywhere holds are (POS picker, web stock, second consignment). | contributes nothing (it cannot name serials) |
| `gunshop_required_agreements` | dotted path | `(sales_doc) → [template, …]` — Agreement Template names the customer of this POS Invoice / Sales Invoice must have signed before it is submitted, on top of the Items' own *Agreement Required to Buy*. Merged into `ffl_core.agreements.required_templates`; switched-off templates are dropped. Checked in `before_submit` (`refuse_missing_required`), never on a return, a POS consolidation invoice or a web / GunBroker sale. To require a signature for something of your own (a booking, a rental), call `ffl_core.agreements.require_agreement(customer, template)` in your own validate — it raises `AgreementRequired`. | logged, contributes nothing (the sale goes on) |
| `gunshop_pending_queue_exclusion_fields` | **fieldname** (string) | A Sales Invoice Link fieldname the extension owns (its own custom field) marking invoices whose fulfilment lives on the extension's record. Such invoices stay out of the Pending Order queue, the counter-hold registry and the counter-order marker (Edit / Cancel Order), and core does not auto-book dealer-transfer dispositions for them (the extension's own record did). They are *not* channel mirrors: tax, receipt and Sales Report rules still apply. Column-guarded — a site without the column ignores the entry. | — (data, not code) |
| `gunshop_disposition_stock_handled` | dotted path | `(ffl_disposition_doc) → bool`; True = the extension already moved this gun's stock, core must not Material-Issue it on submit. | treated as True (fail closed: a second stock move is the worse error) |
| `gunshop_website_user_home_page` | dotted path | `(user) → route \| None` for a signed-in **Website User** (core has already checked that — a Desk user never reaches providers). First non-None answer wins. Answer a Website route even when your feature is switched off (`ffl_core.WEBSITE_USER_FALLBACK_HOME_PAGE` is frappe's account page): None hands the user the Desk, which a Website User cannot open. | skipped |
| `gunshop_woo_product_meta` | dotted path | `(doctype, name) → {meta_key: value}` where doctype is `"Item"` (non-firearm / uniform-price product) or `"Serial No"` (per-serial gun). Appended to the WooCommerce product payload's `meta_data` on every full push. Keys MUST match `^_[a-z0-9_]{2,60}$` (WordPress treats a leading underscore as hidden meta) and a key core already emits wins the collision — checkout enforcement and the SOLD lifecycle read those. Pair it with `gunshop_woo_product_meta_keys` or the value silently reverts on the next lean push. | logged, skipped — the listing still publishes without the extension's flag |
| `gunshop_woo_product_meta_keys` | **meta key** (string) | A key the extension may write through `gunshop_woo_product_meta`, declared statically so it also survives a *lean* update (`pos_fields_only=True`, the machine-triggered push that filters everything not POS-owned so hand-curated meta such as SEO plugin fields is preserved). Declared but never emitted is harmless; emitted but not declared means the value lands on a full push and is dropped by the next lean one. | — (data, not code) |
| `gunshop_woo_push_fields` | **dict** `{"Item": [fieldname, …], "Serial No": [fieldname, …]}` | Custom fields whose change should enqueue a WooCommerce push of the document's product, exactly like core's own price/status fields do. Declare every field your `gunshop_woo_product_meta` provider reads: without it the provider only runs when a core field changes in the same save, so a flag edited on its own never reaches the store. The push is the lean (`pos_fields_only`) one — pair with `gunshop_woo_product_meta_keys`. Fires while the extension is switched off too (hooks are static); the provider then contributes nothing and the push is a harmless no-op. | — (data, not code) |
| `gunshop_extension_updates` | dotted path | `() → source \| None` — a zero-arg factory returning an object with `latest() → {ext_id: {version, sha256, size, requires_core}}` (the newest release of each extension this core can run) and `fetch(ext_id, version) → bytes` (the zip), or None when it is not set up on this site. The first source there is feeds the Extensions page's **Update** button: `ffl_core.extensions.updates.check` (daily and on Refresh) notes what is newer on each POS Extension row an update may replace; `api.extensions.fetch_update` downloads it, refuses bytes whose sha256 is not the one `latest` announced, and hands it to the upload pipeline (`install_upload` still takes the id — the page sends it after the manager confirms in its dialog). A source that serves one publisher's packages sets a `publisher_id` attribute: only extensions installed from that publisher are offered its updates, and `fetch_update` refuses a package from any other publisher or one whose own `requires_core` this core does not meet. ffl_integrations registers the Data Service's `/v1/extensions` source over Data Service Settings. | `source`: logged, next provider; `latest`: logged, the rows keep their last answer; `fetch`: logged, the page says the download failed |
| `gunshop_extension_toggled` | dotted path | `(app, effective: bool)` exactly once each time the extension's *effective* state changes while it is installed on the site — a manager flips the switch, or a core update makes it (in)compatible. Install and uninstall do not call it. Runs for every provider, **including the extension that just stopped** — do not wrap it in `@extension_hook`. Use it to add/remove workspace shortcuts (`ffl_core.workspace_shortcuts`). | logged; the change is already saved |
| `gunshop_sidebar_items` | **list of dicts** (data, not a path) | `Sidebar Item` fields per row (`type`, `label`, `link_type`, `link_to`, `icon`, `child`, `indent`, `collapsible`, `keep_closed`, …); the first row is normally a `Section Break` and its links `child: 1`. While the extension is effectively on, core writes the rows into the Gun Shop sidebar's site layer (`Custom Sidebar`) right before Settings, and takes them out again when it goes off or is uninstalled (`gunshop_extension_toggled`, app install/uninstall and every migrate rebuild it; `ffl_core.setup.gunshop_sidebar`). A module whose own sidebar mirrors the Gun Shop's (`mirror_gun_shop_sidebar`) gets the same layer, so the extension's pages show the same hidden links and the section once; rows the mirror already lists in its `extra_items` are moved into the section, not repeated. Core's own tiles on the home page are a separate thing (`ffl_core.workspace_shortcuts`). | rows skipped; the sidebar shows no section for the extension |
| `gunshop_dropship_listing` | dotted path | `(item_code, distributor \| None, fields: tuple) → dict \| None` — the Item's website drop-ship listing at that distributor (any house when None) as a dict of `fields`. The counter's quote, fulfilment matrix and drop-ship routing read the listing's SKU and fulfilment mode through it (`distributor.dropship_hooks.listing_for`). First non-None answer wins. A blank distributor matches nothing unless the caller asks for any house (then the provider receives None). | raises into the caller (no silent "not listed": the counter would re-stamp a listed Item) |
| `gunshop_dropship_suggested_price` | dotted path | `(distributor_doc, catalog_row, listing) → number \| None` — the counter's suggested price for a catalog product under the drop-ship pricing rules. First non-None answer wins; with no provider core suggests MAP, else MSRP, else cost. | raises into the quote (never a silently un-marked-up price) |
| `gunshop_distributor_source_settled` | dotted path | `(source_doctype, source_name)` once every distributor order of a non-counter source (a web order) has settled; the layer that created those orders completes the source (enqueue after commit — the call can come from inside the last order's save). Called for every provider. | raises into the distributor order save that settled it (the save rolls back, as the inline enqueue did) |
| `gunshop_distributor_feed_stale` | dotted path | `(distributor, *, aged: bool) → bool` when a distributor's quantity feed is stale (`aged`: it had a heartbeat and aged out — a cold start is not). Zero what you publish from that feed only when `aged`; return True when your zero-stock interlock is on (the admin alert says so). | raises into the watchdog run (a stale feed must not pass as "interlock off") |
| `gunshop_distributor_hub_rows` | dotted path | `(rows)` — the Distributors hub's rows (`frappe._dict` per house); add your own display columns in place. The hub card shows the markup line when a provider sets `markup_rules` (the drop-ship layer also sets `default_markup_pct` and the house's stock-publishing fields). The Catalog Service / Drop-ship Center buttons follow `dropship_hooks.layer_active()`, not this hook: no `gunshop_dropship_listing` provider — or only one from an extension the store has switched off — no buttons. | logged, the column stays blank |
| `gunshop_ai_connected` | dotted path | `() → bool` — whether the extension's AI service is connected (its service set up and reachable as far as it knows, without a network call: the answer is read at every desk boot). Any truthy answer makes `ffl_integrations.data_service.capabilities()["ai"]` `"connected"` (else `"off"`), which the desk reads as `frappe.boot.ffl_capabilities.ai`. Wrap it in `@extension_hook` so a switched-off extension answers None. ffl_integrations reads it; the core never imports the extension. | treated as not connected; logged at most once an hour |
| `gunshop_card_processors` | dotted path | `() → CardProcessor` — a zero-arg factory returning an `ffl_integrations.payments.base.CardProcessor`: one card gateway (start / poll / cancel a charge on the card device, void, refund, list terminals, a read-only probe for Settings → Payments), answering in `CardResult`s and raising `CardProcessorError` with an honest `outcome_known`. The core keeps every money fact itself (`ffl_integrations.payments.api`: charge rows and their stamps, refund locks, the refund split across cards, counter terminal payments and their Payment Entries) — the adapter never writes a document. At most one processor is switched on (the Payments page and Payroc Settings refuse a second; two on and new charges stop with a message naming both). Every charge row records the processor's `code`, and void / refund / poll go back to that processor, not to the active one. ffl_integrations registers Payroc through this hook. Switch your processor on and off from the Payments page so the Gun Shop sidebar follows. | logged, that processor counts as not installed: no new charges, and a refund of a charge it took is refused until a manager records the portal refund (Confirm Portal Refund), exactly as when the processor is switched off |
| `gunshop_catalog_change_cursors` | **`"DocType.field"`** (string) | A product change-feed cursor the extension keeps on one of its Singles (it follows `catalog.changes` / `/products/changes`). While any registered cursor holds a value, Data Service Settings refuses to change *Catalog source* and names the field: the Data Service and the Local catalog number changes differently, so a stored cursor would skip or replay changes against the other. When a site moves the other way, from the Local catalog to a Data Service, the core clears every registered cursor (straight on `tabSingles`, without running your controller), so a blank cursor must mean "start over": read the feed from its start, or take a fresh baseline. ffl_integrations registers `Data Service Settings.category_products_cursor`. | — (data, not code; a malformed entry is skipped) |

`enabled` on the POS Extension row is the manager's intent; the core never
rewrites it. What runs is `effective = enabled ∧ installed_on_site ∧ compatible`
(`is_enabled`, `require_enabled`, `@extension_hook`, the page's switch). An
extension that falls out of `requires_core` is paused, not switched off, and
resumes by itself once compatible again.

Boot: `frappe.boot.gunshop_extensions[id] = {enabled, version, title}` for every
extension installed on the site, where `enabled` is already the effective state —
what `gunstore.extensions` renders from.

Contract tests: `ffl_core/tests/test_extension_hooks.py` (a fake provider per hook).

## First-party only

Registered only by an extension whose manifest declares `first_party` (the lint refuses them otherwise):
they decide what the website sells and at what price, or answer another app's endpoint path. Today the
maintainers' website drop-ship extension is their one provider; without it every caller
falls back as the last column says.

| hook | value | called with → returns | without a provider / when it fails |
|---|---|---|---|
| `distributor_stock_source` | dotted path | `(item_code) → int \| None` — the quantity the WooCommerce push publishes for a drop-ship listed product. First non-None wins (`ffl_woo_sync.woocommerce.mapping.stock_for_item`, every stock path). | None: the store's own per-warehouse stock |
| `distributor_stock_batch` | dotted path | `(item_codes: list) → context manager \| None` — called once with every Item of the WooCommerce reconcile's Pass 1 (`ffl_woo_sync.woocommerce.tasks._run_reconcile`); core enters every answer (`contextlib.ExitStack`) and holds them open around the Pass 1 loop, so a `distributor_stock_source` provider can read the whole pass's catalog rows in a few batched calls instead of one per Item. Must not change any answer, only how it is read. None: nothing to batch. | logged (Error Log), left out — the pass runs unbatched for it; a failure on exit is logged too and never costs Passes 2–3 |
| `dropship_source_for_item` | dotted path | `(item_code, *, lead=True) → {distributor, sku, fulfillment_mode, lead_days} \| None` — who ships the product. Recorded on each web-order line at checkout (lane / source), read for the listing push's fulfilment and lead-time meta. | None: the store ships it (lane `local`) |
| `distributor_web_price` | dotted path | `(item_code) → float \| None` — the website price of a listed product (`mapping.web_price_for_item`); the counter price tag is untouched. | None: `Item.standard_rate` |
| `map_mode_for_item` | dotted path | `(item_code, brand) → str` — the brand's MAP mode for the push's `_osa_map_mode`. | `MAP is minimum price` (`mapping.MAP_MODE_DEFAULT`; was `Floor`) |
| `distributor_listing_push_failed` | dotted path | `(item_code, site, error)` — a queued Woo push of a listed product failed (ffl_woo_sync push tasks). | logged |
| `woo_order_cancelled` | dotted path | `(woo_online_order_name)` — the customer cancelled a web order (ffl_woo_sync's cancel handler writes with `db.set_value`, so no doc_event fires). | logged; the order stays cancelled |
| `override_whitelisted_methods` | **dict** `{old dotted path: new dotted path}` | Frappe's own hook: a request to the old endpoint path runs the new one. For endpoints that moved out of the core into the extension, so existing API clients keep working. | — |

## The JavaScript registry

An extension's bundle registers once with `gunstore.extensions`. The core decides where things render, in what order, and hides everything while the extension is not running (switched off, or paused because it does not fit this core). Never insert nodes into the POS yourself and never override `erpnext.PointOfSale.*` prototypes.

```js
// public/js/acme_layaway.bundle.js
gunstore.extensions.register('acme_layaway', {
  posActions: [{
    id: 'acme-layaway-start',
    label: __('Layaway'),
    icon: 'calendar',
    condition: (ctx) => ctx.invoice && ctx.invoice.customer && !ctx.invoice.is_return,
    onClick: (ctx) => gunstore.ext.acme_layaway.openDialog(ctx),
  }],
  serialActions:   [{ id, label, onClick({ frm }) }],   // Serial No form
  customerActions: [{ id, label, onClick({ frm }) }],   // Customer form
  settingsLinks:   [{ label: __('Layaway'), doctype: 'Acme Layaway Settings' }],
  onDeskReady() { /* once per page load, after the desk is ready */ },
});
```

| Entry | Where it appears | Notes |
|---|---|---|
| `posActions` | Buttons on the sale screen, after the core's own | `condition(ctx)` must be synchronous and cheap. `ctx` is a read-only snapshot (current invoice, customer, selected serial, POS Profile). |
| `serialActions` | Serial No form toolbar, under an **Extensions** group | `onClick({ frm })`. |
| `customerActions` | Customer form toolbar, under an **Extensions** group | `onClick({ frm })`. |
| `settingsLinks` | The **Settings** button on the extension's card in Settings → Extensions | One entry per Settings Single you ship. |
| `onDeskReady()` | Nowhere; it runs code | Called once after the desk is ready, for each extension that is effectively on. Use it to mount something on every desk page. A throw is logged to the console and does not stop other extensions. |

Other useful pieces: `gunstore.extensions.isEnabled(id, boot)`, `ffl.featureOffPage(page, featureTitle, whereToTurnOn)` for an extension's own Desk pages, and `gunstore.renderReleaseNotes(entries)`.

Desk pages: an extension's pages live in its own module. To list them in the Gun Shop sidebar and the Dashboard's **All records**, declare `gunshop_sidebar_items` (above). The core keeps that section in step with the switch. To give your module's own pages the same sidebar as the core pages, call `ffl_core.setup.sidebar.mirror_gun_shop_sidebar(<name>, module=<module>, extra_items=[...])` from `after_install` / `after_migrate`. Frappe lets it write a sidebar only during install, migrate, a patch or developer mode, so called from a switch handler it writes nothing until the next migrate. Declare `add_to_apps_screen` with a `has_permission` that returns False so the extension does not become a separate app in the app switcher.

## Workspace shortcuts

`ffl_core.workspace_shortcuts.ensure_shortcut(label, link_to, *, anchor, block_id, color="Blue", shortcut_type="DocType", workspace="Settings")` and `remove_shortcut(label, *, workspace="Settings")` add and remove cards on a workspace. `shortcut_type` is `DocType`, `Page`, `Report` or `URL`, and `link_to` is the matching target. Call them from `gunshop_extension_toggled` so the cards follow whether the extension runs. Staff land on the Dashboard, not on a workspace, so put the way to your pages in `gunshop_sidebar_items` first.

## First-party surface

Extensions published by the project maintainers (permission `first_party`, and an id on the list shipped in the lint) may also call a documented set of core functions that third-party extensions may not: dealer-transfer dispositions, custody flags, the consignment warehouses, settlement invoicing and the ShipStation client. They may also register the first-party-only hooks above.
