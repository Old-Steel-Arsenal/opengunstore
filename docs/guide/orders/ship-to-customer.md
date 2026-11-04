# Ship accessories to a customer

For counter staff, when ammo, optics, parts or other non-firearm goods are mailed to a customer's address.

No license is involved, so there is no destination FFL. What matters is the ship-to: it decides the sales tax.

## Before you start

- A firearm cannot go to a home address. For a gun, see [Ship a firearm to another dealer](ship-to-dealer.md).
- Gun parts are not firearms. An upper, a barrel or a conversion kit ships like any accessory.
- The screen itself is explained on [Build an order and its splits](build-an-order.md).

## Steps

1. Open **Create order** under **Sales** in the sidebar.
2. Click **Individual**.
3. Pick the **Customer** and check the **Receipt Email**.
4. Under **Recipient & Ship-To**, type the recipient's name, phone, street address, city, state and ZIP.
5. Add each item in **Add Item**.
6. For loose stock from your shelf, pick the location under the line's name, or leave **Taken from: not said**.
7. If a line came from a distributor, click **Plan fulfillment** and **Apply plan**.
8. Type the carrier charge in **Shipping**.
9. Check the tax preview and the **Total**.
10. Take the payment under **Payment**.
11. Click **Create Order**.

The order waits on [Pending Orders](pending-orders.md) until you ship it. See [Ship or hand over an order](ship-or-hand-over.md). A line you took from a shelf location leaves that location when the order ships.

## Sales tax follows the ship-to

Sales tax follows where the goods are handed over, not where the customer lives. The system decides from the ship-to on its own. "Home state" means the state on your store's own address.

| Situation | Tax |
|---|---|
| Picked up in store | Normal rate |
| Ship-to inside your home state | Normal rate |
| Ship-to in another US state | Zero, with the **Out of State Sales** template |
| No ship-to on the order | Normal rate |

- A visitor from another state who buys over the counter pays your home-state tax. That is correct; do not change it.
- The state can be a code or spelled out (`TX` or `Texas`), in any case.
- Shipments outside the United States are taxed normally. An exception is a question for your accountant.
- A register sale can never be made tax-free this way. The register refuses **Out of State Sales** without an out-of-state shipping address and asks you to switch the template back.

The tax preview reads "Out-of-state — no sales tax" for another state, and "Tax exempt — no sales tax" when an exemption is on. An item with its own tax rate is previewed at that rate.

### Tax on shipping

Shipping follows the goods. When the goods are taxed, so is the shipping, unless your home state does not tax shipping.

- With **Shipping Price Includes Sales Tax** on in [FFL Settings](../admin/ffl-settings.md) (the default), the **Shipping** amount is what the customer pays, tax inside. The preview reads, for example, "Shipping $45.00 (sales tax included)".
- With it off, tax is added on top, shown as "(+$… tax)". The setting applies to new orders only.
- In California, shipping charged at cost is not taxed. The **Shipping** box says to put any mark-up or handling on its own taxable line.

### Peace-officer excise exemption (California)

California charges an 11% excise tax on firearms, firearm precursor parts and ammunition. It does not apply to an active or retired peace officer, or the law enforcement agency that employs one. Sales tax still does.

In a California store whose [FFL Settings](../admin/ffl-settings.md) name the excise tax account, the page shows **Peace officer / law enforcement agency — excise tax exempt** under the buyer, for both buyer types. Stores in other states do not see it.

1. Pick the buyer.
2. Tick the box. The tax line drops the excise and reads "(excise exempt)".
3. Pick the **Exempt buyer**: **Active Peace Officer**, **Retired Peace Officer** or **Law Enforcement Agency**.
4. Click **Upload CDTFA-230-FET** and attach the buyer's certificate (PDF, PNG or JPG).

Until both are in, the bottom bar lists **Exempt buyer** and **CDTFA-230-FET certificate**. An officer gives a certificate for every sale. An agency may give one that covers its purchases.

The certificate is kept on that customer's record and does not count for another customer, so changing the buyer asks for a new one. The exemption belongs to this order only. Editing the order keeps it.

This is not the **Tax exempt** box for resale buyers (see [Ship a firearm to another dealer](ship-to-dealer.md#tax-exempt)). An individual service member is not exempt from either tax.

## What happens next

- The receipt is emailed to **Receipt Email**.
- The order appears on [Pending Orders](pending-orders.md), ready to ship.
- With ShipStation, the shipment is sent there so you can buy the label.

## Troubleshooting

| You see | Fix |
|---|---|
| … can't be shipped to … (state restriction) | The item, or the distributor you chose, will not ship there. Remove the line, or plan it from another distributor |
| Shipping Tax Setting Changed | A manager changed the shipping-tax setting while the page was open. Reload the page, then create the order |
| Individual orders must be paid IN FULL before shipping | Collect the balance, or pick **Create only — dispose later** |
