# Troubleshooting

For anyone who meets a refusal or a message they do not understand. Find the symptom, take the first step, then follow the link.

!!! warning "Never work around a compliance refusal"
    A refusal about a 4473, a NICS answer, the bound book or an FFL license is a federal control, not a bug. Fix the record it names. If the control is wrong, get a manager, who has an audited override for that case.

## The most common problems

### A gun cannot be received

The message says the supplier is not registered as an FFL dealer, has no FFL Number, or failed its last check. A firearm may only be acquired from a licensee. Open the supplier, run **Search ATF**, then **Verify FFL**. See [The FFL gate](../stock/receive-goods.md#the-ffl-gate).

### An order cannot ship to a dealer

The destination FFL is expired, not on file, or has no full address. Run **Verify FFL** on it. If it stays refused, ask the buyer for a current license copy or another dealer. Never ship on a license the system refused. See [Ship a firearm to another dealer](../orders/ship-to-dealer.md) and [Verify an FFL](../compliance/ffl-verification.md).

### An order is stuck awaiting a 4473

The form or the background check is not done for every gun on the sale. Find it on **Pending 4473 Orders** and read its NICS status. A Delayed order waits; a Denied one never completes. See [Form 4473 and NICS](../compliance/4473.md) and [Daily checks](../compliance/daily-checks.md#1-pending-4473-orders).

### A gun is missing from the bound book

A daily email `<n> FastBound record(s) stuck unsynced`, or the Dashboard's **A&D** card, lists records that never reached FastBound. Tell a manager the same day when it is a gun that left the store. See [Failed pushes](../admin/fastbound.md#failed-pushes).

### A gun is not on the web store or GunBroker

A push can fail after the click. Read the error on the gun's record and send it again. See [List a gun online](../stock/list-online.md) and, for an administrator, [When a listing did not go out](../admin/online-channels.md#when-a-listing-did-not-go-out).

### A card charge hung or the amount looks wrong

Let the register finish: it reads the outcome from the processor. If a message says money moved but could not be recorded, stop, do not retry, and get a manager. See [Take payment](../counter/payments.md) and [When a charge went wrong](../manager/card-refunds.md#when-a-charge-went-wrong).

### An email says card charges are no longer captured

Someone voided or refunded a charge on the terminal or in the processor's portal, and the POS did not hear. A manager and the bookkeeper decide how to correct it. See [The daily card charge check](../manager/card-refunds.md#the-daily-card-charge-check).

### A serial is refused at the register

The gun is promised elsewhere: a GunBroker buyer or bids, a customer's transfer, out on consignment, or maybe sold on GunBroker. Cancel the line and pick another gun. Never end an auction with bids to free a gun up.

A gun that is gone but still shows in stock is the opposite case. A manager runs [Sync Bound Book](../admin/fastbound.md#sync-bound-book).

### A GunBroker order is disposed but has no Ship button

Dispose booked the guns out but the invoice did not post. Click **Create Invoice** on the card. If it stays in Draft, a manager opens it and reads why. See [GunBroker orders](../orders/gunbroker-orders.md).

### The register refuses a sale held for a web order

The units on the shelf are promised to paid web orders. Ship them to the web customer, or ask a manager to ring the sale. See [Sell accessories](../counter/sell-accessories.md).

### Distributor stock looks wrong or frozen

The house's quantity feed has not refreshed, and an email `Distributor feed stale: <name>` goes out. Tell a manager. See [Distributor setup](../admin/distributors.md#troubleshooting).

## More messages, by screen

Each page lists its own common messages. These are the rest.

### Register

| You see | Fix |
|---|---|
| **Add items to the cart first.** | Add the firearm line, then start the 4473 |
| Popup blocked on **Customer Display** | Allow popups for this address, then click again |
| **This POS Profile does not allow rate changes.** | A manager turns on Allow Rate Change on the register's POS Profile |
| **This is a pickup ticket for … — the money is on that order.** | Collect the balance on the order |
| **That PIN is not right. N tries left.** or **Use the password: the PIN is off.** | Type the right PIN, or the password |
| The guns belong to a different company than the register | Use a register profile for that company |

### Trade-ins

| You see | Fix |
|---|---|
| **Discount the item, not the invoice** | Remove the invoice discount and discount the item line |
| `Say how the seller was paid (Cash, Zelle, Check, ACH), or apply the trade-in as a credit to a sale.` | Pick **Seller is paid by**, or tick the credit box |
| `Pick the seller from Customers to pay them now.` | Pick or create the customer |
| `Enter what the store pays the seller.` | Fill in **Acquisition Cost ($)** |
| On a payout ticket: `Set every payment amount to 0`, `Say how the store paid the customer …` or `A <method> payment needs its reference …` | Clear the payment amounts, pick how the store paid, add the check number or confirmation |
| `Row #N: trade-in <acquisition> was not bought from a private person …` | Take the gun in through Receive Goods |

### Receiving

| You see | Fix |
|---|---|
| `Supplier '<name>' is marked as FFL Dealer but has no FFL Number on file.` | Fill in the FFL Number, or run **Search ATF** |
| `Supplier '<name>' EZ Check status is '<status>'. …` | Run **Verify FFL** on the supplier |
| `Row #N: <serial> is marked as $0 but carries a cost of <amount>.` | Clear the cost, or untick **Cost really was $0** |
| `Item '<code>' appears on multiple lines with conflicting Antique settings` | Receive it as antique or not, on every line |
| `Row #N: '<item>' is not a serialized firearm, so it cannot be received on an FFL Transfer.` | Receive the customer's other goods separately |
| `Row #N: cost of $0 for … needs confirming.` on a consignment | Set **Acquisition Type** to `Consignment` |

### Orders and shipping

| You see | Fix |
|---|---|
| `… is not a Type 03 (Curio & Relic) license — the fourth group must be 03 …` | Retype it from the license copy |
| `C&R license … expired on …` | Ask for the renewed license, or ship to a dealer |
| `Missing C&R details: …` or `"…" is not a valid US state.` | Fill in the field it names; type the state code |
| **C&R Address Mismatch** | A C&R gun ships to the licensed premises only. Match the address to the license |
| **Required: Card approval code** | Type the code from the terminal receipt |
| `That reference looks like a full card number` | Type the approval code, never the card number |
| `Serial … may have sold on GunBroker …` | Check GunBroker, then a manager clicks **Clear "Sold Unknown"** |
| Web pickup: `carries a WooCommerce refund` | Settle the refund on the web store first |
| Web pickup: `still has an outstanding distributor order` or `awaiting a serial from WooCommerce` | Wait for the distributor, or enter every serial on the web order |
| Web pickup: `is not an in-store pickup` | Use **Dispose** instead |

### Store map

| You see | Fix |
|---|---|
| "{location} is a location. Scan the location first, then the things that go in it." | Scan the location label, then the goods |
| "{location} is disabled. Pick another location." | Pick another, or ask a stock manager to enable it |
| "{serial} is not in the store's stock, so it cannot be put in {location}." | Nothing to place: it is sold, shipped or held |
| "A gun's storage location is changed from the Storage Locations page, not on the Serial No itself." | Use the **Move** tab |
| "Only {n} of {item} is not in a location yet, so {qty} cannot be placed." | Lower the quantity |

### Counting the shop

| You see | Fix |
|---|---|
| "{serial} was counted, but it should not be here: {reason}." | Check the reason on the firearm report |
| "{item} is in {group}, which is not part of this count." | Count it in another count |
| "no cost on file — set the item's valuation rate, then adjust by hand" | Ask the bookkeeper to set the item's cost |
| "The stock period is frozen through {date} …" | The books are closed for that date. Ask the bookkeeper |
| "A scan did not save. The list has been reloaded …" | Check the list, then click **Finalize Count** again |

### Fixing a mistake

| You see | Fix |
|---|---|
| `Serial No <n> is '<status>', not Active (in stock).` | Correct Serial works only on a gun in stock |
| `FastBound is not configured` | Ask your administrator. See [FastBound](../admin/fastbound.md) |
| `Item '<code>' is serial-tracked. …` or `… requires serial numbers per unit. …` | Firearms come in and go out through Receive Goods and the sale, not quantity tools |
| `Force-delete is restricted to System Manager.` | Ask a System Manager |

### Customers and the cash drawer

| You see | Fix |
|---|---|
| **A customer cannot be in their own family.** | Pick the head of the family |
| **… has family members (…).** when deleting | Move the members to another family first |
| **… is closing the day right now.** | Someone is counting. Reload in a few minutes |
| **That receipt was already used for another cash expense.** | Attach this purchase's own receipt |
| **Cash entries cannot be deleted. Undo the entry instead.** | A manager uses **Undo** on the Cash Drawer page |
| **This purchase is from before cash tracking started.** | The bookkeeper handles it in the books |

### AI connectors and extensions

| You see | Fix |
|---|---|
| The cpa (or full) MCP server is switched off in MCP Settings. | A System Manager switches it on. See [Settings](../manager/settings.md) |
| **Cannot update** on the Extensions page | Read the reasons in the dialog and ask your developer |

## Where alerts land

- **The bell**, in the app only: NICS results, new web and GunBroker orders, distributor shipments, and records FastBound refused. See [Notifications](../start/dashboard.md#notifications).
- **Email** to **Alert Recipients** in [FFL Settings](../admin/ffl-settings.md), subjects starting `[POS Alert]`. See [Daily checks](../compliance/daily-checks.md).
- A to-do on the stuck record itself, raised once until a person closes it. See [What runs automatically](../admin/background-jobs.md).
