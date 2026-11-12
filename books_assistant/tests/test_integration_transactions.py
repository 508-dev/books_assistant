"""Real DB approval tests. Enable ONLY on a disposable single-company USD site.

These tests commit fixtures to let separate database connections see them.
Set site config books_assistant_transaction_tests=1 explicitly before running.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import uuid

import frappe

from books_assistant.integration import consume_preview, create_preview, resolve_scope


class TestIntegrationTransactions(unittest.TestCase):
    def setUp(self):
        if not frappe.conf.get("books_assistant_transaction_tests"):
            self.skipTest(
                "Requires an explicitly enabled disposable transaction-test site"
            )
        frappe.set_user("Administrator")
        self.site = frappe.local.site
        self.sites_path = frappe.local.sites_path
        self.marker = f"books-assistant-transaction-{uuid.uuid4()}"
        self.scope = resolve_scope()
        self.preview_ids = []
        self.request_id = str(uuid.uuid4())

    def preview(self):
        result = create_preview(
            "test.transaction", self.scope, {"marker": self.marker}, {"amount": 100}
        )
        self.preview_ids.append(result["preview_id"])
        frappe.db.commit()
        return result["preview_id"]

    def worker(self, preview_id, barrier, fail=False):
        frappe.init(site=self.site, sites_path=self.sites_path)
        frappe.connect()
        frappe.set_user("Administrator")
        try:
            # Force an established MVCC snapshot before either thread starts
            # executing. The ledger must still use current locking reads.
            frappe.db.sql("select name from `tabAPI Request` limit 1")

            def authorize(payload, scope):
                frappe.get_doc("Company", scope["company"]).check_permission("read")

            def perform(payload, scope):
                todo = frappe.get_doc(
                    {"doctype": "ToDo", "description": payload["marker"]}
                ).insert()
                if fail:
                    frappe.throw("Intentional test rollback")
                return {"document": todo.name}

            barrier.wait(timeout=10)
            result = consume_preview(
                preview_id,
                self.request_id,
                "test.transaction",
                lambda payload, scope: {"amount": 100},
                perform,
                authorize=authorize,
            )
            frappe.db.commit()
            return result
        except Exception:
            frappe.db.rollback()
            raise
        finally:
            frappe.destroy()

    def tearDown(self):
        if not hasattr(self, "marker"):
            return
        # Direct test cleanup avoids circular Preview/Request Link validation.
        frappe.db.rollback()
        if self.preview_ids:
            frappe.db.delete("API Request", {"preview": ["in", self.preview_ids]})
            frappe.db.delete("API Preview", {"name": ["in", self.preview_ids]})
        frappe.db.delete("ToDo", {"description": self.marker})
        frappe.db.commit()

    def test_concurrent_duplicate_request_executes_once(self):
        preview = self.preview()
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.worker, preview, barrier) for _ in range(2)]
            results = [future.result(timeout=60) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(frappe.db.count("ToDo", {"description": self.marker}), 1)
        self.assertEqual(
            frappe.db.count("API Request", {"preview": preview, "status": "Completed"}),
            1,
        )

    def test_same_request_id_cannot_execute_two_distinct_previews(self):
        previews = [self.preview(), self.preview()]
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.worker, preview, barrier) for preview in previews
            ]
            results, errors = [], []
            for future in futures:
                try:
                    results.append(future.result(timeout=60))
                except frappe.ValidationError as error:
                    errors.append(str(error))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("different operation", errors[0])
        self.assertEqual(frappe.db.count("ToDo", {"description": self.marker}), 1)

    def test_execution_and_ledger_rollback_together(self):
        preview = self.preview()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.worker, preview, threading.Barrier(1), True)
            with self.assertRaises(frappe.ValidationError):
                future.result(timeout=60)
        self.assertEqual(frappe.db.count("ToDo", {"description": self.marker}), 0)
        self.assertEqual(frappe.db.count("API Request", {"preview": preview}), 0)
        self.assertFalse(
            frappe.db.get_value("API Preview", preview, "consumed_request")
        )

    def test_ordinary_accounting_user_cannot_crud_internal_records(self):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"{uuid.uuid4()}@example.test",
                "first_name": "Finance test",
                "send_welcome_email": 0,
                "roles": [{"role": "Accounts Manager"}],
            }
        ).insert(ignore_permissions=True)
        try:
            frappe.set_user(user.name)
            for doctype in ("API Preview", "API Request"):
                for permission in ("read", "create", "write", "delete"):
                    self.assertFalse(
                        frappe.has_permission(doctype, permission),
                        (doctype, permission),
                    )
        finally:
            frappe.set_user("Administrator")
            frappe.db.rollback()
