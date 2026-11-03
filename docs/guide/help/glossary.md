# Glossary

For a new hire in their first week, whenever a word on screen or in this guide is unfamiliar. Terms are in alphabetical order.

**4473.** ATF Form 4473, the Firearms Transaction Record. A buyer fills one in for every firearm they take away from a licensed dealer, and the dealer keeps it.

A firearm sale cannot complete until the order's 4473 status is Completed and the NICS status is Proceed. See [Form 4473 and NICS](../compliance/4473.md).

**4473 record.** The store's own permanent row for each completed Form 4473, electronic or paper, with the outcome and, for an electronic form, a private copy of the signed PDF.

It cannot be deleted, and it survives the order being deleted or the form being redone. The **Form 4473 Records** report lists them. See [Find a 4473](../compliance/4473-records.md).

**A&D, bound book.** The Acquisition and Disposition record every licensee must keep: what firearms came in, from whom, and what went out, to whom. It is the federal record of each gun's chain of custody through the store.

In this system it lives in [FastBound](../admin/fastbound.md), and the system writes to it automatically. See [The bound book](../compliance/bound-book.md).

**ACH.** Automated Clearing House, the American bank-to-bank transfer network. Here it is a way to pay by manual bank transfer, alongside Zelle, check and wire. There is no gateway behind it: someone confirms the money arrived and records it.

**Acquisition.** A firearm entering your inventory, and the bound book entry that records it. Every gun that comes through the door creates one: bought from a distributor, taken in trade or shipped in for a customer transfer. See [Receive goods](../stock/receive-goods.md).

**Antique.** A firearm made before 1899, marked **Antique** on its item. It is serialized, but it is never entered in the bound book and needs no 4473 at sale.

A record booked for an antique by mistake can be marked by a manager as not in the bound book, so it stops being retried. See [FastBound](../admin/fastbound.md).

**Approval code.** The code the card terminal or the Payroc Virtual Terminal gives for an approved charge. A card already run on the Virtual Terminal is recorded with **Already charged on Virtual Terminal** and its approval code, instead of being charged again. See [Take payment](../counter/payments.md).

**Brady date.** The third business day after a background check was started. If NICS is still Delayed past that date, federal law permits the dealer to transfer at their own discretion.

The **Pending 4473 Orders** report prints it as **Brady Day 3** and highlights a Delayed row once it has passed. See [Daily checks](../compliance/daily-checks.md).

**C&R, Type 03.** A Curio and Relic collector's license. A Type 03 holder is a licensee, so a collectible firearm may ship straight to their licensed address with no dealer at the far end.

The license number's fourth group is `03`, as in `1-23-456-03-7C-89012`. The ATF's public directory does not list Type 03 collectors, so a person reads the license copy instead. See [Verify an FFL](../compliance/ffl-verification.md).

**Card charge check.** A daily job that reads the last week's card charges back from Payroc and emails the managers about any voided or refunded outside the POS. See [Card voids and refunds](../manager/card-refunds.md).

**Cash drawer.** The page where the day's cash is counted and closed, and where every cash movement outside a sale is recorded. Terms on it:

- **Expected** cash is what the drawer should hold according to the books.
- **Close the day** is one count and one confirmation. A **count difference** above **Count difference that needs a reason** in Cash Drawer Settings ($20 by default) needs a reason.
- **Deposit to bank** takes cash to the bank. **Add cash from bank** brings it back. A **Cash expense** pays for something out of the drawer.
- **Undo** reverses an entry. Cash records are never deleted.

See [Cash drawer](../counter/cash-drawer.md).

**Charge exactly.** A box under the total on the register and on Create Order. Type the total the customer pays, tax included, and the line prices are worked back from it.

**Check/Money Order.** The way to record a paper check or money order a customer hands over. It goes to the bank, the same as Zelle and ACH, and is never part of the cash drawer count. Its number is required wherever it is recorded. See [Take payment](../counter/payments.md).

**Commission.** The store's cut of a consigned gun's sale price before tax. The consignor is owed the rest. Each gun carries its own rate, which starts from the store default. See [Consignment](../stock/consignment.md).

**Connector Audit Log.** The permanent record of every call made through the store's AI connectors: who, which tool, with what, and whether it worked. See [The MCP server](../../ai/mcp.md).

