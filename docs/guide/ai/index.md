# AI assistants

For anyone in the store who wants to ask an AI assistant such as Claude, ChatGPT or Grok about the store, or have it do routine work for them. Your manager switches the connection on once; after that you connect your own assistant with your own account.

## What an assistant can do here

Once connected, the assistant can look things up in the POS and, on the full connection, do work for you. It sees the same store you see on screen, nothing more:

- **Answer questions.** "Which guns came in this week?", "What did we sell yesterday?", "Who is waiting on a transfer pickup?"
- **Pull reports together.** A sales summary for a date range, stock for one brand, the open web orders that need attention.
- **Do routine steps.** Record a payment, list a gun on the website, close the cash drawer, start a stocktake count. It asks you first.

It does not replace you at the counter. A Form 4473, the background check, handing over a firearm and anything a law or the owner says a person must do stays with a person.

## Three connections

| Connection | What it can do | Who uses it |
|---|---|---|
| **Read-only** | Read reports, stock, orders and customers. It cannot change anything | The bookkeeper, an outside accountant, anyone who only needs answers |
| **Full** | Read, and also change data, within your own role | Counter staff and managers who want it to do work |
| **Website** | Read the store's website: orders, products, SEO and email contacts. A Website Connector Manager can also make the changes the store has set up, such as SEO titles, product text, order notes and contact tags | Staff a manager gives a website role |

Every connection signs in as **you**. The assistant can do only what your own account can do on screen. A counter account stays a counter account. The website connection is off until the store sets it up, and it also needs a website role on your account: **Website Connector Viewer** only reads, **Website Connector Manager** can also change the website. Changes go live on the website at once.

## Before you start

1. A **System Manager** switches the connection on, in **Settings**, then the **MCP Settings** card. See [Settings](../manager/settings.md). The addresses to give an assistant appear there.
2. You have your own POS account. A shared login defeats the record of who did what.
3. You have an assistant that can add a custom connector. See [Connect your assistant](connect.md) for which plans and apps can.

## Rules that keep you safe

!!! warning "It works on the live store"
    The connection points at your store's real POS. A gun the assistant lists is one a customer can buy a minute later. A disposal writes the federal acquisition and disposition record.

- **Read what it says before you say yes.** Before any change, the assistant must tell you what it is about to do and wait. Check the gun, the customer and the amount, then answer.
- **Check one answer against the screen** the first time you use it. If the two disagree, stop and tell your manager.
- **Do not paste in card numbers, passwords or ID numbers.** The POS never hands them to an assistant either.
- **Every call is recorded** under your name in the **Connector Audit Log**, reads included. A System Manager can see it, and the rows can never be deleted.

## Next

- [Connect your assistant](connect.md): Claude, ChatGPT and Grok, on the web, desktop, phone and command line.
- [Working with an assistant](working-with.md): how to ask, with examples for the counter, managers and the bookkeeper.
