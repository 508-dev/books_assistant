"""A month's billing: client invoices from approved timesheets, engineers' bills, and referral fees.

Prepare drafts everything and shows each project's figures with anything to check. Approve submits it: client
invoices (emailed by books_assistant.invoice_email), then the engineer and referral bills, held until the client
pays (books_assistant.holds releases them).

A timesheet is billed whole, in the run whose period contains its end date: an engineer's bill is tracked per
timesheet (timesheet_extended), so splitting one across months would pay its later hours twice or never. Hourly
projects are invoiced from those timesheets; retainer projects use the draft invoice made for them (e.g. by Auto
Repeat), and the run only prepares their engineers' bills. Projects without a customer are internal: their bills
are submitted without a hold, as there's no client payment to wait for.
"""

from collections import defaultdict
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, flt, formatdate, getdate

from books_assistant.invoice_email import recipients, settings as email_settings
from books_assistant.integration import check_document, lock_documents, resolve_scope, selection
from books_assistant.setup import BILLING_APPROVER, HOURLY, RETAINER

SALES_ITEM = "Contract Engineering (Sales)"
PURCHASE_ITEM = "Contract Engineering (Purchasing)"
MARGIN_TARGET_PCT = 5
PAYMENT_TERMS_DAYS = 30
LOOKBACK_DAYS = 90
PREPARERS = ("Accounts Manager", "System Manager", BILLING_APPROVER)
APPROVERS = (BILLING_APPROVER, "System Manager")
APPROVED = "Approved"
LEFT_OUT = "Left out"
APPROVAL_FIELDS = ("project", "customer", "billing_method", "hours", "client_amount", "engineer_cost",
                   "referral_cost", "margin_pct", "sales_invoice", "engineer_bills", "issues", "outcome",
                   "item_status", "error_code", "result_json")


@contextmanager
def editing_run():
    previous = frappe.flags.get("books_assistant_billing_action")
    frappe.flags.books_assistant_billing_action = True
    try:
        yield
    finally:
        frappe.flags.books_assistant_billing_action = previous


