"""Discovery API regressions using test_app's isolated temporary data directory."""
import copy
import csv
import io
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
import discovery_routes


module = integration.module


def patent(identifier, ipc='H01M10/0562', family='family-a', **extra):
    return dict(id=identifier, title='Solid-state battery interface coating',
                abstract='Ceramic sintering creates a zirconate layer.', ipc=ipc, fterm='',
                family=family, year='2024', applicant='Example', label=None,
                label_source=None, label_reason='', score=None, **extra)


def csv_bytes(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=['id', 'title', 'abstract', 'ipc', 'fterm', 'family'], extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


class DiscoveryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        self.assertNotEqual(module.JOB.get('status'), 'running')
        module.JOB.update(status='idle', error=None)
        module.STOP.clear()
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        candidate = module.explorer.enrich({'kind': 'IPC', 'code': 'H01M10/058'})
        module.STATE.clear()
        module.STATE.update(keywords='manual topic', candidates=[candidate], selected=[candidate['key']],
                            patents=[patent('MANUAL-1', ipc='H01M10/058', family='manual-family')],
                            clusters=[], queries=[{'id': 'query-original', 'expression': 'manual expression'}],
                            training=None, agent_log=[], demo=False, import_info=None, classification_view=None,
                            discovery=discovery_routes.new_discovery())
        module.save()

    def manual_snapshot(self):
        return copy.deepcopy({k: v for k, v in module.STATE.items() if k != 'discovery'})

    def plan(self, **extra):
        response = self.client.post('/api/discovery/plan', json={'keywords': '全固体電池', **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['discovery']

    def import_rows(self, rows):
        response = self.client.post('/api/discovery/import', content=csv_bytes(rows), headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['discovery']

    def wait_job(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            # The worker changes status and finishes its atomic save under LOCK.
            with module.LOCK:
                if module.JOB['status'] != 'running':
                    return
            time.sleep(.01)
        self.assertNotEqual(module.JOB['status'], 'running')

    def run_mocked(self, search, **limits):
        module.SETTINGS.update(ops_key='mock-key', ops_secret='mock-secret')
        with patch.object(discovery_routes, 'search_ops', side_effect=search) as mocked, \
                patch.object(module.STOP, 'wait', return_value=False):
            response = self.client.post('/api/discovery/run', json=limits)
            self.assertEqual(response.status_code, 200, response.text)
            self.wait_job()
            calls = list(mocked.call_args_list)
        return module.STATE['discovery'], calls

    def test_plan_and_iterative_csv_preserve_manual_state_and_deduplicate(self):
        manual = self.manual_snapshot()
        plan = self.plan()
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual({f['id'] for f in plan['plan']['facets']},
                         {'device', 'material', 'process', 'interface', 'performance', 'application'})
        first = self.import_rows([patent('EP-100-A1'), patent('EP-100-A1')])
        self.assertEqual(first['document_count'], 1)
        self.assertEqual(first['analysis']['counts']['new_documents'], 1)
        second = self.import_rows([patent('EP100A1'), patent('EP200A1', ipc='C04B35/64', family='family-b')])
        self.assertEqual(second['document_count'], 2)
        self.assertEqual(second['analysis']['counts']['new_documents'], 1)
        self.assertEqual(second['analysis']['counts']['new_classifications'], 1)
        third = self.import_rows([patent('EP200A1', ipc='C04B35/64', family='family-b')])
        self.assertEqual(third['analysis']['counts']['new_documents'], 0)
        self.assertEqual(third['analysis']['counts']['new_classifications'], 0)
        self.assertEqual(len(third['rounds']), 3)
        self.assertNotIn('patents', third)
        self.assertEqual(self.manual_snapshot(), manual)

    def test_manual_human_labels_refresh_and_csv_cannot_overwrite_them(self):
        self.plan()
        self.assertEqual(self.client.post('/api/discovery/analyze').status_code, 200)
        result = self.client.post('/api/labels', json={'ids': ['MANUAL-1'], 'label': 'keep'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/api/discovery/analyze').status_code, 200)
        row = module.STATE['discovery']['patents'][0]
        self.assertEqual((row['label'], row['label_source']), ('keep', 'human'))
        result = self.import_rows([patent('MANUAL-1', ipc='H01M10/058', family='manual-family')])
        self.assertEqual(result['analysis']['recommendations'][0]['relevance_status'], 'human_supported')
        self.client.post('/api/labels', json={'ids': ['MANUAL-1'], 'label': 'exclude'})
        self.client.post('/api/discovery/analyze')
        self.assertEqual(module.STATE['discovery']['analysis']['recommendations'], [])
        self.assertEqual(module.STATE['discovery']['analysis']['counts']['excluded'], 1)
        self.client.post('/api/labels', json={'ids': ['MANUAL-1'], 'label': None})
        self.client.post('/api/discovery/analyze')
        self.assertEqual(module.STATE['discovery']['analysis']['recommendations'][0]['relevance_status'], 'review_pending')

    def test_adopt_retains_evidence_without_selecting_or_rewriting_query(self):
        self.plan()
        self.import_rows([patent('EP200A1', ipc='C04B35/64')])
        before = copy.deepcopy(module.STATE)
        response = self.client.post('/api/discovery/adopt', json={'keys': ['IPC:C04B35/64']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.STATE['selected'], before['selected'])
        self.assertEqual(module.STATE['queries'], before['queries'])
        self.assertEqual(module.STATE['patents'], before['patents'])
        candidate = next(c for c in module.STATE['candidates'] if c['key'] == 'IPC:C04B35/64')
        origin = next(o for o in candidate['origins'] if o.get('discovery_id'))
        self.assertEqual(origin['patent_ids'], ['EP200A1'])
        self.assertEqual(origin['evidence_status'], 'patent_metadata')
        self.assertEqual(module.STATE['classification_view']['keys'], ['IPC:C04B35/64'])

    def test_rejected_inputs_are_atomic(self):
        self.plan()
        before, disk = copy.deepcopy(module.STATE), (module.DATA / 'workspace.json').read_bytes()
        for path, body in [('/api/discovery/adopt', {'keys': ['IPC:UNKNOWN']}),
                           ('/api/discovery/plan', {'keywords': 'battery', 'english_terms': 'battery" or pa=example'}),
                           ('/api/discovery/run', {'max_queries': 13}),
                           ('/api/discovery/run', {'max_rounds': True}),
                           ('/api/discovery/run', {'per_query': 0})]:
            with self.subTest(path=path, body=body):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(module.STATE, before)
                self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_ops_unconfigured_stays_offline(self):
        self.plan()
        before = copy.deepcopy(module.STATE)
        with patch.object(discovery_routes, 'search_ops') as search:
            response = self.client.post('/api/discovery/run', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('未設定', response.text)
        self.assertEqual(module.STATE, before)
        search.assert_not_called()

    def test_llm_manual_and_model_english_coexist_and_public_feedback_is_saved(self):
        manual = self.manual_snapshot()
        with patch.object(module, 'complete', return_value={
                'english_terms': ['autonomous navigation'],
                'facets': [{'id': 'interface', 'terms': ['通信接続'], 'english_terms': ['vehicle communication']}]}):
            response = self.client.post('/api/discovery/plan', json={
                'keywords': '自動運転', 'english_terms': 'self driving', 'use_llm': True})
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()['discovery']['plan']
        self.assertEqual(plan['english_terms'], ['self driving', 'autonomous navigation'])
        self.assertEqual(plan['llm_proposal']['english_terms'], ['autonomous navigation'])
        self.assertEqual(plan['llm_proposal']['facets'][0]['english_terms'], ['vehicle communication'])
        self.assertTrue(all('autonomous navigation' in query['query'] for query in plan['queued_queries']))
        disk = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(disk['state']['discovery']['plan']['llm_proposal'], plan['llm_proposal'])
        self.assertEqual(self.manual_snapshot(), manual)

    def test_wrong_llm_json_keeps_previous_plan_and_saved_workspace(self):
        self.plan()
        before, disk = copy.deepcopy(module.STATE), (module.DATA / 'workspace.json').read_bytes()
        for reply in (None, [], {}, {'candidates': []}, {'english_terms': 'autonomous driving'},
                      {'facets': [{'id': 'process', 'terms': [123]}]}):
            with self.subTest(reply=reply), patch.object(module, 'complete', return_value=reply):
                response = self.client.post('/api/discovery/plan', json={'keywords': '自動運転', 'use_llm': True})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(module.STATE, before)
            self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_invalid_manual_vocabulary_is_rejected_before_model_call(self):
        with patch.object(module, 'complete') as mocked:
            response = self.client.post('/api/discovery/plan', json={
                'keywords': '自動運転', 'english_terms': 'bad" query', 'use_llm': True})
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    def test_ops_respects_round_query_budget_and_retains_every_success(self):
        self.plan()
        before = self.manual_snapshot()
        counter = 0
        def search(query, settings, limit):
            nonlocal counter
            counter += 1
            code = 'H01M10/0562' if counter % 2 else 'C04B35/64'
            return {'patents': [patent(f'EP{counter}A1', ipc=code, family=f'family-{counter}')],
                    'total': 100, 'truncated': True, 'query': query}
        result, calls = self.run_mocked(search, max_queries=4, max_rounds=2, per_query=3)
        self.assertEqual(module.JOB['status'], 'done')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(result['rounds']), 2)
        self.assertEqual(len(result['patents']), 4)
        attempted = [q for r in result['rounds'] for q in r['queries']]
        self.assertEqual(len({q['id'] for q in attempted}), 4)
        self.assertTrue(all(q['truncated'] and q['total'] == 100 for q in attempted))
        self.assertTrue(all(call.args[2] == 3 for call in calls))
        self.assertTrue(any('ic =' in call.args[0] for call in calls[2:]),
                        'The second round must search a classification discovered in the first round.')
        self.assertEqual(self.manual_snapshot(), before)

    def test_stopping_retains_successfully_downloaded_evidence(self):
        self.plan()
        def stop_after_first(query, settings, limit):
            module.STOP.set()
            return {'patents': [patent('EP1A1')], 'total': 1, 'truncated': False}
        result, calls = self.run_mocked(stop_after_first, max_queries=4, max_rounds=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(module.JOB['status'], 'cancelled')
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(result['patents']), 1)
        self.assertEqual(result['analysis']['counts']['documents'], 1)
        self.assertEqual(sum(len(r['queries']) for r in result['rounds']), 1)

    def test_provider_failure_retains_partial_analysis_and_query_provenance(self):
        self.plan()
        counter = 0
        def fail_second(query, settings, limit):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise ValueError('OPS利用上限のため停止')
            return {'patents': [patent('EP1A1')], 'total': 1, 'truncated': False}
        result, calls = self.run_mocked(fail_second, max_queries=4, max_rounds=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(module.JOB['status'], 'error')
        self.assertEqual(result['status'], 'error')
        self.assertIn('利用上限', result['last_error'])
        self.assertEqual(len(result['patents']), 1)
        self.assertIsNotNone(result['analysis'], 'Successful downloads must be analyzed even if a later request fails.')
        self.assertEqual(result['analysis']['counts']['documents'], 1)
        self.assertEqual(sum(len(r['queries']) for r in result['rounds']), 1)

    def test_new_plan_archives_evidence_and_exports_original_snapshot(self):
        self.plan()
        self.import_rows([patent('EP1A1')])
        old = copy.deepcopy(module.STATE['discovery'])
        second = self.plan(keywords='固体電解質')
        self.assertNotEqual(second['id'], old['id'])
        self.assertEqual(second['document_count'], 0)
        self.assertEqual(len(second['archives']), 1)
        archive_id = second['archives'][0]['id']
        archived = self.client.get('/api/discovery/export', params={'archive_id': archive_id})
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()['patents'], old['patents'])
        self.assertEqual(archived.json()['plan'], old['plan'])
        self.assertEqual(self.client.get('/api/discovery/export', params={'archive_id': '../workspace'}).status_code, 404)

    def test_ops_secrets_remain_out_of_public_state_and_saved_exports(self):
        response = self.client.post('/api/settings', json={'ops_key': 'ops-key-sensitive', 'ops_secret': 'ops-secret-sensitive'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['settings']['ops_key_set'])
        self.assertTrue(response.json()['settings']['ops_secret_set'])
        self.plan()
        self.import_rows([patent('EP1A1')])
        for output in [response.text, self.client.get('/api/state').text,
                       self.client.get('/api/discovery/export').text,
                       (module.DATA / 'workspace.json').read_text(encoding='utf-8')]:
            self.assertNotIn('ops-key-sensitive', output)
            self.assertNotIn('ops-secret-sensitive', output)
        self.client.post('/api/settings', json={'ops_key': '', 'ops_secret': ''})
        self.assertEqual(module.SETTINGS['ops_key'], 'ops-key-sensitive')
        self.client.post('/api/settings', json={'clear_ops': True})
        self.assertEqual(module.SETTINGS['ops_key'], '')
        self.assertEqual(module.SETTINGS['ops_secret'], '')


if __name__ == '__main__':
    unittest.main()
