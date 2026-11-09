import frappe
from frappe.installer import update_site_config
from frappe.model.document import Document
from frappe.utils import add_days, flt, today

from books_assistant.invoice_email import split_emails

# erp_rules reads site config "erp_rules_disabled_checks"; books_health reads "books_health_recipients".
# These fields mirror them, so both apps keep working without depending on this one.
ERP_RULES_CHECKS = {
    "check_discount_reason": "DiscountReasonCheck",
    "check_hours_as_quantity": "HoursAsQuantityCheck",
    "check_due_date": "DueDateCheck",
    "check_no_cash_account": "NoCashAccountCheck",
    "check_duplicate_payment": "DuplicatePaymentCheck",
    "check_client_paid": "ClientPaidCheck",
}


class AutomationSettings(Document):
    def onload(self):
        load_site_config(self)
        self.set_onload("status", status())

    def validate(self):
        for field in ("always_cc", "health_digest_recipients"):
            self.set(field, "\n".join(split_emails(self.get(field))))

    def on_update(self):
        disabled = [check for field, check in ERP_RULES_CHECKS.items() if not self.get(field)]
        if sorted(frappe.conf.get("erp_rules_disabled_checks") or []) != sorted(disabled):
            update_site_config("erp_rules_disabled_checks", disabled)
        recipients = split_emails(self.health_digest_recipients)
        if (frappe.conf.get("books_health_recipients") or []) != recipients:
            update_site_config("books_health_recipients", recipients)


def load_site_config(doc):
    disabled = set(frappe.conf.get("erp_rules_disabled_checks") or [])
    for field, check in ERP_RULES_CHECKS.items():
        doc.set(field, 0 if check in disabled else 1)
    doc.health_digest_recipients = "\n".join(frappe.conf.get("books_health_recipients") or [])


def status():
    """What the automations are doing and what needs a person, for the top of the settings page."""
    since = add_days(today(), -30)
    held = frappe.db.sql(
        "select count(*), coalesce(sum(outstanding_amount), 0), count(distinct client_invoice) "
        "from `tabPurchase Invoice` where docstatus = 1 and on_hold = 1"
    )[0]
    unsent = frappe.db.sql(
        """
        select si.name from `tabSales Invoice` si
        where si.docstatus = 1 and si.is_return = 0 and si.posting_date >= %s
          and not exists (select 1 from `tabCommunication` c where c.reference_doctype = 'Sales Invoice'
                          and c.reference_name = si.name and c.sent_or_received = 'Sent'
                          and c.communication_medium = 'Email')
        order by si.posting_date desc
        """,
        since,
        pluck=True,
    )
    failed = frappe.get_all(
        "Communication",
        filters={"reference_doctype": "Sales Invoice", "sent_or_received": "Sent", "creation": [">=", since],
                 "delivery_status": ["in", ["Bounced", "Soft-Bounced", "Error", "Rejected", "Expired"]]},
        fields=["reference_name", "recipients", "delivery_status"],
    )
    digest = frappe.get_all(
        "Email Queue", filters={"message": ["like", "%Books Health report%"]}, fields=["creation", "status"],
        order_by="creation desc", limit=1,
    )
    return {
        "bank_lines": frappe.db.count("Bank Transaction", {"docstatus": 1, "unallocated_amount": [">", 0.009]}),
        "bank_rules": frappe.db.count("Bank Rule", {"enabled": 1}),
        "held_bills": held[0],
        "held_amount": flt(held[1], 2),
        "held_waiting_on": held[2],
        "unsent_invoices": unsent,
        "failed_emails": failed,
        "last_digest": digest[0] if digest else None,
        "digest_on": bool(frappe.conf.get("books_health_recipients")),
    }
