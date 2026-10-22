# Working as an AI agent

This page is for an AI agent that reads or acts on a store's OpenGunstore POS: what the system is made of, the rules it does not let you weigh, where each job is done, and what to check before you act. The rest of this documentation is written for the person at the counter. It is still your best source, because it describes what each screen actually does.

## Machine readable copies

| What | Where |
|---|---|
| Index of every page, with a one-line summary each | `llms.txt` at the root of this site |
| The whole documentation as one file | `llms-full.txt` at the root of this site |
| Any single page as Markdown | that page's address plus `.md` |

Read `llms.txt` first and fetch what the task needs. Take `llms-full.txt` only when you have the budget for all of it. All three are generated from the same sources on every build.

To act on the store rather than read about it, see [The MCP server](mcp.md). To install the software on a server, see [Install with an AI agent](../deploy-with-ai.md).

## What the system is made of

A point of sale and back office built on ERPNext, with three apps on top of it. The store sells at a counter, on a WooCommerce shop and on GunBroker, from one shared inventory.

Three record types carry most of the meaning, and confusing them is the most common way to get an answer wrong.

**Item** is a model, the thing a catalog lists. `GLK-19-G5` is an Item.

**Serial No** is one physical firearm. `BXTR421` is a Serial No. A firearm's price, condition, photos and channel listings can all live on the serial rather than the model, because with used guns the serial *is* the price.

**FFL Acquisition and FFL Disposition** are the two halves of the federal acquisition-and-disposition record, the bound book. Every firearm entering the premises gets an acquisition. Every firearm leaving gets a disposition. The pages staff already use create them. They are not paperwork about the transaction. They *are* the federal record of it.

Alongside those: **POS Invoice** for a counter sale, **Sales Invoice** for an order that ships, **Purchase Receipt** for stock arriving, and **Woo Online Order** and **GunBroker Order** for the two outside channels. Others you will meet:

| Record | What it is |
|---|---|
| **Form 4473 Log** | One row per completed Form 4473, electronic or paper, with its outcome and, for an electronic form, a private copy of the signed PDF. Permanent: it cannot be deleted |
| **Distributor Order** / **Distributor Invoice** | What was ordered from a wholesale house, and every invoice on its account. The invoices are a read-only mirror |
| **Cash Drawer Entry** / **Cash Drawer Close** | Deposits, cash from the bank, expenses, seller payments, and each day's count. Never deleted: each one is undone instead |
| **Storage Zone** / **Storage Location** / **Storage Move** | The store map, its slots and shelves, and every move between them. A tracking layer only: no stock or accounting record |
| **Inventory Count** | A stocktake and its scans |
| **Connector Audit Log** | Every call made through the remote MCP connectors. Permanent |

A store can also install extensions, which add pages and record types of their own. Each is switched on and off under Settings → Extensions. A switched-off extension's pages say so, and its records stay.

## Rules you do not get to weigh

These are not preferences. Each one is either federal recordkeeping or a control that exists because it was once absent.

**Never book, edit or delete a bound-book record directly.** Acquisitions and dispositions are written by the workflow that moved the gun. Writing one by hand detaches the federal record from the event it is supposed to describe. If a bound-book entry looks wrong, read [Fixing mistakes](../guide/inventory/corrections.md) and surface it. Do not repair it yourself.

**Never complete a firearm sale that the Form 4473 gate is holding.** The gate is the point of the system. Its statuses, the manager override and the paper route are described in [Form 4473 and NICS](../guide/compliance/4473.md). An override is a human decision that lands on a review report with that human's name on it. It is not an action for an agent.

**Never ship a firearm to a dealer whose license is unverified or expired.** Verification is described in [Verifying an FFL](../guide/compliance/ffl-verification.md).

**Never treat a serial number as interchangeable with another.** A serial identifies one gun in a federal record. If a serial does not match, stop.

**Never move money.** Recording a payment, voiding a card charge, refunding or adjusting a price changes what a customer was charged. Propose it and let a person do it.

**Undo cash drawer records only on the cash drawer page.** The journal entries the drawer books cannot be cancelled, reversed or amended from the accounting screens. Use Undo on [the cash drawer](../guide/selling/cash-drawer.md) page, or cancel the register ticket for a trade-in payout. That keeps the drawer count and the books in step.

