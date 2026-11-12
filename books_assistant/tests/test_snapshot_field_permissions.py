"""Real Frappe field-permission checks for public financial approval previews."""

import unittest
import uuid

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from books_assistant import billing_api
from books_assistant.tests import test_finance_synthetic as finance_fixtures


class TestSnapshotFieldPermissions(unittest.TestCase):
    invoice = finance_fixtures.TestSyntheticFinanceTrajectories.invoice
    prepared_run = finance_fixtures.TestSyntheticFinanceTrajectories.prepared_run

    def setUp(self):
        finance_fixtures.TestSyntheticFinanceTrajectories.setUp(self)
        self.protected_doctypes = set()

    def tearDown(self):
        finance_fixtures.TestSyntheticFinanceTrajectories.tearDown(self)
        for doctype in getattr(self, "protected_doctypes", set()):
            frappe.clear_cache(doctype=doctype)

    def protect(self, doctype, fieldname):
        make_property_setter(doctype, fieldname, "permlevel", 9, "Int")
        self.protected_doctypes.add(doctype)
        frappe.clear_cache(doctype=doctype)

    def reviewer(self):
        roles = [
            "Accounts Manager",
            "Accounts User",
            "Billing Approver",
            "Projects User",
            "Sales User",
        ]
        return frappe.get_doc(
            {
                "doctype": "User",
                "email": f"field-review-{uuid.uuid4()}@example.test",
                "first_name": "Field-permission reviewer",
                "send_welcome_email": 0,
                "roles": [
                    {"role": role} for role in roles if frappe.db.exists("Role", role)
                ],
            }
        ).insert()

    def assert_preview_denied_without_leaking(self, run, project, secret):
        count = frappe.db.count("API Preview")
        with self.assertRaises(frappe.PermissionError):
            result = billing_api.preview_billing_approval(
                run.name, [project.name], self.company
            )
            self.assertFalse(
                secret in frappe.as_json(result),
                "The public preview leaked a field hidden by Frappe",
            )
        self.assertEqual(frappe.db.count("API Preview"), count)

    def test_customer_field_hidden_by_property_setter_cannot_leave_public_preview(self):
        run, project, _sales, _bill = self.prepared_run()
        secret = f"synthetic-private-customer-{uuid.uuid4()}"
        frappe.db.set_value(
            "Customer", self.fixture["customer"], "customer_details", secret
        )
        self.protect("Customer", "customer_details")
        reviewer = self.reviewer()
        frappe.set_user(reviewer.name)
        customer = frappe.get_doc("Customer", self.fixture["customer"])
        customer.check_permission("read")
        self.assertNotIn(9, customer.get_permlevel_access("read"))
        self.assertNotEqual(
            customer.copy_with_fieldlevel_read_permissions().get("customer_details"),
            secret,
        )
        self.assert_preview_denied_without_leaking(run, project, secret)

    def test_child_field_hidden_by_property_setter_cannot_leave_public_preview(self):
        run, project, sales, _bill = self.prepared_run()
        secret = f"synthetic-private-line-{uuid.uuid4()}"
        sales.items[0].description = secret
        sales.save()
        self.protect("Sales Invoice Item", "description")
        reviewer = self.reviewer()
        frappe.set_user(reviewer.name)
        invoice = frappe.get_doc("Sales Invoice", sales.name)
        invoice.check_permission("read")
        invoice.check_permission("submit")
        self.assertNotIn(9, invoice.get_permlevel_access("read"))
        self.assertNotEqual(
            invoice.copy_with_fieldlevel_read_permissions().items[0].get("description"),
            secret,
        )
        self.assert_preview_denied_without_leaking(run, project, secret)

    def test_full_access_keeps_hidden_source_fields_in_stale_preview_comparison(self):
        run, project, _sales, _bill = self.prepared_run()
        self.protect("Customer", "customer_details")
        first = f"synthetic-first-{uuid.uuid4()}"
        frappe.db.set_value(
            "Customer", self.fixture["customer"], "customer_details", first
        )
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], self.company
        )
        customer = next(
            source["document"]
            for source in frozen["snapshot"]["documents"]
            if source["doctype"] == "Customer"
        )
        self.assertEqual(customer["customer_details"], first)
        frappe.db.set_value(
            "Customer",
            self.fixture["customer"],
            "customer_details",
            "synthetic-changed",
        )
        with self.assertRaises(frappe.ValidationError):
            billing_api.approve_billing(frozen["preview_id"], str(uuid.uuid4()))
        self.assertEqual(
            frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 0
        )
