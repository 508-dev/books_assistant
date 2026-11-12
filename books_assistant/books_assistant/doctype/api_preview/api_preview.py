from frappe.model.document import Document


class APIPreview(Document):
    """Internal approval state; no generic user CRUD permissions."""

    pass
