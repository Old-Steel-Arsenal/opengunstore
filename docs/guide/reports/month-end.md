# Month-end close

This is the bookkeeper's monthly checklist. Work through it in order after the last business day of the month. Every report named here opens from [Reports](index.md).

The POS is the business system and the source of the numbers. The Sales Report, Inventory Receipts, the sales-tax liability and Stock Balance are what a bookkeeper carries into the outside books. Your accountant decides which system is the book of record. This page covers what to check in the POS, not how a particular firm books it.

## 1. Sales completeness

- [ ] Run **Sales Report** for the month and compare its total against **Sales Register**. They should agree. A gap is unconsolidated POS Invoices, which the reconsolidation sweep will retry.
- [ ] Reconcile the web store. Total the Web-channel invoices and compare against completed WooCommerce orders for the month. Two normal sources of difference are accessory-only orders, which never touch the counter, and orders not yet disposed on the last day of the month.
- [ ] Match every return invoice for the month against a WooCommerce refund.
- [ ] Reconcile GunBroker. Filter the Sales Report to the GunBroker channel and compare against the marketplace's own month. Most of the difference is timing, because GunBroker records the sale date and the POS records the disposition date. Work it order by order in **GunBroker Statement Reconciliation**, which sorts each line into an amount difference, a timing difference or a missing record.
- [ ] Read the **Accrual** column on that report. It shows none, posted, cancelled or reversed. An amount that matches while the accrual is not **posted** is the real problem. Both sides derive from the same order, so the numbers agree even when the accrual journal entry was never created or was reversed and never re-hung.
- [ ] Run **GunBroker Cash Basis Adjustments** with the period end set to the last day of the month and read item 3, paid but not disposed. Those orders correctly post nothing to the ledger this month.

## 2. Clearing accounts

Every clearing account must reach zero or be explainable.

| Account | What the balance is | How to clear it |
|---|---|---|
| Payroc Card Clearing | Card takings not yet in the bank | Compare with bank deposits. The gap is processor fees. |
| Woo Clearing | Web takings not yet settled | Same treatment against the WooCommerce settlement report. |
| GunBroker Clearing | Card channel only | Zero while the store takes checks and money orders. |

The daily card charge check helps with Payroc Card Clearing. Every day the system reads the last week's card charges back from Payroc and emails an alert, **Payroc: … card charge(s) no longer captured at the gateway**, for any charge voided or refunded outside the POS. Each one also has a comment on its invoice. Every such charge should be resolved before you close. See [Taking payment](../selling/payments.md).

The card payments ledger is the other check. It lists every Payroc payment and refund for a date range, counter and web alike, each matched to what the POS recorded, and flags anything that does not line up: a payment the POS never recorded, a different amount, or a charge voided or refunded in the Payroc portal. Cards keyed on the Virtual Terminal are labeled as such. It has no report screen. Ask for it through an MCP connector (the `payroc_transactions` tool), at most 31 days at a time. It needs **Accounts User**, **Accounts Manager** or **System Manager**. See [The MCP server](../../ai/mcp.md).

GunBroker checks in transit are a bank-side item, not a clearing account. Checks and money orders go into the operating bank account named in GunBroker Settings. Your bank reconciliation will always carry a batch of deposits that have landed in the bank with nothing yet on the ledger. That is by design: the receipt posts on its invoice date, never back on the deposit date, and the money arrives before the gun is disposed. Tie the batch out against the sum of the **Banked Amount** column on item 3 of the Cash Basis Adjustments report. Do not use the sum of order totals, which differs on a short or over payment, and do not net refunds out of it.

## 3. Cash drawer

- [ ] Run **Cash Drawer Weekly** with **From** on the first day of the month and **To** on the last. Despite the name it takes any range up to 400 days.
- [ ] Read the lines at the foot. The **Difference** under **Daily lines add up to** and **Cash ledger movement** must read 0.00, and **Unclassified ledger lines** must read **None**. An unclassified line is a hand entry to the Cash account. Find out what it was before you close.
- [ ] Read the red days. Each is a count that was off by the limit or more, and it carries the counter's reason. The **Count difference** column totals to the Cash Over/Short account.
- [ ] Check the **Counted?** column. A day not counted is covered by the next count, which takes in every day since the last one. If the last day of the month was not counted, the report says so and the books are not compared with a count for it.
- [ ] Hand the accountant any line in **First count & history**. The store's first count set the books to the drawer once, and a later correction to the days before it is booked to it at the next count. The **Note** column says which entry to hand over.

Every journal entry the drawer books (a deposit, cash from the bank, an expense, a seller payment, a count difference) can only be undone from the [Cash Drawer](../selling/cash-drawer.md) page. Cancelling one from the accounting screens is refused and says where to go instead. Entries marked **Owner took (old entries)** are from before that button was removed.

## 4. Inventory and purchasing

