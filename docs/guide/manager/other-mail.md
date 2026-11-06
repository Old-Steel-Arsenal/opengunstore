# Other mail

For Sales Managers. Team mail that is not a customer inquiry is kept in CRM, away from the Leads list, so nothing is lost and staff work only from real inquiries.

## What goes there

Each sender gets one lead with the status **Other mail** and a category:

- Senders at a domain in **Category rules**
- Senders at a domain in **Blocked sender domains** or **Our own domains**
- Automated senders, such as noreply
- Mail the AI extension takes for something other than an inquiry

A customer who already has a contact stays a customer, whatever their domain. A staff member's own mailbox leaves this mail out.

These leads can hold account mail, such as a password reset sent to a team address. So only Sales Managers see them, in the **Other mail** view in CRM. CRM's dashboard still counts them in its lead totals.

## Fix a wrong guess

1. Open the lead in the **Other mail** view.
2. Change its status to **New**.

It becomes an ordinary lead that everyone with Sales User or Sales Manager can see. It stays one when the sender writes again.

## Sort mail by domain

**Category rules** are in [Lead Mail Settings](settings.md#lead-mail-settings). Each row names a domain and a category. To add a category, type a new name in the **Category** column and create it.

If you use a CRM assignment rule on leads, add `status != "Other mail"` to its condition, so this mail is not assigned to anyone.

## Check the spam folder

**Spam folder** in Lead Mail Settings is the name of the folder to check, such as `[Gmail]/Spam` for a Google mailbox. Leave it empty to turn this off. It needs the AI extension's check to be on.

Every 10 minutes, each **Mailbox** address's folder is read without changing anything in it. A new folder starts with its latest 200 mails.

| What the mail is | What happens |
|---|---|
| Real spam | No record is kept |
| From a customer | It comes in as usual, with a note saying where it was found |
| Anything else | It goes to the **Other mail** view |
| The AI gives no answer | The mail is tried again on the next run |
| It cannot be read or stored | It is passed over, and your administrator can find it in the Error Log |

## My follow-ups

CRM's Leads and Deals pages each have a **My follow-ups** view: open records that are yours or have no owner yet, where the last mail came from the customer, oldest first. Reply from there and the record leaves the view.
