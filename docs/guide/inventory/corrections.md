# Fixing mistakes

Stock goes in wrong sometimes: a transposed serial, a cost nobody looked up, a gun filed under the wrong model. These are the tools for putting it right, and what each one leaves behind in the books.

## Before you start

- Most of these need **Stock Manager** or **System Manager**. If you do not have it, the screen shows "Not permitted" and "You do not have enough permissions to access this resource. Please contact your manager to get access." without naming a role. Ask a manager.
- Almost every tool below works on an **in-stock** gun only. Once a firearm has been sold or transferred, it has left the books under the details it carried at the time. Correcting it is a bookkeeping job rather than a button.
- Read what each tool does not do. Several of them deliberately leave FastBound or the general ledger alone.

## Pick the right tool

| The problem | The tool | Where it is |
|---|---|---|
| The serial number is mistyped | **Correct Serial** | Serial No form; Item form's Serial Numbers tab |
| The gun's cost is wrong | **Correct Cost** | Item form, Serial No form, Firearms In Stock report |
| Ammo or accessory count is wrong | **Adjust Quantity** | Item form, non-serialized items |
| A shelf count disagrees for many items | **Inventory Count** | Sidebar: **Inventory → Inventory count** |
| Found stock that was never booked in | **+ Add Stock** | Item form, non-serialized items |
| The gun is filed under the wrong model | **Reassign Item** | Serial No form |
| This system and the bound book disagree | **Sync with FastBound** | Serial No form |
| A test item needs to disappear entirely | **Force Delete** | Item form, Item list Actions menu |
| A seller payment or cash entry was wrong | **Undo** | [Cash Drawer](../selling/cash-drawer.md) page |

## Correct Serial

**Where:** the Serial No form, as a **Correct Serial** button, and on the Item form's Serial Numbers tab as a small **✎ fix** link on each in-stock row.

**When:** the serial on the receiver does not match the serial in the system. A transposition at receiving is the usual cause. It is the right tool only while the gun is still in stock and unsold.

**Steps**

1. Click **Correct Serial**. The dialog is titled **Correct Serial Number** and opens with a warning explaining its scope.
2. Type the **Corrected (new) Serial Number**. The description under the box shows the current one so you can compare.
3. Type a **Reason for correction**. It is required, and the description says it is "Written to the A&D audit trail and pushed to FastBound."
4. Click **Correct Serial**.

**What it leaves behind**

The Serial No record is renamed, not replaced, so stock, price, photos, condition, cost and the FastBound link all survive. Every submitted FFL Acquisition carrying the old serial is repointed to the new one and gains an audit line naming both serials, the reason, who did it and when. The same line is added as a comment on the gun.

The FastBound bound-book item's serial is edited in place, with the same note, in the same transaction. If FastBound refuses, everything above is undone. This tool never leaves this system and the bound book disagreeing.

A per-gun web listing is republished automatically, because its title and SKU carry the serial. A GunBroker listing cannot be corrected in place, so it is ended and a person re-lists it.

!!! warning "Federal recordkeeping"
    A corrected serial has to be corrected in the bound book too. This tool does that for you while FastBound is switched on. If the integration is switched off and the gun already has a bound-book item, the correction still goes through locally and the comment on the gun says so: the bound-book item "still carries the OLD serial and must be corrected there before the integration is re-enabled." Fix it in FastBound by hand before turning the integration back on.

**Refusals**

| Message you see | What it means |
|---|---|
| `Serial No <new> already exists — it names a different firearm.` | Two guns would collide. Resolve it by hand. The tool will not merge two guns. |
| `Serial No <n> is '<status>', not Active (in stock).` | The gun is not in stock. Out of scope. |
| `Serial No <n> has a committed FFL Disposition — it has been sold or transferred.` | The 4473 is archived under the old serial. Out of scope. |
| `This firearm has no FastBound bound-book item` | The gun is not in FastBound. Resolve the link first with **Sync with FastBound**. |
| `FastBound is not configured` | The integration is on but has no credentials. Configure it, or switch it off. |

## Correct Cost

**Where:** the Item form and the Serial No form as a **Correct Cost** button, and on the Firearms In Stock report (see [Daily checks](../compliance/daily-checks.md)) as a small `$` link in the Serial No cell, tooltip "Correct this gun's cost".

**When:** the recorded acquisition cost of an in-stock gun the store owns is wrong. The classic case is a gun received at $0 because nobody had the dealer invoice yet. Until it is fixed, the sale shows close to 100% margin.

