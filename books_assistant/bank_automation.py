"""Opt-in recurring bank classifications and deterministic, audited rule bookings."""

import json
import math
from collections import Counter
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, today

from books_assistant import bank_learning as learning
from books_assistant.matching import invoice_refs, keyword_in, mentions

APPROVERS = ("Accounts Manager", "System Manager")
MAX_LINES_PER_RUN = 100
FROZEN_FIELDS = ("description_contains", "direction", "bank_account", "account", "min_amount", "max_amount")


def rule_config(rule):
    return {field: flt(rule.get(field)) if field in {"min_amount", "max_amount"} else rule.get(field)
            for field in FROZEN_FIELDS}


def validate_auto_rule(rule):
    """A separate explicit authorization freezes the scope, bounds and reviewed evidence."""
    from books_assistant.integration import check_document, resolve_scope, canonical, lock_documents

    previous = rule.get_doc_before_save()
    if not rule.get("auto_book"):
        # Client-supplied read-only metadata is never accepted as a future authorization.
        for field in ("auto_approval_json", "auto_approved_by", "auto_approved_on"):
            rule.set(field, previous.get(field) if previous else None)
        return
    if previous and previous.get("auto_book"):
        if rule_config(previous) != rule_config(rule):
            frappe.throw(_("Disable automatic booking and save before changing its words, bank, account, direction or bounds."))
        for field in ("auto_approval_json", "auto_approved_by", "auto_approved_on"):
            rule.set(field, previous.get(field))
        return
    frappe.only_for(APPROVERS)
    if not rule.enabled or not rule.bank_account or rule.direction not in {"Money in", "Money out"}:
        frappe.throw(_("Automatic booking requires an enabled rule, one bank account and one direction."))
    if "|" in (rule.description_contains or ""):
        frappe.throw(_("Automatic booking requires one merchant keyword, without alternatives."))
    lower, upper = flt(rule.min_amount), flt(rule.max_amount)
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 < lower <= upper):
        frappe.throw(_("Review and set positive minimum and maximum amounts before enabling automatic booking."))
    bank = check_document("Bank Account", rule.bank_account)
    scope = resolve_scope(bank.company)
    lock_documents([("Company", scope["company"])])
    account = check_document("Account", rule.account, company=scope["company"], currency=scope["currency"])
    if not bank.is_company_account or not learning.eligible_ledger(account):
        frappe.throw(_("Automatic rules can only book our own bank lines to an active expense or income ledger."))
    evidence = learning.candidate_for_rule(rule, learning.load_observations(scope), today(), scope)
    if not evidence:
        frappe.throw(_("Automatic booking needs three consistent reconciled transactions across at least two dates, with recent evidence."))
    rule.auto_approval_json = canonical({"version": 1, "scope": scope, "rule": rule_config(rule),
                                         "evidence": evidence["evidence"]})
    rule.auto_approved_by = frappe.session.user
    rule.auto_approved_on = now_datetime()
    rule.evidence_count = evidence["evidence_count"]
    rule.source_transactions = "\n".join(r["bank_transaction"] for r in evidence["evidence"])
    rule.last_evidence_on = evidence["last_evidence_on"]


def record_rule_booking(bank_transaction, rule_name, journal_name, automatic=False):
    """Record every generated rule journal, including manually approved rule actions.

    Called in the same transaction as journal creation/reconciliation. Nothing
    commits here. Journal provenance survives cancellation or later rematching.
    """
    from books_assistant.integration import canonical, check_document

    if frappe.db.exists("Bank Rule Booking", bank_transaction):
        previous = frappe.get_doc("Bank Rule Booking", bank_transaction)
        if previous.journal_entry != journal_name or previous.rule != rule_name:
            frappe.throw(_("A different rule booking is already recorded for this bank transaction."))
        return previous.name
    bt = check_document("Bank Transaction", bank_transaction)
    rule = check_document("Bank Rule", rule_name)
    bank = check_document("Bank Account", bt.bank_account)
    journal = check_document("Journal Entry", journal_name, company=bank.company)
    if journal.docstatus != 1 or flt(bt.unallocated_amount) > 0.005:
        frappe.throw(_("Record rule provenance only after the journal is submitted and the bank line is reconciled."))
    is_automatic = bool(automatic or getattr(frappe.flags, "bank_auto_booking", False))
    doc = frappe.get_doc({
        "doctype": "Bank Rule Booking", "bank_transaction": bank_transaction, "rule": rule_name,
        "journal_entry": journal_name, "company": bank.company, "currency": bt.currency,
        "amount": flt(bt.deposit) or flt(bt.withdrawal), "actor": frappe.session.user,
        "automatic": int(is_automatic), "booked_on": now_datetime(),
        "approval_json": rule.get("auto_approval_json") if is_automatic else None,
        "result_json": canonical({"code": "BOOKED", "rule": rule_config(rule), "journal": journal_name}),
    }).insert(ignore_permissions=True)
    return doc.name


