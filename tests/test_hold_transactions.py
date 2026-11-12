"""Hold hooks preserve database transaction failures for the outer retry owner."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class TestHoldTransactionFailures(unittest.TestCase):
    def setUp(self):
        self.frappe = ModuleType("frappe")
        self.frappe._ = lambda text: text
        self.frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
        self.frappe.QueryTimeoutError = type("QueryTimeoutError", (Exception,), {})
        self.frappe.db = SimpleNamespace(savepoint=Mock(), rollback=Mock())
        self.frappe.log_error = Mock()
        spec = importlib.util.spec_from_file_location("holds_subject", Path(__file__).parents[1] / "books_assistant" / "holds.py")
        self.holds = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": self.frappe}):
            spec.loader.exec_module(self.holds)
        self.doc = SimpleNamespace(doctype="Journal Entry", name="JE-1")

    def assert_transient_escapes(self, error_type):
        original = error_type("transaction aborted")
        with patch.object(self.holds, "release_paid_holds", side_effect=original):
            with self.assertRaises(error_type) as caught:
                self.holds.release_after_payment(self.doc)
        self.assertIs(caught.exception, original)
        self.frappe.db.rollback.assert_not_called()
        self.frappe.log_error.assert_not_called()

    def test_deadlock_reaches_outer_transaction(self):
        self.assert_transient_escapes(self.frappe.QueryDeadlockError)

    def test_timeout_reaches_outer_transaction(self):
        self.assert_transient_escapes(self.frappe.QueryTimeoutError)

    def test_ordinary_release_failure_remains_isolated(self):
        with patch.object(self.holds, "release_paid_holds", side_effect=ValueError("invalid bill")):
            self.holds.release_after_payment(self.doc)
        self.frappe.db.rollback.assert_called_once_with(save_point="release_holds")
        self.frappe.log_error.assert_called_once()
