"""The Start Here page: created on migrate, the default landing page, and counts scoped to roles."""

import json
import unittest
from unittest.mock import patch

import frappe

from books_assistant import start_page


class TestStartPage(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    def test_ensure_creates_the_page_once(self):
        start_page.ensure()
        start_page.ensure()
        ws = frappe.get_doc("Workspace", start_page.WORKSPACE)
        self.assertEqual((ws.public, ws.module), (1, None))  # no module, so everyone can see it
        blocks = [b["data"].get("custom_block_name") for b in json.loads(ws.content) if b["type"] == "custom_block"]
        self.assertEqual(blocks, [start_page.BLOCK])
        self.assertEqual(frappe.db.count("Custom HTML Block", {"name": start_page.BLOCK}), 1)
        self.assertIn("books_assistant.start_page.counts", frappe.db.get_value("Custom HTML Block", start_page.BLOCK, "script"))

    def test_default_landing_page_but_own_choice_kept(self):
        user = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User", "name": ["!=", "Administrator"]},
                              pluck="name", limit=2)
        frappe.db.set_value("User", user[0], "default_workspace", None)
        frappe.db.set_value("User", user[1], "default_workspace", "Accounting")
        start_page.ensure()
        self.assertEqual(frappe.db.get_value("User", user[0], "default_workspace"), start_page.WORKSPACE)
        self.assertEqual(frappe.db.get_value("User", user[1], "default_workspace"), "Accounting")

        new = frappe.get_doc({"doctype": "User", "email": "start.page.test@example.com", "first_name": "Start",
                              "send_welcome_email": 0})
        start_page.default_for_new_user(new)
        self.assertEqual(new.default_workspace, start_page.WORKSPACE)

    def test_counts_follow_roles(self):
        with patch("frappe.get_roles", return_value=["Employee"]):
            self.assertNotIn("bills_to_pay", start_page.counts())
            self.assertNotIn("bank_lines", start_page.counts())
        with patch("frappe.get_roles", return_value=["Accounts Manager"]):
            out = start_page.counts()
            self.assertTrue({"bank_lines", "bills_to_pay", "held_bills", "unpaid_invoices"} <= set(out))
        with patch("frappe.get_roles", return_value=["Billing Approver"]):
            out = start_page.counts()
            self.assertIn("unpaid_invoices", out)
            self.assertNotIn("bills_to_pay", out)
