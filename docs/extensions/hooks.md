# Hooks and registry reference

Everything the core offers an extension to plug into. Anything not on this page is internal and can change in any release. The lint refuses any top-level name in an extension's `hooks.py` that is not a Frappe metadata key, an allowed Frappe hook ([Rules](rules.md#allowed-frappe-hooks)) or a `gunshop_*` hook below.

## The Python SDK

`ffl_core.extensions` (present since core 0.1.0):

```python
import frappe
from ffl_core.extensions import extension_hook, require_enabled, is_enabled
from ffl_core.roles import COUNTER_ROLES

APP = "acme_layaway"

@extension_hook(APP)                        # not effective: returns None without running
def on_sales_invoice_submit(doc, method=None):
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
    require_enabled(APP, roles=COUNTER_ROLES)   # first executable statement
    ...
```

| Function | Behaviour |
|---|---|
| `extension_hook(app)` | Decorator for `doc_events` and `scheduler_events` handlers. While the extension is not effective the wrapper returns `None` and the function does not run. |
| `require_enabled(app, *, roles=None)` | Raises `frappe.PermissionError` when the extension is not effective, or when the user holds none of `roles`. Must be the first executable statement of every endpoint; the gate audit counts it only with `roles=`. |
| `is_enabled(app)` | `True` when effective. For templates and branches; never instead of `require_enabled`. One cache read. |
| `title_of(app)` | The extension's title from the registry. |
| `clear_enabled_cache()` | Drops the desk boot cache. Call it in tests after writing the row (`frappe.db.set_value("POS Extension", APP, "enabled", 0)`). |

Roles: `ffl_core.roles.COUNTER_ROLES`, `STOCK_ROLES`, `SYSTEM_ROLES`, or your own role.

Boot: `frappe.boot.gunshop_extensions[id] = {enabled, version, title}` for every extension installed on the site; `enabled` is the effective state.

### The stable surface

Besides the hooks on this page, an extension may call: `ffl_core.extensions.*`, `ffl_core.roles.*`, `ffl_core.workspace_shortcuts.ensure_shortcut` / `remove_shortcut`, `ffl_core.setup.sidebar.mirror_gun_shop_sidebar`, `ffl_core.utils.single_flag`, `ffl_core.agreements.require_agreement`, the endpoints in the [API reference](../api/index.html), `ffl_integrations.distributor.base.DistributorProvider`, `ffl_integrations.distributor.freight.estimate_orders`, `ffl_integrations.payments.base` (`CardProcessor`, `CardResult`, `CardProcessorError`), and the `ffl_core.pricing` functions below. Everything else is internal.

- `counter_list_price(item_code) -> float`: the Item-level list price the register charges today: the selling Item Price of the default price list, except a uniform-priced firearm model in stock, priced from its cheapest priced gun; 0.0 for a gun priced one by one.
- `counter_price_for(item_code, serial_no=None) -> float`: a quote line's price: a gun priced one by one from that serial (refused unless it is an in-stock serial of the Item), or the cheapest one in stock without a serial; anything else `counter_list_price`.
- `price_readonly_reason(item_code) -> str | None`: why the regular price cannot be changed outside the POS (a gun priced one by one, a sale running today, a drop-ship listing, the transfer fee), translated for staff; None when it can.
- `set_regular_price(item_code, price) -> None`: a price approver (`roles.PRICE_APPROVER_ROLES`) sets the regular price; saves the Item (validate, website pushes, the Version trail) and the selling Item Price, and notes the old and new register price on the Item. Raises `PermissionError` for anyone else and `ValidationError` while `price_readonly_reason` answers or when the price is not above 0.

## Named hooks

Declare them in `hooks.py` like any Frappe hook. The value is a dotted path to a function unless the entry says otherwise. The core calls every provider inside `try/except`; **On failure** says what it assumes then (logged to Error Log unless stated). Contract tests: `ffl_core/tests/test_extension_hooks.py`.

### `gunshop_counter_holds`

