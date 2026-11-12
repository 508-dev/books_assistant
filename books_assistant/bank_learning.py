"""Learn recurring classifications from reconciled books without authorizing bookings.

Historical entries may predate provenance tracking. Enabling auto_book is a
separate human review of specific evidence, never a side effect of learning.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from books_assistant.matching import vendor_key

MIN_EVIDENCE = 3
MIN_DATES = 2
HISTORY_DAYS = 180
FRESHNESS_DAYS = 60
EXCLUDED_ACCOUNT_TYPES = {"Cash", "Bank", "Receivable", "Payable", "Tax", "Chargeable"}
EXCLUDED_KEYWORDS = {"WISE", "STRIPE", "PAYPAL", "VENMO", "BILL.COM", "MERCURY", "INTUIT", "508.DEV", "508.DEV LLC", "BILL", "508"}


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def infer_candidates(observations, today):
    """Require consistent independent lines, not repeated joins or popularity votes."""
    today = as_date(today)
    grouped = defaultdict(lambda: defaultdict(dict))
    blocked = set()
    for row in observations:
        key = vendor_key(row["description"])
        posted = as_date(row["date"])
        if (not key or key in EXCLUDED_KEYWORDS or row.get("generated")
                or not today - timedelta(days=HISTORY_DAYS) <= posted <= today):
            continue
        scope = (row["company"], row["currency"], row["bank_account"], key, row["direction"])
        if row.get("ineligible"):
            blocked.add(scope)
            continue
        grouped[scope][row["account"]][row["bank_transaction"]] = row
    candidates = []
    for scope, accounts in sorted(grouped.items()):
        if scope in blocked or len(accounts) != 1:
            continue  # even a minority disagreement is a material ambiguity
        account, sources = next(iter(accounts.items()))
        rows = list(sources.values())
        dates = {as_date(r["date"]) for r in rows}
        if (len(rows) < MIN_EVIDENCE or len(dates) < MIN_DATES
                or max(dates) < today - timedelta(days=FRESHNESS_DAYS)):
            continue
        company, currency, bank_account, keyword, direction = scope
        candidates.append({
            "company": company, "currency": currency, "bank_account": bank_account, "keyword": keyword,
            "direction": direction, "account": account, "evidence_count": len(rows),
            "last_evidence_on": str(max(dates)), "min_observed": min(r["amount"] for r in rows),
            "max_observed": max(r["amount"] for r in rows),
            "evidence": sorted(rows, key=lambda r: r["bank_transaction"]),
        })
    return candidates


def load_observations(scope, *, history_days=HISTORY_DAYS, for_update=True, max_rows=None, bank_account=None, direction=None):
    """Only simple fully reconciled bank-to-expense/income journals are evidence.

    All source access is checked, and any generated rule journal is excluded by
    journal ID, including when subsequently attached to another bank transaction.
    """
    import frappe
    from frappe.utils import add_days, flt, today
    from books_assistant.integration import check_document, lock_documents

    if not isinstance(history_days, int) or not 1 <= history_days <= 3650:
        raise ValueError("history_days must be between 1 and 3650")
    if max_rows is not None and (not isinstance(max_rows, int) or not 1 <= max_rows <= 10000):
        raise ValueError("max_rows must be between 1 and 10000")
    if direction not in (None, "Money in", "Money out"):
        raise ValueError("unsupported history direction")
    bank_filter = " and bt.bank_account = %(bank_account)s" if bank_account else ""
    direction_filter = " and bt.deposit > 0" if direction == "Money in" else " and bt.withdrawal > 0" if direction else ""
    suffix = " for update" if for_update else ""
    limit = f" order by bt.date desc, bt.name limit {max_rows}" if max_rows else ""
    params = {**scope, "since": add_days(today(), -history_days), "today": today(), "bank_account": bank_account}
    rows = frappe.db.sql(
        """
        select distinct bt.name bank_transaction, bt.description, bt.date, bt.currency, bt.deposit, bt.withdrawal,
               bt.unallocated_amount, bt.bank_account, ba.company, ba.account own_account,
               btp.payment_document, btp.payment_entry, btp.allocated_amount,
               je.name journal, je.docstatus journal_status, je.company journal_company,
               bt.modified bank_modified, je.modified journal_modified,
               (select count(*) from `tabBank Transaction Payments` p where p.parent = bt.name) mapping_count,
               (select count(*) from `tabBank Transaction Payments` p where p.payment_document='Journal Entry'
                  and p.payment_entry=je.name) journal_links
        from `tabBank Transaction` bt
        join `tabBank Account` ba on ba.name = bt.bank_account and ba.is_company_account = 1
        join `tabBank Transaction Payments` btp on btp.parent = bt.name
        left join `tabJournal Entry` je on je.name = btp.payment_entry and btp.payment_document='Journal Entry'
        left join `tabBank Rule Booking` generated on generated.journal_entry=je.name
        where bt.docstatus = 1 and ba.company = %(company)s and bt.currency = %(currency)s
          and bt.date between %(since)s and %(today)s
          and generated.name is null
        """ + bank_filter + direction_filter + limit + suffix, params, as_dict=True,
    )
    observations = []
    for row in rows:
        amount = flt(row.deposit) or flt(row.withdrawal)
        source = {
            "bank_transaction": row.bank_transaction, "journal": row.journal, "description": row.description,
            "date": str(row.date), "company": scope["company"], "currency": scope["currency"],
            "bank_account": row.bank_account, "direction": "Money in" if flt(row.deposit) > 0 else "Money out",
            "amount": amount, "bank_modified": str(row.bank_modified), "journal_modified": str(row.journal_modified),
        }
        # Keep a rejection marker. Ignoring a split or inaccessible contradictory
        # booking would make the remaining history look falsely unanimous.
        rejection = {**source, "ineligible": True}
        if (row.payment_document != "Journal Entry" or row.journal_status != 1 or row.journal_company != scope["company"]
                or row.mapping_count != 1 or row.journal_links != 1 or not 0 <= flt(row.unallocated_amount) < 0.005
                or abs(flt(row.allocated_amount) - amount) >= 0.005):
            observations.append(rejection)
            continue
        try:
            if for_update:
                lock_documents([("Bank Transaction", row.bank_transaction), ("Journal Entry", row.journal),
                                ("Bank Account", row.bank_account)])
            check_document("Bank Transaction", row.bank_transaction, company=scope["company"], currency=scope["currency"])
            check_document("Bank Account", row.bank_account, company=scope["company"])
            journal = check_document("Journal Entry", row.journal, company=scope["company"])
            mappings = frappe.db.sql("select name from `tabBank Transaction Payments` where parent=%s" + suffix, (row.bank_transaction,))
            links = frappe.db.sql("select name from `tabBank Transaction Payments` where payment_document='Journal Entry' and payment_entry=%s" + suffix, (row.journal,))
            if len(mappings) != 1 or len(links) != 1:
                observations.append(rejection)
                continue
            accounts = frappe.db.sql("select * from `tabJournal Entry Account` where parent=%s order by idx" + suffix,
                                     (row.journal,), as_dict=True)
            if (len(accounts) != 2 or journal.get("is_opening") == "Yes" or journal.get("tax_withholding_category")
                    or any(a.get("party") or a.get("party_type") or a.get("reference_type") or a.get("reference_name") for a in accounts)):
                observations.append(rejection)
                continue
            bank_rows = [a for a in accounts if a.account == row.own_account]
            other_rows = [a for a in accounts if a.account != row.own_account]
            if len(bank_rows) != 1 or len(other_rows) != 1:
                observations.append(rejection)
                continue
            bank, contra = bank_rows[0], other_rows[0]
            if for_update:
                lock_documents([("Account", contra.account), ("Account", bank.account)])
            ledger = check_document("Account", contra.account, company=scope["company"], currency=scope["currency"])
            bank_ledger = check_document("Account", bank.account, company=scope["company"], currency=scope["currency"])
            amount = flt(row.deposit) or flt(row.withdrawal)
            money_in = flt(row.deposit) > 0
            if (amount <= 0 or bool(flt(row.deposit)) == bool(flt(row.withdrawal))
                    or not eligible_ledger(ledger) or bank_ledger.account_type != "Bank"
                    or bank_ledger.disabled or bank_ledger.is_group
                    or any(a.account_currency != scope["currency"] or flt(a.exchange_rate) != 1 for a in accounts)):
                observations.append(rejection)
                continue
            if money_in:
                amounts = (flt(bank.debit_in_account_currency), flt(contra.credit_in_account_currency))
                opposite = (flt(bank.credit_in_account_currency), flt(contra.debit_in_account_currency))
            else:
                amounts = (flt(bank.credit_in_account_currency), flt(contra.debit_in_account_currency))
                opposite = (flt(bank.debit_in_account_currency), flt(contra.credit_in_account_currency))
            if any(abs(value - amount) >= 0.005 for value in amounts) or any(opposite):
                observations.append(rejection)
                continue
            observations.append({**source, "account": contra.account})
        except frappe.PermissionError:
            observations.append(rejection)
    return observations


def eligible_ledger(ledger):
    return (ledger.get("root_type") in {"Income", "Expense"} and not ledger.get("is_group")
            and not ledger.get("disabled") and ledger.get("account_type") not in EXCLUDED_ACCOUNT_TYPES)


def candidate_for_rule(rule, observations, today, scope=None):
    for candidate in infer_candidates(observations, today):
        if scope and (candidate["company"] != scope["company"] or candidate["currency"] != scope["currency"]):
            continue
        if (candidate["keyword"] == (rule.description_contains or "").strip().upper()
                and candidate["bank_account"] == rule.bank_account and candidate["account"] == rule.account
                and candidate["direction"] == rule.direction):
            return candidate
    return None


def learn(scope, observations=None):
    """Enable new suggestions with enough evidence; never enable automatic booking."""
    import frappe
    from frappe.utils import today
    from books_assistant.bank_review import unique_rule_name

    observations = load_observations(scope) if observations is None else observations
    proposed, updated = [], []
    from books_assistant.integration import check_document, lock_documents

    for candidate in infer_candidates(observations, today()):
        matching = frappe.db.sql(
            "select name, bank_account from `tabBank Rule` where upper(trim(description_contains))=%(keyword)s "
            "and direction=%(direction)s and (bank_account=%(bank_account)s or bank_account is null or bank_account='') "
            "order by name for update", candidate, as_dict=True)
        evidence = {"evidence_count": candidate["evidence_count"], "last_evidence_on": candidate["last_evidence_on"],
                    "source_transactions": "\n".join(r["bank_transaction"] for r in candidate["evidence"])}
        if matching:
            for row in matching:
                # Any existing disabled/broad/hidden rule is a deliberate decision.
                # Only refresh explanatory metadata on an accessible learned rule.
                lock_documents([("Bank Rule", row.name)])
                if not frappe.has_permission("Bank Rule", doc=row.name, ptype="read"):
                    continue
                rule = check_document("Bank Rule", row.name)
                if (rule.learned and rule.account == candidate["account"] and rule.bank_account == candidate["bank_account"]
                        and frappe.has_permission("Bank Rule", doc=rule, ptype="write")):
                    rule.db_set(evidence, update_modified=False)
                    updated.append(row.name)
            continue
        rule = frappe.get_doc({
            "doctype": "Bank Rule", "rule_name": unique_rule_name(candidate["keyword"].title() + " (learned)"),
            "enabled": 1, "learned": 1, "auto_book": 0,
            "description_contains": candidate["keyword"], "direction": candidate["direction"],
            "bank_account": candidate["bank_account"], "account": candidate["account"],
            "min_amount": candidate["min_observed"], "max_amount": candidate["max_observed"],
            "times_used": 0, **evidence,
            "notes": "Suggested from consistent reconciled historical evidence. Automatic booking requires separate review and authorization.",
        }).insert()
        proposed.append(rule.name)
    return {"rules_created": proposed, "rules_updated": updated}