**Steps**

1. Click **Correct Cost**. The dialog is titled **Correct Cost — <serial>**.
2. Confirm the **Serial Number**. It is locked when you opened the dialog from one gun.
3. Read the line showing the current recorded cost, the ledger valuation and the warehouse.
4. Enter the **New Cost**. It must be greater than 0.
5. Enter a **Reason**. Required, for example "Found dealer invoice — true cost $900".
6. Tick **The amount actually paid to the seller was different** only if the seller really was paid a different amount than recorded. The box appears only when the gun's seller payment was recorded in the cash drawer. Its note reads "Tick it to book the difference to the cash drawer or bank; leave it empty and only the cost changes."
7. Click **Correct Cost**.

**What it leaves behind**

One Stock Reconciliation dated today, revaluing exactly that serial. The quantity never moves, and a reconciliation that would change quantity is refused.

In the ledger, an increase debits the warehouse's stock account and credits the counter account. That counter account is normally **Stock Adjustment**, which is a profit-and-loss account. **No cash or accounts-payable entry is made** unless you ticked **The amount actually paid to the seller was different**. If the gun was paid for and never booked, a bookkeeper still has to post a source-of-cost journal entry. The dialog says so in a follow-up note titled **Bookkeeping note** when the gun previously had almost no recorded cost.

With the box ticked, the difference between the old and the new payment is booked against the same cash drawer or bank account the seller was paid from. If nothing could be booked, a note titled **Cash drawer** says why.

The gun's `purchase_rate` is updated. That is what the sales report reads for firearm cost of goods sold. Comments recording the old cost, the new cost, the reason and your name are added to both the reconciliation and the gun.

**The bound book is not touched.** Cost is not an ATF field, and FastBound's cost is not synced.

**Refusals** include a sold gun ("an already-sold gun's cost is realised and needs a separate journal entry"), a gun on consignment from its owner ("it is not store inventory"), a non-firearm, and a frozen accounting period.

## Seller payments and cash entries

The journal entries the cash drawer books cannot be cancelled, reversed or amended from the accounting screens. A payment to a private seller and every other cash entry is changed or undone by a manager on the [Cash Drawer](../selling/cash-drawer.md) page. The payout on a register ticket where the store paid the customer goes away by cancelling that ticket.

## Adjust Quantity

**Where:** the Item form, on non-serialized stock items only. It never appears on a firearm.

**When:** a physical count of one item disagrees with the system: a write-off, stock found in the back room. To count the whole shop, or many items at once, use [Inventory Count](inventory-count.md) instead. It adjusts every counted non-serialized item in one step.

**Steps**

1. Click **Adjust Quantity**. The dialog is titled **Adjust Quantity — <item>**.
2. Enter **Set Quantity To**. This is the absolute figure on the shelf now, not a difference. Units on register sales not closed yet have already left the shelf; the system allows for them, so do not add them back.
3. Confirm the **Warehouse**.
4. Enter a **Reason**. Required, for example "Annual physical count", "Found in back room" or "Write-off — damaged".
5. Click **Update Quantity**.

**What it leaves behind**

A Stock Reconciliation setting the item to that quantity at that warehouse, with your reason stored as a comment on it. The units already on the shelf keep the cost they have. Units the count adds are valued at that same cost, or at the item's cost when the warehouse held none.

| Message you see | What it means | Fix |
|---|---|---|
| No cost on file for … | The count adds units and the item has no cost to value them at. | Set the item's valuation rate, or use **+ Add Stock** with a rate. |
| An open register return of … is not closed yet. … | Returned units are on the shelf but not yet in stock, and your count is below them. | Include the returned units in the count, or close the register first. |
| Stock is already … at …; nothing to adjust. | The count matches the system. | Nothing to do. |

Nothing touches the bound book, because a serial-tracked firearm cannot reach this tool. If you try, the refusal explains why: "Item '<code>' is serial-tracked. Each unit is its own Serial No record — adjusting bulk qty here would orphan serial entries. Use Receive Goods to add or Stock Entry to remove specific units."

## + Add Stock

**Where:** the Item form, on non-serialized stock items only.

**When:** units need to go in without a purchase document: opening stock, found inventory, a correction to an earlier undercount.

**Steps**

