"""Source snapshots must respect parent and child field-level read permissions."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_billing_contract as billing_fixtures


class SnapshotFieldPermissionsTests(unittest.TestCase):
    setUp = billing_fixtures.BillingContractTests.setUp

    def source(
        self, *, field_level=0, child_level=None, allowed=(0,), administrator=False
    ):
        self.frappe.session = SimpleNamespace(
            user="Administrator" if administrator else "reviewer@example.test"
        )
        self.frappe.PermissionError = PermissionError
        self.frappe.throw.side_effect = lambda message, error=ValueError: (
            _ for _ in ()
        ).throw(error(message))
        tables = (
            [] if child_level is None else [SimpleNamespace(options="Invoice Item")]
        )
        self.frappe.get_meta = Mock(
            return_value=SimpleNamespace(
                fields=[SimpleNamespace(permlevel=child_level)]
            )
        )
        doc = SimpleNamespace(
            meta=SimpleNamespace(
                fields=[SimpleNamespace(permlevel=field_level)],
                get_table_fields=lambda: tables,
            ),
            get_permlevel_access=Mock(return_value=list(allowed)),
            as_dict=Mock(
                return_value={
                    "name": "DOC-1",
                    "protected_value": "source-value",
                    "__onload": "UI only",
                }
            ),
        )
        self.api.check_document.return_value = doc
        return doc

    def snapshot(self):
        return self.api._snapshot_document(
            "Invoice", "DOC-1", {"company": "Co-op", "currency": "USD"}
        )

    def test_row_permission_cannot_expose_a_parent_field_at_an_unreadable_level(self):
        doc = self.source(field_level=9)
        with self.assertRaises(PermissionError):
            self.snapshot()
        doc.as_dict.assert_not_called()

    def test_row_permission_cannot_expose_an_unreadable_child_table_field(self):
        doc = self.source(child_level=9)
        with self.assertRaises(PermissionError):
            self.snapshot()
        doc.as_dict.assert_not_called()

    def test_full_field_permission_retains_complete_source_values_for_stale_checks(
        self,
    ):
        self.source(field_level=9, child_level=9, allowed=(0, 9))
        self.assertEqual(
            self.snapshot(), {"name": "DOC-1", "protected_value": "source-value"}
        )

    def test_administrator_retains_frappe_full_field_access(self):
        doc = self.source(field_level=9, child_level=9, administrator=True)
        self.assertEqual(self.snapshot()["protected_value"], "source-value")
        doc.get_permlevel_access.assert_not_called()


if __name__ == "__main__":
    unittest.main()
