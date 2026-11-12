"""Bank automation cannot be enabled without a permitted runtime owner."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class TestBankAutomationSettings(unittest.TestCase):
    def setUp(self):
        frappe = ModuleType("frappe")
        frappe.throw = Mock(side_effect=ValueError)
        frappe.db = SimpleNamespace(get_value=Mock(return_value=1))
        frappe.get_roles = Mock(return_value=["Accounts Manager"])
        installer = ModuleType("frappe.installer")
        installer.update_site_config = Mock()
        document = ModuleType("frappe.model.document")
        document.Document = object
        utils = ModuleType("frappe.utils")
        utils.add_days = utils.today = Mock()
        utils.flt = lambda value: float(value or 0)
        email = ModuleType("books_assistant.invoice_email")
        email.split_emails = Mock()
        path = Path(__file__).parents[1] / "books_assistant/doctype/automation_settings/automation_settings.py"
        spec = importlib.util.spec_from_file_location("bank_settings_test_subject", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": frappe, "frappe.installer": installer,
                                     "frappe.model.document": document, "frappe.utils": utils,
                                     "books_assistant.invoice_email": email}):
            spec.loader.exec_module(module)
        self.validate = module.validate_bank_automation
        self.frappe = frappe

    def test_disabled_automation_can_be_saved_without_a_user(self):
        self.validate({})
        self.frappe.db.get_value.assert_not_called()
        self.frappe.get_roles.assert_not_called()

    def test_new_check_field_string_zero_defaults_do_not_require_actor(self):
        self.validate({"bank_auto_learn_enabled": "0", "bank_auto_book_enabled": "0"})
        self.frappe.db.get_value.assert_not_called()

    def test_either_switch_requires_an_enabled_actor(self):
        for switch in ("bank_auto_learn_enabled", "bank_auto_book_enabled"):
            with self.subTest(switch=switch):
                for user in (None, "Guest"):
                    with self.assertRaises(ValueError):
                        self.validate({switch: 1, "bank_auto_book_user": user})
                self.frappe.db.get_value.return_value = 0
                with self.assertRaises(ValueError):
                    self.validate({switch: 1, "bank_auto_book_user": "disabled@example.test"})
                self.frappe.db.get_value.return_value = 1

    def test_discord_or_generic_admin_role_does_not_authorize_finance(self):
        self.frappe.get_roles.return_value = ["Admin", "Employee"]
        with self.assertRaises(ValueError):
            self.validate({"bank_auto_book_enabled": 1, "bank_auto_book_user": "user@example.test"})

    def test_each_financial_approver_role_can_own_the_runtime(self):
        for role in ("Accounts Manager", "System Manager"):
            self.frappe.get_roles.return_value = [role]
            self.validate({"bank_auto_book_enabled": 1, "bank_auto_book_user": "finance@example.test"})
            self.frappe.db.get_value.assert_called_with("User", "finance@example.test", "enabled")
            self.frappe.get_roles.assert_called_with("finance@example.test")


if __name__ == "__main__":
    unittest.main()
