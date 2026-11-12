"""Pay a supplier what we owe: earlier credit is applied to their bills first, then a payment covers the rest.

Credit means anything already in the supplier's favour that isn't applied to a bill: an earlier payment that was
more than the bill (or made before it), or a debit note. The payment opens unsaved, ready to check and submit.

Its reference says what the payment is (e.g. "Novo payout 2026-11-06 Jane Doe"), because some banks (Novo) show no
transfer number to copy. Once Bank Review matches the payment to its bank line, the line links the bank's own ID.
"""

import frappe
from frappe import _
from frappe.utils import add_months, flt, today

from books_assistant.integration import (
    check_document, consume_preview, create_preview, lock_documents, resolve_scope, selection,
)

APPROVERS = ("Accounts Manager", "System Manager")


def _authorize(supplier, scope):
    frappe.only_for(APPROVERS)
    check_document("Supplier", supplier)
    resolve_scope(scope["company"])


def _check_bank(bank_account, scope):
    if not bank_account:
        frappe.throw(_("Choose a company bank account."))
    account = check_document("Account", bank_account, company=scope["company"])
    if account.account_type != "Bank" or account.is_group or account.account_currency != scope["currency"]:
        frappe.throw(_("Choose a bank ledger in the company's currency."))
    bank_name = frappe.db.get_value("Bank Account", {"account": bank_account, "is_company_account": 1,
                                                    "company": scope["company"]}, "name")
    if not bank_name:
        frappe.throw(_("Choose one of this company's bank accounts."))
    bank = check_document("Bank Account", bank_name, company=scope["company"])
    return {"account": account.name, "account_modified": str(account.modified),
            "bank_account": bank.name, "bank_modified": str(bank.modified)}


def _selected(invoices, all_eligible=False):
    if isinstance(invoices, str):
        invoices = frappe.parse_json(invoices)
    if isinstance(all_eligible, str):
        all_eligible = frappe.parse_json(all_eligible)
    if all_eligible not in (True, False, 0, 1, None):
        frappe.throw(_("all_eligible must be a boolean."))
    if all_eligible in (True, 1):
        if invoices is not None:
            frappe.throw(_("Choose explicit bills or all eligible bills, not both."))
        return None
    return selection(invoices, name="invoices")


def _preview(supplier, names, scope, bank_account=None):
    _authorize(supplier, scope)
    bills = open_bills(supplier, names, scope)
    if names is not None and set(names) != {b.name for b in bills}:
        frappe.throw(_("Some selected bills are missing, inaccessible, settled, held, or belong to another supplier."))
    credits = credit_entries(supplier, scope)
    bills_total = flt(sum(b.outstanding_amount for b in bills), 2)
    credit_total = flt(sum(c.amount for c in credits), 2)
    to_pay = flt(max(bills_total - credit_total, 0), 2)
    bank = (bank_account or usual_bank_account(supplier, scope)) if to_pay > 0 else None
    bank_details = _check_bank(bank, scope) if to_pay > 0 else None
    return {
        "company": scope["company"], "currency": scope["currency"],
        "supplier": supplier, "bills": bills, "credits": credits,
        "bills_total": bills_total, "credit_total": credit_total,
        "credit_to_apply": min(bills_total, credit_total),
        "to_pay": to_pay,
        "bank_account": bank, "bank_details": bank_details, "posting_date": today(),
        "supplier_modified": str(check_document("Supplier", supplier).modified),
        "held": held_bills(supplier, scope),
        "fronting": fronting_warnings([b.name for b in bills]),
        "side_effects": {"apply_credit": True, "save_payment": False,
                         "submit_payment": False, "bank_transfer": False},
    }


@frappe.whitelist(methods=["GET"])
def preview(supplier, invoices=None, company=None, all_eligible=False, bank_account=None):
    """Read-only payable preview. Expanding to all bills requires explicit all_eligible=True."""
    return _preview(supplier, _selected(invoices, all_eligible), resolve_scope(company), bank_account)


def _authorize_preparation(payload, scope):
    _authorize(payload["supplier"], scope)
    if payload["bank_account"]:
        _check_bank(payload["bank_account"], scope)
    for name in payload["invoices"]:
        check_document("Purchase Invoice", name, permission="write",
                       company=scope["company"], currency=scope["currency"])
    for doctype, name in payload.get("credit_documents", []):
        check_document(doctype, name, permission="write", company=scope["company"], currency=scope["currency"])


def _preparation_snapshot(payload, scope):
    _authorize_preparation(payload, scope)
    view = _preview(payload["supplier"], payload["invoices"], scope, payload["bank_account"])
    documents = [("Supplier", payload["supplier"])]
    if payload["bank_account"]:
        documents.append(("Account", payload["bank_account"]))
        documents.append(("Bank Account", view["bank_details"]["bank_account"]))
    documents += [("Purchase Invoice", b.name) for b in view["bills"]]
    documents += [(c.reference_type, c.reference_name) for c in view["credits"]]
    lock_documents(documents)
    current = _preview(payload["supplier"], payload["invoices"], scope, payload["bank_account"])
    current["credit_ledger"] = _credit_ledger(current["credits"])
    return current