- **Signature:** `(item_code) -> [{serial, source, ref, kind, tooltip, url}]`
- **Purpose:** serials of that Item the extension holds for something not yet disposed (a draft outbound consignment, a layaway). Merged into `ffl_core.reservations.counter_holds` after the counter's own sources, and enforced wherever holds are (POS picker, web stock, a second consignment).
- **On failure:** contributes nothing.

### `gunshop_required_agreements`

- **Signature:** `(sales_doc) -> [template, …]`
- **Purpose:** Agreement Templates the customer of this POS / Sales Invoice must have signed before submit, on top of the Items' own. Checked in `before_submit`; never on a return, a POS consolidation invoice or a web / GunBroker sale. Switched-off templates are dropped. For a signature on your own record, call `ffl_core.agreements.require_agreement(customer, template)` in your own `validate` (raises `AgreementRequired`).
- **On failure:** contributes nothing; the sale goes on.

### `gunshop_pending_queue_exclusion_fields`

- **Value:** a fieldname (string): a Sales Invoice Link custom field you own.
- **Purpose:** invoices with it set are fulfilled on your record: they stay out of the Pending Order queue, the counter-hold registry and the counter-order marker, and the core does not auto-book their dealer-transfer dispositions. Tax, receipt and Sales Report rules still apply. A site without the column ignores the entry.

### `gunshop_disposition_stock_handled`

- **Signature:** `(ffl_disposition_doc) -> bool`
- **Purpose:** `True` = the extension already moved this gun's stock; the core must not Material-Issue it on submit.
- **On failure:** treated as `True` (a second stock move is the worse error).

### `gunshop_website_user_home_page`

- **Signature:** `(user) -> route | None`, for a signed-in Website User only.
- **Purpose:** first non-None answer is the home page. Answer a Website route even while switched off (`ffl_core.WEBSITE_USER_FALLBACK_HOME_PAGE`): `None` sends the user to the Desk, which a Website User cannot open.
- **On failure:** skipped.

### `gunshop_woo_product_meta`

- **Signature:** `(doctype, name) -> {meta_key: value}`; `doctype` is `"Item"` or `"Serial No"`.
- **Purpose:** appended to the WooCommerce product's `meta_data` on every full push. Keys match `^_[a-z0-9_]{2,60}$`; a key the core emits wins. Declare permission `woo_product_meta`, and pair with `gunshop_woo_product_meta_keys` and `gunshop_woo_push_fields`.
- **On failure:** skipped; the listing publishes without your values.

### `gunshop_woo_product_meta_keys`

- **Value:** a meta key (string).
- **Purpose:** declares a key `gunshop_woo_product_meta` may write, so it survives a lean push (`pos_fields_only=True`, which drops everything not POS-owned). Emitted but undeclared = lost on the next lean push.

### `gunshop_woo_push_fields`

- **Value:** `{"Item": [fieldname, …], "Serial No": [fieldname, …]}`
- **Purpose:** custom fields whose change queues a (lean) WooCommerce push, like the core's price and status fields. List every field your `gunshop_woo_product_meta` provider reads. Fires while switched off too (hooks are static); your provider then contributes nothing.

### `gunshop_extension_toggled`

- **Signature:** `(app, effective: bool)`
- **Purpose:** once per change of the effective state of `app` while installed (switch flip, or a compatibility change after a core update); not on install or uninstall. Every provider is called, including the extension that just stopped: **do not** wrap it in `@extension_hook`. Use it for workspace shortcuts.
- **On failure:** logged; the change is already saved.

### `gunshop_sidebar_items`

- **Value:** a list of `Sidebar Item` dicts (`type`, `label`, `link_type`, `link_to`, `icon`, `child`, `indent`, `collapsible`, `keep_closed`, …); normally a `Section Break` first and its links with `child: 1`.
- **Purpose:** while effective, the core writes the rows into the Gun Shop sidebar's site layer (`Custom Sidebar`) before Settings, and removes them when off or uninstalled (rebuilt on toggle, install, uninstall and every migrate). Modules that mirror the Gun Shop sidebar get the same layer.
- **On failure:** rows skipped; no section for the extension.

