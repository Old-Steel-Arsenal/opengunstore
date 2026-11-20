# Move your inventory in

For the administrator and the bookkeeper of a store that is starting on OpenGunstore, when you bring over what is already on the shelves.

A migration has three parts. Distributor products need no file at all. Firearms come from the bound book. Everything else comes from one CSV file.

| What | How it comes in |
|---|---|
| Products you will buy from Sports South, Lipsey's or RSR | Created from the distributor's catalog the first time you order or receive them |
| Firearms you have now | From FastBound, with **Import Bound Book** |
| Accessories, ammunition, optics and parts you have now | From a CSV file, with **Inventory Import** |

## Before you start

- Pick your **cut-over date**: the day your old system's counts are as of. Use the close of business the day before your first sale on OpenGunstore.
- Stop selling in the old system once you export from it. Anything sold after the export puts the counts out of step.
- Have System Manager. Every step below is under **Settings**.

## Distributor products

You do not upload distributor products. When you search a distributor's catalog and order or receive a product, the system creates the item with its brand, model, part number, UPC, caliber, category, cost, MAP and MSRP. A product is matched by UPC, so it is never created twice.

The catalog comes from one of two sources, set in **Data Service Settings** as **Catalog source**:

| Catalog source | What it is |
|---|---|
| **Local** | The system downloads each distributor's feed with your own dealer account and keeps the catalog in your database |
| **Data Service** | A subscription catalog with a curated category tree, cleaned-up titles and descriptions, and one product list across distributors |

Connect each distributor first. See [Distributor setup](distributors.md) and [Integrations](integrations.md).

## Firearms

If your bound book is in FastBound, connect FastBound, then click **FastBound**, then **Import Bound Book**. Use the same **Cut-over Date**, and run it with **Dry Run** first. It creates each gun's item, serial number and acquisition at its FastBound cost. A gun with no cost in FastBound is listed, not imported. See [FastBound](fastbound.md#import-bound-book).

If your bound book is somewhere else, receive each gun on [Receive goods](../stock/receive-goods.md), or ask us about the migration service below.

Firearms never go in the CSV. A firearm row is refused and the report says where to bring it in.

## Everything else: the CSV file

### Fill in the template

1. Open **Settings**, then **Inventory Import** under **Catalog Reference**.
2. Click **Download Template**.
3. Fill in one row per product. Format the UPC column as text before you paste, or a spreadsheet drops leading zeros and turns long numbers into `8.5E+11`.

| Column | What to put in it |
|---|---|
| **UPC** | The barcode number. Rows are matched to existing items and to the distributor catalogs by UPC |
| **Name** | The product name. Needed when no distributor lists the UPC |
| **Brand** | The maker |
| **Part Number** | The manufacturer's part number. Used as the item code when there is no UPC |
| **Category** | One of the store's categories, such as `Magazine`, `Optic` or `Rifle Ammo`. Left blank, the item goes under `Misc Accessory` |
| **Quantity** | Units on the shelf at the cut-over date. Whole numbers |
| **Unit Cost** | What one unit cost you, as your old books carry it. Required |
| **Price** | Your selling price. Optional |

Put each product on one line. Two lines with the same UPC are both refused; add the quantities into one line and run it again.

### Preview and import

1. Attach the file under **CSV File**.
2. Pick the **Warehouse** the stock goes into and the **Cut-over Date**.
3. Click **Preview**. Nothing is written. Each row is shown as an existing item, a new item from a distributor catalog, a new item from your file, skipped, or an error.
4. Fix the errors in your file and preview again until the count and the total at cost match your old system's inventory value.
5. Click the import button, which shows how many rows will come in, and confirm. The import runs in the background, and the page shows the result when it finishes.

Click **Download Report** to keep the row-by-row result.

### What happens next

- Every imported product is on hand in the warehouse at the cost in your file, as of the cut-over date.
- A product with any stock history in the system is skipped. Running the same file again imports nothing new.
- The file is attached to the opening stock record, with a note of the row count, the total at cost and the cut-over date.

### Troubleshooting

| You see | Fix |
|---|---|
| "UPC ... was turned into a number by a spreadsheet" | Format the UPC column as text, paste the numbers again and export |
| "Category ... is not one of the store's categories" | Use a category from **Item Group**, or leave it blank |
| "... already has stock history in the POS, so it is left out" | The product was received or counted before. Ask your bookkeeper how to add it |
| "The books are frozen through ..." or "closed through ..." | Pick a cut-over date after that date |
| "... has no Temporary Opening account" | Add an account of type Temporary to the chart of accounts |

## Other ways to bring the file in

- **An AI assistant.** Connect Claude or ChatGPT to the store with the full address from **MCP Settings** (see [Connect your assistant](../ai/connect.md)), attach your file, and ask it to bring the products in. Start with ten rows and check them before the rest.
- **Our migration service.** Send us the export from your old system as it is, and we import it for you.

## For the bookkeeper

The CSV import posts one opening stock entry for every 500 rows, and the FastBound import one per gun. Each debits the stock account and credits Temporary Opening, dated the cut-over date and marked as opening entries. Sales and cost of goods sold are not touched.

After the import, agree the total with the old system's inventory valuation at the cut-over date, counting only stock the store owns. Then clear Temporary Opening with one journal entry on the cut-over date: debit Temporary Opening, credit Opening Balance Equity. Temporary Opening should read zero before you go live.