class BillingRun(Document):
    def validate(self):
        scope = resolve_scope(self.get("company"))
        self.company, self.currency = scope["company"], scope["currency"]
        if not frappe.flags.get("books_assistant_billing_action"):
            before = self.get_doc_before_save()
            if self.is_new():
                if self.items or self.status != "Draft":
                    frappe.throw(_("Create an empty draft run, then use Prepare Drafts."))
            elif before:
                old_items = [[r.get(f) for f in APPROVAL_FIELDS] for r in before.items]
                new_items = [[r.get(f) for f in APPROVAL_FIELDS] for r in self.items]
                if old_items != new_items or before.status != self.status:
                    frappe.throw(_("Use the billing actions to change prepared projects or their status."))
                if before.status != "Draft" and any(before.get(f) != self.get(f) for f in
                                                      ("period_start", "period_end", "invoice_date")):
                    frappe.throw(_("Discard the drafts before changing this run's dates."))
        if getdate(self.period_start) > getdate(self.period_end):
            frappe.throw(_("The period starts after it ends."))
        self.total_hours = sum(flt(r.hours) for r in self.items)
        self.total_client = sum(flt(r.client_amount) for r in self.items)
        self.total_engineer = sum(flt(r.engineer_cost) for r in self.items)
        self.total_referral = sum(flt(r.referral_cost) for r in self.items)
        self.total_margin_pct = margin_pct(self.total_client, self.total_engineer, self.total_referral)
        self.title = self.period

    def on_trash(self):
        self.reload_for_action(PREPARERS)
        if any(is_approved(r) or r.get("item_status") == "review_required" for r in self.items):
            frappe.throw(_("This run has approved projects, so it can't be deleted."))
        discard(self)

    @property
    def period(self):
        start, end = getdate(self.period_start), getdate(self.period_end)
        if start.day == 1 and add_days(end, 1).day == 1 and (start.year, start.month) == (end.year, end.month):
            return start.strftime("%B %Y")
        return f"{formatdate(start)} to {formatdate(end)}"

    def reload_for_action(self, roles):
        frappe.only_for(roles)
        if self.is_new():
            frappe.throw(_("Save the run first."))
        scope = resolve_scope()
        lock_documents([("Company", scope["company"])])
        lock_documents([("Billing Run", self.name)])
        self.flags.for_update = True
        self.reload()
        self.check_permission("write")
        return resolve_scope(self.get("company"))

    @frappe.whitelist(methods=["POST"])
    def prepare(self):
        scope = self.reload_for_action(PREPARERS)
        if self.status != "Draft":
            frappe.throw(_("This run is already prepared. Discard its drafts to prepare it again."))
        self.set("items", [])
        for project in projects_to_bill(self):
            lock_documents([("Project", project)])
            check_document("Project", project, company=scope["company"])
            self.append("items", prepare_project(self, project))
        if not self.items:
            frappe.throw(_("Nothing to bill: no timesheets ending between {0} and {1} with unbilled hours, and no "
                           "draft retainer invoices.").format(formatdate(self.period_start), formatdate(self.period_end)))
        self.status = "Prepared"
        with editing_run():
            self.save()
        return {"run_id": self.name, "status": self.status}

    @frappe.whitelist(methods=["POST"])
    def approve(self, projects=None, preview_id=None, request_id=None):
        """Compatibility entry point; all approvals consume a reviewed, durable preview."""
        if not preview_id or not request_id:
            frappe.throw(_("Preview the selected projects before approving them."))
        from books_assistant.billing_api import execute_billing_approval

        result = execute_billing_approval(preview_id, request_id, expected_run=self.name,
                                         expected_projects=selection(projects, name="projects"))
        self.reload()
        return result

    def approve_selected(self, projects):
        """Internal execution after a locked preview has been checked."""
        if self.status == "Draft":
            frappe.throw(_("Prepare the run first."))
        projects = set(selection(projects, name="projects"))
        if projects - {row.project for row in self.items}:
            frappe.throw(_("The selection includes projects outside this run."))
        results = []
        for row in self.items:
            if row.project not in projects:
                continue
            if is_done(row) or row.item_status == "review_required":
                frappe.throw(_("This project's state needs a fresh review."))
            # Each project succeeds or fails on its own; nothing is committed (or emailed) until the end.
            frappe.db.savepoint("billing_run_project")
            try:
                result = approve_project(row)
                row.item_status = "approved"
                row.error_code = None
                row.outcome = result["message"]
            except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
                raise
            except Exception as e:
                frappe.db.rollback(save_point="billing_run_project")
                frappe.clear_last_message()
                row.outcome = _("Not approved: {0}").format(frappe.utils.strip_html(str(e)) or repr(e))
                row.item_status = "failed"
                row.error_code = "PROJECT_APPROVAL_FAILED"
                result = {"project": row.project, "status": "failed", "error_code": row.error_code,
                          "message": row.outcome}
            row.result_json = frappe.as_json(result)
            results.append(result)
        self.finish()
        return {"run_id": self.name, "status": self.status, "projects": results}

    @frappe.whitelist(methods=["POST"])
    def leave_out(self, projects):
        """Delete these projects' drafts and free their timesheets, e.g. when they were already billed by hand."""
        self.reload_for_action(APPROVERS)
        projects = set(selection(projects, name="projects"))
        known = {r.project for r in self.items}
        if projects - known:
            frappe.throw(_("The selection includes projects outside this run."))
        for row in self.items:
            if row.project in projects and not is_done(row):
                if row.item_status == "review_required":
                    frappe.throw(_("A System Manager must review this legacy state before its drafts can be removed."))
                discard_row(row)
                row.item_status = "left_out"
                row.error_code = None
                row.result_json = None
                row.outcome = _("{0} by {1}: its drafts were deleted.").format(LEFT_OUT, frappe.session.user)
        self.finish()

    def finish(self):
        if self.items and all(is_done(r) for r in self.items):
            self.status = APPROVED
        with editing_run():
            self.save()

    @frappe.whitelist(methods=["POST"])
    def discard_drafts(self):
        """Delete the drafts this run made and free their timesheets, so the run can be prepared again."""
        self.reload_for_action(PREPARERS)
        if any(is_approved(r) or r.get("item_status") == "review_required" for r in self.items):
            frappe.throw(_("Some projects are already approved. Discard individual drafts by hand instead."))
        discard(self)
        self.set("items", [])
        self.status = "Draft"
        with editing_run():
            self.save()