def _credit_ledger(credits):
    """Current ledger reads detect settlements that do not update Payment Entry.modified."""
    rows = {}
    for credit in credits:
        for row in frappe.db.sql(
            "select name, modified, amount, delinked, voucher_type, voucher_no, "
            "against_voucher_type, against_voucher_no from `tabPayment Ledger Entry` "
            "where (voucher_type = %s and voucher_no = %s) "
            "or (against_voucher_type = %s and against_voucher_no = %s) "
            "order by name for update",
            (credit.reference_type, credit.reference_name, credit.reference_type, credit.reference_name),
            as_dict=True,
        ):
            rows[row.name] = row
    return [rows[name] for name in sorted(rows)]


@frappe.whitelist(methods=["POST"])
def preview_preparation(supplier, invoices=None, company=None, all_eligible=False, bank_account=None):
    """Freeze the exact bills, credit and bank account to be confirmed before credit reconciliation."""
    scope = resolve_scope(company)
    lock_documents([("Company", scope["company"])])
    view = _preview(supplier, _selected(invoices, all_eligible), scope, bank_account)
    if not view["bills"]:
        frappe.throw(_("Nothing is owed to {0} that can be paid now.").format(supplier))
    payload = {"supplier": supplier, "invoices": [b.name for b in view["bills"]],
               "bank_account": view["bank_account"],
               "credit_documents": [(c.reference_type, c.reference_name) for c in view["credits"]]}
    return create_preview("supplier.prepare", scope, payload, _preparation_snapshot(payload, scope))


@frappe.whitelist(methods=["GET"])
def payment_warnings(references):
    """The same warnings for a draft Payment Entry's bills, shown on the form before any money is sent."""
    names = [
        r.get("reference_name") for r in frappe.parse_json(references) or []
        if r.get("reference_doctype") == "Purchase Invoice" and r.get("reference_name")
    ]
    return fronting_warnings([n for n in names if frappe.has_permission("Purchase Invoice", "read", n)])


def fronting_warnings(bill_names):
    """erp_rules' "client paid first" warning for these bills. That check runs when a payment is submitted,
    which is after the money has gone; this shows the same words while there's still time to decide."""
    if not bill_names or "ClientPaidCheck" in (frappe.conf.get("erp_rules_disabled_checks") or []):
        return []
    try:
        from erp_rules.checks.client_paid_check import ClientPaidCheck
    except ImportError:
        return []
    doc = frappe._dict(references=[frappe._dict(reference_doctype="Purchase Invoice", reference_name=name)
                                   for name in dict.fromkeys(bill_names)])
    return [problem.message for problem in ClientPaidCheck().problems(doc)]


@frappe.whitelist(methods=["POST"])
def prepare(preview_id=None, request_id=None):
    """Apply exactly the reviewed credit, then return an unsaved payment. Never transfers bank money."""
    frappe.only_for(APPROVERS)
    if not preview_id or not request_id:
        frappe.throw(_("Review a payment preparation preview and provide a request ID first."))
    return consume_preview(preview_id, request_id, "supplier.prepare", _preparation_snapshot, _prepare,
                           authorize=_authorize_preparation)


def _prepare(payload, scope):
    supplier, names = payload["supplier"], payload["invoices"]
    applied = apply_credit(supplier, names, scope)
    bills = open_bills(supplier, names, scope)
    payment = new_payment(supplier, bills, payload["bank_account"], scope) if bills else None
    return {"status": "prepared", "company": scope["company"], "currency": scope["currency"],
            "payment_entry": payment.as_dict() if payment else None, "credit_applied": applied,
            "bank_transfer": False, "payment_saved": False, "payment_submitted": False}


def open_bills(supplier, names=None, scope=None):
    scope = scope or resolve_scope()
    check_document("Supplier", supplier)
    filters = {"supplier": supplier, "docstatus": 1, "outstanding_amount": [">", 0.009], "is_return": 0,
               "on_hold": 0}
    filters["company"] = scope["company"]
    if names is not None:
        if not names:
            return []
        filters["name"] = ["in", names]
    rows = frappe.get_list(
        "Purchase Invoice",
        filters=filters,
        fields=["name", "posting_date", "bill_no", "project", "grand_total", "outstanding_amount",
                "modified", "company", "currency"],
        limit_page_length=0,
        order_by="posting_date, name",
    )
    current = []
    for row in rows:
        doc = check_document("Purchase Invoice", row.name, company=scope["company"], currency=scope["currency"])
        if doc.supplier != supplier or doc.docstatus != 1 or doc.on_hold or doc.is_return or flt(doc.outstanding_amount) <= 0.009:
            continue
        for field in row:
            row[field] = doc.get(field)
        current.append(row)
    return current


