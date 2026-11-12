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
        frappe.DoesNotExistError = LookupError
        frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
        frappe.QueryTimeoutError = type("QueryTimeoutError", (Exception,), {})
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
            "to_pay": 100,
        }
        events = []
        with (
            patch.object(self.pay, "_authorize_preparation"),
            patch.object(self.pay, "assess_funding", return_value={"cash": {"status": "unverified"}}),
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

    def test_supplier_snapshot_freezes_funding_evidence_and_reservations(self):
        payload = {"supplier": "Supplier", "invoices": ["BILL-1"], "bank_account": "BANK"}
        def payable(*args):
            return {"bills": [Row(name="BILL-1")], "credits": [], "to_pay": 100,
                    "bank_details": {"bank_account": "BANK-ACCOUNT"}}
        with (
            patch.object(self.pay, "_authorize_preparation"),
            patch.object(self.pay, "_preview", side_effect=payable),
            patch.object(self.pay, "lock_documents"),
            patch.object(self.pay, "assess_funding", side_effect=[
                {"cash": {"snapshot": "BALANCE-1", "reserved": 0}},
                {"cash": {"snapshot": "BALANCE-2", "reserved": 500}},
            ]) as funding,
        ):
            first = self.pay._preparation_snapshot(payload, SCOPE)
            second = self.pay._preparation_snapshot(payload, SCOPE)
        funding.assert_called_with("BANK-ACCOUNT", 100, ["BILL-1"], "Supplier")
        # consume_preview hashes the complete returned snapshot before applying credit.
        from books_assistant.integration import fingerprint
        self.assertNotEqual(fingerprint(first), fingerprint(second))
        self.assertEqual(first["funding"]["cash"]["reserved"], 0)
        self.assertEqual(second["funding"]["cash"]["reserved"], 500)

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
            context.return_value.bank_gl = {"BANK": "BANK-GL"}
            self.assertEqual(self.bank._queue(SCOPE), [])
            context.return_value.suggest.assert_not_called()

    def test_hidden_named_invoice_blocks_only_its_line_without_fallthrough(self):
        context = self.br.Context.__new__(self.br.Context)
        context.bank_gl = {"BANK": "BANK-GL"}
        hidden = Row(name="BT-1", bank_account="BANK", deposit=100, withdrawal=0,
                     date="2026-11-13", unallocated_amount=100, description="ACC-SINV-2026-12345")
        self.frappe.db.get_value = Mock(return_value=Row(name="ACC-SINV-2026-12345", docstatus=1))
        self.frappe.has_permission.return_value = False
        with patch.object(context, "_match_existing", return_value=None), \
                patch.object(context, "_booked_elsewhere", return_value=None), \
                patch.object(context, "_transfer") as transfer, \
                patch.object(context, "_rule") as rule, \
                patch.object(context, "_by_party") as party:
            blocked = context.suggest(hidden)
        self.assertEqual(blocked["kind"], self.br.NONE)
        self.assertNotIn("12345", blocked["summary"])
        self.assertNotIn("invoices", blocked)
        transfer.assert_not_called()
        rule.assert_not_called()
        party.assert_not_called()
        with patch.object(context, "_match_existing", return_value={"kind": "match", "confidence": "high"}):
            self.assertEqual(context.suggest(hidden)["kind"], "match")

    def test_hidden_rule_account_blocks_only_matching_line_without_fallthrough(self):
        context = self.br.Context.__new__(self.br.Context)
        context.scope, context.bank_gl = SCOPE, {"BANK": "BANK-GL"}
        context.rules = [types.SimpleNamespace(name="HIDDEN-RULE", account="SECRET-ACCOUNT", times_used=3,
                                               matches=lambda bt, bank: bt.description == "MATCH-RULE",
                                               specificity=lambda bt: 10)]
        line = Row(name="BT-1", bank_account="BANK", deposit=0, withdrawal=100,
                   date="2026-11-13", unallocated_amount=100, description="MATCH-RULE")
        def permitted(doctype, name, **kwargs):
            if doctype == "Account":
                raise PermissionError
            return Row()
        with patch.object(context, "_match_existing", return_value=None), \
                patch.object(context, "_booked_elsewhere", return_value=None), \
                patch.object(context, "_by_invoice_number", return_value=None), \
                patch.object(context, "_transfer", return_value=None), \
                patch.object(self.br, "check_document", side_effect=permitted), \
                patch.object(context, "_by_party", return_value={"kind": "payout", "confidence": "check"}) as party:
            blocked = context.suggest(line)
            self.assertEqual(blocked["kind"], self.br.NONE)
            party.assert_not_called()
            line.description = "UNRELATED"
            self.assertEqual(context.suggest(line)["kind"], "payout")
        self.assertNotIn("SECRET", blocked["summary"])
        self.assertNotIn("rule", blocked)

    def test_hidden_company_bank_still_blocks_duplicate_payment(self):
        context = self.br.Context.__new__(self.br.Context)
        context.scope, context.locking = SCOPE, True
        context.bank_gl = {"BANK-A": "BANK-A-GL"}
        context.company_bank_gl = {"BANK-A": "BANK-A-GL", "HIDDEN-BANK": "SECRET-GL"}
        self.frappe.db.sql.return_value = [Row(name="SECRET-PE", account="SECRET-GL", party="SECRET-PARTY")]
        line = Row(name="BT-1", bank_account="BANK-A", deposit=0, withdrawal=100,
                   date="2026-11-13", unallocated_amount=100, description="SUPPLIER")
        with patch.object(context, "_match_existing", return_value=None), \
                patch.object(context, "_by_invoice_number") as invoice:
            blocked = context.suggest(line)
        self.assertEqual(blocked["kind"], self.br.NONE)
        self.assertNotIn("SECRET", blocked["summary"])
        self.assertIn("SECRET-GL", self.frappe.db.sql.call_args.args[1]["other_gls"])
        self.assertIn("for update", self.frappe.db.sql.call_args.args[0])
        invoice.assert_not_called()

    def test_current_discovery_locks_payment_and_journal_queries(self):
        context = self.br.Context.__new__(self.br.Context)
        context.bank_gl, context.locking, context.claimed = {"BANK": "BANK-GL"}, True, set()
        line = Row(bank_account="BANK", deposit=100, date="2026-11-13")
        self.assertIsNone(context._match_existing(line, 100))
        queries = [call.args[0] for call in self.frappe.db.sql.call_args_list]
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertTrue(query.strip().endswith("for update"))

    def test_ordinary_voucher_hook_locks_company_without_restricting_erp_scope(self):
        with patch.object(self.br, "lock_documents") as lock, patch.object(self.br, "resolve_scope") as scope:
            self.br.serialize_voucher_write(Row(company="Other non-USD company"))
        lock.assert_called_once_with([("Company", "Other non-USD company")])
        scope.assert_not_called()
        self.frappe.only_for.assert_not_called()
        self.frappe.db.commit.assert_not_called()

    def test_deadlock_aborts_bank_batch_without_rolling_back_a_missing_savepoint(self):
        with patch.object(self.bank, "_queue", return_value=[{"bank_transaction": "BT-1"}]), \
                patch.object(self.br, "bank_line", return_value=Row(unallocated_amount=100)), \
                patch.object(self.br, "execute", side_effect=self.frappe.QueryDeadlockError("retry")):
            with self.assertRaises(self.frappe.QueryDeadlockError):
                self.bank._approve({"bank_transactions": ["BT-1"]}, SCOPE)
        self.frappe.db.rollback.assert_not_called()

    def test_unavailable_held_invoice_is_redacted_without_blocking_eligible_bills(self):
        for state in (PermissionError, LookupError, "cancelled"):
            held = Row(name="HELD-PI", client_invoice="SECRET-SI", hold_comment="Waiting for SECRET-SI")
            self.frappe.get_list.return_value = [held]
            def permitted(doctype, name, **kwargs):
                if doctype == "Sales Invoice":
                    if state == "cancelled":
                        return Row(docstatus=2, company=SCOPE["company"], currency="USD")
                    raise state
                return Row(modified="v1")
            with self.subTest(state=state), \
                    patch.object(self.pay, "resolve_scope", return_value=SCOPE), \
                    patch.object(self.pay, "_authorize"), \
                    patch.object(self.pay, "check_document", side_effect=permitted), \
                    patch.object(self.pay, "open_bills", return_value=[Row(name="ELIGIBLE", outstanding_amount=60)]), \
                    patch.object(self.pay, "credit_entries", return_value=[]), \
                    patch.object(self.pay, "_check_bank"), \
                    patch.object(self.pay, "fronting_warnings", return_value=[]):
                viewed = self.pay.preview("Supplier", ["ELIGIBLE"], bank_account="BANK")
            self.assertEqual(viewed["to_pay"], 60)
            self.assertIsNone(viewed["held"][0].client_invoice)
            self.assertNotIn("SECRET", viewed["held"][0].hold_comment)
            self.assertEqual(viewed["held"][0].reason_code, "blocking_invoice_unavailable")

    def test_credit_only_preview_and_preparation_do_not_need_bank_account(self):
        with patch.object(self.pay, "resolve_scope", return_value=SCOPE), \
                patch.object(self.pay, "_authorize"), \
                patch.object(self.pay, "check_document", return_value=Row(modified="v1")), \
                patch.object(self.pay, "open_bills", return_value=[Row(name="PI-1", outstanding_amount=100)]), \
                patch.object(self.pay, "credit_entries", return_value=[Row(amount=100)]), \
                patch.object(self.pay, "held_bills", return_value=[]), \
                patch.object(self.pay, "fronting_warnings", return_value=[]), \
                patch.object(self.pay, "usual_bank_account") as choose, \
                patch.object(self.pay, "_check_bank") as check:
            viewed = self.pay.preview("Supplier", ["PI-1"])
        self.assertEqual(viewed["to_pay"], 0)
        self.assertIsNone(viewed["bank_account"])
        self.assertIsNone(viewed["bank_details"])
        choose.assert_not_called()
        check.assert_not_called()
        with patch.object(self.pay, "apply_credit", return_value=100), \
                patch.object(self.pay, "open_bills", return_value=[]), \
                patch.object(self.pay, "new_payment") as payment:
            result = self.pay._prepare({"supplier": "Supplier", "invoices": ["PI-1"], "bank_account": None}, SCOPE)
        self.assertEqual(result["credit_applied"], 100)
        self.assertIsNone(result["payment_entry"])
        payment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
