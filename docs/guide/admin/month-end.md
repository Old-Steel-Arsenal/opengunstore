# Month-end close

For the bookkeeper, after the last business day of the month: the checklist to work through, in order, before the month is locked.

Every report named here opens from [Reports](../manager/reports.md). Why the accounts behave as they do, with the worked examples, is on [For the bookkeeper](bookkeeper.md).

The POS is the business system and the source of the numbers. The Sales Report, Inventory Receipts, the sales-tax liability and Stock Balance are what you carry into the outside books. Your accountant decides which system is the book of record; this page covers what to check in the POS.

## 1. Sales completeness

- [ ] Run **Sales Report** for the month and compare its total with **Sales Register**. They should agree. A gap is register sales not yet consolidated, which the 15-minute sweep retries. See [What runs automatically](background-jobs.md).
- [ ] Reconcile the web store. Total the Web-channel invoices and compare with completed WooCommerce orders for the month. Accessory-only orders, which never touch the counter, and orders not yet disposed on the last day are normal differences.
- [ ] Match every return invoice for the month with a WooCommerce refund.
- [ ] Reconcile GunBroker. Filter the Sales Report to the GunBroker channel and compare with the marketplace's month. Most of the difference is timing: GunBroker records the sale date, the POS the disposition date.
- [ ] Work GunBroker order by order in **GunBroker Statement Reconciliation**. It sorts each line into an amount difference, a timing difference or a missing record.
- [ ] Read the **Accrual** column on that report: none, posted, cancelled or reversed. A matching amount whose accrual is not **posted** is the real problem.
- [ ] Run **GunBroker Cash Basis Adjustments** with the period end on the last day of the month, and read item 3, paid but not disposed. Those orders correctly post nothing this month.

To run **GunBroker Statement Reconciliation**, choose the statement (Fee Statement or Sales Tax Statement) and its period, then attach the CSV or paste the table into the text box. It compares order by order, never monthly totals.

**GunBroker Cash Basis Adjustments** takes a company and a period. Every figure is recomputed as of the period end, so the same period end always gives the same schedule. Hand it to the accountant with the preamble printed at the top.

## 2. Clearing accounts

Every clearing account must reach zero or be explained.

| Account | The balance is | Clear it against |
|---|---|---|
| Payroc Card Clearing | Card takings not yet in the bank | Bank deposits. The gap is processor fees |
| Woo Clearing | Web takings not yet settled | The WooCommerce settlement report |
| GunBroker Clearing | Card channel only | Zero while the store takes checks and money orders |

