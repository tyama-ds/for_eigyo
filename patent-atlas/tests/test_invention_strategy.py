"""Semantic checks for element-specific searches, evidence, and scoped NOT."""
import copy
import itertools
import unittest
from unittest.mock import patch

import invention_strategy as strategy
from query_formats import export_query


def candidates():
    return [dict(key='IPC:B60W30/00', kind='IPC', code='B60W30/00', title='Vehicle control', selectable=True, verified=True),
            dict(key='IPC:G01S13/00', kind='IPC', code='G01S13/00', title='Radar', selectable=True, verified=True),
            dict(key='CPC:G01S13/86', kind='CPC', code='G01S13/86', title='Sensor fusion', selectable=True, verified=True),
            dict(key='CPC:G01S', kind='CPC', code='G01S', title='CPC heading', selectable=False)]


def sources():
    return [dict(id='p1', title='センサを用いる車両制御', abstract='レーダの測距結果に基づいて車両を停止する。')]


def request(**changes):
    result = dict(purpose='先行技術探索', goal='検知と制御の関係を調べる', keywords='車両 自動運転',
                  invention_text='レーダで障害物を検知し、検知結果に基づいて車両を停止する。',
                  manual_elements=['レーダ', '車両を停止'], user_aspects=[])
    result.update(changes)
    return result


def manual_plan(**changes):
    return strategy.plan_invention(request(**changes), sources(), candidates(), {})


def model_result():
    return dict(purpose='先行技術探索', summary='検知部と制御部をそれぞれ検索し、作用関係も検討する。', questions=[], elements=[
        dict(id='e1', name='障害物の検知', description='レーダで障害物を検知する。', relation='検知結果を制御部へ渡す。',
             evidence=[dict(source_id='invention_text', quote='レーダで障害物を検知')], perspectives=[
                 dict(id='e1p1', name='検知手段', terms=['レーダ', 'radar'], classification_keys=['IPC:G01S13/00'],
                      reason='入力に記載された測距手段から検索する。'),
                 dict(id='e1p2', name='検知機能', terms=['障害物検知'], classification_keys=[], reason='検知機能から検索する。')]),
        dict(id='e2', name='車両の停止制御', description='検知結果に基づいて車両を停止する。', relation='e1の検知結果を受けて停止する。',
             evidence=[dict(source_id='p1', quote='レーダの測距結果に基づいて車両を停止する。')], perspectives=[
                 dict(id='e2p1', name='制御動作', terms=['車両停止', 'vehicle stopping'], classification_keys=['IPC:B60W30/00'],
                      reason='要約に記載された停止動作から検索する。')])])


def payload(plan, ids=None, *, strategy_name='element'):
    ids = ids or [plan['elements'][0]['id']]
    return dict(strategy=strategy_name, element_ids=ids,
                elements=[dict(id=e['id'], perspectives=[dict(id=p['id'], terms=copy.deepcopy(p['terms']),
                    classification_keys=copy.deepcopy(p['classification_keys']), class_mode='text')
                    for p in e['perspectives']]) for e in plan['elements'] if e['id'] in ids],
                perspective_joins={}, exclusions=[], confirm_exclusions=False, confirm_class_intersection=False)


def matches(node, atoms):
    if node['op'] == 'text':
        return node['value'] in atoms
    if node['op'] == 'class':
        return node['system'] + ':' + node['value'] in atoms
    values = [matches(child, atoms) for child in node['children']]
    if node['op'] == 'not':
        return values[0] and not values[1]
    return all(values) if node['op'] == 'and' else any(values)


