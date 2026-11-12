"""Emailing client invoices: who receives them, which PDF layout, and noticing when an email bounces.

Invoices are emailed when a Billing Approver submits them (Automation Settings can switch that off), and any
time with the Email Invoice button. Each email goes through ERPNext's own Communication, so it appears on the
invoice's timeline.
"""

import re

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime, validate_email_address

from books_assistant.setup import BILLING_APPROVER

DEFAULT_SUBJECT = "Invoice {{ doc.name }} from 508.dev"
DEFAULT_BODY = (
    "<p>Hello,</p><p>Please find attached invoice {{ doc.name }} from 508.dev for "
    "{{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}, due {{ frappe.utils.formatdate(doc.due_date) }}."
    "</p><p>Payment details are on the invoice. Please include the invoice number {{ doc.name }} in your payment "
    "reference so we can match it.</p><p>Thank you,<br>508.dev</p>"
)
BOUNCE_SENDER = re.compile(r"mailer-daemon|postmaster|mail delivery (?:system|subsystem)", re.I)
BOUNCE_SUBJECT = re.compile(
    r"undeliver|delivery status notification|returned to sender|delivery (?:has )?failed|failure notice|"
    r"could not be delivered|mail delivery failed|delivery failure",
    re.I,
)


def settings():
    return frappe.get_cached_doc("Automation Settings")


def split_emails(text):
    """Valid addresses from text separated by commas, semicolons or new lines, in order, without duplicates."""
    found = []
    for part in re.split(r"[,;\n]+", text or ""):
        email = part.strip()
        if email and validate_email_address(email) and email.lower() not in (f.lower() for f in found):
            found.append(email)
    return found


def recipients(doc):
    """(to, cc) for a Sales Invoice."""
    customer = frappe.db.get_value("Customer", doc.customer, ["invoice_to", "invoice_cc", "email_id"], as_dict=True) or {}
    to = split_emails(customer.get("invoice_to")) or split_emails(doc.contact_email) or _contact_emails(doc.customer)
    if not to:
        to = split_emails(customer.get("email_id"))
    cc = split_emails(customer.get("invoice_cc")) + split_emails(settings().always_cc)
    to_lower = {t.lower() for t in to}
    return to, [c for c in dict.fromkeys(cc) if c.lower() not in to_lower]


def _contact_emails(customer):
    rows = frappe.db.sql(
        """
        select c.email_id from `tabContact` c
        join `tabDynamic Link` dl on dl.parent = c.name and dl.parenttype = 'Contact'
        where dl.link_doctype = 'Customer' and dl.link_name = %s and ifnull(c.email_id, '') != ''
        order by c.is_billing_contact desc, c.is_primary_contact desc, c.modified desc
        limit 1
        """,
        customer,
    )
    return split_emails(rows[0][0]) if rows else []


def print_format(doc):
    """The customer's own layout, else the timesheet layout for invoices with hours, else the default."""
    s = settings()
    chosen = frappe.db.get_value("Customer", doc.customer, "invoice_print_format")
    if not chosen and doc.get("timesheets") and s.timesheet_print_format:
        chosen = s.timesheet_print_format
    return chosen or s.invoice_print_format or None


def _render(template, default, doc):
    return frappe.render_template(template or default, {"doc": doc})


def on_submit(doc, method):
    """Email the invoice when a Billing Approver submits it. A failure never stops the invoice being submitted:
    it's noted on the invoice, which then shows under "never emailed" in Automation Settings."""
    if doc.is_return or BILLING_APPROVER not in frappe.get_roles():
        return
    frappe.db.savepoint("invoice_email")
    try:
        if not settings().send_invoice_emails:
            return
        send(doc)
    except Exception as e:
        frappe.db.rollback(save_point="invoice_email")
        frappe.clear_last_message()
        reason = frappe.utils.strip_html(str(e)) or repr(e)
        frappe.log_error(f"Invoice email for {doc.name} failed", reference_doctype=doc.doctype, reference_name=doc.name)
        doc.add_comment("Comment", _("Not emailed: {0}. Use Email Invoice to send it.").format(reason))
        frappe.msgprint(_("{0} was submitted but not emailed: {1}").format(doc.name, reason), indicator="orange",
                        alert=True)


@frappe.whitelist()
def email_defaults(sales_invoice):
    """Recipients, subject, message and layout to prefill the Email Invoice dialog."""
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    doc.check_permission("email")
    to, cc = recipients(doc)
    s = settings()
    return {
        "recipients": ", ".join(to),
        "cc": ", ".join(cc),
        "subject": _render(s.email_subject, DEFAULT_SUBJECT, doc),
        "message": _render(s.email_body, DEFAULT_BODY, doc),
        "print_format": print_format(doc),
    }


