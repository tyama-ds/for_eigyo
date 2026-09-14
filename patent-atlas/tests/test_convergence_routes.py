"""Convergence API tests use only test_app's isolated temporary workspace."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
from discovery_routes import new_discovery
from orchestration_engine import new_orchestration


module = integration.module


def patent(identifier):
    return dict(id=identifier, title='固体電池の界面製造方法 ' + identifier,
                abstract='固体電解質と電極の界面層を形成する。', ipc=['H01M10/0562'],
                fterm=[], fi=[], applicant='Example', year='2025', family='',
                label=None, label_source=None, label_reason='', score=None)


class ConvergenceRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        self.assertNotEqual(module.JOB.get('status'), 'running')
        module.JOB.update(status='idle', kind=None, error=None)
        module.STOP.clear()
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        candidate = module.explorer.enrich(dict(kind='IPC', code='H01M10/058'))
        module.STATE.clear()
        module.STATE.update(keywords='全固体電池', candidates=[candidate], selected=[candidate['key']],
                            patents=[patent('JP2024000001A'), patent('JP2024000002A')],
                            clusters=[], queries=[], training=None, agent_log=[], demo=False,
                            import_info=None, classification_view=None, discovery=new_discovery(),
                            orchestration=new_orchestration())
        self.query = module.make_query()
        module.save()

    def post(self, action, body=None):
        response = self.client.post('/api/convergence/' + action, json=body or {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('convergence', response.json())
        return response.json()

    def records(self):
        return module.STATE.get('convergence_tracking', {}).get('records', [])

    def upload(self, identifiers, merge=False):
        source = '公報番号,発明の名称,要約,IPC\n' + ''.join(
            f'{identifier},固体電池の界面製造方法 {identifier},固体電解質と電極の界面層を形成する。,H01M10/0562\n'
            for identifier in identifiers)
        response = self.client.post('/api/upload', params={'merge': str(merge).lower()},
                                    content=source.encode('utf-8-sig'),
                                    headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 200, response.text)
        return copy.deepcopy(module.STATE['search_result'])

    def defer(self, index=0):
        module.STATE['patents'][index].update(label=None, label_source='agent',
                                              label_reason='界面処理の詳細が足りないため保留',
                                              agent_confidence=.6, llm_decision='unsure', score=.4)
        return module.STATE['patents'][index]['id']

    def assert_rejected_unchanged(self, action, body, status=400):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post('/api/convergence/' + action, json=body)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)

    def test_get_does_not_invent_history_and_baseline_cannot_complete(self):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        for _ in range(2):
            response = self.client.get('/api/state')
            self.assertEqual(response.status_code, 200)
            self.assertIn('convergence', response.json())
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
        self.assertEqual(self.records(), [])
        self.post('record', {'total_hits': 2, 'query_id': self.query['id']})
        self.post('record', {'total_hits': 2, 'query_id': self.query['id']})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]['scope'], 'workspace_baseline')
        self.assert_rejected_unchanged('complete', {})

    def test_csv_snapshot_counts_incoming_unique_publications_even_when_appending(self):
        observation = self.upload(['JP2024000002A', 'JP2024000003A', 'JP2024000003A'], merge=True)
        self.assertEqual(len(module.STATE['patents']), 3)
        self.assertEqual(observation['uploaded_count'], 2)
        self.assertEqual(len(set(observation['publication_keys'])), 2)
        self.assertEqual(observation['scope'], 'csv')
        self.post('record', {'observation_id': observation['id'], 'total_hits': 2,
                             'query_id': self.query['id']})
        self.assertEqual(len(self.records()), 1)

    def test_record_is_idempotent_but_a_new_identical_csv_creates_a_new_observation(self):
        incoming = ['JP2024000001A', 'JP2024000002A']
        first = self.upload(incoming)
        body = dict(observation_id=first['id'], total_hits=100, query_id=self.query['id'])
        self.post('record', body)
        self.post('record', {**body, 'total_hits': 101})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]['total_hits'], 101)
        second = self.upload(incoming, merge=True)
        self.assertNotEqual(first['id'], second['id'])
        self.post('record', {**body, 'observation_id': second['id']})
        self.assertEqual(len(self.records()), 2)

    def test_invalid_total_unknown_query_and_stale_observation_preserve_state(self):
        first = self.upload(['JP2024000001A', 'JP2024000002A'])
        body = dict(observation_id=first['id'], total_hits=2, query_id=self.query['id'])
        for invalid in (-1, 1, True, 2.5, '2'):
            with self.subTest(total=invalid):
                self.assert_rejected_unchanged('record', {**body, 'total_hits': invalid})
        self.assert_rejected_unchanged('record', {**body, 'query_id': 'missing-query'})
        self.upload(['JP2024000001A', 'JP2024000002A'], merge=True)
        self.assert_rejected_unchanged('record', body, status=409)

    def test_missing_query_and_unresolved_patents_do_not_allow_completion(self):
        for _ in range(5):
            observation = self.upload(['JP2024000001A', 'JP2024000002A'])
            self.defer()
            module.STATE['patents'][1].update(label='keep', label_source='human')
            self.post('record', dict(observation_id=observation['id'], total_hits=2, query_id=None))
        self.assertEqual(len(self.records()), 5)
        self.assert_rejected_unchanged('complete', {})

    def test_human_review_only_accepts_deferred_and_invalidates_training_without_calling_llm(self):
        identifier = self.defer()
        self.assert_rejected_unchanged('review', {'id': identifier, 'label': 'keep',
                                                 'expected_reason': '以前の理由', 'expected_confidence': .6}, status=409)
        module.STATE['training'] = {'mode': 'llm', 'completed_count': 2}
        module.STATE['patents'][1]['score'] = .9
        with patch.object(module, 'complete') as mocked:
            self.post('review', {'id': identifier, 'label': 'keep'})
        mocked.assert_not_called()
        reviewed = module.STATE['patents'][0]
        self.assertEqual((reviewed['label'], reviewed['label_source']), ('keep', 'human'))
        self.assertIsNone(module.STATE['training'])
        self.assertTrue(all(row['score'] is None for row in module.STATE['patents']))
        self.assert_rejected_unchanged('review', {'id': identifier, 'label': 'exclude'}, status=409)
        self.assert_rejected_unchanged('review', {'id': module.STATE['patents'][1]['id'], 'label': 'keep'}, status=409)

    def test_review_updates_current_record_without_extra_points_and_reset_preserves_history(self):
        observation = self.upload(['JP2024000001A', 'JP2024000002A'])
        identifier = self.defer()
        self.post('record', dict(observation_id=observation['id'], total_hits=2, query_id=self.query['id']))
        before = copy.deepcopy(self.records()[0])
        self.post('review', {'id': identifier, 'label': 'exclude'})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((before['deferred_count'], self.records()[0]['deferred_count']), (1, 0))
        self.assertEqual((before['exclude_count'], self.records()[0]['exclude_count']), (0, 1))
        history = copy.deepcopy(module.STATE['convergence_tracking'])
        response = self.client.post('/api/orchestration/reset', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.STATE['convergence_tracking'], history)
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['convergence_tracking'], history)

    def test_four_complete_stable_results_allow_completion_until_judgments_change(self):
        for _ in range(4):
            observation = self.upload(['JP2024000001A', 'JP2024000002A'])
            for row in module.STATE['patents']:
                row.update(label='keep', label_source='human')
            report = self.post('record', dict(observation_id=observation['id'], total_hits=2,
                                               query_id=self.query['id']))['convergence']
        self.assertEqual(report['stable_streak'], 3)
        self.assertTrue(report['eligible_to_complete'], report['blockers'])
        result = self.post('complete')['convergence']
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(module.STATE['convergence_tracking']['completed_at'])
        response = self.client.post('/api/labels', json={'ids': [module.STATE['patents'][0]['id']], 'label': 'exclude'})
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()['convergence']
        self.assertFalse(report['eligible_to_complete'])
        self.assertNotEqual(report['status'], 'completed')
        self.assertTrue(any('判定が変わりました' in value for value in report['blockers']))
        self.assert_rejected_unchanged('complete', {})

    def test_orchestrator_accepts_new_identical_csv_and_attributes_record_to_executed_query(self):
        incoming = ['JP2024000001A', 'JP2024000002A']
        first = self.upload(incoming)
        self.assertEqual(first['query_id'], self.query['id'])

        def cycle(action, body):
            response = self.client.post('/api/orchestration/' + action, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with module.LOCK:
                    if module.JOB['status'] != 'running':
                        break
                time.sleep(.01)
            self.assertEqual(module.JOB['status'], 'done', module.JOB)
            self.assertEqual(module.STATE['orchestration']['status'], 'waiting_csv')

        with patch.object(module, 'complete') as mocked:
            cycle('start', {'judge': False, 'review': False})
            self.assertEqual(len(self.records()), 1)
            self.assertEqual(self.records()[0]['query_id'], self.query['id'])
            next_query_id = module.STATE['queries'][-1]['id']
            self.assertNotEqual(next_query_id, self.query['id'])
            second = self.upload(incoming, merge=True)
            self.assertEqual(second['query_id'], next_query_id)
            self.assertNotEqual(second['id'], first['id'])
            cycle('resume', {})
        mocked.assert_not_called()
        self.assertEqual(module.STATE['orchestration']['cycle'], 2)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[-1]['query_id'], next_query_id)
        self.assertNotEqual(self.records()[-1]['query_id'], module.STATE['queries'][-1]['id'])

    def test_busy_mutations_and_save_failures_are_atomic(self):
        identifier = self.defer()
        body = {'total_hits': 2, 'query_id': self.query['id']}
        module.JOB.update(status='running', kind='training')
        for action, payload in [('record', body), ('review', {'id': identifier, 'label': 'keep'}), ('complete', {})]:
            with self.subTest(action=action):
                self.assert_rejected_unchanged(action, payload, status=409)
        module.JOB.update(status='idle', kind=None)
        for action, payload in [('record', body), ('review', {'id': identifier, 'label': 'keep'})]:
            with self.subTest(action=action), patch.object(module, 'save', side_effect=OSError('test write failure')):
                self.assert_rejected_unchanged(action, payload)


if __name__ == '__main__':
    unittest.main()