def is_approved(row):
    return row.get("item_status") == "approved"


def is_done(row):
    return row.get("item_status") in ("approved", "left_out")


def discard(run):
    for row in run.items:
        discard_row(row)


def discard_row(row):
    """Delete the row's draft bills (freeing their timesheets) and the draft client invoice it made. A retainer's
    invoice wasn't made by the run, so it's kept."""
    kept = []
    for name in bill_names(row):
        if frappe.db.exists("Purchase Invoice", name):
            check_document("Purchase Invoice", name, permission="delete" if
                           frappe.db.get_value("Purchase Invoice", name, "docstatus") == 0 else "read")
        if frappe.db.get_value("Purchase Invoice", name, "docstatus") == 0:
            for timesheet in frappe.get_all("Timesheet", filters={"purchase_invoice": name}, pluck="name"):
                frappe.db.set_value("Timesheet", timesheet,
                                    {"purchase_invoice": None, "purchase_invoice_status": "Not Created"})
            frappe.delete_doc("Purchase Invoice", name)
        elif frappe.db.exists("Purchase Invoice", name):
            kept.append(name)
    row.engineer_bills = "\n".join(kept)
    if row.sales_invoice and row.billing_method == HOURLY and \
            frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus") == 0:
        check_document("Sales Invoice", row.sales_invoice, permission="delete")
        invoice, row.sales_invoice = row.sales_invoice, None
        if row.name:
            frappe.db.set_value("Billing Run Item", row.name, "sales_invoice", None)
        frappe.delete_doc("Sales Invoice", invoice)


# -------------------------------------------------------------------- preparing


def projects_to_bill(run):
    """Projects with timesheets ending in the period that have uninvoiced hours or no engineer bill yet, plus
    retainer projects with a draft invoice that isn't in another run."""
    hours = frappe.db.sql_list(
        """
        select distinct td.project from `tabTimesheet Detail` td join `tabTimesheet` ts on ts.name = td.parent
        where ts.docstatus = 1 and ifnull(td.project, '') != '' and ts.end_date between %(start)s and %(end)s
          and ((td.is_billable = 1 and td.sales_invoice is null and td.billing_hours > 0)
               or ifnull(ts.purchase_invoice_status, '') in ('', 'Not Created'))
        for update
        """,
        {"start": run.period_start, "end": run.period_end},
    )
    retainers = frappe.db.sql_list(
        """
        select distinct si.project from `tabSales Invoice` si join `tabProject` p on p.name = si.project
        where si.docstatus = 0 and p.billing_method = %(retainer)s
          and si.name not in (select sales_invoice from `tabBilling Run Item` where parent != %(run)s
                              and ifnull(sales_invoice, '') != '')
        for update
        """,
        {"retainer": RETAINER, "run": run.name or ""},
    )
    return sorted(set(hours) | set(retainers))


