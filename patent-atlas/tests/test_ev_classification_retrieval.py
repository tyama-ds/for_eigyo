"""Regression: EV keywords -> fresh, independently verified IPC/FI -> query."""
import copy
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import test_app as integration
import test_invention_routes as invention_tests
import classification_catalog as ipc
import classification_retrieval as retrieval
import fi_catalog
from discovery_engine import propose_plan
from query_formats import export_query
from query_library import parse_example

module = integration.module


class EVRetrievalTests(unittest.TestCase):
    def test_vehicle_is_an_example_not_a_fixed_profile(self):
        rows = retrieval.retrieve_elements([dict(id='e1',name='充電設備',terms=['charging electric vehicles'])])
        self.assertTrue(any(r['code'].startswith('B60L53/') for r in rows))
        self.assertEqual(retrieval.retrieve('unidentifiedquuxtechnology'), [])
        self.assertFalse(hasattr(retrieval, 'EV_ENTRIES'))
        self.assertTrue(all(r['matched_elements'] for r in rows))

    def test_external_query_keeps_technical_phrase_and_literal_ja_is_searchable(self):
        plan = propose_plan('electric vehicle', [], [])
        broad = plan['queued_queries'][0]
        self.assertIn('ta = "electric vehicle"', broad['query'])
        self.assertNotIn('ta = "electric"', broad['query'])
        self.assertNotIn('ta = "vehicle"', broad['query'])
        self.assertIn('B60L53/00', {r['code'] for r in ipc.search('IPC', '充電')})
        self.assertEqual(retrieval.phrases('"electric vehicle" "battery charging"'), ['electric vehicle','battery charging'])

    def test_fi_is_verified_independently_and_keeps_suffix_and_export_field(self):
        row = module.explorer.enrich(dict(kind='FI', code='B60L15/20 A'))
        self.assertEqual(row['code'], 'B60L15/20@A')
        self.assertTrue(row['verified'])
        self.assertIsNone(ipc.lookup('IPC', row['code']))
        self.assertTrue(fi_catalog.metadata()['complete'])
        self.assertTrue(module.explorer.enrich(dict(kind='FI', code='H01M10/0562'))['verified'])
        q = dict(boolean_tree=dict(op='class', system='FI', value=row['code']), classifications=[row])
        exported = export_query(q, 'jplatpat')
        self.assertTrue(exported['can_copy'], exported)
        self.assertEqual(exported['expression'], 'B60L15/20@A/FI')
        imported = parse_example(exported['expression'])
        self.assertEqual(imported['status'], 'editable')
        self.assertEqual(imported['query']['boolean_tree'], q['boolean_tree'])
        self.assertFalse(export_query(q, 'espacenet')['can_copy'])
        codes = module.explorer.parse_codes('FI', 'B60L50/60; B60L15/20 A\nB60L7/10')
        self.assertEqual(codes, ['B60L50/60','B60L15/20@A','B60L7/10'])

    def test_model_receives_official_context_and_cannot_invent_fi(self):
        def respond(_settings, _system, payload, **kwargs):
            if 'technology' in payload and 'provided_candidates' not in payload:
                return dict(elements=[dict(id='e1',name='車両の充電',terms=['charging electric vehicles'],relation='車両への電力供給')],questions=[])
            self.assertIn('FI:B60L53/00', [r['key'] for r in payload['provided_candidates']])
            return dict(selections=[dict(key='FI:B60L53/00',element_ids=['e1'],relation='direct',reason='車両充電の定義')])
        with patch.object(module, 'complete', side_effect=respond) as llm:
            rows = module.candidate_list('電気自動車', True)
        self.assertEqual(llm.call_count, 2)
        payload = llm.call_args.args[2]
        self.assertTrue(any(r['key']=='FI:B60L53/00' and r['definition'] for r in payload['provided_candidates']))
        self.assertTrue(next(r for r in rows if r['key']=='FI:B60L53/00')['llm_proposed'])
        with self.assertRaises(ValueError):
            module.llm_candidate_rows({'candidates':[{'kind':'FI','code':'B60L99/99'}]}, [])
        lab = propose_plan('電気自動車', [], [], {'candidate_hypotheses':[
            {'kind':'FI', 'code':'B60L53/00', 'terms':None, 'facets':None,
             'origins':[{'type':'patent','patent_id':'invented'}], 'title':'invented'}]})
        row = next(r for r in lab['candidate_hypotheses'] if r['key']=='FI:B60L53/00')
        self.assertFalse(row.get('origins'))
        self.assertNotEqual(row['title'], 'invented')


class EVInventionRouteTests(unittest.TestCase):
    setUp = invention_tests.InventionRoutesTests.setUp
    post = invention_tests.InventionRoutesTests.post
    payload = invention_tests.InventionRoutesTests.payload

    @classmethod
    def setUpClass(cls): cls.client = TestClient(module.app)

    def test_new_topic_refreshes_pool_and_fi_reaches_adopted_query(self):
        module.STATE['candidates'].append(module.explorer.enrich(dict(kind='IPC', code='H01M10/0562')))
        before = copy.deepcopy(module.STATE['patents'])
        body = dict(brief=dict(entry_mode='discover', purpose='技術動向', goal='電気自動車の充電',
                    keywords='電気自動車', user_aspects=[], target_ids=[]),
                    manual_elements=['electric propulsion', 'charging electric vehicles'], use_llm=False)
        result = self.post('invention/plan', body)
        plan = result['research_workbench']['invention']['plan']
        candidates = {c['key']: c for c in plan['classification_candidates']}
        self.assertTrue(any(k.startswith('IPC:B60L') for k in candidates))
        self.assertIn('FI:B60L53/00', candidates)
        self.assertNotIn('IPC:H01M10/0562', candidates)
        self.assertIn('IPC:B60', candidates, 'An explicitly selected seed is retained')
        selected = self.payload(plan)
        selected['elements'][0]['perspectives'][0].update(classification_keys=['FI:B60L53/00'], class_mode='class')
        preview = self.post('invention/preview', selected)
        self.assertEqual(export_query(preview['query'], 'jplatpat')['expression'], 'B60L53/00/FI')
        result = self.post('invention/apply', dict(selected, expected_preview_hash=preview['preview_hash']))
        self.assertEqual(result['queries'][-1]['boolean_tree']['system'], 'FI')
        self.assertEqual(module.STATE['patents'], before)
        self.assertEqual(module.SETTINGS, self.settings)

    def test_manual_screen_browses_and_selects_fi_without_auto_selecting_candidates(self):
        selected = copy.deepcopy(module.STATE['selected'])
        response = self.client.post('/api/candidates', json={'keywords':'電気自動車', 'use_llm':False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(module.STATE['selected'], selected)
        response = self.client.post('/api/classifications/browse', json={'kind':'FI','direction':'children','code':'B60L15/20'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('FI:B60L15/20@A', response.json()['classification_view']['keys'])
        response = self.client.post('/api/selection', json={'selected':['FI:B60L15/20@A']})
        self.assertEqual(response.status_code, 200)
        q = module.make_query()
        self.assertIn('B60L15/20@A/FI', export_query(q, 'jplatpat')['expression'])


if __name__ == '__main__': unittest.main()
