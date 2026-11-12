"""Local, reproducible held-out evaluation of bank classification suggestions.

All models receive the same training records. Chronological results measure
later dates; merchant-disjoint results are a separate generalization diagnostic
and may train on dates later than some test rows. Neither score authorizes
bookings. Reports contain aggregates, never transaction descriptions or IDs.
"""

import hashlib
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from platform import python_implementation, python_version

from books_assistant.bank_learning import EXCLUDED_KEYWORDS
from books_assistant.matching import vendor_key

EVALUATION_VERSION = "bank-classifier-eval-v1"
MIN_TRAINING_EXAMPLES = 3
SCOPE_FIELDS = ("company", "currency", "bank_account", "direction")
REQUIRED_FIELDS = (*SCOPE_FIELDS, "bank_transaction", "journal", "account", "date", "description")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def prepare_observations(observations):
    """Dedupe joins and fail closed on conflicting versions of a transaction."""
    if not isinstance(observations, list):
        raise ValueError("observations must be a JSON list")
    stats = Counter(input_count=len(observations))
    by_transaction = defaultdict(dict)
    for source in observations:
        if not isinstance(source, dict):
            stats["excluded_invalid_count"] += 1
            continue
        if source.get("generated") or source.get("ineligible"):
            stats["excluded_generated_or_ineligible_count"] += 1
            continue
        if any(not isinstance(source.get(field), str) or not source[field].strip()
               for field in REQUIRED_FIELDS if field != "date"):
            stats["excluded_invalid_count"] += 1
            continue
        try:
            row = {field: source[field] for field in REQUIRED_FIELDS}
            row["date"] = _date(source["date"])
            amount = float(source.get("amount", 0))
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("amount must be positive and finite")
            row["amount"] = amount
        except (KeyError, TypeError, ValueError, OverflowError):
            stats["excluded_invalid_count"] += 1
            continue
        identity = _canonical(row)
        versions = by_transaction[row["bank_transaction"]]
        if identity in versions:
            stats["excluded_duplicate_count"] += 1
        else:
            versions[identity] = row
    rows = []
    for versions in by_transaction.values():
        if len(versions) > 1:
            stats["excluded_conflicting_version_count"] += len(versions)
        else:
            rows.extend(versions.values())
    rows.sort(key=lambda row: (row["date"], row["bank_transaction"], row["journal"]))
    stats["usable_count"] = len(rows)
    return rows, dict(stats)


def _split_result(name, train, test, **metadata):
    test_transactions = {row["bank_transaction"] for row in test}
    test_journals = {row["journal"] for row in test}
    purged = [row for row in train
              if row["bank_transaction"] in test_transactions or row["journal"] in test_journals]
    train = [row for row in train
             if row["bank_transaction"] not in test_transactions and row["journal"] not in test_journals]
    return {"name": name, "train": train, "test": test,
            "purged_source_overlap_count": len(purged),
            "source_overlap_count": 0, **metadata}


def chronological_split(rows, test_fraction=0.25):
    """Hold out complete latest dates, never individual rows from a shared date."""
    _validate_fraction(test_fraction)
    dates = sorted({row["date"] for row in rows})
    if len(dates) < 2:
        return _split_result("chronological", [], [], reason_code="AT_LEAST_TWO_DATES_REQUIRED")
    boundary = max(1, min(len(dates) - 1, math.floor(len(dates) * (1 - test_fraction))))
    cutoff = dates[boundary]
    return _split_result("chronological", [row for row in rows if row["date"] < cutoff],
                         [row for row in rows if row["date"] >= cutoff], cutoff_date=cutoff,
                         train_date_count=boundary, test_date_count=len(dates) - boundary)


