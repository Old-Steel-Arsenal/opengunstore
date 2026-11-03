# Deposits and special orders

For counter staff, when a customer orders something the store does not have, pays a deposit or nothing now, and collects it at the counter when it comes in.

The register has no button for an order the store has to source. You take it on **Create Order** as a pickup order, with a distributor line sent to the store.

## Before you start

- The customer must be an individual. A **Business (FFL)** order cannot include a distributor line.
- A gun already on your shelf cannot go on a pickup order. The customer takes it home today, so ring it on the register. See [Sell a gun](../counter/sell-a-gun.md).
- A gun ordered from a distributor to the store is allowed. No destination FFL is asked for. The Form 4473 runs at the register when the gun is handed over.
- An antique firearm needs no 4473, so it can be picked up from stock.

## Take the order

1. Open **Create order** under **Sales** in the sidebar.
2. Click **Individual** and pick the **Customer**.
3. Tick **Pickup in store — the customer collects at the counter**. The address boxes clear and lock, and no shipping is charged.
4. Type the item into **Add Item** and pick it from the distributor results.
5. Click **Plan fulfillment**.
6. Choose **Ship to store** for the line, then click **Apply plan**.
7. Under **Payment**, type the deposit in **Amount**. The note reads "Deposit: … now — … still due." For no money now, tick **Pay later**.
8. Click **Create Order**.

The note under **Pickup in store** reads "Counter pickup — no shipping address needed. Distributor lines can only be ordered to the store." Setting a destination FFL clears the checkbox. Adding a shelf gun turns it off and says so.

## Place the distributor order

The order waits on [Pending Orders](pending-orders.md). Place its distributor leg there with **Place order**. A ship-to-store leg may be placed on a deposit, or before anything is paid, because the goods come to your own shelf.

Each placed leg becomes a distributor order a person confirms. See [Order from a distributor](../stock/distributor-orders.md).

## When the goods arrive

Receive them as usual. See [Receive a distributor delivery](../stock/receive-distributor.md).

- **A firearm.** Its serial is held for this order, and a $0 pickup ticket opens on the register. Run the 4473 there and collect the balance at hand-over, or earlier with **Record Payment**. See [Hand over a gun on a pickup ticket](../counter/sell-a-gun.md#hand-over-a-gun-on-a-pickup-ticket).
- **Anything else.** The order moves to **Ready for pickup**. Collect the balance with **Record Payment**, hand the goods over, then click **Mark Picked Up**.

The customer always pays the balance before taking the goods. The steps on the queue are on [Ship or hand over an order](ship-or-hand-over.md).

## Pay later

**Pay later** books the order on account with nothing collected. Collect with **Record Payment** on Pending Orders, or at the register when a pickup order's gun is handed over.

## What happens next

- The receipt for the deposit is emailed to **Receipt Email**.
- The order shows on Pending Orders until the goods are collected.
- The bell rings when the distributor ships, with the customer's name.

## Troubleshooting

| You see | Fix |
|---|---|
| Not a Counter Pickup | A shelf gun or a destination FFL is on the order. Ring the gun on the register, or clear **Pickup in store** or the FFL |
| … can't order … to the store — … Reopen the fulfillment plan. | The distributor was switched off after you planned. Click **Plan fulfillment** and choose again |
| Distributor ordering … is for individual retail customers only … | Switch to **Individual**, or sell from stock |
| … can't be shipped to … (state restriction) | Plan the line from another distributor, or remove it |
