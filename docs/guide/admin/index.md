# Admin and bookkeeper

For the store's administrator and its bookkeeper, when you set up the system, connect outside services, or close the books.

These pages use some system terms that counter staff never need. Every page here needs System Manager, or an accounts role for the bookkeeping pages.

| I need to... | Go to |
|---|---|
| Close the month and hand figures to the accountant | [Month-end close](month-end.md) |
| See what a sale, a refund or a correction books in the accounts | [For the bookkeeper](bookkeeper.md) |
| Connect or fix FastBound, Payroc, a distributor, ShipStation, GunBroker, WooCommerce or the Data Service | [Integrations](integrations.md) |
| Set the store's FFL number, alert emails, counter rules or 4473 mode | [FFL Settings](ffl-settings.md) |
| Know what the system does overnight and where its alerts go | [What runs automatically](background-jobs.md) |
| Fix a record FastBound refused, or check the two books agree | [FastBound](fastbound.md) |
| Switch a distributor on, test it or change its priority | [Distributor setup](distributors.md) |
| Understand how the web store and GunBroker stay in step | [Web store and GunBroker setup](online-channels.md) |

Installing and upgrading the system itself is in the [self-hosting guide](../../self-hosting.md).

!!! warning "Never read a secret back out"
    Settings pages that hold API keys, passwords and webhook secrets store them encrypted, to be written once. Never copy one into a message, a ticket or a document. If a credential is wrong, replace it.
