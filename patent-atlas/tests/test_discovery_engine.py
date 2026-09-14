"""Offline discovery correctness; no app state, network, or user files touched."""
import copy
import unittest

import discovery_engine as engine


def patent(identifier, ipc='H01M10/058', family='', **extra):
    return dict(id=identifier, title='Solid-state battery sintering interface',
                abstract='A zirconate layer improves conductivity.', ipc=ipc,
                fterm='', family=family, label=None, label_source=None, **extra)


class DiscoveryEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = engine.propose_plan('全固体電池', [], [])

    def test_plan_has_independent_facets_and_unrestricted_topic_search(self):
        self.assertEqual({f['id'] for f in self.plan['facets']},
                         {'device', 'material', 'process', 'interface', 'performance', 'application'})
        self.assertIn('all-solid-state battery', self.plan['english_terms'])
        self.assertEqual(len(self.plan['queued_queries']), 7)
        for query in self.plan['queued_queries']:
            self.assertIn('ta = "all-solid-state battery"', query['query'])
            self.assertEqual(query['ipc_codes'], [])
        codes = [r['code'] for r in self.plan['candidate_hypotheses']]
        self.assertTrue(any(c.startswith('C01') for c in codes))
        self.assertTrue(any(c.startswith('C04') for c in codes))

    def test_dictionary_and_llm_hypotheses_never_claim_patent_support(self):
        plan = engine.propose_plan('battery', [], [], dict(candidate_hypotheses=[
            dict(kind='IPC', code='H99Z9999/999999', title='Invented classification'),
            dict(kind='IPC', code='H01M10/058', title='Invented title'),
        ]))
        self.assertNotIn('H99Z9999/999999', [r['code'] for r in plan['candidate_hypotheses']])
        for row in plan['candidate_hypotheses']:
            self.assertTrue(row['verified'])
            self.assertEqual(row['evidence_status'], 'hypothesis')
            self.assertEqual(row['support_count'], 0)
            self.assertEqual(row['evidence'], [])
            self.assertNotEqual(row['title'], 'Invented title')

    def test_empty_workspace_keeps_core_and_process_and_gains_adjacent_hypotheses(self):
        rows = {row['code']: row for row in self.plan['candidate_hypotheses']}
        self.assertIn('H01M10/0562', rows)
        self.assertIn('H01M4/139', rows)
        self.assertIn('process', rows['H01M4/139']['facets'])
        self.assertIn('C04B35/64', rows)
        self.assertTrue(any(row['code'].startswith('C01') and 'material' in row['facets'] for row in rows.values()))
        for code in ('C01D13/00', 'B23H3/00', 'B23H3/04', 'H01M8/04291', 'H01M8/18'):
            self.assertNotIn(code, rows)

    def test_dictionary_matching_needs_nearby_definitions_not_class_headings_or_crossrefs(self):
        lithium = {r['code'] for r in engine._dictionary_matches('lithium compounds', limit=200)}
        self.assertIn('C01D15/00', lithium)
        self.assertNotIn('C01D13/00', lithium)
        coating = {r['code'] for r in engine._dictionary_matches('coating electrodes', limit=200)}
        self.assertFalse(any(code.startswith('B23H') for code in coating))
        solid = {r['code'] for r in engine._dictionary_matches('solid electrolyte', limit=200)}
        self.assertIn('H01M10/0562', solid)
        top_solid = {r['code'] for r in engine._dictionary_matches('solid electrolyte')}
        self.assertNotIn('H01M8/04291', top_solid)

    def test_untranslated_japanese_never_becomes_an_unrelated_english_query(self):
        plan = engine.propose_plan('未知の新規技術', [], [])
        self.assertEqual(plan['english_terms'], [])
        self.assertEqual(plan['queued_queries'], [])
        self.assertTrue(any('英語' in warning for warning in plan['warnings']))

    def test_llm_query_injection_and_clause_budget(self):
        malicious = 'battery" or pn = "*'
        plan = engine.propose_plan('battery', [], [], dict(
            english_terms=[malicious] + ['term ' + str(i) for i in range(12)],
            facets=[dict(id='process', english_terms=['manufacturing', malicious, 'sintering'])]))
        for query in plan['queued_queries']:
            self.assertNotIn('pn =', query['query'])
            self.assertLessEqual(query['query'].count('ta = '), 10)
            self.assertIn('ta = "battery"', query['query'])

    def test_llm_proposal_records_only_applied_additions_and_query_destinations(self):
        plan = engine.propose_plan('新しい電池技術', [], [], dict(
            english_terms=['model battery'],
            facets=[dict(id='process', terms=['圧延'], english_terms=['calendering'])]),
            manual_english_terms=['manual battery'])
        self.assertEqual(plan['english_terms'], ['manual battery', 'model battery'])
        proposal = plan['llm_proposal']
        self.assertTrue(proposal['used'])
        self.assertTrue(proposal['created_at'])
        self.assertEqual(proposal['english_terms'], ['model battery'])
        self.assertEqual(proposal['facets'], [dict(id='process', label='製造・プロセス',
                                                  terms=['圧延'], english_terms=['calendering'])])
        self.assertEqual(proposal['applied_counts'], dict(english_terms=1, facets=1, facet_terms=2, classifications=0))
        self.assertEqual(proposal['query_ids'], [query['id'] for query in plan['queued_queries']])
        self.assertIn('calendering', next(query['query'] for query in plan['queued_queries'] if query['facet_id'] == 'process'))
        self.assertNotIn('raw', proposal)

    def test_llm_duplicate_only_is_visible_as_used_without_new_additions(self):
        plan = engine.propose_plan('battery', [], [], dict(english_terms=['battery'],
                                  facets=[dict(id='process', terms=['製造'], english_terms=['manufacturing'])]))
        proposal = plan['llm_proposal']
        self.assertTrue(proposal['used'])
        self.assertEqual(proposal['english_terms'], [])
        self.assertEqual(proposal['facets'], [])
        self.assertEqual(proposal['query_ids'], [])
        self.assertTrue(any('新しく追加' in notice for notice in proposal['notices']))
        self.assertFalse(engine.propose_plan('battery', [], [])['llm_proposal']['used'])

    def test_malformed_llm_shapes_cannot_be_silently_used_as_default_plan(self):
        bad = [{}, [], {'english_terms': 'battery'}, {'english_terms': [1]},
               {'english_terms': []}, {'candidates': []}, {'facets': 'process'},
               {'facets': [{'id': 'process', 'terms': '製造'}]},
               {'facets': [{'id': 'process'}]}, {'candidate_hypotheses': ['H01M']}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.propose_plan('battery', [], [], llm_plan=value)

    def test_term_budget_and_unknown_facets_explain_why_not_applied(self):
        plan = engine.propose_plan('未知の技術', [], [], dict(
            english_terms=['model topic', 'unsafe" query'],
            facets=[dict(id='unknown', terms=['対象外'])]),
            manual_english_terms=['manual ' + str(i) for i in range(7)])
        proposal = plan['llm_proposal']
        self.assertEqual(proposal['english_terms'], ['model topic'])
        self.assertEqual(proposal['query_ids'], [])
        self.assertEqual(proposal['facets'], [])
        self.assertTrue(any('model topic' in notice and '未使用' in notice for notice in proposal['notices']))
        self.assertTrue(any('対応しない観点' in notice for notice in proposal['notices']))
        self.assertFalse(any('unsafe' in query['query'] for query in plan['queued_queries']))

    def test_llm_classification_addition_has_traceable_verified_key(self):
        plan = engine.propose_plan('新しい技術', [], [], dict(candidate_hypotheses=[
            dict(kind='IPC', code='H01M10/058'), dict(kind='IPC', code='H99Z9999/999999')]))
        self.assertEqual(plan['llm_proposal']['candidate_keys'], ['IPC:H01M10/058'])
        self.assertEqual(plan['llm_proposal']['received_counts']['classifications'], 2)
        self.assertEqual(plan['llm_proposal']['applied_counts']['classifications'], 1)

    def test_real_metadata_deduplicates_families_and_preserves_human_labels(self):
        rows = [patent('EP111A1', family='f1'), patent('US222A1', family='f1'), patent('JP333A1', family='f2')]
        rows[0].update(label='keep', label_source='human')
        before = copy.deepcopy(rows)
        result = engine.analyze_patents(self.plan, rows + [rows[0]])
        row = result['recommendations'][0]
        self.assertEqual(row['support_count'], 3)
        self.assertEqual(row['family_count'], 2)
        self.assertEqual(row['positive_count'], 1)
        self.assertEqual(row['positive_family_count'], 1)
        self.assertEqual(row['relevance_status'], 'human_supported')
        self.assertEqual(len(row['evidence']), 2)
        self.assertEqual(rows, before)

    def test_human_exclusions_are_negative_only_and_generate_no_not_clause(self):
        rows = [patent('keep'), patent('drop'), patent('other', ipc='G06V10/00')]
        for row in rows[1:]:
            row.update(label='exclude', label_source='human', title='unrelated endoscope terminology', abstract='')
        result = engine.analyze_patents(self.plan, rows)
        self.assertEqual(len(result['recommendations']), 1)
        rec = result['recommendations'][0]
        self.assertEqual(rec['support_count'], 1)
        self.assertEqual(rec['negative_count'], 1)
        self.assertEqual(rec['excluded_patent_ids'], ['drop'])
        self.assertNotIn('endoscope', [t['term'] for t in result['new_terms']])
        self.assertTrue(all(' not ' not in q['query'] for q in result['next_queries']))

    def test_title_alone_is_not_classification_evidence(self):
        result = engine.analyze_patents(self.plan, [patent('x', ipc='')])
        self.assertEqual(result['recommendations'], [])
        self.assertTrue(result['new_terms'])

    def test_unknown_cpc_and_ipc_are_never_conflated(self):
        result = engine.analyze_patents(self.plan, [patent('x', cpc='H01M10/058')])
        rows = {r['key']: r for r in result['recommendations']}
        self.assertEqual(set(rows), {'IPC:H01M10/058', 'CPC:H01M10/058'})
        self.assertTrue(rows['IPC:H01M10/058']['verified'])
        self.assertFalse(rows['CPC:H01M10/058']['verified'])
        self.assertEqual(rows['CPC:H01M10/058']['relevance_status'], 'review_pending')

    def test_malformed_classification_is_reported_not_partially_inferred(self):
        result = engine.analyze_patents(self.plan, [patent('x', ipc='H01M10/058 BROKEN')])
        self.assertEqual(result['recommendations'], [])
        self.assertTrue(any('未解析' in w for w in result['warnings']))

    def test_synthetic_documents_are_marked_and_do_not_drive_ipc_queries(self):
        result = engine.analyze_patents(self.plan, [patent('DEMO-01-001')])
        row = result['recommendations'][0]
        self.assertEqual(row['synthetic_count'], 1)
        self.assertEqual(row['real_family_count'], 0)
        self.assertTrue(row['evidence'][0]['synthetic'])
        self.assertFalse(any(q['ipc_codes'] for q in result['next_queries']))

    def test_recursion_is_topic_anchored_stable_bounded_and_counts_new_evidence(self):
        rows = [patent('EP111A1', family='f1'), patent('EP222A1', ipc='C04B35/00', family='f2')]
        first = engine.analyze_patents(self.plan, rows)
        snapshot = copy.deepcopy(first)
        second = engine.analyze_patents(self.plan, rows, previous=first)
        self.assertEqual(second['counts']['new_documents'], 0)
        self.assertEqual(second['counts']['new_families'], 0)
        self.assertEqual(second['counts']['new_classifications'], 0)
        self.assertTrue(second['no_new_evidence'])
        self.assertEqual(first, snapshot)
        self.assertEqual(first['next_queries'], second['next_queries'])
        self.assertLessEqual(len(first['next_queries']), 12)
        self.assertTrue(any(q['ipc_codes'] == ['C04B35/00'] for q in first['next_queries']))
        for query in first['next_queries']:
            self.assertIn('ta = "all-solid-state battery"', query['query'])
        third = engine.analyze_patents(self.plan, rows + [patent('EP333A1', family='f3')], previous=second)
        self.assertEqual(third['counts']['new_documents'], 1)
        self.assertEqual(third['counts']['new_families'], 1)

    def test_missing_families_explicitly_fall_back_to_publication(self):
        result = engine.analyze_patents(self.plan, [patent('a'), patent('b')])
        self.assertEqual(result['counts']['families'], 2)
        self.assertEqual(result['counts']['family_fallback_documents'], 2)
        self.assertTrue(any('ファミリー情報' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
