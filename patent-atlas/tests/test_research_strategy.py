import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import research_strategy as strategy
from prompt_templates import CLASSIFICATION_SYSTEM, DISCOVERY_SYSTEM, JUDGMENT_SYSTEM, PROMPT_VERSION


def brief(**values):
    return dict(entry_mode='target', purpose='技術動向の母集団作成', goal='固体電池の工程を調べる',
                keywords='電池', user_aspects=['界面'], target_ids=['p1'], **values)


def patents():
    return [dict(id='p1', title='電池の界面', abstract='硫化物電解質と電極との界面を加圧する工程。',
                 ipc=[], fi='H01M10/0562; H01M10/058', fterm=['5H029AJ06'], cpc=['H01M10/0562']),
            dict(id='p2', title='未選択の個別資料', abstract='この本文は送信しない', ipc=['A01B1/00'])]


def llm_result():
    return dict(purpose='技術動向の母集団作成', summary='界面と電池の観点を検討し、ターゲットの回収を確認する。',
                concepts=[dict(id='aspect1', name='界面', role='required', terms=['界面', 'interface'],
                               abstract_terms=['接触部'], reason='p1の要約に界面が記載されている。', evidence_ids=['p1']),
                          dict(id='keyword1', name='電池', role='required', terms=['電池', 'battery'],
                               abstract_terms=['蓄電装置'], reason='入力キーワードを保持し、表記ゆれを検討。', evidence_ids=['p1'])],
                questions=['製造工程も独立の必須観点にしますか。'])


