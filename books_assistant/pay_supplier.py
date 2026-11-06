"""Pay a supplier what we owe: earlier credit is applied to their bills first, then a payment covers the rest.

Credit means anything already in the supplier's favour that isn't applied to a bill: an earlier payment that was
more than the bill (or made before it), or a debit note. The payment opens unsaved, ready to check and submit.

Its reference says what the payment is (e.g. "Novo payout 2026-11-06 Jane Doe"), because some banks (Novo) show no
transfer number to copy. Once Bank Review matches the payment to its bank line, the line links the bank's own ID.
"""

import frappe
from frappe import _
from frappe.utils import add_months, flt, today

APPROVERS = ("Accounts Manager", "System Manager")


@frappe.whitelist()
def preview(supplier, invoices=None):
    frappe.only_for(APPROVERS)
    bills = open_bills(supplier, frappe.parse_json(invoices) if invoices else None)
    credits = credit_entries(supplier)
    bills_total = flt(sum(b.outstanding_amount for b in bills), 2)
    credit_total = flt(sum(c.amount for c in credits), 2)
    return {
        "bills": bills,
        "credits": credits,
        "bills_total": bills_total,
        "credit_total": credit_total,
        "credit_to_apply": min(bills_total, credit_total),
        "to_pay": flt(max(bills_total - credit_total, 0), 2),
        "bank_account": usual_bank_account(supplier),
    }


@frappe.whitelist()
def prepare(supplier, invoices=None, bank_account=None):
    """Apply credit to the bills, then return an unsaved payment for whatever is still open on them."""
    frappe.only_for(APPROVERS)
    names = [b.name for b in open_bills(supplier, frappe.parse_json(invoices) if invoices else None)]
    if not names:
        frappe.throw(_("Nothing is owed to {0}.").format(supplier))
    applied = apply_credit(supplier, names)
    bills = open_bills(supplier, names)
    payment = new_payment(supplier, bills, bank_account or usual_bank_account(supplier)) if bills else None
    return {"payment_entry": payment.as_dict() if payment else None, "credit_applied": applied}


def open_bills(supplier, names=None):
    filters = {"supplier": supplier, "docstatus": 1, "outstanding_amount": [">", 0.009], "is_return": 0}
    if names:
        filters["name"] = ["in", names]
    return frappe.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=["name", "posting_date", "bill_no", "project", "grand_total", "outstanding_amount"],
        order_by="posting_date, name",
    )


def _reconciliation(supplier):
    from erpnext.accounts.party import get_party_account

    company = frappe.defaults.get_user_default("Company") or frappe.get_all("Company", pluck="name", limit=1)[0]
    pr = frappe.get_doc(
        {
            "doctype": "Payment Reconciliation",
            "company": company,
            "party_type": "Supplier",
            "party": supplier,
            "receivable_payable_account": get_party_account("Supplier", supplier, company),
        }
    )
    pr.get_unreconciled_entries()
    return pr


def credit_entries(supplier):
    return [
        frappe._dict(reference_type=p.reference_type, reference_name=p.reference_name, posting_date=p.posting_date,
                     amount=flt(p.amount))
        for p in _reconciliation(supplier).payments
    ]


def apply_credit(supplier, bill_names):
    """Apply the supplier's unapplied credit to these bills, oldest bill and oldest credit first."""
    pr = _reconciliation(supplier)
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


def new_payment(supplier, bills, bank_gl):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

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
    pe.posting_date = pe.reference_date = today()
    pe.reference_no = payout_reference(bank_gl, supplier)
    return pe


def payout_reference(bank_gl, supplier):
    """A reference that identifies the payout, for banks that show no transfer number."""
    bank = (bank_gl or "").split(" - ")[0] or _("Bank")
    return f"{bank} payout {today()} {supplier}"[:140]


def usual_bank_account(supplier):
    """The bank account this supplier has most often been paid from in the past year."""
    row = frappe.db.sql(
        """
        select paid_from, count(*) n from `tabPayment Entry`
        where docstatus = 1 and payment_type = 'Pay' and party_type = 'Supplier' and party = %s and posting_date >= %s
          and paid_from in (select account from `tabBank Account` where is_company_account = 1)
        group by paid_from order by n desc limit 1
        """,
        (supplier, add_months(today(), -12)),
    )
    if row:
        return row[0][0]
    return frappe.db.get_value("Bank Account", {"is_company_account": 1, "is_default": 1}, "account") or frappe.db.get_value(
        "Bank Account", {"is_company_account": 1}, "account"
    )
