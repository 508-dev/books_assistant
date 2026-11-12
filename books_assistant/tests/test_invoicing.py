"""Invoice emails, bounces, holds and the billing run's arithmetic. Runs against the site's own data with made-up
customers and addresses; everything is rolled back after each test, and no email is sent."""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from books_assistant import holds, invoice_email as ie
from books_assistant.books_assistant.doctype.billing_run import billing_run as br


def customer(**fields):
    doc = frappe.get_doc({"doctype": "Customer", "customer_name": f"Test Customer {frappe.generate_hash(length=6)}",
                          "customer_type": "Company", **fields}).insert()
    return doc.name


def invoice(customer_name, **fields):
    company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name", limit=1)[0]
    si = frappe.get_doc({
        "doctype": "Sales Invoice", "customer": customer_name, "company": company, "posting_date": today(),
        "due_date": add_days(today(), 30), "items": [{"item_code": br.SALES_ITEM, "qty": 2, "rate": 100}], **fields,
    }).insert()
    return si


class TestEmails(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        frappe.flags.mute_emails = True

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_document_cache("Automation Settings", "Automation Settings")

    def test_split_emails(self):
        self.assertEqual(ie.split_emails("a@x.com, b@x.com;\nA@x.com\n not-an-email ,c@x.com"),
                         ["a@x.com", "b@x.com", "c@x.com"])
        self.assertEqual(ie.split_emails(None), [])

    def test_recipients_prefer_invoice_addresses(self):
        name = customer(invoice_to="ap@example.com\nfin@example.com", invoice_cc="pm@example.com, ap@example.com",
                        email_id="general@example.com")
        frappe.get_single("Automation Settings").db_set("always_cc", "books@example.com")
        frappe.clear_document_cache("Automation Settings", "Automation Settings")
        to, cc = ie.recipients(invoice(name))
        self.assertEqual(to, ["ap@example.com", "fin@example.com"])
        self.assertEqual(cc, ["pm@example.com", "books@example.com"])

    def test_recipients_fall_back_to_customer_email(self):
        to, _cc = ie.recipients(invoice(customer(email_id="general@example.com")))
        self.assertEqual(to, ["general@example.com"])
        self.assertEqual(ie.recipients(invoice(customer()))[0], [])

    def test_print_format_choice(self):
        settings = frappe.get_single("Automation Settings")
        si = invoice(customer())
        self.assertEqual(ie.print_format(si), settings.invoice_print_format)
        si.append("timesheets", {"billing_hours": 1, "billing_amount": 100})
        self.assertEqual(ie.print_format(si), settings.timesheet_print_format)
        other = frappe.get_all("Print Format", filters={"doc_type": "Sales Invoice", "disabled": 0}, pluck="name")[-1]
        frappe.db.set_value("Customer", si.customer, "invoice_print_format", other)
        self.assertEqual(ie.print_format(si), other)

    def test_submit_without_address_or_role_sends_nothing(self):
        si = invoice(customer())
        with patch.object(ie, "send") as send:
            with patch("frappe.get_roles", return_value=["Accounts User"]):
                ie.on_submit(si, "on_submit")
            send.assert_not_called()

    def test_failed_email_does_not_block_the_invoice(self):
        si = invoice(customer(invoice_to="ap@example.com"))
        with patch.object(ie, "send", side_effect=frappe.OutgoingEmailError("No outgoing account")), \
                patch("frappe.get_roles", return_value=["Billing Approver"]):
            ie.on_submit(si, "on_submit")
        self.assertTrue(frappe.db.exists("Comment", {"reference_name": si.name, "content": ["like", "Not emailed%"]}))


class TestBounces(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def sent(self, si, to):
        return frappe.get_doc({
            "doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email",
            "sent_or_received": "Sent", "recipients": to, "subject": f"Invoice {si.name}",
            "reference_doctype": "Sales Invoice", "reference_name": si.name,
        }).insert(ignore_permissions=True)

    def bounce(self, content, **fields):
        return frappe.get_doc({
            "doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email",
            "sent_or_received": "Received", "sender": "MAILER-DAEMON@mail.example.net",
            "subject": "Undelivered Mail Returned to Sender", "content": content, **fields,
        }).insert(ignore_permissions=True)

    def test_bounce_found_by_address(self):
        si = invoice(customer())
        email = self.sent(si, "gone@example.com")
        self.bounce("<gone@example.com>: host said: 550 5.1.1 User unknown")
        self.assertEqual(frappe.db.get_value("Communication", email.name, "delivery_status"), "Bounced")
        note = frappe.get_all("Comment", filters={"reference_name": si.name, "content": ["like", "%bounced%"]},
                              pluck="content")
        self.assertIn("550 5.1.1", note[0])

    def test_bounce_found_by_reply_link(self):
        si = invoice(customer())
        email = self.sent(si, "x@example.com")
        self.bounce("Delivery failed", in_reply_to=email.name)
        self.assertEqual(frappe.db.get_value("Communication", email.name, "delivery_status"), "Bounced")

    def test_ordinary_reply_is_not_a_bounce(self):
        si = invoice(customer())
        email = self.sent(si, "client@example.com")
        frappe.get_doc({
            "doctype": "Communication", "communication_type": "Communication", "communication_medium": "Email",
            "sent_or_received": "Received", "sender": "client@example.com", "subject": f"Re: Invoice {si.name}",
            "content": "Thanks, paying this week. client@example.com",
        }).insert(ignore_permissions=True)
        self.assertNotEqual(frappe.db.get_value("Communication", email.name, "delivery_status"), "Bounced")

    def test_bounce_reason(self):
        self.assertEqual(ie._bounce_reason("x\n550 5.1.1 <a@b.c>: User unknown\ny"), "550 5.1.1 <a@b.c>: User unknown")
        self.assertEqual(ie._bounce_reason("nothing useful"), "")


class TestHoldsAndRun(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def test_margin(self):
        self.assertEqual(br.margin_pct(1000, 800, 100), 10)
        self.assertEqual(br.margin_pct(0, 800, 0), 0)

    def test_period_label(self):
        run = frappe.get_doc({"doctype": "Billing Run", "period_start": "2026-10-01", "period_end": "2026-10-31"})
        self.assertEqual(run.period, "October 2026")
        run.period_end = "2026-10-15"
        self.assertNotEqual(run.period, "October 2026")

    def test_period_must_be_in_order(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({"doctype": "Billing Run", "period_start": "2026-10-31", "period_end": "2026-10-01",
                            "invoice_date": today()}).insert()

    def test_release_only_when_client_paid(self):
        held = frappe.get_all("Purchase Invoice", filters={"docstatus": 1, "outstanding_amount": [">", 0]},
                              fields=["name"], limit=1)
        si = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "outstanding_amount": [">", 0]}, pluck="name",
                            limit=1)
        if not held or not si:
            self.skipTest("needs an unpaid bill and an unpaid invoice")
        frappe.db.set_value("Purchase Invoice", held[0].name, {"on_hold": 1, "client_invoice": si[0]})
        holds.release_paid_holds()
        self.assertEqual(frappe.db.get_value("Purchase Invoice", held[0].name, "on_hold"), 1)
        frappe.db.set_value("Sales Invoice", si[0], "outstanding_amount", 0)
        holds.release_paid_holds()
        self.assertEqual(frappe.db.get_value("Purchase Invoice", held[0].name, "on_hold"), 0)

    def test_release_can_be_switched_off(self):
        bill = frappe.get_all("Purchase Invoice", filters={"docstatus": 1}, pluck="name", limit=1)
        paid = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "outstanding_amount": 0}, pluck="name", limit=1)
        if not bill or not paid:
            self.skipTest("needs a bill and a paid invoice")
        frappe.db.set_value("Purchase Invoice", bill[0], {"on_hold": 1, "client_invoice": paid[0]})
        frappe.get_single("Automation Settings").db_set("auto_release_holds", 0)
        holds.release_paid_holds()
        self.assertEqual(frappe.db.get_value("Purchase Invoice", bill[0], "on_hold"), 1)
        frappe.get_single("Automation Settings").db_set("auto_release_holds", 1)
        holds.release_paid_holds()
        self.assertEqual(frappe.db.get_value("Purchase Invoice", bill[0], "on_hold"), 0)


