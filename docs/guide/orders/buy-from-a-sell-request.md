# Buy a gun from a sell request

For sales staff, when someone offers the store a gun on the website's sell-your-gun form.

Each request is its own CRM deal, titled **Sell:** with the gun and the person's name, apart from anything the person is buying. Find them all in the **Sell requests** view of the Deals list. The deal's **Gun offered** section shows the gun, its photos and where the buy stands.

Nothing on the deal books the gun. It enters inventory only when it is at the store and you take it in through the register's trade-in.

## Before you start

- A manager turns on **Link deals to POS orders** in [Lead Mail Settings](../manager/settings.md#lead-mail-settings).
- Receiving the gun needs the same role as a [trade-in](../counter/trade-ins.md): **Sales User**, **Stock Manager** or **System Manager**.

## Make an offer

1. Open the deal. Click **Make offer**.
2. Type the amount and click **Save offer**. The offer shows in **Gun offered**.
3. Contact the seller the way they prefer. Nothing is emailed for you.
4. When they accept, click **Seller accepted**. The deal shows the day and that it waits for the gun.

The offer is what the store pays. It is not counted as a sale anywhere.

## Receive the gun

Do this only with the gun in your hands, at the store. A walk-in with no offer yet works the same way.

1. Open the deal and click **Receive gun**.
2. If the deal has no customer yet, **Who is the seller?** opens. Pick the customer, or click **Create customer**.
3. The register opens with the **Trade-in (Individual)** dialog filled from the deal: the seller, the gun's details and the offer as the cost. **Apply as credit to this sale** starts unticked, so the store pays the seller now. **Condition Grade** starts empty.
4. Inspect the gun and pick its **Condition Grade**. Check every other field against the gun and the seller's ID, and fill in the rest as for any [trade-in](../counter/trade-ins.md).
5. Click **Submit Trade-in**.

## What happens next

- The gun is booked the way every trade-in is: in stock, in the bound book, and the seller paid.
- A moment later the deal links to the receipt and turns **Won**. A trade-in taken as credit links at once and turns **Won** when that sale is checked out.
- If the seller backs out, set the deal **Lost** as usual.

## Troubleshooting

| You see | Fix |
|---|---|
| A photo shows as a link, not a picture | The POS could not copy it from the website yet and tries again each day. The link opens it if you are signed in to the website. |
| **This deal is a gun offered to the store** | **Open order** and **Hold gun** are for selling. Use **Make offer** and **Receive gun**. |
| **Make an offer first** | Click **Make offer** before **Seller accepted**. |
| **The gun is received already** | The deal is done. Open the receipt from **Gun offered**. |