def prepare_project(run, project):
    p = check_document("Project", project, company=run.company)
    method = p.billing_method or HOURLY
    customer = p.customer or last_customer(project)
    row = frappe._dict(project=project, customer=customer, billing_method=method, hours=0, client_amount=0,
                       engineer_cost=0, referral_cost=0, margin_pct=0, item_status="pending")
    if customer:
        check_document("Customer", customer)
    issues = []
    if not p.customer and customer:
        issues.append(_("The project has no customer; using {0} from its last invoice. Set it on the project.")
                      .format(customer))

    if method == HOURLY:
        lines, on_drafts = sales_lines(run, project)
        if on_drafts:
            issues.append(_("Some hours are already on draft invoice(s) {0}, so they were left out.")
                          .format(", ".join(on_drafts)))
        if lines and not customer:
            issues.append(_("{0} billable hours but the project has no customer, so no invoice was made. Set the "
                            "customer on the project.").format(flt(sum(flt(line.billing_hours) for line in lines), 2)))
        elif lines:
            issues += made_by_hand(run, "Sales Invoice", project)
            si = make_client_invoice(run, project, customer, lines)
            row.update(sales_invoice=si.name, client_amount=flt(si.grand_total),
                       hours=flt(sum(flt(line.billing_hours) for line in lines), 2))
    else:
        drafts = frappe.db.sql("select name, grand_total from `tabSales Invoice` where project = %s "
                               "and docstatus = 0 order by posting_date desc for update", project, as_dict=True)
        lock_documents([("Sales Invoice", draft.name) for draft in drafts])
        for draft in drafts:
            check_document("Sales Invoice", draft.name, company=run.company, currency=run.currency)
        if drafts:
            check_document("Sales Invoice", drafts[0].name, company=run.company, currency=run.currency)
            row.update(sales_invoice=drafts[0].name, client_amount=flt(drafts[0].grand_total))
            if len(drafts) > 1:
                issues.append(_("{0} draft invoices for this retainer; using {1}.").format(len(drafts), drafts[0].name))
        else:
            issues.append(_("Retainer with no draft invoice. Draft this month's (or make last month's repeat), then "
                            "discard and prepare again."))

    earlier_cost, earlier_bills = already_billed(run, project)  # before this run links its own bills
    bills, cost, engineer_hours, bill_issues = make_engineer_bills(run, project)
    if earlier_bills:
        issues.append(_("Engineer bills already exist for some of these timesheets ({0}); their cost is included in "
                        "the margin.").format(", ".join(earlier_bills)))
    invoiced_amount, invoiced = already_invoiced(run, project)
    if invoiced and method == HOURLY:
        issues.append(_("Some of these hours were already invoiced on {0}; that amount is included in the margin.")
                      .format(", ".join(invoiced)))
        row.client_amount = flt(flt(row.client_amount) + invoiced_amount, 2)
    row.engineer_bills = "\n".join(bills)
    row.engineer_cost = flt(cost + earlier_cost, 2)
    if not row.hours:
        row.hours = flt(engineer_hours, 2)
    issues += bill_issues
    issues += earlier_unbilled(run, project, method)

    if row.sales_invoice:
        new_invoice = frappe.db.get_value("Sales Invoice", row.sales_invoice, "grand_total")
        row.referral_cost = expected_referral(project, new_invoice)
        to, _cc = recipients(frappe.get_doc("Sales Invoice", row.sales_invoice))
        if not to:
            issues.append(_("{0} has no invoice email address, so the invoice won't be emailed. Add one under "
                            "Invoice Emails on the customer.").format(customer))
    if row.client_amount:
        row.margin_pct = margin_pct(row.client_amount, row.engineer_cost, row.referral_cost)
        if row.margin_pct < MARGIN_TARGET_PCT:
            issues.append(_("Margin {0}% is under the {1}% target.").format(flt(row.margin_pct, 1), MARGIN_TARGET_PCT))
    if bills and not row.sales_invoice:
        if not customer:
            issues.append(_("Internal project (no customer): its bills are submitted without a hold."))
        elif len(invoiced) == 1:
            issues.append(_("The bills will wait for {0} to be paid.").format(invoiced[0]))
        else:
            issues.append(_("No client invoice to wait for (the hours aren't billable, or were invoiced on more than "
                            "one invoice), so these bills will be held until released by hand."))
    row.issues = "\n".join(issues)
    return row


