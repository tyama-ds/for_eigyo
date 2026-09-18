"""LLM judgment checks: injected model replies, isolated application data only."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
import llm_judgment


module = integration.module


def decision(patent_id, verdict='keep', confidence=.95, relevance=.85, reason='本文が基準に対応しています。'):
    return dict(id=patent_id, decision=verdict, confidence=confidence, relevance=relevance, reason=reason)


def answer(payload):
    return {'decisions': [decision(row['id']) for row in payload['patents']]}


def applied_query():
    return dict(id='query-applied', purpose='界面技術の母集団を作る', facets=[
        dict(id='facet1', name='界面', role='required', terms=['界面', 'interface'],
             abstract_terms=['未採用の抽象語'])])


class JudgmentValidationTests(unittest.TestCase):
    def test_union_criteria_keep_alternatives_and_change_resume_fingerprint(self):
        query = applied_query()
        query['facets'] = [dict(id='a', name='自動運転', role='required', terms=['自動運転'], or_group='driving'),
                           dict(id='b', name='運転システム', role='required', terms=['運転システム'], or_group='driving'),
                           dict(id='c', name='自動車', role='required', terms=['自動車'])]
        before = copy.deepcopy(query)
        result = llm_judgment.prepare({}, [dict(id='p', label=None)], '', 'local', adopted_query=query)
        self.assertIn('和集合（いずれかの観点）: 自動運転 OR 運転システム', result['criteria'])
        self.assertIn('必須観点: 自動車', result['criteria'])
        self.assertNotIn('必須観点: 運転システム', result['criteria'])
        self.assertEqual(result['query_context']['concepts'][1]['or_group'], 'driving')
        query['facets'][1]['or_group'] = 'independent'
        separated = llm_judgment.prepare({}, [dict(id='p', label=None)], '', 'local', adopted_query=query)
        self.assertNotEqual(result['context_fingerprint'], separated['context_fingerprint'])
        self.assertEqual(before['facets'][1]['or_group'], 'driving')
        for value in ([], 'bad group', 'x' * 33):
            query['facets'][1]['or_group'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                llm_judgment.snapshot_query_context(query)

    def test_applied_query_context_fills_empty_criteria_without_overriding_explicit_input(self):
        query = applied_query()
        before = copy.deepcopy(query)
        rows = [dict(id='p1', label=None)]
        automatic = llm_judgment.prepare({}, rows, 'old keywords', 'local', adopted_query=query)
        explicit = llm_judgment.prepare({'criteria': '今回の独自基準'}, rows, 'old keywords', 'local', adopted_query=query)
        self.assertEqual(automatic['criteria_source'], 'adopted_query')
        self.assertIn('界面技術の母集団を作る', automatic['criteria'])
        self.assertIn('必須観点: 界面', automatic['criteria'])
        self.assertEqual(explicit['criteria'], '今回の独自基準')
        self.assertEqual(explicit['criteria_source'], 'explicit')
        self.assertNotIn('未採用の抽象語', json.dumps(automatic['query_context'], ensure_ascii=False))
        self.assertEqual(automatic['context_fingerprint'], explicit['context_fingerprint'])
        automatic['query_context']['concepts'][0]['terms'].append('changed')
        self.assertEqual(query, before)

    def test_prepare_accepts_full_csv_limits_and_retains_default_hundred(self):
        rows = [dict(id=f'publication-{n}', label=None, label_source=None) for n in range(5000)]
        for count in (443, 5000):
            with self.subTest(count=count):
                options = llm_judgment.prepare({'max_items': count}, rows, 'topic', 'local')
                self.assertEqual(options['max_items'], count)
                self.assertEqual(options['target_ids'], [row['id'] for row in rows[:count]])
                self.assertEqual(options['available_count'], 5000)
        self.assertEqual(len(llm_judgment.prepare({}, rows, 'topic', 'local')['target_ids']), 100)
        with self.assertRaises(ValueError):
            llm_judgment.prepare({'max_items': 5001}, rows, 'topic', 'local')

    def test_all_batch_ids_and_probability_fields_are_required(self):
        valid = {'decisions': [decision('a'), decision('b')]}
        changes = [None, [], {}, {'decisions': {}}, {'decisions': [decision('a')]},
                   {'decisions': [decision('a'), decision('a')]},
                   {'decisions': [decision('a'), decision('outside')]}]
        for field, value in [('id', []), ('decision', 'keep|exclude'), ('confidence', True),
                             ('confidence', '0.9'), ('confidence', float('nan')),
                             ('confidence', float('inf')), ('confidence', -.1),
                             ('relevance', False), ('relevance', '0.9'), ('relevance', float('nan')),
                             ('relevance', 1.1), ('reason', ''), ('reason', {}), ('reason', 'a'*1001)]:
            invalid = copy.deepcopy(valid)
            invalid['decisions'][1][field] = value
            changes.append(invalid)
        for field in ('id', 'decision', 'confidence', 'relevance', 'reason'):
            invalid = copy.deepcopy(valid)
            del invalid['decisions'][1][field]
            changes.append(invalid)
        for result in changes:
            with self.subTest(result=result), self.assertRaises(ValueError):
                llm_judgment.validate_decisions(result, ['a', 'b'], .8)
        self.assertEqual(valid['decisions'][0], decision('a'))

    def test_deferred_results_have_no_label_or_score_and_distinct_confidence(self):
        result = {'decisions': [decision('high', relevance=.86, confidence=.97),
                                decision('low', relevance=.99, confidence=.2),
                                decision('unsure', verdict='unsure', relevance=.98, confidence=.99)]}
        high, low, unsure = llm_judgment.validate_decisions(result, ['high', 'low', 'unsure'], .8)
        self.assertEqual(high['score'], .86)
        self.assertEqual(high['agent_confidence'], .97)
        self.assertEqual(high['score_source'], 'llm')
        for item in (low, unsure):
            self.assertIsNone(item['score'])
            self.assertIsNone(item['label'])
            self.assertEqual(item['label_source'], 'agent')
            self.assertIsNone(item['score_source'])
            self.assertGreater(item['llm_relevance'], .9)

    def test_prepare_prioritizes_fresh_then_deferred_and_protects_human_labels(self):
        rows = [dict(id='deferred', label=None, label_source='agent'),
                dict(id='human', label='keep', label_source='human'),
                dict(id='agent_done', label='exclude', label_source='agent'),
                dict(id='fresh', label=None, label_source=None)]
        options = llm_judgment.prepare({}, rows, 'fallback criterion', 'local')
        self.assertEqual(options['criteria'], 'fallback criterion')
        self.assertEqual(options['target_ids'], ['fresh', 'deferred'])
        self.assertEqual(llm_judgment.prepare({'max_items': 1}, rows, 'topic', 'local')['target_ids'], ['fresh'])
        for invalid in [dict(threshold=value) for value in (True, '0.8', float('nan'), float('inf'), .49, 1.01)]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                llm_judgment.prepare(invalid, rows, 'topic', 'local')


class JudgmentRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(module.app)
        module.JOB.update(status='idle')
        module.STOP.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.client.post('/api/demo')
        module.SETTINGS.update(provider='local', base_url='http://example.invalid/v1', model='test-model', api_key='isolated-secret')
        module.STATE['patents'] = module.STATE['patents'][:10]
        for row in module.STATE['patents']:
            row['score'] = .123
        module.STATE['patents'][0].update(label='keep', label_source='human', label_reason='人の必要判定')
        module.STATE['patents'][1].update(label='exclude', label_source='human', label_reason='人の不要判定')
        module.save()

    def tearDown(self):
        self.wait_job()
        module.STOP.clear()

    def wait_job(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # The worker sets its final status before saving, while holding LOCK.
            # Wait for that save too before removing mocks or resetting fixtures.
            with module.LOCK:
                if module.JOB['status'] != 'running':
                    return
            time.sleep(.01)
        self.assertNotEqual(module.JOB['status'], 'running')

    def test_local_llm_receives_human_examples_and_saves_distinct_scores(self):
        before = copy.deepcopy(module.STATE)
        requests = []

        def respond(settings, system, payload, **kwargs):
            schema = kwargs['response_schema']
            self.assertEqual(schema['properties']['decisions']['minItems'], len(payload['patents']))
            self.assertEqual(schema['properties']['decisions']['maxItems'], len(payload['patents']))
            self.assertEqual(schema['properties']['decisions']['items']['properties']['id']['enum'], payload['required_ids'])
            requests.append(copy.deepcopy((settings, system, payload)))
            choices = [('keep', .95, .91), ('exclude', .92, .12), ('keep', .3, .99), ('unsure', .99, .95)]
            return {'decisions': [decision(row['id'], *values) for row, values in zip(payload['patents'], choices)]}

        with patch.object(module, 'complete', side_effect=respond):
            response = self.client.post('/api/train', json={'mode': 'llm', 'criteria': '固体電池の製造工程'})
            self.assertEqual(response.status_code, 200)
            self.wait_job()
        self.assertEqual(module.JOB['status'], 'done', module.JOB)
        self.assertEqual(len(requests), 2)
        for settings, system, payload in requests:
            from prompt_templates import JUDGMENT_SYSTEM
            self.assertEqual(system, JUDGMENT_SYSTEM)
            self.assertEqual(settings['provider'], 'local')
            self.assertEqual(settings['api_key'], 'isolated-secret')
            self.assertEqual(payload['criteria'], '固体電池の製造工程')
            self.assertEqual({item['decision'] for item in payload['human_examples']}, {'keep', 'exclude'})
            self.assertTrue(all('id' not in item for item in payload['human_examples']))
            self.assertEqual(payload['required_ids'], [item['id'] for item in payload['patents']])
            self.assertEqual(payload['required_decision_count'], len(payload['patents']))
            self.assertIn('relevance', system)
        rows = module.STATE['patents']
        for before_row, after_row in zip(before['patents'][:2], rows[:2]):
            for key in ('label', 'label_source', 'label_reason'):
                self.assertEqual(before_row[key], after_row[key])
            self.assertIsNone(after_row['score'])
            self.assertIsNone(after_row['score_source'])
        self.assertEqual(rows[2]['score'], .91)
        self.assertEqual(rows[2]['score_source'], 'llm')
        self.assertEqual(rows[2]['agent_confidence'], .95)
        self.assertEqual(rows[3]['score'], .12)
        self.assertEqual(rows[3]['label'], 'exclude')
        for row in (rows[4], rows[5], rows[8], rows[9]):
            self.assertIsNone(row['label'])
            self.assertIsNone(row['score'])
            self.assertIsNone(row['score_source'])
        trained = module.STATE['training']
        self.assertEqual((trained['completed_count'], trained['total_count'], trained['judged_count'], trained['deferred_count']), (8, 8, 4, 4))
        self.assertEqual(trained['mode'], 'llm')
        self.assertEqual(trained['train_count'], 0)
        self.assertIsNone(trained['metrics'])
        self.assertEqual(trained['job_id'], response.json()['job_id'])
        self.assertTrue(trained['includes_agent_labels'])
        self.assertEqual(module.STATE['queries'], before['queries'])
        public = self.client.get('/api/state').json()
        self.assertEqual(public['training']['status'], 'completed')
        self.assertNotIn('isolated-secret', json.dumps(public))
        persisted = (module.DATA / 'workspace.json').read_text(encoding='utf-8')
        self.assertNotIn('isolated-secret', persisted)
        self.assertEqual(json.loads(persisted)['state']['training']['completed_count'], 8)

    def test_zero_examples_and_keywords_fallback_are_allowed_with_bounded_count(self):
        for row in module.STATE['patents']:
            row.update(label=None, label_source=None, label_reason='')
        module.STATE['keywords'] = 'vehicle sensing'
        with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)) as complete:
            response = self.client.post('/api/train', json={'mode': 'llm', 'max_items': 1})
            self.assertEqual(response.status_code, 200)
            self.wait_job()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.args[2]['human_examples'], [])
        self.assertEqual(complete.call_args.args[2]['criteria'], 'vehicle sensing')
        self.assertEqual(module.STATE['training']['total_count'], 1)

    def test_applied_query_is_snapshotted_and_unadopted_plan_is_not_sent(self):
        query = applied_query()
        query['boolean_tree'] = dict(op='or', children=[dict(op='text', value='界面'), dict(op='text', value='interface')])
        expected_conditions = copy.deepcopy(query['boolean_tree'])
        requests = []
        workbench = dict(active_query_id=query['id'], plan=dict(purpose='未採用の別目的', concepts=['未採用の別観点']))

        def respond(settings, system, payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            query['facets'][0]['terms'][0] = '判定開始後の変更'
            query['boolean_tree']['children'][0]['value'] = '判定開始後の変更'
            workbench['active_query_id'] = 'another-query'
            return answer(payload)

        with patch.dict(module.STATE, {'queries': [query], 'research_workbench': workbench}):
            with patch.object(module, 'complete', side_effect=respond):
                response = self.client.post('/api/train', json={'mode': 'llm'})
                self.assertEqual(response.status_code, 200, response.text)
                self.wait_job()
            self.assertGreater(len(requests), 1)
            for payload in requests:
                self.assertEqual(payload['criteria_source'], 'adopted_query')
                self.assertEqual(payload['purpose'], '界面技術の母集団を作る')
                self.assertEqual(payload['concepts'][0]['terms'], ['界面', 'interface'])
                self.assertEqual(payload['search_conditions'], expected_conditions)
                self.assertEqual(payload['adopted_query_id'], 'query-applied')
                self.assertNotIn('未採用', json.dumps(payload, ensure_ascii=False))
            self.assertEqual(module.STATE['training']['query_context']['concepts'][0]['terms'], ['界面', 'interface'])

    def test_unadopted_plan_alone_does_not_change_judgment_criteria(self):
        with patch.dict(module.STATE, {'queries': [], 'keywords': '元のテーマ',
                                      'research_workbench': dict(plan=applied_query())}):
            with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)) as complete:
                response = self.client.post('/api/train', json={'mode': 'llm', 'max_items': 1})
                self.assertEqual(response.status_code, 200)
                self.wait_job()
            payload = complete.call_args.args[2]
            self.assertEqual(payload['criteria'], '元のテーマ')
            self.assertNotIn('concepts', payload)
            self.assertNotIn('purpose', payload)

    def test_applied_context_fingerprint_controls_score_resumption(self):
        originals = copy.deepcopy(module.STATE['patents'])
        original_query = applied_query()
        context = llm_judgment.snapshot_query_context(original_query)
        for changed in (False, True):
            with self.subTest(changed=changed):
                rows = copy.deepcopy(originals)
                rows[2].update(label='keep', label_source='agent', label_reason='前回の理由',
                               score=.88, score_source='llm', agent_confidence=.96, llm_relevance=.88)
                query = copy.deepcopy(original_query)
                if changed:
                    query['facets'][0]['terms'] = ['接合']
                prior = dict(mode='llm', criteria='同一の明示基準', provider='local', model='test-model',
                             context_fingerprint=llm_judgment.context_fingerprint(context))
                with patch.dict(module.STATE, {'patents': rows, 'training': prior, 'queries': [query],
                                              'research_workbench': dict(active_query_id=query['id'])}):
                    with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)) as complete:
                        response = self.client.post('/api/train', json={'mode': 'llm', 'criteria': '同一の明示基準', 'max_items': 1})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.wait_job()
                    self.assertEqual(complete.call_args.args[2]['criteria'], '同一の明示基準')
                    self.assertEqual(rows[2]['label'], 'keep')
                    self.assertEqual(rows[2]['score'], None if changed else .88)

    def test_resuming_same_judgment_preserves_only_completed_llm_scores(self):
        rows = module.STATE['patents']
        rows[0].update(score=.77, score_source='llm')  # Human scores are still invalidated.
        rows[2].update(label='keep', label_source='agent', label_reason='前回の理由', score=.88,
                       score_source='llm', agent_confidence=.96, llm_relevance=.88)
        rows[3].update(label='exclude', label_source='agent', score=.15, score_source='lightweight')
        retained = copy.deepcopy(rows[2])
        module.STATE['training'] = dict(mode='llm', criteria='same criteria', provider='local',
                                        model='test-model', status='error')
        with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)):
            response = self.client.post('/api/train', json={'mode': 'llm', 'criteria': 'same criteria', 'max_items': 1})
            self.assertEqual(response.status_code, 200)
            self.wait_job()
        self.assertEqual(module.JOB['status'], 'done', module.JOB)
        self.assertEqual(rows[2], retained)
        for row in (rows[0], rows[3], rows[5]):
            self.assertIsNone(row['score'])
            self.assertIsNone(row['score_source'])
        self.assertEqual(rows[0]['label'], 'keep')
        self.assertEqual(rows[0]['label_source'], 'human')
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['patents'][2]['score'], .88)

    def test_changed_judgment_context_invalidates_prior_llm_scores_but_keeps_labels(self):
        original_rows = copy.deepcopy(module.STATE['patents'])
        baseline = dict(mode='llm', criteria='same criteria', provider='local', model='test-model', status='error')
        for key, previous_value in [('criteria', 'other criteria'), ('provider', 'api'),
                                    ('model', 'other-model'), ('mode', 'lightweight')]:
            with self.subTest(changed=key):
                module.STATE['patents'] = copy.deepcopy(original_rows)
                row = module.STATE['patents'][2]
                row.update(label='keep', label_source='agent', label_reason='前回の理由', score=.88,
                           score_source='llm', agent_confidence=.96, llm_relevance=.88)
                module.STATE['training'] = dict(baseline, **{key: previous_value})
                with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)):
                    response = self.client.post('/api/train', json={'mode': 'llm', 'criteria': 'same criteria', 'max_items': 1})
                    self.assertEqual(response.status_code, 200)
                    self.wait_job()
                self.assertEqual(module.JOB['status'], 'done', module.JOB)
                self.assertIsNone(row['score'])
                self.assertIsNone(row['score_source'])
                self.assertEqual((row['label'], row['label_source'], row['label_reason']),
                                 ('keep', 'agent', '前回の理由'))
                self.assertEqual(row['agent_confidence'], .96)

    def test_learning_replaces_llm_score_source_without_reusing_old_relevance(self):
        for mode in ('lightweight', 'transformer'):
            for row in module.STATE['patents']:
                row.update(score=.9, score_source='llm', llm_relevance=.9)
            scores = {row['id']: .25 for row in module.STATE['patents']}
            with patch.object(module, 'train', return_value=dict(scores=scores, metrics=None, train_count=4)):
                self.assertEqual(self.client.post('/api/train', json={'mode': mode}).status_code, 200)
                self.wait_job()
            self.assertEqual(module.JOB['status'], 'done')
            for row in module.STATE['patents']:
                self.assertEqual(row['score'], .25)
                self.assertEqual(row['score_source'], mode)
                self.assertEqual(row['llm_relevance'], .9)

    def test_unexpected_llm_failure_exposes_no_internal_details(self):
        with patch.object(module, 'complete', side_effect=RuntimeError('private-model-response api-key-secret')) as complete:
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
            self.wait_job()
        public = self.client.get('/api/state').text
        self.assertEqual(module.JOB['status'], 'error')
        self.assertNotIn('private-model-response', public)
        self.assertNotIn('api-key-secret', public)
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertEqual(complete.call_count, 1)

    def test_request_failure_is_not_retried_as_a_judgment_format_error(self):
        with patch.object(module, 'complete', side_effect=ValueError('LLMに接続できませんでした。')) as complete:
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
            self.wait_job()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(module.JOB['status'], 'error')
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][2:]))

    def test_invalid_judgment_retries_whole_batch_before_any_result_is_saved(self):
        requests = []

        def respond(settings, system, payload, **kwargs):
            requests.append(copy.deepcopy((system, payload)))
            if len(requests) == 1:
                result = answer(payload)
                result['decisions'].append(decision('untrusted-extra-id'))
                return result
            training = module.STATE['training']
            self.assertEqual((training['completed_count'], training['judged_count']), (0, 0))
            self.assertEqual(training['message'], '判定形式を再確認中 0/4')
            self.assertEqual(module.JOB['message'], training['message'])
            self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][2:]))
            saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['state']['training']['completed_count'], 0)
            self.assertTrue(all(row['label'] is None for row in saved['state']['patents'][2:]))
            return answer(payload)

        with patch.object(module, 'complete', side_effect=respond):
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm', 'max_items': 4}).status_code, 200)
            self.wait_job()
        self.assertEqual(module.JOB['status'], 'done', module.JOB)
        self.assertEqual(len(requests), 2)
        first, second = requests
        self.assertEqual(first[1], second[1])
        self.assertTrue(all('id' not in row for row in second[1]['human_examples']))
        self.assertEqual(second[1]['required_decision_count'], 4)
        self.assertEqual(second[1]['required_ids'], [row['id'] for row in module.STATE['patents'][2:6]])
        self.assertEqual(second[0], llm_judgment.SYSTEM + llm_judgment.RETRY_INSTRUCTION)
        self.assertNotIn('untrusted-extra-id', second[0])
        self.assertNotIn('untrusted-extra-id', json.dumps(second[1]))
        self.assertEqual(module.STATE['training']['completed_count'], 4)
        self.assertTrue(all(row['label'] == 'keep' for row in module.STATE['patents'][2:6]))

    def test_invalid_request_offline_and_empty_data_are_synchronous_and_atomic(self):
        invalid_bodies = [{'mode': 'bad'}, {'mode': 'llm', 'criteria': None},
                          {'mode': 'llm', 'criteria': 'a'*5001}]
        invalid_bodies += [dict(mode='llm', max_items=value) for value in (0, 5001, 1.5, True, '5', None)]
        invalid_bodies += [dict(mode='llm', threshold=value) for value in (.49, 1.1, True, '0.8', None)]
        with patch.object(module, 'complete') as complete:
            for body in invalid_bodies:
                before = copy.deepcopy(module.STATE)
                disk = (module.DATA / 'workspace.json').read_bytes()
                with self.subTest(body=body):
                    self.assertEqual(self.client.post('/api/train', json=body).status_code, 400)
                    self.assertEqual(module.STATE, before)
                    self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
                    self.assertEqual(module.JOB['status'], 'idle')
            module.SETTINGS['provider'] = 'offline'
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 400)
            module.SETTINGS['provider'] = 'local'
            module.STATE['keywords'] = ''
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 400)
            module.STATE['patents'] = []
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm', 'criteria': 'topic'}).status_code, 400)
            complete.assert_not_called()

    def test_invalid_second_batch_retains_first_batch_and_progress(self):
        requests = 0

        def respond(settings, system, payload, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                return answer(payload)
            result = answer(payload)
            result['decisions'][-1]['id'] = 'outside-batch'
            return result

        with patch.object(module, 'complete', side_effect=respond):
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
            self.wait_job()
        self.assertEqual(module.JOB['status'], 'error')
        self.assertEqual(requests, 3)  # One successful batch, then one failed batch and its single retry.
        training = module.STATE['training']
        self.assertEqual(training['status'], 'error')
        self.assertEqual((training['completed_count'], training['total_count'], training['progress']), (4, 8, 50))
        self.assertTrue(all(row['label'] == 'keep' for row in module.STATE['patents'][2:6]))
        self.assertTrue(all(row['label'] is None and row['score'] is None for row in module.STATE['patents'][6:]))
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['training']['completed_count'], 4)

    def test_stop_before_corrective_request_does_not_send_it(self):
        original = llm_judgment.validate_decisions

        def stop_on_invalid(*args):
            try:
                return original(*args)
            except ValueError:
                module.STOP.set()
                raise

        with patch.object(module, 'complete', return_value={'decisions': []}) as complete:
            with patch.object(llm_judgment, 'validate_decisions', side_effect=stop_on_invalid):
                self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                self.wait_job()
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][2:]))

    def test_stop_during_corrective_request_discards_its_valid_response(self):
        in_retry, release = threading.Event(), threading.Event()
        requests = 0

        def respond(settings, system, payload, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                return {'decisions': []}
            in_retry.set()
            release.wait(5)
            return answer(payload)

        with patch.object(module, 'complete', side_effect=respond):
            try:
                self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                self.assertTrue(in_retry.wait(5))
                state = self.client.get('/api/state').json()
                self.assertEqual(state['training']['message'], '判定形式を再確認中 0/8')
                self.assertEqual(state['training']['completed_count'], 0)
                self.assertEqual(self.client.post('/api/job/stop').status_code, 200)
            finally:
                release.set()
                self.wait_job()
        self.assertEqual(requests, 2)
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][2:]))

    def test_initial_save_failure_restores_scores_and_temporary_file_before_error_report(self):
        before_rows = copy.deepcopy(module.STATE['patents'])
        temp_path = module.DATA / 'workspace.tmp'
        temp_path.write_bytes(b'previous temporary contents')
        original_save = module.save
        failed = False
        observed = []

        def fail_initial_save():
            nonlocal failed
            if not failed:
                failed = True
                temp_path.write_bytes(b'failed new temporary contents')
                raise OSError('private disk error')
            if module.STATE['training']['status'] == 'error' and not observed:
                observed.append(dict(rows=copy.deepcopy(module.STATE['patents']), temp=temp_path.read_bytes(),
                                     progress=module.JOB['progress'],
                                     metadata=copy.deepcopy(module.STATE['training'])))
            original_save()

        with patch.object(module, 'save', side_effect=fail_initial_save):
            with patch.object(module, 'complete') as complete:
                self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                self.wait_job()
                complete.assert_not_called()
        self.assertEqual(module.JOB['status'], 'error')
        self.assertEqual(module.STATE['patents'], before_rows)
        self.assertEqual(observed[0]['rows'], before_rows)
        self.assertEqual(observed[0]['temp'], b'previous temporary contents')
        self.assertEqual(observed[0]['progress'], 0)
        self.assertEqual(observed[0]['metadata']['completed_count'], 0)
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertNotIn('private disk error', self.client.get('/api/state').text)
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['patents'], before_rows)
        self.assertFalse(temp_path.exists())

    def test_failed_batch_save_rolls_back_uncommitted_rows_and_counts(self):
        for failed_count, retained_count in ((4, 0), (8, 4)):
            with self.subTest(failed_count=failed_count):
                self.setUp()
                original_save = module.save
                failed = False
                observed = []
                temp_path = module.DATA / 'workspace.tmp'

                def fail_batch_save():
                    nonlocal failed
                    metadata = module.STATE['training']
                    if not failed and metadata['status'] == 'running' and metadata['completed_count'] == failed_count:
                        failed = True
                        temp_path.write_bytes(b'partially written batch')
                        raise OSError('private disk error')
                    if metadata['status'] == 'error' and not observed:
                        observed.append(dict(rows=copy.deepcopy(module.STATE['patents']), temp_exists=temp_path.exists(),
                                             metadata=copy.deepcopy(metadata), progress=module.JOB['progress']))
                    original_save()

                with patch.object(module, 'save', side_effect=fail_batch_save):
                    with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)) as complete:
                        self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                        self.wait_job()
                self.assertEqual(module.JOB['status'], 'error')
                self.assertEqual(complete.call_count, failed_count // llm_judgment.BATCH_SIZE)
                metadata = module.STATE['training']
                self.assertEqual(metadata['status'], 'error')
                self.assertEqual(metadata['completed_count'], retained_count)
                self.assertEqual(metadata['judged_count'], retained_count)
                self.assertEqual(metadata['progress'], retained_count * 100 // 8)
                self.assertEqual(observed[0]['progress'], retained_count * 100 // 8)
                self.assertFalse(observed[0]['temp_exists'])
                self.assertTrue(all(row['label'] == 'keep' for row in module.STATE['patents'][2:2+retained_count]))
                self.assertTrue(all(row['label'] is None and row['score'] is None and row['score_source'] is None
                                    for row in module.STATE['patents'][2+retained_count:]))
                self.assertTrue(all(row['label'] is None for row in observed[0]['rows'][2+retained_count:]))
                saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['state']['training']['completed_count'], retained_count)
                self.assertEqual(saved['state']['patents'], module.STATE['patents'])
                self.assertFalse(temp_path.exists())

    def test_stop_during_response_discards_pending_batch_and_keeps_saved_work(self):
        in_second, release = threading.Event(), threading.Event()
        requests = 0

        def respond(settings, system, payload, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 2:
                in_second.set()
                release.wait(5)
            return answer(payload)

        with patch.object(module, 'complete', side_effect=respond):
            try:
                self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                self.assertTrue(in_second.wait(5))
                state = self.client.get('/api/state').json()
                self.assertEqual(state['training']['completed_count'], 4)
                self.assertEqual(state['training']['status'], 'running')
                self.assertIn('4/8', state['job']['message'])
                self.assertEqual(state['job']['kind'], 'training')
                self.assertEqual(state['job']['mode'], 'llm')
                self.assertEqual(self.client.post('/api/job/stop').status_code, 200)
            finally:
                release.set()
                self.wait_job()
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(module.STATE['training']['status'], 'cancelled')
        self.assertEqual(module.STATE['training']['completed_count'], 4)
        self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][6:]))

    def test_current_human_label_is_checked_again_before_commit(self):
        target = module.STATE['patents'][2]

        def respond(settings, system, payload, **kwargs):
            with module.LOCK:
                target.update(label='exclude', label_source='human', label_reason='応答待ち中の人の判定')
            return answer(payload)

        with patch.object(module, 'complete', side_effect=respond):
            self.assertEqual(self.client.post('/api/train', json={'mode': 'llm', 'max_items': 1}).status_code, 200)
            self.wait_job()
        self.assertEqual(target['label'], 'exclude')
        self.assertEqual(target['label_source'], 'human')
        self.assertEqual(target['label_reason'], '応答待ち中の人の判定')
        self.assertIsNone(target['score'])
        self.assertEqual(module.STATE['training']['protected_count'], 1)
        self.assertEqual(module.STATE['training']['judged_count'], 0)

    def test_stop_immediately_before_commit_does_not_count_discarded_results(self):
        original = llm_judgment.validate_decisions

        def stop_after_validation(*args):
            updates = original(*args)
            module.STOP.set()
            return updates

        with patch.object(module, 'complete', side_effect=lambda settings, system, payload, **kwargs: answer(payload)):
            with patch.object(llm_judgment, 'validate_decisions', side_effect=stop_after_validation):
                self.assertEqual(self.client.post('/api/train', json={'mode': 'llm'}).status_code, 200)
                self.wait_job()
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(module.STATE['training']['completed_count'], 0)
        self.assertEqual(module.STATE['training']['judged_count'], 0)
        self.assertTrue(all(row['label'] is None for row in module.STATE['patents'][2:]))


if __name__ == '__main__':
    unittest.main()
