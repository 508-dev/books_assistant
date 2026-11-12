"""Frozen Bank Review approvals shared by the ERP interface and integrations."""

import frappe
from frappe import _
from frappe.utils import cstr, flt, strip_html

from books_assistant.integration import (
    check_document,
    consume_preview,
    create_preview,
    fingerprint,
    lock_documents,
    resolve_scope,
    selection,
)


def _check_line(name, scope, permission="read"):
    doc = check_document(
        "Bank Transaction", name, permission=permission, currency=scope["currency"]
    )
    bank = check_document("Bank Account", doc.bank_account, company=scope["company"])
    account = check_document(
        "Account", bank.account, company=scope["company"], currency=scope["currency"]
    )
    if not bank.is_company_account or account.account_type != "Bank":
        frappe.throw(_("Choose one of the company's bank accounts."))
    return doc


def _queue(scope, bank_account=None):
    from books_assistant import bank_review as br

    if bank_account:
        check_document("Bank Account", bank_account, company=scope["company"])
    context = br.Context(scope["company"])
    rows = []
    for bt in br.unreconciled_lines(bank_account):
        current = _check_line(bt.name, scope)
        if current.docstatus != 1 or flt(current.unallocated_amount) <= 0.009:
            continue
        if bank_account and current.bank_account != bank_account:
            continue
        for field in bt:
            bt[field] = current.get(field)
        rows.append(
            {
                **context.suggest(bt),
                "company": scope["company"],
                "currency": scope["currency"],
            }
        )
    return rows


def _dependencies(s):
    documents = [
        ("Bank Transaction", s["bank_transaction"]),
        ("Bank Account", s["bank_account"]),
    ]
    if s.get("bank_gl"):
        documents.append(("Account", s["bank_gl"]))
    if s.get("voucher"):
        documents.append((s["voucher_type"], s["voucher"]))
    documents += [(i["doctype"], i["name"]) for i in s.get("invoices", [])]
    if s.get("party_type") in ("Customer", "Supplier") and s.get("party"):
        documents.append((s["party_type"], s["party"]))
    for key in ("deposit_line", "withdrawal_line"):
        if s.get(key):
            documents.append(("Bank Transaction", s[key]))
            paired = check_document("Bank Transaction", s[key])
            documents.append(("Bank Account", paired.bank_account))
            bank = check_document("Bank Account", paired.bank_account)
            if bank.account:
                documents.append(("Account", bank.account))
    if s.get("rule"):
        documents.append(("Bank Rule", s["rule"]))
    if s.get("account"):
        documents.append(("Account", s["account"]))
    return sorted(set(documents))


def _reviewed_row(s, scope):
    versions = []
    for doctype, name in _dependencies(s):
        doc = check_document(
            doctype, name, company=scope["company"], currency=scope["currency"]
        )
        if doctype == "Bank Transaction":
            _check_line(name, scope)
        versions.append(
            {
                "doctype": doctype,
                "name": name,
                "modified": str(doc.modified),
                "docstatus": doc.docstatus,
            }
        )
    row = {**s, "versions": versions}
    row["fingerprint"] = fingerprint(row)
    return row


def get_queue(bank_account=None, company=None):
    from books_assistant.bank_review import APPROVERS

    frappe.only_for(APPROVERS)
    scope = resolve_scope(company)
    return [_reviewed_row(row, scope) for row in _queue(scope, bank_account)]


def _authorize(payload, scope):
    from books_assistant.bank_review import APPROVERS

    frappe.only_for(APPROVERS)
    resolve_scope(scope["company"])
    for name in payload["bank_transactions"]:
        _check_line(name, scope, "write")
    for doctype, name in payload.get("documents", []):
        permission = "write" if doctype in ("Bank Transaction", "Bank Rule") else "read"
        check_document(
            doctype,
            name,
            permission=permission,
            company=scope["company"],
            currency=scope["currency"],
        )


