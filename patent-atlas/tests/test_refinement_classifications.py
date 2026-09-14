"""Classification feedback and query updates use test_app's isolated data directory."""
import copy
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_app as integration
from fastapi.testclient import TestClient

from refinement_classifications import classification_feedback

module = integration.module


def patent(identifier, code='H04L', label=None, source=None, score=None, family=''):
    return dict(id=identifier, title='battery electrolyte', abstract='', ipc=code, fterm='',
                label=label, label_source=source, score=score, family=family)


class ClassificationFeedbackTests(unittest.TestCase):
    def test_labels_take_precedence_and_predictions_remain_separate(self):
        rows = [patent('K', label='keep', source='human', score=.01, family='K'),
                patent('E', label='exclude', source='agent', score=.99, family='E'),
                patent('PK', score=.8, family='PK'), patent('PE', score=.2, family='PE'),
                patent('MID', score=.5), patent('HELD', source='agent', score=None)]
        before = copy.deepcopy(rows)
        result = classification_feedback(rows, ['F-term:5H029AM11'])
        item = result['candidates'][0]
        self.assertEqual([item[name + '_count'] for name in ('keep', 'exclude', 'predicted_keep', 'predicted_exclude')], [1, 1, 1, 1])
        self.assertEqual(item['keep_ids'], ['K'])
        self.assertEqual(item['exclude_ids'], ['E'])
        self.assertEqual(item['predicted_keep_ids'], ['PK'])
        self.assertFalse(item['recommended'])
        self.assertIn('NOT条件にしません', item['reason'])
        self.assertEqual(result['active_keys'], ['F-term:5H029AM11'])
        self.assertEqual(rows, before)

    def test_family_duplicates_do_not_manufacture_a_majority(self):
        rows = [patent('K1', label='keep', family='shared'), patent('K2', label='keep', family='shared'),
                patent('E1', label='exclude', family='other')]
        item = classification_feedback(rows)['candidates'][0]
        self.assertEqual((item['keep_count'], item['keep_family_count']), (2, 1))
        self.assertEqual((item['family_count'], item['duplicate_family_count']), (2, 1))
        self.assertFalse(item['recommended'])
        rows.append(patent('K3', label='keep', family='independent'))
        self.assertEqual(classification_feedback(rows)['recommended_keys'], ['IPC:H04L'])

    def test_duplicate_publications_prefer_human_label(self):
        rows = [patent('JP-1', label='exclude', source='agent'),
                patent('JP1', label='keep', source='human'),
                patent('JP 1', label='exclude', source='agent')]
        result = classification_feedback(rows)
        item = result['candidates'][0]
        self.assertEqual((item['keep_count'], item['exclude_count']), (1, 0))
        self.assertTrue(any('重複公報2件' in warning for warning in result['warnings']))

    def test_malformed_and_unknown_ipc_are_not_guessed_or_cross_mapped(self):
        rows = [patent('GOOD', 'Ｈ０４Ｌ (2026.01); H04L', label='keep'),
                patent('UNKNOWN', 'H99Z9999/999999', label='keep'),
                patent('PARTIAL', 'H01M10/0562;bad', label='keep'),
                patent('OTHER', 'Y02E60/10', label='keep'),
                patent('WRONG-TYPE', 12, label='keep')]
        result = classification_feedback(rows)
        self.assertEqual([item['key'] for item in result['candidates']], ['IPC:H04L'])
        self.assertEqual(result['candidates'][0]['keep_count'], 1)
        self.assertTrue(any('UNKNOWN' in warning for warning in result['warnings']))
        self.assertTrue(any('PARTIAL' in warning for warning in result['warnings']))
        self.assertTrue(any('OTHER' in warning for warning in result['warnings']))
        self.assertTrue(any('WRONG-TYPE' in warning for warning in result['warnings']))

    def test_prediction_only_is_visible_but_never_recommended(self):
        result = classification_feedback([patent('PK1', score=.9), patent('PK2', score=.95)])
        self.assertEqual(result['recommended_keys'], [])
        self.assertEqual(result['candidates'][0]['predicted_keep_count'], 2)
        self.assertTrue(any('ファミリーIDがなく' in warning for warning in result['warnings']))

    def test_nonfinite_invalid_and_undecided_scores_are_ignored(self):
        rows = [patent(str(index), score=score) for index, score in enumerate((float('nan'), float('inf'), -1, 2, True, '.9', None))]
        self.assertEqual(classification_feedback(rows)['candidates'], [])

    def test_only_explicit_metadata_is_counted_and_upper_ipc_is_supported(self):
        result = classification_feedback([patent('S', ['H', 'H04', 'H04L'], label='keep')])
        self.assertEqual(set(result['recommended_keys']), {'IPC:H', 'IPC:H04', 'IPC:H04L'})
        result = classification_feedback([patent('G', 'H04L1/00', label='keep')])
        self.assertEqual([item['code'] for item in result['candidates']], ['H04L1/00'])


class ClassificationRefinementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        self.before = copy.deepcopy(module.STATE)
        module.JOB['status'] = 'idle'
        candidates = [module.explorer.enrich(dict(kind=kind, code=code)) for kind, code in
                      [('IPC', 'H01M10/00'), ('F-term', '5H029AM11')]]
        module.STATE.update(keywords='battery', candidates=candidates,
                            selected=['IPC:H01M10/00', 'F-term:5H029AM11'], queries=[], classification_view=None,
                            patents=[patent('KEEP-1', label='keep', source='human', family='A'),
                                     patent('KEEP-2', label='keep', source='agent', family='B'),
                                     patent('DROP-1', 'G02B6/00', label='exclude', source='human', family='C')])
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        module.save()

    def tearDown(self):
        module.STATE.clear()
        module.STATE.update(self.before)
        module.save()

    def assert_atomic_rejection(self, body):
        before = copy.deepcopy(module.STATE)
        saved = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post('/api/query', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), saved)

    def test_feedback_is_read_only_and_exposes_active_and_recommended_keys(self):
        before = copy.deepcopy(module.STATE)
        saved = (module.DATA / 'workspace.json').read_bytes()
        result = self.client.get('/api/refinement').json()
        feedback = result['classifications']
        self.assertEqual(feedback['active_keys'], module.STATE['selected'])
        self.assertEqual(feedback['recommended_keys'], ['IPC:H04L'])
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), saved)

    def test_explicit_classifications_update_or_selection_and_detached_evidence(self):
        response = self.client.post('/api/query', json=dict(refine=True, include=[], exclude=[], classification_keys=['IPC:H04L', 'F-term:5H029AM11']))
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        item = state['queries'][-1]
        self.assertEqual(state['selected'], ['IPC:H04L', 'F-term:5H029AM11'])
        self.assertIn('IPC="H04L"', item['expression'])
        self.assertIn(' OR ', item['expression'])
        self.assertNotIn('H01M10/00', item['expression'])
        changes = item['classification_changes']
        self.assertEqual(changes['added_keys'], ['IPC:H04L'])
        self.assertEqual(changes['removed_keys'], ['IPC:H01M10/00'])
        self.assertEqual(changes['evidence'][0]['keep_ids'], ['KEEP-1', 'KEEP-2'])
        self.assertEqual(changes['evidence'][0]['keep_count'], 2)
        saved_query = copy.deepcopy(module.STATE['queries'][-1])
        candidate = next(row for row in module.STATE['candidates'] if row['key'] == 'IPC:H04L')
        candidate['origins'][-1]['keep_ids'].append('CHANGED')
        module.STATE['patents'][0]['label'] = 'exclude'
        self.assertEqual(module.STATE['queries'][-1], saved_query)

    def test_omitting_classification_keys_keeps_current_selection_and_pool(self):
        candidates, selected = copy.deepcopy(module.STATE['candidates']), list(module.STATE['selected'])
        response = self.client.post('/api/query', json={'refine': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.STATE['candidates'], candidates)
        self.assertEqual(module.STATE['selected'], selected)
        self.assertNotIn('classification_changes', module.STATE['queries'][-1])

    def test_classification_change_preserves_previous_boolean_conditions(self):
        module.STATE['queries'] = [dict(id='previous', keywords=['battery'], classifications=copy.deepcopy(module.STATE['candidates']), include_terms=['electrolyte'], exclude_terms=['camera'])]
        response = self.client.post('/api/query', json={'refine': True, 'classification_keys': ['IPC:H04L']})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['queries'][-1]
        self.assertEqual(item['include_terms'], ['electrolyte'])
        self.assertEqual(item['exclude_terms'], ['camera'])
        self.assertEqual(item['removed_terms'], [])

    def test_agent_final_query_adds_reviewed_ipc_and_preserves_existing_conditions(self):
        original_keys = list(module.STATE['selected'])
        module.STATE['patents'][2]['ipc'] = 'H04L'
        module.STATE['patents'].append(patent('NEW-KEEP', 'H04L1/00', family='D'))
        module.STATE['queries'] = [dict(id='previous', keywords=['battery'],
                                        classifications=copy.deepcopy(module.STATE['candidates']),
                                        include_terms=['electrolyte'], exclude_terms=[])]
        previous = copy.deepcopy(module.STATE['queries'][0])
        answers = [dict(selected=original_keys, reason='既存分類を維持'),
                   dict(decisions=[dict(id='NEW-KEEP', decision='keep', confidence=.95, relevance=.9,
                                        reason='要約の電池電解質が判断基準に一致')])]
        with patch.dict(module.SETTINGS, {'provider': 'local'}), patch.object(module, 'complete', side_effect=answers):
            response = self.client.post('/api/agent', json={'criteria': 'battery electrolyte', 'max_items': 1})
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 10
            while module.JOB['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(module.JOB['status'], 'done', module.JOB)
        state = self.client.get('/api/state').json()
        item = state['queries'][-1]
        self.assertEqual(state['queries'][0], previous)
        self.assertEqual(item['include_terms'], ['electrolyte'])
        self.assertEqual(item['exclude_terms'], [])
        self.assertEqual(item['removed_terms'], [])
        self.assertTrue(set(original_keys) <= set(state['selected']))
        self.assertTrue({'IPC:H04L', 'IPC:H04L1/00'} <= set(state['selected']))
        self.assertIn('IPC="H01M10/00"', item['expression'])
        self.assertIn('IPC="H04L1/00"', item['expression'])
        self.assertIn('TEXT="electrolyte"', item['expression'])
        self.assertNotIn('NOT', item['expression'])
        changes = item['classification_changes']
        self.assertEqual(set(changes['added_keys']), {'IPC:H04L', 'IPC:H04L1/00'})
        self.assertEqual(changes['removed_keys'], [])
        evidence = {row['key']: row for row in changes['evidence']}
        self.assertEqual(evidence['IPC:H04L1/00']['keep_ids'], ['NEW-KEEP'])
        self.assertEqual(evidence['IPC:H04L1/00']['evidence'][0]['label_source'], 'agent')
        self.assertEqual(evidence['IPC:H04L']['exclude_ids'], ['DROP-1'])
        self.assertEqual(evidence['IPC:H04L1/00']['type'], 'refinement')

    def test_refinement_form_keeps_conditions_after_only_manual_selection_changes(self):
        module.STATE['queries'] = [dict(id='previous', keywords=['battery'], classifications=copy.deepcopy(module.STATE['candidates']), include_terms=['electrolyte'], exclude_terms=['camera'])]
        module.STATE['selected'] = ['F-term:5H029AM11']
        response = self.client.get('/api/refinement')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['active_include'], ['electrolyte'])
        self.assertEqual(result['active_exclude'], ['camera'])
        module.STATE['keywords'] = 'different topic'
        result = self.client.get('/api/refinement').json()
        self.assertEqual(result['active_include'], [])
        self.assertEqual(result['active_exclude'], [])

    def test_invalid_and_stale_inputs_leave_memory_and_saved_file_unchanged(self):
        for body in [dict(refine=True, classification_keys=['IPC:H99Z9999/999999']),
                     dict(refine=True, classification_keys=['H04L']),
                     dict(refine=True, classification_keys=None),
                     dict(refine=True, classification_keys='IPC:H04L'),
                     dict(refine=False, classification_keys=['IPC:H04L']),
                     dict(refine=True, classification_keys=['IPC:H04L'], include=['not-a-candidate']),
                     dict(refine='false')]:
            with self.subTest(body=body):
                self.assert_atomic_rejection(body)

    def test_no_base_condition_and_candidate_capacity_errors_are_atomic(self):
        module.STATE['keywords'] = ''
        module.save()
        self.assert_atomic_rejection(dict(refine=True, classification_keys=[]))
        with patch.object(module.explorer, 'merge_candidates', side_effect=ValueError('候補上限')):
            self.assert_atomic_rejection(dict(refine=True, classification_keys=['IPC:H04L']))

    def test_save_failure_rolls_back_state_and_pending_file(self):
        before = copy.deepcopy(module.STATE)
        saved = (module.DATA / 'workspace.json').read_bytes()
        pending = module.DATA / 'workspace.tmp'
        self.assertFalse(pending.exists())
        def fail_save():
            pending.write_text('partial file', encoding='utf-8')
            raise OSError('simulated disk error')
        with patch.object(module, 'save', side_effect=fail_save):
            response = TestClient(module.app, raise_server_exceptions=False).post('/api/query', json={'refine': True, 'classification_keys': ['IPC:H04L']})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), saved)
        self.assertFalse(pending.exists())


if __name__ == '__main__':
    unittest.main()
