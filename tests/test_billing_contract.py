"""Billing contract tests without a live site; database/transaction tests still require bench.

Frappe is injected only while loading isolated module copies, so these tests do
not replace Frappe for other tests in the process.
"""

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "books_assistant/books_assistant/doctype/billing_run/billing_run.py"


class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


class Document:
    def __init__(self, **fields):
        self.flags = Row()
        self.__dict__.update(fields)

    def get(self, name, default=None):
        return getattr(self, name, default)

    def save(self):
        self.saved = True


def strict_selection(value, **kwargs):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not value:
        raise ValueError("selection must be nonempty")
    return value


def load_module(name, relative, modules):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class BillingContractTests(unittest.TestCase):
    def setUp(self):
        self.frappe = types.ModuleType("frappe")
        self.frappe._ = lambda value: value
        self.frappe.whitelist = lambda **kwargs: lambda function: function
        self.frappe.only_for = Mock()
        self.frappe.flags = Row()
        self.frappe.db = Mock()
        self.frappe.clear_last_message = Mock()
        self.frappe.as_json = json.dumps
        self.frappe.throw = Mock(side_effect=ValueError)
        self.frappe.utils = types.ModuleType("frappe.utils")
        for name in ("add_days", "formatdate", "getdate"):
            setattr(self.frappe.utils, name, Mock())
        self.frappe.utils.flt = lambda value, *args: float(value or 0)
        self.frappe.utils.cint = lambda value: int(value or 0)
        self.frappe.utils.now_datetime = Mock()
        self.frappe.utils.validate_email_address = lambda value: "@" in value
        self.frappe.utils.strip_html = lambda value: value
        integration = types.ModuleType("books_assistant.integration")
        integration.resolve_scope = Mock(
            return_value={"company": "Co-op", "currency": "USD"}
        )
        integration.check_document = Mock()
        integration.lock_documents = Mock()
        integration.selection = strict_selection
        integration.create_preview = Mock()
        integration.consume_preview = Mock()
        setup = types.ModuleType("books_assistant.setup")
        setup.BILLING_APPROVER = "Billing Approver"
        setup.HOURLY = "Hourly from timesheets"
        setup.RETAINER = "Retainer (invoiced separately)"
        emails = types.ModuleType("books_assistant.invoice_email")
        emails.recipients = Mock(return_value=([], []))
        emails.settings = Mock()
        document = types.ModuleType("frappe.model.document")
        document.Document = Document
        self.modules = {
            "frappe": self.frappe,
            "frappe.utils": self.frappe.utils,
            "frappe.model": types.ModuleType("frappe.model"),
            "frappe.model.document": document,
            "books_assistant.integration": integration,
            "books_assistant.invoice_email": emails,
            "books_assistant.setup": setup,
        }
        self.br = load_module(
            "isolated_billing_run", str(CONTROLLER.relative_to(ROOT)), self.modules
        )
        self.modules[
            "books_assistant.books_assistant.doctype.billing_run.billing_run"
        ] = self.br
        self.api = load_module(
            "isolated_billing_api", "books_assistant/billing_api.py", self.modules
        )

    def test_runtime_uses_machine_state_even_when_prose_disagrees(self):
        self.assertFalse(
            self.br.is_approved(
                Row(item_status="pending", outcome="Approved: old text")
            )
        )
        self.assertTrue(
            self.br.is_approved(Row(item_status="approved", outcome="Translated text"))
        )
        self.assertTrue(
            self.br.is_done(Row(item_status="left_out", outcome="Any display text"))
        )
        self.assertFalse(
            self.br.is_done(
                Row(item_status="review_required", outcome="Approved elsewhere")
            )
        )

    def test_legacy_migration_fails_closed_for_unknown_or_translated_outcomes(self):
        migration = load_module(
            "isolated_migration",
            "books_assistant/patches/structure_billing_status.py",
            self.modules,
        )
        self.assertEqual(migration.legacy_status("Approved: SI-1"), "approved")
        self.assertEqual(migration.legacy_status("Left out by admin"), "left_out")
        self.assertEqual(migration.legacy_status("Not approved: validation"), "failed")
        self.assertEqual(migration.legacy_status("Genehmigt: SI-1"), "review_required")
        self.assertEqual(migration.legacy_status(None), "pending")

    def test_old_approve_cannot_bypass_a_preview(self):
        run = self.br.BillingRun(name="BR-1")
        with self.assertRaises(ValueError):
            run.approve(["P-1"])

    def test_empty_and_unknown_project_selections_do_no_work(self):
        run = self.br.BillingRun(
            name="BR-1", status="Prepared", items=[Row(project="P-1")]
        )
        with patch.object(self.br, "approve_project") as submit:
            for selection in ([], "[]", None, ["UNKNOWN"]):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    run.approve_selected(selection)
            submit.assert_not_called()

    def test_partial_failure_preserves_success_and_returns_structured_errors(self):
        rows = [
            Row(project="P-1", item_status="pending"),
            Row(project="P-2", item_status="pending"),
        ]
        run = self.br.BillingRun(name="BR-1", status="Prepared", items=rows)
        success = {
            "project": "P-1",
            "status": "approved",
            "message": "Submitted",
            "sales_invoice": "SI-1",
        }
        with patch.object(
            self.br,
            "approve_project",
            side_effect=[success, ValueError("Invalid bill")],
        ):
            result = run.approve_selected(["P-1", "P-2"])
        self.assertEqual(rows[0].item_status, "approved")
        self.assertEqual(rows[1].item_status, "failed")
        self.assertEqual(rows[1].error_code, "PROJECT_APPROVAL_FAILED")
        self.assertEqual(result["projects"][0]["sales_invoice"], "SI-1")
        self.assertEqual(result["projects"][1]["error_code"], "PROJECT_APPROVAL_FAILED")
        self.frappe.db.rollback.assert_called_once_with(
            save_point="billing_run_project"
        )
        self.assertEqual(run.status, "Prepared")
        self.assertTrue(run.saved)

    def test_completed_or_unmigrated_projects_are_not_executed(self):
        for status in ("approved", "left_out", "review_required"):
            run = self.br.BillingRun(
                status="Prepared", items=[Row(project="P-1", item_status=status)]
            )
            with (
                patch.object(self.br, "approve_project") as submit,
                self.assertRaises(ValueError),
            ):
                run.approve_selected(["P-1"])
            submit.assert_not_called()

    def test_controller_locks_company_then_reloads_canonical_run(self):
        run = self.br.BillingRun(name="BR-1", items=[Row(project="FORGED")])
        run.is_new = lambda: False
        run.reload = Mock(
            side_effect=lambda: setattr(run, "items", [Row(project="STORED")])
        )
        run.check_permission = Mock()
        run.reload_for_action(self.br.APPROVERS)
        self.assertEqual(run.items[0].project, "STORED")
        self.assertTrue(run.flags.for_update)
        self.assertEqual(
            self.br.lock_documents.call_args_list[0].args[0], [("Company", "Co-op")]
        )
        self.assertEqual(
            self.br.lock_documents.call_args_list[1].args[0], [("Billing Run", "BR-1")]
        )
        run.check_permission.assert_called_once_with("write")

    def test_preview_rejects_legacy_state_before_reading_financial_data(self):
        run = Document(
            status="Prepared", items=[Row(project="P-1", item_status="review_required")]
        )
        with (
            patch.object(self.api, "_authorize", return_value=run),
            self.assertRaises(ValueError),
        ):
            self.api._snapshot(
                {"run_id": "BR-1", "projects": ["P-1"]},
                {"company": "Co-op", "currency": "USD"},
            )
        self.frappe.db.sql.assert_not_called()

    def test_preview_binding_is_checked_even_on_idempotent_replay(self):
        def replay(preview_id, request_id, action, snapshot, perform, *, authorize):
            authorize(
                {"run_id": "BR-OTHER", "projects": ["P-1"]},
                {"company": "Co-op", "currency": "USD"},
            )

        with (
            patch.object(self.api, "consume_preview", side_effect=replay),
            patch.object(self.api, "_authorize") as authorize,
            self.assertRaises(ValueError),
        ):
            self.api.execute_billing_approval(
                "PREVIEW", "REQUEST", expected_run="BR-1", expected_projects=["P-1"]
            )
        authorize.assert_not_called()

    def test_prepare_api_loads_stored_run_with_write_permission(self):
        run = Document(
            prepare=Mock(return_value={"run_id": "BR-1", "status": "Prepared"})
        )
        self.api.check_document.return_value = run
        result = self.api.prepare_billing("BR-1", company="Co-op")
        self.api.check_document.assert_called_once_with(
            "Billing Run", "BR-1", permission="write", company="Co-op"
        )
        self.assertEqual(result["api_version"], 1)
        run.prepare.assert_called_once()

    def test_locked_email_settings_use_current_database_values_and_typed_switches(self):
        email = load_module(
            "isolated_invoice_email", "books_assistant/invoice_email.py", self.modules
        )
        self.frappe.flags.books_assistant_billing_locked = True
        self.frappe.db.sql.return_value = [
            ("send_invoice_emails", "0"),
            ("hold_engineer_bills", "1"),
        ]
        self.frappe.get_doc = lambda values: Document(**values)
        settings = email.settings()
        self.assertFalse(settings.send_invoice_emails)
        self.assertTrue(settings.hold_engineer_bills)
        self.assertIn("for update", self.frappe.db.sql.call_args.args[0])

    def test_locked_contact_fallback_checks_permission_before_disclosing_email(self):
        email = load_module(
            "isolated_invoice_email", "books_assistant/invoice_email.py", self.modules
        )
        self.frappe.flags.books_assistant_billing_locked = True
        self.frappe.db.sql.return_value = [("SECRET-CONTACT", "secret@example.test")]
        email.check_document.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            email._contact_emails("Customer")
        email.check_document.assert_called_once_with("Contact", "SECRET-CONTACT")

    def test_period_amounts_come_from_a_current_locking_read(self):
        current = [Row(name="TD-1", time_sheet="TS-1", billing_amount=200)]
        self.frappe.db.sql.return_value = current
        run = Document(
            company="Co-op", period_start="2026-10-01", period_end="2026-10-31"
        )
        self.assertEqual(self.br.period_timesheets(run, "P-1")[0].billing_amount, 200)
        self.assertTrue(
            self.frappe.db.sql.call_args.args[0].rstrip().endswith("for update")
        )
        self.br.check_document.assert_called_once_with(
            "Timesheet", "TS-1", company="Co-op"
        )

    def test_period_read_rejects_inaccessible_timesheets_before_returning_amounts(self):
        self.frappe.db.sql.return_value = [
            Row(name="TD-SECRET", time_sheet="TS-SECRET", billing_amount=200)
        ]
        self.br.check_document.side_effect = PermissionError
        run = Document(
            company="Co-op", period_start="2026-10-01", period_end="2026-10-31"
        )
        with self.assertRaises(PermissionError):
            self.br.period_timesheets(run, "P-1")

    def test_existing_bill_links_come_from_current_timesheets_not_a_second_plain_read(
        self,
    ):
        rows = [
            Row(time_sheet="TS-1", purchase_invoice="PI-CURRENT", costing_amount=175)
        ]
        run = Document(company="Co-op", currency="USD")
        with patch.object(self.br, "period_timesheets", return_value=rows):
            amount, names = self.br.already_billed(run, "P-1")
        self.assertEqual((amount, names), (175, ["PI-CURRENT"]))
        self.br.check_document.assert_called_once_with(
            "Purchase Invoice", "PI-CURRENT", company="Co-op", currency="USD"
        )

    def test_draft_invoice_diagnostics_do_not_disclose_inaccessible_record_names(self):
        self.frappe.db.sql.return_value = [("TD-1", "SECRET-DRAFT")]
        self.br.check_document.side_effect = PermissionError
        run = Document(company="Co-op", currency="USD")
        with (
            patch.object(self.br, "period_timesheets", return_value=[Row(name="TD-1")]),
            self.assertRaises(PermissionError),
        ):
            self.br.sales_lines(run, "P-1")

    def snapshot_project(self, row, records, referral_names=()):
        run = Document(
            name="BR-1",
            status="Prepared",
            items=[row],
            modified="v1",
            period_start="2026-10-01",
            period_end="2026-10-31",
            invoice_date="2026-11-01",
        )
        self.frappe.conf = Row()
        self.frappe.get_roles = Mock(return_value=[])
        self.frappe.get_all = Mock(return_value=[])
        self.frappe.db.sql.return_value = [(name,) for name in referral_names]
        self.api.check_document.side_effect = lambda doctype, name, **kwargs: records[
            (doctype, name)
        ]
        self.api.invoice_email.settings.return_value = Document(send_invoice_emails=0)
        self.api.invoice_email.print_format = Mock(return_value=None)
        with (
            patch.object(self.api, "_authorize", return_value=run),
            patch.object(self.api, "invoiced_by", return_value=None),
            patch.object(self.api, "_snapshot_document", return_value={}),
        ):
            snapshot = self.api._snapshot(
                {"run_id": run.name, "projects": [row.project]},
                {"company": "Co-op", "currency": "USD"},
            )
        return snapshot["projects"][0]

    def test_preview_recalculates_profitability_after_prepared_drafts_are_edited(self):
        row = Row(
            project="P-1",
            customer="C-1",
            item_status="pending",
            sales_invoice="SI-1",
            engineer_bills="PI-1",
            client_amount=1000,
            engineer_cost=600,
            referral_cost=50,
            margin_pct=35,
            issues="Preparation warning",
        )
        invoice = Document(grand_total=1000, docstatus=0)
        bill = Document(grand_total=600)
        project = Document(referrers=[Row(supplier="REF-1", percentage=5)])
        records = {
            ("Sales Invoice", "SI-1"): invoice,
            ("Purchase Invoice", "PI-1"): bill,
            ("Project", "P-1"): project,
        }
        before = self.snapshot_project(row, records)
        self.assertEqual(
            (before["engineer_cost"], before["referral_cost"], before["margin_pct"]),
            (600, 50, 35),
        )
        # The run's preparation summaries stay unchanged; the new preview must use edited source documents.
        invoice.grand_total, bill.grand_total, project.referrers[0].percentage = (
            2000,
            1800,
            10,
        )
        after = self.snapshot_project(row, records)
        self.assertEqual(
            (
                after["client_amount"],
                after["engineer_cost"],
                after["referral_cost"],
                after["margin_pct"],
            ),
            (2000, 1800, 200, 0),
        )
        self.assertTrue(after["current_margin_warning"])
        self.assertEqual(after["issues_scope"], "billing_preparation")

    def test_preview_margin_excludes_preparation_time_earlier_invoices_and_bills(self):
        row = Row(
            project="P-1",
            customer="C-1",
            item_status="pending",
            sales_invoice="SI-1",
            engineer_bills="PI-1\nPI-1",
            client_amount=4000,
            engineer_cost=3000,
            referral_cost=50,
            margin_pct=23.75,
        )
        records = {
            ("Sales Invoice", "SI-1"): Document(grand_total=1000, docstatus=0),
            ("Purchase Invoice", "PI-1"): Document(grand_total=600),
            ("Project", "P-1"): Document(referrers=[]),
        }
        snapshot = self.snapshot_project(row, records)
        self.assertEqual(snapshot["profitability_scope"], "selected_approval_documents")
        self.assertEqual(
            (
                snapshot["client_amount"],
                snapshot["engineer_cost"],
                snapshot["margin_pct"],
            ),
            (1000, 600, 40),
        )
        self.assertEqual(snapshot["engineer_bills"], ["PI-1"])

    def test_submitted_invoice_uses_actual_active_referral_bills_once(self):
        row = Row(
            project="P-1",
            customer="C-1",
            item_status="pending",
            sales_invoice="SI-1",
            engineer_bills="PI-1",
            referral_cost=9999,
            margin_pct=-999,
        )
        records = {
            ("Sales Invoice", "SI-1"): Document(grand_total=1000, docstatus=1),
            ("Purchase Invoice", "PI-1"): Document(grand_total=600),
            ("Purchase Invoice", "REF-PI-1"): Document(
                grand_total=80, supplier="REF-1"
            ),
            ("Project", "P-1"): Document(
                referrers=[Row(supplier="REF-1", percentage=50)]
            ),
        }
        snapshot = self.snapshot_project(row, records, ["REF-PI-1", "REF-PI-1"])
        self.assertEqual((snapshot["referral_cost"], snapshot["margin_pct"]), (80, 32))
        self.assertEqual(snapshot["referral_bills"], ["REF-PI-1"])
        self.assertIn("docstatus != 2", self.frappe.db.sql.call_args.args[0])

    def test_draft_referrals_do_not_double_count_existing_fees_or_duplicate_suppliers(
        self,
    ):
        row = Row(
            project="P-1",
            customer="C-1",
            item_status="pending",
            sales_invoice="SI-1",
            engineer_bills="PI-1",
        )
        records = {
            ("Sales Invoice", "SI-1"): Document(grand_total=1000, docstatus=0),
            ("Purchase Invoice", "PI-1"): Document(grand_total=600),
            ("Purchase Invoice", "REF-PI-1"): Document(
                grand_total=80, supplier="REF-1"
            ),
            ("Project", "P-1"): Document(
                referrers=[
                    Row(supplier="REF-1", percentage=50),
                    Row(supplier="REF-2", percentage=5),
                    Row(supplier="REF-2", percentage=20),
                ]
            ),
        }
        snapshot = self.snapshot_project(row, records, ["REF-PI-1"])
        self.assertEqual((snapshot["referral_cost"], snapshot["margin_pct"]), (130, 27))

    def test_edited_invoice_linkage_requires_a_new_preparation(self):
        scope = {"company": "Co-op", "currency": "USD"}
        for doctype, project, customer in (
            ("Sales Invoice", "OTHER", "C-1"),
            ("Sales Invoice", "P-1", "OTHER"),
            ("Purchase Invoice", "OTHER", None),
        ):
            with self.subTest(doctype=doctype, project=project, customer=customer):
                row = Row(
                    project="P-1",
                    customer="C-1",
                    sales_invoice="SI-1" if doctype == "Sales Invoice" else None,
                    engineer_bills="PI-1" if doctype == "Purchase Invoice" else None,
                )
                run = Document(items=[row])
                invoice = Document(
                    project=project, customer=customer, check_permission=Mock()
                )
                self.api.check_document.side_effect = lambda dt, name, **kwargs: (
                    run if dt == "Billing Run" else invoice
                )
                with self.assertRaises(ValueError):
                    self.api._authorize({"run_id": "BR-1", "projects": ["P-1"]}, scope)
                invoice.check_permission.assert_not_called()


if __name__ == "__main__":
    unittest.main()
