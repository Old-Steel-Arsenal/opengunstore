# What runs automatically

For the administrator and managers, when a record changed overnight or an alert arrived and you want to know which job did it.

Some things happen without anyone clicking. Every job below runs on the server's own schedule, in the site's time zone, so 23:00 means 11 PM in the shop. Jobs that belong to an extension stop when that extension is switched off on the Extensions page.

## The schedule

| When | What runs |
|---|---|
| 07:00 daily | Membership renewals |
| 23:00 daily | Automatic register close |
| 00:05 daily | GunBroker sale-window price sweep |
| Daily | Membership close-out, FastBound push and delete retries, FastBound list refresh, stuck-record digest, GunBroker audit, item quantity self-heal, sale price refresh, abandoned 4473 alert, pending-orders digest, card charge check, Sports South credit poll, extension update check |
| Hourly | Stale trade-in draft alert, signed 4473 PDF copies, GunBroker listing sweep, distributor invoice pull, storage location sweep |
| Every 30 minutes | Distributor order poll |
| Every 15 minutes | Register consolidation safety net, feed staleness watchdog, category mirror, local catalog sync, GunBroker order poll, GunBroker end-listing confirmation, auction bid watch |
| Every 10 minutes | GunBroker tracking poll |
| Every 2 hours | WooCommerce reconcile, order audit, serial reservation retry, shipped-undisposed check, web revenue backstop |

A job for an integration does nothing while that integration is switched off. A GunBroker job, for example, does nothing while **Enable GunBroker** is off in GunBroker Settings.

## The nightly register close

At 23:00 the system closes every register shift still open. For each one it builds a POS Closing Entry with the counted amount set to the expected amount, so the variance is zero, and submits it. That consolidates the day's POS Invoices into Sales Invoices, so the books are clean by morning.

A closing entry that could not post shows **POS Closing Failed**: open it, read **Error**, fix the cause and click **Retry**.

A shift opened within the last hour is skipped, so a till opened shortly before 11 PM is left alone. A night the job misses is caught the next night.

This is a backstop. An automatic close trusts the till, because nobody counted the drawer. Count the drawer and press **Close the day** on the [Cash drawer](../counter/cash-drawer.md) page, and the nightly job finds nothing to do. See [A day at the counter](../start/day-at-the-counter.md).

!!! note "The close covers the shift owner's sales"
    A sale rung up by someone other than the person who opened the shift is not picked up by the nightly close. A separate sweep every fifteen minutes retries any POS Invoice whose consolidation failed or was missed.

    If one keeps failing, the sweep raises a task on it instead, because an unconsolidated sale leaves a sold gun looking like it is still in stock.

## Counter and inventory

**Abandoned 4473s, daily.** A register ticket whose 4473 is still Pending two business days past its Brady date raises one task for the managers. The gun stays off every sales channel while that draft lives.

The job only alerts. It never deletes the draft, releases the gun or touches FastBound. A manager finishes the 4473 or clears the order. See [Form 4473 and NICS](../compliance/4473.md).

**Pending-orders digest, daily.** One email to the alert recipients listing every counter order that is blocked, or older than **Pending Order overdue after (days)** in [FFL Settings](ffl-settings.md). Nothing is sent when nothing is stuck. See [Pending Orders](../orders/pending-orders.md).

**Item quantity and sale prices, daily.** The quantity shown on each item is recounted from stock. Sale prices whose start or end date has come are applied.

**Storage locations, hourly.** A gun that has left stock has its storage location cleared, for any path the moment-of-sale update missed. See [Find and move stock](../stock/find-and-move.md).

## Memberships

**Renewals, 07:00 daily.** Each auto-renewing membership whose period has ended is charged to its saved card. Declined ones are retried each morning until their grace days run out, then suspended. Yearly members are emailed their reminder.

Nothing runs while **Charge renewals automatically** is off. See [Memberships](../counter/memberships.md).

Close-out, daily. A membership that does not renew turns Cancelled or Expired after its last day.

## Cards

**Card charge check, daily.** The system reads the last week's captured card charges back from Payroc. A charge voided or refunded on the terminal's own menu or in the Payroc portal never reaches the POS, so the invoice still says paid.

Each one found gets a comment on its invoice, and the managers get one email titled "Payroc: _N_ card charge(s) no longer captured at the gateway". The job never re-books money. See [Card voids and refunds](../manager/card-refunds.md).

## FastBound

Retries and lists, daily. Two jobs retry pushes and deletes that failed, so a passing network problem clears overnight. A third refreshes the cached acquisition and disposition type lists. Records a person has marked as not in the bound book are never retried.

Stuck-record digest, daily. One email lists every acquisition and disposition carrying a sync error. Entries that will not retry on their own are tagged `[needs human]`, and a cancelled record that never reached FastBound is left out. The subject reads `<n> FastBound record(s) stuck unsynced`, at most once a day.