### `gunshop_dropship_listing`

- **Signature:** `(item_code, distributor | None, fields: tuple) -> dict | None`
- **Purpose:** the Item's website drop-ship listing at that distributor (any house when `None`), as a dict of `fields`; read by the counter's quote, fulfilment matrix and drop-ship routing. First non-None wins. Its presence (from an effective extension) also switches on the Catalog Service / Drop-ship Center buttons.
- **On failure:** raises into the caller (never a silent "not listed").

### `gunshop_dropship_suggested_price`

- **Signature:** `(distributor_doc, catalog_row, listing) -> number | None`
- **Purpose:** the counter's suggested price for a catalog product under drop-ship pricing. First non-None wins; without a provider: MAP, else MSRP, else cost.
- **On failure:** raises into the quote.

### `gunshop_distributor_source_settled`

- **Signature:** `(source_doctype, source_name)`
- **Purpose:** every distributor order of a non-counter source (a web order) has settled; complete the source. Enqueue after commit: the call can come from inside the last order's save. Every provider is called.
- **On failure:** raises into that distributor order's save (it rolls back).

### `gunshop_distributor_feed_stale`

- **Signature:** `(distributor, *, aged: bool) -> bool`
- **Purpose:** a distributor's quantity feed is stale (`aged`: it had a heartbeat and aged out; a cold start is not). Zero what you publish from it only when `aged`; return `True` when your zero-stock interlock is on.
- **On failure:** raises into the watchdog run.

### `gunshop_distributor_hub_rows`

- **Signature:** `(rows)`; one `frappe._dict` per house, edited in place.
- **Purpose:** add display columns to the Distributors hub.
- **On failure:** the column stays blank.

### `gunshop_ai_connected`

- **Signature:** `() -> bool`, no network call (read at every desk boot). Wrap it in `@extension_hook`.
- **Purpose:** any truthy answer makes `ffl_integrations.data_service.capabilities()["ai"]` `"connected"` (`frappe.boot.ffl_capabilities.ai`).
- **On failure:** not connected; logged at most once an hour.

### `gunshop_card_processors`

- **Signature:** `() -> CardProcessor` (an `ffl_integrations.payments.base.CardProcessor`).
- **Purpose:** one card gateway: start / poll / cancel a charge on the device, void, refund, list terminals, a read-only probe for Settings → Payments, answering `CardResult` and raising `CardProcessorError` with an honest `outcome_known`. The core keeps every money fact (charge rows, refund locks, the split across cards, terminal payments and their Payment Entries); the adapter writes no document. At most one processor may be on and configured (`ready()`); each charge row records the processor's `code`, and void / refund / poll go back to that processor. Optional card on file: `supports_card_on_file = True`, accept `save_card` in `start_charge`, answer the token in `CardResult.stored_credential` (+ `card_expiry`, MMYY), implement `charge_stored`, `find_stored_charge(order_reference)`, `delete_stored(merchant_reference)` and `order_reference(document, attempt)`. Never put the token in `raw`.
- **On failure:** that processor counts as not installed: no new charges; a refund of its charge waits for a manager's Confirm Portal Refund.

### `gunshop_crm_mail_screen`

- **Signature:** `(mail: dict) -> {"customer": bool, "reason": str, "category": str} | None`. `mail` is `{"communication": name, "sender": address, "sender_name": str, "to": [addresses], "subject": str, "body": plain text, 4000 characters at most, "team_address": the team address it came to}`.
- **Purpose:** judge mail to a team address (Lead Mail Settings) from someone with no lead and no customer contact yet. With at least one provider registered, such mail does not become a lead while it is pulled: a short background job asks the providers first. The first `customer: False` makes it other mail: a lead with the status Other mail and the answer's category, with a Comment "Not a customer inquiry (category): reason" on the mail; `None` is no opinion. Any other outcome makes an ordinary lead and shares it as without the hook. The job checks again before it asks: if the mail was linked or the sender has become a lead or a customer, the mail goes by the normal rules and no provider is asked. Later mail from that sender goes on the same lead and is not screened. Category rules, blocked domains, automated senders, known customers and known leads run before this and are not screened. With Lead Mail Settings › Spam folder set, the same providers also judge that folder's mail before anything is stored (`communication` is empty): `category: "spam"` leaves no record, `None` tries the mail again on the next run. Blocking network calls are fine (background job); wrap in `@extension_hook`.
- **On failure:** logged; counts as no opinion, so the lead is made.