def period_timesheets(run, project, extra="", values=None):
    rows = frappe.db.sql(
        f"""
        select td.name, td.parent as time_sheet, td.from_time, td.to_time, td.hours, td.billing_hours,
               td.billing_amount, td.billing_rate, td.costing_amount, td.costing_rate, td.activity_type,
               td.description, td.is_billable, td.sales_invoice, ts.employee, ts.employee_name,
               ts.purchase_invoice_status, ts.purchase_invoice, emp.supplier as employee_supplier
        from `tabTimesheet Detail` td join `tabTimesheet` ts on ts.name = td.parent
        left join `tabEmployee` emp on emp.name = ts.employee
        where ts.docstatus = 1 and td.project = %(project)s and ts.end_date between %(start)s and %(end)s {extra}
        order by ts.employee, td.from_time
        for update
        """,
        {"project": project, "start": run.period_start, "end": run.period_end, **(values or {})},
        as_dict=True,
    )
    names = sorted({r.time_sheet for r in rows})
    lock_documents([("Timesheet", name) for name in names])
    for name in names:
        check_document("Timesheet", name, company=run.company)
    return rows


def sales_lines(run, project):
    """Billable, uninvoiced rows of the period's timesheets, less any already on a draft invoice."""
    rows = period_timesheets(run, project, "and td.is_billable = 1 and td.sales_invoice is null and td.billing_hours > 0")
    drafts = dict(frappe.db.sql(
        """
        select sit.timesheet_detail, si.name from `tabSales Invoice Timesheet` sit
        join `tabSales Invoice` si on si.name = sit.parent where si.docstatus = 0 and sit.timesheet_detail in %(rows)s
        for update
        """,
        {"rows": [r.name for r in rows] or [""]},
    ))
    lock_documents([("Sales Invoice", name) for name in set(drafts.values())])
    for name in set(drafts.values()):
        check_document("Sales Invoice", name, company=run.company, currency=run.currency)
    return [r for r in rows if r.name not in drafts], sorted(set(drafts.values()))


def make_client_invoice(run, project, customer, lines):
    """One line per billing rate, as invoices have been laid out so far, with the timesheet rows attached."""
    by_rate = defaultdict(list)
    for line in lines:
        by_rate[flt(line.billing_rate, 2)].append(line)
    si = frappe.new_doc("Sales Invoice")
    si.update({
        "company": run.company,
        "currency": run.currency,
        "customer": customer,
        "project": project,
        "posting_date": run.invoice_date,
        "set_posting_time": 1,
        "remarks": _("Engineering, {0}. Prepared by Billing Run {1}.").format(run.period, run.name),
    })
    if not frappe.db.get_value("Customer", customer, "payment_terms"):
        si.due_date = add_days(run.invoice_date, PAYMENT_TERMS_DAYS)
    for rate, rows in sorted(by_rate.items(), reverse=True):
        hours = flt(sum(flt(r.billing_hours) for r in rows), 2)
        amount = flt(sum(flt(r.billing_amount) for r in rows), 2)
        si.append("items", {
            "item_code": SALES_ITEM,
            "qty": hours,
            "rate": rate or flt(amount / hours, 2),
            "description": _("Contract engineering, {0}").format(run.period),
            "project": project,
        })
    for r in lines:
        si.append("timesheets", {
            "time_sheet": r.time_sheet, "timesheet_detail": r.name, "activity_type": r.activity_type,
            "description": r.description, "from_time": r.from_time, "to_time": r.to_time,
            "billing_hours": r.billing_hours, "billing_amount": r.billing_amount, "project_name": project,
        })
    si.insert()
    return si


