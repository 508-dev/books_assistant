"""Bank review queue: a suggested action for every unreconciled bank line, approved in bulk.

Suggestions, most preferred first:
  match     an existing payment or journal entry for the same amount on this bank account within a week.
            Matching first is what stops a payout being recorded twice.
  receipt   money in that names a client invoice, or matches a client's open invoice -> a payment applied to it
  payout    money out that names a bill, or matches a supplier's open bill -> a payment applied to it
  transfer  the other side of a move between our own bank accounts -> one journal entry for both lines
  rule      a Bank Rule (subscriptions, bank fees, interest) -> a journal entry to the rule's account

Nothing is booked until someone approves, and approving works the suggestion out again on the server.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, cstr, date_diff, flt, nowdate, strip_html

from books_assistant.matching import bank_name, invoice_refs, mentions, vendor_key

HIGH, CHECK, NONE = "high", "check", "none"
WINDOW_DAYS = 7  # how far apart a bank line and an existing entry for it can be dated
TRANSFER_DAYS = 5  # how far apart the two sides of a transfer can land
APPROVERS = ("Accounts Manager", "System Manager")
# Payment processors and our own name: seeing these says nothing about which party paid.
NOT_A_PARTY_NAME = {"INTUIT", "INTUIT INC.", "WISE", "WISE INC", "STRIPE", "PAYPAL", "VENMO", "BILL.COM", "MERCURY",
                    "508.DEV LLC", "508.DEV", "INTEREST"}


@frappe.whitelist()
def get_queue(bank_account=None):
    frappe.only_for(APPROVERS)
    context = Context()
    return [context.suggest(bt) for bt in unreconciled_lines(bank_account)]


@frappe.whitelist()
def approve(bank_transactions):
    """Carry out the suggestion for each bank line. Each line is committed on its own, so one failure
    doesn't undo the others."""
    frappe.only_for(APPROVERS)
    context, results = Context(), []
    for name in frappe.parse_json(bank_transactions):
        try:
            bt = bank_line(name)
            if not bt or flt(bt.unallocated_amount) <= 0.009:
                results.append({"name": name, "ok": True, "message": _("Already reconciled")})
                continue
            suggestion = context.suggest(bt)
            if suggestion["kind"] == NONE:
                results.append({"name": name, "ok": False, "message": _("Nothing to approve")})
                continue
            message = execute(suggestion)
            frappe.db.commit()
            results.append({"name": name, "ok": True, "message": message})
        except Exception as e:
            frappe.db.rollback()
            results.append({"name": name, "ok": False, "message": strip_html(cstr(e)).strip() or repr(e)})
    return results


def unreconciled_lines(bank_account=None):
    filters = {"docstatus": 1, "unallocated_amount": [">", 0.009]}
    if bank_account:
        filters["bank_account"] = bank_account
    return frappe.get_all(
        "Bank Transaction",
        filters=filters,
        fields=["name", "date", "bank_account", "deposit", "withdrawal", "unallocated_amount", "description",
                "party_type", "party", "currency"],
        order_by="date desc, name desc",
    )


def bank_line(name):
    return frappe.db.get_value(
        "Bank Transaction",
        {"name": name, "docstatus": 1},
        ["name", "date", "bank_account", "deposit", "withdrawal", "unallocated_amount", "description",
         "party_type", "party", "currency"],
        as_dict=True,
    )


