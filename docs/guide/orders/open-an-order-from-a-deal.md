# Open an order from a CRM deal

For sales staff, when a deal in CRM is ready to become a sale and the store has turned on **Link deals to POS orders**.

A deal never sells anything by itself. **Open order** fills in the [Create Order](build-an-order.md) page, and you finish the sale there with its usual checks: the buyer's status, the serial numbers, the FFL, the 4473, the bound book.

## Before you start

- A manager turns the link on in [Lead Mail Settings](../manager/settings.md#lead-mail-settings). While it is off, a deal shows no **Open order** button and no **Customer** section.
- You see only the deals your CRM access lets you see.

## Check the customer

The **Customer** section on the deal's side panel shows who the deal is with:

- Banned status and any staff alerts, in color at the top
- FFL status for a dealer
- **Lifetime spend**: what they have bought, by the same figure as Net Sales in the [Sales Report](../manager/sales-report.md)
- **Open orders**: their orders on Pending Order, with what they still owe
- **Last orders**: their most recent sales and returns

It never shows an ID number, a date of birth, a 4473 or a background check. Before the deal has a customer, it lists the customers whose email or phone match.

## Steps

1. Open the deal and click **Open order** at the top.
2. If the deal has no customer yet, the **Who is the buyer?** box lists the matches. Click the **Use** button with the right name, or click **Create customer** to make a new one from the deal's contact. A new customer needs an email address.
3. The **Create Order** page opens with the buyer and the deal's items and serial numbers filled in.
4. Check the prices. They are the prices the page charges today, not the deal's discounts. Change one the way you would on any order.
5. A gun with no serial number on the deal waits for you: its serial picker opens, and you pick one. A gun that has sold since is taken off, and the page says why.
6. Finish the order and click **Create Order**.

## What happens next

The first invoice made this way marks the deal **Won** and sets its closed date. Won means invoiced. It does not mean paid or handed over.

The deal's value follows the sale: the item prices and transfer fees of all its orders, returns taken off. A trade-in does not reduce it. Sales tax and shipping are not in it. While the deal has orders, the value cannot be typed over.

| What happens to the order | The deal's value | The deal's status | A task for the owner |
|---|---|---|---|
| A further order on the same deal | Goes up | Stays | No |
| [Edit Order](change-an-order.md) | Follows the edit | Stays | No |
| A return | Goes down | Stays | Yes |
| Cancel the order | Goes down | Stays | Yes |

A return or a cancel never reopens the deal. The owner decides what comes next, and if the deal has no owner the task stays unassigned on the deal.

## If something goes wrong

| What you see | What to do |
|---|---|
| **Banned customer** on the Create Order page | Do not serve them. A manager can override the ban from that box |
| **This contact has no email address** or **Enter the buyer's email** | Add an email to the deal's contact, then click **Open order** again |
| The deal stayed open after the order | The order is fine. Ask your administrator to check the Error Log, and mark the deal **Won** by hand |

A sale rung at the register does not reach the deal. Mark that deal **Won** by hand.