def merchant_disjoint_split(rows, test_fraction=0.25):
    """Hold out normalized merchant groups with a deterministic SHA-256 ordering."""
    _validate_fraction(test_fraction)
    grouped = defaultdict(list)
    excluded = 0
    for row in rows:
        merchant = vendor_key(row["description"])
        if not merchant or merchant in EXCLUDED_KEYWORDS:
            excluded += 1
            continue
        grouped[merchant].append(row)
    merchants = sorted(grouped, key=lambda value: (_hash(value), value))
    if len(merchants) < 2:
        return _split_result("merchant_disjoint", [], [], excluded_no_merchant_count=excluded,
                             reason_code="AT_LEAST_TWO_MERCHANTS_REQUIRED")
    test_count = max(1, min(len(merchants) - 1, math.ceil(len(merchants) * test_fraction)))
    heldout = set(merchants[:test_count])
    train = [row for merchant in merchants if merchant not in heldout for row in grouped[merchant]]
    test = [row for merchant in merchants if merchant in heldout for row in grouped[merchant]]
    return _split_result("merchant_disjoint", train, test, test_merchant_count=test_count,
                         train_merchant_count=len(merchants) - test_count, merchant_overlap_count=0,
                         excluded_no_merchant_count=excluded,
                         temporal_order_enforced=False)


def _validate_fraction(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError("test_fraction must be between 0 and 1")


class MerchantHistoryBaseline:
    """Exact normalized merchant consensus, with the same independent-row floor."""

    model_version = "exact-merchant-consensus-v1"

    def __init__(self, observations):
        self.groups = defaultdict(list)
        self.dataset_hash = _hash(sorted(observations, key=_canonical))
        for row in observations:
            merchant = vendor_key(row["description"])
            if merchant and merchant not in EXCLUDED_KEYWORDS:
                self.groups[tuple(row[field] for field in SCOPE_FIELDS) + (merchant,)].append(row)

    def predict(self, transaction):
        merchant = vendor_key(transaction["description"])
        key = tuple(transaction[field] for field in SCOPE_FIELDS) + (merchant,)
        rows = self.groups.get(key, [])
        accounts = {row["account"] for row in rows}
        if (len(rows) >= MIN_TRAINING_EXAMPLES and len({row["date"] for row in rows}) >= 2
                and len(accounts) == 1):
            return {"status": "suggested", "account": next(iter(accounts))}
        return {"status": "abstain"}


def approximate_model_bytes(model):
    """Approximate reachable Python object bytes; excludes interpreter/native RSS."""
    seen = set()

    def size(value):
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        total = sys.getsizeof(value)
        if isinstance(value, dict):
            total += sum(size(key) + size(item) for key, item in value.items())
        elif isinstance(value, (list, tuple, set, frozenset)):
            total += sum(size(item) for item in value)
        elif hasattr(value, "__dict__") and not isinstance(value, type) and not callable(value):
            total += size(vars(value))
        return total

    return size(model)


def evaluate_model(factory, train, test):
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    model = factory(train)
    train_cpu = time.process_time() - cpu_start
    train_wall = time.perf_counter() - wall_start
    counts = Counter(test_count=len(test))
    reasons = Counter()
    model_version = getattr(model, "model_version", None)
    training_sources = {row["bank_transaction"] for row in train}
    latencies = []
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    for row in test:
        # Held-out labels and source IDs must never be available to the model.
        transaction = {field: row[field] for field in (*SCOPE_FIELDS, "description", "date", "amount")}
        start = time.perf_counter()
        prediction = model.predict(transaction)
        latencies.append((time.perf_counter() - start) * 1000)
        if prediction.get("model_version"):
            model_version = prediction["model_version"]
        if prediction.get("reason_code"):
            reasons[prediction["reason_code"]] += 1
        if any(source not in training_sources for source in prediction.get("evidence", [])):
            raise ValueError("prediction cites evidence outside the training set")
        if prediction.get("status") == "suggested":
            if not prediction.get("account"):
                raise ValueError("a suggested prediction must include an account")
            counts["suggested_count"] += 1
            counts["correct_count" if prediction["account"] == row["account"] else "wrong_count"] += 1
        elif prediction.get("status") == "abstain":
            counts["abstained_count"] += 1
        else:
            raise ValueError("unknown classifier prediction status")
    predict_cpu = time.process_time() - cpu_start
    predict_wall = time.perf_counter() - wall_start
    suggested, correct, total = counts["suggested_count"], counts["correct_count"], len(test)
    ordered = sorted(latencies)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2) if ordered else None
    return {
        **{key: counts[key] for key in ("test_count", "suggested_count", "abstained_count", "correct_count", "wrong_count")},
        "coverage": suggested / total if total else None,
        "precision_among_suggestions": correct / suggested if suggested else None,
        "overall_accuracy_including_abstentions": correct / total if total else None,
        "train_wall_seconds": train_wall, "train_cpu_seconds": train_cpu,
        "predict_wall_seconds": predict_wall, "predict_cpu_seconds": predict_cpu,
        "median_predict_ms": median,
        "p95_predict_ms": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None,
        "approximate_model_bytes": approximate_model_bytes(model),
        "decision_reason_counts": dict(sorted(reasons.items())),
        "model_version": model_version or "unspecified",
        "model_dataset_hash": getattr(model, "dataset_hash", None),
    }


