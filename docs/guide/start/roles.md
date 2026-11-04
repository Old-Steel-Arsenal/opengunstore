# Roles and sign-in

For managers setting up a new person's login, and for anyone told "you do not have permission". Your login carries one or more roles, and the roles decide which pages open and which buttons appear.

## Signing in

Every person gets their own login: an email address and a password that a manager sets up. Never share a login. The system records who rang each sale, who received each gun and who attested each 4473, and a shared login destroys that trail.

The shared **Administrator** login never receives the bell's notices, which is one more reason to give everyone their own.

On a shared counter screen, **Switch user** in the sidebar's foot lists everyone with a counter role. Pick yourself and type your password, or your counter PIN once you have one (**Set my PIN** in the same dialog).

After a few idle minutes the screen shows **Screen locked**. The person signed in picks themselves to carry on, and anyone else signs in as themselves. The wait is **Lock Idle Screen After (minutes)** in [FFL Settings](../admin/ffl-settings.md): 5 by default, 0 for never.

## The roles at a glance

| Role | Typical person | What they can open |
|---|---|---|
| **Sales User** | Counter clerk | Dashboard, Point of Sale, Create Order, Pending Order, Cash Drawer, Storage Locations, trade-ins, the 4473 flow, 4473 records, Pending 4473 Orders, Pending Transfer Pickups |
| **Sales Manager** | Shift lead, owner | Everything Sales User opens, plus Sales Report, Cash Drawer Weekly, Hand-Asserted 4473 Dispositions, and the manager-only buttons below |
| **Stock User** | Receiving helper | Dashboard, Receive Goods, Upload Photos by Serial, Inventory Count, Storage Locations, Firearms In Stock, the compliance queues |
| **Stock Manager** | Inventory lead | Everything Stock User has, plus Create Order, Pending Order, Cash Drawer, trade-ins, Distributors, Distributor Orders, finishing a count |
| **Purchase User / Purchase Manager** | Buyer | Receive Goods, Distributors, Distributor Orders |
| **Item Manager** | Merchandiser | Upload Photos by Serial |
| **Accounts User / Accounts Manager** | Bookkeeper, CPA | Inventory Receipts, Cash Drawer Weekly, Sales Report and the GunBroker reports (Accounts Manager), Dashboard and Pending 4473 Orders (Accounts User) |
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

The register (Point of Sale) has its own role list, without Stock Manager or System Manager. Anyone who works the register needs **Sales User** or **Sales Manager** on top of their other roles. A **Sales Manager** also needs **Sales User** to ring sales and take trade-ins; the standard role profiles always pair them.

## What a clerk can do on the register

A login with **Sales User** (or Sales Manager with Sales User) runs the register start to finish without any Accounts role:

- open the shift, sell, take payment, print the receipt, return a sale and close the shift;
- delete a draft order with **Delete Order**. Deleting a cancelled invoice is for a Sales Manager or System Manager.

A clerk cannot cancel or amend a completed sale or a closing, delete a posted sale, or record payments or journal lines in the accounts. Undoing a completed sale is a return, not a cancellation.

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

A counter login can run the 4473 and transfer-pickup reports. The bound-book disposition list stays closed to Sales User and Sales Manager.

## Stock Manager cannot run a 4473

A Stock Manager can build and complete counter orders and see the compliance queues. Running or recording a Form 4473 is narrower: only **Sales User**, **Sales Manager** and **System Manager** may do it.

!!! warning "Federal recordkeeping"
    A 4473 is a transaction with a buyer. Stock Manager receives and books inventory but never transacts with a buyer, so it is left out of the 4473 group. The same list guards the counter button and the bound-book disposition behind it, so no other screen gets around it.

## Opening a page is not always booking on it

- **Receive Goods** opens for Stock User and the purchase roles, but booking a receipt needs **Stock Manager** or **System Manager**.
- **Inventory Count** lets a Stock User scan. Finishing the count and correcting the stock needs **Stock Manager** or **System Manager**.
- **Storage Locations** lets every counter and stock login look at the map and move things. Creating zones, adding or removing slots and editing the floor plan need **Stock Manager** or **System Manager**.
- A **trade-in** can be taken by **Sales User**, **Stock Manager** and **System Manager**. A **Sales Manager** login also needs **Sales User**. See [Trade-ins](../counter/trade-ins.md).

## Manager-only actions

These buttons check for **Sales Manager** or **System Manager** when you click them, on any page:

- **Card refund** (**Void**, **Refund**, **Resolve card reversal**), **Charge saved card** and **Remove** on a saved card. See [Card voids and refunds](../manager/card-refunds.md).
- **Void Card Charge**, **Unlock Card Refund** and **Confirm Portal Refund** on a card payment. See [Card voids and refunds](../manager/card-refunds.md).
- Memberships: creating and changing plans, **Needs attention**, **Retry card**, **Look up card charge** and **Cancel**. Selling a membership and **Take payment** are for counter staff. See [Memberships](../counter/memberships.md).
- **Manager Override 4473** on a parked firearm order, and **Redo 4473** on an order whose 4473 is complete. See [Form 4473 and NICS](../compliance/4473.md).
- On the [Cash drawer](../counter/cash-drawer.md) page: **Undo** on a cash entry, **Undo the last count**, **Change method** on a seller payment, and **Record payment** or **Paid outside the system** for a private seller nobody has paid yet. Changing the payment on a gun bought the old way is **System Manager** only.
- **Merge into…** on a customer. See [Customers](../counter/customers.md).
- Ringing up accessories that a paid web order is waiting on. A clerk is refused; a manager can ring the sale and the invoice notes it. See [Sell accessories](../counter/sell-accessories.md).
- Approving a lower price or a return, while FFL Settings asks for it. A manager picks themselves in the **Manager approval needed** dialog and types their counter PIN (or password) and a reason. A manager signed in is never asked. See [Sell accessories](../counter/sell-accessories.md#lower-a-price).

Closing an open register shift by hand, outside the register, is **Sales Manager**, **Stock Manager** or **System Manager**. See [A day at the counter](day-at-the-counter.md).

Marking a bound-book record as one that never belongs in the bound book is **Stock Manager** or **System Manager**, and has no button. See [FastBound](../admin/fastbound.md).