- [ ] Spot-check Stock In Hand on the balance sheet against the FastBound bound book count.
- [ ] Look for zero-cost guns. Run the Sales Report and check the cost flags. A gun that sold with no recorded purchase price needs its cost basis filled in. See [Corrections](../inventory/corrections.md).
- [ ] Hand last month's receipts to the accountant. Run **Inventory Receipts** with Period set to Last Month. The Summary view gives category by receipt class for the purchase and inventory entries. The Units view exports the per-unit detail as CSV. **Revaluation** rows are cost corrections with a unit count of zero, so book them as an inventory adjustment rather than a new purchase. Transfer and Custody rows are hidden unless you tick their Include boxes.
- [ ] Note for the tax preparer: a firearm disposition leaves stock through a material issue that posts to **Stock Adjustment**, so debits on that account arising from firearm dispositions are cost of goods sold, not shrinkage. Non-firearm lines such as ammunition and optics post explicitly to Cost of Goods Sold and are outside this manual reclassification.
- [ ] Check what is owed to consignors. Run the General Ledger on the **Consignment Payable** account, or the Accounts Payable report with **Payable Account** set to it, and compare each consignor's balance with what has been paid out. See [Selling guns on consignment](../inventory/consignment.md#what-the-store-owes-the-consignor).
- [ ] On **Inventory Receipts** for the month, the **Seller not settled** card must read zero. Any receipt named in the red line under the table is a private seller still owed money, or paid on a receipt that was cancelled. A manager records or corrects the payment on the [Cash Drawer](../selling/cash-drawer.md) page.

Receiving stops at Receive Goods. Whether you track distributor purchases as payables is your accountant's choice, and the POS does not age them.

Purchases from private sellers are different. The seller is paid on Receive Goods, in the Trade-in dialog or on the Cash Drawer page, by cash, Zelle, check or ACH.

## 5. Web accessory stock legs

A web order's Sales Invoice does not move stock itself. Its firearms leave through the disposition, and its accessory lines leave through a delivery note raised against the invoice. When that leg is missing the Sales Report still shows a cost, because its cost column comes from the bin valuation and not from the ledger, so the report can never reveal the gap. Before closing:

1. Confirm no web invoice in the month still owes a delivery note.
2. Confirm no open task is outstanding on a Sales Invoice or a web order. These are raised once each for a stock leg that could not be issued, a revenue booking that keeps failing, an order whose only invoice is cancelled, a refund held because someone is holding the stock leg, and an invoice left in draft on a total mismatch. Each one is an unbooked sale or an overstated shelf, and none may be carried into a closed month.
3. A submitted return against a web delivery note puts the goods back at their original valuation on the return's own date. The Sales Report does not net that out, so in a month with such a return expect report cost to exceed ledger cost of goods sold by the returned amount, and reconcile from the ledger.

Backfill a missing delivery note on the Sales Invoice's own date while the period is still open. Once the period is closed, post it on the correction date and say so in the delivery note comment. Never both.

## 6. Tax

- [ ] Sales tax. Total the Sales Tax Payable column on **Sales Register** for the month. That is the tax collected and due. Post the payment journal entry against Sales Tax Payable after you file. Whether an order counts as in-state is decided from the state on the store's own address.
- [ ] Shipping tax. Whether shipping is taxable depends on your state. With **Shipping Price Includes Sales Tax** on in [FFL Settings](../settings/ffl-settings.md) (the default), the shipping price on an order is what the customer paid, and the tax is carved out of it and reported as usual.
- [ ] Web refunds. A refunded web order takes the refunded tax and shipping off the invoice's own tax and shipping lines, so Sales Tax Payable drops by the refunded tax.
- [ ] Spot-check zero-tax invoices. They should all be shipments to out-of-state FFLs. An in-state zero-tax invoice with no exemption certificate needs chasing. **GunBroker orders are exempt from this rule.** Sales Tax Payable is always zero on a GunBroker order, because GunBroker collects and remits as the marketplace provider into its own liability account. Check that account's credits for the month instead.
- [ ] Reconcile the Sales Tax card. The Sales Tax card on the Sales Report's Orders + line detail and Tax lines views is all sales tax charged in the period, including the tax on shipping. It does not include tax a marketplace such as GunBroker collected. It should equal the month's Sales Tax Payable credits from sales invoices, so do not add shipping tax on top. Any difference should be a journal-entry adjustment or an order the Data Flags card has flagged. File from the ledger.

      For example, at an 8.25% rate, a sale of $320.00 of goods with $23.10 of shipping carries $26.40 of tax on the goods and a $1.91 shipping tax row. The card shows $28.31, the same as the Sales Tax Payable credit and as 8.25% of the $343.10 taxable. Adding the shipping tax again would read $30.22 and overstate the tax due by $1.91. The invoice's Total Taxes and Charges, $51.41, is not the tax figure, because it includes the $23.10 of shipping income.
