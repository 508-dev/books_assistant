"""Disposable-site fixtures for a real old-schema -> new-schema migration test.

Copy this file into the old app checkout before seeding. It deliberately imports
no new app modules. Never enable these fixtures on a production site.
"""

import json
from pathlib import Path
import unittest

import frappe
from frappe.utils import add_days, getdate, today

COMPANY = "Synthetic Finance API Company"
ABBR = "SFAPI"
MANIFEST = "books_assistant_synthetic_migration.json"


def _guard():
    if not frappe.conf.get("books_assistant_synthetic_site"):
        frappe.throw(
            "Set books_assistant_synthetic_site=1 only on a disposable test site."
        )
    frappe.set_user("Administrator")
    frappe.flags.mute_emails = True


def _ensure(doctype, name, **fields):
    if frappe.db.exists(doctype, name):
        return frappe.get_doc(doctype, name)
    return frappe.get_doc({"doctype": doctype, **fields}).insert(
        ignore_permissions=True
    )


def prepare_legacy_install():
    """Provide the setup masters and site-specific formats required by the old app."""
    _guard()
    from erpnext.setup.setup_wizard.operations.install_fixtures import install

    if not frappe.db.exists("Item Group", "All Item Groups"):
        install(country="United States")
    if frappe.db.exists("DocType", "Department") and not frappe.db.exists(
        "Department", "All Departments"
    ):
        frappe.get_doc(
            {
                "doctype": "Department",
                "department_name": "All Departments",
                "is_group": 1,
            }
            # ERP's global tree root predates a company; child departments still
            # use ordinary company validation. This exception is fixture-only.
        ).insert(ignore_permissions=True, ignore_mandatory=True)
    # The baseline schema references these production-local names as Link
    # defaults before its after_install hook can run. Synthetic placeholders
    # reproduce that configuration; no PDF is sent or production template used.
    for name in (
        "Sales Invoice with Bank Info",
        "Sales Invoice with Bank Info and Timesheet",
    ):
        if not frappe.db.exists("Print Format", name):
            frappe.get_doc(
                {
                    "doctype": "Print Format",
                    "name": name,
                    "doc_type": "Sales Invoice",
                    "print_format_type": "Jinja",
                    "custom_format": 1,
                    "html": "<p>{{ doc.name }}</p>",
                }
            ).insert(ignore_permissions=True)
    frappe.db.commit()


def _leaf_account(company, root_type):
    names = frappe.get_all(
        "Account",
        filters={"company": company, "root_type": root_type, "is_group": 0},
        pluck="name",
        order_by="name",
        limit=1,
    )
    if not names:
        frappe.throw(f"Synthetic chart has no {root_type} account.")
    return names[0]


