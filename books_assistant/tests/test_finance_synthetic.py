"""Real accounting trajectories on an explicitly enabled, disposable synthetic site.

Run with bench --site <disposable> run-tests --app books_assistant
--module books_assistant.tests.test_finance_synthetic. These tests use actual
ERPNext invoices, ledgers and bank reconciliation, with no mocked integrations.
No email or bank transfer is sent. Each test rolls back its accounting records;
seed_minimal_site commits only the reusable synthetic setup masters.
"""

import unittest
import uuid

import frappe
from frappe.utils import add_days, flt, getdate, today

from books_assistant import bank_review, billing_api, pay_supplier
from books_assistant.books_assistant.doctype.billing_run.billing_run import editing_run
from books_assistant.setup import HOURLY
from books_assistant.tests.synthetic_site import seed_minimal_site


class TestSyntheticFinanceTrajectories(unittest.TestCase):
    def setUp(self):
        if not frappe.conf.get("books_assistant_synthetic_site"):
            self.skipTest(
                "Requires books_assistant_synthetic_site=1 on a disposable site"
            )
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True
        frappe.flags.books_assistant_locked_documents = set()
        frappe.flags.books_assistant_billing_locked = False
        self.fixture = seed_minimal_site()
        self.marker = f"synthetic-finance-{uuid.uuid4()}"
        self.company = self.fixture["company"]

    def tearDown(self):
        if not hasattr(self, "fixture"):
            return
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.flags.books_assistant_locked_documents = set()
        frappe.flags.books_assistant_billing_locked = False
        frappe.clear_document_cache("Automation Settings", "Automation Settings")
        frappe.clear_last_message()

    def invoice(self, doctype, amount, *, submitted=False, project=None):
        sales = doctype == "Sales Invoice"
        fields = {
            "doctype": doctype,
            "company": self.company,
            "currency": "USD",
            "project": project,
            "posting_date": today(),
            "due_date": add_days(today(), 30),
            "set_posting_time": 1,
            "conversion_rate": 1,
            "price_list_currency": "USD",
            "plc_conversion_rate": 1,
            "remarks": self.marker,
            "naming_series": "ACC-SINV-.YYYY.-" if sales else "ACC-PINV-.YYYY.-",
            "items": [
                {
                    "item_code": self.fixture["item"],
                    "qty": 1,
                    "rate": amount,
                    "project": project,
                    "cost_center": self.fixture["cost_center"],
                    "income_account" if sales else "expense_account": self.fixture[
                        "income_account" if sales else "expense_account"
                    ],
                }
            ],
        }
        if sales:
            fields.update(
                customer=self.fixture["customer"],
                selling_price_list=self.fixture["selling_price_list"],
            )
        else:
            fields.update(
                supplier=self.fixture["supplier"],
                buying_price_list=self.fixture["buying_price_list"],
            )
        doc = frappe.get_doc(fields).insert()
        if submitted:
            doc.submit()
        return doc

    def bank_line(self, amount, invoice):
        line = frappe.get_doc(
            {
                "doctype": "Bank Transaction",
                "date": today(),
                "bank_account": self.fixture["bank_account"],
                "currency": "USD",
                "description": f"Payment for {invoice.name} {self.marker}",
                "deposit": amount,
                "withdrawal": 0,
                "party_type": "Customer",
                "party": self.fixture["customer"],
            }
        ).insert()
        line.submit()
        return line

    def freeze_bank(self, line):
        rows = bank_review.get_queue(
            bank_account=self.fixture["bank_account"], company=self.company
        )
        reviewed = next(row for row in rows if row["bank_transaction"] == line.name)
        frozen = bank_review.preview_approval(
            [line.name],
            {line.name: reviewed["fingerprint"]},
            company=self.company,
            bank_account=self.fixture["bank_account"],
        )
        return reviewed, frozen

    def assert_unused(self, preview_id):
        self.assertEqual(frappe.db.count("API Request", {"preview": preview_id}), 0)
        self.assertFalse(
            frappe.db.get_value("API Preview", preview_id, "consumed_request")
        )

    def prepared_run(self, sales_amount=347.59, engineer_amount=241.13):
        self.assertTrue(
            frappe.get_meta("Purchase Invoice").has_field("referral_source_si"),
            "Install referral_fee on the disposable site before billing trajectories",
        )
        self.assertTrue(
            frappe.get_meta("Timesheet").has_field("purchase_invoice"),
            "Install timesheet_extended on the disposable site before billing trajectories",
        )
        project = frappe.get_doc(
            {
                "doctype": "Project",
                "project_name": self.marker,
                "company": self.company,
                "customer": self.fixture["customer"],
                "billing_method": HOURLY,
            }
        ).insert()
        sales = self.invoice("Sales Invoice", sales_amount, project=project.name)
        bill = self.invoice("Purchase Invoice", engineer_amount, project=project.name)
        with editing_run():
            run = frappe.get_doc(
                {
                    "doctype": "Billing Run",
                    "company": self.company,
                    "currency": "USD",
                    "period_start": getdate(today()).replace(day=1),
                    "period_end": today(),
                    "invoice_date": today(),
                    "status": "Prepared",
                    "items": [
                        {
                            "project": project.name,
                            "customer": self.fixture["customer"],
                            "billing_method": HOURLY,
                            "hours": 1,
                            "client_amount": sales.grand_total,
                            "engineer_cost": bill.grand_total,
                            "referral_cost": 0,
                            "sales_invoice": sales.name,
                            "engineer_bills": bill.name,
                            "item_status": "pending",
                        }
                    ],
                }
            ).insert()
        frappe.db.set_single_value("Automation Settings", "hold_engineer_bills", 1)
        frappe.clear_document_cache("Automation Settings", "Automation Settings")
        return run, project, sales, bill

    def test_supplier_preview_confirm_retry_returns_one_exact_unsaved_payment(self):
        bill = self.invoice("Purchase Invoice", 183.27, submitted=True)
        payments_before = frappe.db.count("Payment Entry")
        previews_before = frappe.db.count("API Preview")
        viewed = pay_supplier.preview(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        self.assertEqual(frappe.db.count("API Preview"), previews_before)
        self.assertEqual(flt(viewed["to_pay"], 2), 183.27)
        self.assertEqual(flt(viewed["credit_to_apply"], 2), 0)
        frozen = pay_supplier.preview_preparation(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        request = str(uuid.uuid4())
        result = pay_supplier.prepare(frozen["preview_id"], request)
        self.assertEqual(result, pay_supplier.prepare(frozen["preview_id"], request))
        payment = result["payment_entry"]
        self.assertEqual(payment["docstatus"], 0)
        self.assertEqual(payment["__islocal"], 1)
        self.assertEqual(payment["party"], self.fixture["supplier"])
        self.assertEqual(payment["paid_from"], self.fixture["bank_gl"])
        self.assertEqual(payment["bank_account"], self.fixture["bank_account"])
        self.assertEqual(flt(payment["paid_amount"], 2), 183.27)
        self.assertEqual(
            [row["reference_name"] for row in payment["references"]], [bill.name]
        )
        self.assertFalse(result["bank_transfer"])
        self.assertFalse(result["payment_saved"])
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before)
        self.assertEqual(
            frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 1
        )
        bill.reload()
        self.assertEqual(flt(bill.outstanding_amount, 2), 183.27)

    def test_supplier_credit_is_applied_once_and_remains_in_outer_transaction(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import (
            get_payment_entry,
        )

        bill = self.invoice("Purchase Invoice", 100, submitted=True)
        credit = get_payment_entry(
            "Purchase Invoice",
            bill.name,
            party_amount=30,
            bank_account=self.fixture["bank_gl"],
        )
        # An earlier payment exists but has not yet been assigned to any bill.
        credit.set("references", [])
        credit.update(
            {
                "paid_amount": 30,
                "received_amount": 30,
                "posting_date": today(),
                "reference_date": today(),
                "reference_no": self.marker,
                "bank_account": self.fixture["bank_account"],
            }
        )
        credit.insert()
        credit.submit()
        credit.reload()
        self.assertEqual(flt(credit.unallocated_amount, 2), 30)
        self.assertFalse(credit.references)
        bill.reload()
        self.assertEqual(flt(bill.outstanding_amount, 2), 100)
        payments_before = frappe.db.count("Payment Entry")
        viewed = pay_supplier.preview(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        self.assertEqual(
            (flt(viewed["credit_to_apply"], 2), flt(viewed["to_pay"], 2)), (30, 70)
        )
        self.assertEqual(
            [row.reference_name for row in viewed["credits"]], [credit.name]
        )
        # A read-only preview must leave the credit entirely unapplied.
        credit.reload()
        self.assertEqual(flt(credit.unallocated_amount, 2), 30)
        frozen = pay_supplier.preview_preparation(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        request = str(uuid.uuid4())
        # Rolling back to this savepoint at the end proves the ERP provider did
        # not internally commit accounting writes outside the API transaction.
        frappe.db.savepoint("synthetic_supplier_credit")
        result = pay_supplier.prepare(frozen["preview_id"], request)
        payment = result["payment_entry"]
        self.assertEqual(flt(result["credit_applied"], 2), 30)
        self.assertEqual(flt(payment["paid_amount"], 2), 70)
        self.assertEqual(payment["__islocal"], 1)
        self.assertEqual(
            [row["reference_name"] for row in payment["references"]], [bill.name]
        )
        self.assertEqual(flt(payment["references"][0]["allocated_amount"], 2), 70)
        self.assertFalse(result["payment_saved"])
        self.assertFalse(result["bank_transfer"])
        bill.reload()
        credit.reload()
        self.assertEqual(flt(bill.outstanding_amount, 2), 70)
        self.assertEqual(flt(credit.unallocated_amount, 2), 0)
        self.assertEqual([row.reference_name for row in credit.references], [bill.name])
        self.assertEqual(flt(credit.references[0].allocated_amount, 2), 30)

        def ledgers():
            return {
                "gl": frappe.get_all(
                    "GL Entry",
                    filters={"voucher_no": ["in", [bill.name, credit.name]]},
                    fields=["name", "voucher_no", "debit", "credit"],
                    order_by="name",
                ),
                "payment": frappe.get_all(
                    "Payment Ledger Entry",
                    filters={"voucher_no": credit.name},
                    fields=[
                        "name",
                        "amount",
                        "delinked",
                        "against_voucher_type",
                        "against_voucher_no",
                    ],
                    order_by="name",
                ),
            }

        booked = ledgers()
        self.assertEqual(result, pay_supplier.prepare(frozen["preview_id"], request))
        self.assertEqual(ledgers(), booked)
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before)
        self.assertEqual(
            frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 1
        )
        credit.reload()
        self.assertEqual([row.reference_name for row in credit.references], [bill.name])
        self.assertEqual(flt(credit.references[0].allocated_amount, 2), 30)
        frappe.db.rollback(save_point="synthetic_supplier_credit")
        bill.reload()
        credit.reload()
        self.assertEqual(flt(bill.outstanding_amount, 2), 100)
        self.assertEqual(flt(credit.unallocated_amount, 2), 30)
        self.assertFalse(credit.references)
        self.assert_unused(frozen["preview_id"])

    def test_supplier_hold_added_after_review_prevents_credit_or_payment(self):
        bill = self.invoice("Purchase Invoice", 193.41, submitted=True)
        frozen = pay_supplier.preview_preparation(
            self.fixture["supplier"],
            [bill.name],
            company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        bill.db_set({"on_hold": 1, "hold_comment": "Synthetic hold added after review"})
        payments_before = frappe.db.count("Payment Entry")
        with self.assertRaises(frappe.ValidationError):
            pay_supplier.prepare(frozen["preview_id"], str(uuid.uuid4()))
        self.assert_unused(frozen["preview_id"])
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before)
        bill.reload()
        self.assertEqual(flt(bill.outstanding_amount, 2), 193.41)
        self.assertTrue(bill.on_hold)

    def test_bank_receipt_confirm_retry_pays_invoice_exactly_once(self):
        sales = self.invoice("Sales Invoice", 127.43, submitted=True)
        line = self.bank_line(sales.grand_total, sales)
        payments_before = frappe.db.count("Payment Entry")
        reviewed, frozen = self.freeze_bank(line)
        self.assertEqual(reviewed["kind"], "receipt")
        self.assertEqual([row["name"] for row in reviewed["invoices"]], [sales.name])
        request = str(uuid.uuid4())
        result = bank_review.approve(frozen["preview_id"], request)
        self.assertTrue(result["results"][0]["ok"], result["results"])
        self.assertEqual(result, bank_review.approve(frozen["preview_id"], request))
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before + 1)
        self.assertEqual(
            frappe.db.count("Payment Entry Reference", {"reference_name": sales.name}),
            1,
        )
        self.assertEqual(
            frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 1
        )
        sales.reload()
        line.reload()
        self.assertEqual(flt(sales.outstanding_amount, 2), 0)
        self.assertEqual(flt(line.unallocated_amount, 2), 0)
        self.assertEqual(len(line.payment_entries), 1)
        payment = frappe.get_doc("Payment Entry", line.payment_entries[0].payment_entry)
        self.assertEqual(payment.docstatus, 1)
        self.assertEqual(payment.paid_to, self.fixture["bank_gl"])
        self.assertEqual(flt(payment.received_amount, 2), 127.43)

    def test_bank_existing_payment_is_matched_without_creating_another(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import (
            get_payment_entry,
        )

        sales = self.invoice("Sales Invoice", 139.67, submitted=True)
        payment = get_payment_entry(
            "Sales Invoice", sales.name, bank_account=self.fixture["bank_gl"]
        )
        payment.update(
            {
                "posting_date": today(),
                "reference_date": today(),
                "reference_no": self.marker,
                "bank_account": self.fixture["bank_account"],
            }
        )
        payment.insert()
        payment.submit()
        line = self.bank_line(sales.grand_total, sales)
        payments_before = frappe.db.count("Payment Entry")
        reviewed, frozen = self.freeze_bank(line)
        self.assertEqual(
            (reviewed["kind"], reviewed["voucher"]), ("match", payment.name)
        )
        request = str(uuid.uuid4())
        result = bank_review.approve(frozen["preview_id"], request)
        self.assertTrue(result["results"][0]["ok"], result["results"])
        self.assertEqual(result, bank_review.approve(frozen["preview_id"], request))
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before)
        line.reload()
        self.assertEqual(flt(line.unallocated_amount, 2), 0)
        self.assertEqual(
            [row.payment_entry for row in line.payment_entries], [payment.name]
        )

    def test_bank_changed_preview_books_nothing(self):
        sales = self.invoice("Sales Invoice", 151.19, submitted=True)
        line = self.bank_line(sales.grand_total, sales)
        _reviewed, frozen = self.freeze_bank(line)
        line.db_set(
            "description", f"Updated bank description for {sales.name} {self.marker}"
        )
        payments_before = frappe.db.count("Payment Entry")
        with self.assertRaises(frappe.ValidationError):
            bank_review.approve(frozen["preview_id"], str(uuid.uuid4()))
        self.assert_unused(frozen["preview_id"])
        self.assertEqual(frappe.db.count("Payment Entry"), payments_before)
        sales.reload()
        line.reload()
        self.assertEqual(flt(sales.outstanding_amount, 2), 151.19)
        self.assertEqual(flt(line.unallocated_amount, 2), 151.19)

    def test_billing_approval_submits_exact_documents_holds_bill_and_replays(self):
        run, project, sales, bill = self.prepared_run()
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], company=self.company
        )
        reviewed = frozen["snapshot"]["projects"][0]
        self.assertEqual(reviewed["sales_invoice"], sales.name)
        self.assertEqual(reviewed["engineer_bills"], [bill.name])
        self.assertFalse(reviewed["email_on_approval"])
        request = str(uuid.uuid4())
        result = billing_api.approve_billing(frozen["preview_id"], request)
        self.assertEqual(
            result["projects"][0]["status"], "approved", result["projects"]
        )
        self.assertEqual(result["projects"][0]["submitted_bills"], [bill.name])
        ledger_count = frappe.db.count(
            "GL Entry", {"voucher_no": ["in", [sales.name, bill.name]]}
        )
        self.assertGreater(ledger_count, 0)
        self.assertEqual(
            result, billing_api.approve_billing(frozen["preview_id"], request)
        )
        self.assertEqual(
            frappe.db.count(
                "GL Entry", {"voucher_no": ["in", [sales.name, bill.name]]}
            ),
            ledger_count,
        )
        self.assertEqual(
            frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 1
        )
        run.reload()
        sales.reload()
        bill.reload()
        self.assertEqual(
            (run.status, run.items[0].item_status), ("Approved", "approved")
        )
        self.assertEqual((sales.docstatus, bill.docstatus), (1, 1))
        self.assertTrue(bill.on_hold)
        self.assertEqual(bill.client_invoice, sales.name)
        self.assertEqual(
            frappe.db.count(
                "Communication",
                {
                    "reference_doctype": "Sales Invoice",
                    "reference_name": sales.name,
                    "communication_medium": "Email",
                    "sent_or_received": "Sent",
                },
            ),
            0,
        )

    def test_billing_changed_invoice_requires_review_and_submits_nothing(self):
        run, project, sales, bill = self.prepared_run(359.71, 253.29)
        frozen = billing_api.preview_billing_approval(
            run.name, [project.name], company=self.company
        )
        sales.reload()
        sales.items[0].rate = 401.83
        sales.save()
        with self.assertRaises(frappe.ValidationError):
            billing_api.approve_billing(frozen["preview_id"], str(uuid.uuid4()))
        self.assert_unused(frozen["preview_id"])
        sales.reload()
        bill.reload()
        run.reload()
        self.assertEqual((sales.docstatus, bill.docstatus), (0, 0))
        self.assertEqual(
            (run.status, run.items[0].item_status), ("Prepared", "pending")
        )
        self.assertEqual(
            frappe.db.count(
                "GL Entry", {"voucher_no": ["in", [sales.name, bill.name]]}
            ),
            0,
        )
