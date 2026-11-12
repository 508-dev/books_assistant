"""Classification suggestions preserve permissions, budgets and booking boundaries."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class Doc(dict):
    def __getattr__(self, key):
        return self.get(key)

    def as_dict(self):
        return dict(self)


class TestBankClassification(unittest.TestCase):
    def setUp(self):
        self.frappe = ModuleType('frappe')
        self.frappe._ = lambda text: text
        self.frappe.whitelist = lambda **kwargs: lambda fn: fn
        self.frappe.only_for = Mock()
        self.frappe._dict = Doc
        self.frappe.logger = Mock()
        utils = ModuleType('frappe.utils')
        utils.flt = lambda value: float(value or 0)
        utils.today = lambda: '2026-11-13'
        self.scope = {'company': 'Co-op', 'currency': 'USD'}
        self.bt = Doc(name='BT-NEW', description='GITHUB SUBSCRIPTION', bank_account='Bank', deposit=0,
                      withdrawal=20, unallocated_amount=20, date='2026-11-13', modified='v1', docstatus=1)
        self.bank_api = ModuleType('books_assistant.bank_api')
        self.bank_api._check_line = Mock(return_value=self.bt)
        self.review = ModuleType('books_assistant.bank_review')
        self.context = SimpleNamespace(bank_gl={'Bank': 'Bank GL'}, suggest=Mock(return_value={'kind': 'none'}))
        self.review.Context = Mock(return_value=self.context)
        self.automation = ModuleType('books_assistant.bank_automation')
        self.automation._existing_candidate = Mock(return_value=None)
        self.integration = ModuleType('books_assistant.integration')
        self.integration.check_document = Mock()
        self.integration.resolve_scope = Mock(return_value=self.scope)
        self.integration.fingerprint = lambda data: hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        self.classifier = ModuleType('books_assistant.bank_classifier')
        self.local = SimpleNamespace(dataset_hash='dataset-v1', predict=Mock(return_value={
            'status': 'abstain', 'reason_code': 'LOW_SIMILARITY', 'model_version': 'local-v1'}))
        self.classifier.Classifier = Mock(return_value=self.local)
        from books_assistant.bank_classifier import merchant_key, provider_context
        self.classifier.provider_context = provider_context
        self.classifier.merchant_key = merchant_key
        self.provider = ModuleType('books_assistant.cloudflare_decision')
        self.client = SimpleNamespace(classify=Mock(return_value={'status': 'suggested', 'account': 'Software',
                                                                  'reason_code': 'MODEL_SUGGESTION'}))
        self.provider.CloudflareDecisionClient = Mock(return_value=self.client)
        modules = {'frappe': self.frappe, 'frappe.utils': utils, 'books_assistant.integration': self.integration,
                   'books_assistant.bank_api': self.bank_api, 'books_assistant.bank_review': self.review,
                   'books_assistant.bank_automation': self.automation, 'books_assistant.bank_classifier': self.classifier,
                   'books_assistant.cloudflare_decision': self.provider}
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        import books_assistant
        self.review_patch = patch.object(books_assistant, 'bank_review', self.review, create=True)
        self.review_patch.start()
        self.addCleanup(self.review_patch.stop)
        spec = importlib.util.spec_from_file_location('bank_classification_subject', Path(__file__).parents[1] / 'books_assistant/bank_classification.py')
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.settings = {'bank_local_classifier_enabled': 1, 'bank_cloudflare_classifier_enabled': 1,
                         'bank_classifier_daily_calls': 10}
        self.api._settings = Mock(side_effect=lambda: self.settings)
        self.history = [{'bank_transaction': 'OLD-1', 'journal': 'JE-1', 'description': 'GITHUB MONTHLY', 'account': 'Software',
                         'amount': 20, 'direction': 'Money out', 'company': 'Co-op', 'currency': 'USD', 'bank_account': 'Bank'}]
        self.api._history = Mock(return_value=self.history)
        self.api._categories = Mock(return_value=[{'account': 'Software', 'name': 'Software expense'}])
        self.reserve = self.api._reserve_daily = Mock(return_value=True)

    def test_role_denial_prevents_reading_history_or_calling_provider(self):
        self.frappe.only_for.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.api.suggest('BT-NEW')
        self.bank_api._check_line.assert_not_called()
        self.client.classify.assert_not_called()

    def test_record_denial_prevents_provider_calls(self):
        self.bank_api._check_line.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.api.suggest('BT-NEW')
        self.client.classify.assert_not_called()

    def test_saved_action_wins_without_training_or_network(self):
        self.context.suggest.return_value = {'kind': 'rule', 'account': 'Software'}
        result = self.api.suggest('BT-NEW')
        self.assertEqual((result['source'], result['status']), ('rules', 'existing_action'))
        self.api._history.assert_not_called()
        self.client.classify.assert_not_called()
        self.reserve.assert_not_called()

    def test_partial_or_future_or_unsubmitted_line_never_uses_models(self):
        for changes in ({'unallocated_amount': 10}, {'date': '2027-01-01'}, {'docstatus': 0}):
            with self.subTest(changes=changes), patch.dict(self.bt, changes):
                self.assertEqual(self.api.suggest('BT-NEW')['reason_code'], 'INELIGIBLE_BANK_LINE')
        self.client.classify.assert_not_called()

    def test_local_suggestion_skips_hosted_budget_and_network(self):
        self.local.predict.return_value = {'status': 'suggested', 'account': 'Software', 'reason_code': 'LOCAL_SUGGESTION'}
        result = self.api.suggest('BT-NEW')
        self.assertEqual(result['source'], 'local')
        self.assertEqual(result['execution'], 'review_required')
        self.client.classify.assert_not_called()
        self.reserve.assert_not_called()

    def test_local_abstention_uses_bounded_cloudflare_context(self):
        result = self.api.suggest('BT-NEW')
        self.assertEqual((result['source'], result['account']), ('cloudflare', 'Software'))
        self.assertEqual(result['execution'], 'review_required')
        transaction, categories, examples = self.client.classify.call_args.args
        self.assertEqual(categories, [{'account': 'Software', 'name': 'Software expense'}])
        self.assertEqual(len(examples), 1)
        self.assertNotIn('bank_transaction', examples[0])
        self.assertEqual(result['dataset_hash'], 'dataset-v1')
        self.assertEqual(transaction['currency'], 'USD')
        self.automation._existing_candidate.assert_called_once_with(self.bt, 'Bank GL', self.scope, for_update=False)

    def test_hosted_opt_out_or_budget_exhaustion_makes_no_network_call(self):
        self.settings['bank_cloudflare_classifier_enabled'] = 0
        self.assertEqual(self.api.suggest('BT-NEW')['source'], 'local')
        self.settings['bank_cloudflare_classifier_enabled'] = 1
        self.reserve.return_value = False
        self.assertEqual(self.api.suggest('BT-NEW')['reason_code'], 'PROVIDER_BUDGET_UNAVAILABLE')
        self.client.classify.assert_not_called()

    def test_disabled_local_prediction_cannot_be_used(self):
        self.settings['bank_local_classifier_enabled'] = 0
        self.local.predict.return_value = {'status': 'suggested', 'account': 'Software', 'reason_code': 'LOCAL_SUGGESTION'}
        self.assertEqual(self.api.suggest('BT-NEW')['source'], 'cloudflare')

    def test_unknown_provider_account_never_reaches_ui(self):
        self.client.classify.return_value = {'status': 'suggested', 'account': 'FORGED', 'reason_code': 'MODEL_SUGGESTION'}
        result = self.api.suggest('BT-NEW')
        self.assertEqual(result['reason_code'], 'INVALID_CATEGORY')
        self.assertNotIn('account', result)

    def test_conflicting_or_inaccessible_history_blocks_both_models(self):
        conflict = {**self.history[0], 'bank_transaction': 'OLD-2', 'account': 'Other expense'}
        self.api._history.return_value = self.history + [conflict]
        self.assertEqual(self.api.suggest('BT-NEW')['reason_code'], 'AMBIGUOUS_HISTORY')
        self.local.predict.assert_not_called()
        self.client.classify.assert_not_called()

    def test_hidden_payment_candidate_and_history_overflow_abstain(self):
        self.automation._existing_candidate.return_value = 'EXISTING_PAYMENT'
        self.assertEqual(self.api.suggest('BT-NEW')['reason_code'], 'EXISTING_PAYMENT')
        self.api._history.assert_not_called()
        self.automation._existing_candidate.return_value = None
        self.api._history.return_value = self.history * (self.api.MAX_HISTORY + 1)
        self.assertEqual(self.api.suggest('BT-NEW')['reason_code'], 'HISTORY_CAPACITY_EXCEEDED')
        self.client.classify.assert_not_called()

    def test_logs_contain_metadata_not_bank_text_or_model_body(self):
        self.api.suggest('BT-NEW')
        logged = str(self.frappe.logger.return_value.info.call_args)
        for private in ('GITHUB', 'OLD-1', 'BT-NEW', 'Software'):
            self.assertNotIn(private, logged)
        self.assertIn('bank.classify', logged)

    def test_daily_reservation_uses_atomic_site_scoped_counter_and_fails_closed(self):
        from importlib import util
        spec = util.spec_from_file_location('bank_budget_subject', Path(__file__).parents[1] / 'books_assistant/bank_classification.py')
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cache = SimpleNamespace(make_key=Mock(return_value='site:company:day'), eval=Mock(return_value=11))
        self.frappe.cache = Mock(return_value=cache)
        self.assertFalse(module._reserve_daily(self.scope, 10))
        self.assertIn("redis.call('INCR'", cache.eval.call_args.args[0])
        self.assertEqual(cache.eval.call_args.args[1:], (1, 'site:company:day'))
        cache.eval.side_effect = RuntimeError
        self.assertFalse(module._reserve_daily(self.scope, 10))


    def test_empty_benchmark_reports_no_data_and_makes_no_hosted_call(self):
        self.api._history.return_value = []
        result = self.api.benchmark("Co-op")
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["source_label"], "visible_erp_history")
        self.client.classify.assert_not_called()
        self.reserve.assert_not_called()


    def test_local_unknown_account_is_rejected_even_when_hosted_is_disabled(self):
        self.settings['bank_cloudflare_classifier_enabled'] = 0
        self.local.predict.return_value = {'status': 'suggested', 'account': 'FORGED', 'reason_code': 'CENTROID_MATCH'}
        result = self.api.suggest('BT-NEW')
        self.assertEqual(result['status'], 'abstain')
        self.assertNotIn('account', result)

    def _real_rule_controller(self):
        self.frappe._ = lambda value: value
        self.frappe.throw = Mock(side_effect=ValueError)
        utils = sys.modules['frappe.utils']
        utils.add_days = utils.date_diff = utils.nowdate = Mock()
        self.integration.lock_documents = Mock()
        self.frappe.get_doc = Mock()
        sys.modules['books_assistant.bank_classification'] = self.api
        spec = importlib.util.spec_from_file_location('model_rule_subject', Path(__file__).parents[1] / 'books_assistant/bank_review.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.unique_rule_name = Mock(return_value='Suggested rule')
        return module

    def test_model_rule_creation_rejects_a_changed_source(self):
        controller = self._real_rule_controller()
        reviewed = self.integration.fingerprint(self.api.transaction_snapshot(self.bt, self.scope))
        self.bt['modified'] = 'v2'
        with self.assertRaises(ValueError):
            controller.create_rule('BT-NEW', 'GITHUB', 'Software', reviewed)
        self.frappe.get_doc.assert_not_called()

    def test_model_rule_creation_is_bank_scoped_and_has_no_booking_authority(self):
        controller = self._real_rule_controller()
        reviewed = self.integration.fingerprint(self.api.transaction_snapshot(self.bt, self.scope))
        controller.create_rule('BT-NEW', 'GITHUB', 'Software', reviewed)
        self.integration.lock_documents.assert_any_call([("Bank Transaction", "BT-NEW")])
        payload = self.frappe.get_doc.call_args.args[0]
        self.assertEqual(payload['bank_account'], 'Bank')
        self.assertEqual(payload['auto_book'], 0)
        self.assertEqual(payload['account'], 'Software')

    def test_read_only_training_query_does_not_take_accounting_locks(self):
        from books_assistant import bank_learning
        sys.modules['frappe.utils'].add_days = Mock(return_value='2024-11-13')
        self.frappe.db = SimpleNamespace(sql=Mock(return_value=[]))
        self.integration.lock_documents = Mock()
        rows = bank_learning.load_observations(self.scope, history_days=730, for_update=False, max_rows=2001)
        self.assertEqual(rows, [])
        query = self.frappe.db.sql.call_args.args[0].lower()
        self.assertNotIn('for update', query)
        self.assertIn('limit 2001', query)
        self.integration.lock_documents.assert_not_called()


    def test_suggestion_returns_current_source_fields_and_reconciliation_state_fingerprint(self):
        result = self.api.suggest('BT-NEW')
        self.assertEqual(result['description'], self.bt.description)
        self.assertEqual(result['keyword'], 'GITHUB')
        self.assertEqual(result['bank_account'], self.bt.bank_account)
        before = result['input_fingerprint']
        self.bt['unallocated_amount'] = 10
        self.assertNotEqual(before, self.integration.fingerprint(self.api.transaction_snapshot(self.bt, self.scope)))

    def test_unrelated_unicode_merchant_history_does_not_block_suggestions(self):
        self.bt['description'] = 'ヤマダタロウ サービス'
        self.api._history.return_value = [
            {**self.history[0], 'description': 'ヤマダタロウ サービス'},
            {**self.history[0], 'bank_transaction': 'OLD-2', 'description': 'タナカジロウ サービス', 'account': 'Other'},
        ]
        self.local.predict.return_value = {'status': 'suggested', 'account': 'Software', 'reason_code': 'CENTROID_MATCH'}
        self.assertEqual(self.api.suggest('BT-NEW')['source'], 'local')

    def test_read_only_blocker_queries_do_not_hold_locks_during_external_call(self):
        utils = sys.modules['frappe.utils']
        utils.now_datetime = Mock()
        spec = importlib.util.spec_from_file_location('readonly_blocker_subject', Path(__file__).parents[1] / 'books_assistant/bank_automation.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.frappe.db = SimpleNamespace(sql=Mock(return_value=[]))
        self.assertIsNone(module._existing_candidate(self.bt, 'Bank GL', self.scope, for_update=False))
        self.assertTrue(self.frappe.db.sql.call_count > 0)
        self.assertTrue(all('for update' not in call.args[0].lower() for call in self.frappe.db.sql.call_args_list))
