import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from books_assistant.funding import APPROVERS, lock_bank, latest_snapshot, permitted, resolve
from books_assistant.funding_policy import money


class BankFundingSnapshot(Document):
    def validate(self):
        frappe.only_for(APPROVERS)
        if not self.is_new():
            frappe.throw("Bank balance evidence is immutable. Record a new snapshot instead.")
        bank, account = lock_bank(self.bank_account)
        self.company = account.company
        self.currency = account.account_currency or frappe.get_cached_value("Company", account.company, "default_currency")
        checked = get_datetime(self.balance_as_of)
        if checked > now_datetime():
            frappe.throw("The balance check cannot be in the future.")
        latest = latest_snapshot(bank.name)
        if latest and checked <= get_datetime(latest.balance_as_of):
            frappe.throw("Record a balance checked after the latest snapshot.")
        for key in ("available_balance", "pending_outflows", "minimum_buffer"):
            try:
                amount = money(self.get(key) or 0)
            except ValueError:
                frappe.throw("Enter finite bank balance amounts.")
            if key != "available_balance" and amount < 0:
                frappe.throw("Commitments and the cash buffer cannot be negative.")
            self.set(key, float(amount))
        seen = set()
        for row in self.reflected_reservations or []:
            if row.reservation in seen:
                frappe.throw("A reservation can be reflected only once.")
            seen.add(row.reservation)
            record = permitted("Payout Reservation", row.reservation)
            payment = permitted("Payment Entry", record.payment_entry, current=True)
            record = permitted("Payout Reservation", row.reservation, current=True)
            if record.bank_account != bank.name or record.status not in ("Active", "Invalid"):
                frappe.throw("Only active reservations for this bank can be reflected.")
            if (payment.docstatus != 1 or checked < get_datetime(record.creation)
                    or checked < get_datetime(payment.modified)):
                frappe.throw("Record the sent payment before confirming it is reflected in a newer balance.")
            resolution = row.get("resolution") or "Reflected"
            changed = payment.paid_from != account.name or money(payment.paid_amount) != money(record.amount)
            if resolution == "Not sent from this bank":
                if record.status != "Invalid" or payment.paid_from == account.name:
                    frappe.throw("Only an invalid reservation for a payout recorded against another bank can be resolved as not sent.")
                if not (row.get("resolution_reason") or "").strip():
                    frappe.throw("Confirm with a reason that no transfer was sent from this bank.")
            elif resolution == "Reflected":
                if payment.paid_from != account.name:
                    frappe.throw("Use Not sent from this bank when the recorded payout uses a different bank.")
                if changed and (record.status != "Invalid" or not (row.get("resolution_reason") or "").strip()):
                    frappe.throw("Record a reason confirming the changed payout amount is included in this balance.")
            else:
                frappe.throw("Choose a supported reservation resolution.")
            if payment.company != account.company or payment.paid_from_account_currency != self.currency:
                frappe.throw("The recorded payout must use this company's currency.")

    def after_insert(self):
        for row in self.reflected_reservations or []:
            status = "Released" if row.get("resolution") == "Not sent from this bank" else "Reflected"
            resolve(frappe.get_doc("Payout Reservation", row.reservation), status,
                    f"Resolved with bank snapshot {self.name}: {row.get('resolution_reason') or self.notes}")

    def on_trash(self):
        frappe.throw("Bank balance snapshots are audit records and cannot be deleted.")
