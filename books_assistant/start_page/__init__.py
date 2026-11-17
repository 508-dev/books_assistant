"""The "Start Here" page: the first thing people see in the ERP, with big buttons for the common tasks.

It's a public Workspace holding one Custom HTML Block. Both are created and kept up to date on every migrate
(edit the files next to this one, not the records in the ERP), and the workspace becomes the default landing
page for every user who hasn't picked their own.
"""

import json
import os

import frappe

WORKSPACE = "Start Here"
BLOCK = "Start Here Actions"
HERE = os.path.dirname(__file__)


def _read(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def ensure():
    """Create or update the block and the workspace, and make it the default landing page."""
    block = frappe.get_doc("Custom HTML Block", BLOCK) if frappe.db.exists("Custom HTML Block", BLOCK) \
        else frappe.new_doc("Custom HTML Block")
    block.update({"name": BLOCK, "html": _read("actions.html"), "script": _read("actions.js"),
                  "style": _read("actions.css"), "private": 0})
    block.flags.ignore_permissions = True
    block.save() if not block.is_new() else block.insert(set_name=BLOCK)

    content = [
        {"id": "start-here-header", "type": "header",
         "data": {"text": '<span class="h4"><b>What do you want to do?</b></span>', "col": 12}},
        {"id": "start-here-actions", "type": "custom_block", "data": {"custom_block_name": BLOCK, "col": 12}},
    ]
    ws = frappe.get_doc("Workspace", WORKSPACE) if frappe.db.exists("Workspace", WORKSPACE) else frappe.new_doc("Workspace")
    # No module: Frappe only shows a module's workspaces to people with access to that module, and this one is
    # for everyone (engineers can't open anything in Books Assistant).
    ws.update({"label": WORKSPACE, "title": WORKSPACE, "public": 1, "module": None, "icon": "home",
               "sequence_id": 0.5, "is_hidden": 0, "content": json.dumps(content)})
    ws.set("custom_blocks", [{"custom_block_name": BLOCK, "label": BLOCK}])
    ws.flags.ignore_permissions = True
    ws.save() if not ws.is_new() else ws.insert()

    # Everyone who hasn't chosen a default lands here. A user can still pick another in their settings.
    frappe.db.sql(
        "update `tabUser` set default_workspace = %s where enabled = 1 and user_type = 'System User' "
        "and ifnull(default_workspace, '') = ''",
        WORKSPACE,
    )


def default_for_new_user(doc, method=None):
    if doc.user_type in (None, "", "System User") and not doc.get("default_workspace") \
            and frappe.db.exists("Workspace", WORKSPACE):
        doc.default_workspace = WORKSPACE


@frappe.whitelist()
def counts():
    """Small numbers shown on the buttons, only for what the user's roles let them act on."""
    roles = set(frappe.get_roles())
    out = {}
    employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user, "status": "Active"}, "name")
    if employee:
        out["my_draft_timesheets"] = frappe.db.count("Timesheet", {"employee": employee, "docstatus": 0})
    if roles & {"Accounts Manager", "System Manager"}:
        out["bank_lines"] = frappe.db.count("Bank Transaction", {"docstatus": 1, "unallocated_amount": [">", 0.009]})
        out["bills_to_pay"] = frappe.db.count("Purchase Invoice", {"docstatus": 1, "is_return": 0, "on_hold": 0,
                                                                   "outstanding_amount": [">", 0.009]})
        out["held_bills"] = frappe.db.count("Purchase Invoice", {"docstatus": 1, "on_hold": 1,
                                                                 "outstanding_amount": [">", 0.009]})
    if roles & {"Accounts Manager", "System Manager", "Billing Approver"}:
        out["unpaid_invoices"] = frappe.db.count("Sales Invoice", {"docstatus": 1, "is_return": 0,
                                                                   "outstanding_amount": [">", 0.009]})
        run = frappe.get_all("Billing Run", filters={"status": ["in", ["Draft", "Prepared"]]},
                             fields=["name", "status"], order_by="creation desc", limit=1)
        out["open_billing_run"] = run[0] if run else None
    out["employee"] = employee
    return out