def overlaps(rule, bt):
    """Ignore specificity and amount bands when detecting competing destinations."""
    if rule.bank_account and rule.bank_account != bt.bank_account:
        return False
    if rule.direction == "Money in" and not flt(bt.deposit):
        return False
    if rule.direction == "Money out" and not flt(bt.withdrawal):
        return False
    return any(keyword_in(part, bt.description) for part in (rule.description_contains or "").split("|"))


def evidence_still_valid(rule, observations):
    try:
        approved = json.loads(rule.auto_approval_json or "null")
    except (ValueError, TypeError):
        return False
    if (not isinstance(approved, dict) or approved.get("version") != 1 or approved.get("rule") != rule_config(rule)
            or not rule.auto_approved_by or not rule.auto_approved_on):
        return False
    lower, upper = flt(rule.min_amount), flt(rule.max_amount)
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 < lower <= upper):
        return False
    scope = approved.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"company", "currency"}:
        return False
    candidate = learning.candidate_for_rule(rule, observations, today(), scope)
    if not candidate:
        return False
    # New evidence may contradict or invalidate the review, but never broadens it.
    current = {r["bank_transaction"]: r for r in candidate["evidence"]}
    frozen = approved.get("evidence")
    if (not isinstance(frozen, list) or any(not isinstance(r, dict) or not isinstance(r.get("bank_transaction"), str) for r in frozen)
            or len({r["bank_transaction"] for r in frozen}) != len(frozen)):
        return False
    valid = [r for r in frozen if current.get(r["bank_transaction"]) == r]
    return (len(valid) >= learning.MIN_EVIDENCE
            and len({r["date"] for r in valid}) >= learning.MIN_DATES
            and approved.get("scope", {}).get("company") == candidate["company"]
            and approved.get("scope", {}).get("currency") == candidate["currency"])


@contextmanager
def _current_reader():
    """Read committed blockers without locking unrelated business records.

    A separate read-only, autocommit connection sees commits made after the
    caller established a REPEATABLE READ snapshot. The caller's transaction,
    source locks and accounting writes are neither committed nor replaced.
    """
    connection = frappe.db.create_connection()
    try:
        connection.autocommit(True)
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")

        def read(statement, parameters=None, *, as_dict=False):
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                rows = cursor.fetchall()
                if as_dict:
                    columns = [column[0] for column in cursor.description]
                    return [frappe._dict(zip(columns, row)) for row in rows]
                return rows

        yield read
    finally:
        connection.close()


def _existing_candidate(bt, bank_gl, scope):
    with _current_reader() as current:
        def query(statement, parameters=None, *, as_dict=False):
            # Include the caller's own uncommitted documents as well as newly
            # committed external records. A stale extra blocker only defers work.
            own = (frappe.db.sql(statement, as_dict=as_dict) if parameters is None
                   else frappe.db.sql(statement, parameters, as_dict=as_dict))
            fresh = current(statement, parameters, as_dict=as_dict)
            return list(own) + list(fresh)

        return _check_existing_candidate(bt, bank_gl, scope, query)