class PlanningTests(unittest.TestCase):
    def test_technical_description_can_start_seedless_decomposition_with_exact_quote(self):
        data = request(goal='レーダで障害物を検知し車両を停止する。', invention_text='', manual_elements=[])
        result = model_result()
        result['elements'][0]['evidence'] = [dict(source_id='research_goal', quote='レーダで障害物を検知')]
        result['elements'][1]['evidence'] = [dict(source_id='research_goal', quote='車両を停止する。')]
        with patch.object(strategy, 'complete', return_value=result):
            plan = strategy.plan_invention(data, [], candidates(), {}, use_llm=True)
        self.assertEqual(plan['elements'][0]['evidence_status'], 'text_match')
        self.assertEqual(plan['input']['goal'], data['goal'])

    def test_manual_preserves_input_without_automatic_decomposition(self):
        data = request(manual_elements=[' レーダ ', '車両を停止'], user_aspects=['必須: 車両'])
        original = copy.deepcopy(data)
        source_rows, pool = sources(), candidates()
        with patch('invention_strategy.complete') as network:
            plan = strategy.plan_invention(data, source_rows, pool, {})
        network.assert_not_called()
        self.assertEqual(plan['method'], 'manual')
        self.assertEqual(plan['input'], original)
        self.assertEqual(data, original)
        self.assertEqual([e['id'] for e in plan['elements']], ['e1', 'e2'])
        self.assertEqual([e['perspectives'][0]['terms'] for e in plan['elements']], [['レーダ'], ['車両を停止']])
        self.assertIn('自動分解は未実施', plan['summary'])
        self.assertEqual(plan['elements'][0]['evidence_status'], 'text_match')
        self.assertIn('文字列一致', plan['elements'][0]['evidence_note'])
        plan['input']['manual_elements'].append('編集')
        plan['sources'][0]['title'] = '編集'
        self.assertEqual(data, original)
        self.assertEqual(source_rows, sources())
        self.assertEqual(pool, candidates())

    def test_manual_does_not_split_keywords_or_source_sentences(self):
        plan = manual_plan(manual_elements=[])
        self.assertEqual(plan['elements'], [])
        self.assertIn('1行に1要素', ' '.join(plan['questions']))

    def test_manual_long_or_boolean_text_is_preserved_as_description_for_editing(self):
        values = ['レーダ AND カメラ', '要素' * 150]
        plan = manual_plan(manual_elements=values)
        self.assertEqual([element['description'] for element in plan['elements']], values)
        self.assertTrue(all(element['perspectives'][0]['terms'] == [] for element in plan['elements']))
        self.assertTrue(all(len(element['name']) <= 160 for element in plan['elements']))
        with self.assertRaisesRegex(ValueError, '空'):
            strategy.compose_invention_query(plan, payload(plan), candidates())

    def test_missing_evidence_is_explicitly_hypothetical(self):
        plan = manual_plan(manual_elements=['光学センサ'])
        element = plan['elements'][0]
        self.assertEqual(element['evidence'], [])
        self.assertEqual(element['evidence_status'], 'hypothesis')
        self.assertIn('仮説', element['evidence_note'])

    def test_llm_mock_validates_evidence_and_passes_bounded_schema(self):
        result = model_result()
        with patch('invention_strategy.complete', return_value=result) as network:
            plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {'provider': 'local'}, True)
        self.assertEqual(plan['method'], 'llm')
        self.assertEqual(plan['prompt_version'], 'invention-2026-09-18-v1')
        self.assertEqual(plan['elements'][1]['evidence'][0]['source_id'], 'p1')
        args, kwargs = network.call_args
        self.assertIn('思考過程は出力せず', args[1])
        self.assertEqual(args[2]['input']['keywords'], '車両 自動運転')
        self.assertNotIn('CPC:G01S', [row['key'] for row in args[2]['provided_classifications']])
        self.assertEqual(kwargs['response_schema']['properties']['elements']['maxItems'], 8)
        self.assertEqual(result, model_result())

    def test_llm_manual_anchors_cannot_be_dropped_or_changed(self):
        for elements in (model_result()['elements'], []):
            result = model_result()
            result['elements'] = elements
            with self.subTest(elements=elements), patch('invention_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_invention(request(), sources(), candidates(), {}, True)
        result = model_result()
        for element, manual in zip(result['elements'], request()['manual_elements']):
            element.update(name=manual, description=manual)
        with patch('invention_strategy.complete', return_value=result):
            plan = strategy.plan_invention(request(), sources(), candidates(), {}, True)
        self.assertEqual([e['description'] for e in plan['elements']], request()['manual_elements'])

    def test_direct_element_term_remains_an_editable_or_alternative(self):
        data = request(manual_elements=[])
        result = model_result()
        result['elements'][0]['name'] = '冷却ファン'
        result['elements'][0]['perspectives'][0]['terms'] = ['ヒートシンク', '蒸発冷却', '液体冷却']
        before_result, before_data = copy.deepcopy(result), copy.deepcopy(data)
        with patch.object(strategy, 'complete', return_value=result):
            plan = strategy.plan_invention(data, sources(), candidates(), {}, True)
        first = plan['elements'][0]['perspectives'][0]
        self.assertEqual(first['terms'], ['冷却ファン', 'ヒートシンク', '蒸発冷却', '液体冷却'])
        self.assertEqual(first['anchor_term'], '冷却ファン')
        self.assertEqual(first['reason'], result['elements'][0]['perspectives'][0]['reason'])
        self.assertNotIn('anchor_term', plan['elements'][0]['perspectives'][1])
        body = payload(plan)
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        tree = strategy.compose_invention_query(plan, body, candidates())['boolean_tree']
        self.assertTrue(matches(tree, {'冷却ファン'}))
        self.assertTrue(matches(tree, {'ヒートシンク'}))
        self.assertFalse(matches(tree, {'無関係'}))
        body['elements'][0]['perspectives'][0]['terms'].remove('冷却ファン')
        edited = strategy.compose_invention_query(plan, body, candidates())['boolean_tree']
        self.assertFalse(matches(edited, {'冷却ファン'}))
        self.assertEqual(result, before_result)
        self.assertEqual(data, before_data)

    def test_direct_element_term_does_not_duplicate_existing_spelling(self):
        result = model_result()
        result['elements'][0]['name'] = 'Cooling fan'
        result['elements'][0]['perspectives'][0]['terms'] = ['冷却ファン', 'cooling fan']
        with patch.object(strategy, 'complete', return_value=result):
            plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)
        first = plan['elements'][0]['perspectives'][0]
        self.assertEqual(first['terms'], ['冷却ファン', 'cooling fan'])
        self.assertEqual(first['anchor_term'], 'Cooling fan')

    def test_full_term_list_is_preserved_and_missing_element_name_is_flagged(self):
        for existing_anchor in (False, True):
            result = model_result()
            result['elements'][0]['name'] = '冷却ファン'
            terms = [f'語句{number}' for number in range(12)]
            if existing_anchor:
                terms[5] = '冷却ファン'
            result['elements'][0]['perspectives'][0]['terms'] = terms
            with self.subTest(existing_anchor=existing_anchor), patch.object(strategy, 'complete', return_value=result):
                plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)
            first = plan['elements'][0]['perspectives'][0]
            self.assertEqual(first['terms'], terms)
            self.assertEqual(len(first['terms']), 12)
            if existing_anchor:
                self.assertEqual(first['anchor_term'], '冷却ファン')
            else:
                self.assertNotIn('anchor_term', first)
                self.assertTrue(any('冷却ファン' in question and '12語' in question for question in plan['questions']))

    def test_expression_like_element_names_are_not_inserted_as_search_terms(self):
        for name in ('冷却ファン OR ヒートシンク', '冷却ファン/TX', '(冷却ファン)'):
            result = model_result()
            result['elements'][0]['name'] = name
            before = copy.deepcopy(result)
            with self.subTest(name=name), patch.object(strategy, 'complete', return_value=result):
                plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)
            first = plan['elements'][0]['perspectives'][0]
            self.assertEqual(first['terms'], result['elements'][0]['perspectives'][0]['terms'])
            self.assertNotIn('anchor_term', first)
            self.assertTrue(any(name in question and '自動追加できません' in question for question in plan['questions']))
            self.assertEqual(result, before)

    def test_hallucinated_unknown_or_cross_field_quotes_fail(self):
        for evidence in ([dict(source_id='invention_text', quote='カメラで障害物を検知')],
                         [dict(source_id='unselected', quote='レーダ')],
                         [dict(source_id='p1', quote='車両制御\nレーダ')],
                         [dict(source_id='invention_text', quote='')]):
            result = model_result()
            result['elements'][0]['evidence'] = evidence
            with self.subTest(evidence=evidence), patch('invention_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)

    def test_llm_missing_evidence_is_hypothesis_not_fabricated(self):
        result = model_result()
        result['elements'][0]['evidence'] = []
        with patch('invention_strategy.complete', return_value=result):
            plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)
        self.assertEqual(plan['elements'][0]['evidence_status'], 'hypothesis')
        self.assertIn('仮説', plan['elements'][0]['evidence_note'])

    def test_boundary_ellipsis_quotes_are_exact_fragments_with_audit_metadata(self):
        original = '温度センサで電池の温度を取得し、取得した温度に応じて冷却ファンの回転速度を変える制御部を備える。'
        data = request(manual_elements=[], invention_text=original)
        result = model_result()
        quotes = ['温度センサで電池の温度を取得し...', '…取得した温度に応じて冷却ファンの回転速度を変える制御部…']
        expected = ['温度センサで電池の温度を取得し', '取得した温度に応じて冷却ファンの回転速度を変える制御部']
        for element, quote in zip(result['elements'], quotes):
            element['evidence'] = [dict(source_id='invention_text', quote=quote)]
        before = copy.deepcopy(result)
        before_data = copy.deepcopy(data)
        with patch.object(strategy, 'complete', return_value=result):
            plan = strategy.plan_invention(data, sources(), candidates(), {}, True)
        for element, quote, fragment in zip(plan['elements'], quotes, expected):
            self.assertEqual(element['evidence'], [dict(source_id='invention_text', quote=fragment,
                original_quote=quote, quote_adjustment='boundary_ellipsis_removed')])
            self.assertEqual(element['evidence_status'], 'text_match')
            self.assertIn(fragment, original)
        self.assertEqual(result, before)
        self.assertEqual(data, before_data)

    def test_repeated_boundary_ellipsis_and_single_field_matching(self):
        fields = {'p1': ['温度センサで測定する', '電池温度に応じて冷却する']}
        for quote, expected in [('……温度センサ…', '温度センサ'),
                                ('...電池温度に応じて冷却する', '電池温度に応じて冷却する'),
                                ('温度センサ...', '温度センサ')]:
            with self.subTest(quote=quote):
                self.assertEqual(strategy._evidence([dict(source_id='p1', quote=quote)], fields),
                    [dict(source_id='p1', quote=expected, original_quote=quote,
                          quote_adjustment='boundary_ellipsis_removed')])
        with self.assertRaises(ValueError):
            strategy._evidence([dict(source_id='p1', quote='…測定する電池温度…')], fields)

    def test_literal_ellipsis_in_original_is_not_adjusted(self):
        for quote in ('温度センサ...', '…電池温度…', '温度…センサ', '……'):
            with self.subTest(quote=quote):
                evidence = [dict(source_id='p1', quote=quote)]
                self.assertEqual(strategy._evidence(evidence, {'p1': ['原文に' + quote + 'と記載']}), evidence)

    def test_boundary_correction_never_repairs_internal_or_fabricated_quotes(self):
        fields = {'p1': ['温度センサで電池の温度を取得し、冷却する。']}
        invalid = ['', '…', '……', '...', '温度…', '電池の...', '…    …',
                   '温度センサ...冷却する。', '…温度センサ…冷却する…',
                   '湿度センサ...', '温度センサで電池の温度を取得し。...',
                   '温度センサで電池の温度を取得し、冷却する!...']
        for quote in invalid:
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                strategy._evidence([dict(source_id='p1', quote=quote)], fields)
        with self.assertRaises(ValueError):
            strategy._evidence([dict(source_id='p1', quote='…温度センサ…冷却する…')],
                               {'p1': ['温度センサ…冷却する']})
        with self.assertRaises(ValueError):
            strategy._evidence([dict(source_id='unknown', quote='温度センサ...')], fields)
        with self.assertRaisesRegex(ValueError, '重複'):
            strategy._evidence([dict(source_id='p1', quote='温度センサ'),
                                dict(source_id='p1', quote='温度センサ...')], fields)

    def test_llm_purpose_unknown_refs_empty_conditions_and_excess_are_rejected(self):
        variants = []
        changed = model_result(); changed['purpose'] = '別の目的'; variants.append(changed)
        changed = model_result(); changed['elements'][0]['id'] = 'arbitrary'; variants.append(changed)
        changed = model_result(); changed['elements'][0]['perspectives'][0]['id'] = 'unknown'; variants.append(changed)
        for keys in (['IPC:A01B1/00'], ['CPC:G01S']):
            changed = model_result(); changed['elements'][0]['perspectives'][0]['classification_keys'] = keys; variants.append(changed)
        changed = model_result(); changed['elements'][0]['perspectives'][0].update(terms=[], classification_keys=[]); variants.append(changed)
        changed = model_result(); changed['elements'][0]['perspectives'][0]['terms'] = ['x'] * 13; variants.append(changed)
        changed = model_result(); changed['elements'][0]['perspectives'][0]['terms'] = ['radar OR camera']; variants.append(changed)
        changed = model_result(); changed['elements'] *= 5; variants.append(changed)
        for result in variants:
            with self.subTest(result=result), patch('invention_strategy.complete', return_value=result), self.assertRaises(ValueError):
                strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)

    def test_more_than_sixteen_perspectives_rejected_even_with_valid_ids(self):
        result = model_result()
        result['elements'] = []
        for index in range(1, 7):
            element = copy.deepcopy(model_result()['elements'][0])
            element['id'] = f'e{index}'
            element['perspectives'] = []
            for number in range(1, 4):
                perspective = copy.deepcopy(model_result()['elements'][0]['perspectives'][0])
                perspective['id'] = f'e{index}p{number}'
                element['perspectives'].append(perspective)
            result['elements'].append(element)
        with patch('invention_strategy.complete', return_value=result), self.assertRaisesRegex(ValueError, '16'):
            strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)

    def test_input_limits_and_types_fail_before_network(self):
        bad = [dict(invention_text='x' * 20001), dict(manual_elements=['x'] * 9),
               dict(manual_elements=['x' * 601]), dict(manual_elements=['a\nb']),
               dict(manual_elements=['a', ' a ']), dict(user_aspects=['x' * 161]), dict(keywords=[])]
        for changes in bad:
            with self.subTest(changes=changes), patch('invention_strategy.complete') as network, self.assertRaises(ValueError):
                strategy.plan_invention(request(**changes), sources(), candidates(), {}, True)
            network.assert_not_called()

    def test_sources_and_candidate_pool_are_bounded_and_known(self):
        for rows, pool in ((sources() * 21, candidates()), (sources(), candidates() * 400),
                           ([dict(id='invention_text', title='x')], candidates()),
                           (sources(), [dict(key='IPC:x', kind='IPC', code='y')])):
            with self.subTest(rows=rows, pool=pool), self.assertRaises(ValueError):
                strategy.plan_invention(request(), rows, pool, {})
        by_key = {row['key']: row for row in candidates()}
        self.assertEqual(strategy.plan_invention(request(), sources(), by_key, {})['method'], 'manual')


