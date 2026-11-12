"""Read-only finance views for ERP clients, scoped to the authenticated user's records."""

from collections import defaultdict
from datetime import datetime, timezone
from functools import partial

import frappe
from frappe import _
from frappe.model import default_fields
from frappe.utils import add_days, flt, today

from books_assistant.integration import resolve_scope

READERS = ("Accounts Manager", "System Manager", "Billing Approver")
VERSION = 1


def _company(company):
    frappe.only_for(READERS)
    if not isinstance(company, str) or not company.strip():
        frappe.throw(_("Company is required."))
    resolve_scope(company)
    doc = frappe.get_doc("Company", company)
    doc.check_permission("read")
    return doc


def _rows(doctype, filters, fields, unavailable=None):
    # get_list applies document permissions and User Permissions, unlike get_all / raw SQL.
    permitted = set(default_fields) | set(
        frappe.get_meta(doctype).get_permitted_fieldnames(
            user=frappe.session.user, permission_type="read", with_virtual_fields=False
        )
    )
    if not frappe.has_permission(doctype, "read") or not set(fields).issubset(
        permitted
    ):
        if unavailable is not None:
            unavailable.add(doctype)
        return []
    rows = frappe.get_list(
        doctype, filters=filters, fields=fields, limit_page_length=0, order_by="name"
    )
    # Custom has_permission hooks are evaluated on documents, not by get_list.
    return [row for row in rows if frappe.has_permission(doctype, "read", doc=row.name)]


def _totals(rows):
    totals = defaultdict(float)
    for row in rows:
        totals[row.currency] += flt(row.outstanding_amount)
    return [
        {"currency": currency, "amount": flt(amount, 2)}
        for currency, amount in sorted(totals.items())
    ]


@frappe.whitelist(methods=["GET"])
def get_finance_status(company):
    """Return visible work awaiting attention; amounts are never added across currencies."""
    _company(company)
    unavailable = set()
    visible_rows = partial(_rows, unavailable=unavailable)
    accounts = visible_rows(
        "Bank Account", {"company": company, "is_company_account": 1}, ["name"]
    )
    bank_lines = (
        visible_rows(
            "Bank Transaction",
            {
                "bank_account": ["in", [a.name for a in accounts]],
                "docstatus": 1,
                "unallocated_amount": [">", 0.009],
            },
            ["name", "date", "bank_account", "currency", "unallocated_amount"],
        )
        if accounts
        else []
    )
    overdue = visible_rows(
        "Sales Invoice",
        {
            "company": company,
            "docstatus": 1,
            "is_return": 0,
            "outstanding_amount": [">", 0.009],
            "due_date": ["<", today()],
        },
        ["name", "customer", "due_date", "currency", "outstanding_amount"],
    )
    held = visible_rows(
        "Purchase Invoice",
        {
            "company": company,
            "docstatus": 1,
            "on_hold": 1,
            "outstanding_amount": [">", 0.009],
        },
        ["name", "supplier", "currency", "outstanding_amount", "client_invoice"],
    )
    client_names = {b.client_invoice for b in held if b.client_invoice}
    visible_client_names = (
        {
            r.name
            for r in visible_rows(
                "Sales Invoice",
                {"company": company, "name": ["in", sorted(client_names)]},
                ["name"],
            )
        }
        if client_names
        else set()
    )
    for bill in held:
        if bill.client_invoice not in visible_client_names:
            bill.client_invoice = None
    pending = visible_rows(
        "Billing Run",
        {"company": company, "status": "Prepared"},
        ["name", "period_start", "period_end", "status"],
    )
    recent_invoices = visible_rows(
        "Sales Invoice",
        {
            "company": company,
            "docstatus": 1,
            "posting_date": [">=", add_days(today(), -30)],
        },
        ["name"],
    )
    failures = (
        visible_rows(
            "Communication",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": ["in", [i.name for i in recent_invoices]],
                "sent_or_received": "Sent",
                "communication_medium": "Email",
                "delivery_status": [
                    "in",
                    ["Bounced", "Soft-Bounced", "Error", "Rejected", "Expired"],
                ],
            },
            ["name", "reference_name", "delivery_status", "creation"],
        )
        if recent_invoices
        else []
    )
    return {
        "api_version": VERSION,
        "contract_version": VERSION,
        "company": company,
        "as_of": today(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "unavailable_sources": sorted(unavailable),
        "scope": "records_visible_to_current_user",
        "unmatched_bank_lines": bank_lines,
        "pending_billing_runs": pending,
        "overdue_invoices": overdue,
        "overdue_totals": _totals(overdue),
        "held_bills": held,
        "held_totals": _totals(held),
        "failed_invoice_emails": failures,
    }


@frappe.whitelist(methods=["GET"])
def get_supplier_payment_status(supplier, company):
    """Explain holds without applying credits, creating payments, or releasing bills."""
    _company(company)
    unavailable = set()
    visible_rows = partial(_rows, unavailable=unavailable)
    frappe.get_doc("Supplier", supplier).check_permission("read")
    bills = visible_rows(
        "Purchase Invoice",
        {
            "company": company,
            "supplier": supplier,
            "docstatus": 1,
            "is_return": 0,
            "outstanding_amount": [">", 0.009],
        },
        [
            "name",
            "posting_date",
            "due_date",
            "currency",
            "outstanding_amount",
            "on_hold",
            "client_invoice",
        ],
    )
    visible_clients = (
        visible_rows(
            "Sales Invoice",
            {
                "company": company,
                "name": [
                    "in",
                    sorted({b.client_invoice for b in bills if b.client_invoice}),
                ],
            },
            ["name", "docstatus", "currency", "outstanding_amount"],
        )
        if any(b.client_invoice for b in bills)
        else []
    )
    clients = {i.name: i for i in visible_clients}
    result = []
    for bill in bills:
        row = dict(bill)
        client = clients.get(bill.client_invoice)
        if not client:
            row["client_invoice"] = None
        row["status"] = "held" if bill.on_hold else "eligible_for_payment"
        if bill.on_hold:
            row["reason_code"] = (
                "waiting_for_client_payment"
                if client
                and client.docstatus == 1
                and flt(client.outstanding_amount) > 0.009
                else "hold_requires_review"
            )
            row["blocking_invoice"] = dict(client) if client else None
        else:
            row["reason_code"] = "not_on_hold"
            row["blocking_invoice"] = None
        result.append(row)
    return {
        "api_version": VERSION,
        "contract_version": VERSION,
        "company": company,
        "supplier": supplier,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "unavailable_sources": sorted(unavailable),
        "as_of": today(),
        "scope": "records_visible_to_current_user",
        "bills": result,
        "outstanding_totals": _totals(bills),
        "eligible_totals": _totals([b for b in bills if not b.on_hold]),
        "held_totals": _totals([b for b in bills if b.on_hold]),
        "payment_execution": "not_started",
    }
