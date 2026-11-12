import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from books_assistant.matching import keyword_in

# Supplier and customer payments are matched by the queue itself, against their invoices.
NOT_FOR_RULES = ("Cash", "Receivable", "Payable")


class BankRule(Document):
    def validate(self):
        self._lock_companies()
        account = frappe.db.get_value("Account", self.account, ["account_type", "is_group"], as_dict=True)
        if account.is_group:
            frappe.throw(_("Choose a ledger account, not a group."))
        if account.account_type in NOT_FOR_RULES:
            frappe.throw(
                _("A rule can't book to a {0} account. Supplier and customer payments are matched against their "
                  "invoices by the Bank Review queue without a rule.").format(account.account_type)
            )
        if account.account_type == "Bank" and not frappe.db.exists(
            "Bank Account", {"account": self.account, "is_company_account": 1}
        ):
            frappe.throw(_("A rule can only book to a bank account that is one of our own (a transfer)."))
        if self.max_amount and flt(self.min_amount) > flt(self.max_amount):
            frappe.throw(_("Minimum Amount is more than Maximum Amount."))
        self.description_contains = "|".join(
            part.strip() for part in (self.description_contains or "").split("|") if part.strip()
        )

        from books_assistant.bank_automation import validate_auto_rule

        validate_auto_rule(self)

    def _lock_companies(self):
        """Serialize all rule edits with automatic and manually approved bookings.

        Company locks also protect against a newly inserted overlapping rule on
        databases whose isolation level does not retain range/gap locks.
        """
        previous = self.get_doc_before_save()
        accounts = {self.account}
        if previous and previous.get("account"):
            accounts.add(previous.account)
        companies = {frappe.db.get_value("Account", account, "company") for account in accounts if account}
        for company in sorted(c for c in companies if c):
            frappe.db.sql("select name from `tabCompany` where name=%s for update", (company,))

    def on_trash(self):
        self._lock_companies()

    def before_rename(self, old_name, new_name, merge=False):
        self._lock_companies()

    def specificity(self, transaction):
        """Length of the longest of this rule's words that appears in the bank description."""
        return max((len(part.strip()) for part in self.description_contains.split("|")
                    if part.strip() and keyword_in(part, transaction.description)), default=0)

    def matches(self, transaction, bank_account):
        """Whether this rule applies to a Bank Transaction (as a dict)."""
        amount = flt(transaction.deposit) or flt(transaction.withdrawal)
        money_in = flt(transaction.deposit) > 0
        if (self.direction == "Money out" and money_in) or (self.direction == "Money in" and not money_in):
            return False
        if self.bank_account and self.bank_account != bank_account:
            return False
        if self.min_amount and amount < flt(self.min_amount):
            return False
        if self.max_amount and amount > flt(self.max_amount):
            return False
        if self.account == frappe.db.get_value("Bank Account", bank_account, "account"):
            return False  # a transfer to the account the money is already in
        return any(keyword_in(part, transaction.description) for part in self.description_contains.split("|"))
