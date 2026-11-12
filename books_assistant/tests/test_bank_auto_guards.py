"""Authorization and evidence guards, isolated from Frappe infrastructure."""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from books_assistant.tests.test_bank_learning_policy import TODAY, consistent_history, observation


class Doc(dict):
    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value

    def set(self, key, value):
        self[key] = value


class TestAutomaticBookingGuards(unittest.TestCase):
    def setUp(self):
        frappe = ModuleType("frappe")
        frappe._ = lambda text: text
        frappe.whitelist = lambda **kwargs: lambda fn: fn
        frappe.only_for = Mock()
        frappe.throw = Mock(side_effect=ValueError)
        frappe.session = SimpleNamespace(user="finance@example.test")
        frappe.set_user = Mock(side_effect=lambda user: setattr(frappe.session, "user", user))
        frappe.db = SimpleNamespace(get_value=Mock(return_value=1))
        frappe.get_roles = Mock(return_value=["Accounts Manager"])
        utils = ModuleType("frappe.utils")
        utils.flt = lambda value: float(value or 0)
        utils.today = lambda: str(TODAY)
        utils.now_datetime = lambda: "2026-11-13 12:00:00"
        spec = importlib.util.spec_from_file_location("bank_auto_guard_subject", Path(__file__).parents[1] / "bank_automation.py")
        self.auto = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
            spec.loader.exec_module(self.auto)
        self.frappe = frappe
        self.rule = Doc(name="GitHub", enabled=1, auto_book=1, description_contains="GITHUB", direction="Money out",
                        bank_account="Operating Bank", account="Software Expense", min_amount=10, max_amount=30,
                        auto_approved_by="reviewer@example.test", auto_approved_on="2026-11-13 10:00:00")
        self.approval = {"version": 1, "scope": {"company": "Co-op", "currency": "USD"},
                         "rule": self.auto.rule_config(self.rule), "evidence": consistent_history()}
        self.rule.auto_approval_json = json.dumps(self.approval)

    def test_unchanged_reviewed_evidence_authorizes_rule(self):
        self.assertTrue(self.auto.evidence_still_valid(self.rule, consistent_history()))

    def test_changed_category_limits_or_merchant_invalidate_review(self):
        for field, value in (("account", "Other Expense"), ("min_amount", 1), ("max_amount", 300),
                             ("description_contains", "KAGI"), ("bank_account", "Savings"), ("direction", "Money in")):
            with self.subTest(field=field):
                changed = Doc({**self.rule, field: value})
                self.assertFalse(self.auto.evidence_still_valid(changed, consistent_history()))

    def test_removed_or_edited_source_requires_review(self):
        self.assertFalse(self.auto.evidence_still_valid(self.rule, consistent_history()[:2]))
        history = consistent_history()
        history[0] = {**history[0], "amount": 100}
        self.assertFalse(self.auto.evidence_still_valid(self.rule, history))

    def test_new_conflict_blocks_previously_reviewed_rule(self):
        history = consistent_history() + [{**consistent_history()[0], "bank_transaction": "BT-NEW", "account": "Other Expense"}]
        self.assertFalse(self.auto.evidence_still_valid(self.rule, history))

    def test_alternate_merchant_key_conflict_invalidates_existing_automatic_authorization(self):
        conflict = observation(4, description="PAYMENT GITHUB", account="Other Expense")
        self.assertFalse(self.auto.evidence_still_valid(self.rule, consistent_history() + [conflict]))

    def test_alternate_merchant_key_ineligible_history_invalidates_existing_authorization(self):
        marker = observation(4, description="PAYMENT GITHUB", ineligible=True)
        marker.pop("account")
        self.assertFalse(self.auto.evidence_still_valid(self.rule, consistent_history() + [marker]))

    def test_conflicting_classification_outside_frozen_amount_range_does_not_expand_or_disable_rule(self):
        conflict = observation(4, description="PAYMENT GITHUB", account="Other Expense", amount=1000)
        self.assertTrue(self.auto.evidence_still_valid(self.rule, consistent_history() + [conflict]))
        self.assertEqual(self.auto.rule_config(self.rule)["max_amount"], 30)

    def test_new_consistent_sources_do_not_expand_reviewed_rule_limits(self):
        history = consistent_history() + [{**consistent_history()[0], "bank_transaction": "BT-NEW", "amount": 1000}]
        self.assertTrue(self.auto.evidence_still_valid(self.rule, history))
        self.assertEqual(self.auto.rule_config(self.rule)["max_amount"], 30)

    def test_missing_actor_or_review_time_cannot_authorize_money(self):
        for field in ("auto_approved_by", "auto_approved_on"):
            with self.subTest(field=field):
                self.assertFalse(self.auto.evidence_still_valid(Doc({**self.rule, field: None}), consistent_history()))

    def test_wrong_company_or_currency_cannot_reuse_review(self):
        for key, value in (("company", "Another Company"), ("currency", "EUR")):
            with self.subTest(key=key):
                approval = {**self.approval, "scope": {**self.approval["scope"], key: value}}
                changed = Doc({**self.rule, "auto_approval_json": json.dumps(approval)})
                self.assertFalse(self.auto.evidence_still_valid(changed, consistent_history()))

    def test_duplicate_reviewed_sources_do_not_reach_threshold(self):
        history = consistent_history()
        approval = {**self.approval, "evidence": [history[0], history[0], history[1]]}
        changed = Doc({**self.rule, "auto_approval_json": json.dumps(approval)})
        self.assertFalse(self.auto.evidence_still_valid(changed, history))

    def test_malformed_review_requires_review_instead_of_crashing(self):
        for content in (None, "bad json", "null", "[]", '{"version":1,"evidence":[null]}'):
            with self.subTest(content=content):
                changed = Doc({**self.rule, "auto_approval_json": content})
                self.assertFalse(self.auto.evidence_still_valid(changed, consistent_history()))

    def test_overlap_detection_ignores_competing_amount_range(self):
        transaction = Doc(bank_account="Operating Bank", deposit=0, withdrawal=20, description="GITHUB SUBSCRIPTION")
        competing = Doc({**self.rule, "min_amount": 100, "max_amount": 200})
        self.assertTrue(self.auto.overlaps(competing, transaction))
        self.assertFalse(self.auto.overlaps(competing, Doc({**transaction, "bank_account": "Other Bank"})))
        self.assertFalse(self.auto.overlaps(competing, Doc({**transaction, "deposit": 20, "withdrawal": 0})))
        self.assertFalse(self.auto.overlaps(competing, Doc({**transaction, "description": "KAGI"})))

    def test_scheduled_execution_restores_actor_on_failure(self):
        settings = Doc(bank_auto_book_enabled=1, bank_auto_book_user="scheduled@example.test")
        with patch.object(self.auto, "_settings", return_value=settings), patch.object(self.auto, "_run", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.auto.run_scheduled()
        self.assertEqual(self.frappe.session.user, "finance@example.test")
        self.assertEqual(self.frappe.set_user.call_args_list[0].args, ("scheduled@example.test",))
        self.assertEqual(self.frappe.set_user.call_args_list[-1].args, ("finance@example.test",))

    def test_disabled_schedule_has_no_execution_or_actor_switch(self):
        with patch.object(self.auto, "_settings", return_value=Doc()), patch.object(self.auto, "_run") as run:
            self.assertEqual(self.auto.run_scheduled()["code"], "DISABLED")
        run.assert_not_called()
        self.frappe.set_user.assert_not_called()

    def test_configured_user_cannot_be_disabled_or_nonfinancial(self):
        settings = Doc(bank_auto_book_user="scheduled@example.test")
        self.frappe.db.get_value.return_value = 0
        with self.assertRaises(ValueError):
            self.auto._configured_user(settings)
        self.frappe.db.get_value.return_value = 1
        self.frappe.get_roles.return_value = ["Employee"]
        with self.assertRaises(ValueError):
            self.auto._configured_user(settings)


    def _authorization_dependencies(self):
        integration = ModuleType("books_assistant.integration")
        integration.lock_documents = Mock()
        integration.canonical = lambda value: json.dumps(value, sort_keys=True)
        integration.resolve_scope = Mock(return_value={"company": "Co-op", "currency": "USD"})
        integration.check_document = Mock(side_effect=lambda doctype, *args, **kwargs:
            Doc(company="Co-op", is_company_account=1) if doctype == "Bank Account" else
            Doc(root_type="Expense", account_type="Expense Account", disabled=0, is_group=0))
        return integration

    def test_enabling_auto_book_freezes_evidence_and_authenticated_reviewer(self):
        self.rule.get_doc_before_save = Mock(return_value=None)
        integration = self._authorization_dependencies()
        with patch.dict(sys.modules, {"books_assistant.integration": integration}), patch.object(
            self.auto.learning, "load_observations", return_value=consistent_history()
        ):
            self.auto.validate_auto_rule(self.rule)
        approved = json.loads(self.rule.auto_approval_json)
        self.assertEqual(approved["evidence"], consistent_history())
        self.assertEqual(approved["scope"], {"company": "Co-op", "currency": "USD"})
        self.assertEqual(self.rule.auto_approved_by, "finance@example.test")
        self.assertEqual(self.rule.evidence_count, 3)
        self.assertEqual(self.rule.source_transactions, "BT-1\nBT-2\nBT-3")

    def test_insufficient_history_cannot_enable_automatic_booking(self):
        self.rule.get_doc_before_save = Mock(return_value=None)
        with patch.dict(sys.modules, {"books_assistant.integration": self._authorization_dependencies()}), patch.object(
            self.auto.learning, "load_observations", return_value=consistent_history()[:2]
        ):
            with self.assertRaises(ValueError):
                self.auto.validate_auto_rule(self.rule)

    def test_alternate_merchant_key_conflict_cannot_enable_automatic_booking(self):
        self.rule.get_doc_before_save = Mock(return_value=None)
        conflict = observation(4, description="PAYMENT GITHUB", account="Other Expense")
        with patch.dict(sys.modules, {"books_assistant.integration": self._authorization_dependencies()}), patch.object(
            self.auto.learning, "load_observations", return_value=consistent_history() + [conflict]
        ):
            with self.assertRaises(ValueError):
                self.auto.validate_auto_rule(self.rule)

    def test_nonfinite_missing_or_reversed_limits_cannot_enable_booking(self):
        with patch.dict(sys.modules, {"books_assistant.integration": self._authorization_dependencies()}):
            for lower, upper in ((0, 30), (10, 0), (30, 10), (float("nan"), 30), (10, float("inf"))):
                with self.subTest(lower=lower, upper=upper):
                    changed = Doc({**self.rule, "min_amount": lower, "max_amount": upper})
                    changed.get_doc_before_save = Mock(return_value=None)
                    with self.assertRaises(ValueError):
                        self.auto.validate_auto_rule(changed)

    def test_active_authorization_metadata_cannot_be_replaced_from_client(self):
        previous = Doc(self.rule)
        self.rule.get_doc_before_save = Mock(return_value=previous)
        self.rule.auto_approval_json = "forged"
        self.rule.auto_approved_by = "forged@example.test"
        with patch.dict(sys.modules, {"books_assistant.integration": self._authorization_dependencies()}):
            self.auto.validate_auto_rule(self.rule)
        self.assertEqual(self.rule.auto_approval_json, previous.auto_approval_json)
        self.assertEqual(self.rule.auto_approved_by, previous.auto_approved_by)

    def test_active_authorized_rule_cannot_change_its_destination_or_limits(self):
        previous = Doc(self.rule)
        self.rule.get_doc_before_save = Mock(return_value=previous)
        self.rule.account = "Other Expense"
        with patch.dict(sys.modules, {"books_assistant.integration": self._authorization_dependencies()}):
            with self.assertRaises(ValueError):
                self.auto.validate_auto_rule(self.rule)


if __name__ == "__main__":
    unittest.main()
