"""Evaluation tests use synthetic observations and require no ERP site."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from books_assistant.bank_classifier_eval import (
    MerchantHistoryBaseline, benchmark, chronological_split, evaluate_model,
    merchant_disjoint_split, prepare_observations,
)
from books_assistant.matching import vendor_key


def observation(number, **changes):
    return {"bank_transaction": f"BT-{number}", "journal": f"JE-{number}",
            "description": "GITHUB INC SUBSCRIPTION", "date": f"2026-10-{number:02d}",
            "company": "Synthetic Co-op", "currency": "USD", "bank_account": "Synthetic Bank",
            "direction": "Money out", "account": "Software Expense", "amount": 20, **changes}


class AbstainingClassifier:
    model_version = "test-abstain-v1"

    def __init__(self, observations):
        self.observations = observations

    def predict(self, transaction):
        return {"status": "abstain"}


class TestObservationPreparation(unittest.TestCase):
    def test_deduplicates_joins_but_removes_all_conflicting_versions(self):
        rows = [observation(1), observation(1), observation(2), observation(2, account="Office Expense")]
        prepared, counts = prepare_observations(rows)
        self.assertEqual([row["bank_transaction"] for row in prepared], ["BT-1"])
        self.assertEqual(counts["excluded_duplicate_count"], 1)
        self.assertEqual(counts["excluded_conflicting_version_count"], 2)

    def test_generated_ineligible_and_invalid_rows_do_not_enter_the_dataset(self):
        rows = [observation(1), observation(2, generated=True), observation(3, ineligible=True),
                observation(4, journal=""), observation(5, amount=float("nan")),
                observation(6, date="invalid-date"), None]
        prepared, counts = prepare_observations(rows)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(counts["excluded_generated_or_ineligible_count"], 2)
        self.assertEqual(counts["excluded_invalid_count"], 4)

    def test_both_source_ids_are_required_to_guard_split_leakage(self):
        for field in ("bank_transaction", "journal"):
            with self.subTest(field=field):
                self.assertEqual(prepare_observations([observation(1, **{field: None})])[0], [])

    def test_preparation_is_independent_of_input_order(self):
        rows = [observation(3), observation(1), observation(2)]
        self.assertEqual(prepare_observations(rows)[0], prepare_observations(list(reversed(rows)))[0])


class TestEvaluationSplits(unittest.TestCase):
    def test_chronological_holdout_contains_whole_dates(self):
        rows = [observation(1), observation(2, date="2026-10-01"),
                observation(3, date="2026-10-02"), observation(4, date="2026-10-02"),
                observation(5, date="2026-10-03"), observation(6, date="2026-10-03")]
        split = chronological_split(rows, 0.34)
        self.assertEqual({row["date"] for row in split["train"]}, {"2026-10-01"})
        self.assertEqual({row["date"] for row in split["test"]}, {"2026-10-02", "2026-10-03"})
        self.assertLess(max(row["date"] for row in split["train"]), min(row["date"] for row in split["test"]))

    def test_chronological_purge_prevents_journal_and_transaction_leakage(self):
        rows = [observation(1, journal="shared-journal"), observation(2),
                observation(1, date="2026-10-03"), observation(4, journal="shared-journal")]
        split = chronological_split(rows, 0.5)
        self.assertEqual(split["purged_source_overlap_count"], 1)
        self.assertEqual([row["bank_transaction"] for row in split["train"]], ["BT-2"])
        train_bt = {row["bank_transaction"] for row in split["train"]}
        test_bt = {row["bank_transaction"] for row in split["test"]}
        train_je = {row["journal"] for row in split["train"]}
        test_je = {row["journal"] for row in split["test"]}
        self.assertFalse(train_bt & test_bt)
        self.assertFalse(train_je & test_je)

    def test_same_date_dataset_explicitly_has_insufficient_temporal_evidence(self):
        split = chronological_split([observation(i, date="2026-10-01") for i in range(1, 5)])
        self.assertEqual(split["reason_code"], "AT_LEAST_TWO_DATES_REQUIRED")
        self.assertFalse(split["train"])
        self.assertFalse(split["test"])

    def test_merchant_groups_cannot_leak_through_description_variants_or_scopes(self):
        rows = [observation(1), observation(2, description="DEBIT_CARD GITHUB.COM CA"),
                observation(3, description="GITHUB MEMBERSHIP", company="Other Synthetic Company"),
                observation(4, description="KAGI SEARCH"), observation(5, description="KAGI SEARCH YEARLY"),
                observation(6, description="ELEST.IO HOSTING")]
        split = merchant_disjoint_split(rows)
        train = {vendor_key(row["description"]) for row in split["train"]}
        test = {vendor_key(row["description"]) for row in split["test"]}
        self.assertFalse(train & test)
        self.assertTrue(train)
        self.assertTrue(test)
        github_rows = [row for row in split["train"] + split["test"] if vendor_key(row["description"]) == "GITHUB"]
        self.assertEqual(len(github_rows), 3)
        self.assertEqual(split["merchant_overlap_count"], 0)
        reverse = merchant_disjoint_split(list(reversed(rows)))
        self.assertEqual({row["bank_transaction"] for row in split["test"]},
                         {row["bank_transaction"] for row in reverse["test"]})

    def test_merchant_split_also_purges_source_journals(self):
        rows = [observation(1, journal="shared", description="GITHUB SUBSCRIPTION"),
                observation(2, journal="shared", description="KAGI SUBSCRIPTION")]
        split = merchant_disjoint_split(rows, 0.5)
        self.assertEqual(split["purged_source_overlap_count"], 1)
        self.assertEqual(split["train"], [])

    def test_provider_only_rows_do_not_become_independent_merchants(self):
        rows = [observation(1, description="BILL.COM"), observation(2)]
        split = merchant_disjoint_split(rows)
        self.assertEqual(split["excluded_no_merchant_count"], 1)
        self.assertEqual(split["reason_code"], "AT_LEAST_TWO_MERCHANTS_REQUIRED")

    def test_invalid_split_fractions_fail_explicitly(self):
        for fraction in (0, 1, -0.1, float("nan"), float("inf")):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                chronological_split([], fraction)


class TestEvaluationMetrics(unittest.TestCase):
    def test_precision_coverage_wrong_count_and_abstention_are_distinct(self):
        class Model:
            def __init__(self, train):
                pass

            def predict(self, transaction):
                self.assert_no_label(transaction)
                if transaction["description"] == "ABSTAIN":
                    return {"status": "abstain"}
                return {"status": "suggested", "account": "Software Expense"}

            @staticmethod
            def assert_no_label(transaction):
                if {"account", "bank_transaction", "journal"} & transaction.keys():
                    raise AssertionError("test labels or source IDs leaked into prediction")

        test = [observation(4, description="CORRECT"), observation(5, description="WRONG", account="Office Expense"),
                observation(6, description="ABSTAIN")]
        result = evaluate_model(Model, [observation(1)], test)
        self.assertEqual((result["correct_count"], result["wrong_count"], result["abstained_count"]), (1, 1, 1))
        self.assertEqual(result["coverage"], 2 / 3)
        self.assertEqual(result["precision_among_suggestions"], 0.5)
        self.assertEqual(result["overall_accuracy_including_abstentions"], 1 / 3)
        self.assertGreater(result["approximate_model_bytes"], 0)
        for field in ("train_cpu_seconds", "train_wall_seconds", "predict_cpu_seconds", "predict_wall_seconds", "p95_predict_ms"):
            self.assertGreaterEqual(result[field], 0)

    def test_precision_without_suggestions_is_unknown_not_one_hundred_percent(self):
        result = evaluate_model(AbstainingClassifier, [observation(1)], [observation(4)])
        self.assertIsNone(result["precision_among_suggestions"])
        self.assertEqual(result["coverage"], 0)
        self.assertEqual(result["overall_accuracy_including_abstentions"], 0)

    def test_evidence_outside_training_is_detected_and_prediction_metadata_is_recorded(self):
        class Model:
            def __init__(self, rows):
                pass

            def predict(self, transaction):
                return {"status": "abstain", "evidence": ["BT-1"], "reason_code": "CAUTIOUS",
                        "model_version": "from-prediction"}

        result = evaluate_model(Model, [observation(1)], [observation(4)])
        self.assertEqual(result["model_version"], "from-prediction")
        self.assertEqual(result["decision_reason_counts"], {"CAUTIOUS": 1})
        with self.assertRaises(ValueError):
            evaluate_model(Model, [observation(2)], [observation(4)])

    def test_merchant_baseline_requires_consensus_and_independent_dates(self):
        rows = [observation(1), observation(2), observation(3)]
        query = observation(4)
        self.assertEqual(MerchantHistoryBaseline(rows).predict(query), {"status": "suggested", "account": "Software Expense"})
        self.assertEqual(MerchantHistoryBaseline(rows[:2]).predict(query)["status"], "abstain")
        self.assertEqual(MerchantHistoryBaseline(rows + [observation(5, account="Other")]).predict(query)["status"], "abstain")
        same_day = [observation(number, date="2026-10-01") for number in range(1, 4)]
        self.assertEqual(MerchantHistoryBaseline(same_day).predict(query)["status"], "abstain")
        self.assertEqual(MerchantHistoryBaseline(rows).predict(observation(4, bank_account="Other Bank"))["status"], "abstain")

    def test_report_has_reproducible_dataset_and_split_hashes(self):
        rows = [observation(i) for i in range(1, 6)] + [observation(6, description="KAGI SUBSCRIPTION")]
        first = benchmark(rows, classifier_factory=AbstainingClassifier, source_label="synthetic")
        second = benchmark(list(reversed(rows)), classifier_factory=AbstainingClassifier, source_label="synthetic")
        self.assertEqual(first["source_label"], "synthetic")
        self.assertEqual(first["dataset_hash"], second["dataset_hash"])
        for name in first["splits"]:
            self.assertEqual(first["splits"][name]["split_hash"], second["splits"][name]["split_hash"])
        serialized = json.dumps(first)
        self.assertNotIn("GITHUB INC SUBSCRIPTION", serialized)
        self.assertNotIn('"BT-1"', serialized)
        self.assertNotIn('"JE-1"', serialized)

    def test_factory_only_receives_training_rows(self):
        received = []

        def factory(train):
            received.append([row["bank_transaction"] for row in train])
            return AbstainingClassifier(train)

        rows = [observation(i) for i in range(1, 6)]
        report = benchmark(rows, classifier_factory=factory)
        self.assertEqual(received, [["BT-1", "BT-2", "BT-3"]])
        self.assertEqual(report["splits"]["chronological"]["test_count"], 2)

    def test_empty_and_insufficient_datasets_are_reported_explicitly(self):
        empty = benchmark([], classifier_factory=AbstainingClassifier)
        self.assertEqual(empty["status"], "no_data")
        self.assertEqual(empty["reason_code"], "NO_USABLE_OBSERVATIONS")
        small = benchmark([observation(1), observation(2)], classifier_factory=AbstainingClassifier)
        self.assertEqual(small["status"], "insufficient_data")
        self.assertEqual(small["splits"]["chronological"]["reason_code"], "NO_CLASS_WITH_THREE_TRAINING_EXAMPLES")


class TestBenchmarkCommand(unittest.TestCase):
    def command(self, *args):
        script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_bank_classifier.py"
        completed = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)
        return completed.returncode, json.loads(completed.stdout)

    def test_no_input_reports_no_data(self):
        code, report = self.command()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "no_data")

    def test_synthetic_fixture_runs_the_real_classifier_without_source_overlap(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "bank_classifier_synthetic.json"
        code, report = self.command(str(fixture))
        self.assertEqual(code, 0)
        self.assertEqual(report["source_label"], "synthetic")
        self.assertEqual(report["preparation"]["usable_count"], 16)
        for split in report["splits"].values():
            self.assertEqual(split["source_overlap_count"], 0)
            self.assertEqual(split["test_count"], 4)
            self.assertEqual(split["models"]["classifier"]["model_version"], "tfidf-nearest-centroid-v1")
            self.assertEqual(split["models"]["classifier"]["test_count"], 4)
            self.assertIn("merchant_history_baseline", split["models"])

    def test_synthetic_dataset_is_labeled_and_invalid_json_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.json"
            path.write_text(json.dumps({"label": "synthetic", "observations": []}))
            code, report = self.command(str(path))
            self.assertEqual(code, 0)
            self.assertEqual(report["source_label"], "synthetic")
            path.write_text("sensitive transaction content is invalid JSON")
            code, report = self.command(str(path))
            self.assertEqual(code, 2)
            self.assertEqual(report, {"status": "invalid_data", "reason_code": "INVALID_LOCAL_INPUT_OR_OPTIONS"})


class TestHostedEvaluation(unittest.TestCase):
    def test_paid_calls_are_explicitly_capped_across_splits(self):
        from unittest.mock import Mock
        from books_assistant.bank_classifier_eval import HostedClassifier
        rows = [observation(i, description="GITHUB SOFTWARE" if i % 2 else "KAGI SOFTWARE") for i in range(1, 17)]
        budget = {"maximum": 2, "attempted": 0}
        client = Mock()
        client.classify.return_value = {"status": "suggested", "account": "Software Expense"}
        report = benchmark(rows, additional_factories={"cloudflare": lambda train: HostedClassifier(train, client, budget)})
        self.assertEqual(client.classify.call_count, 2)
        self.assertEqual(budget["attempted"], 2)
        self.assertIn("cloudflare", report["splits"]["chronological"]["models"])
        for call in client.classify.call_args_list:
            transaction, categories, examples = call.args
            self.assertNotIn("account", transaction)
            self.assertNotIn("bank_transaction", transaction)
            self.assertTrue(examples)
            self.assertTrue(all("bank_transaction" not in item and "journal" not in item for item in examples))

    def test_hosted_context_does_not_select_arbitrary_reused_journal_evidence(self):
        from books_assistant.bank_classifier import provider_context
        rows = [observation(1, journal="REUSED"), observation(2, journal="REUSED", account="Other expense")]
        categories, examples = provider_context(observation(3), rows)
        self.assertEqual(categories, [])
        self.assertEqual(examples, [])

    def test_default_cli_has_no_hosted_calls_and_rejects_invalid_call_cap(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "bank_classifier_synthetic.json"
        script = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_bank_classifier.py"
        result = subprocess.run([sys.executable, str(script), str(fixture)], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["hosted_evaluation"]["enabled"], False)
        result = subprocess.run([sys.executable, str(script), str(fixture), "--max-cloudflare-calls", "0"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid_data")


if __name__ == "__main__":
    unittest.main()
