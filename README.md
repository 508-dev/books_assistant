# books_assistant

Prepares routine bookkeeping in ERPNext so a person only has to approve it. Nothing is booked until someone does.

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

## Install

```bash
bench get-app https://github.com/508-dev/books_assistant
bench --site <site> install-app books_assistant
bench --site <site> migrate   # syncs the Bank Review page and the custom fields
```

## Tests

```bash
bench --site <site> run-tests --app books_assistant
```

The matching tests are self-contained. The queue and payment tests run against the site's own data (use a copy of production) and roll back everything they create.

## License

MIT, see [license.txt](license.txt).