- [ ] Marketplace sales. Open the Sales Report, choose the tax-lines view, filter Channel to GunBroker and read the **Marketplace sales** card (the state in brackets is the store's own). It is the merchandise net plus freight income on orders delivered to your state, and it excludes the marketplace's collected tax and buyer service fee. If your state's return has a line for marketplace sales, this is the figure. Keep GunBroker's tax statement as the provider's proof of collection.
- [ ] For order-by-order review, the tax-lines view carries a **Ship-to State** column and a **Marketplace Exempt** column. The second one separates "zero tax because the buyer holds an exemption" from "zero tax because that state has none".

## 7. Lock the period

When everything above is done, advance the closing date to the end of the month so a filed period cannot be edited. The field is **`accounts_frozen_till_date` on the Company record**, not in Accounts Settings.

!!! warning "Locking changes what refunds can do"
    Once a period is frozen, a WooCommerce refund on an order inside it cannot reconcile: the amendment re-posts the original invoice's reversal on its own date and the freeze refuses it, so the job fails and leaves an error log. Unfreeze, run the refund reconciliation, then freeze again. A backfilled delivery note dated into a frozen period fails the same way, which is why the rule in step 5 sends it to the correction date instead.

## 8. The GunBroker channel

Five accounts carry the marketplace's money through the books:

| Account | What it holds | How it clears |
|---|---|---|
| GunBroker MPF Sales Tax Payable | Tax GunBroker collected for you as marketplace provider | Against the tax statement. Remit when GunBroker's statement says to, by journal entry. |
| GunBroker Marketplace Fee Payable | The buyer service fee GunBroker charges the buyer through you | Against whichever statement carries it. The report lists it separately from the collected tax for that reason. |
| Accrued GunBroker Fees | Final value fees and FFL document fees you owe, accrued per item | Against GunBroker's fee bill. Clear by journal entry. |
| Marketplace Fees - GunBroker | The expense side of that accrual | A profit and loss expense, never cost of goods. The same gun must cost the same in either channel. |
| GunBroker Clearing | The card channel's clearing account | Zero while the store takes checks only. |

Settle these with journal entries, not purchase invoices. There is no payable to age.

Renaming any of these accounts in the chart of accounts silently breaks the Sales Report. It matches on the bare account name, and a renamed account sends those tax lines back to unclassified. The report still runs and the data flags turn red. Rename only with a developer, who has to change the matching list at the same time.

The books accrue, and a return may be filed on the cash basis. Seven items bridge the two, and the **GunBroker Cash Basis Adjustments** report is the schedule. Give the accountant the report with the preamble printed at the top of it. A GunBroker order is always paid before it is invoiced, and the receipt posts on the invoice date, never on the deposit date. Without that, reconciling the bank and reading the invoices counts the same check twice.

The daily GunBroker audit raises eight kinds of alert. All of them should be clear before you close. See [Background jobs](../reference/background-jobs.md) and [GunBroker](../channels/gunbroker.md).

## GunBroker refunds and chargebacks

**The POS deliberately offers no refund button for a GunBroker sale.** There is no control on any page and no interface behind one. Refund on the GunBroker side first, using their refund or return flow, and mirror it here afterwards.

The reason is sales tax. GunBroker files it under its own license as the marketplace provider. Only a refund run through their flow makes their statement and their state filing go down. A refund you issue yourself leaves you having refunded the buyer's tax while GunBroker still reports the original amount, and the store absorbs the difference.

Three rules govern the entries afterwards:

1. **Refund on GunBroker first, mirror here second.**
2. **A payment entry posts on the invoice date, never back on the deposit date.** A GunBroker order is always paid before it is invoiced, so "the buyer paid" does not mean "there is a payment entry to reverse". The payment entry only exists once the Sales Invoice exists.
3. **Every hand-built refund payment entry needs the order number in its remarks.** Tick **Custom Remarks** first, then type `GunBroker Order #<number>`, one order number per payment entry. The Cash Basis Adjustments report recognizes a refund by that mark. Miss it and the order stays on the paid-but-not-disposed list forever, and the year's gross receipts are overstated. Put two order numbers in one remark and both orders are counted at full value, and both are understated.

!!! warning "Tick Custom Remarks before you type"
    Remarks is read-only until Custom Remarks is ticked, and saving without it makes the system rewrite the whole remark from the amount and the party. Your order number disappears with no error and no warning, and the cash basis schedule never sees that refund again.

Cancelling a Sales Invoice unlinks its payment entry rather than voiding it, so the default is to cancel the invoice only and leave the payment alone. The one case that starts by cancelling the payment entry is a bounced or forged check, where the money never arrived at all.

A whole-gun return also needs a bound book acquisition from the receiving FFL at the original purchase cost. Without it the book says the gun is still with someone else while it is sitting in your safe.

Book a chargeback as a loss against the clearing account. Once you have dealt with it, fill in **Chargeback Handled At** on the GunBroker Order or the daily audit reports it again every day.
