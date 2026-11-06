from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


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
    "Customer": [_bank_names("default_bank_account")],
}


def create_fields():
    create_custom_fields(CUSTOM_FIELDS, update=True)
