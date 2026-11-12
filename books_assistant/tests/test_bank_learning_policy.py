"""Pure tests for the evidence policy; no ERP site or database is required."""

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from books_assistant.bank_learning import candidate_for_rule, eligible_ledger, infer_candidates


TODAY = date(2026, 11, 13)


def observation(number, days_ago=10, **changes):
    row = {
        "bank_transaction": f"BT-{number}",
        "journal": f"JE-{number}",
        "description": "GITHUB SUBSCRIPTION",
        "date": str(TODAY - timedelta(days=days_ago)),
        "company": "Co-op",
        "currency": "USD",
        "bank_account": "Operating Bank",
        "direction": "Money out",
        "account": "Software Expense",
        "amount": 20,
    }
    return {**row, **changes}


def consistent_history(**changes):
    return [observation(1, 40, **changes), observation(2, 20, **changes), observation(3, 10, **changes)]


class TestBankLearningEvidence(unittest.TestCase):
    def test_three_distinct_lines_on_two_dates_create_an_explained_candidate(self):
        rows = [observation(3, 10, amount=30), observation(1, 20, amount=10), observation(2, 20, amount=20)]
        result = infer_candidates(rows, TODAY)
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["evidence_count"], 3)
        self.assertEqual(candidate["keyword"], "GITHUB")
        self.assertEqual(candidate["last_evidence_on"], str(TODAY - timedelta(days=10)))
        self.assertEqual((candidate["min_observed"], candidate["max_observed"]), (10, 30))
        self.assertEqual([r["bank_transaction"] for r in candidate["evidence"]], ["BT-1", "BT-2", "BT-3"])
        self.assertEqual(candidate["account"], "Software Expense")

    def test_two_lines_do_not_create_a_rule(self):
        self.assertEqual(infer_candidates(consistent_history()[:2], TODAY), [])

    def test_three_lines_on_one_date_are_not_independent_enough(self):
        rows = [observation(number, 10) for number in range(3)]
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_repeated_join_rows_do_not_increase_evidence(self):
        rows = consistent_history()[:2]
        self.assertEqual(infer_candidates(rows * 5, TODAY), [])
        candidate = infer_candidates(consistent_history() * 4, TODAY)[0]
        self.assertEqual(candidate["evidence_count"], 3)

    def test_one_conflicting_classification_blocks_a_three_to_one_majority(self):
        rows = consistent_history() + [observation(4, 5, account="Office Expense")]
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_conflicting_duplicate_join_is_not_silently_counted_for_one_destination(self):
        rows = consistent_history() + [observation(3, 10, account="Office Expense")]
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_ineligible_or_inaccessible_history_blocks_false_unanimity(self):
        marker = observation(4, 5, ineligible=True)
        marker.pop("account")
        self.assertEqual(infer_candidates(consistent_history() + [marker], TODAY), [])

    def test_ineligible_history_in_another_scope_does_not_block_this_bank(self):
        marker = observation(4, 5, bank_account="Other Bank", ineligible=True)
        marker.pop("account")
        self.assertEqual(len(infer_candidates(consistent_history() + [marker], TODAY)), 1)

    def test_scope_boundaries_cannot_be_pooled_to_reach_threshold(self):
        for field, other_value in (
            ("company", "Another Company"),
            ("currency", "EUR"),
            ("bank_account", "Savings Bank"),
            ("direction", "Money in"),
        ):
            with self.subTest(field=field):
                rows = consistent_history()[:2] + [observation(3, 10, **{field: other_value})]
                self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_independent_scopes_keep_their_own_destination_and_amount_range(self):
        rows = consistent_history() + [
            observation(number, days, company="Another Company", currency="EUR", bank_account="Euro Bank",
                        direction="Money in", account="Subscription Income", amount=200)
            for number, days in ((4, 35), (5, 15), (6, 5))
        ]
        candidates = infer_candidates(rows, TODAY)
        self.assertEqual(len(candidates), 2)
        by_company = {c["company"]: c for c in candidates}
        self.assertEqual(by_company["Co-op"]["account"], "Software Expense")
        other = by_company["Another Company"]
        self.assertEqual((other["currency"], other["bank_account"], other["direction"]),
                         ("EUR", "Euro Bank", "Money in"))
        self.assertEqual((other["min_observed"], other["max_observed"]), (200, 200))

    def test_conflict_in_another_scope_does_not_poison_this_scope(self):
        rows = consistent_history() + [observation(4, 10, bank_account="Savings Bank", account="Other Expense")]
        self.assertEqual(len(infer_candidates(rows, TODAY)), 1)

    def test_automatically_generated_bookings_never_become_evidence(self):
        rows = consistent_history()[:2] + [observation(3, 5, generated=True)]
        self.assertEqual(infer_candidates(rows, TODAY), [])
        rows += [observation(4, 3, generated=True), observation(5, 1, generated=True)]
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_automatically_generated_conflicting_booking_is_not_human_evidence(self):
        rows = consistent_history() + [observation(4, 5, generated=True, account="Other Expense")]
        self.assertEqual(infer_candidates(rows, TODAY)[0]["evidence_count"], 3)

    def test_history_boundary_includes_day_180_and_excludes_day_181(self):
        rows = [observation(1, 180), observation(2, 30), observation(3, 10)]
        self.assertEqual(len(infer_candidates(rows, TODAY)), 1)
        rows[0] = observation(1, 181)
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_recent_evidence_is_required_even_with_three_historical_lines(self):
        rows = [observation(1, 120), observation(2, 90), observation(3, 61)]
        self.assertEqual(infer_candidates(rows, TODAY), [])
        rows[2] = observation(3, 60)
        self.assertEqual(len(infer_candidates(rows, TODAY)), 1)

    def test_future_lines_do_not_supply_evidence(self):
        rows = consistent_history()[:2] + [observation(3, -1)]
        self.assertEqual(infer_candidates(rows, TODAY), [])

    def test_provider_names_do_not_identify_the_underlying_merchant(self):
        for description in ("WISE INC", "STRIPE", "PAYPAL", "VENMO", "BILL.COM", "MERCURY", "INTUIT", "508.DEV LLC"):
            with self.subTest(description=description):
                self.assertEqual(infer_candidates(consistent_history(description=description), TODAY), [])

    def test_empty_or_nonidentifying_descriptions_do_not_create_candidates(self):
        for description in (None, "", "123456", "XY"):
            with self.subTest(description=description):
                self.assertEqual(infer_candidates(consistent_history(description=description), TODAY), [])

    def test_iso_date_strings_and_datetime_inputs_produce_identical_candidates(self):
        rows = consistent_history()
        dated = [{**row, "date": datetime.fromisoformat(row["date"])} for row in rows]
        first = infer_candidates(rows, TODAY)[0]
        second = infer_candidates(dated, datetime(2026, 11, 13, 12))[0]
        for key in ("evidence_count", "last_evidence_on", "min_observed", "max_observed"):
            self.assertEqual(first[key], second[key])


