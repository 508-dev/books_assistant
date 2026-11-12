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

**PDF layout:** the customer's **Invoice Print Format** if set. Otherwise the timesheet layout when the invoice has timesheet hours, otherwise the default. Both defaults are chosen in Automation Settings. New installations use ERPNext's standard layout until a format is configured; upgrades preserve existing selections and intentional blanks.

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

For a test path that requires no production export, see
[Synthetic ERP verification](books_assistant/tests/README.md). It includes a real
old-to-new migration, repeat migration, and guarded accounting/concurrency suites.

## License

MIT, see [license.txt](license.txt).

## Integration API (v1)

Custom app methods are callable through Frappe's authenticated HTTP API. The
ERP session user is the actor: use that user's API/OAuth credentials and ERP
permissions. A Discord ID or an `actor` argument is never delegated authority.
A trusted backend must verify its Discord-to-ERP identity mapping before enabling
these finance actions. Keep credentials on the backend and out of Discord.

This version supports exactly one Company with USD books and no foreign-currency
financial records. Unsupported scope fails explicitly before accounting work.
Read endpoints respect the authenticated user's record permissions. Results
contain private financial information; do not publish them to shared channels.

Call dotted methods at `/api/method/<method>`. Frappe wraps return values in
`message`; errors retain its normal exception/status behavior and shared contract
errors add a machine-readable top-level `error_code`.

| Method | HTTP | Purpose |
| --- | --- | --- |
| `books_assistant.api.get_finance_status(company)` | GET | Visible pending billing, unmatched bank lines, overdue invoices, held bills and failed invoice emails. |
| `books_assistant.api.get_supplier_payment_status(supplier, company)` | GET | Outstanding bills, held/eligible amounts and blocking client invoice explanations. |
| `books_assistant.bank_review.get_queue(bank_account=None, company=None)` | GET | Suggestions with reviewed fingerprints. |
| `books_assistant.bank_review.preview_approval(bank_transactions, expected_fingerprints, company=None, bank_account=None)` | POST | Freeze selected suggestions; reject changes since the displayed queue. |
| `books_assistant.bank_review.approve(preview_id, request_id)` | POST | Execute the reviewed bank actions, with per-line results. |
| `books_assistant.pay_supplier.preview(supplier, invoices=None, company=None, all_eligible=False, bank_account=None)` | GET | Read payable bills, held bills, available credits and payment side effects. |
| `books_assistant.pay_supplier.preview_preparation(...)` | POST | Freeze the supplier preparation with the same arguments as `preview`. |
| `books_assistant.pay_supplier.prepare(preview_id, request_id)` | POST | Apply reviewed credits and return an **unsaved** payment; no bank transfer. |
| `books_assistant.billing_api.prepare_billing(run_id, company=None)` | POST | Prepare drafts on an existing saved Billing Run. |
| `books_assistant.billing_api.preview_billing_approval(run_id, projects, company=None)` | POST | Freeze selected billing documents, amounts, recipients, email settings and hold effects. |
| `books_assistant.billing_api.approve_billing(preview_id, request_id)` | POST | Submit reviewed projects and return document, hold and email results. |

Selections must contain 1–100 distinct document names. Empty lists never expand
to all documents. Supplier selection can use `all_eligible=True` explicitly;
that option cannot be combined with an invoice list. Other operations always
require explicit selected projects/bank lines.

### Review and execute

1. Read the current state and select the exact documents.
2. POST a preview. Review the returned `snapshot`, including credit application,
   recipients, amounts and hold consequences. Creating a preview stores approval
   state but does not book accounting entries.
3. POST its `preview_id` and a new unique `request_id` to the execution endpoint.
   Previews expire after ten minutes, belong to their ERP actor/action, and are
   single-use. Rechecking live data rejects stale previews.
4. If the response is lost, retry the **same preview and request ID**. The durable
   API Request ledger returns the original result without repeating accounting
   work. Reusing a request ID for another operation is rejected. Authorization is
   rechecked on replay, even after a successful execution.

