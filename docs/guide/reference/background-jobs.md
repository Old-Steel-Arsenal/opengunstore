# What the system does on its own

Some things happen without anyone clicking. Know what they are, so a document that changed overnight reads as normal rather than as someone else's mistake.

Everything below runs on the server's own schedule. Cron times are evaluated in the site's time zone, so 23:00 means 11 PM in the shop's zone. Jobs that belong to an extension stop when that extension is switched off under Settings → Extensions.

## The schedule

| When | What runs |
|---|---|
| 07:00 daily | Membership renewals |
| 23:00 daily | Automatic POS close |
| 00:05 daily | GunBroker sale-window price sweep |
| Daily | Membership close-out, FastBound push and delete retries, FastBound list refresh, stuck-record digest, GunBroker audit, item quantity self-heal, sale price refresh, abandoned 4473 alert, pending-orders digest, card charge check, Sports South credit poll, extension update check |
| Hourly | Stale trade-in draft nudge, signed 4473 PDF copies, GunBroker listing sweep, distributor invoice pull, storage location sweep |
| Every 30 minutes | Distributor order poll |
| Every 15 minutes | POS consolidation safety net, feed staleness watchdog, category mirror, local catalog sync, GunBroker order poll, GunBroker end-listing confirmation, auction bid watch |
| Every 10 minutes | GunBroker tracking poll |
| Every 2 hours | WooCommerce reconcile, order audit, serial reservation retry, shipped-undisposed check, web revenue backstop |

Some of these do nothing until the matching integration is switched on. A GunBroker job, for example, is a no-op while GunBroker Settings has **Enable GunBroker** off.

## The nightly POS close

At 23:00 the system closes every shift that is still open. It builds a POS Closing Entry for each one, sets the counted amount equal to the expected amount so the variance is zero, and submits it. That consolidates the day's POS Invoices into Sales Invoices, so the books are clean by morning.

A shift opened within the last hour is skipped, so a till opened shortly before 11 PM is not swept up by accident. A night the job misses is caught the next night.

This is a backstop, not the intended way to end a day. An automatic close trusts the till by definition, because nobody counted the drawer. Count the drawer and press **Close the day** on the [cash drawer](../selling/cash-drawer.md) page, and the nightly job finds nothing to do. See [Opening and closing the day](../selling/day-end.md).

!!! note "The close is scoped to the shift's owner"
    A sale rung up by someone other than the person who opened the shift is not picked up by the nightly close. A separate sweep runs every fifteen minutes and retries any POS Invoice whose consolidation failed or was missed, so those leftovers get consolidated the next day. If one keeps failing, the sweep raises a task on it rather than retrying forever, because an unconsolidated sale leaves a sold gun looking like it is still in stock.

## Counter and inventory

**Abandoned 4473s, daily.** A register ticket whose 4473 is still Pending two business days past its Brady date raises one task for the managers. The gun is off every sales channel while that draft lives. The job only alerts. It never deletes the draft, never releases the gun and never touches FastBound. A manager finishes the 4473 or clears the order on the [Pending 4473 Orders](../compliance/4473.md) report.

**Pending-orders digest, daily.** One email to the alert recipients listing every counter order that is blocked, or older than **Pending Order overdue after (days)** on [FFL Settings](../settings/ffl-settings.md). Nothing is sent when nothing is stuck. See [The Pending Order queue](../selling/pending-orders.md).

**Item quantity and sale prices, daily.** The quantity shown on each item is recounted from stock, and sale prices whose start or end date has come are applied.

**Storage locations, hourly.** A gun that has left stock has its storage location cleared, for any path the moment-of-sale update missed. See [Storage locations](../inventory/storage-locations.md).

## Memberships

