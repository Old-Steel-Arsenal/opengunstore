# Bookkeeping: cash drawer, reports and month-end

What drawer entries book, what the Sales Report reads, the clearing accounts and sales tax. Part of [For the bookkeeper](bookkeeper.md).

## What each drawer entry books

"The drawer should hold" is the cash account's balance plus cash from register sales still being posted in open shifts.

| Entry | Booked as |
|---|---|
| Deposit, cash from bank, cash expense, seller payment for received goods | A Journal Entry |
| Count difference | A Journal Entry to the cash over and short account, with the reason |
| The store's very first count | Sets the books to what was counted, with no loss booked; the accountant gets a note |
| "Paid outside the system" | Nothing |

Count differences and the first count change the Log balance but are not counted as cash in or out. Purchases received before **Cash tracking started** (Cash Drawer Settings) are left to the accountant.

When **Accounts a cash expense can go to** is empty, a cash expense can go to the standard accounts: Print and Stationery, Office Maintenance Expenses, Freight and Forwarding Charges, Postal Expenses, Meals, Entertainment Expenses and Miscellaneous Expenses.

On Cash Drawer Weekly, the week foots when "Daily lines add up to" equals "Cash ledger movement", Difference is 0.00, and the "Unclassified ledger lines" Note reads "None."

## Cash drawer entries

Every journal entry the drawer books (a deposit, cash from the bank, an expense, a seller payment, a count difference) can be undone only from the Cash Drawer page. Cancelling, reversing or amending one from the accounting screens is refused with a message saying where to go. This includes entries labeled **Owner took (old entries)**.

A register-ticket payout, where the store paid the customer for trade-ins, goes away by cancelling that ticket.

**First count & history** on Cash Drawer Weekly: the store's first count set the books to the drawer once. A later correction to the days before it is booked to it at the next count.


## Sales Report: what it reads

Sales fan out across two document types, so the report reads both and counts each sale once:

- **POS Invoice**, submitted, for counter sales. Channel **POS**.
- **Sales Invoice**, submitted and not consolidated, for everything raised as an order. Channel **Web**, **GunBroker** or **Manual** by where the order came from.

Consolidated Sales Invoices are left out. They are the nightly roll-ups of POS Invoices the report already counted, so including them would double the day. Today's counter sales show at once, before the nightly close.

The **Tax lines** view reads per-line tax from the invoice's tax detail. It adds an order-level shipping row, and an unclassified-charge row for any tax account that is neither sales tax nor shipping.

## Sales Report and the books

- Net Sales is higher than the ledger income account by exactly the period's trade-in allowances, because Net Sales is gross of trade-in credits. Profit still ties to the ledger. On the netted figure a 15% gun would read about 39% margin.
- **Do not reconcile the Cost column against ledger cost of goods sold for non-serialized lines.** The ledger books those at actual first-in first-out cost, and the report estimates them from the bin's moving average. The gap grows with ammunition volume.
- **A refunded web order comes off its own lines.** The refund takes the refunded sales tax and shipping off the invoice's tax and shipping lines, not off the goods. In a month with refunds the Tax lines view shows lower tax and shipping, not a goods discount.
- A GunBroker sale shows zero sales tax. GunBroker collects and remits as the marketplace provider under its own license, so the tax lands on a marketplace liability account. Do not chase in-state GunBroker orders as missing-tax exceptions.
- **Marketplace fees are an expense, not cost of goods.** They sit in Channel Fees, never inside Cost.

Cost is computed, not stored. Nothing stores cost on an invoice line, so the report computes it per line:

| Line | Cost |
|---|---|
| Trade-in credit | Zero |
| Consigned gun | Net sales × (1 − commission rate) |
| Firearm | The sum of purchase prices on its serials |
| Anything else | Signed quantity × moving-average cost in that warehouse |

| Card | What it holds |
|---|---|
| **Sales Tax** | All sales tax charged in the period, including tax on shipping, excluding tax a marketplace collected. It should equal the period's Sales Tax Payable credits from sales invoices |
| **Marketplace sales** | Merchandise net plus freight income on GunBroker orders delivered to the store's state, excluding the marketplace's collected tax and buyer service fee |

## Clearing accounts

| Account | What the balance is | How to clear it |
|---|---|---|
| Payroc Card Clearing | Card takings not yet in the bank | Compare with bank deposits. The gap is processor fees |
| Woo Clearing | Web takings not yet settled | Same, against the WooCommerce settlement report |
| GunBroker Clearing | Card channel only | Zero while the store takes checks and money orders |

