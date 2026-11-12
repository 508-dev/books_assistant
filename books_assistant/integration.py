"""Shared contracts for authenticated financial integrations.

The ERP session is the actor. Callers must authorize the business action before
using these helpers; a Discord user supplied in a payload is never authority.
No helper commits: the request/result and accounting writes commit together.
"""

import hashlib
import json
import re
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

PREVIEW_TTL_MINUTES = 10
MAX_SELECTION = 100


def fail(code, message, exception=None):
    """A stable error code alongside Frappe's ordinary error response."""
    if getattr(frappe.local, "response", None) is not None:
        frappe.local.response["error_code"] = code
    frappe.throw(message, exception or frappe.ValidationError)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    )


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def selection(value, *, allow_none=False, name="selection"):
    if value is None and allow_none:
        return None
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except (ValueError, TypeError):
            fail("INVALID_SELECTION", f"{name} must be a JSON list of document names.")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SELECTION:
        fail(
            "INVALID_SELECTION",
            f"{name} must contain between 1 and {MAX_SELECTION} document names.",
        )
    if any(
        not isinstance(item, str) or not item.strip() or len(item) > 140
        for item in value
    ):
        fail("INVALID_SELECTION", f"{name} contains an invalid document name.")
    names = [item.strip() for item in value]
    if len(names) != len(set(names)):
        fail("INVALID_SELECTION", f"{name} must not contain duplicate document names.")
    return names


def resolve_scope(company=None):
    """Fail closed on financial configurations the existing calculations cannot support."""
    if frappe.session.user == "Guest":
        fail(
            "AUTHENTICATION_REQUIRED",
            "Sign in to use the finance API.",
            frappe.PermissionError,
        )
    companies = frappe.get_all(
        "Company", fields=["name", "default_currency"], limit_page_length=2
    )
    if len(companies) != 1:
        fail(
            "UNSUPPORTED_SCOPE",
            "Finance API v1 requires a site with exactly one company.",
        )
    site_company = companies[0]
    if company is not None and company != site_company.name:
        fail(
            "INVALID_COMPANY",
            "The requested company is outside the supported scope.",
            frappe.PermissionError,
        )
    company_doc = check_document("Company", site_company.name)
    currency = company_doc.default_currency
    if currency != "USD":
        fail("UNSUPPORTED_CURRENCY", "Finance API v1 supports a USD company only.")
    # The existing matching/reconciliation code assumes base-currency amounts.
    # Reject foreign financial data rather than silently summing unlike units.
    sources = {
        "Account": ("account_currency",),
        "Sales Invoice": ("currency",),
        "Purchase Invoice": ("currency",),
        "Bank Transaction": ("currency",),
        "Payment Entry": ("paid_from_account_currency", "paid_to_account_currency"),
    }
    for doctype, fields in sources.items():
        for field in fields:
            filters = {field: ["not in", [currency, ""]]}
            if doctype != "Account":
                filters["docstatus"] = ["!=", 2]
            if frappe.db.exists(doctype, filters):
                fail(
                    "UNSUPPORTED_CURRENCY",
                    "Finance API v1 cannot operate on foreign-currency financial records.",
                )
    return {"company": site_company.name, "currency": currency}


def check_document(doctype, name, *, permission="read", company=None, currency=None):
    locked = getattr(frappe.flags, "books_assistant_locked_documents", None) or set()
    doc = (
        frappe.get_doc(doctype, name, for_update=True)
        if (doctype, name) in locked
        else frappe.get_doc(doctype, name)
    )
    doc.check_permission(permission)
    if company and doc.get("company") and doc.company != company:
        fail(
            "INVALID_COMPANY",
            "A referenced document belongs to another company.",
            frappe.PermissionError,
        )
    if currency:
        for field in (
            "currency",
            "account_currency",
            "paid_from_account_currency",
            "paid_to_account_currency",
        ):
            if doc.get(field) and doc.get(field) != currency:
                fail(
                    "UNSUPPORTED_CURRENCY",
                    "A referenced document has an unsupported currency.",
                )
    return doc


def lock_documents(documents):
    """Deterministic row locks; only internal code supplies DocType identifiers."""
    for doctype, name in sorted(set(documents)):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _]*", doctype):
            fail("INVALID_DOCUMENT", "Invalid document type.")
        rows = frappe.db.sql(
            f"select name from `tab{doctype}` where name = %s for update", (name,)
        )
        if not rows:
            fail("DOCUMENT_MISSING", f"{doctype} {name} no longer exists.")
        locked = getattr(frappe.flags, "books_assistant_locked_documents", None)
        if locked is None:
            locked = frappe.flags.books_assistant_locked_documents = set()
        locked.add((doctype, name))