def _bank_snapshot(payload, scope):
    from books_assistant.bank_review import NONE

    _authorize(payload, scope)
    names = set(payload["bank_transactions"])
    rows = [
        row
        for row in _queue(scope, payload.get("bank_account"))
        if row["bank_transaction"] in names
    ]
    if names != {row["bank_transaction"] for row in rows}:
        frappe.throw(
            _("A selected bank line changed or was reconciled. Refresh Bank Review.")
        )
    lock_documents([doc for row in rows for doc in _dependencies(row)])
    rows = [
        row
        for row in _queue(scope, payload.get("bank_account"))
        if row["bank_transaction"] in names
    ]
    if names != {row["bank_transaction"] for row in rows}:
        frappe.throw(
            _("A selected bank line changed or was reconciled. Refresh Bank Review.")
        )
    if any(row["kind"] == NONE for row in rows):
        frappe.throw(_("A selected bank line no longer has an action to approve."))
    return {
        "company": scope["company"],
        "currency": scope["currency"],
        "rows": [_reviewed_row(row, scope) for row in rows],
    }


def preview_approval(
    bank_transactions, expected_fingerprints, company=None, bank_account=None
):
    from books_assistant.bank_review import APPROVERS

    frappe.only_for(APPROVERS)
    names = selection(bank_transactions, name="bank_transactions")
    expected = frappe.parse_json(expected_fingerprints)
    if not isinstance(expected, dict) or set(expected) != set(names):
        frappe.throw(_("Provide the fingerprint of every reviewed bank line."))
    scope = resolve_scope(company)
    lock_documents([("Company", scope["company"])])
    payload = {"bank_transactions": names, "bank_account": bank_account}
    snapshot = _bank_snapshot(payload, scope)
    if any(
        expected[row["bank_transaction"]] != row["fingerprint"]
        for row in snapshot["rows"]
    ):
        frappe.throw(
            _("The suggested action changed. Refresh Bank Review and review it again.")
        )
    payload["documents"] = sorted(
        {doc for row in snapshot["rows"] for doc in _dependencies(row)}
    )
    return create_preview("bank.approve", scope, payload, snapshot)


def approve(preview_id=None, request_id=None):
    from books_assistant.bank_review import APPROVERS

    frappe.only_for(APPROVERS)
    if not preview_id or not request_id:
        frappe.throw(
            _("Review a bank approval preview and provide a request ID first.")
        )
    return consume_preview(
        preview_id,
        request_id,
        "bank.approve",
        _bank_snapshot,
        _approve,
        authorize=_authorize,
    )


def _approve(payload, scope):
    from books_assistant import bank_review as br

    names = set(payload["bank_transactions"])
    rows = [
        row
        for row in _queue(scope, payload.get("bank_account"))
        if row["bank_transaction"] in names
    ]
    results = []
    for row in rows:
        name = row["bank_transaction"]
        frappe.db.savepoint("bank_approval_item")
        try:
            bt = br.bank_line(name)
            if not bt:
                frappe.throw(_("Bank line no longer exists."))
            if flt(bt.unallocated_amount) <= 0.009:
                results.append(
                    {
                        "name": name,
                        "ok": True,
                        "status": "already_reconciled",
                        "message": _("Already reconciled"),
                    }
                )
                continue
            message = br.execute(row)
            results.append(
                {"name": name, "ok": True, "status": "approved", "message": message}
            )
        except Exception as e:
            frappe.db.rollback(save_point="bank_approval_item")
            frappe.clear_last_message()
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "status": "failed",
                    "error_code": "BANK_ACTION_FAILED",
                    "message": strip_html(cstr(e)).strip() or repr(e),
                }
            )
    return {
        "status": "partial_failure"
        if any(not row["ok"] for row in results)
        else "completed",
        "company": scope["company"],
        "currency": scope["currency"],
        "results": results,
    }
