"""Guarded real-site pre-funding-schema migration assertions; no production data."""

import json
from pathlib import Path

import frappe

from books_assistant.tests.synthetic_site import (
    _guard,
    _financial_snapshot,
    _ledger_snapshot,
)

MANIFEST = "funding_synthetic_migration.json"


def manifest_path():
    return Path(frappe.get_site_path("private", "files", MANIFEST))


def seed_before_funding():
    _guard()
    from books_assistant.tests.test_finance_synthetic import (
        TestSyntheticFinanceTrajectories,
    )
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    for name in ("Bank Funding Snapshot", "Payout Reservation", "Funding Reflection"):
        assert not frappe.db.exists("DocType", name), name
    assert not frappe.get_meta("Automation Settings").has_field(
        "funding_balance_max_age_hours"
    )
    fixture = TestSyntheticFinanceTrajectories()
    fixture.setUp()
    sales = fixture.invoice("Sales Invoice", 321.45, submitted=True)
    bill = fixture.invoice("Purchase Invoice", 123.45, submitted=True)
    payment = get_payment_entry(
        "Purchase Invoice", bill.name, bank_account=fixture.fixture["bank_gl"]
    )
    payment.reference_no, payment.reference_date = fixture.marker, bill.posting_date
    payment.insert()
    names = {
        "Sales Invoice": [sales.name],
        "Purchase Invoice": [bill.name],
        "Payment Entry": [payment.name],
    }
    data = {
        "names": names,
        "documents": _financial_snapshot(names),
        "ledger": _ledger_snapshot(
            [name for group in names.values() for name in group]
        ),
        "print_format": frappe.db.get_single_value(
            "Automation Settings", "invoice_print_format"
        ),
    }
    assert len(data["ledger"]) >= 4, "Real submitted invoices must have GL entries"
    manifest_path().write_text(json.dumps(data, sort_keys=True, default=str))
    frappe.db.commit()
    return {"documents": 3, "ledger_entries": len(data["ledger"])}


def assert_after_funding():
    _guard()
    data = json.loads(manifest_path().read_text())
    assert _financial_snapshot(data["names"]) == data["documents"]
    assert (
        _ledger_snapshot([name for group in data["names"].values() for name in group])
        == data["ledger"]
    )
    assert (
        frappe.db.get_single_value("Automation Settings", "invoice_print_format")
        == data["print_format"]
    )
    for name in ("Bank Funding Snapshot", "Payout Reservation", "Funding Reflection"):
        assert frappe.db.exists("DocType", name), name
        assert frappe.db.table_exists(name), name
    assert frappe.get_meta("Funding Reflection").has_field("resolution")
    assert frappe.get_meta("Funding Reflection").has_field("resolution_reason")
    assert float(
        frappe.db.get_single_value(
            "Automation Settings", "funding_balance_max_age_hours"
        )
    ) == data.get("expected_age", 4)
    return {
        "documents_preserved": 3,
        "ledger_entries_preserved": len(data["ledger"]),
        "funding_schema": "present",
    }


def prepare_repeat():
    _guard()
    data = json.loads(manifest_path().read_text())
    data["expected_age"] = 2
    manifest_path().write_text(json.dumps(data, sort_keys=True))
    frappe.db.set_single_value(
        "Automation Settings", "funding_balance_max_age_hours", 2
    )
    frappe.db.commit()
