"""Engineer and referral bills held until the client pays: releasing them once the client invoice is paid."""

import frappe
from frappe import _


def release_paid_holds(*args, **kwargs):
    """Release held bills whose client invoice is fully paid. Runs after payments and hourly, so payments
    applied through Payment Reconciliation are caught too."""
    if not frappe.db.get_single_value("Automation Settings", "auto_release_holds"):
        return
    rows = frappe.db.sql(
        """
        select pi.name, pi.client_invoice from `tabPurchase Invoice` pi
        join `tabSales Invoice` si on si.name = pi.client_invoice
        where pi.docstatus = 1 and pi.on_hold = 1 and si.docstatus = 1 and si.outstanding_amount <= 0.009
        """,
        as_dict=True,
    )
    for row in rows:
        bill = frappe.get_doc("Purchase Invoice", row.name)
        bill.db_set({"on_hold": 0, "release_date": None})
        bill.add_comment("Comment", _("Released for payment: the client paid {0}.").format(row.client_invoice))
