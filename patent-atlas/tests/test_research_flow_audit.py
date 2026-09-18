"""Cross-step audit: saved seed references, imported CSVs and Boolean refinement.

Uses test_app's temporary workspace and mocks network/model calls. These tests
never read the user's CSV or call the user's model.
"""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_app as integration
from discovery_routes import new_discovery
from orchestration_engine import new_orchestration
from projection_engine import compute_evaluation
from query_formats import export_query
from research_routes import new_workbench


module = integration.module


def patent(identifier='JP2024000001A'):
    return dict(id=identifier, title='車両の安全制御', abstract='車両の運転状態を制御する。',
                ipc=['B60W30/00'], fi=[], fterm=[], cpc=[], applicant='Example', year='2024',
                label=None, label_source=None, label_reason='', score=None)


class ResearchFlowAuditTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        self.assertNotEqual(module.JOB.get('status'), 'running')
        self.client = TestClient(module.app)
        module.JOB.update(status='idle', kind=None, error=None)
        module.STOP.clear()
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        module.SETTINGS.update(provider='local', model='unchanged-audit-model',
                               base_url='http://127.0.0.1:1234/v1', proxy='http://audit.invalid:8080')
        self.settings = copy.deepcopy(module.SETTINGS)
        candidate = module.explorer.enrich(dict(kind='IPC', code='B60'))
        module.STATE.clear()
        module.STATE.update(keywords='車両', candidates=[candidate], selected=[], patents=[patent()],
                            queries=[], clusters=[], training=None, agent_log=[], demo=False,
                            import_info=None, classification_view=None, candidate_proposal=None,
                            discovery=new_discovery(), orchestration=new_orchestration(),
                            research_workbench=new_workbench())
        for mocking in (patch.object(module, 'candidate_list', return_value=[candidate]),
                        patch.object(module, 'map_patents', return_value=[])):
            mocking.start()
            self.addCleanup(mocking.stop)
        module.save()

    def post(self, path, body):
        response = self.client.post('/api/' + path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.SETTINGS, self.settings)
        return response.json()

    def make_plan(self, **changes):
        brief = dict(entry_mode='target', purpose='車両の周辺技術を調査する', goal='車両の制御と運転方法',
                     keywords='車両', user_aspects=['制御', '除外: 玩具'], target_ids=['JP2024000001A'])
        brief.update(changes)
        with patch('research_strategy.complete') as llm:
            data = self.post('research/plan', {'brief': brief, 'use_llm': False})
        llm.assert_not_called()
        return data['research_workbench']['plan']

    def replace_csv(self, identifier='JP2024999999A'):
        csv = ('公報番号,発明の名称,要約,IPC\n'
               f'{identifier},車両の安全制御,車両の運転状態を制御する。,B60W30/00\n').encode('utf-8-sig')
        response = self.client.post('/api/upload', content=csv, headers={'content-type': 'text/csv'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.SETTINGS, self.settings)
        return response.json()

    def adopt_plan(self, plan):
        concepts = [dict(id=c['id'], role=c['role'], terms=list(c['terms'])) for c in plan['concepts']]
        concepts[0]['terms'] = ['制御', 'control']
        payload = dict(plan_id=plan['id'], concepts=concepts, classification_keys=['IPC:B60'])
        preview = self.post('research/preview', payload)
        result = self.post('research/apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        return result['queries'][-1], payload

    def test_seed_survives_replaced_search_result_and_missing_target_stays_visible(self):
        plan = self.make_plan()
        initial_seed = copy.deepcopy(module.STATE['research_workbench']['target_patents'][0])
        query, _ = self.adopt_plan(plan)
        result = self.replace_csv()
        self.assertEqual([p['id'] for p in result['patents']], ['JP2024999999A'])
        self.assertEqual(result['research_workbench']['target_patents'], [initial_seed])
        self.assertEqual(result['search_result']['query_id'], query['id'])
        evaluation = self.client.get('/api/map/evaluation')
        self.assertEqual(evaluation.status_code, 200, evaluation.text)
        self.assertEqual(evaluation.json()['target_found'], 0)
        self.assertEqual(evaluation.json()['missing_target_ids'], ['JP2024000001A'])
        # The saved seed remains usable despite being absent from the new results.
        replanned = self.post('research/plan', {'use_llm': False})['research_workbench']['plan']
        self.assertEqual(replanned['source_ids'], ['JP2024000001A'])
        self.assertEqual(replanned['sources'][0]['abstract'], initial_seed['abstract'])
        self.assertEqual(replanned['sources'][0]['ipc'], initial_seed['ipc'])
        self.assertEqual(replanned['brief']['purpose'], plan['brief']['purpose'])
        self.assertEqual(len(module.STATE['queries']), 1)

    def test_manual_add_preserves_every_current_seed_before_first_plan(self):
        # A selected CSV row has not yet been persisted by a plan. Adding a
        # second target must preserve both source records across replacement.
        data = self.post('research/targets/add', dict(
            id='JP2024000002A', title='車両の操舵', abstract='車両を操舵する。', ipc='B60W30/00',
            target_ids=['JP2024000001A']))
        wb = data['research_workbench']
        self.assertEqual(set(wb['brief']['target_ids']), {'JP2024000001A', 'JP2024000002A'})
        self.assertEqual({p['id'] for p in wb['target_patents']}, {'JP2024000001A', 'JP2024000002A'})
        self.replace_csv()
        replanned = self.post('research/plan', {'brief': {'purpose': '二つのターゲットの周辺調査'},
                                              'use_llm': False})['research_workbench']['plan']
        self.assertEqual(set(replanned['source_ids']), {'JP2024000001A', 'JP2024000002A'})

    def test_discovery_without_targets_retains_saved_reference_and_allows_reselecting_it(self):
        self.make_plan()
        reference = copy.deepcopy(module.STATE['research_workbench']['target_patents'][0])
        self.replace_csv()
        result_rows = copy.deepcopy(module.STATE['patents'])
        discover = self.make_plan(entry_mode='discover', target_ids=[])
        self.assertEqual(discover['source_ids'], [])
        self.assertEqual(module.STATE['research_workbench']['brief']['target_ids'], [])
        self.assertEqual(module.STATE['research_workbench']['target_patents'], [reference])
        self.assertEqual(module.STATE['patents'], result_rows)
        self.assertEqual([p['id'] for p in module.STATE['patents']], ['JP2024999999A'])
        selected = self.make_plan(entry_mode='target', target_ids=[reference['id']])
        self.assertEqual(selected['source_ids'], [reference['id']])
        self.assertEqual(selected['sources'][0]['abstract'], reference['abstract'])
        self.assertEqual(selected['sources'][0]['ipc'], reference['ipc'])
        self.assertEqual(module.STATE['patents'], result_rows)

    def test_adding_target_retains_unchecked_reference_without_selecting_or_reimporting_it(self):
        self.make_plan()
        reference = copy.deepcopy(module.STATE['research_workbench']['target_patents'][0])
        self.replace_csv()
        result = self.post('research/targets/add', dict(id='JP2024000002A', title='新しい制御方法',
                           abstract='新しい対象の制御。', ipc='B60W30/00', target_ids=[]))
        self.assertEqual(result['research_workbench']['brief']['target_ids'], ['JP2024000002A'])
        refs = {p['id']: p for p in result['research_workbench']['target_patents']}
        self.assertEqual(refs[reference['id']], reference)
        self.assertIn('JP2024000002A', refs)
        self.assertNotIn(reference['id'], [p['id'] for p in result['patents']])
        self.assertIn('JP2024999999A', [p['id'] for p in result['patents']])

    def test_reference_limit_rejects_plan_and_manual_add_atomically_without_pruning(self):
        module.STATE['research_workbench']['target_patents'] = [patent(f'ARCHIVE{i:05}') for i in range(5000)]
        module.save()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        plan_body = {'use_llm': False, 'brief': dict(entry_mode='target', purpose='周辺技術',
                         goal='車両制御', keywords='車両', target_ids=['JP2024000001A'])}
        add_body = dict(id='JP2024000002A', title='新しい制御方法', abstract='制御する。', ipc='B60W30/00', target_ids=[])
        for endpoint, body in [('plan', plan_body), ('targets/add', add_body)]:
            with self.subTest(endpoint=endpoint):
                response = self.client.post('/api/research/' + endpoint, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('5,000', response.json()['detail'])
                self.assertEqual(module.STATE, before)
                self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
                self.assertEqual(module.SETTINGS, self.settings)
        # At capacity, selecting an already saved seed updates it without
        # silently dropping the other 4,999 references or adding them to CSV.
        plan_body['brief']['target_ids'] = ['ARCHIVE00000']
        accepted = self.post('research/plan', plan_body)
        self.assertEqual(len(accepted['research_workbench']['target_patents']), 5000)
        self.assertEqual(accepted['research_workbench']['brief']['target_ids'], ['ARCHIVE00000'])
        self.assertEqual([p['id'] for p in accepted['patents']], ['JP2024000001A'])

    def test_alias_publication_in_csv_counts_as_recovered_target(self):
        self.make_plan()
        self.replace_csv('JP-2024-000001-A')
        evaluation = self.client.get('/api/map/evaluation')
        self.assertEqual(evaluation.status_code, 200, evaluation.text)
        self.assertEqual(evaluation.json()['target_count'], 1)
        self.assertEqual(evaluation.json()['target_found'], 1)
        self.assertEqual(evaluation.json()['missing_target_ids'], [])

    def test_sparse_replacement_csv_does_not_erase_saved_seed_abstract_or_classifications(self):
        self.make_plan()
        reference = copy.deepcopy(module.STATE['research_workbench']['target_patents'][0])
        csv = '公報番号,発明の名称,要約\nJP2024000001A,車両の安全制御,\n'.encode('utf-8-sig')
        response = self.client.post('/api/upload', content=csv, headers={'content-type': 'text/csv'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(module.STATE['patents'][0]['abstract'])
        plan = self.post('research/plan', {'use_llm': False})['research_workbench']['plan']
        self.assertEqual(plan['sources'][0]['abstract'], reference['abstract'])
        self.assertEqual(plan['sources'][0]['ipc'], reference['ipc'])
        self.assertEqual(module.STATE['research_workbench']['target_patents'][0]['abstract'], reference['abstract'])
        self.assertTrue(any('参照' in note or '補完' in note for note in plan['classification_notes']))
        # Result CSV content stays as imported; only the separate seed reference
        # may supplement planning, never conceal missing fields in the map.
        self.assertFalse(module.STATE['patents'][0]['abstract'])

    def test_aliases_never_inflate_target_recovery_count(self):
        rows = [patent(), patent('JP-2024-000001-A')]
        before = copy.deepcopy(rows)
        data = compute_evaluation(rows, target_ids=['JP2024000001A', 'JP-2024-000001-A', 'JP2024000002A'])
        self.assertEqual(data['target_count'], 2)
        self.assertEqual(data['target_found'], 1)
        self.assertEqual(data['missing_target_ids'], ['JP2024000002A'])
        self.assertEqual(rows, before)

    def test_negative_seed_safety_remains_after_csv_replacement(self):
        self.make_plan(user_aspects=['制御', '除外: 車両'], keywords='')
        self.replace_csv()
        plan = module.STATE['research_workbench']['plan']
        body = dict(plan_id=plan['id'], concepts=[dict(id=c['id'], terms=c['terms'], role=c['role'])
                                                 for c in plan['concepts']], classification_keys=[])
        before = copy.deepcopy(module.STATE)
        response = self.client.post('/api/research/preview', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('ターゲット', response.text)
        self.assertEqual(module.STATE, before)

    def test_not_protection_uses_current_keeps_and_selected_seeds_not_unselected_archive_labels(self):
        archived = patent('JP-archived-seed')
        archived.update(title='玩具の制御方法', abstract='玩具の走行を制御する。', label='keep', label_source='human')
        module.STATE['research_workbench']['target_patents'] = [archived]
        plan = self.make_plan(entry_mode='discover', target_ids=[], user_aspects=['制御', '除外: 玩具'], keywords='')
        body = dict(plan_id=plan['id'], concepts=[dict(id=c['id'], terms=c['terms'], role=c['role'])
                                                 for c in plan['concepts']], classification_keys=[])
        # An unselected seed outside the current CSV is reference material,
        # even when it was marked keep for an earlier topic.
        self.post('research/preview', body)
        self.assertEqual(module.STATE['research_workbench']['target_patents'][0]['label'], 'keep')
        selected = self.make_plan(target_ids=[archived['id']], user_aspects=['制御', '除外: 玩具'], keywords='')
        selected_body = {**body, 'plan_id': selected['id']}
        response = self.client.post('/api/research/preview', json=selected_body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('ターゲット', response.json()['detail'])
        # A current row's label overrides the saved row's prior label. Its
        # missing text may still be supplemented from the source reference.
        current = copy.deepcopy(archived)
        current.update(title='', abstract='', label='exclude')
        module.STATE['patents'] = [current]
        unselected = self.make_plan(entry_mode='discover', target_ids=[], user_aspects=['制御', '除外: 玩具'], keywords='')
        body['plan_id'] = unselected['id']
        self.post('research/preview', body)
        module.STATE['patents'][0]['label'] = 'keep'
        response = self.client.post('/api/research/preview', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('必要特許', response.json()['detail'])

    def test_refinement_adds_and_removes_feedback_without_flattening_facet_ast(self):
        initial, _ = self.adopt_plan(self.make_plan())
        original_tree = copy.deepcopy(initial['boolean_tree'])
        original_concepts = copy.deepcopy(initial['concept_tree'])
        self.assertEqual(original_concepts['op'], 'not')
        self.assertEqual(original_concepts['children'][0]['children'][0]['op'], 'or')
        with patch.object(module, 'refinement_terms', return_value={'include': ['安全'], 'exclude': ['充電設備']}):
            refined = self.post('query', {'refine': True, 'include': ['安全'], 'exclude': ['充電設備']})['queries'][-1]
            restored = self.post('query', {'refine': True, 'include': [], 'exclude': []})['queries'][-1]
        self.assertEqual(refined['concept_tree'], original_concepts)
        self.assertEqual(refined['boolean_tree']['op'], 'not')
        self.assertEqual(refined['boolean_tree']['children'][1]['value'], '充電設備')
        self.assertEqual(restored['boolean_tree'], original_tree)
        self.assertEqual(restored['purpose'], initial['purpose'])
        self.assertEqual(restored['facets'], initial['facets'])
        self.assertEqual(restored['target_ids'], initial['target_ids'])
        self.assertEqual(module.STATE['research_workbench']['active_query_id'], restored['id'])
        self.assertTrue(export_query(restored, 'jplatpat')['can_copy'])
        self.assertEqual(len(module.STATE['queries']), 3)
        saved = json.loads((module.DATA / 'workspace.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state']['queries'][-1]['boolean_tree'], original_tree)


if __name__ == '__main__':
    unittest.main()
