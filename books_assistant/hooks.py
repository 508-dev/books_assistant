app_name = "books_assistant"
app_title = "Books Assistant"
app_publisher = "508.dev"
app_description = "Prepares bookkeeping work for approval: a bank review queue with bulk approve, and supplier payouts."
app_email = "admin@508.dev"
app_license = "mit"

after_install = "books_assistant.setup.create_fields"
after_migrate = ["books_assistant.setup.create_fields"]

app_include_js = ["/assets/books_assistant/js/pay_supplier.js"]

doctype_js = {"Supplier": "public/js/supplier.js"}
doctype_list_js = {"Purchase Invoice": "public/js/purchase_invoice_list.js"}
