"""Exercise booking trajectories with fake ERP boundaries, including rollback."""

import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from books_assistant.tests import test_bank_auto_guards as guards
from books_assistant.tests.test_bank_auto_guards import Doc
from books_assistant.tests.test_bank_learning_policy import consistent_history


class TestBankAutomaticBookingRuntime(unittest.TestCase):
    def setUp(self):
        fixture = guards.TestAutomaticBookingGuards()
        fixture.setUp()
        self.auto, self.frappe, self.rule = fixture.auto, fixture.frappe, fixture.rule
        self.raw_settings = self.auto._settings
        self.raw_existing_candidate = self.auto._existing_candidate
        self.frappe.flags = SimpleNamespace()
        self.frappe.log_error = Mock()
        self.frappe.db.set_single_value = Mock()
        self.frappe.db.savepoint = Mock()
        self.frappe.db.rollback = Mock(side_effect=self.rollback)
        self.frappe.db.exists = Mock(side_effect=self.exists)
        self.frappe.db.get_value = Mock(side_effect=lambda doctype, *a, **kw: "JE-AUTO" if doctype == "Bank Transaction Payments" else 1)
        self.frappe.get_list = Mock(side_effect=lambda dt, **kw: ["Operating Bank"] if dt == "Bank Account" else ["BT-NEXT"])
        self.bt = Doc(name="BT-NEXT", docstatus=1, bank_account="Operating Bank", deposit=0, withdrawal=20,
                      unallocated_amount=20, currency="USD", description="GITHUB SUBSCRIPTION", date="2026-11-13")
        self.bank = Doc(name="Operating Bank", account="Bank GL", company="Co-op", is_company_account=1)
        self.gl = Doc(name="Bank GL", company="Co-op", account_currency="USD", account_type="Bank", disabled=0, is_group=0)
        self.ledger = Doc(name="Software Expense", root_type="Expense", company="Co-op", account_currency="USD", disabled=0,
                          is_group=0, account_type="Expense Account")
        self.rule.matches = lambda bt, bank: self.rule.min_amount <= (bt.withdrawal or bt.deposit) <= self.rule.max_amount
        self.settings = Doc(bank_auto_book_enabled=1, bank_auto_learn_enabled=0, bank_auto_book_user="finance@example.test")
        self.booked, self.audited = False, False
        self.documents = {("Bank Transaction", self.bt.name): self.bt, ("Bank Account", self.bank.name): self.bank,
                          ("Account", self.gl.name): self.gl, ("Account", self.ledger.name): self.ledger,
                          ("Bank Rule", self.rule.name): self.rule}
        self.integration = ModuleType("books_assistant.integration")
        self.integration.resolve_scope = Mock(return_value={"company": "Co-op", "currency": "USD"})
        self.integration.lock_documents = Mock()
        self.integration.check_document = Mock(side_effect=lambda dt, name, **kw: self.documents[(dt, name)])
        self.review = ModuleType("books_assistant.bank_review")
        self.review.bank_line = Mock(return_value=self.bt)
        self.review.execute = Mock(side_effect=self.execute)
        suggestion = {"kind": "rule", "rule": self.rule.name, "account": self.rule.account,
                      "bank_transaction": self.bt.name, "amount": 20}
        self.review.Context = Mock(side_effect=lambda: SimpleNamespace(rules=[self.rule], suggest=Mock(return_value=suggestion)))
        import books_assistant
        self.patches = [
            patch.dict(sys.modules, {"books_assistant.integration": self.integration, "books_assistant.bank_review": self.review}),
            patch.object(books_assistant, "bank_review", self.review, create=True),
            patch.object(self.auto, "_settings", side_effect=lambda: self.settings),
            patch.object(self.auto, "_current_rules", side_effect=lambda: [self.rule]),
            patch.object(self.auto, "_existing_candidate", return_value=None),
            patch.object(self.auto.learning, "load_observations", return_value=consistent_history()),
            patch.object(self.auto, "record_rule_booking", side_effect=self.record),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def exists(self, doctype, *args, **kwargs):
        return self.booked if doctype == "Bank Transaction Payments" else self.audited if doctype == "Bank Rule Booking" else False

    def execute(self, suggestion):
        self.booked = True
        self.bt.unallocated_amount = 0
        return "Created JE-AUTO"

    def record(self, *args, **kwargs):
        self.audited = True

    def rollback(self, **kwargs):
        self.booked = self.audited = False
        self.bt.unallocated_amount = 20

    def codes(self, result):
        return [row["code"] for row in result["results"]]

    def test_authorized_rule_books_and_records_result_once_across_retries(self):
        result = self.auto._run("Co-op")
        self.assertEqual(self.codes(result), ["BOOKED"])
        self.assertTrue(self.booked and self.audited)
        self.assertEqual(result["results"][0]["journal_entry"], "JE-AUTO")
        self.auto.record_rule_booking.assert_called_once_with("BT-NEXT", "GitHub", "JE-AUTO", automatic=True)
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["NOT_UNRECONCILED"])
        self.review.execute.assert_called_once()
        self.assertFalse(self.frappe.flags.bank_auto_booking)

    def test_recording_failure_rolls_back_journal_and_reconciliation_and_allows_retry(self):
        self.auto.record_rule_booking.side_effect = RuntimeError("audit storage unavailable")
        result = self.auto._run("Co-op")
        self.assertEqual(self.codes(result), ["EXECUTION_FAILED"])
        self.assertFalse(self.booked or self.audited)
        self.assertEqual(self.bt.unallocated_amount, 20)
        self.frappe.db.rollback.assert_called_once_with(save_point="bank_auto_0")
        self.assertFalse(self.frappe.flags.bank_auto_booking)
        self.auto.record_rule_booking.side_effect = self.record
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["BOOKED"])

    def test_execution_failure_restores_flags_and_rolls_back(self):
        def failed(suggestion):
            self.execute(suggestion)
            raise RuntimeError("ERP validation failed")
        self.review.execute.side_effect = failed
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["EXECUTION_FAILED"])
        self.assertFalse(self.booked)
        self.auto.record_rule_booking.assert_not_called()
        self.assertFalse(self.frappe.flags.bank_auto_booking)

    def test_partial_bank_line_never_executes(self):
        self.bt.unallocated_amount = 10
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["NOT_UNRECONCILED"])
        self.review.execute.assert_not_called()

    def test_out_of_reviewed_amount_range_never_executes(self):
        self.bt.withdrawal = self.bt.unallocated_amount = 50
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["NO_AUTHORIZED_RULE"])
        self.review.execute.assert_not_called()

    def test_hidden_existing_candidate_prevents_creating_a_second_booking(self):
        self.auto._existing_candidate.return_value = "EXISTING_PAYMENT"
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["EXISTING_PAYMENT"])
        self.review.execute.assert_not_called()

    def test_changed_evidence_blocks_execution_before_side_effects(self):
        self.auto.learning.load_observations.return_value = consistent_history()[:2]
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["EVIDENCE_CHANGED"])
        self.review.execute.assert_not_called()

    def test_competing_rule_to_different_account_blocks_even_outside_amount_range(self):
        competing = Doc({**self.rule, "name": "Different destination", "account": "Other Expense", "min_amount": 100})
        self.auto._current_rules.side_effect = lambda: [self.rule, competing]
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["AMBIGUOUS_RULE"])
        self.review.execute.assert_not_called()

    def test_kill_switch_before_execution_stops_booking(self):
        enabled = self.settings
        disabled = Doc({**enabled, "bank_auto_book_enabled": 0})
        self.auto._settings.side_effect = [enabled, enabled, disabled]
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["DISABLED"])
        self.review.execute.assert_not_called()

    def test_scheduler_actor_change_fails_before_classification_or_writes(self):
        self.settings.bank_auto_book_user = "other-finance@example.test"
        with self.assertRaises(ValueError):
            self.auto._run("Co-op", scheduled=True)
        self.review.execute.assert_not_called()
        self.auto.learning.load_observations.assert_not_called()

    def test_manual_run_never_impersonates_configured_account(self):
        self.settings.bank_auto_book_user = "other-finance@example.test"
        self.auto.run("Co-op")
        self.frappe.only_for.assert_called_once_with(self.auto.APPROVERS)
        self.frappe.set_user.assert_not_called()

    def test_settings_use_current_reads_and_parse_disabled_string_values(self):
        self.frappe.db.sql = Mock(return_value=[("bank_auto_book_enabled", "0"), ("bank_auto_learn_enabled", "1"),
                                                ("bank_auto_book_user", "finance@example.test")])
        settings = self.raw_settings()
        self.assertFalse(settings["bank_auto_book_enabled"])
        self.assertTrue(settings["bank_auto_learn_enabled"])
        self.assertEqual(settings["bank_auto_book_user"], "finance@example.test")
        self.assertIn("for update", self.frappe.db.sql.call_args.args[0].lower())

    def test_raw_hidden_voucher_or_transfer_blocker_does_not_depend_on_context_visibility(self):
        for table, code in (("tabPayment Entry", "EXISTING_PAYMENT"), ("tabJournal Entry` je", "EXISTING_JOURNAL"),
                            ("tabBank Transaction` bt", "TRANSFER_CANDIDATE"), ("tabPurchase Invoice", "INVOICE_CANDIDATE")):
            with self.subTest(code=code):
                self.frappe.db.sql = Mock(side_effect=lambda sql, *a, **kw: [("hidden-document",)] if table in sql else [])
                result = self.raw_existing_candidate(self.bt, self.gl.name, {"company": "Co-op", "currency": "USD"})
                self.assertEqual(result, code)
                self.assertNotIn("hidden-document", result)

    def test_raw_hidden_party_candidate_blocks_expense_classification(self):
        self.frappe.db.sql = Mock(return_value=[Doc(name="Hidden Customer", customer_name="GITHUB SUBSCRIPTION", bank_names="")])
        self.assertEqual(self.raw_existing_candidate(self.bt, self.gl.name, {"company": "Co-op", "currency": "USD"}),
                         "PARTY_CANDIDATE")


    def test_unmatched_lines_advance_scan_cursor_without_booking(self):
        self.auto._current_rules.side_effect = lambda: []
        result = self.auto._run("Co-op")
        self.assertEqual(self.codes(result), ["NO_MATCHING_RULE"])
        self.review.execute.assert_not_called()
        self.frappe.db.set_single_value.assert_called_once_with("Automation Settings", "bank_auto_scan_after", "BT-NEXT")

    def test_scan_resumes_after_previous_review_only_lines(self):
        self.settings.bank_auto_scan_after = "BT-EARLIER"
        self.auto._run("Co-op")
        query = next(call for call in self.frappe.get_list.call_args_list if call.args[0] == "Bank Transaction")
        self.assertEqual(query.kwargs["filters"]["name"], [">", "BT-EARLIER"])
        self.assertEqual(query.kwargs["order_by"], "name")
        self.assertEqual(query.kwargs["limit_page_length"], self.auto.MAX_LINES_PER_RUN)

    def test_scan_wraps_when_no_names_follow_previous_cursor(self):
        self.settings.bank_auto_scan_after = "BT-ZZZ"
        filters_seen = []
        def get_list(doctype, **kwargs):
            if doctype == "Bank Account":
                return ["Operating Bank"]
            filters_seen.append(dict(kwargs["filters"]))
            return [] if "name" in kwargs["filters"] else ["BT-NEXT"]
        self.frappe.get_list.side_effect = get_list
        self.assertEqual(self.codes(self.auto._run("Co-op")), ["BOOKED"])
        self.assertEqual(filters_seen[0]["name"], [">", "BT-ZZZ"])
        self.assertNotIn("name", filters_seen[1])


    def test_operator_summary_retains_decision_counts_without_record_details(self):
        self.auto.run("Co-op")
        summary_call = next(call for call in self.frappe.db.set_single_value.call_args_list
                            if call.args[1] == "bank_auto_last_summary")
        import json
        summary = json.loads(summary_call.args[2])
        self.assertEqual(summary["decisions"], {"BOOKED": 1})
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["action_id"], "bank.auto.run")
        for private in ("BT-NEXT", "JE-AUTO", "GitHub", "finance@example.test"):
            self.assertNotIn(private, summary_call.args[2])
