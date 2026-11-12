"""Real REPEATABLE READ races against ordinary ERP draft submission.

Only run with books_assistant_synthetic_site=1 on a disposable site. Fixtures
are committed so an independent ordinary ERP connection can submit the draft;
cleanup cancels/deletes only documents created by this test.
"""

from concurrent.futures import ThreadPoolExecutor
import unittest
import uuid

import frappe
from frappe.utils import flt, today

from books_assistant import bank_review
from books_assistant.tests.synthetic_site import seed_minimal_site
from books_assistant.tests import test_finance_synthetic as synthetic


class TestBankDiscoveryTransactions(unittest.TestCase):
    invoice = synthetic.TestSyntheticFinanceTrajectories.invoice
    bank_line = synthetic.TestSyntheticFinanceTrajectories.bank_line
    freeze_bank = synthetic.TestSyntheticFinanceTrajectories.freeze_bank

    def setUp(self):
        if not frappe.conf.get("books_assistant_synthetic_site"):
            self.skipTest("Requires books_assistant_synthetic_site=1 on a disposable site")
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True
        frappe.flags.books_assistant_locked_documents = set()
        self.fixture = seed_minimal_site()
        self.company = self.fixture["company"]
        self.site, self.sites_path = frappe.local.site, frappe.local.sites_path
        self.marker = f"synthetic-bank-race-{uuid.uuid4()}"
        self.created = []
        self.previews = []
        for doctype in ("Payment Entry", "Journal Entry"):
            hooks = frappe.get_hooks("doc_events").get(doctype, {})
            for event in ("before_validate", "before_update_after_submit", "before_cancel"):
                self.assertIn("books_assistant.bank_review.serialize_voucher_write", hooks.get(event, []))

    def track(self, doc):
        self.created.append((doc.doctype, doc.name))
        return doc

    def submit_ordinary_draft(self, doctype, name):
        frappe.init(site=self.site, sites_path=self.sites_path)
        frappe.connect()
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True
        try:
            # The normal Document.submit path locks its pre-existing voucher in
            # check_if_latest before running the company serialization hook.
            doc = frappe.get_doc(doctype, name)
            doc.submit()
            frappe.db.commit()
            return doc.name
        except Exception:
            frappe.db.rollback()
            raise
        finally:
            frappe.destroy()

    def exercise(self, doctype, amount):
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        sales = self.track(self.invoice("Sales Invoice", amount, submitted=True))
        if doctype == "Payment Entry":
            draft = get_payment_entry("Sales Invoice", sales.name, bank_account=self.fixture["bank_gl"])
            draft.update({"posting_date": today(), "reference_date": today(), "reference_no": self.marker,
                          "bank_account": self.fixture["bank_account"]})
        else:
            draft = frappe.get_doc({
                "doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": self.company,
                "posting_date": today(), "user_remark": self.marker,
                "accounts": [
                    {"account": self.fixture["bank_gl"], "debit_in_account_currency": amount,
                     "cost_center": self.fixture["cost_center"]},
                    {"account": sales.debit_to, "party_type": "Customer", "party": self.fixture["customer"],
                     "credit_in_account_currency": amount, "reference_type": "Sales Invoice",
                     "reference_name": sales.name, "cost_center": self.fixture["cost_center"]},
                ],
            })
        self.track(draft.insert())
        line = self.track(self.bank_line(amount, sales))
        frappe.db.commit()
        reviewed, frozen = self.freeze_bank(line)
        self.assertEqual(reviewed["kind"], "receipt")
        self.previews.append(frozen["preview_id"])
        frappe.db.commit()
        frappe.db.sql("set session transaction isolation level repeatable read")
        self.assertEqual(frappe.db.get_value(doctype, draft.name, "docstatus"), 0)
        counts = {kind: frappe.db.count(kind) for kind in ("Payment Entry", "Journal Entry")}
        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(self.submit_ordinary_draft, doctype, draft.name).result(timeout=60), draft.name)
        # Prove this connection's established consistent-read snapshot is stale.
        self.assertEqual(frappe.db.get_value(doctype, draft.name, "docstatus"), 0)
        with self.assertRaises(frappe.ValidationError):
            bank_review.approve(frozen["preview_id"], str(uuid.uuid4()))
        frappe.db.rollback()
        self.assertFalse(frappe.db.get_value("API Preview", frozen["preview_id"], "consumed_request"))
        self.assertEqual(frappe.db.count("API Request", {"preview": frozen["preview_id"]}), 0)
        for kind, count in counts.items():
            self.assertEqual(frappe.db.count(kind), count, kind)
        self.assertEqual(frappe.db.get_value(doctype, draft.name, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", sales.name, "outstanding_amount"), 2), 0)
        self.assertEqual(flt(frappe.db.get_value("Bank Transaction", line.name, "unallocated_amount"), 2), amount)
        current, fresh = self.freeze_bank(line)
        self.previews.append(fresh["preview_id"])
        self.assertEqual((current["kind"], current["voucher_type"], current["voucher"]), ("match", doctype, draft.name))
        result = bank_review.approve(fresh["preview_id"], str(uuid.uuid4()))
        self.assertTrue(result["results"][0]["ok"], result["results"])
        self.assertEqual(flt(frappe.db.get_value("Bank Transaction", line.name, "unallocated_amount"), 2), 0)
        for kind, count in counts.items():
            self.assertEqual(frappe.db.count(kind), count, kind)

    def tearDown(self):
        if not hasattr(self, "created"):
            return
        frappe.set_user("Administrator")
        frappe.db.rollback()
        if self.previews:
            frappe.db.delete("API Request", {"preview": ["in", self.previews]})
            frappe.db.delete("API Preview", {"name": ["in", self.previews]})
        for doctype, name in reversed(self.created):
            if not frappe.db.exists(doctype, name):
                continue
            doc = frappe.get_doc(doctype, name)
            if doc.docstatus == 1:
                doc.cancel()
            # Immutable-ledger cancellation keeps dynamically linked rows.
            # Only remove ledger rows of vouchers tracked by this disposable test.
            for ledger in ("Payment Ledger Entry", "GL Entry"):
                frappe.db.delete(ledger, {"voucher_type": doctype, "voucher_no": name})
            frappe.db.delete("Payment Ledger Entry",
                             {"against_voucher_type": doctype, "against_voucher_no": name})
            frappe.delete_doc(doctype, name, ignore_permissions=True)
        frappe.db.commit()
        frappe.flags.books_assistant_locked_documents = set()

    def test_existing_payment_draft_committed_after_consistent_read_is_not_duplicated(self):
        self.exercise("Payment Entry", 601.37)

    def test_existing_journal_draft_committed_after_consistent_read_is_not_duplicated(self):
        self.exercise("Journal Entry", 613.29)