API Preview and API Request have no generic user CRUD permissions. Execution
locks the preview, then the Company, then referenced documents. Requests/results
and accounting changes commit together at the HTTP transaction boundary; no
bank line commits independently. Each bank line/billing project uses a savepoint,
so the completed operation can contain individual failures. For a new attempt at
failed items, review a new preview and use a new request ID. Request records are
retained as audit/retry state; do not purge them while clients may retry.

Versioned preview/results contain `api_version: 1`; completed operations also
contain `operation_id`. Billing project state is `pending`, `approved`, `failed`,
`left_out`, or `review_required`. Display text never determines transitions.
Invoice email results distinguish queued, disabled, missing recipient, failed,
and previously submitted; **queued does not mean delivered**.

Approval previews calculate profitability from the current selected invoice and
bills, including applicable referral fees. This selected-document margin excludes
earlier invoices/bills that may be included in the prepared run's period totals.
The final confirmation labels that scope and shows current cost/margin warnings.

### Upgrade and validate

Run `bench --site <test-site> migrate` on a disposable copy first. This syncs the
internal DocTypes and runs the post-sync billing-state migration. Recognized
legacy outcomes migrate to machine state. Unknown/translated outcomes become
`review_required` and block approval/deletion/discard: a System Manager must
inspect the referenced invoices/bills and repair the machine state explicitly in
an administrative console. Do not assume those hours can be billed again.
Deploy Python and rebuilt frontend assets together; older callers using
`approve(bank_transactions)` or `prepare(supplier)` must adopt previews and
request IDs. The bundled ERP interface already uses the new contracts.

