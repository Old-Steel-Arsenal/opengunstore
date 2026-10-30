# Build and ship an order

Counter staff use the **Create Order** page when the buyer is not walking out
with the goods today. It builds an order for a firearm mailed to another
dealer's FFL or to a collector's C&R license, for accessories shipped to a
buyer, for a business buying on its own license, and for anything you order
from a distributor to your store. An order for a
customer who pays a deposit and collects the goods when they come in is taken
here too.

Open it from **Create order** under **Sales** in the
[sidebar](../getting-started/dashboard.md#the-sidebar), or click **New order**
at the top of the [Dashboard](../getting-started/dashboard.md). The address is
`/desk/create-order`.

<figure markdown>
![The Create Order page](../screenshots/page-create-order.webp){ .screenshot }
<figcaption>Create Order runs top to bottom. Buyer type first, then the buyer, the ship-to and the items, with payment and fulfillment below the fold.</figcaption>
</figure>

## Before you start

- You need one of these roles: Sales User, Sales Manager, Stock Manager, or
  System Manager.
- A POS Profile must exist for the company that stocks the guns. You do not have
  to open a POS shift first.
- If a firearm is going to another dealer, that dealer's license must be on file
  as an ATF FFL Record, unexpired, with a complete street address and state. See
  [verifying an FFL](../compliance/ffl-verification.md). A collector's C&R
  license is typed in from the license copy instead (see
  [C&R (Type 03) collectors](#cr-type-03-collectors)).
- The page never runs a Form 4473. The buyer completes theirs at the receiving
  dealer. A gun handed over at your counter goes through
  [the POS instead](pos.md). That includes a gun you order from a distributor to
  the store on a pickup order: it is handed over on a pickup ticket at the
  register once it arrives.

## Buyer Type

Two buttons at the top: **Individual** and **Business (FFL)**.

Pick **Business (FFL)** when the buyer is another licensed dealer buying on its
own license. The guns ship to that license, so the page does not ask for a
separate destination. Pick **Individual** for a retail customer.

The choice also sets the payment rule. An individual's firearms may not leave the
store until the order is paid in full. A business may ship on account.

Distributor lines are for individual retail customers only. A **Business (FFL)**
order cannot include a ship-to-store line.

## 1. Buyer

For an individual, fill in:

| Field | Notes |
|---|---|
| **Customer** | Lists customers that are not FFL dealers. |
| **Receipt Email** | Required. This is where the receipt is sent. |
| **Phone** | Optional. Filled from the customer record when blank. |

To add someone who is not on file yet, see [Customers](customers.md).

The pencil button beside **Customer** opens **Fix Customer Name**. Type the
corrected spelling in **Correct Name** and click **Rename**. This renames the
customer everywhere in the system, so use it for spelling fixes only. To sell to
a different person, pick a different customer.

For a business buyer, pick the dealer in **FFL (ATF Record)**. Two buttons sit
beside it:

- **Verify FFL** checks the license against ATF eZ Check and refreshes the
  record.
- **Lookup by FFL #** opens a dialog where you type an **FFL Number** and click
  **Lookup**. A license not yet on file is fetched and created.

Once the FFL resolves, the page creates or reuses that dealer's customer record,
sets the destination to their own license, and fills the ship-to from the license
premises. A card below shows the licensee, the address, and a badge reading
either **Valid to** with a date or **Expired / unverified**.

### Tax exempt

Business buyers get a **Tax Exempt (resale/exemption certificate on file)**
checkbox. Ticking it drops sales tax from this order and stores the exemption on
the customer record when you create the order.

The certificate is optional. Click **Upload Certificate** to attach a PDF, PNG,
or JPG. When one is already attached the page offers **view** and **replace**
links instead. The checkbox alone is what exempts the order. Whether you also
need the paper certificate on file is a matter for your accountant and your
state.

### Peace-officer excise exemption (California)

California charges an 11% excise tax on firearms, firearm precursor parts and
ammunition. It does not apply when the buyer is an active or retired peace
officer, or the law enforcement agency that employs one. Sales tax still does.

In a California store whose [FFL Settings](../settings/ffl-settings.md) name the
excise tax account, the page shows **Peace officer / law enforcement agency —
excise tax exempt** under the buyer, for both buyer types. Stores in other states
do not see it.

1. Tick the box. The tax line drops the excise and reads "(excise exempt)".
2. Pick the **Exempt buyer**: **Active Peace Officer**, **Retired Peace
   Officer** or **Law Enforcement Agency**.
3. Click **Upload CDTFA-230-FET** and attach the buyer's certificate (PDF, PNG
   or JPG). It is kept on that customer's record. Pick the buyer before you
   upload.

Until the buyer and the certificate are in, the bottom bar lists **Exempt
buyer** and **CDTFA-230-FET certificate** and the order cannot be created. An
officer gives a certificate for every sale; an agency may give one that covers
its purchases. A certificate uploaded for one customer does not count for
another, so changing the buyer asks for a new one.

The exemption belongs to this order only. The next order for the same officer
asks again. Editing the order keeps it.

This is not the **Tax exempt** box above: that one drops all tax for a resale
or government buyer. An individual service member is not exempt from either
tax.

## 2. Destination FFL

This section appears only for an **Individual** buyer once the order has at
least one firearm line, and not on a pickup order. It carries the same
**Verify FFL** and **Lookup by FFL #** buttons, and the same license card.

The order cannot be created without a destination for a regulated firearm. The
system rejects a destination that is expired, missing from the file, or missing
a street and a valid state, because the state is what decides sales tax.

### C&R (Type 03) collectors

A licensed collector may receive a curio or relic at the premises on their own
C&R license. The ATF directory holds no Type 03 licenses, so **Verify FFL** and
**Lookup by FFL #** can never find one. You read the license copy and type it in.

<figure markdown>
![The C&R block in Destination FFL](../screenshots/page-create-order-cnr.webp){ .screenshot }
<figcaption>With the C&R box ticked, the lookup buttons give way to the fields read off the license copy.</figcaption>
</figure>

1. Tick **C&R (Type 03) — ships to the collector's own license** at the top of
   the section. The lookup buttons hide and the C&R fields appear.
2. Type the **C&R License Number** exactly as printed, for example
   1-99-888-03-7C-12345. The fourth group must be 03.
3. Fill in **License Expires**, **Collector Name**, **Licensed Premises
   Address**, **City**, **State** and **ZIP**. **Address 2** is optional. A
   collector who has ordered before is already on file, and the blanks fill in
   from that record. Anything you type outranks the stored value, so a renewed
   expiry or a new address is corrected here.
4. Click **Use this C&R license**. "C&R license saved — the order ships to the
   licensed premises." appears, the license card shows, and the ship-to locks to
   the premises.

Until you click **Use this C&R license**, the bottom bar reads
`Destination FFL (click "Use this C&R license")`. Unticking the box clears the
destination, and so does ticking it, so the order never ships to a license that
is no longer on screen.

From here the order is an ordinary FFL transfer: the sales tax follows the
premises state and the gun leaves the bound book as an FFL transfer when you
dispose it. The license is validated again on the server, so a wrong number
format, a lapsed date or an unreadable state is refused with its reason (see
[Troubleshooting](#troubleshooting)).

## 3. Recipient & Ship-To

Recipient name, phone, street address, city, state, and ZIP.

When a destination FFL is set, the page fills these from the license premises and
locks them. A note reads "Locked to the destination FFL address (firearms ship to
the FFL)." Clear the FFL to unlock them.

For an order with no FFL, type the buyer's address here.

### Pickup in store

Tick **Pickup in store — the customer collects at the counter** when the
customer collects the goods here. The address boxes clear, lock, and are not
saved with the order. The note reads "Counter pickup — no shipping address
needed. Distributor lines can only be ordered to the store." No shipping is
charged.

A firearm on a pickup order depends on where the gun is now:

- **A gun already on your shelf** cannot go on a pickup order. The customer takes
  it home today, so that is a retail sale: ring it on the POS register, or give
  the order a destination FFL to ship to. The checkbox is greyed while such a gun
  is on the order, and adding one turns the checkbox off and says so.
- **A gun ordered from a distributor to the store** is allowed. Plan the line as
  **Ship to store**. No destination FFL is asked for. When the gun arrives and is
  received, its serial is held for this order and a $0 pickup ticket opens on the
  POS, where the Form 4473 runs and the gun is handed over. See
  [Pickup tickets](pos.md#pickup-tickets).

An antique firearm needs no 4473, so it can be picked up from stock. Setting a
destination FFL clears the checkbox.

### Orders for goods you do not have, and deposits

The register has no button for an order the store has to source. Take it here: a
customer orders goods you do not have, pays a deposit, and collects them when
they come in.

1. Pick the customer under **Individual**.
2. Tick **Pickup in store — the customer collects at the counter**.
3. Add the item from the distributor results in **Add Item** (see
   [Distributor lines](#distributor-lines)).
4. Open [Plan fulfillment](#plan-fulfillment) and choose **Ship to store** for
   the line. Click **Apply plan**.
5. Under **5. Payment**, type the deposit in **Amount**. The note reads
   "Deposit: … now — … still due."
6. Click **Create Order**.

The order waits on the [Pending Order queue](pending-orders.md). Place the
distributor leg there with **Place order**; a ship-to-store leg may be placed on
a deposit. When the goods arrive and are received:

- **A firearm** gets its pickup ticket on the POS. The balance is collected at
  the register when the gun is handed over, or earlier with **Record Payment**.
- **Anything else** moves to **Ready for pickup**. Collect the balance with
  **Record Payment**, then hand the goods over and click **Mark Picked Up**.

### Sales tax follows the ship-to

Sales tax follows where the goods are handed over, not where the customer lives.
The system decides from the ship-to on the order, and it does so on its own.
"Home state" means the state on your store's own company address.

| Situation | Tax |
|---|---|
| Picked up in store | Charged at the normal rate |
| Ship-to inside your home state | Charged at the normal rate |
| Ship-to in another US state | Zero, using the **Out of State Sales** template |
| No ship-to on the order | Charged at the normal rate |

Points that matter at the counter:

- The test is the ship-to. A customer visiting from another state who buys over
  the counter pays your home-state tax, and that is correct. Do not change it by
  hand.
- The state can be written as a code or spelled out (`TX` or `Texas`), in any
  case. Both are understood, in the preview and on the order.
- Filling in the right ship-to when the order is created is the whole job. The
  tax follows.
- A register sale can never be tax-exempt this way. If the zero-rate template
  ends up on a POS Invoice without an out-of-state shipping address, the system
  refuses the submit and tells you to switch the template back.
- Shipments outside the United States are not covered by this rule. They are
  taxed normally, and an exception is a question for your accountant.

**Tax on shipping.** Shipping follows the goods: when the goods on an order are
taxed, so is the shipping charge, except in a home state that does not tax
shipping.

- With **Shipping Price Includes Sales Tax** on in
  [FFL Settings](../settings/ffl-settings.md) (the default), the amount you type
  in **Shipping** is what the customer pays, with the tax already inside it. The
  preview reads, for example, "Shipping $45.00 (sales tax included)". With the
  setting off, the tax is added on top and the preview shows it as "(+$… tax)".
  The setting applies to new orders only; an order keeps the way it was created.
- In California shipping charged at cost is not taxed. The **Shipping** box says
  "The actual carrier cost — shipping charged at cost is not taxed here. Put any
  mark-up or handling on its own taxable line."

## 4. Items

Type into **Add Item**. One box takes all of these:

- a scanned barcode,
- a SKU or part of an item name,
- a firearm serial number.

Use the up and down arrows and <kbd>Enter</kbd> to pick from the dropdown. Store
stock and distributor catalog results appear in the same list, store stock first.

Scanning a serial adds that exact gun, its serial, and its tag price as a
finished line in one step, so a scanner can run through several guns without
touching the mouse.

### Picking a serial for a firearm

Choosing a firearm from the dropdown opens a card with **Serial (in stock)**,
**Qty**, and **Price**. Pick the serial and the price follows that gun's own
selling price. Quantity is fixed at one per serial. Click **Add to Order**, or
**Cancel** to drop the card.

A serial already on the order is refused a second time.

### Distributor lines

Distributor catalog results appear under **Catalog API — not in local stock**,
one row per product. Each row names the distributors that carry it and their
total quantity. Picking one opens a card with the number of sources, the lowest
cost and the total quantity. Set **Qty** and **Price**, then click
**Add to order**. Price starts at MAP when the catalog has one, else MSRP. A
firearm is fixed at one unit per line.

No distributor is chosen yet. The line lands with an **API** tag reading "choose
source in the plan", and the order cannot be created until you run
[Plan fulfillment](#plan-fulfillment) below.

Gun parts are not firearms. An upper, a barrel or a conversion kit carries no
**Firearm** tag, its quantity can be changed, and no serial is taken. A line is
treated as a firearm only when its item is one.

### The line table

Each line shows a **Firearm** tag where it applies, the serial beneath the name,
quantity, cost, price, and subtotal. Price is editable in place on every line. Quantity
is editable on non-firearm lines. The ✕ button removes a line.

**Cost** sits left of **Price** and shows what the line costs you: the gun's own
cost for a serial, the distributor's cost for a distributor line, and the item's
stock cost otherwise. It reads **…** while it loads and **—** when no cost is known.

Changing the quantity of a distributor line marks it **unconfirmed**. Run
[Plan fulfillment](#plan-fulfillment) again before you create the order.

**Where it is taken from.** A line of loose stock from your own shelf (ammo, an
optic, a part) shows a dropdown under its name listing the
[storage locations](../inventory/storage-locations.md) that hold the item, with
the quantity in each. When the item sits in one place only, that place is picked
for you. With several, pick the one you took it from. The first choice,
**Taken from: not said**, is allowed and is outlined in orange; the units then
show on the
[Not placed list](../inventory/storage-locations.md#the-not-placed-tab) for
someone to confirm later. Firearms and distributor lines have no dropdown.

**Charge exactly.** Below **Total** sits a **Charge exactly** box. Type the total
the customer pays, tax included, and press <kbd>Enter</kbd> or leave the box. The
line prices are worked back from it before tax, in proportion to their current
subtotals, and the box clears. Shipping is kept as it is, so the total must be
more than the shipping charge.

**A price below the list price** asks a manager when you click **Create
Order** (or **Save Changes** on an edit), as on the register: see
[Lowering a price](pos.md#lowering-a-price). The list price is the gun's own
price for a firearm (the cheapest gun of the model in stock while the line has
none), and the Item Price for anything else. Distributor lines (drop-ship and
ship to store) are priced from the distributor's quote and are not checked. Editing an order keeps the prices
it already had; only a price lowered in the edit asks.

Below the table sit **Shipping**, a tax preview, and the running **Total**. The
tax preview reads "Out-of-state — no sales tax" when the ship-to state is not
your home state, and "Tax exempt — no sales tax" when the exemption is on. An
item with its own tax rate is previewed at that rate. If the tax settings fail
to load, an orange message says the total shown may be off. The server
recomputes the real tax from the shipping address when the order is created.

## Plan fulfillment

The **Plan fulfillment** link sits next to the line count. It turns into a
highlighted pill whenever a line still needs a channel.

Clicking it quotes every channel at once and opens the **Fulfillment plan**
dialog. Rows are your order lines. Columns are **Local stock** and every enabled
distributor, in the priority order set on the
[Distributors hub](../distributors/hub.md).

### What each cell shows

| Cell | What it means |
|---|---|
| **_N_ in stock** | Your own stock. It fills the line when it covers the quantity |
| **serial in hand** | The line already carries a serial from your stock |
| **short _N_ — click to split** | Some stock, not enough. Click to split the line |
| Cost · **qty**, then **Ship to store** | The distributor's cost and live quantity, and the button that orders the line to your store |
| **—** | The distributor does not carry the item |
| **unavailable** | The distributor could not be quoted. The other columns still work |

Where local stock is short, clicking the local cell keeps the on-hand quantity
as a local line and moves the remainder to a new line you then route to a
distributor.

A greyed-out **Ship to store** button means the distributor cannot fill this
line, and the reason is written under the button.

| Reason under the button | Why |
|---|---|
| ship to store is switched off for this distributor | **Stocking** is unticked on that distributor |
| insufficient distributor stock | The distributor holds fewer than the line needs |

### Choose a channel for each line

The plan opens with a suggestion for every line. Local stock comes first when it
covers the line. Otherwise the plan picks the lowest-cost distributor that can
fill it, the higher-priority one on a tie. A line you planned before keeps its
choice while that choice still works.

- Click a distributor's cell to order the line from that distributor to your
  store.
- Click the cell under **Local stock** to fill the line from your own stock.

**Ship to store** has the distributor ship to your store, and
you ship the goods on from there or hand them over at the counter. A firearm
shipped to the store is received into your bound book like any other
acquisition, and its serial is assigned to this order when it arrives.

You can pick any distributor that can fill a line, including one other than the
distributor your web store sources that item from. The choice applies to this
order only. The item's web store source does not change.

When you edit an order whose distributor order is already placed, those lines
show one greyed cell, "Already ordered on … — not re-routable here". The plan
leaves them alone and does not quote their freight again.

### The summary and freight estimate

The line under the grid names the legs and shows the sale total, the distributor
cost, the estimated freight, and the margin excluding local cost, with the
freight already taken off. When a line has no way to be filled it reads "_N_
line(s) have no feasible channel — remove them." instead.

Below it, one line per distributor order gives the distributor, **to store**, the number of lines, and the estimated
freight or **free**. Lines are grouped the way they will be ordered, so a
distributor's firearms and accessories count as separate orders.

The estimate comes from each distributor's Freight tab, described in
[Integration settings](../settings/integrations.md). It shows **…** while it is
worked out. It shows **(partial)** when a distributor could not estimate, and that
distributor's line reads **freight unknown**. The freight the distributor invoices
always replaces the estimate.

Shipped to your store, RSR sells full cases at its case price. When RSR lists
one, the cell for that line prices the full cases at it and a note in the cell
reads, for example, **Ship to store: Case of 50 at $7.83 each — 20 more fill
a case**. The Distributor Order line shows the same note under **Case Price**.
Drop-ship lines pay the unit cost, and the cost RSR acknowledges replaces the
estimate.

A Lipsey's drop-ship order is estimated from its flat rates. An order to your
store reads **freight unknown**. A Sports South order shipped to your store uses
Sports South's store shipping rule, not the drop-ship rate: handguns ship free
with 3 or more or $750 or more, and everything else free at $500 or more, below
that 5.00 by default. Both thresholds are on the Sports South Freight tab.

A Sports South order also shows **Signature**, **Adult signature** and **Insurance**
checkboxes. They start from the delivery options in
[Sports South Settings](../settings/integrations.md#sports-south-settings). Tick or untick one for this
order and its freight estimate updates. The choices go to Sports South with the
order and are kept when you edit it. RSR orders show no checkboxes.

### Apply the plan

Click **Apply plan**. It reads **Applying…** while the system sets up an Item for
each distributor line that does not have one yet. When every line applies,
"Fulfillment plan applied." appears and the dialog closes. A line that fails is
listed under **Plan partially applied** and stays unconfirmed, and the dialog
stays open so you can choose again.

Planned distributor lines carry a tag in the line table, with a note under the
item name:

| Tag | Note |
|---|---|
| **Ship to store** and the distributor, on a firearm | Ordered to the store — serial assigned and held when it arrives |
| **Ship to store** and the distributor, on anything else | Ordered to the store — ships once it arrives |

Once planned distributor lines exist, section 6 stops offering the dispose
choice. It lists the legs instead, with **Estimated distributor freight** while
the lines stay as planned, because an order with a distributor line always
defers. The legs are placed with **Place order** on the
[Pending Order queue](pending-orders.md#place-order). A leg may be placed on a
deposit, or before anything is paid, because the goods come to your own shelf.

## 5. Payment

Payment is collected now, with the method you pick, unless you tick
**Pay later**.

<figure markdown>
![The Payment section with Charge exactly above it](../screenshots/page-create-order-payment.webp){ .screenshot }
<figcaption>Charge exactly sits under the total. On a store with the card terminal, Credit Card adds Charge via Terminal.</figcaption>
</figure>

| Control | Notes |
|---|---|
| **Payment Method** | Cash, Credit Card, Zelle, ACH, or Check/Money Order. |
| **Amount** | Defaults to the grand total. Type less for a deposit. Hidden while **Pay later** is ticked. |
| **Charge via Terminal** | Appears for **Credit Card** on a store with the card terminal integration switched on. Ticked by default. |
| **Pay later** | No payment now. The order books on account and you collect it from Pending Order. |
| **Zelle / ACH Transaction Number** | Appears for Zelle and ACH. Required. |
| **Check / money order number** | Appears for Check/Money Order. Required. |
| **Card Approval Code** | Appears for a card that is recorded rather than run on the terminal. Required. |

### Cash, Zelle, ACH and Check/Money Order

The amount is recorded as received when the order is created. Entering less than
the total shows a deposit note with the balance still due. If the buyer is an
individual and you also chose to ship now, that note turns red: an individual
order needs full payment before the guns move.

### Credit Card on the terminal

With **Charge via Terminal** ticked, a blue note reads "The card is charged on
the terminal right after the order is created." That is the order of events:

1. Click **Create Order**. The order books first, on account.
2. **Waiting for terminal** opens with the amount. The customer dips or taps the
   card on the terminal. Only the **Cancel** button in that dialog closes it.
3. On approval a green message shows the authorization code, and the payment is
   recorded on the order for you. The **Order Created** dialog then reads
   **Paid … on the terminal (auth …)**, or for a deposit
   **Deposit … on the terminal (auth …) — … still due.**
4. If the card is declined, the customer backs out, or the terminal times out,
   the order still exists, on account. The dialog says the card was not charged
   and how much is outstanding, and a **Retry card** button runs the same amount
   again. You can also collect it later with **Record Payment** on the
   [Pending Order queue](pending-orders.md#record-payment).

**Dispose & ship now** is skipped on a terminal charge, because the guns may not
move before the money is in. The note under the payment boxes says so, and the
order ships from Pending Order once the card is approved.

### A card already charged

Untick **Charge via Terminal** when the card was already run outside the
system, for example on the Virtual Terminal, and you only need to record it. A
**Card Approval Code** box appears and is required: type the approval code from
the terminal or Virtual Terminal receipt. On a store taking cards on a
standalone terminal, the same box appears for every card payment.

### Pay later

Tick **Pay later** to book the order on account with nothing collected. The
amount box hides. Collect with **Record Payment** on the
[Pending Order queue](pending-orders.md#record-payment), or at the register
when a pickup order's gun is handed over.

!!! warning "Never type a card number"
    The reference field is for an approval or confirmation code. The system
    rejects anything that looks like a full card number, because that would put
    cardholder data into an accounting voucher.

## 6. Fulfillment

Two buttons, defaulting to **Create only — dispose later**.

- **Create only — dispose later** puts the order on the Pending Order queue. The
  gun stays in stock and on the bound book until you ship it.
- **Dispose & ship now** books the transfer, takes the gun out of stock, and
  pushes to FastBound as part of creating the order.

**Order Note (staff)** below is internal. It shows on the Pending Order queue and
is saved on the invoice, and it is kept off the customer's printed invoice.

## Create the order

The bar at the bottom shows what is still missing, as "Required:" followed by a
list, and turns green with "Ready to create." when nothing is. Click
**Create Order**.

### What the system does

1. Builds and submits a Sales Order, then a Sales Invoice derived from it.
2. Applies destination-based sales tax, or none when the buyer is exempt, and
   the shipping charge with any tax it carries.
3. Books the payment. A full cash, Zelle, ACH or check payment rides on the invoice as
   a counter tender. A deposit, and every card charged on the terminal, books as
   a separate Payment Entry — the terminal payment carries the gateway's
   reference as its transaction number.
4. Emails the receipt to the address in **Receipt Email**.
5. For each distributor leg, creates a Draft Distributor Order with the
   distributor the plan chose, under that distributor's own stock number. If
   one cannot be built, the whole order is rolled back.
6. With **Dispose & ship now**, books one FFL transfer disposition per serial,
   issues the stock, and queues the [FastBound](../compliance/fastbound.md) push.
   For a C&R destination, the ATF FFL Record saved from the license copy is the
   receiving license.
7. Takes each loose-stock line out of the location you named when its stock
   leaves: at once with **Dispose & ship now**, otherwise when the order ships
   from the queue.
8. Where ShipStation is enabled, pushes the shipment so staff buy the label
   there.
9. Deletes the draft you resumed, if any.
10. When the buyer has not signed an agreement the sale needs (the Used Item
    Agreement, or one an item names), emails the signing link to the order's
    email, or the customer's when there is none. One link covers every gun on
    the order, unless the agreement has **One copy per gun** ticked. The order
    is created either
    way. With **Dispose & ship now** the order is not disposed: the gun waits on
    the Pending Order queue until it is signed.

The **Order Created** dialog then links the Sales Order and the Sales Invoice,
states what was paid or left outstanding, and says whether the order was disposed
or queued, and lists any agreement not signed yet with the address its link went
to. When a link could not be sent (no email address, a mail error) it says so in
red: send it from the order's **Resend link** on Pending Order. **Print Invoice** opens the invoice print view in a new tab. After a
terminal charge that did not go through, **Retry card** sits beside it.

<figure markdown>
![The Sales Invoice list](../screenshots/list-sales-invoice.webp){ .screenshot }
<figcaption>Orders built here land as Sales Invoices. The list filters on Destination FFL and FFL Buyer Type.</figcaption>
</figure>

!!! danger "Dispose is a bound book entry"
    Disposing books the transfer in the bound book and sends it to FastBound.
    Do this only when the gun is really leaving. Undoing it means cancelling the
    whole order.

An order containing any distributor line always defers, whatever you chose.
Local and ship-to-store lines ship from the Pending Order queue.

## Save Draft and Drafts

**Save Draft** parks the half-built form. Drafts are shared shop-wide, so one
person can park an order and another can finish it.

**Drafts** opens the saved list with each draft's title, who touched it last,
when, and its item total. **Load** restores it. The ✕ button deletes it. Loading
a draft over a form you have already started asks for confirmation first.

A draft reserves nothing. Serials are re-checked when the draft is loaded, and
any gun that has since sold or been claimed by another order is dropped from the
form with the reason given.

## Editing an order

Counter orders can be edited from the **Edit** button on the
[Pending Order queue](pending-orders.md), which opens
`/desk/create-order/edit/<invoice>`.

An orange banner reads "Editing" with the invoice number and warns that saving
cancels it and creates a replacement invoice. **Exit without saving** leaves.

The Payment and Fulfillment sections are replaced by a read-only **Money**
summary showing what has been paid so far and what happens on save:

- a lower new total shows the refund that will be made,
- a higher new total shows the balance left to collect,
- changing the buyer refunds the whole amount to the original buyer and starts
  the new order unpaid.

When a refund is due, pick a **Refund Method**. Zelle and ACH also ask for the
confirmation number, and Check/Money Order for the number of the check you
write back, in **Refund Reference**. The staff order note moves up under the Money block so you
can read and change it. The submit button reads **Save Changes**. Saving shows
an **Order Updated** message naming the replacement invoice, any payment carried
over, any refund, and any balance still to collect, then returns you to the queue.

### After the distributor order is placed

Goods a distributor is already sending cannot be recalled, so they ride the edit
unchanged:

- A line already bought on a placed distributor order reads "Already ordered on
  … — quantity and source are fixed; the price can still change." It has no ✕
  and no quantity box. Its distributor order moves to the new invoice; nothing is
  ordered twice.
- You may change its price, the note, and the other lines. A new line, or more
  of an item than was ordered, is planned and ordered on its own.
- A distributor order still in Draft or Queued is withdrawn and created again
  from the edited order.

### Ready-for-pickup orders

An order whose gun has arrived and is held can still be edited and repriced
before the customer pays. The serial stays held, and an open pickup ticket
follows the new invoice. Changing the buyer while a pickup ticket is open is
refused: delete that POS Invoice first.

### When editing is refused

Editing is refused once the order has a booked disposition, has been pushed to
ShipStation or marked shipped, has a submitted Delivery Note, or has handed its
gun over on a completed pickup ticket. Use **Cancel Order** and rebuild instead.

## Troubleshooting

| Message you see | What it means | Fix |
|---|---|---|
| Required: Destination FFL | The order has a firearm and no destination. | Pick and verify the receiving dealer. |
| Required: Destination FFL (click "Use this C&R license") | The C&R fields are filled but not saved. | Click **Use this C&R license**. |
| … is not a Type 03 (Curio & Relic) license — the fourth group must be 03 … | The number is not a C&R license, or is mistyped. | Retype it from the license copy. |
| C&R license … expired on … | The collector's license has lapsed. | Ask for the renewed license, or ship to a dealer. |
| Missing C&R details: … | A required C&R field is blank. | Fill in the field it names. |
| "…" is not a valid US state. | The premises state cannot be read. | Type the state code or full name. |
| Required: Fulfillment plan (unconfirmed distributor lines) | A distributor line has no confirmed channel. | Click **Plan fulfillment**, then **Apply plan**. |
| _N_ line(s) have no feasible channel — remove them. | No stock, and no distributor can fill the line. | Remove the line, or lower its quantity. |
| … can't order … to the store — … Reopen the fulfillment plan. | The distributor was disabled, or its **Stocking** switch turned off, after you planned. | Click **Plan fulfillment** and choose again. |
| … can't be shipped to … (state restriction) | The item, or the distributor you chose, will not ship there. | Remove the line, or plan it from another distributor. |
| Distributor ordering … is for individual retail customers only … | A **Business (FFL)** order has a distributor line. | Remove the line, or sell from stock. |
| Not a Counter Pickup | A pickup order has a gun from stock, or a destination FFL. | Do what the message says: ring the gun on the register, or clear **Pickup in store** or the FFL. |
| Destination FFL … is not on file — verify it first. | The license has never been fetched. | Use **Lookup by FFL #**, then **Verify FFL**. |
| Destination FFL Expired | The license on file has lapsed. | Run **Verify FFL** to refresh it. |
| Destination FFL Address Incomplete | No street or no valid state on the record. | Open the ATF FFL Record and complete it, or verify it again. |
| Shipping Tax Setting Changed | Someone changed **Shipping Price Includes Sales Tax** while this page was open. | Reload the page to see the correct total, then create the order. |
| Individual orders must be paid IN FULL before shipping | You chose **Dispose & ship now** with a deposit, or with **Pay later**. | Collect the balance, or switch to **Create only — dispose later**. |
| Required: Card approval code | A recorded card payment needs its approval code. | Type the code from the terminal or Virtual Terminal receipt. |
| Card not charged — on account, … outstanding | The terminal declined, timed out, or the charge was cancelled. The order exists. | Click **Retry card**, or collect from Pending Order. |
| On account — outstanding …. Terminal not configured | The card integration is switched on but no terminal device is set up. | Run the card on the Virtual Terminal, then on Pending Order use **Record Payment** with **Already charged on Virtual Terminal** ticked and the approval code. |
| Some firearms are no longer available | A gun on the draft sold or was claimed since you saved. | Pick replacements from the item search. |
| Wrong company for this inventory | The guns belong to a different company than your POS Profile. | Use a POS Profile for that company, or open a shift on it. |
| That reference looks like a full card number | A card number was typed into the reference box. | Enter the terminal's approval code instead. |
| … was already ordered from … — it can't be removed or reduced here … | You removed or reduced a line a distributor is already sending. | Put it back. Only its price may change. |
| … has an open pickup ticket … for the current buyer … | You changed the buyer while the gun's pickup ticket is open. | Delete that POS Invoice first, then change the buyer. |
| … handed its firearm over on … — a completed counter pickup cannot be cancelled or edited … | The gun already left on its pickup ticket. | Nothing to edit here. Ask a manager about a return. |
