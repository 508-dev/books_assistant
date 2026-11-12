app_name = "books_assistant"
app_title = "Books Assistant"
app_publisher = "508.dev"
app_description = (
    "Prepares bookkeeping work for approval: a bank review queue, supplier payouts, the monthly billing run and "
    "invoice emails."
)
app_email = "admin@508.dev"
app_license = "mit"

after_install = "books_assistant.setup.create_fields"
after_migrate = ["books_assistant.setup.create_fields"]

app_include_js = ["/assets/books_assistant/js/pay_supplier.js"]

doctype_js = {
    "Supplier": "public/js/supplier.js",
    "Sales Invoice": "public/js/sales_invoice.js",
    "Payment Entry": "public/js/payment_entry.js",
}
doctype_list_js = {"Purchase Invoice": "public/js/purchase_invoice_list.js"}

doc_events = {
    "Sales Invoice": {"on_submit": "books_assistant.invoice_email.on_submit"},
    # A payment may finish paying a client invoice, releasing the engineer bills waiting on it.
    "Payment Entry": {"on_submit": "books_assistant.holds.release_after_payment"},
    "Journal Entry": {"on_submit": "books_assistant.holds.release_after_payment"},
    "Communication": {"after_insert": "books_assistant.invoice_email.check_for_bounce"},
}

scheduler_events = {
    # Catches payments applied without a new Payment Entry (e.g. Payment Reconciliation).
    "hourly": ["books_assistant.holds.release_paid_holds"],
}