class Context:
    """What a queue run needs to know once: bank accounts, rules, parties, and entries already spoken for."""

    def __init__(self):
        self.bank_gl = dict(
            frappe.get_all("Bank Account", filters={"is_company_account": 1}, fields=["name", "account"], as_list=True)
        )
        self.rules = [
            frappe.get_doc("Bank Rule", name)
            for name in frappe.get_all("Bank Rule", filters={"enabled": 1}, order_by="times_used desc", pluck="name")
        ]
        self.parties = {"Customer": self._parties("Customer", "customer_name"),
                        "Supplier": self._parties("Supplier", "supplier_name")}
        self.claimed = set()  # entries already suggested for another line in this run
        self.pairs = {}  # bank line -> the other side of its transfer

    def _parties(self, doctype, title_field):
        parties = {}
        for row in frappe.get_all(doctype, filters={"disabled": 0}, fields=["name", title_field, "bank_names"]):
            names = {row.name, row.get(title_field)}
            names |= {line.strip() for line in (row.bank_names or "").splitlines() if line.strip()}
            parties[row.name] = {n for n in names if n}
        return parties

    def party_in(self, description, party_type):
        found = [p for p, names in self.parties[party_type].items() if any(mentions(description, n) for n in names)]
        return found[0] if len(found) == 1 else None

    # ---------------------------------------------------------------- suggestions

    def suggest(self, bt):
        amount = flt(bt.unallocated_amount)
        full = abs(amount - (flt(bt.deposit) or flt(bt.withdrawal))) < 0.005
        suggestion = self._match_existing(bt, amount) or self._booked_elsewhere(bt, amount)
        if not suggestion and full:
            suggestion = (
                self._by_invoice_number(bt, amount)
                or self._transfer(bt, amount)
                or self._rule(bt)
                or self._by_party(bt, amount)
            )
        if not suggestion:
            suggestion = {"kind": NONE, "confidence": NONE, "summary": self._hint(bt, full)}
        return {
            "bank_transaction": bt.name,
            "date": str(bt.date),
            "bank_account": bt.bank_account,
            "bank_gl": self.bank_gl.get(bt.bank_account),
            "description": bt.description,
            "deposit": flt(bt.deposit),
            "withdrawal": flt(bt.withdrawal),
            "amount": amount,
            "keyword": vendor_key(bt.description),
            **suggestion,
        }

    def _match_existing(self, bt, amount):
        gl = self.bank_gl.get(bt.bank_account)
        if not gl:
            return None
        money_in = flt(bt.deposit) > 0
        params = {"gl": gl, "amount": amount, "bank_account": bt.bank_account,
                  "lo": add_days(bt.date, -WINDOW_DAYS), "hi": add_days(bt.date, WINDOW_DAYS)}
        not_linked = """not exists (
            select 1 from `tabBank Transaction Payments` btp join `tabBank Transaction` b on b.name = btp.parent
            where btp.payment_document = %(doctype)s and btp.payment_entry = {name} and b.docstatus = 1
              and b.bank_account = %(bank_account)s)"""
        payments = frappe.db.sql(
            f"""
            select 'Payment Entry' doctype, pe.name, pe.posting_date, pe.party_type, pe.party
            from `tabPayment Entry` pe
            where pe.docstatus = 1 and pe.posting_date between %(lo)s and %(hi)s
              and {"pe.paid_to = %(gl)s and abs(pe.received_amount - %(amount)s) < 0.005" if money_in
                   else "pe.paid_from = %(gl)s and abs(pe.paid_amount - %(amount)s) < 0.005"}
              and {not_linked.format(name="pe.name")}
            """,
            {**params, "doctype": "Payment Entry"},
            as_dict=True,
        )
        journals = frappe.db.sql(
            f"""
            select distinct 'Journal Entry' doctype, je.name, je.posting_date, null party_type, null party
            from `tabJournal Entry` je join `tabJournal Entry Account` a on a.parent = je.name
            where je.docstatus = 1 and je.posting_date between %(lo)s and %(hi)s and a.account = %(gl)s
              and abs(a.{"debit_in_account_currency" if money_in else "credit_in_account_currency"} - %(amount)s) < 0.005
              and {not_linked.format(name="je.name")}
            """,
            {**params, "doctype": "Journal Entry"},
            as_dict=True,
        )
        candidates = [c for c in payments + journals if (c.doctype, c.name) not in self.claimed]
        if not candidates:
            return None

        def rank(c):
            named = c.party and any(mentions(bt.description, n) for n in self.parties.get(c.party_type, {}).get(c.party, {c.party}))
            return (0 if named else 1, abs(date_diff(c.posting_date, bt.date)))

        candidates.sort(key=rank)
        best = candidates[0]
        sure = len(candidates) == 1 or rank(best)[0] < rank(candidates[1])[0]
        self.claimed.add((best.doctype, best.name))
        who = f" to {best.party}" if best.party and not money_in else f" from {best.party}" if best.party else ""
        return {
            "kind": "match",
            "confidence": HIGH if sure else CHECK,
            "summary": _("Matches {0} {1}{2} ({3})").format(_(best.doctype), best.name, who, best.posting_date)
            + ("" if sure else _("; {0} other entries could also match").format(len(candidates) - 1)),
            "voucher_type": best.doctype,
            "voucher": best.name,
        }

    def _booked_elsewhere(self, bt, amount):
        """A payment for this amount, dated within a week, booked to a different one of our bank accounts and
        not matched to any bank line: probably this money, with the wrong bank account. Creating a new payment
        would record it twice, so the line is held for a person to correct."""
        gl = self.bank_gl.get(bt.bank_account)
        other_gls = [a for a in self.bank_gl.values() if a and a != gl]
        if not other_gls:
            return None
        money_in = flt(bt.deposit) > 0
        rows = frappe.db.sql(
            f"""
            select pe.name, pe.party, {"pe.paid_to" if money_in else "pe.paid_from"} account
            from `tabPayment Entry` pe
            where pe.docstatus = 1 and pe.posting_date between %(lo)s and %(hi)s
              and {"pe.paid_to" if money_in else "pe.paid_from"} in %(other_gls)s
              and abs({"pe.received_amount" if money_in else "pe.paid_amount"} - %(amount)s) < 0.005
              and not exists (select 1 from `tabBank Transaction Payments` btp
                              where btp.payment_document = 'Payment Entry' and btp.payment_entry = pe.name)
            """,
            {"lo": add_days(bt.date, -WINDOW_DAYS), "hi": add_days(bt.date, WINDOW_DAYS), "other_gls": other_gls,
             "amount": amount},
            as_dict=True,
        )
        if not rows:
            return None
        pe = rows[0]
        return {
            "kind": NONE,
            "confidence": NONE,
            "summary": _("Probably {0} ({1}), but it was booked to {2} instead of {3}. Correct its bank account "
                         "(cancel and amend), then it will match here.").format(pe.name, pe.party, pe.account, gl),
        }

    def _by_invoice_number(self, bt, amount):
        money_in = flt(bt.deposit) > 0
        doctype = "Sales Invoice" if money_in else "Purchase Invoice"
        invoices = [i for i in (current_invoice(doctype, ref) for ref in invoice_refs(bt.description, doctype)) if i]
        invoices = [i for i in invoices if flt(i.outstanding_amount) > 0.009]
        if not invoices or len({i.party for i in invoices}) > 1:
            return None
        return self._apply_to(amount, invoices, doctype, _("named in the bank description"))

    def _by_party(self, bt, amount):
        money_in = flt(bt.deposit) > 0
        party_type, doctype = ("Customer", "Sales Invoice") if money_in else ("Supplier", "Purchase Invoice")
        party = bt.party if bt.party_type == party_type and bt.party else self.party_in(bt.description, party_type)
        if not party:
            return None
        open_invoices = outstanding_invoices(doctype, party)
        exact = [i for i in open_invoices if abs(flt(i.outstanding_amount) - amount) < 0.01]
        if len(exact) == 1:
            return self._apply_to(amount, exact, doctype, _("the only open one for that amount"), sure=False)
        if open_invoices and abs(sum(flt(i.outstanding_amount) for i in open_invoices) - amount) < 0.01:
            return self._apply_to(amount, open_invoices, doctype, _("all of their open ones"), sure=False)
        return {
            "kind": NONE,
            "confidence": NONE,
            "summary": _("Looks like {0}, but none of their open {1}s adds up to this amount").format(
                party, _("invoice") if money_in else _("bill")
            ),
        }

    def _apply_to(self, amount, invoices, doctype, how, sure=True):
        total = sum(flt(i.outstanding_amount) for i in invoices)
        names = ", ".join(i.name for i in invoices)
        party = invoices[0].party
        kind = "receipt" if doctype == "Sales Invoice" else "payout"
        if abs(total - amount) < 0.01:
            confidence, note = (HIGH if sure else CHECK), ""
        elif amount < total:
            confidence, note = CHECK, _(" (pays {0} of {1})").format(fmt(amount), fmt(total))
        else:
            confidence = CHECK
            note = _(" (pays {0} more than is open; the rest will sit unapplied)").format(fmt(amount - total))
        held = [i.name for i in invoices if i.get("on_hold")]
        if held:
            confidence = CHECK
            note += _(". {0} is on hold until the client pays; recording this payment releases it").format(", ".join(held))
        verb = _("Payment from {0} for {1}") if kind == "receipt" else _("Payment to {0} for {1}")
        return {
            "kind": kind,
            "confidence": confidence,
            "summary": verb.format(party, names) + f", {how}" + note,
            "party_type": "Customer" if kind == "receipt" else "Supplier",
            "party": party,
            "invoices": [{"doctype": doctype, "name": i.name, "allocated": min(flt(i.outstanding_amount), amount)} for i in invoices],
        }

    def _transfer(self, bt, amount):
        money_in = flt(bt.deposit) > 0
        pair_name = self.pairs.get(bt.name)
        if not pair_name:
            candidates = frappe.db.sql(
                f"""
                select bt.name from `tabBank Transaction` bt
                join `tabBank Account` ba on ba.name = bt.bank_account
                where bt.docstatus = 1 and ba.is_company_account = 1 and bt.bank_account != %(bank_account)s
                  and abs(bt.unallocated_amount - %(amount)s) < 0.005
                  and abs(bt.{"withdrawal" if money_in else "deposit"} - %(amount)s) < 0.005
                  and bt.date between %(lo)s and %(hi)s
                order by abs(datediff(bt.date, %(date)s)), bt.name
                """,
                {"bank_account": bt.bank_account, "amount": amount, "date": bt.date,
                 "lo": add_days(bt.date, -TRANSFER_DAYS), "hi": add_days(bt.date, TRANSFER_DAYS)},
                pluck=True,
            )
            candidates = [c for c in candidates if c not in self.pairs]
            if not candidates:
                return None
            pair_name = candidates[0]
            self.pairs[bt.name], self.pairs[pair_name] = pair_name, bt.name
        pair = frappe.db.get_value("Bank Transaction", pair_name, ["name", "bank_account", "date"], as_dict=True)
        source, target = (pair, bt) if money_in else (bt, pair)
        return {
            "kind": "transfer",
            "confidence": HIGH,
            "summary": _("Transfer from {0} to {1}; the other side is {2} ({3})").format(
                source.bank_account, target.bank_account, pair.name, pair.date
            ),
            "withdrawal_line": source.name,
            "deposit_line": target.name,
        }

    def _rule(self, bt):
        """The most specific matching rule: the longest matched words first (so "GOOGLE CLOUD" beats
        "GOOGLE"), then the most used."""
        matched = [r for r in self.rules if r.matches(bt, bt.bank_account)]
        matched.sort(key=lambda r: (-r.specificity(bt), -(r.times_used or 0)))
        if matched:
            rule = matched[0]
            return {
                "kind": "rule",
                "confidence": HIGH if rule.times_used >= 2 else CHECK,
                "summary": _("Rule “{0}”: book to {1}").format(rule.name, rule.account),
                "rule": rule.name,
                "account": rule.account,
            }
        return None

    def _hint(self, bt, full):
        if not full:
            return _("Partly reconciled: {0} of {1} still unmatched").format(
                fmt(bt.unallocated_amount), fmt(flt(bt.deposit) or flt(bt.withdrawal))
            )
        return _("No match yet. Make a rule if this repeats, or reconcile it in the Bank Reconciliation Tool.")


