---
name: firearm-inventory
description: Use for any question about guns in stock — how many firearms the store has, which guns or serial numbers are on hand, what a model sells for, or stock of any item. Explains what "in stock" and "for sale" mean in a gun store and which OpenGunstore APIs answer it.
---

# Firearm inventory in OpenGunstore

Every firearm is serial-tracked: one `Serial No` per gun, on an `Item` with `is_firearm = 1`. Count guns by Serial No, never by `Bin` quantity, the `Stock Balance` report or Item Group totals — those mix in transfer guns, consigned guns and non-gun items.

## Answer with these calls

All of them are read-only and run without approval through `call_api`.

| Question | Call |
|---|---|
| How many guns do we have? How many can we sell? | `ffl_core.firearm.firearm_stock_summary` |
| Which units of a model are for sale, and at what price? | `ffl_core.firearm.available_serials_with_prices` with `item_codes` |
| Stock of a non-gun item (ammo, accessories, parts) | `ffl_core.api.item_admin.get_item_stock` with `item_codes` |
| The full list of guns by serial, with location and source | `run_report` on `Firearms In Stock` |

Find item codes first with `read_records` on `Item` (filter by `item_name` like, or `manufacturer`).

## What the numbers mean

- **On hand**: the Serial No is `Active`. The store physically has the gun or still answers for it.
- **For sale**: on hand and the counter would offer it. A gun is not for sale when it is held on a ticket or order, reserved by a web or GunBroker order, consigned out to another dealer, or a customer's transfer gun (the store holds it, the customer owns it).
- **Antiques** are counted on hand but are not in the A&D book.

When someone asks "how many guns do we have", give on hand and for sale together, and say what makes up the difference if it is large. Break down by warehouse or firearm type only when asked.

## Do not

- Do not count with `Stock Balance`, `Stock Ledger` or `Bin`.
- Do not report a held or reserved gun as available.
- Do not change stock with `create`, `update` or `execute` (Stock Entry, Stock Reconciliation, Serial No). Receiving, counting and correcting stock are done in the POS screens; see the `firearm-records` skill.
