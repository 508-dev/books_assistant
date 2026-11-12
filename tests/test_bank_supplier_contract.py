"""Financial entry point contracts without a live Frappe site.

These exercise the real boundary functions with controlled provider responses.
Ledger writes and row-lock behavior still require bench integration tests.
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"company": "Co-op", "currency": "USD"}


class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BankSupplierContractTests(unittest.TestCase):
    def setUp(self):
        frappe = types.ModuleType("frappe")
        frappe._ = lambda value: value
        frappe.parse_json = lambda value: (
            json.loads(value) if isinstance(value, str) else value
        )
        frappe._dict = Row
        frappe.only_for = Mock()
        frappe.clear_last_message = Mock()
        frappe.db = Mock()
        frappe.db.sql.return_value = []
        frappe.has_permission = Mock(return_value=True)
        frappe.get_list = Mock(return_value=[])
        frappe.conf = Row()
        frappe.local = Row(response={})
        frappe.session = Row(user="approver@example.test")
        frappe.ValidationError = ValueError
        frappe.PermissionError = PermissionError
        frappe.throw = Mock(
            side_effect=lambda message, *args: (_ for _ in ()).throw(
                ValueError(message)
            )
        )

        def whitelist(**kwargs):
            def decorate(function):
                function.allowed_methods = kwargs.get("methods")
                return function

            return decorate

        frappe.whitelist = whitelist
        utils = types.ModuleType("frappe.utils")
        utils.flt = lambda value, precision=None: (
            round(float(value or 0), precision)
            if precision is not None
            else float(value or 0)
        )
        for name in (
            "add_days",
            "add_months",
            "date_diff",
            "get_datetime",
            "now_datetime",
        ):
            setattr(utils, name, Mock(return_value=datetime(2026, 11, 13)))
        utils.today = utils.nowdate = lambda: "2026-11-13"
        utils.strip_html = utils.cstr = lambda value: str(value)
        frappe.utils = utils
        package = types.ModuleType("books_assistant")
        package.__path__ = [str(ROOT / "books_assistant")]
        self.modules = {
            "frappe": frappe,
            "frappe.utils": utils,
            "books_assistant": package,
        }
        self.environment = patch.dict(sys.modules, self.modules)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        integration = load("test_real_integration", "books_assistant/integration.py")
        sys.modules["books_assistant.integration"] = integration
        self.br = load("books_assistant.bank_review", "books_assistant/bank_review.py")
        package.bank_review = self.br
        sys.modules["books_assistant.bank_review"] = self.br
        self.bank = load("books_assistant.bank_api", "books_assistant/bank_api.py")
        self.pay = load(
            "books_assistant.pay_supplier", "books_assistant/pay_supplier.py"
        )
        self.frappe = frappe

    def test_mutations_are_post_only_and_reads_get_only(self):
        for function in (
            self.br.approve,
            self.br.preview_approval,
            self.br.create_rule,
            self.br.learn_from_history,
            self.pay.prepare,
            self.pay.preview_preparation,
        ):
            self.assertEqual(function.allowed_methods, ["POST"])
        for function in (
            self.br.get_queue,
            self.pay.preview,
            self.pay.payment_warnings,
        ):
            self.assertEqual(function.allowed_methods, ["GET"])

    def test_supplier_selection_never_expands_empty_or_omitted_inputs(self):
        for selection in (None, [], "[]", "null", {}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.pay._selected(selection)
        self.assertIsNone(self.pay._selected(None, all_eligible=True))
        self.assertIsNone(self.pay._selected("null", all_eligible="true"))
        for selection in ([], ["BILL-1"]):
            with self.assertRaises(ValueError):
                self.pay._selected(selection, all_eligible=True)
        self.assertEqual(self.pay._selected('["BILL-1"]'), ["BILL-1"])

    def test_old_supplier_prepare_cannot_bypass_confirmation(self):
        with patch.object(self.pay, "consume_preview") as consume:
            with self.assertRaises(ValueError):
                self.pay.prepare()
            consume.assert_not_called()

    def test_supplier_preview_is_pure_and_rejects_ineligible_selected_bills(self):
        bill = Row(name="BILL-1", outstanding_amount=100)
        with (
            patch.object(self.pay, "resolve_scope", return_value=SCOPE),
            patch.object(self.pay, "_authorize"),
            patch.object(self.pay, "check_document", return_value=Row(modified="v1")),
            patch.object(self.pay, "open_bills", return_value=[bill]),
            patch.object(self.pay, "credit_entries", return_value=[Row(amount=30)]),
            patch.object(self.pay, "usual_bank_account", return_value="BANK"),
            patch.object(self.pay, "_check_bank"),
            patch.object(self.pay, "held_bills", return_value=[]),
            patch.object(self.pay, "fronting_warnings", return_value=[]),
            patch.object(self.pay, "apply_credit") as apply,
            patch.object(self.pay, "create_preview") as create,
        ):
            result = self.pay.preview("Supplier", ["BILL-1"])
            self.assertEqual((result["credit_to_apply"], result["to_pay"]), (30, 70))
            self.assertFalse(result["side_effects"]["bank_transfer"])
            with self.assertRaises(ValueError):
                self.pay.preview("Supplier", ["BILL-1", "MISSING"])
            apply.assert_not_called()
            create.assert_not_called()

    def test_supplier_empty_internal_selection_returns_no_bills(self):
        with patch.object(self.pay, "check_document"):
            self.assertEqual(self.pay.open_bills("Supplier", [], SCOPE), [])
        self.frappe.get_list.assert_not_called()

    def test_supplier_snapshot_locks_credit_and_bills_before_rereading(self):
        payload = {
            "supplier": "Supplier",
            "invoices": ["BILL-1"],
            "bank_account": "BANK",
        }
        view = {
            "bills": [Row(name="BILL-1")],
            "credits": [Row(reference_type="Payment Entry", reference_name="CREDIT-1")],
            "bank_details": {"bank_account": "BANK-ACCOUNT"},
        }
        events = []
        with (
            patch.object(self.pay, "_authorize_preparation"),
            patch.object(
                self.pay,
                "_preview",
                side_effect=lambda *args: events.append("read") or view,
            ),
            patch.object(
                self.pay, "lock_documents", side_effect=lambda docs: events.append(docs)
            ),
        ):
            self.pay._preparation_snapshot(payload, SCOPE)
        self.assertEqual(events[0], "read")
        self.assertIn(("Purchase Invoice", "BILL-1"), events[1])
        self.assertIn(("Payment Entry", "CREDIT-1"), events[1])
        self.assertEqual(events[2], "read")

    def test_supplier_preparation_applies_reviewed_credit_without_saving_or_sending(
        self,
    ):
        payload = {
            "supplier": "Supplier",
            "invoices": ["BILL-1"],
            "bank_account": "BANK",
        }
        payment = Mock()
        payment.as_dict.return_value = {"doctype": "Payment Entry", "__islocal": 1}
        with (
            patch.object(self.pay, "apply_credit", return_value=30) as apply,
            patch.object(self.pay, "open_bills", return_value=[Row(name="BILL-1")]),
            patch.object(self.pay, "new_payment", return_value=payment),
        ):
            result = self.pay._prepare(payload, SCOPE)
        apply.assert_called_once_with("Supplier", ["BILL-1"], SCOPE)
        payment.insert.assert_not_called()
        payment.submit.assert_not_called()
        self.assertEqual(result["credit_applied"], 30)
        self.assertFalse(result["payment_saved"])
        self.assertFalse(result["bank_transfer"])

    def test_bank_empty_selection_and_missing_fingerprints_create_nothing(self):
        with patch.object(self.bank, "create_preview") as create:
            for names, expected in (([], {}), (["BT-1"], {}), (["BT-1"], [])):
                with (
                    self.subTest(names=names, expected=expected),
                    self.assertRaises(ValueError),
                ):
                    self.bank.preview_approval(names, expected)
            create.assert_not_called()

    def test_bank_preview_rejects_changed_action_and_preserves_selected_filter(self):
        snapshot = {"rows": [{"bank_transaction": "BT-1", "fingerprint": "current"}]}
        with (
            patch.object(self.bank, "resolve_scope", return_value=SCOPE),
            patch.object(self.bank, "lock_documents"),
            patch.object(self.bank, "_bank_snapshot", return_value=snapshot) as build,
            patch.object(self.bank, "create_preview") as create,
        ):
            with self.assertRaises(ValueError):
                self.bank.preview_approval(
                    ["BT-1"], {"BT-1": "old"}, bank_account="BANK-A"
                )
            self.assertEqual(build.call_args.args[0]["bank_account"], "BANK-A")
            create.assert_not_called()

    def test_bank_freezes_current_action_and_uses_common_authorized_execution(self):
        row = {"bank_transaction": "BT-1", "fingerprint": "current"}
        with (
            patch.object(self.bank, "resolve_scope", return_value=SCOPE),
            patch.object(self.bank, "lock_documents"),
            patch.object(self.bank, "_bank_snapshot", return_value={"rows": [row]}),
            patch.object(
                self.bank, "_dependencies", return_value=[("Bank Transaction", "BT-1")]
            ),
            patch.object(
                self.bank, "create_preview", return_value={"preview_id": "PREVIEW"}
            ) as create,
        ):
            result = self.bank.preview_approval(["BT-1"], {"BT-1": "current"})
            self.assertEqual(result["preview_id"], "PREVIEW")
            self.assertEqual(create.call_args.args[0], "bank.approve")
            self.assertEqual(
                create.call_args.args[2]["documents"], [("Bank Transaction", "BT-1")]
            )
        with patch.object(
            self.bank, "consume_preview", return_value={"status": "completed"}
        ) as consume:
            self.bank.approve("PREVIEW", "REQUEST")
            self.assertIs(consume.call_args.kwargs["authorize"], self.bank._authorize)

    def test_bank_partial_failure_uses_savepoints_and_never_commits(self):
        rows = [{"bank_transaction": "BT-1"}, {"bank_transaction": "BT-2"}]
        with (
            patch.object(self.bank, "_queue", return_value=rows),
            patch.object(
                self.br, "bank_line", return_value=Row(unallocated_amount=100)
            ),
            patch.object(
                self.br,
                "execute",
                side_effect=["Created PE-1", ValueError("Invalid bank")],
            ),
        ):
            result = self.bank._approve({"bank_transactions": ["BT-1", "BT-2"]}, SCOPE)
        self.assertEqual(
            [r["status"] for r in result["results"]], ["approved", "failed"]
        )
        self.assertEqual(result["results"][1]["error_code"], "BANK_ACTION_FAILED")
        self.frappe.db.rollback.assert_called_once_with(save_point="bank_approval_item")
        self.frappe.db.commit.assert_not_called()

    def test_bank_replay_authorization_rechecks_all_original_documents(self):
        payload = {
            "bank_transactions": ["BT-1"],
            "documents": [("Purchase Invoice", "BILL-1")],
        }
        with (
            patch.object(self.bank, "resolve_scope", return_value=SCOPE),
            patch.object(self.bank, "_check_line") as line,
            patch.object(self.bank, "check_document", side_effect=PermissionError),
        ):
            with self.assertRaises(PermissionError):
                self.bank._authorize(payload, SCOPE)
        line.assert_called_once_with("BT-1", SCOPE, "write")

    def test_inaccessible_existing_match_blocks_new_payment_suggestion(self):
        context = self.br.Context.__new__(self.br.Context)
        context.bank_gl = {"BANK": "BANK-GL"}
        context.claimed = set()
        transaction = Row(
            name="BT-1", bank_account="BANK", deposit=100, date="2026-11-13"
        )
        self.frappe.db.sql.side_effect = [
            [Row(doctype="Payment Entry", name="HIDDEN")],
            [],
        ]
        self.frappe.has_permission.return_value = False
        result = context._match_existing(transaction, 100)
        self.assertEqual(result["kind"], self.br.NONE)
        self.assertNotIn("HIDDEN", result["summary"])

    def test_supplier_current_eligibility_overrides_stale_query_row(self):
        self.frappe.get_list.return_value = [Row(name="BILL-1", outstanding_amount=100)]
        current = Row(
            name="BILL-1",
            supplier="Supplier",
            docstatus=1,
            on_hold=1,
            is_return=0,
            outstanding_amount=100,
        )
        with patch.object(self.pay, "check_document", return_value=current):
            self.assertEqual(self.pay.open_bills("Supplier", ["BILL-1"], SCOPE), [])

    def test_credit_snapshot_uses_current_ledger_reads_for_payment_settlements(self):
        self.frappe.db.sql.return_value = [Row(name="LEDGER-1", amount=30, delinked=0)]
        result = self.pay._credit_ledger(
            [Row(reference_type="Payment Entry", reference_name="CREDIT-1")]
        )
        self.assertEqual(result[0]["amount"], 30)
        self.assertIn("for update", self.frappe.db.sql.call_args.args[0])
        self.assertEqual(
            self.frappe.db.sql.call_args.args[1],
            ("Payment Entry", "CREDIT-1", "Payment Entry", "CREDIT-1"),
        )

    def test_bank_current_reconciliation_state_overrides_stale_queue_query(self):
        stale = Row(name="BT-1", unallocated_amount=100, bank_account="BANK")
        current = Row(
            name="BT-1", unallocated_amount=0, bank_account="BANK", docstatus=1
        )
        with (
            patch.object(self.br, "Context") as context,
            patch.object(self.br, "unreconciled_lines", return_value=[stale]),
            patch.object(self.bank, "_check_line", return_value=current),
        ):
            self.assertEqual(self.bank._queue(SCOPE), [])
            context.return_value.suggest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
