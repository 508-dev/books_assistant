"""Funding advice and durable approval; never sends money or blocks recording past transfers.

All mutations serialize on Company, then the bank Account row. Reservations are deliberately not
expired: an unrecorded transfer may already have left the bank.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from books_assistant.funding_policy import cash_check, fingerprint, money

APPROVERS = ("Accounts Manager", "System Manager")
RESERVED = ("Active", "Invalid")


def permitted(doctype, name, permission="read", current=False):
    doc = frappe.get_doc(doctype, name, for_update=True) if current else frappe.get_doc(doctype, name)
    doc.check_permission(permission)
    return doc


def bank_details(bank_account):
    bank = permitted("Bank Account", bank_account)
    if not bank.is_company_account or not bank.account:
        frappe.throw(_("Choose a company bank account linked to a ledger account."))
    account = permitted("Account", bank.account)
    permitted("Company", account.company)
    from books_assistant.integration import resolve_scope

    resolve_scope(account.company)
    if bank.company != account.company:
        frappe.throw(_("Bank Account and ledger company must match."))
    if account.account_type != "Bank" or account.is_group or account.disabled:
        frappe.throw(_("Choose an enabled bank ledger account."))
    return bank, account


def lock_bank(bank_account):
    bank, account = bank_details(bank_account)
    frappe.db.sql("select name from `tabCompany` where name = %s for update", account.company)
    frappe.db.sql("select name from `tabAccount` where name = %s for update", account.name)
    frappe.db.sql("select name from `tabBank Account` where name = %s for update", bank.name)
    return bank, account


def latest_snapshot(bank_account):
    # Locking reads see the latest committed state even after waiting for another approval.
    rows = frappe.db.sql(
        "select name, available_balance, pending_outflows, minimum_buffer, balance_as_of "
        "from `tabBank Funding Snapshot` where bank_account = %s "
        "order by balance_as_of desc, creation desc, name desc limit 1 for update",
        bank_account, as_dict=True,
    )
    return rows[0] if rows else None


def reservations(bank_account=None, payment_entry=None):
    filters, values = ["status in ('Active', 'Invalid')"], []
    if bank_account:
        filters.append("bank_account = %s")
        values.append(bank_account)
    if payment_entry:
        filters.append("payment_entry = %s")
        values.append(payment_entry)
    return frappe.db.sql(
        "select name, payment_entry, bank_account, amount, status, payment_fingerprint, snapshot, fronting_reason, creation "
        "from `tabPayout Reservation` where " + " and ".join(filters) + " order by name for update",
        tuple(values), as_dict=True,
    )


def payment_signature(doc):
    fields = (
        "company", "payment_type", "party_type", "party", "paid_from", "paid_to", "bank_account",
        "party_bank_account", "paid_amount", "received_amount", "paid_from_account_currency",
        "paid_to_account_currency", "source_exchange_rate", "target_exchange_rate", "reference_no",
        "reference_date", "posting_date", "mode_of_payment", "project", "cost_center",
        "taxes", "deductions", "apply_tax_withholding_amount", "tax_withholding_category",
    )
    data = {key: doc.get(key) for key in fields}
    data["references"] = [
        {key: row.get(key) for key in ("reference_doctype", "reference_name", "allocated_amount", "exchange_rate")}
        for row in doc.get("references") or []
    ]
    # A bank-detail edit must also invalidate an approval for the same party_bank_account ID.
    if doc.get("party_bank_account"):
        data["recipient_bank_version"] = frappe.db.get_value("Bank Account", doc.party_bank_account, "modified")
    return fingerprint(data)


def client_funding(bill, company, currency):
    """Exact links only; absent links or non-cash offsets cannot establish client cash receipt."""
    names = {bill.get("client_invoice")} if bill.get("client_invoice") else set()
    if not names and bill.get("timesheet"):
        names.update(frappe.get_all("Timesheet Detail", filters={"parent": bill.timesheet,
                     "sales_invoice": ["is", "set"]}, pluck="sales_invoice", distinct=True))
    if not names:
        return {"bill": bill.name, "status": "unverified", "reason": "missing_client_invoice"}
    invoices = []
    for name in sorted(names):
        invoice = permitted("Sales Invoice", name, current=True)
        state = "unverified"
        if invoice.company == company and invoice.currency == currency and invoice.docstatus == 1 and not invoice.is_return:
            if money(invoice.outstanding_amount) > 0:
                state = "unpaid"
            elif money(invoice.grand_total) > 0 and settled_receipts(invoice) >= money(invoice.grand_total):
                state = "received"
        invoices.append({"invoice": name, "status": state, "outstanding": invoice.outstanding_amount,
                         "modified": str(invoice.modified)})
    status = "received" if all(i["status"] == "received" for i in invoices) else (
        "unpaid" if any(i["status"] == "unpaid" for i in invoices) else "unverified"
    )
    return {"bill": bill.name, "status": status, "invoices": invoices}


def settled_receipts(invoice):
    # A manually set clearance_date or a credit note alone is not bank evidence.
    rows = frappe.db.sql(
        """
        select pe.name, pe.paid_to, pe.received_amount, sum(per.allocated_amount) allocated
        from `tabPayment Entry` pe join `tabPayment Entry Reference` per on per.parent = pe.name
        where pe.docstatus = 1 and pe.payment_type = 'Receive' and pe.party_type = 'Customer'
          and pe.party = %s and pe.company = %s and pe.paid_from_account_currency = %s
          and pe.paid_to_account_currency = %s and per.reference_doctype = 'Sales Invoice'
          and per.reference_name = %s
        group by pe.name, pe.paid_to, pe.received_amount for update
        """, (invoice.customer, invoice.company, invoice.currency, invoice.currency, invoice.name), as_dict=True,
    )
    total = money(0)
    for row in rows:
        matched = frappe.db.sql(
            """
            select coalesce(sum(bp.allocated_amount), 0)
            from `tabBank Transaction Payments` bp join `tabBank Transaction` bt on bt.name = bp.parent
            join `tabBank Account` ba on ba.name = bt.bank_account
            where bp.payment_document = 'Payment Entry' and bp.payment_entry = %s
              and bt.docstatus = 1 and bt.deposit > 0 and bt.withdrawal = 0 and bt.date <= %s
              and bt.currency = %s and ba.account = %s and ba.company = %s for update
            """, (row.name, now_datetime().date(), invoice.currency, row.paid_to, invoice.company),
        )[0][0]
        if money(row.received_amount) > 0 and money(matched) >= money(row.received_amount):
            total += max(min(money(row.allocated), money(row.received_amount)), money(0))
    return total


def assess(bank_account, amount, bill_names, supplier=None, exclude_payment=None):
    bank, account = lock_bank(bank_account)
    currency = account.account_currency or frappe.get_cached_value("Company", account.company, "default_currency")
    bills, clients, issues = [], [], []
    for name in sorted(set(bill_names)):
        bill = permitted("Purchase Invoice", name, current=True)
        if bill.company != account.company or bill.currency != currency or (supplier and bill.supplier != supplier):
            frappe.throw(_("Bills and payout account must have the same company, supplier and currency."))
        if bill.docstatus != 1 or bill.is_return or bill.on_hold or money(bill.outstanding_amount) <= 0:
            issues.append("ineligible_bill")
        bills.append({"name": bill.name, "modified": str(bill.modified), "outstanding": bill.outstanding_amount,
                      "on_hold": bill.on_hold})
        # Every project bill is checked. Unlinked non-project expenses are not assumed client-funded.
        engineering = any(i.item_code == "Contract Engineering (Purchasing)" for i in bill.get("items") or [])
        if engineering or bill.project or bill.get("client_invoice") or bill.get("timesheet"):
            clients.append(client_funding(bill, account.company, currency))
    pending = reservations(bank_account=bank.name)
    reserved = sum((money(r.amount) for r in pending if r.payment_entry != exclude_payment), money(0))
    snapshot = latest_snapshot(bank.name)
    max_age = frappe.db.get_single_value("Automation Settings", "funding_balance_max_age_hours") or 4
    cash = cash_check(snapshot, amount, reserved, now_datetime(), float(max_age))
    if not bill_names:
        issues.append("missing_bills")
    if currency != frappe.get_cached_value("Company", account.company, "default_currency"):
        issues.append("unsupported_currency")
    report = {"version": 1, "bank_account": bank.name, "company": account.company, "currency": currency,
              "cash": cash, "clients": clients, "issues": issues,
              "fronting_required": any(c["status"] != "received" for c in clients),
              "can_approve": cash["status"] == "sufficient" and not issues,
              "bills": bills, "reservation_state": fingerprint(
                  [dict(r) for r in pending if r.payment_entry != exclude_payment])}
    report["review_token"] = fingerprint(report)
    return report


@frappe.whitelist(methods=["GET", "POST"])
def check(bank_account, amount, invoices=None, supplier=None):
    """Read-only contract for ERP and future Discord clients. Bank Account is an ERP document ID."""
    frappe.only_for(APPROVERS)
    names = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices
    if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
        frappe.throw(_("Pass an explicit list of bills."))
    return assess(bank_account, amount, names, supplier)



def payout_bank(doc):
    if doc.get("bank_account"):
        bank, account = bank_details(doc.bank_account)
        if account.name != doc.paid_from or account.company != doc.company:
            frappe.throw(_("Company Bank Account must match the payout account and company."))
        return bank.name
    names = frappe.get_all("Bank Account", filters={"account": doc.paid_from, "is_company_account": 1,
                           "company": doc.company}, pluck="name")
    if len(names) != 1:
        frappe.throw(_("Choose the exact Company Bank Account for this payout."))
    bank_details(names[0])
    return names[0]


def payment_report(doc):
    if doc.docstatus != 0 or doc.payment_type != "Pay" or doc.party_type != "Supplier":
        frappe.throw(_("Funding approval is for a saved draft supplier payout."))
    bank = payout_bank(doc)
    if not bank:
        frappe.throw(_("The payout account needs a company Bank Account."))
    if money(doc.paid_amount) <= 0:
        frappe.throw(_("Payout amount must be positive."))
    refs = doc.get("references") or []
    names = [r.reference_name for r in refs if r.reference_doctype == "Purchase Invoice"]
    report = assess(bank, doc.paid_amount, names, doc.party, exclude_payment=doc.name)
    if report["company"] != doc.company or report["currency"] != doc.paid_from_account_currency:
        frappe.throw(_("Payment company and currency must match the payout account."))
    if (any(r.reference_doctype != "Purchase Invoice" or money(r.allocated_amount) <= 0 for r in refs)
            or doc.paid_from_account_currency != doc.paid_to_account_currency
            or len(names) != len(set(names)) or doc.get("taxes") or doc.get("deductions")
            or sum((money(r.allocated_amount) for r in refs), money(0)) != money(doc.paid_amount)):
        report["issues"].append("unsupported_payment_allocation")
    outstanding = {b["name"]: money(b["outstanding"]) for b in report["bills"]}
    if any(money(r.allocated_amount) > outstanding.get(r.reference_name, money(0)) for r in refs):
        report["issues"].append("allocation_exceeds_outstanding")
    if doc.get("party_bank_account"):
        permitted("Bank Account", doc.party_bank_account)
    own = reservations(payment_entry=doc.name)
    signature = payment_signature(doc)
    report.update(payment_entry=doc.name, payment_fingerprint=signature)
    report["approval"] = dict(own[0]) if own else None
    if own and (own[0].status != "Active" or own[0].payment_fingerprint != signature or own[0].bank_account != bank):
        report["issues"].append("approval_changed_release_required")
    report["can_approve"] = report["can_approve"] and not report["issues"]
    report.pop("review_token", None)
    report["review_token"] = fingerprint({k: v for k, v in report.items() if k != "approval"})
    return report


@frappe.whitelist(methods=["GET", "POST"])
def check_payment(payment_entry):
    frappe.only_for(APPROVERS)
    return payment_report(permitted("Payment Entry", payment_entry))


@frappe.whitelist(methods=["POST"])
def approve(payment_entry, review_token, fronting_reason=None):
    """Recheck the reviewed state, then reserve cash atomically. Retries return the same reservation."""
    frappe.only_for(APPROVERS)
    doc = permitted("Payment Entry", payment_entry, "write")
    company = doc.company
    frappe.db.sql("select name from `tabCompany` where name = %s for update", company)
    doc = permitted("Payment Entry", payment_entry, "write", current=True)
    if doc.company != company:
        frappe.throw(_("Payment company changed. Review funding again."))
    bank = payout_bank(doc)
    lock_bank(bank)
    report = payment_report(doc)
    existing = report["approval"]
    if report["review_token"] != review_token:
        frappe.throw(_("Funding or payment details changed. Review the new check before approving."))
    if not report["can_approve"]:
        frappe.throw(_("Funding is insufficient or unverified. Review the funding check."))
    reason = (fronting_reason or (existing or {}).get("fronting_reason") or "").strip()
    if report["fronting_required"] and not reason:
        frappe.throw(_("Record a reason for paying before verified client receipt."))
    if existing and existing["status"] == "Active":
        if existing.get("snapshot") != report["cash"]["snapshot"] or (reason and not existing.get("fronting_reason")):
            record = permitted("Payout Reservation", existing["name"])
            record.flags.funding_operation = True
            record.snapshot, record.fronting_reason = report["cash"]["snapshot"], reason
            record.assessment = json.dumps(report, default=str)
            record.save(ignore_permissions=True)
            doc.add_comment("Comment", _("Payout funding rechecked; reservation {0}. {1}").format(record.name, reason))
        return {"reservation": existing["name"], "assessment": report}
    reservation = frappe.get_doc({
        "doctype": "Payout Reservation", "payment_entry": doc.name, "bank_account": bank,
        "company": doc.company, "currency": report["currency"], "amount": doc.paid_amount,
        "status": "Active", "snapshot": report["cash"]["snapshot"],
        "payment_fingerprint": report["payment_fingerprint"], "fronting_reason": reason,
        "assessment": json.dumps(report, default=str),
    })
    reservation.flags.funding_operation = True
    reservation.insert(ignore_permissions=True)
    doc.add_comment("Comment", _("Payout funding approved; reservation {0}. {1}").format(
        reservation.name, fronting_reason or ""))
    return {"reservation": reservation.name, "assessment": report}


@frappe.whitelist(methods=["POST"])
def release(reservation, reason):
    frappe.only_for(APPROVERS)
    record = permitted("Payout Reservation", reservation)
    lock_bank(record.bank_account)
    payment = permitted("Payment Entry", record.payment_entry, current=True)
    record = permitted("Payout Reservation", reservation, current=True)
    if record.status == "Released":
        return record.name
    if record.status not in RESERVED or not (reason or "").strip():
        frappe.throw(_("Only an active or invalid reservation can be released, with a reason."))
    if payment.docstatus == 1:
        frappe.throw(_("For a submitted payment, confirm reflection in a new bank balance instead of releasing it."))
    resolve(record, "Released", reason)
    return record.name


def resolve(record, status, reason):
    record.flags.funding_operation = True
    record.status, record.resolution_reason, record.resolved_by = status, reason, frappe.session.user
    record.save(ignore_permissions=True)


def invalidate_payment(doc, method=None):
    """An edited/cancelled approval loses its authority but keeps its cash reserved until reviewed."""
    for row in reservations(payment_entry=doc.name):
        if row.status == "Active" and (doc.docstatus == 2 or row.payment_fingerprint != payment_signature(doc)):
            record = frappe.get_doc("Payout Reservation", row.name)
            resolve(record, "Invalid", "Payment details changed; review before sending or releasing funds.")
