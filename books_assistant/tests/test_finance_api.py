"""Boundary tests runnable without an installed Frappe site."""

import importlib.util
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class Row(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class TestFinanceAPI(unittest.TestCase):
    def setUp(self):
        self.frappe = ModuleType("frappe")
        self.frappe.whitelist = lambda **kw: lambda fn: fn
        self.frappe.only_for = Mock()
        self.frappe.throw = Mock(side_effect=ValueError)
        self.frappe._ = lambda text: text
        self.frappe.get_doc = Mock()
        self.frappe.session = SimpleNamespace(user="reader@example.test")
        self.frappe.has_permission = Mock(return_value=True)
        self.frappe.get_meta = Mock()
        self.fields = {
            "name",
            "date",
            "bank_account",
            "currency",
            "unallocated_amount",
            "customer",
            "due_date",
            "outstanding_amount",
            "supplier",
            "client_invoice",
            "period_start",
            "period_end",
            "status",
            "reference_name",
            "delivery_status",
            "creation",
            "posting_date",
            "on_hold",
            "docstatus",
        }
        self.frappe.get_meta.return_value.get_permitted_fieldnames.side_effect = (
            lambda **kwargs: self.fields
        )
        self.records = {}
        self.queries = []

        def get_list(doctype, **kwargs):
            self.queries.append((doctype, kwargs))
            return self.records.get(doctype, [])

        self.frappe.get_list = get_list
        utils = ModuleType("frappe.utils")
        utils.flt = lambda n, precision=None: (
            round(float(n or 0), precision) if precision is not None else float(n or 0)
        )
        utils.today = lambda: "2026-11-13"
        utils.add_days = lambda day, delta: "2026-10-14"
        utils.now_datetime = lambda: datetime(2026, 11, 13)
        integration = ModuleType("books_assistant.integration")
        integration.resolve_scope = Mock(
            return_value={"company": "Company A", "currency": "USD"}
        )
        model = ModuleType("frappe.model")
        model.default_fields = [
            "name",
            "creation",
            "modified",
            "modified_by",
            "owner",
            "docstatus",
        ]
        spec = importlib.util.spec_from_file_location(
            "finance_api_test_subject", Path(__file__).parents[1] / "api.py"
        )
        self.api = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "frappe": self.frappe,
                "frappe.utils": utils,
                "frappe.model": model,
                "books_assistant.integration": integration,
            },
        ):
            spec.loader.exec_module(self.api)

    def test_company_and_role_are_checked_before_any_queries(self):
        self.frappe.only_for.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.api.get_finance_status("Company A")
        self.assertEqual(self.queries, [])
        self.frappe.only_for.side_effect = None
        self.frappe.get_doc.return_value.check_permission.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.api.get_finance_status("Company A")
        self.assertEqual(self.queries, [])

    def test_requires_explicit_company(self):
        with self.assertRaises(ValueError):
            self.api.get_finance_status("")
        self.assertEqual(self.queries, [])

    def test_status_scopes_queries_and_keeps_currencies_separate(self):
        self.records["Bank Account"] = [Row(name="Bank A")]
        self.records["Sales Invoice"] = [
            Row(name="INV-A", currency="USD", outstanding_amount=100),
            Row(name="INV-B", currency="EUR", outstanding_amount=20),
        ]
        result = self.api.get_finance_status("Company A")
        self.assertEqual(
            result["overdue_totals"],
            [{"currency": "EUR", "amount": 20}, {"currency": "USD", "amount": 100}],
        )
        for doctype, args in self.queries:
            if doctype not in ("Bank Transaction", "Communication"):
                self.assertEqual(args["filters"]["company"], "Company A")
        bank_filters = next(
            args["filters"]
            for doctype, args in self.queries
            if doctype == "Bank Transaction"
        )
        self.assertEqual(bank_filters["bank_account"], ["in", ["Bank A"]])
        self.assertEqual(result["scope"], "records_visible_to_current_user")

    def test_no_banks_does_not_issue_unscoped_bank_query(self):
        self.api.get_finance_status("Company A")
        self.assertFalse(any(dt == "Bank Transaction" for dt, _ in self.queries))

    def test_supplier_explains_held_and_eligible_bills_without_side_effects(self):
        self.records["Purchase Invoice"] = [
            Row(
                name="BILL-1",
                currency="USD",
                outstanding_amount=50,
                on_hold=1,
                client_invoice="CLIENT-1",
            ),
            Row(
                name="BILL-2",
                currency="EUR",
                outstanding_amount=20,
                on_hold=0,
                client_invoice=None,
            ),
        ]
        self.records["Sales Invoice"] = [
            Row(name="CLIENT-1", docstatus=1, currency="USD", outstanding_amount=100)
        ]
        result = self.api.get_supplier_payment_status("Supplier A", "Company A")
        self.assertEqual(
            result["bills"][0]["reason_code"], "waiting_for_client_payment"
        )
        self.assertEqual(result["bills"][0]["blocking_invoice"]["name"], "CLIENT-1")
        self.assertEqual(result["eligible_totals"], [{"currency": "EUR", "amount": 20}])
        self.assertEqual(result["held_totals"], [{"currency": "USD", "amount": 50}])
        filters = self.queries[0][1]["filters"]
        self.assertEqual(
            (filters["company"], filters["supplier"]), ("Company A", "Supplier A")
        )
        self.frappe.get_doc.assert_any_call("Supplier", "Supplier A")
        self.assertEqual(result["payment_execution"], "not_started")

    def test_inaccessible_client_invoice_is_not_disclosed(self):
        self.records["Purchase Invoice"] = [
            Row(
                name="BILL-1",
                currency="USD",
                outstanding_amount=50,
                on_hold=1,
                client_invoice="SECRET",
            )
        ]
        result = self.api.get_supplier_payment_status("Supplier A", "Company A")
        self.assertIsNone(result["bills"][0]["client_invoice"])
        self.assertIsNone(result["bills"][0]["blocking_invoice"])
        self.assertEqual(result["bills"][0]["reason_code"], "hold_requires_review")
        result = self.api.get_finance_status("Company A")
        self.assertIsNone(result["held_bills"][0]["client_invoice"])

    def test_paid_invoice_with_stale_hold_requires_review(self):
        self.records["Purchase Invoice"] = [
            Row(
                name="BILL-1",
                currency="USD",
                outstanding_amount=50,
                on_hold=1,
                client_invoice="PAID",
            )
        ]
        self.records["Sales Invoice"] = [
            Row(name="PAID", docstatus=1, currency="USD", outstanding_amount=0)
        ]
        result = self.api.get_supplier_payment_status("Supplier A", "Company A")
        self.assertEqual(result["bills"][0]["reason_code"], "hold_requires_review")

    def test_unheld_bill_does_not_disclose_inaccessible_client_invoice(self):
        self.records["Purchase Invoice"] = [
            Row(
                name="BILL-1",
                currency="USD",
                outstanding_amount=50,
                on_hold=0,
                client_invoice="SECRET",
            )
        ]
        result = self.api.get_supplier_payment_status("Supplier A", "Company A")
        self.assertIsNone(result["bills"][0]["client_invoice"])
        self.assertIsNone(result["bills"][0]["blocking_invoice"])
        self.assertEqual(result["bills"][0]["reason_code"], "not_on_hold")

    def test_unreadable_bank_source_does_not_hide_other_sections(self):
        self.records["Billing Run"] = [Row(name="RUN-1", status="Prepared")]
        self.frappe.has_permission.side_effect = lambda doctype, *args, **kwargs: (
            doctype != "Bank Account"
        )
        result = self.api.get_finance_status("Company A")
        self.assertEqual(result["pending_billing_runs"][0]["name"], "RUN-1")
        self.assertEqual(result["unavailable_sources"], ["Bank Account"])
        self.assertFalse(
            any(
                doctype in ("Bank Account", "Bank Transaction")
                for doctype, _ in self.queries
            )
        )

    def test_custom_record_permission_hook_excludes_hidden_client(self):
        self.records["Purchase Invoice"] = [
            Row(
                name="BILL-1",
                currency="USD",
                outstanding_amount=50,
                on_hold=0,
                client_invoice="SECRET",
            )
        ]
        self.records["Sales Invoice"] = [
            Row(name="SECRET", currency="USD", docstatus=1, outstanding_amount=50)
        ]
        self.frappe.has_permission.side_effect = lambda doctype, *args, **kwargs: (
            kwargs.get("doc") != "SECRET"
        )
        result = self.api.get_supplier_payment_status("Supplier A", "Company A")
        self.assertIsNone(result["bills"][0]["client_invoice"])

    def test_hidden_amount_field_withholds_invoice_section(self):
        self.fields.remove("outstanding_amount")
        result = self.api.get_finance_status("Company A")
        self.assertEqual(result["overdue_invoices"], [])
        self.assertIn("Sales Invoice", result["unavailable_sources"])
        self.assertFalse(
            any(
                doctype == "Sales Invoice" and "outstanding_amount" in kwargs["fields"]
                for doctype, kwargs in self.queries
            )
        )


if __name__ == "__main__":
    unittest.main()