class CompositionTests(unittest.TestCase):
    def setUp(self):
        with patch('invention_strategy.complete', return_value=model_result()):
            self.plan = strategy.plan_invention(request(manual_elements=[]), sources(), candidates(), {}, True)

    def compose(self, body):
        return strategy.compose_invention_query(self.plan, body, candidates())

    def test_single_element_does_not_require_other_features_or_input_keywords(self):
        body = payload(self.plan)
        before_plan, before_body, before_candidates = copy.deepcopy(self.plan), copy.deepcopy(body), candidates()
        query = self.compose(body)
        self.assertTrue(matches(query['boolean_tree'], {'radar', '障害物検知'}))
        self.assertFalse(matches(query['boolean_tree'], {'radar', '車両停止', '自動運転'}))
        self.assertEqual(query['classifications'], [])
        self.assertEqual(query['invention_context']['element_ids'], ['e1'])
        self.assertEqual(len(query['invention_context']['elements']), 1)
        self.assertNotIn('concept_tree', query)
        self.assertNotIn('facets', query)
        self.assertEqual(self.plan, before_plan)
        self.assertEqual(body, before_body)
        self.assertEqual(candidates(), before_candidates)

    def test_explicit_perspective_subset_and_or_join_are_preserved(self):
        body = payload(self.plan)
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        query = self.compose(body)
        self.assertTrue(matches(query['boolean_tree'], {'radar'}))
        self.assertEqual(len(query['invention_context']['elements'][0]['perspectives']), 1)
        body = payload(self.plan)
        body['perspective_joins'] = {'e1': 'or'}
        query = self.compose(body)
        self.assertTrue(matches(query['boolean_tree'], {'障害物検知'}))
        self.assertTrue(matches(query['boolean_tree'], {'radar'}))
        self.assertFalse(matches(query['boolean_tree'], set()))

    def test_mixed_ipc_keyword_local_and_global_not_truth_table(self):
        body = payload(self.plan, ['e1', 'e2'], strategy_name='alternatives')
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        body['elements'][0]['perspectives'][0].update(terms=['radar'], class_mode='or')
        body['elements'][1]['perspectives'][0].update(terms=['braking'], class_mode='and')
        body['exclusions'] = [dict(scope='e1', terms=['toy']), dict(scope='all', terms=['rail'])]
        body.update(confirm_exclusions=True, confirm_class_intersection=True)
        query = self.compose(body)
        atoms = ['radar', 'IPC:G01S13/00', 'braking', 'IPC:B60W30/00', 'toy', 'rail']
        for bits in itertools.product((False, True), repeat=len(atoms)):
            present = {value for value, bit in zip(atoms, bits) if bit}
            expected = (((bits[0] or bits[1]) and not bits[4]) or (bits[2] and bits[3])) and not bits[5]
            self.assertEqual(matches(query['boolean_tree'], present), expected, present)
        self.assertTrue(matches(query['boolean_tree'], {'braking', 'IPC:B60W30/00', 'toy'}))
        self.assertFalse(matches(query['boolean_tree'], {'radar', 'toy'}))
        self.assertEqual(query['boolean_tree']['op'], 'not')
        self.assertEqual(query['boolean_tree']['children'][0]['op'], 'or')
        self.assertEqual(query['invention_context']['boolean_tree'], query['boolean_tree'])
        for output in ('espacenet', 'patentscope'):
            rendered = export_query(query, output)
            self.assertTrue(rendered['can_copy'], rendered)
        # The existing J-PlatPat exporter deliberately refuses >3 bracket
        # levels; a complex branch exclusion must not be flattened unsafely.
        rendered = export_query(query, 'jplatpat')
        self.assertFalse(rendered['can_copy'])
        self.assertTrue(any('3階層' in problem for problem in rendered['problems']))

    def test_combination_is_only_selected_elements_and_requires_all_selected(self):
        self.plan['elements'].append(dict(id='e3', name='温度補正', description='温度補正', relation='', evidence=[],
            perspectives=[dict(id='e3p1', name='温度', terms=['temperature'], classification_keys=[], reason='仮説')]))
        body = payload(self.plan, ['e1', 'e2'], strategy_name='combination')
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        query = self.compose(body)
        self.assertTrue(matches(query['boolean_tree'], {'radar', 'vehicle stopping'}))
        self.assertFalse(matches(query['boolean_tree'], {'radar', 'temperature'}))
        self.assertEqual(query['invention_context']['element_ids'], ['e1', 'e2'])

    def test_class_intersection_requires_explicit_bool_confirmation(self):
        # Text AND classification inside one perspective.
        body = payload(self.plan)
        body['elements'][0]['perspectives'][0]['class_mode'] = 'and'
        with self.assertRaisesRegex(ValueError, '分類の交差'):
            self.compose(body)
        body['confirm_class_intersection'] = True
        self.compose(body)
        # Two classification-bearing perspectives combined with AND.
        body = payload(self.plan)
        body['elements'][0]['perspectives'][0]['class_mode'] = 'or'
        body['elements'][0]['perspectives'][1].update(classification_keys=['CPC:G01S13/86'], class_mode='class')
        with self.assertRaisesRegex(ValueError, '分類の交差'):
            self.compose(body)
        body['perspective_joins'] = {'e1': 'or'}
        self.compose(body)
        # Two classification-bearing elements combined with AND.
        body = payload(self.plan, ['e1', 'e2'], strategy_name='combination')
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        for element in body['elements']:
            element['perspectives'][0]['class_mode'] = 'class'
        with self.assertRaisesRegex(ValueError, '分類の交差'):
            self.compose(body)
        body['strategy'] = 'alternatives'
        self.compose(body)

    def test_class_alternatives_do_not_become_and(self):
        body = payload(self.plan)
        body['elements'][0]['perspectives'] = body['elements'][0]['perspectives'][:1]
        body['elements'][0]['perspectives'][0].update(class_mode='class',
            classification_keys=['IPC:G01S13/00', 'CPC:G01S13/86'])
        query = self.compose(body)
        for key in ('IPC:G01S13/00', 'CPC:G01S13/86'):
            self.assertTrue(matches(query['boolean_tree'], {key}))
        self.assertEqual(len(query['classifications']), 2)

    def test_mode_default_is_text_and_unused_values_are_retained_but_validated(self):
        body = payload(self.plan)
        del body['elements'][0]['perspectives'][0]['class_mode']
        query = self.compose(body)
        self.assertEqual(query['invention_context']['elements'][0]['perspectives'][0]['class_mode'], 'text')
        self.assertEqual(query['invention_context']['elements'][0]['perspectives'][0]['classification_keys'], ['IPC:G01S13/00'])
        self.assertEqual(query['classifications'], [])
        body['elements'][0]['perspectives'][0]['classification_keys'] = ['IPC:UNKNOWN']
        with self.assertRaises(ValueError):
            self.compose(body)

    def test_not_requires_confirmation_and_selected_scope(self):
        body = payload(self.plan)
        body['exclusions'] = [dict(scope='e1', terms=['toy'])]
        with self.assertRaisesRegex(ValueError, 'NOT'):
            self.compose(body)
        body['confirm_exclusions'] = 1
        with self.assertRaisesRegex(ValueError, '真偽値'):
            self.compose(body)
        body['confirm_exclusions'] = True
        self.compose(body)
        for exclusion in (dict(scope='e2', terms=['toy']), dict(scope='all', terms=[]), dict(scope='all', terms=['toy OR car'])):
            body['exclusions'] = [exclusion]
            with self.subTest(exclusion=exclusion), self.assertRaises(ValueError):
                self.compose(body)

    def test_unknown_duplicate_omitted_or_empty_conditions_never_drop_silently(self):
        variants = []
        body = payload(self.plan); body['element_ids'] = ['unknown']; variants.append(body)
        body = payload(self.plan); body['element_ids'] *= 2; variants.append(body)
        body = payload(self.plan); body['elements'] *= 2; variants.append(body)
        body = payload(self.plan); body['elements'] = []; variants.append(body)
        body = payload(self.plan, ['e1', 'e2'], strategy_name='combination'); body['elements'].pop(); variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'] = []; variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0]['id'] = 'e2p1'; variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'] = [body['elements'][0]['perspectives'][0]] * 2; variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0]['terms'] = []; variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0]['terms'] = ['radar/TX']; variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0]['terms'] = ['x'] * 13; variants.append(body)
        body = payload(self.plan); body['perspective_joins'] = {'e2': 'and'}; variants.append(body)
        body = payload(self.plan); body['strategy'] = 'combination'; variants.append(body)
        body = payload(self.plan, ['e1', 'e2']); variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0].update(class_mode='or', classification_keys=[]); variants.append(body)
        body = payload(self.plan); body['elements'][0]['perspectives'][0].update(class_mode='class', classification_keys=['CPC:G01S']); variants.append(body)
        for body in variants:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.compose(body)

    def test_tree_node_limit_applies_after_composition(self):
        pool = []
        for number in range(1, 13):
            pool.append(dict(key=f'IPC:G01S13/{number:02}', kind='IPC', code=f'G01S13/{number:02}', selectable=True))
        plan = dict(elements=[])
        for number in range(1, 9):
            perspectives = [dict(id=f'e{number}p{i}', terms=[f'term{i}{j}' for j in range(12)],
                                 classification_keys=[row['key'] for row in pool]) for i in (1, 2)]
            plan['elements'].append(dict(id=f'e{number}', perspectives=perspectives))
        body = payload(plan, [e['id'] for e in plan['elements']], strategy_name='alternatives')
        body['confirm_class_intersection'] = True
        for element in body['elements']:
            for perspective in element['perspectives']:
                perspective['class_mode'] = 'or'
        with self.assertRaisesRegex(ValueError, '256'):
            strategy.compose_invention_query(plan, body, pool)


if __name__ == '__main__':
    unittest.main()
