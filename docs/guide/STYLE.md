# Writing style for the OpenGunstore user guide

This file is not published. It is the standard every page under `docs/guide/`
follows, checked in part by `python3 scripts/docs/guide_lint.py` (CI runs it).

Audience: counter clerks and store managers at an FFL gun shop. Assume retail
competence and zero ERPNext, Frappe or software knowledge. American English.
Pages under `admin/` are for the administrator and the bookkeeper and may use
system terms, but the rest of these rules still apply.

## Voice
- Second person, present tense, imperative for steps. "Scan the barcode." Not
  "The barcode should be scanned."
- Say what the screen shows, then what to do. Never speculate about behavior you
  did not read in the source.
- Never use the words "simply", "just", "easy", "obviously".
- Use em dashes sparingly. Prefer a period when the thought is complete.
- Never invent a button, field, status, or menu item. Every UI label you name
  in bold must appear verbatim in the source (`apps/`) of the version the guide
  describes. If you cannot confirm a label, describe the action without naming
  a control.
- Paragraphs are at most three sentences (the lint stops at 350 characters).
- Describe what the system does today. No design rationale ("That is
  deliberate", "so that we…") and no history ("used to", "now", "since
  version", "older links"). Release notes say what changed.

## Words staff never see
- No Desk addresses (`/app/...`, `/desk/...`). Say where to click: "Open
  **Settings**, then **FFL Settings** under **FFL & Compliance**", or "In the
  sidebar, open **Inventory**, then **Receive Goods**."
- No ERPNext document type names on staff pages: Sales Order, Sales Invoice,
  Payment Entry, Delivery Note, Stock Reconciliation, Journal Entry, Stock
  Entry, POS Closing Entry, "doctype". Say what the person sees: "the order",
  "the receipt", "the invoice", "the payment", "the count".
- Keep a document name only where staff open a list with that title. Then bold
  it as the on-screen label (**Sales Invoice**) and say once that it is the
  list they open.
- No "ERPNext's own…" asides and no retry calls with a record type and name.
  When something needs an administrator, say "ask your administrator" and link
  the admin page.

## Page shape
Every page starts with an H1, then one sentence saying who this is for and
when ("For counter staff, when a customer buys a gun to take home today.").
That line is also the page's summary in `llms.txt`.

Then use this order where it applies:

1. **Before you start**: prerequisites, permissions, common blockers.
2. **Steps**: a numbered list, one action per step. Bold the exact control
   label: "Click **Submit & Receive**."
3. **What happens next**: two or three outcomes the person can see or check
   (receipt emailed, bound book updated, stock reduced). Ledger and accounting
   detail goes to [For the bookkeeper](admin/bookkeeper.md), not here.
4. **Troubleshooting**: at most five rows, the most common first, as "You
   see" / "Fix". Rare or admin-only rows go on the admin page they belong to;
   the top symptoms across the guide are on [Troubleshooting](help/troubleshooting.md).

Word budget: a staff page stays under about 1,500 words (the lint stops at
1,600). Split a longer page by task. Admin pages may run longer but follow the
paragraph and table rules.

## Write a thing once
FastBound pushes and retries, card charges and refunds, and the 4473 gate each
have one home page. Other pages say one sentence and link there:

| Topic | Home |
|---|---|
| The 4473 gate, statuses, paper route, override, Redo 4473 | [Form 4473 and NICS](compliance/4473.md) |
| A FastBound push failed, Retry, Sync with FastBound | [FastBound](admin/fastbound.md#failed-pushes) |
| Card on the terminal, card refunds and voids | [Take payment](counter/payments.md) and [Card voids and refunds](manager/card-refunds.md) |

## Admonitions (MkDocs Material)
Use sparingly, at most two per page.

```
!!! warning "Federal recordkeeping"
    Text.

!!! note
    Text.

!!! tip
    Text.
```

Reserve `!!! danger` for actions that break federal compliance or destroy data.

## Screenshots
Reference only files that exist in `docs/guide/screenshots/` (WebP, 1600 px
wide, shot with a made-up store name). Use a figure with a caption, and put the
path relative to the page:

```
<figure markdown>
![The Dashboard](../screenshots/page-dashboard.webp){ .screenshot }
<figcaption>The Dashboard: what is waiting today.</figcaption>
</figure>
```

Do not invent screenshot filenames. If a step needs a picture that does not
exist, write the step without one.

## Cross-links
Link with relative paths ending in `.md`, e.g.
`[the bound book](../compliance/bound-book.md)`. Only link to pages in the
navigation. When a page moves, add its old path to `redirect_maps` in
`mkdocs.yml` so old links keep working.

## Tables
Use tables for field references, status meanings, and troubleshooting. Keep to
three short columns where possible and cell text under about 12 words.

## Compliance language
This is a federally licensed business. When a step touches the bound book, Form
4473, NICS, or an FFL license, say plainly what the law-facing consequence is.
Do not give legal advice, and do not tell staff a shortcut around a control.