def make_engineer_bills(run, project):
    """One draft bill per engineer for the period's timesheets that have no bill yet (all hours worked, at their
    costing rate). Returns the names, total cost, total hours and anything to check."""
    from timesheet_extended.timesheet_extended.timesheet_utils import link_timesheets_to_purchase_invoice

    rows = period_timesheets(run, project, "and ifnull(ts.purchase_invoice_status, '') in ('', 'Not Created')")
    by_supplier, issues = defaultdict(list), []
    for r in rows:
        if r.employee_supplier:
            by_supplier[r.employee_supplier].append(r)
    for name in sorted({r.employee_name for r in rows if not r.employee_supplier}):
        hours = sum(flt(r.hours) for r in rows if r.employee_name == name and not r.employee_supplier)
        issues.append(_("{0} has no supplier on their Employee record, so no bill was made for their {1}h.")
                      .format(name, flt(hours, 2)))
    names, cost, total_hours = [], 0, 0
    for supplier, entries in sorted(by_supplier.items()):
        hours = flt(sum(flt(e.hours) for e in entries), 2)
        amount = flt(sum(flt(e.costing_amount) for e in entries), 2)
        if not hours:
            continue
        if not amount:
            issues.append(_("{0}'s hours have no costing rate, so their bill is $0. Check their Activity Cost.")
                          .format(supplier))
        issues += made_by_hand(run, "Purchase Invoice", project, supplier)
        timesheets = sorted({e.time_sheet for e in entries})
        check_document("Supplier", supplier)
        rates = {flt(e.costing_rate, 2) for e in entries}
        pi = frappe.new_doc("Purchase Invoice")
        pi.update({
            "company": run.company,
            "currency": run.currency,
            "supplier": supplier,
            "project": project,
            "posting_date": run.invoice_date,
            "set_posting_time": 1,
            "timesheet": timesheets[0] if len(timesheets) == 1 else None,
            "remarks": _("Engineering, {0}. Prepared by Billing Run {1}.").format(run.period, run.name),
        })
        pi.append("items", {
            "item_code": PURCHASE_ITEM,
            "qty": hours,
            "rate": rates.pop() if len(rates) == 1 else flt(amount / hours, 2),
            "description": _("Contract engineering, {0}").format(run.period),
            "project": project,
        })
        pi.insert()
        link_timesheets_to_purchase_invoice(pi.name, timesheets)
        names.append(pi.name)
        cost += flt(pi.grand_total)
        total_hours += hours
    return names, cost, total_hours, issues


def made_by_hand(run, doctype, project, supplier=None):
    """Invoices or bills dated since the period began that aren't linked to any timesheet. Hours have often been
    invoiced by hand that way, which leaves the timesheets looking unbilled, so these may already cover them."""
    if doctype == "Sales Invoice":
        found = frappe.db.sql(
            """
            select name, posting_date, grand_total from `tabSales Invoice` si
            where si.project = %(project)s and si.docstatus = 1 and si.is_return = 0 and si.posting_date >= %(start)s
              and not exists (select 1 from `tabSales Invoice Timesheet` t where t.parent = si.name)
            order by posting_date
            for update
            """,
            {"project": project, "start": run.period_start},
            as_dict=True,
        )
    else:
        found = frappe.db.sql(
            """
            select name, posting_date, grand_total from `tabPurchase Invoice` pi
            where pi.project = %(project)s and pi.supplier = %(supplier)s and pi.docstatus = 1 and pi.is_return = 0
              and ifnull(pi.is_referral_fee, 0) = 0 and pi.posting_date >= %(start)s and ifnull(pi.timesheet, '') = ''
              and not exists (select 1 from `tabTimesheet` t where t.purchase_invoice = pi.name)
            order by posting_date
            for update
            """,
            {"project": project, "supplier": supplier, "start": run.period_start},
            as_dict=True,
        )
    for row in found:
        lock_documents([(doctype, row.name)])
        check_document(doctype, row.name, company=run.company, currency=run.currency)
    return [_("{0} ({1}, {2}) was made by hand, not from timesheets. If it already covers these hours, leave this "
              "project out (Approve and Send, then Leave Out).").format(f.name, formatdate(f.posting_date),
                                                                       fmt(f.grand_total)) for f in found]


