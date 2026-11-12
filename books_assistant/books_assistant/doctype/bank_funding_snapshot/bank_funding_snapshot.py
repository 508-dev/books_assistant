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
            if payment.docstatus != 1 or checked < get_datetime(record.creation):
                frappe.throw("Record the sent payment before confirming it is reflected in a newer balance.")
            if payment.paid_from != account.name or money(payment.paid_amount) != money(record.amount):
                frappe.throw("The recorded payment must match the reserved bank and amount.")

    def after_insert(self):
        for row in self.reflected_reservations or []:
            resolve(frappe.get_doc("Payout Reservation", row.reservation), "Reflected",
                    f"Confirmed deducted in bank snapshot {self.name}: {self.notes}")

    def on_trash(self):
        frappe.throw("Bank balance snapshots are audit records and cannot be deleted.")