# -------------------------------------------------------------------- carrying out a suggestion


def execute(s):
    if s["kind"] == "match":
        link(s["bank_transaction"], s["voucher_type"], s["voucher"], s["amount"])
        return _("Matched to {0}").format(s["voucher"])
    if s["kind"] in ("receipt", "payout"):
        payment = make_payment(s)
        link(s["bank_transaction"], "Payment Entry", payment, s["amount"])
        return _("Created {0}").format(payment)
    if s["kind"] == "transfer":
        journal = make_transfer(s)
        return _("Created {0} for both sides of the transfer").format(journal)
    if s["kind"] == "rule":
        journal = journal_from_line(s["bank_transaction"], s["account"])
        rule = frappe.get_doc("Bank Rule", s["rule"])
        rule.db_set({"times_used": rule.times_used + 1, "last_used_on": nowdate()}, update_modified=False)
        return _("Created {0}").format(journal)
    frappe.throw(_("Nothing to approve"))


def link(bank_transaction, doctype, name, amount):
    from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

    reconcile_vouchers(
        bank_transaction, frappe.as_json([{"payment_doctype": doctype, "payment_name": name, "amount": amount}])
    )


def make_payment(s):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    bt = frappe.get_doc("Bank Transaction", s["bank_transaction"])
    for inv in s["invoices"]:
        release_if_held(inv["doctype"], inv["name"], bt.name)
    first = s["invoices"][0]
    pe = get_payment_entry(first["doctype"], first["name"], party_amount=s["amount"], bank_account=s["bank_gl"])
    if len(s["invoices"]) > 1:
        pe.set("references", [])
        for inv in s["invoices"]:
            row = frappe.db.get_value(inv["doctype"], inv["name"], ["grand_total", "outstanding_amount", "due_date"], as_dict=True)
            pe.append("references", {
                "reference_doctype": inv["doctype"], "reference_name": inv["name"], "due_date": row.due_date,
                "total_amount": row.grand_total, "outstanding_amount": row.outstanding_amount,
                "allocated_amount": inv["allocated"], "exchange_rate": 1,
            })
        pe.paid_amount = pe.received_amount = s["amount"]
    if pe.payment_type == "Receive":
        pe.paid_to = s["bank_gl"]
    else:
        pe.paid_from = s["bank_gl"]
    pe.update({
        "posting_date": bt.date,
        "reference_no": (bt.description or bt.name)[:140],
        "reference_date": bt.date,
        "bank_account": bt.bank_account,
        "mode_of_payment": None,
        "custom_remarks": 1,
        "remarks": _("From Bank Review, bank line {0}: {1}").format(bt.name, bt.description),
    })
    pe.insert()
    pe.submit()
    return pe.name