The daily card charge check reads the last week's card charges back from Payroc. It emails **Payroc: … card charge(s) no longer captured at the gateway** for any charge voided or refunded outside the POS, with a comment on its invoice. Resolve each before closing (see [Correcting a charge the daily check flags](bookkeeper-selling.md#correcting-a-charge-the-daily-check-flags)).

## GunBroker checks in transit

GunBroker checks in transit are a bank-side item, not a clearing account. Checks and money orders go into the operating bank account named in GunBroker Settings (**Check deposit account**).

The bank reconciliation always carries a batch of deposits that have landed in the bank with nothing yet on the ledger. The receipt posts on its invoice date, never on the deposit date, and the money arrives before the gun is disposed.

Tie the batch out against the sum of the **Banked Amount** column on item 3 of GunBroker Cash Basis Adjustments. Do not use the sum of order totals, which differs on a short or over payment, and do not net refunds out of it.

## Firearm dispositions and Stock Adjustment

A firearm disposition leaves stock through a material issue that posts to **Stock Adjustment**. Debits on that account arising from firearm dispositions are cost of goods sold, not shrinkage.

Non-firearm lines such as ammunition and optics post to Cost of Goods Sold directly and are outside this manual reclassification.

## Web accessory stock legs

A web order's Sales Invoice does not move stock itself. Its firearms leave through the disposition, and its accessory lines leave through a Delivery Note raised against the invoice.

When that leg is missing, the Sales Report still shows a cost, because its cost comes from the bin valuation, not the ledger. The report cannot reveal the gap.

An open task is raised once on the Sales Invoice or web order for each of these:

- A stock leg that could not be issued.
- A revenue booking that keeps failing.
- An order whose only invoice is cancelled.
- A refund held because someone is holding the stock leg.
- An invoice left in draft on a total mismatch.

A submitted return against a web Delivery Note puts the goods back at their original valuation on the return's own date. The Sales Report does not net that out, so in such a month report cost exceeds ledger cost of goods sold by the returned amount.

## Sales tax and shipping tax

- With **Shipping Price Includes Sales Tax** on in FFL Settings (the default), the shipping price on an order is what the customer paid. The tax is carved out of it and reported as usual. Whether shipping is taxable depends on the state.
- A refunded web order takes the refunded tax and shipping off the invoice's own tax and shipping lines, so Sales Tax Payable drops by the refunded tax.
- Sales Tax Payable is always zero on a GunBroker order. GunBroker collects and remits as the marketplace provider into its own liability account, GunBroker MPF Sales Tax Payable. Check that account's credits instead.

Worked example at an 8.25% rate: $320.00 of goods with $23.10 of shipping carries $26.40 of tax on the goods and a $1.91 shipping tax row.

| Figure | Amount |
|---|---|
| Sales Tax card, and the Sales Tax Payable credit (8.25% of $343.10 taxable) | $28.31 |
| Wrong: adding the shipping tax again | $30.22, overstating tax due by $1.91 |
| The invoice's Total Taxes and Charges (not the tax figure: it includes $23.10 of shipping income) | $51.41 |

## The GunBroker accounts

| Account | What it holds | How it clears |
|---|---|---|
| GunBroker MPF Sales Tax Payable | Tax GunBroker collected as marketplace provider | Against the tax statement. Remit when GunBroker's statement says to, by journal entry |
| GunBroker Marketplace Fee Payable | The buyer service fee GunBroker charges the buyer through you | Against whichever statement carries it. The report lists it apart from the collected tax |
| Accrued GunBroker Fees | Final value fees and FFL document fees owed, accrued per item | Against GunBroker's fee bill, by journal entry |
| Marketplace Fees - GunBroker | The expense side of that accrual | A profit-and-loss expense, never cost of goods |
| GunBroker Clearing | The card channel's clearing account | Zero while the store takes checks only |

Settle these with journal entries, not purchase invoices. There is no payable to age.

The books accrue, and a return may be filed on the cash basis. Seven items bridge the two, and **GunBroker Cash Basis Adjustments** is the schedule.

A GunBroker order is always paid before it is invoiced, and the receipt posts on the invoice date, never on the deposit date. Otherwise reconciling the bank and reading the invoices would count the same check twice.

There is no refund button for GunBroker orders in the POS. GunBroker files the sales tax under its own license, so a refund issued in the POS would refund the buyer's tax while GunBroker still reports the original amount, and the store would absorb the difference.
