"""Rule-set changes share the posting lock, including new rules and deletion."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class TestBankRuleSerialization(unittest.TestCase):
    def setUp(self):
        frappe = ModuleType("frappe")
        frappe._ = lambda value: value
        frappe.db = SimpleNamespace(get_value=Mock(side_effect=lambda dt, name, field: {"New Ledger": "Z Company", "Old Ledger": "A Company"}[name]), sql=Mock())
        utils = ModuleType("frappe.utils")
        utils.flt = float
        document = ModuleType("frappe.model.document")
        document.Document = object
        spec = importlib.util.spec_from_file_location("bank_rule_serialization_subject", Path(__file__).parents[1] / "books_assistant/doctype/bank_rule/bank_rule.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils, "frappe.model.document": document}):
            spec.loader.exec_module(module)
        self.rule = module.BankRule()
        self.rule.account = "New Ledger"
        self.rule.get_doc_before_save = Mock(return_value=None)
        self.frappe = frappe

    def test_new_rule_locks_destination_company_before_becoming_visible(self):
        self.rule._lock_companies()
        self.assertEqual(self.frappe.db.sql.call_args.args[1], ("Z Company",))
        self.assertIn("for update", self.frappe.db.sql.call_args.args[0].lower())

    def test_reassignment_locks_both_companies_in_stable_order(self):
        self.rule.get_doc_before_save.return_value = SimpleNamespace(account="Old Ledger", get=lambda key: "Old Ledger")
        self.rule._lock_companies()
        self.assertEqual([call.args[1] for call in self.frappe.db.sql.call_args_list], [("A Company",), ("Z Company",)])

    def test_deleting_or_renaming_rule_uses_same_company_lock(self):
        self.rule.on_trash()
        self.rule.before_rename("Old Name", "New Name")
        self.assertEqual(self.frappe.db.sql.call_count, 2)
        self.assertTrue(all(call.args[1] == ("Z Company",) for call in self.frappe.db.sql.call_args_list))