def make_transfer(s):
    in_line = frappe.db.get_value("Bank Transaction", s["deposit_line"], ["bank_account", "unallocated_amount"], as_dict=True)
    in_gl = frappe.db.get_value("Bank Account", in_line.bank_account, "account")
    journal = journal_from_line(s["withdrawal_line"], in_gl)
    link(s["deposit_line"], "Journal Entry", journal, in_line.unallocated_amount)
    return journal


def journal_from_line(bank_transaction, account):
    """A Bank Entry from the bank line to the account, reconciled to the line (ERPNext's own path)."""
    from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import create_journal_entry_bts

    bt = frappe.db.get_value("Bank Transaction", bank_transaction, ["date", "description"], as_dict=True)
    date = str(bt.date)  # ERPNext 15.12x type-checks this as text
    create_journal_entry_bts(
        bank_transaction,
        reference_number=(bt.description or bank_transaction)[:140],
        reference_date=date,
        posting_date=date,
        entry_type="Bank Entry",
        second_account=account,
    )
    return frappe.db.get_value(
        "Bank Transaction Payments",
        {"parent": bank_transaction, "payment_document": "Journal Entry"},
        "payment_entry",
        order_by="creation desc",
    )


# -------------------------------------------------------------------- invoices


def current_invoice(doctype, name):
    """The live version of an invoice, following amendments if the named one was cancelled."""
    for _hop in range(5):
        row = frappe.db.get_value(doctype, name, ["name", "docstatus", "outstanding_amount"] + party_fields(doctype),
                                  as_dict=True)
        if not row:
            return None
        if row.docstatus == 1:
            return row
        name = frappe.db.get_value(doctype, {"amended_from": name, "docstatus": ["!=", 2]}, "name")
        if not name:
            return None
    return None