class ResearchStrategyTests(unittest.TestCase):
    def test_offline_keeps_literal_inputs_and_actual_classifications(self):
        with patch('research_strategy.complete') as network:
            plan = strategy.plan_research(brief(), patents(), {'provider': 'offline'})
        network.assert_not_called()
        self.assertEqual(plan['method'], 'offline')
        self.assertEqual(plan['prompt_version'], PROMPT_VERSION)
        self.assertEqual(plan['concepts'][0]['terms'], ['界面'])
        self.assertEqual(plan['concepts'][0]['abstract_terms'], [])
        self.assertEqual(plan['concepts'][0]['evidence_ids'], ['p1'])
        self.assertEqual(plan['source_ids'], ['p1'])
        self.assertEqual(plan['sources'][0]['ipc'], [])
        self.assertEqual(plan['sources'][0]['fi'], ['H01M10/0562', 'H01M10/058'])
        self.assertEqual(plan['sources'][0]['fterm'], ['5H029AJ06'])
        self.assertFalse(plan['sources'][0]['claims_available'])
        json.dumps(plan, ensure_ascii=False, allow_nan=False)

    def test_offline_source_selection_does_not_claim_unseen_text(self):
        rows = patents()
        rows[0]['abstract'] = ''
        request = brief()
        request['user_aspects'] = ['未記載の性能']
        plan = strategy.plan_research(request, rows, {})
        self.assertEqual(plan['concepts'][0]['evidence_ids'], [])
        self.assertEqual(plan['sources'][0]['evidence_scope'], 'title_only')
        self.assertFalse(plan['sources'][0]['abstract_available'])
        self.assertTrue(any('要約がない' in q for q in plan['questions']))
        self.assertTrue(any('請求項全文' in q for q in plan['questions']))

    def test_offline_goal_only_returns_question_not_fake_terms(self):
        plan = strategy.plan_research(dict(goal='仕様がまだ曖昧な研究課題'), [], {})
        self.assertEqual(plan['concepts'], [])
        self.assertTrue(any('実在する代表特許' in q for q in plan['questions']))
        self.assertEqual(plan['sources'], [])

    def test_selected_seed_title_is_marked_as_literal_optional_not_claim(self):
        plan = strategy.plan_research(dict(entry_mode='target', target_ids=['p1']), patents(), {})
        self.assertEqual(plan['concepts'][0]['terms'], ['電池の界面'])
        self.assertEqual(plan['concepts'][0]['role'], 'optional')
        self.assertIn('原文', plan['concepts'][0]['reason'])
        self.assertEqual(plan['concepts'][0]['abstract_terms'], [])

    def test_full_brief_and_overflow_keywords_preserved(self):
        request = dict(keywords='電池 制御 温度', user_aspects=['観点' + str(i) for i in range(8)])
        plan = strategy.plan_research(request, [], {})
        self.assertEqual(len(plan['concepts']), 8)
        self.assertEqual(plan['brief']['keywords'], request['keywords'])
        self.assertEqual(plan['unmapped_keywords'], ['電池', '制御', '温度'])
        self.assertTrue(any('未割当' in q for q in plan['questions']))

    def test_phrase_keywords_are_not_merged_as_synonyms(self):
        plan = strategy.plan_research(dict(keywords='"solid electrolyte" 界面'), [], {})
        self.assertEqual([c['terms'] for c in plan['concepts']], [['solid electrolyte'], ['界面']])
        self.assertTrue(all(c['role'] == 'required' for c in plan['concepts']))

    def test_explicit_aspect_roles(self):
        plan = strategy.plan_research(dict(user_aspects=['必須: 界面', '任意: 工程', '除外: 玩具']), [], {})
        self.assertEqual([c['role'] for c in plan['concepts']], ['required', 'optional', 'exclude'])
        self.assertEqual(plan['concepts'][2]['terms'], ['玩具'])

    def test_llm_payload_only_contains_selected_sources_and_preserves_settings(self):
        request, rows = brief(), patents()
        settings = dict(provider='local', base_url='http://localhost:1234/v1', model='qwen', proxy='http://proxy.local:8080', api_key='test-only')
        before = copy.deepcopy((request, rows, settings))
        with patch('research_strategy.complete', return_value=llm_result()) as network:
            plan = strategy.plan_research(request, rows, settings, use_llm=True)
        sent_settings, system, payload = network.call_args.args
        self.assertEqual(sent_settings, settings)
        self.assertEqual(payload['sources'][0]['id'], 'p1')
        self.assertEqual(len(payload['sources']), 1)
        self.assertNotIn('未選択', json.dumps(payload, ensure_ascii=False))
        self.assertNotIn('test-only', json.dumps(payload, ensure_ascii=False))
        self.assertIn('anchor_concepts', payload)
        self.assertIn('response_schema', network.call_args.kwargs)
        self.assertEqual(plan['method'], 'llm')
        self.assertEqual((request, rows, settings), before)

    def test_adapter_cannot_mutate_callers_inputs(self):
        request, rows, settings = brief(), patents(), {'provider': 'local', 'nested': {'a': 1}}
        before = copy.deepcopy((request, rows, settings))

        def adapter(sent_settings, system, payload, **kwargs):
            sent_settings['nested']['a'] = 100
            payload['brief']['keywords'] = 'changed'
            payload['sources'][0]['ipc'].append('invented')
            return llm_result()

        with patch('research_strategy.complete', side_effect=adapter):
            plan = strategy.plan_research(request, rows, settings, use_llm=True)
        self.assertEqual((request, rows, settings), before)
        self.assertEqual(plan['sources'][0]['ipc'], [])
        self.assertEqual(plan['brief']['keywords'], '電池')

    def test_invalid_model_structures_fail_without_mutation(self):
        invalid = [None, [], {'concepts': []}]
        transformations = [
            lambda r: r.update(concepts=[]),
            lambda r: r.update(purpose='異なる目的'),
            lambda r: r.update(summary=''),
            lambda r: r.update(query='invented Boolean'),
            lambda r: r['concepts'][0].update(terms=[]),
            lambda r: r['concepts'][0].update(terms=['x'] * 13),
            lambda r: r['concepts'][0].update(role='maybe'),
            lambda r: r['concepts'][0].update(id='bad id'),
            lambda r: r['concepts'][0].update(evidence_ids=['p2']),
            lambda r: r['concepts'][0].update(evidence_ids=['invented']),
            lambda r: r['concepts'][0].update(evidence_ids=['p1', 'p1']),
            lambda r: r['concepts'][0].update(terms=['battery OR car']),
            lambda r: r['concepts'][0].update(terms=['EN_ALL:car']),
            lambda r: r['concepts'][0].update(name='変更された観点'),
            lambda r: r['concepts'][1].update(terms=['battery']),
            lambda r: r['concepts'][0].pop('reason'),
        ]
        for transform in transformations:
            result = llm_result()
            transform(result)
            invalid.append(result)
        request, rows, settings = brief(), patents(), {'provider': 'local'}
        before = copy.deepcopy((request, rows, settings))
        for result in invalid:
            with self.subTest(result=result), patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_research(request, rows, settings, use_llm=True)
        self.assertEqual((request, rows, settings), before)

    def test_duplicate_terms_are_safely_normalized_with_concrete_precedence(self):
        result = llm_result()
        result['concepts'][0].update(terms=['界面', 'Interface', '界面', 'interface'],
                                     abstract_terms=['INTERFACE', '接触部', '接触部', 'contact', 'CONTACT'])
        original = copy.deepcopy(result)
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(brief(), patents(), {}, use_llm=True)
        concept = plan['concepts'][0]
        self.assertEqual(concept['terms'], ['界面', 'Interface'])
        self.assertEqual(concept['abstract_terms'], ['接触部', 'contact'])
        self.assertTrue(any('重複を整理' in question for question in plan['questions']))
        self.assertEqual(result, original)

    def test_duplicate_raw_bounds_and_unique_combined_bounds_still_apply(self):
        invalid = [dict(terms=['界面'] * 13), dict(abstract_terms=['接触部'] * 13),
                   dict(terms=['語' + str(i) for i in range(12)], abstract_terms=['別語']),
                   dict(terms=['界面', '界面', None]), dict(abstract_terms=['接触部', '接触部', 'a OR b'])]
        for changes in invalid:
            result = llm_result()
            result['concepts'][0].update(changes)
            with self.subTest(changes=changes), patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_research(brief(), patents(), {}, use_llm=True)
        result = llm_result()
        terms = ['語' + str(i) for i in range(12)]
        result['concepts'][0].update(terms=terms, abstract_terms=terms)
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(brief(), patents(), {}, use_llm=True)
        self.assertEqual(plan['concepts'][0]['terms'], terms)
        self.assertEqual(plan['concepts'][0]['abstract_terms'], [])

    def test_keyword_spelling_is_preserved_only_if_exact_input_exists_in_raw_concrete_terms(self):
        request = brief()
        request['keywords'] = 'Battery'
        result = llm_result()
        result['concepts'][1].update(name='Battery', terms=['BATTERY', 'Battery', 'battery'], abstract_terms=['battery'])
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(request, patents(), {}, use_llm=True)
        self.assertEqual(plan['concepts'][1]['terms'], ['Battery'])
        self.assertEqual(plan['concepts'][1]['abstract_terms'], [])
        result['concepts'][1].update(terms=['battery'], abstract_terms=['Battery'])
        with patch('research_strategy.complete', return_value=result), self.assertRaisesRegex(ValueError, '入力キーワードを省略'):
            strategy.plan_research(request, patents(), {}, use_llm=True)

    def test_normalizing_terms_does_not_relax_role_evidence_or_anchor_validation(self):
        changes = [lambda r: r['concepts'][0].update(role='optional'),
                   lambda r: r['concepts'][0].update(evidence_ids=['invented']),
                   lambda r: r['concepts'][0].update(evidence_ids=['p1', 'p1']),
                   lambda r: r['concepts'][1].update(terms=['battery'])]
        for change in changes:
            result = llm_result()
            result['concepts'][0].update(terms=['界面', '界面'], abstract_terms=['界面'])
            change(result)
            with patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_research(brief(), patents(), {}, use_llm=True)

    def test_missing_anchor_is_restored_literally_without_matching_model_meaning(self):
        result = llm_result()
        result['concepts'][0]['id'] = 'c1'
        result['concepts'].pop()
        original = copy.deepcopy(result)
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(brief(), patents(), {}, use_llm=True)
        self.assertEqual([item['id'] for item in plan['concepts']], ['aspect1', 'keyword1', 'c1'])
        restored, keyword, model = plan['concepts']
        self.assertEqual(restored['terms'], ['界面'])
        self.assertEqual(restored['abstract_terms'], [])
        self.assertEqual(restored['evidence_ids'], ['p1'])
        self.assertIn('利用者の原文', restored['reason'])
        self.assertIn('未実施', restored['reason'])
        self.assertEqual(keyword['terms'], ['電池'])
        self.assertEqual(model['terms'], ['界面', 'interface'])
        self.assertEqual(model['abstract_terms'], ['接触部'])
        self.assertTrue(any('入力観点2件' in question for question in plan['questions']))
        self.assertEqual(result, original)

    def test_all_anchors_take_priority_over_extra_proposals_at_limit(self):
        request = brief()
        request.update(keywords='', user_aspects=['観点' + str(i) for i in range(8)])
        result = llm_result()
        result['concepts'] = [dict(result['concepts'][0], id='c1'), dict(result['concepts'][0], id='c2')]
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(request, patents(), {}, use_llm=True)
        self.assertEqual([item['id'] for item in plan['concepts']], ['aspect' + str(i) for i in range(1, 9)])
        self.assertTrue(all(item['abstract_terms'] == [] for item in plan['concepts']))
        self.assertTrue(any('追加提案2件を省略' in question for question in plan['questions']))

    def test_invalid_extra_proposal_is_not_hidden_by_anchor_capacity(self):
        request = brief()
        request.update(keywords='', user_aspects=['観点' + str(i) for i in range(8)])
        mutations = [dict(evidence_ids=['invented']), dict(role='exclude'), dict(terms=['a OR b']),
                     dict(terms=['語'] * 13), dict(id='bad id')]
        for changes in mutations:
            result = llm_result()
            result['concepts'] = [dict(result['concepts'][0], id='c1')]
            result['concepts'][0].update(changes)
            with self.subTest(changes=changes), patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_research(request, patents(), {}, use_llm=True)

    def test_captured_synthetic_local_response_preserves_inputs_and_separates_proposals(self):
        result = json.loads((Path(__file__).parent / 'fixtures' / 'research-plan-gemma-synthetic.json').read_text(encoding='utf-8'))
        request = dict(entry_mode='target', purpose=result['purpose'],
                       goal='電極と固体電解質の接触改善、加圧工程に注目する。',
                       keywords='電池 界面', user_aspects=[], target_ids=['TEST-001'])
        rows = [dict(id='TEST-001', title='試験用の固体電解質電池',
                     abstract='電極と固体電解質を加圧して接触を改善し、界面抵抗を低減する試験用の説明。',
                     ipc='H01M10/0562', fi='', fterm='', cpc='')]
        before = copy.deepcopy((result, request, rows))
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(request, rows, {}, use_llm=True)
        self.assertEqual(len(plan['concepts']), 8)
        self.assertEqual([item['id'] for item in plan['concepts']], ['keyword1', 'keyword2', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6'])
        self.assertEqual(plan['concepts'][0]['terms'], ['電池'])
        self.assertEqual(plan['concepts'][1]['terms'], ['界面'])
        self.assertTrue(all(item['abstract_terms'] == [] for item in plan['concepts'][:2]))
        self.assertEqual(plan['concepts'][2]['abstract_terms'], ['固体電解質'])
        self.assertTrue(any('入力観点2件' in question and '重複を整理' in question for question in plan['questions']))
        self.assertEqual((result, request, rows), before)
        from research_routes import concept_conditions
        node, _ = concept_conditions(plan, {'concepts': [
            dict(id=c['id'], role='required', terms=c['terms']) for c in plan['concepts'][:3]]})
        self.assertEqual(node.op, 'and')
        self.assertEqual([child.value for child in node.children[:2]], ['電池', '界面'])
        self.assertEqual(node.children[2].op, 'or')
        self.assertEqual([child.value for child in node.children[2].children], ['電極材料', '電極', '材料'])

    def test_unrequested_exclusion_rejected(self):
        result = llm_result()
        result['concepts'].append(dict(id='c1', name='除外候補', role='exclude', terms=['機械'], abstract_terms=[], reason='勝手な推測', evidence_ids=[]))
        with patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
            strategy.plan_research(brief(), patents(), {}, use_llm=True)

    def test_required_input_failure_occurs_before_network(self):
        invalid = [{}, {'entry_mode': 'bad', 'goal': 'test'}, {'entry_mode': [], 'goal': 'test'}, {'goal': 3},
                   {'keywords': 'a OR b'}, {'user_aspects': ['x'] * 9},
                   {'target_ids': ['unknown']}, {'target_ids': ['p1', 'p1']}]
        for request in invalid:
            with self.subTest(request=request), patch('research_strategy.complete') as network, self.assertRaises(ValueError):
                strategy.plan_research(request, patents(), {}, use_llm=True)
            network.assert_not_called()

    def test_purpose_boundary_matches_ui_and_api_at_300_characters(self):
        request = brief()
        request['purpose'] = '目' * 300
        result = llm_result()
        result['purpose'] = request['purpose']
        with patch('research_strategy.complete', return_value=result):
            plan = strategy.plan_research(request, patents(), {}, use_llm=True)
        self.assertEqual(plan['purpose'], request['purpose'])
        request['purpose'] += '的'
        with patch('research_strategy.complete') as network, self.assertRaisesRegex(ValueError, '300文字以内'):
            strategy.plan_research(request, patents(), {}, use_llm=True)
        network.assert_not_called()
        result['purpose'] += '的'
        with patch('research_strategy.complete', return_value=result), self.assertRaisesRegex(ValueError, '300文字以内'):
            strategy.plan_research(brief(), patents(), {}, use_llm=True)

    def test_unknown_evidence_without_sources_rejected(self):
        result = dict(purpose='探索・母集団作成', summary='課題から計画する', concepts=[
            dict(id='c1', name='観点', role='required', terms=['電池'], abstract_terms=[], reason='仮説', evidence_ids=['p1'])], questions=[])
        with patch('research_strategy.complete', return_value=result), self.assertRaises(ValueError):
            strategy.plan_research(dict(goal='電池技術'), patents(), {}, use_llm=True)

    def test_prompt_contracts_preserve_existing_response_shapes(self):
        self.assertIn('"candidates"', CLASSIFICATION_SYSTEM)
        self.assertIn('"decisions"', JUDGMENT_SYSTEM)
        self.assertIn('required_decision_count', JUDGMENT_SYSTEM)
        self.assertIn('unsure', JUDGMENT_SYSTEM)
        self.assertIn('criteriaを優先', JUDGMENT_SYSTEM)
        self.assertIn('"facets"', DISCOVERY_SYSTEM)
        self.assertIn('"english_terms"', DISCOVERY_SYSTEM)
        self.assertNotEqual(DISCOVERY_SYSTEM, CLASSIFICATION_SYSTEM)


if __name__ == '__main__':
    unittest.main()
