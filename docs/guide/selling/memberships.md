# Memberships

Sell monthly or yearly memberships, charge renewals to the customer's saved card,
and follow up failed renewals. Nothing changes at the counter until a manager
creates a plan.

## Before you start

- Open the **Memberships** page from **Customers → Memberships** in the sidebar.
- A membership is sold to a named customer, never the walk-in customer.
- Give the customer an email address. Renewal receipts and the yearly renewal
  reminder go by email only, so a customer with no address gets neither.
- Memberships are not rung up on the register. A plan's product is refused there
  with **Sell memberships from the customer's Memberships panel (Customer form),
  not the register.**
- Auto-renew needs **Card on file** switched on in the Payments hub (see
  [Saving a card to charge later](payments.md#saving-a-card-to-charge-later)).
  The card is saved when the first membership payment is charged on the terminal.
  A card saved at the register cannot be used for renewals.

## Creating a plan

A **Sales Manager** or **System Manager** does this.

1. On the Memberships page, click **New plan**.
2. Type the **Plan name**, choose **Billed** (Monthly or Yearly) and enter the
   **Price per period**.
3. Open **More options** for the rest:

| Field | What it does |
|---|---|
| **Registration fee** | Charged once, on the customer's first invoice for this plan |
| **Auto-renew by default** | Pre-ticks **Auto-renew** when the plan is sold |
| **Grace period when a renewal is declined** and **Grace days** | Off: the first declined renewal suspends the membership. On: the card is retried every morning for the grace days first |
| **Membership terms** | An agreement the customer signs on the tablet for each new membership |
| **Description** | Your own note |

4. Click **Create plan**.

Saving a plan makes its product, and a registration fee product when there is a
fee. If the plan is tax-exempt, after saving open its **Billed Item** (on the
Membership Plan record) and set an exempt tax template. Click a plan's card to change it. Untick **On sale** to stop new
sign-ups. Changing the **Price per period** changes what every current member is
charged at their next renewal. Current members stay as they are. **Billed** cannot be changed once the
plan has members.

## Selling a membership

Open the customer, find **Memberships** under **Agreements**, and click **Sell
membership**. From the Memberships page, click **Sell membership** at the top and
pick the customer too.

1. Choose the **Plan**. **Auto-renew** starts as the plan's default. Leave it
   ticked to charge the card on the terminal and keep it for renewals. Untick it
   to take cash or another payment method.
2. Click **Continue**.
3. If the plan has terms, the **Request agreement** dialog opens: send it to the
   tablet, show the QR code or email the link. **Get terms signed** on the row
   reopens it.
4. Click **Collect payment**. With auto-renew, the terminal charges the card.
   The customer first signs the card-on-file authorization if they have not.
   Without it, pick the **Payment Method** (Cash, Credit Card, Zelle, ACH or
   Check/Money Order) and click **Take payment**.

The membership turns **Active** when its invoice is paid in full. If the store
cannot save cards right now, the system asks **Take this payment without
auto-renew?**. A membership paid without a saved card has auto-renew switched
off.

To abandon a sign-up nobody paid for, click **Drop** and give a reason. It is
refused once a payment or card charge touched the invoice.

## The Memberships panel

Each membership on the customer shows its status and the buttons that fit it.

| Status | Meaning |
|---|---|
| **Pending** | Waiting for the first payment |
| **Active** | Paid up |
| **Past Due** | The renewal was declined. The card is retried each morning until **Grace until**. |
| **Suspended** | The renewal failed with no grace, or the grace ran out |
| **Cancelled**, **Expired** | Over |

| Button | Who | What it does |
|---|---|---|
| **Turn auto-renew off** / **Turn auto-renew on** | Counter staff | **Turn auto-renew on** needs a card saved while paying for a membership with auto-renew ticked. Off needs nothing |
| **Change card** | Counter staff | Pick another saved card for renewals |
| **Retry card** | Manager | Charges the saved card now for an overdue renewal |
| **Look up card charge** | Manager | Replaces **Retry card** when the card processor never confirmed a charge |
| **Take payment** | Counter staff | Pays an overdue renewal with any payment method |
| **Cancel** | Manager | Asks for a **Reason**. It runs to the end of the paid period and is not renewed. Tick **End it today** to end it now |

A refund is not part of cancelling. For a card payment, end the membership with
**End it today**, then use **Card refund → Refund** with type **Return** on its
invoice (see [Taking payment](payments.md#card-refund-on-a-counter-order)). A
membership paid another way gets a credit note made by hand.

## Renewals

Every morning at 7 AM store time, each auto-renewing membership whose period has
ended is charged to its saved card.

| Result | What happens |
|---|---|
| Approved | The next period starts the day after the last one ended, and the receipt is emailed |
| Declined | The renewal invoice is voided. With grace, the membership goes **Past Due** and is retried each morning. Without, it is **Suspended** |
| No card the system can charge | No invoice is made. The membership is handled as declined |
| The processor never answered | The invoice stays open and is never charged again by itself. A manager clicks **Look up card charge**. If it still says not confirmed and the processor's portal shows no charge, open the renewal invoice, click **Charge saved card** and use **Release as not charged**. |
| The processor refused the request | The membership keeps its status and the charge is tried again tomorrow |

A renewal paid late keeps the
original dates. A suspended member who pays starts again that day, and the
months in between are not billed. Yearly members get a reminder email before
their charge. Monthly members get none.

## Following up (managers)

The tiles at the top of the Memberships page show **Active members**, **Monthly
billing**, **Renewing in 7 days** and **Needs attention**. Below them, **Needs
attention** lists each problem with the reason and its invoice.

| Why | What to do |
|---|---|
| **Card charge not confirmed** | **Look up card charge**. If it still says not confirmed and the processor's portal shows no charge, open the renewal invoice, click **Charge saved card** and use **Release as not charged**. |
| **Past due: retried daily until** a date, or **Suspended** | **Retry card**, or **Take payment** at the counter |
| **Renewal not tried yet**, **Renewal not charged** | **Retry card** or **Take payment**. If the page warns that renewals are not charged automatically, see Renewal settings |
| **Sign-up not paid since** a date | **Take payment** or **Drop** |

No **Retry card** button means the saved card cannot be charged. Use **Change card**
on the customer's panel, or **Take payment**.

The members list below it searches by **Name or phone** and filters by plan and
status. It shows **Active** by default. Click a row to open the customer.

## Renewal settings

Click **Renewal settings** on the Memberships page, or open **Membership Settings**
in the **POS & Sales** card of the [Settings workspace](../settings/index.md).
Sales Manager and System Manager.

| Field | What it does |
|---|---|
| **Charge renewals automatically** | On by default. Off, nothing is charged and the page warns **Renewals are not charged automatically. Turn on Charge renewals automatically in Renewal settings.** |
| **Yearly renewal reminder (days before)** | 7 by default. 0 sends no reminder |

## Troubleshooting

| Message you see | What it means | Fix |
|---|---|---|
| **… already has a … membership of …** | The customer already has a live membership of that plan | Work from the existing one |
| **… has no card saved for recurring charges.** | The customer has no card saved for renewals. One is saved only when a membership is sold and paid on the terminal with **Auto-renew** ticked | This membership ends at its period end. Sell the next one with auto-renew |
| **… has a payment or a card charge on it.** | Money touched an invoice you tried to drop or cancel | Finish the payment, or refund it first |
| **… is the invoice of membership …** | A membership invoice cannot be cancelled from the invoice | Cancel from the Memberships panel. Give money back with **Card refund** or a credit note |
