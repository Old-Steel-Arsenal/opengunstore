# Distributor setup

For managers and the administrator, when you connect a wholesale house, switch it on or off, set which house the system prefers, or check why a house is not answering.

A distributor is a wholesale house you buy from, such as RSR or Sports South. The POS holds one **Distributor** record per house, with its own credentials and switches.

Everything ordered from a house becomes a **Distributor Order**, numbered like `DO-2609-0001`: the year and month it was made, then a running number. Purchasing staff place and track orders on [Order from a distributor](../stock/distributor-orders.md).

## Two kinds of order

| Order Type | Where the goods go | When |
|---|---|---|
| **Stocking** | Your store, into inventory | Restocking the shelf |
| **Procure To Store** | Your store first, then the customer | The customer has bought it and the goods pass through your hands |

## Where to find it

| What | Where |
|---|---|
| Ordering and tracking | **Distributor orders** under **Purchasing** in the sidebar |
| What is on its way | The **Arriving** card on the [Dashboard](../start/dashboard.md) |
| Setting up a house | Open **Settings**, then **Distributors** under **Integrations** |

## What the POS will not do

Cancelling a distributor order in the POS never reaches the house. The system has no way to cancel an order with a distributor. Once an order is live at the house, stopping it takes a phone call or an email, so every order waits in **Draft** until a person confirms it.

## The distributors hub

The **Distributors** page shows one card per Distributor record. Anyone with a purchasing or stock role can read it. Changing a switch or the order needs System Manager or Purchase Manager.

Across the top of a card sit the priority number, the house name and its provider code, and the linked Supplier when the record names one. A house whose provider code has no integration behind it is marked **not registered** in red, and its **Test Connection** button is disabled.

### The switches

| Switch | On means |
|---|---|
| **Enabled** | The house is in use. Off, it is not searched, quoted or ordered from |
| **Stocking** | You may raise stocking and procure-to-store orders |
| **Poll** | Every 30 minutes, status, tracking and serials are pulled for its live orders |

A card with **Enabled** off is greyed. The switches save the moment you click them, and a failure puts the box back where it was.

### The summary line

For RSR, Sports South and Lipsey's, a read-only line under the switches sums up the house's own settings page. Lipsey's shows only **Ordering**.

| Item | Reads | Meaning |
|---|---|---|
| **Ordering** | **live** | **Enabled** is ticked and **Allow live orders** is on |
| **Ordering** | **off (disabled)** | **Enabled** is unticked on this card |
| **Ordering** | **off (Allow live orders is off)** | The switch on the settings page is off |
| **Ship to store** | **free over $500.00** | The freight rule for orders shipped to your store |

**Ordering** reads **live** only when both layers are on; otherwise it names the layer that is off, or both. While **Allow live orders** is off, nothing is sent to the house.

The **Ship to store** wording follows the house's rules. RSR reads **free** or **free over** an amount. Sports South gives one threshold for handguns and one for everything else.

Click **Ordering** to open the settings page on its Account tab, or **Ship to store** to open it on the Freight tab. See [Integrations](integrations.md).

### The health line

| Item | Tells you |
|---|---|
| **Quantity feed** | How long ago the live quantity heartbeat arrived |
| **Catalog** | How long ago the catalog content heartbeat arrived |
| **Last order poll** | When this house's orders were last polled. Only while **Poll** is on |

Any of these can read "never". When the provider cannot be reached, the health line is replaced by the error in red, and the rest of the card still draws. **Catalog: off** means catalog reads are off for that house, which is a state and not a fault.

When Data Service Settings has **Catalog source** set to **Local**, a further line shows **Local catalog** with the offers loaded and, for each feed, when it last succeeded, how many rows it read and whether the last run failed.

### The order line

A clickable summary reads **N draft**, **N in flight** and **N need attention**, and opens the Distributor Order list filtered to this house. In flight covers Queued, Placed, Acknowledged and Partially Shipped. Need attention covers Hold and Error, and turns red when it is not zero. Complete and Cancelled orders are not counted.

### The buttons