### `gunshop_catalog_change_cursors`

- **Value:** `"DocType.field"` (string): a product change-feed cursor on one of your Singles.
- **Purpose:** while any registered cursor holds a value, Data Service Settings refuses to change *Catalog source*. Moving from a Local catalog to a Data Service clears every registered cursor directly in `tabSingles`, so a blank cursor must mean "start over". A malformed entry is skipped.

### Core-integration only: `gunshop_extension_updates`

Not declarable by an extension: it is not on the lint's whitelist, so an extension that declares it fails the lint ("not on the whitelist"). Only the core apps register it; `ffl_integrations` registers the Data Service's `/v1/extensions` source.

- **Signature:** `() -> source | None`; `source.latest() -> {ext_id: {version, sha256, size, requires_core}}`, `source.fetch(ext_id, version) -> bytes`, optional attribute `publisher_id`.
- **Purpose:** feeds **Update to {version}** on the Extensions page. The first non-None source is used; `ffl_core.extensions.updates.check` runs daily and on Refresh. `fetch_update` refuses bytes whose sha256 differs from `latest`'s, a package from another publisher, or one whose `requires_core` this core does not meet, then hands it to the upload pipeline.
- **On failure:** `source`: next provider; `latest`: rows keep their last answer; `fetch`: the page says the download failed.

## First-party only

Declarable only by an extension whose manifest has `first_party` (lint). They decide what the website sells and at what price, or let an app answer another app's endpoints or authenticate requests.

| Hook | Signature | Without a provider / on failure |
|---|---|---|
| `distributor_stock_source` | `(item_code) -> int \| None`: website quantity of a drop-ship listed product; first non-None wins | the store's own stock |
| `distributor_stock_batch` | `(item_codes: list) -> context manager \| None`: held open around the WooCommerce reconcile's Pass 1 so `distributor_stock_source` can batch reads; must not change answers | logged, pass runs unbatched |
| `dropship_source_for_item` | `(item_code, *, lead=True) -> {distributor, sku, fulfillment_mode, lead_days} \| None` | the store ships it (lane `local`) |
| `distributor_web_price` | `(item_code) -> float \| None`: website price | `Item.standard_rate` |
| `map_mode_for_item` | `(item_code, brand) -> str`; the answer `Always show in cart` also rides an Item with no MAP (empty `_osa_map_price`), so the storefront hides its price | `MAP is minimum price` |
| `distributor_listing_push_failed` | `(item_code, site, error)` | logged |
| `woo_order_cancelled` | `(woo_online_order_name)` | logged; the order stays cancelled |
| `override_whitelisted_methods` | Frappe's hook, `{old path: new path}`: only to keep an endpoint that moved from the core into the extension answering at its old path | — |
| `auth_hooks` | Frappe's hook: authenticates a request | — |

## The JavaScript registry

