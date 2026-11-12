import frappe
from frappe.model.document import Document


class PayoutReservation(Document):
    def validate(self):
        if not self.flags.funding_operation:
            frappe.throw("Use the funding approval and release actions to change a reservation.")

    def on_trash(self):
        frappe.throw("Funding reservations are audit records and cannot be deleted.")