| Button | What it does |
|---|---|
| **Test Connection** | Runs the provider's live probe and shows each check with a green, red or grey dot. Read-only; places no order |
| **Sync now** | Queues every due feed of a house read from the local catalog. Local catalog mode only, System Manager only |
| **Pricing & stock** | Opens the Distributor record |
| **Account & shipping** | Opens the provider's own settings page: account, ordering and freight. Only when the provider has one |
| **Add Distributor** | At the bottom of the page. Asks which provider, then opens a new Distributor form |

## Priority

Priority orders the cards, lowest number first, and that is the order the system prefers. It sets the column order in the fulfillment plan on Create Order and picks the default house when nothing else names one. On a cost tie in the plan, the higher-priority house wins.

Change it with the small **↑** and **↓** buttons in a card's header; they appear once there is more than one house. Houses on the same number sort oldest first, so a never-ordered house at 0 sits at the top. Moving a card rewrites every house's priority as 1 through N.

## The Distributor record

**Pricing & stock** opens the house's own record:

| Tab | Holds |
|---|---|
| **General** | **Code**, **Name**, **Provider**, **Supplier**, the switches (the last is labelled **Poll orders** here) and **Priority** |
| **Catalog service** | **Endpoint**, **Client ID** and **Secret**, for a house read from its own catalog service. Blank uses the site-wide connection on Data Service Settings |
| **Status** | **Last order poll** |

**Supplier** is the vendor on receipts and cost invoices for this house. A house with no Supplier cannot be received from a distributor order.

## The Distributor Order record

The order record carries the house, the order type, the account, the lines, the addresses, and everything the house has echoed back: invoice, tracking and serial numbers. It is not a submittable document; it walks a status field instead (see [the status walk](../stock/distributor-orders.md#the-status-walk)).

| Field | Shows |
|---|---|
| **Distributor**, **Order Type** | The house and the lane: **Stocking** or **Procure To Store** |
| **Account** | The account at the house the order is placed on |
| **Distributor Order No.** | The house's own number. Status polls and holds are keyed by it |
| **Fill or Kill** | RSR only. Ticked, a line RSR cannot fill is dropped instead of back-ordered. Decided at placement |
| **Shipping service** | The carrier service sent to the house, where it offers a choice |
| **Delivery options chosen at the counter**, **Signature**, **Adult signature**, **Insurance** | Sports South only. The options picked on Create Order |
| **Status**, **Hold / Cancel Reason** | Where the order is, and why it is held or was cancelled |
| **Receive Status** | **Not Received**, **Partial** or **Received** |

Each line carries the SKU, the quantities ordered, acknowledged, invoiced and received, the invoiced unit cost, and the invoice, tracking and serial numbers the house reported.

Placement runs in the background after **Confirm & Queue**. The placement worker is idempotent, so a retry cannot place the same order twice. An order in **Error** can be queued again once the cause is fixed.

## Troubleshooting

| You see | Fix |
|---|---|
| A card marked **not registered** | The provider code has no integration installed. Check the code on the Distributor record |
| Distributor _House_ has no Supplier set, so _DO-…_ cannot be received. … | Set **Supplier** on the Distributor record |
| The health line shows an error in red | The provider cannot be reached. Run **Test Connection** and check the house's settings page |
| Email `Distributor feed stale: <name>` (heartbeat older than 90 minutes) | Read the **Quantity feed** and **Catalog** ages on the card and run **Test Connection**. With a Data Service catalog, test Data Service Settings too. Stale at the source is the house's problem; fresh at the source but stale here is this site's sync |
| … has _N_ order(s) still on the way (on Receive Goods) | A house's supplier was picked by hand while it has open orders. Receive the parcel from **Incoming shipments** if it is one of them |
| `Invoice <n> belongs to order <order> — receive it there.` | Receive it under that order in **Incoming shipments** |
| `Invoice <n> looks like the parcel for open order <order> …` | Receive that order instead, or finish or cancel the order first |
| `Invoice <n> <SKU>: N unit(s) scanned but only M left to receive on it.` | Check the count against the packing slip |
| No unit cost on _N_ line(s) of this order: … | Enter the cost on the order line from the house's invoice, then receive again |
| Nothing new to receive. | Everything invoiced is already booked. Check **Receive Status** on the order |
