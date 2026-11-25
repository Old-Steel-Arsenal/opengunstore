# Open an order from a CRM deal

For sales staff, when a deal in CRM is ready to become a sale and the store has turned on **Link deals to POS orders**.

A deal never sells anything by itself. **Open order** fills in the [Create Order](build-an-order.md) page, and you finish the sale there with its usual checks: the buyer's status, the serial numbers, the FFL, the 4473, the bound book.

## Before you start

- A manager turns the link on in [Lead Mail Settings](../manager/settings.md#lead-mail-settings). While it is off, a deal shows no **Open order** button and no **Customer** section, and a deal or lead shows no **Matches** section.
- You see only the deals your CRM access lets you see.
- A deal's page is titled with the person's name. To find a deal, type the name in the **Full Name** filter at the top of the Deals list.

## Check the customer

The **Customer** section on the deal's side panel shows who the deal is with:

- Banned status and any staff alerts, in color at the top
- FFL status for a dealer
- **Lifetime spend**: what they have bought, by the same figure as Net Sales in the [Sales Report](../manager/sales-report.md)
- **Open orders**: their orders on Pending Order, with what they still owe, and their web orders not yet shipped or picked up, with the web order's status
- **Last orders**: their most recent sales and returns

It never shows an ID number, a date of birth, a 4473 or a background check. Before the deal has a customer, it lists the customers whose email or phone match.

## See what the store has for them

The **Matches** section on the side panel of a deal or a lead lists what is in stock for this customer:

- The items on the record. A question sent from a product page on the website puts that gun there.
- What fits the customer's open wants
- What fits what they said they are looking for, when the store keeps customer memory. With a budget on file, the nearest prices come first.

Each line shows the price the register charges now and how many can be sold now. A gun priced one by one shows its lowest price. An item on the record with none left shows **Out of stock**; a gun priced one by one that has sold is no longer listed. The list is worked out each time the page opens. With nothing to show, the section is not there.

To keep one of those guns for the customer, see [Hold a gun for a deal](hold-a-gun-for-a-deal.md).

## Steps

1. Open the deal and click **Open order** at the top.
2. If the deal has no customer yet, the **Who is the buyer?** box lists the matches. A contact that belongs to several customers is never picked for you; they are all in the list. Click the **Use** button with the right name, or click **Create customer** to make a new one from the deal's contact. A new customer needs an email address.
3. The **Create Order** page opens with the buyer and the deal's items and serial numbers filled in.
4. Check the prices. They are the prices the page charges today (a gun's own price, any other item's selling price), not the deal's discounts. An item with no price opens at 0. Change a price the way you would on any order.
5. A gun with no serial number on the deal waits for you: its serial picker opens, and you pick one. A gun that has sold since is taken off, and the page says why.
6. Finish the order and click **Create Order**. If you change the buyer first, the order is made for the new buyer and is not linked to the deal.

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
| The deal stayed open after the order | The order is fine. The deal is brought up to date a moment later by itself. If it still has not changed, ask your administrator to check the Error Log, and mark the deal **Won** by hand. A return or cancel that failed this way gives no task to the owner |

A sale rung at the register does not reach the deal. Mark that deal **Won** by hand.

## Want list

When the customer is looking for something the store does not have, record it on the deal.

1. Open the deal and click **Add want** at the top.
2. Type a few **Keywords**, such as `Mosin 91/30`, pick an **Item (optional)**, or both. Add a **Budget** and **Notes** if they help.
3. Click **Add want**.

When stock that fits arrives, through a receipt or a trade-in, whoever added the want gets a CRM task. Keywords match without case, spaces or punctuation, so `P228` also finds `P-228`. Nothing is emailed to the customer, and a customer's transfer gun never counts.

To close a want that is no longer needed, open it in the **CRM Want** list (search for it with ++ctrl+k++) and set **Status** to **Closed**.

### Wants from what the customer said

When your manager turns on **Add wants from customer memory**, a gun or product the customer asks for in a message becomes a want by itself, if nothing in stock fits it. The want carries the budget the customer gave, and its **Customer message** field links the message it came from. Nothing is made from a line staff wrote in the memory.

While the want is **Open**, it follows the customer's memory: a changed line changes the want, and a line the assistant takes out closes it. A want you edited, closed or that has matched stays as it is.

A lead has no contact yet, so its wants appear when you convert it to a deal. The task for a matching arrival goes to the deal's owner. A deal nobody owns sends it to every Sales Manager, until someone answers the customer and becomes the owner.

## Web orders on the deal

While the link is on, a customer's web orders leave notes on their open deal, on the deal's **Notes** tab: placed, shipped or picked up, cancelled, and refunded, each with its amount. A customer with no open deal and no open lead gets no note and no new deal.

A paid web order closes the sale it came from, matched by the order's email:

| The customer has | What happens |
|---|---|
| An open deal, with no order opened from it and no gun held | The deal is set to **Won**. Its value is the order total. |
| An open deal with an order opened from it, or a gun held | Only the note. |
| No open deal, but an open lead | The lead becomes a deal with the same owner and mail. The deal is set to **Won**, its value is the order total, and it is linked to the order's customer. |
| A lead that is lost or in Other mail, or nothing open | Nothing changes. |

Only a deal or lead made before the order counts. If several leads match, only the newest one becomes a deal. Nobody gets an email or a task.

The deal shows the order in **Won by web order**. If the order is cancelled or fully refunded, the deal goes back to the open status it had and its value is cleared. A partial refund only adds a note.

## When a web order's payment fails

While the link is on, a web order whose payment failed, or that was cancelled without ever being paid, goes on the customer's open deal. A customer with no open deal gets one.

The deal's **Notes** tab gets a note with the amount, the items, and the link where the customer can pay the order. The deal gets one task, "Payment failed on web order", to contact the customer. If the deal has an owner, the task is theirs.

A second failed payment adds to the same task. Nothing is emailed to the customer.

The task is marked **Done**, with a note on the deal, when the customer's next web order is paid or an invoice is made from the deal.
