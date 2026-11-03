# Consignment

For stock staff and managers, when someone leaves a gun with the store to sell for them.

The owner, the **consignor**, leaves the gun with the store. The store sells it, keeps a commission and owes the consignor the rest. You take the gun in on [Receive Goods](receive-goods.md), the register sells it like any other gun, and the bookkeeper pays the consignor.

## How it works

1. The gun is received with an **Acquisition Type** of `Consignment` from a private seller. It enters stock at $0, because the store did not buy it.
2. It sells at the register, on the web store or on GunBroker, like any other gun.
3. At the sale, the system records the consignor's share as money the store owes them. The store's profit on the sale is the commission.
4. The bookkeeper pays the consignor out of that balance. See [For the bookkeeper](../admin/bookkeeper.md).

The commission is the store's cut of the price before tax. On a $1,000 gun at 20%, the store keeps $200 and owes the consignor $800. Sales tax is charged on the full price and is not part of the split.

## Before you start

- You need the **Stock Manager** or **System Manager** role.
- The consignor must be a customer record. Receive Goods records them as a private seller, so have their name, address and photo ID. See [Buy from a private seller](private-seller.md).
- The **Acquisition Type** list comes from your FastBound account and must include `Consignment`. If it is missing, ask your administrator to click **Sync FastBound Lists** on FastBound Settings. Without FastBound there is no type to pick, and this page cannot take a gun in on consignment.
- Agree the price and the commission with the consignor. The store default is 20%.

## Take a gun in

1. Open **Receive Goods**.
2. Set **Acquisition Source** to **Individual**. With **FFL Dealer** selected, the receive fails when you submit.
3. Fill in the seller block: **Seller (Customer)**, the name fields, **Seller ID Type**, **Seller ID Number** and the address.
4. Pick a **Source**. The `Consignment` entry there is only a reporting label.
5. Set **Acquisition Type** to `Consignment`. A **Consignment Commission %** box appears with the store default.
6. Scan the gun and fill in its card. Leave **Cost** at **0**, and enter the agreed **Sell Price**, the serial and the firearm details.
7. Change the card's **Consignment Commission %** if this gun's rate differs.
8. Click **Add to Receipt**, then **Submit & Receive**. No payment dialog opens: the consignor is paid after the sale.

!!! warning "Do not enter a cost"
    A cost above $0 is booked as what the store paid for the gun. Leave it at zero. The consignor's share is worked out from the sale price.

!!! warning "Federal recordkeeping"
    The receive writes the usual bound-book acquisition, with the consignor as the seller, and sends it to FastBound. The consignor's ID and address are recorded as for any private-party acquisition.

Each gun's serial record then shows:

| Field | What it holds |
|---|---|
| **Consignment** | Ticked: the gun is someone else's. |
| **Consignment Commission %** | The store's cut, from the card or the store default. |
| **Consignor** | A supplier record named after the consignor, made on first use and reused after. |

The gun adds nothing to stock value. On Inventory Receipts its type is `Consignment`.

## Sell the gun

Nothing changes at the counter. Pick the gun's serial and ring it up as usual. A consigned gun lists on the web store and [GunBroker](list-online.md) like any other once its **Consignor** is set, and an online sale owes the consignor the same share.

- The commission is frozen on the sale, so a later change to the gun cannot change an old sale.
- A rate of zero or over 100 is refused with a message naming the gun. A rate of 100% is allowed and owes the consignor nothing.
- Two guns with different rates cannot share one line. Put each gun on its own line.

## Change the commission

A sale uses the most specific rate that is set.

| Where | Who | Changes |
|---|---|---|
| **Default Consignment Commission %** on [FFL Settings](../admin/ffl-settings.md) | System Manager | Guns received from now on |
| **Consignment Commission %** on Receive Goods, at the top or on a card | Stock Manager | The gun as it is received |
| **Consignment Commission %** on the gun's serial record | Stock Manager, Item Manager | That gun, until it sells |
| **Consignment Commission %** on the item row of an unsubmitted invoice | Accounts User | That one sale only |

A blank or zero rate on a sale means "use the gun's rate". A blank or zero rate on a gun fills in the store default when saved. The register has no commission box, so change the gun's rate before you ring it up.

## Find what is on consignment

- **Dashboard**: the **Firearms in stock** tile adds "N on consignment" under the count.
- **Serial numbers** (sidebar, **Inventory**): filter on **Consignment** ticked and Status `Active`. The record shows the **Consignor** and rate.
- **Inventory Receipts**: set **Type** to `Consignment` to see what came in over a period.
- **Sales Report**: a sold consigned gun has a teal triangle on its Profit cell, and the **Tax lines** view has a **Consignment?** column. See [Sales Report](../manager/sales-report.md).

## Give a gun back unsold

There is no **Return to consignor** button. A gun going back to its owner leaves stock and the bound book like any gun leaving the store, and the system does neither step for you. Book it out the way your compliance process requires, then check Firearms In Stock against FastBound.

If a gun was received on consignment by mistake and has not left the store, a manager can cancel the receipt. The gun goes back to how it was, and the acquisition is cancelled.

## Undo a consigned sale

- A consigned sale cannot be returned. A return that holds a consigned gun is refused on the first save.
- To undo the sale, a manager cancels the original invoice. The amount owed to the consignor goes with it.
- Ask the bookkeeper first whether the consignor was already paid for that sale. Cancelling does not claw back a payment. See [For the bookkeeper](../admin/bookkeeper.md).

## Fix a gun missed at receive

A gun received as a normal purchase can become a consignment gun later.

1. Open the gun's serial record.
2. Tick **Consignment**.
3. Pick the **Consignor**. Saving without one is refused.
4. Save. A blank **Consignment Commission %** fills in the store default.

This changes who the sale pays. It does not change the bound book or the gun's cost.

## Limits

- Only Receive Goods takes guns in on consignment. The trade-in dialog has no consignment option.
- Only the **Acquisition Type** makes a gun consigned. A `Consignment` **Source** with another type does not.
- The consignor is matched by name. Two people with the same name share one balance, and a consignor named like an existing supplier uses that supplier.
- **Correct Cost** is not available on a consigned gun.
- A consigned gun and a store-owned gun on one line book the consignor correctly, but the Sales Report overstates that line's cost. Sell them on separate lines.
- Taking a sold consigned gun back in, for example as a trade-in, clears the old consignor. The new receipt decides who owns it.
- There is no consignor statement, payout button or reminder.

## Troubleshooting

| You see | Fix |
|---|---|
| `Consignor name required to book a consignment payable.` | Receive again with **Acquisition Source** set to **Individual** and the seller block filled in. |
| `Select a Consignor for a consignment firearm — the sale proceeds (net of commission) are owed to them.` | Pick the **Consignor** on the gun. |
| `Consignment commission % for … must be greater than 0 and at most 100 …` | Set a rate on the gun or on the invoice line. |
| `The guns on line … carry different consignment commission rates …` | Put each gun on its own line. |
| `Consignment sales cannot be returned (invoice …, gun …). Cancel the original sale instead.` | Ask a manager to cancel the original sale. |