The bundle registers once; the core decides where things render, orders them, de-duplicates them and hides them while the extension is not effective.

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
  serialActions:   [{ id, label, onClick({ frm }) }],
  customerActions: [{ id, label, onClick({ frm }) }],
  settingsLinks:   [{ label: __('Layaway'), doctype: 'Acme Layaway Settings' }],
  onDeskReady() { /* once per page load */ },
});
```

| Entry | Renders | Notes |
|---|---|---|
| `posActions` | Sale-screen buttons, after the core's | `condition(ctx)` synchronous and O(1); `ctx` is a read-only snapshot (invoice, customer, selected serial, POS Profile). |
| `serialActions` | Serial No form, **Extensions** group | `onClick({ frm })` |
| `customerActions` | Customer form, **Extensions** group | `onClick({ frm })` |
| `settingsLinks` | **Settings** on the extension's card | One per Settings Single. |
| `onDeskReady()` | — | Once after the desk is ready, per effective extension. A throw is logged to the console and does not stop the others. |

**Send a ticket to the register from your own page.** Build the draft on the server with `ffl_core.api.pos_ticket.build_ticket` (the caller's open shift; your rates are the ticket's and are not asked for a manager's approval — the HTTP endpoint `open_ticket` judges a caller's typed rates like the register does), then:

```js
frappe.route_options = { ffl_pos_invoice: name };   // the draft POS Invoice
frappe.set_route('point-of-sale');
```

The register opens it in the cart the way Recent Orders › Draft does (a sale already in the cart is parked first, after asking); the clerk adds lines and takes payment as usual. A name that is not an open draft is refused with a notice.

A rate you put on the ticket is your code's price: the register does not ask a manager to approve it afterwards. When a person chose a price below list on your page, take the manager's approval in your own flow before you build the ticket.

Also available: `gunstore.extensions.isEnabled(id, boot)`, `ffl.featureOffPage(page, featureTitle, whereToTurnOn)` for your own Desk pages, `gunstore.renderReleaseNotes(entries)`.

### Shared front-end helpers

Loaded on every Desk page (`app_include_js`). Read them through `window.ffl` and fall back when absent, as a core older than your extension may not have them.

- `ffl.aamva.parse(raw, { ids })`: reads the text a license scanner typed (the PDF417 barcode on a US driver license or ID card). Returns `null` when the text is not a license, else `{first, middle, last, suffix, address1, address2, city, state, zip, country}`, with `{idNumber, dob, expiry}` (dates `YYYY-MM-DD`, `''` when unreadable) added only when `ids` is true. Pass `ids: !!frappe.boot.ffl_store_customer_id_numbers`, the store's choice to keep ID numbers.
- `ffl.customerFlags.check(customer, { key })`: the counter's customer check, as the register runs it. Returns `Promise<boolean>`: `false` when the customer is Banned and no manager overrode it in the box shown; otherwise `true`, after showing the customer's active alerts once per `key`. An empty customer or a failed read resolves `true` (the server still refuses a sale to a Banned customer).
- `ffl.customerQuickEntry.newFromScan(raw)`: opens the New Customer dialog for an individual, filled from that scan by the dialog's own Scan ID (name and address; the license number and birth date only while the store keeps ID numbers). Returns `Promise<string | null>`: the customer's name once the clerk saves it, or picks an existing customer at the duplicate check; `null` when the dialog is closed without one, when the clerk moves to Edit in full page, or when the site opens the full Customer form instead of the dialog. Called again before it has answered, it returns the same promise and fills that same dialog from the new scan (from the newest one, if the dialog is still loading).

## Desk pages, sidebar, shortcuts

- List your pages in the Gun Shop sidebar and the Dashboard's **All records** with `gunshop_sidebar_items`.
- To give your module's pages the Gun Shop sidebar, call `ffl_core.setup.sidebar.mirror_gun_shop_sidebar(<name>, module=<module>, extra_items=[...])` from `after_install` / `after_migrate`. Frappe lets it write a sidebar only during install, migrate, a patch or developer mode.
- Declare `add_to_apps_screen` with a `has_permission` that returns `False`, so the extension does not appear as a separate app.
- `ffl_core.workspace_shortcuts.ensure_shortcut(label, link_to, *, anchor, block_id, color="Blue", shortcut_type="DocType", workspace="Settings")` / `remove_shortcut(label, *, workspace="Settings")`; `shortcut_type` is `DocType`, `Page`, `Report` or `URL`. Call them from `gunshop_extension_toggled`.
