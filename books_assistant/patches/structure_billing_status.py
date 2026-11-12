"""One-time interpretation of legacy outcomes; runtime decisions use item_status only."""

import frappe


def legacy_status(outcome):
    if not outcome:
        return "pending"
    if outcome.startswith("Approved"):
        return "approved"
    if outcome.startswith("Left out"):
        return "left_out"
    if outcome.startswith("Not approved"):
        return "failed"
    # Translated/custom outcomes cannot be interpreted reliably. Never assume they are payable again.
    return "review_required"


def execute():
    companies = frappe.get_all("Company", fields=["name", "default_currency"])
    scope = companies[0] if len(companies) == 1 else None
    for row in frappe.get_all("Billing Run Item", fields=["name", "parent", "outcome"]):
        status = legacy_status(row.outcome)
        frappe.db.set_value("Billing Run Item", row.name, "item_status", status, update_modified=False)
        if status == "review_required":
            frappe.db.set_value("Billing Run", row.parent, "status", "Prepared", update_modified=False)
    if scope:
        for name in frappe.get_all("Billing Run", pluck="name"):
            frappe.db.set_value("Billing Run", name,
                                {"company": scope.name, "currency": scope.default_currency}, update_modified=False)