- [ ] Resolve every **Payroc: … card charge(s) no longer captured at the gateway** alert from the month. Each charge voided or refunded outside the POS also has a comment on its invoice. See [Card voids and refunds](../manager/card-refunds.md).
- [ ] Check the card payments ledger. It lists every Payroc payment and refund for a date range, counter and web, matched to what the POS recorded, and flags a payment the POS never recorded, a different amount, or a charge voided or refunded in the Payroc portal.
- [ ] Tie out GunBroker checks in transit against the sum of the **Banked Amount** column on item 3 of the Cash Basis Adjustments report. Do not use the sum of order totals, and do not net refunds out of it. See [For the bookkeeper](bookkeeper.md#gunbroker-checks-in-transit).

The card payments ledger has no report screen. Ask for it through an MCP connector (the `payroc_transactions` tool), at most 31 days at a time. It needs **Accounts User**, **Accounts Manager** or **System Manager**. Cards keyed on the Virtual Terminal are labeled as such. See [The MCP server](../../ai/mcp.md).

## 3. Cash drawer

- [ ] Run **Cash Drawer Weekly** with **From** on the first of the month and **To** on the last. It takes any range up to 400 days.
- [ ] Read the lines at the foot. The **Difference** under **Daily lines add up to** and **Cash ledger movement** must read 0.00, and **Unclassified ledger lines** must read **None**.
- [ ] Find out what any unclassified line was before you close. It is a hand entry to the Cash account.
- [ ] Read the red days. Each is a count off by the limit or more, with the counter's reason. The **Count difference** column totals to the Cash Over/Short account.
- [ ] Check the **Counted?** column. A day not counted is covered by the next count, which takes in every day since the last one. If the month's last day was not counted, the report says so.
- [ ] Hand the accountant any line in **First count & history**. The **Note** column says which entry to hand over.

Every entry the drawer books can be undone only from the [Cash drawer](../counter/cash-drawer.md) page. See [For the bookkeeper](bookkeeper.md#cash-drawer-entries).

## 4. Inventory and purchasing

- [ ] Spot-check Stock In Hand on the balance sheet against the FastBound bound book count.
- [ ] Look for zero-cost guns. Run the Sales Report and check the cost flags. A gun that sold with no purchase price needs its cost filled in. See [Fix a mistake](../stock/corrections.md).
- [ ] Run **Inventory Receipts** with Period set to Last Month. The Summary view gives category by receipt class; the Units view exports per-unit detail as CSV. Hand both to the accountant.
- [ ] Book **Revaluation** rows as an inventory adjustment, not a purchase. They are cost corrections with a unit count of zero. Transfer and Custody rows stay hidden unless you tick their Include boxes.
- [ ] Tell the tax preparer how firearm dispositions post. See [For the bookkeeper](bookkeeper.md#firearm-dispositions-and-stock-adjustment).
- [ ] Check what is owed to consignors. Run the General Ledger on the **Consignment Payable** account, or Accounts Payable with **Payable Account** set to it. Compare each consignor's balance with what has been paid out. See [Consignment](../stock/consignment.md).
- [ ] On Inventory Receipts for the month, the **Seller not settled** card must read zero. A receipt named in the red line is a private seller still owed money, or paid on a cancelled receipt. A manager records or corrects the payment on the [Cash drawer](../counter/cash-drawer.md) page.

Receiving stops at Receive Goods. Whether you track distributor purchases as payables is your accountant's choice; the POS does not age them.

Private sellers are paid on Receive Goods, in the Trade-in dialog or on the Cash Drawer page, by cash, Zelle, check or ACH.

## 5. Web accessory stock legs

- [ ] Confirm no web invoice in the month still owes a delivery note.
- [ ] Confirm no open task is outstanding on a Sales Invoice or a web order. Each one is an unbooked sale or an overstated shelf, and none may be carried into a closed month.
- [ ] In a month with a return against a web delivery note, expect report cost to exceed ledger cost of goods sold by the returned amount. Reconcile from the ledger.

Backfill a missing delivery note on the Sales Invoice's own date while the period is still open. Once the period is closed, post it on the correction date and say so in the delivery note's comment. Never both. See [For the bookkeeper](bookkeeper.md#web-accessory-stock-legs).

## 6. Tax

- [ ] Total the Sales Tax Payable column on **Sales Register** for the month. That is the tax collected and due. Post the payment journal entry against Sales Tax Payable after you file.
- [ ] Check shipping tax against your state's rule. See **Shipping Price Includes Sales Tax** in [FFL Settings](ffl-settings.md).
- [ ] Spot-check zero-tax invoices. They should all be shipments to out-of-state FFLs. An in-state zero-tax invoice with no exemption certificate needs chasing. GunBroker orders are the exception: check the marketplace tax account instead.
- [ ] Reconcile the Sales Report's **Sales Tax** card with the month's Sales Tax Payable credits from sales invoices. Do not add shipping tax on top. A difference should be a journal entry or a row the **Data Flags** card flags. File from the ledger.
- [ ] Marketplace sales. In the Sales Report's **Tax lines** view filtered to GunBroker, read the **Marketplace sales** card. If your state's return has a line for marketplace sales, this is the figure. Keep GunBroker's tax statement as the provider's proof of collection.
- [ ] For order-by-order review, use the **Ship-to State** and **Marketplace Exempt** columns of the Tax lines view. The second separates "zero because the buyer is exempt" from "zero because that state has no tax".

Whether an order counts as in-state is decided from the state on the store's own address. The worked example and the refund rules are on [For the bookkeeper](bookkeeper.md#sales-tax-and-shipping-tax).

## 7. Lock the period

When everything above is done, advance the closing date to the end of the month so a filed period cannot be edited. The field is `accounts_frozen_till_date` on the Company record, not in Accounts Settings.

!!! warning "Locking changes what refunds can do"
    A WooCommerce refund on an order inside a frozen period cannot reconcile, and the job fails with an error log. Unfreeze, run the refund reconciliation, then freeze again.

    A backfilled delivery note dated into a frozen period fails the same way. That is why step 5 sends it to the correction date.

## 8. The GunBroker channel

- [ ] Clear the GunBroker accounts against their statements and bills, by journal entry. There is no payable to age. The accounts are listed on [For the bookkeeper](bookkeeper.md#the-gunbroker-accounts).
- [ ] Give the accountant the **GunBroker Cash Basis Adjustments** report with its preamble, if the return is filed on the cash basis.
- [ ] Clear every alert from the daily GunBroker audit (eight kinds). See [What runs automatically](background-jobs.md) and [GunBroker orders](../orders/gunbroker-orders.md).

!!! danger "Never rename the GunBroker accounts on your own"
    The Sales Report matches these accounts by name. Renaming one sends its tax lines back to unclassified: the report still runs and the data flags turn red. Rename only with a developer, who changes the matching list at the same time.

## GunBroker refunds and chargebacks

The POS has no refund button for a GunBroker sale. GunBroker files the sales tax under its own license, and only a refund through their flow lowers their statement and their state filing.

1. Refund on GunBroker first, with their refund or return flow.
2. Mirror it in the POS afterwards. A payment entry posts on the invoice date, never back on the deposit date. It exists only once the Sales Invoice exists.
3. On each hand-built refund payment entry, tick **Custom Remarks** first.
4. Type `GunBroker Order #<number>` in the remarks, one order number per payment entry.

!!! warning "Tick Custom Remarks before you type"
    Remarks is read-only until Custom Remarks is ticked. Saving without it rewrites the whole remark from the amount and the party, and your order number disappears with no warning.

    The Cash Basis Adjustments report recognizes a refund only by that mark. Without it the order stays on the paid-but-not-disposed list and the year's gross receipts are overstated. Put two order numbers in one remark and both orders are counted at full value, and both are understated.

- Cancel the invoice only. Cancelling a Sales Invoice unlinks its payment entry rather than voiding it, so leave the payment alone. The one exception is a bounced or forged check, where the money never arrived: start by cancelling the payment entry.
- A whole-gun return also needs a bound book acquisition from the receiving FFL at the original purchase cost. Without it the book says the gun is still with someone else.
- **A chargeback** is booked as a loss against the clearing account. Then fill in **Chargeback Handled At** on the GunBroker Order, or the daily audit reports it again every day.
