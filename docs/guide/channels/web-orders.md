# Web orders

A customer buys on the WooCommerce store. This page follows that order into the POS: what record it becomes, what the fields mean, and what staff do to finish it. Counter staff work these from the [Pending Order queue](../selling/pending-orders.md).

## How the order arrives

WooCommerce sends the order to the POS the moment it is placed, through a webhook. The POS creates a **Woo Online Order** record, one per web order, named `WOO-ORD-` and a number. The notifications bell rings for counter staff and managers: "New web order #…" with the buyer and the total. An order placed while still awaiting payment rings later, when WooCommerce moves it on.

At the same moment the POS soft-reserves stock:

- A per-gun listing carries the serial in its SKU, so that exact Serial No is reserved.
- A listing that covers several identical guns has no serial in it. The POS records a claim on a unit and names no gun, because it cannot know which one staff will pull off the rack. It names one only in the single case where exactly one gun is free, this order has spent nothing yet and no other order holds an unnamed claim. With one candidate there is nothing to choose between.
- A non-firearm line records the line only. Nothing is reserved.
- When no free serial exists at all, the line records a reservation error for staff to sort out.

The reservation is pushed back to WooCommerce right away, so the storefront stops offering a gun that is already spoken for.

!!! note
    A placeholder hold is a guess, not a decision. If staff later name that exact gun on a different order, the placeholder moves aside and the other order gets a different unit of the same item. A serial a human has actually confirmed is never taken away.

## Reading a Woo Online Order

Open one from the link on its Pending Order card, or from **Web orders** under **Online** in the sidebar.

### Status fields

| Field | Values | Meaning |
|---|---|---|
| **WooCommerce Status** | The live status from the store | `processing`, `completed` and `awaiting-stock` count as paid. |
| **Site** | retail | The store the order came from. |
| **Transferred At** | Timestamp | When the POS finished the order. Revenue is dated by it. |

The POS also keeps its own fulfillment status (Pending Fulfillment, Transferred or Cancelled). It is hidden from the form and managed automatically. Only a Pending Fulfillment order that is paid appears on the queue.

An order that is placed but not paid still holds its serial, and still does not reach the Pending Order queue. A firearm does not ship for an order the customer has not paid for.

### Receiving FFL

The dealer the buyer chose at checkout: **FFL License Number**, **FFL Business Name**, **FFL Phone**, the premises street, city, state and ZIP, and **FFL Expiration**.

**ATF FFL Record** links the resolved license in the POS. That link, not the typed number, is what the disposition uses. **FFL Raw Meta (fallback)** keeps what the storefront sent, for the case where nothing resolved.

**C&R (03) Transfer** is ticked when the buyer gave a Type 03 collector license rather than a dealer's. There is no dealer at the far end and no Form 4473, so the dispose dialog asks staff to verify the license by hand.

### C&R Verification

Filled in by staff at the moment of dispose, not by the storefront: **Collector Name (as printed on license)**, **C&R License Expiration**, the licensed premises address, plus **Verified By** and **Verified At**.

The expiration is deliberately not carried over from a previous order. A license that was valid last month may have lapsed since, so it is read off the license every time.

### Ship To (buyer)

The address the buyer's non-firearm goods ship to. On an order with a gun, the store sets the shipping address to the receiving dealer, so by default this block holds the dealer's premises. A shopper can choose at checkout to have the non-firearm items shipped to their own address instead. Then this block holds that address. The gun always goes to the receiving dealer.

Fix a wrong address on WooCommerce, not here.

### Amounts

**Order Total**, **Total Tax**, **Shipping Total** and **Discount Total**, in the order's own currency, copied from WooCommerce. The POS does not recompute them. These are the figures the Pending Order card shows.

### Order Lines

One row per WooCommerce line: item code, quantity, unit price, subtotal, line total and line tax. A line that could not be reserved carries its reservation error here. **Taken From Location** records where in the store staff took the units from at Dispose.

### Firearm Serials

One row per regulated firearm unit on the order. This is the table staff and the POS actually work from.

| Column | Meaning |
|---|---|
| **Item Code** | The item this unit is for. |
| **Unit #** | Which unit of a multi-quantity line this row is. |
| **Confirmed Serial** | The gun staff named for this unit. Blank means the order is still waiting. |
| **Stock Hold Serial** | The placeholder gun the POS is holding meanwhile. |
| **Reservation Error** | Why a serial that arrived was refused. |

### WooCommerce Refunds

Refunds recorded against the order on WooCommerce: refund id, amount, reason, and when the POS recorded it. A refunded order is not something the POS nets down at the counter. Settle the refund on WooCommerce first.

| Column | Meaning |
|---|---|
| **Tax Refunded** | Sales tax WooCommerce gave back in this refund. |
| **Shipping Refunded** | Shipping WooCommerce gave back, before tax. |
| **Breakdown** | Fetched, Pending, Not in Woo or Rejected. |

