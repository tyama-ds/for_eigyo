import copy
import itertools
import json
import unittest

from query_library import adapt_example, parse_example, portable_example, validate_tree
from query_formats import build_tree, export_query


def evaluate(tree, values):
    op = tree['op']
    if op in ('text', 'class'):
        return values[tree['value']]
    children = [evaluate(c, values) for c in tree['children']]
    if op == 'and':
        return all(children)
    if op == 'or':
        return any(children)
    return children[0] and not children[1]


class QueryLibraryTests(unittest.TestCase):
    def test_jplatpat_nested_or_and_negative_scopes_are_preserved(self):
        entry = parse_example('[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX+ゲーム/TX]')
        self.assertEqual(entry['status'], 'editable')
        tree = entry['query']['boolean_tree']
        keys = ['車両', '自動車', 'B60', '玩具', 'ゲーム']
        for bits in itertools.product((False, True), repeat=len(keys)):
            v = dict(zip(keys, bits))
            self.assertEqual(evaluate(tree, v), (v['車両'] or v['自動車']) and v['B60'] and not (v['玩具'] or v['ゲーム']))
        self.assertEqual(entry['terms'], ['車両', '自動車', '玩具', 'ゲーム'])
        self.assertEqual(entry['classification_terms'], ['IPC:B60'])

    def test_negative_and_group_does_not_turn_into_negative_or(self):
        tree = parse_example('[車両/TX]-[[玩具/TX]*[模型/TX]]')['query']['boolean_tree']
        self.assertTrue(evaluate(tree, {'車両': True, '玩具': True, '模型': False}))
        self.assertFalse(evaluate(tree, {'車両': True, '玩具': True, '模型': True}))

    def test_prefix_classifications_and_multiple_systems_keep_types(self):
        entry = parse_example('[B/IP+B60/IP+B60W/IP+B60W30/00/IP+B60W30/00/CP+3D241BA01/FT]')
        self.assertEqual(entry['status'], 'editable', entry['issues'])
        self.assertEqual(entry['classification_terms'], ['IPC:B', 'IPC:B60', 'IPC:B60W', 'IPC:B60W30/00', 'CPC:B60W30/00', 'F-term:3D241BA01'])
        self.assertTrue(all(not c['verified'] for c in entry['query']['classifications']))

    def test_quoted_phrase_is_one_term_and_hyphen_is_literal(self):
        entry = parse_example("['fuel cell'/TX]*['solid-state battery'/TX]")
        self.assertEqual(entry['status'], 'editable')
        self.assertEqual(entry['terms'], ['fuel cell', 'solid-state battery'])
        alternate = parse_example('["fuel cell"/TX]')
        self.assertEqual(alternate['terms'], ['fuel cell'])

    def test_mixed_unbracketed_operators_do_not_guess_precedence(self):
        for text in ('車両/TX+制御/TX*B60/IP', '車両/TX-玩具/TX-模型/TX', 'A/TX B/TX'):
            with self.subTest(text=text):
                entry = parse_example(text)
                self.assertEqual(entry['status'], 'reference_only')
                self.assertIsNone(entry['query'])
                self.assertEqual(entry['original_text'], text)

    def test_unsupported_field_proximity_and_wildcards_remain_references(self):
        for text in ('車両/TI', '車両*/TX', '車両?/TX', '車両/TX NEAR 制御/TX', '車両/TX OR 制御/TX', '(車両/TX+制御/TX)', "['a*b'/TX]", 'fuel cell/TX'):
            with self.subTest(text=text):
                self.assertEqual(parse_example(text)['status'], 'reference_only')
        entry = parse_example('ftxt="vehicle" AND ipc=B60', 'espacenet')
        self.assertEqual(entry['status'], 'reference_only')
        with self.assertRaisesRegex(ValueError, '参照専用'):
            adapt_example(entry, {})

    def test_structured_json_legacy_snapshot_preserves_logic_and_metadata(self):
        data = {'schema_version': 1, 'name': '原式', 'purpose': '先行技術調査', 'facets': ['用途', '制御'], 'source_format': 'espacenet',
                'query': {'keywords': ['車両'], 'include_terms': ['運転', '制御'], 'exclude_terms': ['玩具'], 'classifications': [{'kind': 'IPC', 'code': 'B60', 'verified': True}]}}
        entry = parse_example(json.dumps(data, ensure_ascii=False), 'portable_json')
        self.assertEqual(entry['status'], 'editable', entry['issues'])
        self.assertEqual(entry['name'], '原式')
        self.assertEqual(entry['purpose'], '先行技術調査')
        self.assertEqual(entry['facets'], ['用途', '制御'])
        self.assertFalse(entry['query']['classifications'][0]['verified'])
        v = {'車両': True, '運転': False, '制御': True, '玩具': False, 'B60': True}
        self.assertTrue(evaluate(entry['query']['boolean_tree'], v))
        v['B60'] = False
        self.assertFalse(evaluate(entry['query']['boolean_tree'], v))

    def test_portable_round_trip_does_not_import_runtime_or_fabricated_verification(self):
        entry = parse_example('[車両/TX+自動車/TX]*[B60/IP]', name='車両')
        data = portable_example(entry)
        data['query']['api_key'] = 'do-not-import'
        data['query']['classifications'][0]['verified'] = True
        data['settings'] = {'proxy': 'do-not-import'}
        again = parse_example(json.dumps(data), 'portable_json')
        self.assertEqual(again['query']['boolean_tree'], entry['query']['boolean_tree'])
        self.assertNotIn('do-not-import', json.dumps(again['query']))
        self.assertFalse(again['query']['classifications'][0]['verified'])

    def test_adaptation_preserves_original_and_class_system_and_scopes(self):
        entry = parse_example('[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX]', purpose='車両の調査')
        original = copy.deepcopy(entry)
        query = adapt_example(entry, {'車両': '航空機', '玩具': '模型'}, {'IPC:B60': 'B64'})
        self.assertEqual(entry, original)
        self.assertEqual(query['provenance']['original_purpose'], '車両の調査')
        self.assertEqual(query['provenance']['example_id'], entry['id'])
        self.assertEqual(query['classifications'], [{'kind': 'IPC', 'code': 'B64', 'verified': False}])
        self.assertEqual(query['boolean_tree']['op'], 'not')
        self.assertEqual(query['boolean_tree']['children'][0]['children'][0]['op'], 'or')
        self.assertEqual(query['boolean_tree']['children'][1]['value'], '模型')
        self.assertNotIn('purpose', query)  # New purpose is explicit API/workbench input.

    def test_no_changes_still_produces_detached_preview(self):
        entry = parse_example('[車両/TX]*[B60/IP]')
        q = adapt_example(entry, {})
        self.assertEqual(q['boolean_tree'], entry['query']['boolean_tree'])
        q['boolean_tree']['children'][0]['value'] = 'changed'
        self.assertEqual(entry['query']['boolean_tree']['children'][0]['value'], '車両')

    def test_replacements_must_name_existing_full_terms_and_cannot_delete(self):
        entry = parse_example("['fuel cell'/TX]*[B60/IP]")
        for replacements, classes in [({'fuel': 'battery'}, {}), ({'fuel cell': ''}, {}), ({'fuel cell': 'x OR y/TX'}, {}), ({}, {'B60': 'B64'}), ({}, {'IPC:B60': ''}), ({}, {'IPC:B60': 'B64 OR B65'}), ({}, {'IPC:B60': 'Y02E10/00'})]:
            with self.subTest(replacements=replacements, classes=classes), self.assertRaises(ValueError):
                adapt_example(entry, replacements, classes)

    def test_invalid_json_is_rejected_and_json_expression_only_is_reference(self):
        for raw in ('{', '{"schema_version":1,"schema_version":1}', '{"v":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_example(raw, 'portable_json')
        entry = parse_example(json.dumps({'schema_version': 1, 'query': {'expression': 'vehicle OR motor'}}), 'portable_json')
        self.assertEqual(entry['status'], 'reference_only')

    def test_invalid_ast_extra_children_and_limits_cannot_escape_validation(self):
        for tree in ({'op': 'eval', 'value': 'x'}, {'op': 'not', 'children': [{'op': 'text', 'value': 'a'}]}, {'op': 'text', 'value': 'a', 'children': [{'op': 'text', 'value': 'b'}]}):
            with self.subTest(tree=tree), self.assertRaises(ValueError):
                validate_tree(tree)
        tree = {'op': 'text', 'value': 'x'}
        for _ in range(13):
            tree = {'op': 'and', 'children': [tree, {'op': 'text', 'value': 'y'}]}
        self.assertEqual(parse_example(json.dumps({'schema_version': 1, 'query': {'boolean_tree': tree}}), 'portable_json')['status'], 'reference_only')
        with self.assertRaises(ValueError):
            parse_example('x' * 100001)
        with self.assertRaises(ValueError):
            parse_example('x\x00/TX')

    def test_current_jplatpat_exports_can_be_imported_without_tree_loss(self):
        query = {'keywords': ['車両', '制御'], 'include_terms': ['運転', '誘導'], 'exclude_terms': ['玩具', '模型'], 'classifications': [{'kind': 'IPC', 'code': 'B60', 'verified': True}]}
        expression = export_query(query, 'jplatpat')['expression']
        entry = parse_example(expression)
        self.assertEqual(entry['status'], 'editable', (expression, entry['issues']))
        # Compare truth tables instead of superficial rendered formatting.
        keys = ['車両', '制御', '運転', '誘導', '玩具', '模型', 'B60']
        for bits in itertools.product((False, True), repeat=len(keys)):
            v = dict(zip(keys, bits))
            expected = v['車両'] and v['制御'] and (v['運転'] or v['誘導']) and v['B60'] and not (v['玩具'] or v['模型'])
            self.assertEqual(evaluate(entry['query']['boolean_tree'], v), expected)

    def test_original_paste_is_preserved_including_surrounding_whitespace(self):
        original = '\n  [車両/TX]*[B60/IP]  \n'
        entry = parse_example(original)
        self.assertEqual(entry['status'], 'editable')
        self.assertEqual(entry['original_text'], original)

    def test_imported_adapted_ast_round_trip_through_service_export_keeps_logic(self):
        entry = parse_example('[[車両/TX]-[[模型/TX]*[玩具/TX]]]+[B60/IP]')
        adapted = adapt_example(entry, {'車両': '航空機'}, {'IPC:B60': 'B64'})
        output = export_query(adapted, 'jplatpat', allow_unverified=True)
        again = parse_example(output['expression'])
        self.assertEqual(again['status'], 'editable', again['issues'])
        self.assertEqual(again['query']['boolean_tree'], adapted['boolean_tree'])
        self.assertEqual(again['query']['boolean_tree']['children'][0]['children'][1]['op'], 'and')

    def test_import_limits_match_exporter_children_limit(self):
        entry = parse_example('[' + '+'.join(f'word{i}/TX' for i in range(65)) + ']')
        self.assertEqual(entry['status'], 'reference_only')


if __name__ == '__main__':
    unittest.main()
