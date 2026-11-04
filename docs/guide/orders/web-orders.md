# Web orders

For counter staff, when a customer buys on the WooCommerce store and the order has to be booked out, shipped or picked up here.

## How a web order arrives

The store sends each order to the POS the moment it is placed. The POS makes a web order record, numbered `WOO-ORD-` and a number, and the notifications bell rings: "New web order #…" with the buyer and the total. An order still awaiting payment rings later, when WooCommerce moves it on.

At the same moment the POS holds stock for it:

- A per-gun listing carries the serial, so that exact gun is held.
- A listing that covers several identical guns holds a unit without naming a gun, because staff choose which one comes off the rack. It names one only when exactly one gun is free and no other order holds an unnamed unit.
- A non-firearm line holds nothing.
- When no free gun exists at all, the line records a reservation error for staff to sort out.

The web store stops offering a gun as soon as it is held.

!!! note
    A held unit without a named gun is a placeholder. If staff name that exact gun on another order, this order is given a different unit of the same item. A serial a person has confirmed is never taken away.

Only a paid order reaches the [Pending Order](pending-orders.md) queue. An unpaid order still holds its gun, but nothing ships until the customer pays.

## Ship a web order to the buyer's dealer

1. Find the order on **Pending Order**. The **Web** chip shows only web orders.
2. If the card reads **Awaiting serial from WooCommerce**, open the order on the WooCommerce Edit Order screen and enter the gun's serial there. It comes back to the POS by itself.
3. Pull the gun and the other goods.
4. Click **Dispose** and work the dialog. See [Book the guns out](ship-or-hand-over.md#book-the-guns-out-dispose).
5. Buy the label with the ShipStation plugin on WooCommerce. The order is already there, with the store's own address and items.

A web card offers **Dispose** only. There is no **Record Payment** (the buyer paid on the store), no **Edit** or **Cancel Order** (change the order on WooCommerce), and no **Ship**. A label pushed from the POS would be a second shipment for the same parcel.

If the serial you typed is not free stock, is the wrong item or is held by another order, the card shows a **Serial conflict** badge with the reason. Correct it on the WooCommerce order.

What happens at Dispose:

- Each gun is in the [bound book](../compliance/bound-book.md) as a transfer and goes to [FastBound](../admin/fastbound.md), and the goods leave stock.
- The sale is recorded with the WooCommerce amounts.
- The listing comes off the store the order came from.

## A collector (C&R, Type 03) buyer

When the buyer gave a Type 03 collector license, the order's **C&R (03) Transfer** box is ticked. A collector has no dealer at the far end and no Form 4473, so the **C&R (Type 03) verification** block in the Dispose dialog is the verification. The same block appears on a GunBroker order to a collector.

1. Read the license itself, every time. Last month's expiry date is never carried over.
2. Enter the **C&R license number** and the **License expiration date**.
3. Enter the **Collector's full name**.
4. Enter the licensed premises: **Address 1**, **Address 2**, **City**, **State**, **ZIP**. The parcel ships there.
5. Tick **I have compared these details against the collector's C&R license**.

The button stays disabled until every required field is filled, the license number has 03 as its fourth group, and the expiry is today or later. Premises whose state or ZIP do not match the shipping address are refused. A counter C&R order is checked on [Create Order](build-an-order.md) instead.

## The buyer picks up here

When the buyer chose your own shop from the FFL list at checkout, they collect at the counter. The card shows **In-store pickup** and a **Pickup** button, and Dispose is refused: a transfer to yourself would record an exit that never happened. Your license number is set in [FFL Settings](../admin/ffl-settings.md).

1. Click **Pickup**. For a gun, a draft register ticket opens, already paid in full by the web payment.
2. Run the [Form 4473 and NICS](../compliance/4473.md) on that ticket.
3. Complete the ticket at the register. Do not take money again.

Anything else the customer buys goes on a ticket of its own. This ticket completes only while it totals what WooCommerce charged.

The order stays on the queue until that ticket is completed, so an abandoned draft stays visible. The card meanwhile offers **Open counter order**, which returns you to the same draft. An order with no regulated gun has no 4473, so Pickup closes it on the spot.

Pickup is refused while the order is unpaid on WooCommerce, while it carries a WooCommerce refund, while a distributor leg on it is still outstanding, or while a gun ordered to the store has not arrived.

## Read the web order

Open a web order from the link on its card, or from **Web orders** under **Online** in the sidebar.

| Section | What it holds |
|---|---|
| **WooCommerce Status** | The live status on the store. `processing`, `completed` and `awaiting-stock` count as paid. |
| **Transferred At** | When the POS finished the order. |
| Receiving FFL | The dealer chosen at checkout, its license, address and **FFL Expiration**. |
| **ATF FFL Record** | The license the POS matched. Dispose uses this, not the typed number. |
| C&R Verification | Filled in by staff at Dispose, with **Verified By** and **Verified At**. |
| Ship To (buyer) | Where non-firearm goods ship. Normally the dealer's premises. |
| Amounts | Totals copied from WooCommerce, as shown on the card. |
| Order Lines | One row per line, with any reservation error and **Taken From Location**. |
| Firearm Serials | One row per gun: **Confirmed Serial**, **Stock Hold Serial**, **Reservation Error**. |
| WooCommerce Refunds | Refunds recorded on the store. |

A blank **Confirmed Serial** means the order is still waiting for staff to name the gun. A shopper can choose at checkout to have non-firearm items shipped to their own address. The gun always goes to the receiving dealer. Fix a wrong address on WooCommerce, not here.

A refund is not something you net down at the counter. Settle it on WooCommerce first. The bookkeeper's view of refunds is in [For the bookkeeper](../admin/bookkeeper.md).

## Troubleshooting

| You see | Fix |
|---|---|
| Awaiting serial from WooCommerce | Enter the serial on the WooCommerce Edit Order screen. |
| No Active unreserved serial available | The order sold a gun with no free stock. Check stock, then correct the order on WooCommerce. |
| Verify the FFL first | Verify the buyer's dealer license, then dispose. See [Verify an FFL](../compliance/ffl-verification.md). |
| has not been paid on WooCommerce | Take payment on WooCommerce, then ring the pickup. |
| Held for a web order | The register tried to sell units a paid web order is waiting for. Ship them to the web customer, or ask a manager to ring the sale. |
