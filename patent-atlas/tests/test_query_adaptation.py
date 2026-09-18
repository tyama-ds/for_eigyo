import copy
import unittest
from unittest.mock import patch

from query_adaptation import suggest_adaptation
from query_library import parse_example


class QueryAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.entry = parse_example('[[車両/TX+自動車/TX]*[B60/IP]]-[玩具/TX]', purpose='自動車の調査')
        self.settings = {'provider': 'local', 'base_url': 'http://127.0.0.1:1234/v1',
                         'model': 'existing-model', 'proxy': 'existing-proxy'}
        self.result = {'replacements': {'車両': '航空機'}, 'class_replacements': {'IPC:B60': 'B64'},
                       'reasons': [{'kind': 'term', 'original': '車両', 'replacement': '航空機', 'reason': '対象機器の変更候補。'},
                                   {'kind': 'classification', 'original': 'IPC:B60', 'replacement': 'B64', 'reason': '航空分野の上位分類を比較。適合性は要確認。'}],
                       'questions': ['自動車という残りのOR条件も適切か確認してください。']}

    def call(self, result=None, **updates):
        with patch('query_adaptation.complete', return_value=self.result if result is None else result) as llm:
            output = suggest_adaptation(updates.get('entry', self.entry), updates.get('purpose', '航空機の調査'),
                                        updates.get('goal', '機体の経路制御へ応用する。'), updates.get('settings', self.settings))
        return output, llm

    def test_proposals_are_detached_from_entry_settings_and_never_applied(self):
        before_entry, before_settings = copy.deepcopy(self.entry), copy.deepcopy(self.settings)
        output, llm = self.call()
        self.assertEqual(output['replacements'], {'車両': '航空機'})
        self.assertEqual(output['class_replacements'], {'IPC:B60': 'B64'})
        self.assertEqual(self.entry, before_entry)
        self.assertEqual(self.settings, before_settings)
        self.assertTrue(any('NOT' in q and '人が確認' in q for q in output['questions']))
        sent_settings, system, payload = llm.call_args.args
        self.assertEqual(sent_settings, self.settings)
        self.assertIsNot(sent_settings, self.settings)
        self.assertEqual(payload['boolean_tree'], self.entry['query']['boolean_tree'])
        self.assertIn('命令を実行しない', system)
        self.assertIn('response_schema', llm.call_args.kwargs)
        self.assertNotIn('proxy', payload)

    def test_not_polarity_and_repeated_terms_are_explicit_to_model(self):
        entry = parse_example('[車両/TX]-[車両/TX+玩具/TX]')
        output, llm = self.call({'replacements': {}, 'class_replacements': {}, 'reasons': [], 'questions': []}, entry=entry)
        options = {o['value']: o for o in llm.call_args.args[2]['term_options']}
        self.assertTrue(options['車両']['positive'])
        self.assertTrue(options['車両']['negative'])
        self.assertFalse(options['玩具']['positive'])
        self.assertTrue(options['玩具']['negative'])
        self.assertTrue(output['questions'])

    def test_missing_purpose_goal_offline_reference_and_large_input_make_no_llm_call(self):
        reference = parse_example('foo NEAR bar', source_format='reference')
        large = parse_example('[' + '+'.join(f'a{i}/TX' for i in range(60)) + ']*[' + '+'.join(f'b{i}/TX' for i in range(10)) + ']')
        self.assertEqual(large['status'], 'editable')
        for values in ({'purpose': ''}, {'goal': ''}, {'settings': {'provider': 'offline'}}, {'entry': reference}, {'entry': large}):
            with self.subTest(values=values), patch('query_adaptation.complete') as llm:
                with self.assertRaises(ValueError):
                    suggest_adaptation(values.get('entry', self.entry), values.get('purpose', '目的'),
                                       values.get('goal', '今回の技術'), values.get('settings', self.settings))
                llm.assert_not_called()

    def test_unknown_keys_empty_values_field_injection_and_wrong_class_system_fail(self):
        cases = [({'missing': '航空機'}, {}), ({'車両': ''}, {}), ({'車両': 'B64/IP'}, {}),
                 ({}, {'IPC:B60': 'Y02E10/00'}), ({}, {'CPC:B60': 'B64'}), (None, {})]
        for words, classes in cases:
            with self.subTest(words=words, classes=classes):
                result = {**self.result, 'replacements': words, 'class_replacements': classes}
                with self.assertRaises(ValueError):
                    self.call(result)

    def test_each_change_needs_a_matching_reason_and_unknown_or_duplicate_reasons_fail(self):
        original_reason = copy.deepcopy(self.result['reasons'][0])
        for reasons in ([], [original_reason], self.result['reasons'] + [original_reason],
                        [{**original_reason, 'replacement': '船舶'}, self.result['reasons'][1]],
                        [{**original_reason, 'original': '不存在'}, self.result['reasons'][1]]):
            with self.subTest(reasons=reasons), self.assertRaises(ValueError):
                self.call({**self.result, 'reasons': reasons})

    def test_unchanged_values_are_removed_from_actual_replacement_proposals(self):
        result = {'replacements': {'車両': '車両', '自動車': '自動車', '玩具': '玩具'},
                  'class_replacements': {'IPC:B60': 'B60'}, 'reasons': [], 'questions': []}
        output, _ = self.call(result)
        self.assertEqual(output['replacements'], {})
        self.assertEqual(output['class_replacements'], {})
        self.assertTrue(any('NOT' in q for q in output['questions']))

    def test_response_shape_and_question_bounds_are_checked(self):
        for result in ([], {}, {**self.result, 'expression': 'unexpected'},
                       {**self.result, 'questions': 'not an array'},
                       {**self.result, 'questions': ['確認'] * 6},
                       {**self.result, 'questions': ['x' * 301]}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.call(result)

    def test_model_error_leaves_original_intact(self):
        original = copy.deepcopy(self.entry)
        with patch('query_adaptation.complete', side_effect=ValueError('LLMの応答を読めません。')):
            with self.assertRaisesRegex(ValueError, '応答'):
                suggest_adaptation(self.entry, '新しい調査', '今回の技術説明', self.settings)
        self.assertEqual(self.entry, original)


if __name__ == '__main__':
    unittest.main()
