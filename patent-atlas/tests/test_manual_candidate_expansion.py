"""Manual constellation uses multi-aspect hypotheses without changing evidence."""
import copy
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
import test_app as integration


module = integration.module


class ManualCandidateExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(module.app)

    def setUp(self):
        self.assertEqual(module.DATA, Path(integration.TEMP.name))
        module.JOB.update(status='idle')
        module.SETTINGS.clear()
        module.SETTINGS.update(module.DEFAULT_SETTINGS)
        self.assertEqual(self.client.post('/api/demo').status_code, 200)

    def visible(self, state):
        indexed = {candidate['key']: candidate for candidate in state['candidates']}
        return [indexed[key] for key in state['classification_view']['keys']]

    def test_manual_keyword_generation_adds_real_non_h_hypotheses_without_touching_work(self):
        module.STATE['discovery'] = {'id': 'preserve-lab', 'plan': {'keywords': 'another topic'},
                                     'patents': [], 'rounds': [{'round': 1}], 'status': 'planned'}
        module.STATE['patents'][0].update(label='keep', label_source='human')
        module.save()
        before = copy.deepcopy(module.STATE)
        response = self.client.post('/api/candidates', json={'keywords': '全固体電池', 'use_llm': False})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        for field in ('selected', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)
        rows = self.visible(state)
        by_code = {row['code']: row for row in rows}
        for code in ('C01D15/00', 'C04B35/00', 'C04B37/00'):
            self.assertIn(code, by_code)
            self.assertTrue(by_code[code]['verified'])
            self.assertEqual(by_code[code]['evidence_status'], 'hypothesis')
            self.assertEqual(by_code[code]['relevance_status'], 'review_pending')
            self.assertTrue(by_code[code]['recommendation_sources'])
            self.assertTrue(by_code[code]['title_official'])
            self.assertTrue(by_code[code]['ancestors'])
            self.assertTrue(by_code[code]['terms'])
        self.assertLessEqual(len(rows), 24)
        all_codes = {row['code'] for row in state['candidates']}
        self.assertTrue({'H01M10/0562', 'H01M4/139', 'C04B35/64'} <= all_codes)
        self.assertFalse({'C01D13/00', 'B23H3/00', 'H01M8/04291'} & all_codes)

    def test_coarse_overview_retains_precise_process_codes_facets_and_reasons(self):
        candidates = module.candidate_list('全固体電池')
        original = copy.deepcopy(candidates)
        rows = module.explorer.overview(candidates, '全固体電池')
        ceramic = next(row for row in rows if row['code'] == 'C04B35/00')
        self.assertTrue({'material', 'process'} <= set(ceramic['facets']))
        matched = {row['code']: row for row in ceramic['matched_classifications']}
        self.assertIn('C04B35/64', matched)
        self.assertIn('sintering', matched['C04B35/64']['reason'])
        self.assertIn('sintering', ceramic['terms'])
        self.assertEqual(candidates, original)

    def test_explicit_recommendation_and_selection_survive_regeneration(self):
        response = self.client.post('/api/classifications/seeds', json={'ipc_text': 'G06V10/00', 'label': 'User target'})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/api/selection', json={'selected': ['IPC:G06V10/00']})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/api/candidates', json={'keywords': '全固体電池'})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(state['selected'], ['IPC:G06V10/00'])
        target = next(row for row in self.visible(state) if row['code'] == 'G06V10/00')
        self.assertTrue(any(origin.get('label') == 'User target' for origin in target['origins']))

    def test_classification_only_overview_retains_targets_without_requiring_lab_keywords(self):
        self.assertEqual(self.client.post('/api/keywords', json={'keywords': ''}).status_code, 200)
        self.assertEqual(self.client.post('/api/classifications/seeds', json={
            'ipc_text': 'C04B35/64', 'label': 'User target'}).status_code, 200)
        self.assertEqual(self.client.post('/api/selection', json={'selected': ['IPC:C04B35/64']}).status_code, 200)
        before = copy.deepcopy(module.STATE)
        response = self.client.post('/api/classifications/browse', json={'direction': 'overview'})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        for field in ('selected', 'keywords', 'patents', 'queries', 'training', 'discovery'):
            self.assertEqual(module.STATE[field], before[field], field)
        self.assertIn('IPC:C04B35/64', state['classification_view']['keys'])
        self.assertIn('キーワードが空欄', state['classification_view']['note'])

    def test_manual_keyword_limit_remains_independent_from_lab_limit(self):
        words = ' '.join('材料' + str(i) + 'あ' * 310 for i in range(7))
        self.assertTrue(2000 < len(words) <= 3000)
        response = self.client.post('/api/candidates', json={'keywords': words})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['keywords'], words)
        response = self.client.post('/api/discovery/plan', json={'keywords': words})
        self.assertEqual(response.status_code, 400, 'The lab retains its separate input limit.')


if __name__ == '__main__':
    unittest.main()