def held_bills(supplier, scope=None):
    """Bills on hold, which aren't paid until released (automatically when the client pays, or by hand)."""
    scope = scope or resolve_scope()
    rows = frappe.get_list(
        "Purchase Invoice",
        filters={"supplier": supplier, "company": scope["company"], "docstatus": 1,
                 "outstanding_amount": [">", 0.009], "on_hold": 1},
        fields=["name", "posting_date", "project", "outstanding_amount", "client_invoice", "hold_comment", "currency"],
        limit_page_length=0,
        order_by="posting_date, name",
    )
    for row in rows:
        check_document("Purchase Invoice", row.name, company=scope["company"], currency=scope["currency"])
        if row.client_invoice:
            try:
                client = check_document("Sales Invoice", row.client_invoice)
                available = (client.docstatus == 1 and client.company == scope["company"]
                             and client.currency == scope["currency"])
            except (frappe.PermissionError, frappe.DoesNotExistError):
                frappe.clear_last_message()
                available = False
            if not available:
                row.client_invoice = None
                row.hold_comment = _("The linked client invoice needs review by an accounting manager.")
                row.reason_code = "blocking_invoice_unavailable"
    return rows


def _reconciliation(supplier, scope=None):
    from erpnext.accounts.party import get_party_account

    scope = scope or resolve_scope()
    company = scope["company"]
    check_document("Supplier", supplier)
    party_account = get_party_account("Supplier", supplier, company)
    account = check_document("Account", party_account, company=company)
    if account.account_currency != scope["currency"]:
        frappe.throw(_("Supplier credit reconciliation requires the company currency."))
    pr = frappe.get_doc(
        {
            "doctype": "Payment Reconciliation",
            "company": company,
            "party_type": "Supplier",
            "party": supplier,
            "receivable_payable_account": party_account,
        }
    )
    pr.get_unreconciled_entries()
    return pr


def credit_entries(supplier, scope=None):
    scope = scope or resolve_scope()
    rows = [
        frappe._dict(reference_type=p.reference_type, reference_name=p.reference_name, posting_date=p.posting_date,
                     amount=flt(p.amount))
        for p in _reconciliation(supplier, scope).payments
    ]
    for row in rows:
        doc = check_document(row.reference_type, row.reference_name, company=scope["company"])
        row.modified = str(doc.modified)
    return rows


def apply_credit(supplier, bill_names, scope=None):
    """Apply the supplier's unapplied credit to these bills, oldest bill and oldest credit first."""
    pr = _reconciliation(supplier, scope)
    payments = sorted((p.as_dict() for p in pr.payments), key=lambda p: (p.posting_date, p.reference_name))
    invoices = sorted(
        (i.as_dict() for i in pr.invoices if i.invoice_type == "Purchase Invoice" and i.invoice_number in bill_names),
        key=lambda i: (i.invoice_date, i.invoice_number),
    )
    if not payments or not invoices:
        return 0
    pr.allocate_entries(frappe._dict(payments=payments, invoices=invoices))
    applied = flt(sum(flt(row.allocated_amount) for row in pr.allocation), 2)
    if applied:
        pr.reconcile()
    return applied


def new_payment(supplier, bills, bank_gl, scope=None):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    scope = scope or resolve_scope()
    _check_bank(bank_gl, scope)
    for bill in bills:
        check_document("Purchase Invoice", bill.name, company=scope["company"], currency=scope["currency"])
    pe = get_payment_entry("Purchase Invoice", bills[0].name, bank_account=bank_gl)
    pe.set("references", [])
    for bill in bills:
        pe.append("references", {
            "reference_doctype": "Purchase Invoice",
            "reference_name": bill.name,
            "total_amount": bill.grand_total,
            "outstanding_amount": bill.outstanding_amount,
            "allocated_amount": bill.outstanding_amount,
            "exchange_rate": 1,
        })
    total = flt(sum(b.outstanding_amount for b in bills), 2)
    pe.paid_amount = pe.received_amount = total
    pe.paid_from = bank_gl
    # ERPNext fills Company Bank Account with the default bank; make it the one actually paid from.
    pe.bank_account = frappe.db.get_value("Bank Account", {"account": bank_gl, "is_company_account": 1}, "name")
    pe.posting_date = pe.reference_date = today()
    pe.reference_no = payout_reference(bank_gl, supplier)
    return pe


def payout_reference(bank_gl, supplier):
    """A reference that identifies the payout, for banks that show no transfer number."""
    bank = (bank_gl or "").split(" - ")[0] or _("Bank")
    return f"{bank} payout {today()} {supplier}"[:140]


def usual_bank_account(supplier, scope=None):
    """The bank account this supplier has most often been paid from in the past year."""
    scope = scope or resolve_scope()
    row = frappe.db.sql(
        """
        select paid_from, count(*) n from `tabPayment Entry`
        where docstatus = 1 and payment_type = 'Pay' and party_type = 'Supplier' and party = %s and posting_date >= %s
          and company = %s
          and paid_from in (select account from `tabBank Account` where is_company_account = 1 and company = %s)
        group by paid_from order by n desc limit 1
        """,
        (supplier, add_months(today(), -12), scope["company"], scope["company"]),
    )
    if row:
        return row[0][0]
    return frappe.db.get_value("Bank Account", {"is_company_account": 1, "company": scope["company"], "is_default": 1}, "account") or frappe.db.get_value(
        "Bank Account", {"is_company_account": 1, "company": scope["company"]}, "account"
    )
