"""Approval state-machine tests without a site; DB locking is covered by Bench tests."""

import copy
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class Row(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    __setattr__ = dict.__setitem__


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.records = {}
        self.trace = []
        self.now = datetime(2026, 11, 13, 12)
        self.sequence = 0
        frappe = self.frappe = ModuleType("frappe")
        frappe.session = SimpleNamespace(user="accountant@example.test")
        frappe.flags = SimpleNamespace()
        frappe.local = SimpleNamespace(response={})
        frappe.ValidationError = ValueError
        frappe.PermissionError = PermissionError
        frappe.parse_json = json.loads
        frappe.throw = lambda message, exception=ValueError: (_ for _ in ()).throw(
            exception(message)
        )
        frappe.generate_hash = self.key
        frappe.get_all = Mock(return_value=[Row(name="Co-op", default_currency="USD")])
        self.denied = set()
        test = self

        class Doc(Row):
            def insert(self, **kwargs):
                self.name = (
                    self.get("preview_key") or self.get("request_key") or self.name
                )
                record_key = (self.doctype, self.name)
                if record_key in test.records:
                    raise ValueError("duplicate")
                test.records[record_key] = self
                return self

            def check_permission(self, permission):
                if (self.doctype, self.name, permission) in test.denied:
                    raise PermissionError("not allowed")

            def db_set(self, field, value=None, **kwargs):
                self.update(field if isinstance(field, dict) else {field: value})

        self.Doc = Doc

        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, dict):
                return Doc(doctype)
            self.trace.append(("get_doc", doctype, name, kwargs))
            return self.records[(doctype, name)]

        frappe.get_doc = get_doc
        self.records[("Company", "Co-op")] = Doc(
            doctype="Company", name="Co-op", default_currency="USD"
        )
        snapshots = {}

        def sql(query, parameters):
            self.trace.append(("sql", query, parameters))
            doctype = re.search(r"`tab(.+?)`", query).group(1)
            return (
                [(parameters[0],)] if (doctype, parameters[0]) in self.records else []
            )

        def exists(doctype, filters):
            if isinstance(filters, str):
                return (doctype, filters) in self.records
            return False

        def savepoint(name):
            snapshots[name] = {
                key: dict(value) for key, value in copy.deepcopy(self.records).items()
            }

        def rollback(save_point):
            self.records = {
                key: Doc(value) for key, value in snapshots[save_point].items()
            }

        frappe.db = SimpleNamespace(
            sql=sql,
            exists=Mock(side_effect=exists),
            savepoint=savepoint,
            rollback=rollback,
        )
        utils = ModuleType("frappe.utils")
        utils.now_datetime = lambda: self.now
        utils.get_datetime = lambda value: (
            datetime.fromisoformat(value) if isinstance(value, str) else value
        )
        spec = importlib.util.spec_from_file_location(
            "integration_subject",
            Path(__file__).parents[1] / "books_assistant/integration.py",
        )
        self.subject = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
            spec.loader.exec_module(self.subject)
        self.scope = {"company": "Co-op", "currency": "USD"}
        self.payload = {"invoices": ["INV-1"]}
        self.snapshot = {"amount": 100, "invoice": "INV-1", "modified": "v1"}
        self.authorize = Mock()
        self.build = Mock(side_effect=lambda payload, scope: self.snapshot)
        self.perform = Mock(return_value={"documents": ["PAY-1"]})

    def key(self, length):
        self.sequence += 1
        return str(self.sequence).zfill(length)

    def preview(self):
        return self.subject.create_preview(
            "pay", self.scope, self.payload, self.snapshot
        )["preview_id"]

    def consume(self, preview_id, request_id="request-1", **kwargs):
        return self.subject.consume_preview(
            preview_id,
            request_id,
            "pay",
            self.build,
            self.perform,
            authorize=self.authorize,
            **kwargs,
        )

    def test_exact_retry_returns_original_result_after_expiry_without_executing(self):
        preview = self.preview()
        first = self.consume(preview)
        self.now += timedelta(hours=1)
        self.snapshot = {"amount": 0}
        again = self.consume(preview)
        self.assertEqual(first, again)
        self.perform.assert_called_once_with(self.payload, self.scope)
        self.assertEqual(self.authorize.call_count, 2)
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(first["api_version"], 1)
        self.assertTrue(first["operation_id"])

    def test_changed_amount_or_document_version_prevents_execution(self):
        for field in ("amount", "modified"):
            with self.subTest(field=field):
                preview = self.preview()
                self.snapshot = {**self.snapshot, field: "changed"}
                with self.assertRaisesRegex(ValueError, "reviewed data changed"):
                    self.consume(preview)
                self.assertEqual(
                    self.frappe.local.response["error_code"], "PREVIEW_STALE"
                )
                self.perform.assert_not_called()
                self.assertFalse(any(key[0] == "API Request" for key in self.records))

    def test_expired_unused_preview_is_rejected(self):
        preview = self.preview()
        self.now += timedelta(minutes=11)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.consume(preview)
        self.perform.assert_not_called()

    def test_actor_and_action_are_bound_to_preview(self):
        preview = self.preview()
        self.frappe.session.user = "other@example.test"
        with self.assertRaises(PermissionError):
            self.consume(preview)
        self.perform.assert_not_called()
        self.frappe.session.user = "accountant@example.test"
        self.records[("API Preview", preview)].action = "another-action"
        with self.assertRaises(PermissionError):
            self.consume(preview)

    def test_request_id_cannot_be_reused_for_another_preview(self):
        self.consume(self.preview())
        with self.assertRaisesRegex(ValueError, "different operation"):
            self.consume(self.preview())
        self.assertEqual(self.perform.call_count, 1)

    def test_preview_cannot_be_consumed_with_another_request_id(self):
        preview = self.preview()
        self.consume(preview)
        with self.assertRaisesRegex(ValueError, "already been used"):
            self.consume(preview, request_id="request-2")
        self.assertEqual(self.perform.call_count, 1)

    def test_permission_revocation_blocks_replay(self):
        preview = self.preview()
        self.consume(preview)
        self.authorize.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.consume(preview)
        self.assertEqual(self.perform.call_count, 1)

    def test_ledger_and_effects_rollback_together_when_execution_fails(self):
        preview = self.preview()

        def partial_then_fail(payload, scope):
            self.records[("Payment Entry", "PAY-1")] = self.Doc(
                doctype="Payment Entry", name="PAY-1"
            )
            raise ValueError("ledger refused")

        self.perform.side_effect = partial_then_fail
        with self.assertRaisesRegex(ValueError, "ledger refused"):
            self.consume(preview)
        self.assertNotIn(("Payment Entry", "PAY-1"), self.records)
        self.assertFalse(any(key[0] == "API Request" for key in self.records))
        self.assertFalse(self.records[("API Preview", preview)].get("consumed_request"))
        self.perform.side_effect = None
        self.assertTrue(self.consume(preview)["operation_id"])

    def test_locks_are_deterministic_and_reads_after_lock_are_current(self):
        self.subject.lock_documents([("Company", "Co-op"), ("Company", "Co-op")])
        self.subject.check_document("Company", "Co-op")
        sql = [entry for entry in self.trace if entry[0] == "sql"]
        self.assertEqual(len(sql), 1)
        self.assertIn("for update", sql[0][1])
        self.assertEqual(self.trace[-1][-1], {"for_update": True})

    def test_selection_never_expands_empty_or_malformed_inputs(self):
        for value in (
            None,
            [],
            "[]",
            {},
            True,
            "null",
            [""],
            [1],
            ["INV-1", "INV-1"],
            ["INV-1"] * 101,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.subject.selection(value)
        self.assertIsNone(self.subject.selection(None, allow_none=True))
        self.assertEqual(self.subject.selection('["INV-1"]'), ["INV-1"])

    def test_scope_rejects_multiple_companies_and_foreign_currency(self):
        self.frappe.get_all.return_value *= 2
        with self.assertRaisesRegex(ValueError, "exactly one company"):
            self.subject.resolve_scope()
        self.frappe.get_all.return_value = [Row(name="Co-op", default_currency="USD")]
        self.records[("Company", "Co-op")].default_currency = "EUR"
        with self.assertRaisesRegex(ValueError, "USD"):
            self.subject.resolve_scope()
        self.records[("Company", "Co-op")].default_currency = "USD"
        self.frappe.db.exists.side_effect = lambda dt, filters: dt == "Purchase Invoice"
        with self.assertRaisesRegex(ValueError, "foreign-currency"):
            self.subject.resolve_scope()

    def test_authentication_scope_and_record_permissions_fail_closed(self):
        self.frappe.session.user = "Guest"
        with self.assertRaises(PermissionError):
            self.subject.resolve_scope()
        self.frappe.session.user = "accountant@example.test"
        with self.assertRaises(PermissionError):
            self.subject.resolve_scope("Other Company")
        self.denied.add(("Company", "Co-op", "read"))
        with self.assertRaises(PermissionError):
            self.subject.resolve_scope()

    def test_malformed_execution_inputs_and_missing_authorizer_do_no_work(self):
        preview = self.preview()
        for request_id in (None, "", " ", {}, "x" * 129):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                self.consume(preview, request_id=request_id)
        with self.assertRaisesRegex(ValueError, "authorization callback"):
            self.subject.consume_preview(
                preview, "request-1", "pay", self.build, self.perform
            )
        self.perform.assert_not_called()

    def test_invalid_operation_result_rolls_back_the_ledger(self):
        preview = self.preview()
        self.perform.return_value = None
        with self.assertRaisesRegex(ValueError, "structured result"):
            self.consume(preview)
        self.assertFalse(any(key[0] == "API Request" for key in self.records))
        self.assertFalse(self.records[("API Preview", preview)].get("consumed_request"))

    def test_frappe_dict_flags_missing_attributes_return_none(self):
        class Flags(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        self.frappe.flags = Flags()
        self.subject.check_document("Company", "Co-op")


if __name__ == "__main__":
    unittest.main()