When the breakdown is **Fetched**, the refunded tax comes off the invoice's tax line and the refunded shipping off its shipping line, so sales tax payable drops with the refund. **Pending** means the breakdown has not been read yet. The whole refund books as a discount on the goods until the reconcile job reads it. **Not in Woo** and **Rejected** also book as a goods discount, and an alert was raised for each.

## Entering the serial

For a per-gun listing the serial is already known. For any other firearm line, somebody has to say which gun is going, and that happens on the WooCommerce Edit Order screen, not in the POS. The serial travels back through the same webhook and lands in **Confirmed Serial**.

Until it does, the Pending Order card shows **Awaiting serial from WooCommerce** and the Dispose button is disabled. If the serial you typed is not free stock, is the wrong item, or is already held by another order, the card shows a **Serial conflict** badge and prints the reason. Correct it on the WooCommerce order.

## On the Pending Order queue

Paid web orders show up as cards tagged **Web**, mixed in with counter and GunBroker rows. The **Web** chip at the top of the page shows only them.

A web row offers one action: **Dispose**. It carries no Ship, Record Payment, Edit, or Cancel Order button.

- **Record Payment** is absent because the buyer paid on WooCommerce before the order ever reached you.
- **Edit** and **Cancel Order** are absent because the order belongs to WooCommerce. Change it there.
- **Ship** is absent because the POS does not buy web labels. See below.

### What Dispose does

When the order carries loose stock (ammunition, optics, parts), the dispose dialog first asks **Where did these come from?**, with a location box for each line that sits in a store location: "Pick the location each was taken from. Leave blank if you are not sure — it goes on the Not placed list." See [storage locations](../inventory/storage-locations.md#the-not-placed-tab).

1. Books one FFL Transfer disposition per firearm serial into the [bound book](../compliance/bound-book.md).
2. Issues the stock, so the gun leaves inventory.
3. Queues the push to [FastBound](../compliance/fastbound.md).
4. Books the revenue as a Sales Invoice mirroring the WooCommerce amounts. That invoice does not move stock again, because the disposition already did.
5. Retires the listing on the store the order came from.
6. Takes each loose-stock line out of the location you picked.

## Who buys the shipping label

Your own ShipStation plugin on WooCommerce does, not the POS. A web order arrives in ShipStation directly from the store, with the store's own address and item data. That is why the POS never offers **Ship** on a web row, and why pushing one from here would create a second shipment for the same parcel.

Counter orders are the opposite case. The POS pushes those, because nothing else knows about them.

## In-store pickup

When the buyer picks your own shop from the FFL list at checkout, they are collecting at the counter. There is no far end, so Dispose is refused: booking a transfer to yourself would put an entry in the bound book that never happened. Your own license number is set in [FFL Settings](../settings/ffl-settings.md).

The card shows an **In-store pickup** badge and a **Pickup** button instead.

1. Click **Pickup**. For a firearm the POS opens a draft counter order, already paid, and takes you straight to it.
2. Run the [Form 4473 and NICS](../compliance/4473.md) on that order the ordinary way, then complete it at the register. The ticket arrives with the WooCommerce payment already on it for the full amount, and the payment screen shows it paid. Do not take money again. The buyer already paid on the store. Anything else the customer buys goes on a ticket of its own; this one completes only while it totals what WooCommerce charged.
3. The web order stays on the queue until that counter order is submitted, so an abandoned draft is visible rather than lost. Meanwhile the card offers **Open counter order**, which returns you to the same draft.

An order with no regulated firearm has no 4473 to run. Pickup closes it on the spot, issuing the stock, retiring the listing and booking the revenue.

Pickup is refused while the order is unpaid on WooCommerce, while it carries a WooCommerce refund, or while a distributor leg on it is still outstanding or a gun being procured to the store has not arrived.

## Troubleshooting

| Message you see | What it means | Fix |
|---|---|---|
| Awaiting serial from WooCommerce | No serial has been entered for a firearm line. | Enter it on the WooCommerce Edit Order screen. |
| No Active unreserved serial available | The order sold a gun you do not have free stock for. | Check stock, then correct the order on WooCommerce. |
| Verify the FFL first | The buyer's dealer license is expired or was never resolved. | Verify the license, then dispose. |
| has not been paid on WooCommerce | You tried to ring a pickup for an unpaid order. | Take payment on WooCommerce first. |
| carries a WooCommerce refund | The order was partly refunded. | Settle the refund on WooCommerce before ringing it. |
| still has an outstanding distributor order | Part of the order has not shipped from the distributor. | Wait for the distributor leg to settle. |
| is not an in-store pickup | Pickup was used on an order that ships to another dealer. | Use **Dispose** instead. |
| still has firearm unit(s) awaiting a serial from WooCommerce | Some guns on the order have no serial yet. | Enter every serial on the WooCommerce order first. |
| Held for a web order | The register tried to sell units a paid web order is waiting for. | Ship them to the web customer, or ask a manager to ring the sale. |
