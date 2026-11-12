"""Version 1 integration contracts for reviewing and approving stored Billing Runs."""

import frappe
from frappe import _

from books_assistant import invoice_email
from books_assistant.books_assistant.doctype.billing_run.billing_run import (
    APPROVERS,
    PREPARERS,
    bill_names,
    invoiced_by,
)
from books_assistant.integration import (
    check_document,
    consume_preview,
    create_preview,
    lock_documents,
    resolve_scope,
    selection,
)
from books_assistant.setup import BILLING_APPROVER

ACTION = "billing.approve"


def _authorize(payload, scope):
    frappe.only_for(APPROVERS)
    resolve_scope(scope["company"])
    run = check_document(
        "Billing Run",
        payload["run_id"],
        permission="write",
        company=scope["company"],
        currency=scope["currency"],
    )
    projects = selection(payload["projects"], name="projects")
    rows = [row for row in run.items if row.project in projects]
    if {row.project for row in rows} != set(projects):
        frappe.throw(_("The selection includes projects outside this run."))
    for row in rows:
        check_document("Project", row.project, company=scope["company"])
        if row.customer:
            check_document("Customer", row.customer)
        for doctype, name in _row_invoices(row):
            doc = check_document(
                doctype, name, company=scope["company"], currency=scope["currency"]
            )
            doc.check_permission("submit")
    return run


def _row_invoices(row):
    documents = [("Purchase Invoice", name) for name in bill_names(row)]
    if row.sales_invoice:
        documents.append(("Sales Invoice", row.sales_invoice))
    return documents


def _snapshot_document(doctype, name, scope):
    doc = check_document(
        doctype, name, company=scope["company"], currency=scope["currency"]
    )
    data = doc.as_dict()
    # Onload/UI fields are not part of the stored financial document.
    return {key: value for key, value in data.items() if not key.startswith("__")}