def send(doc):
    """Email the invoice with its PDF. Returns the Communication name, or None if the customer has no address."""
    to, cc = recipients(doc)
    if not to:
        frappe.msgprint(
            _("{0} was not emailed: {1} has no invoice email address. Add one under Invoice Emails on the "
              "customer, then use Email Invoice.").format(doc.name, doc.customer),
            indicator="orange",
            alert=True,
        )
        return None
    from frappe.core.doctype.communication.email import make

    s = settings()
    result = make(
        doctype="Sales Invoice",
        name=doc.name,
        recipients=", ".join(to),
        cc=", ".join(cc) or None,
        subject=_render(s.email_subject, DEFAULT_SUBJECT, doc),
        content=_render(s.email_body, DEFAULT_BODY, doc),
        send_email=True,
        print_format=print_format(doc),
        print_letterhead=True,
    )
    frappe.msgprint(_("Emailed {0} to {1}").format(doc.name, ", ".join(to)), alert=True, indicator="green")
    return result.get("name")


# -------------------------------------------------------------------- bounces


def check_for_bounce(doc, method):
    """When a bounce notice arrives in the invoice mailbox, mark the invoice email Bounced, note it on the
    invoice and alert the Billing Approvers. Needs incoming mail switched on for the sending Email Account."""
    if doc.sent_or_received != "Received" or doc.communication_medium != "Email":
        return
    if not (BOUNCE_SENDER.search(doc.sender or "") or BOUNCE_SUBJECT.search(doc.subject or "")):
        return
    original = _bounced_email(doc)
    if not original:
        return
    frappe.db.set_value("Communication", original.name, "delivery_status", "Bounced")
    reason = _bounce_reason(doc.content)
    message = _("The email of {0} to {1} bounced{2}. Check the address under Invoice Emails on the customer, then "
                "use Email Invoice to send it again.").format(
        original.reference_name, original.recipients, f": {reason}" if reason else ""
    )
    frappe.get_doc(original.reference_doctype, original.reference_name).add_comment("Comment", message)
    for user in _approvers():
        frappe.get_doc({
            "doctype": "Notification Log",
            "for_user": user,
            "type": "Alert",
            "subject": _("Invoice email for {0} bounced").format(original.reference_name),
            "email_content": message,
            "document_type": original.reference_doctype,
            "document_name": original.reference_name,
        }).insert(ignore_permissions=True)


def _bounced_email(bounce):
    """The invoice email a bounce refers to: by reply link, by Message-ID, or by the failed address."""
    fields = ["name", "reference_doctype", "reference_name", "recipients"]
    if bounce.in_reply_to:
        row = frappe.db.get_value("Communication", bounce.in_reply_to, fields, as_dict=True)
        if row and row.reference_doctype == "Sales Invoice":
            return row
    content = bounce.content or ""
    for message_id in re.findall(r"<([^<>\s@]+@[^<>\s]+)>", content):
        communication = frappe.db.get_value("Email Queue", {"message_id": message_id}, "communication")
        row = communication and frappe.db.get_value("Communication", communication, fields, as_dict=True)
        if row and row.reference_doctype == "Sales Invoice":
            return row
    own = (frappe.db.get_value("Email Account", bounce.email_account, "email_id") or "").lower()
    for address in dict.fromkeys(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", content)):
        if address.lower() == own or BOUNCE_SENDER.search(address):
            continue
        row = frappe.db.get_value(
            "Communication",
            {"reference_doctype": "Sales Invoice", "sent_or_received": "Sent", "recipients": ["like", f"%{address}%"],
             "creation": [">", add_days(now_datetime(), -14)]},
            fields,
            as_dict=True,
            order_by="creation desc",
        )
        if row:
            return row
    return None


def _bounce_reason(content):
    text = re.sub(r"</?[a-zA-Z][^<>@]*>", " ", content or "")  # HTML tags, not <address@example.com>
    match = re.search(r"\b(?:5\d\d|4\d\d)[ -]\d\.\d\.\d+[^\n]{0,160}", text) or re.search(r"(?i)(?:said|reason):[^\n]{0,160}", text)
    return " ".join(match.group(0).split()) if match else ""


def _approvers():
    """Billing Approvers, or Accounts Managers until anyone has that role."""
    for role in (BILLING_APPROVER, "Accounts Manager"):
        users = frappe.get_all(
            "User",
            filters={"enabled": 1, "user_type": "System User", "name": ["in", frappe.get_all(
                "Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")]},
            pluck="name",
        )
        users = [u for u in users if u not in ("Administrator", "Guest")]
        if users:
            return users
    return ["Administrator"]
