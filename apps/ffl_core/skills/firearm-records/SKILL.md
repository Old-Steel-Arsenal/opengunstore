---
name: firearm-records
description: Read before creating, updating, deleting, submitting or cancelling anything that touches a firearm, a serial number, a customer's ID, a 4473, the A&D book, a transfer, a consignment or a sale with a gun on it. Lists what an agent may not change directly and where staff do it instead.
---

# Firearm records are compliance records

A licensed dealer must keep an exact Acquisition & Disposition (A&D) book and a Form 4473 for every sale. In OpenGunstore those records are written by the POS flows, which run the checks the law needs. Writing them directly skips those checks, so do not.

## Never change these directly

Do not use `create`, `update`, `delete`, `run_action` or `execute` on:

- `Serial No` of a firearm, or the firearm fields of an `Item` (`is_firearm`, `is_antique`, `firearm_type`, manufacturer, model, caliber, importer)
- `Form 4473 Log` and anything about a 4473 or NICS check
- `FFL Acquisition`, `FFL Disposition` and anything synced to the bound book (FastBound)
- `Customer ID Document` and a customer's ID fields
- `POS Invoice`, `Sales Invoice`, `Sales Order` or `Delivery Note` with a firearm line: no submit, cancel, amend or edit
- `Stock Entry`, `Stock Reconciliation` or `Purchase Receipt` that moves a firearm
- Transfers, consignments, trade-ins and intakes

Tell the user which POS screen does it instead: receiving goods, trade-in, the counter, the 4473 screen, the transfer dialog, or Inventory Count.

## Customer identity

Never repeat a Social Security number, a full ID or license number, or a date of birth in a reply, even when a record you read contains one. Say that the record has it.

## Reading is fine

Reading records and running reports to answer a question needs no approval. When unsure whether something is a write, treat it as one and ask.