def _check_existing_candidate(bt, bank_gl, scope, query):
    """Boolean-only raw checks deliberately include hidden accounting candidates.

    A restricted service user must not create a second booking merely because
    an existing voucher, invoice or transfer is outside that user's record view.
    """
    from books_assistant.matching import bank_name

    amount = flt(bt.deposit) or flt(bt.withdrawal)
    if bt.get("party") or bt.get("party_type"):
        return "PARTY_TRANSACTION"
    if invoice_refs(bt.description) or invoice_refs(bt.description, "Purchase Invoice"):
        return "INVOICE_REFERENCE"
    for doctype, title in (("Customer", "customer_name"), ("Supplier", "supplier_name")):
        for party in query(f"select name, {title}, bank_names from `tab{doctype}`", as_dict=True):
            names = {party.name, party.get(title)} | set((party.bank_names or "").splitlines())
            if any(n and mentions(bt.description, n) for n in names):
                return "PARTY_CANDIDATE"
    # Processor/person-shaped descriptions are not sufficient proof of an expense.
    extracted = bank_name(bt.description)
    if extracted and learning.vendor_key(extracted) in learning.EXCLUDED_KEYWORDS:
        return "PAYMENT_PROCESSOR"
    params = {**scope, "bank": bt.bank_account, "gl": bank_gl, "amount": amount,
              "date": bt.date, "money_in": int(flt(bt.deposit) > 0), "name": bt.name}
    payments = query(
        """
        select pe.name from `tabPayment Entry` pe
        where pe.docstatus = 1 and pe.company = %(company)s and abs(datediff(pe.posting_date, %(date)s)) <= 7
          and ((%(money_in)s = 1 and abs(pe.received_amount - %(amount)s) < 0.005)
            or (%(money_in)s = 0 and abs(pe.paid_amount - %(amount)s) < 0.005))
          and not exists (select 1 from `tabBank Transaction Payments` p join `tabBank Transaction` b on b.name=p.parent
                           where p.payment_document='Payment Entry' and p.payment_entry=pe.name and b.docstatus=1)
        limit 1
        """, params,
    )
    if payments:
        return "EXISTING_PAYMENT"
    journals = query(
        """
        select je.name from `tabJournal Entry` je join `tabJournal Entry Account` a on a.parent=je.name
        where je.docstatus=1 and je.company=%(company)s and a.account=%(gl)s
          and abs(datediff(je.posting_date, %(date)s)) <= 7
          and ((%(money_in)s = 1 and abs(a.debit_in_account_currency - %(amount)s) < 0.005)
            or (%(money_in)s = 0 and abs(a.credit_in_account_currency - %(amount)s) < 0.005))
          and not exists (select 1 from `tabBank Transaction Payments` p join `tabBank Transaction` b on b.name=p.parent
                           where p.payment_document='Journal Entry' and p.payment_entry=je.name and b.docstatus=1)
        limit 1
        """, params,
    )
    if journals:
        return "EXISTING_JOURNAL"
    transfers = query(
        """
        select bt.name from `tabBank Transaction` bt join `tabBank Account` ba on ba.name=bt.bank_account
        where bt.docstatus=1 and ba.company=%(company)s and ba.is_company_account=1
          and bt.bank_account != %(bank)s and bt.currency=%(currency)s
          and abs(bt.unallocated_amount - %(amount)s) < 0.005 and abs(datediff(bt.date, %(date)s)) <= 5
          and ((%(money_in)s=1 and abs(bt.withdrawal - %(amount)s) < 0.005)
            or (%(money_in)s=0 and abs(bt.deposit - %(amount)s) < 0.005)) limit 1
        """, params,
    )
    if transfers:
        return "TRANSFER_CANDIDATE"
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        if query(f"select name from `tab{doctype}` where docstatus=1 and company=%(company)s "
                         "and currency=%(currency)s and abs(outstanding_amount - %(amount)s)<0.005 limit 1", params):
            return "INVOICE_CANDIDATE"
    return None


def _configured_user(settings):
    user = settings.get("bank_auto_book_user")
    if (not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled")
            or not set(frappe.get_roles(user)) & set(APPROVERS)):
        frappe.throw(_("Bank automation requires an enabled ERP user with an accounting approval role."))
    return user


def _settings(*, for_update=True):
    # Locking current reads avoid stale repeatable-read snapshots after lock waits.
    # The bounded batch is atomic; a switch save may wait for that batch to finish.
    values = dict(frappe.db.sql("select field, value from `tabSingles` where doctype='Automation Settings'"
                                + (" for update" if for_update else "")))
    for field in ("bank_auto_learn_enabled", "bank_auto_book_enabled"):
        values[field] = bool(int(values.get(field) or 0))
    return values


def _current_rules():
    from books_assistant.integration import check_document, lock_documents

    names = frappe.db.sql("select name from `tabBank Rule` where enabled=1 order by name for update", pluck=True)
    # Inspect the entire rule set, including potential hidden conflicts. A rule we
    # cannot authorize reading blocks automation rather than silently disappearing.
    lock_documents([("Bank Rule", name) for name in names])
    return [check_document("Bank Rule", name) for name in names]


