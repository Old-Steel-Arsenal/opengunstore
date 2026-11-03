# Change or cancel an order

For counter staff and managers, when an order on Pending Orders needs a change before it ships, or has to be undone.

An order built on **Create Order** can be edited until something is booked against it. After that, the only way back is to cancel it and build a new one.

## Before you start

- **Edit** is offered on counter orders only, while nothing has been booked: no disposition and no ShipStation push.
- Editing cancels the original invoice and creates a replacement. The cancelled one is kept.
- Editing is refused once the order has a booked disposition, was pushed to ShipStation or marked shipped, has been delivered, or handed its gun over on a completed pickup ticket. Use **Cancel Order** instead.

## Edit an order

1. Open [Pending Orders](pending-orders.md) and find the order.
2. Click **Edit**. The order opens on Create Order with an orange "Editing" banner and the invoice number.
3. Make your changes to the buyer, the items, the prices or the note.
4. Read the **Money** summary. It shows what has been paid and what happens on save: a refund for a lower total, a balance to collect for a higher one.
5. If a refund is due, pick a **Refund Method**. For Zelle and ACH type the confirmation number, and for a check the number of the check you write back, in **Refund Reference**.
6. Click **Save Changes**.

**Order Updated** names the replacement invoice, any payment carried over, any refund and any balance due, then returns you to the queue. **Exit without saving** leaves without changes.

The staff order note moves up under **Money**, so you can read and change it. Prices the order already had are kept; only a price you lower in the edit asks for a manager.

### Change the buyer on a paid order

When the order has money on it and you pick a different buyer, a **Payment** box appears under **Money**. It starts blank, and the order cannot be saved until you choose:

| Choice | On save |
|---|---|
| **Refund old buyer** | The full amount goes back to the original buyer. The new order starts unpaid |
| **Carry payment to new buyer** | The money already paid pays the new order. Nothing is refunded |

With **Carry payment to new buyer**, a lower total refunds only the difference to the original buyer. The receipt keeps a note of who really paid.

When the payment cannot move, the second choice is not offered and the box says why. For example, it was taken in a closed period, is matched to the bank, or its card charge was voided or refunded. If the original buyer is not getting the money back, do not save. Ask the bookkeeper to move the payment.

### After the distributor order is placed

Goods a distributor is already sending cannot be recalled, so they ride the edit unchanged.

- Such a line reads "Already ordered on … — quantity and source are fixed; the price can still change." It has no ✕ and no quantity box.
- Its distributor order moves to the new invoice. Nothing is ordered twice.
- A new line, or more of an item than was ordered, is planned and ordered on its own.
- A distributor order still in Draft or Queued is withdrawn and created again from the edited order.

### A gun waiting for pickup

An order whose gun has arrived and is held can still be edited and repriced before the customer pays. The serial stays held, and the open pickup ticket follows the new invoice.

Changing the buyer while a pickup ticket is open is refused. Delete that open pickup ticket first.

## Cancel an order

**Cancel Order** on Pending Orders undoes a counter order in one go. The dialog lists exactly what will happen before you agree.

1. Click **Cancel Order** on the order's card.
2. Read the list of what will be undone.
3. Type the **Cancellation Reason**. It is required.
4. If money was taken, pick a **Refund Method**, and fill in **Refund Reference** for Zelle, ACH or a check.
5. Tick the confirmation box. This arms the red **Cancel Order** button.
6. Click **Cancel Order**.

A counter pickup cannot be cancelled once its gun was handed over on a completed pickup ticket. While its pickup ticket is still open, delete that ticket first.

!!! warning "Cancelled records are kept"
    Cancelling deletes nothing. The invoice, the dispositions and the refund stay on the record, which keeps the reversal auditable.

## What happens next

- Firearms go back into stock and back into the bound book and FastBound, undoing any disposition.
- Any ShipStation shipment is voided, and a refund is recorded for what was paid.
- The order and its invoice are cancelled and kept as the audit trail.

## Troubleshooting

| You see | Fix |
|---|---|
| … was already ordered from … — it can't be removed or reduced here … | Put the line back. Only its price may change |
| … has booked disposition(s), or … was already pushed to ShipStation or marked shipped | The order is past editing. Use **Cancel Order** and build it again |
| The payment on … cannot move to the new buyer: … | Leave the order as it is. Ask the bookkeeper to move the payment |
| … has an open pickup ticket … for the current buyer … | Delete the open pickup ticket, then change the buyer |
| … handed its firearm over on … — a completed counter pickup cannot be cancelled or edited … | The gun already left. Ask a manager about a return |