See [FastBound](fastbound.md#failed-pushes) for what to do with each.

**Signed 4473 copies, hourly.** For each completed electronic 4473 whose PDF is not copied yet, the POS fetches the signed PDF from FastBound and keeps a private copy on the 4473 record. A few are done per run, newest first, so an outage catches up over the following hours. See [Find a 4473](../compliance/4473-records.md).

## The trade-in alert

**Hourly.** Any draft trade-in acquisition whose possession date is before today raises an alert. The bound book deadline for a gun you have taken possession of is the close of the **next business day**, counted from physical possession, whatever the buyer's side of the sale is doing.

The alert fires earlier than the legal deadline, so staff still have the whole next business day. The email names each draft and serial and says what to do:

- complete the checkout;
- submit the acquisition directly, if the store is keeping the gun;
- delete the draft, if the customer took the gun back.

It goes out at most twice a day. See [Trade-ins](../counter/trade-ins.md).

## GunBroker

**Daily audit.** One job reads every place a GunBroker order can be stuck halfway and reports eight kinds of problem:

1. a gun disposed with no invoice;
2. an invoice with no payment recorded;
3. an order paid but not disposed past the alert threshold;
4. GunBroker shows cancelled or refunded while the invoice still stands;
5. a fee accrual left on a cancelled invoice;
6. a chargeback;
7. an order whose sync failed five times running;
8. an invoice more than a day old with no valid fee accrual.

Each step of the order flow logs its failures instead of stopping, so the bound book exit is never rolled back. Clear all eight before [month end](month-end.md).

**Hourly listing sweep.** Asks GunBroker what is actually live, to catch any event the other jobs missed.

Every fifteen minutes, three jobs:

- The order poll pulls new orders down. A gun can sell on GunBroker and still sit on the shelf looking available for up to fifteen minutes; the counter's pre-sale check covers that window.
- The end-listing confirmation chases listings the POS asked GunBroker to end and could not confirm. Until confirmed, a gun that has left the shelf can still be bought.
- The auction bid watch reads live auctions, so the first bid locks the gun within fifteen minutes.

**Every ten minutes.** Tracking numbers come back from the shipping label the store bought. Only orders with a step still outstanding are read.

00:05 daily. A model's sale window opens or closes at midnight, so this sweep re-prices the affected GunBroker listings.

## Distributors

**Order poll, every thirty minutes.** Live distributor orders are polled for status, tracking and serials, slowing to every two hours as an order ages.

An order raises an alert when it sits untouched for fourteen days, was short-shipped, has invoiced freight that differs from what was booked, or stays on hold for days. See [Order from a distributor](../stock/distributor-orders.md).

**Invoices, hourly.** Every invoice on each distributor account from the last 30 days is pulled in, whoever placed the order, and matched to a POS order where one exists. It is a read-only copy and posts nothing to the books. **Pull invoices now** on Distributor Orders runs it at once.

**Sports South credits, daily.** The system reads yesterday's Sports South credit memos, folds the short units onto their distributor orders and alerts for the customer refund.

**Feed staleness watchdog, every fifteen minutes.** Each distributor's quantity feed is checked against a 90-minute threshold. Past it, the system emails once per distributor that the published quantities can no longer be trusted. A feed that cannot be read at all counts as stale.

**Local catalog sync, every fifteen minutes.** With **Catalog source** set to **Local** on Data Service Settings, each distributor feed that is due is queued on the catalog worker. Otherwise it does nothing.

**Category mirror, every fifteen minutes.** The shared category list is copied in and matched to item groups and website categories, per the **Mode** on Data Service Settings. See [Integrations](integrations.md#data-service-settings).

## WooCommerce

Every two hours, five jobs share one tick:

- Products are reconciled against the web store, on the schedule set in WooCommerce Settings.
- The order audit checks once a day for web orders missing from the POS.
- Stuck serial reservations are retried, in small batches.
- A check looks for orders marked shipped on the web store but still pending here, which would mean a gun out of the building with no bound book entry. It reports once a day.
- A backstop re-runs revenue bookings and stock legs that never landed.

## Extension updates

**Daily.** The POS asks the update service which newer versions of its extensions exist, so the Extensions page can offer **Update**. It installs nothing.

## Where alerts land

**Email** goes to **Alert Recipients** in [FFL Settings](ffl-settings.md), or to every System Manager when that field is empty. Subjects start with `[POS Alert]`. Repeats are throttled per kind of problem, so something failing every fifteen minutes sends one message, not ninety-six.

A task is raised on the record itself when something is stranded: a stock leg that could not be issued, a revenue booking that keeps failing, an invoice stuck in draft, an abandoned 4473. It goes to the stock, sales and system managers, once, and stays open until a person closes it. Closing the task records that someone dealt with it.

**The bell** in the app rings as things happen. It is never emailed.

| Bell | Who hears it |
|---|---|
| A NICS verdict: Proceed, Delayed, Denied, and a Delayed check clearing | Sales User, Sales Manager, System Manager |
| A new web or GunBroker order | Sales User, Sales Manager, Stock Manager, System Manager |
| A distributor shipped a stocking or ship-to-store order | Same counter roles |
| FastBound refused a record for good | Stock Manager, Sales Manager, System Manager |

The Administrator account never gets bells. Check all three places as part of [Daily checks](../compliance/daily-checks.md).
