# Synthetic ERP verification

No production export is required for these checks. Use a disposable ERPNext
installation with a single synthetic USD company; never run the fixture commands
on the production site. Fixture guards reject an existing unrelated company.
No outgoing email account, worker, scheduler, bank feed or payment provider is
needed. The tests record accounting documents; they do not send money.

## Tested environment

- Official `frappe/erpnext:v15.124.5` image: ERPNext 15.124.5, Frappe 15.126.1,
  Python 3.11; MariaDB 10.6 and Redis 7.
- `referral_fee` source `48c2b6f` and `timesheet_extended` source `38c46ad` installed
  before seeding. Billing uses their real DocTypes, custom fields and hooks.
- Books Health installed for the optional cross-app suite.
- Isolated Docker network, no published ports, generated test credentials and no
  production data. Stock ERP setup masters, synthetic parties/items/accounts and
  explicit USD price lists are used.

## Real accounting and concurrency tests

Set these only on the disposable site:

```sh
bench --site <test-site> set-config books_assistant_synthetic_site 1 --parse
bench --site <test-site> set-config books_assistant_transaction_tests 1 --parse
bench --site <test-site> set-config books_health_synthetic_tests 1 --parse
bench --site <test-site> execute books_assistant.tests.synthetic_site.seed_minimal_site
bench --site <test-site> execute books_assistant.tests.synthetic_site.run_site_tests --kwargs '{"include_health":1}'
```

`run_site_tests` raises on failures **or skipped tests**, instead of relying on
Bench versions that print test failures but return exit status zero. It bypasses
ERPNext's generic test fixtures/before-tests hook, which creates extra test
companies incompatible with the API's deliberate single-company scope.

The 22 real-site tests cover:

- Canonical stored-run reloads, denied forged state and explicit selections.
- Two concurrent connections under REPEATABLE READ, duplicate execution,
  request-ID reuse, rollback and internal-record permissions.
- Actual bank receipts, matching an existing payment, stale suggestions and
  retries without duplicate payments.
- Supplier pure previews, exact unsaved payments, newly held bills and a real
  $30 credit reconciled against a $100 bill, leaving $70 payable. Rolling back to
  an outer savepoint verifies that reconciliation and the request ledger share
  the caller's transaction.
- Actual billing submission, bill holds, stale invoices and ledger-stable retries.
- Books Health findings, pagination, source permissions, foreign-currency
  rejection and reads that do not mutate SQL records or email queues.

For individual suites, `bench run-tests --skip-before-tests --skip-test-records
--module <module>` is also available. Check the printed result or use the strict
runner above; a zero Bench exit status alone is insufficient.

## Old-schema migration test

1. Start a new disposable ERP site with the old Books Assistant source
   (`c2b3329`). Remove files introduced by the new source instead of overlaying
   the old archive onto a newer checkout. Do not install the new schema yet.
2. Put `synthetic_site.py` into the old app's `books_assistant/tests/` directory.
   Enable `books_assistant_synthetic_site=1`.
3. Before installing the old app, import the helper from a Bench console and
   call `prepare_legacy_install()`. It provides ERP setup roots and synthetic
   placeholders for the old schema's two site-specific print-format defaults.
   The global Department tree root is the only fixture inserted without a
   company; ordinary financial documents retain their full ERP validation.
4. Install the actual referral/timesheet dependencies and the old app. The
   referral fixture sync must complete before seeding financial documents.
5. Run:

```sh
bench --site <test-site> execute books_assistant.tests.synthetic_site.seed_legacy_migration
```

This requires the old fields/internal-table absence and seeds five historical
outcomes, eight real invoices/bills (draft and submitted) and eight GL entries.
The expected snapshots are stored only under the test site's private files.

6. Replace the app with the new source, retaining the helper, and run:

```sh
bench --site <test-site> migrate
bench --site <test-site> execute books_assistant.tests.synthetic_site.assert_legacy_migration
bench --site <test-site> execute books_assistant.tests.synthetic_site.prepare_repeat_migration_check
bench --site <test-site> migrate
bench --site <test-site> execute books_assistant.tests.synthetic_site.assert_legacy_migration
```

The assertions check actual schema creation and Patch Log execution; all five
state mappings; preserved timestamps, links, saved print-layout selections,
invoice/bill records and GL entries. Unknown outcomes become `review_required`.
The second migration must preserve a deliberate post-upgrade machine-state
sentinel rather than reinterpreting legacy prose again.

Also smoke-test a fresh new-version install with neither production print format
present. It should succeed with blank format settings (ERPNext's standard
layout), while upgrading an existing site's saved choices preserves them.

Synthetic tests cover controlled cases. A sanitized production clone can add
historical data-shape coverage later, but is not required for these migrations
or transaction checks.
