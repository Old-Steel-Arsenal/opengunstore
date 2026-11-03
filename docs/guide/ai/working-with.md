# Working with an assistant

For anyone using a connected assistant day to day. It explains how to ask so the answer is right the first time, and gives examples you can copy for the counter, managers and the bookkeeper.

## How to ask

An assistant works best with the same things a new coworker would need.

- **Say what you want and for when.** "Sales for last week" leaves it to guess; "Sales from Monday Oct 27 to Sunday Nov 2, firearms and accessories separately" does not.
- **Name the thing exactly.** Give the serial number, the customer's full name or phone number, the order number or the SKU. A gun described as "the black Glock" may match several.
- **Ask for the format you need.** A short list, a table, totals only, or a draft email.
- **One change at a time.** Ask it to look first, read what it found, then ask for the change.
- **Ask where a number came from.** "Which report did you use?" lets you check it on screen.

## When it wants to change something

Before any change, the assistant stops and tells you what it is about to do, then waits for you. This is the moment that matters.

1. Read the gun, the serial, the customer and the amount it names.
2. If anything is wrong, say no and correct it: "No, the serial is BX4471, not BX4417."
3. If it is right, say yes. It then does the step and tells you the result.
4. Check the result on screen the first few times.

Never tell it to skip the confirmation. If it did something you did not expect, stop and tell your manager; the **Connector Audit Log** shows exactly what it did.

## Examples at the counter

These work on the full connection unless marked read-only.

| You ask | What it does |
|---|---|
| "Do we have any Glock 19s in stock? Show the serials and prices." | Looks up firearms in stock (read-only works) |
| "Find the customer with phone 555-0142 and show their open orders." | Finds the customer and their orders (read-only works) |
| "Where is serial BXTR422 in the store?" | Reads its storage location |
| "Which transfer guns are waiting for pickup, oldest first?" | Lists the pending transfer pickups |
| "What is in the cash drawer right now?" | Reads today's drawer |
| "Record a $200 cash deposit from Dale Whitaker on his open order." | Finds the order, tells you what it will record, waits for your yes |
| "List serial BXTR422 on the website at $549." | Tells you the gun and price, waits, then lists it |

## Examples for managers

| You ask | What it does |
|---|---|
| "Give me today's store overview: sales, open orders, anything stuck." | Reads the overview and the queues |
| "Which web orders are still waiting, and on what?" | Lists the pending web orders and what each one waits for |
| "Compare firearm sales this month with last month, by brand." | Builds the comparison from the Sales Report |
| "Count the firearms in stock by manufacturer." | Reads Firearms In Stock and totals it |
| "Are there bound book mismatches I should look at?" | Reads the bound book check and explains each row |
| "Start a stocktake count for ammunition only." | Tells you what count it will create, waits, then starts it |
| "Close today's cash drawer. The count is $1,240." | Shows the expected amount and the difference, waits, then closes it |

## Examples for the bookkeeper

Use the read-only connection for all of these.

| You ask | What it does |
|---|---|
| "How much sales tax did we collect in October, and what do we owe?" | Reads the sales tax liability for the month |
| "Profit and loss for October, compared with September." | Reads the financial statements |
| "List every card payment and refund from Oct 1 to Oct 31 and flag any that do not match the POS." | Reads the card payments ledger |
| "Which customers owe us money, and how long overdue?" | Reads aged receivables |
| "What inventory did we receive in October, by supplier and cost?" | Reads Inventory Receipts |

For the month-end steps themselves, follow [Month-end close](../admin/month-end.md) on screen; the assistant can gather the numbers for each step.

## Work that saves the most time

- **The morning check.** "What needs attention today?" reads the same overview as the Dashboard: sales, open 4473s, pickups waiting, the cash drawer, the bound book and parcels on the way.
- **Answering a customer quickly.** "Do we have a 9mm compact under $500, and when did the last one sell?"
- **Writing.** "Draft a text to Dale Whitaker saying his transfer is ready for pickup." The assistant drafts; you send it yourself.

## What to keep away from it

- Card numbers, passwords, Social Security numbers and full ID numbers. Do not type them into the chat.
- Decisions the law or the owner gives to a person: approving a sale after a delayed or denied background check, overriding a 4473, deciding whether to hand over a firearm.
- Anything you cannot check. If you cannot tell whether its answer is right, do it on screen.
