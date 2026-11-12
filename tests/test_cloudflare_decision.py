"""Offline provider contract tests; never contact Cloudflare or use real tokens."""

import io
import json
import math
import os
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from books_assistant import cloudflare_decision as provider


def answer(choice="c0", probabilities=None, **changes):
    result = {
        "model": "clef-flash",
        "answers": {"account": {"type": "choice", "choice": choice,
                                 "probabilities": probabilities or {"c0": 0.95, "c1": 0.02, "needs_review": 0.03},
                                 "confidence": 0.95}},
        "usage": {"input_tokens": 512, "output_tokens": 0},
    }
    result.update(changes)
    return {"success": True, "errors": [], "messages": [], "result": result}


class TestCloudflareDecision(unittest.TestCase):
    def setUp(self):
        self.transaction = {"description": "GITHUB monthly subscription", "amount": "25.00",
                            "direction": "Money out", "currency": "USD"}
        self.categories = [{"account": "Software - Co-op", "description": "Recurring software subscriptions"},
                           {"account": "Travel - Co-op", "description": "Business travel expenses"}]
        self.examples = [{**self.transaction, "account": "Software - Co-op"}]
        # These placeholders authenticate no account. Tests inject the transport.
        self.environment = patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "a" * 32,
                                                  "CLOUDFLARE_API_TOKEN": "test-only-placeholder"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.transport = Mock(return_value=json.dumps(answer()).encode())
        self.client = provider.CloudflareDecisionClient(transport=self.transport)

    def classify(self, response=None, **kwargs):
        if response is not None:
            self.transport.return_value = json.dumps(response).encode()
        return self.client.classify(kwargs.get("transaction", self.transaction),
                                    kwargs.get("categories", self.categories), kwargs.get("examples", self.examples))

    def test_official_choice_envelope_becomes_typed_proposal(self):
        result = self.classify()
        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["account"], "Software - Co-op")
        self.assertEqual(result["reason_code"], "MODEL_SUGGESTION")
        self.assertEqual(result["model_id"], "@cf/cloudflare/clef-flash")
        self.assertEqual(result["provider_score"], 0.95)
        self.assertEqual(result["input_tokens"], 512)
        self.assertEqual(len(result["proposal_version"]), 64)
        self.assertNotIn("auto_book", result)

    def test_request_has_fixed_https_destination_typed_choice_and_timeout(self):
        self.classify()
        request, timeout = self.transport.call_args.args
        self.assertEqual(timeout, 5.0)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32
                         + "/ai/run/@cf/cloudflare/clef-flash")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "clef-flash")
        self.assertEqual(body["questions"]["account"]["type"], "choice")
        self.assertEqual(set(body["questions"]["account"]["criteria"]), {"c0", "c1", "needs_review"})
        self.assertTrue(all(isinstance(value, str) and value for value in body["questions"]["account"]["criteria"].values()))
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(set(body["questions"]["account"]), {"type", "instructions", "criteria"})
        self.assertLessEqual(len(request.data), provider.MAX_REQUEST_BYTES)

    def test_unneeded_erp_fields_never_leave_process(self):
        transaction = {**self.transaction, "supplier": "Private Person", "iban": "private-bank-details",
                       "document": {"all": "raw-document"}, "company": "Private Company"}
        example = {**self.examples[0], "owner": "private@example.invalid", "bank_transaction": "PRIVATE-ID"}
        categories = [{**item, "private_note": "private-category-note"} for item in self.categories]
        result = self.classify(transaction=transaction, examples=[example], categories=categories)
        request, _timeout = self.transport.call_args.args
        serialized = request.data.decode()
        for private in ("Private Person", "private-bank-details", "raw-document", "Private Company",
                        "private@example.invalid", "PRIVATE-ID", "private-category-note", "test-only-placeholder"):
            self.assertNotIn(private, serialized)
            self.assertNotIn(private, repr(result))

    def test_credentials_are_read_fresh_and_never_stored_on_client(self):
        first = self.classify()
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "replacement-test-placeholder"}):
            second = self.classify()
        request, _timeout = self.transport.call_args.args
        self.assertEqual(request.get_header("Authorization"), "Bearer replacement-test-placeholder")
        self.assertEqual(first["proposal_version"], second["proposal_version"])
        self.assertNotIn("replacement-test-placeholder", repr(vars(self.client)))

    def test_proposal_hash_changes_with_evidence_policy_or_transaction(self):
        original = self.classify()["proposal_version"]
        changed = self.classify(transaction={**self.transaction, "amount": "26"})["proposal_version"]
        self.assertNotEqual(original, changed)
        self.assertNotEqual(original, self.classify(examples=[])["proposal_version"])
        stricter = provider.CloudflareDecisionClient(min_score=.99, transport=self.transport)
        self.assertNotEqual(original, stricter.classify(self.transaction, self.categories, self.examples)["proposal_version"])
        self.assertEqual(original, self.classify(categories=list(reversed(self.categories)))["proposal_version"])

    def test_proposal_hash_binds_internal_account_mapping_even_with_same_display_name(self):
        original = self.classify(categories=[{"account": "Internal 1", "name": "Software"}], examples=[])
        changed = self.classify(categories=[{"account": "Internal 2", "name": "Software"}], examples=[])
        self.assertNotEqual(original["proposal_version"], changed["proposal_version"])

    def test_missing_or_malformed_configuration_never_calls_provider(self):
        for changes in ({"CLOUDFLARE_API_TOKEN": ""}, {"CLOUDFLARE_ACCOUNT_ID": "../wrong-host"},
                        {"CLOUDFLARE_API_TOKEN": "line\nbreak"}):
            with self.subTest(changes=tuple(changes)), patch.dict(os.environ, changes):
                self.assertEqual(self.classify()["reason_code"], "NOT_CONFIGURED")
        self.transport.assert_not_called()

    def test_unknown_account_in_examples_and_duplicate_categories_fail_closed(self):
        for changes in ({"examples": [{**self.examples[0], "account": "Forbidden"}]},
                        {"categories": self.categories + self.categories}, {"categories": []},
                        {"examples": [{**self.examples[0], "currency": "EUR"}]},
                        {"examples": [{**self.examples[0], "direction": "in"}]}):
            with self.subTest(changes=changes):
                self.assertEqual(self.classify(**changes)["reason_code"], "INVALID_INPUT")
        self.transport.assert_not_called()

    def test_invalid_amounts_and_directions_never_reach_provider(self):
        for amount in (0, -1, True, "NaN", "Infinity", "1e999999", "1e-999999", [], {}):
            with self.subTest(amount=amount):
                self.assertEqual(self.classify(transaction={**self.transaction, "amount": amount})["reason_code"],
                                 "INVALID_INPUT")
        self.assertEqual(self.classify(transaction={**self.transaction, "direction": "transfer"})["reason_code"],
                         "INVALID_INPUT")
        self.transport.assert_not_called()

    def test_input_size_count_and_text_caps_do_not_silently_truncate(self):
        variants = ({"categories": [{"account": str(i)} for i in range(provider.MAX_CATEGORIES + 1)]},
                    {"examples": self.examples * (provider.MAX_EXAMPLES + 1)},
                    {"transaction": {**self.transaction, "description": "x" * 513}})
        for changes in variants:
            with self.subTest(changes=list(changes)):
                self.assertEqual(self.classify(**changes)["reason_code"], "INVALID_INPUT")
        self.transport.assert_not_called()

    def test_whole_request_byte_cap_is_enforced_for_multibyte_payloads(self):
        categories = [{"account": str(i), "description": "界" * 256} for i in range(32)]
        self.assertEqual(self.classify(categories=categories)["reason_code"], "INVALID_INPUT")
        self.transport.assert_not_called()

    def test_review_choice_is_first_class_abstention(self):
        result = self.classify(answer("needs_review", {"c0": .01, "c1": .01, "needs_review": .98}))
        self.assertEqual(result["status"], "abstain")
        self.assertEqual(result["reason_code"], "NEEDS_REVIEW")
        self.assertNotIn("account", result)

    def test_low_score_and_close_runner_up_abstain(self):
        weak = self.classify(answer("c0", {"c0": .8, "c1": .1, "needs_review": .1}))
        self.assertEqual(weak["reason_code"], "LOW_SCORE")
        client = provider.CloudflareDecisionClient(min_score=.5, min_margin=.2, transport=self.transport)
        self.transport.return_value = json.dumps(answer("c0", {"c0": .54, "c1": .45, "needs_review": .01})).encode()
        close = client.classify(self.transaction, self.categories, self.examples)
        self.assertEqual(close["reason_code"], "LOW_MARGIN")
        self.assertNotIn("account", close)

    def test_untrusted_distribution_must_cover_only_complete_allowed_choices(self):
        for probabilities in ({"c0": 1}, {"c0": .9, "c1": .1, "evil": 0},
                              {"c0": .95, "c1": .02, "needs_review": .2},
                              {"c0": .1, "c1": .8, "needs_review": .1}):
            with self.subTest(probabilities=probabilities):
                result = self.classify(answer("c0", probabilities))
                self.assertEqual(result["reason_code"], "INVALID_RESPONSE")
                self.assertNotIn("account", result)

    def test_unknown_choice_wrong_type_and_missing_confidence_are_invalid(self):
        variants = []
        wrong_choice = answer("evil")
        variants.append(wrong_choice)
        wrong_type = answer()
        wrong_type["result"]["answers"]["account"]["type"] = "score"
        variants.append(wrong_type)
        missing_confidence = answer()
        del missing_confidence["result"]["answers"]["account"]["confidence"]
        variants.append(missing_confidence)
        for malformed in variants:
            self.assertEqual(self.classify(malformed)["reason_code"], "INVALID_RESPONSE")

    def test_nonfinite_boolean_out_of_range_and_string_scores_are_rejected(self):
        for score in (math.nan, math.inf, -math.inf, True, "0.95", -0.1, 1.1):
            with self.subTest(score=score):
                response = answer()
                response["result"]["answers"]["account"]["probabilities"]["c0"] = score
                self.assertEqual(self.classify(response)["reason_code"], "INVALID_RESPONSE")

    def test_malformed_truncated_mismatched_model_or_usage_is_rejected(self):
        variants = [answer(truncated=True), answer(input_truncated=True), answer(finish_reason="length"),
                    answer(model="another-model"), answer(usage={"input_tokens": True, "output_tokens": 0}),
                    answer(usage={"input_tokens": 24_577, "output_tokens": 0}), answer(usage={})]
        unsuccessful = answer()
        unsuccessful["success"] = False
        variants.append(unsuccessful)
        for response in variants:
            with self.subTest(response=response):
                self.assertEqual(self.classify(response)["reason_code"], "INVALID_RESPONSE")
        for raw in (b'{"success":true,"result":', b"\xff", b"x" * (provider.MAX_RESPONSE_BYTES + 1),
                    b'{"success":true,"success":false}', b"null", b"[]"):
            self.transport.return_value = raw
            self.assertEqual(self.classify()["reason_code"], "INVALID_RESPONSE")

    def test_provider_errors_are_sanitized_without_retries_or_echoing_body(self):
        cases = [(401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (429, "RATE_LIMITED"),
                 (500, "PROVIDER_UNAVAILABLE"), (400, "PROVIDER_REJECTED"), (302, "PROVIDER_REJECTED")]
        for status, expected in cases:
            with self.subTest(status=status):
                self.transport.reset_mock()
                self.transport.side_effect = HTTPError("https://private", status, "private-provider-message", {},
                                                      io.BytesIO(b"private-response-body"))
                result = self.classify()
                self.assertEqual(result["reason_code"], expected)
                self.assertEqual(self.transport.call_count, 1)
                self.assertNotIn("private", repr(result))
                self.assertNotIn("test-only-placeholder", repr(result))

    def test_timeouts_and_network_errors_are_bounded_abstentions(self):
        for exception, expected in ((TimeoutError("private-timeout"), "TIMEOUT"),
                                    (URLError(socket.timeout("private-timeout")), "TIMEOUT"),
                                    (URLError("private-host"), "PROVIDER_UNAVAILABLE"),
                                    (RuntimeError("private-unexpected"), "PROVIDER_UNAVAILABLE")):
            with self.subTest(exception=type(exception).__name__):
                self.transport.side_effect = exception
                result = self.classify()
                self.assertEqual(result["reason_code"], expected)
                self.assertNotIn("private", repr(result))

    def test_transport_redirects_are_disabled(self):
        handler = provider._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://another-host.invalid"))

    def test_policy_rejects_unbounded_or_nonfinite_timeout(self):
        for timeout in (None, True, 0, 11, math.inf, math.nan):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                provider.CloudflareDecisionClient(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
