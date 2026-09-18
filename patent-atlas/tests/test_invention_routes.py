"""New element workflow uses an isolated workspace and reviewed Boolean scopes."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_research_routes as existing
import llm_judgment

module = existing.module


class InventionRoutesTests(unittest.TestCase):
    setUp = existing.ResearchRoutesTests.setUp
    post = existing.ResearchRoutesTests.post
    unchanged = existing.ResearchRoutesTests.unchanged

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def plan(self):
        body = dict(brief=dict(entry_mode='target', purpose='発明要素の先行例を調べる',
            goal='検出結果を使う走行制御', keywords='車両', user_aspects=[],
            target_ids=['JP2024000001A']), manual_elements=['障害物検出', '走行制御'],
            invention_text='障害物を検出し、検出結果に応じて車両を制御する。', use_llm=False)
        with patch('invention_strategy.complete') as complete:
            result = self.post('invention/plan', body)
        complete.assert_not_called()
        return result['research_workbench']['invention']['plan']

    def payload(self, plan, strategy='element'):
        elements = plan['elements'][:1] if strategy == 'element' else plan['elements']
        return dict(plan_id=plan['id'], strategy=strategy, element_ids=[e['id'] for e in elements],
            elements=[dict(id=e['id'], perspectives=[dict(id=p['id'], terms=p['terms'],
                classification_keys=[], class_mode='text') for p in e['perspectives']]) for e in elements],
            perspective_joins={e['id']: 'and' for e in elements}, exclusions=[],
            confirm_exclusions=False, confirm_class_intersection=False)

    def test_individual_search_then_combination_preserves_sources_and_settings(self):
        plan = self.plan()
        self.assertEqual(plan['target_ids'], ['JP2024000001A'])
        self.assertEqual(module.SETTINGS, self.settings)
        self.assertEqual(module.STATE['queries'], [])
        body = self.payload(plan)
        preview = self.post('invention/preview', body)
        self.assertEqual(preview['query']['boolean_tree'], {'op':'text', 'value':'障害物検出'})
        state = self.post('invention/apply', {**body, 'expected_preview_hash':preview['preview_hash']})
        single = state['queries'][-1]
        self.assertNotIn('走行制御', single['expression'])
        self.assertEqual(single['invention_context']['strategy'], 'element')
        snapshot = llm_judgment.snapshot_query_context(single)
        self.assertEqual(len(snapshot['invention_search']['elements']), 1)
        self.assertEqual(snapshot['search_conditions'], single['boolean_tree'])
        body = self.payload(plan, 'combination')
        preview = self.post('invention/preview', body)
        self.assertEqual(preview['query']['boolean_tree']['op'], 'and')
        self.post('invention/apply', {**body, 'expected_preview_hash':preview['preview_hash']})
        self.assertEqual(len(module.STATE['queries']), 2)
        self.assertEqual(module.STATE['queries'][0], single)
        self.assertEqual(module.SETTINGS, self.settings)

    def test_branch_not_and_class_kw_scope_survive_apply_and_refinement(self):
        plan = self.plan()
        body = self.payload(plan, 'alternatives')
        key = next(c['key'] for c in plan['classification_candidates'] if c['code']=='B60W30/00')
        body['elements'][0]['perspectives'][0].update(classification_keys=[key], class_mode='and')
        body.update(confirm_class_intersection=True, confirm_exclusions=True,
                    exclusions=[dict(scope=plan['elements'][1]['id'], terms=['玩具'])])
        preview = self.post('invention/preview', body)
        tree = preview['query']['boolean_tree']
        self.assertEqual(tree['op'], 'or')
        self.assertEqual(tree['children'][0]['op'], 'and')
        self.assertEqual(tree['children'][1]['op'], 'not')
        self.post('invention/apply', {**body, 'expected_preview_hash':preview['preview_hash']})
        adopted = copy.deepcopy(module.STATE['queries'][-1])
        module.make_query(refine=True, include=[], exclude=[])
        refined = module.STATE['queries'][-1]
        self.assertEqual(refined['boolean_tree'], adopted['boolean_tree'])
        self.assertEqual(refined['invention_context'], adopted['invention_context'])
        self.assertEqual(refined['invention_plan_id'], adopted['invention_plan_id'])

    def test_changed_scope_or_new_brief_cannot_apply_old_preview(self):
        plan = self.plan(); body = self.payload(plan, 'alternatives')
        body.update(confirm_exclusions=True, exclusions=[dict(scope='all', terms=['玩具'])])
        preview = self.post('invention/preview', body)
        body['exclusions'][0]['scope'] = plan['elements'][0]['id']
        self.unchanged('invention/apply', {**body, 'expected_preview_hash':preview['preview_hash']}, 409)
        self.post('brief', {'goal':'新しい目的の説明'})
        self.unchanged('invention/preview', body, 409)

    def test_plan_failure_or_concurrent_change_retains_previous_plan(self):
        plan = self.plan()
        with patch('invention_strategy.complete', side_effect=ValueError('形式不正')):
            self.unchanged('invention/plan', {'use_llm':True}, 400)
        self.assertEqual(module.STATE['research_workbench']['invention']['plan'], plan)
        def changed(*args, **kwargs):
            module.STATE['research_workbench']['revision'] += 1
            return copy.deepcopy(plan)
        with patch('invention_routes.plan_invention', side_effect=changed):
            response = self.client.post('/api/research/invention/plan', json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(module.STATE['research_workbench']['invention']['plan'], plan)

    def test_preview_read_only_and_invalid_references_no_mutation(self):
        plan = self.plan(); body = self.payload(plan)
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA/'workspace.json').read_bytes()
        self.post('invention/preview', body)
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA/'workspace.json').read_bytes(), disk)
        body['elements'][0]['perspectives'][0]['classification_keys'] = ['IPC:NOTINPLAN']
        self.unchanged('invention/preview', body)
        self.unchanged('invention/plan', {'use_llm':'yes'})

    def test_save_failure_rolls_back_adoption(self):
        plan = self.plan(); body = self.payload(plan)
        preview = self.post('invention/preview', body)
        before = copy.deepcopy(module.STATE)
        disk = (module.DATA/'workspace.json').read_bytes()
        # commit closes over the save reference from registration.
        with patch.object(Path, 'replace', side_effect=OSError('test disk failure')):
            with self.assertRaises(OSError):
                self.client.post('/api/research/invention/apply', json={**body, 'expected_preview_hash':preview['preview_hash']})
        self.assertEqual(module.STATE, before)
        self.assertEqual((module.DATA/'workspace.json').read_bytes(), disk)


if __name__ == '__main__':
    unittest.main()
