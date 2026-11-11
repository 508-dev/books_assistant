# books_assistant

Prepares routine bookkeeping in ERPNext so a person only has to approve it. Nothing is booked until someone does.

- [Bank Review](#bank-review): suggested matches for every bank line, approved in bulk.
- [Pay supplier](#pay-supplier): one payment for everything owed to a supplier, after applying their credit.
- [Monthly Billing Run](#monthly-billing-run): client invoices and engineer bills from timesheets; bills held until the client pays.
- [Invoice emails](#invoice-emails): sent on approval, with chosen recipients and PDF layout, and bounce handling.
- [Automation Settings](#automation-settings): what the automations are doing, and a switch for each.

## Bank Review

**Bank Review** (`/app/bank-review`, Accounts Managers) lists every unreconciled bank line with a suggested action and a confidence, as Confident, Check or Needs you. Tick lines, or use **Select confident**, then **Approve selected**. Each approved line creates or links its entry and is reconciled; lines that fail are reported and left alone.

Suggestions, most preferred first:

| Kind | When | What approving does |
|---|---|---|
| Match | A payment or journal entry for the same amount on this bank account, dated within a week, and not yet matched to a bank line | Reconciles the line to it. This comes first so a payout recorded by hand is never recorded again from the bank line. |
| Receipt / payout | The description names an invoice or bill (`ACC-SINV-2026-00280`, `ACCSINV202600277`, `SINV202600267`, amended `-1` versions), or the payer/payee is recognised and one open invoice (or all of them) adds up to the amount | Creates a payment applied to those invoices, so there's no separate Payment Reconciliation step. |
| Transfer | A line for the same amount, in the opposite direction, on another of our bank accounts within 5 days | One journal entry, reconciled to both lines. |
| Rule | A **Bank Rule** matches (subscriptions, bank fees, interest, transfers to an account with no matching line yet) | A Bank Entry journal from the line to the rule's account, created the same way as ERPNext's Bank Reconciliation Tool. |

**Held for a person (Needs you):**
- Lines that are only partly reconciled.
- Lines whose payment seems to have been booked to a different bank account. The suggestion names the payment so it can be corrected; it is not recorded twice.
- Anything unrecognised. Lines that look like they'll repeat have a **Make rule** button.

**Learn rules and bank names from history** (page menu) reads past reconciliations:
- **Rules:** it creates a Bank Rule for every merchant booked to the same account at least twice, for example GitHub, Google, Elest.io and Kagi to Software Subscriptions, or Chase fees to Bank Charges.
- **Bank names:** it records how parties appear on bank statements (e.g. a payee's name in another script, or a company name that differs from the ERPNext record) in a new **Names on Bank Statements** field on Supplier and Customer. Names that appeared for more than one party are skipped, as are card purchases and payment services (Wise, Intuit, Venmo…).

Existing rules and names are kept, so it's safe to run again.

## Pay supplier

**Pay what we owe** on a Supplier (under *Create*), or **Pay selected bills** in the Purchase Invoice list:

1. Lists their open bills, or the selected ones, and any credit in their favour that isn't applied yet (an earlier overpayment, a payment made before the bill, a debit note).
2. Applies that credit to the oldest bills first.
3. Opens a new payment for the rest, with every bill attached, the amount filled in, and the bank account they're usually paid from. The reference names the bank it's paid from, e.g. `Novo payout 2026-11-06 <supplier>`. Novo shows no transfer number, so keep it as is. A payment from Wise can use Wise's own transfer number instead if you prefer. Then save and submit. Once Bank Review matches the payment, its bank line links the bank's own transaction ID.

Bills on hold (see below) aren't included; the dialog lists them with the client invoice each one is waiting for.

## Monthly Billing Run

**Billing Run** (`/app/billing-run`) bills a month in two steps.

**Prepare Drafts** (Accounts Managers and Billing Approvers) picks every submitted timesheet that *ends* in the period. A timesheet is billed whole, in the run covering the day it ends, because an engineer's bill is tracked per timesheet; splitting one across months would pay its later hours twice or never. For each project it drafts:

- **Hourly projects:** a client invoice for the billable hours not yet invoiced, with one line per billing rate and the timesheet rows attached. Due in 30 days unless the customer has payment terms.
- **Retainer projects:** no new invoice. The run uses the project's draft invoice (e.g. one made each month by Auto Repeat). Set **Billing Method** on the Project.
- **Every project:** one bill per engineer for the timesheets that have no bill yet. A bill covers all hours worked, at the engineer's costing rate, and is linked to its timesheets.

Each project shows its hours, client amount, engineer cost, expected referral fees and margin. Anything to check is listed above the table:
- invoices or bills that look like they were made by hand;
- hours already invoiced or billed;
- earlier timesheets that look unbilled;
- engineers with no supplier;
- a margin under 5%;
- a customer with no invoice email address.

**Discard Drafts** deletes the drafts and frees the timesheets, so the run can be prepared again.

**Approve and Send** (Billing Approvers) works on the ticked projects:
1. Submits each project's client invoice, which emails it (below).
2. Submits the engineer bills and the referral fees that submitting the invoice created.
3. Puts those bills **on hold until the client pays** that invoice.

Each project succeeds or fails on its own, and the reason is shown. **Leave Out Ticked** deletes those projects' drafts instead, for hours that were already billed by hand. Projects without a customer are internal, so their bills aren't held.

### Holds

A held bill can't be paid. Its **Paid After Client Pays** field names the client invoice it's waiting for, and it's released automatically once that invoice is fully paid. That check runs when any payment or journal entry is submitted, and hourly to catch Payment Reconciliation. To pay early, use **Unblock Invoice** on the bill. If Bank Review sees the money has already gone out for a held bill, recording that payment releases it.

## Invoice emails

When a **Billing Approver** submits a client invoice, it's emailed with its PDF and logged on the invoice's timeline. Anyone can send one again with **Email Invoice** on a submitted invoice, which opens ERPNext's email dialog prefilled with the same details.

**Recipients:**
- **To:** the customer's **Send Invoices To** addresses (Customer, under *Invoice Emails*). If there are none, the invoice's contact, then the customer's billing or primary contact, then the customer's email.
- **CC:** the customer's **CC Invoices To**, plus **Always CC** from Automation Settings.

Both fields take several addresses, separated by commas or new lines.

**PDF layout:** the customer's **Invoice Print Format** if set. Otherwise the timesheet layout when the invoice has timesheet hours, otherwise the default. Both defaults are chosen in Automation Settings.

A failed email never stops the invoice being submitted: it's noted on the invoice and listed in Automation Settings.

**Bounces:** when a bounce notice reaches the sending mailbox, the invoice's email is marked *Bounced*. The failure reason is noted on the invoice, and Billing Approvers are notified (Accounts Managers until anyone has that role). This needs incoming mail switched on for the sending Email Account.

## Automation Settings

**Automation Settings** (`/app/automation-settings`, System Managers) has a status panel at the top and a switch for each automation.

The status panel shows:
- bank lines waiting;
- bills on hold;
- invoices from the last 30 days that were never emailed;
- bounced or failed invoice emails;
- when the weekly books health email last went out.

The switches cover:
- **Invoice emails:** on or off, plus the default layouts, Always CC, and the subject and message (Jinja, with `doc` as the invoice).
- **Holds:** whether bills are held, and whether holds release automatically.
- **Entry checks:** each erp_rules check.
- **Weekly books health email:** its recipients.

The erp_rules and books_health settings are stored in site config, where those apps read them, so they work without this app.

## Install

```bash
bench get-app https://github.com/508-dev/books_assistant
bench --site <site> install-app books_assistant
bench --site <site> migrate   # syncs the pages, doctypes, custom fields and the Billing Approver role
```

## Tests

```bash
bench --site <site> run-tests --app books_assistant
```

The matching tests are self-contained. The others run against the site's own data (use a copy of production), with made-up customers and addresses where they need them, and roll back everything they create. No email is sent.

## License

MIT, see [license.txt](license.txt).
