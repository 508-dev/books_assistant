"""Bounded CPU-only TF-IDF suggestions for bank classifications.

Cosine similarity and class margins are retrieval scores, not probabilities.
This module proposes account labels; callers own permissions, live accounting
checks and every decision to create a financial document. No model is persisted.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
import hashlib
from itertools import islice
import json
import math
import re

from books_assistant.matching import searchable, vendor_key

MODEL_VERSION = "tfidf-nearest-centroid-v1"
DATASET_VERSION = 1
MAX_INPUT_ROWS = 10_000
MAX_TRAINING_DOCS = 2_000
MAX_DOCS_PER_SCOPE = 500
MAX_SCOPES = 32
MAX_TEXT_CHARS = 512
MAX_TOKENS = 64
MAX_FEATURES_PER_DOCUMENT = 1_000
MAX_FEATURES = 8_000
MIN_CLASS_EXAMPLES = 3
MIN_SIMILARITY = 0.72
MIN_MARGIN = 0.12
MIN_NEIGHBOR_SIMILARITY = 0.65
MIN_NEIGHBORS = 2
MIN_FEATURE_COVERAGE = 0.55
MAX_EVIDENCE = 3
SCOPE_FIELDS = ("company", "currency", "bank_account", "direction")
_STOP_WORDS = {"ach", "pos", "debit", "credit", "card", "payment", "payments", "online", "reference", "ref",
               "inc", "llc", "com", "www", "http", "https"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _scope(row):
    if not isinstance(row, Mapping):
        return None
    values = [row.get(field) for field in SCOPE_FIELDS]
    if values[-1] is None or "deposit" in row or "withdrawal" in row:
        try:
            deposit, withdrawal = float(row.get("deposit") or 0), float(row.get("withdrawal") or 0)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(deposit) or not math.isfinite(withdrawal) or deposit < 0 or withdrawal < 0:
            return None
        if (deposit > 0) == (withdrawal > 0):
            return None
        direction = "Money in" if deposit else "Money out"
        if values[-1] is not None and values[-1] != direction:
            return None
        values[-1] = direction
    if any(not isinstance(value, str) or not value.strip() or len(value) > 140 for value in values):
        return None
    if values[-1] not in {"Money in", "Money out"}:
        return None
    return tuple(value.strip() for value in values)


def merchant_key(description):
    key = vendor_key(description)
    if key:
        return key
    # Preserve non-Latin merchant identifiers without inventing transliterations.
    words = re.findall(r"[^\W\d_]+", searchable(description), flags=re.UNICODE)
    return words[0] if words and len(words[0]) >= 3 else None


def _text(description):
    tokens = re.findall(r"[^\W_]+", searchable(description).casefold(), flags=re.UNICODE)
    return " ".join(token[:40] for token in tokens if len(token) >= 2
                    and not any(character.isdigit() for character in token) and token not in _STOP_WORDS)[:MAX_TEXT_CHARS]


def _features(text):
    words = text.split()[:MAX_TOKENS]
    counts = Counter("w:" + word for word in words)
    counts.update("b:" + left + " " + right for left, right in zip(words, words[1:]))
    characters = Counter()
    for word in words:
        padded = "^" + word + "$"
        for size in (3, 4, 5):
            characters.update(f"c{size}:" + padded[index:index + size] for index in range(len(padded) - size + 1))
    # Keep whole-word features first, then a deterministic bounded char vocabulary.
    available = MAX_FEATURES_PER_DOCUMENT - len(counts)
    counts.update(dict(sorted(characters.items())[:max(0, available)]))
    return counts


def _weight(feature):
    return 2.0 if feature.startswith("w:") else 1.5 if feature.startswith("b:") else 0.6


def _unit(vector):
    norm = math.sqrt(sum(value * value for value in vector.values()))
    return {feature: value / norm for feature, value in vector.items()} if norm else {}


def _vector(counts, idf):
    return _unit({feature: (1.0 + math.log(count)) * idf[feature] * _weight(feature)
                  for feature, count in sorted(counts.items()) if feature in idf})


def _dot(left, right):
    return min(1.0, max(0.0, sum(left[feature] * right.get(feature, 0.0) for feature in sorted(left))))


class Classifier:
    """Train per-scope nearest centroids from validated historical observations.

    Ineligible markers veto their merchant, generated rows are excluded, and
    conflicting/reused source identities cannot create independent evidence.
    Input overflow rejects the model rather than sampling away contradictions.
    """

    def __init__(self, observations):
        self._models = {}
        self._blocked = set()
        self._limited_scopes = set()
        self._input_limited = False
        try:
            raw = list(islice(iter(observations), MAX_INPUT_ROWS + 1))
        except TypeError:
            raw = []
        if len(raw) > MAX_INPUT_ROWS:
            self._input_limited = True
            self.dataset_hash = _hash({"version": DATASET_VERSION, "rejected": "input_limit", "cap": MAX_INPUT_ROWS})
            return
        rows = []
        for row in raw:
            scope = _scope(row)
            if scope is None or row.get("generated"):
                continue
            description = row.get("description")
            if not isinstance(description, str):
                continue
            merchant = merchant_key(description[:MAX_TEXT_CHARS])
            key = (scope, merchant)
            if row.get("ineligible") or len(description) > MAX_TEXT_CHARS:
                self._blocked.add(key)
                continue
            account, source, journal = (row.get(field) for field in ("account", "bank_transaction", "journal"))
            if any(not isinstance(value, str) or not value.strip() or len(value) > 140 for value in (account, source, journal)):
                self._blocked.add(key)
                continue
            text = _text(description)
            if not text or merchant is None:
                continue
            item = {"scope": scope, "merchant": merchant, "account": account, "source": source, "journal": journal,
                    "text": text, "description": description, "date": str(row.get("date") or "")[:40],
                    "amount": str(row.get("amount") or "")[:64],
                    "bank_modified": str(row.get("bank_modified") or "")[:40],
                    "journal_modified": str(row.get("journal_modified") or "")[:40]}
            rows.append(item)
        # Exact repeated joins collapse. A source with disagreeing variants is not
        # valid training evidence, regardless of which variant arrived first.
        unique = {_hash(row): row for row in rows}
        rows = [unique[key] for key in sorted(unique)]
        by_source, by_journal = defaultdict(list), defaultdict(list)
        for row in rows:
            by_source[row["source"]].append(row)
            by_journal[row["journal"]].append(row)
        for variants in (*by_source.values(), *by_journal.values()):
            if len(variants) > 1:
                self._blocked.update((row["scope"], row["merchant"]) for row in variants)
        labels = defaultdict(set)
        for row in rows:
            labels[(row["scope"], row["merchant"])].add(row["account"])
        self._blocked.update(key for key, accounts in labels.items() if len(accounts) > 1)
        retained = [row for row in rows if (row["scope"], row["merchant"]) not in self._blocked]
        partitions = defaultdict(list)
        for row in retained:
            partitions[row["scope"]].append(row)
        if len(partitions) > MAX_SCOPES or len(retained) > MAX_TRAINING_DOCS:
            self._limited_scopes.update(partitions)
        self._limited_scopes.update(scope for scope, partition in partitions.items() if len(partition) > MAX_DOCS_PER_SCOPE)
        self.dataset_hash = _hash({"version": DATASET_VERSION, "rows": rows,
                                   "blocked": sorted(self._blocked), "limited_scopes": sorted(self._limited_scopes)})
        for scope, partition in sorted(partitions.items()):
            if scope in self._limited_scopes:
                continue
            self._models[scope] = self._fit(partition)

    @staticmethod
    def _fit(rows):
        counts = [_features(row["text"]) for row in rows]
        frequencies = Counter()
        for terms in counts:
            frequencies.update(terms.keys())
        vocabulary = sorted(frequencies, key=lambda feature: (-frequencies[feature], feature))[:MAX_FEATURES]
        idf = {feature: math.log((1.0 + len(rows)) / (1.0 + frequencies[feature])) + 1.0 for feature in vocabulary}
        centroids, examples = defaultdict(lambda: defaultdict(float)), defaultdict(list)
        for row, terms in zip(rows, counts):
            vector = _vector(terms, idf)
            if not vector:
                continue
            examples[row["account"]].append((row["source"], vector))
            for feature, value in vector.items():
                centroids[row["account"]][feature] += value
        return {"idf": idf, "centroids": {account: _unit(dict(sorted(values.items()))) for account, values in centroids.items()},
                "examples": dict(examples), "dataset_hash": _hash({"version": DATASET_VERSION, "rows": rows})}

    def predict(self, transaction):
        """Return an explained suggestion or a conservative reason for abstention."""
        scope = _scope(transaction)
        result = {"status": "abstain", "similarity": 0.0, "margin": 0.0, "evidence": [],
                  "model_version": MODEL_VERSION, "dataset_version": DATASET_VERSION, "dataset_hash": self.dataset_hash}
        if scope is None:
            return {**result, "reason_code": "INVALID_SCOPE"}
        if self._input_limited or scope in self._limited_scopes:
            return {**result, "reason_code": "DATASET_LIMIT"}
        description = transaction.get("description")
        if not isinstance(description, str) or not description.strip():
            return {**result, "reason_code": "EMPTY_DESCRIPTION"}
        if len(description) > MAX_TEXT_CHARS:
            return {**result, "reason_code": "TEXT_LIMIT"}
        if (scope, merchant_key(description)) in self._blocked:
            return {**result, "reason_code": "AMBIGUOUS_HISTORY"}
        model = self._models.get(scope)
        if not model:
            return {**result, "reason_code": "NO_SCOPE_HISTORY"}
        result["scope_dataset_hash"] = model["dataset_hash"]
        terms = _features(_text(description))
        if not terms:
            return {**result, "reason_code": "EMPTY_DESCRIPTION"}
        known = sum(1 for feature in terms if feature in model["idf"])
        if known / len(terms) < MIN_FEATURE_COVERAGE:
            return {**result, "reason_code": "LOW_FEATURE_COVERAGE"}
        vector = _vector(terms, model["idf"])
        scores = sorted(((_dot(vector, centroid), account) for account, centroid in model["centroids"].items()),
                        key=lambda item: (-item[0], item[1]))
        if not scores:
            return {**result, "reason_code": "NO_SCOPE_HISTORY"}
        similarity, account = scores[0]
        margin = similarity - (scores[1][0] if len(scores) > 1 else 0.0)
        result.update(similarity=round(similarity, 6), margin=round(margin, 6))
        neighbors = sorted(((_dot(vector, example), source) for source, example in model["examples"][account]),
                           key=lambda item: (-item[0], item[1]))
        evidence = [source for score, source in neighbors if score >= MIN_NEIGHBOR_SIMILARITY]
        result["evidence"] = evidence[:MAX_EVIDENCE]
        if len(model["examples"][account]) < MIN_CLASS_EXAMPLES:
            return {**result, "reason_code": "INSUFFICIENT_CLASS_EXAMPLES"}
        if similarity < MIN_SIMILARITY:
            return {**result, "reason_code": "LOW_SIMILARITY"}
        if margin < MIN_MARGIN:
            return {**result, "reason_code": "AMBIGUOUS_CLASS"}
        if len(evidence) < MIN_NEIGHBORS:
            return {**result, "reason_code": "INSUFFICIENT_NEIGHBORS"}
        return {**result, "status": "suggested", "account": account, "reason_code": "CENTROID_MATCH"}


def provider_context(transaction, observations, categories=None):
    """Project only same-scope historical examples into a bounded hosted request."""
    scope = _scope(transaction)
    valid = [r for r in observations if scope is not None and _scope(r) == scope
             and not r.get("generated") and not r.get("ineligible") and r.get("account")]
    # Collapse identical joins and reject reused identities instead of selecting
    # whichever contradictory label happened to arrive first.
    unique = {_hash({key: str(row.get(key, "")) for key in (
        "bank_transaction", "journal", "account", "description", "date", "amount")}): row for row in valid}
    valid = [unique[key] for key in sorted(unique)]
    sources = Counter(r.get("bank_transaction") for r in valid)
    journals = Counter(r.get("journal") for r in valid)
    valid = [r for r in valid if sources[r.get("bank_transaction")] == 1 and journals[r.get("journal")] == 1]
    if categories is None:
        categories = [{"account": account, "name": account} for account in sorted({r["account"] for r in valid})]
    allowed = {category["account"] for category in categories}
    words = set(_text(transaction.get("description", "")).split())
    ranked = []
    for row in valid:
        source, journal = row.get("bank_transaction"), row.get("journal")
        if row["account"] not in allowed or not source or not journal:
            continue
        overlap = len(words & set(_text(row["description"]).split()))
        ranked.append((overlap, source, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    examples = [{key: row[key] for key in ("description", "amount", "direction", "currency", "account")}
                for _score, _source, row in ranked[:12]]
    return categories, examples