def create_preview(action, scope, payload, snapshot):
    actor = frappe.session.user
    if actor == "Guest":
        fail(
            "AUTHENTICATION_REQUIRED",
            "Sign in to create an approval preview.",
            frappe.PermissionError,
        )
    resolved = resolve_scope(scope["company"])
    if resolved != scope:
        fail("SCOPE_CHANGED", "Company settings changed. Review the action again.")
    expires_at = now_datetime() + timedelta(minutes=PREVIEW_TTL_MINUTES)
    doc = frappe.get_doc(
        {
            "doctype": "API Preview",
            "preview_key": frappe.generate_hash(length=32),
            "actor": actor,
            "action": action,
            "scope_json": canonical(scope),
            "payload_json": canonical(payload),
            "snapshot_json": canonical(snapshot),
            "snapshot_hash": fingerprint(snapshot),
            "expires_at": expires_at,
        }
    ).insert(ignore_permissions=True)
    return {
        "api_version": 1,
        "preview_id": doc.name,
        "expires_at": str(expires_at),
        "snapshot": snapshot,
    }


def consume_preview(
    preview_id, request_id, action, build_snapshot, perform, *, authorize=None
):
    """Consume a frozen preview once, retaining a durable result for safe retries.

    authorize(payload, scope) is called even on replay. build_snapshot must lock
    and reread affected documents; perform must never commit independently.
    An exception rolls back all writes made by perform, including partial work.
    """
    for value, label in ((preview_id, "preview_id"), (request_id, "request_id")):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            fail(
                "INVALID_REQUEST",
                f"{label} must be a nonempty string of at most 128 characters.",
            )
    if authorize is None:
        fail(
            "AUTHORIZATION_REQUIRED",
            "An authorization callback is required for financial execution.",
        )
    lock_documents([("API Preview", preview_id)])
    preview = frappe.get_doc("API Preview", preview_id, for_update=True)
    actor = frappe.session.user
    if actor == "Guest" or preview.actor != actor or preview.action != action:
        fail(
            "PREVIEW_FORBIDDEN",
            "This preview is not available to this actor or action.",
            frappe.PermissionError,
        )
    scope = frappe.parse_json(preview.scope_json)
    payload = frappe.parse_json(preview.payload_json)
    if resolve_scope(scope["company"]) != scope:
        fail("SCOPE_CHANGED", "Company settings changed. Review the action again.")
    # All executions through these APIs serialize on the supported Company.
    # Source row locks additionally coordinate with ordinary ERP document saves.
    lock_documents([("Company", scope["company"])])
    if resolve_scope(scope["company"]) != scope:
        fail("SCOPE_CHANGED", "Company settings changed. Review the action again.")
    authorize(payload, scope)
    request_name = fingerprint({"actor": actor, "request_id": request_id})
    input_hash = fingerprint(
        {"preview_id": preview_id, "action": action, "payload": payload, "scope": scope}
    )
    # A locking read sees a concurrently committed request even when an earlier
    # scope/permission query established a REPEATABLE READ snapshot.
    existing = frappe.db.sql(
        "select name from `tabAPI Request` where name = %s for update", (request_name,)
    )
    if existing:
        lock_documents([("API Request", request_name)])
        request = frappe.get_doc("API Request", request_name, for_update=True)
        if request.input_hash != input_hash:
            fail(
                "REQUEST_ID_REUSED",
                "This request ID was already used for a different operation.",
            )
        if request.status == "Completed":
            return frappe.parse_json(request.result_json)
        fail("REQUEST_IN_PROGRESS", "This request is already in progress.")
    if preview.get("consumed_request"):
        fail(
            "PREVIEW_CONSUMED",
            "This preview has already been used. Retry with the original request ID.",
        )
    if get_datetime(preview.expires_at) <= now_datetime():
        fail("PREVIEW_EXPIRED", "The preview expired. Review the action again.")
    snapshot = build_snapshot(payload, scope)
    if fingerprint(snapshot) != preview.snapshot_hash:
        fail(
            "PREVIEW_STALE",
            "The reviewed data changed. Review a new preview before approving.",
        )
    # The Company lock serializes concurrent inserts for this site's actors,
    # including attempts to reuse a request ID with a different preview.
    frappe.db.savepoint("api_execution")
    request = frappe.get_doc(
        {
            "doctype": "API Request",
            "request_key": request_name,
            "actor": actor,
            "request_id": request_id,
            "action": action,
            "preview": preview_id,
            "input_hash": input_hash,
            "status": "Running",
        }
    ).insert(ignore_permissions=True)
    try:
        result = perform(payload, scope)
        if not isinstance(result, dict):
            fail(
                "INVALID_RESULT",
                "Financial operations must return a structured result.",
            )
        result = {**result, "api_version": 1, "operation_id": request.name}
        encoded = canonical(result)
        request.db_set(
            {"status": "Completed", "result_json": encoded}, update_modified=False
        )
        preview.db_set("consumed_request", request.name, update_modified=False)
    except Exception as error:
        if isinstance(error, (frappe.QueryDeadlockError, frappe.QueryTimeoutError)):
            # InnoDB can already have aborted the entire transaction. Rolling
            # back to a now-missing savepoint would mask the retriable error.
            frappe.db.rollback()
        else:
            frappe.db.rollback(save_point="api_execution")
        raise
    return json.loads(encoded)
