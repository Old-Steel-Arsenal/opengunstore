# Ship or hand over an order

For counter staff, when an order on [Pending Orders](pending-orders.md) is ready to move: collect the money, book the guns out, ship the parcel, or hand the goods over at the counter.

Every button below is on the order's card on the **Pending Order** page. A card shows only the buttons its order is allowed, and a greyed-out button has a short line under it saying why.

## Collect a payment (Record Payment)

For a counter order sold on account, or one that still owes a balance. Web orders are paid before they reach the queue. GunBroker orders have their own [Record Payment](#record-a-gunbroker-payment).

1. Click **Record Payment** on the card.
2. Pick the **Payment Method**: Cash, Credit Card, Zelle, ACH, or Check/Money Order.
3. When **Transaction Number / Approval Code** shows, enter the Zelle or ACH confirmation, the check number, or a card's approval code. Never the card number.
4. Check the **Amount**. It starts at the outstanding balance.
5. Set **Payment received on** to the day the money arrived, even if that was an earlier day.
6. Save the payment.

On a store with the card terminal, **Credit Card** runs the terminal for the amount and shows **Card approved — … fully paid.** or **Card approved — … still outstanding.** A declined or cancelled card records nothing. See [Take payment](../counter/payments.md).

If the card was already run on the Virtual Terminal or a standalone terminal, tick **Already charged on Virtual Terminal** and enter the approval code instead.

## Record a GunBroker payment

Recording the money is what opens **Dispose**. GunBroker's own "payment received" flag on their website does not.

1. Click **Record Payment** on the card.
2. Pick the **Payment Method**: Credit Card, Zelle, ACH or Check/Money Order. GunBroker orders are not paid in cash.
3. Enter the check or money-order number, the Zelle or ACH confirmation, or the card approval code.
4. Check the **Amount**.
5. Set **Payment received on** to the day the money went into the bank, not the day the envelope arrived.

A check is booked to the deposit account in GunBroker Settings. Credit Card, Zelle and ACH are booked to the store's own accounts for them, the same as a counter order. The payment entry itself is written when the order is disposed and invoiced.

A manager can tick **Key in card number (no terminal)** to type the buyer's card on the card processor's payment page. The approval is recorded as the order's payment. Card-not-present rates apply. Without the box, take the card on the Virtual Terminal and enter its approval code.

## Book the guns out (Dispose)

**Dispose** writes the gun's exit into the bound book. Do it on the day the gun is packed and leaves.

1. Click **Dispose** on the card.
2. Read the dialog: the customer, the receiving dealer with its license expiry, and every serial or item about to be booked.
3. Compare each serial in the dialog with the gun in your hand.
4. On a web or GunBroker order with ammunition, optics or parts, answer **Where did these come from?** with the storage location of each line. Leave it blank if you are not sure.
5. Tick **I've verified the FFL and serial number(s)**.
6. Confirm.

!!! danger "This writes the bound book"
    Dispose is the entry in your bound book and it goes to FastBound. Read the serials in the dialog against the guns in your hand before you tick the box.

A blank location goes on the Not placed list to confirm later. See [Find and move stock](../stock/find-and-move.md). Counter orders do not ask, because the location was picked on Create Order.

What happens next:

- Each gun is in the bound book as a transfer to the receiving dealer, and leaves stock.
- The record goes to [FastBound](../admin/fastbound.md).
- The card shows **✓ Disposed**, or moves to its next step.

A counter order with no receiving license never gets Dispose: there is no transfer to record. When Dispose is greyed out, the line under it says why:

| Line under the button | What to do |
|---|---|
| Verify the FFL first. | Verify the license. See [Verify an FFL](../compliance/ffl-verification.md). |
| Record payment before shipping. | Collect the balance with **Record Payment**. |
| Awaiting serial from WooCommerce. | Enter the serial on the WooCommerce order. |
| Serial rejected — correct it on the WooCommerce order. | Correct the serial on WooCommerce. |
| Counter pickup — hand it over and Mark Picked Up. | Hand it over and use **Mark Picked Up**. |
| Waiting for ship-to-store goods to arrive. | Wait until the goods are received. |

A collector buyer (C&R, Type 03) on a web or GunBroker order gets an extra block in the dialog. See [Web orders](web-orders.md#a-collector-cr-type-03-buyer).

### The customer has not signed

When an agreement on the order is unsigned, Dispose opens with a red warning, and booking out without the signed acknowledgment is recorded on the order.

1. Type a reason, if you have one.
2. Click **Dispose without signature**.

The order's invoice gets a comment naming you, the unsigned agreements and the reason. Once the customer signs, Dispose works as usual and the signed copy is attached.

## Ship with ShipStation

**Ship** sends the order to ShipStation, where you pick the carrier and buy the label. It does not touch the bound book.

1. Click **Ship** on the card.
2. Check the preview: recipient, the c/o line for the receiving dealer, the address, and the items with their serials.
3. Click **Push to ShipStation**.
4. Buy the label in ShipStation.

Ship is greyed out for an expired FFL, for an unpaid individual's order, and while ship-to-store goods have not arrived. A counter pickup gets no Ship. On a GunBroker row, Ship opens after **Record Payment**, before or after Dispose. Web orders never get Ship: the ShipStation plugin on WooCommerce buys their label.

## Mark an order shipped without ShipStation

For a label bought elsewhere, ShipStation switched off, or a license that expired after Dispose. On a counter pickup the same button reads **Mark Picked Up**, for goods handed across the counter. Counter rows only.

1. Collect any balance first. An individual's order must be paid in full.
2. Click **Mark Shipped (no ShipStation)** or **Mark Picked Up**. When the card has a usable **Ship**, the button is inside Ship's window.
3. Confirm.

The order is stamped shipped, any accessories on it leave stock, and the row clears.

Marked before the guns are booked out, it closes the shipping half only. The bound book is not touched, and the row stays with its **Dispose** button. A pickup that still has a gun to hand over is refused: the gun leaves on its pickup ticket.

## Hand a gun over at the counter (Pickup)

For an order the buyer collects here with a gun on it: a web order placed to your own license, or a counter order built with **Pickup in store**. These cannot be disposed, because a transfer to yourself would record an exit that never happened.

1. Click **Pickup** on the card. A draft register ticket opens for the gun.
2. Run the [Form 4473](../compliance/4473.md) on that ticket.
3. Collect any balance and complete the sale at the register.

The button then reads **Open counter order** and returns you to the same draft. A counter pickup's ticket usually opens by itself when the gun is received during an open register shift. See [Hand over a gun on a pickup ticket](../counter/sell-a-gun.md#hand-over-a-gun-on-a-pickup-ticket).

While the ticket cannot open yet, Pickup stays greyed with the reason, for example "The gun ordered to the store has not arrived yet." A web order with no regulated gun finishes on the spot.

## Place the distributor order

For a counter order with a distributor order still in Draft.

1. Click **Place order** on the card.
2. Read the confirmation. If the order still owes money, it shows how much and that the balance is collected at pickup.
3. Confirm.

An order can be placed on a deposit or before anything is paid; the gun is not handed over until the order is settled. A placed order cannot be cancelled from here. A leg that fails stays in Draft, listed under **Some distributor orders were not placed** with its reason.

## Serve a line from the shelf

A ship-to-store line still on its way can be served from stock. **Fulfill from local stock (_n_)** shows only when something in stock could serve it. Click it, check what the order needs against what the shelf has, pick the serial for a gun, and confirm. The distributor order stays placed, and its parcel becomes store stock when it lands.

## Fix a distributor leg in trouble

A leg the distributor killed, short-shipped or held past its limit shows in red under **Needs attention**. **Re-source** orders the line again from another distributor. **Cancel line** takes the line off the order through Edit.

Neither reaches the distributor. Each shows the order number and reference to quote when you phone them. Once the distributor confirms, cancel the distributor order on its own form. See [Order from a distributor](../stock/distributor-orders.md).

GunBroker cards have no **Edit**, **Cancel Order** or **Mark Shipped**: a GunBroker sale cannot be undone from the seller's side, and tracking goes back to GunBroker by itself.

## Troubleshooting

| You see | Fix |
|---|---|
| …: … firearm(s) still have NO disposition — … | You marked it shipped first. Dispose the guns. |
| … still has a firearm to hand over — complete its pickup ticket (4473) first. | Hand the gun over on its pickup ticket. |
| … still owes … — collect the balance on the order before handing over the gun. | Collect it in the ticket's **Balance due** box, or with **Record Payment**. |
| No other house can ship to the store. | Cancel the line, or wait for the distributor to restock. |
| Terminal not configured | Take the card on the Virtual Terminal, tick **Already charged on Virtual Terminal**, enter the code. |