def _run(company=None, *, scheduled=False):
    from books_assistant import bank_review as br
    from books_assistant.integration import resolve_scope, check_document, lock_documents

    settings = _settings(for_update=False)
    if not (settings.get("bank_auto_learn_enabled") or settings.get("bank_auto_book_enabled")):
        return {"code": "DISABLED", "rules_created": [], "results": []}
    configured = _configured_user(settings)
    if scheduled and configured != frappe.session.user:
        frappe.throw(_("The configured bank automation user changed. Retry the scheduled run."))
    scope = resolve_scope(company)
    lock_documents([("Company", scope["company"])])
    # Match every finance mutation's Company-before-dependent-record lock order.
    settings = _settings()
    if not (settings.get("bank_auto_learn_enabled") or settings.get("bank_auto_book_enabled")):
        return {"code": "DISABLED", "rules_created": [], "results": []}
    if scheduled and _configured_user(settings) != frappe.session.user:
        frappe.throw(_("The configured bank automation user changed. Retry the scheduled run."))
    observations = learning.load_observations(scope)
    learned = {"rules_created": [], "rules_updated": []}
    if settings.get("bank_auto_learn_enabled"):
        learned = learning.learn(scope, observations)
    results = []
    if not settings.get("bank_auto_book_enabled"):
        return {"code": "CLASSIFIED", **learned, "results": results}
    banks = frappe.get_list("Bank Account", filters={"company": scope["company"], "is_company_account": 1},
                            pluck="name", limit_page_length=0)
    if not banks:
        return {"code": "NO_BANKS", **learned, "results": results}
    filters = {"docstatus": 1, "bank_account": ["in", banks], "currency": scope["currency"],
               "unallocated_amount": [">", 0.009], "date": ["<=", today()]}
    cursor = settings.get("bank_auto_scan_after")
    if cursor:
        filters["name"] = [">", cursor]
    names = frappe.get_list("Bank Transaction", filters=filters, pluck="name", order_by="name",
                            limit_page_length=MAX_LINES_PER_RUN)
    if not names and cursor:
        filters.pop("name")
        names = frappe.get_list("Bank Transaction", filters=filters, pluck="name", order_by="name",
                                limit_page_length=MAX_LINES_PER_RUN)
    for index, name in enumerate(names):
        current_settings = _settings()
        if not current_settings.get("bank_auto_book_enabled"):
            results.append({"bank_transaction": name, "code": "DISABLED"})
            break
        if scheduled and _configured_user(current_settings) != frappe.session.user:
            results.append({"bank_transaction": name, "code": "ACTOR_CHANGED"})
            break
        savepoint = f"bank_auto_{index}"
        frappe.db.savepoint(savepoint)
        try:
            lock_documents([("Bank Transaction", name)])
            bt = check_document("Bank Transaction", name, permission="write", currency=scope["currency"])
            amount = flt(bt.deposit) or flt(bt.withdrawal)
            if (bt.docstatus != 1 or amount <= 0 or bool(flt(bt.deposit)) == bool(flt(bt.withdrawal))
                    or abs(flt(bt.unallocated_amount) - amount) >= 0.005
                    or frappe.db.exists("Bank Transaction Payments", {"parent": name})
                    or frappe.db.exists("Bank Rule Booking", name)):
                results.append({"bank_transaction": name, "code": "NOT_UNRECONCILED"})
                continue
            bank = check_document("Bank Account", bt.bank_account, company=scope["company"])
            gl = check_document("Account", bank.account, company=scope["company"], currency=scope["currency"])
            if not bank.is_company_account or gl.disabled or gl.is_group or gl.account_type != "Bank":
                results.append({"bank_transaction": name, "code": "INVALID_BANK"})
                continue
            context = br.Context()  # Each candidate gets fresh claim/transfer state.
            context.rules = _current_rules()
            matching = [r for r in context.rules if overlaps(r, bt)]
            if not matching:
                results.append({"bank_transaction": name, "code": "NO_MATCHING_RULE"})
                continue
            if len({r.account for r in matching}) != 1:
                results.append({"bank_transaction": name, "code": "AMBIGUOUS_RULE"})
                continue
            automatic = [r for r in matching if r.get("auto_book") and r.matches(bt, bt.bank_account)]
            if len(automatic) != 1:
                results.append({"bank_transaction": name, "code": "NO_AUTHORIZED_RULE"})
                continue
            rule = automatic[0]
            lock_documents([("Bank Rule", rule.name), ("Bank Account", bank.name), ("Account", rule.account), ("Account", bank.account)])
            rule = check_document("Bank Rule", rule.name)
            if not (rule.enabled and rule.get("auto_book") and rule.matches(bt, bt.bank_account)):
                results.append({"bank_transaction": name, "code": "RULE_CHANGED"})
                continue
            ledger = check_document("Account", rule.account, company=scope["company"], currency=scope["currency"])
            if not learning.eligible_ledger(ledger):
                results.append({"bank_transaction": name, "code": "INVALID_LEDGER"})
                continue
            try:
                frozen = json.loads(rule.auto_approval_json or "null")
            except (ValueError, TypeError):
                frozen = None
            if not isinstance(frozen, dict):
                results.append({"bank_transaction": name, "code": "REVIEW_REQUIRED"})
                continue
            references = [(doctype, evidence[field]) for evidence in frozen.get("evidence", [])
                          for doctype, field in (("Bank Transaction", "bank_transaction"), ("Journal Entry", "journal"))]
            lock_documents(references)
            # Reread evidence while source rows are locked; cached evidence never authorizes money.
            current_observations = learning.load_observations(scope)
            if not evidence_still_valid(rule, current_observations):
                results.append({"bank_transaction": name, "code": "EVIDENCE_CHANGED"})
                continue
            blocker = _existing_candidate(bt, bank.account, scope)
            if blocker:
                results.append({"bank_transaction": name, "code": blocker})
                continue
            context = br.Context()
            context.rules = _current_rules()
            refreshed = [r for r in context.rules if overlaps(r, bt)]
            suggestion = context.suggest(br.bank_line(name))
            if (len({r.account for r in refreshed}) != 1 or suggestion.get("kind") != "rule"
                    or suggestion.get("rule") != rule.name or suggestion.get("account") != rule.account):
                results.append({"bank_transaction": name, "code": "CLASSIFICATION_CHANGED"})
                continue
            # Last opportunity for the kill switch, after potentially expensive checks.
            if not _settings().get("bank_auto_book_enabled"):
                results.append({"bank_transaction": name, "code": "DISABLED"})
                break
            old_flag = getattr(frappe.flags, "bank_auto_booking", False)
            try:
                frappe.flags.bank_auto_booking = True
                message = br.execute(suggestion)
                journal = frappe.db.get_value("Bank Transaction Payments",
                                              {"parent": name, "payment_document": "Journal Entry"}, "payment_entry")
                record_rule_booking(name, rule.name, journal, automatic=True)
            finally:
                frappe.flags.bank_auto_booking = old_flag
            results.append({"bank_transaction": name, "code": "BOOKED", "rule": rule.name,
                            "journal_entry": journal, "message": message})
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            raise
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(title=f"Bank automatic booking failed: {name}")
            results.append({"bank_transaction": name, "code": "EXECUTION_FAILED"})
    if results:
        # Persist progress even for lines needing review, so they cannot starve
        # later eligible transactions. The settings row lock serializes runs.
        frappe.db.set_single_value("Automation Settings", "bank_auto_scan_after", results[-1]["bank_transaction"])
    return {"code": "COMPLETED", **learned, "results": results}