class TestRuleEvidenceLookup(unittest.TestCase):
    def setUp(self):
        self.rule = SimpleNamespace(description_contains=" github ", bank_account="Operating Bank",
                                    account="Software Expense", direction="Money out")

    def test_rule_requires_matching_merchant_bank_destination_and_direction(self):
        self.assertIsNotNone(candidate_for_rule(self.rule, consistent_history(), TODAY))
        for field, value in (("description_contains", "KAGI"), ("bank_account", "Savings Bank"),
                             ("account", "Other Expense"), ("direction", "Money in")):
            with self.subTest(field=field):
                changed = SimpleNamespace(**{**vars(self.rule), field: value})
                self.assertIsNone(candidate_for_rule(changed, consistent_history(), TODAY))

    def test_inconsistent_or_insufficient_evidence_does_not_qualify_an_existing_rule(self):
        self.assertIsNone(candidate_for_rule(self.rule, consistent_history()[:2], TODAY))
        rows = consistent_history() + [observation(4, account="Other Expense")]
        self.assertIsNone(candidate_for_rule(self.rule, rows, TODAY))


class TestEligibleLearningLedger(unittest.TestCase):
    def ledger(self, **changes):
        return {"root_type": "Expense", "account_type": "Expense Account", "is_group": 0, "disabled": 0, **changes}

    def test_only_enabled_leaf_income_or_expense_accounts_qualify(self):
        for root_type in ("Income", "Expense"):
            with self.subTest(root_type=root_type):
                self.assertTrue(eligible_ledger(self.ledger(root_type=root_type)))
        for root_type in ("Asset", "Liability", "Equity", None):
            with self.subTest(root_type=root_type):
                self.assertFalse(eligible_ledger(self.ledger(root_type=root_type)))
        self.assertFalse(eligible_ledger(self.ledger(is_group=1)))
        self.assertFalse(eligible_ledger(self.ledger(disabled=1)))

    def test_cash_bank_party_and_tax_ledgers_are_always_excluded(self):
        for account_type in ("Cash", "Bank", "Receivable", "Payable", "Tax", "Chargeable"):
            with self.subTest(account_type=account_type):
                self.assertFalse(eligible_ledger(self.ledger(account_type=account_type)))


if __name__ == "__main__":
    unittest.main()
