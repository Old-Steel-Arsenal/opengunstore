# Fix a mistake

For stock managers, when stock went in wrong: a mistyped serial, a wrong cost, a gun filed under the wrong model.

## Before you start

- Most of these tools need **Stock Manager** or **System Manager**. Without it the screen shows "Not permitted" and asks you to contact your manager. Ask a manager.
- Almost every tool works on an **in-stock** gun only. A sold or transferred gun left the books under the details it had then, and fixing it is a job for the bookkeeper, not a button.
- Read what each tool leaves alone. Several do not touch FastBound or the accounts.

## Pick the right tool

| The problem | The tool and where it is |
|---|---|
| The serial number is mistyped | **Correct Serial**, on the gun's serial record or the item's **Serial Numbers** tab |
| A gun's cost is wrong | **Correct Cost**, on the item, the serial record or the Firearms In Stock report |
| One ammo or accessory count is wrong | **Adjust Quantity**, on the item |
| Many shelf counts are wrong | [Count the shop](inventory-count.md) |
| Found stock that was never booked in | **+ Add Stock**, on the item |
| The gun is filed under the wrong model | **Reassign Item**, on the serial record |
| The POS and the bound book disagree | **Sync with FastBound**, on the serial record |
| A test item has to disappear | **Force Delete**, on the item (System Manager) |
| A seller payment or cash entry was wrong | **Undo** on the [Cash drawer](../counter/cash-drawer.md) page |

Open a gun's serial record from the sidebar: **Inventory**, then **Serial numbers**. Open an item from **Inventory**, then **Items**.

## Correct Serial

Use it when the serial on the receiver does not match the system, usually a typo at receiving. The gun must be in stock and unsold. On the item's **Serial Numbers** tab it is the small **✎ fix** link on each in-stock row.

1. Click **Correct Serial**. The **Correct Serial Number** dialog opens.
2. Type the **Corrected (new) Serial Number**. The current one shows under the box to compare.
3. Type a **Reason for correction**. It is required and goes to the A&D audit trail and FastBound.
4. Click **Correct Serial**.

What changes:

- The gun keeps everything (stock, price, photos, cost, FastBound link) under its new serial. Its acquisition records point to the new serial, with an audit line naming both serials, the reason, who and when.
- FastBound's copy is corrected in the same step. If FastBound refuses, nothing changes.
- A per-gun web listing is republished. A GunBroker listing cannot be corrected, so it is ended and someone lists the gun again.

!!! warning "Federal recordkeeping"
    A corrected serial must be corrected in the bound book too. With FastBound switched off, the fix still goes through here, and a comment on the gun says the bound-book item "still carries the OLD serial and must be corrected there before the integration is re-enabled." Fix it in FastBound by hand before turning the integration back on.

The tool never merges two guns. If the new serial already exists for another gun, sort it out by hand.

## Correct Cost

Use it when an in-stock gun the store owns has the wrong cost. The classic case is a gun received at $0 before the dealer invoice arrived, so its sale would show almost 100% margin. On the Firearms In Stock report it is the small `$` link in the serial cell.

1. Click **Correct Cost**. The **Correct Cost — <serial>** dialog opens.
2. Confirm the **Serial Number**.
3. Read the current recorded cost, the book value and the warehouse.
4. Enter the **New Cost**. It must be above 0.
5. Enter a **Reason**, for example "Found dealer invoice — true cost $900".
6. Tick **The amount actually paid to the seller was different** only if the seller really was paid a different amount. The box shows only when the seller was paid through the cash drawer.
7. Click **Correct Cost**.

What changes:

- The gun's cost changes from today. The Sales Report uses the new cost when the gun sells. A comment on the gun records old cost, new cost, reason and your name.
- No money moves, unless you ticked the seller box. Then the difference is booked against the cash drawer or bank the seller was paid from. A **Cash drawer** note says why if it could not be.
- The bound book is not touched. Cost is not an ATF field.

If the gun had almost no recorded cost, a **Bookkeeping note** follows. Pass it to your bookkeeper: a gun paid for but never booked needs an entry from them. See [For the bookkeeper](../admin/bookkeeper.md).

