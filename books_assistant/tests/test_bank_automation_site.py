"""Real MariaDB accounting tests; explicitly enabled synthetic sites only.

No model/provider calls or production data. Separate connections exercise stale
REPEATABLE READ snapshots and actual row locks. run_real_tests fails on skips.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import unittest
import uuid

import frappe
from frappe.utils import add_days, flt, today

from books_assistant.tests.synthetic_site import seed_minimal_site

SETTINGS = ("bank_auto_learn_enabled", "bank_auto_book_enabled", "bank_auto_book_user", "bank_auto_scan_after",
            "bank_auto_last_run_on", "bank_auto_last_summary")
MANIFEST = "synthetic-bank-automation-upgrade.json"


def _guard():
    if not frappe.conf.get("books_assistant_synthetic_site") or not frappe.conf.get("books_assistant_transaction_tests"):
        raise AssertionError("Enable both synthetic-site and transaction-test flags on a disposable site")


def _line(fixture, keyword, amount, age=0):
    doc = frappe.get_doc({"doctype": "Bank Transaction", "date": add_days(today(), -age),
        "bank_account": fixture["bank_account"], "currency": "USD", "description": keyword,
        "deposit": 0, "withdrawal": amount}).insert()
    doc.submit()
    return doc


def _historical(fixture, keyword, account=None):
    from books_assistant import bank_review

    result = []
    for index, age in enumerate((20, 10, 2)):
        line = _line(fixture, keyword, 31.17 + index, age)
        journal = bank_review.journal_from_line(line.name, account or fixture["expense_account"])
        result.append((line.name, journal))
    return result


def seed_pre_upgrade():
    """Seed only old-schema bank journals and a manual rule before feature migration."""
    _guard()
    fixture = seed_minimal_site()
    if frappe.get_meta("Bank Rule").has_field("auto_book"):
        raise AssertionError("Seed before installing the bank automation schema")
    keyword = "LEGACYAUTO" + uuid.uuid4().hex[:10].upper()
    history = _historical(fixture, keyword)
    rule = frappe.get_doc({"doctype": "Bank Rule", "rule_name": keyword, "enabled": 1,
        "description_contains": keyword, "direction": "Money out", "bank_account": fixture["bank_account"],
        "account": fixture["expense_account"], "min_amount": 30, "max_amount": 40}).insert()
    documents = {dt: {name: frappe.get_doc(dt, name).as_dict() for name in names} for dt, names in (
        ("Bank Transaction", [row[0] for row in history]), ("Journal Entry", [row[1] for row in history]))}
    gl = frappe.get_all("GL Entry", filters={"voucher_no": ["in", [r[1] for r in history]]}, fields=["*"], order_by="name")
    manifest = {"rule": rule.name, "history": history, "documents": documents, "gl": gl,
                "rule_fields": {key: rule.get(key) for key in ("account", "bank_account", "times_used", "enabled", "modified")}}
    Path(frappe.get_site_path("private", "files", MANIFEST)).write_text(json.dumps(manifest, default=str, sort_keys=True))
    frappe.db.commit()
    return {"source_transactions": len(history), "source_journals": len(history), "gl_entries": len(gl)}


def assert_upgrade():
    _guard()
    manifest = json.loads(Path(frappe.get_site_path("private", "files", MANIFEST)).read_text())
    def canonical(value):
        return json.loads(json.dumps(value, default=str, sort_keys=True))
    for dt, docs in manifest["documents"].items():
        for name, expected in docs.items():
            assert canonical(frappe.get_doc(dt, name).as_dict()) == expected, (dt, name)
    gl = frappe.get_all("GL Entry", filters={"voucher_no": ["in", [r[1] for r in manifest["history"]]]}, fields=["*"], order_by="name")
    assert canonical(gl) == manifest["gl"], "Migration changed the general ledger"
    rule = frappe.get_doc("Bank Rule", manifest["rule"])
    assert canonical({key: rule.get(key) for key in manifest["rule_fields"]}) == manifest["rule_fields"]
    assert not rule.auto_book, "Migration granted automatic booking to an old rule"
    assert frappe.db.exists("DocType", "Bank Rule Booking")
    assert not frappe.db.get_single_value("Automation Settings", "bank_auto_book_enabled")
    return {"bank_transactions_preserved": 3, "journals_preserved": 3, "gl_entries_preserved": len(gl), "automatic_booking": "disabled"}


class TestRealBankAutomation(unittest.TestCase):
    def setUp(self):
        _guard()
        frappe.set_user("Administrator")
        frappe.flags.books_assistant_locked_documents = set()
        frappe.flags.bank_auto_booking = False
        frappe.flags.mute_emails = True
        self.fixture = seed_minimal_site()
        self.site, self.sites_path = frappe.local.site, frappe.local.sites_path
        self.keyword = "AUTOTEST" + uuid.uuid4().hex[:12].upper()
        self.settings = {key: frappe.db.get_single_value("Automation Settings", key) for key in SETTINGS}
        self.history, self.targets, self.rule_name = [], [], None
        self.committed = False

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        if self.committed:
            names = [r[0] for r in self.history] + self.targets
            journals = [r[1] for r in self.history]
            journals += frappe.get_all("Bank Transaction Payments", filters={"parent": ["in", names], "payment_document": "Journal Entry"}, pluck="payment_entry")
            frappe.db.delete("Bank Rule Booking", {"bank_transaction": ["in", names]})
            # Guarded synthetic cleanup of precisely named fixtures, including
            # submitted ledgers visible to both connections. Never production.
            frappe.db.delete("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": ["in", journals]})
            frappe.db.delete("Journal Entry Account", {"parent": ["in", journals]})
            frappe.db.delete("Journal Entry", {"name": ["in", journals]})
            frappe.db.delete("Bank Transaction Payments", {"parent": ["in", names]})
            frappe.db.delete("Bank Transaction", {"name": ["in", names]})
            if self.rule_name:
                frappe.db.delete("Bank Rule", {"name": self.rule_name})
            for key, value in self.settings.items():
                frappe.db.set_single_value("Automation Settings", key, value)
            frappe.db.commit()
        frappe.flags.books_assistant_locked_documents = set()
        frappe.flags.bank_auto_booking = False
        frappe.clear_document_cache("Automation Settings", "Automation Settings")

    def enable(self, *, learn=False, book=True):
        for key, value in {"bank_auto_learn_enabled": int(learn), "bank_auto_book_enabled": int(book),
                           "bank_auto_book_user": "Administrator", "bank_auto_scan_after": ""}.items():
            frappe.db.set_single_value("Automation Settings", key, value)

    def prepare(self):
        self.history = _historical(self.fixture, self.keyword)
        rule = frappe.get_doc({"doctype": "Bank Rule", "rule_name": self.keyword, "enabled": 1, "auto_book": 1,
            "description_contains": self.keyword, "direction": "Money out", "bank_account": self.fixture["bank_account"],
            "account": self.fixture["expense_account"], "min_amount": 30, "max_amount": 40}).insert()
        self.rule_name = rule.name
        self.enable()
        target = _line(self.fixture, self.keyword, 32.49)
        self.targets.append(target.name)
        return rule, target

    def run_automation(self):
        from books_assistant.bank_automation import run
        return run(self.fixture["company"])

    def code(self, result, line):
        return next(row["code"] for row in result["results"] if row["bank_transaction"] == line.name)

    def test_real_rule_booking_retry_and_provenance_do_not_reinforce_learning(self):
        from books_assistant.bank_learning import load_observations
        rule, line = self.prepare()
        before = frappe.db.count("Journal Entry", {"docstatus": 1})
        self.assertEqual(self.code(self.run_automation(), line), "BOOKED")
        self.assertEqual(frappe.db.count("Journal Entry", {"docstatus": 1}), before + 1)
        self.assertEqual(flt(frappe.db.get_value("Bank Transaction", line.name, "unallocated_amount")), 0)
        audit = frappe.get_doc("Bank Rule Booking", line.name)
        self.assertEqual((audit.rule, audit.automatic, audit.actor), (rule.name, 1, "Administrator"))
        self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": audit.journal_entry}), 2)
        self.run_automation()
        self.assertEqual(frappe.db.count("Journal Entry", {"docstatus": 1}), before + 1)
        observations = load_observations({"company": self.fixture["company"], "currency": "USD"})
        self.assertNotIn(line.name, {row["bank_transaction"] for row in observations})

    def test_learning_saves_explained_rule_without_bookings(self):
        self.history = _historical(self.fixture, self.keyword)
        self.enable(learn=True, book=False)
        before = frappe.db.count("Journal Entry")
        result = self.run_automation()
        names = frappe.get_all("Bank Rule", filters={"description_contains": self.keyword}, pluck="name")
        self.assertEqual(len(names), 1)
        self.assertIn(names[0], result["rules_created"])
        rule = frappe.get_doc("Bank Rule", names[0])
        self.assertEqual((rule.evidence_count, rule.auto_book), (3, 0))
        self.assertEqual(set(rule.source_transactions.splitlines()), {r[0] for r in self.history})
        self.assertEqual(frappe.db.count("Journal Entry"), before)

    def test_alternate_prefix_conflict_blocks_authorization_and_existing_rule(self):
        rule, line = self.prepare()
        account = frappe.get_doc("Account", self.fixture["expense_account"])
        other = frappe.get_doc({"doctype": "Account", "account_name": self.keyword + " Other",
            "company": self.fixture["company"], "parent_account": account.parent_account,
            "root_type": "Expense", "is_group": 0, "account_currency": "USD"}).insert()
        extra = _line(self.fixture, "PAYMENT " + self.keyword, 32.49, 5)
        from books_assistant.bank_review import journal_from_line
        journal_from_line(extra.name, other.name)
        before = frappe.db.count("Journal Entry")
        self.assertEqual(self.code(self.run_automation(), line), "EVIDENCE_CHANGED")
        self.assertEqual(frappe.db.count("Journal Entry"), before)
        rule.auto_book = 0
        rule.save()
        rule.auto_book = 1
        with self.assertRaises(frappe.ValidationError):
            rule.save()

    def test_disabled_rule_and_amount_outlier_create_no_journal(self):
        rule, line = self.prepare()
        line.db_set({"withdrawal": 300, "unallocated_amount": 300})
        before = frappe.db.count("Journal Entry")
        self.assertEqual(self.code(self.run_automation(), line), "NO_AUTHORIZED_RULE")
        rule.enabled = 0
        rule.save()
        self.run_automation()
        self.assertEqual(frappe.db.count("Journal Entry"), before)
        self.assertFalse(frappe.db.exists("Bank Rule Booking", line.name))

    def test_outer_rollback_removes_journal_reconciliation_audit_and_counters(self):
        rule, line = self.prepare()
        before = frappe.db.count("Journal Entry")
        frappe.db.savepoint("real_auto_outer")
        self.assertEqual(self.code(self.run_automation(), line), "BOOKED")
        frappe.db.rollback(save_point="real_auto_outer")
        self.assertEqual(frappe.db.count("Journal Entry"), before)
        self.assertFalse(frappe.db.exists("Bank Rule Booking", line.name))
        self.assertEqual(flt(frappe.db.get_value("Bank Transaction", line.name, "unallocated_amount")), 32.49)
        self.assertEqual(frappe.db.get_value("Bank Rule", rule.name, "times_used"), 0)

    def test_user_without_finance_role_cannot_run_automation(self):
        _rule, line = self.prepare()
        user = frappe.get_doc({"doctype": "User", "email": uuid.uuid4().hex + "@example.test",
            "first_name": "Synthetic nonfinance", "send_welcome_email": 0,
            "roles": [{"role": "Employee"}]}).insert(ignore_permissions=True)
        before = frappe.db.count("Journal Entry")
        try:
            frappe.set_user(user.name)
            with self.assertRaises(frappe.PermissionError):
                self.run_automation()
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(frappe.db.count("Journal Entry"), before)
        self.assertFalse(frappe.db.exists("Bank Rule Booking", line.name))

    def test_finance_role_cannot_forge_or_delete_provenance(self):
        user = frappe.get_doc({"doctype": "User", "email": uuid.uuid4().hex + "@example.test",
            "first_name": "Synthetic finance", "send_welcome_email": 0,
            "roles": [{"role": "Accounts Manager"}]}).insert(ignore_permissions=True)
        try:
            frappe.set_user(user.name)
            for permission in ("create", "write", "delete"):
                self.assertFalse(frappe.has_permission("Bank Rule Booking", permission), permission)
        finally:
            frappe.set_user("Administrator")

    def test_scheduler_books_as_configured_finance_user_and_restores_caller(self):
        from books_assistant.bank_automation import run_scheduled
        _rule, line = self.prepare()
        user = frappe.get_doc({"doctype": "User", "email": uuid.uuid4().hex + "@example.test",
            "first_name": "Synthetic scheduled finance", "send_welcome_email": 0,
            "roles": [{"role": "Accounts Manager"}]}).insert(ignore_permissions=True)
        frappe.db.set_single_value("Automation Settings", "bank_auto_book_user", user.name)
        result = run_scheduled()
        self.assertEqual(self.code(result, line), "BOOKED")
        self.assertEqual(frappe.get_doc("Bank Rule Booking", line.name).actor, user.name)
        self.assertEqual(frappe.session.user, "Administrator")

    def test_secondary_blocker_connection_is_read_only(self):
        from books_assistant.bank_automation import _current_reader
        from pymysql.err import OperationalError
        with _current_reader() as query:
            self.assertTrue(query("select name from `tabCompany` limit 1"))
            with self.assertRaises(OperationalError) as caught:
                query("update `tabCustomer` set customer_details=customer_details where name=%s", (self.fixture["customer"],))
            self.assertIn(caught.exception.args[0], (1290, 1792))

    def test_cancelled_source_cannot_keep_a_rule_authorized(self):
        _rule, line = self.prepare()
        source, journal = self.history[0]
        frappe.get_doc("Bank Transaction", source).cancel()
        frappe.get_doc("Journal Entry", journal).cancel()
        before = frappe.db.count("Journal Entry")
        self.assertEqual(self.code(self.run_automation(), line), "EVIDENCE_CHANGED")
        self.assertEqual(frappe.db.count("Journal Entry"), before)
        self.assertFalse(frappe.db.exists("Bank Rule Booking", line.name))

    def worker(self, barrier):
        frappe.init(site=self.site, sites_path=self.sites_path)
        frappe.connect()
        frappe.set_user("Administrator")
        try:
            frappe.db.sql("set session transaction isolation level repeatable read")
            frappe.db.sql("select name from `tabBank Rule Booking` limit 1")
            barrier.wait(timeout=15)
            from books_assistant.bank_automation import run
            result = run(self.fixture["company"])
            frappe.db.commit()
            return result
        except Exception:
            frappe.db.rollback()
            raise
        finally:
            frappe.destroy()

    def test_concurrent_rr_workers_book_exactly_once(self):
        _rule, line = self.prepare()
        frappe.db.commit()
        self.committed = True
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result(timeout=90) for future in [executor.submit(self.worker, barrier) for _ in range(2)]]
        frappe.db.rollback()
        booked = [row for result in results for row in result["results"] if row["code"] == "BOOKED" and row["bank_transaction"] == line.name]
        self.assertEqual(len(booked), 1)
        audit = frappe.get_doc("Bank Rule Booking", line.name)
        self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": audit.journal_entry}), 2)
        self.assertEqual(frappe.db.count("Bank Transaction Payments", {"parent": line.name}), 1)

    def test_blocker_scan_does_not_lock_unrelated_customer(self):
        from books_assistant.bank_automation import _existing_candidate
        _rule, line = self.prepare()
        frappe.db.commit()
        self.committed = True
        self.assertIsNone(_existing_candidate(line, self.fixture["bank_gl"], {"company": self.fixture["company"], "currency": "USD"}))
        def update():
            frappe.init(site=self.site, sites_path=self.sites_path)
            frappe.connect()
            try:
                frappe.db.sql("set session innodb_lock_wait_timeout=2")
                frappe.db.sql("update `tabCustomer` set customer_details=customer_details where name=%s", self.fixture["customer"])
                frappe.db.rollback()
                return True
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertTrue(executor.submit(update).result(timeout=10))

    def test_current_reader_sees_committed_blocker_after_rr_snapshot(self):
        from books_assistant.bank_automation import _existing_candidate
        _rule, line = self.prepare()
        frappe.db.commit()
        self.committed = True
        frappe.db.sql("set session transaction isolation level repeatable read")
        frappe.db.sql("select name from `tabJournal Entry` limit 1")
        def create():
            frappe.init(site=self.site, sites_path=self.sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            try:
                journal = frappe.get_doc({"doctype": "Journal Entry", "company": self.fixture["company"],
                    "posting_date": today(), "user_remark": self.keyword,
                    "accounts": [{"account": self.fixture["expense_account"], "debit_in_account_currency": 32.49, "cost_center": self.fixture["cost_center"]},
                                 {"account": self.fixture["bank_gl"], "credit_in_account_currency": 32.49}]}).insert()
                journal.submit()
                frappe.db.commit()
                return journal.name
            finally:
                frappe.destroy()
        with ThreadPoolExecutor(max_workers=1) as executor:
            journal = executor.submit(create).result(timeout=20)
        self.history.append((line.name, journal))
        self.assertEqual(_existing_candidate(line, self.fixture["bank_gl"], {"company": self.fixture["company"], "currency": "USD"}), "EXISTING_JOURNAL")


def run_real_tests(include_api=False, include_funding=False, include_health=False):
    _guard()
    modules = [__name__]
    if include_api:
        modules += ["books_assistant.tests.test_billing_api", "books_assistant.tests.test_integration_transactions",
                    "books_assistant.tests.test_finance_synthetic", "books_assistant.tests.test_bank_discovery_transactions",
                    "books_assistant.tests.test_bank_supplier_permissions", "books_assistant.tests.test_read_api_permissions",
                    "books_assistant.tests.test_snapshot_field_permissions"]
    if include_funding:
        modules.append("books_assistant.tests.test_funding_synthetic")
    if include_health:
        if "books_health" not in frappe.get_installed_apps():
            raise AssertionError("Install Books Health before requesting its real tests")
        modules.append("books_health.tests.test_api_synthetic")
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    previous = frappe.flags.in_test
    frappe.flags.in_test = True
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful() or result.skipped:
            raise AssertionError(f"Automation tests failed: {len(result.failures)} failures, {len(result.errors)} errors, {len(result.skipped)} skips")
        return {"tests_run": result.testsRun, "failures": 0, "errors": 0, "skipped": 0}
    finally:
        frappe.flags.in_test = previous