def _class_counts(rows):
    return dict(sorted(Counter(row["account"] for row in rows).items()))


def benchmark(observations, *, test_fraction=0.25, source_label="unlabeled_local_data", classifier_factory=None, additional_factories=None):
    _validate_fraction(test_fraction)
    rows, preparation = prepare_observations(observations)
    report = {
        "evaluation_version": EVALUATION_VERSION, "source_label": source_label,
        "status": "no_data" if not rows else "ok", "dataset_hash": _hash(rows),
        "preparation": preparation, "class_counts": _class_counts(rows), "test_fraction": test_fraction,
        "accuracy_definition": "correct suggestions / all held-out rows; abstentions count as noncorrect",
        "eligibility_definition": "only valid labeled observations are scored; generated and ineligible rows are excluded",
        "footprint_definition": "approximate reachable Python object bytes; not process RSS",
        "runtime": {"implementation": python_implementation(), "python_version": python_version()},
        "splits": {},
    }
    if not rows:
        report["reason_code"] = "NO_USABLE_OBSERVATIONS"
        return report
    if classifier_factory is None:
        from books_assistant.bank_classifier import Classifier
        classifier_factory = Classifier
    for splitter in (chronological_split, merchant_disjoint_split):
        split = splitter(rows, test_fraction)
        train, test = split.pop("train"), split.pop("test")
        result = {**split, "train_count": len(train), "test_count": len(test),
                  "train_class_counts": _class_counts(train), "test_class_counts": _class_counts(test),
                  "split_hash": _hash({"train": train, "test": test})}
        groups = Counter(tuple(row[field] for field in SCOPE_FIELDS) + (row["account"],) for row in train)
        result["supported_training_class_count"] = sum(count >= MIN_TRAINING_EXAMPLES for count in groups.values())
        if not train or not test:
            result["status"] = "insufficient_data"
            result.setdefault("reason_code", "EMPTY_TRAIN_OR_TEST_AFTER_SOURCE_PURGE")
        else:
            result["status"] = "ok" if result["supported_training_class_count"] else "insufficient_data"
            if result["status"] == "insufficient_data":
                result["reason_code"] = "NO_CLASS_WITH_THREE_TRAINING_EXAMPLES"
            result["models"] = {
                "merchant_history_baseline": evaluate_model(MerchantHistoryBaseline, train, test),
                "classifier": evaluate_model(classifier_factory, train, test),
            }
            for name, factory in (additional_factories or {}).items():
                result["models"][name] = evaluate_model(factory, train, test)
        report["splits"][split["name"]] = result
    if all(split["status"] == "insufficient_data" for split in report["splits"].values()):
        report["status"] = "insufficient_data"
    return report


class HostedClassifier:
    """Explicit benchmark-only hosted calls using training examples and a shared quota."""

    model_version = "cloudflare-clef-flash-eval-v1"

    def __init__(self, observations, client, budget):
        self.observations, self.client, self.budget = observations, client, budget
        self.dataset_hash = _hash(observations)

    def predict(self, transaction):
        from books_assistant.bank_classifier import provider_context

        categories, examples = provider_context(transaction, self.observations)
        if not examples or not 1 <= len(categories) <= 32:
            return {"status": "abstain", "reason_code": "INSUFFICIENT_OR_EXCESS_CATEGORIES"}
        if self.budget["attempted"] >= self.budget["maximum"]:
            return {"status": "abstain", "reason_code": "EVALUATION_CALL_LIMIT"}
        self.budget["attempted"] += 1
        return self.client.classify(transaction, categories, examples)