def already_billed(run, project):
    """Cost of the period's timesheet hours that already have an engineer bill, and those bills."""
    rows = period_timesheets(run, project, "and ifnull(ts.purchase_invoice_status, '') not in ('', 'Not Created')")
    bills = sorted({r.purchase_invoice for r in rows if r.purchase_invoice})
    lock_documents([("Purchase Invoice", name) for name in bills])
    for name in bills:
        check_document("Purchase Invoice", name, company=run.company, currency=run.currency)
    return flt(sum(flt(r.costing_amount) for r in rows), 2), sorted(bills)


def already_invoiced(run, project):
    """Amount of the period's billable hours already on submitted client invoices, and those invoices."""
    rows = period_timesheets(run, project, "and td.is_billable = 1 and td.sales_invoice is not null")
    names = {r.sales_invoice for r in rows}
    lock_documents([("Sales Invoice", name) for name in names])
    invoices = {name for name in names if check_document("Sales Invoice", name, company=run.company,
                                                        currency=run.currency).docstatus == 1}
    return flt(sum(flt(r.billing_amount) for r in rows if r.sales_invoice in invoices), 2), sorted(invoices)


def earlier_unbilled(run, project, method):
    """Timesheets that ended shortly before the period and still look unbilled, which this run doesn't include.
    For retainers only the engineer side counts, as their client invoices aren't made from timesheets."""
    uninvoiced = "uninvoiced > 0 or " if method == HOURLY else ""
    rows = frappe.db.sql(
        f"""
        select ts.name, ts.end_date,
               sum(case when td.is_billable = 1 and td.sales_invoice is null then td.billing_hours else 0 end) uninvoiced,
               max(ifnull(ts.purchase_invoice_status, '') in ('', 'Not Created')) unpaid
        from `tabTimesheet` ts join `tabTimesheet Detail` td on td.parent = ts.name
        where ts.docstatus = 1 and td.project = %(project)s and ts.end_date >= %(lo)s and ts.end_date < %(start)s
        group by ts.name, ts.end_date
        having {uninvoiced}unpaid = 1
        order by ts.end_date
        for update
        """,
        {"project": project, "start": run.period_start, "lo": add_days(run.period_start, -LOOKBACK_DAYS)},
        as_dict=True,
    )
    if not rows:
        return []
    lock_documents([("Timesheet", row.name) for row in rows])
    for row in rows:
        check_document("Timesheet", row.name, company=run.company)
    names = ", ".join(f"{r.name} ({formatdate(r.end_date)})" for r in rows)
    return [_("Earlier timesheets that look unbilled aren't included: {0}. If they weren't billed by hand, start "
              "the period earlier.").format(names)]


def expected_referral(project, client_amount):
    """What referral_fee will bill when the invoice is submitted: grand total × each referrer's percentage."""
    rows = frappe.db.sql("select supplier, percentage from `tabProject Referrer` where parent = %s "
                         "and parenttype = 'Project' for update", project, as_dict=True)
    return flt(sum(round(flt(client_amount) * flt(r.percentage) / 100, 2) for r in rows if r.supplier), 2)


def last_customer(project):
    name = frappe.db.get_value("Sales Invoice", {"project": project, "docstatus": 1}, "name",
                               order_by="posting_date desc", for_update=True)
    if name:
        lock_documents([("Sales Invoice", name)])
    return check_document("Sales Invoice", name).customer if name else None


def margin_pct(client, engineer, referral):
    client = flt(client)
    return flt((client - flt(engineer) - flt(referral)) / client * 100, 2) if client else 0


