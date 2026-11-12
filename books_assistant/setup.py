import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

BILLING_APPROVER = "Billing Approver"
HOURLY = "Hourly from timesheets"
RETAINER = "Retainer (invoiced separately)"


def _bank_names(after):
    return {
        "fieldname": "bank_names",
        "label": "Names on Bank Statements",
        "fieldtype": "Small Text",
        "insert_after": after,
        "description": "How this party appears in bank descriptions, one per line (e.g. a legal name, a "
        "spelling variant, or a name in another script). The bank review queue uses these to recognise them.",
        "module": "Books Assistant",
    }


CUSTOM_FIELDS = {
    "Supplier": [_bank_names("default_bank_account")],
    "Customer": [
        _bank_names("default_bank_account"),
        {
            "fieldname": "invoicing_section",
            "label": "Invoice Emails",
            "fieldtype": "Section Break",
            "insert_after": "bank_names",
            "collapsible": 1,
            "module": "Books Assistant",
        },
        {
            "fieldname": "invoice_to",
            "label": "Send Invoices To",
            "fieldtype": "Small Text",
            "insert_after": "invoicing_section",
            "description": "Email addresses, separated by commas or new lines. If empty, the customer's billing "
            "(or primary) contact is used.",
            "module": "Books Assistant",
        },
        {
            "fieldname": "invoice_cc",
            "label": "CC Invoices To",
            "fieldtype": "Small Text",
            "insert_after": "invoice_to",
            "description": "Email addresses copied on this customer's invoices, separated by commas or new lines.",
            "module": "Books Assistant",
        },
        {
            "fieldname": "invoice_print_format",
            "label": "Invoice Print Format",
            "fieldtype": "Link",
            "options": "Print Format",
            "insert_after": "invoice_cc",
            "description": "The PDF layout for this customer's invoices. If empty, the default from Automation "
            "Settings is used.",
            "module": "Books Assistant",
        },
    ],
    "Project": [
        {
            "fieldname": "billing_method",
            "label": "Billing Method",
            "fieldtype": "Select",
            "options": f"{HOURLY}\n{RETAINER}",
            "default": HOURLY,
            "insert_after": "customer",
            "description": "Hourly: the monthly Billing Run invoices the client from approved timesheets. Retainer: "
            "the client is invoiced separately (e.g. a repeating invoice); the run only prepares the engineers' bills.",
            "module": "Books Assistant",
        }
    ],
    "Purchase Invoice": [
        {
            "fieldname": "client_invoice",
            "label": "Paid After Client Pays",
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "insert_after": "on_hold",
            "read_only": 1,
            "allow_on_submit": 1,
            "description": "This bill is on hold until the client pays this invoice, then released automatically. "
            "Unblock it by hand to pay earlier.",
            "module": "Books Assistant",
        }
    ],
}


def create_fields():
    create_custom_fields(CUSTOM_FIELDS, update=True)
    if not frappe.db.exists("Role", BILLING_APPROVER):
        frappe.get_doc(
            {"doctype": "Role", "role_name": BILLING_APPROVER, "desk_access": 1, "is_custom": 1}
        ).insert(ignore_permissions=True)
    initialise_settings()


def initialise_settings():
    """Give Automation Settings its defaults the first time, reading the switches that live in site config
    (erp_rules checks, books_health recipients) so existing choices are kept.

    `bench install-app` can run this before the app's doctypes are synced (when the bench's cached module map
    predates the app), so it waits for the migrate that follows instead of failing the install."""
    if not frappe.db.exists("DocType", "Automation Settings"):
        return
    # tabSingles has no `name` column, so db.exists (which selects name)
    # cannot reliably test its rows. Never reset saved switches on migrate.
    if frappe.db.sql(
        "select 1 from `tabSingles` where doctype = %s limit 1", "Automation Settings"
    ):
        # Numeric singleton reads cast an absent field to zero; inspect its row.
        if not frappe.db.sql(
            "select 1 from `tabSingles` where doctype = %s and field = %s limit 1",
            ("Automation Settings", "funding_balance_max_age_hours"),
        ):
            frappe.db.set_single_value("Automation Settings", "funding_balance_max_age_hours", 4)
        return
    from books_assistant.books_assistant.doctype.automation_settings.automation_settings import load_site_config

    settings = frappe.get_single("Automation Settings")
    for df in settings.meta.fields:
        if df.default is None or df.fieldtype in ("Section Break", "Column Break", "HTML"):
            continue
        missing = df.fieldtype == "Link" and not frappe.db.exists(df.options, df.default)
        # e.g. a print format this site doesn't have: leave it empty, so ERPNext's own default is used
        settings.set(df.fieldname, None if missing else df.default)
    load_site_config(settings)
    settings.save(ignore_permissions=True)