**Consignment.** A gun that belongs to someone else, left with the store to sell. Its Serial No is ticked **Consignment** and names the **Consignor**. The store sells it, keeps the commission and owes the consignor the rest. See [Consignment](../stock/consignment.md).

**Consignor.** The person who left a gun with the store on consignment. The system keeps a supplier record for them. What the store owes them is tracked until the bookkeeper pays it out.

**Custody warehouse.** A stock location for firearms that belong to customers, such as a gun received for a transfer and waiting for its owner. A gun there is not yours to sell, so it is left out of sellable stock and hidden from Inventory Receipts unless you ask for it.

**Customer status.** **Active**, **Inactive**, **Prospect** or **Banned** on a customer's Profile tab. A banned customer needs a reason and shows in red on the customer list. Two records for one person are joined with **Merge into…**. See [Customers](../counter/customers.md).

**Dashboard.** The page staff land on after signing in: today's sales, open 4473s, guns waiting on pickup, the cash drawer, records not yet in the bound book, and distributor orders arriving, with **All records** at the bottom. See [The Dashboard](../start/dashboard.md).

**Disposition.** A firearm leaving your inventory, and the bound book entry that records it. A retail sale, a dealer transfer, a destroyed or stolen gun: each is a disposition, and each must name where the gun went.

**Distributor invoice.** A read-only copy of an invoice on a distributor account, pulled every hour and matched to the POS order it belongs to. One with no POS order behind it is marked **Outside POS**. See [Receive a distributor delivery](../stock/receive-distributor.md).

**Distributor order.** One purchase from one wholesale house, numbered like `DO-2609-0001` (year, month, running number). It waits in Draft until a person confirms it. See [Order from a distributor](../stock/distributor-orders.md).

**Extension.** An add-on that brings its own pages and records, installed and switched on or off from **Extensions** in Settings. See [Settings](../manager/settings.md).

**eZ Check.** The ATF's public service for confirming that an FFL number is valid and unexpired. The system checks other dealers against it before receiving or shipping firearms. See [Verify an FFL](../compliance/ffl-verification.md).

**FastBound.** The electronic bound book service this system connects to. It holds the official A&D record, and it is where an electronic 4473 is completed.

Your acquisitions and dispositions are sent to it, and it tells the POS when a 4473 finishes. See [FastBound](../admin/fastbound.md).

**FFL.** Federal Firearms License, and by extension the business that holds one. "Ship it to their FFL" means ship it to the licensed dealer who will run the buyer's 4473. An FFL number looks like `1-23-456-78-9A-01234`.

**FFL type codes.** The two-digit group inside the license number that says what kind of licensee it is: 01 a dealer in firearms, 02 a pawnbroker, 03 a Curio and Relic collector, 07 a manufacturer, 08 an importer. Type 03 matters day to day, because a collector's license changes where a gun may ship.

**Incoming shipments.** The list on Receive Goods of distributor parcels on their way to the store, including parcels ordered outside the POS. See [Receive a distributor delivery](../stock/receive-distributor.md).

**Inventory count.** A stocktake: scan what is on the shelves, see what is missing or unexpected, and correct the quantities of items without serial numbers in one step. Guns are only reported, never adjusted. See [Count the shop](../stock/inventory-count.md).

**Item groups.** The families items are filed under. Firearms are **Pistol**, **Revolver**, **Rifle**, **Shotgun**, **Other Firearm** and **Receiver**, and NFA items have their own group. Gun parts are under **Parts**: regular items with no serial number. See [Models, serials and prices](../stock/items-and-serials.md).

**Item versus serial.** An **Item** is the model: "Glock 19 Gen5 9mm". A **Serial No** is one physical gun with its own serial number, cost, photos and bound book history.

Ammunition and accessories are items with a quantity and no serials. Pricing, listings and reports all tell the two apart. See [Models, serials and prices](../stock/items-and-serials.md).

**Manager Override 4473.** A manager's button that finishes a firearm sale whose 4473 was completed but is stuck electronically, or was done straight in the bound book. It records who overrode it and why, and every override lands on the **Hand-Asserted 4473 Dispositions** report. See [Form 4473 and NICS](../compliance/4473.md).

