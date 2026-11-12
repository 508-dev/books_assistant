"""Real accounting role/User Permission boundaries on the disposable synthetic site."""

import unittest
import uuid

import frappe
from frappe.utils import today

from books_assistant import bank_review, pay_supplier
from books_assistant.tests.synthetic_site import seed_minimal_site
from books_assistant.tests import test_finance_synthetic as synthetic


class TestBankSupplierPermissions(unittest.TestCase):
    invoice = synthetic.TestSyntheticFinanceTrajectories.invoice

    def setUp(self):
        if not frappe.conf.get("books_assistant_synthetic_site"):
            self.skipTest("Requires books_assistant_synthetic_site=1 on a disposable site")
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True
        frappe.flags.books_assistant_locked_documents = set()
        self.fixture = seed_minimal_site()
        self.company = self.fixture["company"]
        self.marker = f"synthetic-permission-{uuid.uuid4()}"
        self.actor = frappe.get_doc({
            "doctype": "User", "email": f"synthetic-{uuid.uuid4()}@example.test",
            "first_name": "Synthetic finance operator", "send_welcome_email": 0,
            "roles": [{"role": "Accounts Manager"}],
        }).insert(ignore_permissions=True)

    def tearDown(self):
        if not hasattr(self, "fixture"):
            return
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.flags.books_assistant_locked_documents = set()
        frappe.clear_last_message()

    def allow(self, doctype, name, applicable_for=None):
        fields = {"doctype": "User Permission", "user": self.actor.name,
                  "allow": doctype, "for_value": name,
                  "apply_to_all_doctypes": 0 if applicable_for else 1}
        if applicable_for:
            fields["applicable_for"] = applicable_for
        frappe.get_doc(fields).insert(ignore_permissions=True)

    def act(self):
        frappe.clear_cache(user=self.actor.name)
        frappe.set_user(self.actor.name)

    def line(self, amount, description, *, money_in=False):
        doc = frappe.get_doc({
            "doctype": "Bank Transaction", "date": today(), "bank_account": self.fixture["bank_account"],
            "currency": "USD", "description": description,
            "deposit": amount if money_in else 0, "withdrawal": 0 if money_in else amount,
        }).insert()
        doc.submit()
        return doc

    def visible_customer(self):
        customer = frappe.copy_doc(frappe.get_doc("Customer", self.fixture["customer"]))
        customer.customer_name = self.marker
        return customer.insert()

    def test_hidden_named_invoice_blocks_only_affected_line_and_safe_approval_still_previews(self):
        visible_customer = self.visible_customer()
        hidden = self.invoice("Sales Invoice", 121.23, submitted=True)
        visible = self.invoice("Sales Invoice", 145.67)
        visible.customer = visible_customer.name
        visible.save()
        visible.submit()
        hidden_line = self.line(hidden.grand_total, f"Payment for {hidden.name}", money_in=True)
        visible_line = self.line(visible.grand_total, f"Payment for {visible.name}", money_in=True)
        self.allow("Customer", visible_customer.name)
        self.act()
        self.assertFalse(frappe.has_permission("Sales Invoice", "read", hidden.name))
        rows = {row["bank_transaction"]: row for row in bank_review.get_queue(
            bank_account=self.fixture["bank_account"], company=self.company,
        )}
        self.assertEqual(rows[hidden_line.name]["kind"], "none")
        self.assertNotIn(hidden.name, rows[hidden_line.name]["summary"])
        self.assertNotIn("invoices", rows[hidden_line.name])
        self.assertEqual(rows[visible_line.name]["kind"], "receipt")
        frozen = bank_review.preview_approval(
            [visible_line.name], {visible_line.name: rows[visible_line.name]["fingerprint"]},
            company=self.company, bank_account=self.fixture["bank_account"],
        )
        self.assertEqual([row["bank_transaction"] for row in frozen["snapshot"]["rows"]], [visible_line.name])

    def test_hidden_rule_account_blocks_only_matching_line(self):
        visible = self.fixture["expense_account"]
        hidden = frappe.get_all("Account", filters={
            "company": self.company, "root_type": "Expense", "is_group": 0, "name": ["!=", visible],
        }, pluck="name", limit=1)[0]
        self.allow("Account", self.fixture["bank_gl"], applicable_for="Account")
        self.allow("Account", visible, applicable_for="Account")
        lines = []
        for label, account, amount in (("HIDDEN", hidden, 18.31), ("VISIBLE", visible, 22.47)):
            keyword = f"SYNTHETIC{label}{uuid.uuid4().hex}"
            frappe.get_doc({
                "doctype": "Bank Rule", "rule_name": keyword,
                "description_contains": keyword, "direction": "Money out", "account": account,
            }).insert()
            lines.append(self.line(amount, keyword))
        self.act()
        self.assertFalse(frappe.has_permission("Account", "read", hidden))
        rows = {row["bank_transaction"]: row for row in bank_review.get_queue(
            bank_account=self.fixture["bank_account"], company=self.company,
        )}
        self.assertEqual(rows[lines[0].name]["kind"], "none")
        self.assertNotIn(hidden, rows[lines[0].name]["summary"])
        self.assertNotIn("rule", rows[lines[0].name])
        self.assertEqual(rows[lines[1].name]["kind"], "rule")
        self.assertEqual(rows[lines[1].name]["account"], visible)

    def test_inaccessible_company_bank_still_prevents_duplicate_payout(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        base = frappe.get_doc("Account", self.fixture["bank_gl"])
        account = frappe.get_doc({
            "doctype": "Account", "account_name": self.marker,
            "company": self.company, "parent_account": base.parent_account,
            "is_group": 0, "account_type": "Bank", "account_currency": "USD",
        }).insert()
        original_bank = frappe.get_doc("Bank Account", self.fixture["bank_account"])
        bank = frappe.get_doc({
            "doctype": "Bank Account", "account_name": self.marker, "bank": original_bank.bank,
            "company": self.company, "is_company_account": 1,
            "account": account.name, "bank_account_no": "1111111111",
        }).insert()
        bill = self.invoice("Purchase Invoice", 97.83, submitted=True)
        payment = get_payment_entry("Purchase Invoice", bill.name, bank_account=account.name)
        payment.update({"reference_no": self.marker, "reference_date": today(), "bank_account": bank.name})
        payment.insert()
        payment.submit()
        # A second open bill makes a duplicate payout otherwise look actionable.
        another_bill = self.invoice("Purchase Invoice", 97.83, submitted=True)
        line = self.line(97.83, f"Payment for {another_bill.name}")
        self.allow("Bank Account", self.fixture["bank_account"])
        self.act()
        self.assertFalse(frappe.has_permission("Bank Account", "read", bank.name))
        rows = {row["bank_transaction"]: row for row in bank_review.get_queue(
            bank_account=self.fixture["bank_account"], company=self.company,
        )}
        self.assertEqual(rows[line.name]["kind"], "none")
        for secret in (payment.name, bank.name, account.name):
            self.assertNotIn(secret, rows[line.name]["summary"])
        self.assertNotIn("invoices", rows[line.name])

    def test_hidden_client_invoice_on_held_bill_does_not_block_eligible_supplier_payment(self):
        visible_customer = self.visible_customer()
        hidden = self.invoice("Sales Invoice", 166.29, submitted=True)
        held = self.invoice("Purchase Invoice", 117.43, submitted=True)
        held.client_invoice = hidden.name
        held.save()
        held.block_invoice(hold_comment=f"Waiting for private invoice {hidden.name}")
        payable = self.invoice("Purchase Invoice", 162.57, submitted=True)
        self.allow("Customer", visible_customer.name)
        self.act()
        self.assertTrue(frappe.has_permission("Purchase Invoice", "read", held.name))
        self.assertFalse(frappe.has_permission("Sales Invoice", "read", hidden.name))
        viewed = pay_supplier.preview(
            self.fixture["supplier"], [payable.name], company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        explanation = next(row for row in viewed["held"] if row.name == held.name)
        self.assertIsNone(explanation.client_invoice)
        self.assertNotIn(hidden.name, explanation.hold_comment)
        self.assertEqual(viewed["to_pay"], 162.57)
        frozen = pay_supplier.preview_preparation(
            self.fixture["supplier"], [payable.name], company=self.company,
            bank_account=self.fixture["bank_gl"],
        )
        result = pay_supplier.prepare(frozen["preview_id"], str(uuid.uuid4()))
        self.assertEqual([row["reference_name"] for row in result["payment_entry"]["references"]], [payable.name])