def _snapshot(payload, scope):
    """Lock and reload canonical documents; never trust a client-supplied run object."""
    lock_documents([("Company", scope["company"])])
    lock_documents([("Billing Run", payload["run_id"])])
    run = _authorize(payload, scope)
    if run.status != "Prepared":
        frappe.throw(_("Prepare the run before previewing approval."))
    projects = set(payload["projects"])
    rows = [row for row in run.items if row.project in projects]
    if any(row.item_status not in ("pending", "failed") for row in rows):
        frappe.throw(
            _(
                "Only pending or failed projects can be approved. Review legacy states first."
            )
        )

    documents = set()
    for row in rows:
        documents.add(("Project", row.project))
        if row.customer:
            documents.add(("Customer", row.customer))
        documents.update(_row_invoices(row))
        waiting_on = invoiced_by(bill_names(row))
        if waiting_on:
            documents.add(("Sales Invoice", waiting_on))
        for name in frappe.get_all(
            "Timesheet",
            filters={"purchase_invoice": ["in", bill_names(row) or [""]]},
            pluck="name",
        ):
            documents.add(("Timesheet", name))
        if row.sales_invoice:
            for (name,) in frappe.db.sql(
                "select name from `tabPurchase Invoice` where referral_source_si = %s "
                "and docstatus = 0 order by name for update",
                row.sales_invoice,
            ):
                check_document(
                    "Purchase Invoice",
                    name,
                    company=scope["company"],
                    currency=scope["currency"],
                )
                documents.add(("Purchase Invoice", name))
            for name in frappe.get_all(
                "Sales Invoice Timesheet",
                filters={"parent": row.sales_invoice},
                pluck="time_sheet",
            ):
                if name:
                    documents.add(("Timesheet", name))
        if row.customer:
            for name in frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "link_doctype": "Customer",
                    "link_name": row.customer,
                },
                pluck="parent",
            ):
                documents.add(("Contact", name))
    lock_documents(sorted(documents))
    # Read email settings, contacts and recipient fields with current locking reads from here through execution.
    frappe.flags.books_assistant_billing_locked = True
    settings = invoice_email.settings()

    snapshots = []
    for doctype, name in sorted(documents):
        snapshots.append(
            {
                "doctype": doctype,
                "name": name,
                "document": _snapshot_document(doctype, name, scope),
            }
        )
    selected = []
    for row in rows:
        invoice = (
            check_document(
                "Sales Invoice",
                row.sales_invoice,
                company=scope["company"],
                currency=scope["currency"],
            )
            if row.sales_invoice
            else None
        )
        to, cc = invoice_email.recipients(invoice) if invoice else ([], [])
        will_email = bool(
            invoice
            and invoice.docstatus == 0
            and to
            and settings.send_invoice_emails
            and BILLING_APPROVER in frappe.get_roles()
        )
        if will_email:
            invoice.check_permission("email")
        bill_total = sum(
            check_document(
                "Purchase Invoice",
                name,
                company=scope["company"],
                currency=scope["currency"],
            ).grand_total
            for name in bill_names(row)
        )
        selected.append(
            {
                "project": row.project,
                "customer": row.customer,
                "item_status": row.item_status,
                "sales_invoice": row.sales_invoice,
                "client_amount": invoice.grand_total if invoice else 0,
                "engineer_bills": bill_names(row),
                "engineer_cost": bill_total,
                "referral_cost": row.referral_cost,
                "margin_pct": row.margin_pct,
                "issues": row.issues,
                "recipients": to,
                "cc": cc,
                "print_format": invoice_email.print_format(invoice)
                if invoice
                else None,
                "email_on_approval": will_email,
                "email_state_on_success": "queued" if will_email else "not_queued",
            }
        )
    return {
        "api_version": 1,
        "run_id": run.name,
        "run_modified": str(run.modified),
        "company": scope["company"],
        "currency": scope["currency"],
        "period_start": str(run.period_start),
        "period_end": str(run.period_end),
        "invoice_date": str(run.invoice_date),
        "projects": selected,
        "documents": snapshots,
        "settings": {
            key: settings.get(key)
            for key in (
                "send_invoice_emails",
                "hold_engineer_bills",
                "always_cc",
                "email_subject",
                "email_body",
                "invoice_print_format",
                "timesheet_print_format",
            )
        },
        "disabled_checks": sorted(frappe.conf.get("erp_rules_disabled_checks") or []),
    }


@frappe.whitelist(methods=["POST"])
def preview_billing_approval(run_id, projects, company=None):
    """Persist the exact selected documents and email consequences for human review."""
    frappe.only_for(APPROVERS)
    scope = resolve_scope(company)
    payload = {
        "run_id": run_id,
        "projects": sorted(selection(projects, name="projects")),
    }
    return {
        "api_version": 1,
        **create_preview(ACTION, scope, payload, _snapshot(payload, scope)),
    }


def execute_billing_approval(
    preview_id, request_id, expected_run=None, expected_projects=None
):
    def authorize(payload, scope):
        if expected_run is not None and payload["run_id"] != expected_run:
            frappe.throw(_("The preview belongs to another Billing Run."))
        if expected_projects is not None and set(payload["projects"]) != set(
            expected_projects
        ):
            frappe.throw(_("The selected projects differ from the reviewed preview."))
        _authorize(payload, scope)

    def perform(payload, scope):
        run = _authorize(payload, scope)
        return {
            "api_version": 1,
            "company": scope["company"],
            "currency": scope["currency"],
            **run.approve_selected(payload["projects"]),
        }

    return consume_preview(
        preview_id, request_id, ACTION, _snapshot, perform, authorize=authorize
    )


@frappe.whitelist(methods=["POST"])
def approve_billing(preview_id, request_id):
    """Consume a preview once; retries return the same durable operation result."""
    return execute_billing_approval(preview_id, request_id)


@frappe.whitelist(methods=["POST"])
def prepare_billing(run_id, company=None):
    frappe.only_for(PREPARERS)
    scope = resolve_scope(company)
    run = check_document(
        "Billing Run", run_id, permission="write", company=scope["company"]
    )
    return {"api_version": 1, **run.prepare()}