Standalone boundary/state-machine checks (no Frappe installation):

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest books_assistant.tests.test_finance_api -v
```

Also run the existing Bench suite and the new site tests before deployment:

```bash
bench --site <test-site> run-tests --app books_assistant
```

The `test_integration_transactions` module additionally needs site config
`books_assistant_transaction_tests=1` on a disposable site: it commits temporary
fixtures to test separate database connections, duplicate requests, request-key
reuse, rollback and internal-record permission denial. Never enable it on a
production site. Standalone fakes do not prove database locking or migration
correctness; the Bench tests need a real Frappe/ERPNext installation.

## Payout funding check

[View screenshots of the funding warning, approval review, and bank evidence form](docs/screenshots/README.md). These local UI previews use sample data and the actual funding renderer; they are not live ERPNext captures.

Before sending money, an Accounts Manager or System Manager can use **Review payout funding** on a saved draft supplier Payment Entry. The supplier payout dialog also shows the funding check when its bank account changes. Its frozen preparation preview includes the funding assessment, so changes to bank evidence or reservations require a new review before credit reconciliation. Preparing a draft does not approve a transfer.

If an explicitly linked client invoice is missing or inaccessible to the ERP actor, the funding check and supplier draft preparation fail closed. A fronting reason cannot bypass source-document permissions; an authorized accounting user must correct the link or access first.

The check separates two questions:

- **Client receipt:** engineering/project bills use the exact **Paid After Client Pays** invoice link, or the invoices linked through all associated timesheets, including grouped bills linked by `Timesheet.purchase_invoice`. An outstanding invoice warns that the co-op is fronting payment. A paid invoice counts as verified client funding only with a submitted customer receipt fully matched to incoming bank transactions. Missing links, credit-note-only settlements, and unsupported currencies remain unverified. Project/date guesses and a manually entered clearance date cannot establish receipt.
- **Available cash:** the bank's verified available balance, less other commitments not yet reflected by the bank, active payout reservations, and a minimum buffer. An ERP ledger balance is never used as evidence of cash available at the bank.

### Record bank evidence

This app does **not** connect to a live bank-balance API. Open **Bank Funding Snapshot** (`/app/bank-funding-snapshot`), or **Record bank balance** from the Payment Entry. Read the **available** balance in the bank portal and record its check time, currency, other committed payments, minimum buffer, and evidence notes. Snapshots are immutable; record a newer one to correct or refresh them. **Maximum Bank Balance Age (Hours)** in Automation Settings defaults to 4 and can be set up to 24. Missing, stale, or future balance evidence cannot approve a payout.

Only include other commitments **not already deducted by the bank and not reserved here**. If the bank's available balance already reflects a transfer, do not subtract it again. The app cannot discover transfers made outside this workflow: admins must include those commitments and refresh balances before transfers.

### Approve and reserve

1. Save the draft Payment Entry and select **Review payout funding**. The review covers the entire payment, its exact bills and allocations, recipient, company, currency, bank account, bank evidence, and other reservations.
2. Insufficient or unverified cash blocks **Approve and reserve funds**. Unpaid or unverified client receipts require an explicit fronting reason from an authorized approver. Existing invoice holds still apply; unblocking a bill is a separate action.
3. Approval rechecks the reviewed state and serializes on Company and the bank ledger account before inserting a durable **Payout Reservation**. A changed preview requires another review. Duplicate clicks for the same unchanged payment reuse its reservation. Before transferring, re-open the funding review to check current bank evidence.
4. Unsaved changes show a neutral save-to-refresh notice rather than counting the payment twice. Saved edits to an approved payment invalidate its approval while keeping its amount reserved. Release an unsent reservation with a recorded reason before approving the changed payment. Reservations never expire automatically because money may already have left the bank.
5. After the transfer is recorded as a submitted Payment Entry, record a new bank snapshot and list its reservation under **Payouts Already Reflected in This Balance**, only when the bank balance really includes that debit. This removes the reservation deduction atomically with the new balance, avoiding double subtraction. Submitted payouts cannot free cash through the release action. If an edited amount was already sent, select **Reflected** and record a reason confirming the actual changed debit is included in the new balance. If the payout was recorded against another bank, select **Not sent from this bank** for the invalid original-bank reservation and explicitly confirm with a reason that the original transfer did not occur. Both paths require submitted accounting evidence and a balance checked after that evidence; they preserve the submitted payment and record the resolution actor and reason.

The first release shares Finance API v1 scope (one USD company, no foreign-currency financial records) and supports positive supplier payouts in USD, with one or more Purchase Invoice allocations totaling the payment and no additional payment taxes/deductions. Other shapes require manual review and do not receive funding approval. Existing bank reconciliation and recording historical transfers remain available: these controls govern funding approval, not posting accounting evidence after money was sent. They do not prevent someone transferring directly in the bank portal.

### Integration contract

Authenticated endpoints, restricted to Accounts Manager/System Manager and document/company access:

| Method | Endpoint | Inputs |
|---|---|---|
| GET/POST | `books_assistant.funding.check` | `bank_account` (Bank Account document ID), `amount`, explicit `invoices` list, optional `supplier` |
| GET/POST | `books_assistant.funding.check_payment` | Saved draft `payment_entry` |
| POST | `books_assistant.funding.approve` | `payment_entry`, exact `review_token`, `fronting_reason` when required |
| POST | `books_assistant.funding.release` | `reservation`, reason confirming it was not sent |

Checks return version 1 structured cash/receipt states, stable reason codes, a review token and approval eligibility. Approval returns the durable reservation ID and assessment. A shared Discord service credential does not establish the human actor's authority; Discord approval commands need verified ERP identity mapping before consuming these endpoints. No Discord financial approval command is enabled by this change.

Run the self-contained checks without a Frappe site:

```bash
python3 -m unittest books_assistant.tests.test_funding_policy books_assistant.tests.test_funding_api books_assistant.tests.test_matching
node --test books_assistant/tests/funding_ui.test.cjs
```

The funding suite also runs against a disposable ERPNext/MariaDB site using the guarded synthetic setup in [the test guide](books_assistant/tests/README.md). It exercises actual invoices, bank receipts, reservation lifecycle, permissions and two-connection REPEATABLE READ approval races. A separate pre-funding-schema upgrade check compares full invoice/payment documents and GL rows before and after migration, then verifies a repeat migration preserves a configured freshness setting. These tests use no production export, bank provider or outgoing email. Browser theme/lifecycle checks against the installed production version remain separate from database verification.