1. Click **+ Add Stock**. The dialog is titled **Add Stock — <item>**.
2. Enter the **Quantity** to add.
3. Confirm the **Warehouse**.
4. Enter a **Cost / Rate** if you know it. It defaults to the last purchase rate, then the distributor's dealer price.
5. Add **Remarks** if the reason is worth recording.
6. Click **Add to Stock**.

**What it leaves behind**

A submitted Stock Entry of type Material Receipt, with the normal ledger posting for a receipt. Your remarks are stored on it. No Serial No records and no bound-book entry.

That last point is the reason firearms are excluded. The refusal says it plainly: "Item '<code>' requires serial numbers per unit. Use the Receive Goods page so each unit gets a Serial No record and the FFL Acquisition is created." Firearms go through [Receive Goods](receive-goods.md), always.

## Reassign Item

**Where:** the Serial No form.

**When:** a gun is filed under the wrong model item. This happens when a used gun was given its own item and belongs in a shared model pool, or when the wrong SKU was picked at receiving.

**Steps**

1. Click **Reassign Item**. The dialog is titled **Reassign to another Item** and opens with a note explaining what survives.
2. Pick the **Target Item**. Only firearm items are offered.
3. Click **Reassign**.

**What it leaves behind**

Two stock movements at the gun's value in the books, out of the old item and into the new one, with the same Serial No throughout. The gun keeps its own recorded cost, so moving it never revalues it. The record is never deleted, so the serial, price, photos, condition, manufacturer, importer and model all carry across. Every submitted FFL Acquisition for the serial is repointed to the new item in place, keeping its identity, its timestamp and its FastBound link.

**FastBound is not changed.** The dialog says so. Reassignment is an internal re-bucketing, and the bound book records the gun, not which SKU you filed it under.

Any per-gun web listing is delisted, because that product belonged to the old item and would otherwise leave the gun sellable twice.

**Refusals** cover a sold or transferred gun, a target that is not a firearm, and moving a bound-book gun onto an antique item.

## Sync with FastBound

**Where:** the Serial No form.

**When:** the **Check FastBound differences** scan on the Firearms In Stock report (see [Daily checks](../compliance/daily-checks.md)) says this gun differs from the bound book.

Clicking it compares Manufacturer, Importer, Model, Caliber and Firearm Type against FastBound. If they match you get "✓ Already in sync with FastBound." If they do not, the **Reconcile with FastBound** dialog lists each differing field with a choice of **Use FastBound** or **Use POS**. Choosing **Use POS** pushes your value to the bound book.

Full detail is on [FastBound](../compliance/fastbound.md). A record that should never be in the bound book at all is a manager task described there too, under [A record that does not belong in the bound book](../compliance/fastbound.md#a-record-that-does-not-belong-in-the-bound-book).

## Force Delete

**Where:** the Item form as a red **Force Delete** button, and the Item list under **Actions** for deleting several at once. Both are System Manager only.

**When:** an item was created by mistake and has to disappear. This is a cleanup tool, not a routine correction. Standard behavior refuses to delete an item referenced by a submitted document, and this is the override.

**Steps**

1. Click **Force Delete**. The dialog is titled **Force Delete Items** and shows what would happen: submitted documents to cancel, draft documents to delete, linked records to remove, and any item codes that were not found.
2. Read the footer, which reads: "This action is irreversible. Cancelled documents retain their cancelled history; deleted ledger / serial / FFL records cannot be recovered."
3. Click **Cancel & Delete**, or **Delete** when nothing submitted is involved.

**What it leaves behind**

Cancelled invoices, delivery notes, stock entries, purchase receipts and orders, which keep their cancelled history. Then it removes stock ledger entries, bins, Serial No records, batches, item prices and, critically, **the item's FFL Acquisition and FFL Disposition records**. Then the item itself.

!!! danger "This destroys bound-book records"
    Force Delete removes this system's copy of the acquisitions and dispositions for that item. FastBound is not touched, so the bound book keeps its entries and the two are now out of step. Never run it on an item that holds a real firearm. Use it for test data and mistakes made before anything was received.

If you are not a System Manager the refusal is explicit: "Force-delete is restricted to System Manager."

## Related pages

- [Items and serials](items-and-serials.md) for the fields these tools change.
- [The bound book](../compliance/bound-book.md) for what an acquisition record holds.
- [FastBound](../compliance/fastbound.md) for reconciling with the bound book.
- [Troubleshooting](../reference/troubleshooting.md).
