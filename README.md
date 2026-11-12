# books_assistant

Prepares routine bookkeeping in ERPNext for approval, with optional automatic booking of tightly scoped recurring bank rules.

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

### Automatic learning and booking

Bank automation is opt-in. In **Automation Settings**, a System Manager chooses an enabled **Bank Automation User**, then enables **Learn Bank Rules Automatically**, **Book Approved Bank Rules Automatically**, or both. Scheduled work runs hourly with that user's ERP permissions. Both switches default to off. A run handles at most 100 bank lines, rotating through the queue so unresolved lines cannot starve later matches; switching booking off stops subsequent runs, and saving the switch can wait for an active batch to finish.

Learning uses reconciled expense/income history to suggest recurring merchant categories. It requires three distinct bank transactions across at least two dates, consistent company, currency, bank account, direction and destination account, within 180 days, with the latest evidence no older than 60 days. The suggested rule stores the source transactions and observed amount range. Existing and disabled rules are preserved. Usage counts do not establish learning confidence.

Automatic booking additionally requires explicitly enabling **Auto Book** on each reviewed Bank Rule and setting positive minimum and maximum amounts. Review the evidence before authorizing a rule: older history may predate provenance tracking. Automatic execution never widens the limits. Conflicting history, competing categories, amounts outside the limits, stale evidence, partial reconciliation, and existing payment/invoice/transfer candidates stay in Bank Review.

The initial automatic path handles two-leg recurring expense/income journals only. Customer and supplier payments, transfers, holds, split journals, tax postings and foreign currencies require review. The supported company/currency scope follows finance API v1. The general queue's **Confident** label alone never authorizes automatic booking. Automation Settings shows the last run time and aggregate decision counts; a POST to `books_assistant.bank_automation.run(company)` returns per-line reason codes with the caller's ERP permissions.

Every rule-created journal is recorded in **Bank Rule Booking**, including manually approved rule actions. Automatic records retain the source transaction, rule, evidence, actor and resulting journal. Rule-generated journals are excluded from later learning, including when matched to another bank line. Source and rule locks, live validation, and one transaction for booking/reconciliation/audit protect retries and duplicate workers. Correct an incorrect submitted entry through ERPNext's normal cancellation and amendment process.

### Hybrid category suggestions

For unresolved bank lines, **Suggest category** uses existing deterministic actions first, then an optional local classifier, then optional Cloudflare Clef-flash. Enable **Local Category Classifier** and/or **Cloudflare Category Suggestions** in Automation Settings. Both default off. The account suggestions are separate from executable Bank Review actions: they require review, can prefill **Make rule**, and never authorize automatic booking. A reviewed rule is scoped to the source bank account; creating it checks that the classified bank line has not changed. Automatic booking still requires the existing separate rule authorization.

The local model is a bounded TF-IDF nearest-centroid classifier implemented in Python's standard library. It runs on CPU, with no GPU, model downloads, new packages or persisted pickle files. Each request trains from visible, validated historical journal classifications from the preceding 730 days. Generated rule journals, inaccessible or ambiguous source records and unsupported ledgers are excluded or veto their merchant. Scope is company/currency/bank/direction. The local scores are cosine similarities and margins, not probabilities of correctness. Each classification loads only its source bank/direction. Limits are 2,000 history rows per request, 500 training documents per scope and 8,000 features per scope; reaching a capacity limit causes abstention rather than silently dropping conflicting evidence.

Cloudflare fallback uses its **hosted REST endpoint**; no Worker or model deployment is needed. Give the ERP processes `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` (with Workers AI permission), then enable the Cloudflare switch. See [.env.example](.env.example). The request sends the bank description, amount/direction/currency, permitted category labels and up to twelve same-scope historical examples. It omits bank transaction IDs, journal IDs and credentials from model context. Cloudflare's [Clef-flash documentation](https://developers.cloudflare.com/workers-ai/models/clef-flash/) describes the hosted API.

Hosted requests have a five-second socket timeout, no redirects or retries, a 16 KiB request cap and a 64 KiB response cap. Unknown categories, malformed or truncated responses, errors and weak scores abstain. Returned probabilities are model estimates and have not been calibrated against these books. The **Daily Cloudflare Call Limit** defaults to 100 attempts per company/site/day, shared across users; Redis atomically reserves failed attempts too. This operational counter can reset on cache eviction/reset and is not a provider billing cap. If Redis is unavailable, hosted calls stop. Neither a provider error nor a model prediction changes accounting documents.

`POST /api/method/books_assistant.bank_classification.suggest` accepts `bank_transaction` and optional `company`. It returns versioned status/source/reason codes, model/dataset/proposal hashes and a source fingerprint. Logs contain bounded machine metadata, without bank descriptions, historical text or provider error bodies. The ERP session user's finance role and record access are checked before classifying. The deterministic automatic-booking job never calls a model.

#### Benchmark before enabling broader automation

The Bank Review menu's **Benchmark local category classifier** calls `books_assistant.bank_classification.benchmark` and replays visible ERP history without provider calls or accounting writes. Reports compare exact-merchant history rules with the local classifier on complete held-out dates and, separately, unseen merchant groups. They show coverage, precision among suggestions, wrong counts, abstentions, timing and approximate reachable Python model memory (not process RSS). Sparse or missing data is reported explicitly. Neither result automatically changes thresholds or booking permissions.

For local observation JSON, run:

```bash
python3 scripts/benchmark_bank_classifier.py observations.json
# Explicitly opt into hosted comparison; requires the environment variables above.
python3 scripts/benchmark_bank_classifier.py observations.json --cloudflare --max-cloudflare-calls 20
# Synthetic mechanics check only; this does not measure ERP accuracy.
python3 scripts/benchmark_bank_classifier.py books_assistant/tests/fixtures/bank_classifier_synthetic.json
```

Hosted comparisons use the same held-out sets and training-only examples. The call cap is shared across both splits and ranges from 1 to 100 attempts; once exhausted, remaining predictions abstain and reduce coverage. The CLI does not query ERP or load credentials from files. Input rows require `bank_transaction`, `journal`, `description`, `date`, `company`, `currency`, `bank_account`, `direction`, `account` and a positive `amount`; use verified labels and preserve source IDs to check leakage. Output contains aggregate results/category counts, not source descriptions or bank transaction IDs. Real-data and hosted performance remain unverified until run with authorized history and credentials.

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

The matching and bank-automation policy/boundary tests are self-contained. Site-backed tests run against the site's own data (use a copy of production), with made-up customers and addresses where they need them, and roll back everything they create. No email is sent.

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
node tests/test_bank_classification_ui.js
python3 -m unittest books_assistant.tests.test_bank_classifier books_assistant.tests.test_bank_classifier_eval -v
python3 -m unittest books_assistant.tests.test_finance_api -v
python3 -m unittest books_assistant.tests.test_bank_learning_policy books_assistant.tests.test_bank_auto_guards books_assistant.tests.test_bank_auto_execution books_assistant.tests.test_bank_automation_settings books_assistant.tests.test_bank_rule_provenance books_assistant.tests.test_bank_rule_serialization books_assistant.tests.test_matching -v
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