def seed_minimal_site():
    """Create one USD company, standard chart, parties, nonstock service item and bank."""
    _guard()
    companies = frappe.get_all("Company", pluck="name")
    if companies and companies != [COMPANY]:
        frappe.throw(
            "Synthetic fixtures require an empty site or their own single test company."
        )
    roots = (
        ("Item Group", "All Item Groups"),
        ("Customer Group", "All Customer Groups"),
        ("Supplier Group", "All Supplier Groups"),
        ("Territory", "All Territories"),
        ("UOM", "Nos"),
    )
    if any(not frappe.db.exists(doctype, name) for doctype, name in roots):
        # A freshly installed ERPNext site may not have completed its setup wizard.
        from erpnext.setup.setup_wizard.operations.install_fixtures import install

        install(country="United States")
    company = _ensure(
        "Company",
        COMPANY,
        company_name=COMPANY,
        abbr=ABBR,
        country="United States",
        default_currency="USD",
        create_chart_of_accounts_based_on="Standard Template",
        chart_of_accounts="Standard",
    )
    company.reload()
    if company.default_currency != "USD":
        frappe.throw("Synthetic company must use USD.")
    company.default_income_account = _leaf_account(COMPANY, "Income")
    company.default_expense_account = _leaf_account(COMPANY, "Expense")
    for field, account_type in (
        ("stock_received_but_not_billed", "Stock Received But Not Billed"),
        ("asset_received_but_not_billed", "Asset Received But Not Billed"),
        ("default_inventory_account", "Stock"),
        ("stock_adjustment_account", "Stock Adjustment"),
        ("expenses_included_in_valuation", "Expenses Included In Valuation"),
        (
            "expenses_included_in_asset_valuation",
            "Expenses Included In Asset Valuation",
        ),
    ):
        if company.meta.has_field(field):
            company.set(
                field,
                frappe.db.get_value(
                    "Account",
                    {
                        "company": COMPANY,
                        "is_group": 0,
                        "account_type": account_type,
                    },
                    "name",
                ),
            )
    company.save(ignore_permissions=True)
    year = getdate(today()).year
    from erpnext.accounts.utils import get_fiscal_year

    if not get_fiscal_year(today(), company=COMPANY, boolean=True):
        _ensure(
            "Fiscal Year",
            f"Synthetic {year}",
            year=f"Synthetic {year}",
            year_start_date=f"{year}-01-01",
            year_end_date=f"{year}-12-31",
            companies=[{"company": COMPANY}],
        )
    frappe.defaults.set_global_default("company", COMPANY)
    frappe.defaults.set_user_default("Company", COMPANY)
    frappe.defaults.set_global_default("currency", "USD")
    selling = _ensure(
        "Price List",
        "Synthetic API Selling",
        price_list_name="Synthetic API Selling",
        currency="USD",
        selling=1,
        buying=0,
    )
    buying = _ensure(
        "Price List",
        "Synthetic API Buying",
        price_list_name="Synthetic API Buying",
        currency="USD",
        selling=0,
        buying=1,
    )
    frappe.db.set_single_value("Selling Settings", "selling_price_list", selling.name)
    frappe.db.set_single_value("Buying Settings", "buying_price_list", buying.name)
    _ensure("UOM", "Hour", uom_name="Hour", must_be_whole_number=0)
    _ensure(
        "Customer Group",
        "Synthetic API Clients",
        customer_group_name="Synthetic API Clients",
        parent_customer_group="All Customer Groups",
        is_group=0,
    )
    _ensure(
        "Supplier Group",
        "Synthetic API Suppliers",
        supplier_group_name="Synthetic API Suppliers",
        parent_supplier_group="All Supplier Groups",
        is_group=0,
    )
    _ensure(
        "Item Group",
        "Synthetic API Services",
        item_group_name="Synthetic API Services",
        parent_item_group="All Item Groups",
        is_group=0,
    )
    _ensure(
        "Territory",
        "Synthetic API Territory",
        territory_name="Synthetic API Territory",
        parent_territory="All Territories",
        is_group=0,
    )
    customer = _ensure(
        "Customer",
        "Synthetic API Client",
        customer_name="Synthetic API Client",
        customer_type="Company",
        customer_group="Synthetic API Clients",
        territory="Synthetic API Territory",
    )
    supplier = _ensure(
        "Supplier",
        "Synthetic API Engineer",
        supplier_name="Synthetic API Engineer",
        supplier_type="Individual",
        supplier_group="Synthetic API Suppliers",
        country="United States",
    )
    item = _ensure(
        "Item",
        "SYNTHETIC-API-SERVICE",
        item_code="SYNTHETIC-API-SERVICE",
        item_name="Synthetic API service",
        item_group="Synthetic API Services",
        stock_uom="Hour",
        is_stock_item=0,
    )
    bank = _ensure("Bank", "Synthetic API Bank", bank_name="Synthetic API Bank")
    account_name = f"Synthetic API Bank - {ABBR}"
    if not frappe.db.exists("Account", account_name):
        parent = frappe.get_all(
            "Account",
            filters={"company": COMPANY, "root_type": "Asset", "is_group": 1},
            pluck="name",
            order_by="lft",
            limit=1,
        )[0]
        frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": "Synthetic API Bank",
                "company": COMPANY,
                "parent_account": parent,
                "is_group": 0,
                "account_type": "Bank",
                "account_currency": "USD",
            }
        ).insert(ignore_permissions=True)
    bank_accounts = frappe.get_all(
        "Bank Account",
        filters={"company": COMPANY, "account": account_name},
        pluck="name",
    )
    if not bank_accounts:
        bank_account = frappe.get_doc(
            {
                "doctype": "Bank Account",
                "account_name": "Synthetic API Operating",
                "bank": bank.name,
                "company": COMPANY,
                "is_company_account": 1,
                "account": account_name,
                "bank_account_no": "0000000000",
            }
        ).insert(ignore_permissions=True)
    else:
        bank_account = frappe.get_doc("Bank Account", bank_accounts[0])
    if frappe.db.exists("DocType", "Automation Settings"):
        frappe.db.set_single_value("Automation Settings", "send_invoice_emails", 0)
        frappe.clear_document_cache("Automation Settings", "Automation Settings")
    result = {
        "company": COMPANY,
        "currency": "USD",
        "customer": customer.name,
        "supplier": supplier.name,
        "item": item.name,
        "bank_account": bank_account.name,
        "bank_gl": account_name,
        "income_account": _leaf_account(COMPANY, "Income"),
        "expense_account": _leaf_account(COMPANY, "Expense"),
        "cost_center": company.cost_center,
        "selling_price_list": selling.name,
        "buying_price_list": buying.name,
    }
    frappe.db.commit()
    return result


def _manifest_path():
    return Path(frappe.get_site_path("private", "files", MANIFEST))


