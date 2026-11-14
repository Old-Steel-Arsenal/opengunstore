# Hold a gun for a deal

For sales staff, when a customer on a CRM deal wants a gun kept for them for a couple of days.

A hold keeps one gun for one deal for 48 hours. While it lasts, only the deal's customer can buy that gun at the register or on Create Order, and the website shows it out of stock. A hold takes no money. To take a deposit, [build the order](build-an-order.md) instead.

## Before you start

- A manager turns on **Hold guns for deals** in [Lead Mail Settings](../manager/settings.md#lead-mail-settings). It needs **Link deals to POS orders** on too. Turned off, it stops new holds; holds already placed run to their end.
- The gun is on one of the deal's product lines with its serial number.
- Only a gun priced one by one can be held. A model sold as one product for all its guns cannot.

## Steps

1. Open the deal. Open the three-dot menu at the top and click **Hold gun**.
2. If the deal has no customer yet, the **Who is the buyer?** box opens first. Pick the customer, or click **Create customer**.
3. The **Hold guns** box lists each gun on the deal. A gun that can be held is ticked. A gun that cannot shows why under its name.
4. Click **Hold for 48 hours**.

The customer card on the deal shows **Held:** with the serial number and the time the hold ends. The deal's timeline gets a note.

## Extend or release a hold

Open **Hold gun** again. A held gun shows **Held until** and the time, with an **Extend** and a **Release** button.

| Who | Extend | Release |
|---|---|---|
| The person who placed the hold | Once, without a manager | Yes |
| The deal owner | With a manager's PIN | Yes |
| Anyone else | With a manager's PIN | With a manager's PIN |

Each **Extend** adds 48 hours. A manager signed in does both without a PIN. Holding the same gun again for the same deal after its hold ran out also takes a manager.

## At the counter

A gun held for someone else cannot be sold to another customer. The register and Create Order open **Gun held for a deal**, with the customer, the deal and the time the hold ends.

1. Click **Cancel** to leave the gun alone, or **Release and sell** to sell it anyway.
2. For **Release and sell**, pick the manager and type their PIN, and pick a reason. A manager signed in only picks the reason.
3. Click **Release**. The sale goes on.

The deal owner gets a task saying who released the hold and why. Selling the gun to the deal's own customer needs none of this, and ends the hold.

## What happens next

- While a gun is held, the website shows it out of stock. Its page stays up.
- When the hold ends, the gun is back on sale everywhere within a few minutes.
- A hold ends by itself when its time runs out, when the deal is **Won** or **Lost**, when the gun comes off the deal or the deal is deleted, and when the gun is sold to the deal's customer.
- A gun on sale on GunBroker cannot be held. End the listing first.

## If something goes wrong

| What you see | What to do |
|---|---|
| **Hold gun** is not in the menu | Ask a manager to turn on **Hold guns for deals** |
| A gun shows **Held for another deal until** | Ask whoever holds it, or wait for the time shown |
| **is listed on GunBroker** | End the GunBroker listing, then hold the gun |
| **was held for this deal before** | Ask a manager to hold it again |
| The deal owner got a task saying the hold ended | A web order or another sale took the gun first. Tell the customer |