Correct Cost refuses a sold gun, a consigned gun, a non-firearm and a closed accounting period.

## Adjust Quantity

Use it on one non-serialized item when the shelf disagrees: a write-off, stock found in the back room. It never appears on a firearm.

1. Click **Adjust Quantity**.
2. Enter **Set Quantity To**: the number on the shelf now, not the difference. Units on register sales not yet closed have already left the shelf, and the system allows for them, so do not add them back.
3. Confirm the **Warehouse**.
4. Enter a **Reason**, such as "Found in back room" or "Write-off — damaged".
5. Click **Update Quantity**.

The item is set to that quantity, with your reason on record. Units already on the shelf keep their cost, and added units take the same cost (or the item's cost when the warehouse held none). Nothing touches the bound book.

## + Add Stock

Use it for non-serialized units that come in without a purchase: opening stock, found stock, an earlier undercount.

1. Click **+ Add Stock**.
2. Enter the **Quantity** to add.
3. Confirm the **Warehouse**.
4. Enter a **Cost / Rate** if you know it. It starts at the last purchase rate, then the distributor's dealer price. Left blank, the cost already on file is used. With no cost on file, you are asked to enter one.
5. Add **Remarks** if the reason is worth keeping.
6. Click **Add to Stock**.

Firearms are refused. A gun always goes through [Receive Goods](receive-goods.md), so it gets its own serial record and acquisition.

A service, such as a fee or labor, has no stock to add. Untick **Maintain Stock** on the item instead.

## Reassign Item

Use it when a gun is filed under the wrong model, for example a used gun that belongs in a shared model pool, or the wrong SKU picked at receiving.

1. Click **Reassign Item**. The **Reassign to another Item** dialog opens.
2. Pick the **Target Item**. Only firearm items are offered.
3. Click **Reassign**.

The gun moves to the new item with the same serial, cost, price, photos and details. Its acquisition records follow it. FastBound is not changed, because the bound book records the gun, not the SKU. A per-gun web listing is taken down so the gun cannot sell twice.

Reassign refuses a sold or transferred gun, a non-firearm target, and moving a bound-book gun onto an antique item.

## Sync with FastBound

Use it when **Check FastBound differences** on the Firearms In Stock report (see [Daily checks](../compliance/daily-checks.md)) says a gun differs from the bound book.

1. Click **Sync with FastBound** on the serial record.
2. If the details match, you see "✓ Already in sync with FastBound."
3. Otherwise the **Reconcile with FastBound** dialog lists each differing field. Choose **Use FastBound** or **Use POS** for each. **Use POS** sends your value to the bound book.

A record that should not be in the bound book at all is a manager task. See [FastBound](../admin/fastbound.md).

## Force Delete

System Manager only. It is on the item as a red **Force Delete** button, and on the item list under **Actions** for several at once. It is a cleanup tool for items created by mistake.

1. Click **Force Delete**. The **Force Delete Items** dialog lists what would be cancelled, deleted and removed.
2. Read the footer: "This action is irreversible. Cancelled documents retain their cancelled history; deleted ledger / serial / FFL records cannot be recovered."
3. Click **Cancel & Delete**, or **Delete** when nothing submitted is involved.

!!! danger "This destroys bound-book records"
    Force Delete removes this system's acquisitions and dispositions for the item, its serial records and its stock history. FastBound is not touched, so the two books no longer match. Never run it on an item that holds a real firearm. Use it for test data and mistakes made before anything was received.

## Troubleshooting

| You see | Fix |
|---|---|
| `Serial No <new> already exists — it names a different firearm.` | Two guns would collide. Check both guns and sort it out by hand. |
| `Serial No <n> has a committed FFL Disposition — it has been sold or transferred.` | The gun has been sold or transferred under that serial. Correct Serial cannot change it. |
| `This firearm has no FastBound bound-book item` | Link it first with **Sync with FastBound**. |
| No cost on file for … | Set the item's **Valuation Rate**, or use **+ Add Stock** with a rate. |
| An open register return of … is not closed yet. … | Count the returned units too, or close the register first. |

More messages: [Troubleshooting](../help/troubleshooting.md).
