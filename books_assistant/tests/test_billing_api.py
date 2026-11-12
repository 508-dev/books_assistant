"""Site-backed billing permission/state tests. Run on a disposable copy via bench."""

import unittest
from unittest.mock import patch

import frappe

from books_assistant.billing_api import preview_billing_approval
from books_assistant.books_assistant.doctype.billing_run import billing_run


class TestBillingAPI(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.run = frappe.get_doc({
            "doctype": "Billing Run", "period_start": "1900-01-01", "period_end": "1900-01-31",
            "invoice_date": "1900-02-01", "status": "Draft",
        }).insert()

    def tearDown(self):
        frappe.db.rollback()

    def test_resource_updates_cannot_forge_approved_state(self):
        self.run.status = "Approved"
        with self.assertRaises(frappe.ValidationError):
            self.run.save()
        self.assertEqual(frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft")

    def test_client_supplied_dates_are_reloaded_before_preparing(self):
        # frm.call may carry an entire client-side doc. The saved dates remain authoritative.
        self.run.period_start = "2020-01-01"
        seen = []

        def no_projects(run):
            seen.append(str(run.period_start))
            return []

        with patch.object(billing_run, "projects_to_bill", side_effect=no_projects), \
                self.assertRaises(frappe.ValidationError):
            self.run.prepare()
        self.assertEqual(seen, ["1900-01-01"])
        self.assertEqual(frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft")

    def test_empty_selection_creates_no_preview(self):
        count = frappe.db.count("API Preview")
        for projects in ([], "[]", None):
            with self.subTest(projects=projects), self.assertRaises(frappe.ValidationError):
                preview_billing_approval(self.run.name, projects)
        self.assertEqual(frappe.db.count("API Preview"), count)

    def test_approval_without_review_changes_no_documents(self):
        count = frappe.db.count("API Request")
        with self.assertRaises(frappe.ValidationError):
            self.run.approve(projects=["not-a-real-project"])
        self.assertEqual(frappe.db.count("API Request"), count)
        self.assertEqual(frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft")