**Do not resolve an ambiguity in a firearm's identity by guessing.** Ask.

## Where the truth lives

This documentation describes behavior. It does not define it. When it and the system disagree, the system is right and the page has a bug worth reporting. Two kinds of statement drift most often, so check them against the system:

- **Role claims.** Which role opens which page is set in the code. If access matters to your task, verify it rather than assume.
- **Automatic behavior.** Statements about what the system does for you, without being asked.

When you are unsure, report what you found and what you could not determine. In this domain an honest "I could not confirm which gun this is" is worth more than a confident answer. The downstream record is a federal one, and a wrong entry is harder to undo than a missing one.

## Before you start

Establish three things about yourself, in this order.

1. **Which surface you have.** The Desk UI (pages at `/app/...`, which the browser may show as `/desk/...`), a whitelisted HTTP endpoint, [the store's MCP connectors](mcp.md), or the [API reference](../api/index.html). They are not equivalent: the UI runs client-side steps that an endpoint does not, and some workflows exist only as a page.
2. **Whether you are on production.** A store runs a live site, and a developer runs a local development site that is also the automated test fixture. Records with names beginning `_Test` mean you are not on production, and nothing you do there means anything.
3. **What your role opens.** Access is by role, and roles differ per page. On a remote connector you act with the signed-in person's own roles. See [Roles and permissions](../guide/getting-started/roles.md).

## Reading

Reads are safe and should be your default. The reports are the store's own questions, already asked well, and each one is a better starting point than a raw query.

| Question | Read this |
|---|---|
| What guns are in stock, where each came from, and where it sits in the store | Firearms In Stock |
| Which sales are held waiting on a form or a check | Pending 4473 Orders |
| Every completed Form 4473 and its outcome | Form 4473 Records |
| Whose gun is sitting in the safe unclaimed | Pending Transfer Pickups |
| What came into stock in a period, and how | Inventory Receipts |
| What sold, across every channel, and at what margin | Sales Report |
| Cash in and out of the drawer, day by day | Cash Drawer Weekly, and the Log tab on the cash drawer page |
| Card payments taken on the terminal | The Card payments list (Card Terminal Payment) |
| What a distributor has invoiced, including orders placed outside the POS | Distributor Orders → Track Orders → Invoices |
| Which sales a human vouched for rather than the system | Hand-Asserted 4473 Dispositions |

[Reports](../guide/reports/index.md) covers the filters and the columns. Two of these carry a legal deadline rather than a preference: Pending 4473 Orders and Pending Transfer Pickups, which ages guns the store is holding for someone else.

## The work queues

If you are asked what needs doing, start here.

**The [Dashboard](../guide/getting-started/dashboard.md)** is where staff land. Its blocks are the store's own summary of what is open: 4473s waiting, guns waiting on pickup, the cash drawer, records not yet in the bound book, and distributor orders arriving.

**The notification bell** carries NICS verdicts, new web and GunBroker orders, a distributor shipping to the store, and a bound-book refusal, each linked to its document. It is per person.

**[Pending Order](../guide/selling/pending-orders.md)** at `/app/pending-dispositions`, labelled **Orders** in the sidebar, merges counter orders, WooCommerce orders and GunBroker orders into one list. What a row can do depends on which of the three it came from. GunBroker rows deliberately cannot be edited or cancelled here.

**[Distributor Orders](../guide/distributors/restock.md)** at `/app/distributor-restock` holds what is being ordered in from a wholesale house. Its **Track Orders** tab lists every open order.

**Receive Goods → Incoming shipments** at `/app/receive-goods` lists distributor parcels on their way to the store, including parcels ordered outside the POS.

**The [cash drawer](../guide/selling/cash-drawer.md)** at `/app/cash-drawer` lists **Purchases from private sellers with no seller payment recorded**.

## Doing

Each row is a workflow, its entry point, and the thing about it that most often goes wrong.

| Job | Entry point | Watch for |
|---|---|---|
| Book stock in | `/app/receive-goods` | A firearm cannot be received from a supplier that is not a verified FFL dealer. The page says so plainly and refuses. |
| Take a gun in trade | The **Trade-in** button on the register | Paying the seller now records a receipt from the seller with the payment: Cash, Zelle, Check or ACH, with a reference unless it is cash. A trade-in applied as credit still has a federal record due by the next business day, so a parked draft is a deadline, not a to-do. |
| Sell a firearm at the counter | `/app/point-of-sale` | The sale cannot complete until the form and the check are recorded. Do not attempt to route around that. |
| Build an order that ships | `/app/create-order` | The destination license is verified as part of the flow. A firearm never ships to an unverified or expired one. |
| Finish an order | `/app/pending-dispositions` | Disposing books the federal record and removes the gun from stock. It is the hardest step in the whole system to undo. |
| Close the day | `/app/cash-drawer` | One count closes the day. Cash records are undone on this page only, never from accounting screens. |
| Put stock on a sales channel | The Item or Serial No form | A model is listed from the Item. One specific gun is listed from its Serial No. Choosing the wrong one lists the wrong thing. |
| Order from a distributor | `/app/distributor-restock` | Every order waits in Draft until a person confirms it. Placement cannot be taken back from here. |
| Receive a distributor order | **Receive** on the Distributor Order | An order with a firearm opens Receive Goods with its lines staged, for a person to check each gun and submit. It is never submitted for you. |

## Marking a record as not in the bound book

Sometimes a submitted acquisition or disposition should never reach the bound book at all. The usual case is an antique, a receiver made before 1899, booked by mistake: FastBound refuses it because it never acquired the gun. Such a record keeps showing on the Dashboard's **Not in the bound book yet** list, in the daily digest of stuck records, and in the daily retry.

A manager can mark it as not belonging in the bound book. There is no button in the POS for this. It is a server method, called from an AI client connected to the store's full MCP server (not the read-only one), by a person signed in as a Stock Manager or System Manager:

| Method | Arguments |
|---|---|
| `ffl_integrations.fastbound.reconcile.mark_not_in_bound_book` | `doctype` (`FFL Acquisition` or `FFL Disposition`), `name`, `reason` |
| `ffl_integrations.fastbound.reconcile.clear_not_in_bound_book` | `doctype`, `name` |

On the connector the call goes through the generic method tool, `frappe_run_method`.

**What marking does.** The record's FastBound sync error becomes `[NOT A&D] <reason>`. From then on the daily retry, the stuck digest and the Dashboard leave it alone. The record itself is not cancelled, because its stock movement is right. A comment on the record keeps who marked it, the reason, and the sync error it had before.

**What clearing does.** It undoes the mark when the record turns out to belong in the bound book after all. The sync error is emptied, so the daily retry pushes it again, and the Dashboard lists it until it lands. A comment records who cleared it.

The method refuses a reason left blank, a record that is not a submitted FFL Acquisition or FFL Disposition, and a record that already has a FastBound ID. The staff-side steps are on [FastBound](../guide/compliance/fastbound.md).

!!! warning "A federal record decision"
    Marking a record keeps it out of the bound book for good. Do it only on a
    person's explicit instruction, for a record that person has checked, and
    put their reason in the call. The connector does not ask for a second
    confirmation on this method, so state the record and the reason and wait
    for a yes before you call it. Never mark a record to make the Dashboard
    list go away.

## What happens that you did not ask for

The system acts on its own, and some of what you observe will be its work rather than a person's. [What runs automatically](../guide/reference/background-jobs.md) is the full schedule. The ones most likely to confuse a reading of current state:

- The register closes itself at night, so a shift left open is usually closed by morning.
- Orders arrive from the outside channels on a poll, not instantly.
- A reconcile compares the local bound book against FastBound daily, and its alerts are how a push failure surfaces.
- Distributor order statuses, tracking and serials arrive on a poll, and distributor invoices on an hourly pull.

## Failure

[Troubleshooting](../guide/reference/troubleshooting.md) maps the messages staff actually see to what they mean. Two habits matter more than the list.

**Read the refusal as information.** This system blocks things deliberately and says why. A refusal to receive a firearm, to ship to a dealer, or to complete a sale is the control doing its job. The correct response is to report the blocker, never to look for a path around it.

**A failed write may have left nothing behind, or may not have.** Ask what actually landed before retrying. A retry that assumes the first attempt did nothing is how one gun becomes two records. On a remote connector, the Connector Audit Log shows whether the call started and how it ended.

## Scope

Nothing here authorizes an action. Whether you may act at all, and on which site, comes from whoever is running you. Where this page and that instruction disagree, stop and ask.
