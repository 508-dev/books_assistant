"""Boundary tests runnable without a Frappe site; DB operations are mocked explicitly."""

from contextlib import ExitStack
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


class Row(dict):
    def __getattr__(self, key):
        return self.get(key)


class Rejected(Exception):
    pass


def load_module(path, name, frappe):
    utils = ModuleType("frappe.utils")
    utils.now_datetime = lambda: datetime(2026, 11, 13, 12)
    utils.get_datetime = lambda value: value
    model = ModuleType("frappe.model")
    document = ModuleType("frappe.model.document")
    document.Document = object
    with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils,
                                "frappe.model": model, "frappe.model.document": document}):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class TestFundingAPI(unittest.TestCase):
    def setUp(self):
        self.frappe = MagicMock()
        self.frappe.whitelist.side_effect = lambda **kwargs: lambda function: function
        self.frappe._ = lambda text: text
        self.frappe.throw.side_effect = Rejected
        self.frappe.parse_json.side_effect = __import__("json").loads
        self.api = load_module(Path(__file__).parents[1] / "funding.py", "funding_under_test", self.frappe)
        self.doc = MagicMock()
        self.doc.name, self.doc.company, self.doc.paid_amount = "PE-1", "508", 6000
        self.doc.paid_from = "Bank - 508"
        self.doc.get.return_value = None
        self.frappe.db.get_value.return_value = "Novo"
        self.report = dict(payment_fingerprint="signature", can_approve=True, fronting_required=False,
                           review_token="review", approval=None, currency="USD", cash={"snapshot": "SNAP-1"})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.real_permitted = self.api.permitted
        self.permitted = self.stack.enter_context(patch.object(self.api, "permitted", return_value=self.doc))
        self.stack.enter_context(patch.object(self.api, "payout_bank", return_value="Novo"))
        self.lock = self.stack.enter_context(patch.object(self.api, "lock_bank"))
        self.real_payment_report = self.api.payment_report
        self.payment_report = self.stack.enter_context(patch.object(self.api, "payment_report", return_value=self.report))

    def test_unauthorized_actor_cannot_reach_financial_reads(self):
        self.frappe.only_for.side_effect = Rejected("forbidden")
        with self.assertRaises(Rejected):
            self.api.check_payment("PE-1")
        self.permitted.assert_not_called()
        self.payment_report.assert_not_called()

    def test_approval_rechecks_under_bank_lock_before_reserving(self):
        order = []
        self.lock.side_effect = lambda *args: order.append("lock")
        self.payment_report.side_effect = lambda *args: order.append("recheck") or self.report
        record = self.frappe.get_doc.return_value
        record.name = "RES-1"
        result = self.api.approve("PE-1", "review")
        self.assertEqual(order, ["lock", "recheck"])
        self.assertEqual(result["reservation"], "RES-1")
        payload = self.frappe.get_doc.call_args.args[0]
        self.assertEqual((payload["amount"], payload["snapshot"], payload["status"]), (6000, "SNAP-1", "Active"))
        record.insert.assert_called_once_with(ignore_permissions=True)
        self.doc.add_comment.assert_called_once()

    def test_changed_review_and_insufficient_or_unverified_funds_cannot_reserve(self):
        for token, can_approve in (("old-review", True), ("review", False)):
            with self.subTest(token=token, can_approve=can_approve):
                self.report["can_approve"] = can_approve
                with self.assertRaises(Rejected):
                    self.api.approve("PE-1", token)
        self.frappe.get_doc.assert_not_called()

    def test_fronting_requires_recorded_reason(self):
        self.report["fronting_required"] = True
        with self.assertRaises(Rejected):
            self.api.approve("PE-1", "review", "  ")
        self.frappe.get_doc.assert_not_called()
        self.api.approve("PE-1", "review", "Authorized advance from operating reserves")
        self.assertIn("Authorized advance", self.frappe.get_doc.call_args.args[0]["fronting_reason"])

    def test_repeat_click_returns_same_reservation_without_second_write(self):
        self.report["approval"] = dict(name="RES-1", status="Active", payment_fingerprint="signature", snapshot="SNAP-1")
        result = self.api.approve("PE-1", "review")
        self.assertEqual(result["reservation"], "RES-1")
        self.frappe.get_doc.assert_not_called()
        with self.assertRaises(Rejected):
            self.api.approve("PE-1", "stale-review")

    def test_submitted_payment_cannot_silently_free_reserved_cash(self):
        record = Row(bank_account="Novo", status="Active", payment_entry="PE-1")
        record.reload = MagicMock()
        self.permitted.side_effect = lambda doctype, name, **kwargs: Row(docstatus=1) if doctype == "Payment Entry" else record
        with patch.object(self.api, "resolve") as resolve:
            with self.assertRaises(Rejected):
                self.api.release("RES-1", "Cancel payout")
            resolve.assert_not_called()

    def test_missing_exact_client_link_is_unverified(self):
        bill = Row(name="PINV-1")
        result = self.api.client_funding(bill, "508", "USD")
        self.assertEqual(result["status"], "unverified")
        self.frappe.get_all.assert_not_called()

    def test_paid_invoice_without_bank_receipts_is_unverified(self):
        invoice = Row(name="SINV-1", company="508", currency="USD", docstatus=1,
                      is_return=0, outstanding_amount=0, grand_total=10000, modified="v1")
        self.permitted.return_value = invoice
        with patch.object(self.api, "settled_receipts", return_value=0):
            self.assertEqual(self.api.client_funding(Row(name="PINV-1", client_invoice="SINV-1"), "508", "USD")["status"], "unverified")
        invoice.outstanding_amount = 1
        invoice["outstanding_amount"] = 1
        self.assertEqual(self.api.client_funding(Row(name="PINV-1", client_invoice="SINV-1"), "508", "USD")["status"], "unpaid")

    def test_exact_fully_matched_client_receipt_is_received(self):
        self.permitted.return_value = Row(name="SINV-1", company="508", currency="USD", docstatus=1,
                                         is_return=0, outstanding_amount=0, grand_total=10000, modified="v1")
        with patch.object(self.api, "settled_receipts", return_value=10000):
            result = self.api.client_funding(Row(name="PINV-1", client_invoice="SINV-1"), "508", "USD")
            self.assertEqual(result["status"], "received")

    def test_bank_receipt_must_cover_whole_payment_not_just_invoice_allocation(self):
        invoice = Row(name="SINV-1", company="508", currency="USD", customer="Client")
        row = Row(name="RECEIPT-1", paid_to="Bank - 508", received_amount=10000, allocated=6000)
        for matched, expected in ((6000, 0), (10000, 6000)):
            with self.subTest(matched=matched):
                self.frappe.db.sql.side_effect = [[row], [[matched]]]
                self.assertEqual(self.api.settled_receipts(invoice), expected)

    def test_edited_approval_becomes_invalid_without_freeing_cash(self):
        self.doc.docstatus = 0
        row = Row(name="RES-1", status="Active", payment_fingerprint="old")
        with patch.object(self.api, "reservations", return_value=[row]), \
                patch.object(self.api, "payment_signature", return_value="new"), \
                patch.object(self.api, "resolve") as resolve:
            self.api.invalidate_payment(self.doc)
            self.assertEqual(resolve.call_args.args[1], "Invalid")

    def test_record_permissions_checked_not_just_role(self):
        doc = self.frappe.get_doc.return_value
        doc.check_permission.side_effect = Rejected("denied")
        with self.assertRaises(Rejected):
            self.real_permitted("Payment Entry", "PE-1")
        self.payment_report.assert_not_called()

    def test_assessment_scopes_company_currency_and_keeps_missing_client_link_uncertain(self):
        self.lock.return_value = (Row(name="Novo"), Row(name="Bank - 508", company="508", account_currency="USD"))
        self.frappe.get_cached_value.return_value = "USD"
        self.frappe.db.get_single_value.return_value = 4
        bill = Row(name="PI-1", company="508", currency="USD", supplier="Developer", docstatus=1,
                   is_return=0, on_hold=0, outstanding_amount=6000, modified="v1", project="Client project")
        self.permitted.return_value = bill
        snapshot = Row(name="SNAP-1", balance_as_of=datetime(2026, 11, 13, 12), available_balance=8000,
                       pending_outflows=1000, minimum_buffer=1000)
        with patch.object(self.api, "latest_snapshot", return_value=snapshot), \
                patch.object(self.api, "reservations", return_value=[Row(name="RES-2", payment_entry="PE-2", amount=2000)]):
            report = self.api.assess("Novo", 6000, ["PI-1"], "Developer")
            self.assertEqual(report["cash"]["shortfall"], 2000)
            self.assertFalse(report["can_approve"])
            self.assertTrue(report["fronting_required"])
            self.assertEqual(report["clients"][0]["reason"], "missing_client_invoice")
            bill["currency"] = "EUR"
            with self.assertRaises(Rejected):
                self.api.assess("Novo", 6000, ["PI-1"], "Developer")

    def test_review_token_survives_own_reservation_but_changes_with_other_commitments(self):
        doc = Row(name="PE-1", company="508", docstatus=0, payment_type="Pay", party_type="Supplier",
                  party="Developer", paid_amount=6000, paid_from_account_currency="USD", paid_to_account_currency="USD",
                  references=[Row(reference_doctype="Purchase Invoice", reference_name="PI-1", allocated_amount=6000)])
        def assessment(*args, **kwargs):
            return dict(company="508", currency="USD", cash={"status": "sufficient"}, issues=[], can_approve=True,
                        bills=[{"name": "PI-1", "outstanding": 6000}], fronting_required=False, clients=[],
                        reservations=[], review_token="ignored")
        own = Row(name="RES-1", bank_account="Novo", status="Active", payment_fingerprint="sig")
        with patch.object(self.api, "assess", side_effect=assessment) as assess, \
                patch.object(self.api, "reservations", return_value=[]) as reservations, \
                patch.object(self.api, "payment_signature", return_value="sig"):
            original = self.real_payment_report(doc)
            reservations.return_value = [own]
            retry = self.real_payment_report(doc)
            self.assertEqual(original["review_token"], retry["review_token"])
            def changed(*args, **kwargs):
                report = assessment()
                report["reservations"] = [{"name": "RES-2", "amount": 1000}]
                return report
            assess.side_effect = changed
            self.assertNotEqual(original["review_token"], self.real_payment_report(doc)["review_token"])

    def test_snapshot_reflection_requires_submitted_matching_bank_and_amount(self):
        path = Path(__file__).parents[1] / "books_assistant/doctype/bank_funding_snapshot/bank_funding_snapshot.py"
        with patch.dict(sys.modules, {"books_assistant.funding": self.api}):
            module = load_module(path, "snapshot_under_test", self.frappe)
        bank, account = Row(name="Novo"), Row(name="Bank - 508", company="508", account_currency="USD")
        module.lock_bank.return_value = bank, account
        snapshot = module.BankFundingSnapshot()
        snapshot.is_new = lambda: True
        snapshot.bank_account, snapshot.balance_as_of = "Novo", datetime(2026, 11, 13, 11)
        snapshot.available_balance, snapshot.pending_outflows, snapshot.minimum_buffer = 8000, 0, 1000
        snapshot.get = lambda key: getattr(snapshot, key)
        snapshot.set = lambda key, value: setattr(snapshot, key, value)
        snapshot.reflected_reservations = [Row(reservation="RES-1")]
        reservation = Row(bank_account="Novo", status="Active", payment_entry="PE-1", amount=6000,
                          creation=datetime(2026, 11, 13, 10))
        payment = Row(docstatus=0, paid_from="Bank - 508", paid_amount=6000)
        module.permitted.side_effect = lambda doctype, name, **kwargs: reservation if doctype == "Payout Reservation" else payment
        with patch.object(module, "latest_snapshot", return_value=None):
            with self.assertRaises(Rejected):
                snapshot.validate()
            payment["docstatus"] = 1
            payment["paid_amount"] = 5000
            with self.assertRaises(Rejected):
                snapshot.validate()
            payment["paid_amount"] = 6000
            snapshot.validate()
            self.assertEqual(snapshot.company, "508")
            self.assertEqual(snapshot.currency, "USD")

    def test_existing_reservation_cannot_bypass_new_client_funding_warning(self):
        self.report.update(fronting_required=True, approval=dict(name="RES-1", status="Active", snapshot="SNAP-1"))
        with self.assertRaises(Rejected):
            self.api.approve("PE-1", "review")
        self.frappe.get_doc.assert_not_called()
