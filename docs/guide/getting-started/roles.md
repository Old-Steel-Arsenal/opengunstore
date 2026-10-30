# Who can do what

Your login carries one or more roles, and the roles decide which pages open and
which buttons appear. Managers assign them. This page is the reference for
deciding what a new person needs.

## The roles at a glance

| Role | Typical person | What they can open |
|---|---|---|
| **Sales User** | Counter clerk | Dashboard, Point of Sale, Create Order, Pending Order, Cash Drawer, Storage Locations, trade-ins, the 4473 flow, 4473 records, Pending 4473 Orders, Pending Transfer Pickups |
| **Sales Manager** | Shift lead, owner | Everything Sales User opens, plus Sales Report, Cash Drawer Weekly, Hand-Asserted 4473 Dispositions, and the manager-only buttons listed below |
| **Stock User** | Receiving helper | Dashboard, Receive Goods, Upload Photos by Serial, Inventory Count, Storage Locations, Firearms In Stock, and the compliance queues |
| **Stock Manager** | Inventory lead | Everything Stock User has, plus Create Order, Pending Order, Cash Drawer, trade-ins, Distributors, Distributor Orders, and finishing a count |
| **Purchase User / Purchase Manager** | Buyer | Receive Goods, Distributors, Distributor Orders |
| **Item Manager** | Merchandiser | Upload Photos by Serial |
| **Accounts User / Accounts Manager** | Bookkeeper, CPA | Inventory Receipts, Cash Drawer Weekly, Sales Report (Accounts Manager), the GunBroker reports (Accounts Manager), Dashboard and Pending 4473 Orders (Accounts User) |
| **System Manager** | Owner, administrator | Everything, including FFL Settings, Cash Drawer Settings, MCP Settings, the Payments hub, Extensions and every integration's settings |


## What each page requires

| Page | Roles |
|---|---|
| Dashboard | Sales User, Sales Manager, Stock User, Stock Manager, Accounts User, System Manager |
| Point of Sale | Sales User, Sales Manager, Accounts User, Accounts Manager |
| Create Order | Sales User, Sales Manager, Stock Manager, System Manager |
| Pending Order (**Orders** in the sidebar) | Sales User, Sales Manager, Stock Manager, System Manager |
| Cash Drawer | Sales User, Sales Manager, Stock Manager, System Manager |
| Memberships | Sales User, Sales Manager, Stock Manager, System Manager, Accounts User, Accounts Manager |
| Receive Goods | Stock User, Stock Manager, Purchase User, Purchase Manager, System Manager |
| Upload Photos by Serial | Stock User, Stock Manager, Item Manager, System Manager |
| Inventory Count | Stock User, Stock Manager, System Manager |
| Storage Locations | Sales User, Sales Manager, Stock User, Stock Manager, System Manager |
| Distributors, Distributor Orders | Purchase User, Purchase Manager, Stock Manager, System Manager |
| Payments hub, Extensions | System Manager |

Point of Sale is the one page in that table this system did not write. It is
ERPNext's own register and carries ERPNext's own role list, which is why Stock
Manager and System Manager are absent from it: a login that has to open the
register needs **Sales User** or **Sales Manager** on top of whatever else it
holds. Accounts User and Accounts Manager are on that list for the same reason —
ERPNext put them there, not because bookkeeping needs the register.

### What a counter clerk can do on the register

A login with only **Sales User** (plus Stock User for stock counts), or
**Sales Manager** together with **Sales User**, can run the register from start
to finish without any Accounts role:

- open the shift, sell and take payment, print the receipt, return a sale
  and close the shift (the closing posts the day's sales);
- delete a draft order with **Delete Order**. Deleting a cancelled invoice is
  for a Sales Manager or System Manager.

ERPNext's own permissions stop short of this, so the system adds the missing
ones when it is installed or updated. They cover only the register:
creating and submitting POS Invoices, POS Opening Entries and POS Closing
Entries, and, because posting a closing creates the day's Sales Invoice under
the person who closed, creating and submitting that Sales Invoice.

A clerk still cannot cancel or amend a submitted sale or a closing, delete a
posted sale, or create a Journal Entry or Payment Entry, and has no access to
the ledger. Undoing a posted sale is a return, not a cancellation.

A **Sales Manager** also needs **Sales User** to run the register, the same
rule as for a trade-in: ERPNext gives Sales Manager no access to items, accounts
or serial-number records, and this system does not copy those to it. The
standard role profiles always pair them.

## What each report requires

| Report | Roles |
|---|---|
| Firearms In Stock | Stock User, Stock Manager, System Manager |
| Pending 4473 Orders | Sales User, Stock User, Stock Manager, Accounts User, System Manager |
| Pending Transfer Pickups | Sales User, Stock User, Stock Manager, System Manager |
| Form 4473 Records (**4473 records**) | Sales User, Sales Manager, System Manager |
| Sales Report | Sales Manager, Accounts Manager, System Manager |
| Cash Drawer Weekly | Sales Manager, Accounts User, Accounts Manager, System Manager |
| Inventory Receipts | Stock Manager, Purchase Manager, Accounts User, Accounts Manager, System Manager |
| Hand-Asserted 4473 Dispositions | Sales Manager, System Manager |
| GunBroker Cash Basis Adjustments, GunBroker Statement Reconciliation | Accounts Manager, System Manager |

A counter login can run the 4473 and transfer-pickup reports because Sales User
also holds report access on the register's invoices and on the bound-book
acquisitions. The bound-book disposition list stays closed to Sales User and
Sales Manager.

## Rules worth knowing

### Stock Manager cannot run a 4473

Stock Manager sits inside the general counter group, so a Stock Manager can
build and complete counter orders and see the compliance queues. Running or
recording a Form 4473 is narrower on purpose: only **Sales User**, **Sales
Manager** and **System Manager** may do it.

!!! warning "Federal recordkeeping"
    A 4473 is a transaction with a buyer. Stock Manager receives and books
    inventory but never transacts with one, so that role is deliberately left
    out of the 4473 group. The same list guards the button at the counter and
    the bound-book disposition behind it, so the restriction cannot be walked
    around from another screen.

### Opening a page is not always booking on it

Some pages open for more roles than may finish the job on them:

- **Receive Goods** opens for Stock User and the purchase roles, but booking a
  receipt needs **Stock Manager** or **System Manager**.
- **Inventory Count** lets a Stock User scan. Finishing the count and correcting
  the stock needs **Stock Manager** or **System Manager**.
- **Storage Locations** lets every counter and stock login look at the store map
  and move things. Creating zones, adding or removing slots and editing the
  floor plan need **Stock Manager** or **System Manager**.
- A **trade-in** can be taken by **Sales User**, **Stock Manager** and
  **System Manager**. A **Sales Manager** login also needs **Sales User**. See
  [Trade-ins](../inventory/trade-ins.md).

## Manager-only actions

Some buttons check for **Sales Manager** or **System Manager** at the moment you
click them, regardless of which page you are on:

- **Card refund** (**Void**, **Refund**, **Resolve card reversal**), **Charge saved
  card** and **Remove** on a saved card, on a counter order's Sales Invoice and the
  customer. See [Taking payment](../selling/payments.md).
- On the [Memberships](../selling/memberships.md) page: creating and changing
  plans, **Needs attention**, **Retry card**, **Look up card charge** and
  **Cancel**. Selling a membership and **Take payment** are for counter staff.
- **Manager Override 4473** on a parked firearm order, whether its 4473 is
  pending or was done straight in the bound book. See
  [Selling a firearm](../selling/firearm-sale.md#manager-override).
- **Redo 4473** on an order whose 4473 is already complete.
- **Void Card Charge**, **Unlock Card Refund** and **Confirm Portal Refund** on
  a card payment. See [Taking payment](../selling/payments.md).
- On the [Cash Drawer](../selling/cash-drawer.md) page: **Undo** on a cash entry,
  **Undo the last count**, **Change method** on a seller payment for received
  goods, and **Record payment** or **Paid outside the system** for a private
  seller nobody has paid yet. Changing the payment on a gun bought the old way
  is **System Manager** only.
- **Merge into…** on a customer. See [Customers](../selling/customers.md).
- Ringing up accessories that a paid web order is waiting on. A counter clerk is
  refused; a manager can ring the sale and the invoice notes it. See
  [Point of Sale](../selling/pos.md).
- **Approving a lower price or a return.** While FFL Settings asks for it, a
  price a clerk lowers below the list price, an invoice discount, or a return,
  saves only once a
  **Sales Manager** or **System Manager** picks themselves in the
  **Manager approval needed** dialog and types their counter PIN (or password)
  and a reason. A manager signed in is never asked. See
  [Point of Sale](../selling/pos.md#lowering-a-price).

Closing an open register shift by hand outside the register is **Sales
Manager**, **Stock Manager** or **System Manager**. See
[Opening and closing the day](../selling/day-end.md).

Marking a bound-book record as one that never belongs in the bound book is
**Stock Manager** or **System Manager**, and has no button. See
[FastBound](../compliance/fastbound.md).
