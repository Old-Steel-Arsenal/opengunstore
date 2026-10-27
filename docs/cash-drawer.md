# Cash drawer: daily count, cash entries, log, weekly report

For staff: **Gun Shop → Cash Drawer**. For the accountant: **Cash Drawer Weekly** (menu → Export to get the CSV).
The rules are the accounting treatment below and the store owner's decisions. Code: `ffl_core/cash_drawer.py`.

## What the drawer should hold

The books carry one cash account per company (`Cash - <abbr>`); the drawer's expected amount is that account's balance
(cancelled entries excluded) plus the cash of POS invoices that are submitted but not merged yet, and of draft POS
invoices where Take Payment took cash (a part payment, or paid before the 4473 clears; dated by the server time Take
Payment stamped, listed as "Cash paid on an open order"). ERPNext's own closing
figure is not used for cash. There is no safe in the store and the company reserve kept elsewhere is out of scope: the page
counts the drawer only, and the counted cash is the next opening.

## What staff do (one button, one confirmation)

| Button | Who | Books |
|---|---|---|
| Close the day | counter | Count the drawer. Closes the open POS shifts. A difference is booked `CASH-VARIANCE` (Cash Over/Short against Cash). From **$20** (setting) a written reason is needed and the weekly report flags the day; the close is never blocked. |
| Deposit to bank | counter | `CASH-DEPOSIT` Dr Operating Bank / Cr Cash |
| Cash expense | counter | `CASH-EXPENSE` Dr the expense account / Cr Cash. Memo required; receipt photo optional (when attached: uploaded by you a moment ago, attached to nothing else, kept private). **$200** cap (setting), accounts from the list in Cash Drawer Settings (default: Print and Stationery, Office Maintenance, Freight, Postal, Meals, Entertainment, Miscellaneous Expenses; Meals is its own account because meals are 50% deductible). Sales tax paid is part of the expense. |
| Add cash from bank | counter | `CASH-FROMBANK` Dr Cash / Cr Operating Bank (cash taken out of the bank to fill the drawer); reference optional. Every one is in the Log with who and when. |
| Trade-in dialog (POS) | stock | Applied as credit: nothing is paid here (the register ticket carries it; a ticket below zero pays the difference, `CASH-TRADEIN`). Not applied as credit: the store buys the gun now. Pay method Cash / Zelle / Check / ACH is required, plus its reference unless Cash. The gun is received on a Purchase Receipt from the seller, dated today, Source *Trade-in* (the acquisition stays a Trade-in), and the payment is booked with it: `CASH-GOODS` Dr Stock Received But Not Billed / Cr Cash or Operating Bank, the same as a private-seller purchase on Receive Goods. A cost correction (Correct Cost dialog) books the difference (Payout Adjustment, Dr/Cr Stock Adjustment against Cash or the bank, linked to the receipt) only when you tick *The amount actually paid to the seller was different*; otherwise only the cost changes. A purchase is either paid out or spent as a credit on a sale, never both. Seller payments made the old way (`CASH-PAYOUT`, Dr Stock Adjustment) stay as they are: a System Manager can still undo one, book it again or change its method (Change method), and cost corrections still follow it; no new one is made. |

Managers can **Undo** an entry (until a count has been taken after it) and the latest count; only a System Manager undoes a seller payment (its cost corrections go with it).

## First count and missed nights

* The first counted close of a company sets the books to the physical drawer once: `CASH-CUTOFF` Dr/Cr Operating Bank against
  Cash, never Cash Over/Short. History before that is handled in QuickBooks by the accountant (no backfill).
* The 23:00 job still closes and merges the POS shifts, but leaves an **uncounted** day (no entry booked). The next count
  absorbs every uncounted day since the last count and says how many it covered.
* After the first count nobody should post to `Cash - <abbr>` by hand: such lines show up under *Unclassified* in the report.

## Accounts (created per company from its abbr by the migrate step)

`Cash Over/Short - X` (Expense, under Indirect Expenses) and
`Operating Bank - X` (Bank, under Bank Accounts; also set as the company's default bank account when that is empty). The bank is, in order: the company's usable default bank account (never a Payroc / Woo / GunBroker clearing account), the account its Zelle / ACH mode of payment settles to, an existing Operating Bank, else Operating Bank is created by the first cash entry. A migrate on a new site makes none, so `store_setup` does not end up with a second bank.
None of the cash entries touches Sales, Sales Tax Payable, Debtors, COGS or Stock In Hand. No Form 8300 flag.

The page lists today's cash lines (or everything since the last close, whichever is later) with a count of older lines; seller payments by bank are listed apart and are not in the drawer.

**Owner draws** were removed on 2026-10-02 (owner decision): cash an owner takes is not recorded from the page. Old
`CASH-OWNER` entries still show (as "Owner took cash (old entry)", with who took it) and can still be undone; the weekly
report shows their column only for a range that has one. The `Due from Owner` account is no longer created.

## Log (tab on the Cash Drawer page)

Every cash movement in a date range (default the last 7 days, at most a year), oldest first: time, what, details, who
entered it, the document, in / out and the drawer balance after it, from the opening balance (books before the range) to
the closing one; CSV export. Register sales are listed one POS invoice each, dated by the invoice itself, before and after
the nightly merge alike (the merged invoice is dated like the shift's last sale, so a shift that crossed midnight would
otherwise move). A line entered on another day than it is dated for shows "entered <date>". Counter roles and Accounts
User / Manager can read it; it is also the MCP tool `cash_drawer_log` (on the cpa surface).

## Cancelling a drawer journal entry

A journal entry the cash drawer made (stamped `ffl_cash_drawer_source` with its Cash Drawer Entry or Close, which must point back to it) is cancelled only through the Cash Drawer page's own paths: Undo on the entry, a System Manager's fix of a seller payment, undoing the count, or cancelling a trade-in register ticket (its payout goes with it). Desk, imports, scripts and the MCP cancel tool are refused, and so are Desk's *Reverse Journal Entry* of one and an *Amend* of one the page cancelled (`cash_drawer.refuse_drawer_je_cancel` / `refuse_drawer_je_copies`). The refusal names the way that works; a close that can no longer be undone goes to the accountant. A leftover whose entry was already undone, or whose count was already taken back, can be cancelled anywhere. A correction recipe in bench console that really must cancel one sets `je.flags.cash_drawer_cancel = True` before `je.cancel()` -- and then fixes the Cash Drawer Entry's status by hand.
