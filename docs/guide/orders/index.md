# Orders and shipping

For counter staff, when an order will not leave the store in the customer's hands today: it ships, waits for goods, or came in from the web store or GunBroker.

Every such order ends up on one screen, **Pending Order**. Open it from **Orders** under **Sales** in the sidebar. It shows counter, web and GunBroker orders side by side, grouped by what has to happen next.

## I need to...

| I need to... | Go to |
|---|---|
| Build an order away from the register | [Build an order and its splits](build-an-order.md) |
| Ship a gun to another dealer | [Ship a firearm to another dealer](ship-to-dealer.md) |
| Ship ammunition or accessories to a customer | [Ship accessories to a customer](ship-to-customer.md) |
| Take a deposit on something the store does not have | [Deposits and special orders](special-orders.md) |
| Change or cancel an order | [Change or cancel an order](change-an-order.md) |
| See every order still waiting on something | [Pending Orders](pending-orders.md) |
| Book guns out, ship, or hand an order over | [Ship or hand over an order](ship-or-hand-over.md) |
| Finish an order from the web store | [Web orders](web-orders.md) |
| Finish a GunBroker sale | [GunBroker orders](gunbroker-orders.md) |

## Where online orders show

The sidebar's **Online** section has two lists. Each one shows only while its integration is switched on.

| Sidebar entry | What it opens |
|---|---|
| **Web orders** | Every order the WooCommerce store sent in |
| **GunBroker orders** | Every order read from GunBroker |

Day to day, work these orders from **Orders** under **Sales** instead. The lists are for looking an order up.

## How you hear about a new order

The notifications bell rings when an order comes in: "New web order #…" or "New GunBroker order #…", with the buyer and the order total. It rings for counter staff and managers, inside the app only. Nothing is emailed.

A web order still awaiting payment rings later, when WooCommerce moves it on. See [the Dashboard](../start/dashboard.md) for the bell.

## Prices and stock come from the store system

The POS sets what a gun costs and whether it is available, and sends both to the web store and to GunBroker. A price typed on WooCommerce or on GunBroker is overwritten by the next update from the POS. Change prices in the POS. See [List a gun online](../stock/list-online.md).

Orders travel the other way. A web or GunBroker sale comes back into the POS, where the money, the stock and the bound book entry are recorded. The online channels keep their own order record: the POS does not edit or cancel a web order, and it cannot undo a GunBroker sale.