**MAP.** Minimum Advertised Price. The lowest price a manufacturer or distributor lets you advertise a product at, which is not the lowest price you may sell it at. The counter price is never raised to MAP.

**MCP connector.** The address an AI assistant connects to so it can read, or work, the POS, signed in with a person's own account. Each store has a read-only one and a full one, switched on in MCP Settings. See [The MCP server](../../ai/mcp.md).

**MSRP.** Manufacturer's Suggested Retail Price. The list price from the manufacturer. It is a starting point for pricing, not a rule.

**NICS.** The National Instant Criminal Background Check System, run by the FBI. Every retail firearm buyer goes through it, and it answers Proceed, Delayed or Denied.

A firearm sale needs Proceed before it can complete. A Delayed answer starts the Brady date clock. Denied means the gun does not leave.

**Not placed.** A tab on the Storage Locations page listing guns and stock with no location yet, and units sold without saying where they came from. See [Find and move stock](../stock/find-and-move.md).

**Notifications bell.** The bell near the top of the sidebar. It rings in the app, never by email, for NICS results, new web and GunBroker orders, a distributor shipping to the store, and a record FastBound refused. See [The Dashboard](../start/dashboard.md#notifications).

**Outside POS.** A distributor invoice, or a parcel, for an order placed somewhere other than the POS, such as the distributor's website.

**Pickup ticket.** A $0 register ticket that hands over a gun somebody already bought on a counter order, or a web order collected at your counter. The money stays on the order.

The ticket carries the gun and its serial, so the 4473 runs on the register and the gun leaves the bound book at **Complete Order**. In Recent Orders its label shows the order's money: **Paid**, **Deposit … · … due** or **Unpaid · … due**. See [Sell a gun](../counter/sell-a-gun.md#hand-over-a-gun-on-a-pickup-ticket).

**Private seller.** A person, not a business, who sells you a gun. The seller is paid on Receive Goods or in the Trade-in dialog by Cash, Zelle, Check or ACH.

A purchase with no seller payment recorded shows on the cash drawer page and on the **Seller not settled** card of the Inventory Receipts report. See [Buy from a private seller](../stock/private-seller.md).

**Register ticket and invoice.** A counter sale rung up on the register is a register ticket. A web order, a GunBroker order or anything built on Create Order gets an invoice. Each night the day's register tickets are rolled into invoices, and the [Sales Report](../manager/sales-report.md) counts each sale once. The bookkeeper's view is on [For the bookkeeper](../admin/bookkeeper.md).

**Redo 4473.** Starting a sale's 4473 again. The earlier form shows as Cancelled in the 4473 records, and a paper 4473 or a manager override can finish the sale afterwards. See [Form 4473 and NICS](../compliance/4473.md#redo-4473).

**Serial No.** The per-gun record: the serial number, manufacturer, model, caliber, importer, condition and year, cost, selling price, storage location, photos, its listing on each sales channel and its link into FastBound. Almost everything the store does to one gun happens on this record.

**ShipStation.** The shipping service that buys labels and returns tracking numbers. **Ship** on Pending Orders sends the order to it, and a scheduled job reads the tracking back. See [Ship or hand over an order](../orders/ship-or-hand-over.md).

**Storage location.** Where a gun or item sits in the store. A **zone** is either a run of numbered **slots**, one gun per slot, or one open location such as a shelf, case or safe that holds anything.

The **store map** draws the zones on a floor plan. Moving something between locations changes no stock and no books. See [Find and move stock](../stock/find-and-move.md).

**Store pays customer.** What the register shows when a trade-in is worth more than the sale: one ticket, no sales tax, and the store pays the difference by Cash, Zelle, Check or ACH. See [Trade-ins](../counter/trade-ins.md).

**Transfer.** A customer buys a gun somewhere else, has it shipped to you, and completes their 4473 here. You charge a transfer fee, run the 4473 and NICS, and hand over a gun that was never yours to sell. The gun still passes through the bound book both ways. See [Customer transfers](../counter/transfers.md).

**Virtual Terminal.** Payroc's web page for keying a card by hand. A charge taken there is recorded in the POS with its approval code, not charged again.

**Warehouse.** A named stock location in the books. A store has ordinary sales warehouses plus special ones, such as a custody warehouse for customer property. The warehouse decides whether a gun can be sold; where it sits on the floor is its storage location.
