"""Standalone classifier behavior and resource bounds; no ERP or network needed."""

import unittest
from unittest.mock import patch

from books_assistant import bank_classifier as classifier


def row(number, description="GITHUB SUBSCRIPTION", account="Software Expense", **changes):
    return {"bank_transaction": f"BT-{number}", "journal": f"JE-{number}", "description": description,
            "date": "2026-11-01", "company": "508", "currency": "USD", "bank_account": "Operating",
            "direction": "Money out", "account": account, "amount": 20, **changes}


def history():
    return ([row(i) for i in range(3)]
            + [row(i, "MONTHLY BANK SERVICE FEE", "Bank Charges") for i in range(3, 6)])


def query(description="GITHUB SUBSCRIPTION", **changes):
    return {"description": description, "company": "508", "currency": "USD", "bank_account": "Operating",
            "direction": "Money out", **changes}


class TestBankClassifier(unittest.TestCase):
    def setUp(self):
        self.model = classifier.Classifier(history())

    def test_distinguishes_recurring_categories_and_returns_source_evidence(self):
        for description, account, expected in (("GITHUB SUBSCRIPTIONS", "Software Expense", {"BT-0", "BT-1", "BT-2"}),
                                                ("MONTHLY BANK SERVICE FEE", "Bank Charges", {"BT-3", "BT-4", "BT-5"})):
            with self.subTest(account=account):
                result = self.model.predict(query(description))
                self.assertEqual((result["status"], result["account"]), ("suggested", account))
                self.assertEqual(set(result["evidence"]), expected)
                self.assertGreaterEqual(result["similarity"], classifier.MIN_SIMILARITY)
                self.assertGreaterEqual(result["margin"], classifier.MIN_MARGIN)
                self.assertEqual(result["model_version"], classifier.MODEL_VERSION)
                self.assertEqual(result["dataset_version"], classifier.DATASET_VERSION)
                self.assertEqual(result["dataset_hash"], self.model.dataset_hash)
                self.assertNotIn("probability", result)

    def test_bank_feed_prefixes_and_reference_numbers_do_not_change_the_category(self):
        result = self.model.predict(query("DEBIT_CARD GITHUB SUBSCRIPTION 0123456"))
        self.assertEqual((result["status"], result["account"]), ("suggested", "Software Expense"))

    def test_non_latin_descriptions_can_supply_and_match_evidence(self):
        rows = [row(i, "カギソフトウェア 利用料") for i in range(3)]
        result = classifier.Classifier(rows).predict(query("カギソフトウェア 利用料"))
        self.assertEqual((result["status"], result["account"]), ("suggested", "Software Expense"))

    def test_never_borrows_another_company_currency_bank_or_direction(self):
        for field, different in (("company", "Other Co"), ("currency", "EUR"),
                                 ("bank_account", "Savings"), ("direction", "Money in")):
            with self.subTest(field=field):
                result = self.model.predict(query(**{field: different}))
                self.assertEqual((result["status"], result["reason_code"]), ("abstain", "NO_SCOPE_HISTORY"))
                self.assertNotIn("account", result)

    def test_partitioned_examples_cannot_be_pooled_to_reach_support_threshold(self):
        rows = [row(1), row(2), row(3, currency="EUR")]
        result = classifier.Classifier(rows).predict(query())
        self.assertEqual((result["status"], result["reason_code"]), ("abstain", "INSUFFICIENT_CLASS_EXAMPLES"))

    def test_can_derive_direction_from_an_unambiguous_bank_movement(self):
        transaction = query(deposit=0, withdrawal=20)
        transaction.pop("direction")
        self.assertEqual(self.model.predict(transaction)["status"], "suggested")
        for change in ({"deposit": 20}, {"withdrawal": -1}, {"withdrawal": float("nan")}, {"withdrawal": "bad"}):
            with self.subTest(change=change):
                self.assertEqual(self.model.predict({**transaction, **change})["reason_code"], "INVALID_SCOPE")
        self.assertEqual(self.model.predict(query(deposit=20, withdrawal=0))["reason_code"], "INVALID_SCOPE")

    def test_malformed_scope_or_text_abstains_without_account(self):
        for transaction, expected in ((None, "INVALID_SCOPE"), ({}, "INVALID_SCOPE"),
                                      (query(company=""), "INVALID_SCOPE"), (query(description=None), "EMPTY_DESCRIPTION"),
                                      (query(description=""), "EMPTY_DESCRIPTION"), (query(description="123456"), "EMPTY_DESCRIPTION"),
                                      (query(description="X" * (classifier.MAX_TEXT_CHARS + 1)), "TEXT_LIMIT")):
            with self.subTest(transaction=transaction):
                result = self.model.predict(transaction)
                self.assertEqual((result["status"], result["reason_code"]), ("abstain", expected))
                self.assertNotIn("account", result)

    def test_unknown_merchant_with_no_feature_support_abstains(self):
        result = self.model.predict(query("ZENORBIT ASTRONOMICAL OBSERVATORY"))
        self.assertEqual((result["status"], result["reason_code"]), ("abstain", "LOW_FEATURE_COVERAGE"))

    def test_generated_examples_do_not_train_or_create_ambiguity(self):
        rows = history() + [row(7, account="Other Expense", generated=True)]
        result = classifier.Classifier(rows).predict(query())
        self.assertEqual(result, self.model.predict(query()))
        sparse = [row(1), row(2), row(3, generated=True)]
        self.assertEqual(classifier.Classifier(sparse).predict(query())["reason_code"], "INSUFFICIENT_CLASS_EXAMPLES")

    def test_ineligible_source_blocks_false_unanimity_for_its_merchant(self):
        marker = row(7, ineligible=True)
        marker.pop("account")
        result = classifier.Classifier(history() + [marker]).predict(query())
        self.assertEqual((result["status"], result["reason_code"]), ("abstain", "AMBIGUOUS_HISTORY"))
        marker["bank_account"] = "Savings"
        self.assertEqual(classifier.Classifier(history() + [marker]).predict(query())["status"], "suggested")

    def test_a_minority_conflicting_account_blocks_the_merchant(self):
        rows = history() + [row(7, account="Office Expense")]
        result = classifier.Classifier(rows).predict(query())
        self.assertEqual((result["status"], result["reason_code"]), ("abstain", "AMBIGUOUS_HISTORY"))

    def test_duplicate_joins_never_add_independent_source_support(self):
        result = classifier.Classifier([row(1)] * 10).predict(query())
        self.assertEqual(result["reason_code"], "INSUFFICIENT_CLASS_EXAMPLES")
        repeated = classifier.Classifier(history() * 5)
        self.assertEqual(repeated.predict(query()), self.model.predict(query()))

    def test_reused_bank_transaction_or_journal_is_not_independent_evidence(self):
        for changes in ({"bank_transaction": "BT-0"}, {"journal": "JE-0"}):
            with self.subTest(changes=changes):
                rows = history() + [row(7, **changes)]
                result = classifier.Classifier(rows).predict(query())
                self.assertEqual(result["reason_code"], "AMBIGUOUS_HISTORY")

    def test_underrepresented_competing_account_is_not_silently_discarded(self):
        model = classifier.Classifier([row(i) for i in range(3)] + [row(3, "MONTHLY SERVICE FEE", "Bank Charges")])
        result = model.predict(query("MONTHLY SERVICE FEE"))
        self.assertEqual(result["reason_code"], "INSUFFICIENT_CLASS_EXAMPLES")
        self.assertNotIn("account", result)

    def test_exact_centroid_tie_abstains_deterministically(self):
        # The payment prefix is deliberately absent from feature vectors; the
        # distinct merchant keys avoid turning this into an exact-history veto.
        rows = ([row(i, "GITHUB", "Zulu Expense") for i in range(3)]
                + [row(i, "PAYMENT GITHUB", "Alpha Expense") for i in range(3, 6)])
        result = classifier.Classifier(rows).predict(query("GITHUB"))
        self.assertEqual((result["status"], result["reason_code"], result["margin"]), ("abstain", "AMBIGUOUS_CLASS", 0.0))
        self.assertNotIn("account", result)
        self.assertEqual(classifier.Classifier(list(reversed(rows))).predict(query("GITHUB")), result)

    def test_fingerprint_and_predictions_are_stable_across_input_order(self):
        reversed_model = classifier.Classifier(list(reversed(history())))
        self.assertEqual(reversed_model.dataset_hash, self.model.dataset_hash)
        self.assertEqual(reversed_model.predict(query()), self.model.predict(query()))
        changed = history()
        changed[0] = {**changed[0], "journal_modified": "2026-11-02 12:00:00"}
        self.assertNotEqual(classifier.Classifier(changed).dataset_hash, self.model.dataset_hash)

    def test_prediction_does_not_train_on_the_query_label_or_identity(self):
        transaction = query(account="Incorrect Query Label", bank_transaction="BT-HELDOUT", journal="JE-HELDOUT")
        result = self.model.predict(transaction)
        self.assertEqual(result, self.model.predict(query()))
        self.assertNotIn("BT-HELDOUT", result["evidence"])

    def test_input_and_partition_capacity_limits_abstain_instead_of_sampling(self):
        for name, limit in (("MAX_INPUT_ROWS", 3), ("MAX_TRAINING_DOCS", 3), ("MAX_DOCS_PER_SCOPE", 3)):
            with self.subTest(name=name), patch.object(classifier, name, limit):
                result = classifier.Classifier(history()).predict(query())
                self.assertEqual((result["status"], result["reason_code"]), ("abstain", "DATASET_LIMIT"))

    def test_scope_capacity_limit_never_uses_a_partial_scope_collection(self):
        rows = history() + [row(i, bank_account="Savings") for i in range(6, 9)]
        with patch.object(classifier, "MAX_SCOPES", 1):
            model = classifier.Classifier(rows)
            self.assertEqual(model.predict(query())["reason_code"], "DATASET_LIMIT")
            self.assertEqual(model.predict(query(bank_account="Savings"))["reason_code"], "DATASET_LIMIT")

    def test_text_and_vocabulary_work_are_bounded(self):
        oversized = row(10, "GITHUB " + "A" * classifier.MAX_TEXT_CHARS)
        self.assertEqual(classifier.Classifier(history() + [oversized]).predict(query())["reason_code"], "AMBIGUOUS_HISTORY")
        with patch.object(classifier, "MAX_FEATURES", 20):
            model = classifier.Classifier(history())
            self.assertTrue(all(len(partition["idf"]) <= 20 for partition in model._models.values()))
        self.assertLessEqual(len(classifier._features(" ".join(["abcdefghijk"] * 64))), classifier.MAX_FEATURES_PER_DOCUMENT)

    def test_empty_or_malformed_training_rows_do_not_create_a_model(self):
        for data in (None, [], [None, "bad", {}], [row(1, account=None)]):
            with self.subTest(data=data):
                self.assertEqual(classifier.Classifier(data).predict(query())["status"], "abstain")


if __name__ == "__main__":
    unittest.main()
