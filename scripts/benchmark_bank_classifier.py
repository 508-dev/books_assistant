#!/usr/bin/env python3
"""Benchmark local observation JSON; this script does not connect to ERP. Hosted calls require --cloudflare.

Input is a JSON list, or {"observations": [...], "label": "synthetic"}.
Each row needs bank_transaction, journal, description, date, company, currency,
bank_account, direction, account, and a positive amount. Output is aggregate JSON.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from books_assistant.bank_classifier_eval import HostedClassifier, benchmark


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path, nargs="?", help="local observation JSON file")
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--synthetic", action="store_true", help="label supplied observations as synthetic")
    parser.add_argument("--cloudflare", action="store_true", help="explicitly send held-out transactions and training examples to Cloudflare")
    parser.add_argument("--max-cloudflare-calls", type=int, default=20, help="maximum attempts across both splits (1–100)")
    args = parser.parse_args(argv)
    if args.observations is None:
        print(json.dumps({"status": "no_data", "reason_code": "NO_LOCAL_OBSERVATION_FILE"}, indent=2))
        return 0
    try:
        if not 1 <= args.max_cloudflare_calls <= 100:
            raise ValueError("invalid request limit")
        factories = {}
        budget = {"maximum": args.max_cloudflare_calls, "attempted": 0}
        if args.cloudflare:
            from books_assistant.cloudflare_decision import CloudflareDecisionClient
            client = CloudflareDecisionClient()
            factories["cloudflare"] = lambda train: HostedClassifier(train, client, budget)
        document = json.loads(args.observations.read_text())
        label = "synthetic" if args.synthetic else "unlabeled_local_data"
        if isinstance(document, dict):
            if document.get("label") == "synthetic":
                label = "synthetic"
            document = document.get("observations")
        report = benchmark(document, test_fraction=args.test_fraction, source_label=label, additional_factories=factories)
        report["hosted_evaluation"] = {"enabled": args.cloudflare, **budget}
    except (OSError, ValueError, TypeError, KeyError):
        # Do not echo financial input records or filesystem paths in error reports.
        print(json.dumps({"status": "invalid_data", "reason_code": "INVALID_LOCAL_INPUT_OR_OPTIONS"}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
