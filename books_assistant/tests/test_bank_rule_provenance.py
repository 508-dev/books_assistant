"""Rule-created journals retain provenance even when automation is disabled."""

import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from books_assistant.tests import test_bank_auto_guards as guards
from books_assistant.tests.test_bank_auto_guards import Doc


class TestRuleBookingProvenance(unittest.TestCase):
    def setUp(self):
        fixture = guards.TestAutomaticBookingGuards()
        fixture.setUp()
        self.auto, self.frappe, self.rule = fixture.auto, fixture.frappe, fixture.rule
        self.frappe.flags = SimpleNamespace(bank_auto_booking=False)
        self.frappe.db.exists = Mock(return_value=False)
        self.insert = Mock(return_value=Doc(name="BT-NEW"))
        self.payloads = []

        def get_doc(value, name=None):
            if isinstance(value, dict):
                self.payloads.append(value)
                return SimpleNamespace(insert=self.insert)
            return Doc(name="BT-NEW", journal_entry="JE-NEW", rule="GitHub")

        self.frappe.get_doc = Mock(side_effect=get_doc)
        self.bt = Doc(name="BT-NEW", bank_account="Operating Bank", currency="USD", deposit=0, withdrawal=20,
                      unallocated_amount=0, docstatus=1)
        self.journal = Doc(name="JE-NEW", docstatus=1, company="Co-op")
        docs = {"Bank Transaction": self.bt, "Bank Rule": self.rule,
                "Bank Account": Doc(company="Co-op"), "Journal Entry": self.journal}
        integration = fixture._authorization_dependencies()
        integration.check_document = Mock(side_effect=lambda dt, *args, **kwargs: docs[dt])
        self.patch = patch.dict(sys.modules, {"books_assistant.integration": integration})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_manual_rule_records_provenance_without_granting_auto_authority(self):
        self.assertEqual(self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-NEW"), "BT-NEW")
        payload = self.payloads[0]
        self.assertEqual((payload["bank_transaction"], payload["rule"], payload["journal_entry"]),
                         ("BT-NEW", "GitHub", "JE-NEW"))
        self.assertEqual(payload["automatic"], 0)
        self.assertIsNone(payload["approval_json"])
        self.assertEqual(payload["actor"], "finance@example.test")
        self.insert.assert_called_once_with(ignore_permissions=True)

    def test_scheduler_flag_retains_exact_reviewed_authorization(self):
        self.frappe.flags.bank_auto_booking = True
        self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-NEW")
        self.assertEqual(self.payloads[0]["automatic"], 1)
        self.assertEqual(self.payloads[0]["approval_json"], self.rule.auto_approval_json)

    def test_replay_reuses_original_record_without_inserting_another(self):
        self.frappe.db.exists.return_value = True
        self.assertEqual(self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-NEW"), "BT-NEW")
        self.insert.assert_not_called()
        self.assertEqual(self.payloads, [])

    def test_reusing_transaction_for_another_journal_is_rejected(self):
        self.frappe.db.exists.return_value = True
        with self.assertRaises(ValueError):
            self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-DIFFERENT")
        self.insert.assert_not_called()

    def test_incomplete_reconciliation_or_unsubmitted_journal_cannot_claim_success(self):
        self.bt.unallocated_amount = 10
        with self.assertRaises(ValueError):
            self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-NEW")
        self.bt.unallocated_amount = 0
        self.journal.docstatus = 0
        with self.assertRaises(ValueError):
            self.auto.record_rule_booking("BT-NEW", "GitHub", "JE-NEW")
        self.insert.assert_not_called()
