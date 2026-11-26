# Daily reply list

For managers and sales staff, to see each morning which customers are waiting for an answer.

Every day at 8:00 the POS emails a table of the CRM leads and deals where the customer wrote last and nobody has answered. The longest wait is first. Nothing is sent to a customer.

## Turn it on

1. Open [Lead Mail Settings](settings.md#lead-mail-settings).
2. Tick **Daily reply list**. It starts off.
3. To send everything to one or more people, type their addresses in **Send it to everyone at**. Use commas or one address per line.
4. Save.

Each lead or deal owner gets their own records without being listed. An address in **Send it to everyone at** gets every record, including the ones with no owner. A team address such as sales@ cannot go in the list.

## What is in the table

A record is listed when its status is open or ongoing, it is not other mail, and its newest email is from the customer, not from an automated sender such as noreply@. A website question counts as an email from the customer. A record with an open task is listed too, even when it is answered.

| Column | Shows |
|---|---|
| **Customer** | The customer's name, linked to the lead or deal, and its number |
| **Owner** | Who owns the record |
| **Waiting message** | The subject of the customer's latest email |
| **Date** and **Days** | When it arrived and how many days it has waited |
| **Messages** | How many emails the customer has sent since the store last wrote |
| **Tasks** | The titles of the open tasks |
| **Summary** | The summary from the customer's memory, when the record has one |

The email's subject says how many customers are listed. Someone with nothing to answer gets no email.

## What happens next

- Open a record from its link and answer it. It drops off the next morning's list once the store's reply is the newest email. A record with an open task stays until the task is done.
- The list never shows ID numbers, card numbers, birth dates or the other details the POS keeps out of customer memory.

## If something goes wrong

| What you see | What to do |
|---|---|
| No email arrives | Check that **Daily reply list** is ticked and that your administrator has set up a default outgoing email account |
| A customer you answered is still listed | The newest email on the record is still the customer's. Reply from the lead or deal page |
| A record with an open task is listed with no date | The task is what keeps it here. Finish or cancel the task |
| Saving the settings says an address is a team address | Use a staff member's own address |
