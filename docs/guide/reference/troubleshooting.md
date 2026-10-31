# Troubleshooting

Start with the symptom table, then read the section it points to. Every message
quoted here is the wording the system actually uses.

!!! warning "Never work around a compliance refusal"
    A refusal about a 4473, a NICS answer, the bound book or an FFL license is a
    federal control, not a bug. Fix the underlying record. If the control is
    genuinely wrong, get a manager, who has an audited override for exactly
    that case.

## Symptom table

| What you see | Where | Go to |
|---|---|---|
| Cannot receive a firearm, supplier is not an FFL dealer | Receive Goods, Purchase Receipt | [Receiving is refused](#receiving-is-refused) |
| Cannot ship, destination FFL is expired or not on file | Create Order, invoice submit | [Shipping is refused](#shipping-is-refused) |
| Order will not submit, 4473 or NICS not clear | Register, Pending Order | [An order is stuck awaiting a 4473](#an-order-is-stuck-awaiting-a-4473) |
| A gun is missing from the bound book | Daily alert email, Serial No | [A FastBound push failed](#a-fastbound-push-failed) |
| A gun is not on the website or the marketplace | Item, Serial No | [A listing did not go out](#a-listing-did-not-go-out) |
| Distributor quantities look wrong or frozen | Distributors hub, alert email | [A distributor feed is stale](#a-distributor-feed-is-stale) |
| A card charge hung, or the amount is unclear | Register | [A card charge went wrong](#a-card-charge-went-wrong) |
| A serial shows as sold in the wrong place | Register, Firearms In Stock | [A serial is sold in the wrong place](#a-serial-is-sold-in-the-wrong-place) |
| A GunBroker order is disposed but has no Ship button | Pending Order | [A GunBroker order is disposed but cannot ship](#a-gunbroker-order-is-disposed-but-cannot-ship) |
| An email says card charges are no longer captured | Alert email, invoice comment | [A card charge was voided outside the POS](#a-card-charge-was-voided-outside-the-pos) |
| The register refuses a sale held for a web order | Register | [Other messages](#other-messages) |
| A cash drawer action is refused | Cash drawer page | [Other messages](#other-messages) |
| An AI connector or extension update is refused | MCP client, Extensions page | [Other messages](#other-messages) |

## Receiving is refused

**What you see.** One of three messages when you submit a receipt with a firearm
line:

```
Cannot receive firearm from supplier '<name>' — not registered as FFL Dealer.
Open the Supplier form and use 'Search ATF' to populate FFL information.
```

```
Supplier '<name>' is marked as FFL Dealer but has no FFL Number on file.
```

```
Supplier '<name>' EZ Check status is '<status>'. Re-verify the FFL before
receiving firearms.
```

**What it means.** A firearm may only be acquired from a licensee, so the
supplier record has to say who they are and their license has to have passed
verification. The three messages are the three ways that fails: nobody has
marked them as a dealer, they are marked but the number is blank, or the number
is on file and its last check did not come back acceptable.

**What to do.** Open the Supplier form. Use **Search ATF** to look the license
up and fill the fields in. If the license is on file already, run the
verification again. A license that has genuinely expired needs a current copy
from the supplier before anything is received.

**Escalate to.** A manager if the supplier insists the license is current and
verification keeps failing. See
[FFL verification](../compliance/ffl-verification.md).

## Shipping is refused

**What you see.** One of three messages:

```
Destination FFL <number> has no valid (unexpired) license on file — run Verify
FFL before shipping.
```

```
Destination FFL <number> is missing a complete address (street + a valid state)
— open the FFL and complete it (or run Verify FFL) before shipping, so sales
tax is computed correctly.
```

```
Destination FFL <number> is not on file — verify it first.
```

**What it means.** In order, the license we hold has expired, the record has no
usable street and state, or there is no record at all. The address matters
beyond the shipment, because sales tax is computed from where the gun goes.

**What to do.** Open the FFL record and run **Verify FFL**, which refreshes the
name, address and expiry from the ATF. If verification cannot find it, ask the
receiving dealer for a current copy of their license and enter it by hand.

For a Curio and Relic buyer the messages are different, because a Type 03
license cannot be looked up and a person confirms it instead:

```
C&R license <number> expired on <date>.
```

```
<number> is not a Type 03 (Curio & Relic) license — the fourth group must be
03, in the form 1-23-456-03-7C-89012.
```

A C&R gun must ship to the licensed premises, so a state or ZIP that disagrees
with the license is refused as a **C&R Address Mismatch**.

**Escalate to.** A manager. Do not ship on a license the system has refused.

## An order is stuck awaiting a 4473

**What you see.** On submit:

```
Cannot submit firearm transfer: 4473 form status is '<status>', must be
'Completed'.
```

```
Cannot submit firearm transfer: NICS status is '<status>', must be 'Proceed'.
```

Each message continues with what to do next, and the ending depends on how this
store handles 4473s. With FastBound it tells you to click **FastBound 4473**,
complete the form there and wait for the webhook to mark the order Completed.
In Paper 4473 Mode it tells you to complete the paper form, run the background
check, then click **Record Paper 4473** on the payment screen.

There is a third message that catches a gun added, or a serial swapped, after
the 4473 was finished:

```
Cannot submit: firearm serial(s) <serials> have no completed 4473 with NICS
Proceed on this order. Each firearm must clear its own 4473 first.
```

**What it means.** Every gun leaves against its own completed form and its own
background check. The order-level status is not enough on its own.

**What to do.** Find the order on the
[Pending 4473 Orders report](../reports/index.md) and read its NICS column. A
Delayed order waits until it clears or until its Brady date arrives. A Denied
order does not complete, and the gun goes back on the shelf. A missing 4473
means somebody needs to run it. If a gun was added after the fact, run a 4473
for that gun.

**Escalate to.** A manager. Managers have an audited override for a 4473 that is
genuinely finished but stuck electronically. Every override lands on the
[Hand-Asserted 4473 Dispositions report](../reports/index.md) with the name of
who did it and the reason they gave.

## A FastBound push failed

**What you see.** A daily alert email with the subject `<n> FastBound record(s)
stuck unsynced`, listing each acquisition or disposition and its error. Entries
tagged `[needs human]` will not retry themselves. On the record, the
**FastBound Sync Error** field holds the same text and the FastBound ID is
empty. The Dashboard's A&D card counts the record under **Not logged**, and
**Review _N_ not logged** lists it as **Will retry** or **Needs a person**. When
FastBound refuses a record for good, the managers' notification bell also rings:
`A&D book: FastBound refused <record type> <record> — <reason>`.

**What it means.** The bound book entry exists here but never reached FastBound.
Retries run daily, so a transient network error clears on its own. A rejection
does not: FastBound refused the data and will refuse it again.

**What to do.** Open the record and read the error. The common cause is a
missing required field, which FastBound rejects outright. Fill it in and let the
daily reconcile retry, or push it again from the record. On a Serial No,
**Sync with FastBound** pushes that one gun.

A record whose sync error begins `[NOT A&D]` was checked by a manager and marked
as not belonging in the bound book, such as an antique booked by mistake. It is
never retried and drops off the Dashboard list. See
[FastBound](../compliance/fastbound.md).

To find divergence rather than failure, open
[Firearms In Stock](../reports/index.md) and use **Check FastBound
differences**, which scans in-stock guns field by field and lists any that
disagree.

**Escalate to.** A manager same-day if the record is a disposition. A gun that
has left the building with no bound book exit is a recordkeeping gap, not a
sync backlog.

## A listing did not go out

**What you see.** The gun is in stock here but is not on the site or the
marketplace, and no error was shown at the time.

**What it means.** Pushes are queued and can fail after the click. Each channel
records its own state on the Serial No or the Item:

| Field | What it tells you |
|---|---|
| GunBroker Push Pending | A listing went out and the result was never confirmed. A second attempt is blocked on purpose, because GunBroker enforces no uniqueness and a hopeful retry creates two live listings for one gun. |
| GunBroker Push Attempted At | When that unconfirmed attempt happened. The grace period runs from here. |
| GunBroker Delisted | Somebody ended this listing. No automatic path ever re-lists a gun carrying this flag. A person does, by pressing Send to GunBroker, which also clears it. |
| GunBroker Price Dirty | The price changed and GunBroker has not been told. The next sweep publishes it. |
| GunBroker Missing Count | How many consecutive sweeps found no such listing. |

For WooCommerce, failures surface as a message beginning `WooCommerce sync
failed:`, `WooCommerce delist failed:` or `WooCommerce API error:` followed by
the store's own reason, and the settings form's **Last error** field on the
Status tab keeps the most recent one.

**What to do.** Read Last error on the channel's settings page first. Then push
the single item or serial again from its own form. One GunBroker refusal has a
specific fix:

```
Serial <serial> cannot be listed: <reason>.
```

It names the listing term that is unusable, most often a price or a duration.

**Escalate to.** A manager if a GunBroker push has been pending for more than a
day, since the recovery scan should have settled it. See
[GunBroker](../channels/gunbroker.md) and
[WooCommerce](../channels/woocommerce.md).

## A distributor feed is stale

**What you see.** An alert email titled `Distributor feed stale: <name>` saying how old the heartbeat is against a 90 minute threshold and that the published quantities are no longer trustworthy. On the [Distributors hub](../distributors/hub.md) the quantity feed and catalog heartbeats show the same age.

**What it means.** The distributor's quantity file has not refreshed. Anything the store shows from it is a guess.

**What to do.** On that distributor's hub card, read the **Quantity feed** and **Catalog** ages and press **Test Connection**. If the catalog comes from a Data Service, press **Test Connection** on Data Service Settings too. A feed that is stale at the source has nothing to fix on this site. A feed that is fresh at the source but stale here points at this site's own sync.

**Escalate to.** A manager, and a distributor rep if the error points at their end.

## A card charge went wrong

**What you see.** Either the terminal never came back and the register is still
waiting, or the sale went through with the amount split across more than one
card and the totals do not look right, or a message saying the gateway moved
money but the system could not record it.

**What it means.** Three different situations, and they are not
interchangeable:

| Situation | What is true |
|---|---|
| Timed out or cancelled | The instruction may or may not have captured. The system follows the gateway's own record before deciding. |
| Partially approved | Part of the amount was taken. The register tracks how much of the card total remains and refuses a second charge that would exceed it. |
| Money moved, record failed | The gateway definitely took the money and the system definitely could not write it down. |

**What to do.** For a timeout, let the register finish resolving. It reads the
outcome from the gateway rather than assuming. For a partial approval, take the
remainder on another tender. The register will refuse an amount larger than what
is left on the card total, and it will refuse a second charge on an invoice that
is already fully paid by card.

For the third case, stop. The message tells you not to retry the refund or the
void, because that would move money again, and to write down the invoice number
for the back office. An alert email also goes out. Reconcile against the Payroc
portal by hand before anything else is attempted.

A card already run on the Payroc Virtual Terminal is recorded, not charged
again: on Record Payment tick **Already charged on Virtual Terminal** and type
the approval code. See [Taking payment](../selling/payments.md).

A void or refund the processor did not confirm locks that charge until a manager
checks the portal and clicks **Resolve card reversal**. See
[Card refund on a counter order](../selling/payments.md#card-refund-on-a-counter-order).

**Escalate to.** A manager immediately for the third case. A refund or a void
needs manager rights in any case.

## A card charge was voided outside the POS

**What you see.** An alert email titled `Payroc: <n> card charge(s) no longer
captured at the gateway`, and a comment on each invoice it names.

**What it means.** The POS booked the charge as paid, but Payroc no longer holds
it as captured. Someone voided or refunded it on the terminal's own menu or in
the Payroc portal, and the POS never heard. The daily check reads the last
week's charges back from Payroc to catch exactly this.

**What to do.** Confirm with the operator named in the email what happened. Then
correct the books: collect the payment again, record the cash or other tender
that replaced it, or write it off. The check never changes the money itself.

**Escalate to.** A manager and the bookkeeper. It is an accounting decision.

## A serial is sold in the wrong place

**What you see.** At the register, on a firearm line:

```
Serial <serial> already has a buyer on GunBroker — do not sell it at the
counter. Cancel this line.
```

```
Serial <serial> has bids in a GunBroker auction — it is promised to the high
bidder until the auction ends. Cancel this line.
```

From a web order, a serial row carrying a reservation error such as `No Active
unreserved serial available for '<item>' to hold stock — staff action
required`.

**What it means.** Another channel got there first. The register checks
GunBroker before letting a listed gun be sold at the counter, and a gun with a
live bid is promised to that bidder until the auction ends. On the web side, the
order arrived and no free unit was available to hold for it.

**What to do.** Cancel the line and tell the customer. Do not sell a gun that
another channel has taken, and do not end an auction that has bids to free a gun
up. For a web order with no available unit, find a replacement unit or contact
the buyer. The retry sweep re-attempts reservations that failed for a transient
reason, but it deliberately does not retry a genuine out-of-stock.

The opposite case also happens: a gun that is sold or gone but still shows as
in stock here. Run **Sync Bound Book** on
[Firearms In Stock](../reports/index.md). It lists every in-stock gun FastBound
reports as disposed and offers to take them out of stock.

**Escalate to.** A manager before removing anything from stock, and before
touching an auction that has bids.

## A GunBroker order is disposed but cannot ship

**What you see.** After Dispose, an orange message reads **Disposed … firearm(s),
but the invoice did not post. Use Create Invoice on the card.** On the Pending
Order card there is a **Create Invoice** button and no **Ship** button.

**What it means.** The bound book exit was written, so the guns are out, but the
order's invoice did not post.

**What to do.** Click **Create Invoice**. **Invoice … posted — this order can now
ship.** means **Ship** is back. **Invoice … was left in Draft. Open it to see
why.** means open that invoice and read the reason. See [GunBroker](../channels/gunbroker.md#working-the-order).

**Escalate to.** A manager if the invoice stays in Draft.

## Other messages

Refusals that can come up on several pages. Messages that belong to one page are
in that page's own Troubleshooting table.

| Message you see | What it means | Fix |
|---|---|---|
| _Item_: selling _N_, only _M_ free — the rest is held for web order(s) _#…_. Ship those units to the web customer instead, or ask a manager to ring this sale. | The shelf units are promised to paid web orders | Ship them to the web customer. A Sales Manager can ring the sale, and the invoice notes it |
| _Slot_ already holds _serial_. A slot holds one firearm: pick a free slot, or move _serial_ out first. | A numbered slot takes one gun | Pick a free slot, or move the other gun. See [Storage locations](../inventory/storage-locations.md) |
| _Slot_ is a single-firearm slot, and _item_ is not a firearm. Use a display case, rack or safe instead. | Slots are for guns only | Put it in a shelf, case or safe location |
| Today's cash was already counted at _time_ (_amount_). A manager can undo that count if it was wrong. | The day is already closed | A manager uses **Undo the last count** on the [cash drawer](../selling/cash-drawer.md) page |
| Cash entries cannot be deleted. Undo the entry instead. | Cash records are permanent | Use **Undo** on the cash drawer page |
| _Record_ is a 4473 record — it cannot be deleted. | 4473 records are permanent | Nothing to do. See [4473 records](../compliance/4473-records.md) |
| Say how the seller was paid (Cash, Zelle, Check, ACH), or apply the trade-in as a credit to a sale. | A trade-in needs either a seller payment or a credit | Pick how the seller was paid, or apply it as credit. See [Trade-ins](../inventory/trade-ins.md) |
| The cpa MCP server is switched off in MCP Settings. (or "full") | That AI connector is off for this store. `cpa` is the read-only connector | A System Manager ticks it in MCP Settings. See [The MCP server](../../ai/mcp.md) |
| **Cannot update** (Extensions page) | The new version cannot be applied. The dialog says why, for example that the package does not pass the checks | Read the reasons in the dialog, and ask your developer |

## Where alerts land

Three places.

- **Email.** Operational alerts go to the addresses in **Alert Recipients** on
  [FFL Settings](../settings/ffl-settings.md), and to every System Manager when
  that field is empty. Subjects start with `[POS Alert]`. Repeated failures are
  throttled, so one broken thing sends one digest rather than one email per
  attempt.
- **An assigned task** on the stuck document itself, raised once and only once
  until a person closes it.
- **The notification bell** in the app, never emailed: NICS verdicts, new web
  and GunBroker orders, a distributor shipping to the store, and a record
  FastBound refused for good. See
  [the Dashboard](../getting-started/dashboard.md).

See [What runs automatically](background-jobs.md) and
[Daily checks](../compliance/daily-checks.md).
