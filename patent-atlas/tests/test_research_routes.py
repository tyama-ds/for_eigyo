"""Research entry/API integration uses only test_app's temporary workspace."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import classification_bridge
import test_app as integration
from discovery_routes import new_discovery
from orchestration_engine import new_orchestration
from query_formats import export_query
from research_routes import new_workbench

module = integration.module
REAL_CANDIDATE_LIST = module.candidate_list


def patent(identifier='JP2024000001A', title='車両の走行制御方法'):
    return dict(id=identifier, title=title, abstract='車両の走行状態に基づいて制御する。',
                ipc=['B60W30/00'], fi=[], fterm=[], cpc=[], applicant='Example', year='2024',
                family='', label=None, label_source=None, label_reason='', score=None)


class ResearchRoutesTests(unittest.TestCase):
    def test_union_group_preview_apply_and_refinement_preserve_boolean_meaning(self):
        plan = self.plan(keywords='自動運転 自動車 運転システム')
        payload = self.query_payload(plan)
        preview = self.post('preview', payload)
        tree = preview['query']['boolean_tree']
        def matches(node, present):
            if node['op'] == 'text': return node['value'] in present
            values = [matches(child, present) for child in node['children']]
            return all(values) if node['op'] == 'and' else any(values)
        self.assertTrue(matches(tree, {'自動車', '自動運転'}))
        self.assertTrue(matches(tree, {'自動車', '運転システム'}))
        self.assertFalse(matches(tree, {'自動運転', '運転システム'}))
        self.assertFalse(matches(tree, {'自動車'}))
        for format_id in ('jplatpat', 'espacenet', 'google_patents'):
            exported = export_query(preview['query'], format_id, replacements=None if format_id == 'jplatpat' else
                                    {'自動車': 'automobile', '自動運転': 'autonomous driving', '運転システム': 'driving system'})
            self.assertTrue(exported['can_copy'], exported)
            self.assertIn('+' if format_id == 'jplatpat' else 'OR', exported['expression'])
        changed = copy.deepcopy(payload)
        for facet in changed['concepts']: facet['or_group'] = ''
        self.unchanged('apply', {**changed, 'expected_preview_hash': preview['preview_hash']}, 409)
        result = self.post('apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        self.assertEqual(result['queries'][-1]['boolean_tree'], tree)
        grouped = [f for f in result['queries'][-1]['facets'] if f['name'] in ('自動運転', '運転システム')]
        self.assertEqual(grouped[0]['or_group'], grouped[1]['or_group'])
        with patch.object(module, 'refinement_terms', return_value={'include': [], 'exclude': []}):
            response = self.client.post('/api/query', json={'refine': True, 'include': [], 'exclude': []})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(module.STATE['queries'][-1]['boolean_tree'], tree)
        self.assertEqual(module.SETTINGS, self.settings)

    def test_group_changes_cannot_union_explicit_required_or_excluded_aspects(self):
        plan = self.plan(keywords='自動運転', user_aspects=['必須: 自動車', '除外: 玩具'])
        payload = self.query_payload(plan)
        payload['concepts'][1]['role'] = 'exclude'
        for index in (0, 1):
            changed = copy.deepcopy(payload)
            changed['concepts'][index]['or_group'] = 'driving'
            self.unchanged('preview', changed)
        for value in (['group'], 'not a group', 'a' * 33):
            changed = copy.deepcopy(payload)
            changed['concepts'][-1]['or_group'] = value
            self.unchanged('preview', changed)
        # Ordinary input terms remain manually regroupable across technical areas.
        plan = self.plan(keywords='語甲 語乙 語丙')
        payload = self.query_payload(plan)
        payload['concepts'][0]['or_group'] = 'custom'
        payload['concepts'][1]['or_group'] = 'custom'
        preview = self.post('preview', payload)
        self.assertEqual(preview['query']['boolean_tree']['children'][0]['op'], 'or')

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
        module.SETTINGS.update(provider='local', base_url='http://127.0.0.1:1234/v1',
                               model='existing-local-model', api_key='retained-test-key',
                               proxy='http://proxy-test.invalid:8080', ops_key='retained-ops-key',
                               ops_secret='retained-ops-secret', classification_layout='semantic')
        candidate = module.explorer.enrich(dict(kind='IPC', code='B60'))
        module.STATE.clear()
        module.STATE.update(keywords='既存キーワード', candidates=[candidate], selected=[candidate['key']],
                            patents=[patent(), patent('JP2024000002A', '車両の誘導装置')],
                            clusters=[], queries=[], training=None, agent_log=[], demo=False,
                            import_info=None, classification_view=None, candidate_proposal=None,
                            discovery=new_discovery(), orchestration=new_orchestration(),
                            research_workbench=new_workbench())
        self.candidates_patch = patch.object(module, 'candidate_list', return_value=[candidate])
        self.candidates_patch.start()
        self.addCleanup(self.candidates_patch.stop)
        self.settings = copy.deepcopy(module.SETTINGS)
        module.save()

    def post(self, path, body=None):
        response = self.client.post('/api/research/' + path, json=body or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def unchanged(self, path, body, status=400):
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post('/api/research/' + path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
        self.assertEqual(module.SETTINGS, self.settings)
        return response

    def plan(self, mode='target', **updates):
        brief = dict(entry_mode=mode, purpose='先行技術調査', goal='車両の運転技術を調査する。',
                     keywords='車両 制御', user_aspects=[],
                     target_ids=[module.STATE['patents'][0]['id']] if mode == 'target' else [])
        brief.update(updates)
        with patch('research_strategy.complete') as llm:
            result = self.post('plan', {'brief': brief, 'use_llm': False})
        llm.assert_not_called()
        return result['research_workbench']['plan']

    def query_payload(self, plan, classification_keys=None):
        return dict(plan_id=plan['id'], concepts=[dict(id=c['id'], terms=c['terms'], role='required') for c in plan['concepts']],
                    classification_keys=classification_keys or [])

    def import_example(self, text='[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX]', **values):
        body = dict(text=text, source_format='jplatpat', name='先行例', purpose='元の目的')
        body.update(values)
        result = self.post('library/import', body)
        return result['research_workbench']['library'][-1]

    def llm_plan_response(self):
        return dict(purpose='先行技術調査', summary='対象と機能を分け、既知の必要例の回収を確認する。',
                    concepts=[dict(id='keyword1', name='車両', role='required', terms=['車両', '自動車'],
                                   abstract_terms=['移動体'], reason='対象物の具体語と機能による抽象語。', evidence_ids=['JP2024000001A']),
                              dict(id='keyword2', name='制御', role='required', terms=['制御'],
                                   abstract_terms=['自律制御'], reason='制御方式を比較する。', evidence_ids=['JP2024000001A'])],
                    questions=['請求項と既知の必要例を照合してください。'])

    def llm_plan_body(self):
        return dict(use_llm=True, brief=dict(entry_mode='target', purpose='先行技術調査',
                    goal='車両の運転技術を調査する。', keywords='車両 制御', user_aspects=[],
                    target_ids=['JP2024000001A']))

    def test_target_plan_uses_seed_metadata_and_does_not_change_settings_or_selection(self):
        before = copy.deepcopy(module.STATE)
        plan = self.plan()
        self.assertEqual(plan['target_ids'], ['JP2024000001A'])
        self.assertIn('IPC:B60W30/00', [c['key'] for c in plan['classification_candidates']])
        self.assertEqual(plan['sources'][0]['ipc'], ['B60W30/00'])
        self.assertEqual(module.STATE['selected'], before['selected'])
        self.assertEqual(module.STATE['queries'], before['queries'])
        self.assertEqual(module.STATE['keywords'], before['keywords'])
        self.assertEqual(module.SETTINGS, self.settings)
        public = self.client.get('/api/state').text
        stored = (module.DATA / 'workspace.json').read_text(encoding='utf-8')
        for secret in ('retained-test-key', 'proxy-test.invalid', 'retained-ops-key', 'retained-ops-secret'):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret, stored)

    def test_seedless_discovery_plan_requires_no_target_and_remains_a_hypothesis(self):
        plan = self.plan(mode='discover')
        self.assertEqual(plan['sources'], [])
        self.assertEqual(plan['target_ids'], [])
        self.assertTrue(any('ターゲット' in q for q in plan['questions']))
        self.assertTrue(all(any(o['type'] == 'concept_hypothesis' for o in c['origins']) for c in plan['classification_candidates']))
        self.unchanged('plan', {'brief': {'entry_mode': 'target', 'target_ids': []}, 'use_llm': False})

    def test_plan_preview_apply_preserves_facet_or_and_not_structure(self):
        plan = self.plan(user_aspects=['用途', '除外: 玩具'], keywords='車両')
        payload = self.query_payload(plan, ['IPC:B60'])
        payload['concepts'][0]['terms'] = ['走行', '運転']
        payload['concepts'][1]['role'] = 'exclude'
        before = copy.deepcopy(module.STATE)
        preview = self.post('preview', payload)
        self.assertEqual(module.STATE, before)
        self.assertEqual(preview['query']['concept_tree']['op'], 'not')
        self.assertEqual(preview['query']['concept_tree']['children'][0]['children'][0]['op'], 'or')
        result = self.post('apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        query = result['queries'][-1]
        self.assertEqual(query['boolean_tree'], preview['query']['boolean_tree'])
        self.assertEqual(query['purpose'], '先行技術調査')
        self.assertEqual(query['target_ids'], ['JP2024000001A'])
        self.assertEqual(result['selected'], ['IPC:B60'])
        self.assertEqual(result['keywords'], '車両')
        self.assertEqual(query['term_catalog'], ['走行', '運転', '車両', '玩具'])
        self.assertEqual(module.SETTINGS, self.settings)
        self.assertTrue(export_query(query, 'jplatpat')['can_copy'])

    def test_missing_stale_or_changed_preview_cannot_apply(self):
        plan = self.plan()
        payload = self.query_payload(plan)
        self.unchanged('apply', payload, 409)
        preview = self.post('preview', payload)
        changed = copy.deepcopy(payload)
        changed['concepts'][0]['terms'] = ['航空機']
        self.unchanged('apply', {**changed, 'expected_preview_hash': preview['preview_hash']}, 409)
        self.post('brief', {'purpose': '別の目的'})
        self.unchanged('apply', {**payload, 'expected_preview_hash': preview['preview_hash']}, 409)

    def test_exclusion_that_hits_target_is_rejected_before_query_creation(self):
        plan = self.plan(user_aspects=['用途', '除外: 車両'], keywords='制御')
        payload = self.query_payload(plan)
        payload['concepts'][1]['role'] = 'exclude'
        self.unchanged('preview', payload)
        self.assertEqual(module.STATE['queries'], [])

    def test_plan_failure_and_concurrent_change_do_not_replace_existing_plan(self):
        original = self.plan()
        with patch('research_strategy.complete', side_effect=ValueError('生成に失敗')):
            self.unchanged('plan', {'use_llm': True})
        self.assertEqual(module.STATE['research_workbench']['plan'], original)
        def concurrent_update(*args):
            module.STATE['research_workbench']['brief']['purpose'] = '別タブで変更'
            return []
        with patch.object(module, 'candidate_list', side_effect=concurrent_update):
            response = self.client.post('/api/research/plan', json={'use_llm': False})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(module.STATE['research_workbench']['plan'], original)
        self.assertEqual(module.STATE['research_workbench']['brief']['purpose'], '別タブで変更')

    def test_library_import_adapt_apply_preserves_original_and_settings(self):
        entry = self.import_example()
        original = copy.deepcopy(entry)
        payload = dict(example_id=entry['id'], replacements={'車両': '航空機'},
                       class_replacements={'IPC:B60': 'B64'}, purpose='航空機の調査')
        before = copy.deepcopy(module.STATE)
        preview = self.post('library/adapt', payload)
        self.assertEqual(module.STATE, before)
        self.assertEqual(preview['query']['boolean_tree']['op'], 'not')
        self.assertEqual(preview['query']['classifications'][0]['code'], 'B64')
        self.assertTrue(preview['query']['classifications'][0]['verified'])
        result = self.post('library/apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        self.assertEqual(result['research_workbench']['library'][0], original)
        query = result['queries'][-1]
        self.assertEqual(query['boolean_tree'], preview['query']['boolean_tree'])
        self.assertEqual(query['provenance']['example_id'], entry['id'])
        self.assertEqual(query['purpose'], '航空機の調査')
        self.assertEqual(result['keywords'], '既存キーワード')
        self.assertEqual(result['selected'], ['IPC:B64'])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_library_apply_rejects_unreviewed_changed_conditions_and_reference_entries(self):
        entry = self.import_example()
        payload = {'example_id': entry['id'], 'replacements': {'車両': '航空機'}, 'purpose': '比較調査'}
        self.unchanged('library/apply', payload, 409)
        preview = self.post('library/adapt', payload)
        self.unchanged('library/apply', {**payload, 'replacements': {'車両': '船舶'}, 'expected_preview_hash': preview['preview_hash']}, 409)
        ref = self.import_example('ftxt="vehicle" AND ipc=B60', source_format='espacenet')
        self.assertEqual(ref['status'], 'reference_only')
        self.unchanged('library/adapt', {'example_id': ref['id']})
        self.unchanged('library/apply', {'example_id': ref['id'], 'expected_preview_hash': 'made-up'})

    def test_imported_verification_and_runtime_fields_are_not_trusted(self):
        data = dict(schema_version=1, name='JSON例', query={'keywords': ['車両'],
                    'classifications': [{'kind': 'CPC', 'code': 'Y99Z9999/99', 'verified': True}],
                    'api_key': 'user-supplied-not-a-setting'}, settings={'provider': 'openai'})
        entry = self.import_example(json.dumps(data), source_format='portable_json')
        preview = self.post('library/adapt', {'example_id': entry['id']})
        self.assertFalse(preview['query']['classifications'][0]['verified'])
        self.assertNotIn('api_key', preview['query'])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_save_failure_rolls_back_state_history_and_pending_file(self):
        entry = self.import_example()
        payload = {'example_id': entry['id'], 'purpose': '保存失敗テスト'}
        preview = self.post('library/adapt', payload)
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        pending = module.DATA / 'workspace.tmp'
        pending.write_bytes(b'previous pending content')
        try:
            with patch.object(Path, 'replace', side_effect=OSError('simulated write failure')):
                response = TestClient(module.app, raise_server_exceptions=False).post(
                    '/api/research/library/apply', json={**payload, 'expected_preview_hash': preview['preview_hash']})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(module.STATE, before)
            self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
            self.assertEqual(pending.read_bytes(), b'previous pending content')
            self.assertEqual(module.SETTINGS, self.settings)
        finally:
            pending.unlink(missing_ok=True)

    def test_busy_mutations_do_not_change_workspace(self):
        module.JOB.update(status='running')
        try:
            for path, body in [('brief', {'purpose': '変えない'}), ('plan', {'use_llm': False}),
                               ('library/import', {'text': '[車両/TX]'}),
                               ('targets/add', {'id': 'JP3', 'title': '新しい特許'})]:
                with self.subTest(path=path):
                    self.unchanged(path, body, 409)
        finally:
            module.JOB.update(status='idle')

    def test_manual_target_add_reuses_canonical_existing_id_for_number_variations(self):
        row = module.STATE['patents'][0]
        result = self.post('targets/add', {'id': 'JP-2024-000001-A', 'title': row['title'], 'abstract': row['abstract']})
        self.assertEqual(len(result['patents']), 2)
        self.assertEqual(result['research_workbench']['brief']['target_ids'], ['JP2024000001A'])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_manual_target_add_marks_existing_training_as_dataset_changed(self):
        module.STATE['training'] = {'mode': 'lightweight', 'sample_count': 2}
        result = self.post('targets/add', {'id': 'JP2024000003A', 'title': '車両の新しい走行制御', 'ipc': 'B60W30/00'})
        self.assertEqual(len(result['patents']), 3)
        self.assertTrue(result['training']['dataset_changed'])
        self.assertGreaterEqual(result['training']['added_since_training'], 1)
        self.assertIn('JP2024000003A', result['research_workbench']['brief']['target_ids'])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_brief_rejects_values_that_cannot_be_used_by_planning(self):
        for field, value in [('goal', '長' * 5001), ('user_aspects', ['長' * 161]),
                             ('target_ids', [f'JP{i}' for i in range(21)])]:
            with self.subTest(field=field):
                self.unchanged('brief', {field: value})
        self.unchanged('brief', {'revision': 999, 'purpose': '変更'}, 409)

    def test_new_workflow_rejects_invalid_ids_duplicate_aspects_and_raw_operators(self):
        self.unchanged('brief', {'target_ids': ['missing']})
        plan = self.plan()
        payload = self.query_payload(plan)
        duplicate = copy.deepcopy(payload)
        duplicate['concepts'].append(copy.deepcopy(duplicate['concepts'][0]))
        self.unchanged('preview', duplicate)
        operator = copy.deepcopy(payload)
        operator['concepts'][0]['terms'] = ['A/TX+B/TX']
        self.unchanged('preview', operator)
        self.unchanged('library/import', {'text': '{broken', 'source_format': 'portable_json'})
        self.unchanged('library/import', {'text': 'a' * 100001})

    def test_refinement_retains_imported_not_scope_instead_of_flattening_keywords(self):
        entry = self.import_example('[車両/TX]-[[玩具/TX]*[模型/TX]]')
        payload = {'example_id': entry['id']}
        preview = self.post('library/adapt', payload)
        self.post('library/apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        initial = copy.deepcopy(module.STATE['queries'][-1])
        response = self.client.post('/api/query', json={'refine': True, 'include': [], 'exclude': []})
        self.assertEqual(response.status_code, 200, response.text)
        refined = response.json()['queries'][-1]
        self.assertEqual(refined['boolean_tree'], initial['boolean_tree'])
        self.assertEqual(refined['boolean_tree']['children'][1]['op'], 'and')
        self.assertEqual(module.SETTINGS, self.settings)

    def test_unmapped_keywords_need_explicit_review_before_applying_plan(self):
        plan = self.plan(mode='discover', keywords='語1 語2 語3 語4 語5 語6 語7 語8 語9')
        self.assertEqual(plan['unmapped_keywords'], ['語9'])
        payload = self.query_payload(plan)
        self.unchanged('preview', payload)
        # Acknowledgement is deliberately explicit; omitted words remain in the
        # plan/input record and never silently become an extra AND condition.
        preview = self.post('preview', {**payload, 'acknowledge_unmapped': True})
        self.assertIsInstance(preview['preview_hash'], str)

    def test_projection_and_evaluation_are_read_only_and_keep_patent_ids(self):
        module.STATE['patents'][0].update(label='keep', label_source='human')
        module.STATE['patents'][1].update(label='exclude', label_source='agent')
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        response = self.client.post('/api/map/project', json={'method': 'pca'})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual([p['id'] for p in data['projection']['points']], [p['id'] for p in before['patents']])
        self.assertEqual(data['evaluation']['human_keep'], 1)
        self.assertEqual(data['evaluation']['human_exclude'], 0)
        self.assertEqual(data['evaluation']['machine_exclude'], 1)
        self.assertIsNone(data['evaluation']['signal_noise_ratio'])
        self.assertEqual(self.client.get('/api/map/evaluation').status_code, 200)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
        for method in ([], {}, None, True, 'invalid'):
            with self.subTest(method=method):
                invalid = self.client.post('/api/map/project', json={'method': method})
                self.assertEqual(invalid.status_code, 400, invalid.text)
                self.assertEqual(module.STATE, before)

    def test_adoption_of_unlinked_results_requires_explicit_reference_acknowledgement(self):
        entry = self.import_example()
        payload = {'example_id': entry['id']}
        preview = self.post('library/adapt', payload)
        result = self.post('library/apply', {**payload, 'expected_preview_hash': preview['preview_hash']})
        query = result['queries'][-1]
        body = {'query_id': query['id'], 'note': '既知の特許群で内容を確認した。'}
        self.unchanged('adopt', body)
        acknowledged = self.post('adopt', {**body, 'allow_unlinked': True})
        record = acknowledged['research_workbench']['adoptions'][-1]
        self.assertFalse(record['query_matches_observation'])
        self.assertIn('再現率', record['scope'])
        module.STATE['search_result'] = {'id': 'csv-result', 'query_id': query['id']}
        linked = self.post('adopt', body)['research_workbench']['adoptions'][-1]
        self.assertTrue(linked['query_matches_observation'])
        self.assertEqual(linked['observed_query_id'], query['id'])
        self.assertEqual(linked['search_result_id'], 'csv-result')
        self.assertEqual(module.SETTINGS, self.settings)

    def test_llm_adaptation_is_explicit_proposal_without_state_or_disk_write(self):
        entry = self.import_example()
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA / 'workspace.json').read_bytes()
        generated = dict(replacements={'車両': '航空機'}, class_replacements={'IPC:B60': 'B64'},
                         reasons=[{'kind': 'term', 'original': '車両', 'replacement': '航空機', 'reason': '機器の対象領域を変更する候補。'},
                                  {'kind': 'classification', 'original': 'IPC:B60', 'replacement': 'B64', 'reason': '航空分野を比較する候補。'}],
                         questions=[])
        with patch('query_adaptation.complete', return_value=generated) as llm:
            result = self.post('library/suggest', {'example_id': entry['id'], 'purpose': '航空技術の調査', 'goal': '機体の経路制御を調べたい。'})
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(result['replacements'], {'車両': '航空機'})
        self.assertTrue(any('NOT' in question for question in result['questions']))
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA / 'workspace.json').read_bytes(), disk)
        self.assertEqual(module.SETTINGS, self.settings)

    def test_failed_and_unsupported_llm_suggestion_keep_library_and_history(self):
        entry = self.import_example()
        with patch('query_adaptation.complete', side_effect=ValueError('LLMの応答が不正')) as llm:
            self.unchanged('library/suggest', {'example_id': entry['id'], 'purpose': '新目的', 'goal': '新しい技術'})
        self.assertEqual(llm.call_count, 1)
        ref = self.import_example('unsupported NEAR expression', source_format='reference')
        with patch('query_adaptation.complete') as llm:
            self.unchanged('library/suggest', {'example_id': ref['id'], 'purpose': '新目的', 'goal': '新しい技術'})
            self.unchanged('library/suggest', {'example_id': entry['id'], 'purpose': '', 'goal': '新しい技術'})
        llm.assert_not_called()

    def test_llm_plan_passes_purpose_concepts_selected_sources_and_existing_connection_to_classification(self):
        module.STATE['patents'][0].update(fi=['B60W30/00'], fterm=['3D241BA01'], cpc=['B60W30/00'])
        classification_response = {'candidates': [
            {'kind': 'IPC', 'code': 'B60W30/00', 'reason': '制御方式の探索仮説。'},
            {'kind': 'IPC', 'code': 'B64', 'reason': '隣接領域の比較候補。'},
            {'kind': 'F-term', 'code': '3D241BA01', 'reason': '車両制御の観点候補。'},
        ]}
        before_rows = copy.deepcopy(module.STATE['patents'])
        with patch('research_strategy.complete', return_value=self.llm_plan_response()) as plan_llm, \
                patch.object(module, 'complete', return_value=classification_response) as class_llm, \
                patch.object(module, 'candidate_list', wraps=REAL_CANDIDATE_LIST) as candidates:
            result = self.post('plan', self.llm_plan_body())
        self.assertEqual(plan_llm.call_count, 1)
        self.assertEqual(class_llm.call_count, 1)
        self.assertEqual(candidates.call_count, 1)
        self.assertIs(candidates.call_args.args[1], True)
        call_context = candidates.call_args.kwargs['research_context']
        plan = result['research_workbench']['plan']
        self.assertEqual(call_context['purpose'], '先行技術調査')
        self.assertEqual(call_context['concepts'], plan['concepts'])
        self.assertEqual([source['id'] for source in call_context['sources']], ['JP2024000001A'])
        self.assertEqual(call_context['sources'][0]['ipc'], ['B60W30/00'])
        self.assertEqual(call_context['sources'][0]['fi'], ['B60W30/00'])
        self.assertEqual(call_context['sources'][0]['fterm'], ['3D241BA01'])
        self.assertEqual(call_context['sources'][0]['cpc'], ['B60W30/00'])
        self.assertEqual(candidates.call_args.kwargs['connection'], self.settings)
        sent_connection, _, sent_payload = class_llm.call_args.args
        self.assertEqual(sent_connection, self.settings)
        self.assertEqual(sent_payload['research_context'], call_context)
        self.assertIn('移動体', sent_payload['keywords'])
        self.assertNotIn('retained-test-key', json.dumps(sent_payload))
        self.assertEqual(plan['classification_proposal']['status'], 'completed')
        self.assertEqual(plan['classification_proposal']['accepted_count'], 3)
        self.assertIn('IPC:B64', [c['key'] for c in plan['classification_candidates']])
        self.assertEqual(module.STATE['patents'], before_rows)
        self.assertEqual(module.STATE['queries'], [])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_classification_llm_failure_keeps_concept_plan_seed_metadata_and_dictionary_candidates(self):
        dictionary_candidate = module.explorer.enrich(dict(kind='IPC', code='B60', reason='辞書の比較候補。'))
        def failing_llm_or_dictionary(text, use_llm=False, **options):
            if use_llm:
                return REAL_CANDIDATE_LIST(text, True, **options)
            return [copy.deepcopy(dictionary_candidate)]
        with patch('research_strategy.complete', return_value=self.llm_plan_response()) as plan_llm, \
                patch.object(module, 'complete', side_effect=ValueError('分類候補のJSONが不正')) as class_llm, \
                patch.object(module, 'candidate_list', side_effect=failing_llm_or_dictionary) as candidates:
            result = self.post('plan', self.llm_plan_body())
        plan = result['research_workbench']['plan']
        self.assertEqual(plan_llm.call_count, 1)
        self.assertEqual(class_llm.call_count, 1)
        self.assertEqual([call.args[1] for call in candidates.call_args_list], [True, False])
        self.assertEqual(plan['method'], 'llm')
        self.assertEqual(plan['concepts'][0]['terms'], ['車両', '自動車'])
        self.assertEqual(plan['concepts'][0]['abstract_terms'], ['移動体'])
        self.assertEqual(plan['classification_proposal']['status'], 'failed')
        self.assertIn('JSON', plan['classification_proposal']['error'])
        self.assertTrue(any('保持しています' in note for note in plan['classification_notes']))
        self.assertIn('IPC:B60W30/00', [c['key'] for c in plan['classification_candidates']])
        self.assertTrue(any(any(o.get('type') == 'concept_hypothesis' for o in c.get('origins', [])) for c in plan['classification_candidates']))
        self.assertEqual(plan['target_ids'], ['JP2024000001A'])
        self.assertEqual(plan['sources'][0]['ipc'], ['B60W30/00'])
        self.assertEqual(module.STATE['queries'], [])
        self.assertEqual(module.SETTINGS, self.settings)

    def test_hypothesis_for_seed_code_does_not_replace_actual_classification_reason_or_origins(self):
        seed = next(c for c in classification_bridge.extract(module.STATE['patents'][0])['items'] if c['key'] == 'IPC:B60W30/00')
        llm_reason = 'この理由は実付与の根拠として表示してはいけない探索仮説。'
        with patch('research_strategy.complete', return_value=self.llm_plan_response()), \
                patch.object(module, 'complete', return_value={'candidates': [{'kind': 'IPC', 'code': 'B60W30/00', 'reason': llm_reason}]}), \
                patch.object(module, 'candidate_list', wraps=REAL_CANDIDATE_LIST):
            result = self.post('plan', self.llm_plan_body())
        plan = result['research_workbench']['plan']
        actual = next(c for c in plan['classification_candidates'] if c['key'] == seed['key'])
        self.assertEqual(actual['reason'], seed['reason'])
        self.assertNotIn(llm_reason, actual['reason'])
        self.assertEqual(actual['evidence_status'], 'patent_metadata')
        self.assertEqual(actual['classification_origins'], seed['classification_origins'])
        self.assertTrue(any(o.get('type') == 'patent' and o.get('patent_id') == 'JP2024000001A' for o in actual['origins']))
        self.assertTrue(any(o.get('type') == 'concept_hypothesis' for o in actual['origins']))
        self.assertTrue(any(source.get('type') == 'llm' and llm_reason in source.get('reason', '') for source in actual.get('recommendation_sources', [])))

    def test_offline_plan_never_calls_either_llm_and_marks_proposal_not_requested(self):
        body = self.llm_plan_body()
        body['use_llm'] = False
        with patch('research_strategy.complete') as plan_llm, \
                patch.object(module, 'complete') as class_llm, \
                patch.object(module, 'candidate_list', wraps=REAL_CANDIDATE_LIST) as candidates:
            result = self.post('plan', body)
        plan_llm.assert_not_called()
        class_llm.assert_not_called()
        self.assertEqual([call.args[1] for call in candidates.call_args_list], [False])
        plan = result['research_workbench']['plan']
        self.assertEqual(plan['method'], 'offline')
        self.assertEqual(plan['classification_proposal']['status'], 'not_requested')
        self.assertEqual(module.SETTINGS, self.settings)


if __name__ == '__main__':
    unittest.main()
