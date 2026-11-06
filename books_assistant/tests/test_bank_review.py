"""Runs against the site's own data (a copy of production). Everything is rolled back after each test."""

import unittest

import frappe
from frappe.utils import flt

from books_assistant import bank_review as br
from books_assistant import pay_supplier as ps


def bank_line(bank_account, amount, description, date="2026-11-05", money_in=False):
    doc = frappe.get_doc({
        "doctype": "Bank Transaction", "date": date, "bank_account": bank_account, "currency": "USD",
        "description": description, "deposit": amount if money_in else 0, "withdrawal": 0 if money_in else amount,
    }).insert()
    doc.submit()
    return doc.name


def reconciled(name):
    return flt(frappe.db.get_value("Bank Transaction", name, "unallocated_amount")) < 0.01


class TestBankReview(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def suggest(self, name):
        return br.Context().suggest(br.bank_line(name))

    def test_queue_is_well_formed(self):
        queue = br.get_queue()
        self.assertEqual(len(queue), len({s["bank_transaction"] for s in queue}))
        for s in queue:
            self.assertIn(s["kind"], ("match", "receipt", "payout", "transfer", "rule", "none"))
            self.assertIn(s["confidence"], ("high", "check", "none"))
            self.assertTrue(s["summary"])

    def test_receipt_naming_an_invoice(self):
        invoice = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "outstanding_amount": [">", 100]},
                                 fields=["name", "outstanding_amount"], limit=1)[0]
        line = bank_line("Novo - Novo", invoice.outstanding_amount, f"Payment for {invoice.name.replace('-', '')}",
                         money_in=True)
        s = self.suggest(line)
        self.assertEqual((s["kind"], s["confidence"]), ("receipt", "high"))
        br.execute(s)
        self.assertTrue(reconciled(line))
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice.name, "outstanding_amount")), 0)

    def test_existing_payment_is_matched_not_recreated(self):
        line = bank_line("Novo - Novo", 4321.09, "ACME LTD", money_in=True)
        invoice = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "outstanding_amount": [">", 0]},
                                 fields=["name"], limit=1)[0]
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        pe = get_payment_entry("Sales Invoice", invoice.name, party_amount=4321.09, bank_account="Novo - 5")
        pe.update({"posting_date": "2026-11-04", "reference_no": "x", "reference_date": "2026-11-04"})
        pe.insert()
        pe.submit()
        s = self.suggest(line)
        self.assertEqual((s["kind"], s["voucher"]), ("match", pe.name))
        br.execute(s)
        self.assertTrue(reconciled(line))

    def test_transfer_links_both_sides(self):
        out_line = bank_line("Novo - Novo", 1234.56, "508.dev LLC")
        in_line = bank_line("Chase - Chase", 1234.56, "ORIG CO NAME:508.dev LLC ORIG ID:1", money_in=True)
        s = self.suggest(in_line)
        self.assertEqual(s["kind"], "transfer")
        self.assertEqual((s["withdrawal_line"], s["deposit_line"]), (out_line, in_line))
        br.execute(s)
        self.assertTrue(reconciled(out_line) and reconciled(in_line))

    def test_rule_books_a_journal_entry(self):
        rule = frappe.get_doc({"doctype": "Bank Rule", "rule_name": "Testvendor", "description_contains": "TESTVENDOR",
                               "direction": "Money out", "account": "Software Subscriptions - 5"}).insert()
        line = bank_line("Novo - Novo", 12.34, "TESTVENDOR 9MLINO6F")
        s = self.suggest(line)
        self.assertEqual((s["kind"], s["rule"]), ("rule", rule.name))
        br.execute(s)
        self.assertTrue(reconciled(line))
        self.assertEqual(frappe.db.get_value("Bank Rule", rule.name, "times_used"), 1)

    def test_rules_cannot_book_to_parties(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({"doctype": "Bank Rule", "rule_name": "Bad", "description_contains": "X",
                            "account": "1310 - Debtors - 5"}).insert()

    def test_learning_creates_rules_and_names(self):
        message = br.learn_from_history()
        self.assertIn("rules", message)
        self.assertTrue(frappe.db.exists("Bank Rule", {"description_contains": "GITHUB", "direction": "Money out"}))


class TestPaySupplier(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def test_prepares_an_unsaved_payment_for_what_is_owed(self):
        supplier = frappe.db.get_value("Purchase Invoice", {"docstatus": 1, "outstanding_amount": [">", 1], "is_return": 0},
                                       "supplier")
        preview = ps.preview(supplier)
        result = ps.prepare(supplier)
        payment = result["payment_entry"]
        self.assertEqual(payment["__islocal"], 1)
        self.assertAlmostEqual(flt(payment["paid_amount"]), flt(preview["to_pay"]), places=2)
        self.assertEqual(len(payment["references"]), len(ps.open_bills(supplier)))
        self.assertIn("payout", payment["reference_no"])
        self.assertIn(supplier, payment["reference_no"])
