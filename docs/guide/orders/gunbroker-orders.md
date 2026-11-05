# GunBroker orders

For counter and operations staff, when a gun or accessory sold on GunBroker has to be paid, booked out and shipped. Listing on GunBroker is on [List a gun online](../stock/list-online.md).

## How a GunBroker order arrives

Every fifteen minutes the POS reads new and changed orders from GunBroker and makes a **GunBroker Order** for each new one. The notifications bell rings: "New GunBroker order #…" with the buyer and the total. An order more than a week old, such as one read back later, does not ring.

The order appears on [Pending Orders](pending-orders.md) with the **GunBroker** tag. Open the record itself from **GunBroker orders** under **Online** in the sidebar.

Almost every field on the record is copied from GunBroker and read-only: the order number and status, the buyer, the ship-to address, the dealer's license and its eZ Check result, the amounts, the payment and the tracking. **Status** is the store's own state: Pending Fulfillment, Transferred (the bound book exit is booked) or Cancelled.

The buyer is matched to an existing customer by GunBroker account, then by email. A customer flagged as an FFL dealer is never reused for a GunBroker purchase. See [Customers](../counter/customers.md).

## Work the order

1. **Wait for payment.** The buyer sends a certified check or money order. No card gateway is connected, so card payment is refused.
2. **Bank it and wait for the funds to clear.** Do not ship on receipt of a check. Forged certified checks are the common fraud against GunBroker sellers.
3. Click Record Payment and record the payment. See [Record a GunBroker payment](ship-or-hand-over.md#record-a-gunbroker-payment).
4. **Wait for the receiving dealer's license.** The system verifies it through eZ Check. Dispose stays disabled with "Verify the FFL first." while the license is expired or unverified.
5. Click Ship to send the order to ShipStation, and buy the label there. This works before or after Dispose once payment is in, the license is verified and every unit has a gun. A Type 03 collector order must be disposed first.
6. Click Dispose on the day you pack the gun. See [Book the guns out](ship-or-hand-over.md#book-the-guns-out-dispose). A collector buyer gets the [C&R block](web-orders.md#a-collector-cr-type-03-buyer).
7. **If Dispose says the invoice did not post**, click **Create Invoice** on the card. **Ship** comes back once it posts.

What happens at Dispose:

- The invoice is raised, the gun is in the bound book as a transfer, and the record goes to [FastBound](../admin/fastbound.md).
- The goods leave stock.
- Tracking goes back to GunBroker once the gun is disposed and the label is bought.

GunBroker's own payment flag is a click on their website. It is recorded, but it does not open Dispose. **Record Payment** records what reached the bank.

GunBroker cards have no **Edit** and no **Cancel Order**, because a GunBroker sale cannot be undone from the seller's side. Cancellations and refunds start on GunBroker. See [Returns and cancellations](#returns-and-cancellations).

## Antiques and accessories

An order made entirely of pre-1899 antiques, with no dealer license on it, skips step 4. It ships to the buyer's own address, and Dispose takes the gun out of stock without writing the bound book or FastBound. Mix an antique with a regulated gun, or let the buyer give a license number, and the whole order ships to the receiving FFL.

An ammunition or accessory order has no serials, and Dispose takes the goods out of stock without touching the bound book. Unless the order also has a gun or a license number, it ships to the buyer, and the dialog's **Ships to** line reads "the buyer directly — nothing here needs an FFL". It still asks where the goods came from.

## A gun sold on GunBroker is scanned at the counter

When somebody scans a gun at the counter, the POS asks GunBroker once whether it already has a buyer.

- **GunBroker says it is sold.** The counter cannot sell it. The GunBroker buyer paid first. Explain that the gun sold online and offer something else.
- **GunBroker does not answer in time.** The counter sells as normal. If a real double sale results, treat it as a refund case with your accountant.

Between two fifteen-minute reads, a gun can be sold on GunBroker and still look available on the shelf. That gap is what this counter check covers.

## Returns and cancellations

Start on GunBroker every time and let the POS follow what happens there. The POS has no button that refunds a GunBroker order. GunBroker collects and files the sales tax on these orders, so only their process moves the state filing with the refund.

When the POS sees a cancellation, refund or return on GunBroker, it raises an alert and changes no accounts.

- **The gun has not shipped.** Release the reservation. If money was taken, the bookkeeper makes the correction (see [Month-end close](../admin/month-end.md)). Then fill in **Refund Handled At** on the GunBroker Order, or the order keeps appearing in the morning alert.
- The gun shipped and comes back whole. It must be acquired back into the bound book. The gun is in your safe while the book says somebody else has it, which is a federal recordkeeping problem. See [Receive goods](../stock/receive-goods.md).

## Daily alerts

A daily audit emails one message per kind of problem. The last two rows come from an hourly listing check. Each one is money or a gun going wrong.

| Alert | What to do |
|---|---|
| Disposed but not invoiced | The gun left and revenue was not recorded. Operations: **Create Invoice** on the card. |
| Paid a long time and not disposed | Operations: waiting for an FFL, or forgotten. |
| Cancelled, refunded or returned on GunBroker while the invoice stands | Operations and accounting: see [Returns and cancellations](#returns-and-cancellations). |
| Chargeback | Accounting: a payment was reversed. |
| One order failing to sync repeatedly | The administrator: it will not sync again on its own. |
| A listing ended outside the POS | Operations: confirm why. |
| Two live listings for the same gun | End the extra one at once. It is a double-sale risk. |
| A cancelled invoice still carrying fee accrual | Accounting: the automatic reversal failed. |

## Do not do these

- Do not change a price on the GunBroker site. The next update from the POS overwrites it.
- Do not create a listing by hand on the GunBroker site. The POS will not recognize it, stock will not match, and it can still be bought.
- Do not press End again because GunBroker's in-progress list still shows a finished listing. That list catches up slowly. Judge by the single item's own status and wait a few minutes.
- Do not relist because the item number changed. GunBroker gives a listing a new number every thirty days when it renews it, and the POS adopts the new number by itself.

## Troubleshooting

| You see | Fix |
|---|---|
| Verify the FFL first. | Wait for or verify the receiving dealer's license. See [Verify an FFL](../compliance/ffl-verification.md). |
| Awaiting payment | Bank the payment, then **Record Payment**. |
| Dispose says the invoice did not post | Click **Create Invoice** on the card. See [Troubleshooting](../help/troubleshooting.md). |
| The counter refuses a gun GunBroker reports as sold | The online buyer paid first. Offer the customer something else. |
| The order keeps appearing in the morning alert | Fill in **Refund Handled At** once the refund is settled. |
