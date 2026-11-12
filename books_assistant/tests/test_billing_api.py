"""Site-backed billing permission/state tests. Run on a disposable copy via bench."""

import unittest
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from books_assistant import billing_api
from books_assistant.billing_api import preview_billing_approval
from books_assistant.books_assistant.doctype.billing_run import billing_run
from books_assistant.tests import test_finance_synthetic as finance_fixtures


class TestBillingAPI(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.run = frappe.get_doc(
            {
                "doctype": "Billing Run",
                "period_start": "1900-01-01",
                "period_end": "1900-01-31",
                "invoice_date": "1900-02-01",
                "status": "Draft",
            }
        ).insert()

    def tearDown(self):
        frappe.db.rollback()

    def test_resource_updates_cannot_forge_approved_state(self):
        self.run.status = "Approved"
        with self.assertRaises(frappe.ValidationError):
            self.run.save()
        self.assertEqual(
            frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft"
        )

    def test_client_supplied_dates_are_reloaded_before_preparing(self):
        # frm.call may carry an entire client-side doc. The saved dates remain authoritative.
        self.run.period_start = "2020-01-01"
        seen = []

        def no_projects(run):
            seen.append(str(run.period_start))
            return []

        with (
            patch.object(billing_run, "projects_to_bill", side_effect=no_projects),
            self.assertRaises(frappe.ValidationError),
        ):
            self.run.prepare()
        self.assertEqual(seen, ["1900-01-01"])
        self.assertEqual(
            frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft"
        )

    def test_empty_selection_creates_no_preview(self):
        count = frappe.db.count("API Preview")
        for projects in ([], "[]", None):
            with (
                self.subTest(projects=projects),
                self.assertRaises(frappe.ValidationError),
            ):
                preview_billing_approval(self.run.name, projects)
        self.assertEqual(frappe.db.count("API Preview"), count)

    def test_approval_without_review_changes_no_documents(self):
        count = frappe.db.count("API Request")
        with self.assertRaises(frappe.ValidationError):
            self.run.approve(projects=["not-a-real-project"])
        self.assertEqual(frappe.db.count("API Request"), count)
        self.assertEqual(
            frappe.db.get_value("Billing Run", self.run.name, "status"), "Draft"
        )


class TestBillingSnapshotBoundaries(unittest.TestCase):
    """Actual documents and User Permissions on the explicitly enabled synthetic site."""

    setUp = finance_fixtures.TestSyntheticFinanceTrajectories.setUp
    tearDown = finance_fixtures.TestSyntheticFinanceTrajectories.tearDown
    invoice = finance_fixtures.TestSyntheticFinanceTrajectories.invoice
    prepared_run = finance_fixtures.TestSyntheticFinanceTrajectories.prepared_run

    def contact(self, *, billing=False, primary=False):
        return frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": f"Boundary {uuid.uuid4()}",
                "is_billing_contact": int(billing),
                "is_primary_contact": int(primary),
                "email_ids": [
                    {"email_id": f"{uuid.uuid4()}@example.test", "is_primary": 1}
                ],
                "links": [
                    {"link_doctype": "Customer", "link_name": self.fixture["customer"]}
                ],
            }
        ).insert()

    def restricted_reader(self, allowed_contact):
        roles = [
            "Accounts Manager",
            "Accounts User",
            "Billing Approver",
            "Projects User",
            "Sales User",
        ]
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"boundary-{uuid.uuid4()}@example.test",
                "first_name": "Boundary reviewer",
                "send_welcome_email": 0,
                "roles": [
                    {"role": role} for role in roles if frappe.db.exists("Role", role)
                ],
            }
        ).insert()
        frappe.get_doc(
            {
                "doctype": "User Permission",
                "user": user.name,
                "allow": "Contact",
                "for_value": allowed_contact.name,
                "apply_to_all_doctypes": 1,
            }
        ).insert()
        return user

    def test_no_engineer_bills_never_snapshots_unrelated_unbilled_timesheets(self):
        run, project, sales, _bill = self.prepared_run()
        with billing_run.editing_run():
            run.items[0].engineer_bills = ""
            run.save()
        activity = frappe.get_all("Activity Type", pluck="name", limit=1)
        if not activity:
            activity = [
                frappe.get_doc(
                    {"doctype": "Activity Type", "activity_type": self.marker}
                )
                .insert()
                .name
            ]
        start = now_datetime()
        unrelated = frappe.get_doc(
            {
                "doctype": "Timesheet",
                "company": self.company,
                "time_logs": [
                    {
                        "activity_type": activity[0],
                        "from_time": start,
                        "to_time": add_to_date(start, hours=1),
                        "hours": 1,
                    }
                ],
            }
        ).insert()
        frappe.db.set_value("Timesheet", unrelated.name, "purchase_invoice", "")
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], self.company
        )
        sources = {
            (document["doctype"], document["name"])
            for document in frozen["snapshot"]["documents"]
        }
        self.assertNotIn(("Timesheet", unrelated.name), sources)
        self.assertNotIn(
            ("Timesheet", unrelated.name), frappe.flags.books_assistant_locked_documents
        )
        self.assertEqual(frozen["snapshot"]["projects"][0]["engineer_bills"], [])

    def test_explicit_recipients_ignore_a_restricted_unrelated_customer_contact(self):
        run, project, _sales, _bill = self.prepared_run()
        allowed, unrelated = self.contact(primary=True), self.contact()
        user = self.restricted_reader(allowed)
        frappe.db.set_value(
            "Customer", self.fixture["customer"], "invoice_to", "billing@example.test"
        )
        frappe.set_user(user.name)
        self.assertFalse(frappe.has_permission("Contact", "read", unrelated))
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], self.company
        )
        self.assertEqual(
            frozen["snapshot"]["projects"][0]["recipients"], ["billing@example.test"]
        )
        self.assertIsNone(frozen["snapshot"]["projects"][0]["recipient_contact"])
        self.assertFalse(
            any(
                source["doctype"] == "Contact"
                for source in frozen["snapshot"]["documents"]
            )
        )

    def test_actual_contact_fallback_is_required_and_frozen(self):
        run, project, sales, _bill = self.prepared_run()
        actual, unrelated = self.contact(billing=True, primary=True), self.contact()
        frappe.db.set_value("Customer", self.fixture["customer"], "invoice_to", "")
        frappe.db.set_value("Sales Invoice", sales.name, "contact_email", "")
        user = self.restricted_reader(actual)
        frappe.set_user(user.name)
        self.assertFalse(frappe.has_permission("Contact", "read", unrelated))
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], self.company
        )
        self.assertEqual(
            frozen["snapshot"]["projects"][0]["recipient_contact"], actual.name
        )
        sources = {
            (source["doctype"], source["name"])
            for source in frozen["snapshot"]["documents"]
        }
        self.assertIn(("Contact", actual.name), sources)
        self.assertNotIn(("Contact", unrelated.name), sources)

    def test_inaccessible_actual_contact_fallback_denies_preview(self):
        run, project, sales, _bill = self.prepared_run()
        allowed, actual = self.contact(primary=True), self.contact(billing=True)
        user = self.restricted_reader(allowed)
        frappe.db.set_value("Customer", self.fixture["customer"], "invoice_to", "")
        frappe.db.set_value("Sales Invoice", sales.name, "contact_email", "")
        frappe.set_user(user.name)
        self.assertFalse(frappe.has_permission("Contact", "read", actual))
        count = frappe.db.count("API Preview")
        with self.assertRaises(frappe.PermissionError):
            billing_api.preview_billing_approval(run.name, [project.name], self.company)
        self.assertEqual(frappe.db.count("API Preview"), count)

    def test_referral_credit_note_has_negative_total_and_reduces_reviewed_cost(self):
        run, project, sales, _bill = self.prepared_run(1000, 600)
        project.append(
            "referrers", {"supplier": self.fixture["supplier"], "percentage": 10}
        )
        project.save()
        fee = self.invoice("Purchase Invoice", 100, project=project.name)
        fee.referral_source_si, fee.is_referral_fee = sales.name, 1
        fee.save()
        fee.submit()
        credit = frappe.copy_doc(fee)
        credit.is_return, credit.return_against = 1, fee.name
        credit.items[0].qty = -0.4
        credit.insert()
        # ERPNext's return insertion hooks update saved totals/timestamps.
        credit.reload()
        credit.submit()
        self.assertEqual(credit.grand_total, -40)
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], self.company
        )
        selected = frozen["snapshot"]["projects"][0]
        self.assertEqual((selected["referral_cost"], selected["margin_pct"]), (60, 34))
        self.assertEqual(set(selected["referral_bills"]), {fee.name, credit.name})
        sales.reload()
        sales.submit()
        self.assertEqual(
            frappe.db.count(
                "Purchase Invoice",
                {"referral_source_si": sales.name, "docstatus": ["!=", 2]},
            ),
            2,
        )