**Renewals, 07:00 daily.** Each auto-renewing membership whose period has ended is
charged to its saved card. Declined ones are retried each morning until their
grace days run out, then suspended, and yearly members are emailed their
reminder. Nothing runs while **Charge renewals automatically** is off. **Close-out,
daily.** A membership that does not renew turns Cancelled or Expired after its
last day. See [Memberships](../selling/memberships.md#renewals).

## Cards

**Card charge check, daily.** The system reads the last week's captured card charges back from Payroc. A charge voided or refunded on the terminal's own menu or in the Payroc portal never reaches the POS, so the invoice still says paid. Each one found gets a comment on its invoice, and the managers get one email titled "Payroc: _N_ card charge(s) no longer captured at the gateway". The job never re-books money. Deciding what happened is an accounting step. See [Taking payment](../selling/payments.md).

## FastBound

**Daily.** Three jobs run. Two retry pushes and deletes that failed, so a transient network problem clears itself overnight. The third refreshes the cached acquisition and disposition type lists from FastBound. Records a person has marked as not in the bound book are never retried. See [FastBound](../compliance/fastbound.md).

**Daily digest.** A fourth job reads every acquisition and disposition carrying a sync error and emails one digest listing them. Entries that will not retry on their own are tagged `[needs human]`. A cancelled record that never reached FastBound is left out, because it has no bound book obligation. The subject line reads `<n> FastBound record(s) stuck unsynced`, and at most one digest goes out per day. See [A FastBound push failed](troubleshooting.md#a-fastbound-push-failed).

**Signed 4473 copies, hourly.** For each completed electronic 4473 whose PDF has not been copied yet, the POS fetches the signed PDF from FastBound and keeps a private copy on the 4473 record. A few are done per run, newest first, so a FastBound outage catches up over the following hours. See [4473 records](../compliance/4473-records.md).

## The trade-in nudge

**Hourly.** Any draft trade-in acquisition whose possession date is before today raises an alert. It is hourly rather than daily on purpose. The bound book clock for a gun you have taken possession of runs to the close of the **next business day**, and it runs from physical possession, independent of whatever the buyer's side of the sale is doing. A delayed background check must not quietly carry an unbooked gun past that line.

The job fires stricter than the legal deadline, so staff still have the whole next business day to act. The email names each draft and its serial and says what to do: complete the checkout, submit the acquisition directly if the store is keeping the gun, or delete the draft if the customer took it back. The digest goes out at most twice a day. See [Trade-ins](../inventory/trade-ins.md).

## GunBroker

**Daily audit.** One job reads everything a GunBroker order can be stuck halfway through and reports eight kinds of problem: a gun disposed with no invoice, an invoice with no payment recorded, an order paid but not disposed past the alert threshold, a status conflict where GunBroker shows cancelled or refunded while the invoice still stands, a fee accrual left on a cancelled invoice, a chargeback, an order whose sync has failed five times running, and an invoice more than a day old with no valid fee accrual. Every step of the order flow logs its failures rather than raising them, so the bound book exit is never rolled back. That is exactly why something has to read the wreckage once a day. Clear all eight before [month end](../reports/month-end.md).

**Hourly listing sweep.** The only job that asks GunBroker what is actually true. Everything else reacts to a local event, and events go missing. It is a no-op when the integration is switched off.

**Every fifteen minutes.** Three jobs. The order poll pulls new orders down. Fifteen minutes is the window in which a gun can sell on GunBroker and still be sitting on the shelf looking available, which is the window the counter's pre-sale check covers. The end-listing confirmation chases listings the POS asked GunBroker to end and could not verify at the time. It runs on this timer rather than the hourly one because an unconfirmed end is the one state where a gun that has left the shelf can still be bought. The auction bid watch reads live auctions so the first bid locks the gun within fifteen minutes.

**Every ten minutes.** Tracking numbers are carried back from the shipping label the store bought. It only looks at orders with a step still outstanding, so a day with nothing shipping makes no calls at all.

**Shortly after midnight.** A model's sale window opens or closes at midnight with no Item save to say so, so a sweep at 00:05 re-prices the affected GunBroker listings.

## Distributors

**Order poll, every thirty minutes.** Live distributor orders are polled for status, tracking and serials, backing off from every thirty minutes to every two hours as an order ages. Orders sitting untouched for fourteen days raise an alert. An order also alerts when it was short-shipped, when its invoiced freight differs from what was booked, and when an order stays on hold for days. See [Distributor orders](../distributors/restock.md).

**Invoices, hourly.** Every invoice on each distributor account from the last 30 days is pulled into the POS, whoever placed the order, and matched to a POS order where one exists. It is a read-only copy: nothing posts to the books. **Pull invoices now** on Distributor Orders runs it at once.

**Sports South credits, daily.** Sports South credit memos post overnight. The system reads yesterday's, folds the short units onto their distributor orders and alerts for the customer refund.

**Feed staleness watchdog, every fifteen minutes.** Each distributor's quantity feed heartbeat is checked against a 90 minute threshold. Past it, the system emails once per distributor to say the published quantities are no longer trustworthy. A heartbeat that cannot be read at all is treated as stale, on the principle that not knowing is not the same as being fine.

**Local catalog sync, every fifteen minutes.** When Data Service Settings has **Catalog source** set to **Local**, each distributor feed that is due is queued on the catalog worker. With any other setting this job does nothing.

**Category mirror, every fifteen minutes.** The shared catalog category list is copied into the POS and matched to item groups and website categories. Its mode is set on Data Service Settings → **Categories**: **Off** does nothing, **Report only** (the default) lists what it would change, and **Apply** changes it.

## WooCommerce

**Every two hours.** Five jobs share one tick. Products are reconciled against the store, on the schedule set in WooCommerce Settings. The order audit checks once a day for store orders missing here. Stuck serial reservations are retried, bounded so a backlog cannot flood the store with calls. A check looks for orders marked shipped on the store but still pending here, which would mean a firearm out of the building with no bound book entry, and reports once a day. Last, a backstop re-runs revenue bookings and stock legs that never landed.

## Extension updates

**Daily.** The POS asks the update service which newer versions of its extensions exist, so the Extensions page can offer **Update**. It installs nothing. With no update service configured, the check has nothing to ask.

## Where alerts land

Three places, and all of them matter.

**Email** goes to the addresses in **Alert Recipients** on [FFL Settings](../settings/ffl-settings.md). Leave that field empty and every System Manager receives them instead, so an alert is never silently dropped. Subjects start with `[POS Alert]`. Repeated failures are throttled per kind of problem, so something failing every fifteen minutes sends one message, not ninety-six.

**An assigned task** is raised on the document itself for a stranded record: a stock leg that could not be issued, a revenue booking that keeps failing, an invoice stuck in draft, an abandoned 4473. It goes to the stock managers, the sales managers and the system managers, and it is raised exactly once and stays open until a person closes it. That is deliberate. A condition that persists gets one task, not one per run, and closing the task is how you say a human has dealt with it.

**The notification bell** in the app rings as things happen, not on a schedule. It is never emailed.

| Bell | Who hears it |
|---|---|
| A NICS verdict on a sale: Proceed, Delayed, Denied, and a Delayed check clearing | Sales User, Sales Manager, System Manager |
| A new web order or GunBroker order | Counter roles: Sales User, Sales Manager, Stock Manager, System Manager |
| A distributor shipped a stocking or ship-to-store order | Counter roles |
| FastBound refused a record for good | Stock Manager, Sales Manager, System Manager |

The Administrator account never gets bells. See [the Dashboard](../getting-started/dashboard.md).

Check all three as part of [Daily checks](../compliance/daily-checks.md).
