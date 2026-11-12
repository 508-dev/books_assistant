"""Real record and role permissions for read-only finance summaries."""

import json
import unittest
import uuid
from unittest.mock import patch

import frappe

from books_assistant import api
from books_assistant.tests.synthetic_site import seed_minimal_site


def deny_hidden_sales_invoice(doc, ptype=None, user=None, **kwargs):
    if doc.name == frappe.flags.get("read_api_hidden_invoice"):
        return False


class TestReadFinancePermissions(unittest.TestCase):
    def setUp(self):
        if not frappe.conf.get("books_assistant_synthetic_site"):
            self.skipTest("Requires an explicitly enabled synthetic site")
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True
        frappe.flags.books_assistant_locked_documents = set()
        self.fixture = seed_minimal_site()
        self.marker = "read-api-" + uuid.uuid4().hex

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.flags.read_api_hidden_invoice = None
        frappe.clear_cache()

    def user(self, roles):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": self.marker + "@example.test",
                "first_name": "Synthetic read user",
                "send_welcome_email": 0,
                "roles": [{"role": role} for role in roles],
            }
        ).insert(ignore_permissions=True)
        return user.name

    def test_system_manager_can_read_available_sections_without_bank_permission(self):
        from books_assistant.books_assistant.doctype.billing_run.billing_run import editing_run
        from frappe.utils import today

        with editing_run():
            run = frappe.get_doc({"doctype": "Billing Run", "company": self.fixture["company"],
                "currency": "USD", "period_start": today(), "period_end": today(),
                "invoice_date": today(), "status": "Prepared"}).insert()
        user = self.user(["System Manager"])
        frappe.set_user(user)
        self.assertFalse(frappe.has_permission("Bank Account", "read"))
        result = api.get_finance_status(self.fixture["company"])
        self.assertIn("Bank Account", result["unavailable_sources"])
        self.assertEqual(result["unmatched_bank_lines"], [])
        self.assertIn(run.name, {row["name"] for row in result["pending_billing_runs"]})

    def test_settings_initializer_preserves_saved_disabled_switches(self):
        from books_assistant import setup

        expected = {
            "send_invoice_emails": 0,
            "hold_engineer_bills": 0,
            "auto_release_holds": 0,
            "email_subject": "Synthetic saved subject",
        }
        for field, value in expected.items():
            frappe.db.set_single_value("Automation Settings", field, value)
        with patch(
            "frappe.get_single",
            side_effect=AssertionError("existing settings must not be initialized"),
        ):
            setup.initialise_settings()
        for field, value in expected.items():
            current = frappe.db.get_single_value("Automation Settings", field)
            self.assertEqual(int(current) if isinstance(value, int) else current, value)

    def test_unheld_bill_redacts_client_denied_by_actual_permission_hook(self):
        from books_assistant.tests.test_finance_synthetic import (
            TestSyntheticFinanceTrajectories,
        )

        # Reuse document factories without inheriting or rerunning their suite.
        factory = TestSyntheticFinanceTrajectories()
        factory.fixture = self.fixture
        factory.company = self.fixture["company"]
        factory.marker = self.marker
        invoice = factory.invoice("Sales Invoice", 100)
        bill = factory.invoice("Purchase Invoice", 70)
        bill.client_invoice = invoice.name
        bill.submit()
        self.assertFalse(bill.on_hold)
        user = self.user(["Accounts Manager", "Billing Approver"])
        frappe.set_user(user)
        original = frappe.get_hooks

        def hooks(name=None, *args, **kwargs):
            value = original(name, *args, **kwargs)
            if name == "has_permission":
                value = dict(value or {})
                value["Sales Invoice"] = [__name__ + ".deny_hidden_sales_invoice"]
            return value

        frappe.flags.read_api_hidden_invoice = invoice.name
        with patch("frappe.get_hooks", side_effect=hooks):
            self.assertFalse(
                frappe.has_permission("Sales Invoice", "read", doc=invoice.name)
            )
            result = api.get_supplier_payment_status(
                self.fixture["supplier"], self.fixture["company"]
            )
        row = next(row for row in result["bills"] if row["name"] == bill.name)
        self.assertIsNone(row["client_invoice"])
        self.assertIsNone(row["blocking_invoice"])
        self.assertNotIn(invoice.name, json.dumps(result, default=str))