def _canonical(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _financial_snapshot(names):
    return {
        doctype: {
            name: _canonical(frappe.get_doc(doctype, name).as_dict()) for name in values
        }
        for doctype, values in names.items()
    }


def _ledger_snapshot(names):
    return _canonical(
        frappe.get_all(
            "GL Entry",
            filters={"voucher_no": ["in", names]},
            fields=["*"],
            order_by="name",
        )
    )


def seed_legacy_migration():
    """Must run with the old Billing Run schema and controller installed."""
    _guard()
    manifest_path = _manifest_path()
    if manifest_path.exists():
        frappe.throw(
            "This site already has a synthetic migration manifest; use a fresh disposable site."
        )
    for doctype, fields in (
        ("Billing Run", ("company", "currency")),
        ("Billing Run Item", ("item_status", "error_code", "result_json")),
    ):
        if any(frappe.get_meta(doctype).has_field(field) for field in fields):
            frappe.throw("Seed legacy fixtures before installing the new app schema.")
    if any(
        frappe.db.exists("DocType", doctype)
        for doctype in ("API Preview", "API Request")
    ):
        frappe.throw(
            "The legacy fixture must not already contain the new approval tables."
        )
    fixture = seed_minimal_site()
    cases = [
        ("pending", "", "Prepared"),
        ("approved", "Approved: historical synthetic run.", "Approved"),
        ("left_out", "Left out by a historical operator.", "Approved"),
        ("failed", "Not approved: historical validation failure.", "Prepared"),
        ("review_required", "Genehmigt: historischer Testlauf.", "Approved"),
    ]
    records, names = [], {"Sales Invoice": [], "Purchase Invoice": []}
    for expected, outcome, status in cases:
        project = frappe.get_doc(
            {
                "doctype": "Project",
                "project_name": f"Synthetic migration {expected}",
                "customer": fixture["customer"],
                "company": COMPANY,
            }
        ).insert(ignore_permissions=True)
        sales_invoice = purchase_invoice = None
        if expected != "left_out":
            common = {
                "company": COMPANY,
                "currency": "USD",
                "project": project.name,
                "posting_date": today(),
                "due_date": add_days(today(), 30),
                "set_posting_time": 1,
                "conversion_rate": 1,
                "price_list_currency": "USD",
                "plc_conversion_rate": 1,
            }
            sales_invoice = frappe.get_doc(
                {
                    "doctype": "Sales Invoice",
                    **common,
                    "customer": fixture["customer"],
                    "selling_price_list": fixture["selling_price_list"],
                    "items": [
                        {
                            "item_code": fixture["item"],
                            "qty": 2,
                            "rate": 100,
                            "income_account": fixture["income_account"],
                            "cost_center": fixture["cost_center"],
                            "project": project.name,
                        }
                    ],
                }
            ).insert(ignore_permissions=True)
            purchase_invoice = frappe.get_doc(
                {
                    "doctype": "Purchase Invoice",
                    **common,
                    "supplier": fixture["supplier"],
                    "buying_price_list": fixture["buying_price_list"],
                    "items": [
                        {
                            "item_code": fixture["item"],
                            "qty": 2,
                            "rate": 75,
                            "expense_account": fixture["expense_account"],
                            "cost_center": fixture["cost_center"],
                            "project": project.name,
                        }
                    ],
                }
            ).insert(ignore_permissions=True)
            if expected in ("approved", "review_required"):
                sales_invoice.submit()
                purchase_invoice.client_invoice = sales_invoice.name
                purchase_invoice.on_hold = 1
                purchase_invoice.hold_comment = (
                    "Synthetic historical client-payment hold"
                )
                purchase_invoice.submit()
            names["Sales Invoice"].append(sales_invoice.name)
            names["Purchase Invoice"].append(purchase_invoice.name)
        run = frappe.get_doc(
            {
                "doctype": "Billing Run",
                "period_start": "2026-10-01",
                "period_end": "2026-10-31",
                "invoice_date": today(),
                "status": status,
                "items": [
                    {
                        "project": project.name,
                        "customer": fixture["customer"],
                        "sales_invoice": sales_invoice.name if sales_invoice else None,
                        "engineer_bills": purchase_invoice.name
                        if purchase_invoice
                        else None,
                        "client_amount": 200 if sales_invoice else 0,
                        "engineer_cost": 150 if purchase_invoice else 0,
                        "outcome": outcome,
                    }
                ],
            }
        ).insert(ignore_permissions=True)
        row = run.items[0]
        records.append(
            {
                "run": run.name,
                "item": row.name,
                "expected": expected,
                "outcome": outcome,
                "sales_invoice": row.sales_invoice,
                "engineer_bills": row.engineer_bills,
                "run_modified": str(run.modified),
                "item_modified": str(row.modified),
            }
        )
    frappe.db.commit()
    document_names = sum(names.values(), [])
    manifest = {
        "company": COMPANY,
        "currency": "USD",
        "cases": records,
        "names": names,
        "financial_documents": _financial_snapshot(names),
        "gl_entries": _ledger_snapshot(document_names),
        "print_formats": {
            field: frappe.db.get_single_value("Automation Settings", field)
            for field in ("invoice_print_format", "timesheet_print_format")
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
    )
    return {
        "legacy_runs": len(records),
        "financial_documents": len(document_names),
        "submitted_gl_entries": len(manifest["gl_entries"]),
        "manifest": MANIFEST,
    }


def assert_legacy_migration():
    """Verify actual post-model-sync migration and preservation of accounting data."""
    _guard()
    manifest = json.loads(_manifest_path().read_text())
    assert {
        field: frappe.db.get_single_value("Automation Settings", field)
        for field in manifest["print_formats"]
    } == manifest["print_formats"], (
        "Existing selected print formats changed during migration"
    )
    for doctype, fields in (
        ("Billing Run", ("company", "currency")),
        ("Billing Run Item", ("item_status", "error_code", "result_json")),
    ):
        for field in fields:
            assert frappe.get_meta(doctype).has_field(field), (doctype, field)
    for doctype in ("API Preview", "API Request"):
        assert frappe.db.exists("DocType", doctype), doctype
    patch = "books_assistant.patches.structure_billing_status"
    assert frappe.db.exists("Patch Log", {"patch": patch}), (
        "Migration patch did not run through bench migrate"
    )
    for case in manifest["cases"]:
        run, row = (
            frappe.get_doc("Billing Run", case["run"]),
            frappe.get_doc("Billing Run Item", case["item"]),
        )
        assert row.item_status == case["expected"], (case["expected"], row.item_status)
        if "error_code" in case:
            assert row.error_code == case["error_code"], (
                "A repeated migrate remapped an existing machine state"
            )
        assert row.outcome == case["outcome"]
        assert row.sales_invoice == case["sales_invoice"]
        assert row.engineer_bills == case["engineer_bills"]
        assert (
            run.company == manifest["company"] and run.currency == manifest["currency"]
        )
        assert (
            str(run.modified) == case["run_modified"]
            and str(row.modified) == case["item_modified"]
        )
        if case["expected"] == "review_required":
            assert run.status == "Prepared", (
                "Unknown legacy outcomes must require review"
            )
    assert _financial_snapshot(manifest["names"]) == manifest["financial_documents"], (
        "Accounting documents changed during migration"
    )
    assert (
        _ledger_snapshot(sum(manifest["names"].values(), [])) == manifest["gl_entries"]
    ), "General ledger changed during migration"
    return {
        "migration_cases_verified": len(manifest["cases"]),
        "financial_documents_preserved": sum(map(len, manifest["names"].values())),
        "gl_entries_preserved": len(manifest["gl_entries"]),
    }


def prepare_repeat_migration_check():
    """After the first successful assertion, verify a second migrate skips the completed patch."""
    assert_legacy_migration()
    manifest_path = _manifest_path()
    manifest = json.loads(manifest_path.read_text())
    case = next(case for case in manifest["cases"] if not case["outcome"])
    # A deliberately different post-upgrade state detects an accidental rerun of legacy text interpretation.
    frappe.db.set_value(
        "Billing Run Item",
        case["item"],
        {"item_status": "failed", "error_code": "SYNTHETIC_POST_UPGRADE_STATE"},
        update_modified=False,
    )
    frappe.db.commit()
    case.update(expected="failed", error_code="SYNTHETIC_POST_UPGRADE_STATE")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"second_migration_sentinel": "ready", "item": case["item"]}


def run_site_tests(include_health=False):
    """Run required real-site suites with a failing exit status on errors or skips.

    Some Bench versions print failed tests but still exit zero; raising here
    makes automated synthetic validation unambiguous. This is not an HTTP API.
    """
    _guard()
    seed_minimal_site()
    modules = [
        "books_assistant.tests.test_billing_api",
        "books_assistant.tests.test_integration_transactions",
        "books_assistant.tests.test_finance_synthetic",
        "books_assistant.tests.test_bank_discovery_transactions",
        "books_assistant.tests.test_bank_supplier_permissions",
        "books_assistant.tests.test_read_api_permissions",
    ]
    if include_health:
        if "books_health" not in frappe.get_installed_apps():
            frappe.throw("Install Books Health before requesting its real-site suite.")
        modules.append("books_health.tests.test_api_synthetic")
    previous = frappe.flags.get("in_test")
    frappe.flags.in_test = True
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful() or result.skipped:
            raise AssertionError(
                f"Synthetic site tests failed: {len(result.failures)} failures, "
                f"{len(result.errors)} errors, {len(result.skipped)} skipped."
            )
        return {
            "tests_run": result.testsRun,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "modules": modules,
        }
    finally:
        frappe.flags.in_test = previous