class TestNeverBlocks(unittest.TestCase):
    """Problems in this app must never stop an invoice or payment being submitted, nor fail an install."""

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.rollback()

    def test_invoice_submits_when_settings_are_unreadable(self):
        si = invoice(customer(invoice_to="ap@example.com"))
        with patch.object(ie, "settings", side_effect=ImportError("Module import failed for Automation Settings")), \
                patch("frappe.get_roles", return_value=["Billing Approver"]):
            ie.on_submit(si, "on_submit")
        self.assertTrue(frappe.db.exists("Comment", {"reference_name": si.name, "content": ["like", "Not emailed%"]}))

    def test_payment_submits_when_releasing_fails(self):
        pe = frappe._dict(doctype="Payment Entry", name="TEST-PAY")
        with patch.object(holds, "release_paid_holds", side_effect=ImportError("Automation Settings missing")):
            holds.release_after_payment(pe)
        self.assertTrue(frappe.db.exists("Error Log", {"method": "Releasing held bills failed", "reference_name": "TEST-PAY"}))

    def test_settings_wait_for_their_doctype(self):
        from books_assistant import setup

        real_exists = frappe.db.exists
        with patch.object(frappe.db, "exists", side_effect=lambda dt, *a, **k: False if (dt, a[:1]) == (
                "DocType", ("Automation Settings",)) else real_exists(dt, *a, **k)), \
                patch("frappe.get_single", side_effect=AssertionError("must not open the settings")):
            setup.initialise_settings()