def _report(result):
    """Persist a bounded, aggregate operator summary without exposing record IDs."""
    if result["code"] != "DISABLED":
        decisions = Counter(row["code"] for row in result.get("results", []))
        summary = {"contract_version": 1, "action_id": "bank.auto.run", "code": result["code"],
                   "processed": sum(decisions.values()), "decisions": dict(decisions),
                   "rules_created": len(result.get("rules_created", [])),
                   "rules_updated": len(result.get("rules_updated", []))}
        frappe.db.set_single_value("Automation Settings", "bank_auto_last_run_on", now_datetime())
        frappe.db.set_single_value("Automation Settings", "bank_auto_last_summary", json.dumps(summary, sort_keys=True))
    return result


@frappe.whitelist(methods=["POST"])
def run(company=None):
    """Manual runs retain the authenticated caller's identity and permissions."""
    frappe.only_for(APPROVERS)
    return _report(_run(company))


def run_scheduled():
    settings = _settings(for_update=False)
    if not (settings.get("bank_auto_learn_enabled") or settings.get("bank_auto_book_enabled")):
        return {"code": "DISABLED", "results": []}
    original_user = frappe.session.user
    user = _configured_user(settings)
    try:
        frappe.set_user(user)
        return _report(_run(scheduled=True))
    finally:
        frappe.set_user(original_user)