def bill_names(row):
    return [n for n in (row.engineer_bills or "").split() if n]


def fmt(amount):
    return f"${flt(amount, 2):,.2f}"


# -------------------------------------------------------------------- approving


def approve_project(row):
    """Submit the client invoice (which emails it), then the engineer and referral bills, held until the client
    pays. Raises on any failure, so the caller rolls the whole project back."""
    scope = resolve_scope()
    invoice = row.sales_invoice
    if invoice and not frappe.db.exists("Sales Invoice", invoice):
        frappe.throw(_("The reviewed invoice no longer exists. Prepare the project again."))
    email = {"state": "not_applicable"}
    if invoice:
        si = check_document("Sales Invoice", invoice, company=scope["company"], currency=scope["currency"])
        if si.docstatus == 0:
            si.submit()
            email = si.flags.get("books_assistant_email_result") or {"state": "not_queued"}
        elif si.docstatus == 2:
            frappe.throw(_("{0} was cancelled.").format(invoice))
        else:
            email = {"state": "previously_submitted"}
    # Referral fees only for the invoice this run submits; drafts for older invoices are left to whoever made them.
    referral_bills = frappe.get_all("Purchase Invoice", filters={"referral_source_si": invoice, "docstatus": 0},
                                    pluck="name") if invoice else []
    bills = bill_names(row)
    if any(not frappe.db.exists("Purchase Invoice", b) for b in bills):
        frappe.throw(_("A reviewed engineer bill no longer exists. Prepare the project again."))
    wait_on = invoice or invoiced_by(bills)
    unpaid = bool(wait_on) and flt(check_document("Sales Invoice", wait_on, company=scope["company"],
                                                 currency=scope["currency"]).outstanding_amount) > 0.009
    hold = email_settings().hold_engineer_bills and row.customer and \
        (unpaid or not wait_on)
    submitted = []
    for name in bills + referral_bills:
        pi = check_document("Purchase Invoice", name, company=scope["company"], currency=scope["currency"])
        pi.check_permission("submit")
        if pi.docstatus != 0:
            continue
        pi.client_invoice = wait_on
        if hold:
            pi.on_hold = 1
            pi.hold_comment = (_("Paid after the client pays {0}.").format(wait_on) if wait_on else
                               _("No client invoice in Billing Run {0}. Release by hand once the client has paid for "
                                 "these hours.").format(row.parent))
        pi.submit()
        submitted.append(name)
    if not row.customer:
        held = _(" (internal, not held)")
    elif not hold:
        held = _(", not held: {0} is already paid").format(wait_on) if wait_on else ""
    elif wait_on:
        held = _(", held until the client pays {0}").format(wait_on)
    else:
        held = _(", held until released by hand")
    message = _("Approved: {0}{1} bill(s) submitted{2}.").format(
        _("{0} submitted; ").format(invoice) if invoice else "", len(submitted), held if submitted else "")
    return {"project": row.project, "status": "approved", "sales_invoice": invoice,
            "submitted_bills": submitted, "engineer_bills": bills, "referral_bills": referral_bills,
            "held": bool(hold), "waiting_on": wait_on, "email": email, "message": message,
            "bill_results": [dict(name=name, **(frappe.db.get_value("Purchase Invoice", name,
                              ["docstatus", "on_hold", "client_invoice"], as_dict=True) or {}))
                             for name in bills + referral_bills]}


def invoiced_by(bills):
    """The single submitted client invoice that already billed these bills' timesheets, if there is one."""
    if not bills:
        return None
    found = frappe.db.sql_list(
        """
        select distinct td.sales_invoice from `tabTimesheet Detail` td
        join `tabTimesheet` ts on ts.name = td.parent join `tabSales Invoice` si on si.name = td.sales_invoice
        where ts.purchase_invoice in %(bills)s and si.docstatus = 1
        for update
        """,
        {"bills": bills},
    )
    return found[0] if len(found) == 1 else None
