"""Real funding trajectories. Run only on an explicitly enabled synthetic ERP site."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import unittest
import uuid

import frappe
from frappe.utils import now_datetime, today
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from books_assistant import bank_review, funding, pay_supplier
from books_assistant.tests import test_finance_synthetic as finance_fixtures


class TestFundingSynthetic(unittest.TestCase):
    setUp = finance_fixtures.TestSyntheticFinanceTrajectories.setUp
    tearDown = finance_fixtures.TestSyntheticFinanceTrajectories.tearDown
    invoice = finance_fixtures.TestSyntheticFinanceTrajectories.invoice
    bank_line = finance_fixtures.TestSyntheticFinanceTrajectories.bank_line
    freeze_bank = finance_fixtures.TestSyntheticFinanceTrajectories.freeze_bank

    def snapshot(self, balance=1000, *, bank=None, reflected=None, **fields):
        return frappe.get_doc(
            {
                "doctype": "Bank Funding Snapshot",
                "bank_account": bank or self.fixture["bank_account"],
                "available_balance": balance,
                "balance_as_of": now_datetime(),
                "pending_outflows": 0,
                "minimum_buffer": 0,
                "notes": self.marker,
                "reflected_reservations": reflected or [],
                **fields,
            }
        ).insert()

    def payout(self, amount=100, project=None):
        bill = self.invoice("Purchase Invoice", amount, submitted=True, project=project)
        payment = get_payment_entry(
            "Purchase Invoice", bill.name, bank_account=self.fixture["bank_gl"]
        )
        payment.update(
            {
                "bank_account": self.fixture["bank_account"],
                "reference_no": self.marker,
                "reference_date": today(),
                "posting_date": today(),
            }
        )
        payment.insert()
        return bill, payment

    def approve(self, payment, reason=None):
        check = funding.check_payment(payment.name)
        return funding.approve(payment.name, check["review_token"], reason)[
            "reservation"
        ]

    def alter_amount(self, payment, amount):
        payment.paid_amount = payment.received_amount = amount
        payment.references[0].allocated_amount = amount
        payment.save()

    def another_bank(self):
        original = frappe.get_doc("Account", self.fixture["bank_gl"])
        account = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": f"Funding alternate {uuid.uuid4().hex[:8]}",
                "company": self.company,
                "parent_account": original.parent_account,
                "account_type": "Bank",
                "account_currency": "USD",
                "is_group": 0,
            }
        ).insert()
        bank = frappe.get_doc(
            {
                "doctype": "Bank Account",
                "account_name": account.account_name,
                "bank": "Synthetic API Bank",
                "is_company_account": 1,
                "company": self.company,
                "account": account.name,
            }
        ).insert()
        return bank, account

    def paid_client_invoice(self, amount=100):
        sales = self.invoice("Sales Invoice", amount, submitted=True)
        line = self.bank_line(amount, sales)
        _reviewed, frozen = self.freeze_bank(line)
        result = bank_review.approve(frozen["preview_id"], str(uuid.uuid4()))
        self.assertTrue(result["results"][0]["ok"], result)
        sales.reload()
        self.assertEqual(sales.outstanding_amount, 0)
        return sales

    def test_missing_stale_and_shortfall_never_reserve(self):
        _bill, payment = self.payout(100)
        initial = funding.check_payment(payment.name)
        self.assertEqual(initial["cash"]["reason"], "missing_balance")
        with self.assertRaises(frappe.ValidationError):
            funding.approve(payment.name, initial["review_token"])
        self.snapshot(1000, balance_as_of=now_datetime() - timedelta(hours=5))
        self.assertEqual(
            funding.check_payment(payment.name)["cash"]["reason"], "stale_balance"
        )
        self.snapshot(80)
        report = funding.check_payment(payment.name)
        self.assertEqual(report["cash"]["shortfall"], 20)
        with self.assertRaises(frappe.ValidationError):
            funding.approve(payment.name, report["review_token"])
        self.assertFalse(
            frappe.db.exists("Payout Reservation", {"payment_entry": payment.name})
        )

    def test_approval_retry_and_unsent_release_preserve_exact_cash(self):
        _bill, payment = self.payout(100)
        self.snapshot(150)
        report = funding.check_payment(payment.name)
        first = funding.approve(payment.name, report["review_token"])
        retry = funding.approve(payment.name, report["review_token"])
        self.assertEqual(first["reservation"], retry["reservation"])
        self.assertEqual(
            frappe.db.count("Payout Reservation", {"payment_entry": payment.name}), 1
        )
        other_bill, other = self.payout(100)
        self.assertEqual(funding.check_payment(other.name)["cash"]["shortfall"], 50)
        funding.release(first["reservation"], "Synthetic payout not sent")
        self.assertTrue(funding.check_payment(other.name)["can_approve"])
        self.assertEqual(
            frappe.db.get_value(
                "Purchase Invoice", other_bill.name, "outstanding_amount"
            ),
            100,
        )

    def test_new_balance_invalidates_review_and_supplier_preparation(self):
        bill, payment = self.payout(100)
        self.snapshot(1000)
        report = funding.check_payment(payment.name)
        frozen = pay_supplier.preview_preparation(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        self.snapshot(90)
        with self.assertRaises(frappe.ValidationError):
            funding.approve(payment.name, report["review_token"])
        with self.assertRaises(frappe.ValidationError):
            pay_supplier.prepare(frozen["preview_id"], str(uuid.uuid4()))
        self.assertFalse(
            frappe.db.exists("API Request", {"preview": frozen["preview_id"]})
        )

    def test_bank_metadata_change_requires_new_review(self):
        _bill, payment = self.payout(100)
        self.snapshot(1000)
        report = funding.check_payment(payment.name)
        bank = frappe.get_doc("Bank Account", self.fixture["bank_account"])
        bank.db_set("bank_account_no", "SYNTHETIC-CHANGED")
        with self.assertRaises(frappe.ValidationError):
            funding.approve(payment.name, report["review_token"])
        self.assertFalse(
            frappe.db.exists("Payout Reservation", {"payment_entry": payment.name})
        )

    def test_fronting_reason_required_for_unpaid_exact_client_invoice(self):
        bill, payment = self.payout(100)
        sales = self.invoice("Sales Invoice", 200, submitted=True)
        bill.db_set("client_invoice", sales.name)
        self.snapshot(1000)
        report = funding.check_payment(payment.name)
        self.assertTrue(report["fronting_required"])
        with self.assertRaises(frappe.ValidationError):
            funding.approve(payment.name, report["review_token"])
        result = funding.approve(
            payment.name, report["review_token"], "Approved synthetic operating advance"
        )
        record = frappe.get_doc("Payout Reservation", result["reservation"])
        self.assertEqual(record.fronting_reason, "Approved synthetic operating advance")

    def test_grouped_timesheets_resolve_all_bank_paid_client_invoices(self):
        project = frappe.get_doc(
            {
                "doctype": "Project",
                "project_name": self.marker,
                "company": self.company,
                "customer": self.fixture["customer"],
            }
        ).insert()
        bill, payment = self.payout(100, project.name)
        invoices = [self.paid_client_invoice(80), self.paid_client_invoice(90)]
        activity = frappe.get_doc(
            {"doctype": "Activity Type", "activity_type": self.marker}
        ).insert()
        for index, invoice in enumerate(invoices):
            sheet = frappe.get_doc(
                {
                    "doctype": "Timesheet",
                    "company": self.company,
                    "time_logs": [
                        {
                            "activity_type": activity.name,
                            "from_time": f"{today()} {8 + index:02d}:00:00",
                            "hours": 1,
                            "is_billable": 1,
                            "billing_rate": 100,
                            "costing_rate": 50,
                        }
                    ],
                }
            ).insert()
            # The real extension owns these links after invoice creation.
            sheet.db_set("purchase_invoice", bill.name)
            frappe.db.set_value(
                "Timesheet Detail",
                sheet.time_logs[0].name,
                "sales_invoice",
                invoice.name,
            )
        bill.reload()
        self.assertFalse(bill.timesheet)
        self.assertFalse(bill.client_invoice)
        report = funding.client_funding(bill, self.company, "USD")
        self.assertEqual(report["status"], "received")
        self.assertEqual(
            {row["invoice"] for row in report["invoices"]},
            {invoice.name for invoice in invoices},
        )
        self.snapshot(1000)
        # Exercise the grouped fallback through the normal check as well.
        check = funding.check_payment(payment.name)
        self.assertFalse(check["fronting_required"])
        sheet = frappe.get_all(
            "Timesheet", filters={"purchase_invoice": bill.name}, pluck="name"
        )[0]
        detail = frappe.get_doc("Timesheet", sheet).time_logs[0]
        frappe.db.set_value("Timesheet Detail", detail.name, "sales_invoice", None)
        self.assertEqual(
            funding.client_funding(bill, self.company, "USD")["status"], "unverified"
        )

    def test_amount_edit_then_submit_can_reflect_actual_debit(self):
        _bill, payment = self.payout(100)
        self.snapshot(1000)
        reservation = self.approve(payment)
        self.alter_amount(payment, 70)
        self.assertEqual(
            frappe.db.get_value("Payout Reservation", reservation, "status"), "Invalid"
        )
        payment.submit()
        with self.assertRaises(frappe.ValidationError):
            funding.release(reservation, "Cannot silently release submitted cash")
        with self.assertRaises(frappe.ValidationError):
            self.snapshot(930, reflected=[{"reservation": reservation}])
        self.snapshot(
            930,
            reflected=[
                {
                    "reservation": reservation,
                    "resolution": "Reflected",
                    "resolution_reason": "Confirmed actual 70 debit included; original approval was 100",
                }
            ],
        )
        self.assertEqual(
            frappe.db.get_value("Payout Reservation", reservation, "status"),
            "Reflected",
        )
        other_bill, other = self.payout(100)
        self.assertEqual(
            funding.check_payment(other.name)["cash"]["available_for_payout"], 930
        )
        self.assertEqual(payment.docstatus, 1)
        self.assertEqual(
            frappe.db.get_value(
                "Purchase Invoice", other_bill.name, "outstanding_amount"
            ),
            100,
        )

    def test_bank_edit_then_submit_can_resolve_original_bank_without_cancelling_payment(
        self,
    ):
        _bill, payment = self.payout(100)
        self.snapshot(1000)
        reservation = self.approve(payment)
        bank, account = self.another_bank()
        payment.paid_from, payment.bank_account = account.name, bank.name
        payment.save()
        payment.submit()
        self.assertEqual(
            frappe.db.get_value("Payout Reservation", reservation, "status"), "Invalid"
        )
        with self.assertRaises(frappe.ValidationError):
            self.snapshot(1000, reflected=[{"reservation": reservation}])
        self.snapshot(
            1000,
            reflected=[
                {
                    "reservation": reservation,
                    "resolution": "Not sent from this bank",
                    "resolution_reason": "Checked original bank: no transfer sent; payout recorded at alternate bank",
                }
            ],
        )
        self.assertEqual(
            frappe.db.get_value("Payout Reservation", reservation, "status"), "Released"
        )
        self.assertEqual(payment.docstatus, 1)
        self.assertEqual(
            funding.latest_snapshot(self.fixture["bank_account"]).available_balance,
            1000,
        )

    def test_normal_submitted_payout_reflection_does_not_subtract_twice(self):
        _bill, payment = self.payout(100)
        self.snapshot(1000)
        reservation = self.approve(payment)
        payment.submit()
        self.assertEqual(
            frappe.db.get_value("Payout Reservation", reservation, "status"), "Active"
        )
        with self.assertRaises(frappe.ValidationError):
            self.snapshot(
                900,
                balance_as_of=frappe.db.get_value(
                    "Payout Reservation", reservation, "creation"
                ),
                reflected=[{"reservation": reservation}],
            )
        self.snapshot(900, reflected=[{"reservation": reservation}])
        _other_bill, other = self.payout(100)
        check = funding.check_payment(other.name)
        self.assertEqual(check["cash"]["reserved"], 0)
        self.assertEqual(check["cash"]["available_for_payout"], 900)

    def test_permissions_prevent_cash_approval_and_generic_reservation_mutations(self):
        _bill, payment = self.payout(100)
        self.snapshot(1000)
        reservation = self.approve(payment)
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"funding-{uuid.uuid4()}@example.test",
                "first_name": "Synthetic funding reader",
                "send_welcome_email": 0,
                "roles": [{"role": "Accounts User"}],
            }
        ).insert(ignore_permissions=True)
        frappe.set_user(user.name)
        previous = frappe.flags.in_test
        frappe.flags.in_test = (
            False  # only_for intentionally bypasses checks during Frappe tests.
        )
        try:
            with self.assertRaises(frappe.PermissionError):
                funding.check_payment(payment.name)
            for operation in ("create", "write", "delete"):
                self.assertFalse(frappe.has_permission("Payout Reservation", operation))
        finally:
            frappe.flags.in_test = previous
            frappe.set_user("Administrator")
        record = frappe.get_doc("Payout Reservation", reservation)
        record.status = "Released"
        with self.assertRaises(frappe.ValidationError):
            record.save()

    def test_existing_settings_preserve_switches_and_initialize_only_missing_freshness(
        self,
    ):
        from books_assistant.setup import initialise_settings

        frappe.db.delete(
            "Singles",
            {
                "doctype": "Automation Settings",
                "field": "funding_balance_max_age_hours",
            },
        )
        frappe.db.set_single_value("Automation Settings", "send_invoice_emails", 0)
        initialise_settings()
        self.assertEqual(
            frappe.db.get_single_value(
                "Automation Settings", "funding_balance_max_age_hours", cache=False
            ),
            4,
        )
        self.assertEqual(
            frappe.db.get_single_value(
                "Automation Settings", "send_invoice_emails", cache=False
            ),
            0,
        )
        frappe.db.set_single_value(
            "Automation Settings", "funding_balance_max_age_hours", 2
        )
        initialise_settings()
        self.assertEqual(
            frappe.db.get_single_value(
                "Automation Settings", "funding_balance_max_age_hours", cache=False
            ),
            2,
        )

    def test_inaccessible_linked_client_fails_closed_even_for_finance_manager(self):
        bill, payment = self.payout(100)
        sales = self.invoice("Sales Invoice", 200, submitted=True)
        bill.db_set("client_invoice", sales.name)
        self.snapshot(1000)
        other = frappe.copy_doc(frappe.get_doc("Customer", self.fixture["customer"]))
        other.customer_name = f"Other synthetic client {uuid.uuid4().hex[:8]}"
        other.insert()
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"restricted-{uuid.uuid4()}@example.test",
                "first_name": "Restricted finance",
                "send_welcome_email": 0,
                "roles": [{"role": "Accounts Manager"}],
            }
        ).insert(ignore_permissions=True)
        frappe.get_doc(
            {
                "doctype": "User Permission",
                "user": user.name,
                "allow": "Customer",
                "for_value": other.name,
                "apply_to_all_doctypes": 1,
            }
        ).insert()
        frappe.set_user(user.name)
        previous = frappe.flags.in_test
        frappe.flags.in_test = False
        try:
            self.assertTrue(frappe.has_permission("Purchase Invoice", "read", doc=bill))
            self.assertFalse(frappe.has_permission("Sales Invoice", "read", doc=sales))
            with self.assertRaises(frappe.PermissionError):
                funding.check_payment(payment.name)
            with self.assertRaises(frappe.PermissionError):
                pay_supplier.preview_preparation(
                    self.fixture["supplier"],
                    [bill.name],
                    company=self.company,
                    bank_account=self.fixture["bank_gl"],
                )
            self.assertFalse(
                frappe.db.exists("Payout Reservation", {"payment_entry": payment.name})
            )
        finally:
            frappe.flags.in_test = previous
            frappe.set_user("Administrator")

    def test_snapshot_is_immutable_and_future_or_duplicate_evidence_fails(self):
        snapshot = self.snapshot(1000)
        snapshot.notes = "Changed evidence"
        with self.assertRaises(frappe.ValidationError):
            snapshot.save()
        with self.assertRaises(frappe.ValidationError):
            self.snapshot(1000, balance_as_of=now_datetime() + timedelta(hours=1))
        with self.assertRaises(frappe.ValidationError):
            self.snapshot(1000, balance_as_of=snapshot.balance_as_of)


class TestFundingConcurrency(unittest.TestCase):
    invoice = finance_fixtures.TestSyntheticFinanceTrajectories.invoice
    payout = TestFundingSynthetic.payout
    snapshot = TestFundingSynthetic.snapshot

    def setUp(self):
        finance_fixtures.TestSyntheticFinanceTrajectories.setUp(self)
        if not frappe.conf.get("books_assistant_transaction_tests"):
            raise AssertionError(
                "Enable transaction tests only on this disposable site"
            )
        self.site, self.sites_path = frappe.local.site, frappe.local.sites_path
        self.committed = False

    def worker(self, payment, token, barrier):
        frappe.init(site=self.site, sites_path=self.sites_path)
        frappe.connect()
        frappe.set_user("Administrator")
        try:
            frappe.db.rollback()
            frappe.db.sql("set session transaction isolation level repeatable read")
            frappe.db.sql("select name from `tabPayout Reservation` limit 1")
            barrier.wait(timeout=15)
            try:
                result = funding.approve(payment, token)
                frappe.db.commit()
                return result["reservation"]
            except frappe.ValidationError:
                frappe.db.rollback()
                return None
        finally:
            frappe.destroy()

    def test_two_connections_cannot_reserve_same_available_cash_twice(self):
        _first_bill, first = self.payout(100)
        _second_bill, second = self.payout(100)
        self.snapshot(150)
        tokens = [
            funding.check_payment(payment.name)["review_token"]
            for payment in (first, second)
        ]
        frappe.db.commit()
        self.committed = True
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: self.worker(*args, barrier),
                    [(first.name, tokens[0]), (second.name, tokens[1])],
                )
            )
        frappe.db.rollback()
        self.assertEqual(sum(result is not None for result in results), 1)
        records = funding.reservations(bank_account=self.fixture["bank_account"])
        self.assertEqual(sum(float(row.amount) for row in records), 100)

    def test_duplicate_approval_under_repeatable_read_creates_one_reservation(self):
        _bill, payment = self.payout(100)
        self.snapshot(150)
        token = funding.check_payment(payment.name)["review_token"]
        frappe.db.commit()
        self.committed = True
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.worker, payment.name, token, barrier)
                for _ in range(2)
            ]
            results = [future.result(timeout=60) for future in futures]
        frappe.db.rollback()
        self.assertTrue(results[0])
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            frappe.db.count("Payout Reservation", {"payment_entry": payment.name}), 1
        )

    def tearDown(self):
        if self.committed:
            # Synthetic-only cleanup for records that must be visible to two connections.
            frappe.db.rollback()
            for row in frappe.get_all(
                "Payment Entry", filters={"reference_no": self.marker}, fields=["name"]
            ):
                for record in frappe.get_all(
                    "Payout Reservation",
                    filters={"payment_entry": row.name},
                    pluck="name",
                ):
                    frappe.db.delete(
                        "Version",
                        {"ref_doctype": "Payout Reservation", "docname": record},
                    )
                    frappe.db.delete("Payout Reservation", {"name": record})
                frappe.delete_doc("Payment Entry", row.name, force=True)
            for row in frappe.get_all(
                "Purchase Invoice", filters={"remarks": self.marker}, pluck="name"
            ):
                invoice = frappe.get_doc("Purchase Invoice", row)
                invoice.cancel()
                frappe.delete_doc("Purchase Invoice", row, force=True)
                # ERP's immutable cancellation rows outlive deletion and naming
                # counters can be reused. Remove only these synthetic vouchers.
                frappe.db.delete(
                    "GL Entry", {"voucher_type": "Purchase Invoice", "voucher_no": row}
                )
                frappe.db.delete(
                    "Payment Ledger Entry",
                    {"voucher_type": "Purchase Invoice", "voucher_no": row},
                )
            for row in frappe.get_all(
                "Bank Funding Snapshot", filters={"notes": self.marker}, pluck="name"
            ):
                frappe.db.delete("Bank Funding Snapshot", {"name": row})
            frappe.db.commit()
        finance_fixtures.TestSyntheticFinanceTrajectories.tearDown(self)


def run_tests():
    if not frappe.conf.get("books_assistant_synthetic_site") or not frappe.conf.get(
        "books_assistant_transaction_tests"
    ):
        raise AssertionError(
            "Funding real-site tests require an explicitly enabled disposable site"
        )
    suite = unittest.defaultTestLoader.loadTestsFromNames(
        [
            __name__ + ".TestFundingSynthetic",
            __name__ + ".TestFundingConcurrency",
        ]
    )
    previous = frappe.flags.get("in_test")
    frappe.flags.in_test = True
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful() or result.skipped:
            raise AssertionError(
                f"Funding tests failed: {len(result.failures)} failures, {len(result.errors)} errors, {len(result.skipped)} skipped"
            )
        return {"tests_run": result.testsRun, "failures": 0, "errors": 0, "skipped": 0}
    finally:
        frappe.flags.in_test = previous