def party_fields(doctype):
    """The party as "party", and for bills whether they're on hold (waiting for the client to pay)."""
    return ["customer as party"] if doctype == "Sales Invoice" else ["supplier as party", "on_hold", "client_invoice"]


def outstanding_invoices(doctype, party):
    party_field = "customer" if doctype == "Sales Invoice" else "supplier"
    return frappe.get_all(
        doctype,
        filters={party_field: party, "docstatus": 1, "outstanding_amount": [">", 0.009], "is_return": 0},
        fields=["name", "outstanding_amount", "posting_date"] + party_fields(doctype),
        order_by="posting_date, name",
    )


def release_if_held(doctype, name, bank_transaction):
    """The money has already left the bank, so a hold on the bill no longer applies."""
    if doctype != "Purchase Invoice" or not frappe.db.get_value(doctype, name, "on_hold"):
        return
    bill = frappe.get_doc(doctype, name)
    bill.db_set({"on_hold": 0, "release_date": None})
    bill.add_comment("Comment", _("Released from hold: paid from bank line {0} before the client paid.")
                     .format(bank_transaction))


def fmt(amount):
    return f"${flt(amount, 2):,.2f}"


# -------------------------------------------------------------------- rules and bank names


@frappe.whitelist()
def create_rule(bank_transaction, keyword, account):
    """Make a rule from one bank line: its direction and the words to match, booked to the account."""
    frappe.only_for(APPROVERS)
    bt = frappe.db.get_value("Bank Transaction", bank_transaction, ["deposit"], as_dict=True)
    keyword = keyword.strip()
    rule = frappe.get_doc({
        "doctype": "Bank Rule",
        "rule_name": unique_rule_name(keyword.title()),
        "description_contains": keyword,
        "direction": "Money in" if flt(bt.deposit) > 0 else "Money out",
        "account": account,
    }).insert()
    return rule.name


def unique_rule_name(base):
    name, n = base, 2
    while frappe.db.exists("Bank Rule", name):
        name, n = f"{base} {n}", n + 1
    return name


@frappe.whitelist()
def learn_from_history():
    """Create rules from past bank lines booked by journal entry, and record how parties appear on bank
    statements from past matched payments. Existing rules and names are left alone."""
    frappe.only_for(APPROVERS)
    return _("Created {0} rules: {1}. Added {2} bank names for {3} parties.").format(*_learn_rules(), *_learn_bank_names())


def _learn_rules():
    company_banks = set(frappe.get_all("Bank Account", filters={"is_company_account": 1}, pluck="account"))
    rows = frappe.db.sql(
        """
        select bt.description, bt.deposit, ba.account own_account, btp.payment_entry journal
        from `tabBank Transaction` bt
        join `tabBank Account` ba on ba.name = bt.bank_account
        join `tabBank Transaction Payments` btp on btp.parent = bt.name and btp.payment_document = 'Journal Entry'
        where bt.docstatus = 1
        """,
        as_dict=True,
    )
    seen = defaultdict(lambda: defaultdict(int))
    for row in rows:
        accounts = set(frappe.get_all("Journal Entry Account", filters={"parent": row.journal}, pluck="account"))
        accounts.discard(row.own_account)
        if len(accounts) != 1:
            continue
        account = accounts.pop()
        account_type = frappe.get_cached_value("Account", account, "account_type")
        if account_type in ("Cash", "Receivable", "Payable") or (account_type == "Bank" and account not in company_banks):
            continue
        key = vendor_key(row.description)
        if key:
            seen[(key, "Money in" if flt(row.deposit) > 0 else "Money out")][account] += 1
    created = []
    for (key, direction), accounts in sorted(seen.items()):
        if len(accounts) != 1:
            continue  # booked to different accounts in the past: not safe to automate
        account, uses = next(iter(accounts.items()))
        if uses < 2 or frappe.db.exists("Bank Rule", {"description_contains": key, "direction": direction}):
            continue
        rule = frappe.get_doc({
            "doctype": "Bank Rule",
            "rule_name": unique_rule_name(key.title() + (" (in)" if direction == "Money in" else "")),
            "description_contains": key,
            "direction": direction,
            "account": account,
            "learned": 1,
            "times_used": uses,
        }).insert()
        created.append(rule.name)
    return len(created), ", ".join(created) or _("none")


def _learn_bank_names():
    rows = frappe.db.sql(
        """
        select bt.description, pe.party_type, pe.party from `tabBank Transaction` bt
        join `tabBank Transaction Payments` btp on btp.parent = bt.name and btp.payment_document = 'Payment Entry'
        join `tabPayment Entry` pe on pe.name = btp.payment_entry
        where bt.docstatus = 1 and pe.docstatus = 1 and pe.party_type in ('Customer', 'Supplier')
        """,
        as_dict=True,
    )
    context = Context()
    owners = defaultdict(set)
    for row in rows:
        name = bank_name(row.description)
        if not name or name.upper() in NOT_A_PARTY_NAME:
            continue
        known = context.parties[row.party_type].get(row.party, {row.party})
        if any(mentions(row.description, n) for n in known):
            continue
        owners[(row.party_type, name.casefold())].add((row.party_type, row.party, name))
    added = defaultdict(list)
    for entries in owners.values():
        if len({p for _t, p, _n in entries}) != 1:
            continue  # the same name paid for different customers (or suppliers): ambiguous
        party_type, party, name = next(iter(entries))
        added[(party_type, party)].append(name)
    for (party_type, party), names in added.items():
        current = frappe.db.get_value(party_type, party, "bank_names") or ""
        lines = [l for l in current.splitlines() if l.strip()]
        lines += [n for n in names if n.casefold() not in {l.casefold() for l in lines}]
        frappe.db.set_value(party_type, party, "bank_names", "\n".join(lines))
    return sum(len(n) for n in added.values()), len(added)
